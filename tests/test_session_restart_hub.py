"""Restarting a running turn, hub and node (DESIGN_NOTES "Restarting a running turn"): the human's sends with a mode
through the real routes and `board session send --mode`, the node's protocol and its races, and a parent agent's
`board_session_send(mode=...)` with its budget and its results. `steer` is refused for every session (R6). Fake
processes speaking what Claude Code 2.1.330 and codex-cli 0.171.0 were measured to write (test_session_restart),
never a real agent CLI."""

import httpx
import pytest

from agent_comms import cli, hosted, hosted_results, hosted_spawn as hs, nodes, node_sessions
from agent_comms.core import Conflict
from agent_comms.session_host import INTERRUPTED

import test_providers_codex as cx
from test_hosted_sessions import NoReports, clienv, make_node, msg, show, start, tick, written  # noqa: F401
from test_hosted_spawn import (_no_codex_home_of_the_users, bound, budget, grant, results_of, spawn,  # noqa: F401
                               with_spawn)
from test_hosted_tree import as_started, began, proc_of, senv, stored  # noqa: F401
from test_session_host import result
from test_session_restart import CAPS, aborted, receipt

STEER_NO = "mid-turn steering is not available yet; use restart to redirect a running turn, or queue"


# ------------------------------------------------------------ helpers


def begin(env, sid, node=None) -> None:
    """A Claude session's process begins the turn it was given, and says what it can (Claude Code 2.1.330's
    `init`: its mode, its tools, its capabilities)."""
    call, proc = proc_of(env, sid, node)
    proc.emit(began(sid, *as_started(call["argv"])) | {"capabilities": CAPS})
    tick(env, node)


def a_turn(env, text="first") -> tuple[str, object]:
    """A Claude root session whose first turn is in flight on its node and has begun."""
    sid = start(env, text)["session_id"]
    tick(env)
    begin(env, sid)
    assert show(env, sid)["in_flight"] == 1
    return sid, proc_of(env, sid)[1]


def cdx(env, node=None):
    """The node's latest Codex process."""
    node = node or env.node
    i = max(i for i, c in enumerate(node.session_spawner.calls) if c["argv"][:2] == ["codex", "app-server"])
    return node.session_spawner.children[i]


def c_turn(env, text="first", **kw) -> tuple[str, object]:
    """A Codex root session whose first turn is in flight on its node, named and begun."""
    sid = start(env, text, agent="codex", **kw)["session_id"]
    tick(env)
    child = cdx(env)
    child.emit(cx.reply(1, cx.INIT_RESULT))
    tick(env)
    child.emit(cx.reply(2, cx.THREAD_RESULT))
    tick(env)
    child.emit(cx.reply(3, cx.TURN_REPLY), cx.TURN_STARTED)
    tick(env)
    assert show(env, sid)["in_flight"] == 1 and stored(env, sid)["modes"] == ["restart"]
    return sid, child


def interrupts(proc) -> list[dict]:
    return [x for x in written(proc) if x.get("type") == "control_request"
            and (x.get("request") or {}).get("subtype") == "interrupt"]


def interrupted(env, proc, sid, ok=True, node=None) -> None:
    """Claude Code answers the interrupt written to it (a success receipt, or a refusal) and the turn aborts."""
    proc.emit(receipt(interrupts(proc)[-1]["request_id"], ok), aborted(sid))
    tick(env, node)


def users(proc) -> list[str]:
    return [x["message"]["content"] for x in written(proc) if x.get("type") == "user"]


def post_send(env, sid, text, expect=200, **body) -> dict:
    r = env.human.post(f"/api/hosted-sessions/{sid}/messages", json={"message": text, **body})
    assert r.status_code == expect, r.text
    return r.json()


def node_tree(env, tree: int) -> None:
    """The node's latest hello as a node of session protocol `tree` sends it."""
    with hosted.db.write_tx(env.board.conn) as c:
        info = nodes._get(c, nodes.NODE_PREFIX + "laptop")
        info["session_host"]["tree"] = tree
        nodes._put(c, nodes.NODE_PREFIX + "laptop", info, "laptop", env.clock())


def edit_msg(env, sid, n, **fields) -> None:
    with hosted.db.write_tx(env.board.conn) as c:
        m = hosted._get(c, hosted._msg_key(sid, n))
        hosted._put(c, hosted._msg_key(sid, n), m | fields, "hub", env.clock())


def listed(env, sid, tree=4) -> list[dict]:
    rows = env.laptop.get("/api/nodes/sessions", params={"tree": tree}).json()["sessions"]
    return next(x for x in rows if x["session_id"] == sid)["messages"]


def prune(env, sid, n) -> None:
    with hosted.db.write_tx(env.board.conn) as c:
        c.execute("DELETE FROM board_state WHERE key = ?", (hosted._msg_key(sid, n),))     # what the prune does


def child_in_turn(env, task="long task", agent="claude"):
    """A hosted Claude root with the spawn grant, and a child of it whose first task is in flight and has begun."""
    from test_hosted_spawn import a_root
    sid = a_root(env)
    grant(env, agents=("claude", "codex"))
    kid = spawn(env, bound(env, sid), task=task, agent=agent)["child"]["id"]
    tick(env)
    begin(env, kid)
    assert show(env, kid)["in_flight"] == 1
    return sid, kid, proc_of(env, kid)[1]



# ------------------------------------------------------------ the protocol


def test_the_node_says_what_each_session_can_do_now_and_the_hub_keeps_it(senv):
    sid = start(senv, "first")["session_id"]
    tick(senv)
    assert stored(senv, sid).get("modes") == [], "a Claude process says what it can only when its turn begins"
    begin(senv, sid)
    assert stored(senv, sid)["modes"] == ["restart"], "no adapter steers (R6)"
    cid, _ = c_turn(senv, project="/work/second")
    assert stored(senv, cid)["modes"] == ["restart"]
    assert "restart" in senv.laptop.get("/api/nodes/sessions", params={"tree": 4}).json()["features"]
    r = senv.laptop.post(f"/api/nodes/sessions/{sid}", json={"modes": ["restart", "fly"]})
    assert r.status_code == 400 and "modes must list" in r.text
    r = senv.laptop.post(f"/api/nodes/sessions/{sid}", json={"events": [{"kind": "steered", "message": 1}]})
    assert r.status_code == 422, "no steering event exists"
    r = senv.laptop.post(f"/api/nodes/sessions/{sid}", json={"events": [{"kind": "turn_ended", "message": 1,
                                                                          "restarted_by": 0}]})
    assert r.status_code == 400 and "restarted_by" in r.text


def test_queue_is_the_default_and_answers_and_lists_exactly_as_before(senv):
    sid, _ = a_turn(senv)
    out = post_send(senv, sid, "next")
    assert set(out) == {"session", "message"} and set(out["message"]) == {"session_id", *hosted._MSG_FIELDS}
    assert listed(senv, sid)[1] == {"n": 2, "kind": "user", "state": "queued", "text": "next"}
    assert not {"mode", "into", "request_id", "sent_by"} & set(hosted._get(senv.board.conn, hosted._msg_key(sid, 2)))
    assert "receipts" not in stored(senv, sid)


def test_a_node_rolled_back_to_protocol_3_with_a_restart_pending_runs_it_once_as_ordinary_work(senv, monkeypatch):
    """R4-01: the older node is never listed the mode; the message is the session's next queued one, so its
    watermark is the ordinary one: nothing is reported lost but the turn its process had, and the message runs once."""
    sid, proc = a_turn(senv)
    post_send(senv, sid, "start over", mode="restart")
    assert listed(senv, sid, 3)[1] == {"n": 2, "kind": "user", "state": "queued", "text": "start over"}
    monkeypatch.setattr(node_sessions, "SESSION_PROTOCOL", 3)
    logs = senv.tmp / "laptop-logs"
    old = make_node(senv, config=senv.node.config, log_dir=logs)       # the node rolled back, its process restarted
    old.sessions.recover()
    old.hello()
    for _ in range(3):
        tick(senv, old)
    m1, m2 = msg(senv, sid, 1), msg(senv, sid, 2)
    assert m1["state"] == "failed" and m1["detail"].startswith(node_sessions.LOST), "the turn its old process had"
    assert m2["state"] == "delivered" and "restarted_by" not in m1
    assert sum(users(c).count("start over") for c in old.session_spawner.children) == 1

# ------------------------------------------------------------ the human's modes


def test_steer_is_refused_for_every_session_with_one_reason(senv):
    """R6: nothing is enqueued for it."""
    sid, _ = a_turn(senv)
    cid, _ = c_turn(senv, project="/work/second")
    for s in (sid, cid):
        r = post_send(senv, s, "x", expect=409, mode="steer")
        assert r["message"] == f"cannot steer into session {s}: {STEER_NO}"
        assert show(senv, s)["queued"] == 0


def test_auto_starts_an_idle_session_and_queues_behind_a_turn_in_flight_never_restarting(senv):
    sid, child = c_turn(senv)
    out = post_send(senv, sid, "behind it", mode="auto")
    assert (out["mode"], out["why"]) == ("queue", "queued behind the turn in flight (message 1)")
    assert "mode" not in out["message"]
    tick(senv)
    assert "turn/interrupt" not in [m.get("method") for m in written(child)], "auto never restarts"
    cid = start(senv, "x", project="/work/second")["session_id"]
    tick(senv)
    begin(senv, cid)
    proc = proc_of(senv, cid)[1]
    proc.emit(result(cid, text="done"))
    tick(senv)
    out = post_send(senv, cid, "now", mode="auto")
    assert (out["mode"], out["why"]) == ("queue", "the session was idle: it starts at once")
    tick(senv)
    assert users(proc)[-1] == "now"


def test_a_restart_is_refused_with_the_reason(senv):
    sid, proc = a_turn(senv)
    proc.emit(result(sid, text="done"))
    tick(senv)
    r = post_send(senv, sid, "x", expect=409, mode="restart")
    assert r["message"] == f"cannot restart the turn in flight of session {sid}: no turn is in flight"
    post_send(senv, sid, "the next turn")
    tick(senv)
    begin(senv, sid)
    node_tree(senv, 3)                          # an un-upgraded node
    r = post_send(senv, sid, "x", expect=409, mode="restart")
    assert "its node laptop speaks session protocol 3; restarting needs 4" in r["message"]
    node_tree(senv, 4)
    with hosted.db.write_tx(senv.board.conn) as c:
        r_ = hosted._session(c, sid)
        hosted._put(c, hosted.SESSION_PREFIX + sid, r_ | {"modes": []}, "laptop", senv.clock())
    r = post_send(senv, sid, "x", expect=409, mode="restart")
    assert "this session's agent CLI cannot restart a running turn (its node says so)" in r["message"]
    post_send(senv, sid, "x", expect=422, mode="sideways")


def test_a_wake_or_a_result_delivery_turn_is_finished_untouched(senv):
    sid, _ = c_turn(senv)
    for kind, words in (("wake", "a wake turn"), ("child", "a result-delivery turn")):
        edit_msg(senv, sid, 1, kind=kind)
        r = post_send(senv, sid, "x", expect=409, mode="restart")
        assert f"the turn in flight (message 1) is {words}" in r["message"] and "finishes untouched" in r["message"]
    edit_msg(senv, sid, 1, kind="user", cancel=senv.clock())
    r = post_send(senv, sid, "x", expect=409, mode="restart")
    assert "is being cancelled" in r["message"]


def test_a_restart_is_refused_while_other_messages_are_queued(senv):
    """R7: a restart never runs out of order; a second restart while one waits is refused the same way."""
    sid, proc = a_turn(senv)
    post_send(senv, sid, "older")
    r = post_send(senv, sid, "x", expect=409, mode="restart")
    assert r["message"] == (f"cannot restart the turn in flight of session {sid}: 1 message(s) are already queued "
                            "for this session; a restart would have to jump them: cancel them or send with queue")
    cid, child = c_turn(senv, project="/work/second")
    post_send(senv, cid, "first restart", mode="restart")
    r = post_send(senv, cid, "x", expect=409, mode="restart")
    assert "1 message(s) are already queued" in r["message"]

def test_a_restart_interrupts_the_turn_and_runs_the_message_next_as_an_ordinary_turn(senv):
    """R2 and R7: interrupt; the old turn's end (interrupted only because the provider said so); then the message, the
    session's next queued one, by the ordinary path."""
    sid, proc = a_turn(senv)
    out = post_send(senv, sid, "start over", mode="restart")
    assert (out["mode"], out["why"], out["message"]["into"]) == ("restart", None, 1)
    assert listed(senv, sid)[1] == {"n": 2, "kind": "user", "state": "queued", "mode": "restart", "into": 1,
                                    "text": "start over"}, "listed as any next message, with what it is for"
    tick(senv)
    assert len(interrupts(proc)) == 1 and users(proc) == ["first"]
    interrupted(senv, proc, sid)
    m1, m2 = msg(senv, sid, 1), msg(senv, sid, 2)
    assert (m1["state"], m1["detail"], m1["restarted_by"]) == ("failed", INTERRUPTED.format(n="2"), 2)
    assert (m2["state"], m2["replaced"]) == ("delivered", 1) and users(proc)[-1] == "start over"
    assert senv.node.sessions.handed[sid] == 2, "the ordinary watermark"
    post_send(senv, sid, "after it")
    begin(senv, sid)
    proc.emit(result(sid, text="fresh"))
    tick(senv)
    view = show(senv, sid)
    assert (msg(senv, sid, 2)["state"], view["reply_for"], view["reply"]) == ("done", 2, "fresh")
    assert users(proc)[-1] == "after it"

def test_a_codex_restart_through_the_hub(senv):
    sid, child = c_turn(senv)
    post_send(senv, sid, "start over", mode="restart")
    tick(senv)
    [stop] = [m for m in written(child) if m.get("method") == "turn/interrupt"]
    assert stop["params"] == {"threadId": cx.THREAD, "turnId": cx.TURN}
    child.emit(cx.reply(stop["id"], {}), cx.completed(status="interrupted"))
    tick(senv)
    assert msg(senv, sid, 1)["restarted_by"] == 2 and msg(senv, sid, 2)["replaced"] == 1
    assert [m["params"]["input"][0]["text"] for m in written(child) if m.get("method") == "turn/start"][-1] \
        == "start over"


def test_a_restarted_turn_that_finished_first_or_refused_the_interrupt_keeps_its_outcome_and_no_link(senv):
    """AC-12 and R3-03: the restart then simply runs next."""
    sid, proc = a_turn(senv)
    post_send(senv, sid, "start over", mode="restart")
    tick(senv)
    proc.emit(result(sid, text="finished by itself"))
    tick(senv)
    m1, m2 = msg(senv, sid, 1), msg(senv, sid, 2)
    assert m1["state"] == "done" and "restarted_by" not in m1 and "replaced" not in m2
    assert m2["state"] == "delivered"
    begin(senv, sid)
    post_send(senv, sid, "again", mode="restart")
    tick(senv)
    interrupted(senv, proc, sid, ok=False)      # the CLI refused the interrupt; the turn then aborted by itself
    m2, m3 = msg(senv, sid, 2), msg(senv, sid, 3)
    assert m2["state"] == "failed" and "restarted_by" not in m2 and m2["detail"] != INTERRUPTED.format(n="3")
    assert m3["state"] == "delivered" and "replaced" not in m3


# ------------------------------------------------------------ request ids


def test_a_retried_send_finds_its_receipt_for_its_whole_window_after_the_message_is_pruned(senv):
    """AC-10: the receipt outlives the message's own record (pruned a day after it settled), and none is evicted
    inside its seven days, however many there are."""
    sid, proc = a_turn(senv)
    first = post_send(senv, sid, "x", request_id="rq-1")
    again = post_send(senv, sid, "x", request_id="rq-1")
    assert again["created"] is False and again["message"]["n"] == first["message"]["n"] == 2
    assert show(senv, sid)["queued"] == 1, "nothing new was sent"
    post_send(senv, sid, "x", expect=400, request_id="no spaces")
    prune(senv, sid, 2)
    for i in range(600):        # more than any cap would keep, each message pruned once settled
        post_send(senv, sid, f"m{i}", request_id=f"id-{i}")
        prune(senv, sid, 3 + i)
        with hosted.db.write_tx(senv.board.conn) as c:
            r_ = hosted._session(c, sid)
            hosted._put(c, hosted.SESSION_PREFIX + sid, r_ | {"queued": 0}, "hub", senv.clock())
    senv.clock.advance(hosted.RECEIPT_SECONDS - 10)
    for rid, n in (("rq-1", 2), ("id-0", 3), ("id-599", 602)):
        again = post_send(senv, sid, "x", request_id=rid)
        assert (again["created"], again["message"]["n"], again["message"]["state"]) == (False, n, "settled"), rid
    senv.clock.advance(11)
    assert post_send(senv, sid, "x", request_id="rq-1")["created"] is True, "after its window it is a new send"


def test_a_message_row_answers_a_retry_whatever_became_of_its_receipt(senv):
    sid, proc = a_turn(senv)
    post_send(senv, sid, "m0", request_id="id-0")
    with hosted.db.write_tx(senv.board.conn) as c:
        r_ = hosted._session(c, sid)
        hosted._put(c, hosted.SESSION_PREFIX + sid, r_ | {"receipts": {}}, "hub", senv.clock())
    again = post_send(senv, sid, "m0", request_id="id-0")
    assert again["created"] is False and again["message"]["n"] == 2


def test_a_wait_on_a_receipt_only_retry_says_its_reply_expired(clienv, capsys):
    """R2-06."""
    sid, proc = a_turn(clienv)
    cli.main(["session", "send", sid, "x", "--request-id", "w1"])
    capsys.readouterr()
    prune(clienv, sid, 2)
    cli.main(["session", "send", sid, "x", "--request-id", "w1", "--wait"])
    err = capsys.readouterr().err
    assert "already sent for this request id (message 2: settled)" in err and "its reply is no longer kept" in err


# ------------------------------------------------------------ the CLI


def test_the_cli_sends_with_a_mode_and_says_what_happened(clienv, capsys):
    sid, _ = a_turn(clienv)
    cli.main(["session", "send", sid, "start over", "--mode", "restart"])
    assert "message 2 interrupts the turn in flight (message 1) and runs next instead" in capsys.readouterr().out
    cli.main(["session", "send", sid, "behind", "--mode", "auto", "--request-id", "r1"])
    assert "message 3: queued behind the turn in flight (message 1)" in capsys.readouterr().out
    cli.main(["session", "send", sid, "behind", "--mode", "auto", "--request-id", "r1"])
    assert "already sent for this request id (message 3: queued); nothing new was sent" in capsys.readouterr().out
    with pytest.raises(SystemExit, match=STEER_NO.replace("(", "\\(")):
        cli.main(["session", "send", sid, "x", "--mode", "steer"])
    cli.main(["session", "show", sid])
    assert "message 2 [queued] (restart of message 1)" in capsys.readouterr().out


def test_show_lists_both_links_of_a_restart():
    """AC-12: a message's own mode does not hide a link it has."""
    line = cli._fmt_mode({"mode": "restart", "into": 1, "replaced": 1, "restarted_by": 5})
    assert line == (" (restart of message 1) (it interrupted message 1) (interrupted: message 5 replaced it)")


def test_show_says_in_words_that_a_parents_restarted_turn_was_kept():
    """Review follow-up (#103): `kept` has a plain label, as the other result states do."""
    [line] = cli._fmt_result({"n": 3, "state": "kept", "child": "c" * 8, "name": "general-1", "message": 1,
                              "outcome": "interrupted", "detail": "interrupted by a restart: message 2 replaced it"})
    assert line.startswith("result 3 [kept, not sent as a turn: its parent restarted the turn itself] general-1 ")
    assert ": interrupted (interrupted by a restart: message 2 replaced it)" in line


# ------------------------------------------------------------ the node: its record, its reports, its races


def test_a_restart_pending_when_the_node_restarted_runs_once_on_the_next_node(senv):
    """AC-04's case, without a record of its own: the message is ordinary queued work behind the lost turn."""
    sid, proc = a_turn(senv)
    post_send(senv, sid, "start over", mode="restart")
    tick(senv)
    assert len(interrupts(proc)) == 1
    logs = senv.tmp / "laptop-logs"
    node = make_node(senv, config=senv.node.config, log_dir=logs)      # the node process is gone; another starts
    node.sessions.recover()
    node.hello()
    for _ in range(3):
        tick(senv, node)
    assert msg(senv, sid, 1)["state"] == "failed" and msg(senv, sid, 2)["state"] == "delivered"
    assert sum(users(c).count("start over") for c in node.session_spawner.children) == 1

def test_a_fetch_between_the_old_turns_end_and_the_replacements_write_reports_nothing_lost(senv):
    """R3-01, for a restart: the message is ordinary queued work there, handed over once."""
    sid, proc = a_turn(senv)
    post_send(senv, sid, "start over", mode="restart")
    tick(senv)
    host = senv.node.sessions.host
    host.may_start = lambda: False          # nothing new is written for now
    interrupted(senv, proc, sid)
    for _ in range(2):
        tick(senv)                          # fetches with the message held, unwritten
    assert msg(senv, sid, 2)["state"] == "queued" and msg(senv, sid, 1)["restarted_by"] == 2
    host.may_start = None
    tick(senv)
    assert msg(senv, sid, 2)["state"] == "delivered" and users(proc).count("start over") == 1


def test_a_restart_waits_for_codex_to_name_the_turn_and_nothing_is_reported_meanwhile(senv):
    sid = start(senv, "first", agent="codex")["session_id"]
    tick(senv)
    child = cdx(senv)
    child.emit(cx.reply(1, cx.INIT_RESULT))
    tick(senv)
    child.emit(cx.reply(2, cx.THREAD_RESULT))
    tick(senv)
    assert show(senv, sid)["in_flight"] == 1
    post_send(senv, sid, "start over", mode="restart")
    for _ in range(3):
        tick(senv)
    assert msg(senv, sid, 2)["state"] == "queued" and "turn/interrupt" not in [m.get("method") for m in written(child)]
    child.emit(cx.reply(3, cx.TURN_REPLY), cx.TURN_STARTED)
    tick(senv)
    assert [m["method"] for m in written(child) if m.get("method") == "turn/interrupt"] == ["turn/interrupt"]

def test_a_restart_cancelled_before_its_interrupt_was_written_never_interrupts(senv):
    """R2: its starter's cancel reaches the host too: the interrupt is no longer asked for."""
    sid = start(senv, "first", agent="codex")["session_id"]
    tick(senv)
    child = cdx(senv)
    child.emit(cx.reply(1, cx.INIT_RESULT))
    tick(senv)
    child.emit(cx.reply(2, cx.THREAD_RESULT))
    tick(senv)
    post_send(senv, sid, "start over", mode="restart")
    tick(senv)                              # asked for; Codex has not named the turn, so nothing is written yet
    edit_msg(senv, sid, 2, cancel=senv.clock())
    tick(senv)
    child.emit(cx.reply(3, cx.TURN_REPLY), cx.TURN_STARTED)
    tick(senv)
    assert "turn/interrupt" not in [m.get("method") for m in written(child)]
    assert msg(senv, sid, 2)["state"] == "failed" and msg(senv, sid, 1)["state"] == "delivered"


def test_a_report_an_older_hub_refuses_is_kept_and_sent_again_in_order(senv):
    """AC-05: no batch of events is dropped for a field a rolled-back hub refuses; a session's events never overtake
    one another."""
    runner = senv.node.sessions
    sid, proc = a_turn(senv)
    post_send(senv, sid, "start over", mode="restart")
    tick(senv)
    posts, refuse = [], {"on": True}
    real = runner.client

    class RolledBack:
        def request(self, method, path, **kw):
            if method == "POST":
                posts.append(kw.get("json"))
                if refuse["on"] and "modes" in (kw.get("json") or {}):
                    return httpx.Response(422, json={"message": "extra field"},
                                          request=httpx.Request(method, "http://hub" + path))
            return real.request(method, path, **kw)
    runner.client = RolledBack()
    interrupted(senv, proc, sid)            # the turn's end, with `restarted_by` and the status: refused once
    assert any(e.get("restarted_by") == 2 for _, e in runner.pending), "kept, not dropped"
    refuse["on"] = False
    senv.clock.advance(node_sessions.RETRY_SECONDS + 1)
    tick(senv)
    tick(senv)
    assert msg(senv, sid, 1)["restarted_by"] == 2 and msg(senv, sid, 2)["state"] == "delivered"
    # Order: with what the hub takes unknown, nothing of the session goes past the event it holds.
    end = {"kind": "turn_ended", "message": 9, "ok": False, "delivered": True, "detail": "", "denials": [],
           "meta": {}, "text": "", "answered": True, "restarted_by": 10}
    runner.hub_features, runner.features_known = set(), False
    runner.pending = [(sid, end), (sid, {"kind": "delivered", "message": 10})]
    posts.clear()
    assert runner._flush()
    assert not [e for body in posts for e in body["events"]], "the delivery waits behind the turn's end"
    # A hub that said it takes no restarts: both go, in order, the end without the field.
    runner.hub_features, runner.features_known = {"answered"}, True
    assert runner._flush()
    sent = [e for body in posts for e in body["events"]]
    assert [e["kind"] for e in sent] == ["turn_ended", "delivered"] and "restarted_by" not in sent[0]


def test_a_pass_stopped_by_a_later_sessions_report_keeps_only_what_was_not_sent(senv):
    """Review follow-up (#103): the events a pass already posted for an earlier session are dropped even when a later
    session's report stops the pass (the hub unreachable), so they are never posted again."""
    runner = senv.node.sessions
    a, b = "a" * 8, "b" * 8
    runner.pending = [(a, {"kind": "delivered", "message": 1}), (b, {"kind": "delivered", "message": 1})]
    posts = []

    class Down:
        def request(self, method, path, **kw):
            posts.append(path)
            if path.endswith(b):
                raise httpx.ConnectError("the hub went away")
            return httpx.Response(200, json={}, request=httpx.Request(method, "http://hub" + path))
    runner.client = Down()
    assert runner._flush() is False
    assert runner.pending == [(b, {"kind": "delivered", "message": 1})], "a's event was sent: not kept to go again"
    runner.client = type("Up", (), {"request": lambda self, method, path, **kw: (posts.append(path), httpx.Response(
        200, json={}, request=httpx.Request(method, "http://hub" + path)))[1]})()
    posts.clear()
    assert runner._flush() is True and runner.pending == []
    assert posts == [f"/api/nodes/sessions/{b}"], "only b's report goes again"


def test_a_hub_downgraded_with_a_restart_pending_records_one_turn_each(senv, monkeypatch):
    """R3-02, for a restart: a hub from before restarts gets the interrupted turn's end without `restarted_by`: that
    turn failed (its detail says it was interrupted), the restart's message is an ordinary turn of its own, with its
    own reply."""
    sid, proc = a_turn(senv)
    post_send(senv, sid, "start over", mode="restart")
    tick(senv)
    monkeypatch.setattr(hosted, "NODE_REPORT_FEATURES", ("answered",))      # the hub, rolled back
    tick(senv)
    interrupted(senv, proc, sid)
    begin(senv, sid)
    proc.emit(result(sid, text="fresh"))
    tick(senv)
    m1, m2, view = msg(senv, sid, 1), msg(senv, sid, 2), show(senv, sid)
    assert (m1["state"], m1["detail"], m1.get("restarted_by")) == ("failed", INTERRUPTED.format(n="2"), None)
    assert m2["state"] == "done" and (view["turns"], view["reply_for"], view["reply"]) == (2, 2, "fresh")


def test_a_parents_restart_on_a_downgraded_hub_is_delivered_to_the_parent_as_an_ordinary_result(senv, monkeypatch):
    """R4-02, documented degradation: without `restarted_by` an older hub cannot know the parent restarted its own
    task, so the interrupted turn's result reaches the parent as a result turn, not `kept`."""
    sid, kid, proc = child_in_turn(senv)
    hs.send(senv.board, senv.p["claude"], bound(senv, sid), kid, message="do this instead", mode="restart")
    tick(senv)
    monkeypatch.setattr(hosted, "NODE_REPORT_FEATURES", ("answered",))      # the hub, rolled back
    tick(senv)
    interrupted(senv, proc, kid)
    [x] = [x for x in results_of(senv, sid) if x["child"] == kid and x["message"] == 1]
    assert x["state"] != "kept" and x["outcome"] == "interrupted" and "restarted_by" not in msg(senv, kid, 1)


def test_a_node_that_goes_silent_with_a_restart_pending_runs_the_restart_once_when_it_is_back(senv):
    """AC-08, for a restart: the hub settles the old turn as lost; the node, back, writes the message once."""
    sid, proc = a_turn(senv)
    post_send(senv, sid, "start over", mode="restart")
    tick(senv)
    senv.clock.advance(nodes.NODE_LOST_SECONDS + 5)
    assert msg(senv, sid, 1)["state"] == "failed" and msg(senv, sid, 2)["state"] == "queued"
    interrupted(senv, proc, sid)
    tick(senv)
    assert msg(senv, sid, 2)["state"] == "delivered" and users(proc).count("start over") == 1
    assert msg(senv, sid, 1)["detail"].startswith("node_lost"), "the late end changes nothing settled"


# ------------------------------------------------------------ the checkout


def test_a_parents_restart_meets_the_checkout_exactly_as_an_ordinary_queued_task(senv, monkeypatch):
    """AC-09 / R7: a dispatched run wanting the checkout while a restart is pending: the restart's message is listed,
    reserved and held as any queued task behind a running turn is (main's rules), and only its interrupt is extra."""
    sid, kid, proc = child_in_turn(senv)
    other = spawn(senv, bound(senv, sid), task="other task", agent="claude")["child"]["id"]
    tick(senv)
    begin(senv, other)
    hs.send(senv.board, senv.p["claude"], bound(senv, sid), kid, message="do this instead", mode="restart")
    hs.send(senv.board, senv.p["claude"], bound(senv, sid), other, message="do this instead")
    monkeypatch.setattr(hosted, "_checkout_busy", lambda board, r, runs=None: True)
    a, b = ([m for m in listed(senv, x) if m["n"] == 2][0] for x in (kid, other))
    assert {k: v for k, v in a.items() if k not in ("mode", "into")} == b == {
        "n": 2, "kind": "task", "state": "queued", "held": "checkout"}, "held, as any queued task"
    monkeypatch.undo()
    a, b = ([m for m in listed(senv, x) if m["n"] == 2][0] for x in (kid, other))
    assert "do this instead" in a["text"] and "do this instead" in b["text"], "reserved, as any queued task"

# ------------------------------------------------------------ a parent agent's sends


def test_a_parents_restart_runs_in_the_ordinary_frame_and_keeps_the_interrupted_result_even_with_a_full_outbox(
        senv, monkeypatch):
    """R3 and R2-05: `kept` before any limit of the automatic delivery; a human's restart still tells the parent."""
    monkeypatch.setattr(hosted_results, "MAX_WAITING", 0)        # the parent's outbox counts as full
    sid, kid, proc = child_in_turn(senv)
    turns_before = len(budget(senv)["tasks"])
    out = hs.send(senv.board, senv.p["claude"], bound(senv, sid), kid, message="do this instead", mode="restart")
    assert (out["mode"], out["created"], out["message"]["into"]) == ("restart", True, 1)
    assert len(budget(senv)["tasks"]) == turns_before + 1, "a restart is a child turn of the budget"
    tick(senv)
    interrupted(senv, proc, kid)
    assert users(proc)[-1].startswith("[agent-comms] A follow-up message from"), "an ordinary follow-up turn"
    [x] = [x for x in results_of(senv, sid) if x["child"] == kid and x["message"] == 1]
    assert (x["outcome"], x["state"]) == ("interrupted", "kept"), "kept, not undelivered for the full outbox"
    assert hosted_results.claim(senv.board, sid, "claude", child=kid, message=1)["outcome"] == "interrupted", \
        "a call waiting for that turn takes it"
    monkeypatch.setattr(hosted_results, "MAX_WAITING", 50)
    begin(senv, kid)
    proc.emit(result(kid, text="done instead"))
    tick(senv)
    hs.send(senv.board, senv.p["claude"], bound(senv, sid), kid, message="third")
    tick(senv)
    tick(senv)
    begin(senv, kid)
    n = show(senv, kid)["in_flight"]
    post_send(senv, kid, "the human says stop", mode="restart")
    tick(senv)
    interrupted(senv, proc, kid)
    y = next(x for x in results_of(senv, sid) if x["child"] == kid and x["message"] == n)
    assert y["outcome"] == "interrupted" and y["state"] != "kept", "the human's restart tells the parent"


def test_a_wrong_restart_link_from_a_faulty_node_never_keeps_a_parents_result(senv):
    """Review follow-up (#103): `restarted_by` is kept, and a result `kept`, only when the message it names is a
    restart of this very turn (`mode` restart, `into` it), as the hub already checked for `replaced`."""
    sid, kid, proc = child_in_turn(senv)
    other = spawn(senv, bound(senv, sid), task="other task", agent="claude")["child"]["id"]
    tick(senv)
    begin(senv, other)
    for x in (kid, other):
        hs.send(senv.board, senv.p["claude"], bound(senv, sid), x, message="next, queued")     # an ORDINARY task
    end = {"kind": "turn_ended", "message": 1, "ok": False, "delivered": True, "detail": "aborted", "denials": [],
           "meta": {}, "text": "", "answered": True}
    # The node names an ordinary queued task as the turn's restart: not stored.
    r = senv.laptop.post(f"/api/nodes/sessions/{kid}", json={"events": [end | {"restarted_by": 2}]})
    assert r.status_code == 200, r.text
    assert "restarted_by" not in msg(senv, kid, 1) and "replaced" not in msg(senv, kid, 2)
    # A link already on the record (however it got there) that names no restart of the turn keeps nothing either.
    edit_msg(senv, other, 1, restarted_by=2)
    r = senv.laptop.post(f"/api/nodes/sessions/{other}", json={"events": [end]})
    assert r.status_code == 200, r.text
    for x in (kid, other):
        [res] = [y for y in results_of(senv, sid) if y["child"] == x and y["message"] == 1]
        assert res["state"] != "kept", x


def test_a_restart_named_on_a_turn_that_completed_is_delivered_as_any_result(senv):
    """Sol R5: `kept` needs an interrupted outcome too. A node that names a valid restart of the turn while reporting
    the turn a success: the result is delivered, not kept."""
    sid, kid, proc = child_in_turn(senv)
    hs.send(senv.board, senv.p["claude"], bound(senv, sid), kid, message="do this instead", mode="restart")
    end = {"kind": "turn_ended", "message": 1, "ok": True, "delivered": True, "detail": "", "denials": [],
           "meta": {}, "text": "done anyway", "answered": True, "restarted_by": 2}
    r = senv.laptop.post(f"/api/nodes/sessions/{kid}", json={"events": [end]})
    assert r.status_code == 200, r.text
    [x] = [x for x in results_of(senv, sid) if x["child"] == kid and x["message"] == 1]
    assert (x["outcome"], x["state"]) == ("completed", "pending"), "an ordinary result, waiting for its parent"


def test_a_parents_restart_whose_interrupt_was_refused_still_tells_the_parent(senv):
    """R3-03: the turn aborted by itself after the CLI refused the interrupt: its result is the parent's to hear."""
    sid, kid, proc = child_in_turn(senv)
    hs.send(senv.board, senv.p["claude"], bound(senv, sid), kid, message="do this instead", mode="restart")
    tick(senv)
    interrupted(senv, proc, kid, ok=False)
    [x] = [x for x in results_of(senv, sid) if x["child"] == kid and x["message"] == 1]
    assert x["outcome"] == "failed" and x["state"] != "kept"


def test_a_parent_may_restart_only_its_own_task_steers_never_and_auto_says_what_it_did(senv):
    sid, kid, proc = child_in_turn(senv)
    with pytest.raises(Conflict, match=STEER_NO):
        hs.send(senv.board, senv.p["claude"], bound(senv, sid), kid, message="x", mode="steer")
    proc.emit(result(kid, text="done"))
    tick(senv)
    assert senv.human.post(f"/api/hosted-sessions/{kid}/messages", json={"message": "the human's own"}).status_code \
        == 200
    tick(senv)
    begin(senv, kid)
    assert show(senv, kid)["in_flight"] == 2
    with pytest.raises(Conflict, match="the turn in flight \\(message 2\\) is not one you started"):
        hs.send(senv.board, senv.p["claude"], bound(senv, sid), kid, message="x", mode="restart")
    out = hs.send(senv.board, senv.p["claude"], bound(senv, sid), kid, message="x", mode="auto")
    assert (out["mode"], out["why"]) == ("queue", "queued behind the turn in flight (message 2)")


def test_a_default_follow_up_checks_the_grant_before_its_text_as_main_did(senv):
    """Pinned: without a request id, a send with no live grant and invalid text is refused for the grant."""
    from agent_comms.core import Forbidden, Invalid
    sid, kid, proc = child_in_turn(senv)
    with pytest.raises(Invalid):
        hs.send(senv.board, senv.p["claude"], bound(senv, sid), kid, message="   ")
    senv.clock.advance(2 * 3600)            # every grant has expired
    with pytest.raises(Forbidden, match="no live spawn grant"):
        hs.send(senv.board, senv.p["claude"], bound(senv, sid), kid, message="   ")


def test_a_parent_retry_charges_and_sends_nothing_again_even_at_the_limit(senv):
    """AC-10: the duplicate is found in the deciding transaction, before any refusal."""
    with_spawn(senv, max_turns_without_human=2)
    sid, kid, proc = child_in_turn(senv)           # the task: turn 1
    first = hs.send(senv.board, senv.p["claude"], bound(senv, sid), kid, message="a", mode="restart",
                    request_id="z")
    charged = len(budget(senv)["tasks"])
    real_resent = hs._resent
    hs._resent = lambda *a, **kw: None             # as a concurrent retry that passed the fast path before the first
    try:
        again = hs.send(senv.board, senv.p["claude"], bound(senv, sid), kid, message="a", mode="restart",
                        request_id="z")
    finally:
        hs._resent = real_resent
    assert again["created"] is False and again["message"]["n"] == first["message"]["n"]
    assert len(budget(senv)["tasks"]) == charged and show(senv, kid)["queued"] == 1


def test_a_parent_retry_returns_its_message_after_spawning_was_switched_off(senv):
    sid, kid, proc = child_in_turn(senv)
    first = hs.send(senv.board, senv.p["claude"], bound(senv, sid), kid, message="a", mode="restart",
                    request_id="w")
    with_spawn(senv, enabled=False)
    again = hs.send(senv.board, senv.p["claude"], bound(senv, sid), kid, message="a", mode="restart",
                    request_id="w")
    assert again["created"] is False and again["message"]["n"] == first["message"]["n"]
    with pytest.raises(Conflict, match="child sessions are off"):
        hs.send(senv.board, senv.p["claude"], bound(senv, sid), kid, message="b", request_id="other")
