# 09 — Web interface (P3c)

The browser interface at `https://bgp.elfak.net:8443`. It follows the frozen controls (doc 04 v1.2) and the locked design (`docs/design/`, v1.2), and talks only to the controller API (doc 08).

Status: confirmed 2026‑11‑03. **P3c‑1 built** (2026‑11‑03): login, header, Benchmark tab, live view; browser test `tests/controller/ui_e2e.py` passes. P3c‑2 and P3c‑3 to follow.

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
