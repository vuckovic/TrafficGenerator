# SPDX-License-Identifier: MIT
"""DUT log parsing and evaluation (P5, spec §11, controls L1-L6).

Input: the log the controller saved at the end of the run (dut-bgp.log) and any files uploaded
for the run (dutlogs/*). Every line becomes {time, neighbour, event, detail, severity}; lines
that match no pattern are kept as "unclassified". The DUT clock is aligned to ours by matching
the DUT's "neighbour up" lines to the times our routers reached Established (or set by hand).
The evaluation (counts by type and phase, unexpected session downs with their cause, the first
error under load, agreement with what our routers saw) goes to dut-events.json, the report and
figure F9.
"""
from __future__ import annotations

import gzip
import json
import re
import statistics
from datetime import datetime, timedelta, timezone
from pathlib import Path

PLATFORMS = ("cisco", "mikrotik", "bird", "frr")
EVENTS = ("up", "down", "notification_sent", "notification_received", "hold_expired", "max_prefix_warning",
          "max_prefix_exceeded", "resource", "error", "config", "other")
SEVERITY = {"up": "info", "down": "warning", "notification_sent": "warning", "notification_received": "warning",
            "hold_expired": "error", "max_prefix_warning": "warning", "max_prefix_exceeded": "error",
            "resource": "error", "error": "error", "config": "info", "other": "info"}
FAULTS = ("hold_expired", "max_prefix_warning", "max_prefix_exceeded", "resource", "error")
IP = r"(\d{1,3}(?:\.\d{1,3}){3})"
MONTHS = {m: i + 1 for i, m in enumerate("Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split())}

# ---------------------------------------------------------------- time stamps
TS = [  # (regex at the start of the line, kind)
    (re.compile(r"^\s*(?:\d+:\s*)?[*.]?(?P<mon>[A-Z][a-z]{2})\s+(?P<day>\d{1,2})\s+(?:(?P<year>\d{4})\s+)?"
                r"(?P<h>\d{1,2}):(?P<m>\d{2}):(?P<s>\d{2})(?:\.(?P<f>\d{1,6}))?(?:\s+(?P<tz>[A-Z]{2,5}))?:?\s+"), "mon"),
    (re.compile(r"^\s*(?P<y>\d{4})[-/](?P<mo>\d{2})[-/](?P<d>\d{2})[ T](?P<h>\d{2}):(?P<m>\d{2}):(?P<s>\d{2})"
                r"(?:[.,](?P<f>\d{1,6}))?(?P<off>[+-]\d{2}:?\d{2}|Z)?\s+"), "iso"),
    (re.compile(r"^\s*(?P<mon>[a-z]{3})/(?P<day>\d{2})/(?P<y>\d{4})\s+(?P<h>\d{2}):(?P<m>\d{2}):(?P<s>\d{2})\s+"), "ros6"),
    (re.compile(r"^\s*(?P<h>\d{2}):(?P<m>\d{2}):(?P<s>\d{2})\s+"), "hms"),
]


def parse_time(line: str, year: int, day: datetime) -> tuple[float | None, str]:
    """-> (epoch seconds as written on the device, treated as UTC; rest of the line). Times without a
    date (RouterOS "hh:mm:ss" for today) take the run's date. None when the line has no clock time
    (e.g. Cisco uptime stamps "1w2d:")."""
    for rx, kind in TS:
        m = rx.match(line)
        if not m:
            continue
        g = m.groupdict()
        frac = float("0." + g["f"]) if g.get("f") else 0.0
        try:
            if kind == "mon":
                y = int(g["year"]) if g.get("year") else year
                dt = datetime(y, MONTHS[g["mon"]], int(g["day"]), int(g["h"]), int(g["m"]), int(g["s"]), tzinfo=timezone.utc)
            elif kind == "ros6":
                dt = datetime(int(g["y"]), MONTHS[g["mon"].capitalize()], int(g["day"]), int(g["h"]), int(g["m"]),
                              int(g["s"]), tzinfo=timezone.utc)
            elif kind == "iso":
                dt = datetime(int(g["y"]), int(g["mo"]), int(g["d"]), int(g["h"]), int(g["m"]), int(g["s"]), tzinfo=timezone.utc)
                # a zone suffix (journalctl short-iso) is ignored: the alignment finds the offset anyway
            else:
                dt = day.replace(hour=int(g["h"]), minute=int(g["m"]), second=int(g["s"]), microsecond=0)
        except (ValueError, KeyError):
            return None, line
        return dt.timestamp() + frac, line[m.end():]
    return None, line


# ---------------------------------------------------------------- patterns per platform
def _n(m, key="n"):
    return m.group(key) if m and key in m.re.groupindex else None


P = {
    "cisco": [
        (r"%BGP-5-ADJCHANGE: neighbor " + IP + r"(?: vpn vrf \S+)? Up", "up"),
        (r"%BGP-5-ADJCHANGE: neighbor " + IP + r"(?: vpn vrf \S+)? Down\s*(?P<d>.*)", "down"),
        (r"%BGP_SESSION-5-ADJCHANGE: neighbor " + IP + r".*?(?:removed from session|session closed)\s*(?P<d>.*)", "down"),
        (r"%BGP-3-NOTIFICATION: sent to neighbor " + IP + r"\s*(?:\(\S+\)\s*)?(?P<d>.*)", "notification_sent"),
        (r"%BGP-3-NOTIFICATION: received from neighbor " + IP + r"\s*(?:\(\S+\)\s*)?(?P<d>.*)", "notification_received"),
        (r"%BGP-\d-NBR_RESET: Neighbor " + IP + r" reset\s*(?P<d>.*)", "down"),
        (r"%BGP-\d-MAXPFXEXCEED: .*?from " + IP + r"(?P<d>.*)", "max_prefix_exceeded"),
        (r"%BGP-\d-MAXPFX: .*?from " + IP + r"(?P<d>.*)", "max_prefix_warning"),
        (r"%SYS-\d-(?:MALLOCFAIL|CPUHOG|CHUNKMALLOCFAIL|NOMEMORY)\S*:?\s*(?P<d>.*)", "resource"),
        (r"%(?:BGP|TCP)-\d-\w*(?:ERR|FAIL|BADMSG)\w*:?\s*(?P<d>.*)", "error"),
        (r"%SYS-5-CONFIG_I:\s*(?P<d>.*)", "config"),
        (r"%BGP\S*-\d-\S+:\s*(?P<d>.*)", "other"),
    ],
    "frr": [
        (r"%ADJCHANGE: neighbor " + IP + r"\S*(?: in vrf \S+)? Up", "up"),
        (r"%ADJCHANGE: neighbor " + IP + r"\S*(?: in vrf \S+)? Down\s*(?P<d>.*)", "down"),
        (r"%NOTIFICATION(?:\(\w+\))?: sent to neighbor " + IP + r"\S*\s*(?P<d>.*)", "notification_sent"),
        (r"%NOTIFICATION(?:\(\w+\))?: received from neighbor " + IP + r"\S*\s*(?P<d>.*)", "notification_received"),
        (r"%MAXPFXEXCEED: .*?" + IP + r"(?P<d>.*)", "max_prefix_exceeded"),
        (r"%MAXPFX: .*?" + IP + r"(?P<d>.*)", "max_prefix_warning"),
        (r"(?i)" + IP + r".*?hold timer expired(?P<d>.*)", "hold_expired"),
        (r"(?i)(?:out of memory|memory (?:exhausted|allocation failed))(?P<d>.*)", "resource"),
    ],
    "bird": [
        (r"(?P<n>[\w.-]+): State changed to up", "up"),
        (r"(?P<n>[\w.-]+): State changed to (?:down|stop|flush)\s*(?P<d>.*)", "down"),
        (r"(?i)(?P<n>[\w.-]+): Received: Hold timer expired(?P<d>.*)", "hold_expired"),
        (r"(?i)(?P<n>[\w.-]+): Error: Hold timer expired(?P<d>.*)", "hold_expired"),
        (r"(?P<n>[\w.-]+): Received: (?P<d>.*)", "notification_received"),
        (r"(?P<n>[\w.-]+): Error: (?P<d>.*)", "notification_sent"),
        (r"(?i)(?P<n>[\w.-]+): .*?(?:route|import) limit exceeded(?P<d>.*)", "max_prefix_exceeded"),
        (r"(?i)(?P<n>[\w.-]+): .*?(?:route|import) limit .*?warn(?P<d>.*)", "max_prefix_warning"),
        (r"(?i)(?:out of memory|cannot allocate)(?P<d>.*)", "resource"),
    ],
    "mikrotik": [   # RouterOS 7 terse: "<topics> <message>"; neighbour by session name (tgen-R01-1) or address
        # 7.11 at topics bgp,debug,state logs only "Entering OpenConfirm" (one per session, just before
        # Established) with "ConnId{ <dut>:179 -> <peer>:<port> }": the peer is the address after "->"
        (r"Entering (?:OpenConfirm|Established) state.*?-> " + IP + r":\d+(?P<d>)", "up"),
        (r"(?i)(?P<n>tgen-R\d+)\S*.*?\b(?:established|state:? ?established|went up)\b(?P<d>.*)", "up"),
        (r"(?i)" + IP + r".*?\b(?:established|went up)\b(?P<d>.*)", "up"),
        (r"(?i)(?P<n>tgen-R\d+)\S*.*?hold ?timer.*?expired(?P<d>.*)", "hold_expired"),
        (r"(?i)(?P<n>tgen-R\d+)\S*.*?notification.*?(?:sent|send)(?P<d>.*)", "notification_sent"),
        (r"(?i)(?P<n>tgen-R\d+)\S*.*?notification(?P<d>.*)", "notification_received"),
        (r"(?i)(?P<n>tgen-R\d+)\S*.*?(?:terminated|closed|went down|\bdown\b|idle)(?P<d>.*)", "down"),
        (r"(?i)" + IP + r".*?(?:terminated|closed|went down|\bdown\b)(?P<d>.*)", "down"),
        (r"(?i)(?P<n>tgen-R\d+)\S*.*?(?:prefix limit|max.?prefix)(?P<d>.*)", "max_prefix_exceeded"),
        (r"(?i)(?:out of memory|not enough memory)(?P<d>.*)", "resource"),
        (r"(?i)(?:added|removed|changed) by \S+(?P<d>.*)", "config"),
    ],
}
PC = {k: [(re.compile(rx), ev) for rx, ev in v] for k, v in P.items()}


def detect(text: str) -> str | None:
    """L3 auto: which platform wrote this log."""
    score = {
        "cisco": len(re.findall(r"%(?:BGP|SYS|BGP_SESSION)-\d-", text)),
        "frr": len(re.findall(r"%ADJCHANGE|%NOTIFICATION|BGP: \[", text)),
        "bird": len(re.findall(r"<(?:INFO|RMT|WARN|ERR|TRACE|AUTH|BUG|FATAL)>|State changed to|bird(?:2|6)?\[\d+\]", text)),
        "mikrotik": len(re.findall(r"\broute,bgp\b|\bbgp,(?:info|error|warning|debug)\b|tgen-R\d+-\d", text)),
    }
    best = max(score, key=score.get)
    return best if score[best] else None


def neighbour_map(meta: dict, results: list[dict]) -> dict[str, str]:
    """address, BIRD protocol and RouterOS session names -> our router name."""
    out: dict[str, str] = {}
    for res in results:
        for r in res["routers"]:
            n, ip = r["name"], r.get("ip")
            out[n] = n
            if ip:
                out[ip] = n
                out["n_" + ip.replace(".", "_")] = n
            out[f"tgen_{n}"] = n
            out[f"tgen-{n}"] = n
    return out


def parse(text: str, platform: str, year: int, day: datetime, nmap: dict[str, str]) -> tuple[list, list]:
    """-> (events [[t_dev, neighbour|None, router|None, event, detail, line_no]], unclassified [[t_dev, line, line_no]])"""
    events, uncl = [], []
    for i, raw in enumerate(text.splitlines(), 1):
        line = raw.rstrip()
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        t, rest = parse_time(line, year, day)
        if t is None:   # BIRD via journalctl: "2026-11-12T22:15:01+0100 host bird[1]: msg" is handled by iso;
            rest = line  # others keep the whole line
        hit = None
        for rx, ev in PC.get(platform, []):
            m = rx.search(rest)
            if m:
                hit = (m, ev)
                break
        if not hit:
            uncl.append([t, line[:400], i])
            continue
        m, ev = hit
        nb = None
        if m.groups():
            gi = m.re.groupindex
            nb = m.group("n") if "n" in gi else (m.group(1) if m.re.groups and re.fullmatch(IP, m.group(1) or "") else None)
        detail = (m.group("d") if "d" in m.re.groupindex and m.group("d") else "").strip(" :,-")
        router = None
        if nb:
            router = nmap.get(nb) or nmap.get(re.sub(r"-\d+$", "", nb))
        events.append([t, nb, router, ev, detail[:200] or rest.strip()[:200], i])
    return events, uncl


# ---------------------------------------------------------------- clock alignment (L4)
def align(dut_events: list, ours_up: dict[str, list[float]], ours_down: dict[str, list] | None = None) -> tuple[float | None, int]:
    """Offset (device clock minus ours, s): DUT "up" lines vs. our Established times, and DUT down /
    notification lines vs. our session downs; the difference shared by the most routers (2 s bins),
    then the median within it. -> (offset, number of routers that agree)."""
    downs = {r: [t for t, _ in v] for r, v in (ours_down or {}).items()}
    cand = []
    for t, _, router, ev, *_ in dut_events:
        if t is None or not router:
            continue
        ref = ours_up.get(router, []) if ev == "up" else downs.get(router, []) if ev in (
            "down", "notification_sent", "notification_received", "hold_expired") else []
        for u in ref:
            cand.append((t - u, router))
    if not cand:
        return None, 0
    bins: dict[int, set] = {}
    for d, r in cand:
        bins.setdefault(round(d / 2), set()).add(r)
    # ties are common when the log holds several runs (BIRD's file, a syslog server): each earlier or later
    # run pairs with as many of our routers. Then prefer a whole time-zone step (15 min) and more pairs.
    npairs: dict[int, int] = {}
    for d, _ in cand:
        npairs[round(d / 2)] = npairs.get(round(d / 2), 0) + 1

    def zone_like(b: int) -> bool:
        return abs(2 * b - round(2 * b / 900) * 900) <= 4
    best = max(bins, key=lambda b: (len(bins[b]), zone_like(b), npairs[b], -abs(b)))
    near = [d for d, r in cand if abs(d / 2 - best) <= 1]
    return round(statistics.median(near), 3), len(bins[best])


# ---------------------------------------------------------------- evaluation (L6)
def read_logs(rdir: Path) -> list[tuple[str, str]]:
    """dut-bgp.log and every uploaded file (dutlogs/*, .gz allowed) -> [(name, text)]."""
    out = []
    f = rdir / "dut-bgp.log"
    if f.exists():
        out.append(("dut-bgp.log", f.read_text(errors="replace")))
    up = rdir / "dutlogs"
    if up.is_dir():
        for p in sorted(up.iterdir()):
            if p.is_file():
                data = p.read_bytes()
                if p.suffix == ".gz" or data[:2] == b"\x1f\x8b":
                    try:
                        data = gzip.decompress(data)
                    except OSError:
                        continue
                out.append((f"dutlogs/{p.name}", data.decode(errors="replace")))
    return out


def evaluate(rdir: Path, meta: dict, results: list[dict], phases: list[dict], ticks: list[dict],
             settings: dict | None = None, adapter: str | None = None) -> dict:
    """Parse the run's DUT logs, align, evaluate; writes dut-events.json and returns its summary part."""
    settings = settings or {}
    sfile = rdir / "dutlog-settings.json"
    if not settings and sfile.exists():
        settings = json.loads(sfile.read_text())
    t = meta["timing"]
    t0 = t["establish_at"] / 1000
    end = (t.get("end_at") or (ticks[-1]["t_ms"] if ticks else t["announce_at"])) / 1000
    day = datetime.fromtimestamp(t0, timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    nmap = neighbour_map(meta, results)
    files = read_logs(rdir)
    want = settings.get("platform") or "auto"
    events, uncl, used = [], [], []
    for name, text in files:
        plat = want if want != "auto" else (detect(text) or {"bird-ssh": "bird", "cisco": "cisco", "mikrotik": "mikrotik",
                                                             "frr": "frr", "bird": "bird"}.get(adapter or "", None))
        if not plat:
            uncl += [[None, l[:400], i, name] for i, l in enumerate(text.splitlines(), 1) if l.strip()][:2000]
            used.append({"file": name, "platform": None, "lines": len(text.splitlines())})
            continue
        ev, un = parse(text, plat, day.year, day, nmap)
        events += [e + [name] for e in ev]
        uncl += [u + [name] for u in un]
        used.append({"file": name, "platform": plat, "lines": len(text.splitlines()), "events": len(ev), "unclassified": len(un)})
    # our own session events (agent 0.4.2): router -> up times / down times
    ours_up: dict[str, list[float]] = {}
    ours_down: dict[str, list[tuple[float, str]]] = {}
    for res in results:
        for r in res["routers"]:
            for e in r.get("session_events", []):
                ts = e[0] / 1e9
                if e[1] == "up":
                    ours_up.setdefault(r["name"], []).append(ts)
                else:
                    ours_down.setdefault(r["name"], []).append((ts, e[1]))
            if not r.get("session_events") and r.get("t_established"):
                ours_up.setdefault(r["name"], []).append(r["t_established"] / 1e9)
    # L4 offset
    mode = settings.get("offset", "auto")
    pairs = 0
    if isinstance(mode, (int, float)):
        offset, source = float(mode), "manual"
    else:
        offset, pairs = align(events, ours_up, ours_down)
        source = "auto" if offset is not None else "none"
        if offset is None:
            # no matching session lines: assume only the time zone differs (whole quarter hours)
            ts = sorted(e[0] for e in events if e[0] is not None)
            if ts:
                guess = round((ts[len(ts) // 2] - (t0 + end) / 2) / 900) * 900
                offset, source = float(guess), "zone"
            else:
                offset = 0.0
    # a manual offset is entered as "DUT clock minus ours" like the automatic one
    # keep this run's window: from 60 s before the start to 180 s after the end
    lo, hi = t0 - 60, end + 180

    def rel(td):
        return None if td is None else round(td - offset - t0, 3)

    def phase_at(tr):
        if tr is None:
            return None
        name = None
        for p in phases:
            if tr >= (p["at_ms"] / 1000 - t0) - 0.001:
                name = p["type"]
        return name
    out_ev = []
    for td, nb, router, ev, detail, ln, fname in events:
        tr = rel(td)
        if td is not None and not (lo <= td - offset <= hi):
            continue
        out_ev.append([tr, router or nb, ev, detail, SEVERITY[ev], phase_at(tr), fname, ln])
    out_ev.sort(key=lambda e: (e[0] is None, e[0] or 0))
    out_un = [[rel(td), line, fname, ln] for td, line, ln, fname in uncl
              if td is None or lo <= td - offset <= hi][:3000]
    # counts
    counts: dict[str, int] = {}
    by_phase: dict[str, dict[str, int]] = {}
    for e in out_ev:
        counts[e[2]] = counts.get(e[2], 0) + 1
        by_phase.setdefault(e[5] or "—", {}).setdefault(e[2], 0)
        by_phase[e[5] or "—"][e[2]] += 1
    # unexpected downs: a DUT "down" not matched by a planned down of ours (flap, teardown) within 10 s
    teardown_at = next((p["at_ms"] / 1000 - t0 for p in phases if p["type"] == "teardown"), None)
    unexpected = []
    for e in out_ev:
        if e[2] != "down" or e[0] is None:
            continue
        r = e[1]
        planned = any(k == "down_planned" and abs((ts - t0) - e[0]) <= 10 for ts, k in ours_down.get(r, []))
        if planned or (teardown_at is not None and e[0] >= teardown_at - 2):
            continue
        cause = next((x[3] for x in out_ev if x[1] == r and x[2] in ("notification_sent", "notification_received",
                                                                      "hold_expired", "max_prefix_exceeded")
                      and x[0] is not None and abs(x[0] - e[0]) <= 5), e[3])
        unexpected.append([e[0], r, cause, e[5]])
    # first error under load, with the offered rate at that moment
    first = next((e for e in out_ev if e[2] in FAULTS and e[0] is not None), None)
    first_err = None
    if first:
        tk = [x for x in ticks if x.get("type", "tick") == "tick"]
        near = min(tk, key=lambda x: abs(x["t_ms"] / 1000 - t0 - first[0]), default=None)
        rate = ((near or {}).get("rates") or {}).get("sent_s")
        first_err = {"t_s": first[0], "router": first[1], "event": first[2], "detail": first[3], "phase": first[5],
                     "offered_updates_s": rate}
    # agreement: per router, DUT up/down count vs ours, in the window
    agree = []
    for r in sorted(set(ours_up) | set(ours_down)):
        du = sum(1 for e in out_ev if e[1] == r and e[2] == "up")
        dd = sum(1 for e in out_ev if e[1] == r and e[2] == "down")
        ou = len(ours_up.get(r, []))
        od = len(ours_down.get(r, []))
        agree.append([r, ou, du, od, dd])
    matched = sum(1 for a in agree if a[1] == a[2])
    summary = {"files": used, "platform": next((u["platform"] for u in used if u["platform"]), None),
               "offset_s": round(offset, 3), "offset_source": source, "offset_pairs": pairs,
               "events": len(out_ev), "unclassified": len(out_un), "counts": counts, "by_phase": by_phase,
               "unexpected_downs": unexpected[:100], "first_error": first_err,
               "agreement": {"routers": len(agree), "up_counts_match": matched, "rows": agree[:200]}}
    doc = summary | {"events_list": out_ev[:5000], "unclassified_list": out_un,
                     "settings": {"platform": want, "offset": mode}}
    (rdir / "dut-events.json").write_text(json.dumps(doc, separators=(",", ":")))
    return summary


def gate_line(s: dict) -> tuple[str, None, str]:
    """INFO row for the verdict table."""
    if not s["files"]:
        return "DUT log", None, "no DUT log for this run"
    off = {"auto": f"clock offset {s['offset_s']:+.1f} s (auto, {s['offset_pairs']} routers)",
           "manual": f"clock offset {s['offset_s']:+.1f} s (manual)",
           "zone": f"clock offset {s['offset_s']:+.0f} s (time zone only: no matching session lines)",
           "none": "clock offset not found"}[s["offset_source"]]
    u = len(s["unexpected_downs"])
    fe = s["first_error"]
    return ("DUT log", None, f"{s['events']} events, {u} unexpected session down{'s' if u != 1 else ''}"
            + (f", first {fe['event'].replace('_', ' ')} at {fe['t_s']:.0f} s" if fe else "") + f"; {off}")
