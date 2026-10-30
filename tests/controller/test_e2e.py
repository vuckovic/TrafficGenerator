#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""End-to-end test of tgen-controller (docs/08 §9) on one Linux box.

Six real agents on 127.0.0.1:7701-7706 (router /32s on `lo`), BIRD 2 as route
server on 10.100.0.9 (loopback, multihop), the controller on 127.0.0.1:18080.
Everything goes through the controller's HTTP API and WebSocket, as the web UI will.
Needs root, bird and the Python packages of docs/08 §8.

  sudo tests/controller/test_e2e.py [--agent agent/build/tgen-agent]
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import httpx

REPO = Path(__file__).resolve().parents[2]
SIM = REPO / "tests" / "bgp" / "sim"
os.environ["TGEN_DUTS_FILE"] = str(SIM / "duts-sim.toml")   # mock Cisco DUTs (netmiko mock -> BIRD)
sys.path.insert(0, str(REPO / "controller"))
sys.path.insert(0, str(REPO / "tools"))
from tgen.planner import bird_config  # noqa: E402
from tgen_controller.auth import hash_password  # noqa: E402
from tgen_controller.engine import make_plan  # noqa: E402
from tgen_controller.scenario import Scenario  # noqa: E402
from tgen_controller.settings import load  # noqa: E402

TOKEN = "e2e-token-0123456789abcdef"
PASSWORD = "e2e-admin-password"
DUT_IP, DUT_ASN, PORT = "10.100.0.9", 64500, 18080
NODES = [f"CMNODE0{i}" for i in range(1, 7)]
FAILS: list[str] = []


def check(cond: bool, what: str) -> None:
    print(("  PASS  " if cond else "  FAIL  ") + what, flush=True)
    if not cond:
        FAILS.append(what)


def routers_block(routers: int, prefixes: int) -> dict:
    return {"total": routers, "prefixes": prefixes, "seed": 7, "hold_time": 9}


def wait_state(c: httpx.Client, rid: str, states: tuple, timeout: float) -> dict:
    t0 = time.time()
    while time.time() - t0 < timeout:
        r = c.get(f"/api/runs/{rid}").json()
        if r["state"] in states:
            return r
        time.sleep(1)
    return c.get(f"/api/runs/{rid}").json()


class Live(threading.Thread):
    """Follows /ws/live like the UI does."""

    def __init__(self, cookie: str):
        super().__init__(daemon=True)
        self.cookie, self.msgs, self.stop = cookie, [], False
        self.ready = threading.Event()

    def run(self) -> None:
        from websockets.sync.client import connect
        with connect(f"ws://127.0.0.1:{PORT}/ws/live", additional_headers={"Cookie": f"tgen_session={self.cookie}"}) as ws:
            self.ready.set()
            while not self.stop:
                try:
                    self.msgs.append((time.time(), json.loads(ws.recv(timeout=1))))
                except TimeoutError:
                    continue


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--agent", default=str(REPO / "agent" / "build" / "tgen-agent"))
    ap.add_argument("--routers", type=int, default=12)
    ap.add_argument("--prefixes", type=int, default=500)
    ap.add_argument("--keep", action="store_true", help="keep the work folder")
    args = ap.parse_args()
    work = Path(tempfile.mkdtemp(prefix="tgen-ctl-e2e-"))
    print(f"work folder {work}")
    (work / "token").write_text(TOKEN + "\n")
    (work / "cluster.env").write_text((REPO / "deploy" / "cluster.env.example").read_text())
    ports = {n: 7700 + i for i, n in enumerate(NODES, 1)}
    (work / "controller.toml").write_text(
        f'password_hash = "{hash_password(PASSWORD)}"\n'
        f'data_dir = "{work / "data"}"\ncluster_env = "{work / "cluster.env"}"\n'
        f'agent_token_file = "{work / "token"}"\ntraffic_iface = "lo"\nsecure_cookie = false\n'
        "max_duration_s = 600\n\n[agents]\n" + "".join(f'{n} = "127.0.0.1:{p}"\n' for n, p in ports.items()))
    (work / "sim").mkdir()
    env = os.environ | {"TGEN_CONTROLLER_CONFIG": str(work / "controller.toml"), "SIM_ROOT": str(work / "sim"),
                        "TGEN_DUT_PASSWORD_SIM_ASR": "dummy",
                        "PYTHONPATH": f"{SIM / 'pymock'}:{REPO / 'controller'}:{REPO / 'tools'}"}
    procs: list[subprocess.Popen] = []
    try:
        # --- BIRD as route server, configured for exactly the routers the controller will plan
        cfg = load(work / "controller.toml")
        quick = Scenario.model_validate({
            "name": "e2e quick check", "dut": {"ip": DUT_IP, "asn": DUT_ASN, "role": "route_server", "configure": False},
            "routers": routers_block(args.routers, args.prefixes),
            "phases": [{"type": "establish"}, {"type": "announce", "timeout_s": 60},
                       {"type": "steady", "duration_s": 5}, {"type": "teardown", "timeout_s": 60}]})
        _, meta = make_plan(cfg, quick, "bird", DUT_IP, DUT_ASN, "route_server")
        subprocess.run(["ip", "addr", "replace", f"{DUT_IP}/32", "dev", "lo"], check=True)
        (work / "bird.conf").write_text(bird_config(meta, DUT_IP, DUT_ASN, multihop=True, log_file=str(work / "bird.log")))
        subprocess.run(["bird", "-c", str(work / "bird.conf"), "-s", str(work / "bird.ctl"), "-P", str(work / "bird.pid")],
                       check=True)

        # --- agents and controller
        for n in NODES:
            log = open(work / f"agent-{n}.log", "w")
            procs.append(subprocess.Popen([args.agent, "serve", "--listen", f"127.0.0.1:{ports[n]}",
                                           "--token-file", str(work / "token")], stdout=log, stderr=log))
        clog = open(work / "controller.log", "w")
        procs.append(subprocess.Popen([sys.executable, "-m", "tgen_controller", "serve", "--port", str(PORT)],
                                      env=env, stdout=clog, stderr=clog))
        c = httpx.Client(base_url=f"http://127.0.0.1:{PORT}", timeout=30)
        for _ in range(50):
            try:
                c.get("/api/health")
                break
            except httpx.HTTPError:
                time.sleep(0.2)

        print("session and status", flush=True)
        check(c.get("/api/health").status_code == 200, "/api/health without login")
        check(c.get("/api/cluster").status_code == 401, "/api/cluster needs a session")
        check(c.post("/api/login", json={"password": "wrong"}).status_code == 401, "wrong password refused")
        r = c.post("/api/login", json={"password": PASSWORD})
        check(r.status_code == 200 and "tgen_session" in c.cookies, "login sets the session cookie")
        sc_hdr = r.headers.get("set-cookie", "").lower()
        check("httponly" in sc_hdr and "samesite=strict" in sc_hdr, "cookie is HttpOnly, SameSite=Strict")
        cl = c.get("/api/cluster").json()["nodes"]
        check(len(cl) == 6 and all(n["reachable"] and n["state"] == "idle" for n in cl), "6 agents reachable and idle")
        check(all(abs(n["clock_offset_ms"]) < 50 for n in cl), "clock offsets < 50 ms")
        check({"quick_check", "throughput_knee"} <= set(c.get("/api/presets").json()), "presets listed")
        duts = c.get("/api/duts").json()
        check("sim-asr" in duts and duts["sim-asr"]["password_set"] and "password" not in json.dumps(duts).replace("password_set", ""), "DUT inventory, no secrets")
        bad = c.post("/api/runs", json={"scenario": {"dut": {"ip": DUT_IP}, "phases": [{"type": "announce"}]}})
        check(bad.status_code == 422, "invalid scenario refused (first phase must be establish)")

        # --- run 1: quick check, followed live
        print("run 1: quick check", flush=True)
        live = Live(c.cookies["tgen_session"])
        live.start()
        check(live.ready.wait(5), "WebSocket connected with the session cookie")
        r = c.post("/api/runs", json={"scenario": quick.model_dump()})
        check(r.status_code == 202, f"run accepted ({r.status_code})")
        rid = r.json()["id"]
        run = wait_state(c, rid, ("done", "failed", "stopped"), 240)
        live.stop = True
        live.join(3)
        check(run["state"] == "done", f"run 1 done ({run['state']}, {run.get('error')})")
        s = run.get("summary") or {}
        check(s.get("gate_ok") is True, "gate passed: " + "; ".join(f"{g['name']}={g['passed']}" for g in s.get("gate", [])))
        check(s.get("sessions") == f"{args.routers}/{args.routers}", f"sessions {s.get('sessions')}")
        types = [p["type"] for p in run["phases"]]
        check(types == ["establish", "announce", "steady", "teardown"], f"phases recorded {types}")
        check(all(p["end_ms"] for p in run["phases"]), "every phase has an end time")
        ticks = [m for _, m in live.msgs if m.get("type") == "tick" and m.get("run") == rid]
        check(len(ticks) >= 5, f"live ticks received ({len(ticks)})")
        check(any(t["sessions"].get("Established") == args.routers for t in ticks), "live: all Established seen")
        check(any((t["latency_ms"]["n"] or 0) > 0 for t in ticks), "live: latency joined from samples")
        check(any(t["nodes"] and t["nodes"][0].get("cpu_pct") is not None for t in ticks), "live: node CPU reported")
        states = [m["state"] for _, m in live.msgs if m.get("type") == "state"]
        check(states[:2] == ["preparing", "running"] and states[-1] == "done", f"state events {states}")
        check(any(m.get("type") == "event" for _, m in live.msgs), "events streamed")
        files = {f["name"] for f in c.get(f"/api/runs/{rid}/files").json()}
        need = {"scenario.json", "meta.json", "report.json", "summary.txt", "live.jsonl"} | {f"result-{n}.json" for n in NODES}
        check(need <= files, f"run folder complete (missing {sorted(need - files)})")
        check(c.get(f"/api/runs/{rid}/report").json()["sessions"]["established"] == args.routers, "report.json served")
        check(c.get(f"/api/runs/{rid}/files/..%2Fcontroller.db").status_code == 404, "no path traversal")
        check(len(c.get(f"/api/runs/{rid}/ticks").json()) >= 5, "ticks stored in SQLite")
        st = [n["state"] for n in c.get("/api/cluster").json()["nodes"]]
        check(all(x == "idle" for x in st), f"agents idle after the run {st}")
        lo = subprocess.run(["ip", "-4", "-o", "addr", "show", "dev", "lo"], capture_output=True, text=True).stdout
        check(not any(r_["ip"] + "/32" in lo for r_ in meta["routers"]), "router addresses removed")
        h = c.get("/api/health").json()
        check(h["run_max"]["api_p95_ms"] < 500 and h["run_max"]["loop_lag_ms"] < 2000, f"AC6 figures {h['run_max']}")
        check(not h["violations"], f"no AC6 violations {h['violations']}")

        # --- run 2: stepped churn and a flap, stopped by the user
        print("run 2: churn + flap, then stop", flush=True)
        sc2 = quick.model_copy(deep=True)
        sc2.name = "e2e stop"
        sc2 = Scenario.model_validate(sc2.model_dump() | {"phases": [
            {"type": "establish"}, {"type": "announce", "timeout_s": 60},
            {"type": "churn", "rate": 5, "step": {"add": 5, "every_s": 3, "max": 15}, "duration_s": 9},
            {"type": "flap", "kind": "reset", "routers": 2, "down_s": 2, "duration_s": 6},
            {"type": "steady", "duration_s": 120}, {"type": "teardown"}]})
        rid2 = c.post("/api/runs", json={"scenario": sc2.model_dump()}).json()["id"]
        check(c.post("/api/runs", json={"scenario": quick.model_dump()}).status_code == 409, "second run refused (409)")
        t0 = time.time()
        while time.time() - t0 < 90:
            ph = [p["type"] for p in c.get(f"/api/runs/{rid2}").json()["phases"]]
            if "steady" in ph:
                break
            time.sleep(1)
        run2 = c.get(f"/api/runs/{rid2}").json()
        churn = [p for p in run2["phases"] if p["type"] == "churn"]
        check(bool(churn) and churn[0]["end_ms"] - churn[0]["at_ms"] >= 8500, "stepped churn ran its duration")
        flap = [p for p in run2["phases"] if p["type"] == "flap"]
        check(bool(flap) and len(flap[0]["params"].get("selected", [])) == 2, "flap selected 2 routers")
        t_stop = time.time()
        check(c.post(f"/api/runs/{rid2}/stop").status_code == 200, "stop accepted")
        st = [n["state"] for n in c.get("/api/cluster").json()["nodes"]]
        check(time.time() - t_stop < 2 and all(x in ("finished", "idle") for x in st),
              f"agents stopped within 1 s of the request ({st})")
        run2 = wait_state(c, rid2, ("done", "failed", "stopped"), 120)
        check(run2["state"] == "stopped", f"run 2 ended stopped ({run2['state']})")
        check(any("stopped" in e["text"] for e in run2["events"]), "stop recorded as an event")
        check((run2.get("summary") or {}).get("resets") == 0, "flap resets counted as planned, not unexpected")
        check(len(c.get("/api/runs").json()) == 2, "history lists both runs")
        check(c.delete(f"/api/runs/{rid2}").status_code == 200 and c.get(f"/api/runs/{rid2}").status_code == 404,
              "finished run deleted")

        # --- run 3: a Cisco DUT configured by the controller (netmiko mock: IOS config -> BIRD)
        print("run 3: Cisco DUT (mock) configured by the controller", flush=True)
        subprocess.run(["kill", (work / "bird.pid").read_text().strip()], check=False)   # frees port 179
        time.sleep(1)
        pv = c.post("/api/duts/sim-asr/preview", json={"routers": 6}).json()
        check(pv.get("ok") and "router bgp" in pv.get("config", ""), "DUT preview renders IOS config")
        sc3 = Scenario.model_validate({
            "name": "e2e cisco mock", "dut": {"profile": "sim-asr", "mrai": 0, "poll_s": 3},
            "routers": routers_block(6, 200),
            "phases": [{"type": "establish", "timeout_s": 60}, {"type": "announce", "timeout_s": 60},
                       {"type": "steady", "duration_s": 6}, {"type": "withdraw", "timeout_s": 60}]})
        rid3 = c.post("/api/runs", json={"scenario": sc3.model_dump()}).json()["id"]
        run3 = wait_state(c, rid3, ("done", "failed", "stopped"), 240)
        check(run3["state"] == "done", f"run 3 done ({run3['state']}, {run3.get('error')})")
        s3 = run3.get("summary") or {}
        check(s3.get("sessions") == "6/6" and s3.get("missing") == 0, f"Cisco mock: sessions {s3.get('sessions')}, "
              f"missing {s3.get('missing')}")
        check(any(g["passed"] is None for g in s3.get("gate", [])), "attribute checks are INFO on a real device")
        f3 = {f["name"] for f in c.get(f"/api/runs/{rid3}/files").json()}
        check({"dut-config.ios", "dut-sessions.jsonl", "dut-bgp.log"} <= f3, f"DUT files in the run folder {sorted(f3)}")
        check("advertisement-interval 0" in c.get(f"/api/runs/{rid3}/files/dut-config.ios").text, "MRAI 0 pushed")
        check("dummy" not in json.dumps(c.get(f"/api/runs/{rid3}").json()), "DUT password not stored")
        t = c.post("/api/duts/sim-asr/test", json={}).json()
        check(t.get("ok") and t.get("tgen_neighbours") == 0, f"tgen neighbours removed after the run ({t.get('tgen_neighbours')})")

        # --- lock-out last: it blocks this address
        for _ in range(5):
            c.post("/api/login", json={"password": "wrong"})
        check(c.post("/api/login", json={"password": PASSWORD}).status_code == 429, "lock-out after 5 failures")
        c.post("/api/logout")
    finally:
        for p in procs:
            p.terminate()
        for p in procs:
            try:
                p.wait(timeout=15)
            except subprocess.TimeoutExpired:
                p.kill()
        pid = work / "bird.pid"
        if pid.exists():
            subprocess.run(["kill", pid.read_text().strip()], check=False)
        subprocess.run(["pkill", "-f", f"bird -c {work}/"], check=False)
        for ip in (DUT_IP, "10.100.0.111"):
            subprocess.run(["ip", "addr", "del", f"{ip}/32", "dev", "lo"], check=False, capture_output=True)
    print(f"\n{'FAILED: ' + str(len(FAILS)) if FAILS else 'ALL PASSED'}  (logs in {work})")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
