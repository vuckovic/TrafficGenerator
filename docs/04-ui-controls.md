# 04 — Web interface controls (for confirmation)

Status: **FROZEN v1.1 (2026‑10‑20)**: v1.0 of 2026‑10‑19 plus G9, the U2 change and U8–U16, all confirmed · visual design LOCKED v1 (`docs/design/`) — Q1–Q6 answered (spec v0.4). **Every non‑obvious control gets a short help text (ⓘ tooltip); role and mode choices also show a visible explanation label; every metric (live tiles, tables, report verdict rows, figure values) shows a short explanation on hover and on keyboard focus.** **Every chart is interactive:** a cursor follows the mouse (or a slider, for keyboard use) and shows the exact value of each series at that point; time‑based charts have zoom levels and a window slider to move the time range left and right. Please mark each control: ✅ accept · ✏️ change (say how) · ❌ drop. A short reply is enough, e.g. *"all ✅ except C9 ❌, R3b ✏️ max 2 M"*.

**Layout** (tabs):
1. **Benchmark** — set up the DUT, routers and scenario on the left; live view on the right.
2. **Traffic** — TCP/IP and BGP‑stateless data plane.
3. **Reports** — run reports and comparison.
4. **DUT logs** — upload and evaluate.
5. **History**.
6. **Nodes & settings**.

## G — Global (all tabs)

| ID | Control | Type | Default | Validation / notes |
|---|---|---|---|---|
| G1 | Run type | radio: **BGP benchmark** · TCP/IP traffic · BGP stateless · benchmark + data‑plane traffic | BGP benchmark | selects which panels apply |
| G2 | **Start** | button | — | enabled when the form validates and all selected nodes are ready |
| G3 | **Stop** | button (always visible, red) | — | stops all nodes within 1 s; runs the teardown phase if possible |
| G4 | Run state | status badge + current phase + elapsed time | IDLE | IDLE / VALIDATING / ARMING / RUNNING (phase n/m) / STOPPING / ANALYSING / DONE / ERROR |
| G5 | Nodes | 6 checkboxes CMNODE01–06 with status and RAM | all | ≥ 1 |
| G6 | Run name / notes | text | auto `run‑YYYYMMDD‑HHMMSS` | stored with the run and printed on the report |
| G7 | Profiles | dropdown + Save / Load / Delete | — | complete configurations (DUT, routers, scenario, traffic) |
| G8 | Random seed | number | random, shown | same seed = same ASNs, prefixes and timings |
| G9 | **Language** | flag button in the top‑right corner of every page → menu with flag + native name: 🇬🇧 English · 🇷🇸 Српски | browser language (sr* → Српски), else English | choice kept per browser; all texts from `ui/i18n/labels.csv` (one column per language, English fallback); adding a language = adding a column and a flag (see `ui/i18n/README.md`) |

## U — Device under test (Benchmark tab)

| ID | Control | Type | Default | Validation / notes |
|---|---|---|---|---|
| U1 | **DUT role** | radio: **Route server** (default) · **Route reflector** · **Router (forwards traffic)** — with the explanation label below | Route server | Q1/Q5; sets session type, ASN handling, correctness checks and whether data‑plane tests are available |
| U1a | Role explanation label (shown under U1, changes with the selection) | text | — | **Route server:** *"IXP route server. Every emulated router is a separate eBGP peer with its own AS number. The route server passes routes between peers without adding its own AS number and without changing the next hop. Control‑plane test only."* · **Route reflector:** *"iBGP route reflector. All emulated routers are clients in the DUT's AS; the AS numbers you set appear as origin AS in the AS_PATH. The reflector adds ORIGINATOR_ID and CLUSTER_LIST and keeps the next hop. Control‑plane test only."* · **Router (forwards traffic):** *"BGP router that sets itself as next hop. Routes are learned and re‑advertised, and data traffic between the emulated routers' prefixes is sent through the DUT, so forwarding, loss and data‑plane convergence can be measured."* |
| U2 | Detected platform | read‑only: model and version read from the DUT (U14 Test connection), e.g. "RouterOS 7.11 · CRS106‑1C‑5S" | — | was a dropdown; the platform is now chosen in U8. Printed on the report; selects the log parser |
| U3 | DUT IP and ASN | IPv4 + ASN | from cluster config | in RR mode all routers use this ASN |
| U4 | DUT config export | button: download a neighbour config for the selected platform | — | BIRD, FRR, RouterOS 7, IOS‑XE |
| U5 | SNMP polling (Q3: option) | checkbox + version (v2c / v3) + community or v3 user/auth/priv | off | read‑only; "Test" button |
| U6 | SSH polling (Q3: option) | checkbox + host, user, password/key, platform command set | off | read‑only; "Test" button |
| U7 | Polling interval | number (s) | 5 | 1–60 |

**DUT configuration (new card under *Device under test*):** before each run the controller adds one passive BGP neighbour per emulated router (its IP and AS number, role settings) to the DUT over SSH, verifies the count, and removes exactly those neighbours afterwards. Only entries tagged `tgen` are touched.

| ID | Control | Type | Default | Validation / notes |
|---|---|---|---|---|
| U8 | **Platform** | segmented: **MikroTik RouterOS 7** · **Cisco IOS / IOS‑XE** (IOS 15.x, IOS‑XE 16.x/17.x, 2015 and later) · **BIRD 2/3** · **Quagga / FRR** — plus a note label under it per platform; dropdown entries *BIRD stand‑in on a cluster node* and *Other (configure manually)* | BIRD stand‑in | note texts: RouterOS adds its own AS (no transparent RS); Cisco RS needs route‑server‑client (IOS‑XE 3.3S+/IOS 15.2S+) and writes running‑config only; BIRD uses its own include file; Quagga is EOL, FRR commands are used |
| U9 | Configure the DUT before each run | checkbox | on | off = neighbours are configured by hand (U4 export still available) |
| U10 | SSH address · port | IPv4/hostname · number | = DUT IP (U3) · 22 | management address may differ from the BGP peering address |
| U11 | User | text | admin (MikroTik, Cisco) · root (BIRD, FRR) | |
| U12 | Authentication | radio: **password — asked at start, not stored** · SSH key of CMNODE01 ("Show public key" to copy to the device) | password | password lives only in memory for the run (D2: no stored device secrets); Cisco: **enable secret** field, only if the user is not privilege 15 |
| U13 | Linux DUT options (BIRD, FRR) | use sudo (checkbox) · BIRD include file · birdc socket | on · /etc/bird/tgen.conf · default | BIRD: bird.conf gets one `include` line the first time (shown in the run log) |
| U14 | Actions | buttons: **Test connection** · **Preview config** · **Push now** · **Remove** · **Status** + status line (model, version, CPU, free memory, tgen neighbours, Established) | — | Preview shows the exact text that will be sent; Push now is optional (a run pushes its own config) |
| U15 | After the run | dropdown: remove neighbours · keep neighbours | remove | keep = for manual inspection; the next run replaces them |
| U16 | Route limit | number (routes the DUT must hold) | 20 000 for MikroTik, empty (none) otherwise | Start is refused above it, with the reason |

## R — Emulated routers (Benchmark tab)

| ID | Control | Type | Default | Validation / notes |
|---|---|---|---|---|
| R1 | **Routers per node** | number, or switch to "total, auto‑distribute" | 10 per node (60); CMNODE01 at 0.7 weight → 7, CMNODE02–04 → 11, CMNODE05–06 → 10 (S7) | 1–30 per node (180 total, limited by the S4 pool on a /24; Q4); the planner shows the IPs and memory needed |
| R2 | Placement | radio: equal per node · RAM‑weighted | equal | RAM‑weighted puts *full* receivers on CMNODE01–04 |
| R3 | **Routers table** | one row per router: name · **ASN** · **prefix count** · prefixes status ("1 000 generated" / "245 pasted") · **[Generate]** · **[Paste…]** · receive mode · node · peer IP (auto) · delete | filled from R1/R4 | ASN 1–4294967295, unique (eBGP), ≠ DUT ASN |
| R3a | ASN per router | number **or** [Random] (draws an unused ASN from the R4 range) | from R4 | in RR mode: origin ASN for the AS_PATH |
| R3b | Prefix count per router | number | 10 000 | 1–1 000 000; changing it after Generate asks to regenerate |
| R3c | [Generate] | button per router | — | random prefixes per R5; reproducible with G8; preview of first 20 + download |
| R3d | [Paste…] | dialog: one CIDR per line (commas/spaces accepted) or .txt upload | — | validated; duplicates removed; count updated; invalid lines listed with line numbers |
| R3e | Receive mode per router | dropdown: full · timestamps · count | 1 full per node, rest timestamps | §4.3 of the spec; memory check |
| R4 | **ASN assignment (bulk)** | mode: sequential (first, step) · random (range) · paste list; range presets: private 16‑bit · private 32‑bit · documentation · custom | random, private 32‑bit | DUT ASN excluded |
| R4a | Prefix count (bulk) | fixed or random min–max for all routers | fixed 10 000 | |
| R4b | Generate all / Clear all | buttons | — | |
| R5 | Prefix generator settings | pool(s) (CIDR list) · prefix length min–max · uniqueness: across all routers / per router / overlap allowed | **10.0.0.0/8 (excl. 10.100.0.0/16), /24–/28, unique across all** | live check that the pool is large enough (this default holds > 1 M distinct prefixes; 600 000 needed at default scale); mgmt and traffic subnets excluded |
| R5a | Pool preset | **private large (default, 10/8 /24–/28)** · benchmarking 198.18.0.0/15 (only 512 × /24 — small tests) · RFC 1918 all · public‑looking (global unicast /24, reserved excluded — most realistic) · custom | private large | public‑looking: warning, isolated DUT only, needs S3 allow‑list |
| R6 | AS_PATH | length min–max · origin prepend · transit ASN pool | 1–3 · 0 · 64700–64799 | router's ASN always first (eBGP) |
| R7 | ORIGIN · NEXT_HOP · MED | dropdown · radio (self / custom) · checkbox + value | IGP · self · off | |
| R8 | Communities | standard and large lists, "random k from pool" | none | RFC 1997 / RFC 8092 |
| R9 | Timers and capabilities | hold / keepalive (s) · 4‑byte ASN · route refresh | 90 / 30 · on · on | hold 0 or ≥ 3 |
| R10 | Import / export router set | buttons, JSON/CSV | — | reuse exact sets across runs |
| R11 | MRT import (P6) | file upload (RIS/RouteViews) → routers, ASNs, prefixes, AS_PATHs | — | D4 |

## C — Scenario (Benchmark tab)

| ID | Control | Type | Default | Validation / notes |
|---|---|---|---|---|
| C1 | **Preset** | dropdown: Quick check · Initial convergence · Throughput knee · Resilience · Full IXP day · Custom | Quick check | fills C2 |
| C2 | **Phase list** | ordered list: add / remove / reorder / duplicate; each phase shows type, duration and parameters | from C1 | types: Establish, Initial load, Steady, Churn, Flap/failure, Teardown |
| C3 | Establish | sessions per second (0 = all at once) · timeout | 0 · 60 s | |
| C4 | Initial load | announce rate (prefixes/s, 0 = as fast as possible) · prefixes per UPDATE | 0 · 100 | 1–300 per UPDATE |
| C5 | Steady | duration (s) | 60 | |
| C6 | Churn | rate: constant or **stepped** (start, step, step duration, max) · mix: announce % / withdraw % / attribute change % · traffic shape (constant · on/off · ramp, D6) | stepped 100 → +100 every 30 s → 2 000 /s · 40/40/20 | |
| C7 | Flap / failure | % of routers · type: reset (NOTIFICATION) · silent (hold‑timer expiry) · mass withdraw · interval and count | 10 % · reset · once | |
| C8 | Teardown | withdraw rate (0 = all at once) · close sessions | 0 · yes | |
| C9 | Repetitions | number | 1 | runs C2 n times; the report shows the spread |
| C10 | Thresholds | p99 latency limit for the knee (s) · convergence target (s) · allowed missing routes | 1 s · — · 0 | used in the report verdict |
| C11 | Timeline preview | read‑only chart of planned phases and rates | — | updates live while editing |

## T — Traffic tab (data plane)

| ID | Control | Type | Default | Validation / notes |
|---|---|---|---|---|
| T1 | Total target rate | number + unit (Mbit/s · kpps) | 900 Mbit/s | 1–1000; capped by S3; no single node above link − S7 reserve |
| T2 | Duration / ramp‑up | numbers (s) | 60 / 5 | 0 = until Stop |
| T3 | Rate split | equal · weighted per node | equal | |
| T4 | Next‑hop IP | IPv4 + "resolve" (shows the MAC) | DUT IP | must resolve before Start |
| T5 | Destinations | radio: **subnets** (list) · **scenario prefixes** (towards routes announced by selected routers) | subnets | *scenario prefixes* only when U1 = Router (forwards traffic); label: "A route server or reflector keeps the original next hop, so traffic would bypass the DUT." Enables F11 |
| T6 | Source / destination subnets | CIDR lists | 198.18.0.0/16 → 198.19.0.0/16 | no overlap with management subnet |
| T7 | Address selection | sequential · random | random | |
| T8 | Protocol mix | TCP % · UDP % · ICMP % | 60 / 35 / 5 | sum = 100 |
| T9 | TCP flags profile | SYN · SYN‑ACK · ACK+data · RST · FIN · custom | ACK+data | |
| T10 | Ports | source range · destination list | 1024–65535 · 80, 443, 53 | |
| T11 | Packet size | fixed · range · IMIX 7:4:1 | IMIX | 64–1518 B |
| T12 | Flows per node · refresh | numbers | 10 000 · 0 s | |
| T13 | DSCP · TTL | numbers | 0 · 64 | |
| T14 | Payload · marker | zeros/random/incrementing · marker on/off | zeros · on | marker enables loss/latency at the sink |
| T15 | Traffic shape | constant · on/off (ms/ms) · ramp (start % → end % over s) | constant | D6 |
| T16 | BGP stateless options | message mix (announce/withdraw/KEEPALIVE/OPEN/NOTIFICATION %) · prefixes per UPDATE | 70/20/8/1/1 · 10 | only for run type "BGP stateless" |

## M — Live view (right‑hand panel)

| ID | Control | Type | Notes |
|---|---|---|---|
| M1 | Phase bar | timeline with the current phase highlighted | |
| M2 | Sessions | counters by state + table: router · node · state · msgs in/out · prefixes sent/received | |
| M3 | Convergence progress | % expected routes received (all receivers, slowest) | live F2 |
| M4 | Live latency | p50/p99 per second + offered rate | live F4 |
| M5 | DUT resources | CPU / memory sparkline (if U5/U6) | live F10 |
| M6 | Generator health | per node: CPU node‑average, send lag, temperature (70 %/90 % marks) | AC3/AC4 at a glance |
| M6a | Control‑plane health | CMNODE01 reserve in use (CPU / RAM / link), API response time, live‑update delay; amber/red when the reservation is at risk | AC6; the scenario pauses automatically when red |
| M7 | Data plane | rate chart (L2/L1), per‑node table, sink loss/latency | when traffic is running |
| M8 | Events | scrolling list: session changes, NOTIFICATIONs, warnings, safety refusals | |

## X — Reports tab

| ID | Control | Type | Default | Notes |
|---|---|---|---|---|
| X1 | Run report | page per run: header, verdict table, figures F1–F12 (spec §12), tables | generated at the end of each run | figures without data are hidden |
| X2 | Figure options | per figure: zoom to a phase, linear/log axis, show/hide series | — | |
| X3 | Export | PDF (print layout) · HTML · figures as SVG/PNG · data as CSV/JSON | — | |
| X4 | **Comparison** | select 2–5 runs → overlaid F2, F3, F5, F10 + side‑by‑side metrics table | — | e.g. Cisco vs MikroTik vs BIRD |
| X5 | Report notes | text field per report (conclusions, setup notes) | — | printed on the report |
| X6 | Regenerate | button: re‑run the analysis (e.g. after adding DUT logs or changing thresholds) | — | |

## L — DUT logs tab

| ID | Control | Type | Default | Notes |
|---|---|---|---|---|
| L1 | Upload logs | file upload, multiple, .txt/.log/.gz | — | attached to the selected run |
| L2 | Run | dropdown (which run the logs belong to) | latest | |
| L3 | Platform | auto‑detect · Cisco · MikroTik · BIRD · Quagga/FRR | auto | |
| L4 | Time offset | auto (match session‑up events) · manual (± s, time zone) | auto | the offset used is shown |
| L5 | Parsed events | table: time · neighbour · event · detail · severity; filters; "unclassified" lines tab | — | |
| L6 | Log summary | counts by event type and phase; first error under load; agreement with observed sessions | — | flows into the report (F9) |

## H — History

| ID | Control | Type | Notes |
|---|---|---|---|
| H1 | Runs list | time · name · run type · DUT · scale · duration · key metric · verdict | sortable, filters |
| H2 | Open report | link to X1 | |
| H3 | Re‑run | load this configuration into the form | |
| H4 | Capture sample | pcap of N packets from node X (during a run) | N = 1000 |
| H5 | Delete run | with confirmation | |

## S — Nodes & settings

| ID | Control | Type | Default | Notes |
|---|---|---|---|---|
| S1 | Node status | reachable · agent version · link · EEE · CPU · RAM · temperature · restart agent | — | |
| S2 | Sink node | none · CMNODE02–06 (CMNODE01 excluded, §10.2) | none | receives and counts data‑plane traffic (stand‑in or measurement) |
| S3 | Safety limits | max data rate (≤ 1000 Mbit/s) · max duration · subnet allow‑list · max routers/prefixes | 1000 · 3600 s · empty · 180 / 2 M | |
| S4 | Router IP pool | range on the traffic subnet | 10.100.0.20–250 (231) | ≥ number of routers; nodes .11–.16 and DUT excluded |
| S5 | Workers per node | number 1–3 | 2 | advanced |
| S6 | Login | admin password: login page, change password, logout | set at install | D2 |
| S7 | **Control‑plane reservation** | CMNODE01: CPU reserve % · memory reserve % · load weight · per‑link bandwidth reserve (Mbit/s) | 30 % · 30 % · 0.7 · 20 Mbit/s | label: *"Keeps the web interface and controller reachable while a simulation runs at full load."*; minimum 20 % / 20 % / 5 Mbit/s |
