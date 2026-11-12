# SPDX-License-Identifier: MIT
"""SNMP v2c polling of the DUT (U5, docs/10 §6): read-only, with the net-snmp command-line tools.

What is read, when the device has it:
- system: sysDescr, sysName, sysUpTime, sysObjectID (the Test button);
- CPU: CISCO-PROCESS-MIB cpmCPUTotal5secRev, else HOST-RESOURCES hrProcessorLoad (average);
- memory: CISCO-MEMORY-POOL-MIB (processor pool), else UCD memAvailReal/memTotalReal (Linux), else
  HOST-RESOURCES hrStorage of type RAM (RouterOS);
- the interface towards the generator: IF-MIB 64-bit octets, errors and discards (the profile's
  `interface`, else the busiest one), as rates between polls;
- BGP: BGP4-MIB peer state and update counters, CISCO-BGP4-MIB accepted prefixes, for our peers only.

The community is a credential: it is passed through a temporary snmp.conf (mode 0600, removed after
the call), never on the command line, never written to the run folder.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import time
from contextlib import contextmanager

SYS = {"descr": ".1.3.6.1.2.1.1.1.0", "object_id": ".1.3.6.1.2.1.1.2.0", "uptime": ".1.3.6.1.2.1.1.3.0",
       "name": ".1.3.6.1.2.1.1.5.0"}
CISCO_CPU = ".1.3.6.1.4.1.9.9.109.1.1.1.1.6"          # cpmCPUTotal5secRev
HR_CPU = ".1.3.6.1.2.1.25.3.3.1.2"                    # hrProcessorLoad
CISCO_MEM_USED = ".1.3.6.1.4.1.9.9.48.1.1.1.5"        # ciscoMemoryPoolUsed (.1 = processor)
CISCO_MEM_FREE = ".1.3.6.1.4.1.9.9.48.1.1.1.6"
UCD_TOTAL, UCD_AVAIL = ".1.3.6.1.4.1.2021.4.5.0", ".1.3.6.1.4.1.2021.4.6.0"
HR_STORAGE = ".1.3.6.1.2.1.25.2.3.1"                  # .2 type .4 unit .5 size .6 used
HR_RAM = ".1.3.6.1.2.1.25.2.1.2"
IF_DESCR, IF_OPER = ".1.3.6.1.2.1.2.2.1.2", ".1.3.6.1.2.1.2.2.1.8"
IF_IN_DISC, IF_IN_ERR, IF_OUT_DISC = ".1.3.6.1.2.1.2.2.1.13", ".1.3.6.1.2.1.2.2.1.14", ".1.3.6.1.2.1.2.2.1.19"
IF_NAME, IF_HC_IN, IF_HC_OUT = ".1.3.6.1.2.1.31.1.1.1.1", ".1.3.6.1.2.1.31.1.1.1.6", ".1.3.6.1.2.1.31.1.1.1.10"
BGP_STATE, BGP_IN_UPD, BGP_OUT_UPD = ".1.3.6.1.2.1.15.3.1.2", ".1.3.6.1.2.1.15.3.1.10", ".1.3.6.1.2.1.15.3.1.11"
CBGP_ACC = ".1.3.6.1.4.1.9.9.187.1.2.4.1.1"           # cbgpPeerAcceptedPrefixes.<ip>.<afi>.<safi>
CBGP2_ACC = ".1.3.6.1.4.1.9.9.187.1.2.8.1.1"          # cbgpPeer2AcceptedPrefixes.<type>.<len>.<addr>.<afi>.<safi>


class SnmpError(Exception):
    pass


def available() -> bool:
    return bool(shutil.which("snmpget") and shutil.which("snmpbulkwalk"))


def community_env(profile: str) -> str:
    return "TGEN_DUT_SNMP_" + "".join(c if c.isalnum() else "_" for c in profile).upper()


@contextmanager
def _conf(community: str):
    if not community or any(c in community for c in "\n\r"):
        raise SnmpError("SNMP community missing")
    d = tempfile.mkdtemp(prefix="tgen-snmp-")
    try:
        p = os.path.join(d, "snmp.conf")
        fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(f"defVersion 2c\ndefCommunity {community}\n")
        yield {**os.environ, "SNMPCONFPATH": d, "MIBS": "", "MIBDIRS": d}
    finally:
        shutil.rmtree(d, ignore_errors=True)


def parse(out: str) -> dict[str, str]:
    """`-On -Oq -Ot -Oe` output -> {numeric oid: value}; "No Such ..." values dropped."""
    res: dict[str, str] = {}
    for line in out.splitlines():
        m = re.match(r"^(\.[\d.]+)\s+(.*)$", line.strip())
        if not m:
            continue
        v = m.group(2).strip()
        if v.startswith("No Such") or v.startswith("No more variables"):
            continue
        if len(v) >= 2 and v[0] == v[-1] == '"':
            v = v[1:-1]
        res[m.group(1)] = v
    return res


class Session:
    """One device. `poll()` keeps the previous counters to give rates."""

    def __init__(self, host: str, community: str, port: int = 161, timeout: float = 2.0, retries: int = 1):
        self.host, self.community, self.port = host, community, port
        self.timeout, self.retries = timeout, retries
        self.prev: dict | None = None
        self.if_index: str | None = None
        self.kind: str | None = None          # "cisco" | "other", from sysObjectID

    def _run(self, tool: str, oids: list[str], wall: float = 20) -> dict[str, str]:
        if not available():
            raise SnmpError("net-snmp tools not installed (apt install snmp)")
        target = f"udp:{self.host}:{self.port}"
        args = [tool, "-On", "-Oq", "-Ot", "-Oe", "-t", str(self.timeout), "-r", str(self.retries)]
        if tool == "snmpbulkwalk":
            args += ["-Cr25"]
        with _conf(self.community) as env:
            try:
                p = subprocess.run(args + [target] + oids, capture_output=True, text=True, timeout=wall, env=env)
            except subprocess.TimeoutExpired as e:
                raise SnmpError(f"SNMP to {self.host}: no answer in {wall:.0f} s") from e
        if p.returncode != 0 and not p.stdout.strip():
            msg = (p.stderr.strip().splitlines() or ["error"])[-1]
            if "Timeout" in msg:
                raise SnmpError(f"SNMP to {self.host}:{self.port}: no answer (SNMP off, wrong community or an ACL?)")
            raise SnmpError(f"SNMP to {self.host}: {msg[:200]}")
        return parse(p.stdout)

    def get(self, *oids: str) -> dict[str, str]:
        return self._run("snmpget", list(oids))

    def walk(self, oid: str) -> dict[str, str]:
        out = self._run("snmpbulkwalk", [oid], wall=30)
        return {k: v for k, v in out.items() if k.startswith(oid + ".")}

    # ------------------------------------------------------------ U5 Test
    def system(self) -> dict:
        v = self.get(*SYS.values())
        if not v:
            raise SnmpError(f"SNMP to {self.host}: no answer (community or ACL?)")
        out = {k: v.get(o) for k, o in SYS.items()}
        out["uptime_s"] = round(int(out.pop("uptime")) / 100) if (out.get("uptime") or "").isdigit() else None
        self.kind = "cisco" if (out.get("object_id") or "").startswith(".1.3.6.1.4.1.9.") else "other"
        return out

    # ------------------------------------------------------------ pieces
    def cpu(self) -> float | None:
        for oid in ((CISCO_CPU, HR_CPU) if self.kind == "cisco" else (HR_CPU, CISCO_CPU)):
            vals = [float(x) for x in self.walk(oid).values() if re.fullmatch(r"\d+(\.\d+)?", x)]
            if vals:
                return round(sum(vals) / len(vals), 1)
        return None

    def memory(self) -> dict | None:
        if self.kind == "cisco":
            v = self.get(CISCO_MEM_USED + ".1", CISCO_MEM_FREE + ".1")
            u, f = v.get(CISCO_MEM_USED + ".1"), v.get(CISCO_MEM_FREE + ".1")
            if u and f and u.isdigit() and f.isdigit():
                tot = int(u) + int(f)
                return {"total_mb": round(tot / 2**20, 1), "free_pct": round(100 * int(f) / tot, 1) if tot else None}
        v = self.get(UCD_TOTAL, UCD_AVAIL)
        tot, av = v.get(UCD_TOTAL), v.get(UCD_AVAIL)
        if tot and av and tot.isdigit() and av.isdigit() and int(tot):
            return {"total_mb": round(int(tot) / 1024, 1), "free_pct": round(100 * int(av) / int(tot), 1)}
        st = self.walk(HR_STORAGE)
        for k, typ in st.items():
            if k.startswith(HR_STORAGE + ".2.") and typ in (HR_RAM, HR_RAM.lstrip(".")):
                i = k.rsplit(".", 1)[1]
                unit, size, used = (st.get(f"{HR_STORAGE}.{c}.{i}") for c in (4, 5, 6))
                if unit and size and used and size.isdigit() and int(size):
                    u = int(unit.split()[0]) if unit.split()[0].isdigit() else 1
                    return {"total_mb": round(int(size) * u / 2**20, 1), "free_pct": round(100 * (1 - int(used) / int(size)), 1)}
        return None

    def interfaces(self, want: str | None) -> dict[str, dict]:
        names = self.walk(IF_NAME) or {}
        descr = self.walk(IF_DESCR) or {}
        idx = {k.rsplit(".", 1)[1]: v for k, v in descr.items()}
        idx.update({k.rsplit(".", 1)[1]: v for k, v in names.items() if v})
        if want and not self.if_index:
            w = want.lower().replace(" ", "")
            for i, n in list(idx.items()) + [(k.rsplit(".", 1)[1], v) for k, v in descr.items()]:
                if n.lower().replace(" ", "") == w or _abbrev(n) == _abbrev(want):
                    self.if_index = i
                    break
        cols = {"in_oct": IF_HC_IN, "out_oct": IF_HC_OUT, "in_err": IF_IN_ERR, "in_disc": IF_IN_DISC,
                "out_disc": IF_OUT_DISC, "oper": IF_OPER}
        pick = [self.if_index] if self.if_index else list(idx)
        res = {}
        if self.if_index:
            v = self.get(*[f"{o}.{self.if_index}" for o in cols.values()])
            data = {c: v.get(f"{o}.{self.if_index}") for c, o in cols.items()}
            res[self.if_index] = data
        else:
            walked = {c: self.walk(o) for c, o in cols.items()}
            for i in pick:
                res[i] = {c: walked[c].get(f"{cols[c]}.{i}") for c in cols}
        return {i: {"name": idx.get(i, i), **{k: (int(v) if v and v.lstrip("-").isdigit() else None) for k, v in d.items()}}
                for i, d in res.items()}

    def bgp(self, peers: set[str] | None) -> dict:
        def ours(ip: str) -> bool:
            return not peers or ip in peers
        state = {k[len(BGP_STATE) + 1:]: v for k, v in self.walk(BGP_STATE).items()}
        state = {ip: int(v) for ip, v in state.items() if ours(ip) and v.isdigit()}
        upd_in = {k[len(BGP_IN_UPD) + 1:]: int(v) for k, v in self.walk(BGP_IN_UPD).items() if v.isdigit()}
        acc, have_acc = 0, False
        for k, v in self.walk(CBGP2_ACC).items():          # <type>.<len>.<a>.<b>.<c>.<d>.<afi>.<safi>
            p = k[len(CBGP2_ACC) + 1:].split(".")
            if len(p) >= 8 and p[0] == "1" and p[1] == "4" and v.isdigit() and ours(".".join(p[2:6])):
                acc += int(v)
                have_acc = True
        if not have_acc:
            for k, v in self.walk(CBGP_ACC).items():       # <a>.<b>.<c>.<d>.<afi>.<safi>
                p = k[len(CBGP_ACC) + 1:].split(".")
                if len(p) >= 4 and v.isdigit() and ours(".".join(p[:4])):
                    acc += int(v)
                    have_acc = True
        return {"peers": len(state), "established": sum(1 for s in state.values() if s == 6),
                "in_updates": sum(v for ip, v in upd_in.items() if ours(ip)) if upd_in else None,
                "accepted_prefixes": acc if have_acc else None}

    # ------------------------------------------------------------ one poll
    def poll(self, interface: str | None = None, peers: set[str] | None = None) -> dict:
        """-> {t, cpu_pct, free_mem_pct, if{name, in_mbps, out_mbps, in_err, discards, oper}, bgp{...}, errors}"""
        if self.kind is None:
            self.system()
        rec: dict = {"t": round(time.time(), 3)}
        errs = []
        for key, f in (("cpu_pct", self.cpu), ("mem", self.memory), ("ifs", lambda: self.interfaces(interface)),
                       ("bgp", lambda: self.bgp(peers))):
            try:
                rec[key] = f()
            except SnmpError as e:
                errs.append(f"{key}: {e}")
                if "no answer" in str(e):
                    break
        mem = rec.pop("mem", None) or {}
        rec["free_mem_pct"] = mem.get("free_pct")
        ifs = rec.pop("ifs", None) or {}
        prev, now = self.prev or {}, rec["t"]
        dt = now - prev.get("t", now)
        best = None
        for i, d in ifs.items():
            p = (prev.get("ifs") or {}).get(i) or {}
            row = {"name": d["name"], "oper": {1: "up", 2: "down"}.get(d["oper"], d["oper"]),
                   "in_err": d["in_err"], "discards": (d["in_disc"] or 0) + (d["out_disc"] or 0)
                   if d["in_disc"] is not None or d["out_disc"] is not None else None}
            for c, k in (("in_oct", "in_mbps"), ("out_oct", "out_mbps")):
                row[k] = round((d[c] - p[c]) * 8 / dt / 1e6, 3) if dt > 0 and d[c] is not None and p.get(c) is not None \
                    and d[c] >= p[c] else None
            if self.if_index or best is None or (row["in_mbps"] or 0) > (best["in_mbps"] or 0):
                best = row
        rec["if"] = best
        b = rec.get("bgp") or {}
        pb = (prev.get("bgp") or {})
        if b.get("in_updates") is not None and pb.get("in_updates") is not None and dt > 0:
            b["in_updates_s"] = round(max(0, b["in_updates"] - pb["in_updates"]) / dt, 1)
        if errs:
            rec["errors"] = errs
        self.prev = {"t": now, "ifs": ifs, "bgp": b}
        return rec


def _abbrev(name: str) -> str:
    """GigabitEthernet0/0/1 -> gi0/0/1 (Cisco ifName style)."""
    m = re.match(r"([A-Za-z]+)\s*([\d/.:]+)$", name.strip())
    return (m.group(1)[:2] + m.group(2)).lower() if m else name.lower()
