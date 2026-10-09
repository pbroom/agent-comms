"""Automatic-recovery escalations are structured questions the human answers in one click (Needs you shows Recommended,
Alternative, Write your own reply), and the bounded decision actions behind them: unstick, decline_task, release_task.
The question is built from server facts only; choosing runs the action inside the answer's transaction, refuses (409)
when the task or thread moved on, and clears the item from Needs you."""

import json

import pytest

from agent_comms import autorecover, browser_readiness, decision_actions, resolve, unstick
from agent_comms.core import Conflict, Forbidden, Invalid

from conftest import legacy_plain
from test_autorecover import (INJECTION, LEASE, PAST_GRACE, abandon, aenv, agent_task, auto_posts,  # noqa: F401
                              records)

MARKERS = ("IGNORE", "evil.example", "TASK ", "TITLE", "please do it", "Runner ended")


def needs_you(env):
    return [p["id"] for p in env.board.snapshot(env.p["human"])["needs_you"]]


def choose(env, post_id, option_id, who="human"):
    return resolve.resolve(env.board, env.p[who], post_id, "choose", None, env.config, option_id=option_id)


def options(post):
    q = post["decision_question"]
    return {o["id"]: o for o in q["options"]}, q["recommended_option_id"]


def post_count(env):
    return env.board.conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0]


def unclaimed_escalation(env):
    """A task codex created and accepted itself, unclaimed past the stall window: straight to the human."""
    task = agent_task(env, accepted_by="self")
    env.clock.advance(autorecover.UNCLAIMED_AFTER_SECONDS + 60)
    env.d.tick()
    [note] = auto_posts(env)
    return task, env.board.get_post(env.p["human"], note["id"])


def failed_recovery(env):
    """Abandoned work whose automatic recovery request ended blocked."""
    sid, task, ask = abandon(env)
    env.clock.advance(PAST_GRACE)
    env.d.tick()
    env.spawner.children[0].code = 1
    env.clock.advance(5)
    env.d.tick()
    env.d.tick()
    note = auto_posts(env)[-1]
    return sid, task, env.board.get_post(env.p["human"], note["id"])


# ---------------------------------------------------------------- the question


def test_an_unclaimed_escalation_is_a_structured_question_from_server_facts(aenv):
    task, note = unclaimed_escalation(aenv)
    assert (note["agent"], note["type"], note["to"], note["needs_response"]) == ("human", "question", [], True)
    assert note["body"].startswith("Automatic recovery did not take") and "not a human click" in note["body"]
    opts, rec = options(note)
    assert rec == "decline" and list(opts) == ["decline", "ask-creator"]
    assert opts["decline"]["action"] == {"type": "decline_task", "task_id": task, "expected_status": "accepted"}
    assert opts["ask-creator"]["action"] == {"type": "unstick", "thread_id": aenv.tid, "agents": ["codex"]}
    assert opts["decline"]["label"] == f"Decline task {task}" and "codex" in opts["ask-creator"]["label"]
    text = json.dumps(note["decision_question"])
    assert not any(m in text for m in MARKERS)
    item = next(p for p in aenv.board.snapshot(aenv.p["human"])["needs_you"] if p["id"] == note["id"])
    assert item["automatic"] is True and item["decision_question"] == note["decision_question"]


def test_a_failed_recovery_offers_relaunch_then_release(aenv):
    sid, task, note = failed_recovery(aenv)
    opts, rec = options(note)
    assert rec == "relaunch" and list(opts) == ["relaunch", "release"]
    assert opts["relaunch"]["action"] == {"type": "unstick", "thread_id": aenv.tid, "agents": ["codex"]}
    assert opts["release"]["action"] == {"type": "release_task", "task_id": task, "expected_owner_session": sid}
    assert not any(m in json.dumps(note["decision_question"]) for m in MARKERS), "the recorded request reason stays out"


def test_a_blocked_task_offers_a_relaunch_to_report_or_keeping_it_blocked(aenv):
    sid, task, _ = abandon(aenv, started=False)
    aenv.board.transition_task(aenv.p["codex"], sid, task, "blocked", "needs a decision " + INJECTION)
    aenv.clock.advance(PAST_GRACE)
    aenv.d.tick()
    [note] = auto_posts(aenv)
    opts, rec = options(aenv.board.get_post(aenv.p["human"], note["id"]))
    assert rec == "relaunch" and opts["relaunch"]["label"] == "Relaunch codex to report what it needs"
    assert opts["keep-blocked"]["outcome"] == "answered" and "action" not in opts["keep-blocked"]
    rules_before = aenv.board.list_dispatch_rules(aenv.p["human"], include_inactive=True)
    out = choose(aenv, note["id"], "keep-blocked")
    assert note["id"] not in needs_you(aenv) and out["option_id"] == "keep-blocked"
    assert aenv.board.get_task(aenv.p["human"], task)["status"] == "blocked"
    assert aenv.board.list_dispatch_rules(aenv.p["human"], include_inactive=True) == rules_before
    assert autorecover.list_records(aenv.board, aenv.p["human"]) == []


def test_a_browser_denial_recommends_the_release(aenv, monkeypatch):
    sid, task, ask = abandon(aenv)
    monkeypatch.setattr(browser_readiness, "request_blocker",
                        lambda board, post_id, recipient: "denied" if post_id == ask["id"] else None)
    aenv.clock.advance(PAST_GRACE)
    aenv.d.tick()
    [note] = auto_posts(aenv)
    opts, rec = options(aenv.board.get_post(aenv.p["human"], note["id"]))
    assert rec == "release" and "browser permission" in opts["relaunch"]["description"]


def test_several_stalls_on_one_thread_share_one_unstick_question(aenv):
    first = agent_task(aenv, accepted_by="self")
    second = agent_task(aenv, accepted_by="self")
    aenv.clock.advance(autorecover.UNCLAIMED_AFTER_SECONDS + 60)
    aenv.d.tick()
    [note] = auto_posts(aenv)
    note = aenv.board.get_post(aenv.p["human"], note["id"])
    opts, rec = options(note)
    assert rec == "unstick" and list(opts) == ["unstick", "leave"]
    assert f"tasks {first}, {second}" in note["decision_question"]["question"]
    assert opts["unstick"]["action"] == {"type": "unstick", "thread_id": aenv.tid, "agents": ["codex"]}
    out = choose(aenv, note["id"], "unstick")
    asked = aenv.board.get_post(aenv.p["human"], out["action_result"]["unstick_post_id"])
    assert f"task {first} is accepted but unclaimed" in asked["body"] and f"task {second}" in asked["body"]


# ---------------------------------------------------------------- one-click answers


def test_decline_declines_the_task_and_clears_needs_you(aenv):
    task, note = unclaimed_escalation(aenv)
    before = post_count(aenv)
    out = choose(aenv, note["id"], "decline")
    assert out["action_result"] == {"task_id": task, "status": "declined", "previous_status": "accepted"}
    assert aenv.board.get_task(aenv.p["human"], task)["status"] == "declined"
    assert note["id"] not in needs_you(aenv)
    receipt = aenv.board.get_post(aenv.p["human"], out["receipt_post_id"])
    assert receipt["body"] == f"Server executed the choice on #{note['id']}. Declined task {task}."
    answer = aenv.board.get_post(aenv.p["human"], out["post_id"])
    assert answer["answer_to"] == [note["id"]] and answer["to"] == []
    assert post_count(aenv) == before + 2
    assert choose(aenv, note["id"], "decline") == out, "a retry returns the receipt"
    aenv.d.tick()
    assert autorecover.list_records(aenv.board, aenv.p["human"]) == []
    events = aenv.board.get_task(aenv.p["human"], task)["events"]
    assert events[-1]["to"] == "declined" and events[-1]["agent"] == "human"


def test_ask_the_creator_runs_unstick_with_a_one_shot_launch(aenv):
    task, note = unclaimed_escalation(aenv)
    out = choose(aenv, note["id"], "ask-creator")
    result = out["action_result"]
    assert result["agents"] == ["codex"] and isinstance(result["rule_id"], int)
    asked = aenv.board.get_post(aenv.p["human"], result["unstick_post_id"])
    assert asked["agent"] == "human" and asked["to"] == ["codex"] and asked["body"].startswith("Unstick:")
    from agent_comms import human_actions
    assert human_actions.post_rule_id(aenv.board, asked["id"]) == result["rule_id"]
    assert note["id"] not in needs_you(aenv)
    aenv.d.tick()
    assert aenv.spawner.agents()[-1] == "codex-cli-fake", "the dispatcher launches codex for the Unstick request"
    # The thread's Unstick cooldown is spent, exactly as after a click.
    with pytest.raises(Conflict, match="unstuck less than"):
        unstick.unstick(aenv.board, aenv.p["human"], aenv.tid, aenv.config)


def test_release_clears_the_lease_and_returns_the_task_to_accepted(aenv):
    sid, task, note = failed_recovery(aenv)
    out = choose(aenv, note["id"], "release")
    t = aenv.board.get_task(aenv.p["human"], task)
    assert (t["status"], t["owner_agent"], t["owner_session"]) == ("accepted", None, None)
    assert out["action_result"] == {"task_id": task, "status": "accepted", "previous_status": "working"}
    assert note["id"] not in needs_you(aenv)


def test_relaunch_unsticks_the_owner(aenv):
    sid, task, note = failed_recovery(aenv)
    out = choose(aenv, note["id"], "relaunch")
    asked = aenv.board.get_post(aenv.p["human"], out["action_result"]["unstick_post_id"])
    assert asked["to"] == ["codex"] and f"task {task}" in asked["body"]


# ---------------------------------------------------------------- refusals roll everything back


def refused(env, note, option_id, exc=Conflict):
    before = post_count(env)
    rules = env.board.list_dispatch_rules(env.p["human"], include_inactive=True)
    with pytest.raises(exc):
        choose(env, note["id"], option_id)
    assert post_count(env) == before
    assert env.board.list_dispatch_rules(env.p["human"], include_inactive=True) == rules
    assert note["id"] in needs_you(env)


def test_decline_is_refused_once_the_task_was_claimed(aenv):
    task, note = unclaimed_escalation(aenv)
    aenv.board.claim_task(aenv.p["codex"], aenv.sid["codex"], task)
    refused(aenv, note, "decline")
    assert aenv.board.get_task(aenv.p["human"], task)["status"] == "working"


def test_unstick_is_refused_when_the_agent_no_longer_holds_up_the_thread(aenv):
    task, note = unclaimed_escalation(aenv)
    aenv.board.claim_task(aenv.p["codex"], aenv.sid["codex"], task)
    refused(aenv, note, "ask-creator")


def test_unstick_is_refused_inside_the_cooldown_and_leaves_no_trace(aenv):
    task, note = unclaimed_escalation(aenv)
    unstick.unstick(aenv.board, aenv.p["human"], aenv.tid, aenv.config)     # the human clicked Unstick by hand
    stamp = aenv.board.conn.execute("SELECT value FROM board_state WHERE key = ?",
                                    (unstick.STATE_PREFIX + str(aenv.tid),)).fetchone()[0]
    refused(aenv, note, "ask-creator")
    assert aenv.board.conn.execute("SELECT value FROM board_state WHERE key = ?",
                                   (unstick.STATE_PREFIX + str(aenv.tid),)).fetchone()[0] == stamp


def test_release_is_refused_after_the_owner_came_back(aenv):
    sid, task, note = failed_recovery(aenv)
    aenv.board.claim_task(aenv.p["codex"], aenv.session("codex"), task)     # a new session reclaimed it
    refused(aenv, note, "release")


def test_paused_or_closed_boards_refuse(aenv):
    task, note = unclaimed_escalation(aenv)
    aenv.board.set_paused(aenv.p["human"], True)
    refused(aenv, note, "decline")
    aenv.board.set_paused(aenv.p["human"], False)
    aenv.board.set_thread_status(aenv.p["human"], aenv.tid, "closed")
    with pytest.raises(Conflict):
        choose(aenv, note["id"], "decline")
    assert aenv.board.get_task(aenv.p["human"], task)["status"] == "accepted"


# ---------------------------------------------------------------- the action types


def question_with(env, thread_id, action, author="codex"):
    return env.post(author, thread_id, "Please choose", "question", needs_response=True, decision_question={
        "question": "Do it?", "context": "",
        "options": [{"id": "yes", "label": "Do it", "description": "the action", "outcome": "approved",
                     "action": action},
                    {"id": "no", "label": "Leave it", "description": "nothing", "outcome": "answered"}],
        "recommended_option_id": "yes"})


@pytest.mark.parametrize("action", [
    {"type": "decline_task", "task_id": 1},
    {"type": "decline_task", "task_id": 1, "expected_status": "done"},
    {"type": "decline_task", "task_id": 0, "expected_status": "accepted"},
    {"type": "decline_task", "task_id": True, "expected_status": "accepted"},
    {"type": "decline_task", "task_id": 1, "expected_status": "accepted", "note": "x"},
    {"type": "release_task", "task_id": 1, "expected_owner_session": "3"},
    {"type": "release_task", "task_id": 1},
    {"type": "unstick", "thread_id": 1, "agents": []},
    {"type": "unstick", "thread_id": 1, "agents": ["codex", "codex"]},
    {"type": "unstick", "thread_id": 1, "agents": ["Codex; rm -rf"]},
    {"type": "unstick", "thread_id": 1},
    {"type": "unstick", "thread_id": 1, "agents": ["codex"], "command": "touch /tmp/x"},
])
def test_exact_fields_and_positive_ids(env, action):
    with pytest.raises(Invalid):
        decision_actions.validate(action)
    with pytest.raises(Invalid):
        question_with(env, env.thread(), action)


def test_task_actions_must_target_the_question_thread_and_only_the_human_runs_them(env):
    from agent_comms.dispatch import DispatchConfig
    here, elsewhere = env.thread(), env.thread()
    task = env.board.create_task(env.p["codex"], env.sid["codex"], elsewhere, title="t")["id"]
    env.board.transition_task(env.p["codex"], env.sid["codex"], task, "accepted")
    q = question_with(env, here, {"type": "decline_task", "task_id": task, "expected_status": "accepted"})
    with pytest.raises(Forbidden):
        resolve.resolve(env.board, env.p["codex"], q["id"], "choose", None, DispatchConfig(), option_id="yes")
    with pytest.raises(Forbidden, match="question thread"):
        resolve.resolve(env.board, env.p["human"], q["id"], "choose", None, DispatchConfig(), option_id="yes")
    q2 = question_with(env, here, {"type": "unstick", "thread_id": elsewhere, "agents": ["codex"]})
    with pytest.raises(Forbidden, match="question thread"):
        resolve.resolve(env.board, env.p["human"], q2["id"], "choose", None, DispatchConfig(), option_id="yes")
    assert env.board.get_task(env.p["human"], task)["status"] == "accepted"
    # In its own thread, the human's choice declines it.
    q3 = question_with(env, elsewhere, {"type": "decline_task", "task_id": task, "expected_status": "accepted"})
    resolve.resolve(env.board, env.p["human"], q3["id"], "choose", None, DispatchConfig(), option_id="yes")
    assert env.board.get_task(env.p["human"], task)["status"] == "declined"


def test_task_actions_need_an_answer_transaction(env):
    tid = env.thread()
    task = env.accepted_task(tid)
    action = {"type": "decline_task", "task_id": task, "expected_status": "accepted"}
    q = env.board.get_post(env.p["human"], question_with(env, tid, action)["id"])
    with pytest.raises(Invalid, match="atomic"):
        decision_actions.execute(env.board, env.p["human"], env.sid["human"], q, q["decision_question"]["options"][0]["action"])


# ---------------------------------------------------------------- escalations stored before questions


def test_an_escalation_without_a_question_still_works(aenv):
    """Live posts 570 and 572 were stored as plain status escalations: they render and resolve as before."""
    task, note = unclaimed_escalation(aenv)
    legacy_plain(aenv, note["id"], "status")
    item = next(p for p in aenv.board.snapshot(aenv.p["human"])["needs_you"] if p["id"] == note["id"])
    assert item["decision_question"] is None and item["type"] == "status" and item["automatic"] is True
    [rec] = autorecover.list_records(aenv.board, aenv.p["human"])
    assert rec["escalation_post_id"] == note["id"]
    with pytest.raises(Invalid, match="no structured options"):
        choose(aenv, note["id"], "decline")
    with pytest.raises(Invalid, match="not written by an active agent"):
        resolve.resolve(aenv.board, aenv.p["human"], note["id"], "ask_options", None, aenv.config)
    resolve.resolve(aenv.board, aenv.p["human"], note["id"], "not_now", None, aenv.config)
    assert note["id"] not in needs_you(aenv)
    assert autorecover.list_records(aenv.board, aenv.p["human"]) == []


def test_a_question_that_cannot_be_built_does_not_block_the_escalation(aenv, monkeypatch):
    def broken(*args):
        raise ValueError("boom")
    monkeypatch.setattr(autorecover, "escalation_question", broken)
    agent_task(aenv, accepted_by="self")
    aenv.clock.advance(autorecover.UNCLAIMED_AFTER_SECONDS + 60)
    aenv.d.tick()
    [note] = auto_posts(aenv)
    assert note["type"] == "status" and note["decision_question"] is None and note["id"] in needs_you(aenv)
    assert "pick one of the options" not in note["body"] and "then Unstick, reassign or decline" in note["body"]


def test_a_late_failure_inside_the_one_click_unstick_rolls_everything_back_and_a_retry_works(aenv, monkeypatch):
    task, note = unclaimed_escalation(aenv)
    stamp_key = unstick.STATE_PREFIX + str(aenv.tid)

    def state():
        return (post_count(aenv), aenv.board.list_dispatch_rules(aenv.p["human"], include_inactive=True),
                aenv.board.conn.execute("SELECT COUNT(*) FROM board_state").fetchone()[0])
    before = state()
    real = aenv.board.create_post

    def fail_on_receipt(*args, **kwargs):
        if kwargs.get("body", "").startswith("Server executed"):    # after Unstick posted and approved its rule
            raise RuntimeError("receipt unavailable")
        return real(*args, **kwargs)
    monkeypatch.setattr(aenv.board, "create_post", fail_on_receipt)
    with pytest.raises(RuntimeError):
        choose(aenv, note["id"], "ask-creator")
    assert state() == before, "no Unstick post, rule, rule binding, cooldown stamp or receipt survives"
    assert aenv.board.conn.execute("SELECT 1 FROM board_state WHERE key = ?", (stamp_key,)).fetchone() is None
    assert note["id"] in needs_you(aenv)
    monkeypatch.setattr(aenv.board, "create_post", real)
    out = choose(aenv, note["id"], "ask-creator")
    assert out["action_result"]["agents"] == ["codex"] and note["id"] not in needs_you(aenv)


def test_one_click_actions_notify_after_the_answer_commits(aenv):
    task, note = unclaimed_escalation(aenv)
    aenv.rec.events.clear()
    out = choose(aenv, note["id"], "decline")
    events = [(e, p) for e, p in aenv.rec.events if e in ("post.created", "task.transition", "task.released")]
    assert ("task.transition", {"task_id": task, "from": "accepted", "to": "declined", "agent": "human"}) in events
    assert [p["post_id"] for e, p in events if e == "post.created"] == [out["receipt_post_id"], out["post_id"]]
    aenv.rec.events.clear()
    choose(aenv, note["id"], "decline")                                   # a retry returns the receipt, silently
    assert [e for e, _ in aenv.rec.events if e in ("post.created", "task.transition")] == []


def test_release_and_unstick_notify_too(aenv):
    sid, task, note = failed_recovery(aenv)
    aenv.rec.events.clear()
    choose(aenv, note["id"], "release")
    assert ("task.released", {"task_id": task, "agent": "human"}) in aenv.rec.events
    aenv.tid = aenv.thread("other")
    task2, note2 = unclaimed_escalation(aenv)
    aenv.rec.events.clear()
    out = choose(aenv, note2["id"], "ask-creator")
    created = [p["post_id"] for e, p in aenv.rec.events if e == "post.created"]
    assert created == [out["action_result"]["unstick_post_id"], out["receipt_post_id"], out["post_id"]]


def claude_lease_expiring_beside_an_unclaimed_codex_escalation(env):
    """Thread T: codex's self-accepted task is escalated to the human (unclaimed), while claude's lease on a
    human-accepted task has expired but is still inside an interactive session's grace."""
    agent_task(env, accepted_by="self")
    claude_sid = env.session("claude")
    theirs = env.accepted_task(env.tid, title="claude's")
    env.board.claim_task(env.p["claude"], claude_sid, theirs)
    env.clock.advance(autorecover.UNCLAIMED_AFTER_SECONDS + 60)
    env.d.tick()
    [note] = auto_posts(env)
    assert records(env) and all(r["agent"] == "codex" for r in records(env).values()), "claude is still in grace"
    return theirs, note


def claude_recoveries(env):
    return [p for p in auto_posts(env) if p["type"] == "request" and p["to"] == ["claude"]]


def test_a_one_click_unstick_for_one_agent_leaves_another_agents_stall_to_automatic_recovery(aenv):
    """Re-review of #56: the narrowed Unstick wrote the thread-wide stamp, and automatic recovery then skipped every
    agent's stall on the thread, so claude's abandoned task never reached anyone."""
    theirs, note = claude_lease_expiring_beside_an_unclaimed_codex_escalation(aenv)
    out = choose(aenv, note["id"], "ask-creator")
    assert out["action_result"]["agents"] == ["codex"]
    assert unstick.unstuck_since(aenv.board.conn, aenv.tid, "codex", 0)
    assert not unstick.unstuck_since(aenv.board.conn, aenv.tid, "claude", 0)
    aenv.clock.advance(PAST_GRACE)
    aenv.d.tick()
    [asked] = claude_recoveries(aenv)
    assert f"task {theirs} was abandoned" in asked["body"]


def test_a_full_unstick_still_covers_every_agent_it_asked(aenv):
    """Control: the dashboard's Unstick asked claude too, so automatic recovery does not ask claude again."""
    theirs, note = claude_lease_expiring_beside_an_unclaimed_codex_escalation(aenv)
    out = unstick.unstick(aenv.board, aenv.p["human"], aenv.tid, aenv.config)
    assert sorted(out["agents"]) == ["claude", "codex"]
    aenv.clock.advance(PAST_GRACE)
    aenv.d.tick()
    assert claude_recoveries(aenv) == []


def test_an_unstick_stamp_from_before_agents_were_recorded_covers_every_agent(env):
    tid = env.thread()
    env.board.conn.execute("INSERT INTO board_state(key, value, updated_by, updated_at) VALUES (?, ?, 'human', ?)",
                           (unstick.STATE_PREFIX + str(tid), json.dumps(100.0), 100.0))
    assert unstick.unstuck_since(env.board.conn, tid, "claude", 50) and unstick.unstuck_since(env.board.conn, tid, "x", 100)
    assert not unstick.unstuck_since(env.board.conn, tid, "claude", 101)


def test_notifications_never_announce_another_writers_post(aenv, monkeypatch):
    task, note = unclaimed_escalation(aenv)
    real = resolve._notify_committed
    other = {}

    def another_writer_first(board, p, new_posts, action, result):
        other["post"] = aenv.post("claude", aenv.tid, "committed in between")    # after the commit, before notify
        return real(board, p, new_posts, action, result)
    monkeypatch.setattr(resolve, "_notify_committed", another_writer_first)
    aenv.rec.events.clear()
    out = choose(aenv, note["id"], "decline")
    announced = [p["post_id"] for e, p in aenv.rec.events if e == "post.created"]
    assert announced.count(other["post"]["id"]) == 1, "only the other writer's own create_post announced it"
    assert announced == [other["post"]["id"], out["receipt_post_id"], out["post_id"]]


def test_a_one_click_unstick_asks_only_the_named_agents(aenv):
    """An agent-written question can trigger Unstick for the agents it names only, never every stuck agent."""
    agent_task(aenv, accepted_by="self")                                   # codex is stuck here ...
    theirs = aenv.board.create_task(aenv.p["claude"], aenv.sid["claude"], aenv.tid, title="c")["id"]
    aenv.board.transition_task(aenv.p["claude"], aenv.sid["claude"], theirs, "accepted")   # ... and claude
    assert unstick.stuck_agents(aenv.board, aenv.tid)[0] == ["codex", "claude"]
    q = aenv.post("claude", aenv.tid, "unstick codex?", "question", needs_response=True, decision_question={
        "question": "Ask codex?", "context": "",
        "options": [{"id": "yes", "label": "Ask codex", "description": "Unstick", "outcome": "approved",
                     "action": {"type": "unstick", "thread_id": aenv.tid, "agents": ["codex"]}},
                    {"id": "no", "label": "No", "description": "nothing", "outcome": "answered"}],
        "recommended_option_id": "yes"})
    out = choose(aenv, q["id"], "yes")
    asked = aenv.board.get_post(aenv.p["human"], out["action_result"]["unstick_post_id"])
    assert asked["to"] == ["codex"] and "claude" not in asked["body"]
    rules = {r["id"]: r for r in aenv.board.list_dispatch_rules(aenv.p["human"])}
    assert rules[out["action_result"]["rule_id"]]["agents"] == ["codex"]
