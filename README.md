# TrafficGenerator

Distributed BGP and TCP/IP traffic generator for IXP research, running on a
6‑node Raspberry Pi CM5 cluster (DeskPi Super6C).

**Status:** Stage 1 — provisioning and cluster validation. Generator code
starts after the cluster test plan passes and the specification is approved.

| Path | Purpose |
|---|---|
| `docs/00-findings-and-approach.md` | Hardware findings, design options, recommendation, risks |
| `docs/01-cluster-test-plan.md` | Provisioning procedure and connectivity/operations tests |
| `deploy/install-prereqs.sh` | Idempotent per‑node installer (Raspberry Pi OS Bookworm 64‑bit) |
| `deploy/cluster.env.example` | Cluster IP plan and tuning parameters |
| `deploy/ansible/` | Parallel provisioning of all 6 nodes |
| `tests/cluster/connectivity_test.sh` | Automated test suite T01–T10 |

Quick start: see `docs/01-cluster-test-plan.md` §3.
