# SPDX-License-Identifier: MIT
"""Unit tests of the DUT log parsers and the evaluation (P5, docs/10).
  python3 -m pytest tests/controller/test_dutlog.py
"""
from __future__ import annotations

import gzip
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "tools"))
from tgen import dutlog  # noqa: E402

FIX = REPO / "tests" / "fixtures" / "dut-logs"
DAY = datetime(2026, 11, 12, tzinfo=timezone.utc)
T0 = DAY.timestamp() + 20 * 3600          # our run starts 20:00:00 UTC


def run(n=12, ip0=20, ups=None, downs=None):
    """meta + results for n routers R01.. at 10.100.0.<ip0+i>; ups[i] = Established time (s from T0)."""
    ups = ups or [1.0 + 0.2 * i for i in range(n)]
    routers = []
    for i in range(n):
        ev = [[int((T0 + ups[i]) * 1e9), "up", ""]]
        for (j, t, kind) in downs or []:
            if j == i:
                ev.append([int((T0 + t) * 1e9), kind, ""])
        routers.append({"name": f"R{i + 1:02d}", "ip": f"10.100.0.{ip0 + i}", "session_events": ev,
                        "t_established": int((T0 + ups[i]) * 1e9)})
    meta = {"timing": {"establish_at": T0 * 1000, "announce_at": (T0 + 10) * 1000, "end_at": (T0 + 300) * 1000}}
    phases = [{"type": "establish", "at_ms": T0 * 1000}, {"type": "announce", "at_ms": (T0 + 10) * 1000},
              {"type": "flap", "at_ms": (T0 + 100) * 1000}, {"type": "teardown", "at_ms": (T0 + 280) * 1000}]
    ticks = [{"t_ms": (T0 + s) * 1000, "rates": {"sent_s": 100.0 * s}} for s in range(0, 300, 5)]
    return meta, [{"node": "cmnode02", "routers": routers}], phases, ticks


def hms(t):
    return datetime.fromtimestamp(t, timezone.utc)


# ---------------------------------------------------------------- time stamps
def test_parse_time_formats():
    y = 2026
    t, rest = dutlog.parse_time("*Nov 12 20:01:02.345: %BGP-5-ADJCHANGE: x", y, DAY)
    assert abs(t - (T0 + 62.345)) < 1e-6 and rest.startswith("%BGP")
    t, _ = dutlog.parse_time("000123: Nov 12 2026 20:01:02 CET: %BGP-5-ADJCHANGE: x", y, DAY)
    assert t == T0 + 62
    t, _ = dutlog.parse_time("2026-11-12T20:01:02+0100 host bird[11]: x", y, DAY)
    assert t == T0 + 62
    t, _ = dutlog.parse_time("2026/11/12 20:01:02.5 BGP: x", y, DAY)
    assert t == T0 + 62.5
    t, _ = dutlog.parse_time("nov/12/2026 20:01:02 route,bgp,info x", y, DAY)
    assert t == T0 + 62
    t, _ = dutlog.parse_time(" 20:01:02 route,bgp,info x", y, DAY)
    assert t == T0 + 62
    t, rest = dutlog.parse_time("1w2d: %BGP-5-ADJCHANGE: x", y, DAY)
    assert t is None and rest.startswith("1w2d")


# ---------------------------------------------------------------- parsers
CISCO = """# device clock at collection: *21:10:00.000 CET Thu Nov 12 2026
*Nov 12 21:00:01.100: %BGP-5-ADJCHANGE: neighbor 10.100.0.20 Up
*Nov 12 21:00:01.300: %BGP-5-ADJCHANGE: neighbor 10.100.0.21 Up
*Nov 12 21:01:30.000: %BGP-5-MAXPFX: Number of prefixes received from 10.100.0.20 (afi 0) reaches 750, max 1000
*Nov 12 21:01:40.000: %BGP-3-NOTIFICATION: sent to neighbor 10.100.0.21 4/0 (hold time expired) 0 bytes
*Nov 12 21:01:40.010: %BGP-5-ADJCHANGE: neighbor 10.100.0.21 Down BGP Notification sent
*Nov 12 21:01:40.020: %BGP_SESSION-5-ADJCHANGE: neighbor 10.100.0.21 IPv4 Unicast topology base removed from session  BGP Notification sent
*Nov 12 21:02:00.000: %SYS-2-MALLOCFAIL: Memory allocation of 65536 bytes failed from 0x1234, alignment 0
*Nov 12 21:02:05.000: %LINK-3-UPDOWN: Interface GigabitEthernet0/0/1, changed state to up
"""


def test_cisco_parse():
    nmap = dutlog.neighbour_map({}, run()[1])
    ev, un = dutlog.parse(CISCO, "cisco", 2026, DAY, nmap)
    kinds = [e[3] for e in ev]
    assert kinds == ["up", "up", "max_prefix_warning", "notification_sent", "down", "down", "resource"]
    assert ev[0][2] == "R01" and ev[1][2] == "R02" and ev[3][2] == "R02"
    assert "hold time expired" in ev[3][4]
    assert len(un) == 1 and "LINK-3-UPDOWN" in un[0][1]
    assert dutlog.detect(CISCO) == "cisco"


FRR = """2026/11/12 20:00:01 BGP: [M7Q4P-46WDR] %ADJCHANGE: neighbor 10.100.0.20(Unknown) in vrf default Up
2026/11/12 20:00:50 BGP: [HZN6M-XRM1G] %NOTIFICATION: received from neighbor 10.100.0.20 6/2 (Cease/Administrative Shutdown) 0 bytes
2026/11/12 20:00:50 BGP: [M7Q4P-46WDR] %ADJCHANGE: neighbor 10.100.0.20(Unknown) in vrf default Down Peer closed the session
2026/11/12 20:00:55 BGP: something else
"""


def test_frr_parse():
    ev, un = dutlog.parse(FRR, "frr", 2026, DAY, dutlog.neighbour_map({}, run()[1]))
    assert [e[3] for e in ev] == ["up", "notification_received", "down"]
    assert all(e[2] == "R01" for e in ev) and "Cease" in ev[1][4]
    assert len(un) == 1
    assert dutlog.detect(FRR) == "frr"


BIRD = """2026-11-12 20:00:01.123 <TRACE> n_10_100_0_20: State changed to up
2026-11-12 20:00:01.200 <TRACE> tgen_R02: State changed to up
2026-11-12 20:02:00.000 <RMT> n_10_100_0_20: Received: Hold timer expired
2026-11-12 20:02:00.001 <TRACE> n_10_100_0_20: State changed to stop
2026-11-12 20:02:00.002 <INFO> Reconfigured
"""


def test_bird_parse():
    ev, un = dutlog.parse(BIRD, "bird", 2026, DAY, dutlog.neighbour_map({}, run()[1]))
    assert [(e[2], e[3]) for e in ev] == [("R01", "up"), ("R02", "up"), ("R01", "hold_expired"), ("R01", "down")]
    assert len(un) == 1 and dutlog.detect(BIRD) == "bird"


MTIK = """ 20:00:01 route,bgp,info tgen-R01-1 established
 20:00:40 route,bgp,error tgen-R02-1 Hold timer expired
 20:00:40 route,bgp,info tgen-R02-1 session terminated
 20:00:41 system,info user admin logged in
"""


def test_mikrotik_parse():
    ev, un = dutlog.parse(MTIK, "mikrotik", 2026, DAY, dutlog.neighbour_map({}, run()[1]))
    assert [(e[2], e[3]) for e in ev] == [("R01", "up"), ("R02", "hold_expired"), ("R02", "down")]
    assert len(un) == 1


def test_mikrotik_real_fixture():
    """RouterOS 7.11.2 logs only "Entering OpenConfirm" with the peer after "->"; clock 14 h off."""
    text = (FIX / "mikrotik-7.11.2-crs106.log").read_text()
    assert dutlog.detect(text) == "mikrotik"
    ev, un = dutlog.parse(text, "mikrotik", 2026, DAY, dutlog.neighbour_map({}, run(n=12, ip0=20)[1]))
    assert len(ev) == 12 and not un
    assert {e[2] for e in ev} == {f"R{i:02d}" for i in range(1, 13)} and all(e[3] == "up" for e in ev)


# ---------------------------------------------------------------- alignment and evaluation
def test_align_finds_offset_and_ignores_outliers():
    ours = {f"R{i:02d}": [T0 + i] for i in range(1, 11)}
    dut = [[T0 + i + 3600.4, None, f"R{i:02d}", "up", "", 1] for i in range(1, 11)]
    dut.append([T0 + 5000, None, "R03", "up", "", 1])          # a re-establish we didn't see
    off, n = dutlog.align(dut, ours)
    assert abs(off - 3600.4) < 0.01 and n == 10
    assert dutlog.align([], ours) == (None, 0)


def _cisco_run_log(offset):
    """A Cisco log 'offset' s ahead of our clock: 12 ups, MAXPFX at 60 s, an unplanned down at 150 s on R03
    (hold timer), a planned flap down at 100 s on R05 and the teardown at 285 s."""
    f = lambda s: hms(T0 + s + offset).strftime("*%b %d %H:%M:%S.") + f"{int((T0 + s) * 1000) % 1000:03d}"  # noqa: E731
    lines = [f"{f(1.0 + 0.2 * i)}: %BGP-5-ADJCHANGE: neighbor 10.100.0.{20 + i} Up" for i in range(12)]
    lines += [f"{f(60)}: %BGP-5-MAXPFX: Number of prefixes received from 10.100.0.20 (afi 0) reaches 750, max 1000",
              f"{f(100.5)}: %BGP-5-ADJCHANGE: neighbor 10.100.0.24 Down Peer closed the session",
              f"{f(150)}: %BGP-3-NOTIFICATION: sent to neighbor 10.100.0.22 4/0 (hold time expired) 0 bytes",
              f"{f(150.01)}: %BGP-5-ADJCHANGE: neighbor 10.100.0.22 Down BGP Notification sent",
              f"{f(285)}: %BGP-5-ADJCHANGE: neighbor 10.100.0.21 Down Peer closed the session",
              f"{f(-5000)}: %BGP-5-ADJCHANGE: neighbor 10.100.0.20 Up"]   # an older run: outside the window
    return "\n".join(lines) + "\n"


def test_evaluate_cisco(tmp_path):
    meta, results, phases, ticks = run(downs=[(4, 100.2, "down_planned")])
    (tmp_path / "dut-bgp.log").write_text(_cisco_run_log(3600))
    s = dutlog.evaluate(tmp_path, meta, results, phases, ticks, adapter="cisco")
    assert s["platform"] == "cisco" and s["offset_source"] == "auto" and abs(s["offset_s"] - 3600) < 0.5
    assert s["counts"]["up"] == 12                       # the old run's line is outside the window
    assert [u[1] for u in s["unexpected_downs"]] == ["R03"]
    assert "hold time expired" in s["unexpected_downs"][0][2]
    fe = s["first_error"]
    assert fe["event"] == "max_prefix_warning" and abs(fe["t_s"] - 60) < 0.5 and fe["offered_updates_s"] == 6000.0
    assert s["by_phase"]["announce"]["max_prefix_warning"] == 1 and s["by_phase"]["flap"]["down"] == 2 and s["by_phase"]["teardown"]["down"] == 1
    assert s["agreement"]["routers"] == 12 and s["agreement"]["up_counts_match"] == 12
    doc = json.loads((tmp_path / "dut-events.json").read_text())
    assert doc["events_list"][0][2] == "up" and doc["settings"] == {"platform": "auto", "offset": "auto"}
    name, verdict, text = dutlog.gate_line(s)
    assert verdict is None and "1 unexpected session down," in text and "+3600" in text


def test_evaluate_manual_offset_upload_gz_and_settings(tmp_path):
    meta, results, phases, ticks = run()
    (tmp_path / "dutlogs").mkdir()
    (tmp_path / "dutlogs" / "syslog.gz").write_bytes(gzip.compress(_cisco_run_log(-7200).encode()))
    (tmp_path / "dutlog-settings.json").write_text(json.dumps({"platform": "cisco", "offset": -7200}))
    s = dutlog.evaluate(tmp_path, meta, results, phases, ticks)
    assert s["offset_source"] == "manual" and s["offset_s"] == -7200
    assert s["files"][0]["file"] == "dutlogs/syslog.gz" and s["counts"]["up"] == 12


def test_evaluate_zone_fallback_and_no_log(tmp_path):
    meta, results, phases, ticks = run()
    # only lines we cannot pair with our sessions (resource messages), one hour ahead
    (tmp_path / "dut-bgp.log").write_text("".join(
        hms(T0 + 3600 + s).strftime("*%b %d %H:%M:%S.000") + ": %SYS-2-MALLOCFAIL: Memory allocation failed\n"
        for s in (50, 60, 70)))
    s = dutlog.evaluate(tmp_path, meta, results, phases, ticks, adapter="cisco")
    assert s["offset_source"] == "zone" and s["offset_s"] == 3600 and s["counts"] == {"resource": 3}
    empty = tmp_path / "e"
    empty.mkdir()
    s = dutlog.evaluate(empty, meta, results, phases, ticks)
    assert s["files"] == [] and dutlog.gate_line(s)[2] == "no DUT log for this run"
