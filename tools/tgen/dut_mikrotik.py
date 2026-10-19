# SPDX-License-Identifier: MIT
"""MikroTik RouterOS 7 as device under test: generate, push and remove the BGP
neighbour configuration for the emulated routers of a run.

Transport: OpenSSH + sshpass (no extra Python packages). The password is taken
from the environment variable TGEN_DUT_PASSWORD or asked interactively; it is
never written to disk or to the command line. An SSH key works too (no password).

Everything this module adds carries the comment "tgen", so it can be found and
removed without touching the rest of the router's configuration.
"""
from __future__ import annotations

import getpass
import os
import re
import subprocess
import tempfile
from pathlib import Path

SSH_OPTS = ["-o", "ConnectTimeout=8", "-o", "StrictHostKeyChecking=accept-new", "-o", "LogLevel=ERROR"]
RSC_NAME = "tgen-bgp.rsc"
TAG = "tgen"

# DUT role -> RouterOS 7 connection settings (spec §5.1)
ROLE_SETTINGS = {
    # IXP route server: keep the original next hop. RouterOS has no transparent
    # route-server mode, so it will most likely prepend its own AS; the
    # benchmark's correctness check reports this ("paths_with_dut_asn").
    "route_server": {"local.role": "ebgp", "nexthop-choice": "propagate"},
    # iBGP route reflector: every emulated router is a client in the DUT's AS
    "route_reflector": {"local.role": "ibgp-rr", "nexthop-choice": "propagate"},
    # router that forwards traffic: next hop = DUT
    "router": {"local.role": "ebgp", "nexthop-choice": "force-self"},
}

# Rough safe limits for small RouterOS devices (routes the DUT must hold).
# CRS106-1C-5S: 1 core 400 MHz, 128 MB RAM.
DEFAULT_MAX_ROUTES = 20_000


class DutError(RuntimeError):
    pass


class MikroTik:
    def __init__(self, host: str, user: str = "admin", password: str | None = None):
        self.host, self.user = host, user
        self.password = password if password is not None else os.environ.get("TGEN_DUT_PASSWORD")

    # ------------------------------------------------------------- transport
    def _auth(self) -> tuple[list[str], dict]:
        env = dict(os.environ)
        if self.password:
            env["SSHPASS"] = self.password
            return ["sshpass", "-e"], env
        return [], env

    def ask_password_if_needed(self) -> None:
        if self.password is None and os.isatty(0):
            self.password = getpass.getpass(f"password for {self.user}@{self.host} (not stored): ") or None

    def run(self, command: str, timeout: float = 60) -> str:
        pre, env = self._auth()
        batch = [] if self.password else ["-o", "BatchMode=yes"]
        r = subprocess.run([*pre, "ssh", *SSH_OPTS, *batch, f"{self.user}@{self.host}", command],
                           capture_output=True, text=True, timeout=timeout, env=env)
        if r.returncode != 0:
            raise DutError(f"{self.host}: `{command}` failed: {(r.stderr or r.stdout).strip()[-300:]}")
        return r.stdout

    def upload(self, local: Path, remote_name: str) -> None:
        pre, env = self._auth()
        r = subprocess.run([*pre, "scp", "-q", *SSH_OPTS, str(local), f"{self.user}@{self.host}:{remote_name}"],
                           capture_output=True, text=True, timeout=60, env=env)
        if r.returncode != 0:
            raise DutError(f"{self.host}: upload failed: {r.stderr.strip()[-300:]}")

    # ----------------------------------------------------------------- facts
    def version(self) -> str:
        out = self.run("/system resource print")
        m = re.search(r"version:\s*([0-9][^\s]*)", out)
        if not m:
            raise DutError("could not read RouterOS version")
        return m.group(1)

    def resources(self) -> dict:
        out = self.run("/system resource print")
        get = lambda k: (re.search(rf"{k}:\s*(.+)", out) or [None, ""])[1].strip()  # noqa: E731
        return {"version": get("version"), "board": get("board-name"), "cpu": get("cpu"),
                "cpu_load": get("cpu-load"), "free_memory": get("free-memory"), "total_memory": get("total-memory")}

    # ---------------------------------------------------------------- config
    def push(self, rsc: str, expected: int) -> int:
        """Removes any previous tgen BGP config, imports the new one, returns
        the number of tgen connections the router reports."""
        major = int(self.version().split(".")[0])
        if major < 7:
            raise DutError("RouterOS 6 is not supported by this generator (BGP syntax differs); v7 required")
        with tempfile.NamedTemporaryFile("w", suffix=".rsc", delete=False) as f:
            f.write(rsc)
            tmp = Path(f.name)
        try:
            self.upload(tmp, RSC_NAME)
        finally:
            tmp.unlink(missing_ok=True)
        out = self.run(f"/import file-name={RSC_NAME} verbose=no", timeout=120)
        if "executed successfully" not in out:
            raise DutError(f"import did not succeed: {out.strip()[-400:]}")
        self.run(f'/file remove [find name="{RSC_NAME}"]')
        n = self.count()
        if n != expected:
            raise DutError(f"router has {n} tgen BGP connections, expected {expected}")
        return n

    def count(self) -> int:
        out = self.run(f'/routing bgp connection print count-only where comment~"{TAG}"')
        return int(out.strip() or 0)

    def remove(self) -> None:
        self.run(f'/routing bgp connection remove [find where comment~"{TAG}"]')

    def sessions(self) -> str:
        return self.run(f'/routing bgp session print terse where comment~"{TAG}"', timeout=60)

    def bgp_log(self) -> str:
        """BGP-related log lines (input for the P5 log parser)."""
        return self.run('/log print where topics~"bgp"', timeout=60)


def render_rsc(meta: dict, dut_ip: str, dut_asn: int, role: str, hold_time: int = 90) -> str:
    """RouterOS 7 script: one passive BGP connection per emulated router."""
    if role not in ROLE_SETTINGS:
        raise ValueError(f"unknown role {role}")
    rs = ROLE_SETTINGS[role]
    run_id = meta["run_id"]
    lines = [
        f"# TrafficGenerator run {run_id}: {len(meta['routers'])} emulated routers, role {role}",
        "# generated - do not edit; removed by: /routing bgp connection remove [find where comment~\"tgen\"]",
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
            "routing-table=main"
        )
    return "\n".join(lines) + "\n"
