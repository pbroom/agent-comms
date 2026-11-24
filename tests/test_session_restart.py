"""Restarting a running turn (DESIGN_NOTES "Restarting a running turn"), the agent side: the adapters from strings
(what Claude Code 2.1.330 and codex-cli 0.171.0 were measured to write), the session host with fake processes and the
state machine's table (`CELLS`), and both stand-ins (tests/stand_in_*.py) through real pipes. No adapter steers
(R6). The hub, the node and the spawn tools: test_session_restart_hub. Never a real agent CLI."""

import json
import time
from pathlib import Path

import pytest

from agent_comms import providers
from agent_comms.providers.claude import ClaudeSession
from agent_comms.session_host import INTERRUPTED

import test_providers_codex as cx
from test_providers_claude import main_bytes
from conftest import PROJECT
from test_session_host import (SID, codex_host, codex_ready, henv, init, open_session, real_codex_host,  # noqa: F401
                               real_host, result)

ROOT = Path(__file__).resolve().parents[1]
CAPS = ["msg_lifecycle_v1", "interrupt_receipt_v1"]     # what Claude Code 2.1.330's `init` lists, among others


# ------------------------------------------------------------ helpers


def cinit(sid=SID, caps=CAPS) -> dict:
    """An `init` as Claude Code 2.1.330 writes it, with its capabilities (test_session_host's has none)."""
    return init(sid) | {"capabilities": list(caps), "claude_code_version": "2.1.330"}


def aborted(sid=SID) -> dict:
    """The result of a turn the interrupt ended (measured: `error_during_execution`, `aborted_tools`)."""
    return result(sid, subtype="error_during_execution", is_error=True, text=None) | {
        "terminal_reason": "aborted_tools"}


def receipt(request_id, ok=True) -> dict:
    """Claude Code's answer to a control request (measured for `interrupt`: success, with `still_queued`)."""
    return {"type": "control_response", "response": (
        {"subtype": "success", "request_id": request_id, "response": {"still_queued": []}} if ok
        else {"subtype": "error", "request_id": request_id, "error": "no turn to interrupt"})}


def lines(data: bytes) -> list[dict]:
    return [json.loads(x) for x in data.split(b"\n") if x]


def kinds(events):
    return [(e.kind, e.message_id) for e in events]


def claude_in_turn(caps=CAPS) -> ClaudeSession:
    p = ClaudeSession()
    p.argv(["claude", "-p", "{prompt}"], session_id=SID, resume=False, model=None)
    p.turn("first")
    p.feed(json.dumps(cinit(caps=caps)).encode())
    return p


def claude_turn(henv, text="first", caps=CAPS):
    """A Claude session whose first turn is in flight (written, begun)."""
    s = open_session(henv)
    henv.host.send(SID, text, message_id="1")
    henv.host.tick()
    child = henv.spawner.children[-1]
    child.emit(cinit(caps=caps))
    assert [e.kind for e in henv.host.tick()] == ["turn_started"] and s.in_flight.id == "1"
    return s, child


def codex_turn(henv):
    """A Codex session whose first turn (message 1) is in flight, named by Codex (turn/start was request 3)."""
    host, s = codex_host(henv)
    henv.host = host
    host.send(SID, "first", message_id="1")
    host.tick()
    child = henv.spawner.children[-1]
    codex_ready(host, child)
    host.tick()
    child.emit(cx.reply(3, cx.TURN_REPLY), cx.TURN_STARTED)
    host.tick()
    assert s.in_flight.id == "1" and s.begun
    return s, child


def sent(child) -> list[dict]:
    return lines(child.stdin)


def methods(child) -> list[str]:
    return [m.get("method") for m in sent(child)]


def users(child) -> list[str]:
    return [x["message"]["content"] for x in lines(child.stdin) if x.get("type") == "user"]


def interrupts(child) -> list[dict]:
    return [x for x in lines(child.stdin) if x.get("type") == "control_request"
            and x.get("request", {}).get("subtype") == "interrupt"]


def answered(child, ok=True) -> None:
    """Claude Code answers the last interrupt written to it, then the turn's result (an abort)."""
    child.emit(receipt(interrupts(child)[-1]["request_id"], ok), aborted())


# ------------------------------------------------------------ the adapters


def test_claude_never_steers_and_restarts_once_its_process_said_it_acknowledges_the_interrupt():
    p = ClaudeSession()
    p.argv(["claude", "-p", "{prompt}"], session_id=SID, resume=False, model=None)
    assert providers.modes(p) == () and p.steers is False and not hasattr(p, "steer"), "R6: no mid-turn steering"
    # Byte for byte what a turn has always been, and the uuid key turn correlation adds (its one named change).
    [data] = p.turn("hello\nthere")
    assert main_bytes(data) == b'{"type": "user", "message": {"role": "user", "content": "hello\\nthere"}}\n'
    p.feed(json.dumps(init()).encode())                      # a CLI that lists no capabilities
    assert providers.modes(p) == () and p.interrupt_turn() is None
    assert providers.modes(claude_in_turn()) == ("restart",)
    p = claude_in_turn()
    p.argv(["claude", "-p", "{prompt}"], session_id=SID, resume=True, model=None)
    assert providers.modes(p) == (), "a new process says it again"


def test_claude_counts_an_abort_as_the_interrupts_only_after_a_success_receipt_for_that_request():
    """R3-03: the receipt is matched to the interrupt's own request id; an error receipt interrupted nothing."""
    p = claude_in_turn()
    [data] = p.interrupt_turn()
    req = json.loads(data)
    assert req["type"] == "control_request" and req["request"] == {"subtype": "interrupt"}
    assert p.feed(json.dumps(receipt(req["request_id"])).encode()) == ([], [])
    [ended], _ = p.feed(json.dumps(aborted()).encode())
    assert p.interrupted(ended)
    q = claude_in_turn()
    [data] = q.interrupt_turn()
    q.feed(json.dumps(receipt(json.loads(data)["request_id"], ok=False)).encode())
    [own], _ = q.feed(json.dumps(aborted()).encode())
    assert not q.interrupted(own), "refused: a later abort is the turn's own"
    r = claude_in_turn()
    [data] = r.interrupt_turn()
    r.feed(json.dumps(receipt("interrupt-someone-else")).encode())
    [other], _ = r.feed(json.dumps(aborted()).encode())
    assert not r.interrupted(other), "a receipt for another request says nothing of this one"
    [ok], _ = claude_in_turn().feed(json.dumps(result()).encode())
    assert not p.interrupted(ok), "only an abort is the interrupt's doing"
    t = claude_in_turn()
    [data] = t.interrupt_turn()
    t.feed(json.dumps(receipt(json.loads(data)["request_id"])).encode())
    [failed], _ = t.feed(json.dumps(result(subtype="error_during_execution", is_error=True)).encode())
    assert not t.interrupted(failed), "a failure that is no abort is the turn's own, receipt or not"


def test_codex_never_steers_and_its_interrupt_is_turn_interrupt_with_the_turns_id():
    p = cx.ready()
    assert providers.modes(p) == ("restart",) and p.steers is False and not hasattr(p, "steer")
    p.turn("long")
    assert p.interrupt_turn() is None, "Codex has not named the turn yet"
    p.feed(cx.reply(3, cx.TURN_REPLY))
    [stop] = cx.sent(p.interrupt_turn())
    assert stop == {"id": 4, "method": "turn/interrupt", "params": {"threadId": cx.THREAD, "turnId": cx.TURN}}
    assert p.feed(cx.reply(4, {})) == ([], [])
    [old], _ = p.feed(cx.line(cx.completed(status="interrupted")))
    assert p.interrupted(old)
    for status in ("completed", "failed"):
        q = cx.in_turn("long")
        [done], _ = q.feed(cx.line(cx.completed(status=status)))
        assert not q.interrupted(done), status


def test_codex_counts_an_interrupted_end_as_the_restarts_only_after_a_success_answer_to_that_request():
    """R5-01: Codex ends a turn `interrupted` for causes of its own; a rejected or unanswered request interrupted
    nothing."""
    def named():
        q = cx.in_turn("long")
        q.feed(cx.reply(3, cx.TURN_REPLY))
        [stop] = cx.sent(q.interrupt_turn())
        return q, stop["id"]
    q, rid = named()
    q.feed(cx.reply(rid, error={"code": -32600, "message": "no active turn to interrupt"}))
    [end], _ = q.feed(cx.line(cx.completed(status="interrupted")))
    assert not q.interrupted(end), "rejected, then interrupted by something else"
    q, rid = named()
    [end], _ = q.feed(cx.line(cx.completed(status="interrupted")))
    assert not q.interrupted(end), "not answered (yet): no restart's"
    q, rid = named()
    q.feed(cx.reply(rid + 1, {}))
    [end], _ = q.feed(cx.line(cx.completed(status="interrupted")))
    assert not q.interrupted(end), "an answer to another request says nothing of this one"
    q, rid = named()
    q.feed(cx.reply(rid, {}))
    [end], _ = q.feed(cx.line(cx.completed()))
    assert not q.interrupted(end), "accepted, but the turn completed by itself"


def test_codex_retires_an_unanswered_restart_request_with_its_turn():
    """Review follow-up (#103): a restart's turn/interrupt that is never answered does not stay pending after its
    turn ended (as the turn's own turn/start does not); a late answer then changes nothing."""
    q = cx.in_turn("long")
    q.feed(cx.reply(3, cx.TURN_REPLY))
    [stop] = cx.sent(q.interrupt_turn())
    assert q._pending.get(stop["id"]) == "turn/interrupt"
    [end], _ = q.feed(cx.line(cx.completed(status="interrupted")))
    assert stop["id"] not in q._pending, "retired with the turn"
    assert q.feed(cx.reply(stop["id"], {})) == ([], [])


# ------------------------------------------------------------ the session host


def test_a_claude_restart_interrupts_the_turn_and_its_message_is_then_the_next_ordinary_turn(henv):
    """R2 and R7: the interrupt alone; the old turn's end says `restarted_by`; the message comes as any next one."""
    s, child = claude_turn(henv)
    henv.host.interrupt_for(SID, "1", "2")
    assert henv.host.tick() == [], "the interrupt is no delivery"
    assert len(interrupts(child)) == 1 and users(child) == ["first"]
    answered(child)
    [end] = henv.host.tick()
    assert (end.kind, end.message_id, end.detail, end.restarted_by) == ("turn_ended", "1", INTERRUPTED.format(n="2"),
                                                                        "2")
    assert s.restart_by is None and not s.busy
    henv.host.send(SID, "start over", message_id="2")       # the caller's next ordinary message
    assert kinds(henv.host.tick()) == [("delivered", "2")]
    assert {k: v for k, v in lines(child.stdin)[-1].items() if k != "uuid"} == {
        "type": "user", "message": {"role": "user", "content": "start over"}}


def test_a_codex_restart_is_turn_interrupt_with_the_turns_id(henv):
    s, child = codex_turn(henv)
    henv.host.interrupt_for(SID, "1", "2")
    assert henv.host.tick() == []
    assert sent(child)[-1] == {"id": 4, "method": "turn/interrupt", "params": {"threadId": cx.THREAD,
                                                                                "turnId": cx.TURN}}
    child.emit(cx.reply(4, {}), cx.line(cx.completed(status="interrupted")))
    [end] = henv.host.tick()
    assert end.restarted_by == "2"


def test_an_abort_is_the_restarts_only_once_its_interrupt_was_written_whole(henv):
    """AC-12 and R2-02: an interrupt still in the buffer did not end the turn, and is never written after it."""
    s, child = codex_turn(henv)
    child.accept = 0
    henv.host.interrupt_for(SID, "1", "2")
    henv.host.tick()
    # (Interrupted by something else; even an answer that reads as the request's does not make it the restart's: the
    # host's own rule, that the interrupt was written whole, is what this pins.)
    child.emit(cx.reply(4, {}), cx.line(cx.completed(status="interrupted")))
    old = next(e for e in henv.host.tick() if e.kind == "turn_ended")
    assert old.restarted_by is None and old.detail != INTERRUPTED.format(n="2")
    child.accept = None
    henv.host.tick()
    assert "turn/interrupt" not in methods(child), "the stale interrupt is dropped"


def test_a_restart_waits_for_a_claude_turn_to_begin_before_it_knows_whether_it_can(henv):
    """What a Claude process can do is known once its `init` came: the interrupt waits for it."""
    open_session(henv)
    henv.host.send(SID, "first", message_id="1")
    henv.host.tick()
    child = henv.spawner.children[-1]
    henv.host.interrupt_for(SID, "1", "2")
    assert henv.host.tick() == [] and not interrupts(child)
    child.emit(cinit())
    assert kinds(henv.host.tick()) == [("turn_started", "1")] and len(interrupts(child)) == 1


def test_a_restart_waits_for_a_later_claude_turn_to_begin_too(henv):
    """Review follow-up (#103), measured on Claude Code 2.1.330: from a process's second turn on it has said it can
    restart (its first `init`), but an interrupt written before THIS turn's `init` is answered `success` and the turn
    then runs whole. The interrupt waits for the turn's own `init`."""
    s, child = claude_turn(henv)
    child.emit(result())
    assert kinds(henv.host.tick()) == [("turn_ended", "1")]
    henv.host.send(SID, "second, long", message_id="2")
    assert kinds(henv.host.tick()) == [("delivered", "2")] and s.in_flight.id == "2" and not s.begun
    assert henv.host.modes(SID) == ("restart",), "the process said so in its first turn"
    henv.host.interrupt_for(SID, "2", "3")
    henv.host.tick()
    assert not interrupts(child), "not before the turn began"
    child.emit(cinit())
    assert kinds(henv.host.tick()) == [("turn_started", "2")] and len(interrupts(child)) == 1
    answered(child)
    [end] = henv.host.tick()
    assert (end.message_id, end.restarted_by) == ("2", "3")


def test_a_codex_restart_is_written_once_turn_start_named_the_turn_even_without_turn_started(henv):
    """Sol R1: Codex is interrupted as soon as it named the turn (as on main): its answer to turn/start is enough,
    `turn/started` may come late or not at all."""
    host, s = codex_host(henv)
    henv.host = host
    host.send(SID, "first", message_id="1")
    host.tick()
    child = henv.spawner.children[-1]
    codex_ready(host, child)
    host.tick()
    child.emit(cx.reply(3, cx.TURN_REPLY))          # the turn's id, and no turn/started
    host.tick()
    assert s.in_flight.id == "1" and not s.begun
    host.interrupt_for(SID, "1", "2")
    host.tick()
    assert sent(child)[-1] == {"id": 4, "method": "turn/interrupt", "params": {"threadId": cx.THREAD,
                                                                                "turnId": cx.TURN}}


def test_each_adapter_says_when_its_turn_takes_the_interrupt():
    """Sol R1: the seam (`takes_interrupt`). Claude: once THIS turn's `init` came; Codex: once it named the turn."""
    p = claude_in_turn()
    assert p.takes_interrupt()
    p.feed(json.dumps(result()).encode())
    p.turn("second")
    assert p.restarts and not p.takes_interrupt(), "the process can restart, but this turn has not begun"
    p.feed(json.dumps(cinit()).encode())
    assert p.takes_interrupt()
    q = cx.ready()
    q.turn("long")
    assert not q.takes_interrupt(), "Codex has not named the turn yet"
    q.feed(cx.reply(3, cx.TURN_REPLY))
    assert q.takes_interrupt(), "its answer to turn/start names it"


def test_a_claude_receipt_before_the_turns_init_never_credits_a_later_abort_to_the_restart(tmp_path):
    """Sol R2, through real pipes with the stand-in: an interrupt written before a turn's `init` (as a node on the
    previous version may write it) gets an early `success` receipt that was not applied; a later abort of the same
    turn (here by another interrupt, not the restart's) is the turn's own. Deterministic: the stand-in holds the
    turn's `init` until a file exists (`wait-init`), and the file is made only once the early receipt was read."""
    import subprocess
    import sys
    p = ClaudeSession()
    argv = p.argv(["claude", "-p", "{prompt}"], session_id=SID, resume=False, model=None)
    proc = subprocess.Popen([sys.executable, str(ROOT / "tests" / "stand_in_claude.py"), *argv[1:]], cwd=tmp_path,
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)

    def until(stop):
        """Feed the adapter each line the process writes, up to the first that `stop(parsed line, events)` accepts."""
        for raw in proc.stdout:
            events, _ = p.feed(raw)
            if stop(json.loads(raw), events):
                return events
        raise AssertionError("the stand-in exited")
    ended = lambda ev, events: any(e.kind == "turn_ended" for e in events)      # noqa: E731
    try:
        proc.stdin.write(b"".join(p.turn("first")))
        proc.stdin.flush()
        [first] = until(ended)
        assert first.ok and p.restarts
        gate, release = tmp_path / "gate", tmp_path / "release"
        line = b"".join(p.turn(f"wait-init {gate} hold {release}"))
        early = p.interrupt_turn()          # written behind the line, before the turn's `init`
        proc.stdin.write(line + b"".join(early))
        proc.stdin.flush()
        until(lambda ev, events: ev.get("type") == "control_response")          # the early receipt
        assert not p._interrupt_ok, "received before the turn's init: not applied"
        gate.touch()                                                            # only now the turn's `init`
        until(lambda ev, events: any(e.kind == "turn_started" for e in events))
        proc.stdin.write(json.dumps({"type": "control_request", "request": {"subtype": "interrupt"},
                                     "request_id": "someone-else"}).encode() + b"\n")
        proc.stdin.flush()
        [end] = until(ended)
        assert end.meta.get("terminal_reason") == "aborted_tools" and not p.interrupted(end), \
            "the abort is not the early interrupt's"
    finally:
        proc.kill()
        proc.wait()


def test_a_restarted_turn_that_finished_first_keeps_its_outcome(henv):
    s, child = claude_turn(henv)
    henv.host.interrupt_for(SID, "1", "2")
    henv.host.tick()
    child.emit(result(text="done by itself"))
    [old] = henv.host.tick()
    assert (old.ok, old.detail, old.restarted_by, old.text) == (True, "", None, "done by itself")


def test_a_restart_for_a_turn_not_in_flight_or_a_cli_that_cannot_interrupts_nothing(henv):
    s, child = claude_turn(henv)
    henv.host.interrupt_for(SID, "4", "5")
    assert henv.host.tick() == [] and not interrupts(child) and s.restart_by is None
    henv.host.interrupt_for(SID, "1", "6")
    child.emit(result())        # read first in the pass: the turn is over before the interrupt's write
    assert kinds(henv.host.tick()) == [("turn_ended", "1")] and not interrupts(child) and s.restart_by is None


def test_a_claude_process_that_did_not_say_it_acknowledges_the_interrupt_is_never_interrupted(henv):
    s, child = claude_turn(henv, caps=["msg_lifecycle_v1"])
    henv.host.interrupt_for(SID, "1", "2")
    assert henv.host.tick() == [] and not interrupts(child) and s.restart_by is None
    assert henv.host.modes(SID) == ()


def test_one_restart_at_a_time_on_the_host_too(henv):
    s, child = claude_turn(henv)
    henv.host.interrupt_for(SID, "1", "2")
    henv.host.interrupt_for(SID, "1", "3")
    henv.host.tick()
    henv.host.interrupt_for(SID, "1", "2")      # (asked again by the next listing: nothing changes)
    henv.host.tick()
    assert len(interrupts(child)) == 1 and s.restart_by == "2"


def test_a_session_without_a_process_reports_no_modes(henv):
    open_session(henv)
    assert henv.host.modes(SID) == ()


# ------------------------------------------------------------ the state machine (DESIGN_NOTES: its table)


def _in_T(h):
    return claude_turn(h)


def _in_H(h):
    """An interrupt asked for, not written: Codex has not named the turn yet."""
    host, s = codex_host(h)
    h.host = host
    host.send(SID, "first", message_id="1")
    host.tick()
    child = h.spawner.children[-1]
    codex_ready(host, child)
    host.tick()
    host.interrupt_for(SID, "1", "2")
    host.tick()
    assert s.restart_by == "2" and not s.restart_sent
    return s, child


def _in_R(h, accept=None):
    s, child = claude_turn(h)
    child.accept = accept
    h.host.interrupt_for(SID, "1", "2")
    h.host.tick()
    assert s.restart_sent and s.restart_written is (accept is None)
    return s, child


def _cancel(h, n):
    _CANCELLED.extend(h.host.cancel(SID, n, "cancelled by the session that started it"))


def _then_written(h, c):
    c.accept = None
    h.host.tick()
    return True


def _end(ev):
    return next(e for e in ev if e.kind == "turn_ended" and e.message_id == "1")


_CANCELLED: list = []
# (cell, set-up, event, what must hold after it). Each `cell` names the table's state and event (DESIGN_NOTES).
CELLS = [
    ("T x result", _in_T, lambda h, s, c: c.emit(result()),
     lambda h, s, c, ev: kinds(ev) == [("turn_ended", "1")] and not s.busy),
    ("H x result", _in_H, lambda h, s, c: c.emit(cx.reply(3, cx.TURN_REPLY), cx.line(cx.completed())),
     lambda h, s, c, ev: _end(ev).restarted_by is None and "turn/interrupt" not in methods(c)
     and s.restart_by is None),
    ("H x cancel M", _in_H, lambda h, s, c: _cancel(h, "2"),
     lambda h, s, c, ev: ev == [] and s.restart_by is None and not c.terminated and "turn/interrupt" not in methods(c)),
    ("R(unwritten) x result", lambda h: _in_R(h, accept=0), lambda h, s, c: c.emit(aborted()),
     lambda h, s, c, ev: _end(ev).restarted_by is None and _then_written(h, c) and not interrupts(c)),
    ("R x result (interrupted)", _in_R, lambda h, s, c: answered(c),
     lambda h, s, c, ev: _end(ev).restarted_by == "2" and _end(ev).detail == INTERRUPTED.format(n="2")),
    ("R x result (interrupt refused)", _in_R, lambda h, s, c: answered(c, ok=False),
     lambda h, s, c, ev: _end(ev).restarted_by is None and _end(ev).detail != INTERRUPTED.format(n="2")),
    ("R x result (finished first)", _in_R, lambda h, s, c: c.emit(result()),
     lambda h, s, c, ev: _end(ev).restarted_by is None and _end(ev).ok),
    ("R x result (failed otherwise)", _in_R,
     lambda h, s, c: c.emit(result(subtype="error_during_execution", is_error=True)),
     lambda h, s, c, ev: _end(ev).restarted_by is None and _end(ev).detail == ""),
    ("R x process exit", _in_R, lambda h, s, c: setattr(c, "code", 1),
     lambda h, s, c, ev: _end(ev).restarted_by is None and not _end(ev).ok and s.restart_by is None),
    ("R x cancel T", _in_R, lambda h, s, c: _cancel(h, "1"),
     lambda h, s, c, ev: _end(ev).restarted_by is None and c.terminated and s.restart_by is None),
    ("R x cancel M", _in_R, lambda h, s, c: (_cancel(h, "2"), answered(c)),
     lambda h, s, c, ev: _end(ev).restarted_by is None and s.restart_by is None),
]


@pytest.mark.parametrize("cell,setup,event,check", CELLS, ids=[c[0] for c in CELLS])
def test_the_state_machine_table(henv, cell, setup, event, check):
    s, child = setup(henv)
    _CANCELLED.clear()
    event(henv, s, child)
    events = list(_CANCELLED) + henv.host.tick()
    assert check(henv, s, child, events), (cell, [(e.kind, e.message_id, e.detail) for e in events])


def test_the_table_and_its_cells_agree():
    """R2-07: one parametrized case per host cell of the table in DESIGN_NOTES ("Host cells: 11")."""
    notes = (ROOT / "DESIGN_NOTES.md").read_text()
    assert f"Host cells: {len(CELLS)}" in notes and len(CELLS) == 11


# ------------------------------------------------------------ both stand-ins, through real pipes


def tick_until(host, test, seconds=30.0) -> list:
    seen, deadline = [], time.monotonic() + seconds
    while time.monotonic() < deadline:
        seen += host.tick()
        if test(seen):
            return seen
        time.sleep(0.02)
    raise AssertionError(f"not within {seconds}s; saw {[(e.kind, e.message_id) for e in seen]}")


def ended(seen, n):
    return next((e for e in seen if e.kind == "turn_ended" and e.message_id == n), None)


def restart_scenario(host, tmp_path, first=1):
    """A long turn interrupted for a restart; the restart's message then runs as the next ordinary turn."""
    release, a, b = tmp_path / f"release-{first}", str(first), str(first + 1)
    host.send(SID, f"hold {release}", message_id=a)
    tick_until(host, lambda seen: any(e.kind == "turn_started" for e in seen))
    host.interrupt_for(SID, a, b)
    seen = tick_until(host, lambda seen: ended(seen, a) is not None)
    old = ended(seen, a)
    host.send(SID, "say kiwi instead", message_id=b)
    seen = tick_until(host, lambda seen: ended(seen, b) is not None)
    release.touch()
    return {"interrupted": (old.ok, old.detail, old.restarted_by), "restarted": ended(seen, b).text}


def test_the_claude_stand_in_is_restarted_in_band(tmp_path, monkeypatch):
    host, s = real_host(tmp_path, monkeypatch, (ROOT / "tests" / "stand_in_claude.py").read_text(),
                        timeout_seconds=60, grace=2)
    try:
        out = restart_scenario(host, tmp_path)
        pid = s.child.pid
    finally:
        host.stop_all()
    assert out["interrupted"] == (False, INTERRUPTED.format(n="2"), "2")
    assert out["restarted"].endswith(": SAY KIWI INSTEAD") and pid, "the same process took the next turn"


def test_the_codex_stand_in_links_a_restart_only_to_the_interruption_its_own_accepted_request_caused(tmp_path):
    """R5-01, through real pipes: rejected then interrupted by itself; accepted but completed normally."""
    host = real_codex_host(tmp_path, timeout_seconds=60)
    host.open(SID, agent="codex", runtime="codex-cli", project=PROJECT)
    out = {}
    try:
        for first, cue in ((1, "refuse-interrupt self-interrupt"), (3, "ignore-interrupt")):
            release, a = tmp_path / f"release-{first}", str(first)
            host.send(SID, f"hold {release} {cue}", message_id=a)
            tick_until(host, lambda seen: any(e.kind == "turn_started" and e.message_id == a for e in seen))
            host.interrupt_for(SID, a, str(first + 1))
            tick_until(host, lambda seen: host.sessions[SID].restart_sent and not host.sessions[SID].outbuf,
                       seconds=10)
            time.sleep(0.3)         # its answer arrives
            release.touch()
            end = ended(tick_until(host, lambda seen: ended(seen, a) is not None), a)
            out[cue] = (end.ok, end.meta.get("status"), end.restarted_by)
    finally:
        host.stop_all()
    assert out["refuse-interrupt self-interrupt"] == (False, "interrupted", None)
    assert out["ignore-interrupt"] == (True, "completed", None)


def test_the_codex_stand_in_is_restarted_in_band(tmp_path):
    host = real_codex_host(tmp_path, timeout_seconds=60)
    host.open(SID, agent="codex", runtime="codex-cli", project=PROJECT)
    try:
        out = restart_scenario(host, tmp_path)
    finally:
        host.stop_all()
    assert out["interrupted"] == (False, INTERRUPTED.format(n="2"), "2")
    assert out["restarted"].endswith(": SAY KIWI INSTEAD")
