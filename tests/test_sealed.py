"""Sealed posts must be invisible to other agents on EVERY read path."""

import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from agent_comms.api import create_app
from agent_comms.core import Conflict, Forbidden, NotFound
from agent_comms.mcp_server import build_mcp

REF = [{"kind": "commit", "path": "/work/repo", "rev": "abc123"}]
SECRET = "SECRET-FINDING-TEXT"


def _setup(env):
    tid = env.thread("review")
    task = env.accepted_task(tid, title="feature")
    sealed = env.post("codex", tid, SECRET, "finding", task_id=task, refs=REF, sealed=True, to=["codex", "grok"])
    return tid, task, sealed["id"]


def _all_read_paths(env, name, tid, pid):
    """Yield every serialized payload `name` can obtain that might contain the post."""
    b, p, s = env.board, env.p[name], env.sid[name]
    yield b.read_updates(p, s)
    yield b.read_updates(p, s, thread_id=tid, history=True)
    yield b.read_updates(p, s, only="addressed")
    yield b.list_posts(p, tid)
    yield b.snapshot(p)
    yield b.snapshot(p, closed_threads=True)
    try:
        yield b.get_post(p, pid)
    except NotFound:
        yield {}


def _mcp_read(env, name, tid, monkeypatch):
    from mcp import Client

    monkeypatch.setenv("AGENT_COMMS_TOKEN", env.tokens[name])
    mcp = build_mcp(env.board, "stdio")

    async def go():
        async with Client(mcp) as c:
            await c.call_tool("board_register", {"project": "/work/repo"})
            a = await c.call_tool("board_read_updates", {})
            b = await c.call_tool("board_read_updates", {"thread_id": tid, "history": True})
            return a.content[0].text + b.content[0].text

    return asyncio.run(go())


def _http_reads(env, name, tid, pid):
    client = TestClient(create_app(env.board))
    h = {"Authorization": f"Bearer {env.tokens[name]}", "X-Board-Session": str(env.sid[name])}
    return "".join(client.get(u, headers=h).text for u in (
        "/api/updates", f"/api/threads/{tid}/posts", f"/api/posts/{pid}", "/api/state",
        f"/api/updates?thread_id={tid}&history=true"))


@pytest.mark.parametrize("viewer", ["claude", "grok"])
def test_sealed_hidden_on_every_read_path(env, viewer, monkeypatch):
    tid, task, pid = _setup(env)
    for payload in _all_read_paths(env, viewer, tid, pid):
        assert SECRET not in json.dumps(payload)
    assert SECRET not in _http_reads(env, viewer, tid, pid)
    assert SECRET not in _mcp_read(env, viewer, tid, monkeypatch)
    with pytest.raises(NotFound):
        env.board.get_post(env.p[viewer], pid)


@pytest.mark.parametrize("viewer", ["codex", "human"])
def test_sealed_visible_to_author_and_human(env, viewer, monkeypatch):
    tid, task, pid = _setup(env)
    assert SECRET in json.dumps(env.board.list_posts(env.p[viewer], tid))
    assert SECRET in json.dumps(env.board.snapshot(env.p[viewer]))
    assert SECRET in _http_reads(env, viewer, tid, pid)
    assert env.board.get_post(env.p[viewer], pid)["sealed"] is True


def test_auto_unseal_when_every_named_reviewer_posted(env):
    tid, task, pid = _setup(env)
    # a non-finding or unsealed finding from grok does not count
    env.post("grok", tid, "I'll look", "status", task_id=task)
    env.post("grok", tid, "open finding", "finding", task_id=task, refs=REF)
    assert SECRET not in json.dumps(env.board.list_posts(env.p["claude"], tid))
    r = env.post("grok", tid, "GROK-SEALED", "finding", task_id=task, refs=REF, sealed=True, to=["codex", "grok"])
    assert set(r["auto_unsealed_post_ids"]) == {pid, r["id"]}
    posts = json.dumps(env.board.list_posts(env.p["claude"], tid))
    assert SECRET in posts and "GROK-SEALED" in posts
    assert env.board.get_post(env.p["claude"], pid)["unsealed_by"] == "auto:reviewers"


def test_sealed_post_on_other_task_does_not_count(env):
    tid, task, pid = _setup(env)
    other = env.accepted_task(tid, title="other")
    env.post("grok", tid, "elsewhere", "finding", task_id=other, refs=REF, sealed=True, to=["grok"])
    assert env.board.get_post(env.p["human"], pid)["sealed"] is True


def test_only_human_unseals(env):
    tid, task, pid = _setup(env)
    with pytest.raises(Forbidden):
        env.board.unseal(env.p["codex"], pid)
    env.board.unseal(env.p["human"], pid)
    assert env.board.get_post(env.p["claude"], pid)["body"] == SECRET
    with pytest.raises(Conflict):
        env.board.unseal(env.p["human"], pid)


def test_unsealed_post_reappears_for_readers_who_already_acked(env):
    tid, task, pid = _setup(env)
    env.post("codex", tid, "public note")
    r = env.board.read_updates(env.p["claude"], env.sid["claude"])
    assert [p["body"] for p in r["posts"]] == ["public note"]
    r = env.board.read_updates(env.p["claude"], env.sid["claude"], ack_through=r["ack_through"])
    assert r["posts"] == []
    env.board.unseal(env.p["human"], pid)
    r = env.board.read_updates(env.p["claude"], env.sid["claude"])
    assert [p["id"] for p in r["posts"]] == [pid] and r["posts"][0]["was_sealed"]
