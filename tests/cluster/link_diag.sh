#!/usr/bin/env bash
# ============================================================================
# TrafficGenerator - link error localisation
# Sends UDP from every node to every other node, one pair at a time, and
# records how many FCS/symbol errors the RECEIVER counts for each pair.
#
#   row full of errors    -> that SOURCE node/slot transmits bad frames
#   column full of errors -> that TARGET node/slot receives badly
#   scattered             -> board-level noise (power, seating, switch)
#
# Run on CMNODE01:  ./link_diag.sh [--config FILE] [--secs 5] [--rate 900]
# Takes ~ (30 pairs x secs) + overhead  (default ~3.5 min).
# ============================================================================
# shellcheck disable=SC1091,SC2029
set -uo pipefail
export LC_ALL=C
CONFIG="${HOME}/TrafficGenerator/deploy/cluster.env"
SECS=5; RATE=900
while [[ $# -gt 0 ]]; do
  case "$1" in
    --config) CONFIG="$2"; shift 2 ;;
    --secs)   SECS="$2"; shift 2 ;;
    --rate)   RATE="$2"; shift 2 ;;
    -h|--help) sed -n '2,14p' "$0"; exit 0 ;;
    *) echo "unknown option $1"; exit 2 ;;
  esac
done
# shellcheck source=../../deploy/cluster.env.example
source "$CONFIG"
SSH_OPTS=(-o BatchMode=yes -o ConnectTimeout=5)
mgmt_ip()    { echo "${MGMT_PREFIX}.$((MGMT_HOST_BASE + $1))"; }
traffic_ip() { echo "${TRAFFIC_PREFIX}.$((TRAFFIC_HOST_BASE + $1))"; }
nname()      { echo "${NODE_HOSTNAMES[$(( $1 - 1 ))]}"; }
on()         { local n="$1"; shift; ssh "${SSH_OPTS[@]}" "${SSH_USER:-pi}@$(mgmt_ip "$n")" "$@" </dev/null; }
errs()       { on "$1" "sudo -n /usr/sbin/ethtool -S ${MGMT_IFACE} | awk -F: '/frame_check_sequence_errors|symbol_errors|rx_.*crc/ {s+=\$2} END{print s+0}'"; }
OUT="${HOME}/tgen-test-results/linkdiag-$(date +%Y%m%d-%H%M%S)"; mkdir -p "$OUT"

echo "== EEE (Energy Efficient Ethernet) status - should be disabled everywhere"
for n in $(seq 1 "$NODE_COUNT"); do
  st=$(on "$n" "sudo -n /usr/sbin/ethtool --show-eee ${MGMT_IFACE} 2>&1 | grep -iE 'EEE status|enabled|disabled|not supported' | head -1 | sed 's/^[[:space:]]*//'")
  printf '  %-9s %s\n' "$(nname "$n")" "${st:-unknown}" | tee -a "$OUT/eee.txt"
done

echo "== starting UDP receivers"
for n in $(seq 1 "$NODE_COUNT"); do
  on "$n" "pkill -x iperf3; iperf3 -s -D -B $(traffic_ip "$n") -p 5301"
done
sleep 1

declare -A M LOSS
echo "== ${SECS}s x ${RATE} Mbit/s UDP per directed pair (source -> target)"
for s in $(seq 1 "$NODE_COUNT"); do
  for t in $(seq 1 "$NODE_COUNT"); do
    (( s == t )) && continue
    b=$(errs "$t")
    lp=$(on "$s" "iperf3 -J -u -l 1400 -b ${RATE}M -t ${SECS} -c $(traffic_ip "$t") -p 5301 -B $(traffic_ip "$s")" \
         | jq -r '.end.sum.lost_percent // "?"' 2>/dev/null)
    a=$(errs "$t")
    M[$s,$t]=$(( ${a:-0} - ${b:-0} )); LOSS[$s,$t]="${lp:-?}"
    printf '  %s -> %s : rx errors +%s, udp loss %s%%\n' "$(nname "$s")" "$(nname "$t")" "${M[$s,$t]}" "${LOSS[$s,$t]}" >> "$OUT/pairs.txt"
    printf '.'
  done
done
echo
for n in $(seq 1 "$NODE_COUNT"); do on "$n" "pkill -x iperf3"; done

{
  echo
  echo "RX error increase on TARGET (column) while SOURCE (row) was sending:"
  printf '%-10s' "src\\dst"; for t in $(seq 1 "$NODE_COUNT"); do printf '%9s' "$(nname "$t" | sed 's/CMNODE/N/')"; done; printf '%9s\n' "rowsum"
  for s in $(seq 1 "$NODE_COUNT"); do
    printf '%-10s' "$(nname "$s")"; rs=0
    for t in $(seq 1 "$NODE_COUNT"); do
      if (( s == t )); then printf '%9s' "-"; else printf '%9s' "${M[$s,$t]}"; rs=$(( rs + M[$s,$t] )); fi
    done
    printf '%9s\n' "$rs"
  done
  printf '%-10s' "colsum"
  for t in $(seq 1 "$NODE_COUNT"); do cs=0; for s in $(seq 1 "$NODE_COUNT"); do (( s != t )) && cs=$(( cs + M[$s,$t] )); done; printf '%9s' "$cs"; done
  echo
} | tee "$OUT/matrix.txt"
echo "details: $OUT"
