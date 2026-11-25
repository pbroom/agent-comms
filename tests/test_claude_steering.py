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


def claude_steered(env):
    """A Claude root, N (message 1) in flight in its owned bracket, a human steer accepted, offered and written."""
    sid = start(env, "a long turn")["session_id"]
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
    Claude, on main at 2b84f42); the one named change is `modes` (a verified 2.1.330 process says `steer`)."""
    import copy
    ours, our_modes = _no_steer_run(henv, monkeypatch, caplog, seed, copy.deepcopy(claude.STEER_MODES))
    base, base_modes = _no_steer_run(henv, monkeypatch, caplog, seed, {})
    assert ours == base
    assert all("steer" not in m for m in base_modes)
    assert [tuple(x for x in m if x != "steer") for m in our_modes] == [tuple(m) for m in base_modes]


# ---------------------------------------------------------------- the model: seeded sequences, an independent reference


class _Model:
    """This test's own reading of part D's tables for one written steer (independent of the adapter and the host):
    N's bracket open and owned, S written whole, T = 60 s. It says what N's one end must carry."""

    def __init__(self, t0):
        self.phase, self.comp, self.verified, self.amb = "n", False, True, False
        self.bracket, self.s_turn, self.since, self.t0 = None, False, t0, t0
        self.end = None         # (outcome, why, native text or None, S's result ok or None)
        self.native = None

    def finish(self, outcome, why, s_ok=None):
        if self.end is None:
            self.end = (outcome, why, self.native, s_ok)

    def finalize(self):
        self.finish(*(("next_turn", "own_turn_cut", False) if self.s_turn else ("unconfirmed", "turn_cut")))

    def frame(self, kind):
        if self.end is not None:
            return
        if self.phase == "n":
            if kind == "n_res_ns":
                self.native = "N:ns"
                self.finish("in_turn", "answered")
            elif kind == "n_res_n":
                self.native = "N:n"
                if self.verified:
                    self.phase, self.since = "h", self.now
                else:
                    self.phase = "lost"
                    self.finalize()
            elif kind in ("s_res", "cli_takes_s"):
                if self.verified and not self.comp:
                    self.native = "S:" + kind
                    self.finish("unconfirmed", "s_only")
                else:
                    self.verified, self.amb = False, True
            elif kind in ("compete", "s_init", "cli_open"):
                if self.verified:
                    self.comp = True
            elif kind in ("plain", "error"):
                self.verified, self.amb = False, True
            return
        # the hold (verified). A result that lists S is a bracket's terminal result consuming S (a late one listing N
        # and S too); one listing only N (already settled) is nobody's.
        if kind in ("s_res", "cli_takes_s", "n_res_ns"):
            if self.bracket == "s":
                self.native_s = True
                self.finish("next_turn", "own_turn", True)
            else:
                self.finish("unconfirmed", "consumed")
        elif kind == "s_init":
            if self.bracket is None:
                self.bracket, self.s_turn, self.since = "s", True, self.now
        elif kind in ("cli_open", "compete"):
            if self.bracket is None:
                self.bracket = "cli"
        elif kind == "cli_end":
            if self.bracket == "cli":
                self.bracket = None
        elif kind == "plain":
            if self.bracket == "s":
                self.finish("next_turn", "own_turn", True)
            else:
                self.finalize()
        elif kind == "error":
            if self.bracket != "s":
                self.finalize()

    def host_end(self, how):
        """A host-made end of N: in the hold, the finalizer; before N's native end, N fails and S `turn_cut`."""
        if self.end is not None:
            return
        if self.phase == "n":
            self.finish("unconfirmed", "turn_cut")
        else:
            self.finalize()


FRAMES = {"n_res_ns": lambda n: owned(n, S, text="N:ns"), "n_res_n": lambda n: owned(n, text="N:n"),
          "s_res": lambda n: owned(S, text="S:s_res"),
          "cli_takes_s": lambda n: owned(S, text="S:cli_takes_s") | {"origin": NOTIFY},
          "s_init": lambda n: vinit(S), "cli_open": lambda n: vinit(), "cli_end": lambda n: foreign(),
          "compete": lambda n: vinit("0be7c0de-0000-4000-8000-000000000000"), "plain": lambda n: plain(text="?"),
          "error": lambda n: result(subtype="error_during_execution", is_error=True, text=""),
          "note": lambda n: nonturn()}


@pytest.mark.parametrize("seed", range(200))
def test_model_claude_steer_sequences_against_an_independent_reference(henv, monkeypatch, seed, request):
    """Seeded sequences of every frame part D's tables name (and exits, cancels, closes, stops of every turn and the
    clock) after a written steer: the host publishes N's end exactly once, with the outcome, the why, N's native
    reply and S's own result the reference model reads; a close's `ended` comes after it; nothing is written to the
    process once S is decided. Reach is asserted over the 200 seeds (test_model_reach)."""
    import random
    rnd = random.Random(seed)
    s, child, n = steered(henv, monkeypatch)
    model = _Model(henv.clock())
    model.now = henv.clock()
    got, kinds_seen = [], []
    for _ in range(rnd.randint(1, 20)):
        if s.held is None:
            kind = rnd.choice(list(FRAMES) * 3 + ["n_res_n"] * 8 + ["wait", "wait", "exit", "cancel", "close",
                                                                    "stop_turns"])
        elif s.held.s_turn:     # S's own turn runs: its result, or an end the host makes of it
            kind = rnd.choice(["s_res"] * 3 + ["plain", "error", "note", "cli_end", "wait", "wait", "exit", "cancel",
                                               "close", "stop_turns"])
        else:       # held: S's own turn, and every end the host can make of it, more often
            kind = rnd.choice(["s_init"] * 4 + ["s_res", "cli_takes_s", "cli_open", "cli_end", "note", "plain", "error",
                                                "compete", "n_res_ns", "wait", "wait", "wait", "exit", "cancel",
                                                "close", "stop_turns"])
        kinds_seen.append(kind)
        out = []
        if kind in FRAMES:
            child.emit(FRAMES[kind](n))
            model.frame(kind)
            out = henv.host.tick()
        elif kind == "wait":
            henv.clock.advance(rnd.choice([10, 30, 61]))
            model.now = henv.clock()
            if model.end is None and model.now - model.since >= 60:
                model.host_end("timeout")
            out = henv.host.tick()
        elif kind == "exit":
            child.code = 0
            model.host_end("exit")
            out = henv.host.tick()
        elif kind == "cancel":
            model.host_end("cancel")
            out = henv.host.cancel(SID, "1", "cancelled") + henv.host.tick()
        elif kind == "close":
            model.host_end("close")
            out = henv.host.close(SID, "closed") + henv.host.tick()
        elif kind == "stop_turns":
            model.host_end("stop")
            out = henv.host.stop_turns("lost the hub")
        got += out
        if model.end is not None and kind not in ("wait",):
            for _ in range(3):
                got += henv.host.tick()
        model.now = henv.clock()
        if model.end is not None and ends(got):
            break
        if s.ended or (s.child is None and not s.held):
            break
    for _ in range(60):     # the drain: the bound and the bounded stop run out
        if ends(got):
            break
        henv.clock.advance(5)
        model.now = henv.clock()
        if model.end is None and model.now - model.since >= 60:
            model.host_end("timeout")
        got += henv.host.tick()
    mine = ends(got)
    assert len(mine) == 1, (seed, kinds_seen, got)
    end = mine[0]
    want_outcome, want_why, native, s_ok = model.end
    assert (end.steer["outcome"], end.steer["why"]) == (want_outcome, want_why), (seed, kinds_seen, end.steer)
    if native is not None and want_why not in ("turn_cut",) or (native is not None and model.phase == "h"):
        assert end.text == native, (seed, kinds_seen, end.text, native)
    if want_outcome == "next_turn":
        assert end.steer["result"]["ok"] is s_ok, (seed, kinds_seen, end.steer)
    kinds = [e.kind for e in got]
    if "ended" in kinds:
        assert kinds.index("ended") > kinds.index("turn_ended"), (seed, kinds_seen)
    reach = request.config.cache.get("claude_steer_reach", {}) if False else None  # (counted in test_model_reach)
    _REACH.append((want_outcome, want_why, tuple(kinds_seen)))


_REACH: list = []


def test_model_reach():
    """The model's walks reach every row of the Claude outcome tables (run after the 200 seeds, in file order)."""
    if len(_REACH) < 200:
        pytest.skip("run with the model's seeds")
    rows = {(o, w) for o, w, _ in _REACH}
    for row in [("in_turn", "answered"), ("next_turn", "own_turn"), ("next_turn", "own_turn_cut"),
                ("unconfirmed", "consumed"), ("unconfirmed", "s_only"), ("unconfirmed", "turn_cut")]:
        assert row in rows, (row, sorted(rows))
    counts = {row: sum(1 for o, w, _ in _REACH if (o, w) == row) for row in rows}
    print("MODEL REACH", sorted(counts.items()))
    assert all(c >= 3 for c in counts.values()), counts
    assert any("compete" in k and o == "unconfirmed" for o, _, k in _REACH), "an ambiguity after a competing init"


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
    """Review round 1, ruling A: only an `init` listing exactly [S] opens S's own turn. [S, X] matches no predicate:
    it neither opens, ends nor releases anything for S, and an [S] result in no bracket after it releases nothing
    either; the hold runs to its bound (the finalizer: `unconfirmed`)."""
    s, child, n = steered(henv, monkeypatch)
    child.emit(owned(n))
    henv.host.tick()
    child.emit(vinit(S, "0be7c0de-0000-4000-8000-000000000001"))
    assert henv.host.tick() == [] and not s.held.s_turn
    child.emit(owned(S, text="?"))
    assert henv.host.tick() == [] and s.held is not None
    henv.clock.advance(61)
    out = henv.host.tick()
    [end] = ends(out) or ends(henv.host.tick())
    assert end.steer == {"outcome": "unconfirmed", "why": "turn_cut"}


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
    runner.hub_features = {"answered", "restart", "steer"}
    runner._emit([HostEvent("5e551011-0000-4000-8000-000000000000", "held", "1", ok=True, text="x")])
    assert runner.checkpoints["5e551011-0000-4000-8000-000000000000"]["text"] == "x"
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
    {"user_message_uuids": [S], "capabilities": ["interrupt_receipt_v1"]}],
    ids=["twice", "scalar", "notification", "other_version", "no_lifecycle"])
def test_r1_b2_only_the_exact_init_opens_ss_own_turn(henv, monkeypatch, init):
    """Blocker 2: S's own turn opens only on an `init` listing exactly [S], no origin, of a version in STEER_MODES
    with the lifecycle capability; none of these does, and none makes a later [S] result `next_turn`."""
    s, child, n = steered(henv, monkeypatch)
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
    """Blocker 2 (departure 3 overruled): a late [N, S] during the hold releases nothing; S's own turn that follows
    gives S's own reply, never the late frame's."""
    s, child, n = steered(henv, monkeypatch)
    child.emit(owned(n, text="N"))
    henv.host.tick()
    child.emit(owned(n, S, text="late"))
    assert henv.host.tick() == [] and s.held is not None
    child.emit(vinit(S), owned(S, text="S's own"))
    [end] = ends(henv.host.tick())
    assert end.text == "N" and end.steer["result"]["text"] == "S's own"


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
