"""Follow-ups after #119 (DESIGN_NOTES "After #119: follow-ups"; rulings A1 to A3).

A1: an explicit close follows that session's earlier terminal events. The watchdog's self-stop fails a session's turn
into its own lane (`watched`, no hub contact); the listing that restores contact closes that SAME session. The
close's `ended` must not be handed off ahead of the earlier `turn_ended`: the hub keeps the no-contact detail, as on
256c3e6 and e6b421f. For a Codex session, a Claude session with `claude_steer = false`, and one with the default.
A3: a reserved stop whose commit raises on the watchdog's thread: what becomes of the held end."""
import pytest

from agent_comms import hosted
from test_claude_steering import cinit, env, written  # noqa: F401 (the node-backed env)
from test_session_host import henv  # noqa: F401
from test_hosted_sessions import P3, start, tick
from test_hosted_tree import proc_of


def _a_turn_in_flight(env, kind, monkeypatch):
    runner = env.node.sessions
    if kind == "codex":
        from test_codex_steering import codex_turn_in_flight
        sid, child = codex_turn_in_flight(env, project=P3)
        return sid, child
    if kind == "claude_off":
        monkeypatch.setattr(runner.host.config.sessions, "claude_steer", False)
    sid = start(env, "a long turn")["session_id"]
    tick(env)
    child = proc_of(env, sid)[1]
    child.emit(cinit(sid, written(child)[-1]["uuid"]))
    tick(env)
    s = runner.host.sessions[sid]
    assert s.provider.steering_on is (kind == "claude_default")
    return sid, child


@pytest.mark.parametrize("kind", ["codex", "claude_off", "claude_default"])
def test_a1_a_close_of_the_self_stopped_session_follows_its_watched_turn_end(env, monkeypatch, kind):
    """Ruling A1 (the reviewer's sequence): the self-stop fails the session's turn into `watched`; the listing that
    restores contact closes that same session: its `turn_ended` is handed off ahead of the close's `ended`, so the hub
    records the turn with the self-stop's no-contact detail, as on 256c3e6."""
    runner = env.node.sessions
    sid, child = _a_turn_in_flight(env, kind, monkeypatch)
    with runner.lock:
        runner.host.stop_turns(runner._self_stop_detail(), own=True, into=runner.watched)
    assert [e for e in runner.watched if e.session_id == sid and e.kind == "turn_ended"]
    assert env.human.post(f"/api/hosted-sessions/{sid}/close").status_code == 200
    tick(env, times=3)
    m = hosted._get(env.board.conn, hosted._msg_key(sid, 1))
    assert m["state"] == "failed" and m["detail"] == runner._self_stop_detail(), m


# ---------------------------------------------------------------- A3: a reserved stop whose commit raises


def _held_cut_whose_commit_raises(henv, monkeypatch, exits_on_terminate):
    """A held N whose contact ran out; the watchdog's boundary cuts it (the reserved stop, no drain) and the commit
    raises after the reservation (an injected failure in the decision): escalation armed, the hold neither decided
    nor fenced. Then the decision works again: (session, child)."""
    from test_claude_correlation import owned
    from test_claude_steering import steered
    s, child, n = steered(henv, monkeypatch)
    child.exits_on_terminate = exits_on_terminate
    s.steer.cover_until = henv.clock() + 10_000
    child.emit(owned(n, text="N's own"))
    henv.host.tick()
    assert s.held is not None
    s.steer.cover_until = henv.clock() - 1
    real_fail = henv.host._fail

    def bad(*a, **k):
        raise RuntimeError("the decision failed")
    henv.host._fail = bad
    with pytest.raises(RuntimeError):
        henv.host._hold_cut(s, henv.clock(), henv.host._outbox, drain=False)     # (the watchdog's cut)
    henv.host._fail = real_fail
    assert s.stopping_since is not None and child.terminated == 1, "escalation armed"
    assert s.held is not None and s.held.fenced_at is None and s.held.decided is None, "neither decided nor fenced"
    return s, child


def _ends(out):
    return [e for e in out if e.kind == "turn_ended" and e.message_id == "1"]


def test_a3_after_a_raising_commit_the_verified_reap_settles_the_held_end(henv, monkeypatch):
    """Ruling A3: the process exits on its SIGTERM; at the verified reap its held end is settled once: N's native
    end, S `unconfirmed` (`turn_cut`); nothing is left unsettled."""
    s, child = _held_cut_whose_commit_raises(henv, monkeypatch, exits_on_terminate=True)
    out = henv.host.tick()
    [end] = _ends(out)
    assert end.ok and end.text == "N's own" and end.steer == {"outcome": "unconfirmed", "why": "turn_cut"}
    assert s.held is None and s.child is None and s.in_flight is None
    assert not _ends(henv.host.tick())


def test_a3_after_a_raising_commit_the_held_turns_timeout_and_the_kill_settle_it_at_the_reap(henv, monkeypatch):
    """Ruling A3: the process ignores its SIGTERM; the held turn's own timeout comes and goes (the process is already
    stopping: nothing more is decided or signalled); the kill scan's SIGKILL after the grace, then the verified reap:
    N's end is settled once, there."""
    s, child = _held_cut_whose_commit_raises(henv, monkeypatch, exits_on_terminate=False)
    out = []
    henv.clock.advance(henv.host.timeout_minutes * 60 + 1)
    out += henv.host.tick()
    assert child.killed, "the kill scan escalated"
    out += henv.host.tick()
    [end] = _ends(out)
    assert end.ok and end.text == "N's own" and end.steer["outcome"] == "unconfirmed"
    assert s.held is None and s.child is None
