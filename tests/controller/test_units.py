# SPDX-License-Identifier: MIT
"""Unit tests of the controller (docs/08 §9): scenario, timeline, auth, lock-out, AC6, store.
  python3 -m pytest tests/controller/test_units.py
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest
from pydantic import ValidationError

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "controller"))
from tgen_controller import auth  # noqa: E402
from tgen_controller.health import Health  # noqa: E402
from tgen_controller.scenario import PRESETS, Phase, Scenario, expand_churn  # noqa: E402
from tgen_controller.store import Store  # noqa: E402

DUT = {"ip": "10.100.0.16", "configure": False}


def test_presets_validate():
    for name, p in PRESETS.items():
        Scenario.model_validate({"name": name, "dut": DUT, "phases": p["phases"]})


def test_first_phase_must_be_establish():
    with pytest.raises(ValidationError):
        Scenario.model_validate({"dut": DUT, "phases": [{"type": "announce"}]})


def test_announce_required():
    with pytest.raises(ValidationError):
        Scenario.model_validate({"dut": DUT, "phases": [{"type": "establish"}, {"type": "steady", "duration_s": 5}]})


def test_duration_required():
    with pytest.raises(ValidationError):
        Phase.model_validate({"type": "churn", "rate": 10})


def test_password_never_dumped():
    sc = Scenario.model_validate({"dut": DUT | {"password": "secret"}, "phases": PRESETS["quick_check"]["phases"]})
    assert sc.dut.password == "secret"
    assert "secret" not in sc.model_dump_json()


def test_churn_default_mix():
    assert Phase(type="churn", rate=5, duration_s=10).mix == {"announce": 40, "withdraw": 40, "attr": 20}


def test_expand_churn_steps():
    p = Phase.model_validate({"type": "churn", "rate": 100, "step": {"add": 100, "every_s": 30, "max": 250},
                              "duration_s": 120})
    assert expand_churn(p) == [(0.0, 100), (30.0, 200), (60.0, 250), (90.0, 250)]
    assert expand_churn(Phase(type="churn", rate=7, duration_s=9)) == [(0.0, 7)]


def test_password_hash():
    h = auth.hash_password("correct horse")
    assert h.startswith("scrypt$") and auth.check_password("correct horse", h)
    assert not auth.check_password("wrong", h) and not auth.check_password("x", "garbage")


def test_session_token():
    key = b"k" * 32
    t = auth.make_token(key, 1)
    assert auth.valid_token(key, t)
    assert not auth.valid_token(b"x" * 32, t)
    assert not auth.valid_token(key, t[:-1] + ("0" if t[-1] != "0" else "1"))
    assert not auth.valid_token(key, auth.make_token(key, -1))      # expired
    assert not auth.valid_token(key, None)


def test_lockout():
    lo = auth.Lockout(5, 900, 900)
    for _ in range(4):
        lo.failure("a")
    assert not lo.is_blocked("a")
    lo.failure("a")
    assert lo.is_blocked("a") > 800 and not lo.is_blocked("b")
    lo2 = auth.Lockout(5, 900, 900)
    for _ in range(4):
        lo2.failure("a")
    lo2.success("a")
    lo2.failure("a")
    assert not lo2.is_blocked("a")


def test_health_violations():
    h = Health(mem_reserve_mb=0)
    for _ in range(20):
        h.request(5)
    assert not h.violations()
    for _ in range(20):
        h.request(900)
    assert any("API p95" in v for v in h.violations())
    h2 = Health(mem_reserve_mb=0)
    h2.ws_sent(2500)
    assert any("live delay" in v for v in h2.violations()) and h2.run_max["ws_delay_ms"] == 2500
    h2.reset_run()
    assert h2.run_max["ws_delay_ms"] == 0


def test_store(tmp_path):
    s = Store(tmp_path / "c.db")
    s.create_run("r1", "x", {"a": 1}, str(tmp_path))
    s.update_run("r1", state="running", summary={"k": 2})
    s.phase("r1", 0, "establish", {}, 1)
    s.phase_end("r1", 0, 2)
    s.event("r1", "phase", "info", "hello")
    s.tick("r1", 3, {"type": "tick"})
    r = s.run("r1")
    assert r["state"] == "running" and r["summary"] == {"k": 2} and r["scenario"] == {"a": 1}
    assert s.phases("r1")[0]["end_ms"] == 2 and s.events("r1")[0]["text"] == "hello" and len(s.ticks("r1")) == 1
    assert s.fail_unfinished() == ["r1"] and s.run("r1")["state"] == "failed"
    s.delete_run("r1")
    assert s.run("r1") is None and not s.ticks("r1")
