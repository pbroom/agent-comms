"""Clients on a machine that is not the hub (remote.py): `board mcp` relays to the hub's /mcp, `board brief` asks the
hub's /api/brief."""
import os
import socket
import subprocess
import sys
import threading
import time

import pytest
from fastapi.testclient import TestClient

from agent_comms import remote
from agent_comms.api import create_app

from conftest import PROJECT


@pytest.mark.parametrize("url,ok", [("https://hub.tail.ts.net", True), ("http://127.0.0.1:8790/", True),
                                    ("hub.tail.ts.net", False), ("ftp://x", False),
                                    ("https://hub/mcp", False), ("https://hub?x=1", False),
                                    ("http://hub.example.com", False), ("http://100.66.145.107:8790", True),
                                    ("https://user:pw@hub", False), ("http://localhost:8790", True)])
def test_hub_url_is_a_bare_http_url(url, ok, monkeypatch):
    monkeypatch.delenv("AGENT_COMMS_HUB", raising=False)
    settings = type("S", (), {"hub": url})()
    if ok:
        assert remote.hub_url(settings) == url.rstrip("/")
    else:
        with pytest.raises(ValueError):
            remote.hub_url(settings)


def test_no_hub_means_local(monkeypatch):
    monkeypatch.delenv("AGENT_COMMS_HUB", raising=False)
    assert remote.hub_url(type("S", (), {"hub": ""})()) is None


def test_brief_from_the_hub_matches_the_local_brief(env):
    tid = env.thread()
    env.post("human", tid, "please", "request", to=["grok"])
    client = TestClient(create_app(env.board), base_url="http://127.0.0.1:8787")
    far = remote.RemoteBoard("http://127.0.0.1:8787", env.tokens["grok"], client=client)
    assert far.whoami()["name"] == "grok"
    assert far.brief([PROJECT]) == env.board.brief(env.p["grok"], [PROJECT])
    assert far.brief([PROJECT], after_seq=0) == env.board.brief(env.p["grok"], [PROJECT], after_seq=0)
    assert client.get("/api/brief", params={"project": ["relative"]},
                      headers={"Authorization": "Bearer " + env.tokens["grok"]}).status_code == 400


def free_port():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


class Served:
    """An ASGI app on a fixed local port, in a thread; stop() and start() again simulate a restart."""

    def __init__(self, make_app, port):
        self.make_app, self.port, self.server, self.thread = make_app, port, None, None

    def start(self):
        import uvicorn
        self.server = uvicorn.Server(uvicorn.Config(self.make_app(), host="127.0.0.1", port=self.port,
                                                    log_level="error"))
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        for _ in range(200):
            if self.server.started:
                return self
            time.sleep(0.02)
        raise RuntimeError("server did not start")

    def stop(self):
        self.server.should_exit = True
        self.thread.join(5)


@pytest.fixture
def hub(env):
    """The board on one port and the gateway in front of it on another, as on the server Mac."""
    from agent_comms import gateway
    board_port, gateway_port = free_port(), free_port()
    board = Served(lambda: create_app(env.board), board_port).start()
    front = Served(lambda: gateway.create_app(f"http://127.0.0.1:{board_port}"), gateway_port).start()
    yield type("Hub", (), {"url": f"http://127.0.0.1:{gateway_port}", "board": board, "gateway": front})
    front.stop()
    board.stop()


def stdio(env, tmp_path, hub_url, argv, extra=None):
    from mcp import StdioServerParameters
    home = tmp_path / "client-home"
    home.mkdir(exist_ok=True)
    return StdioServerParameters(command=sys.executable, args=argv, env={
        **os.environ, "AGENT_COMMS_HOME": str(home), "AGENT_COMMS_HUB": hub_url, **(extra or {})})


async def calls(params, steps):
    """Run `steps(session)` against a stdio MCP server process; returns what it returns."""
    from mcp import ClientSession
    from mcp.client.stdio import stdio_client
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as session:
            await session.initialize()
            return await steps(session)


def run(coro):
    import asyncio
    return asyncio.run(asyncio.wait_for(coro, 60))


def test_board_mcp_relays_through_the_gateway(env, tmp_path, hub):
    params = stdio(env, tmp_path, hub.url, ["-m", "agent_comms.cli", "mcp"], {"AGENT_COMMS_TOKEN": env.tokens["grok"]})

    async def steps(s):
        tools = [t.name for t in (await s.list_tools()).tools]
        return tools, await s.call_tool("board_register", {"project": PROJECT})
    tools, out = run(calls(params, steps))
    assert "board_read_updates" in tools and not out.is_error, out
    assert env.board.conn.execute("SELECT COUNT(*) FROM sessions WHERE agent='grok'").fetchone()[0] >= 2


def test_the_codex_launcher_uses_the_hub_too(env, tmp_path, hub):
    from pathlib import Path
    launcher = str(Path(__file__).parents[1] / "integrations/codex/stdio.py")
    params = stdio(env, tmp_path, hub.url, [launcher], {"AGENT_COMMS_CODEX_TOKEN": env.tokens["codex"],
                                                         "PYTHONPATH": str(Path(__file__).parents[1])})

    async def steps(s):
        return await s.call_tool("board_register", {"project": PROJECT})
    out = run(calls(params, steps))
    assert not out.is_error, out
    assert env.board.conn.execute("SELECT COUNT(*) FROM sessions WHERE agent='codex'").fetchone()[0] >= 2


def test_a_hub_restart_is_healed_without_the_client_noticing(env, tmp_path, hub):
    params = stdio(env, tmp_path, hub.url, ["-m", "agent_comms.cli", "mcp"], {"AGENT_COMMS_TOKEN": env.tokens["grok"]})

    async def steps(s):
        first = await s.call_tool("board_register", {"project": PROJECT})
        hub.gateway.stop()
        hub.board.stop()
        hub.board.start()
        hub.gateway.start()             # both lost every MCP session
        second = await s.call_tool("board_register", {"project": PROJECT})
        return first, second
    first, second = run(calls(params, steps))
    assert not first.is_error and not second.is_error, second


def test_an_unreachable_hub_answers_at_once_and_the_next_call_recovers(env, tmp_path, hub):
    params = stdio(env, tmp_path, hub.url, ["-m", "agent_comms.cli", "mcp"], {"AGENT_COMMS_TOKEN": env.tokens["grok"]})

    async def steps(s):
        from mcp.shared.exceptions import MCPError as McpError
        hub.gateway.stop()
        started = time.monotonic()
        try:
            await s.call_tool("board_register", {"project": PROJECT})
            failed = None
        except McpError as e:
            failed = str(e)
        took = time.monotonic() - started
        hub.gateway.start()
        return failed, took, await s.call_tool("board_register", {"project": PROJECT})
    failed, took, after = run(calls(params, steps))
    assert failed and "unreachable" in failed and took < 10
    assert not after.is_error, after
