"""[dispatch] parallel_worktrees and max_runs_per_agent: one agent runs on several threads of one checkout at once,
each (agent, thread) in its own git worktree (Dispatcher._run_dir_for, ensure_run_worktree). Never spawns a real
agent CLI; git is real."""

import os
import subprocess

import pytest

from agent_comms import dispatch
from agent_comms.core import Forbidden
from agent_comms.dispatch import DispatchConfig, run_worktree_path

from conftest import make_env
from test_dispatch import FakeProcs, FakeSpawner, Recorder, new_dispatcher, runs

GRACE = 120


def git(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), "-c", "user.email=t@example.com", "-c", "user.name=t", *args],
                   check=True, capture_output=True)


@pytest.fixture
def wenv(tmp_path, monkeypatch):
    monkeypatch.setattr(dispatch.shutil, "which", lambda executable, **kw: "/fake/" + executable)
    monkeypatch.setattr(dispatch, "git_run_directory_blocker", lambda cwd, env: None)
    env = make_env(tmp_path)
    repo = tmp_path / "code" / "app"
    repo.mkdir(parents=True)
    git(repo, "init", "-q")
    (repo / "README").write_text("hi\n")
    git(repo, "add", "README")
    git(repo, "commit", "-q", "-m", "init")
    env.repo = str(repo)
    env.wt_root = str(tmp_path / "wt")
    env.config = DispatchConfig.from_dict({
        "runners": {"claude": ["claude-fake", "-p", "{prompt}"]}, "checkout_roots": [str(tmp_path / "code")],
        "live_minutes": 2, "timeout_minutes": 30, "max_concurrent": 5})
    env.config.parallel_worktrees, env.config.worktree_dir, env.config.max_runs_per_agent = True, env.wt_root, 2
    env.spawner = FakeSpawner()
    env.rec = Recorder()
    env.board.notifier = env.rec
    env.log_dir = tmp_path / "dispatch-logs"
    env.procs = FakeProcs(env.spawner)
    env.d = new_dispatcher(env)
    env.board.create_delivery_policy(env.p["human"], agent="claude", scope="all_projects")
    env.threads = [env.board.create_thread(env.p["codex"], env.sid["codex"], f"t{i}", env.repo)["id"]
                   for i in range(3)]
    env.clock.advance(5 * 60)
    return env


def ask(env, tid):
    return env.post("codex", tid, "please take this", "request", to=["claude"])


def test_one_agent_runs_on_two_threads_at_once_in_separate_worktrees(wenv):
    for tid in wenv.threads:
        ask(wenv, tid)
    wenv.clock.advance(GRACE + 1)
    wenv.d.tick()
    cwds = [c["cwd"] for c in wenv.spawner.calls]
    assert len(cwds) == 2, "max_runs_per_agent = 2"
    assert cwds == [run_worktree_path(wenv.wt_root, wenv.repo, "claude", t) for t in wenv.threads[:2]]
    for cwd in cwds:
        common = subprocess.run(["git", "-C", cwd, "rev-parse", "--path-format=absolute", "--git-common-dir"],
                                capture_output=True, text=True, check=True).stdout.strip()
        assert os.path.realpath(common) == os.path.realpath(os.path.join(wenv.repo, ".git"))
    assert {r["run_worktree"] for r in runs(wenv)} == set(cwds)
    wenv.spawner.children[0].code = 0
    wenv.d.tick()
    assert len(wenv.spawner.calls) == 3, "a freed slot takes the third thread"


def test_one_run_per_agent_by_default(wenv):
    wenv.d.config.max_runs_per_agent = 1
    for tid in wenv.threads[:2]:
        ask(wenv, tid)
    wenv.clock.advance(GRACE + 1)
    wenv.d.tick()
    assert len(wenv.spawner.calls) == 1


def test_without_parallel_worktrees_runs_share_the_checkout_one_at_a_time(wenv):
    wenv.d.config.parallel_worktrees = False
    for tid in wenv.threads[:2]:
        ask(wenv, tid)
    wenv.clock.advance(GRACE + 1)
    wenv.d.tick()
    assert [c["cwd"] for c in wenv.spawner.calls] == [wenv.repo]


def test_a_run_registers_its_worktree_as_its_project(wenv):
    ask(wenv, wenv.threads[0])
    wenv.clock.advance(GRACE + 1)
    wenv.d.tick()
    [r] = runs(wenv)
    out = wenv.board.register_session(wenv.p["claude"], r["run_worktree"], dispatch_run_id=r["run_id"])
    row = wenv.board.conn.execute("SELECT project, worktree FROM sessions WHERE id=?",
                                  (out["session_id"],)).fetchone()
    assert (row["project"], row["worktree"]) == (wenv.repo, r["run_worktree"])
    with pytest.raises(Forbidden):
        wenv.board.register_session(wenv.p["claude"], "/somewhere/else", dispatch_run_id=r["run_id"])


def test_a_relaunch_on_the_same_thread_reuses_its_worktree_and_a_foreign_dir_is_refused(wenv):
    path = run_worktree_path(wenv.wt_root, wenv.repo, "claude", wenv.threads[0])
    assert dispatch.ensure_run_worktree(wenv.repo, path) == path
    with open(os.path.join(path, "work.txt"), "w") as f:
        f.write("wip")
    assert dispatch.ensure_run_worktree(wenv.repo, path) == path
    assert os.path.exists(os.path.join(path, "work.txt")), "kept, not recreated"
    other = os.path.join(wenv.wt_root, "x", "claude-t9")
    os.makedirs(other)
    with pytest.raises(Exception):
        dispatch.ensure_run_worktree(wenv.repo, other)


def test_worktrees_of_closed_threads_are_pruned_only_when_nothing_would_be_lost(wenv):
    clean = run_worktree_path(wenv.wt_root, wenv.repo, "claude", wenv.threads[0])
    dirty = run_worktree_path(wenv.wt_root, wenv.repo, "claude", wenv.threads[1])
    still_open = run_worktree_path(wenv.wt_root, wenv.repo, "claude", wenv.threads[2])
    for p in (clean, dirty, still_open):
        dispatch.ensure_run_worktree(wenv.repo, p)
    git(clean, "checkout", "-q", "-b", "feature")       # HEAD on a branch: nothing unique in the tree
    open(os.path.join(dirty, "wip.txt"), "w").write("uncommitted")
    for tid in wenv.threads[:2]:
        wenv.board.set_thread_status(wenv.p["human"], tid, "closed")
    wenv.d._prune_worktrees([])
    assert not os.path.exists(clean) and os.path.exists(dirty) and os.path.exists(still_open)


def test_worktree_dir_may_not_be_home_or_protected(tmp_path):
    home = os.path.realpath(os.path.expanduser("~"))
    for bad in (home, os.path.dirname(home), os.path.join(home, ".ssh", "wt")):
        with pytest.raises(ValueError):
            DispatchConfig.from_dict({"parallel_worktrees": True, "worktree_dir": bad})
    ok = DispatchConfig.from_dict({"parallel_worktrees": True, "worktree_dir": str(tmp_path)})
    assert ok.worktree_root() == str(tmp_path)
    with pytest.raises(ValueError):
        DispatchConfig.from_dict({"parallel_worktrees": "yes"})


def test_a_request_with_an_output_in_the_checkout_runs_in_the_checkout(wenv):
    out = os.path.join(wenv.repo, "report.md")
    wenv.post("codex", wenv.threads[0], "write the report", "request", to=["claude"],
              refs=[{"kind": "output", "path": out}])
    ask(wenv, wenv.threads[1])
    wenv.clock.advance(GRACE + 1)
    wenv.d.tick()
    assert [c["cwd"] for c in wenv.spawner.calls] == [
        wenv.repo, run_worktree_path(wenv.wt_root, wenv.repo, "claude", wenv.threads[1])]
