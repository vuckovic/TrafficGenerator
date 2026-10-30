# SPDX-License-Identifier: MIT
"""Scenario model and presets (spec §5, docs/08 §4)."""
from __future__ import annotations

import copy
from typing import Literal

from pydantic import BaseModel, Field, model_validator

PhaseType = Literal["establish", "announce", "steady", "churn", "flap", "withdraw", "teardown"]


class Step(BaseModel):
    add: float = Field(gt=0)
    every_s: float = Field(gt=0)
    max: float = Field(gt=0)


class Phase(BaseModel):
    type: PhaseType
    per_s: float = Field(0, ge=0)                     # establish stagger
    rate: float = Field(0, ge=0)                      # announce/withdraw/teardown: prefixes/s, churn: ops/s
    mix: dict[str, float] | None = None               # churn
    step: Step | None = None                          # churn: stepped load
    kind: Literal["reset", "silent", "withdraw_all"] = "reset"   # flap
    routers: int | list[str] = 1                      # flap: how many (or which) routers
    down_s: float = Field(5, ge=0)
    duration_s: float = Field(0, ge=0)                # steady / churn / flap
    timeout_s: float = Field(0, ge=0)                 # establish / announce / withdraw / teardown (0 = default)

    @model_validator(mode="after")
    def _check(self):
        if self.type in ("steady", "churn", "flap") and self.duration_s <= 0:
            raise ValueError(f"{self.type}: duration_s must be > 0")
        if self.type == "churn" and not self.mix:
            self.mix = {"announce": 40, "withdraw": 40, "attr": 20}
        return self


class Dut(BaseModel):
    profile: str | None = None
    adapter: str | None = None            # mikrotik | cisco | bird-ssh | frr | quagga
    ip: str | None = None
    ssh_host: str | None = None
    user: str | None = None
    asn: int | None = None
    role: Literal["route_server", "route_reflector", "router"] | None = None
    mrai: float | None = None
    configure: bool = True                # push the neighbours before the run (needs an adapter)
    keep_config: bool = False
    password: str | None = Field(None, exclude=True)   # never stored
    poll_s: float = 10


class Routers(BaseModel):
    total: int = Field(60, ge=1, le=600)
    prefixes: int = Field(10_000, ge=1, le=200_000)
    per_update: int = Field(0, ge=0)
    seed: int = 1
    hold_time: int = Field(90, ge=3)


class Limits(BaseModel):
    max_duration_s: float = Field(3600, gt=0)


class Scenario(BaseModel):
    name: str = "run"
    dut: Dut
    routers: Routers = Routers()
    phases: list[Phase]
    limits: Limits = Limits()

    @model_validator(mode="after")
    def _check(self):
        types = [p.type for p in self.phases]
        if not types or types[0] != "establish":
            raise ValueError("the first phase must be establish")
        if "announce" not in types:
            raise ValueError("an announce phase (initial load) is required")
        if types.count("establish") > 1:
            raise ValueError("only one establish phase")
        return self


DEFAULT_TIMEOUT = {"establish": 60.0, "announce": 300.0, "withdraw": 120.0, "teardown": 120.0}

_P = {
    "establish": {"type": "establish", "per_s": 0},
    "announce": {"type": "announce", "rate": 0},
    "steady": {"type": "steady", "duration_s": 60},
    "teardown": {"type": "teardown", "rate": 0},
}
PRESETS = {
    "quick_check": {"label": "Quick check", "phases": [_P["establish"], _P["announce"], _P["steady"], _P["teardown"]]},
    "initial_convergence": {"label": "Initial convergence", "phases": [_P["establish"], _P["announce"], _P["teardown"]]},
    "throughput_knee": {"label": "Throughput knee", "phases": [
        _P["establish"], _P["announce"],
        {"type": "churn", "rate": 100, "mix": {"announce": 40, "withdraw": 40, "attr": 20},
         "step": {"add": 100, "every_s": 30, "max": 2000}, "duration_s": 600},
        _P["teardown"]]},
    "resilience": {"label": "Resilience", "phases": [
        _P["establish"], _P["announce"], _P["steady"],
        {"type": "flap", "kind": "reset", "routers": 3, "down_s": 10, "duration_s": 60},
        _P["steady"], _P["teardown"]]},
    "full_ixp_day": {"label": "Full IXP day", "phases": [
        _P["establish"], _P["announce"], {"type": "steady", "duration_s": 120},
        {"type": "churn", "rate": 100, "mix": {"announce": 40, "withdraw": 40, "attr": 20},
         "step": {"add": 100, "every_s": 60, "max": 1000}, "duration_s": 900},
        {"type": "flap", "kind": "reset", "routers": 3, "down_s": 10, "duration_s": 120},
        {"type": "flap", "kind": "silent", "routers": 2, "down_s": 1, "duration_s": 300},
        {"type": "steady", "duration_s": 120}, _P["teardown"]]},
}


def presets() -> dict:
    return copy.deepcopy(PRESETS)


def expand_churn(p: Phase) -> list[tuple[float, float]]:
    """Stepped churn -> [(offset_s, rate), ...] within the phase's duration."""
    if not p.step:
        return [(0.0, p.rate)]
    out, t, r = [], 0.0, p.rate
    while t < p.duration_s:
        out.append((t, min(r, p.step.max)))
        t += p.step.every_s
        r += p.step.add
    return out
