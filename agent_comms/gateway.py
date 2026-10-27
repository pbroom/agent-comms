"""Agent gateway: the one door through which agents on other machines and in the cloud reach this board.

`board serve` only ever binds localhost. `board gateway` also binds localhost (port 8790 by default) and is published
by Tailscale: `tailscale serve` for machines on the tailnet, and optionally `tailscale funnel` for cloud agents
(README "Reaching the board from other machines"). Nothing here opens a port on a public interface.

What passes (fail closed, as integrations/chatgpt/gateway.py):
- Only a bearer token of an active non-human agent: the token is checked against `/api/whoami` upstream (re-checked
  at least every IDENTITY_TTL seconds), and a human token is refused. The board itself re-authenticates the
  forwarded token on every API call and MCP tool call, so a revoked token fails at once on any action; an already
  open MCP event stream (GET /mcp) stays open until the client closes it.
- Bad tokens cost little: a refused token is refused locally for REFUSED_TTL seconds, identity checks share one
  pooled upstream client and at most IDENTITY_CONCURRENCY run at once, and past FAILURE_BUDGET refusals in
  FAILURE_WINDOW seconds only tokens that were already verified get through (others get 429).
- An MCP session belongs to the agent that opened it through this gateway: a request carrying another agent's
  `mcp-session-id` is refused (403), and one the gateway never saw opened is answered 404, so the client initializes
  a new session. One agent cannot read another's event stream or close its session. The human's dashboard,
  sign-in links and admin routes are never reachable through the gateway, whoever calls.
- Only `/mcp` (streamable HTTP, for any MCP client: Claude Code, Codex, Grok, others) and the agent routes of the
  HTTP API listed in API_ROUTES (for clients without MCP). Everything else is 404 before anything is forwarded.
  `/mcp` offers every board tool an agent has locally (browser bookkeeping included); the HTTP list is narrower.
  The board enforces each tool's permissions either way.
- No browser: a request carrying an Origin header is refused (server-to-server only). The raw path must equal the
  decoded path, so encoded tricks (`/%6dcp`) cannot reach another route.
- Only the headers in HEADERS go upstream and only those in RESPONSE_HEADERS come back (no cookies).

The upstream board still does every authorization check; the gateway only narrows what can reach it.
"""
from __future__ import annotations

import asyncio
import hashlib
from collections import OrderedDict
import json
import re
import time

import httpx
from starlette.applications import Starlette
from starlette.background import BackgroundTask
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route

DEFAULT_PORT = 8790
HEADERS = {"accept", "content-type", "authorization", "mcp-session-id", "mcp-protocol-version", "last-event-id",
           "x-board-session"}
RESPONSE_HEADERS = {"content-type", "mcp-session-id", "mcp-protocol-version"}
_ID = r"[1-9][0-9]*"
# (method, path regex) an agent may call over HTTP. Human-only routes (admin, finalize, unseal, resolve, unstick,
# grants admin, login, dashboard state) are deliberately absent.
API_ROUTES = [(m, re.compile(p)) for m, p in (
    ("GET", r"/api/whoami"), ("GET", r"/api/configuration"),
    ("POST", r"/api/sessions"), ("POST", rf"/api/sessions/{_ID}/heartbeat"), ("POST", r"/api/sessions/capabilities"),
    ("GET", r"/api/threads"), ("POST", r"/api/threads"), ("GET", rf"/api/threads/{_ID}/posts"),
    ("PUT", rf"/api/threads/{_ID}/summary"),
    ("GET", r"/api/issues"), ("POST", r"/api/issues"), ("GET", rf"/api/issues/{_ID}"),
    ("POST", rf"/api/issues/{_ID}/(links|comments)"),
    ("POST", r"/api/posts"), ("GET", rf"/api/posts/{_ID}"), ("POST", rf"/api/posts/{_ID}/attention/resolve"),
    ("POST", rf"/api/posts/{_ID}/(request-route|request-repost|request-progress|requests/recover-owner)"),
    ("GET", rf"/api/posts/{_ID}/requests/[A-Za-z0-9_-][A-Za-z0-9_.-]*/history"),
    ("GET", r"/api/updates"), ("POST", r"/api/updates/ack"),
    ("GET", r"/api/tasks"), ("POST", r"/api/tasks"), ("GET", rf"/api/tasks/{_ID}"),
    ("POST", rf"/api/tasks/{_ID}/(claim|renew|release|transition)"),
    ("GET", r"/api/grants"), ("GET", r"/api/brief"),
    ("POST", r"/api/nodes/hello"), ("GET", r"/api/nodes/assignments"), ("POST", r"/api/nodes/runs/[A-Za-z0-9_-][A-Za-z0-9_.:-]*"),
)]
IDENTITY_TTL = 30.0   # seconds a verified token stays verified before /api/whoami is asked again
REFUSED_TTL = 10.0    # seconds a refused token is refused without asking upstream again
FAILURE_WINDOW = 10.0
FAILURE_BUDGET = 30   # refused identity checks per window across all callers; past it, unknown tokens get 429
IDENTITY_CONCURRENCY = 4
MAX_SESSIONS = 10_000


def allowed(method: str, path: str) -> bool:
    if path == "/mcp":
        return method in {"GET", "POST", "DELETE"}
    return any(m == method and rx.fullmatch(path) for m, rx in API_ROUTES)


def _digest(bearer: str) -> str:
    return hashlib.sha256(bearer.encode()).hexdigest()


def create_app(upstream: str = "http://127.0.0.1:8787", transport=None, clock=time.monotonic) -> Starlette:
    verified: dict[str, tuple[float, str]] = {}   # sha256(bearer) -> (checked at, agent name)
    refused: dict[str, float] = {}                # sha256(bearer) -> refused at
    failures: list[float] = []                    # times of refused identity checks (all callers)
    sessions: OrderedDict[str, str] = OrderedDict()   # mcp-session-id -> the agent that opened it (oldest first)
    checks = asyncio.Semaphore(IDENTITY_CONCURRENCY)
    shared: dict[str, httpx.AsyncClient] = {}

    def client() -> httpx.AsyncClient:
        if "c" not in shared:
            shared["c"] = httpx.AsyncClient(transport=transport, timeout=httpx.Timeout(60, read=300), trust_env=False,
                                            limits=httpx.Limits(max_connections=64, max_keepalive_connections=16))
        return shared["c"]

    async def identity(bearer: str) -> str | None | bool:
        """The agent name; None when refused; False when refused without asking (failure budget spent)."""
        key, now = _digest(bearer), clock()
        hit = verified.get(key)
        if hit and now - hit[0] < IDENTITY_TTL:
            return hit[1]
        if now - refused.get(key, float("-inf")) < REFUSED_TTL:
            return None
        failures[:] = [t for t in failures if now - t < FAILURE_WINDOW]
        if hit is None and len(failures) >= FAILURE_BUDGET:
            return False    # under a flood of bad tokens only already-known agents get through
        async with checks:
            r = await client().get(upstream + "/api/whoami", headers={"authorization": bearer})
        try:
            body = r.json() if r.status_code == 200 else None
        except ValueError:
            body = None
        if not isinstance(body, dict) or body.get("is_human") is not False or not isinstance(body.get("name"), str):
            verified.pop(key, None)
            if len(refused) > 10_000:
                refused.clear()
            refused[key] = clock()
            failures.append(clock())
            return None
        if len(verified) > 1000:
            verified.clear()
        verified[key] = (clock(), body["name"])
        return body["name"]

    async def proxy(request: Request):
        auth = request.headers.getlist("authorization")
        if len(auth) != 1 or not auth[0].startswith("Bearer ") or len(auth[0]) < 16:
            return JSONResponse({"error": "unauthorized"}, 401)
        if "origin" in request.headers:
            return JSONResponse({"error": "origin_not_allowed"}, 403)
        raw = request.scope.get("raw_path", b"").decode("ascii", "replace")
        if raw != request.url.path or not allowed(request.method, raw):
            return JSONResponse({"error": "route_not_allowed"}, 404)
        try:
            agent = await identity(auth[0])
        except httpx.HTTPError:
            return JSONResponse({"error": "upstream_unavailable"}, 502)
        if agent is False:
            return JSONResponse({"error": "too_many_failed_sign_ins"}, 429)
        if agent is None:
            return JSONResponse({"error": "agent_identity_required"}, 403)
        session = request.headers.get("mcp-session-id")
        if raw == "/mcp" and session is not None:
            owner = sessions.get(session)
            if owner is None:
                # Not opened through this gateway (or since evicted): 404 makes an MCP client initialize afresh.
                return JSONResponse({"error": "unknown_session"}, 404)
            if owner != agent:
                return JSONResponse({"error": "session_belongs_to_another_agent"}, 403)
        body = await request.body()
        if raw == "/mcp" and request.method == "POST" and session is None:
            # Some clients probe this before initialize; answer "not found" instead of the SDK's 400 so they
            # fall back to initialize (as integrations/chatgpt/gateway.py does).
            try:
                message = json.loads(body)
            except (ValueError, UnicodeDecodeError):
                message = None
            if (isinstance(message, dict) and message.get("jsonrpc") == "2.0"
                    and message.get("method") == "server/discover" and type(message.get("id")) in (str, int)):
                return JSONResponse({"jsonrpc": "2.0", "id": message["id"],
                                     "error": {"code": -32601, "message": "Method not found"}})
        headers = {k: v for k, v in request.headers.items() if k in HEADERS}
        url = upstream + raw + ("?" + request.url.query if request.url.query else "")
        c = client()
        try:
            response = await c.send(c.build_request(request.method, url, headers=headers, content=body), stream=True)
        except (httpx.HTTPError, ValueError):
            return JSONResponse({"error": "upstream_unavailable"}, 502)
        opened = response.headers.get("mcp-session-id")
        if raw == "/mcp" and opened and session is None:
            sessions[opened] = agent
            while len(sessions) > MAX_SESSIONS:
                sessions.popitem(last=False)
        if raw == "/mcp" and session is not None and request.method == "DELETE" and response.status_code < 300:
            sessions.pop(session, None)

        async def stream():
            try:
                async for chunk in response.aiter_bytes():
                    yield chunk
            except httpx.HTTPError:
                return
            finally:
                await response.aclose()

        return StreamingResponse(stream(), status_code=response.status_code,
                                 headers={k: v for k, v in response.headers.items() if k in RESPONSE_HEADERS},
                                 background=BackgroundTask(response.aclose))

    async def health(request: Request):
        return JSONResponse({"ok": True})

    return Starlette(routes=[
        Route("/healthz", health, methods=["GET"]),
        Route("/{path:path}", proxy, methods=["GET", "POST", "DELETE", "PUT", "PATCH", "OPTIONS", "HEAD"])])


def serve(port: int | None = None) -> None:
    import uvicorn

    from .config import Settings
    settings = Settings.load()
    upstream = f"http://[{settings.host}]:{settings.port}" if ":" in settings.host else \
        f"http://{settings.host}:{settings.port}"
    # Always 127.0.0.1: `tailscale serve <port>` proxies to 127.0.0.1, whatever address the board itself uses.
    uvicorn.run(create_app(upstream), host="127.0.0.1", port=port or DEFAULT_PORT, access_log=False)
