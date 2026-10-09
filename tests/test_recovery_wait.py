"""Automatic owner handoff (DESIGN_NOTES "Automatic owner handoff"): verifying an owner and recovering ownership within
the existing scope never needs the human. A transient blocker (another session or run still busy in the owner's
checkout) records a recovery wait; the dispatcher relaunches the agent once the worktree is free, at most three times
per request per rolling day, and only then asks the human. Persistent blockers keep going to the human. Never spawns a
real agent CLI."""

import asyncio
import json
import pathlib

import pytest

from agent_comms import autorecover, db, dispatch, recovery, requests, workstreams
from agent_comms.core import Conflict, Invalid

from conftest import ASK
from test_autorecover import PAST_GRACE, aenv, git_project, needs_you  # noqa: F401


class Handoff:
    """Request #ask on thread tid, started by codex's interactive session `old`, which abandoned it. Codex's new
    session `new` reclaimed the task in the same checkout, while claude's session `peer` is busy there."""


def blocked_handoff(env, tmp_path) -> Handoff:
    h = Handoff()
    h.project = git_project(tmp_path)
    h.old = env.session("codex", project=h.project)
    h.tid = env.board.create_thread(env.p["human"], env.sid["human"], "audit", h.project)["id"]
    h.task = env.board.create_task(env.p["human"], env.sid["human"], h.tid, title="audit")["id"]
    env.board.claim_task(env.p["codex"], h.old, h.task)
    h.ask = env.post("human", h.tid, "run the audit", "request", to=["codex"], task_id=h.task)
    requests.progress(env.board, env.p["codex"], h.old, h.ask["id"], "codex", "started", "on it")
    env.clock.advance(PAST_GRACE)
    h.new = env.session("codex", project=h.project)
    env.board.claim_task(env.p["codex"], h.new, h.task)
    h.peer = env.session("claude", project=h.project)      # live, activity unknown: a transient blocker
    return h


def auto_posts(env):
    """Posts the dispatcher made as the human (automatic recoveries), on any thread."""
    return [env.board.get_post(env.p["human"], r[0]) for r in env.board.conn.execute(
        "SELECT id FROM posts WHERE EXISTS (SELECT 1 FROM board_state WHERE key = ? || posts.id) ORDER BY id",
        (autorecover.POST_PREFIX,))]


def version(env, post_id):
    return env.board.get_post(env.p["human"], post_id)["requests"][0]["version"]


def attempt(env, h, session=None):
    return recovery.transfer_ended_owner(env.board, env.p["codex"], session or h.new, h.ask["id"], "codex",
                                         version(env, h.ask["id"]))


def wait_of(env, h):
    return recovery._get_state(env.board.conn, recovery.wait_key(h.ask["id"], "codex"))


def blocked(env, h):
    """The transient block an agent hits, then what it does: mark its work blocked, release, stop."""
    with pytest.raises(recovery.RecoveryWait) as caught:
        attempt(env, h)
    env.board.release_task(env.p["codex"], h.new, h.task)
    return caught.value


def go_quiet(env):
    """Every session goes quiet: past the 90-second activity window and the dispatcher's live window."""
    env.clock.advance(3 * 60)


def owner(env, h):
    return env.board.get_post(env.p["human"], h.ask["id"])["requests"][0]["assigned_session"]


# ---------------------------------------------------------------- classification


def test_transient_and_persistent_blockers_are_classified_by_server_constants_only():
    for text in (workstreams.OWNER_LEASE_ACTIVE, recovery.SHARED_LEASE_ACTIVE, workstreams.PEER_ACTIVITY,
                 workstreams.OWNER_RUN_ACTIVE):
        assert recovery.blocker_kind(text) == "transient"
    for text in ("Owner worktree contains unfinished changes; preserve it before takeover",
                 "Owner worktree has an unfinished Git operation", "Owner worktree belongs to another repository",
                 "Owner worktree cannot be inspected", workstreams.UNKNOWN_ACTIVITY, "host denied",
                 workstreams.PEER_ACTIVITY + " (said an agent)", ""):
        assert recovery.blocker_kind(text) == "persistent"


def test_a_transient_block_is_machine_readable_and_records_a_wait_for_the_exact_version(aenv, tmp_path):
    h = blocked_handoff(aenv, tmp_path)
    v = version(aenv, h.ask["id"])
    with pytest.raises(recovery.RecoveryWait) as caught:
        attempt(aenv, h)
    exc = caught.value
    assert isinstance(exc, Conflict)
    assert exc.details["blocker"] == workstreams.PEER_ACTIVITY
    assert (exc.details["blocker_kind"], exc.details["retry"], exc.details["recovered"]) == ("transient", "automatic", False)
    assert exc.details["recovery_wait"] == {"post_id": h.ask["id"], "recipient": "codex", "version": v,
                                            "retries_used": 0, "max_retries": 3, "state": "waiting"}
    assert "Do not ask the human" in exc.message and "Mark the request you are working on blocked" in exc.message
    wait = wait_of(aenv, h)
    assert (wait["state"], wait["version"], wait["agent"], wait["thread_id"]) == ("waiting", v, "codex", h.tid)
    assert (wait["old_session"], wait["session_id"], wait["blocker"]) == (h.old, h.new, workstreams.PEER_ACTIVITY)
    assert wait["worktree"] == str(pathlib.Path(h.project).resolve()) and h.task in wait["covered_task_ids"]
    assert owner(aenv, h) == h.old and version(aenv, h.ask["id"]) == v, "nothing about the request changed"


@pytest.mark.parametrize("why", ["dirty", "git_operation", "denial"])
def test_a_persistent_block_stays_a_plain_conflict_and_records_no_wait(aenv, tmp_path, monkeypatch, why):
    from agent_comms import browser_readiness
    h = blocked_handoff(aenv, tmp_path)
    if why == "dirty":
        aenv.board.release_task(aenv.p["codex"], h.new, h.task)
    aenv.clock.advance(91)      # the peer is quiet: only the persistent blocker is left
    if why == "dirty":
        pathlib.Path(h.project, "notes.md").write_text("left behind")
        # Only an abandoned owner's checkout is checked for changes when the successor works elsewhere.
        h.new = aenv.session("codex", project=h.project, worktree=git_project(tmp_path, "elsewhere"))
    elif why == "git_operation":
        pathlib.Path(h.project, ".git", "MERGE_HEAD").write_text("pending")
    else:
        monkeypatch.setattr(browser_readiness, "request_blocker", lambda *a: "host denied")
    with pytest.raises(Conflict) as caught:
        attempt(aenv, h)
    assert not isinstance(caught.value, recovery.RecoveryWait)
    assert wait_of(aenv, h) is None and owner(aenv, h) == h.old


def test_a_transient_block_is_reported_only_after_the_persistent_checks(aenv, tmp_path, monkeypatch):
    """A relaunch must not run into a persistent blocker afterwards: a browser denial wins over a busy peer."""
    from agent_comms import browser_readiness
    h = blocked_handoff(aenv, tmp_path)
    monkeypatch.setattr(browser_readiness, "request_blocker", lambda *a: "host denied")
    with pytest.raises(Conflict, match="host denied") as caught:
        attempt(aenv, h)
    assert not isinstance(caught.value, recovery.RecoveryWait) and wait_of(aenv, h) is None


def test_a_later_persistent_block_ends_the_wait(aenv, tmp_path):
    h = blocked_handoff(aenv, tmp_path)
    blocked(aenv, h)
    aenv.clock.advance(91)
    aenv.board.claim_task(aenv.p["codex"], h.new, h.task)
    pathlib.Path(h.project, ".git", "MERGE_HEAD").write_text("pending")
    with pytest.raises(Conflict, match="unfinished Git operation"):
        attempt(aenv, h)
    wait = wait_of(aenv, h)
    assert wait["state"] == "persistent" and "unfinished Git operation" in wait["settled_reason"]
    assert recovery.active_wait(aenv.board, "codex", h.tid) is None


# ---------------------------------------------------------------- HTTP and MCP


def test_http_returns_the_transient_block_as_machine_readable_fields(aenv, tmp_path):
    from fastapi.testclient import TestClient
    from agent_comms.api import create_app
    h = blocked_handoff(aenv, tmp_path)
    client = TestClient(create_app(aenv.board))
    r = client.post(f"/api/posts/{h.ask['id']}/requests/recover-owner",
                    headers={"Authorization": f"Bearer {aenv.tokens['codex']}"},
                    json={"recipient": "codex", "session_id": h.new, "expected_version": version(aenv, h.ask["id"])})
    assert r.status_code == 409
    body = r.json()
    assert (body["error"], body["blocker_kind"], body["retry"], body["recovered"]) == ("conflict", "transient", "automatic", False)
    assert body["recovery_wait"]["post_id"] == h.ask["id"] and "Do not ask the human" in body["next_step"]
    # A persistent block keeps today's plain conflict, now labeled.
    pathlib.Path(h.project, ".git", "MERGE_HEAD").write_text("pending")
    aenv.clock.advance(91)
    r = client.post(f"/api/posts/{h.ask['id']}/requests/recover-owner",
                    headers={"Authorization": f"Bearer {aenv.tokens['codex']}"},
                    json={"recipient": "codex", "session_id": h.new, "expected_version": version(aenv, h.ask["id"])})
    assert r.status_code == 409 and r.json()["blocker_kind"] == "persistent" and "retry" in r.json()


def test_mcp_returns_the_transient_block_as_a_result_not_an_error(aenv, tmp_path, monkeypatch):
    from mcp import Client
    from agent_comms.mcp_server import build_mcp
    h = blocked_handoff(aenv, tmp_path)
    monkeypatch.setenv("AGENT_COMMS_TOKEN", aenv.tokens["codex"])

    async def run():
        async with Client(build_mcp(aenv.board, "stdio")) as client:
            return await client.call_tool("board_recover_request_owner", {
                "post_id": h.ask["id"], "recipient": "codex", "expected_version": version(aenv, h.ask["id"]),
                "session_id": h.new})
    result = asyncio.run(run())
    assert not result.is_error
    out = json.loads(result.content[0].text)
    assert (out["recovered"], out["blocker_kind"], out["retry"]) == (False, "transient", "automatic")
    assert wait_of(aenv, h)["state"] == "waiting"


def test_tool_descriptions_say_recovery_is_routine_and_never_a_question(env):
    from agent_comms.mcp_server import build_mcp
    tools = {t.name: t.description for t in asyncio.run(build_mcp(env.board, "stdio").list_tools())}
    for name in ("board_recover_request_owner", "board_post"):
        assert "pre-authorized" in tools[name] and "retry='automatic'" in tools[name]
    assert "never ask the human" in tools["board_recover_request_owner"]


# ---------------------------------------------------------------- the dispatcher retries


def test_relaunch_waits_for_the_worktree_then_launches_once(aenv, tmp_path):
    h = blocked_handoff(aenv, tmp_path)
    blocked(aenv, h)
    aenv.clock.advance(30)
    aenv.board.heartbeat(aenv.p["claude"], h.peer)        # the peer is still busy
    aenv.d.tick()
    assert auto_posts(aenv) == [] and aenv.spawner.calls == [], "not before the worktree is free"
    assert wait_of(aenv, h)["state"] == "waiting"
    go_quiet(aenv)
    aenv.d.tick()
    [post] = auto_posts(aenv)
    assert post["thread_id"] == h.tid
    assert (post["agent"], post["type"], post["to"], post["needs_response"]) == ("human", "request", ["codex"], True)
    assert post["body"].startswith(autorecover.HEADER)
    assert f"recover the request from session {h.old}" in post["body"] and f"#{h.ask['id']}" in post["body"]
    assert "Automatic retry 1 of 3" in post["body"] and "do not ask the human" in post["body"]
    assert [c["cwd"] for c in aenv.spawner.calls] == [h.project], "the same agent, launched once"
    wait = wait_of(aenv, h)
    assert wait["state"] == "relaunched" and wait["relaunch_post_id"] == post["id"] and len(wait["retries"]) == 1
    aenv.clock.advance(60)
    aenv.d.tick()
    assert len(aenv.spawner.calls) == 1 and len(auto_posts(aenv)) == 1, "in flight: no second relaunch"


def test_the_relaunched_run_recovers_and_settles_the_wait(aenv, tmp_path):
    h = blocked_handoff(aenv, tmp_path)
    blocked(aenv, h)
    go_quiet(aenv)
    aenv.d.tick()
    [run] = [r for r in dispatch.list_runs(aenv.board, aenv.p["human"]) if r["agent"] == "codex"]
    fresh = aenv.board.register_session(aenv.p["codex"], h.project, dispatch_run_id=run["run_id"])["session_id"]
    aenv.board.claim_task(aenv.p["codex"], fresh, h.task)
    out = attempt(aenv, h, fresh)
    assert out["assigned_session"] == fresh
    assert wait_of(aenv, h)["state"] == "resolved"
    assert recovery.active_wait(aenv.board, "codex", h.tid) is None
    assert autorecover.list_records(aenv.board, aenv.p["human"]) == []


def test_three_retries_a_day_then_one_click_question_to_the_human(aenv, tmp_path):
    h = blocked_handoff(aenv, tmp_path)
    blocked(aenv, h)
    for n in (1, 2, 3):
        go_quiet(aenv)
        aenv.d.tick()
        assert len(wait_of(aenv, h)["retries"]) == n
        child = aenv.spawner.children[-1]
        child.code = 0                                 # the relaunched run hits the block again and stops
        fresh = aenv.session("codex", project=h.project)
        aenv.board.claim_task(aenv.p["codex"], fresh, h.task)
        aenv.board.heartbeat(aenv.p["claude"], h.peer)
        with pytest.raises(recovery.RecoveryWait) as caught:
            attempt(aenv, h, fresh)
        assert caught.value.details["recovery_wait"]["retries_used"] == n
        aenv.board.release_task(aenv.p["codex"], fresh, h.task)
        aenv.d.tick()                                  # reap the run
    go_quiet(aenv)
    before = len(aenv.spawner.calls)
    aenv.d.tick()
    assert len(aenv.spawner.calls) == before, "no fourth automatic retry"
    wait = wait_of(aenv, h)
    assert wait["state"] == "escalated" and "3 automatic retries" in wait["reason"]
    note = aenv.board.get_post(aenv.p["human"], wait["escalation_post_id"])
    assert (note["agent"], note["type"], note["to"], note["needs_response"]) == ("human", "question", [], True)
    assert note["id"] in needs_you(aenv)
    q = note["decision_question"]
    assert q["recommended_option_id"] == "relaunch"
    relaunch = next(o for o in q["options"] if o["id"] == "relaunch")
    assert relaunch["action"] == {"type": "unstick", "thread_id": h.tid, "agents": ["codex"]}
    # Once escalated, the question refusal stops (the agent may post a question again) and nothing more is launched.
    assert recovery.active_wait(aenv.board, "codex", h.tid) is None
    asker = aenv.session("codex", project=h.project)
    aenv.board.create_post(aenv.p["codex"], asker, body="Anything else?", type="question", thread_id=h.tid,
                           needs_response=True, decision_question=ASK)
    go_quiet(aenv)
    aenv.d.tick()
    assert len(aenv.spawner.calls) == before
    # A new attempt the same day neither re-arms the wait nor asks the human a second time.
    escalations = len(needs_you(aenv))
    fresh = aenv.session("codex", project=h.project)
    aenv.board.claim_task(aenv.p["codex"], fresh, h.task)
    aenv.board.heartbeat(aenv.p["claude"], h.peer)
    with pytest.raises(recovery.RecoveryWait) as caught:
        attempt(aenv, h, fresh)
    assert caught.value.details["retry"] == "escalated"
    assert caught.value.details["escalation_post_id"] == wait["escalation_post_id"]
    again = wait_of(aenv, h)
    assert again["state"] == "escalated" and again["escalation_post_id"] == wait["escalation_post_id"]
    assert again["first_recorded_at"] == wait["first_recorded_at"]
    assert recovery.active_wait(aenv.board, "codex", h.tid) is None
    aenv.board.release_task(aenv.p["codex"], fresh, h.task)
    go_quiet(aenv)
    aenv.d.tick()
    assert len(aenv.spawner.calls) == before and len(needs_you(aenv)) == escalations
    # A day after the first wait, a new transient block starts a fresh wait.
    aenv.clock.advance(24 * 3600)
    later = aenv.session("codex", project=h.project)
    aenv.board.claim_task(aenv.p["codex"], later, h.task)
    aenv.board.heartbeat(aenv.p["claude"], h.peer)
    with pytest.raises(recovery.RecoveryWait) as caught:
        attempt(aenv, h, later)
    assert caught.value.details["retry"] == "automatic" and wait_of(aenv, h)["state"] == "waiting"


def test_the_retry_budget_is_a_rolling_day(aenv, tmp_path):
    h = blocked_handoff(aenv, tmp_path)
    blocked(aenv, h)
    now = aenv.clock()
    wait = wait_of(aenv, h)
    wait["retries"] = [now - 25 * 3600, now - 600, now - 300]       # one of them is older than a day
    with db.write_tx(aenv.board.conn):
        recovery._put_state(aenv.board, recovery.wait_key(h.ask["id"], "codex"), wait, "test")
    go_quiet(aenv)
    aenv.d.tick()
    assert wait_of(aenv, h)["state"] == "relaunched" and len(aenv.spawner.calls) == 1


def test_a_wait_that_never_frees_goes_to_the_human_after_a_day(aenv, tmp_path):
    h = blocked_handoff(aenv, tmp_path)
    blocked(aenv, h)
    aenv.clock.advance(24 * 3600)
    aenv.board.heartbeat(aenv.p["claude"], h.peer)
    aenv.d.tick()
    wait = wait_of(aenv, h)
    assert wait["state"] == "escalated" and "within 24 hours" in wait["reason"] and aenv.spawner.calls == []


@pytest.mark.parametrize("why", ["setting_off", "paused", "fenced"])
def test_no_relaunch_when_switched_off_paused_or_fenced(aenv, tmp_path, why):
    h = blocked_handoff(aenv, tmp_path)
    blocked(aenv, h)
    go_quiet(aenv)
    if why == "setting_off":
        aenv.board.s.auto_recover_stalled_work = False
    elif why == "paused":
        aenv.board.set_paused(aenv.p["human"], True)
    else:
        aenv.board.conn.execute("UPDATE board_state SET value=? WHERE key=?",
                                (json.dumps("x"), dispatch.Dispatcher.OWNER_KEY))
    aenv.d.tick()
    out = autorecover.tick(aenv.board, aenv.p["human"], runner_for=lambda a: ["x"], fence=aenv.d._fence())
    assert out.get("retried", []) == []
    assert auto_posts(aenv) == [] and aenv.spawner.calls == [] and wait_of(aenv, h)["state"] == "waiting"


def test_a_relaunch_guard_rechecks_inside_the_transaction(aenv, tmp_path, monkeypatch):
    """The fence is checked again inside the post's write transaction: a loop that lost the board posts nothing."""
    h = blocked_handoff(aenv, tmp_path)
    blocked(aenv, h)
    go_quiet(aenv)
    real = autorecover._wait_action

    def then_fence(*a, **kw):
        out = real(*a, **kw)
        aenv.board.conn.execute("UPDATE board_state SET value=? WHERE key=?",
                                (json.dumps("x"), dispatch.Dispatcher.OWNER_KEY))
        return out
    monkeypatch.setattr(autorecover, "_wait_action", then_fence)
    autorecover.tick(aenv.board, aenv.p["human"], runner_for=lambda a: ["x"], fence=aenv.d._fence())
    assert auto_posts(aenv) == [] and wait_of(aenv, h)["state"] == "waiting"
    assert not aenv.board.active_dispatch_rules(aenv.p["human"])


def test_work_the_human_never_asked_for_is_not_relaunched(aenv, tmp_path):
    h = blocked_handoff(aenv, tmp_path)
    blocked(aenv, h)
    # An agent's own request on a thread without a dispatch approval or an authorized task.
    aenv.board.conn.execute("UPDATE posts SET agent='claude', task_id=NULL WHERE id=?", (h.ask["id"],))
    go_quiet(aenv)
    aenv.d.tick()
    assert aenv.spawner.calls == []
    wait = wait_of(aenv, h)
    assert wait["state"] == "escalated" and "nothing was launched" in wait["reason"]


def test_a_wait_settles_when_the_request_moves_on(aenv, tmp_path):
    h = blocked_handoff(aenv, tmp_path)
    blocked(aenv, h)
    with db.write_tx(aenv.board.conn):
        row = aenv.board.get_post(aenv.p["human"], h.ask["id"])["requests"][0]
        requests._save(aenv.board, aenv.p["human"], aenv.sid["human"], row, "finished", "done elsewhere", [],
                       "codex", h.old)
    go_quiet(aenv)
    aenv.d.tick()
    assert wait_of(aenv, h)["state"] == "resolved" and aenv.spawner.calls == []


# ---------------------------------------------------------------- the agent may not ask the human meanwhile


def test_a_question_to_the_human_is_refused_while_the_board_retries(aenv, tmp_path):
    h = blocked_handoff(aenv, tmp_path)
    blocked(aenv, h)
    with pytest.raises(Invalid, match="board retries automatically"):
        aenv.board.create_post(aenv.p["codex"], h.new, body="Recover the owner before relaunching?", type="question",
                               thread_id=h.tid, needs_response=True, decision_question=ASK)
    # Only that agent, only that thread, and only while the wait is live.
    aenv.board.create_post(aenv.p["claude"], h.peer, body="Ask?", type="question", thread_id=h.tid,
                           needs_response=True, decision_question=ASK)
    other = aenv.board.create_thread(aenv.p["human"], aenv.sid["human"], "other", h.project)["id"]
    aenv.board.create_post(aenv.p["codex"], h.new, body="Ask?", type="question", thread_id=other,
                           needs_response=True, decision_question=ASK)
    aenv.board.create_post(aenv.p["codex"], h.new, body="Blocked: waiting for the worktree", type="status",
                           thread_id=h.tid)
    with db.write_tx(aenv.board.conn):
        recovery._settle_wait(aenv.board, h.ask["id"], "codex", "persistent", "test", "dirty")
    aenv.board.create_post(aenv.p["codex"], h.new, body="Ask now?", type="question", thread_id=h.tid,
                           needs_response=True, decision_question=ASK)


# ---------------------------------------------------------------- the dashboard's records


def test_list_records_shows_the_wait_for_the_dashboard(aenv, tmp_path):
    h = blocked_handoff(aenv, tmp_path)
    blocked(aenv, h)
    [item] = autorecover.list_records(aenv.board, aenv.p["human"])
    assert (item["kind"], item["state"], item["request_post_id"], item["agent"]) == ("recovery_wait", "waiting", h.ask["id"], "codex")
    assert (item["retries_used"], item["max_retries"], item["reason"]) == (0, 3, workstreams.PEER_ACTIVITY)
    assert item["owner_session"] == h.old and h.task in item["covered_task_ids"]
    go_quiet(aenv)
    aenv.d.tick()
    [item] = autorecover.list_records(aenv.board, aenv.p["human"])
    assert (item["state"], item["retries_used"]) == ("relaunched", 1) and isinstance(item["post_id"], int)


def test_another_dispatched_run_in_the_checkout_keeps_it_waiting(aenv, tmp_path):
    h = blocked_handoff(aenv, tmp_path)
    blocked(aenv, h)
    go_quiet(aenv)
    run = {"run_id": "s9-claude", "agent": "claude", "thread_id": h.tid, "cwd": h.project, "status": "running",
           "pid": None, "started_at": aenv.clock()}
    aenv.board.conn.execute("INSERT INTO board_state(key,value,updated_by,updated_at) VALUES (?,?,?,?)",
                            ("dispatch.run.s9-claude", json.dumps(run), "dispatcher", aenv.clock()))
    out = autorecover.tick(aenv.board, aenv.p["human"], runner_for=lambda a: ["x"], fence=aenv.d._fence())
    assert out["retried"] == [] and auto_posts(aenv) == []
    assert wait_of(aenv, h)["state"] == "waiting"
    assert recovery.transient_blocker(aenv.board, wait_of(aenv, h)) == workstreams.OWNER_RUN_ACTIVE
    run["status"] = "exited"
    aenv.board.conn.execute("UPDATE board_state SET value=? WHERE key='dispatch.run.s9-claude'", (json.dumps(run),))
    out = autorecover.tick(aenv.board, aenv.p["human"], runner_for=lambda a: ["x"], fence=aenv.d._fence())
    assert len(out["retried"]) == 1


# ---------------------------------------------------------------- review fixes: a relaunch never blocks itself


def put_run(env, run_id, **fields):
    value = {"run_id": run_id, "agent": "codex", "status": "running", "pid": None, "started_at": env.clock(), **fields}
    env.board.conn.execute("""INSERT INTO board_state(key,value,updated_by,updated_at) VALUES (?,?,?,?)
                              ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
                           ("dispatch.run." + run_id, json.dumps(value), "dispatcher", env.clock()))


def test_a_relaunched_run_recovers_an_abandoned_owner_without_claiming_first(aenv, tmp_path):
    """Regression (review P1a): in the same directory, with no task claim, the relaunched session and its own active
    dispatcher run are not "another session busy in the checkout"."""
    h = blocked_handoff(aenv, tmp_path)
    blocked(aenv, h)
    go_quiet(aenv)
    aenv.d.tick()
    [run] = [r for r in dispatch.list_runs(aenv.board, aenv.p["human"]) if r["agent"] == "codex"]
    assert run["status"] == "running" and run["cwd"] == h.project
    fresh = aenv.board.register_session(aenv.p["codex"], h.project, dispatch_run_id=run["run_id"])["session_id"]
    out = attempt(aenv, h, fresh)                               # no board_claim_task first
    assert out["assigned_session"] == fresh and wait_of(aenv, h)["state"] == "resolved"


def test_a_dispatched_successor_recovers_a_taskless_request_from_an_ended_run(aenv, tmp_path):
    """Regression (review P1b): an ended dispatcher owner, a request without a task (so no lease is possible), and a
    successor dispatched run in the same directory: it recovers instead of waiting on itself forever."""
    project = git_project(tmp_path)
    tid = aenv.board.create_thread(aenv.p["human"], aenv.sid["human"], "audit", project)["id"]
    put_run(aenv, "s1-codex", thread_id=tid, cwd=project)
    old = aenv.board.register_session(aenv.p["codex"], project, dispatch_run_id="s1-codex")["session_id"]
    ask = aenv.post("human", tid, "run the audit", "request", to=["codex"])
    requests.progress(aenv.board, aenv.p["codex"], old, ask["id"], "codex", "started", "on it")
    requests.progress(aenv.board, aenv.p["codex"], old, ask["id"], "codex", "blocked", "stopped")
    put_run(aenv, "s1-codex", thread_id=tid, cwd=project, status="exited", ended_at=aenv.clock())
    aenv.clock.advance(100)
    put_run(aenv, "s2-codex", thread_id=tid, cwd=project)
    fresh = aenv.board.register_session(aenv.p["codex"], project, dispatch_run_id="s2-codex")["session_id"]
    out = recovery.transfer_ended_owner(aenv.board, aenv.p["codex"], fresh, ask["id"], "codex", version(aenv, ask["id"]))
    assert out["assigned_session"] == fresh and out["state"] == "queued"
    assert recovery._get_state(aenv.board.conn, recovery.wait_key(ask["id"], "codex")) is None


def test_the_successor_exclusion_still_fences_everyone_else(aenv, tmp_path):
    """Excluding the successor does not excuse another busy session, another active run, or the checkout's state."""
    h = blocked_handoff(aenv, tmp_path)
    blocked(aenv, h)
    go_quiet(aenv)
    aenv.d.tick()
    [run] = [r for r in dispatch.list_runs(aenv.board, aenv.p["human"]) if r["agent"] == "codex"]
    fresh = aenv.board.register_session(aenv.p["codex"], h.project, dispatch_run_id=run["run_id"])["session_id"]
    aenv.board.heartbeat(aenv.p["claude"], h.peer)
    with pytest.raises(recovery.RecoveryWait, match="Another live session"):
        attempt(aenv, h, fresh)
    aenv.clock.advance(91)
    aenv.board.heartbeat(aenv.p["codex"], fresh)
    pathlib.Path(h.project, ".git", "MERGE_HEAD").write_text("pending")
    with pytest.raises(Conflict, match="unfinished Git operation") as caught:
        attempt(aenv, h, fresh)
    assert not isinstance(caught.value, recovery.RecoveryWait)


def test_the_relaunch_request_says_to_claim_the_task_first(aenv, tmp_path):
    h = blocked_handoff(aenv, tmp_path)
    blocked(aenv, h)
    go_quiet(aenv)
    aenv.d.tick()
    [post] = auto_posts(aenv)
    assert "claim (or reclaim) the request's task first if it has one" in post["body"]


# ---------------------------------------------------------------- review fixes: P3s


def test_a_persistent_block_does_not_restart_the_24_hour_clock(aenv, tmp_path):
    h = blocked_handoff(aenv, tmp_path)
    blocked(aenv, h)
    first = wait_of(aenv, h)["first_recorded_at"]
    with db.write_tx(aenv.board.conn):
        recovery._settle_wait(aenv.board, h.ask["id"], "codex", "persistent", "test", "dirty")
    aenv.clock.advance(3600)
    aenv.board.claim_task(aenv.p["codex"], h.new, h.task)
    aenv.board.heartbeat(aenv.p["claude"], h.peer)
    with pytest.raises(recovery.RecoveryWait):
        attempt(aenv, h)
    wait = wait_of(aenv, h)
    assert wait["state"] == "waiting" and wait["first_recorded_at"] == first


def test_the_boards_own_automatic_posts_do_not_count_as_the_human_asking(aenv, tmp_path):
    h = blocked_handoff(aenv, tmp_path)
    blocked(aenv, h)
    aenv.board.conn.execute("UPDATE posts SET task_id=NULL WHERE id=?", (h.ask["id"],))
    assert autorecover._wait_authorized(aenv.board, aenv.p["human"], wait_of(aenv, h))
    aenv.board.conn.execute("INSERT INTO board_state(key,value,updated_by,updated_at) VALUES (?,?,?,?)",
                            (autorecover.POST_PREFIX + str(h.ask["id"]), json.dumps({"kind": "recovery"}),
                             "dispatcher", aenv.clock()))
    assert not autorecover._wait_authorized(aenv.board, aenv.p["human"], wait_of(aenv, h))


def test_wait_relaunches_share_the_per_pass_send_cap(aenv, tmp_path, monkeypatch):
    from test_autorecover import abandon
    monkeypatch.setattr(autorecover, "MAX_SENDS_PER_PASS", 1)
    h = blocked_handoff(aenv, tmp_path)
    blocked(aenv, h)
    abandon(aenv, agent="claude")                  # a stall elsewhere, for the other kind of launch
    aenv.clock.advance(PAST_GRACE)
    out = autorecover.tick(aenv.board, aenv.p["human"], runner_for=lambda a: ["x"], fence=aenv.d._fence())
    assert len(out["sent"]) + len(out["retried"]) == 1
    out = autorecover.tick(aenv.board, aenv.p["human"], runner_for=lambda a: ["x"], fence=aenv.d._fence())
    assert len(out["sent"]) + len(out["retried"]) == 1
