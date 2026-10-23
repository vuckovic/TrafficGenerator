#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Test suite: the same test scheme on several devices, one after another, with
one comparison table at the end.

  tools/tgen_suite.py --duts mikrotik,asr920,c1841
  tools/tgen_suite.py --duts c1841 --steps test,smoke
  tools/tgen_suite.py --duts all --steps test

Steps per device (sizes and rates from its profile in deploy/duts.toml):
  test    SSH login, version, memory, BGP AS, port speed/duplex/errors (changes nothing)
  smoke   one small run at full speed (profile smoke.routers x smoke.prefixes)
  series  rate series up to the knee (profile series.*) - tgen_series.py
  scale   full-speed runs of growing size (profile scale.steps); stops at the first
          failed gate, aborted run or a step above max_routes

The devices run one after another, never at the same time: they share the
cluster's routers, and each result must be the device's alone. A device whose
`test` step fails is skipped. Passwords: TGEN_DUT_PASSWORD_<PROFILE> (e.g.
TGEN_DUT_PASSWORD_C1841) or one prompt per device at the start (never stored).

Results: ~/tgen-test-results/suite-<timestamp>/ (suite.md, suite.json); every
run keeps its own p2-* / series-* folder.
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "tools"))
from tgen import inventory  # noqa: E402

TOOLS = REPO / "tools"
STEPS = ["test", "smoke", "series", "scale"]


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] suite: {msg}", flush=True)


def run_tool(cmd: list[str], env: dict) -> tuple[int, Path | None, str]:
    """Runs a tool, streams its output indented, returns (exit code, results dir, output)."""
    log(" ".join(Path(c).name if i == 1 else c for i, c in enumerate(cmd[1:], 1)))
    out_dir, lines = None, []
    with subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                          stdin=subprocess.DEVNULL, env=env) as p:
        for line in p.stdout:
            sys.stdout.write("    " + line)
            lines.append(line)
            m = re.search(r"results -> (\S+)", line)
            if m and out_dir is None:
                out_dir = Path(m.group(1))
        rc = p.wait()
    return rc, out_dir, "".join(lines)


def p2_row(rc: int, d: Path | None, text: str) -> dict:
    row: dict = {"exit": rc, "dir": str(d) if d else None}
    if not d or not (d / "report.json").exists():
        row["error"] = text.strip().splitlines()[-1][-200:] if text.strip() else f"exit {rc}"
        return row
    rep = json.loads((d / "report.json").read_text())
    do, c = rep.get("dut_output") or {}, rep["convergence"]
    mem = [json.loads(l).get("free_pct") for l in (d / "dut-sessions.jsonl").read_text().splitlines()
           if l.strip()] if (d / "dut-sessions.jsonl").exists() else []
    mem = [m for m in mem if m is not None]
    port = rep.get("dut_port") or {}
    row.update(gate="PASS" if rc == 0 else "FAIL", aborted=rep.get("aborted"),
               sessions=f"{rep['sessions']['established']}/{rep['sessions']['configured']}",
               convergence_s=c.get("initial_s"), prefixes_s=do.get("prefixes_per_s"),
               mbps=do.get("payload_mbps"), link_use_pct=do.get("link_use_pct"),
               p99_ms=rep["latency_announce_ms"].get("p99"), missing=rep["correctness"]["missing_routes"],
               resets=rep["sessions"]["resets"], min_free_mem_pct=min(mem) if mem else None,
               port_errors=port.get("errors_during_run"))
    return row


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--duts", required=True, help="profiles from deploy/duts.toml, comma list, or 'all'")
    ap.add_argument("--steps", default=",".join(STEPS), help=f"comma list of {STEPS}")
    ap.add_argument("--pause", type=float, default=30, help="seconds between runs (lets the DUT settle)")
    ap.add_argument("--no-build", action="store_true", help="do not rebuild the agent before the first run")
    ap.add_argument("--config", default=str(REPO / "deploy" / "cluster.env"))
    ap.add_argument("--extra", default="", help=argparse.SUPPRESS)   # more tgen_p2 options (simulation)
    a = ap.parse_args()
    inv = inventory.load()
    names = list(inv) if a.duts == "all" else [x.strip() for x in a.duts.split(",") if x.strip()]
    unknown = [n for n in names if n not in inv]
    if unknown:
        sys.exit(f"unknown profile(s) {unknown}; in {inventory.path().name}: {', '.join(inv)}")
    steps = [x.strip() for x in a.steps.split(",") if x.strip()]
    if bad := [x for x in steps if x not in STEPS]:
        sys.exit(f"unknown step(s) {bad}; choose from {STEPS}")

    # one password per device up front, so the suite can run unattended afterwards
    pw: dict[str, str | None] = {}
    for n in names:
        pw[n] = os.environ.get(inventory.password_env(n)) or os.environ.get("TGEN_DUT_PASSWORD")
        if pw[n] is None and os.isatty(0):
            d = inv[n]
            pw[n] = getpass.getpass(f"password for {d['user']}@{d['ssh_host']} ({d['label']}; not stored; "
                                    "empty = SSH key): ") or None

    out = Path.home() / "tgen-test-results" / time.strftime("suite-%Y%m%d-%H%M%S")
    out.mkdir(parents=True)
    log(f"devices {names}, steps {steps}; results -> {out}")
    built = a.no_build
    common = ["--config", a.config, *a.extra.split()]
    suite: dict[str, dict] = {}

    def save() -> None:
        (out / "suite.json").write_text(json.dumps(suite, indent=1, default=str))

    for n in names:
        d = inv[n]
        env = dict(os.environ)
        env.pop("TGEN_DUT_PASSWORD", None)
        if pw[n]:
            env["TGEN_DUT_PASSWORD"] = pw[n]
        res: dict = suite.setdefault(n, {"label": d["label"], "peer_ip": d["peer_ip"], "link_mbps": d["link_mbps"]})
        log(f"=== {n}: {d['label']} at {d['peer_ip']} ===")

        if "test" in steps:
            rc, _, text = run_tool([sys.executable, str(TOOLS / "tgen_dut.py"), "--dut-profile", n, "--json",
                                    "--config", a.config, "test"], env)
            try:
                t = json.JSONDecoder().raw_decode(text[text.index("{"):])[0]
            except ValueError:
                t = {"ok": False, "error": text.strip()[-300:]}
            res["test"] = t
            save()
            if not t.get("ok"):
                log(f"{n}: test failed ({t.get('error')}); skipping this device")
                continue

        p2 = [sys.executable, str(TOOLS / "tgen_p2.py"), "--dut-profile", n, *common]
        if "smoke" in steps:
            sm = d.get("smoke", {"routers": 12, "prefixes": 1000})
            rc, rd, text = run_tool(p2 + ["--routers", str(sm["routers"]), "--prefixes", str(sm["prefixes"]),
                                          *(["--no-build"] if built else [])], env)
            built = True
            res["smoke"] = {"size": f"{sm['routers']} x {sm['prefixes']}",
                            "deliveries": sm["routers"] * sm["prefixes"] * (sm["routers"] - 1), **p2_row(rc, rd, text)}
            save()
            time.sleep(a.pause)

        if "series" in steps:
            rc, rd, text = run_tool([sys.executable, str(TOOLS / "tgen_series.py"), "--pause", str(a.pause), "--",
                                     "--dut-profile", n, *common, *(["--no-build"] if built else [])], env)
            built = True
            row: dict = {"exit": rc, "dir": str(rd) if rd else None}
            if rd and (rd / "series.json").exists():
                rows = [x for x in json.loads((rd / "series.json").read_text()) if "error" not in x]
                ok = [x for x in rows if not x.get("past_knee")]
                over = [x for x in rows if x.get("past_knee")]
                row.update(sustainable_rate=max((x["rate"] for x in ok), default=None),
                           sustainable_deliveries_s=max((x["offered_deliveries_s"] for x in ok), default=None),
                           knee_rate=over[0]["rate"] if over else None,
                           max_output_s=max((x["dut_output_s"] or 0 for x in rows), default=None))
            res["series"] = row
            save()
            time.sleep(a.pause)

        if "scale" in steps:
            res["scale"] = []
            # the DUT's measured delivery rate (smoke, or the previous step) sets each step's timeout:
            # a slow DUT that is still working must not fail on a fixed 300 s timeout
            sm = res.get("smoke", {})
            rate = (sm["deliveries"] / sm["convergence_s"]) if sm.get("convergence_s") else None
            mrai = inventory.effective_mrai(d)
            for routers, prefixes in d.get("scale", {}).get("steps", []):
                if d.get("max_routes") and routers * prefixes > d["max_routes"]:
                    log(f"{n}: scale step {routers} x {prefixes} is above max_routes {d['max_routes']}; stopping")
                    break
                deliveries = routers * prefixes * (routers - 1)
                conv = max(300.0, deliveries / rate * 1.5 + 60 + mrai) if rate else 300.0
                log(f"{n}: scale {routers} x {prefixes} = {deliveries:,} deliveries; convergence timeout {conv:.0f} s"
                    + (f" (measured {rate:,.0f} deliveries/s)" if rate else ""))
                rc, rd, text = run_tool(p2 + ["--routers", str(routers), "--prefixes", str(prefixes),
                                              "--converge-timeout", f"{conv:.0f}",
                                              "--withdraw-timeout", f"{max(120.0, conv / 2):.0f}",
                                              *(["--no-build"] if built else [])], env)
                built = True
                row = {"size": f"{routers} x {prefixes}", "routes": routers * prefixes, "deliveries": deliveries,
                       **p2_row(rc, rd, text)}
                if row.get("convergence_s"):
                    rate = deliveries / row["convergence_s"]
                res["scale"].append(row)
                save()
                if row.get("gate") != "PASS":
                    log(f"{n}: scale step {routers} x {prefixes} did not pass; stopping the ladder")
                    break
                time.sleep(a.pause)

    # ----------------------------------------------------------------- report
    f = lambda v, dg=0: "-" if v is None else (f"{v:,.{dg}f}" if isinstance(v, (int, float)) else str(v))  # noqa: E731
    md = [f"# Test suite {out.name}", "",
          "| device | port | version | smoke | smoke conv. s | smoke output prefixes/s | Mb/s (port use) "
          "| sustainable rate /router | knee /router | sustainable deliveries/s | largest scale passed | min free mem % "
          "| port errors |",
          "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for n, r in suite.items():
        t = r.get("test", {})
        port = t.get("port") or {}
        sm, se, sc = r.get("smoke", {}), r.get("series", {}), r.get("scale", [])
        passed = [x for x in sc if x.get("gate") == "PASS"]
        mems = [x.get("min_free_mem_pct") for x in [sm, *sc] if x.get("min_free_mem_pct") is not None]
        errs = {}
        for x in [sm, *sc]:
            for k, v in (x.get("port_errors") or {}).items():
                errs[k] = errs.get(k, 0) + v
        rs = (t.get("resources") or {}) if t else {}
        version = (" ".join(x for x in (rs.get("model") or rs.get("board"), rs.get("train"), rs.get("version")) if x)
                   or (t.get("error", "-")[:60] if t else "-"))
        md.append(" | ".join([
            f"| {n}", f"{port.get('speed_mbps') or r['link_mbps']} Mb/s {port.get('duplex') or ''}".strip(), version,
            f"{sm.get('size', '-')} {sm.get('gate') or sm.get('error', '') if sm else ''}".strip(),
            f(sm.get("convergence_s"), 1), f(sm.get("prefixes_s")),
            (f"{sm['mbps']:.1f} Mb/s" if sm["mbps"] >= 1 else f"{sm['mbps'] * 1000:.0f} kb/s")
            + f" ({f(sm.get('link_use_pct'), 2)} %)" if sm.get("mbps") is not None else "-",
            f(se.get("sustainable_rate"), 1), f(se.get("knee_rate"), 1), f(se.get("sustainable_deliveries_s")),
            passed[-1]["size"] if passed else "-", f(min(mems), 0) if mems else "-",
            ", ".join(f"{k} +{v}" for k, v in errs.items()) or ("none" if sm or sc else "-")]) + " |")
    md += ["", "Runs: " + ", ".join(f"{n}: " + ", ".join(Path(x["dir"]).name for x in
                                                          [r.get("smoke", {}), r.get("series", {}), *r.get("scale", [])]
                                                          if x.get("dir")) for n, r in suite.items())]
    (out / "suite.md").write_text("\n".join(md) + "\n")
    save()
    print("\n" + "\n".join(md) + f"\n\nfiles: {out}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit("suite interrupted")
