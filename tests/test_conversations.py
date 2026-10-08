"""Conversation links: capture (Claude env, Codex rollout files), storage, migration and human-only exposure."""

from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent_comms import conversations as conv
from agent_comms import db
from agent_comms.api import create_app
from agent_comms.core import Board
from agent_comms.mcp_server import build_mcp

from conftest import PROJECT

CLAUDE_ID = "3f2c8a5e-1b7d-4c9e-a0f1-6d5e4c3b2a19"
THREAD_A = "01a11421-ce71-7370-a005-a5179018a42d"
THREAD_B = "01a1170a-99d1-7783-aa58-53c4aa02342a"
THREAD_C = "01a11834-828b-7470-9cbd-91ca38a3ea3f"
URL_RE = r"^(claude://resume\?session=|codex://threads/)[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"


def client_row(env, sid):
    r = env.board.conn.execute("SELECT client_kind, client_session_id FROM sessions WHERE id = ?", (sid,)).fetchone()
    return (r["client_kind"], r["client_session_id"])


def mcp_register(env, monkeypatch, agent="claude", env_value=None, **args):
    monkeypatch.setenv("AGENT_COMMS_TOKEN", env.tokens[agent])
    if env_value is None:
        monkeypatch.delenv(conv.CLAUDE_ENV, raising=False)
    else:
        monkeypatch.setenv(conv.CLAUDE_ENV, env_value)
    from mcp import Client

    mcp = build_mcp(env.board, "stdio")

    async def go():
        async with Client(mcp) as c:
            return json.loads((await c.call_tool("board_register", {"project": PROJECT, **args})).content[0].text)
    return asyncio.run(go())


# ---------------------------------------------------------------- Claude Code: inherited environment


def test_stdio_register_captures_claude_session_from_env(env, monkeypatch):
    reg = mcp_register(env, monkeypatch, env_value=CLAUDE_ID.upper())
    assert client_row(env, reg["session_id"]) == ("claude-code", CLAUDE_ID)   # stored lower-case
    assert CLAUDE_ID not in json.dumps(reg).lower()                           # not echoed to the agent


@pytest.mark.parametrize("value", ["", "not-a-uuid", CLAUDE_ID + "\n", " " + CLAUDE_ID, CLAUDE_ID + "0",
                                   CLAUDE_ID.replace("-", ""), "'; rm -rf ~; echo '", "javascript:alert(1)",
                                   "3f2c8a5e-1b7d-4c9e-a0f1-6d5e4c3b2a1g", CLAUDE_ID[:-1] + "/"])
def test_garbage_env_values_are_never_stored(env, monkeypatch, value):
    reg = mcp_register(env, monkeypatch, env_value=value)
    assert client_row(env, reg["session_id"]) == (None, None)


def test_missing_env_and_codex_identities_store_nothing(env, monkeypatch):
    assert client_row(env, mcp_register(env, monkeypatch)["session_id"]) == (None, None)
    # A Codex CLI started from a Claude Code terminal inherits the variable; it is not Codex's conversation.
    reg = mcp_register(env, monkeypatch, agent="codex", env_value=CLAUDE_ID)
    assert client_row(env, reg["session_id"]) == (None, None)


def test_resume_updates_the_conversation_and_keeps_it_without_one(env, monkeypatch):
    sid = mcp_register(env, monkeypatch, env_value=CLAUDE_ID)["session_id"]
    other = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
    assert mcp_register(env, monkeypatch, env_value=other, resume_session_id=sid)["session_id"] == sid
    assert client_row(env, sid) == ("claude-code", other)
    mcp_register(env, monkeypatch, resume_session_id=sid)
    assert client_row(env, sid) == ("claude-code", other)
    mcp_register(env, monkeypatch, env_value="garbage", resume_session_id=sid)
    assert client_row(env, sid) == ("claude-code", other)


def test_disabled_captures_nothing(env, monkeypatch):
    env.settings.conversations = {"enabled": False}
    assert client_row(env, mcp_register(env, monkeypatch, env_value=CLAUDE_ID)["session_id"]) == (None, None)


def test_http_transport_does_not_capture(env, monkeypatch):
    import httpx
    from mcp import Client
    from mcp.client.streamable_http import streamable_http_client

    monkeypatch.setenv(conv.CLAUDE_ENV, CLAUDE_ID)   # the HTTP server's own environment is not the agent's
    app = create_app(env.board)

    async def go():
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 5555))
            headers = {"Authorization": f"Bearer {env.tokens['claude']}"}
            async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8787", headers=headers) as h:
                async with Client(streamable_http_client("http://127.0.0.1:8787/mcp", http_client=h)) as c:
                    return json.loads((await c.call_tool("board_register", {"project": "/p"})).content[0].text)

    assert client_row(env, asyncio.run(go())["session_id"]) == (None, None)


def test_core_drops_invalid_client_tuples(env):
    for bad in [("claude-code", "x"), ("vscode", CLAUDE_ID), (CLAUDE_ID,), "claude-code", ("claude-code", 5)]:
        sid = env.board.register_session(env.p["claude"], PROJECT, client=bad)["session_id"]
        assert client_row(env, sid) == (None, None)


def test_http_session_body_cannot_set_a_client(env):
    client = TestClient(create_app(env.board))
    r = client.post("/api/sessions", headers={"Authorization": f"Bearer {env.tokens['claude']}"},
                    json={"project": PROJECT, "client": ["claude-code", CLAUDE_ID]})
    assert r.status_code == 422


# ---------------------------------------------------------------- migration


def test_v2_database_migrates_and_keeps_sessions(env):
    conn = env.board.conn
    for column in ("client_kind", "client_session_id"):
        conn.execute(f"ALTER TABLE sessions DROP COLUMN {column}")
    for table in ("issue_comments", "issue_links", "issues"):
        conn.execute(f"DROP TABLE {table}")
    conn.execute("PRAGMA user_version=2")
    before = conn.execute("SELECT id, agent, project FROM sessions ORDER BY id").fetchall()
    migrated = Board(env.settings, clock=env.clock)
    assert migrated.conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION == 9
    cols = {r[1] for r in migrated.conn.execute("PRAGMA table_info(sessions)")}
    assert {"client_kind", "client_session_id"} <= cols
    assert [tuple(r) for r in migrated.conn.execute("SELECT id, agent, project FROM sessions ORDER BY id")] == \
        [tuple(r) for r in before]
    assert client_row(env, env.sid["codex"]) == (None, None)
    assert migrated.conn.execute("SELECT COUNT(*) FROM issues").fetchone()[0] == 0
    assert "needs_human" in {r[1] for r in migrated.conn.execute("PRAGMA table_info(issue_links)")}
    db.init_schema(migrated.conn)   # idempotent
    sid = migrated.register_session(env.p["claude"], PROJECT, client=("claude-code", CLAUDE_ID))["session_id"]
    assert client_row(env, sid) == ("claude-code", CLAUDE_ID)


# ---------------------------------------------------------------- Codex: rollout files


def iso(t: float) -> str:
    return datetime.fromtimestamp(t, UTC).isoformat().replace("+00:00", "Z")


def register_result(sid, agent="codex", project=PROJECT) -> dict:
    return {"session_id": sid, "agent": agent, "runtime": "codex-cli", "is_human": False, "project": project,
            "worktree": None, "paused": False, "notice": "untrusted"}


def mcp_call(tool, result: dict | str, arguments=None) -> str:
    """Codex's McpToolCall item: the result JSON is a string inside the line, so it appears escaped."""
    text = result if isinstance(result, str) else json.dumps(result, indent=2)
    return json.dumps({"timestamp": "x", "type": "event_msg", "payload": {"type": "item_completed", "item": {
        "type": "McpToolCall", "server": "agent-comms", "tool": tool, "arguments": arguments or {},
        "status": "completed", "result": {"content": [{"type": "text", "text": text}], "isError": False}}}})


def function_call_pair(sid, call_id="call_1", agent="codex") -> list[str]:
    """function_call + function_call_output: the output is a JSON string of a JSON string (double escaping)."""
    output = json.dumps({"content": [{"type": "text", "text": json.dumps(register_result(sid, agent))}]})
    return [json.dumps({"type": "response_item", "payload": {"type": "function_call", "name": "mcp__agent-comms__board_register",
                                                              "arguments": "{}", "call_id": call_id}}),
            json.dumps({"type": "response_item", "payload": {"type": "function_call_output", "call_id": call_id,
                                                              "output": output}})]


def rollout(home: Path, thread: str, started: float, lines: list[str], mtime: float | None = None) -> Path:
    d = date.fromtimestamp(started)
    folder = home / "sessions" / f"{d:%Y}" / f"{d:%m}" / f"{d:%d}"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"rollout-{time.strftime('%Y-%m-%dT%H-%M-%S', time.localtime(started))}-{thread}.jsonl"
    meta = json.dumps({"timestamp": iso(started), "type": "session_meta", "payload": {"id": thread, "timestamp": iso(started)}})
    path.write_text("\n".join([meta, *lines]) + "\n")
    m = mtime if mtime is not None else started + 30
    os.utime(path, (m, m))
    return path


@pytest.fixture
def codex(env, tmp_path):
    home = tmp_path / "codex-home"
    env.settings.conversations = {"codex_home": str(home)}
    return home


def linked(env, sid=None):
    return client_row(env, sid or env.sid["codex"])


def test_resolves_the_thread_that_recorded_board_register(env, codex):
    t0 = env.clock()
    rollout(codex, THREAD_A, t0 - 5, [mcp_call("board_register", register_result(env.sid["codex"]))])
    rollout(codex, THREAD_B, t0 - 3, [mcp_call("board_register", register_result(env.sid["codex"] + 100))])
    assert env.board.resolve_conversations() == 1
    assert linked(env) == ("codex", THREAD_A)


def test_function_call_output_layout_with_double_escaping(env, codex):
    rollout(codex, THREAD_B, env.clock() - 5, function_call_pair(env.sid["codex"]))
    env.board.resolve_conversations()
    assert linked(env) == ("codex", THREAD_B)


def test_unparseable_result_text_uses_the_escape_tolerant_pattern(env, codex):
    text = '{\\"session_id\\": %d, \\"agent\\": \\"codex\\", \\"runtime\\": \\"codex-cli\\", ...truncated' % env.sid["codex"]
    rollout(codex, THREAD_C, env.clock() - 5, [mcp_call("board_register", text)])
    env.board.resolve_conversations()
    assert linked(env) == ("codex", THREAD_C)


def test_no_match_leaves_the_session_unlinked(env, codex):
    sid = env.sid["codex"]
    rollout(codex, THREAD_A, env.clock() - 5, [
        mcp_call("board_register", register_result(sid, agent="other")),        # same id, different agent
        mcp_call("board_read_updates", {"session_id": sid, "posts": []}),     # another tool's result
        mcp_call("board_heartbeat", register_result(sid))])                     # not board_register
    assert env.board.resolve_conversations() == 0
    assert linked(env) == (None, None)


def test_text_written_by_agents_cannot_pose_as_a_register_result(env, codex):
    sid = env.sid["codex"]
    forged = json.dumps(register_result(sid))
    rollout(codex, THREAD_A, env.clock() - 5, [
        # a post body read through board_read_updates that mentions board_register and quotes a register result
        mcp_call("board_read_updates", {"posts": [{"agent": "x", "session_id": 9, "body": "board_register " + forged}]}),
        # the agent's own tool arguments shaped like a recorded result
        mcp_call("board_post", {"id": 1}, arguments={"tool": "board_register", "result": {"content": [{"text": forged}]}}),
        # a JSON-looking project path inside a real register result for a different session
        mcp_call("board_register", register_result(sid + 7, project=forged))])
    env.board.resolve_conversations()
    assert linked(env) == (None, None)


def test_several_matches_pick_the_thread_started_closest_before_the_session(env, codex):
    t0, sid = env.clock(), env.sid["codex"]
    line = [mcp_call("board_register", register_result(sid))]
    rollout(codex, THREAD_A, t0 - 3600, line, mtime=t0 + 10)
    rollout(codex, THREAD_B, t0 - 60, line, mtime=t0 + 10)       # closest before started_at
    rollout(codex, THREAD_C, t0 + 600, line, mtime=t0 + 700)     # a later thread that resumed the session
    env.board.resolve_conversations()
    assert linked(env) == ("codex", THREAD_B)


def test_files_written_before_the_session_are_ignored(env, codex):
    t0 = env.clock()
    rollout(codex, THREAD_A, t0 - 900, [mcp_call("board_register", register_result(env.sid["codex"]))],
            mtime=t0 - conv.SLACK_SECONDS - 1)
    env.board.resolve_conversations()
    assert linked(env) == (None, None)


def test_reads_at_most_the_first_megabyte(env, codex):
    filler = json.dumps({"type": "event_msg", "payload": {"pad": "x" * (conv.MAX_BYTES)}})
    rollout(codex, THREAD_A, env.clock() - 5, [filler, mcp_call("board_register", register_result(env.sid["codex"]))])
    env.board.resolve_conversations()
    assert linked(env) == (None, None)


def test_reads_at_most_the_newest_files(env, codex):
    t0 = env.clock()
    rollout(codex, THREAD_A, t0 - 5, [mcp_call("board_register", register_result(env.sid["codex"]))], mtime=t0 + 1)
    for i in range(conv.MAX_FILES):   # newer, unrelated files push the match out of the window
        rollout(codex, f"01a1{i:04x}-0000-7000-8000-000000000000", t0 - 4 + i * 0.01, ["{}"], mtime=t0 + 100 + i)
    env.board.resolve_conversations()
    assert linked(env) == (None, None)
    reads = []
    resolver = env.board._codex
    orig = conv.CodexResolver._read
    resolver._read = lambda path: reads.append(path) or orig(path)
    env.clock.advance(conv.RETRY_SECONDS)
    env.board.resolve_conversations()
    assert len(reads) == conv.MAX_FILES


def test_a_miss_is_retried_at_most_once_a_minute(env, codex):
    sid = env.sid["codex"]
    assert env.board.resolve_conversations() == 0
    rollout(codex, THREAD_A, env.clock() - 5, [mcp_call("board_register", register_result(sid))])
    env.clock.advance(conv.RETRY_SECONDS - 1)
    assert env.board.resolve_conversations() == 0 and linked(env) == (None, None)
    env.clock.advance(1)
    assert env.board.resolve_conversations() == 1 and linked(env) == ("codex", THREAD_A)


def test_stale_sessions_and_non_codex_sessions_are_not_looked_up(env, codex):
    t0 = env.clock()
    rollout(codex, THREAD_A, t0 - 5, [mcp_call("board_register", register_result(env.sid["claude"], agent="claude"))])
    env.board.resolve_conversations()
    assert linked(env, env.sid["claude"]) == (None, None)   # claude-code runtime: env capture only
    env.clock.advance(conv.RECENT_SECONDS + 1)
    rollout(codex, THREAD_B, t0 - 5, [mcp_call("board_register", register_result(env.sid["codex"]))],
            mtime=env.clock())
    env.board.resolve_conversations()
    assert linked(env) == (None, None)


def test_disabled_never_scans(env, codex, monkeypatch):
    env.settings.conversations = {"enabled": False, "codex_home": str(codex)}
    rollout(codex, THREAD_A, env.clock() - 5, [mcp_call("board_register", register_result(env.sid["codex"]))])
    monkeypatch.setattr(conv.CodexResolver, "resolve", lambda *a: pytest.fail("scanned while disabled"))
    assert env.board.resolve_conversations() == 0
    human = TestClient(create_app(env.board)).get("/api/state", headers={"Authorization": f"Bearer {env.tokens['human']}"})
    assert all(s["conversation"] is None for s in human.json()["sessions"])


def test_missing_codex_home_is_harmless(env, tmp_path):
    env.settings.conversations = {"codex_home": str(tmp_path / "nope")}
    assert env.board.resolve_conversations() == 0


@pytest.mark.parametrize("table", [{"enabled": "yes"}, {"codex_home": "relative/path"}, {"codex_home": 5},
                                   {"surprise": 1}, []])
def test_config_validation(table):
    with pytest.raises(ValueError):
        conv.ConversationConfig.from_dict(table)


def test_committed_board_toml_defaults_validate():
    from agent_comms.config import REPO_ROOT, Settings
    from agent_comms.core import check_reloadable

    s = Settings.load(REPO_ROOT / "board.toml", local=False)
    check_reloadable(s)
    assert conv.ConversationConfig.from_dict(s.conversations) == conv.ConversationConfig(enabled=True, codex_home="")
    s.conversations = {"enabled": "nope"}
    with pytest.raises(ValueError):
        check_reloadable(s)
    assert conv.config_of(s).enabled is False   # an invalid table turns the feature off, never on


def test_codex_home_default(monkeypatch):
    monkeypatch.delenv("CODEX_HOME", raising=False)
    assert conv.ConversationConfig().codex_dir() == Path("~/.codex").expanduser()
    assert conv.ConversationConfig().codex_dir({"CODEX_HOME": "/x/codex"}) == Path("/x/codex")
    assert conv.ConversationConfig(codex_home="/y").codex_dir({"CODEX_HOME": "/x"}) == Path("/y")


# ---------------------------------------------------------------- exposure


def state(env, who):
    r = TestClient(create_app(env.board)).get("/api/state", headers={"Authorization": f"Bearer {env.tokens[who]}"})
    assert r.status_code == 200
    return r.json()


def linked_env(env, codex):
    csid = env.board.register_session(env.p["claude"], PROJECT, worktree="/work/wt",
                                      client=("claude-code", CLAUDE_ID))["session_id"]
    rollout(codex, THREAD_A, env.clock() - 5, [mcp_call("board_register", register_result(env.sid["codex"]))])
    tid = env.thread()
    task = env.accepted_task(tid)
    assert env.board.claim_task(env.p["claude"], csid, task)["status"] == "working"
    idle = env.accepted_task(tid)
    return csid, task, idle


def test_human_state_has_links_built_from_validated_ids(env, codex):
    import re

    csid, task, idle = linked_env(env, codex)
    out = state(env, "human")
    sessions = {s["id"]: s for s in out["sessions"]}
    assert sessions[csid]["conversation"] == {"app": "Claude", "url": f"claude://resume?session={CLAUDE_ID}",
                                              "resume_command": f"claude --resume {CLAUDE_ID}", "cwd": "/work/wt"}
    assert sessions[env.sid["codex"]]["conversation"] == {
        "app": "ChatGPT", "url": f"codex://threads/{THREAD_A}", "resume_command": f"codex resume {THREAD_A}", "cwd": PROJECT}
    assert sessions[env.sid["grok"]]["conversation"] is None
    for s in out["sessions"]:
        assert "client_kind" not in s and "client_session_id" not in s
        if s["conversation"]:
            assert re.match(URL_RE, s["conversation"]["url"])
    tasks = {t["id"]: t for t in out["threads"][0]["tasks"]}
    assert tasks[task]["owner_conversation"]["url"] == f"claude://resume?session={CLAUDE_ID}"
    assert tasks[idle]["owner_conversation"] is None


def test_task_link_ends_with_the_lease(env, codex):
    csid, task, _ = linked_env(env, codex)
    env.board.transition_task(env.p["claude"], csid, task, "done")
    tasks = {t["id"]: t for t in state(env, "human")["threads"][0]["tasks"]}
    assert tasks[task]["owner_conversation"] is None


def test_a_tampered_row_never_becomes_a_url(env):
    sid = env.sid["claude"]
    env.board.conn.execute("UPDATE sessions SET client_kind='claude-code', client_session_id=? WHERE id=?",
                           ("javascript:alert(1)", sid))
    sessions = {s["id"]: s for s in state(env, "human")["sessions"]}
    assert sessions[sid]["conversation"] is None


def test_agents_never_see_conversation_ids(env, codex):
    linked_env(env, codex)
    state(env, "human")   # resolves the Codex thread
    assert linked(env) == ("codex", THREAD_A)
    for who in ("claude", "codex", "grok"):
        out = state(env, who)
        text = json.dumps(out)
        assert CLAUDE_ID not in text and THREAD_A not in text
        assert "conversation" not in text and "client_kind" not in text
    # Agents' views never trigger a rollout scan either.
    env.board._codex = None
    state(env, "codex")
    assert env.board._codex is None
