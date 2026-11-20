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

from agent_comms import runner_preflight as rp
from agent_comms.dispatch import DispatchConfig, spawn_piped
from agent_comms.providers import Event
from agent_comms.session_host import STATE_FILE, SessionHost

from conftest import PROJECT, FakeClock
from test_claude_transcripts import tool_result, tool_use
from test_dispatch import FakeChild, FakeProcs, FakeSpawner

ROOT = Path(__file__).resolve().parents[1]
SID = "5b0c1f2e-7a6d-4e3b-9c8a-1d2e3f4a5b6c"
SID2 = "2c33cbfc-3334-4290-b654-b73ccf444afa"
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
                       sleep=lambda s: henv.clock.advance(s), **kw)


@pytest.fixture
def henv(tmp_path):
    work = tmp_path / "checkout"
    work.mkdir()
    henv = SimpleNamespace(clock=FakeClock(), work=str(work), log_dir=tmp_path / "node-logs", signals=[])
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
                       sleep=lambda s: order.append(("sleep", s)))
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
    assert not any("{" in a for a in argv), "no placeholder is left"
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
    """claude_tool_projects is the human's opt-in to the wider scoped tools; a session never widens a runner itself."""
    open_session(henv)
    henv.host.send(SID, "hello")
    henv.host.tick()
    argv = henv.spawner.calls[0]["argv"]
    assert argv == ["claude", "-p", "--permission-mode", "dontAsk", "--allowedTools=mcp__agent-comms",
                    "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
                    "--permission-prompts", "none", "--session-id", SID]


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
    """A provider shaped like a request/response protocol: a handshake before any turn, its own interrupt."""
    waits_for_ready = True

    def __init__(self):
        self.fed = []

    def argv(self, template, *, session_id, resume, model):
        return [template[0], "app-server", "resume" if resume else "new", session_id]

    def on_open(self):
        return [b'{"method":"initialize"}\n']

    def turn(self, text):
        return [json.dumps({"method": "turn/start", "text": text}).encode() + b"\n"]

    def feed(self, line):
        self.fed.append(line)
        if line == b"hello":
            return [Event("ready", provider_id="thread-1")], [b'{"method":"initialized"}\n']
        if line == b"asks":
            return [Event("approval_requested", denials=("shell",))], []
        if line == b"interrupted":
            return [Event("turn_ended", ok=True)], []
        return [], []

    def interrupt(self):
        return [b'{"method":"turn/interrupt"}\n']


def test_a_provider_with_a_handshake_gets_no_turn_before_it_is_ready_and_interrupts_in_band(henv):
    henv.config.runners["codex-cli"] = ["codex", "exec", "{prompt}"]
    host = make_host(henv, provider_for=lambda template: HandshakeProvider)
    s = host.open(SID, agent="codex", runtime="codex-cli", project=PROJECT)
    mid = host.send(SID, "do it")
    assert host.tick() == []
    [child] = henv.spawner.children
    assert henv.spawner.calls[0]["argv"] == ["codex", "app-server", "new", SID]
    assert child.stdin == b'{"method":"initialize"}\n' and s.state == "starting"
    assert host.tick() == [] and child.stdin.count(b"\n") == 1, "the turn waits for the handshake"

    child.emit(b"hello")
    assert kinds(host.tick()) == [("delivered", mid)]
    assert child.stdin.splitlines()[1:] == [b'{"method":"initialized"}', b'{"method": "turn/start", "text": "do it"}']
    assert (s.state, s.provider_id, s.resume) == ("running", "thread-1", True)

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


def test_a_handshake_that_never_finishes_fails_the_message_waiting_on_it(henv):
    henv.config.runners["codex-cli"] = ["codex", "exec", "{prompt}"]
    host = make_host(henv, provider_for=lambda template: HandshakeProvider)
    host.open(SID, agent="codex", runtime="codex-cli", project=PROJECT)
    mid = host.send(SID, "do it")
    host.tick()
    henv.clock.advance(30 * 60)
    [ev] = host.tick()
    assert (ev.kind, ev.message_id, ev.ok, ev.delivered) == ("turn_ended", mid, False, False)
    assert henv.spawner.children[0].terminated == 1 and len(henv.spawner.calls) == 1


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


def until(host, want, seconds=20.0):
    """Tick until an event of this kind arrives (real processes take real time). Returns every event seen."""
    seen, deadline = [], time.monotonic() + seconds
    while time.monotonic() < deadline:
        seen += host.tick()
        if any(e.kind == want for e in seen):
            return seen
        time.sleep(0.02)
    raise AssertionError(f"no {want} event within {seconds}s; saw {kinds(seen)}")


def test_real_pipes_carry_turns_a_timeout_terminates_the_group_and_the_next_message_resumes(tmp_path, monkeypatch):
    work = tmp_path / "checkout"
    work.mkdir()
    exe = tmp_path / "bin" / "claude"
    exe.parent.mkdir()
    exe.write_text(f"#!{sys.executable}\n{STAND_IN}")
    exe.chmod(0o700)
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    config = DispatchConfig.from_dict({"runners": {"claude-code": [str(exe)] + SHIPPED[1:]},
                                       "worktrees": {PROJECT: str(work)}, "kill_grace_seconds": 5})
    host = SessionHost(config, tmp_path / "node-logs", timeout_minutes=3 / 60, spawner=spawn_piped)
    s = host.open(SID, agent="claude", runtime="claude-code", project=PROJECT)
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
