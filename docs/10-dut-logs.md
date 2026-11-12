# 10 — DUT logs and SNMP polling (P5)

Spec §6.2–6.3 and §11, controls L1–L6 and U5 (doc 04), figures F9 and F10 (doc 09 §12).
Status: **built, 2026‑11‑13** — parsers checked on synthetic lines and one real RouterOS sample;
the cluster samples (Q2: runs A, B, C and a MikroTik run) are still to be checked.

Decisions (Dule, 2026‑11‑12): the DUT logs tab looks like the report page; SNMP v2c now (v3 later).

## 1. Log capture (end of every run)

| Platform | What is saved as `dut-bgp.log` |
|---|---|
| Cisco IOS / IOS‑XE | `# device clock at collection: <show clock>`, then `show logging \| include BGP\|SYS-\|TCP-\|LINK-\|LINEPROTO-` (resource messages such as `%SYS-2-MALLOCFAIL`, `CPUHOG` are kept) |
| MikroTik RouterOS 7 | `/system clock print` as a `#` line, then the run's BGP lines from the memory log |
| BIRD / FRR | their log file |

More files can be uploaded per run (L1): syslog‑server extracts, `.txt`, `.log` or `.gz`, ≤ 50 MB each,
saved in the run folder under `dutlogs/`.

The agent (0.4.2) records its own end‑of‑run Cease as a **planned** down (`down_planned`, "end of run (Cease 6/2)"),
so the DUT's matching "neighbour Down" is not counted as unexpected.

## 2. Parsers — `tools/tgen/dutlog.py`

- **Time stamps:** Cisco (`*Nov 12 21:00:01.100:`, with optional sequence number, year and zone), ISO and
  journalctl (`2026-11-12T20:00:01+0100`), FRR (`2026/11/12 20:00:01`), RouterOS 6 (`nov/12/2026 20:00:01`),
  RouterOS 7 (`20:00:01`, the run's date). Lines with only an uptime stamp (`1w2d:`) keep no time.
- **Platform (L3):** auto‑detected from the text (or the run's adapter), or chosen by hand.
- **Each line →** `[time, neighbour, router, event, detail, severity, phase, file, line]`.
  Events: `up`, `down`, `notification_sent`, `notification_received`, `hold_expired`, `max_prefix_warning`,
  `max_prefix_exceeded`, `resource`, `error`, `config`, `other`. Unknown lines are kept as *unclassified*.
- **Neighbour → our router:** by address, BIRD protocol name (`n_10_100_0_20`, `tgen_R01`) or RouterOS
  session name (`tgen-R01-1`).
- **RouterOS 7.11:** at topics `bgp,debug,state` the only session line is "Entering OpenConfirm state … ConnId{ dut:179 -> peer:port }";
  it is read as *session up* for the peer after `->` (real sample `tests/fixtures/dut-logs/mikrotik-7.11.2-crs106.log`, all 12 sessions found).

## 3. Clock alignment (L4)

- **Auto:** the differences between DUT session lines and our own session times (ups against our Established
  times; downs and notifications against our downs) are put in 2 s bins; the bin shared by the most routers wins,
  then the median inside it. When several runs are in one log (BIRD's file, a syslog server) the bins tie;
  then a whole time‑zone step (15 min) and more pairs win.
- **Zone fallback:** no session lines to pair (e.g. only resource messages): the offset is the time‑zone step
  nearest the log's middle — flagged in the page and the verdict row.
- **Manual:** seconds, "DUT clock minus ours" (e.g. 3600 for CET against UTC).
- Only lines from 60 s before the start to 180 s after the end of the run are kept.

## 4. Evaluation (L6) — `dut-events.json`

At the end of every run, on Regenerate, after an upload, a removal or a settings change:

- counts by event and by phase;
- **unexpected session downs** with the cause (the notification or hold‑timer line within 5 s); a down within 10 s
  of one of our planned downs (flap) or after the teardown starts is expected;
- **first error under load** (hold timer, max‑prefix, resource, error) with the offered rate at that second;
- **agreement:** per router, session ups and downs in the DUT log against ours;
- an INFO row in the verdict ("DUT log: 96 events, 0 unexpected session downs; clock offset +3600 s (auto, 6 routers)");
- the F9 **DUT log lane** (warnings and errors always, information lines thinned to 600).

## 5. DUT logs tab (L1–L6)

Opened from the menu or from a report (*DUT log* button, `#logs/<run>`), same style as the report page:

- run picker, link back to the report;
- **Log files:** collected and uploaded files with platform, lines and events; upload; remove uploads;
- **Platform** (auto / Cisco / MikroTik / BIRD / FRR) and **time offset** (auto / manual seconds), *Apply and evaluate*;
  the offset actually used is shown;
- **Log summary:** tiles (parsed events, unexpected downs, errors and limits, unclassified lines), first error,
  events by phase, unexpected downs with cause, agreement;
- **Events** table with search and filters (event, severity, phase) and an **Unclassified lines** tab.

API: `GET /api/runs/{id}/dutlog`, `POST /api/runs/{id}/dutlog/files?name=` (raw body),
`DELETE /api/runs/{id}/dutlog/files/{name}`, `PUT /api/runs/{id}/dutlog/settings` `{platform, offset}`.

## 6. SNMP v2c polling (U5) — `tools/tgen/snmp.py`

- **Control (Benchmark tab, Device under test):** *SNMP polling (v2c, read‑only)* checkbox, community
  (password field), SNMP address (`host` or `host:port`, default the DUT IP and 161), *Test SNMP*
  (system name and description, CPU, free memory, port, our BGP peers Established). Profiles may set
  `snmp = true`, `snmp_host`, `snmp_port` in `duts.toml`.
- **Community = credential:** typed in the page (kept in memory for the run only, never in the scenario,
  database or run folder) or `TGEN_DUT_SNMP_<PROFILE>` in `/etc/tgen/dut-passwords.env` (unattended schemes).
  The inventory refuses community or password keys. The command line never carries it: net‑snmp reads it
  from a temporary `snmp.conf` (0600, removed after each call).
- **Tools:** net‑snmp `snmpget` / `snmpbulkwalk` (Debian package `snmp`, installed by `services.yml`).
- **Every polling interval** (U4, same as the SSH polls), in its own task, results in `dut-snmp.jsonl`:
  - CPU: CISCO‑PROCESS‑MIB `cpmCPUTotal5secRev`, else HOST‑RESOURCES `hrProcessorLoad` (average);
  - memory: CISCO‑MEMORY‑POOL‑MIB (processor pool), else UCD `memAvailReal/memTotalReal`, else `hrStorage` RAM;
  - the port towards the generator (profile `interface`, else the busiest): IF‑MIB 64‑bit octets as Mb/s, errors, discards;
  - BGP4‑MIB peer state and received updates (as updates/s) for **our peers only**, CISCO‑BGP4‑MIB accepted prefixes.
- A failing agent before the run only skips the polling (warning event); failures during the run are reported
  once, and again when it answers.
- **Report:** a line in the DUT table (polls answered, CPU max, free memory min, port in max, updates/s max);
  **F10** uses SNMP when the SSH polls give no CPU/memory; new **F10b** — DUT port in/out and BGP updates/s received.
- Not yet: SNMP v3, and a live M5 sparkline in the Benchmark tab.

## 7. Tests

- `tests/controller/test_dutlog.py`: time stamps, Cisco / FRR / BIRD / RouterOS parsers, the real RouterOS sample,
  alignment, evaluation (auto, manual, `.gz` upload, zone fallback, no log).
- `tests/controller/test_snmp.py`: against a local snmpd with fake BGP MIBs (`tests/bgp/sim/snmp/`), wrong community,
  community never on the command line.
- `test_e2e.py`: BIRD log parsed and aligned, upload / settings / removal; SNMP polls during a run, report summary,
  F10/F10b data, community not stored, Test endpoint.
- `ui_e2e.py`: DUT logs tab (L1–L5), U5 Test SNMP, F10b and the report line, phone width.

## 8. Still needed

- Cluster samples (runs A, B, C, a MikroTik run) and the devices' clock settings, to check the parsers on real logs.
- SNMP switched on in the devices (read‑only community, ACL for CMNODE01) to check CISCO‑PROCESS‑MIB, CISCO‑BGP4‑MIB
  on the ASR‑920 and 1841 and what RouterOS 7 exposes.
