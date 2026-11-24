"""The in-turn result check (DESIGN_NOTES "In-turn result check"): `board_session_check_result` checks a candidate
block against the stored schema of the open request its token names, with the settlement verdict itself, and saves
nothing as a result nor sends anything to the asker. Through the real hub routes, the real MCP server (in memory, over
a real stdio process, and over the remote bridge), with the node's reports posted to the real settlement entry point.
Never a real agent CLI."""

import json
import logging
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

import anyio
import pytest

from agent_comms import hosted, output_schema as o, providers, spawn_mcp
from agent_comms.mcp_server import build_mcp
from agent_comms.providers import REPLY_MAX_BYTES, REPLY_TRUNCATED, cap_reply

from conftest import PROJECT
from test_hosted_results import report
from test_hosted_spawn import a_root, bound, grant, kid_turns, spawn  # noqa: F401
from test_hosted_tree import senv  # noqa: F401
from test_structured_results import offenv, record  # noqa: F401
from test_hosted_sessions import tick

# A schema with a bound of every counted kind, so that count reasons are exercised on both paths.
CHECKED = {"type": "object", "required": ["verdict", "summary", "findings"], "additionalProperties": False,
           "properties": {"verdict": {"enum": ["ok", "changes"]},
                          "summary": {"type": "string", "minLength": 3, "maxLength": 20},
                          "findings": {"type": "array", "minItems": 1, "maxItems": 2, "items": {
                              "type": "object", "required": ["file", "line"],
                              "properties": {"file": {"type": "string"},
                                             "line": {"type": "integer", "minimum": 1}}}}}}
SCHEMA = json.dumps(CHECKED)
FITS = {"verdict": "changes", "summary": "off by one", "findings": [{"file": "src/pager.py", "line": 3}]}
PLANTED = "PLANTED-draft-7f3a91-over-twenty"


def blk(t: str, value=FITS, prose="Done.", payload: str | None = None) -> str:
    body = payload if payload is not None else json.dumps(value)
    return f"{prose}\n<<<result-data {t}>>>\n{body}\n<<<end result-data {t}>>>"


def asked_root(env, schema=SCHEMA) -> str:
    r = env.human.post("/api/hosted-sessions", json={"project": PROJECT, "message": "go", "output_schema": schema})
    assert r.status_code == 200, r.text
    return r.json()["session"]["session_id"]


def ask_again(env, sid, schema=SCHEMA) -> int:
    r = env.human.post(f"/api/hosted-sessions/{sid}/messages", json={"message": "again", "output_schema": schema})
    assert r.status_code == 200, r.text
    return r.json()["message"]["n"]


def tok(env, sid, n=1) -> str:
    return record(env, sid, n)["output"]["token"]


def settle(env, sid, n, text, ok=True) -> dict:
    """The node's report of the turn, through the real settlement entry point (the hub caps the text as it does any
    reply). Returns the message's closed request."""
    report(env, sid, {"kind": "delivered", "message": n}, {"kind": "turn_ended", "message": n, "ok": ok, "text": text})
    return record(env, sid, n)["output"]


def server(env):
    return build_mcp(env.board, "stdio")


def call(env, monkeypatch, block, who="claude", mcp=None) -> dict:
    """The tool, as a session's own stdio board server runs it (the agent's token in its environment)."""
    monkeypatch.setenv("AGENT_COMMS_TOKEN", env.tokens[who])
    mcp = mcp or server(env)

    async def go():
        from mcp import Client
        async with Client(mcp) as c:
            return await c.call_tool(o.CHECK_TOOL, {"block": block})
    r = anyio.run(go)
    assert not r.is_error, r.content
    return json.loads(r.content[0].text)


def dump(env) -> list[str]:
    """Every durable row, read through a connection of its own."""
    c = sqlite3.connect(env.settings.db_path)
    try:
        return list(c.iterdump())
    finally:
        c.close()


# ------------------------------------------------------------ the answer, and what it does not do


def test_a_fitting_block_is_accepted_saves_nothing_and_the_verdict_still_comes_from_the_reply(offenv, monkeypatch):
    sid = asked_root(offenv)
    t = tok(offenv, sid)
    before = dump(offenv)
    a = call(offenv, monkeypatch, blk(t))
    assert a == {"checked": True, "would_be": "accepted", "result_saved": False, "next": o.CHECK_FITS}
    assert set(a) <= set(o.CHECK_KEYS)
    bad = call(offenv, monkeypatch, blk(t, FITS | {"summary": "é" * 25}))
    assert bad["would_be"] == "missing" and bad["code"] == "shape"
    assert bad["reasons"] == ['at $.summary: longer than maxLength 20: it has 25 characters']
    assert bad["next"] == o.CHECK_MISSING.format(code="shape", words=o.CHECK_WORDS["shape"])
    assert dump(offenv) == before, "the board saved nothing: no result, no record, no audit post, no field"
    # The verdict is made from the reply alone: a session that checked and then left the block out is missing.
    out = settle(offenv, sid, 1, "Done, the block was checked.")
    assert out["state"] == "missing" and out["code"] == "no_block"


# ------------------------------------------------------------ parity with settlement (RC-05)


def corpus(t: str) -> list[str]:
    """Replies whose checks reach the verdict: every code a completed turn can get, at the grammar's edges."""
    from test_structured_results import adversarial_replies
    other = "0123456789abcdef" if t != "0123456789abcdef" else "fedcba9876543210"
    big = json.dumps(FITS | {"pad": "x" * o.BLOCK_MAX_BYTES})
    out = [
        blk(t), blk(t, prose=""), blk(t).replace("\n", "\r\n"), blk(t) + "\n\n  \t\n",
        blk(t, FITS | {"summary": "x"}), blk(t, FITS | {"findings": []}), blk(t, FITS | {"findings": [{}] * 3}),
        blk(t, FITS | {"verdict": "maybe", "extra": 1}), blk(t, FITS | {"summary": "\u2028" * 5}),
        blk(t, payload='{"verdict": "ok", "verdict": "ok"}'), blk(t, payload="NaN"), blk(t, payload="[1, 2]"),
        blk(t, payload=f"```json\n{json.dumps(FITS)}\n```"), blk(t, payload=big), blk(t, payload="1e999"),
        blk(t) + "\nafter", blk(t) + "\n" + blk(t), f"<<<end result-data {t}>>>\n" + blk(t),
        blk(other) + "\n" + blk(t), blk(t, payload=f"<<<result-data {other}>>>"),
        "é" * 9000 + blk(t), blk(t) + REPLY_TRUNCATED,   # (a text over 16,384 characters is not read: refused)
    ]
    out += [r for r, _ in adversarial_replies(t)]
    for i in range(12):        # generated instances: each property set, unset or wrong, in turn
        v = json.loads(json.dumps(FITS))
        k = ["verdict", "summary", "findings"][i % 3]
        v[k] = [None, 7, "y" * (i * 3), [{"file": i, "line": -i}], {"a": i}, True][i % 6]
        out.append(blk(t, v))
    return out


def test_the_check_is_the_settlement_verdict_on_the_same_complete_reply(offenv, monkeypatch):
    """For the SAME request and the complete reply of a successful turn attributed to it, the check's answer equals
    the verdict settlement records: state, code and reasons, for every case. The reply goes through the real
    settlement entry point (the node's report), which caps it as the provider and the hub do."""
    sid = asked_root(offenv)
    mcp = server(offenv)
    n, seen = 1, set()
    for reply in corpus("0" * 16):
        if n > 1:
            n = ask_again(offenv, sid)
        t = tok(offenv, sid, n)
        text = reply.replace("0" * 16, t)
        a = call(offenv, monkeypatch, text, mcp=mcp)
        v = settle(offenv, sid, n, text)
        assert a["checked"], (text[:200], a)
        assert (a["would_be"], a.get("code"), a.get("reasons") or []) == (v["state"], v["code"], v["reasons"] or []), \
            (text[:300], a, v)
        seen.add(v["code"])
        n += 1
    assert {None, "shape", "not_json", "not_object", "too_big", "form", "truncated"} <= seen, seen


def test_a_fault_inside_the_verdict_is_internal_for_the_check_and_for_settlement(offenv, monkeypatch):
    sid = asked_root(offenv)
    t = tok(offenv, sid)

    def boom(*a, **kw):
        raise RuntimeError(PLANTED)
    monkeypatch.setattr(o, "validate", boom)      # a stage INSIDE safe_verdict, so its catch is what is tested
    a = call(offenv, monkeypatch, blk(t))
    assert a["would_be"] == "missing" and a["code"] == "internal" and PLANTED not in json.dumps(a)
    assert settle(offenv, sid, 1, blk(t))["code"] == "internal"


def test_the_tool_runs_the_shared_settlement_function_on_the_capped_text(offenv, monkeypatch):
    sid = asked_root(offenv)
    t = tok(offenv, sid)
    seen = []
    real = o.safe_verdict

    def spy(output, **kw):
        seen.append(kw)
        return real(output, **kw)
    monkeypatch.setattr(o, "safe_verdict", spy)
    text = "é" * 9000 + blk(t)
    a = call(offenv, monkeypatch, text)
    assert seen == [{"ok": True, "code": None, "reply": cap_reply(text)}] and a["code"] == "truncated"
    assert o.CHECK_TEXT_MAX == providers.REPLY_MAX_BYTES


# ------------------------------------------------------------ refusals, before any verdict


def test_lookup_refusals_are_fixed_answers(offenv, monkeypatch):
    sid = asked_root(offenv)
    t = tok(offenv, sid)
    mcp = server(offenv)
    no_marker = o.not_checked(o.WHY_NO_MARKER)
    for text in (json.dumps(FITS), "  " + blk(t).replace("\n", "\n  "), f"x\n{json.dumps(FITS)}\n<<<end result-data {t}>>>",
                 f"inline <<<result-data {t}>>> {json.dumps(FITS)} <<<end result-data {t}>>>", "\ufeff" + blk(t, prose="").lstrip("\n")):
        assert call(offenv, monkeypatch, text, mcp=mcp) == no_marker, text
    assert call(offenv, monkeypatch, blk("abcdef0123456789"), mcp=mcp) == o.not_checked(o.WHY_NONE)
    assert call(offenv, monkeypatch, blk(t), who="codex", mcp=mcp) == o.not_checked(o.WHY_NONE), \
        "another agent's request: the same answer as an unknown token"
    settle(offenv, sid, 1, blk(t))
    assert call(offenv, monkeypatch, blk(t), mcp=mcp) == o.not_checked(o.WHY_CLOSED)


def test_a_text_over_the_character_bound_is_refused_before_anything_reads_it(offenv, monkeypatch):
    sid = asked_root(offenv)
    t = tok(offenv, sid)

    def never(*a, **kw):
        raise AssertionError("read")
    monkeypatch.setattr(o, "check_tokens", never)
    monkeypatch.setattr(hosted, "open_request", never)
    text = blk(t) + "x" * (o.CHECK_TEXT_MAX + 1 - len(blk(t)))
    assert call(offenv, monkeypatch, text) == o.not_checked(o.WHY_TOO_LONG)


def test_a_foreign_token_and_an_unknown_one_take_the_same_lookup_path(offenv, monkeypatch):
    """RC-04: another agent's messages are never in the token query; its token and an unknown token give the same
    answer from the same statement (SQL traced on the worker connection that runs it). No constant time is claimed."""
    sid = asked_root(offenv)
    t = tok(offenv, sid)
    traces = []
    real = hosted.open_request

    def traced(board, agent, tokens):
        sql = []
        board.conn.set_trace_callback(sql.append)
        try:
            out = real(board, agent, tokens)
        finally:
            board.conn.set_trace_callback(None)
        traces.append((out, [s.replace(tokens[0], "T") for s in sql]))
        return out
    monkeypatch.setattr(hosted, "open_request", traced)
    mcp = server(offenv)
    a = call(offenv, monkeypatch, blk(t), who="codex", mcp=mcp)
    b = call(offenv, monkeypatch, blk("abcdef0123456789"), who="codex", mcp=mcp)
    assert a == b and traces[0] == traces[1] and traces[0][0] == ("none", None), traces


def test_the_last_opening_line_names_the_request(offenv, monkeypatch):
    sid = asked_root(offenv)
    old = tok(offenv, sid)
    settle(offenv, sid, 1, blk(old))
    n = ask_again(offenv, sid)
    t = tok(offenv, sid, n)
    quoted = "Earlier I wrote:\n" + blk(old) + "\nand now:\n" + blk(t)
    assert call(offenv, monkeypatch, quoted)["would_be"] == "accepted", "the old block is quoted material"
    inside = blk(t, payload=f"<<<result-data {old}>>>")      # a later opening line, of a closed request
    assert call(offenv, monkeypatch, inside)["code"] == "not_json", "the OPEN request is the one checked"
    assert o.check_tokens(blk(old) + "\n" + blk(t)) == [t, old]
    assert o.check_tokens("  " + blk(t).replace("\n", "\n  ")) == []
    assert settle(offenv, sid, n, quoted)["state"] == "accepted"


def test_states_open_closed_and_none_from_the_stored_records(offenv, monkeypatch):
    sid = asked_root(offenv)
    t = tok(offenv, sid)
    assert hosted.open_request(offenv.board, "claude", [t])[0] == "open"
    key = hosted._msg_key(sid, 1)
    m = record(offenv, sid, 1)
    with offenv.board.conn:
        offenv.board.conn.execute("UPDATE board_state SET value = ? WHERE key = ?",
                                  (json.dumps(m | {"output": m["output"] | {"schema": None}}), key))
    assert hosted.open_request(offenv.board, "claude", [t]) == ("none", None), "asked but no schema: nothing to check"
    with offenv.board.conn:     # an older hub settled it (no verdict): the record says asked, the message done
        offenv.board.conn.execute("UPDATE board_state SET value = ? WHERE key = ?",
                                  (json.dumps(m | {"state": "done"}), key))
    assert hosted.open_request(offenv.board, "claude", [t]) == ("closed", None)
    with offenv.board.conn:
        offenv.board.conn.execute("UPDATE board_state SET value = ? WHERE key = ?", (json.dumps(m), key))
    assert hosted.open_request(offenv.board, "claude", [t])[0] == "open"
    assert hosted.open_request(offenv.board, "codex", [t]) == ("none", None)
    twin = hosted._msg_key(sid, 99)
    with offenv.board.conn:
        offenv.board.conn.execute("INSERT INTO board_state(key, value, updated_by, updated_at) VALUES (?, ?, 'x', 0)",
                                  (twin, json.dumps(m | {"n": 99})))
    assert hosted.open_request(offenv.board, "claude", [t]) == ("none", None), "two messages with one token"
    with offenv.board.conn:
        offenv.board.conn.execute("DELETE FROM board_state WHERE key = ?", (twin,))
    assert hosted.open_request(offenv.board, "claude", ["not a token"]) == ("none", None)


def test_rollback_with_an_outstanding_ask(offenv, monkeypatch):
    """RC-08: a request asked by this hub and still open is checked by a newer checker; once an older hub settles it
    (no verdict: `unchecked`), the check says closed."""
    sid = asked_root(offenv)
    t = tok(offenv, sid)
    assert call(offenv, monkeypatch, blk(t))["would_be"] == "accepted"
    monkeypatch.setattr(hosted, "_closed", lambda *a, **kw: None)       # main's _apply before results as data
    settle(offenv, sid, 1, blk(t))
    monkeypatch.undo()
    assert record(offenv, sid, 1)["output"]["state"] == "asked"
    assert call(offenv, monkeypatch, blk(t)) == o.not_checked(o.WHY_CLOSED)


# ------------------------------------------------------------ reads only; bounded lookup (RC-01, RC-02)


def test_the_check_reads_only_and_a_held_write_lock_does_not_delay_it(offenv, monkeypatch):
    sid = asked_root(offenv)
    t = tok(offenv, sid)
    statements = []
    real = hosted.open_request

    def traced(board, agent, tokens):
        board.conn.set_trace_callback(statements.append)
        try:
            return real(board, agent, tokens)
        finally:
            board.conn.set_trace_callback(None)
    monkeypatch.setattr(hosted, "open_request", traced)
    before = dump(offenv)
    other = sqlite3.connect(offenv.settings.db_path, isolation_level=None, timeout=30)
    other.execute("BEGIN IMMEDIATE")        # another writer holds the write lock throughout
    try:
        t0 = time.monotonic()
        a = call(offenv, monkeypatch, blk(t))
        assert a["would_be"] == "accepted" and time.monotonic() - t0 < 5
    finally:
        other.execute("ROLLBACK")
        other.close()
    assert statements and all(s.lstrip().upper().startswith(("SELECT", "PRAGMA")) for s in statements), statements
    assert dump(offenv) == before
    # The snapshot is released once the check exits: a checkpoint can move the whole WAL back.
    c = sqlite3.connect(offenv.settings.db_path)
    busy, _, _ = c.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    c.close()
    assert busy == 0


def test_the_lookup_has_a_work_budget_independent_of_retention(offenv, monkeypatch):
    sid = asked_root(offenv)
    t = tok(offenv, sid)
    monkeypatch.setattr(hosted, "LOOKUP_STEPS", 0)
    monkeypatch.setattr(hosted, "LOOKUP_EVERY", 1)
    assert hosted.open_request(offenv.board, "claude", [t]) == ("failed", None)
    assert call(offenv, monkeypatch, blk(t)) == o.not_checked(o.WHY_FAILED)
    monkeypatch.undo()
    assert hosted.open_request(offenv.board, "claude", [t])[0] == "open"
    assert offenv.board.conn.execute("PRAGMA busy_timeout").fetchone()[0] == 30000, "restored after the lookup"


def test_a_settlement_right_after_the_lookup_gives_a_stale_answer_that_changes_nothing(offenv, monkeypatch):
    """One statement is one snapshot: no settlement can fall between two reads. One that commits after it makes the
    answer stale (advice only); the verdict is the reply's, and the next check says closed."""
    sid = asked_root(offenv)
    t = tok(offenv, sid)
    real = hosted.open_request

    def then_settled(board, agent, tokens):
        out = real(board, agent, tokens)
        from agent_comms.core import Board
        other = Board(offenv.settings, clock=offenv.clock)
        hosted.report(other, other.authenticate(offenv.tokens["laptop"]), sid,
                      events=[{"kind": "turn_ended", "message": 1, "ok": True, "text": "no block"}])
        return out
    monkeypatch.setattr(hosted, "open_request", then_settled)
    assert call(offenv, monkeypatch, blk(t))["would_be"] == "accepted"
    monkeypatch.undo()
    assert record(offenv, sid, 1)["output"]["code"] == "no_block"
    assert call(offenv, monkeypatch, blk(t)) == o.not_checked(o.WHY_CLOSED)


# ------------------------------------------------------------ the bounds (RC-03)


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def test_the_rate_bound_reserves_atomically_and_expires():
    clock = Clock()
    b = spawn_mcp.CheckBounds(clock)
    assert all(b.rate("a") for _ in range(b.RATE)) and not b.rate("a") and b.rate("b")
    clock.t += b.RATE_SECONDS - 1
    assert not b.rate("a")
    clock.t += 1
    assert b.rate("a"), "a window older than RATE_SECONDS has expired"
    clock.t += b.RATE_SECONDS + 1
    b.rate("c")
    assert set(b.calls) == {"c"}, "expired windows are dropped from the map"
    part = spawn_mcp.CheckBounds(clock)
    for _ in range(30):
        part.rate("p")
    clock.t += 30
    for _ in range(30):
        part.rate("p")
    assert not part.rate("p")
    clock.t += 30
    assert part.rate("p"), "the calls older than the window no longer count, while the newer ones still do"
    c2 = spawn_mcp.CheckBounds(Clock())
    ok = []
    threads = [threading.Thread(target=lambda: ok.append(c2.rate("x"))) for _ in range(150)]
    [x.start() for x in threads]
    [x.join() for x in threads]
    assert ok.count(True) == c2.RATE, "simultaneous calls: exactly the bound is reserved"


def test_the_agent_map_and_the_request_map_are_bounded():
    clock = Clock()
    b = spawn_mcp.CheckBounds(clock)
    b.AGENTS, b.REQUESTS = 3, 3
    for name in "abcd":
        clock.t += 1
        b.rate(name)
    assert list(b.calls) == ["b", "c", "d"], "the oldest agent window is evicted"
    for r in ("r1", "r2", "r3", "r4"):
        b.count(r)
    assert list(b.counts) == ["r2", "r3", "r4"]
    one = spawn_mcp.CheckBounds(clock)
    assert all(one.count("req") for _ in range(o.CHECKS_PER_REQUEST)) and not one.count("req")


def test_concurrency_fails_fast():
    b = spawn_mcp.CheckBounds()
    assert all(b.enter() for _ in range(b.CONCURRENT)) and not b.enter()
    b.leave()
    assert b.enter()


def test_through_the_tool_the_request_count_the_rate_and_saturation(offenv, monkeypatch):
    sid = asked_root(offenv)
    t = tok(offenv, sid)
    mcp = server(offenv)
    clock = Clock()
    mcp.check_bounds.clock = clock
    for i in range(o.CHECKS_PER_REQUEST):          # different texts, one request: one count
        assert call(offenv, monkeypatch, blk(t, prose=f"Done {i}."), mcp=mcp)["checked"]
    assert call(offenv, monkeypatch, blk(t, prose="Once more."), mcp=mcp) == o.not_checked(o.WHY_LIMIT)
    settle(offenv, sid, 1, blk(t))
    n = ask_again(offenv, sid)
    assert call(offenv, monkeypatch, blk(tok(offenv, sid, n)), mcp=mcp)["checked"], "another request: its own count"
    t = tok(offenv, sid, n)
    clock.t += 1000
    for _ in range(mcp.check_bounds.RATE):
        call(offenv, monkeypatch, json.dumps(FITS), mcp=mcp)
    assert call(offenv, monkeypatch, blk(t), mcp=mcp) == o.not_checked(o.WHY_RATE.format(n=60))
    clock.t += 1000
    mcp.check_bounds.busy = mcp.check_bounds.CONCURRENT
    assert call(offenv, monkeypatch, blk(t), mcp=mcp) == o.not_checked(o.WHY_BUSY)
    mcp.check_bounds.busy = 0
    assert call(offenv, monkeypatch, blk(t), mcp=mcp)["checked"], "once no longer busy, the other request checks"


def test_the_validators_violation_work_is_capped():
    schema = {"type": "object", "properties": {"x": {"type": "array", "items": {
        "type": "object", "required": [f"f{i}" for i in range(50)],
        "properties": {f"f{i}": {} for i in range(50)}}}}}
    value = {"x": [{}] * 1000}         # 50,000 violations if the walk went on
    t0 = time.perf_counter()
    reasons = o.validate(schema, value)
    assert time.perf_counter() - t0 < 0.5
    assert len(reasons) == o.REASONS_MAX and reasons[-1] == f"(and at least {o.VIOLATIONS_MAX} more)"


# ------------------------------------------------------------ counts in reasons (RC-07)


def test_the_four_count_reasons_say_the_instances_count_and_never_a_value():
    s = {"type": "object", "properties": {
        "s": {"type": "string", "minLength": 4, "maxLength": 5},
        "a": {"type": "array", "minItems": 2, "maxItems": 3}}}
    assert o.validate(s, {"s": "éé"}) == ["at $.s: shorter than minLength 4: it has 2 characters"]
    assert o.validate(s, {"s": "é😀é😀é😀"}) == ["at $.s: longer than maxLength 5: it has 6 characters"], \
        "code points (6), not UTF-8 bytes (18)"
    assert o.validate(s, {"a": [1]}) == ["at $.a: fewer than minItems 2 items: it has 1"]
    assert o.validate(s, {"a": [1, 2, 3, 4]}) == ["at $.a: more than maxItems 3 items: it has 4"]
    secret = {"type": "object", "properties": {"n": {"type": "integer", "maximum": 3}, "e": {"enum": ["a", "b"]},
                                               "c": {"const": "k"}}}
    for r in o.validate(secret, {"n": 987654, "e": "zz-planted", "c": "yy-planted"}):
        assert "987654" not in r and "planted" not in r and "a" + '"' not in r


def test_count_reasons_keep_the_ascii_150_byte_and_ten_reason_bounds():
    names = {('q"\\' * 15) + f"{i:02d}": {"type": "string", "maxLength": 1} for i in range(14)}
    s = {"type": "object", "properties": {"o": {"type": "object", "properties": names}}}
    reasons = o.validate(s, {"o": {k: "é" * 8000 for k in names}})
    assert len(reasons) == o.REASONS_MAX and reasons[-1] == "(and 5 more)"
    assert all(r.isascii() and len(r.encode()) <= o.REASON_MAX and "\n" not in r for r in reasons), reasons
    assert all("it has 8000 characters" in r for r in reasons[:-1])
    assert len(o.result_line({"state": "missing", "code": "shape", "reasons": reasons}).encode()) <= 1700


def test_count_reasons_in_a_claim_stay_within_the_sdk_serialized_bound(senv):
    from mcp.server.mcpserver.utilities.func_metadata import _convert_to_content
    from agent_comms import hosted_results, hosted_spawn as hs
    from test_hosted_tree import answer
    names = {('q"\\' * 15) + f"{i:02d}": {"type": "string", "maxLength": 1} for i in range(14)}
    schema = {"type": "object", "properties": {"o": {"type": "object", "properties": names}}}
    sid = a_root(senv)
    grant(senv)
    kid = spawn(senv, bound(senv, sid), task="x", output_schema=json.dumps(schema))["child"]["id"]
    tick(senv)
    t = record(senv, kid, 1)["output"]["token"]
    payload = json.dumps({"o": {k: "\u00e9" * 60 for k in names}})       # 60 code points each, 6 bytes escaped
    assert len(payload.encode()) <= o.BLOCK_MAX_BYTES
    answer(senv, kid, blk(t, payload=payload))
    x = hosted_results.claim(senv.board, sid, "claude", child=kid, message=1)
    claimed = hs.taken(x)
    out = claimed["result"]["output"]
    assert out["state"] == "missing" and out["code"] == "shape" and len(out["reasons"]) == o.REASONS_MAX, out
    assert all("it has 60 characters" in r for r in out["reasons"][:-1]) and out["reasons"][-1] == "(and 5 more)"
    plain = hs.taken({k: v for k, v in x.items() if k != "output"})
    text = _convert_to_content(claimed)[0].text
    assert "longer than maxLength 1: it has 60 characters" in text, "a count reason in the serialized claim"
    size = len(text.encode())
    base = len(_convert_to_content(plain)[0].text.encode())
    assert 0 < size - base <= 7000, size - base


# ------------------------------------------------------------ nothing of the block leaves (RC-02)


def test_no_path_logs_or_returns_the_draft_in_process_and_over_the_remote_bridge(offenv, monkeypatch, caplog):
    from fastapi.testclient import TestClient
    from agent_comms import remote
    caplog.set_level(logging.INFO)                  # the hub's and the SDK's own lines
    caplog.set_level(logging.DEBUG, logger="agent_comms")
    sid = asked_root(offenv)
    t = tok(offenv, sid)
    draft = FITS | {"summary": PLANTED, PLANTED: PLANTED}
    answers = [call(offenv, monkeypatch, blk(t, draft))]                      # missing (shape)
    answers.append(call(offenv, monkeypatch, blk(t, payload='{"' + PLANTED + '": 1, "' + PLANTED + '": 2}')))

    def boom(*a, **kw):
        raise RuntimeError(PLANTED)
    monkeypatch.setattr(hosted, "open_request", boom)
    answers.append(call(offenv, monkeypatch, blk(t, draft)))                  # an injected fault
    assert answers[-1] == o.not_checked(o.WHY_FAILED)
    with TestClient(offenv.app, base_url="http://127.0.0.1:8787") as tc:      # the bridge to the hub's HTTP MCP
        out = []
        b = remote.Bridge("http://127.0.0.1:8787", offenv.tokens["claude"], out.append, client=tc)
        b.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                  "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                             "clientInfo": {"name": "test", "version": "1"}}})
        b.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
        for i, args in enumerate(({"block": blk(t, draft)}, {"block": {PLANTED: PLANTED}}, {"block": [PLANTED]}), 2):
            b.handle({"jsonrpc": "2.0", "id": i, "method": "tools/call",
                      "params": {"name": o.CHECK_TOOL, "arguments": args}})
        monkeypatch.undo()
        b.handle({"jsonrpc": "2.0", "id": 9, "method": "tools/call",
                  "params": {"name": o.CHECK_TOOL, "arguments": {"block": blk(t, draft)}}})
    bridged = [json.loads(x["result"]["content"][0]["text"]) for x in out if x.get("id") in (2, 9)]
    assert bridged[0] == o.not_checked(o.WHY_FAILED) and bridged[1]["code"] == "shape"
    assert all(PLANTED not in json.dumps(a) for a in answers + bridged)
    assert PLANTED not in caplog.text, [r.getMessage() for r in caplog.records if PLANTED in r.getMessage()]


def test_over_a_real_stdio_server_with_child_sessions_off(offenv, tmp_path):
    """A real stdio board server in its own process, on the hub's database (a human root, spawning off): the check,
    a bad argument and a non-JSON block, and nothing of the draft on its stderr."""
    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client
    sid = asked_root(offenv)
    t = tok(offenv, sid)
    code = ("import json, sys, logging\n"
            "logging.basicConfig(level=logging.DEBUG)\n"
            "from pathlib import Path\n"
            "from agent_comms.config import Settings\n"
            "from agent_comms.core import Board\n"
            "from agent_comms.mcp_server import build_mcp\n"
            "s = Settings(db_path=Path(sys.argv[1]), agents_path=Path(sys.argv[2]))\n"
            "s.dispatch = json.loads(sys.argv[3])\n"
            "build_mcp(Board(s), 'stdio').run('stdio')\n")
    root_dir = Path(__file__).resolve().parents[1]
    err = tmp_path / "stderr.txt"
    params = StdioServerParameters(command=sys.executable, args=["-c", code, str(offenv.settings.db_path),
                                                                 str(offenv.settings.agents_path),
                                                                 json.dumps(offenv.board.s.dispatch)],
                                   env={"PYTHONPATH": str(root_dir), "PATH": "/usr/bin:/bin", "HOME": str(tmp_path),
                                        "AGENT_COMMS_TOKEN": offenv.tokens["claude"]})
    draft = FITS | {"summary": PLANTED}

    async def calls():
        with err.open("w") as e:
            async with stdio_client(params, errlog=e) as (r, w):
                async with ClientSession(r, w) as s:
                    await s.initialize()
                    tools = {x.name for x in (await s.list_tools()).tools}
                    out = []
                    for block in (blk(t), blk(t, draft), {PLANTED: 1}, blk(t, payload=PLANTED)):
                        res = await s.call_tool(o.CHECK_TOOL, {"block": block})
                        out.append((bool(res.is_error), res.content[0].text if res.content else ""))
                    return tools, out
    tools, got = anyio.run(calls)
    assert o.CHECK_TOOL in tools
    assert json.loads(got[0][1])["would_be"] == "accepted"
    assert json.loads(got[1][1])["code"] == "shape" and json.loads(got[3][1])["code"] == "not_json"
    assert got[2][0], "a non-string block is the SDK's own argument error, to its caller"
    assert PLANTED not in err.read_text()


def test_an_older_server_lists_no_check_tool(tmp_path):
    """RC-08: main's own MCP server (exported from git) has no check tool: a session with it sees none, and the
    frame's sentence is conditional ("If you have no such tool or cannot call it, skip this step")."""
    root_dir = Path(__file__).resolve().parents[1]
    main = "e923bd4"
    if subprocess.run(["git", "-C", str(root_dir), "cat-file", "-e", main + "^{commit}"],
                      capture_output=True).returncode != 0:
        pytest.skip(f"main's commit {main} is not in this clone")
    old = tmp_path / "main"
    old.mkdir()
    archive = subprocess.run(["git", "-C", str(root_dir), "archive", main, "agent_comms"], capture_output=True,
                             check=True).stdout
    subprocess.run(["tar", "-x", "-C", str(old)], input=archive, check=True)
    code = ("import asyncio\n"
            "from pathlib import Path\n"
            "from agent_comms.config import Settings\n"
            "from agent_comms.core import Board\n"
            "from agent_comms.mcp_server import build_mcp\n"
            f"s = Settings(db_path=Path({str(tmp_path / 'b.db')!r}), agents_path=Path({str(tmp_path / 'a.toml')!r}))\n"
            "print(sorted(t.name for t in asyncio.run(build_mcp(Board(s), 'stdio').list_tools())))\n")
    r = subprocess.run([sys.executable, "-c", code], cwd=str(old), capture_output=True, text=True, timeout=120,
                       env={"PYTHONPATH": str(old), "PATH": "/usr/bin:/bin", "HOME": str(tmp_path)})
    assert r.returncode == 0, r.stderr[-2000:]
    assert "board_session_spawn" in r.stdout and o.CHECK_TOOL not in r.stdout
    assert o.TASK_ASK_CHECK.count("If you have no such tool or cannot call it, skip this step") == 1
    assert o.TASK_ASK_CHECK.count("you need not say in your reply that you skipped it") == 1
    assert "whatever it says" not in o.TASK_ASK_CHECK, "nothing that could override the truthful-result rule"
    assert o.TASK_ASK_CHECK.count("search for it by that name with ToolSearch") == 1
    assert "mcp__" not in o.TASK_ASK_CHECK + o.HUMAN_ASK_CHECK, "no server name the hub cannot know"
    for ask in (o.TASK_ASK_CHECK, o.HUMAN_ASK_CHECK):        # the check sentence before the truthful-result one
        assert ask.index("it never replaces the block") < ask.index("Put only what is true in it")


# ------------------------------------------------------------ the frame (RC-06)


def test_the_sentence_is_in_asked_frames_for_claude_sessions_only_and_never_without_a_schema(senv):
    r = senv.human.post("/api/hosted-sessions", json={"project": PROJECT, "message": "go", "output_schema": SCHEMA})
    sid = r.json()["session"]["session_id"]
    text = record(senv, sid, 1)["text"]
    t = tok(senv, sid)
    assert text.endswith(o.human_block(t, o.prepare_schema(SCHEMA)[1], True)), "the human's root start"
    tick(senv)
    from test_hosted_tree import answer
    answer(senv, sid)
    n = ask_again(senv, sid)
    assert o.HUMAN_ASK_CHECK.format(t=tok(senv, sid, n)) in record(senv, sid, n)["text"], "the human's send"
    plain = senv.human.post(f"/api/hosted-sessions/{sid}/messages", json={"message": "plain"}).json()["message"]["n"]
    assert o.CHECK_TOOL not in record(senv, sid, plain)["text"]
    c = senv.human.post(f"/api/hosted-sessions/{sid}/children", json={"message": "child", "output_schema": SCHEMA})
    assert c.status_code == 200, c.text
    kid = c.json()["session"]["session_id"]
    assert o.HUMAN_ASK_CHECK.format(t=tok(senv, kid)) in record(senv, kid, 1)["text"], "the human's child start"
    grant(senv, agents=("claude",))
    out = spawn(senv, bound(senv, sid), task="Review", output_schema=SCHEMA)
    k2 = out["child"]["id"]
    assert o.TASK_ASK_CHECK.format(t=tok(senv, k2)) in record(senv, k2, 1)["text"], "the spawn tool"
    from agent_comms import hosted_spawn as hs
    tick(senv)
    answer(senv, k2)
    sent = hs.send(senv.board, senv.p["claude"], bound(senv, sid), k2, message="again", output_schema=SCHEMA)
    n2 = sent["message"]["n"]
    assert o.TASK_ASK_CHECK.format(t=tok(senv, k2, n2)) in record(senv, k2, n2)["text"], "the send tool"
    cx = spawn(senv, bound(senv, sid), agent="codex", task="Review", output_schema=SCHEMA)["child"]["id"]
    text = record(senv, cx, 1)["text"]
    assert o.TASK_ASK.format(t=tok(senv, cx)) in text and o.CHECK_TOOL not in text, "a Codex session: no sentence"


def test_the_runtime_is_a_hint_a_known_codex_runner_and_pools(offenv):
    b = offenv.board
    assert hosted.tells_check(b, "claude", "claude-code") and not hosted.tells_check(b, "codex", "codex-cli")
    assert not hosted.tells_check(b, "x", None)
    b.s.dispatch = dict(b.s.dispatch) | {"runners": {"claude": ["codex", "exec", "{prompt}"]}}
    assert not hosted.tells_check(b, "claude", "claude-code"), "the hub's own runner for the agent is Codex's"
    assert hosted.check_hint(b, "codex") is False and hosted.check_hint(b, "no-such-agent") is False


def test_the_sentence_is_left_out_where_it_alone_would_not_fit(offenv):
    shown = o.prepare_schema(SCHEMA)[1]
    probe = o.human_block("0" * 16, shown, False)
    room = hosted.MESSAGE_MAX_BYTES - len(probe.encode())
    text = "x" * (room - 10)        # fits without the sentence, not with it
    output, full, why = hosted.ask(text, SCHEMA, "human", check=True)
    assert why is None and o.CHECK_TOOL not in full and len(full.encode()) <= hosted.MESSAGE_MAX_BYTES
    output, full, why = hosted.ask("short", SCHEMA, "human", check=True)
    assert o.CHECK_TOOL in full
    from agent_comms import hosted_spawn as hs
    small = '{"type": "object"}'

    def frame(task, check):
        return hosted.ask(task, small, "parent", check=check, frame=lambda token, sh, c: hs._frame(
            "session x", task, "", [], schema=sh, token=token, targets=None, check=c))
    _, plain, _ = frame("y", False)
    room = hosted.MESSAGE_MAX_BYTES - len(plain.encode())
    task = "y" * (1 + room - 100)                           # fits without the sentence, not with it
    out, full, why = frame(task, True)
    assert why is None and o.CHECK_TOOL not in full and len(full.encode()) <= hosted.MESSAGE_MAX_BYTES
    out, full, why = frame("y" * (1 + room - 1000), True)
    assert why is None and o.CHECK_TOOL in full and len(full.encode()) <= hosted.MESSAGE_MAX_BYTES
    shown = o.prepare_schema(small)[1]
    tight = "y" * (1 + room - 100)
    direct = hs._frame("session x", tight, "", [], schema=shown, token="a" * 16, check=True)
    assert direct == hs._frame("session x", tight, "", [], schema=shown, token="a" * 16, check=False), \
        "the agent path itself leaves the sentence out where it does not fit"


def test_the_block_is_taken_as_text_exactly(offenv, monkeypatch):
    """`block: str` exactly: the SDK pre-parses any other annotation's JSON-looking string, which would collapse a
    duplicate key before the strict decoder sees it. A block passed as bare JSON text stays text (not checked: no
    marker), and a duplicate-key payload reaches the decoder as written (not_json)."""
    tool = server(offenv)._tool_manager.get_tool(o.CHECK_TOOL)
    assert tool.fn_metadata.arg_model.model_fields["block"].annotation is str
    dup = '{"verdict": "ok", "verdict": "changes"}'
    assert tool.fn_metadata.pre_parse_json({"block": dup})["block"] == dup
    sid = asked_root(offenv)
    t = tok(offenv, sid)
    assert call(offenv, monkeypatch, blk(t, payload=dup))["code"] == "not_json"


def test_the_check_needs_no_spawn_gate_grant_or_binding(offenv, monkeypatch):
    """Child sessions off, no grant, no binding: the check works for the asked session's agent (the human root)."""
    from agent_comms import hosted_spawn as hs
    assert not hs.enabled(offenv.board)
    monkeypatch.delenv(hs.HOSTED_ENV, raising=False)
    sid = asked_root(offenv)
    assert call(offenv, monkeypatch, blk(tok(offenv, sid)))["would_be"] == "accepted"


# ------------------------------------------------------------ code review round 1 (R2-01 to R2-08)


def test_admission_comes_before_any_worker_and_a_fifth_call_is_busy_at_once(offenv, monkeypatch):
    """R2-01: four checks paused INSIDE authentication hold the four admissions; a fifth is answered busy at once,
    with no worker taken, and unrelated worker work still runs. Authentication errors stay the board's own."""
    sid = asked_root(offenv)
    t = tok(offenv, sid)
    monkeypatch.setenv("AGENT_COMMS_TOKEN", offenv.tokens["claude"])
    mcp = server(offenv)
    real = offenv.board.authenticate
    gate, inside = threading.Event(), []

    def slow(token):
        inside.append(1)
        gate.wait(20)
        return real(token)
    monkeypatch.setattr(offenv.board, "authenticate", slow)

    async def go():
        from mcp import Client
        results = []
        async with Client(mcp) as c:
            async def one():
                r = await c.call_tool(o.CHECK_TOOL, {"block": blk(t)})
                results.append(json.loads(r.content[0].text))
            async with anyio.create_task_group() as tg:
                for _ in range(mcp.check_bounds.CONCURRENT):
                    tg.start_soon(one)
                with anyio.fail_after(10):
                    while len(inside) < mcp.check_bounds.CONCURRENT:
                        await anyio.sleep(0.01)
                shared = anyio.to_thread.current_default_thread_limiter().borrowed_tokens
                with anyio.fail_after(2):
                    fifth = json.loads((await c.call_tool(o.CHECK_TOOL, {"block": blk(t)})).content[0].text)
                    other = await anyio.to_thread.run_sync(lambda: "ran")     # AnyIO's shared limiter is free
                gate.set()
        return fifth, other, results, shared
    fifth, other, results, shared = anyio.run(go)
    assert shared == 0, "the paused checks hold this tool's own limiter, not AnyIO's shared one"
    assert fifth == o.not_checked(o.WHY_BUSY) and other == "ran" and len(inside) == mcp.check_bounds.CONCURRENT
    assert all(r["would_be"] == "accepted" for r in results) and len(results) == 4
    monkeypatch.setattr(offenv.board, "authenticate", real)
    monkeypatch.setenv("AGENT_COMMS_TOKEN", "not-a-token")

    async def bad():
        from mcp import Client
        async with Client(server(offenv)) as c:
            return await c.call_tool(o.CHECK_TOOL, {"block": blk(t)})
    r = anyio.run(bad)
    assert r.is_error and "unauthorized" in r.content[0].text, "authentication errors are the board's own"


def test_two_open_requests_named_in_one_text_are_ambiguous(offenv, monkeypatch):
    """R2-02: A in flight and B open for the same agent. B's opening marker inside A's payload, or a complete B block
    after a complete A block: two open requests are named, so nothing is checked and the answer asks for the block
    with this turn's token alone. One open candidate (the other closed) is still checked."""
    a_sid = asked_root(offenv)
    a = tok(offenv, a_sid)
    n = ask_again(offenv, a_sid)
    b = tok(offenv, a_sid, n)
    inside = blk(a, payload=f"<<<result-data {b}>>>")
    both = blk(a) + "\n" + blk(b)
    ambiguous = o.not_checked(o.WHY_AMBIGUOUS)
    assert call(offenv, monkeypatch, inside) == ambiguous
    assert call(offenv, monkeypatch, both) == ambiguous
    assert hosted.open_request(offenv.board, "claude", [b, a]) == ("ambiguous", None)
    assert settle(offenv, a_sid, 1, inside)["code"] == "not_json" and record(offenv, a_sid, 1)
    assert call(offenv, monkeypatch, blk(a) + "\n" + blk(b))["would_be"] == "accepted", "A closed: B alone is open"


def test_a_saturated_lookup_fails_closed(offenv, monkeypatch):
    """R2-03: as many matching rows as the statement's cap is indeterminate (a row beyond it could change the answer):
    the fixed lookup-failed answer, never an open request."""
    sid = asked_root(offenv)
    t = tok(offenv, sid)
    m = record(offenv, sid, 1)
    other = "abcdef0123456789"
    with offenv.board.conn:
        for i in range(100, 100 + hosted.LOOKUP_ROWS):
            offenv.board.conn.execute(
                "INSERT INTO board_state(key, value, updated_by, updated_at) VALUES (?, ?, 'x', 0)",
                (hosted._msg_key(sid, i), json.dumps(m | {"n": i, "output": m["output"] | {"token": other}})))
    assert hosted.open_request(offenv.board, "claude", [t, other]) == ("failed", None)
    assert hosted.open_request(offenv.board, "claude", [t])[0] == "open", "the one request alone is not saturated"


def test_the_lookup_restores_the_connections_own_busy_timeout_and_its_deadline_fails(offenv, monkeypatch):
    sid = asked_root(offenv)
    t = tok(offenv, sid)
    c = offenv.board.conn
    c.execute("PRAGMA busy_timeout=913")
    assert hosted.open_request(offenv.board, "claude", [t])[0] == "open"
    assert c.execute("PRAGMA busy_timeout").fetchone()[0] == 913, "the prior timeout, not a default"
    monkeypatch.setattr(hosted, "LOOKUP_SECONDS", -1.0)        # the time budget alone, the step budget untouched
    monkeypatch.setattr(hosted, "LOOKUP_EVERY", 1)
    assert hosted.open_request(offenv.board, "claude", [t]) == ("failed", None)
    assert c.execute("PRAGMA busy_timeout").fetchone()[0] == 913
    c.execute("PRAGMA busy_timeout=30000")


def test_a_writer_commits_and_a_checkpoint_completes_while_validation_is_paused(offenv, monkeypatch):
    """R2-03: the lookup's snapshot is released before validation: while the check is paused inside validation,
    another connection commits a write and a TRUNCATE checkpoint moves the whole WAL back."""
    sid = asked_root(offenv)
    t = tok(offenv, sid)
    real = o.checked
    seen = {}

    def paused(request, text):
        w = sqlite3.connect(offenv.settings.db_path, timeout=5)
        w.execute("INSERT INTO board_state(key, value, updated_by, updated_at) VALUES ('e2e.writer', '1', 'x', 0)")
        w.commit()
        seen["checkpoint"] = w.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        w.execute("DELETE FROM board_state WHERE key = 'e2e.writer'")
        w.commit()
        w.close()
        return real(request, text)
    monkeypatch.setattr(o, "checked", paused)
    assert call(offenv, monkeypatch, blk(t))["would_be"] == "accepted"
    assert seen["checkpoint"][0] == 0, seen


def test_a_human_principal_gets_none_before_any_lookup_even_with_a_matching_row(offenv, monkeypatch):
    """R2-04: a retained session whose agent name is the human's (a name reclassified by an identity refresh):
    the human principal still gets the fixed none answer, and no lookup is made."""
    sid = asked_root(offenv)
    t = tok(offenv, sid)
    key = hosted.SESSION_PREFIX + sid
    r = json.loads(offenv.board.conn.execute("SELECT value FROM board_state WHERE key = ?", (key,)).fetchone()[0])
    with offenv.board.conn:
        offenv.board.conn.execute("UPDATE board_state SET value = ? WHERE key = ?", (json.dumps(r | {"agent": "human"}),
                                                                                     key))
    assert hosted.open_request(offenv.board, "human", [t])[0] == "open", "the row matches the human's name"

    def never(*a, **kw):
        raise AssertionError("looked up")
    monkeypatch.setattr(hosted, "open_request", never)
    assert call(offenv, monkeypatch, blk(t), who="human") == o.not_checked(o.WHY_NONE)


def test_a_pool_of_two_claude_members_one_mapped_to_codex_gets_no_sentence(offenv):
    """R3-03: two Claude-labelled members, only the second mapped by the hub to a Codex runner: the pool's root frame
    has no hint, though the member chosen (the first) is a Claude one. Every member must qualify, not any."""
    from agent_comms.config import create_agent
    b = offenv.board
    create_agent(offenv.settings.agents_path, "claude2", "claude-code")
    b.sync_agents(force=True)
    b.s.dispatch = json.loads(json.dumps(b.s.dispatch)) | {
        "sessions": {"spawn": {"pools": {"cpool": ["claude", "claude2"]}}},
        "runners": {"claude2": ["codex", "exec", "{prompt}"]}}
    assert hosted.tells_check(b, "claude", "claude-code") and not hosted.tells_check(b, "claude2", "claude-code")
    assert not hosted.check_hint(b, "cpool")
    r = offenv.human.post("/api/hosted-sessions", json={"project": PROJECT, "message": "go", "agent": "cpool",
                                                        "output_schema": SCHEMA})
    assert r.status_code == 200, r.text
    assert r.json()["session"]["agent"] == "claude", "the first member, a Claude one, was chosen"
    assert o.CHECK_TOOL not in record(offenv, r.json()["session"]["session_id"], 1)["text"]
    b.s.dispatch = b.s.dispatch | {"runners": {}}
    assert hosted.check_hint(b, "cpool"), "both members Claude: the pool qualifies"


def test_a_pool_gets_the_sentence_only_when_every_member_would(offenv):
    """R2-06: a real pool start. An all-Claude pool's root frame recommends the check; the same pool once the hub
    maps its Claude-labelled member to a Codex runner does not."""
    b = offenv.board
    b.s.dispatch = json.loads(json.dumps(b.s.dispatch)) | {"sessions": {"spawn": {"pools": {"cpool": ["claude"]}}}}
    assert hosted.check_hint(b, "cpool")
    r = offenv.human.post("/api/hosted-sessions", json={"project": PROJECT, "message": "go", "agent": "cpool",
                                                        "output_schema": SCHEMA})
    assert r.status_code == 200, r.text
    sid = r.json()["session"]["session_id"]
    assert r.json()["session"]["agent"] == "claude" and o.CHECK_TOOL in record(offenv, sid, 1)["text"]
    offenv.human.post(f"/api/hosted-sessions/{sid}/close")
    report(offenv, sid, {"kind": "ended", "message": None, "detail": "closed"})
    b.s.dispatch = b.s.dispatch | {"runners": {"claude": ["codex", "exec", "{prompt}"]}}
    assert not hosted.check_hint(b, "cpool")
    r = offenv.human.post("/api/hosted-sessions", json={"project": PROJECT, "message": "go", "agent": "cpool",
                                                        "output_schema": SCHEMA})
    assert r.status_code == 200, r.text
    text = record(offenv, r.json()["session"]["session_id"], 1)["text"]
    assert o.CHECK_TOOL not in text and o.HUMAN_ASK.format(t=o.TOKEN_RE.search(text.split("<<<output-schema ")[1])
                                                           .group(0)) in text


def test_a_rollback_to_main_with_an_outstanding_ask_still_settles_it(offenv, monkeypatch, tmp_path):
    """R2-07: this hub asks; main's own code (e923bd4, results as data without the check, exported from git) settles
    the outstanding message from its report with a verdict, and the check then says closed."""
    root_dir = Path(__file__).resolve().parents[1]
    main = "e923bd4"
    if subprocess.run(["git", "-C", str(root_dir), "cat-file", "-e", main + "^{commit}"],
                      capture_output=True).returncode != 0:
        pytest.skip(f"main's commit {main} is not in this clone")
    sid = asked_root(offenv)
    t = tok(offenv, sid)
    assert call(offenv, monkeypatch, blk(t))["would_be"] == "accepted"
    old = tmp_path / "main"
    old.mkdir()
    archive = subprocess.run(["git", "-C", str(root_dir), "archive", main, "agent_comms"], capture_output=True,
                             check=True).stdout
    subprocess.run(["tar", "-x", "-C", str(old)], input=archive, check=True)
    code = ("import json, sys\n"
            "from pathlib import Path\n"
            "from agent_comms import hosted\n"
            "from agent_comms.config import Settings\n"
            "from agent_comms.core import Board\n"
            "s = Settings(db_path=Path(sys.argv[1]), agents_path=Path(sys.argv[2]))\n"
            "s.dispatch = json.loads(sys.argv[3])\n"
            "b = Board(s)\n"
            "hosted.report(b, b.authenticate(sys.argv[4]), sys.argv[5], events=[{'kind': 'delivered', 'message': 1},"
            " {'kind': 'turn_ended', 'message': 1, 'ok': True, 'text': sys.argv[6]}])\n")
    r = subprocess.run([sys.executable, "-c", code, str(offenv.settings.db_path), str(offenv.settings.agents_path),
                        json.dumps(offenv.board.s.dispatch), offenv.tokens["laptop"], sid, blk(t)], cwd=str(old),
                       capture_output=True, text=True, timeout=120,
                       env={"PYTHONPATH": str(old), "PATH": "/usr/bin:/bin", "HOME": str(tmp_path)})
    assert r.returncode == 0, r.stderr[-2000:]
    assert record(offenv, sid, 1)["output"]["state"] == "accepted", "main settled it, with its own verdict"
    assert call(offenv, monkeypatch, blk(t)) == o.not_checked(o.WHY_CLOSED)


def test_a_malformed_raw_stdio_line_is_logged_without_its_payload(offenv, tmp_path):
    """R2-08: the SDK logs a malformed stdio envelope's validation error at DEBUG before any handler; the board's
    filter keeps the line and drops the payload."""
    code = ("import json, sys, logging\n"
            "logging.basicConfig(level=logging.DEBUG)\n"
            "from pathlib import Path\n"
            "from agent_comms.config import Settings\n"
            "from agent_comms.core import Board\n"
            "from agent_comms.mcp_server import build_mcp\n"
            "s = Settings(db_path=Path(sys.argv[1]), agents_path=Path(sys.argv[2]))\n"
            "build_mcp(Board(s), 'stdio').run('stdio')\n")
    root_dir = Path(__file__).resolve().parents[1]
    line = json.dumps({"jsonrpc": "2.0", "id": 7, "method": 5, "params": {"name": o.CHECK_TOOL,
                                                                         "arguments": {"block": PLANTED}}})
    # Ten malformed frames before any request (R3-02): the SDK's runner keeps eight for replay and drops the rest
    # with a DEBUG line naming the frame; the dispatcher logs each one it is handed.
    r = subprocess.run([sys.executable, "-c", code, str(offenv.settings.db_path), str(offenv.settings.agents_path)],
                       input=(line + "\n") * 10, capture_output=True, text=True, timeout=60,
                       env={"PYTHONPATH": str(root_dir), "PATH": "/usr/bin:/bin", "HOME": str(tmp_path),
                            "AGENT_COMMS_TOKEN": offenv.tokens["claude"]})
    assert "transport yielded exception: ValidationError" in r.stderr, r.stderr[-1500:]
    assert "dropped a frame received before the first request: ValidationError" in r.stderr, r.stderr[-1500:]
    assert PLANTED not in r.stderr and PLANTED not in r.stdout


# ------------------------------------------------------------ code review round 2 (R3-01)


def test_a_fifth_opening_token_is_refused_before_any_lookup(offenv, monkeypatch):
    """R3-01: A delivered and B queued for the same agent; a reply with blocks A, U1, U2, U3, B (U unknown). A four-
    token cut would drop A and accept B while A's settlement says `form`. More than four distinct opening tokens are
    refused with a fixed answer, and no lookup is made."""
    sid = asked_root(offenv)
    a = tok(offenv, sid)
    n = ask_again(offenv, sid)
    b = tok(offenv, sid, n)
    unknown = ["1111111111111111", "2222222222222222", "3333333333333333"]
    reply = "\n".join(blk(x) for x in [a, *unknown, b])
    assert len(o.check_tokens(reply)) == 5 and o.check_tokens(reply)[0] == b
    seen = []
    real = hosted.open_request

    def spy(board, agent, tokens):
        seen.append(tokens)
        return real(board, agent, tokens)
    monkeypatch.setattr(hosted, "open_request", spy)
    assert call(offenv, monkeypatch, reply) == o.not_checked(o.WHY_TOO_MANY)
    assert seen == [], "refused before any lookup"
    four = "\n".join(blk(x) for x in [*unknown, a])
    assert call(offenv, monkeypatch, four)["would_be"] == "accepted" and seen, \
        "four tokens are still looked up (the unknown ones' blocks are quoted material, as at settlement)"
    monkeypatch.undo()
    assert settle(offenv, sid, 1, reply)["code"] == "form", "what A's settlement says of that reply"
