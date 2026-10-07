"""HTTP JSON API + dashboard + MCP streamable HTTP, all in one localhost-only ASGI app."""

from __future__ import annotations

import ipaddress
import math
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal

from fastapi import Depends, FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, ConfigDict

from . import board_settings, dispatch, summary
from .config import Settings
from .core import Board, BoardError, Conflict, Forbidden, Invalid, Principal
from .mcp_server import INSTRUCTIONS, build_mcp

DASHBOARD = Path(__file__).with_name("dashboard.html")
LOCAL_HOSTNAMES = {"127.0.0.1", "localhost", "::1", "testserver"}


class Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SessionIn(Body):
    project: str
    worktree: str | None = None
    resume_session_id: int | None = None


class ThreadIn(Body):
    title: str
    project: str | None = None
    session_id: int | None = None


class SummaryIn(Body):
    summary: str
    session_id: int | None = None


class Ref(Body):
    kind: Literal["file", "commit", "url", "artifact"]
    path: str
    rev: str | None = None


class TaskFields(Body):
    title: str
    acceptance: str = ""
    intends_files: list[str] = []
    depends_on: list[int] = []
    category: Literal["review", "implementation", "tests", "documentation"] | None = None


class PostIn(Body):
    body: str
    type: Literal["question", "proposal", "status", "finding", "handoff", "request", "decision"]
    thread_id: int | None = None
    new_thread_title: str | None = None
    to: list[str] = []
    needs_response: bool = False
    task_id: int | None = None
    refs: list[Ref] = []
    sealed: bool = False
    final: bool = False
    propose_task: TaskFields | None = None
    session_id: int | None = None


class TaskIn(TaskFields):
    thread_id: int
    session_id: int | None = None


class SessionOnly(Body):
    session_id: int | None = None
    note: str | None = None


class TransitionIn(Body):
    status: Literal["proposed", "accepted", "working", "blocked", "done", "declined"]
    note: str | None = None
    session_id: int | None = None


class GrantIn(Body):
    project: str
    category: Literal["review", "implementation", "tests", "documentation"]
    agents: list[str]
    purpose: str
    expires_at: float | None = None


class NotifyRuleIn(Body):
    events: list[str] | None = None
    project: str | None = None
    thread_id: int | None = None
    idle_minutes: int | None = None


class DispatchRuleIn(Body):
    thread_id: int
    agents: list[str]
    purpose: str
    max_launches: int
    expires_in_hours: float | None = None


class AckIn(Body):
    ack_through: int
    thread_id: int | None = None
    session_id: int | None = None


def create_app(board: Board | None = None, settings: Settings | None = None, *,
               mcp_instructions: str = INSTRUCTIONS) -> FastAPI:
    settings = settings or (board.s if board else Settings.load())
    board = board or Board(settings)
    mcp = build_mcp(board, "http", instructions=mcp_instructions)
    mcp_app = mcp.streamable_http_app(streamable_http_path="/mcp", host=settings.host)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        async with mcp.session_manager.run():
            yield

    app = FastAPI(title="agent-comms", lifespan=lifespan, docs_url="/api/docs", redoc_url=None,
                  openapi_url="/api/openapi.json")
    app.state.board = board

    @app.middleware("http")
    async def localhost_only(request: Request, call_next):
        client = request.client.host if request.client else ""
        try:
            loopback = client == "testclient" or ipaddress.ip_address(client).is_loopback
        except ValueError:
            loopback = False
        # Host check defeats DNS rebinding from a browser tab; bearer tokens are still required on top.
        host = request.headers.get("host", "")
        hostname = host[1:].split("]")[0] if host.startswith("[") else host.split(":")[0]
        if not loopback or hostname not in LOCAL_HOSTNAMES:
            return JSONResponse({"error": "forbidden", "message": "agent-comms only serves localhost"}, 403)
        return await call_next(request)

    @app.exception_handler(BoardError)
    async def board_error(_: Request, e: BoardError):
        return JSONResponse({"error": e.code, "message": e.message}, status_code=e.status)

    def principal(request: Request) -> Principal:
        auth = request.headers.get("authorization", "")
        token = auth[7:].strip() if auth.lower().startswith("bearer ") else None
        return board.authenticate(token)

    def sid(p: Principal, request: Request, given: int | None) -> int:
        if given is not None:
            return given
        h = request.headers.get("x-board-session")
        if h and h.isdigit():
            return int(h)
        if p.is_human:
            return board.human_session(p)
        raise Invalid("session_id is required (body field or X-Board-Session header); POST /api/sessions first")

    P = Depends(principal)

    def human(p: Principal = P) -> Principal:
        # Settings and admin routes: the human only. Core enforces this again on every call.
        if not p.is_human:
            raise Forbidden("only the human can manage board settings")
        return p

    H = Depends(human)

    def dispatch_config() -> tuple[dispatch.DispatchConfig, str | None]:
        try:
            return dispatch.DispatchConfig.from_dict(board.s.dispatch), None
        except ValueError as e:
            return dispatch.DispatchConfig(), str(e)

    # ---------------------------------------------------------------- misc
    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    def dashboard():
        return HTMLResponse(DASHBOARD.read_text(), headers={
            "Content-Security-Policy": "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
                                       "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'",
            "Cache-Control": "no-store"})

    @app.get("/api/whoami")
    def whoami(p: Principal = P):
        return {"name": p.name, "runtime": p.runtime, "is_human": p.is_human, "paused": board.is_paused(),
                "limits": board.limits()}

    @app.get("/api/summary")
    def board_summary(p: Principal = H):
        # For the human's menu bar app: counts and server-stamped ids only, never agent-written text.
        return summary.human_summary(board, p, dispatch_config()[0])

    @app.get("/api/needs-you")
    def needs_you(p: Principal = H):
        # The menu bar's "Needs you" submenu: the dashboard's items with a cleaned 80-character preview.
        return summary.needs_you_list(board, p)

    @app.get("/api/state")
    def state(closed: bool = False, p: Principal = P):
        return board.snapshot(p, closed_threads=closed)

    # ---------------------------------------------------------------- sessions
    @app.post("/api/sessions")
    def register(body: SessionIn, p: Principal = P):
        return board.register_session(p, body.project, body.worktree, body.resume_session_id)

    @app.post("/api/sessions/{session_id}/heartbeat")
    def heartbeat(session_id: int, p: Principal = P):
        return board.heartbeat(p, session_id)

    # ---------------------------------------------------------------- threads
    @app.get("/api/threads")
    def threads(project: str | None = None, status: Literal["open", "closed", "all"] = "open", p: Principal = P):
        return {"threads": board.list_threads(p, project, None if status == "all" else status)}

    @app.post("/api/threads")
    def create_thread(body: ThreadIn, request: Request, p: Principal = P):
        return board.create_thread(p, sid(p, request, body.session_id), body.title, body.project)

    @app.post("/api/threads/{thread_id}/close")
    def close_thread(thread_id: int, p: Principal = P):
        return board.set_thread_status(p, thread_id, "closed")

    @app.post("/api/threads/{thread_id}/reopen")
    def reopen_thread(thread_id: int, p: Principal = P):
        return board.set_thread_status(p, thread_id, "open")

    @app.put("/api/threads/{thread_id}/summary")
    def set_summary(thread_id: int, body: SummaryIn, request: Request, p: Principal = P):
        return board.set_summary(p, sid(p, request, body.session_id), thread_id, body.summary)

    @app.get("/api/threads/{thread_id}/posts")
    def list_posts(thread_id: int, since_seq: int = 0, limit: int = 100, p: Principal = P):
        return board.list_posts(p, thread_id, since_seq, limit)

    # ---------------------------------------------------------------- posts
    @app.post("/api/posts")
    def create_post(body: PostIn, request: Request, p: Principal = P):
        d: dict[str, Any] = body.model_dump(exclude={"session_id"})
        d["refs"] = [r.model_dump() for r in body.refs]
        return board.create_post(p, sid(p, request, body.session_id), **d)

    @app.get("/api/posts/{post_id}")
    def get_post(post_id: int, p: Principal = P):
        return board.get_post(p, post_id)

    @app.post("/api/posts/{post_id}/finalize")
    def finalize(post_id: int, p: Principal = P):
        return board.finalize(p, post_id)

    @app.post("/api/posts/{post_id}/unseal")
    def unseal(post_id: int, p: Principal = P):
        return board.unseal(p, post_id)

    @app.get("/api/updates")
    async def updates(request: Request, session_id: int | None = None, thread_id: int | None = None,
                      only: Literal["all", "addressed", "needs_response"] = "all", limit: int = 50,
                      history: bool = False, wait_seconds: int = 0, p: Principal = P):
        # async: wait_seconds > 0 long-polls without holding a worker thread (see Board.read_updates_async)
        s = await run_in_threadpool(sid, p, request, session_id)
        return await board.read_updates_async(p, s, thread_id=thread_id, only=only, limit=limit, history=history,
                                              wait_seconds=wait_seconds)

    @app.post("/api/updates/ack")
    def ack(body: AckIn, request: Request, p: Principal = P):
        return {"acked_through": board.ack(p, sid(p, request, body.session_id), body.ack_through, body.thread_id)}

    # ---------------------------------------------------------------- tasks
    @app.get("/api/tasks")
    def tasks(thread_id: int | None = None, project: str | None = None, open_only: bool = False, p: Principal = P):
        return {"tasks": board.list_tasks(p, thread_id, project, include_closed=not open_only)}

    @app.get("/api/tasks/{task_id}")
    def get_task(task_id: int, p: Principal = P):
        return board.get_task(p, task_id)

    @app.post("/api/tasks")
    def create_task(body: TaskIn, request: Request, p: Principal = P):
        f = body.model_dump(exclude={"session_id", "thread_id"})
        return board.create_task(p, sid(p, request, body.session_id), body.thread_id, **f)

    @app.post("/api/tasks/{task_id}/claim")
    def claim(task_id: int, request: Request, body: SessionOnly | None = None, p: Principal = P):
        return board.claim_task(p, sid(p, request, body.session_id if body else None), task_id)

    @app.post("/api/tasks/{task_id}/renew")
    def renew(task_id: int, request: Request, body: SessionOnly | None = None, p: Principal = P):
        return board.renew_task(p, sid(p, request, body.session_id if body else None), task_id)

    @app.post("/api/tasks/{task_id}/release")
    def release(task_id: int, request: Request, body: SessionOnly | None = None, p: Principal = P):
        return board.release_task(p, sid(p, request, body.session_id if body else None), task_id,
                                  body.note if body else None)

    @app.post("/api/tasks/{task_id}/transition")
    def transition(task_id: int, body: TransitionIn, request: Request, p: Principal = P):
        return board.transition_task(p, sid(p, request, body.session_id), task_id, body.status, body.note)

    @app.get("/api/grants")
    def grants(project: str | None = None, p: Principal = P):
        return {"grants": board.list_grants(p, project)}

    @app.post("/api/admin/grants")
    def create_grant(body: GrantIn, p: Principal = P):
        return board.create_grant(p, **body.model_dump())

    @app.post("/api/admin/grants/{grant_id}/revoke")
    def revoke_grant(grant_id: int, p: Principal = P):
        return board.revoke_grant(p, grant_id)

    # ---------------------------------------------------------------- settings page (human only)
    @app.get("/api/settings")
    def get_settings(p: Principal = H):
        return board_settings.get_settings(board, p)

    @app.put("/api/settings")
    def put_settings(changes: dict[str, Any], p: Principal = H):
        # A partial update of EDITABLE keys only, e.g. {"limits.daily_post_cap_per_agent": 100}; null removes the
        # board.local.toml override. Host, port, paths, runners, env and worktrees are refused (400).
        return board_settings.update_settings(board, p, changes)

    @app.get("/api/admin/notifications")
    def list_notifications(p: Principal = H):
        return {"deliverable": board_settings.notifier_deliverer(board).available(),
                "rules": board.list_notification_subscriptions(p)}

    @app.post("/api/admin/notifications")
    def add_notification(body: NotifyRuleIn, p: Principal = H):
        return board.subscribe_notifications(p, **body.model_dump())

    @app.post("/api/admin/notifications/{rule_id}/remove")
    def remove_notification(rule_id: int, p: Principal = H):
        return {"removed": board.unsubscribe_notifications(p, rule_id)}

    @app.post("/api/admin/notifications/test")
    def test_notification(p: Principal = H):
        from .notify import sample_notification

        deliverer = board_settings.notifier_deliverer(board)
        if not deliverer.available():
            raise Conflict("cannot notify on this machine (needs macOS with /usr/bin/osascript)")
        deliverer(sample_notification())
        return {"sent": True}

    @app.get("/api/admin/dispatch")
    def dispatch_overview(p: Principal = H):
        config, error = dispatch_config()
        return {"status": dispatch.loop_status(board, config),
                "rules": board.list_dispatch_rules(p),
                "runs": dispatch.list_runs(board, p, 10),
                # Read-only: what the files say now. A running dispatcher uses the runners it started with.
                "runners": config.runners, "env": config.env, "worktrees": config.worktrees,
                "risky_runners": {k: dispatch.risky_flags(t) for k, t in config.runners.items()
                                  if dispatch.risky_flags(t)},
                "config_error": error,
                "threads": [{"id": t["id"], "title": t["title"], "project": t["project"]}
                            for t in board.list_threads(p, status="open")],
                "agents": [{"name": r["name"], "runtime": r["runtime"]} for r in board.conn.execute(
                    "SELECT name, runtime FROM agents WHERE active = 1 AND is_human = 0 ORDER BY name")]}

    @app.post("/api/admin/dispatch/rules")
    def approve_workstream(body: DispatchRuleIn, p: Principal = H):
        expires_at = None
        if body.expires_in_hours is not None:
            if not math.isfinite(body.expires_in_hours) or not 0 < body.expires_in_hours <= 24 * 366:
                raise Invalid("expires_in_hours must be a number of hours between 0 and 8784")
            expires_at = board.now() + body.expires_in_hours * 3600
        return board.create_dispatch_rule(p, thread_id=body.thread_id, agents=body.agents, purpose=body.purpose,
                                          max_launches=body.max_launches, expires_at=expires_at)

    @app.post("/api/admin/dispatch/rules/{rule_id}/revoke")
    def revoke_workstream(rule_id: int, p: Principal = H):
        return board.revoke_dispatch_rule(p, rule_id)

    @app.post("/api/admin/dispatch/stop")
    def stop_dispatcher(p: Principal = H):
        # Sets the same flag as `board dispatch stop` and returns at once; the loop stops its agents and exits.
        # Cleaning up runs left by a dispatcher that already exited stays with the CLI.
        config, _ = dispatch_config()
        if not dispatch.loop_status(board, config)["running"]:
            return {"requested": False, "was_running": False,
                    "message": "the dispatcher is not running (`board dispatch stop` also cleans up runs an "
                               "earlier dispatcher left)"}
        dispatch.set_stop_flag(board, p)
        return {"requested": True, "was_running": True}

    # ---------------------------------------------------------------- admin (human only; enforced in core)
    @app.post("/api/admin/pause")
    def pause(p: Principal = P):
        return board.set_paused(p, True)

    @app.post("/api/admin/unpause")
    def unpause(p: Principal = P):
        return board.set_paused(p, False)

    app.mount("/", mcp_app)  # serves /mcp; registered last so the routes above win
    return app


def serve(host: str | None = None, port: int | None = None) -> None:
    import uvicorn

    settings = Settings.load()
    host = host or settings.host
    if host not in ("127.0.0.1", "localhost", "::1"):
        raise SystemExit("agent-comms v1 only binds to localhost")
    uvicorn.run(create_app(settings=settings), host=host, port=port or settings.port, log_level="info")
