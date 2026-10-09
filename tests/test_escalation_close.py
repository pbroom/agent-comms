"""Automatic-recovery escalations close by themselves when their stall clears (DESIGN_NOTES "Triage agent", part 4):
the dispatcher writes an attention resolution with fixed server text in the human identity's name, under the human's
auto_recover_stalled_work setting, and reopens it if the stall comes back. Never spawns a real agent CLI."""

import pytest

from agent_comms import autorecover, db, requests
from test_autorecover import LEASE, PAST_GRACE, abandon, aenv, agent_task, auto_posts, needs_you, records  # noqa: F401
from test_recovery_wait import blocked, blocked_handoff, wait_of


def escalated_unclaimed(env, n=1):
    """n unclaimed tasks codex created (the human accepted them), escalated to the human in one question."""
    tasks = [agent_task(env) for _ in range(n)]
    env.clock.advance(autorecover.UNCLAIMED_AFTER_SECONDS + 60)
    env.d.tick()
    env.spawner.children[0].code = 0
    env.clock.advance(LEASE + 60)
    env.d.tick()
    env.d.tick()
    note = auto_posts(env)[-1]
    assert note["type"] == "question" and note["id"] in needs_you(env)
    assert {r["escalation_post_id"] for r in records(env).values()} == {note["id"]}
    return tasks, note


def resolution(env, post_id):
    return env.board.get_post(env.p["human"], post_id).get("attention_resolution")


def test_an_unclaimed_escalation_closes_when_the_task_is_claimed_and_reopens_if_it_is_unclaimed_again(aenv):
    """Live #602: "Task 23 unclaimed" stayed in Needs you after codex claimed task 23."""
    [task], note = escalated_unclaimed(aenv)
    posts_before = len(aenv.board.list_posts(aenv.p["human"], aenv.tid)["posts"])
    sid = aenv.session("codex")
    aenv.board.claim_task(aenv.p["codex"], sid, task)
    aenv.d.tick()
    assert note["id"] not in needs_you(aenv)
    res = resolution(aenv, note["id"])
    assert res["automatic"] is True and res["resolved_by"] == "human" and res["evidence_post_ids"] == []
    assert res["reason"].startswith("Closed automatically by the dispatcher under your board setting "
                                    "auto_recover_stalled_work (not your click)")
    assert f"task {task} was claimed or settled (now working)" in res["reason"]
    assert "IGNORE" not in res["reason"]
    assert [r["state"] for r in records(aenv).values()] == ["resolved"]
    assert autorecover.list_records(aenv.board, aenv.p["human"]) == []
    assert ("attention.resolved", note["id"]) in [(e, p.get("post_id")) for e, p in aenv.rec.events]
    # No new post: the closure is a resolution on the existing question, not another item.
    assert len(aenv.board.list_posts(aenv.p["human"], aenv.tid)["posts"]) == posts_before
    # The stall comes back: codex releases the task, which is accepted and unclaimed again.
    aenv.board.release_task(aenv.p["codex"], sid, task)
    aenv.d.tick()
    assert note["id"] in needs_you(aenv) and resolution(aenv, note["id"]) is None
    [rec] = records(aenv).values()
    assert rec["state"] == "escalated" and rec["escalation_post_id"] == note["id"]
    assert [r["task_id"] for r in autorecover.list_records(aenv.board, aenv.p["human"])] == [task]
    # And it closes again when the task moves on, still without a new post.
    again = aenv.session("codex")
    aenv.board.claim_task(aenv.p["codex"], again, task)
    aenv.d.tick()
    assert note["id"] not in needs_you(aenv) and resolution(aenv, note["id"])["automatic"] is True
    assert len(aenv.board.list_posts(aenv.p["human"], aenv.tid)["posts"]) == posts_before
    # A finished task can no longer come back: the marker is dropped, the closure stays.
    aenv.board.transition_task(aenv.p["codex"], again, task, "done")
    aenv.d.tick()
    assert aenv.board.conn.execute("SELECT 1 FROM board_state WHERE key = ?",
                                   (autorecover.CLOSED_PREFIX + str(note["id"]),)).fetchone() is None
    assert note["id"] not in needs_you(aenv)


def test_a_post_naming_several_stalls_closes_only_when_all_of_them_cleared(aenv):
    [first, second], note = escalated_unclaimed(aenv, n=2)
    aenv.board.claim_task(aenv.p["codex"], aenv.session("codex"), first)
    aenv.d.tick()
    assert note["id"] in needs_you(aenv) and resolution(aenv, note["id"]) is None
    aenv.board.transition_task(aenv.p["codex"], aenv.sid["codex"], second, "declined")
    aenv.d.tick()
    assert note["id"] not in needs_you(aenv)
    reason = resolution(aenv, note["id"])["reason"]
    assert f"task {first} was claimed or settled (now working)" in reason
    assert f"task {second}" in reason


def test_an_abandoned_task_escalation_closes_when_the_owner_reclaims(aenv):
    sid, task, ask = abandon(aenv)
    aenv.clock.advance(PAST_GRACE)
    aenv.d.tick()
    aenv.spawner.children[0].code = 1          # the run exits: its recovery request is blocked
    aenv.clock.advance(5)
    aenv.d.tick()
    aenv.d.tick()
    note = auto_posts(aenv)[-1]
    assert note["id"] in needs_you(aenv)
    aenv.board.claim_task(aenv.p["codex"], aenv.session("codex"), task)
    aenv.d.tick()
    assert note["id"] not in needs_you(aenv)
    assert f"task {task} was reclaimed, released or settled (now working)" in resolution(aenv, note["id"])["reason"]


def test_merely_awaiting_is_not_a_resolved_stall(aenv):
    """A task that now waits on other work closes the escalation through awaiting.py (in the setter's name), not as
    a stall the dispatcher saw clear; nothing is recorded as resolved."""
    [task], note = escalated_unclaimed(aenv)
    other = aenv.thread("fix")
    fix = aenv.accepted_task(other)
    aenv.board.update_task(aenv.p["human"], aenv.sid["human"], task, depends_on=[fix])
    aenv.d.tick()
    res = resolution(aenv, note["id"])
    assert res is not None and "automatic" not in res and "awaiting other work" in res["reason"]
    assert [r["state"] for r in records(aenv).values()] == ["escalated"]
    assert aenv.board.conn.execute("SELECT 1 FROM board_state WHERE key = ?",
                                   (autorecover.CLOSED_PREFIX + str(note["id"]),)).fetchone() is None


def test_an_escalation_the_human_already_answered_is_left_alone(aenv):
    [task], note = escalated_unclaimed(aenv)
    aenv.post("human", aenv.tid, "I'll look at it", "status", answer_to=[note["id"]])
    aenv.board.claim_task(aenv.p["codex"], aenv.session("codex"), task)
    aenv.d.tick()
    assert resolution(aenv, note["id"]) is None
    assert [r["state"] for r in records(aenv).values()] == ["resolved"]


def test_nothing_closes_while_automatic_recovery_is_switched_off(aenv):
    [task], note = escalated_unclaimed(aenv)
    aenv.board.s.auto_recover_stalled_work = False
    aenv.board.claim_task(aenv.p["codex"], aenv.session("codex"), task)
    aenv.d.tick()
    assert note["id"] in needs_you(aenv)
    aenv.board.s.auto_recover_stalled_work = True
    aenv.d.tick()
    assert note["id"] not in needs_you(aenv)


def test_an_escalated_recovery_wait_closes_when_its_request_progresses(aenv, tmp_path):
    h = blocked_handoff(aenv, tmp_path)
    blocked(aenv, h)
    aenv.clock.advance(24 * 3600)
    aenv.board.heartbeat(aenv.p["claude"], h.peer)
    aenv.d.tick()
    wait = wait_of(aenv, h)
    assert wait["state"] == "escalated" and wait["escalation_post_id"] in needs_you(aenv)
    aenv.d.tick()
    assert wait_of(aenv, h)["state"] == "escalated", "still held by the old session: stays escalated"
    with db.write_tx(aenv.board.conn):
        row = aenv.board.get_post(aenv.p["human"], h.ask["id"])["requests"][0]
        requests._save(aenv.board, aenv.p["human"], aenv.sid["human"], row, "finished", "done elsewhere", [],
                       "codex", h.old)
    aenv.d.tick()
    assert wait_of(aenv, h)["state"] == "resolved"
    assert wait["escalation_post_id"] not in needs_you(aenv)
    reason = resolution(aenv, wait["escalation_post_id"])["reason"]
    assert f"request #{h.ask['id']} was recovered, reassigned or finished" in reason


def test_the_dashboard_says_the_dispatcher_closed_it():
    import pathlib
    html = (pathlib.Path(__file__).resolve().parent.parent / "agent_comms" / "dashboard.html").read_text()
    assert "Closed automatically by the dispatcher (your auto-recovery setting, not your click)" in html


@pytest.mark.parametrize("state", ["sent", "suppressed"])
def test_only_escalated_records_close_posts(aenv, state):
    """A record that never reached the human (sent, or suppressed by the daily cap) has no post to close."""
    task = agent_task(aenv)
    aenv.clock.advance(autorecover.UNCLAIMED_AFTER_SECONDS + 60)
    aenv.d.tick()
    if state == "suppressed":
        key = autorecover.unclaimed_key(task)
        rec = records(aenv)[key] | {"state": "suppressed"}
        with db.write_tx(aenv.board.conn) as c:
            autorecover._update(c, key, rec, aenv.board.now())
    aenv.board.claim_task(aenv.p["codex"], aenv.session("codex"), task)
    aenv.d.tick()
    assert aenv.board.conn.execute("SELECT COUNT(*) FROM attention_resolutions").fetchone()[0] == 0
