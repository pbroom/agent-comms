"""A run on a node starts in that machine's checkout of the project, which the hub cannot see. Its worker may register
with that checkout as its project (the MCP server's default is its cwd): it still belongs to the thread's project."""

import pytest

from agent_comms.core import Forbidden

from conftest import PROJECT
from test_dispatch import allow, human_post, runs
from test_nodes import nenv  # noqa: F401  (fixture)


def started(env):
    env.node.hello()
    allow(env, agents=["grok"])
    human_post(env, ["grok"])
    env.d.tick()
    env.node.tick()
    [r] = runs(env)
    return r["run_id"]


def session_row(env, out):
    return env.board.conn.execute("SELECT project, worktree FROM sessions WHERE id=?", (out["session_id"],)).fetchone()


def test_a_node_worker_registering_its_checkout_as_project_joins_the_threads_project(nenv):
    run_id = started(nenv)
    out = nenv.board.register_session(nenv.p["grok"], nenv.node_dir, dispatch_run_id=run_id)
    row = session_row(nenv, out)
    assert (row["project"], row["worktree"]) == (PROJECT, nenv.node_dir)


def test_a_node_worker_naming_the_threads_project_is_unchanged(nenv):
    run_id = started(nenv)
    out = nenv.board.register_session(nenv.p["grok"], PROJECT, nenv.node_dir, dispatch_run_id=run_id)
    row = session_row(nenv, out)
    assert (row["project"], row["worktree"]) == (PROJECT, nenv.node_dir)


def test_another_agent_still_cannot_use_the_run(nenv):
    run_id = started(nenv)
    with pytest.raises(Forbidden):
        nenv.board.register_session(nenv.p["codex"], nenv.node_dir, dispatch_run_id=run_id)


def test_conflicting_project_and_worktree_are_still_refused(nenv):
    run_id = started(nenv)
    with pytest.raises(Forbidden):
        nenv.board.register_session(nenv.p["grok"], "/somewhere/else", nenv.node_dir, dispatch_run_id=run_id)
