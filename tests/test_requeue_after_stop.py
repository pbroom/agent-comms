"""Requests of a run the dispatcher itself stopped go back to the queue and are relaunched once (requeue.py). A run that
exited on its own keeps today's behavior (blocked); a second stop within a day stays blocked; the human's pause and
the auto_recover_stalled_work setting are respected."""

import json

from agent_comms import dispatch, requests, requeue

from conftest import PROJECT
from test_dispatch import allow, denv, human_post, new_dispatcher, runs  # noqa: F401  (denv is a fixture)


def request_row(env, post):
    return env.board.get_post(env.p["human"], post["id"])["requests"][0]


def launches_left(env, rule):
    return next(r for r in env.board.list_dispatch_rules(env.p["human"], include_inactive=True)
                if r["id"] == rule["id"])["launches_left"]


def stop(env):
    """`board dispatch stop` on a running loop: terminate the children, release the loop."""
    env.d.stop_children(sleep=lambda s: None)
    env.d.release_loop()


def next_dispatcher(env):
    """A following dispatcher (the restart), once the stopped run's agent is no longer live."""
    env.clock.advance(5 * 60)
    env.d = new_dispatcher(env)
    env.d.tick()
    return env.d


def launched(env):
    rule = allow(env, agents=["codex"], max_launches=1)     # one launch, like a one-click (Unstick) approval
    ask = human_post(env, ["codex"])
    env.d.tick()
    assert len(env.spawner.calls) == 1
    return rule, ask


def test_a_stopped_run_requeues_its_request_and_a_following_dispatcher_relaunches_it_once(denv):
    rule, ask = launched(denv)
    before = request_row(denv, ask)
    assert launches_left(denv, rule) == 0
    stop(denv)
    [record] = runs(denv)
    assert record["status"] == "stopped"
    row = request_row(denv, ask)
    assert row["state"] == "queued" and row["assigned_session"] is None and row["version"] == before["version"] + 1
    assert row["reason"].startswith("Requeued automatically: the dispatcher stopped run " + record["run_id"])
    [event] = requests.history(denv.board, denv.p["human"], ask["id"], "codex")
    assert event["state"] == "queued" and event["actor"] is None and "Requeued automatically" in event["reason"]
    assert record["requeued"] == [[ask["id"], "codex"]] and record["launch_refunded"] is True
    stored = json.loads(denv.board.conn.execute("SELECT value FROM board_state WHERE key=?",
                                                (requeue.key(ask["id"], "codex"),)).fetchone()[0])
    assert stored == {"at": denv.clock.t, "run_id": record["run_id"]}
    assert launches_left(denv, rule) == 1        # the stopped run's launch was given back to its one-shot rule
    next_dispatcher(denv)
    assert len(denv.spawner.calls) == 2
    relaunch = next(r for r in runs(denv) if r["run_id"] != record["run_id"])
    assert relaunch["request_ids"] == [ask["id"]] and relaunch["status"] == "running"
    assert launches_left(denv, rule) == 0
    # Once only: further passes never relaunch it again while the relaunch runs or after it.
    denv.clock.advance(10 * 60)
    denv.d.tick()
    assert len(denv.spawner.calls) == 2
    # The stopped run's record no longer reblocks the request on later reconciles.
    assert request_row(denv, ask)["state"] == "queued"


def test_a_second_stop_within_a_day_leaves_the_request_blocked(denv):
    rule, ask = launched(denv)
    stop(denv)
    next_dispatcher(denv)
    assert len(denv.spawner.calls) == 2
    stop(denv)
    row = request_row(denv, ask)
    assert row["state"] == "blocked"
    assert row["reason"].startswith("Runner ended without explicit request completion: stopped")
    assert row["reason"].endswith(requeue.USED)
    assert launches_left(denv, rule) == 0        # no refund without a requeue
    next_dispatcher(denv)
    denv.clock.advance(60 * 60)
    denv.d.tick()
    assert len(denv.spawner.calls) == 2
    assert request_row(denv, ask)["state"] == "blocked"


def test_a_run_that_exits_on_its_own_stays_blocked(denv):
    rule, ask = launched(denv)
    denv.spawner.children[0].code = 0
    denv.d.tick()
    assert runs(denv)[0]["status"] == "exited"
    row = request_row(denv, ask)
    assert row["state"] == "blocked" and row["reason"].startswith("Runner ended without explicit request completion: exited")
    assert denv.board.conn.execute("SELECT 1 FROM board_state WHERE key LIKE 'dispatch.requeue.%'").fetchone() is None
    stop(denv)
    next_dispatcher(denv)
    assert len(denv.spawner.calls) == 1 and launches_left(denv, rule) == 0


def test_a_timed_out_run_stays_blocked(denv):
    rule, ask = launched(denv)
    denv.clock.advance(31 * 60)
    denv.d.tick()                              # terminates the child at the timeout
    denv.d.tick()
    assert runs(denv)[0]["status"] == "timeout"
    assert request_row(denv, ask)["state"] == "blocked"


def test_paused_board_requeues_but_waits_to_relaunch(denv):
    rule, ask = launched(denv)
    denv.board.set_paused(denv.p["human"], True)
    stop(denv)
    row = request_row(denv, ask)
    assert row["state"] == "queued" and row["version"] == 1 and row["reason"].startswith("Requeued automatically")
    next_dispatcher(denv)
    denv.clock.advance(5 * 60)
    denv.d.tick()
    assert len(denv.spawner.calls) == 1
    denv.board.set_paused(denv.p["human"], False)
    denv.d.tick()
    assert len(denv.spawner.calls) == 2


def test_setting_off_keeps_todays_blocked_behavior(denv):
    rule, ask = launched(denv)
    denv.board.s.auto_recover_stalled_work = False
    stop(denv)
    row = request_row(denv, ask)
    assert row["state"] == "blocked" and not row["reason"].endswith(requeue.USED)
    assert launches_left(denv, rule) == 0
    next_dispatcher(denv)
    assert len(denv.spawner.calls) == 1


def test_a_request_the_agent_blocked_itself_is_never_requeued(denv):
    rule, ask = launched(denv)
    [record] = runs(denv)
    sid = denv.board.register_session(denv.p["codex"], PROJECT, dispatch_run_id=record["run_id"])["session_id"]
    requests.progress(denv.board, denv.p["codex"], sid, ask["id"], "codex", "blocked", reason="Needs the human")
    stop(denv)
    row = request_row(denv, ask)
    assert row["state"] == "blocked" and row["reason"] == "Needs the human"
    assert launches_left(denv, rule) == 0


def test_a_started_request_of_the_stopped_run_is_requeued_from_its_session(denv):
    rule, ask = launched(denv)
    [record] = runs(denv)
    sid = denv.board.register_session(denv.p["codex"], PROJECT, dispatch_run_id=record["run_id"])["session_id"]
    requests.progress(denv.board, denv.p["codex"], sid, ask["id"], "codex", "started")
    assert request_row(denv, ask)["assigned_session"] == sid
    stop(denv)
    row = request_row(denv, ask)
    assert row["state"] == "queued" and row["assigned_session"] is None and row["assigned_agent"] == "codex"
    denv.clock.advance(5 * 60)            # the stopped run's session is no longer live
    next_dispatcher(denv)
    assert len(denv.spawner.calls) == 2


def test_stop_without_a_running_loop_requeues_through_the_next_dispatcher(denv, monkeypatch):
    rule, ask = launched(denv)
    [child] = denv.spawner.children

    def signal_group(pid, sig):
        if pid == child.pid:
            child.code = -sig

    monkeypatch.setattr(dispatch, "_signal_group", signal_group)
    denv.clock.advance(120)                       # the loop died (crash): its heartbeat is stale
    out = dispatch.request_stop(denv.board, denv.p["human"], denv.config, sleep=lambda s: None,
                                probe=denv.procs.probe)
    assert out["terminated_runs"] == [runs(denv)[0]["run_id"]] and runs(denv)[0]["status"] == "stopped"
    assert request_row(denv, ask)["state"] == "queued"     # untouched until a dispatcher reconciles it
    next_dispatcher(denv)                                   # reconciles (requeue + refund), then launches
    assert len(denv.spawner.calls) == 2
    assert request_row(denv, ask)["reason"].startswith("Requeued automatically")
