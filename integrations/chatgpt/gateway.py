"""Fail-closed, ChatGPT-only loopback gateway. No dashboard or admin exposure."""
from __future__ import annotations

import hmac
import json
import os
import re
from pathlib import Path

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route
from starlette.background import BackgroundTask

UPSTREAM = "http://127.0.0.1:8787"
HEADERS = {"accept", "content-type", "mcp-session-id", "mcp-protocol-version", "last-event-id", "x-board-session"}
RESPONSE_HEADERS = {"content-type", "mcp-session-id", "mcp-protocol-version"}
ACTIONS = {
    ("POST", "/api/sessions"), ("GET", "/api/updates"),
    ("POST", "/api/updates/ack"), ("POST", "/api/posts"), ("GET", "/api/tasks"),
}


def load_token() -> str:
    token = os.environ.get("AGENT_COMMS_CHATGPT_TOKEN")
    if not token:
        path = Path.home() / ".config/agent-comms/chatgpt.token"
        if not path.is_file() or path.stat().st_mode & 0o077:
            raise RuntimeError("Set AGENT_COMMS_CHATGPT_TOKEN or create a private (0600) chatgpt.token")
        token = path.read_text().strip()
    if not token or any(c.isspace() for c in token):
        raise RuntimeError("ChatGPT token is empty or malformed")
    return token


def allowed(mode: str, method: str, path: str) -> bool:
    if mode == "mcp":
        return path == "/mcp" and method in {"GET", "POST", "DELETE"}
    return (method, path) in ACTIONS or (
        method == "GET" and re.fullmatch(r"/api/tasks/[1-9][0-9]*", path) is not None
    ) or (
        method == "POST" and re.fullmatch(r"/api/tasks/[1-9][0-9]*/(claim|release|transition)", path) is not None
    )


def create_app(token: str, mode: str = "mcp", transport=None) -> Starlette:
    if mode not in {"mcp", "actions"} or not token:
        raise ValueError("A token and mode mcp or actions are required")

    async def proxy(request: Request):
        auth = request.headers.getlist("authorization")
        if len(auth) != 1 or not hmac.compare_digest(auth[0], "Bearer " + token):
            return JSONResponse({"error": "unauthorized"}, 401)
        # Remote clients are server-to-server. Reject all supplied browser origins;
        # never strip a foreign Origin to bypass the upstream MCP SDK protection.
        if "origin" in request.headers:
            return JSONResponse({"error": "origin_not_allowed"}, 403)
        raw = request.scope.get("raw_path", b"").decode("ascii", "replace")
        if raw != request.url.path or not allowed(mode, request.method, raw):
            return JSONResponse({"error": "route_not_allowed"}, 404)
        headers = {k: v for k, v in request.headers.items() if k in HEADERS}
        headers["authorization"] = "Bearer " + token
        client = httpx.AsyncClient(transport=transport, timeout=httpx.Timeout(60, read=300), trust_env=False)
        try:
            # Prevent a mistaken human/Codex token configuration, and honor revocation.
            identity = await client.get(UPSTREAM + "/api/whoami", headers={"authorization": headers["authorization"]})
            if identity.status_code != 200 or identity.json().get("name") != "chatgpt" or identity.json().get("is_human") is not False:
                await client.aclose()
                return JSONResponse({"error": "chatgpt_identity_required"}, 403)
            body = await request.body()
            # ChatGPT probes this unsupported method before legacy initialization.
            # The SDK otherwise returns HTTP 400 "Missing session ID", preventing
            # the caller from falling back to initialize. Keep identity checks above.
            # See openai/tunnel-client#41 and #71; no tool calls are synthesized.
            if mode == "mcp" and request.method == "POST" and "mcp-session-id" not in request.headers:
                try:
                    message = json.loads(body)
                except (ValueError, UnicodeDecodeError):
                    message = None
                if (isinstance(message, dict) and message.get("jsonrpc") == "2.0"
                        and message.get("method") == "server/discover"
                        and type(message.get("id")) in (str, int)):
                    await client.aclose()
                    return JSONResponse({"jsonrpc": "2.0", "id": message["id"],
                                         "error": {"code": -32601, "message": "Method not found"}})
            url = UPSTREAM + raw
            if request.url.query:
                url += "?" + request.url.query
            upstream = await client.send(client.build_request(request.method, url, headers=headers, content=body), stream=True)
        except (httpx.HTTPError, ValueError):
            await client.aclose()
            return JSONResponse({"error": "upstream_unavailable"}, 502)

        async def close():
            await upstream.aclose()
            await client.aclose()

        return StreamingResponse(upstream.aiter_bytes(), status_code=upstream.status_code,
                                 headers={k: v for k, v in upstream.headers.items() if k in RESPONSE_HEADERS},
                                 background=BackgroundTask(close))

    return Starlette(routes=[Route("/{path:path}", proxy, methods=["GET", "POST", "DELETE", "PUT", "PATCH", "OPTIONS", "HEAD"])])


if __name__ == "__main__":
    import argparse
    import uvicorn
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["mcp", "actions"], default="mcp")
    parser.add_argument("--fd", type=int, default=None)
    args = parser.parse_args()
    uvicorn.run(create_app(load_token(), args.mode), host="127.0.0.1", port=8789, fd=args.fd, access_log=False)
