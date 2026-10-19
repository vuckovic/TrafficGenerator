# 04 — Web interface controls (for confirmation)

Status: **PROPOSED v0.2 (2026‑10‑19)** — decisions D1–D8 applied; BGP peer/ASN/prefix controls reworked per your request (B3–B5b) · Please mark each control: ✅ accept · ✏️ change (say how) · ❌ drop. A short reply is enough, e.g. *"all ✅ except T12 ❌, B5 ✏️ max length 20"*.

Layout: one page, **Run** (left: configuration, right: live monitoring), plus the **History** and **Nodes & settings** tabs. The mode selector switches the configuration panel between TCP/IP and BGP.

## G — Global run controls (both modes)

| ID | Control | Type | Default | Validation / notes |
|---|---|---|---|---|
| G1 | Mode | radio: TCP/IP · BGP | TCP/IP | switches the panel |
| G2 | **Start** | button | — | disabled until the form validates and all selected nodes are ready |
| G3 | **Stop** | button (always visible, red) | — | stops all nodes within 1 s |
| G4 | Run state | status badge | IDLE | IDLE / VALIDATING / ARMING / RUNNING / STOPPING / DONE / ERROR |
| G5 | Total target rate | number + unit (Mbit/s · kpps) | 900 Mbit/s | 1–1000 Mbit/s; capped by S3 |
| G6 | Duration | number (s), 0 = until Stop | 60 | ≤ max duration S3 (default 3600) |
| G7 | Ramp‑up | number (s) | 5 | 0–60; linear increase to target |
| G8 | Nodes | 6 checkboxes CMNODE01–06, each showing status | all checked | ≥ 1; nodes not ready are greyed out |
| G9 | Rate split | radio: equal · weighted (per‑node %) | equal | weights sum to 100 |
| G10 | Next‑hop / target IP | IPv4 + "resolve" button showing the MAC | route server IP from config | must resolve before Start |
| G11 | Run name / notes | text | auto `run‑YYYYMMDD‑HHMMSS` | saved with results |
| G12 | Profiles | dropdown + Save / Load / Delete | — | named, stored configurations |
| G13 | Random seed | number | random, shown | same seed = same address/port sequence |
| G14 | Traffic shape (both modes) | dropdown: constant · on/off bursts · ramp, with parameters: on ms / off ms, or ramp start % → end % over N s | constant | decision D6: all three, user selects |

## T — TCP/IP mode

| ID | Control | Type | Default | Validation / notes |
|---|---|---|---|---|
| T1 | Source subnets | list of CIDRs (add/remove rows) | 198.18.0.0/16 | IPv4; no overlap with 192.168.100.0/24; ≤ 64 entries |
| T2 | Destination subnets | list of CIDRs | 198.19.0.0/16 | same rules |
| T3 | Address selection | radio: sequential · random | random | |
| T4 | Protocol mix | 3 sliders/numbers: TCP % · UDP % · ICMP % | 60 / 35 / 5 | sum = 100 |
| T5 | TCP flags profile | dropdown: SYN · SYN‑ACK · ACK+data · RST · FIN · custom (flag checkboxes) | ACK+data | |
| T6 | Source port range | from–to | 1024–65535 | 1–65535 |
| T7 | Destination ports | list of ports/ranges | 80, 443, 53 | |
| T8 | Packet size | radio: fixed · uniform range · IMIX 7:4:1 | IMIX | fixed 64–1518 B; range within 64–1518 |
| T9 | Concurrent flows per node | number | 10 000 | 1–1 000 000 |
| T10 | Flow refresh interval | number (s), 0 = never | 0 | new 5‑tuples every N s |
| T11 | DSCP | number 0–63 | 0 | |
| T12 | TTL | number 1–255 | 64 | |
| T13 | Payload | dropdown: zeros · random · incrementing | zeros | |
| T14 | Payload marker | checkbox (run/seq/timestamp for loss and latency) | on | needs ≥ 16 B of payload |

## B — BGP mode

| ID | Control | Type | Default | Validation / notes |
|---|---|---|---|---|
| B1 | BGP variant | radio: stateless (line rate) · stateful (real sessions) | stateless | |
| B2 | Route server | IP + ASN | from config, ASN 64500 | 2‑ or 4‑byte ASN |
| B3 | **Emulated peers table** | one row per peer: name · **ASN** · **prefix count** · prefixes status ("1 000 generated" / "245 pasted") · **[Generate]** · **[Paste…]** · peer IP (auto) · router ID (auto) · node (auto) · delete | 6 peers | ASN 1–4294967295, unique, ≠ route‑server ASN; per‑row buttons act on that peer only |
| B3a | ASN per peer | number field **or** [Random] button (draws an unused ASN from the B4 ASN range) | from B4 | |
| B3b | Prefix count per peer | number | 1 000 | 1–1 000 000 (stateful full table ≈ 1 M); changing it after Generate asks to regenerate |
| B3c | [Generate] prefixes | button (per peer) | — | draws *count* random prefixes using B5 settings; reproducible with seed G13; preview of first 20 + download |
| B3d | [Paste…] prefixes | dialog: text area, one CIDR per line (commas/spaces also accepted) or .txt upload | — | each line validated; duplicates removed; count updated automatically; invalid lines listed with line numbers; > 100 000 lines → use upload |
| B4 | **Add peers (bulk)** | number of peers + ASN mode: **sequential** (first ASN, step) · **random** (from range) · **paste list** of ASNs + prefix count per peer: fixed or random min–max + checkbox "generate prefixes now" | 50 peers, random ASNs, 1 000 prefixes | ASN range presets: private 16‑bit 64512–65534 · private 32‑bit 4200000000–4294967294 · documentation 64496–64511 · custom; RS ASN excluded |
| B4a | Generate all / Clear all | buttons | — | (re)generate or clear prefixes for every peer at once |
| B5 | **Prefix generator settings** | address pool(s) (CIDR list) · prefix length min–max · overlap: unique across all peers / unique per peer / allowed | pool 198.18.0.0/15, /24–/24, unique across all peers | pool must hold the requested count (live check: "pool fits 512 /24s, requested 6 000" → error); mgmt and traffic subnets excluded |
| B5a | Pool preset | dropdown: benchmarking 198.18.0.0/15 · RFC 1918 · **public‑looking** (random from global unicast space, reserved ranges excluded) · custom | benchmarking | public‑looking shows a warning: only for an isolated route server; requires S3 allow‑list to permit it |
| B5b | Import / export peer set | buttons: export peers + ASNs + prefixes as JSON/CSV; import the same | — | lets you reuse an exact peer set across runs and papers |
| B6 | AS_PATH length | range min–max | 1–5 | 1–64; peer ASN always first |
| B7 | Origin‑AS prepend | number | 0 | 0–10 |
| B8 | Transit ASN pool | ASN list/range | 64700–64799 | used to fill the paths |
| B9 | ORIGIN | dropdown: IGP · EGP · INCOMPLETE · mixed | IGP | |
| B10 | NEXT_HOP | radio: peer IP · custom IP | peer IP | |
| B11 | MED | checkbox + value/range | off | |
| B12 | Communities | standard + large community lists, "random k from pool" | none | RFC 1997 / RFC 8092 formats |
| B13 | Prefixes per UPDATE | number | 10 | 1–300 (one UPDATE must fit a 1518 B frame: ≈ 340 × /24 max; larger values are capped) |
| B14 | Message mix (stateless) | percentages: announce · withdraw · KEEPALIVE · OPEN · NOTIFICATION | 70 / 20 / 8 / 1 / 1 | sum = 100 |
| B15 | Hold time / keepalive (stateful) | numbers (s) | 90 / 30 | hold 0 or ≥ 3 |
| B16 | Initial table push (stateful) | checkbox | on | |
| B17 | Churn rate (stateful) | number (UPDATEs/s total) + withdraw/re‑announce ratio | 100/s, 50 % | |
| B18 | Session flaps (stateful) | checkbox + interval (s) + peers % | off | |
| B19 | 4‑byte ASN capability | checkbox | on | |
| B20 | Export route‑server config | button: download BIRD config for all emulated peers | — | |
| B21 | MRT import (P5) | file upload (RIS/RouteViews dump): fills peers, ASNs, prefixes and AS_PATHs from a real table | — | decision D4 = yes; optional filters: peer ASN, max prefixes |

## M — Live monitoring (right‑hand panel)

| ID | Control | Type | Notes |
|---|---|---|---|
| M1 | Total rate chart | line chart, Mbit/s (L2 and L1) + kpps, last 5 min | target shown as a dashed line |
| M2 | Per‑node table | tx Mbit/s · kpps · drops · CPU node‑avg · hottest core · temperature · state | |
| M3 | CPU indicator per node | bar with 70 % / 90 % marks, green/amber/red | AC3 at a glance |
| M4 | Per‑node rate chart | stacked area | shows the split |
| M5 | BGP sessions (stateful) | table: peer · state · msgs in/out · UPDATEs/s · prefixes | |
| M6 | Sink panel | rx Mbit/s · loss % · reordering · latency p50/p99 | when a sink node is used (S2) |
| M7 | Events log | scrolling list: warnings, safety refusals, errors | |

## H — History

| ID | Control | Type | Notes |
|---|---|---|---|
| H1 | Runs list | table: time · name · mode · duration · avg rate · CPU p95 · AC result | sortable, filter by mode |
| H2 | Run detail | config snapshot + charts | |
| H3 | Export | buttons CSV · JSON | |
| H4 | Capture sample | button: pcap of N packets from node X | N default 1000 |
| H5 | Re‑run | button: load this config into the form | |
| H6 | Delete run | button with confirmation | |

## S — Nodes & settings

| ID | Control | Type | Default | Notes |
|---|---|---|---|---|
| S1 | Node status | table: reachable · agent version · link · EEE · temperature · Restart agent | — | |
| S2 | Sink node | dropdown: none · CMNODE0x | none | that node receives and counts instead of sending (stand‑in until the route server exists) |
| S3 | Safety limits | max total rate (≤ 1000), max duration, subnet allow‑list | 1000 Mbit/s, 3600 s, empty | |
| S4 | Workers per node | number 1–3 | 2 | advanced |
| S5 | Login | single admin password: login page + change password | set at install | decision D2 = (b); session cookie, logout button |
