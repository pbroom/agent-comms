"""Mid-turn steering, step 3: Claude steering (DESIGN_NOTES "Mid-turn steering, step 3: Claude steering, as built").

The host and the adapter, driven with the frames gate M8 measured on Claude Code 2.1.330 (scratchpad steer3/runs):
S taken in at a tool boundary (`in_turn`), S queued past N's last boundary and run as its own turn after N's end
(`next_turn`, S's own `init` echoing exactly [S]), S consumed by a turn the CLI started itself (`unconfirmed`), N's end
held, the checkpoint, the finalizer and the bounded publication. Every steer here goes through step 2's slot, write
boundary and outcome rules: nothing of those is copied."""
import json

import httpx

import pytest

from agent_comms.providers import claude
from agent_comms.providers.claude import ClaudeSession
from agent_comms.session_host import STEER_UNATTRIBUTED
from test_claude_correlation import (NOTIFY, V, emit, foreign, idle_host, nonturn, owned, plain, result, short_host,
                                     uuid_of, vinit)
from test_session_host import SID, SID2, henv, make_host, open_session  # noqa: F401

from agent_comms.session_host import SessionHost as _SessionHost

_REAL_CLAUDE_STEER = getattr(_SessionHost, "claude_steer", None)


@pytest.fixture(autouse=True)
def _claude_steering_on(monkeypatch):
    """(Review round 9, ruling F; the enabling PR) Claude steering is ON by default (`[dispatch.sessions]
    claude_steer`); every test here runs with it switched on, set by this file itself (so that a test reads the same
    on a build where it was off). `_claude_steering_off` gives a test the explicit-off configuration."""
    monkeypatch.setattr(_SessionHost, "claude_steer", lambda self: True, raising=False)
    monkeypatch.setattr(ClaudeSession, "steering_on", True, raising=False)


def _claude_steering_off(monkeypatch, config):
    """The explicit-off configuration for the rest of a test (`claude_steer = false` in the node's file): the host
    reads its config again, the config says off, and the adapter's own word is off until the host's."""
    monkeypatch.setattr(_SessionHost, "claude_steer", _REAL_CLAUDE_STEER)
    monkeypatch.setattr(config.sessions, "claude_steer", False)
    monkeypatch.setattr(ClaudeSession, "steering_on", False)


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
    out = henv.host.tick() + henv.host.tick()
    [end] = ends(out)
    # (rounds 2 and 3, rulings F and L: typed evidence needs the verified incarnation; N's own result stands, its
    # identity intact; S `unconfirmed`: what the frame says of S proves nothing)
    assert end.text == "mine" and end.steer == {"outcome": "unconfirmed", "why": "unproved"}


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


# ---------------------------------------------------------------- the node and the hub (test_hosted_sessions' env)

from agent_comms import hosted, hosted_results, hosted_steer, node_sessions  # noqa: E402
from test_hosted_sessions import make_node, msg, session_env, show, start, tick, written  # noqa: E402
from test_hosted_tree import TREE, proc_of, tree_config  # noqa: E402
from test_session_host import result as plain_result  # noqa: E402


@pytest.fixture
def env(tmp_path):
    """A hub with child sessions on; its node hosts Claude (VERIFIED and STEER_MODES as shipped)."""
    return session_env(tmp_path, dispatch=TREE, config=tree_config)


def cinit(sid, *uuids) -> dict:
    ev = {"type": "system", "subtype": "init", "session_id": sid, "permissionMode": "dontAsk", "tools": ["Read"],
          "claude_code_version": V, "capabilities": ["msg_lifecycle_v1", "interrupt_receipt_v1"]}
    return ev | ({"user_message_uuids": list(uuids)} if uuids else {})


def cresult(sid, *uuids, text="ok", **kw) -> dict:
    return plain_result(sid, text=text, **kw) | (
        {"user_message_uuid": uuids[0], "user_message_uuids": list(uuids)} if uuids else {})


def claude_steered(env, project=None):
    """A Claude root, N (message 1) in flight in its owned bracket, a human steer accepted, offered and written."""
    from conftest import PROJECT
    sid = start(env, "a long turn", project=project or PROJECT)["session_id"]
    tick(env)
    child = proc_of(env, sid)[1]
    n = written(child)[-1]["uuid"]
    child.emit(cinit(sid, n))
    tick(env)
    r = env.human.post(f"/api/hosted-sessions/{sid}/messages", json={"message": TEXT, "mode": "steer"})
    assert r.status_code == 200, r.text
    tick(env)
    tick(env)
    rec = hosted_steer.get(env.board.conn, sid, 1)
    s_line = [x for x in written(child) if x.get("uuid") == rec["uuid"]]
    assert s_line and s_line[0]["message"]["content"] == TEXT, "S written once, with its own uuid"
    return sid, child, n, rec["uuid"]


def test_e2e_in_process_next_turn_checkpoint_then_ns_native_end_with_ss_reply(env):
    """Through the node and the hub: N's result lists only N, so its end is held and checkpointed (`held` on N's
    record, out of every view); S's own turn follows, and N settles once: its native result, `next_turn`, S's
    reply in the steer record (the session's reply stays N's)."""
    sid, child, n, s_uuid = claude_steered(env)
    child.emit(cresult(sid, n, text="N's reply"))
    tick(env, times=2)          # the host holds N's end; the next flush sends the checkpoint (a report of its own)
    m = hosted._get(env.board.conn, hosted._msg_key(sid, 1))
    assert m["state"] == "delivered" and m["held"]["text"] == "N's reply" and m["held"]["ok"] is True
    view = show(env, sid)
    assert view["in_flight"] == 1 and "held" not in json.dumps(view), "the checkpoint is in no view"
    child.emit(cinit(sid, s_uuid), cresult(sid, s_uuid, text="PINEAPPLE"))
    tick(env)
    m = msg(env, sid, 1)
    assert m["state"] == "done" and show(env, sid)["reply"] == "N's reply"
    rec = hosted_steer.get(env.board.conn, sid, 1)
    assert rec["outcome"] == "next_turn" and rec["result"]["reply"] == "PINEAPPLE" and rec["result"]["ok"] is True
    assert m["steer"]["outcome"] == "next_turn" and m["steer"]["result"]["reply"] == "PINEAPPLE"
    assert "held" not in hosted._get(env.board.conn, hosted._msg_key(sid, 1)), "cleared with N's settlement"


def test_e2e_in_process_in_turn_needs_no_hold(env):
    sid, child, n, s_uuid = claude_steered(env)
    child.emit(cresult(sid, n, s_uuid, text="DONE PINEAPPLE"))
    tick(env)
    assert msg(env, sid, 1)["state"] == "done"
    assert hosted_steer.get(env.board.conn, sid, 1)["outcome"] == "in_turn"
    assert "held" not in hosted._get(env.board.conn, hosted._msg_key(sid, 1))


def test_o_the_checkpoint_is_write_once_and_only_for_its_delivered_n(env):
    """Part O: the checkpoint is write-once, for the exact assigned node, a delivered N in flight and a listed,
    unresolved steer into it; refused once N is settled."""
    sid, child, n, s_uuid = claude_steered(env)
    held = {"message": 1, "ok": True, "detail": "", "denials": [], "meta": {}, "text": "first", "answered": True}
    assert env.laptop.post(f"/api/nodes/sessions/{sid}", json={"held": held}).status_code == 200
    again = env.laptop.post(f"/api/nodes/sessions/{sid}", json={"held": held | {"text": "second"}})
    assert again.status_code == 409 and "already has one" in again.text
    assert hosted._get(env.board.conn, hosted._msg_key(sid, 1))["held"]["text"] == "first"
    other = env.human.post("/api/nodes/sessions/" + sid, json={"held": held})
    assert other.status_code in (401, 403, 404), "only the session's assigned node"
    wrong = env.laptop.post(f"/api/nodes/sessions/{sid}", json={"held": held | {"message": 2}})
    assert wrong.status_code == 409


def test_o_a_checkpoint_for_a_turn_with_no_listed_steer_is_refused(env):
    sid = start(env, "a turn")["session_id"]
    tick(env)
    held = {"message": 1, "ok": True, "detail": "", "denials": [], "meta": {}, "text": "x", "answered": True}
    r = env.laptop.post(f"/api/nodes/sessions/{sid}", json={"held": held})
    assert r.status_code == 409 and "took no steer" in r.text


@pytest.mark.parametrize("path", ["lost_node", "close", "replacement_node"])
def test_g_a_committed_checkpoint_decides_ns_native_fields_on_every_settling_path(env, path):
    """G's one rule: once a checkpoint is committed, every settlement of N uses N's native result, S by the rule
    (`unconfirmed`, listed, no host's word on N's own end)."""
    sid, child, n, s_uuid = claude_steered(env)
    child.emit(cresult(sid, n, text="native reply"))
    tick(env, times=2)
    assert "held" in hosted._get(env.board.conn, hosted._msg_key(sid, 1))
    if path == "lost_node":
        env.clock.advance(hosted.nodes.NODE_LOST_SECONDS + 1)
        show(env, sid)
    elif path == "close":
        with hosted.db.write_tx(env.board.conn) as c:
            r = hosted._session(c, sid)
            hosted._end(c, r, "closed", "hub", env.clock())
            hosted._put(c, hosted.SESSION_PREFIX + sid, r, "hub", env.clock())
    else:
        node2 = make_node(env, config=env.node.config, log_dir=env.tmp / "laptop-logs")
        node2.sessions.recover()
        node2.hello()
        tick(env, node2)
        tick(env, node2)
    m = hosted._get(env.board.conn, hosted._msg_key(sid, 1))
    assert m["state"] == "done" and m["ok"] is True and "held" not in m, path
    assert show(env, sid)["reply"] == "native reply"
    rec = hosted_steer.get(env.board.conn, sid, 1)
    assert (rec["outcome"], rec["why"]) == ("unconfirmed", "no_word")


def test_g_without_a_checkpoint_ns_end_is_as_on_main(env):
    """No checkpoint committed (the loss exception, A): a lost node fails N as main does, S by the rule."""
    sid, child, n, s_uuid = claude_steered(env)
    env.clock.advance(hosted.nodes.NODE_LOST_SECONDS + 1)
    view = show(env, sid)
    assert view["messages"][0]["state"] == "failed" and view["messages"][0]["steer"]["outcome"] == "unconfirmed"


def test_h_the_extension_carries_next_turn_with_ss_result_and_a_malformed_one_is_ignored():
    good = {"outcome": "next_turn", "why": "own_turn", "result": {"ok": True, "detail": "", "denials": [], "meta": {},
                                                                    "text": "S", "answered": True}}
    assert hosted_steer.extension(good)["result"]["text"] == "S"
    assert hosted_steer.extension({"outcome": "next_turn", "why": "own_turn"}) is None
    assert hosted_steer.extension(good | {"result": {"ok": "yes"}}) is None
    assert hosted_steer.extension({"outcome": "in_turn", "why": "answered", "result": good["result"]}) is None


def test_h_the_parents_result_record_and_the_renderer_for_next_turn():
    """H, "One result for N": N's raw reply apart, S's reply as a field, the combined text for every renderer;
    the current renderer shows N's reply in its markers, the hub's line outside them, then S's reply in its own."""
    x = {"n": 3, "child": "c", "name": "kid", "agent": "claude", "message": 2, "outcome": "completed", "ok": True,
         "detail": "", "denials": [], "reply": "N's reply",
         "text": "N's reply\n\n" + hosted_results.STEER_JOIN + "\nS's reply",
         "steer": {"outcome": "next_turn", "why": "own_turn", "ok": True, "detail": "", "reply": "S's reply"}}
    text = hosted_results.render([x], token="tok")
    lines = text.splitlines()
    i, j = lines.index("N's reply"), lines.index("S's reply")
    assert lines[i + 2] == hosted_results.STEER_LINES["next_turn"] and i < j
    assert lines[i - 1] == "<<<child-output tok>>>" and lines[j - 1] == "<<<child-output tok>>>"
    assert hosted_results.STEER_JOIN not in text, "the current renderer does not use the combined text"


# ---------------------------------------------------------------- with no steer sent: parity (part K)


def _no_steer_run(henv, monkeypatch, caplog, seed, modes):
    """One seeded sequence of a verified Claude session's frames with no steer ever sent: owned turns, turns the CLI
    started itself, notifications, competing inits, results with no echo, exits and timeouts. Returns everything
    observable: host events, stdin bytes, every log record of the package, every gate callback, and the modes."""
    import logging
    import random
    from test_claude_correlation import failed
    monkeypatch.setattr(claude, "VERIFIED", frozenset({V}))
    monkeypatch.setattr(claude, "STEER_MODES", modes)
    from test_dispatch import FakeProcs, FakeSpawner
    from test_session_host import FakeSessionChild
    caplog.clear()
    caplog.set_level(logging.DEBUG, logger="agent_comms")
    henv.spawner = FakeSpawner(child=FakeSessionChild)
    henv.procs = FakeProcs(henv.spawner)
    henv.host = make_host(henv, timeout_minutes=1)
    calls = []
    henv.host.may_start = lambda: calls.append("may_start") or True
    henv.host.turn_gate = lambda sess: calls.append("turn_gate") or True
    henv.host.steer_authority = lambda sess: calls.append("authority") or True
    rnd = random.Random(seed)
    s = open_session(henv)
    seen, modes_seen, n = [], [], 0
    for _ in range(25):
        child = henv.spawner.children[-1] if henv.spawner.children else None
        act = rnd.choice(["send", "send", "own", "cli", "note", "compete", "plain", "error", "wait", "exit"])
        if act == "send" and not s.inbox:
            n += 1
            henv.host.send(SID, f"turn {n}", message_id=str(n))
        elif child is not None and act == "own":
            u = uuid_of(child) if child.stdin else None
            child.emit(*((vinit(u), owned(u, text="r")) if u else (vinit(),)))
        elif child is not None and act == "cli":
            child.emit(vinit(), foreign())
        elif child is not None and act == "note":
            child.emit(nonturn())
        elif child is not None and act == "compete":
            child.emit(vinit())
        elif child is not None and act == "plain":
            child.emit(plain(text="?"))
        elif child is not None and act == "error":
            child.emit(failed())
        elif child is not None and act == "exit":
            child.code = 0
        henv.clock.advance(rnd.choice([1, 5, 30, 61]))
        seen.append([(e.kind, e.message_id, e.ok, e.detail, e.text, e.steer) for e in henv.host.tick()])
        modes_seen.append(henv.host.modes(SID) if SID in henv.host.sessions else ())
    logs = [(r.name, r.levelname, r.getMessage()) for r in caplog.records]
    stdin = [c.stdin for c in henv.spawner.children]
    return {"events": seen, "logs": logs, "stdin": [__import__("re").sub(rb'"uuid": "[^"]+"', b'"uuid": "u"', x)
                                                    for x in stdin], "calls": calls}, modes_seen


@pytest.mark.parametrize("seed", range(40))
def test_k_with_no_steer_sent_a_claude_session_is_the_build_that_never_steers(henv, monkeypatch, caplog, seed):
    """Part K, the parity rule for step 3: with no steer sent, every Claude frame's handling, host event, stdin byte,
    log line and gate callback is that of the same tree with STEER_MODES empty (no Claude version steers: step 2's
    Claude, on main at 2b84f42); the one named change is `modes` (a verified 2.1.330 process says `steer` and
    `held`, review round 2)."""
    import copy
    ours, our_modes = _no_steer_run(henv, monkeypatch, caplog, seed, copy.deepcopy(claude.STEER_MODES))
    base, base_modes = _no_steer_run(henv, monkeypatch, caplog, seed, {})
    assert ours == base
    assert all("steer" not in m and "held" not in m for m in base_modes)
    assert [tuple(x for x in m if x not in ("steer", "held")) for m in our_modes] == [tuple(m) for m in base_modes]


# ---------------------------------------------------------------- rules the mutation check asked a test for


def test_the_three_outcome_mode_an_init_echoing_s_proves_nothing(henv, monkeypatch):
    """Part L's result (i)'s other branch, for a version recorded THREE: an `init` echoing exactly [S] opens no
    S-owned turn; the hold ends only when that bracket's TERMINAL result lists S: `unconfirmed` (consumed), never
    `next_turn`."""
    monkeypatch.setattr(claude, "STEER_MODES", {V: claude.THREE})
    s, child, n = steered(henv, monkeypatch)
    child.emit(owned(n))
    henv.host.tick()
    child.emit(vinit(S))
    assert henv.host.tick() == [] and not s.held.s_turn
    child.emit(owned(S, text="S's reply"))
    [end] = ends(henv.host.tick())
    assert end.steer == {"outcome": "unconfirmed", "why": "consumed"}


def test_an_init_that_mentions_s_with_another_uuid_opens_no_s_turn(henv, monkeypatch):
    """Only an `init` listing exactly [S] opens S's own turn. [S, X] names S in a shape M8 never measured: never
    evidence, the loss of verification (review round 2, ruling F): the finalizer at once, S `unconfirmed`, and an [S]
    result after it releases nothing."""
    s, child, n = steered(henv, monkeypatch)
    child.emit(owned(n))
    henv.host.tick()
    child.emit(vinit(S, "0be7c0de-0000-4000-8000-000000000001"), owned(S, text="?"))
    out = henv.host.tick() + henv.host.tick()
    [end] = ends(out)
    assert end.steer == {"outcome": "unconfirmed", "why": "turn_cut"} and not s.provider.verified


def test_o_a_checkpoint_whose_steer_was_never_listed_is_refused(env):
    """Part O: the checkpoint needs a LISTED, unresolved steer into N (a steer the node never saw cannot be held)."""
    sid = start(env, "a long turn")["session_id"]
    tick(env)
    child = proc_of(env, sid)[1]
    child.emit(cinit(sid, written(child)[-1]["uuid"]))
    tick(env)
    with hosted.db.write_tx(env.board.conn) as c:
        r = hosted._session(c, sid)
        m = hosted._get(c, hosted._msg_key(sid, 1))
        hosted_steer.accept(c, r, m, TEXT, "user", {"human": True}, "human", None, env.clock())
    held = {"message": 1, "ok": True, "detail": "", "denials": [], "meta": {}, "text": "x", "answered": True}
    r = env.laptop.post(f"/api/nodes/sessions/{sid}", json={"held": held})
    assert r.status_code == 409 and "never listed" in r.text


def test_h_a_parents_next_turn_result_record_keeps_ns_reply_apart_and_combines_the_text(env):
    """H through `hosted_results.turn`: the record keeps N's raw reply (`reply`), S's result as the steer's fields,
    and the combined text (N's reply, a blank line, the plain line, S's reply) in `text`; never for a data turn."""
    with hosted.db.write_tx(env.board.conn) as c:
        child = {"session_id": "c" * 8, "steered": True}
        s = {"session_id": child["session_id"], "into": 2, "kind": "task", "outcome": "next_turn", "why": "own_turn",
             "result": {"ok": True, "detail": "", "denials": [], "meta": {}, "reply": "S's reply"}}
        hosted.nodes._put(c, hosted_steer._key(child["session_id"], 2), s, "hub", env.clock())
        captured = {}
        orig = hosted_results._add
        hosted_results._add = lambda c_, ch, fields, now, kept=False: captured.update(fields)
        try:
            hosted_results.turn(c, child, {"n": 2, "kind": "task"}, ok=True, delivered=True, detail="",
                                text="N's reply", denials=[], now=env.clock())
        finally:
            hosted_results._add = orig
    assert captured["reply"] == "N's reply" and captured["steer"]["reply"] == "S's reply"
    assert captured["text"] == "N's reply\n\n" + hosted_results.STEER_JOIN + "\nS's reply"


def test_g_the_checkpoint_goes_only_to_a_hub_that_names_steer(env):
    """Part G: the node sends the checkpoint only when the hub's last listing named `steer` and what it takes is
    known; otherwise it is not sent (logged), and N's end is never held for it."""
    from agent_comms.session_host import HostEvent
    runner = env.node.sessions
    runner.hub_features, runner.features_known = {"answered", "restart"}, True
    runner._emit([HostEvent("5e551011-0000-4000-8000-000000000000", "held", "1", ok=True, text="x")])
    assert runner.checkpoints == {}
    runner.hub_features = {"answered", "restart", "steer", "held"}
    runner._emit([HostEvent("5e551011-0000-4000-8000-000000000000", "held", "1", ok=True, text="x")])
    assert runner.checkpoints["5e551011-0000-4000-8000-000000000000"].payload["text"] == "x"
    assert runner.pending == [], "the checkpoint is no event of the session's"


def test_o_a_checkpoint_for_a_queued_message_or_a_settled_n_is_refused_with_its_reason(env):
    """Part O (a test the mutation check asked for): the checkpoint is only for N delivered and in flight: a message
    still queued behind it is refused as not the turn in flight, and N once settled is refused as settled."""
    from test_hosted_sessions import send
    sid, child, n, s_uuid = claude_steered(env)
    send(env, sid, "queued behind N")
    held = {"message": 2, "ok": True, "detail": "", "denials": [], "meta": {}, "text": "x", "answered": True}
    r = env.laptop.post(f"/api/nodes/sessions/{sid}", json={"held": held})
    assert r.status_code == 409 and "not the turn in flight" in r.text
    child.emit(cresult(sid, n, s_uuid, text="done"))
    tick(env)
    r = env.laptop.post(f"/api/nodes/sessions/{sid}", json={"held": held | {"message": 1}})
    assert r.status_code == 409 and "it is settled" in r.text


# ---------------------------------------------------------------- review round 1: the blockers' replays


def _next_turn_done(henv, monkeypatch):
    """N held, S's own turn opened and ended: `next_turn` published. (session, child)."""
    s, child, n = steered(henv, monkeypatch)
    child.emit(owned(n, text="N"))
    henv.host.tick()
    child.emit(vinit(S), owned(S, text="S"))
    [end] = ends(henv.host.tick())
    assert end.steer["outcome"] == "next_turn" and s.held is None
    return s, child


def _ordinary_turn(henv, child, mid, text="M's reply"):
    henv.host.send(SID, f"message {mid}", message_id=mid)
    out = henv.host.tick()
    assert (("delivered", mid) in [(e.kind, e.message_id) for e in out]), out
    m = uuid_of(child)
    child.emit(vinit(m), owned(m, text=text))
    return [e for e in henv.host.tick() if e.message_id == mid]


@pytest.mark.parametrize("how", ["next_turn", "consumed"])
def test_r1_b1_after_publication_the_next_ordinary_turn_is_its_own(henv, monkeypatch, how):
    """Blocker 1 (Sol's replay): result[N], init[S], result[S], then init[M], result[M]: M's frames are M's (and the
    same after `consumed`): the attempt is retired before any further frame is read."""
    if how == "next_turn":
        s, child = _next_turn_done(henv, monkeypatch)
    else:
        s, child, n = steered(henv, monkeypatch)
        child.emit(owned(n))
        henv.host.tick()
        child.emit(vinit(), owned(S, text="took S") | {"origin": NOTIFY})
        [end] = ends(henv.host.tick())
        assert end.steer["why"] == "consumed"
    got = _ordinary_turn(henv, child, "2")
    assert [(e.kind, e.ok, e.text) for e in got if e.kind == "turn_ended"] == [("turn_ended", True, "M's reply")]
    assert ("turn_started", "2") in [(e.kind, e.message_id) for e in got]


def test_r1_b1_a_cli_turn_in_the_same_read_as_ss_result_holds_the_barrier(henv, monkeypatch):
    """Blocker 1: S's result and an unmarked `init` in the same read: the CLI's own turn opens after publication,
    so the next message waits behind it (step 1's barrier), as without a steer."""
    s, child, n = steered(henv, monkeypatch)
    child.emit(owned(n))
    henv.host.tick()
    child.emit(vinit(S), owned(S, text="S"), vinit())
    assert ends(henv.host.tick())[0].steer["outcome"] == "next_turn"
    assert s.provider.holds_turns(), "the CLI's own turn is open"
    henv.host.send(SID, "next", message_id="2")
    henv.host.tick()
    assert s.writing is None and s.inbox and s.inbox[0].id == "2", "the next message waits behind the CLI's turn"


def test_r1_b1_a_second_steer_into_a_later_turn_works_as_the_first(henv, monkeypatch):
    s, child = _next_turn_done(henv, monkeypatch)
    henv.host.send(SID, "second", message_id="2")
    henv.host.tick()
    m = uuid_of(child)
    child.emit(vinit(m))
    henv.host.tick()
    s2 = "5e7e7e7e-0000-4000-8000-000000000002"
    henv.host.steer_offer(SID, "2", s2, TEXT, authorized=True)
    henv.host.tick()
    child.emit(owned(m, s2, text="M with S2"))
    [end] = [e for e in henv.host.tick() if e.kind == "turn_ended"]
    assert end.message_id == "2" and end.steer == {"outcome": "in_turn", "why": "answered"}


def test_r1_b1_an_approval_request_in_ss_own_turn_is_not_ns(henv, monkeypatch):
    s, child, n = steered(henv, monkeypatch)
    child.emit(owned(n))
    henv.host.tick()
    child.emit(vinit(S), {"type": "control_request", "request_id": "x",
                          "request": {"subtype": "can_use_tool", "tool_name": "Bash"}})
    out = henv.host.tick()
    assert not [e for e in out if e.kind == "approval_requested"] and not s.waiting
    child.emit(owned(S, text="S"))
    henv.host.tick()
    assert not s.waiting


@pytest.mark.parametrize("init", [
    {"user_message_uuids": [S, S]}, {"user_message_uuids": S}, {"user_message_uuids": [S], "origin": NOTIFY},
    {"user_message_uuids": [S], "claude_code_version": "2.1.329"},
    {"user_message_uuids": [S], "capabilities": ["interrupt_receipt_v1"]},
    {"user_message_uuids": [S], "claude_code_version": "2.1.331"}],
    ids=["twice", "scalar", "notification", "other_version", "no_lifecycle", "verified_not_in_steer_modes"])
def test_r1_b2_only_the_exact_init_opens_ss_own_turn(henv, monkeypatch, init):
    """Blocker 2: S's own turn opens only on an `init` listing exactly [S], no origin, of a version in STEER_MODES
    with the lifecycle capability; none of these does, and none makes a later [S] result `next_turn`. (2.1.331 is
    VERIFIED here but not in STEER_MODES: verification alone does not open S's turn.)"""
    s, child, n = steered(henv, monkeypatch)
    monkeypatch.setattr(claude, "VERIFIED", frozenset({V, "2.1.331"}))
    child.emit(owned(n))
    henv.host.tick()
    frame = {k: v for k, v in vinit().items() if k != "user_message_uuids"} | init
    child.emit(frame)
    henv.host.tick()
    assert s.held is None or not s.held.s_turn
    child.emit(owned(S, text="S"))
    out = henv.host.tick()
    assert all(e.steer is None or e.steer["outcome"] != "next_turn" for e in ends(out))


def test_r1_b2_a_late_n_s_result_in_the_hold_is_no_evidence(henv, monkeypatch):
    """Blocker 2 (departure 3 overruled; round 2, ruling F): a late [N, S] during the hold names N after N's own
    result: never evidence, the loss of verification; the finalizer at once, S `unconfirmed`, and the S frames
    after it give nothing (never the late frame's text, never `next_turn`)."""
    s, child, n = steered(henv, monkeypatch)
    child.emit(owned(n, text="N"))
    henv.host.tick()
    child.emit(owned(n, S, text="late"), vinit(S), owned(S, text="S's own"))
    out = henv.host.tick() + henv.host.tick()
    [end] = ends(out)
    assert end.text == "N" and end.steer == {"outcome": "unconfirmed", "why": "turn_cut"}


def test_r1_b2_an_s_x_result_before_ns_end_is_not_ns_reply(henv, monkeypatch):
    s, child, n = steered(henv, monkeypatch)
    child.emit(owned(S, "0be7c0de-0000-4000-8000-000000000001", text="not N's"))
    assert not ends(henv.host.tick()) and s.in_flight.id == "1"
    child.emit(owned(n, S, text="N's"))
    [end] = ends(henv.host.tick())
    assert end.text == "N's"


def test_r1_b2_an_unverifying_init_in_the_hold_finalizes_it(henv, monkeypatch):
    """Blocker 2: an unmarked `init` of a version not VERIFIED during the hold loses verification: typed `lost`, the
    finalizer at once, S `unconfirmed`."""
    s, child, n = steered(henv, monkeypatch)
    child.emit(owned(n))
    henv.host.tick()
    child.emit(vinit(version="2.1.329"))
    out = henv.host.tick() + henv.host.tick()
    [end] = ends(out)
    assert end.steer == {"outcome": "unconfirmed", "why": "turn_cut"} and child.terminated


def test_r1_b3_evidence_after_the_stop_latch_never_counts(henv, monkeypatch):
    """Blocker 3 (Sol's replay): hold N, set the latch, then S's buffered init and result: not `next_turn`; the stop
    of every turn publishes N's held end with S by what was adjudicated before (`unconfirmed`)."""
    s, child, n = steered(henv, monkeypatch)
    child.emit(owned(n, text="N"))
    henv.host.tick()
    henv.host.steer_stopping()
    child.emit(vinit(S), owned(S, text="S"))
    out = henv.host.tick() + henv.host.stop_all()
    [end] = ends(out)
    assert end.text == "N" and end.steer == {"outcome": "unconfirmed", "why": "turn_cut"}


def test_r1_b3_evidence_after_the_latch_in_ss_own_turn_is_next_turn_cut(henv, monkeypatch):
    s, child, n = steered(henv, monkeypatch)
    child.emit(owned(n, text="N"))
    henv.host.tick()
    child.emit(vinit(S))
    henv.host.tick()
    henv.host.steer_stopping()
    child.emit(owned(S, text="S"))
    out = henv.host.tick() + henv.host.stop_all()
    [end] = ends(out)
    assert end.steer["outcome"] == "next_turn" and end.steer["result"]["ok"] is False


@pytest.mark.parametrize("when", ["held", "s_turn"])
def test_r1_b3_the_steers_contact_running_out_in_the_hold_cuts_it(henv, monkeypatch, when):
    """Blocker 3: the attempt's own contact clock runs out during the hold (listing GETs do not renew it): the cut,
    the finalizer, N's held end unaltered; S `unconfirmed`, or `next_turn` failed when its own turn was proved."""
    s, child, n = steered(henv, monkeypatch)
    s.steer.cover_until = henv.clock() + 20
    child.emit(owned(n, text="N"))
    henv.host.tick()
    if when == "s_turn":
        child.emit(vinit(S))
        henv.host.tick()
    henv.clock.advance(21)
    out = henv.host.tick() + henv.host.tick()
    [end] = ends(out)
    assert end.text == "N" and child.terminated
    assert end.steer["outcome"] == ("unconfirmed" if when == "held" else "next_turn")


def test_r1_b4_a_read_error_while_held_publishes_n_and_ended_at_once(henv, monkeypatch):
    """Blocker 4 (rule 5): a read that raises while N's end is held: N's held end and `ended`, at once."""
    s, child, n = steered(henv, monkeypatch)
    child.emit(owned(n, text="N"))
    henv.host.tick()

    def broken():
        raise OSError("read failed")
    child.read_lines = broken
    out = henv.host.tick()
    kinds = [(e.kind, e.message_id) for e in out]
    assert ("turn_ended", "1") in kinds and ("ended", None) in kinds
    assert kinds.index(("turn_ended", "1")) < kinds.index(("ended", None))
    assert ends(out)[0].text == "N"


def test_r1_b4_a_fenced_end_is_published_at_its_deadline_whatever_the_reads_do(henv, monkeypatch):
    """Blocker 4 (ruling B): a close fenced N's held end; the reads fail on every later tick; the deadline, checked
    first in the tick, still publishes N's end and `ended`, once."""
    s, child, n = steered(henv, monkeypatch, grace=10)
    child.exits_on_terminate = False
    child.emit(owned(n, text="N"))
    henv.host.tick()
    henv.groups.add(child.pid)
    henv.host.close(SID, "closed")

    def broken():
        raise OSError("read failed")
    child.read_lines = broken
    got = []
    for _ in range(5):
        henv.clock.advance(5)
        got += henv.host.tick()
    assert len(ends(got)) == 1 and [e.kind for e in got].count("ended") == 1


def test_r1_s2_byte_evidence_is_per_attempt(henv, monkeypatch):
    """Should 2 (Sol's replay): S1 written into N1; then S2 staged into N2 with zero bytes and retired; a plain
    result for N2 is N2's, as on main (only an attempt a byte of which went forbids the order fallback)."""
    s, child = _next_turn_done(henv, monkeypatch)
    henv.host.send(SID, "second", message_id="2")
    henv.host.tick()
    m = uuid_of(child)
    child.emit(vinit(m))
    henv.host.tick()
    child.accept = 0
    s2 = "5e7e7e7e-0000-4000-8000-000000000002"
    henv.host.steer_offer(SID, "2", s2, TEXT, authorized=True)
    henv.host.tick()
    henv.host.steer_revoke(SID, s2, "omitted")
    henv.host.tick()
    child.accept = None
    child.emit(plain(text="N2's"))
    [end] = [e for e in henv.host.tick() if e.kind == "turn_ended"]
    assert end.message_id == "2" and end.text == "N2's" and s.provider.verified


# ---------------------------------------------------------------- review round 1: the checkpoint's transport (C)


def test_r1_b5_a_refused_checkpoint_never_strips_another_sessions_steer_outcome(env, monkeypatch):
    """Blocker 5 (Sol's replay): A's checkpoint is refused (409); B's later end still carries its valid steer
    extension: the checkpoint's own call forgets no feature and renews nothing."""
    from agent_comms.core import Conflict
    from test_hosted_sessions import P2
    a, a_child, a_n, a_s = claude_steered(env)
    b, b_child, b_n, b_s = claude_steered(env, project=P2)
    monkeypatch.setattr(hosted_steer, "checkpoint", lambda *x, **k: (_ for _ in ()).throw(Conflict("refused")))
    a_child.emit(cresult(a, a_n, text="A"))
    tick(env, times=2)
    runner = env.node.sessions
    assert {"steer", "held"} <= runner.hub_features and runner.features_known, "nothing forgotten"
    b_child.emit(cresult(b, b_n, b_s, text="B with S"))
    tick(env)
    assert hosted_steer.get(env.board.conn, b, 1)["outcome"] == "in_turn", "B's extension was taken"


def test_r1_c_the_checkpoint_has_its_own_short_call_outside_the_report_lock(env):
    """Ruling C: the checkpoint is posted with CHECKPOINT_SECONDS at most, after the report lock is let go; a stall
    or failure of it is logged, and nothing else of the flush depends on it."""
    runner = env.node.sessions
    seen = []

    class Client:
        def request(self, method, path, **kw):
            free = runner.report_lock.acquire(blocking=False)
            if free:
                runner.report_lock.release()
            seen.append((path, kw.get("timeout"), not free))
            raise httpx.ReadTimeout("stalled")
    real = runner.client
    runner.client = Client()
    try:
        runner._post_checkpoints({"5e551011-0000-4000-8000-000000000000": node_sessions.Checkpoint(
            "5e551011-0000-4000-8000-000000000000", None, 1, {"message": 1})})
    finally:
        runner.client = real
    assert seen == [("/api/nodes/sessions/5e551011-0000-4000-8000-000000000000", node_sessions.CHECKPOINT_SECONDS,
                     False)]


def test_r1_c_a_step_2_hub_that_names_steer_gets_no_checkpoint(env):
    from agent_comms.session_host import HostEvent
    runner = env.node.sessions
    runner.hub_features, runner.features_known = {"answered", "restart", "steer"}, True
    runner._emit([HostEvent("5e551011-0000-4000-8000-000000000000", "held", "1", ok=True, text="x", answered=True)])
    assert runner.checkpoints == {}


def test_r1_c_the_hub_refuses_impossible_checkpoints(env):
    """Should 1: only a steer written under the held capability is held (round 2, ruling J: recorded with the
    attempt, not a runtime name); `answered` true is required; an ended session answers an explicit refusal."""
    sid, child, n, s_uuid = claude_steered(env)
    held = {"message": 1, "ok": True, "detail": "", "denials": [], "meta": {}, "text": "x"}
    r = env.laptop.post(f"/api/nodes/sessions/{sid}", json={"held": held})
    assert r.status_code == 409 and "malformed" in r.text, "answered missing"
    with hosted.db.write_tx(env.board.conn) as c:       # (round 2, ruling J: by the attempt's capability)
        st = hosted_steer.get(c, sid, 1)
        st["holds"] = False
        hosted._put(c, hosted_steer._key(sid, 1), st, "hub", env.clock())
    r = env.laptop.post(f"/api/nodes/sessions/{sid}", json={"held": held | {"answered": True}})
    assert r.status_code == 409 and "held capability" in r.text
    with hosted.db.write_tx(env.board.conn) as c:
        st["holds"] = True
        hosted._put(c, hosted_steer._key(sid, 1), st, "hub", env.clock())
        rec = hosted._session(c, sid)
        hosted._end(c, rec, "closed", "hub", env.clock())
        hosted._put(c, hosted.SESSION_PREFIX + sid, rec, "hub", env.clock())
    r = env.laptop.post(f"/api/nodes/sessions/{sid}", json={"held": held | {"answered": True}})
    assert r.status_code == 409 and "has ended" in r.text


def test_r1_c_next_turn_is_taken_only_for_a_claude_steer():
    r = {"session_id": "x", "steered": True, "runtime": "codex-cli"}
    ext = {"outcome": "next_turn", "why": "own_turn", "result": {"ok": True, "detail": "", "denials": [],
                                                                 "meta": {}, "text": "S", "answered": True}}

    class C:
        def execute(self, *a):
            raise AssertionError("not read")
    seen = {}
    orig = hosted_steer.get, hosted_steer._settled
    hosted_steer.get = lambda c, sid, n: {"session_id": sid, "into": n, "outcome": None, "listed_at": 1.0}
    hosted_steer._settled = lambda c, s, e, now: seen.update(ext=e) or s
    try:
        hosted_steer.settle(C(), r, 1, ext, 0.0)
    finally:
        hosted_steer.get, hosted_steer._settled = orig
    assert seen["ext"] is None, "a Codex session's next_turn is an impossible shape: rule 2"


def test_r1_c_nonfinite_metadata_is_left_out_on_both_sides():
    fields = hosted_steer.turn_fields({"ok": True, "meta": {"cost": float("nan"), "n": 2, "x": float("inf")},
                                       "answered": True})
    assert fields["meta"] == {"n": 2}
    assert node_sessions._finite({"a": float("-inf"), "b": 1.5}) == {"b": 1.5}


def test_r1_c_the_reupgrade_sweep_clears_a_stale_checkpoint(env):
    """Should 1: an older hub settled N during a downgrade with its checkpoint still on it: the sweep settles S by
    the rule and clears `held`."""
    sid, child, n, s_uuid = claude_steered(env)
    child.emit(cresult(sid, n, text="N"))
    tick(env, times=2)
    with hosted.db.write_tx(env.board.conn) as c:
        m = hosted._get(c, hosted._msg_key(sid, 1))
        m["state"] = "done"         # what an older hub's settlement leaves: N settled, the steer and `held` not
        hosted._put(c, hosted._msg_key(sid, 1), m, "hub", env.clock())
    show(env, sid)
    m = hosted._get(env.board.conn, hosted._msg_key(sid, 1))
    assert "held" not in m and hosted_steer.get(env.board.conn, sid, 1)["outcome"] == "unconfirmed"


def test_r1_s3_a_claimed_next_turn_keeps_both_blocks_and_ss_failure():
    """Should 3 (Sol's replay): a parent's `wait=true` claim of a `next_turn` result whose S failed: N's reply and
    S's reply each in their own untrusted block, the hub's line between, S's failure reason kept."""
    from agent_comms import hosted_spawn
    x = {"n": 3, "child": "c", "name": "kid", "agent": "claude", "message": 2, "outcome": "completed", "ok": True,
         "detail": "", "denials": [], "reply": "N's reply", "state": "pending",
         "text": "N's reply\n\n" + hosted_results.STEER_JOIN + "\nS's partial",
         "steer": {"outcome": "next_turn", "why": "own_turn", "ok": False, "detail": "its own turn was cut: closed",
                   "reply": "S's partial"}}
    got = hosted_spawn.taken(hosted_results.view_one(x, text=True))
    lines = got["text"].splitlines()
    assert "N's reply" in lines and "S's partial" in lines and hosted_results.STEER_JOIN not in got["text"]
    assert any("its own turn was cut: closed" in line for line in lines)
    assert got["result"]["steer"] == {"outcome": "next_turn", "why": "own_turn", "ok": False,
                                      "detail": "its own turn was cut: closed"}


def test_r1_s3_a_retry_whose_n_is_pruned_keeps_a_known_next_turn(monkeypatch, capsys):
    """Should 3: `send --mode steer --wait` retried after N was pruned: a `next_turn` the send's answer already
    said is said again (S's outcome known), N's own result not available: exit 7."""
    import types
    from agent_comms import cli
    monkeypatch.setattr(cli, "_kept", lambda *a, **k: None)
    a = types.SimpleNamespace(wait=True, json=False)
    r = {"session": {"session_id": "5e551011-0000-4000-8000-000000000000", "name": "s", "agent": "claude",
                     "state": "idle"},
         "steer": {"session": "5e551011-0000-4000-8000-000000000000", "into": 1, "outcome": "next_turn",
                   "state": "settled"}, "created": False, "why": "retry"}
    monkeypatch.setattr(cli, "_fmt_hosted", lambda s: "s")
    with pytest.raises(SystemExit) as e:
        cli._steer_sent(a, lambda *x: None, None, "t", r, "text")
    assert e.value.code == cli.STEER_UNSETTLED_EXIT
    assert cli.STEER_WORDS["next_turn"] in capsys.readouterr().err


# ---------------------------------------------------------------- the model (review round 1, ruling D)


class _M2:
    """This test's own reading of ruling A's predicates and part D's tables, for one attempt (never the adapter's
    or the host's state): the phase, the brackets, verification and time, from the frames this test chose."""

    def __init__(self, now, bytes_):
        self.phase = "n"            # n, held, s_turn, done
        self.bracket = "own"        # own (N's), cli, s, None
        self.comp, self.verified, self.lost = False, True, False
        self.bytes, self.n_start, self.since, self.cover = bytes_, now, None, None
        self.end = None             # (outcome, why, N's text or None, S's reply or None, S ok or None)
        self.native = None
        self.host_cut = False       # the turn ended by a host-made end: its process is stopped

    def finish(self, outcome, why, s_reply=None, s_ok=None):
        if self.end is None:
            self.end, self.phase = (outcome, why, self.native, s_reply, s_ok), "done"

    def cut(self):
        if self.end is not None:
            return
        self.host_cut = True
        if self.phase == "n":
            self.finish("unconfirmed", "turn_cut") if self.bytes else self.finish("not_delivered", None)
        elif self.phase == "s_turn":
            self.finish("next_turn", "own_turn_cut", s_ok=False)
        else:
            self.finish("unconfirmed", "turn_cut")

    def frame(self, kind):
        if self.end is not None:
            return
        p = self.phase
        if p == "n":
            if not self.bytes:
                # No byte went: as without a steer (step 1). N's result, a plain one, an unlisted error, and one
                # echoing only S's uuid (forgotten when the attempt was retired: a uuid this process never got) all
                # end N, as on main.
                if kind in ("res_n", "res_ns", "res_s", "plain", "error"):
                    self.native = {"res_n": "N", "res_ns": "N", "res_s": "S-only", "plain": "?", "error": ""}[kind]
                    if kind == "error":
                        self.verified = False   # (step 1's rule 10: by order, and unverified from now on)
                    self.finish("not_delivered", None)
                return
            if kind == "res_ns":
                self.native = "N"
                if self.verified:
                    self.finish("in_turn", "answered")
                else:
                    # (ruling L) N's identity intact: N's own result; what it says of S proves nothing
                    self.finish("unconfirmed", "unproved")
            elif kind == "res_n":
                self.native = "N"
                if self.verified and not self.lost:
                    self.phase, self.bracket, self.since = "held", None, self.now
                else:
                    self.phase = "held"
                    self.cut()
            elif kind == "res_s":
                if self.verified and self.bracket == "own" and not self.comp:
                    self.native = "S-only"
                    self.finish("unconfirmed", "s_only")
                else:
                    self.lose()             # names S, not in the measured shape (ruling F)
            elif kind in ("compete", "cli_open"):
                if self.verified:
                    self.comp = True
            elif kind in ("s_init", "s_init_bad", "s_notify_end"):
                self.lose()                 # an init or a terminal naming S inside N's bracket (ruling F)
            elif kind in ("plain", "error"):
                self.lose()
        elif p == "held":
            if kind == "s_init" and self.bracket is None and self.verified:
                self.phase, self.bracket, self.since = "s_turn", "s", self.now
            elif kind in ("s_init", "s_init_bad", "late_ns"):
                self.lose()                 # names S or N in a shape M8 never measured here (ruling F)
                self.cut()
            elif kind in ("cli_open", "compete") and self.bracket is None:
                self.bracket = "cli"
            elif kind == "cli_end" and self.bracket == "cli":
                self.bracket = None
            elif kind == "s_zero_note":         # a zero-turn notification naming [S]: never consumption (L)
                self.lose()
                self.cut()
            elif kind in ("cli_takes_s", "s_notify_end"):     # (the same frame: [S], task-notification)
                if self.bracket == "cli" and self.verified:
                    self.finish("unconfirmed", "consumed")
                else:
                    self.lose()
                    self.cut()
            elif kind in ("plain", "error"):
                self.lose()
                self.cut()
        elif p == "s_turn":
            if kind == "s_res":
                self.finish("next_turn", "own_turn", s_reply="S's reply", s_ok=True)
            elif kind == "s_err":
                self.verified = False       # rule 10: S's own turn failed with it, and unverified from now on
                self.finish("next_turn", "own_turn", s_reply="", s_ok=False)
            elif kind in ("late_ns", "s_notify_end", "s_init", "s_origin_null", "s_zero_note"):
                self.lose()                 # a frame naming S or N not as measured (ruling L)
                self.cut()
            # (s_reinit, cli_open, compete: an init naming neither S nor N: no evidence, no loss: ruling L)

    def lose(self):
        self.verified, self.lost = False, True

    def clock(self, now, T=60.0):
        self.now = now
        if self.end is not None:
            return
        if self.phase == "n" and now - self.n_start >= T:
            self.cut()
        elif self.phase in ("held", "s_turn") and now - self.since >= T:
            self.cut()
        elif self.phase in ("held", "s_turn") and self.cover is not None and now >= self.cover:
            self.cut()


def _m2_frames(n, s_):
    other = "0be7c0de-0000-4000-8000-000000000009"
    return {"res_n": owned(n, text="N"), "res_ns": owned(n, s_, text="N"), "res_s": owned(s_, text="S-only"),
            "compete": vinit(other), "cli_open": vinit(), "cli_end": foreign(), "note": nonturn(),
            "plain": plain(text="?"), "error": result(subtype="error_during_execution", is_error=True, text=""),
            "s_init": vinit(s_), "s_init_bad": vinit(s_) | {"origin": NOTIFY},
            "cli_takes_s": owned(s_, text="took S") | {"origin": NOTIFY}, "late_ns": owned(n, s_, text="late"),
            "s_res": owned(s_, text="S's reply"),
            "s_err": result(subtype="error_max_turns", is_error=True, text=""),
            "s_reinit": vinit(), "s_notify_end": owned(s_, text="a notification's") | {"origin": NOTIFY},
            "s_zero_note": owned(s_, text="") | {"origin": NOTIFY, "num_turns": 0},
            "s_origin_null": owned(s_, text="S's reply") | {"origin": None}}


_MENU = {"n": ["res_n"] * 5 + ["res_ns", "res_ns", "res_s", "compete", "cli_open", "cli_end", "note", "plain",
                                 "error", "s_init"],
         "held": ["s_init"] * 6 + ["s_init_bad", "cli_open", "cli_open", "cli_end", "cli_takes_s", "cli_takes_s",
                                   "cli_takes_s", "late_ns", "note", "plain", "error", "s_notify_end",
                                   "s_zero_note"],
         "s_turn": ["s_res"] * 6 + ["s_err", "s_err", "note", "cli_end", "late_ns", "plain", "s_reinit",
                                    "s_notify_end", "cli_open", "s_origin_null", "s_zero_note"]}


def test_model_claude_steer_walks_against_an_independent_reference(henv, monkeypatch):
    """Ruling D: 900 seeded walks of one Claude session over up to two turns, each turn with no steer or a steer
    written whole, in part (then completed or cut at 5 s), or not at all (queued and retired with zero bytes);
    frames chosen from this test's OWN model phase (never production's), one to three per read; and host-made ends
    (the clock, an exit, a cancel, a close, a stop of every turn, the stop latch then shutdown, the attempt's contact
    clock running out). Each turn's end is published exactly once, with the outcome, the why, N's reply and S's own
    reply and `ok` the reference reads; `ended` comes after it; a turn after a steered one is its own. Reach: every
    outcome row (`in_turn`, `next_turn` ok and cut, `consumed`, `s_only`, `unconfirmed` cut and partial,
    `not_delivered`) is reached, counted here, whatever runs before."""
    import random
    from test_dispatch import FakeProcs, FakeSpawner
    from test_session_host import FakeSessionChild
    from agent_comms.session_host import STEER_CUT
    monkeypatch.setattr(claude, "VERIFIED", frozenset({V}))
    reach: dict = {}
    for seed in range(900):
        rnd = random.Random(seed)
        henv.spawner = FakeSpawner(child=FakeSessionChild)
        henv.procs = FakeProcs(henv.spawner)
        henv.host = make_host(henv, timeout_minutes=1)
        henv.host.steer_authority = lambda sess: True
        s, child = idle_host(henv)
        ended_seen, log, verified, retired = False, [seed], True, None
        for turn in range(1, 3):
            mid = str(turn)
            henv.host.send(SID, f"turn {turn}", message_id=mid)
            assert ("delivered", mid) in [(e.kind, e.message_id) for e in henv.host.tick()], log
            n = uuid_of(child)
            child.emit(vinit(n))
            assert ("turn_started", mid) in [(e.kind, e.message_id) for e in henv.host.tick()], log
            s_ = f"5e7e7e7e-0000-4000-8000-00000000000{turn}"
            write = rnd.choice(["none", "full", "full", "full", "partial", "partial_cut", "zero"])
            if not verified:
                write = "none"      # (the model's own reading: an unverified incarnation never steers)
            log.append((turn, write))
            got: list = []
            model = None
            if write != "none":
                cover = rnd.choice([None, None, 40.0])
                henv.host.steer_offer(SID, mid, s_, TEXT, authorized=True,
                                      cover_until=None if cover is None else henv.clock() + cover)
                if write == "zero":
                    child.accept = 0
                    got += henv.host.tick()
                    henv.host.steer_revoke(SID, s_, "omitted")
                    got += henv.host.tick()
                    child.accept = None
                elif write.startswith("partial"):
                    child.accept = 5
                    got += henv.host.tick()
                    if write == "partial_cut":
                        child.accept = 0
                        henv.clock.advance(6)
                        got += henv.host.tick()
                    child.accept = None
                    got += henv.host.tick()
                else:
                    got += henv.host.tick()
                model = _M2(henv.clock(), 0 if write == "zero" else 1)
                model.now = henv.clock()
                model.cover = None if cover is None else model.n_start + cover - (0 if write != "partial_cut" else 6)
            if write == "partial_cut":
                # the cut: N failed with STEER_CUT, S unconfirmed (partial), the process stopped
                mine = [e for e in got if e.kind == "turn_ended" and e.message_id == mid]
                assert len(mine) == 1 and mine[0].detail == STEER_CUT, log
                assert mine[0].steer == {"outcome": "unconfirmed", "why": "partial"}, (log, mine)
                reach[("unconfirmed", "partial")] = reach.get(("unconfirmed", "partial"), 0) + 1
                break
            if model is None:
                # no steer: an ordinary turn, as main; after a decided steer, sometimes late frames of that retired
                # S first (an error and a result naming it): they end, fail and supply text for nothing (ruling F)
                if retired is not None and rnd.random() < 0.6:
                    child.emit(result(subtype="error_during_execution", is_error=True, text="old S")
                               | {"user_message_uuid": retired, "user_message_uuids": [retired]},
                               owned(retired, text="old S"))
                    assert not [e for e in henv.host.tick() if e.kind == "turn_ended"], log
                    reach[("retired_frames", "")] = reach.get(("retired_frames", ""), 0) + 1
                child.emit(owned(n, text="M"))
                mine = [e for e in henv.host.tick() if e.kind == "turn_ended" and e.message_id == mid]
                assert [(e.ok, e.text, e.steer) for e in mine] == [(True, "M", None)], log
                continue
            stopped = False
            for _ in range(rnd.randint(1, 10)):
                if model.end is not None and [e for e in got if e.kind == "turn_ended" and e.message_id == mid]:
                    break
                act = rnd.choice(["frames"] * 6 + ["wait", "wait", "exit", "cancel", "close", "stop", "latch"])
                log.append(act)
                if act == "frames":
                    # One to three frames in one read, each chosen from the model's phase after the one before; the
                    # read stops at the frame that ends the turn in the model (what follows is the next turn's).
                    kinds = []
                    for _ in range(rnd.randint(1, 3)):
                        k = rnd.choice(_MENU[model.phase])
                        kinds.append(k)
                        model.frame(k)
                        if model.end is not None:
                            break
                    log.append(kinds)
                    frames = _m2_frames(n, s_)
                    child.emit(*[frames[k] for k in kinds])
                    got += henv.host.tick()
                elif act == "wait":
                    henv.clock.advance(rnd.choice([5, 20, 45, 61]))
                    model.clock(henv.clock())
                    got += henv.host.tick()
                elif act == "exit":
                    child.code = 0
                    model.cut()
                    got += henv.host.tick()
                    stopped = True
                elif act == "cancel":
                    model.cut()
                    got += henv.host.cancel(SID, mid, "cancelled") + henv.host.tick()
                    stopped = True
                elif act == "close":
                    model.cut()
                    got += henv.host.close(SID, "closed") + henv.host.tick()
                    stopped = True
                elif act == "stop":
                    model.cut()
                    got += henv.host.stop_turns("lost the hub")
                    stopped = True
                else:   # the stop latch, then shutdown: evidence after the latch never counts
                    henv.host.steer_stopping()
                    frames = _m2_frames(n, s_)
                    child.emit(frames["s_init"], frames["s_res"])
                    model.cut()
                    got += henv.host.tick() + henv.host.stop_all()
                    stopped = True
                model.clock(henv.clock())
                if stopped:
                    break
            for _ in range(40):         # the drain: bounds, the bounded stop
                if [e for e in got if e.kind == "turn_ended" and e.message_id == mid]:
                    break
                henv.clock.advance(5)
                model.clock(henv.clock())
                got += henv.host.tick()
            mine = [e for e in got if e.kind == "turn_ended" and e.message_id == mid]
            assert len(mine) == 1, (log, got)
            end = mine[0]
            want_outcome, want_why, native, s_reply, s_ok = model.end
            assert end.steer is not None and end.steer["outcome"] == want_outcome, (log, end.steer, model.end)
            if want_why is not None:
                assert end.steer["why"] == want_why, (log, end.steer, model.end)
            if native is not None and want_outcome != "not_delivered":
                assert end.text == native, (log, end.text, native)
            if want_outcome == "next_turn":
                assert end.steer["result"]["ok"] is s_ok, (log, end.steer)
                if s_ok:
                    assert end.steer["result"]["text"] == s_reply, (log, end.steer)
            kinds_out = [e.kind for e in got]
            if "ended" in kinds_out:
                assert kinds_out.index("ended") > kinds_out.index("turn_ended"), log
                ended_seen = True
            row = (want_outcome, want_why if want_why else "")
            if want_outcome == "next_turn":
                row = ("next_turn", "ok" if s_ok else "cut")
            reach[row] = reach.get(row, 0) + 1
            verified = model.verified
            retired = s_ if model.bytes else None
            if stopped or ended_seen or model.host_cut or (want_outcome == "next_turn" and not s_ok):
                break       # (the model's own reading: the process was stopped, or S's turn left it unverified)
    for row in [("in_turn", "answered"), ("next_turn", "ok"), ("next_turn", "cut"), ("unconfirmed", "consumed"),
                ("unconfirmed", "s_only"), ("unconfirmed", "turn_cut"), ("unconfirmed", "partial"),
                ("not_delivered", ""), ("retired_frames", ""), ("unconfirmed", "unproved")]:
        assert reach.get(row, 0) >= 3, (row, sorted(reach.items()))
    print("MODEL REACH", sorted(reach.items()))



# ---------------------------------------------------------------- part O's rows the review rated Partly or No (ruling D)


def test_r1_o_a_checkpoint_from_another_authenticated_node_is_refused(env):
    """Part O, the checkpoint: an authenticated node that is not the session's own cannot checkpoint its N."""
    from test_hosted_sessions import client
    sid, child, n, s_uuid = claude_steered(env)
    held = {"message": 1, "ok": True, "detail": "", "denials": [], "meta": {}, "text": "x", "answered": True}
    r = client(env, "desktop").post(f"/api/nodes/sessions/{sid}", json={"held": held})
    assert r.status_code in (403, 404, 409), r.text
    assert "held" not in hosted._get(env.board.conn, hosted._msg_key(sid, 1))


@pytest.mark.parametrize("stop", ["timeout", "stop_all"])
def test_r1_o_an_s_init_first_found_by_the_timeout_drain_and_the_hard_stop_contrast(henv, monkeypatch, stop):
    """Part O, the fresh S clock: S's `init` is in the pipe, unread, when the hold's bound comes. The timeout's drain
    finds it and honours it (S's own turn, its own clock; then its result: `next_turn`). A stop of every turn reads
    nothing (the latch, Shutdown simplified): the same buffered `init` proves nothing (`unconfirmed`)."""
    s, child, n = steered(henv, monkeypatch)
    child.emit(owned(n, text="N"))
    henv.host.tick()
    henv.clock.advance(61)
    child.emit(vinit(S))
    if stop == "timeout":
        out = henv.host.tick()
        assert not ends(out) and s.held is not None and s.held.s_turn, "the drain found S's own turn: its clock"
        child.emit(owned(S, text="PINEAPPLE"))
        [end] = ends(henv.host.tick())
        assert end.steer["outcome"] == "next_turn" and end.steer["result"]["ok"] is True
        assert end.steer["result"]["text"] == "PINEAPPLE"
    else:
        henv.host.steer_stopping()
        [end] = ends(henv.host.stop_all())
        assert end.text == "N" and end.steer == {"outcome": "unconfirmed", "why": "turn_cut"}


@pytest.mark.parametrize("buffered", ["n_result", "s_init", "s_result"])
def test_r1_o_signal_with_buffered_evidence_publishes_n_once_before_ended(henv, monkeypatch, buffered):
    """Part O, `signal()` and `stop_all`: the node's stop latch set while evidence is buffered (N's own result before
    the hold; S's `init` or result in the hold): that evidence never counts; N's end is published once, at main's
    point, and before the session's `ended`."""
    s, child, n = steered(henv, monkeypatch)
    if buffered != "n_result":
        child.emit(owned(n, text="N"))
        henv.host.tick()
        if buffered == "s_result":
            child.emit(vinit(S))
            henv.host.tick()
    child.emit({"n_result": owned(n, text="N"), "s_init": vinit(S), "s_result": owned(S, text="S")}[buffered])
    henv.host.steer_stopping()
    out = henv.host.stop_all() + henv.host.close(SID, "the node is stopping") + henv.host.tick()
    kinds = [(e.kind, e.message_id) for e in out]
    [end] = ends(out)
    assert kinds.count(("turn_ended", "1")) == 1
    if ("ended", None) in kinds:
        assert kinds.index(("turn_ended", "1")) < kinds.index(("ended", None))
    if buffered == "s_result":
        assert end.steer["outcome"] == "next_turn" and end.steer["result"]["ok"] is False, "S's proved turn, cut"
    else:
        assert end.steer["outcome"] in ("unconfirmed", "not_delivered") and end.steer.get("result") is None
    assert henv.host.tick() == [] or not ends(henv.host.tick())


@pytest.mark.parametrize("where", ["held", "s_turn"])
def test_r1_o_a_zero_turn_error_notification_in_the_hold_and_in_ss_own_bracket_is_inert(henv, monkeypatch, where):
    """Part O, R6-04: a zero-turn notification's ERROR result (and a success one) in the hold, or inside S's own
    bracket, is no evidence for S: the hold stays; S's own result still ends it as `next_turn`."""
    s, child, n = steered(henv, monkeypatch)
    child.emit(owned(n, text="N"))
    henv.host.tick()
    if where == "s_turn":
        child.emit(vinit(S))
        henv.host.tick()
    child.emit(nonturn(error=True), nonturn())
    assert henv.host.tick() == [] and s.held is not None and s.held.s_turn is (where == "s_turn")
    if where == "held":
        child.emit(vinit(S))
        henv.host.tick()
    child.emit(owned(S, text="PINEAPPLE"))
    [end] = ends(henv.host.tick())
    assert end.steer["outcome"] == "next_turn" and end.steer["result"]["ok"] is True


def test_r1_o_a_close_with_ss_result_buffered_takes_it_in_the_drain(henv, monkeypatch):
    """Part O, a close with S queued: S's own result is buffered (unread) when the close comes. The close's drain
    reads it before the stop: N's native end with `next_turn` and S's own result, then `ended`."""
    s, child, n = steered(henv, monkeypatch)
    child.emit(owned(n, text="N"))
    henv.host.tick()
    child.emit(vinit(S))
    henv.host.tick()
    child.emit(owned(S, text="PINEAPPLE"))
    out = henv.host.close(SID, "closed") + henv.host.tick() + henv.host.tick()
    kinds = [(e.kind, e.message_id) for e in out]
    [end] = ends(out)
    assert end.text == "N" and end.steer["outcome"] == "next_turn" and end.steer["result"]["text"] == "PINEAPPLE"
    assert kinds.index(("turn_ended", "1")) < kinds.index(("ended", None))


def test_r1_o_a_queued_successor_goes_through_the_reap_and_a_new_process(henv, monkeypatch):
    """Part O, a queued successor: message 2, sent during the hold, waits (nothing is written into the held process);
    the process exits; N's end is published at once (S `unconfirmed`); message 2 then starts in a new process and
    its own result is its own."""
    s, child, n = steered(henv, monkeypatch)
    child.emit(owned(n, text="N"))
    henv.host.tick()
    henv.host.send(SID, "the successor", message_id="2")
    before = child.stdin
    assert not [e for e in henv.host.tick() if e.message_id == "2"] and child.stdin == before, "it waits"
    child.code = 1
    out = henv.host.tick()
    [end] = ends(out)
    assert end.text == "N" and end.steer == {"outcome": "unconfirmed", "why": "turn_cut"}
    out += henv.host.tick()
    assert len(henv.spawner.children) == 2 and child.stdin == before, "a new process; nothing into the old one"
    new = henv.spawner.children[-1]
    m = uuid_of(new)
    new.emit(vinit(m), owned(m, text="M's own"))
    [m_end] = ends(henv.host.tick(), "2")
    assert m_end.ok and m_end.text == "M's own" and m_end.steer is None
    assert not ends(henv.host.tick()), "N's end once"


@pytest.mark.parametrize("mode", ["next_turn", "three"])
def test_r1_o_lifecycle_completed_s_then_a_terminal_without_s_keeps_the_hold(henv, monkeypatch, mode):
    """Part O, R8-05: in either mode, a CLI-started bracket during the hold, a lifecycle row saying S `completed`, and
    that bracket's terminal result NOT listing S: no evidence for S (a lifecycle row is none); the hold runs to its
    bound (`unconfirmed`, the finalizer)."""
    if mode == "three":
        monkeypatch.setattr(claude, "STEER_MODES", {V: claude.THREE})
    s, child, n = steered(henv, monkeypatch)
    child.emit(owned(n, text="N"))
    henv.host.tick()
    child.emit(vinit(), {"type": "command_lifecycle", "command_uuid": S, "state": "completed", "session_id": SID,
                         "uuid": "1f000000-0000-4000-8000-000000000001"}, foreign())
    assert not ends(henv.host.tick()) and s.held is not None and not s.held.s_turn
    henv.clock.advance(61)
    out = henv.host.tick()
    [end] = ends(out) or ends(henv.host.tick())
    assert end.text == "N" and end.steer == {"outcome": "unconfirmed", "why": "turn_cut"}


@pytest.mark.parametrize("phase", ["zero", "partial", "whole"])
def test_r1_o_ns_result_without_s_at_each_write_phase(henv, monkeypatch, phase):
    """Part O, R11-03 for Claude: N's own result lists exactly [N] (authoritatively without S). No byte of S went:
    `not_delivered`, at once. Part of S went: the frame cannot be completed into a turn that ended: the cut,
    `unconfirmed`. All of S went: N's end is held (S may still run as its own turn)."""
    monkeypatch.setattr(claude, "VERIFIED", frozenset({V}))
    henv.host = make_host(henv, timeout_minutes=1)
    henv.host.steer_authority = lambda sess: True
    s, child = idle_host(henv)
    henv.host.send(SID, "a long turn", message_id="1")
    henv.host.tick()
    n = uuid_of(child)
    child.emit(vinit(n))
    henv.host.tick()
    child.accept = {"zero": 0, "partial": 7, "whole": None}[phase]
    henv.host.steer_offer(SID, "1", S, TEXT, authorized=True)
    henv.host.tick()
    child.emit(owned(n, text="N"))
    out = henv.host.tick()
    if phase == "whole":
        assert not ends(out) and [e.kind for e in out] == ["held"] and s.held is not None
        return
    out += henv.host.tick() + henv.host.tick()
    [end] = ends(out)
    assert end.text == "N" and s.held is None
    want = ({"outcome": "not_delivered", "why": "not_written"} if phase == "zero" else
            {"outcome": "unconfirmed", "why": "turn_cut"})
    assert end.steer["outcome"] == want["outcome"], end.steer


def test_r1_o_a_timeout_a_close_and_a_late_reap_overlap(henv, monkeypatch):
    """Part O, timeout, close and late reap: the hold's bound stops the process (its group lingers); a close comes;
    a message waits; the group outlives the graces. N's end is published once, at the bound; `ended` after it;
    nothing is handed over or written into the old process; the late reap publishes nothing more."""
    s, child, n = steered(henv, monkeypatch, grace=10)
    child.exits_on_terminate = False
    child.emit(owned(n, text="N"))
    henv.host.tick()
    henv.groups.add(child.pid)
    henv.clock.advance(61)
    out = henv.host.tick()
    assert not ends(out) and s.stopping_since is not None
    henv.host.send(SID, "waits", message_id="2")
    out += henv.host.close(SID, "closed")
    before = child.stdin
    for _ in range(3):
        henv.clock.advance(10)
        out += henv.host.tick()
    kinds = [(e.kind, e.message_id) for e in out]
    assert kinds.count(("turn_ended", "1")) == 1 and ("delivered", "2") not in kinds and child.stdin == before
    [end] = ends(out)
    assert end.text == "N" and end.steer == {"outcome": "unconfirmed", "why": "turn_cut"}
    henv.groups.discard(child.pid)
    child.code = -9
    out2 = henv.host.tick() + henv.host.tick()
    kinds += [(e.kind, e.message_id) for e in out2]
    assert kinds.count(("turn_ended", "1")) == 1, "the late reap publishes nothing more"
    assert ("ended", None) in kinds and kinds.index(("turn_ended", "1")) < kinds.index(("ended", None))
    assert len(henv.spawner.children) == 1, "nothing started for a closed session"


def test_r1_o_a_group_outliving_sigkill_with_a_queued_successor(henv, monkeypatch):
    """Part O, a group that outlives SIGKILL with a successor queued: N's end at the bound (two graces), once; the
    successor is never written into the old process nor started beside its group; after the late reap it starts in
    a new process; no second end of N."""
    s, child, n = steered(henv, monkeypatch, grace=10)
    child.exits_on_terminate = False
    child.emit(owned(n, text="N"))
    henv.host.tick()
    henv.host.send(SID, "the successor", message_id="2")
    henv.groups.add(child.pid)
    henv.clock.advance(61)
    out = henv.host.tick()
    before = child.stdin
    for _ in range(3):
        henv.clock.advance(10)
        out += henv.host.tick()
    [end] = ends(out)
    assert end.steer == {"outcome": "unconfirmed", "why": "turn_cut"} and s.child is child, "still lingering"
    assert child.stdin == before and len(henv.spawner.children) == 1, "unwritable; nothing beside its group"
    henv.groups.discard(child.pid)
    child.code = -9
    out = henv.host.tick() + henv.host.tick()
    assert not ends(out), "no second end of N"
    assert len(henv.spawner.children) == 2 and ("delivered", "2") in [(e.kind, e.message_id) for e in out]



# ---------------------------------------------------------------- the predicates, pinned (review round 1's mutation check)

X = "0be7c0de-0000-4000-8000-000000000001"


def test_r1_m_after_a_deviation_in_the_same_read_no_s_turn_opens(henv, monkeypatch):
    """A plain result during the hold (a deviation: verification lost), then an `init` [S] and a result [S] in the
    same read: an unverified incarnation proves no S turn; the finalizer, S `unconfirmed`."""
    s, child, n = steered(henv, monkeypatch)
    child.emit(owned(n, text="N"))
    henv.host.tick()
    child.emit(plain(text="?"), vinit(S), owned(S, text="S"))
    out = henv.host.tick() + henv.host.tick()
    [end] = ends(out)
    assert end.text == "N" and end.steer == {"outcome": "unconfirmed", "why": "turn_cut"}


@pytest.mark.parametrize("frame", [
    {"user_message_uuids": [S, "N"], "user_message_uuid": "N"},
    {"user_message_uuids": ["N", S], "user_message_uuid": S}],
    ids=["reversed", "scalar_not_first"])
def test_r1_m_ns_result_with_s_is_exactly_n_then_s(henv, monkeypatch, frame):
    """`in_turn` needs N's result to list exactly [N, S] (a list, in that order, its scalar N): [S, N] and [N, S]
    with scalar S name N WITHOUT N's identity (round 3, ruling L: its scalar N and N first): never N's result, the
    loss of verification; with no other result of N's, N fails at its bound (part A's third exception), S
    `unconfirmed`. ([N, S, X] with scalar N keeps N's identity: `test_r3_l_ns_result_survives_with_its_identity`.)"""
    s, child, n = steered(henv, monkeypatch)
    f = {k: ([n if u == "N" else u for u in v] if isinstance(v, list) else (n if v == "N" else v))
         for k, v in frame.items()}
    child.emit(result(text="N") | f)
    out = henv.host.tick()
    assert not ends(out) and not s.provider.verified, out
    henv.clock.advance(61)
    [end] = ends(henv.host.tick())
    assert end.ok is False and end.detail == STEER_UNATTRIBUTED and end.steer["outcome"] == "unconfirmed"


@pytest.mark.parametrize("frame", [
    {"user_message_uuids": [S, S], "user_message_uuid": S}, {"user_message_uuids": [S]},
    {"user_message_uuids": [S], "user_message_uuid": X}],
    ids=["twice", "no_scalar", "other_scalar"])
def test_r1_m_ss_end_is_a_result_listing_exactly_s(henv, monkeypatch, frame):
    """S's own turn ends only on a result listing exactly [S] with scalar S: [S, S], [S] with no scalar and [S]
    with another scalar name S in a shape M8 never measured (ruling F): never S's end, the loss of verification;
    the finalizer at once: `next_turn` with S cut, never the frame's text."""
    s, child, n = steered(henv, monkeypatch)
    child.emit(owned(n, text="N"))
    henv.host.tick()
    child.emit(vinit(S))
    henv.host.tick()
    child.emit(result(text="?") | frame)
    out = henv.host.tick() + henv.host.tick()
    [end] = ends(out)
    assert end.steer["outcome"] == "next_turn" and end.steer["result"]["ok"] is False
    assert end.steer["result"]["text"] != "?" and not s.provider.verified


def test_r1_m_an_s_only_result_needs_a_byte_of_s(henv, monkeypatch):
    """A result listing exactly [S] in N's bracket when no byte of S went (the pipe took nothing): not the S-only
    case (it is no N result); N's own result then ends N, S `not_delivered`."""
    monkeypatch.setattr(claude, "VERIFIED", frozenset({V}))
    henv.host = make_host(henv, timeout_minutes=1)
    henv.host.steer_authority = lambda sess: True
    s, child = idle_host(henv)
    henv.host.send(SID, "a long turn", message_id="1")
    henv.host.tick()
    n = uuid_of(child)
    child.emit(vinit(n))
    henv.host.tick()
    child.accept = 0
    henv.host.steer_offer(SID, "1", S, TEXT, authorized=True)
    henv.host.tick()
    child.emit(owned(S, text="not N's"))
    assert not ends(henv.host.tick())
    child.accept = None
    child.emit(owned(n, text="N"))
    out = henv.host.tick() + henv.host.tick()
    [end] = ends(out)
    assert end.text == "N" and end.steer["outcome"] == "not_delivered"


def test_r1_m_an_s_only_result_with_a_notification_origin_is_not_ns(henv, monkeypatch):
    """The S-only case needs no origin: a result listing exactly [S] with a notification origin in N's bracket is
    not N's result (N does not end with its text)."""
    s, child, n = steered(henv, monkeypatch)
    child.emit(owned(S, text="a notification's") | {"origin": NOTIFY})
    out = henv.host.tick()
    assert all(e.text != "a notification's" for e in ends(out))


def test_r1_m_departure_4_an_echoless_error_in_ss_bracket_ends_it_and_loses_verification(henv, monkeypatch):
    """Departure 4's rule: inside S's proved bracket, an error result with no echo and no origin is step 1's rule
    10: S's own turn failed with it (`next_turn`, S failed), and the incarnation is unverified from then on (the next
    message's line carries no uuid)."""
    s, child, n = steered(henv, monkeypatch)
    child.emit(owned(n, text="N"))
    henv.host.tick()
    child.emit(vinit(S))
    henv.host.tick()
    child.emit(plain(text="broke", subtype="error_during_execution", is_error=True))
    [end] = ends(henv.host.tick())
    assert end.steer["outcome"] == "next_turn" and end.steer["result"]["ok"] is False
    assert end.steer["result"]["text"] == "broke" and not s.provider.verified
    henv.host.send(SID, "next", message_id="2")
    henv.host.tick()
    assert json.loads(child.stdin.split(b"\n")[-2]).get("uuid") is None, "attributed by order from now on"


def test_r1_m_the_adjudicator_decides_inside_the_latchs_lock(henv, monkeypatch):
    """Ruling A: every piece of evidence for S is decided inside the stop latch's lock (so `steer_stopping` on the
    node's signal thread waits for it): while S's own turn opens, another thread cannot take that lock."""
    import threading
    s, child, n = steered(henv, monkeypatch)
    child.emit(owned(n, text="N"))
    henv.host.tick()
    seen = []
    real = s.provider.steer_facts

    def facts(**kw):
        got = []
        t = threading.Thread(target=lambda: got.append(henv.host._steer_stop_lock.acquire(blocking=False)))
        t.start()
        t.join()
        if got and got[0]:
            henv.host._steer_stop_lock.release()
        seen.append(got[0])
        return real(**kw)
    s.provider.steer_facts = facts
    child.emit(vinit(S))
    henv.host.tick()
    assert seen and seen[0] is False, "the lock was held while S's own turn opened"


def test_r1_m_a_fenced_hold_takes_no_more_evidence(henv, monkeypatch):
    """Once the finalizer decided S (a cancel during S's proved own turn: `next_turn`, S cut) and the stop is under
    way, S's result read before the group is gone changes nothing: N's end is published once, S cut."""
    s, child, n = steered(henv, monkeypatch, grace=10)
    child.exits_on_terminate = False
    child.emit(owned(n, text="N"))
    henv.host.tick()
    child.emit(vinit(S))
    henv.host.tick()
    henv.groups.add(child.pid)
    out = henv.host.cancel(SID, "1", "cancelled")
    child.emit(owned(S, text="too late"))
    for _ in range(3):
        henv.clock.advance(10)
        out += henv.host.tick()
    [end] = ends(out)
    assert end.steer["outcome"] == "next_turn" and end.steer["result"]["ok"] is False
    assert end.steer["result"]["text"] != "too late"


def test_r1_m_the_deadline_is_published_before_any_read_of_the_tick(henv, monkeypatch):
    """Ruling B: the bounded publication's deadline is checked at the very start of a tick: when it is due, N's end
    is already out before the tick's first read of any session."""
    s, child, n = steered(henv, monkeypatch, grace=10)
    child.exits_on_terminate = False
    child.emit(owned(n, text="N"))
    henv.host.tick()
    henv.groups.add(child.pid)
    henv.host.close(SID, "closed")
    henv.host.tick()
    henv.clock.advance(21)
    real = henv.host._read
    seen = []

    def read(sess, now, out):
        seen.append(len(ends(out)))
        return real(sess, now, out)
    henv.host._read = read
    out = henv.host.tick()
    assert len(ends(out)) == 1 and seen and seen[0] == 1, "published before the first read"



# ---------------------------------------------------------------- review round 2, ruling F: one qualification


def _s_turn_open(henv, monkeypatch):
    s, child, n = steered(henv, monkeypatch)
    child.emit(owned(n, text="N"))
    henv.host.tick()
    child.emit(vinit(S))
    henv.host.tick()
    assert s.held is not None and s.held.s_turn
    return s, child, n


def test_r2_f_a_notification_in_ss_own_bracket_never_becomes_ss_reply(henv, monkeypatch):
    """Sol's blocker 1: after `init` [S], a competing notification `init`, then a notification terminal echoing [S]:
    never `next_turn` with the notification's text. The second init in S's bracket is a contradiction: verification
    lost, the finalizer at once, S cut."""
    s, child, n = _s_turn_open(henv, monkeypatch)
    child.emit(vinit() | {"origin": NOTIFY}, owned(S, text="a notification's") | {"origin": NOTIFY})
    out = henv.host.tick() + henv.host.tick()
    [end] = ends(out)
    assert end.text == "N" and end.steer["outcome"] == "next_turn" and end.steer["result"]["ok"] is False
    assert end.steer["result"]["text"] != "a notification's" and not s.provider.verified


def test_r2_f_a_notification_terminal_echoing_s_in_ss_bracket_is_a_contradiction(henv, monkeypatch):
    """The notification terminal alone (no competing init): [S] with a notification origin is not S's end."""
    s, child, n = _s_turn_open(henv, monkeypatch)
    child.emit(owned(S, text="a notification's") | {"origin": NOTIFY})
    out = henv.host.tick() + henv.host.tick()
    [end] = ends(out)
    assert end.steer["result"]["ok"] is False and end.steer["result"]["text"] != "a notification's"


def test_r2_f_a_notification_origin_n_s_result_is_not_in_turn(henv, monkeypatch):
    """Sol's blocker 1: [N, S] with a notification origin is not N's result with S: never `in_turn`. (Round 3,
    ruling L: N's identity in it is intact, so N's own result stands; S `unconfirmed`, verification lost.)"""
    s, child, n = steered(henv, monkeypatch)
    child.emit(owned(n, S, text="a notification's") | {"origin": NOTIFY})
    [end] = ends(henv.host.tick())
    assert end.steer == {"outcome": "unconfirmed", "why": "unproved"} and not s.provider.verified


@pytest.mark.parametrize("extra", [{"origin": {"kind": "human"}}, {"user_message_uuids": {}},
                                   {"user_message_uuids": None}, {"user_message_uuid": 7}],
                         ids=["human_origin", "malformed_dict", "null_list", "nonstring_scalar"])
def test_r2_f_rule_10_needs_an_error_with_no_echo_field_and_no_origin(henv, monkeypatch, extra):
    """Sol's blocker 1: rule 10 inside S's bracket is an error with NO echo field at all and NO origin key: an
    explicit human origin, or a malformed echo field (not "empty"), is not S's failure: S's turn goes on to its
    bound (`next_turn` cut), never failed by that frame at once."""
    s, child, n = _s_turn_open(henv, monkeypatch)
    child.emit(plain(text="whose error", subtype="error_during_execution", is_error=True) | extra)
    out = henv.host.tick()
    assert not [e for e in ends(out) if e.steer and e.steer.get("result", {}).get("text") == "whose error"]


def test_r2_f_a_retired_ss_error_never_fails_the_successor(henv, monkeypatch):
    """Sol's blocker 2: after a successful S, message M is in flight; an error echoing S (retired) between
    `init` [M] and M's result: it ends and fails nothing (checked before rule 10); M goes on to its own result."""
    s, child, n = _s_turn_open(henv, monkeypatch)
    child.emit(owned(S, text="PINEAPPLE"))
    [end] = ends(henv.host.tick())
    assert end.steer["outcome"] == "next_turn"
    henv.host.send(SID, "M", message_id="2")
    henv.host.tick()
    m = uuid_of(child)
    child.emit(vinit(m))
    henv.host.tick()
    child.emit(result(subtype="error_during_execution", is_error=True, text="S's old failure")
               | {"user_message_uuid": S, "user_message_uuids": [S]})
    assert not ends(henv.host.tick(), "2") and s.in_flight.id == "2", "M goes on"
    child.emit(owned(m, text="M's own"))
    [m_end] = ends(henv.host.tick(), "2")
    assert m_end.ok and m_end.text == "M's own" and s.provider.verified


def test_r2_f_a_retired_ss_result_never_supplies_the_successors_text(henv, monkeypatch):
    s, child, n = steered(henv, monkeypatch)
    child.emit(owned(n, S, text="N and S"))
    [end] = ends(henv.host.tick())
    assert end.steer["outcome"] == "in_turn"
    henv.host.send(SID, "M", message_id="2")
    henv.host.tick()
    m = uuid_of(child)
    child.emit(vinit(m), owned(S, text="old"), vinit(S))
    assert not ends(henv.host.tick(), "2")
    child.emit(owned(m, text="M's own"))
    [m_end] = ends(henv.host.tick(), "2")
    assert m_end.text == "M's own"


def test_r2_f_the_retired_set_is_bounded_per_incarnation(henv, monkeypatch):
    """Retired S uuids are kept for the life of the process incarnation and never evicted (round 3, ruling L): at
    RETIRED_MAX the incarnation takes no more steers (nothing written); a new incarnation starts with none."""
    s, child, n = steered(henv, monkeypatch)
    p = s.provider
    for i in range(claude.RETIRED_MAX + 5):
        p._attempt = {"s": f"5e7e7e7e-0000-4000-8000-{i:012d}", "n": "x", "bytes": 1, "phase": "n"}
        p.steer_done()
    assert len(p._retired) == claude.RETIRED_MAX + 5 and f"5e7e7e7e-0000-4000-8000-{0:012d}" in p._retired
    assert p.owns() and p.steer_named(TEXT, "5e7e7e7e-0000-4000-8000-ffffffffffff") is None, "no more steers"
    p.argv(["claude", "-p", "{prompt}"], session_id=SID, resume=True, model=None)
    assert p._retired == set(), "a new incarnation starts with none"



# ---------------------------------------------------------------- review round 2, rulings H and I

SID2 = "5b0c1f2e-7a6d-4e3b-9c8a-1d2e3f4a5b6d"


def _held_pair(henv, monkeypatch):
    """Two verified sessions, each with N (message "1") held after a steer written whole: [(session, child)]."""
    s1, c1, n1 = steered(henv, monkeypatch)
    c1.emit(owned(n1, text="N1"))
    henv.host.tick()
    s2 = open_session(henv, sid=SID2)
    S2 = "5e7e7e7e-0000-4000-8000-0000000000b2"

    def mine(ev):
        return ev | {"session_id": SID2}
    henv.host.send(SID2, "zero", message_id="0")
    henv.host.tick()
    c2 = henv.spawner.children[-1]
    m0 = uuid_of(c2)
    c2.emit(mine(vinit(m0)), mine(owned(m0)))
    henv.host.tick()
    henv.host.send(SID2, "a long turn", message_id="1")
    henv.host.tick()
    n2 = uuid_of(c2)
    c2.emit(mine(vinit(n2)))
    henv.host.tick()
    henv.host.steer_offer(SID2, "1", S2, TEXT, authorized=True)
    henv.host.tick()
    c2.emit(mine(owned(n2, text="N2")))
    henv.host.tick()
    assert s1.held is not None and s2.held is not None
    return [(s1, c1), (s2, c2)]


@pytest.mark.parametrize("stop", ["stop_turns", "stop_all"])
def test_r2_h_every_sigterm_goes_before_a_held_publication_persists(henv, monkeypatch, stop):
    """Sol's blocker 4 (ruling H): a stop of every turn publishes every held end at main's point, and every session
    gets its SIGTERM before anything is persisted: `_save_state` (made to record, as a blocking one would show)
    is first called only once both processes were signalled."""
    pair = _held_pair(henv, monkeypatch)
    terms = []
    for s, c in pair:
        real = c.terminate

        def term(real=real):
            terms.append(1)
            return real()
        c.terminate = term
    saved = []
    real_save = henv.host._save_state
    henv.host._save_state = lambda: (saved.append(len(terms)), real_save())
    out = henv.host.stop_turns("lost the hub") if stop == "stop_turns" else henv.host.stop_all()
    assert len([e for e in out if e.kind == "turn_ended"]) == 2
    assert saved and saved[0] == 2, (saved, terms)


def test_r2_h_the_cutoff_lock_is_not_held_while_a_publication_logs_or_saves(henv, monkeypatch):
    """Ruling H: S's own end is decided and the attempt retired under the cutoff lock; the state is saved after it
    is let go (another thread can take it then)."""
    import threading
    s, child, n = _s_turn_open(henv, monkeypatch)
    seen = []
    real_save = henv.host._save_state

    def probe(got):
        ok = henv.host._steer_stop_lock.acquire(blocking=False)
        if ok:
            henv.host._steer_stop_lock.release()
        got.append(ok)

    def save():
        got = []
        t = threading.Thread(target=probe, args=(got,))
        t.start()
        t.join()
        seen.append(got[0])
        return real_save()
    henv.host._save_state = save
    child.emit(owned(S, text="PINEAPPLE"))
    [end] = ends(henv.host.tick())
    assert end.steer["outcome"] == "next_turn" and seen and all(seen), seen


def test_r2_i_a_read_error_then_a_raising_sigterm_never_loses_ns_end(henv, monkeypatch):
    """Sol's blocker 5 (ruling I): N held; a read fails, then the SIGTERM raises and the exception escapes `tick`.
    N's end was published into the host's outbox: the next tick delivers it, exactly once."""
    s, child, n = steered(henv, monkeypatch)
    child.emit(owned(n, text="N"))
    henv.host.tick()

    def broken():
        raise OSError("read failed")

    def bad_term():
        raise OSError("terminate failed")
    child.read_lines, child.terminate = broken, bad_term
    with pytest.raises(OSError):
        henv.host.tick()
    child.read_lines = lambda: []
    child.terminate = lambda: None
    out = henv.host.tick()
    assert [e.text for e in ends(out)] == ["N"]
    assert not ends(henv.host.tick()), "exactly once"


def test_r2_i_a_first_fence_check_that_raises_publishes_at_once_before_any_read(henv, monkeypatch):
    """Ruling I, the guard `_hold_deadline`'s error publishes at once: the tick's first fence check raises (once);
    N's end is out before the tick's first read (the second check, in the pass, would come after the reads)."""
    s, child, n = steered(henv, monkeypatch, grace=10)
    child.exits_on_terminate = False
    child.emit(owned(n, text="N"))
    henv.host.tick()
    henv.groups.add(child.pid)
    henv.host.close(SID, "closed")
    henv.host.tick()
    henv.clock.advance(5)                       # before the deadline: only the raising check publishes
    real_fence = henv.host._hold_fence
    calls = []

    def fence(sess, now, out):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("the first fence check fails")
        return real_fence(sess, now, out)
    henv.host._hold_fence = fence
    real_read = henv.host._read
    seen = []

    def read(sess, now, out):
        seen.append(len(ends(out)))
        return real_read(sess, now, out)
    henv.host._read = read
    out = henv.host.tick()
    assert len(ends(out)) == 1 and (not seen or seen[0] == 1), seen


def test_r2_i_a_host_error_publishes_before_its_stop_is_attempted(henv, monkeypatch):
    """Ruling I, the guard `_at_once` on the host error's `_end`: N's held end is published BEFORE the stop's
    SIGTERM is attempted (a stop that blocks or raises cannot hold it back)."""
    s, child, n = steered(henv, monkeypatch)
    child.emit(owned(n, text="N"))
    henv.host.tick()
    seen = []

    def broken():
        raise OSError("read failed")

    def term():
        seen.append(len(ends(henv.host._outbox)))
        raise OSError("terminate blocked, then failed")
    child.read_lines, child.terminate = broken, term
    with pytest.raises(OSError):
        henv.host.tick()
    assert seen == [1], "published before the SIGTERM was attempted"


def test_r2_i_a_read_error_after_a_close_fenced_n_publishes_at_once(henv, monkeypatch):
    """Ruling I, the guard `_publish_now` in the host error's `finally`: a close marked the session ended and fenced
    N (its group lingers); a read error before the grace: `_end` is skipped (already ended), and only the `finally`
    publishes N's end and `ended`, at once."""
    s, child, n = steered(henv, monkeypatch, grace=10)
    child.exits_on_terminate = False
    child.emit(owned(n, text="N"))
    henv.host.tick()
    henv.groups.add(child.pid)
    out = henv.host.close(SID, "closed")
    assert not ends(out) and s.ended

    def broken():
        raise OSError("read failed")
    child.read_lines = broken
    henv.clock.advance(1)
    out = henv.host.tick()
    kinds = [(e.kind, e.message_id) for e in out]
    assert ("turn_ended", "1") in kinds and ("ended", None) in kinds, kinds


def test_r2_the_bounded_publication_counts_from_the_sigterm(henv, monkeypatch):
    """Should: the 2 x kill_grace clock starts when the SIGTERM is sent (a drain before it does not shorten it)."""
    s, child, n = steered(henv, monkeypatch, grace=10)
    child.exits_on_terminate = False
    child.emit(owned(n, text="N"))
    henv.host.tick()
    henv.groups.add(child.pid)
    real_term = child.terminate
    stamped = []

    def term():
        stamped.append(henv.host.clock())
        return real_term()
    child.terminate = term
    real_poll = child.poll

    def slow_poll():
        henv.clock.advance(2)       # the probe before the SIGTERM takes time
        return real_poll()
    child.poll = slow_poll
    t0 = henv.clock()
    henv.host.close(SID, "closed")
    assert stamped and stamped[0] > t0 and s.held is not None and s.held.fenced_at == stamped[0]



# ---------------------------------------------------------------- review round 2, ruling G


@pytest.mark.parametrize("where", ["held", "s_turn"])
def test_r2_g_an_omission_never_masks_the_contact_running_out(henv, monkeypatch, where):
    """Sol's blocker 3: an authoritative omission revokes the steer's writing first; its contact clock then runs out
    during the hold: the hold is cut all the same (the watchdog's boundary, `steer_deadlines`)."""
    s, child, n = steered(henv, monkeypatch)
    s.steer.cover_until = henv.clock() + 20
    child.emit(owned(n, text="N"))
    henv.host.tick()
    if where == "s_turn":
        child.emit(vinit(S))
        henv.host.tick()
    henv.host.steer_revoke(SID, S, "omitted")
    assert s.steer.revoked == "omitted"
    henv.clock.advance(21)
    out = henv.host.steer_deadlines()
    assert child.terminated and s.steer.expired and s.steer.revoked == "omitted", "cut; the first reason stays"
    out += henv.host.tick() + henv.host.tick()
    [end] = ends(out)
    assert end.text == "N"
    if where == "s_turn":
        assert end.steer["outcome"] == "next_turn" and "contact" in end.steer["result"]["detail"]
    else:
        assert end.steer == {"outcome": "unconfirmed", "why": "turn_cut"}


def test_r2_g_after_the_latch_the_hold_pass_initiates_no_stop(henv, monkeypatch):
    """Sol's blocker 3: once the global stop latch is set, neither the loop's hold pass nor the watchdog's boundary
    starts a single-session stop (even with the contact run out): the stop of every turn publishes N's held end
    from what was processed, once."""
    s, child, n = steered(henv, monkeypatch)
    s.steer.cover_until = henv.clock() + 20
    child.emit(owned(n, text="N"))
    henv.host.tick()
    henv.host.steer_stopping()
    henv.clock.advance(21)
    out = henv.host.steer_deadlines() + henv.host.tick()
    assert not child.terminated and not ends(out) and s.stopping_since is None
    [end] = ends(henv.host.stop_all())
    assert end.text == "N" and end.steer == {"outcome": "unconfirmed", "why": "turn_cut"}


def test_r2_g_the_real_watchdog_cuts_a_full_hold_while_the_loop_is_blocked(env):
    """Ruling G through the node: N held, the hub then unreachable (the loop makes no pass: blocked on hub I/O);
    the node's deadline watchdog (`SessionRunner.enforce_deadlines`, its own thread's call) cuts the hold once the
    attempt's contact ran out: the process gets its SIGTERM with no pass of the loop."""
    sid, child, n, s_uuid = claude_steered(env)
    child.emit(cresult(sid, n, text="N's reply"))
    tick(env, times=2)
    s = env.node.sessions.host.sessions[sid]
    assert s.held is not None and s.steer.cover_until is not None
    env.clock.advance(node_sessions.STEER_CONTACT_SECONDS + 1 if hasattr(node_sessions, "STEER_CONTACT_SECONDS")
                      else 76)
    env.node.sessions.enforce_deadlines()
    assert child.terminated and s.held is not None and s.held.fenced_at is not None


def test_r2_g_runner_signal_during_a_hold_then_shutdown_publishes_n_once(env):
    """Ruling G through `SessionRunner.signal`: the node's signal during a hold (contact run out meanwhile): no
    single-session stop is initiated after it; shutdown publishes N's held end once, at main's point."""
    sid, child, n, s_uuid = claude_steered(env)
    child.emit(cresult(sid, n, text="N's reply"))
    tick(env, times=2)
    runner = env.node.sessions
    s = runner.host.sessions[sid]
    assert s.held is not None
    runner.signal()
    env.clock.advance(80)
    runner.enforce_deadlines()
    assert s.held is not None and s.held.fenced_at is None, "nothing single-session after the latch"
    runner.kill()
    runner.shutdown()
    m = hosted._get(env.board.conn, hosted._msg_key(sid, 1))
    rec = hosted_steer.get(env.board.conn, sid, 1)
    assert m["state"] == "done" and (rec["outcome"], rec["why"]) == ("unconfirmed", "turn_cut"), (m, rec)



# ---------------------------------------------------------------- review round 2, rulings J and K


def test_r2_j_a_claude_alias_runtime_steers_end_to_end(env):
    """Sol's blocker 6 (ruling J): a session whose agent's runtime is another Claude name (here `claude`), with a
    `claude` runner: S written, N's end held and checkpointed, S's own turn, N settled with `next_turn`: the hub
    goes by the capability recorded with the attempt (`held`), never by a literal runtime string."""
    from conftest import PROJECT
    runners = env.node.sessions.host.config.runners
    runners["claude"] = runners["claude-code"]      # (the agent's own runner: a `claude` CLI)
    sid = start(env, "a long turn", project=PROJECT)["session_id"]
    with hosted.db.write_tx(env.board.conn) as c:
        rec = hosted._session(c, sid)
        rec["runtime"] = "claude"
        hosted._put(c, hosted.SESSION_PREFIX + sid, rec, "hub", env.clock())
    tick(env)
    child = proc_of(env, sid)[1]
    n = written(child)[-1]["uuid"]
    child.emit(cinit(sid, n))
    tick(env)
    r = env.human.post(f"/api/hosted-sessions/{sid}/messages", json={"message": TEXT, "mode": "steer"})
    assert r.status_code == 200, r.text
    tick(env)
    tick(env)
    rec = hosted_steer.get(env.board.conn, sid, 1)
    assert rec["holds"] is True and any(x.get("uuid") == rec["uuid"] for x in written(child))
    child.emit(cresult(sid, n, text="N's reply"))
    tick(env, times=2)
    assert hosted._get(env.board.conn, hosted._msg_key(sid, 1))["held"]["text"] == "N's reply"
    child.emit(cinit(sid, rec["uuid"]), cresult(sid, rec["uuid"], text="PINEAPPLE"))
    tick(env, times=3)
    st = hosted_steer.get(env.board.conn, sid, 1)
    assert (st["outcome"], st["why"]) == ("next_turn", "own_turn") and st["result"]["reply"] == "PINEAPPLE"


def test_r2_j_a_step_2_hub_is_never_offered_a_claude_steer(env, monkeypatch):
    """Ruling J, fail closed: against a hub whose listing names `steer` but not `held` (step 2), the node reports
    neither `steer` nor `held` for a Claude session (the hub refuses a steer into it), and its write authority for a
    Claude steer is rechecked at the write: none while the hub does not name `held`."""
    monkeypatch.setattr(hosted, "NODE_REPORT_FEATURES", ("answered", "restart", "steer"))
    sid = start(env, "a long turn")["session_id"]
    tick(env)
    child = proc_of(env, sid)[1]
    n = written(child)[-1]["uuid"]
    child.emit(cinit(sid, n))
    tick(env, times=2)
    assert "steer" not in (hosted._session(env.board.conn, sid).get("modes") or [])
    r = env.human.post(f"/api/hosted-sessions/{sid}/messages", json={"message": TEXT, "mode": "steer"})
    assert r.status_code != 200 and "cannot steer" in r.text
    runner = env.node.sessions
    s = runner.host.sessions[sid]
    assert runner.hub_features == {"answered", "restart", "steer"} and not runner._steer_capable(s)
    assert runner._steer_authority(s), "the capability gates writes only, never the attempt's authority"


def test_r2_k_a_result_is_reported_while_checkpoint_posts_are_stalled(env):
    """Sol's blocker 7 (ruling K): checkpoints go through their own sender thread; with its posts stalled, another
    session's result is reported by the loop at once, without waiting for them."""
    import threading
    import time as _time
    from test_hosted_sessions import P2
    sid, child, n, s_uuid = claude_steered(env)
    other = start(env, "an ordinary turn", project=P2)["session_id"]
    tick(env)
    ochild = proc_of(env, other)[1]
    m = written(ochild)[-1]["uuid"]
    ochild.emit(cinit(other, m))
    tick(env)
    runner = env.node.sessions
    real = runner.client.request
    release, stalled = threading.Event(), threading.Event()

    posters = []

    def request(method, url, *a, **kw):
        if (kw.get("json") or {}).get("held") is not None:
            posters.append(threading.current_thread())
            stalled.set()
            release.wait(10)
        return real(method, url, *a, **kw)
    runner.client.request = request
    runner.start_checkpoint_sender()
    try:
        child.emit(cresult(sid, n, text="N's reply"))
        tick(env, times=2)
        assert stalled.wait(5), "the checkpoint is being posted (and stalls)"
        assert posters and all(t is runner.checkpoint_thread for t in posters), "by the sender's thread only"
        ochild.emit(cresult(other, m, text="the other's reply"))
        t0 = _time.monotonic()
        tick(env)
        assert _time.monotonic() - t0 < 3, "the loop did not wait for the stalled checkpoint"
        assert hosted._get(env.board.conn, hosted._msg_key(other, 1))["state"] == "done"
    finally:
        release.set()
        runner.stopping.set()
        runner.checkpoint_thread.join(5)


def test_r2_k_a_checkpoint_is_rechecked_at_dispatch(env):
    """Ruling K: at dispatch the sender rechecks the hub's capability and that the session's N is still the one held;
    otherwise the checkpoint is dropped (and nothing else changes)."""
    sid, child, n, s_uuid = claude_steered(env)
    child.emit(cresult(sid, n, text="N's reply"))
    runner = env.node.sessions
    runner.host.tick()
    held = {"message": 1, "ok": True, "detail": "", "denials": [], "meta": {}, "text": "x", "answered": True}
    gen = runner.host.sessions[sid].gen
    assert runner._checkpoint_due(node_sessions.Checkpoint(sid, gen, 1, held))
    assert not runner._checkpoint_due(node_sessions.Checkpoint(sid, gen, 2, held)), "another N"
    runner.hub_features = runner.hub_features - {"held"}
    assert not runner._checkpoint_due(node_sessions.Checkpoint(sid, gen, 1, held)), "the hub no longer names held"


def test_r2_shoulds_the_hub_refuses_a_mixed_checkpoint_report_and_impossible_next_turns(env):
    """Shoulds: a checkpoint is a report of its own (one mixed with events or status is refused); `next_turn` takes
    only the shapes a host makes (own_turn answered; own_turn_cut failed, unanswered, no reply)."""
    sid, child, n, s_uuid = claude_steered(env)
    held = {"message": 1, "ok": True, "detail": "", "denials": [], "meta": {}, "text": "x", "answered": True}
    r = env.laptop.post(f"/api/nodes/sessions/{sid}", json={"held": held, "events": [{"kind": "delivered",
                                                                                      "message": 1}]})
    assert r.status_code in (400, 422) and "report of its own" in r.text
    r = env.laptop.post(f"/api/nodes/sessions/{sid}", json={"held": held, "state": "running", "pid": 1})
    assert r.status_code in (400, 422)
    base = {"ok": True, "detail": "", "denials": [], "meta": {}, "text": "S", "answered": True}
    assert hosted_steer.extension({"outcome": "next_turn", "why": "own_turn", "result": base}) is not None
    for why, res in (("own_turn", base | {"answered": False}), ("own_turn_cut", base),
                     ("own_turn_cut", base | {"ok": False}), ("whatever", base),
                     ("own_turn", base | {"message": 1})):
        assert hosted_steer.extension({"outcome": "next_turn", "why": why, "result": res}) is None, (why, res)


def test_r2_shoulds_a_surrogate_in_ss_optional_fields_never_breaks_a_report(env):
    """Shoulds: invalid Unicode (a lone surrogate) in S's metadata or text is sanitised on the node, so the report
    still encodes and reaches the hub."""
    sid, child, n, s_uuid = claude_steered(env)
    child.emit(cresult(sid, n, text="N's reply"))
    tick(env, times=2)
    child.emit(cinit(sid, s_uuid), cresult(sid, s_uuid, text="S \ud800 reply") | {"stop_reason": "x\udfff"})
    tick(env, times=3)
    st = hosted_steer.get(env.board.conn, sid, 1)
    assert st["outcome"] == "next_turn" and "\ud800" not in json.dumps(st)


def test_r2_shoulds_a_stale_checkpoint_is_cleared_when_step_2_settled_both(env):
    """Shoulds: a downgrade to step 2 settled both N and S, leaving N's checkpoint: the re-upgrade sweep clears it."""
    sid, child, n, s_uuid = claude_steered(env)
    child.emit(cresult(sid, n, text="N's reply"))
    tick(env, times=2)
    with hosted.db.write_tx(env.board.conn) as c:
        m = hosted._get(c, hosted._msg_key(sid, 1))
        m["state"] = "done"
        hosted._put(c, hosted._msg_key(sid, 1), m, "hub", env.clock())
        st = hosted_steer.get(c, sid, 1)
        st |= {"outcome": "unconfirmed", "why": "no_word", "settled_at": env.clock()}
        hosted._put(c, hosted_steer._key(sid, 1), st, "hub", env.clock())
    assert "held" in hosted._get(env.board.conn, hosted._msg_key(sid, 1))
    hosted_steer.sweep(env.board, hosted._session(env.board.conn, sid))
    assert "held" not in hosted._get(env.board.conn, hosted._msg_key(sid, 1))



# ---------------------------------------------------------------- review round 2: part O's Partly rows, the shoulds


def test_r2_o_an_s_init_read_only_by_the_timeout_drain_gets_its_clock_from_the_read(henv, monkeypatch):
    """Part O (a drain-only read) and the should on its stamp: the hold's bound comes; S's `init` arrives only
    during the timeout's drain (the pass's own read saw nothing); it is honoured, and S's clock starts when it was
    read (a drain's read is later than the pass's `now`)."""
    s, child, n = steered(henv, monkeypatch)
    child.emit(owned(n, text="N"))
    henv.host.tick()
    henv.clock.advance(61)
    t_pass = henv.clock()
    fed = []

    def hook(sess):
        if not fed:
            fed.append(1)
            henv.clock.advance(1)
            child.emit(vinit(S))
    henv.host.steer_drain_hook = hook
    out = henv.host.tick()
    assert fed and not ends(out) and s.held is not None and s.held.s_turn
    assert s.busy_since == t_pass + 1, "S's clock from the moment its init was read"


@pytest.mark.parametrize("phase", ["zero", "partial", "whole"])
def test_r2_o_an_omission_at_each_byte_phase_then_the_contact_running_out(henv, monkeypatch, phase):
    """Part O (R11-03 then contact expiry): the listing omits the steer (its writing revoked) with no byte, part or
    all of S written; then the attempt's contact runs out (the watchdog's boundary). No byte: nothing more, N's end
    `not_delivered`. Part: the frame is cut (`unconfirmed`, partial). All: the hold is cut (`unconfirmed`)."""
    monkeypatch.setattr(claude, "VERIFIED", frozenset({V}))
    henv.host = make_host(henv, timeout_minutes=1)
    henv.host.steer_authority = lambda sess: True
    s, child = idle_host(henv)
    henv.host.send(SID, "a long turn", message_id="1")
    henv.host.tick()
    n = uuid_of(child)
    child.emit(vinit(n))
    henv.host.tick()
    child.accept = {"zero": 0, "partial": 7, "whole": None}[phase]
    henv.host.steer_offer(SID, "1", S, TEXT, authorized=True, cover_until=henv.clock() + 20)
    henv.host.tick()
    if phase == "whole":
        child.emit(owned(n, text="N"))
        henv.host.tick()
        assert s.held is not None
    henv.host.steer_revoke(SID, S, "omitted")
    henv.clock.advance(21)
    out = henv.host.steer_deadlines()
    child.accept = None
    if phase == "zero":
        child.emit(owned(n, text="N"))
    out += henv.host.tick() + henv.host.tick() + henv.host.tick()
    [end] = ends(out)
    want = {"zero": "not_delivered", "partial": "unconfirmed", "whole": "unconfirmed"}[phase]
    assert end.steer["outcome"] == want, end.steer
    if phase == "partial":
        assert end.steer["why"] == "partial"


def test_r2_o_runner_signal_before_ns_end_publishes_n_once(env):
    """Part O, `SessionRunner.signal` before the hold: S written, N in flight; the node's signal; N's own result then
    read counts for nothing about S: shutdown publishes N's end once, S `unconfirmed`."""
    sid, child, n, s_uuid = claude_steered(env)
    runner = env.node.sessions
    runner.signal()
    child.emit(cresult(sid, n, text="N's reply"))
    runner.kill()
    runner.shutdown()
    rec = hosted_steer.get(env.board.conn, sid, 1)
    assert hosted._get(env.board.conn, hosted._msg_key(sid, 1))["state"] in ("done", "failed")
    assert rec["outcome"] == "unconfirmed", rec


def test_r2_o_a_crash_after_a_dropped_checkpoint_settles_n_by_its_own_end(env):
    """Part O, a crash after a dropped checkpoint: the hub refuses N's checkpoint (dropped, never resent); then the
    process crashes during the hold: N's held native end is published at once and the hub settles N by it (its
    reply), S `unconfirmed`; nothing else was changed by the dropped checkpoint."""
    sid, child, n, s_uuid = claude_steered(env)
    runner = env.node.sessions
    real = runner.client.request

    def refuse(method, url, *a, **kw):
        if (kw.get("json") or {}).get("held") is not None:
            raise httpx.HTTPStatusError("refused", request=httpx.Request("POST", "http://x"),
                                        response=httpx.Response(409))
        return real(method, url, *a, **kw)
    runner.client.request = refuse
    child.emit(cresult(sid, n, text="N's reply"))
    tick(env, times=2)
    assert "held" not in hosted._get(env.board.conn, hosted._msg_key(sid, 1)), "the checkpoint was dropped"
    child.code = -9
    tick(env, times=3)
    m = hosted._get(env.board.conn, hosted._msg_key(sid, 1))
    rec = hosted_steer.get(env.board.conn, sid, 1)
    assert m["state"] == "done" and show(env, sid)["reply"] == "N's reply", m
    assert rec["outcome"] == "unconfirmed", rec



def test_r2_j_a_failed_fetch_never_cuts_a_hold(env):
    """Ruling J's write-time recheck is the write gate's, never the attempt's authority: a failed hub call forgets
    the hub's features for a while; a held N is not cut for it (the e2e's Z7 found this)."""
    sid, child, n, s_uuid = claude_steered(env)
    child.emit(cresult(sid, n, text="N's reply"))
    tick(env, times=2)
    runner = env.node.sessions
    s = runner.host.sessions[sid]
    assert s.held is not None
    runner.hub_features, runner.features_known = set(), False      # (what `_call` does after a failed call)
    runner.enforce_deadlines()
    runner.host.tick()
    assert not child.terminated and s.held is not None and s.held.fenced_at is None



# ---------------------------------------------------------------- review round 2: the mutation check's misses pinned


def test_r2_m_an_s_init_with_a_scalar_echo_is_a_contradiction(henv, monkeypatch):
    """An `init` has no scalar echo (M8): [S] with `user_message_uuid` S opens no S turn: the loss, `unconfirmed`."""
    s, child, n = steered(henv, monkeypatch)
    child.emit(owned(n, text="N"))
    henv.host.tick()
    child.emit(vinit(S) | {"user_message_uuid": S}, owned(S, text="S"))
    out = henv.host.tick() + henv.host.tick()
    [end] = ends(out)
    assert end.steer == {"outcome": "unconfirmed", "why": "turn_cut"} and not s.provider.verified


def test_r2_m_a_late_result_naming_only_n_in_the_hold_is_a_contradiction(henv, monkeypatch):
    """Ruling F: N's result came; a later result naming N alone is not the measured shape: the loss at once."""
    s, child, n = steered(henv, monkeypatch)
    child.emit(owned(n, text="N"))
    henv.host.tick()
    child.emit(owned(n, text="again"))
    out = henv.host.tick() + henv.host.tick()
    [end] = ends(out)
    assert end.text == "N" and end.steer == {"outcome": "unconfirmed", "why": "turn_cut"}


def test_r2_m_consumed_needs_the_notification_origin_measured(henv, monkeypatch):
    """`s_consumed` is a CLI-started turn's terminal [S] with the origin M8 measured (`task-notification`); the same
    [S] with no origin inside the CLI's bracket is a contradiction (the loss), never `consumed`."""
    s, child, n = steered(henv, monkeypatch)
    child.emit(owned(n, text="N"))
    henv.host.tick()
    child.emit(vinit(), owned(S, text="no origin"))
    out = henv.host.tick() + henv.host.tick()
    [end] = ends(out)
    assert end.steer == {"outcome": "unconfirmed", "why": "turn_cut"}


def test_r2_m_an_acknowledgement_after_an_omission_still_keeps_the_contact(henv, monkeypatch):
    """Ruling G: the contact clock is its own: an acknowledgement after an omission (writing revoked) still extends
    it, so a held attempt is not cut at the old deadline."""
    from agent_comms.session_host import STEER_CONTACT_SECONDS
    s, child, n = steered(henv, monkeypatch)
    s.steer.cover_until = henv.clock() + 20
    child.emit(owned(n, text="N"))
    henv.host.tick()
    henv.host.steer_revoke(SID, S, "omitted")
    henv.clock.advance(10)
    s.steer.ack(henv.clock(), henv.clock, SID, 1)
    assert s.steer.cover_until == henv.clock() + STEER_CONTACT_SECONDS
    henv.clock.advance(15)                          # past the old deadline, inside the new one
    henv.host.steer_deadlines()
    assert not child.terminated and s.held is not None and not s.steer.expired


def test_r2_m_the_write_gate_asks_the_hubs_capability(henv, monkeypatch):
    """Ruling J: the write gate asks `steer_capable` at the write: while it says no, not one byte of S is written."""
    monkeypatch.setattr(claude, "VERIFIED", frozenset({V}))
    henv.host = make_host(henv, timeout_minutes=1)
    henv.host.steer_authority = lambda sess: True
    henv.host.steer_capable = lambda sess: False
    s, child = idle_host(henv)
    henv.host.send(SID, "a long turn", message_id="1")
    henv.host.tick()
    child.emit(vinit(uuid_of(child)))
    henv.host.tick()
    henv.host.steer_offer(SID, "1", S, TEXT, authorized=True)
    henv.host.tick()
    assert S.encode() not in child.stdin


def test_r2_m_a_listing_without_held_takes_no_claude_steer_offer(env):
    """Ruling J: a listing that offers a Claude session's steer but does not name `held` (a step-2 hub's) is not
    taken: no attempt is made from it."""
    sid = start(env, "a long turn")["session_id"]
    tick(env)
    child = proc_of(env, sid)[1]
    n = written(child)[-1]["uuid"]
    child.emit(cinit(sid, n))
    tick(env)
    r = env.human.post(f"/api/hosted-sessions/{sid}/messages", json={"message": TEXT, "mode": "steer"})
    assert r.status_code == 200, r.text
    data = env.laptop.get("/api/nodes/sessions", params={"tree": 5}).json()
    assert any(v.get("steer") for v in data["sessions"] if v.get("session_id") == sid), "the listing offers it"
    data["features"] = [f for f in data["features"] if f != "held"]
    runner = env.node.sessions
    with runner.lock:
        runner._reconcile(data)
    assert runner.host.steer_slot(sid) is None, "no attempt taken from a listing without held"


def test_r2_m_a_codex_attempt_is_recorded_without_the_held_capability(env):
    """Ruling J: the hub records the capability the attempt is written under: a Codex steer's record has `holds`
    False (its node never says `held`), so it never takes a checkpoint or `next_turn`."""
    from test_codex_steering import codex_turn_in_flight, post_steer
    from test_hosted_sessions import P3
    sid, child = codex_turn_in_flight(env, project=P3)
    post_steer(env, sid)
    assert hosted_steer.get(env.board.conn, sid, 1)["holds"] is False


def test_r2_m_a_checkpoint_is_not_the_nodes_contact(env):
    """Ruling K: the hub does not count a checkpoint-only report as the node loop's contact."""
    sid, child, n, s_uuid = claude_steered(env)
    child.emit(cresult(sid, n, text="N's reply"))
    with env.node.sessions.lock:
        env.node.sessions.host.tick()           # N held; nothing reported yet
    before = hosted._loop(env.board.conn, "laptop").get("contact")
    env.clock.advance(hosted.LOOP_CONTACT_EVERY + 5)
    held = {"message": 1, "ok": True, "detail": "", "denials": [], "meta": {}, "text": "N's reply", "answered": True}
    r = env.laptop.post(f"/api/nodes/sessions/{sid}", json={"held": held})
    assert r.status_code == 200, r.text
    assert hosted._loop(env.board.conn, "laptop").get("contact") == before



# ---------------------------------------------------------------- review round 3, ruling L: evidence (the adapter)


def test_r3_l_ns_result_survives_with_its_identity(henv, monkeypatch):
    """Ruling L: [N, S, X] with scalar N carries N's identity (scalar N, N first): N's own result stands; the extra
    entries only downgrade S (`unconfirmed`, `unproved`) and lose verification. N is never discarded for them."""
    s, child, n = steered(henv, monkeypatch)
    child.emit(owned(n, S, X, text="N's own"))
    [end] = ends(henv.host.tick())
    assert end.ok and end.text == "N's own" and end.steer == {"outcome": "unconfirmed", "why": "unproved"}
    assert not s.provider.verified


def test_r3_l_a_zero_turn_notification_naming_s_is_not_consumption(henv, monkeypatch):
    """Sol's should: a zero-turn notification echoing exactly [S] inside the CLI's bracket is no measured terminal:
    not consumption (it names S unmeasured: the loss), never `consumed`."""
    s, child, n = steered(henv, monkeypatch)
    child.emit(owned(n, text="N"))
    henv.host.tick()
    child.emit(vinit(), owned(S, text="") | {"origin": NOTIFY, "num_turns": 0})
    out = henv.host.tick() + henv.host.tick()
    [end] = ends(out)
    assert end.steer == {"outcome": "unconfirmed", "why": "turn_cut"}


def test_r3_l_the_257th_steer_never_reopens_a_successors_misattribution(henv, monkeypatch):
    """Sol's blocker: no eviction. After more than RETIRED_MAX decided steers the first S is still known: its late
    error between `init` [M] and M's result fails nothing; M ends with its own result. (At the bound the
    incarnation takes no more steers.)"""
    s, child, n = _s_turn_open(henv, monkeypatch)
    child.emit(owned(S, text="PINEAPPLE"))
    henv.host.tick()
    p = s.provider
    for i in range(claude.RETIRED_MAX + 1):
        p._written.add(f"5e7e7e7e-0000-4000-8000-{i:012d}")
        p._attempt = {"s": f"5e7e7e7e-0000-4000-8000-{i:012d}", "n": "x", "bytes": 1, "phase": "n"}
        p.steer_done()
    henv.host.send(SID, "M", message_id="2")
    henv.host.tick()
    m = uuid_of(child)
    child.emit(vinit(m), result(subtype="error_during_execution", is_error=True, text="S's old failure")
               | {"user_message_uuid": S, "user_message_uuids": [S]})
    assert not ends(henv.host.tick(), "2")
    child.emit(owned(m, text="M's own"))
    [m_end] = ends(henv.host.tick(), "2")
    assert m_end.ok and m_end.text == "M's own"


def test_r3_l_an_echo_list_past_the_cap_is_never_read_in_part(henv, monkeypatch):
    """Sol's blocker: an echo list longer than MAX_ECHO hid an entry past it. Never read in part: a contradiction
    (the loss), never S's end with that frame's text."""
    s, child, n = _s_turn_open(henv, monkeypatch)
    junk = [f"0be7c0de-0000-4000-8000-{i:012d}" for i in range(claude.MAX_ECHO + 1)]
    child.emit(result(text="hidden") | {"user_message_uuid": junk[0], "user_message_uuids": junk + [S]})
    out = henv.host.tick() + henv.host.tick()
    [end] = ends(out)
    assert end.steer["outcome"] == "next_turn" and end.steer["result"]["ok"] is False
    assert end.steer["result"]["text"] != "hidden" and not s.provider.verified


def test_r3_l_an_init_naming_neither_inside_ss_bracket_is_no_evidence_and_no_loss(henv, monkeypatch):
    """Ruling L (reading 3 changed): an unmarked init inside S's own bracket (a compaction's, a notification's) names
    neither S nor N: no evidence and no loss; S still ends with its own measured result."""
    s, child, n = _s_turn_open(henv, monkeypatch)
    child.emit({"type": "system", "subtype": "compact_boundary", "session_id": SID}, vinit())
    assert henv.host.tick() == [] and s.provider.verified and s.held.s_turn
    child.emit(owned(S, text="PINEAPPLE"))
    [end] = ends(henv.host.tick())
    assert end.steer["outcome"] == "next_turn" and end.steer["result"]["text"] == "PINEAPPLE"


@pytest.mark.parametrize("extra", [{"origin": None}, {"subtype": "error_unmeasured", "is_error": True},
                                   {"num_turns": 0}, {"is_error": True}],
                         ids=["origin_null", "subtype", "zero_turns", "is_error_with_success"])
def test_r3_l_ss_end_needs_the_measured_terminal(henv, monkeypatch, extra):
    """Ruling L: S's end needs the measured terminal: no `origin` key (an explicit null was never measured), a
    measured subtype with its `is_error`, one turn or more. Otherwise it names S unmeasured: the loss."""
    s, child, n = _s_turn_open(henv, monkeypatch)
    child.emit(owned(S, text="not proved") | extra)
    out = henv.host.tick() + henv.host.tick()
    [end] = ends(out)
    assert end.steer["outcome"] == "next_turn" and end.steer["result"]["ok"] is False
    assert end.steer["result"]["text"] != "not proved"


def test_r3_l_the_three_outcome_origin_rule_is_its_own_brackets(henv, monkeypatch):
    """Ruling L: in the three-outcome mode, an [S] init's bracket (whose consumption needs no origin) ends with a
    foreign terminal; the NEXT bracket the CLI opens is an ordinary one: its [S] terminal with the measured
    notification origin is consumption."""
    monkeypatch.setattr(claude, "STEER_MODES", {V: claude.THREE})
    s, child, n = steered(henv, monkeypatch)
    child.emit(owned(n, text="N"))
    henv.host.tick()
    child.emit(vinit(S), foreign())
    assert henv.host.tick() == [] and s.held is not None
    child.emit(vinit(), owned(S, text="took S") | {"origin": NOTIFY})
    [end] = ends(henv.host.tick())
    assert end.steer == {"outcome": "unconfirmed", "why": "consumed"}


def test_r3_l_a_frame_naming_the_current_n_and_a_retired_s_is_the_live_attempts(henv, monkeypatch):
    """Ruling L (reading 2): a frame that names a retired S and the CURRENT N is classified against the live attempt
    first: N2's identity intact, N2's own result (S2 `unconfirmed`), never swallowed as the retired S's."""
    s, child, n = steered(henv, monkeypatch)
    child.emit(owned(n, S, text="N1"))
    henv.host.tick()
    henv.host.send(SID, "second", message_id="2")
    henv.host.tick()
    n2 = uuid_of(child)
    child.emit(vinit(n2))
    henv.host.tick()
    s2 = "5e7e7e7e-0000-4000-8000-0000000000c2"
    henv.host.steer_offer(SID, "2", s2, TEXT, authorized=True)
    henv.host.tick()
    child.emit(owned(n2, S, text="N2's own"))
    [end] = ends(henv.host.tick(), "2")
    assert end.text == "N2's own" and end.steer == {"outcome": "unconfirmed", "why": "unproved"}



# ---------------------------------------------------------------- review round 3, rulings M, N, O, P; step 2 parity


def _tick_raising_after(host):
    """The host's next tick publishes, then raises (a fault after publication)."""
    real = host._tick

    def bad(now, out):
        real(now, out)
        raise RuntimeError("a fault after publication")
    host._tick = bad
    return lambda: setattr(host, "_tick", real)


def test_r3_m_a_recovered_checkpoint_goes_through_the_one_dispatcher(env):
    """Sol's blocker (ruling M): N held (its checkpoint published), the tick raises; the watchdog takes the backlog;
    the next pass delivers it through the one dispatcher: the checkpoint to the sender, never `kind: held` in an
    ordinary report (a step-3 hub refuses such a batch)."""
    sid, child, n, s_uuid = claude_steered(env)
    runner = env.node.sessions
    child.emit(cresult(sid, n, text="N's reply"))
    restore = _tick_raising_after(runner.host)
    with pytest.raises(RuntimeError):
        runner.pass_once()
    restore()
    runner.enforce_deadlines()          # the watchdog: whatever it takes, it delivers nothing itself
    tick(env, times=2)
    m = hosted._get(env.board.conn, hosted._msg_key(sid, 1))
    assert m["held"]["text"] == "N's reply", m
    assert all(e.get("kind") != "held" for _, e in runner.pending)


def test_r3_m_the_outbox_is_drained_before_reconciliation_can_invent_lost(env):
    """Sol's blocker (ruling M): N's native end (`in_turn`) published, then the tick raises; the next pass reconciles
    a listing first: the native end, retained in the host's outbox, is dispatched before any LOST is made up."""
    sid, child, n, s_uuid = claude_steered(env)
    runner = env.node.sessions
    child.emit(cresult(sid, n, s_uuid, text="N and S"))
    restore = _tick_raising_after(runner.host)
    with pytest.raises(RuntimeError):
        runner.pass_once()
    restore()
    tick(env, times=2)
    m = hosted._get(env.board.conn, hosted._msg_key(sid, 1))
    st = hosted_steer.get(env.board.conn, sid, 1)
    assert m["state"] == "done" and not str(m.get("detail")).startswith(node_sessions.LOST), m
    assert (st["outcome"], st["why"]) == ("in_turn", "answered")


def test_r3_m_ended_is_published_even_when_the_stop_raises(henv, monkeypatch):
    """Ruling M: a close whose SIGTERM raises still publishes `ended` (and N's end) into the outbox; the next host
    call delivers them."""
    s, child, n = steered(henv, monkeypatch)
    child.emit(owned(n, S, text="N"))
    henv.host.tick()

    def bad_term():
        raise OSError("terminate failed")
    child.terminate = bad_term
    with pytest.raises(OSError):
        henv.host.close(SID, "closed")
    child.terminate = lambda: None
    kinds = [e.kind for e in henv.host.tick()]
    assert "ended" in kinds


def test_r3_m_a_raising_log_handler_loses_no_held_end(henv, monkeypatch):
    """Ruling M: `_emit_hold` puts N's end into the outbox before it logs: a log handler that raises loses nothing."""
    from agent_comms import session_host
    s, child, n = _s_turn_open(henv, monkeypatch)
    real = session_host.log.info

    def bad(msg, *a, **k):
        if "the steer into message" in msg:
            raise RuntimeError("the log handler failed")
        return real(msg, *a, **k)
    monkeypatch.setattr(session_host.log, "info", bad)
    child.emit(owned(S, text="PINEAPPLE"))
    [end] = ends(henv.host.tick())
    assert end.steer["outcome"] == "next_turn"


def test_r3_m_shutdown_delivers_what_stop_all_decided_before_it_raised(env):
    """Ruling M: shutdown's `stop_all` raises part way (here its final save): what it had decided is in the host's
    outbox, and shutdown delivers it all the same."""
    sid = start(env, "an ordinary turn")["session_id"]
    tick(env)
    child = proc_of(env, sid)[1]
    child.emit(cinit(sid, written(child)[-1]["uuid"]))
    tick(env)
    runner = env.node.sessions

    def bad_save():
        raise OSError("the disk is full")
    runner.host._save_state = bad_save
    runner.signal()
    runner.kill()
    runner.shutdown()
    m = hosted._get(env.board.conn, hosted._msg_key(sid, 1))
    assert m["state"] == "failed" and "shut down" in m["detail"], m


def test_r3_m_an_old_event_never_reaches_a_reopened_session(henv, monkeypatch):
    """Ruling M: entries carry the session's incarnation: one published for a session that is then forgotten and
    opened again under the same id is never delivered to the new one."""
    from agent_comms.session_host import HostEvent
    s = open_session(henv)
    henv.host._outbox.append(HostEvent(SID, "turn_ended", "9", ok=True, text="old"))
    s.ended = True
    henv.host.forget(SID)
    open_session(henv)
    henv.host._outbox.append(HostEvent(SID, "delivered", "1"))
    assert [(e.kind, e.message_id) for e in henv.host.take_outbox()] == [("delivered", "1")]


def test_r3_n_the_held_timeout_after_the_latch_starts_no_stop(henv, monkeypatch):
    """Sol's blocker (ruling N): N held, the latch set, then the hold's bound inside the next tick: no single-session
    stop (no SIGTERM); the stop of every turn publishes N's end once."""
    s, child, n = steered(henv, monkeypatch)
    child.emit(owned(n, text="N"))
    henv.host.tick()
    henv.host.steer_stopping()
    henv.clock.advance(61)
    out = henv.host.tick()
    assert not child.terminated and not ends(out) and s.stopping_since is None
    [end] = ends(henv.host.stop_all())
    assert end.text == "N" and end.steer == {"outcome": "unconfirmed", "why": "turn_cut"}


def test_r3_n_after_the_latch_the_cut_changes_no_state(henv, monkeypatch):
    """Ruling N: the first latch check is what keeps an expired hold's state as it is after the latch: no contact
    latched (`expired`), no drain deadline reserved."""
    s, child, n = steered(henv, monkeypatch)
    s.steer.cover_until = henv.clock() + 20
    child.emit(owned(n, text="N"))
    henv.host.tick()
    henv.host.steer_stopping()
    henv.clock.advance(21)
    henv.host.steer_deadlines()
    assert not s.steer.expired and henv.host._drain_deadline is None and not child.terminated


@pytest.mark.parametrize("stop", ["stop_turns", "stop_all"])
def test_r3_n_a_bulk_stop_survives_one_sessions_fault(henv, monkeypatch, stop):
    """Ruling N: one session's SIGTERM raising skips neither the other sessions nor the deferred save."""
    pair = _held_pair(henv, monkeypatch)
    (s1, c1), (s2, c2) = pair

    def bad():
        raise OSError("terminate failed")
    c1.terminate = bad
    saved = []
    real_save = henv.host._save_state
    henv.host._save_state = lambda: (saved.append(1), real_save())
    out = henv.host.stop_turns("lost the hub") if stop == "stop_turns" else henv.host.stop_all()
    assert c2.terminated and saved
    assert {e.session_id for e in out if e.kind == "turn_ended"} == {SID, SID2}


def test_r3_n_the_watchdog_publishes_a_due_fence_with_no_loop_tick(henv, monkeypatch):
    """Ruling N: a fenced hold whose deadline passed is published by the watchdog's boundary (`steer_deadlines`),
    into the outbox, with no loop tick."""
    s, child, n = steered(henv, monkeypatch, grace=10)
    child.exits_on_terminate = False
    child.emit(owned(n, text="N"))
    henv.host.tick()
    henv.groups.add(child.pid)
    henv.host.close(SID, "closed")
    henv.clock.advance(21)
    out = henv.host.steer_deadlines()
    assert [e.text for e in ends(out)] == ["N"] and "ended" in [e.kind for e in out]


def test_r3_n_the_sigterm_is_sent_outside_the_cutoff_lock(henv, monkeypatch):
    """Ruling N, superseded for the reserved stop by the enabling PR's ruling S2 (the name kept for the record): the
    reservation, then escalation armed FIRST (the SIGTERM) under the cutoff lock, then the decision and the fence;
    nothing can fail between the reservation and the signal."""
    import threading
    s, child, n = steered(henv, monkeypatch)
    s.steer.cover_until = henv.clock() + 20
    child.emit(owned(n, text="N"))
    henv.host.tick()
    seen = []

    def probe(got):
        ok = henv.host._steer_stop_lock.acquire(blocking=False)
        if ok:
            henv.host._steer_stop_lock.release()
        got.append(ok)
    real_term = child.terminate

    def term():
        got = []
        t = threading.Thread(target=probe, args=(got,))
        t.start()
        t.join()
        seen.append(got[0])
        return real_term()
    child.terminate = term
    henv.clock.advance(21)
    henv.host.steer_deadlines()
    assert seen == [False], "the SIGTERM is sent inside the reserved stop's critical section (S2)"
    assert s.held is not None and s.held.decided is not None, "and the decision after it"


def _old_hub_validator(monkeypatch):
    """main d11ff99's `modes` check in hosted.report, verbatim (the step-2 hub a rollback goes back to)."""
    from agent_comms.core import Invalid
    real = hosted.report

    def report(board, p, session_id, events=None, state=None, pid=None, resume=None, seq=None, provider_id=None,
               access=None, worktree=None, ceiling=None, checkout=None, modes=None, held=None):
        if modes is not None and (not isinstance(modes, list) or len(modes) > 2
                                  or any(x not in ("steer", "restart") for x in modes) or len(set(modes)) != len(modes)):
            raise Invalid("modes must list what the session can do with a turn in flight: steer, restart")
        return real(board, p, session_id, events, state, pid, resume, seq, provider_id=provider_id, access=access,
                    worktree=worktree, ceiling=ceiling, checkout=checkout, modes=modes, held=held)
    monkeypatch.setattr(hosted, "report", report)


def test_r3_o_a_native_end_survives_a_rollback_hubs_refusal_of_held(env, monkeypatch):
    """Sol's blocker (ruling O): the node still holds the last step-3 capabilities; the hub, rolled back, refuses
    `held` in `modes` (400, main d11ff99's validator): the node drops the pair and sends the report again with the
    base modes: the ordinary Claude turn's native end is recorded, never discarded."""
    sid = start(env, "an ordinary turn")["session_id"]
    tick(env)
    child = proc_of(env, sid)[1]
    m_uuid = written(child)[-1]["uuid"]
    child.emit(cinit(sid, m_uuid))
    tick(env)
    _old_hub_validator(monkeypatch)
    child.emit(cresult(sid, m_uuid, text="the reply"))
    tick(env, times=2)
    m = hosted._get(env.board.conn, hosted._msg_key(sid, 1))
    assert m["state"] == "done" and show(env, sid)["reply"] == "the reply", m


def test_r3_o_capabilities_go_as_a_pair(env):
    """Ruling O: after a refusal forgot `steer` alone, a Claude session's report never says `["restart", "held"]`
    (which the new hub refuses): `steer` and `held` go together or not at all."""
    runner = env.node.sessions
    assert runner._modes_for(("steer", "restart", "held"), {"steer", "held", "restart"}) == ["steer", "restart", "held"]
    assert runner._modes_for(("steer", "restart", "held"), {"held", "restart"}) == ["restart"]
    assert runner._modes_for(("steer", "restart", "held"), {"steer", "restart"}) == ["restart"]
    assert runner._modes_for(("steer", "restart"), {"steer", "restart"}) == ["steer", "restart"], "Codex: as before"


def test_r3_o_next_turn_only_with_ns_answered_end(env):
    """Sol's should (ruling O): a well-shaped `next_turn` on an N end that is not answered (no checkpoint either)
    is ignored: S settles by the rule (`unconfirmed`), never `next_turn`."""
    sid, child, n, s_uuid = claude_steered(env)
    ext = {"outcome": "next_turn", "why": "own_turn", "result": {"ok": True, "detail": "", "denials": [], "meta": {},
                                                                  "text": "S", "answered": True}}
    r = env.laptop.post(f"/api/nodes/sessions/{sid}", json={"events": [
        {"kind": "turn_ended", "message": 1, "ok": False, "detail": "given up", "answered": False, "steer": ext}]})
    assert r.status_code == 200, r.text
    st = hosted_steer.get(env.board.conn, sid, 1)
    assert st["outcome"] != "next_turn", st


def test_r3_p_the_checkpoint_sender_starts_before_the_driving_thread(env, monkeypatch):
    """Ruling P: the sender's thread exists before the loop's thread runs (no production window for the inline
    fallback)."""
    runner = env.node.sessions
    seen = []
    monkeypatch.setattr(runner, "run_forever", lambda: seen.append(runner.checkpoint_thread is not None))
    monkeypatch.setattr(runner, "_watch", lambda: None)
    runner.start()
    runner.thread.join(5)
    runner.stopping.set()
    runner.checkpoint_thread.join(5)
    assert seen == [True]


def test_r3_p_a_checkpoint_is_bound_to_its_incarnation_and_the_stop_flag(env):
    """Ruling P: rechecked right before the POST: never once shutdown began, never for another incarnation."""
    sid, child, n, s_uuid = claude_steered(env)
    child.emit(cresult(sid, n, text="N's reply"))
    runner = env.node.sessions
    runner.host.tick()
    held = {"message": 1, "ok": True, "detail": "", "denials": [], "meta": {}, "text": "x", "answered": True}
    s = runner.host.sessions[sid]
    assert runner._checkpoint_due(node_sessions.Checkpoint(sid, s.gen, 1, held))
    assert not runner._checkpoint_due(node_sessions.Checkpoint(sid, s.gen + 1, 1, held)), "another incarnation"
    runner.stopping.set()
    assert not runner._checkpoint_due(node_sessions.Checkpoint(sid, s.gen, 1, held)), "shutdown began"


def test_r3_parity_a_revoked_codex_attempt_keeps_mains_contact(henv):
    """Step 2 parity: a Codex attempt (its adapter holds nothing) whose writing an omission revoked takes no more
    acknowledgements on its contact clock, as on main; a Claude one (`holds`) still does (ruling G)."""
    from agent_comms.session_host import SteerSlot
    for holds, moves in ((False, False), (True, True)):
        slot = SteerSlot("u", "1", "t", session_id=SID, cover_until=henv.clock() + 20)
        slot.holds, slot.revoked = holds, "omitted"
        before = slot.cover_until
        slot.ack(henv.clock(), henv.clock, SID, 1)
        assert (slot.cover_until != before) is moves, holds


def test_r3_parity_an_ordinary_claude_turn_survives_a_fault_through_the_outbox(henv, monkeypatch):
    """Step 2 parity, ruling I's outbox for ordinary events: an ordinary (never steered) Claude turn's end published,
    the tick raising after it: the next tick delivers it, once."""
    monkeypatch.setattr(claude, "VERIFIED", frozenset({V}))
    henv.host = make_host(henv, timeout_minutes=1)
    s, child = idle_host(henv)
    henv.host.send(SID, "a turn", message_id="1")
    henv.host.tick()
    m = uuid_of(child)
    child.emit(vinit(m), owned(m, text="M"))
    restore = _tick_raising_after(henv.host)
    with pytest.raises(RuntimeError):
        henv.host.tick()
    restore()
    assert [e.text for e in ends(henv.host.tick())] == ["M"] and not ends(henv.host.tick())


def test_r3_parity_a_codex_turn_survives_a_fault_through_the_outbox(henv):
    """Step 2 parity, ruling I's outbox for Codex: a Codex turn's end published, the tick raising after it: the next
    tick delivers it, once."""
    from test_session_host import codex_host, codex_ready
    import test_providers_codex as cx
    host, s = codex_host(henv)
    host.send(SID, "a codex turn", message_id="1")
    host.tick()
    child = henv.spawner.children[-1]
    codex_ready(host, child)
    host.tick()
    start = [json.loads(x) for x in child.stdin.split(b"\n") if x and b'"turn/start"' in x][-1]
    child.emit(cx.reply(start["id"], cx.TURN_REPLY), cx.TURN_STARTED)
    host.tick()
    child.emit(cx.completed())
    restore = _tick_raising_after(host)
    with pytest.raises(RuntimeError):
        host.tick()
    restore()
    got = [e for e in host.tick() if e.kind == "turn_ended"]
    assert len(got) == 1 and not [e for e in host.tick() if e.kind == "turn_ended"]



# ---------------------------------------------------------------- review round 3: the mutation check's misses pinned


def test_r3_m_a_long_echo_list_naming_neither_is_still_a_contradiction(henv, monkeypatch):
    """Ruling L: an echo list longer than MAX_ECHO is a contradiction while an attempt is live, even one that names
    neither S nor N (it is never read in part)."""
    s, child, n = _s_turn_open(henv, monkeypatch)
    junk = [f"0be7c0de-0000-4000-8000-{i:012d}" for i in range(claude.MAX_ECHO + 1)]
    child.emit(result(text="long") | {"user_message_uuid": junk[0], "user_message_uuids": junk})
    henv.host.tick()
    assert not s.provider.verified


def test_r3_m_a_frame_naming_the_live_s_and_a_retired_s_is_the_live_attempts(henv, monkeypatch):
    """Ruling L (reading 2): a frame naming the live attempt's S and a retired S is classified against the live
    attempt first (here: S2 not as measured, the loss), never swallowed as the retired S's."""
    s, child, n = steered(henv, monkeypatch)
    child.emit(owned(n, S, text="N1"))
    henv.host.tick()
    henv.host.send(SID, "second", message_id="2")
    henv.host.tick()
    n2 = uuid_of(child)
    child.emit(vinit(n2))
    henv.host.tick()
    s2 = "5e7e7e7e-0000-4000-8000-0000000000c2"
    henv.host.steer_offer(SID, "2", s2, TEXT, authorized=True)
    henv.host.tick()
    child.emit(owned(s2, S, text="?"))
    henv.host.tick()
    assert not s.provider.verified


def test_r3_o_losing_held_resends_a_claude_sessions_modes(env):
    """Ruling O: a listing that no longer names `held` invalidates a Claude session's reported status (its modes
    change: the pair goes): its next report says the base modes."""
    sid = start(env, "an ordinary turn")["session_id"]
    tick(env)
    child = proc_of(env, sid)[1]
    child.emit(cinit(sid, written(child)[-1]["uuid"]))
    tick(env, times=2)
    assert "held" in hosted._session(env.board.conn, sid)["modes"]
    runner = env.node.sessions
    data = env.laptop.get("/api/nodes/sessions", params={"tree": 5}).json()
    data["features"] = [f for f in data["features"] if f != "held"]
    with runner.lock:
        runner._reconcile(data)
    assert sid not in runner.reported, "its status is said again"
    with runner.lock:
        runner._flush()
    assert hosted._session(env.board.conn, sid)["modes"] == ["restart"]



def test_r3_p_the_sender_drops_a_checkpoint_no_longer_due_at_the_post(env):
    """Ruling P: the sender's thread rechecks a queued checkpoint immediately before its POST: one whose N is no
    longer held (another message, here) is dropped, nothing posted."""
    sid, child, n, s_uuid = claude_steered(env)
    child.emit(cresult(sid, n, text="N's reply"))
    runner = env.node.sessions
    runner.host.tick()
    posted = []
    real = runner.client.request
    runner.client.request = lambda m, u, *a, **kw: (posted.append(kw.get("json")) if (kw.get("json") or {}).get(
        "held") is not None else None, real(m, u, *a, **kw))[1]
    runner.start_checkpoint_sender()
    try:
        with runner.checkpoint_cond:
            runner.checkpoint_queue[sid] = node_sessions.Checkpoint(sid, runner.host.sessions[sid].gen, 2, {
                "message": 2, "ok": True, "detail": "", "denials": [], "meta": {}, "text": "x", "answered": True})
            runner.checkpoint_cond.notify()
        import time as _time
        deadline = _time.monotonic() + 3
        while runner.checkpoint_queue and _time.monotonic() < deadline:
            _time.sleep(0.05)
        _time.sleep(0.2)
        assert not runner.checkpoint_queue and posted == [], posted
    finally:
        runner.stopping.set()
        runner.checkpoint_thread.join(5)



# ---------------------------------------------------------------- review round 4, ruling Q: the adapter is total

JSON_VALUES = [None, True, False, 0, 1, -1, 2.5, "", "x", "success", [], [1], ["success"], {}, {"kind": "x"}]


@pytest.mark.parametrize("subtype", [["success"], {"a": 1}])
def test_r4_q_an_identified_ns_result_with_an_unhashable_subtype_is_ns(henv, monkeypatch, subtype):
    """Sol's blocker (Q1, Q2): N's result with N's identity intact and an array- or object-valued `subtype`: never a
    raise; N's own result (S `unconfirmed`), the session goes on."""
    s, child, n = steered(henv, monkeypatch)
    child.emit(owned(n, text="N's own") | {"subtype": subtype})
    [end] = ends(henv.host.tick())
    assert end.text == "N's own" and end.steer["outcome"] == "unconfirmed" and not s.ended


@pytest.mark.parametrize("subtype", [["success"], {"a": 1}])
def test_r4_q_an_exact_s_result_with_an_unhashable_subtype_never_ends_the_session(henv, monkeypatch, subtype):
    """Sol's blocker (Q1): an exact [S] terminal with a malformed subtype inside S's own turn is not the measured
    shape: no host error, the session goes on; S is cut, never S's end with that frame."""
    s, child, n = _s_turn_open(henv, monkeypatch)
    child.emit(owned(S, text="malformed") | {"subtype": subtype})
    out = henv.host.tick() + henv.host.tick()
    [end] = ends(out)
    assert not s.ended and end.steer["outcome"] == "next_turn" and end.steer["result"]["text"] != "malformed"


def test_r4_q_ns_identity_wins_over_the_length_cap(henv, monkeypatch):
    """Q2: N's identity is checked before the length cap: scalar N and N first, with a list longer than MAX_ECHO,
    is N's own result (S `unconfirmed`)."""
    s, child, n = steered(henv, monkeypatch)
    junk = [f"0be7c0de-0000-4000-8000-{i:012d}" for i in range(claude.MAX_ECHO + 5)]
    child.emit(owned(n, text="N's own") | {"user_message_uuids": [n] + junk})
    [end] = ends(henv.host.tick())
    assert end.text == "N's own" and end.steer == {"outcome": "unconfirmed", "why": "unproved"}


def test_r4_q_retired_s_first_and_live_n_past_the_cap(henv, monkeypatch):
    """Q3: a list with a retired S first and the live N past the cap names the live attempt: classified against it
    (N without N's identity: the loss), never swallowed as the retired S's frame."""
    s, child, n = steered(henv, monkeypatch)
    child.emit(owned(n, S, text="N1"))
    henv.host.tick()
    henv.host.send(SID, "second", message_id="2")
    henv.host.tick()
    n2 = uuid_of(child)
    child.emit(vinit(n2))
    henv.host.tick()
    henv.host.steer_offer(SID, "2", "5e7e7e7e-0000-4000-8000-0000000000c2", TEXT, authorized=True)
    henv.host.tick()
    junk = [f"0be7c0de-0000-4000-8000-{i:012d}" for i in range(claude.MAX_ECHO + 2)]
    child.emit(result(text="?") | {"user_message_uuid": S, "user_message_uuids": [S] + junk + [n2]})
    henv.host.tick()
    assert not s.provider.verified


def test_r4_q_rule_10_only_while_ss_init_is_the_last_init(henv, monkeypatch):
    """Q4 (deviation b narrowed): `init` [S], then an unmarked init inside S's bracket, then an echo-less,
    origin-less error: no evidence for S (another init was seen); S runs to its bound (`next_turn` cut), the error
    never published as S's own."""
    s, child, n = _s_turn_open(henv, monkeypatch)
    child.emit(vinit(), plain(text="whose error", subtype="error_during_execution", is_error=True))
    out = henv.host.tick()
    assert not ends(out) and s.held is not None and s.held.s_turn
    henv.clock.advance(61)
    out = henv.host.tick()
    [end] = ends(out) or ends(henv.host.tick())
    assert end.steer["outcome"] == "next_turn" and end.steer["result"]["ok"] is False
    assert end.steer["result"]["text"] != "whose error"


# ---------------------------------------------------------------- review round 5


def test_r5_q7_a_retired_init_inside_ss_bracket_ends_rule_10(henv, monkeypatch):
    """Sol's blocker 4 (ruling Q7): S1 retired, S2's bracket open, a delayed `init` [S1] (the retired shortcut), then
    an echo-less, origin-less error: never S2's. Any init that is not S's own live init clears the marker, before
    any shortcut."""
    s, child, n = _s_turn_open(henv, monkeypatch)
    s1 = "5e7e7e7e-0000-4000-8000-0000000000a1"
    s.provider._retired.add(s1)
    s.provider._written.add(s1)
    child.emit(vinit(s1), plain(text="whose error", subtype="error_during_execution", is_error=True))
    out = henv.host.tick()
    assert not ends(out) and s.held is not None and s.held.s_turn
    henv.clock.advance(61)
    out = henv.host.tick()
    [end] = ends(out) or ends(henv.host.tick())
    assert end.steer["outcome"] == "next_turn" and end.steer["result"]["ok"] is False
    assert end.steer["result"]["text"] != "whose error"


class _Invalid(Exception):
    pass


def _d11ff99_report(path, body):
    """main d11ff99's report checks a step-2 hub applies to these reports, verbatim (hosted.py 1563-1565, the
    `modes` check: `Invalid`, a 400): an extension is never refused there (hosted_steer.extension ignores one it does
    not take, `next_turn` included)."""
    modes = body.get("modes")
    try:
        if modes is not None and (not isinstance(modes, list) or len(modes) > 2
                                  or any(x not in ("steer", "restart") for x in modes) or len(set(modes)) != len(modes)):
            raise _Invalid("modes must list what the session can do with a turn in flight: steer, restart")
    except _Invalid:
        return 400
    return None


def _claude_next_turn_and_codex_steer_pending(env):
    """As `_claude_end_and_codex_steer_pending`, but A's end carries an actual `next_turn` (S's own turn ran)."""
    from test_codex_steering import codex_turn_in_flight, post_steer
    from test_hosted_sessions import P3
    from agent_comms.session_host import HostEvent
    a = start(env, "an ordinary turn")["session_id"]
    tick(env)
    ca = proc_of(env, a)[1]
    ma = written(ca)[-1]["uuid"]
    ca.emit(cinit(a, ma))
    tick(env)
    b, cb = codex_turn_in_flight(env, project=P3)
    post_steer(env, b)
    tick(env)
    runner = env.node.sessions
    with runner.lock:
        runner._emit([HostEvent(a, "turn_ended", "1", ok=True, text="A's reply", answered=True,
                                steer={"outcome": "next_turn", "why": "own_turn", "result": {
                                    "ok": True, "detail": "", "denials": [], "meta": {}, "text": "S",
                                    "answered": True}}),
                      HostEvent(b, "turn_ended", "1", ok=True, text="B's reply", answered=True,
                                steer={"outcome": "in_turn", "why": "answered"})])
    return a, b, runner


def test_r5_y_a_next_turn_fallback_never_strips_a_codex_outcome(env):
    """Sol's blocker 1 (ruling Y): A carries an actual `next_turn`; a step-2 hub (main d11ff99's validator) refuses
    A's report (400) and takes it at rung (ii); B's valid Codex `in_turn` in the same flush goes WITH its extension
    and settles `in_turn`; no feature is forgotten, so B's next end keeps its extension too."""
    from agent_comms.session_host import HostEvent
    a, b, runner = _claude_next_turn_and_codex_steer_pending(env)
    features = set(runner.hub_features)
    seen = _posts(runner, _d11ff99_report)
    with runner.lock:
        assert runner._flush()
    assert hosted._get(env.board.conn, hosted._msg_key(a, 1))["state"] == "done"
    st = hosted_steer.get(env.board.conn, b, 1)
    assert (st["outcome"], st["why"]) == ("in_turn", "answered"), st
    assert any("steer" in e for e in dict(seen)[b]["events"])
    assert runner.hub_features == features, "a rung (ii) success changes no feature knowledge"
    nxt = runner._for_hub(runner._event(HostEvent(b, "turn_ended", "2", ok=True, steer={"outcome": "in_turn",
                                                                                          "why": "answered"})))
    assert "steer" in nxt, "the next flush's Codex outcome keeps its extension"


def test_r5_y_a_422_of_more_than_the_extensions_forgets_nothing(env):
    """Ruling Y+ (a mutation row): a hub refuses A's report with 422 for its modes (the pair), and takes rung (ii),
    which also lost A's `next_turn`: that 422 was not about the extension alone, so `steer` is not forgotten."""
    a, b, runner = _claude_next_turn_and_codex_steer_pending(env)
    _posts(runner, lambda path, body: 422 if "held" in (body.get("modes") or []) else None)
    with runner.lock:
        assert runner._flush()
    assert "steer" in runner.hub_features, "the 422 was about the modes: nothing forgotten"


def test_r5_y_the_acknowledged_modes_are_recorded_and_corrected_explicitly(env):
    """Ruling Y1, Y+: what the node records as A's reported modes is what the accepted rung sent; after a refetch
    with the same features (the hub takes the pair again), A, quiet, is reported again for that purpose with its
    full modes: the hub's record is not left stale."""
    a, b, runner = _claude_next_turn_and_codex_steer_pending(env)
    orig = runner.client
    _posts(runner, _d11ff99_report)
    with runner.lock:
        assert runner._flush()
    assert hosted._session(env.board.conn, a)["modes"] == ["restart"]
    assert runner.acked_modes[a] == ["restart"]
    runner.client = orig
    seen2 = _posts(runner, lambda path, body: None)     # the hub takes the pair again
    tick(env, times=2)
    assert any(x == a and "held" in (bd.get("modes") or []) for x, bd in seen2), "an explicit correction"
    assert "held" in hosted._session(env.board.conn, a)["modes"]


@pytest.mark.parametrize("code", [422, 400])
def test_r5_y_main_forgets_steer_only_on_its_own_legacy_path(env, code):
    """Ruling Y+ (Codex only): a step-2 hub that refuses a Codex report's steer extension with 422 takes it plainly:
    `steer` is forgotten, as on main e6b421f; refused with 400 instead (another field), nothing is forgotten."""
    from test_codex_steering import codex_turn_in_flight, post_steer
    from agent_comms.session_host import HostEvent
    b, cb = codex_turn_in_flight(env)
    post_steer(env, b)
    tick(env)
    runner = env.node.sessions
    end = HostEvent(b, "turn_ended", "1", ok=True, text="B's reply", answered=True,
                    steer={"outcome": "in_turn", "why": "answered"})
    with runner.lock:
        runner._emit([end])
    _posts(runner, lambda path, body: code if any("steer" in e for e in body["events"]) else None)
    with runner.lock:
        assert runner._flush()
    if code == 422:
        assert "steer" not in runner.hub_features, "main's own path: the plain retry forgets `steer`"
    else:
        assert "steer" in runner.hub_features, "a 400 is not main's legacy rejection: nothing is forgotten"


def _on_watchdog(runner, fn=None):
    """Run the runner's watchdog pass (or `fn`) on a thread registered as the watchdog's, as `_watch` registers its
    own: (thread, done, [ident], [error])."""
    import threading
    from agent_comms import session_host
    done, ident, err = threading.Event(), [], []

    def run():
        ident.append(threading.get_ident())
        reg = getattr(session_host, "WATCH_THREADS", None)
        if reg is not None:
            reg.add(ident[0])
        try:
            (fn or runner.enforce_deadlines)()
        except BaseException as e:      # noqa: BLE001 (the thread's own; the test reads it)
            err.append(e)
        finally:
            if reg is not None:
                reg.discard(ident[0])
            done.set()
    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t, done, ident, err


def _forbid(host, children, calls):
    """Record (name, thread) for every call that reads a pipe, drains, writes a transcript or saves state."""
    import threading
    for name in ("_read", "_drain_before_stop", "_save_state", "_tee"):
        real = getattr(host, name)

        def wrap(*a, _r=real, _n=name, **k):
            calls.append((_n, threading.get_ident()))
            return _r(*a, **k)
        setattr(host, name, wrap)
    for c in children:
        real = c.read_lines

        def read(_r=real):
            calls.append(("read_lines", threading.get_ident()))
            return _r()
        c.read_lines = read


def _claude_partial_with_n_buffered(env):
    """A Claude session: N in flight, S taken in part by the pipe past its limit, N's own result buffered unread."""
    from agent_comms.session_host import STEER_PARTIAL_SECONDS
    sid = start(env, "a long turn")["session_id"]
    tick(env)
    child = proc_of(env, sid)[1]
    n = written(child)[-1]["uuid"]
    child.emit(cinit(sid, n))
    tick(env)
    child.accept = 10
    r = env.human.post(f"/api/hosted-sessions/{sid}/messages", json={"message": TEXT, "mode": "steer"})
    assert r.status_code == 200, r.text
    tick(env, times=2)
    s = env.node.sessions.host.sessions[sid]
    assert s.steer is not None and 0 < s.steer.sent < s.steer.length, "the pipe took part of S"
    child.emit(cresult(sid, n, text="N's own"))
    env.clock.advance(STEER_PARTIAL_SECONDS + 1)
    return sid, child, n, s


@pytest.mark.parametrize("what", ["partial", "hold", "self_stop", "self_stop_raising"])
def test_r5_w_the_watchdog_thread_makes_no_forbidden_call(env, what):
    """Sol's blocker 2 (ruling W): on the watchdog's thread nothing reads a pipe, drains, writes a transcript or
    saves: a Claude cut of a frame taken in part with N's result buffered; a Claude hold whose contact ran out with
    S's frames buffered; the self-stop; the self-stop's exceptional exit. What needs those waits for the loop."""
    runner = env.node.sessions
    host = runner.host
    if what == "partial":
        sid, child, n, s = _claude_partial_with_n_buffered(env)
    elif what == "hold":
        sid, child, n, s_uuid = claude_steered(env)
        child.emit(cresult(sid, n, text="N's reply"))
        tick(env)
        s = host.sessions[sid]
        assert s.held is not None
        child.emit(cinit(sid, s_uuid))
        s.steer.cover_until = env.clock() - 1
    else:
        sid, child, m = _ordinary_turn_in_flight(env)
        if what == "self_stop_raising":
            from test_hosted_sessions import P2
            sid2, c2, m2 = _ordinary_turn_in_flight(env, project=P2)
        child.emit(cresult(sid, m, text="buffered"))
        env.clock.advance(runner.self_stop_seconds + 1)
        if what == "self_stop_raising":
            real, calls_ = host._stop, []

            class Abort(BaseException):
                pass

            def stop(sess, now, **kw):
                calls_.append(1)
                if len(calls_) == 2:
                    raise Abort()
                return real(sess, now, **kw)
            host._stop = stop
    calls = []
    _forbid(host, [c for c in env.node.session_spawner.children], calls)
    t, done, ident, err = _on_watchdog(runner)
    assert done.wait(5)
    bad = [name for name, th in calls if th == ident[0]]
    assert not bad, f"on the watchdog's thread: {bad}"


def test_r5_w_a_codex_cut_on_the_watchdog_writes_nothing_to_disk(env, monkeypatch, tmp_path):
    """Ruling W (a mutation row; the Codex reading): main's watchdog cut of a Codex frame taken in part still reads
    the pipe, with N's native end in it; nothing it reaches writes the state file or a transcript on that thread
    (the loop does, at its next tick)."""
    import threading
    from test_codex_steering import codex_turn_in_flight, post_steer
    from agent_comms import session_host
    from agent_comms.session_host import STEER_PARTIAL_SECONDS
    from agent_comms.providers import codex as cx_mod  # noqa: F401
    import test_codex_steering as tcs
    sid, child = codex_turn_in_flight(env)
    child.accept = 10
    post_steer(env, sid)
    tick(env)
    s = env.node.sessions.host.sessions[sid]
    [req] = [json.loads(x) for x in bytes(s.steer.frame).splitlines()]
    child.emit(tcs.cx.reply(req["id"], {"turnId": tcs.cx.TURN}), tcs.cx.message("native"), tcs.cx.completed())
    env.clock.advance(STEER_PARTIAL_SECONDS)
    import os as _os
    s.transcript_fd = _os.open(tmp_path / "transcript", _os.O_WRONLY | _os.O_CREAT, 0o600)   # (a transcript kept)
    writes = []
    real_write, real_replace = session_host.os.write, session_host.os.replace

    def write(fd, data):
        writes.append(("write", threading.get_ident()))
        return real_write(fd, data)

    def replace(a, b):
        writes.append(("replace", threading.get_ident()))
        return real_replace(a, b)
    monkeypatch.setattr(session_host.os, "write", write)
    monkeypatch.setattr(session_host.os, "replace", replace)
    t, done, ident, err = _on_watchdog(env.node.sessions)
    assert done.wait(5) and not err, err
    monkeypatch.undo()
    assert s.stopping_since is not None, "main's cut, on the watchdog"
    assert not [w for w, th in writes if th == ident[0]], "nothing written to disk on the watchdog's thread"
    tick(env)
    assert show(env, sid)["reply"] == "native"
    assert b"native" in (tmp_path / "transcript").read_bytes(), "the loop wrote what the watchdog's thread read"


def test_r5_w_no_log_handler_runs_on_the_watchdogs_thread(env):
    """Ruling W3 (a mutation row): a log handler that raises: the watchdog's pass logs (a self-stop past its
    deadline) and no handler runs on its thread; the loop's next pass writes the line."""
    import logging
    import threading
    sid, child, m = _ordinary_turn_in_flight(env)
    runner = env.node.sessions
    env.clock.advance(runner.self_stop_seconds + runner.watch_seconds + node_sessions.LOCK_SLACK_SECONDS + 5)
    seen = []

    class Raising(logging.Handler):
        def emit(self, record):
            seen.append((record.getMessage(), threading.get_ident()))
            if "after its deadline" in record.getMessage():
                raise RuntimeError("a handler failed")
    h = Raising()
    logger = logging.getLogger("agent_comms.node_sessions")
    logger.addHandler(h)
    try:
        t, done, ident, err = _on_watchdog(runner)
        assert done.wait(5) and not err, err
        assert not [x for x, th in seen if th == ident[0]], "no handler on the watchdog's thread"
        assert any(e.session_id == sid for e in runner.watched), "the self-stop happened"
        try:
            runner.pass_once()
        except RuntimeError:
            pass
        assert any("after its deadline" in x for x, th in seen), "the loop wrote the watchdog's line"
    finally:
        logger.removeHandler(h)


def test_r5_w_kills_and_fences_with_a_drain_pending_and_a_raising_or_stalling_save(env):
    """Ruling W5: a Claude cut pending with N's result buffered, a session due for its SIGKILL, and a save that
    always raises, then one that stalls: the watchdog's pass returns promptly and sends the due SIGKILL."""
    import threading
    from test_hosted_sessions import P2
    runner = env.node.sessions
    host = runner.host
    sid, child, n, s = _claude_partial_with_n_buffered(env)
    sid2, c2, m2 = _ordinary_turn_in_flight(env, project=P2)
    s2 = host.sessions[sid2]
    c2.exits_on_terminate = False
    with runner.lock:
        host._stop(s2, env.clock())
    env.clock.advance(host.kill_grace + 1)
    stall = threading.Event()
    for save in (lambda: (_ for _ in ()).throw(OSError("the disk is full")), lambda: stall.wait(10)):
        host._save_state = save
        c2.killed = getattr(c2, "killed", False)
        s2.killed = False
        # (C1, review round 2, R5: the first KILL attempt makes a token proof-only; this test's own reset, to send a
        # second due KILL with the other save, clears that too)
        host.groups.held.discard(s2.group_token)
        t, done, ident, err = _on_watchdog(runner)
        assert done.wait(3), "the watchdog's pass returned"
        assert s2.killed, "the due SIGKILL was sent"
    stall.set()


def test_r6_n1_the_kill_step_waits_for_the_loops_lock_as_main(env):
    """Ruling N1 (round 6; round 5's lock-free kill withdrawn): the runner lock is held elsewhere (a stalled loop):
    the due SIGKILL waits for it, with main's unbounded wait, and goes as soon as it is let go (main's; the loop
    holds the lock only for local work: the documented residual)."""
    import threading
    runner = env.node.sessions
    host = runner.host
    sid, child, m = _ordinary_turn_in_flight(env)
    s = host.sessions[sid]
    child.exits_on_terminate = False
    with runner.lock:
        host._stop(s, env.clock())
    env.clock.advance(host.kill_grace + 1)
    release, held = threading.Event(), threading.Event()

    def stalled_loop():
        with runner.lock, host._steer_stop_lock:
            held.set()
            release.wait(10)
    threading.Thread(target=stalled_loop, daemon=True).start()
    assert held.wait(3)
    t, done, ident, err = _on_watchdog(runner)
    import time as _time
    try:
        _time.sleep(runner.watch_seconds + 0.5)
        assert not s.killed and not done.is_set(), "it waits for the lock, never skips the kill"
    finally:
        release.set()
    assert done.wait(5) and s.killed, "and kills as soon as the lock is let go"


def test_r5_w_a_stalled_loop_with_a_partial_write_pending(env):
    """Ruling W+ and W6 (the residual, literally; round 6, N1: the kill step is main's): the loop holds the runner
    lock in a stalled save while a Claude frame taken in part is past its limit: no further byte is written; the
    watchdog's due SIGKILL waits for the lock (main's) and goes as soon as the loop lets go; the loop's next pass
    cuts the frame (nothing more of it) and reports N's own end."""
    import threading
    import time as _time
    from test_hosted_sessions import P2
    runner = env.node.sessions
    host = runner.host
    sid, child, n, s = _claude_partial_with_n_buffered(env)
    sid2, c2, m2 = _ordinary_turn_in_flight(env, project=P2)
    s2 = host.sessions[sid2]
    c2.exits_on_terminate = False
    with runner.lock:
        host._stop(s2, env.clock())
    env.clock.advance(host.kill_grace + 1)
    before = len(child.stdin)
    child.accept = None
    release, held = threading.Event(), threading.Event()

    def stalled_loop():
        with runner.lock:
            held.set()
            release.wait(10)        # (a save stalled inside the loop's own tick)
    threading.Thread(target=stalled_loop, daemon=True).start()
    assert held.wait(3)
    t, done, ident, err = _on_watchdog(runner)
    _time.sleep(runner.watch_seconds + 0.5)
    assert not s2.killed, "pinned (main's): the due SIGKILL waits for the loop's lock"
    assert len(child.stdin) == before, "no further byte of S while the loop is stalled"
    release.set()
    assert done.wait(5) and s2.killed, "the SIGKILL goes as soon as the loop lets go"
    tick(env)
    assert len(child.stdin) == before, "the loop cuts: no further byte of S"
    assert s.stopping_since is not None or s.child is None
    tick(env, times=2)
    assert show(env, sid)["reply"] == "N's own"


def test_r5_x_a_raise_after_the_consumer_committed_duplicates_nothing(env):
    """Sol's V1 (ruling X): the report queue takes A's end and the handoff raises after that: one pending copy."""
    sid, child, m = _ordinary_turn_in_flight(env)
    runner = env.node.sessions
    child.emit(cresult(sid, m, text="A's own"))
    real, calls = runner._add, []

    def add(*a, **k):
        r = real(*a, **k)
        if not calls:
            calls.append(1)
            raise RuntimeError("after the commit")
        return r
    runner._add = add
    runner._flush = lambda final=False: True
    with pytest.raises(RuntimeError):
        runner.pass_once()
    runner._add = real
    runner.pass_once()
    ends_ = [e for x, e in runner.pending if x == sid and e.get("kind") == "turn_ended"]
    assert len(ends_) == 1, ends_


def test_r5_x_both_sides_forget_once_the_source_removed_it(env):
    """Ruling X+ (a mutation row): once the source removed a taken event, the receipt goes too: after a handoff
    that completed, no receipt is left (and after a failed one, exactly the event's)."""
    sid, child, m = _ordinary_turn_in_flight(env)
    runner = env.node.sessions
    child.emit(cresult(sid, m, text="A's own"))
    real, calls = runner.host.ack, []

    def ack(e):
        if not calls:
            calls.append(1)
            raise RuntimeError("the source failed to let it go")
        return real(e)
    runner.host.ack = ack
    runner._flush = lambda final=False: True
    with pytest.raises(RuntimeError):
        runner.pass_once()
    assert len(runner.receipts) == 1, "taken, not yet removed: its receipt is held"
    runner.host.ack = real
    runner.pass_once()
    assert not runner.receipts, "removed: both sides forgot it"
    assert len([e for x, e in runner.pending if x == sid and e.get("kind") == "turn_ended"]) == 1


def test_r5_x_the_handoff_keeps_publication_order_across_lanes(env):
    """Ruling X+ (a mutation row): an event the watchdog published first (in `watched_steer`) goes before one the
    host published after it (in its outbox), whatever the lanes: the handoff takes the earliest published."""
    from agent_comms.session_host import HostEvent
    from test_hosted_sessions import P2
    a, ca, ma = _ordinary_turn_in_flight(env)
    b, cb, mb = _ordinary_turn_in_flight(env, project=P2)
    runner = env.node.sessions
    host = runner.host
    first = HostEvent(b, "turn_ended", "1", ok=False, detail="the watchdog's, first", ident=(b, host.sessions[b].gen, 0))
    runner.watched_steer.append(first)
    ca.emit(cresult(a, ma, text="the host's, after"))
    host._tick(env.clock(), host._outbox)
    runner._flush = lambda final=False: True
    with runner.lock:
        runner._deliver(late=False)
    order = [e.get("detail") or e.get("text") for x, e in runner.pending if e.get("kind") == "turn_ended"]
    assert order == ["the watchdog's, first", "the host's, after"], order


def test_r5_x_a_self_stop_that_raises_puts_what_it_published_into_watched(env):
    """Ruling X3 (a mutation row): the watchdog's self-stop publishes one failure, then raises on the next session:
    what it published is in `watched` (its lane), not lost and not in the outbox."""
    from test_hosted_sessions import P2
    sid1, c1, m1 = _ordinary_turn_in_flight(env)
    sid2, c2, m2 = _ordinary_turn_in_flight(env, project=P2)
    runner = env.node.sessions
    host = runner.host
    real, calls = host._stop, []

    class Abort(BaseException):
        pass

    def stop(sess, now, **kw):
        calls.append(sess.session_id)
        if len(calls) == 2:
            raise Abort()
        return real(sess, now, **kw)
    host._stop = stop
    with pytest.raises(Abort):
        host.stop_turns("the node lost the hub", own=True, into=runner.watched)
    host._stop = real
    assert [(e.session_id, e.kind) for e in runner.watched] == [(c, "turn_ended") for c in calls], "all it published"
    assert not [e for e in host._outbox if e.kind == "turn_ended"], "none of it in the outbox"


def test_r5_x_a_raise_between_the_self_stop_and_watched_loses_nothing(env, monkeypatch):
    """Sol's V1 (ruling X3): the watchdog's warning (a late self-stop) raises between the self-stop and `watched`:
    what the self-stop published is not lost."""
    sid, child, m = _ordinary_turn_in_flight(env)
    runner = env.node.sessions
    env.clock.advance(runner.self_stop_seconds + runner.watch_seconds + node_sessions.LOCK_SLACK_SECONDS + 5)

    def boom(*a, **k):
        raise RuntimeError("a log handler failed")
    monkeypatch.setattr(node_sessions.log, "warning", boom)
    try:
        runner.enforce_deadlines()
    except RuntimeError:
        pass
    monkeypatch.undo()
    assert any(e.session_id == sid and e.kind == "turn_ended" for e in runner.watched), "kept for the loop"


def test_r5_x_a_failed_handoff_keeps_its_lane(env):
    """Ruling X+: a handoff of the self-stop's failure that fails leaves it in `watched` (its lane), never ahead of a
    native end published before it."""
    from agent_comms.session_host import HostEvent
    sid, child, m = _ordinary_turn_in_flight(env)
    runner = env.node.sessions
    runner.watched.append(HostEvent(sid, "turn_ended", "1", ok=False, detail="stopped"))
    name = "_put" if hasattr(runner, "_put") else "_dispatch"     # (the taker; 0cf1300's was `_dispatch`)
    real, calls = getattr(runner, name), []

    def flaky(x):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("the dispatcher failed")
        return real(x)
    setattr(runner, name, flaky)
    with pytest.raises(RuntimeError):
        with runner.lock:
            runner._emit([])
    setattr(runner, name, real)
    assert [e.detail for e in runner.watched] == ["stopped"] and not runner.host._outbox, "its lane kept"


def test_r5_z_a_kill_that_raises_keeps_the_group_owned_and_recorded(henv, monkeypatch):
    """Sol's blocker 3 (ruling Z), with "Ownership until verified exit (C1)", review round 1, A1 (the name kept for
    the record): at shutdown a TERM-ignoring child whose KILL raises keeps its handle and its group entry (HELD); its
    sibling is stopped; the final save records it; and, delivery being uncertain, no later kill is sent by number
    (identity-safe retries are C2 and C3)."""
    import json as _json
    (s1, c1), (s2, c2) = _held_pair(henv, monkeypatch)
    host = henv.host
    c1.exits_on_terminate = False
    real_kill = c1.kill

    def bad():
        raise OSError("kill failed")
    c1.kill = bad
    token = s1.group_token
    host.stop_all()
    assert s2.child is None, "the sibling is stopped"
    assert s1.child is c1 and s1.group_token == token, "the failed group is still owned"
    assert any(token in g for g in host.groups.entries.values()), "its checkout reservation is kept"
    state = _json.loads(host._state_path().read_text())
    assert s1.session_id in state and state[s1.session_id]["pid"] == c1.pid, "recorded for a restart"
    c1.kill = real_kill
    henv.clock.advance(host.kill_grace + 1)
    host.kill_due()
    assert not c1.killed and host.groups.state(token) == host.groups.HELD, "HELD: no later kill by number (A1)"


def test_r5_z_a_failed_final_save_keeps_the_earlier_record(henv, monkeypatch):
    """Ruling Z+: the final save fails: the earlier recoverable record is left as it was (never truncated)."""
    import json as _json
    from agent_comms import session_host
    (s1, c1), (s2, c2) = _held_pair(henv, monkeypatch)
    host = henv.host
    host._save_state()
    before = _json.loads(host._state_path().read_text())
    assert s1.session_id in before

    def bad_write(fd, data):
        raise OSError("the disk is full")
    monkeypatch.setattr(session_host.os, "write", bad_write)
    with pytest.raises(OSError):
        host.stop_all()
    monkeypatch.undo()
    assert _json.loads(host._state_path().read_text()) == before


def test_r5_z_a_survivor_registered_after_the_latch_is_no_sigterm_time(henv, monkeypatch):
    """Ruling Z2, Z+: a steering session's leader exits after the latch, its group survives: registered as stopping,
    but no SIGKILL comes from that registration; the bulk stop sends the SIGTERM, and the SIGKILL comes only after
    the grace from it."""
    s, child, n = steered(henv, monkeypatch)
    host = henv.host
    host.steer_stopping()
    child.code = 0
    child.exits_on_terminate = False
    henv.groups.add(child.pid)
    host.tick()
    assert not child.terminated
    henv.clock.advance(host.kill_grace + 1)
    host.kill_due()
    assert not s.killed, "no SIGKILL without a SIGTERM"
    real_sleep = host.sleep
    host.sleep = lambda x: henv.clock.advance(x)
    host.stop_all()
    host.sleep = real_sleep
    assert child.terminated == 1, "the bulk stop sent the SIGTERM"


def test_r5_small_emit_hold_saves_nothing_under_a_claim(henv, monkeypatch):
    """Sol's disputed row (`_emit_hold`'s deferral under a claim): a held end's host error sets `_at_once`, and
    `_end` publishes the hold inside its claim: nothing is saved while the cutoff lock is held."""
    s, child, n = steered(henv, monkeypatch)
    s.steer.cover_until = henv.clock() + 20
    child.emit(owned(n, text="N's own"))
    henv.host.tick()
    assert s.held is not None
    lock = henv.host._steer_stop_lock
    held_while = []
    real_save = henv.host._save_state
    henv.host._save_state = lambda: (held_while.append(lock._is_owned()), real_save())[1]

    def broken():
        raise OSError("read failed")
    child.read_lines = broken
    out = henv.host.tick()
    child.read_lines = lambda: []
    assert ends(out) and ends(out)[0].text == "N's own"
    assert not any(held_while), "nothing saved under the cutoff lock"
    henv.host.tick()
    assert held_while and not any(held_while), "the loop's next tick saves"


def test_r5_small_an_early_reap_after_the_latch_reads_nothing(henv, monkeypatch):
    """Sol's disputed row (the early reap after the latch): the leader exited, its group survives, and N's result
    with S arrives between the pass's read and the reap's own (the extra read): the reap reads, decides and signals
    nothing; the bulk stop decides once."""
    s, child, n = steered(henv, monkeypatch)
    host = henv.host
    host.steer_stopping()
    child.code = 0
    henv.groups.add(child.pid)
    real, reads = child.read_lines, []

    def read():
        reads.append(1)
        if len(reads) == 1:
            return []           # the pass's own read: nothing yet
        child.emit(owned(n, S, text="N's own")) if len(reads) == 2 else None
        return real()
    child.read_lines = read
    out = host.tick()
    child.read_lines = real
    assert not child.terminated and getattr(s, "parked", True), "the reap signals nothing: the bulk stop owns the group"
    out += host.stop_all()
    assert len(ends(out)) == 1 and child.terminated == 1, "N decided once; the one SIGTERM is the bulk stop's"


def test_r5_small_a_codex_partial_cut_at_the_latch_is_mains(henv):
    """Ruling (smaller): the latch comes during a Codex partial cut's drain: as main e6b421f, the cut goes on (N
    decided by the cut, the process stopped); the claim is for Claude attempts and held ends."""
    from test_codex_steering import partial, ended_of
    from agent_comms.session_host import STEER_PARTIAL_SECONDS
    host, s, child, _ = partial(henv)
    henv.clock.advance(STEER_PARTIAL_SECONDS)
    host.steer_drain_hook = lambda sess: host.steer_stopping()
    events = host.steer_deadlines()
    host.steer_drain_hook = None
    assert s.stopping_since is not None, "the cut stopped the process, as main"
    assert [e.message_id for e in ended_of(events)] == ["a"], "N decided once by the cut"


def test_r5_small_a_broken_pipe_through_the_write_path(henv, monkeypatch):
    """Ruling (smaller): a steering session's process closes its stdin while S is taken in part (the write path):
    the claim decides N once and the SIGTERM goes once."""
    monkeypatch.setattr(claude, "VERIFIED", frozenset({V}))
    henv.host = make_host(henv, timeout_minutes=1)
    henv.host.steer_authority = lambda sess: True
    s, child = idle_host(henv)
    henv.host.send(SID, "a long turn", message_id="1")
    henv.host.tick()
    n = uuid_of(child)
    emit(henv, child, vinit(n))
    child.accept = 10
    henv.host.steer_offer(SID, "1", S, TEXT, authorized=True)
    henv.host.tick()
    assert s.steer is not None and 0 < s.steer.sent
    child.accept = None
    child.broken = True
    out = henv.host.tick() + henv.host.tick()
    assert len(ends(out)) == 1 and child.terminated == 1


def test_r5_known_limit_a_raising_log_handler_between_a_claim_and_its_signal(henv, monkeypatch):
    """Known limit (round 5, pinned as it is today): on the LOOP thread a log handler raises while a claim's deferred
    line is written (after the cutoff lock, before the SIGTERM): the decision stands, made once; the raise ends that
    session's pass (the host-error path), which sends the stop; N is reported once."""
    from agent_comms import session_host
    s, child, n = steered(henv, monkeypatch)
    real, raised = session_host.log.log, []

    def log(level, msg, *args, **kw):
        if "the steer into message" in msg and not raised:
            raised.append(1)
            raise RuntimeError("a log handler failed")
        return real(level, msg, *args, **kw)
    monkeypatch.setattr(session_host.log, "log", log)
    henv.clock.advance(61)          # N's turn times out (the pre-hold timeout: `_steer_end` decides S)
    out = henv.host.tick() + henv.host.tick() + henv.host.tick()
    assert raised, "the handler raised"
    assert len(ends(out)) == 1, "N decided and reported once"
    assert child.terminated >= 1, "the stop went (by the host-error path)"


# ---------------------------------------------------------------- review round 6 (narrow)


def _stopping_turn(env, project=None):
    """An ordinary turn whose process ignores SIGTERM, stopped by the loop (under its lock), its grace not yet over."""
    sid, child, m = _ordinary_turn_in_flight(env, project=project)
    runner = env.node.sessions
    s = runner.host.sessions[sid]
    child.exits_on_terminate = False
    with runner.lock:
        runner.host._stop(s, env.clock())
    return sid, child, s


def test_r6_n1_a_kill_qualified_by_the_watchdog_never_hits_a_replacement(env):
    """Sol's round-6 interleaving 1 (ruling N1): the watchdog qualifies A's process for its SIGKILL; the loop then
    replaces A's child before the signal. On main the kill step holds the runner lock, so the loop cannot run in
    between: the SIGKILL reaches A's own process, never the replacement."""
    import threading
    from test_session_host import FakeSessionChild
    sid, child, s = _stopping_turn(env)
    runner = env.node.sessions
    env.clock.advance(runner.host.kill_grace + 1)
    repl = FakeSessionChild(child.pid + 500)
    loop_done = threading.Event()

    def loop_replaces():
        with runner.lock:
            s.child = repl          # (the loop: A's process went, its successor started)
        loop_done.set()
    fired = []

    class Hooked(type(s)):
        def __setattr__(self, name, value):
            if name == "killed" and value is True and not fired:
                fired.append(1)
                threading.Thread(target=loop_replaces, daemon=True).start()
                loop_done.wait(1)       # (the loop gets its chance here, unless the watchdog holds its lock)
            object.__setattr__(self, name, value)
    s.__class__ = Hooked
    t, done, ident, err = _on_watchdog(runner)
    assert done.wait(5) and not err, err
    loop_done.wait(5)
    s.__class__ = Hooked.__mro__[1]
    assert fired, "the watchdog qualified A"
    assert child.killed and not repl.killed, "the SIGKILL reached A's process, never its replacement"


def test_r6_n1_no_kill_comes_between_a_stop_and_its_sigterm(env):
    """Sol's round-6 interleaving 2 (ruling N1): the loop is paused between registering a stop (`stopping_since`)
    and sending its SIGTERM, long enough for the grace; the watchdog's pass cannot SIGKILL in between: it waits for
    the runner lock, as on main, so the order is SIGTERM, then SIGKILL."""
    import threading
    sid, child, m = _ordinary_turn_in_flight(env)
    runner = env.node.sessions
    host = runner.host
    s = host.sessions[sid]
    child.exits_on_terminate = False
    order = []
    real_term, real_kill = child.terminate, child.kill
    child.terminate = lambda: (order.append("TERM"), real_term())[1]
    child.kill = lambda: (order.append("KILL"), real_kill())[1]
    real_stopping = host.groups.stopping
    watch = {}

    def stopping(token):
        real_stopping(token)
        env.clock.advance(host.kill_grace + 1)      # (the loop is held up here)
        watch["run"] = _on_watchdog(runner)
        watch["run"][1].wait(1)
    host.groups.stopping = stopping
    with runner.lock:
        host._stop(s, env.clock())
    host.groups.stopping = real_stopping
    assert watch["run"][1].wait(5)
    assert order[:1] == ["TERM"], order
    assert "KILL" not in order or order.index("KILL") > order.index("TERM"), order


def test_r7_1_the_watchdogs_pass_takes_the_runner_lock_once(env):
    """Ruling R7-1 (round 6's bounded section withdrawn): the pass takes the runner lock ONCE, with main's unbounded
    wait, and runs the self-stop, the kills and the steer deadlines inside that one section, as main e6b421f."""
    runner = env.node.sessions
    real = runner.lock
    waits = []

    class Recording:
        def __enter__(self):
            waits.append("unbounded")
            return real.__enter__()

        def __exit__(self, *a):
            return real.__exit__(*a)

        def acquire(self, *a, **k):
            waits.append(k.get("timeout", a[1] if len(a) > 1 else "unbounded"))
            return real.acquire(*a, **k)

        def release(self):
            return real.release()
    runner.lock = Recording()
    try:
        runner.enforce_deadlines()
    finally:
        runner.lock = real
    assert waits == ["unbounded"], waits


def test_r6_n2_a_stalled_transcript_write_at_shutdown_holds_up_no_kill(henv, monkeypatch, tmp_path):
    """Ruling N2 (the one regression Sol found in round 6): lines a watchdog drain left buffered for s1's
    transcript, and a transcript write that stalls at shutdown: every group's SIGKILL is still sent before any
    remaining transcript is written."""
    import os as _os
    import threading
    from agent_comms import session_host
    (s1, c1), (s2, c2) = _held_pair(henv, monkeypatch)
    host = henv.host
    c1.exits_on_terminate = c2.exits_on_terminate = False
    fd = _os.open(tmp_path / "t1", _os.O_WRONLY | _os.O_CREAT, 0o600)
    s1.transcript_fd = fd
    if hasattr(s1, "tee_pending"):
        s1.tee_pending.append(b'{"left": "by a watchdog drain"}')
    stall, released = threading.Event(), threading.Event()
    real_write = session_host.os.write

    def write(f, data):
        if f == fd and not released.is_set():
            stall.set()
            released.wait(5)
        return real_write(f, data)
    monkeypatch.setattr(session_host.os, "write", write)
    t = threading.Thread(target=host.stop_all, daemon=True)
    t.start()
    import time as _time
    deadline = _time.monotonic() + 3
    while _time.monotonic() < deadline and not (c1.killed and c2.killed):
        _time.sleep(0.02)
    killed = (c1.killed, c2.killed)
    released.set()
    t.join(5)
    assert killed[0] and killed[1], f"every group's SIGKILL before any transcript write: {killed}"


def test_r6_n3_no_handler_runs_for_a_provider_logger_on_the_watchdogs_thread(env):
    """Ruling W2.3 (N3): a late answer to a Codex request retired with its turn is read by main's watchdog drain;
    the Codex provider's own logger says so. No handler runs on the watchdog's thread (a stalling one would hold the
    pass); the loop's next pass writes the line."""
    import logging
    import threading
    import test_codex_steering as tcs
    from agent_comms.session_host import STEER_PARTIAL_SECONDS
    sid, child = tcs.codex_turn_in_flight(env)
    child.accept = 10
    tcs.post_steer(env, sid)
    tick(env)
    s = env.node.sessions.host.sessions[sid]
    assert s.steer is not None and 0 < s.steer.sent < s.steer.length
    s.provider._retired.add(987)
    child.emit(tcs.cx.reply(987, {"turnId": tcs.cx.TURN}))
    env.clock.advance(STEER_PARTIAL_SECONDS)
    seen, stall = [], threading.Event()

    class Stalling(logging.Handler):
        def emit(self, record):
            seen.append((record.name, threading.get_ident()))
            stall.wait(3)
    h = Stalling()
    pkg = logging.getLogger("agent_comms")
    provider = logging.getLogger("agent_comms.providers.codex")
    level = provider.level
    provider.setLevel(logging.INFO)     # (the provider's line is an INFO one)
    pkg.addHandler(h)
    try:
        t, done, ident, err = _on_watchdog(env.node.sessions)
        returned = done.wait(1.5)
        stall.set()
        done.wait(5)
        assert returned and not err, "the pass was not held by a handler"
        assert not [n for n, th in seen if th == ident[0]], "no handler on the watchdog's thread"
        stall.clear()
        stall.set()
        env.node.sessions.pass_once()
        assert any(n == "agent_comms.providers.codex" for n, th in seen), "the loop wrote the provider's line"
    finally:
        pkg.removeHandler(h)
        provider.setLevel(level)


def test_r6_n3_no_traceback_is_formatted_on_the_watchdogs_thread(env, monkeypatch):
    """Ruling W2.3 (N3): the self-stop raises on the watchdog's thread and is logged with its exception: nothing
    formats a traceback there (only the exception's type and text are kept); the loop writes the line."""
    import threading
    import traceback
    sid, child, m = _ordinary_turn_in_flight(env)
    runner = env.node.sessions
    env.clock.advance(runner.self_stop_seconds + 1)

    def fails(*a, **k):
        raise OSError("the self-stop failed here")
    runner.host.stop_turns = fails
    formatted = []
    for name in ("format_exc", "format_exception", "format_tb", "extract_tb"):
        real = getattr(traceback, name)
        monkeypatch.setattr(traceback, name,
                            lambda *a, _r=real, **k: (formatted.append(threading.get_ident()), _r(*a, **k))[1])
    t, done, ident, err = _on_watchdog(runner)
    assert done.wait(5) and not err, err
    assert ident[0] not in formatted, "no traceback formatted on the watchdog's thread"


def test_r6_y_an_extension_only_next_turn_422_forgets_nothing(env):
    """Ruling Y (the literal legacy rung, Sol's round-6 sequence): A's report carries no modes and only a
    `next_turn` extension; the hub refuses it with 422 and takes it with only `next_turn` stripped (rung (ii)): that
    is not main's plain retry of a step-2 outcome, so `steer` is not forgotten."""
    a, b, runner = _claude_next_turn_and_codex_steer_pending(env)
    runner.hub_features = set(runner.hub_features) - {"restart"}       # (no `modes` in the report)
    _posts(runner, lambda path, body: 422 if any(isinstance(e.get("steer"), dict)
                                                 and e["steer"].get("outcome") == "next_turn"
                                                 for e in body["events"]) else None)
    with runner.lock:
        assert runner._flush()
    assert "steer" in runner.hub_features, "only main's literal legacy rung forgets `steer`"


def test_r6_x_a_checkpoint_and_its_receipt_commit_together(env):
    """Ruling X (round 6): a checkpoint's acceptance and its receipt commit together: a raise after the commit, a
    flush, then the retry: one checkpoint POST."""
    sid, child, n, s_uuid = claude_steered(env)
    runner = env.node.sessions
    posts = []
    real_req = runner.client.request

    def request(method, path, **kw):
        if method == "POST" and isinstance(kw.get("json"), dict) and "held" in kw["json"]:
            posts.append(path)
        return real_req(method, path, **kw)
    runner.client.request = request
    real, calls = runner._checkpoint, []

    def checkpoint(*a, **k):
        r = real(*a, **k)
        if not calls:
            calls.append(1)
            raise RuntimeError("after the commit")
        return r
    runner._checkpoint = checkpoint
    child.emit(cresult(sid, n, text="N's reply"))
    with pytest.raises(RuntimeError):
        runner.pass_once()
    with runner.lock:
        runner._flush()
    runner._checkpoint = real
    tick(env, times=2)
    assert len(posts) == 1, posts


@pytest.mark.parametrize("fails", [1, 2])
def test_r6_x_the_final_shutdown_handoff_is_retried_once(env, fails):
    """Ruling X (round 6): shutdown's final handoff raises: it is tried once more. One failure: everything is
    reported. Two (exhausted, pinned): shutdown still completes; what was not taken stays in the host's outbox (the
    next node process reports those turns as lost), and the error is logged."""
    sid = start(env, "an ordinary turn")["session_id"]
    tick(env)
    child = proc_of(env, sid)[1]
    child.emit(cinit(sid, written(child)[-1]["uuid"]))
    tick(env)
    runner = env.node.sessions
    real, calls = runner._put, []

    def put(e):
        if len(calls) < fails:
            calls.append(1)
            raise RuntimeError("the handoff failed")
        return real(e)
    runner._put = put
    runner.signal()
    runner.kill()
    runner.shutdown()
    runner._put = real
    m = hosted._get(env.board.conn, hosted._msg_key(sid, 1))
    if fails == 1:
        assert m["state"] == "failed" and "shut down" in m["detail"], m
    else:
        assert m["state"] != "failed" or "shut down" not in m["detail"], "exhausted: not reported by this process"
        assert any(e.session_id == sid for e in runner.host._outbox), "left in the outbox"


def test_r6_n4_known_limit_a_registry_wait_ahead_of_the_kill_step(env):
    """Known limit (round 6, N4; main e6b421f's behaviour): a spawn holds the group registry's lock while the
    watchdog's self-stop needs it: the due SIGKILL of another group waits behind it (the kill step comes after the
    self-stop, under the same runner lock, as on main), and goes as soon as the lock is let go."""
    import threading
    import time as _time
    from test_hosted_sessions import P2
    sid2, c2, m2 = _ordinary_turn_in_flight(env, project=P2)
    sid, child, s = _stopping_turn(env)
    runner = env.node.sessions
    host = runner.host
    env.clock.advance(max(host.kill_grace, runner.self_stop_seconds) + 1)
    release, held = threading.Event(), threading.Event()

    def spawn_holding_the_registry():
        with host.groups.lock:
            held.set()
            release.wait(10)
    threading.Thread(target=spawn_holding_the_registry, daemon=True).start()
    assert held.wait(3)
    t, done, ident, err = _on_watchdog(runner)
    _time.sleep(0.5)
    waited = not child.killed
    release.set()
    assert done.wait(5) and not err, err
    assert waited, "pinned: the due SIGKILL waited behind the registry's lock (main's)"
    assert child.killed, "and went once the lock was let go"


def test_r6_n4_known_limit_a_kill_that_returns_with_survivors_releases_ownership(henv, monkeypatch):
    """Known limit (round 6, N4), CLOSED by "Ownership until verified exit (C1)" (the name kept for the record): at
    shutdown a KILL that returns while members of the group survive (a no-op signal, or one not yet delivered): the
    session now KEEPS its handle, its group entry and its record until its group is proved gone; its sibling is
    stopped. (A KILL that RAISES keeps the group too: round 5.)"""
    import json as _json
    (s1, c1), (s2, c2) = _held_pair(henv, monkeypatch)
    host = henv.host
    c1.exits_on_terminate = False
    c1.kill = lambda: None          # (returns; the group is still there)
    henv.groups.add(c1.pid)
    token = s1.group_token
    host.stop_all()
    assert s2.child is None, "the sibling is stopped"
    assert s1.child is c1 and any(token in g for g in host.groups.entries.values()), "kept (C1)"
    assert s1.session_id in _json.loads(host._state_path().read_text()), "its record kept (C1)"


# ---------------------------------------------------------------- review round 7


class _StallingHandler:
    """A handler on the package logger that stalls (as a blocked disk or pipe would) on every record of `thread`
    (all threads if None), and records (logger name, thread, message) for each."""

    def __init__(self, seconds=3.0, thread=None):
        import logging
        import threading
        self.seen, self.release, self.thread = [], threading.Event(), thread

        class H(logging.Handler):
            def emit(h, record):
                me = threading.get_ident()
                self.seen.append((record.name, me, record.getMessage()))
                if self.thread is None or me == self.thread:
                    self.release.wait(seconds)
        self.handler = H()
        self.logger = logging.getLogger("agent_comms")
        self.level = self.logger.level

    def __enter__(self):
        import logging
        self.logger.setLevel(logging.DEBUG)
        self.logger.addHandler(self.handler)
        return self

    def __exit__(self, *a):
        self.release.set()
        self.logger.removeHandler(self.handler)
        self.logger.setLevel(self.level)


def _wait_for(pred, seconds=3.0):
    import time as _time
    deadline = _time.monotonic() + seconds
    while _time.monotonic() < deadline:
        if pred():
            return True
        _time.sleep(0.02)
    return pred()


def test_r7_1_a_kill_due_after_the_scan_goes_when_main_would(env):
    """Sol's round-7 blocker 1 (ruling R7-1): after the watchdog's kill scan the loop takes the runner lock and
    keeps it past one watch interval; a kill that came due after the scan goes as soon as the loop lets go, as on
    main (one critical section per pass: the next pass is already waiting for the lock), never a watch interval
    later."""
    import threading
    import time as _time
    sid, child, s = _stopping_turn(env)
    runner = env.node.sessions
    host = runner.host
    runner.watch_seconds = 0.5
    real = runner.lock
    loop_holds, released, killed = threading.Event(), [], []

    def loop():
        with real:
            loop_holds.set()
            _time.sleep(0.75)
            released.append(_time.monotonic())

    class Proxy:
        """The runner lock, with the loop let in first wherever the watchdog takes it a second time in a pass."""
        def __enter__(self):
            return real.__enter__()

        def __exit__(self, *a):
            return real.__exit__(*a)

        def acquire(self, *a, **k):
            loop_holds.wait(2)
            return real.acquire(*a, **k)

        def release(self):
            return real.release()
    real_kill_due, calls = host.kill_due, []

    def kill_due():
        out = real_kill_due()
        if not calls:
            calls.append(1)
            env.clock.advance(host.kill_grace + 1)      # (it comes due after this scan)
            threading.Thread(target=loop, daemon=True).start()
        return out
    host.kill_due = kill_due
    real_kill = child.kill
    child.kill = lambda: (killed.append(_time.monotonic()), real_kill())[1]
    runner.lock = Proxy()
    watch = threading.Thread(target=runner._watch, daemon=True)
    watch.start()
    try:
        assert _wait_for(lambda: killed, 5), "the kill was sent"
    finally:
        runner.stopping.set()
        watch.join(5)
        runner.lock = real
        runner.stopping.clear()
    assert released and killed[0] - released[0] <= 0.15, (killed, released)


def test_r7_a1_a_held_deadline_on_the_watchdog_never_waits_on_the_registry(env):
    """Ruling A1, as round 8's C1 has it: a spawn holds the group registry while a held Claude end's contact runs
    out and another session's kill comes due after the scan: the watchdog does not wait for the registry and changes
    nothing of the hold (no claim, no fence, no SIGTERM, no kill clock), so the next scan sends the kill on its
    cadence; once the registry is free the hold's stop goes on a later pass, and its SIGKILL is never earlier than
    its real SIGTERM plus the grace."""
    import threading
    import time as _time
    from test_hosted_sessions import P2
    a, ca, n, s_uuid = claude_steered(env)
    runner = env.node.sessions
    host = runner.host
    ca.emit(cresult(a, n, text="N's reply"))
    tick(env)
    sa = host.sessions[a]
    assert sa.held is not None
    ca.exits_on_terminate = False
    b, cb, sb = _stopping_turn(env, project=P2)
    runner.watch_seconds = 0.3
    sa.steer.cover_until = env.clock() - 1      # (the hold's own contact ran out)
    release, held = threading.Event(), threading.Event()

    def spawn_holding_the_registry():
        with host.groups.lock:
            held.set()
            release.wait(10)
    threading.Thread(target=spawn_holding_the_registry, daemon=True).start()
    assert held.wait(3)
    real_kill_due, scans = host.kill_due, []

    def kill_due():
        out = real_kill_due()
        scans.append(_time.monotonic())
        if len(scans) == 1:
            env.clock.advance(host.kill_grace + 1)      # (b's kill comes due after this scan)
        return out
    host.kill_due = kill_due
    killed = []
    real_kill = cb.kill
    cb.kill = lambda: (killed.append(_time.monotonic()), real_kill())[1]
    watch = threading.Thread(target=runner._watch, daemon=True)
    watch.start()
    try:
        assert _wait_for(lambda: killed, 5), "b's kill was sent"
        assert killed[0] - scans[0] <= runner.watch_seconds + 0.5, "on the watchdog's cadence"
        assert sa.held is not None and sa.held.decided is None and sa.held.fenced_at is None, "the hold untouched"
        assert not ca.terminated and sa.stopping_since is None, "no SIGTERM, no kill clock, while the registry waits"
        release.set()
        assert _wait_for(lambda: ca.terminated, 5), "its stop goes once the registry is free"
    finally:
        release.set()
        runner.stopping.set()
        watch.join(5)
        runner.stopping.clear()
    term_at = sa.stopping_since
    assert term_at is not None
    env.clock.advance(host.kill_grace - 1)
    with runner.lock:
        host.kill_due()
    assert not ca.killed, "no SIGKILL before its real SIGTERM plus the grace"


@pytest.mark.parametrize("phase", ["held", "s_turn"])
def test_r7_2_a_held_end_at_shutdown_runs_no_handler_before_the_signals(henv, monkeypatch, phase):
    """Sol's round-7 blocker 2 (ruling R7-2): two held ends at shutdown (one with S's own turn open), a stalling
    log handler: every group's SIGTERM and SIGKILL is sent; the lines are written after the bulk SIGKILL pass."""
    import threading
    (s1, c1), (s2, c2) = _held_pair(henv, monkeypatch)
    host = henv.host
    if phase == "s_turn":
        c1.emit(vinit(S))
        host.tick()
        assert s1.held is not None and s1.held.s_turn
    c1.exits_on_terminate = c2.exits_on_terminate = False
    with _StallingHandler() as h:
        t = threading.Thread(target=host.stop_all, daemon=True)
        t.start()
        h.thread = t.ident
        ok = _wait_for(lambda: c1.terminated and c2.terminated and c1.killed and c2.killed, 2.0)
        h.release.set()
        t.join(10)
    assert ok, (c1.terminated, c2.terminated, c1.killed, c2.killed)


def test_r7_2_a_raising_kill_with_a_stalling_handler_still_kills_the_siblings(henv, monkeypatch):
    """Ruling R7-2: the first group's SIGKILL raises and its log line would stall: the sibling's SIGKILL is sent."""
    import threading
    (s1, c1), (s2, c2) = _held_pair(henv, monkeypatch)
    host = henv.host
    c1.exits_on_terminate = c2.exits_on_terminate = False

    def bad():
        raise OSError("kill failed")
    c1.kill = bad
    with _StallingHandler() as h:
        t = threading.Thread(target=host.stop_all, daemon=True)
        t.start()
        h.thread = t.ident
        ok = _wait_for(lambda: c2.killed, 2.0)
        h.release.set()
        t.join(10)
    assert ok, "the sibling's SIGKILL"
    assert any("SIGKILL" in m for _, _, m in h.seen), "the line was written, after the pass"


@pytest.mark.parametrize("entry", ["shutdown", "signal_then_shutdown"])
def test_r7_2_both_shutdown_entry_points_signal_every_group_with_a_stalling_handler(env, entry):
    """Ruling R7-2: the node's shutdown (signals first) and the loop-exit cleanup (`sessions.shutdown()` directly,
    node.py): a held Claude end and an ordinary sibling, a stalling handler: both groups get their SIGTERM and
    SIGKILL."""
    import threading
    from test_hosted_sessions import P2
    a, ca, n, s_uuid = claude_steered(env)
    runner = env.node.sessions
    ca.emit(cresult(a, n, text="N's reply"))
    tick(env)
    assert runner.host.sessions[a].held is not None
    b, cb, mb = _ordinary_turn_in_flight(env, project=P2)
    ca.exits_on_terminate = cb.exits_on_terminate = False

    def run():
        if entry == "signal_then_shutdown":
            runner.signal()
            runner.kill()
        runner.shutdown()
    with _StallingHandler(seconds=6) as h:
        t = threading.Thread(target=run, daemon=True)
        t.start()
        h.thread = t.ident
        ok = _wait_for(lambda: ca.terminated and cb.terminated and ca.killed and cb.killed, 2.0)
        h.release.set()
        t.join(15)
    assert ok, (ca.terminated, cb.terminated, ca.killed, cb.killed)


def test_r7_a1_the_loop_stops_an_expired_hold_by_itself(env):
    """Ruling A1, as round 8's C1 has it (a mutation row): an expired hold left as it was on the watchdog's thread
    (the registry was busy) is stopped by the loop's next tick once the registry is free, with no watchdog pass:
    the same code on both."""
    import threading
    a, ca, n, s_uuid = claude_steered(env)
    runner = env.node.sessions
    host = runner.host
    ca.emit(cresult(a, n, text="N's reply"))
    tick(env)
    sa = host.sessions[a]
    ca.exits_on_terminate = False
    sa.steer.cover_until = env.clock() - 1
    release, held = threading.Event(), threading.Event()

    def spawn_holding_the_registry():
        with host.groups.lock:
            held.set()
            release.wait(10)
    threading.Thread(target=spawn_holding_the_registry, daemon=True).start()
    assert held.wait(3)
    t, done, ident, err = _on_watchdog(runner)
    assert done.wait(5) and not err, err
    assert sa.held.decided is None and not ca.terminated
    release.set()
    _wait_for(lambda: host.groups.lock.acquire(blocking=False) and (host.groups.lock.release() or True), 3)
    host.tick()
    assert ca.terminated == 1 and sa.stopping_since is not None and sa.held.decided is not None


def test_r7_2_what_a_process_wrote_last_reaches_its_transcript_at_shutdown(henv, monkeypatch, tmp_path):
    """Ruling R7-2 (a mutation row): the deferral ends with the bulk SIGKILL pass: the per-session cleanup after it
    writes a process's last output to its transcript (it is not held back as a watchdog's lines are)."""
    import os as _os
    (s1, c1), (s2, c2) = _held_pair(henv, monkeypatch)
    fd = _os.open(tmp_path / "t1", _os.O_WRONLY | _os.O_CREAT, 0o600)
    s1.transcript_fd = fd
    c1.emit(plain(text="the last words"))
    henv.host.stop_all()
    assert b"the last words" in (tmp_path / "t1").read_bytes()


def test_r7_2_a_stop_all_that_raises_still_saves_for_real(henv, monkeypatch):
    """Ruling R7-2 (a mutation row): the span raises (the wait for the groups): logging is restored first, so the
    final save is a real one (the state file is written), never left deferred."""
    (s1, c1), (s2, c2) = _held_pair(henv, monkeypatch)
    host = henv.host
    c1.exits_on_terminate = False
    host._state_path().unlink(missing_ok=True)

    def bad(_):
        raise OSError("failed")
    host.sleep = bad
    with pytest.raises(OSError):
        host.stop_all()
    assert host._state_path().exists(), "the final save was made"


def test_r7_a2_stop_all_restores_the_callers_prior_diversion(henv, monkeypatch):
    """Ruling A2: `stop_all` run on a thread that was already diverting (a watchdog's) leaves it diverting."""
    from agent_comms import session_host
    _held_pair(henv, monkeypatch)
    seen = []
    t, done, ident, err = _on_watchdog(None, fn=lambda: (henv.host.stop_all(), seen.append(session_host.on_watch())))
    assert done.wait(10) and not err, err
    assert seen == [True]


def test_r7_a2_a_held_publication_at_shutdown_does_no_io(henv, monkeypatch):
    """Ruling A2 (pin): the publication of a held end's result inside `stop_all` stays in place (event order); it
    appends to the outbox in memory and saves nothing there (the save waits for the end of `stop_all`)."""
    from agent_comms import session_host
    _held_pair(henv, monkeypatch)
    host = henv.host
    inside, io = [], []
    real_emit = host._emit_hold

    def emit_hold(*a, **k):
        inside.append(1)
        try:
            return real_emit(*a, **k)
        finally:
            inside.pop()
    host._emit_hold = emit_hold
    for name in ("write", "replace", "open"):
        real = getattr(session_host.os, name)
        monkeypatch.setattr(session_host.os, name,
                            lambda *a, _r=real, _n=name, **k: (io.append(_n) if inside else None, _r(*a, **k))[1])
    host.stop_all()
    assert not io, io


def test_r7_a3_a_deferred_line_carries_the_exception_type_only(env):
    """Ruling A3: a line logged with an exception on a diverting thread is replayed with the exception's TYPE only,
    never its text (which gives no guarantee against steer or message text)."""
    import logging
    from agent_comms import session_host

    class Marked(Exception):
        pass

    def logs():
        try:
            raise Marked("PINEAPPLE-STEER-TEXT")
        except Marked:
            logging.getLogger("agent_comms.node_sessions").exception("hosted sessions: something failed")
    t, done, ident, err = _on_watchdog(None, fn=logs)
    assert done.wait(5) and not err, err
    with _StallingHandler(seconds=0) as h:
        session_host.flush_watch_logs()
    lines = [m for _, _, m in h.seen]
    assert any("something failed" in m and "Marked" in m for m in lines), lines
    assert not any("PINEAPPLE" in m for m in lines), lines


def test_r7_3_the_deferred_queue_counts_what_it_drops(env):
    """Ruling R7-3: more lines than the queue holds are logged on a diverting thread: the replay says, in one line,
    how many were dropped."""
    import logging
    from agent_comms import session_host
    n = session_host._WATCH_LINES.maxlen + 7

    def logs():
        lg = logging.getLogger("agent_comms.node_sessions")
        for i in range(n):
            lg.warning("line %d", i)
    t, done, ident, err = _on_watchdog(None, fn=logs)
    assert done.wait(10) and not err, err
    with _StallingHandler(seconds=0) as h:
        session_host.flush_watch_logs()
    lines = [m for _, _, m in h.seen]
    assert any("7" in m and "dropped" in m for m in lines), lines[-3:]


# ---------------------------------------------------------------- review round 8


def _expired_hold_with_the_registry_busy(env, project=None, before_the_stall=None):
    """A Claude session whose held end's contact ran out, its process ignoring SIGTERM, and a spawn holding the
    group registry (`before_the_stall` runs first, once A is held): (sid, child, session, release)."""
    import threading
    a, ca, n, s_uuid = claude_steered(env, project=project)
    runner = env.node.sessions
    host = runner.host
    ca.emit(cresult(a, n, text="N's reply"))
    tick(env)
    sa = host.sessions[a]
    assert sa.held is not None
    if before_the_stall is not None:
        before_the_stall()
    ca.exits_on_terminate = False
    sa.steer.cover_until = env.clock() - 1
    release, held = threading.Event(), threading.Event()

    def spawn_holding_the_registry():
        with host.groups.lock:
            held.set()
            release.wait(15)
    threading.Thread(target=spawn_holding_the_registry, daemon=True).start()
    assert held.wait(3)
    return a, ca, sa, release


def _registry_free(host):
    return _wait_for(lambda: host.groups.lock.acquire(blocking=False) and (host.groups.lock.release() or True), 3)


def test_r8_c1_the_loops_tick_never_waits_on_the_registry(env):
    """Sol's round-8 blocker 1 (ruling C1): a spawn holds the registry, A's hold has expired, the loop ticks (under
    the runner lock), B's KILL comes due: neither thread waits on the registry; the watchdog takes the runner lock
    and B's KILL goes when main would send it."""
    import threading
    from test_hosted_sessions import P2
    bs = []
    a, ca, sa, release = _expired_hold_with_the_registry_busy(
        env, before_the_stall=lambda: bs.append(_stopping_turn(env, project=P2)))
    b, cb, sb = bs[0]
    runner = env.node.sessions
    host = runner.host
    loop_done = threading.Event()

    def loop():
        with runner.lock:
            host.tick()
        loop_done.set()
    try:
        threading.Thread(target=loop, daemon=True).start()
        assert loop_done.wait(2), "the loop's tick did not wait on the registry"
        env.clock.advance(host.kill_grace + 1)      # (B's KILL comes due: the watchdog's)
        t, done, ident, err = _on_watchdog(runner)
        assert done.wait(2) and not err, err
        assert cb.killed, "B's KILL went"
        assert sa.held.decided is None and not ca.terminated, "A's hold untouched while the registry is busy"
    finally:
        release.set()


def test_r8_c1_during_the_stall_the_checkout_is_as_for_a_hold_before_its_deadline(env):
    """Ruling C1/C2: while the registry is busy an expired hold reads as any held end: its group stays LIVE in the
    registry (no STOPPING barrier invented, none taken away), N's end is not published (N's turn still holds the
    checkout on the hub), its process is not stopped."""
    a, ca, sa, release = _expired_hold_with_the_registry_busy(env)
    runner = env.node.sessions
    host = runner.host
    try:
        out = []
        for _ in range(3):
            with runner.lock:
                out += host.tick()
            t, done, ident, err = _on_watchdog(runner)
            assert done.wait(3) and not err, err
        states = [st for g in host.groups.entries.values() for tok, (st, cwd) in g.items() if tok == sa.group_token]
        assert states == [host.groups.LIVE], states
        assert not [e for e in out + list(host._outbox) if e.kind == "turn_ended" and e.session_id == a]
        assert sa.in_flight is not None and not ca.terminated
    finally:
        release.set()


def test_r8_c1_once_free_reservation_claim_sigterm_publication_in_order(env):
    """Ruling C1, with the enabling PR's ruling S2's order: after the registry frees: the reservation, the SIGTERM
    (escalation armed first), the claim's decision, the publication, once each, in that order; the SIGKILL never
    earlier than that SIGTERM plus the grace."""
    a, ca, sa, release = _expired_hold_with_the_registry_busy(env)
    runner = env.node.sessions
    host = runner.host
    order = []
    real_try, real_fin, real_pub = host.groups.try_stopping, host._finalize_hold, host._publish_hold

    def try_stopping(token, barrier=None):
        got = real_try(token, barrier)
        if got in (host.groups.RESERVED, host.groups.ALREADY):
            order.append("reserve")
        return got
    host.groups.try_stopping = try_stopping
    host._finalize_hold = lambda *x, **k: (order.append("claim"), real_fin(*x, **k))[1]
    host._publish_hold = lambda *x, **k: (order.append("publish"), real_pub(*x, **k))[1]
    real_term = ca.terminate
    ca.terminate = lambda: (order.append("TERM"), real_term())[1]
    with runner.lock:
        host.tick()
    assert order == [], "nothing while the registry is busy"
    release.set()
    assert _registry_free(host)
    with runner.lock:
        host.tick()
    term_at = sa.stopping_since
    assert order == ["reserve", "TERM", "claim"], order
    env.clock.advance(host.kill_grace - 1)
    with runner.lock:
        host.kill_due()
    assert not ca.killed, "no SIGKILL before its SIGTERM plus the grace"
    ca.code = -9
    with runner.lock:
        host.tick()
    assert order == ["reserve", "TERM", "claim", "publish"], order
    assert term_at is not None


def test_r9_d1_a_reserved_stop_whose_sigterm_raises_is_mains(henv, monkeypatch):
    """Sol's round-9 blocker 1 (ruling D1): the checkout reserved, the SIGTERM raises: as on main the kill clock is
    kept, the kill scan sends the SIGKILL after the grace, and N's end is published only once the group is gone (never
    while it is live with no clock). Replaces round 8's no-clock test (that ruling is withdrawn)."""
    s, child, n = steered(henv, monkeypatch)
    s.steer.cover_until = henv.clock() + 20
    child.emit(owned(n, text="N"))
    henv.host.tick()
    henv.clock.advance(21)
    child.exits_on_terminate = False

    def bad():
        raise OSError("the signal failed")
    real_term = child.terminate
    child.terminate = bad
    with pytest.raises(OSError):
        henv.host._hold_cut(s, henv.clock(), henv.host._outbox, drain=False)
    child.terminate = real_term
    assert s.stopping_since is not None, "the kill clock kept, as main"
    out = []
    henv.clock.advance(henv.host.kill_grace - 1)
    out += henv.host.tick()
    assert not child.killed and not ends(out), "no SIGKILL before the grace; N not published while the group lives"
    henv.clock.advance(2)
    out += henv.host.tick() + henv.host.tick()
    assert child.killed, "the kill scan escalated"
    assert len(ends(out)) == 1, "N's end, once, after the group is gone"


def _registry_state(host, token):
    return [st for g in host.groups.entries.values() for tok, (st, cwd) in g.items() if tok == token]


@pytest.mark.parametrize("before", ["live", "stopping"])
def test_r9_e1_a_commit_that_fails_early_undoes_only_its_own_reservation(henv, monkeypatch, before):
    """Ruling E1, superseded by the enabling PR's ruling S2 (the name kept for the record; `Groups.unreserve` is
    gone): the decision raises after the reservation: escalation was armed first, so the group has its owner, its
    kill clock and its SIGTERM, and the checkout stays STOPPING (nothing is undone) whether the barrier was this
    attempt's or there before."""
    s, child, n = steered(henv, monkeypatch)
    s.steer.cover_until = henv.clock() + 20
    child.emit(owned(n, text="N"))
    henv.host.tick()
    henv.clock.advance(21)
    if before == "stopping":
        henv.host.groups.stopping(s.group_token)

    def bad(*a, **k):
        raise RuntimeError("the decision failed")
    monkeypatch.setattr(henv.host, "_fail", bad)
    with pytest.raises(RuntimeError):
        henv.host._hold_cut(s, henv.clock(), henv.host._outbox, drain=False)
    assert _registry_state(henv.host, s.group_token) == [henv.host.groups.STOPPING]
    assert s.stopping_since is not None and child.terminated == 1


def test_r9_d3_try_stopping_says_what_happened():
    """Ruling D3: reserved, already (a barrier this call did not make), busy, missing."""
    import threading
    from agent_comms.node_groups import Groups
    g = Groups()
    started, _ = g.start("/w", "t1", lambda: object())
    assert started
    assert g.try_stopping("t1") == g.RESERVED
    assert g.try_stopping("t1") == g.ALREADY
    assert g.try_stopping("nope") == g.MISSING
    held, release = threading.Event(), threading.Event()

    def hold():
        with g.lock:
            held.set()
            release.wait(5)
    threading.Thread(target=hold, daemon=True).start()
    assert held.wait(3)
    try:
        assert g.try_stopping("t1") == g.BUSY
    finally:
        release.set()


@pytest.mark.parametrize("process", ["alive", "gone"])
def test_r9_e2_a_registry_miss_is_not_a_verified_exit(henv, monkeypatch, process):
    """Ruling E2: the registry has no entry for the group (a miss): as main's `_stop` when `groups.stopping` finds no
    token, the stop goes on and the concrete group is probed: alive, it gets its SIGTERM and keeps its owner and kill
    clock; only a probe that confirms exit takes the gone path (no SIGTERM)."""
    s, child, n = steered(henv, monkeypatch)
    s.steer.cover_until = henv.clock() + 20
    child.emit(owned(n, text="N"))
    henv.host.tick()
    henv.clock.advance(21)
    henv.host.groups._gone(s.group_token)        # (the registry lost its entry)
    if process == "gone":
        child.code = 0
    henv.host._hold_cut(s, henv.clock(), henv.host._outbox, drain=False)
    assert s.stopping_since is not None and s.child is child, "the owner and the clock kept"
    assert child.terminated == (1 if process == "alive" else 0)


def test_r9_d2_a_held_turns_own_timeout_never_waits_on_the_registry(env):
    """Sol's round-9 blocker 2 (rulings D2, E3): the registry stays busy until an expired hold also reaches the
    held turn's own timeout; in that tick neither the hold's cut nor the turn timeout waits on the registry; a
    sibling's SIGKILL comes due while the registry is still held and goes when main would send it. Once the registry
    frees, the stop is committed once."""
    import threading
    from test_hosted_sessions import P2
    bs = []
    a, ca, sa, release = _expired_hold_with_the_registry_busy(
        env, before_the_stall=lambda: bs.append(_stopping_turn(env, project=P2)))
    b, cb, sb = bs[0]
    runner = env.node.sessions
    host = runner.host
    cb.kill = lambda: setattr(cb, "killed", cb.killed + 1)      # (its group outlives the SIGKILL: no reap yet)
    env.clock.advance(host.timeout_minutes * 60 + 1)        # (the held turn's own timeout too)
    runner._renew_contact(env.clock())      # (the node keeps its hub contact: no self-stop in this sequence)
    loop_done = threading.Event()

    def loop():
        with runner.lock:
            host.tick()
        loop_done.set()
    try:
        threading.Thread(target=loop, daemon=True).start()
        assert loop_done.wait(2), "neither the hold's cut nor the turn timeout waited on the registry"
        assert sa.held is not None and not ca.terminated and sa.stopping_since is None, "no stop committed"
        t, done, ident, err = _on_watchdog(runner)
        assert done.wait(2) and not err, err
        assert cb.killed, "the sibling's SIGKILL, due while the registry is held, went"
    finally:
        release.set()
    assert _registry_free(host)
    with runner.lock:
        host.tick()
        host.tick()
    assert ca.terminated == 1, "the stop committed once"


def test_r9_d4_while_the_reservation_waits_s_can_still_settle_n(env):
    """Ruling D4 (pin): no stop is committed while the reservation is unavailable, but the loop's drain still reads:
    S's own turn's evidence settles N (`next_turn`, S's own reply), once."""
    a, ca, sa, release = _expired_hold_with_the_registry_busy(env)
    runner = env.node.sessions
    host = runner.host
    s_uuid = sa.steer.uuid
    try:
        ca.emit(cinit(a, s_uuid), cresult(a, s_uuid, text="S's own"))
        out = []
        with runner.lock:
            out += host.tick() + host.tick()
        mine = list({e.ident: e for e in out if e.kind == "turn_ended" and e.session_id == a}.values())
        [end] = mine        # (the node's host keeps its outbox until the node takes it: one event, by identity)
        assert end.steer and end.steer["outcome"] == "next_turn" and end.steer["result"]["text"] == "S's own"
    finally:
        release.set()


def test_r9_d4_while_the_reservation_waits_the_contact_expiry_latches(env):
    """Ruling D4 (pin): the contact clock's expiry latches (for good) even though no stop could be committed."""
    a, ca, sa, release = _expired_hold_with_the_registry_busy(env)
    runner = env.node.sessions
    try:
        with runner.lock:
            runner.host.tick()
        assert sa.steer.expired and sa.held.decided is None
    finally:
        release.set()


def test_r9_c3_known_limit_the_stall_runs_on_through_the_held_turns_own_timeout(env):
    """Known limit C3 (round 9, D4): the registry stall runs on past the held turn's own timeout: no stop committed,
    the hold intact, no kill clock; once the registry frees, the stop is committed and N settles."""
    a, ca, sa, release = _expired_hold_with_the_registry_busy(env)
    runner = env.node.sessions
    host = runner.host
    try:
        env.clock.advance(host.timeout_minutes * 60 + 2 * host.kill_grace + 1)
        with runner.lock:
            out = host.tick() + host.tick()
        assert not [e for e in out if e.kind == "turn_ended" and e.session_id == a]
        assert sa.held is not None and sa.held.decided is None and sa.stopping_since is None
    finally:
        release.set()
    assert _registry_free(host)
    tick(env)
    ca.code = -9
    tick(env, times=3)
    assert hosted._get(env.board.conn, hosted._msg_key(a, 1))["state"] == "done"


# ---------------------------------------------------------------- review round 9, ruling F: off by default


def test_r9_f_claude_steering_is_off_by_default(henv, monkeypatch):
    """Ruling F, with the enabling PR's ruling S4 and S5(b) (the name kept for the record): the guard of the default
    and of the explicit-off configuration. The default is on; `claude_steer = false` turns it off: the host reads it
    from its config, and copies it onto each Claude adapter it opens. (Round 9's last assert could never fail.)"""
    from agent_comms.dispatch import SessionsConfig
    assert SessionsConfig().claude_steer is True and SessionsConfig.from_dict({}).claude_steer is True
    assert SessionsConfig.from_dict({"claude_steer": False}).claude_steer is False
    monkeypatch.setattr(_SessionHost, "claude_steer", _REAL_CLAUDE_STEER)
    monkeypatch.setattr(ClaudeSession, "steering_on", False)
    assert make_host(henv).claude_steer() is True, "a default config: on"
    assert open_session(henv).provider.steering_on is True, "copied onto the adapter at open"
    _claude_steering_off(monkeypatch, henv.config)
    host = make_host(henv)
    assert host.claude_steer() is False, "the key: off"
    henv.host = host
    assert open_session(henv, sid=SID2).provider.steering_on is False, "copied onto the adapter at open"


def test_r9_f_with_the_default_no_steer_mode_no_slot_no_hold(henv, monkeypatch):
    """Ruling F (the explicit-off configuration since the enabling PR; the name kept for the record): a verified
    2.1.330 incarnation reports no steer mode (no `steer`, no `held`), an offer makes no slot, and N's native end is
    published as on main (no hold can be created)."""
    _claude_steering_off(monkeypatch, henv.config)
    monkeypatch.setattr(claude, "VERIFIED", frozenset({V}))
    henv.host = make_host(henv, timeout_minutes=1)
    henv.host.steer_authority = lambda sess: True
    s, child = idle_host(henv)
    henv.host.send(SID, "a long turn", message_id="1")
    henv.host.tick()
    n = uuid_of(child)
    emit(henv, child, vinit(n))
    assert "steer" not in henv.host.modes(SID) and "held" not in henv.host.modes(SID)
    henv.host.steer_offer(SID, "1", S, TEXT, authorized=True)
    assert s.steer is None, "no slot"
    child.emit(owned(n, text="N's own"))
    [end] = ends(henv.host.tick())
    assert s.held is None and end.text == "N's own" and end.steer is None, "N's end as on main"


def test_r9_f_the_one_place_a_hold_is_made_is_guarded(henv, monkeypatch):
    """Ruling F (off at every layer): even if an adapter said it steers while the node's switch is off, the host never
    holds N's end: the one place a hold is created asks the switch."""
    s, child, n = steered(henv, monkeypatch)        # (on: a steer written whole into N)
    monkeypatch.setattr(_SessionHost, "claude_steer", lambda self: False)   # (the node's switch now off)
    assert s.provider.steering_on, "the adapter alone still says it steers (as no host would leave it)"
    child.emit(owned(n, text="N's own"))
    out = henv.host.tick()
    assert s.held is None, "never a hold with the switch off"
    assert [e.text for e in ends(out)] == ["N's own"]


def test_r9_f_with_the_default_a_steer_for_a_claude_turn_is_mains(env, monkeypatch):
    """Ruling F (the explicit-off configuration since the enabling PR; the name kept for the record): the node reports
    no Claude steer mode, and a steer for a running Claude turn is refused as on main e6b421f: the hub's `cli`
    refusal (the session's modes do not name `steer`)."""
    _claude_steering_off(monkeypatch, env.node.sessions.host.config)
    sid = start(env, "a long turn")["session_id"]
    tick(env)
    child = proc_of(env, sid)[1]
    n = written(child)[-1]["uuid"]
    child.emit(cinit(sid, n))
    tick(env, times=2)
    modes = hosted._session(env.board.conn, sid).get("modes") or []
    assert "steer" not in modes and "held" not in modes, modes
    r = env.human.post(f"/api/hosted-sessions/{sid}/messages", json={"message": TEXT, "mode": "steer"})
    assert hosted_steer.REFUSED["cli"] in r.text, r.text


def test_r8_c1_a_lost_claim_leaves_no_stopping_barrier(henv, monkeypatch):
    """Ruling C1 (pin): the claim is lost between the deadline and the attempt (the latch came): no STOPPING barrier
    is left on the checkout, nothing is claimed or signalled."""
    s, child, n = steered(henv, monkeypatch)
    s.steer.cover_until = henv.clock() + 20
    child.emit(owned(n, text="N"))
    henv.host.tick()
    assert s.held is not None
    henv.clock.advance(21)
    henv.host.steer_drain_hook = lambda sess: henv.host.steer_stopping()     # (the latch, during the drain)
    henv.host._hold_cut(s, henv.clock(), henv.host._outbox)
    henv.host.steer_drain_hook = None
    states = [st for g in henv.host.groups.entries.values() for tok, (st, cwd) in g.items() if tok == s.group_token]
    assert states == [henv.host.groups.LIVE] and not child.terminated and s.held.decided is None


def test_r8_c1_an_expired_hold_at_shutdown_gets_the_real_sigterm(env):
    """Ruling C1 (pin): an expired, still-held session at shutdown gets `stop_all`'s real SIGTERM, and its kill
    clock starts there."""
    a, ca, sa, release = _expired_hold_with_the_registry_busy(env)
    host = env.node.sessions.host
    release.set()
    assert _registry_free(host)
    stopped_at = []
    real_term = ca.terminate
    ca.terminate = lambda: (stopped_at.append(sa.stopping_since), real_term())[1]
    host.stop_all()
    assert ca.terminated == 1 and stopped_at and stopped_at[0] is not None


def test_r8_c3_known_limit_a_registry_stall_past_two_graces(env):
    """Known limit C3 (pinned): a registry stall past two grace periods: nothing published, the hold intact, no kill
    clock; a hub listing during the stall; once the registry frees, N's eventual settlement is taken by the hub."""
    a, ca, sa, release = _expired_hold_with_the_registry_busy(env)
    runner = env.node.sessions
    host = runner.host
    try:
        env.clock.advance(2 * host.kill_grace + 5)
        with runner.lock:
            out = host.tick()
        t, done, ident, err = _on_watchdog(runner)
        assert done.wait(3) and not err, err
        assert not [e for e in out + list(host._outbox) if e.kind == "turn_ended" and e.session_id == a]
        assert sa.held is not None and sa.held.decided is None and sa.stopping_since is None
        with runner.lock:
            runner._flush()
        runner.pass_once()          # (a listing from the hub during the stall)
        assert hosted._get(env.board.conn, hosted._msg_key(a, 1))["state"] != "done"
    finally:
        release.set()
    assert _registry_free(host)
    tick(env)
    ca.code = -9
    tick(env, times=3)
    m = hosted._get(env.board.conn, hosted._msg_key(a, 1))
    assert m["state"] == "done" and show(env, a)["reply"] == "N's reply", m


# ---------------------------------------------------------------- review round 10

CAUSES = ("hold_cut", "timeout", "cancel", "steer_bytes", "write")


def test_r10_h2_a_claude_attempt_keeps_its_path_after_the_adapter_stops_advertising(henv, monkeypatch):
    """Ruling H2 (with the enabling PR's predicate of the cause): the path is chosen by the attempt's recorded Claude
    identity (`slot.holds`) or the actual hold, not the adapter's advertised `holds`: a Claude attempt whose adapter
    lost verification (no longer advertising) still gets the reserved path for a cut of its steer bytes."""
    from agent_comms import session_host
    s, child, n = steered(henv, monkeypatch)
    s.provider._verified = False        # (verification lost: the adapter no longer says `holds`)
    assert not s.provider.holds and s.steer is not None and s.steer.holds
    assert henv.host._reserves(s, session_host.CAUSE_STEER_BYTES), "the attempt's own identity decides"
    assert not henv.host._reserves(s, session_host.CAUSE_TIMEOUT), "a live attempt's timeout is main's (S1)"


def test_r10_h2_a_codex_slot_never_takes_the_claude_path(henv, monkeypatch):
    """Ruling H2: a Codex slot never makes the predicate true, with the switch on."""
    from test_codex_steering import host_n
    host, s, child, _ = host_n(henv)
    host.steer_offer(SID, "a", "u1", "steer", authorized=True)
    host.tick()
    assert s.steer is not None and not s.steer.holds
    assert not any(host._reserves(s, c) for c in CAUSES)


def test_r10_g1_each_term_of_the_predicate_alone_makes_it_false(henv, monkeypatch):
    """Ruling G1: the predicate's three terms each make it false alone. Two of them are redundant in every reachable
    state (the adapter's `steering_on` is copied from the switch at open, and only a Claude adapter has it; every
    Claude slot records `holds`), so no scenario can tell them apart: this pins each directly, on a live Claude
    attempt, so the identity "switch off: the predicate is false" does not depend on the copy, and "a Codex slot:
    false" does not depend on the adapter."""
    s, child, n = steered(henv, monkeypatch)
    host = henv.host
    b = "steer_bytes"
    assert host._reserves(s, b) and s.held is None
    monkeypatch.setattr(_SessionHost, "claude_steer", lambda self: False)
    assert not host._reserves(s, b), "the switch, read on the host, alone"
    monkeypatch.setattr(_SessionHost, "claude_steer", lambda self: True)
    monkeypatch.setattr(s.provider, "steering_on", False, raising=False)
    assert not host._reserves(s, b), "the adapter's copy, alone"
    monkeypatch.setattr(s.provider, "steering_on", True, raising=False)
    monkeypatch.setattr(s.steer, "holds", False)
    assert not host._reserves(s, b), "the attempt's recorded Claude identity, alone (no hold)"
    monkeypatch.setattr(s.steer, "holds", True)
    assert host._reserves(s, b)


def test_r10_g3_a_restart_with_the_switch_off_restores_no_steering(henv, monkeypatch):
    """Ruling G3: a host restarted with the switch off, from a state saved while it was on with a held end: no slot,
    no hold and no queued steer bytes come back; the session's modes name no steering."""
    s, child, n = steered(henv, monkeypatch)
    s.steer.cover_until = henv.clock() + 20
    child.emit(owned(n, text="N"))
    henv.host.tick()
    assert s.held is not None
    henv.host._save_state()
    _claude_steering_off(monkeypatch, henv.config)
    host = make_host(henv, timeout_minutes=1)
    host.recover()
    s2 = host.open(SID, agent="claude", runtime="claude-code", project=PROJECT_OF(henv))
    assert s2.steer is None and s2.held is None and not s2.outbuf and not s2.provider.steering_on
    assert "steer" not in providers_modes(s2.provider) and "held" not in providers_modes(s2.provider)


def PROJECT_OF(henv):
    from test_session_host import PROJECT
    return PROJECT


def providers_modes(provider):
    from agent_comms import providers
    return providers.modes(provider)


def test_r10_g3_a_stale_hub_mode_meets_an_off_node(env, monkeypatch):
    """Ruling G3: the hub still lists a Claude steer mode for a session of an off node (stale): its offer is refused
    at slot creation (no slot, nothing written), and the node's next status drops the mode."""
    _claude_steering_off(monkeypatch, env.node.sessions.host.config)
    sid = start(env, "a long turn")["session_id"]
    tick(env)
    child = proc_of(env, sid)[1]
    n = written(child)[-1]["uuid"]
    child.emit(cinit(sid, n))
    tick(env, times=2)
    with hosted.db.write_tx(env.board.conn) as c:
        r = hosted._session(c, sid)
        hosted._put(c, hosted.SESSION_PREFIX + sid, r | {"modes": ["steer", "restart", "held"]}, "test", env.clock())
    r = env.human.post(f"/api/hosted-sessions/{sid}/messages", json={"message": TEXT, "mode": "steer"})
    tick(env, times=2)
    s = env.node.sessions.host.sessions[sid]
    assert s.steer is None, "no slot"
    assert not [x for x in written(child) if x.get("message", {}).get("content") == TEXT], "nothing written"
    child.emit(cresult(sid, n, text="N's reply"))
    tick(env, times=2)
    assert hosted._session(env.board.conn, sid)["modes"] == ["restart"], "the next status dropped the mode"


@pytest.mark.parametrize("direction", ["node_to_old_hub", "old_node_to_hub"])
def test_r10_g3_mixed_versions_write_no_claude_steer(env, monkeypatch, direction):
    """Ruling G3 (mixed versions), as MODELLED SHAPES (enabling PR, ruling S4: no old binary runs here): this head's
    node with the switch off against a listing shaped as d11ff99's (it names no `held`), and a report shaped as a
    d11ff99 node's (a Claude session's modes `restart` only) against this head's hub: no Claude steer is written
    either way."""
    _claude_steering_off(monkeypatch, env.node.sessions.host.config)
    sid = start(env, "a long turn")["session_id"]
    tick(env)
    child = proc_of(env, sid)[1]
    n = written(child)[-1]["uuid"]
    child.emit(cinit(sid, n))
    runner = env.node.sessions
    if direction == "node_to_old_hub":
        real = runner.client

        class D11ff99Listing:
            def request(self, method, path, **kw):
                r = real.request(method, path, **kw)
                if method == "GET" and path.startswith("/api/nodes/sessions"):
                    data = r.json()
                    if isinstance(data.get("features"), list):
                        data["features"] = [f for f in data["features"] if f != "held"]
                    return httpx.Response(r.status_code, json=data, request=r.request)
                return r
        runner.client = D11ff99Listing()
    else:
        r = env.laptop.post(f"/api/nodes/sessions/{sid}", json={"events": [], "state": "running", "modes": ["restart"],
                                                              "seq": 10 ** 18})
        assert r.status_code == 200, r.text
    tick(env, times=2)
    env.human.post(f"/api/hosted-sessions/{sid}/messages", json={"message": TEXT, "mode": "steer"})
    tick(env, times=2)
    assert not [x for x in written(child) if x.get("message", {}).get("content") == TEXT]
    assert env.node.sessions.host.sessions[sid].steer is None


RESULT_FIELDS = ["subtype", "is_error", "num_turns", "origin", "user_message_uuid", "user_message_uuids", "result",
                 "permission_denials", "terminal_reason", "stop_reason", "duration_ms", "total_cost_usd"]
INIT_FIELDS = ["claude_code_version", "capabilities", "permissionMode", "tools", "user_message_uuids", "origin",
               "user_message_uuid"]


@pytest.mark.parametrize("phase", ["n", "held", "s_turn"])
def test_r4_q_property_every_field_every_json_type_never_raises(henv, monkeypatch, phase):
    """Q5: for a result and an init frame, every field the adapter reads takes every JSON type, in each phase of
    an attempt: the adapter never raises, the session never ends through the host-error path, and a result whose
    N identity is intact (phase n) always ends N with N's own result."""
    for kind, fields in (("result", RESULT_FIELDS), ("init", INIT_FIELDS)):
        for field in fields:
            for value in JSON_VALUES:
                henv.spawner = __import__("test_dispatch").FakeSpawner(child=__import__("test_session_host")
                                                                        .FakeSessionChild)
                henv.procs = __import__("test_dispatch").FakeProcs(henv.spawner)
                s, child, n = steered(henv, monkeypatch)
                if phase != "n":
                    child.emit(owned(n, text="N"))
                    henv.host.tick()
                if phase == "s_turn":
                    child.emit(vinit(S))
                    henv.host.tick()
                base = owned(n, text="N's own") if (kind == "result" and phase == "n") else (
                    owned(S, text="S") if kind == "result" else vinit(S))
                frame = dict(base)
                frame[field] = value
                child.emit(frame)
                out = henv.host.tick()
                assert not s.ended, (kind, field, value, s.error)
                if kind == "result" and phase == "n" and field not in ("user_message_uuid", "user_message_uuids"):
                    # N's identity intact: N's own result, ended (S downgraded) or held (the measured shape)
                    mine = [e for e in out if e.kind in ("turn_ended", "held") and e.message_id == "1"]
                    want = "N's own" if field != "result" else (value if isinstance(value, str) else "")
                    assert mine and mine[0].text == want, (field, value, out)


def test_r4_q_at_retired_max_the_pair_is_not_advertised(henv, monkeypatch):
    """Q6: at RETIRED_MAX the incarnation no longer says `steer` and `held` (its next report drops them)."""
    s, child, n = steered(henv, monkeypatch)
    p = s.provider
    assert "held" in henv.host.modes(SID)
    for i in range(claude.RETIRED_MAX):
        p._retired.add(f"5e7e7e7e-0000-4000-8000-{i:012d}")
    assert "steer" not in henv.host.modes(SID) and "held" not in henv.host.modes(SID)



# ---------------------------------------------------------------- review round 4, ruling R: one fallback per report


def _claude_end_and_codex_steer_pending(env):
    """A: a Claude 2.1.330 session (modes steer, restart, held) whose ordinary turn ended; B: a Codex session with a
    steer listed into its turn and its end `in_turn`. Both ends are in the node's report queue, A's first."""
    from test_codex_steering import codex_turn_in_flight, post_steer
    from test_hosted_sessions import P3
    from agent_comms.session_host import HostEvent
    a = start(env, "an ordinary turn")["session_id"]
    tick(env)
    ca = proc_of(env, a)[1]
    ma = written(ca)[-1]["uuid"]
    ca.emit(cinit(a, ma))
    tick(env)
    b, cb = codex_turn_in_flight(env, project=P3)
    post_steer(env, b)
    tick(env)
    runner = env.node.sessions
    ca.emit(cresult(a, ma, text="A's reply"))
    with runner.lock:
        runner._emit(runner.host.tick())
        runner._emit([HostEvent(b, "turn_ended", "1", ok=True, text="B's reply", answered=True,
                                steer={"outcome": "in_turn", "why": "answered"})])
    return a, b, runner


def _posts(runner, refuse):
    """Every report POST the runner makes, through `refuse(path, body)` (an httpx status to answer, or None)."""
    real, seen = runner.client, []

    class Hub:
        def request(self, method, path, **kw):
            body = kw.get("json") or {}
            if method == "POST" and path.startswith("/api/nodes/sessions/") and "held" not in body:
                seen.append((path.rsplit("/", 1)[-1], json.loads(json.dumps(body))))
                code = refuse(path, body)
                if code is not None:
                    return httpx.Response(code, json={"detail": "refused"}, request=httpx.Request(method, "http://h" + path))
            return real.request(method, path, **kw)
    runner.client = Hub()
    return seen


def _step2_refuses(path, body):
    """main d11ff99's `modes` check (verbatim in effect: only steer and restart, at most two): 400."""
    modes = body.get("modes")
    if modes is not None and (len(modes) > 2 or any(x not in ("steer", "restart") for x in modes)):
        return 400
    return None


def test_r4_r_a_rollback_refusal_of_one_report_never_strips_anothers_outcome(env):
    """Sol's blocker (ruling R): in one flush, A's Claude end is refused by a step-2 hub (400, `held` in modes) and
    sent again without the pair; B's valid Codex `in_turn` after it still goes WITH its steer extension and settles
    `in_turn`. Feature knowledge is not changed by A's refusal."""
    a, b, runner = _claude_end_and_codex_steer_pending(env)
    features = set(runner.hub_features)
    seen = _posts(runner, _step2_refuses)
    with runner.lock:
        assert runner._flush()
    assert hosted._get(env.board.conn, hosted._msg_key(a, 1))["state"] == "done"
    st = hosted_steer.get(env.board.conn, b, 1)
    assert (st["outcome"], st["why"]) == ("in_turn", "answered"), st
    assert [x for x, _ in seen].count(b) == 1 and any("steer" in e for e in dict(seen)[b]["events"])
    assert runner.hub_features == features and runner.features_known, "no feature forgotten by a rejection"
    assert runner.last_sync == float("-inf"), "it asks for a fresh listing"


def test_r4_r_the_new_hub_strips_nothing(env):
    """Ruling R: against the new hub, both reports go once, as built."""
    a, b, runner = _claude_end_and_codex_steer_pending(env)
    seen = _posts(runner, lambda path, body: None)
    with runner.lock:
        assert runner._flush()
    assert sorted(x for x, _ in seen) == sorted([a, b])
    assert "held" in dict(seen)[a]["modes"] and any("steer" in e for e in dict(seen)[b]["events"])


def test_r4_r_one_ladder_reaches_a_payload_without_both_modes_and_extension(env):
    """Ruling R+: a hub that refuses both the steering modes (400) and any steer extension (422): ONE ladder, from
    the original report, ends at a payload without both, and the end is recorded."""
    sid, child, n, s_uuid = claude_steered(env)
    child.emit(cresult(sid, n, s_uuid, text="N and S"))
    runner = env.node.sessions
    with runner.lock:
        runner._emit(runner.host.tick())

    def refuse(path, body):
        if any("steer" in e for e in body.get("events", [])):
            return 422
        return _step2_refuses(path, body)
    seen = _posts(runner, refuse)
    with runner.lock:
        assert runner._flush()
    mine = [body for x, body in seen if x == sid]
    assert len(mine) == 3, mine
    assert "held" not in mine[-1].get("modes", []) and not any("steer" in e for e in mine[-1]["events"])
    assert hosted._get(env.board.conn, hosted._msg_key(sid, 1))["state"] == "done"


def test_r4_r_a_pre_steering_responder_gets_the_plain_rung(env):
    """Ruling R: a responder from before steering (it refuses any steer extension, 422): a Codex steer end goes by
    rung (iii), main's plain retry; N's end is recorded."""
    from test_codex_steering import codex_turn_in_flight, post_steer
    from test_hosted_sessions import P3
    from agent_comms.session_host import HostEvent
    b, cb = codex_turn_in_flight(env, project=P3)
    post_steer(env, b)
    tick(env)
    runner = env.node.sessions
    with runner.lock:
        runner._emit([HostEvent(b, "turn_ended", "1", ok=True, text="B's reply", answered=True,
                                steer={"outcome": "in_turn", "why": "answered"})])
    seen = _posts(runner, lambda path, body: 422 if any("steer" in e for e in body.get("events", [])) else None)
    with runner.lock:
        assert runner._flush()
    mine = [body for x, body in seen if x == b]
    assert len(mine) == 2 and not any("steer" in e for e in mine[-1]["events"])
    assert hosted._get(env.board.conn, hosted._msg_key(b, 1))["state"] == "done"


def test_r4_r_the_write_gate_asks_the_pair(env):
    """Ruling R3 (a mutation row): the write gate of a Claude steer asks the pair, never `held` alone: a listing that
    names `held` without `steer` writes nothing of it."""
    sid, child, n, s_uuid = claude_steered(env)
    runner = env.node.sessions
    s = runner.host.sessions[sid]
    assert runner.host.holds(sid) and runner.features_known and runner._steer_capable(s)
    runner.hub_features = set(runner.hub_features) - {"steer"}
    assert "held" in runner.hub_features and not runner._steer_capable(s)


def test_r4_r_the_checkpoint_gates_ask_the_pair(env):
    """Ruling R3 (a mutation row): the checkpoint is queued, and posted, only for a listing naming the pair, never
    `held` alone."""
    from agent_comms.session_host import HostEvent
    sid, child, n, s_uuid = claude_steered(env)
    runner = env.node.sessions
    runner.hub_features = set(runner.hub_features) - {"steer"}
    assert "held" in runner.hub_features and runner.features_known
    runner._checkpoint(HostEvent(sid, "held", "1", ok=True, text="N's reply", answered=True))
    assert sid not in runner.checkpoints, "no checkpoint queued for `held` alone"


def test_r4_r_a_retrys_success_bookkeeping_is_a_first_trys(env):
    """Ruling R+, V2: the state after a report that went at the second rung is the state after a first-try
    success: the report left the queue, its status recorded as reported, its refusal mark cleared, its ack taken."""
    a, b, runner = _claude_end_and_codex_steer_pending(env)
    _posts(runner, _step2_refuses)
    with runner.lock:
        assert runner._flush()
    assert not [x for x, _ in runner.pending if x in (a, b)]
    assert runner.reported.get(a) == runner.status.get(a) and a not in runner.refused
    assert a in runner.report_ack and b in runner.report_ack



# ---------------------------------------------------------------- review round 4, rulings S, T, U, V


def _ordinary_turn_in_flight(env, project=None):
    sid = start(env, "an ordinary turn", **({"project": project} if project else {}))["session_id"]
    tick(env)
    child = proc_of(env, sid)[1]
    m = written(child)[-1]["uuid"]
    child.emit(cinit(sid, m))
    tick(env)
    return sid, child, m


def test_r4_s_a_buffered_native_end_never_goes_behind_lost(env):
    """Sol's blocker (ruling S): A's native end is published, the tick raises; the watchdog self-stops before the
    next pass; the next pass reports A's native end (not LOST), exactly once."""
    sid, child, m = _ordinary_turn_in_flight(env)
    runner = env.node.sessions
    child.emit(cresult(sid, m, text="A's own"))
    restore = _tick_raising_after(runner.host)
    with pytest.raises(RuntimeError):
        runner.pass_once()
    restore()
    env.clock.advance(runner.self_stop_seconds + 1)
    runner.enforce_deadlines()          # the watchdog's self-stop
    assert all(e.session_id != sid or e.kind != "turn_ended" for e in runner.watched), "never moved behind LOST"
    runner._renew_contact(env.clock())
    tick(env, times=2)
    msg = hosted._get(env.board.conn, hosted._msg_key(sid, 1))
    assert msg["state"] == "done" and show(env, sid)["reply"] == "A's own", msg


def test_r4_s_a_self_stop_that_raises_keeps_its_own_events(env):
    """Ruling S with V (a mutation row): the self-stop publishes one session's failure, then a BaseException escapes
    on the next session: what it published stays in the host's outbox, after what was already there."""
    from agent_comms.session_host import HostEvent
    from test_hosted_sessions import P2
    sid1, c1, m1 = _ordinary_turn_in_flight(env)
    sid2, c2, m2 = _ordinary_turn_in_flight(env, project=P2)
    host = env.node.sessions.host
    host._outbox.append(HostEvent(sid1, "delivered", "1"))      # (what was there before)
    real, calls = host._stop, []

    class Abort(BaseException):
        pass

    def stop(sess, now, **kw):
        calls.append(sess.session_id)
        if len(calls) == 2:
            raise Abort()
        return real(sess, now, **kw)
    host._stop = stop
    with pytest.raises(Abort):
        host.stop_turns("the node lost the hub", own=True)
    host._stop = real
    kinds = [(e.session_id, e.kind) for e in host._outbox]
    assert kinds[0] == (sid1, "delivered") and (calls[0], "turn_ended") in kinds[1:], kinds


@pytest.mark.parametrize("path", ["cancel", "close", "fatal", "host_error", "reap_with_survivors",
                                  "interrupted_timeout", "broken_pipe"])
def test_r4_t_after_the_latch_no_single_session_path_signals_or_decides(henv, monkeypatch, path):
    """Sol's blocker (ruling T): after the global latch, a cancel, a close, a fatal frame, a host error or a reap
    with group survivors of a steering session sends no signal and decides no steer: the bulk stop owns it and
    decides once."""
    s, child, n = steered(henv, monkeypatch)
    henv.host.steer_stopping()
    if path == "cancel":
        henv.host.cancel(SID, "1", "cancelled")
    elif path == "close":
        henv.host.close(SID, "closed")
    elif path == "fatal":
        child.emit(vinit(n) | {"session_id": "another-session"})
        henv.host.tick()
    elif path == "host_error":
        def broken():
            raise OSError("read failed")
        child.read_lines = broken
        henv.host.tick()
        child.read_lines = lambda: []
    elif path == "reap_with_survivors":
        child.code = 0
        henv.groups.add(child.pid)
        henv.host.tick()
    elif path == "interrupted_timeout":
        # (a Claude session's own interrupt is never sent: `interrupt()` is None; the branch is reached only if
        # `interrupted_at` is set, as a Codex session's is)
        s.interrupted_at = henv.clock() - henv.host.kill_grace - 1
        henv.host.tick()
    else:
        s.outbuf = bytearray(b"x")
        child.broken = True
        henv.host.tick()
        child.broken = False
    assert not child.terminated, "no SIGTERM after the latch"
    assert s.steer is not None and s.steer.outcome is None, "no steer decided after the latch"
    out = henv.host.stop_all()
    assert len(ends(out)) == 1 and ends(out)[0].steer == {"outcome": "unconfirmed", "why": "turn_cut"}


def test_r4_t_a_reap_whose_drain_meets_the_latch_decides_nothing(henv, monkeypatch):
    """Ruling T (a mutation row): the process went, leaving members of its group; the latch comes during the reap's
    drain (after the first check): the claim refuses, nothing is decided or signalled, the group is registered as
    stopping; the bulk stop decides once."""
    s, child, n = steered(henv, monkeypatch)
    henv.host.steer_drain_hook = lambda sess: henv.host.steer_stopping()
    child.code = 0
    henv.groups.add(child.pid)
    out = henv.host.tick()
    henv.host.steer_drain_hook = None
    assert henv.host.steer_stopped and not ends(out) and not child.terminated
    # (review round 5, ruling Z2: registered as stopping for the bulk stop, `parked`; no SIGTERM time is invented)
    assert s.steer is not None and s.steer.outcome is None and s.parked and s.stopping_since is None
    [end] = ends(henv.host.stop_all())
    assert end.steer == {"outcome": "unconfirmed", "why": "turn_cut"}


def test_r4_t_a_host_error_of_a_held_end_after_the_latch_decides_nothing(henv, monkeypatch):
    """Ruling T (a mutation row): N's end is held; after the latch a read raises: the host error neither decides S
    nor publishes N's end (`_publish_now`); the bulk stop decides once."""
    s, child, n = steered(henv, monkeypatch)
    s.steer.cover_until = henv.clock() + 20
    child.emit(owned(n, text="N's own"))
    henv.host.tick()
    assert s.held is not None and s.held.decided is None
    henv.host.steer_stopping()

    def broken():
        raise OSError("read failed")
    child.read_lines = broken
    out = henv.host.tick()
    child.read_lines = lambda: []
    assert not ends(out) and s.held is not None and s.held.decided is None and not child.terminated
    [end] = ends(henv.host.stop_all())
    assert end.text == "N's own" and end.steer == {"outcome": "unconfirmed", "why": "turn_cut"}


def test_r4_t_the_cut_whose_drain_meets_the_latch_decides_nothing(henv, monkeypatch):
    """Ruling T (the partial-write cut): the latch comes during the cut's drain: the cut decides nothing (the claim
    refuses); the bulk stop owns N."""
    s, child, n = steered(henv, monkeypatch)
    henv.host.steer_drain_hook = lambda sess: henv.host.steer_stopping()
    s.steer.mark, s.steer.sent = (0, 10), 5      # (a frame taken in part)
    henv.host._steer_cut(s, henv.clock(), henv.host._outbox)
    assert not ends(henv.host._outbox) and not child.terminated


def test_r4_t_a_latch_between_the_first_check_and_the_expiry_changes_no_state(henv, monkeypatch):
    """Ruling T2: the latch arrives after `_hold_cut`'s first check and before the expiry: no state changes (the
    check under the lock is the one that matters)."""
    s, child, n = steered(henv, monkeypatch)
    s.steer.cover_until = henv.clock() + 20
    child.emit(owned(n, text="N"))
    henv.host.tick()
    host = henv.host

    real = host._steer_stop_lock

    class Arriving:
        """The cutoff lock, as another thread's `steer_stopping` would find it: the latch lands just before the
        `_hold_cut` takes it (after its first check)."""
        armed = True

        def __enter__(self):
            if Arriving.armed:
                Arriving.armed = False
                host._steer_stop_lock = real
                host.steer_stopping()
                host._steer_stop_lock = self
            real.acquire()
            return self

        def __exit__(self, *a):
            real.release()
    henv.clock.advance(21)
    host._steer_stop_lock = Arriving()
    try:
        host._hold_cut(s, henv.clock(), host._outbox)
    finally:
        host._steer_stop_lock = real
    assert host.steer_stopped and not s.steer.expired and not child.terminated


def test_r4_t_nothing_logs_or_saves_under_the_cutoff_lock(henv, monkeypatch):
    """Ruling T3: the pre-hold timeout (a live attempt's N times out: `_steer_end` decides S) logs and saves nothing
    while the cutoff lock is held."""
    from agent_comms import session_host
    s, child, n = steered(henv, monkeypatch)
    lock = henv.host._steer_stop_lock
    held_while = []
    real_log = session_host.log.log

    def probe(*a, **k):
        held_while.append(lock._is_owned())
        return real_log(*a, **k)
    monkeypatch.setattr(session_host.log, "log", probe)
    for name in ("info", "warning", "error", "exception"):
        real = getattr(session_host.log, name)
        monkeypatch.setattr(session_host.log, name,
                            lambda *a, _r=real, **k: (held_while.append(lock._is_owned()), _r(*a, **k))[1])
    real_save = henv.host._save_state
    henv.host._save_state = lambda: (held_while.append(lock._is_owned()), real_save())[1]
    henv.clock.advance(61)
    out = henv.host.tick() + henv.host.tick()
    assert ends(out) and held_while and not any(held_while), held_while


def test_r4_t_stop_all_survives_a_fault_in_every_step_of_its_traversal(henv, monkeypatch):
    """Ruling T4: one session's kill, read and close raising skip no other session's cleanup; the final save runs."""
    (s1, c1), (s2, c2) = _held_pair(henv, monkeypatch)
    c1.exits_on_terminate = False

    def bad():
        raise OSError("failed")
    c1.kill = bad
    c1.read_lines = bad
    saved = []
    real_save = henv.host._save_state
    henv.host._save_state = lambda: (saved.append(1), real_save())
    henv.host.stop_all()
    # (review round 5, ruling Z: s1's kill failed and its group may still be there: it stays owned and recorded)
    assert s2.child is None and s1.child is c1 and saved


def test_r4_t_stop_all_saves_even_when_its_traversal_raises(henv, monkeypatch):
    """Ruling T4 (a mutation row): the wait for the groups raises (its sleep): the final save still runs."""
    (s1, c1), (s2, c2) = _held_pair(henv, monkeypatch)
    c1.exits_on_terminate = False

    def bad(_):
        raise OSError("failed")
    henv.host.sleep = bad
    saved = []
    real_save = henv.host._save_state
    henv.host._save_state = lambda: (saved.append(1), real_save())
    with pytest.raises(OSError):
        henv.host.stop_all()
    assert saved


def test_r4_t_a_stop_claimed_before_the_latch_may_signal_after_it(henv, monkeypatch):
    """Ruling T+ (the ordering limit), with the enabling PR's ruling S2: the reserved stop's SIGTERM and its decision
    are one critical section of the cutoff lock, so a latch that comes during the signal waits for both; the bulk
    stop then recognises the claim (N's end decided once, by the claim)."""
    import threading
    s, child, n = steered(henv, monkeypatch)
    s.steer.cover_until = henv.clock() + 20
    child.emit(owned(n, text="N"))
    henv.host.tick()
    real_stop = henv.host._stop
    latched = threading.Event()

    def stop(sess, now, at_signal=False, **kw):
        t = threading.Thread(target=lambda: (henv.host.steer_stopping(), latched.set()), daemon=True)
        t.start()           # the latch comes during the signal ...
        assert not latched.wait(0.2), "... and waits for the reserved stop's critical section"
        return real_stop(sess, now, at_signal=at_signal, **kw)
    henv.host._stop = stop
    henv.clock.advance(21)
    henv.host.steer_deadlines()
    assert latched.wait(3)
    assert child.terminated == 1 and s.held is not None and s.held.decided is not None
    decided = dict(s.held.decided)
    henv.host._stop = real_stop
    [end] = ends(henv.host.stop_all())
    assert end.steer == decided, "the bulk stop made no competing decision"


def test_r4_t_the_watchdog_runs_its_deadlines_whatever_the_save_does(env):
    """Ruling T5: a state save that always raises, then one that stalls: the watchdog's pass still reaches the due
    SIGKILLs, and returns promptly (nothing on its path saves)."""
    import threading
    import time as _time
    sid, child, m = _ordinary_turn_in_flight(env)
    runner = env.node.sessions
    host = runner.host
    reached = []
    real_kill_due = host.kill_due
    host.kill_due = lambda: (reached.append(1), real_kill_due())[1]

    def raises():
        raise OSError("the disk is full")
    host._save_state = raises
    env.clock.advance(runner.self_stop_seconds + 1)
    runner.enforce_deadlines()
    assert reached, "the SIGKILLs were reached"
    stall = threading.Event()
    host._save_state = lambda: stall.wait(10)
    done = threading.Event()
    t = threading.Thread(target=lambda: (runner.enforce_deadlines(), done.set()))
    t0 = _time.monotonic()
    t.start()
    assert done.wait(3) and _time.monotonic() - t0 < 3, "a stalled save never holds the watchdog"
    stall.set()
    t.join(5)


@pytest.mark.parametrize("what", ["cut", "fence"])
def test_r4_t_the_watchdogs_boundary_saves_nothing(henv, monkeypatch, what):
    """Ruling T5 (a mutation row): the watchdog's boundary (`steer_deadlines`) cuts a frame taken in part, or
    publishes a fenced hold whose deadline passed: no save on that path; the loop's next tick saves."""
    s, child, n = steered(henv, monkeypatch, grace=10)
    if what == "cut":
        s.steer.mark, s.steer.sent = (0, 10), 5      # (a frame taken in part)
        s.steer.cover_until = henv.clock() + 1
        henv.clock.advance(2)
    else:
        child.exits_on_terminate = False
        child.emit(owned(n, text="N"))
        henv.host.tick()
        henv.groups.add(child.pid)
        henv.host.close(SID, "closed")
        henv.clock.advance(21)
    saves = []
    real_save = henv.host._save_state
    henv.host._save_state = lambda: saves.append(1)
    out = henv.host.steer_deadlines()
    assert ends(out) and child.terminated and not saves, "nothing saved on the watchdog's path"
    henv.host.tick()
    assert saves, "the loop's next tick saves"
    henv.host._save_state = real_save


def test_r4_t_a_failing_self_stop_keeps_no_later_watchdog_step_from_running(env):
    """Ruling T5 (a mutation row): the self-stop raises: the due SIGKILLs and the steer deadlines still run."""
    sid, child, m = _ordinary_turn_in_flight(env)
    runner = env.node.sessions
    host = runner.host
    reached = []

    def fails(*a, **k):
        raise OSError("the self-stop failed")
    host.stop_turns = fails
    real_kill_due, real_deadlines = host.kill_due, host.steer_deadlines
    host.kill_due = lambda: (reached.append("kill_due"), real_kill_due())[1]
    host.steer_deadlines = lambda **kw: (reached.append("steer_deadlines"), real_deadlines(**kw))[1]
    env.clock.advance(runner.self_stop_seconds + 1)
    runner.enforce_deadlines()
    assert reached == ["kill_due", "steer_deadlines"]


def test_r4_t_the_watchdogs_self_stop_saves_nothing_on_its_thread(env):
    """Ruling T5 (a mutation row): the self-stop of a turn in flight, with a save that stalls: the watchdog's pass
    makes no save and returns promptly; the loop's next tick saves."""
    import threading
    import time as _time
    sid, child, m = _ordinary_turn_in_flight(env)
    runner = env.node.sessions
    host = runner.host
    stall, saves = threading.Event(), []
    real_save = host._save_state
    host._save_state = lambda: (saves.append(threading.get_ident()), stall.wait(10))
    env.clock.advance(runner.self_stop_seconds + 1)
    done = threading.Event()
    t = threading.Thread(target=lambda: (runner.enforce_deadlines(), done.set()))
    t0 = _time.monotonic()
    t.start()
    assert done.wait(3) and _time.monotonic() - t0 < 3 and not saves, "no save on the watchdog's thread"
    assert any(e.session_id == sid for e in runner.watched), "the turn was stopped"
    stall.set()
    t.join(5)
    host._save_state = lambda: saves.append("loop")
    host.tick()
    assert saves and saves[0] == "loop", "the loop's next tick saves"
    host._save_state = real_save


def test_r4_v_the_watchdog_never_waits_on_a_stalled_post(env):
    """Ruling V3: a report POST stalls on the loop's flush (outside the runner lock); the watchdog's pass runs and
    returns promptly meanwhile."""
    import threading
    import time as _time
    sid, child, m = _ordinary_turn_in_flight(env)
    runner = env.node.sessions
    real = runner.client.request
    release, entered = threading.Event(), threading.Event()

    def request(method, path, *a, **kw):
        if method == "POST":
            entered.set()
            release.wait(10)
        return real(method, path, *a, **kw)
    runner.client.request = request
    child.emit(cresult(sid, m, text="A"))
    with runner.lock:
        runner._emit(runner.host.tick())
    t = threading.Thread(target=runner._flush)
    t.start()
    assert entered.wait(3)
    t0 = _time.monotonic()
    runner.enforce_deadlines()
    assert _time.monotonic() - t0 < 2
    release.set()
    t.join(10)


def test_r4_v_the_outbox_to_dispatch_handoff_loses_and_duplicates_nothing(env):
    """Ruling V1: the dispatcher raises on the first event it is handed from the retained outbox: that event and
    the rest stay recoverable; the next pass delivers each exactly once."""
    sid, child, m = _ordinary_turn_in_flight(env)
    runner = env.node.sessions
    child.emit(cresult(sid, m, text="A's own"))
    restore = _tick_raising_after(runner.host)
    with pytest.raises(RuntimeError):
        runner.pass_once()
    restore()
    real = runner._put
    calls = []

    def flaky(event):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("the dispatcher failed")
        return real(event)
    runner._put = flaky
    with pytest.raises(RuntimeError):
        runner.pass_once()
    runner._put = real
    tick(env, times=2)
    ends_queued = [e for x, e in runner.pending if x == sid and e.get("kind") == "turn_ended"]
    assert not ends_queued
    msg = hosted._get(env.board.conn, hosted._msg_key(sid, 1))
    assert msg["state"] == "done" and show(env, sid)["reply"] == "A's own"


def test_r4_v_the_watched_handoff_loses_and_duplicates_nothing(env):
    """Ruling V1 (review round 5, ruling X: the handoff is put, then removal from the source): the watchdog's steer
    list handed to the dispatcher, which raises on its first event: the event stays in its source, in its lane
    (`watched_steer`), and is delivered once on the next pass."""
    from agent_comms.session_host import HostEvent
    sid, child, m = _ordinary_turn_in_flight(env)
    runner = env.node.sessions
    runner.watched_steer = [HostEvent(sid, "turn_ended", "1", ok=True, text="A's own", answered=True,
                                      ident=(sid, 1, 0))]
    real = runner._put
    calls = []

    def flaky(event):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("the dispatcher failed")
        return real(event)
    runner._put = flaky
    with pytest.raises(RuntimeError):
        runner.pass_once()
    runner._put = real
    assert [e.text for e in runner.watched_steer] == ["A's own"] and not runner.host._outbox
    tick(env)
    assert len([e for x, e in runner.pending if x == sid]) <= 1
    assert hosted._get(env.board.conn, hosted._msg_key(sid, 1))["state"] == "done"


def test_r4_v_the_emit_handoff_of_watched_loses_and_duplicates_nothing(env):
    """Ruling V1 (the late list; review round 5, ruling X): the self-stop's `watched` list handed to the dispatcher
    by `_emit`, which raises on its first event: the event stays in `watched` (its lane) and is delivered once on
    the next pass."""
    from agent_comms.session_host import HostEvent
    sid, child, m = _ordinary_turn_in_flight(env)
    runner = env.node.sessions
    runner.watched = [HostEvent(sid, "turn_ended", "1", ok=True, text="A's own", answered=True, ident=(sid, 1, 0))]
    real = runner._put
    calls = []

    def flaky(event):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("the dispatcher failed")
        return real(event)
    runner._put = flaky
    with pytest.raises(RuntimeError):
        with runner.lock:
            runner._emit([])
    runner._put = real
    assert [e.text for e in runner.watched] == ["A's own"] and not runner.host._outbox
    tick(env)
    assert len([e for x, e in runner.pending if x == sid]) <= 1
    assert hosted._get(env.board.conn, hosted._msg_key(sid, 1))["state"] == "done"


def test_r4_u_a_late_checkpoint_post_replaces_nothing(env):
    """Ruling U+: a checkpoint POST that lands late (after an accepted checkpoint, or after N's terminal end) is
    refused: it replaces neither."""
    sid, child, n, s_uuid = claude_steered(env)
    child.emit(cresult(sid, n, text="N's reply"))
    tick(env, times=2)
    first = hosted._get(env.board.conn, hosted._msg_key(sid, 1))["held"]
    late = {"message": 1, "ok": False, "detail": "late", "denials": [], "meta": {}, "text": "late", "answered": True}
    r = env.laptop.post(f"/api/nodes/sessions/{sid}", json={"held": late})
    assert r.status_code == 409 and hosted._get(env.board.conn, hosted._msg_key(sid, 1))["held"] == first
    child.emit(cinit(sid, s_uuid), cresult(sid, s_uuid, text="S"))
    tick(env, times=3)
    before = hosted._get(env.board.conn, hosted._msg_key(sid, 1))
    r = env.laptop.post(f"/api/nodes/sessions/{sid}", json={"held": late})
    assert r.status_code == 409 and hosted._get(env.board.conn, hosted._msg_key(sid, 1)) == before


# ---------------------------------------------------------------- gate M8's real frames (review round 1, ruling E)

M8_DIR = __import__("pathlib").Path(__file__).parent / "fixtures" / "m8_2.1.330"
M8_N, M8_S = "00000000-0000-4000-8000-00000000000e", "00000000-0000-4000-8000-00000000005e"
M8_SID = "00000000-0000-4000-8000-00000000a8a8"


def _m8_runs():
    names = [x for x in (M8_DIR / "MANIFEST").read_text().split("\n") if x and not x.startswith("#")]
    # S is written into N in every scenario but cli_take and cli_late (there the probe wrote S after N had ended,
    # into a turn the CLI started: no steer can be, since one is written only while N is in flight)
    return [x for x in names if not x.startswith(("cli_take", "cli_late"))]


def test_m8_the_manifest_is_39_runs_of_fourteen_scenarios():
    """The run accounting (ruling E): 36 probe runs, 3 of each of 10 scenarios and 2 of each race (at 3, 5 and 7 s),
    and 3 runs of S into a RESUMED process through the node (`resumed`, the e2e's Z10 with the real CLI)."""
    names = [x for x in (M8_DIR / "MANIFEST").read_text().split("\n") if x and not x.startswith("#")]
    scenarios = sorted({x.rsplit("-", 1)[0] for x in names})
    assert len(names) == 39 and len(scenarios) == 14, (len(names), scenarios)
    assert sum(x.startswith("resumed-") for x in names) == 3
    assert all(sum(1 for x in names if x.rsplit("-", 1)[0] == sc) in (2, 3) for sc in scenarios)
    assert len(_m8_runs()) == 33


@pytest.mark.parametrize("run", _m8_runs())
def test_m8_real_frames_through_the_host_give_the_outcome_their_own_frames_prove(henv, monkeypatch, run):
    """Each M8 run's real 2.1.330 frames (sanitised), replayed through the host and the adapter as shipped (VERIFIED,
    STEER_MODES), S offered and written where the probe wrote it: the outcome is the one the frames themselves prove
    under ruling A's exact predicates: `in_turn` where N's result lists exactly [N, S]; else `next_turn`, with S's
    own result's `ok` and its reply's probe word, where a result lists exactly [S] after an `init` listing exactly
    [S]."""
    lines = [json.loads(x) for x in (M8_DIR / f"{run}.jsonl").read_text().splitlines()]
    henv.host = make_host(henv, timeout_minutes=5)
    henv.host.steer_authority = lambda sess: True
    s = open_session(henv)
    henv.host.send(SID, "N", message_id="1")
    henv.host.tick()
    child = henv.spawner.children[-1]
    n = uuid_of(child)

    def real(f):
        text = json.dumps(f).replace(M8_N, n).replace(M8_S, S).replace(M8_SID, SID)
        return json.loads(text)
    out = []
    for f in lines:
        if f.get("probe") == "S":
            out += henv.host.tick()
            henv.host.steer_offer(SID, "1", S, TEXT, authorized=True)
            out += henv.host.tick()
            assert json.loads(child.stdin.split(b"\n")[-2]).get("uuid") == S, run
            continue
        if "probe" in f:
            continue
        child.emit(real(f))
        out += henv.host.tick()
    [end] = ends(out)
    n_res = next(f for f in lines if f.get("type") == "result" and M8_N in (f.get("user_message_uuids") or []))
    s_res = next((f for f in lines if f.get("type") == "result" and f.get("user_message_uuids") == [M8_S]), None)
    if n_res["user_message_uuids"] == [M8_N, M8_S]:
        assert end.steer == {"outcome": "in_turn", "why": "answered"}, (run, end.steer)
    else:
        assert s_res is not None, run
        ok = s_res["subtype"] == "success" and s_res["is_error"] is False
        assert end.steer["outcome"] == "next_turn" and end.steer["result"]["ok"] is ok, (run, end.steer)
        assert end.steer["result"]["text"] == s_res["result"], (run, end.steer)
    assert end.text == n_res["result"], run
