# SPDX-License-Identifier: MIT
"""Turns agent results into the benchmark metrics of spec §6.1."""
from __future__ import annotations

import statistics


def pct(values: list[float], q: float) -> float | None:
    if not values:
        return None
    v = sorted(values)
    return v[min(len(v) - 1, int(round(q * (len(v) - 1))))]


def dist(values: list[float]) -> dict:
    return {"n": len(values), "p50": pct(values, 0.5), "p90": pct(values, 0.9), "p99": pct(values, 0.99),
            "max": max(values) if values else None, "mean": statistics.fmean(values) if values else None}


def analyze(meta: dict, results: list[dict]) -> dict:
    t = meta["timing"]
    ann_ns, wd_ns = t["announce_at"] * 1_000_000, (t["withdraw_at"] or 0) * 1_000_000
    est_ns = t["establish_at"] * 1_000_000
    routers = [r | {"node": res["node"]} for res in results for r in res["routers"]]
    n = len(routers)
    s = lambda ns: ns / 1e9  # noqa: E731
    ms = lambda ns: ns / 1e6  # noqa: E731

    est = [r for r in routers if r["t_established"]]
    setup = [ms(r["t_established"] - r["t_connect_start"]) for r in est if r["t_connect_start"]]
    sessions = {
        "configured": n, "established": len(est),
        "setup_ms": dist(setup),
        "all_established_after_s": s(max(r["t_established"] for r in est) - est_ns) if est else None,
        "resets": sum(r["resets"] for r in routers),
        "notifications_rx": [[r["name"], *x] for r in routers for x in r["notifications_rx"]],
        "max_keepalive_gap_ms": max((r["max_keepalive_gap_ms"] for r in routers), default=0),
    }

    conv = [s(r["t_converged"] - ann_ns) for r in routers if r["t_converged"]]
    not_conv = [{"router": r["name"], "held": r["routes_peak"], "expected": r["expected_rx"]}
                for r in routers if r["expected_rx"] and not r["t_converged"]]
    first_send = min((r["t_first_send"] for r in routers if r["t_first_send"]), default=0)
    last_send = max((r["t_last_send"] for r in routers if r["t_last_send"]), default=0)
    convergence = {
        "receivers_converged": len(conv), "receivers": n,
        "initial_s": max(conv) if conv and len(conv) == n else None,
        "per_receiver_s": dist(conv),
        "not_converged": not_conv[:20],
        "send_window_s": s(last_send - first_send) if first_send else None,
        # paced runs: how long after our last announcement every receiver was complete (the DUT's lag)
        "after_last_send_s": (s(max(r["t_converged"] for r in routers) - last_send)
                              if last_send and len(conv) == n and n else None),
    }

    def latency(tx_key: str, rx_key: str) -> dict:
        tx = {}
        for r in routers:
            for k, ts in r["samples"][tx_key]:
                tx[k] = ts
        lat = [ms(ts - tx[k]) for r in routers for k, ts in r["samples"][rx_key] if k in tx]
        return dist(lat)

    lat_ann, lat_wd = latency("tx", "rx"), latency("wd_tx", "wd_rx")

    empties = [r["t_empty_after_withdraw"] for r in routers if r["t_empty_after_withdraw"]]
    withdraw = {
        "receivers_empty": len(empties),
        "convergence_s": s(max(empties) - wd_ns) if wd_ns and len(empties) == n else None,
    } if wd_ns else None

    rx_first = [r["t_first_rx"] for r in routers if r["t_first_rx"]]
    rx_conv = [r["t_converged"] for r in routers if r["t_converged"]]
    nlri_initial = sum(min(r["nlri_rx"], r["expected_rx"]) for r in routers)
    dut_out = None
    if rx_first and rx_conv:
        span = s(max(rx_conv) - min(rx_first))
        dut_out = {"prefixes_per_s": nlri_initial / span if span > 0 else None,
                   "updates_rx_total": sum(r["updates_rx"] for r in routers),
                   "prefixes_per_update": (sum(r["nlri_rx"] for r in routers) /
                                           max(1, sum(r["updates_rx"] for r in routers))),
                   # BGP bytes the DUT sent until each receiver converged (agents >= 0.3.1)
                   "payload_mbps": (sum(r.get("bytes_rx_at_converged", 0) for r in routers) * 8 / span / 1e6
                                    if span >= 1 and any("bytes_rx_at_converged" in r for r in routers) else None)}  # < 1 s: too short

    role = meta["scenario"]["role"]
    checks = {k: sum(r["checks"][k] for r in routers) for k in routers[0]["checks"]} if routers else {}
    rx_total = sum(r["nlri_rx"] for r in routers)
    correctness = {
        "missing_routes": sum(max(0, r["expected_rx"] - r["routes_peak"]) for r in routers),
        "extra_routes": sum(max(0, r["routes_peak"] - r["expected_rx"]) for r in routers),
        **checks,
        # route server: must add nothing; reflector: must add ORIGINATOR_ID; router: must
        # add its AS and set itself as next hop on every route it passes on
        "attributes": unexpected_attributes(routers, role),
        "attribute_violations": (checks.get("paths_with_dut_asn", 0) + checks.get("next_hop_is_dut", 0))
        if role == "route_server" else checks.get("paths_without_originator", 0) if role == "route_reflector" else
        max(0, rx_total - checks.get("paths_with_dut_asn", 0)) + max(0, rx_total - checks.get("next_hop_is_dut", 0)),
    }

    lags = [r["send_lag_ms"]["p99"] for r in routers if r["send_lag_ms"]["n"]]
    generator = {
        "send_lag_p99_ms": max(lags) if lags else None,
        "nodes": [{"node": res["node"], "routers": len(res["routers"]), "cpu_s": res["cpu_user_s"] + res["cpu_sys_s"],
                   "wall_s": s(res["t_end"] - res["t_start"]), "max_rss_mb": res["max_rss_mb"]} for res in results],
        "errors": [[r["name"], e] for r in routers for e in r["errors"]][:20],
    }
    return {"run_id": meta["run_id"], "sessions": sessions, "convergence": convergence,
            "latency_announce_ms": lat_ann, "latency_withdraw_ms": lat_wd, "withdraw": withdraw,
            "dut_output": dut_out, "correctness": correctness, "generator": generator}


ATTR_NAMES = {1: "ORIGIN", 2: "AS_PATH", 3: "NEXT_HOP", 4: "MED", 5: "LOCAL_PREF", 6: "ATOMIC_AGGREGATE",
              7: "AGGREGATOR", 8: "COMMUNITIES", 9: "ORIGINATOR_ID", 10: "CLUSTER_LIST", 14: "MP_REACH_NLRI",
              15: "MP_UNREACH_NLRI", 16: "EXT_COMMUNITIES", 17: "AS4_PATH", 18: "AS4_AGGREGATOR",
              32: "LARGE_COMMUNITY", 35: "OTC"}
# attributes a DUT in this role may (or must) add to the routes it passes on
ROLE_MAY_ADD = {"route_server": set(), "route_reflector": {5, 9, 10}, "router": set()}


def attr_name(t: int) -> str:
    return f"{ATTR_NAMES.get(t, 'type')}({t})"


def unexpected_attributes(routers: list[dict], role: str) -> dict:
    """Attribute types the DUT added that neither our routers sent nor the role allows."""
    sent = set().union(*(set(r.get("attrs_sent", [])) for r in routers)) if routers else set()
    rx: dict[int, int] = {}
    for r in routers:
        for t, n in (r.get("attrs_rx") or {}).items():
            rx[int(t)] = rx.get(int(t), 0) + n
    allowed = sent | ROLE_MAY_ADD.get(role, set())
    return {"sent": sorted(sent), "received": {attr_name(t): n for t, n in sorted(rx.items())},
            "unexpected": {attr_name(t): n for t, n in sorted(rx.items()) if t not in allowed},
            "dropped": [attr_name(t) for t in sorted(sent) if t not in rx and t not in (4, 5)]}


EXPECT = {"route_server": "route server adds no AS and keeps the next hop",
          "route_reflector": "reflector adds ORIGINATOR_ID",
          "router": "router adds its AS and sets itself as next hop"}


def attribute_detail(report: dict, role: str) -> str:
    c = report["correctness"]
    if role == "route_server":
        return (f'DUT AS in AS_PATH on {c.get("paths_with_dut_asn", 0):,} routes, '
                f'next hop changed to the DUT on {c.get("next_hop_is_dut", 0):,}')
    if role == "route_reflector":
        return f'no ORIGINATOR_ID on {c.get("paths_without_originator", 0):,} routes'
    return (f'DUT AS in AS_PATH on {c.get("paths_with_dut_asn", 0):,} routes, next hop = DUT on '
            f'{c.get("next_hop_is_dut", 0):,}; {c["attribute_violations"]:,} missing')


def gate(report: dict, cpu: dict[str, float] | None = None, cpu_limit: float = 70.0,
         role: str = "route_server", reference_dut: bool = True) -> list[tuple[str, bool | None, str]]:
    """P2 acceptance gate (spec §14): sessions, convergence, correctness, generator validity, CPU.
    Attribute correctness is a PASS/FAIL check against the reference DUT (the BIRD
    stand-in, transparent by construction: a violation means our bug); against a real
    device it describes the device and is reported as INFO (passed = None)."""
    s, c, g = report["sessions"], report["convergence"], report["generator"]
    out = [
        ("all sessions Established", s["established"] == s["configured"], f'{s["established"]}/{s["configured"]}'),
        ("initial convergence reached at every receiver", c["initial_s"] is not None,
         f'{c["receivers_converged"]}/{c["receivers"]}' + (f', {c["initial_s"]:.1f} s' if c["initial_s"] else "")),
        ("no missing/extra routes", report["correctness"]["missing_routes"] == 0
         and report["correctness"]["extra_routes"] == 0,
         f'missing {report["correctness"]["missing_routes"]}, extra {report["correctness"]["extra_routes"]}'),
        ("no unexpected session resets", s["resets"] == 0, str(s["resets"])),
        ("no attributes added beyond the role",
         (not report["correctness"]["attributes"]["unexpected"]) if reference_dut else None,
         ", ".join(f"{k} on {v:,} routes" for k, v in report["correctness"]["attributes"]["unexpected"].items())
         or "none"),
        (f"route attributes as expected ({EXPECT[role]})",
         (report["correctness"]["attribute_violations"] == 0) if reference_dut else None,
         attribute_detail(report, role)),
        ("generator send lag p99 < 10 ms", g["send_lag_p99_ms"] is None or g["send_lag_p99_ms"] < 10,
         "n/a (max-rate mode)" if g["send_lag_p99_ms"] is None else f'{g["send_lag_p99_ms"]:.2f} ms'),
    ]
    if cpu:
        worst = max(cpu.values())
        out.append((f"node-average CPU <= {cpu_limit:.0f} %", worst <= cpu_limit,
                    ", ".join(f"{k} {v:.0f} %" for k, v in cpu.items())))
    return out
