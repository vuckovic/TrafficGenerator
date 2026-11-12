# SPDX-License-Identifier: MIT
"""SNMP v2c collector (U5) against a local net-snmp agent with fake BGP MIBs (tests/bgp/sim/snmp).
Skipped when snmpd / the net-snmp tools are not installed (apt install snmp snmpd).
  python3 -m pytest tests/controller/test_snmp.py
"""
from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "tools"))
from tgen import snmp  # noqa: E402

SIMSNMP = REPO / "tests" / "bgp" / "sim" / "snmp"
COMMUNITY = "tgen-unit-ro"
PEERS = ["10.100.0.20", "10.100.0.21", "10.100.0.22"]

pytestmark = pytest.mark.skipif(not (shutil.which("snmpd") and snmp.available()), reason="net-snmp not installed")


def free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def start_snmpd(work: Path, port: int, community: str, peers: list[str]) -> subprocess.Popen:
    conf = (SIMSNMP / "snmpd.conf.in").read_text().replace("@ADDR@", "127.0.0.1").replace("@PORT@", str(port)) \
        .replace("@COMMUNITY@", community).replace("@DIR@", str(SIMSNMP))
    (work / "snmpd.conf").write_text(conf)
    env = {**os.environ, "TGEN_SNMP_PEERS": ",".join(peers)}
    p = subprocess.Popen(["snmpd", "-f", "-C", "-c", str(work / "snmpd.conf"), "-Lf", str(work / "snmpd.log")], env=env)
    for _ in range(50):
        try:
            snmp.Session("127.0.0.1", community, port=port, timeout=0.3, retries=0).system()
            return p
        except snmp.SnmpError:
            time.sleep(0.1)
    p.kill()
    raise RuntimeError("snmpd did not start")


@pytest.fixture(scope="module")
def agent(tmp_path_factory):
    work = tmp_path_factory.mktemp("snmpd")
    port = free_port()
    p = start_snmpd(work, port, COMMUNITY, PEERS)
    yield port
    p.terminate()
    p.wait(5)


def test_parse_output():
    out = '.1.3.6.1.2.1.1.5.0 "rtr 1"\n.1.3.6.1.2.1.1.3.0 12345\n.1.3.6.1.2.1.15.3.1.2.10.0.0.1 No Such Instance\n'
    assert snmp.parse(out) == {".1.3.6.1.2.1.1.5.0": "rtr 1", ".1.3.6.1.2.1.1.3.0": "12345"}
    assert snmp._abbrev("GigabitEthernet0/0/1") == snmp._abbrev("Gi0/0/1") == "gi0/0/1"
    assert snmp.community_env("asr-920") == "TGEN_DUT_SNMP_ASR_920"


def test_system_and_poll(agent):
    s = snmp.Session("127.0.0.1", COMMUNITY, port=agent)
    sysinfo = s.system()
    assert sysinfo["name"] == "tgen-snmp-test" and sysinfo["uptime_s"] is not None and s.kind == "other"
    r1 = s.poll("lo", set(PEERS[:2]))
    assert r1["if"]["name"] == "lo" and r1["if"]["oper"] == "up" and r1["if"]["in_mbps"] is None   # no rate yet
    assert r1["free_mem_pct"] is not None and 0 < r1["free_mem_pct"] <= 100
    assert r1["bgp"] == {"peers": 2, "established": 2, "in_updates": r1["bgp"]["in_updates"], "accepted_prefixes": 1001}
    time.sleep(1.2)
    r2 = s.poll("lo", set(PEERS[:2]))
    assert r2["if"]["in_mbps"] is not None and r2["bgp"]["in_updates_s"] > 0


def test_wrong_community_and_no_secret_on_command_line(agent, monkeypatch):
    seen = []
    real = subprocess.run

    def spy(args, **kw):
        seen.append(list(args))
        assert Path(kw["env"]["SNMPCONFPATH"]).is_dir()
        return real(args, **kw)
    monkeypatch.setattr(snmp.subprocess, "run", spy)
    with pytest.raises(snmp.SnmpError, match="no answer"):
        snmp.Session("127.0.0.1", "wrong-community", port=agent, timeout=0.5, retries=0).system()
    assert seen and not any("wrong-community" in a for args in seen for a in args)
    with pytest.raises(snmp.SnmpError):
        snmp.Session("127.0.0.1", "", port=agent).system()
