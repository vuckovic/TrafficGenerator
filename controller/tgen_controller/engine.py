# SPDX-License-Identifier: MIT
"""Scenario engine (docs/08 §4): prepare -> run the phases on one timeline -> finish.

One run at a time. Every agent call is async; DUT SSH work runs in a thread, the
analysis in a separate low-priority process, so the API and the live feed stay fast.
"""
from __future__ import annotations

import asyncio
import concurrent.futures as cf
import json
import multiprocessing
import os
import shutil
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from . import __version__
from .agents import Agent, AgentError, each
from .health import Health
from .scenario import DEFAULT_TIMEOUT, Scenario, expand_churn
from .settings import Settings
from .store import Store, now_ms

LEAD_MS = 1000          # phases are scheduled this far ahead, so every node switches at the same moment
END_GRACE_MS = 5000     # teardown -> end (doc 07: everyone first receives everyone's withdrawals)
TICK_S = 1.0


class Busy(RuntimeError):
    pass


class StopRun(Exception):
    pass


def _pct(v: list[float], q: float) -> float | None:
    if not v:
        return None
    s = sorted(v)
    return round(s[min(len(s) - 1, int(q * (len(s) - 1)))], 1)


@dataclass
class Ctx:
    id: str
    dir: Path
    sc: Scenario
    agents: list[Agent] = field(default_factory=list)
    plans: dict = field(default_factory=dict)
    meta: dict = field(default_factory=dict)
    state: str = "created"
    phase_idx: int = -1
    phase_type: str = ""
    phase_info: dict = field(default_factory=dict)
    times: dict = field(default_factory=dict)           # establish_at, announce_at, withdraw_at, end_at (ms)
    stop: asyncio.Event = field(default_factory=asyncio.Event)
    stop_reason: str = ""
    last: dict = field(default_factory=dict)             # latest aggregated stats
    prev: dict = field(default_factory=dict)
    tx: dict = field(default_factory=dict)               # live latency join: (key, gen, kind) -> t_ns
    dut: object = None
    dut_info: dict = field(default_factory=dict)
    dut_poll_task: asyncio.Task | None = None
    dut_lock: threading.Lock = field(default_factory=threading.Lock)   # one SSH command at a time
    node_cpu: dict = field(default_factory=dict)         # node -> [pct, ...]
    port_errors: dict = field(default_factory=dict)      # DUT port counter growth since the start (live, M9)
    dut_load: dict = field(default_factory=dict)         # last DUT free memory / CPU (live)


class Engine:
    def __init__(self, cfg: Settings, store: Store, health: Health, hub):
        self.cfg, self.store, self.health, self.hub = cfg, store, health, hub
        self.client = httpx.AsyncClient()
        self.ctx: Ctx | None = None
        self.task: asyncio.Task | None = None
        self.owner: str | None = None   # a test scheme that holds the engine (P3c-2): no other runs meanwhile
        # spawn, not fork: a forked worker would inherit the listening socket and keep the port
        # open after the controller stops
        self.pool = cf.ProcessPoolExecutor(max_workers=1, initializer=os.nice, initargs=(10,),
                                           mp_context=multiprocessing.get_context("spawn"))

    def agents(self) -> list[Agent]:
        return [Agent(n, self.cfg.agent_token, self.client) for n in self.cfg.nodes]

    # ------------------------------------------------------------------ public
    async def start(self, sc: Scenario, owner: str | None = None) -> str:
        if self.task and not self.task.done():
            raise Busy(f"run {self.ctx.id} is {self.ctx.state}")
        if self.owner and owner != self.owner:
            raise Busy(f"test scheme {self.owner} is running")
        rid = time.strftime("r-%Y%m%d-%H%M%S")
        rdir = self.cfg.runs_dir / rid
        rdir.mkdir(parents=True)
        self.store.create_run(rid, sc.name, sc.model_dump(), str(rdir))
        (rdir / "scenario.json").write_text(json.dumps(sc.model_dump(), indent=1))
        self.ctx = Ctx(rid, rdir, sc)
        self.health.reset_run()
        self.task = asyncio.create_task(self._run(self.ctx))
        return rid

    async def stop(self, rid: str, reason: str = "stopped by the user") -> bool:
        c = self.ctx
        if not c or c.id != rid or c.state not in ("preparing", "running"):
            return False
        c.stop_reason = reason
        c.stop.set()
        # §10.1 item 4: Stop reaches all agents within 1 s, whatever the engine is doing
        await each(c.agents, lambda a: asyncio.wait_for(a.stop(), 1.5), return_exceptions=True)
        return True

    async def cluster(self) -> list[dict]:
        out = []
        res = await each(self.agents(), lambda a: a.status(), return_exceptions=True)
        for n, r in zip(self.cfg.nodes, res):
            d = {"node": n.name, "mgmt_ip": n.mgmt_ip, "traffic_ip": n.traffic_ip, "agent": n.agent}
            if isinstance(r, Exception):
                d.update(reachable=False, error=str(r)[:200])
            else:
                d.update(reachable=True, version=r.get("version"), state=r.get("state"), run_id=r.get("run_id"),
                         routers=r.get("routers"), clock_offset_ms=r.get("now_ms", now_ms()) - now_ms())
            out.append(d)
        return out

    # ------------------------------------------------------------------ helpers
    def _event(self, kind: str, sev: str, text: str) -> None:
        c = self.ctx
        e = self.store.event(c.id, kind, sev, text)
        self.hub.publish({"type": "event", "run": c.id, **e})

    def _state(self, s: str, **kw) -> None:
        self.ctx.state = s
        self.store.update_run(self.ctx.id, state=s, **kw)
        self.hub.publish({"type": "state", "run": self.ctx.id, "state": s, "t_ms": now_ms()})

    def _check_stop(self) -> None:
        if self.ctx.stop.is_set():
            raise StopRun(self.ctx.stop_reason or "stopped")

    async def _sleep(self, s: float) -> None:
        try:
            await asyncio.wait_for(self.ctx.stop.wait(), timeout=max(0.0, s))
        except asyncio.TimeoutError:
            pass
        self._check_stop()

    # ------------------------------------------------------------------ the run
    async def _run(self, c: Ctx) -> None:
        poller = deadline = None
        result_state, error = "done", None
        try:
            await self._prepare(c)
            self._state("running", started_ms=now_ms())
            poller = asyncio.create_task(self._poll(c))
            deadline = asyncio.create_task(self._deadline(c))
            await self._phases(c)
        except StopRun as e:
            result_state = "stopped"
            self._event("run", "warning", f"run stopped: {e}")
        except Exception as e:  # noqa: BLE001 - any failure still runs the cleanup
            result_state, error = "failed", f"{e.__class__.__name__}: {e}"
            self._event("run", "error", error)
        finally:
            for t in (poller, deadline):
                if t:
                    t.cancel()
            if c.dut_poll_task and not c.dut_poll_task.done():
                # a DUT command in a thread cannot be cancelled: let it finish before the
                # final reads use the same SSH session
                try:
                    await asyncio.wait_for(asyncio.shield(c.dut_poll_task), 150)
                except Exception:  # noqa: BLE001
                    pass
            await self._finish(c, result_state, error)

    async def _deadline(self, c: Ctx) -> None:
        limit = min(c.sc.limits.max_duration_s, self.cfg.max_duration_s)
        await asyncio.sleep(limit)
        self._event("guard", "warning", f"maximum run duration {limit:.0f} s reached")
        await self.stop(c.id, f"maximum duration {limit:.0f} s")

    # ------------------------------------------------------------------ prepare
    async def _prepare(self, c: Ctx) -> None:
        from tgen import inventory

        self._state("preparing")
        sc = c.sc
        agents = self.agents()
        st = await each(agents, lambda a: a.status(), return_exceptions=True)
        for a, s in zip(agents, st):
            if isinstance(s, Exception):
                raise RuntimeError(f"agent not reachable: {s}")
            if s.get("state") in ("loaded", "running"):
                raise RuntimeError(f"{a.node.name}: agent busy with run {s.get('run_id')}")
            if s.get("state") == "finished":             # left from an earlier run
                await a.delete()
            off = s.get("now_ms", now_ms()) - now_ms()
            if abs(off) > self.cfg.clock_tolerance_ms:
                raise RuntimeError(f"{a.node.name}: clock differs by {off} ms from the controller (chrony?)")

        # ---- DUT
        from .dutspec import make_dut, resolve
        d = sc.dut
        inv = inventory.load()
        info = resolve(d, inv)
        dut_ip, asn, role, adapter = info["ip"], info["asn"], info["role"], info["adapter"]
        c.dut_info = {k: info[k] for k in ("profile", "label", "adapter", "ip", "asn", "role", "mrai", "link_mbps",
                                           "min_free_mem_pct", "max_routes", "interface")}
        routes = sc.routers.total * sc.routers.prefixes
        if info["max_routes"] and routes > info["max_routes"]:
            raise RuntimeError(f"{routes} routes is above the route limit of {info['max_routes']} for {info['label']}")
        if d.configure and adapter:
            c.dut = make_dut(info, d)
            res = await asyncio.to_thread(c.dut.resources)
            c.dut_info["resources"] = res
            self._event("dut", "info", f"DUT: {res.get('summary')}")
            guard = c.dut_info["min_free_mem_pct"]
            if guard and res.get("free_pct") is not None and res["free_pct"] < guard:
                raise RuntimeError(f"DUT has only {res['free_pct']} % memory free (guard {guard} %)")
            try:
                c.dut_info["port_before"] = await asyncio.to_thread(c.dut.interface_info, dut_ip, info["interface"])
            except Exception as e:  # noqa: BLE001
                self._event("dut", "info", f"port check skipped: {e}")
        self.store.update_run(c.id, dut=c.dut_info)

        # ---- plan
        plans, meta = await asyncio.to_thread(make_plan, self.cfg, sc, c.id, dut_ip, asn, role, inv)
        live_mod = max(1, routes // 2000)                # ~2 000 sampled prefixes for the live latency
        c.agents = [a for a in agents if plans.get(a.node.name, {}).get("routers")]
        for a in c.agents:
            pl = dict(plans[a.node.name])
            pl.pop("timing", None)
            pl.update(seed=sc.routers.seed, hold_time=sc.routers.hold_time, controller_timeout_s=5,
                      live_sample_mod=live_mod)
            if self.cfg.traffic_iface:
                pl["iface"] = self.cfg.traffic_iface
            plans[a.node.name] = pl
            (c.dir / f"plan-{a.node.name}.json").write_text(json.dumps(pl))
        c.plans, c.meta = plans, meta
        (c.dir / "routers.json").write_text(json.dumps(meta["routers"]))   # for the UI's routers table
        self._event("plan", "info", f"{sc.routers.total} routers x {sc.routers.prefixes} prefixes on "
                    + ", ".join(f"{k} {v}" for k, v in meta["router_counts"].items() if v))

        # ---- DUT configuration, then load the agents
        if c.dut:
            cfg_text = c.dut.render(meta, dut_ip, asn, role)
            (c.dir / f"dut-config.{c.dut.config_ext}").write_text(cfg_text)
            n = await asyncio.to_thread(c.dut.push, cfg_text, len(meta["routers"]))
            c.dut_info["configured"] = True
            self._event("dut", "info", f"DUT configured: {n} BGP neighbours, role {role}, AS {asn}")
        self._check_stop()
        await each(c.agents, lambda a: a.load(c.plans[a.node.name]))
        self._event("plan", "info", f"plans loaded on {len(c.agents)} agents")

    # ------------------------------------------------------------------ phases
    async def _send(self, c: Ctx, typ: str, params: dict, at_ms: int | None = None) -> int:
        at = at_ms or now_ms() + LEAD_MS
        await each(c.agents, lambda a: a.phase(typ, at, params))
        return at

    async def _hold_if_unhealthy(self, c: Ctx) -> None:
        """AC6: while the controller is not healthy the next phase waits (max 60 s)."""
        v = self.health.violations()
        if not v:
            return
        self._event("guard", "warning", "holding the scenario: " + "; ".join(v))
        t0 = time.monotonic()
        while self.health.violations() and time.monotonic() - t0 < 60:
            await self._sleep(1)
        self._event("guard", "info", "continuing the scenario")

    async def _wait(self, c: Ctx, cond, timeout_s: float, what: str) -> bool:
        t0 = time.monotonic()
        while time.monotonic() - t0 < timeout_s:
            if c.last and cond(c.last):
                return True
            await self._sleep(0.5)
        self._event("phase", "warning", f"{what}: not reached within {timeout_s:.0f} s, continuing")
        return False

    async def _phases(self, c: Ctx) -> None:
        total = len(c.meta["routers"])
        names = [r["name"] for r in c.meta["routers"]]
        for i, p in enumerate(c.sc.phases):
            await self._hold_if_unhealthy(c)
            self._check_stop()
            params: dict = {}
            if p.type == "establish":
                params = {"per_s": p.per_s}
            elif p.type in ("announce", "withdraw", "teardown"):
                params = {"rate": p.rate}
            elif p.type == "flap":
                if isinstance(p.routers, list):
                    sel = p.routers
                else:                                       # spread over the nodes
                    k = max(1, min(p.routers, total))
                    sel = [names[int(j * total / k)] for j in range(k)]
                params = {"kind": p.kind, "down_s": p.down_s, "routers": sel}
            c.phase_idx, c.phase_type = i, p.type
            if p.type == "churn":
                steps = expand_churn(p)
                at0 = now_ms() + LEAD_MS
                c.phase_info = {"rate": steps[0][1]}
                self.store.phase(c.id, i, p.type, p.model_dump(exclude_none=True), at0)
                self._event("phase", "info", f"phase {i + 1}: churn" + (f", stepped {steps[0][1]:g} -> {steps[-1][1]:g} ops/s"
                                                                          if len(steps) > 1 else f" {p.rate:g} ops/s"))
                for off, rate in steps:
                    at = at0 + int(off * 1000)
                    await self._sleep((at - LEAD_MS - now_ms()) / 1000)
                    await self._send(c, "churn", {"rate": rate, "mix": p.mix}, at)
                    c.phase_info = {"rate": rate}
                await self._sleep((at0 + p.duration_s * 1000 - now_ms()) / 1000)
                await self._send(c, "steady", {})          # stops churn before the next phase
                self.store.phase_end(c.id, i, now_ms())
                continue
            at = await self._send(c, p.type, params)
            c.phase_info = {k: v for k, v in params.items() if k != "routers"} | (
                {"routers": len(params["routers"])} if "routers" in params else {})
            self.store.phase(c.id, i, p.type, p.model_dump(exclude_none=True) | ({"selected": params["routers"]}
                                                                                if p.type == "flap" else {}), at)
            self._event("phase", "info", f"phase {i + 1}: {p.type}" + (f" {params}" if params else ""))
            key = {"establish": "establish_at", "announce": "announce_at", "withdraw": "withdraw_at",
                   "teardown": "withdraw_at"}.get(p.type)
            if key and key not in c.times:
                c.times[key] = at
            await self._sleep((at - now_ms()) / 1000)
            to = p.timeout_s or DEFAULT_TIMEOUT.get(p.type, 60)
            if p.type == "establish":
                await self._wait(c, lambda s: s["sessions"].get("Established", 0) >= total, to, "all sessions Established")
            elif p.type == "announce":
                await self._wait(c, lambda s: s["converged"] >= total, to, "initial convergence")
            elif p.type in ("withdraw", "teardown"):
                await self._wait(c, lambda s: s["routes_held"] == 0, to, "all routes withdrawn")
            else:                                            # steady, flap
                await self._sleep(p.duration_s)
            self.store.phase_end(c.id, i, now_ms())
        # end: Cease and close, after a grace period
        at = await self._send(c, "end", {}, now_ms() + END_GRACE_MS)
        c.times["end_at"] = at
        self._event("phase", "info", "end: all routers close")
        await self._sleep((at - now_ms()) / 1000 + 1)
        t0 = time.monotonic()
        while time.monotonic() - t0 < 20:
            st = await each(c.agents, lambda a: a.status(), return_exceptions=True)
            if all(not isinstance(s, Exception) and s.get("state") == "finished" for s in st):
                break
            await self._sleep(0.5)

    # ------------------------------------------------------------------ live
    async def _poll(self, c: Ctx) -> None:
        last_dut = 0.0
        while True:
            t0 = time.monotonic()
            try:
                await self._tick(c)
            except Exception as e:  # noqa: BLE001 - a missed tick must not end the run
                self._event("live", "warning", f"stats: {e}")
            if c.dut and c.sc.dut.poll_s > 0 and time.monotonic() - last_dut >= c.sc.dut.poll_s and \
                    (c.dut_poll_task is None or c.dut_poll_task.done()):
                last_dut = time.monotonic()
                c.dut_poll_task = asyncio.create_task(self._dut_poll(c))
            await asyncio.sleep(max(0.05, TICK_S - (time.monotonic() - t0)))

    async def _tick(self, c: Ctx) -> None:
        res = await each(c.agents, lambda a: a.stats(), return_exceptions=True)
        t = now_ms()
        agg = {"sessions": {}, "routes_held": 0, "converged": 0, "prefixes_sent": 0, "prefixes_withdrawn": 0,
               "nlri_rx": 0, "withdrawn_rx": 0, "bytes_rx": 0, "resets": 0, "planned_resets": 0, "looped_rx": 0}
        nodes, lat, routers = [], [], []
        for a, s in zip(c.agents, res):
            if isinstance(s, Exception):
                nodes.append({"node": a.node.name, "error": str(s)[:120]})
                continue
            for k, v in s["by_state"].items():
                agg["sessions"][k] = agg["sessions"].get(k, 0) + v
            tot = s["totals"]
            for k in ("routes_held", "converged", "prefixes_sent", "prefixes_withdrawn", "nlri_rx", "withdrawn_rx",
                      "bytes_rx", "resets", "planned_resets", "looped_rx"):
                agg[k] += tot.get(k, 0) or 0
            cpu = s.get("node_cpu_pct")
            if cpu is not None:
                c.node_cpu.setdefault(a.node.name, []).append(cpu)
            nodes.append({"node": a.node.name, "routers": len(s.get("per_router", [])), "cpu_pct": cpu,
                          "temp_c": s.get("node_temp_c"), "send_lag_p99_ms": tot.get("send_lag_p99_ms")})
            for r in s.get("per_router", []):
                routers.append([r["name"], a.node.name, r["state"], r.get("held", 0), r.get("sent", 0), r.get("rx", 0)])
            for key, gen, kind, ts in s["samples"]["tx"]:
                c.tx[(key, gen, 1 if kind == 3 else kind)] = ts
            for key, gen, kind, ts in s["samples"]["rx"]:
                t_tx = c.tx.get((key, gen, kind))
                if t_tx:
                    lat.append((ts - t_tx) / 1e6)
        if len(c.tx) > 200_000:                              # bounded: keep the newest half
            for k in list(c.tx)[: len(c.tx) // 2]:
                del c.tx[k]
        prev, dt = c.prev, (t - c.prev["t_ms"]) / 1000 if c.prev else None
        rate = (lambda k: round((agg[k] - prev[k]) / dt, 1) if dt and dt > 0 else None)
        tick = {"type": "tick", "t_ms": t, "run": c.id, "state": c.state,
                "phase": {"idx": c.phase_idx, "type": c.phase_type, **c.phase_info},
                "sessions": agg["sessions"], "routes_held": agg["routes_held"], "converged": agg["converged"],
                "rates": {"sent_s": rate("prefixes_sent"), "rx_s": rate("nlri_rx"),
                          "withdrawn_sent_s": rate("prefixes_withdrawn"), "withdrawn_rx_s": rate("withdrawn_rx"),
                          "bytes_rx_s": rate("bytes_rx")},
                "latency_ms": {"p50": _pct(lat, 0.5), "p99": _pct(lat, 0.99), "n": len(lat)},
                "resets": agg["resets"], "planned_resets": agg["planned_resets"], "looped_rx": agg["looped_rx"],
                "nodes": nodes, "routers": sorted(routers), "dut_port": self._port_live(c, rate("bytes_rx")),
                "dut_load": c.dut_load or None}
        c.prev = {**agg, "t_ms": t}
        c.last = agg
        if agg["resets"] > (prev.get("resets", 0) if prev else 0):
            self._event("session", "warning", f"unexpected session resets: {agg['resets']}")
        tick["controller"] = self.health.snapshot()
        self.hub.publish(tick, data_t_ms=t)
        stored = {k: v for k, v in tick.items() if k != "routers"}   # per-router rows: live only (size)
        self.store.tick(c.id, t, stored)
        with open(c.dir / "live.jsonl", "a") as f:
            f.write(json.dumps(stored) + "\n")

    @staticmethod
    def _port_live(c: Ctx, bytes_rx_s: float | None) -> dict | None:
        """M9: DUT port and link use. Output in BGP Mb/s + ~6 % TCP/IP/Ethernet overhead."""
        pb = c.dut_info.get("port_before")
        speed = (pb or {}).get("speed_mbps") or c.dut_info.get("link_mbps")
        if not pb and not c.dut:
            return None
        mbps = (bytes_rx_s or 0) * 8 / 1e6 * 1.06
        return {"interface": (pb or {}).get("interface"), "speed_mbps": speed, "duplex": (pb or {}).get("duplex"),
                "up": (pb or {}).get("up"), "mbps": round(mbps, 2), "use_pct": round(100 * mbps / speed, 2) if speed else None,
                "errors": c.port_errors, "reported": bool(pb)}

    async def _dut_poll(self, c: Ctx) -> None:
        guard = c.dut_info.get("min_free_mem_pct") or 0
        rec = await asyncio.to_thread(self._dut_poll_sync, c, guard)
        with open(c.dir / "dut-sessions.jsonl", "a") as f:
            f.write(json.dumps(rec) + "\n")
        if "port_errors" in rec:
            if sum(rec["port_errors"].values()) > sum(c.port_errors.values()):
                self._event("dut", "warning", "new DUT port errors: " + ", ".join(f"{k} +{v}" for k, v in rec["port_errors"].items()))
            c.port_errors = rec["port_errors"]
        if rec.get("free_pct") is not None:
            c.dut_load = {"free_pct": rec["free_pct"], "cpu_load": rec.get("cpu_load"), "t": rec["t"]}
        free = rec.get("free_pct")
        if guard and free is not None and free < guard:
            self._event("guard", "error", f"DUT memory guard: {free} % free < {guard} %")
            await self.stop(c.id, f"DUT memory guard ({free} % free)")

    @staticmethod
    def _dut_poll_sync(c: Ctx, guard: float) -> dict:
        dut = c.dut
        rec: dict = {"t": round(time.time(), 3)}
        with c.dut_lock:
            try:
                dut.call_timeout = 15
                sess, est = dut.sessions()
                rec.update(established=est, raw=sess)
                if guard:
                    ld = dut.load()
                    rec.update(free_pct=ld.get("free_pct"), cpu_load=ld.get("cpu_load"))
                pb = c.dut_info.get("port_before")
                if pb:   # live port errors (M9): one `show interfaces X` per poll
                    from tgen.dut.cisco import interface_delta
                    pa = dut.interface_info(c.dut_info["ip"], pb["interface"])
                    rec["port_errors"] = interface_delta(pb, pa)
            except Exception as e:  # noqa: BLE001
                rec["error"] = str(e)[:300]
            finally:
                dut.call_timeout = None
        return rec

    # ------------------------------------------------------------------ finish
    async def _finish(self, c: Ctx, state: str, error: str | None) -> None:
        self._state("finishing")
        results = []
        if c.agents:
            # anything still running is stopped; results are collected from every agent that has one
            st = await each(c.agents, lambda a: a.status(), return_exceptions=True)
            if any(not isinstance(s, Exception) and s.get("state") in ("loaded", "running") for s in st):
                await each(c.agents, lambda a: a.stop(), return_exceptions=True)
            res = await each(c.agents, lambda a: a.result(), return_exceptions=True)
            for a, r in zip(c.agents, res):
                if isinstance(r, Exception):
                    self._event("result", "warning", f"no result from {a.node.name}: {r}")
                    continue
                (c.dir / f"result-{a.node.name}.json").write_text(json.dumps(r))
                results.append(r)
            await each(c.agents, lambda a: a.delete(), return_exceptions=True)
        if c.dut:
            await asyncio.to_thread(self._dut_finish, c)
        summary = None
        if results and c.times.get("announce_at"):
            c.meta["timing"] = {"establish_at": c.times.get("establish_at"), "announce_at": c.times["announce_at"],
                                "withdraw_at": c.times.get("withdraw_at"), "end_at": c.times.get("end_at", now_ms())}
            (c.dir / "meta.json").write_text(json.dumps(c.meta))
            cpu = {n: _pct(v, 0.95) for n, v in c.node_cpu.items() if v}
            try:
                loop = asyncio.get_running_loop()
                summary = await loop.run_in_executor(self.pool, analyse, str(c.dir), cpu, c.dut_info,
                                                     self.health.run_max, state)
            except Exception as e:  # noqa: BLE001
                self._event("analysis", "error", f"analysis failed: {e}")
        if summary and summary.get("gate_ok") is False and state == "done":
            self._event("gate", "warning", "gate: " + "; ".join(g["name"] for g in summary["gate"] if g["passed"] is False))
        self.store.update_run(c.id, ended_ms=now_ms(), summary=summary or {}, error=error)
        self._state(state)

    def _dut_finish(self, c: Ctx) -> None:
        with c.dut_lock:
            self._dut_finish_locked(c)

    def _dut_finish_locked(self, c: Ctx) -> None:
        dut = c.dut
        dut.call_timeout = None
        for name, fn in (("dut-bgp.log", dut.log), ("dut-resources-end.json", lambda: json.dumps(dut.resources()))):
            try:
                (c.dir / name).write_text(fn())
            except Exception as e:  # noqa: BLE001
                self.store.event(c.id, "dut", "warning", f"{name}: {e}")
        pb = c.dut_info.get("port_before")
        if pb:
            try:
                from tgen.dut.cisco import interface_delta
                pa = dut.interface_info(c.dut_info["ip"], pb["interface"])
                c.dut_info["port_after"] = pa
                c.dut_info["port_errors"] = interface_delta(pb, pa)
            except Exception as e:  # noqa: BLE001
                self.store.event(c.id, "dut", "warning", f"port counters after the run: {e}")
        if c.dut_info.get("configured") and not c.sc.dut.keep_config:
            try:
                dut.remove()
                self.store.event(c.id, "dut", "info", "DUT: tgen neighbours removed")
            except Exception as e:  # noqa: BLE001
                self.store.event(c.id, "dut", "error", f"could not remove the tgen neighbours from the DUT: {e}")
        self.store.update_run(c.id, dut={k: v for k, v in c.dut_info.items() if k != "resources"} |
                              {"resources": c.dut_info.get("resources")})

    def delete_run(self, rid: str) -> bool:
        r = self.store.run(rid)
        if not r or r["state"] in ("created", "preparing", "running", "finishing"):
            return False
        shutil.rmtree(r["dir"], ignore_errors=True)
        self.store.delete_run(rid)
        return True


def make_plan(cfg: Settings, sc: Scenario, rid: str, dut_ip: str, asn: int, role: str, inv: dict | None = None):
    """Routers per node (CMNODE01 at weight 0.7); node, reserved, inventory and DUT addresses are skipped."""
    from tgen import inventory
    from tgen.planner import NodeSpec, Scenario as PlanScenario, build

    nodes = [NodeSpec(n.name, n.weight, [1, 2, 3]) for n in cfg.nodes]
    skip = [n.traffic_ip for n in cfg.nodes] + cfg.reserved_ips + inventory.reserved_ips(inv) + [dut_ip]
    ps = PlanScenario(run_id=rid, dut_ip=dut_ip, dut_asn=asn, role=role, routers_total=sc.routers.total,
                      prefixes_per_router=sc.routers.prefixes, seed=sc.routers.seed,
                      per_update=sc.routers.per_update, hold_time=sc.routers.hold_time, skip_ips=skip)
    return build(ps, nodes)


# ---------------------------------------------------------------------- analysis (separate process)
def analyse(rdir: str, cpu: dict, dut_info: dict, health_max: dict, state: str) -> dict:
    """tools/tgen/analyze.py on the run folder -> report.json, summary.txt; returns the stored summary."""
    from tgen.analyze import analyze, attribute_detail, gate

    d = Path(rdir)
    meta = json.loads((d / "meta.json").read_text())
    results = [json.loads(p.read_text()) for p in sorted(d.glob("result-*.json"))]
    rep = analyze(meta, results)
    role = meta["scenario"]["role"]
    reference = dut_info.get("adapter") in (None, "bird-ssh") and not dut_info.get("profile")
    rep["cpu_node_p95"] = cpu
    rep["dut"] = {k: v for k, v in dut_info.items() if k not in ("resources",)}
    rep["controller_health_max"] = health_max
    rep["version"] = {"controller": __version__, "agents": sorted({r.get("version") for r in results})}
    g = gate(rep, cpu, role=role, reference_dut=reference)
    rep["gate"] = [{"name": n, "passed": p, "detail": det} for n, p, det in g]
    do = rep.get("dut_output") or {}
    if do.get("payload_mbps") is not None and dut_info.get("link_mbps"):
        do["link_use_pct"] = round(100 * do["payload_mbps"] * 1.06 / dut_info["link_mbps"], 2)
    (d / "report.json").write_text(json.dumps(rep, indent=1))
    s, cv, la = rep["sessions"], rep["convergence"], rep["latency_announce_ms"]
    lines = [f"run {meta['run_id']}: {len(meta['routers'])} routers, role {role}, DUT {dut_info.get('label')} "
             f"{dut_info.get('ip')} ({state})",
             f"sessions      : {s['established']}/{s['configured']} Established, resets {s['resets']}",
             f"convergence   : initial {cv['initial_s']:.1f} s" if cv["initial_s"] else
             f"convergence   : NOT reached ({cv['receivers_converged']}/{cv['receivers']})",
             f"latency ann.  : p50 {la['p50'] or 0:.0f} ms, p99 {la['p99'] or 0:.0f} ms (n={la['n']})",
             f"DUT output    : {do.get('prefixes_per_s') or 0:,.0f} prefixes/s",
             f"correctness   : missing {rep['correctness']['missing_routes']:,}, extra "
             f"{rep['correctness']['extra_routes']:,} ({attribute_detail(rep, role)})",
             f"controller    : API p95 max {health_max.get('api_p95_ms')} ms, live delay max "
             f"{health_max.get('ws_delay_ms')} ms, loop lag max {health_max.get('loop_lag_ms')} ms", "", "gate:"]
    lines += [f"  {'INFO' if x['passed'] is None else 'PASS' if x['passed'] else 'FAIL'}  {x['name']}: {x['detail']}"
              for x in rep["gate"]]
    (d / "summary.txt").write_text("\n".join(lines) + "\n")
    return {"sessions": f"{s['established']}/{s['configured']}", "initial_convergence_s": cv["initial_s"],
            "latency_p50_ms": la["p50"], "latency_p99_ms": la["p99"],
            "dut_output_prefixes_s": do.get("prefixes_per_s"), "missing": rep["correctness"]["missing_routes"],
            "resets": s["resets"], "gate": rep["gate"], "gate_ok": all(x["passed"] is not False for x in rep["gate"]),
            "controller_health_max": health_max, "cpu_node_p95": cpu}
