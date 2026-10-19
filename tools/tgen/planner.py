# SPDX-License-Identifier: MIT
"""Builds per-node agent plans for a BGP benchmark run (spec §4, §5, §10.2)."""
from __future__ import annotations

import ipaddress
import time
from dataclasses import dataclass, field

from .prefixes import generate_asns, generate_prefixes


@dataclass
class NodeSpec:
    name: str
    weight: float = 1.0            # CMNODE01 = 0.7 (control-plane reservation)
    cores: list[int] = field(default_factory=list)


@dataclass
class Scenario:
    run_id: str
    dut_ip: str
    dut_asn: int
    role: str = "route_server"     # route_server | route_reflector | router
    routers_total: int = 60
    prefixes_per_router: int = 10_000
    pool: list[str] = field(default_factory=lambda: ["10.0.0.0/8"])
    exclude: list[str] = field(default_factory=lambda: ["10.100.0.0/16"])
    len_min: int = 24
    len_max: int = 28
    router_ip_first: str = "10.100.0.20"
    seed: int = 1
    hold_time: int = 90
    announce_rate: float = 0.0     # prefixes/s per router, 0 = max
    per_update: int = 0
    sample_mod: int = 100
    establish_delay_s: float = 10.0
    establish_window_s: float = 20.0
    converge_timeout_s: float = 300.0
    withdraw: bool = True
    withdraw_timeout_s: float = 120.0
    establish_per_s: float = 0.0


def split(total: int, nodes: list[NodeSpec]) -> list[int]:
    """Largest-remainder split of `total` by node weight."""
    wsum = sum(n.weight for n in nodes)
    raw = [total * n.weight / wsum for n in nodes]
    out = [int(r) for r in raw]
    for i in sorted(range(len(nodes)), key=lambda i: raw[i] - out[i], reverse=True)[: total - sum(out)]:
        out[i] += 1
    return out


def build(sc: Scenario, nodes: list[NodeSpec], now_ms: int | None = None) -> tuple[dict[str, dict], dict]:
    counts = split(sc.routers_total, nodes)
    asns = generate_asns(sc.routers_total, sc.seed, exclude=[sc.dut_asn])
    taken: set = set()
    first_ip = int(ipaddress.ip_address(sc.router_ip_first))
    routers = []
    idx = 0
    for node, n in zip(nodes, counts):
        for _ in range(n):
            ip = str(ipaddress.ip_address(first_ip + idx))
            routers.append({
                "name": f"R{idx + 1:02d}", "node": node.name, "asn": asns[idx], "ip": ip,
                "prefixes": generate_prefixes(sc.prefixes_per_router, sc.seed * 1000 + idx, sc.pool,
                                              sc.len_min, sc.len_max, sc.exclude, taken),
            })
            idx += 1
    total_prefixes = sum(len(r["prefixes"]) for r in routers)


    plans: dict[str, dict] = {}
    for node in nodes:
        mine = [r for r in routers if r["node"] == node.name]
        plans[node.name] = {
            "run_id": sc.run_id, "node": node.name,
            "dut": {"ip": sc.dut_ip, "asn": sc.dut_asn, "role": sc.role},
            "hold_time": sc.hold_time, "announce_rate": sc.announce_rate, "per_update": sc.per_update,
            "sample_mod": sc.sample_mod, "cores": node.cores,
            "timing": {},
            "routers": [{
                "name": r["name"], "asn": r["asn"], "ip": r["ip"], "prefixes": r["prefixes"],
                "receive": "timestamps",
                # the DUT reflects everyone else's routes to this router
                "expected_rx": total_prefixes - len(r["prefixes"]),
            } for r in mine],
        }
    meta = {
        "run_id": sc.run_id, "scenario": sc.__dict__, "nodes": [n.__dict__ for n in nodes],
        "router_counts": dict(zip([n.name for n in nodes], counts)),
        "timing": {},
        "routers": [{k: v for k, v in r.items() if k != "prefixes"} | {"prefix_count": len(r["prefixes"])}
                    for r in routers],
        "total_prefixes": total_prefixes,
    }
    set_timeline(plans, meta, sc, now_ms)
    return plans, meta


def set_timeline(plans: dict[str, dict], meta: dict, sc: Scenario, now_ms: int | None = None) -> None:
    """(Re)sets the absolute timeline. Call it right before the agents start:
    prefix generation and deployment can take a while."""
    now_ms = now_ms or int(time.time() * 1000)
    t_est = now_ms + int(sc.establish_delay_s * 1000)
    t_ann = t_est + int(sc.establish_window_s * 1000)
    t_wd = t_ann + int(sc.converge_timeout_s * 1000) if sc.withdraw else None
    t_end = (t_wd if t_wd else t_ann + int(sc.converge_timeout_s * 1000)) + int(sc.withdraw_timeout_s * 1000)
    timing = {"establish_at": t_est, "announce_at": t_ann, "withdraw_at": t_wd, "end_at": t_end}
    meta["timing"] = dict(timing)
    for p in plans.values():
        p["timing"] = timing | {"establish_per_s": sc.establish_per_s}


def bird_config(meta: dict, dut_ip: str, dut_asn: int, multihop: bool = False, log_file: str | None = None) -> str:
    """BIRD 2 route-server (or reflector) configuration for all emulated routers."""
    role = meta["scenario"]["role"]
    lines = [
        f'log "{log_file}" all;' if log_file else "log stderr all;",
        f"router id {dut_ip};",
        "protocol device {}",
        "template bgp tgen {",
        f"  local {dut_ip} as {dut_asn};",
        "  passive on;",
        "  hold time 90;",
        "  rs client;" if role == "route_server" else ("  rr client;" if role == "route_reflector" else ""),
        "  multihop 2;" if multihop else "",
        "  ipv4 { import all; export all;" + (" next hop self;" if role == "router" else "") + " };",
        "}",
    ]
    for r in meta["routers"]:
        asn = dut_asn if role == "route_reflector" else r["asn"]
        lines.append(f'protocol bgp {r["name"]} from tgen {{ neighbor {r["ip"]} as {asn}; }}')
    return "\n".join(l for l in lines if l) + "\n"
