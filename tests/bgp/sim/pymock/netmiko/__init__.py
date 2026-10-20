# SPDX-License-Identifier: MIT
"""Mock Netmiko + Cisco IOS-XE for the loopback simulation (tests only).

Keeps the BGP part of an IOS running-config in $SIM_ROOT/<host>/ios-bgp.json,
accepts exactly the commands tools/tgen/dut/cisco.py may send (anything else
gets "% Invalid input detected at '^' marker.", as on IOS) and turns the
configuration into BIRD on the DUT address - so a Cisco run only passes when
the generated IOS configuration is complete and consistent (roles included).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

INVALID = "% Invalid input detected at '^' marker."


class ConnectHandler:
    def __init__(self, device_type: str, host: str, username: str, port: int = 22, password: str | None = None,
                 secret: str | None = None, **_):
        if device_type != "cisco_ios":
            raise ValueError(device_type)
        if not password and not _.get("use_keys"):
            raise RuntimeError("Authentication failed")
        self.home = Path(os.environ["SIM_ROOT"]) / host
        self.home.mkdir(parents=True, exist_ok=True)
        self.state_f = self.home / "ios-bgp.json"
        self.alive = True
        self.secret = secret
        self.privileged = username != "lowpriv"

    # --- netmiko API used by the adapter ------------------------------------
    def is_alive(self) -> bool:
        return self.alive

    def disconnect(self) -> None:
        self.alive = False

    def check_enable_mode(self) -> bool:
        return self.privileged

    def enable(self) -> None:
        if self.secret != os.environ.get("SIM_ENABLE_SECRET", "enable"):
            raise RuntimeError("Failed to enter enable mode")
        self.privileged = True

    def send_command(self, cmd: str, read_timeout: float = 10, **_) -> str:
        st = self._state()
        if cmd == "show version":
            return ("Cisco IOS XE Software, Version 17.09.04a\nCisco IOS Software [Cupertino], ISR Software "
                    "(X86_64_LINUX_IOSD-UNIVERSALK9-M), Version 17.9.4a, RELEASE SOFTWARE (fc3)\n"
                    "cisco ISR4331/K9 (1RU) processor with 1795999K/6147K bytes of memory.\n")
        if cmd == "show processes cpu | include CPU utilization":
            return "CPU utilization for five seconds: 4%/1%; one minute: 3%; five minutes: 3%\n"
        if cmd == "show processes memory | include Processor":
            return "Processor Pool Total: 1838842064 Used:  412346120 Free: 1426495944\n"
        if cmd == "show running-config | section router bgp":
            return self._running(st)
        if cmd == "show ip bgp summary":
            est = self._established()
            lines = [f"BGP router identifier 10.100.0.111, local AS number {st.get('asn')}",
                     "Neighbor        V           AS MsgRcvd MsgSent   TblVer  InQ OutQ Up/Down  State/PfxRcd"]
            for ip, n in st["nb"].items():
                lines.append(f"{ip:<15} 4 {n.get('as', 0):>12}      10      10        1    0    0 00:01:00 "
                             + ("5" if ip in est else "Active"))
            return "\n".join(lines) + "\n"
        if cmd == "show logging | include BGP":
            log = self.home / "bird.log"
            return log.read_text() if log.exists() else ""
        return INVALID + "\n"

    def send_config_set(self, lines: list[str], **_) -> str:
        st = self._state()
        out, ctx = [], "global"
        for raw in lines:
            line = raw.strip()
            out.append(f"R1(config)#{line}")
            m = re.fullmatch(r"router bgp (\d+)", line)
            if m:
                if st.get("asn") and st["asn"] != int(m.group(1)):
                    out.append(f"% BGP is already running; AS is {st['asn']}")
                    continue
                st["asn"], ctx = int(m.group(1)), "bgp"
                continue
            if ctx == "bgp" and line == "bgp log-neighbor-changes":
                continue
            if ctx == "bgp" and line == "address-family ipv4":
                ctx = "af"
                continue
            if ctx == "af" and line == "exit-address-family":
                ctx = "bgp"
                continue
            m = re.fullmatch(r"(no )?neighbor (\d+\.\d+\.\d+\.\d+) ?(.*)", line)
            if m and ctx in ("bgp", "af"):
                neg, ip, rest = m.groups()
                if neg and not rest:
                    st["nb"].pop(ip, None)
                    continue
                nb = st["nb"].setdefault(ip, {})
                if ctx == "bgp":
                    if (mm := re.fullmatch(r"remote-as (\d+)", rest)):
                        nb["as"] = int(mm.group(1))
                        continue
                    if (mm := re.fullmatch(r"description (.+)", rest)):
                        nb["desc"] = mm.group(1)
                        continue
                    if rest == "transport connection-mode passive":
                        nb["passive"] = True
                        continue
                    if (mm := re.fullmatch(r"timers (\d+) (\d+)", rest)):
                        nb["hold"] = int(mm.group(2))
                        continue
                elif rest in ("activate", "route-server-client", "route-reflector-client", "next-hop-self"):
                    nb[rest] = True
                    continue
            out.append(INVALID)
        self._save(st)
        self._apply(st)
        return "\n".join(out) + "\n"

    # --- internals ------------------------------------------------------------
    def _state(self) -> dict:
        return json.loads(self.state_f.read_text()) if self.state_f.exists() else {"asn": None, "nb": {}}

    def _save(self, st: dict) -> None:
        self.state_f.write_text(json.dumps(st))

    def _running(self, st: dict) -> str:
        if not st.get("asn"):
            return ""
        lines = [f"router bgp {st['asn']}", " bgp log-neighbor-changes"]
        for ip, n in st["nb"].items():
            lines.append(f" neighbor {ip} remote-as {n.get('as')}")
            if "desc" in n:
                lines.append(f" neighbor {ip} description {n['desc']}")
        lines.append(" address-family ipv4")
        for ip, n in st["nb"].items():
            for k in ("activate", "route-server-client", "route-reflector-client", "next-hop-self"):
                if n.get(k):
                    lines.append(f"  neighbor {ip} {k}")
        return "\n".join(lines) + "\n exit-address-family\n"

    def _established(self) -> set[str]:
        ctl = self.home / "bird.ctl"
        if not ctl.exists():
            return set()
        out = subprocess.run(["birdc", "-s", str(ctl), "show", "protocols", "all"], capture_output=True,
                             text=True).stdout
        est, cur = set(), None
        for line in out.splitlines():
            if line.startswith("n_"):
                cur = "Established" in line
            m = re.search(r"Neighbor address:\s+(\S+)", line)
            if m and cur:
                est.add(m.group(1))
        return est

    def _apply(self, st: dict) -> None:
        """IOS semantics -> BIRD: route-server-client = rs client (transparent),
        route-reflector-client = rr client, next-hop-self = next hop self.
        A neighbour without activate gets no IPv4 channel (as on IOS)."""
        subprocess.run(["pkill", "-f", f"bird -c {self.home}/"], capture_output=True)
        ip_dut = "10.100.0.111"
        subprocess.run(["ip", "addr", "replace", f"{ip_dut}/32", "dev", "lo"], check=True)
        conf = [f'log "{self.home}/bird.log" all;', f"router id {ip_dut};", "protocol device {}"]
        for ip, n in st["nb"].items():
            if not (n.get("as") and n.get("activate")):
                continue
            extra = (" rs client;" if n.get("route-server-client") else "") + \
                    (" rr client;" if n.get("route-reflector-client") else "") + \
                    (" passive on;" if n.get("passive") else "")
            nh = " next hop self;" if n.get("next-hop-self") else ""
            conf.append(f"protocol bgp n_{ip.replace('.', '_')} {{ local {ip_dut} as {st['asn']}; neighbor {ip} as "
                        f"{n['as']}; multihop 2; hold time {n.get('hold', 180)};{extra} "
                        f"ipv4 {{ import all; export all;{nh} }}; }}")
        (self.home / "bird.conf").write_text("\n".join(conf) + "\n")
        if len(conf) > 3:
            subprocess.run(["bird", "-c", str(self.home / "bird.conf"), "-s", str(self.home / "bird.ctl"),
                            "-P", str(self.home / "bird.pid")], check=True)
