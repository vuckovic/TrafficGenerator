#!/usr/bin/env bash
# Runs tools/tgen_suite.py on the simulated cluster against two mock Cisco DUTs
# (tests/bgp/sim/duts-sim.toml): an IOS-XE router at .111 and a "1841" at .100.
# Usage: tests/bgp/sim/run_suite.sh [tgen_suite args, default: --duts sim-asr,sim-1841]
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"; repo="$(cd "$here/../../.." && pwd)"
export SIM_ROOT; SIM_ROOT="$(mktemp -d /tmp/tgen-sim-XXXX)"
sed -e 's/^MGMT_IFACE=.*/MGMT_IFACE="lo"/' -e 's/^SSH_USER=.*/SSH_USER="root"/' \
    "$repo/deploy/cluster.env.example" > "$SIM_ROOT/cluster.env"
ip addr replace 10.100.0.16/32 dev lo
export PATH="$here:$PATH" PYTHONPATH="$here/pymock${PYTHONPATH:+:$PYTHONPATH}" TGEN_DUTS_FILE="$here/duts-sim.toml"
export TGEN_DUT_PASSWORD_SIM_ASR=dummy TGEN_DUT_PASSWORD_SIM_1841=dummy
args=("$@"); [[ ${#args[@]} -gt 0 ]] || args=(--duts sim-asr,sim-1841 --pause 2)
python3 "$repo/tools/tgen_suite.py" --config "$SIM_ROOT/cluster.env" --extra="--bird-multihop --dut-poll 3 --converge-timeout 40 --withdraw-timeout 20" "${args[@]}"
