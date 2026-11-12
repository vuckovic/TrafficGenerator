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
        "snmp": bool(d.snmp if d.snmp is not None else p.get("snmp", False)),
        "snmp_host": d.snmp_host or p.get("snmp_host") or ip, "snmp_port": pick("snmp_port", "snmp_port", 161),
    }


def snmp_community(d) -> str | None:
    """U5: from the request, else the controller's TGEN_DUT_SNMP_<PROFILE> environment; never stored."""
    from tgen.snmp import community_env
    if d.snmp_community:
        return d.snmp_community
    if d.profile:
        return os.environ.get(community_env(d.profile)) or None
    return None


def snmp_test(d, routers_ips: set[str] | None = None) -> dict:
    """U5 Test button: system group and one poll (no rates yet)."""
    from tgen import snmp
    info = resolve(d)
    out: dict = {"action": "snmp", "host": info["snmp_host"], "port": info["snmp_port"]}
    comm = snmp_community(d)
    if not comm:
        return out | {"ok": False, "error": "no SNMP community: enter it, or set " +
                      (snmp.community_env(d.profile) if d.profile else "TGEN_DUT_SNMP_<PROFILE>") + " for the controller"}
    try:
        s = snmp.Session(info["snmp_host"], comm, port=info["snmp_port"])
        out["system"] = s.system()
        p = s.poll(info["interface"], routers_ips)
        out["poll"] = {k: v for k, v in p.items() if k != "t"}
        out["ok"] = True
    except snmp.SnmpError as e:
        out.update(ok=False, error=str(e))
    return out


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


def action(d, action: str, routers: int, cluster_env: str) -> dict:
    """The DUT buttons (U14) and the scheme's connection test: test, preview, push, remove, status.
    Raises ValueError for a request that cannot be resolved."""
    import contextlib
    from tgen.dut import DutError
    from tgen_dut import plan_meta

    info = resolve(d)
    if not info["adapter"]:
        raise ValueError("no platform: choose one (U8)")
    ip, asn, role = info["ip"], info["asn"], info["role"]
    dut = make_dut(info, d)

    class A:  # what plan_meta() reads from the CLI arguments
        seed, dut_ip, dut_asn, config = 1, ip, asn, cluster_env
    A.routers, A.role = routers, role
    out: dict = {"action": action, "platform": dut.platform, "ip": ip, "role": role}
    try:
        if action == "preview":
            out["config"] = dut.render(plan_meta(A), ip, asn, role)
        elif action == "test":
            out["resources"] = dut.resources()
            out["tgen_neighbours"] = dut.count()
            with contextlib.suppress(DutError, NotImplementedError):
                out["port"] = dut.interface_info(ip, info["interface"])
        elif action == "push":
            out["neighbours"] = dut.push(dut.render(plan_meta(A), ip, asn, role), routers)
        elif action == "remove":
            dut.remove()
            out["tgen_neighbours"] = dut.count()
        elif action == "status":
            out["sessions"], out["established"] = dut.sessions()
        out["ok"] = True
    except (DutError, OSError, NotImplementedError) as e:
        out.update(ok=False, error=str(e)[:1000])
    finally:
        close = getattr(dut, "close", None)
        if close:
            close()
    out["notes"] = dut.notes
    return out
