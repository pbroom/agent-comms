"""A later run may take over work whose owner ran on a node: the hub cannot see the node's disk, so the node inspects
its checkout when the run ends and reports it (workstreams.checkout_blocker), and the takeover check uses that."""

import subprocess

import pytest

from agent_comms import nodes, workstreams
from agent_comms.core import Invalid

from test_dispatch import allow, human_post, runs
from test_nodes import nenv  # noqa: F401  (fixture)


def git_checkout(path):
    subprocess.run(["git", "init", "-q", path], check=True)
    subprocess.run(["git", "-C", path, "-c", "user.email=a@b", "-c", "user.name=a", "commit", "-q",
                    "--allow-empty", "-m", "init"], check=True)


def node_run(env, dirty=False):
    git_checkout(env.node_dir)
    if dirty:
        open(f"{env.node_dir}/half-done.txt", "w").write("x")
    env.node.hello()
    allow(env, agents=["grok"])
    post = human_post(env, ["grok"])
    env.d.tick()
    env.node.tick()
    env.node_spawner.children[0].code = 0
    env.node.tick()
    env.d.tick()
    [r] = runs(env)
    return r["run_id"]


def owner(env, run_id, last_seen=0):
    return {"id": 99, "worktree": env.node_dir, "project": env.node_dir, "agent": "grok",
            "dispatch_run_id": run_id, "last_seen": last_seen}


def managed(env):
    return {"post_id": 1, "thread_id": env.tid}


def test_the_node_reports_a_clean_checkout_and_the_takeover_may_proceed(nenv, tmp_path, monkeypatch):
    run_id = node_run(nenv)
    assert nodes.run(nenv.board, run_id)["checkout"] == {"clean": True, "detail": ""}
    monkeypatch.setattr(workstreams.os.path, "isdir", lambda p: False)   # the hub has no such directory
    assert workstreams._inactive(nenv.board, owner(nenv, run_id), managed(nenv)) is None


def test_unfinished_changes_on_the_node_block_the_takeover_with_the_reason(nenv):
    run_id = node_run(nenv, dirty=True)
    assert nodes.run(nenv.board, run_id)["checkout"]["clean"] is False
    assert "unfinished changes" in workstreams._inactive(nenv.board, owner(nenv, run_id), managed(nenv))


def test_without_the_nodes_report_it_still_cannot_be_inspected(nenv):
    run_id = node_run(nenv)
    r = nodes.run(nenv.board, run_id)
    r.pop("checkout")
    with nenv.board.conn:
        nenv.board.conn.execute("UPDATE board_state SET value=? WHERE key=?",
                                (__import__("json").dumps(r), nodes.RUN_PREFIX + run_id))
    assert workstreams._inactive(nenv.board, owner(nenv, run_id), managed(nenv)) == 'Owner worktree cannot be inspected'


def test_a_checkout_report_is_validated(nenv):
    nenv.node.hello()
    for bad in ({"clean": "yes"}, {"clean": True, "extra": 1}, {"clean": True, "detail": "x" * 301}):
        with pytest.raises(Invalid):
            nodes.report(nenv.board, nenv.p["laptop"], "s1-grok", "ended", checkout=bad)


def later_run(env, run_id, **fields):
    """Another run of the same project on the same node, after the owner's: it shares the checkout."""
    r = nodes.run(env.board, run_id) | {"run_id": "s2-grok", "created_at": nodes.run(env.board, run_id)["created_at"] + 5}
    r.update(fields)
    with env.board.conn:
        env.board.conn.execute("INSERT INTO board_state(key,value,updated_by,updated_at) VALUES(?,?,?,?)",
                               (nodes.RUN_PREFIX + "s2-grok", __import__("json").dumps(r), "dispatcher", r["created_at"]))


def test_a_later_run_that_left_the_shared_checkout_dirty_blocks_the_takeover(nenv):
    run_id = node_run(nenv)
    later_run(nenv, run_id, checkout={"clean": False, "detail": "Owner worktree has an unfinished Git operation"})
    assert workstreams._inactive(nenv.board, owner(nenv, run_id), managed(nenv)) == \
        "Owner worktree has an unfinished Git operation"


def test_a_later_run_still_going_in_the_shared_checkout_blocks_the_takeover(nenv):
    run_id = node_run(nenv)
    later_run(nenv, run_id, state="running", checkout=None)
    assert workstreams._inactive(nenv.board, owner(nenv, run_id), managed(nenv)) == 'Owner worktree cannot be inspected'


def test_a_later_clean_run_still_allows_the_takeover(nenv, monkeypatch):
    run_id = node_run(nenv)
    later_run(nenv, run_id, checkout={"clean": True, "detail": ""})
    monkeypatch.setattr(workstreams.os.path, "isdir", lambda p: False)
    assert workstreams._inactive(nenv.board, owner(nenv, run_id), managed(nenv)) is None


def started_run(env):
    git_checkout(env.node_dir)
    env.node.hello()
    allow(env, agents=["grok"])
    human_post(env, ["grok"])
    env.d.tick()
    env.node.tick()
    [r] = runs(env)
    return r["run_id"]


def test_a_node_shutting_down_still_reports_the_checkout(nenv):
    run_id = started_run(nenv)
    nenv.node_spawner.children[0].code = -15
    nenv.node.stop_all()
    r = nodes.run(nenv.board, run_id)
    assert r["state"] == "ended" and r["checkout"] == {"clean": True, "detail": ""}


def test_a_restarted_node_reports_the_checkout_of_a_run_it_stopped(nenv):
    from test_nodes import Clock, FakeSpawner
    from agent_comms.node import Node
    run_id = started_run(nenv)
    open(f"{nenv.node_dir}/half-done.txt", "w").write("x")
    fresh = Node(nenv.node.client, nenv.node.config, nenv.node.log_dir, spawner=FakeSpawner(), clock=Clock(nenv),
                 probe=lambda p, start: "dead", start_of=lambda p: "s", signal_group=lambda p, s: None,
                 sleep=lambda s: None)
    assert fresh.recover() == [run_id]
    fresh.tick()
    r = nodes.run(nenv.board, run_id)
    assert r["state"] == "ended" and r["checkout"]["clean"] is False


def test_the_successors_own_run_in_the_shared_checkout_does_not_block_its_takeover(nenv, monkeypatch):
    run_id = node_run(nenv)
    later_run(nenv, run_id, state="running", checkout=None)        # s2-grok: the successor, recovering the work
    monkeypatch.setattr(workstreams.os.path, "isdir", lambda p: False)
    assert workstreams._inactive(nenv.board, owner(nenv, run_id), managed(nenv), own_run_id="s2-grok") is None


def drop_owner_record(env, run_id):
    with env.board.conn:
        env.board.conn.execute("DELETE FROM board_state WHERE key=?", (nodes.RUN_PREFIX + run_id,))


def test_a_later_report_still_counts_once_the_owners_node_record_is_pruned(nenv, monkeypatch):
    run_id = node_run(nenv)
    later_run(nenv, run_id, checkout={"clean": True, "detail": ""})
    drop_owner_record(nenv, run_id)
    monkeypatch.setattr(workstreams.os.path, "isdir", lambda p: False)
    assert workstreams._inactive(nenv.board, owner(nenv, run_id), managed(nenv)) is None


def test_with_no_report_left_at_all_it_cannot_be_inspected(nenv):
    run_id = node_run(nenv)
    drop_owner_record(nenv, run_id)
    assert workstreams._inactive(nenv.board, owner(nenv, run_id), managed(nenv)) == 'Owner worktree cannot be inspected'
