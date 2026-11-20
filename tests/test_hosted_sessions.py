"""Hosted sessions (PR 2): the hub registry (hosted.py), the node protocol and its loop (node_sessions.py, node.py),
the human-only routes and `board session`. Children are fakes with a scripted stdout (test_session_host), except the
tests at the end, which run the stand-in script (stand_in_claude.py) through real pipes. Never a real agent CLI."""

import io
import json
import os
import signal
import subprocess
import sys
import threading
import time
import urllib.error
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from agent_comms import cli, gateway, hosted, nodes, providers, runner_preflight as rp
from agent_comms.api import create_app
from agent_comms.config import create_agent
from agent_comms.core import Forbidden
from agent_comms.dispatch import DispatchConfig
from agent_comms.node import SELF_STOP_SECONDS, Node
from agent_comms.node_sessions import HANDED_FILE, LOST, SessionRunner
from agent_comms.providers.claude import ClaudeSession
from agent_comms.session_host import SessionHost

from conftest import PROJECT, make_env
from test_dispatch import FakeProcs, FakeSpawner
from test_nodes import Clock
from test_session_host import SHIPPED, FakeSessionChild, init, result, value_after

ROOT = Path(__file__).resolve().parents[1]
P2, P3 = "/work/second", "/work/third"
UNKNOWN = "9e8d7c6b-5a49-4382-a1b0-c9d8e7f6a5b4"
HUB_DISPATCH = {"nodes": {"laptop": {"projects": [PROJECT, P2, P3], "agents": ["claude"]}}}


def node_config(env, **over) -> DispatchConfig:
    d = {"runners": {"claude-code": SHIPPED}, "worktrees": {p: str(env.work) for p in (PROJECT, P2, P3)},
         "timeout_minutes": 30, "kill_grace_seconds": 10, "sessions": {"enabled": True}}
    return DispatchConfig.from_dict(d | over)


def client(env, name) -> TestClient:
    return TestClient(env.app, base_url="http://127.0.0.1:8787",
                      headers={"Authorization": "Bearer " + env.tokens[name]})


def make_node(env, name="laptop", config=None, log_dir=None) -> Node:
    spawner = FakeSpawner(child=FakeSessionChild)
    procs = FakeProcs(spawner)
    node = Node(client(env, name), config or node_config(env), log_dir or env.tmp / f"{name}-logs",
                spawner=FakeSpawner(), clock=Clock(env), probe=procs.probe, start_of=procs.start,
                signal_group=lambda pid, sig: env.signals.append((pid, sig)), sleep=lambda s: env.clock.advance(s),
                session_spawner=spawner, session_group_alive=lambda pid: pid in env.groups)
    node.session_spawner, node.procs = spawner, procs
    if node.sessions is not None:
        node.sessions.long_poll = node.sessions.sync_seconds = 0     # every pass fetches, none waits at the hub
    return node


@pytest.fixture
def senv(tmp_path):
    env = make_env(tmp_path, dispatch=HUB_DISPATCH)
    env.tmp, env.signals = tmp_path, []
    env.groups = set()      # fake pids whose process group still has a member (none: a group goes with its leader)
    env.work = tmp_path / "checkout"
    env.work.mkdir()
    for name in ("laptop", "desktop"):
        env.tokens[name] = create_agent(env.settings.agents_path, name, "node")
        env.p[name] = env.board.authenticate(env.tokens[name])
    env.app = create_app(env.board)
    env.human, env.agent, env.laptop = client(env, "human"), client(env, "claude"), client(env, "laptop")
    env.node = make_node(env)
    env.node.hello()
    return env


def start(env, text="first", project=PROJECT, **kw) -> dict:
    r = env.human.post("/api/hosted-sessions", json={"project": project, "message": text, **kw})
    assert r.status_code == 200, r.text
    return r.json()["session"]


def send(env, sid, text) -> dict:
    r = env.human.post(f"/api/hosted-sessions/{sid}/messages", json={"message": text})
    assert r.status_code == 200, r.text
    return r.json()["message"]


def show(env, sid) -> dict:
    r = env.human.get(f"/api/hosted-sessions/{sid}")
    assert r.status_code == 200, r.text
    return r.json()


def msg(env, sid, n) -> dict:
    return next(m for m in show(env, sid)["messages"] if m["n"] == n)


def tick(env, node=None, times=1) -> None:
    for _ in range(times):
        (node or env.node).sessions.pass_once()


def children(env, node=None) -> list:
    return (node or env.node).session_spawner.children


def run_turn(env, sid, text="ok", node=None, **kw) -> None:
    """The session's process answers the turn it was given; the node reports it."""
    child = next(c for c in reversed(children(env, node)) if value_after_any(env, c, node) == sid)
    child.emit(init(sid), result(sid, text=text, **kw))
    tick(env, node)


def value_after_any(env, child, node=None) -> str:
    argv = (node or env.node).session_spawner.calls[children(env, node).index(child)]["argv"]
    return value_after(argv, "--resume" if "--resume" in argv else "--session-id")


# ------------------------------------------------------------ who may do what


def test_only_the_human_can_start_message_read_or_close_a_hosted_session(senv):
    sid = start(senv)["session_id"]
    calls = [("get", "/api/hosted-sessions", None), ("post", "/api/hosted-sessions", {"project": PROJECT, "message": "x"}),
             ("get", f"/api/hosted-sessions/{sid}", None), ("get", f"/api/hosted-sessions/{sid}/messages/1", None),
             ("post", f"/api/hosted-sessions/{sid}/messages", {"message": "x"}),
             ("post", f"/api/hosted-sessions/{sid}/close", None)]
    for who in ("claude", "codex", "laptop"):
        c = client(senv, who)
        for method, path, body in calls:
            r = c.request(method, path, json=body)
            assert r.status_code == 403, (who, method, path, r.text)
    anonymous = TestClient(senv.app, base_url="http://127.0.0.1:8787")
    assert all(anonymous.request(m, p, json=b).status_code == 401 for m, p, b in calls)
    view = show(senv, sid)
    assert (view["state"], view["queued"], view["stop"]) == ("idle", 1, None), "nothing an agent or node sent took effect"
    for fn, args in ((hosted.start, (PROJECT, "x")), (hosted.send, (sid, "x")), (hosted.show, (sid,)),
                     (hosted.close, (sid,)), (hosted.list_sessions, ()), (hosted.message, (sid, 1))):
        with pytest.raises(Forbidden):
            fn(senv.board, senv.p["claude"], *args)


def test_no_mcp_tool_reaches_hosted_sessions():
    source = (ROOT / "agent_comms" / "mcp_server.py").read_text()
    assert "hosted" not in source and "host.session" not in source and "host.msg" not in source


def test_only_a_node_speaks_the_session_protocol_and_only_for_its_own_sessions(senv):
    sid = start(senv)["session_id"]
    report = {"events": [{"kind": "delivered", "message": 1}]}
    for who in ("claude", "human"):
        c = client(senv, who)
        assert c.get("/api/nodes/sessions").status_code == 403
        assert c.post(f"/api/nodes/sessions/{sid}", json=report).status_code == 403
    desktop = client(senv, "desktop")
    assert desktop.get("/api/nodes/sessions").json()["sessions"] == [], "another node is not shown the session"
    r = desktop.post(f"/api/nodes/sessions/{sid}", json=report)
    assert r.status_code == 404 and "not hosted on this node" in r.text
    assert msg(senv, sid, 1)["state"] == "queued"
    assert senv.laptop.post(f"/api/nodes/sessions/{UNKNOWN}", json=report).status_code == 404
    assert senv.laptop.post("/api/nodes/sessions/not-a-uuid", json=report).status_code == 404
    [listed] = senv.laptop.get("/api/nodes/sessions").json()["sessions"]
    assert listed["session_id"] == sid and listed["messages"] == [{"n": 1, "kind": "user", "state": "queued",
                                                                   "text": "first"}]
    assert senv.laptop.post(f"/api/nodes/sessions/{sid}", json=report).status_code == 200
    assert msg(senv, sid, 1)["state"] == "delivered"


def test_the_gateway_passes_the_nodes_session_routes_and_never_the_humans(senv):
    assert gateway.allowed("GET", "/api/nodes/sessions")
    assert gateway.allowed("POST", f"/api/nodes/sessions/{UNKNOWN}")
    assert not gateway.allowed("POST", "/api/nodes/sessions/../hosted-sessions")
    for method in ("GET", "POST", "PUT", "DELETE"):
        for path in ("/api/hosted-sessions", f"/api/hosted-sessions/{UNKNOWN}", f"/api/hosted-sessions/{UNKNOWN}/close",
                     f"/api/hosted-sessions/{UNKNOWN}/messages", f"/api/hosted-sessions/{UNKNOWN}/messages/1"):
            assert not gateway.allowed(method, path)
    sid = start(senv)["session_id"]
    app = TestClient(gateway.create_app("http://127.0.0.1:8787", httpx.ASGITransport(app=senv.app)))
    node = {"Authorization": "Bearer " + senv.tokens["laptop"]}
    r = app.get("/api/nodes/sessions", headers=node, params={"wait": 0})
    assert r.status_code == 200 and r.json()["sessions"][0]["session_id"] == sid
    assert app.post(f"/api/nodes/sessions/{sid}", headers=node, json={"events": []}).status_code == 200
    assert app.get("/api/hosted-sessions", headers=node).status_code == 404
    human = {"Authorization": "Bearer " + senv.tokens["human"]}
    assert app.get("/api/hosted-sessions", headers=human).status_code == 404
    assert app.get("/api/nodes/sessions", headers=human).status_code == 403, "the gateway refuses a human token"


# ------------------------------------------------------------ placement


def test_a_session_is_placed_only_where_the_hubs_grant_and_a_live_hosting_node_allow(senv):
    def refused(**body):
        r = senv.human.post("/api/hosted-sessions", json={"project": PROJECT, "message": "x"} | body)
        assert r.status_code == 409, r.text
        return r.json()["message"]

    assert "no node may run claude in /work/elsewhere" in refused(project="/work/elsewhere")
    assert "no node may run grok" in refused(agent="grok"), "the grant names agents, too"
    assert "no node may run codex" in refused(agent="codex")

    # The grant covers it, but no node that hosts sessions offers it: a node without [dispatch.sessions] enabled...
    plain = make_node(senv, config=node_config(senv, sessions={"enabled": False}), log_dir=senv.tmp / "plain-logs")
    assert plain.sessions is None
    plain.hello()
    assert "sessions" not in nodes.nodes(senv.board)[0], "the field is only sent when enabled (older hubs)"
    assert "no connected node hosts sessions for claude" in refused()
    # ... one that does not have the project ...
    make_node(senv, config=node_config(senv, worktrees={P2: str(senv.work)}), log_dir=senv.tmp / "p2-logs").hello()
    assert "no connected node hosts sessions" in refused()
    # ... and one that has gone quiet.
    senv.node.hello()
    assert nodes.nodes(senv.board)[0]["sessions"] == {"max_live": 4}
    senv.clock.advance(nodes.NODE_STALE_SECONDS + 1)
    assert "no connected node hosts sessions" in refused()
    senv.node.hello()
    assert start(senv)["node"] == "laptop"


def test_a_grant_by_checkout_root_covers_a_project_inside_it(senv):
    senv.settings.dispatch = {"nodes": {"laptop": {"checkout_roots": ["/work"], "agents": ["claude-code"]}}}
    assert start(senv)["node"] == "laptop", "nodes.grant_covers, and the agent by its runtime, as for a node run"
    r = senv.human.post("/api/hosted-sessions", json={"project": "/elsewhere/repo", "message": "x"})
    assert r.status_code == 409


def test_a_project_the_node_does_not_have_ends_the_session_with_the_reason(senv):
    """The hub goes by what the node offered; the node checks its own checkout again when it opens the session."""
    sid = start(senv)["session_id"]
    senv.node.sessions.host.config.worktrees.pop(PROJECT)
    tick(senv)
    view = show(senv, sid)
    assert view["state"] == "ended" and "no run directory" in view["error"]
    assert msg(senv, sid, 1)["state"] == "failed" and "session ended" in msg(senv, sid, 1)["detail"]
    assert children(senv) == []
    tick(senv, times=2)
    assert senv.node.sessions.pending == [] and not senv.node.sessions.active()


def test_one_session_per_agent_and_project_until_it_has_ended(senv):
    first = start(senv)
    r = senv.human.post("/api/hosted-sessions", json={"project": PROJECT + "/", "message": "again"})
    assert r.status_code == 409 and first["session_id"] in r.json()["message"]
    assert "board session send" in r.json()["message"]
    other = start(senv, project=P2)
    assert other["session_id"] != first["session_id"], "another project is another session"
    assert senv.human.post(f"/api/hosted-sessions/{first['session_id']}/close").status_code == 200
    tick(senv)
    assert show(senv, first["session_id"])["state"] == "ended"
    again = start(senv)
    assert again["session_id"] not in (first["session_id"], other["session_id"])
    listed = senv.human.get("/api/hosted-sessions").json()["sessions"]
    assert {s["session_id"] for s in listed} == {other["session_id"], again["session_id"]}
    everything = senv.human.get("/api/hosted-sessions", params={"all": "true"}).json()["sessions"]
    assert len(everything) == 3


def test_the_default_agent_is_the_one_claude_agent_or_must_be_named(senv):
    assert start(senv)["agent"] == "claude"
    create_agent(senv.settings.agents_path, "claude-two", "claude-code")
    r = senv.human.post("/api/hosted-sessions", json={"project": P2, "message": "x"})
    assert r.status_code == 400 and "claude, claude-two" in r.json()["message"]
    assert start(senv, project=P2, agent="claude")["agent"] == "claude"
    r = senv.human.post("/api/hosted-sessions", json={"project": P3, "message": "x", "agent": "nobody"})
    assert r.status_code == 404
    r = senv.human.post("/api/hosted-sessions", json={"project": P3, "message": "x", "agent": "laptop"})
    assert r.status_code == 404, "a node identity is not an agent"


def test_start_and_send_refuse_what_cannot_be_a_message_or_a_model(senv):
    def status(**body):
        return senv.human.post("/api/hosted-sessions", json={"project": PROJECT, "message": "x"} | body).status_code

    assert status(message="  ") == 400 and status(message="x" * (hosted.MESSAGE_MAX_BYTES + 1)) == 400
    assert status(project="relative/path") == 400 and status(model="--dangerously-skip-permissions") == 400
    s = start(senv, model="haiku")
    assert s["model"] == "haiku"
    sid = s["session_id"]
    assert senv.human.post(f"/api/hosted-sessions/{sid}/messages", json={"message": ""}).status_code == 400
    for _ in range(hosted.MAX_QUEUED - 1):
        send(senv, sid, "more")
    r = senv.human.post(f"/api/hosted-sessions/{sid}/messages", json={"message": "one too many"})
    assert r.status_code == 409 and "messages waiting" in r.json()["message"]
    assert senv.human.get(f"/api/hosted-sessions/{UNKNOWN}").status_code == 404
    assert senv.human.get(f"/api/hosted-sessions/{sid}/messages/999").status_code == 404
    [listed] = senv.laptop.get("/api/nodes/sessions").json()["sessions"]
    assert listed["model"] == "haiku"
    assert [("text" in m) for m in listed["messages"]] == [True] + [False] * (hosted.MAX_QUEUED - 1), \
        "only the next message's text goes out with each fetch"


# ------------------------------------------------------------ turns


def test_an_idle_sessions_message_is_written_on_the_nodes_next_pass_and_a_busy_ones_queues(senv):
    s = start(senv, "first", model="haiku")
    sid = s["session_id"]
    assert (s["node"], s["state"], s["queued"], s["turns"], s["pid"]) == ("laptop", "idle", 1, 0, None)
    tick(senv)
    [child] = children(senv)
    assert child.turns() == ["first"], "one pass of the node: fetched, started, written"
    argv = senv.node.session_spawner.calls[0]["argv"]
    assert value_after(argv, "--session-id") == sid and value_after(argv, "--model") == "haiku"
    view = show(senv, sid)
    assert (view["state"], view["in_flight"], view["queued"], view["pid"]) == ("starting", 1, 0, child.pid)
    assert msg(senv, sid, 1)["state"] == "delivered"

    second = send(senv, sid, "second")
    assert (second["n"], second["state"]) == (2, "queued")
    child.emit(init(sid))
    tick(senv, times=3)
    assert child.turns() == ["first"], "the second message waits at the hub while the first turn is in flight"
    view = show(senv, sid)
    assert (view["state"], view["queued"], view["in_flight"]) == ("running", 1, 1)

    child.emit(result(sid, text="first reply"))
    tick(senv)
    assert child.turns() == ["first", "second"], "written in the pass that saw the first turn end"
    view = show(senv, sid)
    assert (view["turns"], view["reply"], view["reply_for"], view["in_flight"], view["queued"]) == (
        1, "first reply", 1, 2, 0)
    assert view["last_result"]["ok"] is True and view["last_result"]["meta"]["duration_ms"] == 2537
    assert [(m["n"], m["state"]) for m in view["messages"]] == [(1, "done"), (2, "delivered")]
    assert len(children(senv)) == 1, "one process for the whole session"

    run_turn(senv, sid, "second reply")
    view = show(senv, sid)
    assert (view["state"], view["turns"], view["reply"], view["reply_for"]) == ("idle", 2, "second reply", 2)
    assert not senv.node.sessions.active(), "nothing left to do: the next fetch may wait at the hub"


def test_a_failed_turn_is_marked_failed_with_the_reason_and_never_retried(senv):
    sid = start(senv)["session_id"]
    tick(senv)
    [child] = children(senv)
    child.emit(init(sid))
    tick(senv)
    child.code = 1      # the process dies mid-turn
    tick(senv)
    m = msg(senv, sid, 1)
    assert m["state"] == "failed" and "exited (code 1)" in m["detail"]
    view = show(senv, sid)
    assert (view["state"], view["pid"], view["turns"], view["reply"]) == ("idle", None, 1, None)
    assert view["last_result"]["ok"] is False
    tick(senv, times=3)
    assert len(children(senv)) == 1, "nothing is started again for a lost turn"
    send(senv, sid, "again")
    tick(senv)
    assert children(senv)[1].turns() == ["again"]
    assert value_after(senv.node.session_spawner.calls[1]["argv"], "--resume") == sid

    # The agent itself reports a failed turn: failed too, with what it said.
    children(senv)[1].emit(init(sid), result(sid, subtype="error_during_execution", is_error=True, text="API error"))
    tick(senv)
    m = msg(senv, sid, 2)
    assert m["state"] == "failed" and m["detail"] == "the agent reported that the turn failed (error_during_execution)"
    assert show(senv, sid)["reply"] == "API error"


def test_a_turn_with_denied_tools_leaves_the_session_waiting_and_does_not_hold_the_queue(senv):
    sid = start(senv)["session_id"]
    send(senv, sid, "next")
    tick(senv)
    [child] = children(senv)
    child.emit(init(sid), result(sid, denials=("Write", "Bash"), text="could not write"))
    tick(senv)
    assert msg(senv, sid, 1)["state"] == "done" and msg(senv, sid, 1)["denials"] == ["Write", "Bash"]
    assert child.turns() == ["first", "next"], "the next message's turn started all the same"
    assert "TOOL-INPUT-SECRET" not in json.dumps(show(senv, sid)), "names only"
    stored = " ".join(r[0] for r in senv.board.conn.execute("SELECT value FROM board_state"))
    assert "TOOL-INPUT-SECRET" not in stored
    run_turn(senv, sid, "done")
    assert show(senv, sid)["denials"] == []


def test_a_denial_with_nothing_queued_shows_the_session_waiting(senv):
    sid = start(senv)["session_id"]
    tick(senv)
    run_turn(senv, sid, "no", denials=("Write",))
    view = show(senv, sid)
    assert (view["state"], view["denials"], view["last_result"]["denials"]) == ("waiting", ["Write"], ["Write"])
    send(senv, sid, "go on")
    tick(senv)
    assert children(senv)[0].turns() == ["first", "go on"] and show(senv, sid)["state"] == "running"


def test_reports_are_idempotent_and_a_message_only_moves_forward(senv):
    sid = start(senv)["session_id"]
    send(senv, sid, "second")
    post = lambda **body: senv.laptop.post(f"/api/nodes/sessions/{sid}", json=body)
    delivered = {"kind": "delivered", "message": 1}
    ended = {"kind": "turn_ended", "message": 1, "ok": True, "text": "the reply", "meta": {"num_turns": 1}}
    for _ in range(2):
        assert post(events=[delivered], state="running", pid=4242, resume=False).status_code == 200
    view = show(senv, sid)
    assert (view["in_flight"], view["queued"], view["pid"], view["state"]) == (1, 1, 4242, "running")
    for _ in range(3):
        r = post(events=[{"kind": "turn_started", "message": 1}, ended], state="idle", pid=4242)
        assert r.status_code == 200 and "reply" not in r.json() and "the reply" not in r.text
    view = show(senv, sid)
    assert (view["turns"], view["queued"], view["in_flight"], view["reply"]) == (1, 1, None, "the reply")
    # Late and out of order: a settled message stays as it is.
    post(events=[delivered, {"kind": "turn_ended", "message": 1, "ok": False, "detail": "timed out", "text": "late"}])
    assert msg(senv, sid, 1)["state"] == "done" and show(senv, sid)["reply"] == "the reply"
    assert show(senv, sid)["turns"] == 1
    # A result whose `delivered` report never arrived still settles the message and takes it out of the queue.
    post(events=[{"kind": "turn_ended", "message": 2, "ok": False, "delivered": False, "detail": "could not write"}])
    view = show(senv, sid)
    assert (msg(senv, sid, 2)["state"], view["queued"], view["turns"], view["reply"]) == ("failed", 0, 1, "the reply")
    assert post(events=[{"kind": "turn_ended", "message": 77, "ok": True}]).status_code == 200, "unknown: ignored"

    send(senv, sid, "third")
    for _ in range(2):
        assert post(events=[{"kind": "ended", "detail": "the provider reported a fatal error"}]).json()["state"] == "ended"
    view = show(senv, sid)
    assert (view["state"], view["error"], view["queued"]) == ("ended", "the provider reported a fatal error", 0)
    assert msg(senv, sid, 3)["state"] == "failed" and "session ended" in msg(senv, sid, 3)["detail"]
    before = show(senv, sid)
    post(events=[{"kind": "delivered", "message": 3}, {"kind": "turn_ended", "message": 3, "ok": True}], state="idle")
    assert show(senv, sid) == before, "nothing is reported into an ended session"
    r = senv.human.post(f"/api/hosted-sessions/{sid}/messages", json={"message": "x"})
    assert r.status_code == 409 and "has ended" in r.json()["message"]


def test_a_report_the_hub_cannot_read_is_refused_whole(senv):
    sid = start(senv)["session_id"]
    post = lambda **body: senv.laptop.post(f"/api/nodes/sessions/{sid}", json=body).status_code
    assert post(events=[{"kind": "restarted"}]) == 422
    assert post(events=[{"kind": "delivered", "message": 0}]) == 400
    assert post(events=[{"kind": "turn_ended", "message": 1, "ok": True, "denials": ["Bash(rm -rf /)"]}]) == 400
    assert post(events=[{"kind": "turn_ended", "message": 1, "ok": True, "text": "x" * (hosted.REPLY_LIMIT + 1)}]) == 400
    assert post(events=[{"kind": "turn_ended", "message": 1, "ok": True, "meta": {"k": {"nested": 1}}}]) == 400
    assert post(events=[{"kind": "ended", "detail": "x" * 301}]) == 400
    assert post(state="sleeping") == 422 and post(events=[], exec="rm") == 422
    assert msg(senv, sid, 1)["state"] == "queued" and show(senv, sid)["state"] == "idle"


# ------------------------------------------------------------ the reply


def test_the_reply_is_capped_at_16_kb_with_a_marker():
    p = ClaudeSession()
    p.argv(SHIPPED, session_id=UNKNOWN, resume=False, model=None)
    big = "é" * 20_000      # 40 KB of UTF-8
    [ev], _ = p.feed(json.dumps({"type": "result", "subtype": "success", "is_error": False, "session_id": UNKNOWN,
                                 "result": big}).encode())
    assert ev.text.endswith(providers.REPLY_TRUNCATED) and ev.text.startswith("é" * 8192)
    assert len(ev.text.removesuffix(providers.REPLY_TRUNCATED).encode()) == providers.REPLY_MAX_BYTES == 16 * 1024
    assert providers.cap_reply(ev.text) == ev.text, "capping again changes nothing"
    assert "result" not in ev.meta and big[:100] not in repr(ev.meta)
    [small], _ = p.feed(json.dumps({"type": "result", "subtype": "success", "is_error": False, "session_id": UNKNOWN,
                                    "result": "short"}).encode())
    assert small.text == "short"
    assert providers.cap_reply(None) == "" and providers.cap_reply("\ud83d") == "?", "always valid UTF-8"
    assert providers.cap_reply("x" * 16384) == "x" * 16384


def test_only_the_last_reply_is_kept_and_only_the_human_can_read_it(senv):
    sid = start(senv)["session_id"]
    tick(senv)
    run_turn(senv, sid, "FIRST-REPLY-TEXT")
    send(senv, sid, "second")
    tick(senv)
    run_turn(senv, sid, "é" * 20_000)
    view = show(senv, sid)
    assert view["reply_for"] == 2 and view["reply"].endswith(providers.REPLY_TRUNCATED)
    assert len(view["reply"].encode()) == providers.REPLY_MAX_BYTES + len(providers.REPLY_TRUNCATED.encode())
    rows = [(k, v) for k, v in senv.board.conn.execute("SELECT key, value FROM board_state")]
    assert "FIRST-REPLY-TEXT" not in json.dumps(rows), "the earlier reply is gone from the hub"
    assert [k for k, v in rows if "\\u00e9\\u00e9" in v or "éé" in v] == [hosted.SESSION_PREFIX + sid]
    assert all('"text": ""' in v for k, v in rows if k.startswith(hosted.MSG_PREFIX)), "settled: the message text too"

    send(senv, sid, "third")
    tick(senv)
    run_turn(senv, sid, "LAST-REPLY-TEXT")
    assert show(senv, sid)["reply"] == "LAST-REPLY-TEXT"
    assert senv.human.get(f"/api/hosted-sessions/{sid}/messages/3").json()["session"]["reply"] == "LAST-REPLY-TEXT"
    # Never to an agent, and never back to the node.
    assert senv.agent.get(f"/api/hosted-sessions/{sid}").status_code == 403
    for c, paths in ((senv.agent, ["/api/state", "/api/state?closed=true", "/api/threads", "/api/tasks", "/api/grants",
                                   "/api/whoami", "/api/configuration", f"/api/brief?project={PROJECT}"]),
                     (senv.laptop, ["/api/nodes/sessions", "/api/nodes/assignments", "/api/whoami"])):
        for path in paths:
            r = c.get(path)
            assert r.status_code == 200 and "LAST-REPLY-TEXT" not in r.text, path
    r = senv.laptop.post(f"/api/nodes/sessions/{sid}", json={"events": []})
    assert r.status_code == 200 and set(r.json()) == {"session_id", "state", "stop", "turns", "queued", "in_flight"}
    assert "LAST-REPLY-TEXT" not in senv.human.get("/api/state").text, "nor in the dashboard's state, for now"
    transcript = senv.node.sessions.host.transcript_path(sid)
    assert "LAST-REPLY-TEXT" in transcript.read_text() and (transcript.stat().st_mode & 0o777) == 0o600


# ------------------------------------------------------------ policy


@pytest.mark.parametrize("extra", [["--dangerously-skip-permissions"], ["--permission-mode", "bypassPermissions"],
                                   ["--permission-mode=bypassPermissions"],
                                   ["--allow-dangerously-skip-permissions"]])
def test_a_runner_that_bypasses_permissions_cannot_be_hosted(senv, extra):
    runner = ["claude", "-p", "{prompt}"] + extra
    host = SessionHost(node_config(senv, runners={"claude-code": runner}), senv.tmp / "host-logs")
    with pytest.raises(ValueError, match="cannot be hosted as a session"):
        host.open(UNKNOWN, agent="claude", runtime="claude-code", project=PROJECT)
    assert host.sessions == {}

    # Through the node: the session ends with that reason, and nothing is started.
    node = make_node(senv, config=node_config(senv, runners={"claude-code": runner}), log_dir=senv.tmp / "bypass-logs")
    sid = start(senv)["session_id"]
    tick(senv, node)
    view = show(senv, sid)
    assert view["state"] == "ended" and "cannot be hosted as a session" in view["error"]
    assert msg(senv, sid, 1)["state"] == "failed" and children(senv, node) == []


def test_other_permission_modes_are_hosted_as_configured(senv):
    for mode in ("acceptEdits", "dontAsk", "plan", "default"):
        host = SessionHost(node_config(senv, runners={"claude-code": ["claude", "-p", "{prompt}", "--permission-mode",
                                                                      mode]}), senv.tmp / f"logs-{mode}")
        s = host.open(UNKNOWN, agent="claude", runtime="claude-code", project=PROJECT)
        assert value_after(host._argv(s), "--permission-mode") == mode


def test_a_scoped_session_carries_the_denied_tool_instruction_as_an_appended_system_prompt(senv):
    def argv(runner, **over):
        host = SessionHost(node_config(senv, runners={"claude-code": runner}, **over), senv.tmp / "scoped-logs")
        return host._argv(host.open(UNKNOWN, agent="claude", runtime="claude-code", project=PROJECT))

    plain = argv(SHIPPED)
    assert "--append-system-prompt" not in plain and "--allowedTools=mcp__agent-comms" in plain, "scoping is opt-in"
    scoped = argv(SHIPPED, claude_tool_projects=[PROJECT])
    assert value_after(scoped, "--append-system-prompt") == rp.DENIED_TOOL_INSTRUCTION
    assert "--allowedTools=" + ",".join(rp.ALLOWED) in scoped and scoped.count("--append-system-prompt") == 1
    own = argv(SHIPPED + ["--append-system-prompt", "Be brief."], claude_tool_projects=[PROJECT])
    assert own.count("--append-system-prompt") == 1
    assert value_after(own, "--append-system-prompt") == "Be brief.\n\n" + rp.DENIED_TOOL_INSTRUCTION
    assert rp.with_denied_tool_instruction(["claude", "--append-system-prompt=Be brief.", "{prompt}"]) == [
        "claude", "--append-system-prompt=Be brief.\n\n" + rp.DENIED_TOOL_INSTRUCTION, "{prompt}"]


def test_a_process_idle_for_idle_hours_is_stopped_and_the_next_message_resumes_the_session(senv):
    sid = start(senv)["session_id"]
    tick(senv)
    run_turn(senv, sid)
    [child] = children(senv)
    senv.clock.advance(8 * 3600 - 1)
    tick(senv)
    assert child.terminated == 0
    senv.clock.advance(1)
    tick(senv, times=2)
    assert child.terminated == 1 and child.killed == 0
    view = show(senv, sid)
    assert (view["state"], view["pid"], view["turns"]) == ("idle", None, 1), "the session stays; only its process went"
    assert senv.node.sessions.host.sessions[sid].dormant
    tick(senv, times=3)
    assert len(children(senv)) == 1 and not senv.node.sessions.active()

    send(senv, sid, "back")
    tick(senv)
    assert children(senv)[1].turns() == ["back"]
    assert value_after(senv.node.session_spawner.calls[1]["argv"], "--resume") == sid


def test_the_idle_limit_never_stops_a_turn_in_flight(senv):
    node = make_node(senv, config=node_config(senv, sessions={"enabled": True, "idle_hours": 0.1}),
                     log_dir=senv.tmp / "short-logs")
    sid = start(senv)["session_id"]
    tick(senv, node)
    [child] = children(senv, node)
    child.emit(init(sid))
    senv.clock.advance(20 * 60)     # well past idle_hours, short of the turn timeout
    tick(senv, node, times=2)
    assert child.terminated == 0 and msg(senv, sid, 1)["state"] == "delivered"
    run_turn(senv, sid, node=node)
    senv.clock.advance(6 * 60 - 1)
    tick(senv, node)
    assert child.terminated == 0, "idle time counts from the end of the last turn"
    senv.clock.advance(1)
    tick(senv, node)
    assert child.terminated == 1


def test_at_most_max_live_processes_the_longest_idle_one_makes_room(senv):
    node = make_node(senv, config=node_config(senv, sessions={"enabled": True, "max_live": 2}),
                     log_dir=senv.tmp / "two-logs")
    a = start(senv, "a", project=PROJECT)["session_id"]
    tick(senv, node)
    run_turn(senv, a, node=node)
    senv.clock.advance(20)
    b = start(senv, "b", project=P2)["session_id"]
    tick(senv, node)
    run_turn(senv, b, node=node)
    senv.clock.advance(20)
    first, second = children(senv, node)
    c = start(senv, "c", project=P3)["session_id"]
    tick(senv, node)
    assert len(children(senv, node)) == 2 and msg(senv, c, 1)["state"] == "queued", "no third process yet"
    assert (first.terminated, second.terminated) == (1, 0), "the longest-idle process is stopped, the other is not"
    tick(senv, node)
    assert children(senv, node)[2].turns() == ["c"] and msg(senv, c, 1)["state"] == "delivered"
    assert (show(senv, a)["state"], show(senv, a)["pid"]) == ("idle", None), "session a stays, without a process"

    # No idle process: the message stays queued, and nothing running is stopped for it.
    send(senv, b, "b again")
    tick(senv, node)
    assert second.turns() == ["b", "b again"]
    send(senv, a, "a again")
    tick(senv, node, times=3)
    third = children(senv, node)[2]
    assert msg(senv, a, 2)["state"] == "queued" and len(children(senv, node)) == 3
    assert (second.terminated, third.terminated) == (0, 0)
    run_turn(senv, c, node=node)        # c's turn ends: now c is idle and makes room
    tick(senv, node, times=2)
    assert third.terminated == 1 and second.terminated == 0
    assert children(senv, node)[3].turns() == ["a again"] and msg(senv, a, 2)["state"] == "delivered"
    assert value_after(node.session_spawner.calls[3]["argv"], "--resume") == a


def test_the_sessions_table_is_parsed_strictly_and_off_by_default():
    c = DispatchConfig.from_dict({})
    assert (c.sessions.enabled, c.sessions.idle_hours, c.sessions.max_live) == (False, 8.0, 4)
    c = DispatchConfig.from_dict({"sessions": {"enabled": True, "idle_hours": 0.5, "max_live": 2}})
    assert (c.sessions.enabled, c.sessions.idle_hours, c.sessions.max_live) == (True, 0.5, 2)
    for bad, why in (({"enabled": "yes"}, "enabled must be true or false"), ({"idle_hours": 0}, "idle_hours"),
                     ({"idle_hours": True}, "idle_hours"), ({"max_live": 0}, "max_live"),
                     ({"max_live": 2.5}, "max_live"), ({"max_lives": 3}, r"unknown setting \[dispatch.sessions\]"),
                     ("on", "must be a table")):
        with pytest.raises(ValueError, match=why):
            DispatchConfig.from_dict({"sessions": bad})
    with pytest.raises(ValueError, match=r"unknown setting \[dispatch\] session"):
        DispatchConfig.from_dict({"session": {"enabled": True}})
    shipped = DispatchConfig.load(ROOT / "board.toml", local=False)
    assert shipped.sessions.enabled is False, "the shipped settings host nothing"


def test_hello_says_the_node_hosts_sessions_only_when_enabled(senv):
    [info] = nodes.nodes(senv.board)
    assert info["sessions"] == {"max_live": 4}
    for bad in ({"max_live": 0}, {"max_live": 4, "extra": 1}, {}):
        r = senv.laptop.post("/api/nodes/hello", json={"projects": [PROJECT], "runners": ["claude-code"],
                                                       "sessions": bad})
        assert r.status_code == 400, bad
    sent = []
    off = make_node(senv, config=node_config(senv, sessions={"enabled": False}), log_dir=senv.tmp / "off-logs")
    off._call = lambda method, path, **kw: sent.append(kw["json"]) or {}
    off.hello()
    assert "sessions" not in sent[0], "an older hub refuses any hello that carries a field it does not know"


# ------------------------------------------------------------ closing


def test_close_stops_the_process_fails_the_turn_in_flight_and_drops_the_queue(senv):
    sid = start(senv)["session_id"]
    send(senv, sid, "queued")
    tick(senv)
    [child] = children(senv)
    r = senv.human.post(f"/api/hosted-sessions/{sid}/close")
    assert r.status_code == 200 and (r.json()["state"], r.json()["stop"]) != ("ended", None)
    assert r.json()["stop"] == "close", "the node is connected: it stops the process and confirms"
    refused = senv.human.post(f"/api/hosted-sessions/{sid}/messages", json={"message": "x"})
    assert refused.status_code == 409 and "being closed" in refused.json()["message"]
    tick(senv)
    assert child.terminated == 1
    view = show(senv, sid)
    assert (view["state"], view["error"], view["pid"], view["queued"]) == ("ended", "closed by the human", None, 0)
    assert [(m["state"], "session ended" in m["detail"]) for m in view["messages"]] == [("failed", True)] * 2
    assert senv.human.post(f"/api/hosted-sessions/{sid}/close").json()["state"] == "ended", "closing twice is fine"
    tick(senv, times=2)
    assert senv.node.sessions.host.sessions == {} and not senv.node.sessions.active()
    assert json.loads((senv.tmp / "laptop-logs" / HANDED_FILE).read_text()) == {}


def test_a_session_whose_node_is_away_ends_at_once_and_the_node_closes_it_when_it_is_back(senv):
    sid = start(senv)["session_id"]
    tick(senv)
    run_turn(senv, sid)
    [child] = children(senv)
    senv.clock.advance(nodes.NODE_STALE_SECONDS + 1)
    r = senv.human.post(f"/api/hosted-sessions/{sid}/close")
    assert r.json()["state"] == "ended" and "not connected" in r.json()["error"]
    assert child.terminated == 0
    tick(senv)
    assert child.terminated == 1, "the hub no longer lists it, so it must not go on here"
    tick(senv)
    assert senv.node.sessions.host.sessions == {} and senv.node.sessions.pending == []
    assert show(senv, sid)["error"] == r.json()["error"]


def test_a_turn_whose_node_went_silent_fails_as_node_lost(senv):
    sid = start(senv)["session_id"]
    tick(senv)
    senv.clock.advance(nodes.NODE_LOST_SECONDS - 1)
    assert msg(senv, sid, 1)["state"] == "delivered" and show(senv, sid)["node_live"] is False
    senv.clock.advance(2)
    m = msg(senv, sid, 1)
    assert m["state"] == "failed" and m["detail"].startswith("node_lost")
    view = show(senv, sid)
    assert (view["state"], view["in_flight"], view["pid"]) == ("idle", None, None)
    # The node's late result changes nothing; a new message is taken as usual once it is back.
    children(senv)[0].emit(init(sid), result(sid, text="too late"))
    tick(senv)
    assert msg(senv, sid, 1)["state"] == "failed" and show(senv, sid)["reply"] is None


# ------------------------------------------------------------ the node: restart, no hub, shutdown


def test_a_restarted_node_resumes_its_sessions_and_fails_the_turn_that_was_in_flight(senv):
    sid = start(senv)["session_id"]
    tick(senv)
    [old] = children(senv)
    old.emit(init(sid))
    tick(senv)
    assert show(senv, sid)["state"] == "running"

    # The node process dies with the turn in flight. A new one starts over the same log directory.
    node = make_node(senv, log_dir=senv.tmp / "laptop-logs")
    node.procs.external[old.pid] = f"start-{old.pid}"       # the old session process is still alive
    node.sessions.recover()
    assert (old.pid, signal.SIGTERM) in senv.signals, "recover() first: what the last node left is stopped"
    tick(senv, node)
    m = msg(senv, sid, 1)
    assert m["state"] == "failed" and m["detail"] == LOST
    view = show(senv, sid)
    assert (view["state"], view["in_flight"], view["pid"]) == ("idle", None, None)
    tick(senv, node, times=3)
    assert children(senv, node) == [], "the lost turn is never replayed"

    send(senv, sid, "after the restart")
    tick(senv, node)
    [fresh] = children(senv, node)
    assert fresh.turns() == ["after the restart"]
    argv = node.session_spawner.calls[0]["argv"]
    assert value_after(argv, "--resume") == sid and "--session-id" not in argv
    run_turn(senv, sid, "resumed", node=node)
    assert (show(senv, sid)["reply"], show(senv, sid)["turns"]) == ("resumed", 2)


def test_a_message_handed_over_before_a_crash_is_never_written_twice(senv):
    """The hub still lists it as queued (the report never arrived), but the node recorded that it handed it over."""
    sid = start(senv)["session_id"]
    runner, hub = senv.node.sessions, senv.node.sessions.client

    class NoReports:        # the node can fetch, but every report is lost on the way
        def request(self, method, path, **kw):
            if method == "POST":
                raise httpx.ReadTimeout("timed out")
            return hub.request(method, path, **kw)

    runner.client = NoReports()
    runner.pass_once()
    assert children(senv)[0].turns() == ["first"] and msg(senv, sid, 1)["state"] == "queued"
    assert json.loads((senv.tmp / "laptop-logs" / HANDED_FILE).read_text()) == {sid: 1}

    node = make_node(senv, log_dir=senv.tmp / "laptop-logs")
    node.sessions.recover()
    tick(senv, node, times=3)
    m = msg(senv, sid, 1)
    assert m["state"] == "failed" and m["detail"] == LOST
    assert children(senv, node) == [], "not written again"
    assert show(senv, sid)["turns"] == 0
    # The message did reach a process, so Claude may know the session: the next process resumes it (`--session-id`
    # for a session that exists is refused).
    send(senv, sid, "second")
    tick(senv, node)
    assert children(senv, node)[0].turns() == ["second"]
    assert value_after(node.session_spawner.calls[0]["argv"], "--resume") == sid


def test_a_session_that_never_got_a_message_starts_anew_after_a_restart(senv):
    sid = start(senv)["session_id"]
    node = make_node(senv, log_dir=senv.tmp / "laptop-logs")       # the first node process never took a pass
    node.sessions.recover()
    tick(senv, node)
    argv = node.session_spawner.calls[0]["argv"]
    assert value_after(argv, "--session-id") == sid and "--resume" not in argv
    assert msg(senv, sid, 1)["state"] == "delivered"


def test_shutdown_gives_back_a_message_that_never_reached_a_process(senv):
    """stop_all reports the turn in flight and says nothing of a message still in the host's inbox. The node must
    not lose it or call it delivered: it stays queued at the hub and the next node process delivers it, once."""
    sid = start(senv)["session_id"]
    runner = senv.node.sessions
    runner._reconcile(runner._sync(0))
    runner._hand_over(senv.clock())         # handed to the host; the node is stopped before the host writes it
    assert [m.id for m in runner.host.sessions[sid].inbox] == ["1"] and children(senv) == []
    assert json.loads((senv.tmp / "laptop-logs" / HANDED_FILE).read_text()) == {sid: 1}
    runner.shutdown()
    assert json.loads((senv.tmp / "laptop-logs" / HANDED_FILE).read_text()) == {sid: 0}
    m = msg(senv, sid, 1)
    assert (m["state"], m["delivered_at"]) == ("queued", None), "neither lost nor marked delivered"

    node = make_node(senv, log_dir=senv.tmp / "laptop-logs")
    node.sessions.recover()
    tick(senv, node)
    assert children(senv, node)[0].turns() == ["first"]
    assert "--session-id" in node.session_spawner.calls[0]["argv"], "no process ever had it: the session starts anew"
    run_turn(senv, sid, "at last", node=node)
    assert (msg(senv, sid, 1)["state"], show(senv, sid)["reply"], show(senv, sid)["turns"]) == ("done", "at last", 1)


def test_nothing_starts_for_a_session_while_its_stopped_process_group_lingers(senv):
    """PR 1: after a stop the host asked for, the leader's exit is not enough. The node's idle limit and its room for
    another session go through that same stop, so a message waits until the whole group is gone."""
    sid = start(senv)["session_id"]
    tick(senv)
    run_turn(senv, sid)
    [child] = children(senv)
    senv.groups.add(child.pid)              # a tool process in its group ignores SIGTERM
    senv.clock.advance(8 * 3600)
    tick(senv)
    assert child.terminated == 1 and child.code is not None, "parked: the leader went"
    send(senv, sid, "back")
    tick(senv, times=3)
    assert len(children(senv)) == 1 and msg(senv, sid, 2)["state"] == "queued", "the group is still there"
    assert senv.node.sessions.active(), "the node keeps ticking until it is gone"
    senv.clock.advance(10)                  # the kill grace
    tick(senv)
    assert child.killed == 1 and len(children(senv)) == 1
    senv.groups.discard(child.pid)
    tick(senv, times=2)
    assert children(senv)[1].turns() == ["back"] and msg(senv, sid, 2)["state"] == "delivered"


def test_without_the_hub_turns_in_flight_are_stopped_and_idle_processes_kept(senv):
    busy = start(senv, "busy")["session_id"]
    idle = start(senv, "idle", project=P2)["session_id"]
    tick(senv)
    run_turn(senv, idle)
    by_session = {value_after_any(senv, c): c for c in children(senv)}
    by_session[busy].emit(init(busy))
    tick(senv)

    class Down:
        def request(self, *a, **kw):
            raise httpx.ConnectError("connection refused")

    runner, hub = senv.node.sessions, senv.node.sessions.client
    runner.client = Down()
    send(senv, idle, "sent while the node cannot reach the hub")
    senv.clock.advance(SELF_STOP_SECONDS - 1)
    assert runner.pass_once() is False
    assert by_session[busy].terminated == 0, "a short outage stops nothing"
    senv.clock.advance(1)
    runner.pass_once()
    assert (by_session[busy].terminated, by_session[idle].terminated) == (1, 0)
    assert msg(senv, busy, 1)["state"] == "delivered", "the hub has not heard yet"
    senv.clock.advance(5)
    runner.pass_once()
    assert len(children(senv)) == 2 and by_session[idle].turns() == ["idle"], "nothing new starts without the hub"

    runner.client = hub
    senv.clock.advance(5)
    tick(senv)
    m = msg(senv, busy, 1)
    assert m["state"] == "failed" and "no hub contact for 90 seconds" in m["detail"]
    assert by_session[idle].turns() == ["idle", "sent while the node cannot reach the hub"]
    assert show(senv, busy)["state"] == "idle" and show(senv, idle)["state"] == "running"


def test_shutdown_stops_every_process_and_reports_the_turn_in_flight_failed(senv):
    sid = start(senv)["session_id"]
    other = start(senv, "x", project=P2)["session_id"]
    tick(senv)
    run_turn(senv, other)
    senv.node.sessions.shutdown()
    assert all(c.code is not None for c in children(senv))
    m = msg(senv, sid, 1)
    assert m["state"] == "failed" and "shut down" in m["detail"]
    for s in (sid, other):
        view = show(senv, s)
        assert (view["state"], view["pid"]) == ("idle", None), "the sessions go on at the next start"
    assert senv.node.sessions.pass_once() is not None and len(children(senv)) == 2, "a stopped runner starts nothing"


def test_the_nodes_own_loop_never_touches_sessions(senv):
    """Node.tick() is the run loop (poll_seconds); sessions have their own."""
    sid = start(senv)["session_id"]
    senv.node.tick()
    assert children(senv) == [] and msg(senv, sid, 1)["state"] == "queued"


def test_the_nodes_fetch_waits_at_the_hub_until_something_changes(senv):
    sid = start(senv)["session_id"]
    version = senv.laptop.get("/api/nodes/sessions").json()["version"]
    t0 = time.monotonic()
    r = senv.laptop.get("/api/nodes/sessions", params={"wait": 0.6, "seen": version})
    assert 0.5 <= time.monotonic() - t0 < 5 and r.json()["version"] == version, "nothing changed: it waited"
    assert senv.laptop.get("/api/nodes/sessions", params={"wait": 20, "seen": "0" * 16}).status_code == 200, \
        "a version the node has not seen answers at once"

    threading.Timer(0.3, lambda: hosted.send(senv.board, senv.p["human"], sid, "second")).start()
    t0 = time.monotonic()
    r = senv.laptop.get("/api/nodes/sessions", params={"wait": 20, "seen": version})
    waited = time.monotonic() - t0
    assert 0.2 <= waited < 5, f"it answered {waited:.2f}s after the message, not after the full wait"
    assert [m["n"] for m in r.json()["sessions"][0]["messages"]] == [1, 2] and r.json()["version"] != version
    assert senv.laptop.get("/api/nodes/sessions", params={"wait": -1}).status_code == 400
    assert senv.laptop.get("/api/nodes/sessions", params={"wait": 1, "seen": "zz"}).status_code == 400

    # The human's --wait: held until that message's turn is over.
    def finish():
        senv.laptop.post(f"/api/nodes/sessions/{sid}", json={"events": [
            {"kind": "turn_ended", "message": 1, "ok": True, "text": "done"}]})
    threading.Timer(0.3, finish).start()
    t0 = time.monotonic()
    r = senv.human.get(f"/api/hosted-sessions/{sid}/messages/1", params={"wait": 20})
    assert time.monotonic() - t0 < 5 and r.json()["message"]["state"] == "done"
    assert r.json()["session"]["reply"] == "done"


def test_a_wait_at_the_hub_ends_at_once_when_the_server_is_stopping(senv):
    """A node sits in such a wait whenever it has nothing to do; it must not hold up a restart of the hub."""
    sid = start(senv)["session_id"]
    version = senv.laptop.get("/api/nodes/sessions").json()["version"]
    senv.app.state.stopping = lambda: True      # what serve() wires to the server's own exit flag
    t0 = time.monotonic()
    assert senv.laptop.get("/api/nodes/sessions", params={"wait": 20, "seen": version}).json()["version"] == version
    r = senv.human.get(f"/api/hosted-sessions/{sid}/messages/1", params={"wait": 20})
    assert r.json()["message"]["state"] == "queued" and time.monotonic() - t0 < 5


# ------------------------------------------------------------ the CLI


@pytest.fixture
def clienv(senv, monkeypatch, tmp_path):
    """`board session ...` against the test hub: the CLI's HTTP calls are answered by the app, and each wait for a
    turn lets the node take a pass and the stand-in agent answer (in capitals; a text starting with `fail` fails)."""
    home = tmp_path / "cli-home"
    home.mkdir()
    monkeypatch.setenv("AGENT_COMMS_HOME", str(home))
    monkeypatch.setenv("BOARD_TOKEN", senv.tokens["human"])
    senv.requests = []
    answered: dict[int, int] = {}

    def pump():
        tick(senv)
        for i, child in enumerate(children(senv)):
            turns = child.turns()
            if len(turns) > answered.get(i, 0) and child.code is None:
                answered[i] = len(turns)
                sid, text = value_after_any(senv, child), turns[-1]
                if text.startswith("lost"):
                    child.code = 137    # the process dies with the turn in flight
                    continue
                failed = text.startswith("fail")
                child.emit(init(sid), result(sid, text=text.upper(), is_error=failed,
                                             subtype="error_during_execution" if failed else "success"))
        tick(senv)

    class Reply:
        def __init__(self, r):
            self.r = r

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return self.r.content

    class Opener:
        def open(self, req, timeout=None):
            senv.requests.append((req.get_method(), req.full_url))
            assert req.full_url.startswith("http://127.0.0.1:8787/"), "always the local board port"
            path = req.full_url.removeprefix("http://127.0.0.1:8787")
            if "/messages/" in path:
                assert path.endswith("?wait=25"), "--wait waits at the hub, it does not spin"
                path = path.replace("?wait=25", "?wait=0")      # here the node only moves when pumped
                pump()
            r = senv.human.request(req.get_method(), path, content=req.data, headers=dict(req.header_items()))
            if r.status_code >= 400:
                raise urllib.error.HTTPError(req.full_url, r.status_code, "error", {}, io.BytesIO(r.content))
            return Reply(r)

    monkeypatch.setattr("urllib.request.build_opener", lambda *handlers: Opener())
    return senv


def test_board_session_start_and_send_wait_for_the_turn_and_print_the_reply(clienv, capsys, monkeypatch):
    cli.main(["session", "start", PROJECT, "hello there", "--wait", "--model", "haiku"])
    out, err = capsys.readouterr()
    assert out == "HELLO THERE\n", "stdout is the reply and nothing else"
    [s] = clienv.human.get("/api/hosted-sessions").json()["sessions"]
    sid = s["session_id"]
    assert f"session {sid} [idle] claude in {PROJECT} on node laptop" in err and "message 1 queued" in err
    assert s["model"] == "haiku"

    cli.main(["session", "send", sid, "again", "--wait", "--json"])
    out, err = capsys.readouterr()
    data = json.loads(out)
    assert err == "" and data["reply"] == "AGAIN" and data["message"]["n"] == 2 and data["message"]["state"] == "done"
    assert data["session"]["session_id"] == sid and data["session"]["turns"] == 2

    cli.main(["--json", "session", "send", sid, "no wait"])
    data = json.loads(capsys.readouterr().out)
    assert data["message"]["state"] == "queued" and data["message"]["n"] == 3 and "reply" not in data

    monkeypatch.setattr(sys, "stdin", io.StringIO("from a pipe\nsecond line"))
    cli.main(["session", "send", sid, "-", "--wait"])
    assert capsys.readouterr().out == "FROM A PIPE\nSECOND LINE\n"
    assert all(url.startswith("http://127.0.0.1:8787/api/hosted-sessions") for _, url in clienv.requests)


def test_board_session_wait_exits_1_when_the_turn_failed(clienv, capsys):
    cli.main(["session", "start", PROJECT, "hello", "--wait"])
    sid = clienv.human.get("/api/hosted-sessions").json()["sessions"][0]["session_id"]
    capsys.readouterr()
    with pytest.raises(SystemExit) as stop:
        cli.main(["session", "send", sid, "fail please", "--wait"])
    out, err = capsys.readouterr()
    assert stop.value.code == 1
    assert "turn failed: the agent reported that the turn failed (error_during_execution)" in err
    assert out == "FAIL PLEASE\n", "what the agent said is still printed"

    with pytest.raises(SystemExit) as stop:
        cli.main(["session", "send", sid, "fail again", "--wait", "--json"])
    data = json.loads(capsys.readouterr().out)
    assert stop.value.code == 1 and data["message"]["state"] == "failed" and data["message"]["ok"] is False

    # A turn lost with its process: failed, with the reason, and no reply to print.
    with pytest.raises(SystemExit) as stop:
        cli.main(["session", "send", sid, "lost turn", "--wait"])
    out, err = capsys.readouterr()
    assert stop.value.code == 1 and out == ""
    assert "turn failed: the process exited (code 137) before the turn ended" in err


def test_board_session_show_list_and_close(clienv, capsys):
    cli.main(["session", "list"])
    assert capsys.readouterr().out == "(no hosted sessions)\n"
    cli.main(["session", "start", PROJECT, "hello", "--wait"])
    sid = clienv.human.get("/api/hosted-sessions").json()["sessions"][0]["session_id"]
    capsys.readouterr()
    cli.main(["session", "show", sid])
    out = capsys.readouterr().out
    assert f"session {sid} [idle] claude in {PROJECT} on node laptop, turns 1, queued 0" in out
    assert "last result: message 1 ok" in out and "message 1 [done]" in out
    assert out.endswith("reply to message 1:\nHELLO\n") and f"sessions/{sid}.jsonl on node laptop" in out
    cli.main(["session", "show", sid, "--json"])
    data = json.loads(capsys.readouterr().out)
    assert data["reply"] == "HELLO" and data["messages"][0]["state"] == "done"
    cli.main(["session", "list", "--json"])
    assert [s["session_id"] for s in json.loads(capsys.readouterr().out)["sessions"]] == [sid]
    cli.main(["session", "list"])
    assert capsys.readouterr().out.startswith(f"{sid} [idle] claude in {PROJECT}")

    cli.main(["session", "close", sid])
    assert capsys.readouterr().out == f"session {sid} is closing (node laptop stops it and confirms)\n"
    tick(clienv)
    cli.main(["session", "close", sid])
    assert capsys.readouterr().out == f"session {sid} ended\n"
    cli.main(["session", "list"])
    assert capsys.readouterr().out == "(no hosted sessions)\n"
    cli.main(["session", "list", "--all"])
    assert f"{sid} [ended]" in capsys.readouterr().out


def test_board_session_reports_the_hubs_refusal_and_a_server_that_is_not_running(clienv, capsys, monkeypatch):
    with pytest.raises(SystemExit) as stop:
        cli.main(["session", "start", "/work/elsewhere", "hello"])
    assert "error (conflict): no node may run claude in /work/elsewhere" in str(stop.value)
    with pytest.raises(SystemExit) as stop:
        cli.main(["session", "show", UNKNOWN])
    assert "error (not_found)" in str(stop.value)

    class Refusing:
        def open(self, req, timeout=None):
            raise urllib.error.URLError(ConnectionRefusedError(61, "Connection refused"))
    monkeypatch.setattr("urllib.request.build_opener", lambda *handlers: Refusing())
    with pytest.raises(SystemExit) as stop:
        cli.main(["session", "list"])
    assert "the board server is not running at http://127.0.0.1:8787" in str(stop.value)
    monkeypatch.delenv("BOARD_TOKEN")
    monkeypatch.setenv("AGENT_COMMS_TOKEN_FILE", str(clienv.tmp / "no-such-token"))
    with pytest.raises(SystemExit) as stop:
        cli.main(["session", "list"])
    assert "no human token" in str(stop.value)


# ------------------------------------------------------------ real pipes (the stand-in script, never a real agent)


def install_stand_in(directory: Path) -> Path:
    exe = directory / "claude"
    directory.mkdir(parents=True, exist_ok=True)
    exe.write_text(f"#!{sys.executable}\n" + (ROOT / "tests" / "stand_in_claude.py").read_text())
    exe.chmod(0o700)
    return exe


def test_real_pipes_a_queued_message_runs_right_after_the_turn_before_it(senv, tmp_path):
    exe = install_stand_in(tmp_path / "bin")
    config = node_config(senv, runners={"claude-code": [str(exe)] + SHIPPED[1:]}, kill_grace_seconds=5)
    runner = SessionRunner(senv.laptop, config, tmp_path / "real-logs", long_poll=0, sync_seconds=0)
    sid = start(senv, "sleep 0.5 one")["session_id"]
    send(senv, sid, "two")
    send(senv, sid, "deny three")
    seen = []
    try:
        deadline = time.monotonic() + 30
        while msg(senv, sid, 3)["state"] not in ("done", "failed") and time.monotonic() < deadline:
            runner.pass_once()
            states = tuple(m["state"] for m in show(senv, sid)["messages"])
            if not seen or seen[-1] != states:
                seen.append(states)
            time.sleep(0.02)
        view = show(senv, sid)
        assert [m["state"] for m in view["messages"]] == ["done"] * 3, seen
        assert ("delivered", "queued", "queued") in seen and ("done", "delivered", "queued") in seen, \
            "one turn in flight; the next message is written when it ends"
        assert (view["reply"], view["reply_for"], view["turns"]) == ("turn 3: DENY THREE", 3, 3)
        assert (view["state"], view["denials"]) == ("waiting", ["Write"])
        pid = view["pid"]
        assert pid and os.getpgid(pid) == pid, "one process took all three turns"
    finally:
        runner.shutdown()
    assert show(senv, sid)["pid"] is None
    with pytest.raises(ProcessLookupError):
        os.killpg(pid, 0)


def test_the_end_to_end_script_passes_with_the_stand_in(tmp_path):
    """scripts/session-host-e2e.py: a real hub and a real `board node` as subprocesses in a temp directory, driven
    through `board session`. Its timing limits are for an idle machine; a test run gets more slack."""
    env = {k: v for k, v in os.environ.items() if not k.startswith(("AGENT_COMMS_", "BOARD_"))}
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "session-host-e2e.py"), "--max-latency", "10"],
                       capture_output=True, text=True, timeout=180, env=env, cwd=str(tmp_path))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "PASS" in r.stdout and "stand-in" in r.stdout
