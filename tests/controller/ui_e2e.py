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
import re
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
            h0 = pg.evaluate("document.getElementById('latChart').getBoundingClientRect().top")
            fit = pg.evaluate("""() => { const r = []; for (const id of ['kLat','kOut','kConv']) { const e = document.getElementById(id);
                e.textContent = '999\u202F999 ms'; r.push(e.scrollWidth <= e.parentElement.clientWidth - 30 && e.getBoundingClientRect().height < 40); } return r; }""")
            h1 = pg.evaluate("document.getElementById('latChart').getBoundingClientRect().top")
            check(all(fit) and abs(h1 - h0) < 1, f"KPI tiles hold '999 999 ms' on one line, chart does not move ({fit}, {h0:.0f}->{h1:.0f})")
            for w in (1440, 1280, 1024, 390):
                pg.set_viewport_size({"width": w, "height": 1000})
                ok = pg.evaluate("""() => ['kLat','kOut','kConv','kSess'].every(id => { const e = document.getElementById(id);
                    e.textContent = '999\u202F999 ms'; return e.getBoundingClientRect().height < 40 && e.scrollWidth <= e.parentElement.clientWidth; })""")
                check(ok, f"KPI values on one line at {w} px")
            pg.set_viewport_size({"width": 1440, "height": 1000})
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
            pg.fill("#rTotal", "6"); pg.fill("#rPrefixes", "200"); pg.fill("#runName", "ui cisco stop")
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

            print("test scheme on the Cisco mock", flush=True)
            pg.goto(base + "/#scheme"); pg.wait_for_selector("#app:not([hidden])")
            pg.wait_for_selector("#topoBox svg")
            topo = pg.inner_html("#topoBox")
            check(all(x in topo for x in ("CMNODE01", "CMNODE06", "10.100.0.111", "192.168.100.11", "controller")), "K12 topology: nodes, device, addresses")
            check(pg.is_visible("#k1body") and "How this test works" in pg.inner_text("#k1h"), "K1 instructions shown")
            pg.click("#k1Toggle"); check(pg.is_hidden("#k1body"), "K1 can be hidden"); pg.click("#k1Toggle")
            check(pg.locator("#schDevs tr").count() == 2 and pg.locator("#schSizes tr").count() == 2, "K2 devices and sizes from the file")
            pg.locator("#schDevs tr").nth(0).locator("input[type=checkbox]").uncheck()
            pg.click("#schStart")
            check("sim-1841" not in pg.inner_text("#schErr") and "password" in pg.inner_text("#schErr").lower(), "password asked for a device without one: " + pg.inner_text("#schErr"))
            pg.locator("#schDevs tr").nth(0).locator("input[type=checkbox]").check()
            pg.locator("#schDevs tr").nth(1).locator("input[type=checkbox]").uncheck()
            pg.locator("#schDevs tr").nth(0).locator("button").click()
            pg.wait_for_function("document.querySelector('#schDevs tr').textContent.includes('OK ·')", timeout=30000)
            check(True, "K2 connection test from the scheme page")
            pg.locator("#schSteps input").nth(3).uncheck()
            pg.fill("#schPause", "2")
            pg.wait_for_function("document.getElementById('schEst').textContent.includes('min')")
            check("min" in pg.inner_text("#schEst"), "K7 estimate: " + pg.inner_text("#schEst"))
            pg.click("#schStart")
            pg.wait_for_function("document.getElementById('stripText').textContent.startsWith('Test scheme')", timeout=10000)
            check(not pg.is_hidden("#runStrip") and pg.is_disabled("#startBtn") and not pg.is_disabled("#schStop"), "scheme running: strip, Start disabled, Stop scheme enabled")
            pg.wait_for_function("document.querySelector('#topoBox svg').textContent.includes('testing now')", timeout=30000)
            check(True, "topology highlights the device being tested")
            pg.wait_for_function("document.querySelector('#schProg .chip.running') !== null", timeout=30000)
            pg.screenshot(path=str(shots / "scheme-running-en.png"), full_page=True)
            pg.wait_for_function("document.getElementById('stripText').textContent === 'Test scheme finished.'", timeout=600000)
            chips = pg.locator("#schProg .chip").all_inner_texts()
            check(chips == ["Passed", "Passed", "Passed", "n/a", "n/a"], f"K9 progress grid {chips}")
            check(pg.locator("#cmpBody tr").count() == 1 and "not reached" in pg.inner_text("#cmpBody"), "K10 comparison row: " + pg.inner_text("#cmpBody")[:120])
            check(pg.locator("#kneeBox svg polyline").count() >= 1 and pg.locator("#kneeRead li").count() >= 1, "K11 knee chart with reading")
            pg.click("#kneeViews button:nth-child(2)"); check("limit" in pg.inner_html("#kneeBox"), "K11 view 2: time after last send with limits")
            pg.click("#kneeViews button:nth-child(3)"); check(pg.locator("#kneeBox svg rect").count() >= 2, "K11 view 3: sustained bars")
            with pg.expect_download() as dl:
                pg.click("#cmpCsv")
            check("Sustainable" in Path(dl.value.path()).read_text(), "K10 CSV export")
            with pg.expect_download() as dl:
                pg.click("#topoSvg")
            check("<svg" in Path(dl.value.path()).read_text(), "K12 SVG export")
            pg.screenshot(path=str(shots / "scheme-done-en.png"), full_page=True)
            pg.click("header .langsel .hbtn"); pg.click("header .langsel .menu button[lang=sr-Cyrl]")
            pg.wait_for_function("document.documentElement.lang === 'sr-Cyrl'")
            check(pg.inner_text("#stripText") == "Шема је завршена." and "Прошао" in pg.inner_text("#schProg"), "Serbian scheme page and strip")
            check("Топологија" in pg.inner_text("#k12h") and "готово" in pg.inner_html("#topoBox"), "Serbian topology")
            pg.screenshot(path=str(shots / "scheme-done-sr.png"), full_page=True)
            pg.click("header .langsel .hbtn"); pg.click("header .langsel .menu button[lang=en]")
            pg.goto(base + "/#benchmark"); pg.wait_for_selector("#app:not([hidden])")

            print("history, report, settings (P3c-3)", flush=True)
            pg.click("#tabs a[data-tab=history]")
            pg.wait_for_function("document.querySelectorAll('#hisBody tr').length >= 5")
            n_all = pg.locator("#hisBody tr").count()
            check("Passed" in pg.inner_text("#hisBody") or "PASS" in pg.inner_text("#hisBody"), f"H1 runs list ({n_all} rows) with verdicts")
            pg.select_option("#hisType", "scheme")
            n_s = pg.locator("#hisBody tr").count()
            check(0 < n_s < n_all and "Test scheme s-" in pg.inner_text("#hisBody"), f"filter by run type: {n_s} scheme runs")
            pg.select_option("#hisType", "")
            pg.fill("#hisQ", "ui quick check")
            check(pg.locator("#hisBody tr").count() == 1, "search by name")
            pg.fill("#hisQ", "")
            pg.click("#hisHead th:nth-child(6) button"); pg.click("#hisHead th:nth-child(6) button")
            check("▼" in pg.inner_text("#hisHead th:nth-child(6)"), "sortable columns")
            pg.screenshot(path=str(shots / "history-en.png"), full_page=True)
            row = pg.locator("#hisBody tr", has_text="ui quick check")
            row.locator("a", has_text="Open report").click()
            pg.wait_for_selector("#repPage:not([hidden])")
            pg.wait_for_function("document.querySelectorAll('#repGate tr').length >= 5")
            check("ui quick check" in pg.inner_text("#repTitle") and "PASS" in pg.inner_text("#repGate"), "X1 report: title and verdict table")
            check(pg.locator("#repKey tr").count() >= 8 and pg.locator("#repLat tr").count() == 2 and pg.locator("#repNodes tr").count() == 6, "key figures, latency, nodes")
            check(pg.locator("#repPhases tr").count() == 4 and pg.locator("#repEvents li").count() >= 3, "phases and events")
            check("report.json" in pg.inner_text("#repFiles"), "run files listed")
            pg.wait_for_function("document.querySelectorAll('#repFigs section.fig').length === 12")
            figs = pg.locator("#repFigs section.fig").evaluate_all("els => els.map(e => e.dataset.fig)")
            check(figs == [f"F{i}" for i in range(1, 13)], f"P4 figures F1-F12 on the report page {figs}")
            check(pg.locator("#repFigs section[data-fig=F2] svg path").count() >= 2 and "all receivers" in pg.inner_text("#repFigs section[data-fig=F2] .fig-read"), "F2 convergence curve with readout")
            r0 = pg.inner_text("#repFigs section[data-fig=F3] .fig-read")
            pg.locator("#repFigs section[data-fig=F3] input[type=range]").fill("900")
            check(pg.inner_text("#repFigs section[data-fig=F3] .fig-read") != r0, "F3 cursor slider moves the reading")
            pg.locator("#repFigs section[data-fig=F4] .fig-zoom button", has_text="4×").click()
            check(not pg.locator("#repFigs section[data-fig=F4] input[type=range]").nth(1).is_disabled(), "F4 zoom enables the window slider")
            pg.locator("#repFigs section[data-fig=F4] .fig-lg").first.click()
            check(pg.locator("#repFigs section[data-fig=F4] .fig-lg.off").count() == 1, "series hidden from the legend (X2)")
            check(pg.locator("#repFigs section[data-fig=F7] svg rect").count() > 100 and pg.locator("#repFigs section[data-fig=F9] svg circle").count() >= 12, "F7 heatmap and F9 timeline drawn")
            with pg.expect_download() as dl:
                pg.locator("#repFigs section[data-fig=F2] button", has_text="SVG").click()
            check("<svg" in Path(dl.value.path()).read_text(), "figure SVG export (X3)")
            with pg.expect_download() as dl:
                pg.click("#repHtml")
            ht = Path(dl.value.path()).read_text()
            check("<svg" in ht and "Verdict" in ht and "<button" not in ht, "report HTML export, one file")
            pg.emulate_media(media="print"); pg.screenshot(path=str(shots / "report-print-en.png"), full_page=True); pg.emulate_media(media="screen")
            rid_rep = pg.input_value("#repPick")
            pg.click("#repCompare")
            pg.wait_for_selector("#cmpDialog[open]")
            boxes = pg.locator("#cmpList input")
            for i in range(min(3, boxes.count())):
                boxes.nth(i).check()
            pg.click("#cmpGo")
            pg.wait_for_selector("#cmpPage:not([hidden])")
            pg.wait_for_function("document.querySelectorAll('#cmpFigs section.fig').length === 4")
            ncol = pg.locator("#cmpHeadRow th").count()
            check(ncol >= 3 and pg.locator("#cmpRows tr").count() >= 10, f"X4 comparison: metrics side by side ({ncol - 1} runs)")
            check(pg.locator("#cmpFigs section[data-fig=F2] svg path").count() >= 2, "X4 overlaid F2")
            pg.screenshot(path=str(shots / "compare-en.png"), full_page=True)
            pg.goto(base + "/#reports/" + rid_rep); pg.wait_for_selector("#repPage:not([hidden])")
            pg.wait_for_function(f"document.getElementById('repPick').value === '{rid_rep}'")
            pg.fill("#repNotes", "Bilješka: test notes"); pg.click("#repNotesSave")
            pg.wait_for_function("document.getElementById('repNotesState').textContent === 'Saved.'")
            pg.reload(); pg.wait_for_selector("#repPage:not([hidden])"); pg.wait_for_function("document.getElementById('repNotes').value.length > 0")
            check(pg.input_value("#repNotes") == "Bilješka: test notes", "X5 notes saved and kept")
            pg.click("#repRegen")
            pg.wait_for_function("document.getElementById('repNotesState').textContent === 'Report regenerated.'", timeout=60000)
            check(True, "X6 regenerate")
            with pg.expect_download() as dl:
                pg.click("#repCsv")
            check("metric,value" in Path(dl.value.path()).read_text(), "report CSV download")
            pg.screenshot(path=str(shots / "report-en.png"), full_page=True)
            # P5: DUT logs tab (L1-L6), opened from the report
            check(pg.locator("#repFigs section[data-fig=F9]").count() == 1, "F9 present")
            pg.click("#repDutLog"); pg.wait_for_selector("[data-view=logs]:not([hidden])")
            pg.wait_for_function(f"document.getElementById('dlRun').value === '{rid_rep}' && document.querySelectorAll('#dlFiles tr').length >= 1")
            check("No DUT log" in pg.inner_text("#dlFiles"), "L2 a run without a DUT log (custom DUT) says so")
            log = Path(work) / "extra-syslog.log"
            t0 = time.gmtime(int(pg.evaluate("Date.now()") / 1000) - 30)
            log.write_text("".join(time.strftime("*%b %d %H:%M:%S.000", t0) + l + "\n" for l in (
                ": %BGP-5-ADJCHANGE: neighbor 10.100.0.20 Up", ": %SYS-2-MALLOCFAIL: Memory allocation of 65536 bytes failed",
                ": %LINK-3-UPDOWN: Interface Gi0/0/1, changed state to up")))
            pg.set_input_files("#dlUpload", str(log))
            pg.wait_for_function("document.getElementById('dlFiles').textContent.includes('extra-syslog.log')", timeout=60000)
            check("Cisco" in pg.inner_text("#dlMeta") or "cisco" in pg.inner_text("#dlMeta"), "L1 upload evaluated, L3 platform detected")
            pg.wait_for_function("document.querySelectorAll('#dlTiles .kpi').length === 4")
            check(pg.locator("#dlEvBody tr").count() == 2, f"L5 parsed events shown ({pg.inner_text('#dlCount')})")
            pg.select_option("#dlOffMode", "manual"); pg.fill("#dlOff", "3600"); pg.click("#dlApply")
            pg.wait_for_function("document.getElementById('dlOffUsed').textContent.includes('entered by hand')", timeout=60000)
            check(pg.input_value("#dlOff") == "3600", "L4 manual offset applied")
            pg.select_option("#dlOffMode", "auto"); pg.click("#dlApply")
            pg.wait_for_function("!document.getElementById('dlOffUsed').textContent.includes('entered by hand') && !document.getElementById('dlApply').disabled", timeout=60000)
            pg.click("#dlTabUn"); check(pg.locator("#dlEvHead th").count() == 2, "L5 unclassified tab")
            pg.click("#dlTabEv"); pg.select_option("#dlFSev", "error")
            sev = pg.locator("#dlEvBody tr td:nth-child(5)").all_inner_texts()
            check(sev == ["error"], f"L5 severity filter {sev}")
            pg.select_option("#dlFSev", "")
            pg.screenshot(path=str(shots / "dutlogs-en.png"), full_page=True)
            pg.once("dialog", lambda d: d.accept())
            pg.locator("#dlFiles tr", has_text="extra-syslog.log").locator("button").click()
            pg.wait_for_function("document.getElementById('dlFiles').textContent.includes('No DUT log')", timeout=60000)
            check(True, "L1 uploaded file removed")
            pg.click("#tabs a[data-tab=history]")
            pg.wait_for_selector("#hisBody tr")
            pg.locator("#hisBody tr", has_text="ui quick check").locator("button", has_text=re.compile("Re.run")).click()
            pg.wait_for_selector("[data-view=benchmark]:not([hidden])")
            check(pg.input_value("#runName") == "ui quick check" and pg.input_value("#rTotal") == str(ROUTERS) and pg.input_value("#device") == "custom", "H3 re-run loads the configuration")
            check("8 s" in pg.inner_text("#phases"), "re-run keeps the phases")
            pg.click("#tabs a[data-tab=history]"); pg.wait_for_selector("#hisBody tr")
            before = pg.locator("#hisBody tr").count()
            pg.once("dialog", lambda d: d.accept())
            pg.locator("#hisBody tr", has_text="ui quick check").locator("button", has_text="Delete run").click()
            pg.wait_for_function(f"document.querySelectorAll('#hisBody tr').length === {before - 1}")
            check(True, "H5 delete with confirmation")
            pg.click("#tabs a[data-tab=settings]")
            pg.wait_for_function("document.querySelectorAll('#nodesBody tr').length === 6 && document.getElementById('limRouters').value !== ''")
            check(" GB" in pg.inner_text("#nodesBody") and "0.4.2" in pg.inner_text("#nodesBody"), "S1 node status with memory and agent 0.4.2")
            check(pg.locator("#nodesBody button", has_text="Restart agent").count() == 6, "S1 restart buttons")
            pg.fill("#limRouters", "5"); pg.click("#limSave")
            pg.wait_for_function("document.getElementById('limMsg').textContent === 'Saved.'")
            r = httpx.post(base + "/api/runs", json={"scenario": sc.model_dump()}, cookies={"tgen_session": ctx.cookies()[0]["value"]})
            check(r.status_code == 422 and "safety limit" in r.text, "S3 limit applied to the next run")
            pg.click("#limDefaults"); pg.click("#limSave")
            pg.wait_for_function("document.getElementById('limRouters').value === '180'")
            check(True, "S3 back to defaults")
            pg.fill("#pwCur", PASSWORD); pg.fill("#pwNew", "ui-new-password-1"); pg.fill("#pwNew2", "ui-new-password-2"); pg.click("#pwForm button[type=submit]")
            check("differ" in pg.inner_text("#pwMsg"), "S6 new passwords must match")
            pg.fill("#pwNew2", "ui-new-password-1"); pg.click("#pwForm button[type=submit]")
            pg.wait_for_function("document.getElementById('pwMsg').textContent.startsWith('Password changed')")
            pg.fill("#pwCur", "ui-new-password-1"); pg.fill("#pwNew", PASSWORD); pg.fill("#pwNew2", PASSWORD); pg.click("#pwForm button[type=submit]")
            pg.wait_for_function("document.getElementById('pwMsg').textContent.startsWith('Password changed')")
            check(True, "S6 password changed and back")
            pg.screenshot(path=str(shots / "settings-en.png"), full_page=True)
            pg.click("header .langsel .hbtn"); pg.click("header .langsel .menu button[lang=sr-Cyrl]")
            pg.wait_for_function("document.documentElement.lang === 'sr-Cyrl'")
            check("Безбедносна ограничења" in pg.inner_text("[data-view=settings]"), "Serbian settings")
            pg.click("#tabs a[data-tab=reports]"); pg.wait_for_selector("#repPage:not([hidden])")
            pg.wait_for_function("document.querySelectorAll('#repGate tr').length >= 1")
            check("Оцена" in pg.inner_text("#repVh"), "Serbian report page")
            pg.screenshot(path=str(shots / "report-sr.png"), full_page=True)
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
            ph.goto(base + "/#scheme"); ph.wait_for_selector("#topoBox svg"); ph.wait_for_timeout(800)
            w = ph.evaluate("document.documentElement.scrollWidth")
            check(w <= 392, f"scheme page: no horizontal scroll at 390 px (scroll width {w})")
            ph.screenshot(path=str(shots / "scheme-phone-sr.png"), full_page=True)
            for tab in ("history", "reports", "logs", "settings"):
                ph.goto(base + "/#" + tab); ph.wait_for_timeout(1200)
                w = ph.evaluate("document.documentElement.scrollWidth")
                check(w <= 392, f"{tab}: no horizontal scroll at 390 px (scroll width {w})")

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
