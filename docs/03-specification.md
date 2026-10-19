# 03 — TrafficGenerator specification

Status: **v0.2 (2026‑10‑19)** — decisions D1–D8 taken (§14). Web interface controls are listed separately in `04-ui-controls.md` for one‑by‑one confirmation.

## 1. Purpose and scope

A distributed traffic generator for IXP / BGP research, running on the 6‑node CM5 cluster (CMNODE01–06). It produces up to **1 Gbit/s** of either

- **TCP/IP traffic**: crafted (stateless) TCP, UDP and ICMP packets whose source and destination addresses come from user‑defined subnets; or
- **BGP traffic**, selectable as:
  - **stateless**: line‑rate TCP/179 segments carrying valid BGP messages (OPEN, UPDATE, KEEPALIVE, NOTIFICATION) for emulated ASes and prefixes;
  - **stateful**: real BGP sessions from emulated ASes to the route server, with configurable table size and update churn.

The run is configured and monitored from a web interface on CMNODE01 and executed in parallel on all selected nodes.

**Out of scope for v1:** stateful TCP connections in TCP/IP mode (decided: crafted), VLAN tagging (decided: untagged), traffic above 1 Gbit/s, IPv6 (D3: later phase; encoders are address‑family agnostic), and receiving/analysing traffic from third parties.

### 1.1 Acceptance criteria (from the brief, made measurable)

| ID | Criterion | How it is measured |
|---|---|---|
| AC1 | All nodes communicate and generate traffic together | cluster suite T01–T12 PASS; in a run, every selected node reports tx > 0 |
| AC2 | 1 Gbit/s of TCP/IP **or** BGP traffic | aggregate **≥ 940 Mbit/s L2** (≈ 980 Mbit/s on the wire) at 1518 B and IMIX, sustained 10 min, sink/route‑server loss < 0.1 %; L1 also reported (D7) |
| AC3 | CPU ≤ 70 % | **node‑average CPU ≤ 70 % on every node**, 1 s samples, 95th percentile over the run (≤ 90 % accepted in the first milestone) |
| AC4 | Worst case documented | at 64 B frames (1.488 Mpps total) the achieved rate and CPU are reported, but not a pass/fail gate |

## 2. Load model (why this fits the hardware)

1 Gbit/s on the wire, split equally over 6 nodes:

| Frame size | Total | Per node | L2 throughput |
|---|---|---|---|
| 64 B (worst case) | 1 488 kpps | **248 kpps** | 762 Mbit/s |
| IMIX 7:4:1 (64/570/1518 B) | 334 kpps | 56 kpps | 947 Mbit/s |
| 1518 B | 81 kpps | 14 kpps | 987 Mbit/s |

A Cortex‑A76 core sending pre‑built frames through a kernel TX ring with batching is expected to sustain several hundred kpps. Even the 64 B case should need about one core per node, which is **≈ 25 % node average** — well inside AC3. This is an estimate; the P2 micro‑benchmark (§12) confirms it before anything else is built on top.

**Fan‑in constraint:** all nodes send towards one 1 Gbit/s switch port (the route server's). The switch has small buffers, so two rules apply:
- the controller guarantees Σ node rates ≤ the configured total;
- each node paces smoothly, in bursts of at most 32 frames, to avoid tail‑drops at the egress port.

## 3. Architecture

```
            browser (laptop on ETH1)
                  │ HTTP + WebSocket :8080
   ┌──────────────▼───────────────────────── CMNODE01 ─────────┐
   │  tgen-controller (Python/FastAPI)                          │
   │   • web UI (static files)   • run planner / scheduler      │
   │   • REST API + WebSocket    • SQLite run history           │
   └───────┬──────────────────────────────────────────────────┬─┘
           │ REST/JSON over mgmt net (192.168.100.0/24) :9100 │
   ┌───────▼──────┐  ┌──────────────┐        ┌───────────────▼┐
   │ tgen-agent   │  │ tgen-agent   │  ...   │ tgen-agent     │   C++20, one per node
   │ CMNODE01     │  │ CMNODE02     │        │ CMNODE06       │   (CMNODE01 also generates)
   │ ┌──────────┐ │  │              │        │                │
   │ │worker c1 │─┼──┼─ AF_PACKET TX ring per worker, pinned core
   │ │worker c2 │ │  │              │        │                │
   │ └──────────┘ │  │              │        │                │
   └──────┬───────┘  └──────┬───────┘        └───────┬────────┘
          └──── traffic subnet 10.100.0.0/24, eth0, untagged ──→ route server on ETH0
```

| Component | Role | Runs on |
|---|---|---|
| **tgen-agent** (C++20) | data plane: builds and sends packets; BGP speaker (stateful); local stats; optional *sink* role (receive and count) | all 6 nodes, systemd service |
| **tgen-controller** (Python) | web UI, validation, run planning, distribution, start/stop, stats aggregation, history | CMNODE01, systemd service |
| **tgen-cli** (Python, thin) | same API from the shell, for scripted experiments | CMNODE01 |

**Start synchronisation:** the controller sends every agent the same `start_at` timestamp, about 2 s in the future. The clocks are within 1 ms (chrony, T06), so all nodes start together without extra coordination traffic.

## 4. Technologies

| Area | Choice | Why |
|---|---|---|
| Agent language | **C++20, GCC 14** (Trixie default), CMake + Ninja | as requested; mature on aarch64 |
| Packet I/O | **AF_PACKET + PACKET_TX_RING (TPACKET_V2) + PACKET_QDISC_BYPASS**; `sendmmsg` fallback | works with the RP1/macb driver; zero syscalls per packet; no special driver needed. AF_XDP kept as a later optimisation behind the same interface (`IPacketSink`) |
| Async I/O (BGP stateful, agent API) | **standalone Asio** (`libasio-dev`) | header‑only, proven, no Boost dependency |
| Agent HTTP API | **cpp‑httplib** (`libcpp-httplib-dev`) + **nlohmann/json** | header‑only, small |
| Logging | spdlog + fmt | already installed |
| C++ tests | **GoogleTest**, Google Benchmark | already installed |
| Controller | **Python 3.13, FastAPI, uvicorn, pydantic v2, httpx** in a venv with pinned versions | fast to build and extend (students) |
| Storage | **SQLite** (`/var/lib/tgen/tgen.db`) | no server to run |
| Frontend | plain HTML + **Alpine.js** (reactivity) + **uPlot** (charts), **vendored** in the repo, no build step | works without internet in the lab; tiny; students can edit it directly |
| Live updates | WebSocket from controller to browser, 1 s | |
| Packet validation | **tshark** (dissects BGP), Scapy in tests | independent decoder checks our encoders |
| Deployment | Ansible role: build once on CMNODE01 (same arch), copy binary to all nodes, systemd units | matches the existing provisioning |
| Stand‑in route server (tests) | **BIRD 2** on CMNODE06 until the real one arrives | real BGP peer for stateful tests |

New packages are added to `install-prereqs.sh`: `libasio-dev libcpp-httplib-dev bird2 python3-scapy tshark` (on the controller: `python3-venv`).

## 5. Data plane (tgen‑agent)

### 5.1 Threads

- **Workers:** 1–3 TX workers, each pinned to its own core (default 2; core 0 is left for IRQs, the OS and the control thread). Each worker owns its own AF_PACKET socket and TX ring. No locks on the hot path.
- **Control thread:** HTTP API, configuration, stats aggregation (lock‑free per‑worker counters, read once per second).
- **BGP thread** (stateful mode only): Asio event loop for all sessions on that node.

### 5.2 Per‑packet work

1. **Templates:** at arm time, the full Ethernet/IP/L4 headers are pre‑built for each traffic class.
2. **Per packet:** copy the template into a ring slot, then rewrite only the varying fields: addresses, ports, IP ID, TCP sequence number, payload marker. Checksums are updated incrementally (RFC 1624) instead of recomputed.
3. **Address and port selection:** a per‑worker generator walks the worker's shard of the address space, either sequentially or with a fast pseudo‑random generator (xorshift128+, seeded per run). Results are reproducible from the run seed.
4. **Pacing:** a token bucket per worker refilled from `CLOCK_MONOTONIC`. Frames are sent in batches of ≤ 32 and the kernel is kicked once per batch. Short waits use busy‑polling (< 50 µs), longer ones `clock_nanosleep`. The target rate accuracy is ±1 % over 1 s.
5. **Payload marker** (optional, default on): 16 bytes holding run ID, node, worker, sequence number and TX timestamp. A sink can then measure loss, reordering and one‑way latency.

### 5.3 Address sharding across nodes and workers

The controller splits every configured source subnet into contiguous slices, one per node and worker, so two workers never send the same source address. Sharding is by count: a /16 over 6 nodes × 2 workers gives 12 slices of about 5 461 addresses each. Destination subnets are shared by all workers, sequentially or randomly per flow.

### 5.4 Destination MAC (safety‑relevant)

The traffic is IP‑routed, so every frame is addressed to the **next hop's MAC**: the route server (or a DUT, or the stand‑in sink). At arm time each agent resolves the next‑hop IP via ARP on the traffic interface. **If it does not resolve, the agent refuses to start.** It never sends broadcast, multicast or unknown‑unicast data frames, so a misconfiguration cannot flood the switch towards ETH1.

## 6. TCP/IP mode (crafted packets)

| Aspect | Specification |
|---|---|
| L3 | IPv4; source from the *source subnets* list, destination from the *destination subnets* list; TTL and DSCP configurable; DF set; IP ID incrementing per worker |
| Protocol mix | TCP / UDP / ICMP echo, by percentage |
| TCP | flag profile: SYN, SYN‑ACK, ACK+data, RST, FIN, or custom; per‑flow sequence numbers; window 64240; options: MSS only (SYN) |
| UDP | payload of the selected size; ports from ranges |
| ICMP | echo request; identifier per flow; sequence incrementing |
| Ports | source range, destination list/ranges |
| Flows | N concurrent 5‑tuples per node; each packet picks a flow by the address‑selection mode; flows can be refreshed every T seconds |
| Sizes | fixed (64–1518 B), uniform range, or IMIX 7:4:1 |
| Shape | constant, on/off bursts, or linear ramp — selectable per run, in both modes (D6) |

## 7. BGP mode

### 7.1 Common model

- **Route server / DUT:** IP and ASN.
- **Emulated peers:** list of {name, ASN (2‑ or 4‑byte), peer IP, router ID, announced prefixes}. Peers can be generated in bulk ("50 peers, ASNs 64601…64650").
- **ASN per peer:** typed in, drawn at random (unused, from a preset or custom range, never the RS ASN), assigned sequentially in bulk, or pasted as a list.
- **Prefixes per peer:** the user sets the **count** per peer, then either **generates** them (random from the pool, prefix‑length range, uniqueness across or within peers; reproducible from the run seed) or **pastes/uploads** their own list (validated, de‑duplicated). Generation runs on the controller, so 1 M‑prefix tables do not load the browser. Peer sets can be exported/imported as JSON/CSV.
- **MRT import (P5, D4):** real RIS/RouteViews tables fill peers, ASNs, prefixes and AS_PATHs.
- **Path attributes:** ORIGIN, AS_PATH (peer ASN first, length range, prepend count, transit ASNs from a pool), NEXT_HOP (peer IP or custom), optional MED, standard and large communities from a pool.
- **Encoder:** one C++ BGP message encoder (RFC 4271, 4‑byte AS RFC 6793, large communities RFC 8092), shared by both variants, so the bytes on the wire are identical in both modes. Validated against tshark's BGP dissector.

### 7.2 Stateless variant (line rate)

Each emulated peer is a pseudo‑session: a fixed 4‑tuple (peer IP:port ↔ RS IP:179) with **consistent TCP sequence numbers**, and every segment carries one or more **whole** BGP messages (never split across segments). Wireshark therefore reassembles and decodes the stream as BGP.

Message mix is configurable: UPDATE‑announce, UPDATE‑withdraw, KEEPALIVE, OPEN, NOTIFICATION. Prefixes per UPDATE: 1–300 (an UPDATE must fit one 1518 B frame, ≈ 340 × /24 at most).

The route server receives this as unsolicited TCP and will not process it as a session. This mode exercises the wire, capture, sFlow/IPFIX, IDS and ACL paths, not the routing table.

### 7.3 Stateful variant (real sessions)

Each emulated peer opens a real TCP session to the route server. The peer IPs are extra addresses that the agent adds to the traffic interface for the duration of the run and removes afterwards. Behaviour:
- OPEN with capabilities (4‑byte AS, MP‑BGP IPv4 unicast, route refresh);
- the configured hold and keepalive timers;
- optional initial table push;
- then churn at a configured rate (announce, withdraw and re‑announce, path changes);
- optional session flaps.

The controller can **export a ready‑made BIRD route‑server configuration** with all emulated neighbours, so the route‑server side needs no hand‑editing.

Rate note: stateful BGP is limited by what the route server accepts, not by our link. This mode is for control‑plane experiments; the 1 Gbit/s criterion is met with the stateless variant (D1: own C++ speaker on Asio, reusing the encoder).

### 7.4 Node assignment

Peers are distributed round‑robin over the selected nodes, weighted by RAM when tables are large: a full IPv4 table is about 1 M prefixes per peer, so large‑table peers go to the 16 GB nodes CMNODE01–04 first.

## 8. Distribution, run lifecycle and safety

**Lifecycle:** `IDLE → VALIDATING → ARMING (agents build templates, resolve MAC, add peer IPs) → RUNNING → STOPPING → DONE | ERROR`

**Planner:** the total rate is divided equally across the selected nodes, since the CPUs are identical; weights are optional. Address slices and peers are assigned as in §5.3 and §7.4.

**Safety rules** (enforced by both controller and agent):
1. The total rate is capped at the configured limit (default 1000 Mbit/s; never above line rate).
2. Traffic goes only to a resolved unicast next‑hop MAC on the traffic interface, never on the management subnet.
3. Source and destination subnets must not overlap the management subnet (192.168.100.0/24). An optional allow‑list restricts them further; the default suggestion is 198.18.0.0/15 (RFC 2544 benchmarking).
4. **Stop** in the UI or CLI reaches all agents within 1 s. If an agent loses the controller for more than 5 s, it stops by itself (watchdog).
5. Every run has a maximum duration (default 1 h).

## 9. Statistics and results

| Level | Metrics (1 s resolution) |
|---|---|
| Worker | packets and bytes sent, ring‑full events, send errors |
| Node | tx Mbit/s (L2 and L1), pps, CPU node‑average and per core, SoC temperature, throttle flags, NIC counters (ethtool deltas: drops, FCS) |
| BGP stateful | per‑peer state, messages sent/received by type, UPDATEs/s, prefixes announced/withdrawn |
| Sink (optional) | rx Mbit/s, pps, loss % and reordering (from the payload marker), one‑way latency (from TX timestamps) |

Every run is stored in SQLite with a full configuration snapshot, its time series and a summary (average/peak rate, 95th‑percentile CPU, AC pass/fail). Export as CSV and JSON. On request, the agent captures a sample pcap (N packets) on one node.

## 10. APIs

### 10.1 Controller (browser and CLI) — `http://192.168.100.11:8080/api/v1`

| Method | Path | Purpose |
|---|---|---|
| GET | `/nodes` | node status (agent up, version, link, EEE, CPU, temperature) |
| GET / PUT | `/config/draft` | current form state |
| POST | `/runs` | validate and start a run (body = run config) → `run_id` |
| POST | `/runs/{id}/stop` | stop |
| GET | `/runs`, `/runs/{id}` | history and details |
| GET | `/runs/{id}/export?fmt=csv\|json` | results |
| POST | `/runs/{id}/capture` | take a sample pcap |
| GET / POST / DELETE | `/profiles[/{name}]` | saved configurations |
| GET | `/bgp/rs-config?fmt=bird` | generated route‑server config |
| WS | `/ws/live` | 1 s stats stream |

### 10.2 Agent — `http://<node-mgmt-ip>:9100/v1` (controller only; shared token)

`GET /health` · `POST /arm` (node plan) · `POST /start` (`start_at`) · `POST /stop` · `GET /stats` · `POST /capture` · `POST /sink` (enable receive‑and‑count)

The run configuration is one JSON document (schema in `schemas/run.schema.json`, generated from the pydantic models), so the UI, CLI and agents share a single definition.

## 11. Repository layout (target)

```
agent/          C++ agent (src/, include/, tests/, bench/), CMakeLists.txt
controller/     FastAPI app (tgen_controller/), web/ (static UI, vendored JS), tests/
schemas/        run.schema.json
deploy/         installer, Ansible (+ roles/tgen for build & deploy), systemd units
tests/cluster/  connectivity suite (done), acceptance tests (AC1-AC4)
docs/           00-04 + user guide
```

## 12. Test strategy

| Level | What | Tools |
|---|---|---|
| Unit (C++) | encoders (Ethernet/IPv4/TCP/UDP/ICMP/BGP), incremental checksums vs full recompute, address sharding (no overlap, full coverage), token bucket accuracy | GoogleTest |
| Golden packets | generate pcaps, decode with **tshark**, compare fields; BGP messages must dissect without errors | tshark, Scapy |
| Micro‑benchmark (P2 gate) | single node, single worker: kpps and CPU at 64 B / IMIX / 1518 B | Google Benchmark + run on CMNODE02 |
| Controller | pydantic validation, planner splits, API contract | pytest, httpx |
| Integration | full run with the stand‑in sink on CMNODE06 (receive and count) | cluster |
| BGP stateful | sessions to BIRD 2 on CMNODE06; state machine, timers, table push, churn counts verified in BIRD | cluster |
| Acceptance | AC1–AC4 scripted; produces a report | `tests/cluster/acceptance.sh` |

## 13. Milestones

| Phase | Deliverable | Gate |
|---|---|---|
| **P2** | agent core: templates, TX ring, pacing, TCP/IP crafted mode, stats, CLI only | micro‑benchmark: 248 kpps at 64 B on one node with node‑average CPU ≤ 70 % |
| **P3** | controller + web UI (TCP/IP mode), multi‑node planner, sink role, history | **AC1, AC2, AC3 for TCP/IP** |
| **P4** | BGP encoder + stateless variant + BGP UI | **AC2, AC3 for BGP** |
| **P5** | BGP stateful + route‑server config export + BIRD tests | sessions established to BIRD, churn verified |
| **P6** | hardening, docs, user guide, real route server validation (T09/T11) | final acceptance |

## 14. Decisions (taken 2026‑10‑19)

| ID | Question | Decision |
|---|---|---|
| D1 | Stateful BGP implementation | **own C++ speaker on Asio, reusing the stateless encoder** |
| D2 | Web UI access | **one admin password** (login page, session cookie) |
| D3 | IPv6 | **later phase**; encoders designed for IPv4 and IPv6 |
| D4 | Real routing tables | **MRT import in P5** |
| D5 | Docker on CMNODE04 | **stop and disable** (as on CMNODE01) |
| D6 | Traffic shapes | **constant, on/off bursts and ramp, user selects per run** (both modes) |
| D7 | Definition of "1 Gbit/s" | **report L2 and L1; pass/fail on L2 frame bits (≥ 940 Mbit/s)** |
| D8 | CMNODE01 generates traffic | **yes** — 6 senders |

Additional requirement (2026‑10‑19): in BGP mode the user selects or generates AS numbers per peer and sets each peer's prefix count, then generates the prefixes with a button or pastes their own (§7.1, controls B3–B5b).

## 15. Risks

| Risk | Impact | Mitigation |
|---|---|---|
| Kernel TX ring slower than estimated on RP1 | AC3 at small packets | P2 benchmark gate first; AF_XDP behind the same interface |
| Switch egress drops when 6 nodes converge on one port | loss at the route server | smooth pacing (≤ 32‑frame bursts), Σ rate cap, sink measurements |
| Route server rejects or flaps stateful sessions at high churn | P5 results | rate limits per peer; BIRD stand‑in to tune first |
| Generated traffic leaks beyond the lab | external impact | §8 safety rules; ETH1 isolated |
| Spoofed source addresses dropped by the RS host's reverse‑path filtering | sink sees nothing | documented RS host setting (`rp_filter=0` on its traffic interface) |
