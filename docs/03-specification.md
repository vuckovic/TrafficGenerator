# 03 — TrafficGenerator specification

Status: **v0.3 (2026‑10‑19)** — restructured around **benchmarking a BGP device under test (DUT)**. Decisions D1–D8 (2026‑10‑19) still apply (§15). Open questions Q1–Q6 are in §16. Web interface controls: `04-ui-controls.md`.

## 1. Purpose

The cluster **emulates many BGP routers** and uses them to stress and measure a **device under test (DUT)**: an IXP route server, a route reflector, or a BGP router. The DUT may be a Cisco, MikroTik, BIRD or Quagga/FRR device.

- Each cluster node runs a configurable number of emulated routers. Each router has its own AS number, peer IP and prefixes, and holds a real BGP session with the DUT.
- The routers announce, withdraw and change their prefixes following a scripted **test scenario** (§5).
- The same routers also **receive** what the DUT reflects back. Comparing what was sent with what came back, and when, measures the DUT **from the outside, without vendor access**: session setup, convergence time, propagation latency, update throughput, correctness and stability.
- Optional inputs make the picture complete:
  - **DUT logs** (uploaded after the run) and **DUT polling** (SNMP/SSH during the run);
  - **data‑plane traffic** at up to 1 Gbit/s (TCP/IP mode), for DUTs that also forward packets.
- After the run, a **performance report with graphs** is produced (§12).

The method follows the IETF benchmarking terminology and methodology for BGP convergence: RFC 4098 (control plane) and RFC 7747 (data plane).

### 1.1 What stays from v0.2
- **TCP/IP crafted traffic** at up to 1 Gbit/s (§9), now also usable *towards the prefixes learned in the scenario* (data‑plane convergence).
- **BGP stateless** line‑rate messages (§8), for wire, capture and IDS tests that don't need sessions.
- Cluster safety, distribution, the admin password and the decisions D1–D8.

### 1.2 Acceptance criteria

| ID | Criterion | Measured by |
|---|---|---|
| AC1 | All nodes take part: every node runs its share of emulated routers and/or traffic | run report, per‑node section |
| AC2 | Data plane: **≥ 940 Mbit/s L2** (L1 also reported) of TCP/IP or BGP‑stateless traffic for 10 min, sink loss < 0.1 % | §9 / sink |
| AC3 | Generator CPU: **node average ≤ 70 %** (95th percentile over the run) on every node; ≤ 90 % accepted in the first milestone | agent stats |
| AC4 | Control plane: the scenario's target scale (Q4; default **60 emulated routers, 600 000 announced prefixes**) is established and measured without the *generator* becoming the bottleneck. Generator‑side send lag stays < 10 ms p99, so every latency figure reflects the DUT, not us | agent self‑check |
| AC5 | Measurement accuracy: timestamps from the clock‑synchronised cluster (chrony, offset < 1 ms, T06). Latencies are reported in ms with ±1 ms stated uncertainty | report header |

## 2. Test topology

```
                     ┌────────────────────────────── DUT ──────────────────────────────┐
                     │ route server (eBGP, IXP)  /  route reflector (iBGP)  /  router  │
                     │ Cisco · MikroTik · BIRD · Quagga/FRR                            │
                     └───────────────▲───────────────────────────────▲─────────────────┘
                 BGP sessions + data │ (ETH0, 10.100.0.0/24)          │ optional: SNMP / SSH polling,
                                     │                                │ logs uploaded after the run
 ┌──────────── CMNODE01 ───────────┐ ┌── CMNODE02 ──┐      ┌── CMNODE06 ──┐   ┌── controller (CMNODE01) ──┐
 │ R1 AS65001 · R2 AS65002 · ...    │ │ R11 ... R20  │ ...  │ R51 ... R60  │   │ web UI · scenario engine │
 │ emulated routers (BGP speakers) │ │              │      │              │   │ collectors · reports     │
 │ + data-plane workers (optional) │ │              │      │              │   └───────────────────────────┘
 └─────────────────────────────────┘ └──────────────┘      └──────────────┘
```

Every emulated router gets its own IP on the traffic subnet. The agent adds these addresses for the run and removes them afterwards (default pool 10.100.0.20–10.100.0.250 = 231 addresses, enough for up to 30 routers per node; more needs a larger traffic subnet, Q4).

## 3. Architecture

| Component | Role | Runs on |
|---|---|---|
| **tgen‑agent** (C++20) | **BGP router emulator**: N speakers per node, FSM, own encoder/decoder (D1), send/receive timestamping, receive‑side RIB; data‑plane workers (TCP/IP, BGP stateless); local stats | all 6 nodes |
| **tgen‑controller** (Python/FastAPI) | web UI, scenario engine (phases, timing), planner, DUT collectors (SNMP/SSH), log parsers, analysis, report generation, SQLite history | CMNODE01 |
| **tgen‑cli** | scripted experiments through the same API | CMNODE01 |

Control traffic between controller and agents runs on the management network (REST/JSON, shared token). Phase changes carry an absolute `at` timestamp, so every node switches phase at the same moment (clocks within 1 ms).

## 4. Emulated routers

### 4.1 Definition
Each emulated router has:
- name, **ASN** (2‑ or 4‑byte), router ID and peer IP;
- the node it runs on;
- its **announced prefixes**, either a count plus generation, or a pasted list;
- path attributes: AS_PATH policy (prepend, transit ASN pool, length), ORIGIN, NEXT_HOP, MED, communities (standard and large);
- a **receive mode** (§4.3).

### 4.2 Creating routers (UI controls R1–R11)
- **Routers per node** (e.g. 10): the planner creates `routers × selected nodes` routers. Alternatively, set a total and let the planner distribute it, weighted by RAM when receive mode is *full* (CMNODE01–04 have 16 GB, CMNODE05–06 have 8 GB).
- **ASN:** typed in, drawn at random (unused, from a preset range: private 16‑bit, private 32‑bit, documentation or custom; never the DUT's ASN), sequential, or pasted as a list. In **iBGP / route‑reflector** mode all routers share the DUT's AS, and the chosen ASNs become *origin* ASNs in the AS_PATH (Q1).
- **Prefixes:** a count per router, then **[Generate]** (random from a pool and prefix‑length range, unique across routers or per router, reproducible from the run seed) or **[Paste]** / upload (validated, de‑duplicated). Generation runs on the controller, so million‑prefix tables don't load the browser. Router sets can be exported and imported (JSON/CSV). MRT import in P6 (D4).

### 4.3 Receive side
The DUT sends routes back to every emulated router. Each router's **receive mode** decides how much of that it keeps:

| Mode | Keeps | Use |
|---|---|---|
| **full** | complete Adj‑RIB‑In: prefix, attributes, arrival time | correctness checks, latency for every prefix |
| **timestamps** | prefix and arrival time only | latency and convergence at lower memory |
| **count** | counters only | load generators that don't measure |

Memory estimate (≈ 48 B per route in *full*):

| Routers | Prefixes per router | Routes per receiver | Full receivers per node | Memory per node |
|---|---|---|---|---|
| 60 | 10 000 | 590 000 | 10 | 0.26 GiB |
| 60 | 100 000 | 5.9 M | 10 | 2.6 GiB |
| 60 | 100 000 | 5.9 M | 1 | 0.26 GiB |
| 30 | 1 000 000 | 29 M | 1 | 1.3 GiB |

The default is: one *full* router per node, all others *timestamps*. The planner refuses a configuration that would exceed 70 % of a node's RAM.

## 5. Test scenario (phases)

A run is a sequence of phases on a common timeline. The UI offers presets, and the phases can also be edited.

| # | Phase | What happens | What it measures (RFC 4098 terms where applicable) |
|---|---|---|---|
| 1 | **Establish** | all routers open sessions, staggered by *N per second* or all at once | session setup time; how many sessions the DUT accepts per second |
| 2 | **Initial load** | every router announces its full prefix set at a configured rate (or as fast as the session allows) | **initial convergence time**: until every receiver holds every expected route; DUT outbound update rate |
| 3 | **Steady state** | keepalives only, for T seconds | baseline stability, keepalive timing, DUT CPU/memory at rest |
| 4 | **Churn** | announce/withdraw/attribute changes at a rate, constant or **stepped** (e.g. +100 updates/s every 30 s) | **propagation latency** under load, throughput limit (the "knee"), backlog growth |
| 5 | **Flap / failure** | selected sessions are reset or go silent (hold‑timer expiry); prefixes withdrawn en masse | **withdrawal convergence**, recovery time, route‑flap‑damping behaviour |
| 6 | **Teardown** | withdraw everything, close sessions | full‑withdraw convergence, clean shutdown |

**Presets:**
- *Quick check*: 1–2–3–6, 5 min.
- *Initial convergence*: 1–2–6, repeated 3×.
- *Throughput knee*: 1–2–4 (stepped)–6.
- *Resilience*: 1–2–3–5–3–6.
- *Full IXP day*: all phases, 30 min.

Optional **data‑plane traffic** (§9) can run during any phase towards the announced prefixes. This is only meaningful when the DUT forwards packets (a router, not a pure route server; Q5).

## 6. Performance data points

### 6.1 Measured by the emulated routers (always available, no DUT access needed)

| Group | Data point | Definition |
|---|---|---|
| Sessions | **Session setup time** | TCP SYN → Established, per router; p50/p95/max |
| | Sessions established vs. time | count per second during phase 1 |
| | **Session failures** | NOTIFICATIONs received (code/subcode), TCP resets, hold‑timer expiries; per router and in total |
| | Keepalive regularity | interval and jitter of the DUT's KEEPALIVEs |
| | Negotiated capabilities | 4‑byte AS, MP‑BGP, route refresh, ADD‑PATH, … as accepted by the DUT |
| Convergence | **Initial convergence time** | first announcement sent → last expected route received, per receiver and overall |
| | **Withdrawal convergence time** | mass withdraw sent → last route removed at every receiver |
| | Recovery time | session re‑established → full table re‑learned |
| Latency | **Announce propagation latency** | send time at the origin router → arrival time at each receiver, per prefix; distribution p50/p90/p99/max |
| | **Withdraw propagation latency** | same, for withdrawals |
| | Latency vs. offered load | p50/p99 per second, against the churn rate |
| Throughput | **DUT output rate** | prefixes/s and UPDATEs/s received from the DUT |
| | **Max sustainable update rate (knee)** | highest offered rate at which the backlog stays bounded and p99 latency < threshold (default 1 s) |
| | Backlog | routes sent but not yet seen at the receivers, over time |
| | Update packing | prefixes per UPDATE and gaps between UPDATEs from the DUT (MRAI, batching behaviour) |
| Correctness | **Missing / extra routes** | expected vs. received per receiver, after each phase |
| | **Attribute fidelity** | AS_PATH (route server must not insert its ASN), NEXT_HOP unchanged, communities, MED kept/stripped as configured; route‑reflector attributes ORIGINATOR_ID/CLUSTER_LIST present |
| | Policy behaviour | max‑prefix triggers, filtered prefixes, route‑flap damping |
| Generator health | send lag, CPU, memory | proves the generator was not the bottleneck (AC4) |

### 6.2 From the DUT (optional collectors during the run)

| Source | Data points |
|---|---|
| SNMP v2c/v3 | CPU % (HOST‑RESOURCES‑MIB / CISCO‑PROCESS‑MIB / MikroTik), memory used/free, interface counters, BGP4‑MIB peer states and prefix counts |
| SSH CLI (read‑only account) | BIRD `birdc show protocols / memory`, FRR/Quagga `vtysh show bgp summary`, Cisco `show processes cpu`, `show bgp summary`, MikroTik `/routing/bgp/session print`, `/system/resource print` |
| Polling interval | default 5 s (configurable 1–60 s); every sample stored with a timestamp |

### 6.3 From uploaded DUT logs (after the run, §11)
Session up/down events, NOTIFICATIONs, hold‑timer expiries, max‑prefix events, memory/CPU warnings, and errors, all on the same timeline as our measurements.

## 7. tgen‑agent: BGP router emulator (C++20)

- **One Asio event loop per worker thread.** Routers are spread across 2–3 threads per node, pinned to cores.
- **FSM** per RFC 4271: Idle, Connect, Active, OpenSent, OpenConfirm, Established. Timers: hold, keepalive, connect‑retry; configurable MRAI for our own sending.
- **Encoder/decoder** shared with the stateless mode (D1): OPEN with capabilities (4‑byte AS RFC 6793, MP‑BGP RFC 4760, route refresh), UPDATE (IPv4 NLRI; IPv6 via MP_REACH in a later phase, D3), KEEPALIVE, NOTIFICATION; large communities (RFC 8092). Validated against tshark's BGP dissector.
- **Send path:** UPDATEs are pre‑encoded per router at arm time. A pacer releases them at the scenario rate, and each prefix's send time is recorded when its UPDATE is written to the socket.
- **Receive path:** an incremental decoder; each NLRI or withdrawal gets an arrival timestamp (`CLOCK_REALTIME`, chrony‑synchronised) and goes into the router's receive store (§4.3).
- **Expected‑route index:** the controller tells every agent which prefixes each router *should* receive, so convergence and missing/extra routes are computed locally and only summaries cross the network (per second); full detail is fetched after the run.
- **Latency join:** origin send times and receiver arrival times are joined on (prefix, announcement generation) on the controller after each phase.

## 8. BGP stateless mode (unchanged from v0.2)
Line‑rate TCP/179 segments carrying whole, valid BGP messages, with consistent sequence numbers per pseudo‑session, so captures decode cleanly. For wire, capture, sFlow/IPFIX, IDS and ACL tests; not for DUT control‑plane benchmarking.

## 9. Data plane (TCP/IP mode and data‑plane convergence)
As in v0.2: AF_PACKET TX ring, pre‑built templates, incremental checksums, pacing, up to 1 Gbit/s total across 6 nodes. New: **destinations can be "the prefixes announced in the scenario"**. Traffic from router A's prefixes towards router B's prefixes then crosses the DUT, and a sink node (or the receiving routers' nodes) counts arrivals per prefix. That yields the **data‑plane convergence time and blackhole duration** of RFC 7747 when routes are withdrawn or re‑learned. Shapes: constant, on/off, ramp (D6).

## 10. Controller: scenario engine and collectors
- Validates the configuration, builds the plan (routers per node, IPs, prefixes, expected routes) and arms the agents.
- Runs the phases on the shared timeline and records phase boundaries.
- Optional DUT collectors (SNMP/SSH) poll during the run; credentials are stored encrypted and used read‑only.
- After the run: fetches agent detail, joins latencies, runs the analysis, stores everything in SQLite and generates the report.
- **Exports a matching DUT configuration snippet** for BIRD, FRR, MikroTik RouterOS v7 and Cisco IOS‑XE, with all emulated neighbours, so the DUT side is quick to set up.

## 11. DUT log upload and evaluation

**Upload:** one or more files, plain text or .gz. The platform is auto‑detected (or chosen by hand): Cisco IOS/IOS‑XE/IOS‑XR, MikroTik RouterOS 6/7, BIRD 2/3, Quagga/FRR bgpd.

**Parsing:** vendor‑specific patterns are normalised to events `{time, neighbor, event, detail, severity}`. Examples:

| Event | Cisco | MikroTik | BIRD | Quagga/FRR |
|---|---|---|---|---|
| session up/down | `%BGP-5-ADJCHANGE: neighbor X Up/Down` | `bgp … session established / terminated` | `<proto>: State changed to up/down` | `%ADJCHANGE: neighbor X(…) Up/Down` |
| notification | `%BGP-3-NOTIFICATION: sent/received …` | `… notification …` | `<proto>: Received: Hold timer expired` | `… sending NOTIFICATION …` |
| limits | `%BGP-4-MAXPFX` / `MAXPFXEXCEED` | `… max prefix limit …` | `<proto>: Route limit exceeded` | `… MAXPFXEXCEED …` |
| resources | `%SYS-2-MALLOCFAIL`, `CPUHOG` | `… out of memory …` | `… memory …` | `… memory …` |

Exact patterns will be built from **real sample logs** (Q2). Unknown lines are kept and shown as "unclassified".

**Time alignment:** DUT clocks and time zones differ. The offset is detected automatically by matching DUT "session up" events to our observed Established times (median difference), or entered by hand. The report states the offset used.

**Evaluation:**
- event counts by type and by phase;
- unexpected session resets, with their cause;
- the first error under load (e.g. "first MAXPFX at 2 400 updates/s");
- agreement between DUT‑side and our observed session events;
- all events overlaid on the report graphs.

## 12. Reports

Generated automatically at the end of each run (HTML page, printable/exportable to PDF; data as CSV/JSON).

**Header:** DUT (platform, version, role), scenario, scale, run date, verdict table (key metrics against thresholds), clock offset and accuracy.

**Figures (F1–F12):**

| # | Graph | Shows |
|---|---|---|
| F1 | **Session establishment** — established sessions vs. time, plus per‑router setup‑time bars | how fast the DUT accepts peers |
| F2 | **Initial convergence curve** — % of expected routes received vs. time (all receivers, plus the slowest), time‑to‑100 % marked | the headline convergence figure |
| F3 | **Propagation latency CDF** — announce and withdraw, p50/p99 marked | latency distribution at a glance |
| F4 | **Latency over time vs. offered load** — p50/p99 per second with the offered update rate on a second axis | when the DUT starts to lag |
| F5 | **Throughput knee** — offered vs. delivered updates/s, with p99 latency (stepped‑load runs) | the DUT's maximum sustainable rate |
| F6 | **Backlog over time** — routes sent but not yet received | queue build‑up and drain |
| F7 | **Per‑router heatmap** — routers × time, colour = % routes received (or latency) | stragglers and unfair treatment between peers |
| F8 | **Correctness** — missing/extra/attribute‑changed routes per receiver | correctness at a glance |
| F9 | **Event timeline** — DUT log events + our session events + phase boundaries on one time axis | cause and effect |
| F10 | **DUT resources** — CPU %, memory, RIB size vs. time (if collected) | resource cost of the load |
| F11 | **Data‑plane convergence** — delivered traffic per prefix group vs. time around a withdrawal, blackhole duration marked (if enabled) | RFC 7747 metric |
| F12 | **Generator health** — cluster CPU, send lag, rate | proves the measurement is valid |

**Comparison report:** select 2–5 runs (e.g. the same scenario against Cisco, MikroTik and BIRD). F2, F3, F5 and F10 are overlaid, with a side‑by‑side metrics table. This is meant for papers.

## 13. Technologies (unchanged unless noted)
C++20 / GCC 14 / CMake; Asio (event loops); cpp‑httplib + nlohmann/json; spdlog; GoogleTest/Benchmark · Python 3.13 / FastAPI / pydantic v2 / httpx; **pysnmp** and **asyncssh** for DUT collectors; **pandas + numpy** for analysis · SQLite · Alpine.js + uPlot (live) and **Vega‑Lite** (report graphs, vendored, exportable as SVG/PNG) · PDF via the browser's print to PDF (no extra server dependency) · tshark/Scapy for validation · BIRD 2 on CMNODE06 as the stand‑in DUT for development.

## 14. Milestones (re‑ordered: DUT benchmarking first)

| Phase | Deliverable | Gate |
|---|---|---|
| **P2** | agent: BGP encoder/decoder + FSM + pacing; 60 routers against BIRD on CMNODE06; send/receive timestamps; CLI | 60 sessions Established, 600 k prefixes converged; generator send lag p99 < 10 ms; CPU ≤ 70 % |
| **P3** | controller + web UI: routers, prefixes (generate/paste), scenario phases, live view, admin login | full scenario runs from the browser |
| **P4** | analysis + **reports F1–F9, F12**, comparison report; DUT config export | report generated for BIRD and one other DUT |
| **P5** | DUT log upload + parsers (Cisco, MikroTik, BIRD, Quagga/FRR); SNMP/SSH collectors; F10 | sample logs from Q2 parsed correctly |
| **P6** | data plane: TCP/IP crafted traffic at 1 Gbit/s, data‑plane convergence (F11), BGP stateless; MRT import | AC2, AC3 |
| **P7** | hardening, user guide, validation on the real DUTs | final acceptance |

## 15. Decisions (2026‑10‑19)
D1 own C++ BGP speaker on Asio, reusing the encoder · D2 one admin password · D3 IPv6 later, encoders AF‑agnostic · D4 MRT import (now P6) · D5 Docker stopped on CMNODE04 · D6 shapes constant/on‑off/ramp, selectable · D7 report L2 and L1, gate on L2 · D8 CMNODE01 also generates.

## 16. Open questions

| ID | Question | Why it matters | My suggestion |
|---|---|---|---|
| **Q1** | DUT role: **route server** (eBGP, every router its own ASN), **route reflector** (iBGP, one AS), or both selectable? You mentioned both. | session type, attribute checks, ASN handling | both, selectable per run; route server as the default |
| **Q2** | Which exact platforms/versions will you test? Can you send **sample logs** (a session up, a down and a NOTIFICATION) from each? | parsers are built from real lines | Cisco IOS‑XE, RouterOS 7, BIRD 2, FRR — confirm |
| **Q3** | Can we poll the DUT during runs (read‑only SNMP and/or SSH)? | F10 and resource data points | yes, read‑only account |
| **Q4** | Target scale: routers per node and prefixes per router? | IP pool, memory, P2 gate | 10 routers per node (60), 10 000 prefixes each; up to 1 M total |
| **Q5** | Does the DUT also **forward traffic** (router), or is it only a route server/reflector? | whether data‑plane convergence (F11, P6) applies | keep it as an option |
| **Q6** | Is the new milestone order (BGP benchmarking first, 1 Gbit/s data plane in P6) OK? | priorities | yes |

## 17. Risks

| Risk | Mitigation |
|---|---|
| Generator becomes the bottleneck at high churn, falsifying DUT latencies | AC4 self‑check (send lag), per‑node CPU in every report (F12); more nodes/threads |
| Clock error between nodes distorts latency | chrony < 1 ms (T06), uncertainty stated; same‑node origin/receiver pairs as a cross‑check |
| DUT log formats differ between versions | auto‑detect + manual choice, unclassified lines kept, parsers built from Q2 samples |
| Large receive RIBs exhaust 8 GB nodes | receive modes, planner memory check (≤ 70 % RAM), RAM‑weighted placement |
| DUT config effort for 60 neighbours | config snippet export per platform (§10) |
| Generated routes leak beyond the lab | isolated ETH0 segment, benchmarking/private pools by default, "public‑looking" pool only with explicit opt‑in |
