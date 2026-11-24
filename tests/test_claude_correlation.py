"""Claude turn correlation (DESIGN_NOTES "Claude turn correlation (2026-11-24)"; the steering design on branch
claude/steer-loose, its part C, step 1): every host user line carries a fresh uuid, and in a VERIFIED process
incarnation the adapter attributes each `init` and `result` by the evidence it carries (the bracket table), not by
order. VERIFIED ships empty, so every real Claude process today is attributed by order, as on main.

The frames are the shapes Claude Code 2.1.330 was measured to write (the steering design's part B): an owned `init`
lists the line's uuid in `user_message_uuids`; an owned `result` in `user_message_uuid` and `user_message_uuids` and
has no `origin`; a turn the CLI started itself has an `init` with no echo and a `result` with no echo, `num_turns` 1
and `origin.kind` `task-notification`. Adapter rows from strings, the host with fake processes (test_session_host),
and the stand-in (tests/stand_in_claude.py) through real pipes. Never a real agent CLI."""

import json
import time

import pytest

from agent_comms import providers
from agent_comms.providers import claude
from agent_comms.providers.claude import OWN, UNATTRIBUTED, ClaudeSession
from agent_comms.session_host import INTERRUPTED

from test_session_host import ROOT, SID, henv, open_session, real_host, result  # noqa: F401

V = "2.1.330"                                       # the version the tests verify (VERIFIED itself ships empty)
CAPS = ["msg_lifecycle_v1", "interrupt_receipt_v1"]
NOTIFY = {"kind": "task-notification", "producer": "session-task"}


@pytest.fixture
def verified(monkeypatch):
    """A build in which version V passed the release gates (VERIFIED is empty in this one)."""
    monkeypatch.setattr(claude, "VERIFIED", frozenset({V}))


# ------------------------------------------------------------ frames (the steering design's part B)


def vinit(*uuids, version=V, caps=CAPS, mode="dontAsk") -> dict:
    """An `init`: owned when it lists uuids, unmarked (no `user_message_uuids` key at all, as measured) when not."""
    ev = {"type": "system", "subtype": "init", "session_id": SID, "permissionMode": mode, "tools": ["Read"],
          "claude_code_version": version, "capabilities": list(caps)}
    return ev | ({"user_message_uuids": list(uuids)} if uuids else {})


def owned(*uuids, **kw) -> dict:
    """An owned `result`: it echoes these lines (the first is the turn's own)."""
    return result(**kw) | {"user_message_uuid": uuids[0], "user_message_uuids": list(uuids)}


def plain(**kw) -> dict:
    """A `result` with no evidence: no echo, no origin (what main's lines got, and a deviation in a verified one)."""
    return result(**kw)


def foreign(text="background: FINISHED") -> dict:
    """The `result` of a turn the CLI started itself (bg-late, measured): no echo, num_turns 1, a notification."""
    return result(text=text) | {"origin": NOTIFY}


def nonturn(error=False) -> dict:
    """A zero-turn task notification's `result`: no echo, num_turns 0, a notification origin; a success or an error."""
    return result(subtype="error_during_execution" if error else "success", is_error=error, text="") | {
        "num_turns": 0, "origin": NOTIFY}


def refusal() -> dict:
    """What a refused `--resume` writes before any `init` (an error, num_turns 0)."""
    return {"type": "result", "subtype": "error_during_execution", "is_error": True, "num_turns": 0,
            "session_id": SID, "permission_denials": [], "errors": [f"No conversation found with session ID: {SID}"]}


def aborted(*uuids) -> dict:
    """An interrupted turn's result (measured: error_during_execution, aborted_tools, echoing the turn's line)."""
    return owned(*uuids, subtype="error_during_execution", is_error=True, text=None) | {
        "terminal_reason": "aborted_tools"}


# ------------------------------------------------------------ the adapter, from strings


def adapter() -> ClaudeSession:
    p = ClaudeSession()
    p.argv(["claude", "-p", "{prompt}"], session_id=SID, resume=False, model=None)
    return p


def write(p, text="go") -> str:
    """The host writes a turn: its uuid."""
    [data] = p.turn(text)
    return json.loads(data)["uuid"]


def fed(p, ev) -> list[tuple[str, str]]:
    """(kind, turn) of the turn events one line means."""
    events, _ = p.feed(json.dumps(ev).encode())
    return [(e.kind, e.turn) for e in events if e.kind in ("turn_started", "turn_ended")]


def idle_verified() -> ClaudeSession:
    """A verified process after one ordinary turn: no bracket open, no turn in flight."""
    p = adapter()
    m0 = write(p)
    assert fed(p, vinit(m0)) == [("turn_started", "own")] and p.verified
    assert fed(p, owned(m0)) == [("turn_ended", "own")]
    return p


def bracket(state: str):
    """(adapter, M's uuid) with M written and the bracket in this state: none, own (M's) or unattributed."""
    p = idle_verified()
    m = write(p)
    if state == "own":
        fed(p, vinit(m))
    elif state == "unattributed":
        fed(p, vinit())
    assert p._bracket == {"none": None, "own": OWN, "unattributed": UNATTRIBUTED}[state]
    return p, m


def test_verified_ships_empty_so_every_real_process_is_attributed_by_order():
    assert claude.VERIFIED == frozenset(), "a version goes in only once M3, M6 and M7 passed on it"
    p = adapter()
    m = write(p)
    assert fed(p, vinit()) == [("turn_started", "order")], "an unmarked init: by order, as on main"
    assert fed(p, foreign()) == [("turn_ended", "order")], "a foreign result: by order, as on main (the old bug)"
    assert not p.verified and p.holds_turns() is False
    assert fed(p, vinit(m)) == [("turn_started", "order")] and fed(p, nonturn()) == [("turn_ended", "order")]


def test_every_user_line_carries_a_fresh_random_uuid_and_the_same_content_bytes():
    from test_providers_claude import main_bytes
    p = adapter()
    a, b = p.turn("same"), p.turn("same")
    assert main_bytes(a[0]) == main_bytes(b[0]) == b'{"type": "user", "message": {"role": "user", "content": "same"}}\n'
    ids = [json.loads(x[0])["uuid"] for x in (a, b)]
    assert ids[0] != ids[1] and all(len(u) == 36 and u[14] == "4" for u in ids)


def test_the_uuid_does_not_come_from_uuid4(monkeypatch):
    """tests/golden_schema_free.py makes uuid4 (and token_hex) deterministic from one counter: a line's uuid must
    not draw from it, or every id after it in the recording would shift."""
    import uuid
    monkeypatch.setattr(uuid, "uuid4", lambda: (_ for _ in ()).throw(AssertionError("uuid4 called")))
    write(adapter())


@pytest.mark.usefixtures("verified")
def test_the_gate_is_a_verified_version_that_lists_msg_lifecycle_in_its_first_init():
    for init in (vinit(version="2.1.331"), vinit(caps=["interrupt_receipt_v1"]), vinit(version=None)):
        p = adapter()
        write(p)
        assert fed(p, init) == [("turn_started", "order")] and not p.verified
    p = adapter()
    write(p)
    assert fed(p, vinit()) == [("turn_started", "other")] and p.verified


@pytest.mark.usefixtures("verified")
def test_verification_is_lost_for_good_at_the_first_deviation_and_a_new_process_is_asked_again():
    p, m = bracket("none")
    assert fed(p, plain()) == [("turn_ended", "order")], "none | plain while M in flight: by order"
    assert not p.verified and p.deviation
    m2 = write(p)
    assert fed(p, vinit()) == [("turn_started", "order")] and fed(p, owned(m2)) == [("turn_ended", "order")]
    assert not p.verified, "never again in this incarnation, whatever its frames show"
    p.argv(["claude", "-p", "{prompt}"], session_id=SID, resume=True, model=None)     # the next process
    m3 = write(p)
    assert fed(p, vinit(m3)) == [("turn_started", "own")] and p.verified


@pytest.mark.usefixtures("verified")
def test_an_init_that_no_longer_qualifies_is_a_deviation():
    p, m = bracket("own")
    assert fed(p, vinit(version="2.1.999")) == [("turn_started", "order")] and not p.verified


# --- the bracket table, row by row (the steering design's C; named in DESIGN_NOTES' mutation table)


@pytest.mark.usefixtures("verified")
@pytest.mark.parametrize("state", ["none", "own", "unattributed"])
@pytest.mark.parametrize("error", [False, True])
def test_row_any_non_turn_is_inert_in_every_bracket(state, error):
    """Any bracket | non-turn: nothing. The zero-turn ERROR result as well as the empty successful notification
    (the condition for code review, part O)."""
    p, m = bracket(state)
    before = (p._bracket, p._current, p.holds_turns())
    assert fed(p, nonturn(error)) == []
    assert (p._bracket, p._current, p.holds_turns()) == before and p.verified


@pytest.mark.usefixtures("verified")
def test_row_none_owned_init_for_m_opens_ms_owned_bracket():
    p, m = bracket("none")
    assert fed(p, vinit(m)) == [("turn_started", "own")] and p._bracket == OWN and not p.holds_turns()


@pytest.mark.usefixtures("verified")
@pytest.mark.parametrize("m_written", [True, False])
def test_row_none_unmarked_init_opens_an_unattributed_bracket_and_the_barrier_holds(m_written):
    p = idle_verified()
    if m_written:
        write(p)
    assert fed(p, vinit()) == [("turn_started", "other")]
    assert p._bracket == UNATTRIBUTED and p.holds_turns() is True


@pytest.mark.usefixtures("verified")
@pytest.mark.parametrize("which", ["m", "unmarked", "other"])
def test_row_owned_a_second_init_never_replaces_the_bracket(which):
    p, m = bracket("own")
    others = list(p._written - {m})
    init = {"m": vinit(m), "unmarked": vinit(), "other": vinit(others[0])}[which]
    assert fed(p, init) == [("turn_started", "other")]
    assert p._bracket == OWN and p._current == m
    assert p._competing is (which != "m"), "a competing init is noted (the steering design's F-4)"


@pytest.mark.usefixtures("verified")
def test_row_owned_result_listing_m_closes_it_as_ms():
    p, m = bracket("own")
    assert fed(p, owned(m)) == [("turn_ended", "own")] and p._bracket is None and p._current is None


@pytest.mark.usefixtures("verified")
def test_row_owned_plain_result_closes_it_as_ms():
    p, m = bracket("own")
    assert fed(p, plain()) == [("turn_ended", "own")] and p._bracket is None and p.verified, "no deviation here"


@pytest.mark.usefixtures("verified")
def test_row_owned_foreign_result_is_nothing_and_the_bracket_stays_ms():
    p, m = bracket("own")
    assert fed(p, foreign()) == [("turn_ended", "other")] and p._bracket == OWN and p._current == m


@pytest.mark.usefixtures("verified")
def test_row_owned_result_listing_only_other_host_uuids_is_nothing():
    """Also where a result listing only a steer S written into M's bracket would go (F-4's case, the steering
    design's D): no steer is written in this build, and the seam says so."""
    p, m = bracket("own")
    [other] = list(p._written - {m})
    assert p._steer_only(owned(other)) is False
    assert fed(p, owned(other)) == [("turn_ended", "other")] and p._bracket == OWN and p._current == m


@pytest.mark.usefixtures("verified")
def test_row_unattributed_foreign_result_closes_it_and_the_barrier_lifts():
    p, m = bracket("unattributed")
    assert fed(p, foreign()) == [("turn_ended", "other")]
    assert p._bracket is None and not p.holds_turns() and p._current == m, "M (written before it) still in flight"


@pytest.mark.usefixtures("verified")
def test_row_unattributed_owned_result_listing_m_is_ms():
    """Claude took M into the turn it started itself."""
    p, m = bracket("unattributed")
    assert fed(p, owned(m)) == [("turn_ended", "own")] and p._bracket is None and not p.holds_turns()


@pytest.mark.usefixtures("verified")
@pytest.mark.parametrize("m_written", [True, False])
def test_row_unattributed_plain_result_is_a_deviation_by_order(m_written):
    p = idle_verified()
    if m_written:
        write(p)
    fed(p, vinit())
    assert fed(p, plain()) == [("turn_ended", "order")], "the host's turn in flight if any (as main), else logged"
    assert not p.verified and p.deviation and not p.holds_turns()


@pytest.mark.usefixtures("verified")
def test_row_none_owned_result_listing_m_is_ms_when_its_init_was_lost():
    p, m = bracket("none")
    assert fed(p, owned(m)) == [("turn_ended", "own")] and p.verified


@pytest.mark.usefixtures("verified")
def test_row_none_plain_result_with_m_in_flight_is_ms_by_order_and_a_deviation():
    p, m = bracket("none")
    assert fed(p, plain()) == [("turn_ended", "order")] and not p.verified


@pytest.mark.usefixtures("verified")
def test_row_none_foreign_result_is_the_clis_own():
    p, m = bracket("none")
    assert fed(p, foreign()) == [("turn_ended", "other")] and p.verified and p._current == m


@pytest.mark.usefixtures("verified")
def test_row_before_any_init_a_launch_refusal_fails_the_message_as_today():
    p = adapter()
    write(p)
    [ev], _ = p.feed(json.dumps(refusal()).encode())
    assert (ev.kind, ev.turn, ev.ok, ev.answered) == ("turn_ended", "order", False, True)


@pytest.mark.usefixtures("verified")
def test_interrupt_readiness_needs_ms_owned_bracket():
    p, m = bracket("unattributed")
    assert p.restarts and not p.takes_interrupt(), "not during a turn the CLI started, though M is in flight"
    fed(p, foreign())
    assert not p.takes_interrupt()
    fed(p, vinit(m))
    assert p.takes_interrupt()
    # Unverified (as main): any init of the turn makes it ready.
    q = adapter()
    write(q)
    q.feed(json.dumps(vinit(version="9")).encode())
    assert q.takes_interrupt()


# ------------------------------------------------------------ the host, with fake processes


def uuid_of(child, k=-1) -> str:
    users = [json.loads(x) for x in child.stdin.split(b"\n") if x and json.loads(x).get("type") == "user"]
    return users[k]["uuid"]


def emit(henv, child, *events):
    child.emit(*events)
    return [(e.kind, e.message_id) for e in henv.host.tick()]


def started(henv, text="first", mid="1"):
    """The session's process with message `mid` written: (session, child, M's uuid)."""
    s = henv.host.sessions.get(SID) or open_session(henv)
    henv.host.send(SID, text, message_id=mid)
    assert ("delivered", mid) in [(e.kind, e.message_id) for e in henv.host.tick()]
    child = henv.spawner.children[-1]
    return s, child, uuid_of(child)


def idle_host(henv):
    """A verified session after one ordinary turn (message 0)."""
    s, child, m0 = started(henv, "zero", "0")
    assert emit(henv, child, vinit(m0), owned(m0)) == [("turn_started", "0"), ("turn_ended", "0")]
    assert s.provider.verified and not s.busy
    return s, child


def ends(events, mid):
    return [e for e in events if e.kind == "turn_ended" and e.message_id == mid]


# --- the 11 ordinary sequences the steering design's C checks, each settling M once with none stranded


@pytest.mark.usefixtures("verified")
def test_seq_owned_init_then_owned_result(henv):
    s, child = idle_host(henv)
    _, _, m = started(henv)
    child.emit(vinit(m), owned(m, text="mine"))
    evs = henv.host.tick()
    assert [(e.kind, e.message_id) for e in evs] == [("turn_started", "1"), ("turn_ended", "1")]
    assert ends(evs, "1")[0].text == "mine" and ends(evs, "1")[0].ok and not s.busy


@pytest.mark.usefixtures("verified")
def test_seq_owned_init_then_a_plain_failure_result(henv):
    s, child = idle_host(henv)
    _, _, m = started(henv)
    child.emit(vinit(m), plain(subtype="error_during_execution", is_error=True, text="broke"))
    [st, end] = henv.host.tick()
    assert (end.message_id, end.ok, end.text, end.answered) == ("1", False, "broke", True) and s.provider.verified


@pytest.mark.usefixtures("verified")
@pytest.mark.parametrize("error", [False, True])
def test_seq_owned_init_a_zero_turn_notification_then_owned_result(henv, error):
    s, child = idle_host(henv)
    _, _, m = started(henv)
    assert emit(henv, child, vinit(m), nonturn(error)) == [("turn_started", "1")]
    child.emit(owned(m, text="mine"))
    [end] = henv.host.tick()
    assert (end.kind, end.message_id, end.text) == ("turn_ended", "1", "mine")


@pytest.mark.usefixtures("verified")
def test_seq_owned_init_an_unmarked_init_compaction_then_owned_result(henv):
    s, child = idle_host(henv)
    _, _, m = started(henv)
    assert emit(henv, child, vinit(m), vinit()) == [("turn_started", "1")], "the re-init starts nothing"
    child.emit(owned(m, text="mine"))
    [end] = henv.host.tick()
    assert (end.message_id, end.text) == ("1", "mine") and s.provider.verified


@pytest.mark.usefixtures("verified")
def test_seq_an_unattributed_turn_before_ms_own_pair(henv):
    """The bug turn correlation fixes (bg-late, measured): M was written just before a turn the CLI started; that
    turn's frames are not M's, and M's own result is."""
    s, child = idle_host(henv)
    _, _, m = started(henv)
    assert emit(henv, child, vinit(), foreign()) == []
    assert s.in_flight.id == "1" and not s.begun
    child.emit(vinit(m), owned(m, text="mine"))
    evs = henv.host.tick()
    assert [(e.kind, e.message_id) for e in evs] == [("turn_started", "1"), ("turn_ended", "1")]
    assert ends(evs, "1")[0].text == "mine"


def test_seq_unverified_an_unattributed_turn_before_ms_own_pair_is_mains(henv):
    """VERIFIED empty: the same frames are attributed by order, exactly as on main (the CLI's frames taken for M,
    M's own result dropped)."""
    s, child, _ = started(henv, "zero", "0")
    emit(henv, child, vinit(), plain())
    _, _, m = started(henv)
    evs = []
    child.emit(vinit(), foreign())
    evs += henv.host.tick()
    assert [(e.kind, e.message_id) for e in evs] == [("turn_started", "1"), ("turn_ended", "1")]
    assert ends(evs, "1")[0].text == "background: FINISHED", "main's behaviour, unchanged while VERIFIED is empty"
    child.emit(vinit(m), owned(m, text="mine"))
    assert henv.host.tick() == []


@pytest.mark.usefixtures("verified")
def test_seq_unmarked_init_then_plain_result_with_m_in_flight_lost_echoes(henv):
    s, child = idle_host(henv)
    _, _, m = started(henv)
    child.emit(vinit(), plain(text="whose"))
    [end] = henv.host.tick()
    assert (end.kind, end.message_id, end.text) == ("turn_ended", "1", "whose"), "by order, as main"
    assert not s.provider.verified and s.provider.deviation


@pytest.mark.usefixtures("verified")
@pytest.mark.parametrize("frame", ["owned", "plain"])
def test_seq_a_result_with_no_init(henv, frame):
    s, child = idle_host(henv)
    _, _, m = started(henv)
    child.emit(owned(m, text="mine") if frame == "owned" else plain(text="mine"))
    [end] = henv.host.tick()
    assert (end.message_id, end.text) == ("1", "mine")
    assert s.provider.verified is (frame == "owned")


@pytest.mark.usefixtures("verified")
def test_seq_a_launch_refusal_fails_the_message_that_started_the_process(henv):
    s, child, m = started(henv)
    child.emit(refusal())
    [end] = henv.host.tick()
    assert (end.kind, end.message_id, end.ok) == ("turn_ended", "1", False)


@pytest.mark.usefixtures("verified")
def test_seq_an_interrupt_owned_init_then_aborted_owned_result(henv):
    s, child = idle_host(henv)
    _, _, m = started(henv)
    emit(henv, child, vinit(m))
    henv.host.interrupt_for(SID, "1", "2")
    henv.host.tick()
    [req] = [json.loads(x) for x in child.stdin.split(b"\n") if x and json.loads(x).get("type") == "control_request"]
    child.emit({"type": "control_response", "response": {"subtype": "success", "request_id": req["request_id"],
                                                         "response": {"still_queued": []}}}, aborted(m))
    [end] = henv.host.tick()
    assert (end.message_id, end.restarted_by, end.detail) == ("1", "2", INTERRUPTED.format(n="2"))


@pytest.mark.usefixtures("verified")
def test_seq_a_timeout_with_no_result_is_the_hosts_timeout_path(henv):
    s, child = idle_host(henv)
    _, _, m = started(henv)
    emit(henv, child, vinit(m))
    henv.clock.advance(30 * 60 + 1)
    [end] = henv.host.tick()
    assert (end.message_id, end.ok, end.detail) == ("1", False, "timed out after 30 minutes")
    assert child.terminated == 1, "the process was stopped"


@pytest.mark.usefixtures("verified")
def test_seq_a_zero_turn_notification_with_nothing_open(henv):
    s, child = idle_host(henv)
    assert emit(henv, child, nonturn()) == [] and emit(henv, child, nonturn(error=True)) == []
    _, _, m = started(henv)
    child.emit(vinit(m), owned(m, text="mine"))
    assert [e.text for e in henv.host.tick() if e.kind == "turn_ended"] == ["mine"]


@pytest.mark.usefixtures("verified")
def test_review_condition_unattributed_init_then_owned_init_then_plain_result(henv):
    """Part O (R6-04): an unmarked init, then M's own init, then a plain result: the evidence makes the bracket M's
    (an unattributed bracket is no owned one, so it is not 'never replaced'); the competing init is noted; the plain
    result is M's, settled once; the incarnation stays verified (a plain result in an owned bracket is no deviation)."""
    s, child = idle_host(henv)
    _, _, m = started(henv)
    assert emit(henv, child, vinit()) == []
    assert s.provider.holds_turns()
    assert emit(henv, child, vinit(m)) == [("turn_started", "1")]
    assert s.provider._bracket == OWN and s.provider._competing and not s.provider.holds_turns()
    child.emit(plain(text="mine"))
    [end] = henv.host.tick()
    assert (end.message_id, end.text) == ("1", "mine") and s.provider.verified and not s.busy
    assert henv.host.tick() == []


# --- the barrier, the access check, interrupt readiness


@pytest.mark.usefixtures("verified")
def test_the_write_barrier_holds_while_an_unattributed_bracket_is_open(henv):
    s, child = idle_host(henv)
    emit(henv, child, vinit())
    henv.host.send(SID, "wait", message_id="1")
    for _ in range(3):
        henv.clock.advance(60)
        assert henv.host.tick() == [] and s.inbox and child.stdin.count(b'"wait"') == 0
    child.emit(foreign())
    assert [(e.kind, e.message_id) for e in henv.host.tick()] == [("delivered", "1")]


@pytest.mark.usefixtures("verified")
def test_the_barrier_is_bounded_by_the_turn_timeout_then_the_message_fails_and_the_process_stops(henv):
    s, child = idle_host(henv)
    emit(henv, child, vinit())
    henv.host.send(SID, "wait", message_id="1")
    assert henv.host.tick() == []
    henv.clock.advance(30 * 60)
    [end] = henv.host.tick()
    assert (end.kind, end.message_id, end.delivered, end.ok) == ("turn_ended", "1", False, False)
    assert "busy with a turn of its own" in end.detail and child.terminated == 1


@pytest.mark.usefixtures("verified")
def test_a_message_written_just_before_such_a_turn_began_is_not_taken_back(henv):
    s, child = idle_host(henv)
    _, _, m = started(henv)
    emit(henv, child, vinit())
    assert s.in_flight.id == "1" and s.provider.holds_turns()
    henv.host.send(SID, "next", message_id="2")
    assert henv.host.tick() == [] and list(m.id for m in s.inbox) == ["2"]


@pytest.mark.usefixtures("verified")
@pytest.mark.parametrize("state", ["none", "unattributed", "own"])
def test_the_access_check_runs_on_every_init_whatever_its_bracket(henv, state):
    s, child = idle_host(henv)
    if state == "own":
        _, _, m = started(henv)
        emit(henv, child, vinit(m))
    elif state == "unattributed":
        emit(henv, child, vinit())
    child.emit(vinit(mode="bypassPermissions"))
    evs = henv.host.tick()
    assert s.ended and "ended" in [e.kind for e in evs], "a process that bypasses where it was not started so"


@pytest.mark.usefixtures("verified")
def test_the_host_writes_the_interrupt_only_inside_ms_owned_bracket(henv):
    s, child = idle_host(henv)
    _, _, m = started(henv)
    emit(henv, child, vinit())          # the CLI's own turn began just after M was written
    henv.host.interrupt_for(SID, "1", "2")
    henv.host.tick()
    assert b"interrupt" not in child.stdin
    emit(henv, child, foreign())
    assert b"interrupt" not in child.stdin
    emit(henv, child, vinit(m))
    henv.host.tick()
    assert child.stdin.count(b'"interrupt"') == 1


# ------------------------------------------------------------ the stand-in, through real pipes


def stand_in(**env) -> str:
    """tests/stand_in_claude.py with these knobs set in its own environment (a session's process gets only the
    node's minimal environment)."""
    knobs = "".join(f"os.environ[{k!r}] = {v!r}\n" for k, v in env.items())
    return "import os\n" + knobs + (ROOT / "tests" / "stand_in_claude.py").read_text()


def ends_of(host, ids, seconds=30.0) -> dict:
    got, deadline = {}, time.monotonic() + seconds
    while time.monotonic() < deadline and not set(ids) <= set(got):
        for e in host.tick():
            if e.kind == "turn_ended":
                got[e.message_id] = e
        time.sleep(0.02)
    assert set(ids) <= set(got), f"no end for {set(ids) - set(got)}"
    return got


def transcript(tmp_path) -> list[dict]:
    path = tmp_path / "node-logs" / "sessions" / f"{SID}.jsonl"
    return [json.loads(x) for x in path.read_text().splitlines() if x]


@pytest.mark.parametrize("verify", [True, False])
def test_real_pipes_a_turn_the_cli_starts_itself_is_no_messages(tmp_path, monkeypatch, verify):
    """bg-late through the stand-in: message 1's turn is followed by a turn the CLI starts itself; message 2 is
    written right after 1 ends, before that turn begins, and runs after it. Verified: 2's result is its own. With
    VERIFIED empty (as shipped): main's behaviour, unchanged (the CLI's frames are taken for 2, 2's own dropped)."""
    if verify:
        monkeypatch.setattr(claude, "VERIFIED", frozenset({"2.1.330-stand-in"}))
    host, s = real_host(tmp_path, monkeypatch, stand_in(), timeout_seconds=30, grace=2)
    try:
        first = host.send(SID, "bg 0.4 0.3 one")
        ends_of(host, [first])
        second = host.send(SID, "two")
        got = ends_of(host, [second])[second]
        assert got.text == ("turn 2: TWO" if verify else "background: FINISHED")
        if verify:
            assert s.provider.verified
            lines = transcript(tmp_path)
            inits = [x for x in lines if x.get("subtype") == "init"]
            assert [bool(x.get("user_message_uuids")) for x in inits] == [True, False, True]
            assert any(x.get("origin", {}).get("kind") == "task-notification" for x in lines)
    finally:
        host.stop_all()


def test_real_pipes_echo_fields_reach_the_transcript_and_every_line_carries_a_uuid(tmp_path, monkeypatch):
    """Part K's named change on the transcript: the echo fields on init and result, and the lifecycle lines, as the
    CLI writes them; every user line carries the uuid its frames echo."""
    host, s = real_host(tmp_path, monkeypatch, stand_in(), timeout_seconds=30, grace=2)
    try:
        ends_of(host, [host.send(SID, "hello")])
    finally:
        host.stop_all()
    lines = transcript(tmp_path)
    [init] = [x for x in lines if x.get("subtype") == "init"]
    [res] = [x for x in lines if x.get("type") == "result"]
    u = init["user_message_uuids"][0]
    assert res["user_message_uuid"] == u and res["user_message_uuids"] == [u]
    assert [x["state"] for x in lines if x.get("type") == "command_lifecycle"] == ["queued", "started", "completed"]


def test_real_pipes_a_refused_resume_fails_its_message_as_today(tmp_path, monkeypatch):
    monkeypatch.setattr(claude, "VERIFIED", frozenset({"2.1.330-stand-in"}))
    host, s = real_host(tmp_path, monkeypatch, stand_in(STAND_IN_REFUSE_RESUME="1"), timeout_seconds=30, grace=2)
    s.resume = True
    try:
        m = host.send(SID, "hello")
        got = ends_of(host, [m])[m]
        assert (got.ok, got.answered) == (False, True)
    finally:
        host.stop_all()


def test_real_pipes_zero_turn_notifications_and_a_reinit_settle_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(claude, "VERIFIED", frozenset({"2.1.330-stand-in"}))
    host, s = real_host(tmp_path, monkeypatch, stand_in(), timeout_seconds=30, grace=2)
    try:
        a = host.send(SID, "notify-in notify-after reinit one")
        assert ends_of(host, [a])[a].text == "turn 1: NOTIFY-IN NOTIFY-AFTER REINIT ONE"
        b = host.send(SID, "notify-error-in two")
        assert ends_of(host, [b])[b].text == "turn 2: NOTIFY-ERROR-IN TWO"
        assert s.provider.verified
    finally:
        host.stop_all()


def test_real_pipes_a_process_that_stops_echoing_falls_back_to_order(tmp_path, monkeypatch):
    monkeypatch.setattr(claude, "VERIFIED", frozenset({"2.1.330-stand-in"}))
    host, s = real_host(tmp_path, monkeypatch, stand_in(STAND_IN_ECHO="1"), timeout_seconds=30, grace=2)
    try:
        a = host.send(SID, "one")
        assert ends_of(host, [a])[a].text == "turn 1: ONE"
        b = host.send(SID, "two")
        # Its init echoes nothing either: an unattributed bracket, then a plain result (lost echoes): M's by order,
        # and a deviation, so the process is attributed by order from now on.
        assert ends_of(host, [b])[b].text == "turn 2: TWO"
        assert not s.provider.verified and s.provider.deviation
        c = host.send(SID, "three")
        assert ends_of(host, [c])[c].text == "turn 3: THREE"
    finally:
        host.stop_all()


def test_real_pipes_a_restart_interrupt_in_a_verified_process(tmp_path, monkeypatch):
    monkeypatch.setattr(claude, "VERIFIED", frozenset({"2.1.330-stand-in"}))
    host, s = real_host(tmp_path, monkeypatch, stand_in(), timeout_seconds=30, grace=2)
    try:
        a = host.send(SID, "sleep 20")
        deadline = time.monotonic() + 20
        while not s.provider.takes_interrupt() and time.monotonic() < deadline:
            host.tick()
            time.sleep(0.02)
        host.interrupt_for(SID, a, "b")
        got = ends_of(host, [a])[a]
        assert got.restarted_by == "b" and s.provider.verified
    finally:
        host.stop_all()


def test_the_modes_of_a_verified_process_are_still_restart_only():
    """No steering in step 1: `steers` stays False on Claude, and `steer` is refused exactly as on main."""
    assert ClaudeSession.steers is False and "steer" not in providers.modes(ClaudeSession())


# --- rows C's table leaves open (DESIGN_NOTES "Claude turn correlation", choices where C's table is silent)


@pytest.mark.usefixtures("verified")
def test_open_row_none_init_listing_only_settled_lines_is_unattributed():
    p, m = bracket("none")
    [old] = list(p._written - {m})
    assert fed(p, vinit(old)) == [("turn_started", "other")] and p._bracket == UNATTRIBUTED and p.holds_turns()


@pytest.mark.usefixtures("verified")
def test_open_row_unattributed_result_listing_only_settled_lines_closes_it_as_no_messages():
    p, m = bracket("unattributed")
    [old] = list(p._written - {m})
    assert fed(p, owned(old)) == [("turn_ended", "other")]
    assert p._bracket is None and not p.holds_turns() and p._current == m and p.verified


@pytest.mark.usefixtures("verified")
def test_open_row_none_result_listing_only_settled_lines_is_no_messages():
    p, m = bracket("none")
    [old] = list(p._written - {m})
    assert fed(p, owned(old)) == [("turn_ended", "other")] and p._current == m and p.verified


@pytest.mark.usefixtures("verified")
def test_open_row_none_plain_result_with_nothing_in_flight_is_a_deviation():
    p = idle_verified()
    assert fed(p, plain()) == [("turn_ended", "order")] and not p.verified, "by order: only logged, as main"


def test_lifecycle_lines_mean_nothing_to_the_adapter():
    """The real CLI writes `command_lifecycle` lines for lines with a uuid (the transcript gains them)."""
    p = adapter()
    u = write(p)
    for state in ("queued", "started", "completed", "cancelled"):
        assert p.feed(json.dumps({"type": "command_lifecycle", "command_uuid": u, "state": state,
                                  "session_id": SID}).encode()) == ([], [])
