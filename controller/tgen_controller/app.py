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
from pydantic import BaseModel, ValidationError

from . import REPO, __version__, auth
from .engine import Busy, Engine
from .health import Health
from .live import Hub
from .scenario import PRESETS, Scenario
from .settings import Settings, load
from .store import Store, now_ms


class Login(BaseModel):
    password: str


class DutAction(BaseModel):
    password: str | None = None
    routers: int = 60
    role: str | None = None


DUT_ACTIONS = ("test", "preview", "push", "remove", "status")


def create_app(cfg: Settings | None = None) -> FastAPI:
    cfg = cfg or load()
    health = Health()
    hub = Hub(health)
    store = Store(cfg.db_path)
    engine = Engine(cfg, store, health, hub)
    lockout = auth.Lockout()
    started = time.time()

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        for rid in store.fail_unfinished():   # §4.3: a restart during a run fails it
            store.event(rid, "run", "error", "controller restarted during the run")
        mon = asyncio.create_task(health.loop_monitor())
        yield
        mon.cancel()
        if engine.ctx and engine.task and not engine.task.done():
            await engine.stop(engine.ctx.id, "controller shutting down")
            with contextlib.suppress(Exception):
                await asyncio.wait_for(engine.task, 60)
        await engine.client.aclose()
        engine.pool.shutdown(cancel_futures=True)

    app = FastAPI(title="tgen-controller", version=__version__, lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url="/api/openapi.json")
    app.state.cfg, app.state.engine, app.state.store, app.state.health, app.state.hub = cfg, engine, store, health, hub

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
        ok = bool(cfg.password_hash) and await asyncio.to_thread(auth.check_password, body.password, cfg.password_hash)
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

    # ---------------------------------------------------------- status
    @app.get("/api/health")
    async def get_health():
        c = engine.ctx
        return {"version": __version__, "uptime_s": round(time.time() - started), "health": health.snapshot(),
                "violations": health.violations(), "run_max": health.run_max,
                "run": {"id": c.id, "state": c.state} if c else None}

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

    @app.post("/api/duts/{profile}/{action}", dependencies=S)
    async def dut_action(profile: str, action: str, body: DutAction):
        if action not in DUT_ACTIONS:
            raise HTTPException(404, f"unknown action {action}")
        if action in ("push", "remove") and engine.task and not engine.task.done():
            raise HTTPException(409, "a run is active")
        return await asyncio.to_thread(_dut_action, profile, action, body)

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

    def _dut_action(profile: str, action: str, body: DutAction) -> dict:
        """Same as tools/tgen_dut.py, for the UI's DUT buttons (U12-U16)."""
        from tgen import inventory
        from tgen.dut import DutError, make
        from tgen_dut import DEFAULT_ROLE, plan_meta

        p = inventory.load().get(profile)
        if not p:
            raise HTTPException(404, f"unknown profile {profile}")
        adapter, ip = p["adapter"], p["peer_ip"]
        role = body.role or p.get("role") or DEFAULT_ROLE.get(adapter, "route_server")
        asn = p.get("asn", 64500)
        dut = make(adapter, p.get("ssh_host") or ip, p.get("user"), port=p.get("ssh_port", 22),
                   password=body.password or os.environ.get(inventory.password_env(profile)),
                   ssh_legacy=p.get("ssh_legacy", False), mrai=p.get("mrai_s"))

        class A:  # what plan_meta() reads from the CLI arguments
            routers, seed, dut_ip, dut_asn = body.routers, 1, ip, asn
            config = str(cfg.raw.get("cluster_env") or REPO / "deploy" / "cluster.env")
        A.role = role
        out: dict = {"profile": profile, "action": action, "platform": dut.platform}
        try:
            if action == "preview":
                out["config"] = dut.render(plan_meta(A), ip, asn, role)
            elif action == "test":
                out["resources"] = dut.resources()
                out["tgen_neighbours"] = dut.count()
                with contextlib.suppress(DutError, NotImplementedError):
                    out["port"] = dut.interface_info(ip, p.get("interface"))
            elif action == "push":
                out["neighbours"] = dut.push(dut.render(plan_meta(A), ip, asn, role), body.routers)
            elif action == "remove":
                dut.remove()
                out["tgen_neighbours"] = dut.count()
            elif action == "status":
                out["sessions"], out["established"] = dut.sessions()
            out["ok"] = True
        except (DutError, OSError) as e:
            out.update(ok=False, error=str(e)[:1000])
        finally:
            close = getattr(dut, "close", None)
            if close:
                close()
        out["notes"] = dut.notes
        return out

    return app
