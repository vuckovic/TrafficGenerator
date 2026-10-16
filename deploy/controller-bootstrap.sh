#!/usr/bin/env bash
# ============================================================================
# TrafficGenerator - controller bootstrap for CMNODE01
#
# Turns CMNODE01 into the control node for the whole cluster:
#   1. installs control tools (git, ansible, sshpass, fping, jq, iperf3, ...)
#   2. gets the TrafficGenerator repo and creates deploy/cluster.env
#   3. creates an SSH key and installs it on all 6 nodes (asks the password once)
#   4. checks every node: SSH, hostname, IP, OS, model, link, sudo, internet
#   5. optional: --install  runs the Ansible playbook on all nodes
#      optional: --test     runs the cluster connectivity test suite
#
# Run on CMNODE01 as your normal login user (NOT root):
#   bash controller-bootstrap.sh [--user pi] [--route-server 10.100.0.1]
#                                [--sink 10.100.0.1] [--install] [--test]
#                                [--repo-dir ~/TrafficGenerator] [--repo-url URL]
# Safe to run repeatedly.
# ============================================================================
set -Eeuo pipefail

REPO_URL="https://github.com/vuckovic/TrafficGenerator.git"
REPO_DIR=""
SSH_USER_ARG=""
ROUTE_SERVER_ARG=""
SINK_ARG=""
DO_INSTALL=0
DO_TEST=0
LOG="${HOME}/tgen-controller-bootstrap.log"

c_ok=$'\e[32m'; c_bad=$'\e[31m'; c_warn=$'\e[33m'; c_off=$'\e[0m'
log()  { printf '[%s] %s\n' "$(date +%H:%M:%S)" "$*" | tee -a "$LOG"; }
ok()   { printf '  %sOK%s   %s\n'   "$c_ok"   "$c_off" "$*" | tee -a "$LOG"; }
bad()  { printf '  %sFAIL%s %s\n'   "$c_bad"  "$c_off" "$*" | tee -a "$LOG"; }
note() { printf '  %sWARN%s %s\n'   "$c_warn" "$c_off" "$*" | tee -a "$LOG"; }
die()  { bad "$*"; exit 1; }

usage() { sed -n '2,19p' "$0" | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --user)         SSH_USER_ARG="$2"; shift 2 ;;
    --route-server) ROUTE_SERVER_ARG="$2"; shift 2 ;;
    --sink)         SINK_ARG="$2"; shift 2 ;;
    --install)      DO_INSTALL=1; shift ;;
    --test)         DO_TEST=1; shift ;;
    --repo-dir)     REPO_DIR="$2"; shift 2 ;;
    --repo-url)     REPO_URL="$2"; shift 2 ;;
    -h|--help)      usage 0 ;;
    *)              echo "unknown option: $1"; usage 1 ;;
  esac
done

: > "$LOG"
log "=== TrafficGenerator controller bootstrap on $(hostname) ==="

# ------------------------------------------------------------- 0. preflight -
[[ $EUID -ne 0 ]] || die "run as your normal login user, not root (sudo is used where needed)"
sudo -n true 2>/dev/null || { log "sudo password needed for package install:"; sudo -v; }
[[ "$(hostname)" == "CMNODE01" ]] || note "this host is '$(hostname)', expected CMNODE01 - continuing"
[[ "$(uname -m)" == "aarch64" ]] || die "64-bit OS required"

# ------------------------------------------------------- 1. control tools ---
log "--- 1/5 installing control tools"
export DEBIAN_FRONTEND=noninteractive
echo "iperf3 iperf3/start_daemon boolean false" | sudo debconf-set-selections
sudo apt-get -o DPkg::Lock::Timeout=900 update -qq
sudo apt-get -o DPkg::Lock::Timeout=900 install -y -qq --no-install-recommends \
  git ansible sshpass openssh-client fping jq iperf3 sysstat tmux curl ca-certificates \
  python3 >/dev/null
ok "git $(git --version | awk '{print $3}'), $(ansible --version | head -1), fping, jq, iperf3"

# ------------------------------------------------------------- 2. repo ------
log "--- 2/5 repository"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ -z "$REPO_DIR" && -f "${SCRIPT_DIR}/cluster.env.example" && -d "${SCRIPT_DIR}/../.git" ]]; then
  REPO_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"          # script runs from inside a clone
fi
REPO_DIR="${REPO_DIR:-${HOME}/TrafficGenerator}"
if [[ -d "${REPO_DIR}/.git" ]]; then
  if git -C "$REPO_DIR" pull -q --ff-only; then
    ok "updated ${REPO_DIR}"
  else
    note "could not fast-forward ${REPO_DIR} (local changes?) - using as is"
  fi
else
  git clone -q "$REPO_URL" "$REPO_DIR" \
    || die "clone failed. If the repo is private: git clone https://<user>@github.com/vuckovic/TrafficGenerator.git ${REPO_DIR} (token as password), then re-run"
  ok "cloned into ${REPO_DIR}"
fi
DEPLOY="${REPO_DIR}/deploy"
ENVF="${DEPLOY}/cluster.env"
if [[ ! -f "$ENVF" ]]; then
  cp "${DEPLOY}/cluster.env.example" "$ENVF"
  ok "created deploy/cluster.env from example"
fi
setvar() {  # key value -> replace KEY=... line in cluster.env
  sed -i "s|^$1=.*|$1=\"$2\"|" "$ENVF"
}
[[ -n "$SSH_USER_ARG"     ]] && setvar SSH_USER "$SSH_USER_ARG"
[[ -n "$ROUTE_SERVER_ARG" ]] && setvar ROUTE_SERVER_IP "$ROUTE_SERVER_ARG"
[[ -n "$SINK_ARG"         ]] && setvar SINK_IP "$SINK_ARG"
# shellcheck source=cluster.env.example
source "$ENVF"
SSH_USER="${SSH_USER:-$(id -un)}"
[[ "$SSH_USER" == "$(id -un)" ]] || note "SSH_USER=${SSH_USER} differs from current user $(id -un)"
mgmt_ip() { echo "${MGMT_PREFIX}.$((MGMT_HOST_BASE + $1))"; }
NAMES=("${NODE_HOSTNAMES[@]}")
ok "cluster.env: ${#NAMES[@]} nodes, user=${SSH_USER}, route-server=${ROUTE_SERVER_IP:-unset}, sink=${SINK_IP:-unset}"

# ---------------------------------------------------- 3. SSH key to all -----
log "--- 3/5 SSH key distribution"
KEY="${HOME}/.ssh/id_ed25519"
if [[ ! -f "$KEY" ]]; then
  mkdir -p "${HOME}/.ssh" && chmod 700 "${HOME}/.ssh"
  ssh-keygen -q -t ed25519 -N "" -C "tgen-controller@$(hostname)" -f "$KEY"
  ok "created ${KEY}"
fi
SSH_OPTS=(-o ConnectTimeout=5 -o StrictHostKeyChecking=accept-new)
PW=""
for (( n=1; n<=NODE_COUNT; n++ )); do
  ip="$(mgmt_ip "$n")"; name="${NAMES[$((n-1))]}"
  if ssh "${SSH_OPTS[@]}" -o BatchMode=yes "${SSH_USER}@${ip}" true 2>/dev/null; then
    ok "${name} (${ip}) key login already works"
    continue
  fi
  if ! ping -c1 -W2 "$ip" >/dev/null 2>&1; then bad "${name} (${ip}) not reachable (ping)"; continue; fi
  if [[ -z "$PW" ]]; then
    read -r -s -p "  password for ${SSH_USER} on the nodes (asked once, not stored): " PW </dev/tty; echo
  fi
  if SSHPASS="$PW" sshpass -e ssh-copy-id -i "${KEY}.pub" "${SSH_OPTS[@]}" "${SSH_USER}@${ip}" >/dev/null 2>&1 \
     && ssh "${SSH_OPTS[@]}" -o BatchMode=yes "${SSH_USER}@${ip}" true 2>/dev/null; then
    ok "${name} (${ip}) key installed"
  else
    bad "${name} (${ip}) key install failed (wrong password? PasswordAuthentication off?)"
  fi
done
unset PW

# ------------------------------------------------------ 4. node checks ------
log "--- 4/5 node checks"
printf '  %-9s %-15s %-4s %-8s %-4s %-9s %-4s %-4s %-5s %-6s %s\n' NODE IP SSH OS RAM LINK SUDO NET INST TRAFIP MODEL | tee -a "$LOG"
ALL_OK=1; NEED_K=0; NOT_INSTALLED=()
for (( n=1; n<=NODE_COUNT; n++ )); do
  ip="$(mgmt_ip "$n")"; name="${NAMES[$((n-1))]}"
  tip="${TRAFFIC_PREFIX}.$((TRAFFIC_HOST_BASE + n))"
  info=$(ssh "${SSH_OPTS[@]}" -o BatchMode=yes "${SSH_USER}@${ip}" "
    . /etc/os-release
    printf 'host=%s\n' \"\$(hostname)\"
    printf 'os=%s\n' \"\$VERSION_CODENAME\"
    printf 'ram=%s\n' \"\$(awk '/MemTotal/{printf \"%d\", \$2/1048576+0.5}' /proc/meminfo)G\"
    printf 'link=%s\n' \"\$(cat /sys/class/net/${MGMT_IFACE}/speed 2>/dev/null)/\$(cat /sys/class/net/${MGMT_IFACE}/duplex 2>/dev/null)\"
    printf 'sudo=%s\n' \"\$(sudo -n true 2>/dev/null && echo yes || echo pw)\"
    printf 'net=%s\n' \"\$(timeout 5 getent hosts deb.debian.org >/dev/null && echo yes || echo no)\"
    printf 'inst=%s\n' \"\$(test -f /etc/tgen/node.conf && echo yes || echo no)\"
    printf 'tip=%s\n' \"\$(ip -4 -o addr show dev ${MGMT_IFACE} | grep -q ' ${tip}/' && echo yes || echo no)\"
    printf 'model=%s\n' \"\$(tr -d '\0' </proc/device-tree/model | sed 's/Raspberry Pi //')\"
  " 2>/dev/null) || { printf '  %-9s %-15s %s\n' "$name" "$ip" "${c_bad}no SSH${c_off}" | tee -a "$LOG"; ALL_OK=0; continue; }
  g() { echo "$info" | sed -n "s/^$1=//p"; }
  printf '  %-9s %-15s %-4s %-8s %-4s %-9s %-4s %-4s %-5s %-6s %s\n' \
    "$name" "$ip" yes "$(g os)" "$(g ram)" "$(g link)" "$(g sudo)" "$(g net)" "$(g inst)" "$(g tip)" "$(g model)" | tee -a "$LOG"
  [[ "$(g inst)" == yes ]] || { NOT_INSTALLED+=("$name"); }
  [[ "$(g host)" == "$name" ]] || { note "${ip} reports hostname '$(g host)', expected ${name}"; }
  [[ "$(g os)" == trixie || "$(g os)" == bookworm ]] || { note "${name}: unsupported OS '$(g os)'"; ALL_OK=0; }
  [[ "$(g link)" == "1000/full" ]] || note "${name}: link $(g link), expected 1000/full"
  [[ "$(g net)" == yes ]] || { note "${name}: no internet/DNS - installer needs it for apt"; ALL_OK=0; }
  [[ "$(g sudo)" == yes ]] || NEED_K=1
  expected_ram="${NODE_RAM_GB[$((n-1))]}G"
  [[ "$(g ram)" == "$expected_ram" ]] || note "${name}: RAM $(g ram), cluster.env says ${expected_ram}"
done
(( NEED_K )) && note "some nodes need a sudo password: Ansible will ask for it (BECOME password)"
if (( ${#NOT_INSTALLED[@]} )); then
  note "installer has NOT run on: ${NOT_INSTALLED[*]}  -> run with --install (tests will fail until then)"
fi

# Ansible connectivity
cd "${DEPLOY}/ansible"
if ansible tgen -m ping -e "ansible_user=${SSH_USER}" -e ansible_become=false -o >/tmp/tgen_ansible_ping.txt 2>&1; then
  ok "ansible ping: all ${NODE_COUNT} nodes SUCCESS"
else
  bad "ansible ping failed for some nodes:"; grep -v SUCCESS /tmp/tgen_ansible_ping.txt | sed 's/^/       /' | tee -a "$LOG"
  ALL_OK=0
fi

# ---------------------------------------------------- 5. install / test -----
log "--- 5/5 next steps"
BECOME=(); (( NEED_K )) && BECOME=(--ask-become-pass)
if (( DO_INSTALL )); then
  (( ALL_OK )) || die "fix the failures above before --install"
  log "running installer on all nodes via Ansible (takes several minutes)..."
  ansible-playbook site.yml -e reboot=true -e "ansible_user=${SSH_USER}" "${BECOME[@]}" 2>&1 | tee -a "$LOG"
  ok "installation finished"
fi
if (( DO_TEST )); then
  log "running cluster connectivity tests..."
  "${REPO_DIR}/tests/cluster/connectivity_test.sh" --config "$ENVF" || true
fi
if (( ! DO_INSTALL && ! DO_TEST )); then
  echo
  echo "Controller ready. Next:"
  echo "  1) install all nodes : bash ${DEPLOY}/controller-bootstrap.sh --install"
  echo "  2) run the test suite: bash ${DEPLOY}/controller-bootstrap.sh --test"
  echo "     (or both at once with --install --test)"
fi
log "log written to ${LOG}"
