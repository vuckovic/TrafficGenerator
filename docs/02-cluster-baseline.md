# 02 — Cluster baseline (accepted 2026‑10‑17)

Reference result of `tests/cluster/connectivity_test.sh` after provisioning.
Use it to compare later runs. A regression against these numbers means something changed in hardware or OS.

Setup: 6× CM5 (CMNODE01–04 16 GB, CMNODE05–06 8 GB), Raspberry Pi OS Trixie, kernel 6.18.50+rpt‑rpi‑2712, untagged traffic subnet 10.100.0.0/24, no route server yet (T09 stand‑in sink = CMNODE06).

| Test | Result | Value |
|---|---|---|
| T01 SSH | PASS | 6/6 |
| T02 Node config | PASS | 1000/full, **EEE off**, `performance`, traffic IP, CM5, not throttled |
| T03 Mgmt mesh | PASS | 30 pairs × 20 pings, 0 % loss |
| T04 Traffic mesh | PASS | 30 pairs × 20 pings, 0 % loss |
| T05 MTU | PASS | 1500 B DF |
| T06 Time sync | PASS | < 1 ms |
| T07 Per‑link TCP | PASS | ≥ 900 Mbit/s each pair |
| T08 Switch fabric | PASS | 6 concurrent flows, **5517 Mbit/s** aggregate; CPU node‑avg max 23 %, hottest core 77 % (iperf3) |
| T09 1 Gbit/s into one port | PASS | 5 senders → CMNODE06, **945 Mbit/s**, 0 % loss; receiver node‑avg CPU 66 % |
| T10 Thermal | PASS | no throttling, < 80 °C |
| T11 Route server | SKIP | not installed yet |
| T12 Link errors | PASS | no new FCS/symbol errors during run |

## Issues found and fixed on the way

| Symptom | Root cause | Fix |
|---|---|---|
| CMNODE02 had no traffic IP; T02/T04–T08 failed | installer hit dpkg lock (another apt‑get running) | installer waits for the lock (`DPkg::Lock::Timeout=900`), runs `dpkg --configure -a` |
| Occasional 2/20 ping loss from CMNODE01; FCS/symbol errors on CMNODE03/04/05 under light load | **Energy Efficient Ethernet** active on all nodes (new macb EEE support in kernel 6.18); errors on wake‑up from low‑power idle. `link_diag.sh` showed 0 errors under continuous load | tuning script now waits for carrier and disables EEE at boot and on every link‑up (NetworkManager dispatcher); T02 fails if EEE is on |
| Docker on CMNODE01 | user workload | stopped and disabled |

## Open notes
- CMNODE04 runs 1 Docker container (CPU budget / iptables). To be decided.
- T09 must be repeated against the real route server on ETH0 (all 6 nodes sending), plus T11.
