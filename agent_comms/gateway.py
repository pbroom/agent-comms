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
- A long-poll (`_can_wait`: a node's fetch or `board wait`'s call with a positive wait, never an acknowledgement)
  whose caller hangs up before the board answers is dropped upstream at once, so the board's wait ends too
  (DESIGN_NOTES "Long-poll disconnects"). Any other request is forwarded to its end, as before.

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
from pydantic import TypeAdapter, ValidationError
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route

DEFAULT_PORT = 8790
HEADERS = {"accept", "content-type", "authorization", "mcp-session-id", "mcp-protocol-version", "last-event-id",
           "x-board-session"}
RESPONSE_HEADERS = {"content-type", "mcp-session-id", "mcp-protocol-version"}
_ID = r"[1-9][0-9]*"
_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
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
    ("POST", r"/api/wait"),      # `board wait`: a session the human opened waits to be woken (interactive_wake.py)
    ("POST", r"/api/nodes/hello"), ("GET", r"/api/nodes/assignments"), ("POST", r"/api/nodes/runs/[A-Za-z0-9_-][A-Za-z0-9_.:-]*"),
    # Hosted sessions, the node's side only (hosted.py). /api/hosted-sessions is the human's and never passes.
    ("GET", r"/api/nodes/sessions"), ("POST", rf"/api/nodes/sessions/{_UUID}"),
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


_FLOAT = TypeAdapter(float)             # how the board reads `wait` (api.WaitIn.wait, a node fetch's `wait: float`)
_SEEN = re.compile(r"[0-9a-f]{1,64}")   # a version the board's node route accepts (hosted._SEEN)


def _positive(wait) -> bool:
    try:
        return _FLOAT.validate_python(wait) > 0     # nan is not; the board answers a nan wait at once too
    except ValidationError:
        return False


def _can_wait(method: str, path: str, query, body: bytes) -> bool:
    """Whether the board may hold this request open (a long-poll), read only from what the gateway already has, by
    the board's own rules (tests check each shape against the board): a node's fetch with a positive `wait` and a
    valid `seen` (without one the board answers at once), or `board wait`'s buffered JSON body with a positive `wait`
    (coerced as the board coerces it: "10" and true count) and no `ack`. An acknowledgement never waits (the board
    answers an ack at once, whatever its wait), and must reach the board even when its caller has gone: any other
    shape goes the old way."""
    if (method, path) == ("GET", "/api/nodes/sessions"):
        seen = query.get("seen")
        return seen is not None and _SEEN.fullmatch(seen) is not None and _positive(query.get("wait", 0))
    if (method, path) == ("POST", "/api/wait"):
        try:
            message = json.loads(body)
        except (ValueError, UnicodeDecodeError):
            return False
        return isinstance(message, dict) and message.get("ack") is None and _positive(message.get("wait", 0))
    return False


async def _unless_gone(request: Request, pending):
    """`pending` (the upstream response), or None when the caller hangs up first: then the upstream request is
    cancelled, which closes its connection. The body is read by now, so the next message is the disconnect. A
    receive that fails before it is seen is no hang-up: then this waits for the board's answer alone. Nothing is
    left running when this returns: the watcher has ended (so forwarding never reads beside it), a cancelled send
    has been awaited, and a response that arrived but is not handed back (the caller went, or this task was
    cancelled) is closed here."""
    async def hung_up() -> None:
        try:
            while (await request.receive())["type"] != "http.disconnect":
                pass
        except Exception:
            await asyncio.Event().wait()    # until cancelled, once the board has answered
    sending, watching = asyncio.ensure_future(pending), asyncio.ensure_future(hung_up())
    handed = False
    try:
        await asyncio.wait({sending, watching}, return_when=asyncio.FIRST_COMPLETED)
        watching.cancel()
        await asyncio.gather(watching, return_exceptions=True)
        if sending.done():
            handed = True       # no await after this: from here on the caller owns the response
            return sending.result()
        return None             # the caller hung up first: `finally` cancels the send and awaits it
    finally:
        watching.cancel()
        if not sending.done():
            sending.cancel()
        if not (sending.done() and watching.done()):    # never once handed: both are done then, so no await here
            await asyncio.gather(sending, watching, return_exceptions=True)
        if not handed and sending.done() and not sending.cancelled() and sending.exception() is None:
            await sending.result().aclose()


class _Forward(StreamingResponse):
    """The board's response passed on to the caller. It owns that response from here: however the forwarding
    ends (all sent, an upstream error, the caller gone, the task cancelled), the board's response is closed."""

    def __init__(self, upstream: httpx.Response) -> None:
        self.upstream = upstream
        super().__init__(self._chunks(), status_code=upstream.status_code,
                         headers={k: v for k, v in upstream.headers.items() if k in RESPONSE_HEADERS})

    async def _chunks(self):
        try:
            async for chunk in self.upstream.aiter_bytes():
                yield chunk
        except httpx.HTTPError:
            return

    async def __call__(self, scope, receive, send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            await self.upstream.aclose()


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
            if _can_wait(request.method, raw, request.query_params, body):
                response = await _unless_gone(request, c.send(c.build_request(request.method, url, headers=headers,
                                                                              content=body), stream=True))
                if response is None:
                    return JSONResponse({"error": "caller_gone"}, 499)     # nobody reads it
            else:
                response = await c.send(c.build_request(request.method, url, headers=headers, content=body),
                                        stream=True)
        except (httpx.HTTPError, ValueError):
            return JSONResponse({"error": "upstream_unavailable"}, 502)
        opened = response.headers.get("mcp-session-id")
        if raw == "/mcp" and opened and session is None:
            sessions[opened] = agent
            while len(sessions) > MAX_SESSIONS:
                sessions.popitem(last=False)
        if raw == "/mcp" and session is not None and request.method == "DELETE" and response.status_code < 300:
            sessions.pop(session, None)
        return _Forward(response)

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
