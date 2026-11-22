"""Hosted sessions (PR 2): the hub registry (hosted.py), the node protocol and its loop (node_sessions.py, node.py),
the human-only routes and `board session`. Children are fakes with a scripted stdout (test_session_host), except the
tests at the end, which run the stand-in script (stand_in_claude.py) through real pipes. Never a real agent CLI."""

import io
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
import urllib.error
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from agent_comms import cli, gateway, hosted, nodes, providers, runner_preflight as rp
from agent_comms.api import create_app
from agent_comms.config import create_agent
from agent_comms.core import Forbidden
from agent_comms.dispatch import DispatchConfig
from agent_comms.node import SELF_STOP_SECONDS, Node, _Local, shut_down
from agent_comms.node_sessions import HANDED_FILE, LOST, SEQ_FILE, SessionRunner
from agent_comms.providers.claude import ClaudeSession
from agent_comms.session_host import PROVIDER_FILE, SessionHost

from conftest import PROJECT, make_env
from test_dispatch import FakeChild, FakeProcs, FakeSpawner
from test_nodes import Clock
import test_providers_codex as cx
from test_session_host import SHIPPED, FakeSessionChild, init, install_codex_stand_in, result, value_after

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
    return session_env(tmp_path)


def session_env(tmp_path, dispatch=HUB_DISPATCH, config=None):
    """A hub, its app, and one node (`laptop`) that hosts sessions. `config(env)`: the node's own settings."""
    env = make_env(tmp_path, dispatch=dispatch)
    env.tmp, env.signals = tmp_path, []
    env.groups = set()      # fake pids whose process group still has a member (none: a group goes with its leader)
    env.work = tmp_path / "checkout"
    env.work.mkdir()
    for name in ("laptop", "desktop"):
        env.tokens[name] = create_agent(env.settings.agents_path, name, "node")
        env.p[name] = env.board.authenticate(env.tokens[name])
    env.app = create_app(env.board)
    env.human, env.agent, env.laptop = client(env, "human"), client(env, "claude"), client(env, "laptop")
    env.node = make_node(env, config=config(env) if config else None)
    env.node.hello()
    return env


class NoReports:
    """The node's client when it can fetch from the hub but every report is lost on the way."""

    def __init__(self, hub):
        self.hub = hub

    def request(self, method, path, **kw):
        if method == "POST":
            raise httpx.ReadTimeout("timed out")
        return self.hub.request(method, path, **kw)


class Down:
    """The node's client when the hub cannot be reached at all."""

    def request(self, *a, **kw):
        raise httpx.ConnectError("connection refused")


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
    # Only the spawn tools' module (hosted_spawn: an agent's own children, behind its grant) is reached from there.
    assert set(re.findall(r"\bhosted\w*", source)) <= {"hosted_spawn"}
    assert "host.session" not in source and "host.msg" not in source


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
    assert post(events=[{"kind": "ended", "detail": "x", "undelivered": [1]}]) == 422, "no field the hub ignores"
    assert msg(senv, sid, 1)["state"] == "queued" and show(senv, sid)["state"] == "idle"


# ------------------------------------------------------------ the reply


def test_the_reply_is_capped_at_16_kb_with_a_marker():
    p = ClaudeSession()
    p.argv(SHIPPED, session_id=UNKNOWN, resume=False, model=None)
    big = "é" * 20_000      # 40 KB of UTF-8
    [ev], _ = p.feed(json.dumps({"type": "result", "subtype": "success", "is_error": False, "session_id": UNKNOWN,
                                 "result": big}).encode())
    assert ev.text.endswith(providers.REPLY_TRUNCATED) and ev.text.startswith("é" * 8000)
    assert providers.REPLY_MAX_BYTES == 16 * 1024
    assert providers.REPLY_MAX_BYTES - 1 <= len(ev.text.encode()) <= providers.REPLY_MAX_BYTES, \
        "the marker is inside the cap, not on top of it"
    assert providers.cap_reply(ev.text) == ev.text, "capping again changes nothing"
    assert len(providers.cap_reply("x" * 100_000).encode()) == providers.REPLY_MAX_BYTES
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
    assert len(view["reply"].encode()) <= providers.REPLY_MAX_BYTES
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


def test_a_scoped_session_carries_its_commands_and_the_denied_tool_instruction_as_an_appended_system_prompt(senv):
    def argv(runner, **over):
        host = SessionHost(node_config(senv, runners={"claude-code": runner}, **over), senv.tmp / "scoped-logs")
        return host._argv(host.open(UNKNOWN, agent="claude", runtime="claude-code", project=PROJECT))

    plain = argv(SHIPPED)
    assert "--append-system-prompt" not in plain and "--allowedTools=mcp__agent-comms" in plain, "scoping is opt-in"
    scoped = argv(SHIPPED, claude_tool_projects=[PROJECT])
    template = rp.scoped_template(SHIPPED)
    text = rp.tools_notice(template) + " " + rp.HOSTED_DENIED_TOOL_INSTRUCTION
    assert value_after(scoped, "--append-system-prompt") == text, "the hosted wording (test_scoped_notice)"
    assert rp.DENIED_TOOL_INSTRUCTION not in text, "a session's task has no post id to block"
    assert "--allowedTools=" + ",".join(rp.ALLOWED) in scoped and scoped.count("--append-system-prompt") == 1
    own = argv(SHIPPED + ["--append-system-prompt", "Be brief."], claude_tool_projects=[PROJECT])
    assert own.count("--append-system-prompt") == 1
    assert value_after(own, "--append-system-prompt") == "Be brief.\n\n" + text
    assert rp.with_denied_tool_instruction(["claude", "--append-system-prompt=Be brief.", "{prompt}"]) == [
        "claude", "--append-system-prompt=Be brief.\n\n" + rp.tools_notice(["claude", "{prompt}"]) + " "
        + rp.HOSTED_DENIED_TOOL_INSTRUCTION, "{prompt}"]


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
    runner = senv.node.sessions
    runner.client = NoReports(runner.client)
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
    # The provider never confirmed the session, and a hand-over (even a write) is no proof that it exists: the next
    # process creates it. `--resume` here would fail every later message if Claude never got as far as creating it.
    send(senv, sid, "second")
    tick(senv, node)
    assert children(senv, node)[0].turns() == ["second"]
    argv = node.session_spawner.calls[0]["argv"]
    assert value_after(argv, "--session-id") == sid and "--resume" not in argv


def test_only_the_providers_confirmation_makes_a_session_resumable(senv):
    sid = start(senv)["session_id"]
    listed = lambda: senv.laptop.get("/api/nodes/sessions").json()["sessions"][0]
    post = lambda **body: senv.laptop.post(f"/api/nodes/sessions/{sid}", json=body)
    post(events=[{"kind": "delivered", "message": 1}], state="starting", pid=4242, resume=False)
    assert listed()["resume"] is False, "bytes in a pipe do not prove the conversation exists"
    post(events=[{"kind": "turn_ended", "message": 1, "ok": False, "detail": "the process exited (code 1)"}])
    assert listed()["resume"] is False
    send(senv, sid, "second")
    post(events=[{"kind": "delivered", "message": 2}, {"kind": "turn_started", "message": 2}])
    assert listed()["resume"] is True, "the provider began a turn in it"
    post(events=[], state="idle", pid=None, resume=False)
    assert listed()["resume"] is False, "the host's correction after a refused start reaches the hub too"


def test_a_refused_start_flips_the_mode_so_the_session_heals_after_one_failed_message(senv):
    """Claude Code refuses `--session-id` for a session that exists and `--resume` for one that does not, before any
    turn (stderr and exit 1; DESIGN_NOTES). Whichever way the node guessed wrong, the next process starts the other
    way: one failed message, never every later one."""
    sid = start(senv)["session_id"]
    calls = senv.node.session_spawner.calls
    listed = lambda: senv.laptop.get("/api/nodes/sessions").json()["sessions"][0]
    tick(senv)
    assert value_after(calls[0]["argv"], "--session-id") == sid
    with open(calls[0]["log_path"], "a") as f:     # Claude had created the session before an earlier crash
        f.write(f"Error: Session ID {sid} is already in use.\n")
    children(senv)[0].code = 1
    tick(senv)
    m = msg(senv, sid, 1)
    assert m["state"] == "failed" and "exited (code 1)" in m["detail"], "that message is lost, and is not retried"
    assert listed()["resume"] is True
    send(senv, sid, "second")
    tick(senv)
    assert value_after(calls[1]["argv"], "--resume") == sid and "--session-id" not in calls[1]["argv"]

    # The other way round: told to resume a conversation Claude does not have.
    with open(calls[1]["log_path"], "a") as f:
        f.write(f"No conversation found with session ID: {sid}\n")
    children(senv)[1].emit(result(sid, subtype="error_during_execution", is_error=True, text=""))
    children(senv)[1].code = 1
    tick(senv)
    assert msg(senv, sid, 2)["state"] == "failed" and listed()["resume"] is False
    send(senv, sid, "third")
    tick(senv)
    assert value_after(calls[2]["argv"], "--session-id") == sid and "--resume" not in calls[2]["argv"]

    # A process that did confirm the session and later exits is not second-guessed by what an earlier one wrote.
    run_turn(senv, sid, "third reply")
    children(senv)[2].code = 0
    tick(senv)
    assert listed()["resume"] is True and show(senv, sid)["reply"] == "third reply"
    send(senv, sid, "fourth")
    tick(senv)
    assert value_after(calls[3]["argv"], "--resume") == sid


def test_after_a_failed_hub_call_nothing_new_is_handed_over_until_a_fetch_succeeds(senv):
    """The queue and the stop flags the node holds may be out of date: the human may just have closed the session."""
    sid = start(senv)["session_id"]
    send(senv, sid, "second")
    tick(senv)
    [child] = children(senv)
    child.emit(init(sid))
    tick(senv)
    runner, hub = senv.node.sessions, senv.node.sessions.client
    assert runner.queue[sid][0]["text"] == "second", "the node already holds the next message"

    runner.client = Down()
    assert senv.human.post(f"/api/hosted-sessions/{sid}/close").json()["stop"] == "close"
    child.emit(result(sid, text="first reply"))
    assert runner.pass_once() is False
    assert child.turns() == ["first"], "the running turn went on to its end; the cached message was not started"
    senv.clock.advance(1)
    runner.pass_once()
    assert child.turns() == ["first"]

    runner.client = hub
    senv.clock.advance(5)
    tick(senv, times=2)
    assert child.turns() == ["first"] and child.terminated == 1
    view = show(senv, sid)
    assert (view["state"], view["reply"]) == ("ended", "first reply"), "the turn that did run is not lost"
    assert [m["state"] for m in view["messages"]] == ["done", "failed"]


def test_a_paused_board_also_holds_new_turns_back(senv):
    sid = start(senv)["session_id"]
    send(senv, sid, "second")
    tick(senv)
    children(senv)[0].emit(init(sid))
    tick(senv)
    assert senv.node.sessions.queue[sid][0]["text"] == "second", "the node already holds the next message"
    senv.board.set_paused(senv.p["human"], True)        # from here the node's calls are refused (423)
    children(senv)[0].emit(result(sid))
    tick(senv, times=2)
    assert children(senv)[0].turns() == ["first"], "the running turn ended; the cached message was not started"
    senv.board.set_paused(senv.p["human"], False)
    senv.clock.advance(5)
    tick(senv)
    assert children(senv)[0].turns() == ["first", "second"]


def test_shutdown_reports_what_the_loop_had_not_and_the_loop_posts_nothing_after(senv):
    sid = start(senv)["session_id"]
    tick(senv)
    runner, hub = senv.node.sessions, senv.node.sessions.client
    runner.client = NoReports(hub)
    children(senv)[0].emit(init(sid), result(sid, text="finished just before the stop"))
    runner.pass_once()
    assert any(e["kind"] == "turn_ended" for _, e in runner.pending) and msg(senv, sid, 1)["state"] == "delivered"
    runner.client = hub

    # The loop is still alive when shutdown's join times out (it sits in a hub call). Shutdown reports all the same,
    # everything in order: the completion the loop had not delivered, then how the session stands now.
    runner.thread = SimpleNamespace(join=lambda timeout=None: None, is_alive=lambda: True)
    runner.shutdown()
    view = show(senv, sid)
    assert (msg(senv, sid, 1)["state"], view["reply"]) == ("done", "finished just before the stop")
    assert (view["state"], view["pid"], view["turns"]) == ("idle", None, 1)

    # When that loop comes back from its call it posts nothing: its older status would bring the dead pid back.
    runner.status[sid] = ("running", 4242, True)
    runner.pending.append((sid, {"kind": "delivered", "message": 1}))
    assert runner._flush() is False and runner.pass_once() is False
    view = show(senv, sid)
    assert (view["state"], view["pid"]) == ("idle", None)


def test_node_shutdown_signals_every_child_before_reporting_and_always_cleans_up_the_runs(senv):
    sid = start(senv)["session_id"]
    tick(senv)
    node = senv.node
    [session_child] = children(senv)
    run_child = FakeChild(5000)
    node.running["run-1"] = _Local("run-1", run_child, senv.clock(), 1800.0, 10.0)
    posts, hub = [], node.client

    class Recording:
        def request(self, method, path, **kw):
            if method == "POST":
                posts.append((path, run_child.terminated, session_child.terminated))
            return hub.request(method, path, **kw)

    node.client = node.sessions.client = Recording()
    shut_down(node)
    assert posts and all(run and session for _, run, session in posts), "every child had its SIGTERM before any report"
    assert [path for path, _, _ in posts] == ["/api/nodes/runs/run-1", f"/api/nodes/sessions/{sid}"], \
        "the ordinary runs are reported as before; the sessions come after"
    assert node.running == {} and msg(senv, sid, 1)["state"] == "failed"

    # Whatever the sessions do, the runs are stopped and reported.
    for broken in ("signal", "alive", "kill", "shutdown"):
        again = FakeChild(6000)
        node.running["run-2"] = _Local("run-2", again, senv.clock(), 1800.0, 10.0)

        def boom():
            raise RuntimeError("session cleanup failed")
        fake = SimpleNamespace(signal=lambda: None, alive=lambda: False, kill=lambda: None, shutdown=lambda: None)
        setattr(fake, broken, boom)
        node.sessions = fake
        del posts[:]
        if broken == "shutdown":
            with pytest.raises(RuntimeError):
                shut_down(node)
        else:
            shut_down(node)
        assert again.terminated == 1 and node.running == {} and posts[0][0] == "/api/nodes/runs/run-2", broken


def test_a_session_process_that_ignores_sigterm_is_killed_within_its_grace_before_any_report(senv):
    """One grace period for every child, measured from its SIGTERM, and the kills before the first shutdown report:
    a report can stall for as long as a hub call takes, and the stubborn process must not wait that out first."""
    node = senv.node
    node.session_spawner.child = lambda pid: FakeSessionChild(pid, exits_on_terminate=False)
    sid = start(senv)["session_id"]
    tick(senv)
    [stubborn] = children(senv)
    run_child = FakeChild(5000)
    node.running["run-1"] = _Local("run-1", run_child, senv.clock(), 1800.0, 10.0)
    posts, kills, hub, t0 = [], [], node.client, senv.clock()
    kill = stubborn.kill
    stubborn.kill = lambda: (kills.append(senv.clock() - t0), kill())

    class Stalling:
        def request(self, method, path, **kw):
            if method == "POST":
                posts.append((path, stubborn.killed))
                if "/runs/" in path:
                    senv.clock.advance(20)      # the hub does not answer until the call times out
            return hub.request(method, path, **kw)

    node.client = node.sessions.client = Stalling()
    shut_down(node)
    assert run_child.terminated == 1
    assert stubborn.terminated == 1, "one SIGTERM: once it is killed and gone, the reporting phase signals nothing"
    assert kills and kills[0] == pytest.approx(10, abs=0.5), "killed when ITS grace was over, not 20 s of report later"
    assert [path for path, _ in posts] == ["/api/nodes/runs/run-1", f"/api/nodes/sessions/{sid}"]
    assert all(killed for _, killed in posts), "no report before the kill"
    assert node.running == {} and msg(senv, sid, 1)["state"] == "failed" and show(senv, sid)["pid"] is None


def test_a_late_older_report_cannot_undo_the_final_one(senv):
    """A report whose call timed out on the node can still commit at the hub after the node's final report. Its events
    are harmless (a message only moves forward); its status is an old snapshot and is ignored."""
    sid = start(senv)["session_id"]
    send(senv, sid, "second")
    sent, hub = [], senv.node.sessions.client

    class Recording:
        def request(self, method, path, **kw):
            if method == "POST":
                sent.append(kw["json"])
            return hub.request(method, path, **kw)

    senv.node.sessions.client = Recording()
    tick(senv)
    senv.node.sessions.shutdown()       # the final report: the turn failed, no process
    stamps = [body["seq"] for body in sent]
    assert len(stamps) >= 2 and stamps == sorted(set(stamps)), "every report is stamped above the one before"
    final = show(senv, sid)
    assert (final["state"], final["pid"], msg(senv, sid, 1)["state"]) == ("idle", None, "failed")

    late = {"events": [{"kind": "delivered", "message": 1}, {"kind": "delivered", "message": 2}],
            "state": "running", "pid": 4242, "resume": True, "seq": stamps[0] - 1}
    assert senv.laptop.post(f"/api/nodes/sessions/{sid}", json=late).status_code == 200
    view = show(senv, sid)
    assert (view["state"], view["pid"]) == ("idle", None), "the dead pid does not come back"
    assert senv.laptop.get("/api/nodes/sessions").json()["sessions"][0]["resume"] is False
    assert (msg(senv, sid, 1)["state"], msg(senv, sid, 2)["state"]) == ("failed", "delivered"), "events apply as ever"
    same = dict(late, events=[], seq=stamps[-1])
    senv.laptop.post(f"/api/nodes/sessions/{sid}", json=same)
    assert show(senv, sid)["pid"] is None, "nor does a repeat of the last one's stamp"
    # A newer report, and one without a stamp (nothing sends one, the route stays tolerant), apply as before.
    senv.laptop.post(f"/api/nodes/sessions/{sid}", json=dict(late, events=[], seq=stamps[-1] + 1))
    assert (show(senv, sid)["state"], show(senv, sid)["pid"]) == ("running", 4242)
    senv.laptop.post(f"/api/nodes/sessions/{sid}", json={"events": [], "state": "idle", "pid": None})
    assert (show(senv, sid)["state"], show(senv, sid)["pid"]) == ("idle", None)
    assert senv.laptop.post(f"/api/nodes/sessions/{sid}", json={"events": [], "seq": 0}).status_code == 400


def test_after_a_restart_with_the_clock_set_back_the_new_process_is_heard_and_the_old_one_is_not(senv):
    """The stamp that orders reports starts from the wall clock. If that clock ran ahead and is corrected across a
    node restart, the new process would stamp below what the hub holds: the hub's own stored stamp is the floor."""
    sid = start(senv)["session_id"]
    listed = lambda: senv.laptop.get("/api/nodes/sessions").json()["sessions"][0]
    post = lambda **body: senv.laptop.post(f"/api/nodes/sessions/{sid}", json=body)
    senv.node.sessions.wall_ns = lambda: 9_000_000_000_000_000_000      # the old process's clock runs far ahead
    tick(senv)
    run_turn(senv, sid)
    held = listed()["status_seq"]
    assert held >= 9_000_000_000_000_000_000 and show(senv, sid)["state"] == "idle"

    node = make_node(senv, log_dir=senv.tmp / "laptop-logs")       # restarted; the clock has been set back
    node.sessions.wall_ns = lambda: 1_000
    node.sessions.recover()
    send(senv, sid, "after the restart")
    tick(senv, node)
    assert show(senv, sid)["state"] == "starting", "the new process's first status is applied, not ignored"
    assert listed()["status_seq"] > held
    # A report the old process sent before it died arrives now: its status is older than what the hub holds.
    assert post(events=[], state="waiting", pid=4242, resume=True, seq=held).status_code == 200
    view = show(senv, sid)
    assert (view["state"], view["pid"]) == ("starting", children(senv, node)[0].pid)

    # Even one stamped above everything the new process has sent (it was never applied before the restart) only
    # holds until the node's next fetch: the node sees a stamp that is not its own and says again how things stand.
    assert post(events=[], state="waiting", pid=4242, seq=listed()["status_seq"] + 10**6).status_code == 200
    assert show(senv, sid)["pid"] == 4242
    tick(senv, node)
    view = show(senv, sid)
    assert (view["state"], view["pid"]) == ("starting", children(senv, node)[0].pid)


def test_the_next_node_process_stamps_above_everything_the_last_one_issued(senv, monkeypatch):
    """The highest stamp issued is on disk before the report that carries it is sent, and seeds the next process: with
    the clock set far back, the new process's first stamp is still above the old one's last, before any fetch, so
    an old report can neither tie with a new one nor outrank it."""
    sid = start(senv)["session_id"]
    old, hub, seq_file = senv.node.sessions, senv.node.sessions.client, senv.tmp / "laptop-logs" / SEQ_FILE
    on_disk_at_send = []

    class Recording:
        def request(self, method, path, **kw):
            if method == "POST":
                on_disk_at_send.append(json.loads(seq_file.read_text())["seq"] >= kw["json"]["seq"])
            return hub.request(method, path, **kw)

    old.client = Recording()
    old.wall_ns = lambda: 9_000_000_000_000_000_000        # the old process's clock runs far ahead
    tick(senv)
    run_turn(senv, sid)
    n = old.seq
    assert on_disk_at_send and all(on_disk_at_send), "persisted before each report is sent"
    assert json.loads(seq_file.read_text()) == {"seq": n} and (seq_file.stat().st_mode & 0o777) == 0o600
    # The old process issued one more stamp for a report that is still on its way when it dies.
    in_flight = {"events": [], "state": "waiting", "pid": 4242, "resume": True, "seq": old._stamp()}
    n = in_flight["seq"]

    node = make_node(senv, log_dir=senv.tmp / "laptop-logs")       # restarted; the clock has been set far back
    new = node.sessions
    new.wall_ns = lambda: 1_000
    assert new.hub_seq == 0 and new.seq == n, "seeded from the file, before any fetch"
    new.recover()
    send(senv, sid, "after the restart")
    tick(senv, node)
    assert new.seq > n and show(senv, sid)["state"] == "starting", "its first status is above N and is applied"
    assert senv.laptop.post(f"/api/nodes/sessions/{sid}", json=in_flight).status_code == 200
    view = show(senv, sid)
    assert (view["state"], view["pid"]) == ("starting", children(senv, node)[0].pid), "the late old report is ignored"

    # A stamp that cannot be put on disk is still issued: reporting goes on.
    def unwritable(path, data):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(new, "_write_private", unwritable)
    before = new.seq
    run_turn(senv, sid, "still reported", node=node)
    assert new.seq > before and show(senv, sid)["reply"] == "still reported"


def test_settle_decides_again_inside_its_transaction(senv, monkeypatch):
    """A reader finds the node silent for too long; before it writes, the node is back and has reported. The turn the
    node holds must not be failed under it."""
    sid = start(senv)["session_id"]
    tick(senv)
    senv.clock.advance(nodes.NODE_LOST_SECONDS + 5)
    real, raced = hosted.db.write_tx, []

    @contextmanager
    def racing(conn):
        if not raced:
            raced.append(True)      # the node reconnects in the gap between the reader's check and its transaction
            senv.node.hello()
            r = senv.laptop.post(f"/api/nodes/sessions/{sid}", json={"events": [], "state": "running", "pid": 4242})
            assert r.status_code == 200
        with real(conn) as c:
            yield c

    monkeypatch.setattr(hosted.db, "write_tx", racing)
    view = show(senv, sid)
    assert raced, "the reader did decide to settle"
    assert (view["in_flight"], view["state"], view["pid"]) == (1, "running", 4242)
    assert msg(senv, sid, 1)["state"] == "delivered"
    run_turn(senv, sid, "its result counts")
    assert show(senv, sid)["reply"] == "its result counts"


def test_a_message_and_its_session_are_read_from_one_snapshot(senv, monkeypatch):
    """`--wait` reads the message's state and the session's reply together: a turn completing between two separate
    reads would show `done` beside the previous reply."""
    sid = start(senv)["session_id"]
    tick(senv)
    real, raced = hosted._get, []

    def racing(conn, key):
        if key.startswith(hosted.MSG_PREFIX) and not raced:
            raced.append(True)      # the completion commits between the session read and the message read
            t = threading.Thread(target=lambda: hosted.report(senv.board, senv.p["laptop"], sid, [
                {"kind": "turn_ended", "message": 1, "ok": True, "text": "THE REPLY"}]))
            t.start()
            t.join()
        return real(conn, key)

    monkeypatch.setattr(hosted, "_get", racing)
    out = hosted.message(senv.board, senv.p["human"], sid, 1)
    assert raced
    assert (out["message"]["state"], out["session"]["reply_for"], out["session"]["reply"]) == ("delivered", None, None), \
        "both from before the completion: never `done` with the reply still missing"
    out = hosted.message(senv.board, senv.p["human"], sid, 1)
    assert (out["message"]["state"], out["session"]["reply_for"], out["session"]["reply"]) == ("done", 1, "THE REPLY")


def test_a_node_that_no_longer_hosts_sessions_cannot_hold_a_close(senv):
    """Restarted with [dispatch.sessions] off, a node keeps its heartbeat fresh through its run loop, but nothing
    there reads a stop flag any more."""
    sid = start(senv)["session_id"]
    tick(senv)
    run_turn(senv, sid)
    off = make_node(senv, config=node_config(senv, sessions={"enabled": False}), log_dir=senv.tmp / "off-logs")
    off.hello()
    off.tick()
    r = senv.human.post(f"/api/hosted-sessions/{sid}/close")
    assert (r.json()["state"], r.json()["node_live"]) == ("ended", False), "ended on the hub at once"
    refused = senv.human.post("/api/hosted-sessions", json={"project": PROJECT, "message": "x"})
    assert refused.status_code == 409 and "no connected node hosts sessions" in refused.json()["message"]
    # A turn that was in flight there is not left in flight for ever either.
    senv.node.hello()
    other = start(senv, project=P2)["session_id"]
    tick(senv)
    off.hello()
    assert msg(senv, other, 1)["detail"].startswith("node_lost") and show(senv, other)["in_flight"] is None




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


def test_a_node_restarted_without_hosting_still_stops_the_session_processes_the_last_one_left(senv):
    """The node crashed with a hosted turn running and comes back with [dispatch.sessions] off: nobody else would
    ever stop that process, and the node's hello says it hosts nothing."""
    sid = start(senv)["session_id"]
    tick(senv)
    [left] = children(senv)
    logs = senv.tmp / "laptop-logs"
    state = logs / "node-sessions.json"
    assert json.loads(state.read_text())[sid]["pid"] == left.pid
    threads = threading.active_count()

    off = make_node(senv, config=node_config(senv, sessions={"enabled": False}), log_dir=logs)
    off.procs.external[left.pid] = f"start-{left.pid}"      # still alive, and verifiably the recorded process
    senv.groups.add(left.pid)                               # ... with a tool process that ignores SIGTERM
    sent = []
    off._call = lambda method, path, **kw: sent.append(kw.get("json")) or {}
    off.recover()
    assert (left.pid, signal.SIGTERM) in senv.signals and (left.pid, signal.SIGKILL) in senv.signals
    assert json.loads(state.read_text()) == {}, "nothing is left on record"
    off.hello()
    assert off.sessions is None and "sessions" not in sent[0] and threading.active_count() == threads, \
        "cleaning up is not hosting: nothing advertised, nothing started"
    assert children(senv, off) == []

    # Only a process that is verifiably the recorded one is signalled: a reused pid is left alone.
    state.write_text(json.dumps({sid: {"pid": 4321, "proc_start": "start-of-the-old-process", "resume": True}}))
    again = make_node(senv, config=node_config(senv, sessions={"enabled": False}), log_dir=logs)
    again.procs.external[4321] = "start-of-something-else"
    del senv.signals[:]
    again.recover()
    assert senv.signals == [] and json.loads(state.read_text()) == {}


def test_without_a_session_state_file_a_node_that_does_not_host_touches_nothing(senv):
    logs = senv.tmp / "fresh-logs"
    off = make_node(senv, config=node_config(senv, sessions={"enabled": False}), log_dir=logs)
    assert off.recover() == [] and senv.signals == []
    assert sorted(x.name for x in logs.iterdir()) == ["node-runs.json"], \
        "only the node's own run record, as before: no session file, no sessions directory"


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


# ------------------------------------------------------------ Codex sessions (providers/codex.py)

CODEX_RUNNER = ["codex", "exec", "--cd", "{project}", "--sandbox", "workspace-write", "-c", cx.APPROVE, "{prompt}"]
CODEX_GRANT = {"nodes": {"laptop": {"projects": [PROJECT, P2, P3], "agents": ["claude", "codex"]}}}


def codex_config(env, **sessions) -> DispatchConfig:
    return node_config(env, runners={"claude-code": SHIPPED, "codex-cli": CODEX_RUNNER},
                       sessions={"enabled": True, "codex": True} | sessions)


@pytest.fixture
def cenv(tmp_path):
    """senv, with the hub allowing the node the `codex` agent and the node hosting Codex sessions."""
    return session_env(tmp_path, dispatch=CODEX_GRANT, config=codex_config)


def written(child) -> list[dict]:
    return [json.loads(x) for x in child.stdin.split(b"\n")[:-1]]


def codex_handshake(env, child, node=None, thread=cx.THREAD) -> None:
    """Codex answers `initialize` and then `thread/start` (or `thread/resume`); the node takes a pass after each."""
    child.emit(cx.reply(1, cx.INIT_RESULT))
    tick(env, node)
    child.emit(cx.reply(2, {**cx.THREAD_RESULT, "thread": {**cx.THREAD_RESULT["thread"], "id": thread}}))
    tick(env, node)


def codex_turn(env, child, text, rid=3, node=None) -> None:
    child.emit(cx.reply(rid, cx.TURN_REPLY), cx.TURN_STARTED, cx.message("a note", phase="commentary"),
               cx.message(text), cx.completed())
    tick(env, node)


def test_a_hosted_codex_session_takes_turns_through_the_hub_and_its_thread_id_is_on_the_record(cenv):
    sid = start(cenv, "first", agent="codex", model="gpt-6-luna")["session_id"]
    assert sid != cx.THREAD and show(cenv, sid)["provider_id"] is None
    tick(cenv)
    [child], [call] = children(cenv), cenv.node.session_spawner.calls
    assert call["argv"] == ["codex", "app-server", "-c", cx.APPROVE, "--listen", "stdio://"]
    assert [m["method"] for m in written(child)] == ["initialize"]
    assert (show(cenv, sid)["state"], msg(cenv, sid, 1)["state"]) == ("starting", "queued"), "nothing is delivered yet"
    codex_handshake(cenv, child)
    assert written(child)[2]["params"] == {"sandbox": "workspace-write", "approvalPolicy": "never",
                                           "model": "gpt-6-luna"}
    assert call["cwd"] == str(cenv.work), "the process is started in the run directory; Codex is told no other"
    view = show(cenv, sid)
    assert (view["state"], view["provider_id"], msg(cenv, sid, 1)["state"]) == ("running", cx.THREAD, "delivered")
    listing = cenv.laptop.get("/api/nodes/sessions").json()
    assert listing["sessions"][0]["resume"] is True, "a turn was written to that thread: it is one to resume"
    assert cx.THREAD not in json.dumps(listing), "the id is the hub's to show, never to hand back to a node"

    send(cenv, sid, "second")
    codex_turn(cenv, child, "the first answer")
    view = show(cenv, sid)
    assert (view["reply"], view["reply_for"], view["turns"]) == ("the first answer", 1, 1)
    assert msg(cenv, sid, 2)["state"] == "delivered" and written(child)[-1]["method"] == "turn/start"
    assert cenv.laptop.get("/api/nodes/sessions").json()["sessions"][0]["resume"] is True
    assert "codex-cli id " + cx.THREAD in cli._fmt_hosted_detail(view)
    assert cenv.agent.get(f"/api/hosted-sessions/{sid}").status_code == 403, "human only, as for any session"
    assert cx.THREAD not in json.dumps(cenv.agent.get("/api/state").json())


def test_a_restarted_node_resumes_the_codex_thread_from_its_own_record(cenv):
    sid = start(cenv, "first", agent="codex")["session_id"]
    tick(cenv)
    [old] = children(cenv)
    codex_handshake(cenv, old)
    codex_turn(cenv, old, "one")
    cenv.node.sessions.shutdown()
    assert (show(cenv, sid)["state"], show(cenv, sid)["pid"], show(cenv, sid)["provider_id"]) == ("idle", None, cx.THREAD)

    # The hub's copy is for show only: even a wrong one changes nothing about what the node resumes.
    cenv.laptop.post(f"/api/nodes/sessions/{sid}", json={"provider_id": cx.CHILD})
    assert show(cenv, sid)["provider_id"] == cx.CHILD
    node = make_node(cenv, config=codex_config(cenv), log_dir=cenv.tmp / "laptop-logs")
    node.sessions.recover()
    send(cenv, sid, "after the restart")
    tick(cenv, node)
    [fresh] = children(cenv, node)
    fresh.emit(cx.reply(1, cx.INIT_RESULT))
    tick(cenv, node)
    resume = written(fresh)[2]
    assert (resume["method"], resume["params"]["threadId"]) == ("thread/resume", cx.THREAD)
    fresh.emit(cx.reply(2, cx.THREAD_RESULT))
    tick(cenv, node)
    assert written(fresh)[3]["params"]["input"] == [{"type": "text", "text": "after the restart"}]
    codex_turn(cenv, fresh, "still here", node=node)
    view = show(cenv, sid)
    assert (view["reply"], view["turns"], view["provider_id"]) == ("still here", 2, cx.THREAD)


def test_a_codex_thread_that_is_gone_fails_one_message_with_the_reason_and_the_hub_sees_the_new_thread(cenv):
    sid = start(cenv, "first", agent="codex")["session_id"]
    tick(cenv)
    [old] = children(cenv)
    codex_handshake(cenv, old)
    codex_turn(cenv, old, "one")
    cenv.node.sessions.shutdown()
    node = make_node(cenv, config=codex_config(cenv), log_dir=cenv.tmp / "laptop-logs")
    node.sessions.recover()
    send(cenv, sid, "second")
    tick(cenv, node)
    [gone] = children(cenv, node)
    gone.emit(cx.reply(1, cx.INIT_RESULT))
    tick(cenv, node)
    gone.emit(cx.reply(2, error=cx.NO_ROLLOUT))
    tick(cenv, node, times=2)
    m = msg(cenv, sid, 2)
    assert m["state"] == "failed" and "NEW thread" in m["detail"] and cx.THREAD in m["detail"]
    view = show(cenv, sid)
    assert (view["state"], view["provider_id"]) == ("idle", None), "the session stays; its old thread is history"
    assert cenv.laptop.get("/api/nodes/sessions").json()["sessions"][0]["resume"] is False

    send(cenv, sid, "third")
    tick(cenv, node)
    fresh = children(cenv, node)[-1]
    fresh.emit(cx.reply(1, cx.INIT_RESULT))
    tick(cenv, node)
    assert written(fresh)[2]["method"] == "thread/start"
    codex_handshake(cenv, fresh, node=node, thread=cx.CHILD)
    assert show(cenv, sid)["provider_id"] == cx.CHILD and msg(cenv, sid, 3)["state"] == "delivered"


def test_a_refused_resume_recorded_before_a_crash_starts_a_new_thread_at_restart_whatever_the_hub_says(cenv):
    """The node records that Codex refused the resume, fails the message, and dies before the hub hears any of it.
    The hub still says `resume`. The node's own record overrules that: the next message starts the new thread, with
    no second failed message on the way, and the one lost message says what happened."""
    sid = start(cenv, "first", agent="codex")["session_id"]
    tick(cenv)
    [old] = children(cenv)
    codex_handshake(cenv, old)
    codex_turn(cenv, old, "one")
    cenv.node.sessions.shutdown()
    logs = cenv.tmp / "laptop-logs"
    listed = lambda: cenv.laptop.get("/api/nodes/sessions").json()["sessions"][0]

    node = make_node(cenv, config=codex_config(cenv), log_dir=logs)
    node.sessions.recover()
    send(cenv, sid, "second")
    tick(cenv, node)
    [refused] = children(cenv, node)
    refused.emit(cx.reply(1, cx.INIT_RESULT))
    tick(cenv, node)
    node.sessions.client = NoReports(node.sessions.client)      # from here on nothing this node reports arrives
    refused.emit(cx.reply(2, error=cx.NO_ROLLOUT))
    tick(cenv, node, times=2)
    assert json.loads((logs / PROVIDER_FILE).read_text()) == {sid: None}, "the fresh start is on the node's record"
    assert listed()["resume"] is True and msg(cenv, sid, 2)["state"] == "queued", "the hub heard nothing of it"

    again = make_node(cenv, config=codex_config(cenv), log_dir=logs)       # the node process died and came back
    again.sessions.recover()
    tick(cenv, again)
    m = msg(cenv, sid, 2)
    assert m["state"] == "failed" and m["detail"].startswith(LOST) and "starts a NEW one" in m["detail"], "loudly"
    assert children(cenv, again) == [] and listed()["resume"] is False
    send(cenv, sid, "third")
    tick(cenv, again)
    [fresh] = children(cenv, again)
    fresh.emit(cx.reply(1, cx.INIT_RESULT))
    tick(cenv, again)
    assert written(fresh)[2]["method"] == "thread/start", "not the refused resume again, and not a 'no record' failure"
    codex_handshake(cenv, fresh, node=again, thread=cx.CHILD)
    assert [m["state"] for m in show(cenv, sid)["messages"]] == ["done", "failed", "delivered"], "one failure, no more"
    assert json.loads((logs / PROVIDER_FILE).read_text()) == {sid: cx.CHILD}, "the new thread's id replaced the mark"
    assert show(cenv, sid)["provider_id"] == cx.CHILD and listed()["resume"] is True


def test_a_session_whose_codex_runs_a_turn_of_its_own_is_neither_idle_stopped_nor_evicted_for_room(tmp_path):
    """By the host's books such a process is idle. Stopping it would cut a turn that is running."""
    env = session_env(tmp_path, dispatch=CODEX_GRANT, config=lambda env: codex_config(env, max_live=1, idle_hours=1))
    a = start(env, "first", agent="codex")["session_id"]
    tick(env)
    [codex] = children(env)
    codex_handshake(env, codex)
    codex_turn(env, codex, "one")
    codex.emit(cx.own_turn("turn/started"))             # Codex goes on by itself
    tick(env)
    env.clock.advance(3600 + 1)                         # past idle_hours
    tick(env, times=2)
    assert codex.terminated == 0 and env.node.sessions.host.park(a) is False, "not stopped for being idle"
    env.node.hello()                                    # (the node's own loop says hello; an hour has passed)
    b = start(env, "hello", project=P2)["session_id"]   # a Claude session wants the one process slot
    tick(env, times=2)
    assert codex.terminated == 0 and len(children(env)) == 1 and msg(env, b, 1)["state"] == "queued", \
        "nor stopped to make room: the other session's message waits"
    codex.emit(cx.own_turn("turn/completed", status="completed"))
    tick(env, times=3)
    assert codex.terminated == 1, "its own turn is over: now it is idle, and makes room"
    assert len(children(env)) == 2 and msg(env, b, 1)["state"] == "delivered"


def test_a_codex_agent_cannot_be_hosted_on_a_node_that_did_not_turn_codex_on(tmp_path):
    env = session_env(tmp_path, dispatch=CODEX_GRANT, config=lambda env: codex_config(env, codex=False))
    sid = start(env, "first", agent="codex")["session_id"]
    tick(env)
    view = show(env, sid)
    assert view["state"] == "ended" and "cannot be hosted as a session" in view["error"]
    assert children(env) == [], "no process was started"
    claude = start(env, "hello")["session_id"]      # everything else is as it was
    tick(env)
    run_turn(env, claude, "hi")
    assert show(env, claude)["reply"] == "hi"


def test_the_provider_id_in_a_report_is_checked_and_a_claude_session_never_sends_one(senv):
    sid = start(senv)["session_id"]
    post = lambda **body: senv.laptop.post(f"/api/nodes/sessions/{sid}", json=body).status_code
    assert post(provider_id="two words") == 400 and post(provider_id="") == 400 and post(provider_id="x" * 129) == 400
    assert post(provider_id=7) == 422 and post(provider_id=["a"]) == 422
    assert show(senv, sid)["provider_id"] is None
    assert post(provider_id=cx.THREAD, resume=True) == 200 and show(senv, sid)["provider_id"] == cx.THREAD
    assert post(state="idle") == 200 and show(senv, sid)["provider_id"] == cx.THREAD, "a report without it keeps it"
    assert post(resume=False) == 200 and show(senv, sid)["provider_id"] is None, "the node starts the session anew"

    sent = []
    hub = senv.node.sessions.client

    class Recording:
        def request(self, method, path, **kw):
            if method == "POST":
                sent.append(kw.get("json"))
            return hub.request(method, path, **kw)

    senv.node.sessions.client = Recording()
    tick(senv)
    run_turn(senv, sid, "hello")
    assert show(senv, sid)["reply"] == "hello" and sent and not any("provider_id" in body for body in sent), \
        "Claude's own id is the session id: its reports are what they were"


def test_a_node_that_died_before_it_could_report_the_first_turn_resumes_the_same_codex_thread(cenv):
    """Codex may have recorded the turn. The hub was told nothing (not `delivered`, not the thread id, not `resume`),
    so only the node's own record can keep the restart from silently starting a NEW thread."""
    sid = start(cenv, "first", agent="codex")["session_id"]
    cenv.node.sessions.client = NoReports(cenv.node.sessions.client)     # it fetches; nothing it reports arrives
    tick(cenv)
    [old] = children(cenv)
    old.emit(cx.reply(1, cx.INIT_RESULT))
    tick(cenv)
    old.emit(cx.reply(2, cx.THREAD_RESULT))
    tick(cenv)
    assert written(old)[-1]["method"] == "turn/start"
    listed = cenv.laptop.get("/api/nodes/sessions").json()["sessions"][0]
    assert listed["resume"] is False and show(cenv, sid)["provider_id"] is None and msg(cenv, sid, 1)["state"] == "queued"

    node = make_node(cenv, config=codex_config(cenv), log_dir=cenv.tmp / "laptop-logs")
    node.procs.external[old.pid] = f"start-{old.pid}"       # the old session process is still alive
    node.sessions.recover()
    tick(cenv, node)
    m = msg(cenv, sid, 1)
    assert (m["state"], m["detail"]) == ("failed", LOST), "the turn is lost and said to be; it is not replayed"
    send(cenv, sid, "second")
    tick(cenv, node)
    [fresh] = children(cenv, node)
    fresh.emit(cx.reply(1, cx.INIT_RESULT))
    tick(cenv, node)
    resume = written(fresh)[2]
    assert (resume["method"], resume["params"]["threadId"]) == ("thread/resume", cx.THREAD)
    fresh.emit(cx.reply(2, cx.THREAD_RESULT))
    tick(cenv, node)
    assert [m["params"]["input"][0]["text"] for m in written(fresh) if m.get("method") == "turn/start"] == ["second"]
    codex_turn(cenv, fresh, "it remembers", node=node)
    view = show(cenv, sid)
    assert (view["reply"], view["provider_id"]) == ("it remembers", cx.THREAD)


def test_real_pipes_a_hosted_codex_session_through_the_hub_and_a_node_restart(tmp_path):
    env = session_env(tmp_path, dispatch=CODEX_GRANT, config=codex_config)     # its hello offers the codex runner
    exe = install_codex_stand_in(tmp_path / "bin")
    config = node_config(env, runners={"codex-cli": [str(exe), *CODEX_RUNNER[1:]]}, kill_grace_seconds=5,
                         sessions={"enabled": True, "codex": True})

    def until(runner, done, seconds=30.0):
        deadline = time.monotonic() + seconds
        while not done() and time.monotonic() < deadline:
            runner.pass_once()
            time.sleep(0.02)
        assert done(), show(env, sid)

    runner = SessionRunner(env.laptop, config, tmp_path / "real-logs", long_poll=0, sync_seconds=0)
    sid = start(env, "sleep 0.5 one", agent="codex")["session_id"]
    send(env, sid, "deny two")
    try:
        until(runner, lambda: msg(env, sid, 2)["state"] in ("done", "failed"))
        view = show(env, sid)
        assert [m["state"] for m in view["messages"]] == ["done", "done"]
        assert (view["reply"], view["turns"], view["denials"]) == ("turn 2: DENY TWO", 2, ["commandExecution"])
        pid, thread = view["pid"], view["provider_id"]
        assert pid and os.getpgid(pid) == pid and thread, "one process took both turns"
    finally:
        runner.shutdown()
    assert show(env, sid)["pid"] is None
    with pytest.raises(ProcessLookupError):
        os.killpg(pid, 0)

    again = SessionRunner(env.laptop, config, tmp_path / "real-logs", long_poll=0, sync_seconds=0)
    again.recover()
    send(env, sid, "recall")
    try:
        until(again, lambda: msg(env, sid, 3)["state"] in ("done", "failed"))
        view = show(env, sid)
        assert view["reply"] == "turn 3: FIRST WAS SLEEP 0.5 ONE", "the same thread, with its earlier turns"
        assert view["provider_id"] == thread and view["pid"] != pid
    finally:
        again.shutdown()


def test_the_end_to_end_script_passes_with_the_stand_in(tmp_path):
    """scripts/session-host-e2e.py: a real hub and a real `board node` as subprocesses in a temp directory, driven
    through `board session`. Its timing limits are for an idle machine; a test run gets more slack.

    A run that takes too long is stopped with SIGTERM, on which the script stops its hub, its node and every session
    process and removes its temp directory; only if that does not end it is it killed. (Killed outright, as
    `subprocess.run(timeout=...)` does, it would leave all of them running on this machine.)"""
    env = {k: v for k, v in os.environ.items() if not k.startswith(("AGENT_COMMS_", "BOARD_"))}
    proc = subprocess.Popen([sys.executable, str(ROOT / "scripts" / "session-host-e2e.py"), "--max-latency", "10",
                             "--max-wake", "10"],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env, cwd=str(tmp_path))
    try:
        out, err = proc.communicate(timeout=300)
    except subprocess.TimeoutExpired:
        proc.terminate()
        try:
            out, err = proc.communicate(timeout=120)
        except subprocess.TimeoutExpired:
            proc.kill()
            out, err = proc.communicate()
        pytest.fail("the end-to-end script did not finish in 300 seconds and was stopped:\n" + out[-4000:] + err[-2000:])
    assert proc.returncode == 0, out + err
    assert "PASS" in out and "stand-in" in out
