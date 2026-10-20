# SPDX-License-Identifier: MIT
"""MikroTik RouterOS 7 as DUT: one passive /routing bgp connection per emulated
router, deployed as a .rsc script (scp + /import). Everything carries the
comment "tgen"; removal touches nothing else."""
from __future__ import annotations

import re
import tempfile
from pathlib import Path

from .base import TAG, Dut, DutError

RSC_NAME = "tgen-bgp.rsc"

# DUT role -> RouterOS 7 connection settings (spec §5.1)
ROLE_SETTINGS = {
    # RouterOS has no transparent route-server mode: it will most likely prepend
    # its own AS; the correctness check reports this ("paths_with_dut_asn").
    "route_server": {"local.role": "ebgp", "nexthop-choice": "propagate"},
    "route_reflector": {"local.role": "ibgp-rr", "nexthop-choice": "propagate"},
    "router": {"local.role": "ebgp", "nexthop-choice": "force-self"},
}


def parse_sessions(raw: str) -> list[dict]:
    """`/routing bgp session print terse` -> one dict per session. Line shape seen on
    RouterOS 7.11.2: `60 E name=tgen-R10-1 remote.address=10.100.0.29 ... uptime=7s580ms
    last-started=2026-10-20 21:43:05 prefix-count=0` (the E flag = established)."""
    out = []
    for line in raw.splitlines():
        m = re.match(r"\s*\d+\s+([A-Z]*)\s*(name=.*)$", line)
        if not m:
            continue
        flags, rest = m.groups()
        kv = dict(re.findall(r'([\w.-]+)=("[^"]*"|\d{4}-\d\d-\d\d \d\d:\d\d:\d\d|\S*)', rest))
        out.append({"name": kv.get("name", ""), "remote": kv.get("remote.address", ""),
                    "established": "E" in flags, "last_started": kv.get("last-started", ""),
                    "uptime": kv.get("uptime", ""), "prefix_count": int(kv.get("prefix-count") or 0)})
    return out


class MikroTik(Dut):
    platform = "MikroTik RouterOS 7"
    config_ext = "rsc"
    default_max_routes = 20_000     # CRS106-1C-5S: 1 core 400 MHz, 128 MB RAM

    def resources(self) -> dict:
        out = self.run("/system resource print")
        get = lambda k: (re.search(rf"{k}:\s*(.+)", out) or [None, ""])[1].strip()  # noqa: E731
        r = {"version": get("version"), "board": get("board-name"), "cpu": get("cpu"),
             "cpu_load": get("cpu-load"), "free_memory": get("free-memory"), "total_memory": get("total-memory")}
        if not r["version"]:
            raise DutError("could not read RouterOS version (is this a MikroTik?)")
        if int(r["version"].split(".")[0]) < 7:
            raise DutError(f"RouterOS {r['version']}: version 7 required (BGP syntax differs in v6)")
        r["summary"] = (f"{r['board']} RouterOS {r['version']}, CPU {r['cpu']} load {r['cpu_load']}, "
                        f"memory free {r['free_memory']} of {r['total_memory']}")
        return r

    def render(self, meta: dict, dut_ip: str, dut_asn: int, role: str, hold_time: int = 90) -> str:
        rs = ROLE_SETTINGS[role]
        run_id = meta["run_id"]
        lines = [
            f"# TrafficGenerator run {run_id}: {len(meta['routers'])} emulated routers, role {role}",
            f'# generated - do not edit; removed by: /routing bgp connection remove [find where comment~"{TAG}"]',
            f'/routing bgp connection remove [find where comment~"{TAG}"]',
        ]
        for r in meta["routers"]:
            remote_as = dut_asn if role == "route_reflector" else r["asn"]
            lines.append(
                "/routing bgp connection add "
                f'name={TAG}-{r["name"]} comment="{TAG} {run_id} {r["name"]} node {r["node"]}" '
                f"as={dut_asn} router-id={dut_ip} local.address={dut_ip} local.role={rs['local.role']} "
                f'remote.address={r["ip"]} remote.as={remote_as} '
                f"connect=no listen=yes hold-time={hold_time}s nexthop-choice={rs['nexthop-choice']} "
                "routing-table=main")
        return "\n".join(lines) + "\n"

    def push(self, config: str, expected: int) -> int:
        # where this run's lines start in the memory log (it also holds earlier runs)
        self._log_start = self._log_count()
        with tempfile.NamedTemporaryFile("w", suffix=".rsc", delete=False) as f:
            f.write(config)
            tmp = Path(f.name)
        try:
            self.upload(tmp, RSC_NAME)
        finally:
            tmp.unlink(missing_ok=True)
        out = self.run(f"/import file-name={RSC_NAME} verbose=no", timeout=120, check=False)
        if "executed successfully" not in out:
            # re-run verbosely: RouterOS then echoes each line and names the one that failed
            detail = self.run(f"/import file-name={RSC_NAME} verbose=yes", timeout=180, check=False)
            self.run(f'/file remove [find name="{RSC_NAME}"]', check=False)
            raise DutError("RouterOS did not accept the script; last output:\n"
                           + ((detail or out).strip()[-800:] or "(no output)"))
        self.run(f'/file remove [find name="{RSC_NAME}"]')
        self._enable_bgp_log()
        n = self.count()
        if n != expected:
            raise DutError(f"router has {n} tgen BGP connections, expected {expected}")
        return n

    def _enable_bgp_log(self) -> None:
        """RouterOS 7 does not log BGP by default: log BGP events (no per-update
        debug lines) to memory during the run, so dut-bgp.log is not empty. A
        refused rule is a warning only - it must never block a benchmark.
        Logging rules have no `comment` in RouterOS 7.11 (the CLI rejects it), so
        our rule is tagged by its `prefix`, which also marks the lines it logs."""
        self.run(f'/system logging remove [find where prefix="{TAG}"]', check=False)
        last = ""
        # RouterOS 7.11 logs BGP session changes only at debug level (seen on the
        # CRS106: nothing with bgp,!debug); keep debug, drop packet dumps
        for topics in ('"bgp,!packet,!raw"', "bgp"):
            out = self.run(f'/system logging add topics={topics} action=memory prefix={TAG}', check=False)
            if self.run(f'/system logging print count-only where prefix="{TAG}"', check=False).strip() not in ("", "0"):
                self.notes.append(f"BGP logging enabled for the run (topics {topics.strip(chr(34))})")
                return
            last = out.strip()
        self.notes.append(f"could not enable BGP logging on the DUT ({last[-200:] or 'no reason given'}); "
                          "dut-bgp.log may be empty")

    def count(self) -> int:
        return int(self.run(f'/routing bgp connection print count-only where comment~"{TAG}"').strip() or 0)

    def remove(self) -> None:
        self.run(f'/routing bgp connection remove [find where comment~"{TAG}"]')
        self.run(f'/system logging remove [find where prefix="{TAG}"]', check=False)

    def sessions(self) -> tuple[str, int]:
        out = self.run(f'/routing bgp session print terse where name~"{TAG}"')
        # RouterOS may answer "0" with a non-zero exit code (seen on 7.11.2 at teardown)
        n = self.run(f'/routing bgp session print count-only where name~"{TAG}" and established', check=False)
        try:
            return out, int(n.strip().splitlines()[-1]) if n.strip() else 0
        except ValueError:
            raise DutError(f"{self.host}: unexpected answer to the session count: {n.strip()[-120:]}") from None

    def _log_count(self) -> int:
        try:
            return int(self.run("/log print count-only", check=False).strip() or 0)
        except ValueError:
            return 0

    def log(self) -> str:
        """This run's BGP lines from the memory log ("time topics message", terse),
        plus the router's own records of our tgen neighbours being added/removed.
        Lines from before push() are skipped (the memory log keeps earlier runs;
        it holds 1 000 lines by default, so if it wrapped, everything is kept)."""
        lines = self.run("/log print terse", timeout=120).splitlines()
        start = getattr(self, "_log_start", 0)
        if 0 < start <= len(lines) < 1000:
            lines = lines[start:]
        keep = [l for l in lines
                if len(l.split()) > 1 and ("bgp" in l.split()[1].split(",") or f" {TAG}-" in l or f" {TAG} " in l)]
        return "\n".join(keep) + "\n"
