"""Public boundary tests: only a ChatGPT credential reaches allowed board routes."""
import importlib.util
from pathlib import Path

import httpx
import pytest
from starlette.testclient import TestClient

spec = importlib.util.spec_from_file_location("gateway", Path(__file__).parents[1] / "integrations/chatgpt/gateway.py")
gateway = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gateway)


def client(mode="mcp", identity=None):
    requests = []
    def upstream(request):
        requests.append(request)
        if request.url.path == "/api/whoami":
            return httpx.Response(200, json=identity if identity is not None else {"name": "chatgpt", "is_human": False})
        return httpx.Response(200, json={"ok": True}, headers={"mcp-session-id": "session", "set-cookie": "secret=bad"})
    return TestClient(gateway.create_app("test-chatgpt", mode, httpx.MockTransport(upstream))), requests


@pytest.mark.parametrize("token", [None, "Bearer codex", "Bearer human", "Basic test-chatgpt"])
def test_authentication_required(token):
    app, seen = client()
    assert app.post("/mcp", headers={"Authorization": token} if token else {}).status_code == 401
    assert not seen


@pytest.mark.parametrize("mode,path,method", [
    ("mcp", "/", "GET"), ("mcp", "/api/state", "GET"), ("mcp", "/mcp/", "POST"),
    ("mcp", "/%6dcp", "POST"), ("mcp", "/mcp", "OPTIONS"),
    ("actions", "/mcp", "POST"), ("actions", "/api/admin/pause", "POST"),
    ("actions", "/api/posts/1/unseal", "POST"), ("actions", "/api/tasks", "POST"),
    ("actions", "/api/state", "GET"), ("actions", "/api/tasks/1/renew", "POST"),
])
def test_routes_fail_closed(mode, path, method):
    app, seen = client(mode)
    assert app.request(method, path, headers={"Authorization": "Bearer test-chatgpt"}).status_code == 404
    assert not seen


def test_foreign_origin_rejected():
    app, seen = client()
    assert app.post("/mcp", headers={"Authorization": "Bearer test-chatgpt", "Origin": "https://evil.example"}).status_code == 403
    assert not seen


@pytest.mark.parametrize("identity", [{"name": "human", "is_human": True}, {"name": "codex", "is_human": False}])
def test_misconfigured_token_rejected(identity):
    app, seen = client(identity=identity)
    assert app.post("/mcp", headers={"Authorization": "Bearer test-chatgpt"}).status_code == 403
    assert len(seen) == 1


@pytest.mark.parametrize("mode,path,method", [
    ("mcp", "/mcp", "POST"), ("mcp", "/mcp", "GET"), ("mcp", "/mcp", "DELETE"),
    ("actions", "/api/sessions", "POST"), ("actions", "/api/updates?session_id=1", "GET"),
    ("actions", "/api/updates/ack", "POST"), ("actions", "/api/posts", "POST"),
    ("actions", "/api/tasks", "GET"), ("actions", "/api/tasks/1", "GET"),
    ("actions", "/api/tasks/1/claim", "POST"), ("actions", "/api/tasks/1/release", "POST"),
    ("actions", "/api/tasks/1/transition", "POST"),
])
def test_proxy_success_and_header_boundary(mode, path, method):
    app, seen = client(mode)
    response = app.request(method, path, headers={"Authorization": "Bearer test-chatgpt", "Host": "public.example", "X-Forwarded-Host": "evil", "Cookie": "human=secret", "MCP-Protocol-Version": "2025-03-26", "X-Board-Session": "1"})
    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert response.headers["mcp-session-id"] == "session"
    assert "set-cookie" not in response.headers
    assert "access-control-allow-origin" not in response.headers
    forwarded = seen[-1]
    assert forwarded.headers["host"] == "127.0.0.1:8787"
    assert "cookie" not in forwarded.headers and "x-forwarded-host" not in forwarded.headers
    assert forwarded.headers["mcp-protocol-version"] == "2025-03-26"
    assert forwarded.url.query == path.partition("?")[2].encode()


def test_duplicate_authorization_is_rejected():
    app, seen = client()
    response = app.post("/mcp", headers=[("Authorization", "Bearer test-chatgpt"), ("Authorization", "Bearer human")])
    assert response.status_code == 401
    assert not seen


def test_upstream_failure_does_not_leak_internal_errors():
    def unavailable(request):
        raise httpx.ConnectError("internal detail and credentials", request=request)
    app = TestClient(gateway.create_app("test-chatgpt", transport=httpx.MockTransport(unavailable)))
    response = app.post("/mcp", headers={"Authorization": "Bearer test-chatgpt"})
    assert response.status_code == 502
    assert response.json() == {"error": "upstream_unavailable"}


def test_revoked_token_cannot_reach_mcp():
    seen = []
    def revoked(request):
        seen.append(request)
        return httpx.Response(401, json={"error": "invalid_token"})
    app = TestClient(gateway.create_app("test-chatgpt", transport=httpx.MockTransport(revoked)))
    response = app.post("/mcp", headers={"Authorization": "Bearer test-chatgpt"})
    assert response.status_code == 403
    assert [request.url.path for request in seen] == ["/api/whoami"]


def test_sessionless_discover_returns_protocol_fallback_after_identity_check():
    app, seen = client()
    response = app.post("/mcp", headers={"Authorization": "Bearer test-chatgpt"},
                        json={"jsonrpc": "2.0", "id": 42, "method": "server/discover"})
    assert response.status_code == 200
    assert response.json() == {"jsonrpc": "2.0", "id": 42, "error": {"code": -32601, "message": "Method not found"}}
    assert [request.url.path for request in seen] == ["/api/whoami"]


def test_discover_does_not_bypass_identity_or_bearer():
    app, seen = client(identity={"name": "human", "is_human": True})
    message = {"jsonrpc": "2.0", "id": 1, "method": "server/discover"}
    assert app.post("/mcp", json=message).status_code == 401
    assert not seen
    assert app.post("/mcp", headers={"Authorization": "Bearer test-chatgpt"}, json=message).status_code == 403


@pytest.mark.parametrize("message,headers", [
    ({"jsonrpc": "2.0", "method": "server/discover"}, {}),
    ({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, {}),
    ({"jsonrpc": "2.0", "id": 1, "method": "server/discover"}, {"MCP-Session-Id": "existing"}),
])
def test_discover_shim_leaves_other_protocol_traffic_unchanged(message, headers):
    app, seen = client()
    response = app.post("/mcp", headers={"Authorization": "Bearer test-chatgpt", **headers}, json=message)
    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert seen[-1].url.path == "/mcp"
