"""Clients on a machine that is not the hub (DESIGN_NOTES "Nodes"): `board mcp` and `board brief` talk to the hub
over its gateway instead of opening a local database.

Set `[server] hub = "https://<hub>.<tailnet>.ts.net"` in this machine's board.local.toml (or AGENT_COMMS_HUB in the
environment). Nothing else changes for the clients: Claude Code's and Codex's `board mcp --agent <name>` launchers,
the session-start hook's `board brief`, keep their configuration. The agent's token is read from
~/.config/agent-comms/<agent>.token as before and sent only as a bearer header to the hub, never in argv.

- `bridge`: relays MCP's newline-delimited JSON-RPC messages from stdio to the hub's /mcp (streamable HTTP) and the
  answers back. The hub's own MCP server answers every call (same tools, same instructions). It heals itself: it
  keeps the client's `initialize` and `notifications/initialized`, and when the hub no longer knows the session
  (hub or gateway restarted: 404) it opens a new one with them and retries the call once. A hub it cannot reach
  answers each request at once with a JSON-RPC error naming the hub, and the next request tries again, so a sleep,
  wake or tailnet blip never leaves the session hung or dead. Requests run concurrently (a long-polling read does not
  hold up the next call). Server-initiated messages outside a call's own response, and Claude Code channel push
  (`--channel`), are not relayed: they need a local board.
- `brief`: the same counts `Board.brief` returns, from the hub's GET /api/brief.
"""
from __future__ import annotations

import ipaddress
import json
import os
import sys
import threading
from typing import Any, Callable
from urllib.parse import urlparse

import httpx


def hub_url(settings: Any) -> str | None:
    """The hub this machine's clients use, or None when the board is local. Plain http only to a loopback or tailnet
    (100.64.0.0/10) address; anything else must be https, so a bearer token never crosses a network in clear."""
    url = os.environ.get("AGENT_COMMS_HUB") or getattr(settings, "hub", "") or ""
    if not url:
        return None
    u = urlparse(url)
    if u.scheme not in ("http", "https") or not u.hostname or u.path not in ("", "/") or u.query or u.fragment \
            or u.username or u.password:
        raise ValueError("[server] hub must be a bare http(s) URL such as https://hub.tailnet.ts.net")
    if u.scheme == "http":
        try:
            ip = ipaddress.ip_address(u.hostname)
        except ValueError:
            ip = None
        if u.hostname != "localhost" and (ip is None or not (ip.is_loopback or ip in TAILNET)):
            raise ValueError("[server] hub over plain http is allowed only for localhost or a tailnet 100.x address; "
                             "use https")
    return url.rstrip("/")


TAILNET = ipaddress.ip_network("100.64.0.0/10")
UNREACHABLE = -32000


def _error(msg_id: Any, text: str) -> dict:
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": UNREACHABLE, "message": text}}


class Bridge:
    """The relay's state: one HTTP client, the MCP session, and what is needed to open a new one."""

    def __init__(self, hub: str, token: str, write: Callable[[dict], None], client: Any = None):
        self.hub, self.write = hub, write
        self.client = client or httpx.Client(timeout=httpx.Timeout(330, connect=5), trust_env=False)
        self.client.headers["Authorization"] = "Bearer " + token
        self.session: str | None = None
        self.version: str | None = None
        self.init: dict | None = None          # the client's initialize request
        self.initialized: dict | None = None   # ... and its notifications/initialized
        self.lock = threading.Lock()           # opening a session is one at a time

    def _post(self, message: dict, session: str | None) -> httpx.Response:
        headers = {"accept": "application/json, text/event-stream", "content-type": "application/json"}
        if session:
            headers["mcp-session-id"] = session
        if self.version:
            headers["mcp-protocol-version"] = self.version
        return self.client.post(self.hub + "/mcp", headers=headers, content=json.dumps(message))

    @staticmethod
    def _messages(r: httpx.Response) -> list[dict]:
        """The JSON-RPC messages in a response: a JSON body (one or a batch) or an event stream."""
        if r.status_code == 202 or not r.content:
            return []
        if r.headers.get("content-type", "").startswith("text/event-stream"):
            out = []
            for event in r.text.split("\n\n"):
                data = "\n".join(line[5:].lstrip() for line in event.splitlines() if line.startswith("data:"))
                if data:
                    out.append(json.loads(data))
            return out
        body = r.json()
        return body if isinstance(body, list) else [body]

    def _open(self) -> None:
        """A new session from the client's own initialize (answer discarded) and initialized notification."""
        if self.init is None:
            raise RuntimeError("the client has not initialized")
        r = self._post(self.init | {"id": "agent-comms-reinit"}, None)
        r.raise_for_status()
        self.session = r.headers.get("mcp-session-id")
        if self.initialized is not None:
            self._post(self.initialized, self.session)

    def handle(self, message: dict) -> None:
        msg_id, method = message.get("id"), message.get("method")
        try:
            if method == "initialize":
                with self.lock:
                    self.init, self.session = message, None
                    r = self._post(message, None)
                    if r.status_code < 400:
                        self.session = r.headers.get("mcp-session-id")
                    out = self._messages(r) if r.status_code < 400 else [_error(msg_id, f"hub refused: {r.status_code}")]
                    for m in out:
                        v = (m.get("result") or {}).get("protocolVersion") if isinstance(m, dict) else None
                        self.version = v or self.version
                for m in out:
                    self.write(m)
                return
            if method == "notifications/initialized":
                self.initialized = message
            session = self.session
            r = self._post(message, session)
            if r.status_code == 404 and self.init is not None:
                with self.lock:
                    if self.session == session:      # nobody reopened it meanwhile
                        self._open()
                r = self._post(message, self.session)
            if r.status_code >= 400:
                if msg_id is not None:
                    self.write(_error(msg_id, f"agent-comms hub {self.hub} answered {r.status_code}"))
                return
            for m in self._messages(r):
                self.write(m)
        except (httpx.HTTPError, ValueError, RuntimeError) as e:
            if msg_id is not None:
                self.write(_error(msg_id, f"agent-comms hub {self.hub} unreachable ({type(e).__name__}); "
                                          "retrying on the next call"))


def bridge(hub: str, token: str) -> None:
    """Relay MCP between this process's stdio and the hub's /mcp until stdin closes."""
    print(f"agent-comms: using the board at {hub}", file=sys.stderr, flush=True)
    out_lock = threading.Lock()

    def write(message: dict) -> None:
        with out_lock:
            sys.stdout.write(json.dumps(message, separators=(",", ":")) + "\n")
            sys.stdout.flush()

    b = Bridge(hub, token, write)
    workers: list[threading.Thread] = []
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except ValueError:
            continue
        if not isinstance(message, dict):
            continue
        if message.get("method") in ("initialize", "notifications/initialized"):
            b.handle(message)            # in order: everything after depends on them
            continue
        t = threading.Thread(target=b.handle, args=(message,), daemon=True)
        t.start()
        workers = [w for w in workers if w.is_alive()] + [t]
    for t in workers:
        t.join(5)


class RemoteBoard:
    """The few read-only calls `board brief` needs, against the hub."""

    def __init__(self, hub: str, token: str, client: Any = None):
        # A 1 s connect timeout: the prompt hook runs this on every prompt and must not stall when the hub is down.
        self.client = client or httpx.Client(base_url=hub, timeout=httpx.Timeout(5, connect=1), trust_env=False)
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
