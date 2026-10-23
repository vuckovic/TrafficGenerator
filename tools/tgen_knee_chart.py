#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Knee chart: every tested device's rate series in one self-contained HTML page.

  tools/tgen_knee_chart.py                         # newest series per device and MRAI in ~/tgen-test-results
  tools/tgen_knee_chart.py ~/tgen-test-results/suite-20261023-221000   # the series of one suite
  tools/tgen_knee_chart.py series-A series-B --out knee.html

Three views:
  1. delivered vs offered (log-log). A device that keeps up sits on the diagonal;
     the knee is where its line bends away. Hollow rings = past the knee.
  2. how long receivers were still incomplete after our last send, with the limit
     the knee rule uses (5 s, or 5 s + the device's advertisement interval).
  3. the highest offered rate each device sustained (log scale): the comparison.

Every point is re-judged with the current knee rule (tools/tgen/knee.py), so
series measured with an older rule are shown correctly. Open the file in any
browser; nothing is loaded from the network.
"""
from __future__ import annotations

import argparse
import html
import json
import math
import re
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "tools"))
from tgen import inventory  # noqa: E402
from tgen import knee as knee_rule  # noqa: E402

RESULTS = Path.home() / "tgen-test-results"
DASH = ' stroke-dasharray="7 5"'
# categorical slots (validated reference palette, light / dark); colour follows the device
SLOTS = [("#2a78d6", "#3987e5"), ("#eb6834", "#d95926"), ("#1baf7a", "#199e70"), ("#6250d6", "#9085e9"),
         ("#e87ba4", "#d55181"), ("#008300", "#008300")]
FIXED_SLOT = {"mikrotik": 0, "asr920": 1, "c1841": 2, "bird": 3}


# ------------------------------------------------------------------ data
def series_dirs(args: list[str]) -> list[Path]:
    if not args:
        return sorted(RESULTS.glob("series-*"))
    out = []
    for a in args:
        p = Path(a).expanduser()
        if not p.exists() and (RESULTS / a).exists():
            p = RESULTS / a
        if (p / "suite.json").exists():
            for dev in json.loads((p / "suite.json").read_text()).values():
                out += [Path(v["dir"]) for k, v in dev.items() if k.startswith("series") and isinstance(v, dict)
                        and v.get("dir")]
        elif (p / "series.json").exists():
            out.append(p)
        else:
            sys.exit(f"{a}: neither a series-* nor a suite-* folder")
    return out


def opt(passthrough: str, name: str) -> str | None:
    m = re.search(rf"{re.escape(name)}[ =](\S+)", passthrough)
    return m.group(1) if m else None


def load_series(d: Path, inv: dict) -> dict | None:
    md = (d / "series.md").read_text() if (d / "series.md").exists() else ""
    m = re.search(r"Passed to every run: `([^`]*)`", md)
    passthrough = m.group(1) if m else ""
    prof_name = opt(passthrough, "--dut-profile")
    prof = inv.get(prof_name) if prof_name else None
    device = prof_name or opt(passthrough, "--dut") or "bird"
    label = prof["label"] if prof else {"bird": "BIRD 2 stand-in"}.get(device, device)
    mrai_arg = opt(passthrough, "--dut-mrai")
    mrai = inventory.effective_mrai(prof, float(mrai_arg) if mrai_arg is not None else None)
    pushed = mrai_arg is not None
    rows = []
    for r in json.loads((d / "series.json").read_text()):
        if "error" in r or not r.get("offered_deliveries_s"):
            continue
        sw = r.get("send_window_s")
        if sw is None and r.get("dir") and (Path(r["dir"]) / "report.json").exists():
            sw = json.loads((Path(r["dir"]) / "report.json").read_text())["convergence"].get("send_window_s")
        k = knee_rule.past_knee(r["offered_deliveries_s"], r.get("dut_output_s"), r.get("after_last_send_s"), sw, mrai)
        rows.append({"rate": r["rate"], "offered": r["offered_deliveries_s"], "output": r.get("dut_output_s"),
                     "after": r.get("after_last_send_s"), "per_update": r.get("per_update"),
                     "routers": r.get("routers"), "missing": r.get("missing"), "dir": Path(r.get("dir") or "").name,
                     **k})
    if not rows:
        return None
    rows.sort(key=lambda x: x["offered"])
    ok = [x for x in rows if not x["past_knee"]]
    over = [x for x in rows if x["past_knee"]]
    mrai_label = (f"MRAI {mrai:g} s" + (" (pushed)" if pushed else " (device default)")) if (mrai or pushed) else ""
    return {"device": device, "label": label, "mrai": mrai, "mrai_label": mrai_label, "dir": d.name,
            "stamp": d.name.replace("series-", ""), "rows": rows,
            "sustainable": max((x["offered"] for x in ok), default=None),
            "knee_at": min((x["offered"] for x in over if x["offered"] > max((y["offered"] for y in ok), default=0)),
                           default=None),
            "limit_s": knee_rule.backlog_limit(mrai)}


# ------------------------------------------------------------------ svg helpers
def nice(v: float, up: bool) -> float:
    """Nearest 1/2/5 x 10^k above (up) or below v."""
    e = math.floor(math.log10(v))
    steps = [m * 10 ** k for k in (e - 1, e, e + 1) for m in (1, 2, 5)]
    return min(x for x in steps if x >= v) if up else max(x for x in steps if x <= v)


def log_ticks(lo: float, hi: float) -> list[float]:
    ticks, e = [], math.floor(math.log10(lo))
    mults = (1, 2, 5) if math.log10(hi / lo) <= 3.2 else (1, 3)
    while 10 ** e <= hi * 1.0001:
        for m in mults:
            v = m * 10 ** e
            if lo * 0.999 <= v <= hi * 1.001:
                ticks.append(v)
        e += 1
    return ticks


def fmt(v: float) -> str:
    return f"{v:,.0f}" if v >= 10 else f"{v:,.1f}".rstrip("0").rstrip(".")


class Plot:
    W, H, L, R, T, B = 760, 360, 70, 24, 18, 52

    def __init__(self, xlo, xhi, ylo, yhi, ylog=True):
        self.xlo, self.xhi, self.ylo, self.yhi, self.ylog = xlo, xhi, ylo, yhi, ylog
        self.parts: list[str] = []

    def x(self, v):
        return self.L + (math.log10(v) - math.log10(self.xlo)) / (math.log10(self.xhi) - math.log10(self.xlo)) * \
            (self.W - self.L - self.R)

    def y(self, v):
        if self.ylog:
            f = (math.log10(max(v, self.ylo)) - math.log10(self.ylo)) / (math.log10(self.yhi) - math.log10(self.ylo))
        else:
            f = (min(max(v, self.ylo), self.yhi) - self.ylo) / (self.yhi - self.ylo)
        return self.H - self.B - f * (self.H - self.T - self.B)

    def axes(self, xlabel, ylabel, yticks):
        p = self.parts
        for v in log_ticks(self.xlo, self.xhi):
            X = self.x(v)
            p.append(f'<line class="grid" x1="{X:.1f}" x2="{X:.1f}" y1="{self.T}" y2="{self.H - self.B}"/>')
            p.append(f'<text class="tick" x="{X:.1f}" y="{self.H - self.B + 18}" text-anchor="middle">{fmt(v)}</text>')
        for v in yticks:
            Y = self.y(v)
            p.append(f'<line class="grid" x1="{self.L}" x2="{self.W - self.R}" y1="{Y:.1f}" y2="{Y:.1f}"/>')
            p.append(f'<text class="tick" x="{self.L - 8}" y="{Y + 4:.1f}" text-anchor="end">{fmt(v)}</text>')
        p.append(f'<line class="axis" x1="{self.L}" x2="{self.W - self.R}" y1="{self.H - self.B}" y2="{self.H - self.B}"/>')
        p.append(f'<text class="axlabel" x="{(self.L + self.W - self.R) / 2}" y="{self.H - 10}" text-anchor="middle">'
                 f'{xlabel}</text>')
        p.append(f'<text class="axlabel" transform="translate(16,{(self.T + self.H - self.B) / 2}) rotate(-90)" '
                 f'text-anchor="middle">{ylabel}</text>')

    def svg(self, title):
        return (f'<svg viewBox="0 0 {self.W} {self.H}" role="img" aria-label="{html.escape(title)}">'
                + "".join(self.parts) + "</svg>")


def after_txt(r: dict) -> str:
    return "–" if r["after"] is None else f"{r['after']:.1f}"


def tip(s: dict, r: dict) -> str:
    verdict = "PAST THE KNEE" if r["past_knee"] else "kept up"
    why = (" (output too low)" if r["behind"] else "") + (" (still behind after last send)" if r["backlog"] else "")
    return html.escape(
        f"{s['label']}{' · ' + s['mrai_label'] if s['mrai_label'] else ''}\n"
        f"{r['rate']:g} prefixes/s per router × {r['routers']} routers\n"
        f"offered {r['offered']:,.0f} deliveries/s · delivered {r['output'] or 0:,.0f}/s\n"
        f"complete {r['after'] if r['after'] is not None else '–'} s after our last send "
        f"(limit {s['limit_s']:g} s)\n{verdict}{why}")


# ------------------------------------------------------------------ page
def build(series: list[dict]) -> str:
    devices = []
    for s in series:
        if s["device"] not in devices:
            devices.append(s["device"])
    free = [i for i in range(len(SLOTS)) if i not in FIXED_SLOT.values()]
    slot = {}
    for d in devices:
        if d in FIXED_SLOT:
            slot[d] = FIXED_SLOT[d]
        else:
            slot[d] = free.pop(0) if free else len(slot) % len(SLOTS)
    pts = [r for s in series for r in s["rows"]]
    xlo = nice(min(r["offered"] for r in pts) * 0.8, up=False)
    xhi = nice(max(r["offered"] for r in pts) * 1.25, up=True)
    # ---- 1. delivered vs offered
    p1 = Plot(xlo, xhi, xlo, xhi)
    p1.axes("offered deliveries / s (log)", "delivered by the DUT / s (log)", log_ticks(xlo, xhi))
    p1.parts.append(f'<line class="ideal" x1="{p1.x(xlo):.1f}" y1="{p1.y(xlo):.1f}" x2="{p1.x(xhi):.1f}" '
                    f'y2="{p1.y(xhi):.1f}"/>')
    p1.parts.append(f'<text class="note" x="{p1.x(xhi) - 6:.1f}" y="{p1.y(xhi) + 16:.1f}" text-anchor="end">'
                    f'keeping up: delivered = offered</text>')
    for s in series:
        c = f"var(--s{slot[s['device']]})"
        rs = [r for r in s["rows"] if r["output"]]
        d = " ".join(f"{'M' if i == 0 else 'L'}{p1.x(r['offered']):.1f},{p1.y(r['output']):.1f}" for i, r in enumerate(rs))
        dash = ' stroke-dasharray="7 5"' if s["mrai"] else ""
        p1.parts.append(f'<path d="{d}" fill="none" stroke="{c}" stroke-width="2"{dash}/>')
        for r in rs:
            cls = "ring" if r["past_knee"] else "dot"
            p1.parts.append(f'<circle class="{cls}" cx="{p1.x(r["offered"]):.1f}" cy="{p1.y(r["output"]):.1f}" r="5.5" '
                            f'style="--c:{c}"/><circle class="hit" cx="{p1.x(r["offered"]):.1f}" '
                            f'cy="{p1.y(r["output"]):.1f}" r="12" data-tip="{tip(s, r)}"/>')
        if s["knee_at"]:
            k = next(r for r in rs if r["offered"] == s["knee_at"])
            p1.parts.append(f'<text class="knee" x="{p1.x(k["offered"]) + 9:.1f}" y="{p1.y(k["output"]) + 4:.1f}">'
                            f'knee</text>')
    # ---- 2. still incomplete after last send
    afters = [r["after"] for r in pts if r["after"] is not None]
    yhi2 = max([*afters, *[s["limit_s"] for s in series]]) * 1.15
    p2 = Plot(xlo, xhi, 0, yhi2, ylog=False)
    step = max(5, round(yhi2 / 5 / 5) * 5)
    p2.axes("offered deliveries / s (log)", "seconds still incomplete after our last send",
            [v for v in range(0, int(yhi2) + 1, step)])
    for lim in sorted({s["limit_s"] for s in series}):
        Y = p2.y(lim)
        p2.parts.append(f'<line class="limit" x1="{p2.L}" x2="{p2.W - p2.R}" y1="{Y:.1f}" y2="{Y:.1f}"/>'
                        f'<text class="note" x="{p2.W - p2.R - 4}" y="{Y - 6:.1f}" text-anchor="end">knee limit '
                        f'{lim:g} s{" (5 s + MRAI)" if lim > knee_rule.BACKLOG_S else ""}</text>')
    for s in series:
        c = f"var(--s{slot[s['device']]})"
        rs = [r for r in s["rows"] if r["after"] is not None]
        if not rs:
            continue
        d = " ".join(f"{'M' if i == 0 else 'L'}{p2.x(r['offered']):.1f},{p2.y(r['after']):.1f}" for i, r in enumerate(rs))
        dash = ' stroke-dasharray="7 5"' if s["mrai"] else ""
        p2.parts.append(f'<path d="{d}" fill="none" stroke="{c}" stroke-width="2"{dash}/>')
        for r in rs:
            cls = "ring" if r["past_knee"] else "dot"
            p2.parts.append(f'<circle class="{cls}" cx="{p2.x(r["offered"]):.1f}" cy="{p2.y(r["after"]):.1f}" r="5.5" '
                            f'style="--c:{c}"/><circle class="hit" cx="{p2.x(r["offered"]):.1f}" '
                            f'cy="{p2.y(r["after"]):.1f}" r="12" data-tip="{tip(s, r)}"/>')
    # ---- 3. sustainable rate per device (bars, log)
    bars = sorted(series, key=lambda s: -(s["sustainable"] or 0))
    bh, gap, top = 26, 14, 10
    H3 = top + len(bars) * (bh + gap) + 44
    p3 = Plot(xlo, xhi, 0, 1, ylog=False)
    p3.H, p3.L, p3.B = H3, 230, 44
    for v in log_ticks(xlo, xhi):
        X = p3.x(v)
        p3.parts.append(f'<line class="grid" x1="{X:.1f}" x2="{X:.1f}" y1="{top}" y2="{H3 - 44}"/>'
                        f'<text class="tick" x="{X:.1f}" y="{H3 - 26}" text-anchor="middle">{fmt(v)}</text>')
    p3.parts.append(f'<text class="axlabel" x="{(p3.L + p3.W - p3.R) / 2}" y="{H3 - 6}" text-anchor="middle">'
                    f'highest offered deliveries / s the DUT kept up with (log)</text>')
    for i, s in enumerate(bars):
        Y = top + i * (bh + gap)
        c = f"var(--s{slot[s['device']]})"
        name = s["label"].split(" (")[0]
        p3.parts.append(f'<text class="barname" x="{p3.L - 10}" y="{Y + bh / 2 - 2:.1f}" text-anchor="end">'
                        f'{html.escape(name)}</text><text class="tick" x="{p3.L - 10}" y="{Y + bh / 2 + 12:.1f}" '
                        f'text-anchor="end">{html.escape(s["mrai_label"] or "no MRAI")}</text>')
        if s["sustainable"]:
            X = p3.x(s["sustainable"])
            hatch = ' class="bar hatch"' if s["mrai"] else ' class="bar"'
            p3.parts.append(f'<rect{hatch} x="{p3.L}" y="{Y}" width="{max(4, X - p3.L):.1f}" height="{bh}" rx="4" '
                            f'style="--c:{c}"/>')
            txt = (f"{s['sustainable']:,.0f} /s · knee before {s['knee_at']:,.0f}" if s["knee_at"] else
                   f"≥ {s['sustainable']:,.0f} /s · no knee reached")
            if X > p3.W - 230:      # long bar: label inside, right-aligned
                p3.parts.append(f'<text class="barin" x="{X - 8:.1f}" y="{Y + bh / 2 + 4:.1f}" text-anchor="end">'
                                f'{txt}</text>')
            else:
                p3.parts.append(f'<text class="barval" x="{X + 8:.1f}" y="{Y + bh / 2 + 4:.1f}">{txt}</text>')
        else:
            p3.parts.append(f'<text class="barval" x="{p3.L + 8}" y="{Y + bh / 2 + 4:.1f}">knee below the lowest rate '
                            f'tested ({s["rows"][0]["offered"]:,.0f} /s)</text>')
        p3.parts.append(f'<rect class="hit" x="0" y="{Y}" width="{p3.W}" height="{bh}" data-tip="'
                        + html.escape(f"{s['label']}\n{s['mrai_label'] or 'no MRAI'}\nseries {s['dir']}\n"
                                      + (f"kept up to {s['sustainable']:,.0f} deliveries/s" if s["sustainable"]
                                         else "past the knee at every rate tested")
                                      + (f"; knee before {s['knee_at']:,.0f}/s" if s["knee_at"] else "")) + '"/>')
    # ---- legend + table
    legend = "".join(
        f'<span class="lg"><svg width="34" height="12"><line x1="1" y1="6" x2="33" y2="6" stroke="var(--s{slot[s["device"]]})"'
        f' stroke-width="2"{DASH if s["mrai"] else ""}/></svg>'
        f'{html.escape(s["label"].split(" (")[0])}{" · " + html.escape(s["mrai_label"]) if s["mrai_label"] else ""}'
        f'</span>' for s in series)
    rows_html = "".join(
        f"<tr><td>{html.escape(s['label'].split(' (')[0])}</td><td>{html.escape(s['mrai_label'] or '–')}</td>"
        f"<td>{r['rate']:g}</td><td>{r['offered']:,.0f}</td><td>{(r['output'] or 0):,.0f}</td>"
        f"<td>{after_txt(r)}</td>"
        f"<td>{'past the knee' if r['past_knee'] else 'kept up'}</td><td>{html.escape(r['dir'])}</td></tr>"
        for s in series for r in s["rows"])
    css_slots_l = "".join(f"--s{i}:{l};" for i, (l, _) in enumerate(SLOTS))
    css_slots_d = "".join(f"--s{i}:{d};" for i, (_, d) in enumerate(SLOTS))
    stamp = time.strftime("%Y-%m-%d %H:%M")
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>BGP knee comparison</title>
<style>
:root{{color-scheme:light;--bg:#fcfcfb;--ink:#0b0b0b;--ink2:#52514e;--ink3:#8a8984;--grid:#e6e5e1;--axis:#b9b8b2;{css_slots_l}}}
@media (prefers-color-scheme:dark){{:root:not([data-theme="light"]){{color-scheme:dark;--bg:#1a1a19;--ink:#f0efec;--ink2:#c3c2b7;
--ink3:#8f8e86;--grid:#2e2e2c;--axis:#55544f;{css_slots_d}}}}}
:root[data-theme="dark"]{{color-scheme:dark;--bg:#1a1a19;--ink:#f0efec;--ink2:#c3c2b7;--ink3:#8f8e86;--grid:#2e2e2c;
--axis:#55544f;{css_slots_d}}}
body{{margin:0;background:var(--bg);color:var(--ink);font:14px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif}}
main{{max-width:900px;margin:0 auto;padding:24px 16px 48px}}
h1{{font-size:22px;margin:0 0 4px}} h2{{font-size:16px;margin:28px 0 4px}} p.sub{{color:var(--ink2);margin:0 0 10px}}
.legend{{display:flex;flex-wrap:wrap;gap:6px 18px;margin:12px 0 4px;color:var(--ink2);font-size:13px}}
.lg{{display:inline-flex;align-items:center;gap:6px}}
svg{{width:100%;height:auto;display:block;overflow:visible}}
.grid{{stroke:var(--grid);stroke-width:1}} .axis{{stroke:var(--axis);stroke-width:1}}
.tick{{fill:var(--ink3);font-size:11px}} .axlabel{{fill:var(--ink2);font-size:12px}}
.note{{fill:var(--ink3);font-size:11px}} .knee{{fill:var(--ink);font-size:12px;font-weight:600}}
.ideal{{stroke:var(--axis);stroke-width:1.5;stroke-dasharray:2 4}} .limit{{stroke:var(--ink3);stroke-width:1;stroke-dasharray:5 4}}
.dot{{fill:var(--c);stroke:var(--bg);stroke-width:2}} .ring{{fill:var(--bg);stroke:var(--c);stroke-width:2.5}}
.bar{{fill:var(--c)}} .hatch{{fill:var(--c);fill-opacity:.55;stroke:var(--c);stroke-width:1.5}}
.barname{{fill:var(--ink);font-size:13px}} .barin{{fill:var(--bg);font-size:12px;font-weight:600}}
.lg svg{{width:34px;height:12px;display:inline-block}} .barval{{fill:var(--ink2);font-size:12px}}
.hit{{fill:transparent;cursor:default}}
#tip{{position:fixed;pointer-events:none;background:var(--ink);color:var(--bg);padding:7px 9px;border-radius:6px;
font-size:12px;white-space:pre;display:none;z-index:9;max-width:90vw}}
table{{border-collapse:collapse;width:100%;font-size:12px;margin-top:8px}} th,td{{text-align:left;padding:4px 8px;
border-bottom:1px solid var(--grid)}} th{{color:var(--ink2);font-weight:600}} td:nth-child(n+3):nth-child(-n+6){{text-align:right}}
details{{margin-top:28px}} summary{{cursor:pointer;color:var(--ink2)}} .key{{color:var(--ink2);font-size:13px}}
</style></head><body><main>
<h1>Where each router stops keeping up</h1>
<p class="sub">BGP rate series, router role. A <b>delivery</b> is one prefix sent by the DUT to one emulated router.
Generated {stamp} from {len(series)} series.</p>
<div class="legend">{legend}</div>
<p class="key">Filled dot = kept up · hollow ring = past the knee · dashed line = the device's own 30 s advertisement
interval (updates batched), solid = no batching. Hover any point for details.</p>
<h2>1 · Delivered vs offered</h2>
<p class="sub">On the dotted diagonal the DUT delivers everything we offer. The knee is where a line leaves it.</p>
{p1.svg("Delivered vs offered deliveries per second")}
<h2>2 · Still incomplete after our last send</h2>
<p class="sub">Seconds until every router had all routes, counted from our last announcement. Above the limit = falling behind.</p>
{p2.svg("Seconds still incomplete after the last send")}
<h2>3 · Highest rate each device kept up with</h2>
<p class="sub">Log scale. Solid bar = no batching; lighter bar = with the device&#39;s own 30 s advertisement interval.</p>
{p3.svg("Highest sustained deliveries per second per device")}
<details><summary>Table of all runs</summary><table><thead><tr><th>device</th><th>MRAI</th><th>rate /router</th>
<th>offered /s</th><th>delivered /s</th><th>after last send s</th><th>verdict</th><th>run</th></tr></thead>
<tbody>{rows_html}</tbody></table></details>
</main><div id="tip"></div>
<script>
const t=document.getElementById('tip');
document.addEventListener('mousemove',e=>{{const h=e.target.closest('[data-tip]');
if(!h){{t.style.display='none';return}}t.textContent=h.dataset.tip;t.style.display='block';
const w=t.offsetWidth,x=e.clientX+14+w>innerWidth?e.clientX-w-14:e.clientX+14;t.style.left=x+'px';t.style.top=(e.clientY+14)+'px'}});
</script></body></html>"""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("dirs", nargs="*", help="series-* or suite-* folders (default: all series in ~/tgen-test-results)")
    ap.add_argument("--all", action="store_true", help="keep every series, not only the newest per device and MRAI")
    ap.add_argument("--out", default=None, help="output file (default ~/tgen-test-results/knee-<time>.html)")
    a = ap.parse_args()
    inv = inventory.load()
    loaded = [s for s in (load_series(d, inv) for d in series_dirs(a.dirs)) if s]
    if not a.all:
        newest: dict = {}
        for s in sorted(loaded, key=lambda s: s["stamp"]):
            newest[(s["device"], s["mrai_label"])] = s
        loaded = list(newest.values())
    if not loaded:
        sys.exit("no usable rate series found")
    order = {d: i for i, d in enumerate(["mikrotik", "asr920", "c1841", "bird"])}
    loaded.sort(key=lambda s: (order.get(s["device"], 9), s["device"], s["mrai"]))
    out = Path(a.out).expanduser() if a.out else RESULTS / time.strftime("knee-%Y%m%d-%H%M%S.html")
    out.write_text(build(loaded))
    for s in loaded:
        sus = f"{s['sustainable']:,.0f}" if s["sustainable"] else "-"
        knee = f"  knee before {s['knee_at']:,.0f} /s" if s["knee_at"] else ""
        print(f"{s['label']:<45} {s['mrai_label'] or 'no MRAI':<28} sustained {sus:>8} /s{knee}  ({s['dir']})")
    print(f"chart: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
