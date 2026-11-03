# UI design — LOCKED v1.2 (2026‑11‑03)

Approved reference for the web interface look and behaviour. The P3 implementation follows it.
Changes go through a new design version, not ad hoc.

- **Canvas (interactive preview):** https://claude.ai/artifact/9TCXNVgEh2PCtjfFLkAb8U — private; share it from the canvas's Share menu if others need it.
- **Sources (snapshot):** `Main.dc.html` (Benchmark tab, live run), `Main.sr.dc.html` (the same screen in Serbian Cyrillic), `TestScheme.dc.html` / `TestScheme.sr.dc.html` (Test scheme tab, v1.2), `Report.dc.html` (run report), `canvas.json`.

## Locked decisions
| Area | Decision |
|---|---|
| Look | dark top bar (#16191D), light panels (#F3F4F1 / #FFFFFF), teal accent #0E6E66, orange #C2410C for p99/warnings, violet #5B4FC4 for cursors; IBM Plex Sans + IBM Plex Mono for numbers |
| Layout | tabs: Benchmark · Test scheme (v1.2) · Traffic · Reports · DUT logs · History · Nodes & settings; Benchmark = configuration left, live view right; stacks at phone width |
| Run control | status badge (state · phase · elapsed), Start, always‑visible red Stop |
| DUT role | segmented selector + explanation label under it |
| Routers | table rows with ASN + Random, prefix count, Generate / Paste…, receive mode, session state |
| Scenario | phase cards (done / running / next) + parameters |
| Metrics | every metric has a hover/focus explanation (dotted underline) |
| Charts | interactive: value cursor (mouse + slider), readout line, zoom + window slider on time charts; values drawn as text over the chart (real UI: uPlot) |
| Report | header, verdict table (PASS/FAIL/INFO), figures F1–F12, comparison, export PDF/CSV/JSON |

Sample values in the preview are illustrative, not measured.

## v1.1 additions (locked 2026‑10‑20, all confirmed by the user)
| Area | Change |
|---|---|
| Language | Flag selector in the top‑right corner of every page (G9): 🇬🇧 EN / 🇷🇸 СР. It opens a menu with flag and native name; the choice is kept per browser. Texts come from `ui/i18n/labels.csv`. In the preview, choosing Српски opens the Serbian artboard. |
| DUT configuration | New card on the Benchmark tab under *Device under test* (U8–U16): platform segmented selector (MikroTik RouterOS 7 · Cisco IOS / IOS‑XE · BIRD 2/3 · Quagga/FRR) with a platform note; SSH address / port / user / authentication; Cisco enable secret; sudo and BIRD include file for Linux DUTs; route limit; keep or remove neighbours after the run; Test connection · Preview config · Push now · Remove · Status; connection status line; collapsible config preview. |
| Device under test card | *Platform / version* dropdown replaced by *Detected platform* (read from the device; the platform is chosen in the new card). |
| Header (all screens) | One row in every language from 1440 px up:
- host name under the product name;
- tabs with 8 px side padding;
- Log out as an icon button (user name in its tooltip) — **confirmed by the user 2026‑10‑20**;
- language button = flag + caret (Serbian: state flag with coat of arms, `ui/i18n/flags/rs.svg`; in the canvas it is the uploaded asset `/_blob/5daebb85…`);
- run controls never wrap internally; on narrower windows they move right‑aligned to a second row.

Measured with IBM Plex at 1440 px: English 64 px high, Serbian 64 px; at 1280 px only Serbian wraps. |
| Prefix pool sample | `10.0.0.0/8 · /24–/28` (spec §18 change of 2026‑10‑19). |

## v1.2 additions (locked 2026‑11‑03, approved by the user; controls frozen in docs/04 v1.2, DOC‑40)
| Area | Change |
|---|---|
| Device selector (U0) | First field of *Device under test*: one entry per device in `deploy/duts.toml`, plus *BIRD stand‑in* and *Custom*. Choosing a device fills role, DUT IP/ASN, platform, SSH address, user, route limit, memory guard and MRAI, and the status line. Shows whether a password for the device is set on the controller. Note under it: the list is edited only in the file. |
| Memory guard (U17), MRAI (U18) | Two fields in *DUT configuration*. MRAI is a dropdown (device default · 0 s · custom), disabled with the reason on non‑Cisco platforms. |
| Port and link use (M9) | Live‑view card: port · speed · duplex · state, link use % with 70/90 % marks and a 5‑minute sparkline, new port errors. Hidden when the platform cannot report its port (MikroTik). |
| Test scheme tab (K1–K11) | New screen: **How this test works** panel (collapsible, open on first visit), Devices (checkbox, password, Test connection, status) with the sizes from the file, Steps with pause / stop on failure / estimate / Start · Stop, Progress grid (device × step, chips Passed · Running · Waiting · n/a · Failed, link to the live view), Comparison table (explanations on every column), Knee chart with three views (delivered vs offered log–log · time to finish after the last send · sustained rate), value cursor + slider and readout. Device colours fixed: MikroTik blue #2A6FDB, ASR‑920 orange #D9661F, 1841 aqua #0F8C9C; dashed = device MRAI, solid = MRAI 0, ringed points past the knee. |
| Charts | Axis and value labels are HTML over the SVG (as on the Benchmark chart), so they render in every language. |
| Texts | All new texts are in `ui/i18n/labels.csv` (English and Serbian). |

## Preview images (`previews/`)
Rendered at 1440 px for documentation and Jira: v1.2 screens on 2026-11-03, the others from v1.1 on 2026-10-27. Sample values are illustrative, not measured.

| File | Shows |
|---|---|
| `benchmark-en.png` | Benchmark tab, live run (English), v1.2 |
| `benchmark-sr-cyrl.png` | the same screen in Serbian Cyrillic, v1.2 |
| `test-scheme-en.png`, `test-scheme-sr-cyrl.png` | Test scheme tab (v1.2), English and Serbian |
| `report-en.png` | run report |
| `header-en.png`, `header-sr-cyrl.png` | header in both languages (one row at 1440 px) |
| `dut-configuration-en.png` | DUT configuration card (U8–U16) |
