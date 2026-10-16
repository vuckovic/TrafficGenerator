#!/usr/bin/env bash
# ============================================================================
# TrafficGenerator - node prerequisites installer
# Target : Raspberry Pi CM5 on DeskPi Super6C, Raspberry Pi OS Trixie 64-bit (Bookworm also supported)
# Usage  : sudo ./install-prereqs.sh --node-id N [--config cluster.env]
#                                    [--skip-network] [--force] [--dry-run]
# Idempotent: safe to run repeatedly. Run on every node (Ansible does this
# for you, see deploy/ansible/site.yml).
# ============================================================================
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG="${SCRIPT_DIR}/cluster.env"
NODE_ID=""
SKIP_NETWORK=0
FORCE=0
DRY_RUN=0
LOG_FILE="/var/log/tgen-install.log"
REBOOT_NEEDED=0
WARNINGS=()

# ---------------------------------------------------------------- helpers ---
log()  { printf '[%s] %s\n' "$(date +%H:%M:%S)" "$*" | tee -a "$LOG_FILE"; }
warn() { WARNINGS+=("$*"); log "WARN: $*"; }
die()  { log "ERROR: $*"; exit 1; }
run()  { if (( DRY_RUN )); then log "DRY-RUN: $*"; else "$@"; fi; }
trap 'die "failed at line ${LINENO}: ${BASH_COMMAND}"' ERR

usage() {
  sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'
  exit "${1:-0}"
}

# Write a file only if content differs (keeps idempotency, reports change)
write_file() {
  local path="$1" content="$2"
  if [[ -f "$path" ]] && [[ "$(cat "$path")" == "$content" ]]; then
    return 1
  fi
  if (( DRY_RUN )); then log "DRY-RUN: write $path"; return 0; fi
  install -d "$(dirname "$path")"
  printf '%s\n' "$content" > "$path"
  log "wrote $path"
  return 0
}

# Insert/replace a marked block in a file
upsert_block() {
  local path="$1" content="$2"
  local begin="# >>> tgen managed block >>>" end="# <<< tgen managed block <<<"
  [[ -f "$path" ]] || die "$path not found"
  local current=""
  current="$(sed -n "/^${begin}\$/,/^${end}\$/p" "$path")"
  local wanted
  wanted="$(printf '%s\n%s\n%s' "$begin" "$content" "$end")"
  [[ "$current" == "$wanted" ]] && return 1
  (( DRY_RUN )) && { log "DRY-RUN: update block in $path"; return 0; }
  cp -a "$path" "${path}.tgen.bak"
  sed -i "/^${begin}\$/,/^${end}\$/d" "$path"
  printf '%s\n' "$wanted" >> "$path"
  log "updated managed block in $path (backup: ${path}.tgen.bak)"
  return 0
}

mgmt_ip()    { echo "${MGMT_PREFIX}.$((MGMT_HOST_BASE + $1))"; }
traffic_ip() { echo "${TRAFFIC_PREFIX}.$((TRAFFIC_HOST_BASE + $1))"; }

# ------------------------------------------------------------------- args ---
while [[ $# -gt 0 ]]; do
  case "$1" in
    --node-id)      NODE_ID="${2:-}"; shift 2 ;;
    --config)       CONFIG="${2:-}"; shift 2 ;;
    --skip-network) SKIP_NETWORK=1; shift ;;
    --force)        FORCE=1; shift ;;
    --dry-run)      DRY_RUN=1; shift ;;
    -h|--help)      usage 0 ;;
    *)              echo "unknown option: $1"; usage 1 ;;
  esac
done

[[ $EUID -eq 0 ]] || die "run as root (sudo)"
[[ -f "$CONFIG" ]] || die "config not found: $CONFIG (copy cluster.env.example)"
# shellcheck source=cluster.env.example
source "$CONFIG"
if ! [[ "$NODE_ID" =~ ^[1-9][0-9]*$ ]] || (( NODE_ID > NODE_COUNT )); then
  die "--node-id must be 1..${NODE_COUNT}"
fi
touch "$LOG_FILE"

HOSTNAME_NEW="${NODE_HOSTNAMES[$((NODE_ID-1))]}"
[[ -n "$HOSTNAME_NEW" ]] || die "no NODE_HOSTNAMES entry for node ${NODE_ID}"
[[ "$MGMT_MODE" == "dhcp" ]] && MGMT_MODE="keep"   # legacy alias
IS_CONTROLLER=0; (( NODE_ID == CONTROLLER_NODE )) && IS_CONTROLLER=1

log "=== TrafficGenerator prerequisites: node ${NODE_ID} (${HOSTNAME_NEW}) ==="

# -------------------------------------------------------------- preflight ---
step_preflight() {
  log "--- preflight"
  [[ "$(uname -m)" == "aarch64" ]] || die "64-bit OS required (uname -m=$(uname -m))"
  # shellcheck disable=SC1091
  source /etc/os-release
  case "${VERSION_CODENAME:-}" in
    trixie)   ;;
    bookworm) log "Bookworm detected (supported; Trixie is the primary target)" ;;
    *)        (( FORCE )) || die "unsupported OS '${VERSION_CODENAME:-?}' (use --force)" ;;
  esac
  local model; model="$(tr -d '\0' < /proc/device-tree/model 2>/dev/null || echo unknown)"
  log "model: $model | kernel: $(uname -r)"
  [[ "$model" == *"Compute Module 5"* ]] || warn "not a CM5 ($model)"
  ip link show "$MGMT_IFACE" >/dev/null 2>&1 || die "interface $MGMT_IFACE missing"
  local speed; speed="$(cat "/sys/class/net/${MGMT_IFACE}/speed" 2>/dev/null || echo 0)"
  [[ "$speed" == "1000" ]] || warn "${MGMT_IFACE} link speed is '${speed}', expected 1000"
  local mem_gb; mem_gb=$(( $(awk '/MemTotal/{print $2}' /proc/meminfo) / 1024 / 1024 + 1 ))
  log "memory: ~${mem_gb} GB (config says ${NODE_RAM_GB[$((NODE_ID-1))]} GB)"
  local want_ip; want_ip="$(mgmt_ip "$NODE_ID")"
  if ip -4 -o addr show dev "$MGMT_IFACE" | grep -q " ${want_ip}/"; then
    log "mgmt address ${want_ip} present on ${MGMT_IFACE}"
  else
    warn "expected mgmt address ${want_ip} not found on ${MGMT_IFACE} - check --node-id / cluster.env"
  fi
  if [[ "${HOSTNAME,,}" != "${HOSTNAME_NEW,,}" ]] && [[ "$(hostname)" != "$HOSTNAME_NEW" ]]; then
    warn "hostname is '$(hostname)', cluster.env says '${HOSTNAME_NEW}' for node ${NODE_ID}"
  fi
  timeout 10 getent hosts deb.debian.org >/dev/null || die "no DNS/internet for apt"
}

# --------------------------------------------------------------- packages ---
REQUIRED_PKGS=(
  build-essential g++ cmake ninja-build pkg-config git gdb ccache
  libpcap-dev libbpf-dev libssl-dev libcap2-bin
  libspdlog-dev libfmt-dev nlohmann-json3-dev libyaml-cpp-dev
  libgtest-dev libgmock-dev
  iproute2 ethtool iperf3 tcpdump fping chrony sysstat jq
  python3 python3-venv python3-pip
)
OPTIONAL_PKGS=(
  libxdp-dev xdp-tools bpftool linux-perf tshark nload htop
  lldpd stress-ng libbenchmark-dev
)
CONTROLLER_PKGS=( ansible sshpass )

step_packages() {
  log "--- packages"
  export DEBIAN_FRONTEND=noninteractive
  # avoid interactive debconf prompts
  echo "iperf3 iperf3/start_daemon boolean false" | debconf-set-selections
  echo "wireshark-common wireshark-common/install-setuid boolean false" | debconf-set-selections
  # Wait (up to 15 min) for any other apt/dpkg process - e.g. automatic
  # updates or an earlier interrupted run - instead of failing immediately.
  local APT=(apt-get -o DPkg::Lock::Timeout=900 -o Dpkg::Options::=--force-confold)
  if pgrep -x 'apt-get|apt|dpkg|unattended-upgr' >/dev/null; then
    log "another package manager is running: $(pgrep -a -x 'apt-get|apt|dpkg|unattended-upgr' | head -3 | tr '\n' ';') - waiting for it"
  fi
  # recover from a previously interrupted dpkg run
  run dpkg --configure -a >/dev/null 2>&1 || warn "dpkg --configure -a reported problems"
  run "${APT[@]}" update -qq
  run "${APT[@]}" install -y -qq --no-install-recommends "${REQUIRED_PKGS[@]}" \
    || die "required package install failed"
  local p
  for p in "${OPTIONAL_PKGS[@]}"; do
    run "${APT[@]}" install -y -qq --no-install-recommends "$p" >/dev/null 2>&1 \
      || warn "optional package unavailable: $p"
  done
  if (( IS_CONTROLLER )); then
    run "${APT[@]}" install -y -qq --no-install-recommends "${CONTROLLER_PKGS[@]}" \
      || die "controller package install failed"
  fi
  # allow tgen binaries to use raw sockets without root later (setcap at deploy)
}

# ----------------------------------------------------------------- sysctl ---
step_sysctl() {
  log "--- sysctl"
  local conf
  conf=$(cat <<'EOF'
# TrafficGenerator network tuning
net.core.rmem_max = 67108864
net.core.wmem_max = 67108864
net.core.rmem_default = 4194304
net.core.wmem_default = 4194304
net.core.netdev_max_backlog = 30000
net.core.optmem_max = 1048576
net.core.busy_poll = 50
net.core.busy_read = 50
net.ipv4.tcp_rmem = 4096 1048576 67108864
net.ipv4.tcp_wmem = 4096 1048576 67108864
net.ipv4.tcp_congestion_control = cubic
net.ipv4.ip_local_port_range = 10000 65000
# generated packets use spoofed source subnets: never route them back in
net.ipv4.conf.all.rp_filter = 0
net.ipv4.conf.default.rp_filter = 0
net.ipv4.ip_forward = 0
# keep ARP answers on the right interface (mgmt and traffic share eth0)
net.ipv4.conf.all.arp_ignore = 1
net.ipv4.conf.all.arp_announce = 2
vm.swappiness = 10
kernel.perf_event_paranoid = 1
EOF
)
  if write_file /etc/sysctl.d/90-tgen.conf "$conf"; then
    run sysctl --system >/dev/null
  fi
}

# --------------------------------------------- CPU governor / NIC tuning ----
step_tuning_service() {
  log "--- tuning service (governor=${CPU_GOVERNOR}, EEE off, ring max)"
  local script
  script=$(cat <<EOF
#!/usr/bin/env bash
# Applied at every boot by tgen-tuning.service
for g in /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor; do
  echo ${CPU_GOVERNOR} > "\$g" 2>/dev/null || true
done
IF=${MGMT_IFACE}
# Energy Efficient Ethernet adds latency jitter on bursty TX
ethtool --set-eee "\$IF" eee off 2>/dev/null || true
# maximise descriptor rings if driver supports it
read -r RXM TXM < <(ethtool -g "\$IF" 2>/dev/null | awk '/Pre-set maximums/{f=1} f&&/^RX:/{r=\$2} f&&/^TX:/{t=\$2; print r, t; exit}')
[[ -n "\${RXM:-}" ]] && ethtool -G "\$IF" rx "\$RXM" tx "\$TXM" 2>/dev/null || true
# larger TX queue for burst absorption
ip link set dev "\$IF" txqueuelen 10000 || true
exit 0
EOF
)
  write_file /usr/local/sbin/tgen-tuning.sh "$script" && run chmod 0755 /usr/local/sbin/tgen-tuning.sh
  local unit
  unit=$(cat <<'EOF'
[Unit]
Description=TrafficGenerator host tuning
After=network-pre.target
Wants=network-pre.target

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/tgen-tuning.sh
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
EOF
)
  write_file /etc/systemd/system/tgen-tuning.service "$unit" && run systemctl daemon-reload
  run systemctl enable --now tgen-tuning.service >/dev/null
}

# --------------------------------------------------------------- firmware ---
step_boot_config() {
  log "--- /boot/firmware/config.txt"
  local cfg=/boot/firmware/config.txt
  [[ -f "$cfg" ]] || { warn "$cfg not found, skipping"; return; }
  local block="arm_64bit=1"
  if [[ "$DISABLE_RADIOS" == "yes" ]]; then
    block+=$'\n'"dtoverlay=disable-wifi"$'\n'"dtoverlay=disable-bt"
  fi
  upsert_block "$cfg" "$block" && REBOOT_NEEDED=1 || true
}

# --------------------------------------------------------------- hostname ---
step_hostname() {
  log "--- hostname/hosts"
  if [[ "$MGMT_MODE" == "static" ]] && [[ "$(hostname)" != "$HOSTNAME_NEW" ]]; then
    run hostnamectl set-hostname "$HOSTNAME_NEW"
  fi   # MGMT_MODE=keep: existing hostname is never changed
  local hosts="" n name
  for (( n=1; n<=NODE_COUNT; n++ )); do
    name="${NODE_HOSTNAMES[$((n-1))]}"
    hosts+="$(mgmt_ip "$n")	${name}"$'\n'
    hosts+="$(traffic_ip "$n")	${name}-t"$'\n'
  done
  hosts="${hosts%$'\n'}"
  local me; me="$(hostname)"
  if grep -q '^127\.0\.1\.1' /etc/hosts; then
    run sed -i "s/^127\.0\.1\.1.*/127.0.1.1\t${me}/" /etc/hosts
  else
    run sh -c "printf '127.0.1.1\t%s\n' '${me}' >> /etc/hosts"
  fi
  upsert_block /etc/hosts "$hosts" || true
}

# ---------------------------------------------------------------- network ---
step_network() {
  (( SKIP_NETWORK )) && { log "--- network: skipped (--skip-network)"; return; }
  log "--- network (NetworkManager)"
  command -v nmcli >/dev/null || die "nmcli not found (Raspberry Pi OS uses NetworkManager)"

  # Management: static profile is created but NOT activated now, so an SSH
  # session over the old address survives. It takes over at next reboot.
  if [[ "$MGMT_MODE" == "static" ]]; then
    local ip; ip="$(mgmt_ip "$NODE_ID")"
    if ! nmcli -t -f NAME con show | grep -qx tgen-mgmt; then
      run nmcli con add type ethernet con-name tgen-mgmt ifname "$MGMT_IFACE" \
        ipv4.method manual ipv4.addresses "${ip}/${MGMT_CIDR}" \
        ipv4.gateway "$MGMT_GW" ipv4.dns "$MGMT_DNS" ipv6.method disabled \
        connection.autoconnect yes connection.autoconnect-priority 100
      REBOOT_NEEDED=1
    else
      run nmcli con mod tgen-mgmt ipv4.addresses "${ip}/${MGMT_CIDR}" \
        ipv4.gateway "$MGMT_GW" ipv4.dns "$MGMT_DNS"
    fi
    # Other ethernet profiles on this iface must not autoconnect
    local c
    while IFS=: read -r c t; do
      [[ "$t" == "802-3-ethernet" && "$c" != "tgen-mgmt" ]] && \
        run nmcli con mod "$c" connection.autoconnect no
    done < <(nmcli -t -f NAME,TYPE con show)
  else
    log "MGMT_MODE=keep: existing management config (name/IP) left untouched"
  fi

  local tip; tip="$(traffic_ip "$NODE_ID")"
  if [[ "$TRAFFIC_MODE" == "vlan" ]]; then
    if ! nmcli -t -f NAME con show | grep -qx tgen-traffic; then
      run nmcli con add type vlan con-name tgen-traffic ifname "${MGMT_IFACE}.${TRAFFIC_VLAN}" \
        dev "$MGMT_IFACE" id "$TRAFFIC_VLAN" \
        ipv4.method manual ipv4.addresses "${tip}/${TRAFFIC_CIDR}" \
        ipv4.never-default yes ipv6.method disabled connection.autoconnect yes
    else
      run nmcli con mod tgen-traffic ipv4.addresses "${tip}/${TRAFFIC_CIDR}"
    fi
    run nmcli con up tgen-traffic >/dev/null || warn "tgen-traffic not up yet (will be after reboot)"
  else
    # untagged: traffic subnet as secondary address on the management profile
    local prof
    if [[ "$MGMT_MODE" == "static" ]]; then
      prof="tgen-mgmt"
    else
      prof="$(nmcli -t -f NAME,DEVICE con show --active | awk -F: -v d="$MGMT_IFACE" '$2==d{print $1; exit}')"
      [[ -n "$prof" ]] || { warn "no active NetworkManager profile on ${MGMT_IFACE}; add ${tip}/${TRAFFIC_CIDR} manually"; return; }
    fi
    if nmcli -g ipv4.addresses con show "$prof" | tr ',' '\n' | grep -q "^ *${tip}/"; then
      log "traffic address ${tip} already in profile '${prof}'"
    else
      run nmcli con mod "$prof" +ipv4.addresses "${tip}/${TRAFFIC_CIDR}"
      log "added traffic address ${tip}/${TRAFFIC_CIDR} to profile '${prof}'"
      if [[ "$MGMT_MODE" == "keep" ]]; then
        # reapply adds the address live without bouncing the link (SSH survives)
        run nmcli device reapply "$MGMT_IFACE" \
          || { warn "live reapply failed; address active after reboot"; REBOOT_NEEDED=1; }
      fi
    fi
  fi
}

# ----------------------------------------------------------------- chrony ---
step_chrony() {
  log "--- chrony (time sync: needed for latency/one-way measurements)"
  local conf ctrl_ip; ctrl_ip="$(mgmt_ip "$CONTROLLER_NODE")"
  if (( IS_CONTROLLER )); then
    conf=$(cat <<EOF
pool ${NTP_UPSTREAM} iburst
allow ${MGMT_PREFIX}.0/${MGMT_CIDR}
local stratum 8
makestep 1 3
rtcsync
EOF
)
  else
    conf=$(cat <<EOF
server ${ctrl_ip} iburst minpoll 2 maxpoll 4 prefer
pool ${NTP_UPSTREAM} iburst
makestep 1 3
rtcsync
EOF
)
  fi
  if write_file /etc/chrony/conf.d/tgen.conf "$conf"; then
    # disable distro default pool so ours wins
    sed -i 's/^\(pool .*debian.*\)$/# \1  # disabled by tgen/' /etc/chrony/chrony.conf || true
    run systemctl restart chrony
  fi
  run systemctl enable chrony >/dev/null
}

# ------------------------------------------------------------ app layout ----
step_layout() {
  log "--- user, directories"
  getent group tgen >/dev/null || run groupadd --system tgen
  id tgen >/dev/null 2>&1 || run useradd --system -g tgen -d /opt/tgen -s /usr/sbin/nologin tgen
  run install -d -o tgen -g tgen -m 0755 /opt/tgen /opt/tgen/bin /var/log/tgen /var/lib/tgen
  run install -d -m 0755 /etc/tgen
  local nodeconf
  nodeconf=$(cat <<EOF
NODE_ID=${NODE_ID}
HOSTNAME=${HOSTNAME_NEW}
IS_CONTROLLER=${IS_CONTROLLER}
MGMT_IP=$(mgmt_ip "$NODE_ID")
TRAFFIC_IP=$(traffic_ip "$NODE_ID")
TRAFFIC_IFACE=$( [[ "$TRAFFIC_MODE" == vlan ]] && echo "${MGMT_IFACE}.${TRAFFIC_VLAN}" || echo "$MGMT_IFACE")
RAM_GB=${NODE_RAM_GB[$((NODE_ID-1))]}
EOF
)
  write_file /etc/tgen/node.conf "$nodeconf" || true
  # install sha of config used
  cp "$CONFIG" /etc/tgen/cluster.env
}

# ----------------------------------------------------------------- verify ---
step_verify() {
  log "--- verification"
  local t
  for t in g++ cmake ninja iperf3 tcpdump fping ethtool chronyc; do
    command -v "$t" >/dev/null || die "missing tool after install: $t"
  done
  log "g++   : $(g++ -dumpfullversion)"
  log "cmake : $(cmake --version | head -1)"
  log "gov   : $(cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor 2>/dev/null || echo n/a)"
  log "temp  : $(vcgencmd measure_temp 2>/dev/null || echo n/a)"
  log "throttled: $(vcgencmd get_throttled 2>/dev/null || echo n/a)"
}

main() {
  step_preflight
  step_packages
  step_sysctl
  step_tuning_service
  step_boot_config
  step_hostname
  step_layout
  step_chrony
  step_network
  step_verify
  log "=== done: node ${NODE_ID} ==="
  if (( ${#WARNINGS[@]} )); then
    log "warnings (${#WARNINGS[@]}):"; printf '   - %s\n' "${WARNINGS[@]}" | tee -a "$LOG_FILE"
  fi
  if (( REBOOT_NEEDED )); then
    log "REBOOT REQUIRED (new mgmt IP $(mgmt_ip "$NODE_ID") / firmware config takes effect)"
    touch /var/run/reboot-required
  fi
}
main
