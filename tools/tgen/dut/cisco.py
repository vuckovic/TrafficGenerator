# SPDX-License-Identifier: MIT
"""Cisco IOS 15.x / IOS-XE 16.x-17.x (releases from 2015 on) as DUT.

Transport: Netmiko (prompt handling, paging, enable mode); Debian package
python3-netmiko. Enable secret, if the user is not privilege 15, from
TGEN_DUT_ENABLE (or prompted).

Configuration goes into the running config only - nothing is written to
startup-config, so a reload also removes it. Each neighbour carries the
description "tgen ..."; removal deletes exactly those neighbours.

Roles (all under `address-family ipv4`):
  route_server    -> neighbor X route-server-client   (BGP Route Server feature:
                     IOS-XE 3.3S+/16.x+, IOS 15.2S+; no AS prepend, next hop kept)
  route_reflector -> neighbor X route-reflector-client (remote-as = DUT AS)
  router          -> neighbor X next-hop-self
IOS-XR and NX-OS use a different syntax and are refused.

Old releases (Cisco 1841, IOS 15.1(4)M): SSH there offers only SHA-1 key
exchange and ssh-rsa host keys. ssh_legacy=True (inventory) checks that the
installed paramiko still has them, keeps key authentication on ssh-rsa and
allows more time per command (a 1841 is slow to print its configuration).
"""
from __future__ import annotations

import getpass
import os
import re
import time

from .base import TAG, Dut, DutError

ROLE_AF = {"route_server": "route-server-client", "route_reflector": "route-reflector-client",
           "router": "next-hop-self"}


class Cisco(Dut):
    platform = "Cisco IOS / IOS-XE"
    config_ext = "ios"

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.secret = os.environ.get("TGEN_DUT_ENABLE")
        self._conn = None
        self.legacy = bool(self.opts.get("ssh_legacy"))

    # ------------------------------------------------------------- transport
    def _connect(self):
        if self._conn is not None and self._conn.is_alive():
            return self._conn
        try:
            from netmiko import ConnectHandler
        except ImportError as e:
            raise DutError("Cisco support needs Netmiko: sudo apt-get install -y python3-netmiko") from e
        params = dict(device_type="cisco_ios", host=self.host, port=self.port, username=self.user,
                      fast_cli=False, conn_timeout=15, auth_timeout=20)
        if self.legacy:
            check_legacy_ssh()
            # old IOS only knows ssh-rsa signatures for public keys; slow CPU: more time per command
            params.update(disabled_algorithms={"pubkeys": ["rsa-sha2-512", "rsa-sha2-256"]},
                          global_delay_factor=2, conn_timeout=30, auth_timeout=40)
        if self.password:
            params["password"] = self.password
        if self.key:
            params.update(use_keys=True, key_file=self.key)
        elif not self.password:
            params["use_keys"] = True     # ssh-agent / default keys
        if self.secret:
            params["secret"] = self.secret
        try:
            c = ConnectHandler(**params)
        except Exception as e:  # netmiko raises several exception types
            raise DutError(f"{self.host}: SSH login failed: {e}") from e
        if not c.check_enable_mode():
            if self.secret is None and os.isatty(0):
                self.secret = getpass.getpass(f"enable secret for {self.host} (not stored): ")
                c.secret = self.secret
            try:
                c.enable()
            except Exception as e:
                raise DutError(f"{self.host}: cannot enter enable mode (set TGEN_DUT_ENABLE or use a "
                               f"privilege-15 user): {e}") from e
        self._conn = c
        return c

    def cmd(self, command: str, timeout: float = 60) -> str:
        if self.legacy:
            timeout *= 2
        try:
            out = self._connect().send_command(command, read_timeout=self._timeout(timeout))
        except DutError:
            raise
        except Exception as e:  # netmiko ReadTimeout and friends
            self.close()
            raise DutError(f"{self.host}: `{command}`: {e}") from e
        if re.search(r"^% (Invalid|Incomplete|Ambiguous)", out, re.M):
            raise DutError(f"{self.host}: `{command}`: {out.strip()[:200]}")
        return out

    def close(self) -> None:
        if self._conn is not None:
            self._conn.disconnect()
            self._conn = None

    # ------------------------------------------------------------- facts
    def resources(self) -> dict:
        v = self.cmd("show version")
        if re.search(r"IOS XR|NX-OS|Nexus", v):
            raise DutError("IOS-XR / NX-OS are not supported (different BGP syntax); IOS or IOS-XE required")
        m = re.search(r"Version\s+([0-9][^\s,]*)", v)
        if not m:
            raise DutError("could not read the IOS version (is this a Cisco IOS/IOS-XE device?)")
        ver = m.group(1)
        xe = "IOS XE" in v or "IOS-XE" in v
        major = int(re.match(r"(\d+)", ver).group(1))
        if not xe and major < 15:
            raise DutError(f"IOS {ver}: releases from 2015 on (IOS 15.x, IOS-XE) are supported")
        # "cisco ASR-920-24SZ-M (Freescale P2020) processor ..." / "Cisco 1841 (revision 7.0) with ..."
        model = re.search(r"^[Cc]isco (\S+) \(.*\) (?:processor|with)", v, re.M)
        cpu = self.cmd("show processes cpu | include CPU utilization")
        cpu5 = re.search(r"five seconds:\s*(\d+)%", cpu)
        mem = self.cmd("show processes memory | include Processor")
        mm = re.search(r"Total:\s*(\d+)\s+Used:\s*(\d+)\s+Free:\s*(\d+)", mem)
        r = {"version": ver, "train": "IOS-XE" if xe else "IOS", "model": model.group(1) if model else "",
             "cpu_load": f"{cpu5.group(1)}%" if cpu5 else "",
             "total_memory": f"{int(mm.group(1)) // 1048576}MiB" if mm else "",
             "free_memory": f"{int(mm.group(3)) // 1048576}MiB" if mm else "",
             "free_pct": round(100 * int(mm.group(3)) / max(1, int(mm.group(1))), 1) if mm else None}
        r["summary"] = (f"Cisco {r['model']} {r['train']} {ver}, CPU {r['cpu_load']}, "
                        f"processor memory free {r['free_memory']} of {r['total_memory']}")
        return r

    def interface_info(self, peer_ip: str, name: str | None = None) -> dict:
        """The port carrying peer_ip: speed, duplex and error counters (read before and
        after a run; a 100 Mb/s port with a duplex mismatch shows late collisions/CRC)."""
        if not name:
            cfg = self.cmd("show running-config | include ^interface|^ ip address", timeout=120)
            cur = None
            for line in cfg.splitlines():
                m = re.match(r"interface (\S+)", line)
                if m:
                    cur = m.group(1)
                elif cur and re.match(rf"\s+ip address {re.escape(peer_ip)}\s", line):
                    name = cur
                    break
            if not name:
                raise DutError(f"no interface has {peer_ip} configured (add it, or set `interface` in the inventory)")
        return parse_interface(self.cmd(f"show interfaces {name}"), name)

    # ---------------------------------------------------------------- render
    def render(self, meta: dict, dut_ip: str, dut_asn: int, role: str, hold_time: int = 90) -> str:
        ka = max(1, hold_time // 3)
        out = [f"! TrafficGenerator run {meta['run_id']}: {len(meta['routers'])} emulated routers, role {role}",
               "! generated - running-config only; neighbours removed by the tool after the run",
               f"router bgp {dut_asn}",
               " bgp log-neighbor-changes"]
        for r in meta["routers"]:
            ip, asn = r["ip"], dut_asn if role == "route_reflector" else r["asn"]
            out += [f" neighbor {ip} remote-as {asn}",
                    f" neighbor {ip} description {TAG} {meta['run_id']} {r['name']} node {r['node']}",
                    f" neighbor {ip} transport connection-mode passive",
                    f" neighbor {ip} timers {ka} {hold_time}"]
        out.append(" address-family ipv4")
        for r in meta["routers"]:
            out += [f"  neighbor {r['ip']} activate", f"  neighbor {r['ip']} {ROLE_AF[role]}"]
        out.append(" exit-address-family")
        return "\n".join(out) + "\n"

    # ---------------------------------------------------------------- config
    def _bgp(self) -> tuple[str | None, list[str]]:
        cfg = self.cmd("show running-config | section router bgp", timeout=120)
        asn = re.search(r"^router bgp (\d+)", cfg, re.M)
        ips = re.findall(rf"^\s*neighbor (\S+) description {TAG}\b", cfg, re.M)
        return (asn.group(1) if asn else None), ips

    def _config(self, lines: list[str]) -> str:
        c = self._connect()
        out = c.send_config_set(lines, read_timeout=max(60, len(lines) * 0.5), cmd_verify=False)
        bad = [l for l in out.splitlines() if re.match(r"\s*% ", l)]
        if bad:
            # which commands were refused: the echoed command line just before each "% ..." line
            olines = out.splitlines()
            refused = [olines[i - 1].split("#", 1)[-1].strip() for i, l in enumerate(olines)
                       if re.match(r"\s*% ", l) and i > 0]
            hint = (" - route-server-client needs the BGP Route Server feature (IOS-XE 3.3S+/IOS 15.2S+), which this "
                    "device does not have; use --role router" if any("route-server-client" in r for r in refused) else "")
            raise DutError(f"{self.host} rejected {len(bad)} configuration line(s), e.g. `{refused[0] if refused else ''}`: "
                           f"{bad[0].strip()}{hint}")
        return out

    def push(self, config: str, expected: int) -> int:
        asn, _ = self._bgp()
        want = re.search(r"^router bgp (\d+)", config, re.M).group(1)
        if asn and asn != want:
            raise DutError(f"the device already runs `router bgp {asn}`; set --dut-asn {asn}")
        if self.count():                 # leftovers of an aborted or kept run
            self.notes.append("removed tgen neighbours left from an earlier run")
            self.remove()
        self._config([l for l in config.splitlines() if l.strip() and not l.startswith("!")])
        n = self.count()
        if n != expected:
            raise DutError(f"device has {n} tgen neighbours, expected {expected}")
        self.pushed_at = time.time()
        return n

    def count(self) -> int:
        return len(self._bgp()[1])

    def remove(self) -> None:
        asn, ips = self._bgp()
        if ips:
            self._config([f"router bgp {asn}", *[f" no neighbor {ip}" for ip in ips]])
        left = self.count()
        if left:
            raise DutError(f"{left} tgen neighbours still configured after removal")

    def sessions(self) -> tuple[str, int]:
        _, ips = self._bgp()
        out = self.cmd("show ip bgp summary", timeout=120)
        mine, est = set(ips), 0
        for line in out.splitlines():
            f = line.split()
            if f and f[0] in mine and len(f) >= 10 and f[9].isdigit():
                est += 1
        return out, est

    def log(self) -> str:
        return self.cmd("show logging | include BGP", timeout=120)


def check_legacy_ssh() -> None:
    """IOS 15.1 and older offer only SHA-1 key exchange; recent paramiko releases dropped it."""
    try:
        import paramiko
    except ImportError:
        return
    kex = set(getattr(paramiko.Transport, "_kex_info", {}))
    if not kex & {"diffie-hellman-group14-sha1", "diffie-hellman-group1-sha1", "diffie-hellman-group-exchange-sha1"}:
        raise DutError(f"paramiko {paramiko.__version__} has no SHA-1 key exchange, which this old IOS needs for SSH; "
                       "use Debian's python3-paramiko (3.x) on the controller")


COUNTERS = {
    "input_errors": r"(\d+) input errors", "crc": r"(\d+) CRC", "overrun": r"(\d+) overrun",
    "ignored": r"(\d+) ignored", "output_errors": r"(\d+) output errors", "collisions": r"(\d+) collisions",
    "late_collision": r"(\d+) late collision", "output_drops": r"Total output drops: (\d+)",
    "input_queue_drops": r"Input queue: \d+/\d+/(\d+)/", "packets_in": r"(\d+) packets input",
    "packets_out": r"(\d+) packets output",
}


def parse_interface(text: str, name: str) -> dict:
    """`show interfaces X` (IOS 15 / IOS-XE) -> speed, duplex, counters."""
    r: dict = {"interface": name, "up": bool(re.search(r"line protocol is up", text))}
    m = re.search(r"(Full|Half|Auto)[- ][Dd]uplex,\s*(\d+)\s*(Mb/s|Mbps|Gb/s|Gbps)", text)
    if m:
        r["duplex"] = m.group(1).lower()
        r["speed_mbps"] = int(m.group(2)) * (1000 if m.group(3).startswith("G") else 1)
    else:
        bw = re.search(r"BW (\d+) Kbit", text)
        r["duplex"], r["speed_mbps"] = None, int(bw.group(1)) // 1000 if bw else None
    for k, rx in COUNTERS.items():
        m = re.search(rx, text)
        r[k] = int(m.group(1)) if m else None
    return r


def interface_delta(before: dict, after: dict) -> dict:
    """Counter growth during the run (errors and drops only)."""
    keys = [k for k in COUNTERS if k not in ("packets_in", "packets_out")]
    return {k: after[k] - before[k] for k in keys
            if before.get(k) is not None and after.get(k) is not None and after[k] != before[k]}
