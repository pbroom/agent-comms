"""Claude turn correlation (DESIGN_NOTES "Claude turn correlation (2026-11-24)"; the steering design on branch
claude/steer-loose, its part C, step 1, with the review's rule changes): while some Claude Code version is VERIFIED,
host user lines carry a fresh uuid, and in a verified process incarnation the adapter attributes each `init` and
`result` by the evidence it carries (the bracket table), not by order. VERIFIED ships empty, so every real Claude
process today gets main's bytes and is attributed by order, as on main.

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
from agent_comms.session_host import INTERRUPTED

from test_session_host import ROOT, SID, henv, open_session, real_host, result  # noqa: F401

V = "2.1.330"                                       # the version the tests verify (VERIFIED itself ships empty)
CAPS = ["msg_lifecycle_v1", "interrupt_receipt_v1"]
NOTIFY = {"kind": "task-notification", "producer": "session-task"}
UUID_KEY = re.compile(rb', "uuid": "[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}"\}\n$')


@pytest.fixture
def verified(monkeypatch):
    """A build in which version V passed the release gates (VERIFIED is empty in this one)."""
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


def bracket(state: str, written=True):
    """(adapter, M's uuid or None) with M written (unless not `written`) and the bracket in this state."""
    p = idle_verified()
    m = write(p) if written else None
    if state == "own":
        fed(p, vinit(m))
    elif state == "unattributed":
        fed(p, vinit())
    assert p._bracket == {"none": None, "own": OWN, "unattributed": UNATTRIBUTED}[state]
    return p, m


def settled_uuid(p, m):
    [old] = list(p._written - {m})
    return old


# --- C1-05: a uuid only while verification is possible


def test_verified_ships_empty_and_lines_are_mains_bytes_and_attributed_by_order():
    assert claude.VERIFIED == frozenset(), "a version goes in only once its release gates passed"
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
    before = (p._bracket, p.holds_turns(), p._brackets)
    assert fed(p, vinit(settled_uuid(p, m))) == [("turn_started", "other")]
    assert (p._bracket, p.holds_turns(), p._brackets) == before and p.verified


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


def test_seq_unverified_an_unattributed_turn_before_ms_own_pair_is_mains(henv):
    """VERIFIED empty: the same frames are attributed by order, exactly as on main."""
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
def test_c1_01_the_barrier_is_rechecked_before_a_staged_turns_first_byte(henv):
    """A pipe that took nothing; then the CLI begins a turn of its own before the next attempt: the staged message
    is not written into that turn. It waits (its own clock), and goes once the turn ended; or fails at the bound."""
    s, child = idle_host(henv)
    child.accept = 0
    henv.host.send(SID, "next", message_id="1")
    assert henv.host.tick() == [] and s.writing.id == "1" and b'"next"' not in child.stdin
    child.accept = None
    assert emit(henv, child, vinit()) == [] and b'"next"' not in child.stdin and s.writing.id == "1"
    henv.clock.advance(60)
    assert henv.host.tick() == [] and b'"next"' not in child.stdin
    assert emit(henv, child, foreign()) == [("delivered", "1")] and child.turns()[-1] == "next"
    # ... and the bound
    child.emit(vinit(uuid_of(child)), owned(uuid_of(child)))
    henv.host.tick()
    child.accept = 0
    henv.host.send(SID, "later", message_id="2")
    henv.host.tick()
    child.accept = None
    emit(henv, child, vinit())
    henv.clock.advance(30 * 60)
    [end] = henv.host.tick()
    assert (end.message_id, end.delivered, end.ok) == ("2", False, False) and b'"later"' not in child.stdin
    assert child.terminated == 1


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
    s, child = idle_host(henv)
    emit(henv, child, vinit())
    s.bind = "a" * 64                   # given since the process started: it must be restarted for the message
    henv.host.send(SID, "wait", message_id="1")
    assert henv.host.tick() == [] and child.terminated == 0, "not stopped while the CLI runs its own turn"
    henv.clock.advance(30 * 60)
    [end] = henv.host.tick()
    assert (end.message_id, end.delivered, end.ok) == ("1", False, False) and child.terminated == 1


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
    assert [m.id for m in s.inbox] == ["2"]


@pytest.mark.usefixtures("verified")
def test_c1_02_the_same_waiter_behind_another_cli_turn_gets_a_fresh_clock(henv):
    """The CLI's turn ends and another begins before the waiting message could be written: its wait starts again."""
    s, child = idle_host(henv)
    emit(henv, child, vinit())
    henv.host.send(SID, "wait", message_id="1")
    henv.host.tick()
    henv.clock.advance(29 * 60)
    assert emit(henv, child, foreign(), vinit()) == []
    henv.clock.advance(2 * 60)
    assert henv.host.tick() == [] and [m.id for m in s.inbox] == ["1"]
    henv.clock.advance(28 * 60)
    [end] = henv.host.tick()
    assert (end.message_id, end.delivered) == ("1", False)


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
    VERIFIED empty (as shipped): main's behaviour, unchanged (the CLI's frames are taken for 2, 2's own dropped)."""
    if verify:
        monkeypatch.setattr(claude, "VERIFIED", STAND_IN_V)
    host, s = real_host(tmp_path, monkeypatch, stand_in(), timeout_seconds=30, grace=2)
    try:
        first = host.send(SID, "bg 0.4 0.3 one")
        ends_of(host, [first])
        second = host.send(SID, "two")
        got = ends_of(host, [second])[second]
        assert got.text == ("turn 2: TWO" if verify else "background: FINISHED")
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
