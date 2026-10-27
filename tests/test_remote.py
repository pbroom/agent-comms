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
                                    ("https://hub/mcp", False), ("https://hub?x=1", False)])
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


def test_board_mcp_relays_to_the_hub(env, tmp_path):
    """A real `board mcp --agent` process with a hub configured: an MCP client on its stdio registers on the hub."""
    import asyncio
    import uvicorn
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    server = uvicorn.Server(uvicorn.Config(create_app(env.board), host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    try:
        home = tmp_path / "client-home"
        home.mkdir()
        params = StdioServerParameters(command=sys.executable, args=["-m", "agent_comms.cli", "mcp"], env={
            **os.environ, "AGENT_COMMS_HOME": str(home), "AGENT_COMMS_HUB": f"http://127.0.0.1:{port}",
            "AGENT_COMMS_TOKEN": env.tokens["grok"]})

        async def run():
            async with stdio_client(params) as (r, w):
                async with ClientSession(r, w) as session:
                    await session.initialize()
                    tools = [t.name for t in (await session.list_tools()).tools]
                    out = await session.call_tool("board_register", {"project": PROJECT})
                    return tools, out

        tools, out = asyncio.run(asyncio.wait_for(run(), 30))
        assert "board_read_updates" in tools
        assert not out.is_error, out
        assert env.board.conn.execute("SELECT COUNT(*) FROM sessions WHERE agent='grok'").fetchone()[0] >= 2
    finally:
        server.should_exit = True
        thread.join(5)
