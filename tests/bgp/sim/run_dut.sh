#!/usr/bin/env bash
# Runs the simulated cluster against an external DUT adapter:
#   bird-ssh, frr : a REAL BIRD / FRR daemon, reached through the fake ssh as host "dut-linux"
#   cisco         : mock Netmiko + IOS-XE (tests/bgp/sim/pymock), translated to BIRD
# Usage: tests/bgp/sim/run_dut.sh bird-ssh|frr|cisco [tgen_p2 args...]
# Needs root, bird (bird2) and/or frr installed.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
kind="$1"; shift
dut_ip=10.100.0.111
fail() { echo "FAIL: $*" >&2; exit 1; }

case "$kind" in
bird-ssh)
  ip addr replace "$dut_ip/32" dev lo
  T="$(mktemp -d /tmp/tgen-dutbird-XXXX)"
  # a "user's" BIRD: main config WITHOUT the include line - the adapter must add it
  printf 'router id %s;\nprotocol device {}\n' "$dut_ip" > "$T/bird.conf"
  bird -c "$T/bird.conf" -s "$T/bird.ctl" -P "$T/bird.pid"
  trap 'kill "$(cat "$T/bird.pid")" 2>/dev/null || true' EXIT
  "$here/run_sim.sh" --dut bird-ssh --dut-ip "$dut_ip" --dut-ssh-host dut-linux \
      --dut-bird-include "$T/tgen.conf" --dut-bird-conf "$T/bird.conf" --dut-bird-socket "$T/bird.ctl" "$@"
  grep -q "include \"$T/tgen.conf\";" "$T/bird.conf" || fail "include line not added"
  n=$(birdc -s "$T/bird.ctl" show protocols | grep -c '^tgen_' || true)
  [[ "$n" == 0 ]] || fail "$n tgen protocols left in BIRD"
  echo "DUT cleanup check: include present, 0 tgen protocols left"
  ;;
frr)
  # FRR in its own network namespace (a router on the same box would treat the
  # emulated routers' next hops as its own addresses), linked by a veth pair
  export SIM_DUT_NETNS=tgdut; ns=tgdut
  ip addr del "$dut_ip/32" dev lo 2>/dev/null || true
  ip netns del $ns 2>/dev/null || true; ip link del tgv0 2>/dev/null || true
  ip netns add $ns; ip link add tgv0 type veth peer name tgv1; ip link set tgv1 netns $ns
  ip link set tgv0 up; ip route replace "$dut_ip/32" dev tgv0
  ip -n $ns addr add "$dut_ip/24" dev tgv1; ip -n $ns link set tgv1 up; ip -n $ns link set lo up
  mkdir -p /etc/frr/$ns; cp /etc/frr/daemons /etc/frr/vtysh.conf /etc/frr/$ns/
  sed -i 's/^bgpd=no/bgpd=yes/' /etc/frr/$ns/daemons
  printf 'frr defaults traditional\nhostname dut\n' > /etc/frr/$ns/frr.conf; chown -R frr:frr /etc/frr/$ns
  cleanup() { ip netns exec $ns /usr/lib/frr/frrinit.sh stop $ns >/dev/null 2>&1 || true
              ip netns del $ns 2>/dev/null || true; ip link del tgv0 2>/dev/null || true; }
  trap cleanup EXIT
  ip netns exec $ns /usr/lib/frr/frrinit.sh start $ns >/dev/null
  for _ in $(seq 20); do vtysh -N $ns -c 'show version' >/dev/null 2>&1 && break; sleep 0.5; done
  vtysh -N $ns -c 'show version' | head -1
  "$here/run_sim.sh" --dut frr --dut-ip "$dut_ip" --dut-ssh-host dut-linux "$@"
  n=$(vtysh -N $ns -c 'show running-config' | grep -c 'description tgen' || true)
  [[ "$n" == 0 ]] || fail "$n tgen neighbours left in FRR"
  vtysh -N $ns -c 'show running-config' | grep -q 'route-map TGEN-ALL' && fail "route-map TGEN-ALL left"
  echo "DUT cleanup check: 0 tgen neighbours, no route-map left"
  ;;
cisco)
  export PYTHONPATH="$here/pymock${PYTHONPATH:+:$PYTHONPATH}"
  TGEN_DUT_PASSWORD="${TGEN_DUT_PASSWORD:-dummy}" "$here/run_sim.sh" --dut cisco --dut-ip "$dut_ip" "$@"
  st="$(ls -td /tmp/tgen-sim-* | head -1)/$dut_ip/ios-bgp.json"
  python3 -c "import json,sys; n=json.load(open(sys.argv[1]))['nb']; sys.exit(f'FAIL: {len(n)} neighbours left') if n else print('DUT cleanup check: 0 tgen neighbours left')" "$st"
  kill "$(cat "$(dirname "$st")/bird.pid" 2>/dev/null)" 2>/dev/null || true
  ;;
*) echo "usage: $0 bird-ssh|frr|cisco [args]"; exit 2 ;;
esac
