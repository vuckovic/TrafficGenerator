#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Mock RouterOS 7 for the loopback simulation (used by the fake ssh).

Understands the commands tools/tgen/dut_mikrotik.py sends. On /import it parses
the uploaded tgen-bgp.rsc and starts BIRD on the DUT address with exactly the
neighbours, AS numbers and roles the script defines - so a run only works if
the generated RouterOS script is complete and consistent.
"""
import os
import re
import subprocess
import sys
from pathlib import Path

home = Path(os.environ["MOCK_HOME"])
state = home / "connections.txt"
cmd = " ".join(sys.argv[1:])


def connections() -> list[dict]:
    if not state.exists():
        return []
    return [dict(re.findall(r'(\S+?)=("[^"]*"|\S+)', line)) for line in state.read_text().splitlines() if line]


def start_bird(conns: list[dict]) -> None:
    subprocess.run(["pkill", "-f", "bird -c " + str(home)], capture_output=True)
    if not conns:
        return
    c0 = conns[0]
    ip, asn = c0["local.address"], c0["as"]
    subprocess.run(["ip", "addr", "replace", f"{ip}/32", "dev", "lo"], check=True)
    lines = [f'log "{home}/bird.log" all;', f"router id {c0['router-id']};", "protocol device {}"]
    for c in conns:
        rr = c["local.role"] == "ibgp-rr"
        nh = " next hop self;" if c["nexthop-choice"] == "force-self" else ""
        lines.append(
            f"protocol bgp {c['name'].replace('-', '_')} {{ local {ip} as {asn}; neighbor {c['remote.address']} "
            f"as {c['remote.as']}; passive on; multihop 2; hold time {int(c['hold-time'].rstrip('s'))};"
            f"{' rr client;' if rr else ''} ipv4 {{ import all; export all;{nh} }}; }}")
    (home / "bird.conf").write_text("\n".join(lines) + "\n")
    subprocess.run(["bird", "-c", str(home / "bird.conf"), "-s", str(home / "bird.ctl"), "-P", str(home / "bird.pid")],
                   check=True)


if cmd.startswith("/system resource print"):
    print("        version: 7.11 (stable)\n     board-name: CRS106-1C-5S\n            cpu: MIPS 24Kc V7.4\n"
          "       cpu-load: 3%\n    free-memory: 92.1MiB\n   total-memory: 128.0MiB")
elif cmd.startswith("/import file-name="):
    rsc = (home / "tgen-bgp.rsc").read_text()
    adds = [l[len("/routing bgp connection add "):] for l in rsc.splitlines() if l.startswith("/routing bgp connection add ")]
    state.write_text("\n".join(adds) + "\n")
    start_bird(connections())
    print("\nScript file loaded and executed successfully")
elif cmd.startswith("/file remove"):
    (home / "tgen-bgp.rsc").unlink(missing_ok=True)
elif "count-only" in cmd:
    print(len(connections()))
elif cmd.startswith("/routing bgp connection remove"):
    state.write_text("")
    start_bird([])
elif cmd.startswith("/routing bgp session print"):
    out = subprocess.run(["birdc", "-s", str(home / "bird.ctl"), "show", "protocols"], capture_output=True, text=True).stdout
    for line in out.splitlines():
        if " BGP " in line:
            print(f'name="{line.split()[0]}" ' + ("established" if "Established" in line else "idle"))
elif cmd.startswith("/log print"):
    print((home / "bird.log").read_text() if (home / "bird.log").exists() else "")
else:
    print(f"bad command name ({cmd})", file=sys.stderr)
    sys.exit(1)
