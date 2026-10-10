"""Stopping the dispatcher halts by default: the stopped runs' requests stay blocked. Only a maintenance restart
(`board dispatch stop --requeue`) requeues them and lets a following dispatcher relaunch them once (requeue.py). A run
that exited on its own keeps today's behavior; a second stop within a day stays blocked; the human's pause and the
auto_recover_stalled_work setting are respected; a one-click rule never revives for another post."""

import json

from agent_comms import cli, db, dispatch, human_actions, requests, requeue
from agent_comms.dispatch import Dispatcher

from conftest import PROJECT
from test_dispatch import allow, denv, human_post, new_dispatcher, runs  # noqa: F401  (denv is a fixture)


def request_row(env, post):
    return env.board.get_post(env.p["human"], post["id"])["requests"][0]


def rule_state(env, rule_id):
    return next(r for r in env.board.list_dispatch_rules(env.p["human"], include_inactive=True) if r["id"] == rule_id)


def launches_left(env, rule):
    return rule_state(env, rule["id"])["launches_left"]


def restart_stop(env):
    """`board dispatch stop --requeue` on a running loop: terminate the children for a restart, release the loop."""
    env.d.stop_children(sleep=lambda s: None, requeue_requests=True)
    env.d.release_loop()


def halt_stop(env):
    """A plain `board dispatch stop`."""
    env.d.stop_children(sleep=lambda s: None)
    env.d.release_loop()


def next_dispatcher(env):
    """A following dispatcher (the restart), once the stopped run's agent is no longer live."""
    env.clock.advance(5 * 60)
    env.d = new_dispatcher(env)
    env.d.tick()
    return env.d


def launched(env):
    rule = allow(env, agents=["codex"], max_launches=1)
    ask = human_post(env, ["codex"])
    env.d.tick()
    assert len(env.spawner.calls) == 1
    return rule, ask


def codex_runs_for(env, post_id):
    return [r for r in runs(env) if r["agent"] == "codex" and post_id in r.get("request_ids", [])]


# ---------------------------------------------------------------- a maintenance restart requeues once


def test_a_restart_requeues_its_request_and_a_following_dispatcher_relaunches_it_once(denv):
    rule, ask = launched(denv)
    before = request_row(denv, ask)
    assert launches_left(denv, rule) == 0
    restart_stop(denv)
    [record] = runs(denv)
    assert record["status"] == "stopped" and record["stop_mode"] == "requeue"
    row = request_row(denv, ask)
    assert row["state"] == "queued" and row["assigned_session"] is None and row["version"] == before["version"] + 1
    assert row["reason"].startswith("Requeued automatically: a maintenance restart")
    [event] = requests.history(denv.board, denv.p["human"], ask["id"], "codex")
    assert event["state"] == "queued" and event["actor"] is None and "Requeued automatically" in event["reason"]
    assert record["requeued"] == [[ask["id"], "codex"]] and record["launch_refunded"] is True
    assert record["requeue_decided"] == [ask["id"]] and record["one_click"] is False
    stored = json.loads(denv.board.conn.execute("SELECT value FROM board_state WHERE key=?",
                                                (requeue.key(ask["id"], "codex"),)).fetchone()[0])
    assert stored == {"at": denv.clock.t, "run_id": record["run_id"]}
    assert launches_left(denv, rule) == 1        # the stopped launch went back to the rule that launched this post
    next_dispatcher(denv)
    assert len(denv.spawner.calls) == 2
    relaunch = next(r for r in runs(denv) if r["run_id"] != record["run_id"])
    assert relaunch["request_ids"] == [ask["id"]] and relaunch["status"] == "running"
    assert launches_left(denv, rule) == 0
    denv.clock.advance(10 * 60)
    denv.d.tick()
    assert len(denv.spawner.calls) == 2           # once only
    assert request_row(denv, ask)["state"] == "queued"   # the stopped run's record never reblocks it


def test_a_second_restart_within_a_day_leaves_the_request_blocked(denv):
    rule, ask = launched(denv)
    restart_stop(denv)
    next_dispatcher(denv)
    assert len(denv.spawner.calls) == 2
    restart_stop(denv)
    row = request_row(denv, ask)
    assert row["state"] == "blocked"
    assert row["reason"].startswith("Runner ended without explicit request completion: stopped")
    assert row["reason"].endswith(requeue.USED)
    assert launches_left(denv, rule) == 0        # no refund without a requeue
    next_dispatcher(denv)
    denv.clock.advance(60 * 60)
    denv.d.tick()
    assert len(denv.spawner.calls) == 2 and request_row(denv, ask)["state"] == "blocked"


def test_the_requeued_ids_are_reported_to_the_cli(denv):
    rule, ask = launched(denv)
    asked = denv.clock.t

    def sleep(_):
        dispatch.set_stop_flag(denv.board, denv.p["human"], requeue_requests=True)

    denv.d.run_forever(sleep=sleep)
    assert requeue.requeued_since(denv.board, asked) == [
        {"post_id": ask["id"], "recipient": "codex", "run_id": runs(denv)[0]["run_id"]}]
    assert request_row(denv, ask)["state"] == "queued"


# ---------------------------------------------------------------- halting is the default


def test_a_plain_stop_halts_no_requeue_no_refund(denv):
    rule, ask = launched(denv)

    def sleep(_):
        dispatch.request_stop(denv.board, denv.p["human"], denv.config, wait_seconds=0, sleep=lambda s: None)

    denv.d.run_forever(sleep=sleep)
    [record] = runs(denv)
    assert record["status"] == "stopped" and record["stop_mode"] == "halt"
    row = request_row(denv, ask)
    assert row["state"] == "blocked" and not row["reason"].endswith(requeue.USED)
    assert launches_left(denv, rule) == 0
    next_dispatcher(denv)
    assert len(denv.spawner.calls) == 1


def test_ctrl_c_or_sigterm_never_requeues(denv):
    rule, ask = launched(denv)

    def sleep(_):
        denv.d.stopping = True        # what the CLI's SIGINT/SIGTERM handler does

    denv.d.run_forever(sleep=sleep)
    assert runs(denv)[0]["stop_mode"] == "halt" and request_row(denv, ask)["state"] == "blocked"


def test_a_plain_stop_with_no_loop_running_halts(denv, monkeypatch):
    rule, ask = launched(denv)
    _kill_on_signal(denv, monkeypatch)
    denv.clock.advance(120)
    out = dispatch.request_stop(denv.board, denv.p["human"], denv.config, sleep=lambda s: None, probe=denv.procs.probe)
    assert out["requeue"] is False and "requeue_pending" not in out
    next_dispatcher(denv)
    assert runs(denv)[0]["stop_mode"] == "halt"
    assert request_row(denv, ask)["state"] == "blocked" and len(denv.spawner.calls) == 1


def test_never_requeued_when_the_board_is_paused_at_stop_time(denv):
    rule, ask = launched(denv)
    denv.board.set_paused(denv.p["human"], True)
    restart_stop(denv)
    assert runs(denv)[0]["stop_mode"] == "halt"
    assert request_row(denv, ask)["state"] == "blocked" and launches_left(denv, rule) == 0
    denv.board.set_paused(denv.p["human"], False)
    next_dispatcher(denv)
    assert len(denv.spawner.calls) == 1


def test_paused_after_a_restart_waits_to_relaunch(denv):
    rule, ask = launched(denv)
    restart_stop(denv)
    assert request_row(denv, ask)["reason"].startswith("Requeued automatically")
    denv.board.set_paused(denv.p["human"], True)
    next_dispatcher(denv)
    denv.clock.advance(5 * 60)
    denv.d.tick()
    assert len(denv.spawner.calls) == 1
    denv.board.set_paused(denv.p["human"], False)
    denv.d.tick()
    assert len(denv.spawner.calls) == 2


def test_the_cli_stop_flag_and_paused_refusal(denv):
    denv.d.heartbeat()                         # the loop is running
    denv.board.set_paused(denv.p["human"], True)
    out = dispatch.request_stop(denv.board, denv.p["human"], denv.config, wait_seconds=0, sleep=lambda s: None,
                                requeue_requests=True)
    assert out["requeue"] is False and "paused" in out["requeue_refused"]
    assert json.loads(denv.board.conn.execute("SELECT value FROM board_state WHERE key=?",
                                              (Dispatcher.STOP_KEY,)).fetchone()[0]) is True   # a halt flag


def test_cli_stop_help_and_output(denv, monkeypatch, capsys, tmp_path):
    denv.d.release_loop()
    monkeypatch.setattr(cli, "Settings", type("S", (), {"load": staticmethod(lambda: denv.settings)}))
    monkeypatch.setenv("BOARD_TOKEN", denv.tokens["human"])
    monkeypatch.setenv("AGENT_COMMS_HOME", str(tmp_path / "home"))
    cli.main(["dispatch", "stop", "--requeue"])
    assert "dispatcher is not running" in capsys.readouterr().out
    for argv in (["dispatch", "stop", "--help"], ["dispatch", "--help"]):
        try:
            cli.main(argv)
        except SystemExit:
            pass
    text = capsys.readouterr().out
    assert "--requeue" in text and "maintenance restart" in text and "stay blocked" in text


# ---------------------------------------------------------------- what is never requeued


def test_a_run_that_exits_on_its_own_stays_blocked(denv):
    rule, ask = launched(denv)
    denv.spawner.children[0].code = 0
    denv.d.tick()
    assert runs(denv)[0]["status"] == "exited"
    row = request_row(denv, ask)
    assert row["state"] == "blocked" and row["reason"].startswith("Runner ended without explicit request completion: exited")
    assert denv.board.conn.execute("SELECT 1 FROM board_state WHERE key LIKE 'dispatch.requeue.%'").fetchone() is None
    restart_stop(denv)
    next_dispatcher(denv)
    assert len(denv.spawner.calls) == 1 and launches_left(denv, rule) == 0


def test_a_clean_exit_already_known_at_stop_time_is_recorded_as_exited(denv):
    rule, ask = launched(denv)
    denv.spawner.children[0].code = 0          # it exited by itself; the dispatcher has not reaped it yet
    restart_stop(denv)
    [record] = runs(denv)
    assert record["status"] == "exited" and "stop_mode" not in record
    assert request_row(denv, ask)["state"] == "blocked" and launches_left(denv, rule) == 0


def test_a_timed_out_run_stays_blocked(denv):
    rule, ask = launched(denv)
    denv.clock.advance(31 * 60)
    denv.d.tick()                              # terminates the child at the timeout
    denv.d.tick()
    assert runs(denv)[0]["status"] == "timeout"
    assert request_row(denv, ask)["state"] == "blocked"


def test_setting_off_keeps_todays_blocked_behavior(denv):
    rule, ask = launched(denv)
    denv.board.s.auto_recover_stalled_work = False
    restart_stop(denv)
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
    restart_stop(denv)
    row = request_row(denv, ask)
    assert row["state"] == "blocked" and row["reason"] == "Needs the human"
    assert launches_left(denv, rule) == 0


def test_a_started_request_of_the_stopped_run_is_requeued_from_its_session(denv):
    rule, ask = launched(denv)
    [record] = runs(denv)
    sid = denv.board.register_session(denv.p["codex"], PROJECT, dispatch_run_id=record["run_id"])["session_id"]
    requests.progress(denv.board, denv.p["codex"], sid, ask["id"], "codex", "started")
    assert request_row(denv, ask)["assigned_session"] == sid
    restart_stop(denv)
    row = request_row(denv, ask)
    assert row["state"] == "queued" and row["assigned_session"] is None and row["assigned_agent"] == "codex"
    denv.clock.advance(5 * 60)            # the stopped run's session is no longer live
    next_dispatcher(denv)
    assert len(denv.spawner.calls) == 2


def _kill_on_signal(env, monkeypatch):
    def signal_group(pid, sig):
        for child in env.spawner.children:
            if child.pid == pid:
                child.code = -sig
    monkeypatch.setattr(dispatch, "_signal_group", signal_group)


def test_a_restart_with_no_loop_running_requeues_through_the_next_dispatcher(denv, monkeypatch):
    rule, ask = launched(denv)
    _kill_on_signal(denv, monkeypatch)
    denv.clock.advance(120)                       # the loop died (crash): its heartbeat is stale
    out = dispatch.request_stop(denv.board, denv.p["human"], denv.config, sleep=lambda s: None,
                                probe=denv.procs.probe, requeue_requests=True)
    assert out["terminated_runs"] == [runs(denv)[0]["run_id"]] and runs(denv)[0]["status"] == "stopped"
    assert out["requeue"] is True and out["requeue_pending"] == [ask["id"]]
    assert request_row(denv, ask)["version"] == 0          # untouched until a dispatcher reconciles it
    next_dispatcher(denv)                                   # reconciles (requeue + refund), then launches
    assert len(denv.spawner.calls) == 2
    assert request_row(denv, ask)["reason"].startswith("Requeued automatically")


# ---------------------------------------------------------------- one-click rules never revive for another post


def one_click(env, thread_id, agent="codex"):
    post, rule = human_actions.post_as_human(env.board, env.p["human"], thread_id=thread_id,
                                             body="Unstick: fixed text", type="request", to=[agent],
                                             needs_response=True, launch=[agent], purpose="Unstick this thread")
    return post, rule


def stopped_one_click_run(env, monkeypatch):
    """Review P1's repro, up to the stop: an Unstick rule bound to P launches codex; claude posts Q to codex on the same
    thread; the loop dies and `board dispatch stop --requeue` runs with no loop, so the run is recorded stopped."""
    p_post, rule = one_click(env, env.tid)
    env.d.tick()
    assert [r["request_ids"] for r in runs(env)] == [[p_post["id"]]] and runs(env)[0]["one_click"] is True
    q_post = env.post("claude", env.tid, "please look", "request", to=["codex"], needs_response=True)
    _kill_on_signal(env, monkeypatch)
    env.clock.advance(120)
    dispatch.request_stop(env.board, env.p["human"], env.config, sleep=lambda s: None, probe=env.procs.probe,
                          requeue_requests=True)
    assert runs(env)[0]["status"] == "stopped" and requeue.undecided(runs(env)[0])
    return p_post, q_post, rule


def test_a_one_click_rule_is_not_revived_for_another_post_after_a_stop(denv, monkeypatch):
    p_post, q_post, rule = stopped_one_click_run(denv, monkeypatch)
    # Any other one-click post prunes dead bindings. P's stays: its rule's stopped run is not decided yet.
    other = denv.board.create_thread(denv.p["human"], denv.sid["human"], "other", PROJECT)["id"]
    one_click(denv, other, agent="claude")
    assert human_actions.post_rule_id(denv.board, p_post["id"]) == rule["id"]
    next_dispatcher(denv)                     # requeues P and refunds the bound rule ...
    assert launches_left(denv, rule) == 1
    for child in denv.spawner.children:       # ... the other one-click launch (claude) holds the run directory
        child.code = 0 if child.code is None else child.code
    denv.d.tick()                             # ... then codex is relaunched for P only
    assert len(codex_runs_for(denv, p_post["id"])) == 2
    assert codex_runs_for(denv, q_post["id"]) == []
    assert launches_left(denv, rule) == 0
    denv.clock.advance(60 * 60)
    denv.d.tick()
    assert codex_runs_for(denv, q_post["id"]) == []
    # Decided now: the binding may be pruned later like any other.
    assert not any(requeue.undecided(r) for r in runs(denv))


def test_no_refund_when_the_binding_is_already_gone(denv, monkeypatch):
    p_post, q_post, rule = stopped_one_click_run(denv, monkeypatch)
    with db.write_tx(denv.board.conn) as c:   # a binding removed before this fix (or by hand)
        c.execute("DELETE FROM board_state WHERE key = ?", (human_actions.POST_RULE_PREFIX + str(p_post["id"]),))
    next_dispatcher(denv)
    assert request_row(denv, p_post)["state"] == "queued"      # requeued ...
    assert launches_left(denv, rule) == 0                      # ... but the one-click rule stays spent
    assert runs(denv)[0]["requeue_decided"] == [p_post["id"]] and "launch_refunded" not in runs(denv)[0]
    denv.clock.advance(60 * 60)
    denv.d.tick()
    assert codex_runs_for(denv, q_post["id"]) == [] and len(codex_runs_for(denv, p_post["id"])) == 1


def test_a_prune_between_recording_the_stop_and_the_requeue_keeps_the_binding(denv, monkeypatch):
    p_post, rule = one_click(denv, denv.tid)
    denv.d.tick()
    original = Dispatcher._request_failure

    def racing(self, post_id, *args, **kw):
        # The run is already recorded stopped (its own transaction); another process prunes bindings right now.
        with db.write_tx(denv.board.conn):
            human_actions.prune_post_rules(denv.board, denv.p["human"])
        return original(self, post_id, *args, **kw)

    monkeypatch.setattr(Dispatcher, "_request_failure", racing)
    restart_stop(denv)
    assert human_actions.post_rule_id(denv.board, p_post["id"]) == rule["id"]
    assert launches_left(denv, rule) == 1 and runs(denv)[0]["launch_refunded"] is True
    monkeypatch.setattr(Dispatcher, "_request_failure", original)
    next_dispatcher(denv)
    assert len(codex_runs_for(denv, p_post["id"])) == 2
