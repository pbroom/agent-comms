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
        "nodes": {"laptop": {"projects": [PROJECT], "agents": ["grok"]}},
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


def test_the_hub_decides_which_projects_and_agents_a_node_may_take(nenv):
    nenv.d.config.nodes = {"laptop": {"projects": ["/work/other"], "agents": ["grok"]}}
    nenv.node.hello()                        # offers PROJECT, which the hub does not allow it
    allow(nenv, agents=["grok"])
    human_post(nenv, ["grok"])
    nenv.d.tick()
    assert all(r.get("node") is None for r in runs(nenv))
    nenv.d.config.nodes = {}                 # no entry: nothing at all
    assert nodes.node_for(nenv.board, PROJECT, "grok", "grok", nenv.d.config.nodes) is None


def test_a_node_identity_cannot_use_board_tools(nenv):
    client = nenv.node.client
    assert client.post("/api/sessions", json={"project": PROJECT}).status_code == 403
    assert client.get("/api/updates", params={"session_id": 1}).status_code == 403


def test_a_stop_before_start_ends_the_run_and_the_node_refuses_it(nenv):
    nenv.node.hello()
    allow(nenv, agents=["grok"])
    human_post(nenv, ["grok"])
    nenv.d.tick()
    run_id = runs(nenv)[0]["run_id"]
    nodes.RemoteChild(nenv.board, run_id).terminate()
    assert nodes.run(nenv.board, run_id)["state"] == "ended"
    nenv.node.tick()
    assert nenv.node_spawner.calls == [], "a stopped assignment is never started"


def test_a_run_the_hub_no_longer_lists_is_stopped_on_the_node(nenv):
    nenv.node.hello()
    allow(nenv, agents=["grok"])
    human_post(nenv, ["grok"])
    nenv.d.tick()
    nenv.node.tick()
    run_id = runs(nenv)[0]["run_id"]
    nodes.end_lost(nenv.board, run_id)      # e.g. the hub counted the node lost during a network drop
    nenv.node.tick()
    assert nenv.node_spawner.children[0].terminated == 1


def test_a_node_out_of_contact_stops_its_own_runs_before_the_hub_counts_them_lost(nenv):
    import httpx as _httpx
    from agent_comms import node as node_mod
    assert node_mod.SELF_STOP_SECONDS < nodes.NODE_LOST_SECONDS
    nenv.node.hello()
    allow(nenv, agents=["grok"])
    human_post(nenv, ["grok"])
    nenv.d.tick()
    nenv.node.tick()
    child = nenv.node_spawner.children[0]

    def down(*a, **kw):
        raise _httpx.ConnectError("hub unreachable")
    nenv.node.client.request = down
    nenv.clock.advance(node_mod.SELF_STOP_SECONDS + 1)
    with pytest.raises(_httpx.ConnectError):
        nenv.node.tick()
    assert child.terminated == 1, "local runs are stopped even while the hub cannot be reached"


def test_a_restarted_node_stops_what_its_predecessor_left_before_reporting(nenv, tmp_path):
    nenv.node.hello()
    allow(nenv, agents=["grok"])
    human_post(nenv, ["grok"])
    nenv.d.tick()
    nenv.node.tick()
    run_id = runs(nenv)[0]["run_id"]
    pid = nenv.node_spawner.children[0].pid
    signals, alive = [], {pid}

    def signal_group(p, sig):
        signals.append((p, sig))
        alive.discard(p)
    fresh = Node(nenv.node.client, nenv.node.config, nenv.node.log_dir, spawner=FakeSpawner(), clock=Clock(nenv),
                 probe=lambda p, start: "ours" if p in alive else "dead", start_of=lambda p: "s",
                 signal_group=signal_group, sleep=lambda s: None)
    import httpx as _httpx
    real = fresh.client.request

    def down(*a, **kw):
        raise _httpx.ConnectError("hub unreachable")
    fresh.client.request = down
    assert fresh.recover() == [run_id], "local cleanup needs no hub"
    import signal as _signal
    assert signals == [(pid, _signal.SIGTERM)]
    assert nodes.run(nenv.board, run_id)["state"] == "running", "reported once the hub answers"
    fresh.client.request = real
    fresh.tick()
    r = nodes.run(nenv.board, run_id)
    assert r["state"] == "ended" and "restarted" in r["error"]


def test_dispatcher_stop_leaves_an_unconfirmed_remote_run_active_for_the_next_one(nenv):
    nenv.node.hello()
    allow(nenv, agents=["grok"])
    human_post(nenv, ["grok"])
    nenv.d.tick()
    nenv.node.tick()
    nenv.d.config.kill_grace_seconds = 0
    nenv.d.stop_children(sleep=lambda s: None)
    [r] = runs(nenv)
    assert r["status"] == "running" and r["stop_mode"], "the agent and directory stay held until the node confirms"
    assert nodes.run(nenv.board, r["run_id"])["stop"] in ("terminate", "kill")
    nenv.node.tick()                          # the node stops it and reports
    nenv.node.tick()
    assert nodes.run(nenv.board, r["run_id"])["state"] == "ended"
    d2 = new_dispatcher(nenv)
    d2.tick()
    assert runs(nenv)[0]["status"] == "stopped"


def test_a_dead_nodes_orphaned_run_ends_even_while_the_hub_keeps_asking_it_to_stop(nenv):
    nenv.node.hello()
    allow(nenv, agents=["grok"])
    human_post(nenv, ["grok"])
    nenv.d.tick()
    nenv.node.tick()
    run_id = runs(nenv)[0]["run_id"]
    d2 = new_dispatcher(nenv)                # the hub restarts; the node then goes away for good
    nenv.clock.advance(31 * 60)              # past the run's timeout: d2 asks the node to stop it, every pass
    for _ in range(5):
        d2.tick()
        nenv.clock.advance(5)
    assert nodes.run(nenv.board, run_id)["state"] == "ended"
    [r] = [x for x in runs(nenv) if x["run_id"] == run_id]
    assert r["status"] in ("gone", "stopped"), "the record ends, freeing the agent and its directory"


def test_the_hub_stopping_a_run_while_the_node_starts_it_stops_it_at_once(nenv):
    nenv.node.hello()
    allow(nenv, agents=["grok"])
    human_post(nenv, ["grok"])
    nenv.d.tick()
    run_id = runs(nenv)[0]["run_id"]
    [r] = nenv.node._call("GET", "/api/nodes/assignments")["runs"]    # the node fetched it while still assigned
    nodes.RemoteChild(nenv.board, run_id).terminate()                  # ... then the hub stopped it
    nenv.node._start(r)
    assert nenv.node_spawner.children[0].terminated == 1


def test_a_report_the_hub_refuses_is_dropped_and_the_node_keeps_working(nenv):
    nenv.node.unreported = ["s999-grok"]        # a run the hub no longer knows (pruned after a long outage)
    nenv.node.tick()
    assert nenv.node.unreported == []
    assert nodes.nodes(nenv.board)[0]["node"] == "laptop", "hello still went through"
