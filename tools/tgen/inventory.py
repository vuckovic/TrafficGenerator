# SPDX-License-Identifier: MIT
"""DUT inventory: deploy/duts.toml (or deploy/duts.example.toml).

One profile per device. A profile fills in command-line defaults; anything given
on the command line wins. All peer addresses are kept out of the router pool.
"""
from __future__ import annotations

import argparse
import os
import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
ADAPTERS = {"mikrotik", "cisco", "bird-ssh", "frr", "quagga"}
ROLES = {"router", "route_reflector", "route_server"}

# profile field -> argparse dest (tgen_p2.py / tgen_dut.py)
ARG_MAP = {"adapter": "dut", "peer_ip": "dut_ip", "ssh_host": "dut_ssh_host", "ssh_port": "dut_port",
           "user": "dut_user", "asn": "dut_asn", "role": "role", "max_routes": "dut_max_routes",
           "min_free_mem_pct": "dut_min_free_mem_pct", "link_mbps": "dut_link_mbps",
           "ssh_legacy": "dut_ssh_legacy", "interface": "dut_interface", "mrai_s": "dut_mrai"}


class InventoryError(ValueError):
    pass


def path() -> Path:
    if os.environ.get("TGEN_DUTS_FILE"):            # tests
        return Path(os.environ["TGEN_DUTS_FILE"])
    own = REPO / "deploy" / "duts.toml"
    return own if own.exists() else REPO / "deploy" / "duts.example.toml"


def load(p: Path | None = None) -> dict[str, dict]:
    p = p or path()
    if not p.exists():
        return {}
    try:
        data = tomllib.loads(p.read_text())
    except tomllib.TOMLDecodeError as e:
        raise InventoryError(f"{p}: {e}") from e
    out = {}
    for name, d in data.items():
        if not isinstance(d, dict):
            continue
        if d.get("adapter") not in ADAPTERS:
            raise InventoryError(f"{p} [{name}]: adapter must be one of {sorted(ADAPTERS)}")
        if not d.get("peer_ip"):
            raise InventoryError(f"{p} [{name}]: peer_ip missing")
        if d.get("role", "router") not in ROLES:
            raise InventoryError(f"{p} [{name}]: role must be one of {sorted(ROLES)}")
        d = dict(d)
        d.setdefault("label", name)
        d.setdefault("ssh_host", d["peer_ip"])
        d.setdefault("role", "router")
        d.setdefault("link_mbps", 1000)
        d.setdefault("min_free_mem_pct", 0)
        d.setdefault("ssh_legacy", False)
        d["name"] = name
        out[name] = d
    return out


def reserved_ips(inv: dict[str, dict] | None = None) -> list[str]:
    inv = load() if inv is None else inv
    return [d["peer_ip"] for d in inv.values()]


def effective_mrai(prof: dict | None, override: float | None = None) -> float:
    """Seconds the DUT may hold back updates per neighbour (MRAI): what we push, else what
    the device does by default (profile device_mrai_s), else 0."""
    if override is not None:
        return float(override)
    if not prof:
        return 0.0
    return float(prof["mrai_s"] if prof.get("mrai_s") is not None else prof.get("device_mrai_s", 0))


def password_env(name: str) -> str:
    return "TGEN_DUT_PASSWORD_" + "".join(c if c.isalnum() else "_" for c in name).upper()


def apply_profile(ap: argparse.ArgumentParser, argv: list[str] | None = None) -> dict | None:
    """If --dut-profile NAME is given, set the parser's defaults from that profile
    (call before ap.parse_args). Also maps TGEN_DUT_PASSWORD_<NAME> to
    TGEN_DUT_PASSWORD when the latter is not set. Returns the profile or None."""
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--dut-profile", default=None)
    known, _ = pre.parse_known_args(argv)
    if not known.dut_profile:
        return None
    inv = load()
    if known.dut_profile not in inv:
        ap.error(f"unknown --dut-profile {known.dut_profile}; profiles in {path().name}: {', '.join(inv) or 'none'}")
    prof = inv[known.dut_profile]
    dests = {a.dest for a in ap._actions}
    ap.set_defaults(**{dest: prof[k] for k, dest in ARG_MAP.items() if k in prof and dest in dests})
    pw = os.environ.get(password_env(prof["name"]))
    if pw and not os.environ.get("TGEN_DUT_PASSWORD"):
        os.environ["TGEN_DUT_PASSWORD"] = pw
    return prof
