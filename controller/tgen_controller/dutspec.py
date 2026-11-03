# SPDX-License-Identifier: MIT
"""One place that turns a scenario's `dut` block (a profile from the inventory, explicit
fields, or both: explicit fields win for this run only) into the resolved facts and an
adapter. Used by the engine and by the UI's DUT buttons (U0, U8-U18)."""
from __future__ import annotations

import os

DEFAULT_ROLE = {"mikrotik": "router"}


def resolve(d, inv: dict | None = None) -> dict:
    """d: scenario.Dut. Returns the facts (no secrets)."""
    from tgen import inventory

    inv = inventory.load() if inv is None else inv
    prof = inv.get(d.profile) if d.profile else None
    if d.profile and not prof:
        raise ValueError(f"unknown DUT profile {d.profile}")
    p = prof or {}
    adapter = d.adapter or p.get("adapter")
    ip = d.ip or p.get("peer_ip")
    if not ip:
        raise ValueError("DUT address missing (profile or dut.ip)")

    def pick(field, key, default=None):
        v = getattr(d, field)
        return v if v is not None else p.get(key, default)

    return {
        "profile": d.profile, "label": p.get("label") or adapter or "external", "adapter": adapter, "ip": ip,
        "ssh_host": d.ssh_host or p.get("ssh_host") or ip, "ssh_port": pick("ssh_port", "ssh_port", 22),
        "user": d.user or p.get("user"), "asn": pick("asn", "asn", 64500),
        "role": d.role or p.get("role") or DEFAULT_ROLE.get(adapter or "", "route_server"),
        "mrai": pick("mrai", "mrai_s"), "device_mrai_s": p.get("device_mrai_s"),
        "link_mbps": pick("link_mbps", "link_mbps", 1000), "max_routes": pick("max_routes", "max_routes"),
        "min_free_mem_pct": pick("min_free_mem_pct", "min_free_mem_pct", 0) or 0,
        "interface": d.interface or p.get("interface"), "ssh_legacy": pick("ssh_legacy", "ssh_legacy", False),
        "sudo": d.sudo, "bird_include": d.bird_include,
    }


def password(d) -> str | None:
    from tgen import inventory
    if d.password:
        return d.password
    if d.profile:
        return os.environ.get(inventory.password_env(d.profile)) or None
    return None


def make_dut(info: dict, d):
    """Adapter for the resolved facts; password and enable secret from the request only
    (or the controller's TGEN_DUT_PASSWORD_<PROFILE> environment)."""
    from tgen.dut import make
    opts = {"ssh_legacy": info["ssh_legacy"], "mrai": info["mrai"]}
    if info["sudo"] is False:
        opts["no_sudo"] = True
    if info["bird_include"]:
        opts["bird_include"] = info["bird_include"]
    dut = make(info["adapter"], info["ssh_host"], info["user"], port=info["ssh_port"], password=password(d), **opts)
    if getattr(d, "enable", None) and hasattr(dut, "secret"):
        dut.secret = d.enable
    return dut
