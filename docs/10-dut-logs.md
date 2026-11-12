# 10 — DUT logs and polling (P5) — plan

Spec §6.2–6.3 and §11, controls L1–L6 and U5 (doc 04), figure F9 (doc 09 §12). Status: **plan, 2026‑11‑12**, waiting for sample logs (Q2) and two decisions.

## 1. What already exists

- At the end of every run the controller saves the device's BGP log as `dut-bgp.log`:
  - Cisco: `show logging | include BGP`;
  - MikroTik: the run's BGP lines from the memory log (BGP logging switched on for the run);
  - BIRD / FRR: their log files.
- During the run the controller polls the device over SSH every 10 s (`dut-sessions.jsonl`: established sessions, free memory, CPU) — already drawn as F10.

## 2. What P5 adds

1. **Log capture, wider:** for Cisco, the whole `show logging` for the run window (not only `BGP` lines), so resource messages such as `%SYS-2-MALLOCFAIL` and `CPUHOG` are kept. Device clock and time zone (`show clock`, `/system clock print`) saved with the log.
2. **Parsers** (`tools/tgen/dutlog.py`): Cisco IOS / IOS‑XE, MikroTik RouterOS 7, BIRD 2/3, FRR/Quagga. Each line becomes `{time, neighbour, event, detail, severity}`; events: session up / down, notification sent / received (with code), hold‑timer expiry, max‑prefix, memory / CPU warning, error. Unknown lines are kept as *unclassified*. Platform auto‑detected (L3).
3. **Time alignment (L4):** the DUT clock offset is found by matching the DUT's "neighbour Up" lines to our Established times (median difference), or entered by hand; the offset used is shown in the report.
4. **Automatic evaluation (L6)** at the end of every run, and again on Regenerate:
   - event counts by type and by phase;
   - unexpected session downs with their cause (the notification text);
   - the first error under load ("first MAXPFX at 2 400 updates/s");
   - agreement: DUT‑side session events vs. what our routers saw.
   - Written to `dut-events.json`; the report gets a *DUT log* section and an INFO row in the verdict, and F9 gets its **DUT log lane**.
5. **DUT logs tab (L1–L6):** upload more log files for a run (e.g. from a syslog server; .txt/.log/.gz), choose the run, platform and time offset, the parsed‑events table with filters and an *unclassified* tab, the summary. Upload re‑runs the evaluation and the report figures.
6. **SNMP (U5)** — see decision 2.

## 3. Needed from Dule

- **Sample logs (Q2):** the run folders of tonight's runs A, B, C and one MikroTik run (they contain `dut-bgp.log`, our session times and the DUT polls), plus each device's clock settings.
- **Decision 1 — DUT logs tab layout:** there is no design artboard for it. Proposal: the same style as the report page — run picker and upload at the top, summary tiles, then the events table with filters and an *unclassified* tab.
- **Decision 2 — SNMP:** the SSH polling already gives CPU, memory and session counts (F10). SNMP would add interface counters and BGP4‑MIB per‑peer prefix counts, but needs SNMP switched on in the devices and adds `pysnmp`. Options: (a) SNMP v2c now, read‑only community, as an option per device; (b) leave SNMP for later and keep SSH polling only.
