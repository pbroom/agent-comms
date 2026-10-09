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


def test_a_declined_dependency_counts_as_finished_for_claims(env):
    tid = env.thread()
    _, fix = other_task(env, status="accepted")
    task = env.accepted_task(tid, depends_on=[fix])
    with pytest.raises(Conflict, match="unfinished"):
        env.board.claim_task(env.p["codex"], env.sid["codex"], task)
    env.board.transition_task(env.p["human"], env.sid["human"], fix, "declined")
    assert env.board.claim_task(env.p["codex"], env.sid["codex"], task)["status"] == "working"
    assert env.board.get_task(env.p["human"], task)["waiting_on"] == []


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


def test_a_sent_recovery_settles_when_the_task_starts_awaiting(aenv):
    task = agent_task(aenv)
    aenv.clock.advance(autorecover.UNCLAIMED_AFTER_SECONDS + 60)
    aenv.d.tick()
    assert [r["state"] for r in records(aenv).values()] == ["sent"]
    _, fix = other_task(aenv)
    aenv.board.update_task(aenv.p["codex"], aenv.sid["codex"], task, depends_on=[fix])
    assert autorecover.list_records(aenv.board, aenv.p["human"]) == []
    aenv.clock.advance(LEASE + 60)
    aenv.d.tick()
    assert [r["state"] for r in records(aenv).values()] == ["recovered"]
    assert len(auto_posts(aenv)) == 1, "no escalation"


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
