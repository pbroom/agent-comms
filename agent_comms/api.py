"""HTTP JSON API + dashboard + MCP streamable HTTP, all in one localhost-only ASGI app."""

from __future__ import annotations

import ipaddress
import math
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal

from fastapi import Depends, FastAPI, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import StrictInt, BaseModel, ConfigDict

from . import capabilities, requests, attention, board_settings, dispatch, human_actions, issues, resolve, summary, unstick, weblogin
from .config import Settings
from .core import Board, BoardError, Conflict, Forbidden, Invalid, Principal
from .mcp_server import INSTRUCTIONS, build_mcp

DASHBOARD = Path(__file__).with_name("dashboard.html")
LOCAL_HOSTNAMES = {"127.0.0.1", "localhost", "::1", "testserver"}
CSRF_HEADER = "x-board-request"   # the dashboard sends "X-Board-Request: 1" on every request
PAGE_CSP = ("default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src 'self' data:; "
            "connect-src 'self'; frame-ancestors 'none'")
LINK_EXPIRED_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Link expired</title>
<style>body{font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;max-width:32rem;margin:15vh auto;
padding:0 16px;color:#1d1d1b;background:#f6f6f4}@media (prefers-color-scheme:dark){body{color:#ecebe6;background:#151514}}
code{font-family:ui-monospace,Menlo,monospace}</style></head>
<body><h1>Link expired</h1><p>Sign-in links work once and only for a minute. Run <code>board dashboard</code>
again (or use the menu bar app) to get a new one.</p></body></html>
"""


class Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CapabilitiesIn(Body):
    session_id: int | None = None
    capabilities: list[str]
    evidence: str
    ttl_seconds: int = 1800
    activity: Literal['idle', 'active', 'unknown'] = 'unknown'


class RequestRouteIn(Body):
    session_id: int | None = None
    recipient: str
    required_capabilities: list[str]
    expected_version: StrictInt


class RequestProgressIn(Body):
    session_id: int | None = None
    recipient: str
    state: Literal["queued", "started", "blocked", "finished"]
    reason: str = ""
    evidence_post_ids: list[StrictInt] | None = None
    expected_version: StrictInt | None = None
    completion: dict | None = None


class SessionIn(Body):
    project: str
    worktree: str | None = None
    resume_session_id: int | None = None
    dispatch_run_id: str | None = None


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
    continuation_scope: dict | None = None


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
    decision_question: dict | None = None   # asks the human to choose: same schema as an issue's
    continuation: dict | None = None
    session_id: int | None = None


class IssueIn(Body):
    decision_question: dict | None = None
    title: str
    body: str
    thread_id: int
    post_id: int | None = None
    needs_human: bool = True
    session_id: int | None = None


class IssueLinkIn(Body):
    thread_id: int
    post_id: int | None = None
    session_id: int | None = None


class IssueCommentIn(Body):
    decision_question: dict | None = None
    body: str
    kind: Literal["comment", "evidence", "proposal", "request"] = "comment"
    session_id: int | None = None


class IssueDecisionIn(Body):
    selected_option_id: str | None = None
    expected_question_version: int | None = None
    body: str | None = None
    thread_ids: list[int]
    outcome: Literal["answered", "approved", "declined"] = "answered"
    session_id: int | None = None


class IssueResolutionIn(Body):
    body: str
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


class LoginLinkIn(Body):
    next: str = "/"


class AttentionResolutionIn(Body):
    session_id: int | None = None
    reason: str
    evidence_post_ids: list[StrictInt]


class ResolveIn(Body):
    action: Literal["approve", "approve_launch", "reject", "not_now", "reply", "choose", "ask_options"]
    text: str | None = None
    option_id: str | None = None   # choose: one of the post's decision_question option ids
    note: str | None = None        # choose: the human's optional note (<= 1 KB)


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

    def port_of(request: Request) -> int:
        return request.url.port or settings.port

    def session_cookie(request: Request) -> str | None:
        return request.cookies.get(weblogin.cookie_name(port_of(request)))

    def check_csrf(request: Request) -> None:
        # A cookie rides along on any request the browser makes, so a cookie-authenticated request must also show
        # it came from this dashboard: a custom header (another origin cannot add one without a CORS preflight,
        # which this server never grants) and, when the browser sends one, an Origin equal to this server's.
        if request.headers.get(CSRF_HEADER) != "1":
            raise Forbidden("cookie-authenticated requests need the X-Board-Request: 1 header")
        origin = request.headers.get("origin")
        if origin is not None and origin != f"http://{request.headers.get('host', '')}":
            raise Forbidden("cross-origin request refused")

    def set_session_cookie(response: Response, request: Request, secret: str, max_age: int) -> None:
        # Not Secure: Safari does not keep Secure cookies set over http://127.0.0.1 (DESIGN_NOTES "Dashboard sign-in").
        response.set_cookie(weblogin.cookie_name(port_of(request)), secret, max_age=max_age, path="/",
                            httponly=True, samesite="strict")

    def clear_session_cookie(response: Response, request: Request) -> None:
        response.delete_cookie(weblogin.cookie_name(port_of(request)), path="/", httponly=True, samesite="strict")

    def principal(request: Request, response: Response) -> Principal:
        # A bearer header always wins, and is all that /mcp and the ChatGPT gateway accept. The session cookie is
        # the dashboard's: it resolves to the human only, and only for the /api routes in this file.
        auth = request.headers.get("authorization")
        secret = session_cookie(request)
        if auth is not None or not secret:
            request.state.auth = "bearer"
            token = auth[7:].strip() if auth and auth.lower().startswith("bearer ") else None
            return board.authenticate(token)
        check_csrf(request)
        p, renewed = weblogin.authenticate_session(board, secret)
        request.state.auth = "cookie"
        if renewed is not None:
            set_session_cookie(response, request, secret, renewed)
        return p

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
        return HTMLResponse(DASHBOARD.read_text(), headers={"Content-Security-Policy": PAGE_CSP,
                                                           "Cache-Control": "no-store"})

    # ---------------------------------------------------------------- dashboard sign-in (see weblogin.py)
    @app.post("/api/login-links")
    def create_login_link(request: Request, body: LoginLinkIn | None = None, p: Principal = P):
        # Bearer only: a cookie session must not be able to mint itself a fresh session past its absolute limit.
        if request.state.auth != "bearer":
            raise Forbidden("sign-in links need the human bearer token")
        code = weblogin.create_link(board, p, body.next if body else "/")
        # Contract (menu bar app, CLI): exactly http://127.0.0.1:<port>/login/<code>, no query and no fragment.
        # The cookie is per host, so every sign-in lands on 127.0.0.1. Only a board bound to ::1 differs.
        host = "[::1]" if settings.host == "::1" else "127.0.0.1"
        return {"url": f"http://{host}:{port_of(request)}/login/{code}",
                "expires_in_seconds": weblogin.LINK_TTL_SECONDS}

    @app.get("/login/{code}", include_in_schema=False)
    def login(code: str, request: Request):
        headers = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}
        try:
            secret, target, max_age = weblogin.redeem_link(
                board, code, weblogin.browser_label(request.headers.get("user-agent")))
        except BoardError:
            return HTMLResponse(LINK_EXPIRED_PAGE, status_code=410,
                                headers=headers | {"Content-Security-Policy": PAGE_CSP})
        # 303 to the same-origin path stored with the code (never taken from this request); a #fragment is kept.
        response = Response(status_code=303, headers=headers | {"Location": target})
        set_session_cookie(response, request, secret, max_age)
        return response

    @app.post("/api/web-sessions/logout")
    def logout(request: Request):
        # Works with an expired session too. The CSRF check keeps other pages from signing you out.
        secret = session_cookie(request)
        signed_out = False
        if secret:
            check_csrf(request)
            signed_out = weblogin.sign_out(board, secret)
        response = JSONResponse({"signed_out": signed_out})
        clear_session_cookie(response, request)
        return response

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
        out = board.snapshot(p, closed_threads=closed)
        if p.is_human:
            # For the thread status dots: dispatcher runs in progress. Server-stamped metadata only, and only
            # while the dispatcher loop is alive (a crashed loop leaves stale "running" records behind).
            config, _ = dispatch_config()
            live = dispatch.loop_status(board, config).get("running", False)
            out["active_runs"] = [{"thread_id": r.get("thread_id"), "agent": r.get("agent"), "run_id": r.get("run_id"),
                                   "started_at": r.get("started_at")}
                                  for r in dispatch.list_runs(board, p, 20)
                                  if live and r.get("status") in ("starting", "running")]
            # For "Approve & launch" in the Needs you callout: agents with a runner and no live session.
            out["launchable_agents"] = human_actions.launchable_agents(board, config)
        return out

    # ---------------------------------------------------------------- sessions
    @app.post("/api/sessions")
    def register(body: SessionIn, p: Principal = P):
        return board.register_session(p, body.project, body.worktree, body.resume_session_id, dispatch_run_id=body.dispatch_run_id)

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

    @app.post("/api/threads/{thread_id}/unstick")
    def unstick_thread(thread_id: int, p: Principal = P):
        # Posts a fixed request as the human to the agents the thread is waiting on, after approving a one-shot
        # dispatcher rule for them (see unstick.py). Human only (core checks). The agents come from the database.
        return unstick.unstick(board, p, thread_id, dispatch_config()[0])

    @app.put("/api/threads/{thread_id}/summary")
    def set_summary(thread_id: int, body: SummaryIn, request: Request, p: Principal = P):
        return board.set_summary(p, sid(p, request, body.session_id), thread_id, body.summary)

    @app.get("/api/threads/{thread_id}/posts")
    def list_posts(thread_id: int, since_seq: int = 0, limit: int = 100, p: Principal = P):
        return board.list_posts(p, thread_id, since_seq, limit)

    # ----------------------------------------------------------- shared issues
    @app.get("/api/issues")
    def list_issues(project: str | None = None, status: Literal["open", "resolved"] | None = None,
                    query: str | None = None, thread_id: int | None = None, p: Principal = P):
        return issues.list_issues(board, p, project=project, status=status, query=query, thread_id=thread_id)

    @app.post("/api/issues")
    def create_issue(body: IssueIn, request: Request, p: Principal = P):
        return issues.create_issue(board, p, sid(p, request, body.session_id),
                                   **body.model_dump(exclude={"session_id"}))

    @app.get("/api/issues/{issue_id}")
    def get_issue(issue_id: int, p: Principal = P):
        return issues.get_issue(board, p, issue_id)

    @app.post("/api/issues/{issue_id}/links")
    def link_issue(issue_id: int, body: IssueLinkIn, request: Request, p: Principal = P):
        return issues.link_issue(board, p, sid(p, request, body.session_id), issue_id,
                                 **body.model_dump(exclude={"session_id"}))

    @app.post("/api/issues/{issue_id}/comments")
    def comment_issue(issue_id: int, body: IssueCommentIn, request: Request, p: Principal = P):
        return issues.comment_issue(board, p, sid(p, request, body.session_id), issue_id,
                                    **body.model_dump(exclude={"session_id"}))

    @app.post("/api/issues/{issue_id}/decisions")
    def decide_issue(issue_id: int, body: IssueDecisionIn, request: Request, p: Principal = H):
        return issues.decide_issue(board, p, sid(p, request, body.session_id), issue_id,
                                   **body.model_dump(exclude={"session_id"}))

    @app.post("/api/issues/{issue_id}/resolve")
    def resolve_issue(issue_id: int, body: IssueResolutionIn, request: Request, p: Principal = H):
        return issues.resolve_issue(board, p, sid(p, request, body.session_id), issue_id, body.body)

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

    @app.post("/api/posts/{post_id}/attention/resolve")
    def close_attention(post_id: int, body: AttentionResolutionIn, request: Request, p: Principal = P):
        return attention.close_attention(board, p, sid(p, request, body.session_id), post_id,
                                         body.reason, body.evidence_post_ids)

    @app.post("/api/posts/{post_id}/resolve")
    def resolve_post(post_id: int, body: ResolveIn, p: Principal = P):
        # The Needs you callout's one-click actions: posts fixed text (or the human's reply) as the human to the
        # post's author; approve_launch first approves a one-shot dispatcher rule for it (see resolve.py).
        return resolve.resolve(board, p, post_id, body.action, body.text, dispatch_config()[0],
                               option_id=body.option_id, note=body.note)

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

    @app.get("/api/web-sessions")
    def web_sessions(request: Request, p: Principal = H):
        current = session_cookie(request) if request.state.auth == "cookie" else None
        sliding, absolute = weblogin.lifetimes(board.s)
        return {"sessions": weblogin.list_sessions(board, p, current), "session_days": sliding // weblogin.DAY,
                "session_max_days": absolute // weblogin.DAY}

    @app.post("/api/web-sessions/revoke-all")
    def revoke_all_web_sessions(request: Request, response: Response, p: Principal = H):
        n = weblogin.revoke_all(board, p)
        if request.state.auth == "cookie":
            clear_session_cookie(response, request)
        return {"revoked": n}

    @app.post("/api/web-sessions/{session_id}/revoke")
    def revoke_web_session(session_id: str, request: Request, response: Response, p: Principal = H):
        out = weblogin.revoke_session(board, p, session_id)
        if request.state.auth == "cookie" and weblogin.session_id_of(session_cookie(request)) == session_id:
            clear_session_cookie(response, request)
        return out

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

    @app.post("/api/sessions/capabilities")
    def register_capabilities(body: CapabilitiesIn, request: Request, p: Principal = P):
        return capabilities.register(board, p, sid(p, request, body.session_id),
                                     body.capabilities, body.evidence, body.ttl_seconds, body.activity)

    @app.post("/api/posts/{post_id}/request-route")
    def route_request(post_id: int, body: RequestRouteIn, request: Request, p: Principal = P):
        return capabilities.route(board, p, sid(p, request, body.session_id), post_id,
                                  body.recipient, body.required_capabilities, body.expected_version)

    @app.post("/api/posts/{post_id}/request-progress")
    def request_progress(post_id: int, body: RequestProgressIn, request: Request, p: Principal = P):
        values = body.model_dump(exclude={"session_id"})
        return requests.progress(board, p, sid(p, request, body.session_id), post_id, **values)

    @app.get("/api/posts/{post_id}/requests/{recipient}/history")
    def request_history(post_id: int, recipient: str, p: Principal = P):
        return {"events": requests.history(board, p, post_id, recipient)}

    from . import browser_api
    browser_api.install(app, board, principal, sid)
    app.mount("/", mcp_app)  # serves /mcp; registered last so the routes above win
    return app


def serve(host: str | None = None, port: int | None = None) -> None:
    import uvicorn

    settings = Settings.load()
    host = host or settings.host
    if host not in ("127.0.0.1", "localhost", "::1"):
        raise SystemExit("agent-comms v1 only binds to localhost")
    uvicorn.run(create_app(settings=settings), host=host, port=port or settings.port, log_level="info")
