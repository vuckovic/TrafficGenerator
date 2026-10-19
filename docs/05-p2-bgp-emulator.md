# 05 — P2: BGP router emulator (tgen-agent) — runbook and results

Milestone P2 (spec §14): the C++ BGP router emulator, measured against BIRD 2 as a stand‑in DUT.

## What is built

| Part | Path | Notes |
|---|---|---|
| BGP encoder/decoder | `agent/include/tgen/bgp.hpp`, `agent/src/bgp.cpp` | OPEN (4‑octet AS, MP‑BGP IPv4, route refresh), UPDATE (ORIGIN, AS_PATH, AS4_PATH merge, NEXT_HOP, MED, LOCAL_PREF, COMMUNITIES, LARGE_COMMUNITY, ORIGINATOR_ID/CLUSTER_LIST decode), KEEPALIVE, NOTIFICATION; packing to 4096 B; stream parser |
| Router emulator | `agent/src/router.cpp` | RFC 4271 FSM (active side), hold/keepalive, max‑rate or paced announce, withdraw phase, re‑announce after a reset, receive store, sampled send/arrival timestamps, End‑of‑RIB, per‑role correctness counters |
| Agent | `agent/src/main.cpp` | runs N routers on 1–3 pinned threads; absolute timeline from the plan; watchdog; JSON result |
| Unit tests | `agent/tests/` | 16 tests incl. a hand‑computed UPDATE byte vector, fragmentation, AS_TRANS/AS4_PATH |
| Planner, analysis | `tools/tgen/` | prefix/ASN generator (seeded), RAM‑weighted split (CMNODE01 0.7), BIRD config, metrics of spec §6.1, P2 gate |
| Cluster run | `tools/tgen_p2.py` | build, deploy, router addresses, BIRD on CMNODE06, synchronized start, CPU sampling, collect, analyse, gate, cleanup |
| Local test | `tests/bgp/local_bird_test.py` | one box, BIRD 2 on loopback |
| Cluster simulation | `tests/bgp/sim/run_sim.sh` | runs `tgen_p2.py` unchanged against 6 simulated nodes on one box |
| systemd units | `deploy/systemd/` | P3 daemon units; CMNODE01 limits drop‑in (spec §10.2) |

## Run it on the cluster (CMNODE01)

```bash
cd ~/TrafficGenerator && git pull
sudo apt-get install -y libasio-dev libcpp-httplib-dev   # or: bash deploy/controller-bootstrap.sh --install
tools/tgen_p2.py --routers 12 --prefixes 1000 --converge-timeout 60      # quick check (~2 min)
tools/tgen_p2.py                                                          # P2 gate: 60 x 10 000 (~8 min)
```

Options: `--role route_reflector|router`, `--rate N` (prefixes/s per router, paced), `--per-update N`, `--nodes CMNODE01,CMNODE02`, `--dut-node CMNODE06`, `--seed N`, `--converge-timeout S`, `--withdraw-timeout S`.

**Layout used:**
- BIRD 2 runs on **CMNODE06** at 10.100.0.16, AS 64500, as `rs client` for every emulated router.
- The **60 routers run on CMNODE01–05**, split 9/13/13/13/12 (CMNODE01 at 0.7 weight), using addresses 10.100.0.20 and up as /32s on eth0, removed afterwards.
- On CMNODE01 the agent runs in a cgroup scope: `AllowedCPUs=1-3`, `CPUQuota=280%`, `MemoryMax=11200M`.

**Timeline:**
1. start +15 s: sessions open;
2. +20 s: all routers announce;
3. after the convergence timeout: all withdraw;
4. after the withdraw timeout: Cease and close.

**Output:** `~/tgen-test-results/p2-<timestamp>/` contains:
- `summary.txt` (gate) and `report.json` (all metrics);
- `result-<node>.json`, `agent-<node>.log`, `cpu-<node>.txt`;
- `bird.conf`, `bird.log` (also sample input for the P5 log parser), `bird-mid.txt`.

## P2 gate (spec §14)

| Check | Pass when |
|---|---|
| Sessions | 60/60 Established |
| Initial convergence | every receiver holds every expected route (59 × 10 000 = 590 000 each) |
| Correctness | 0 missing, 0 extra; per role: route server adds no AS, keeps the next hop; reflector adds ORIGINATOR_ID |
| Stability | 0 unexpected session resets |
| Generator validity | send lag p99 < 10 ms (paced mode) |
| CPU | node‑average p95 ≤ 70 % on every generator node |

## Results so far (development machine, x86, 2 cores, BIRD 2.14 on loopback)

| Run | Sessions | Initial convergence | Announce latency p50 / p99 | Withdraw convergence | BIRD output | Agent |
|---|---|---|---|---|---|---|
| 60 × 10 000, route server, max rate | 60/60 in 2.7 s | **28.6 s** (35.4 M routes) | 13.9 s / 28.2 s | 19.2 s | 1.24 M prefixes/s | 34 s CPU, 1.46 GB (all 60 routers in one process) |
| 12 × 5 000, route server | 12/12 | 0.34 s | — / 330 ms | — | 1.96 M prefixes/s | |
| 6 × 2 000, paced 1 000/s per router | 6/6 | 1.7 s | — | — | — | send lag p99 **1.4 ms** |
| 6 × 2 000, route reflector | 6/6 | 0.06 s | — | — | — | ORIGINATOR_ID on 100 % |
| 6 × 2 000, router (next‑hop‑self) | 6/6 | 0.04 s | — | — | — | NH = DUT, DUT AS prepended on 100 % |
| `tgen_p2.py` simulated 6‑node cluster, 12 × 2 000 | 12/12 | 0.2 s | 97 ms / 181 ms | 0.1 s | 1.5 M prefixes/s | gate PASS |

With every router sending at maximum rate at once, the latency figures are BIRD's queueing under a burst of 600 000 prefixes. That is a DUT property, which is exactly what the benchmark should expose. The real numbers come from the CM5 cluster.

## Known limits (P2)
- IPv4 only (D3). Receive mode `full` behaves like `timestamps`: full attribute storage and the exact diff come with the P4 analysis.
- Our routers do not answer ROUTE‑REFRESH (the DUT rarely asks; the Adj‑RIB‑Out is static).
- The agent runs per benchmark (CLI). The daemon with an HTTP API for the controller comes in P3.
