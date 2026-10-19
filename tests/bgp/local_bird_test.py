#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Local end-to-end test: tgen-agent routers <-> BIRD 2 route server on loopback.

Run as root on a Linux box with BIRD 2 installed (no cluster needed):
  sudo tests/bgp/local_bird_test.py --agent agent/build/tgen-agent --routers 12 --prefixes 2000
Exit code 0 = gate passed.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
from tgen.analyze import analyze, gate  # noqa: E402
from tgen.planner import NodeSpec, Scenario, bird_config, build  # noqa: E402

DUT_IP, DUT_ASN, NET = "10.200.0.1", 64500, "10.200.0"


def sh(cmd: list[str], check=True) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, check=check, capture_output=True, text=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--agent", default="agent/build/tgen-agent")
    ap.add_argument("--routers", type=int, default=12)
    ap.add_argument("--prefixes", type=int, default=2000)
    ap.add_argument("--role", default="route_server", choices=["route_server", "route_reflector", "router"])
    ap.add_argument("--rate", type=float, default=0, help="prefixes/s per router (0 = max)")
    ap.add_argument("--timeout", type=float, default=60, help="convergence timeout, s")
    ap.add_argument("--cores", default="", help="e.g. 0,1")
    args = ap.parse_args()

    work = Path(tempfile.mkdtemp(prefix="tgen-local-"))
    ips = [f"{NET}.{20 + i}" for i in range(args.routers)]
    for ip in [DUT_IP, *ips]:
        sh(["ip", "addr", "replace", f"{ip}/32", "dev", "lo"])

    sc = Scenario(run_id=f"local-{int(time.time())}", dut_ip=DUT_IP, dut_asn=DUT_ASN, role=args.role,
                  routers_total=args.routers, prefixes_per_router=args.prefixes,
                  exclude=["10.100.0.0/16", "10.200.0.0/16"], router_ip_first=ips[0],
                  announce_rate=args.rate, establish_delay_s=3, establish_window_s=8,
                  converge_timeout_s=args.timeout, withdraw_timeout_s=min(30.0, args.timeout))
    cores = [int(c) for c in args.cores.split(",") if c != ""]
    t0 = time.time()
    plans, meta = build(sc, [NodeSpec("LOCAL", 1.0, cores)])
    print(f"plan: {args.routers} routers x {args.prefixes} prefixes = {meta['total_prefixes']} "
          f"({time.time() - t0:.1f} s to generate)")

    conf = work / "bird.conf"
    conf.write_text(bird_config(meta, DUT_IP, DUT_ASN, multihop=True, log_file=str(work / "bird.log")))
    sock = work / "bird.ctl"
    bird = subprocess.Popen(["bird", "-f", "-c", str(conf), "-s", str(sock), "-P", str(work / "bird.pid")],
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    time.sleep(1)
    if bird.poll() is not None:
        print("BIRD failed to start:", bird.stderr.read())
        return 2

    plan_file, result_file = work / "plan.json", work / "result.json"
    plan_file.write_text(json.dumps(plans["LOCAL"]))
    try:
        rc = subprocess.run([args.agent, "--plan", str(plan_file), "--out", str(result_file)],
                            stderr=open(work / "agent.log", "w")).returncode
    finally:
        bird.send_signal(signal.SIGTERM)
        bird.wait(timeout=10)
    if rc != 0:
        print("agent failed, see", work / "agent.log")
        return 2

    report = analyze(meta, [json.loads(result_file.read_text())])
    (work / "report.json").write_text(json.dumps(report, indent=2))
    print(json.dumps({k: report[k] for k in ("sessions", "convergence", "latency_announce_ms",
                                              "latency_withdraw_ms", "withdraw", "dut_output", "correctness")},
                     indent=1, default=str)[:4000])
    ok = True
    for name, passed, detail in gate(report):
        print(f"  {'PASS' if passed else 'FAIL'}  {name}: {detail}")
        ok &= passed
    print("work dir:", work)
    return 0 if ok else 1


if __name__ == "__main__":
    if os.geteuid() != 0:
        sys.exit("run as root (adds loopback addresses, starts BIRD)")
    sys.exit(main())
