# 00 — Findings, options and proposed approach

Status: **Decisions confirmed 2026‑10‑16 (see §0)** · Stage 1 of the workflow (inspect → approach → install → test plan)

## 0. Decisions confirmed (2026‑10‑16)

| # | Question | Decision |
|---|---|---|
| Q2 | Meaning of BGP traffic | **Selectable in the UI**: stateless line‑rate BGP PDUs *and* stateful sessions to the route server |
| Q3 | TCP/IP mode | **Crafted** (stateless) TCP/UDP/ICMP packets with user‑defined subnets |
| Q4 | Implementation languages | Web interface/controller in **Python**, traffic generator in **C++** |
| Q5 | 70 % CPU criterion | **Node average** (hottest core still reported for diagnosis) |
| Q6 | Network layout | **No VLAN tagging** for now; route server on board port **ETH0**; everything on the internal switch |
| Q7 | Board port ETH1 | Free (internal switch) — used for laptop / web UI access |
| Q8 | Attached PDF | Camera ISP spec attached by mistake; ignored |
| — | Node names / addresses | **Kept as installed**: CMNODE01–06 = 192.168.100.11–.16 (`MGMT_MODE=keep`) |
| Q9 | OS | **Raspberry Pi OS Trixie** (Bookworm still supported by the installer) |

Block diagram: `docs/assets/super6c-block-diagram.png` (from DeskPi; the vendor drew it for CM4).

## 1. Findings from the inputs

| # | Source | Finding | Consequence |
|---|---|---|---|
| F1 | Super6C README | Nodes are interconnected by an onboard **RTL8370N 8‑port gigabit switch**: 6 ports → one per module, 2 ports → the external RJ45 jacks (ETH0, ETH1). | Each CM5 has **exactly one 1 GbE link**. The "two gigabit cards" are two uplink ports of the same switch, not two NICs per node. |
| F2 | Super6C README | No switch management interface is documented (unmanaged as shipped). | No VLAN membership enforcement, no port isolation. Broadcast, multicast and unknown‑unicast frames are flooded to **both** ETH0 and ETH1. |
| F3 | Super6C README | Per‑module DC‑DC, 100 W PSU (19–24 V DC or ATX 12 V), M.2 2280 M‑key (PCIe x1) and microSD per module, 3× 12 V fan headers. | Six CM5 under sustained load plus NVMe must be power‑ and thermal‑checked (test M2, T10). |
| F4 | Super6C README / resellers | Board is designed for CM4; resellers now list CM5 support. | CM5 boot, PCIe and Ethernet must be verified on this exact board revision (test T02). |
| F5 | CM5 platform | BCM2712, 4× Cortex‑A76 @ 2.4 GHz; Ethernet MAC lives in the RP1 I/O controller (Linux `macb` driver). | Kernel AF_PACKET always works. Native (zero‑copy) AF_XDP support in this driver is **unverified** — treat it as an optional optimisation. |
| F6 | Attached PDF | `RP-008158-DS-1` is the **Raspberry Pi Image Signal Processor (PiSP)** spec — attached by mistake. | Ignored. The CM5 design files (`.dts` device trees) are not needed: we use stock Raspberry Pi OS kernels. |
| F7 | Constraints | 1 Gbit/s total towards the route server on ETH0, shared by 6 nodes. | Each node needs only ~167 Mbit/s. Worst case (64‑byte frames) = 1.488 Mpps total ≈ **248 kpps per node**; at IMIX ≈ 60 kpps per node. |
| F8 | Constraints | 4× 16 GB, 2× 8 GB RAM. | RAM is irrelevant for packet generation; it only matters for BGP RIBs (a full IPv4+IPv6 table fits easily in 8 GB). Work is split by **CPU**, which is identical on all six. |

## 2. Decisions — options, strongest argument, evidence, recommendation

### D1. Separating web UI traffic from test traffic (consequence of F1/F2)

| Option | Strongest argument |
|---|---|
| **A. 802.1Q VLAN** (`eth0` untagged = mgmt, `eth0.100` = traffic) | Zero hardware cost; clean L3 separation; DUT/IXP port can be a tagged trunk. |
| B. Untagged, separate subnet on the same link | Works even if the switch or DUT mishandles tags. |
| C. M.2 PCIe NIC per node (e.g. i226 2.5 GbE) | True physical separation of mgmt and traffic — the only option that literally meets "one port for UI, the other for simulation" per node. |

- **Evidence that changes the decision:** T04 fails (switch drops tagged frames) → B. Leak test M1 shows test data frames on the ETH1 side → put a managed switch/firewall on ETH1 or go to C.
- **Decision (2026‑10‑16): B — untagged, separate subnet** (`TRAFFIC_MODE=untagged`, the new default). A remains available in the installer for later.

### D2. Packet I/O engine (C++ data plane)

| Option | Strongest argument |
|---|---|
| **A. AF_PACKET TPACKET_V3 TX ring + `PACKET_QDISC_BYPASS`** | Works on any driver; 248 kpps per node is well within reach of one A76 core with batched rings. |
| B. AF_XDP | Lowest CPU per packet (zero‑copy if the driver supports it). |
| C. DPDK | Industry standard on x86, but no poll‑mode driver for RP1/macb → not viable here. |
| D. Plain kernel sockets (`sendmmsg`) | Simplest, but cannot forge arbitrary source subnets/AS paths without raw sockets. |

- **Evidence:** a micro‑benchmark showing >70 % per‑core load at 248 kpps with A → implement B behind the same interface.
- **Recommendation:** A behind an `IPacketSink` abstraction, so B can be added later without touching generators.

### D3. What "BGP traffic" means (needs your decision)

| Option | Strongest argument |
|---|---|
| A. **Stateful** — C++ BGP speakers emulate many peers/ASes, open real sessions to a DUT (BIRD/OpenBGPD route server) and pump UPDATEs. | Realistic: the DUT actually processes routes; ideal for IXP route‑server research. |
| B. **Stateless** — line‑rate TCP/179 segments carrying valid BGP OPEN/UPDATE/KEEPALIVE PDUs with configured AS paths and prefixes, no handshake. | The only way to produce 1 Gbit/s of BGP‑looking traffic (acceptance criterion); good for capture, IDS, sFlow/IPFIX, ACL tests. |
| C. Both, selectable in the UI. | Covers control‑plane and data‑plane experiments. |

- **Evidence:** if your experiments need the DUT to install routes → A is mandatory. If you only analyse traffic on the wire → B suffices.
- **Decision:** C — both, selectable in the UI. Build order: B first (it carries the 1 Gbit/s acceptance test), then A.

### D4. Control plane and web interface

| Option | Strongest argument |
|---|---|
| A. **C++ agents (data plane) + Python FastAPI controller + lightweight JS frontend** on CMNODE01 | UI iteration is fast and easy for students to extend; the controller is not performance‑critical. |
| B. All C++ (Drogon/Crow controller) | Single language and toolchain. |
| C. Go controller | Single static binary, good concurrency. |

- Agent↔controller: REST/JSON (header‑only `cpp-httplib`) for commands and a 1 s stats push — minimal dependencies.
- **Decision:** A — Python web interface/controller, C++ generator agents.

### D5. Cluster orchestration

| Option | Strongest argument |
|---|---|
| **A. Ansible + systemd** | No runtime CPU overhead (important for the 70 % budget); raw sockets and VLANs work natively. |
| B. k3s | Declarative rollout, but costs CPU on every node and pods would need `hostNetwork` + privileged anyway. |

- **Recommendation:** A (already implemented in `deploy/ansible`).

## 3. Overall recommendation and the single biggest risk

**Recommendation:** subnet‑separated, untagged traffic on the single onboard link (D1‑B), an AF_PACKET C++ agent on each node with one pinned TX worker per core (max 3 of 4 cores, 1 left for OS/IRQ/agent), a FastAPI controller + web UI on CMNODE01, and Ansible + systemd for deployment.

**Single biggest risk:** management and test traffic **share one physical link per node and one unmanaged switch** with no VLAN separation. Any generator mistake — unknown destination MAC, broadcast, or spoofed prefixes — is flooded to every port, including the ETH1 side where the web UI user sits, and onward if ETH1 is ever bridged to the faculty LAN.
Mitigations built into the plan: the generator will refuse to start without a resolved next‑hop MAC (the route server); it will never emit broadcast/multicast data; leak test M1 runs before any generator work; and ETH1 should stay on a laptop or isolated lab switch, never directly on the campus network.

## 4. Minimal implementation approach (phases)

| Phase | Content | Gate |
|---|---|---|
| P0 | Provisioning script, Ansible, connectivity test (**this delivery**) | You run the test plan and report PASS |
| P1 | Full specification (architecture, packet formats, API, UI controls) | You confirm each UI control |
| P2 | `tgen-agent` C++20: packet templates, incremental checksum rewrite, token‑bucket pacing, per‑core workers, stats | Single‑node benchmark ≤ 70 % per core |
| P3 | Controller + web UI (TCP/IP mode: subnets, protocols, rates) | 1 Gbit/s TCP/IP from 6 nodes |
| P4 | BGP stateless mode (AS numbers, prefixes, AS paths), then stateful sessions | 1 Gbit/s BGP |
| P5 | Hardening, tests, docs | Acceptance criteria |

**Parallelisation model:** the controller splits target rate and the address/prefix space into 6 equal shards (CPU is identical on all nodes). Each agent splits its shard over its pinned worker threads; every worker owns its own TX ring, so the hot path has no locks and no shared state.
