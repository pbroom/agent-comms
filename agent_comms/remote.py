"""Clients on a machine that is not the hub (DESIGN_NOTES "Nodes"): `board mcp` and `board brief` talk to the hub
over its gateway instead of opening a local database.

Set `[server] hub = "https://<hub>.<tailnet>.ts.net"` in this machine's board.local.toml (or AGENT_COMMS_HUB in the
environment). Nothing else changes for the clients: Claude Code's and Codex's `board mcp --agent <name>` launchers,
the session-start hook's `board brief`, keep their configuration. The agent's token is read from
~/.config/agent-comms/<agent>.token as before and sent only as a bearer header to the hub, never in argv.

- `bridge`: a transparent stdio <-> streamable-HTTP relay of MCP messages. The hub's own MCP server answers every
  call (same tools, same instructions); this process only carries the JSON-RPC messages. Claude Code channel push
  (`--channel`) is not relayed: it needs a local board.
- `brief`: the same counts `Board.brief` returns, from the hub's GET /api/brief.
"""
from __future__ import annotations

import os
from typing import Any
from urllib.parse import urlparse

import httpx


def hub_url(settings: Any) -> str | None:
    """The hub this machine's clients use, or None when the board is local."""
    url = os.environ.get("AGENT_COMMS_HUB") or getattr(settings, "hub", "") or ""
    if not url:
        return None
    u = urlparse(url)
    if u.scheme not in ("http", "https") or not u.netloc or u.path not in ("", "/") or u.query or u.fragment:
        raise ValueError("[server] hub must be a bare http(s) URL such as https://hub.tailnet.ts.net")
    return url.rstrip("/")


def bridge(hub: str, token: str) -> None:
    """Relay MCP between this process's stdio and the hub's /mcp until stdin closes."""
    import anyio
    import httpx2
    from mcp.client.streamable_http import streamable_http_client
    from mcp.server.stdio import stdio_server

    async def relay(source, sink) -> None:
        async for message in source:
            if isinstance(message, Exception):
                continue        # a transport error on one message; the stream itself goes on
            await sink.send(message)

    async def main() -> None:
        async with httpx2.AsyncClient(headers={"Authorization": "Bearer " + token},
                                      timeout=httpx2.Timeout(60, read=300), trust_env=False) as http:
            async with stdio_server() as (local_in, local_out):
                async with streamable_http_client(hub + "/mcp", http_client=http) as streams:
                    remote_in, remote_out = streams[0], streams[1]
                    async with anyio.create_task_group() as tg:
                        async def upstream():
                            await relay(local_in, remote_out)
                            tg.cancel_scope.cancel()       # the client closed stdin: we are done
                        tg.start_soon(upstream)
                        tg.start_soon(relay, remote_in, local_out)

    anyio.run(main)


class RemoteBoard:
    """The few read-only calls `board brief` needs, against the hub."""

    def __init__(self, hub: str, token: str, client: Any = None):
        self.client = client or httpx.Client(base_url=hub, timeout=httpx.Timeout(10, connect=5), trust_env=False)
        self.client.headers["Authorization"] = "Bearer " + token

    def whoami(self) -> dict:
        r = self.client.get("/api/whoami")
        r.raise_for_status()
        return r.json()

    def brief(self, projects: list[str], after_seq: int | None = None) -> dict:
        params: dict[str, Any] = {"project": projects}
        if after_seq is not None:
            params["after_seq"] = after_seq
        r = self.client.get("/api/brief", params=params)
        r.raise_for_status()
        return r.json()
