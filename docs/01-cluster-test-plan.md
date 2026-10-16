# 01 — Cluster connectivity and operations test plan

Goal: prove that all six CM5 nodes are provisioned identically, can reach each other on both the management and traffic networks, and that the Super6C switch can carry 1 Gbit/s out of ETH2 — **before** any generator code is written.

## 1. Test topology

```
                 Super6C (RTL8370N, unmanaged)
   +---------------------------------------------------+
   | tgen1  tgen2  tgen3  tgen4  tgen5  tgen6           |
   |   |      |      |      |      |      |            |
   |  [p1]  [p2]   [p3]   [p4]   [p5]   [p6]  [ETH1] [ETH2]
   +-------------------------------------------|-------|---+
                                               |       |
   mgmt (untagged) 192.168.50.0/24             |       |  traffic VLAN 100, 10.100.0.0/24
   isolated lab switch / firewalled port ------+       +-- sink / DUT host 10.100.0.1
   + your laptop (browser, SSH)                             (iperf3 servers, later the DUT)
```

## 2. Prerequisites

Hardware
- 6× CM5 seated, with heatsinks; fans connected to the 12 V headers.
- Storage per node (eMMC, microSD or NVMe) flashed with **Raspberry Pi OS Lite 64‑bit (Bookworm)**.
- Use Raspberry Pi Imager's advanced options to set user `pi`, enable SSH with your public key, and set hostnames `tgen1`–`tgen6`.
- ETH1 → isolated management switch (not straight into the campus LAN — see the leak test M1).
- ETH2 → a sink PC (Linux) for tests T09 and M1.

Sink PC on ETH2 (for T09/M1)
```bash
sudo ip link add link <if> name <if>.100 type vlan id 100
sudo ip addr add 10.100.0.1/24 dev <if>.100 && sudo ip link set <if>.100 up
for p in 5201 5202 5203 5204 5205 5206; do iperf3 -s -D -p $p; done
```

## 3. Provisioning procedure

1. On your laptop: `git clone https://github.com/vuckovic/TrafficGenerator && cd TrafficGenerator/deploy`
2. `cp cluster.env.example cluster.env` and edit the IP plan. Set `SINK_IP=10.100.0.1` if the sink is connected.
3. Edit `ansible/inventory.ini`: set `ansible_host` to each node's **current** (DHCP) address.
4. `cd ansible && ./bootstrap-ssh.sh pi` (skip this if Imager already installed your key).
5. `ansible-playbook site.yml -e reboot=true` — installs all six nodes in parallel, reboots them onto their static IPs, and authorises tgen1's key on every node.
6. Update `inventory.ini` to the static IPs (`192.168.50.11`–`.16`).
7. On tgen1: `git clone …` then `./tests/cluster/connectivity_test.sh`

Manual alternative (no Ansible), on each node:
`sudo ./deploy/install-prereqs.sh --node-id N && sudo reboot`

## 4. Automated test cases (`tests/cluster/connectivity_test.sh`)

| ID | Test | Method | Pass criterion |
|---|---|---|---|
| T01 | SSH reachability | tgen1 → every node over mgmt | all 6 respond |
| T02 | Node configuration | OS, model, link, governor, traffic IP, throttling | 1000/full, `performance`, traffic IP present, CM5, `throttled=0x0` |
| T03 | Mgmt full mesh | `fping` 20 pkts from each node to the other 5 | 0 % loss on all 30 directed pairs |
| T04 | Traffic full mesh | same on VLAN 100 | 0 % loss → **proves RTL8370N passes 802.1Q tags** |
| T05 | MTU | `ping -M do -s 1472` on the ring | 1500‑byte frames pass unfragmented |
| T06 | Time sync | `chronyc tracking` | offset < 1 ms on every node |
| T07 | Per‑link throughput | iperf3 TCP N→N+1, one at a time | ≥ 900 Mbit/s each |
| T08 | Switch fabric | all 6 ring flows **concurrently** + per‑core CPU | each ≥ 850 Mbit/s (≈ 5.4 Gbit/s aggregate) |
| T09 | **1 Gbit/s out of ETH2** | 6 nodes × UDP 158 Mbit/s → sink, + per‑core CPU | aggregate ≥ 900 Mbit/s, loss < 0.5 % |
| T10 | Thermal | throttle flags and temperature after load | `0x0`, < 80 °C |

Options: `--quick` (5 s runs, skips T08) and `--only T03,T09`. The exit code equals the number of failures. Raw data goes to `~/tgen-test-results/<timestamp>/` (`summary.txt`, `nodes.txt`, `ping.txt`, `throughput.txt`, `cpu.txt`, JSON files).

Note: T08/T09 CPU numbers come from iperf3, so they are only a **baseline**. The 70 % criterion applies to our generator later.

## 5. Manual test cases

| ID | Test | Procedure | Pass criterion |
|---|---|---|---|
| M1 | **Leak test (safety)** | Laptop on the ETH1 side: `sudo tcpdump -eni <if> 'vlan 100 or net 10.100.0.0/24'` while running `--only T09` | no UDP data frames; at most a few ARP broadcasts |
| M2 | Power | Meter on the PSU input during T08 | below 80 % of the PSU rating; no node resets |
| M3 | Reboot persistence | Press the board reset, wait 2 min, run `--quick` | all PASS without manual steps |
| M4 | Node loss | Power‑cycle a single node mid‑test (or remove one CM5) | the remaining nodes still pass T03/T04 (no cluster‑wide dependency except chrony on tgen1) |
| M5 | Web UI port reachability | From the laptop: `curl -v http://192.168.50.11:8080` | TCP connection refused (nothing listening yet) — proves routing, not firewalling |

## 6. What to send back

Paste `summary.txt`, plus `cpu.txt` and `nodes.txt`, and the M1–M5 results, e.g.:

```
T01..T10: PASS=10 FAIL=0 SKIP=0
M1: 3 ARP frames, 0 data   M2: 71 W   M3: OK   M4: OK   M5: refused
```

On any FAIL, include the matching raw file. T04 failing switches us to `TRAFFIC_MODE=untagged`; an M1 failure means a hardware or network change before we proceed.
