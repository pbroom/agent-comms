"""Machine nodes (nodes.py, node.py): the hub's dispatcher assigns a run to a node on another machine; the node starts
it from its own config and reports back. Never spawns a real agent CLI."""

import pytest
from fastapi.testclient import TestClient

from agent_comms import dispatch, nodes
from agent_comms.api import create_app
from agent_comms.config import create_agent
from agent_comms.core import Forbidden, Invalid
from agent_comms.dispatch import DispatchConfig, build_prompt
from agent_comms.node import Node

from conftest import PROJECT, make_env
from test_dispatch import FakeProcs, FakeSpawner, Recorder, allow, human_post, new_dispatcher, runs


class Clock:
    def __init__(self, env):
        self.env = env

    def __call__(self):
        return self.env.clock()


@pytest.fixture
def nenv(tmp_path, monkeypatch):
    monkeypatch.setattr(dispatch.shutil, "which", lambda executable, **kw: "/fake/" + executable)
    monkeypatch.setattr(dispatch, "git_run_directory_blocker", lambda cwd, env: None)
    env = make_env(tmp_path)
    # The hub has runners but no checkout of PROJECT: work there can only run on a node.
    env.config = DispatchConfig.from_dict({
        "runners": {"codex": ["codex-cli-fake", "exec", "{prompt}"]},
        "live_minutes": 2, "timeout_minutes": 30, "kill_grace_seconds": 10, "max_concurrent": 2})
    env.spawner = FakeSpawner()
    env.rec = Recorder()
    env.board.notifier = env.rec
    env.log_dir = tmp_path / "dispatch-logs"
    env.procs = FakeProcs(env.spawner)
    env.d = new_dispatcher(env)
    env.tid = env.thread("t")
    env.d.tick()
    env.clock.advance(5 * 60)
    # The node machine: its own checkout and a runner for an agent the hub has no runner for (grok).
    node_dir = tmp_path / "laptop-checkout"
    node_dir.mkdir()
    env.node_dir = str(node_dir)
    token = create_agent(env.settings.agents_path, "laptop", "node")
    env.p["laptop"] = env.board.authenticate(token)
    env.node_spawner = FakeSpawner()
    client = TestClient(create_app(env.board), base_url="http://127.0.0.1:8787",
                        headers={"Authorization": "Bearer " + token})
    node_config = DispatchConfig.from_dict({
        "runners": {"grok": ["grok-fake", "--prompt", "{prompt}", "--dir", "{project}"]},
        "worktrees": {PROJECT: str(node_dir)}, "max_concurrent": 1})
    env.node = Node(client, node_config, tmp_path / "node-logs", spawner=env.node_spawner, clock=Clock(env))
    return env


def test_only_a_node_identity_may_speak_the_node_protocol(nenv):
    with pytest.raises(Forbidden):
        nodes.hello(nenv.board, nenv.p["grok"], [PROJECT], ["grok"], 1)
    with pytest.raises(Forbidden):
        nodes.assignments(nenv.board, nenv.p["human"])
    with pytest.raises(Invalid):
        nodes.hello(nenv.board, nenv.p["laptop"], ["relative/path"], ["grok"], 1)
    with pytest.raises(Invalid):
        nodes.hello(nenv.board, nenv.p["laptop"], [PROJECT], ["bad name!"], 1)


def test_a_run_for_a_project_only_a_node_has_is_assigned_to_it_and_started_there(nenv):
    nenv.node.hello()
    rule = allow(nenv, agents=["grok"])
    post = human_post(nenv, ["grok"])
    nenv.d.tick()
    assert nenv.spawner.calls == [], "the hub starts nothing itself"
    [r] = runs(nenv)
    run_id = f"s{post['seq']}-grok"
    assert (r["agent"], r["status"], r["node"], r["cwd"]) == ("grok", "running", "laptop", PROJECT)
    nenv.node.tick()
    [call] = nenv.node_spawner.calls
    prompt = build_prompt(nenv.tid, rule["id"], rule["purpose"], [post["id"]], run_id)
    assert call["argv"] == ["grok-fake", "--prompt", prompt, "--dir", nenv.node_dir]
    assert call["cwd"] == nenv.node_dir
    assert nodes.run(nenv.board, run_id)["state"] == "running"
    # The run ends on the node; the hub's dispatcher sees it end like a local child.
    nenv.node_spawner.children[0].code = 0
    nenv.node.tick()
    assert nodes.run(nenv.board, run_id)["state"] == "ended"
    nenv.d.tick()
    [r] = runs(nenv)
    assert r["status"] == "exited" and r["exit_code"] == 0
    [req] = nenv.board.get_post(nenv.p["human"], post["id"])["requests"]
    assert req["state"] in ("blocked", "queued"), "an unsettled request is settled or requeued as for a local run"


def test_a_local_checkout_always_wins(nenv, tmp_path):
    nenv.node.hello()
    local = tmp_path / "hub-checkout"
    local.mkdir()
    nenv.d.config.worktrees = {PROJECT: str(local)}
    nenv.d.config.runners["grok"] = ["grok-hub", "{prompt}"]
    allow(nenv, agents=["grok"])
    human_post(nenv, ["grok"])
    nenv.d.tick()
    assert nenv.spawner.agents() == ["grok-hub"]
    assert "node" not in runs(nenv)[0]


def test_no_live_node_means_no_launch(nenv):
    nenv.node.hello()
    nenv.clock.advance(nodes.NODE_STALE_SECONDS + 1)
    allow(nenv, agents=["grok"])
    human_post(nenv, ["grok"])
    nenv.d.tick()
    assert nenv.spawner.calls == [] and nodes.nodes(nenv.board)[0]["node"] == "laptop"
    assert all(r.get("node") is None for r in runs(nenv))


def test_a_timeout_on_the_hub_stops_the_run_on_the_node(nenv):
    nenv.node.hello()
    allow(nenv, agents=["grok"])
    human_post(nenv, ["grok"])
    nenv.d.tick()
    nenv.node.tick()
    nenv.node.last_hello = nenv.clock()      # keep the node live through the jump below
    nenv.clock.advance(30 * 60)
    nenv.node.hello()
    nenv.d.tick()                            # hub: timeout, asks the node to terminate
    child = nenv.node_spawner.children[0]
    nenv.node.tick()
    assert child.terminated == 1
    nenv.node.tick()                         # the child exited on terminate; reported ended
    nenv.d.tick()
    assert runs(nenv)[0]["status"] == "timeout"


def test_a_silent_node_loses_its_run(nenv):
    nenv.node.hello()
    allow(nenv, agents=["grok"])
    human_post(nenv, ["grok"])
    nenv.d.tick()
    nenv.node.tick()
    nenv.clock.advance(nodes.NODE_LOST_SECONDS + 1)
    nenv.d.tick()
    [r] = runs(nenv)
    assert r["status"] == "exited" and r["exit_code"] == -1
    assert nodes.run(nenv.board, r["run_id"])["error"] == "node_lost"


def test_a_node_without_the_runner_or_project_reports_a_failed_start(nenv):
    nodes.hello(nenv.board, nenv.p["laptop"], [PROJECT], ["grok"], 1)   # claims a runner it does not have
    nenv.node.config.runners = {}
    allow(nenv, agents=["grok"])
    human_post(nenv, ["grok"])
    nenv.d.tick()
    nenv.node.tick()
    [r] = [nodes.run(nenv.board, x["run_id"]) for x in runs(nenv)]
    assert r["state"] == "ended" and "no runner configured" in r["error"]
    assert nenv.node_spawner.calls == []


def test_only_the_assigned_node_may_report(nenv):
    nenv.node.hello()
    allow(nenv, agents=["grok"])
    human_post(nenv, ["grok"])
    nenv.d.tick()
    run_id = runs(nenv)[0]["run_id"]
    other = nenv.board.authenticate(create_agent(nenv.settings.agents_path, "desktop", "node"))
    from agent_comms.core import NotFound
    with pytest.raises(NotFound):
        nodes.report(nenv.board, other, run_id, "ended", exit_code=0)
    assert nodes.assignments(nenv.board, other)["runs"] == []
