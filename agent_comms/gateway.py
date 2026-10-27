"""Agent gateway: the one door through which agents on other machines and in the cloud reach this board.

`board serve` only ever binds localhost. `board gateway` also binds localhost (port 8790 by default) and is published
by Tailscale: `tailscale serve` for machines on the tailnet, and optionally `tailscale funnel` for cloud agents
(README "Reaching the board from other machines"). Nothing here opens a port on a public interface.

What passes (fail closed, as integrations/chatgpt/gateway.py):
- Only a bearer token of an active non-human agent: each request's token is checked against `/api/whoami`
  upstream (re-checked at least every IDENTITY_TTL seconds, so a revoked or rotated token stops working within
  30 s), and a human token is refused. The human's dashboard,
  sign-in links and admin routes are never reachable through the gateway, whoever calls.
- Only `/mcp` (streamable HTTP, for any MCP client: Claude Code, Codex, Grok, others) and the agent routes of the
  HTTP API listed in API_ROUTES (for clients without MCP). Everything else is 404 before anything is forwarded.
- No browser: a request carrying an Origin header is refused (server-to-server only). The raw path must equal the
  decoded path, so encoded tricks (`/%6dcp`) cannot reach another route.
- Only the headers in HEADERS go upstream and only those in RESPONSE_HEADERS come back (no cookies).

The upstream board still does every authorization check; the gateway only narrows what can reach it.
"""
from __future__ import annotations

import hmac
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
    ("GET", rf"/api/posts/{_ID}/requests/[A-Za-z0-9_.-]+/history"),
    ("GET", r"/api/updates"), ("POST", r"/api/updates/ack"),
    ("GET", r"/api/tasks"), ("POST", r"/api/tasks"), ("GET", rf"/api/tasks/{_ID}"),
    ("POST", rf"/api/tasks/{_ID}/(claim|renew|release|transition)"),
    ("GET", r"/api/grants"),
)]
IDENTITY_TTL = 30.0   # seconds a verified token stays verified before /api/whoami is asked again


def allowed(method: str, path: str) -> bool:
    if path == "/mcp":
        return method in {"GET", "POST", "DELETE"}
    return any(m == method and rx.fullmatch(path) for m, rx in API_ROUTES)


def create_app(upstream: str = "http://127.0.0.1:8787", transport=None, clock=time.monotonic) -> Starlette:
    verified: dict[str, tuple[float, str]] = {}   # bearer -> (checked at, agent name)

    async def identity(client: httpx.AsyncClient, bearer: str) -> str | None:
        hit = verified.get(bearer)
        if hit and clock() - hit[0] < IDENTITY_TTL:
            return hit[1]
        r = await client.get(upstream + "/api/whoami", headers={"authorization": bearer})
        try:
            body = r.json() if r.status_code == 200 else None
        except ValueError:
            body = None
        if not isinstance(body, dict) or body.get("is_human") is not False or not isinstance(body.get("name"), str):
            verified.pop(bearer, None)
            return None
        if len(verified) > 1000:
            verified.clear()
        verified[bearer] = (clock(), body["name"])
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
        client = httpx.AsyncClient(transport=transport, timeout=httpx.Timeout(60, read=300), trust_env=False)
        try:
            if await identity(client, auth[0]) is None:
                await client.aclose()
                return JSONResponse({"error": "agent_identity_required"}, 403)
            body = await request.body()
            if raw == "/mcp" and request.method == "POST" and "mcp-session-id" not in request.headers:
                # Some clients probe this before initialize; answer "not found" instead of the SDK's 400 so they
                # fall back to initialize (as integrations/chatgpt/gateway.py does).
                try:
                    message = json.loads(body)
                except (ValueError, UnicodeDecodeError):
                    message = None
                if (isinstance(message, dict) and message.get("jsonrpc") == "2.0"
                        and message.get("method") == "server/discover" and type(message.get("id")) in (str, int)):
                    await client.aclose()
                    return JSONResponse({"jsonrpc": "2.0", "id": message["id"],
                                         "error": {"code": -32601, "message": "Method not found"}})
            headers = {k: v for k, v in request.headers.items() if k in HEADERS}
            url = upstream + raw + ("?" + request.url.query if request.url.query else "")
            response = await client.send(client.build_request(request.method, url, headers=headers, content=body),
                                         stream=True)
        except (httpx.HTTPError, ValueError):
            await client.aclose()
            return JSONResponse({"error": "upstream_unavailable"}, 502)

        async def close():
            await response.aclose()
            await client.aclose()

        return StreamingResponse(response.aiter_bytes(), status_code=response.status_code,
                                 headers={k: v for k, v in response.headers.items() if k in RESPONSE_HEADERS},
                                 background=BackgroundTask(close))

    async def health(request: Request):
        return JSONResponse({"ok": True})

    return Starlette(routes=[
        Route("/healthz", health, methods=["GET"]),
        Route("/{path:path}", proxy, methods=["GET", "POST", "DELETE", "PUT", "PATCH", "OPTIONS", "HEAD"])])


def serve(port: int | None = None) -> None:
    import uvicorn

    from .config import Settings
    settings = Settings.load()
    host = "::1" if ":" in settings.host else "127.0.0.1"
    upstream = f"http://[{settings.host}]:{settings.port}" if ":" in settings.host else \
        f"http://{settings.host}:{settings.port}"
    uvicorn.run(create_app(upstream), host=host, port=port or DEFAULT_PORT, access_log=False)
