"""One dispatched run per run directory (DESIGN_NOTES "Automatic owner handoff"): runs for threads that share a
checkout queue behind each other, like they do behind max_concurrent and one run per agent, and launch in order once
the directory is free. Runs in different directories are unaffected."""

from test_dispatch import allow, denv, human_post, restart, runs  # noqa: F401


def second_thread(env, project=None):
    """Another thread: in the same project (so the same run directory) unless `project` is given."""
    if project is None:
        return env.thread("another audit in the same checkout")
    return env.board.create_thread(env.p["human"], env.sid["human"], "elsewhere", project)["id"]


def test_a_run_for_another_thread_in_the_same_directory_waits_until_it_is_free(denv):
    other = second_thread(denv)
    allow(denv, agents=["codex", "claude"])
    allow(denv, agents=["codex", "claude"], thread_id=other)
    human_post(denv, ["codex"])
    human_post(denv, ["claude"], thread_id=other)
    denv.d.tick()
    assert denv.spawner.agents() == ["codex-cli-fake"], "max_concurrent is 2, but the directory is in use"
    pending = denv.d._get(denv.d.PENDING_KEY)
    assert [v["thread_id"] for v in pending.values()] == [other], "the trigger is kept, not dropped"
    denv.clock.advance(60)
    denv.d.tick()
    assert denv.spawner.agents() == ["codex-cli-fake"]
    denv.spawner.children[0].code = 0          # codex's run ends: the directory is free
    denv.d.tick()
    assert denv.spawner.agents() == ["codex-cli-fake", "claude-fake"]
    assert {r["thread_id"]: r["cwd"] for r in runs(denv)} == {denv.tid: denv.workdir, other: denv.workdir}


def test_runs_in_different_directories_still_run_side_by_side(denv, tmp_path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    other = second_thread(denv, str(elsewhere))
    allow(denv, agents=["codex", "claude"])
    allow(denv, agents=["codex", "claude"], thread_id=other)
    human_post(denv, ["codex"])
    human_post(denv, ["claude"], thread_id=other)
    denv.d.tick()
    assert denv.spawner.agents() == ["codex-cli-fake", "claude-fake"]
    assert sorted(c["cwd"] for c in denv.spawner.calls) == sorted([denv.workdir, str(elsewhere)])


def test_the_directory_is_compared_by_real_path(denv, tmp_path):
    link = tmp_path / "repo-link"
    link.symlink_to(denv.workdir)
    other = second_thread(denv, str(link))
    allow(denv, agents=["codex", "claude"])
    allow(denv, agents=["codex", "claude"], thread_id=other)
    human_post(denv, ["codex"])
    human_post(denv, ["claude"], thread_id=other)
    denv.d.tick()
    assert denv.spawner.agents() == ["codex-cli-fake"], "a symlink to the same checkout is the same directory"


def test_a_live_run_left_by_an_earlier_dispatcher_holds_its_directory(denv):
    other = second_thread(denv)
    allow(denv, agents=["codex", "claude"])
    allow(denv, agents=["codex", "claude"], thread_id=other)
    human_post(denv, ["codex"])
    denv.d.tick()
    [orphan] = denv.spawner.children
    restart(denv)
    human_post(denv, ["claude"], thread_id=other)
    denv.clock.advance(5 * 60)
    denv.d.tick()
    assert denv.spawner.agents() == ["codex-cli-fake"]
    orphan.code = 0
    denv.d.tick()
    assert denv.spawner.agents() == ["codex-cli-fake", "claude-fake"]


def test_waiting_runs_launch_in_order_when_the_directory_frees(denv):
    """Fairness: the oldest waiting trigger takes the free directory; the next waits for it in turn."""
    second, third = second_thread(denv), second_thread(denv)
    for tid in (denv.tid, second, third):
        allow(denv, agents=["codex", "claude", "grok"], thread_id=tid)
    denv.config.runners["grok"] = ["grok-fake", "{prompt}"]
    human_post(denv, ["codex"])
    denv.d.tick()
    human_post(denv, ["claude"], thread_id=second)
    human_post(denv, ["grok"], thread_id=third)
    denv.d.tick()
    assert denv.spawner.agents() == ["codex-cli-fake"]
    denv.spawner.children[0].code = 0
    denv.d.tick()
    assert denv.spawner.agents() == ["codex-cli-fake", "claude-fake"], "the older trigger goes first"
    denv.spawner.children[1].code = 0
    denv.d.tick()
    assert denv.spawner.agents() == ["codex-cli-fake", "claude-fake", "grok-fake"]
    assert [r["thread_id"] for r in sorted(runs(denv), key=lambda r: r["post_seq"])] == [denv.tid, second, third]
