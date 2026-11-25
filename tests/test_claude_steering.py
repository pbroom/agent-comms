"""Mid-turn steering, step 3: Claude steering (DESIGN_NOTES "Mid-turn steering, step 3: Claude steering, as built").

The host and the adapter, driven with the frames gate M8 measured on Claude Code 2.1.330 (scratchpad steer3/runs):
S taken in at a tool boundary (`in_turn`), S queued past N's last boundary and run as its own turn after N's end
(`next_turn`, S's own `init` echoing exactly [S]), S consumed by a turn the CLI started itself (`unconfirmed`), N's end
held, the checkpoint, the finalizer and the bounded publication. Every steer here goes through step 2's slot, write
boundary and outcome rules: nothing of those is copied."""
import json

import pytest

from agent_comms.providers import claude
from agent_comms.providers.claude import ClaudeSession
from agent_comms.session_host import STEER_UNATTRIBUTED
from test_claude_correlation import (NOTIFY, V, emit, foreign, idle_host, nonturn, owned, plain, result, short_host,
                                     uuid_of, vinit)
from test_session_host import SID, henv, make_host, open_session  # noqa: F401

S = "5e7e7e7e-0000-4000-8000-00000000000s".replace("s", "5")
TEXT = "Change of plan from me: include the codeword PINEAPPLE in your reply."


def steered(henv, monkeypatch, timeout=1, grace=None):
    """A verified session with N (message "1") in flight in its owned bracket and S offered and written whole:
    (session, child, N's uuid)."""
    monkeypatch.setattr(claude, "VERIFIED", frozenset({V}))
    henv.host = make_host(henv, timeout_minutes=timeout)
    if grace is not None:
        henv.host.kill_grace = grace
    henv.host.steer_authority = lambda sess: True
    s, child = idle_host(henv)
    henv.host.send(SID, "a long turn", message_id="1")
    assert ("delivered", "1") in [(e.kind, e.message_id) for e in henv.host.tick()]
    n = uuid_of(child)
    assert emit(henv, child, vinit(n)) == [("turn_started", "1")]
    assert "steer" in henv.host.modes(SID), "a verified 2.1.330 incarnation steers"
    henv.host.steer_offer(SID, "1", S, TEXT, authorized=True)
    henv.host.tick()
    line = json.loads(child.stdin.split(b"\n")[-2])
    assert line == {"type": "user", "message": {"role": "user", "content": TEXT}, "uuid": S}
    return s, child, n


def ends(events, mid="1"):
    return [e for e in events if e.kind == "turn_ended" and e.message_id == mid]


# ---------------------------------------------------------------- who steers


def test_only_a_verified_incarnation_of_an_m8_version_steers(monkeypatch):
    """`steers` is the incarnation's: verified, and its version in STEER_MODES (gate M8's record). Another verified
    version (in VERIFIED, not in STEER_MODES) never steers; nor an unverified one; nor the class as such."""
    monkeypatch.setattr(claude, "VERIFIED", frozenset({V, "2.1.331"}))
    assert ClaudeSession.steers is False
    for version, steers in ((V, True), ("2.1.331", False), ("2.1.329", False)):
        p = ClaudeSession()
        p.argv(["claude", "-p", "{prompt}"], session_id=SID, resume=False, model=None)
        p.turn("x")
        p.feed(json.dumps(vinit(version=version)).encode())
        assert p.steers is steers, version
    assert claude.STEER_MODES == {"2.1.330": "next_turn"}, "M8's record on 2.1.330: the four outcomes"


# ---------------------------------------------------------------- at N's native end


def test_in_turn_n_s_result_lists_its_own_uuid_and_s(henv, monkeypatch):
    """M8 (a), measured: S written during N's tool call is read at that boundary: N's result lists [N, S]."""
    s, child, n = steered(henv, monkeypatch)
    child.emit(owned(n, S, text="DONE PINEAPPLE"))
    [end] = ends(henv.host.tick())
    assert end.ok and end.text == "DONE PINEAPPLE" and end.steer == {"outcome": "in_turn", "why": "answered"}
    assert s.held is None and s.steer is None and not s.busy


def test_not_delivered_n_ended_before_s_was_written(henv, monkeypatch):
    """N's native end before any byte of S went: `not_delivered`, at once (`not_owned`: no pass saw the slot with
    N's bracket proved, step 2's rule)."""
    monkeypatch.setattr(claude, "VERIFIED", frozenset({V}))
    henv.host = make_host(henv, timeout_minutes=1)
    henv.host.steer_authority = lambda sess: True
    s, child = idle_host(henv)
    henv.host.send(SID, "a turn", message_id="1")
    henv.host.tick()
    n = uuid_of(child)
    child.emit(vinit(n))
    henv.host.tick()
    henv.host.steer_offer(SID, "1", S, TEXT, authorized=True)
    child.emit(owned(n))
    [end] = ends(henv.host.tick())
    assert end.steer == {"outcome": "not_delivered", "why": "not_owned"} and S.encode() not in child.stdin


def test_s_is_never_written_before_ns_own_init(henv, monkeypatch):
    """N is the turn in flight but its own `init` has not come: its bracket is not proved N's, so no byte of S goes;
    N's end then says `not_delivered` (`not_owned`)."""
    monkeypatch.setattr(claude, "VERIFIED", frozenset({V}))
    henv.host = make_host(henv, timeout_minutes=1)
    henv.host.steer_authority = lambda sess: True
    s, child = idle_host(henv)
    henv.host.send(SID, "a turn", message_id="1")
    henv.host.tick()
    n = uuid_of(child)
    henv.host.steer_offer(SID, "1", S, TEXT, authorized=True)
    henv.host.tick()
    assert S.encode() not in child.stdin
    child.emit(vinit(n), owned(n))
    [end] = ends(henv.host.tick())
    assert end.steer == {"outcome": "not_delivered", "why": "not_owned"}


def test_next_turn_s_ran_as_its_own_turn_after_ns_end(henv, monkeypatch):
    """M8 (b), (c), (e), measured: S past N's last boundary. N's result lists only N: N's end is held (a checkpoint,
    no end); then an `init` echoing exactly [S] opens S's own turn, and its result ends the hold: N's native end,
    unaltered, with `next_turn` and S's own result."""
    s, child, n = steered(henv, monkeypatch)
    child.emit(owned(n, text="the story. DONE"))
    out = henv.host.tick()
    assert not ends(out) and [(e.kind, e.message_id, e.text) for e in out] == [("held", "1", "the story. DONE")]
    assert s.held is not None and s.busy and s.in_flight.id == "1"
    child.emit(vinit(S))
    assert henv.host.tick() == [] and s.held.s_turn
    child.emit(owned(S, text="PINEAPPLE"))
    [end] = ends(henv.host.tick())
    assert end.ok and end.text == "the story. DONE"
    assert end.steer["outcome"] == "next_turn" and end.steer["result"]["text"] == "PINEAPPLE"
    assert end.steer["result"]["ok"] is True and s.held is None and not s.busy and s.steer is None


def test_next_turn_after_an_error_end_of_n(henv, monkeypatch):
    """M8 (e), measured: N ends in an error (`error_max_turns`, an interrupt's abort) listing only N; S then runs as
    its own turn. N's end is its own error, unaltered; S's result is S's."""
    s, child, n = steered(henv, monkeypatch)
    child.emit(owned(n, subtype="error_max_turns", is_error=True, text=""))
    assert [e.kind for e in henv.host.tick()] == ["held"]
    child.emit(vinit(S), owned(S, text="DONE PINEAPPLE"))
    [end] = ends(henv.host.tick())
    assert end.ok is False and end.meta.get("subtype") == "error_max_turns"
    assert end.steer["outcome"] == "next_turn" and end.steer["result"]["ok"] is True


def test_s_consumed_by_a_turn_the_cli_started_itself_is_unconfirmed(henv, monkeypatch):
    """M8 (d), measured (cli_take): a CLI-started turn takes S in at its tool boundary; its TERMINAL result lists S
    (origin task-notification). S was consumed, by which turn first is not known: `unconfirmed`, the hold ends."""
    s, child, n = steered(henv, monkeypatch)
    child.emit(owned(n))
    henv.host.tick()
    child.emit(vinit())                                 # an unmarked init: the CLI's own turn
    assert henv.host.tick() == [] and s.held is not None
    child.emit(owned(S, text="NOTED PLUM") | {"origin": NOTIFY})
    [end] = ends(henv.host.tick())
    assert end.steer == {"outcome": "unconfirmed", "why": "consumed"} and s.held is None


def test_an_unattributed_bracket_ending_without_s_keeps_the_hold(henv, monkeypatch):
    """M8 (d), measured (cli_late, race): a turn the CLI started itself ends without listing S: nothing for S; then
    S's own turn follows, `next_turn` (a notification that competes never takes S's turn)."""
    s, child, n = steered(henv, monkeypatch)
    child.emit(owned(n))
    henv.host.tick()
    child.emit(vinit(), foreign(text="NOTED"))
    assert henv.host.tick() == [] and s.held is not None
    child.emit(vinit(S), owned(S, text="NOTED PLUM"))
    [end] = ends(henv.host.tick())
    assert end.steer["outcome"] == "next_turn" and end.steer["result"]["text"] == "NOTED PLUM"


def test_a_zero_turn_notification_in_the_hold_is_inert(henv, monkeypatch):
    s, child, n = steered(henv, monkeypatch)
    child.emit(owned(n))
    henv.host.tick()
    child.emit(nonturn(), nonturn(error=True))
    assert henv.host.tick() == [] and s.held is not None


def test_an_s_only_result_in_ns_own_bracket_is_ns_and_s_is_unconfirmed(henv, monkeypatch):
    """Part D's S-only case (never seen in M8's 30 runs): a result listing only S inside N's owned bracket, opened by
    N's own `init` with no competing `init` noted: N's native result, S `unconfirmed`, N ends at once."""
    s, child, n = steered(henv, monkeypatch)
    child.emit(owned(S, text="S only"))
    [end] = ends(henv.host.tick())
    assert end.text == "S only" and end.steer == {"outcome": "unconfirmed", "why": "s_only"} and s.held is None


def test_an_s_only_result_after_a_competing_init_is_nobodys_and_n_fails_at_its_bound(henv, monkeypatch):
    """Part D, G-3: a competing `init` was noted in N's bracket, so an S-only result there cannot be N's: it is
    nobody's, the incarnation is unverified, and with no result of N's own, N fails at its bound with part A's
    third exception's words, S `unconfirmed`."""
    s, child, n = steered(henv, monkeypatch)
    child.emit(vinit(S))                                # a competing init inside N's bracket
    henv.host.tick()
    child.emit(owned(S, text="ambiguous"))
    assert not ends(henv.host.tick()) and not s.provider.verified
    henv.clock.advance(61)
    [end] = ends(henv.host.tick())
    assert end.ok is False and end.detail == STEER_UNATTRIBUTED and end.steer["outcome"] == "unconfirmed"


def test_a_plain_result_after_a_steer_is_never_ns_by_order(henv, monkeypatch):
    """G-3: once S went, N gets no result by order: a plain result in N's bracket is nobody's; N's own result,
    listing N, still settles it (the incarnation unverified meanwhile)."""
    s, child, n = steered(henv, monkeypatch)
    child.emit(plain(text="whose?"))
    assert not ends(henv.host.tick()) and not s.provider.verified
    child.emit(owned(n, S, text="mine"))
    [end] = ends(henv.host.tick())
    assert end.text == "mine" and end.steer == {"outcome": "in_turn", "why": "answered"}


# ---------------------------------------------------------------- the finalizer and the bounded publication


def test_the_bound_a_held_end_times_out_a_turn_timeout_after_ns_native_end(henv, monkeypatch):
    """The bound: the turn timeout from N's native end. Then the finalizer: S `unconfirmed` (no own turn proved), the
    process stopped, and N's native end published once the group is gone."""
    s, child, n = steered(henv, monkeypatch)
    henv.clock.advance(30)
    child.emit(owned(n, text="N's own"))
    henv.host.tick()
    henv.clock.advance(59)
    assert not ends(henv.host.tick()), "the bound runs from N's native end, not from N's start"
    henv.clock.advance(2)
    out = henv.host.tick()
    [end] = ends(out) or ends(henv.host.tick())
    assert end.ok and end.text == "N's own" and end.steer == {"outcome": "unconfirmed", "why": "turn_cut"}
    assert child.terminated


def test_the_fresh_clock_s_own_turn_gets_a_whole_turn_timeout(henv, monkeypatch):
    """Part O: a fresh S clock. S's own turn proved 50 s into the hold has its own turn timeout from its `init`; cut
    at that bound, it is `next_turn` with S failed ("its own turn was cut")."""
    s, child, n = steered(henv, monkeypatch)
    child.emit(owned(n))
    henv.host.tick()
    henv.clock.advance(50)
    child.emit(vinit(S))
    henv.host.tick()
    henv.clock.advance(30)                              # 80 s after N's end, 30 s into S's turn
    assert not ends(henv.host.tick()) and s.held is not None
    henv.clock.advance(31)
    out = henv.host.tick()
    [end] = ends(out) or ends(henv.host.tick())
    assert end.steer["outcome"] == "next_turn" and end.steer["result"]["ok"] is False
    assert end.steer["result"]["detail"].startswith("its own turn was cut: timed out")


def test_the_bounded_publication_a_lingering_group_delays_ns_end_at_most_two_graces(henv, monkeypatch):
    """Part O (R11-01): a group that outlives SIGKILL. N's native end is published once, 2 x kill_grace after the
    stop, S `unconfirmed`; the process stays unwritable and main's stopping barrier holds the checkout."""
    s, child, n = steered(henv, monkeypatch, grace=10)
    child.exits_on_terminate = False
    child.emit(owned(n))
    henv.host.tick()
    henv.clock.advance(61)
    out = henv.host.tick()
    assert not ends(out) and s.stopping_since is not None, "stopped: N's end waits for the group"
    henv.groups.add(child.pid)
    henv.clock.advance(10)
    assert not ends(henv.host.tick()) and child.killed
    henv.clock.advance(10)
    [end] = ends(henv.host.tick())
    assert end.steer == {"outcome": "unconfirmed", "why": "turn_cut"} and s.child is child, "still lingering"
    assert s.held is None and not s.busy


def test_the_queued_s_close_finalizer_n_end_then_ended(henv, monkeypatch):
    """Part O: a fully written S still queued in the CLI when the session is closed: the finalizer (drain, bounded
    stop), N's native end, and only then `ended`; S `unconfirmed`."""
    s, child, n = steered(henv, monkeypatch)
    child.emit(owned(n, text="N's own"))
    henv.host.tick()
    out = henv.host.close(SID, "closed")
    assert not [e for e in out if e.kind in ("turn_ended", "ended")], "both wait for the bounded stop"
    out = henv.host.tick()
    kinds = [(e.kind, e.message_id) for e in out]
    assert kinds.index(("turn_ended", "1")) < kinds.index(("ended", None))
    [end] = ends(out)
    assert end.ok and end.text == "N's own" and end.steer == {"outcome": "unconfirmed", "why": "turn_cut"}


def test_a_cancel_of_n_during_the_hold_reports_ns_native_result(henv, monkeypatch):
    """N's cancel during the hold: "a turn that had already completed keeps its result": N's native end, S
    `unconfirmed` (or `next_turn` failed if S's own turn was proved)."""
    s, child, n = steered(henv, monkeypatch)
    child.emit(owned(n, text="N's own"))
    henv.host.tick()
    child.emit(vinit(S))
    henv.host.tick()
    out = henv.host.cancel(SID, "1", "cancelled") + henv.host.tick()
    [end] = ends(out)
    assert end.ok and end.text == "N's own" and end.steer["outcome"] == "next_turn"
    assert end.steer["result"]["detail"] == "its own turn was cut: cancelled"


def test_an_exit_during_the_hold_publishes_at_once(henv, monkeypatch):
    s, child, n = steered(henv, monkeypatch)
    child.emit(owned(n))
    henv.host.tick()
    child.code = 0
    [end] = ends(henv.host.tick())
    assert end.ok and end.steer == {"outcome": "unconfirmed", "why": "turn_cut"}


def test_a_deviation_in_the_hold_finalizes_at_once(henv, monkeypatch):
    """A plain result in an unattributed bracket while N's end is held: nothing later can prove S quiet (G-2): the
    finalizer at once, S `unconfirmed`."""
    s, child, n = steered(henv, monkeypatch)
    child.emit(owned(n))
    henv.host.tick()
    child.emit(vinit(), plain(text="whose?"))
    out = henv.host.tick() + henv.host.tick()
    [end] = ends(out)
    assert end.steer == {"outcome": "unconfirmed", "why": "turn_cut"} and child.terminated


@pytest.mark.parametrize("stop", ["stop_turns", "stop_all"])
def test_a_stop_of_every_turn_publishes_a_held_end_at_mains_point(henv, monkeypatch, stop):
    """Shutdown simplified: a stop of every turn and shutdown are main's: the held end is published where main fails
    the turn, reading nothing more; S as what was read decides it."""
    s, child, n = steered(henv, monkeypatch)
    child.emit(owned(n, text="N's own"))
    henv.host.tick()
    out = henv.host.stop_turns("lost the hub") if stop == "stop_turns" else henv.host.stop_all()
    [end] = ends(out)
    assert end.ok and end.text == "N's own" and end.steer == {"outcome": "unconfirmed", "why": "turn_cut"}


def test_the_checkpoint_carries_ns_native_end_once(henv, monkeypatch):
    """The checkpoint (`held`): N's native end, capped as an end's, once; nothing of N else is reported meanwhile."""
    s, child, n = steered(henv, monkeypatch)
    child.emit(owned(n, text="native", subtype="success"))
    out = henv.host.tick()
    [held] = [e for e in out if e.kind == "held"]
    assert held.message_id == "1" and held.ok and held.text == "native" and held.answered
    assert henv.host.tick() == []
