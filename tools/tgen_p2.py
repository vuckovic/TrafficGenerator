#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""P2 cluster benchmark: emulated BGP routers on the cluster against a DUT.

Run on CMNODE01 as your normal user (passwordless sudo on all nodes, as set up
by deploy/controller-bootstrap.sh):

  tools/tgen_p2.py                         # 60 routers x 10 000 prefixes vs BIRD on CMNODE06
  tools/tgen_p2.py --routers 12 --prefixes 1000 --converge-timeout 60   # quick check
  tools/tgen_p2.py --role route_reflector

What it does: builds tgen-agent, plans the routers (CMNODE01 at 0.7 weight),
starts BIRD 2 as route server on the DUT node, adds the router addresses,
starts all agents on one timeline (CMNODE01 inside its cgroup limits), samples
CPU on every node, collects and analyses the results, prints the P2 gate and
cleans up (addresses, BIRD). Results: ~/tgen-test-results/p2-<timestamp>/
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "tools"))
from tgen.analyze import analyze, gate  # noqa: E402
from tgen.dut_mikrotik import DEFAULT_MAX_ROUTES, DutError, MikroTik, render_rsc  # noqa: E402
from tgen.planner import NodeSpec, Scenario, bird_config, build, set_timeline  # noqa: E402

SSH_OPTS = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=5", "-o", "StrictHostKeyChecking=accept-new"]
REMOTE_DIR = "tgen/run"            # under the SSH user's home on every node
CMNODE01_LIMITS = ["-p", "AllowedCPUs=1-3", "-p", "CPUQuota=280%", "-p", "MemoryMax=11200M",
                   "-p", "MemoryHigh=10500M", "-p", "CPUWeight=100"]


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def load_env(path: Path) -> dict:
    script = f"""source {shlex.quote(str(path))}
for v in NODE_COUNT MGMT_PREFIX MGMT_HOST_BASE MGMT_IFACE TRAFFIC_PREFIX TRAFFIC_HOST_BASE SSH_USER; do
  printf '%s=%s\\n' "$v" "${{!v}}"; done
printf 'NODE_HOSTNAMES=%s\\n' "${{NODE_HOSTNAMES[*]}}"
printf 'NODE_RAM_GB=%s\\n' "${{NODE_RAM_GB[*]}}"
"""
    out = subprocess.run(["bash", "-c", script], check=True, capture_output=True, text=True).stdout
    env = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
    env["NODE_HOSTNAMES"] = env["NODE_HOSTNAMES"].split()
    return env


class Cluster:
    def __init__(self, env: dict):
        self.env = env
        self.user = env.get("SSH_USER") or os.environ.get("USER", "pi")
        names = env["NODE_HOSTNAMES"]
        base, mbase, tbase = env["MGMT_PREFIX"], int(env["MGMT_HOST_BASE"]), int(env["TRAFFIC_HOST_BASE"])
        self.mgmt = {n: f"{base}.{mbase + i + 1}" for i, n in enumerate(names)}
        self.traffic = {n: f'{env["TRAFFIC_PREFIX"]}.{tbase + i + 1}' for i, n in enumerate(names)}
        self.iface = env.get("MGMT_IFACE", "eth0")

    def ssh(self, node: str, cmd: str, check: bool = True, timeout: float | None = 120) -> subprocess.CompletedProcess:
        r = subprocess.run(["ssh", *SSH_OPTS, f"{self.user}@{self.mgmt[node]}", cmd],
                           capture_output=True, text=True, timeout=timeout)
        if check and r.returncode != 0:
            raise RuntimeError(f"{node}: `{cmd}` failed ({r.returncode}): {r.stderr.strip()[-400:]}")
        return r

    def put(self, node: str, local: Path, remote: str) -> None:
        subprocess.run(["scp", "-q", *SSH_OPTS, str(local), f"{self.user}@{self.mgmt[node]}:{remote}"], check=True)

    def get(self, node: str, remote: str, local: Path) -> bool:
        return subprocess.run(["scp", "-q", *SSH_OPTS, f"{self.user}@{self.mgmt[node]}:{remote}", str(local)],
                              capture_output=True).returncode == 0


def parallel(fn, items):
    with cf.ThreadPoolExecutor(max_workers=8) as ex:
        return list(ex.map(fn, items))


def build_agent() -> Path:
    if not Path("/usr/include/asio.hpp").exists():
        sys.exit("libasio-dev missing: sudo apt-get install -y libasio-dev  (or re-run the installer)")
    bdir = REPO / "agent" / "build"
    subprocess.run(["cmake", "-S", str(REPO / "agent"), "-B", str(bdir), "-G", "Ninja",
                    "-DCMAKE_BUILD_TYPE=Release"], check=True, capture_output=True)
    subprocess.run(["cmake", "--build", str(bdir)], check=True)
    tests = bdir / "tgen-tests"
    if tests.exists():
        r = subprocess.run([str(tests), "--gtest_brief=1"], capture_output=True, text=True)
        log("unit tests: " + (r.stdout.strip().splitlines()[-1] if r.stdout else "?"))
        if r.returncode != 0:
            sys.exit("unit tests failed:\n" + r.stdout[-2000:])
    return bdir / "tgen-agent"


def cpu_summary(mpstat_text: str, t_from: float, t_to: float) -> dict:
    """Node-average busy % from lines "<epoch> <idle%>" (see the /proc/stat sampler
    in main), restricted to the run window."""
    vals = []
    for line in mpstat_text.splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        try:
            ts, idle = float(parts[0]), float(parts[-1])
        except ValueError:
            continue
        if t_from <= ts <= t_to:
            vals.append(100.0 - idle)
    if not vals:
        return {"avg": None, "p95": None, "max": None, "n": 0}
    s = sorted(vals)
    return {"avg": sum(vals) / len(vals), "p95": s[int(0.95 * (len(s) - 1))], "max": s[-1], "n": len(vals)}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=str(REPO / "deploy" / "cluster.env"))
    ap.add_argument("--dut", default="bird", choices=["bird", "mikrotik"],
                    help="bird = BIRD 2 stand-in on --dut-node; mikrotik = external RouterOS 7 device at --dut-ip")
    ap.add_argument("--dut-node", default="CMNODE06", help="node running BIRD as stand-in DUT (--dut bird)")
    ap.add_argument("--dut-ip", default="10.100.0.111", help="DUT address (--dut mikrotik)")
    ap.add_argument("--dut-user", default="admin", help="DUT SSH user; password from TGEN_DUT_PASSWORD or prompt")
    ap.add_argument("--dut-asn", type=int, default=64500)
    ap.add_argument("--dut-max-routes", type=int, default=DEFAULT_MAX_ROUTES,
                    help="refuse larger runs against small hardware DUTs (routes the DUT must hold)")
    ap.add_argument("--keep-dut-config", action="store_true", help="leave the BGP neighbours on the DUT after the run")
    ap.add_argument("--nodes", default="", help="router nodes, comma list (default: all except the DUT node)")
    ap.add_argument("--routers", type=int, default=60)
    ap.add_argument("--prefixes", type=int, default=10_000)
    ap.add_argument("--role", default="route_server", choices=["route_server", "route_reflector", "router"])
    ap.add_argument("--rate", type=float, default=0, help="prefixes/s per router (0 = max)")
    ap.add_argument("--per-update", type=int, default=0)
    ap.add_argument("--converge-timeout", type=float, default=300)
    ap.add_argument("--withdraw-timeout", type=float, default=120)
    ap.add_argument("--seed", type=int, default=int(time.time()) % 100000)
    ap.add_argument("--no-build", action="store_true")
    ap.add_argument("--bird-multihop", action="store_true", help=argparse.SUPPRESS)  # loopback simulation only
    args = ap.parse_args()

    env = load_env(Path(args.config))
    cl = Cluster(env)
    names = env["NODE_HOSTNAMES"]
    external = args.dut == "mikrotik"
    if not external and args.dut_node not in names:
        sys.exit(f"unknown DUT node {args.dut_node}")
    # with an external DUT every node can host routers; with BIRD the DUT node is reserved
    router_nodes = [n for n in (args.nodes.split(",") if args.nodes else names)
                    if n and (external or n != args.dut_node)]
    controller = names[0]
    nodes = [NodeSpec(n, 0.7 if n == controller else 1.0, [1, 2, 3]) for n in router_nodes]
    dut_ip = args.dut_ip if external else cl.traffic[args.dut_node]
    dut_label = f"MikroTik {args.dut_ip}" if external else f"BIRD on {args.dut_node}"
    dut_routes = args.routers * args.prefixes
    if external and dut_routes > args.dut_max_routes:
        sys.exit(f"{args.routers} x {args.prefixes} = {dut_routes} routes is above --dut-max-routes "
                 f"{args.dut_max_routes} for this DUT (a CRS106 has 128 MB RAM). Reduce --routers/--prefixes, "
                 "or raise --dut-max-routes deliberately.")
    mt = None
    if external:
        mt = MikroTik(args.dut_ip, args.dut_user)
        mt.ask_password_if_needed()
        try:
            res = mt.resources()
        except DutError as e:
            sys.exit(f"cannot reach the DUT: {e}")
        log(f"DUT: {res['board']} RouterOS {res['version']}, CPU {res['cpu']} load {res['cpu_load']}, "
            f"memory free {res['free_memory']} of {res['total_memory']}")
        if int(res["version"].split(".")[0]) < 7:
            sys.exit("RouterOS 7 required on the DUT")

    out = Path.home() / "tgen-test-results" / time.strftime("p2-%Y%m%d-%H%M%S")
    out.mkdir(parents=True)
    log(f"results -> {out}")

    agent = Path(REPO / "agent" / "build" / "tgen-agent") if args.no_build else build_agent()
    sc = Scenario(run_id=out.name, dut_ip=dut_ip, dut_asn=args.dut_asn, role=args.role,
                  skip_ips=list(cl.traffic.values()),
                  routers_total=args.routers, prefixes_per_router=args.prefixes, seed=args.seed,
                  announce_rate=args.rate, per_update=args.per_update,
                  establish_delay_s=15, establish_window_s=20,
                  converge_timeout_s=args.converge_timeout, withdraw_timeout_s=args.withdraw_timeout)
    t0 = time.time()
    plans, meta = build(sc, nodes)
    log(f"plan: {args.routers} routers x {args.prefixes} prefixes = {meta['total_prefixes']} on "
        + ", ".join(f"{k} {v}" for k, v in meta["router_counts"].items()) + f" ({time.time() - t0:.0f} s)")

    added: dict[str, list[str]] = {}
    bird_started = False
    dut_configured = False
    results: list[dict] = []
    cpu: dict = {}
    try:
        if external:
            # --- external DUT: push the BGP neighbours of this run --------------------
            rsc = render_rsc(meta, dut_ip, args.dut_asn, args.role)
            (out / "dut-config.rsc").write_text(rsc)
            n = mt.push(rsc, expected=len(meta["routers"]))
            dut_configured = True
            log(f"DUT configured: {n} BGP connections (comment \"tgen\"), role {args.role}, AS {args.dut_asn}")
        else:
            # --- DUT stand-in: BIRD 2 on the DUT node -----------------------------
            log(f"starting BIRD 2 route server on {args.dut_node} ({dut_ip}, AS {args.dut_asn})")
            cl.ssh(args.dut_node, f"mkdir -p {REMOTE_DIR}")
            cl.ssh(args.dut_node, "command -v bird >/dev/null || sudo apt-get install -y -qq bird2 >/dev/null; "
                                  "sudo systemctl disable --now bird >/dev/null 2>&1; true", timeout=600)
            conf = out / "bird.conf"
            conf.write_text(bird_config(meta, dut_ip, args.dut_asn, multihop=args.bird_multihop,
                                        log_file="/var/log/tgen-bird.log"))
            cl.put(args.dut_node, conf, f"{REMOTE_DIR}/bird.conf")
            cl.ssh(args.dut_node, f"sudo pkill -x bird; sleep 0.5; sudo truncate -s0 /var/log/tgen-bird.log 2>/dev/null; "
                                  f"sudo bird -c {REMOTE_DIR}/bird.conf -s /run/tgen-bird.ctl -P /run/tgen-bird.pid")
            bird_started = True

        # --- agents, router addresses -----------------------------------------
        def prep(n: str) -> None:
            cl.ssh(n, f"mkdir -p {REMOTE_DIR} && rm -f {REMOTE_DIR}/result.json {REMOTE_DIR}/agent.log")
            cl.put(n, agent, f"{REMOTE_DIR}/tgen-agent")
            ips = [r["ip"] for r in plans[n]["routers"]]
            cmds = "; ".join(f"sudo ip addr replace {ip}/32 dev {cl.iface}" for ip in ips)
            cl.ssh(n, cmds)
            added[n] = ips
        parallel(prep, router_nodes)
        log("agents deployed, router addresses configured")

        # --- timeline fixed now, plans uploaded last ---------------------------
        sc.establish_delay_s = 15
        set_timeline(plans, meta, sc)
        for n in router_nodes:
            (out / f"plan-{n}.json").write_text(json.dumps(plans[n]))
        parallel(lambda n: cl.put(n, out / f"plan-{n}.json", f"{REMOTE_DIR}/plan.json"), router_nodes)
        tm = meta["timing"]
        run_s = (tm["end_at"] / 1000) - time.time()
        log(f"start in {(tm['establish_at'] / 1000 - time.time()):.0f} s; run ends in {run_s:.0f} s "
            f"(convergence timeout {args.converge_timeout:.0f} s, withdraw {args.withdraw_timeout:.0f} s)")

        # --- CPU sampling on all nodes (incl. DUT) -----------------------------
        secs = int(run_s) + 10
        # tiny /proc/stat sampler: one "<epoch> <idle%>" line per second, flushed
        sampler = ("import time,sys\n"
                   "def rd():\n"
                   " v=[int(x) for x in open('/proc/stat').readline().split()[1:]]\n"
                   " return sum(v), v[3]+v[4]\n"
                   f"end=time.time()+{secs}\n"
                   "t0,i0=rd()\n"
                   "while time.time()<end:\n"
                   " time.sleep(1); t1,i1=rd()\n"
                   " print(int(time.time()), round(100.0*(i1-i0)/max(1,t1-t0),1), flush=True); t0,i0=t1,i1\n")
        for n in sorted(set([*router_nodes] + ([] if external else [args.dut_node]))):
            cl.ssh(n, f"nohup python3 -c {shlex.quote(sampler)} > {REMOTE_DIR}/cpu.txt 2>/dev/null < /dev/null &")

        # --- run ----------------------------------------------------------------
        limits_ok = cl.ssh(controller, f"sudo systemd-run --quiet --scope {' '.join(CMNODE01_LIMITS)} true",
                           check=False).returncode == 0 if controller in router_nodes else True
        if not limits_ok:
            log(f"WARNING {controller}: cgroup limits not accepted by systemd; falling back to taskset -c 1-3 "
                "(no CPU quota / memory cap)")
        procs = {}
        for n in router_nodes:
            run = f"{REMOTE_DIR}/tgen-agent --plan {REMOTE_DIR}/plan.json --out {REMOTE_DIR}/result.json"
            if n == controller and limits_ok:  # spec §10.2: generator limited to 70 % CPU / 11.2 GB
                run = (f"sudo systemd-run --quiet --scope {' '.join(CMNODE01_LIMITS)} "
                       f"sudo -u {cl.user} {run}")
            elif n == controller:
                run = f"taskset -c 1-3 {run}"
            procs[n] = subprocess.Popen(["ssh", *SSH_OPTS, f"{cl.user}@{cl.mgmt[n]}",
                                         f"cd ~ && {run} 2> {REMOTE_DIR}/agent.log"])
        mid = tm["announce_at"] / 1000 + 5
        while time.time() < tm["end_at"] / 1000 + 20 and any(p.poll() is None for p in procs.values()):
            if mid and time.time() > mid:
                if external:
                    try:
                        sess = mt.sessions()
                        (out / "dut-sessions-mid.txt").write_text(sess)
                        res = mt.resources()
                        (out / "dut-resources-mid.json").write_text(json.dumps(res))
                        log(f"DUT mid-run: {sess.count('established')} sessions established, "
                            f"CPU load {res['cpu_load']}, free memory {res['free_memory']}")
                    except DutError as e:
                        log(f"DUT mid-run query failed: {e}")
                else:
                    r = cl.ssh(args.dut_node, "sudo birdc -s /run/tgen-bird.ctl show protocols | grep -c Established; "
                                              "sudo birdc -s /run/tgen-bird.ctl show memory | tail -1", check=False)
                    (out / "bird-mid.txt").write_text(r.stdout)
                    log("BIRD mid-run: " + " | ".join(r.stdout.split("\n")[:2]).strip() + " sessions Established")
                mid = 0
            time.sleep(2)
        for n, p in procs.items():
            if p.poll() is None:
                p.terminate()
            if p.wait() != 0:
                log(f"WARNING {n}: agent exit code {p.returncode}")

        # --- collect ---------------------------------------------------------
        for n in router_nodes:
            if cl.get(n, f"{REMOTE_DIR}/result.json", out / f"result-{n}.json"):
                results.append(json.loads((out / f"result-{n}.json").read_text()))
            else:
                log(f"WARNING {n}: no result")
            cl.get(n, f"{REMOTE_DIR}/agent.log", out / f"agent-{n}.log")
        for n in sorted(set([*router_nodes] + ([] if external else [args.dut_node]))):
            if cl.get(n, f"{REMOTE_DIR}/cpu.txt", out / f"cpu-{n}.txt"):
                cpu[n] = cpu_summary((out / f"cpu-{n}.txt").read_text(), tm["establish_at"] / 1000,
                                     tm["end_at"] / 1000)
        if external:
            try:
                (out / "dut-bgp.log").write_text(mt.bgp_log())
                (out / "dut-resources-end.json").write_text(json.dumps(mt.resources()))
            except DutError as e:
                log(f"could not fetch DUT log: {e}")
        else:
            r = cl.ssh(args.dut_node, "sudo cat /var/log/tgen-bird.log", check=False)
            (out / "bird.log").write_text(r.stdout)
    finally:
        log("cleanup: router addresses" + (", BIRD" if bird_started else "")
            + (", DUT BGP neighbours" if dut_configured and not args.keep_dut_config else ""))
        if dut_configured and not args.keep_dut_config:
            try:
                mt.remove()
            except DutError as e:
                log(f"WARNING: could not remove the tgen neighbours from the DUT: {e}")
        for n, ips in added.items():
            cl.ssh(n, "; ".join(f"sudo ip addr del {ip}/32 dev {cl.iface} 2>/dev/null" for ip in ips) + "; true",
                   check=False)
        if bird_started:
            cl.ssh(args.dut_node, "sudo pkill -x bird; true", check=False)

    (out / "meta.json").write_text(json.dumps(meta))
    report = analyze(meta, results)
    report["cpu_node_avg"] = cpu
    (out / "report.json").write_text(json.dumps(report, indent=2))

    gen_cpu = {n: v["p95"] for n, v in cpu.items() if n in router_nodes and v["p95"] is not None}
    c, la, lw = report["convergence"], report["latency_announce_ms"], report["latency_withdraw_ms"]
    lines = [
        f"run {out.name}: {args.routers} routers x {args.prefixes} prefixes, role {args.role}, DUT {dut_label}",
        f"sessions      : {report['sessions']['established']}/{report['sessions']['configured']} Established, "
        f"setup p90 {report['sessions']['setup_ms']['p90'] or 0:.0f} ms, resets {report['sessions']['resets']}",
        f"convergence   : initial {c['initial_s']:.1f} s" if c["initial_s"] else
        f"convergence   : NOT reached ({c['receivers_converged']}/{c['receivers']})",
        f"latency ann.  : p50 {la['p50'] or 0:.0f} ms, p99 {la['p99'] or 0:.0f} ms (n={la['n']})",
        f"latency wd.   : p50 {lw['p50'] or 0:.0f} ms, p99 {lw['p99'] or 0:.0f} ms (n={lw['n']})",
        f"withdraw conv.: {report['withdraw']['convergence_s']:.1f} s" if report["withdraw"] and
        report["withdraw"]["convergence_s"] else "withdraw conv.: n/a",
        f"DUT output    : {(report['dut_output'] or {}).get('prefixes_per_s') or 0:,.0f} prefixes/s",
        "CPU node-avg  : " + ", ".join(f"{n} avg {v['avg']:.0f} % p95 {v['p95']:.0f} %"
                                        for n, v in cpu.items() if v["avg"] is not None),
        "",
        "P2 gate:",
    ]
    ok = True
    for name, passed, detail in gate(report, gen_cpu):
        lines.append(f"  {'PASS' if passed else 'FAIL'}  {name}: {detail}")
        ok &= passed
    text = "\n".join(lines)
    (out / "summary.txt").write_text(text + "\n")
    print("\n" + text + f"\n\nfiles: {out}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
