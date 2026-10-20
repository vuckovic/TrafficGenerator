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
from tgen.analyze import analyze, attribute_detail, gate  # noqa: E402
from tgen.dut import ADAPTERS, DutError, make as make_dut  # noqa: E402
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
        try:
            r = subprocess.run(["ssh", "-n", *SSH_OPTS, f"{self.user}@{self.mgmt[node]}", cmd],
                               capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired:
            r = subprocess.CompletedProcess([], 124, "", f"timed out after {timeout} s")
        if check and r.returncode != 0:
            raise RuntimeError(f"{node}: `{cmd}` failed ({r.returncode}): {r.stderr.strip()[-400:]}")
        return r

    def put(self, node: str, local: Path, remote: str) -> None:
        subprocess.run(["scp", "-q", *SSH_OPTS, str(local), f"{self.user}@{self.mgmt[node]}:{remote}"], check=True,
                       timeout=300)

    def get(self, node: str, remote: str, local: Path) -> bool:
        try:
            return subprocess.run(["scp", "-q", *SSH_OPTS, f"{self.user}@{self.mgmt[node]}:{remote}", str(local)],
                                  capture_output=True, timeout=120).returncode == 0
        except subprocess.TimeoutExpired:
            return False


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


# default DUT role per platform: a device that cannot act as a transparent route
# server is tested in the role it really has (MikroTik RouterOS 7 adds its own AS)
DEFAULT_ROLE = {"mikrotik": "router"}


def dut_view(jsonl: Path) -> dict | None:
    """Summary of the DUT's own session list, polled during the run (MikroTik format)."""
    from tgen.dut.mikrotik import parse_sessions
    if not jsonl.exists():
        return None
    polls = [json.loads(l) for l in jsonl.read_text().splitlines() if l.strip()]
    per: dict[str, dict] = {}
    ok = [p for p in polls if "raw" in p]
    for p in ok:
        for x in parse_sessions(p["raw"]):
            d = per.setdefault(x["remote"], {"starts": set(), "max_prefixes": 0, "seen_down": 0})
            if x["last_started"]:
                d["starts"].add(x["last_started"])
            d["max_prefixes"] = max(d["max_prefixes"], x["prefix_count"])
            d["seen_down"] += 0 if x["established"] else 1
    if not per:
        return {"polls": len(polls), "failed_polls": len(polls) - len(ok), "sessions": 0}
    starts = sorted(t for d in per.values() for t in d["starts"])
    return {"polls": len(polls), "failed_polls": len(polls) - len(ok), "sessions": len(per),
            "first_start": starts[0] if starts else None, "last_start": starts[-1] if starts else None,
            "restarts": sum(max(0, len(d["starts"]) - 1) for d in per.values()),
            "polls_not_established": sum(d["seen_down"] for d in per.values()),
            "max_prefixes_from_peers": sum(d["max_prefixes"] for d in per.values()),
            "per_peer": {k: {"starts": sorted(v["starts"]), "max_prefixes": v["max_prefixes"]} for k, v in per.items()}}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=str(REPO / "deploy" / "cluster.env"))
    ap.add_argument("--dut", default="bird", choices=["bird", *ADAPTERS],
                    help="bird = BIRD 2 stand-in started on --dut-node; mikrotik (RouterOS 7), cisco (IOS 15 / "
                         "IOS-XE), bird-ssh (existing BIRD 2/3), frr, quagga = external device at --dut-ip, "
                         "configured over SSH before the run and cleaned up after it")
    ap.add_argument("--dut-node", default="CMNODE06", help="node running BIRD as stand-in DUT (--dut bird)")
    ap.add_argument("--dut-ip", default="10.100.0.111", help="DUT BGP peering address (external DUT)")
    ap.add_argument("--dut-ssh-host", default="", help="DUT SSH/management address if not --dut-ip")
    ap.add_argument("--dut-port", type=int, default=22, help="DUT SSH port")
    ap.add_argument("--dut-user", default=None,
                    help="DUT SSH user (default admin for mikrotik/cisco, root for bird-ssh/frr/quagga); "
                         "password from TGEN_DUT_PASSWORD or prompt; Cisco enable secret from TGEN_DUT_ENABLE")
    ap.add_argument("--dut-key", default=None, help="SSH private key for the DUT (instead of a password)")
    ap.add_argument("--dut-no-sudo", action="store_true", help="Linux DUT: run birdc/vtysh without sudo")
    ap.add_argument("--dut-bird-include", default=None, help="bird-ssh: include file (default /etc/bird/tgen.conf)")
    ap.add_argument("--dut-bird-conf", default=None, help="bird-ssh: main config (default /etc/bird/bird.conf)")
    ap.add_argument("--dut-bird-socket", default=None, help="bird-ssh: birdc control socket")
    ap.add_argument("--dut-asn", type=int, default=64500)
    ap.add_argument("--dut-max-routes", type=int, default=None,
                    help="refuse larger runs (routes the DUT must hold); default 20 000 for mikrotik, none otherwise")
    ap.add_argument("--dut-poll", type=float, default=10,
                    help="external DUT: read its BGP session list every N s during the run (0 = off); "
                         "each poll is an SSH login on the DUT")
    ap.add_argument("--keep-dut-config", action="store_true", help="leave the BGP neighbours on the DUT after the run")
    ap.add_argument("--nodes", default="", help="router nodes, comma list (default: all except the DUT node)")
    ap.add_argument("--routers", type=int, default=60)
    ap.add_argument("--prefixes", type=int, default=10_000)
    ap.add_argument("--role", default=None, choices=["route_server", "route_reflector", "router"],
                    help="DUT role (default: router for mikrotik, route_server otherwise)")
    ap.add_argument("--rate", type=float, default=0, help="prefixes/s per router (0 = max)")
    ap.add_argument("--per-update", type=int, default=0)
    ap.add_argument("--converge-timeout", type=float, default=300)
    ap.add_argument("--withdraw-timeout", type=float, default=120)
    ap.add_argument("--seed", type=int, default=int(time.time()) % 100000)
    ap.add_argument("--no-build", action="store_true")
    ap.add_argument("--bird-multihop", action="store_true", help=argparse.SUPPRESS)  # loopback simulation only
    args = ap.parse_args()
    role_defaulted = args.role is None
    args.role = args.role or DEFAULT_ROLE.get(args.dut, "route_server")

    env = load_env(Path(args.config))
    cl = Cluster(env)
    names = env["NODE_HOSTNAMES"]
    external = args.dut != "bird"
    if not external and args.dut_node not in names:
        sys.exit(f"unknown DUT node {args.dut_node}")
    # with an external DUT every node can host routers; with BIRD the DUT node is reserved
    router_nodes = [n for n in (args.nodes.split(",") if args.nodes else names)
                    if n and (external or n != args.dut_node)]
    controller = names[0]
    nodes = [NodeSpec(n, 0.7 if n == controller else 1.0, [1, 2, 3]) for n in router_nodes]
    dut_ip = args.dut_ip if external else cl.traffic[args.dut_node]
    mt = None
    if external:
        mt = make_dut(args.dut, args.dut_ssh_host or args.dut_ip, args.dut_user, port=args.dut_port, key=args.dut_key,
                      no_sudo=args.dut_no_sudo, bird_include=args.dut_bird_include, bird_conf=args.dut_bird_conf,
                      bird_socket=args.dut_bird_socket, multihop=args.bird_multihop)
    dut_label = f"{mt.platform} {args.dut_ip}" if external else f"BIRD on {args.dut_node}"
    dut_routes = args.routers * args.prefixes
    max_routes = args.dut_max_routes if args.dut_max_routes is not None else (mt.default_max_routes if mt else None)
    if external and max_routes and dut_routes > max_routes:
        sys.exit(f"{args.routers} x {args.prefixes} = {dut_routes} routes is above the limit of {max_routes} for "
                 f"this DUT ({mt.platform}). Reduce --routers/--prefixes, or raise --dut-max-routes deliberately.")
    if external:
        mt.ask_password_if_needed()
        try:
            res = mt.resources()
        except DutError as e:
            sys.exit(f"DUT check failed: {e}")
        log(f"DUT: {res['summary']}")
        if role_defaulted:
            log(f"role: {args.role} (default for --dut {args.dut})")

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
            cfg = mt.render(meta, dut_ip, args.dut_asn, args.role)
            (out / f"dut-config.{mt.config_ext}").write_text(cfg)
            dut_configured = True      # set before push: a half-applied config is removed too
            n = mt.push(cfg, expected=len(meta["routers"]))
            for note in mt.notes:
                log(f"DUT note: {note}")
            log(f"DUT configured: {n} BGP neighbours (tagged \"tgen\"), role {args.role}, AS {args.dut_asn}")
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
            # -n / DEVNULL: background sessions must never read the user's terminal
            procs[n] = subprocess.Popen(["ssh", "-n", *SSH_OPTS, f"{cl.user}@{cl.mgmt[n]}",
                                         f"cd ~ && {run} 2> {REMOTE_DIR}/agent.log"], stdin=subprocess.DEVNULL)
        mid = tm["announce_at"] / 1000 + 5
        next_poll = tm["establish_at"] / 1000 if external and args.dut_poll > 0 else None
        polls = (out / "dut-sessions.jsonl").open("w") if next_poll else None
        poll_fail = 0
        while time.time() < tm["end_at"] / 1000 + 20 and any(p.poll() is None for p in procs.values()):
            if next_poll and time.time() >= next_poll and time.time() < tm["end_at"] / 1000:
                # DUT's own view of the sessions, for the event timeline (F9)
                t_poll = time.time()
                mt.call_timeout = 15            # a poll must never stall the run
                try:
                    sess, est = mt.sessions()
                    polls.write(json.dumps({"t": round(t_poll, 3), "established": est, "raw": sess}) + "\n")
                    poll_fail = 0
                except DutError as e:
                    polls.write(json.dumps({"t": round(t_poll, 3), "error": str(e)[:300]}) + "\n")
                    poll_fail += 1
                    log(f"DUT poll failed ({poll_fail}): {e}")
                finally:
                    mt.call_timeout = None
                    polls.flush()
                next_poll = t_poll + args.dut_poll
                if poll_fail >= 2:
                    log("DUT polling stopped after 2 failures in a row (the DUT is not answering SSH in time)")
                    next_poll = None
            if mid and time.time() > mid:
                if external:
                    mt.call_timeout = 20
                    try:
                        sess, est = mt.sessions()
                        (out / "dut-sessions-mid.txt").write_text(sess)
                        res = mt.resources()
                        (out / "dut-resources-mid.json").write_text(json.dumps(res))
                        log(f"DUT mid-run: {est} sessions established; {res['summary']}")
                    except DutError as e:
                        log(f"DUT mid-run query failed: {e}")
                    finally:
                        mt.call_timeout = None
                else:
                    r = cl.ssh(args.dut_node, "sudo birdc -s /run/tgen-bird.ctl show protocols | grep -c Established; "
                                              "sudo birdc -s /run/tgen-bird.ctl show memory | tail -1", check=False)
                    (out / "bird-mid.txt").write_text(r.stdout)
                    log("BIRD mid-run: " + " | ".join(r.stdout.split("\n")[:2]).strip() + " sessions Established")
                mid = 0
            time.sleep(1)
        if polls:
            polls.close()
        for n, p in procs.items():
            if p.poll() is None:
                log(f"WARNING {n}: agent still running after the end of the run; stopping it")
                p.terminate()
            try:
                rc = p.wait(timeout=15)
            except subprocess.TimeoutExpired:
                p.kill()
                rc = p.wait()
            if rc != 0:
                log(f"WARNING {n}: agent exit code {rc}")

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
                (out / "dut-bgp.log").write_text(mt.log())
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
        # never leave an agent behind (e.g. one that did not stop at end_at)
        for n in added:
            cl.ssh(n, "sudo pkill -x tgen-agent; true", check=False, timeout=30)
        for n, ips in added.items():
            cl.ssh(n, "; ".join(f"sudo ip addr del {ip}/32 dev {cl.iface} 2>/dev/null" for ip in ips) + "; true",
                   check=False)
        if bird_started:
            cl.ssh(args.dut_node, "sudo pkill -x bird; true", check=False)

    (out / "meta.json").write_text(json.dumps(meta))
    report = analyze(meta, results)
    report["cpu_node_avg"] = cpu
    report["dut_view"] = dut_view(out / "dut-sessions.jsonl") if external and args.dut == "mikrotik" else None
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
        f"correctness   : missing {report['correctness']['missing_routes']:,}, extra "
        f"{report['correctness']['extra_routes']:,}, attribute violations {report['correctness']['attribute_violations']:,} "
        f"({attribute_detail(report, args.role)}), own routes sent back {report['correctness'].get('looped_rx', 0):,}",
        *([f"DUT view      : {dv['sessions']} sessions, started {(dv.get('first_start') or '?')[-8:]}–"
            f"{(dv.get('last_start') or '?')[-8:]} (DUT clock), restarts {dv.get('restarts', 0)}, "
            f"peak prefixes from our routers {dv.get('max_prefixes_from_peers', 0):,} "
            f"({dv['polls']} polls, {dv['failed_polls']} failed)"] if (dv := report.get("dut_view")) else []),
        "CPU node-avg  : " + ", ".join(f"{n} avg {v['avg']:.0f} % p95 {v['p95']:.0f} %"
                                        for n, v in cpu.items() if v["avg"] is not None),
        "",
        "P2 gate:",
    ]
    ok = True
    for name, passed, detail in gate(report, gen_cpu, role=args.role, reference_dut=not external):
        lines.append(f"  {'INFO' if passed is None else 'PASS' if passed else 'FAIL'}  {name}: {detail}")
        ok &= passed is not False
    text = "\n".join(lines)
    (out / "summary.txt").write_text(text + "\n")
    print("\n" + text + f"\n\nfiles: {out}")
    return 0 if ok else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except DutError as e:
        sys.exit(f"DUT error: {e}")
