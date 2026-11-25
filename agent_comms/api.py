"""HTTP JSON API + dashboard + MCP streamable HTTP, all in one localhost-only ASGI app."""

from __future__ import annotations

import ipaddress
import math
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal

from fastapi import Depends, FastAPI, Query, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import StrictBool, StrictInt, BaseModel, ConfigDict
from starlette.requests import HTTPConnection

from . import capabilities, requests, attention, board_settings, dispatch, human_actions, issues, resolve, summary, unstick, weblogin, workstreams
from .config import Settings
from .core import Board, BoardError, Conflict, Forbidden, Invalid, Principal
from .mcp_server import INSTRUCTIONS, RequestReplyIn, build_mcp

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


class NodeHelloIn(Body):
    projects: list[str]
    runners: list[str]
    max_concurrent: StrictInt = 1
    headless_browser: list[str] = []
    checkout_roots: list[str] = []
    access: dict[str, list[str]] = {}
    sessions: dict[str, StrictInt] | None = None    # {"max_live": n}: the node hosts sessions (hosted.py)
    session_host: dict[str, Any] | None = None      # {"tree": 1, "runners": {key: [words]}[, "dirs": {key: dir}]}
    homes: dict[str, str] | None = None             # runner key -> its account folder (agent_accounts.py), path only
    usage: Any = None   # runner key -> plan usage seen (account_pools.py): advisory, any shape; what is unusable is dropped


class NodeReportIn(Body):
    state: Literal['running', 'ended']
    pid: StrictInt | None = None
    exit_code: StrictInt | None = None
    error: str | None = None
    checkout: dict[str, Any] | None = None


class HostedEventIn(Body):
    kind: Literal['delivered', 'turn_started', 'turn_ended', 'approval_requested', 'ended', 'worktree_removed']
    message: StrictInt | None = None
    restarted_by: StrictInt | None = None   # turn_ended: the restart whose interrupt the provider said ended it
    ok: StrictBool | None = None
    delivered: StrictBool = True
    denials: list[str] = []
    meta: dict[str, Any] = {}
    detail: str = ""
    text: str = ""
    answered: StrictBool = False    # turn_ended: the provider itself ended the turn (session_host.HostEvent.answered)
    steer: Any = None   # turn_ended: the owning host's word on the steer into it (hosted_steer.extension: a malformed
                        # one is ignored there, never refused here)


class NodeSessionReportIn(Body):
    events: list[HostedEventIn] = []
    state: Literal['starting', 'idle', 'running', 'waiting', 'ended'] | None = None
    pid: StrictInt | None = None
    resume: StrictBool | None = None
    seq: StrictInt | None = None    # orders this node's reports on the session; an older status is ignored
    provider_id: str | None = None  # the provider's own id for the session (Codex's thread id), for the record
    access: str | None = None       # the access the session really has on the node (session_access.LEVELS)
    worktree: str | None = None     # where a child session's own worktree is on the node
    ceiling: str | None = None      # a root: the most a child may have under it now (full only once confirmed)
    checkout: str | None = None     # a root: the directory it runs in on the node
    modes: list[str] | None = None  # what the session's adapter can do with a turn in flight (restart)
    held: Any = None    # Claude steering: the checkpoint of N's held native end (hosted_steer.checkpoint)


class HostedStartIn(Body):
    project: str
    message: str
    agent: str | None = None
    model: str | None = None
    output_schema: str | None = None    # the JSON TEXT of a schema (output_schema.py decodes it strictly)


class HostedChildIn(Body):
    """A session started under another (hosted_tree.py). Words only: the node maps `access` and `effort` to flags."""
    message: str
    agent: str | None = None
    model: str | None = None
    effort: str | None = None
    title: str | None = None
    role: str | None = None
    isolation: str | None = None
    access: str | None = None
    request_id: str | None = None
    output_schema: str | None = None    # the JSON TEXT of a schema (output_schema.py decodes it strictly)


class HostedMessageIn(Body):
    message: str
    mode: Literal['auto', 'queue', 'steer', 'restart'] = 'queue'     # hosted.SEND_MODES
    request_id: str | None = None   # a retry with the same id returns the first call's message
    output_schema: str | None = None    # the JSON TEXT of a schema (output_schema.py decodes it strictly)


class WaitIn(Body):
    """An open call of a session the human opened himself (interactive_wake.py): `board wait`, run by its hook."""
    client_session_id: str
    projects: list[str]
    nonce: str
    client: str = "claude-code"
    wait: float = 0
    ack: str | None = None      # the id of the wake this process was offered: it has it


class SpawnGrantIn(Body):
    """The human lets agents start child sessions in a project (hosted_spawn.create_grant)."""
    project: str
    agents: list[str]
    access: str
    hours: float
    hosted_only: StrictBool = False     # only sessions known by their binding (hosted sessions) may start children


class AllowanceIn(Body):
    may_spawn: StrictBool | None = None
    share: StrictInt | None = None


class RequestRouteIn(Body):
    session_id: int | None = None
    recipient: str
    required_capabilities: list[str]
    expected_version: StrictInt


class RequestOwnershipIn(Body):
    session_id: StrictInt | None = None
    recipient: str
    expected_version: StrictInt


class RequestRepostIn(Body):
    session_id: StrictInt | None = None
    recipient: str
    expected_version: StrictInt
    target_thread_id: StrictInt


class RequestProgressIn(Body):
    session_id: int | None = None
    recipient: str
    state: Literal["queued", "started", "blocked", "finished"]
    reason: str = ""
    evidence_post_ids: list[StrictInt] | None = None
    expected_version: StrictInt | None = None
    completion: dict | None = None
    recover_blocked: bool = False
    disposition: Literal['completed', 'superseded'] | None = None


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
    kind: Literal["file", "commit", "url", "artifact", "output"]
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
    browser_requirement: dict | None = None
    answer_to: list[StrictInt] | None = None
    request_reply: RequestReplyIn | None = None
    idempotency_key: str | None = None
    # A prevention proposal for this Unstick/recovery request, or the owner's forward of a proposal (prevention.py)
    prevention_for: StrictInt | None = None
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
    delivery_agents: dict[str, str] | None = None
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
    status: Literal["proposed", "accepted", "working", "blocked", "done", "declined"] | None = None
    note: str | None = None
    depends_on: list[StrictInt] | None = None   # replaces the task's dependencies (awaiting.py); [] clears them
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


class InboxPolicyIn(Body):
    scope: Literal['project', 'all_projects'] = 'project'
    project: str | None = None
    daily_launch_limit: StrictInt | None = None   # None: no daily limit
    include_existing: StrictBool = False
    pickup_grace_seconds: StrictInt = 120


class DeliveryPolicyIn(Body):
    agent: str
    scope: Literal['project', 'all_projects'] = 'project'
    project: str | None = None
    daily_launch_limit: StrictInt | None = None   # None: no daily limit
    pickup_grace_seconds: StrictInt = 120


class LoginLinkIn(Body):
    next: str = "/"


class AttentionResolutionIn(Body):
    session_id: int | None = None
    reason: str
    evidence_post_ids: list[StrictInt]


class ResolveIn(Body):
    delivery_agent: str | None = None
    action: Literal["approve", "approve_launch", "reject", "not_now", "reply", "choose", "ask_options"]
    text: str | None = None
    option_id: str | None = None   # choose: one of the post's decision_question option ids
    note: str | None = None        # choose: the human's optional note (<= 1 KB)


class DeliveryResetIn(Body):
    expected_version: StrictInt


class AckIn(Body):
    ack_through: int
    thread_id: int | None = None
    session_id: int | None = None


def is_local(request: HTTPConnection) -> bool:
    client = request.client.host if request.client else ""
    try:
        loopback = client == "testclient" or ipaddress.ip_address(client).is_loopback
    except ValueError:
        loopback = False
    # Host check defeats DNS rebinding from a browser tab; bearer tokens are still required on top.
    host = request.headers.get("host", "")
    hostname = host[1:].split("]")[0] if host.startswith("[") else host.split(":")[0]
    return loopback and hostname in LOCAL_HOSTNAMES


class LocalhostOnly:
    """Every HTTP request must come from a loopback address and name a local Host, or it is answered 403 here.
    Plain ASGI, not `@app.middleware("http")` (Starlette's BaseHTTPMiddleware) as before: behind that a route's
    `request.is_disconnected()` never turned true, so a long-poll went on after its caller had hung up (DESIGN_NOTES
    "Long-poll disconnects"). Here receive and send pass through untouched. Other scopes (lifespan) pass, as before."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "http" and not is_local(HTTPConnection(scope)):
            response = JSONResponse({"error": "forbidden", "message": "agent-comms only serves localhost"}, 403)
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


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
    app.add_middleware(LocalhostOnly)

    @app.exception_handler(BoardError)
    async def board_error(_: Request, e: BoardError):
        # Machine-readable extras some errors carry (recovery.RecoveryWait: blocker_kind, retry, recovery_wait).
        extra = {k: v for k, v in (getattr(e, "details", None) or {}).items() if k not in ("error", "message")}
        return JSONResponse({"error": e.code, "message": e.message, **extra}, status_code=e.status)

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
            p = board.authenticate(token)
            if (p.runtime == "node" and not p.is_human and not request.url.path.startswith("/api/nodes/")
                    and request.url.path != "/api/whoami"):
                raise Forbidden("a node identity may only use /api/nodes/* (and /api/whoami)")   # nodes.py
            return p
        check_csrf(request)
        p, renewed = weblogin.authenticate_session(board, secret)
        request.state.auth = "cookie"
        if renewed is not None:
            set_session_cookie(response, request, secret, renewed)
        return p

    def sid(p: Principal, request: Request, given: int | None) -> int:
        if given is not None:
            return guarded(p, request, given)
        h = request.headers.get("x-board-session")
        if h and h.isdigit():
            return guarded(p, request, int(h))
        if p.is_human:
            return board.human_session(p)
        raise Invalid("session_id is required (body field or X-Board-Session header); POST /api/sessions first")

    def guarded(p: Principal, request: Request, session_id: int) -> int:
        # A board session registered with a hosted session's binding is that session's alone, here as over MCP: its
        # use needs the same binding, verified (hosted_spawn.check_board_session).
        if not p.is_human:
            from . import hosted_spawn
            hosted_spawn.check_board_session(board.conn, session_id, hosted_spawn.Ident.from_headers(request.headers),
                                             p.name)
        return session_id

    P = Depends(principal)

    def human(p: Principal = P) -> Principal:
        # Settings and admin routes: the human only. Core enforces this again on every call.
        if not p.is_human:
            raise Forbidden("only the human can manage board settings")
        return p

    H = Depends(human)

    def dispatch_config() -> tuple[dispatch.DispatchConfig, str | None]:
        try:
            return dispatch.DispatchConfig.from_dict(board.s.dispatch, settings=board.s), None
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
                "limits": board.limits(), "configuration": board.configuration_status(p)}

    @app.get("/api/configuration")
    def configuration_status(p: Principal = P):
        return board.configuration_status(p)

    @app.post("/api/configuration/refresh")
    def refresh_configuration(p: Principal = H):
        return board.refresh_configuration(p)

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
                                  if live and r.get("status") in dispatch.ACTIVE]
            # For "Approve & launch" in the Needs you callout: agents with a runner and no live session.
            out["launchable_agents"] = human_actions.launchable_agents(board, config)
            # For the thread status dots: automatic recoveries pending or escalated to the human (autorecover.py).
            from . import autorecover
            out["auto_recovery"] = autorecover.list_records(board, p)
            # The Sessions tab's count: hosted sessions that have not ended (hosted_lineage.py).
            from . import hosted_lineage
            out["hosted_live"] = hosted_lineage.live_count(board)
        return out

    # ---------------------------------------------------------------- sessions
    @app.post("/api/sessions")
    def register(body: SessionIn, request: Request, p: Principal = P):
        from . import hosted_spawn
        who, on = hosted_spawn.Ident.from_headers(request.headers), hosted_spawn.enabled(board)
        rid = body.resume_session_id
        # As over MCP, in register's own transaction: no resuming another hosted session's board session; a
        # connection that proves to be a hosted session binds this one to it.
        return board.register_session(
            p, body.project, body.worktree, rid, dispatch_run_id=body.dispatch_run_id,
            on_session=lambda c, s: (hosted_spawn.check_resume(c, rid, who, p.name) if rid is not None
                                     else None, hosted_spawn.bind_in_register(c, p, s, who, board.now(), on)))

    @app.post("/api/sessions/{session_id}/heartbeat")
    def heartbeat(session_id: int, request: Request, p: Principal = P):
        return board.heartbeat(p, guarded(p, request, session_id))

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
        if body.request_reply is not None:
            d["request_reply"] = body.request_reply.model_dump(exclude_none=True)
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
                               option_id=body.option_id, note=body.note, delivery_agent=body.delivery_agent)

    @app.post("/api/posts/{post_id}/continuation/reset-delivery")
    def reset_continuation_delivery(post_id: int, body: DeliveryResetIn, p: Principal = H):
        # Human only: retry a fallback delivery whose worker failed or exited before registering (workstreams.py).
        return workstreams.reset_delivery(board, p, board.human_session(p), post_id, body.expected_version)

    @app.post("/api/posts/{post_id}/unseal")
    def unseal(post_id: int, p: Principal = P):
        return board.unseal(p, post_id)

    @app.get("/api/updates")
    async def updates(request: Request, session_id: int | None = None, thread_id: int | None = None,
                      only: Literal["all", "addressed", "needs_response"] = "all", limit: int = 50,
                      history: bool = False, wait_seconds: int = 0,
                      post_ids: list[int] | None = Query(None, description=(
                          "Read exactly these posts (1-20 ids, repeat the parameter: post_ids=1&post_ids=2) as far "
                          "as you may see them, without touching your cursor. Not combinable with the other filters.")),
                      p: Principal = P):
        # async: wait_seconds > 0 long-polls without holding a worker thread (see Board.read_updates_async)
        s = await run_in_threadpool(sid, p, request, session_id)
        return await board.read_updates_async(p, s, thread_id=thread_id, only=only, limit=limit, history=history,
                                              wait_seconds=wait_seconds, post_ids=post_ids)

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
        return board.update_task(p, sid(p, request, body.session_id), task_id, body.status, body.note,
                                 body.depends_on)

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
                "inbox_policies": board.list_inbox_policies(p, include_inactive=True),
                "delivery_policies": board.list_delivery_policies(p, include_inactive=True),
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

    @app.post("/api/admin/dispatch/inbox-policies")
    def create_inbox_policy(body: InboxPolicyIn, p: Principal = H):
        return board.create_inbox_policy(p, **body.model_dump())

    @app.post("/api/admin/dispatch/inbox-policies/{policy_id}/revoke")
    def revoke_inbox_policy(policy_id: int, p: Principal = H):
        return board.revoke_inbox_policy(p, policy_id)

    @app.post("/api/admin/dispatch/delivery-policies")
    def create_delivery_policy(body: DeliveryPolicyIn, p: Principal = H):
        return board.create_delivery_policy(p, **body.model_dump())

    @app.post("/api/admin/dispatch/delivery-policies/{policy_id}/revoke")
    def revoke_delivery_policy(policy_id: int, p: Principal = H):
        return board.revoke_delivery_policy(p, policy_id)

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

    @app.get("/api/brief")
    def brief(p: Principal = P, project: list[str] = Query(default=[]), after_seq: int | None = None):
        # The session-start hook's counts (Board.brief) for agents on other machines (remote.py).
        if not project or len(project) > 20 or not all(isinstance(x, str) and x.startswith("/") for x in project):
            raise Invalid("give 1 to 20 absolute project paths")
        return board.brief(p, project, after_seq=after_seq)

    @app.post("/api/nodes/hello")
    def node_hello(body: NodeHelloIn, p: Principal = P):
        from . import nodes
        return nodes.hello(board, p, body.projects, body.runners, body.max_concurrent,
                           body.headless_browser, body.checkout_roots, body.access, body.sessions,
                           session_host=body.session_host, homes=body.homes, usage=body.usage)

    @app.get("/api/nodes/assignments")
    def node_assignments(p: Principal = P):
        from . import nodes
        return nodes.assignments(board, p)

    @app.post("/api/nodes/runs/{run_id}")
    def node_report(run_id: str, body: NodeReportIn, p: Principal = P):
        from . import nodes
        return nodes.report(board, p, run_id, body.state, body.pid, body.exit_code, body.error, body.checkout)

    # Hosted sessions, the node's side (hosted.py): inside the node-identity gate with the routes above (`principal`
    # lets a node identity reach only /api/nodes/*, and hosted.py refuses everyone but a node here).
    def waiter_gone(request: Request):
        # A waiting call ends early when its caller hung up or this server is stopping (serve() sets `stopping`):
        # a node sits in such a wait whenever it has nothing to do, and must not hold up a restart of the hub.
        async def gone() -> bool:
            stopping = getattr(app.state, "stopping", None)
            return bool(stopping and stopping()) or await request.is_disconnected()
        return gone

    @app.get("/api/nodes/sessions")
    async def node_sessions(request: Request, wait: float = 0, seen: str | None = None, tree: int = 0,
                            applied: str | None = None, bound: float | None = None, p: Principal = P):
        # async: wait > 0 long-polls without holding a worker thread (as /api/updates does)
        # tree: the node says it speaks the child-session protocol; without it no child session is listed to it.
        from . import hosted
        # tree 2: it also speaks the spawn protocol (bindings, interactive children, cancel: hosted_tree.node_spawns)
        # applied: the version of the listing its session loop last applied; bound: how long it may take to stop a
        # session's process after the hub last heard from it (hosted.may_still_run).
        out = await hosted.node_sessions_wait(board, p, wait, seen, waiter_gone(request),
                                              tree=max(0, min(int(tree), 1000)), applied=applied, bound=bound)
        if tree >= 2:
            # A successful call of the session loop (hosted.last_able). A database write: in a worker thread, as
            # the listing's own reads are (hosted._poll), never on the event loop.
            await run_in_threadpool(hosted.note_contact, board, p)
        return out

    @app.post("/api/nodes/sessions/{session_id}")
    def node_session_report(session_id: str, body: NodeSessionReportIn, p: Principal = P):
        from . import hosted
        out = hosted.report(board, p, session_id, [e.model_dump() for e in body.events],
                            body.state, body.pid, body.resume, body.seq, provider_id=body.provider_id,
                            access=body.access, worktree=body.worktree, ceiling=body.ceiling,
                            checkout=body.checkout, modes=body.modes, held=body.held)
        hosted.note_contact(board, p)           # whatever path it took: a successful call of the session loop
        return out

    # A session the human opened himself waits here to be woken (interactive_wake.py): any agent, for its own
    # conversation. Off unless [dispatch.sessions.interactive] wake = true; then it answers {"state": "off"}.
    @app.post("/api/wait")
    async def wake_wait(request: Request, body: WaitIn, p: Principal = P):
        from . import interactive_wake
        return await interactive_wake.wait(board, p, body.model_dump(), waiter_gone(request))

    # Hosted sessions, the human's side: only the human starts, messages, reads or closes one (hosted.py checks on
    # every call; a node identity never gets past `principal`). The reply text is returned here and nowhere else.
    @app.get("/api/hosted-sessions")
    def hosted_sessions(all: bool = False, p: Principal = P):
        from . import hosted
        return hosted.list_sessions(board, p, include_ended=all)

    @app.get("/api/hosted-trees")
    def hosted_trees(all: bool = False, p: Principal = P):
        # The dashboard's lineage view (hosted_lineage.py): every session tree, read-only, human only.
        from . import hosted_lineage
        return hosted_lineage.trees(board, p, include_ended=all)

    @app.get("/api/hosted-trees/{session_id}")
    def hosted_tree_detail(session_id: str, p: Principal = P):
        # ... and its details panel for one session: read-only and capped (`board session show` is unchanged).
        from . import hosted_lineage
        return hosted_lineage.detail(board, p, session_id)

    @app.post("/api/hosted-sessions")
    def hosted_start(body: HostedStartIn, p: Principal = P):
        from . import hosted
        board._require_human(p, "start a hosted session")
        config, error = dispatch_config()
        if error:
            raise Conflict(f"the hub's [dispatch] settings cannot be read: {error}")
        return hosted.start(board, p, body.project, body.message, agent=body.agent, model=body.model,
                            allowed=config.nodes, output_schema=body.output_schema)

    @app.post("/api/hosted-sessions/{session_id}/children")
    def hosted_child(session_id: str, body: HostedChildIn, p: Principal = P):
        # A session under another one (hosted_tree.py). Human only, like every route here, and off unless the hub
        # says [dispatch.sessions.spawn] enabled = true.
        from . import hosted_tree
        board._require_human(p, "start a child session")
        config, error = dispatch_config()
        if error:
            raise Conflict(f"the hub's [dispatch] settings cannot be read: {error}")
        return hosted_tree.start_child(board, p, session_id, body.message, allowed=config.nodes,
                                       spawn=config.sessions.spawn, **body.model_dump(exclude={"message"}))

    @app.get("/api/hosted-sessions/{session_id}")
    def hosted_show(session_id: str, p: Principal = P):
        from . import hosted
        return hosted.show(board, p, session_id)

    @app.post("/api/hosted-sessions/{session_id}/messages")
    def hosted_send(session_id: str, body: HostedMessageIn, p: Principal = P):
        from . import hosted
        return hosted.send(board, p, session_id, body.message, mode=body.mode, request_id=body.request_id,
                           output_schema=body.output_schema)

    @app.get("/api/hosted-sessions/{session_id}/messages/{n}")
    async def hosted_message(request: Request, session_id: str, n: int, wait: float = 0, steer: bool = False,
                             p: Principal = P):
        # steer=1: with the terminal status of the steer into that turn (hosted_steer.view), for `send --mode steer`
        from . import hosted
        kw = {"steer": True} if steer else {}
        return await hosted.message_wait(board, p, session_id, n, wait, waiter_gone(request), **kw)

    @app.post("/api/hosted-sessions/{session_id}/close")
    def hosted_close(session_id: str, p: Principal = P):
        from . import hosted, hosted_spawn
        board._require_human(p, "close a hosted session")
        if hosted_spawn.is_iroot(board.conn, session_id):
            return hosted_spawn.close_iroot(board, p, session_id)   # an interactive session's tree of children
        return hosted.close(board, p, session_id)

    # Agent-started child sessions (hosted_spawn.py): the human's grant and allowances. Human only; the gateway
    # passes none of these routes.
    @app.post("/api/hosted-sessions/{session_id}/allowance")
    def hosted_allowance(session_id: str, body: AllowanceIn, p: Principal = P):
        from . import hosted_spawn
        return hosted_spawn.set_allowance(board, p, session_id, body.may_spawn, body.share)

    @app.get("/api/spawn-grants")
    def spawn_grants(all: bool = False, p: Principal = P):
        from . import hosted_spawn
        return hosted_spawn.list_grants(board, p, include_inactive=all)

    @app.post("/api/spawn-grants")
    def spawn_grant(body: SpawnGrantIn, p: Principal = P):
        from . import hosted_spawn
        return hosted_spawn.create_grant(board, p, body.project, body.agents, body.access, body.hours,
                                         hosted_only=body.hosted_only)

    @app.post("/api/spawn-grants/{grant_id}/revoke")
    def spawn_grant_revoke(grant_id: int, p: Principal = P):
        from . import hosted_spawn
        return hosted_spawn.revoke_grant(board, p, grant_id)

    @app.post("/api/sessions/capabilities")
    def register_capabilities(body: CapabilitiesIn, request: Request, p: Principal = P):
        return capabilities.register(board, p, sid(p, request, body.session_id),
                                     body.capabilities, body.evidence, body.ttl_seconds, body.activity)

    @app.post("/api/posts/{post_id}/request-route")
    def route_request(post_id: int, body: RequestRouteIn, request: Request, p: Principal = P):
        return capabilities.route(board, p, sid(p, request, body.session_id), post_id,
                                  body.recipient, body.required_capabilities, body.expected_version)

    @app.post("/api/posts/{post_id}/request-repost")
    def repost_request(post_id: int, body: RequestRepostIn, request: Request, p: Principal = P):
        from . import decision_actions
        return decision_actions.repost(board, p, sid(p, request, body.session_id), post_id,
                                       body.recipient, body.expected_version, body.target_thread_id)

    @app.post("/api/posts/{post_id}/request-progress")
    def request_progress(post_id: int, body: RequestProgressIn, request: Request, p: Principal = P):
        values = body.model_dump(exclude={"session_id", "disposition"})
        return requests.progress(board, p, sid(p, request, body.session_id), post_id,
                                 terminal_disposition=body.disposition, **values)

    @app.post("/api/posts/{post_id}/requests/recover-owner")
    def recover_request_owner(post_id: int, body: RequestOwnershipIn, request: Request, p: Principal = P):
        from . import recovery
        return recovery.transfer_ended_owner(board, p, sid(p, request, body.session_id),
                                              post_id, body.recipient, body.expected_version)

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
    app = create_app(settings=settings)
    server = uvicorn.Server(uvicorn.Config(app, host=host, port=port or settings.port, log_level="info"))
    app.state.stopping = lambda: server.should_exit     # lets a waiting call (hosted sessions) end when asked to stop
    # As uvicorn.run does for one worker without reload: Ctrl-C ends quietly, a failed start exits 3.
    try:
        server.run()
    except KeyboardInterrupt:
        pass
    if not server.started:
        raise SystemExit(3)
