# SPDX-License-Identifier: MIT
"""HTTP API and WebSocket (docs/08 §3)."""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import time
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ValidationError

from . import REPO, __version__, auth
from .engine import LIMIT_DEFAULTS, Busy, Engine, LimitError
from .health import Health
from .live import Hub
from .scenario import PRESETS, Dut, Scenario
from .scheme import STEPS, SchemeError, SchemeRunner
from .settings import Settings, load
from .store import Store, now_ms


class Login(BaseModel):
    password: str


class PasswordChange(BaseModel):
    current: str
    new: str = Field(min_length=10, max_length=200)


class Notes(BaseModel):
    notes: str = Field("", max_length=20000)


class Limits(BaseModel):
    """S3 safety limits."""
    max_routers: int = Field(ge=1, le=600)
    max_prefixes_total: int = Field(ge=1, le=10_000_000)
    max_duration_s: float = Field(ge=60, le=86400)
    max_rate_mbps: float = Field(gt=0, le=1000)
    subnet_allow: list[str] = Field(default_factory=list, max_length=50)


class DutAction(BaseModel):
    password: str | None = None
    enable: str | None = None
    routers: int = Field(60, ge=1, le=600)
    role: str | None = None


class DutRequest(BaseModel):
    """U14 buttons for any device: a profile, explicit fields, or both (as a scenario's dut)."""
    dut: Dut
    routers: int = Field(60, ge=1, le=600)


DUT_ACTIONS = ("test", "preview", "push", "remove", "status")


class SchemeDevice(BaseModel):
    profile: str
    password: str | None = None    # kept in memory for the scheme only
    enable: str | None = None


class SchemeRequest(BaseModel):
    """K2-K6: devices in order, steps, pause between runs, stop a device on failure."""
    devices: list[SchemeDevice] = Field(min_length=1, max_length=20)
    steps: list[str] = list(STEPS)
    pause_s: float = Field(30, ge=0, le=600)
    stop_on_failure: bool = True


class SchemePlanRequest(BaseModel):
    devices: list[str] = Field(min_length=1, max_length=20)
    steps: list[str] = list(STEPS)
    pause_s: float = Field(30, ge=0, le=600)


def create_app(cfg: Settings | None = None) -> FastAPI:
    cfg = cfg or load()
    health = Health()
    hub = Hub(health)
    store = Store(cfg.db_path)
    engine = Engine(cfg, store, health, hub)
    schemes = SchemeRunner(engine, store, hub, cfg)
    lockout = auth.Lockout()
    started = time.time()

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        for rid in store.fail_unfinished():   # §4.3: a restart during a run fails it
            store.event(rid, "run", "error", "controller restarted during the run")
        store.fail_unfinished_schemes()
        mon = asyncio.create_task(health.loop_monitor())
        yield
        mon.cancel()
        if schemes.active():
            schemes.stop_flag.set()
        if engine.ctx and engine.task and not engine.task.done():
            await engine.stop(engine.ctx.id, "controller shutting down")
            with contextlib.suppress(Exception):
                await asyncio.wait_for(engine.task, 60)
        await engine.client.aclose()
        engine.pool.shutdown(cancel_futures=True)

    app = FastAPI(title="tgen-controller", version=__version__, lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url="/api/openapi.json")
    app.state.cfg, app.state.engine, app.state.store, app.state.health, app.state.hub = cfg, engine, store, health, hub
    app.state.schemes = schemes

    @app.middleware("http")
    async def timing(request: Request, call_next):
        t0 = time.perf_counter()
        resp = await call_next(request)
        health.request((time.perf_counter() - t0) * 1000)
        return resp

    def need_session(request: Request) -> None:
        if not auth.valid_token(cfg.secret, request.cookies.get(auth.COOKIE)):
            raise HTTPException(401, "login required")

    S = [Depends(need_session)]

    # ---------------------------------------------------------- session
    @app.post("/api/login")
    async def login(body: Login, request: Request, response: Response):
        addr = request.client.host if request.client else "?"
        wait = lockout.is_blocked(addr)
        if wait:
            raise HTTPException(429, f"too many failed logins, try again in {int(wait // 60) + 1} min")
        ph = store.setting("password_hash") or cfg.password_hash     # changed in the page (S6) wins over the file
        ok = bool(ph) and await asyncio.to_thread(auth.check_password, body.password, ph)
        if not ok:
            lockout.failure(addr)
            raise HTTPException(401, "wrong password")
        lockout.success(addr)
        response.set_cookie(auth.COOKIE, auth.make_token(cfg.secret, cfg.session_hours), httponly=True,
                            samesite="strict", secure=cfg.secure_cookie, max_age=int(cfg.session_hours * 3600))
        return {"ok": True}

    @app.post("/api/logout")
    async def logout(response: Response):
        response.delete_cookie(auth.COOKIE)
        return {"ok": True}

    @app.get("/api/me", dependencies=S)
    async def me():
        return {"user": "admin"}

    @app.post("/api/password", dependencies=S)
    async def change_password(body: PasswordChange):
        """S6: the new hash goes to the database (the controller cannot write /etc); set-password resets it."""
        ph = store.setting("password_hash") or cfg.password_hash
        if not (ph and await asyncio.to_thread(auth.check_password, body.current, ph)):
            raise HTTPException(403, "current password is wrong")
        store.set_setting("password_hash", await asyncio.to_thread(auth.hash_password, body.new))
        return {"ok": True}

    # ---------------------------------------------------------- settings (S1, S3)
    @app.get("/api/settings/limits", dependencies=S)
    async def get_limits():
        return {"limits": engine.limits(), "defaults": dict(LIMIT_DEFAULTS, max_duration_s=float(cfg.max_duration_s))}

    @app.put("/api/settings/limits", dependencies=S)
    async def put_limits(body: Limits):
        import ipaddress
        try:
            nets = [str(ipaddress.ip_network(n.strip(), strict=False)) for n in body.subnet_allow if n.strip()]
        except ValueError as e:
            raise HTTPException(422, f"subnet: {e}")
        if engine.task and not engine.task.done():
            raise HTTPException(409, "a run is active")
        store.set_setting("limits", body.model_dump() | {"subnet_allow": nets})
        return {"limits": engine.limits()}

    @app.post("/api/nodes/{name}/restart", dependencies=S)
    async def restart_agent(name: str):
        if engine.task and not engine.task.done() or schemes.active():
            raise HTTPException(409, "a run or test scheme is active")
        ag = next((a for a in engine.agents() if a.node.name == name), None)
        if not ag:
            raise HTTPException(404, "no such node")
        try:
            await ag.restart()
        except Exception as e:  # noqa: BLE001
            raise HTTPException(502, f"{name}: {e}")
        return {"ok": True}

    # ---------------------------------------------------------- status
    @app.get("/api/health")
    async def get_health():
        c = engine.ctx
        return {"version": __version__, "uptime_s": round(time.time() - started), "health": health.snapshot(),
                "violations": health.violations(), "run_max": health.run_max,
                "run": {"id": c.id, "state": c.state} if c else None,
                "scheme": {"id": schemes.doc["id"], "state": schemes.doc["state"]} if schemes.doc else None}

    @app.get("/api/cluster", dependencies=S)
    async def cluster():
        return {"controller_node": cfg.controller_node, "nodes": await engine.cluster()}

    @app.get("/api/presets", dependencies=S)
    async def presets():
        return PRESETS

    @app.get("/api/duts", dependencies=S)
    async def duts():
        from tgen import inventory
        safe = ("label", "adapter", "peer_ip", "ssh_host", "user", "asn", "role", "link_mbps", "max_routes",
                "min_free_mem_pct", "ssh_legacy", "interface", "mrai_s", "device_mrai_s", "smoke", "series", "scale")
        return {name: {k: v for k, v in p.items() if k in safe} | {
                    "password_set": bool(os.environ.get(inventory.password_env(name)))}
                for name, p in inventory.load().items()}

    def _dut_guard(action: str) -> None:
        if action not in DUT_ACTIONS:
            raise HTTPException(404, f"unknown action {action}")
        if action in ("push", "remove") and (engine.task and not engine.task.done() or schemes.active()):
            raise HTTPException(409, "a run or test scheme is active")

    @app.post("/api/duts/{profile}/{action}", dependencies=S)
    async def dut_action(profile: str, action: str, body: DutAction):
        _dut_guard(action)
        d = Dut(profile=profile, role=body.role, password=body.password, enable=body.enable)
        return await asyncio.to_thread(_dut_action, d, action, body.routers) | {"profile": profile}

    @app.post("/api/dut/{action}", dependencies=S)
    async def dut_action_any(action: str, body: DutRequest):
        _dut_guard(action)
        return await asyncio.to_thread(_dut_action, body.dut, action, body.routers)

    # ---------------------------------------------------------- test scheme (P3c-2, K1-K12)
    @app.post("/api/schemes/plan", dependencies=S)
    async def scheme_plan(body: SchemePlanRequest):
        from tgen import inventory
        inv = inventory.load()
        bad = [n for n in body.devices if n not in inv]
        if bad:
            raise HTTPException(422, "unknown devices: " + ", ".join(bad))
        return SchemeRunner.plan(inv, body.devices, body.steps, body.pause_s)

    @app.post("/api/schemes", status_code=202, dependencies=S)
    async def scheme_start(body: SchemeRequest):
        try:
            sid = await schemes.start([d.model_dump() | {"password": d.password, "enable": d.enable} for d in body.devices],
                                      body.steps, body.pause_s, body.stop_on_failure)
        except SchemeError as e:
            raise HTTPException(409 if "running" in str(e) or "active" in str(e) else 422, str(e))
        return {"id": sid}

    @app.get("/api/schemes", dependencies=S)
    async def scheme_list():
        return [{k: d.get(k) for k in ("id", "state", "created_ms", "ended_ms", "devices", "steps", "error")}
                for d in store.schemes(50)]

    @app.get("/api/schemes/{sid}", dependencies=S)
    async def scheme_get(sid: str):
        d = schemes.doc if schemes.doc and schemes.doc["id"] == sid else store.scheme(sid)
        if not d:
            raise HTTPException(404, "no such scheme")
        return d

    @app.post("/api/schemes/{sid}/stop", dependencies=S)
    async def scheme_stop(sid: str):
        if not (schemes.doc and schemes.doc["id"] == sid and await schemes.stop()):
            raise HTTPException(409, "scheme is not running")
        return {"ok": True}

    @app.get("/api/topology", dependencies=S)
    async def topology():
        return _topology()

    def _topology() -> dict:
        """K12: the lab as configured (cluster.env, controller.toml, duts.toml). No secrets."""
        from tgen import inventory
        from tgen_p2 import load_env
        env = load_env(Path(cfg.raw.get("cluster_env") or REPO / "deploy" / "cluster.env"))
        devs = [{"profile": n, "label": p["label"], "adapter": p["adapter"], "bgp_ip": p["peer_ip"],
                 "ssh_ip": p["ssh_host"], "asn": p.get("asn", 64500), "interface": p.get("interface"),
                 "link_mbps": p.get("link_mbps"), "role": p.get("role")} for n, p in inventory.load().items()]
        return {"nodes": [{"name": n.name, "mgmt_ip": n.mgmt_ip, "test_ip": n.traffic_ip,
                           "controller": n.name == cfg.controller_node, "agent_port": int(env.get("AGENT_PORT") or 7700)}
                          for n in cfg.nodes],
                "devices": devs,
                "mgmt_net": f'{env.get("MGMT_PREFIX", "192.168.100")}.0/{env.get("MGMT_CIDR", 24)}',
                "test_net": f'{env.get("TRAFFIC_PREFIX", "10.100.0")}.0/{env.get("TRAFFIC_CIDR", 24)}',
                "gateway": cfg.raw.get("gateway") or env.get("MGMT_GW") or None,
                "public": cfg.raw.get("public_url"), "board_switch": cfg.raw.get("board_switch", "DeskPi Super6C"),
                "router_first_ip": f'{env.get("TRAFFIC_PREFIX", "10.100.0")}.20',
                "lab_switch": cfg.raw.get("lab_switch")}

    @app.get("/api/i18n")
    async def i18n():
        return _i18n()

    # ---------------------------------------------------------- runs
    @app.post("/api/runs", status_code=202, dependencies=S)
    async def create_run(request: Request):
        try:
            sc = Scenario.model_validate((await request.json()).get("scenario"))
        except (ValidationError, ValueError, AttributeError) as e:
            raise HTTPException(422, str(e)[:2000])
        try:
            rid = await engine.start(sc)
        except Busy as e:
            raise HTTPException(409, str(e))
        except LimitError as e:
            raise HTTPException(422, str(e))
        return {"id": rid}

    @app.get("/api/runs", dependencies=S)
    async def list_runs(limit: int = 50):
        return store.runs(min(max(limit, 1), 500))

    def _get(rid: str) -> dict:
        r = store.run(rid)
        if not r:
            raise HTTPException(404, "no such run")
        return r

    @app.get("/api/runs/{rid}", dependencies=S)
    async def get_run(rid: str):
        r = _get(rid)
        return r | {"phases": store.phases(rid), "events": store.events(rid)}

    @app.get("/api/runs/{rid}/ticks", dependencies=S)
    async def get_ticks(rid: str):
        _get(rid)
        return store.ticks(rid)

    @app.post("/api/runs/{rid}/stop", dependencies=S)
    async def stop_run(rid: str):
        _get(rid)
        if not await engine.stop(rid):
            raise HTTPException(409, "run is not active")
        return {"ok": True}

    @app.get("/api/runs/{rid}/report", dependencies=S)
    async def report(rid: str):
        f = Path(_get(rid)["dir"]) / "report.json"
        if not f.exists():
            raise HTTPException(404, "no report (yet)")
        return JSONResponse(json.loads(f.read_text()))

    @app.get("/api/runs/{rid}/report.csv", dependencies=S)
    async def report_csv(rid: str):
        """X1 export: the report's figures as metric,value rows (lists of values are left out)."""
        import csv
        import io
        r = _get(rid)
        f = Path(r["dir"]) / "report.json"
        if not f.exists():
            raise HTTPException(404, "no report (yet)")
        rep = json.loads(f.read_text())
        out = io.StringIO()
        w = csv.writer(out)
        w.writerow(["metric", "value"])

        def walk(p: str, v) -> None:
            if isinstance(v, dict):
                for k, x in v.items():
                    walk(f"{p}.{k}" if p else k, x)
            elif isinstance(v, list):
                if p == "gate":
                    for g in v:
                        w.writerow([f"gate.{g['name']}", "INFO" if g["passed"] is None else "PASS" if g["passed"] else "FAIL"])
                elif all(not isinstance(x, (dict, list)) for x in v) and len(v) <= 20:
                    w.writerow([p, " ".join(map(str, v))])
            else:
                w.writerow([p, "" if v is None else v])
        walk("", rep)
        if r.get("notes"):
            w.writerow(["notes", r["notes"]])
        return Response(out.getvalue(), media_type="text/csv",
                        headers={"Content-Disposition": f'attachment; filename="{rid}-report.csv"'})

    @app.put("/api/runs/{rid}/notes", dependencies=S)
    async def put_notes(rid: str, body: Notes):
        _get(rid)
        store.update_run(rid, notes=body.notes)
        return {"ok": True}

    @app.post("/api/runs/{rid}/regenerate", dependencies=S)
    async def regenerate(rid: str):
        _get(rid)
        try:
            return {"summary": await engine.regenerate(rid)}
        except ValueError as e:
            raise HTTPException(409, str(e))

    @app.get("/api/runs/{rid}/files", dependencies=S)
    async def files(rid: str):
        d = Path(_get(rid)["dir"])
        return [{"name": p.name, "bytes": p.stat().st_size} for p in sorted(d.iterdir()) if p.is_file()]

    @app.get("/api/runs/{rid}/files/{name}", dependencies=S)
    async def file(rid: str, name: str):
        d = Path(_get(rid)["dir"]).resolve()
        f = (d / name).resolve()
        if f.parent != d or not f.is_file():
            raise HTTPException(404, "no such file")
        return FileResponse(f)

    @app.delete("/api/runs/{rid}", dependencies=S)
    async def delete_run(rid: str):
        _get(rid)
        if not engine.delete_run(rid):
            raise HTTPException(409, "run is still active")
        return {"ok": True}

    # ---------------------------------------------------------- live
    @app.websocket("/ws/live")
    async def live(ws: WebSocket):
        if not auth.valid_token(cfg.secret, ws.cookies.get(auth.COOKIE)):
            await ws.close(code=4401)
            return
        await ws.accept()
        with contextlib.suppress(WebSocketDisconnect, RuntimeError):
            await hub.serve(ws)

    def _dut_action(d: Dut, action: str, routers: int) -> dict:
        from .dutspec import action as dut_action
        try:
            return dut_action(d, action, routers, str(cfg.raw.get("cluster_env") or REPO / "deploy" / "cluster.env"))
        except ValueError as e:
            raise HTTPException(422, str(e))

    _i18n_cache: dict = {}

    def _i18n() -> dict:
        """ui/i18n/labels.csv as JSON: languages (from the _lang.* rows) and texts."""
        import csv
        f = REPO / "ui" / "i18n" / "labels.csv"
        m = f.stat().st_mtime
        if _i18n_cache.get("m") != m:
            rows = list(csv.reader(f.open(encoding="utf-8")))
            langs = rows[0][2:]
            meta = {r[0][6:]: r[2:] for r in rows[1:] if r and r[0].startswith("_lang.")}
            _i18n_cache.update(m=m, data={
                "languages": [{"tag": tag, **{k: v[i] for k, v in meta.items() if i < len(v)}} for i, tag in enumerate(langs)],
                "texts": {r[0]: {tag: r[2 + i] for i, tag in enumerate(langs) if 2 + i < len(r) and r[2 + i]}
                          for r in rows[1:] if r and not r[0].startswith("_lang.")}})
        return _i18n_cache["data"]

    # ---------------------------------------------------------- web interface (P3c), same origin
    flags = REPO / "ui" / "i18n" / "flags"
    web = Path(cfg.raw.get("web_dir") or REPO / "ui" / "web")
    if flags.is_dir():
        app.mount("/i18n/flags", StaticFiles(directory=flags), name="flags")
    if web.is_dir():
        app.mount("/", StaticFiles(directory=web, html=True), name="web")

    return app
