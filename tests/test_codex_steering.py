"""Mid-turn steering, step 2: Codex steering with the shared hub, node and host parts (DESIGN_NOTES "Mid-turn
steering, loose contract: design (2026-11-24)", parts A, E-K and O; "Mid-turn steering, step 2: Codex steering, as
built"). The adapter from its lines alone; the host with fake processes and a fake clock; the hub and the node
in-process (test_hosted_sessions' env), with Codex lines as codex-cli 0.171.0 writes them (tests/
test_providers_codex.py and the M5 measurement). Never a real agent CLI here (scripts/session-host-e2e.py runs the
stand-in and the real CLI)."""

import json
import re

import httpx
import pytest

from agent_comms import cli, hosted, hosted_results, hosted_spawn as hs, hosted_steer, node_sessions, providers
from agent_comms.core import Conflict, Invalid
from agent_comms.providers.codex import CodexSession
from agent_comms.session_host import STEER_CUT, STEER_PARTIAL_SECONDS

import test_providers_codex as cx
from conftest import PROJECT
from test_hosted_sessions import (P2, P3, children, codex_handshake, make_node, msg, session_env, show, start, tick,
                                  written)
from test_hosted_spawn import _no_codex_home_of_the_users, bound, budget, grant, results_of, spawn  # noqa: F401
from test_hosted_tree import TREE, answer, began, as_started, proc_of, root, tree_config  # noqa: F401
from test_session_host import SID, codex_host, codex_ready, make_host  # noqa: F401
from test_session_host import henv  # noqa: F401  (the fixture)

TEXT = "IMPORTANT: also say PINEAPPLE"


@pytest.fixture
def env(tmp_path):
    """A hub with child sessions on; its node hosts Claude and Codex (test_hosted_tree's settings)."""
    return session_env(tmp_path, dispatch=TREE, config=tree_config)


# ------------------------------------------------------------ helpers


def user_item(client_id, turn=cx.TURN, thread=cx.THREAD, method="item/completed") -> dict:
    """The userMessage item of input a turn took, as 0.171.0 writes it (M5: its `clientId` is the turn/start's
    `clientUserMessageId`; a steer's has none)."""
    return {"method": method, "params": {"threadId": thread, "turnId": turn, "item": {
        "type": "userMessage", "id": "item-1", "clientId": client_id, "content": [{"type": "text", "text": "x"}]}}}


def codex_of(env, node=None):
    """The node's latest Codex process."""
    node = node or env.node
    i = max(i for i, c in enumerate(node.session_spawner.calls) if c["argv"][:2] == ["codex", "app-server"])
    return node.session_spawner.children[i]


def last_start(child) -> dict:
    return [x for x in written(child) if x.get("method") == "turn/start"][-1]


def steers_of(child) -> list[dict]:
    return [x for x in written(child) if x.get("method") == "turn/steer"]


def codex_turn_in_flight(env, text="long", project=PROJECT, prove=True, node=None) -> tuple[str, object]:
    """A Codex root whose first turn (message 1) is in flight on its node, named by the answer to its turn/start and,
    with `prove`, proved its own (a userMessage item with its clientId in that turn)."""
    sid = start(env, text, agent="codex", project=project)["session_id"]
    tick(env, node)
    child = codex_of(env, node)
    codex_handshake(env, child, node)
    st = last_start(child)
    child.emit(cx.reply(st["id"], cx.TURN_REPLY), cx.TURN_STARTED,
               *([user_item(st["params"]["clientUserMessageId"])] if prove else []))
    tick(env, node)
    assert show(env, sid)["in_flight"] == 1
    return sid, child


def post_steer(env, sid, text=TEXT, expect=200, **kw) -> dict:
    r = env.human.post(f"/api/hosted-sessions/{sid}/messages", json={"message": text, "mode": "steer", **kw})
    assert r.status_code == expect, r.text
    return r.json()


def steer_rec(env, sid, n=1) -> dict | None:
    return hosted_steer.get(env.board.conn, sid, n)


def answer_steer(child, req, result=None, error=None):
    child.emit(cx.reply(req["id"], result if error is None else None, error))


def end_turn(env, child, text="done", node=None):
    child.emit(cx.message(text), cx.completed())
    tick(env, node)


def steered(env, text="long", **kw):
    """A Codex turn in flight, a human steer accepted and written into it (its turn/steer request)."""
    sid, child = codex_turn_in_flight(env, text, **kw)
    post_steer(env, sid)
    tick(env)
    [req] = steers_of(child)
    return sid, child, req


def hub_node(env, tree):
    with hosted.db.write_tx(env.board.conn) as c:
        info = hosted.nodes._get(c, hosted.nodes.NODE_PREFIX + "laptop")
        info["session_host"]["tree"] = tree
        hosted.nodes._put(c, hosted.nodes.NODE_PREFIX + "laptop", info, "laptop", env.clock())


# ------------------------------------------------------------ the adapter (part F, M5)


def proved(order="answer_first") -> tuple[CodexSession, str]:
    p = cx.ready()
    [start_] = cx.sent(p.turn("long"))
    cid = start_["params"]["clientUserMessageId"]
    lines = [cx.reply(3, cx.TURN_REPLY), cx.line(cx.TURN_STARTED), cx.line(user_item(cid))]
    if order == "item_first":
        lines = [cx.line(cx.TURN_STARTED), cx.line(user_item(cid)), cx.reply(3, cx.TURN_REPLY)]
    for x in lines:
        p.feed(x)
    return p, cid


@pytest.mark.parametrize("order", ["answer_first", "item_first"])
def test_f_a_turn_is_proved_the_messages_by_its_answer_and_its_clientid_item_in_either_order(order):
    p, _ = proved(order)
    assert p.owns()
    [req] = cx.sent(p.steer(TEXT))
    assert req == {"id": 4, "method": "turn/steer", "params": {
        "threadId": cx.THREAD, "expectedTurnId": cx.TURN, "input": [{"type": "text", "text": TEXT}]}}
    assert p.steer(TEXT) is None, "one steer per turn"


def test_f_no_proof_no_write():
    """The answer alone, the item alone, an item in another turn or with another id, an id only from turn/started:
    none proves the turn N's, and nothing is written."""
    p = cx.ready()
    cid = cx.sent(p.turn("long"))[0]["params"]["clientUserMessageId"]
    p.feed(cx.reply(3, cx.TURN_REPLY))
    assert not p.owns() and p.steer(TEXT) is None, "the answer alone"
    p.feed(cx.line(user_item(cid, turn="01a1fe39-0000-7000-8000-000000000000")))
    assert not p.owns(), "an item in another turn"
    p.feed(cx.line(user_item("another-id")))
    assert not p.owns(), "an item with another clientId"
    q = cx.ready()
    cid = cx.sent(q.turn("long"))[0]["params"]["clientUserMessageId"]
    q.feed(cx.line(cx.TURN_STARTED))
    q.feed(cx.line(user_item(cid)))
    assert not q.owns(), "turn/started named the turn, the answer did not (yet)"
    q.feed(cx.line(cx.completed()))
    q.feed(cx.reply(3, cx.TURN_REPLY))
    assert not q.owns(), "after N's end nothing is proved"


def test_f_a_join_is_proved_once_its_item_comes_in_the_turn_the_answer_named():
    """M5, the join: a turn/start while Codex runs a turn of its own is answered with THAT turn; the item with the
    message's clientId comes in it later (measured on 0.171.0). The item before the answer counts too."""
    p = cx.ready()
    p.feed(cx.own_turn("turn/started"))
    cid = cx.sent(p.turn("join"))[0]["params"]["clientUserMessageId"]
    own = cx.CHILD_TURN
    p.feed(cx.reply(3, {"turn": {**cx.TURN_REPLY["turn"], "id": own}}))
    assert not p.owns()
    p.feed(cx.line(user_item(cid, turn=own, method="item/started")))
    assert p.owns()
    [req] = cx.sent(p.steer(TEXT))
    assert req["params"]["expectedTurnId"] == own


@pytest.mark.parametrize("answer, want", [
    ({"result": {"turnId": cx.TURN}}, "ok"),
    ({"result": {"turnId": "01a1fe39-0000-7000-8000-000000000000"}}, "other"),
    ({"result": {}}, "other"),
    ({"error": {"code": -32600, "message": "no active turn to steer"}}, "refused"),
    ({"error": {"code": -32600, "message": f"expected active turn id `{cx.TURN}` but found `b`"}}, "refused"),
    ({"error": {"code": -32600, "message": "expected active turn id `a` but found `b`"}}, "other"),
    ({"error": {"code": -32600, "message": "expectedTurnId must not be empty"}}, "other"),
    ({"error": {"code": -32600.0, "message": "no active turn to steer"}}, "other"),
    ({"error": {"code": True, "message": "no active turn to steer"}}, "other"),
    ({"result": {"turnId": cx.TURN}, "error": {"code": -32600, "message": "no active turn to steer"}}, "other"),
    ({"result": {"turnId": cx.TURN, "extra": 1}}, "other"),
    ({"result": {"turnId": cx.TURN}, "method": "x"}, None),     # (a request, not an answer)
    ({"error": {"code": -32600, "message": "no active turn to steer, and more"}}, "other"),
    ({"error": {"code": -32603, "message": "no active turn to steer"}}, "other"),
    ({"error": {"code": -32603, "message": "internal error"}}, "other"),
    (None, None),
])
def test_f_what_the_steer_request_was_answered_before_ns_end(answer, want):
    """S2-01: only an exclusive, well-formed answer to the fully written request is evidence; an integer code; a
    quoted expected id that is the request's own."""
    p, _ = proved()
    [req] = cx.sent(p.steer(TEXT))
    p.steer_written()
    if answer is not None:
        p.feed(cx.line({"id": req["id"], **answer}))
    [ended], _ = p.feed(cx.line(cx.completed()))
    assert ended.kind == "turn_ended" and p.steer_result() == want


def test_f_an_answer_after_ns_end_changes_nothing_and_is_logged(caplog):
    p, _ = proved()
    [req] = cx.sent(p.steer(TEXT))
    p.steer_written()
    p.feed(cx.line(cx.completed()))
    assert p.steer_result() is None
    with caplog.at_level("INFO", logger="agent_comms.providers.codex"):
        assert p.feed(cx.reply(req["id"], {"turnId": cx.TURN})) == ([], [])
    assert p.steer_result() is None and "late answer" in caplog.text


def test_s2_01_an_answer_read_before_the_frame_was_fully_written_is_no_evidence():
    p, _ = proved()
    [req] = cx.sent(p.steer(TEXT))
    p.feed(cx.reply(req["id"], {"turnId": cx.TURN}))       # before the host said every byte is in the pipe
    p.steer_written()
    p.feed(cx.line(cx.completed()))
    assert p.steer_result() == "other"


def test_s2_01_a_double_answer_with_a_success_and_a_refusal_is_unconfirmed_through_the_host(henv):
    host, s, child, _ = host_n(henv)
    host.steer_offer(SID, "a", "u1", TEXT, authorized=True)
    host.tick()
    [req] = steers_of(child)
    child.emit(cx.line({"id": req["id"], "result": {"turnId": cx.TURN},
                        "error": {"code": -32600, "message": "no active turn to steer"}}))
    child.emit(cx.message("done"), cx.completed())
    [end] = ended_of(host.tick())
    assert end.steer == {"outcome": "unconfirmed", "why": "other_answer"}


def test_f_a_retired_frame_is_never_answered_into_anything():
    p, _ = proved()
    [req] = cx.sent(p.steer(TEXT))
    p.steer_retire()
    assert p.feed(cx.reply(req["id"], {"turnId": cx.TURN})) == ([], [])
    p.feed(cx.line(cx.completed()))
    assert p.steer_result() is None


def test_f_every_turn_start_carries_a_fresh_client_id_and_claude_never_steers():
    p = cx.ready()
    a = cx.sent(p.turn("one"))[0]["params"]["clientUserMessageId"]
    p.feed(cx.line(cx.completed()))
    b = cx.sent(p.turn("two"))[0]["params"]["clientUserMessageId"]
    assert a != b and cx.UUID4.fullmatch(a) and cx.UUID4.fullmatch(b)
    from agent_comms.providers.claude import ClaudeSession
    assert ClaudeSession.steers is False and "steer" not in providers.modes(ClaudeSession())
    assert providers.modes(p) == ("steer", "restart")


# ------------------------------------------------------------ the host's slot (parts D-F)


def host_n(henv, prove=True, authority=True):
    """A host with a Codex session whose message `a` is in flight, named and (with `prove`) proved; the node's
    authority answer is `authority` (a list to change it later)."""
    host, s = codex_host(henv)
    gate = authority if isinstance(authority, list) else [authority]
    host.steer_authority = lambda sess: gate[0]
    host.send(SID, "long", message_id="a")
    host.tick()
    child = henv.spawner.children[-1]
    codex_ready(host, child)
    host.tick()
    st = last_start(child)
    child.emit(cx.reply(st["id"], cx.TURN_REPLY), cx.TURN_STARTED,
               *([user_item(st["params"]["clientUserMessageId"])] if prove else []))
    host.tick()
    return host, s, child, gate


def ended_of(events) -> list:
    return [e for e in events if e.kind == "turn_ended"]


def test_host_writes_the_steer_once_when_every_condition_holds_and_it_is_in_turn(henv):
    host, s, child, _ = host_n(henv)
    host.steer_offer(SID, "a", "u1", TEXT, authorized=True)
    host.tick()
    [req] = steers_of(child)
    assert req["params"]["expectedTurnId"] == cx.TURN
    host.steer_offer(SID, "a", "u1", TEXT, authorized=True)      # the hub offers it again: the same steer
    host.tick()
    assert len(steers_of(child)) == 1, "written at most once"
    answer_steer(child, req, {"turnId": cx.TURN})
    child.emit(cx.message("done"), cx.completed())
    [end] = ended_of(host.tick())
    assert end.steer == {"outcome": "in_turn", "why": "answered"} and end.ok
    assert s.steer is None and "u1" in s.steered
    host.steer_offer(SID, "a", "u1", TEXT, authorized=True)
    assert s.steer is None, "a steer this host took is never taken again"


@pytest.mark.parametrize("why", ["unauthorized", "revoked", "refused", "unbound", "may_start", "turn_gate",
                                 "authority", "not_proved"])
def test_host_writes_nothing_unless_every_condition_holds_and_says_not_delivered(henv, why):
    host, s, child, gate = host_n(henv, prove=why != "not_proved")
    host.steer_offer(SID, "a", "u1", TEXT, authorized=why != "unauthorized",
                     refused="too_big" if why == "refused" else None)
    if why == "revoked":
        host.steer_revoke(SID, "u1", "omitted")
    if why == "unbound":
        s.bind = "b" * 64
    if why == "may_start":
        host.may_start = lambda: False
    if why == "turn_gate":
        host.turn_gate = lambda sess: False
    if why == "authority":
        gate[0] = False
    host.tick()
    assert steers_of(child) == []
    host.may_start, host.turn_gate, s.bind = None, None, None
    child.emit(cx.message("done"), cx.completed())
    [end] = ended_of(host.tick())
    assert end.steer == {"outcome": "not_delivered", "why": "not_owned" if why == "not_proved" else "not_written"}


def test_host_a_steer_for_a_turn_not_in_flight_or_a_second_one_is_never_taken(henv):
    host, s, child, _ = host_n(henv)
    host.steer_offer(SID, "b", "u1", TEXT, authorized=True)
    assert s.steer is None, "not the turn in flight here"
    host.steer_offer(SID, "a", "u1", TEXT, authorized=True)
    host.steer_offer(SID, "a", "u2", TEXT, authorized=True)
    assert s.steer.uuid == "u1"


@pytest.mark.parametrize("answer, outcome", [
    ({"error": {"code": -32600, "message": "no active turn to steer"}}, ("not_delivered", "refused")),
    ({"error": {"code": -32603, "message": "internal error"}}, ("unconfirmed", "other_answer")),
    (None, ("unconfirmed", "no_answer")),
])
def test_host_the_answer_table_at_ns_end(henv, answer, outcome):
    host, s, child, _ = host_n(henv)
    host.steer_offer(SID, "a", "u1", TEXT, authorized=True)
    host.tick()
    [req] = steers_of(child)
    if answer is not None:
        child.emit(cx.line({"id": req["id"], **answer}))
    child.emit(cx.message("done"), cx.completed())
    [end] = ended_of(host.tick())
    assert (end.steer["outcome"], end.steer["why"]) == outcome


def test_host_a_frame_not_one_byte_of_which_went_is_taken_back_and_retired_at_ns_end(henv):
    host, s, child, _ = host_n(henv)
    child.accept = 0                # the pipe takes nothing
    host.steer_offer(SID, "a", "u1", TEXT, authorized=True)
    host.tick()
    assert s.steer.mark is not None and s.steer.sent == 0 and b"turn/steer" in bytes(s.outbuf)
    child.accept = None
    child.emit(cx.message("done"), cx.completed())
    [end] = ended_of(host.tick())
    assert end.steer == {"outcome": "not_delivered", "why": "not_written"}
    assert b"turn/steer" not in bytes(s.outbuf) and steers_of(child) == []
    assert s.provider._steer is None and s.provider._pending.get(5) is None, "its request id is retired"


def test_r11_03_an_omission_before_any_byte_takes_the_frame_back_and_after_a_full_write_changes_nothing(henv):
    host, s, child, _ = host_n(henv)
    child.accept = 0
    host.steer_offer(SID, "a", "u1", TEXT, authorized=True)
    host.tick()
    host.steer_revoke(SID, "u1", "omitted")
    child.accept = None
    host.tick()
    assert steers_of(child) == [] and s.steer.mark is None, "never written"
    child.emit(cx.message("done"), cx.completed())
    assert ended_of(host.tick())[0].steer == {"outcome": "not_delivered", "why": "not_written"}
    # After a full write: the outcome is still tracked (the answer decides), and the dedupe state stays.
    host2, s2, child2, _ = host_n(henv)
    host2.steer_offer(SID, "a", "u9", TEXT, authorized=True)
    host2.tick()
    [req] = steers_of(child2)
    host2.steer_revoke(SID, "u9", "omitted")
    answer_steer(child2, req, {"turnId": cx.TURN})
    child2.emit(cx.message("done"), cx.completed())
    assert ended_of(host2.tick())[0].steer == {"outcome": "in_turn", "why": "answered"}
    assert "u9" in s2.steered


def partial(henv, take=10, authority=True):
    host, s, child, gate = host_n(henv, authority=authority)
    child.accept = take
    host.steer_offer(SID, "a", "u1", TEXT, authorized=True)
    host.tick()
    assert 0 < s.steer.sent < s.steer.length, "the pipe took part of the frame"
    return host, s, child, gate


def test_d_a_partial_frame_is_completed_while_authority_lasts(henv):
    host, s, child, _ = partial(henv)
    host.send(SID, "next", message_id="b")
    child.accept = None
    henv.clock.advance(STEER_PARTIAL_SECONDS - 1)
    host.tick()
    [req] = steers_of(child)
    assert s.steer.mark is None and s.steer.sent == s.steer.length
    answer_steer(child, req, {"turnId": cx.TURN})
    child.emit(cx.message("done"), cx.completed())
    [end] = ended_of(host.tick())
    assert end.steer["outcome"] == "in_turn"


def test_d_no_next_turn_is_staged_behind_a_half_written_frame(henv):
    """The half frame blocks the next turn while it may still be completed."""
    host, s, child, _ = partial(henv)
    child.accept = 0
    host.send(SID, "next", message_id="b")
    host.tick()
    assert s.writing is None and list(s.inbox)[0].id == "b", "nothing is staged behind the steer frame"
    child.accept = None
    host.tick()
    assert s.steer.mark is None and steers_of(child), "completed while N runs"
    host.tick()
    assert list(s.inbox)[0].id == "b", "the next turn waits for N's end as ever"


def test_s2_02_after_ns_end_the_rest_of_a_half_frame_never_goes_the_process_is_cut(henv):
    """Review round 1, S2-02 (this replaces round 0's test, which expected the suffix after N had ended: wrong).
    N's native result is kept; the rest of the frame is not written; the process is stopped."""
    host, s, child, _ = partial(henv)
    written_before = len(child.stdin)
    child.emit(cx.message("done"), cx.completed())      # N ends while the frame is still half written
    child.accept = None
    events = host.tick()
    [end] = ended_of(events)
    assert end.ok and end.text == "done" and end.steer == {"outcome": "unconfirmed", "why": "partial"}
    assert len(child.stdin) == written_before, "not one more byte of the frame"
    assert s.stopping_since is not None and s.steer is None


@pytest.mark.parametrize("gate", ["turn_gate", "unbound", "may_start"])
def test_s2_02_the_rest_of_a_half_frame_goes_only_while_every_gate_holds(henv, gate):
    host, s, child, _ = partial(henv)
    child.accept = None
    written_before = len(child.stdin)
    if gate == "turn_gate":
        host.turn_gate = lambda sess: False
    elif gate == "unbound":
        s.bind = "c" * 64
    else:
        host.may_start = lambda: False
    events = host.tick()
    [end] = ended_of(events)
    assert end.detail == STEER_CUT and len(child.stdin) == written_before and s.stopping_since is not None


def test_s2_02_the_5_s_limit_is_read_on_a_fresh_clock_at_the_write(henv):
    """A clock advance inside one pass (the read takes long): at the write boundary the limit has passed."""
    host, s, child, _ = partial(henv)
    child.accept = None
    real = child.read_lines

    def slow():
        henv.clock.advance(STEER_PARTIAL_SECONDS)
        return real()
    child.read_lines = slow
    before = len(child.stdin)
    [end] = ended_of(host.tick())
    assert end.detail == STEER_CUT and len(child.stdin) == before


@pytest.mark.parametrize("how", ["time", "authority", "omission"])
def test_d_the_cut_a_partial_frame_that_cannot_be_completed_stops_the_process(henv, how):
    host, s, child, gate = partial(henv, authority=[True])
    if how == "time":
        henv.clock.advance(STEER_PARTIAL_SECONDS)
    elif how == "authority":
        gate[0] = False
    else:
        host.steer_revoke(SID, "u1", "omitted")
    child.emit(cx.line(cx.message("half")))            # what the process wrote is read first
    events = host.tick()
    [end] = ended_of(events)
    assert (end.ok, end.detail) == (False, STEER_CUT)
    assert end.steer == {"outcome": "unconfirmed", "why": "partial"}
    assert s.stopping_since is not None and s.steer is None
    assert written(child)[-1].get("method") != "turn/steer" and not child.stdin.endswith(b"\n"), \
        "the half frame is never completed"


def test_d_the_watchdog_cuts_a_partial_frame_past_its_limit_between_passes(henv):
    host, s, child, _ = partial(henv)
    henv.clock.advance(STEER_PARTIAL_SECONDS + 0.1)
    [end] = ended_of(host.steer_deadlines())
    assert end.detail == STEER_CUT and end.steer["outcome"] == "unconfirmed" and s.stopping_since is not None


def test_d_a_native_end_drained_at_the_cut_is_ns_own(henv):
    host, s, child, _ = partial(henv)
    henv.clock.advance(STEER_PARTIAL_SECONDS)
    child.emit(cx.message("finished first"), cx.completed())
    [end] = ended_of(host.tick())
    assert end.ok and end.text == "finished first", "its native result, read before the stop"
    assert end.steer == {"outcome": "unconfirmed", "why": "partial"} and s.stopping_since is not None


@pytest.mark.parametrize("how", ["exit", "timeout", "cancel", "close", "stop_turns", "stop_all"])
def test_o_n_ended_with_the_process_after_a_byte_is_unconfirmed_even_with_a_matched_receipt(henv, how):
    """Part O: Codex's contact cut (and every end the host makes) before any native result is cached, including a
    valid matched turn/steer receipt immediately before the process exits: never `in_turn`."""
    host, s, child, _ = host_n(henv)
    host.steer_offer(SID, "a", "u1", TEXT, authorized=True)
    host.tick()
    [req] = steers_of(child)
    answer_steer(child, req, {"turnId": cx.TURN})       # the valid receipt
    if how == "exit":
        child.code = 1
        events = host.tick()
    elif how == "timeout":
        henv.clock.advance(31 * 60)
        host.tick()                                     # the interrupt is written
        henv.clock.advance(henv.config.kill_grace_seconds)
        events = host.tick()
    elif how == "cancel":
        events = host.cancel(SID, "a", "cancelled")
    elif how == "close":
        events = host.close(SID, "closed")
    elif how == "stop_turns":
        events = host.stop_turns("the node had no hub contact")
    else:
        events = host.stop_all()
    [end] = ended_of(events)
    assert end.steer == {"outcome": "unconfirmed", "why": "turn_cut"} and not end.ok


# The slot's states x N's ends (as tests/test_session_restart.py's CELLS): (state, end) -> (outcome, why).
STEER_CELLS = {
    ("unproved", "native"): ("not_delivered", "not_owned"),
    ("unproved", "exit"): ("not_delivered", "not_owned"),
    ("unauthorized", "native"): ("not_delivered", "not_written"),
    ("queued", "native"): ("not_delivered", "not_written"),
    ("queued", "exit"): ("not_delivered", "not_written"),
    ("written", "native"): ("unconfirmed", "no_answer"),
    ("written", "native_ok"): ("in_turn", "answered"),
    ("written", "native_refused"): ("not_delivered", "refused"),
    ("written", "native_other"): ("unconfirmed", "other_answer"),
    ("written", "exit"): ("unconfirmed", "turn_cut"),
    ("written", "exit_ok"): ("unconfirmed", "turn_cut"),
    ("partial", "native"): ("unconfirmed", "partial"),
    ("partial", "exit"): ("unconfirmed", "partial"),
}


@pytest.mark.parametrize("state, end", sorted(STEER_CELLS))
def test_cells_every_slot_state_by_every_end_of_n(henv, state, end):
    host, s, child, _ = host_n(henv, prove=state != "unproved")
    if state == "queued":
        child.accept = 0
    if state == "partial":
        child.accept = 10
    host.steer_offer(SID, "a", "u1", TEXT, authorized=state != "unauthorized")
    host.tick()
    if state == "partial":
        child.accept = 0        # the rest waits
    reqs = steers_of(child)
    if end in ("native_ok", "exit_ok"):
        answer_steer(child, reqs[0], {"turnId": cx.TURN})
    elif end == "native_refused":
        answer_steer(child, reqs[0], error={"code": -32600, "message": "no active turn to steer"})
    elif end == "native_other":
        answer_steer(child, reqs[0], error={"code": -32603, "message": "internal error"})
    if end.startswith("native"):
        child.emit(cx.message("done"), cx.completed())
    else:
        child.code = 1
    [ended] = ended_of(host.tick())
    assert (ended.steer["outcome"], ended.steer["why"]) == STEER_CELLS[(state, end)]


def test_host_drain_reads_a_native_end_before_a_shutdowns_signal(henv):
    host, s, child, _ = host_n(henv)
    host.steer_offer(SID, "a", "u1", TEXT, authorized=True)
    host.tick()
    [req] = steers_of(child)
    child.emit(cx.reply(req["id"], {"turnId": cx.TURN}), cx.message("done"), cx.completed())
    [end] = ended_of(host.drain())
    assert end.steer["outcome"] == "in_turn" and end.ok
    assert ended_of(host.stop_all()) == [], "N's end is emitted once"


# ------------------------------------------------------------ the hub: accepting and refusing (part H)


def test_h_a_human_steer_into_a_codex_turn_is_accepted_and_answered_as_a_steer(env):
    sid, child = codex_turn_in_flight(env)
    out = post_steer(env, sid, request_id="s1")
    assert out["message"] is None and out["mode"] == "steer" and out["created"] is True
    assert out["steer"] == {"session": sid, "into": 1, "outcome": None, "state": "accepted"}
    assert "message 1" in out["why"]
    rec = steer_rec(env, sid)
    assert (rec["kind"], rec["by"], rec["listed_at"], rec["outcome"]) == ("user", {"human": "human"}, None, None)
    assert show(env, sid)["queued"] == 0 and len(show(env, sid)["messages"]) == 1, "no message of its own"
    again = post_steer(env, sid, text="a retry's own text", request_id="s1")
    assert again["created"] is False and again["steer"]["into"] == 1 and again["why"] == hosted_steer.RETRY_WHY
    assert steer_rec(env, sid)["text"] == TEXT, "never with the retry's text"


def refusal(env, sid, why, expect=409, **kw):
    out = post_steer(env, sid, expect=expect, **kw)
    assert out["message"] == f"cannot steer into session {sid}: {why}", out
    return out


def test_h_every_refusal_of_a_human_steer_with_its_words(env, monkeypatch):
    sid, child = codex_turn_in_flight(env)
    refusal(env, sid, hosted_steer.REFUSED["big"], text="x" * (hosted_steer.STEER_MAX_BYTES - 1))
    refusal(env, sid, hosted_steer.REFUSED["schema"], output_schema='{"type": "object"}')
    env.human.post(f"/api/hosted-sessions/{sid}/messages", json={"message": "behind"})
    refusal(env, sid, hosted_steer.REFUSED["queued"].format(k=1))
    with hosted.db.write_tx(env.board.conn) as c:       # (the queued one taken away again)
        m = hosted._get(c, hosted._msg_key(sid, 2))
        hosted._put(c, hosted._msg_key(sid, 2), m | {"state": "failed"}, "hub", env.clock())
        r = hosted._session(c, sid)
        hosted._put(c, hosted.SESSION_PREFIX + sid, r | {"queued": 0}, "hub", env.clock())
    post_steer(env, sid)
    refusal(env, sid, hosted_steer.REFUSED["unsettled"].format(n=1))
    env.board.s.dispatch = json.loads(json.dumps(env.board.s.dispatch))
    env.board.s.dispatch["sessions"]["steer"] = False
    refusal(env, sid, hosted_steer.REFUSED["off"])
    env.board.s.dispatch["sessions"]["steer"] = True
    hub_node(env, 4)
    refusal(env, sid, hosted_steer.REFUSED["protocol"].format(node="laptop", tree=4, need=5))
    hub_node(env, 5)
    # N already took one: once it is settled, a second steer into the same N is refused too.
    with hosted.db.write_tx(env.board.conn) as c:
        s = hosted_steer.get(c, sid, 1)
        hosted_steer._settled(c, s, None, env.clock())
    refusal(env, sid, hosted_steer.REFUSED["took"].format(n=1))


def test_h_refused_for_claude_idle_wake_result_and_data_turns(env):
    claude = root(env)
    call, proc = proc_of(env, claude)
    proc.emit(began(claude, *as_started(call["argv"])) | {"capabilities": ["interrupt_receipt_v1"]})
    tick(env)
    refusal(env, claude, hosted_steer.REFUSED["cli"])
    sid, child = codex_turn_in_flight(env, project=P2)
    end_turn(env, child)
    refusal(env, sid, hosted.REFUSED["idle"])
    asked = start(env, "data please", agent="codex", project=P3, output_schema='{"type": "object"}')["session_id"]
    tick(env)
    c2 = codex_of(env)
    codex_handshake(env, c2)
    st = last_start(c2)
    c2.emit(cx.reply(st["id"], cx.TURN_REPLY), cx.TURN_STARTED)
    tick(env)
    refusal(env, asked, hosted_steer.REFUSED["data"])


def test_h_refused_for_a_wake_turn_and_a_closing_or_ended_session(env):
    sid, child = codex_turn_in_flight(env)
    with hosted.db.write_tx(env.board.conn) as c:
        m = hosted._get(c, hosted._msg_key(sid, 1))
        hosted._put(c, hosted._msg_key(sid, 1), m | {"kind": "wake"}, "hub", env.clock())
    refusal(env, sid, hosted.REFUSED["kind"].format(n=1, what=hosted.KIND_WORDS["wake"]))
    env.human.post(f"/api/hosted-sessions/{sid}/close")
    refusal(env, sid, hosted_steer.REFUSED["closing"])


def test_h_a_restart_waits_for_an_unsettled_steer(env):
    sid, child = codex_turn_in_flight(env)
    post_steer(env, sid)
    r = env.human.post(f"/api/hosted-sessions/{sid}/messages", json={"message": "x", "mode": "restart"})
    assert r.status_code == 409 and hosted.REFUSED["steered"].format(n=1) in r.json()["message"]


def test_h_a_receipt_only_retry_answers_expired(env):
    sid, child = codex_turn_in_flight(env)
    post_steer(env, sid, request_id="s1")
    with hosted.db.write_tx(env.board.conn) as c:
        c.execute("DELETE FROM board_state WHERE key = ?", (hosted_steer._key(sid, 1),))
    out = post_steer(env, sid, request_id="s1")
    assert out["created"] is False and out["steer"]["state"] == "expired" and out["why"] == hosted_steer.EXPIRED_WHY


# ------------------------------------------------------------ the hub: every offer (part G)


def listing(env, tree=5) -> dict:
    return env.laptop.get("/api/nodes/sessions", params={"tree": tree}).json()


def entry(env, sid, tree=5) -> dict:
    return next(x for x in listing(env, tree)["sessions"] if x["session_id"] == sid)


def test_g_every_offer_is_made_in_the_listings_write_transaction_and_sets_listed_at_once(env):
    sid, child = codex_turn_in_flight(env)
    before = listing(env)["version"]
    post_steer(env, sid)
    assert steer_rec(env, sid)["listed_at"] is None
    item = entry(env, sid)["steer"]
    rec = steer_rec(env, sid)
    assert item == {"into": 1, "uuid": rec["uuid"], "text": TEXT} and rec["listed_at"] is not None
    first = rec["listed_at"]
    env.clock.advance(1)
    v = listing(env)
    assert steer_rec(env, sid)["listed_at"] == first, "set once"
    assert v["version"] != before, "an eligible steer is part of the version"
    assert "steer" in v["features"]


def test_g_an_older_node_is_never_offered_a_steer_and_that_withdraws_it(env):
    sid, child = codex_turn_in_flight(env)
    post_steer(env, sid)
    assert "steer" not in entry(env, sid, tree=4)
    rec = steer_rec(env, sid)
    assert rec["withdrawn_at"] is not None and rec["listed_at"] is None and rec["withdrawn"] == "protocol"
    assert "steer" not in entry(env, sid), "withdrawn: never offered again"


@pytest.mark.parametrize("why", ["checkout", "cancelled", "expired"])
def test_g_a_failed_check_withdraws_for_good_even_when_it_later_holds_again(env, monkeypatch, why):
    """N-3: first-exposure revalidation against a dispatch claim, a cancel, a node that went silent (and a lease a
    later hello revives): withdrawn once, never rendered again."""
    sid, child = codex_turn_in_flight(env)
    post_steer(env, sid)
    if why == "checkout":
        monkeypatch.setattr(hosted, "_checkout_busy", lambda board, r, runs=None: True)
    elif why == "cancelled":
        with hosted.db.write_tx(env.board.conn) as c:
            hosted_steer.cancel(c, sid, 1, env.clock(), "x")
    else:
        monkeypatch.setattr(hosted, "_lost", lambda conn, r, now: True)
    assert "steer" not in entry(env, sid)
    assert steer_rec(env, sid)["withdrawn"] == why and steer_rec(env, sid)["withdrawn_at"] is not None
    monkeypatch.undo()
    env.node.hello()                        # a later hello: what failed holds again
    assert "steer" not in entry(env, sid), "a withdrawn steer is never rendered again"


def test_g_the_fallback_and_a_read_snapshot_never_show_a_steer(env, monkeypatch):
    sid, child = codex_turn_in_flight(env)
    post_steer(env, sid)
    from agent_comms import hosted_wake
    monkeypatch.setattr(hosted_wake, "_unsettled", lambda board, node: (_ for _ in ()).throw(RuntimeError("x")))
    got = entry(env, sid)
    assert "steer" not in got and steer_rec(env, sid)["listed_at"] is None, "the fallback offers nothing"
    monkeypatch.undo()
    monkeypatch.setattr(hosted_steer, "due", lambda conn, node: False)     # no write transaction is taken
    assert "steer" not in entry(env, sid) and steer_rec(env, sid)["listed_at"] is None


def test_g_a_rolled_back_offer_is_not_shown(env, monkeypatch):
    """A listing whose write transaction fails after the offer was made shows it nowhere (the fallback's build gets
    no offer)."""
    sid, child = codex_turn_in_flight(env)
    post_steer(env, sid)
    real = hosted._reserve_tasks
    calls = []

    def boom(board, c, node, now):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("boom")
        return real(board, c, node, now)
    monkeypatch.setattr(hosted, "_reserve_tasks", boom)
    got = entry(env, sid)
    assert "steer" not in got and steer_rec(env, sid)["listed_at"] is None


# ------------------------------------------------------------ the hub: settling, once, with N (part G)


def test_g_the_owning_hosts_extension_decides_in_turn(env):
    sid, child, req = steered(env)
    answer_steer(child, req, {"turnId": cx.TURN})
    end_turn(env, child)
    rec = steer_rec(env, sid)
    assert (rec["outcome"], rec["why"], rec["text"]) == ("in_turn", "answered", "")
    assert msg(env, sid, 1)["state"] == "done" and show(env, sid)["reply"] == "done"
    assert show(env, sid)["messages"][0]["steer"]["outcome"] == "in_turn"
    assert "steer_unsettled" not in show(env, sid)


def report_end(env, sid, steer=None, n=1, ok=True):
    ev = {"kind": "turn_ended", "message": n, "ok": ok, "text": "r", "answered": True}
    if steer is not None:
        ev["steer"] = steer
    r = env.laptop.post(f"/api/nodes/sessions/{sid}", json={"events": [ev]})
    assert r.status_code == 200, r.text


@pytest.mark.parametrize("listed, ext, want", [
    (True, {"outcome": "in_turn", "why": "answered"}, ("in_turn", "answered")),
    (False, {"outcome": "in_turn", "why": "answered"}, ("not_delivered", "never_listed")),
    (True, {"outcome": "not_delivered", "why": "refused"}, ("not_delivered", "refused")),
    (False, {"outcome": "not_delivered", "why": "not_owned"}, ("not_delivered", "not_owned")),
    (True, {"outcome": "unconfirmed", "why": "no_answer"}, ("unconfirmed", "no_answer")),
    (True, None, ("unconfirmed", "no_word")),
    (False, None, ("not_delivered", "never_listed")),
    (True, {"outcome": "next_turn", "why": "x"}, ("unconfirmed", "no_word")),
    (True, {"outcome": "in_turn", "why": "Bad Word"}, ("unconfirmed", "no_word")),
    (True, {"outcome": "in_turn", "why": "answered", "extra": 1}, ("unconfirmed", "no_word")),
    (True, "in_turn", ("unconfirmed", "no_word")),
])
def test_g_the_one_precedence_rule_on_the_nodes_end(env, listed, ext, want):
    sid, child = codex_turn_in_flight(env)
    post_steer(env, sid)
    if listed:
        entry(env, sid)
    report_end(env, sid, ext)
    rec = steer_rec(env, sid)
    assert (rec["outcome"], rec["why"]) == want
    assert msg(env, sid, 1)["state"] == "done", "N's own end is taken whatever its extension"
    report_end(env, sid, {"outcome": "in_turn", "why": "answered"})
    assert (steer_rec(env, sid)["outcome"], steer_rec(env, sid)["why"]) == want, "never revised"


def test_g_a_replacement_nodes_lost_end_settles_it_by_the_rule(env):
    sid, child = codex_turn_in_flight(env)
    post_steer(env, sid)
    entry(env, sid)
    node2 = make_node(env, config=env.node.config, log_dir=env.tmp / "laptop-logs")
    node2.sessions.recover()
    node2.hello()
    tick(env, node2)
    tick(env, node2)
    assert msg(env, sid, 1)["detail"].startswith(node_sessions.LOST)
    assert (steer_rec(env, sid)["outcome"], steer_rec(env, sid)["why"]) == ("unconfirmed", "no_word")
    assert all(steers_of(c) == [] for c in node2.session_spawner.children), "never written by the replacement"


def test_g_a_lost_node_settles_it_by_the_rule(env):
    sid, child = codex_turn_in_flight(env)
    post_steer(env, sid)
    env.clock.advance(hosted.nodes.NODE_LOST_SECONDS + 1)
    view = show(env, sid)
    assert view["messages"][0]["state"] == "failed" and view["messages"][0]["steer"]["outcome"] == "not_delivered"


@pytest.mark.parametrize("how", ["close_away", "cascade", "orphan"])
def test_g_end_settles_it_by_the_rule(env, how):
    sid, child = codex_turn_in_flight(env)
    post_steer(env, sid)
    entry(env, sid)
    with hosted.db.write_tx(env.board.conn) as c:
        r = hosted._session(c, sid)
        hosted._end(c, r, f"by {how}", "hub", env.clock())
        hosted._put(c, hosted.SESSION_PREFIX + sid, r, "hub", env.clock())
    assert (steer_rec(env, sid)["outcome"], steer_rec(env, sid)["why"]) == ("unconfirmed", "no_word")


def test_j_the_reupgrade_sweep_settles_a_steer_an_older_hub_left_and_leaves_n_as_it_was(env):
    sid, child = codex_turn_in_flight(env)
    post_steer(env, sid)
    entry(env, sid)
    with hosted.db.write_tx(env.board.conn) as c:     # an older hub settled N, knowing nothing of the steer
        m = hosted._get(c, hosted._msg_key(sid, 1))
        hosted._put(c, hosted._msg_key(sid, 1), m | {"state": "done", "ok": True, "detail": "as the old hub"}, "x",
                    env.clock())
        r = hosted._session(c, sid)
        hosted._put(c, hosted.SESSION_PREFIX + sid, r | {"in_flight": None}, "x", env.clock())
    n_before = hosted._get(env.board.conn, hosted._msg_key(sid, 1))
    listing(env)
    assert (steer_rec(env, sid)["outcome"], steer_rec(env, sid)["why"]) == ("unconfirmed", "no_word")
    assert hosted._get(env.board.conn, hosted._msg_key(sid, 1)) == n_before


def test_g_a_settled_steer_is_pruned_with_ns_message_and_the_session(env):
    sid, child, req = steered(env)
    end_turn(env, child)
    with hosted.db.write_tx(env.board.conn) as c:
        c.execute("DELETE FROM board_state WHERE key = ?", (hosted._msg_key(sid, 1),))
        hosted_steer.prune_settled(c, sid)
    assert steer_rec(env, sid) is None
    other, c2 = codex_turn_in_flight(env, project=P2)
    post_steer(env, other)
    with hosted.db.write_tx(env.board.conn) as c:
        c.execute("DELETE FROM board_state WHERE key = ?", (hosted.SESSION_PREFIX + other,))
        hosted_steer.prune_orphans(c)
    assert steer_rec(env, other) is None


# ------------------------------------------------------------ the node: write authority (part E)


def test_e_the_contact_clock_is_75_s_of_acknowledged_send_times_and_listing_gets_alone_do_not_count(env):
    sid, child = codex_turn_in_flight(env, prove=False)
    post_steer(env, sid)
    tick(env)
    s = env.node.sessions.host.sessions[sid]
    assert s.steer is not None and s.steer.authorized and s.steer.revoked is None
    env.node.sessions.client = _GetsOnly(env.laptop)     # fetches go on; every report is lost
    env.clock.advance(node_sessions.STEER_CONTACT_SECONDS)
    tick(env)
    assert s.steer.revoked == "contact", "listing GETs alone do not renew it"
    env.node.sessions.client = env.laptop
    env.clock.advance(node_sessions.RETRY_SECONDS)
    env.node.hello()                                      # contact back: the loss is one-way
    st = last_start(child)
    child.emit(user_item(st["params"]["clientUserMessageId"]))
    tick(env)
    assert steers_of(child) == []
    end_turn(env, child)
    assert (steer_rec(env, sid)["outcome"], steer_rec(env, sid)["why"]) == ("not_delivered", "not_written")


class _GetsOnly:
    def __init__(self, hub):
        self.hub = hub

    def request(self, method, path, **kw):
        if method == "POST":
            raise httpx.ReadTimeout("lost")
        return self.hub.request(method, path, **kw)


def _record(sent=100.0):
    """(Review round 3, structure 1: these two tests now drive the attempt's own record, where the contact lives.)"""
    from agent_comms.session_host import SteerSlot
    return SteerSlot("u", "1", TEXT, session_id="s", cover_until=sent + node_sessions.STEER_CONTACT_SECONDS)


def test_e_an_acknowledged_report_naming_n_renews_it_and_one_naming_another_turn_does_not(env):
    slot = _record(100.0)
    slot.ack(100.0, lambda: 100.5, "s", 1)
    assert slot.contact_ok(150.0)
    slot.ack(160.0, lambda: 160.5, "s", 2)
    assert not slot.contact_ok(176.0), "a report whose answer names another turn is no evidence for N"


def test_r7_05_a_late_acknowledgement_cannot_erase_a_stale_interval(env):
    slot = _record(100.0)
    assert slot.contact_ok(170.0)
    slot.ack(180.0, lambda: 181.0, None, None)      # acknowledged later, SENT after the 75 s had run out
    assert not slot.contact_ok(181.0), "the interval [175, 180] was stale"


def test_r7_05_the_offers_age_is_read_on_the_monotonic_clock_when_its_answer_is_read(env):
    sid, child = codex_turn_in_flight(env)
    post_steer(env, sid)

    class Slow:
        def __init__(self, hub, clock):
            self.hub, self.clock = hub, clock

        def request(self, method, path, **kw):
            out = self.hub.request(method, path, **kw)
            if method == "GET":
                self.clock.advance(node_sessions.STEER_OFFER_AGE)   # the answer is read 20 s after it was sent
            return out
    env.node.sessions.client = Slow(env.laptop, env.clock)
    env.node.hello()
    tick(env)
    s = env.node.sessions.host.sessions[sid]
    assert s.steer is not None and not s.steer.authorized and steers_of(child) == []
    assert s.steer.revoked is None, "an old answer is unusable, not a loss: the next offer may be used"
    env.node.sessions.client = env.laptop
    tick(env)
    assert len(steers_of(child)) == 1


def test_e_the_status_is_posted_at_least_every_20_s_while_a_steer_is_held(env):
    sid, child = codex_turn_in_flight(env, prove=False)
    post_steer(env, sid)
    tick(env)
    runner = env.node.sessions
    first = runner.report_ack[sid][0]
    env.clock.advance(node_sessions.STEER_STATUS_SECONDS)
    tick(env)
    tick(env)
    assert runner.report_ack[sid][0] > first and runner.report_ack[sid][1] == 1


def test_o_a_hellos_contact_counts_from_when_it_was_sent_not_when_its_answer_came(env):
    """Part O: node.py's `last_hello` is a receipt time; the steer's clock takes the hello's SEND time, and a
    report's, so a slow hub never makes the node's authority look younger than the hub's evidence."""
    runner = env.node.sessions

    class Slow:
        def __init__(self, hub, clock):
            self.hub, self.clock = hub, clock

        def request(self, method, path, **kw):
            sent = self.clock()
            out = self.hub.request(method, path, **kw)
            self.clock.advance(30)          # the answer comes 30 s after the request was sent
            self.sent = sent
            return out
    slow = Slow(env.laptop, env.clock)
    env.node.client = slow
    env.node.hello()
    assert runner.hello_ack == slow.sent and env.node.last_hello == slow.sent + 30
    sid, child = codex_turn_in_flight(env, prove=False)
    runner.client = slow
    runner.reported.clear()
    runner._flush()
    assert runner.report_ack[sid][0] == slow.sent, "a report's contact is its send time too"


def test_e_the_watchdog_revokes_on_acknowledged_send_times(env):
    sid, child = codex_turn_in_flight(env, prove=False)
    post_steer(env, sid)
    tick(env)
    s = env.node.sessions.host.sessions[sid]
    env.clock.advance(node_sessions.STEER_CONTACT_SECONDS)
    env.node.sessions.enforce_deadlines()
    assert s.steer.revoked == "contact"


def test_e_the_node_holds_the_text_to_the_cap_again(env, monkeypatch):
    monkeypatch.setattr(node_sessions, "STEER_MAX_BYTES", 10)
    sid, child = codex_turn_in_flight(env)
    post_steer(env, sid)
    tick(env)
    s = env.node.sessions.host.sessions[sid]
    assert s.steer.refused == "too_big" and steers_of(child) == []
    end_turn(env, child)
    assert steer_rec(env, sid)["outcome"] == "not_delivered"


def test_e_a_listing_that_omits_the_steer_revokes_it(env):
    sid, child = codex_turn_in_flight(env, prove=False)
    post_steer(env, sid)
    tick(env)
    s = env.node.sessions.host.sessions[sid]
    with hosted.db.write_tx(env.board.conn) as c:
        hosted_steer.cancel(c, sid, 1, env.clock(), "x")    # the hub withdraws it at its next offer
    tick(env)
    assert s.steer.revoked == "omitted"


def test_j_the_extension_goes_only_to_a_hub_that_names_steer_and_ns_end_never_waits_for_it(env):
    runner = env.node.sessions
    ev = {"kind": "turn_ended", "message": 1, "steer": {"outcome": "in_turn", "why": "answered"}, "restarted_by": 2}
    runner.hub_features, runner.features_known = {"answered", "restart"}, True
    assert "steer" not in runner._for_hub(ev)
    runner.hub_features, runner.features_known = set(), False
    no_restart = {k: v for k, v in ev.items() if k != "restarted_by"}
    assert runner._takes(no_restart), "N's end is never held for feature discovery"
    runner.hub_features = {"steer"}
    assert runner._for_hub(ev)["steer"] == ev["steer"]


def test_j_modes_say_steer_only_to_a_hub_that_steers(env):
    sid, child = codex_turn_in_flight(env)
    assert hosted._session(env.board.conn, sid)["modes"] == ["steer", "restart"]
    runner = env.node.sessions
    runner.hub_features = {"answered", "restart"}
    runner.reported.clear()
    sent = []
    real = runner._call

    def spy(method, path, **kw):
        if method == "POST":
            sent.append(kw["json"].get("modes"))
        return real(method, path, **kw)
    runner._call = spy
    runner._flush()
    assert sent and sent[-1] == ["restart"]


# ------------------------------------------------------------ through the node, every outcome


def test_e2e_in_process_a_refused_steer_is_not_delivered_and_an_internal_error_unconfirmed(env):
    sid, child, req = steered(env)
    answer_steer(child, req, error={"code": -32600, "message": "no active turn to steer"})
    end_turn(env, child)
    assert (steer_rec(env, sid)["outcome"], steer_rec(env, sid)["why"]) == ("not_delivered", "refused")
    other, c2 = codex_turn_in_flight(env, project=P2)
    post_steer(env, other)
    tick(env)
    [req2] = steers_of(c2)
    answer_steer(c2, req2, error={"code": -32603, "message": "internal error"})
    end_turn(env, c2)
    assert (steer_rec(env, other)["outcome"], steer_rec(env, other)["why"]) == ("unconfirmed", "other_answer")


def test_e2e_in_process_a_node_restart_after_the_write_is_unconfirmed_and_never_resent(env):
    sid, child, req = steered(env)
    node2 = make_node(env, config=env.node.config, log_dir=env.tmp / "laptop-logs")
    node2.sessions.recover()
    node2.hello()
    for _ in range(3):
        tick(env, node2)
    assert (steer_rec(env, sid)["outcome"], steer_rec(env, sid)["why"]) == ("unconfirmed", "no_word")
    assert all(steers_of(c) == [] for c in node2.session_spawner.children)


def test_e2e_in_process_a_partition_after_the_write_ends_n_with_the_self_stop_unconfirmed(env):
    sid, child, req = steered(env)
    runner = env.node.sessions

    class Down:
        def request(self, *a, **kw):
            raise httpx.ConnectError("refused")
    runner.client = Down()
    env.clock.advance(runner.self_stop_seconds + 1)
    runner.enforce_deadlines()
    runner.client = env.laptop
    env.node.hello()
    tick(env)
    tick(env)
    m = msg(env, sid, 1)
    assert m["state"] == "failed", "the self-stop ended it (the hub hears of it as main reports it)"
    assert steer_rec(env, sid)["outcome"] == "unconfirmed", "written, and N ended with its process"


def test_e2e_in_process_shutdown_drains_a_steer_attempt_first(env):
    sid, child, req = steered(env)
    child.emit(cx.reply(req["id"], {"turnId": cx.TURN}), cx.message("done"), cx.completed())
    env.node.sessions.signal()
    env.node.sessions.shutdown()
    rec = steer_rec(env, sid)
    assert msg(env, sid, 1)["state"] == "done" and rec["outcome"] == "in_turn", "its native end, drained, counts"


# ------------------------------------------------------------ the parent's steer (H), its result and its wait


def codex_child(env, task="long task"):
    sid = root(env)
    answer(env, sid)
    grant(env, agents=("claude", "codex"))
    kid = spawn(env, bound(env, sid), task=task, agent="codex")["child"]["id"]
    tick(env)
    child = codex_of(env)
    codex_handshake(env, child)
    st = last_start(child)
    child.emit(cx.reply(st["id"], cx.TURN_REPLY), cx.TURN_STARTED, user_item(st["params"]["clientUserMessageId"]))
    tick(env)
    assert show(env, kid)["in_flight"] == 1
    return sid, kid, child


def test_h_a_parent_steers_its_own_codex_child_charged_one_turn_and_its_result_carries_the_outcome(env):
    sid, kid, child = codex_child(env)
    used = budget(env)["seq"]
    out = hs.send(env.board, env.p["claude"], bound(env, sid), kid, message=TEXT, mode="steer", request_id="p1")
    assert out["message"] is None and out["steer"]["into"] == 1 and out["created"] is True
    assert budget(env)["seq"] == used + 1, "one child turn at acceptance"
    rec = steer_rec(env, kid)
    assert rec["kind"] == "task" and hs.TASK_HEADER.split("{what}")[0] in rec["text"]
    assert hosted_steer.STEER_WHAT in rec["text"] and hosted_steer.STEER_LINE in rec["text"] and TEXT in rec["text"]
    again = hs.send(env.board, env.p["claude"], bound(env, sid), kid, message="other", mode="steer", request_id="p1")
    assert again["created"] is False and steer_rec(env, kid)["text"] == rec["text"]
    assert budget(env)["seq"] == used + 1, "a retry charges nothing"
    tick(env)
    [req] = steers_of(child)
    answer_steer(child, req, {"turnId": cx.TURN})
    end_turn(env, child, "the child's answer")
    [x] = [x for x in results_of(env, sid) if x["child"] == kid and x["message"] == 1]
    assert x["steer"] == {"outcome": "in_turn", "why": "answered"} and x["text"] == "the child's answer"
    text = hosted_results.render([x], token="0123456789abcdef")
    line = hosted_results.STEER_LINES["in_turn"]
    assert "\n" + line + "\n<<<child-output 0123456789abcdef>>>\nthe child's answer\n" in text, \
        "the hub's line is outside the child's markers, right before them"
    view = hs.status(env.board, env.p["claude"], bound(env, sid), kid)["children"][0]
    assert view["steer"]["outcome"] == "in_turn"


def test_h_a_parent_may_not_steer_a_turn_the_human_started_nor_the_human_one_the_parent_started(env):
    sid, kid, child = codex_child(env)
    r = env.human.post(f"/api/hosted-sessions/{kid}/messages", json={"message": "x", "mode": "steer"})
    assert r.status_code == 409 and hosted_steer.REFUSED["foreign_human"].format(n=1) in r.json()["message"]
    with pytest.raises(Conflict, match=re.escape(hosted_steer.REFUSED["spawn"])):
        hs.send(env.board, env.p["claude"], bound(env, sid), kid, message="x", mode="steer", may_spawn=False)
    with pytest.raises(Conflict, match=re.escape(hosted_steer.REFUSED["schema"])):
        hs.send(env.board, env.p["claude"], bound(env, sid), kid, message="x", mode="steer",
                output_schema='{"type": "object"}')
    end_turn(env, child)
    env.human.post(f"/api/hosted-sessions/{kid}/messages", json={"message": "the human's"})
    tick(env)
    st = last_start(child)
    child.emit(cx.reply(st["id"], cx.TURN_REPLY), cx.TURN_STARTED)
    tick(env)
    with pytest.raises(Conflict, match=re.escape(hosted.REFUSED["foreign"].format(n=2))):
        hs.send(env.board, env.p["claude"], bound(env, sid), kid, message="x", mode="steer")


def test_o_on_a_wait_timeout_the_steer_status_is_read_before_the_sender_is_told_it_is_unsettled(env, monkeypatch):
    sid, kid, child = codex_child(env)
    hs.send(env.board, env.p["claude"], bound(env, sid), kid, message=TEXT, mode="steer")
    order = []
    real_view = hosted_steer.view

    def view(s):
        order.append("read")
        return real_view(s)
    monkeypatch.setattr(hosted_steer, "view", view)
    out = hs.steer_waited(env.board, sid, kid, 1, None)
    assert order == ["read"] and out["note"] == hs.STEER_UNSETTLED and out["steer_status"]["state"] == "accepted"
    with hosted.db.write_tx(env.board.conn) as c:       # settled meanwhile: it says so, not "unsettled"
        hosted_steer._settled(c, hosted_steer.get(c, kid, 1), None, env.clock())
    out = hs.steer_waited(env.board, sid, kid, 1, None)
    assert out["note"] != hs.STEER_UNSETTLED and out["steer_status"]["outcome"] == "not_delivered"


def mcp_call(env, sid, monkeypatch, name, args):
    import asyncio
    from mcp import Client
    from agent_comms.mcp_server import build_mcp
    monkeypatch.setenv("AGENT_COMMS_TOKEN", env.tokens["claude"])
    monkeypatch.setenv(hs.HOSTED_ENV, sid)
    monkeypatch.setenv(hs.KEY_ENV, bound(env, sid).key)

    async def call():
        async with Client(build_mcp(env.board, "stdio")) as c:
            r = await c.call_tool(name, args)
            assert not r.is_error, r.content[0].text
            return json.loads(r.content[0].text)
    return asyncio.run(call())


def test_h_wait_true_through_mcp_takes_ns_result_with_the_steer_status(env, monkeypatch):
    sid, kid, child = codex_child(env)

    async def the_turn_ends_meanwhile(board, parent, by, child=None, message=None, wait=0, gone=None):
        tick(env)
        [req] = steers_of(codex_of(env))
        answer_steer(codex_of(env), req, {"turnId": cx.TURN})
        end_turn(env, codex_of(env), "answered")
        return {"result": hosted_results.claim(board, parent, by, child=child, message=message)}
    monkeypatch.setattr(hosted_results, "claim_wait", the_turn_ends_meanwhile)
    out = mcp_call(env, sid, monkeypatch, "board_session_send",
                   {"child": kid, "message": TEXT, "mode": "steer", "wait": True})
    assert out["message"] is None and out["steer"]["into"] == 1
    assert out["result"]["result"]["steer"] == {"outcome": "in_turn", "why": "answered"}
    assert out["steer_status"]["outcome"] == "in_turn" and "note" not in out


def test_h_wait_true_through_mcp_that_runs_out_says_unsettled_only_after_reading_the_status(env, monkeypatch):
    sid, kid, child = codex_child(env)

    async def nothing_yet(board, parent, by, child=None, message=None, wait=0, gone=None):
        return {"result": None}
    monkeypatch.setattr(hosted_results, "claim_wait", nothing_yet)
    out = mcp_call(env, sid, monkeypatch, "board_session_send",
                   {"child": kid, "message": TEXT, "mode": "steer", "wait": True})
    assert out["result"] is None and out["note"] == hs.STEER_UNSETTLED
    assert out["steer_status"]["state"] == "accepted"


# ------------------------------------------------------------ the human's views and the CLI (H)


def test_h_show_message_and_list_say_where_the_steer_stands(env):
    sid, child = codex_turn_in_flight(env)
    post_steer(env, sid)
    assert show(env, sid)["steer_unsettled"] == {"into": 1}
    listed_ = env.human.get("/api/hosted-sessions").json()["sessions"]
    assert next(s for s in listed_ if s["session_id"] == sid)["steer_unsettled"] == {"into": 1}
    assert "steer unsettled (into message 1)" in cli._fmt_hosted(show(env, sid))
    got = env.human.get(f"/api/hosted-sessions/{sid}/messages/1", params={"steer": 1}).json()
    assert got["steer"]["state"] == "accepted" and "text" not in got["steer"] and "uuid" not in got["steer"]
    plain = env.human.get(f"/api/hosted-sessions/{sid}/messages/1").json()
    assert "steer" not in plain, "without ?steer=1 the answer is main's"


@pytest.fixture
def steer_cli(env, monkeypatch):
    def api(settings, token, method, path, body=None, timeout=10):
        r = env.human.request(method, path, json=body)
        if r.status_code != 200:
            raise SystemExit(f"error ({r.json().get('code')}): {r.json().get('message')}")
        return r.json()
    monkeypatch.setattr(cli, "_api", api)
    monkeypatch.setattr(cli, "_human_token", lambda: "t")
    return env


def run_cli(*argv):
    try:
        cli.main(["session", *argv])
    except SystemExit as e:
        return e.code
    return 0


@pytest.mark.parametrize("outcome, code", [("in_turn", 0), ("not_delivered", 5), ("unconfirmed", 6)])
def test_h_send_mode_steer_wait_exits_0_5_6_and_prints_ns_reply(steer_cli, capsys, monkeypatch, outcome, code):
    env = steer_cli
    sid, child = codex_turn_in_flight(env)
    real = hosted.message_wait

    async def settle_then(board, p, session_id, n, wait=0, gone=None, steer=False):
        if steer_rec(env, sid)["outcome"] is None:
            entry(env, sid)
            report_end(env, sid, {"outcome": outcome, "why": "answered" if outcome == "in_turn" else
                                  "refused" if outcome == "not_delivered" else "no_answer"})
        return await real(board, p, session_id, n, wait, gone, steer=steer)
    monkeypatch.setattr(hosted, "message_wait", settle_then)
    assert run_cli("send", sid, TEXT, "--mode", "steer", "--wait") == code
    out = capsys.readouterr()
    assert out.out.strip() == "r" and f"steer: {cli.STEER_WORDS[outcome]}" in out.err
    assert (TEXT in out.err) == (outcome == "not_delivered"), "the text this call sent, to send again"


def test_h_send_mode_steer_wait_exits_1_when_n_failed(steer_cli, capsys, monkeypatch):
    env = steer_cli
    sid, child = codex_turn_in_flight(env)
    real = hosted.message_wait

    async def settle_then(board, p, session_id, n, wait=0, gone=None, steer=False):
        if steer_rec(env, sid)["outcome"] is None:
            entry(env, sid)
            report_end(env, sid, {"outcome": "in_turn", "why": "answered"}, ok=False)
        return await real(board, p, session_id, n, wait, gone, steer=steer)
    monkeypatch.setattr(hosted, "message_wait", settle_then)
    assert run_cli("send", sid, TEXT, "--mode", "steer", "--wait") == 1


def test_h_output_schema_with_mode_steer_is_refused_before_sending(steer_cli, capsys):
    env = steer_cli
    sid, child = codex_turn_in_flight(env)
    code = run_cli("send", sid, TEXT, "--mode", "steer", "--output-schema", '{"type": "object"}')
    assert isinstance(code, str) and "does not go with --mode steer" in code
    assert steer_rec(env, sid) is None, "nothing was sent"


def test_h_the_frame_of_a_parents_steer(env):
    text = hs.render_task("session X (agent claude)", TEXT, steer=True, token="0123456789abcdef")
    assert text.startswith("[agent-comms] " + hosted_steer.STEER_WHAT + " from session X")
    assert text.splitlines()[1] == hosted_steer.STEER_LINE
    assert hs.render_task("w", TEXT, follow_up=True, token="0123456789abcdef") == hs.render_task(
        "w", TEXT, follow_up=True, token="0123456789abcdef", steer=False), "the follow-up frame is main's"


def test_the_setting_is_read_strictly_and_on_by_default():
    from agent_comms.dispatch import SessionsConfig
    assert SessionsConfig().steer is True and SessionsConfig.from_dict({"steer": False}).steer is False
    with pytest.raises(ValueError):
        SessionsConfig.from_dict({"steer": "no"})


def test_a_malformed_extension_never_refuses_the_report(env):
    sid, child = codex_turn_in_flight(env)
    r = env.laptop.post(f"/api/nodes/sessions/{sid}", json={"events": [
        {"kind": "turn_ended", "message": 1, "ok": True, "text": "r", "steer": [1, 2, 3]}]})
    assert r.status_code == 200 and msg(env, sid, 1)["state"] == "done"
    assert hosted_steer.extension({"outcome": "in_turn"}) is None
    with pytest.raises(Invalid):
        hosted.send_mode(None, {"state": "idle"}, "steer", by_parent=False)


# ------------------------------------------------------------ an older hub (J, K's proof)


OLDER = "d4b92e5"       # main's code before steering (the golden's pin), run for real below


def test_k_an_older_hub_takes_ns_stripped_end_behind_its_real_report_route_and_the_sweep_settles_s(env, tmp_path):
    """J, K, S2-07: a hub downgraded with a steer unsettled, while the node still believes it names `steer`. Main's
    own code (exported from git) runs as a real HTTP server on this hub's database; the node's final shutdown flush
    sends N's end WITH the extension, main's strict route refuses it (422), and the node's own retry, in that same
    flush, sends it without (nothing here strips it by hand). Its records are what main makes of any end: the
    message, the session, the parent's result and the parent's turn text, with no steer anywhere. Back on this hub,
    the sweep settles S by the rule and leaves N as it was."""
    import socket
    import time as _time
    import subprocess
    import sys
    from pathlib import Path
    from agent_comms.session_host import HostEvent
    root_dir = Path(__file__).resolve().parents[1]
    if subprocess.run(["git", "-C", str(root_dir), "cat-file", "-e", OLDER + "^{commit}"],
                      capture_output=True).returncode != 0:
        pytest.skip(f"main's commit {OLDER} is not in this clone")
    sid, kid, child = codex_child(env)
    hs.send(env.board, env.p["claude"], bound(env, sid), kid, message=TEXT, mode="steer")
    tick(env)
    assert steer_rec(env, kid)["listed_at"] is not None and len(steers_of(child)) == 1
    runner = env.node.sessions
    old = tmp_path / "main"
    old.mkdir()
    archive = subprocess.run(["git", "-C", str(root_dir), "archive", OLDER, "agent_comms"], capture_output=True,
                             check=True).stdout
    subprocess.run(["tar", "-x", "-C", str(old)], input=archive, check=True)
    assert not (old / "agent_comms" / "hosted_steer.py").exists(), "main's code, without this change"
    boot = ("import json, sys\n"
            "from pathlib import Path\n"
            "import uvicorn\n"
            "from agent_comms.api import create_app\n"
            "from agent_comms.config import Settings\n"
            "from agent_comms.core import Board\n"
            "s = Settings(db_path=Path(sys.argv[1]), agents_path=Path(sys.argv[2]))\n"
            "s.dispatch = json.loads(sys.argv[3])\n"
            "uvicorn.run(create_app(Board(s)), host='127.0.0.1', port=int(sys.argv[4]), log_level='warning')\n")
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    envs = {"PYTHONPATH": str(old), "PATH": "/usr/bin:/bin", "HOME": str(tmp_path)}
    hub = subprocess.Popen([sys.executable, "-c", boot, str(env.settings.db_path), str(env.settings.agents_path),
                            json.dumps(env.board.s.dispatch), str(port)], cwd=str(old), env=envs,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        client = httpx.Client(base_url=f"http://127.0.0.1:{port}",
                              headers={"Authorization": "Bearer " + env.tokens["laptop"]}, timeout=10)
        deadline = _time.monotonic() + 60
        while _time.monotonic() < deadline:
            try:
                client.get("/api/whoami")
                break
            except httpx.HTTPError:
                _time.sleep(0.2)
        posts = []
        real_request = client.request

        def spy(method, path, **kw):
            r = real_request(method, path, **kw)
            if method == "POST":
                posts.append((r.status_code, [("steer" in e) for e in (kw.get("json") or {}).get("events", [])]))
            return r
        client.request = spy
        runner.client = client
        runner.hub_features, runner.features_known = {"answered", "restart", "steer"}, True    # as last named
        runner.status.pop(kid, None)
        runner._emit([HostEvent(kid, "turn_ended", "1", ok=True, text="the child's answer", answered=True,
                                steer={"outcome": "in_turn", "why": "answered"})])
        runner.stopping.set()
        assert runner._flush(final=True), "the final flush got N's end through"
        assert posts[:2] == [(422, [True]), (200, [False])], posts
    finally:
        hub.terminate()
        hub.wait(30)
    code = ("import json, sys\n"
            "from pathlib import Path\n"
            "from agent_comms.config import Settings\n"
            "from agent_comms.core import Board\n"
            "from agent_comms import hosted_results\n"
            "b = Board(Settings(db_path=Path(sys.argv[1]), agents_path=Path(sys.argv[2])))\n"
            "x = [x for x in hosted_results._results(b.conn, sys.argv[3]) if x.get('message') == 1]\n"
            "print(json.dumps({'text': hosted_results.render(x, token='0123456789abcdef')}))\n")
    r = subprocess.run([sys.executable, "-c", code, str(env.settings.db_path), str(env.settings.agents_path), sid],
                       cwd=str(old), capture_output=True, text=True, env=envs, timeout=120)
    assert r.returncode == 0, r.stderr[-2000:]
    turn_text = json.loads(r.stdout.strip().splitlines()[-1])["text"]
    m = hosted._get(env.board.conn, hosted._msg_key(kid, 1))
    assert (m["state"], m["ok"]) == ("done", True) and hosted._session(env.board.conn, kid)["reply"] == \
        "the child's answer"
    [x] = [x for x in results_of(env, sid) if x["child"] == kid and x["message"] == 1]
    assert x["outcome"] == "completed" and x["text"] == "the child's answer" and "steer" not in x
    assert "<<<child-output 0123456789abcdef>>>\nthe child's answer\n" in turn_text and "steer" not in turn_text
    assert steer_rec(env, kid)["outcome"] is None, "the older hub never reads a steer"
    listing(env)                        # back on this hub: the re-upgrade sweep
    rec = steer_rec(env, kid)
    assert (rec["outcome"], rec["why"]) == ("unconfirmed", "no_word")
    assert hosted._get(env.board.conn, hosted._msg_key(kid, 1)) == m, "N stays as the older hub settled it"


# ------------------------------------------------------------ the checks the mutation check found untested


def test_h_refused_once_the_session_has_ended(env):
    sid, child = codex_turn_in_flight(env)
    with hosted.db.write_tx(env.board.conn) as c:
        r = hosted._session(c, sid)
        hosted._end(c, r, "ended here", "hub", env.clock())
        hosted._put(c, hosted.SESSION_PREFIX + sid, r | {"stop": None}, "hub", env.clock())
    refusal(env, sid, hosted_steer.REFUSED["ended"])


def test_h_refused_while_the_record_names_a_turn_in_flight_that_is_settled(env):
    """A racy read: the session names message 1 in flight, and that message is already settled: no turn is in
    flight."""
    sid, child = codex_turn_in_flight(env)
    with hosted.db.write_tx(env.board.conn) as c:
        m = hosted._get(c, hosted._msg_key(sid, 1))
        hosted._put(c, hosted._msg_key(sid, 1), m | {"state": "done"}, "hub", env.clock())
    refusal(env, sid, hosted.REFUSED["idle"])


def test_h_refused_while_the_turn_is_being_cancelled(env):
    sid, kid, child = codex_child(env)
    hs.cancel(env.board, env.p["claude"], bound(env, sid), kid)
    with pytest.raises(Conflict, match=re.escape(hosted.REFUSED["cancelled"].format(n=1))):
        hs.send(env.board, env.p["claude"], bound(env, sid), kid, message="x", mode="steer")


def test_g_a_steer_into_a_session_being_closed_is_withdrawn_at_its_next_offer(env):
    sid, child = codex_turn_in_flight(env)
    post_steer(env, sid)
    env.human.post(f"/api/hosted-sessions/{sid}/close")
    assert "steer" not in entry(env, sid)
    assert steer_rec(env, sid)["withdrawn"] == "ended"


def test_g_a_steer_whose_n_is_no_longer_the_turn_in_flight_is_withdrawn(env):
    sid, child = codex_turn_in_flight(env)
    post_steer(env, sid)
    with hosted.db.write_tx(env.board.conn) as c:
        r = hosted._session(c, sid)
        hosted._put(c, hosted.SESSION_PREFIX + sid, r | {"in_flight": None}, "hub", env.clock())
    assert "steer" not in entry(env, sid)
    assert steer_rec(env, sid)["withdrawn"] == "not_in_flight"


def test_g_an_offer_whose_transaction_fails_after_it_was_made_is_shown_by_no_later_build(env, monkeypatch):
    """The build inside the listing's write transaction fails after the offer was made: the transaction rolls back
    and the wake fallback builds the answer again, without that offer (and listed_at is not set)."""
    sid, child = codex_turn_in_flight(env)
    post_steer(env, sid)
    real = hosted._listed
    calls = []

    def once(board, p, tree=False, shown=None):
        calls.append(shown)
        if len(calls) == 1:
            raise RuntimeError("the first build fails")
        return real(board, p, tree, shown)
    monkeypatch.setattr(hosted, "_listed", once)
    got = entry(env, sid)
    assert calls[0] and "steer" not in got and steer_rec(env, sid)["listed_at"] is None


def test_g_settle_never_revises_a_settled_steer(env):
    sid, child = codex_turn_in_flight(env)
    post_steer(env, sid)
    with hosted.db.write_tx(env.board.conn) as c:
        r = hosted._session(c, sid)
        first = dict(hosted_steer.settle(c, r, 1, None, env.clock()))
        again = hosted_steer.settle(c, r, 1, {"outcome": "in_turn", "why": "answered"}, env.clock() + 5)
    assert (first["outcome"], again["outcome"], again["settled_at"]) == ("not_delivered", "not_delivered",
                                                                         first["settled_at"])


def test_h_a_parents_retry_inside_the_transaction_answers_the_same_steer(env, monkeypatch):
    """The retry lookup in the write transaction (after the fast path missed, as for a concurrent retry)."""
    sid, kid, child = codex_child(env)
    hs.send(env.board, env.p["claude"], bound(env, sid), kid, message=TEXT, mode="steer", request_id="p2")
    used = budget(env)["seq"]
    monkeypatch.setattr(hs, "_resent", lambda *a, **kw: None)
    again = hs.send(env.board, env.p["claude"], bound(env, sid), kid, message="other", mode="steer", request_id="p2")
    assert again["created"] is False and again["steer"]["into"] == 1 and budget(env)["seq"] == used


def test_e_the_node_watchdog_cuts_a_half_written_frame_past_its_limit(env):
    sid, child = codex_turn_in_flight(env)
    child.accept = 10
    post_steer(env, sid)
    tick(env)
    s = env.node.sessions.host.sessions[sid]
    assert s.steer is not None and 0 < s.steer.sent < s.steer.length
    env.clock.advance(STEER_PARTIAL_SECONDS)
    env.node.sessions.enforce_deadlines()
    assert s.stopping_since is not None and any(e.detail == STEER_CUT for e in env.node.sessions.watched_steer)


def test_d_the_watchdogs_cut_reads_what_the_process_wrote_first(henv):
    """Between passes (the watchdog's `steer_deadlines`), a native end already in the pipe is read before the
    process is stopped: N keeps its own result."""
    host, s, child, _ = partial(henv)
    henv.clock.advance(STEER_PARTIAL_SECONDS)
    child.emit(cx.message("finished first"), cx.completed())
    [end] = ended_of(host.steer_deadlines())
    assert end.ok and end.text == "finished first" and s.stopping_since is not None


def test_the_cells_table_is_the_one_in_design_notes():
    from pathlib import Path
    notes = (Path(__file__).resolve().parents[1] / "DESIGN_NOTES.md").read_text()
    assert f"Steer cells: {len(STEER_CELLS)}" in notes


# ------------------------------------------------------------ review round 1 (S2-01 .. S2-14, and part O's gaps)


def held_slot(env, prove=False):
    """A Codex turn in flight on the node, a human steer offered and held there (not written unless proved)."""
    sid, child = codex_turn_in_flight(env, prove=prove)
    post_steer(env, sid)
    tick(env)
    s = env.node.sessions.host.sessions[sid]
    assert s.steer is not None and s.steer.authorized
    return sid, child, s


def test_s2_03_an_ack_sent_before_the_deadline_and_read_after_it_never_revives_authority(env):
    """A report SENT at 74 s and acknowledged at 76 s: the authority, resting on contact sent at 0 s, ran out at 75 s
    and is latched as lost before that acknowledgement counts."""
    sid, child, s = held_slot(env)
    runner = env.node.sessions
    t0 = runner.steer_seen[sid]
    runner.hello_ack, runner.report_ack[sid] = t0, (t0, 1)
    env.clock.advance(node_sessions.STEER_CONTACT_SECONDS + 1)       # 76 s after the contact
    runner._ack_report(sid, t0 + 74, {"session_id": sid, "in_flight": 1})
    assert s.steer.revoked == "contact", "latched before the late acknowledgement was published"
    runner.note_hello(t0 + 74)
    assert s.steer.revoked == "contact"


def test_s2_03_an_ack_names_exactly_this_session_and_an_integer_n(env):
    sid, child, s = held_slot(env)
    runner = env.node.sessions
    for answer in ({"session_id": sid, "in_flight": True}, {"session_id": "another", "in_flight": 1},
                   {"session_id": sid, "in_flight": 1.0}, {"in_flight": 1}, None):
        runner._ack_report(sid, 1.0, answer)
        assert runner.report_ack[sid][1] is None, answer
        assert runner._steer_contact(sid, 1, float("-inf"), runner.report_ack) == float("-inf")
    assert runner._steer_contact(sid, True, float("-inf"), {sid: (1.0, 1)}) == float("-inf"), "N is an integer too"


def test_s2_03_a_real_report_is_acknowledged_for_its_own_session_and_n(env):
    """Part O, a real report identity check: the hub's own answer to the node's report names the session and N."""
    sid, child, s = held_slot(env)
    runner = env.node.sessions
    runner.report_ack.clear()
    runner.reported.clear()
    runner._flush()
    sent, n = runner.report_ack[sid]
    assert n == 1 and type(n) is int and sent <= env.clock()


def test_s2_04_a_zero_byte_frame_whose_gate_fails_is_revoked_and_a_recovered_gate_never_writes_it(henv):
    host, s, child, _ = host_n(henv)
    child.accept = 0
    host.steer_offer(SID, "a", "u1", TEXT, authorized=True)
    host.tick()
    assert s.steer.mark is not None and s.steer.sent == 0
    host.turn_gate = lambda sess: False
    host.tick()
    assert s.steer.revoked == "gate" and s.steer.mark is None and b"turn/steer" not in bytes(s.outbuf)
    host.turn_gate, child.accept = None, None       # the gate recovers
    host.tick()
    host.tick()
    assert steers_of(child) == [] and s.provider.steer(TEXT) is None, "never written, no second request"
    child.emit(cx.message("done"), cx.completed())
    assert ended_of(host.tick())[0].steer == {"outcome": "not_delivered", "why": "not_written"}


class _Featureless:
    """The node's client, with the listing's `features` taken out (an older hub, or a capability not known)."""

    def __init__(self, hub, drop_steer_only=False):
        self.hub, self.only = hub, drop_steer_only

    def request(self, method, path, **kw):
        r = self.hub.request(method, path, **kw)
        if method == "GET" and path == "/api/nodes/sessions" and r.status_code == 200:
            data = r.json()
            data["features"] = [f for f in data.get("features", []) if f != "steer"] if self.only else None
            return httpx.Response(200, json=data, request=r.request)
        return r


@pytest.mark.parametrize("only", [False, True])
def test_s2_05_a_listing_that_does_not_name_steer_fences_and_revokes_nothing(env, only):
    sid, child, s = held_slot(env)
    env.node.sessions.client = _Featureless(env.laptop, drop_steer_only=only)
    st = last_start(child)
    child.emit(user_item(st["params"]["clientUserMessageId"]))     # proved now
    tick(env)
    assert s.steer.fenced and s.steer.revoked is None and steers_of(child) == [], "fenced: nothing written"
    env.node.sessions.client = env.laptop
    tick(env)
    assert not s.steer.fenced and len(steers_of(child)) == 1, "an authoritative listing still lists it: written"


def test_s2_05_a_featureless_listing_carrying_an_offer_is_taken_by_nobody(env):
    sid, child = codex_turn_in_flight(env)
    post_steer(env, sid)

    class Offering(_Featureless):
        pass
    env.node.sessions.client = Offering(env.laptop)
    tick(env)
    assert env.node.sessions.host.sessions[sid].steer is None and steers_of(child) == []


def test_s2_05_a_fenced_zero_byte_frame_goes_later_as_the_same_request(henv):
    host, s, child, _ = host_n(henv)
    child.accept = 0
    host.steer_offer(SID, "a", "u1", TEXT, authorized=True)
    host.tick()
    first = bytes(s.steer.frame)
    host.steer_fence(SID)
    host.tick()
    assert s.steer.mark is None and s.steer.revoked is None and s.steer.frame == first
    child.accept = None
    host.tick()
    assert steers_of(child) == [], "nothing while fenced"
    host.steer_offer(SID, "a", "u1", TEXT, authorized=True)        # an authoritative listing lists it again
    host.tick()
    [req] = steers_of(child)
    assert json.dumps(req).encode() + b"\n" == first or req["id"] == json.loads(first)["id"], "the same request"


def test_s2_05_and_r11_03_a_full_write_re_offered_through_reconciliation_is_never_written_again(env):
    sid, child = codex_turn_in_flight(env)
    post_steer(env, sid)
    tick(env)
    assert len(steers_of(child)) == 1
    for _ in range(3):
        tick(env)
    assert len(steers_of(child)) == 1, "the hub offers it again (N runs): the host never writes it twice"


def test_s2_05_an_authoritative_omission_still_revokes_for_good(env):
    sid, child, s = held_slot(env)
    with hosted.db.write_tx(env.board.conn) as c:
        hosted_steer.cancel(c, sid, 1, env.clock(), "x")
    tick(env)
    assert s.steer.revoked == "omitted"


def test_s2_06_a_native_end_the_watchdog_drained_reaches_the_hub_before_any_lost(env):
    """Part O's watchdog-to-hub ordering and the cut with the receipt and the native end both in stdout."""
    sid, child = codex_turn_in_flight(env)
    child.accept = 10
    post_steer(env, sid)
    tick(env)
    [req] = [json.loads(x) for x in bytes(env.node.sessions.host.sessions[sid].steer.frame).splitlines()]
    child.emit(cx.reply(req["id"], {"turnId": cx.TURN}), cx.message("native"), cx.completed())
    env.clock.advance(STEER_PARTIAL_SECONDS)
    env.node.sessions.enforce_deadlines()       # the watchdog drains and cuts, between passes
    tick(env)
    m = msg(env, sid, 1)
    assert m["state"] == "done" and show(env, sid)["reply"] == "native", "N's own end, not a synthetic LOST"
    assert (steer_rec(env, sid)["outcome"], steer_rec(env, sid)["why"]) == ("unconfirmed", "partial")


@pytest.mark.parametrize("how", ["close", "cancel", "stop_turns", "stop_all"])
def test_s2_06_a_direct_stop_drains_a_live_steer_attempt_first(henv, how):
    host, s, child, _ = host_n(henv)
    host.steer_offer(SID, "a", "u1", TEXT, authorized=True)
    host.tick()
    [req] = steers_of(child)
    child.emit(cx.reply(req["id"], {"turnId": cx.TURN}), cx.message("native"), cx.completed())
    events = (host.close(SID, "closed") if how == "close" else host.cancel(SID, "a", "cancelled") if how == "cancel"
              else host.stop_turns("no contact") if how == "stop_turns" else host.stop_all())
    [end] = ended_of(events)
    assert end.ok and end.text == "native" and end.steer == {"outcome": "in_turn", "why": "answered"}


def test_s2_06_shutdown_drains_a_native_end_larger_than_one_read(henv):
    """Part O: a native end that takes many reads (one line per read here) is drained whole."""
    host, s, child, _ = host_n(henv)
    host.steer_offer(SID, "a", "u1", TEXT, authorized=True)
    host.tick()
    [req] = steers_of(child)

    def one_at_a_time():
        return [child.stdout.popleft()] if child.stdout else []
    child.read_lines = one_at_a_time
    child.emit(cx.reply(req["id"], {"turnId": cx.TURN}), *[cx.message(f"note {i}", phase="commentary")
                                                           for i in range(40)], cx.message("native"), cx.completed())
    [end] = ended_of(host.stop_all())
    assert end.ok and end.text == "native" and end.steer["outcome"] == "in_turn"


def test_o_a_codex_partial_cut_a_close_and_a_late_reap_end_n_once(henv):
    host, s, child, _ = partial(henv)
    henv.clock.advance(STEER_PARTIAL_SECONDS)
    events = host.steer_deadlines()
    events += host.close(SID, "closed")
    child.code = 0
    events += host.tick()
    events += host.tick()
    ends = [e for e in events if e.kind == "turn_ended"]
    assert len(ends) == 1 and ends[0].steer == {"outcome": "unconfirmed", "why": "partial"}
    assert [e.kind for e in events].count("ended") == 1


def test_s2_07_a_refused_shutdown_report_goes_again_without_the_steer_outcome_in_that_flush(env):
    from agent_comms.session_host import HostEvent
    runner = env.node.sessions
    sid, child = codex_turn_in_flight(env)
    posts = []

    class RefusesSteer:
        def __init__(self, hub):
            self.hub = hub

        def request(self, method, path, **kw):
            if method == "POST" and any("steer" in e for e in (kw.get("json") or {}).get("events", [])):
                posts.append("refused")
                return httpx.Response(422, json={"detail": "extra field"},
                                      request=httpx.Request(method, "http://hub" + path))
            posts.append("taken")
            return self.hub.request(method, path, **kw)
    runner.client = RefusesSteer(env.laptop)
    runner.hub_features, runner.features_known = {"answered", "restart", "steer"}, True
    runner._emit([HostEvent(sid, "turn_ended", "1", ok=True, text="the end", answered=True,
                            steer={"outcome": "unconfirmed", "why": "turn_cut"})])
    assert runner._flush(final=True)
    assert posts[:2] == ["refused", "taken"] and msg(env, sid, 1)["state"] == "done"
    assert show(env, sid)["reply"] == "the end" and runner.pending == []


def two_steered_roots(env):
    a, ca = codex_turn_in_flight(env)
    b, cb = codex_turn_in_flight(env, project=P2)
    post_steer(env, a)
    post_steer(env, b)
    return a, b


def test_s2_08_offer_writes_are_all_or_nothing(env, monkeypatch):
    a, b = two_steered_roots(env)
    real = hosted_steer._offer_problem
    calls = []

    def second_fails(*args):
        calls.append(1)
        if len(calls) == 2:
            raise RuntimeError("a fault on the second steer")
        return real(*args)
    monkeypatch.setattr(hosted_steer, "_offer_problem", second_fails)
    got = listing(env)
    assert all("steer" not in x for x in got["sessions"])
    assert steer_rec(env, a)["listed_at"] is None and steer_rec(env, b)["listed_at"] is None


def test_s2_08_listed_at_only_for_an_entry_rendered_into_the_answer(env, monkeypatch):
    """An entry the renderer leaves out (here: the request's protocol below 5 at render time) is never stamped."""
    sid, child = codex_turn_in_flight(env)
    post_steer(env, sid)
    monkeypatch.setattr(hosted_steer, "renderable", lambda c, r, tree: False)
    got = entry(env, sid)
    assert "steer" not in got and steer_rec(env, sid)["listed_at"] is None


def test_s2_08_an_interactive_child_the_renderer_excludes_is_never_stamped(env):
    sid, child = codex_turn_in_flight(env)
    post_steer(env, sid)
    with hosted.db.write_tx(env.board.conn) as c:
        r = hosted._session(c, sid)
        hosted._put(c, hosted.SESSION_PREFIX + sid, r | {"parent": "0" * 8 + "-0000-4000-8000-" + "0" * 12,
                                                         "parent_kind": "interactive"}, "hub", env.clock())
    env.board.s.dispatch = json.loads(json.dumps(env.board.s.dispatch))
    env.board.s.dispatch["sessions"]["spawn"]["enabled"] = False      # such a child is not listed then
    got = listing(env)
    assert all(x["session_id"] != sid for x in got["sessions"])
    assert steer_rec(env, sid)["listed_at"] is None


@pytest.mark.parametrize("what", ["modes", "recorded"])
def test_s2_08_modes_and_the_recorded_protocol_are_rechecked_at_every_offer(env, what):
    sid, child = codex_turn_in_flight(env)
    post_steer(env, sid)
    if what == "modes":
        with hosted.db.write_tx(env.board.conn) as c:
            r = hosted._session(c, sid)
            hosted._put(c, hosted.SESSION_PREFIX + sid, r | {"modes": ["restart"]}, "hub", env.clock())
    else:
        hub_node(env, 4)
    assert "steer" not in entry(env, sid)
    assert steer_rec(env, sid)["withdrawn"] == ("modes" if what == "modes" else "protocol")


def test_s2_09_discovering_steer_resends_only_a_session_whose_report_gains_it(env):
    claude = root(env)
    call, proc = proc_of(env, claude)
    proc.emit(began(claude, *as_started(call["argv"])) | {"capabilities": ["interrupt_receipt_v1"]})
    tick(env)
    cdx, child = codex_turn_in_flight(env, project=P2)
    runner = env.node.sessions
    runner.hub_features = {"answered", "restart"}        # the hub, before it named steer
    runner.reported = dict(runner.status)
    posted = []
    real = runner._call

    def spy(method, path, **kw):
        if method == "POST":
            posted.append(path.rsplit("/", 1)[-1])
        return real(method, path, **kw)
    runner._call = spy
    claude_seq = hosted._session(env.board.conn, claude).get("status_seq")
    tick(env)                                             # a listing that names steer
    assert cdx in posted and claude not in posted, "only Codex's report gains `steer`"
    assert hosted._session(env.board.conn, claude).get("status_seq") == claude_seq


def test_s2_10_the_drain_list_marks_an_unsettled_steer_on_a_child_row(env):
    sid, kid, child = codex_child(env)
    hs.send(env.board, env.p["claude"], bound(env, sid), kid, message=TEXT, mode="steer")
    text = cli._fmt_hosted_tree(env.human.get("/api/hosted-sessions").json()["sessions"])
    kid_line = next(line for line in text.splitlines() if kid in line)
    assert kid_line.startswith(" ") and "steer unsettled (into message 1)" in kid_line


def test_s2_11_a_steer_whose_n_is_terminal_settles_on_a_read_with_its_node_offline(env):
    sid, child = codex_turn_in_flight(env)
    post_steer(env, sid)
    entry(env, sid)
    with hosted.db.write_tx(env.board.conn) as c:     # an older hub settled N; the node is gone since
        m = hosted._get(c, hosted._msg_key(sid, 1))
        hosted._put(c, hosted._msg_key(sid, 1), m | {"state": "done", "ok": True}, "x", env.clock())
        r = hosted._session(c, sid)
        hosted._put(c, hosted.SESSION_PREFIX + sid, r | {"in_flight": None}, "x", env.clock())
    view = show(env, sid)
    assert view["messages"][0]["steer"]["outcome"] == "unconfirmed" and "steer_unsettled" not in view


def test_s2_11_the_cli_wait_never_exits_0_without_a_terminal_steer_status(steer_cli, capsys, monkeypatch):
    env = steer_cli
    sid, child = codex_turn_in_flight(env)
    monkeypatch.setattr(cli, "STEER_SETTLE_POLLS", 1)
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    real = hosted.message_wait

    async def n_done_steer_open(board, p, session_id, n, wait=0, gone=None, steer=False):
        out = await real(board, p, session_id, n, wait, gone, steer=steer)
        out["message"] = out["message"] | {"state": "done"}
        out["steer"] = (out.get("steer") or {}) | {"outcome": None, "state": "accepted"}
        return out
    monkeypatch.setattr(hosted, "message_wait", n_done_steer_open)
    assert run_cli("send", sid, TEXT, "--mode", "steer", "--wait") == cli.STEER_UNSETTLED_EXIT
    assert "steer: not settled" in capsys.readouterr().err


def test_s2_12_a_receipt_only_retry_with_wait_does_not_wait(steer_cli, capsys, monkeypatch):
    env = steer_cli
    sid, child = codex_turn_in_flight(env)
    post_steer(env, sid, request_id="e1")
    with hosted.db.write_tx(env.board.conn) as c:
        c.execute("DELETE FROM board_state WHERE key = ?", (hosted_steer._key(sid, 1),))

    async def never(*a, **kw):
        raise AssertionError("nothing is waited for")
    monkeypatch.setattr(hosted, "message_wait", never)
    assert run_cli("send", sid, TEXT, "--mode", "steer", "--request-id", "e1", "--wait") == cli.STEER_UNSETTLED_EXIT


def test_s2_12_an_expired_mcp_retry_does_not_wait(env, monkeypatch):
    sid, kid, child = codex_child(env)
    hs.send(env.board, env.p["claude"], bound(env, sid), kid, message=TEXT, mode="steer", request_id="x1")
    with hosted.db.write_tx(env.board.conn) as c:
        c.execute("DELETE FROM board_state WHERE key = ?", (hosted_steer._key(kid, 1),))

    async def never(*a, **kw):
        raise AssertionError("nothing is waited for")
    monkeypatch.setattr(hosted_results, "claim_wait", never)
    out = mcp_call(env, sid, monkeypatch, "board_session_send",
                   {"child": kid, "message": "other", "mode": "steer", "request_id": "x1", "wait": True})
    assert out["created"] is False and out["steer"]["state"] == "expired" and out["note"] == hs.STEER_EXPIRED
    assert out["steer_status"] is None


def test_s2_12_status_shows_every_retained_steer_by_n(env):
    sid, kid, child = codex_child(env)
    hs.send(env.board, env.p["claude"], bound(env, sid), kid, message=TEXT, mode="steer")
    tick(env)
    end_turn(env, child)
    hs.send(env.board, env.p["claude"], bound(env, sid), kid, message="again")
    tick(env)
    st = last_start(child)
    child.emit(cx.reply(st["id"], cx.TURN_REPLY), cx.TURN_STARTED, user_item(st["params"]["clientUserMessageId"]))
    tick(env)
    hs.send(env.board, env.p["claude"], bound(env, sid), kid, message="second steer", mode="steer")
    view = hs.status(env.board, env.p["claude"], bound(env, sid), kid)["children"][0]
    assert set(view["steers"]) == {"1", "2"} and view["steer"]["into"] == 2


def test_s2_12_the_wait_reads_status_and_disposition_in_one_snapshot(env, monkeypatch):
    """Part O, a wait with a concurrent settlement: another connection settles the steer between the two reads; the
    answer pairs what one snapshot holds (still accepted, still unsettled), never an accepted steer with a later
    disposition."""
    import sqlite3
    sid, kid, child = codex_child(env)
    hs.send(env.board, env.p["claude"], bound(env, sid), kid, message=TEXT, mode="steer")
    real = hosted_results.claimed

    def settle_meanwhile(conn, parent, child_id, n):
        other = sqlite3.connect(env.settings.db_path, timeout=10)
        s = json.loads(other.execute("SELECT value FROM board_state WHERE key = ?",
                                     (hosted_steer._key(kid, 1),)).fetchone()[0])
        s |= {"outcome": "in_turn", "why": "answered"}
        other.execute("UPDATE board_state SET value = ? WHERE key = ?", (json.dumps(s), hosted_steer._key(kid, 1)))
        # ... and N's result is delivered to the parent as a turn, by that same other writer
        x = {"parent": sid, "n": 99, "child": kid, "message": 1, "state": "delivered", "outcome": "completed"}
        other.execute("INSERT INTO board_state(key, value, updated_by, updated_at) VALUES (?, ?, 'x', 0)",
                      (hosted_results._key(sid, 99), json.dumps(x)))
        other.commit()
        other.close()
        return real(conn, parent, child_id, n)
    monkeypatch.setattr(hosted_results, "claimed", settle_meanwhile)
    out = hs.steer_waited(env.board, sid, kid, 1, None)
    assert out["steer_status"]["state"] == "accepted" and out["note"] == hs.STEER_UNSETTLED, \
        "one snapshot: never an accepted steer beside a result already delivered"


def test_s2_12_plain_show_prints_the_steers_outcome(env):
    sid, child, req = steered(env)
    answer_steer(child, req, {"turnId": cx.TURN})
    end_turn(env, child)
    assert "steer: taken into the turn (answered)" in cli._fmt_hosted_detail(show(env, sid))


def test_s2_13_ack_entries_are_kept_for_open_sessions_only(env):
    sid, child, s = held_slot(env)
    runner = env.node.sessions
    runner.report_ack["0" * 8 + "-dead-4000-8000-" + "0" * 12] = (1.0, 1)
    tick(env)
    assert set(runner.report_ack) <= set(runner.host.sessions)
    env.human.post(f"/api/hosted-sessions/{sid}/close")
    for _ in range(4):
        tick(env)
    assert sid not in runner.report_ack and sid not in runner.steer_seen


@pytest.mark.parametrize("delta, ok", [(-1, True), (0, True), (1, False)])
def test_o_the_cap_at_the_exact_boundary_with_escaped_text(env, delta, ok):
    """12,288 bytes as json.dumps writes the text (quotes, escapes and \\u sequences counted): one under, at, over."""
    sid, child = codex_turn_in_flight(env)
    unit = 'é\n"'                       # 6 + 2 + 2 bytes encoded
    body = unit * 1000
    rest = hosted_steer.STEER_MAX_BYTES - hosted_steer.encoded_size(body) + delta
    text = body + "a" * rest
    assert hosted_steer.encoded_size(text) == hosted_steer.STEER_MAX_BYTES + delta
    r = env.human.post(f"/api/hosted-sessions/{sid}/messages", json={"message": text, "mode": "steer"})
    assert (r.status_code == 200) == ok, r.text
    assert len(json.dumps(text).encode()) - hosted_steer.STEER_MAX_BYTES == delta
    assert (len(json.dumps(text).encode()) > node_sessions.STEER_MAX_BYTES) == (not ok), "the node's check agrees"


@pytest.mark.parametrize("delta, ok", [(-1, True), (0, True), (1, False)])
def test_o_the_cap_at_the_exact_boundary_after_a_parents_frame(env, monkeypatch, delta, ok):
    sid, kid, child = codex_child(env)
    monkeypatch.setattr(hs.secrets, "token_hex", lambda n=8: "0123456789abcdef")
    who = hs._who(hs.resolve(env.board, env.p["claude"], bound(env, sid), None))
    framed = hosted_steer.encoded_size(hs.render_task(who, "x", steer=True))
    text = "x" + "é" * ((hosted_steer.STEER_MAX_BYTES - framed + delta) // 6) + "a" * (
        (hosted_steer.STEER_MAX_BYTES - framed + delta) % 6)
    assert hosted_steer.encoded_size(hs.render_task(who, text, steer=True)) == hosted_steer.STEER_MAX_BYTES + delta
    if ok:
        out = hs.send(env.board, env.p["claude"], bound(env, sid), kid, message=text, mode="steer")
        assert out["steer"]["state"] == "accepted"
    else:
        with pytest.raises(Conflict, match=re.escape(hosted_steer.REFUSED["big"])):
            hs.send(env.board, env.p["claude"], bound(env, sid), kid, message=text, mode="steer")


def test_s2_08_a_fault_rolls_back_a_withdrawal_made_earlier_in_that_offer(env, monkeypatch):
    a, b = two_steered_roots(env)
    real = hosted_steer._offer_problem
    calls = []

    def first_withdrawn_second_fails(*args):
        calls.append(1)
        if len(calls) == 1:
            return "checkout"
        raise RuntimeError("a fault on the second steer")
    monkeypatch.setattr(hosted_steer, "_offer_problem", first_withdrawn_second_fails)
    listing(env)
    assert all(steer_rec(env, x)["withdrawn_at"] is None for x in (a, b)), "the withdrawal was rolled back too"
    monkeypatch.setattr(hosted_steer, "_offer_problem", real)


def test_s2_03_a_hello_acknowledged_after_the_deadline_never_revives_authority(env):
    sid, child, s = held_slot(env)
    runner = env.node.sessions
    t0 = runner.steer_seen[sid]
    runner.hello_ack, runner.report_ack[sid] = t0, (t0, 1)
    env.clock.advance(node_sessions.STEER_CONTACT_SECONDS + 1)
    runner.note_hello(t0 + 74)              # sent before the deadline, acknowledged after it
    assert s.steer.revoked == "contact"


def test_s2_13_a_forgotten_sessions_ack_goes_with_it(env):
    sid, child, s = held_slot(env)
    runner = env.node.sessions
    assert sid in runner.report_ack
    env.human.post(f"/api/hosted-sessions/{sid}/close")
    runner.client = _GetsOnly(env.laptop)   # no report is acknowledged from here on
    for _ in range(6):
        tick(env)
        env.clock.advance(node_sessions.RETRY_SECONDS)
        if sid not in runner.host.sessions:
            break
    assert sid not in runner.host.sessions and sid not in runner.report_ack and sid not in runner.steer_seen


def test_s2_02_a_partial_frames_5_s_count_from_its_first_byte_on_a_fresh_clock(henv):
    """The pass that writes the first bytes is slow (its reads take 4 s each): the 5 s start at that write, not at
    the pass's start, so a frame completed 1 s later is not cut."""
    host, s, child, _ = host_n(henv)
    child.accept = 10
    real = child.read_lines

    def slow():
        henv.clock.advance(4)
        return real()
    child.read_lines = slow
    host.steer_offer(SID, "a", "u1", TEXT, authorized=True)
    host.tick()
    child.read_lines = real
    assert 0 < s.steer.sent < s.steer.length
    henv.clock.advance(1)
    child.accept = None
    host.tick()
    assert s.stopping_since is None and len(steers_of(child)) == 1, "completed within its 5 s"


def test_s2_01_an_answer_with_any_other_key_is_no_evidence():
    p, _ = proved()
    [req] = cx.sent(p.steer(TEXT))
    p.steer_written()
    p.feed(cx.line({"id": req["id"], "result": {"turnId": cx.TURN}, "unexpected": 1}))
    p.feed(cx.line(cx.completed()))
    assert p.steer_result() == "other"


# ------------------------------------------------------------ review round 2 (R2-01 .. R2-12, and part O's gaps)


def _answered(msg_):
    p, _ = proved()
    [req] = cx.sent(p.steer(TEXT))
    p.steer_written()
    p.feed(cx.line({"id": req["id"]} | msg_))
    p.feed(cx.line(cx.completed()))
    return p.steer_result()


@pytest.mark.parametrize("version, want", [(None, "ok"), ("2.0", "ok"), ("1.0", "other"), (2.0, "other"), ("", "other")])
def test_r2_01_a_jsonrpc_other_than_absent_or_2_0_is_no_evidence(version, want):
    extra = {} if version is None else {"jsonrpc": version}
    assert _answered({"result": {"turnId": cx.TURN}} | extra) == want


@pytest.mark.parametrize("found, want", [("01a1fe39-0000-7000-8000-000000000000", "refused"), (cx.TURN, "other"),
                                         ("", "other")])
def test_r2_01_a_mismatch_refusal_needs_a_nonempty_found_id_other_than_the_expected(found, want):
    err = {"code": -32600, "message": f"expected active turn id `{cx.TURN}` but found `{found}`"}
    assert _answered({"error": err}) == want


@pytest.mark.parametrize("empty", ["answer", "item", "both"])
def test_r2_01_an_empty_proof_id_never_proves_a_turn(empty):
    p = cx.ready()
    [start_] = cx.sent(p.turn("long"))
    cid = start_["params"]["clientUserMessageId"]
    turn = "" if empty in ("answer", "both") else cx.TURN
    item_turn = "" if empty in ("item", "both") else cx.TURN
    p.feed(cx.reply(start_["id"], {"turn": {**cx.TURN_REPLY["turn"], "id": turn}}))
    p.feed(cx.line(user_item(cid, turn=item_turn)))
    assert not p.owns() and p.steer(TEXT) is None, "nothing is written into a turn not proved by nonempty ids"


def _fenced_frame(henv):
    """A host whose steer frame (authorized, not a byte written) was taken back by a fence: kept out of the buffer."""
    host, s, child, gate = host_n(henv)
    child.accept = 0
    host.steer_offer(SID, "a", "u1", TEXT, authorized=True)
    host.tick()
    host.steer_fence(SID)
    host.tick()
    assert s.steer.mark is None and s.steer.frame is not None and s.steer.fenced
    child.accept = None
    return host, s, child, gate


@pytest.mark.parametrize("how", ["stale", "another_n"])
def test_r2_02_only_a_fresh_offer_of_the_same_steer_for_the_same_n_lifts_a_fence(henv, how):
    host, s, child, _ = _fenced_frame(henv)
    if how == "stale":
        host.steer_offer(SID, "a", "u1", TEXT, authorized=False)    # an authoritative listing too old to use
    else:
        host.steer_offer(SID, "b", "u1", TEXT, authorized=True)     # the same uuid, for another turn
    host.tick()
    host.tick()
    assert steers_of(child) == [] and s.steer.fenced, "the fence holds"
    host.steer_offer(SID, "a", "u1", TEXT, authorized=True)
    host.tick()
    assert len(steers_of(child)) == 1, "a fresh offer of it for N lifts it"


@pytest.mark.parametrize("gate", ["turn_gate", "unbound", "may_start", "authority"])
def test_r2_02_a_gate_lost_while_the_frame_waits_out_of_the_buffer_is_latched(henv, gate):
    host, s, child, flag = _fenced_frame(henv)
    bind0 = s.bind
    if gate == "turn_gate":
        host.turn_gate = lambda sess: False
    elif gate == "unbound":
        s.bind = "c" * 64
    elif gate == "may_start":
        host.may_start = lambda: False
    else:
        flag[0] = False
    host.tick()
    host.turn_gate = host.may_start = None      # every gate recovers
    s.bind = bind0
    flag[0] = True
    host.steer_offer(SID, "a", "u1", TEXT, authorized=True)
    host.tick()
    host.tick()
    assert steers_of(child) == [], "never written once a gate failed while it was fenced"
    child.emit(cx.message("done"), cx.completed())
    assert ended_of(host.tick())[0].steer == {"outcome": "not_delivered", "why": "not_written"}


def test_r2_02_an_expired_re_offer_after_a_fence_never_lifts_it(env):
    """Part O: fenced by a listing that does not name `steer`, then an authoritative listing that names it again but
    whose answer is read past STEER_OFFER_AGE: nothing is written."""
    sid, child, s = held_slot(env)
    env.node.sessions.client = _Featureless(env.laptop)
    st = last_start(child)
    child.emit(user_item(st["params"]["clientUserMessageId"]))     # proved now
    tick(env)
    assert s.steer.fenced and steers_of(child) == []

    class Slow:
        def __init__(self, hub, clock):
            self.hub, self.clock = hub, clock

        def request(self, method, path, **kw):
            out = self.hub.request(method, path, **kw)
            if method == "GET":
                self.clock.advance(node_sessions.STEER_OFFER_AGE)
            return out
    env.node.sessions.client = Slow(env.laptop, env.clock)
    env.node.hello()
    tick(env)
    assert steers_of(child) == [] and s.steer.fenced, "an answer too old to use lifts no fence"


def test_r2_03_the_5_s_limit_is_asked_after_every_gate_callback(henv):
    """The final gate begins at 4.95 s of the partial frame's age and its callback returns past 5 s: the rest of the
    frame is not written; the process is cut."""
    host, s, child, _ = partial(henv)
    child.accept = None
    henv.clock.advance(STEER_PARTIAL_SECONDS - 0.15 - (henv.clock() - s.steer.partial_since))

    def slow_authority(sess):
        henv.clock.advance(0.1)
        return True
    host.steer_authority = slow_authority
    before = len(child.stdin)
    events = host.tick()
    assert len(child.stdin) == before, "not one more byte past 5 s"
    assert [e.detail for e in ended_of(events)] == [STEER_CUT]


def _human_n_in_a_child_steered(env):
    """A Codex child of an agent's session; the human's turn N (message 2) is in flight there, proved, with the
    human's steer written into it."""
    sid, kid, child = codex_child(env)
    end_turn(env, child, "the task's answer")
    env.human.post(f"/api/hosted-sessions/{kid}/messages", json={"message": "the human's"})
    tick(env)
    st = last_start(child)
    child.emit(cx.reply(st["id"], cx.TURN_REPLY), cx.TURN_STARTED, user_item(st["params"]["clientUserMessageId"]))
    tick(env)
    post_steer(env, kid)
    tick(env)
    [req] = steers_of(child)
    return sid, kid, child, req


def test_r2_04_cancelling_a_queued_message_behind_n_never_drops_ns_native_end(env):
    sid, kid, child, req = _human_n_in_a_child_steered(env)
    hs.send(env.board, env.p["claude"], bound(env, sid), kid, message="a task behind it")
    hs.cancel(env.board, env.p["claude"], bound(env, sid), kid)        # the parent's own: only the queued task
    child.emit(cx.reply(req["id"], {"turnId": cx.TURN}), cx.message("the human's answer"), cx.completed())
    tick(env)
    tick(env)
    m = msg(env, kid, 2)
    assert m["state"] == "done" and show(env, kid)["reply"] == "the human's answer", "N's own end, not LOST"
    assert (steer_rec(env, kid, 2)["outcome"], steer_rec(env, kid, 2)["why"]) == ("in_turn", "answered")


def test_r2_04_a_cancel_of_a_message_behind_n_reads_nothing_of_the_process(henv):
    host, s, child, _ = host_n(henv)
    host.steer_offer(SID, "a", "u1", TEXT, authorized=True)
    host.tick()
    [req] = steers_of(child)
    host.send(SID, "next", message_id="b")
    child.emit(cx.reply(req["id"], {"turnId": cx.TURN}), cx.message("native"), cx.completed())
    events = host.cancel(SID, "b", "cancelled")
    assert [(e.kind, e.message_id) for e in events] == [("turn_ended", "b")], "only the cancelled message"
    assert s.stopping_since is None
    [end] = [e for e in ended_of(host.tick()) if e.message_id == "a"]
    assert end.ok and end.text == "native" and end.steer == {"outcome": "in_turn", "why": "answered"}


def _fragmented(child, chunks: int):
    """The child's reads take bytes into a line not complete yet `chunks` times (no line, `bytes_read` grows), then
    return what is there."""
    real, left = child.read_lines, [chunks]
    child.bytes_read = 0

    def read():
        if left[0] > 0:
            left[0] -= 1
            child.bytes_read += 4096
            return []
        return real()
    child.read_lines = read


@pytest.mark.parametrize("how", ["close", "stop_turns", "stop_all"])
def test_r2_05_a_drain_goes_on_while_reads_make_progress_without_a_line(henv, how):
    host, s, child, _ = host_n(henv)
    host.steer_offer(SID, "a", "u1", TEXT, authorized=True)
    host.tick()
    [req] = steers_of(child)
    child.emit(cx.reply(req["id"], {"turnId": cx.TURN}), cx.message("native"), cx.completed())
    _fragmented(child, 3)
    events = (host.close(SID, "closed") if how == "close" else host.stop_turns("no contact") if how == "stop_turns"
              else host.stop_all())
    [end] = ended_of(events)
    assert end.ok and end.text == "native" and end.steer == {"outcome": "in_turn", "why": "answered"}


def test_r2_05_the_cut_reads_while_reads_make_progress_without_a_line(henv):
    host, s, child, _ = partial(henv)
    henv.clock.advance(STEER_PARTIAL_SECONDS)
    child.emit(cx.message("finished first"), cx.completed())
    _fragmented(child, 2)
    [end] = ended_of(host.steer_deadlines())
    assert end.ok and end.text == "finished first" and s.stopping_since is not None


@pytest.mark.parametrize("how", ["close", "stop_all"])
def test_r2_05_a_drain_error_neither_skips_the_stop_nor_loses_what_was_read(henv, how):
    host, s, child, _ = host_n(henv)
    host.steer_offer(SID, "a", "u1", TEXT, authorized=True)
    host.tick()
    [req] = steers_of(child)
    # (Review round 3: the drain stops as soon as N's end is read, so the error is put between the answer and N's
    # end: the first read takes the answer and a note, the second fails, the rest comes after the stop.)
    child.emit(cx.reply(req["id"], {"turnId": cx.TURN}), cx.message("a note", phase="commentary"),
               cx.message("native"), cx.completed())
    real, calls = child.read_lines, []

    def read():
        calls.append(1)
        if len(calls) == 1:
            return [child.stdout.popleft(), child.stdout.popleft()]
        if len(calls) == 2:
            raise OSError("the pipe failed")
        return real()
    child.read_lines = read
    try:
        events = host.close(SID, "closed") if how == "close" else host.stop_all()
    except OSError:
        pytest.fail("a drain error skipped the stop and lost what was read")
    [end] = ended_of(events)
    assert end.steer == {"outcome": "unconfirmed", "why": "turn_cut"}, "the answer read before the error counts"
    assert s.ended or s.child is None or s.stopping_since is not None, "the stop went on"


class _Timed:
    """The node's client with each POST taking `takes` seconds of the fake clock, cut short (ReadTimeout) when the
    call's own timeout is shorter; the first report carrying a steer outcome is refused (422)."""

    def __init__(self, env, takes):
        self.env, self.takes, self.calls = env, takes, []

    def request(self, method, path, **kw):
        if method != "POST":
            return self.env.laptop.request(method, path, **kw)
        t = kw.get("timeout")
        limit = 20.0 if t is None else (t.read if isinstance(t, httpx.Timeout) else float(t))
        self.calls.append(limit)
        if limit < self.takes:
            self.env.clock.advance(limit)
            raise httpx.ReadTimeout("timed out")
        self.env.clock.advance(self.takes)
        if any("steer" in e for e in (kw.get("json") or {}).get("events", [])):
            return httpx.Response(422, json={"detail": "extra field"}, request=httpx.Request(method, "http://hub" + path))
        return self.env.laptop.request(method, path, **kw)


def test_r2_06_a_reports_retry_fits_in_what_is_left_of_one_calls_budget(env):
    from agent_comms.session_host import HostEvent
    runner = env.node.sessions
    sid, child = codex_turn_in_flight(env)
    runner.client = _Timed(env, 15.0)
    runner.hub_features, runner.features_known = {"answered", "restart", "steer"}, True
    runner._emit([HostEvent(sid, "turn_ended", "1", ok=True, text="the end", answered=True,
                            steer={"outcome": "unconfirmed", "why": "turn_cut"})])
    t0 = env.clock()
    runner._flush()
    assert env.clock() - t0 <= 20.0, "the pair took no longer than one call: the shutdown's wait outlasts it"
    assert node_sessions.REPORT_WAIT_SECONDS > 20.0
    assert any(x == sid for x, _ in runner.pending), "not sent: kept for the next flush, never dropped"


def test_r2_06_a_flush_under_way_when_shutdown_begins_posts_no_other_session(env):
    from agent_comms.session_host import HostEvent
    runner = env.node.sessions
    a, ca = codex_turn_in_flight(env)
    b, cb = codex_turn_in_flight(env, project=P2)
    # (Review round 3, the parity rule: this applies only while a steer attempt is live; b holds one here. With none,
    # the flush is main's: test_k_parity_a_flush_overlapping_shutdown_with_no_steer_is_mains.)
    post_steer(env, b)
    tick(env)
    assert runner.host.attempts()
    posted = []

    class StopsAfterFirst:
        def request(self, method, path, **kw):
            if method == "POST":
                posted.append(path)
                runner.stopping.set()       # shutdown begins while this report is on its way
            return env.laptop.request(method, path, **kw)
    runner.client = StopsAfterFirst()
    runner._emit([HostEvent(a, "turn_ended", "1", ok=True, text="a", answered=True),
                  HostEvent(b, "turn_ended", "1", ok=True, text="b", answered=True)])
    runner._flush()
    assert len(posted) == 1, "the lock is held for one report at most once shutdown waits for it"
    assert [x for x, _ in runner.pending] == [b], "the other session's end is kept for shutdown's own flush"
    runner.stopping.clear()


def test_r2_07_a_renewal_acknowledged_while_the_first_offers_fetch_was_out_keeps_the_contact(env):
    """Sol's sequence: contact acknowledged at 0, the fetch sent at 70, a hello sent at 72 and acknowledged at once,
    the fetch's answer read at 78. The contact never broke: the offer holds authority and is written."""
    sid, child = codex_turn_in_flight(env)
    runner = env.node.sessions

    class NoN:
        """Reports are taken, but their answers name no turn: only hellos are evidence here."""

        def __init__(self):
            self.slow = False

        def request(self, method, path, **kw):
            if method == "GET" and path == "/api/nodes/sessions" and self.slow:
                env.clock.advance(2)
                env.node.hello()                    # the renewal, sent at 72, acknowledged at once
                env.clock.advance(6)
                return env.laptop.request(method, path, **kw)
            r = env.laptop.request(method, path, **kw)
            if method == "POST" and path.startswith("/api/nodes/sessions/") and r.status_code == 200:
                return httpx.Response(200, json=r.json() | {"in_flight": None}, request=r.request)
            return r
    client = NoN()
    runner.client = client
    tick(env)
    runner.report_ack.clear()
    post_steer(env, sid)
    env.node.hello()                                # contact at 0
    env.clock.advance(70)
    client.slow = True
    tick(env)                                       # the fetch sent at 70, its answer read at 78
    client.slow = False
    s = runner.host.sessions[sid]
    assert s.steer is not None and s.steer.revoked is None and s.steer.authorized
    assert len(steers_of(child)) == 1


def test_r2_08_steer_text_that_is_not_utf_8_is_refused_on_every_path_before_anything_is_stored(env):
    sid, child = codex_turn_in_flight(env)
    bad = "steer \ud800 here"
    r = env.human.post(f"/api/hosted-sessions/{sid}/messages", headers={"Content-Type": "application/json"},
                       content=json.dumps({"message": bad, "mode": "steer"}).encode())    # (an escaped surrogate)
    assert r.status_code == 409 and "not valid Unicode" in r.json()["message"]
    assert steer_rec(env, sid) is None, "nothing stored"
    p, kid, kchild = codex_child(env)
    with pytest.raises(Conflict, match="not valid Unicode"):
        hs.send(env.board, env.p["claude"], bound(env, p), kid, message=bad, mode="steer")
    assert steer_rec(env, kid) is None


def test_r2_08_listed_at_is_stamped_only_for_an_answer_that_could_be_written(env):
    sid, child = codex_turn_in_flight(env)
    with hosted.db.write_tx(env.board.conn) as c:      # a record from before the check (never through `target`)
        r = hosted._session(c, sid)
        m = hosted._get(c, hosted._msg_key(sid, 1))
        hosted_steer.accept(c, r, m, "steer \ud800 here", "user", {"human": "peter"}, "peter", None, env.clock())
    try:
        env.laptop.get("/api/nodes/sessions", params={"tree": 5})
    except Exception:
        pass        # the HTTP layer cannot write it (as on main for any such text)
    assert steer_rec(env, sid)["listed_at"] is None, "never exposed: it could not be sent"


def test_r2_09_an_acknowledgement_is_published_while_the_host_is_busy(env):
    """Part O, ack receipt while the host is busy: the host's lock is held (local work); a hello acknowledged
    meanwhile is published at once, at its receipt time, and the steer's contact counts it. (Round 4: no thread and no
    timeout: the acknowledgement is published by the very thread that holds the host's lock, whose lock refuses any
    wait on it, so a publication that needed it would fail here, not hang.)"""
    sid, child, s = held_slot(env)
    runner = env.node.sessions
    t0 = runner.steer_seen[sid]
    env.clock.advance(70)
    busy = _NoWait(runner.lock)
    runner.lock = busy
    with busy:                                      # the host is busy
        runner.note_hello(t0 + 69)
        env.clock.advance(10)                       # the lock is held past 75 s from the old contact
    runner.lock = busy.real
    assert runner.hello_ack == t0 + 69
    assert runner.host._steer_gate(s, s.steer, env.clock()) and s.steer.revoked is None, \
        "contact sent at 69 holds until 144"


def test_r2_09_with_no_steer_a_hello_never_takes_the_hosts_lock(env, monkeypatch):
    runner = env.node.sessions
    taken = []

    class Spy:
        def __init__(self, lock):
            self.lock = lock

        def __enter__(self):
            taken.append("with")
            return self.lock.__enter__()

        def __exit__(self, *a):
            return self.lock.__exit__(*a)

        def acquire(self, *a, **kw):
            taken.append("acquire")
            return self.lock.acquire(*a, **kw)

        def release(self):
            return self.lock.release()
    monkeypatch.setattr(runner, "lock", Spy(runner.lock))
    env.node.hello()
    assert taken == [], "a hello with no steer held is main's: no wait on local work"


def test_r2_10_a_parents_retry_sweeps_first_and_never_says_accepted_for_a_terminal_n(env):
    sid, kid, child = codex_child(env)
    hs.send(env.board, env.p["claude"], bound(env, sid), kid, message=TEXT, mode="steer", request_id="p1")
    entry(env, kid)
    with hosted.db.write_tx(env.board.conn) as c:     # an older hub settled N; the node is gone since
        m = hosted._get(c, hosted._msg_key(kid, 1))
        hosted._put(c, hosted._msg_key(kid, 1), m | {"state": "done", "ok": True}, "x", env.clock())
        r = hosted._session(c, kid)
        hosted._put(c, hosted.SESSION_PREFIX + kid, r | {"in_flight": None}, "x", env.clock())
    again = hs.send(env.board, env.p["claude"], bound(env, sid), kid, message="x", mode="steer", request_id="p1")
    assert again["created"] is False and again["steer"]["state"] == "settled"
    assert again["steer"]["outcome"] == "unconfirmed"


def test_r2_10_a_queued_carrier_is_said_queued_not_delivered(env, monkeypatch):
    sid, kid, child = codex_child(env)
    hs.send(env.board, env.p["claude"], bound(env, sid), kid, message=TEXT, mode="steer")
    real = hosted_results._results

    def one_queued(conn, parent):
        return [{"child": kid, "message": 1, "state": "queued", "n": 7}] if parent == sid else real(conn, parent)
    monkeypatch.setattr(hosted_results, "_results", one_queued)
    out = hs.steer_waited(env.board, sid, kid, 1, None)
    assert "queued" in out["note"] and "went to you as a turn" not in out["note"]


def test_r2_10_a_retention_miss_between_the_post_and_the_first_get_exits_7(env, monkeypatch, capsys):
    sid, child = codex_turn_in_flight(env)
    first = []

    def api(settings, token, method, path, body=None, timeout=10):
        if method == "GET" and "/messages/" in path and not first:
            first.append(path)
            with hosted.db.write_tx(env.board.conn) as c:       # pruned meanwhile: N and its steer are gone
                c.execute("DELETE FROM board_state WHERE key = ?", (hosted._msg_key(sid, 1),))
                c.execute("DELETE FROM board_state WHERE key = ?", (hosted_steer._key(sid, 1),))
        r = env.human.request(method, path, json=body)
        if r.status_code != 200:
            raise SystemExit(f"error ({r.json().get('error')}): {r.json().get('message')}")
        return r.json()
    monkeypatch.setattr(cli, "_api", api)
    monkeypatch.setattr(cli, "_human_token", lambda: "t")
    assert run_cli("send", sid, TEXT, "--mode", "steer", "--wait") == 7


def test_r2_11_the_modes_allowance_is_for_recorded_codex_sessions_only():
    import golden_schema_free as g
    claude, codex = "00000000-0000-0000-0000-00000000000a", "00000000-0000-0000-0000-00000000000b"
    out = {"records": {f"host.session.{claude}": {"runtime": "claude-code", "modes": ["restart"]},
                       f"host.session.{codex}": {"runtime": "codex-cli", "modes": ["restart"]}},
           "node_bodies": [[f"/api/nodes/sessions/{claude}", {"modes": ["steer", "restart"]}],
                           [f"/api/nodes/sessions/{codex}", {"modes": ["steer", "restart"]}]]}
    g.step2_named(out)
    assert out["node_bodies"][0][1]["modes"] == ["steer", "restart"], "a Claude report's steer is never hidden"
    assert out["node_bodies"][1][1]["modes"] == ["restart"]


# --- R2-12: the M5 join probe, replayed against a stand-in app-server


FAKE_SERVER = r'''#!{python}
import json, sys
TID, T, OTHER = "01a2101a-59e8-7a60-b459-42fae8f0bb66", "01a2101a-5a12-7ec0-bd81-388b63620fda", "01a2101a-ffff-7000-8000-000000000000"
SCRIPT = {script}

def out(m):
    sys.stdout.write(json.dumps(m) + "\n")
    sys.stdout.flush()

for line in sys.stdin:
    m = json.loads(line)
    rid, method = m.get("id"), m.get("method")
    if method == "initialize":
        out({{"id": rid, "result": {{}}}})
    elif method == "thread/start":
        out({{"id": rid, "result": {{"thread": {{"id": TID}}}}}})
    elif method == "turn/start" and rid == 3:
        out({{"id": rid, "result": {{"turn": {{"id": T, "status": "inProgress"}}}}}})
        out({{"method": "item/started", "params": {{"threadId": TID, "turnId": T, "item": {{"type": "commandExecution"}}}}}})
    elif method == "turn/start" and rid == 4:
        j = m["params"]["clientUserMessageId"]
        for step in SCRIPT:
            if step == "answer":
                out({{"id": 4, "result": {{"turn": {{"id": T, "status": "inProgress"}}}}}})
            elif step == "item":
                out({{"method": "item/completed", "params": {{"threadId": TID, "turnId": T, "item": {{"type": "userMessage", "clientId": j}}}}}})
            elif step == "other_done":
                out({{"method": "turn/completed", "params": {{"threadId": TID, "turn": {{"id": OTHER, "status": "completed"}}}}}})
            elif step == "wrong_turn_item":
                out({{"method": "item/completed", "params": {{"threadId": TID, "turnId": OTHER, "item": {{"type": "userMessage", "clientId": j}}}}}})
            elif step == "float_answer_other":
                out({{"id": 4.0, "result": {{"turn": {{"id": OTHER, "status": "inProgress"}}}}}})
            elif step == "answer_other":
                out({{"id": 4, "result": {{"turn": {{"id": OTHER, "status": "inProgress"}}}}}})
            elif step == "done":
                out({{"method": "turn/completed", "params": {{"threadId": TID, "turn": {{"id": T, "status": "completed"}}}}}})
'''


def _probe(tmp_path, script, seconds=None):
    """The probe (`_m5_join_probe`) run against a stand-in app-server that answers the handshake and A, then writes
    `script` after J's turn/start. The run object has only what the probe uses."""
    import importlib.util
    import os
    import sys
    from pathlib import Path
    from types import SimpleNamespace
    spec = importlib.util.spec_from_file_location("e2e_r2_12", Path(__file__).resolve().parents[1] / "scripts"
                                                  / "session-host-e2e.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    fake = tmp_path / "codex"
    fake.write_text(FAKE_SERVER.format(python=sys.executable, script=json.dumps(script)))
    fake.chmod(0o755)
    (tmp_path / "logs").mkdir(exist_ok=True)
    run = object.__new__(mod.Run)
    run.tmp, run.pids = tmp_path, set()
    run.args = SimpleNamespace(codex_bin=str(fake), codex_model="m")
    run.env = {"PATH": os.environ["PATH"], "HOME": str(tmp_path)}
    run.codex_quiet = lambda: []
    if seconds is not None:
        run.m5_seconds = seconds
    return run


@pytest.mark.parametrize("script", [["answer", "item", "done"], ["item", "answer", "done"],
                                    ["answer", "other_done", "item", "done"]],
                         ids=["reply_first", "item_first", "another_turns_completion_is_not_ts"])
def test_r2_12_the_join_probe_takes_both_observations_before_ts_own_completion(tmp_path, script):
    run = _probe(tmp_path, script)
    run._m5_join_probe()        # passes: no SystemExit


def test_r2_12_the_join_probe_refuses_an_observation_after_ts_completion(tmp_path):
    run = _probe(tmp_path, ["answer", "done", "item"])
    with pytest.raises(SystemExit, match="FAIL"):
        run._m5_join_probe()


def test_r2_12_the_join_probe_never_blocks_past_its_deadline(tmp_path):
    import os
    import signal
    import threading
    run = _probe(tmp_path, [], seconds=2)       # J is never answered, and the pipe stays open
    said = []

    def go():
        try:
            run._m5_join_probe()
        except SystemExit as e:
            said.append(str(e))
    th = threading.Thread(target=go, daemon=True)
    th.start()
    th.join(15)
    alive = th.is_alive()
    for pid in run.pids:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    th.join(30)
    assert not alive and said and "timed out" in said[0]


# --- part O's gaps (round 2)


@pytest.mark.parametrize("extra, ok", [(0, True), (1, False)])
def test_o_the_12288_byte_cap_on_both_seams_literally(env, monkeypatch, extra, ok):
    """12,288 bytes, written out here (never the production constant): a text whose `json.dumps` is exactly 12,288
    bytes is taken by the hub and by the node; one byte more is refused by both."""
    sid, child = codex_turn_in_flight(env)
    text = "a" * (12_288 - 2 + extra)
    assert len(json.dumps(text).encode()) == 12_288 + extra
    r = env.human.post(f"/api/hosted-sessions/{sid}/messages", json={"message": text, "mode": "steer"})
    assert (r.status_code == 200) == ok
    if not ok:
        # the node's seam on its own: the hub's cap lifted, the node's still holds at 12,288
        monkeypatch.setattr(hosted_steer, "STEER_MAX_BYTES", 10 ** 6)
        post_steer(env, sid, text=text)
    tick(env)
    s = env.node.sessions.host.sessions[sid]
    assert (s.steer.refused is None) == ok and (s.steer.refused == "too_big") == (not ok)


def test_o_the_watchdog_with_a_fully_written_steer_a_valid_receipt_and_no_native_end(env):
    sid, child, req = steered(env)
    answer_steer(child, req, {"turnId": cx.TURN})
    tick(env)
    runner = env.node.sessions
    env.clock.advance(runner.self_stop_seconds + 1)     # no hub call meanwhile: the self-stop is due
    runner.enforce_deadlines()
    assert any(e.kind == "turn_ended" and e.steer == {"outcome": "unconfirmed", "why": "turn_cut"}
               for e in runner.watched_steer)
    env.node.hello()
    tick(env)
    assert msg(env, sid, 1)["state"] == "failed"
    assert (steer_rec(env, sid)["outcome"], steer_rec(env, sid)["why"]) == ("unconfirmed", "turn_cut")


@pytest.mark.parametrize("written", ["partial", "full"])
def test_o_an_authoritative_omission_through_reconciliation_after_a_partial_or_full_write(env, written):
    sid, child = codex_turn_in_flight(env)
    if written == "partial":
        child.accept = 10
    post_steer(env, sid)
    tick(env)
    s = env.node.sessions.host.sessions[sid]
    with hosted.db.write_tx(env.board.conn) as c:       # the hub no longer offers it: an authoritative omission
        hosted_steer.cancel(c, sid, 1, env.clock(), "x")
    if written == "partial":
        assert 0 < s.steer.sent < s.steer.length
        tick(env)
        assert s.stopping_since is not None or s.child is None, "the rest never goes: the process is cut"
        tick(env)
        tick(env)
        assert (steer_rec(env, sid)["outcome"], steer_rec(env, sid)["why"]) == ("unconfirmed", "partial")
    else:
        [req] = steers_of(child)
        tick(env)
        assert s.steer.revoked == "omitted" and len(steers_of(child)) == 1
        answer_steer(child, req, {"turnId": cx.TURN})
        end_turn(env, child, "native")
        assert (steer_rec(env, sid)["outcome"], steer_rec(env, sid)["why"]) == ("in_turn", "answered"), \
            "fully written before the omission: N's own answer decides"


def test_o_shutdown_reads_the_pipe_before_its_sigterm(env):
    sid, child, req = steered(env)
    child.emit(cx.reply(req["id"], {"turnId": cx.TURN}), cx.message("native"), cx.completed())
    unread = []
    real = child.terminate

    def terminate():
        unread.append(len(child.stdout))
        return real()
    child.terminate = terminate
    env.node.sessions.signal()
    assert unread == [0], "what the process wrote was read before SIGTERM"
    assert any(e.kind == "turn_ended" and e.text == "native" for e in env.node.sessions.watched_steer)
    env.node.sessions.stopping.clear()


def test_o_a_turn_timeout_and_a_close_overlapping_with_a_lingering_group_end_n_once(henv):
    host, s, child, _ = partial(henv)
    henv.groups.add(child.pid)                  # the group lingers after its leader
    henv.clock.advance(30 * 60 + 1)             # henv's turn timeout
    events = host.tick()
    events += host.close(SID, "closed")
    child.code = 0
    for _ in range(3):
        henv.clock.advance(host.kill_grace)
        events += host.tick()
    henv.groups.discard(child.pid)
    events += host.tick()
    ends = [e for e in events if e.kind == "turn_ended" and e.message_id == "a"]
    assert len(ends) == 1 and ends[0].steer == {"outcome": "unconfirmed", "why": "partial"}
    assert [e.kind for e in events].count("ended") == 1


def test_o_the_mcp_steer_wait_is_one_45_s_wait(env, monkeypatch):
    sid, kid, child = codex_child(env)
    waits = []

    async def nothing_yet(board, parent, by, child=None, message=None, wait=0, gone=None):
        waits.append(wait)
        return {"result": None}
    monkeypatch.setattr(hosted_results, "claim_wait", nothing_yet)
    mcp_call(env, sid, monkeypatch, "board_session_send", {"child": kid, "message": TEXT, "mode": "steer", "wait": True})
    assert waits == [45]


def test_k_a_session_that_never_took_a_steer_is_never_read_for_steers(env, monkeypatch):
    sid, child = codex_turn_in_flight(env)
    calls = []
    real = hosted_steer.steers

    def counted(conn, session_id):
        calls.append(session_id)
        return real(conn, session_id)
    monkeypatch.setattr(hosted_steer, "steers", counted)
    show(env, sid)
    env.human.get("/api/hosted-sessions")
    assert sid not in calls, "no steer query for a session with no steer record (part K)"


# ------------------------------------------------------------ the write authority against a reference model (rounds 2, 3)


CONTACT = 75.0          # the rules' numbers, written out here (never read from the code under test)
OFFER_AGE = 20.0
PARTIAL = 5.0
STEPS = (0.5, 2.0, 4.9, 5.1, 10.0, 20.0, 40.0, 74.0, 76.0)


class _Model:
    """What the written rules allow (parts D, E, G; review rounds 1 to 3), kept apart from the code. Authority for S
    at instant t: an offer of S for N that was fresh (its fetch's answer received within OFFER_AGE of its send,
    contact covering the fetch from its send to that receipt) at `g`, the send time of the first such fetch; contact
    covering every instant from `g` to t (each acknowledgement counts from its RECEIPT until CONTACT after its SEND);
    no authoritative omission of S once the node holds it; no fence standing (a listing without the feature sets one;
    only such a fresh offer lifts it); every gate true; N not ended. A frame taken in part is completed only within
    PARTIAL of its first byte. The code may be stricter (its latches); never laxer."""

    def __init__(self):
        self.acks: list[tuple[float, float]] = []       # (sent, received): hellos, and reports naming N
        self.g: float | None = None
        self.held = self.omitted = self.fenced = self.n_ended = self.cancelling = False
        self.native = self.answered = self.answer_read = False
        self.gates = {"turn": True, "bind": True, "may": True}

    def ack(self, sent, received):
        self.acks.append((sent, received))

    def covered(self, a, b):
        """Every instant of [a, b] covered by an acknowledgement received by then."""
        cur, moved = a, True
        while moved:
            moved = False
            for s, r in self.acks:
                if r <= cur and s + CONTACT > cur:
                    cur, moved = s + CONTACT, True
        return cur > b

    def listing(self, kind, fetch_sent, received):
        if kind == "featureless":
            self.fenced = True
        elif kind in ("without", "wrong_uuid"):
            self.omitted = self.omitted or self.held
        elif kind == "with" and not self.omitted and not self.n_ended and not self.cancelling:
            self.held = True
            if received - fetch_sent < OFFER_AGE and self.covered(fetch_sent, received):
                if self.g is None:
                    self.g = fetch_sent
                self.fenced = False

    def authority(self, t):
        return (self.g is not None and not self.omitted and not self.fenced and not self.n_ended
                and all(self.gates.values()) and self.covered(self.g, t))


STEER_LINE = b'"method": "turn/steer"'
EVENTS = ["pass", "local", "hello", "step", "gate", "accept", "answer", "end", "cancel_live", "cancel_queued", "stop",
          "watchdog", "boundary_ack"]


def _run_model(tmp_path, seed: int, steps: int = 30, script=None):
    """One sequence (random from `seed`, or `script`: a list of (event, choice) pairs) through the real node and host,
    with a scripted hub client and the fake Codex process. Observed apart from the code under test (R3-11): the
    frame from what the ADAPTER made (its `steer`), the bytes of it in every write from the write's own data, N's end
    from the process's stdout and from the ends the HOST returned (never the node's `_add`). Returns what it saw."""
    import random
    from types import SimpleNamespace
    from test_hosted_sessions import session_env as make
    rnd = random.Random(seed)
    env = make(tmp_path, dispatch=TREE, config=tree_config)
    sid, child = codex_turn_in_flight(env)
    post_steer(env, sid)
    base = listing(env)
    runner, host = env.node.sessions, env.node.sessions.host
    model, log_ = _Model(), [f"seed {seed}" if script is None else f"script {script}"]
    model.ack(runner.hello_ack, runner.hello_ack)
    if sid in runner.report_ack and runner.report_ack[sid][1] == 1:
        model.ack(runner.report_ack[sid][0], runner.report_ack[sid][0])
    s = host.sessions[sid]
    bind0 = s.bind
    seen = SimpleNamespace(frames=[], writes=[], ends=[])    # writes: (time, data, taken, authority, N ended)
    real_steer = s.provider.steer

    def steer(text):
        got = real_steer(text)
        if got:
            seen.frames.append(b"".join(got))
        return got
    s.provider.steer = steer
    real_write = child.write

    def write(data):
        n = real_write(data)
        t = env.clock()
        seen.writes.append((t, bytes(data), n, model.authority(t), model.n_ended))
        return n
    child.write = write
    real_read = child.read_lines

    def read():
        lines = real_read()
        for x in lines:
            if (seen.frames and b'"turnId"' in x and b'"result"' in x and model.answered
                    and not model.n_ended):
                model.answer_read = True
            if b'"turn/completed"' in x and not model.n_ended:
                model.n_ended = model.native = True
        return lines
    child.read_lines = read

    def ends_of(fn):
        def wrapped(*a, **kw):
            got = fn(*a, **kw)
            for e in got:
                if e.session_id == sid and e.kind == "turn_ended" and e.message_id == "1":
                    seen.ends.append(e)
                    model.n_ended = True
            return got
        return wrapped
    for name in ("tick", "cancel", "stop_turns", "steer_deadlines", "drain", "close", "stop_all"):
        setattr(host, name, ends_of(getattr(host, name)))
    t_gate, m_gate = host.turn_gate, host.may_start
    host.turn_gate = lambda sess: model.gates["turn"] and (t_gate is None or t_gate(sess))
    host.may_start = lambda: model.gates["may"] and (m_gate is None or m_gate())
    state = SimpleNamespace(kind="with", post="timely", inner=[], end_posted=False, end_emitted=False, cancel=None)

    class Hub:
        def request(self, method, path, **kw):
            if method == "GET" and path == "/api/nodes/sessions":
                fetch_sent = env.clock()
                for what, x in state.inner:
                    if what == "step":
                        env.clock.advance(x)
                    else:
                        sent = env.clock() - x
                        runner.note_hello(sent)
                        model.ack(sent, env.clock())
                data = json.loads(json.dumps(base))
                e = next(v for v in data["sessions"] if v["session_id"] == sid)
                if state.end_posted:
                    e["messages"] = []
                if state.cancel == 1 and e["messages"]:
                    e["messages"][0]["cancel"] = True       # N cancelled: through the node's reconciliation
                    model.cancelling = True     # (the node applies a cancel before the listing's steer: no new slot)
                elif state.cancel == 2:
                    e["messages"] = e["messages"] + [{"n": 2, "kind": "user", "state": "queued", "cancel": True}]
                state.cancel = None
                if not model.gates["bind"]:
                    e["bind"] = "c" * 64        # the hub's binding changed: the process has another one
                if state.kind == "featureless":
                    data["features"] = [f for f in data["features"] if f != "steer"]
                elif state.kind == "without":
                    e.pop("steer", None)
                elif state.kind == "wrong_n":
                    e["steer"] = e["steer"] | {"into": 2}
                elif state.kind == "wrong_uuid":
                    e["steer"] = e["steer"] | {"uuid": "0" * 8 + "-0000-4000-8000-" + "0" * 12}
                model.listing(state.kind, fetch_sent, env.clock())
                model.cancelling = False
                return httpx.Response(200, json=data, request=httpx.Request(method, "http://hub" + path))
            if method == "POST" and path == f"/api/nodes/sessions/{sid}":
                body = kw.get("json") or {}
                if state.post == "lost":
                    raise httpx.ReadTimeout("lost")
                sent = env.clock()
                if any(ev.get("kind") == "turn_ended" and ev.get("message") == 1 for ev in body.get("events", [])):
                    state.end_posted = True
                if state.post == "late":
                    env.clock.advance(rnd.choice((1.0, 30.0, 80.0)))
                n_open = not state.end_posted
                if n_open:
                    model.ack(sent, env.clock())
                return httpx.Response(200, json={"session_id": sid, "in_flight": 1 if n_open else None},
                                      request=httpx.Request(method, "http://hub" + path))
            return env.laptop.request(method, path, **kw)
    runner.client = Hub()

    def pass_(kind, post, inner):
        state.kind, state.post, state.inner = kind, post, inner
        runner.fresh, runner.last_sync, runner.retry_at = False, float("-inf"), float("-inf")
        runner.pass_once(wait=0)

    plan = script if script is not None else [None] * steps
    for i, step in enumerate(plan):
        ev = step[0] if step is not None else rnd.choices(EVENTS, weights=[10, 4, 4, 6, 3, 3, 3, 1, 1, 1, 1, 2, 3])[0]
        arg = step[1] if step is not None else None
        if ev == "pass":
            if arg is None:
                kinds = ["with", "without", "featureless", "wrong_n"] + (["wrong_uuid"] if model.held else [])
                arg = (rnd.choices(kinds, weights=[8, 1, 2, 1, 1][:len(kinds)])[0],
                       rnd.choice(["timely", "timely", "late", "lost"]),
                       [rnd.choice([("step", rnd.choice(STEPS)), ("hello", rnd.choice((0.0, 5.0, 80.0)))])
                        for _ in range(rnd.randint(0, 2))])
            log_.append(f"{i} pass: listing {arg[0]}, reports {arg[1]}, during the fetch {arg[2]}")
            pass_(*arg)
        elif ev == "local":
            log_.append(f"{i} a local pass")
            with runner.lock:
                runner._steer_watch(env.clock())
                runner._emit(host.tick())
        elif ev == "hello":
            late = arg if arg is not None else rnd.choice((0.0, 0.0, 10.0, 80.0))
            log_.append(f"{i} a hello acknowledged, sent {late} s ago")
            sent = env.clock() - late
            runner.note_hello(sent)
            model.ack(sent, env.clock())
        elif ev == "step":
            dt = arg if arg is not None else rnd.choice(STEPS)
            log_.append(f"{i} {dt} s pass")
            env.clock.advance(dt)
        elif ev == "gate":
            g = arg if arg is not None else rnd.choice(["turn", "bind", "may"])
            model.gates[g] = not model.gates[g]
            log_.append(f"{i} gate {g}: {model.gates[g]}")
            if g == "bind":
                s.bind = bind0 if model.gates["bind"] else "c" * 64
        elif ev == "accept":
            child.accept = arg if step is not None else rnd.choice((None, 0, 7, 25))
            log_.append(f"{i} the pipe takes {child.accept} per write")
        elif ev == "answer":
            frame = next((f for f in seen.frames if f in bytes(child.stdin)), None)     # the frame written, any
            if frame is not None and bytes(child.stdin).count(frame) == 1 and not model.answered \
                    and not model.n_ended and not state.end_emitted:      # Codex answers only within the turn
                model.answered = True
                log_.append(f"{i} Codex answers the steer (in turn)")
                child.emit(cx.reply(json.loads(frame)["id"], {"turnId": cx.TURN}))
        elif ev == "end" and not model.n_ended:
            log_.append(f"{i} N ends natively (its lines in the pipe)")
            state.end_emitted = True
            child.emit(cx.message("native"), cx.completed())
        elif ev == "cancel_live" and not model.n_ended:
            log_.append(f"{i} N is cancelled (the next listing says so)")
            state.cancel = 1
            pass_("with", "timely", [])
        elif ev == "cancel_queued":
            log_.append(f"{i} a message queued behind N is cancelled (the next listing says so)")
            state.cancel = 2
            pass_("with", "timely", [])
        elif ev == "stop" and not model.n_ended:
            log_.append(f"{i} every turn is stopped")
            with runner.lock:
                runner._emit(host.stop_turns("stopped"))
        elif ev == "watchdog":
            log_.append(f"{i} the watchdog")
            runner.enforce_deadlines()
        elif ev == "boundary_ack":
            # (Round 4, R3-11:) an acknowledgement published concurrently with a write, driven deterministically: at
            # the next write boundary, just before the attempt's lock is taken (the acknowledgement wins the race) or
            # just after it is released (the write wins). An acknowledgement that arrives while the write runs waits
            # for that lock (R3-01), so these two are every order there is.
            when, late = arg if arg is not None else (rnd.choice(("before", "after")), rnd.choice((0.0, 10.0, 80.0)))
            log_.append(f"{i} a hello acknowledged {when} the next write boundary, sent {late} s before it")

            def at_boundary(w, sess, when=when, late=late, i=i):
                if w != when:
                    return
                host.steer_write_hook = None            # one shot
                sent = env.clock() - late
                runner.note_hello(sent)
                model.ack(sent, env.clock())
                log_.append(f"  ({i}) the boundary's hello published at {env.clock()}")
            host.steer_write_hook = at_boundary
        _check(child, seen, model, log_)
    # (Round 4, R3-11:) every sequence ends: N ends natively if nothing ended it, local passes read it, and the host
    # must then have said N's end. Without this a sequence that never ended N checked no settlement at all.
    host.steer_write_hook = None
    if not seen.ends:
        if not model.n_ended and not state.end_emitted:
            log_.append("end: N ends natively (its lines in the pipe)")
            state.end_emitted = True
            child.emit(cx.message("native"), cx.completed())
        for k in range(3):
            log_.append(f"end: local pass {k}")
            with runner.lock:
                runner._steer_watch(env.clock())
                runner._emit(host.tick())
            if seen.ends:
                break
        if not seen.ends:
            raise AssertionError("the host never said N's end\n  " + "\n  ".join(log_))
    _check(child, seen, model, log_)
    return seen, model, log_


def _frame_bytes(seen):
    """Every frame's spans together (`_frame_spans`), and whether any frame was left in part."""
    per = _frame_spans(seen)
    return [x for spans, _ in per for x in spans], any(a for _, a in per)


def _frame_spans(seen):
    """(Independent of the host's bookkeeping, R3-11; round 4: EVERY frame the adapter made, not only the first:) for
    each frame, the writes that carried its bytes, from the write's own data and the bytes the pipe took, and whether
    any byte went after it was left in part. [([(time, frame bytes, authority, N ended)], abandoned)], by frame."""
    return [_one_frame(frame, seen.writes) for frame in seen.frames]


def _one_frame(frame, writes):
    done, spans, abandoned = 0, [], False
    for t, data, n, auth, ended in writes:
        if done == 0:
            i = data.find(frame)
            part = max(0, min(n, i + len(frame)) - i) if i >= 0 else 0
        elif done < len(frame):
            rest = frame[done:]
            if data.startswith(rest):
                part = min(n, len(rest))
            else:
                part, abandoned = 0, abandoned or n > 0
        else:
            part = 0
        if part:
            spans.append((t, part, auth, ended))
            done += part
    return spans, abandoned


def _check(child, seen, model, log_):
    def fail(why):
        raise AssertionError(why + "\n  " + "\n  ".join(log_))
    stdin = bytes(child.stdin)
    if stdin.count(STEER_LINE) > 1 or len([f for f in seen.frames if f in stdin]) > 1:
        fail("S was written more than once")
    per = _frame_spans(seen)
    if sum(bool(spans) for spans, _ in per) > 1:
        fail("bytes of more than one steer frame were written")
    written, length = 0, len(seen.frames[0]) if seen.frames else 0
    for k, (spans, abandoned) in enumerate(per):
        if abandoned:
            fail(f"bytes went to the process after steer frame {k} was left in part")
        first = None
        for t, part, auth, ended in spans:
            if not auth:
                fail(f"a byte of S (frame {k}) was written at {t} without authority")
            if ended:
                fail(f"a byte of S (frame {k}) was written at {t} after N ended")
            if first is None:
                first = t
            elif t - first >= PARTIAL:
                fail(f"the rest of a partial frame was written {t - first} s after its first byte")
            written += part
        if spans:
            length = len(seen.frames[k])
    if sum(isinstance(e.steer, dict) for e in seen.ends) > 1:
        fail("the host said S's outcome more than once")
    if seen.ends:
        steer = seen.ends[0].steer
        if (written or model.held) and not isinstance(steer, dict):
            fail("a steer the node held got no outcome at N's end")
        if isinstance(steer, dict):
            got = steer.get("outcome")
            want = ({"not_delivered"} if written == 0 else {"unconfirmed"} if written < length
                    else {"in_turn"} if model.native and model.answer_read else {"unconfirmed"})
            if got not in want:
                fail(f"outcome {got} ({steer.get('why')}): the evidence allows only {sorted(want)} (written {written} "
                     f"of {length}, native end {model.native}, answer read {model.answer_read})")


@pytest.mark.parametrize("block", range(8))
def test_model_random_sequences_of_the_node_and_host_write_authority(tmp_path, block):
    """Seeded random sequences (timely, late and in-fetch acknowledgements, every kind of listing, gate loss and
    recovery, partial and full writes, N's native end, cancels of N and of a message behind it through the node's
    reconciliation, stops, the watchdog, steps across 5 s and 75 s) against `_Model`: no byte of S without authority
    at that instant, S at most once, nothing of S after N ended, nothing after a frame left in part, a broken clock
    never heals, one outcome for a held S on N's end and the one the evidence allows. On failure the seed and every
    step are printed."""
    for k in range(25):
        seed = block * 1000 + k
        _run_model(tmp_path / f"s{seed}", seed)


def test_model_progress_a_fresh_offer_into_a_proved_turn_is_written_and_settles_in_turn(tmp_path):
    """Review round 3, R3-11: a deterministic case asserts PROGRESS, not only safety: every condition holds, so S is
    written whole, answered, and N's end says `in_turn`."""
    seen, model, _ = _run_model(tmp_path, 0, script=[("pass", ("with", "timely", [])), ("answer", None),
                                                     ("end", None), ("local", None)])
    spans, _ = _frame_bytes(seen)
    assert sum(p for _, p, _, _ in spans) == len(seen.frames[0]), "written whole"
    assert seen.ends and seen.ends[0].steer == {"outcome": "in_turn", "why": "answered"}


def test_model_settlement_a_held_steer_not_one_byte_of_which_went_settles_not_delivered(tmp_path):
    """R3-11: a steer held with its frame queued and the pipe full (not a byte written) still gets its outcome at N's
    end: `not_delivered`."""
    seen, model, _ = _run_model(tmp_path, 0, script=[("accept", 0), ("pass", ("with", "timely", [])),
                                                     ("end", None), ("local", None)])
    assert _frame_bytes(seen)[0] == []
    assert seen.ends and seen.ends[0].steer["outcome"] == "not_delivered"


@pytest.mark.parametrize("when", ["before", "after"])
def test_model_reach_an_acknowledgement_at_the_write_boundary_either_side_of_the_lock(tmp_path, when):
    """Round 4 (R3-11): the model reaches an acknowledgement published concurrently with the write, at the boundary,
    either side of the attempt's lock, deterministically (`steer_write_hook`): it fires, the write stays authorized
    and whole, and N's end says `in_turn`."""
    seen, model, log_ = _run_model(tmp_path, 0, script=[("boundary_ack", (when, 0.0)), ("pass", ("with", "timely", [])),
                                                        ("answer", None), ("end", None), ("local", None)])
    assert any("the boundary's hello published" in x for x in log_), "the boundary's acknowledgement fired"
    spans, _ = _frame_bytes(seen)
    assert sum(p for _, p, _, _ in spans) == len(seen.frames[0])
    assert seen.ends and seen.ends[0].steer == {"outcome": "in_turn", "why": "answered"}


def test_model_every_sequence_ends_with_the_hosts_end_of_n(tmp_path):
    """Round 4 (R3-11): a sequence that never ended N is ended natively after its last step, and the host's end, with
    the steer's one outcome, is then required and checked."""
    seen, model, log_ = _run_model(tmp_path, 0, script=[("accept", 0), ("pass", ("with", "timely", []))])
    assert any(x.startswith("end: N ends natively") for x in log_)
    assert seen.ends and seen.ends[0].steer["outcome"] == "not_delivered"


def test_model_checker_follows_every_frame_not_only_the_first():
    """Round 4 (R3-11): the checker itself. A second frame's bytes are seen (here, written without authority, and
    written at all beside the first's): it fails on them; the first frame alone was clean."""
    from types import SimpleNamespace
    first, second = b'{"id": 7, "method": "turn/steer"}\n', b'{"id":8,"method":"turn/steer"}\n'
    model = _Model()
    clean = SimpleNamespace(frames=[first, second], writes=[(1.0, first, len(first), True, False)], ends=[])
    _check(SimpleNamespace(stdin=bytearray(first)), clean, model, [])
    two = SimpleNamespace(frames=[first, second], ends=[],
                          writes=[(1.0, first, len(first), True, False), (2.0, second, 5, True, False)])
    with pytest.raises(AssertionError, match="more than one steer frame"):       # (the second only in part)
        _check(SimpleNamespace(stdin=bytearray(first + second[:5])), two, model, [])
    late = SimpleNamespace(frames=[first, second], ends=[], writes=[(2.0, second, len(second), False, False)])
    with pytest.raises(AssertionError, match=r"frame 1\) was written at 2.0 without authority"):
        _check(SimpleNamespace(stdin=bytearray(second)), late, model, [])


def test_model_the_checker_finds_an_unauthorized_prefix_followed_by_another_line():
    """R3-11, the checker itself: a partial frame written without authority and then another line, and a held steer
    whose N ended with no outcome, are both found."""
    from types import SimpleNamespace
    frame = b'{"id": 9, "method": "turn/steer", "params": {}}\n'
    child = SimpleNamespace(stdin=frame[:20] + b'{"other": 1}\n')
    seen = SimpleNamespace(frames=[frame], ends=[],
                           writes=[(1.0, frame, 20, False, False), (2.0, b'{"other": 1}\n', 13, True, False)])
    with pytest.raises(AssertionError, match="left in part|without authority"):
        _check(child, seen, _Model(), ["synthetic"])
    held = _Model()
    held.held = True
    from agent_comms.session_host import HostEvent
    seen = SimpleNamespace(frames=[], writes=[], ends=[HostEvent("s", "turn_ended", "1", ok=True)])
    with pytest.raises(AssertionError, match="no outcome"):
        _check(SimpleNamespace(stdin=b""), seen, held, ["synthetic"])


# --- the gaps round 2's mutation check found


def test_r2_05_a_drain_that_keeps_making_progress_stops_at_its_time_bound(henv):
    """A process that writes without end (each read takes bytes and 0.5 s): the drain stops at its 2 s bound, and
    the stop goes on."""
    host, s, child, _ = host_n(henv)
    host.steer_offer(SID, "a", "u1", TEXT, authorized=True)
    host.tick()
    reads = []
    child.bytes_read = 0

    def endless():
        reads.append(1)
        henv.clock.advance(0.5)
        child.bytes_read += 4096
        return []
    child.read_lines = endless
    host.close(SID, "closed")
    assert len(reads) <= 2.0 / 0.5 + 1, "bounded by time, not only by its read count"
    assert s.ended


def test_r2_05_the_piped_child_counts_the_bytes_a_read_takes_into_a_line_not_complete_yet(tmp_path):
    import sys
    import time
    from agent_comms.dispatch import spawn_piped
    child = spawn_piped([sys.executable, "-c", "import sys, time; sys.stdout.write('abc'); sys.stdout.flush(); "
                         "time.sleep(5)"], cwd=str(tmp_path), env={"PATH": "/usr/bin:/bin"},
                        log_path=tmp_path / "stderr.log")
    try:
        deadline = time.monotonic() + 10
        lines = []
        while child.bytes_read == 0 and time.monotonic() < deadline:
            lines += child.read_lines()
            time.sleep(0.05)
        assert lines == [] and child.bytes_read == 3, "no line yet, and the read made progress"
    finally:
        child.kill()
        child.proc.wait(10)
        child.close()


def test_r2_07_an_acknowledgement_received_after_the_contact_ran_out_is_a_break_even_during_a_fetch(env):
    """Contact acknowledged at 0, the fetch sent at 70; a hello SENT at 72 is acknowledged only at 80, after the
    contact ran out at 75: a break. The offer read at 81 holds no authority, though that hello is under 75 s old."""
    sid, child = codex_turn_in_flight(env)
    runner = env.node.sessions
    t0 = []

    class LateHello:
        slow = False

        def request(self, method, path, **kw):
            if method == "GET" and path == "/api/nodes/sessions" and self.slow:
                env.clock.advance(10)
                runner.note_hello(t0[0] + 72)       # sent at 72, its acknowledgement read at 80
                env.clock.advance(1)
                return env.laptop.request(method, path, **kw)
            r = env.laptop.request(method, path, **kw)
            if method == "POST" and path.startswith("/api/nodes/sessions/") and r.status_code == 200:
                return httpx.Response(200, json=r.json() | {"in_flight": None}, request=r.request)
            return r
    client = LateHello()
    runner.client = client
    tick(env)
    runner.report_ack.clear()
    post_steer(env, sid)
    env.node.hello()
    t0.append(env.clock())
    env.clock.advance(70)
    client.slow = True
    tick(env)
    client.slow = False
    assert steers_of(child) == [], "the contact broke at 75: no authority"


def test_r2_09_a_break_latched_while_the_host_was_busy_is_kept_for_good(env):
    """A hello SENT at 74 s is acknowledged at 80 s, while the host's lock is held: the break is latched at once in
    the attempt's own record (review round 3: no notification has to reach the host). Later the latest contact is
    only 6 s old, yet the authority is lost."""
    sid, child, s = held_slot(env)
    runner = env.node.sessions
    t0 = runner.steer_seen[sid]
    env.clock.advance(80)
    with runner.lock:                   # busy: the latch does not need the host's lock
        runner.note_hello(t0 + 74)
        assert s.steer.revoked == "contact"
    assert not env.node.sessions.host._steer_gate(s, s.steer, env.clock()) and s.steer.revoked == "contact"


def test_r2_10_the_waits_answer_sweeps_first(env):
    sid, kid, child = codex_child(env)
    hs.send(env.board, env.p["claude"], bound(env, sid), kid, message=TEXT, mode="steer")
    entry(env, kid)
    with hosted.db.write_tx(env.board.conn) as c:     # an older hub settled N; the node is gone since
        m = hosted._get(c, hosted._msg_key(kid, 1))
        hosted._put(c, hosted._msg_key(kid, 1), m | {"state": "done", "ok": True}, "x", env.clock())
        r = hosted._session(c, kid)
        hosted._put(c, hosted.SESSION_PREFIX + kid, r | {"in_flight": None}, "x", env.clock())
    out = hs.steer_waited(env.board, sid, kid, 1, None)
    assert out["steer_status"]["outcome"] == "unconfirmed" and out.get("note") != hs.STEER_UNSETTLED


def test_r2_06_no_retry_is_made_with_under_a_second_of_the_budget_left(env):
    from agent_comms.session_host import HostEvent
    runner = env.node.sessions
    sid, child = codex_turn_in_flight(env)
    timed = _Timed(env, 19.5)
    runner.client = timed
    runner.hub_features, runner.features_known = {"answered", "restart", "steer"}, True
    runner._emit([HostEvent(sid, "turn_ended", "1", ok=True, text="the end", answered=True,
                            steer={"outcome": "unconfirmed", "why": "turn_cut"})])
    runner._flush()
    assert len(timed.calls) == 1, "half a second left: no retry in this flush"
    assert any(x == sid for x, _ in runner.pending), "kept for the next flush"


def _fetch_with(env, during, before=None):
    """The node's first fetch of an offer of S, sent 70 s after a hello, with `during(t0)` run while it is out; its
    reports are taken but their answers name no turn (only what the test adds is evidence). Returns the child."""
    sid, child = codex_turn_in_flight(env)
    runner = env.node.sessions
    t0 = []

    class Hub:
        slow = False

        def request(self, method, path, **kw):
            if method == "GET" and path == "/api/nodes/sessions" and self.slow:
                during(t0[0])
                return env.laptop.request(method, path, **kw)
            r = env.laptop.request(method, path, **kw)
            if method == "POST" and path.startswith("/api/nodes/sessions/") and r.status_code == 200:
                return httpx.Response(200, json=r.json() | {"in_flight": None}, request=r.request)
            return r
    hub = Hub()
    runner.client = hub
    tick(env)
    runner.report_ack.clear()
    post_steer(env, sid)
    env.node.hello()
    t0.append(env.clock())
    if before is not None:
        before(sid, t0[0])
    env.clock.advance(70)
    hub.slow = True
    tick(env)
    hub.slow = False
    return sid, child


def test_r2_07_an_acknowledgement_for_another_turn_never_bridges_a_break(env):
    """While the fetch is out: a report whose answer names ANOTHER turn (sent at 72), then a hello sent at 74 and
    acknowledged at 80; the answer read at 81 (11 s old). For N the contact ran out at 75: a break, which that report
    must not bridge."""
    runner = env.node.sessions
    sid_ = []

    def during(t0):
        env.clock.advance(2)
        runner._ack_report(sid_[0], t0 + 72, {"session_id": sid_[0], "in_flight": 2})
        env.clock.advance(8)
        runner.note_hello(t0 + 74)
        env.clock.advance(1)
    sid, child = _fetch_with(env, during, before=lambda sid, t0: sid_.append(sid))
    assert steers_of(child) == []


def test_r2_07_what_an_earlier_fetch_saw_never_bridges_a_break_in_this_one(env):
    """An acknowledgement left from an earlier fetch (a report naming N, at 60, superseded by one naming another turn
    at 65) must not count in this fetch's walk: the hello acknowledged at 80 still comes after the break at 75."""
    runner = env.node.sessions

    def before(sid, t0):
        # (Review round 3: published through the node's own path, before the fetch is sent, not put in a list.)
        runner._publish_ack(t0 + 60, sid, 1)
        runner._publish_ack(t0 + 65, sid, 2)

    def during(t0):
        env.clock.advance(10)
        runner.note_hello(t0 + 74)
        env.clock.advance(1)
    sid, child = _fetch_with(env, during, before=before)
    assert steers_of(child) == []


# ------------------------------------------------------------ review round 3 (R3-01 .. R3-12, part O, the parity rule)


def _near_deadline(env):
    """A held, unproved steer; the clock 74.9 s after the contact its authority rests on; the turn proved now, so the
    frame is made and written in the next HOST pass (no report renews the contact in between)."""
    sid, child, s = held_slot(env)
    runner = env.node.sessions
    t0 = runner.steer_seen[sid]
    env.clock.advance(t0 + 74.9 - env.clock())
    st = last_start(child)
    child.emit(user_item(st["params"]["clientUserMessageId"]))
    return sid, child, s, runner, t0


def test_r3_01_a_break_published_while_the_write_runs_comes_after_it(env):
    """The check passes at 74.9 s; during the pipe write the deadline passes and a hello is acknowledged (a break,
    latched). That acknowledgement must not be published before the write it follows returned: it waits for the
    attempt's lock, which the boundary holds across its check and its write. (Round 4: the acknowledgement's arrival
    at that lock is signalled by the lock itself, not guessed from a timeout.)"""
    import threading
    sid, child, s, runner, t0 = _near_deadline(env)
    lock = _SignalLock(s.steer.lock)
    s.steer.lock = lock
    real = child.write
    seen, threads = [], []

    def write(data):
        if b'"turn/steer"' in data and not seen:
            env.clock.advance(0.2)
            th = threading.Thread(target=runner.note_hello, args=(t0 + 1,))
            threads.append(th)
            th.start()
            assert lock.waiting.wait(30), "the acknowledgement never reached the record's lock"
            seen.append(s.steer.revoked)        # None: the break waits for this write to return
        return real(data)
    child.write = write
    with runner.lock:
        runner._emit(runner.host.tick())
    for th in threads:
        th.join(30)
    assert seen == [None], "the break was latched before the write it follows"
    assert not threads[0].is_alive()


def test_r3_01_the_clocks_are_read_under_the_attempts_lock_after_every_callback(env):
    """(The boundary's hook, deterministic:) just before the check the deadline passes; the check, reading its clock
    under the lock after the callbacks, writes nothing; a break latched just after a write leaves the rest unwritten."""
    sid, child, s, runner, t0 = _near_deadline(env)
    host = runner.host

    def before(when, sess):
        if when == "before":
            env.clock.advance(0.2)
    host.steer_write_hook = before
    with runner.lock:
        runner._emit(host.tick())
    assert steers_of(child) == [] and s.steer.revoked == "contact"


def test_r3_01_a_break_latched_just_after_a_partial_write_leaves_the_rest_unwritten(env):
    sid, child, s, runner, t0 = _near_deadline(env)
    host = runner.host
    child.accept = 10

    def after(when, sess):
        if when == "after":
            env.clock.advance(0.2)
            runner.note_hello(t0 + 1)       # received after the contact ran out: latched
    host.steer_write_hook = after
    with runner.lock:
        runner._emit(host.tick())
    host.steer_write_hook = None
    child.accept = None
    before = len(child.stdin)
    with runner.lock:
        events = host.tick()
        runner._emit(events)
    assert len(child.stdin) == before and [e.detail for e in ended_of(events)] == [STEER_CUT]


def test_r3_02_a_same_uuid_offer_for_another_n_never_touches_ns_contact(env):
    """A report whose answer names turn 2, and a listing naming S's uuid for turn 2: neither is evidence for N (1).
    N's contact runs out at 75 s; a hello acknowledged at 100 s comes after the break and never revives it."""
    sid, child, s = held_slot(env)
    runner = env.node.sessions
    t0 = runner.steer_seen[sid]

    class WrongN:
        def request(self, method, path, **kw):
            r = env.laptop.request(method, path, **kw)
            if method == "GET" and path == "/api/nodes/sessions" and r.status_code == 200:
                data = r.json()
                for e in data["sessions"]:
                    if e["session_id"] == sid and isinstance(e.get("steer"), dict):
                        e["steer"] = e["steer"] | {"into": 2}
                return httpx.Response(200, json=data, request=r.request)
            if method == "POST" and path.startswith("/api/nodes/sessions/") and r.status_code == 200:
                return httpx.Response(200, json=r.json() | {"in_flight": 2}, request=r.request)
            return r
    runner.client = WrongN()
    env.clock.advance(60)
    runner._ack_report(sid, t0 + 60, {"session_id": sid, "in_flight": 2})
    runner.fresh, runner.last_sync = False, float("-inf")
    real_listed = runner._steer_listed

    def listed_then_a_late_hello(*a, **kw):
        real_listed(*a, **kw)
        # Right after the listing is applied (before anything else of this pass): 100 s after N's contact, a hello
        # sent at 99 s is acknowledged.
        env.clock.advance(t0 + 100 - env.clock())
        runner.note_hello(t0 + 99)
    runner._steer_listed = listed_then_a_late_hello
    tick(env)
    runner._steer_listed = real_listed
    st = last_start(child)
    child.emit(user_item(st["params"]["clientUserMessageId"]))      # proved now
    with runner.lock:
        runner._emit(runner.host.tick())
    assert steers_of(child) == [], "N's contact broke at 75 s; turn 2's evidence never counted for it"


def test_r3_02_a_revocation_is_revalidated_against_the_attempts_key(henv):
    host, s, child, _ = host_n(henv)
    host.steer_offer(SID, "a", "u1", TEXT, authorized=True)
    host.steer_revoke(SID, "u1", "omitted", "b")        # for another N: not this attempt
    host.steer_revoke(SID, "u2", "omitted")             # another steer
    assert s.steer.revoked is None
    host.tick()
    assert len(steers_of(child)) == 1


def test_r3_03_an_answer_received_in_time_is_fresh_however_long_its_reconciliation_waits(env):
    """The listing's answer comes 0 s after its send; the loop then waits 25 s for its lock (the watchdog's work):
    the offer's age is judged at receipt, the authority at the write, and S is written. (Round 4: the loop's
    wait is signalled by the lock, not assumed after a sleep.)"""
    import threading
    sid, child = codex_turn_in_flight(env)
    post_steer(env, sid)
    runner = env.node.sessions
    holders = []

    class Busy:
        armed = True

        def request(self, method, path, **kw):
            r = env.laptop.request(method, path, **kw)
            if method == "GET" and path == "/api/nodes/sessions" and self.armed:
                self.armed = False
                got = threading.Event()
                lock = _SignalLock(runner.lock)
                runner.lock = lock

                def hold():
                    lock.quiet = threading.get_ident()
                    with lock:
                        got.set()
                        assert lock.waiting.wait(30), "the loop never waited for the lock"
                        env.clock.advance(node_sessions.STEER_OFFER_AGE + 5)
                h = threading.Thread(target=hold)
                holders.append(h)
                h.start()
                assert got.wait(30)
            return r
    runner.client = Busy()
    runner.fresh, runner.last_sync = False, float("-inf")
    tick(env)
    for h in holders:
        h.join(30)
    runner.lock = runner.lock.real if isinstance(runner.lock, _SignalLock) else runner.lock
    assert len(steers_of(child)) == 1, "an answer received in time is fresh"


def test_r3_03_a_hello_sent_again_is_dated_from_the_attempt_that_got_through(env):
    node = env.node
    object.__setattr__(node.config, "runner_homes", {"claude": "/tmp/agent-comms-test-home"})
    real = node.client

    class RefusesFirst:
        first = True

        def request(self, method, path, **kw):
            if method == "POST" and path == "/api/nodes/hello" and self.first:
                self.first = False
                env.clock.advance(30)
                return httpx.Response(422, json={"detail": "homes"}, request=httpx.Request(method, "http://hub" + path))
            return real.request(method, path, **kw)
    node.client = RefusesFirst()
    t = env.clock()
    node.hello()
    node.client = real
    assert env.node.sessions.hello_ack == t + 30, "dated from the attempt that got through"


def _endless(child, clock, step=0.5):
    """The child's reads take bytes without end (a line never completes), each read `step` s of the clock."""
    child.bytes_read = 0
    reads = []

    def read():
        reads.append(1)
        clock.advance(step)
        child.bytes_read += 4096
        return []
    child.read_lines = read
    return reads


def test_r3_04_shutdown_drains_every_session_within_one_deadline(env):
    runner = env.node.sessions
    children = []
    for project in (PROJECT, P2, P3):
        sid, child = codex_turn_in_flight(env, project=project)
        post_steer(env, sid)
        children.append(child)
    tick(env)
    assert all(runner.host.sessions[x].steer is not None for x in runner.host.sessions)
    for child in children:
        _endless(child, env.clock)
    t = env.clock()
    runner.signal()
    assert env.clock() - t <= 2.0 + 0.5, "one deadline for the whole shutdown, not one per session"
    assert all(c.terminated for c in children)
    runner.stopping.clear()


def test_r3_04_a_sessions_drain_stops_once_ns_end_is_read(henv):
    host, s, child, _ = host_n(henv)
    host.steer_offer(SID, "a", "u1", TEXT, authorized=True)
    host.tick()
    [req] = steers_of(child)
    child.emit(cx.reply(req["id"], {"turnId": cx.TURN}), cx.message("native"), cx.completed())
    real, reads = child.read_lines, []

    def read():
        reads.append(1)
        if len(reads) == 1:
            return real()
        henv.clock.advance(0.5)
        child.bytes_read = getattr(child, "bytes_read", 0) + 4096     # more bytes after N's end
        return []
    child.read_lines = read
    [end] = ended_of(host.drain())
    assert end.ok and end.text == "native" and len(reads) == 1, "N's end read: nothing more is drained"


def test_r3_04_the_grace_starts_at_the_actual_sigterm(env):
    from agent_comms import node as node_mod
    sid, child = codex_turn_in_flight(env)
    post_steer(env, sid)
    tick(env)
    child.exits_on_terminate = False
    _endless(child, env.clock)
    times = {}
    real_term, real_kill = child.terminate, child.kill

    def term():
        times.setdefault("term", env.clock())
        return real_term()

    def kill():
        times.setdefault("kill", env.clock())
        return real_kill()
    child.terminate, child.kill = term, kill
    node_mod.shut_down(env.node)
    assert times["kill"] - times["term"] >= env.node.config.kill_grace_seconds, "the whole grace after the SIGTERM"


def _steered_answered(henv):
    host, s, child, _ = host_n(henv)
    host.steer_offer(SID, "a", "u1", TEXT, authorized=True)
    host.tick()
    [req] = steers_of(child)
    return host, s, child, req


def test_r3_05_a_native_end_behind_buffered_output_wins_over_the_reap(henv):
    host, s, child, req = _steered_answered(henv)
    child.emit(cx.reply(req["id"], {"turnId": cx.TURN}), cx.message("native"), cx.completed())
    _fragmented(child, 2)
    child.code = 0                      # the process exited with its end still in the pipe
    ends = ended_of(host.tick())
    assert [(e.ok, e.text) for e in ends] == [(True, "native")]
    assert ends[0].steer == {"outcome": "in_turn", "why": "answered"}


def test_r3_05_a_native_end_behind_buffered_output_wins_over_the_turn_timeout(henv):
    host, s, child, req = _steered_answered(henv)
    henv.clock.advance(30 * 60)
    host.tick()                         # the timeout: Codex's own interrupt is asked for first
    child.emit(cx.reply(req["id"], {"turnId": cx.TURN}), cx.message("native"), cx.completed())
    _fragmented(child, 2)
    henv.clock.advance(host.kill_grace)
    ends = ended_of(host.tick())
    assert [e.text for e in ends] == ["native"], "N's own end (after the interrupt), not the host's timeout"
    assert ends[0].steer == {"outcome": "in_turn", "why": "answered"}


def test_r3_05_a_native_end_behind_buffered_output_wins_over_a_broken_pipe(henv):
    host, s, child, _ = host_n(henv)
    real = child.write

    def write(data):
        if b'"turn/steer"' in data:
            raise BrokenPipeError("the process no longer reads")
        return real(data)
    child.write = write
    child.emit(cx.message("native"), cx.completed())
    _fragmented(child, 2)
    host.steer_offer(SID, "a", "u1", TEXT, authorized=True)
    ends = ended_of(host.tick())
    assert [(e.ok, e.text) for e in ends] == [(True, "native")]
    assert ends[0].steer == {"outcome": "not_delivered", "why": "not_written"}


def test_r3_06_a_report_exchange_has_a_total_deadline_whatever_the_transport_does(env, monkeypatch):
    """A slow drip: the hub's answer never comes until the test lets it, every byte inside any per-operation timeout.
    The exchange of a report carrying a steer outcome is given up at its total deadline, and the end stays queued.
    (Round 4: no measured time: the answer is held until the flush has RETURNED, which it could do only by its own
    deadline.)"""
    import threading
    from agent_comms.session_host import HostEvent
    monkeypatch.setattr(node_sessions, "REPORT_PAIR_SECONDS", 0.5)
    monkeypatch.setattr(node_sessions, "REPORT_FIRST_SECONDS", 0.4, raising=False)
    monkeypatch.setattr(node_sessions, "REPORT_RETRY_SECONDS", 0.1, raising=False)
    runner = env.node.sessions
    sid, child = codex_turn_in_flight(env)
    gate = threading.Event()

    class Drip:
        def request(self, method, path, **kw):
            if method == "POST":
                gate.wait()             # held until the flush below has returned
            return env.laptop.request(method, path, **kw)
    runner.client = Drip()
    runner.hub_features, runner.features_known = {"answered", "restart", "steer"}, True
    runner._emit([HostEvent(sid, "turn_ended", "1", ok=True, text="the end", answered=True,
                            steer={"outcome": "unconfirmed", "why": "turn_cut"})])
    flush = threading.Thread(target=runner._flush, daemon=True)
    flush.start()
    flush.join(60)
    returned = not flush.is_alive()
    gate.set()
    assert returned, "the flush waited for the hub's answer: no total deadline"
    assert any(x == sid for x, _ in runner.pending)


class _Durations:
    """The node's client with each POST taking the next of `takes` seconds of the fake clock (cut short when the
    call's own timeout is shorter); a report carrying a steer outcome that gets through is refused (422)."""

    def __init__(self, env, takes):
        self.env, self.takes, self.calls = env, list(takes), []

    def request(self, method, path, **kw):
        if method != "POST":
            return self.env.laptop.request(method, path, **kw)
        takes = self.takes.pop(0) if self.takes else 0.0
        t = kw.get("timeout")
        limit = 20.0 if t is None else (t.read if isinstance(t, httpx.Timeout) else float(t))
        self.calls.append((takes, limit))
        if limit < takes:
            self.env.clock.advance(limit)
            raise httpx.ReadTimeout("timed out")
        self.env.clock.advance(takes)
        if any("steer" in e for e in (kw.get("json") or {}).get("events", [])):
            return httpx.Response(422, json={"detail": "extra field"}, request=httpx.Request(method, "http://hub" + path))
        return self.env.laptop.request(method, path, **kw)


def test_r3_06_the_final_flush_sends_ns_end_plainly_after_a_slow_refusal(env):
    """Shutdown's last flush: the report with N's steer outcome takes 19.5 s and is refused (422). N's end still
    goes, plainly, in the retry's own reserved time: never left behind on exit."""
    from agent_comms.session_host import HostEvent
    runner = env.node.sessions
    sid, child = codex_turn_in_flight(env)
    runner.client = _Durations(env, [19.5, 0.1])
    runner.hub_features, runner.features_known = {"answered", "restart", "steer"}, True
    runner._emit([HostEvent(sid, "turn_ended", "1", ok=True, text="the end", answered=True,
                            steer={"outcome": "unconfirmed", "why": "turn_cut"})])
    runner.stopping.set()
    assert runner._flush(final=True)
    runner.stopping.clear()
    assert msg(env, sid, 1)["state"] == "done" and runner.pending == []


def test_r3_07_an_unheld_cancel_never_posts_the_watchdogs_end_ahead_of_lost(env):
    """No steer anywhere: the watchdog stopped a child's turn (its end waits for the loop); the parent cancels its
    queued task. The node posts what main posts, in main's order: LOST for N first."""
    sid, kid, child = codex_child(env)
    hs.send(env.board, env.p["claude"], bound(env, sid), kid, message="a task behind it")
    runner = env.node.sessions
    posted = []
    real = runner.client

    class Recording:
        def request(self, method, path, **kw):
            if method == "POST" and path == f"/api/nodes/sessions/{kid}":
                posted.extend((e.get("message"), e.get("detail") or "") for e in (kw.get("json") or {}).get("events", [])
                              if e.get("kind") == "turn_ended")
            return real.request(method, path, **kw)
    runner.client = Recording()
    env.clock.advance(runner.self_stop_seconds + 1)
    runner.enforce_deadlines()
    hs.cancel(env.board, env.p["claude"], bound(env, sid), kid)
    tick(env, times=3)
    first_n = next(d for m, d in posted if m == 1)
    assert first_n.startswith("the node restarted"), f"main reports LOST first; this posted: {first_n!r}"


def _steer_reads(monkeypatch):
    """Every read of a steer record: a point read of a steer key, or a range scan of the steer prefix."""
    reads = []
    real_get, real_range = hosted_steer._get, hosted._range

    def get(conn, key, *a, **kw):
        if str(key).startswith(hosted_steer.STEER_PREFIX):
            reads.append(("get", key))
        return real_get(conn, key, *a, **kw)

    def range_(prefix, *a, **kw):
        if str(prefix).startswith(hosted_steer.STEER_PREFIX):
            reads.append(("range", prefix))
        return real_range(prefix, *a, **kw)
    monkeypatch.setattr(hosted_steer, "_get", get)
    monkeypatch.setattr(hosted, "_range", range_)
    return reads


def test_r3_08_no_steer_read_of_any_kind_for_a_session_that_never_took_one(env, monkeypatch):
    sid, kid, child = codex_child(env)
    end_turn(env, child, "the task's answer")
    root_sid = sid
    reads = _steer_reads(monkeypatch)
    for target in (root_sid, kid):
        show(env, target)
        env.human.get(f"/api/hosted-sessions/{target}/messages/1")
        env.human.get(f"/api/hosted-sessions/{target}/messages/1", params={"steer": 1})
    env.human.get("/api/hosted-sessions")
    hs.status(env.board, env.p["claude"], bound(env, root_sid), kid)
    mine = [r for r in reads if root_sid in r[1] or kid in r[1]]
    assert mine == [], f"steer reads for sessions that never took one: {mine}"


def test_r3_09_one_malformed_stored_steer_neither_fails_nor_hides_the_listing(env):
    good, gchild = codex_turn_in_flight(env)
    bad, bchild = codex_turn_in_flight(env, project=P2)
    with hosted.db.write_tx(env.board.conn) as c:      # a stored steer no current build would accept
        r = hosted._session(c, bad)
        m = hosted._get(c, hosted._msg_key(bad, 1))
        hosted_steer.accept(c, r, m, "steer \ud800 here", "user", {"human": "peter"}, "peter", None, env.clock())
    try:
        got = env.laptop.get("/api/nodes/sessions", params={"tree": 5})
        ok, data = got.status_code == 200, got.json() if got.status_code == 200 else {}
    except Exception:
        ok, data = False, {}
    assert ok, "the listing still answers"
    ids = {e["session_id"]: e for e in data["sessions"]}
    assert good in ids and bad in ids and "steer" not in ids[bad]
    rec = steer_rec(env, bad)
    assert rec["withdrawn_at"] is not None and rec["listed_at"] is None
    end_turn(env, bchild)
    assert steer_rec(env, bad)["outcome"] == "not_delivered"


def test_r3_10_a_known_outcome_keeps_its_exit_when_n_is_pruned_before_the_get(env, monkeypatch):
    sid, child = codex_turn_in_flight(env, prove=False)
    post_steer(env, sid, request_id="q1")
    tick(env)
    end_turn(env, child)
    assert steer_rec(env, sid)["outcome"] == "not_delivered"
    first = []

    def api(settings, token, method, path, body=None, timeout=10):
        if method == "GET" and "/messages/" in path and not first:
            first.append(path)
            with hosted.db.write_tx(env.board.conn) as c:       # pruned meanwhile
                c.execute("DELETE FROM board_state WHERE key = ?", (hosted._msg_key(sid, 1),))
                c.execute("DELETE FROM board_state WHERE key = ?", (hosted_steer._key(sid, 1),))
        r = env.human.request(method, path, json=body)
        if r.status_code != 200:
            raise SystemExit(f"error ({r.json().get('error')}): {r.json().get('message')}")
        return r.json()
    monkeypatch.setattr(cli, "_api", api)
    monkeypatch.setattr(cli, "_human_token", lambda: "t")
    assert run_cli("send", sid, TEXT, "--mode", "steer", "--request-id", "q1", "--wait") == 5


def test_r3_10_the_waits_note_points_at_steer_status_and_names_the_carrier_only_when_it_carries_it(env, monkeypatch):
    sid, kid, child = codex_child(env)
    hs.send(env.board, env.p["claude"], bound(env, sid), kid, message=TEXT, mode="steer")
    real = hosted_results._results
    carried = []

    def one(conn, parent):
        if parent != sid:
            return real(conn, parent)
        return [{"child": kid, "message": 1, "state": "delivered", "n": 7} | (carried[0] if carried else {})]
    monkeypatch.setattr(hosted_results, "_results", one)
    note = hs.steer_waited(env.board, sid, kid, 1, None)["note"]
    assert "steer_status" in note and "with the steer's outcome" not in note and "with it" not in note
    carried.append({"steer": {"outcome": "in_turn", "why": "answered"}})
    note = hs.steer_waited(env.board, sid, kid, 1, None)["note"]
    assert "steer_status" in note and "with the steer's outcome" in note


@pytest.mark.parametrize("script", [["wrong_turn_item", "item", "answer", "done"],
                                    ["float_answer_other", "answer", "item", "done"]],
                         ids=["a_wrong_turn_item_first", "a_float_id_answer_first"])
def test_r3_12_the_join_probe_records_only_ts_item_and_the_integer_request_id(tmp_path, script):
    run = _probe(tmp_path, script)
    run._m5_join_probe()        # passes: the decoy is never recorded in place of the real observation


def test_o_the_omission_after_a_partial_write_writes_not_one_byte_more(env):
    """Part O (round 3): the partial case asserts the exact bytes written."""
    sid, child = codex_turn_in_flight(env)
    child.accept = 10
    post_steer(env, sid)
    tick(env)
    s = env.node.sessions.host.sessions[sid]
    sent = s.steer.sent
    frame_head = bytes(s.steer.frame)[:sent]
    with hosted.db.write_tx(env.board.conn) as c:
        hosted_steer.cancel(c, sid, 1, env.clock(), "x")
    child.accept = None
    before = bytes(child.stdin)
    tick(env)
    assert bytes(child.stdin) == before and before.endswith(frame_head), "the prefix only, not one byte more"


# --- the gaps round 3's mutation check found


def test_r3_gap_a_valid_steer_in_an_answer_that_cannot_be_written_is_not_exposed(env):
    """Another session's queued text cannot be written as UTF-8 (a damaged record): the listing's answer cannot be
    sent, so the valid steer in it was never exposed (`listed_at` stays unset)."""
    good, gchild = codex_turn_in_flight(env)
    other, ochild = codex_turn_in_flight(env, project=P2)
    env.human.post(f"/api/hosted-sessions/{other}/messages", json={"message": "queued behind"})
    with hosted.db.write_tx(env.board.conn) as c:
        m = hosted._get(c, hosted._msg_key(other, 2))
        hosted._put(c, hosted._msg_key(other, 2), m | {"text": "queued \ud800 behind"}, "x", env.clock())
    post_steer(env, good)
    try:
        env.laptop.get("/api/nodes/sessions", params={"tree": 5})
    except Exception:
        pass
    assert steer_rec(env, good)["listed_at"] is None


def test_r3_gap_an_answer_received_in_time_stays_fresh_with_acknowledgements_after_it(env):
    """R3-03 with a hello acknowledged while the reconciliation waits (after the answer came): the offer's age is
    still the one at receipt."""
    import threading
    sid, child = codex_turn_in_flight(env)
    post_steer(env, sid)
    runner = env.node.sessions
    holders = []

    class Busy:
        armed = True

        def request(self, method, path, **kw):
            r = env.laptop.request(method, path, **kw)
            if method == "GET" and path == "/api/nodes/sessions" and self.armed:
                self.armed = False
                got = threading.Event()
                lock = _SignalLock(runner.lock)
                runner.lock = lock

                def hold():
                    lock.quiet = threading.get_ident()
                    with lock:
                        got.set()
                        assert lock.waiting.wait(30), "the loop never waited for the lock"
                        env.clock.advance(node_sessions.STEER_OFFER_AGE + 5)
                        runner.note_hello(env.clock())       # acknowledged after the answer came
                h = threading.Thread(target=hold)
                holders.append(h)
                h.start()
                assert got.wait(30)
            return r
    runner.client = Busy()
    runner.fresh, runner.last_sync = False, float("-inf")
    tick(env)
    for h in holders:
        h.join(30)
    runner.lock = runner.lock.real if isinstance(runner.lock, _SignalLock) else runner.lock
    assert len(steers_of(child)) == 1, "an answer received in time is fresh"


def test_r3_gap_the_plain_timeout_with_a_frame_queued_drains_first(henv):
    """The timeout's plain branch (no interrupt can be queued behind a frame taken in part): a native end behind
    buffered output is still N's own."""
    host, s, child, _ = partial(henv)
    child.accept = 0
    henv.clock.advance(30 * 60)         # (the timeout is looked at before the frame's 5 s in a pass)
    child.emit(cx.message("native"), cx.completed())
    _fragmented(child, 2)
    ends = ended_of(host.tick())
    assert [e.text for e in ends] == ["native"]


def test_r3_gap_shutdown_signals_the_sessions_holding_no_steer_before_it_drains(env):
    runner = env.node.sessions
    a, ca = codex_turn_in_flight(env)
    post_steer(env, a)
    b, cb = codex_turn_in_flight(env, project=P2)
    tick(env)
    _endless(ca, env.clock)
    at = {}
    real = cb.terminate

    def term():
        at.setdefault("b", env.clock())
        return real()
    cb.terminate = term
    t = env.clock()
    runner.signal()
    assert at.get("b") == t, "the session holding no steer got its SIGTERM before the drain began"
    runner.stopping.clear()


def test_r3_gap_the_final_flush_posts_the_steer_carrying_report_first(env):
    from agent_comms.session_host import HostEvent
    runner = env.node.sessions
    a, ca = codex_turn_in_flight(env)
    b, cb = codex_turn_in_flight(env, project=P2)
    order = []
    real = runner.client

    class Recording:
        def request(self, method, path, **kw):
            if method == "POST" and path.startswith("/api/nodes/sessions/"):
                order.append(path.rsplit("/", 1)[-1])
            return real.request(method, path, **kw)
    runner.client = Recording()
    runner.hub_features, runner.features_known = {"answered", "restart", "steer"}, True
    runner._emit([HostEvent(a, "turn_ended", "1", ok=True, text="a", answered=True),
                  HostEvent(b, "turn_ended", "1", ok=True, text="b", answered=True,
                            steer={"outcome": "unconfirmed", "why": "turn_cut"})])
    runner.stopping.set()
    runner._flush(final=True)
    runner.stopping.clear()
    assert order[0] == b, "the report with the steer outcome first, in the shutdown's own flush"


def test_r3_gap_a_childs_result_reads_no_steer_when_the_child_never_took_one(env, monkeypatch):
    sid, kid, child = codex_child(env)
    reads = _steer_reads(monkeypatch)
    end_turn(env, child, "the task's answer")       # its result is made for the parent
    assert [r for r in reads if kid in r[1]] == []


def test_r3_gap_the_join_probe_fails_at_once_on_an_answer_naming_another_turn(tmp_path):
    run = _probe(tmp_path, ["answer_other", "item", "done"])
    with pytest.raises(SystemExit, match="named another turn"):
        run._m5_join_probe()


class _SignalLock:
    """A lock (or RLock) wrapped so that a test knows, without sleeping, that another thread has come to wait on it:
    `waiting` is set by any acquire from a thread other than `quiet` (the one that made it, unless set)."""

    def __init__(self, real):
        import threading
        self.real, self.quiet, self.waiting = real, threading.get_ident(), threading.Event()

    def acquire(self, blocking=True, timeout=-1):
        import threading
        if threading.get_ident() != self.quiet:
            self.waiting.set()
        return self.real.acquire(blocking, timeout)

    def release(self):
        return self.real.release()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *exc):
        self.release()


class _NoWait(_SignalLock):
    """The host's lock, held by this thread, refusing any wait on it: a caller that needed it fails at once."""

    def __init__(self, real):
        super().__init__(real)
        self.held = False

    def acquire(self, blocking=True, timeout=-1):
        if self.held and blocking:
            raise AssertionError("waited on the host's lock")
        got = self.real.acquire(blocking, timeout)
        self.held = self.held or got
        return got

    def release(self):
        self.held = False
        return self.real.release()


# ------------------------------------------------------------ review round 4 (R4-01, R4-02, R4-09)


def test_r4_01_a_gate_seen_closed_after_authorization_is_latched_before_any_frame_exists(env):
    """S is authorized while N's turn is not proved yet (no frame made); a pass sees the parent's gate closed. When
    the proof and the gate come back, nothing is written: the loss was latched in the record."""
    sid, child, s = held_slot(env)
    runner = env.node.sessions
    host = runner.host
    assert s.steer.authorized and s.steer.frame is None
    real = host.turn_gate
    host.turn_gate = lambda sess: False
    with runner.lock:
        runner._emit(host.tick())
    host.turn_gate = real
    st = last_start(child)
    child.emit(user_item(st["params"]["clientUserMessageId"]))     # proved now
    tick(env)
    tick(env)
    assert steers_of(child) == [], "a gate lost once authorized is lost for good"
    assert s.steer.revoked == "gate"


def test_r4_01_a_write_never_comes_after_the_node_began_to_stop(env):
    """Shutdown begins (the node's stopping flag) just before the boundary takes the record's lock, while a pass
    holds the host's: the write does not happen."""
    sid, child, s = held_slot(env)
    runner = env.node.sessions
    host = runner.host
    st = last_start(child)
    child.emit(user_item(st["params"]["clientUserMessageId"]))

    def stop_now(when, sess):
        if when == "before":
            runner.stopping.set()
    host.steer_write_hook = stop_now
    with runner.lock:
        runner._emit(host.tick())
    host.steer_write_hook = None
    runner.stopping.clear()
    assert steers_of(child) == [], "a steer byte went after the node began to stop"


def test_r4_gap_a_write_checked_after_the_records_were_marked_is_refused_though_the_flag_is_not_set_yet(env):
    """The window inside the flag's `set()`: every record is marked, the flag itself not set yet, and a pass on
    another thread reaches the boundary. The record's own `stopping` refuses it (the flag alone would still say go),
    so no write can be under way once the flag is set. Barriers only: the pass signals its pipe write or its return."""
    import threading
    sid, child, s = held_slot(env)
    runner = env.node.sessions
    host = runner.host
    st = last_start(child)
    child.emit(user_item(st["params"]["clientUserMessageId"]))     # proved: the next pass would write
    progress, release, marked, threads = threading.Event(), threading.Event(), [], []
    real_write = child.write

    def write(data):
        if b'"turn/steer"' in data:
            progress.set()                      # under way in the pipe
            assert release.wait(30)
        return real_write(data)
    child.write = write

    def a_pass():
        try:
            with runner.lock:
                runner._emit(host.tick())
        finally:
            progress.set()                      # returned
    real_mark = host.steer_stopping

    def mark():
        real_mark()
        marked.append(s.steer.stopping)
        th = threading.Thread(target=a_pass)
        threads.append(th)
        th.start()
        assert progress.wait(30)
    host.steer_stopping = mark
    runner.stopping.set()
    release.set()
    for th in threads:
        th.join(30)
    del host.steer_stopping
    runner.stopping.clear()
    assert marked == [True], "setting the flag marks every record first"
    assert steers_of(child) == [], "a steer write was under way when the flag was set"


def test_r4_gap_an_attempt_made_after_the_node_began_to_stop_is_never_written(env):
    """The flag is set inside a pass that is already past its own check, just before it applies a fresh offer into a
    proved turn: the attempt is made after the records were marked, so it has no mark; the node's authority, asked
    at the boundary, refuses it."""
    sid, child = codex_turn_in_flight(env, prove=True)
    post_steer(env, sid)
    runner = env.node.sessions
    host = runner.host
    real_offer = host.steer_offer

    def offer(*a, **kw):
        runner.stopping.set()
        return real_offer(*a, **kw)
    host.steer_offer = offer
    tick(env)
    del host.steer_offer
    runner.stopping.clear()
    s = host.sessions[sid]
    assert s.steer is not None and s.steer.authorized and not getattr(s.steer, "stopping", False), "made unmarked"
    assert steers_of(child) == [], "a steer byte went after the node began to stop"


def test_r4_02_an_acknowledgements_receipt_is_read_after_the_records_lock_is_taken(env):
    """A hello is published at 74.99 s while the record's lock is held (a write under way); it gets the lock at
    75.01 s, after the contact ran out at 75 s: its receipt is 75.01, a break, not a renewal."""
    import threading
    sid, child, s = held_slot(env)
    runner = env.node.sessions
    t0 = runner.steer_seen[sid]
    env.clock.advance(t0 + 74.99 - env.clock())
    lock = _SignalLock(s.steer.lock)
    s.steer.lock = lock
    with lock:                                          # the record's lock held: a write under way
        th = threading.Thread(target=runner.note_hello, args=(t0 + 70,))
        th.start()
        assert lock.waiting.wait(30), "the acknowledgement never came to the record's lock"
        env.clock.advance(0.02)                         # 75.01 s when it gets the lock
    th.join(30)
    assert s.steer.revoked == "contact", "received after the contact ran out: latched, not extended"


def test_r4_02_a_listings_receipt_is_read_after_the_evidence_lock_is_taken(env):
    """Part E's receipt rule at its second call site: the listing's answer comes 0 s after its send, but the lock
    that freezes the fetch's history (`ack_lock`) is held meanwhile for 25 s: its receipt is the time it got that
    lock, so the offer is 25 s old there and not used (an age refusal, not a loss: the next fetch's offer is)."""
    import threading
    sid, child = codex_turn_in_flight(env)
    post_steer(env, sid)
    runner = env.node.sessions
    holders = []

    class Busy:
        armed = True

        def request(self, method, path, **kw):
            r = env.laptop.request(method, path, **kw)
            if method == "GET" and path == "/api/nodes/sessions" and self.armed:
                self.armed = False
                got = threading.Event()
                lock = _SignalLock(runner.ack_lock)
                runner.ack_lock = lock

                def hold():
                    lock.quiet = threading.get_ident()
                    with lock:
                        got.set()
                        assert lock.waiting.wait(30), "the fetch never waited for the evidence lock"
                        env.clock.advance(node_sessions.STEER_OFFER_AGE + 5)
                h = threading.Thread(target=hold)
                holders.append(h)
                h.start()
                assert got.wait(30)
            return r
    runner.client = Busy()
    runner.fresh, runner.last_sync = False, float("-inf")
    tick(env)
    for h in holders:
        h.join(30)
    runner.ack_lock = runner.ack_lock.real if isinstance(runner.ack_lock, _SignalLock) else runner.ack_lock
    assert holders and steers_of(child) == [], "received 25 s after its send: the offer is too old to use"
    runner.note_hello(env.clock())
    runner.fresh, runner.last_sync = False, float("-inf")
    tick(env)
    assert len(steers_of(child)) == 1, "an age refusal is not a loss: the next fresh offer is used"


def test_r4_09_a_known_in_turn_with_n_pruned_says_the_outcome_and_that_ns_status_is_unavailable(env, monkeypatch, capsys):
    sid, child = codex_turn_in_flight(env)
    post_steer(env, sid, request_id="q1")
    tick(env)
    [req] = steers_of(child)
    answer_steer(child, req, {"turnId": cx.TURN})
    end_turn(env, child, "answered")
    assert steer_rec(env, sid)["outcome"] == "in_turn"
    first = []

    def api(settings, token, method, path, body=None, timeout=10):
        if method == "GET" and "/messages/" in path and not first:
            first.append(path)
            with hosted.db.write_tx(env.board.conn) as c:       # N's record pruned meanwhile
                c.execute("DELETE FROM board_state WHERE key = ?", (hosted._msg_key(sid, 1),))
        r = env.human.request(method, path, json=body)
        if r.status_code != 200:
            raise SystemExit(f"error ({r.json().get('error')}): {r.json().get('message')}")
        return r.json()
    monkeypatch.setattr(cli, "_api", api)
    monkeypatch.setattr(cli, "_human_token", lambda: "t")
    assert run_cli("send", sid, TEXT, "--mode", "steer", "--request-id", "q1", "--wait") == 7
    err = capsys.readouterr().err
    assert "outcome is known" in err and cli.STEER_WORDS["in_turn"] in err and "exit status are not available" in err
