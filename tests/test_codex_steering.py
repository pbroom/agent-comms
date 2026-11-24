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
    ({"error": {"code": -32600, "message": "expected active turn id `a` but found `b`"}}, "refused"),
    ({"error": {"code": -32600, "message": "expectedTurnId must not be empty"}}, "refused"),
    ({"error": {"code": -32600, "message": "no active turn to steer, and more"}}, "other"),
    ({"error": {"code": -32603, "message": "no active turn to steer"}}, "other"),
    ({"error": {"code": -32603, "message": "internal error"}}, "other"),
    (None, None),
])
def test_f_what_the_steer_request_was_answered_before_ns_end(answer, want):
    p, _ = proved()
    [req] = cx.sent(p.steer(TEXT))
    if answer is not None:
        p.feed(cx.line({"id": req["id"], **answer}))
    [ended], _ = p.feed(cx.line(cx.completed()))
    assert ended.kind == "turn_ended" and p.steer_result() == want


def test_f_an_answer_after_ns_end_changes_nothing_and_is_logged(caplog):
    p, _ = proved()
    [req] = cx.sent(p.steer(TEXT))
    p.feed(cx.line(cx.completed()))
    assert p.steer_result() is None
    with caplog.at_level("INFO", logger="agent_comms.providers.codex"):
        assert p.feed(cx.reply(req["id"], {"turnId": cx.TURN})) == ([], [])
    assert p.steer_result() is None and "late answer" in caplog.text


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
    host, s, child, _ = partial(henv)
    child.emit(cx.message("done"), cx.completed())      # N ends while the frame is still half written
    [end] = ended_of(host.tick())
    assert end.steer == {"outcome": "unconfirmed", "why": "partial"}
    host.send(SID, "next", message_id="b")
    host.tick()
    assert s.writing is None and list(s.inbox)[0].id == "b", "nothing is staged behind the steer frame"
    child.accept = None
    host.tick()
    assert s.steer is None and steers_of(child), "completed, and the slot is gone"
    host.tick()
    assert last_start(child)["params"]["input"][0]["text"] == "next", "then the next turn, as its own line"


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


def test_e_an_acknowledged_report_naming_n_renews_it_and_one_naming_another_turn_does_not(env):
    runner = env.node.sessions
    runner.hello_ack = float("-inf")
    runner.report_ack = {"s": (100.0, 1)}
    runner.steer_seen = {"s": 100.0}
    assert runner._steer_check("s", 1, 150.0)
    runner.report_ack = {"s": (160.0, 2)}
    assert not runner._steer_check("s", 1, 176.0), "a report whose answer names another turn is no evidence for N"


def test_r7_05_a_late_acknowledgement_cannot_erase_a_stale_interval(env):
    runner = env.node.sessions
    runner.hello_ack, runner.report_ack, runner.steer_seen = 100.0, {}, {"s": 100.0}
    assert runner._steer_check("s", 1, 170.0)
    runner.hello_ack = 180.0        # acknowledged later, SENT after the 75 s had run out
    assert not runner._steer_check("s", 1, 181.0), "the interval [175, 180] was stale"


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
    """J, K: a hub downgraded with a steer unsettled. The node sends N's end as it does to a hub that does not name
    `steer` (`_for_hub`: no extension); main's own code (exported from git) takes it behind its real report route,
    and its records are what main makes of any end: the message, the session, the parent's result and the parent's
    turn text, with no steer anywhere. Back on this hub, the sweep settles S by the rule and leaves N as it was."""
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
    runner.hub_features, runner.features_known = {"answered", "restart"}, True     # what the older hub names
    event = runner._for_hub(runner._event(HostEvent(kid, "turn_ended", "1", ok=True, text="the child's answer",
                                                    answered=True, steer={"outcome": "in_turn", "why": "answered"})))
    assert "steer" not in event and event["text"] == "the child's answer"
    old = tmp_path / "main"
    old.mkdir()
    archive = subprocess.run(["git", "-C", str(root_dir), "archive", OLDER, "agent_comms"], capture_output=True,
                             check=True).stdout
    subprocess.run(["tar", "-x", "-C", str(old)], input=archive, check=True)
    assert not (old / "agent_comms" / "hosted_steer.py").exists(), "main's code, without this change"
    code = ("import json, sys\n"
            "from pathlib import Path\n"
            "from fastapi.testclient import TestClient\n"
            "from agent_comms.api import create_app\n"
            "from agent_comms.config import Settings\n"
            "from agent_comms.core import Board\n"
            "from agent_comms import hosted, hosted_results\n"
            "s = Settings(db_path=Path(sys.argv[1]), agents_path=Path(sys.argv[2]))\n"
            "s.dispatch = json.loads(sys.argv[3])\n"
            "b = Board(s)\n"
            "c = TestClient(create_app(b), base_url='http://127.0.0.1:8787',\n"
            "               headers={'Authorization': 'Bearer ' + sys.argv[4]})\n"
            "r = c.post('/api/nodes/sessions/' + sys.argv[5], json={'events': [json.loads(sys.argv[6])]})\n"
            "assert r.status_code == 200, r.text\n"
            "x = [x for x in hosted_results._results(b.conn, sys.argv[7]) if x.get('message') == 1]\n"
            "print(json.dumps({'text': hosted_results.render(x, token='0123456789abcdef')}))\n")
    r = subprocess.run([sys.executable, "-c", code, str(env.settings.db_path), str(env.settings.agents_path),
                        json.dumps(env.board.s.dispatch), env.tokens["laptop"], kid, json.dumps(event), sid],
                       cwd=str(old), capture_output=True, text=True,
                       env={"PYTHONPATH": str(old), "PATH": "/usr/bin:/bin", "HOME": str(tmp_path)}, timeout=120)
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
