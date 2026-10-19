# UI design — LOCKED v1 (2026‑10‑19)

Approved reference for the web interface look and behaviour. The P3 implementation follows it.
Changes go through a new design version, not ad hoc.

- **Canvas (interactive preview):** https://claude.ai/artifact/9TCXNVgEh2PCtjfFLkAb8U — private; share it from the canvas's Share menu if others need it.
- **Sources (snapshot of the locked version):** `Main.dc.html` (Benchmark tab, live run), `Report.dc.html` (run report), `canvas.json`.

## Locked decisions
| Area | Decision |
|---|---|
| Look | dark top bar (#16191D), light panels (#F3F4F1 / #FFFFFF), teal accent #0E6E66, orange #C2410C for p99/warnings, violet #5B4FC4 for cursors; IBM Plex Sans + IBM Plex Mono for numbers |
| Layout | tabs: Benchmark · Traffic · Reports · DUT logs · History · Nodes & settings; Benchmark = configuration left, live view right; stacks at phone width |
| Run control | status badge (state · phase · elapsed), Start, always‑visible red Stop |
| DUT role | segmented selector + explanation label under it |
| Routers | table rows with ASN + Random, prefix count, Generate / Paste…, receive mode, session state |
| Scenario | phase cards (done / running / next) + parameters |
| Metrics | every metric has a hover/focus explanation (dotted underline) |
| Charts | interactive: value cursor (mouse + slider), readout line, zoom + window slider on time charts; values drawn as text over the chart (real UI: uPlot) |
| Report | header, verdict table (PASS/FAIL/INFO), figures F1–F12, comparison, export PDF/CSV/JSON |

Sample values in the preview are illustrative, not measured.
