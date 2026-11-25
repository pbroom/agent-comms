"""Claude turn correlation (DESIGN_NOTES "Claude turn correlation (2026-11-24)"; the steering design on branch
claude/steer-loose, its part C, step 1, with the review's rule changes): while some Claude Code version is VERIFIED,
host user lines carry a fresh uuid, and in a verified process incarnation the adapter attributes each `init` and
`result` by the evidence it carries (the bracket table), not by order. VERIFIED is {"2.1.330"} since the
switch-on; each test sets the VERIFIED it needs (`verified`, or VERIFIED empty, whose path is main's).

The frames are the shapes Claude Code 2.1.330 was measured to write (the steering design's part B and this change's
probes): an owned `init` lists the line's uuid in `user_message_uuids`; an owned `result` in `user_message_uuid` and
`user_message_uuids` and has no `origin`; a turn the CLI started itself has an `init` with no echo and a `result` with
no echo, `num_turns` 1 and `origin.kind` `task-notification`. Adapter rows from strings, the host with fake processes
(test_session_host), and the stand-in (tests/stand_in_claude.py) through real pipes. Never a real agent CLI."""

import json
import re
import time

import pytest

from agent_comms import providers
from agent_comms.providers import claude
from agent_comms.providers.claude import OWN, UNATTRIBUTED, ClaudeSession
from agent_comms.session_host import BINDING_CHANGED, INTERRUPTED, TURN_WITHDRAWN

from test_session_host import ROOT, SID, FakeSessionChild, henv, make_host, open_session, real_host, result  # noqa: F401

V = "2.1.330"                                       # the version the tests verify (the one in VERIFIED)
CAPS = ["msg_lifecycle_v1", "interrupt_receipt_v1"]
NOTIFY = {"kind": "task-notification", "producer": "session-task"}
UUID_KEY = re.compile(rb', "uuid": "[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}"\}\n$')


@pytest.fixture
def verified(monkeypatch):
    """A build in which version V passed the release gates (as this one ships, set again by the test)."""
    monkeypatch.setattr(claude, "VERIFIED", frozenset({V}))


# ------------------------------------------------------------ frames


def vinit(*uuids, version=V, caps=CAPS, mode="dontAsk") -> dict:
    """An `init`: owned when it lists uuids, unmarked (no `user_message_uuids` key at all, as measured) when not."""
    ev = {"type": "system", "subtype": "init", "session_id": SID, "permissionMode": mode, "tools": ["Read"]}
    ev |= ({"claude_code_version": version} if version is not None else {})
    ev |= ({"capabilities": list(caps)} if caps is not None else {})
    return ev | ({"user_message_uuids": list(uuids)} if uuids else {})


def owned(*uuids, field=None, **kw) -> dict:
    """An owned `result` echoing these lines (the first is the turn's own); `field`: only that one echo field."""
    echo = {"user_message_uuid": uuids[0], "user_message_uuids": list(uuids)}
    return result(**kw) | (echo if field is None else {field: echo[field]})


def plain(**kw) -> dict:
    """A `result` with no evidence: no echo, no origin."""
    return result(**kw)


def failed(**kw) -> dict:
    """A plain error `result` (what a turn that failed on its own might write with no echo)."""
    return result(subtype="error_during_execution", is_error=True, text="", **kw)


def foreign(text="background: FINISHED", error=False) -> dict:
    """The `result` of a turn the CLI started itself (bg-late, measured): no echo, num_turns 1, a notification."""
    return result(text=text, subtype="error_during_execution" if error else "success", is_error=error) | {
        "origin": NOTIFY}


def nonturn(error=False) -> dict:
    """A zero-turn task notification's `result`: no echo, num_turns 0, a notification origin."""
    return result(subtype="error_during_execution" if error else "success", is_error=error, text="") | {
        "num_turns": 0, "origin": NOTIFY}


def refusal() -> dict:
    """What a refused `--resume` writes before any `init` (measured on 2.1.330: an error, num_turns 0, no echo)."""
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


def write(p, text="go") -> str | None:
    """The host writes a turn: its uuid (None: the line has none)."""
    [data] = p.turn(text)
    return json.loads(data).get("uuid")


def raw(p, ev) -> tuple[list, list]:
    return p.feed(json.dumps(ev).encode())


def fed(p, ev) -> list[tuple[str, str]]:
    """(kind, turn) of every event one line means (nothing filtered out)."""
    events, replies = raw(p, ev)
    assert replies == []
    return [(e.kind, e.turn) for e in events]


def idle_verified() -> ClaudeSession:
    """A verified process after one ordinary turn: no bracket open, no turn in flight."""
    p = adapter()
    m0 = write(p)
    assert fed(p, vinit(m0)) == [("ready", "order"), ("turn_started", "own")] and p.verified
    assert fed(p, owned(m0)) == [("turn_ended", "own")]
    return p


def bracket(state: str, written=True, delivered=True, activity=True):
    """(adapter, M's uuid or None) with M written (unless not `written`; delivered, as the host confirms once every
    byte is written, unless not `delivered`) and the bracket in this state (an unattributed one with model activity
    seen in it unless not `activity`)."""
    p = idle_verified()
    m = write(p) if written else None
    if written and delivered:
        p.delivered()
    if state == "own":
        fed(p, vinit(m))
    elif state == "unattributed":
        fed(p, vinit())
        if activity:
            assert raw(p, {"type": "assistant", "session_id": SID, "message": {"content": []}}) == ([], [])
    assert p._bracket == {"none": None, "own": OWN, "unattributed": UNATTRIBUTED}[state]
    return p, m


def settled_uuid(p, m):
    [old] = list(p._written - {m})
    return old


# --- C1-05: a uuid only while verification is possible


def test_verified_is_2_1_330_only():
    """Switched on for Claude Code 2.1.330 after its release gates (M3, M6, M7 and review rounds 1 to 5); a version
    goes in only once its gates passed."""
    assert claude.VERIFIED == frozenset({"2.1.330"})


def test_another_version_is_attributed_by_order_and_its_lines_after_its_first_init_are_mains(monkeypatch):
    p = adapter()
    assert json.loads(p.turn("first")[0]).get("uuid"), "before the first init it may still be verified"
    assert fed(p, vinit(version="2.1.331")) == [("ready", "order"), ("turn_started", "order")]
    assert p.turn("hello\nthere") == [b'{"type": "user", "message": {"role": "user", "content": "hello\\nthere"}}\n']
    assert fed(p, foreign()) == [("turn_ended", "order")] and not p.verified and p.holds_turns() is False


def test_with_no_verifiable_version_lines_are_mains_bytes_and_attributed_by_order(monkeypatch):
    monkeypatch.setattr(claude, "VERIFIED", frozenset())
    p = adapter()
    assert p.turn("hello\nthere") == [b'{"type": "user", "message": {"role": "user", "content": "hello\\nthere"}}\n']
    assert fed(p, vinit()) == [("ready", "order"), ("turn_started", "order")]
    assert fed(p, foreign()) == [("turn_ended", "order")], "a foreign result: by order, as on main (the old bug)"
    assert write(p) is None and not p.verified and p.holds_turns() is False


@pytest.mark.usefixtures("verified")
def test_lines_carry_a_fresh_uuid_until_the_first_init_shows_the_process_cannot_be_verified():
    p = adapter()
    a, b = p.turn("same"), p.turn("same")
    for data in (a[0], b[0]):
        assert UUID_KEY.search(data) and data[:-len(UUID_KEY.search(data).group(0))] + b"}\n" == (
            b'{"type": "user", "message": {"role": "user", "content": "same"}}\n')
    assert json.loads(a[0])["uuid"] != json.loads(b[0])["uuid"]
    fed(p, vinit(version="2.1.999"))
    assert write(p) is None, "this incarnation cannot be verified: main's bytes from now on"
    q = idle_verified()
    assert write(q) is not None
    fed(q, plain())                         # a deviation
    assert write(q) is None, "nor after verification is lost"


def test_the_uuid_does_not_come_from_uuid4(monkeypatch):
    """tests/golden_schema_free.py makes uuid4 (and token_hex) deterministic from one counter."""
    import uuid
    monkeypatch.setattr(claude, "VERIFIED", frozenset({V}))
    monkeypatch.setattr(uuid, "uuid4", lambda: (_ for _ in ()).throw(AssertionError("uuid4 called")))
    assert write(adapter()) is not None


# --- the gate


@pytest.mark.usefixtures("verified")
def test_the_gate_is_a_verified_version_that_lists_msg_lifecycle_in_its_first_init():
    for init in (vinit(version="2.1.331"), vinit(caps=["interrupt_receipt_v1"]), vinit(version=None)):
        p = adapter()
        m = write(p)
        assert fed(p, init)[-1] == ("turn_started", "order") and not p.verified
        # Pinned: a qualifying init after a bad first one changes nothing.
        assert fed(p, vinit(m)) == [("turn_started", "order")] and not p.verified
    p = adapter()
    write(p)
    assert fed(p, vinit())[-1] == ("turn_started", "other") and p.verified


@pytest.mark.usefixtures("verified")
def test_verification_is_lost_for_good_at_the_first_deviation_and_a_new_process_is_asked_again():
    p, m = bracket("none")
    assert fed(p, plain()) == [("turn_ended", "order")] and not p.verified and p.deviation
    write(p)
    assert fed(p, vinit()) == [("turn_started", "order")] and fed(p, foreign()) == [("turn_ended", "order")]
    assert not p.verified
    p.argv(["claude", "-p", "{prompt}"], session_id=SID, resume=True, model=None)     # the next process
    m3 = write(p)
    assert fed(p, vinit(m3))[-1] == ("turn_started", "own") and p.verified


@pytest.mark.usefixtures("verified")
@pytest.mark.parametrize("field", ["user_message_uuid", "user_message_uuids"])
def test_each_echo_field_alone_is_evidence_with_m_after_an_older_uuid(field):
    p, m = bracket("own")
    old = settled_uuid(p, m)
    ev = result() | ({field: m} if field == "user_message_uuid" else {field: [old, m]})
    assert fed(p, ev) == [("turn_ended", "own")] and p.verified
    q, m2 = bracket("none")
    assert fed(q, vinit(settled_uuid(q, m2), m2))[-1] == ("turn_started", "own"), "M after an older uuid"


# --- the bracket table, row by row


@pytest.mark.usefixtures("verified")
@pytest.mark.parametrize("written", [True, False])
def test_row_7a_a_no_query_notification_closes_an_unattributed_bracket_with_no_model_activity(written):
    """An unmarked init and a SUCCESSFUL zero-turn notification with no model activity between them was a no-query
    notification (a background Agent's quota denial can be one): it ends the bracket; the barrier lifts."""
    p, _ = bracket("unattributed", written=written, activity=False)
    assert raw(p, nonturn()) == ([], [])
    assert p._bracket is None and not p.holds_turns() and p.verified


@pytest.mark.usefixtures("verified")
@pytest.mark.parametrize("frame", [
    {"type": "assistant", "session_id": SID, "message": {"content": [{"type": "text", "text": "x"}]}},
    {"type": "stream_event", "session_id": SID, "event": {}},
    {"type": "user", "session_id": SID, "message": {"content": [{"type": "tool_result"}]}},
    {"type": "system", "subtype": "thinking_tokens", "session_id": SID, "estimated_tokens": 5}])
def test_row_7a_after_any_model_activity_a_zero_turn_notification_stays_inert(frame):
    p, _ = bracket("unattributed", activity=False)
    raw(p, frame)
    assert raw(p, nonturn()) == ([], []) and p._bracket == UNATTRIBUTED and p.holds_turns()


@pytest.mark.usefixtures("verified")
def test_c3_03_7a_needs_subtype_success_is_error_false_and_zero_turns():
    for ev in (nonturn() | {"subtype": "error_during_execution"}, nonturn() | {"is_error": None},
               nonturn() | {"subtype": "error_max_turns", "is_error": False}):
        p, _ = bracket("unattributed", written=False, activity=False)
        assert raw(p, ev) == ([], []) and p._bracket == UNATTRIBUTED and p.holds_turns()


@pytest.mark.usefixtures("verified")
def test_row_7a_only_a_successful_one_and_not_a_replayed_line_or_a_status_frame():
    p, _ = bracket("unattributed", written=False, activity=False)
    raw(p, {"type": "user", "isReplay": True, "session_id": SID, "message": {"content": "go"}})
    raw(p, {"type": "system", "subtype": "status", "session_id": SID, "status": "compacting"})
    assert raw(p, nonturn(error=True)) == ([], []) and p._bracket == UNATTRIBUTED, "an error does not close it"
    assert raw(p, nonturn()) == ([], []) and p._bracket is None, "replay and status rows are no model activity"


@pytest.mark.usefixtures("verified")
@pytest.mark.parametrize("state", ["none", "own", "unattributed"])
def test_row_any_non_turn_success_is_inert_in_every_bracket(state):
    p, m = bracket(state)
    before = (p._bracket, p._current, p.holds_turns())
    assert raw(p, nonturn()) == ([], [])
    assert (p._bracket, p._current, p.holds_turns()) == before and p.verified


@pytest.mark.usefixtures("verified")
@pytest.mark.parametrize("state", ["none", "unattributed"])
def test_row_any_non_turn_error_with_nothing_in_flight_is_inert(state):
    """The non-turn predicate against a zero-turn ERROR result (part O): inert when nothing is in flight."""
    p, _ = bracket(state, written=False)
    before = (p._bracket, p.holds_turns())
    assert raw(p, nonturn(error=True)) == ([], []) and (p._bracket, p.holds_turns()) == before and p.verified


@pytest.mark.usefixtures("verified")
def test_row_none_owned_init_for_m_opens_ms_owned_bracket():
    p, m = bracket("none")
    assert fed(p, vinit(m)) == [("turn_started", "own")] and p._bracket == OWN and not p.holds_turns()


@pytest.mark.usefixtures("verified")
@pytest.mark.parametrize("m_written", [True, False])
def test_row_none_unmarked_init_opens_an_unattributed_bracket_and_the_barrier_holds(m_written):
    p, _ = bracket("none", written=m_written)
    assert fed(p, vinit()) == [("turn_started", "other")]
    assert p._bracket == UNATTRIBUTED and p.holds_turns() is True


@pytest.mark.usefixtures("verified")
@pytest.mark.parametrize("state", ["none", "unattributed"])
def test_row_an_init_listing_only_settled_lines_changes_no_bracket(state):
    """C1-03: a late init[A] after A settled opens no barrier against B, and closes none."""
    p, m = bracket(state)
    before = (p._bracket, p.holds_turns(), p._caps)
    assert fed(p, vinit(settled_uuid(p, m), caps=None, version=None)) == [("turn_started", "other")]
    assert (p._bracket, p.holds_turns(), p._caps) == before and p.verified, (
        "nothing of it is taken, not even missing capabilities or version (C1-03)")


@pytest.mark.usefixtures("verified")
@pytest.mark.parametrize("which", ["m", "unmarked", "settled", "no_version", "no_caps", "other_version"])
def test_row_owned_a_second_init_is_access_checked_only(which):
    """C1-04: inside M's owned bracket a re-init never replaces the bracket, never starts M again and replaces no
    capability, whatever it carries; one that no longer qualifies downgrades the process when the bracket closes."""
    p, m = bracket("own")
    init = {"m": vinit(m), "unmarked": vinit(), "settled": vinit(settled_uuid(p, m)), "no_version": vinit(version=None),
            "no_caps": vinit(caps=None), "other_version": vinit(m, version="2.1.999")}[which]
    assert fed(p, init) == [("turn_started", "other")]
    assert p._bracket == OWN and p._current == m and p.verified and p.restarts and p.takes_interrupt()
    assert p._competing is (which not in ("m", "other_version"))
    assert fed(p, foreign()) == [("turn_ended", "other")], "a foreign result still settles nothing"
    assert fed(p, owned(m)) == [("turn_ended", "own")]
    assert p.verified is (which in ("m", "unmarked", "settled")), "the downgrade comes only once the bracket closed"


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
@pytest.mark.parametrize("state", ["none", "own", "unattributed"])
def test_row_a_result_listing_only_settled_lines_changes_no_bracket(state):
    """C1-03: a late result[A] does not close a live CLI-started bracket (nor M's): logged, no message's."""
    p, m = bracket(state)
    before = (p._bracket, p.holds_turns(), p._current)
    assert fed(p, owned(settled_uuid(p, m))) == [("turn_ended", "other")]
    assert (p._bracket, p.holds_turns(), p._current) == before and p.verified


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
def test_row_unattributed_owned_init_for_m_makes_the_bracket_ms():
    p, m = bracket("unattributed")
    assert fed(p, vinit(m)) == [("turn_started", "own")] and p._bracket == OWN and p._competing


@pytest.mark.usefixtures("verified")
@pytest.mark.parametrize("m_written", [True, False])
def test_row_unattributed_plain_result_is_a_deviation_by_order(m_written):
    p, _ = bracket("unattributed", written=m_written)
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
def test_row_none_plain_result_with_nothing_in_flight_is_a_deviation():
    p, _ = bracket("none", written=False)
    assert fed(p, plain()) == [("turn_ended", "order")] and not p.verified, "by order: only logged, as main"


@pytest.mark.usefixtures("verified")
def test_row_none_foreign_result_is_the_clis_own():
    p, m = bracket("none")
    assert fed(p, foreign()) == [("turn_ended", "other")] and p.verified and p._current == m


@pytest.mark.usefixtures("verified")
def test_row_before_any_init_a_launch_refusal_fails_the_message_as_today():
    p = adapter()
    write(p)
    [ev], _ = raw(p, refusal())
    assert (ev.kind, ev.turn, ev.ok, ev.answered) == ("turn_ended", "order", False, True)


# --- rule 10 (the review's departure from C): an error result that does not list M, while M is in flight


@pytest.mark.usefixtures("verified")
@pytest.mark.parametrize("state", ["none", "own", "unattributed"])
@pytest.mark.parametrize("frame", ["plain", "zero_turn", "foreign", "settled"])
def test_rule_an_error_result_not_listing_m_while_m_is_in_flight_is_ms_by_order(state, frame):
    """Before the non-turn and foreign classes, in every bracket: M's by order, exactly as main settles it, and the
    incarnation is attributed by order from then on."""
    p, m = bracket(state)
    ev = {"plain": failed(), "zero_turn": nonturn(error=True), "foreign": foreign(error=True),
          "settled": owned(settled_uuid(p, m), subtype="error_during_execution", is_error=True)}[frame]
    assert fed(p, ev) == [("turn_ended", "order")]
    assert not p.verified and p.deviation and not p.holds_turns() and p._current is None


@pytest.mark.usefixtures("verified")
@pytest.mark.parametrize("state", ["none", "unattributed"])
@pytest.mark.parametrize("frame", ["plain", "zero_turn", "settled"])
def test_c2_01_an_error_while_m_is_staged_or_partly_written_settles_nothing_and_keeps_the_bracket(state, frame):
    """Zero-turn and ambiguous errors stay inert while M is undelivered (C2-01, kept in round 3)."""
    p, m = bracket(state, delivered=False)
    before = (p._bracket, p.holds_turns(), p._current)
    ev = {"plain": failed(), "zero_turn": nonturn(error=True),
          "settled": owned(settled_uuid(p, m), subtype="error_during_execution", is_error=True)}[frame]
    assert raw(p, ev) == ([], [])
    assert (p._bracket, p.holds_turns(), p._current) == before and p.verified


@pytest.mark.usefixtures("verified")
@pytest.mark.parametrize("state", ["none", "unattributed"])
@pytest.mark.parametrize("error", [False, True])
def test_c3_01_foreign_terminal_evidence_ends_the_clis_turn_while_m_is_undelivered(state, error):
    """A result with a non-human origin and one or more turns is the CLI's own turn ending, a success or an error:
    it closes the unattributed bracket (the barrier lifts), settles nothing of M's, keeps verification."""
    p, m = bracket(state, delivered=False)
    assert fed(p, foreign(error=error)) == [("turn_ended", "other")]
    assert p._bracket is None and not p.holds_turns() and p._current == m and p.verified


@pytest.mark.usefixtures("verified")
def test_rule_an_error_with_nothing_in_flight_settles_nothing_as_before():
    p, _ = bracket("unattributed", written=False)
    assert fed(p, foreign(error=True)) == [("turn_ended", "other")] and p.verified and not p.holds_turns()


@pytest.mark.usefixtures("verified")
def test_rule_an_interrupts_aborted_result_lists_its_line_and_is_ms_by_evidence():
    p, m = bracket("own")
    assert fed(p, aborted(m)) == [("turn_ended", "own")] and p.verified


@pytest.mark.usefixtures("verified")
def test_interrupt_readiness_needs_ms_owned_bracket():
    p, m = bracket("unattributed")
    assert p.restarts and not p.takes_interrupt(), "not during a turn the CLI started, though M is in flight"
    fed(p, foreign())
    assert not p.takes_interrupt()
    fed(p, vinit(m))
    assert p.takes_interrupt()
    q = adapter()                       # unverified (as main): any init of the turn makes it ready
    write(q)
    raw(q, vinit(version="9"))
    assert q.takes_interrupt()


def test_lifecycle_and_replayed_user_lines_mean_nothing_to_the_adapter(monkeypatch):
    """The real CLI writes `command_lifecycle` lines for lines with a uuid, and with --replay-user-messages each line
    back as a `user` frame (`isReplay`, measured): no event, no reply, in either kind of process."""
    for verified in (frozenset(), frozenset({V})):
        monkeypatch.setattr(claude, "VERIFIED", verified)
        p = adapter()
        u = write(p) or "1e64c1ac-3dc2-45ff-bad6-ee954a1892a1"
        for state in ("queued", "started", "completed", "cancelled"):
            assert raw(p, {"type": "command_lifecycle", "command_uuid": u, "state": state, "session_id": SID,
                           "uuid": "0d1d6a3c-7f61-4b47-9c8e-2a1b3c4d5e6f"}) == ([], [])
        assert raw(p, {"type": "user", "message": {"role": "user", "content": "go"}, "isReplay": True,
                       "parent_tool_use_id": None, "session_id": SID, "uuid": u}) == ([], [])


# ------------------------------------------------------------ the host, with fake processes


def uuid_of(child, k=-1) -> str | None:
    users = [json.loads(x) for x in child.stdin.split(b"\n") if x and json.loads(x).get("type") == "user"]
    return users[k].get("uuid")


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


# --- the ordinary sequences part C checks, each settling M once with none stranded, and rule 10's


@pytest.mark.usefixtures("verified")
def test_seq_owned_init_then_owned_result(henv):
    s, child = idle_host(henv)
    _, _, m = started(henv)
    child.emit(vinit(m), owned(m, text="mine"))
    evs = henv.host.tick()
    assert [(e.kind, e.message_id) for e in evs] == [("turn_started", "1"), ("turn_ended", "1")]
    assert ends(evs, "1")[0].text == "mine" and ends(evs, "1")[0].ok and not s.busy


@pytest.mark.usefixtures("verified")
def test_seq_owned_init_then_a_plain_failure_result_is_ms(henv):
    """Rule 10 applies too (an error not listing M): M's by order, the same outcome; by order from then on."""
    s, child = idle_host(henv)
    _, _, m = started(henv)
    child.emit(vinit(m), plain(subtype="error_during_execution", is_error=True, text="broke"))
    [st, end] = henv.host.tick()
    assert (end.message_id, end.ok, end.text, end.answered) == ("1", False, "broke", True)
    assert not s.provider.verified


@pytest.mark.usefixtures("verified")
def test_seq_owned_init_a_zero_turn_notification_then_owned_result(henv):
    s, child = idle_host(henv)
    _, _, m = started(henv)
    assert emit(henv, child, vinit(m), nonturn()) == [("turn_started", "1")]
    child.emit(owned(m, text="mine"))
    [end] = henv.host.tick()
    assert (end.kind, end.message_id, end.text) == ("turn_ended", "1", "mine")


@pytest.mark.usefixtures("verified")
@pytest.mark.parametrize("frame", ["zero_turn_error", "foreign_error"])
def test_seq_owned_bracket_then_an_error_that_does_not_list_m_settles_m_at_once(henv, frame):
    s, child = idle_host(henv)
    _, _, m = started(henv)
    child.emit(vinit(m), nonturn(error=True) if frame == "zero_turn_error" else foreign(error=True))
    evs = henv.host.tick()
    assert [(e.kind, e.message_id, e.ok) for e in evs] == [("turn_started", "1", None), ("turn_ended", "1", False)]
    assert not s.busy and not s.provider.verified


@pytest.mark.usefixtures("verified")
def test_seq_unattributed_bracket_then_an_error_result_with_m_in_flight(henv):
    """Sol's case: a CLI-started turn consumed M and failed terminally without M's uuid, keeping its notification
    origin: M settles at once with that failure, as on main, and the next message proceeds."""
    s, child = idle_host(henv)
    _, _, m = started(henv)
    assert emit(henv, child, vinit()) == []
    henv.host.send(SID, "next", message_id="2")
    child.emit(foreign(error=True, text="usage limit reached"))
    evs = henv.host.tick()
    assert [(e.kind, e.message_id) for e in evs] == [("turn_ended", "1"), ("delivered", "2")]
    assert (ends(evs, "1")[0].ok, ends(evs, "1")[0].text) == (False, "usage limit reached")
    assert not s.provider.verified


@pytest.mark.usefixtures("verified")
def test_seq_an_error_result_with_nothing_in_flight_settles_nothing(henv):
    s, child = idle_host(henv)
    assert emit(henv, child, vinit(), foreign(error=True)) == []
    assert emit(henv, child, nonturn(error=True)) == [] and s.provider.verified
    _, _, m = started(henv)
    child.emit(vinit(m), owned(m, text="mine"))
    assert [e.text for e in henv.host.tick() if e.kind == "turn_ended"] == ["mine"]


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


def test_seq_unverified_an_unattributed_turn_before_ms_own_pair_is_mains(henv, monkeypatch):
    """VERIFIED empty: the same frames are attributed by order, exactly as on main."""
    monkeypatch.setattr(claude, "VERIFIED", frozenset())
    s, child, m0 = started(henv, "zero", "0")
    assert m0 is None
    emit(henv, child, vinit(), plain())
    started(henv)
    child.emit(vinit(), foreign())
    evs = henv.host.tick()
    assert [(e.kind, e.message_id) for e in evs] == [("turn_started", "1"), ("turn_ended", "1")]
    assert ends(evs, "1")[0].text == "background: FINISHED", "main's behaviour, unchanged while VERIFIED is empty"
    child.emit(vinit(), plain(text="mine"))
    assert henv.host.tick() == []


@pytest.mark.usefixtures("verified")
def test_seq_unmarked_init_then_plain_result_with_m_in_flight_lost_echoes(henv):
    s, child = idle_host(henv)
    started(henv)
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
    assert s.provider.verified, "an aborted result lists its line: unaffected by rule 10"


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
    """Part O (R6-04): an unmarked init, then M's own init, then a plain result: the evidence makes the bracket M's;
    the competing init is noted; the plain result is M's, settled once; no deviation."""
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


@pytest.mark.usefixtures("verified")
def test_a_late_init_of_a_settled_line_opens_no_barrier_against_the_next_message(henv):
    s, child, a = started(henv, "a", "a")
    emit(henv, child, vinit(a), owned(a))
    assert emit(henv, child, vinit(a)) == []
    henv.host.send(SID, "b", message_id="b")
    assert [(e.kind, e.message_id) for e in henv.host.tick()] == [("delivered", "b")]


@pytest.mark.usefixtures("verified")
def test_a_late_result_of_a_settled_line_does_not_release_a_cli_started_bracket(henv):
    s, child, a = started(henv, "a", "a")
    emit(henv, child, vinit(a), owned(a))
    emit(henv, child, vinit())
    henv.host.send(SID, "b", message_id="b")
    assert emit(henv, child, owned(a)) == [] and s.provider.holds_turns() and b'"b"' not in child.stdin


class _ZeroPipe(FakeSessionChild):
    """A process whose stdin takes nothing until the test says so (`accept`)."""

    def __init__(self, pid, exits_on_terminate=True):
        super().__init__(pid, exits_on_terminate)
        self.accept = 0


@pytest.mark.usefixtures("verified")
@pytest.mark.parametrize("activity", [True, False])
def test_c2_01_sols_sequence_an_error_never_lets_a_staged_m_into_the_clis_turn(henv, activity):
    """Zero-byte write, then an unmarked init (with or without model activity), then a zero-turn notification
    ERROR: the error is not M's (M is not delivered), it does not end the bracket, and M is not written."""
    s, child = idle_host(henv)
    child.accept = 0
    henv.host.send(SID, "next", message_id="1")
    henv.host.tick()
    child.accept = None
    frames = [vinit()] + ([{"type": "assistant", "session_id": SID, "message": {"content": []}}] if activity else [])
    assert emit(henv, child, *frames, nonturn(error=True)) == []
    assert b'"next"' not in child.stdin and s.writing is None and [m.id for m in s.inbox] == ["1"], (
        "back in the inbox: a message is never held while staged")
    assert s.provider.verified and s.provider.holds_turns() and s.provider._current is None
    assert emit(henv, child, nonturn(error=True)) == [] and b'"next"' not in child.stdin, "still inert"
    assert emit(henv, child, foreign()) == [("delivered", "1")], "the CLI's turn really ended: now it goes"
    valid_stdin(child, once=["next"])


@pytest.mark.usefixtures("verified")
def test_c2_01_rule_10_needs_a_delivered_m(henv):
    """The same error once M is fully written: M's by order, as main settles it."""
    s, child = idle_host(henv)
    _, _, m = started(henv)
    emit(henv, child, vinit())
    child.emit(nonturn(error=True))
    [end] = henv.host.tick()
    assert (end.message_id, end.ok) == ("1", False) and not s.provider.verified


@pytest.mark.usefixtures("verified")
def test_7a_sols_gate_sequence_a_no_query_notification_then_m_is_written_and_settles(henv):
    """2.1.330's conditional path: a background Agent's quota denial becomes a no-query notification, an unmarked
    init and a SUCCESSFUL zero-turn task-notification result while the host is idle. The bracket closes, so M is
    written at once and settles by its own frames."""
    s, child = idle_host(henv)
    assert emit(henv, child, vinit(), nonturn()) == [] and not s.provider.holds_turns()
    henv.host.send(SID, "next", message_id="1")
    assert [(e.kind, e.message_id) for e in henv.host.tick()] == [("delivered", "1")]
    m = uuid_of(child)
    child.emit(vinit(m), owned(m, text="mine"))
    assert [(e.kind, e.message_id, e.text) for e in henv.host.tick()] == [
        ("turn_started", "1", ""), ("turn_ended", "1", "mine")]


@pytest.mark.usefixtures("verified")
def test_7b_the_same_sequence_with_7a_disabled_delivers_m_after_the_deadline(henv, monkeypatch):
    """The backstop: the bracket never closes (7a off in this test), the deadline passes, the process is stopped and
    M, never written, goes to the next process, which writes it once and settles it."""
    monkeypatch.setattr(claude, "model_activity", lambda ev: True)      # every frame counts as activity: no 7a
    s, child = idle_host(henv)
    emit(henv, child, {"type": "system", "subtype": "status", "session_id": SID})
    assert emit(henv, child, vinit(), nonturn()) == [] and s.provider.holds_turns()
    henv.host.send(SID, "next", message_id="1")
    assert henv.host.tick() == []
    henv.clock.advance(30 * 60)
    assert henv.host.tick() == [] and child.terminated == 1 and b'"next"' not in child.stdin
    evs = henv.host.tick()
    fresh = henv.spawner.children[-1]
    assert ("delivered", "1") in [(e.kind, e.message_id) for e in evs] and fresh.turns() == ["next"]
    m = uuid_of(fresh)
    fresh.emit(vinit(m), owned(m, text="mine"))
    assert [(e.kind, e.message_id, e.text) for e in henv.host.tick()] == [
        ("turn_started", "1", ""), ("turn_ended", "1", "mine")]
    assert sum(c.stdin.count(b'"next"') for c in henv.spawner.children) == 1, "written once, ever"


@pytest.mark.usefixtures("verified")
def test_c3_01_sols_sequence_a_foreign_error_lifts_the_barrier_for_a_staged_m(henv):
    """M staged at zero bytes, an unmarked init, then an error result with a task-notification origin and
    num_turns 1: the CLI's own turn ended, so the barrier lifts and M is written at once (main would write it as
    soon as the pipe took it), exactly once, and settles by its own frames; no restart."""
    s, child = idle_host(henv)
    child.accept = 0
    henv.host.send(SID, "next", message_id="1")
    henv.host.tick()
    child.accept = None
    assert emit(henv, child, vinit()) == [] and b'"next"' not in child.stdin
    assert emit(henv, child, foreign(error=True, text="usage limit")) == [("delivered", "1")]
    m = uuid_of(child)
    child.emit(vinit(m), owned(m, text="mine"))
    assert [(e.kind, e.message_id, e.text) for e in henv.host.tick()] == [
        ("turn_started", "1", ""), ("turn_ended", "1", "mine")]
    assert child.stdin.count(b'"next"') == 1 and len(henv.spawner.children) == 1 and s.provider.verified
    valid_stdin(child, once=["next"])


@pytest.mark.usefixtures("verified")
def test_gate_shape_1_a_no_query_notification_then_m_written_and_settled_without_waiting(henv):
    """Quota shape (i), native-shaped (probe runs): the idle host, an unmarked init, a SUCCESSFUL zero-turn
    task-notification result; then M: written at once (no hold interval: the clock never moves), exactly once,
    settled by its own frames, no process restart."""
    s, child = idle_host(henv)
    child.emit(vinit() | {"uuid": "9a1b2c3d-0000-4000-8000-000000000001"},
               {"type": "system", "subtype": "task_notification", "session_id": SID, "task_id": "t1",
                "status": "failed", "uuid": "9a1b2c3d-0000-4000-8000-000000000002"},
               nonturn() | {"duration_ms": 3, "uuid": "9a1b2c3d-0000-4000-8000-000000000003"})
    assert henv.host.tick() == [] and not s.provider.holds_turns()
    henv.host.send(SID, "next", message_id="1")
    assert [(e.kind, e.message_id) for e in henv.host.tick()] == [("delivered", "1")]
    m = uuid_of(child)
    child.emit({"type": "command_lifecycle", "command_uuid": m, "state": "started", "session_id": SID},
               vinit(m), {"type": "assistant", "session_id": SID, "message": {"content": [{"type": "text"}]},
                          "user_message_uuid": m, "user_message_uuids": [m]}, owned(m, text="mine"))
    assert [(e.kind, e.message_id, e.text) for e in henv.host.tick()] == [
        ("turn_started", "1", ""), ("turn_ended", "1", "mine")]
    assert child.stdin.count(b'"next"') == 1 and len(henv.spawner.children) == 1 and child.terminated == 0
    valid_stdin(child, once=["next"])


@pytest.mark.usefixtures("verified")
@pytest.mark.parametrize("m_is", ["staged", "arriving"])
def test_gate_shape_2_a_foreign_quota_error_after_model_activity_then_m_written_and_settled(henv, m_is):
    """Quota shape (ii): M staged at zero bytes, or arriving while the CLI's turn runs; an unmarked init, model
    activity, then a terminal quota error with a task-notification origin and a positive turn count; then M:
    written at once, exactly once, settled by its own frames, no process restart."""
    s, child = idle_host(henv)
    if m_is == "staged":
        child.accept = 0
        henv.host.send(SID, "next", message_id="1")
        henv.host.tick()
        child.accept = None
    child.emit(vinit(), {"type": "assistant", "session_id": SID, "parent_tool_use_id": None,
                         "message": {"content": [{"type": "text", "text": "checking"}]}, "error": "rate_limit"},
               {"type": "system", "subtype": "thinking_tokens", "session_id": SID, "estimated_tokens": 9})
    henv.host.tick()
    if m_is == "arriving":
        henv.host.send(SID, "next", message_id="1")
        assert henv.host.tick() == [] and s.provider.holds_turns()
    child.emit(foreign(error=True, text="You've hit your limit") | {"subtype": "error_during_execution",
                                                                     "terminal_reason": "api_error"})
    assert [(e.kind, e.message_id) for e in henv.host.tick()] == [("delivered", "1")]
    m = uuid_of(child)
    child.emit(vinit(m), owned(m, text="mine"))
    assert [(e.kind, e.message_id, e.text) for e in henv.host.tick()] == [
        ("turn_started", "1", ""), ("turn_ended", "1", "mine")]
    assert child.stdin.count(b'"next"') == 1 and len(henv.spawner.children) == 1 and child.terminated == 0
    valid_stdin(child, once=["next"])


# --- review round 4: the inbox owns the hold (a message is never held while staged)


def valid_stdin(*children, once=()):
    """Every line written to these processes is a whole JSON object (nothing appended to an unfinished one), and
    each text in `once` was written as exactly one user line, across them all."""
    texts = []
    for child in children:
        assert child.stdin == b"" or child.stdin.endswith(b"\n"), "only whole lines"
        for line in child.stdin.split(b"\n")[:-1]:
            ev = json.loads(line)
            assert isinstance(ev, dict) and ev.get("type") in ("user", "control_request"), line
            if ev["type"] == "user":
                texts.append(ev["message"]["content"])
    for text in once:
        assert texts.count(text) == 1, (text, texts)


def settled(events, mid):
    return [e for e in events if e.kind == "turn_ended" and e.message_id == mid]


def staged_zero(henv, text="next", mid="1"):
    """A verified idle session; message `mid` staged, the pipe taking nothing."""
    s, child = idle_host(henv)
    child.accept = 0
    henv.host.send(SID, text, message_id=mid)
    assert henv.host.tick() == [] and s.writing.id == mid and s.turn_at is not None
    return s, child


def short_host(henv, monkeypatch):
    monkeypatch.setattr(claude, "VERIFIED", frozenset({V}))
    henv.host = make_host(henv, timeout_minutes=1)


def test_r4_a_sols_timing_m_goes_back_to_the_inbox_and_is_staged_afresh(henv, monkeypatch):
    """Timeout 60 s: staged at t=0 with zero bytes; the CLI's turn opens at t=45, so M goes back to the inbox (no
    trace in the adapter); a plain result at t=95 downgrades the incarnation. Follow-up (counted time) edited the
    end: the pass at t=95 first sees the turn ended, so the 50 s since the pass at t=45 count nothing (an interval
    counts only if M was held back by the agent at both its ends): M has 45 s, and the downgraded process writes it,
    once; it settles once by its own result. It never fails, nothing is restarted."""
    short_host(henv, monkeypatch)
    s, child = staged_zero(henv)
    staged_id = json.loads(bytes(s.outbuf[s.turn_at:]))["uuid"]
    henv.clock.advance(45)
    assert emit(henv, child, vinit()) == [] and s.writing is None and [m.id for m in s.inbox] == ["1"]
    assert s.busy_since is None and s.provider._current is None and not s.provider._open
    assert staged_id not in s.provider._written, "no uuid outstanding for it"
    child.accept = None
    henv.clock.advance(50)                              # t=95
    assert emit(henv, child, plain()) == [("delivered", "1")] and not s.provider.verified
    assert child.terminated == 0 and child.turns()[-1] == "next" and not s.in_flight.recovered
    child.emit(plain(text="mine"))
    evs = henv.host.tick()
    assert [(e.kind, e.message_id, e.text) for e in settled(evs, "1")] == [("turn_ended", "1", "mine")]
    valid_stdin(child, once=["next"])


def test_r4_a_an_init_first_read_on_the_old_deadline_unstages_before_any_timeout(henv, monkeypatch):
    """Review round 5 (C5-01) edited this replay: M's one no-progress budget began at its staging (t=0), so at t=60,
    when the init is first read, M is unstaged and its budget is spent: the 7b restart, not a failure (and not the
    t=120 of round 4, which gave the inbox a fresh interval). Its second budget starts at that restart (review
    round 7, C7-02: was "the next process gives it one fresh budget")."""
    short_host(henv, monkeypatch)
    s, child = staged_zero(henv)
    henv.clock.advance(60)                              # the old write deadline, and only now the CLI's init is read
    child.emit(vinit())
    evs = henv.host.tick()
    assert not settled(evs, "1") and child.terminated == 1 and [m.id for m in s.inbox] == ["1"], "7b at t=60"
    assert s.inbox[0].recovered and s.inbox[0].counted == 0.0 and s.inbox[0].counted_at is None
    assert b'"next"' not in child.stdin


@pytest.mark.usefixtures("verified")
def test_r4_b_a_binding_change_and_the_turns_end_in_one_pass_take_m_to_the_newly_bound_process(henv):
    s, child = staged_zero(henv)
    assert emit(henv, child, vinit()) == [] and [m.id for m in s.inbox] == ["1"]
    s.bind = "b" * 64
    child.emit(foreign())                               # the CLI's turn ends in the same pass as the binding change
    evs = henv.host.tick()
    assert not settled(evs, "1") and child.terminated == 1 and [m.id for m in s.inbox] == ["1"], "not failed"
    henv.host.tick()
    fresh = henv.spawner.children[-1]
    call = henv.spawner.calls[-1]
    assert fresh is not child and call["env"].get("AGENT_COMMS_HOSTED_KEY") == "b" * 64
    assert fresh.turns() == ["next"] and s.in_flight.id == "1"
    valid_stdin(child, fresh, once=["next"])


@pytest.mark.usefixtures("verified")
def test_r4_c_the_rest_of_a_partly_written_control_frame_stays_and_m_follows_as_its_own_line(henv):
    """A partly written interrupt control frame is in the buffer, M staged behind it with zero bytes; the CLI's turn
    opens (M goes back: only its own bytes leave the buffer); the binding changes and reverts before any restart:
    the control frame completes intact, M follows as its own line, and no `delivered` for M before its bytes."""
    s, child = idle_host(henv)
    _, _, m0 = started(henv, "zero-two", "a")
    emit(henv, child, vinit(m0))
    henv.host.interrupt_for(SID, "a", "b")
    child.accept = 10
    henv.host.tick()                                    # 10 bytes of the interrupt's line in the pipe
    child.accept = 0
    child.emit(owned(m0))                               # the turn ends by itself; the interrupt is half written
    assert [e.kind for e in henv.host.tick()] == ["turn_ended"]
    henv.host.send(SID, "next", message_id="1")
    assert henv.host.tick() == [] and s.writing.id == "1"
    rest = bytes(s.outbuf[:s.turn_at])
    child.emit(vinit())
    evs = henv.host.tick()
    assert not [e for e in evs if e.kind == "delivered"] and bytes(s.outbuf) == rest, "only M's bytes left the buffer"
    s.bind = "c" * 64
    henv.host.tick()
    s.bind = None                                       # it reverts before any restart
    child.accept = None
    evs = emit(henv, child, foreign())
    assert ("delivered", "1") in evs and child.terminated == 0
    valid_stdin(child, once=["next"])
    lines = [json.loads(x) for x in child.stdin.split(b"\n")[:-1]]
    assert [x["type"] for x in lines][-2:] == ["control_request", "user"] and lines[-2]["request"]["subtype"] == (
        "interrupt")


@pytest.mark.usefixtures("verified")
def test_r4_d_a_replacement_process_whose_pipe_takes_nothing_gets_no_extra_interval(henv):
    """After 7b the next process's pipe takes nothing and it, too, begins a turn of its own: M goes back to the
    inbox at once, and its (second) hold deadline is 30 minutes from there, not later; then main's failure."""
    s, child = idle_host(henv)
    emit(henv, child, vinit())
    henv.host.send(SID, "next", message_id="1")
    henv.host.tick()
    henv.clock.advance(30 * 60)
    assert henv.host.tick() == [] and child.terminated == 1, "7b, the first time"
    henv.spawner.child = _ZeroPipe
    henv.host.tick()
    fresh = henv.spawner.children[-1]
    assert s.writing.id == "1" and fresh.stdin == b""
    fresh.emit(vinit())
    assert henv.host.tick() == [] and s.writing is None and [m.id for m in s.inbox] == ["1"]
    henv.clock.advance(30 * 60 - 1)
    assert henv.host.tick() == [] and fresh.terminated == 0
    henv.clock.advance(1)
    [end] = henv.host.tick()
    assert (end.message_id, end.delivered, end.ok) == ("1", False, False) and fresh.terminated == 1
    valid_stdin(child, fresh)


@pytest.mark.usefixtures("verified")
def test_r4_d_a_staged_m_with_no_bracket_is_governed_by_its_budget(henv):
    """Review round 6 edited this replay (it was `..._keeps_mains_write_timer`): staged in a process that can be
    verified, M has a budget from that staging, and the budget governs it, not the write timer: a pipe that takes
    nothing for a whole turn timeout is the 7b restart (never written, so it cannot arrive twice); the next process
    gives it one fresh budget, and a second one spent fails it. Main fails it at the first timeout."""
    s, child = staged_zero(henv)
    henv.spawner.child = _ZeroPipe                      # the next process's pipe takes nothing either
    henv.clock.advance(30 * 60)
    evs = henv.host.tick()                              # 7b; the next process at the next pass (follow-up: a pass
    assert not settled(evs, "1") and child.terminated == 1      # whose budget acted writes nothing else)
    henv.host.tick()
    fresh = henv.spawner.children[-1]
    assert fresh is not child and s.writing.id == "1" and s.writing.counted == 0.0
    henv.clock.advance(30 * 60)
    [end] = settled(henv.host.tick(), "1")
    assert (end.delivered, end.ok) == (False, False)
    valid_stdin(child, fresh)


@pytest.mark.usefixtures("verified")
def test_r4_f_fifo_an_unstaged_m_goes_first_ahead_of_its_successor(henv):
    s, child = staged_zero(henv)
    henv.host.send(SID, "after", message_id="2")
    child.accept = None
    assert emit(henv, child, vinit()) == [] and [m.id for m in s.inbox] == ["1", "2"]
    assert emit(henv, child, foreign()) == [("delivered", "1")]
    m = uuid_of(child)
    child.emit(vinit(m), owned(m, text="one"))
    evs = henv.host.tick()
    assert [(e.kind, e.message_id) for e in evs] == [("turn_started", "1"), ("turn_ended", "1"), ("delivered", "2")]
    valid_stdin(child, once=["next", "after"])
    users = [json.loads(x)["message"]["content"] for x in child.stdin.split(b"\n")[:-1]
             if json.loads(x)["type"] == "user"]
    assert users[-2:] == ["next", "after"]


@pytest.mark.usefixtures("verified")
def test_r4_f_a_cancel_while_an_unstaged_m_waits_in_the_inbox(henv):
    s, child = staged_zero(henv)
    henv.host.send(SID, "after", message_id="2")
    child.accept = None
    emit(henv, child, vinit())
    [end] = henv.host.cancel(SID, "1", "cancelled")
    assert (end.message_id, end.delivered) == ("1", False) and [m.id for m in s.inbox] == ["2"]
    assert child.terminated == 0, "it was in the inbox: nothing to stop"
    assert emit(henv, child, foreign()) == [("delivered", "2")]
    valid_stdin(child, once=["after"])
    assert b'"next"' not in child.stdin


@pytest.mark.usefixtures("verified")
def test_r4_f_a_restart_into_an_unstaged_m_interrupts_nothing(henv):
    """The restart interlock: a restart asked for the turn of M, which is back in the inbox, writes no interrupt
    (nothing of M is in flight); M runs later as an ordinary turn."""
    s, child = staged_zero(henv)
    child.accept = None
    emit(henv, child, vinit())
    henv.host.interrupt_for(SID, "1", "2")
    assert henv.host.tick() == [] and b"interrupt" not in child.stdin and s.restart_by is None
    assert emit(henv, child, foreign()) == [("delivered", "1")]
    valid_stdin(child, once=["next"])


@pytest.mark.usefixtures("verified")
def test_r4_f_7b_for_an_unstaged_m_and_its_count_survives_the_unstage(henv):
    s, child = staged_zero(henv)
    emit(henv, child, vinit())                          # unstaged: the inbox's hold from now
    henv.clock.advance(30 * 60)
    assert henv.host.tick() == [] and child.terminated == 1 and s.inbox[0].recovered, "7b, once"
    henv.spawner.child = _ZeroPipe
    henv.host.tick()
    fresh = henv.spawner.children[-1]
    fresh.emit(vinit())
    henv.host.tick()                                    # unstaged again
    henv.clock.advance(30 * 60)
    [end] = henv.host.tick()
    assert (end.message_id, end.delivered) == ("1", False), "the second time: main's failure"
    valid_stdin(child, fresh)


def test_r4_g_parity_no_unstage_with_verified_empty_or_for_codex(henv, monkeypatch):
    from agent_comms.session_host import SessionHost
    monkeypatch.setattr(SessionHost, "_unstage", lambda *a: (_ for _ in ()).throw(AssertionError("unstaged")))
    s, child, _ = started(henv, "zero", "0")            # VERIFIED empty: by order, as on main
    emit(henv, child, vinit(), plain())
    child.accept = 0
    henv.host.send(SID, "next", message_id="1")
    henv.host.tick()
    emit(henv, child, vinit(), foreign())
    assert s.turn_at is None
    from test_session_host import codex_host, codex_ready
    import test_providers_codex as cx
    host, cs = codex_host(henv)
    host.send(SID, "first", message_id="c")
    host.tick()
    cchild = henv.spawner.children[-1]
    codex_ready(host, cchild)
    host.tick()
    cchild.emit(cx.reply(3, cx.TURN_REPLY), cx.TURN_STARTED, cx.message("one"), cx.completed())
    host.tick()
    cchild.accept = 0
    host.send(SID, "second", message_id="d")
    host.tick()
    cchild.emit(cx.own_turn("turn/started"))
    host.tick()
    host.tick()
    assert cs.turn_at is None


# --- review round 5: one no-progress budget per message (C5-01), exact-bytes unstage (R-1)


def test_c5_01_sols_alternating_sequence_is_bounded_at_two_budgets_with_nothing_written(henv, monkeypatch):
    """Sol's sequence: timeout 60 s, M staged at t=0 (the pipe takes nothing), the CLI's own turn from t=45 to t=90,
    then again, and again. M's one budget runs from t=0 across unstages, restagings and the brackets' ends: at t=60
    the 7b restart (M first in the inbox, nothing failed); the next process, its pipe taking nothing too and its
    turns of its own alternating the same way, gives M ONE fresh budget, from its staging: spent at t=120+, and M
    fails as main's `_held` fails it. Not one byte of M ever reached a process; every stdin line is whole."""
    short_host(henv, monkeypatch)
    t0 = henv.clock()
    s, child = staged_zero(henv)

    def cycle(proc, start):
        """From `start`: the CLI's turn opens 45 s later and closes at 90 s; ticks every 5 s, until a stop or 120 s."""
        events = []
        while henv.clock() < start + 120 and proc.terminated == 0:
            henv.clock.advance(5)
            k = henv.clock() - start
            if k % 90 == 45:
                proc.emit(vinit())
            elif k % 90 == 0:
                proc.emit(foreign())
            events += henv.host.tick()
        return events

    evs = cycle(child, t0)
    assert child.terminated == 1 and henv.clock() - t0 == 60 and not settled(evs, "1"), "7b at the budget, t=60"
    assert [m.id for m in s.inbox] == ["1"] and s.inbox[0].recovered
    henv.spawner.child = _ZeroPipe
    staged_at = henv.clock()
    evs = henv.host.tick()                              # the old group is gone: the next process, M staged at once
    fresh = henv.spawner.children[-1]
    assert fresh is not child and s.writing.id == "1" and s.writing.counted == 0.0 and s.writing.counted_at == (
        staged_at)
    fresh.emit(vinit())                                 # its first init: verified, a turn of its own
    evs += henv.host.tick()
    evs += cycle(fresh, henv.clock())
    [end] = settled(evs, "1")
    assert (end.delivered, end.ok) == (False, False) and "in two periods of" in end.detail
    assert henv.clock() - staged_at >= 60 and henv.clock() - staged_at < 70, "the second budget, from its staging"
    assert b'"next"' not in child.stdin and b'"next"' not in fresh.stdin
    valid_stdin(child, fresh)


@pytest.mark.usefixtures("verified")
def test_c5_01_the_budget_is_spent_while_m_is_staged_again_with_no_byte_accepted(henv):
    """Blocked from t=0 by the CLI's turn, released at t=10 and staged again into a pipe that takes nothing: the
    budget (not the new staging's write clock) is spent: 7b, M unstaged exactly and first in the inbox, nothing
    failed. Follow-up (counted time): the 10 s before the pass at t=10, which first saw the turn ended, count
    nothing, so the 7b comes at 30 min + 10 s; and the next process starts at the next pass."""
    s, child = idle_host(henv)
    t0 = henv.clock()
    emit(henv, child, vinit())
    henv.host.send(SID, "next", message_id="1")
    henv.host.tick()
    henv.clock.advance(10)
    child.accept = 0
    assert emit(henv, child, foreign()) == [] and s.writing.id == "1" and s.busy_since == henv.clock()
    henv.clock.advance(30 * 60 - 1)
    assert henv.host.tick() == [] and child.terminated == 0
    henv.clock.advance(1)
    evs = henv.host.tick()
    assert not settled(evs, "1") and child.terminated == 1 and henv.clock() - t0 == 30 * 60 + 10
    evs = henv.host.tick()
    fresh = henv.spawner.children[-1]
    assert fresh is not child and ("delivered", "1") in [(e.kind, e.message_id) for e in evs], (
        "the next process took M at the next pass")
    assert b'"next"' not in child.stdin
    valid_stdin(child, fresh, once=["next"])


@pytest.mark.usefixtures("verified")
def test_c5_01_a_successors_budget_starts_when_it_becomes_the_head(henv):
    s, child = idle_host(henv)
    emit(henv, child, vinit())
    henv.host.send(SID, "first", message_id="1")
    henv.host.send(SID, "second", message_id="2")
    henv.host.tick()
    henv.clock.advance(20 * 60)
    henv.host.cancel(SID, "1", "cancelled")
    henv.host.tick()
    assert s.inbox[0].counted == 0.0 and s.inbox[0].counted_at == henv.clock(), "its budget starts now, as the head"
    henv.clock.advance(29 * 60)
    assert henv.host.tick() == [] and child.terminated == 0
    henv.clock.advance(60)
    assert henv.host.tick() == [] and child.terminated == 1 and [m.id for m in s.inbox] == ["2"]


@pytest.mark.usefixtures("verified")
def test_c5_01_an_accepted_byte_ends_the_budget(henv):
    """Progress: once a byte of M's line is accepted the write owns it, and no budget runs for it."""
    s, child = idle_host(henv)
    child.accept = 5
    henv.host.send(SID, "partial", message_id="1")
    henv.host.tick()
    assert s.writing.counted is None and s.turn_at is None


@pytest.mark.usefixtures("verified")
def test_r1_an_unstage_removes_exactly_the_staged_lines_bytes(henv):
    """Pre-merge review R-1: bytes queued AFTER the staged line (nothing appends there today; a later change may)
    survive an unstage, as do the bytes before it."""
    s, child = staged_zero(henv)
    before, line = bytes(s.outbuf[:s.turn_at]), bytes(s.outbuf[s.turn_at:])
    after = b'{"type": "control_request", "request_id": "later", "request": {"subtype": "interrupt"}}\n'
    s.outbuf += after
    child.emit(vinit())
    henv.host.tick()
    assert s.writing is None and bytes(s.outbuf) == before + after and line not in bytes(s.outbuf)


def test_r3_event_turn_takes_no_part_in_equality():
    """Pre-merge review R-3: like the other added fields, `turn` is compare=False."""
    from agent_comms.providers import Event
    assert Event("turn_started", turn="own") == Event("turn_started", turn="other") == Event("turn_started")


# --- review round 6: one budget invariant, in one place (C6-01, C6-02)


def test_c6_01_a_downgrade_does_not_free_a_budgeted_message_from_its_budget(henv, monkeypatch):
    """Sol's sequence: timeout 60 s; M's first budget is spent (7b at t=60), its second begins at t=60 in the next
    process; that process begins a turn of its own (M back to the inbox), then a plain result downgrades it, M is
    staged again, zero bytes accepted: M fails by its second budget, not by a staging's write timer (t=179). Zero
    bytes of M ever written; every stdin line whole. Follow-up (counted time): passes every 5 s, the result read at
    t=120; the 5 s before that pass count nothing (it first saw the turn ended), so 55 s to t=115, then 5 s staged
    in the downgraded process: M fails at t=125 (it was t=120 with one pass at t=119)."""
    short_host(henv, monkeypatch)
    t0 = henv.clock()
    s, child = staged_zero(henv)
    henv.clock.advance(45)
    emit(henv, child, vinit())
    henv.clock.advance(15)                              # t=60: the first budget is spent
    assert henv.host.tick() == [] and child.terminated == 1 and s.inbox[0].recovered
    henv.spawner.child = _ZeroPipe
    henv.host.tick()                                    # the next process: M staged at t=60, its second budget
    fresh = henv.spawner.children[-1]
    assert s.writing.id == "1" and s.writing.counted == 0.0 and s.writing.counted_at == t0 + 60
    fresh.emit(vinit())
    henv.host.tick()                                    # verified, a turn of its own: M back to the inbox
    assert s.writing is None and [m.id for m in s.inbox] == ["1"]
    for _ in range(11):                                 # to t=115, a pass every 5 s
        henv.clock.advance(5)
        assert henv.host.tick() == []
    assert s.inbox[0].counted == 55
    henv.clock.advance(5)                               # t=120: a plain result downgrades the process
    fresh.emit(plain())
    assert henv.host.tick() == [] and not s.provider.verified and s.writing.id == "1" and s.turn_at is not None
    henv.clock.advance(5)                               # t=125
    [end] = settled(henv.host.tick(), "1")
    assert (end.delivered, end.ok) == (False, False) and henv.clock() - t0 == 125
    assert b'"next"' not in child.stdin and b'"next"' not in fresh.stdin
    valid_stdin(child, fresh)


def test_c6_01_the_first_budget_too_survives_a_downgrade(henv, monkeypatch):
    """Follow-up (counted time): passes every 5 s; the 5 s before the pass at t=50 that first sees the turn ended
    count nothing, so the budget (10 s staged, then 35 s held, then staged again in the downgraded process) is spent
    at t=65, not t=60; still its budget, not this staging's write timer (t=110)."""
    short_host(henv, monkeypatch)
    t0 = henv.clock()
    s, child = staged_zero(henv)
    henv.clock.advance(10)
    emit(henv, child, vinit())                          # back to the inbox
    for _ in range(7):                                  # to t=45
        henv.clock.advance(5)
        henv.host.tick()
    henv.clock.advance(5)
    emit(henv, child, plain())                          # t=50: downgrade; M staged again, still zero bytes
    assert not s.provider.verified and s.writing.id == "1" and s.turn_at is not None and s.writing.counted == 45
    henv.spawner.child = _ZeroPipe
    for _ in range(2):                                  # t=55, t=60
        henv.clock.advance(5)
        assert henv.host.tick() == [] and child.terminated == 0
    henv.clock.advance(5)                               # t=65: its budget, not this staging's write timer
    evs = henv.host.tick()
    assert not settled(evs, "1") and child.terminated == 1 and henv.clock() - t0 == 65
    assert s.inbox[0].recovered and b'"next"' not in child.stdin, "7b: the next process has it, with a fresh budget"


def test_c6_02_a_result_first_read_after_the_budget_is_the_budgets_7b_not_a_write(henv, monkeypatch):
    """M waits in the inbox from t=0 behind the CLI's turn; the turn's result is first read at t=75, after M's 60 s
    budget ran out: the release pass does not stage M into the original process; it is the 7b restart, and the next
    process writes M once. Follow-up (counted time): with passes every 15 s while the turn runs, the budget is spent
    at the pass at t=60, and the result read at t=75 finds the process going; with no pass between t=0 and t=75 it
    counts nothing (`test_c9_a_pass_that_first_sees_the_turn_ended_counts_nothing_before_it`)."""
    short_host(henv, monkeypatch)
    s, child = idle_host(henv)
    emit(henv, child, vinit())
    henv.host.send(SID, "next", message_id="1")
    henv.host.tick()
    for _ in range(4):
        henv.clock.advance(15)
        evs = henv.host.tick()
    assert child.terminated == 1 and s.inbox[0].recovered, "7b at t=60"
    henv.clock.advance(15)
    child.emit(foreign())
    henv.host.tick()
    henv.host.tick()
    fresh = henv.spawner.children[-1]
    assert fresh is not child and fresh.turns() == ["next"] and b'"next"' not in child.stdin
    valid_stdin(child, fresh, once=["next"])


def test_c6_02_with_the_7b_spent_a_late_release_is_mains_failure(henv, monkeypatch):
    """Follow-up (counted time): passes every 15 s while the second process's turn runs, so its 60 s are spent at
    t=120 + a pass and M fails before the result read at t=135 could release it."""
    short_host(henv, monkeypatch)
    s, child = idle_host(henv)
    emit(henv, child, vinit())
    henv.host.send(SID, "next", message_id="1")
    henv.host.tick()
    henv.clock.advance(60)
    henv.host.tick()                                    # 7b
    henv.spawner.child = _ZeroPipe
    henv.host.tick()
    fresh = henv.spawner.children[-1]
    fresh.emit(vinit())
    henv.host.tick()                                    # the second budget, from its staging; back to the inbox
    evs = []
    for _ in range(4):
        henv.clock.advance(15)
        evs += henv.host.tick()
    fresh.accept = None
    henv.clock.advance(15)
    fresh.emit(foreign())
    [end] = settled(evs + henv.host.tick(), "1")
    assert (end.delivered, end.ok) == (False, False) and b'"next"' not in fresh.stdin
    valid_stdin(child, fresh)


def test_c6_parity_with_verified_empty_no_message_ever_gets_a_budget(henv, monkeypatch):
    monkeypatch.setattr(claude, "VERIFIED", frozenset())
    s, child, _ = started(henv, "zero", "0")
    child.accept = 0
    henv.host.send(SID, "next", message_id="1")
    emit(henv, child, vinit(), plain())
    assert s.writing.counted is None and s.turn_at is None
    child.emit(vinit(), foreign())
    henv.clock.advance(30 * 60)
    [end] = settled(henv.host.tick(), "1")
    assert end.detail == "timed out after 30 minutes", "main's write timer"


# --- review round 7: the 7b recovery is earned (C7-01); a budgeted head with no process (C7-02)


def candidate_run(henv, monkeypatch, verified, script):
    """A fresh session (timeout 60 s) whose first process's pipe takes nothing: M is staged before any `init` (the
    process is only a candidate while some version is VERIFIED), then `script`: (seconds to advance, frames to emit)
    per pass. What the run shows outside: every pass's events, every process's stdin without the uuid key, how often
    each was stopped, how many there were, and what is left in the inbox."""
    import dataclasses
    from test_dispatch import FakeProcs, FakeSpawner
    monkeypatch.setattr(claude, "VERIFIED", frozenset(verified))
    henv.spawner = FakeSpawner(child=_ZeroPipe)
    henv.procs = FakeProcs(henv.spawner)
    henv.host = make_host(henv, timeout_minutes=1)
    s = open_session(henv)
    henv.host.send(SID, "next", message_id="1")
    passes = [[dataclasses.asdict(e) for e in henv.host.tick()]]
    assert s.writing.id == "1" and s.child is not None and henv.spawner.children[0].stdin == b""
    staged = s.turn_at is not None
    for dt, frames in script:
        henv.clock.advance(dt)
        if frames:
            henv.spawner.children[-1].emit(*frames)
        passes.append([dataclasses.asdict(e) for e in henv.host.tick()])
    return {"passes": passes, "stdin": [UUID_KEY.sub(b"}\n", c.stdin) for c in henv.spawner.children],
            "stops": [c.terminated for c in henv.spawner.children], "inbox": [m.id for m in s.inbox],
            "staged_tracked": staged}


@pytest.mark.parametrize("frames", [[vinit(version="2.1.329")], []], ids=["unqualified_init", "no_init"])
def test_c7_01_a_candidate_that_never_verifies_is_mains_write_timer_and_failure(henv, monkeypatch, frames):
    """Sol's sequence (and a candidate that never writes an `init`): M staged with zero bytes before the first
    `init`, which reports 2.1.329 (or never comes); at t=60 M fails by the write timer and the process is stopped,
    with no restart and no requeue: every pass's events, the stdin and the stops are those of THIS tree run with
    VERIFIED empty, byte for byte (the uuid key on that one staged line aside); that run is main's by the golden's
    case 1 (test_structured_results). The byte span was tracked meanwhile (it is needed had it verified)."""
    script = [(5, frames), (55, []), (1, []), (60, [])]
    ours = candidate_run(henv, monkeypatch, {V}, script)
    main = candidate_run(henv, monkeypatch, (), script)
    assert ours["staged_tracked"] and not main["staged_tracked"]
    del ours["staged_tracked"], main["staged_tracked"]
    assert ours == main
    [end] = [e for p in ours["passes"] for e in p if e["kind"] == "turn_ended"]
    assert (end["message_id"], end["delivered"], end["ok"]) == ("1", False, False)
    assert end["detail"] == henv.host._timeout_detail() and ours["passes"][2] == [end], "at t=60, by main's timer"
    assert ours["stops"] == [1] and ours["inbox"] == [], "no restart, no requeue"


def test_c7_01_a_candidate_that_verifies_at_t30_and_then_blocks_earns_the_7b(henv, monkeypatch):
    """Staged at t=0 before any `init`; at t=30 the process verifies and begins a turn of its own (M back to the
    inbox, held): M was held under a verified process during its budget, so at t=60 it is the 7b restart; the next
    process writes it once."""
    run = candidate_run(henv, monkeypatch, {V}, [(30, [vinit()]), (30, [])])
    s = henv.host.sessions[SID]
    assert not [e for p in run["passes"] for e in p if e["kind"] == "turn_ended"]
    assert run["stops"][0] == 1 and len(run["stops"]) <= 2 and set(run["stdin"]) == {b""}
    assert (s.writing or s.inbox[0]).id == "1" and (s.writing or s.inbox[0]).recovered
    henv.spawner.child = FakeSessionChild               # the next process's pipe is open
    henv.spawner.children[-1].accept = None             # (if it is already there)
    henv.host.tick()
    henv.host.tick()
    fresh = henv.spawner.children[-1]
    assert len(henv.spawner.children) == 2 and fresh.turns() == ["next"] and fresh.terminated == 0
    valid_stdin(*henv.spawner.children, once=["next"])


def test_c7_01_verified_then_downgraded_keeps_the_7b_it_earned(henv, monkeypatch):
    """Staged at t=0 before any `init`; verified at t=20 (a turn of its own: M back to the inbox), downgraded at
    t=40 by a plain result (M staged again, zero bytes): the budget was earned at t=20, the downgrade does not take
    it away (C6-01), so it is the 7b restart. Follow-up (counted time): the 20 s before the pass at t=40, which
    first saw the turn ended, count nothing, so that is at t=80, not t=60."""
    run = candidate_run(henv, monkeypatch, {V}, [(20, [vinit()]), (20, [plain()]), (20, []), (20, [])])
    s = henv.host.sessions[SID]
    assert not [e for p in run["passes"] for e in p if e["kind"] == "turn_ended"]
    assert run["stops"][0] == 1 and set(run["stdin"]) == {b""}
    msg = s.writing or s.inbox[0]
    assert msg.id == "1" and msg.recovered


def test_c7_01_a_verification_and_a_downgrade_read_in_one_pass_still_earn_it(henv, monkeypatch):
    """The process verifies and is downgraded by frames read in ONE pass (an init that qualifies, then a plain result
    of the CLI's own turn): M was staged, with its budget, under a verified process, so the budget was earned, line
    by line, and at t=60 it is the 7b restart, not main's failure."""
    run = candidate_run(henv, monkeypatch, {V}, [(10, [vinit(), plain()]), (50, [])])
    s = henv.host.sessions[SID]
    assert not [e for p in run["passes"] for e in p if e["kind"] == "turn_ended"]
    msg = s.writing or s.inbox[0]
    assert run["stops"][0] == 1 and msg.id == "1" and msg.recovered and set(run["stdin"]) == {b""}

def test_c7_01_an_unqualified_runner_never_restarts_for_a_blocked_message_whatever_the_frames(henv, monkeypatch):
    """The same with frames that would hold a verified process (a bracket opened and closed): a process whose first
    `init` reports 2.1.329 holds nothing, unstages nothing, and fails M when this tree with VERIFIED empty does
    (main's, by the golden's case 1)."""
    script = [(5, [vinit(version="2.1.329")]), (10, [foreign()]), (10, [vinit()]), (35, []), (1, [])]
    ours = candidate_run(henv, monkeypatch, {V}, script)
    main = candidate_run(henv, monkeypatch, (), script)
    del ours["staged_tracked"], main["staged_tracked"]
    assert ours == main and ours["stops"] == [1] and ours["inbox"] == []


@pytest.mark.parametrize("frames", [[vinit()], [vinit(), foreign(), vinit()], [vinit(version="2.1.329")], []])
def test_c8_03_with_verified_empty_no_message_has_a_budget_and_no_no_progress_line_is_written(
        henv, monkeypatch, caplog, frames):
    """The three no-progress lines (and HELD) are written only by `_spent` for a message with a budget; with
    VERIFIED empty no message ever has one, so none of them can appear, whatever the process writes and however long
    its pipe takes nothing (main's fixture has none either)."""
    import logging
    caplog.set_level(logging.INFO, logger="agent_comms")
    run = candidate_run(henv, monkeypatch, (), [(5, frames), (55, []), (1, []), (125, [])])
    s = henv.host.sessions[SID]
    assert not run["staged_tracked"] and all(m.counted is None for m in s.inbox)
    assert "made no progress" not in caplog.text and "two periods of" not in json.dumps(run["passes"])


def held_then_unbound(henv, monkeypatch):
    """Timeout 60 s, a verified idle session, the CLI's own turn open; M sent at t=0 is held (its budget from t=0);
    the binding changes, so once the turn closes the process is stopped for it. Returns (session, child, t0)."""
    short_host(henv, monkeypatch)
    s, child = idle_host(henv)
    t0 = henv.clock()
    emit(henv, child, vinit())
    henv.host.send(SID, "next", message_id="1")
    assert henv.host.tick() == [] and s.inbox[0].counted == 0 and s.inbox[0].counted_at == t0 and s.inbox[0].earned
    s.bind = "e" * 64
    return s, child, t0


def test_c7_02_sols_sequence_the_replacement_needed_anyway_is_the_recovery(henv, monkeypatch):
    """The CLI's turn closes at t=59: the binding change stops the verified child; its group is reaped only at t=61:
    one replacement, not stopped, and it writes M once. Follow-up (counted time): the wait for the binding, and for
    the old process to go, counts nothing, so M is not even recovered (C7-02 had it recovered by then)."""
    s, child, t0 = held_then_unbound(henv, monkeypatch)
    henv.clock.advance(59)
    henv.groups.add(child.pid)                          # its group outlives it for a moment
    assert emit(henv, child, foreign()) == [] and child.terminated == 1
    henv.clock.advance(2)                               # t=61
    henv.groups.discard(child.pid)
    evs = henv.host.tick()
    fresh = henv.spawner.children[-1]
    assert len(henv.spawner.children) == 2 and fresh.terminated == 0 and ("delivered", "1") in [
        (e.kind, e.message_id) for e in evs]
    assert fresh.turns() == ["next"] and not s.in_flight.recovered and s.in_flight.counted is None
    henv.host.tick()
    assert fresh.terminated == 0
    valid_stdin(child, fresh, once=["next"])


def unbound_and_gone(henv, monkeypatch):
    """As above, with the process stopped and reaped at t=10; then no slot (admission denied)."""
    s, child, t0 = held_then_unbound(henv, monkeypatch)
    henv.clock.advance(10)
    henv.host.admit = lambda sess: False
    emit(henv, child, foreign())                        # stopped and reaped in this pass; no slot for the next
    assert s.child is None and [m.id for m in s.inbox] == ["1"] and child.terminated == 1
    return s, child, t0


def test_c7_02_admission_denied_past_the_first_budget_the_spawn_when_admitted_is_the_recovery(henv, monkeypatch):
    """Follow-up (counted time): with no process nothing counts, so at t=60 nothing is recovered or stopped; the one
    process that starts when a slot comes writes M once."""
    s, child, t0 = unbound_and_gone(henv, monkeypatch)
    henv.clock.advance(50)                              # t=60: no process, nothing counted
    assert henv.host.tick() == [] and not s.inbox[0].recovered and s.inbox[0].counted == 0
    assert len(henv.spawner.children) == 1, "nothing spawned while no slot"
    henv.clock.advance(30)                              # t=90: a slot
    henv.host.admit = None
    evs = henv.host.tick()
    fresh = henv.spawner.children[-1]
    assert len(henv.spawner.children) == 2 and ("delivered", "1") in [(e.kind, e.message_id) for e in evs]
    henv.host.tick()
    assert fresh.terminated == 0 and fresh.turns() == ["next"]


def test_c7_02_admission_denied_for_two_timeouts_m_waits_and_is_written_when_a_slot_comes(henv, monkeypatch):
    """Follow-up (counted time; was `..._past_the_second_budget_fails_it_without_spawning`, a ruling withdrawn): no
    slot is the host's own wait, as on main: M waits, nothing is failed or stopped, and the process that starts when a
    slot comes, at t=300, writes it once."""
    s, child, t0 = unbound_and_gone(henv, monkeypatch)
    for _ in range(58):                                 # to t=300, a pass every 5 s
        henv.clock.advance(5)
        assert henv.host.tick() == []
    assert len(henv.spawner.children) == 1 and [m.id for m in s.inbox] == ["1"] and not s.inbox[0].recovered
    henv.host.admit = None
    evs = henv.host.tick()
    fresh = henv.spawner.children[-1]
    assert ("delivered", "1") in [(e.kind, e.message_id) for e in evs] and fresh.turns() == ["next"]


@pytest.mark.parametrize("gone_at", [100, 130])
def test_c7_02_a_delayed_reap_past_the_deadline(henv, monkeypatch, gone_at):
    """Stopped at t=10 for the binding, its group gone only at t=100 (or t=130). Follow-up (counted time; C7-02 had
    the budget run on meanwhile, M recovered at t=60 and failed at t=120 in the second case): a process going down
    counts nothing, so nothing is recovered or failed; once the group is gone the one replacement writes M."""
    s, child, t0 = held_then_unbound(henv, monkeypatch)
    henv.clock.advance(10)
    henv.groups.add(child.pid)
    emit(henv, child, foreign())
    assert child.terminated == 1 and s.child is child
    for t in range(15, gone_at + 1, 5):
        henv.clock.advance(5)
        if t == gone_at:
            henv.groups.discard(child.pid)
        evs = henv.host.tick()
        if t < gone_at:
            assert not settled(evs, "1") and not s.inbox[0].recovered and s.inbox[0].counted == 0
    fresh = henv.spawner.children[-1]
    assert len(henv.spawner.children) == 2 and fresh.turns() == ["next"] and fresh.terminated == 0


# --- follow-up after the switch-on: the budget counts only the agent's holding back (counted time)


@pytest.mark.parametrize("when", ["after_the_turn", "during_the_turn"])
@pytest.mark.parametrize("gate", ["turn_gate", "may_start"])
def test_c9_a_host_gate_that_withholds_the_message_counts_nothing(henv, monkeypatch, gate, when):
    """The reviewer's sequence (a verified child whose CLI runs a turn of its own when M arrives: held, its budget
    from t=0; at t=5 the turn ends but the host's turn gate is closed, as for a `full` child whose parent's process
    was parked), and the same with `may_start` false (the node past its self-stop deadline), and with the gate
    closing while the CLI's turn still runs: for 300 s (five timeouts) nothing is stopped, nothing is failed, the
    budget counts nothing; once the gate opens (and the turn is over) the same process writes M, once, as on main."""
    short_host(henv, monkeypatch)
    s, child = idle_host(henv)
    flag = {"open": True}
    if gate == "turn_gate":
        henv.host.turn_gate = lambda sess: flag["open"]
    else:
        henv.host.may_start = lambda: flag["open"]
    emit(henv, child, vinit())
    henv.host.send(SID, "next", message_id="1")
    assert henv.host.tick() == [] and s.inbox[0].counted == 0 and s.inbox[0].earned   # held by the CLI's turn
    henv.clock.advance(5)
    flag["open"] = False
    if when == "after_the_turn":
        child.emit(foreign())
    for _ in range(60):
        assert henv.host.tick() == []
        henv.clock.advance(5)
    assert child.terminated == 0 and len(henv.spawner.children) == 1 and [m.id for m in s.inbox] == ["1"]
    assert s.inbox[0].counted == 0 and not s.inbox[0].recovered
    if when == "during_the_turn":
        child.emit(foreign())
    flag["open"] = True
    assert [(e.kind, e.message_id) for e in henv.host.tick()] == [("delivered", "1")]
    assert child.turns()[-1] == "next" and child.terminated == 0
    valid_stdin(child, once=["next"])


@pytest.mark.parametrize("gate", ["groups", "admit"])
def test_c9_no_process_counts_nothing_and_the_budget_already_counted_is_kept(henv, monkeypatch, gate):
    """M held by the CLI's turn for 30 s (counted), then the process exits by itself: no process, and none may start
    (a stopping group in its checkout, or no slot): for 300 s nothing counts, nothing is failed, nothing started;
    once one may start, it writes M once. The 30 s already counted stay with the message (they are not lost, nor
    reset), so a turn of the next process's own would leave it 30 s."""
    short_host(henv, monkeypatch)
    s, child = idle_host(henv)
    flag = {"open": True}
    if gate == "groups":
        monkeypatch.setattr(henv.host.groups, "blocked", lambda cwd: not flag["open"])
    else:
        henv.host.admit = lambda sess: flag["open"]
    emit(henv, child, vinit())
    henv.host.send(SID, "next", message_id="1")
    henv.host.tick()
    for _ in range(6):
        henv.clock.advance(5)
        henv.host.tick()
    assert s.inbox[0].counted == 30
    flag["open"] = False
    child.code = 1                                      # it exits by itself: nothing in flight, M in the inbox
    henv.clock.advance(5)
    assert henv.host.tick() == [] and s.child is None
    for _ in range(60):
        henv.clock.advance(5)
        assert henv.host.tick() == []
    assert len(henv.spawner.children) == 1 and [m.id for m in s.inbox] == ["1"] and s.inbox[0].counted == 30
    flag["open"] = True
    evs = henv.host.tick()
    fresh = henv.spawner.children[-1]
    assert ("delivered", "1") in [(e.kind, e.message_id) for e in evs] and fresh.turns() == ["next"]


def test_c9_after_a_7b_the_old_process_going_down_counts_nothing(henv, monkeypatch):
    """The 7b restart at t=60; the old process's group then outlives it for 120 s (two timeouts): a process going
    down counts nothing, though its adapter still shows the CLI's turn open, so M is neither failed nor counted; the
    one replacement writes it once."""
    short_host(henv, monkeypatch)
    s, child = idle_host(henv)
    emit(henv, child, vinit())
    henv.host.send(SID, "next", message_id="1")
    henv.host.tick()
    henv.groups.add(child.pid)
    henv.clock.advance(60)
    assert henv.host.tick() == [] and child.terminated == 1 and s.inbox[0].recovered and s.child is child
    for _ in range(24):
        henv.clock.advance(5)
        assert henv.host.tick() == []
    assert s.child is child and s.inbox[0].counted == 0
    henv.groups.discard(child.pid)
    evs = henv.host.tick() + henv.host.tick()
    fresh = henv.spawner.children[-1]
    assert fresh is not child and ("delivered", "1") in [(e.kind, e.message_id) for e in evs]
    assert fresh.turns() == ["next"] and not settled(evs, "1")


def test_c9_a_message_staged_in_a_verified_process_earned_its_7b_though_the_next_read_downgrades_it(
        henv, monkeypatch):
    """Staged at t=0, zero bytes, in a verified process: that is a counted state under a verified process, so the
    budget is earned at the staging (not only at a later pass). The first line read at the next pass downgrades the
    process (a plain result): still the 7b restart at t=60, not main's write timer."""
    short_host(henv, monkeypatch)
    s, child = idle_host(henv)
    child.accept = 0
    henv.host.send(SID, "next", message_id="1")
    assert henv.host.tick() == [] and s.writing.id == "1" and s.writing.earned
    henv.clock.advance(5)
    child.emit(plain())
    henv.host.tick()
    assert not s.provider.verified and s.writing.id == "1"
    for _ in range(10):
        henv.clock.advance(5)
        assert not settled(henv.host.tick(), "1")
    henv.clock.advance(5)                               # t=60
    evs = henv.host.tick()
    assert not settled(evs, "1") and child.terminated == 1 and s.inbox[0].recovered

def test_c9_a_pass_that_first_sees_the_turn_ended_counts_nothing_before_it(henv, monkeypatch):
    """The rule for the interval between two passes: it counts only if M was held back by the agent at BOTH its
    ends. M held at t=0; the CLI's result is first read at t=75, with no pass between: the pass at t=75 sees the
    turn ended, so the 75 s count nothing (they may have been spent after the turn ended), and the same process
    writes M at t=75 (C6-02's 7b needs a pass while the turn still runs)."""
    short_host(henv, monkeypatch)
    s, child = idle_host(henv)
    emit(henv, child, vinit())
    henv.host.send(SID, "next", message_id="1")
    henv.host.tick()
    henv.clock.advance(75)
    child.emit(foreign())
    assert [(e.kind, e.message_id) for e in henv.host.tick()] == [("delivered", "1")]
    assert child.terminated == 0 and not s.in_flight.recovered and child.turns()[-1] == "next"


def test_c9_gate_flapping_ends_within_two_timeouts_of_counted_time(henv, monkeypatch):
    """The turn gate closed and open by turns (30 s each, a pass every 5 s), while the CLI's own turns hold M in
    the open intervals, and the next process's pipe takes nothing: M's outcome (the 7b once, then main's failure, or
    the gate withdrawing a staged line, main's) comes within two timeouts of counted time (and one pass each); the
    wall time is longer, for the gate's closed intervals count nothing; nothing is written twice. Counted time is
    measured here from the passes' own record: an interval between two passes counts if, at both, M was staged
    with no byte accepted, or first in the inbox with the gate open and the CLI's turn open."""
    short_host(henv, monkeypatch)
    s, child = idle_host(henv)
    flag = {"open": True}
    henv.host.turn_gate = lambda sess: flag["open"]
    henv.spawner.child = _ZeroPipe
    emit(henv, child, vinit())
    henv.host.send(SID, "next", message_id="1")
    henv.host.tick()
    t0, counted, prev, outcome, restarts = henv.clock(), 0.0, True, None, 0
    while outcome is None and henv.clock() - t0 < 1200:
        henv.clock.advance(5)
        k = henv.clock() - t0
        flag["open"] = (k // 30) % 2 == 0
        p = henv.spawner.children[-1]
        if s.child is p and not p.stdout and k % 30 == 5:
            p.emit(vinit())                             # a turn of its own, at the start of every open interval
        evs = henv.host.tick()
        for e in evs:
            if e.message_id == "1" and e.kind in ("delivered", "turn_ended"):
                outcome = (e.kind, e.delivered, e.detail)
        m = s.writing if s.writing is not None and s.writing.id == "1" else (s.inbox[0] if s.inbox else None)
        now = (m is not None and s.child is not None and s.stopping_since is None and (
            (m is s.writing and s.turn_at is not None)
            or (m is not s.writing and flag["open"] and s.provider.holds_turns())))
        counted += 5 if (prev and now) else 0
        prev = now
        restarts = sum(c.terminated for c in henv.spawner.children)
    assert outcome is not None and outcome[0] == "turn_ended" and outcome[1] is False, outcome
    assert counted <= 2 * 60 + 2 * 5, ("within two timeouts of counted time", counted)
    assert henv.clock() - t0 > 2 * 60 + 2 * 5, "the gate's closed intervals counted nothing"
    assert restarts <= 2 and len(henv.spawner.children) <= 2
    valid_stdin(*henv.spawner.children)
    assert not any(b'"next"' in c.stdin for c in henv.spawner.children), "never written"


@pytest.mark.parametrize("version, caps, said", [
    (V, CAPS, "reports Claude Code 2.1.330; turn correlation is on for it"),
    ("2.1.331", CAPS, "reports Claude Code 2.1.331; turn correlation is off for it (attributed by order)"),
    (V, ["interrupt_receipt_v1"], "reports Claude Code 2.1.330; turn correlation is off for it (attributed by order)"),
    (None, CAPS, "reports Claude Code no version; turn correlation is off for it (attributed by order)"),
])
def test_c9_each_process_says_at_its_first_init_whether_turn_correlation_is_on(monkeypatch, caplog, version, caps,
                                                                                said):
    """How the owner sees that it is on: one INFO line per process, at its first `init` (the version it reports, or
    none, and whether correlation is on for it); not again at a later init, and again for the next process."""
    import logging
    monkeypatch.setattr(claude, "VERIFIED", frozenset({V}))
    caplog.set_level(logging.INFO, logger="agent_comms")
    p = adapter()
    write(p)
    raw(p, vinit(version=version, caps=caps))
    raw(p, vinit(version=version, caps=caps))
    lines = [r.getMessage() for r in caplog.records if "turn correlation is" in r.getMessage()]
    assert lines == [f"Claude session {SID}: this process {said}"]
    assert all(r.levelname == "INFO" for r in caplog.records if "turn correlation is" in r.getMessage())
    p.argv(["claude", "-p", "{prompt}"], session_id=SID, resume=True, model=None)     # the next process
    write(p)
    raw(p, vinit(version=version, caps=caps))
    assert len([r for r in caplog.records if "turn correlation is" in r.getMessage()]) == 2


@pytest.mark.parametrize("version", ["2.1.330\nClaude session forged: this process reports Claude Code 2.1.330; "
                                     "turn correlation is on for it", "9" * 10_000, "2.1.330\x1b[2J\u202e\U0001f600",
                                     "\x01" * 64, "\x00" * 1_000_000],
                         ids=["newline", "long", "controls", "64_controls", "million_nul"])
def test_c10_the_reported_version_goes_in_the_log_escaped_and_bounded(monkeypatch, caplog, version):
    """The version is the process's own text: in the log line no newline or control character (nor anything outside
    printable ASCII) appears, and at most 64 of its characters, with its length said when cut (review round 10)."""
    import logging
    monkeypatch.setattr(claude, "VERIFIED", frozenset({V}))
    caplog.set_level(logging.INFO, logger="agent_comms")
    p = adapter()
    write(p)
    raw(p, vinit(version=version))
    [line] = [r.getMessage() for r in caplog.records if "turn correlation is" in r.getMessage()]
    assert all(" " <= ch <= "~" for ch in line), repr(line)
    assert line.startswith(f"Claude session {SID}: this process reports Claude Code ") and line.endswith(
        "; turn correlation is off for it (attributed by order)")
    shown = line[len(f"Claude session {SID}: this process reports Claude Code "):-len(
        "; turn correlation is off for it (attributed by order)")]
    assert shown == claude.shown_version(version) and len(shown) <= 64 + len(f"... ({len(version)} characters)")
    if len(version) > 64:
        assert shown.endswith(f"... ({len(version)} characters)")
    assert "\n" not in line and "\x1b" not in line
    if "\n" in version:
        assert "\\u000a" in shown

def test_c11_the_version_is_sliced_before_it_is_escaped(monkeypatch):
    """Bounded work whatever the init's size (review round 11): the escaper sees at most 64 characters of the
    version, and the rendered text is capped at 64 too (64 control characters render as 384, cut to 64)."""
    seen = []
    sub = claude.re.sub

    def spy(pattern, repl, text, *a, **kw):
        seen.append(len(text))
        return sub(pattern, repl, text, *a, **kw)

    monkeypatch.setattr(claude.re, "sub", spy)
    shown = claude.shown_version("\x01" * 1_000_000)
    assert seen == [64] and shown == "\\u0001" * 10 + "\\u00" + "... (1000000 characters)"
    assert claude.shown_version("\x01" * 64).startswith("\\u0001" * 10) and claude.shown_version(
        "\x01" * 64).endswith("... (64 characters)")
    assert claude.shown_version("2.1.330") == "2.1.330" and claude.shown_version(None) == "no version"

def test_c9_with_verified_empty_no_process_says_whether_turn_correlation_is_on(monkeypatch, caplog):
    """With VERIFIED empty the line is never written (main's logs, pinned by the golden's case 1, are untouched)."""
    import logging
    monkeypatch.setattr(claude, "VERIFIED", frozenset())
    caplog.set_level(logging.DEBUG, logger="agent_comms")
    p = adapter()
    write(p)
    raw(p, vinit())
    raw(p, vinit(version="2.1.331"))
    assert not [r for r in caplog.records if "turn correlation" in r.getMessage()]

# --- review round 10: host-side withholding wins over the budget in the staged state too


def held_then_staged(henv, monkeypatch, gate):
    """Sol's replay, timeout 60 s, a pass every 5 s: M held by the CLI's own turn from t=0 (40 s counted: the pass
    at t=45 first sees the turn ended), staged at t=45 into a pipe that takes nothing, 15 s more counted to t=60.
    `gate`: "turn_gate" or "may_start", installed open (or None: neither). Returns (session, child, flag)."""
    short_host(henv, monkeypatch)
    s, child = idle_host(henv)
    flag = {"open": True}
    if gate == "turn_gate":
        henv.host.turn_gate = lambda sess: flag["open"]
    elif gate == "may_start":
        henv.host.may_start = lambda: flag["open"]
    emit(henv, child, vinit())
    child.accept = 0
    henv.host.send(SID, "next", message_id="1")
    assert henv.host.tick() == []
    for _ in range(8):                                  # to t=40, held
        henv.clock.advance(5)
        assert henv.host.tick() == []
    henv.clock.advance(5)                               # t=45: the turn ends; M staged, zero bytes
    child.emit(foreign())
    assert henv.host.tick() == [] and s.writing.id == "1" and s.turn_at is not None and s.writing.counted == 40
    for _ in range(3):                                  # to t=60
        henv.clock.advance(5)
        assert henv.host.tick() == []
    assert s.writing.counted == 55 and child.terminated == 0
    return s, child, flag


def test_c10_the_turn_gate_closing_on_a_staged_message_is_mains_withdrawal_not_the_budget(henv, monkeypatch):
    """Sol's replay: the parent's gate closes at t=65 with M staged, zero bytes, and no new turn of the CLI's own.
    Main's withdrawal (TURN_WITHDRAWN, the process stopped) is what happens, as on main; the budget charges nothing
    after the gate closed (55 s, not 60) and neither restarts the process nor requeues M (it did, before)."""
    s, child, flag = held_then_staged(henv, monkeypatch, "turn_gate")
    henv.clock.advance(5)
    flag["open"] = False
    [end] = settled(henv.host.tick(), "1")
    assert (end.delivered, end.ok) == (False, False) and end.detail == TURN_WITHDRAWN
    assert child.terminated == 1 and not s.inbox and len(henv.spawner.children) == 1


@pytest.mark.parametrize("gate", ["turn_gate", "may_start"])
def test_c10_a_new_turn_and_a_closed_gate_unstage_m_and_it_waits_behind_the_gate(henv, monkeypatch, gate):
    """The same, but the CLI begins a new turn of its own at t=65 as the gate (or `may_start`) closes: M is taken
    back (unstaged) and waits in the inbox behind the closed gate for 300 s, nothing counted, stopped or failed; once
    the gate opens and the turn is over, the same process writes M, once."""
    s, child, flag = held_then_staged(henv, monkeypatch, gate)
    henv.clock.advance(5)
    flag["open"] = False
    child.emit(vinit())
    assert henv.host.tick() == [] and s.writing is None and [m.id for m in s.inbox] == ["1"]
    for _ in range(60):
        henv.clock.advance(5)
        assert henv.host.tick() == []
    assert s.inbox[0].counted == 55 and not s.inbox[0].recovered and child.terminated == 0
    child.emit(foreign())
    child.accept = None
    flag["open"] = True
    henv.clock.advance(5)
    assert [(e.kind, e.message_id) for e in henv.host.tick()] == [("delivered", "1")]
    assert child.turns()[-1] == "next" and child.terminated == 0
    valid_stdin(child, once=["next"])


def test_c10_may_start_false_on_a_staged_message_charges_nothing_and_mains_write_timer_ends_it(henv, monkeypatch):
    """The node latched (`may_start` false) at t=65 with M staged since t=45, zero bytes, no new turn: main withdraws
    nothing here (it keeps the staged line), the budget charges nothing while the host withholds new work (55 s),
    and main's write timer from the staging governs instead (review round 11: a staged message always has a
    clock): M fails at t=105, main's detail, the process stopped; nothing is requeued."""
    s, child, flag = held_then_staged(henv, monkeypatch, "may_start")
    henv.clock.advance(5)
    flag["open"] = False
    for _ in range(7):                                  # t=65 to t=95
        assert henv.host.tick() == []
        henv.clock.advance(5)
    assert henv.host.tick() == [] and s.writing.counted == 55      # t=100
    henv.clock.advance(5)                               # t=105: the staging (t=45) plus the timeout
    [end] = settled(henv.host.tick(), "1")
    assert (end.delivered, end.ok) == (False, False) and end.detail == henv.host._timeout_detail()
    assert child.terminated == 1 and not s.inbox


def test_c10_a_binding_change_on_a_staged_message_is_mains_withdrawal_not_the_budget(henv, monkeypatch):
    """A new binding at t=65 with M staged, zero bytes, no new turn: main's withdrawal (BINDING_CHANGED, the process
    stopped), not the budget's restart (55 s counted, not 60)."""
    s, child, flag = held_then_staged(henv, monkeypatch, None)
    henv.clock.advance(5)
    s.bind = "e" * 64
    [end] = settled(henv.host.tick(), "1")
    assert (end.delivered, end.ok) == (False, False) and end.detail == BINDING_CHANGED
    assert child.terminated == 1 and not s.inbox


def test_c10_a_binding_change_and_a_new_turn_hold_m_behind_the_turn_which_counts(henv, monkeypatch):
    """A new binding at t=65 as the CLI begins a new turn of its own: M is taken back and waits for that turn, the
    only thing between it and the restart for its binding, so that wait counts (the follow-up's choice for a binding
    behind the CLI's turn): with 55 s counted at t=60 and the interval to t=65 counted at both ends, the budget is
    spent at t=65, the 7b restart, and the next process, started with the new binding, writes M once."""
    s, child, flag = held_then_staged(henv, monkeypatch, None)
    henv.clock.advance(5)
    s.bind = "e" * 64
    child.emit(vinit())
    assert henv.host.tick() == [] and child.terminated == 1 and s.inbox[0].recovered
    henv.host.tick()
    evs = henv.host.tick()
    fresh = henv.spawner.children[-1]
    assert fresh is not child and fresh.turns() == ["next"] and not settled(evs, "1")
    valid_stdin(child, fresh, once=["next"])


def test_c10_a_binding_change_and_a_new_turn_early_restart_for_the_binding_once_it_ends(henv, monkeypatch):
    """The same at t=50 (45 s counted): M is taken back and held behind the CLI's turn; the turn ends at t=55 (the
    pass that first sees it counts nothing before it); the process is restarted for the binding, and the next one
    writes M once. Not recovered."""
    short_host(henv, monkeypatch)
    s, child = idle_host(henv)
    emit(henv, child, vinit())
    child.accept = 0
    henv.host.send(SID, "next", message_id="1")
    henv.host.tick()
    for _ in range(8):
        henv.clock.advance(5)
        henv.host.tick()
    henv.clock.advance(5)
    child.emit(foreign())
    henv.host.tick()                                    # t=45: staged, zero bytes
    henv.clock.advance(5)                               # t=50: a new binding and a new turn
    s.bind = "e" * 64
    child.emit(vinit())
    assert henv.host.tick() == [] and s.writing is None and s.inbox[0].counted == 45
    henv.clock.advance(5)                               # t=55: the turn ends; restarted for the binding
    child.emit(foreign())
    henv.host.tick()
    assert child.terminated == 1 and not s.inbox[0].recovered and s.inbox[0].counted == 45
    evs = henv.host.tick() + henv.host.tick()
    fresh = henv.spawner.children[-1]
    assert fresh is not child and fresh.turns() == ["next"] and not settled(evs, "1")
    valid_stdin(child, fresh, once=["next"])


# --- review round 11: a staged message is never without a clock


def latched_run(henv, monkeypatch, verified, kind, reopen_at=None):
    """The latched-but-not-silent node (`may_start` false from t=5: a hub call straddled the self-stop deadline, so
    the latch is set but the watchdog sees contact): M staged at t=0 into a pipe that takes nothing. `kind`:
    "verified" (a process that verified, then one ordinary turn, M staged after it: a budget, earned), "other" (M
    staged before the first `init`, which reports 2.1.329), "no_init" (M staged, no `init` ever). A pass every 5 s
    to t=120; at `reopen_at` the latch clears and the pipe takes the line. What is seen outside: every pass's
    events, the stdin without the uuid key, the stops, the inbox."""
    import dataclasses
    from test_dispatch import FakeProcs, FakeSpawner
    monkeypatch.setattr(claude, "VERIFIED", frozenset(verified))
    henv.spawner = FakeSpawner(child=FakeSessionChild)
    henv.procs = FakeProcs(henv.spawner)
    henv.host = make_host(henv, timeout_minutes=1)
    flag = {"open": True}
    henv.host.may_start = lambda: flag["open"]
    s = open_session(henv)
    if kind == "verified":
        henv.host.send(SID, "zero", message_id="0")
        henv.host.tick()
        child = henv.spawner.children[-1]
        u = uuid_of(child)
        child.emit(*((vinit(u), owned(u)) if u else (vinit(), plain())))
        henv.host.tick()
        child.accept = 0
    else:
        henv.spawner.child = _ZeroPipe
    henv.host.send(SID, "next", message_id="1")
    passes = [[dataclasses.asdict(e) for e in henv.host.tick()]]
    child = henv.spawner.children[-1]
    assert s.writing.id == "1" and child.stdin.count(b'"next"') == 0
    budgeted = s.writing.counted is not None
    for k in range(1, 25):
        henv.clock.advance(5)
        if k == 1:
            flag["open"] = False
            if kind == "other":
                child.emit(vinit(version="2.1.329"))
        if reopen_at is not None and k * 5 == reopen_at:
            flag["open"] = True
            child.accept = None
        passes.append([dataclasses.asdict(e) for e in henv.host.tick()])
    return {"passes": passes, "stdin": [UUID_KEY.sub(b"}\n", c.stdin) for c in henv.spawner.children],
            "stops": [c.terminated for c in henv.spawner.children], "inbox": [m.id for m in s.inbox]}, budgeted


@pytest.mark.parametrize("kind", ["verified", "other", "no_init"])
def test_c11_latched_but_not_silent_a_staged_message_keeps_mains_write_timer(henv, monkeypatch, kind):
    """Sol's replay, with the latch closed from t=5 and the watchdog doing nothing (the node is not silent): M,
    staged at t=0 with zero bytes, fails at t=60 by main's write timer, with main's detail, and its process is
    stopped, for a verified 2.1.330 process with a budgeted message, a 2.1.329 one and one that never writes an
    init alike: every pass's events, the stdin (the uuid key aside) and the stops are those of the same run with
    VERIFIED empty (main's, by the golden's case 1). The budget counts nothing meanwhile."""
    ours, budgeted = latched_run(henv, monkeypatch, {V}, kind)
    main, main_budgeted = latched_run(henv, monkeypatch, (), kind)
    assert budgeted and not main_budgeted
    assert ours == main
    [end] = [e for p in ours["passes"] for e in p if e["kind"] == "turn_ended" and e["message_id"] == "1"]
    assert end["delivered"] is False and end["detail"] == henv.host._timeout_detail()
    assert ours["passes"][12] == [end], "at t=60"


def test_c11_the_latch_closing_and_opening_again_before_the_write_timer_the_budget_resumes(henv, monkeypatch):
    """The latch closed from t=5 to t=40: main's write timer from the staging (t=0) governs meanwhile and has not run
    out; at t=40 the latch clears, the budget governs again (it resumes: it counted the 5 s to t=5 and counts from
    the pass at t=40 on, not the closed interval), and the line is written once when the pipe takes it, at t=50."""
    short_host(henv, monkeypatch)
    s, child = idle_host(henv)
    flag = {"open": True}
    henv.host.may_start = lambda: flag["open"]
    child.accept = 0
    henv.host.send(SID, "next", message_id="1")
    henv.host.tick()
    m = s.writing
    assert m.counted == 0
    henv.clock.advance(5)
    henv.host.tick()
    assert m.counted == 5
    flag["open"] = False
    for _ in range(7):                                  # to t=40, latched
        henv.clock.advance(5)
        assert henv.host.tick() == []
    assert m.counted == 5 and s.writing is m and child.terminated == 0
    flag["open"] = True
    henv.clock.advance(5)                               # t=45: the budget again (the interval to t=45 counts nothing)
    assert henv.host.tick() == [] and m.counted == 5
    henv.clock.advance(5)                               # t=50
    child.accept = None
    assert [(e.kind, e.message_id) for e in henv.host.tick()] == [("delivered", "1")]
    assert child.terminated == 0 and child.turns()[-1] == "next"
    valid_stdin(child, once=["next"])


def test_c11_the_latch_and_the_budget_never_give_more_than_the_write_timer_and_two_budgets(henv, monkeypatch):
    """The latch closed at every other pair of passes while the pipe takes nothing: the write timer runs from the
    staging in the closed passes, the budget in the open ones; at the first closed pass at or after the staging plus
    the timeout, main's write timer ends it (a budget not yet spent cannot keep it)."""
    short_host(henv, monkeypatch)
    s, child = idle_host(henv)
    flag = {"open": True}
    henv.host.may_start = lambda: flag["open"]
    child.accept = 0
    henv.host.send(SID, "next", message_id="1")
    henv.host.tick()
    t0, end = henv.clock(), None
    while end is None and henv.clock() - t0 < 600:
        henv.clock.advance(5)
        flag["open"] = (henv.clock() - t0) % 20 < 10
        for e in settled(henv.host.tick(), "1"):
            end = (henv.clock() - t0, e.detail)
    assert end is not None and end[0] <= 60 + 3 * 60 + 10, end
    assert end[1] in (henv.host._timeout_detail(), henv.host.HELD.format(m=1)), end


# --- review round 12: an unearned message is on main's path, entirely


def candidate_latch_run(henv, monkeypatch, verified, frames, script):
    """M staged at t=0, zero bytes, before any `init`; `script`: per pass, (seconds, may_start open?, pipe takes the
    line?, frames to emit). What is seen outside, as `latched_run`."""
    import dataclasses
    from test_dispatch import FakeProcs, FakeSpawner
    monkeypatch.setattr(claude, "VERIFIED", frozenset(verified))
    henv.spawner = FakeSpawner(child=_ZeroPipe)
    henv.procs = FakeProcs(henv.spawner)
    henv.host = make_host(henv, timeout_minutes=1)
    flag = {"open": True}
    henv.host.may_start = lambda: flag["open"]
    s = open_session(henv)
    henv.host.send(SID, "next", message_id="1")
    passes = [[dataclasses.asdict(e) for e in henv.host.tick()]]
    child = henv.spawner.children[-1]
    assert s.writing.id == "1" and child.stdin == b""
    stopped_at = None
    for dt, open_, takes, emitted in script:
        henv.clock.advance(dt)
        flag["open"] = open_
        child.accept = None if takes else 0
        if emitted:
            child.emit(*emitted)
        passes.append([dataclasses.asdict(e) for e in henv.host.tick()])
        if stopped_at is None and child.terminated:
            stopped_at = len(passes) - 1                # the pass at which its process was stopped
    return {"passes": passes, "stdin": [UUID_KEY.sub(b"}\n", c.stdin) for c in henv.spawner.children],
            "stops": [c.terminated for c in henv.spawner.children], "inbox": [m.id for m in s.inbox],
            "stopped_at": stopped_at}, s


@pytest.mark.parametrize("frames", [[vinit(version="2.1.329")], []], ids=["other", "no_init"])
def test_c12_a_reopened_latch_leaves_an_unearned_line_on_mains_write_timer(henv, monkeypatch, frames):
    """Sol's replay: M staged at t=0 (zero bytes, before any init), the latch closed at t=5 (with a 2.1.329 init, or
    none), and at t=60 the latch reopens AND the pipe takes the line: main's write timer from the staging is the
    only clock of a message whose budget is not earned, so M times out at t=60, as with VERIFIED empty, event for
    event; it is not delivered (it was, on 44d0424: the provisional budget took over once counting resumed)."""
    script = [(5, False, False, frames)] + [(5, False, False, [])] * 10 + [(5, True, True, [])]
    ours, s = candidate_latch_run(henv, monkeypatch, {V}, frames, script)
    main, _ = candidate_latch_run(henv, monkeypatch, (), frames, script)
    assert ours == main
    [end] = [e for p in ours["passes"] for e in p if e["kind"] == "turn_ended"]
    assert end["delivered"] is False and end["detail"] == henv.host._timeout_detail()
    assert ours["passes"][12] == [end], "at t=60"


def test_c12_a_candidate_that_verifies_while_latched_is_unearned_until_a_counted_pass(henv, monkeypatch):
    """The one way a verified process can have a staged message that has not earned its budget: it verifies (its
    first init read at t=30, its turn over in the same read) while the latch is closed, so no counted state under
    it yet. Still latched at t=60: main's write timer, at t=60, as with VERIFIED empty, every pass's events equal.
    (Had its init opened a turn of the CLI's own still running at the pass's end, the line would be taken back, as
    the byte span is kept for, and M would wait in the inbox behind the latch, a host-side wait as on main.)"""
    script = [(5, False, False, [])] * 5 + [(5, False, False, [vinit(), foreign()])] + [(5, False, False, [])] * 6
    ours, s = candidate_latch_run(henv, monkeypatch, {V}, [], script)
    main, _ = candidate_latch_run(henv, monkeypatch, (), [], script)
    assert ours == main
    [end] = [e for p in ours["passes"] for e in p if e["kind"] == "turn_ended"]
    assert end["detail"] == henv.host._timeout_detail() and ours["passes"][12] == [end]


def test_c12_a_candidate_that_verifies_while_latched_earns_at_the_first_counted_pass(henv, monkeypatch):
    """The same, but the latch reopens at t=45 with the pipe still taking nothing: the first counted pass under the
    verified process earns the budget, and from it the round-11 rule applies (its budget while counted). The
    hand-over cannot end it earlier than main: the budget counted nothing before (the sample at t=5 is already
    latched), and the 7b comes once it holds the timeout of counted time, at t=105, after main's t=60; nor give it
    more than the write timer plus two budgets."""
    script = ([(5, False, False, [])] * 5 + [(5, False, False, [vinit(), foreign()])] + [(5, False, False, [])] * 2
              + [(5, True, False, [])] * 14)
    ours, s = candidate_latch_run(henv, monkeypatch, {V}, [], script)
    ends = [e for p in ours["passes"] for e in p if e["kind"] == "turn_ended"]
    assert not ends, "not ended earlier than main (main ends it at t=60); the 7b keeps it"
    msg = s.writing or s.inbox[0]
    assert msg.id == "1" and msg.earned
    assert ours["stops"][0] == 1 and msg.recovered and ours["stopped_at"] == 21, (
        "the 7b at t=105: nothing counted before the latch (the sample at t=5 is already latched), 60 s from t=45")


def test_c12_a_verified_stage_latched_then_reopened_follows_its_budget(henv, monkeypatch):
    """Sol's round-12 regression for the budget's branch in `_hold_to_timeout`: a verified, earned stage latched at
    t=40 and reopened with a writable pipe at t=65 (no pass between): at t=65 its counted state holds, so its budget,
    not the write timer (overdue since t=60), governs: the line is written, once."""
    short_host(henv, monkeypatch)
    s, child = idle_host(henv)
    flag = {"open": True}
    henv.host.may_start = lambda: flag["open"]
    child.accept = 0
    henv.host.send(SID, "next", message_id="1")
    henv.host.tick()
    assert s.writing.earned
    henv.clock.advance(40)
    flag["open"] = False
    assert henv.host.tick() == []
    henv.clock.advance(25)                              # t=65
    flag["open"] = True
    child.accept = None
    assert [(e.kind, e.message_id) for e in henv.host.tick()] == [("delivered", "1")]
    valid_stdin(child, once=["next"])


def test_c12_the_gates_are_read_once_per_pass(henv, monkeypatch):
    """The budget and the write timer's choice of clock read the host's gates once per pass, from the same reading
    (review round 12): `may_start` answering open and then closed within one pass (a latch set mid-pass) cannot let
    the budget be the clock for one and the write timer for the other. An earned stage at t=0, latched at the passes
    from t=10 to t=55 (5 s counted); at the next pass, t=65, the first reading is open: the budget governs that whole
    pass (the write timer, overdue since t=60, is not asked), nothing fails, and the interval to t=65 counts nothing
    (t=55 was latched)."""
    short_host(henv, monkeypatch)
    s, child = idle_host(henv)
    answers = []
    henv.host.may_start = lambda: answers.pop(0) if answers else False
    child.accept = 0
    answers[:] = [True] * 50
    henv.host.send(SID, "next", message_id="1")
    henv.host.tick()
    henv.clock.advance(5)
    henv.host.tick()
    assert s.writing.earned and s.writing.counted == 5
    answers[:] = []
    for _ in range(10):                                 # to t=55, latched
        henv.clock.advance(5)
        assert henv.host.tick() == []
    henv.clock.advance(10)                              # t=65: open at the first reading, closed after
    answers[:] = [True]
    assert henv.host.tick() == [] and s.writing.id == "1" and s.writing.counted == 5

# --- review round 13: the gate sample is the tick's; accounting has no side effects


def test_c13_the_latch_closing_with_a_verifying_init_and_a_downgrade_in_the_first_drain_earns_nothing(
        henv, monkeypatch):
    """Through `tick()`: M staged at t=0 (zero bytes, before any init), passes every 5 s with the latch open; at
    t=20 the latch closes and the first drain of that tick reads a qualifying init and a downgrade at once. The gate
    sample is that tick's (closed), so the line read under the verified process earns nothing: M stays unearned,
    on main's path, and times out at t=60, every pass's events equal to the run with VERIFIED empty. (With the
    previous tick's sample it was earned, and delivered at t=60.)"""
    script = ([(5, True, False, [])] * 3 + [(5, False, False, [vinit(), plain()])] + [(5, False, False, [])] * 7
              + [(5, True, True, [])] + [(5, True, True, [])])
    ours, s = candidate_latch_run(henv, monkeypatch, {V}, [], script)
    main, _ = candidate_latch_run(henv, monkeypatch, (), [], script)
    assert ours == main
    [end] = [e for p in ours["passes"] for e in p if e["kind"] == "turn_ended"]
    assert end["detail"] == henv.host._timeout_detail() and ours["passes"][12] == [end], "at t=60"


def test_c13_the_latch_reopening_with_a_verifying_init_and_a_downgrade_in_the_first_drain_earns_it(
        henv, monkeypatch):
    """Through `tick()`: M staged at t=0, the latch closed from t=5; at t=20 it reopens and the first drain reads a
    qualifying init and a downgrade at once. The sample is that tick's (open): the line read under the verified
    process earns the budget, which governs from then (the downgrade does not take it away): no timeout at t=60; the
    7b once it holds the timeout of counted time, at t=80 (counted from the end of the pass at t=20). (With the
    previous tick's sample it was not earned, and timed out at t=60.)"""
    script = ([(5, False, False, [])] * 3 + [(5, True, False, [vinit(), plain()])] + [(5, True, False, [])] * 13)
    ours, s = candidate_latch_run(henv, monkeypatch, {V}, [], script)
    assert not [e for p in ours["passes"] for e in p if e["kind"] == "turn_ended"]
    msg = s.writing or s.inbox[0]
    assert msg.earned and msg.recovered and ours["stopped_at"] == 16, ("the 7b at t=80", ours["stopped_at"])


@pytest.mark.parametrize("kind", ["other", "no_init", "verified"])
def test_c13_the_budget_never_calls_the_stateful_may_start_more_than_main(henv, monkeypatch, kind):
    """Budget accounting has no side effects (review round 13): with VERIFIED = {"2.1.330"}, the calls of the
    stateful `may_start` (the node's sets its latch at the self-stop deadline), how many and when, are those of the
    run with VERIFIED empty, for a 2.1.329 runner, a no-init runner (each with a zero-byte staged line, an unearned
    budget) and a verified runner with nothing to count; the accounting reads `may_start_query` instead."""
    from test_dispatch import FakeProcs, FakeSpawner

    def run(verified):
        monkeypatch.setattr(claude, "VERIFIED", frozenset(verified))
        henv.spawner = FakeSpawner(child=FakeSessionChild if kind == "verified" else _ZeroPipe)
        henv.procs = FakeProcs(henv.spawner)
        henv.host = make_host(henv, timeout_minutes=1)
        calls, queries, t0 = [], [], henv.clock()
        henv.host.may_start = lambda: calls.append(henv.clock() - t0) or True
        henv.host.may_start_query = lambda: queries.append(henv.clock() - t0) or True
        open_session(henv)
        henv.host.send(SID, "next", message_id="1")
        henv.host.tick()
        child = henv.spawner.children[-1]
        for k in range(1, 15):
            henv.clock.advance(5)
            if k == 1 and kind == "other":
                child.emit(vinit(version="2.1.329"))
            if k == 1 and kind == "verified":
                u = uuid_of(child)
                child.emit(*((vinit(u), owned(u)) if u else (vinit(), plain())))
            henv.host.tick()
        return calls, queries

    ours, queried = run({V})
    main, main_queried = run(())
    assert ours == main and main_queried == []
    if kind != "verified":
        assert queried, "the unearned budget's accounting read the query, not the stateful call"


def test_c13_outside_a_tick_the_gates_are_read_afresh(henv, monkeypatch):
    """No path outside a tick reads a tick's sample (only `stop_all`'s last read of each process happens there):
    after a tick that sampled the latch open, closing it is seen at once by a reading outside a tick."""
    short_host(henv, monkeypatch)
    s, child = idle_host(henv)
    flag = {"open": True}
    henv.host.may_start = lambda: flag["open"]
    child.accept = 0
    henv.host.send(SID, "next", message_id="1")
    henv.host.tick()                                    # sampled (a budgeted staged line): open
    flag["open"] = False
    assert henv.host._gates(s) == (True, False) and not henv.host._counting(s, s.writing)

def test_c13_the_nodes_may_start_query_never_sets_the_latch(tmp_path):
    """node_sessions: `_may_start_query` answers as `_may_start` with no side effect: past the self-stop deadline
    it says no and leaves the latch unset; `_may_start` then sets it."""
    from test_hosted_spawn import _past_the_self_stop, _waiting_on_a_stopping_group
    env, proc, waiting = _waiting_on_a_stopping_group(tmp_path)
    runner = env.node.sessions
    assert runner.host.may_start_query == runner._may_start_query, "the host's accounting reads it"
    assert runner._may_start_query() is True and runner.latched_at is None
    _past_the_self_stop(env)
    assert runner._may_start_query() is False and runner.latched_at is None
    assert runner._may_start() is False and runner.latched_at is not None
    assert runner._may_start_query() is False


def test_c9_property_random_sequences_in_counted_time(henv, monkeypatch):
    """Seeded random walks (review rounds 6 to 8, and the follow-up after the switch-on) on a host with 2.1.330
    VERIFIED, timeout T = 60 s, over: sends; the live pipe taking nothing, everything, or a few bytes per write; the
    NEXT process's pipe starting so; the CLI's own turn opening and closing; a downgrade; the in-flight message's own
    result; an init reporting 2.1.329; a binding change; a delayed reap and its release; a process that exits by
    itself; a pipe that raises on write; a cancelled message; and the host-side gates: the turn gate, `may_start`,
    admission, a stopping group in the checkout, each closed and opened; with time steps from 1 s to past two
    timeouts. Then a drain (every gate open, groups and pipes released). Every process starts as a candidate.
    Asserted, at every pass's budget step (a wrapper of `_budget` records what the host decided), against this
    test's OWN reading of both ends of every interval (from the flags it set and the session's plain fields, never
    the host's `_counting`; a host gate closed withholds a staged message too, review round 10):
    - the host's count equals this test's: an interval is added iff it was counted at both ends, and then whole;
    - the budget acts (the 7b restart, the failure, or the drop of an unearned one) on the FIRST pass at which this
      test's total reaches T, never before and never a pass late; the 7b only for a message seen in a counted state
      under a verified process (line by line, and at each pass's end); no budget exceeds T plus one step;
    - a staged zero-byte message always has a clock that can end it (review rounds 11 and 12; the generator has the
      latch, `may_start` false with the pipe taking the line again or not): at no pass's end is it still staged T or
      more after its final staging unless it is earned and in its counted state; it is never delivered on a pass at
      which it is overdue unless its earned budget is that pass's clock; and an unearned staged message's write-timer
      failure comes at main's time exactly (the first pass at or after its staging plus T);
    - at every pass's end, `counted_at` is that pass's time exactly when the message is in a counted state as this
      test sees it (staged with no byte accepted in a live process; or first in the inbox, a live process, every
      gate open, the CLI's turn open);
    and for every message sent: exactly one outcome with its time (none skipped, no `ended`); written whole at most
    once over all processes, never after a message sent behind it, written iff delivered; its bytes taken by ONE
    process only (the fake's write records which process took each message's first byte, C9-01); and once a byte of
    it was taken, or if it never had a budget, its outcome by P(s + T), s its final staging (main's write timer).
    A failure names the seed and every step."""
    import bisect
    import random
    from types import SimpleNamespace
    from agent_comms.session_host import SessionHost
    from test_dispatch import FakeProcs, FakeSpawner
    monkeypatch.setattr(claude, "VERIFIED", frozenset({V}))
    T, STEPS = 60.0, [1, 5, 5, 5, 10, 30, 65, 125]
    feed, budget = ClaudeSession.feed, SessionHost._budget
    watch = {"see": lambda: None, "pre": lambda m, now: None, "obs": lambda rec: None, "holder": lambda: None}

    def feeding(self, line):
        got = feed(self, line)
        watch["see"]()      # verification is observed line by line, as the host observes it
        return got

    def budgeting(self, s, now, out):
        m = watch["holder"]()       # this test's own choice of the message whose budget can run (not `_holder`)
        rec = watch["pre"](m, now) if m is not None else None       # ... and its own reading
        acted = budget(self, s, now, out)
        if rec is not None:
            rec.update(acted=acted, after=rec["msg"].counted, recovered_after=rec["msg"].recovered)
            watch["obs"](rec)
        return acted

    monkeypatch.setattr(ClaudeSession, "feed", feeding)
    monkeypatch.setattr(SessionHost, "_budget", budgeting)
    for seed in range(200):
        rnd = random.Random(seed)
        start = {"accept": None}
        took = {}                                       # message -> the processes that took bytes of its line

        class Child(FakeSessionChild):
            def __init__(self, pid, exits_on_terminate=True):
                super().__init__(pid, exits_on_terminate)
                self.accept = start["accept"]

            def write(self, data):
                tail = self.stdin[self.stdin.rfind(b"\n") + 1:]
                n = super().write(data)
                seg, pos = tail + data, 0
                for line in seg.split(b"\n"):
                    if pos < len(tail) + n and pos + len(line) + 1 > len(tail):     # it took bytes of this line
                        named = re.search(rb'"content": "text (m\d)"', line)
                        if named:
                            took.setdefault(named.group(1).decode(), set()).add(self.pid)
                    pos += len(line) + 1
                return n

        henv.groups.clear()
        henv.spawner = FakeSpawner(child=Child)
        henv.procs = FakeProcs(henv.spawner)
        henv.host = make_host(henv, timeout_minutes=T / 60)
        flags = {"gate": True, "may": True, "slot": True, "groups": True}
        henv.host.turn_gate = lambda sess: flags["gate"]
        henv.host.may_start = lambda: flags["may"]
        henv.host.admit = lambda sess: flags["slot"]
        monkeypatch.setattr(henv.host.groups, "blocked", lambda cwd: not flags["groups"])
        s = henv.host.open(SID, agent="claude", runtime="claude-code", project="/work/repo")
        sent, outcome, extra, steps, pass_times = [], {}, [], [], []
        staged, seen_verified, last_holder, peak, budget_clock = {}, set(), [None], {}, {}
        progressed, end_state, totals = {}, {}, {}

        def holder():
            m = s.writing if s.writing is not None and s.turn_at is not None else (
                s.inbox[0] if s.writing is None and s.in_flight is None and s.inbox else None)
            return m if m is not None and m.counted is not None else None

        def counted_state(m):
            """This test's own reading, from the flags it set and the session's plain fields (never the host's
            `_counting`): is `m` held back by the agent now, with nothing on the host side withholding it?"""
            if s.child is None or s.stopping_since is not None or not flags["gate"] or not flags["may"] or s.checking:
                return False
            if m is s.writing:
                return s.turn_at is not None and not s.unbound
            return s.writing is None and s.in_flight is None and s.inbox and s.inbox[0] is m and (
                s.provider.holds_turns())

        def see():
            m = holder()
            if m is not None and counted_state(m) and s.provider.verified:
                seen_verified.add(m.id)

        def pre(m, now):
            see()
            mine, earned = counted_state(m), m.id in seen_verified
            budget_clock[(m.id, now)] = mine and earned     # the budget is this pass's clock (else the write timer)
            return {"t": now, "id": m.id, "mine": mine, "earned": earned, "before": m.counted,
                    "recovered": m.recovered, "msg": m}

        def observe(rec):
            """Both ends of the interval read by this test (the end of the previous pass, `end_state`; now, `pre`),
            its own total per message and budget, and the budget's action due on the FIRST pass that reaches T."""
            t, m, key = rec["t"], rec["id"], (rec["id"], rec["recovered"])
            earlier = pass_times[-2] if len(pass_times) >= 2 else None
            counts = rec["mine"] and earlier is not None and end_state.get(m) == earlier
            if abs(rec["before"] - totals.setdefault(key, 0.0)) > 1e-6:
                extra.append((m, "the host's count is not this test's", rec["before"], totals[key], t))
            total = totals[key] + ((t - earlier) if counts else 0.0)
            totals[key] = total
            due = rec["mine"] and rec["earned"] and total >= T      # an unearned budget never acts (round 12)
            if due and not rec["acted"]:
                extra.append((m, "not acted on at the first pass that reaches T", total, t, dict(flags)))
            if rec["acted"] and not due:
                extra.append((m, "acted on though not due", total, rec["mine"], rec["earned"], t, dict(flags)))
            if not rec["acted"] and rec["after"] != total:
                extra.append((m, "the host's count other than the interval (or dropped)", rec["after"], total, t))
            if rec["recovered_after"] and not rec["recovered"] and m not in seen_verified:
                extra.append((m, "a 7b restart it never earned", t))
            peak[key] = max(peak.get(key, 0.0), total)
        watch["see"], watch["pre"], watch["obs"], watch["holder"] = see, pre, observe, holder

        def record(mid, kind, detail=""):
            if mid in outcome:
                extra.append((mid, kind, henv.clock()))
            else:
                outcome[mid] = (kind, henv.clock(), detail)

        def tick():
            pass_times.append(henv.clock())
            for e in henv.host.tick():
                if e.kind == "delivered":
                    record(e.message_id, "delivered")
                elif e.kind == "turn_ended" and not e.delivered:
                    record(e.message_id, "failed", e.detail)
                elif e.kind == "turn_ended" and outcome.get(e.message_id, ("?",))[0] != "delivered":
                    extra.append((e.message_id, "ended, never delivered", henv.clock()))
                elif e.kind == "ended":
                    extra.append(("the session", "ended", e.detail, henv.clock()))
            if s.writing is not None:
                if last_holder[0] != (id(s.writing), id(s.child)):
                    staged[s.writing.id] = henv.clock()         # (re)staged in this pass: its write clock starts
                if s.turn_at is None:
                    progressed.setdefault(s.writing.id, henv.clock())
            last_holder[0] = (id(s.writing), id(s.child)) if s.writing is not None else None
            for q in s.inbox:
                staged.pop(q.id, None)                      # back in the inbox: no staging until the next
            w = s.writing
            if (w is not None and s.turn_at is not None and s.child is not None and s.stopping_since is None
                    and not (counted_state(w) and w.id in seen_verified)
                    and henv.clock() - staged.get(w.id, henv.clock()) >= T):
                # Review rounds 11 and 12: a staged zero-byte message always has a clock that can end it: an earned
                # one's budget while its counted state holds, main's write timer from the staging otherwise.
                extra.append((w.id, "staged, zero bytes, with no clock that ends it", staged[w.id], henv.clock(),
                              dict(flags)))
            m = holder()
            end_state.clear()
            if m is not None:
                want = henv.clock() if counted_state(m) else None
                end_state[m.id] = want                  # one end of the next interval, as this test reads it
                if m.counted_at != want:
                    extra.append((m.id, "the pass's end mark", m.counted_at, want, dict(flags)))
            see()
            for q in list(s.inbox) + [s.writing if s.turn_at is not None else None]:
                # (a message written whole within the pass that staged it may be earned there unseen: it has no
                # budget left to matter)
                if q is not None and q.counted is not None and q.earned != (q.id in seen_verified):
                    # Review round 13: the host earns a budget exactly when this test, from its own flags (the
                    # tick's) and each line read, sees the message in a counted state under a verified process.
                    extra.append((q.id, "earned" if q.earned else "not earned", "though this test saw otherwise",
                                  henv.clock(), dict(flags)))

        def proc():
            return henv.spawner.children[-1] if henv.spawner.children and s.child is not None else None

        for k in range(70):
            p = proc()
            action = rnd.choice(["send", "send", "zero", "open", "partial", "next", "bracket", "bracket", "close",
                                 "downgrade", "own", "other", "bind", "linger", "release", "exit", "broken",
                                 "cancel", "gate", "may", "slot", "groups", "latch", "latch", "flip", "flip", "flip"])
            arg = None
            if action == "send" and len(sent) < 6:
                arg = f"m{len(sent)}"
                sent.append(arg)
                henv.host.send(SID, f"text {arg}", message_id=arg)
            elif action == "zero" and p is not None:
                p.accept = 0
            elif action == "open" and p is not None:
                p.accept = None
            elif action == "partial" and p is not None:
                p.accept = arg = rnd.choice([1, 3, 7])
            elif action == "next":
                start["accept"] = arg = rnd.choice([None, 0, 3])
            elif action == "bracket" and p is not None:
                p.emit(vinit())
            elif action == "close" and p is not None:
                p.emit(foreign())
            elif action == "downgrade" and p is not None:
                p.emit(plain())
            elif action == "own" and p is not None and s.in_flight is not None:
                u = uuid_of(p)
                p.emit(*((vinit(u), owned(u)) if u else (plain(),)))
            elif action == "other" and p is not None:
                p.emit(vinit(version="2.1.329"))
            elif action == "bind":
                s.bind = None if s.bind else "e" * 64
            elif action == "linger" and p is not None:
                henv.groups.add(p.pid)
            elif action == "release":
                henv.groups.clear()
            elif action == "exit" and p is not None:
                p.code = 1                                  # it exits by itself: the turn it had is lost (main's)
            elif action == "broken" and p is not None:
                p.broken = True                             # its next write raises: the message fails (main's)
            elif action == "cancel" and sent:
                arg = rnd.choice(sent)
                for e in henv.host.cancel(SID, arg, "cancelled"):
                    if e.kind == "turn_ended" and not e.delivered:
                        record(e.message_id, "failed")
            elif action in flags:
                flags[action] = arg = not flags[action]
            elif action == "flip" and p is not None:
                # Review round 13: the latch changes and, in the same tick's first drain, a qualifying init and a
                # downgrade arrive together.
                flags["may"] = arg = not flags["may"]
                p.emit(vinit(), plain())
            elif action == "latch":
                # Latched but not silent (review round 11): `may_start` false while the watchdog sees contact; the
                # pipe then takes the staged line again (main still writes a staged line then), or still takes none.
                flags["may"] = False
                arg = rnd.choice(["reopen", "blocked"])
                if p is not None:
                    p.accept = None if arg == "reopen" else 0
            dt = rnd.choice(STEPS)
            steps.append((action, arg, dt))
            henv.clock.advance(dt)
            tick()
        for key in flags:
            flags[key] = True
        start["accept"] = None
        henv.groups.clear()
        if proc() is not None:
            proc().accept = None
        for _ in range(150):                            # drain: 750 s, no new frames
            henv.clock.advance(5)
            tick()

        def first_pass(x):
            i = bisect.bisect_left(pass_times, x - 1e-9)
            return pass_times[i] if i < len(pass_times) else float("inf")

        try:
            assert not extra, ("an invariant", extra)
            assert sorted(outcome) == sorted(sent), ("every message one outcome", sorted(outcome), sent)
            assert all(v <= T + max(STEPS) for v in peak.values()), ("a budget past T and a step", peak)
            assert all(len(pids) == 1 for pids in took.values()), ("bytes of one message on two processes", took)
            texts = [json.loads(x)["message"]["content"] for c in henv.spawner.children
                     for x in c.stdin.split(b"\n")[:-1] if json.loads(x).get("type") == "user"]
            for c in henv.spawner.children:
                # Whole lines, each one object; a process stopped (or gone) in the middle of a line may end with that
                # line's beginning, never with anything appended to it.
                valid_stdin(SimpleNamespace(stdin=c.stdin[:c.stdin.rfind(b"\n") + 1]))
                assert c.stdin.endswith(b"\n") or c.stdin == b"" or c.terminated or c.killed or (
                    c.code is not None), "only a stopped or exited one"
            order = [t.split(" ", 1)[1] for t in texts]
            assert len(order) == len(set(order)), ("written twice", order)
            assert order == [m for m in sent if m in order], ("out of order", order, sent)
            assert set(order) == {m for m, (kind, _, _) in outcome.items() if kind == "delivered"}, (
                "written iff delivered", order, outcome)
            budgeted = {key[0] for key in peak}
            for m in sent:
                kind, at, detail = outcome[m]
                if m in staged and kind == "delivered" and at >= first_pass(staged[m] + T) and not budget_clock.get(
                        (m, at)):
                    # Review round 12: on the pass at which its line is overdue, main's write timer ends it before
                    # anything is written; only an earned budget in its counted state can be the clock instead.
                    assert False, (m, "delivered on a pass its write timer had it overdue", staged[m], at)
                if m in staged and m not in seen_verified:
                    # An unearned staged message is on main's path: whatever its outcome, never after its write
                    # timer's pass, and its write timer's failure exactly at main's time (review round 13).
                    assert at <= first_pass(staged[m] + T), (m, kind, "an unearned line ended after main's write timer",
                                                              staged[m], at)
                    if detail == henv.host._timeout_detail():
                        assert at == first_pass(staged[m] + T), (m, "an unearned line's timeout not at main's time",
                                                                  staged[m], at)
                if m in staged and m not in budgeted:
                    assert at <= first_pass(staged[m] + T), (m, kind, at, "main's write timer from", staged[m])
                elif m in staged and m in progressed:
                    # Its write timer runs from its staging, but only once its budget is over (a budgeted staged
                    # message is governed by its budget, which a host gate can hold still: review round 10).
                    bound = first_pass(max(staged[m] + T, progressed[m] + 1e-6))
                    assert at <= bound, (m, kind, at, "the write timer from", staged[m], "progress", progressed[m])
        except AssertionError as e:
            raise AssertionError(f"seed {seed}: {e}; steps {steps}") from None

# --- the barrier, its bound and its clock, the access check, interrupt readiness


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
def test_the_barrier_is_bounded_then_a_never_written_message_waits_for_the_next_process_once(henv):
    """Rule 7b: at the deadline the message, not one byte of which was written, is kept and the process is stopped;
    the next process takes it. Held again past a second deadline it fails as main's `_held` fails it."""
    s, child = idle_host(henv)
    emit(henv, child, vinit())
    henv.host.send(SID, "wait", message_id="1")
    assert henv.host.tick() == []
    henv.clock.advance(30 * 60)
    assert henv.host.tick() == [] and child.terminated == 1 and [m.id for m in s.inbox] == ["1"]
    assert b'"wait"' not in child.stdin
    henv.spawner.child = _ZeroPipe                     # the next process's pipe takes nothing at first
    henv.host.tick()                                    # the old group is gone: a new process, the message staged
    fresh = henv.spawner.children[-1]
    assert fresh is not child and s.writing.id == "1" and fresh.stdin == b""
    fresh.emit(vinit())                                 # ... and that process begins a turn of its own first
    henv.host.tick()
    fresh.accept = None
    assert henv.host.tick() == [] and fresh.stdin == b"", "behind the barrier again"
    henv.clock.advance(30 * 60)
    [end] = henv.host.tick()
    assert (end.message_id, end.delivered, end.ok) == ("1", False, False) and "in two periods of" in (
        end.detail), "once per message: the second time it fails as main's `_held` fails it"
    assert fresh.terminated == 1


@pytest.mark.usefixtures("verified")
def test_c1_01_the_barrier_is_rechecked_before_a_staged_turns_first_byte(henv):
    """A pipe that took nothing; then the CLI begins a turn of its own before the next attempt: the staged message
    is not written into that turn. It goes back to the inbox (review round 4) and waits there, and goes once the turn
    ended; or, at the bound, is kept for the next process."""
    s, child = idle_host(henv)
    child.accept = 0
    henv.host.send(SID, "next", message_id="1")
    assert henv.host.tick() == [] and s.writing.id == "1" and b'"next"' not in child.stdin
    child.accept = None
    assert emit(henv, child, vinit()) == [] and b'"next"' not in child.stdin and s.writing is None
    assert [m.id for m in s.inbox] == ["1"]
    henv.clock.advance(60)
    assert henv.host.tick() == [] and b'"next"' not in child.stdin
    assert emit(henv, child, foreign()) == [("delivered", "1")] and child.turns()[-1] == "next"
    # ... and the bound: kept for the next process (never written), which writes it
    child.emit(vinit(uuid_of(child)), owned(uuid_of(child)))
    henv.host.tick()
    child.accept = 0
    henv.host.send(SID, "later", message_id="2")
    henv.host.tick()
    child.accept = None
    emit(henv, child, vinit())
    henv.clock.advance(30 * 60)
    assert henv.host.tick() == [] and b'"later"' not in child.stdin and child.terminated == 1
    assert [m.id for m in s.inbox] == ["2"] and s.writing is None
    henv.host.tick()
    assert henv.spawner.children[-1].turns() == ["later"]
    valid_stdin(*henv.spawner.children, once=["next", "later"])


@pytest.mark.usefixtures("verified")
def test_c1_01_a_line_already_partly_written_is_completed(henv):
    """It cannot be taken back: once its first byte is in the pipe, the rest follows even behind the barrier."""
    s, child = idle_host(henv)
    child.accept = 5
    henv.host.send(SID, "partial", message_id="1")
    assert henv.host.tick() == [] and 0 < len(child.stdin.split(b"\n")[-1]) == 5
    child.accept = None
    assert emit(henv, child, vinit()) == [("delivered", "1")] and child.turns()[-1] == "partial"


@pytest.mark.usefixtures("verified")
def test_c1_02_a_changed_binding_does_not_let_a_message_wait_without_bound(henv):
    """A new binding while the CLI runs a turn of its own: the host would restart the process for it at once, and
    waits only for the agent's turn, so that wait counts (the follow-up's counted time keeps C1-02): at the timeout
    the process is stopped and the message, never written, waits for the next one."""
    s, child = idle_host(henv)
    emit(henv, child, vinit())
    s.bind = "a" * 64                   # given since the process started: it must be restarted for the message
    henv.host.send(SID, "wait", message_id="1")
    assert henv.host.tick() == [] and child.terminated == 0, "not stopped while the CLI runs its own turn"
    henv.clock.advance(30 * 60)
    assert henv.host.tick() == [] and child.terminated == 1 and [m.id for m in s.inbox] == ["1"], (
        "bounded: the process is stopped and the message, never written, waits for the next one")


@pytest.mark.usefixtures("verified")
@pytest.mark.parametrize("same_bracket", [True, False])
def test_c1_02_a_cancelled_waiter_leaves_no_clock_for_the_next_message(henv, same_bracket):
    s, child = idle_host(henv)
    emit(henv, child, vinit())
    henv.host.send(SID, "first", message_id="1")
    henv.host.tick()
    henv.clock.advance(29 * 60)
    henv.host.cancel(SID, "1", "cancelled")
    if not same_bracket:
        emit(henv, child, foreign(), vinit())       # another turn of the CLI's own
    henv.host.send(SID, "second", message_id="2")
    assert henv.host.tick() == []
    henv.clock.advance(2 * 60)
    assert henv.host.tick() == [], "its own clock: it does not fail at once"
    assert [m.id for m in s.inbox] == ["2"] and child.terminated == 0, "nor is the process stopped for it"


@pytest.mark.usefixtures("verified")
def test_c2_03_the_deadline_belongs_to_the_continuously_blocked_message(henv):
    """C2-03: the CLI's turn ends and another begins in the same read, with no write opportunity between: the same
    message's deadline is kept (it does not wait without bound)."""
    s, child = idle_host(henv)
    emit(henv, child, vinit())
    henv.host.send(SID, "wait", message_id="1")
    henv.host.tick()
    henv.clock.advance(29 * 60)
    assert emit(henv, child, foreign(), vinit()) == []
    henv.clock.advance(60)
    assert henv.host.tick() == [] and child.terminated == 1, "its deadline, not a fresh one"
    assert [m.id for m in s.inbox] == ["1"]


@pytest.mark.usefixtures("verified")
def test_c5_01_a_write_opportunity_with_no_byte_accepted_does_not_restart_the_budget(henv):
    """Review round 5 (C5-01) replaces round 2's `test_c2_03_after_a_write_opportunity_the_clock_starts_again`: a
    pass at which the barrier lifted, and a write was tried that the pipe did not take, is no progress: the message
    keeps its one budget. Follow-up (counted time): the 29 minutes have no pass between their ends, and the pass at
    t=29 first saw the turn ended, so they count nothing; with passes every minute they count 28 (the minute before
    that pass is lost), and the 7b comes at t=31, not at a fresh budget's t=59."""
    s, child = idle_host(henv)
    child.accept = 0
    henv.host.send(SID, "wait", message_id="1")
    henv.host.tick()
    emit(henv, child, vinit())                      # staged at t=0, unstaged: blocked from t=0
    for _ in range(28):
        henv.clock.advance(60)
        assert henv.host.tick() == []
    henv.clock.advance(60)
    assert emit(henv, child, foreign()) == []       # t=29: the barrier lifted, a write was tried (nothing taken)
    assert emit(henv, child, vinit()) == []         # unstaged again at t=29
    assert s.inbox[0].counted == 28 * 60
    henv.clock.advance(60)
    assert henv.host.tick() == [] and child.terminated == 0
    henv.clock.advance(60)
    assert henv.host.tick() == [] and child.terminated == 1 and [m.id for m in s.inbox] == ["1"], "7b at t=31"
    assert b'"wait"' not in child.stdin


@pytest.mark.usefixtures("verified")
def test_a_message_written_just_before_such_a_turn_began_is_not_taken_back(henv):
    s, child = idle_host(henv)
    started(henv)
    emit(henv, child, vinit())
    assert s.in_flight.id == "1" and s.provider.holds_turns()
    henv.host.send(SID, "next", message_id="2")
    assert henv.host.tick() == [] and [m.id for m in s.inbox] == ["2"]


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


STAND_IN_V = frozenset({"2.1.330-stand-in"})


@pytest.mark.parametrize("verify", [True, False])
def test_real_pipes_a_turn_the_cli_starts_itself_is_no_messages(tmp_path, monkeypatch, verify):
    """bg-late through the stand-in: message 1's turn is followed by a turn the CLI starts itself; message 2 is
    written right after 1 ends, before that turn begins, and runs after it. Verified: 2's result is its own. With
    VERIFIED empty: main's behaviour, unchanged (the CLI's frames are taken for 2, 2's own dropped)."""
    monkeypatch.setattr(claude, "VERIFIED", STAND_IN_V if verify else frozenset())
    host, s = real_host(tmp_path, monkeypatch, stand_in(), timeout_seconds=30, grace=2)
    try:
        first = host.send(SID, "bg 0.4 0.3 one")
        ends_of(host, [first])
        second = host.send(SID, "two")
        got = ends_of(host, [second])[second]
        assert got.text == ("turn 2: TWO" if verify else "background: FINISHED")
        # C2-05: wait for the real pair (the CLI's turn and message 2's own) and settle nothing twice
        later, deadline = [], time.monotonic() + 20
        while time.monotonic() < deadline and len([x for x in transcript(tmp_path) if x.get("type") == "result"]) < 3:
            later += host.tick()
            time.sleep(0.02)
        later += host.tick()
        frames = transcript(tmp_path)
        assert [x.get("subtype") for x in frames if x.get("type") == "system" and x.get("subtype") == "init"] == [
            "init"] * 3 and len([x for x in frames if x.get("type") == "result"]) == 3, "the real pair was recorded"
        assert not [e for e in later if e.kind == "turn_ended"], "no second settlement"
        inits = [x for x in transcript(tmp_path) if x.get("subtype") == "init"]
        assert [bool(x.get("user_message_uuids")) for x in inits] == ([True, False, True] if verify
                                                                      else [False, False, False])
    finally:
        host.stop_all()


def test_real_pipes_echo_fields_reach_the_transcript_and_every_line_carries_a_uuid(tmp_path, monkeypatch):
    monkeypatch.setattr(claude, "VERIFIED", STAND_IN_V)
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
    monkeypatch.setattr(claude, "VERIFIED", STAND_IN_V)
    host, s = real_host(tmp_path, monkeypatch, stand_in(STAND_IN_REFUSE_RESUME="1"), timeout_seconds=30, grace=2)
    s.resume = True
    try:
        m = host.send(SID, "hello")
        got = ends_of(host, [m])[m]
        assert (got.ok, got.answered) == (False, True)
    finally:
        host.stop_all()


def test_real_pipes_zero_turn_notifications_and_a_reinit_settle_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(claude, "VERIFIED", STAND_IN_V)
    host, s = real_host(tmp_path, monkeypatch, stand_in(), timeout_seconds=30, grace=2)
    try:
        a = host.send(SID, "notify-in notify-after reinit one")
        assert ends_of(host, [a])[a].text == "turn 1: NOTIFY-IN NOTIFY-AFTER REINIT ONE"
        assert s.provider.verified
        b = host.send(SID, "notify-error-in two")
        got = ends_of(host, [b])[b]
        assert (got.ok, got.text) == (False, ""), "rule 10: the zero-turn error is the turn's by order"
        assert not s.provider.verified
    finally:
        host.stop_all()


def test_real_pipes_a_process_that_stops_echoing_falls_back_to_order(tmp_path, monkeypatch):
    monkeypatch.setattr(claude, "VERIFIED", STAND_IN_V)
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
    monkeypatch.setattr(claude, "VERIFIED", STAND_IN_V)
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


def test_parity_with_verified_empty_the_host_tracks_no_turn_start_and_tells_no_delivery(henv, monkeypatch):
    """Sol's round-3 parity residue: with VERIFIED empty the host's write path for Claude is main's (no `turn_at`,
    no `delivered()`), and Codex never gets either."""
    calls = []
    monkeypatch.setattr(claude, "VERIFIED", frozenset())
    monkeypatch.setattr(ClaudeSession, "delivered", lambda self: calls.append("delivered"))
    henv.spawner.child = _ZeroPipe                      # the pipe takes nothing at first: the turn stays staged
    s = open_session(henv)
    henv.host.send(SID, "first", message_id="1")
    henv.host.tick()
    assert s.writing.id == "1" and s.turn_at is None, "no turn start tracked while it is staged"
    henv.spawner.children[-1].accept = None
    henv.host.tick()
    assert s.turn_at is None and s.in_flight.id == "1" and calls == [] and not s.provider.barrier_before_first_byte
    from test_session_host import codex_host, codex_ready
    henv.spawner.child = FakeSessionChild
    host, cs = codex_host(henv)
    host.send(SID, "first", message_id="c")
    host.tick()
    codex_ready(host, henv.spawner.children[-1])
    host.tick()
    assert cs.turn_at is None and cs.in_flight is not None and not hasattr(cs.provider, "barrier_before_first_byte")
    monkeypatch.setattr(claude, "VERIFIED", frozenset({V}))
    assert ClaudeSession().barrier_before_first_byte is True
