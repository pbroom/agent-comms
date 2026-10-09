"""Automatic recovery bounds (review of PR #55): stall age, per-pass and per-agent launch budgets, who decides
(self-accepted tasks, blocked tasks, threads waiting on the human), the launch clock, races, the dashboard's
handled escalations, pruning, and takeover gates for a quiet interactive owner."""

import json
import pathlib

import pytest

from agent_comms import autorecover, recovery, unstick
from agent_comms.core import Conflict

from conftest import ASK, PROJECT
from test_autorecover import (LEASE, PAST_GRACE, abandon, abandoned_request, aenv, agent_task, auto_posts,  # noqa: F401
                              records, version)


def test_a_dispatched_session_is_abandoned_after_ten_minutes_an_interactive_one_after_a_lease(aenv):
    sid, task, _ = abandon(aenv, started=False)
    aenv.board.conn.execute("UPDATE sessions SET dispatch_run_id='gone-run' WHERE id=?", (sid,))
    run = {"run_id": "gone-run", "agent": "codex", "thread_id": aenv.tid, "status": "exited", "ended_at": aenv.clock()}
    aenv.board.conn.execute("INSERT INTO board_state(key,value,updated_by,updated_at) VALUES (?,?,?,?)",
                            ("dispatch.run.gone-run", json.dumps(run), "dispatcher", aenv.clock()))
    other = aenv.thread("interactive")
    isid = aenv.session("claude")
    itask = aenv.accepted_task(other)
    aenv.board.claim_task(aenv.p["claude"], isid, itask)
    aenv.clock.advance(LEASE + autorecover.GRACE_SECONDS + 60)
    aenv.d.tick()
    assert [r["task_id"] for r in records(aenv).values()] == [task]


def test_a_fourteen_day_old_stall_gets_no_launch(aenv):
    abandon(aenv)
    agent_task(aenv)
    aenv.clock.advance(14 * 24 * 3600)
    aenv.d.tick()
    assert auto_posts(aenv) == [] and records(aenv) == {} and aenv.spawner.calls == []


def test_a_stall_a_few_hours_old_is_still_recovered(aenv):
    abandon(aenv)
    aenv.clock.advance(5 * 3600)
    aenv.d.tick()
    assert len(auto_posts(aenv)) == 1 and len(aenv.spawner.calls) == 1


def test_at_most_three_sends_per_pass(aenv):
    for i in range(5):
        tid = aenv.thread(f"t{i}")
        sid = aenv.session("codex")
        aenv.board.claim_task(aenv.p["codex"], sid, aenv.accepted_task(tid))
    aenv.clock.advance(PAST_GRACE)
    aenv.d.tick()
    assert len(records(aenv)) == autorecover.MAX_SENDS_PER_PASS == 3
    aenv.d.tick()
    assert len(records(aenv)) == 5


def test_the_launch_budget_per_agent_and_thread_stops_a_launch_loop(aenv):
    """Claim, go silent, get launched, repeat: the third stall on the thread within a day goes to the human."""
    for _ in range(3):
        sid = aenv.session("codex")
        task = aenv.accepted_task(aenv.tid)
        aenv.board.claim_task(aenv.p["codex"], sid, task)
        aenv.clock.advance(PAST_GRACE)
        aenv.d.tick()
        for child in aenv.spawner.children:
            child.code = 0
        aenv.board.transition_task(aenv.p["human"], aenv.sid["human"], task, "declined")
        aenv.d.tick()
    assert len([p for p in auto_posts(aenv) if p["type"] == "request"]) == autorecover.AGENT_BUDGET == 2
    note = [p for p in auto_posts(aenv) if p["type"] == "question"][-1]
    assert "automatic launch budget for codex on this thread (2 per 24 hours) is spent" in note["body"]
    assert len(aenv.spawner.calls) == 2
    aenv.clock.advance(autorecover.BUDGET_WINDOW_SECONDS)
    assert autorecover._budget_used(aenv.board.conn, "codex", aenv.tid, aenv.clock()) == []


def test_the_budget_counts_unclaimed_and_abandoned_together(aenv):
    aenv.board.conn.execute("INSERT INTO board_state(key,value,updated_by,updated_at) VALUES (?,?,?,?)",
                            (autorecover.budget_key("codex", aenv.tid), json.dumps([aenv.clock()] * 2), "dispatcher",
                             aenv.clock()))
    agent_task(aenv)
    aenv.clock.advance(autorecover.UNCLAIMED_AFTER_SECONDS + 60)
    aenv.d.tick()
    [note] = auto_posts(aenv)
    assert note["type"] == "question" and "budget" in note["body"] and aenv.spawner.calls == []


def test_a_self_accepted_unclaimed_task_goes_to_the_human_without_a_launch(aenv):
    task = agent_task(aenv, accepted_by="self")
    aenv.clock.advance(autorecover.UNCLAIMED_AFTER_SECONDS + 60)
    aenv.d.tick()
    [note] = auto_posts(aenv)
    assert note["to"] == [] and note["needs_response"] and aenv.spawner.calls == []
    assert f"task {task} (unclaimed, codex): its creator accepted it, not you or a standing grant" in note["body"]
    [rec] = records(aenv).values()
    assert rec["state"] == "escalated" and rec["post_id"] is None
    aenv.d.tick()
    assert len(auto_posts(aenv)) == 1


def test_a_grant_covered_unclaimed_task_launches(aenv):
    aenv.board.create_grant(aenv.p["human"], project=PROJECT, category="implementation", agents=["codex"],
                            purpose="parser work")
    tid = aenv.board.create_task(aenv.p["codex"], aenv.sid["codex"], aenv.tid, title="t", category="implementation")["id"]
    aenv.board.transition_task(aenv.p["codex"], aenv.sid["codex"], tid, "accepted")
    aenv.clock.advance(autorecover.UNCLAIMED_AFTER_SECONDS + 60)
    aenv.d.tick()
    [post] = auto_posts(aenv)
    assert post["type"] == "request" and aenv.spawner.agents() == ["codex-cli-fake"]


def test_a_blocked_task_goes_to_the_human_without_a_launch(aenv):
    sid, task, _ = abandon(aenv, started=False)
    aenv.board.transition_task(aenv.p["codex"], sid, task, "blocked", "needs a decision")
    aenv.clock.advance(PAST_GRACE)
    aenv.d.tick()
    [note] = auto_posts(aenv)
    assert note["to"] == [] and aenv.spawner.calls == []
    assert "was blocked when its owner went silent" in note["body"]


def test_a_thread_waiting_on_the_human_gets_no_launch(aenv):
    abandon(aenv)
    question = aenv.post("claude", aenv.tid, "which option?", "question", needs_response=True,
                         decision_question=ASK)
    aenv.clock.advance(PAST_GRACE)
    aenv.d.tick()
    [note] = auto_posts(aenv)
    assert note["to"] == [] and aenv.spawner.calls == []
    assert f"the thread is waiting on you (post #{question['id']})" in note["body"]


def test_the_ttl_starts_at_the_actual_launch_not_while_queued(aenv):
    aenv.config.timeout_minutes = 600
    busy = aenv.thread("busy")
    aenv.board.create_dispatch_rule(aenv.p["human"], thread_id=busy, agents=["codex"], purpose="other work",
                                    max_launches=1)
    aenv.clock.advance(5 * 60)                          # the setup sessions are no longer live
    aenv.post("human", busy, "go", "request", to=["codex"])
    aenv.d.tick()
    assert len(aenv.spawner.calls) == 1                 # codex is busy on another thread
    abandon(aenv)
    aenv.clock.advance(PAST_GRACE)
    aenv.d.tick()
    assert len(auto_posts(aenv)) == 1 and len(aenv.spawner.calls) == 1   # queued behind one run per agent
    aenv.clock.advance(LEASE + 60)
    aenv.d.tick()
    assert len(auto_posts(aenv)) == 1, "no escalation while the launch is still queued"
    aenv.spawner.children[0].code = 0
    aenv.d.tick()                                       # reaped; a run that just ended counts as live briefly
    aenv.clock.advance(5 * 60)
    aenv.d.tick()
    assert len(aenv.spawner.calls) == 2                 # launched now
    aenv.clock.advance(LEASE - 120)
    aenv.d.tick()
    assert len(auto_posts(aenv)) == 1
    aenv.clock.advance(180)
    aenv.d.tick()
    assert "was not reclaimed or settled within 30 minutes" in auto_posts(aenv)[-1]["body"]


def escalate_by_failed_run(aenv):
    abandon(aenv)
    aenv.clock.advance(PAST_GRACE)
    aenv.d.tick()
    aenv.spawner.children[0].code = 1
    aenv.d.tick()
    aenv.d.tick()
    [rec] = records(aenv).values()
    assert rec["state"] == "escalated"
    return rec


def test_escalation_revokes_the_rule_and_says_this_stall(aenv):
    rec = escalate_by_failed_run(aenv)
    rule = next(r for r in aenv.board.list_dispatch_rules(aenv.p["human"], include_inactive=True)
                if r["id"] == rec["rule_id"])
    assert rule["state"] == "revoked"
    assert "No further automatic launches will be made for this stall." in auto_posts(aenv)[-1]["body"]


def test_a_human_unstick_during_the_send_wins(aenv, monkeypatch):
    abandon(aenv)
    aenv.clock.advance(PAST_GRACE)
    real = autorecover._guard

    def unstick_then_check(board, fence, items, agent, thread_id):
        check = real(board, fence, items, agent, thread_id)

        def later():
            board.conn.execute("INSERT INTO board_state(key,value,updated_by,updated_at) VALUES (?,?,?,?)",
                               (unstick.STATE_PREFIX + str(thread_id), json.dumps(board.now()), "human", board.now()))
            check()
        return later
    monkeypatch.setattr(autorecover, "_guard", unstick_then_check)
    aenv.d.tick()
    assert auto_posts(aenv) == [] and records(aenv) == {} and aenv.spawner.calls == []


def test_the_dashboard_stops_showing_an_escalation_once_the_human_answers_it(aenv):
    escalate_by_failed_run(aenv)
    [item] = autorecover.list_records(aenv.board, aenv.p["human"])
    aenv.post("human", aenv.tid, "I will look at it", "status", answer_to=[item["escalation_post_id"]])
    assert autorecover.list_records(aenv.board, aenv.p["human"]) == []


def test_the_dashboard_stops_showing_an_escalation_after_unstick(aenv):
    escalate_by_failed_run(aenv)
    assert autorecover.list_records(aenv.board, aenv.p["human"])
    aenv.clock.advance(1)
    unstick.unstick(aenv.board, aenv.p["human"], aenv.tid, aenv.config)
    assert autorecover.list_records(aenv.board, aenv.p["human"]) == []


def test_one_bad_row_does_not_stop_the_pass(aenv, monkeypatch):
    first = abandon(aenv)[1]
    other = aenv.thread("other")
    sid = aenv.session("claude")
    second = aenv.accepted_task(other)
    aenv.board.claim_task(aenv.p["claude"], sid, second)
    aenv.clock.advance(PAST_GRACE)
    real = autorecover._abandoned_item

    def boom(board, r, *a):
        if r["id"] == first:
            raise RuntimeError("bad row")
        return real(board, r, *a)
    monkeypatch.setattr(autorecover, "_abandoned_item", boom)
    aenv.d.tick()
    assert [r["task_id"] for r in records(aenv).values()] == [second]


def test_unclaimed_tasks_wait_while_their_creator_works_another_task_here(aenv):
    agent_task(aenv)
    other = aenv.accepted_task(aenv.tid)
    aenv.clock.advance(autorecover.UNCLAIMED_AFTER_SECONDS + 60)
    aenv.board.claim_task(aenv.p["codex"], aenv.sid["codex"], other)
    aenv.d.tick()
    assert auto_posts(aenv) == []


def test_prune_drops_finished_and_used_records_at_most_hourly(aenv):
    sid, task, ask = abandon(aenv)
    aenv.clock.advance(PAST_GRACE)
    aenv.d.tick()
    conn = aenv.board.conn
    has = lambda pattern: conn.execute("SELECT 1 FROM board_state WHERE key LIKE ?", (pattern,)).fetchone()
    assert records(aenv) and has("auto_recovery.budget.%")
    new = aenv.session("codex")
    aenv.board.claim_task(aenv.p["codex"], new, task)        # records the replaced lease
    assert has("session.abandoned.%")
    conn.execute("UPDATE request_progress SET state='finished' WHERE assigned_session=?", (sid,))   # used up
    aenv.board.transition_task(aenv.p["codex"], new, task, "done")
    assert autorecover.prune(aenv.board) == 0, "at most hourly"
    aenv.clock.advance(autorecover.PRUNE_EVERY_SECONDS)
    assert autorecover.prune(aenv.board) >= 2
    assert records(aenv) == {} and not has("session.abandoned.%")
    assert has("auto_recovery.budget.%"), "the budget stays for its window"
    assert has("auto_recovery.post.%"), "post markers stay (agent-post cap)"
    aenv.clock.advance(autorecover.BUDGET_WINDOW_SECONDS)
    autorecover.prune(aenv.board)
    assert not has("auto_recovery.budget.%")


def test_records_older_than_a_week_are_pruned(aenv):
    abandon(aenv)
    aenv.clock.advance(PAST_GRACE)
    aenv.d.tick()
    aenv.clock.advance(autorecover.RECORD_TTL_SECONDS + 1)
    autorecover.prune(aenv.board, force=True)
    assert records(aenv) == {}


def test_same_worktree_takeover_of_a_dirty_checkout_is_refused(env, tmp_path):
    project, old, tid, task, ask = abandoned_request(env, tmp_path)
    env.clock.advance(PAST_GRACE)
    new = env.session("codex", project=project)
    env.board.claim_task(env.p["codex"], new, task)              # the authorized successor in the same worktree
    pathlib.Path(project, "left-behind.txt").write_text("uncommitted work of the old session")
    with pytest.raises(Conflict, match="unfinished changes"):
        recovery.transfer_ended_owner(env.board, env.p["codex"], new, ask["id"], "codex", version(env, ask))
    row = env.board.get_post(env.p["human"], ask["id"])["requests"][0]
    assert row["assigned_session"] == old and row["state"] == "started"


def test_a_quiet_interactive_owner_within_a_lease_ttl_is_not_abandoned(env, tmp_path):
    project, old, tid, task, ask = abandoned_request(env, tmp_path)
    env.clock.advance(LEASE + autorecover.GRACE_SECONDS + 60)   # enough for a dispatched session, not an interactive one
    new = env.session("codex", project=project)
    env.board.claim_task(env.p["codex"], new, task)
    with pytest.raises(Conflict, match="30 minutes ago"):
        recovery.transfer_ended_owner(env.board, env.p["codex"], new, ask["id"], "codex", version(env, ask))


def test_a_dispatched_session_resumed_interactively_gets_the_full_grace(aenv):
    sid, task, _ = abandon(aenv, started=False)
    run = {"run_id": "r1", "agent": "codex", "thread_id": aenv.tid, "status": "exited", "ended_at": aenv.clock()}
    aenv.board.conn.execute("UPDATE sessions SET dispatch_run_id='r1' WHERE id=?", (sid,))
    aenv.board.conn.execute("INSERT INTO board_state(key,value,updated_by,updated_at) VALUES (?,?,?,?)",
                            ("dispatch.run.r1", json.dumps(run), "dispatcher", aenv.clock()))
    aenv.clock.advance(60)
    aenv.board.heartbeat(aenv.p["codex"], sid)        # seen after its run ended: the human resumed it
    session = aenv.board.conn.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
    assert recovery.abandon_grace(aenv.board, session) == LEASE
    aenv.clock.advance(LEASE + autorecover.GRACE_SECONDS + 60)
    aenv.d.tick()
    assert records(aenv) == {}


def own_silent_task(env, title):
    """An agent opens its own thread, claims a task it proposed itself (require_human_accept is off), goes silent."""
    sid = env.session("codex")
    tid = env.board.create_thread(env.p["codex"], sid, title, PROJECT)["id"]
    task = env.board.create_task(env.p["codex"], sid, tid, title="t")["id"]
    env.board.claim_task(env.p["codex"], sid, task)
    return tid, task


def test_an_agent_cannot_loop_through_its_own_abandoned_tasks_across_threads(aenv):
    for n in range(6):
        own_silent_task(aenv, f"cycle {n}")
        aenv.clock.advance(PAST_GRACE)
        aenv.d.tick()
    assert aenv.spawner.calls == [], "nothing the human authorized: no launch"
    notes = [p for p in aenv.board.snapshot(aenv.p["human"])["needs_you"]]
    assert len(notes) == autorecover.ESCALATION_DAILY_CAP == 5, "at most five posts about one agent a day"
    assert all("neither you nor a standing grant authorized this work" in p["body"] for p in notes)
    assert sorted(r["state"] for r in records(aenv).values()) == ["escalated"] * 5 + ["suppressed"]


def test_thirty_abandoned_threads_give_five_needs_you_posts(aenv):
    for n in range(30):
        own_silent_task(aenv, f"flood {n}")
    aenv.clock.advance(PAST_GRACE)
    for _ in range(10):
        aenv.d.tick()
    assert aenv.spawner.calls == []
    assert len(aenv.board.snapshot(aenv.p["human"])["needs_you"]) == 5
    states = [r["state"] for r in records(aenv).values()]
    assert states.count("escalated") == 5 and states.count("suppressed") == 25
    # Suppressed stalls get no dashboard record (the plain stalled labels show them), and nothing is posted later.
    assert len(autorecover.list_records(aenv.board, aenv.p["human"])) == 5
    aenv.clock.advance(60)
    aenv.d.tick()
    assert len(aenv.board.snapshot(aenv.p["human"])["needs_you"]) == 5
    # The cap is per rolling day and is pruned with the budgets.
    aenv.clock.advance(autorecover.BUDGET_WINDOW_SECONDS)
    autorecover.prune(aenv.board, force=True)
    assert not aenv.board.conn.execute("SELECT 1 FROM board_state WHERE key LIKE 'auto_recovery.escalations.%'").fetchone()


def test_the_escalation_cap_is_rechecked_inside_the_transaction(aenv, monkeypatch):
    own_silent_task(aenv, "one")
    aenv.clock.advance(PAST_GRACE)
    real = autorecover._escalations_left
    calls = {"n": 0}

    def spent_on_recheck(conn, agent, now):
        calls["n"] += 1
        return calls["n"] == 1 and real(conn, agent, now)    # free at the scan, spent by the time of the write
    monkeypatch.setattr(autorecover, "_escalations_left", spent_on_recheck)
    aenv.d.tick()
    assert aenv.board.snapshot(aenv.p["human"])["needs_you"] == []


def test_only_a_human_request_to_the_agent_before_the_claim_authorizes_a_launch(aenv):
    def thread_with(post_kw):
        sid = aenv.session("codex")
        tid = aenv.board.create_thread(aenv.p["codex"], sid, "t", PROJECT)["id"]
        if post_kw:
            aenv.post("human", tid, "hello", **post_kw)
        aenv.clock.advance(1)
        task = aenv.board.create_task(aenv.p["codex"], sid, tid, title="t")["id"]
        aenv.board.claim_task(aenv.p["codex"], sid, task)
        return tid
    status = thread_with({"type": "status"})
    other = thread_with({"type": "request", "to": ["claude"]})
    asked = thread_with({"type": "handoff", "to": ["codex"]})
    aenv.clock.advance(PAST_GRACE)
    aenv.d.tick()
    by_thread = {r["thread_id"]: r["state"] for r in records(aenv).values()}
    assert by_thread == {status: "escalated", other: "escalated", asked: "sent"}
    assert len(aenv.spawner.calls) == 1


def test_a_human_post_before_the_claim_authorizes_the_abandoned_launch(aenv):
    sid = aenv.session("codex")
    tid = aenv.board.create_thread(aenv.p["codex"], sid, "asked by the human", PROJECT)["id"]
    aenv.post("human", tid, "please take this on", "request", to=["codex"])
    aenv.clock.advance(1)
    task = aenv.board.create_task(aenv.p["codex"], sid, tid, title="t")["id"]
    aenv.board.claim_task(aenv.p["codex"], sid, task)
    aenv.clock.advance(PAST_GRACE)
    aenv.d.tick()
    assert len(aenv.spawner.calls) == 1


def test_an_automatic_post_does_not_count_as_the_human_asking(aenv):
    tid, task = own_silent_task(aenv, "auto only")
    # A marked automatic post from an earlier recovery is on the thread, before the claim.
    note = aenv.post("human", tid, "automatic", "status")
    aenv.board.conn.execute("INSERT INTO board_state(key,value,updated_by,updated_at) VALUES (?,?,?,?)",
                            (autorecover.POST_PREFIX + str(note["id"]), "{}", "dispatcher", aenv.clock()))
    aenv.board.conn.execute("UPDATE posts SET created_at=? WHERE id=?", (aenv.clock() - 60, note["id"]))
    aenv.clock.advance(PAST_GRACE)
    aenv.d.tick()
    assert aenv.spawner.calls == []


def test_a_per_agent_daily_budget_across_threads(aenv):
    aenv.board.conn.execute("INSERT INTO board_state(key,value,updated_by,updated_at) VALUES (?,?,?,?)",
                            (autorecover.agent_budget_key("codex"), json.dumps([aenv.clock()] * 6), "dispatcher",
                             aenv.clock()))
    abandon(aenv)
    aenv.clock.advance(PAST_GRACE)
    aenv.d.tick()
    [note] = auto_posts(aenv)
    assert "daily automatic launch budget for codex (6 per 24 hours across all threads) is spent" in note["body"]
    assert aenv.spawner.calls == []
