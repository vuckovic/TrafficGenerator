#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""End-to-end test of `tgen-agent serve` (docs/07-agent-api.md) on one Linux box.

Six agents (one per simulated node) on 127.0.0.1:7701-7706, BIRD 2 as route
server on 10.100.0.16 (loopback, multihop), router /32s managed by the agents on
`lo`. A minimal controller drives every phase and checks the results. Needs root
and bird. Usage: sudo tests/agent/test_serve.py [--agent agent/build/tgen-agent]
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "tools"))
from tgen.planner import NodeSpec, Scenario, bird_config, build  # noqa: E402

TOKEN = "test-token-0123456789abcdef"
DUT_IP, DUT_ASN = "10.100.0.16", 64500
FAILS: list[str] = []


def check(cond: bool, what: str) -> None:
    print(("  PASS  " if cond else "  FAIL  ") + what, flush=True)
    if not cond:
        FAILS.append(what)


def api(port: int, method: str, path: str, body=None, token: str = TOKEN, timeout: float = 30):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data, method=method,
                                 headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--agent", default=str(REPO / "agent" / "build" / "tgen-agent"))
    ap.add_argument("--routers", type=int, default=12)
    ap.add_argument("--prefixes", type=int, default=500)
    args = ap.parse_args()
    work = Path(tempfile.mkdtemp(prefix="tgen-serve-"))
    (work / "token").write_text(TOKEN + "\n")
    os.chmod(work / "token", 0o600)
    procs = []
    nodes = [f"CMNODE0{i}" for i in range(1, 7)]
    ports = {n: 7700 + i for i, n in enumerate(nodes, 1)}
    try:
        # --- DUT: BIRD route server on loopback --------------------------------
        subprocess.run(["ip", "addr", "replace", f"{DUT_IP}/32", "dev", "lo"], check=True)
        sc = Scenario(run_id="serve-test", dut_ip=DUT_IP, dut_asn=DUT_ASN, routers_total=args.routers,
                      prefixes_per_router=args.prefixes, hold_time=9, seed=7,
                      skip_ips=[f"10.100.0.{11 + i}" for i in range(6)])
        plans, meta = build(sc, [NodeSpec(n, 0.7 if n == nodes[0] else 1.0) for n in nodes])
        (work / "bird.conf").write_text(bird_config(meta, DUT_IP, DUT_ASN, multihop=True,
                                                    log_file=str(work / "bird.log")))
        subprocess.run(["bird", "-c", str(work / "bird.conf"), "-s", str(work / "bird.ctl"),
                        "-P", str(work / "bird.pid")], check=True)

        # --- agents -------------------------------------------------------------
        for n in nodes:
            log = open(work / f"agent-{n}.log", "w")
            procs.append(subprocess.Popen([args.agent, "serve", "--listen", f"127.0.0.1:{ports[n]}",
                                           "--token-file", str(work / "token")], stdout=log, stderr=log))
        time.sleep(0.5)
        print("API basics", flush=True)
        st, body = api(ports[nodes[0]], "GET", "/v1/status")
        check(st == 200 and body.get("state") == "idle", f"status answers, state idle ({st} {body.get('state')})")
        st, _ = api(ports[nodes[0]], "GET", "/v1/status", token="wrong-token-wrong-token")
        check(st == 401, f"wrong token refused ({st})")
        st, body = api(ports[nodes[0]], "PUT", "/v1/run", {"run_id": "x", "dut": {"ip": "bad", "asn": 1}, "routers": []})
        check(st == 400, f"invalid plan refused ({st}: {body.get('error', '')[:60]})")

        # --- load plans -----------------------------------------------------------
        for n in nodes:
            p = dict(plans[n])
            p.pop("timing", None)
            p.update(iface="lo", seed=7, hold_time=9, live_sample_mod=10, controller_timeout_s=5)
            st, body = api(ports[n], "PUT", "/v1/run", p, timeout=120)
            check(st == 200, f"{n}: plan loaded ({st} {body})")
        lo = subprocess.run(["ip", "-4", "-o", "addr", "show", "dev", "lo"], capture_output=True, text=True).stdout
        check(all(r["ip"] + "/32" in lo for r in meta["routers"]), "agents added all router /32 addresses")

        # --- phases on one absolute timeline -------------------------------------
        t0 = int(time.time() * 1000) + 2000
        names = [r["name"] for r in meta["routers"]]
        flap_reset, flap_silent, flap_wd = names[1], names[4], names[7]
        timeline = [
            (0, "establish", {"per_s": 0}),
            (3000, "announce", {"rate": 0}),
            (8000, "steady", {}),
            (10000, "churn", {"rate": 40, "mix": {"announce": 40, "withdraw": 40, "attr": 20}}),
            (13000, "flap", {"kind": "reset", "down_s": 2, "routers": [flap_reset]}),
            (13000, "flap", {"kind": "silent", "down_s": 1, "routers": [flap_silent]}),
            (13000, "flap", {"kind": "withdraw_all", "down_s": 2, "routers": [flap_wd]}),
            (34000, "teardown", {"rate": 0}),
            (38000, "end", {}),
        ]
        for off, typ, params in timeline:
            for n in nodes:
                st, body = api(ports[n], "POST", "/v1/run/phase", {"type": typ, "at_ms": t0 + off, "params": params})
                if st != 202:
                    check(False, f"{n}: phase {typ} accepted ({st} {body})")
        print("phases scheduled; polling stats every second", flush=True)

        tx, rx, seen_est, max_held, live_lat = {}, {}, 0, 0, []
        churn_rx_gen = 0
        while time.time() * 1000 < t0 + 40000:
            time.sleep(1)
            tot_est, held = 0, 0
            for n in nodes:
                st, s = api(ports[n], "GET", "/v1/run/stats")
                if st != 200:
                    continue
                tot_est += s["by_state"].get("Established", 0)
                held += s["totals"]["routes_held"]
                for key, gen, kind, t in s["samples"]["tx"]:
                    tx[(key, gen, kind if kind != 3 else 1)] = t
                for key, gen, kind, t in s["samples"]["rx"]:
                    churn_rx_gen += 1 if gen else 0
                    k = (key, gen, kind)
                    if k in tx:
                        live_lat.append((t - tx[k]) / 1e6)
            seen_est = max(seen_est, tot_est)
            max_held = max(max_held, held)
        expected_total = sum(r["expected_rx"] for pl in plans.values() for r in pl["routers"])

        print("live view", flush=True)
        check(seen_est == args.routers, f"all {args.routers} sessions Established at some point ({seen_est})")
        check(max_held >= expected_total, f"initial load complete: {max_held:,} of {expected_total:,} routes held")
        check(len(live_lat) > 20, f"live latency samples joined: {len(live_lat)} "
              f"(p50 {sorted(live_lat)[len(live_lat) // 2] if live_lat else 0:.1f} ms)")
        check(churn_rx_gen > 0, f"churn announcements with generation tags received live ({churn_rx_gen})")

        # --- results ----------------------------------------------------------------
        results = []
        for n in nodes:
            st, body = api(ports[n], "GET", "/v1/run/result", timeout=60)
            check(st == 200, f"{n}: result available ({st})")
            results.append(body)
        rs = {r["name"]: r for res in results for r in res.get("routers", [])}
        print("results", flush=True)
        check(all(r["t_converged"] for r in rs.values()), "every router converged in the initial load")
        churn = {k: sum(r["churn"][k] for r in rs.values()) for k in ("announced", "withdrawn", "attribute_changes")}
        check(all(v > 0 for v in churn.values()), f"churn operations of every kind: {churn}")
        ctx = {(k, g): t for r in rs.values() for k, g, kind, t in r["churn"]["tx"] if kind in (1, 3)}
        joined = [(t - ctx[(k, g)]) / 1e6 for r in rs.values() for k, g, t in r["churn"]["rx"] if (k, g) in ctx]
        check(len(joined) > 50, f"churn latency joined on (prefix, generation): n={len(joined)}, "
              f"p50 {sorted(joined)[len(joined) // 2] if joined else 0:.1f} ms")
        check(rs[flap_reset]["planned_resets"] >= 1 and rs[flap_reset]["sessions_established"] >= 2,
              f"flap reset: {flap_reset} went down (planned) and came back")
        check(rs[flap_silent]["planned_resets"] >= 1 and [6, 0] != rs[flap_silent]["notifications_rx"][:1],
              f"flap silent: {flap_silent} dropped by the DUT's hold timer "
              f"(notifications received {rs[flap_silent]['notifications_rx']})")
        check(rs[flap_wd]["prefixes_withdrawn"] >= args.prefixes, f"flap withdraw_all: {flap_wd} withdrew everything "
              f"({rs[flap_wd]['prefixes_withdrawn']})")
        check(sum(r["resets"] for r in rs.values()) == 0,
              f"no unexpected resets (planned: {sum(r['planned_resets'] for r in rs.values())})")
        check(all(r["state"] == "Stopped" for r in rs.values()), "all routers stopped after teardown/end")
        check(all(r["routes_held"] == 0 for r in rs.values()), "teardown: no routes held at the end")

        for n in nodes:
            api(ports[n], "DELETE", "/v1/run")
        lo = subprocess.run(["ip", "-4", "-o", "addr", "show", "dev", "lo"], capture_output=True, text=True).stdout
        check(not any(r["ip"] + "/32" in lo for r in meta["routers"]), "DELETE removed the router addresses")

        # --- watchdog: a running run without controller contact stops itself -------
        print("watchdog", flush=True)
        n = nodes[1]
        p = dict(plans[n])
        p.pop("timing", None)
        p.update(iface="lo", controller_timeout_s=3)
        api(ports[n], "PUT", "/v1/run", p)
        api(ports[n], "POST", "/v1/run/phase", {"type": "establish", "at_ms": int(time.time() * 1000), "params": {}})
        time.sleep(5)  # no requests: the agent must stop the run by itself
        log = (work / f"agent-{n}.log").read_text()
        check("watchdog: no contact with the controller" in log, "watchdog stopped the run after 3 s without contact")
        st, s = api(ports[n], "GET", "/v1/run/stats")
        check(s.get("state") == "finished", f"run state after the watchdog: {s.get('state')}")
        api(ports[n], "DELETE", "/v1/run")
    finally:
        for p in procs:
            p.terminate()
        subprocess.run(["pkill", "-f", f"bird -c {work}"], capture_output=True)
        print(f"work dir: {work}")
    print(("FAILED: " + "; ".join(FAILS)) if FAILS else "ALL PASS")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
