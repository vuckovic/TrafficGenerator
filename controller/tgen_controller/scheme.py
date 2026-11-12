# SPDX-License-Identifier: MIT
"""Test scheme runner (P3c-2, docs/09 §9): the standard comparison test of the devices in
deploy/duts.toml, as tools/tgen_suite.py does it, but as ordinary controller runs (each one
is in History with its own report). One device after another, one run at a time:

  1 connection test  2 smoke  3 rate series (per MRAI variant, up to the knee)  4 scale ladder

The knee rule is tools/tgen/knee.py. Passwords are kept in memory for the scheme only.
"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

from . import REPO
from .scenario import Dut, Scenario
from .store import now_ms

STEPS = ("test", "smoke", "series", "scale")
COLS = ("conn", "smoke", "series_device", "series_zero", "scale")      # K9 progress columns
ACTIVE = ("running",)


class SchemeError(RuntimeError):
    pass


def _f(v):
    return None if v is None else round(float(v), 2)


def run_row(rdir: Path, run: dict) -> dict:
    """Key figures of one finished run, from its report (as tgen_suite.p2_row / tgen_series.run_one)."""
    row: dict = {"run": run["id"], "state": run["state"], "error": run.get("error")}
    f = rdir / "report.json"
    if not f.exists():
        return row
    rep = json.loads(f.read_text())
    do, c, la = rep.get("dut_output") or {}, rep["convergence"], rep["latency_announce_ms"]
    mem = []
    js = rdir / "dut-sessions.jsonl"
    if js.exists():
        mem = [x.get("free_pct") for x in map(json.loads, filter(str.strip, js.read_text().splitlines()))]
    mem = [m for m in mem if m is not None]
    summ = run.get("summary") or {}
    row.update(gate_ok=summ.get("gate_ok"), sessions=f"{rep['sessions']['established']}/{rep['sessions']['configured']}",
               convergence_s=_f(c.get("initial_s")), after_last_send_s=_f(c.get("after_last_send_s")),
               send_window_s=_f(c.get("send_window_s")), output_s=_f(do.get("prefixes_per_s")),
               mbps=_f(do.get("payload_mbps")), link_use_pct=_f(do.get("link_use_pct")),
               p50_ms=la.get("p50"), p99_ms=la.get("p99"), missing=rep["correctness"]["missing_routes"],
               resets=rep["sessions"]["resets"], min_free_mem_pct=min(mem) if mem else None,
               port_errors=((run.get("dut") or {}).get("port_errors")))
    return row


class SchemeRunner:
    def __init__(self, engine, store, hub, cfg):
        self.engine, self.store, self.hub, self.cfg = engine, store, hub, cfg
        self.doc: dict | None = None
        self.task: asyncio.Task | None = None
        self.secrets: dict = {}
        self.stop_flag = asyncio.Event()

    def active(self) -> bool:
        return bool(self.task and not self.task.done())

    # ------------------------------------------------------------------ plan / estimate
    @staticmethod
    def plan(inv: dict, devices: list[str], steps: list[str], pause_s: float) -> dict:
        """Per device: the step sizes from the file and a rough duration (K3, K4, K7)."""
        from tgen import inventory
        out = {}
        for n in devices:
            p = inv[n]
            sm = p.get("smoke", {"routers": 12, "prefixes": 1000})
            ser = p.get("series", {})
            variants = ser.get("mrai", ["default"])
            rates = [float(x) for x in ser.get("rates", [2, 5, 8, 10, 15])]
            sc = [s for s in p.get("scale", {}).get("steps", [])
                  if not p.get("max_routes") or s[0] * s[1] <= p["max_routes"]]
            est = {"test": 15 if "test" in steps else 0, "series": 0, "scale": 0,
                   "smoke": 60 + sm["routers"] * sm["prefixes"] * (sm["routers"] - 1) / 3000 if "smoke" in steps else 0}
            if "series" in steps:
                for v in variants:
                    mrai = inventory.effective_mrai(p, 0 if v != "default" else None)
                    est["series"] += sum(ser.get("prefixes", 1000) / r * 2.3 + 40 + mrai + pause_s for r in rates)
            if "scale" in steps:
                est["scale"] = sum(120 + s[0] * s[1] * (s[0] - 1) / 3000 + pause_s for s in sc)
            out[n] = {"label": p["label"], "smoke": sm, "series": {"routers": ser.get("routers", 12),
                      "prefixes": ser.get("prefixes", 100), "rates": rates,
                      "mrai": ["default" if v == "default" else float(v) for v in variants]},
                      "device_mrai_s": p.get("device_mrai_s"), "scale": sc, "estimate_s": est,
                      "estimate_total_s": round(sum(est.values()) + pause_s * 2)}
        return out

    # ------------------------------------------------------------------ public
    async def start(self, devices: list, steps: list[str], pause_s: float, stop_on_failure: bool) -> str:
        from tgen import inventory
        if self.active():
            raise SchemeError(f"scheme {self.doc['id']} is running")
        if self.engine.task and not self.engine.task.done():
            raise SchemeError(f"run {self.engine.ctx.id} is active")
        inv = inventory.load()
        names = [d["profile"] for d in devices]
        if not names or any(n not in inv for n in names):
            raise SchemeError("unknown or no devices: " + ", ".join(n for n in names if n not in inv))
        steps = [s for s in STEPS if s in steps]
        if not steps:
            raise SchemeError("no steps")
        sid = time.strftime("s-%Y%m%d-%H%M%S")
        plan = self.plan(inv, names, steps, pause_s)
        self.secrets = {d["profile"]: (d.get("password"), d.get("enable")) for d in devices}
        prog = {}
        for n in names:
            variants = plan[n]["series"]["mrai"]
            prog[n] = {c: {"state": "waiting", "detail": ""} for c in COLS}
            if "test" not in steps:
                prog[n]["conn"]["state"] = "na"
            if "smoke" not in steps:
                prog[n]["smoke"]["state"] = "na"
            if "series" not in steps:
                prog[n]["series_device"]["state"] = prog[n]["series_zero"]["state"] = "na"
            elif 0.0 not in variants:
                prog[n]["series_zero"] = {"state": "na", "detail": "no MRAI setting" if inv[n]["adapter"] == "mikrotik" else "",
                                          "info": {"k": "routeros"} if inv[n]["adapter"] == "mikrotik" else None}
            if "scale" not in steps:
                prog[n]["scale"]["state"] = "na"
        self.doc = {"id": sid, "state": "running", "created_ms": now_ms(), "ended_ms": None, "devices": names,
                    "steps": steps, "pause_s": pause_s, "stop_on_failure": stop_on_failure, "plan": plan,
                    "progress": prog, "current": None, "results": {n: {"label": inv[n]["label"], "adapter": inv[n]["adapter"]} for n in names},
                    "runs": [], "error": None}
        self.stop_flag = asyncio.Event()
        self.engine.owner = sid
        self._save()
        self.task = asyncio.create_task(self._main())
        return sid

    async def stop(self) -> bool:
        if not self.active():
            return False
        self.stop_flag.set()
        e = self.engine
        if e.ctx and e.task and not e.task.done():
            await e.stop(e.ctx.id, "test scheme stopped by the user")
        return True

    # ------------------------------------------------------------------ helpers
    def _save(self) -> None:
        d = self.doc
        d["compare"] = self._compare()
        d["knee"] = self._knee()
        d["remaining_s"] = self._remaining()
        self.store.save_scheme(d["id"], d["state"], d["created_ms"], d)
        self.hub.publish({"type": "scheme", "scheme": d})

    def _cell(self, dev: str, col: str, state: str, detail: str = "", run: str | None = None,
              info: dict | None = None) -> None:
        """detail: English text (CLI, logs); info: the same as data, translated by the web page."""
        c = self.doc["progress"][dev][col]
        c.update(state=state, detail=detail, info=info)
        if run:
            c["run"] = run
        if state == "running":
            self.doc["current"] = {"device": dev, "col": col, "run": run, "detail": detail, "since_ms": now_ms()}
        self._save()

    def _remaining(self) -> int:
        d = self.doc
        if d["state"] != "running":
            return 0
        tot = 0
        colstep = {"conn": "test", "smoke": "smoke", "series_device": "series", "series_zero": "series", "scale": "scale"}
        for n in d["devices"]:
            est = d["plan"][n]["estimate_s"]
            nvar = max(1, len(d["plan"][n]["series"]["mrai"]))
            for col, c in d["progress"][n].items():
                if c["state"] in ("waiting", "running"):
                    s = est[colstep[col]] / (nvar if colstep[col] == "series" else 1)
                    tot += s / 2 if c["state"] == "running" else s
        return round(tot)

    async def _sleep(self, s: float) -> None:
        try:
            await asyncio.wait_for(self.stop_flag.wait(), timeout=max(0.0, s))
        except asyncio.TimeoutError:
            pass

    def _dut(self, dev: str, **kw) -> Dut:
        pw, en = self.secrets.get(dev, (None, None))
        return Dut(profile=dev, password=pw, enable=en, poll_s=10, **kw)

    async def _run(self, dev: str, col: str, name: str, routers: int, prefixes: int, rate: float,
                   conv: float, wd: float, mrai: float | None = None, detail: str = "", info: dict | None = None) -> dict:
        """One controller run (establish -> announce at `rate` -> withdraw), as a tgen_p2.py run."""
        sc = Scenario.model_validate({
            "name": f"{self.doc['id']} {dev} {name}",
            "dut": self._dut(dev, mrai=mrai),
            "routers": {"total": routers, "prefixes": prefixes},
            "phases": [{"type": "establish", "timeout_s": 90}, {"type": "announce", "rate": rate, "timeout_s": conv},
                       {"type": "withdraw", "rate": rate, "timeout_s": wd}],
            "limits": {"max_duration_s": min(self.cfg.max_duration_s, conv + wd + 300)}})
        rid = await self.engine.start(sc, owner=self.doc["id"])
        self.doc["runs"].append(rid)
        self._cell(dev, col, "running", detail, run=rid, info=info)
        task = self.engine.task
        await asyncio.shield(task)
        run = self.store.run(rid)
        return run_row(Path(run["dir"]), run)

    # ------------------------------------------------------------------ the scheme
    async def _main(self) -> None:
        d = self.doc
        try:
            for i, dev in enumerate(d["devices"]):
                if self.stop_flag.is_set():
                    break
                ok = await self._device(dev)
                if not ok and self.stop_flag.is_set():
                    break
                if i < len(d["devices"]) - 1:
                    await self._sleep(d["pause_s"])
            d["state"] = "stopped" if self.stop_flag.is_set() else "done"
        except Exception as e:  # noqa: BLE001
            d["state"], d["error"] = "failed", f"{e.__class__.__name__}: {e}"
        finally:
            for prog in d["progress"].values():
                for c in prog.values():
                    if c["state"] in ("waiting", "running"):
                        c["state"] = "skipped" if c["state"] == "waiting" else ("failed" if d["state"] == "failed" else "skipped")
            d["current"], d["ended_ms"] = None, now_ms()
            self.engine.owner = None
            self.secrets = {}
            self._save()

    async def _device(self, dev: str) -> bool:
        from tgen import inventory
        from tgen.knee import past_knee
        from .dutspec import action

        d, res, p = self.doc, self.doc["results"][dev], self.doc["plan"][dev]
        prof = inventory.load()[dev]
        steps, stop_fail = d["steps"], d["stop_on_failure"]

        def skip_rest(reason: str, k: str) -> None:
            for c in d["progress"][dev].values():
                if c["state"] == "waiting":
                    c.update(state="skipped", detail=reason, info={"k": k})
            self._save()

        # 1 connection test
        if "test" in steps:
            self._cell(dev, "conn", "running")
            pw, en = self.secrets.get(dev, (None, None))
            try:
                t = await asyncio.to_thread(action, Dut(profile=dev, password=pw, enable=en), "test", 6,
                                            str(self.cfg.raw.get("cluster_env") or REPO / "deploy" / "cluster.env"))
            except Exception as e:  # noqa: BLE001
                t = {"ok": False, "error": f"{e.__class__.__name__}: {e}"}
            res["test"] = {k: v for k, v in t.items() if k != "notes"}
            if t.get("ok"):
                rs = t.get("resources") or {}
                v = (rs.get("summary") or "").split(",")[0]
                self._cell(dev, "conn", "passed", v, info={"k": "text", "v": v})
            else:
                v = (t.get("error") or "")[:160]
                self._cell(dev, "conn", "failed", v, info={"k": "text", "v": v})
                skip_rest("connection test failed", "skip_conn")
                return False
        if self.stop_flag.is_set():
            return False

        rate_est = None    # measured deliveries/s, sets the scale timeouts (as tgen_suite.py)
        # 2 smoke
        if "smoke" in steps:
            sm = p["smoke"]
            row = await self._run(dev, "smoke", "smoke", sm["routers"], sm["prefixes"], 0, 300, 120,
                                  detail=f"{sm['routers']} × {sm['prefixes']}",
                                  info={"k": "size", "r": sm["routers"], "p": sm["prefixes"]})
            row["size"] = f"{sm['routers']} × {sm['prefixes']}"
            row["deliveries"] = sm["routers"] * sm["prefixes"] * (sm["routers"] - 1)
            res["smoke"] = row
            good = row.get("state") == "done" and row.get("gate_ok") and not row.get("missing")
            if row.get("convergence_s"):
                rate_est = row["deliveries"] / row["convergence_s"]
            if self.stop_flag.is_set():
                self._cell(dev, "smoke", "skipped", "stopped", info={"k": "stopped"})
                return False
            if row.get("sessions"):
                self._cell(dev, "smoke", "passed" if good else "failed",
                           f"{row.get('convergence_s') or '—'} s · {row.get('missing', '—')} missing",
                           info={"k": "missing", "s": row.get("convergence_s"), "m": row.get("missing")})
            else:
                v = (row.get("error") or row.get("state") or "")[:160]
                self._cell(dev, "smoke", "failed", v, info={"k": "text", "v": v})
            if not good and stop_fail:
                skip_rest("smoke failed", "skip_smoke")
                return False
            await self._sleep(d["pause_s"])
        if self.stop_flag.is_set():
            return False

        # 3 rate series, one per MRAI variant
        if "series" in steps:
            ser = p["series"]
            res["series"] = {}
            for v in ser["mrai"]:
                col = "series_device" if v == "default" else "series_zero"
                mrai = inventory.effective_mrai(prof, None if v == "default" else v)
                key = "default" if v == "default" else f"{v:g}"
                out = res["series"][key] = {"mrai": "default" if v == "default" else v, "mrai_s": mrai, "rows": []}
                knee = None
                for j, r in enumerate(ser["rates"]):
                    if self.stop_flag.is_set():
                        break
                    conv = ser["prefixes"] / r * 1.5 + 60 + mrai
                    wd = ser["prefixes"] / r * 1.3 + 20
                    row = await self._run(dev, col, f"series {key} {r:g}/s", ser["routers"], ser["prefixes"], r, conv, wd,
                                          mrai=None if v == "default" else v,
                                          detail=f"{r:g} /s ({j + 1}/{len(ser['rates'])})",
                                          info={"k": "rate", "r": r, "i": j + 1, "n": len(ser["rates"])})
                    n = ser["routers"]
                    row.update(rate=r, offered_deliveries_s=r * n * (n - 1), offered_prefixes_s=r * n)
                    if row.get("sessions"):
                        k = past_knee(row["offered_deliveries_s"], row.get("output_s"), row.get("after_last_send_s"),
                                      row.get("send_window_s"), mrai)
                        row["past_knee"] = k["past_knee"]
                        if k["past_knee"] and knee is None:
                            knee = r
                    out["rows"].append(row)
                    self._save()
                    if knee is not None:
                        break
                    await self._sleep(d["pause_s"])
                rows = [x for x in out["rows"] if "past_knee" in x]
                ok = [x for x in rows if not x["past_knee"]]
                out.update(sustainable_rate=max((x["rate"] for x in ok), default=None),
                           sustainable_deliveries_s=max((x["offered_deliveries_s"] for x in ok), default=None),
                           knee_rate=knee, max_output_s=max((x.get("output_s") or 0 for x in rows), default=None),
                           top_rate=ser["rates"][-1])
                if self.stop_flag.is_set():
                    self._cell(dev, col, "skipped", "stopped", info={"k": "stopped"})
                    return False
                if not rows:
                    self._cell(dev, col, "failed", "no run completed", info={"k": "no_run"})
                elif knee is not None:
                    self._cell(dev, col, "passed", f"knee at {knee:g} /s", info={"k": "knee", "r": knee})
                else:
                    self._cell(dev, col, "passed", f"no knee up to {ser['rates'][-1]:g} /s",
                               info={"k": "noknee", "r": ser["rates"][-1]})
        if self.stop_flag.is_set():
            return False

        # 4 scale ladder (stops at the first step that does not pass)
        if "scale" in steps:
            res["scale"] = []
            mrai = inventory.effective_mrai(prof)
            done = None
            for routers, prefixes in p["scale"]:
                if self.stop_flag.is_set():
                    break
                deliveries = routers * prefixes * (routers - 1)
                conv = max(300.0, deliveries / rate_est * 1.5 + 60 + mrai) if rate_est else 300.0
                row = await self._run(dev, "scale", f"scale {routers}x{prefixes}", routers, prefixes, 0, conv,
                                      max(120.0, conv / 2), detail=f"{routers} × {prefixes}",
                                      info={"k": "size", "r": routers, "p": prefixes})
                row.update(size=f"{routers} × {prefixes}", routes=routers * prefixes, deliveries=deliveries)
                res["scale"].append(row)
                if row.get("convergence_s"):
                    rate_est = deliveries / row["convergence_s"]
                if not (row.get("state") == "done" and row.get("gate_ok")):
                    break
                done = row["size"]
                await self._sleep(d["pause_s"])
            if self.stop_flag.is_set():
                self._cell(dev, "scale", "skipped", "stopped", info={"k": "stopped"})
                return False
            if done:
                self._cell(dev, "scale", "passed", done, info={"k": "size", "v": done})
            elif not res["scale"]:
                self._cell(dev, "scale", "failed", "no step within the route limit", info={"k": "limit"})
            else:
                err = res["scale"][-1].get("error")
                self._cell(dev, "scale", "failed", (err or "first step failed")[:160],
                           info={"k": "text", "v": err[:160]} if err else {"k": "first_failed"})
        return True

    # ------------------------------------------------------------------ K10, K11
    def _compare(self) -> list[dict]:
        out = []
        for n in self.doc["devices"]:
            r = self.doc["results"].get(n, {})
            sm, sc = r.get("smoke") or {}, r.get("scale") or []
            passed = [x for x in sc if x.get("state") == "done" and x.get("gate_ok")]
            mems = [x.get("min_free_mem_pct") for x in [sm, *sc] if x.get("min_free_mem_pct") is not None]
            series = r.get("series") or {"default": {}}
            for key, s in series.items():
                first = key == next(iter(series))
                out.append({"device": n, "label": r.get("label"), "mrai": s.get("mrai", "default"), "mrai_s": s.get("mrai_s"),
                            "conn": (r.get("test") or {}).get("ok"),
                            "smoke_conv_s": sm.get("convergence_s") if first else None,
                            "smoke_missing": sm.get("missing") if first else None,
                            "sustainable_rate": s.get("sustainable_rate"), "knee_rate": s.get("knee_rate"),
                            "top_rate": s.get("top_rate"), "sustainable_deliveries_s": s.get("sustainable_deliveries_s"),
                            "max_output_s": max([x for x in [s.get("max_output_s"),
                                                             *([sm.get("output_s"), *[y.get("output_s") for y in sc]] if first else [])]
                                                 if x] or [None]),
                            "largest_scale": (passed[-1]["size"] if passed else None) if first else None,
                            "min_free_mem_pct": (min(mems) if mems else None) if first else None})
        return out

    def _knee(self) -> list[dict]:
        out = []
        for n in self.doc["devices"]:
            for key, s in (self.doc["results"].get(n, {}).get("series") or {}).items():
                pts = [[x["offered_deliveries_s"], x.get("output_s"), x.get("after_last_send_s"), bool(x.get("past_knee"))]
                       for x in s.get("rows", []) if "past_knee" in x]
                out.append({"device": n, "mrai": s["mrai"], "mrai_s": s["mrai_s"], "limit_s": 5 + (s["mrai_s"] or 0),
                            "points": pts, "sustainable_deliveries_s": s.get("sustainable_deliveries_s"),
                            "knee_rate": s.get("knee_rate"), "label": self.doc["results"][n].get("label"),
                            "running": self.doc["state"] == "running" and (self.doc.get("current") or {}).get("device") == n
                            and (self.doc["current"] or {}).get("col") == ("series_device" if key == "default" else "series_zero")})
        return out
