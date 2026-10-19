#!/usr/bin/env bash
# Simulates the 6-node cluster on one Linux box (root, BIRD 2, mpstat) to test
# tools/tgen_p2.py end to end. Usage: tests/bgp/sim/run_sim.sh [tgen_p2 args...]
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"; repo="$(cd "$here/../../.." && pwd)"
export SIM_ROOT; SIM_ROOT="$(mktemp -d /tmp/tgen-sim-XXXX)"
sed -e 's/^MGMT_IFACE=.*/MGMT_IFACE="lo"/' -e 's/^SSH_USER=.*/SSH_USER="root"/' \
    "$repo/deploy/cluster.env.example" > "$SIM_ROOT/cluster.env"
ip addr replace 10.100.0.16/32 dev lo
PATH="$here:$PATH" python3 "$repo/tools/tgen_p2.py" --config "$SIM_ROOT/cluster.env" --bird-multihop "$@"
