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


def test_a_live_session_in_the_project_that_read_it_is_left_to_pick_it_up(penv):
    policy(penv)
    handoff(penv)
    sid = penv.session("claude", PROJECT)
    penv.board.read_updates(penv.p["claude"], sid)      # handed the request: it has a delivery receipt
    penv.clock.advance(GRACE + 1)
    penv.board.register_session(penv.p["claude"], PROJECT, resume_session_id=sid)   # still live
    penv.d.tick()
    assert penv.spawner.calls == []


def test_a_live_session_that_never_read_it_does_not_hold_it_back(penv):
    policy(penv)
    handoff(penv)
    penv.clock.advance(GRACE + 1)
    penv.session("claude", PROJECT)                     # live in the project, idle: never read the request
    penv.d.tick()
    assert penv.spawner.agents() == ["claude-fake"]


def test_a_workstream_rule_falls_back_to_the_policy_when_the_agent_is_only_live(penv):
    rule = allow(penv, agents=["claude"])
    delivery = policy(penv)
    penv.clock.advance(1)
    handoff(penv)
    penv.session("claude", "/work/elsewhere")           # live elsewhere: the workstream rule waits for it
    penv.d.tick()
    assert penv.spawner.calls == []
    penv.clock.advance(GRACE + 1)
    penv.session("claude", "/work/elsewhere")
    penv.d.tick()
    [r] = runs(penv)
    assert r["rule_id"] == delivery["id"] != rule["id"]


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


def test_no_daily_limit_by_default(penv, monkeypatch):
    from agent_comms import requeue
    monkeypatch.setattr(requeue, "MAX_RETRIES", 0)
    rule = policy(penv)
    assert rule["daily_launch_limit"] is None and rule["launches_left"] is None
    for n in range(1, 4):
        handoff(penv)
        penv.clock.advance(GRACE + 1)
        penv.d.tick()
        assert len(penv.spawner.calls) == n
        penv.spawner.children[-1].code = 0
        penv.d.tick()
        penv.clock.advance(penv.config.live_minutes * 60 + 1)
    [out] = penv.board.list_delivery_policies(penv.p["human"])
    assert out["state"] == "active" and out["launches_left"] is None
    assert policy(penv, agent="grok", daily_launch_limit=500)["daily_launch_limit"] == 500, "no upper bound"


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


def _checkout(root, name):
    repo = root / name
    (repo / ".git").mkdir(parents=True)
    return str(repo)


def test_a_git_checkout_inside_a_checkout_root_needs_no_worktrees_entry(penv, tmp_path):
    root = tmp_path / "code"
    repo = _checkout(root, "app")
    plain = root / "notes"
    plain.mkdir()
    penv.d.config.checkout_roots = [str(root)]
    policy(penv, scope="all_projects")
    for project in (repo, str(plain), str(root)):
        tid = penv.board.create_thread(penv.p["codex"], penv.sid["codex"], "x", project)["id"]
        penv.post("codex", tid, "please do this", "request", to=["claude"])
    penv.clock.advance(GRACE + 1)
    penv.d.tick()
    [call] = penv.spawner.calls
    assert call["cwd"] == repo, "only a Git checkout strictly inside the root, never a plain folder or the root"


def test_discovered_checkouts_refuse_symlinks_and_protected_places(tmp_path, monkeypatch):
    root = tmp_path / "code"
    repo = _checkout(root, "app")
    (root / "link").symlink_to(repo)
    config = DispatchConfig.from_dict({"checkout_roots": [str(root)]})
    assert config.checkout_for(repo) == repo
    assert config.checkout_for(str(root / "link")) is None
    assert config.checkout_for(repo + "/../app") is None
    config.protected_dirs = [repo]
    assert config.checkout_for(repo) is None
    config.worktrees = {"/work/x": "/work/x-run"}
    assert config.checkout_for("/work/x/") == "/work/x-run" and config.run_dir("/nowhere") == "/nowhere"
    for bad in (["/"], ["relative"], [str(root / "missing")], [str(root / "link")], "x"):
        with pytest.raises(ValueError):
            DispatchConfig.from_dict({"checkout_roots": bad})


def test_a_node_takes_projects_inside_its_checkout_roots(penv):
    from agent_comms import nodes
    from agent_comms.config import create_agent
    node = penv.board.authenticate(create_agent(penv.settings.agents_path, "laptop", "node"))
    penv.d.config.worktrees = {}
    penv.d.config.nodes = {"laptop": {"projects": [], "checkout_roots": ["/work"], "agents": ["claude"]}}
    policy(penv)
    handoff(penv)
    penv.clock.advance(GRACE + 1)
    nodes.hello(penv.board, node, [], ["claude"], 1, checkout_roots=["/work"])
    penv.d.tick()
    [r] = runs(penv)
    assert r["node"] == "laptop"
    assert not nodes.grant_covers({"checkout_roots": ["/work"]}, "/work/../etc")
    assert not nodes.grant_covers({"checkout_roots": ["/work"]}, "/work")


def test_a_later_live_reader_holds_it_back_after_the_first_reader_went_quiet(penv):
    policy(penv)
    post = handoff(penv)
    first = penv.session("claude", PROJECT)
    penv.board.read_updates(penv.p["claude"], first)
    penv.clock.advance(GRACE + 1)                       # the first reader is stale now
    second = penv.session("claude", PROJECT)
    penv.board.read_posts(penv.p["claude"], second, [post["id"]])
    penv.d.tick()
    assert penv.spawner.calls == [], "the second, live reader has it"
