#!/usr/bin/env bash
# tools/tgen_series.py against the simulated cluster (see run_sim.sh).
# Usage: tests/bgp/sim/run_series.sh [series args] -- [tgen_p2 args]
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"; repo="$(cd "$here/../../.." && pwd)"
export SIM_ROOT; SIM_ROOT="$(mktemp -d /tmp/tgen-sim-XXXX)"
sed -e 's/^MGMT_IFACE=.*/MGMT_IFACE="lo"/' -e 's/^SSH_USER=.*/SSH_USER="root"/' \
    "$repo/deploy/cluster.env.example" > "$SIM_ROOT/cluster.env"
ip addr replace 10.100.0.16/32 dev lo
own=(); while [[ $# -gt 0 && "$1" != "--" ]]; do own+=("$1"); shift; done; [[ "${1:-}" == "--" ]] && shift
PATH="$here:$PATH" python3 "$repo/tools/tgen_series.py" "${own[@]}" -- --config "$SIM_ROOT/cluster.env" --bird-multihop "$@"
