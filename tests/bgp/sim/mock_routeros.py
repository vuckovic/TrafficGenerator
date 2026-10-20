#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Mock RouterOS 7 for the loopback simulation (used by the fake ssh).

Understands the commands tools/tgen/dut/mikrotik.py sends. On /import it parses
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
    if os.environ.get("SIM_IMPORT_FAIL"):          # test: RouterOS rejects the script
        if "verbose=yes" in cmd:
            print("/routing bgp connection add name=tgen-R01 ...\nexpected end of command (line 4 column 31)")
        sys.exit(0)
    rsc = (home / "tgen-bgp.rsc").read_text()
    adds = [l[len("/routing bgp connection add "):] for l in rsc.splitlines() if l.startswith("/routing bgp connection add ")]
    state.write_text("\n".join(adds) + "\n")
    start_bird(connections())
    print("\nScript file loaded and executed successfully")
elif cmd.startswith("/file remove"):
    (home / "tgen-bgp.rsc").unlink(missing_ok=True)
elif cmd.startswith("/routing bgp session print") and os.environ.get("SIM_DUT_HANG_SESSIONS"):
    import time as _t
    _t.sleep(3600)                                # test: DUT stops answering session queries
elif cmd.startswith("/routing bgp session print count-only"):
    out = subprocess.run(["birdc", "-s", str(home / "bird.ctl"), "show", "protocols"], capture_output=True, text=True).stdout
    print(sum(1 for line in out.splitlines() if " BGP " in line and "Established" in line))
elif cmd.startswith("/log print count-only"):
    print(3)
elif "count-only" in cmd:
    print(len(connections()))
elif cmd.startswith("/system logging remove"):
    (home / "logging.txt").write_text("")
elif cmd.startswith("/system logging add"):
    if "comment=" in cmd:                          # as RouterOS 7.11: logging rules have no comment
        print(f"expected end of command (line 1 column {cmd.index('comment=') + 1})")
    else:
        (home / "logging.txt").write_text(cmd)
elif cmd.startswith("/system logging print count-only"):
    print(1 if (home / "logging.txt").exists() and (home / "logging.txt").read_text() else 0)
elif cmd.startswith("/routing bgp connection remove"):
    state.write_text("")
    start_bird([])
elif cmd.startswith("/routing bgp session print"):
    out = subprocess.run(["birdc", "-s", str(home / "bird.ctl"), "show", "protocols"], capture_output=True, text=True).stdout
    for line in out.splitlines():
        if " BGP " in line:
            print(f'name="{line.split()[0]}" ' + ("established" if "Established" in line else "idle"))
elif cmd.startswith("/log print terse"):
    print(" 01-02 00:40:28 system,info tgen-R01 added by admin\n 01-02 00:44:19 system,info tgen-R01 removed by admin")
    print(" 06:11:05 system,info,account user admin logged in from 10.100.0.11 via ssh")
    import time as _t
    hhmmss = _t.strftime("%H:%M:%S")
    print(f" {hhmmss} system,info,account user admin logged in from 10.100.0.11 via ssh")
    for line in ((home / "bird.log").read_text().splitlines() if (home / "bird.log").exists() else []):
        print(f" {hhmmss} route,bgp,debug tgen {line}")
else:
    print(f"bad command name ({cmd})", file=sys.stderr)
    sys.exit(1)
