#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Device-under-test configuration over SSH, without running a benchmark.

The same adapters tgen_p2.py uses before and after every run; the web UI's
DUT configuration buttons (Test connection / Preview / Push / Remove / Status)
call these actions.

  tools/tgen_dut.py --dut-profile asr920 test        # a device from deploy/duts.toml
  tools/tgen_dut.py --dut mikrotik --dut-ip 10.100.0.111 test
  tools/tgen_dut.py --dut cisco --dut-ip 10.100.0.1 --routers 12 preview
  tools/tgen_dut.py --dut frr --dut-ip 10.100.0.30 --routers 12 --role router push
  tools/tgen_dut.py --dut frr --dut-ip 10.100.0.30 status
  tools/tgen_dut.py --dut frr --dut-ip 10.100.0.30 remove

preview / push use the same router addresses and AS numbers a run with the
same --routers, --seed and nodes would use. A benchmark run pushes its own
configuration anyway (replacing earlier tgen neighbours), so push is only
needed to pre-check a device. Password: TGEN_DUT_PASSWORD or prompt.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "tools"))
from tgen.dut import ADAPTERS, DutError, make  # noqa: E402
from tgen.planner import NodeSpec, Scenario, build  # noqa: E402
from tgen_p2 import load_env  # noqa: E402
from tgen import inventory  # noqa: E402


def plan_meta(args) -> dict:
    env = load_env(Path(args.config))
    names = env["NODE_HOSTNAMES"]
    prefix = env.get("TRAFFIC_PREFIX", "10.100.0")
    node_ips = [f"{prefix}.{11 + i}" for i in range(len(names))]
    nodes = [NodeSpec(n, 0.7 if i == 0 else 1.0) for i, n in enumerate(names)]
    sc = Scenario(run_id="tgen-dut-preview", dut_ip=args.dut_ip, dut_asn=args.dut_asn, role=args.role,
                  routers_total=args.routers, prefixes_per_router=1, seed=args.seed,
                  skip_ips=node_ips + env["TRAFFIC_RESERVED"] + inventory.reserved_ips())
    return build(sc, nodes)[1]


# default DUT role per platform: a device that cannot act as a transparent route
# server is tested in the role it really has (MikroTik RouterOS 7 adds its own AS)
DEFAULT_ROLE = {"mikrotik": "router"}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("action", choices=["test", "preview", "push", "remove", "status"])
    ap.add_argument("--config", default=str(REPO / "deploy" / "cluster.env"))
    ap.add_argument("--dut-profile", default=None, help="device from deploy/duts.toml (options given here win)")
    ap.add_argument("--dut", default=None, choices=list(ADAPTERS))
    ap.add_argument("--dut-ip", default=None, help="DUT BGP peering address")
    ap.add_argument("--dut-ssh-host", default="", help="SSH/management address if not --dut-ip")
    ap.add_argument("--dut-port", type=int, default=22)
    ap.add_argument("--dut-user", default=None)
    ap.add_argument("--dut-key", default=None)
    ap.add_argument("--dut-no-sudo", action="store_true")
    ap.add_argument("--dut-bird-include", default=None)
    ap.add_argument("--dut-bird-conf", default=None)
    ap.add_argument("--dut-bird-socket", default=None)
    ap.add_argument("--dut-asn", type=int, default=64500)
    ap.add_argument("--dut-ssh-legacy", action="store_true", help="old IOS (e.g. 1841, IOS 15.1): SHA-1 SSH")
    ap.add_argument("--dut-mrai", type=float, default=None,
                    help="Cisco: set the advertisement interval (MRAI) of every tgen neighbour, in s "
                         "(default: leave the device default, 30 s for eBGP on IOS)")
    ap.add_argument("--dut-interface", default=None, help="DUT port carrying --dut-ip (default: found automatically)")
    ap.add_argument("--role", default=None, choices=["route_server", "route_reflector", "router"],
                    help="DUT role (default: router for mikrotik, route_server otherwise)")
    ap.add_argument("--routers", type=int, default=60)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--json", action="store_true", help="machine-readable output (for the web UI)")
    inventory.apply_profile(ap)
    args = ap.parse_args()
    if not args.dut or not args.dut_ip:
        ap.error("give --dut-profile, or --dut and --dut-ip")
    args.role = args.role or DEFAULT_ROLE.get(args.dut, "route_server")

    dut = make(args.dut, args.dut_ssh_host or args.dut_ip, args.dut_user, port=args.dut_port, key=args.dut_key,
               no_sudo=args.dut_no_sudo, bird_include=args.dut_bird_include, bird_conf=args.dut_bird_conf,
               bird_socket=args.dut_bird_socket, ssh_legacy=args.dut_ssh_legacy, mrai=args.dut_mrai)
    result: dict = {"dut": args.dut, "platform": dut.platform, "action": args.action}
    try:
        if args.action == "preview":
            result["config"] = dut.render(plan_meta(args), args.dut_ip, args.dut_asn, args.role)
        else:
            dut.ask_password_if_needed()
            if args.action == "test":
                result["resources"] = dut.resources()
                result["tgen_neighbours"] = dut.count()
                try:
                    result["port"] = dut.interface_info(args.dut_ip, args.dut_interface)
                except DutError as e:
                    dut.notes.append(f"port check: {e}")
            elif args.action == "push":
                dut.resources()                                  # version / platform check first
                meta = plan_meta(args)
                result["config"] = dut.render(meta, args.dut_ip, args.dut_asn, args.role)
                try:
                    result["neighbours"] = dut.push(result["config"], len(meta["routers"]))
                except DutError:
                    try:
                        dut.remove()                             # never leave a half-applied configuration
                    except DutError:
                        pass
                    raise
            elif args.action == "remove":
                dut.remove()
                result["neighbours"] = dut.count()
            elif args.action == "status":
                text, est = dut.sessions()
                result.update(neighbours=dut.count(), established=est, sessions=text)
        result["notes"] = dut.notes
        result["ok"] = True
    except DutError as e:
        result.update(ok=False, error=str(e))

    if args.json:
        print(json.dumps(result, indent=2))
    elif not result["ok"]:
        print(f"ERROR: {result['error']}", file=sys.stderr)
    elif args.action == "preview":
        print(result["config"], end="")
    elif args.action == "test":
        print(f"OK  {result['resources']['summary']}\n    tgen neighbours configured now: {result['tgen_neighbours']}")
        if pt := result.get("port"):
            print(f"    port {pt['interface']} ({args.dut_ip}): {pt.get('speed_mbps') or '?'} Mb/s "
                  f"{pt.get('duplex') or '?'}-duplex, {'up' if pt['up'] else 'DOWN'}; input errors {pt.get('input_errors')}, "
                  f"CRC {pt.get('crc')}, late collisions {pt.get('late_collision')}, output drops {pt.get('output_drops')}")
    elif args.action == "push":
        print(f"OK  {result['neighbours']} neighbours configured ({args.role}, AS {args.dut_asn})")
    elif args.action == "remove":
        print(f"OK  tgen neighbours left: {result['neighbours']}")
    else:
        print(result["sessions"], end="")
        print(f"tgen neighbours {result['neighbours']}, Established {result['established']}")
    for n in result.get("notes", []):
        print(f"note: {n}", file=sys.stderr)
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
