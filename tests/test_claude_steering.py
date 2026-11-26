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
    """Ruling N: the decision and the fence under the cutoff lock; the group registry and the SIGTERM after it."""
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
    assert seen == [True]


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
    assert s.steer is not None and s.steer.outcome is None and s.stopping_since is not None
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
    assert s2.child is None and s1.child is None and saved


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
    """Ruling T+ (the ordering limit): a stop claimed before the latch signals after it: the same SIGTERM shutdown
    sends, once; the bulk stop recognises the claim (N's end decided once, by the claim)."""
    s, child, n = steered(henv, monkeypatch)
    s.steer.cover_until = henv.clock() + 20
    child.emit(owned(n, text="N"))
    henv.host.tick()
    real_stop = henv.host._stop

    def stop(sess, now, at_signal=False):
        henv.host.steer_stopping()          # the latch lands between the claim and the signal
        return real_stop(sess, now, at_signal=at_signal)
    henv.host._stop = stop
    henv.clock.advance(21)
    henv.host.steer_deadlines()
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
    host.steer_deadlines = lambda: (reached.append("steer_deadlines"), real_deadlines())[1]
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
    real = runner._dispatch
    calls = []

    def flaky(events):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("the dispatcher failed")
        return real(events)
    runner._dispatch = flaky
    with pytest.raises(RuntimeError):
        runner.pass_once()
    runner._dispatch = real
    tick(env, times=2)
    ends_queued = [e for x, e in runner.pending if x == sid and e.get("kind") == "turn_ended"]
    assert not ends_queued
    msg = hosted._get(env.board.conn, hosted._msg_key(sid, 1))
    assert msg["state"] == "done" and show(env, sid)["reply"] == "A's own"


def test_r4_v_the_watched_handoff_loses_and_duplicates_nothing(env):
    """Ruling V1: the watchdog's steer list handed to the dispatcher, which raises on its first event: the events
    go back to the host's outbox and are delivered once on the next pass."""
    from agent_comms.session_host import HostEvent
    sid, child, m = _ordinary_turn_in_flight(env)
    runner = env.node.sessions
    runner.watched_steer = [HostEvent(sid, "turn_ended", "1", ok=True, text="A's own", answered=True)]
    real = runner._dispatch
    calls = []

    def flaky(events):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("the dispatcher failed")
        return real(events)
    runner._dispatch = flaky
    with pytest.raises(RuntimeError):
        runner.pass_once()
    runner._dispatch = real
    assert [e.text for e in runner.host._outbox] == ["A's own"]
    tick(env)
    assert len([e for x, e in runner.pending if x == sid]) <= 1
    assert hosted._get(env.board.conn, hosted._msg_key(sid, 1))["state"] == "done"


def test_r4_v_the_emit_handoff_of_watched_loses_and_duplicates_nothing(env):
    """Ruling V1 (the late list): the self-stop's `watched` list handed to the dispatcher by `_emit`, which raises on
    its first event: the events go back to the host's outbox and are delivered once on the next pass."""
    from agent_comms.session_host import HostEvent
    sid, child, m = _ordinary_turn_in_flight(env)
    runner = env.node.sessions
    runner.watched = [HostEvent(sid, "turn_ended", "1", ok=True, text="A's own", answered=True)]
    real = runner._dispatch
    calls = []

    def flaky(events):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("the dispatcher failed")
        return real(events)
    runner._dispatch = flaky
    with pytest.raises(RuntimeError):
        with runner.lock:
            runner._emit([])
    runner._dispatch = real
    assert not runner.watched and [e.text for e in runner.host._outbox] == ["A's own"]
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
