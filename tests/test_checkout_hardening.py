"""Checkout hardening, step 1 (DESIGN_NOTES "Checkout hardening for all turns and runs: design (2026-11-24)", D-1 and
the step-1 tests of J): the node stops what the hub's listing leaves out, stops runs before it starts any, and holds
one lock per OS user. Fake processes and the real hub (test_hosted_sessions), never a real agent CLI."""
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import httpx
import pytest

from agent_comms import hosted, node as node_mod
from agent_comms.node import NodeRunning, _Local, hold_lock, lock_path
from agent_comms.node_sessions import SESSION_PROTOCOL
from test_hosted_spawn import _no_codex_home_of_the_users, grant  # noqa: F401
from test_hosted_sessions import make_node, msg, show, start, tick, written  # noqa: F401
from test_hosted_tree import senv  # noqa: F401
from test_nodes import nenv  # noqa: F401
from test_session_host import result
from test_session_restart_hub import a_turn, c_turn, cdx, edit_msg, post_send
import test_providers_codex as cx

ROOT = Path(__file__).resolve().parents[1]


class Posts:
    """The session runner's client, recording every report it sends to the hub."""

    def __init__(self, real):
        self.real, self.bodies = real, []

    def request(self, method, path, **kw):
        if method == "POST":
            self.bodies.append((path, kw.get("json")))
        return self.real.request(method, path, **kw)

    def events_for(self, sid, n):
        return [e for path, body in self.bodies if path.endswith(sid) for e in (body or {}).get("events", [])
                if e.get("message") == n]


def record_posts(env) -> Posts:
    runner = env.node.sessions
    runner.client = Posts(runner.client)
    return runner.client


def host_of(env, sid):
    return env.node.sessions.host.sessions[sid]


def held_ids(s) -> list[str]:
    return [m.id for m in s.inbox] + [m.id for m in (s.writing, s.in_flight) if m is not None]


def listing(env) -> dict:
    r = env.laptop.get("/api/nodes/sessions", params={"tree": SESSION_PROTOCOL})
    assert r.status_code == 200, r.text
    return r.json()


def settle(env, sid, n) -> None:
    """The hub settles message n (as a node_lost or a human's decision would): its listing names it no more."""
    edit_msg(env, sid, n, state="failed", detail="node_lost: settled by the hub")


def damage(env, sid, n) -> None:
    """Message n's record on the hub can no longer be read (hosted._messages skips it)."""
    with hosted.db.write_tx(env.board.conn) as c:
        c.execute("UPDATE board_state SET value = ? WHERE key = ?", ("{not json", hosted._msg_key(sid, n)))


# ------------------------------------------------------------ D-1: what the listing leaves out, the node stops


def test_a_held_turn_the_listing_leaves_out_is_stopped_with_its_process_and_nothing_is_reported(senv):
    """N1: a turn the hub settled while the node held it no longer runs to its end."""
    sid, proc = a_turn(senv)
    posts = record_posts(senv)
    settle(senv, sid, 1)
    tick(senv)
    s = host_of(senv, sid)
    assert s.in_flight is None and s.writing is None and not s.inbox
    assert proc.terminated, "its process is stopped (the conversation resumes in the next one)"
    assert posts.events_for(sid, 1) == [], "the hub left it out, so nothing is reported for it"
    # Its late result changes nothing at the hub; the session goes on with the next message, in a new process.
    proc.emit(result(sid, text="too late"))
    tick(senv)
    assert msg(senv, sid, 1)["state"] == "failed" and show(senv, sid)["reply"] is None
    assert posts.events_for(sid, 1) == []
    n = post_send(senv, sid, "next")["message"]["n"]
    tick(senv)
    assert held_ids(host_of(senv, sid)) == [str(n)] and len(senv.node.session_spawner.children) == 2


def test_one_in_the_inbox_is_dropped_unwritten_and_nothing_is_reported(senv):
    """A Codex session's first message waits in the inbox for the handshake; the hub settles it meanwhile."""
    sid = start(senv, "first", agent="codex")["session_id"]
    tick(senv)
    child = cdx(senv)
    s = host_of(senv, sid)
    assert [m.id for m in s.inbox] == ["1"] and s.writing is None and s.in_flight is None
    posts = record_posts(senv)
    settle(senv, sid, 1)
    tick(senv)
    assert held_ids(s) == []
    child.emit(cx.reply(1, cx.INIT_RESULT))
    tick(senv)
    child.emit(cx.reply(2, cx.THREAD_RESULT))
    tick(senv)
    assert "turn/start" not in [m.get("method") for m in written(child)], "never written"
    assert posts.events_for(sid, 1) == []
    assert not child.terminated, "nothing was running: the process is left as it is"


def test_a_held_turn_with_a_delivered_report_still_waiting_is_stopped_all_the_same(senv):
    sid, proc = a_turn(senv)
    runner = senv.node.sessions
    runner.pending.append((sid, {"kind": "delivered", "message": 1}))     # its `delivered` not sent yet
    settle(senv, sid, 1)
    with runner.lock:
        runner._reconcile(listing(senv))
    s = host_of(senv, sid)
    assert s.in_flight is None and proc.terminated
    assert not any(x == sid and e.get("kind") == "turn_ended" for x, e in runner.pending)


def test_a_held_message_whose_turn_end_waits_to_be_sent_is_left_alone(senv):
    """Only a message for which no `turn_ended` report is waiting here is over for the hub."""
    sid, proc = a_turn(senv)
    runner = senv.node.sessions
    runner.pending.append((sid, {"kind": "turn_ended", "message": 1, "ok": True, "delivered": True}))
    settle(senv, sid, 1)
    with runner.lock:
        runner._reconcile(listing(senv))
    assert held_ids(host_of(senv, sid)) == ["1"] and not proc.terminated


def test_a_listing_whose_messages_is_not_a_list_changes_nothing(senv):
    sid, proc = a_turn(senv)
    runner = senv.node.sessions
    data = listing(senv)
    for bad in (None, "1", {"n": 1}):      # (None: the key is missing)
        for view in data["sessions"]:
            if view["session_id"] == sid:
                if bad is None:
                    view.pop("messages", None)
                else:
                    view["messages"] = bad
        with runner.lock:
            runner._reconcile(data)
        assert held_ids(host_of(senv, sid)) == ["1"] and not proc.terminated, bad


def test_a_restart_the_listing_no_longer_names_is_withdrawn_before_the_host_is_ticked(senv):
    """The restart's message sits in the inbox of a process that is being replaced (its binding changed), and its
    interrupt was asked for again by the listing meanwhile: once the hub leaves it out, the intent is cleared and the
    message dropped, both before the host is ticked."""
    sid, child = c_turn(senv)
    child.exits_on_terminate = False        # it takes its kill grace to go
    n = post_send(senv, sid, "start over", mode="restart")["message"]["n"]
    grant(senv, agents=("claude", "codex"))     # the session gets a binding: its process must be replaced
    tick(senv)
    s = host_of(senv, sid)
    assert s.restart_by == str(n) and s.unbound
    child.emit(cx.completed())              # turn 1 ends; message n is handed over and the process is stopped for it
    tick(senv)
    assert [m.id for m in s.inbox] == [str(n)] and s.stopping_since is not None, held_ids(s)
    tick(senv)                              # the listing still names n as a restart: its interrupt is asked for again
    assert s.restart_by == str(n) and [m.id for m in s.inbox] == [str(n)]
    posts = record_posts(senv)
    seen = []
    real_tick = senv.node.sessions.host.tick

    def first_tick():
        seen.append((s.restart_by, held_ids(s)))
        return real_tick()

    senv.node.sessions.host.tick = first_tick
    settle(senv, sid, n)
    tick(senv)
    assert seen[0] == (None, []), seen
    assert posts.events_for(sid, n) == []
    senv.clock.advance(60)                  # the old process is killed and gone; nothing is left to start a new one
    tick(senv)
    tick(senv)
    assert all("turn/start" not in [m.get("method") for m in written(c)] for c in senv.node.session_spawner.children
               if c is not child)
    assert [m.get("method") for m in written(child)].count("turn/start") == 1


def test_a_restart_whose_interrupt_was_not_written_yet_is_forgotten(senv):
    sid, proc = a_turn(senv)
    runner = senv.node.sessions
    n = post_send(senv, sid, "start over", mode="restart")["message"]["n"]
    with runner.lock:
        runner._reconcile(listing(senv))        # asked for, not written yet (the host has not been ticked)
    s = host_of(senv, sid)
    assert s.restart_by == str(n)
    settle(senv, sid, n)
    with runner.lock:
        runner._reconcile(listing(senv))
    assert s.restart_by is None and held_ids(s) == ["1"] and not proc.terminated, "turn 1 is still listed: it goes on"


def test_with_a_record_the_hub_cannot_read_the_other_sessions_go_on(senv):
    """The held turn whose record is damaged is stopped and nothing is sent for it, so the node's other sessions go on
    being fetched and reported."""
    sid, proc = a_turn(senv)
    posts = record_posts(senv)
    damage(senv, sid, 1)
    tick(senv)
    assert host_of(senv, sid).in_flight is None and proc.terminated
    assert posts.events_for(sid, 1) == []
    other = start(senv, "second", project="/work/second")["session_id"]
    tick(senv)
    tick(senv)
    assert msg(senv, other, 1)["state"] == "delivered", "fetched, handed over and reported"
    from test_hosted_sessions import run_turn
    run_turn(senv, other, text="fine")
    assert msg(senv, other, 1)["state"] == "done" and show(senv, other)["reply"] == "fine"
    assert posts.events_for(sid, 1) == []


# ------------------------------------------------------------ D-1: stop before start, in the run loop


class Assignments:
    """A hub that lists these runs to the node and takes its reports."""

    def __init__(self, runs):
        self.runs, self.posts = runs, []

    def request(self, method, path, **kw):
        req = httpx.Request(method, "http://hub" + path)
        if method == "GET" and path == "/api/nodes/assignments":
            return httpx.Response(200, json={"runs": self.runs}, request=req)
        self.posts.append((path, kw.get("json")))
        return httpx.Response(200, json={}, request=req)


def test_a_run_the_hub_no_longer_lists_or_asks_to_stop_is_signalled_before_an_assigned_run_is_looked_at(nenv):
    """N2: the replacement is never started first."""
    from test_dispatch import FakeChild
    node = nenv.node
    order = []
    node.client = Assignments([{"run_id": "new", "state": "assigned", "agent": "grok", "project": "x", "prompt": "p"},
                               {"run_id": "asked", "state": "running", "stop": "kill"}])
    for rid in ("gone", "asked"):
        node.running[rid] = _Local(rid, FakeChild(4000 + len(node.running)), nenv.clock(), 60, 10)
    node.last_hello = nenv.clock()
    real_stop = node._stop
    node._stop = lambda local, how: (order.append(("stop", local.run_id, how)), real_stop(local, how))[1]
    node._start = lambda r: order.append(("start", r["run_id"]))
    node.tick()
    assert order[:2] == [("stop", "gone", "terminate"), ("stop", "asked", "kill")], order
    assert ("start", "new") in order and order.index(("start", "new")) > 1


# ------------------------------------------------------------ D-1: one `board node` per OS user


def test_the_lock_lives_under_the_accounts_home_whatever_home_and_agent_comms_home_say(monkeypatch, tmp_path):
    import pwd
    want = Path(pwd.getpwuid(os.getuid()).pw_dir) / ".local" / "state" / "agent-comms" / "node.lock"
    assert lock_path() == want
    monkeypatch.setenv("HOME", str(tmp_path / "elsewhere"))
    monkeypatch.setenv("AGENT_COMMS_HOME", str(tmp_path / "another-home"))
    assert lock_path() == want


def test_the_lock_is_exclusive_private_and_not_passed_to_children(tmp_path):
    path = tmp_path / "state" / "agent-comms" / "node.lock"
    fd = hold_lock(path)
    try:
        assert (path.parent.stat().st_mode & 0o777) == 0o700
        assert not os.get_inheritable(fd)
        with pytest.raises(NodeRunning, match="already running"):
            hold_lock(path)         # another open file: refused, also inside one process
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], close_fds=False)
    finally:
        os.close(fd)
    try:
        again = hold_lock(path)     # the child, alive, does not hold it
        os.close(again)
    finally:
        child.kill()
        child.wait()


HOLDER = textwrap.dedent("""
    import sys, time
    from agent_comms.node import hold_lock
    fd = hold_lock(sys.argv[1])
    print("held", flush=True)
    time.sleep(60)
""")

# A `board node` whose account's home directory (the password database's) is the test's: only that is faked.
SECOND = textwrap.dedent("""
    import pwd, sys
    from types import SimpleNamespace
    pwd.getpwuid = lambda uid: SimpleNamespace(pw_dir=sys.argv[1])
    from agent_comms import cli, node
    node._run = lambda *a, **kw: sys.exit("reached the node's work")
    cli.main(["node", "--hub", "http://127.0.0.1:9", "--name", "laptop"])
""")


def test_a_second_board_node_exits_also_with_another_agent_comms_home_and_another_home(tmp_path):
    account = tmp_path / "account"
    path = account / ".local" / "state" / "agent-comms" / "node.lock"
    env = {k: v for k, v in os.environ.items() if not k.startswith("AGENT_COMMS")}
    env["PYTHONPATH"] = str(ROOT)
    holder = subprocess.Popen([sys.executable, "-c", HOLDER, str(path)], stdout=subprocess.PIPE, text=True, env=env,
                              cwd=tmp_path)
    try:
        assert holder.stdout.readline().strip() == "held"
        for home, ac_home in ((account, None), (tmp_path / "h2", tmp_path / "ac2"), (tmp_path / "h3", account)):
            e = env | {"HOME": str(home), "AGENT_COMMS_TOKEN": "not-a-real-token"}
            if ac_home is not None:
                e["AGENT_COMMS_HOME"] = str(ac_home)
            r = subprocess.run([sys.executable, "-c", SECOND, str(account)], capture_output=True, text=True, env=e,
                               cwd=tmp_path, timeout=60)
            assert r.returncode == 1, (r.stdout, r.stderr)
            assert "another `board node` of this user is already running" in r.stderr and "reached" not in r.stderr
    finally:
        holder.kill()
        holder.wait()
    # Once the first is gone, the next one takes the lock and goes on to its work.
    r = subprocess.run([sys.executable, "-c", SECOND, str(account)], capture_output=True, text=True,
                       env=env | {"AGENT_COMMS_TOKEN": "not-a-real-token"}, cwd=tmp_path, timeout=60)
    assert "reached the node's work" in r.stderr, (r.stdout, r.stderr)


def test_run_takes_the_lock_before_it_touches_anything(tmp_path, monkeypatch):
    path = tmp_path / "node.lock"
    fd = hold_lock(path)
    touched = []
    monkeypatch.setattr(node_mod, "_run", lambda *a, **kw: touched.append(a))
    try:
        with pytest.raises(SystemExit, match="already running"):
            node_mod.run("http://127.0.0.1:9", "token", lock=path)
    finally:
        os.close(fd)
    assert touched == []
    node_mod.run("http://127.0.0.1:9", "token", lock=path)
    assert len(touched) == 1
    os.close(hold_lock(path))       # released when `board node` ends
