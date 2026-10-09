"""Tasks that wait on other tasks, across threads: depends_on set after creation, the awaiting state (no Unstick, no
automatic-recovery escalation), Needs you escalations that close, and the automatic request to continue once the last
dependency finishes. Never spawns a real agent CLI."""

import json

import pytest
from fastapi.testclient import TestClient

from agent_comms import autorecover, awaiting, unstick
from agent_comms.api import create_app
from agent_comms.core import Conflict, Forbidden, Invalid, NotFound

from test_autorecover import INJECTION, LEASE, PAST_GRACE, abandon, aenv, agent_task, auto_posts, needs_you, records  # noqa: F401


def other_task(env, status="working"):
    """A task in another thread (the fix the first thread waits on), claimed by claude."""
    other = env.thread("other thread")
    task = env.accepted_task(other, title="FIX " + INJECTION)
    if status in ("working", "done", "blocked"):
        env.board.claim_task(env.p["claude"], env.sid["claude"], task)
    if status in ("done", "blocked"):
        env.board.transition_task(env.p["claude"], env.sid["claude"], task, status)
    return other, task


# ---------------------------------------------------------------- setting depends_on


def test_depends_on_can_be_set_across_threads_by_creator_owner_or_human(env):
    tid = env.thread()
    other, fix = other_task(env)
    mine = env.board.create_task(env.p["codex"], env.sid["codex"], tid, title="t")["id"]
    out = env.board.update_task(env.p["codex"], env.sid["codex"], mine, depends_on=[fix])
    assert out["depends_on"] == [fix]
    assert out["waiting_on"] == [{"id": fix, "thread_id": other, "status": "working", "title": "FIX " + INJECTION,
                                  "thread_status": "open"}]
    with pytest.raises(Forbidden):
        env.board.update_task(env.p["grok"], env.sid["grok"], mine, depends_on=[])
    assert env.board.update_task(env.p["human"], env.sid["human"], mine, depends_on=[])["depends_on"] == []
    # The owner may set them too.
    env.board.transition_task(env.p["human"], env.sid["human"], mine, "accepted")
    env.board.claim_task(env.p["claude"], env.sid["claude"], mine)
    assert env.board.update_task(env.p["claude"], env.sid["claude"], mine, depends_on=[fix])["depends_on"] == [fix]
    events = [e for e in env.board.get_task(env.p["human"], mine)["events"] if e["event"] == "depends_on"]
    assert [e["note"] for e in events] == [f"depends_on: {fix}", "depends_on: none", f"depends_on: {fix}"]


@pytest.mark.parametrize("bad, error", [([999], NotFound), (["1"], Invalid), ([True], Invalid), ("3", Invalid),
                                        (list(range(1, 30)), Invalid)])
def test_depends_on_is_validated(env, bad, error):
    tid = env.thread()
    task = env.accepted_task(tid)
    with pytest.raises(error):
        env.board.update_task(env.p["human"], env.sid["human"], task, depends_on=bad)


def test_depends_on_refuses_itself_and_cycles(env):
    tid = env.thread()
    a, b, c = (env.accepted_task(tid, title=x) for x in "abc")
    with pytest.raises(Invalid, match="itself"):
        env.board.update_task(env.p["human"], env.sid["human"], a, depends_on=[a])
    env.board.update_task(env.p["human"], env.sid["human"], a, depends_on=[b])
    env.board.update_task(env.p["human"], env.sid["human"], b, depends_on=[c])
    with pytest.raises(Invalid, match="cycle"):
        env.board.update_task(env.p["human"], env.sid["human"], c, depends_on=[a])     # c -> a -> b -> c
    with pytest.raises(Invalid, match="cycle"):
        env.board.update_task(env.p["human"], env.sid["human"], b, depends_on=[a])     # b -> a -> b
    assert env.board.get_task(env.p["human"], c)["depends_on"] == []


def test_depends_on_on_a_finished_task_or_an_unfinished_task_in_a_closed_thread_is_refused(env):
    tid = env.thread()
    other, fix = other_task(env)
    task = env.accepted_task(tid)
    env.board.set_thread_status(env.p["human"], other, "closed")
    with pytest.raises(Conflict, match="closed thread"):
        env.board.update_task(env.p["human"], env.sid["human"], task, depends_on=[fix])
    env.board.transition_task(env.p["human"], env.sid["human"], task, "declined")
    with pytest.raises(Conflict):
        env.board.update_task(env.p["human"], env.sid["human"], task, depends_on=[])


def test_status_and_depends_on_together_and_neither(env):
    tid = env.thread()
    _, fix = other_task(env)
    task = env.board.create_task(env.p["codex"], env.sid["codex"], tid, title="t")["id"]
    out = env.board.update_task(env.p["codex"], env.sid["codex"], task, "accepted", None, [fix])
    assert out["status"] == "accepted" and out["depends_on"] == [fix]
    with pytest.raises(Invalid):
        env.board.update_task(env.p["codex"], env.sid["codex"], task)


def test_claims_still_need_done_but_a_declined_dependency_ends_the_wait(env):
    tid = env.thread()
    _, fix = other_task(env, status="accepted")
    task = env.accepted_task(tid, depends_on=[fix])
    assert awaiting.awaits(env.board.conn, task)
    env.board.transition_task(env.p["human"], env.sid["human"], fix, "declined")
    assert not awaiting.awaits(env.board.conn, task)
    assert env.board.get_task(env.p["human"], task)["waiting_on"] == []
    with pytest.raises(Conflict, match="unfinished"):
        env.board.claim_task(env.p["codex"], env.sid["codex"], task)    # claims still need every dependency done
    env.board.update_task(env.p["human"], env.sid["human"], task, depends_on=[])
    assert env.board.claim_task(env.p["codex"], env.sid["codex"], task)["status"] == "working"


def test_http_sets_depends_on_without_a_status(env):
    client = TestClient(create_app(env.board))
    tid = env.thread()
    _, fix = other_task(env)
    task = env.board.create_task(env.p["codex"], env.sid["codex"], tid, title="t")["id"]
    h = {"Authorization": f"Bearer {env.tokens['codex']}"}
    r = client.post(f"/api/tasks/{task}/transition", json={"depends_on": [fix], "session_id": env.sid["codex"]}, headers=h)
    assert r.status_code == 200 and r.json()["depends_on"] == [fix] and r.json()["status"] == "proposed"
    r = client.post(f"/api/tasks/{task}/transition", json={"depends_on": [task], "session_id": env.sid["codex"]}, headers=h)
    assert r.status_code == 400
    r = client.post(f"/api/tasks/{task}/transition", json={"session_id": env.sid["codex"]}, headers=h)
    assert r.status_code == 400


def test_the_mcp_tool_takes_depends_on():
    import inspect
    from agent_comms import mcp_server
    src = inspect.getsource(mcp_server)
    assert "depends_on: list[int] | None = None" in src and "board.update_task(p, sid, task_id, status, note, depends_on)" in src


# ---------------------------------------------------------------- awaiting is not stalled


def test_unstick_leaves_awaiting_tasks_alone(env):
    tid = env.thread()
    _, fix = other_task(env)
    blocked = env.accepted_task(tid, title="b")
    env.board.claim_task(env.p["codex"], env.sid["codex"], blocked)
    env.board.transition_task(env.p["codex"], env.sid["codex"], blocked, "blocked")
    unclaimed = env.board.create_task(env.p["codex"], env.sid["codex"], tid, title="u")["id"]
    env.board.transition_task(env.p["codex"], env.sid["codex"], unclaimed, "accepted")
    assert unstick.stuck_agents(env.board, tid)[0] == ["codex"]
    for task in (blocked, unclaimed):
        env.board.update_task(env.p["codex"], env.sid["codex"], task, depends_on=[fix])
    assert unstick.stuck_agents(env.board, tid) == ([], [])
    with pytest.raises(Conflict, match="nothing here is waiting on an agent"):
        unstick.unstick(env.board, env.p["human"], tid, None)
    # An expired lease on an awaiting task is not a stall either.
    env.clock.advance(LEASE + 60)
    assert unstick.stuck_agents(env.board, tid) == ([], [])


def test_no_automatic_recovery_for_an_awaiting_task(aenv):
    other, fix = other_task(aenv)
    unclaimed = agent_task(aenv)
    aenv.board.update_task(aenv.p["codex"], aenv.sid["codex"], unclaimed, depends_on=[fix])
    sid, abandoned, _ = abandon(aenv)
    aenv.board.update_task(aenv.p["codex"], sid, abandoned, depends_on=[fix])
    aenv.clock.advance(PAST_GRACE)
    aenv.board.heartbeat(aenv.p["claude"], aenv.sid["claude"])    # the fix is still being worked on
    aenv.board.claim_task(aenv.p["claude"], aenv.sid["claude"], fix)
    aenv.d.tick()
    assert auto_posts(aenv) == [] and records(aenv) == {} and aenv.spawner.calls == []


def test_a_sent_recovery_waits_while_the_task_awaits_and_resumes_after(aenv):
    task = agent_task(aenv)
    aenv.clock.advance(autorecover.UNCLAIMED_AFTER_SECONDS + 60)
    aenv.d.tick()
    assert [r["state"] for r in records(aenv).values()] == ["sent"]
    _, fix = other_task(aenv)
    aenv.board.update_task(aenv.p["codex"], aenv.sid["codex"], task, depends_on=[fix])
    assert autorecover.list_records(aenv.board, aenv.p["human"]) == []
    aenv.clock.advance(LEASE + 60)
    aenv.d.tick()
    assert [r["state"] for r in records(aenv).values()] == ["sent"], "neither settled nor escalated while awaiting"
    assert len(auto_posts(aenv)) == 1
    aenv.board.update_task(aenv.p["codex"], aenv.sid["codex"], task, depends_on=[])    # the wait is dropped
    aenv.d.tick()
    posts = auto_posts(aenv)
    assert len(posts) == 2 and posts[1]["id"] in needs_you(aenv), "the stall resumes and escalates once"
    for _ in range(3):
        aenv.clock.advance(LEASE)
        aenv.d.tick()
    assert len(auto_posts(aenv)) == 2


def test_a_waiting_escalation_closes_once_the_dependency_is_set(aenv):
    # The creator accepted its own task: automatic recovery goes straight to the human (a Needs you question).
    task = agent_task(aenv, accepted_by="codex")
    aenv.clock.advance(autorecover.UNCLAIMED_AFTER_SECONDS + 60)
    aenv.d.tick()
    [note] = auto_posts(aenv)
    assert note["id"] in needs_you(aenv)
    assert [r["state"] for r in autorecover.list_records(aenv.board, aenv.p["human"])] == ["escalated"]
    _, fix = other_task(aenv)
    out = aenv.board.update_task(aenv.p["human"], aenv.sid["human"], task, depends_on=[fix])
    assert out["closed_escalation_post_ids"] == [note["id"]]
    assert note["id"] not in needs_you(aenv)
    assert autorecover.list_records(aenv.board, aenv.p["human"]) == []
    resolution = aenv.board.get_post(aenv.p["human"], note["id"])["attention_resolution"]
    assert resolution["resolved_by"] == "human" and f"task {task} now waits on task {fix}" in resolution["reason"]
    assert "FIX" not in resolution["reason"], "no agent-written text"


def test_a_grouped_escalation_closes_only_when_every_task_it_names_awaits(aenv):
    a = agent_task(aenv, accepted_by="codex")
    b = agent_task(aenv, accepted_by="codex")
    aenv.clock.advance(autorecover.UNCLAIMED_AFTER_SECONDS + 60)
    aenv.d.tick()
    [note] = auto_posts(aenv)
    _, fix = other_task(aenv)
    assert aenv.board.update_task(aenv.p["codex"], aenv.sid["codex"], a, depends_on=[fix])["closed_escalation_post_ids"] == []
    assert note["id"] in needs_you(aenv)
    assert [r["task_id"] for r in autorecover.list_records(aenv.board, aenv.p["human"])] == [b]
    out = aenv.board.update_task(aenv.p["codex"], aenv.sid["codex"], b, depends_on=[fix])
    assert out["closed_escalation_post_ids"] == [note["id"]] and note["id"] not in needs_you(aenv)
    assert aenv.board.get_post(aenv.p["human"], note["id"])["attention_resolution"]["resolved_by"] == "codex"


# ---------------------------------------------------------------- keeps moving


def waiting_task(env, owner=None):
    """codex's human-accepted task that waits on claude's fix in another thread."""
    task = agent_task(env)
    _, fix = other_task(env)
    env.board.update_task(env.p["codex"], env.sid["codex"], task, depends_on=[fix])
    return task, fix


def finish(env, fix):
    env.board.claim_task(env.p["claude"], env.sid["claude"], fix)
    env.board.transition_task(env.p["claude"], env.sid["claude"], fix, "done")


def test_the_creator_is_asked_to_continue_once_when_the_last_dependency_finishes(aenv):
    task, fix = waiting_task(aenv)
    aenv.clock.advance(10 * 60)             # codex is no longer live (so the dispatcher launches it)
    aenv.d.tick()
    assert auto_posts(aenv) == []
    finish(aenv, fix)
    aenv.clock.advance(5)
    aenv.d.tick()
    [post] = auto_posts(aenv)
    assert post["to"] == ["codex"] and post["agent"] == "human" and post["type"] == "request"
    assert post["body"].startswith(autorecover.HEADER)
    assert f"Task {task}'s dependencies are finished; continue it." in post["body"]
    assert "stalled on you" not in post["body"] and "IGNORE" not in post["body"] and "FIX" not in post["body"]
    assert aenv.spawner.agents() == ["codex-cli-fake"]
    from agent_comms import human_actions
    rule_id = human_actions.post_rule_id(aenv.board, post["id"])
    rule = next(r for r in aenv.board.list_dispatch_rules(aenv.p["human"], include_inactive=True) if r["id"] == rule_id)
    assert rule["purpose"] == autorecover.CONTINUE_PURPOSE.format(thread=aenv.tid) and rule["max_launches"] == 1
    [rec] = records(aenv).values()
    assert rec["kind"] == "continue" and rec["state"] == "sent"
    # Once per satisfaction event, also across passes and a restart.
    for _ in range(2):
        aenv.clock.advance(60)
        aenv.d.tick()
    assert len(auto_posts(aenv)) == 1 and len(aenv.spawner.calls) == 1
    # Claiming it settles the record.
    aenv.board.claim_task(aenv.p["codex"], aenv.session("codex"), task)
    aenv.d.tick()
    assert [r["state"] for r in records(aenv).values()] == ["recovered"]


def test_an_owner_holding_a_live_lease_is_the_one_asked(aenv):
    env = aenv
    task, fix = waiting_task(env)
    env.board.update_task(env.p["codex"], env.sid["codex"], task, depends_on=[])
    env.board.claim_task(env.p["claude"], env.sid["claude"], task)    # claude takes it over, then it waits again
    env.board.update_task(env.p["claude"], env.sid["claude"], task, depends_on=[fix])
    finish(env, fix)
    env.board.claim_task(env.p["claude"], env.sid["claude"], task)    # renews: claude holds a live lease
    env.d.tick()
    [post] = auto_posts(env)
    assert post["to"] == ["claude"]


def test_a_new_dependency_set_is_a_new_event(aenv):
    task, fix = waiting_task(aenv)
    finish(aenv, fix)
    aenv.d.tick()
    assert len(auto_posts(aenv)) == 1
    _, fix2 = other_task(aenv)
    aenv.board.update_task(aenv.p["codex"], aenv.sid["codex"], task, depends_on=[fix2])
    aenv.d.tick()
    assert len(auto_posts(aenv)) == 1
    finish(aenv, fix2)
    aenv.clock.advance(LEASE + 120)    # the first request's clock ran out meanwhile; that is not a second launch
    aenv.d.tick()
    bodies = [p["body"] for p in auto_posts(aenv) if p["type"] == "request"]
    assert len(bodies) == 2 and all("dependencies are finished; continue it" in b for b in bodies)


def test_satisfied_at_set_time_is_not_an_event(aenv):
    task = agent_task(aenv)
    _, fix = other_task(aenv, status="done")
    aenv.board.update_task(aenv.p["codex"], aenv.sid["codex"], task, depends_on=[fix])
    assert awaiting.state(aenv.board.conn, task)["waiting"] is False
    aenv.d.tick()
    assert auto_posts(aenv) == []


@pytest.mark.parametrize("why", ["setting_off", "paused", "no_runner", "budget", "per_task", "unstuck"])
def test_continuations_respect_the_guards_and_budgets(aenv, why):
    agent = "grok" if why == "no_runner" else "codex"
    task = agent_task(aenv, agent=agent)
    _, fix = other_task(aenv)
    aenv.board.update_task(aenv.p[agent], aenv.sid[agent], task, depends_on=[fix])
    if why == "budget":
        now = aenv.board.now()
        aenv.board.conn.execute("INSERT INTO board_state(key, value, updated_by, updated_at) VALUES (?, ?, 'x', ?)",
                                (autorecover.budget_key(agent, aenv.tid), json.dumps([now] * autorecover.AGENT_BUDGET), now))
    if why == "per_task":
        aenv.board.conn.execute("INSERT INTO board_state(key, value, updated_by, updated_at) VALUES (?, ?, 'x', 0)",
                                (autorecover.attempts_key(task), json.dumps(autorecover.MAX_PER_TASK)))
    finish(aenv, fix)
    if why == "setting_off":
        aenv.board.s.auto_recover_stalled_work = False
    elif why == "paused":
        aenv.board.set_paused(aenv.p["human"], True)
    elif why == "unstuck":
        aenv.clock.advance(5)
        aenv.board.conn.execute("INSERT INTO board_state(key, value, updated_by, updated_at) VALUES (?, ?, 'human', 0)",
                                (unstick.STATE_PREFIX + str(aenv.tid), json.dumps(aenv.board.now())))
    aenv.clock.advance(5)
    aenv.d.tick()
    posts = auto_posts(aenv)
    assert aenv.spawner.calls == [], "nothing launched"
    if why in ("budget", "per_task"):
        [note] = posts                        # the human is told instead, with one-click options
        assert note["to"] == [] and note["type"] == "question" and note["id"] in needs_you(aenv)
        assert f"task {task} (dependencies finished, codex)" in note["body"]
    else:
        assert posts == []


def test_work_the_human_never_authorized_goes_to_the_human(aenv):
    task = agent_task(aenv, accepted_by="codex")
    _, fix = other_task(aenv)
    aenv.board.update_task(aenv.p["codex"], aenv.sid["codex"], task, depends_on=[fix])
    finish(aenv, fix)
    aenv.d.tick()
    [note] = auto_posts(aenv)
    assert note["type"] == "question" and aenv.spawner.calls == []
    assert "neither you nor a standing grant authorized this work" in note["body"]
    q = note["decision_question"]
    assert q["recommended_option_id"] == "ask-creator"
    assert q["question"].startswith(f"Task {task}'s dependencies are finished but nobody continued it")


def test_a_continuation_that_does_not_take_escalates(aenv):
    task, fix = waiting_task(aenv)
    finish(aenv, fix)
    aenv.clock.advance(5 * 60)
    aenv.d.tick()
    aenv.spawner.children[0].code = 0
    aenv.clock.advance(LEASE + 60)
    aenv.d.tick()
    aenv.d.tick()
    note = auto_posts(aenv)[-1]
    assert note["to"] == [] and f"task {task} (dependencies finished, codex)" in note["body"]
    assert note["id"] in needs_you(aenv)


def test_pruning_keeps_waiting_records_and_drops_finished_ones(aenv):
    task, fix = waiting_task(aenv)
    key = awaiting.STATE_PREFIX + str(task)
    autorecover.prune(aenv.board, force=True)
    assert aenv.board.conn.execute("SELECT 1 FROM board_state WHERE key = ?", (key,)).fetchone()
    aenv.board.transition_task(aenv.p["human"], aenv.sid["human"], task, "declined")
    autorecover.prune(aenv.board, force=True)
    assert not aenv.board.conn.execute("SELECT 1 FROM board_state WHERE key = ?", (key,)).fetchone()



def test_a_continuation_not_claimed_within_a_ttl_escalates_with_that_reason(aenv):
    task, fix = waiting_task(aenv)
    live = aenv.session("codex")               # codex is live: nothing launches, the request waits
    finish(aenv, fix)
    aenv.board.heartbeat(aenv.p["codex"], live)
    aenv.d.tick()
    assert len(auto_posts(aenv)) == 1 and aenv.spawner.calls == []
    aenv.clock.advance(LEASE + 60)
    aenv.board.heartbeat(aenv.p["codex"], live)
    aenv.d.tick()
    note = auto_posts(aenv)[-1]
    assert "was not claimed or continued within 30 minutes of the automatic request to continue it" in note["body"]
    assert note["decision_question"]["recommended_option_id"] == "ask-creator"


# ---------------------------------------------------------------- review fixes: what counts, reopening, projects


def escalated_task(env):
    """codex accepted its own task: automatic recovery goes straight to the human. Returns (task, escalation post)."""
    task = agent_task(env, accepted_by="codex")
    env.clock.advance(autorecover.UNCLAIMED_AFTER_SECONDS + 60)
    env.d.tick()
    [note] = auto_posts(env)
    assert note["id"] in needs_you(env)
    return task, note


def own_task(env, agent="codex"):
    """A task the agent created itself, in another thread of the same project."""
    other = env.thread("agent's own thread")
    return env.board.create_task(env.p[agent], env.sid[agent], other, title="dummy")["id"]


def test_a_dependency_on_a_task_the_setter_created_itself_does_not_count(aenv):
    task, note = escalated_task(aenv)
    dummy = own_task(aenv)
    out = aenv.board.update_task(aenv.p["codex"], aenv.sid["codex"], task, depends_on=[dummy])
    assert out["depends_on"] == [dummy] and out["waiting_on"] == [] and out["closed_escalation_post_ids"] == []
    assert not awaiting.awaits(aenv.board.conn, task) and note["id"] in needs_you(aenv)
    # Another agent's task, or the human setting it, counts.
    theirs = own_task(aenv, agent="claude")
    out = aenv.board.update_task(aenv.p["codex"], aenv.sid["codex"], task, depends_on=[dummy, theirs])
    assert [d["id"] for d in out["waiting_on"]] == [theirs] and out["closed_escalation_post_ids"] == [note["id"]]
    codex_own = own_task(aenv)
    t2 = agent_task(aenv)
    assert aenv.board.update_task(aenv.p["human"], aenv.sid["human"], t2, depends_on=[codex_own])["waiting_on"]


def test_the_sql_and_python_rules_agree(aenv):
    task = agent_task(aenv)
    mine, theirs = own_task(aenv), own_task(aenv, agent="claude")
    other, closed_dep = other_task(aenv, status="accepted")
    for deps in ([], [mine], [theirs], [mine, theirs], [closed_dep]):
        aenv.board.update_task(aenv.p["codex"], aenv.sid["codex"], task, depends_on=deps)
        row = aenv.board._task_row(task)
        assert awaiting.awaits(aenv.board.conn, task) == bool(awaiting.waiting_on(aenv.board.conn, row)), deps
    aenv.board.set_thread_status(aenv.p["human"], other, "closed")
    row = aenv.board._task_row(task)
    assert not awaiting.awaits(aenv.board.conn, task) and awaiting.waiting_on(aenv.board.conn, row) == []
    assert [d["id"] for d in awaiting.blocked_by_closed(aenv.board.conn, row)] == [closed_dep]


def test_clearing_the_dependency_reopens_the_escalation_without_new_posts(aenv):
    task, note = escalated_task(aenv)
    _, fix = other_task(aenv)
    out = aenv.board.update_task(aenv.p["codex"], aenv.sid["codex"], task, depends_on=[fix])
    assert out["closed_escalation_post_ids"] == [note["id"]] and note["id"] not in needs_you(aenv)
    out = aenv.board.update_task(aenv.p["codex"], aenv.sid["codex"], task, depends_on=[])
    assert out["reopened_escalation_post_ids"] == [note["id"]]
    assert note["id"] in needs_you(aenv)
    assert "attention_resolution" not in aenv.board.get_post(aenv.p["human"], note["id"])
    [item] = autorecover.list_records(aenv.board, aenv.p["human"])
    assert item["state"] == "escalated" and item["escalation_post_id"] == note["id"]
    # Setting and clearing again posts nothing new: the same item closes and comes back.
    for _ in range(2):
        aenv.board.update_task(aenv.p["codex"], aenv.sid["codex"], task, depends_on=[fix])
        aenv.d.tick()
        aenv.board.update_task(aenv.p["codex"], aenv.sid["codex"], task, depends_on=[])
        aenv.d.tick()
    assert len(auto_posts(aenv)) == 1 and note["id"] in needs_you(aenv)


def test_switching_to_a_dependency_that_does_not_count_reopens_the_escalation(aenv):
    task, note = escalated_task(aenv)
    _, fix = other_task(aenv)
    aenv.board.update_task(aenv.p["codex"], aenv.sid["codex"], task, depends_on=[fix])
    out = aenv.board.update_task(aenv.p["codex"], aenv.sid["codex"], task, depends_on=[own_task(aenv)])
    assert out["reopened_escalation_post_ids"] == [note["id"]] and note["id"] in needs_you(aenv)


def test_a_finished_dependency_keeps_it_closed_and_the_continuation_takes_over(aenv):
    task, note = escalated_task(aenv)
    _, fix = other_task(aenv)
    aenv.board.update_task(aenv.p["codex"], aenv.sid["codex"], task, depends_on=[fix])
    finish(aenv, fix)
    aenv.d.tick()
    assert note["id"] not in needs_you(aenv), "a satisfaction event does not reopen it"
    posts = auto_posts(aenv)
    assert len(posts) == 2 and posts[1]["type"] == "question", "not authorized: the human is asked anew"
    assert posts[1]["decision_question"]["question"].startswith(f"Task {task}'s dependencies are finished")
    assert [r["kind"] for r in autorecover.list_records(aenv.board, aenv.p["human"])] == ["continue"]


def test_closing_the_blocking_thread_ends_the_wait_and_reopens_the_escalation(aenv):
    task, note = escalated_task(aenv)
    other, fix = other_task(aenv)
    aenv.board.update_task(aenv.p["codex"], aenv.sid["codex"], task, depends_on=[fix])
    assert unstick.stuck_agents(aenv.board, aenv.tid) == ([], [])
    aenv.board.set_thread_status(aenv.p["human"], other, "closed")
    assert not awaiting.awaits(aenv.board.conn, task)
    out = aenv.board.get_task(aenv.p["human"], task)
    assert out["waiting_on"] == [] and [d["id"] for d in out["blocked_by_closed"]] == [fix]
    assert note["id"] in needs_you(aenv)
    assert unstick.stuck_agents(aenv.board, aenv.tid)[0] == ["codex"]


def test_dependencies_stay_in_the_tasks_project_or_the_boards_own(aenv, tmp_path, monkeypatch):
    home = tmp_path / "board-home"
    home.mkdir()
    monkeypatch.setenv("AGENT_COMMS_HOME", str(home))
    task = agent_task(aenv)
    elsewhere = aenv.board.create_thread(aenv.p["human"], aenv.sid["human"], "other project", "/work/other")["id"]
    foreign = aenv.accepted_task(elsewhere)
    with pytest.raises(Invalid, match="another project"):
        aenv.board.update_task(aenv.p["human"], aenv.sid["human"], task, depends_on=[foreign])
    with pytest.raises(Invalid, match="another project"):
        aenv.accepted_task(aenv.tid, depends_on=[foreign])
    board_thread = aenv.board.create_thread(aenv.p["human"], aenv.sid["human"], "board fix", str(home))["id"]
    board_fix = aenv.accepted_task(board_thread)
    assert aenv.board.update_task(aenv.p["codex"], aenv.sid["codex"], task, depends_on=[board_fix])["waiting_on"]


def test_only_the_lease_holder_or_the_human_changes_dependencies_while_leased(aenv):
    task = agent_task(aenv)                                         # codex created it
    _, fix = other_task(aenv)
    aenv.board.claim_task(aenv.p["claude"], aenv.sid["claude"], task)
    with pytest.raises(Forbidden, match="leased by claude"):
        aenv.board.update_task(aenv.p["codex"], aenv.sid["codex"], task, depends_on=[fix])
    aenv.board.update_task(aenv.p["claude"], aenv.sid["claude"], task, depends_on=[fix])
    aenv.board.update_task(aenv.p["human"], aenv.sid["human"], task, depends_on=[])
    aenv.clock.advance(LEASE + 60)                                  # the lease expired: the creator may again
    aenv.board.update_task(aenv.p["codex"], aenv.sid["codex"], task, depends_on=[fix])


def test_agents_cannot_remove_a_dependency_the_human_set(aenv):
    task = agent_task(aenv)
    _, fix = other_task(aenv)
    theirs = own_task(aenv, agent="claude")
    aenv.board.update_task(aenv.p["human"], aenv.sid["human"], task, depends_on=[fix])
    with pytest.raises(Forbidden, match="only the human can remove it"):
        aenv.board.update_task(aenv.p["codex"], aenv.sid["codex"], task, depends_on=[])
    aenv.board.update_task(aenv.p["codex"], aenv.sid["codex"], task, depends_on=[fix, theirs])
    assert awaiting.state(aenv.board.conn, task)["setters"] == {str(fix): "human", str(theirs): "codex"}
    aenv.board.update_task(aenv.p["codex"], aenv.sid["codex"], task, depends_on=[fix])
    aenv.board.update_task(aenv.p["human"], aenv.sid["human"], task, depends_on=[])


def test_update_task_is_atomic(aenv):
    task, note = escalated_task(aenv)
    _, fix = other_task(aenv)
    with pytest.raises(Conflict, match="cannot move task"):   # a refused transition
        aenv.board.update_task(aenv.p["codex"], aenv.sid["codex"], task, "done", None, [fix])
    assert aenv.board.get_task(aenv.p["human"], task)["depends_on"] == []
    assert note["id"] in needs_you(aenv) and awaiting.state(aenv.board.conn, task) is None


def test_the_send_guard_rechecks_the_dependency_generation(aenv):
    task, fix = waiting_task(aenv)
    finish(aenv, fix)
    gen = awaiting.state(aenv.board.conn, task)["generation"]
    item = {"kind": "continue", "key": autorecover.continue_key(task, gen), "task_id": task, "generation": gen,
            "stalled_since": aenv.board.now()}
    check = autorecover._guard(aenv.board, aenv.d._fence(), [item], "codex", aenv.tid)
    check()                                                         # still satisfied, same generation
    _, fix2 = other_task(aenv)
    aenv.board.update_task(aenv.p["codex"], aenv.sid["codex"], task, depends_on=[fix2])
    with pytest.raises(Conflict, match="changed meanwhile"):
        check()


def test_a_declined_dependency_asks_to_remove_it_before_claiming(aenv):
    task, fix = waiting_task(aenv)
    aenv.board.transition_task(aenv.p["human"], aenv.sid["human"], fix, "declined")
    aenv.clock.advance(5 * 60)
    aenv.d.tick()
    [post] = auto_posts(aenv)
    assert f"Task {task}'s dependencies are finished; continue it." in post["body"]
    assert "if one was declined, first remove it with board_update_task(depends_on=[...])" in post["body"]
    marker = aenv.board.conn.execute("SELECT value FROM board_state WHERE key = ?",
                                     (autorecover.POST_PREFIX + str(post["id"]),)).fetchone()[0]
    assert json.loads(marker)["kind"] == "continue"


# ---------------------------------------------------------------- re-review: nobody asked, missed reconciles


def silent_blocked_owner(env):
    """The human's task #1: codex claims it, blocks it and goes silent; automatic recovery tells the human (a blocked
    task goes to the human, not to a launch). codex, the expired owner, then makes it wait on claude's task, which
    closes that escalation. Returns (task, escalation post, claude's task)."""
    task = env.accepted_task(env.tid, title="TASK " + INJECTION)
    sid = env.session("codex")
    env.board.claim_task(env.p["codex"], sid, task)
    env.board.transition_task(env.p["codex"], sid, task, "blocked")
    env.clock.advance(PAST_GRACE)
    env.d.tick()
    [note] = auto_posts(env)
    assert note["id"] in needs_you(env) and env.spawner.calls == []
    theirs = own_task(env, agent="claude")
    out = env.board.update_task(env.p["codex"], sid, task, depends_on=[theirs])
    assert out["closed_escalation_post_ids"] == [note["id"]] and note["id"] not in needs_you(env)
    return task, note, theirs


def finish_theirs(env, theirs, status="done"):
    if status == "done":
        env.board.claim_task(env.p["claude"], env.sid["claude"], theirs)
    env.board.transition_task(env.p["claude"], env.sid["claude"], theirs, status)


def test_the_expired_owner_is_asked_when_the_creator_is_the_human(aenv):
    task, note, theirs = silent_blocked_owner(aenv)
    finish_theirs(aenv, theirs)
    assert note["id"] not in needs_you(aenv), "pending: the dispatcher decides on its next pass"
    aenv.clock.advance(5 * 60)
    aenv.d.tick()
    [req] = [p for p in auto_posts(aenv) if p["type"] == "request"]
    assert req["to"] == ["codex"] and f"Task {task}'s dependencies are finished; continue it." in req["body"]
    assert aenv.spawner.agents() == ["codex-cli-fake"]
    assert note["id"] not in needs_you(aenv), "taken over by the request to continue"
    aenv.d.tick()
    stall = [r for r in records(aenv).values() if r["kind"] == "abandoned"]
    assert [r["state"] for r in stall] == ["resolved"]


@pytest.mark.parametrize("why", ["no_runner", "inactive"])
def test_nobody_to_ask_reopens_the_escalation(aenv, monkeypatch, why):
    task, note, theirs = silent_blocked_owner(aenv)
    if why == "no_runner":
        monkeypatch.setattr(aenv.d, "_runner", lambda agent: None)
    else:
        aenv.board.conn.execute("UPDATE agents SET active = 0 WHERE name = 'codex'")
    finish_theirs(aenv, theirs)
    aenv.clock.advance(5 * 60)
    aenv.d.tick()
    assert aenv.spawner.calls == [] and [p for p in auto_posts(aenv) if p["type"] == "request"] == []
    assert note["id"] in needs_you(aenv), "the escalation is back: nobody was asked to continue"
    gen = awaiting.state(aenv.board.conn, task)["generation"]
    assert awaiting.continue_record(aenv.board.conn, task, gen)["state"] == "none"
    stall = [r for r in records(aenv).values() if r["kind"] == "abandoned"]
    assert [r["state"] for r in stall] == ["escalated"], "the stall record is not resolved"
    if why == "no_runner":
        assert [r["task_id"] for r in autorecover.list_records(aenv.board, aenv.p["human"])] == [task]
    for _ in range(2):
        aenv.clock.advance(LEASE)
        aenv.d.tick()
    assert note["id"] in needs_you(aenv) and len(auto_posts(aenv)) == 1, "no churn"


def test_switched_off_recovery_reopens_a_satisfied_closure(aenv):
    task, note, theirs = silent_blocked_owner(aenv)
    aenv.board.s.auto_recover_stalled_work = False
    finish_theirs(aenv, theirs)
    assert note["id"] not in needs_you(aenv)
    aenv.d.tick()
    assert note["id"] in needs_you(aenv) and aenv.spawner.calls == []


def test_a_declined_dependency_that_ends_the_wait_unsatisfied_reopens_at_once(aenv):
    task, note = escalated_task(aenv)
    theirs, mine = own_task(aenv, agent="claude"), own_task(aenv)
    aenv.board.update_task(aenv.p["codex"], aenv.sid["codex"], task, depends_on=[theirs, mine])
    assert note["id"] not in needs_you(aenv)
    finish_theirs(aenv, theirs, status="declined")      # mine does not count and is unfinished: not satisfied
    assert not awaiting.awaits(aenv.board.conn, task) and not awaiting.satisfied(aenv.board.conn, task)
    assert note["id"] in needs_you(aenv), "reopened by the transition itself, without a dispatcher pass"


def test_the_dispatcher_pass_is_a_backstop(aenv):
    task, note = escalated_task(aenv)
    other, fix = other_task(aenv)
    aenv.board.update_task(aenv.p["codex"], aenv.sid["codex"], task, depends_on=[fix])
    # A change no reconcile hook sees (here: the thread closed behind the board's back).
    aenv.board.conn.execute("UPDATE threads SET status = 'closed' WHERE id = ?", (other,))
    assert note["id"] not in needs_you(aenv)
    aenv.d.tick()
    assert note["id"] in needs_you(aenv)
