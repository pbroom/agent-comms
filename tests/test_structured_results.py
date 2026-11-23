"""Structured results through the real hub (DESIGN_NOTES "Structured results"): asking (the human's routes and
`board session`, the spawn tools over the real MCP server), the frame, the verdict at settlement on every terminal
path and failing open, who sees what (the asker's one hub line and facts; nothing for anyone else), the reply byte for
byte as main's, retries resolved before schema refusals, the acknowledgement only when the call carried a schema,
rollback and re-upgrade, and the rendered bounds. Real hub routes and the node's SessionRunner with fake processes, as
in test_hosted_results and test_hosted_spawn. Never a real agent CLI."""

import json
import re
import subprocess
import sys
import time
from pathlib import Path

import anyio
import pytest

from agent_comms import hosted, hosted_results, hosted_spawn as hs, hosted_tree, output_schema as o
from agent_comms.core import Invalid
from agent_comms.mcp_server import build_mcp
from agent_comms.node_sessions import LOST
from agent_comms.providers import REPLY_MAX_BYTES, REPLY_TRUNCATED

from conftest import PROJECT
from test_hosted_results import TOKEN as RESULT_TOKEN, fetch, report, results, stored_result, turns_of
from test_hosted_sessions import clienv, msg, show, start, tick, written  # noqa: F401
from test_hosted_spawn import a_root, bound, grant, interactive, kid_turns, spawn
from test_hosted_tree import (_reports_follow_the_command_line, answer, begin, child, proc_of, root,  # noqa: F401
                              senv, stored)
from test_output_schema import GOOD, REVIEW

SCHEMA = json.dumps(REVIEW)
ASKED = re.compile(r"<<<output-schema ([0-9a-f]{16})>>>")


def token_of(text: str) -> str:
    return ASKED.search(text).group(1)


def reply_with(token: str, value=GOOD, prose="Reviewed: one bug.") -> str:
    return f"{prose}\n<<<result-data {token}>>>\n{json.dumps(value)}\n<<<end result-data {token}>>>"


def record(env, sid, n) -> dict:
    return json.loads(env.board.conn.execute("SELECT value FROM board_state WHERE key = ?",
                                             (hosted._msg_key(sid, n),)).fetchone()[0])


def get_message(env, sid, n) -> dict:
    r = env.human.get(f"/api/hosted-sessions/{sid}/messages/{n}")
    assert r.status_code == 200, r.text
    return r.json()


def human_turns(env, sid) -> list[str]:
    return turns_of(env, sid)


# ------------------------------------------------------------ the human root, child sessions OFF


@pytest.fixture
def offenv(tmp_path):
    from test_hosted_sessions import session_env
    return session_env(tmp_path)        # child sessions off: the hub's shipped setting


def test_a_human_root_asks_with_child_sessions_off_and_gets_a_verdict_on_the_reply_as_written(offenv):
    r = offenv.human.post("/api/hosted-sessions", json={"project": PROJECT, "message": "Review the diff",
                                                        "output_schema": SCHEMA})
    assert r.status_code == 200, r.text
    out = r.json()
    sid = out["session"]["session_id"]
    assert out["output_request"] == {"stored": True, "schema_sha": o.prepare_schema(SCHEMA)[2]}
    assert out["message"]["output"] == {"state": "asked", "schema_sha": out["output_request"]["schema_sha"]}
    tick(offenv)
    [written_text] = human_turns(offenv, sid)
    t = token_of(written_text)
    assert written_text.startswith("Review the diff\n\n[agent-comms] The human also asks for this turn's result")
    assert written_text.endswith(f"<<<output-schema {t}>>>\n{o.prepare_schema(SCHEMA)[1]}\n<<<end output-schema {t}>>>")
    text = reply_with(t)
    from test_hosted_sessions import run_turn
    run_turn(offenv, sid, text=text)
    m = get_message(offenv, sid, 1)
    assert m["session"]["reply"] == text, "the reply is main's, block included: never changed"
    assert m["message"]["output"]["state"] == "accepted" and m["message"]["output"]["reply"] == hosted.REPLY_WHOLE_BLOCK
    assert m["result_data"] == GOOD, "derived from the reply in this same answer"
    rec = record(offenv, sid, 1)["output"]
    assert rec["state"] == "accepted" and rec["data_sha"] == o.digest(GOOD) and "schema" not in rec
    assert "off by one" not in json.dumps(rec), "no copy of the data is stored"


def test_a_poll_for_message_n_after_n_plus_1_finished_is_a_historical_verdict_without_data(offenv):
    """R3-01 / R3-02: the reply the verdict was made from was replaced; the view says so and derives nothing."""
    from test_hosted_sessions import run_turn
    sid = offenv.human.post("/api/hosted-sessions", json={"project": PROJECT, "message": "one",
                                                          "output_schema": SCHEMA}).json()["session"]["session_id"]
    tick(offenv)
    run_turn(offenv, sid, text=reply_with(token_of(human_turns(offenv, sid)[0])))
    assert offenv.human.post(f"/api/hosted-sessions/{sid}/messages", json={"message": "two"}).status_code == 200
    tick(offenv)
    run_turn(offenv, sid, text="second reply")
    m = get_message(offenv, sid, 1)
    assert m["message"]["output"]["state"] == "accepted" and m["message"]["output"]["reply"] == hosted.REPLY_GONE
    assert "result_data" not in m
    s = show(offenv, sid)
    assert [x.get("output", {}).get("reply") for x in s["messages"]] == [hosted.REPLY_GONE, None]
    assert "result_data" not in s and "output" not in s["messages"][1], "a schema-free message has no new key"


def test_an_accepted_verdict_whose_reply_no_longer_matches_derives_nothing(offenv):
    """R3-02: a mismatch (here the stored digest changed under it) leaves the verdict and omits the field."""
    from test_hosted_sessions import run_turn
    sid = offenv.human.post("/api/hosted-sessions", json={"project": PROJECT, "message": "one",
                                                          "output_schema": SCHEMA}).json()["session"]["session_id"]
    tick(offenv)
    run_turn(offenv, sid, text=reply_with(token_of(human_turns(offenv, sid)[0])))
    m = record(offenv, sid, 1)
    m["output"]["data_sha"] = "0" * 64
    hosted._put(offenv.board.conn, hosted._msg_key(sid, 1), m, "test", offenv.board.now())
    got = get_message(offenv, sid, 1)
    assert got["message"]["output"]["state"] == "accepted" and "result_data" not in got


@pytest.mark.parametrize("reply,code", [
    ("no block at all", "no_block"),
    ("x\n<<<result-data {t}>>>\n{{\"verdict\": \"maybe\", \"findings\": []}}\n<<<end result-data {t}>>>", "shape"),
    ("x\n<<<result-data {t}>>>\n{{\"verdict\": \"ok\", \"verdict\": \"ok\"}}\n<<<end result-data {t}>>>",
     "not_json"),
    ("x\n<<<result-data {t}>>>\n[]\n<<<end result-data {t}>>>", "not_object"),
    ("x\n<<<result-data {t}>>>\n{{}}\n<<<end result-data {t}>>>\nP.S.", "form"),
])
def test_a_completed_turn_without_a_valid_block_is_missing_with_its_code_and_the_reply_untouched(offenv, reply,
                                                                                                    code):
    from test_hosted_sessions import run_turn
    sid = offenv.human.post("/api/hosted-sessions", json={"project": PROJECT, "message": "go",
                                                          "output_schema": SCHEMA}).json()["session"]["session_id"]
    tick(offenv)
    text = reply.format(t=token_of(human_turns(offenv, sid)[0]))
    run_turn(offenv, sid, text=text)
    m = get_message(offenv, sid, 1)
    assert m["session"]["reply"] == text and m["message"]["output"]["state"] == "missing"
    assert m["message"]["output"]["code"] == code and m["message"]["output"]["words"] == o.WORDS[code]
    assert "result_data" not in m


def test_a_truncated_reply_is_missing_truncated(offenv):
    from test_hosted_sessions import run_turn
    sid = offenv.human.post("/api/hosted-sessions", json={"project": PROJECT, "message": "go",
                                                          "output_schema": SCHEMA}).json()["session"]["session_id"]
    tick(offenv)
    t = token_of(human_turns(offenv, sid)[0])
    run_turn(offenv, sid, text="p" * REPLY_MAX_BYTES + reply_with(t))
    out = record(offenv, sid, 1)["output"]
    assert out["code"] == "truncated" and show(offenv, sid)["reply"].endswith(REPLY_TRUNCATED)


def test_the_turn_codes_follow_the_facts_the_outcome_reads():
    assert hosted._turn_code(hosted_results.CANCELLED, False) == "cancelled"
    assert hosted._turn_code("session ended: closed", True) == "session_ended"
    assert hosted._turn_code(LOST, True) == "lost"
    assert hosted._turn_code("anything", False) == "withdrawn"
    assert hosted._turn_code(hosted_results.INTERRUPTED + ": message 3 replaced it", True) == "interrupted"
    assert hosted._turn_code(hosted_results.TIMED_OUT + "30 minutes", True) == "timed_out"
    assert hosted._turn_code("the agent reported that the turn failed", True) == "failed"


def asked_root(env) -> tuple[str, str]:
    sid = env.human.post("/api/hosted-sessions", json={"project": PROJECT, "message": "go",
                                                       "output_schema": SCHEMA}).json()["session"]["session_id"]
    tick(env)
    return sid, token_of(human_turns(env, sid)[0])


def test_a_turn_that_did_not_complete_is_never_read(offenv):
    sid, t = asked_root(offenv)
    report(offenv, sid, {"kind": "delivered", "message": 1},
           {"kind": "turn_ended", "message": 1, "ok": False, "detail": "timed out after 30 minutes",
            "text": reply_with(t)})
    out = record(offenv, sid, 1)["output"]
    assert (out["state"], out["code"]) == ("missing", "timed_out")


def test_the_node_going_silent_closes_the_request_node_lost(offenv):
    sid, _ = asked_root(offenv)
    report(offenv, sid, {"kind": "delivered", "message": 1})
    offenv.clock.advance(10_000)
    show(offenv, sid)       # the settling read
    assert record(offenv, sid, 1)["output"]["code"] == "node_lost"


def test_closing_the_session_closes_the_request_session_ended(offenv):
    sid, _ = asked_root(offenv)
    report(offenv, sid, {"kind": "delivered", "message": 1})
    offenv.board.conn.execute("SELECT 1")
    with hosted.db.write_tx(offenv.board.conn) as c:
        r = hosted._session(c, sid)
        hosted._end(c, r, "closed", "hub", offenv.board.now())
        hosted._put(c, hosted.SESSION_PREFIX + sid, r, "hub", offenv.board.now())
    out = record(offenv, sid, 1)["output"]
    assert (out["state"], out["code"]) == ("missing", "session_ended")


def test_a_late_or_duplicate_report_changes_no_verdict(offenv):
    sid, t = asked_root(offenv)
    ev = {"kind": "turn_ended", "message": 1, "ok": True, "text": reply_with(t)}
    report(offenv, sid, {"kind": "delivered", "message": 1}, ev)
    first = record(offenv, sid, 1)["output"]
    report(offenv, sid, ev | {"ok": False, "text": "other"})
    assert record(offenv, sid, 1)["output"] == first


@pytest.mark.parametrize("stage", ["scan", "decode_block", "validate", "digest", "load_schema", "decode"])
def test_a_fault_in_a_childs_verdict_is_missing_internal_and_its_reply_outbox_and_restart_link_are_mains(
        senv, monkeypatch, stage):
    """R3-03 / R4-07: a CHILD's turn restarted by its parent, in ONE report with the fault in the middle (the
    interrupted turn's end, then the restart's own turn, whose verdict faults): the restart link both ways, the exact
    reply, the parent's outbox records (the interrupted turn's `kept`, the new one's with its exact text) and the
    outcomes are as on main; only the verdict says `internal`. `load_schema` faults the stored-schema decode alone."""
    sid, kid, t = asked_child(senv)
    report(senv, kid, {"kind": "turn_started", "message": 1}, modes=["restart"])
    out = hs.send(senv.board, senv.p["claude"], bound(senv, sid), kid, message="do this instead", mode="restart",
                  output_schema=SCHEMA)
    assert out["message"]["mode"] == "restart" and out["message"]["into"] == 1
    t2 = re.search(r"<<<output-schema ([0-9a-f]{16})>>>", record(senv, kid, 2)["text"]).group(1)
    sha = o.prepare_schema(SCHEMA)[2]

    def boom(*a, **kw):
        raise RuntimeError("feature fault")
    monkeypatch.setattr(o, stage, boom)
    text = reply_with(t2)
    report(senv, kid, {"kind": "turn_ended", "message": 1, "ok": False, "restarted_by": 2, "text": "partial",
                       "detail": hosted_results.INTERRUPTED + ": message 2 replaced it"},
           {"kind": "delivered", "message": 2}, {"kind": "turn_ended", "message": 2, "ok": True, "text": text})
    m1, m2, k = record(senv, kid, 1), record(senv, kid, 2), stored(senv, kid)
    assert (m1["state"], m1["restarted_by"], m2["state"], m2["replaced"]) == ("failed", 2, "done", 1)
    assert (m1["output"]["state"], m1["output"]["code"]) == ("missing", "interrupted"), "an unsuccessful end is " \
        "never read, so it cannot fault"
    assert (m2["output"]["state"], m2["output"]["code"]) == ("missing", "internal")
    assert (k["reply"], k["reply_for"], k["turns"]) == (text, 2, 2)
    first, second = stored_result(senv, sid, 1), stored_result(senv, sid, 2)
    assert (first["state"], first["outcome"], first["message"], first["text"]) == ("kept", "interrupted", 1,
                                                                                    "partial")
    assert first["output"] == {"state": "missing", "code": "interrupted", "reasons": None, "schema_sha": sha}
    assert (second["state"], second["outcome"], second["message"], second["text"]) == ("pending", "completed", 2,
                                                                                        text)
    assert second["output"] == {"state": "missing", "code": "internal", "reasons": None, "schema_sha": sha}


def adversarial_replies(token: str) -> list[tuple[str, str | None]]:
    """Near-cap replies of four kinds, with the verdict each must give (None: accepted)."""
    def block(payload: str) -> str:
        return f"prose\n<<<result-data {token}>>>\n{payload}\n<<<end result-data {token}>>>"
    # 1. Accepted: an 8,192-byte payload full of escapes (quotes, backslashes, \u escapes), every note within 500.
    note = '\\"\\u00e9\\\\' * 40           # each decodes to 3 characters; 120 in all
    one = '{"file": "src/pager.py", "line": 3, "note": "' + note + '"}'
    payload = '{"verdict": "changes", "findings": [' + ", ".join([one] * 30) + "]}"
    while len(payload.encode()) > o.BLOCK_MAX_BYTES:
        payload = payload[:payload.rfind(', {"file"')] + "]}"
    payload = payload[:-2] + " " * (o.BLOCK_MAX_BYTES - len(payload.encode())) + "]}"
    assert len(payload.encode()) == o.BLOCK_MAX_BYTES
    # 2. Shape: 1,000 empty findings and a wrong verdict: reasons capped while the walk runs.
    shape = '{"verdict": "maybe", "findings": [' + ",".join(["{}"] * 1000) + "]}"
    # 3. Not JSON: 33 levels of nesting.
    deep = "[" * 33 + "]" * 33
    # 4. Form: the block twice.
    two = block("{}") + "\n" + block("{}")
    return [(block(payload), None), (block(shape), "shape"), (block(deep), "not_json"), (two, "form")]


def test_a_report_of_200_events_with_near_cap_adversarial_blocks_gives_each_verdict_and_holds_the_lock_briefly(
        offenv):
    """R3-03 / R4-07: 49 asked turns in one 200-event report, cycling through near-cap accepted, shape, not-JSON and
    form replies: every verdict is the one expected (none `internal`), and the whole report is quick."""
    sid = start(offenv, text="x")["session_id"]
    token = "0123456789abcdef"
    with hosted.db.write_tx(offenv.board.conn) as c:
        r = hosted._session(c, sid)
        for i in range(49):
            hosted._enqueue(c, r, "user", f"m{i}", "human", offenv.board.now(),
                            output=hosted.ask(f"m{i}", SCHEMA, "human")[0] | {"token": token})
    kinds = adversarial_replies(token)
    events = [{"kind": "delivered", "message": 1}, {"kind": "turn_ended", "message": 1, "ok": True, "text": "x"}]
    for n in range(2, 51):
        events += [{"kind": "delivered", "message": n},
                   {"kind": "turn_ended", "message": n, "ok": True, "text": kinds[n % 4][0]}]
    events += [events[-1]] * (200 - len(events))         # repeats change nothing: the report is still 200 events
    assert len(events) == 200 and all(len(k[0].encode()) <= REPLY_MAX_BYTES for k in kinds)
    t0 = time.perf_counter()
    report(offenv, sid, *events)
    took = time.perf_counter() - t0
    for n in range(2, 51):
        out = record(offenv, sid, n)["output"]
        want = kinds[n % 4][1]
        assert (out["state"], out["code"]) == (("accepted", None) if want is None else ("missing", want)), n
    shaped = record(offenv, sid, 5)["output"]["reasons"]       # (5 % 4 == 1: the shape kind)
    assert len(shaped) == o.REASONS_MAX and shaped[-1].startswith("(and ")
    assert took < 5.0, took


# ------------------------------------------------------------ the human's retries (R2-10, R3-07)


def test_a_schema_refusal_waits_for_the_retry_lookup_and_the_acknowledgement_needs_a_schema(senv):
    sid = root(senv)
    answer(senv, sid)
    first = senv.human.post(f"/api/hosted-sessions/{sid}/messages", json={"message": "plain", "request_id": "r1"})
    assert first.status_code == 200 and "output_request" not in first.json()
    retry = senv.human.post(f"/api/hosted-sessions/{sid}/messages",
                            json={"message": "plain", "request_id": "r1", "output_schema": '{"type": "array"}'})
    assert retry.status_code == 200, "a retry gets its message back before the schema is refused"
    assert retry.json()["output_request"] == {"stored": False, "why": "the original message (message 2) asked for no "
                                                                      "result as data"}
    bad = senv.human.post(f"/api/hosted-sessions/{sid}/messages",
                          json={"message": "new", "request_id": "r2", "output_schema": '{"type": "array"}'})
    assert bad.status_code == 400 and 'the top level must be "type": "object"' in bad.json()["message"]
    ok = senv.human.post(f"/api/hosted-sessions/{sid}/messages",
                         json={"message": "new", "request_id": "r3", "output_schema": SCHEMA}).json()
    again = senv.human.post(f"/api/hosted-sessions/{sid}/messages",
                            json={"message": "new", "request_id": "r3"}).json()
    assert ok["output_request"]["stored"] and "output_request" not in again, "only a call that carried a schema"


def test_the_humans_child_start_retry_is_resolved_before_its_schema_refusal(senv):
    sid = root(senv)
    answer(senv, sid)
    made = child(senv, sid, "go", request_id="c1")
    r = senv.human.post(f"/api/hosted-sessions/{sid}/children",
                        json={"message": "go", "request_id": "c1", "output_schema": "{not json"})
    assert r.status_code == 200 and r.json()["session"]["session_id"] == made["session_id"]
    assert r.json()["output_request"]["stored"] is False
    r = senv.human.post(f"/api/hosted-sessions/{sid}/children",
                        json={"message": "go", "request_id": "c2", "output_schema": "{not json"})
    assert r.status_code == 400 and "not strict JSON" in r.json()["message"]


# ------------------------------------------------------------ an agent asks: spawn, send, the result


def asked_child(env, **kw) -> tuple[str, str, str]:
    sid = a_root(env)
    grant(env)
    out = spawn(env, bound(env, sid), task="Review the diff", output_schema=SCHEMA, **kw)
    assert out["output_request"]["stored"] and out["created"]
    kid = out["child"]["id"]
    tick(env)
    return sid, kid, token_of(kid_turns(env, kid)[0])


def test_the_childs_frame_has_the_ask_line_then_the_task_then_the_schema(senv):
    sid, kid, t = asked_child(senv)
    [text] = kid_turns(senv, kid)
    lines = text.split("\n")
    i = lines.index(f"<<<task {t}>>>")
    assert lines[i - 1] == o.TASK_ASK.format(t=t) and lines[i - 2].startswith("You may start helper sessions")
    assert lines[-3:] == [f"<<<output-schema {t}>>>", o.prepare_schema(SCHEMA)[1], f"<<<end output-schema {t}>>>"]


def test_the_parent_gets_its_result_turn_plus_one_hub_line_and_the_reply_as_written(senv):
    sid, kid, t = asked_child(senv)
    text = reply_with(t)
    answer(senv, kid, text)
    [res] = results(senv, sid)
    assert res["output"] == {"state": "accepted", "schema_sha": o.prepare_schema(SCHEMA)[2],
                             "reply": hosted_results.REPLY_NOT_IN_VIEW}
    tick(senv)
    turn = turns_of(senv, sid)[-1]
    tok = RESULT_TOKEN.search(turn).group(1)
    assert f"{o.ACCEPTED_LINE}\n<<<child-output {tok}>>>\n{text}\n<<<end child-output {tok}>>>" in turn
    assert turn.count("Result as data") == 1


def test_a_two_result_batch_and_a_marked_repeat_carry_each_results_own_line(senv):
    """R3-01: the verdict line always stands right above the reply it was made from."""
    sid = a_root(senv)
    grant(senv)
    kids = []
    for task in ("a", "b"):
        out = spawn(senv, bound(senv, sid), task=task, output_schema=SCHEMA)
        kids.append(out["child"]["id"])
    tick(senv)
    t0 = token_of(kid_turns(senv, kids[0])[0])
    from test_hosted_tree import as_started, began
    from test_session_host import result
    for kid, text in ((kids[0], reply_with(t0)), (kids[1], "no data here")):
        call, proc = proc_of(senv, kid)
        proc.emit(began(kid, *as_started(call["argv"])), result(kid, text=text))
    tick(senv)          # both turns end in one pass: two results wait
    tick(senv)          # ... and go as one turn
    turn = turns_of(senv, sid)[-1]
    assert turn.count(o.ACCEPTED_LINE) == 1 and turn.count("missing (no_block)") == 1
    sections = re.split(r"\nResult \d of 2 ", turn)[1:]
    assert len(sections) == 2
    for sec in sections:
        if reply_with(t0) in sec:
            assert sec.index(o.ACCEPTED_LINE) < sec.index(reply_with(t0)) and "missing" not in sec
        else:
            assert sec.index("missing (no_block)") < sec.index("no data here") and o.ACCEPTED_LINE not in sec
    # The parent's process dies before its provider says the turn started: each result goes once more, a marked
    # repeat, its own line with it.
    proc_of(senv, sid)[1].code = 1
    tick(senv, times=3)
    again = turns_of(senv, sid)[-1]
    assert again != turn and again.count("may repeat result") == 2
    assert again.count(o.ACCEPTED_LINE) == 1 and again.count("missing (no_block)") == 1


def test_a_claim_carries_the_verdict_in_its_facts_and_its_line_once_and_clears_the_reply(senv):
    sid, kid, t = asked_child(senv)
    text = reply_with(t)
    answer(senv, kid, text)
    out = hs.taken(hosted_results.claim(senv.board, sid, "claude", child=kid, message=1))
    assert out["result"]["output"] == {"state": "accepted", "schema_sha": o.prepare_schema(SCHEMA)[2]}
    assert out["text"].count(o.ACCEPTED_LINE) == 1 and text in out["text"]
    assert stored_result(senv, sid, 1)["text"] == "" and "value" not in json.dumps(stored_result(senv, sid, 1))


def test_status_shows_the_verdict_of_the_callers_task_turn_labelled_historical(senv):
    sid, kid, t = asked_child(senv)
    st = hs.status(senv.board, senv.p["claude"], bound(senv, sid))
    [view] = st["children"]
    assert view["in_flight_output"]["state"] == "asked" or view["last_task_turn"] is None
    answer(senv, kid, "no block")
    [view] = hs.status(senv.board, senv.p["claude"], bound(senv, sid))["children"]
    assert view["last_task_turn"]["output"] == {"state": "missing", "schema_sha": o.prepare_schema(SCHEMA)[2],
                                                "code": "no_block", "reasons": [], "reply": hs.STATUS_REPLY}
    assert "in_flight_output" not in view


def test_a_human_asked_turn_of_a_human_started_child_gives_its_parent_exactly_mains_result(senv):
    """R2-05: the parent gets the prose (block included) and no line, no field."""
    sid = root(senv)
    answer(senv, sid)
    kid = child(senv, sid, "go", output_schema=SCHEMA)["session_id"]
    tick(senv)
    t = token_of(turns_of(senv, kid)[0])
    text = reply_with(t)
    answer(senv, kid, text)
    assert record(senv, kid, 1)["output"]["state"] == "accepted"
    [res] = results(senv, sid)
    assert "output" not in res and "output" not in stored_result(senv, sid, 1)
    tick(senv)
    turn = turns_of(senv, sid)[-1]
    assert "Result as data" not in turn and text in turn


def test_send_asks_for_a_follow_ups_result_and_a_retry_gets_the_original(senv):
    sid, kid, t = asked_child(senv)
    answer(senv, kid, reply_with(t))
    out = hs.send(senv.board, senv.p["claude"], bound(senv, sid), kid, message="again", request_id="s1",
                  output_schema=SCHEMA)
    assert out["output_request"] == {"stored": True, "schema_sha": o.prepare_schema(SCHEMA)[2]}
    retry = hs.send(senv.board, senv.p["claude"], bound(senv, sid), kid, message="again", request_id="s1",
                    output_schema='{"type": "array"}')
    assert retry["output_request"] == {"stored": True, "schema_sha": o.prepare_schema(SCHEMA)[2], "original": True}
    plain = hs.send(senv.board, senv.p["claude"], bound(senv, sid), kid, message="again", request_id="s1")
    assert "output_request" not in plain
    with pytest.raises(Invalid, match="top level"):
        hs.send(senv.board, senv.p["claude"], bound(senv, sid), kid, message="x", request_id="s2",
                output_schema='{"type": "array"}')
    with pytest.raises(Invalid, match="applies to a message"):
        hs.send(senv.board, senv.p["claude"], bound(senv, sid), kid, may_spawn=False, output_schema=SCHEMA)


def test_a_spawn_retry_is_resolved_before_its_schema_refusal(senv):
    sid = a_root(senv)
    grant(senv)
    first = spawn(senv, bound(senv, sid), task="x", request_id="k1")
    again = spawn(senv, bound(senv, sid), task="x", request_id="k1", output_schema="{broken")
    assert again["created"] is False and again["child"]["id"] == first["child"]["id"]
    assert again["output_request"]["stored"] is False
    with pytest.raises(Invalid, match="not strict JSON"):
        spawn(senv, bound(senv, sid), task="x", request_id="k2", output_schema="{broken")


def test_a_frame_that_fits_only_once_the_targets_are_cut_is_accepted_and_one_that_never_fits_is_refused():
    """R4-08 (SR-09): with a schema the WHOLE message is measured; the spawn line's targets fall back to the pointer
    to board_session_capabilities, and only what still does not fit is refused."""
    targets = "targets " + ", ".join(f"claude/model-{i:02d}-" + "x" * 140 for i in range(50))
    line = hs.SPAWN_LINE.format(targets=targets, levels="you are at depth 1 of at most 2", live=4, hour=11, turns=39)
    schema = {"type": "object", "properties": {f"p{i}": {"type": "string", "description": "d" * 280}
                                               for i in range(25)}}
    shown = o.prepare_schema(json.dumps(schema))[1]
    assert len(shown) <= o.SCHEMA_MAX_BYTES

    def asked(task):
        return hosted.ask(task, json.dumps(schema), "parent", frame=lambda token, s: hs._frame(
            "hosted session x (agent claude)", task, line, [], schema=s, token=token, targets=targets))

    short_room = len(hs._frame("hosted session x (agent claude)", "", line.replace(targets, o.SHORT_TARGETS), [],
                               schema=shown, token="0" * 16, targets=None).encode())
    task = "t" * (hosted.MESSAGE_MAX_BYTES - short_room - 10)
    full = hs.render_task("hosted session x (agent claude)", task, line, token="0" * 16, schema=shown)
    assert len(full.encode()) > hosted.MESSAGE_MAX_BYTES, "with its targets the message would not fit"
    output, text, why = asked(task)
    assert why is None and o.SHORT_TARGETS in text and targets not in text
    assert len(text.encode()) <= hosted.MESSAGE_MAX_BYTES
    _, _, why = asked(task + "t" * 200)
    assert why and "over the 65,536 of one message" in why


def test_an_interactive_root_asks_and_its_claim_carries_the_verdict(senv):
    grant(senv)
    out = spawn(senv, interactive(senv), task="look", output_schema=SCHEMA)
    kid = out["child"]["id"]
    tick(senv)
    t = token_of(kid_turns(senv, kid)[0])
    answer(senv, kid, reply_with(t))
    res = anyio.run(lambda: hosted_results.claim_wait(senv.board, out["child"]["parent"], "claude", child=kid,
                                                      message=1, wait=1))
    facts = hs.taken(res["result"])
    assert facts["result"]["output"]["state"] == "accepted"


NASTY = '{"type": "object", "type": "object", "x": ' + "[" * 25 + "]" * 25 + "}"


def test_the_mcp_tools_take_the_schema_as_text_over_stdio_for_spawn_and_send(senv, monkeypatch):
    """R2-03 / R4-08: `output_schema: str` exactly, so the SDK does not pre-decode it; a duplicate key and deep
    nesting reach the strict decoder as written, for spawn AND send, through the stdio server's own call path."""
    sid = a_root(senv)
    grant(senv)
    kid = spawn(senv, bound(senv, sid), task="first")["child"]["id"]
    seen = []
    real = o.prepare_schema

    def spy(text):
        seen.append(text)
        return real(text)
    monkeypatch.setattr(o, "prepare_schema", spy)
    mcp = build_mcp(senv.board, "stdio")
    for name in ("board_session_spawn", "board_session_send"):
        tool = mcp._tool_manager.get_tool(name)
        assert tool.fn_metadata.arg_model.model_fields["output_schema"].annotation is str
        assert tool.fn_metadata.pre_parse_json({"output_schema": NASTY})["output_schema"] == NASTY
    monkeypatch.setenv("AGENT_COMMS_TOKEN", senv.tokens["claude"])
    monkeypatch.setenv(hs.HOSTED_ENV, sid)
    monkeypatch.setenv(hs.KEY_ENV, hs.bind_key(senv.board.conn, sid))
    before = len(hosted._messages(senv.board.conn, kid))
    for name, args in (("board_session_spawn", {"task": "x"}), ("board_session_send", {"child": kid, "message": "x"})):
        async def call():
            return await mcp.call_tool(name, args | {"output_schema": NASTY})
        with pytest.raises(Exception, match="not strict JSON"):
            anyio.run(call)
    assert seen == [NASTY, NASTY]
    assert len(hosted._messages(senv.board.conn, kid)) == before and len(hs._own_children(
        senv.board.conn, hs.resolve(senv.board, senv.p["claude"], bound(senv, sid)))) == 1, "nothing was enqueued"


def test_the_mcp_tools_take_the_schema_as_text_over_the_remote_bridge_for_spawn_and_send(senv):
    """R4-08: the same through the remote bridge to the hub's HTTP MCP endpoint, with the session's binding headers;
    an empty string is the declared default (not asked), and a valid schema is acknowledged with ITS fingerprint."""
    from fastapi.testclient import TestClient

    from agent_comms import remote
    sid = a_root(senv)
    grant(senv)
    kid = spawn(senv, bound(senv, sid), task="first")["child"]["id"]
    hs.ensure_secret(senv.board)
    with TestClient(senv.app, base_url="http://127.0.0.1:8787") as tc:
        out = []
        b = remote.Bridge("http://127.0.0.1:8787", senv.tokens["claude"], out.append, client=tc)
        b.client.headers[hs.H_SESSION], b.client.headers[hs.H_KEY] = sid, hs.bind_key(senv.board.conn, sid)
        b.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                  "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                             "clientInfo": {"name": "test", "version": "1"}}})
        b.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})

        def call(i, name, args):
            b.handle({"jsonrpc": "2.0", "id": i, "method": "tools/call", "params": {"name": name, "arguments": args}})
            return out[-1]["result"]
        for i, (name, args) in enumerate((("board_session_spawn", {"task": "x"}),
                                          ("board_session_send", {"child": kid, "message": "x"})), 10):
            res = call(i, name, args | {"output_schema": NASTY})
            assert res.get("isError") and "not strict JSON" in res["content"][0]["text"], res
            plain = json.loads(call(i + 20, name, args | {"output_schema": ""})["content"][0]["text"])
            assert "output_request" not in plain, "an empty string is the MCP default: not asked"
            asked = json.loads(call(i + 40, name, args | {"output_schema": SCHEMA})["content"][0]["text"])
            assert asked["output_request"] == {"stored": True, "schema_sha": o.prepare_schema(SCHEMA)[2]}


# ------------------------------------------------------------ rollback and re-upgrade (SR-11, R2-06, R3-08)


def test_a_message_settled_by_an_older_hub_shows_unchecked_and_its_full_reply(offenv, monkeypatch):
    sid, t = asked_root(offenv)
    monkeypatch.setattr(hosted, "_closed", lambda *a, **kw: None)     # main's _apply: no verdict is made
    text = reply_with(t)
    report(offenv, sid, {"kind": "delivered", "message": 1}, {"kind": "turn_ended", "message": 1, "ok": True,
                                                              "text": text})
    monkeypatch.undo()
    m = get_message(offenv, sid, 1)
    assert m["message"]["output"]["state"] == "unchecked" and m["session"]["reply"] == text
    assert "result_data" not in m


MAIN = "a8b90cc"      # the commit this change is built on: main's own code, run for real below


def test_mains_own_reader_claims_an_asked_result_whole_block_included(senv, tmp_path):
    """R3-08 / R4-08: main's code (exported from git) opens this hub's database and claims a result whose message
    asked for data: it returns the complete reply, block included, and knows nothing of the verdict."""
    root_dir = Path(__file__).resolve().parents[1]
    if subprocess.run(["git", "-C", str(root_dir), "cat-file", "-e", MAIN + "^{commit}"],
                      capture_output=True).returncode != 0:
        pytest.skip(f"main's commit {MAIN} is not in this clone")
    sid, kid, t = asked_child(senv)
    text = reply_with(t)
    answer(senv, kid, text)
    old = tmp_path / "main"
    old.mkdir()
    archive = subprocess.run(["git", "-C", str(root_dir), "archive", MAIN, "agent_comms"], capture_output=True,
                             check=True).stdout
    subprocess.run(["tar", "-x", "-C", str(old)], input=archive, check=True)
    assert not (old / "agent_comms" / "output_schema.py").exists(), "main's code, without this change"
    code = ("import json, sys\n"
            "from agent_comms import hosted_results, hosted_spawn\n"
            "from pathlib import Path\n"
            "from agent_comms.config import Settings\n"
            "from agent_comms.core import Board\n"
            "s = Settings(db_path=Path(sys.argv[1]), agents_path=Path(sys.argv[2]))\n"
            "s.dispatch = json.loads(sys.argv[3])\n"
            "b = Board(s)\n"
            "x = hosted_results.claim(b, sys.argv[4], 'claude', child=sys.argv[5], message=1)\n"
            "print(json.dumps(hosted_spawn.taken(x)))\n")
    r = subprocess.run([sys.executable, "-c", code, str(senv.settings.db_path), str(senv.settings.agents_path),
                        json.dumps(senv.board.s.dispatch), sid, kid], cwd=str(old), capture_output=True, text=True,
                       env={"PYTHONPATH": str(old), "PATH": "/usr/bin:/bin", "HOME": str(tmp_path)}, timeout=120)
    assert r.returncode == 0, r.stderr[-2000:]
    got = json.loads(r.stdout.strip().splitlines()[-1])
    assert text in got["text"] and "output" not in got["result"] and "Result as data" not in got["text"]
    assert stored_result(senv, sid, 1)["state"] == "claimed", "main's reader took it, under main's claim rules"


# ------------------------------------------------------------ the CLI


def test_the_cli_reads_a_schema_file_with_a_bound(tmp_path):
    from agent_comms import cli
    f = tmp_path / "s.json"
    f.write_text(SCHEMA)
    assert cli._read_schema(str(f)) == SCHEMA and cli._read_schema(SCHEMA) == SCHEMA
    f.write_bytes(b" " * (o.SCHEMA_MAX_BYTES + 1))
    with pytest.raises(SystemExit, match="over 8,192 bytes"):
        cli._read_schema(str(f))


def test_wait_exits_0_3_and_4_with_their_words(offenv, monkeypatch, capsys):
    from agent_comms import cli
    from test_hosted_sessions import run_turn
    calls = []

    def api(settings, token, method, path, body=None, timeout=10):
        r = offenv.human.request(method, path, json=body)
        assert r.status_code == 200, r.text
        calls.append(path)
        if method == "POST":
            sid = r.json()["session"]["session_id"]
            tick(offenv)
            text = human_turns(offenv, sid)[-1]
            run_turn(offenv, sid, text=reply_with(token_of(text)) if "valid" in body["message"] else "nothing")
        return r.json()
    monkeypatch.setattr(cli, "_api", api)
    monkeypatch.setattr(cli, "_human_token", lambda: "t")

    def run(*argv):
        try:
            cli.main(["session", *argv])
        except SystemExit as e:
            return e.code
        return 0

    assert run("start", PROJECT, "valid one", "--output-schema", SCHEMA, "--wait") in (0, None)
    err = capsys.readouterr().err
    assert "result as data: accepted" in err
    sid = offenv.board.conn.execute("SELECT key FROM board_state WHERE key LIKE 'host.session.%'").fetchone()[0][13:]
    assert run("send", sid, "nothing", "--output-schema", SCHEMA, "--wait") == 3
    assert "result as data: missing (no_block)" in capsys.readouterr().err
    assert run("send", sid, "plain", "--wait") in (0, None)


# ------------------------------------------------------------ with no schema, byte for byte as on main


def test_with_no_schema_every_frame_record_answer_listing_and_cli_text_is_mains(tmp_path):
    """tests/fixtures/schema_free_main.json is golden_schema_free.py's recording of main's code (a8b90cc): frames,
    result turns, a claim, spawn and send answers and their retries, status, show, a message poll, list, the node's
    listing, the lineage view, CLI text and every hosted record. This checkout's run must be the same, byte for
    byte (time stamps and the temporary directory aside)."""
    from pathlib import Path

    import golden_schema_free
    main = json.loads((Path(__file__).parent / "fixtures" / "schema_free_main.json").read_text())
    ours = json.loads(json.dumps(golden_schema_free.record(tmp_path), sort_keys=True))
    for key in sorted(main):
        assert ours[key] == main[key], key
    assert set(ours) == set(main)


def test_a_later_reply_that_repeats_the_block_derives_nothing_for_the_earlier_message(offenv):
    """R3-02: only the reply that belongs to the message (reply_for) is read, even if a later one ends with the very
    same block (a session that repeats itself)."""
    from test_hosted_sessions import run_turn
    sid, t = asked_root(offenv)
    text = reply_with(t)
    run_turn(offenv, sid, text=text)
    assert get_message(offenv, sid, 1)["result_data"] == GOOD
    offenv.human.post(f"/api/hosted-sessions/{sid}/messages", json={"message": "again"})
    tick(offenv)
    run_turn(offenv, sid, text=text)            # the same block, the earlier message's token
    m = get_message(offenv, sid, 1)
    assert m["message"]["output"]["reply"] == hosted.REPLY_GONE and "result_data" not in m


def test_the_full_reason_allowance_fits_the_claim_facts_and_its_framed_text_on_one_line(senv):
    """R3-06: ten reasons at their byte allowance, names with quotes and backslashes, in the claim's facts and in
    its framed text."""
    names = ['q"\\' * 12 + str(i) for i in range(12)]
    schema = {"type": "object", "required": names, "properties": {n: {"type": "string"} for n in names}}
    sid = a_root(senv)
    grant(senv)
    out = spawn(senv, bound(senv, sid), task="x", output_schema=json.dumps(schema))
    kid = out["child"]["id"]
    tick(senv)
    t = token_of(kid_turns(senv, kid)[0])
    answer(senv, kid, f"x\n<<<result-data {t}>>>\n{{}}\n<<<end result-data {t}>>>")
    claimed = hs.taken(hosted_results.claim(senv.board, sid, "claude", child=kid, message=1))
    reasons = claimed["result"]["output"]["reasons"]
    assert len(reasons) == o.REASONS_MAX and reasons[-1] == "(and 3 more)"
    assert all(len(r.encode()) <= o.REASON_MAX and r.isascii() and "\n" not in r for r in reasons)
    [line] = [x for x in claimed["text"].split("\n") if x.startswith("Result as data")]
    assert line.endswith(".") and line.isascii() and "; ".join(reasons) in line
    for r in reasons[:-1]:
        assert r.count('\\"') >= 1 and r.endswith('is missing'), "names whole, quoted, their escapes intact"


def test_projections_are_whitelists_without_the_schema_or_the_token(senv):
    """R3-07: the fingerprint where intended; the schema and the token never as metadata."""
    sid, kid, t = asked_child(senv)
    shown = o.prepare_schema(SCHEMA)[1]
    answer(senv, kid, reply_with(t))
    views = [show(senv, kid)["messages"], show(senv, sid).get("results"),
             hs.status(senv.board, senv.p["claude"], bound(senv, sid)),
             senv.human.get(f"/api/hosted-trees/{kid}").json()["messages"],
             senv.human.get(f"/api/hosted-trees/{sid}").json()["results"]]
    for v in views:
        text = json.dumps(v)
        assert t not in text and shown not in text and '"token"' not in text and '"schema"' not in text
    assert o.prepare_schema(SCHEMA)[2] in json.dumps(views[0])


def test_wait_exits_0_without_result_data_and_4_for_a_child_start_retry_whose_message_is_gone(monkeypatch, capsys):
    """R3-02: an accepted verdict whose data cannot be re-derived still exits 0; a child start's retry that finds
    only the child (its first message pruned) has no verdict: exit 4."""
    from agent_comms import cli
    sess = {"session_id": "s", "parent": "p", "node": "n", "node_live": True, "state": "idle", "agent": "claude",
            "name": "general-1", "access": "read-only", "isolation": "shared", "model": None, "turns": 0,
            "queued": 0, "reply": "x", "reply_for": 1, "transcript": "t", "role": "general", "stop": None}

    def api(settings, token, method, path, body=None, timeout=10):
        if method == "POST":
            return {"session": sess, "message": None if "children" in path else {"n": 1, "state": "queued"},
                    "created": False, "output_request": {"known": False, "why": "the original message is gone"}}
        return {"session": sess, "message": {"n": 1, "state": "done", "detail": "", "denials": [],
                                             "output": {"state": "accepted", "schema_sha": "ab",
                                                        "reply": hosted.REPLY_WHOLE_BLOCK}}}
    monkeypatch.setattr(cli, "_api", api)
    monkeypatch.setattr(cli, "_human_token", lambda: "t")

    def run(*argv):
        try:
            cli.main(["session", *argv])
        except SystemExit as e:
            return e.code
        return 0
    assert run("send", "s", "x", "--output-schema", SCHEMA, "--wait", "--json") == 0
    assert "result_data" not in capsys.readouterr().out
    assert run("start", "--parent", "p", "x", "--request-id", "r", "--output-schema", SCHEMA, "--wait") == 4
    assert "no verdict: the first message is no longer kept on the hub" in capsys.readouterr().err


def cli_runner(monkeypatch, api):
    from agent_comms import cli
    monkeypatch.setattr(cli, "_api", api)
    monkeypatch.setattr(cli, "_human_token", lambda: "t")

    def run(*argv):
        try:
            cli.main(["session", *argv])
        except SystemExit as e:
            return e.code
        return 0
    return run


SESS = {"session_id": "s", "parent": None, "node": "n", "node_live": True, "state": "idle", "agent": "claude", "project": "/p",
        "model": None, "turns": 1, "queued": 0, "reply": "x", "reply_for": 1, "transcript": "t", "stop": None}


def test_a_structured_wait_whose_message_is_pruned_before_its_poll_exits_4_and_a_schema_free_one_as_ever(
        monkeypatch, capsys):
    """R4-06: the POST answered (queued); a later completion pruned the message before the poll read it: the poll's
    404 is "no longer kept", exit 4, for a structured wait. A schema-free wait keeps main's handling (the error, exit
    1), and so does a transport failure."""
    calls = []

    def api(settings, token, method, path, body=None, timeout=10):
        calls.append(method)
        if method == "POST":
            return {"session": SESS, "message": {"n": 1, "state": "queued"}}
        raise SystemExit("error (not_found): session s has no message 1")
    run = cli_runner(monkeypatch, api)
    assert run("send", "s", "x", "--output-schema", SCHEMA, "--wait") == 4
    assert "no verdict: the message is no longer kept on the hub" in capsys.readouterr().err
    assert run("send", "s", "x", "--wait") == "error (not_found): session s has no message 1"

    def down(settings, token, method, path, body=None, timeout=10):
        if method == "POST":
            return {"session": SESS, "message": {"n": 1, "state": "queued"}}
        raise SystemExit("the board server is not running at http://127.0.0.1:8787 (refused)")
    run = cli_runner(monkeypatch, down)
    assert run("send", "s", "x", "--output-schema", SCHEMA, "--wait").startswith("the board server is not running")


def test_a_structured_retry_whose_message_already_ended_takes_the_verdict_from_the_post_and_polls_nothing(
        monkeypatch, capsys):
    """R4-06: a retry's POST returns the original message, already over, with its verdict: no poll is made (the
    record may be pruned before one could read it), and the exit follows that verdict."""
    calls = []

    def api(settings, token, method, path, body=None, timeout=10):
        calls.append(method)
        if method == "POST":
            return {"session": SESS, "created": False, "message": {
                "n": 1, "state": "done", "detail": "", "denials": [],
                "output": {"state": "missing", "code": "no_block", "words": o.WORDS["no_block"], "reasons": [],
                           "schema_sha": "ab", "reply": hosted.REPLY_WHOLE}},
                "output_request": {"stored": True, "schema_sha": "ab", "original": True}}
        raise AssertionError("no poll")
    run = cli_runner(monkeypatch, api)
    assert run("send", "s", "x", "--output-schema", SCHEMA, "--request-id", "r", "--wait") == 3
    assert calls == ["POST"] and "result as data: missing (no_block)" in capsys.readouterr().err


# ------------------------------------------------------------ R4-01, R4-02: what is refused before anything runs


def counts(env) -> tuple[int, int]:
    sessions = len(hosted._sessions(env.board.conn))
    msgs = env.board.conn.execute("SELECT COUNT(*) FROM board_state WHERE key LIKE 'host.msg.%'").fetchone()[0]
    return sessions, msgs


def test_an_empty_or_invalid_schema_over_http_is_refused_and_nothing_is_enqueued_while_null_is_not_asked(senv):
    """R4-01: absent or null is "not asked"; an EMPTY string is a schema given, and refused with its words, on the
    root start, the child start and the send."""
    sid = root(senv)
    answer(senv, sid)
    for bad in ("", "   ", "{broken", '{"type": "array"}'):
        before = counts(senv)
        for path, body in (("/api/hosted-sessions", {"project": "/work/second", "message": "go"}),
                           (f"/api/hosted-sessions/{sid}/children", {"message": "go"}),
                           (f"/api/hosted-sessions/{sid}/messages", {"message": "go"})):
            r = senv.human.post(path, json=body | {"output_schema": bad})
            assert r.status_code == 400 and r.json()["message"].startswith("output_schema"), (path, bad, r.text)
        assert counts(senv) == before, f"nothing was enqueued for {bad!r}"
    r = senv.human.post(f"/api/hosted-sessions/{sid}/messages", json={"message": "go", "output_schema": None})
    assert r.status_code == 200 and "output_request" not in r.json() and "output" not in r.json()["message"]


def test_the_cli_refuses_an_empty_schema_argument_or_file_before_any_request(monkeypatch, tmp_path):
    """R4-01: `--output-schema ''` and a zero-byte (or blank) file stop the CLI before it calls the hub."""
    calls = []

    def api(*a, **kw):
        calls.append(a)
        raise AssertionError("no request")
    run = cli_runner(monkeypatch, api)
    empty, blank = tmp_path / "empty.json", tmp_path / "blank.json"
    empty.write_bytes(b"")
    blank.write_text(" \n\t")
    assert "is empty" in run("send", "s", "x", "--output-schema", "", "--wait")
    assert "is empty" in run("start", "/work/p", "x", "--output-schema", "  ")
    assert "is empty" in run("send", "s", "x", "--output-schema", str(empty))
    assert "is empty" in run("start", "--parent", "p", "x", "--output-schema", str(blank))
    assert calls == []


def test_fresh_invalid_schemas_on_the_agent_tools_enqueue_nothing(senv):
    """R4-01: spawn and send with a refusable schema leave no child and no message behind."""
    sid = a_root(senv)
    grant(senv)
    kid = spawn(senv, bound(senv, sid), task="first")["child"]["id"]
    before = counts(senv)
    for bad in ("", "{broken", '{"type": "array"}'):
        with pytest.raises(Invalid):
            spawn(senv, bound(senv, sid), task="x", output_schema=bad)
        with pytest.raises(Invalid):
            hs.send(senv.board, senv.p["claude"], bound(senv, sid), kid, message="x", output_schema=bad)
    assert counts(senv) == before


def test_a_blank_or_oversized_child_task_with_a_valid_schema_is_refused_with_mains_words_and_starts_nothing(senv):
    """R4-02: the ORIGINAL task is checked first, as main checks it: a schema never makes a blank (or an oversized)
    task into a runnable one."""
    sid = root(senv)
    answer(senv, sid)
    before = counts(senv)
    r = senv.human.post(f"/api/hosted-sessions/{sid}/children", json={"message": "   ", "output_schema": SCHEMA})
    assert r.status_code == 400 and r.json()["message"] == "message text is empty"
    big = "x" * (hosted.MESSAGE_MAX_BYTES + 1)
    r = senv.human.post(f"/api/hosted-sessions/{sid}/children", json={"message": big, "output_schema": SCHEMA})
    assert r.status_code == 400 and r.json()["message"] == "message text is over 64 KB"
    assert counts(senv) == before
    grant(senv)
    with pytest.raises(Invalid, match="the task is empty"):
        spawn(senv, bound(senv, sid), task="  ", output_schema=SCHEMA)


# ------------------------------------------------------------ R4-08: the retry lookup wins a real race


def test_a_spawn_retry_that_loses_the_race_to_a_concurrent_first_call_gets_its_child_not_a_refusal(senv,
                                                                                                  monkeypatch):
    """The loser's fast path misses (the winner has not committed); the winner commits; the loser's deciding
    transaction finds the winner's child and returns it, though the loser's own schema would be refused."""
    sid = a_root(senv)
    grant(senv)
    real = hs._retried
    won = {}

    def fast_path(*a, **kw):
        out = real(*a, **kw)
        assert out is None, "the fast path missed"
        monkeypatch.setattr(hs, "_retried", real)
        won["first"] = spawn(senv, bound(senv, sid), task="x", request_id="race", output_schema=SCHEMA)
        return out
    monkeypatch.setattr(hs, "_retried", fast_path)
    lost = spawn(senv, bound(senv, sid), task="x", request_id="race", output_schema="{broken")
    assert lost["created"] is False and lost["child"]["id"] == won["first"]["child"]["id"]
    assert lost["output_request"] == {"stored": True, "schema_sha": o.prepare_schema(SCHEMA)[2], "original": True}


def test_a_child_start_retry_that_loses_the_race_gets_the_winners_child(senv, monkeypatch):
    sid = root(senv)
    answer(senv, sid)
    real = hosted_tree._existing
    won = {}

    def outer(*a, **kw):
        out = real(*a, **kw)
        if not won:
            won["first"] = None
            monkeypatch.setattr(hosted_tree, "_existing", real)
            won["first"] = child(senv, sid, "go", request_id="race", output_schema=SCHEMA)
        return out
    monkeypatch.setattr(hosted_tree, "_existing", outer)
    r = senv.human.post(f"/api/hosted-sessions/{sid}/children",
                        json={"message": "go", "request_id": "race", "output_schema": "{broken"})
    assert r.status_code == 200, r.text
    assert r.json()["session"]["session_id"] == won["first"]["session_id"] and r.json()["created"] is False


def test_a_human_send_retry_that_loses_the_race_gets_the_winners_message(senv, monkeypatch):
    sid = root(senv)
    answer(senv, sid)
    real = hosted.ask
    won = {}

    def asking(text, schema, by, frame=None):
        out = real(text, schema, by, frame)
        if not won:
            won["first"] = None
            monkeypatch.setattr(hosted, "ask", real)
            won["first"] = senv.human.post(f"/api/hosted-sessions/{sid}/messages",
                                           json={"message": "go", "request_id": "race", "output_schema": SCHEMA}).json()
        return out
    monkeypatch.setattr(hosted, "ask", asking)
    r = senv.human.post(f"/api/hosted-sessions/{sid}/messages",
                        json={"message": "go", "request_id": "race", "output_schema": "{broken"})
    assert r.status_code == 200, r.text
    assert r.json()["message"]["n"] == won["first"]["message"]["n"] and r.json()["created"] is False
    assert r.json()["output_request"]["stored"] is True


# ------------------------------------------------------------ R4-04: what each projection says of the reply


def test_each_view_labels_what_it_holds_of_the_reply(senv):
    """R4-04. The details panel cuts a reply at 4,000 characters, so an accepted reply with 4,001 characters of prose
    before its block is there only in part; `show` has it whole, with the block wording; a missing verdict never gets
    the block wording; a replaced reply is "gone" only when no result still carries it."""
    from agent_comms import hosted_lineage
    from test_hosted_tree import as_started, began
    from test_session_host import result
    sid = root(senv)
    answer(senv, sid)
    senv.human.post(f"/api/hosted-sessions/{sid}/messages", json={"message": "busy"})
    kid = child(senv, sid, "go", output_schema=SCHEMA)["session_id"]
    tick(senv)                          # the parent's turn is in flight: its children's results wait for it
    t = token_of(turns_of(senv, kid)[0])

    def kid_answers(text):
        call, proc = proc_of(senv, kid)
        proc.emit(began(kid, *as_started(call["argv"])), result(kid, text=text))
        tick(senv)
    kid_answers(reply_with(t, prose="p" * 4001))
    assert show(senv, kid)["messages"][0]["output"]["reply"] == hosted.REPLY_WHOLE_BLOCK
    detail = senv.human.get(f"/api/hosted-trees/{kid}").json()
    assert detail["messages"][0]["output"]["reply"] == hosted.REPLY_CUT
    assert len(detail["output"]["text"]) == hosted_lineage.OUTPUT_MAX and detail["output"]["truncated"]
    senv.human.post(f"/api/hosted-sessions/{kid}/messages", json={"message": "again", "output_schema": SCHEMA})
    tick(senv)
    kid_answers("no block this time")
    views = {m["n"]: m.get("output") for m in show(senv, kid)["messages"]}
    assert views[2]["state"] == "missing" and views[2]["reply"] == hosted.REPLY_WHOLE, "no block wording"
    assert [x["state"] for x in results(senv, sid)] == ["pending", "pending"]
    assert views[1]["reply"] == hosted.REPLY_COPY, "the result waiting for the parent still carries message 1's"
    answer(senv, sid)                   # the parent's turn ends; the results go as its next turn ...
    tick(senv)
    answer(senv, sid)                   # ... which its process takes: delivered, their texts cleared
    assert [x["state"] for x in results(senv, sid)] == ["delivered", "delivered"]
    views = {m["n"]: m.get("output") for m in show(senv, kid)["messages"]}
    assert views[1]["reply"] == hosted.REPLY_GONE, "no result carries it any more"


def test_a_childs_result_verdict_is_in_the_details_and_not_in_the_tree_poll_and_has_its_words(senv):
    sid, kid, t = asked_child(senv)
    answer(senv, kid, "no block")
    trees = senv.human.get("/api/hosted-trees").json()["trees"]
    node = next(k for k in trees[0]["session"]["children"] if k["session_id"] == kid)
    assert node["result"] and "verdict" not in node["result"], "the poll stays small"
    detail = senv.human.get(f"/api/hosted-trees/{sid}").json()
    [res] = detail["results"]
    assert res["verdict"]["state"] == "missing" and res["verdict"]["words"] == o.WORDS["no_block"]
    kid_detail = senv.human.get(f"/api/hosted-trees/{kid}").json()
    assert kid_detail["version"] == node["version"], "the version still covers the verdict"
    from agent_comms import cli
    text = cli._fmt_hosted_detail(show(senv, sid))
    assert "missing (no_block): the reply does not end with a result-data block" in text and "None" not in text


# ------------------------------------------------------------ R4-05: schema-free paths do no extra work


def test_schema_free_status_and_settlement_do_no_extra_lookup_or_classification(senv, monkeypatch):
    sid = a_root(senv)
    grant(senv)
    kid = spawn(senv, bound(senv, sid), task="plain")["child"]["id"]
    tick(senv)
    r = stored(senv, kid)
    assert r["in_flight"] == 1 and "output_asked" not in r

    def no_get(*a, **kw):
        raise AssertionError("no lookup for a session that was never asked")
    monkeypatch.setattr(hs, "_get", no_get)
    assert hs._in_flight_request(senv.board.conn, r) == {}
    monkeypatch.undo()

    def no_code(*a, **kw):
        raise AssertionError("a failed turn of a message that asked for nothing is not classified")
    monkeypatch.setattr(hosted, "_turn_code", no_code)
    report(senv, kid, {"kind": "turn_ended", "message": 1, "ok": False, "detail": "boom", "text": ""})
    assert record(senv, kid, 1)["state"] == "failed" and "output" not in record(senv, kid, 1)


# ------------------------------------------------------------ R4-10: the size bounds, through the SDK's serializer


def test_near_cap_reasons_with_quotes_and_backslashes_stay_within_the_stated_bounds_as_the_sdk_serializes_them(senv):
    """Ten reasons of 120 to 150 bytes, each holding quoted names made of quotes and backslashes, measured in the
    result line and in the claim as the MCP SDK serializes a tool's dict answer (indent 2): within the notes' bounds
    (the line at most 1,700 bytes; the claim at most 7,000 bytes over the same claim without a verdict)."""
    from mcp.server.mcpserver.utilities.func_metadata import _convert_to_content
    outer = '"\\\\' * 20 + "A"
    inner = {f'q"\\\\{i:02d}' + '"\\\\' * 15: {"type": "string"} for i in range(14)}
    schema = {"type": "object", "properties": {outer: {"type": "object", "properties": inner}}}
    sid = a_root(senv)
    grant(senv)
    kid = spawn(senv, bound(senv, sid), task="x", output_schema=json.dumps(schema))["child"]["id"]
    tick(senv)
    t = token_of(kid_turns(senv, kid)[0])
    value = {outer: {k: 1 for k in inner}}
    answer(senv, kid, f"x\n<<<result-data {t}>>>\n{json.dumps(value)}\n<<<end result-data {t}>>>")
    x = hosted_results.claim(senv.board, sid, "claude", child=kid, message=1)
    claimed = hs.taken(x)
    reasons = claimed["result"]["output"]["reasons"]
    assert len(reasons) == o.REASONS_MAX
    assert all(120 <= len(r.encode()) <= o.REASON_MAX for r in reasons[:-1]), [len(r) for r in reasons]
    assert all('\\"' in r and "\\\\" in r for r in reasons[:-1])
    line = o.result_line(x["output"])
    assert len(line.encode()) <= 1700, len(line.encode())
    plain = hs.taken({k: v for k, v in x.items() if k != "output"})
    size = len(_convert_to_content(claimed)[0].text.encode())
    base = len(_convert_to_content(plain)[0].text.encode())
    assert 0 < size - base <= 7000, size - base


# ------------------------------------------------------------ code review round 5


def passthrough(env, calls):
    """The CLI's `_api` straight onto this hub's routes (the real server path), recording each call."""
    def api(settings, token, method, path, body=None, timeout=10):
        calls.append((method, path))
        r = env.human.request(method, path, json=body)
        if r.status_code != 200:
            raise SystemExit(f"error ({r.json().get('error')}): {r.json().get('message')}")
        return r.json()
    return api


def test_a_structured_retry_of_an_ended_message_gets_its_label_and_data_from_the_same_snapshot(senv, monkeypatch,
                                                                                                capsys):
    """R5-01: a retry (same request id) of an accepted send, and of a child start, whose reply is still the latest:
    the POST's answer labels the reply as whole with the block, carries the digest-checked `result_data`, and the
    CLI polls nothing."""
    from test_hosted_sessions import run_turn
    sid = root(senv)
    answer(senv, sid)
    senv.human.post(f"/api/hosted-sessions/{sid}/messages",
                    json={"message": "go", "request_id": "r1", "output_schema": SCHEMA})
    tick(senv)
    run_turn(senv, sid, text=reply_with(token_of(turns_of(senv, sid)[-1])))
    kid = child(senv, sid, "go", request_id="c1", output_schema=SCHEMA)["session_id"]
    tick(senv)
    answer(senv, kid, reply_with(token_of(turns_of(senv, kid)[0])))
    calls = []
    run = cli_runner(monkeypatch, passthrough(senv, calls))
    capsys.readouterr()
    for argv in (["send", sid, "go", "--request-id", "r1"], ["start", "--parent", sid, "go", "--request-id", "c1"]):
        calls.clear()
        assert run(*argv, "--output-schema", SCHEMA, "--wait", "--json") == 0
        got = json.loads(capsys.readouterr().out)
        assert [m for m, _ in calls] == ["POST"], "no poll: the verdict came with the POST"
        assert got["message"]["output"]["reply"] == hosted.REPLY_WHOLE_BLOCK and got["result_data"] == GOOD


def test_a_structured_wait_whose_session_is_pruned_exits_4_and_a_schema_free_one_as_ever(monkeypatch, capsys):
    """R5-02."""
    def api(settings, token, method, path, body=None, timeout=10):
        if method == "POST":
            return {"session": SESS, "message": {"n": 1, "state": "queued"}}
        raise SystemExit("error (not_found): no such hosted session")
    run = cli_runner(monkeypatch, api)
    assert run("send", "s", "x", "--output-schema", SCHEMA, "--wait") == 4
    assert "no verdict: the message is no longer kept on the hub" in capsys.readouterr().err
    assert run("send", "s", "x", "--wait") == "error (not_found): no such hosted session"


def test_after_a_claim_the_result_says_the_reply_went_with_it_and_the_child_still_holds_its_own(senv):
    """R5-03 (a): the label describes the result record, which no longer holds the reply; the child's own view still
    has it whole as its latest."""
    sid, kid, t = asked_child(senv)
    answer(senv, kid, reply_with(t))
    hosted_results.claim(senv.board, sid, "claude", child=kid, message=1)
    [res] = show(senv, sid)["results"]
    assert res["output"]["reply"] == hosted_results.REPLY_WENT and "child keeps its own latest" in \
        hosted_results.REPLY_WENT and "not kept on the hub" not in hosted_results.REPLY_WENT
    assert show(senv, kid)["messages"][0]["output"]["reply"] == hosted.REPLY_WHOLE_BLOCK


def test_the_copy_label_points_to_no_view_that_omits_the_text():
    """R5-03 (b): while the result waits, the parent's `show` does not list its text: the label says where the reply
    goes, not a view to look in."""
    assert "board session show" not in hosted.REPLY_COPY and "goes to the parent" in hosted.REPLY_COPY


def test_show_labels_from_one_snapshot_of_the_session_and_its_messages(offenv, monkeypatch):
    """R5-03 (c): a turn that settles (from another connection) between `show`'s reads of the session and of its
    messages: both reads see the same state, so the message is never labelled from a session record that does not
    have its reply yet."""
    from agent_comms.core import Board
    sid, t = asked_root(offenv)
    report(offenv, sid, {"kind": "delivered", "message": 1})
    other = Board(offenv.settings, clock=offenv.clock)
    node = other.authenticate(offenv.tokens["laptop"])
    real = hosted._messages
    done = []

    def settle_meanwhile(conn, session_id):
        if not done:
            done.append(1)
            hosted.report(other, node, sid, events=[{"kind": "turn_ended", "message": 1, "ok": True,
                                                       "text": reply_with(t)}])
        return real(conn, session_id)
    monkeypatch.setattr(hosted, "_messages", settle_meanwhile)
    s = show(offenv, sid)
    monkeypatch.setattr(hosted, "_messages", real)
    assert done, "the settlement committed between the reads"
    out = s["messages"][0].get("output", {})
    assert out.get("reply") != hosted.REPLY_NONE, "never labelled from the session record of before the turn"
    assert out.get("state") == "asked" and s["reply_for"] is None, "both reads saw the state of before"
    after = show(offenv, sid)["messages"][0]["output"]
    assert (after["state"], after["reply"]) == ("accepted", hosted.REPLY_WHOLE_BLOCK)


def test_a_plain_message_after_the_last_ask_is_never_read_for_status(senv, monkeypatch):
    """R5-04: `output_asked` keeps the number of the last asked message; a later plain one in flight is not read."""
    sid, kid, t = asked_child(senv)
    answer(senv, kid, reply_with(t))
    hs.send(senv.board, senv.p["claude"], bound(senv, sid), kid, message="plain one")
    tick(senv)
    r = stored(senv, kid)
    assert (r["output_asked"], r["in_flight"]) == (1, 2)

    def no_get(*a, **kw):
        raise AssertionError("no read of a plain message after the last ask")
    monkeypatch.setattr(hs, "_get", no_get)
    assert hs._in_flight_request(senv.board.conn, r) == {}


def test_a_tree_poll_reads_each_parents_outbox_once_however_many_asks_its_child_made(senv, monkeypatch):
    """R5-04: a child with several historical asks (each reply replaced by the next) is labelled from ONE read of
    its parent's outbox per poll."""
    sid = root(senv)
    answer(senv, sid)
    kid = child(senv, sid, "go", output_schema=SCHEMA)["session_id"]
    tick(senv)
    answer(senv, kid, reply_with(token_of(turns_of(senv, kid)[0])))
    for i in range(4):
        senv.human.post(f"/api/hosted-sessions/{kid}/messages", json={"message": f"again {i}",
                                                                       "output_schema": SCHEMA})
        tick(senv)
        answer(senv, kid, "no block")
    real = hosted_results._results
    reads = []

    def counted(conn, pid):
        reads.append(pid)
        return real(conn, pid)
    monkeypatch.setattr(hosted_results, "_results", counted)
    assert senv.human.get("/api/hosted-trees").status_code == 200
    assert reads.count(sid) == 1, reads


def test_a_malformed_schema_reaches_the_strict_decoder_over_a_real_stdio_server_for_spawn_and_send(senv, tmp_path):
    """R5-05: a real `board mcp`-style stdio server in its own process (build_mcp(...).run("stdio") on this hub's
    database, with the session's binding in its environment), driven over JSON-RPC on its pipes: a duplicate-key,
    deeply nested schema is refused by the strict decoder on spawn AND on send, and nothing is enqueued."""
    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client
    sid = a_root(senv)
    grant(senv)
    kid = spawn(senv, bound(senv, sid), task="first")["child"]["id"]
    hs.ensure_secret(senv.board)
    before = counts(senv)
    code = ("import json, sys\n"
            "from pathlib import Path\n"
            "from agent_comms.config import Settings\n"
            "from agent_comms.core import Board\n"
            "from agent_comms.mcp_server import build_mcp\n"
            "s = Settings(db_path=Path(sys.argv[1]), agents_path=Path(sys.argv[2]))\n"
            "s.dispatch = json.loads(sys.argv[3])\n"
            "build_mcp(Board(s), 'stdio').run('stdio')\n")
    root_dir = Path(__file__).resolve().parents[1]
    env = {"PYTHONPATH": str(root_dir), "PATH": "/usr/bin:/bin", "HOME": str(tmp_path),
           "AGENT_COMMS_TOKEN": senv.tokens["claude"], hs.HOSTED_ENV: sid,
           hs.KEY_ENV: hs.bind_key(senv.board.conn, sid)}
    params = StdioServerParameters(command=sys.executable, args=["-c", code, str(senv.settings.db_path),
                                                                 str(senv.settings.agents_path),
                                                                 json.dumps(senv.board.s.dispatch)], env=env)

    async def calls():
        async with stdio_client(params) as (r, w):
            async with ClientSession(r, w) as s:
                await s.initialize()
                out = []
                for name, args in (("board_session_spawn", {"task": "x"}),
                                   ("board_session_send", {"child": kid, "message": "x"})):
                    res = await s.call_tool(name, args | {"output_schema": NASTY})
                    out.append((bool(res.is_error), res.content[0].text if res.content else ""))
                return out
    got = anyio.run(calls)
    assert [e for e, _ in got] == [True, True] and all("not strict JSON" in text for _, text in got), got
    assert counts(senv) == before


# ------------------------------------------------------------ code review round 6


def settle_from_another_connection(env, sid, text):
    """The node's report on `sid`'s message 1, committed through a connection of its own (as another request)."""
    from agent_comms.core import Board
    other = Board(env.settings, clock=env.clock)
    hosted.report(other, other.authenticate(env.tokens["laptop"]), sid,
                  events=[{"kind": "turn_ended", "message": 1, "ok": True, "text": text}])


def test_a_child_start_retry_reads_the_child_and_its_message_in_one_snapshot(senv, monkeypatch):
    """R6-01: the child's turn settles (from another connection) between the retry's read of the child and its read
    of the message: both reads see one moment, so the answer never pairs the child of before with the accepted
    message (which would label it "left no reply" and leave out its reply and data)."""
    sid = root(senv)
    answer(senv, sid)
    kid = child(senv, sid, "go", request_id="c1", output_schema=SCHEMA)["session_id"]
    tick(senv)
    text = reply_with(token_of(turns_of(senv, kid)[0]))
    real = hosted._session
    armed, done = [True], []

    def session_then_settle(conn, session_id):
        out = real(conn, session_id)
        if armed[0] and session_id == kid and not done:
            done.append(1)
            settle_from_another_connection(senv, kid, text)
        return out
    monkeypatch.setattr(hosted, "_session", session_then_settle)
    r = senv.human.post(f"/api/hosted-sessions/{sid}/children",
                        json={"message": "go", "request_id": "c1", "output_schema": SCHEMA})
    armed[0] = False
    assert r.status_code == 200 and done, r.text
    got = r.json()
    assert got["created"] is False
    assert got["message"]["output"]["state"] == "asked" and got["session"]["reply_for"] is None, \
        "the child and its message as of one moment: before the settlement"
    assert got["message"]["output"].get("reply") != hosted.REPLY_NONE
    again = senv.human.post(f"/api/hosted-sessions/{sid}/children",
                            json={"message": "go", "request_id": "c1", "output_schema": SCHEMA}).json()
    assert again["message"]["output"]["reply"] == hosted.REPLY_WHOLE_BLOCK and again["result_data"] == GOOD
    assert again["session"]["reply"] == text


def test_the_details_panel_reads_the_session_and_its_messages_in_one_snapshot(offenv, monkeypatch):
    """R6-01: the same for the dashboard's details (and its tree poll, which shares the wrapper)."""
    from agent_comms import hosted_lineage
    sid, t = asked_root(offenv)
    report(offenv, sid, {"kind": "delivered", "message": 1})
    real = hosted_lineage._messages
    done = []

    def messages_after_a_settlement(conn, session_id):
        if session_id == sid and not done:
            done.append(1)
            settle_from_another_connection(offenv, sid, reply_with(t))
        return real(conn, session_id)
    monkeypatch.setattr(hosted_lineage, "_messages", messages_after_a_settlement)
    d = offenv.human.get(f"/api/hosted-trees/{sid}").json()
    assert done
    out = d["messages"][0]["output"]
    assert out["state"] == "asked" and d.get("output") is None, "both reads before the settlement"
    monkeypatch.setattr(hosted_lineage, "_messages", real)
    after = offenv.human.get(f"/api/hosted-trees/{sid}").json()["messages"][0]["output"]
    assert (after["state"], after["reply"]) == ("accepted", hosted.REPLY_WHOLE_BLOCK)


def test_a_replaced_reply_kept_undelivered_says_the_parents_record_holds_it(senv, monkeypatch):
    """R6-02: with the parent's waiting results at their limit, the result is kept `undelivered`: neither delivery
    nor a claim takes it, so its label says the parent's result record holds the reply, not that it goes there."""
    from agent_comms import hosted_results as hr
    sid = root(senv)
    answer(senv, sid)
    kid = child(senv, sid, "go", output_schema=SCHEMA)["session_id"]
    tick(senv)
    monkeypatch.setattr(hr, "MAX_WAITING", 0)
    answer(senv, kid, reply_with(token_of(turns_of(senv, kid)[0])))
    [res] = results(senv, sid)
    assert res["state"] == "undelivered"
    monkeypatch.setattr(hr, "MAX_WAITING", 50)
    senv.human.post(f"/api/hosted-sessions/{kid}/messages", json={"message": "again"})
    tick(senv)
    answer(senv, kid, "a later reply")
    label = show(senv, kid)["messages"][0]["output"]["reply"]
    assert label == hosted.REPLY_COPY_KEPT and "goes to the parent" not in label
    assert "the parent's result record still holds it" in label
