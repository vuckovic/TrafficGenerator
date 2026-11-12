# SPDX-License-Identifier: MIT
"""Report figures F1-F12 (spec §12) as compact data series: figures.json in the run folder.

Built from the agents' results (per-router times and the sampled prefixes, 1 in sample_mod),
the live samples the controller stored each second (live.jsonl), the DUT polls
(dut-sessions.jsonl), and the run's phases and events. The web page draws them.
Every series is cut to at most a few hundred points so a report stays small.
Times are seconds from the start of the run (establish), except F2 and F3.
"""
from __future__ import annotations

import bisect
import math
import re

MAXPTS = 300


def _r(v, d=3):
    return None if v is None else round(v, d)


def _thin(rows: list, n: int = MAXPTS) -> list:
    if len(rows) <= n:
        return rows
    step = len(rows) / n
    out = [rows[int(i * step)] for i in range(n)]
    if out[-1] is not rows[-1]:
        out.append(rows[-1])
    return out


def _pctl(sorted_v: list[float], q: float):
    if not sorted_v:
        return None
    return sorted_v[min(len(sorted_v) - 1, int(q * (len(sorted_v) - 1) + 0.5))]


def build(meta: dict, results: list[dict], ticks: list[dict], dut_polls: list[dict],
          phases: list[dict], events: list[dict]) -> dict:
    t = meta["timing"]
    t0 = t["establish_at"]                       # ms
    ann = t["announce_at"]
    wd = t.get("withdraw_at")
    end = t.get("end_at") or (ticks[-1]["t_ms"] if ticks else ann)
    routers = [r | {"node": res["node"]} for res in results for r in res["routers"]]
    rel = lambda ms_: _r((ms_ - t0) / 1000, 3)   # noqa: E731
    ns2s = lambda ns, base=t0: (ns / 1e6 - base) / 1000  # noqa: E731
    out: dict = {"version": 1, "t0_ms": t0, "announce_s": rel(ann), "withdraw_s": rel(wd) if wd else None,
                 "end_s": rel(end), "routers": len(routers)}

    # ---------------- F1 session establishment
    est = sorted(ns2s(r["t_established"]) for r in routers if r["t_established"])
    tail = [[_r(est[-1] + max(0.5, 0.2 * est[-1]), 4), len(est)]] if est else []   # room after the last one
    out["f1"] = {"curve": _thin([[0, 0]] + [[_r(x, 4), i + 1] for i, x in enumerate(est)] + tail),
                 "configured": len(routers),
                 "setup_ms": sorted([[r["name"], _r((r["t_established"] - r["t_connect_start"]) / 1e6, 2)]
                                     for r in routers if r["t_established"] and r["t_connect_start"]],
                                    key=lambda x: -x[1])[:60]}

    # ---------------- F2 initial convergence: share of expected routes held vs time since the announce
    wd_ns = (wd or 10**18) * 1_000_000
    per: dict[str, list[float]] = {}
    for r in routers:
        ts = sorted((x[1] - ann * 1_000_000) / 1e9 for x in r["samples"]["rx"] if x[1] < wd_ns)
        per[r["name"]] = ts
    expect_share = {r["name"]: (min(r["nlri_rx"], r["expected_rx"]) / r["expected_rx"] if r["expected_rx"] else 1.0)
                    for r in routers}
    finals = [x for v in per.values() for x in v[-1:]]
    span = max(finals) if finals else 1.0
    span = max(span * 1.08, 0.05)
    grid = [span * i / 200 for i in range(201)]

    def share(name: str, x: float) -> float:
        v = per[name]
        return 100.0 * expect_share[name] * (bisect.bisect_right(v, x) / len(v)) if v else 0.0

    names = [r["name"] for r in routers if r["expected_rx"]]
    slowest = max(names, key=lambda n: (per[n][-1] if per[n] else math.inf, -len(per[n])), default=None)
    out["f2"] = {"t": [_r(x, 4) for x in grid],
                 "all": [_r(sum(share(n, x) for n in names) / max(1, len(names)), 2) for x in grid],
                 "slowest": [_r(share(slowest, x), 2) for x in grid] if slowest else [],
                 "slowest_name": slowest,
                 "slowest_s": _r(per[slowest][-1], 3) if slowest and per[slowest] else None,
                 "initial_s": _r(max((r["t_converged"] / 1e6 - ann) / 1000 for r in routers if r["t_converged"]), 3)
                 if routers and all(r["t_converged"] for r in routers if r["expected_rx"]) else None}

    # ---------------- F3 latency CDF (ms), announce and withdraw
    def lat(tx_key: str, rx_key: str) -> list[float]:
        tx = {k: ts for r in routers for k, ts in r["samples"][tx_key]}
        return sorted((ts - tx[k]) / 1e6 for r in routers for k, ts in r["samples"][rx_key] if k in tx)

    def cdf(v: list[float]) -> dict:
        if not v:
            return {"pts": [], "p50": None, "p99": None, "n": 0}
        qs = [i / 200 for i in range(1, 201)]     # from 0.5 %: the fastest few would stretch the log axis
        return {"pts": [[_r(max(_pctl(v, q), 0.01), 3), _r(q, 4)] for q in qs], "p50": _r(_pctl(v, 0.5), 3),
                "p99": _r(_pctl(v, 0.99), 3), "n": len(v)}
    out["f3"] = {"announce": cdf(lat("tx", "rx")), "withdraw": cdf(lat("wd_tx", "wd_rx"))}

    # ---------------- F4, F6, F12 from the live samples (one per second)
    tk = [x for x in ticks if x.get("type", "tick") == "tick"]
    f4, f6, f12_rate = [], [], []
    fan = (sum(r["expected_rx"] for r in routers) / max(1, sum(r["prefixes_configured"] for r in routers))) if routers else 1
    cum_sent = cum_rx = 0.0
    prev = None
    node_names = sorted({n["node"] for x in tk for n in (x.get("nodes") or [])})
    cpu = {n: [] for n in node_names}
    lag = []
    for x in tk:
        ts = rel(x["t_ms"])
        rt = x.get("rates") or {}
        la = x.get("latency_ms") or {}
        f4.append([ts, la.get("p50"), la.get("p99"), rt.get("sent_s"), rt.get("rx_s")])
        dt = (x["t_ms"] - prev) / 1000 if prev else 0
        prev = x["t_ms"]
        cum_sent += (rt.get("sent_s") or 0) * dt * fan
        cum_rx += (rt.get("rx_s") or 0) * dt
        held = x.get("routes_held")
        f6.append([ts, _r(max(0.0, cum_sent - cum_rx), 0), held])
        nodes = {n["node"]: n for n in (x.get("nodes") or [])}
        for n in node_names:
            cpu[n].append(nodes.get(n, {}).get("cpu_pct"))
        lag.append(max([n.get("send_lag_p99_ms") or 0 for n in nodes.values()] or [0]))
        f12_rate.append(rt.get("sent_s"))
    idx = list(range(len(tk)))
    keep = set(_thin(idx))
    pick = lambda arr: [a for i, a in enumerate(arr) if i in keep]  # noqa: E731
    out["f4"] = {"rows": pick(f4), "cols": ["t", "p50", "p99", "offered", "delivered"]}
    out["f6"] = {"rows": pick(f6), "cols": ["t", "backlog", "held"], "fanout": _r(fan, 3)}
    out["f12"] = {"t": pick([r[0] for r in f4]), "nodes": {n: pick(v) for n, v in cpu.items()},
                  "send_lag_p99_ms": pick(lag), "offered": pick(f12_rate),
                  "node_cpu_p95": None}

    # ---------------- F5 throughput knee inside the run: delivered vs offered, binned by offered rate
    pts = [(r[3] * fan, r[4], r[2]) for r in f4 if r[3] and r[3] > 0 and r[4] is not None]
    bins: dict[int, list] = {}
    for off, dlv, p99 in pts:
        bins.setdefault(int(round(math.log10(off) * 8)), []).append((off, dlv, p99))
    f5 = []
    for _, v in sorted(bins.items()):
        if len(v) < 2:
            continue
        o = sorted(a for a, _, _ in v)[len(v) // 2]
        d = sorted(b for _, b, _ in v)[len(v) // 2]
        p = [c for _, _, c in v if c is not None]
        f5.append([_r(o, 1), _r(d, 1), _r(max(p), 1) if p else None, len(v)])
    out["f5"] = {"pts": f5, "stepped": len(f5) >= 3}

    # ---------------- F7 per-router heatmap: % of expected routes held per router and time column
    cols = 48
    hspan = max(span, 0.05)
    col_t = [hspan * (i + 1) / cols for i in range(cols)]
    rows = []
    for r in sorted(routers, key=lambda r: r["name"]):
        if not r["expected_rx"]:
            continue
        downs = [(e[0] - ann * 1_000_000) / 1e9 for e in r.get("session_events", []) if e[1] == "down"]
        rows.append([r["name"], [_r(share(r["name"], x), 1) for x in col_t],
                     [_r(d, 3) for d in downs if 0 <= d <= hspan]])
    out["f7"] = {"cols_s": [_r(x, 4) for x in col_t], "rows": rows[:180]}

    # ---------------- F8 correctness per receiver
    out["f8"] = {"rows": [[r["name"], r["expected_rx"], r["routes_peak"], max(0, r["expected_rx"] - r["routes_peak"]),
                           max(0, r["routes_peak"] - r["expected_rx"]), r["checks"].get("looped_rx", 0)]
                          for r in sorted(routers, key=lambda r: r["name"])],
                 "cols": ["router", "expected", "received", "missing", "extra", "looped"]}

    # ---------------- F9 event timeline
    sess = []
    for r in routers:
        for e in r.get("session_events", []):
            sess.append([_r(ns2s(e[0]), 3), r["name"], e[1], e[2] if len(e) > 2 else ""])
    sess.sort()
    dut_track = []
    last = None
    for p in dut_polls:
        if p.get("established") is None:
            continue
        if p["established"] != last:
            dut_track.append([_r(p["t"] - t0 / 1000, 2), p["established"]])
            last = p["established"]
    out["f9"] = {"phases": [[p["type"], rel(p["at_ms"]), rel(p["end_ms"]) if p.get("end_ms") else None, (p.get("params") or {})]
                            for p in phases],
                 "sessions": sess[:600],
                 "dut_sessions": dut_track[:300],
                 "events": [[rel(e["t_ms"]), e["kind"], e["severity"], e["text"][:200]] for e in events][:400],
                 "dut_log": []}     # filled from dut-events.json by the caller (P5)

    # ---------------- F10 DUT resources (from the DUT polls, when the device was configured by us)
    f10 = []
    for p in dut_polls:
        c = p.get("cpu_load")
        m = re.search(r"(\d+(?:\.\d+)?)", str(c)) if c is not None else None
        f10.append([_r(p["t"] - t0 / 1000, 2), p.get("free_pct"), float(m.group(1)) if m else None, p.get("established")])
    out["f10"] = {"rows": _thin(f10), "cols": ["t", "free_mem_pct", "cpu_pct", "established"]}
    out["f11"] = None    # data-plane convergence: P6
    return out
