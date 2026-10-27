"""Standing delivery policies (core.create_delivery_policy, Dispatcher._delivery_rule_for): the human's standing OK to
launch an agent with its normal runner, in the project's checkout, for requests addressed to it that nobody picks up.
Never spawns a real agent CLI."""

import pytest

from agent_comms import dispatch
from agent_comms.core import DELIVERY_PURPOSE, Conflict, Forbidden, Invalid
from agent_comms.dispatch import DispatchConfig, build_prompt

from conftest import PROJECT, make_env
from test_dispatch import FakeProcs, FakeSpawner, Recorder, allow, new_dispatcher, runs

GRACE = 120


@pytest.fixture
def penv(tmp_path, monkeypatch):
    monkeypatch.setattr(dispatch.shutil, "which", lambda executable, **kw: "/fake/" + executable)
    monkeypatch.setattr(dispatch, "git_run_directory_blocker", lambda cwd, env: None)
    env = make_env(tmp_path)
    workdir = tmp_path / "repo"
    workdir.mkdir()
    env.workdir = str(workdir)
    env.config = DispatchConfig.from_dict({
        "runners": {"claude": ["claude-fake", "-p", "{prompt}"], "grok": ["grok-fake", "{prompt}"]},
        "worktrees": {PROJECT: str(workdir)}, "live_minutes": 2, "timeout_minutes": 30, "max_concurrent": 2})
    env.spawner = FakeSpawner()
    env.rec = Recorder()
    env.board.notifier = env.rec
    env.log_dir = tmp_path / "dispatch-logs"
    env.procs = FakeProcs(env.spawner)
    env.d = new_dispatcher(env)
    env.tid = env.thread("t", as_="codex")      # an agent's thread: no workstream approval exists
    env.d.tick()
    env.clock.advance(5 * 60)                   # setup sessions are idle now
    return env


def policy(env, agent="claude", **kw):
    kw.setdefault("project", PROJECT)
    if kw.get("scope") == "all_projects":
        kw.pop("project")
    return env.board.create_delivery_policy(env.p["human"], agent=agent, **kw)


def handoff(env, to="claude"):
    return env.post("codex", env.tid, "please take this over", "handoff", to=[to])


def test_an_unpicked_request_launches_the_agent_in_the_project_checkout_after_the_grace(penv):
    rule = policy(penv)
    post = handoff(penv)
    penv.clock.advance(GRACE - 10)
    penv.d.tick()
    assert penv.spawner.calls == [], "a live session gets the grace period to pick it up first"
    penv.clock.advance(20)
    penv.d.tick()
    [call] = penv.spawner.calls
    assert call["cwd"] == penv.workdir
    assert call["argv"] == ["claude-fake", "-p", build_prompt(penv.tid, rule["id"], DELIVERY_PURPOSE, [post["id"]],
                                                                f"s{post['seq']}-claude")]
    [r] = runs(penv)
    assert r["rule_id"] == rule["id"] and r["status"] == "running"


def test_a_live_session_in_another_project_does_not_hold_it_back(penv):
    policy(penv)
    handoff(penv)
    penv.clock.advance(GRACE + 1)
    penv.session("claude", "/work/elsewhere")     # live, but cannot read this project's request for it
    penv.d.tick()
    assert penv.spawner.agents() == ["claude-fake"]


def test_a_live_session_in_the_project_is_left_to_pick_it_up(penv):
    policy(penv)
    handoff(penv)
    penv.clock.advance(GRACE + 1)
    penv.session("claude", PROJECT)
    penv.d.tick()
    assert penv.spawner.calls == []


def test_only_requests_after_the_policy_and_in_its_scope(penv):
    handoff(penv)                                   # before the policy
    penv.clock.advance(1)
    policy(penv)
    other = penv.board.create_thread(penv.p["codex"], penv.sid["codex"], "other", "/work/other")["id"]
    penv.post("codex", other, "elsewhere", "handoff", to=["claude"])   # another project
    handoff(penv, to="grok")                        # another agent
    penv.clock.advance(GRACE + 1)
    penv.d.tick()
    assert penv.spawner.calls == []


def test_all_projects_scope_and_a_project_policy_wins(penv):
    wide = policy(penv, scope="all_projects")
    narrow = policy(penv)
    post = handoff(penv)
    penv.clock.advance(GRACE + 1)
    penv.d.tick()
    assert runs(penv)[0]["rule_id"] == narrow["id"] != wide["id"]
    assert post["id"]


def test_the_daily_budget_holds(penv, monkeypatch):
    from agent_comms import requeue
    monkeypatch.setattr(requeue, "MAX_RETRIES", 0)     # the ended run's request is not relaunched (refunded)
    policy(penv, daily_launch_limit=1)
    handoff(penv)
    penv.clock.advance(GRACE + 1)
    penv.d.tick()
    penv.spawner.children[0].code = 0
    penv.d.tick()
    penv.clock.advance(penv.config.live_minutes * 60 + 1)
    handoff(penv)
    penv.clock.advance(GRACE + 1)
    penv.d.tick()
    assert len(penv.spawner.calls) == 1, "one launch a day"
    penv.clock.advance(24 * 3600)
    penv.d.tick()
    assert len(penv.spawner.calls) == 2, "the budget resets the next UTC day"


def test_a_workstream_approval_still_wins(penv):
    policy(penv)
    rule = allow(penv, agents=["claude"])
    penv.clock.advance(1)
    handoff(penv)
    penv.d.tick()                                   # an ordinary rule needs no grace period
    assert runs(penv)[0]["rule_id"] == rule["id"]


def test_policies_are_the_humans_to_make_one_per_scope(penv):
    with pytest.raises(Forbidden):
        penv.board.create_delivery_policy(penv.p["codex"], agent="claude", project=PROJECT)
    with pytest.raises(Invalid):
        policy(penv, agent="human")
    with pytest.raises(Invalid):
        policy(penv, project="relative")
    with pytest.raises(Invalid):
        policy(penv, daily_launch_limit=0)
    first = policy(penv)
    with pytest.raises(Conflict):
        policy(penv)
    penv.board.revoke_delivery_policy(penv.p["human"], first["id"])
    assert policy(penv)["state"] == "active"


def test_a_covered_request_never_raises_the_undelivered_alert(penv):
    from agent_comms import delivery
    penv.d.config.undelivered_alert_minutes = 1
    penv.d.tick()                                   # sets the alert watermark
    policy(penv)
    penv.clock.advance(1)
    handoff(penv)
    penv.session("claude", PROJECT)                 # live in the project: the policy waits, nobody reads
    penv.clock.advance(GRACE + 1)
    penv.board.conn.execute("UPDATE sessions SET last_seen = ? WHERE agent='claude'", (penv.clock(),))
    penv.d.tick()
    assert not [k for (k,) in penv.board.conn.execute("SELECT key FROM board_state WHERE key LIKE ?",
                                                      (delivery.ALERT_PREFIX + "%",))]


def test_a_node_runs_it_when_the_hub_has_no_checkout(penv, tmp_path):
    from agent_comms import nodes
    from agent_comms.config import create_agent
    node = penv.board.authenticate(create_agent(penv.settings.agents_path, "laptop", "node"))
    nodes.hello(penv.board, node, [PROJECT], ["claude"], 1)
    penv.d.config.worktrees = {}
    penv.d.config.nodes = {"laptop": {"projects": [PROJECT], "agents": ["claude"]}}
    policy(penv)
    handoff(penv)
    penv.clock.advance(GRACE + 1)
    nodes.hello(penv.board, node, [PROJECT], ["claude"], 1)
    penv.d.tick()
    assert penv.spawner.calls == []
    [r] = runs(penv)
    assert r["node"] == "laptop" and nodes.run(penv.board, r["run_id"])["state"] == "assigned"


def test_an_all_projects_policy_runs_only_in_configured_checkouts(penv):
    policy(penv, scope="all_projects")
    home = penv.board.create_thread(penv.p["codex"], penv.sid["codex"], "x", "/Users/someone")["id"]
    penv.post("codex", home, "run in my home directory", "request", to=["claude"])
    penv.clock.advance(GRACE + 1)
    penv.d.tick()
    assert penv.spawner.calls == [] and runs(penv) == [], "a path an agent named is never a run directory"
