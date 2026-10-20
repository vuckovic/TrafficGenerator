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

## External DUT: MikroTik, Cisco, BIRD, Quagga/FRR

`tgen_p2.py --dut <platform>` configures a real device over SSH before the run and cleans it up afterwards. `--dut bird`, the default, is still the managed BIRD stand‑in on CMNODE06.

| `--dut` | Platform | Transport | What is deployed | Default user |
|---|---|---|---|---|
| `mikrotik` | RouterOS 7 | ssh + scp + `/import` | one `/routing bgp connection` per router, comment `tgen`; plus a `/system logging` rule added by a separate command after the import (topics `"bgp,!packet,!raw"`, i.e. including debug, because RouterOS 7.11 logs BGP session changes only at debug level; falling back to `bgp`; tagged with `prefix=tgen`, because logging rules have no `comment` in 7.11; a refused rule is only a warning), because RouterOS 7 does not log BGP by default | admin |
| `cisco` | IOS 15.x, IOS‑XE 16.x/17.x (2015 and later) | Netmiko (`python3-netmiko`) | `router bgp` neighbours with description `tgen …`, **running‑config only** | admin |
| `bird-ssh` | BIRD 2/3 on any Linux host | ssh + scp, `birdc configure` | protocols `tgen_R..` in their own include file (default `/etc/bird/tgen.conf`) | root |
| `frr` / `quagga` | FRRouting, Quagga | ssh, `vtysh -c …` | neighbours with description `tgen …` + route‑map `TGEN-ALL` (permit) in/out | root |

```bash
export TGEN_DUT_PASSWORD='…'        # or leave unset: asked interactively; SSH keys work too (--dut-key)
export TGEN_DUT_ENABLE='…'          # Cisco only, if the user is not privilege 15
tools/tgen_p2.py --dut mikrotik --dut-ip 10.100.0.111 --routers 12 --prefixes 100 --converge-timeout 60
tools/tgen_p2.py --dut cisco    --dut-ip 10.100.0.1 --dut-ssh-host 192.168.100.1 --routers 12 --prefixes 1000
tools/tgen_p2.py --dut frr      --dut-ip 10.100.0.30 --role router
tools/tgen_dut.py --dut cisco --dut-ip 10.100.0.1 --routers 12 preview     # show the config, change nothing
tools/tgen_dut.py --dut cisco --dut-ip 10.100.0.1 test|push|status|remove
```

**Options:**

| Option | Purpose |
|---|---|
| `--dut-ssh-host`, `--dut-port` | management address and SSH port, if different from the peering address |
| `--dut-user`, `--dut-key` | SSH user and private key |
| `--dut-no-sudo` | run `birdc` / `vtysh` without sudo (otherwise root or passwordless sudo is needed) |
| `--dut-bird-include`, `--dut-bird-conf`, `--dut-bird-socket` | BIRD include file, main config file and control socket |
| `--dut-max-routes` | route‑limit guard; default 20 000 for MikroTik, none for the others |
| `--keep-dut-config` | leave the neighbours on the device after the run |
| `--dut-poll N` | read the DUT's BGP session list every N s during the run into `dut-sessions.jsonl` (default 10, 0 = off). This is the DUT's own view of each session, for the event timeline; each poll is an SSH login on the DUT, cut off after 15 s, and polling stops after 2 failures in a row |
| `--role` | default **router** for `mikrotik` (RouterOS cannot be a transparent route server), `route_server` otherwise |

**What the tool does:**

1. **Before the run:**
   - reads the device's facts (platform, version, CPU, memory) and refuses unsupported versions (RouterOS 6, IOS before 15, IOS‑XR, NX‑OS);
   - applies the route‑limit guard;
   - removes tgen leftovers of an earlier run;
   - pushes the configuration (saved as `dut-config.<rsc|ios|conf|vtysh>`) and checks the neighbour count.
2. **During the run:** `dut-sessions-mid.txt`, `dut-resources-mid.json`.
3. **After the run:** `dut-bgp.log` (P5 parser input), `dut-resources-end.json`, then removes the tgen neighbours.

**Role mapping:**

| Role | MikroTik | Cisco | BIRD | FRR / Quagga |
|---|---|---|---|---|
| route_server | `local.role=ebgp`, NH propagate (**adds its AS**) | `route-server-client` | `rs client` | `route-server-client` |
| route_reflector | `local.role=ibgp-rr` | `route-reflector-client` | `rr client` | `route-reflector-client` |
| router | `nexthop-choice=force-self` | `next-hop-self` | `next hop self` | `next-hop-self` |

**Loop detection:** the emulated routers drop routes that carry their own AS (eBGP) or their own ORIGINATOR_ID (iBGP), like a real router does. These are counted as `looped_rx` in `report.json`. FRR sends every route back to its originator, so this is normal there: one route back per announced prefix.

**Verified in the simulation, all gate checks passing, cleanup checked:**

| DUT | route_server | router | route_reflector |
|---|---|---|---|
| MikroTik (mock RouterOS 7 → BIRD) | 12/12 PASS | — | — |
| Cisco (mock Netmiko + IOS‑XE → BIRD) | 6/6 PASS, transparent | 6/6 PASS, DUT AS + NH self | 6/6 PASS |
| BIRD 2.14, real daemon via `bird-ssh` | 12/12 PASS | 6/6 PASS | 6/6 PASS |
| FRR 8.4.4, real bgpd in its own netns | 12/12 PASS, transparent, 3 600 looped back | 6/6 PASS | 6/6 PASS |

Commands: `tests/bgp/sim/run_sim.sh --dut mikrotik …` and `tests/bgp/sim/run_dut.sh bird-ssh|frr|cisco …`.

Not yet verified on real hardware:
- the CRS106 at 10.100.0.111;
- any Cisco device. The Cisco test runs against a mock; real IOS prompts and timing are handled by Netmiko.

## Rate series: finding the knee (`tools/tgen_series.py`)

```bash
tools/tgen_series.py --rates 2,4,6,8,10,12 -- --dut mikrotik --routers 12 --prefixes 100 --withdraw-timeout 30
```

**What it does:**
- Runs `tgen_p2.py` once per paced rate (prefixes/s per router). Everything after `--` goes to every run.
- Asks for the DUT password once, for the whole series.
- Sets each run's convergence timeout to prefixes ÷ rate × 1.5 + 60 s, unless you give one.
- Stops one run after the knee (`--stop-after-knee`).
- Withdrawals are paced too, so it raises `--withdraw-timeout` when a rate needs more (prefixes ÷ rate × 1.3 + 20 s).

**Knee rule:** the DUT falls behind, i.e. its output is below 0.9 × offered deliveries (`--knee-ratio`), or receivers are still incomplete more than 5 s after our last send (`--knee-backlog-s`). Offered deliveries = rate × routers × (routers − 1). Announce p99 above 1 s (`--slo-p99-ms`) is reported as "latency OK: no" but is **not** a knee: a high but stable latency is not falling behind. (The first version used the p99 limit as the knee and stopped too early on the CRS106.)

**Output:** `~/tgen-test-results/series-<timestamp>/series.md|csv|json`. Each run keeps its own `p2-*` folder.

**Columns:**
- offered prefixes/s, offered deliveries/s, DUT output /s;
- convergence: from the first announcement, and **after the last send** (the DUT's lag);
- announce latency p50 / p99, our send lag p99, DUT CPU mid‑run, missing routes, resets, gate.

**Pacing:** works per UPDATE message. Without `--per-update`, a paced run packs **10 prefixes per UPDATE** (user decision 2026‑10‑20; real routers pack many prefixes per message). Before the pacing fix, all of a router's prefixes went out in one UPDATE, so "paced" runs were not paced. Use `--per-update 1` for the hardest case.

**Unexpected attributes:** the agent records every path‑attribute type it receives (`attrs_rx`) and every type our routers send (`attrs_sent`). The report flags types the DUT added beyond the role: a route server and a router may add nothing new; a reflector may add LOCAL_PREF, ORIGINATOR_ID and CLUSTER_LIST. This is PASS/FAIL for the BIRD stand‑in and INFO for a real DUT. Example: CRS106 reflector → `ATOMIC_AGGREGATE(6)` on every route. FRR 8.4 as reflector in our configuration advertises MED 0 to the clients (`Metric 0` in `advertised-routes`, none on the stored path); possibly caused by our outbound route‑map.

## P2 gate (spec §14)

| Check | Pass when |
|---|---|
| Sessions | 60/60 Established |
| Initial convergence | every receiver holds every expected route (59 × 10 000 = 590 000 each) |
| Correctness | 0 missing, 0 extra; per role: route server adds no AS, keeps the next hop; reflector adds ORIGINATOR_ID |
| Stability | 0 unexpected session resets |
| Generator validity | send lag p99 < 10 ms (paced mode) |
| CPU | node‑average p95 ≤ 70 % on every generator node |

## Results on the cluster (CM5, real hardware)

### P2 gate on the cluster: **PASSED** (2026‑10‑20, `p2-20261020-233819`)

60 routers × 10 000 prefixes against the BIRD 2 stand‑in on CMNODE06. Routers on CMNODE01–05 (CMNODE01 at 0.7 weight).

| Check | Result |
|---|---|
| Sessions | **60/60 Established**, setup p90 11.2 s, 0 resets |
| Initial convergence | **18.8 s**: all 60 receivers hold 590 000 routes each (35.4 M routes) |
| Correctness | 0 missing, 0 extra, 0 attribute violations, nothing added beyond the role |
| Announce latency p50 / p99 | 9.4 s / 18.6 s (n = 349 103): BIRD's queue under a 600 k burst |
| Withdraw convergence | 9.2 s (latency p50 / p99 4.6 s / 9.1 s) |
| DUT output | **1.88 M prefixes/s** (BIRD on one CM5) |
| Generator CPU (node average p95) | CMNODE01 2 %, CMNODE02 4 %, CMNODE03–05 2 %. Large headroom. CMNODE06 (BIRD) p95 25 % |

**Compared with the development machine** (x86, everything on one box): convergence is faster on the cluster (18.8 vs 28.6 s), because BIRD has its own node.

**Open observation:** session setup p90 is 11.2 s (2.7 s on the development machine). Likely BIRD bringing 60 passive protocols up on a CM5, or our connect retry after early refusals. It does not affect the gate. To look at in P3, where sessions start in the Establish phase.


| Date | Run | DUT | Sessions | Initial convergence | Announce latency p50 / p99 | Withdraw convergence | DUT output | Gate |
|---|---|---|---|---|---|---|---|---|
| 2026‑10‑20 | `p2-20261020-194352`: 12 × 100, route server, max rate | MikroTik CRS106‑1C‑5S, RouterOS 7.11 | 12/12, setup p90 2.4 s | 13.5 s | 6.8 s / 13.5 s | 2.4 s | **1 062 prefixes/s** | PASS |
| 2026‑10‑20 | `p2-20261020-200115`: same, repeat (DUT CPU 100 % mid‑run) | MikroTik CRS106‑1C‑5S, RouterOS 7.11.2 | 12/12, setup p90 2.3 s | 13.4 s | 11.4 s / 13.4 s | 2.4 s | **1 070 prefixes/s** | PASS |
| 2026‑10‑20 | `p2-20261020-200829`: same, with BGP logging on the DUT | MikroTik CRS106‑1C‑5S, RouterOS 7.11.2 | 12/12, setup p90 1.8 s | 13.3 s | 9.1 s / 13.3 s | 2.3 s | **1 075 prefixes/s** | PASS |
| 2026‑10‑20 | `p2-20261020-202630`: same, BGP debug logging on the DUT | MikroTik CRS106‑1C‑5S, RouterOS 7.11.2 | 12/12, setup p90 2.5 s | 13.6 s | 8.9 s / 11.5 s | 2.4 s | **1 053 prefixes/s** (logging cost ≤ 2 %) | PASS |
| 2026‑10‑20 | `p2-20261020-214235`: 12 × 100, **router** (new default), polling 10 s | MikroTik CRS106‑1C‑5S, RouterOS 7.11.2 | 12/12, setup p90 2.7 s | 14.7 s | 11.9 s / 14.3 s | 2.4 s | **966 prefixes/s** | PASS; AS + next‑hop‑self on 13 200/13 200 ✔ |
| 2026‑10‑20 | `p2-20261020-214711`: 12 × 100, **route reflector** | MikroTik CRS106‑1C‑5S, RouterOS 7.11.2 | 12/12, setup p90 2.7 s | 14.9 s | 7.4 s / 14.8 s | 2.4 s | **957 prefixes/s** | PASS; **no ORIGINATOR_ID on 13 200/13 200** (INFO) |
| 2026‑10‑20 | `p2-20261020-220302`: route reflector, repeat (DUT view: 18 polls, 0 failed; sessions up 22:03:32–35) | MikroTik CRS106‑1C‑5S, RouterOS 7.11.2 | 12/12, setup p90 2.7 s | 15.3 s | 11.9 s / 14.6 s | 2.6 s | **933 prefixes/s** | PASS; no ORIGINATOR_ID on 13 200/13 200 (INFO), repeatable |
| 2026‑10‑20 | `p2-20261020-221105`: route reflector, with packet capture on CMNODE02 | MikroTik CRS106‑1C‑5S, RouterOS 7.11.2 | 12/12, setup p90 2.7 s | 15.0 s | 8.5 s / 14.9 s | 2.5 s | **970 prefixes/s** | PASS; capture confirms the missing ORIGINATOR_ID |

**Reading:**
- **The CRS106 is the bottleneck.** 13 200 route deliveries (1 200 prefixes × 11 receivers) at about 1 060/s matches the 13.5 s convergence.
- **The latency is queueing.** All routers sent at once, so prefixes waited in the DUT's queue.
- **Generator CPU was about 0 %.** The cluster was not a factor.
- **Repeatable:** three runs within 1 % (1 062 / 1 070 / 1 075 prefixes/s; convergence 13.5 / 13.4 / 13.3 s). The DUT's CPU was still at 43–65 % a few minutes after the previous run.
- **RouterOS is not a transparent route server.** In the `route_server` role it added its AS (64500) to **all 13 200** delivered routes and kept the next hop (0 changed). RouterOS 7 has no route‑server mode and no filter that removes its own AS, so this is a property of the device. The gate reports it as INFO for real DUTs.
- **DUT clock:** synced by NTP from CMNODE01 since 2026‑10‑20 20:50: status `synchronized`, offset 23.5 ms at first sync. RouterOS logs with 1 s resolution. Before that the clock was unsynchronised (its log showed `01-02 00:40` / `06:11` while CMNODE01 showed 20:26).
- **Where the delay is (all in the DUT):**
  - session setup, about 2.5 s: connections are accepted one after another (its own log: 12 × OpenConfirm over 2 s);
  - distribution, about 13 s: 13 200 deliveries at about 1 070/s on one core at 100 % CPU, so prefixes queue;
  - withdrawals, 2.3 s: no attributes to process.

  Generator send lag and node CPU were negligible.
- **BGP log on RouterOS 7.11:** only "Entering OpenConfirm" (topics `route,bgp,debug,state`); sample in `tests/fixtures/dut-logs/`. Up/down times come from `--dut-poll`.
- **Decision (user, 2026‑10‑20):** the MikroTik is tested as a **router** by default. No idle wait before runs: the point is to load the router.
- **Router role:** correct (AS added and next hop = DUT on every route). It is about 10 % slower than in the route‑server role (966 vs about 1 060 prefixes/s): rewriting the next hop costs work, and polling may add a little.
- **Route‑reflector role, confirmed by a packet capture:** every route was reflected (0 missing), but RouterOS 7.11.2 adds **neither ORIGINATOR_ID (type 9) nor CLUSTER_LIST (type 10)**, both of which RFC 4456 §8 requires. It also adds **ATOMIC_AGGREGATE (type 6)** to every reflected route, without aggregating.
  - Capture: `tcpdump` on CMNODE02, all BGP traffic with 10.100.0.111.
  - UPDATEs **sent by the DUT** (`ip.src == 10.100.0.111`): 102 with attributes, each carrying types 1, 2, 3, 5 **and 6**, none carrying 9 or 10; 6 without attributes (withdrawals / End‑of‑RIB). Our own 2 announcements carry 1, 2, 3, 5 only.
  - Command: `tshark -r rr.pcap -Y "bgp.type == 2" -T fields -e bgp.update.path_attribute.type_code | tr "," "\n" | sort -n | uniq -c`.
- **DUT view:** `dut-sessions.jsonl` gives per‑session `E` flag, `last-started` and `prefix-count` (format: `tests/fixtures/dut-sessions/`). Summarised as `dut_view` in `report.json` and in the summary.
- **First rate series** (`series-20261020-224843`, router role, 12 × 100, **1 prefix per UPDATE**):

  | rate /router | offered deliveries/s | DUT output /s | convergence | after last send | p50 / p99 | DUT CPU |
  |---|---|---|---|---|---|---|
  | 2 | 264 | 264 | 50.1 s | 0.6 s | 611 / 943 ms | 100 % |
  | 4 | 528 | 516 (98 %) | 25.7 s | 0.9 s | 1 430 / 1 967 ms | 100 % |

  - At 4/s it kept up (98 % delivered, 0.9 s after the last send). The series stopped there only because of the old p99 rule.
  - *(Corrected below: 264/s was what we offered, not a limit. The router's CPU reads 100 % mid‑run in every run, so it does not show saturation.)*
  - Withdrawals at 2/s did not finish within `--withdraw-timeout 30` (100 prefixes take 50 s), so withdraw convergence was n/a. The series now raises the timeout itself.
- **Knee of the CRS106, router role, 12 routers** (`series-20261020-230313`, `series-20261020-231033`):

  | prefixes per UPDATE | keeps up at | falls behind at | delivered when overloaded |
  |---|---|---|---|
  | 1 | 528 deliveries/s (4/s per router): 98 %, 1.0 s after the last send | 792/s (6/s): 87 %, backlog 2.9 s | **686/s** |
  | 10 | 528/s (4/s): 100 %, 1.8 s | 1 056/s (8/s): 83 %, backlog 6.7 s | **872/s** |
  | about 100 (max‑rate runs) | — | — | **about 1 060/s** |

  - **Most of the cost is per route.** Packing more prefixes per UPDATE raises the ceiling by about 25 % (1 → 10) and 20 % more (10 → about 100).
  - The earlier reading "the limit is per message" was wrong: 264/s at 2/s was what we offered, not a ceiling.
  - **DUT CPU reads 100 % mid‑run in every run**, even when the router keeps up easily. RouterOS averages over the last second, measured inside our SSH login, so CPU does not show saturation; delivered rate and backlog do.
- **Next:** narrow the knees (1 prefix: 4.5–5.5/s; 10 prefixes: 5–7/s).

**Syncing the DUT clock with CMNODE01 (NTP):**
```bash
# on CMNODE01
echo "allow 10.100.0.0/24" | sudo tee /etc/chrony/conf.d/tgen-dut.conf && sudo systemctl restart chrony
# MikroTik (RouterOS 7)
ssh admin@10.100.0.111 '/system clock set time-zone-name=Europe/Belgrade'
ssh admin@10.100.0.111 '/system ntp client set enabled=yes mode=unicast servers=10.100.0.11'
ssh admin@10.100.0.111 '/system ntp client print'      # status: synchronized
sudo chronyc clients | grep 10.100.0.111               # on CMNODE01
```

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
