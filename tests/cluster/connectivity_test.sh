#!/usr/bin/env bash
# ============================================================================
# TrafficGenerator - cluster connectivity & operations test
# Run on the controller node (CMNODE01) as SSH_USER after install + reboot:
#   ./connectivity_test.sh [--config /etc/tgen/cluster.env] [--quick] [--only T03,T07]
# Exit code = number of failed tests. Results in ~/tgen-test-results/<ts>/
# ============================================================================
# shellcheck disable=SC2015,SC2029,SC1091  # A&&B||C safe: result() always returns 0
set -uo pipefail
export LC_ALL=C

CONFIG="/etc/tgen/cluster.env"
QUICK=0
ONLY=""
DUR=10                      # iperf seconds
PASS=0; FAIL=0; SKIP=0
SSH_OPTS=(-o BatchMode=yes -o ConnectTimeout=5 -o StrictHostKeyChecking=accept-new)

while [[ $# -gt 0 ]]; do
  case "$1" in
    --config) CONFIG="$2"; shift 2 ;;
    --quick)  QUICK=1; DUR=5; shift ;;
    --only)   ONLY="$2"; shift 2 ;;
    -h|--help) sed -n '2,8p' "$0"; exit 0 ;;
    *) echo "unknown option $1"; exit 2 ;;
  esac
done
[[ -f "$CONFIG" ]] || { echo "config not found: $CONFIG"; exit 2; }
# shellcheck source=../../deploy/cluster.env.example
source "$CONFIG"

OUT="${HOME}/tgen-test-results/$(date +%Y%m%d-%H%M%S)"
mkdir -p "$OUT"
SUMMARY="$OUT/summary.txt"

mgmt_ip()    { echo "${MGMT_PREFIX}.$((MGMT_HOST_BASE + $1))"; }
traffic_ip() { echo "${TRAFFIC_PREFIX}.$((TRAFFIC_HOST_BASE + $1))"; }
tif()        { [[ "$TRAFFIC_MODE" == vlan ]] && echo "${MGMT_IFACE}.${TRAFFIC_VLAN}" || echo "$MGMT_IFACE"; }
on()         { local n="$1"; shift; ssh "${SSH_OPTS[@]}" "${SSH_USER:-pi}@$(mgmt_ip "$n")" "$@"; }
nname()      { echo "${NODE_HOSTNAMES[$(( $1 - 1 ))]:-node$1}"; }
nodes()      { seq 1 "$NODE_COUNT"; }
next_node()  { echo $(( $1 % NODE_COUNT + 1 )); }

want() { [[ -z "$ONLY" || ",$ONLY," == *",$1,"* ]]; }
result() {   # id status message
  local id="$1" st="$2" msg="$3"
  case "$st" in PASS) ((PASS++)) ;; FAIL) ((FAIL++)) ;; *) ((SKIP++)) ;; esac
  printf '%-4s %-5s %s\n' "$id" "$st" "$msg" | tee -a "$SUMMARY"
  return 0
}

# --------------------------------------------------- CPU sampling helpers ---
cpu_start() {  # seconds
  local n; for n in $(nodes); do
    on "$n" "nohup mpstat -P ALL 1 $1 > /tmp/tgen_mpstat.txt 2>&1 &" </dev/null
  done
}
cpu_collect() {  # label -> prints "<worst node avg %> <node> <worst single core %>"
  # Acceptance criterion (decided 2026-10-16): NODE AVERAGE CPU <= 70 %.
  local n worst=0 worst_node=0 core_max=0 line avg core
  for n in $(nodes); do
    line=$(on "$n" "awk '\$1==\"Average:\" && \$2==\"all\" {a=100-\$NF}
                      \$1==\"Average:\" && \$2 ~ /^[0-9]+\$/ {u=100-\$NF; if(u>m)m=u}
                      END{printf \"%.0f %.0f\", a, m}' /tmp/tgen_mpstat.txt" </dev/null)
    read -r avg core <<<"$line"
    echo "  $1 $(nname "$n") avg=${avg:-?}% max-core=${core:-?}%" >> "$OUT/cpu.txt"
    if [[ -n "${avg:-}" ]] && (( avg > worst )); then worst=$avg; worst_node=$n; fi
    if [[ -n "${core:-}" ]] && (( core > core_max )); then core_max=$core; fi
  done
  echo "${worst} ${worst_node} ${core_max}"
}

start_iperf_servers() {
  local n; for n in $(nodes); do
    on "$n" "pkill -x iperf3; iperf3 -s -D -B $(traffic_ip "$n") -p 5201" </dev/null
  done
  sleep 1
}
stop_iperf_servers() { local n; for n in $(nodes); do on "$n" "pkill -x iperf3" </dev/null; done; }

echo "TrafficGenerator cluster test  $(date -Is)  config=$CONFIG" | tee "$SUMMARY"
echo "traffic mode=$TRAFFIC_MODE iface=$(tif) route-server=${ROUTE_SERVER_IP:-none} sink=${SINK_IP:-none}" | tee -a "$SUMMARY"
echo "----------------------------------------------------------------" | tee -a "$SUMMARY"

# ---------------------------------------------------------------- T01 SSH ---
if want T01; then
  bad=""
  for n in $(nodes); do on "$n" true </dev/null || bad+=" $(nname "$n")"; done
  [[ -z "$bad" ]] && result T01 PASS "SSH to all ${NODE_COUNT} nodes via mgmt" \
                  || { result T01 FAIL "SSH failed:$bad"; echo "Cannot continue without SSH"; exit 99; }
fi

# ------------------------------------------------------- T02 node config ----
if want T02; then
  bad=""
  for n in $(nodes); do
    info=$(on "$n" "
      . /etc/os-release; echo os=\$VERSION_CODENAME
      echo model=\$(tr -d '\0' </proc/device-tree/model | tr ' ' _)
      echo speed=\$(cat /sys/class/net/${MGMT_IFACE}/speed)
      echo duplex=\$(cat /sys/class/net/${MGMT_IFACE}/duplex)
      echo gov=\$(cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor)
      echo tip=\$(ip -4 -o addr show dev $(tif) | grep -c $(traffic_ip "$n")/)
      echo thr=\$(vcgencmd get_throttled 2>/dev/null | cut -d= -f2)
      echo temp=\$(vcgencmd measure_temp 2>/dev/null | grep -oE '[0-9.]+')
      echo inst=\$(test -f /etc/tgen/node.conf && echo yes || echo no)
      echo mem=\$(awk '/MemTotal/{printf \"%d\", \$2/1048576+0.5}' /proc/meminfo)" </dev/null)
    echo "$(nname "$n"): $(echo "$info" | tr '\n' ' ')" >> "$OUT/nodes.txt"
    get() { echo "$info" | sed -n "s/^$1=//p"; }
    [[ "$(get speed)" == 1000 && "$(get duplex)" == full ]] || bad+=" $(nname "$n"):link=$(get speed)/$(get duplex)"
    [[ "$(get inst)" == yes ]]                              || bad+=" $(nname "$n"):INSTALLER-NOT-RUN"
    [[ "$(get gov)" == "${CPU_GOVERNOR}" ]]                 || bad+=" $(nname "$n"):gov=$(get gov)"
    [[ "$(get tip)" == 1 ]]                                 || bad+=" $(nname "$n"):no-traffic-ip"
    [[ "$(get model)" == *Compute_Module_5* ]]              || bad+=" $(nname "$n"):model"
    [[ "$(get thr)" == 0x0 ]]                               || bad+=" $(nname "$n"):throttled=$(get thr)"
  done
  [[ -z "$bad" ]] && result T02 PASS "1000/full, governor, traffic IP, CM5, not throttled (see nodes.txt)" \
                  || result T02 FAIL "$bad"
fi

# ------------------------------------------------ T03/T04 full-mesh ping ----
mesh_ping() {  # id label ipfunc  -> reports failing DIRECTED PAIRS, not just sources
  local id="$1" label="$2" fn="$3" bad="" n m targets ip loss
  declare -A ip2name=()
  for m in $(nodes); do ip2name[$($fn "$m")]="$(nname "$m")"; done
  for n in $(nodes); do
    targets=""; for m in $(nodes); do (( m != n )) && targets+=" $($fn "$m")"; done
    out=$(on "$n" "fping -q -c 20 -p 20 $targets 2>&1" </dev/null)
    echo "== $(nname "$n") $label" >> "$OUT/ping.txt"; echo "$out" >> "$OUT/ping.txt"
    echo "$out" | grep -q 'xmt/rcv' || { bad+=" $(nname "$n")(fping-no-output)"; continue; }
    while read -r ip loss; do
      [[ "$loss" == 0 ]] || bad+=" $(nname "$n")->${ip2name[$ip]:-$ip}(${loss}%)"
    done < <(echo "$out" | sed -n 's#^\([0-9.]*\) *: xmt/rcv/%loss = [0-9]*/[0-9]*/\([0-9]*\)%.*#\1 \2#p')
  done
  # summarise targets that fail from every other node (= that node is the problem)
  local hint="" name cnt
  for m in $(nodes); do
    name="$(nname "$m")"; cnt=$(grep -o -- "->${name}(" <<<"$bad" | wc -l)
    (( cnt >= NODE_COUNT - 1 )) && hint+=" ${name}"
  done
  if [[ -z "$bad" ]]; then
    result "$id" PASS "${label}: 30 directed pairs x20 pings, 0% loss"
  elif [[ -n "$hint" ]]; then
    result "$id" FAIL "${label}: unreachable from all others:${hint} (check its address with: ip -4 addr show ${MGMT_IFACE})"
  else
    result "$id" FAIL "${label}: lossy pairs:$bad (see ping.txt)"
  fi
}
want T03 && mesh_ping T03 "mgmt mesh"    mgmt_ip
want T04 && mesh_ping T04 "traffic mesh (${TRAFFIC_MODE} on $(tif))" traffic_ip

# --------------------------------------------------------------- T05 MTU ----
if want T05; then
  bad=""
  for n in $(nodes); do
    m=$(next_node "$n")
    on "$n" "ping -q -c 3 -M do -s 1472 -I $(tif) $(traffic_ip "$m") >/dev/null 2>&1" </dev/null \
      || bad+=" $n->$m"
  done
  [[ -z "$bad" ]] && result T05 PASS "1500-byte frames DF across traffic net" \
                  || result T05 FAIL "MTU 1500 DF failed:$bad"
fi

# --------------------------------------------------------- T06 time sync ----
if want T06; then
  bad=""
  for n in $(nodes); do
    if ! on "$n" "command -v chronyc >/dev/null" </dev/null; then
      bad+=" $(nname "$n")(chrony-not-installed)"; continue
    fi
    off=$(on "$n" "chronyc -n tracking | awk '/System time/{print \$4}'" </dev/null)
    echo "$(nname "$n") offset=${off}s" >> "$OUT/time.txt"
    awk -v o="${off:-9}" 'BEGIN{exit !(o<0.001 && o>-0.001)}' || bad+=" $(nname "$n")(${off:-?}s)"
  done
  [[ -z "$bad" ]] && result T06 PASS "chrony offset < 1 ms on all nodes" \
                  || result T06 FAIL "offset too large:$bad"
fi

# ---------------------------------------------- T07 pairwise throughput -----
if want T07; then
  start_iperf_servers; bad=""
  for n in $(nodes); do
    m=$(next_node "$n")
    bps=$(on "$n" "iperf3 -J -t $DUR -c $(traffic_ip "$m") -B $(traffic_ip "$n")" </dev/null \
          | tee "$OUT/t07_${n}_${m}.json" | jq -r '.end.sum_received.bits_per_second // 0')
    mbps=$(awk -v b="${bps:-0}" 'BEGIN{printf "%d", b/1e6}')
    echo "  $n->$m ${mbps} Mbit/s" >> "$OUT/throughput.txt"
    (( mbps >= 900 )) || bad+=" $n->$m:${mbps}"
  done
  stop_iperf_servers
  [[ -z "$bad" ]] && result T07 PASS "each node pair >= 900 Mbit/s TCP (sequential)" \
                  || result T07 FAIL "below 900 Mbit/s:$bad"
fi

# ------------------------------- T08 all nodes concurrently (switch fabric) -
if want T08 && (( ! QUICK )); then
  start_iperf_servers; cpu_start $((DUR + 2))
  for n in $(nodes); do
    m=$(next_node "$n")
    on "$n" "iperf3 -J -t $DUR -c $(traffic_ip "$m") -B $(traffic_ip "$n")" </dev/null \
      > "$OUT/t08_${n}_${m}.json" &
  done
  wait; sleep 2
  read -r cpu cpun coremax < <(cpu_collect T08)
  stop_iperf_servers
  total=0; bad=""
  for f in "$OUT"/t08_*.json; do
    mb=$(jq -r '.end.sum_received.bits_per_second // 0' "$f" | awk '{printf "%d", $1/1e6}')
    total=$((total + mb)); (( mb >= 850 )) || bad+=" $(basename "$f" .json):${mb}"
  done
  [[ -z "$bad" ]] && result T08 PASS "6 concurrent flows, aggregate ${total} Mbit/s; CPU node-avg max ${cpu}% ($(nname "$cpun")), hottest core ${coremax}%" \
                  || result T08 FAIL "aggregate ${total} Mbit/s, slow:$bad"
fi

# ----------------------------- T09 aggregate 1 Gbit/s into one port --------
# SINK_IP = external host on ETH0 (route server)   -> all 6 nodes send to it
# SINK_IP = a node's traffic IP (e.g. 10.100.0.16)  -> stand-in: that node runs
#           the iperf3 servers itself, the other 5 nodes send to it
if want T09; then
  if [[ -z "${SINK_IP}" ]]; then
    result T09 SKIP "SINK_IP not set (route server on ETH0, or a node's traffic IP as stand-in)"
  else
    sink_node=0
    for n in $(nodes); do [[ "$(traffic_ip "$n")" == "$SINK_IP" ]] && sink_node=$n; done
    senders=(); for n in $(nodes); do (( n != sink_node )) && senders+=("$n"); done
    if (( sink_node )); then
      where="stand-in sink $(nname "$sink_node")"
      on "$sink_node" "pkill -x iperf3; for p in \$(seq 5201 $((5200 + NODE_COUNT))); do iperf3 -s -D -B ${SINK_IP} -p \$p; done" </dev/null
      sleep 1
    else
      where="ETH0 port (${SINK_IP})"
    fi
    per=$(( 950 / ${#senders[@]} ))
    rm -f "$OUT"/t09_*.json
    cpu_start $((DUR + 2))
    for n in "${senders[@]}"; do
      on "$n" "iperf3 -J -u -l 1400 -b ${per}M -t $DUR -c ${SINK_IP} -p $((5200 + n)) -B $(traffic_ip "$n")" \
        </dev/null > "$OUT/t09_${n}.json" &
    done
    wait; sleep 2
    read -r cpu cpun coremax < <(cpu_collect T09)
    (( sink_node )) && on "$sink_node" "pkill -x iperf3" </dev/null
    total=0; lossmax=0
    for f in "$OUT"/t09_*.json; do
      mb=$(jq -r '.end.sum.bits_per_second // 0' "$f" | awk '{printf "%d", $1/1e6}')
      lp=$(jq -r '.end.sum.lost_percent // 100' "$f")
      total=$((total + mb))
      awk -v a="$lp" -v b="$lossmax" 'BEGIN{exit !(a>b)}' && lossmax=$lp
    done
    if (( total >= 900 )) && awk -v l="$lossmax" 'BEGIN{exit !(l<0.5)}'; then
      result T09 PASS "${where}: ${#senders[@]} senders, ${total} Mbit/s UDP, max loss ${lossmax}%, CPU node-avg max ${cpu}% ($(nname "$cpun"))"
    else
      result T09 FAIL "${where}: ${#senders[@]} senders, ${total} Mbit/s, max loss ${lossmax}%"
    fi
  fi
fi

# ---------------------------------------------------- T10 thermal after load -
if want T10; then
  bad=""
  for n in $(nodes); do
    t=$(on "$n" "vcgencmd get_throttled | cut -d= -f2; vcgencmd measure_temp | grep -oE '[0-9.]+'" </dev/null | tr '\n' ' ')
    echo "$(nname "$n") throttled/temp: $t" >> "$OUT/thermal.txt"
    read -r thr temp <<<"$t"
    [[ "$thr" == 0x0 ]] || bad+=" $(nname "$n"):thr=$thr"
    awk -v x="${temp:-99}" 'BEGIN{exit !(x<80)}' || bad+=" $(nname "$n"):${temp}C"
  done
  [[ -z "$bad" ]] && result T10 PASS "no throttling, all < 80 C after load" \
                  || result T10 FAIL "$bad"
fi

# ------------------------------------------- T11 route server reachability -
if want T11; then
  if [[ -z "${ROUTE_SERVER_IP:-}" ]]; then
    result T11 SKIP "ROUTE_SERVER_IP not set"
  else
    bad=""
    for n in $(nodes); do
      on "$n" "fping -q -c 10 -p 50 ${ROUTE_SERVER_IP} 2>&1 | grep -q '/0%'" </dev/null || bad+=" $(nname "$n")"
      if on "$n" "timeout 3 bash -c '</dev/tcp/${ROUTE_SERVER_IP}/179' 2>/dev/null" </dev/null; then
        echo "$(nname "$n"): TCP/179 open on route server" >> "$OUT/route_server.txt"
      else
        echo "$(nname "$n"): TCP/179 closed/filtered (OK before BGP is configured)" >> "$OUT/route_server.txt"
      fi
    done
    [[ -z "$bad" ]] && result T11 PASS "route server ${ROUTE_SERVER_IP} reachable from all nodes, 0% loss (BGP port: route_server.txt)" \
                    || result T11 FAIL "route server unreachable/lossy from:$bad"
  fi
fi

echo "----------------------------------------------------------------" | tee -a "$SUMMARY"
echo "PASS=$PASS FAIL=$FAIL SKIP=$SKIP   results: $OUT" | tee -a "$SUMMARY"
exit "$FAIL"
