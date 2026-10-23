#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Rate series: run tgen_p2.py once per paced announce rate and compare the runs
in one table, with an estimate of the DUT's knee.

  tools/tgen_series.py --rates 2,5,8,10,15 -- --dut mikrotik --routers 12 --prefixes 100
  tools/tgen_series.py -- --dut-profile c1841     # rates, routers, prefixes from deploy/duts.toml

Everything after `--` is passed to every tgen_p2.py run (--rate is set per run).
Per run: offered rate, prefixes per UPDATE, DUT output, convergence (from the first
announcement and after our last send), announce latency p50/p99, our send lag, DUT
CPU mid-run, gate. Knee = the first rate where the DUT falls behind: it delivers less
than --knee-ratio of what is offered, or still needs more than --knee-backlog-s after
our last send. Latency above --slo-p99-ms is reported (column "latency OK") but does
not stop the series: a high but stable latency is not a knee. Withdrawals are paced
like announcements, so --withdraw-timeout is raised when a rate needs more.

Results: ~/tgen-test-results/series-<timestamp>/ (series.md, series.csv,
series.json); each run keeps its own p2-* folder.
"""
from __future__ import annotations

import argparse
import csv
import getpass
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
P2 = REPO / "tools" / "tgen_p2.py"
sys.path.insert(0, str(REPO / "tools"))
from tgen import inventory  # noqa: E402


def arg_value(args: list[str], name: str, default: str | None = None) -> str | None:
    for i, a in enumerate(args):
        if a == name and i + 1 < len(args):
            return args[i + 1]
        if a.startswith(name + "="):
            return a.split("=", 1)[1]
    return default


def run_one(rate: float, passthrough: list[str], converge: float, log) -> dict:
    cmd = [sys.executable, str(P2), *passthrough, "--rate", str(rate), "--converge-timeout", f"{converge:.0f}"]
    log(f"rate {rate:g}/s per router: {' '.join(cmd[2:])}")
    out_dir, lines = None, []
    with subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                          stdin=subprocess.DEVNULL) as p:
        for line in p.stdout:
            sys.stdout.write("    " + line)
            lines.append(line)
            m = re.search(r"results -> (\S+)", line)
            if m:
                out_dir = Path(m.group(1))
        rc = p.wait()
    row = {"rate": rate, "exit": rc, "dir": str(out_dir) if out_dir else None}
    if not out_dir or not (out_dir / "report.json").exists():
        row["error"] = "".join(lines[-3:]).strip()[-300:] or f"exit {rc}"
        return row
    rep = json.loads((out_dir / "report.json").read_text())
    meta = json.loads((out_dir / "meta.json").read_text())
    n = len(meta["routers"])
    la, c = rep["latency_announce_ms"], rep["convergence"]
    mid = out_dir / "dut-resources-mid.json"
    plans = sorted(out_dir.glob("plan-*.json"))
    per_update = json.loads(plans[0].read_text()).get("per_update") if plans else None
    row.update(
        routers=n,
        offered_prefixes_s=rate * n, per_update=per_update or "max",
        offered_deliveries_s=rate * n * (n - 1),         # every prefix goes to every other router
        dut_output_s=(rep.get("dut_output") or {}).get("prefixes_per_s"),
        convergence_s=c.get("initial_s"), after_last_send_s=c.get("after_last_send_s"),
        p50_ms=la.get("p50"), p99_ms=la.get("p99"), n_samples=la.get("n"),
        send_lag_p99_ms=rep["generator"].get("send_lag_p99_ms"),
        dut_cpu_mid=json.loads(mid.read_text()).get("cpu_load") if mid.exists() else None,
        missing=rep["correctness"]["missing_routes"], resets=rep["sessions"]["resets"],
        gate="PASS" if rc == 0 else "FAIL")
    return row


def main() -> int:
    argv = sys.argv[1:]
    passthrough = argv[argv.index("--") + 1:] if "--" in argv else []
    own = argv[:argv.index("--")] if "--" in argv else argv
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--rates", default=None,
                    help="prefixes/s per router, comma list (default: the profile's series.rates, else 2,5,8,10,15)")
    ap.add_argument("--knee-ratio", type=float, default=0.9, help="delivered/offered below this = past the knee")
    ap.add_argument("--knee-backlog-s", type=float, default=None,
                    help="receivers still incomplete this long after our last send = past the knee "
                         "(default 5 s plus the DUT's advertisement interval, MRAI, from its profile)")
    ap.add_argument("--slo-p99-ms", type=float, default=1000, help="latency target, reported only")
    ap.add_argument("--stop-after-knee", type=int, default=1, help="runs to do past the knee before stopping")
    ap.add_argument("--pause", type=float, default=0, help="seconds between runs")
    a = ap.parse_args(own)
    prof_name = arg_value(passthrough, "--dut-profile")
    prof = inventory.load().get(prof_name) if prof_name else None
    if prof_name and not prof:
        sys.exit(f"unknown --dut-profile {prof_name}")
    ser = (prof or {}).get("series", {})
    for opt, key in (("--routers", "routers"), ("--prefixes", "prefixes")):
        if key in ser and arg_value(passthrough, opt) is None:
            passthrough += [opt, str(ser[key])]
    rates = [float(x) for x in a.rates.split(",") if x.strip()] if a.rates else \
        [float(x) for x in ser.get("rates", [2, 5, 8, 10, 15])]
    # a DUT that batches updates for MRAI seconds is not "behind" during that time
    mrai_arg = arg_value(passthrough, "--dut-mrai")
    mrai = inventory.effective_mrai(prof, float(mrai_arg) if mrai_arg is not None else None)
    if a.knee_backlog_s is None:
        a.knee_backlog_s = 5 + mrai
    if any(x in passthrough for x in ("--rate",)):
        sys.exit("do not pass --rate after --; use --rates")

    # one password prompt for the whole series (never stored; passed via the environment)
    dut = arg_value(passthrough, "--dut", prof["adapter"] if prof else "bird")
    if prof and os.environ.get(inventory.password_env(prof_name)):
        os.environ["TGEN_DUT_PASSWORD"] = os.environ[inventory.password_env(prof_name)]
    if dut != "bird" and not os.environ.get("TGEN_DUT_PASSWORD") and not arg_value(passthrough, "--dut-key") \
            and os.isatty(0):
        pw = getpass.getpass("DUT password for the whole series (not stored; empty = SSH key): ")
        if pw:
            os.environ["TGEN_DUT_PASSWORD"] = pw

    prefixes = float(arg_value(passthrough, "--prefixes", "10000"))
    user_converge = arg_value(passthrough, "--converge-timeout")
    user_withdraw = arg_value(passthrough, "--withdraw-timeout")

    def drop(args: list[str], name: str) -> list[str]:
        return [x for i, x in enumerate(args)
                if not (x == name or (i > 0 and args[i - 1] == name) or x.startswith(name + "="))]
    passthrough = drop(drop(passthrough, "--converge-timeout"), "--withdraw-timeout")
    out = Path.home() / "tgen-test-results" / time.strftime("series-%Y%m%d-%H%M%S")
    out.mkdir(parents=True)

    def log(msg: str) -> None:
        print(f"[{time.strftime('%H:%M:%S')}] series: {msg}", flush=True)

    log(f"rates {rates} prefixes/s per router; knee backlog {a.knee_backlog_s:g} s"
        + (f" (5 s + MRAI {mrai:g} s)" if mrai else "") + f"; results -> {out}")
    rows, past_knee, knee = [], 0, None
    for i, r in enumerate(rates):
        # paced: sending takes prefixes/rate seconds; leave room for the DUT to catch up
        converge = float(user_converge) if user_converge else prefixes / r * 1.5 + 60 + mrai
        need_wd = prefixes / r * 1.3 + 20                  # withdrawals are paced too
        withdraw = max(float(user_withdraw), need_wd) if user_withdraw else need_wd
        if user_withdraw and withdraw > float(user_withdraw):
            log(f"withdraw timeout raised to {withdraw:.0f} s for {r:g}/s (withdrawing takes {prefixes / r:.0f} s)")
        row = run_one(r, passthrough + ["--withdraw-timeout", f"{withdraw:.0f}"], converge, log)
        rows.append(row)
        if "error" not in row:
            behind = row["dut_output_s"] is not None and row["dut_output_s"] < a.knee_ratio * row["offered_deliveries_s"]
            backlog = row["after_last_send_s"] is None or row["after_last_send_s"] > a.knee_backlog_s
            over = behind or backlog
            row["past_knee"] = over
            row["latency_ok"] = (row["p99_ms"] or 0) <= a.slo_p99_ms
            if over and knee is None:
                knee = r
            past_knee += 1 if over else 0
        (out / "series.json").write_text(json.dumps(rows, indent=1))
        if past_knee >= a.stop_after_knee and i < len(rates) - 1:
            log(f"past the knee at {r:g}/s; stopping the series (--stop-after-knee {a.stop_after_knee})")
            break
        if a.pause and i < len(rates) - 1:
            time.sleep(a.pause)

    ok = [x for x in rows if "error" not in x]
    sustainable = max((x["rate"] for x in ok if not x["past_knee"]), default=None)
    cols = ["rate", "per_update", "offered_prefixes_s", "offered_deliveries_s", "dut_output_s", "convergence_s", "after_last_send_s",
            "p50_ms", "p99_ms",
            "send_lag_p99_ms", "dut_cpu_mid", "missing", "resets", "gate", "latency_ok", "past_knee", "dir"]
    with open(out / "series.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    fmt = lambda v, d=0: "-" if v is None else (f"{v:,.{d}f}" if isinstance(v, (int, float)) else str(v))  # noqa: E731
    md = ["| rate /router | prefixes /UPDATE | offered prefixes/s | offered deliveries/s | DUT output /s "
          "| convergence s | after last send s | p50 ms | p99 ms | send lag p99 ms | DUT CPU | missing | resets "
          "| gate | latency OK | past knee |",
          "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for x in rows:
        if "error" in x:
            md.append(f"| {x['rate']:g} | run failed: {x['error'][:80]} |" + " |" * 14)
            continue
        md.append(f"| {x['rate']:g} | {x['per_update']} | {fmt(x['offered_prefixes_s'])} | {fmt(x['offered_deliveries_s'])} | "
                  f"{fmt(x['dut_output_s'])} | {fmt(x['convergence_s'], 1)} | {fmt(x['after_last_send_s'], 1)} | {fmt(x['p50_ms'])} | {fmt(x['p99_ms'])} | "
                  f"{fmt(x['send_lag_p99_ms'], 2)} | {x['dut_cpu_mid'] or '-'} | {x['missing']} | {x['resets']} | "
                  f"{x['gate']} | {'yes' if x['latency_ok'] else 'no'} | {'yes' if x['past_knee'] else 'no'} |")
    verdict = (f"Highest sustainable rate in this series: {sustainable:g} prefixes/s per router "
               f"({sustainable * ok[0]['routers']:g} offered prefixes/s). " if sustainable else
               "No rate in this series was sustainable. ") + \
        (f"Knee between {sustainable:g} and {knee:g} /s per router." if knee and sustainable else
         f"Knee at or below {knee:g} /s per router." if knee else "No knee reached: try higher rates.")
    head = (f"# Rate series {out.name}\n\nPassed to every run: `{' '.join(passthrough)}`\n\n"
            f"Knee rule: DUT output < {a.knee_ratio:g} × offered deliveries, or receivers still incomplete "
            f"{a.knee_backlog_s:g} s after our last send. Latency OK = announce p99 <= {a.slo_p99_ms:g} ms.\n\n")
    (out / "series.md").write_text(head + "\n".join(md) + f"\n\n{verdict}\n")
    print("\n" + "\n".join(md) + f"\n\n{verdict}\nfiles: {out}")
    return 0 if ok else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit("series interrupted")
