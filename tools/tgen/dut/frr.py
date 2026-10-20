# SPDX-License-Identifier: MIT
"""FRRouting (and its predecessor Quagga) on a Linux host as DUT, via vtysh.

The configuration is a list of vtysh commands, applied in one vtysh session as
`vtysh -c ... -c ...` (works on FRR and Quagga; no `vtysh -f` needed). Every
neighbour gets the description "tgen ..."; removal parses the running config
for those descriptions and deletes exactly those neighbours plus the route-map
TGEN-ALL. eBGP policy: FRR >= 7.4 drops eBGP routes without a policy
(ebgp-requires-policy), so every neighbour gets route-map TGEN-ALL (permit all)
in and out - no global setting is changed.

Quagga is end-of-life (last release 1.2.4, 2018); the same commands are used,
tested against FRR 8.4 only.
"""
from __future__ import annotations

import re
import time

from .base import TAG, DutError, LinuxDut, q

RMAP = "TGEN-ALL"
ROLE_AF = {
    "route_server": "route-server-client",       # no AS prepend, next hop kept
    "route_reflector": "route-reflector-client",
    "router": "next-hop-self",
}
CHUNK = 40      # neighbours per vtysh invocation (keeps each command line small)


class Frr(LinuxDut):
    platform = "FRRouting / Quagga"
    config_ext = "vtysh"
    daemon = "bgpd"

    def vtysh(self, *cmds: str, timeout: float = 120) -> str:
        args = " ".join(f"-c {q(c)}" for c in cmds)
        return self.run(f"{self.sudo()}vtysh {args}", timeout=timeout, check=False)

    def resources(self) -> dict:
        v = self.vtysh("show version")
        m = re.search(r"(FRRouting|Quagga)\s+(\S+)", v)
        if not m:
            raise DutError(f"no FRR/Quagga vtysh answering on {self.host}: {v.strip()[-200:]}")
        r = self.host_resources(self.daemon)
        r["product"], r["version"] = m.group(1), m.group(2)
        r["summary"] = (f"{r['product']} {r['version']} on {r['host']}, load {r['load1']} ({r['cpus']} CPUs), "
                        f"memory free {r['free_memory']} of {r['total_memory']}, bgpd {r['bgpd_rss']}")
        return r

    # ---------------------------------------------------------------- render
    def render(self, meta: dict, dut_ip: str, dut_asn: int, role: str, hold_time: int = 90) -> str:
        """vtysh command list, one per line, run in one session per CHUNK of
        neighbours (each chunk starts with 'configure terminal' / 'router bgp')."""
        out = [f"! TrafficGenerator run {meta['run_id']}: {len(meta['routers'])} emulated routers, role {role}",
               "! generated - do not edit; neighbours removed by the tool after the run",
               "configure terminal",
               f"route-map {RMAP} permit 10",
               "exit"]
        ka = max(1, hold_time // 3)
        routers = meta["routers"]
        for i in range(0, len(routers), CHUNK):
            part = routers[i:i + CHUNK]
            if i:
                out.append("configure terminal")
            out.append(f"router bgp {dut_asn}")
            for r in part:
                ip, asn = r["ip"], dut_asn if role == "route_reflector" else r["asn"]
                out += [f"neighbor {ip} remote-as {asn}",
                        f"neighbor {ip} description {TAG} {meta['run_id']} {r['name']} node {r['node']}",
                        f"neighbor {ip} passive",
                        f"neighbor {ip} timers {ka} {hold_time}"]
                if self.opts.get("multihop"):          # loopback simulation only
                    out.append(f"neighbor {ip} disable-connected-check")
            out.append("address-family ipv4 unicast")
            for r in part:
                ip = r["ip"]
                out += [f"neighbor {ip} activate",
                        f"neighbor {ip} {ROLE_AF[role]}",
                        f"neighbor {ip} route-map {RMAP} in",
                        f"neighbor {ip} route-map {RMAP} out"]
            out += ["exit-address-family", "end"]
        return "\n".join(out) + "\n"

    def _apply(self, text: str) -> str:
        """Runs each 'configure terminal' ... 'end' block in one vtysh session."""
        blocks, cur = [], []
        for line in text.splitlines():
            if not line.strip() or line.startswith("!"):
                continue
            if line == "configure terminal" and cur:
                blocks.append(cur)
                cur = []
            cur.append(line)
        if cur:
            blocks.append(cur)
        outs = []
        for b in blocks:
            o = self.vtysh(*b, timeout=300)
            bad = [l for l in o.splitlines() if l.lstrip().startswith("%")]
            if bad:
                raise DutError(f"vtysh rejected the configuration: {' | '.join(bad[:3])}"
                               + (" (another BGP instance with a different AS is running? set --dut-asn to it)"
                                  if any("AS is" in l or "already running" in l for l in bad) else ""))
            outs.append(o)
        return "\n".join(outs)

    def push(self, config: str, expected: int) -> int:
        if self.count():                 # leftovers of an aborted or kept run
            self.notes.append("removed tgen neighbours left from an earlier run")
            self.remove()
        self._apply(config)
        n = self.count()
        if n != expected:
            raise DutError(f"bgpd has {n} tgen neighbours, expected {expected}")
        self.pushed_at = time.time()
        return n

    # ---------------------------------------------------------------- state
    def _running(self) -> tuple[str | None, list[str]]:
        cfg = self.vtysh("show running-config")
        asn = re.search(r"^router bgp (\d+)", cfg, re.M)
        ips = re.findall(rf"^\s*neighbor (\S+) description {TAG}\b", cfg, re.M)
        return (asn.group(1) if asn else None), ips

    def count(self) -> int:
        return len(self._running()[1])

    def remove(self) -> None:
        asn, ips = self._running()
        if ips:
            cmds = []
            for i in range(0, len(ips), CHUNK * 4):
                cmds.append(["configure terminal", f"router bgp {asn}",
                             *[f"no neighbor {ip}" for ip in ips[i:i + CHUNK * 4]], "end"])
            for c in cmds:
                self.vtysh(*c, timeout=300)
        self.vtysh("configure terminal", f"no route-map {RMAP}", "end")
        left = self.count()
        if left:
            raise DutError(f"{left} tgen neighbours still configured after removal")

    def sessions(self) -> tuple[str, int]:
        _, ips = self._running()
        out = self.vtysh("show ip bgp summary")
        mine = set(ips)
        est = 0
        for line in out.splitlines():
            f = line.split()
            # Neighbor V AS MsgRcvd MsgSent TblVer InQ OutQ Up/Down State/PfxRcd [PfxSnt Desc]
            if f and f[0] in mine and len(f) >= 10 and f[9].isdigit():
                est += 1
        return out, est

    def log(self) -> str:
        since = int(getattr(self, "pushed_at", time.time() - 3600)) - 5
        s = self.sudo()
        out = self.run(f"{s}journalctl -u frr -u bgpd -u quagga --since @{since} --no-pager -o short-iso 2>/dev/null",
                       check=False, timeout=60)
        if not out.strip() or "No entries" in out:
            out = self.run(f"{s}sh -c 'tail -n 5000 /var/log/frr/bgpd.log /var/log/frr/frr.log "
                           f"/var/log/quagga/bgpd.log 2>/dev/null'", check=False, timeout=60)
        return out or "# no bgpd log found (journal frr/bgpd/quagga or /var/log/frr, /var/log/quagga)\n"


class Quagga(Frr):
    platform = "Quagga"
