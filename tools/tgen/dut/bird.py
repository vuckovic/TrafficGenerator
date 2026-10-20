# SPDX-License-Identifier: MIT
"""BIRD 2/3 on a Linux host as DUT (an existing BIRD, reached over SSH).

The neighbours go into their own include file (default /etc/bird/tgen.conf);
the main config gets one line `include "/etc/bird/tgen.conf";` the first time
(idempotent, announced in the run log). Removal empties the include file and
reconfigures, so the main config stays valid. Applied with `birdc configure`
(soft reconfiguration: other protocols keep running).

(The managed stand-in - BIRD started by the tool on CMNODE06 - is `--dut bird`
and lives in tgen_p2.py; this adapter is `--dut bird-ssh`.)
"""
from __future__ import annotations

import re
import tempfile
import time
from pathlib import Path

from .base import TAG, DutError, LinuxDut, q

ROLE_LINES = {
    "route_server": "rs client;",          # transparent: no AS prepend, next hop kept
    "route_reflector": "rr client;",
    "router": "",                          # + next hop self in the channel
}


class Bird(LinuxDut):
    platform = "BIRD 2/3"
    config_ext = "conf"

    @property
    def include(self) -> str:
        return self.opts.get("bird_include") or "/etc/bird/tgen.conf"

    @property
    def main_conf(self) -> str:
        return self.opts.get("bird_conf") or "/etc/bird/bird.conf"

    def birdc(self, cmd: str, timeout: float = 60) -> str:
        sock = self.opts.get("bird_socket")
        return self.run(f"{self.sudo()}birdc {'-s ' + q(sock) + ' ' if sock else ''}{cmd}", timeout=timeout)

    def resources(self) -> dict:
        st = self.birdc("show status")
        m = re.search(r"BIRD\s+(\S+)", st)
        if not m:
            raise DutError(f"no BIRD answering on {self.host}: {st.strip()[-200:]}")
        r = self.host_resources("bird")
        r["version"] = m.group(1)
        mem = self.birdc("show memory")
        tot = re.search(r"Total:\s+(\S+\s*\S*)", mem)
        r["bird_memory"] = tot.group(1).strip() if tot else ""
        r["summary"] = (f"BIRD {r['version']} on {r['host']}, load {r['load1']} ({r['cpus']} CPUs), "
                        f"memory free {r['free_memory']} of {r['total_memory']}, BIRD {r['bird_memory']}")
        return r

    def render(self, meta: dict, dut_ip: str, dut_asn: int, role: str, hold_time: int = 90) -> str:
        nh = " next hop self;" if role == "router" else ""
        lines = [
            f"# TrafficGenerator run {meta['run_id']}: {len(meta['routers'])} emulated routers, role {role}",
            "# generated - do not edit; emptied by the tool after the run",
            f"template bgp {TAG}_tpl {{",
            f"  local {dut_ip} as {dut_asn};",
            "  passive on;",
            f"  hold time {hold_time};",
            f"  {ROLE_LINES[role]}" if ROLE_LINES[role] else "",
            "  multihop 2;" if self.opts.get("multihop") else "",
            f"  ipv4 {{ import all; export all;{nh} }};",
            "}",
        ]
        for r in meta["routers"]:
            asn = dut_asn if role == "route_reflector" else r["asn"]
            lines.append(f'protocol bgp {TAG}_{r["name"]} from {TAG}_tpl {{ '
                         f'description "{TAG} {meta["run_id"]} {r["name"]} node {r["node"]}"; '
                         f'neighbor {r["ip"]} as {asn}; }}')
        return "\n".join(x for x in lines if x) + "\n"

    def _install(self, text: str) -> None:
        with tempfile.NamedTemporaryFile("w", suffix=".conf", delete=False) as f:
            f.write(text)
            tmp = Path(f.name)
        remote_tmp = f"{TAG}-bird-{int(time.time())}.conf"     # in the SSH user's home
        try:
            self.upload(tmp, remote_tmp)
        finally:
            tmp.unlink(missing_ok=True)
        s = self.sudo()
        self.run(f"{s}install -m 0644 {q(remote_tmp)} {q(self.include)} && rm -f {q(remote_tmp)}")

    def _configure(self) -> None:
        out = self.birdc("configure", timeout=300)
        if not re.search(r"Reconfigur(ed|ation in progress)", out):
            raise DutError(f"birdc configure failed: {out.strip()[-400:]}")

    def push(self, config: str, expected: int) -> int:
        s = self.sudo()
        self._install(config)
        line = f'include "{self.include}";'
        if not self._probe(f"{s}grep -qF {q(line)} {q(self.main_conf)}"):
            self.run(f"printf '\\n# TrafficGenerator emulated routers (emptied after each run)\\n%s\\n' {q(line)} "
                     f"| {s}tee -a {q(self.main_conf)} >/dev/null")
            self.notes.append(f"added `{line}` to {self.main_conf} (one time; stays, harmless when empty)")
        self._configure()
        n = self.count()
        if n != expected:
            raise DutError(f"BIRD has {n} {TAG}_ protocols, expected {expected}")
        self.pushed_at = time.time()
        return n

    def count(self) -> int:
        out = self.birdc("show protocols")
        return sum(1 for l in out.splitlines() if l.startswith(f"{TAG}_R"))

    def remove(self) -> None:
        self._install("# TrafficGenerator: no emulated routers configured\n")
        self._configure()

    def sessions(self) -> tuple[str, int]:
        lines = [l for l in self.birdc("show protocols").splitlines() if l.startswith(f"{TAG}_R")]
        return "\n".join(lines) + "\n", sum(1 for l in lines if "Established" in l)

    def log(self) -> str:
        since = int(getattr(self, "pushed_at", time.time() - 3600)) - 5
        out = self.run(f"{self.sudo()}journalctl -u bird -u bird2 --since @{since} --no-pager -o short-iso 2>/dev/null",
                       check=False, timeout=60)
        return out if out.strip() and "No entries" not in out else "# no journal entries for bird/bird2; set `log syslog all;` or check the log file in bird.conf\n"
