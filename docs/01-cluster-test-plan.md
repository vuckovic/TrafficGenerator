# 01 — Cluster connectivity and operations test plan

Goal: prove that all six CM5 nodes are provisioned identically, can reach each other and the route server on both subnets, and that the Super6C switch can carry 1 Gbit/s to the route‑server port — **before** any generator code is written.

## 1. Test topology

The Super6C block diagram (`docs/assets/super6c-block-diagram.png`) shows that all six modules and **both** RJ45 jacks hang off one internal gigabit switch. The whole cluster is therefore one L2 segment. Management and test traffic are separated by IP subnet only (no VLAN tagging — decided 2026‑10‑16).

```
                 Super6C internal gigabit switch (unmanaged)
   +---------------------------------------------------------------------+
   | CMNODE01 CMNODE02 CMNODE03 CMNODE04 CMNODE05 CMNODE06               |
   |   .11      .12      .13      .14      .15      .16                  |
   |  [p1]     [p2]     [p3]     [p4]     [p5]     [p6]   [ETH0]  [ETH1] |
   +-------------------------------------------------------|-------|-----+
                                                           |       |
        route server  10.100.0.1/24  ----------------------+       +-- laptop / web UI
        (+ iperf3 servers for T09)                                     192.168.100.x/24

   one L2 segment:  mgmt 192.168.100.0/24  +  traffic 10.100.0.0/24 (untagged, eth0)
```

## 2. Prerequisites

Hardware
- 6× CM5 seated, with heatsinks; fans connected to the 12 V headers.
- Storage per node flashed with **Raspberry Pi OS Lite 64‑bit (Trixie)**.
- Existing node names and management addresses are **kept**: CMNODE01–CMNODE06 = 192.168.100.11–.16 (CMNODE01–04 16 GB, CMNODE05–06 8 GB). The installer never changes them (`MGMT_MODE=keep`); it only adds the traffic address 10.100.0.11–.16 to the existing connection, live, without dropping SSH.
- `SSH_USER` in `cluster.env` and `ansible_user` in `inventory.ini` must match your login user (default `pi`).
- **Internet access for the nodes during installation** (apt). See open question Q1 in `00-findings-and-approach.md`.
- ETH0 → route server host. ETH1 → your laptop (or a lab switch) for SSH and the web UI.

Route server host on ETH0 (for T09, T11 and M1), untagged:
```bash
sudo ip addr add 10.100.0.1/24 dev <if>
for p in 5201 5202 5203 5204 5205 5206; do iperf3 -s -D -p $p; done
```
The iperf3 servers are only needed during the test. The route‑server daemon (BIRD/OpenBGPD) is not required yet.

## 3. Provisioning procedure

1. On your laptop: `git clone https://github.com/vuckovic/TrafficGenerator && cd TrafficGenerator/deploy`
2. `cp cluster.env.example cluster.env`, check the IP plan, and set `ROUTE_SERVER_IP=10.100.0.1` and `SINK_IP=10.100.0.1`.
3. `ansible/inventory.ini` already lists CMNODE01–06 at 192.168.100.11–.16; only adjust `ansible_user` if needed.
4. `cd ansible && ./bootstrap-ssh.sh pi` (skip this if Imager already installed your key).
5. `ansible-playbook site.yml -e reboot=true` — installs all six nodes in parallel, reboots only nodes that need it (firmware config), and authorises CMNODE01's key on every node.
6. On CMNODE01: `git clone …` then `./tests/cluster/connectivity_test.sh`

Manual alternative (no Ansible), on each node:
`sudo ./deploy/install-prereqs.sh --node-id N && sudo reboot`

## 4. Automated test cases (`tests/cluster/connectivity_test.sh`)

| ID | Test | Method | Pass criterion |
|---|---|---|---|
| T01 | SSH reachability | CMNODE01 → every node over mgmt | all 6 respond |
| T02 | Node configuration | OS, model, link, governor, traffic IP, throttling | 1000/full, `performance`, traffic IP present, CM5, `throttled=0x0` |
| T03 | Mgmt full mesh | `fping` 20 pkts from each node to the other 5 | 0 % loss on all 30 directed pairs |
| T04 | Traffic full mesh | same, on the 10.100.0.0/24 addresses | 0 % loss |
| T05 | MTU | `ping -M do -s 1472` on the ring | 1500‑byte frames pass unfragmented |
| T06 | Time sync | `chronyc tracking` | offset < 1 ms on every node |
| T07 | Per‑link throughput | iperf3 TCP N→N+1, one at a time | ≥ 900 Mbit/s each |
| T08 | Switch fabric | all 6 ring flows **concurrently** + CPU | each ≥ 850 Mbit/s (≈ 5.4 Gbit/s aggregate) |
| T09 | **1 Gbit/s to the route‑server port (ETH0)** | 6 nodes × UDP 158 Mbit/s → `SINK_IP`, + CPU | aggregate ≥ 900 Mbit/s, loss < 0.5 % |
| T10 | Thermal | throttle flags and temperature after load | `0x0`, < 80 °C |
| T11 | Route server reachability | `fping` from every node, plus a TCP/179 probe (informational) | 0 % loss from all 6 |

CPU is reported as **node average** (the acceptance criterion: ≤ 70 %, ≤ 90 % accepted initially), with the hottest single core alongside for diagnosis. Per‑node detail is in `cpu.txt`.

Options: `--quick` (5 s runs, skips T08) and `--only T03,T09`. The exit code equals the number of failures. Raw data goes to `~/tgen-test-results/<timestamp>/`.

Note: T08/T09 CPU numbers come from iperf3, so they are only a **baseline**. The 70 % criterion applies to our generator later.

## 5. Manual test cases

| ID | Test | Procedure | Pass criterion |
|---|---|---|---|
| M1 | **Leak test (safety)** | On the laptop at ETH1: `sudo tcpdump -eni <if> 'net 10.100.0.0/24 and not arp'` while running `--only T09` | no (or only a handful of initial) UDP data frames — the switch has learned the route server's MAC on ETH0 |
| M2 | Power | Meter on the PSU input during T08 | below 80 % of the PSU rating; no node resets |
| M3 | Reboot persistence | Press the board reset, wait 2 min, run `--quick` | all PASS without manual steps |
| M4 | Node loss | Power‑cycle a single node mid‑test (or remove one CM5) | the remaining nodes still pass T03/T04 (no cluster‑wide dependency except chrony on CMNODE01) |
| M5 | Web UI port reachability | From the laptop: `curl -v http://192.168.100.11:8080` | TCP connection refused (nothing listening yet) — proves routing, not firewalling |

## 6. What to send back

Paste `summary.txt`, plus `cpu.txt` and `nodes.txt`, and the M1–M5 results, e.g.:

```
T01..T11: PASS=11 FAIL=0 SKIP=0
M1: 0 data frames   M2: 71 W   M3: OK   M4: OK   M5: refused
```

On any FAIL, include the matching raw file.
