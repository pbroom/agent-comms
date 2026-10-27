"""The agent gateway (agent_comms/gateway.py): only agent tokens reach /mcp and the agent API routes."""
import httpx
import pytest
from starlette.testclient import TestClient

from agent_comms import gateway

AGENT = "Bearer agent-token-123456"


class Clock:
    t = 0.0

    def __call__(self):
        return self.t


def client(identity=None, status=200, clock=None):
    seen = []

    def upstream(request):
        seen.append(request)
        if request.url.path == "/api/whoami":
            return httpx.Response(status, json=identity if identity is not None else {"name": "grok", "is_human": False})
        return httpx.Response(200, json={"ok": True},
                              headers={"mcp-session-id": "s", "set-cookie": "secret=bad", "x-internal": "no"})
    app = gateway.create_app("http://upstream", httpx.MockTransport(upstream), clock=clock or Clock())
    return TestClient(app), seen


@pytest.mark.parametrize("token", [None, "Basic abc", "Bearer short"])
def test_a_bearer_token_is_required(token):
    app, seen = client()
    assert app.post("/mcp", headers={"Authorization": token} if token else {}).status_code == 401
    assert not seen


@pytest.mark.parametrize("method,path", [
    ("GET", "/"), ("GET", "/api/state"), ("POST", "/api/admin/pause"), ("POST", "/api/admin/grants"),
    ("POST", "/api/posts/1/unseal"), ("POST", "/api/posts/1/finalize"), ("POST", "/api/posts/1/resolve"),
    ("POST", "/api/threads/1/unstick"), ("POST", "/api/login-links"), ("GET", "/login/abc"),
    ("POST", "/mcp/"), ("POST", "/%6dcp"), ("OPTIONS", "/mcp"), ("POST", "/api/threads/1/close"),
    ("GET", "/api/docs"), ("GET", "/api/posts/0"),
])
def test_other_routes_fail_closed(method, path):
    app, seen = client()
    assert app.request(method, path, headers={"Authorization": AGENT}).status_code == 404
    assert not seen


def test_browsers_are_refused():
    app, seen = client()
    assert app.post("/mcp", headers={"Authorization": AGENT, "Origin": "https://evil.example"}).status_code == 403
    assert not seen


@pytest.mark.parametrize("identity,status", [({"name": "human", "is_human": True}, 200), ({"error": "x"}, 401),
                                              ({"name": "x"}, 200)])
def test_human_or_unknown_tokens_are_refused(identity, status):
    app, seen = client(identity, status)
    assert app.post("/mcp", headers={"Authorization": AGENT}).status_code == 403
    assert [r.url.path for r in seen] == ["/api/whoami"]


@pytest.mark.parametrize("method,path", [
    ("POST", "/mcp"), ("GET", "/mcp"), ("DELETE", "/mcp"), ("POST", "/api/sessions"), ("GET", "/api/updates"),
    ("POST", "/api/updates/ack"), ("POST", "/api/posts"), ("POST", "/api/tasks/3/claim"),
    ("POST", "/api/posts/7/request-progress"), ("GET", "/api/posts/7/requests/claude-code/history"),
])
def test_agent_routes_pass_with_the_callers_own_token(method, path):
    app, seen = client()
    r = app.request(method, path, headers={"Authorization": AGENT, "Cookie": "web=secret"})
    assert r.status_code == 200 and r.headers["mcp-session-id"] == "s"
    assert "set-cookie" not in r.headers and "x-internal" not in r.headers
    forwarded = seen[-1]
    assert forwarded.url.path == path and forwarded.headers["authorization"] == AGENT
    assert "cookie" not in forwarded.headers


def test_identity_is_rechecked_after_the_ttl():
    clock = Clock()
    app, seen = client(clock=clock)
    app.post("/mcp", headers={"Authorization": AGENT})
    app.post("/mcp", headers={"Authorization": AGENT})
    assert [r.url.path for r in seen].count("/api/whoami") == 1
    clock.t += gateway.IDENTITY_TTL + 1
    app.post("/mcp", headers={"Authorization": AGENT})
    assert [r.url.path for r in seen].count("/api/whoami") == 2


def test_discover_probe_gets_method_not_found():
    app, seen = client()
    r = app.post("/mcp", headers={"Authorization": AGENT},
                 json={"jsonrpc": "2.0", "id": 1, "method": "server/discover"})
    assert r.json()["error"]["code"] == -32601


def test_health_needs_no_token():
    app, _ = client()
    assert app.get("/healthz").json() == {"ok": True}


def test_end_to_end_against_the_real_board(env):
    """A real board behind the gateway: an agent token registers and posts; the human's token is refused."""
    from agent_comms.api import create_app as board_app
    from conftest import PROJECT
    tid = env.thread()
    app = TestClient(gateway.create_app("http://127.0.0.1:8787", httpx.ASGITransport(app=board_app(env.board))))
    agent = {"Authorization": "Bearer " + env.tokens["grok"]}
    sid = app.post("/api/sessions", headers=agent, json={"project": PROJECT}).json()["session_id"]
    r = app.post("/api/posts", headers={**agent, "X-Board-Session": str(sid)},
                 json={"thread_id": tid, "body": "from another machine", "type": "status"})
    assert r.status_code == 200, r.text
    assert app.get("/api/updates", headers=agent, params={"session_id": sid}).status_code == 200
    human = {"Authorization": "Bearer " + env.tokens["human"]}
    assert app.get("/api/whoami", headers=human).status_code == 403
    assert app.post("/api/admin/pause", headers=human).status_code == 404


def test_mcp_through_the_gateway_lists_board_tools(env):
    """Any MCP client (here the reference SDK) initializes and lists tools through the gateway with its own token."""
    import asyncio
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client
    from agent_comms.api import create_app as board_app

    board = board_app(env.board)
    front = gateway.create_app("http://127.0.0.1:8787", httpx.ASGITransport(app=board, client=("127.0.0.1", 5555)))

    async def run():
        async with board.router.lifespan_context(board):
            transport = httpx.ASGITransport(app=front)
            headers = {"Authorization": f"Bearer {env.tokens['grok']}"}
            async with httpx.AsyncClient(transport=transport, headers=headers) as http:
                async with streamable_http_client("http://gateway.test/mcp", http_client=http) as streams:
                    async with ClientSession(streams[0], streams[1]) as session:
                        await session.initialize()
                        return [t.name for t in (await session.list_tools()).tools]

    assert "board_read_updates" in asyncio.run(run())


def test_a_flood_of_bad_tokens_costs_at_most_one_upstream_check_each_then_none():
    app, seen = client({"error": "bad"}, 401)
    for i in range(gateway.FAILURE_BUDGET):
        assert app.post("/mcp", headers={"Authorization": f"Bearer junk-token-{i:06d}"}).status_code == 403
        assert app.post("/mcp", headers={"Authorization": f"Bearer junk-token-{i:06d}"}).status_code == 403
    assert len(seen) == gateway.FAILURE_BUDGET, "a refused token is refused locally on retry"
    assert app.post("/mcp", headers={"Authorization": "Bearer junk-token-new"}).status_code == 429
    assert len(seen) == gateway.FAILURE_BUDGET, "past the budget, unknown tokens never reach the board"


def test_known_agents_still_pass_during_a_flood():
    clock = Clock()
    calls = {"n": 0}

    def upstream(request):
        if request.url.path == "/api/whoami":
            calls["n"] += 1
            if request.headers["authorization"] == AGENT:
                return httpx.Response(200, json={"name": "grok", "is_human": False})
            return httpx.Response(401, json={"error": "bad"})
        return httpx.Response(200, json={"ok": True})
    app = TestClient(gateway.create_app("http://upstream", httpx.MockTransport(upstream), clock=clock))
    assert app.post("/mcp", headers={"Authorization": AGENT}).status_code == 200
    for i in range(gateway.FAILURE_BUDGET):
        app.post("/mcp", headers={"Authorization": f"Bearer junk-token-{i:06d}"})
    clock.t += gateway.IDENTITY_TTL + 1
    assert app.post("/mcp", headers={"Authorization": AGENT}).status_code == 200


def test_an_mcp_session_belongs_to_the_agent_that_opened_it():
    tokens = {"Bearer grok-token-0000001": "grok", "Bearer codex-token-000001": "codex"}

    def upstream(request):
        if request.url.path == "/api/whoami":
            return httpx.Response(200, json={"name": tokens[request.headers["authorization"]], "is_human": False})
        sid = request.headers.get("mcp-session-id") or "sess-of-" + tokens[request.headers["authorization"]]
        return httpx.Response(200, json={"ok": True}, headers={"mcp-session-id": sid})
    app = TestClient(gateway.create_app("http://upstream", httpx.MockTransport(upstream), clock=Clock()))
    grok, codex = {"Authorization": "Bearer grok-token-0000001"}, {"Authorization": "Bearer codex-token-000001"}
    assert app.post("/mcp", headers=grok).headers["mcp-session-id"] == "sess-of-grok"
    for method in ("GET", "POST", "DELETE"):
        r = app.request(method, "/mcp", headers={**codex, "mcp-session-id": "sess-of-grok"})
        assert r.status_code == 403, method
    assert app.get("/mcp", headers={**grok, "mcp-session-id": "sess-of-grok"}).status_code == 200
    assert app.delete("/mcp", headers={**grok, "mcp-session-id": "sess-of-grok"}).status_code == 200


def test_an_unknown_session_id_is_404_and_a_closed_one_is_forgotten():
    tokens = {"Bearer grok-token-0000001": "grok"}

    def upstream(request):
        if request.url.path == "/api/whoami":
            return httpx.Response(200, json={"name": "grok", "is_human": False})
        return httpx.Response(200, json={"ok": True}, headers={"mcp-session-id": "s-new"})
    app = TestClient(gateway.create_app("http://upstream", httpx.MockTransport(upstream), clock=Clock()))
    grok = {"Authorization": "Bearer grok-token-0000001"}
    assert app.get("/mcp", headers={**grok, "mcp-session-id": "someone-elses"}).status_code == 404
    app.post("/mcp", headers=grok)
    assert app.delete("/mcp", headers={**grok, "mcp-session-id": "s-new"}).status_code == 200
    assert app.get("/mcp", headers={**grok, "mcp-session-id": "s-new"}).status_code == 404
    assert tokens


def test_a_node_token_reaches_only_the_node_routes(env):
    from agent_comms.api import create_app as board_app
    from agent_comms.config import create_agent
    token = create_agent(env.settings.agents_path, "laptop", "node")
    app = TestClient(gateway.create_app("http://127.0.0.1:8787", httpx.ASGITransport(app=board_app(env.board))))
    node = {"Authorization": "Bearer " + token}
    assert app.post("/api/sessions", headers=node, json={"project": "/work/repo"}).status_code == 403
    assert app.get("/api/updates", headers=node, params={"session_id": 1}).status_code == 403
    r = app.post("/api/nodes/hello", headers=node, json={"projects": ["/work/repo"], "runners": ["grok"]})
    assert r.status_code == 200, r.text
