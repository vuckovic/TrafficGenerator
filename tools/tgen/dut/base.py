# SPDX-License-Identifier: MIT
"""Common interface and SSH transport for devices under test (DUT).

Every adapter does the same job for its platform:
  render()   -> configuration text with one passive BGP neighbour per emulated router
  push()     -> deploy it over SSH, verify the neighbour count
  remove()   -> delete exactly what push() added (everything tagged "tgen")
  sessions() -> (raw text, number of Established tgen sessions)
  resources()-> dict with at least "summary" (one line for the run log)
  log()      -> BGP-related log lines (input for the P5 log parsers)

Credentials: password from TGEN_DUT_PASSWORD or an interactive prompt (never
stored, never on the command line); an SSH key (--dut-key or ssh-agent) works
without a password. Cisco's enable secret, if needed, from TGEN_DUT_ENABLE.
"""
from __future__ import annotations

import getpass
import os
import shlex
import signal
import subprocess
from pathlib import Path

TAG = "tgen"
SSH_OPTS = ["-o", "ConnectTimeout=8", "-o", "StrictHostKeyChecking=accept-new", "-o", "LogLevel=ERROR"]


class DutError(RuntimeError):
    pass


class Dut:
    """Base class. Subclasses set `platform`, `config_ext` and implement the methods."""
    platform = "?"
    config_ext = "txt"
    default_max_routes: int | None = None       # None = no guard
    needs_linux_sudo = False

    def __init__(self, host: str, user: str, port: int = 22, key: str | None = None,
                 password: str | None = None, **opts):
        self.host, self.user, self.port, self.key = host, user, port, key
        self.password = password if password is not None else os.environ.get("TGEN_DUT_PASSWORD")
        self.opts = opts
        self.notes: list[str] = []      # things the run log should mention (e.g. a one-time change)
        self.call_timeout: float | None = None   # caps every call's timeout (set short for polling)

    # ------------------------------------------------------------- transport
    def ask_password_if_needed(self) -> None:
        if self.password is None and not self.key and os.isatty(0):
            self.password = getpass.getpass(f"password for {self.user}@{self.host} "
                                            "(not stored; empty = use SSH key/agent): ") or None

    def _ssh_base(self, prog: str = "ssh") -> tuple[list[str], dict]:
        env = dict(os.environ)
        pre: list[str] = []
        if self.password:
            env["SSHPASS"] = self.password
            pre = ["sshpass", "-e"]
        opts = list(SSH_OPTS)
        if not self.password:
            opts += ["-o", "BatchMode=yes"]
        if self.key:
            opts += ["-i", self.key]
        opts += (["-p", str(self.port)] if prog == "ssh" else ["-P", str(self.port)])
        return [*pre, prog, *opts], env

    def _timeout(self, t: float) -> float:
        return min(t, self.call_timeout) if self.call_timeout else t

    def _exec(self, argv: list[str], env: dict, timeout: float, what: str) -> subprocess.CompletedProcess:
        """Runs sshpass/ssh/scp in its own process group. On timeout the WHOLE group
        is killed: killing only sshpass leaves ssh holding the output pipes, and the
        caller then waits forever (seen on the CRS106 at 100 % CPU)."""
        timeout = self._timeout(timeout)
        p = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
                             text=True, env=env, start_new_session=True)
        try:
            out, err = p.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            p.communicate()
            raise DutError(f"{self.host}: {what} timed out after {timeout:.0f} s (DUT not answering)") from None
        return subprocess.CompletedProcess(argv, p.returncode, out, err)

    def run(self, command: str, timeout: float = 60, check: bool = True) -> str:
        base, env = self._ssh_base()
        r = self._exec([*base, f"{self.user}@{self.host}", command], env, timeout, f"`{command[:60]}`")
        if check and r.returncode != 0:
            raise DutError(f"{self.host}: `{command[:80]}` failed: {(r.stderr or r.stdout).strip()[-300:]}")
        return r.stdout

    def upload(self, local: Path, remote: str) -> None:
        base, env = self._ssh_base("scp")
        r = self._exec([*base, "-q", str(local), f"{self.user}@{self.host}:{remote}"], env, 60, "upload")
        if r.returncode != 0:
            raise DutError(f"{self.host}: upload failed: {r.stderr.strip()[-300:]}")

    # ------------------------------------------------------------- interface
    def resources(self) -> dict:
        raise NotImplementedError

    def interface_info(self, peer_ip: str, name: str | None = None) -> dict | None:
        """Port carrying peer_ip: speed_mbps, duplex, error counters. None = not supported."""
        return None

    def render(self, meta: dict, dut_ip: str, dut_asn: int, role: str, hold_time: int = 90) -> str:
        raise NotImplementedError

    def push(self, config: str, expected: int) -> int:
        raise NotImplementedError

    def count(self) -> int:
        raise NotImplementedError

    def remove(self) -> None:
        raise NotImplementedError

    def sessions(self) -> tuple[str, int]:
        raise NotImplementedError

    def log(self) -> str:
        raise NotImplementedError


class LinuxDut(Dut):
    """BIRD / FRR / Quagga on a Linux host: commands may need root (sudo -n)."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._sudo: str | None = None

    def sudo(self) -> str:
        """'' when the SSH user is root, else 'sudo -n ' (passwordless sudo required)."""
        if self._sudo is None:
            if self.opts.get("no_sudo") or self.run("id -u").strip() == "0":
                self._sudo = ""
            elif self._probe("sudo -n true"):
                self._sudo = "sudo -n "
            else:
                raise DutError(f"{self.user}@{self.host} is not root and has no passwordless sudo; "
                               "the daemon's control socket needs root (or add the user to the bird/frrvty group "
                               "and pass --dut-no-sudo)")
        return self._sudo

    def _probe(self, cmd: str) -> bool:
        base, env = self._ssh_base()
        return self._exec([*base, f"{self.user}@{self.host}", cmd], env, 15, f"`{cmd[:60]}`").returncode == 0

    def host_resources(self, daemon: str) -> dict:
        out = self.run("cat /proc/loadavg; nproc; free -m | awk '/^Mem:/{print $2, $7}'; "
                       f"ps -o rss= -C {shlex.quote(daemon)} | awk '{{s+=$1}} END {{print s+0}}'; hostname")
        load, ncpu, mem, rss, hn = (out.strip().split("\n") + [""] * 5)[:5]
        total, avail = (mem.split() + ["0", "0"])[:2]
        return {"host": hn, "load1": load.split()[0] if load else "", "cpus": ncpu,
                "total_memory": f"{total}MiB", "free_memory": f"{avail}MiB",
                f"{daemon}_rss": f"{int(rss or 0) // 1024}MiB"}


def q(s: str) -> str:
    return shlex.quote(s)
