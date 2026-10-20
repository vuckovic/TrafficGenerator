# UI design — LOCKED v1 (2026‑10‑19) · v1.1 additions proposed (2026‑10‑20)

Approved reference for the web interface look and behaviour. The P3 implementation follows it.
Changes go through a new design version, not ad hoc.

- **Canvas (interactive preview):** https://claude.ai/artifact/9TCXNVgEh2PCtjfFLkAb8U — private; share it from the canvas's Share menu if others need it.
- **Sources (snapshot):** `Main.dc.html` (Benchmark tab, live run), `Main.sr.dc.html` (the same screen in Serbian Cyrillic), `Report.dc.html` (run report), `canvas.json`.

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

## v1.1 additions (proposed 2026‑10‑20, awaiting confirmation)
| Area | Change |
|---|---|
| Language | Flag selector in the top‑right corner of every page (G9): 🇬🇧 EN / 🇷🇸 СР. It opens a menu with flag and native name; the choice is kept per browser. Texts come from `ui/i18n/labels.csv`. In the preview, choosing Српски opens the Serbian artboard. |
| DUT configuration | New card on the Benchmark tab under *Device under test* (U8–U16): platform segmented selector (MikroTik RouterOS 7 · Cisco IOS / IOS‑XE · BIRD 2/3 · Quagga/FRR) with a platform note; SSH address / port / user / authentication; Cisco enable secret; sudo and BIRD include file for Linux DUTs; route limit; keep or remove neighbours after the run; Test connection · Preview config · Push now · Remove · Status; connection status line; collapsible config preview. |
| Device under test card | *Platform / version* dropdown replaced by *Detected platform* (read from the device; the platform is chosen in the new card). |
| Header (all screens) | One row in every language from 1440 px up:
- host name under the product name;
- tabs with 8 px side padding;
- Log out as an icon button (user name in its tooltip) — **confirmed by the user 2026‑10‑20**;
- language button = flag + caret;
- run controls never wrap internally; on narrower windows they move right‑aligned to a second row.

Measured with IBM Plex at 1440 px: English 64 px high, Serbian 64 px; at 1280 px only Serbian wraps. |
| Prefix pool sample | `10.0.0.0/8 · /24–/28` (spec §18 change of 2026‑10‑19). |
