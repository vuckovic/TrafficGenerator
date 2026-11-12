#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Browser end-to-end test of the web interface (docs/09 §4), P3c-1.

The same loopback setup as test_e2e.py (six agents, BIRD route server on 10.100.0.9,
the Cisco mock), the controller serving ui/web, and Chromium (Playwright) using the
interface as a person would: log in, run a quick check from the Benchmark tab, follow
the live view, use the DUT buttons on the mock Cisco, stop a run, switch language.
Screenshots go to the work folder.

  sudo tests/controller/ui_e2e.py [--agent agent/build/tgen-agent] [--shots DIR]
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

REPO = Path(__file__).resolve().parents[2]
SIM = REPO / "tests" / "bgp" / "sim"
os.environ["TGEN_DUTS_FILE"] = str(SIM / "duts-sim.toml")
sys.path.insert(0, str(REPO / "controller"))
sys.path.insert(0, str(REPO / "tools"))
from tgen.planner import bird_config  # noqa: E402
from tgen_controller.auth import hash_password  # noqa: E402
from tgen_controller.engine import make_plan  # noqa: E402
from tgen_controller.scenario import Scenario  # noqa: E402
from tgen_controller.settings import load  # noqa: E402

TOKEN, PASSWORD = "ui-token-0123456789abcdef", "ui-admin-password"
DUT_IP, DUT_ASN, PORT = "10.100.0.9", 64500, 18081
NODES = [f"CMNODE0{i}" for i in range(1, 7)]
ROUTERS, PREFIXES, SEED, HOLD = 12, 500, 7, 9
FAILS: list[str] = []


def check(cond: bool, what: str) -> None:
    print(("  PASS  " if cond else "  FAIL  ") + what, flush=True)
    if not cond:
        FAILS.append(what)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--agent", default=str(REPO / "agent" / "build" / "tgen-agent"))
    ap.add_argument("--shots", default=None)
    args = ap.parse_args()
    work = Path(tempfile.mkdtemp(prefix="tgen-ui-e2e-"))
    shots = Path(args.shots or work / "shots"); shots.mkdir(parents=True, exist_ok=True)
    print(f"work folder {work}")
    (work / "token").write_text(TOKEN + "\n")
    (work / "cluster.env").write_text((REPO / "deploy" / "cluster.env.example").read_text())
    ports = {n: 7700 + i for i, n in enumerate(NODES, 1)}
    (work / "controller.toml").write_text(
        f'password_hash = "{hash_password(PASSWORD)}"\n'
        f'data_dir = "{work / "data"}"\ncluster_env = "{work / "cluster.env"}"\n'
        f'agent_token_file = "{work / "token"}"\ntraffic_iface = "lo"\nsecure_cookie = false\n'
        "[agents]\n" + "".join(f'{n} = "127.0.0.1:{p}"\n' for n, p in ports.items()))
    (work / "sim").mkdir()
    env = os.environ | {"TGEN_CONTROLLER_CONFIG": str(work / "controller.toml"), "SIM_ROOT": str(work / "sim"),
                        "TGEN_DUT_PASSWORD_SIM_ASR": "dummy",
                        "PYTHONPATH": f"{SIM / 'pymock'}:{REPO / 'controller'}:{REPO / 'tools'}"}
    procs: list[subprocess.Popen] = []
    base = f"http://127.0.0.1:{PORT}"
    try:
        cfg = load(work / "controller.toml")
        sc = Scenario.model_validate({"dut": {"ip": DUT_IP, "asn": DUT_ASN, "role": "route_server", "configure": False},
                                      "routers": {"total": ROUTERS, "prefixes": PREFIXES, "seed": SEED, "hold_time": HOLD},
                                      "phases": [{"type": "establish"}, {"type": "announce"}]})
        _, meta = make_plan(cfg, sc, "bird", DUT_IP, DUT_ASN, "route_server")
        subprocess.run(["ip", "addr", "replace", f"{DUT_IP}/32", "dev", "lo"], check=True)
        (work / "bird.conf").write_text(bird_config(meta, DUT_IP, DUT_ASN, multihop=True, log_file=str(work / "bird.log")))
        subprocess.run(["bird", "-c", str(work / "bird.conf"), "-s", str(work / "bird.ctl"), "-P", str(work / "bird.pid")], check=True)
        for n in NODES:
            log = open(work / f"agent-{n}.log", "w")
            procs.append(subprocess.Popen([args.agent, "serve", "--listen", f"127.0.0.1:{ports[n]}", "--token-file", str(work / "token")],
                                          stdout=log, stderr=log))
        clog = open(work / "controller.log", "w")
        procs.append(subprocess.Popen([sys.executable, "-m", "tgen_controller", "serve", "--port", str(PORT)], env=env, stdout=clog, stderr=clog))
        for _ in range(50):
            try:
                httpx.get(base + "/api/health"); break
            except httpx.HTTPError:
                time.sleep(0.2)

        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            b = p.chromium.launch()
            ctx = b.new_context(viewport={"width": 1440, "height": 1000}, locale="en-GB")
            pg = ctx.new_page()
            errors: list[str] = []
            pg.on("pageerror", lambda e: errors.append(str(e)))
            pg.on("console", lambda m: errors.append(m.text) if m.type == "error" and "401" not in m.text else None)

            print("login", flush=True)
            pg.goto(base + "/")
            pg.wait_for_selector("#login:not([hidden])")
            check(pg.locator("#login h1").inner_text() == "Log in", "login page in English")
            pg.screenshot(path=str(shots / "login-en.png"))
            pg.fill("#pw", "wrong"); pg.click("#loginForm button[type=submit]")
            pg.wait_for_selector("#loginErr:not([hidden])")
            check("Wrong password" in pg.inner_text("#loginErr"), "wrong password message")
            pg.fill("#pw", PASSWORD); pg.click("#loginForm button[type=submit]")
            pg.wait_for_selector("#app:not([hidden])")
            pg.wait_for_function("document.querySelectorAll('#device option').length >= 3")
            check(pg.inner_text("#tabs a[aria-current=page]") == "Benchmark", "Benchmark tab active")
            opts = pg.locator("#device option").all_inner_texts()
            check(any("simulated IOS-XE" in o for o in opts) and opts[-1].startswith("Custom"), f"device selector from the inventory ({len(opts)} entries)")
            check(pg.inner_text("#badgeText") == "IDLE", "badge IDLE")

            print("DUT buttons on the Cisco mock", flush=True)
            pg.select_option("#device", "sim-asr")
            check(pg.input_value("#dutIp") == "10.100.0.111" and pg.input_value("#guard") == "10", "device fills IP and memory guard")
            check(pg.locator("#roleSeg label.on").inner_text() == "Router (forwards traffic)", "device fills the role")
            check(not pg.locator("#mrai").is_disabled(), "MRAI enabled for Cisco")
            pg.click("[data-dut=test]")
            pg.wait_for_function("document.getElementById('dutStatus').textContent.includes('Connected') || document.getElementById('dutStatus').textContent.includes('failed')", timeout=30000)
            check("Connected" in pg.inner_text("#dutStatus"), "Test connection: " + pg.inner_text("#dutStatus")[:80])
            pg.click("[data-dut=preview]")
            pg.wait_for_selector("#previewBox:not([hidden])")
            check("router bgp" in pg.inner_text("#previewText"), "Preview config shows IOS config")
            pg.select_option("#device", "sim-1841")
            check(pg.inner_text("#pwState").startswith("not set"), "1841: password not set on the controller")

            print("quick check against BIRD, configured by hand", flush=True)
            pg.select_option("#device", "custom")
            pg.click("#roleSeg label:nth-child(1)")
            pg.fill("#dutIp", DUT_IP); pg.fill("#dutAsn", str(DUT_ASN))
            pg.uncheck("#configure")
            pg.fill("#rTotal", str(ROUTERS)); pg.fill("#rPrefixes", str(PREFIXES)); pg.fill("#rHold", str(HOLD)); pg.fill("#seed", str(SEED))
            pg.fill("#runName", "ui quick check")
            pg.select_option("#preset", "quick_check")
            pg.click("#phases li:nth-child(3) button")
            pg.fill("#pf_dur", "8"); pg.press("#pf_dur", "Tab")
            check("8 s" in pg.inner_text("#phases li:nth-child(3)"), "phase parameter edited")
            pg.click("#startBtn")
            pg.wait_for_selector("#runStrip:not([hidden])", timeout=3000)
            first = pg.inner_text("#stripText")
            check(any(w in first for w in ("Starting", "Preparing", "Running")), f"run strip appears at once: {first}")
            check(pg.title().startswith("●"), "page title marks the active run")
            pg.wait_for_function("document.getElementById('stripText').textContent.startsWith('Running')", timeout=30000)
            check(pg.locator("#stripSegs span").count() == 4 and not pg.is_hidden("#stripSpin"), "strip: phase progress and spinner while running")
            pg.wait_for_function("document.getElementById('badgeText').textContent.startsWith('RUNNING')", timeout=30000)
            check(pg.is_disabled("#startBtn") and not pg.is_disabled("#stopBtn"), "Start disabled, Stop enabled while running")
            pg.wait_for_function(f"document.getElementById('kSess').textContent.startsWith('{ROUTERS}/')", timeout=30000)
            check(True, "live: all sessions shown")
            pg.wait_for_function("document.querySelectorAll('#routersBody tr').length >= 12", timeout=10000)
            check("Established" in pg.inner_text("#routersBody"), "routers table shows session states")
            pg.wait_for_function("document.getElementById('kConv').textContent.endsWith(' s')", timeout=60000)
            check(True, "initial convergence shown: " + pg.inner_text("#kConv"))
            time.sleep(3)
            pg.screenshot(path=str(shots / "benchmark-running-en.png"), full_page=True)
            check(pg.locator("#latChart canvas").count() >= 1, "latency chart drawn")
            check("p50" in pg.inner_text("#readout"), "chart readout: " + pg.inner_text("#readout")[:80])
            check(pg.locator("#genHealth .mono").count() >= 12, "generator health for 6 nodes")
            pg.wait_for_function("document.getElementById('badgeText').textContent.startsWith('DONE')", timeout=120000)
            check(True, "run DONE in the badge")
            pg.wait_for_function("document.getElementById('stripText').textContent === 'Run finished.'", timeout=10000)
            check("Summary" in pg.inner_text("#stripLinks"), "strip: finished, with summary link")
            pg.wait_for_selector("#runLine a")
            check("Summary" in pg.inner_text("#runLine"), "summary link after the run")
            check(len(pg.locator("#events li").all()) >= 5, "events listed")

            print("language", flush=True)
            pg.click("header .langsel .hbtn")
            pg.click("header .langsel .menu button[lang=sr-Cyrl]")
            pg.wait_for_function("document.documentElement.lang === 'sr-Cyrl'")
            check(pg.inner_text("#stripText") == "Тест је завршен.", "Serbian strip: finished")
            check("Резиме" in pg.inner_text("#stripLinks"), "Serbian strip: summary link")
            check(pg.inner_text("#tabs a[aria-current=page]") == "Мерење", "Serbian tab names")
            check(pg.inner_text("#startBtn") == "Покрени", "Serbian Start")
            pg.screenshot(path=str(shots / "benchmark-done-sr.png"), full_page=True)
            pg.reload(); pg.wait_for_selector("#app:not([hidden])")
            check(pg.inner_text("#startBtn") == "Покрени", "language kept after reload")

            print("Cisco mock run, stopped from the browser", flush=True)
            subprocess.run(["kill", (work / "bird.pid").read_text().strip()], check=False); time.sleep(1)
            pg.select_option("#device", "sim-asr")
            pg.check("#configure")
            pg.fill("#rTotal", "6"); pg.fill("#rPrefixes", "200")
            pg.select_option("#preset", "quick_check")
            pg.click("#phases li:nth-child(3) button"); pg.fill("#pf_dur", "120"); pg.press("#pf_dur", "Tab")
            pg.click("#startBtn")
            pg.wait_for_selector("#runStrip:not([hidden])", timeout=3000)
            check(any(w in pg.inner_text("#stripText") for w in ("Покретање", "Припрема", "У току")), "Serbian strip at Start: " + pg.inner_text("#stripText"))
            pg.wait_for_function("document.getElementById('stripText').textContent.startsWith('У току — фаза')", timeout=60000)
            check("протекло" in pg.inner_text("#stripTime"), "Serbian strip while running: " + pg.inner_text("#stripText"))
            pg.wait_for_function("document.getElementById('kSess').textContent.startsWith('6/')", timeout=60000)
            pg.wait_for_function("!document.getElementById('portCard').hidden", timeout=30000)
            check("Gi" in pg.inner_text("#portLine") or "Ethernet" in pg.inner_text("#portLine") or pg.inner_text("#portLine") != "", "port card (M9): " + pg.inner_text("#portLine"))
            time.sleep(4)
            check("активан" in pg.inner_text("#portLine"), "Serbian port line: " + pg.inner_text("#portLine"))
            pg.screenshot(path=str(shots / "benchmark-cisco-sr.png"), full_page=True)
            pg.click("#stopBtn")
            pg.wait_for_function("document.getElementById('badgeText').textContent.startsWith('ЗАУСТАВЉЕНО')", timeout=90000)
            check(True, "stopped from the browser")
            check(pg.inner_text("#stripText") == "Тест је заустављен.", "Serbian strip: stopped")
            pg.click("header .langsel .hbtn"); pg.click("header .langsel .menu button[lang=en]")

            print("phone width", flush=True)
            ph = b.new_context(viewport={"width": 390, "height": 844}, locale="sr-RS").new_page()
            ph.goto(base + "/"); ph.wait_for_selector("#login:not([hidden])")
            check(ph.inner_text("#login h1") == "Пријава", "first visit follows the browser language (sr)")
            ph.screenshot(path=str(shots / "login-phone-sr.png"))
            ph.fill("#pw", PASSWORD); ph.click("#loginForm button[type=submit]"); ph.wait_for_selector("#app:not([hidden])")
            ph.wait_for_timeout(1500)
            w = ph.evaluate("document.documentElement.scrollWidth")
            check(w <= 392, f"no horizontal scroll at 390 px (scroll width {w})")
            ph.screenshot(path=str(shots / "benchmark-phone.png"), full_page=True)

            print("other tabs", flush=True)
            pg.click("#tabs a[data-tab=settings]")
            pg.wait_for_function("document.querySelectorAll('#nodesBody tr').length === 6")
            check(True, "Nodes & settings shows 6 nodes")
            check(not errors, "no browser errors: " + "; ".join(errors[:3]))
            b.close()
    finally:
        for p in procs:
            p.terminate()
        for p in procs:
            try:
                p.wait(timeout=15)
            except subprocess.TimeoutExpired:
                p.kill()
        subprocess.run(["pkill", "-f", f"bird -c {work}/"], check=False)
        for ip in (DUT_IP, "10.100.0.111"):
            subprocess.run(["ip", "addr", "del", f"{ip}/32", "dev", "lo"], check=False, capture_output=True)
    print(f"\n{'FAILED: ' + str(len(FAILS)) if FAILS else 'ALL PASSED'}  (screenshots in {shots})")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
