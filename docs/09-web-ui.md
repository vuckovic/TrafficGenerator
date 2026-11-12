# 09 — Web interface (P3c)

The browser interface at `https://bgp.elfak.net:8443`. It follows the frozen controls (doc 04 v1.2) and the locked design (`docs/design/`, v1.2), and talks only to the controller API (doc 08).

Status: confirmed 2026‑11‑03. **P3c‑1 built** (2026‑11‑03): login, header, Benchmark tab, live view. **P3c‑2 built** (2026‑11‑12): Test scheme tab with the scheme runner and the network topology (§10). **P3c‑3 built** (2026‑11‑12): History, Reports, Nodes & settings (§11). Browser test `tests/controller/ui_e2e.py` passes.

## 1. How it is built and served

- **Served by the controller**, same origin as the API: `/` is the interface, `/api` and `/ws` the API. One login cookie, no CORS, nothing else to install.
- **No build step.** Plain HTML, CSS and JavaScript modules in `ui/web/`, copied to `/opt/tgen/ui/web` by `services.yml`. Nothing to compile on the nodes; there is no npm on the cluster.
- **Libraries, kept in the repo (no CDN at run time; the lab works offline):**
  - **uPlot** (MIT) for the time charts, as the design says;
  - **IBM Plex Sans and Mono** (OFL) as local font files.
- **Texts** come from `ui/i18n/labels.csv`, served as JSON by `GET /api/i18n` (no login needed, so the login page is translated too). The language choice is kept per browser (G9). Number formats follow the `_lang.*` rows.
- **Screens follow the design artboards one to one**: the same colours, sizes, explanations on hover and keyboard focus, and phone‑width stacking.

## 2. Delivery in three steps

| Step | Contents | Needs in the controller |
|---|---|---|
| **P3c‑1 Benchmark** | Login page; header with tabs, language, run badge, Start / Stop, log out. **Benchmark tab:** Device under test with the **device selector (U0)**, role, DUT IP / ASN; DUT configuration (U8–U18) with Test connection · Preview config · Push now · Remove · Status; emulated routers (count per node or total, prefixes per router, seed, ASN range); scenario presets and phase parameters (C). **Live view:** sessions, convergence, latency (uPlot, cursor, zoom, window), DUT output, **port and link use (M9)**, generator health, control‑plane health (AC6), events. | `GET /api/i18n`; static files; the scenario takes a memory‑guard value (U17); the live tick carries the DUT port and link use; the agent reports node temperature (agent 0.4.1) |
| **P3c‑2 Test scheme** | The whole Test scheme tab (K1–K11): instructions, devices with passwords and connection tests, steps, estimate, progress grid, comparison table, knee chart (three views). | a **scheme runner** in the controller (`POST /api/schemes`, `GET /api/schemes/{id}`, stop). It runs the same steps as `tgen_suite.py` as ordinary runs, one after another, with the knee rule from `tools/tgen/knee.py` |
| **P3c‑3 History and settings** | **History** (H1–H3, H5): list, open, re‑run, delete. **Reports** (X1, X5, X6): the run report page with header, verdict table, key figures and tables from `report.json`; export JSON / CSV. **Nodes & settings**: node status (S1), safety limits (S3), change password (S6). | `POST /api/password`; settings stored in the controller |

## 3. Left for later phases (as already planned)

- **P4 reports:** figures F1–F12, report comparison (X4) and PDF export.
- **P5:** DUT logs tab (L), SNMP polling (U5).
- **P6 data plane:** Traffic tab (T), H4 packet capture, router traffic.
- **Detailed router editing** (R3c–R3e paste and per‑router receive mode, R6–R11 attributes): the tab shows the generated routers; editing them needs planner and agent support. I propose P4, unless you want it earlier.
- **Profiles (G7):** saving whole configurations, in P3c‑3 if time allows.

## 4. Tests

- **Browser end to end** (Playwright, the same loopback setup as `tests/controller/test_e2e.py`):
  - log in;
  - run a quick check from the Benchmark tab and follow the live view;
  - run a two‑step scheme against the Cisco mock;
  - switch the language;
  - check the explanations, keyboard focus and phone width.
- **Screenshots** of every screen compared with the design previews by eye, in both languages.
- **On the cluster:** you run one benchmark and one scheme from the browser.

## 5. Open points

- **Password file for unattended schemes** (the earlier question 2). Until you decide, a scheme asks for each device's password when it starts, and keeps it in memory only.

## 6. P3c‑1 as built (2026‑11‑03)

- **Files:**
  - `ui/web/index.html`, `app.css`;
  - `js/app.js` (screens), `js/chart.js` (latency chart), `js/i18n.js`, `js/api.js`;
  - `vendor/uplot` (uPlot 1.6.31, MIT) and `vendor/fonts` (IBM Plex Sans and Mono, Latin and Cyrillic, OFL).
- **Not yet in P3c‑1:**
  - The BIRD stand‑in entry in the device list. The controller cannot start the stand‑in yet; a BIRD you run yourself works as *Custom* with platform BIRD.
  - Profiles (G7) and SNMP (U5).
  - The routers table shows the planned routers and their live session state; editing them comes later (§3).
- **Browser test**, against the loopback setup:
  - log in (wrong password, then right);
  - device selector and DUT buttons on the Cisco mock;
  - a quick check against BIRD, configured by hand: live sessions, convergence, chart, routers table, health, events, DONE with summary links;
  - Serbian, including after a reload;
  - a Cisco‑mock run with the port card, stopped from the browser;
  - phone width (390 px, no horizontal scroll), first visit in the browser's language;
  - Nodes & settings, and no browser errors.

## 7. Run strip (2026‑11‑12)

Requested by the user after the first cluster test: pressing Start gave no visible sign that a test was running (the header badge alone was too easy to miss). Added as **G10** (doc 04).
- **Behaviour:** see doc 04 G10.
- **Translations:** all texts in English and Serbian Cyrillic (`ui.strip.*`, `ui.starting`). The port line (duplex, up/down) is now translated too.
- **Browser test:** checks the strip in both languages at Start, while running (phase n of m, progress, spinner), finished (with the summary link) and stopped, plus the title marker.
- **Previews:** `docs/design/previews/run-strip-en.png`, `run-strip-sr-cyrl.png`.
- **Commits:** 0476bde (strip), and this change (Serbian checks, translated port line).

## 8. KPI tiles hold six‑digit values (2026‑11‑12)

Reported by the user: a five‑digit latency made the *Latency p99 (now)* value wrap to a second line, which pushed the chart and everything below it down and back up.
- **Fix:** every KPI tile is at least 190 px wide, enough for "999 999 ms" on one line (10 monospace characters at 24 px), and the value never wraps. The small line under the value always reserves two lines. The tiles sit two by two in the live column on a 1440 px screen, or four in a row when there is room.
- **Browser test:** puts "999 999 ms" into the tiles at 1440, 1280, 1024 and 390 px and checks that each stays on one line and the chart does not move.

## 9. P3c‑2 Test scheme — plan (2026‑11‑12)

The confirmed controls K1–K11 (doc 04 v1.2), plus **K12 Test network topology** (proposed 2026‑11‑12, at the user's request; design on the canvas, *Test scheme* artboards, English and Serbian).

- **Scheme runner (controller):**
  - `POST /api/schemes` `{devices: [{profile, password?, enable?}], steps: [test, smoke, series, scale], pause_s, stop_on_failure}` → `202 {id}`;
  - `GET /api/schemes/{id}`: progress grid, comparison rows and knee‑chart series;
  - `POST /api/schemes/{id}/stop`; `GET /api/schemes` (history).
  - It runs the same steps as `tools/tgen_suite.py` as ordinary runs (each one appears in History), one at a time, with the device sizes and MRAI variants from `deploy/duts.toml` and the knee rule from `tools/tgen/knee.py`.
  - Passwords are kept in memory for the scheme only.
- **Topology (K12):** `GET /api/topology` (no secrets): nodes with management and test addresses and roles, devices with BGP and SSH addresses, model, AS and port, gateway, public address, emulated‑router range. The page draws it as one SVG and highlights the device in the running step.
- **Open point:** the physical wiring between the Super6C board port ETH0 and the three devices (one lab switch? which ports?) is drawn as "Lab switch" until confirmed.

K12 approved by the user on 2026‑11‑12 ("this is it"); doc 04 is now v1.3.

## 10. P3c‑2 as built (2026‑11‑12)

- **Controller** (`controller/tgen_controller/scheme.py`, `app.py`, `store.py`):
  - `SchemeRunner` runs, for each chosen device in turn: connection test → smoke → rate series (one per MRAI variant in `deploy/duts.toml`, rates from the file, stops after the first rate past the knee, `tools/tgen/knee.py`) → scale ladder (stops at the first step that does not pass; timeouts from the measured delivery rate, as `tgen_suite.py`). Each step is an ordinary run (`establish → announce at the rate → withdraw`), so it appears in History with its own report.
  - A failed connection test or smoke skips the rest of that device when *Stop on failure* is on; the scheme goes on with the next device.
  - While a scheme runs, single runs and Push / Remove are refused (409). Stop scheme stops the running test within the usual 1.5 s and skips the rest. A controller restart marks an unfinished scheme failed.
  - Stored in SQLite (`schemes` table, one JSON document per scheme): plan, progress grid, results per device, comparison rows (K10), knee‑chart series (K11). **Passwords are not stored**: they are kept in memory for the scheme only, or come from `TGEN_DUT_PASSWORD_<PROFILE>` on the controller.
  - Progress is published on `/ws/live` as `{type: "scheme", scheme: {...}}` after every change.
- **API:**
  - `POST /api/schemes/plan {devices, steps, pause_s}`: sizes from the file and the estimate (K3, K7);
  - `POST /api/schemes {devices: [{profile, password?, enable?}], steps, pause_s, stop_on_failure}` → `202 {id}`;
  - `GET /api/schemes` (list), `GET /api/schemes/{id}`, `POST /api/schemes/{id}/stop`;
  - `GET /api/topology`: nodes (management and test address, controller, agent port), devices (BGP and SSH address, platform, AS, port, link speed), both subnets, gateway, public address, first emulated‑router address. No secrets.
  - `/api/health` also shows the active scheme.
- **Web page** (`ui/web/js/scheme.js`, view in `index.html`):
  - K1 instructions (can be hidden); K12 topology as one SVG built from `/api/topology`, the device under test highlighted (*testing now*, *waiting*, *done*), export SVG / PNG; K2 devices with password field and Test button; sizes and MRAI variants from the file; K3 steps, K5 pause, K6 stop on failure, K7 estimate (remaining and total, per device); K8 Start / Stop scheme; K9 progress grid with a link to the live view or to each run's summary; K10 comparison with CSV and Markdown export; K11 knee chart in three views (delivered vs offered log–log, time after last send with the limits, sustained bars), cursor and slider with readings, export SVG / PNG.
  - The run strip (G10) shows *Test scheme <id>: <device> · <step>* above the current run, and *Test scheme finished / stopped / failed* with a link to the results. The header Stop button stops the scheme.
  - All texts in English and Serbian Cyrillic (`scheme.*`, `ui.strip.scheme*`). Step details come from the controller as data and are translated on the page.
- **Settings** (optional, `controller.toml`): `public_url`, `gateway`, `board_switch`, `lab_switch` (once set, the "wiring not confirmed" note disappears).
- **Tests:**
  - `tests/controller/test_e2e.py` run 4: a full scheme on the Cisco mock (test, smoke, 2‑rate series, 2‑step scale = 5 runs), 409 for a second scheme and for a single run, progress, comparison, knee series, no password stored, live messages; then a scheme stopped during its smoke.
  - `tests/controller/ui_e2e.py`: the scheme page in the browser (topology, instructions, password check, Test, estimate, start, strip, highlight, progress, comparison, three chart views, CSV and SVG export, Serbian, phone width).
  - `tests/controller/test_units.py`: comparison and knee tables, scheme store, restart.
  - Results 2026‑11‑12: test_e2e all pass, ui_e2e 66 checks pass, units 13 pass. Screenshots: `docs/web-ui/scheme-*.png`.
- **Jira / Confluence:** DOC‑49 (done), DOC‑50 (execute the cluster test plan); Confluence page *TrafficGenerator — Test plan: Web interface P3c‑2 (Test scheme tab)*, 34 tests.

## 11. P3c‑3 as built (2026‑11‑12)

Controls H1–H3, H5 (History), X1, X5, X6 (Reports) and S1, S3, S6 (Nodes & settings), as planned in §2. Figures F1–F12, report comparison (X4) and PDF stay in P4; capture (H4), sink node (S2), IP pool (S4), workers (S5) and the control‑plane reservation form (S7) stay with their phases.

- **History** (`ui/web/js/history.js`): every run (up to 500), newest first: time and run id, name, run type (single run or *Test scheme s‑…*, linked), device, routers × prefixes, duration, key metric (initial convergence and p99), verdict chip (PASS, FAIL, running, stopped, failed). Every column sorts; search by name, run id or device; filters for run type and verdict. Actions: **Open report**, **Re‑run** (loads device, DUT settings, routers and phases into the Benchmark tab; nothing starts until Start), **Delete run** (asks first; refused while the run is active).
- **Reports** (`ui/web/js/report.js`), `#reports/<run id>`:
  - header: run id, date, duration, state, scheme it belongs to; title *device · name · routers × prefixes*; DUT role, nodes, controller and agent versions; a picker for other runs;
  - **Verdict** table from the analysis gate (PASS / FAIL / INFO with the measured result);
  - **Key figures** (sessions and setup time, initial convergence, after the last send, withdraw convergence, DUT output and prefixes per UPDATE, link use, missing / extra routes, resets, controller health), **latency** percentiles for announce and withdraw, **generator nodes** (routers, CPU p95, memory);
  - **latency and offered load over the run** from the samples stored during the run (uPlot, elapsed time, All / 5 min / 1 min, cursor and window sliders);
  - phases with start, duration and parameters; device under test; events; links to every run file;
  - **Notes** (X5), saved with the run and included in the CSV export; **Regenerate** (X6) runs the analysis again on the run folder; **Export CSV** (`/api/runs/{id}/report.csv`, metric,value), **Export JSON**, **summary.txt**.
  - The finished‑run strip now links to the report.
- **Nodes & settings** (`ui/web/js/settings.js`):
  - **Node status** (S1): reachable, agent version and state, link speed / duplex of the traffic interface, EEE (from `ethtool`, — when not available; ⚠ when on), CPU %, cores, RAM free / total, SoC temperature, clock offset; **Restart agent** per node (asks first; refused during a run or scheme). Needs **agent 0.4.2** (`host` block in `/v1/status`, `POST /v1/restart`, doc 07).
  - **Safety limits** (S3): max routers (180), max prefixes in total (2 000 000), max duration (from `controller.toml`, 3600 s), max data rate (1000 Mb/s, applies from P6), DUT subnet allow‑list. Saved in the controller's database; checked before every run and scheme step (`422` with the reason).
  - **Change password** (S6): current password, new one twice (at least 10 characters). The new hash is kept in the database (the controller cannot write `/etc`); `sudo -u tgen tgen-controller set-password` on CMNODE01 sets a new one in `controller.toml` and clears the one from the page.
- **All texts** in English and Serbian Cyrillic (`history.*`, `report.*`, `settings.*`).
- **Tests:** `test_e2e.py` (limits, notes, CSV, regenerate, password change and login, node facts, agent restart exits with 75), `ui_e2e.py` (history list, filter, search, sort, report page with every section, notes kept after reload, regenerate, CSV, re‑run, delete, node status, limits applied, password change, Serbian, phone width for every tab). All pass. Screenshots: `docs/web-ui/history-en.png`, `report-en.png`, `report-sr.png`, `settings-en.png`.
- **Update on CMNODE01:** rebuild the agent (`cmake --build agent/build -j4`, 0.4.2) before running the playbook; install `ethtool` on the nodes for the EEE column.
