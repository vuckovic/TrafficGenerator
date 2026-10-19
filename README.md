# TrafficGenerator

Distributed BGP and TCP/IP traffic generator for IXP research, running on a
6‑node Raspberry Pi CM5 cluster (DeskPi Super6C).

**Status:** cluster validated (`docs/02-cluster-baseline.md`), specification frozen v1.0, UI design locked; **P2 (BGP router emulator) built and tested locally** — `docs/05-p2-bgp-emulator.md`.

| Path | Purpose |
|---|---|
| `docs/00-findings-and-approach.md` | Hardware findings, design options, recommendation, risks |
| `docs/01-cluster-test-plan.md` | Provisioning procedure and connectivity/operations tests |
| `docs/02-cluster-baseline.md` | Accepted cluster test result (reference) |
| `docs/03-specification.md` | Full specification (draft, open decisions D1–D8) |
| `docs/04-ui-controls.md` | Web interface controls (frozen) |
| `docs/05-p2-bgp-emulator.md` | P2 runbook and results |
| `docs/design/` | Locked UI design v1 |
| `agent/` | C++20 BGP router emulator `tgen-agent` + unit tests |
| `tools/tgen_p2.py`, `tools/tgen/` | Cluster benchmark run, planner, analysis |
| `tests/bgp/` | Local BIRD test and 6-node cluster simulation |
| `deploy/controller-bootstrap.sh` | Run on CMNODE01: control tools, SSH keys to all nodes, node checks, `--install`, `--test` |
| `deploy/install-prereqs.sh` | Idempotent per‑node installer (Raspberry Pi OS Trixie 64‑bit; Bookworm also supported) |
| `deploy/cluster.env.example` | Cluster IP plan and tuning parameters |
| `deploy/ansible/` | Parallel provisioning of all 6 nodes |
| `tests/cluster/connectivity_test.sh` | Automated test suite T01–T12; `link_diag.sh` link error matrix |

Quick start (on CMNODE01):

```bash
git clone https://github.com/vuckovic/TrafficGenerator.git ~/TrafficGenerator
bash ~/TrafficGenerator/deploy/controller-bootstrap.sh            # prepare + check
bash ~/TrafficGenerator/deploy/controller-bootstrap.sh --install --test
```
