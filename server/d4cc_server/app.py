"""FastAPI application: ingest API, board API, live stream and UI."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import json
import time
from pathlib import Path

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import __version__, actions, board, db, settings
from .config import Config
from .health import Health, InvalidHeartbeat
from .hub import Hub
from .ingest import Ingestor, InvalidEvent

HERE = Path(__file__).parent
COOKIE = "d4cc_session"
MAX_BATCH = 100
PRUNE_INTERVAL = 3600


def create_app(cfg: Config | None = None) -> FastAPI:
    cfg = cfg or Config.from_env()
    cfg.validate()
    conn = db.connect(cfg.db_path)
    settings.load(conn, cfg)
    ingestor = Ingestor(conn)
    health = Health(conn, cfg)
    hub = Hub()
    templates = Jinja2Templates(directory=HERE / "templates")

    def check_health() -> None:
        if health.reconcile(time.time()):
            hub.publish("resync", None)

    async def health_loop():
        last_prune = 0.0
        while True:
            await asyncio.sleep(cfg.health_interval)
            try:
                check_health()
                if time.time() - last_prune > PRUNE_INTERVAL:
                    last_prune = time.time()
                    counts = actions.prune(conn, cfg.retention, last_prune)
                    if any(counts.values()):
                        print(f"retention: pruned {counts}")
            except Exception as exc:  # keep the loop alive
                print(f"maintenance failed: {exc!r}")

    @contextlib.asynccontextmanager
    async def lifespan(_app):
        task = asyncio.create_task(health_loop())
        try:
            yield
        finally:
            task.cancel()

    app = FastAPI(title="Dash4ClCo", version=__version__, docs_url=None, redoc_url=None, lifespan=lifespan)
    app.state.cfg, app.state.conn, app.state.hub, app.state.health = cfg, conn, hub, health
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")

    # -- auth ---------------------------------------------------------------

    def cookie_value() -> str:
        return hmac.new((cfg.admin_token or "").encode(), b"d4cc-admin-session", hashlib.sha256).hexdigest()

    def is_admin(request: Request) -> bool:
        if cfg.auth_disabled:
            return True
        return hmac.compare_digest(request.cookies.get(COOKIE, ""), cookie_value())

    def require_admin(request: Request) -> None:
        if not is_admin(request):
            raise HTTPException(401, "login required")

    def require_host(request: Request):
        auth = request.headers.get("authorization", "")
        if not auth.lower().startswith("bearer "):
            raise HTTPException(401, "missing bearer token")
        host = db.host_for_token(conn, auth[7:].strip())
        if host is None:
            raise HTTPException(401, "unknown or revoked token")
        return host

    # -- ingest -------------------------------------------------------------

    @app.post("/api/events", status_code=202)
    async def ingest(request: Request, host=Depends(require_host)):
        try:
            payload = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise HTTPException(400, "invalid JSON")
        events = payload if isinstance(payload, list) else [payload]
        if len(events) > MAX_BATCH:
            raise HTTPException(413, "too many events")
        received = time.time()
        touched: set[str] = set()
        errors = []
        for i, ev in enumerate(events):
            try:
                touched |= ingestor.apply(host["id"], ev, received).sessions
            except InvalidEvent as exc:
                errors.append({"index": i, "error": str(exc)})
        for sid in touched:
            data = board.get_session(conn, sid)
            if data:
                hub.publish("session", data)
        if touched:
            check_health()
        if errors and len(errors) == len(events):
            return JSONResponse({"accepted": 0, "errors": errors}, status_code=400)
        return {"accepted": len(events) - len(errors), "errors": errors}

    @app.post("/api/heartbeat", status_code=202)
    async def heartbeat(request: Request, host=Depends(require_host)):
        try:
            payload = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise HTTPException(400, "invalid JSON")
        try:
            health.heartbeat(host["id"], payload, time.time())
        except InvalidHeartbeat as exc:
            raise HTTPException(400, str(exc))
        hub.publish("host", board.get_host(conn, host["id"]))
        check_health()
        return {"ok": True}

    # -- read API -----------------------------------------------------------

    @app.get("/healthz")
    def healthz():
        conn.execute("SELECT 1")
        return {"ok": True, "version": __version__}

    @app.get("/api/board", dependencies=[Depends(require_admin)])
    def api_board():
        return board.board(conn, cfg)

    @app.get("/api/stream", dependencies=[Depends(require_admin)])
    async def stream(request: Request):
        queue = hub.subscribe()

        async def gen():
            try:
                yield "retry: 3000\n\n"
                while True:
                    try:
                        kind, data = await asyncio.wait_for(queue.get(), timeout=15)
                    except asyncio.TimeoutError:
                        yield ": ping\n\n"
                        continue
                    yield f"event: {kind}\ndata: {json.dumps(data)}\n\n"
            finally:
                hub.unsubscribe(queue)

        return StreamingResponse(
            gen(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # -- card actions (admin, JSON bodies) -----------------------------------

    async def json_body(request: Request) -> dict:
        if request.headers.get("content-type", "").split(";")[0].strip() != "application/json":
            raise HTTPException(415, "expected application/json")
        try:
            body = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise HTTPException(400, "invalid JSON")
        if not isinstance(body, dict):
            raise HTTPException(400, "expected an object")
        return body

    def run_action(fn, *args, sid: str | None = None):
        now = time.time()
        conn.execute("BEGIN IMMEDIATE")
        try:
            result = fn(conn, *args, now)
            conn.execute("COMMIT")
        except actions.NotFound as exc:
            conn.execute("ROLLBACK")
            raise HTTPException(404, str(exc))
        except actions.Conflict as exc:
            conn.execute("ROLLBACK")
            raise HTTPException(409, str(exc))
        except Exception:
            conn.execute("ROLLBACK")
            raise
        check_health()
        if sid is None and result is not None and "session_id" in result.keys():
            sid = result["session_id"]
        if sid:
            data = board.get_session(conn, sid)
            if data:
                hub.publish("session", data)
        else:
            hub.publish("resync", None)
        return {"ok": True}

    @app.post("/api/sessions/{sid}/marker", dependencies=[Depends(require_admin)])
    async def marker_set(sid: str, request: Request):
        body = await json_body(request)
        return run_action(actions.set_marker, sid, body.get("note"), sid=sid)

    @app.delete("/api/sessions/{sid}/marker", dependencies=[Depends(require_admin)])
    def marker_clear(sid: str):
        return run_action(actions.clear_marker, sid, sid=sid)

    @app.post("/api/sessions/{sid}/notes", dependencies=[Depends(require_admin)])
    async def note_add(sid: str, request: Request):
        body = await json_body(request)
        return run_action(actions.add_note, sid, body.get("text"), sid=sid)

    @app.post("/api/sessions/{sid}/end", dependencies=[Depends(require_admin)])
    async def session_end(sid: str, request: Request):
        body = await json_body(request)
        return run_action(actions.mark_ended, sid, body.get("note"), sid=sid)

    @app.post("/api/sessions/{sid}/archive", dependencies=[Depends(require_admin)])
    async def session_archive(sid: str, request: Request):
        await json_body(request)
        return run_action(actions.archive, sid)

    @app.post("/api/sessions/{sid}/unarchive", dependencies=[Depends(require_admin)])
    async def session_unarchive(sid: str, request: Request):
        await json_body(request)
        return run_action(actions.unarchive, sid)

    @app.post("/api/alerts/{alert_id}/ack", dependencies=[Depends(require_admin)])
    async def alert_ack(alert_id: int, request: Request):
        body = await json_body(request)
        return run_action(actions.ack_alert, alert_id, body.get("note"))

    @app.get("/api/sessions/{sid}/history", dependencies=[Depends(require_admin)])
    def session_history(sid: str):
        try:
            data = actions.history(conn, sid)
        except actions.NotFound as exc:
            raise HTTPException(404, str(exc))
        data["session"] = board.get_session(conn, sid)
        return data

    @app.get("/api/archived", dependencies=[Depends(require_admin)])
    def archived_sessions():
        rows = conn.execute("SELECT * FROM sessions WHERE archived_at IS NOT NULL"
                            " ORDER BY archived_at DESC LIMIT 200").fetchall()
        return {"sessions": [board.session_dict(conn, r) for r in rows]}

    # -- settings -------------------------------------------------------------

    @app.get("/api/settings", dependencies=[Depends(require_admin)])
    def settings_get():
        return {"values": settings.current(cfg), "fields": settings.describe()}

    @app.put("/api/settings", dependencies=[Depends(require_admin)])
    async def settings_put(request: Request):
        body = await json_body(request)
        try:
            settings.save(conn, cfg, body)
        except settings.InvalidSetting as exc:
            raise HTTPException(422, str(exc))
        check_health()
        hub.publish("resync", None)
        return {"values": settings.current(cfg)}

    # -- UI -----------------------------------------------------------------

    @app.get("/login", response_class=HTMLResponse)
    def login_form(request: Request):
        return templates.TemplateResponse(request, "login.html", {"error": None, "version": __version__})

    @app.post("/login")
    def login(request: Request, token: str = Form(...)):
        if cfg.auth_disabled or hmac.compare_digest(token, cfg.admin_token or ""):
            resp = RedirectResponse("/", status_code=303)
            resp.set_cookie(COOKIE, cookie_value(), httponly=True, samesite="strict", max_age=30 * 86400)
            return resp
        return templates.TemplateResponse(request, "login.html", {"error": "Invalid token", "version": __version__},
                                          status_code=401)

    @app.post("/logout")
    def logout():
        resp = RedirectResponse("/login", status_code=303)
        resp.delete_cookie(COOKIE)
        return resp

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request):
        if not is_admin(request):
            return RedirectResponse("/login", status_code=303)
        return templates.TemplateResponse(
            request, "board.html", {"version": __version__, "auth": not cfg.auth_disabled}
        )

    @app.get("/settings", response_class=HTMLResponse)
    def settings_page(request: Request):
        if not is_admin(request):
            return RedirectResponse("/login", status_code=303)
        return templates.TemplateResponse(
            request, "settings.html", {"version": __version__, "auth": not cfg.auth_disabled}
        )

    @app.get("/favicon.ico")
    def favicon():
        return Response(status_code=204)

    return app
