"""Session host (session_host.py): one long-lived agent process per session, turns pushed on stdin, one in flight,
timeouts, restart with resume, leftovers of an earlier host, the transcript. Never starts a real agent CLI: children
are fakes with a scripted stdout and a captured stdin, except one test that runs a small stand-in script through the
real pipes."""

import json
import os
import signal
import stat
import sys
import time
from collections import deque
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_comms import runner_preflight as rp, session_host
from agent_comms.dispatch import DispatchConfig, spawn_piped
from agent_comms.providers import Event
from agent_comms.session_host import PROVIDER_FILE, STATE_FILE, SessionHost

import test_providers_codex as cx
from conftest import PROJECT, FakeClock
from test_claude_transcripts import tool_result, tool_use
from test_dispatch import FakeChild, FakeProcs, FakeSpawner

ROOT = Path(__file__).resolve().parents[1]
SID = "5b0c1f2e-7a6d-4e3b-9c8a-1d2e3f4a5b6c"
SID2 = "b66694f5-8c10-476a-ba2f-489c99218ed7"
OTHER = "9e8d7c6b-5a49-4382-a1b0-c9d8e7f6a5b4"
SHIPPED = ["claude", "-p", "{prompt}", "--permission-mode", "dontAsk", "--allowedTools=mcp__agent-comms"]
SECRET_INPUT = {"file_path": "/Users/someone/secret/notes.txt", "content": "TOOL-INPUT-SECRET"}


def init(sid=SID) -> dict:
    return {"type": "system", "subtype": "init", "session_id": sid, "permissionMode": "dontAsk", "tools": ["Read"]}


def result(sid=SID, denials=(), subtype="success", is_error=False, text="ok") -> dict:
    """A `result` event as Claude Code 2.1.322 writes it (the fields the host reads, and the reply text it must not)."""
    return {"type": "result", "subtype": subtype, "is_error": is_error, "num_turns": 1, "result": text,
            "session_id": sid, "stop_reason": "end_turn", "terminal_reason": "completed", "duration_ms": 2537,
            "permission_denials": [{"tool_name": t, "tool_use_id": "toolu_01", "tool_input": SECRET_INPUT}
                                   for t in denials]}


class FakeSessionChild(FakeChild):
    """FakeChild with the pipes a hosted session needs: `stdout` is the script, `stdin` what the host wrote."""

    def __init__(self, pid, exits_on_terminate=True):
        super().__init__(pid, exits_on_terminate)
        self.stdin = b""
        self.stdout = deque()
        self.accept = None      # most bytes one write takes (None: all of them; 0: the pipe is full)
        self.broken = False     # the child no longer reads

    def write(self, data):
        if self.broken:
            raise BrokenPipeError("Broken pipe")
        n = len(data) if self.accept is None else min(self.accept, len(data))
        self.stdin += data[:n]
        return n

    def read_lines(self):
        out = list(self.stdout)
        self.stdout.clear()
        return out

    def emit(self, *events):
        for e in events:
            self.stdout.append(e if isinstance(e, bytes) else
                               (e if isinstance(e, str) else json.dumps(e)).encode())

    def turns(self):
        """The user turns written so far (complete lines only)."""
        lines = self.stdin.split(b"\n")[:-1]
        return [json.loads(x)["message"]["content"] for x in lines]


def make_host(henv, **kw):
    return SessionHost(henv.config, henv.log_dir, timeout_minutes=kw.pop("timeout_minutes", 30),
                       spawner=kw.pop("spawner", henv.spawner), clock=henv.clock, probe=henv.procs.probe,
                       start_of=henv.procs.start, signal_group=lambda pid, sig: henv.signals.append((pid, sig)),
                       sleep=lambda s: henv.clock.advance(s), group_alive=lambda pid: pid in henv.groups, **kw)


@pytest.fixture
def henv(tmp_path):
    work = tmp_path / "checkout"
    work.mkdir()
    henv = SimpleNamespace(clock=FakeClock(), work=str(work), log_dir=tmp_path / "node-logs", signals=[],
                           groups=set())    # pids whose process group still has a member besides the (fake) leader
    henv.config = DispatchConfig.from_dict({"runners": {"claude-code": SHIPPED}, "worktrees": {PROJECT: str(work)},
                                            "timeout_minutes": 30, "kill_grace_seconds": 10})
    henv.spawner = FakeSpawner(child=FakeSessionChild)
    henv.procs = FakeProcs(henv.spawner)
    henv.host = make_host(henv)
    return henv


def open_session(henv, sid=SID, **kw):
    return henv.host.open(sid, agent="claude", runtime="claude-code", project=PROJECT, **kw)


def kinds(events, sid=None):
    return [(e.kind, e.message_id) for e in events if sid is None or e.session_id == sid]


def value_after(argv, flag):
    return argv[argv.index(flag) + 1]


# ------------------------------------------------------------ turns


def test_an_idle_message_is_written_within_one_tick_and_a_busy_one_waits_for_the_result(henv):
    s = open_session(henv)
    assert henv.host.tick() == [] and henv.spawner.calls == [], "nothing starts before there is a message"
    assert (s.state, s.dormant) == ("idle", True)
    first = henv.host.send(SID, "first")
    assert kinds(henv.host.tick()) == [("delivered", first)]
    [child] = henv.spawner.children
    assert child.turns() == ["first"]
    assert s.state == "starting", "Claude confirms the session only once it has a turn"

    second = henv.host.send(SID, "second")
    child.emit(init(), tool_use("toolu_01", name="Read"))
    assert kinds(henv.host.tick()) == [("turn_started", first)]
    assert s.state == "running" and s.provider_id == SID
    for _ in range(3):
        henv.clock.advance(5)
        assert henv.host.tick() == []
    assert child.turns() == ["first"], "the second message stays queued while the first turn is in flight"

    child.emit(tool_result("toolu_01", "file text"), result())
    events = henv.host.tick()
    assert kinds(events) == [("turn_ended", first), ("delivered", second)]
    assert events[0].ok is True and events[0].denials == ()
    assert events[0].meta == {"subtype": "success", "is_error": False, "num_turns": 1, "duration_ms": 2537,
                              "stop_reason": "end_turn", "terminal_reason": "completed"}, "never the reply text"
    assert child.turns() == ["first", "second"]
    assert len(henv.spawner.calls) == 1, "one process for the whole session"

    child.emit(init(), result())
    assert kinds(henv.host.tick()) == [("turn_started", second), ("turn_ended", second)]
    assert s.state == "idle" and not s.dormant
    third = henv.host.send(SID, "third")
    assert kinds(henv.host.tick()) == [("delivered", third)] and child.turns()[-1] == "third"
    assert s.state == "running", "the process already confirmed the session"


def test_a_turn_is_one_json_line_whatever_the_text_holds(henv):
    open_session(henv)
    text = 'line one\nline two\r\n{"type":"result"}  "quoted"'
    henv.host.send(SID, text)
    henv.host.tick()
    [child] = henv.spawner.children
    assert child.stdin.count(b"\n") == 1 and child.stdin.endswith(b"\n")
    assert json.loads(child.stdin) == {"type": "user", "message": {"role": "user", "content": text}}


def test_a_message_is_delivered_only_once_its_bytes_are_written(henv):
    s = open_session(henv, SID2)
    slow = FakeSessionChild(2000)
    slow.accept = 0
    host = henv.host
    host.spawner = lambda argv, **kw: slow
    mid = host.send(SID2, "y" * 5000)
    assert host.tick() == [] and slow.stdin == b"", "the pipe is full: nothing is delivered"
    assert s.writing is not None and s.in_flight is None
    slow.accept = 3000
    assert host.tick() == [] and len(slow.stdin) == 3000, "part of a line is not a delivery"
    assert json.loads((henv.log_dir / STATE_FILE).read_text())[SID2]["turn"] == mid
    assert kinds(host.tick()) == [("delivered", mid)]
    assert slow.turns() == ["y" * 5000]


def test_a_message_the_process_never_took_fails_as_undelivered(henv):
    s = open_session(henv)
    dead = FakeSessionChild(2000, exits_on_terminate=True)
    dead.broken = True
    henv.host.spawner = lambda argv, **kw: dead
    mid = henv.host.send(SID, "hello")
    [ev] = henv.host.tick()
    assert (ev.kind, ev.message_id, ev.ok, ev.delivered) == ("turn_ended", mid, False, False)
    assert "could not write" in ev.detail and dead.terminated == 1
    assert s.state == "idle" and s.dormant and not s.inbox, "it is reported, not retried behind the caller's back"


def test_a_process_that_exits_mid_turn_fails_the_turn_and_the_next_message_resumes(henv):
    s = open_session(henv)
    first = henv.host.send(SID, "first")
    henv.host.tick()
    [child] = henv.spawner.children
    child.emit(init())
    henv.host.tick()
    child.code = 1
    [ev] = henv.host.tick()
    assert (ev.kind, ev.message_id, ev.ok, ev.delivered) == ("turn_ended", first, False, True)
    assert "exited (code 1)" in ev.detail and s.dormant
    assert henv.host.tick() == [] and len(henv.spawner.calls) == 1, "nothing restarts without a message"
    second = henv.host.send(SID, "second")
    assert kinds(henv.host.tick()) == [("delivered", second)]
    argv = henv.spawner.calls[1]["argv"]
    assert value_after(argv, "--resume") == SID and "--session-id" not in argv
    assert henv.spawner.children[1].turns() == ["second"], "the lost turn is not written again"


def test_a_result_written_just_before_the_process_exits_still_counts(henv):
    open_session(henv)
    mid = henv.host.send(SID, "bye")
    henv.host.tick()
    [child] = henv.spawner.children
    child.emit(init(), result())
    child.code = 0
    events = henv.host.tick()
    assert kinds(events) == [("turn_started", mid), ("turn_ended", mid)] and events[1].ok is True


# ------------------------------------------------------------ timeout and restart


def test_a_turn_past_the_timeout_is_terminated_and_failed_and_the_next_message_resumes(henv):
    s = open_session(henv)
    first = henv.host.send(SID, "first")
    henv.host.tick()
    [child] = henv.spawner.children
    child.emit(init())
    henv.host.tick()
    henv.clock.advance(30 * 60 - 1)
    assert henv.host.tick() == [] and child.terminated == 0
    henv.clock.advance(1)
    [ev] = henv.host.tick()
    assert (ev.kind, ev.message_id, ev.ok, ev.delivered) == ("turn_ended", first, False, True)
    assert ev.detail == "timed out after 30 minutes"
    assert child.terminated == 1 and child.killed == 0, "SIGTERM for its process group (PopenChild.terminate)"
    assert (s.state, s.dormant, s.in_flight) == ("idle", True, None)
    assert henv.host.tick() == [] and len(henv.spawner.calls) == 1, "dormant until there is a message"

    second = henv.host.send(SID, "second")
    assert kinds(henv.host.tick()) == [("delivered", second)]
    assert len(henv.spawner.calls) == 2
    argv = henv.spawner.calls[1]["argv"]
    assert value_after(argv, "--resume") == SID and "--session-id" not in argv
    fresh = henv.spawner.children[1]
    assert fresh.turns() == ["second"], "the interrupted turn is never replayed"
    fresh.emit(init(), result())
    assert kinds(henv.host.tick()) == [("turn_started", second), ("turn_ended", second)]


def test_a_process_that_ignores_sigterm_is_killed_after_the_grace_period_before_anything_restarts(henv):
    s = open_session(henv)
    stubborn = FakeSessionChild(2000, exits_on_terminate=False)
    henv.host.spawner = lambda argv, **kw: stubborn
    first = henv.host.send(SID, "first")
    queued = henv.host.send(SID, "queued")
    henv.host.tick()
    henv.clock.advance(30 * 60)
    assert kinds(henv.host.tick()) == [("turn_ended", first)]
    henv.host.spawner = henv.spawner
    stubborn.emit(result())     # too late: the turn was already given up
    henv.clock.advance(9)
    assert henv.host.tick() == [] and stubborn.killed == 0 and henv.spawner.calls == []
    assert not s.dormant and s.inbox, "the queued message waits for the old process to be gone"
    henv.clock.advance(1)
    assert kinds(henv.host.tick()) == [("delivered", queued)]
    assert stubborn.killed == 1 and len(henv.spawner.calls) == 1
    assert "--session-id" in henv.spawner.calls[0]["argv"], "the session was never confirmed, so nothing to resume"
    assert stubborn.turns() == ["first"]


def test_a_stopped_group_that_outlives_its_leader_holds_the_next_start_until_it_is_killed(henv):
    """The leader exits on SIGTERM; a tool process in its group does not and may still be writing files."""
    s = open_session(henv)
    first = henv.host.send(SID, "first")
    queued = henv.host.send(SID, "queued")
    henv.host.tick()
    [child] = henv.spawner.children
    child.emit(init())
    henv.host.tick()
    henv.groups.add(child.pid)
    henv.clock.advance(30 * 60)
    assert kinds(henv.host.tick()) == [("turn_ended", first)]
    assert child.terminated == 1 and child.poll() == -15, "the leader is gone"
    assert not s.dormant and len(henv.spawner.calls) == 1 and s.inbox, "nothing starts beside the rest of its group"
    henv.clock.advance(9)
    assert henv.host.tick() == [] and child.killed == 0 and len(henv.spawner.calls) == 1
    henv.clock.advance(1)
    assert henv.host.tick() == [] and child.killed == 1, "SIGKILL for the group after the grace period"
    assert len(henv.spawner.calls) == 1, "still there this pass"
    henv.groups.clear()
    assert kinds(henv.host.tick()) == [("delivered", queued)]
    assert value_after(henv.spawner.calls[1]["argv"], "--resume") == SID


def test_a_process_that_exits_by_itself_is_held_until_its_group_is_gone(henv):
    """Spawn review round 5, S6 (this replaces the old rule that such a process was not held for its group): its
    turn fails at once, as on any exit of its own, but its group is stopped as the stop path stops one, and nothing
    starts beside it (nor is it let go of) until the whole group is gone."""
    s = open_session(henv)
    first = henv.host.send(SID, "first")
    queued = henv.host.send(SID, "queued")
    henv.host.tick()
    [child] = henv.spawner.children
    henv.groups.add(child.pid)
    child.code = 1
    assert kinds(henv.host.tick()) == [("turn_ended", first)], "its turn is over: the leader is gone"
    assert child.terminated == 1 and s.child is child and len(henv.spawner.calls) == 1, \
        "its group is signalled, and nothing starts beside what is left of it"
    henv.clock.advance(henv.host.kill_grace)
    henv.host.tick()
    assert child.killed == 1 and s.child is child and len(henv.spawner.calls) == 1
    henv.groups.discard(child.pid)              # the group is gone
    assert kinds(henv.host.tick()) == [("delivered", queued)]
    assert len(henv.spawner.calls) == 2 and not s.dormant


def test_stop_all_kills_a_group_whose_leader_exited_but_which_still_has_members(henv):
    open_session(henv)
    henv.host.send(SID, "first")
    henv.host.tick()
    [child] = henv.spawner.children
    henv.groups.add(child.pid)
    start = henv.clock()
    henv.host.stop_all()
    assert child.terminated == 1, "the leader exited on SIGTERM (FakeChild)"
    assert henv.clock() - start >= 10, "it waited the grace period for the group"
    assert child.killed == 1


def test_a_session_confirmed_while_its_process_is_being_stopped_resumes(henv):
    """Claude refuses --session-id for a session that exists ("already in use"), so a late init must still count."""
    s = open_session(henv)
    stubborn = FakeSessionChild(2000, exits_on_terminate=False)
    henv.host.spawner = lambda argv, **kw: stubborn
    first = henv.host.send(SID, "first")
    henv.host.tick()
    henv.clock.advance(30 * 60)
    assert kinds(henv.host.tick()) == [("turn_ended", first)] and s.resume is False
    stubborn.emit(init(), result())
    assert henv.host.tick() == [], "the turn was already given up: its late events are dropped"
    assert s.resume is True and s.ready is False and s.state == "idle"
    assert json.loads((henv.log_dir / STATE_FILE).read_text())[SID]["resume"] is True
    henv.host.spawner = henv.spawner
    henv.clock.advance(10)
    second = henv.host.send(SID, "second")
    assert kinds(henv.host.tick()) == [("delivered", second)]
    argv = henv.spawner.calls[0]["argv"]
    assert value_after(argv, "--resume") == SID and "--session-id" not in argv


def test_stop_all_fails_the_turn_in_flight_and_clears_the_state_file(henv):
    open_session(henv)
    mid = henv.host.send(SID, "first")
    henv.host.tick()
    [child] = henv.spawner.children
    assert json.loads((henv.log_dir / STATE_FILE).read_text())[SID]["pid"] == child.pid
    [ev] = henv.host.stop_all()
    assert (ev.kind, ev.message_id, ev.ok) == ("turn_ended", mid, False) and "shut down" in ev.detail
    assert child.terminated == 1
    assert json.loads((henv.log_dir / STATE_FILE).read_text()) == {}


# ------------------------------------------------------------ session id


def test_a_session_id_mismatch_on_init_is_fatal_for_that_session_only(henv):
    bad = open_session(henv, SID)
    good = open_session(henv, SID2)
    m1 = henv.host.send(SID, "one")
    queued = henv.host.send(SID, "queued")
    m2 = henv.host.send(SID2, "two")
    henv.host.tick()
    c1, c2 = henv.spawner.children
    assert value_after(henv.spawner.calls[0]["argv"], "--session-id") == SID
    assert value_after(henv.spawner.calls[1]["argv"], "--session-id") == SID2
    c1.emit(init(OTHER), result(OTHER))
    c2.emit(init(SID2))
    events = henv.host.tick()
    assert kinds(events, SID) == [("turn_ended", m1), ("ended", None)]
    failed, ended = [e for e in events if e.session_id == SID]
    assert failed.ok is False and "session id" in ended.detail and OTHER not in ended.detail
    assert ended.undelivered == (queued,)
    assert bad.state == "ended" and c1.terminated == 1 and c1.turns() == ["one"]
    with pytest.raises(ValueError):
        henv.host.send(SID, "more")

    assert kinds(events, SID2) == [("turn_started", m2)] and good.state == "running" and c2.terminated == 0
    c2.emit(result(SID2))
    assert kinds(henv.host.tick()) == [("turn_ended", m2)]
    assert good.state == "idle"
    assert list(json.loads((henv.log_dir / STATE_FILE).read_text())) == [SID2]
    henv.host.forget(SID)
    assert SID not in henv.host.sessions


def test_a_session_whose_process_cannot_start_ends_and_names_its_queue(henv):
    open_session(henv)
    henv.host.spawner = FakeSpawner(fail_for={"claude"}, child=FakeSessionChild)
    a, b = henv.host.send(SID, "a"), henv.host.send(SID, "b")
    [ev] = henv.host.tick()
    assert ev.kind == "ended" and ev.undelivered == (a, b) and "could not start" in ev.detail
    assert henv.host.state(SID) == "ended"


# ------------------------------------------------------------ leftovers of an earlier host


def test_orphan_cleanup_on_host_start_stops_only_verified_leftovers_before_starting_anything(henv):
    order = []
    henv.procs.external = {4001: "start-4001", 4002: "start-reused", 4003: None, 4005: "start-4005"}
    henv.log_dir.mkdir(parents=True)
    (henv.log_dir / STATE_FILE).write_text(json.dumps({
        SID: {"pid": 4001, "proc_start": "start-4001", "cwd": henv.work, "resume": True, "turn": "m7"},   # ours
        SID2: {"pid": 4002, "proc_start": "start-4002", "cwd": henv.work},     # pid reused by something else
        OTHER: {"pid": 4003, "proc_start": "start-4003"},                      # alive, identity unverifiable
        "11111111-2222-4333-8444-555555555555": {"pid": 4004, "proc_start": "start-4004"},     # gone
        "not-a-session-id": {"pid": 4005, "proc_start": "start-4005"},         # not something this host wrote
        "66666666-2222-4333-8444-555555555555": "garbage",
    }))

    def spawner(argv, **kw):
        order.append("spawn")
        return henv.spawner(argv, **kw)
    host = SessionHost(henv.config, henv.log_dir, spawner=spawner, clock=henv.clock, probe=henv.procs.probe,
                       start_of=henv.procs.start, signal_group=lambda pid, sig: order.append((pid, sig)),
                       sleep=lambda s: order.append(("sleep", s)), group_alive=lambda pid: False)
    host.open(SID, agent="claude", runtime="claude-code", project=PROJECT, resume=True)
    host.send(SID, "after restart")
    host.tick()
    assert order == [(4001, signal.SIGTERM), ("sleep", 10.0), (4001, signal.SIGKILL), "spawn"]
    assert value_after(henv.spawner.calls[0]["argv"], "--resume") == SID
    assert list(json.loads((henv.log_dir / STATE_FILE).read_text())) == [SID], "only what this host now runs"


def test_recover_reports_what_the_last_host_left_and_runs_once(henv):
    henv.procs.external = {4001: "start-4001"}
    henv.log_dir.mkdir(parents=True)
    (henv.log_dir / STATE_FILE).write_text(json.dumps({
        SID: {"pid": 4001, "proc_start": "start-4001", "resume": True, "turn": "m7"},
        SID2: {"pid": 4002, "proc_start": "start-4002", "resume": False, "turn": None}}))
    left = henv.host.recover()
    assert left == {SID: {"stopped": True, "resume": True, "turn": "m7"},
                    SID2: {"stopped": False, "resume": False, "turn": None}}
    assert [s for _, s in henv.signals] == [signal.SIGTERM, signal.SIGKILL] and {p for p, _ in henv.signals} == {4001}
    assert henv.host.recover() == {} and len(henv.signals) == 2
    assert json.loads((henv.log_dir / STATE_FILE).read_text()) == {}


def leftovers(henv, groups):
    """Three recorded sessions: two verified leaders that exit on SIGTERM (4001, 4002) and one live pid whose identity
    cannot be verified (4003). `groups`: the process groups that still have a member whatever SIGTERM did."""
    henv.procs.external = {4001: "start-4001", 4002: "start-4002", 4003: None}
    henv.groups.update(groups)
    henv.log_dir.mkdir(parents=True)
    (henv.log_dir / STATE_FILE).write_text(json.dumps({
        SID: {"pid": 4001, "proc_start": "start-4001", "resume": True, "turn": "m7"},
        SID2: {"pid": 4002, "proc_start": "start-4002"}, OTHER: {"pid": 4003, "proc_start": "start-4003"}}))

    def signal_group(pid, sig):
        henv.signals.append((pid, sig))
        if sig == signal.SIGTERM:
            henv.procs.external.pop(pid, None)      # the leader goes; whatever ignores SIGTERM stays in its group
    henv.host.signal_group = signal_group


def test_recover_kills_a_leftover_group_whose_leader_exited_on_sigterm(henv):
    """The leader no longer verifies after the grace period, so stop_verified sends no SIGKILL; a tool process in its
    group that ignored SIGTERM would otherwise run on beside the resumed session."""
    leftovers(henv, groups={4001, 4003})
    kill = henv.host.signal_group

    def signal_group(pid, sig):
        kill(pid, sig)
        if sig == signal.SIGKILL:
            henv.groups.discard(pid)
    henv.host.signal_group = signal_group
    start = henv.clock()
    left = henv.host.recover()
    assert henv.signals == [(4001, signal.SIGTERM), (4002, signal.SIGTERM), (4001, signal.SIGKILL)], \
        "only the group that outlived its verified leader; never the unverifiable pid's"
    assert {k: v["stopped"] for k, v in left.items()} == {SID: True, SID2: True, OTHER: False}
    assert henv.clock() - start == 10, "the group was gone at once: no further wait"
    assert 4003 in henv.groups and json.loads((henv.log_dir / STATE_FILE).read_text()) == {}


def test_recover_waits_at_most_the_grace_period_for_a_killed_group(henv):
    leftovers(henv, groups={4001})      # this fake group never goes
    start = henv.clock()
    henv.host.recover()
    assert henv.signals[-1] == (4001, signal.SIGKILL) and henv.signals.count((4001, signal.SIGKILL)) == 1
    assert 20 <= henv.clock() - start < 21, "one grace period for SIGTERM, at most one more for the killed group"


def test_real_recover_kills_a_group_member_that_ignored_sigterm(tmp_path):
    """Real processes: a leftover leader that exits on SIGTERM and a child in its group that ignores it."""
    leader = spawn_piped([sys.executable, "-c", (
        "import signal, subprocess, sys, time\n"
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(600)'],\n"
        "                 preexec_fn=lambda: signal.signal(signal.SIGTERM, signal.SIG_IGN))\n"
        "print('up', flush=True)\n"
        "time.sleep(600)\n")], cwd=str(tmp_path), env=dict(os.environ), log_path=tmp_path / "stderr.log")
    try:
        deadline = time.monotonic() + 10
        while not leader.read_lines() and time.monotonic() < deadline:
            time.sleep(0.02)
        from agent_comms.dispatch import process_start
        log_dir = tmp_path / "node-logs"
        log_dir.mkdir()
        (log_dir / STATE_FILE).write_text(json.dumps({
            SID: {"pid": leader.pid, "proc_start": process_start(leader.pid), "resume": True, "turn": None}}))

        def sleep(seconds):
            time.sleep(seconds)
            leader.poll()       # the test is this leader's parent: reap it, as a dead host's leftovers already are
        host = SessionHost(DispatchConfig.from_dict({"kill_grace_seconds": 1}), log_dir, sleep=sleep)
        assert host.recover() == {SID: {"stopped": True, "resume": True, "turn": None}}
        assert leader.poll() == -signal.SIGTERM, "the leader went on SIGTERM; only the group SIGKILL reached the rest"
        assert not session_host.group_alive(leader.pid)
        assert_group_gone(leader.pid, seconds=1.0)
    finally:
        leader.kill()
        leader.close()


def test_a_host_with_no_state_file_recovers_nothing_and_writes_none(henv):
    assert henv.host.recover() == {} and henv.signals == []
    assert not (henv.log_dir / STATE_FILE).exists()


# ------------------------------------------------------------ argv


def test_the_derived_argv_is_scoped_has_no_prompt_and_carries_the_stream_flags(henv):
    henv.config.claude_tool_projects = [PROJECT]
    open_session(henv)
    henv.host.send(SID, "the message")
    henv.host.tick()
    [call] = henv.spawner.calls
    argv = call["argv"]
    assert argv[:2] == ["claude", "-p"]
    assert not any(a in ("{prompt}", "{project}", "{thread}") for a in argv), "no placeholder is left"
    assert "the message" not in argv, "a turn travels on stdin, never in the argv"
    assert value_after(argv, "--input-format") == "stream-json"
    assert value_after(argv, "--output-format") == "stream-json" and "--verbose" in argv
    assert value_after(argv, "--permission-prompts") == "none"
    assert argv[-2:] == ["--session-id", SID]
    # Scoped exactly as a dispatched run in an opted-in project is (runner_preflight.scoped_template).
    assert value_after(argv, "--permission-mode") == "dontAsk" and argv.count("--permission-mode") == 1
    assert "--allowedTools=" + ",".join(rp.ALLOWED) in argv and "--allowedTools=mcp__agent-comms" not in argv
    assert any(a.startswith("--disallowedTools=") and rp.DENIED[0] in a for a in argv)
    assert call["cwd"] == henv.work
    assert not any("TOKEN" in k.upper() for k in call["env"]) and "AGENT_COMMS_HOME" in call["env"]
    assert call["log_path"] == henv.log_dir / "sessions" / f"{SID}.stderr.log"


def test_a_project_that_is_not_opted_in_keeps_the_runner_as_written(henv):
    """claude_tool_projects is the human's opt-in to the wider scoped tools; a session never widens a runner itself.
    The one thing a hosted session adds to the shipped runner is restrictive: the settings key that switches the
    bypass mode off (session_access.claude_sealed), for every launch that is not a full-access one."""
    open_session(henv)
    henv.host.send(SID, "hello")
    henv.host.tick()
    argv = henv.spawner.calls[0]["argv"]
    assert argv == ["claude", "-p", "--permission-mode", "dontAsk", "--allowedTools=mcp__agent-comms",
                    "--settings", '{"permissions":{"disableBypassPermissionsMode":"disable"}}',
                    "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
                    "--permission-prompts", "none", "--session-id", SID]
    assert henv.spawner.children[0].stdin.count(b"\n") == 1, "the turn and nothing else: the process is asked nothing"


def test_the_shipped_claude_runner_is_the_template_these_tests_derive_from():
    shipped = DispatchConfig.load(ROOT / "board.toml", local=False).runners["claude-code"]
    assert shipped == SHIPPED


def test_writable_roots_reach_an_unscoped_session_like_a_dispatched_run(henv, tmp_path):
    extra = tmp_path / "extra"
    extra.mkdir()
    henv.config.writable_roots = {PROJECT: [str(extra)]}
    s = open_session(henv)
    assert f"--add-dir={extra}" in s.template
    henv.config.claude_tool_projects = [PROJECT]
    assert not any(a.startswith("--add-dir") for a in open_session(henv, SID2).template), "scoped runs get none"


@pytest.mark.parametrize("extra", [["--session-id", OTHER], ["--resume", OTHER], ["--continue"], ["-c"],
                                   ["--output-format", "json"], ["--output-format=text"],
                                   ["--input-format", "text"], ["--permission-prompts", "host"],
                                   ["--permission-prompt-tool", "mcp__x__approve"], ["--fork-session"],
                                   ["--no-session-persistence"]])
def test_a_runner_that_sets_session_or_stream_flags_is_refused_at_open(henv, extra):
    for scoped in ([], [PROJECT]):
        henv.config.claude_tool_projects = scoped
        henv.config.runners["claude-code"] = SHIPPED + extra
        with pytest.raises(ValueError):
            open_session(henv)
    assert henv.host.sessions == {} and henv.spawner.calls == []


@pytest.mark.parametrize("model", ["--dangerously-skip-permissions", "-x", "haiku --resume x", "haiku\n", "",
                                   "a/b", "$(id)", "x" * 101, 7, ["haiku"]])
def test_a_model_override_that_is_not_a_plain_name_never_reaches_the_argv(henv, model):
    with pytest.raises(ValueError):
        open_session(henv, model=model)
    assert henv.host.sessions == {}


def test_a_model_override_replaces_the_runners_own_model(henv):
    for i, own in enumerate((["--model", "haiku"], ["--model=haiku"], [])):
        henv.config.runners["claude-code"] = SHIPPED[:3] + own + SHIPPED[3:]
        sid = f"0000000{i}-7a6d-4e3b-9c8a-1d2e3f4a5b6c"
        open_session(henv, sid, model="claude-opus-4-1[1m]")
        henv.host.send(sid, "hi")
        henv.host.tick()
        argv = henv.spawner.calls[-1]["argv"]
        assert value_after(argv, "--model") == "claude-opus-4-1[1m]" and not any("haiku" in a for a in argv)
        assert sum(a.split("=")[0] == "--model" for a in argv) == 1
    henv.config.runners["claude-code"] = SHIPPED[:3] + ["--model", "haiku"] + SHIPPED[3:]
    assert value_after(open_session(henv).provider.argv(
        henv.config.runners["claude-code"], session_id=SID, resume=False, model=None), "--model") == "haiku"


@pytest.mark.parametrize("sid", ["", "abc", SID.upper(), SID + "\n", "../" + SID, SID.replace("-", ""), None])
def test_only_a_lower_case_uuid_is_a_session_id(henv, sid):
    with pytest.raises(ValueError):
        open_session(henv, sid)


def test_open_refuses_what_this_machine_cannot_host(henv):
    with pytest.raises(LookupError, match="no runner"):
        henv.host.open(SID, agent="grok", runtime="grok", project=PROJECT)
    henv.config.runners["codex-cli"] = ["codex", "exec", "{prompt}"]
    with pytest.raises(LookupError, match="cannot be hosted"):
        henv.host.open(SID, agent="codex", runtime="codex-cli", project=PROJECT)
    with pytest.raises(FileNotFoundError):
        henv.host.open(SID, agent="claude", runtime="claude-code", project="/work/elsewhere")
    open_session(henv)
    with pytest.raises(ValueError, match="already open"):
        open_session(henv)
    with pytest.raises(ValueError):
        henv.host.send(SID, "   ")
    with pytest.raises(LookupError):
        henv.host.send(SID2, "hello")


# ------------------------------------------------------------ transcript and state files


def test_the_transcript_keeps_every_stdout_line_and_is_mode_600(henv):
    open_session(henv)
    henv.host.send(SID, "first")
    henv.host.tick()
    [child] = henv.spawner.children
    lines = [json.dumps(init()), tool_use("toolu_01", name="Read"), "not json at all", json.dumps(result())]
    child.emit(*lines)
    henv.host.tick()
    path = henv.host.transcript_path(SID)
    assert path == henv.log_dir / "sessions" / f"{SID}.jsonl"
    assert path.read_text().splitlines() == lines
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE((henv.log_dir / "sessions").stat().st_mode) == 0o700
    assert stat.S_IMODE((henv.log_dir / STATE_FILE).stat().st_mode) == 0o600
    record = json.loads((henv.log_dir / STATE_FILE).read_text())[SID]
    assert record == {"pid": child.pid, "proc_start": f"start-{child.pid}", "cwd": henv.work, "resume": True,
                      "turn": None}


def test_a_restarted_process_appends_to_the_same_transcript_and_a_loose_file_is_tightened(henv):
    (henv.log_dir / "sessions").mkdir(parents=True)
    path = henv.log_dir / "sessions" / f"{SID}.jsonl"
    path.write_text("earlier line\n")
    os.chmod(path, 0o644)
    open_session(henv)
    henv.host.send(SID, "first")
    henv.host.tick()
    henv.spawner.children[0].emit(init())
    henv.host.tick()
    henv.spawner.children[0].code = 0
    henv.host.tick()
    henv.host.send(SID, "second")
    henv.host.tick()
    henv.spawner.children[1].emit(init(), result())
    henv.host.tick()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert [json.loads(x).get("type") if x.startswith("{") else x for x in path.read_text().splitlines()] == [
        "earlier line", "system", "system", "result"]


# ------------------------------------------------------------ permission denials


def test_denials_on_a_result_put_the_session_in_waiting_with_tool_names_only(henv):
    s = open_session(henv)
    mid = henv.host.send(SID, "write the file")
    henv.host.tick()
    [child] = henv.spawner.children
    child.emit(init(), result(denials=["Write", "Bash", "Write", "bad name; rm -rf"]))
    events = henv.host.tick()
    ended = events[-1]
    assert (ended.kind, ended.message_id, ended.ok) == ("turn_ended", mid, True)
    assert ended.denials == ("Write", "Bash", "unknown") and s.denials == ended.denials
    assert s.state == "waiting" and henv.host.state(SID) == "waiting"
    assert "TOOL-INPUT-SECRET" not in repr(events) + repr(s) and "secret/notes" not in repr(events) + repr(s)
    assert "TOOL-INPUT-SECRET" not in (henv.log_dir / STATE_FILE).read_text()
    assert henv.host.tick() == [] and s.state == "waiting", "it stays waiting until there is another message"

    nxt = henv.host.send(SID, "try another way")
    assert kinds(henv.host.tick()) == [("delivered", nxt)]
    assert s.state == "running" and s.denials == ()
    child.emit(init(), result())
    henv.host.tick()
    assert s.state == "idle"


# ------------------------------------------------------------ the provider seam


class HandshakeProvider:
    """A provider shaped like a request/response protocol: a handshake before any turn, its own interrupt, and an id
    of its own for the session (which the host must hand back after a restart)."""
    waits_for_ready = True

    def __init__(self):
        self.fed, self.adopted, self.gone = [], [], False

    def adopt(self, provider_id):
        self.adopted.append(provider_id)

    def session_exists(self, stderr):
        return False if self.gone else None

    def argv(self, template, *, session_id, resume, model):
        self.gone = False
        return [template[0], "app-server", "resume" if resume else "new", session_id]

    def on_open(self):
        return [b'{"method":"initialize"}\n']

    def turn(self, text, kind="user"):
        self.kinds = getattr(self, "kinds", []) + [kind]
        return [json.dumps({"method": "turn/start", "text": text}).encode() + b"\n"]

    def feed(self, line):
        self.fed.append(line)
        if line == b"hello":
            return [Event("ready", provider_id="thread-1")], [b'{"method":"initialized"}\n']
        if line == b"asks":
            return [Event("approval_requested", denials=("shell",))], []
        if line == b"interrupted":
            return [Event("turn_ended", ok=True)], []
        if line == b"started":
            return [Event("turn_started")], []
        if line == b"refused":
            return [Event("start_failed", detail="the provider refused: busy")], []
        if line == b"gone":
            self.gone = True
            return [Event("start_failed", detail="no thread thread-1: the next message starts a NEW thread")], []
        return [], []

    def interrupt(self):
        return [b'{"method":"turn/interrupt"}\n']


def handshake_host(henv, **kw):
    henv.config.runners["codex-cli"] = ["codex", "exec", "{prompt}"]
    host = make_host(henv, provider_for=lambda template: HandshakeProvider, **kw)
    return host, host.open(SID, agent="codex", runtime="codex-cli", project=PROJECT)


def test_a_provider_with_a_handshake_gets_no_turn_before_it_is_ready_and_interrupts_in_band(henv):
    host, s = handshake_host(henv)
    mid = host.send(SID, "do it")
    assert host.tick() == []
    [child] = henv.spawner.children
    assert henv.spawner.calls[0]["argv"] == ["codex", "app-server", "new", SID]
    assert child.stdin == b'{"method":"initialize"}\n' and s.state == "starting"
    assert host.tick() == [] and child.stdin.count(b"\n") == 1, "the turn waits for the handshake"

    child.emit(b"hello")
    assert kinds(host.tick()) == [("delivered", mid)]
    assert child.stdin.splitlines()[1:] == [b'{"method":"initialized"}', b'{"method": "turn/start", "text": "do it"}']
    assert (s.state, s.provider_id, s.resume) == ("running", "thread-1", True), \
        "from the first turn written to it the provider's session is one to resume"
    assert json.loads((henv.log_dir / STATE_FILE).read_text())[SID]["resume"] is True
    child.emit(b"started")
    assert kinds(host.tick()) == [("turn_started", mid)]

    child.emit(b"asks")
    [ev] = host.tick()
    assert (ev.kind, ev.message_id, ev.denials) == ("approval_requested", mid, ("shell",))
    assert s.state == "waiting" and s.in_flight is not None, "the turn is still in flight; nothing was approved"

    henv.clock.advance(30 * 60)
    assert host.tick() == [] and child.terminated == 0
    assert child.stdin.splitlines()[-1] == b'{"method":"turn/interrupt"}'
    child.emit(b"interrupted")
    [ev] = host.tick()
    assert (ev.kind, ev.ok, ev.detail) == ("turn_ended", False, "timed out after 30 minutes")
    assert child.terminated == 0 and not s.dormant, "an in-band interrupt keeps the process"


def test_an_in_band_interrupt_that_is_ignored_ends_in_sigterm(henv):
    henv.config.runners["codex-cli"] = ["codex", "exec", "{prompt}"]
    host = make_host(henv, provider_for=lambda template: HandshakeProvider)
    s = host.open(SID, agent="codex", runtime="codex-cli", project=PROJECT)
    mid = host.send(SID, "do it")
    host.tick()
    [child] = henv.spawner.children
    child.emit(b"hello")
    host.tick()
    henv.clock.advance(30 * 60)
    host.tick()
    henv.clock.advance(10)
    [ev] = host.tick()
    assert (ev.kind, ev.message_id, ev.ok) == ("turn_ended", mid, False) and child.terminated == 1 and s.dormant


def test_a_handshake_has_its_own_short_timeout_and_fails_the_message_waiting_on_it(henv):
    """A process that never becomes ready is given a minute, not the turn's 30."""
    host, s = handshake_host(henv)
    mid, queued = host.send(SID, "do it"), host.send(SID, "next")
    host.tick()
    [child] = henv.spawner.children
    henv.clock.advance(59)
    assert host.tick() == [] and child.terminated == 0 and s.state == "starting"
    henv.clock.advance(1)
    [ev] = host.tick()
    assert (ev.kind, ev.message_id, ev.ok, ev.delivered) == ("turn_ended", mid, False, False)
    assert ev.detail == "the agent process was not ready after 60 seconds"
    assert child.terminated == 1 and child.stdin == b'{"method":"initialize"}\n', "its message was never written"
    # The message behind it gets a process of its own, and a fresh minute.
    assert host.tick() == [] and len(henv.spawner.calls) == 2
    assert henv.spawner.calls[1]["argv"] == ["codex", "app-server", "new", SID], "nothing was confirmed: no resume"
    henv.spawner.children[1].emit(b"hello")
    assert kinds(host.tick()) == [("delivered", queued)]


def test_the_handshake_timeout_is_the_hosts_to_set_and_never_cuts_a_turn(henv):
    host, s = handshake_host(henv, handshake_seconds=5)
    mid = host.send(SID, "do it")
    host.tick()
    henv.clock.advance(4)
    [child] = henv.spawner.children
    child.emit(b"hello")
    assert kinds(host.tick()) == [("delivered", mid)]
    henv.clock.advance(600)
    assert host.tick() == [] and child.terminated == 0, "a turn has the turn timeout"
    with pytest.raises(ValueError):
        make_host(henv, handshake_seconds=0)
    assert make_host(henv).handshake_seconds == 60
    slow = make_host(henv, provider_for=lambda template: HandshakeProvider, handshake_seconds=5)
    slow.open(SID2, agent="codex", runtime="codex-cli", project=PROJECT)
    late = slow.send(SID2, "do it")
    slow.tick()
    henv.clock.advance(5)
    [ev] = slow.tick()
    assert (ev.message_id, ev.detail) == (late, "the agent process was not ready after 5 seconds")


def test_a_ready_that_arrives_after_the_handshake_was_given_up_is_nothing_to_resume(henv):
    henv.config.runners["codex-cli"] = ["codex", "exec", "{prompt}"]
    stubborn = FakeSessionChild(2000, exits_on_terminate=False)
    host = make_host(henv, provider_for=lambda template: HandshakeProvider, spawner=lambda argv, **kw: stubborn)
    s = host.open(SID, agent="codex", runtime="codex-cli", project=PROJECT)
    mid = host.send(SID, "do it")
    host.tick()
    henv.clock.advance(60)
    assert kinds(host.tick()) == [("turn_ended", mid)]
    stubborn.emit(b"hello")
    assert host.tick() == [] and s.resume is False, "no turn ever started in that thread"
    assert not (henv.log_dir / PROVIDER_FILE).exists()


def test_a_process_that_exits_during_its_handshake_fails_the_message_that_started_it(henv):
    """Otherwise a process that can never start would be started again on every pass."""
    host, s = handshake_host(henv)
    mid, queued = host.send(SID, "do it"), host.send(SID, "next")
    host.tick()
    henv.spawner.children[0].code = 1
    [ev] = host.tick()
    assert (ev.kind, ev.message_id, ev.ok, ev.delivered) == ("turn_ended", mid, False, False)
    assert ev.detail == "the process exited (code 1) before it was ready" and not s.ended
    assert len(henv.spawner.calls) == 2, "the next message gets its own start, once"
    henv.spawner.children[1].code = 1
    assert kinds(host.tick()) == [("turn_ended", queued)]
    assert host.tick() == [] and len(henv.spawner.calls) == 2, "nothing starts without a message"


def test_a_refused_handshake_fails_the_waiting_message_with_the_reason_and_keeps_the_session(henv):
    host, s = handshake_host(henv)
    mid, queued = host.send(SID, "do it"), host.send(SID, "next")
    host.tick()
    [child] = henv.spawner.children
    child.emit(b"refused")
    [ev] = host.tick()
    assert (ev.kind, ev.message_id, ev.ok, ev.delivered) == ("turn_ended", mid, False, False)
    assert ev.detail == "the provider refused: busy" and child.terminated == 1
    assert not s.ended and s.resume is False
    assert host.tick() == [] and len(henv.spawner.calls) == 2, "the next message starts another process"
    henv.spawner.children[1].emit(b"hello")
    assert kinds(host.tick()) == [("delivered", queued)]


def test_the_providers_own_id_is_recorded_and_handed_back_after_a_restart(henv):
    host, s = handshake_host(henv)
    assert s.provider.adopted == [None], "nothing is on record for a new session"
    mid = host.send(SID, "do it")
    host.tick()
    [child] = henv.spawner.children
    child.emit(b"hello")
    host.tick()
    child.emit(b"started", b"interrupted")
    assert kinds(host.tick()) == [("turn_started", mid), ("turn_ended", mid)]
    record = henv.log_dir / PROVIDER_FILE
    assert json.loads(record.read_text()) == {SID: "thread-1"} and stat.S_IMODE(record.stat().st_mode) == 0o600
    host.stop_all()
    assert json.loads(record.read_text()) == {SID: "thread-1"}, "the record outlives the process and the host"

    later = make_host(henv, provider_for=lambda template: HandshakeProvider)
    s2 = later.open(SID, agent="codex", runtime="codex-cli", project=PROJECT, resume=True)
    assert s2.provider.adopted == ["thread-1"] and s2.provider_id == "thread-1"
    later.send(SID, "again")
    later.tick()
    assert henv.spawner.calls[-1]["argv"] == ["codex", "app-server", "resume", SID]
    # An ended session's record is dropped, and so is that of a session the hub no longer lists.
    later.open(SID2, agent="codex", runtime="codex-cli", project=PROJECT)
    later.send(SID2, "hi")
    later.tick()
    henv.spawner.children[-1].emit(b"hello")
    later.tick()
    assert json.loads(record.read_text()) == {SID: "thread-1", SID2: "thread-1"}
    later.close(SID2)
    later.tick()
    later.forget(SID2)
    assert json.loads(record.read_text()) == {SID: "thread-1"}
    record.write_text(json.dumps({SID: "thread-1", OTHER: "thread-9", "junk": "x", SID2: "not a plain id"}))
    later.keep_provider_ids({SID2})
    assert json.loads(record.read_text()) == {SID: "thread-1"}, "this host's own session stays; the rest is gone"


def test_a_claude_session_records_no_provider_id_of_its_own(henv):
    """Claude's id for a session is the hub's: there is nothing to record, and no file appears."""
    s = open_session(henv)
    henv.host.send(SID, "first")
    henv.host.tick()
    henv.spawner.children[0].emit(init(), result())
    henv.host.tick()
    assert s.provider_id == SID and not (henv.log_dir / PROVIDER_FILE).exists()


def test_a_provider_session_that_is_gone_fails_one_message_loudly_and_the_next_starts_anew(henv):
    host, s = handshake_host(henv)
    host.send(SID, "do it")
    host.tick()
    henv.spawner.children[0].emit(b"hello")
    host.tick()
    henv.spawner.children[0].emit(b"started", b"interrupted")
    host.tick()
    assert host.park(SID) is True
    host.tick()
    assert s.dormant and s.resume is True

    lost = host.send(SID, "second")
    host.tick()
    assert henv.spawner.calls[1]["argv"] == ["codex", "app-server", "resume", SID]
    henv.spawner.children[1].emit(b"gone")
    [ev] = host.tick()
    assert (ev.kind, ev.message_id, ev.ok, ev.delivered) == ("turn_ended", lost, False, False)
    assert "NEW thread" in ev.detail, "never silently"
    assert (s.resume, s.provider_id) == (False, None) and not s.ended
    assert json.loads((henv.log_dir / PROVIDER_FILE).read_text()) == {SID: None}, \
        "its id is forgotten on disk too, and a fresh start is on record in its place"
    assert host.tick() == [] and len(henv.spawner.calls) == 2, "the lost message is not written again"
    third = host.send(SID, "third")
    host.tick()
    assert henv.spawner.calls[2]["argv"] == ["codex", "app-server", "new", SID]
    henv.spawner.children[2].emit(b"hello")
    assert kinds(host.tick()) == [("delivered", third)]


def test_a_crash_right_after_the_first_turn_was_written_resumes_the_thread_and_never_starts_a_new_one(henv):
    """The provider may have recorded the turn although this node never read `turn/started` nor reported anything:
    the hub and the old state file both say there is nothing to resume. This machine's own record says otherwise,
    and it was written before the turn's first byte."""
    host, s = handshake_host(henv)
    mid = host.send(SID, "do it")
    host.tick()
    assert not (henv.log_dir / PROVIDER_FILE).exists(), "an id alone, with no turn written, is nothing to resume"
    henv.spawner.children[0].emit(b"hello")
    assert kinds(host.tick()) == [("delivered", mid)]
    assert json.loads((henv.log_dir / PROVIDER_FILE).read_text()) == {SID: "thread-1"} and s.resume is True

    later = make_host(henv, provider_for=lambda template: HandshakeProvider)    # the node process died and restarted
    s2 = later.open(SID, agent="codex", runtime="codex-cli", project=PROJECT, resume=False)
    assert s2.resume is True and s2.provider.adopted == ["thread-1"], "not a NEW conversation, silently"
    after = later.send(SID, "after")
    later.tick()
    fresh = henv.spawner.children[-1]
    assert henv.spawner.calls[-1]["argv"] == ["codex", "app-server", "resume", SID]
    fresh.emit(b"hello")
    assert kinds(later.tick()) == [("delivered", after)]
    assert b"do it" not in fresh.stdin and fresh.stdin.count(b"turn/start") == 1, "the lost turn is not replayed"


def test_a_handshake_that_cannot_be_written_fails_its_message_once_and_starts_nothing_more_for_it(henv):
    """The pipe breaks on `initialize`: the message that made the process start is still in the inbox. It must fail
    there and then, or every pass would start another process for it and report nothing."""
    henv.config.runners["codex-cli"] = ["codex", "exec", "{prompt}"]
    made = []

    def spawner(argv, **kw):
        made.append(FakeSessionChild(2000 + len(made)))
        made[-1].broken = True
        return made[-1]

    host = make_host(henv, provider_for=lambda template: HandshakeProvider, spawner=spawner)
    s = host.open(SID, agent="codex", runtime="codex-cli", project=PROJECT)
    mid, queued = host.send(SID, "do it"), host.send(SID, "next")
    [ev] = host.tick()
    assert (ev.kind, ev.message_id, ev.ok, ev.delivered) == ("turn_ended", mid, False, False)
    assert "could not write" in ev.detail and len(made) == 1 and made[0].terminated == 1
    [ev] = host.tick()
    assert (ev.message_id, ev.delivered) == (queued, False) and len(made) == 2, "the next message: its own start, once"
    for _ in range(5):
        assert host.tick() == []
    assert len(made) == 2 and not s.ended and not s.inbox


def test_a_resume_the_provider_refuses_is_on_record_before_its_failure_is_reported(henv):
    """The fresh start is decided when the refusal is read, not when the process has gone: a node that restarts
    right after reporting the failed message must not try the same resume again."""
    host, s = handshake_host(henv)
    host.send(SID, "do it")
    host.tick()
    henv.spawner.children[0].emit(b"hello")
    host.tick()
    henv.spawner.children[0].emit(b"started", b"interrupted")
    host.tick()
    host.stop_all()

    stubborn = FakeSessionChild(3000, exits_on_terminate=False)
    later = make_host(henv, provider_for=lambda template: HandshakeProvider, spawner=lambda argv, **kw: stubborn)
    s2 = later.open(SID, agent="codex", runtime="codex-cli", project=PROJECT, resume=True)
    lost = later.send(SID, "second")
    later.tick()
    stubborn.emit(b"gone")
    [ev] = later.tick()
    assert (ev.message_id, ev.ok) == (lost, False) and "NEW thread" in ev.detail
    assert s2.child is stubborn and s2.stopping_since is not None, "the process has not even gone yet"
    assert (s2.resume, s2.provider_id) == (False, None)
    assert json.loads((henv.log_dir / PROVIDER_FILE).read_text()) == {SID: None}, "a fresh start, on this machine's record"
    assert json.loads((henv.log_dir / STATE_FILE).read_text())[SID]["resume"] is False

    # A restart at this very moment. The hub never heard of the refusal and still says "resume": the local record
    # overrules it, so the next message does not walk into the same refused resume (nor into a "no record" failure).
    third = make_host(henv, provider_for=lambda template: HandshakeProvider)
    assert third.recover()[SID]["resume"] is False
    s3 = third.open(SID, agent="codex", runtime="codex-cli", project=PROJECT, resume=True)
    assert (s3.resume, s3.fresh_start, s3.provider.adopted) == (False, True, [None])
    mid = third.send(SID, "third")
    third.tick()
    assert henv.spawner.calls[-1]["argv"] == ["codex", "app-server", "new", SID]
    henv.spawner.children[-1].emit(b"hello")
    assert kinds(third.tick()) == [("delivered", mid)], "no failed message on the way to the new session"
    assert json.loads((henv.log_dir / PROVIDER_FILE).read_text()) == {SID: "thread-1"}, \
        "the new session's id takes the mark's place: from here on it is the one to resume"
    assert (s3.resume, s3.fresh_start) == (True, False)
    fourth = make_host(henv, provider_for=lambda template: HandshakeProvider)
    assert fourth.open(SID, agent="codex", runtime="codex-cli", project=PROJECT, resume=False).resume is True


# ------------------------------------------------------------ the Codex adapter behind the host (fake pipes)


def codex_host(henv, **kw):
    henv.config.runners["codex-cli"] = ["codex", "exec", "--cd", "{project}", "--sandbox", "workspace-write",
                                        "-c", cx.APPROVE, "{prompt}"]
    henv.config.sessions.codex = True
    host = make_host(henv, **kw)
    return host, host.open(SID, agent="codex", runtime="codex-cli", project=PROJECT)


def written(child) -> list[dict]:
    return [json.loads(x) for x in child.stdin.split(b"\n")[:-1]]


def codex_ready(host, child) -> None:
    """Codex answers the handshake: the session's first message is written on the pass that reads `thread/start`."""
    child.emit(cx.reply(1, cx.INIT_RESULT))
    host.tick()
    child.emit(cx.reply(2, cx.THREAD_RESULT), cx.THREAD_STARTED)


def test_codex_is_hosted_only_where_the_machine_turned_it_on(henv):
    henv.config.runners["codex-cli"] = ["codex", "exec", "--cd", "{project}", "{prompt}"]
    with pytest.raises(LookupError, match="cannot be hosted"):
        henv.host.open(SID, agent="codex", runtime="codex-cli", project=PROJECT)
    henv.config.sessions.codex = True
    s = make_host(henv).open(SID, agent="codex", runtime="codex-cli", project=PROJECT)
    assert isinstance(s.provider, cx.CodexSession)
    henv.config.runners["codex-cli"] = ["codex", "exec", "--dangerously-bypass-approvals-and-sandbox", "{prompt}"]
    with pytest.raises(ValueError, match="cannot be hosted"):
        make_host(henv).open(SID2, agent="codex", runtime="codex-cli", project=PROJECT)


def test_a_hosted_codex_session_writes_only_the_handshake_before_it_is_ready(henv):
    host, s = codex_host(henv)
    mid = host.send(SID, "the message")
    assert host.tick() == []
    [call], [child] = henv.spawner.calls, henv.spawner.children
    assert call["argv"] == ["codex", "app-server", "-c", cx.APPROVE, "--listen", "stdio://"]
    assert call["cwd"] == henv.work and "the message" not in call["argv"] and henv.work not in call["argv"]
    assert [m["method"] for m in written(child)] == ["initialize"] and s.state == "starting"
    henv.clock.advance(5)
    assert host.tick() == [] and len(written(child)) == 1, "nothing but the handshake until Codex answers it"

    child.emit(cx.reply(1, cx.INIT_RESULT))
    assert host.tick() == []
    assert [m["method"] for m in written(child)] == ["initialize", "initialized", "thread/start"]
    assert written(child)[2]["params"] == {"sandbox": "workspace-write", "approvalPolicy": "never"}, \
        "no cwd (the process was started in the run directory) and no instructions of ours"
    assert "the message" not in child.stdin.decode(), "still no turn"

    child.emit(cx.reply(2, cx.THREAD_RESULT), cx.THREAD_STARTED)
    assert kinds(host.tick()) == [("delivered", mid)]
    assert written(child)[3] == {"id": 3, "method": "turn/start", "params": {
        "threadId": cx.THREAD, "input": [{"type": "text", "text": "the message"}]}}
    assert (s.state, s.provider_id, s.resume) == ("running", cx.THREAD, True)
    assert json.loads((henv.log_dir / PROVIDER_FILE).read_text()) == {SID: cx.THREAD}

    queued = host.send(SID, "second")
    child.emit(cx.reply(3, cx.TURN_REPLY), cx.TURN_STARTED, cx.message("a note", phase="commentary"))
    assert kinds(host.tick()) == [("turn_started", mid)]
    assert len(written(child)) == 4, "the second message waits for the turn to end"
    child.emit(cx.message("the answer"), cx.completed())
    events = host.tick()
    assert kinds(events) == [("turn_ended", mid), ("delivered", queued)]
    assert (events[0].ok, events[0].text, events[0].meta) == (True, "the answer",
                                                              {"status": "completed", "duration_ms": 1227})
    assert written(child)[4]["method"] == "turn/start" and len(henv.spawner.calls) == 1, "one process, both turns"
    transcript = host.transcript_path(SID).read_text().splitlines()
    assert len(transcript) == 8 and json.loads(transcript[-1])["method"] == "turn/completed"


def test_a_hosted_codex_turn_past_the_timeout_is_interrupted_in_band_and_the_process_stays(henv):
    host, s = codex_host(henv)
    mid = host.send(SID, "long")
    host.tick()
    [child] = henv.spawner.children
    codex_ready(host, child)
    host.tick()
    child.emit(cx.reply(3, cx.TURN_REPLY), cx.TURN_STARTED)
    host.tick()
    henv.clock.advance(30 * 60)
    assert host.tick() == [] and child.terminated == 0
    assert written(child)[-1] == {"id": 4, "method": "turn/interrupt",
                                  "params": {"threadId": cx.THREAD, "turnId": cx.TURN}}
    child.emit(cx.reply(4, {}), cx.completed("interrupted"))
    [ev] = host.tick()
    assert (ev.kind, ev.message_id, ev.ok, ev.detail) == ("turn_ended", mid, False, "timed out after 30 minutes")
    assert child.terminated == 0 and s.state == "idle" and not s.dormant
    after = host.send(SID, "after")
    assert kinds(host.tick()) == [("delivered", after)] and len(henv.spawner.calls) == 1


def test_a_codex_approval_request_is_declined_and_the_turn_ends_waiting_with_the_kind_only(henv):
    host, s = codex_host(henv)
    mid = host.send(SID, "do something")
    host.tick()
    [child] = henv.spawner.children
    codex_ready(host, child)
    host.tick()
    child.emit(cx.reply(3, cx.TURN_REPLY), cx.TURN_STARTED, cx.APPROVAL)
    assert kinds(host.tick()) == [("turn_started", mid)]
    assert written(child)[-1] == {"id": 0, "result": {"decision": "decline"}}
    child.emit(cx.DECLINED, cx.message("it was refused"), cx.completed())
    [ev] = host.tick()
    assert (ev.kind, ev.ok, ev.denials, ev.text) == ("turn_ended", True, ("commandExecution",), "it was refused")
    assert s.state == "waiting" and s.denials == ("commandExecution",)
    assert "ssh" not in repr(ev) and "secret" not in repr(ev)


def test_a_restarted_host_resumes_the_codex_thread_it_recorded(henv):
    host, s = codex_host(henv)
    host.send(SID, "first")
    host.tick()
    [child] = henv.spawner.children
    codex_ready(host, child)
    host.tick()
    child.emit(cx.reply(3, cx.TURN_REPLY), cx.TURN_STARTED, cx.message("one"), cx.completed())
    host.tick()
    host.stop_all()

    later = make_host(henv)
    s2 = later.open(SID, agent="codex", runtime="codex-cli", project=PROJECT, resume=True)
    assert s2.provider_id == cx.THREAD, "from this machine's own record"
    mid = later.send(SID, "second")
    later.tick()
    fresh = henv.spawner.children[-1]
    fresh.emit(cx.reply(1, cx.INIT_RESULT))
    later.tick()
    resume = written(fresh)[2]
    assert resume["method"] == "thread/resume" and resume["params"]["threadId"] == cx.THREAD
    assert resume["params"]["excludeTurns"] is True and resume["params"]["approvalPolicy"] == "never"
    assert resume["params"]["cwd"] == henv.work, "where this node runs the project NOW, not where the thread began"
    fresh.emit(cx.reply(2, cx.THREAD_RESULT))
    assert kinds(later.tick()) == [("delivered", mid)]
    assert written(fresh)[3]["params"]["threadId"] == cx.THREAD


def test_a_codex_thread_that_is_gone_fails_one_message_loudly_then_a_new_thread_starts(henv):
    henv.log_dir.mkdir(parents=True)
    (henv.log_dir / PROVIDER_FILE).write_text(json.dumps({SID: cx.THREAD}))
    henv.config.runners["codex-cli"] = ["codex", "exec", "--cd", "{project}", "{prompt}"]
    henv.config.sessions.codex = True
    host = make_host(henv)
    s = host.open(SID, agent="codex", runtime="codex-cli", project=PROJECT, resume=True)
    lost = host.send(SID, "second")
    host.tick()
    [child] = henv.spawner.children
    child.emit(cx.reply(1, cx.INIT_RESULT))
    host.tick()
    child.emit(cx.reply(2, error=cx.NO_ROLLOUT))
    [ev] = host.tick()
    assert (ev.kind, ev.message_id, ev.ok, ev.delivered) == ("turn_ended", lost, False, False)
    assert cx.THREAD in ev.detail and "NEW thread" in ev.detail and child.terminated == 1
    assert (s.resume, s.provider_id) == (False, None)
    assert json.loads((henv.log_dir / PROVIDER_FILE).read_text()) == {SID: None}

    third = host.send(SID, "third")
    host.tick()
    fresh = henv.spawner.children[-1]
    fresh.emit(cx.reply(1, cx.INIT_RESULT))
    host.tick()
    assert written(fresh)[2]["method"] == "thread/start", "a new thread, said out loud one message earlier"
    new = {**cx.THREAD_RESULT, "thread": {**cx.THREAD_RESULT["thread"], "id": cx.CHILD}}
    fresh.emit(cx.reply(2, new))
    assert kinds(host.tick()) == [("delivered", third)] and s.provider_id == cx.CHILD
    assert json.loads((henv.log_dir / PROVIDER_FILE).read_text()) == {SID: cx.CHILD}


def test_a_codex_runner_whose_cd_is_another_directory_cannot_be_hosted(henv, tmp_path):
    """The thread works where its process is started (the node's run directory): no `cwd` is sent that could name
    another place, so a runner that asks for one is refused instead of quietly working somewhere else."""
    henv.config.sessions.codex = True
    henv.config.runners["codex-cli"] = ["codex", "exec", "--cd", str(tmp_path), "{prompt}"]
    with pytest.raises(ValueError, match="--cd must be"):
        make_host(henv).open(SID, agent="codex", runtime="codex-cli", project=PROJECT)
    henv.config.runners["codex-cli"] = ["codex", "exec", "--cd", henv.work, "{prompt}"]
    host = make_host(henv)
    host.open(SID, agent="codex", runtime="codex-cli", project=PROJECT)       # the run directory itself, spelled out
    host.send(SID, "hello")
    host.tick()
    assert henv.spawner.calls[-1]["cwd"] == henv.work and henv.work not in henv.spawner.calls[-1]["argv"]


def codex_idle_after_one_turn(henv):
    host, s = codex_host(henv)
    host.send(SID, "first")
    host.tick()
    [child] = henv.spawner.children
    codex_ready(host, child)
    host.tick()
    child.emit(cx.reply(3, cx.TURN_REPLY), cx.TURN_STARTED, cx.message("one"), cx.completed())
    host.tick()
    assert s.state == "idle"
    return host, s, child


def test_a_message_waits_while_codex_runs_a_turn_of_its_own_in_the_thread(henv):
    """Written now, the message would be added to Codex's own turn and get THAT turn's result."""
    host, s, child = codex_idle_after_one_turn(henv)
    child.emit(cx.own_turn("turn/started"))
    held = host.send(SID, "second")
    before = len(written(child))
    for _ in range(3):
        henv.clock.advance(5)
        assert host.tick() == []
    assert len(written(child)) == before and len(s.inbox) == 1 and s.state == "idle", "not written, not delivered"
    child.emit(cx.message("Codex's own business", turn=cx.CHILD_TURN), cx.own_turn("turn/completed", status="completed"))
    assert kinds(host.tick()) == [("delivered", held)]
    assert written(child)[-1]["params"]["input"] == [{"type": "text", "text": "second"}]
    child.emit(cx.reply(4, cx.TURN_REPLY), cx.TURN_STARTED, cx.message("two"), cx.completed())
    events = host.tick()
    assert kinds(events) == [("turn_started", held), ("turn_ended", held)] and events[1].text == "two"


def test_a_message_held_for_a_whole_turn_timeout_fails_and_the_process_is_stopped(henv):
    host, s, child = codex_idle_after_one_turn(henv)
    child.emit(cx.own_turn("turn/started"))
    held = host.send(SID, "second")
    assert host.tick() == []
    henv.clock.advance(30 * 60 - 1)
    assert host.tick() == [] and child.terminated == 0
    henv.clock.advance(1)
    [ev] = host.tick()
    assert (ev.kind, ev.message_id, ev.ok, ev.delivered) == ("turn_ended", held, False, False)
    assert "a turn of its own for 30 minutes" in ev.detail and child.terminated == 1 and not s.ended
    after = host.send(SID, "third")
    host.tick()
    fresh = henv.spawner.children[-1]
    assert fresh is not child and written(fresh)[0]["method"] == "initialize"
    fresh.emit(cx.reply(1, cx.INIT_RESULT))
    host.tick()
    assert written(fresh)[2]["method"] == "thread/resume", "the thread, with whatever that turn did, is resumed"
    fresh.emit(cx.reply(2, cx.THREAD_RESULT))
    assert kinds(host.tick()) == [("delivered", after)]


def test_a_codex_process_that_exits_before_it_is_ready_fails_the_message_and_the_session_stays(henv):
    """`codex app-server --strict-config` with an unknown key in the user's config exits 1 before any line."""
    host, s = codex_host(henv)
    mid = host.send(SID, "hello")
    host.tick()
    henv.spawner.children[0].code = 1
    [ev] = host.tick()
    assert (ev.kind, ev.message_id, ev.ok) == ("turn_ended", mid, False) and "exited (code 1)" in ev.detail
    assert not s.ended and s.dormant


# ------------------------------------------------------------ real pipes (a stand-in script, never a real agent)

STAND_IN = r'''
import json, os, sys, time
args = sys.argv[1:]
sid = args[args.index("--resume" if "--resume" in args else "--session-id") + 1]
with open(os.environ["TMPDIR"] + "/argv.jsonl", "a") as f:
    f.write(json.dumps(args) + "\n")
sys.stderr.write("stand-in started\n")
for line in sys.stdin:
    text = json.loads(line)["message"]["content"]
    print(json.dumps({"type": "system", "subtype": "init", "session_id": sid}), flush=True)
    if text == "hang":
        time.sleep(600)
    print(json.dumps({"type": "assistant", "session_id": sid, "pad": "x" * 200000}), flush=True)
    print(json.dumps({"type": "result", "subtype": "success", "is_error": False, "session_id": sid,
                      "result": text.upper(), "permission_denials": []}), flush=True)
'''


def assert_group_gone(pgid, seconds=5.0):
    """No process is left in the group. Just after its last member is killed, macOS answers EPERM for an instant
    (only an exited, not yet reaped process is left) before ESRCH."""
    deadline = time.monotonic() + seconds
    while True:
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return
        except PermissionError:
            pass
        assert time.monotonic() < deadline, f"process group {pgid} still has a member"
        time.sleep(0.005)


def until(host, want, seconds=20.0):
    """Tick until an event of this kind arrives (real processes take real time). Returns every event seen."""
    seen, deadline = [], time.monotonic() + seconds
    while time.monotonic() < deadline:
        seen += host.tick()
        if any(e.kind == want for e in seen):
            return seen
        time.sleep(0.02)
    raise AssertionError(f"no {want} event within {seconds}s; saw {kinds(seen)}")


def real_host(tmp_path, monkeypatch, script, timeout_seconds, grace):
    """A host with real processes: the runner is a stand-in script named `claude` (TMPDIR is where it leaves notes)."""
    work = tmp_path / "checkout"
    work.mkdir()
    exe = tmp_path / "bin" / "claude"
    exe.parent.mkdir()
    exe.write_text(f"#!{sys.executable}\n{script}")
    exe.chmod(0o700)
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    config = DispatchConfig.from_dict({"runners": {"claude-code": [str(exe)] + SHIPPED[1:]},
                                       "worktrees": {PROJECT: str(work)}, "kill_grace_seconds": grace})
    host = SessionHost(config, tmp_path / "node-logs", timeout_minutes=timeout_seconds / 60, spawner=spawn_piped)
    return host, host.open(SID, agent="claude", runtime="claude-code", project=PROJECT)


# Its first process starts a tool process in the same group that ignores SIGTERM (set before exec, so there is no
# race), then hangs. SIGTERM ends the leader only.
STRAY_STAND_IN = r'''
import json, os, signal, subprocess, sys, time
args = sys.argv[1:]
resumed = "--resume" in args
sid = args[args.index("--resume" if resumed else "--session-id") + 1]
for line in sys.stdin:
    print(json.dumps({"type": "system", "subtype": "init", "session_id": sid}), flush=True)
    if not resumed:
        tool = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"],
                                preexec_fn=lambda: signal.signal(signal.SIGTERM, signal.SIG_IGN))
        with open(os.environ["TMPDIR"] + "/stray.pid", "w") as f:
            f.write(str(tool.pid))
        time.sleep(600)
    print(json.dumps({"type": "result", "subtype": "success", "is_error": False, "session_id": sid,
                      "permission_denials": []}), flush=True)
'''


def test_real_group_member_that_ignores_sigterm_is_killed_before_the_session_starts_again(tmp_path, monkeypatch):
    host, s = real_host(tmp_path, monkeypatch, STRAY_STAND_IN, timeout_seconds=2, grace=1)
    try:
        first, second = host.send(SID, "first"), host.send(SID, "second")
        seen = until(host, "turn_ended")
        assert (seen[-1].message_id, seen[-1].ok) == (first, False)
        stopped_at, leader = time.monotonic(), s.child.pid
        stray = int((tmp_path / "stray.pid").read_text())
        assert os.getpgid(stray) == leader, "the tool process is in the session's process group"
        deadline = time.monotonic() + 5
        while s.child.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        assert s.child.poll() is not None, "SIGTERM ended the leader"
        os.kill(stray, 0)       # the tool process ignored it
        assert host.tick() == [] and not s.dormant, "nothing starts while a member of the old group runs"

        seen = until(host, "delivered")
        assert kinds(seen)[-1] == ("delivered", second)
        assert time.monotonic() - stopped_at >= 0.9, "it waited out the grace period"
        assert not session_host.group_alive(leader), "the whole old group was gone before the next process started"
        assert_group_gone(leader, seconds=1.0)
        seen = until(host, "turn_ended")
        assert (seen[-1].message_id, seen[-1].ok) == (second, True) and s.child.pid != leader
    finally:
        host.stop_all()


def test_real_pipes_carry_turns_a_timeout_terminates_the_group_and_the_next_message_resumes(tmp_path, monkeypatch):
    host, s = real_host(tmp_path, monkeypatch, STAND_IN, timeout_seconds=3, grace=5)
    try:
        first, second = host.send(SID, "one"), host.send(SID, "two")
        seen = until(host, "turn_ended")
        seen += until(host, "turn_ended") if kinds(seen).count(("turn_ended", second)) == 0 else []
        assert [k for k in kinds(seen) if k[0] != "turn_started"] == [
            ("delivered", first), ("turn_ended", first), ("delivered", second), ("turn_ended", second)]
        assert all(e.ok for e in seen if e.kind == "turn_ended") and s.state == "idle"
        pid = s.child.pid
        assert os.getpgid(pid) == pid, "it leads its own process group"
        record = json.loads((tmp_path / "node-logs" / STATE_FILE).read_text())[SID]
        assert record["pid"] == pid and record["proc_start"], "ps reported its start time"

        hang = host.send(SID, "hang")
        seen = until(host, "turn_ended")
        failed = seen[-1]
        assert (failed.message_id, failed.ok, failed.delivered) == (hang, False, True) and "timed out" in failed.detail
        deadline = time.monotonic() + 10
        while not s.dormant and time.monotonic() < deadline:
            host.tick()
            time.sleep(0.02)
        assert s.dormant, "SIGTERM ended the process"
        with pytest.raises(ProcessLookupError):
            os.killpg(pid, 0)

        after = host.send(SID, "three")
        seen = until(host, "turn_ended")
        assert (seen[-1].message_id, seen[-1].ok) == (after, True)
        runs = [json.loads(x) for x in (tmp_path / "argv.jsonl").read_text().splitlines()]
        assert len(runs) == 2 and runs[0][-2:] == ["--session-id", SID] and runs[1][-2:] == ["--resume", SID]
        transcript = host.transcript_path(SID).read_text().splitlines()
        assert [json.loads(x)["type"] for x in transcript] == [
            "system", "assistant", "result", "system", "assistant", "result", "system", "system", "assistant",
            "result"]
        assert [e["result"] for e in map(json.loads, transcript) if e["type"] == "result"] == ["ONE", "TWO", "THREE"]
        err = tmp_path / "node-logs" / "sessions" / f"{SID}.stderr.log"
        assert err.read_text() == "stand-in started\n" * 2 and stat.S_IMODE(err.stat().st_mode) == 0o600
    finally:
        host.stop_all()
    assert s.child is None and json.loads((tmp_path / "node-logs" / STATE_FILE).read_text()) == {}


# ------------------------------------------------------------ real pipes, the Codex stand-in (stand_in_codex.py)


def install_codex_stand_in(directory: Path) -> Path:
    """tests/stand_in_codex.py as an executable named `codex`; it keeps its threads beside itself."""
    directory.mkdir(parents=True, exist_ok=True)
    exe = directory / "codex"
    exe.write_text(f"#!{sys.executable}\n" + (ROOT / "tests" / "stand_in_codex.py").read_text())
    exe.chmod(0o700)
    return exe


def real_codex_host(tmp_path, timeout_seconds=30.0, grace=5):
    work = tmp_path / "checkout"
    work.mkdir(exist_ok=True)
    exe = install_codex_stand_in(tmp_path / "bin")
    config = DispatchConfig.from_dict({
        "runners": {"codex-cli": [str(exe), "exec", "--cd", "{project}", "--sandbox", "read-only", "-c", cx.APPROVE,
                                  "{prompt}"]},
        "worktrees": {PROJECT: str(work)}, "kill_grace_seconds": grace, "sessions": {"codex": True}})
    return SessionHost(config, tmp_path / "node-logs", timeout_minutes=timeout_seconds / 60, spawner=spawn_piped)


def turn_end(host, message_id, seconds=20.0):
    """Tick until this message's turn is over (real processes take real time)."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        for e in host.tick():
            if e.kind == "turn_ended" and e.message_id == message_id:
                return e
        time.sleep(0.02)
    raise AssertionError(f"the turn of {message_id} did not end within {seconds}s")


def test_real_pipes_each_way_a_claude_turn_ends_says_whether_the_provider_took_it(tmp_path, monkeypatch):
    """Round 3 of result delivery (S10): `answered` is the adapter's word, per path (providers.Event). Claude: a
    `result` line for the turn, a success or an error, is the provider's own end; a process that exits before any of
    the turn's lines, and a timeout the host ends by stopping the process, are not."""
    host, s = real_host(tmp_path, monkeypatch, (ROOT / "tests" / "stand_in_claude.py").read_text(),
                        timeout_seconds=3, grace=2)
    try:
        seen = []
        for text in ("hello", "fail on purpose", "crash now", "sleep 30 then answer"):
            ev = turn_end(host, host.send(SID, text), seconds=30)
            seen.append((text, ev.ok, ev.answered))
            while s.child is not None and s.stopping_since is not None:     # a stopped process goes first
                host.tick()
                time.sleep(0.02)
        assert seen == [("hello", True, True), ("fail on purpose", False, True), ("crash now", False, False),
                        ("sleep 30 then answer", False, False)]
    finally:
        host.stop_all()


def test_real_pipes_each_way_a_codex_turn_ends_says_whether_the_provider_took_it(tmp_path):
    """Codex: `turn/completed` for a turn that was accepted (its `turn/start` answered with the turn, or
    `turn/started` seen) is the provider's own end, completed, failed or interrupted after a timeout; an error answer
    to `turn/start`, a process that exits before anything of the turn, and a handshake that fails are not."""
    store = tmp_path / "bin" / "stand-in-codex"
    host = real_codex_host(tmp_path, timeout_seconds=3)
    s = host.open(SID, agent="codex", runtime="codex-cli", project=PROJECT)
    seen = []
    try:
        for text in ("hello", "fail on purpose", "reject this", "sleep 60 while the host waits", "crash now"):
            ev = turn_end(host, host.send(SID, text), seconds=30)
            seen.append((text, ev.ok, ev.answered))
        thread = s.provider_id
    finally:
        host.stop_all()
    assert seen == [("hello", True, True), ("fail on purpose", False, True), ("reject this", False, False),
                    ("sleep 60 while the host waits", False, True), ("crash now", False, False)]
    (store / "threads" / f"{thread}.json").unlink()    # the handshake fails: Codex cannot resume the thread
    later = real_codex_host(tmp_path)
    later.open(SID, agent="codex", runtime="codex-cli", project=PROJECT, resume=True)
    try:
        ev = turn_end(later, later.send(SID, "anyone there"))
        assert (ev.ok, ev.delivered, ev.answered) == (False, False, False)
    finally:
        later.stop_all()


def test_real_pipes_a_codex_session_takes_turns_declines_is_interrupted_in_band_and_resumes(tmp_path):
    store = tmp_path / "bin" / "stand-in-codex"
    host = real_codex_host(tmp_path, timeout_seconds=2)
    s = host.open(SID, agent="codex", runtime="codex-cli", project=PROJECT, model="gpt-6-luna")
    try:
        first, second = host.send(SID, "remember lantern"), host.send(SID, "deny this")
        ev = turn_end(host, first)
        assert (ev.ok, ev.text, ev.denials) == (True, "turn 1: REMEMBER LANTERN", ()), "not the sub-agent's answer"
        ev = turn_end(host, second)
        assert (ev.ok, ev.text, ev.denials) == (True, "turn 2: DENY THIS", ("commandExecution",))
        assert s.state == "waiting" and "secret" not in repr(ev)
        pid, thread = s.child.pid, s.provider_id
        assert json.loads((store / "argv.jsonl").read_text()) == ["app-server", "-c", cx.APPROVE, "--listen", "stdio://"]
        [started] = [json.loads(x) for x in (store / "requests.jsonl").read_text().splitlines()]
        assert started == {"method": "thread/start", "params": {
            "sandbox": "read-only", "approvalPolicy": "never", "model": "gpt-6-luna"}}
        ready = next(m for m in map(json.loads, host.transcript_path(SID).read_text().splitlines())
                     if isinstance(m.get("result"), dict) and "thread" in m["result"])
        assert os.path.realpath(ready["result"]["cwd"]) == os.path.realpath(tmp_path / "checkout"), \
            "the thread works where its process was started"
        transcript = [json.loads(x) for x in host.transcript_path(SID).read_text().splitlines()]
        declined = [m["params"]["item"]["status"] for m in transcript if m.get("method") == "item/completed"
                    and m["params"]["item"].get("type") == "commandExecution"]
        assert declined == ["declined"], "the stand-in got the refusal and ran nothing"

        hang = host.send(SID, "sleep 60 while the host waits")
        ev = turn_end(host, hang)
        assert (ev.ok, ev.delivered) == (False, True) and "timed out" in ev.detail
        assert s.child is not None and s.child.pid == pid and s.child.poll() is None, "interrupted, not stopped"
        after = host.send(SID, "after")
        assert turn_end(host, after).text == "turn 4: AFTER"
        assert s.child.pid == pid, "one process took all four turns"
    finally:
        host.stop_all()
    with pytest.raises(ProcessLookupError):
        os.killpg(pid, 0)
    record = tmp_path / "node-logs" / PROVIDER_FILE
    assert json.loads(record.read_text()) == {SID: thread} and stat.S_IMODE(record.stat().st_mode) == 0o600

    # A new host (the node restarted): the thread is resumed from this machine's record, with its turns.
    later = real_codex_host(tmp_path)
    s2 = later.open(SID, agent="codex", runtime="codex-cli", project=PROJECT, resume=True)
    try:
        recall = later.send(SID, "recall")
        assert turn_end(later, recall).text == "turn 5: FIRST WAS REMEMBER LANTERN"
        assert s2.child.pid != pid and s2.provider_id == thread
        resumed = json.loads((store / "requests.jsonl").read_text().splitlines()[-1])
        assert resumed["method"] == "thread/resume" and resumed["params"]["threadId"] == thread
        assert resumed["params"]["cwd"] == str(tmp_path / "checkout")
    finally:
        later.stop_all()

    # Codex lost the thread (its rollout is gone): one message fails and says so; the next starts a new thread.
    (store / "threads" / f"{thread}.json").unlink()
    third = real_codex_host(tmp_path)
    s3 = third.open(SID, agent="codex", runtime="codex-cli", project=PROJECT, resume=True)
    try:
        ev = turn_end(third, third.send(SID, "anyone there"))
        assert (ev.ok, ev.delivered) == (False, False) and thread in ev.detail and "NEW thread" in ev.detail
        ev = turn_end(third, third.send(SID, "start over"))
        assert (ev.ok, ev.text) == (True, "turn 1: START OVER")
        assert s3.provider_id not in (None, thread) and json.loads(record.read_text()) == {SID: s3.provider_id}
    finally:
        third.stop_all()
