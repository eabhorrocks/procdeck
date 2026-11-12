"""HTTP API + static dashboard. Binds to 127.0.0.1 only; every /api call needs the token header."""
from __future__ import annotations

import os
import threading
import time
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from .config import WEB_DIR, config_path, validate_config_text
from .daemon import Daemon
from .spec import SpecError, parse as parse_spec
from . import condaenv


def create_app(d: Daemon, token: str, port: int, on_shutdown=None) -> FastAPI:
    app = FastAPI(title="procdeck", docs_url=None, redoc_url=None, openapi_url=None)
    allowed_origins = {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}

    @app.middleware("http")
    async def guard(request: Request, call_next):
        if request.url.path.startswith("/api"):
            origin = request.headers.get("origin")
            if origin and origin not in allowed_origins:
                return JSONResponse({"detail": "origin not allowed"}, status_code=403)
            if request.headers.get("x-procdeck-token") != token:
                return JSONResponse({"detail": "missing or bad token"}, status_code=403)
        return await call_next(request)

    def run_or_404(ref: str) -> dict:
        r = d.store.find_run(ref)
        if not r:
            raise HTTPException(404, f"no run {ref!r}")
        return r

    # ------------------------------------------------------------- state
    @app.get("/api/state")
    def state():
        return d.state()

    @app.get("/api/top")
    def top(n: int = 20):
        s = d.state()
        groups = [{k: v for k, v in g.items() if k not in ("children", "cpu_hist")} for g in s["groups"][:n]]
        return {"system": s["system"], "alert": s["alert"], "admission": s["admission"], "groups": groups}

    # ------------------------------------------------------------- runs
    @app.post("/api/submit")
    def submit(body: dict = Body(...)):
        spec_path = body.get("spec_path")
        text = body.get("spec_text")
        if spec_path:
            p = Path(spec_path)
            if not p.is_file():
                raise HTTPException(400, f"spec file not found: {spec_path}")
            text = p.read_text(encoding="utf-8")
        if not text:
            raise HTTPException(400, "give spec_path or spec_text")
        try:
            plan = parse_spec(text, spec_path, d.profiles, env_exists=condaenv.env_exists)
        except SpecError as e:
            return JSONResponse({"ok": False, "errors": e.errors}, status_code=422)
        with d.op_lock:
            if body.get("dry_run"):
                return {"ok": True, "dry_run": True, **d.runner.dry_run(plan)}
            run_id = d.runner.submit(plan, body.get("submitted_by") or "")
        return {"ok": True, "run_id": run_id, "name": plan.name, "steps": len(plan.steps), "warnings": plan.warnings}

    @app.get("/api/runs")
    def runs(limit: int = 30):
        return d.runner.runs_view(limit)

    @app.get("/api/runs/{ref}")
    def run_detail(ref: str):
        r = run_or_404(ref)
        return d.runner.run_detail(r["id"])

    @app.get("/api/runs/{ref}/log")
    def run_log(ref: str, key: str, lines: int = 200):
        r = run_or_404(ref)
        return {"run_id": r["id"], "key": key, "text": d.runner.log_tail(r["id"], key, lines)}

    @app.post("/api/runs/{ref}/action")
    def run_action(ref: str, body: dict = Body(...)):
        r = run_or_404(ref)
        a, key = body.get("action"), body.get("key")
        with d.op_lock:
            if a == "cancel":
                d.runner.cancel(r["id"], key)
            elif a == "pause":
                d.runner.pause(r["id"], True)
            elif a == "resume":
                d.runner.pause(r["id"], False)
            elif a == "retry":
                n = d.runner.retry(r["id"], key)
                return {"ok": True, "retried": n}
            elif a == "priority":
                d.runner.set_priority(r["id"], int(body.get("value", 0)))
            else:
                raise HTTPException(400, f"unknown action {a!r}")
        d._runs_t = 0
        return {"ok": True}

    @app.post("/api/admission")
    def admission(body: dict = Body(...)):
        d.runner.manual_pause = bool(body.get("paused"))
        return {"ok": True, "paused": d.runner.manual_pause}

    # ------------------------------------------------------------- processes
    @app.get("/api/procs/{pid}/info")
    def proc_info(pid: int):
        return d.ctl.info(pid)

    @app.post("/api/procs/{pid}/steer")
    def steer(pid: int, body: dict = Body(...)):
        try:
            if body.get("profile"):
                return d.ctl.apply_profile(pid, body["profile"], tree=body.get("tree", True))
            return d.ctl.steer(pid, priority=body.get("priority"), cores=body.get("cores"),
                               cpu_mode=body.get("cpu_mode"), cpu_value=body.get("cpu_value"),
                               mem_cap_gb=body.get("mem_cap_gb"), tree=body.get("tree", True))
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.post("/api/procs/{pid}/signal")
    def signal(pid: int, body: dict = Body(...)):
        a = body.get("action")
        if a not in ("suspend", "resume", "kill"):
            raise HTTPException(400, "action must be suspend, resume or kill")
        try:
            with d.op_lock:
                return d.ctl.signal(pid, a)
        except ValueError as e:
            raise HTTPException(404, str(e))

    # ------------------------------------------------------------- history / config
    @app.get("/api/history")
    def history(minutes: float = 60):
        since = time.time() - minutes * 60
        return {"system": d.store.sys_history(since), "jobs": d.store.top_jobs_history(since, 10),
                "scripts": d.store.script_stats()}

    @app.get("/api/scripts")
    def scripts(pattern: str | None = None):
        return d.store.script_stats(pattern)

    @app.get("/api/config/{name}")
    def get_config(name: str):
        try:
            p = config_path(name)
        except ValueError as e:
            raise HTTPException(404, str(e))
        return {"name": name, "path": str(p), "text": p.read_text(encoding="utf-8") if p.exists() else ""}

    @app.put("/api/config/{name}")
    def put_config(name: str, body: dict = Body(...)):
        try:
            p = config_path(name)
            validate_config_text(name, body.get("text", ""))
        except Exception as e:  # noqa: BLE001 - report YAML/validation errors to the editor
            raise HTTPException(422, str(e))
        p.write_text(body["text"], encoding="utf-8")
        d.reload_config()
        return {"ok": True}

    @app.get("/api/rules/log")
    def rules_log():
        return d.rules.log[-50:]

    @app.post("/api/shutdown")
    def shutdown():
        d.stop_event.set()
        if on_shutdown:
            threading.Timer(0.3, on_shutdown).start()
        return {"ok": True}

    if WEB_DIR.is_dir():
        app.mount("/", StaticFiles(directory=str(WEB_DIR), html=True), name="web")
    return app
