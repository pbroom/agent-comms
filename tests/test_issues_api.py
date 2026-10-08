"""Shared issues cross the real HTTP and MCP authentication boundaries."""
import asyncio
import json

from fastapi.testclient import TestClient
from mcp import Client

from agent_comms.api import create_app
from agent_comms.mcp_server import build_mcp


def headers(env, agent="codex"):
    return {"Authorization": f"Bearer {env.tokens[agent]}", "X-Board-Session": str(env.sid[agent])}


def test_http_shared_issue_lifecycle(env):
    client = TestClient(create_app(env.board))
    first, second = env.thread("first"), env.thread("second")
    source = env.post("codex", first)["id"]
    other = env.post("claude", second)["id"]
    response = client.post("/api/issues", headers=headers(env), json={
        "title": "Repository permission", "body": "Git metadata is read-only", "thread_id": first, "post_id": source})
    assert response.status_code == 200, response.text
    issue_id = response.json()["id"]
    path = f"/api/issues/{issue_id}"
    linked = client.post(path + "/links", headers=headers(env, "claude"), json={"thread_id": second, "post_id": other})
    assert linked.status_code == 200
    assert {(l["thread_id"], l["post_id"]) for l in linked.json()["links"]} == {(first, source), (second, other)}
    for kind in ("evidence", "proposal"):
        response = client.post(path + "/comments", headers=headers(env, "claude"), json={"body": "Scoped fix", "kind": kind})
        assert response.status_code == 200
    found = client.get("/api/issues", headers=headers(env), params={"query": "permission", "thread_id": second}).json()
    assert [i["id"] for i in found] == [issue_id]
    decision = {"body": "Approved for first thread only", "thread_ids": [first], "outcome": "approved"}
    assert client.post(path + "/decisions", headers=headers(env), json=decision).status_code == 403
    response = client.post(path + "/decisions", headers=headers(env, "human"), json=decision)
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "open"
    assert response.json()["needs_human"] is False
    assert response.json()["decisions"][0]["thread_ids"] == [first]
    assert client.post(path + "/resolve", headers=headers(env), json={"body": "Fixed"}).status_code == 403
    resolved = client.post(path + "/resolve", headers=headers(env, "human"), json={"body": "Verified the fix"})
    assert resolved.status_code == 200, resolved.text
    assert resolved.json()["status"] == "resolved"
    reopened = client.post(path + "/comments", headers=headers(env), json={"body": "New decision needed", "kind": "request"})
    assert reopened.json()["status"] == "open" and reopened.json()["needs_human"]


def test_http_issue_auth_sessions_and_sealed_sources(env):
    client = TestClient(create_app(env.board))
    thread = env.thread()
    payload = {"title": "Access", "body": "Public explanation", "thread_id": thread}
    assert client.get("/api/issues").status_code == 401
    assert client.post("/api/issues", json=payload).status_code == 401
    wrong_session = {**headers(env), "X-Board-Session": str(env.sid["claude"])}
    assert client.post("/api/issues", headers=wrong_session, json=payload).status_code == 403
    task = env.accepted_task(thread)
    sealed = env.post("codex", thread, "Secret evidence", "finding", sealed=True, task_id=task,
                      refs=[{"kind": "commit", "path": "/work/repo", "rev": "abc"}], to=["claude"])
    response = client.post("/api/issues", headers=headers(env), json={**payload, "post_id": sealed["id"]})
    assert response.status_code == 403
    assert client.get("/api/issues", headers=headers(env)).json() == []


def test_mcp_issue_discovery_and_collaboration(env, monkeypatch):
    monkeypatch.setenv("AGENT_COMMS_TOKEN", env.tokens["codex"])
    first, second = env.thread("first"), env.thread("second")
    mcp = build_mcp(env.board, "stdio")

    async def go():
        async with Client(mcp) as client:
            tools = {t.name: t for t in (await client.list_tools()).tools}
            expected = {"board_list_issues", "board_get_issue", "board_create_issue", "board_link_issue", "board_comment_issue"}
            assert expected <= tools.keys()
            assert "board_decide_issue" not in tools and "board_resolve_issue" not in tools
            schema = tools["board_comment_issue"].input_schema
            assert schema["properties"]["kind"]["enum"] == ["comment", "evidence", "proposal", "request"]
            await client.call_tool("board_register", {"project": "/work/repo"})

            async def call(name, args):
                result = await client.call_tool(name, args)
                assert not result.is_error, result
                return json.loads(result.content[0].text)

            created = await call("board_create_issue", {"title": "Permission", "body": "Same blocker", "thread_id": first})
            issue_id = created["id"]
            await call("board_link_issue", {"issue_id": issue_id, "thread_id": second})
            await call("board_comment_issue", {"issue_id": issue_id, "body": "Fix suggestion", "kind": "proposal"})
            found = await call("board_list_issues", {"query": "Permission", "thread_id": second})
            assert [i["id"] for i in found["issues"]] == [issue_id]
            detail = await call("board_get_issue", {"issue_id": issue_id})
            assert len(detail["links"]) == 2
            assert detail["comments"][-1]["kind"] == "proposal"
            assert detail["decisions"] == []

    asyncio.run(go())
