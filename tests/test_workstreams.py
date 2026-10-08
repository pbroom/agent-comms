"""Dependent stack work must retain one fenced owner and verifiable completion."""
from __future__ import annotations

from copy import deepcopy
import subprocess
import threading

import pytest

from agent_comms import capabilities, requests
from agent_comms.core import Conflict, Forbidden, Invalid


def root_scope():
    return dict(fix_ref="refs/heads/main", descendants=["refs/heads/child"],
                agents=["codex", "claude"], required_checks=["test"], required_capabilities=["git:write"])


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


@pytest.fixture
def stack(env, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.name", "Test")
    git(repo, "config", "user.email", "test@example.invalid")
    (repo / "tracked").write_text("base\n")
    git(repo, "add", "tracked")
    git(repo, "commit", "-m", "base")
    base = git(repo, "rev-parse", "HEAD")
    (repo / "tracked").write_text("fixed\n")
    git(repo, "commit", "-am", "lower branch fix")
    fix = git(repo, "rev-parse", "HEAD")
    git(repo, "switch", "-c", "child")
    (repo / "child").write_text("descendant\n")
    git(repo, "add", "child")
    git(repo, "commit", "-m", "descendant")
    head = git(repo, "rev-parse", "HEAD")
    git(repo, "switch", "main")
    owner_path = tmp_path / "owner-worktree"
    fallback_path = tmp_path / "fallback-worktree"
    git(repo, "worktree", "add", str(owner_path), "child")
    git(repo, "worktree", "add", "--detach", str(fallback_path), "main")
    for name in env.sid:
        path = owner_path if name == "codex" else fallback_path if name == "claude" else repo
        env.board.register_session(env.p[name], str(repo), str(path),
                                   resume_session_id=env.sid[name])
    thread = env.board.create_thread(env.p["human"], env.sid["human"], "Stack propagation", str(repo))["id"]
    root = env.board.create_task(env.p["human"], env.sid["human"], thread, title="Fix lower branch",
                                 continuation_scope=root_scope())["id"]
    env.board.transition_task(env.p["human"], env.sid["human"], root, "done")
    spec = dict(owner_session=env.sid["codex"], fallback_session=env.sid["claude"],
                root_task_id=root, fix_commit=fix, descendants=["refs/heads/child"],
                required_checks=["test"], required_capabilities=["git:write"], ack_seconds=120)
    return dict(env=env, repo=repo, owner_path=owner_path, fallback_path=fallback_path,
                thread=thread, root=root, spec=spec, fix=fix, head=head, base=base)


def create(stack, author="human", **changes):
    env = stack["env"]
    spec = {**stack["spec"], **changes}
    return env.board.create_post(env.p[author], env.sid[author], thread_id=stack["thread"],
                                 body="Propagate the lower fix to the descendant stack", type="handoff",
                                 to=["codex", "claude"], continuation=spec)


def current(stack, post):
    return stack["env"].board.get_post(stack["env"].p["human"], post["id"])["requests"][0]


def probe(stack, name, activity="idle", **kw):
    env = stack["env"]
    env.board.heartbeat(env.p[name], env.sid[name])
    return capabilities.register(env.board, env.p[name], env.sid[name], ["git:write"],
                                 "Inspected working tree and git write access", activity=activity, **kw)


def route(stack, post, version=None, actor="human"):
    env = stack["env"]
    return capabilities.route(env.board, env.p[actor], env.sid[actor], post["id"], "codex", ["git:write"],
                              current(stack, post)["version"] if version is None else version)


def progress(stack, post, state, actor="codex", **kw):
    env = stack["env"]
    kw.setdefault("expected_version", current(stack, post)["version"])
    if state == "started":
        probe(stack, actor, activity="active")
    result = requests.progress(env.board, env.p[actor], env.sid[actor], post["id"], "codex", state, **kw)
    if state == "started":
        env.board.claim_task(env.p[actor], env.sid[actor], post["task_id"])
    return result


def completion(stack):
    return {"descendants": [{"ref": "refs/heads/child", "head": stack["head"], "contains_fix": True,
                              "checks": {"test": {"head": stack["head"], "status": "passed"}}}]}


def finish(stack, post, **kw):
    env = stack["env"]
    evidence = env.board.create_post(env.p["codex"], env.sid["codex"], thread_id=stack["thread"],
                                     body="Verified descendant ancestry and tests on the recorded head", type="status")
    kw.setdefault("completion", completion(stack))
    kw.setdefault("evidence_post_ids", [evidence["id"]])
    return progress(stack, post, "finished", reason="Descendants verified", **kw)


def test_continuation_creates_one_request_and_dependent_task_even_for_self(stack):
    post = create(stack, author="codex")
    assert len(post["requests"]) == 1
    row = post["requests"][0]
    assert row["assigned_session"] == stack["env"].sid["codex"]
    assert row["recipient"] == "codex"
    task = stack["env"].board.get_task(stack["env"].p["human"], post["task_id"])
    assert task["depends_on"] == [stack["root"]]


def test_duplicate_fix_cannot_create_second_task_or_request(stack):
    post = create(stack)
    try:
        duplicate = create(stack)
    except Conflict:
        duplicate = post
    assert duplicate["id"] == post["id"]
    db = stack["env"].board.conn
    assert db.execute("SELECT COUNT(*) FROM continuations").fetchone()[0] == 1
    assert db.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 2
    assert db.execute("SELECT COUNT(*) FROM request_progress").fetchone()[0] == 1


def test_ack_deadline_does_not_reroute_early_but_clean_checked_out_branch_can_transfer(stack):
    post = create(stack)
    probe(stack, "codex")
    probe(stack, "claude")
    route(stack, post)
    assert current(stack, post)["assigned_session"] == stack["env"].sid["codex"]
    stack["env"].clock.advance(121)
    probe(stack, "codex")
    probe(stack, "claude")
    routed = route(stack, post)
    assert routed["assigned_session"] == stack["env"].sid["claude"]
    assert git(stack["owner_path"], "symbolic-ref", "HEAD") == "refs/heads/child"
    assert git(stack["owner_path"], "status", "--porcelain") == ""


@pytest.mark.parametrize("obstacle", ["active", "unknown", "stale_idle", "untracked", "tracked", "lease"])
def test_takeover_preserves_active_unknown_or_dirty_owner(stack, obstacle):
    post = create(stack)
    env = stack["env"]
    probe(stack, "codex")
    env.clock.advance(121)
    if obstacle != "stale_idle":
        probe(stack, "codex", activity=obstacle if obstacle in ("active", "unknown") else "idle")
    probe(stack, "claude")
    if obstacle == "untracked":
        (stack["owner_path"] / "unfinished").write_text("Do not lose me\n")
    elif obstacle == "tracked":
        (stack["owner_path"] / "tracked").write_text("unfinished changes\n")
    elif obstacle == "lease":
        task = env.board.create_task(env.p["human"], env.sid["human"], stack["thread"], title="Other active work")
        env.board.claim_task(env.p["codex"], env.sid["codex"], task["id"])
    before = git(stack["owner_path"], "status", "--porcelain")
    try:
        route(stack, post)
    except Conflict:
        pass
    assert current(stack, post)["assigned_session"] == env.sid["codex"]
    assert git(stack["owner_path"], "status", "--porcelain") == before


def _tick_rule(stack):
    env = stack["env"]
    return env.board.create_dispatch_rule(env.p["human"], thread_id=stack["thread"], agents=["codex", "claude"],
                                          purpose="Propagate this approved stack fix", max_launches=2)


def test_a_healthy_lease_owner_is_neither_blocked_nor_resequenced(stack, monkeypatch):
    from agent_comms import workstreams
    env = stack["env"]
    _tick_rule(stack)
    post = create(stack)
    progress(stack, post, "started")                         # started, then claimed: a live lease
    env.clock.advance(121)                                   # past the acknowledgement deadline, within the lease
    env.board.heartbeat(env.p["codex"], env.sid["codex"])
    seq = env.board.get_post(env.p["human"], post["id"])["seq"]
    calls = []
    monkeypatch.setattr(workstreams, "_run_git", lambda *a: calls.append(a) or "")
    workstreams.tick(env.board, env.p["human"])
    managed = workstreams.get_for_post(env.board, post["id"])
    lease = env.board._task_row(post["task_id"])["lease_expires_at"]
    assert managed["blocker"] == "" and calls == []
    assert env.board.get_post(env.p["human"], post["id"])["seq"] == seq
    assert managed["deadline"] == lease and current(stack, post)["assigned_session"] == env.sid["codex"]


def test_one_click_launch_rules_never_approve_continuation_reconciliation(stack):
    """An Unstick / Approve & launch rule is bound to its own post (human_actions): it may launch only for that post,
    so it must not count as the dispatch approval that automatic continuation reconciliation requires."""
    from agent_comms import human_actions, workstreams
    env = stack["env"]
    post = create(stack)
    _, one_click = human_actions.post_as_human(
        env.board, env.p["human"], thread_id=stack["thread"], body="Unstick: please continue", type="request",
        to=["codex", "claude"], needs_response=True, launch=["codex", "claude"], purpose="Unstick this thread")
    assert one_click["agents"] == ["codex", "claude"] and one_click["active"]
    env.clock.advance(121)
    probe(stack, "codex")
    probe(stack, "claude")
    workstreams.tick(env.board, env.p["human"])
    assert current(stack, post)["assigned_session"] == env.sid["codex"]     # not taken over
    with pytest.raises(Forbidden, match="lacks current dispatch approval"):
        workstreams.reconcile(env.board, env.p["human"], None, post["id"], current(stack, post)["version"])
    assert current(stack, post)["assigned_session"] == env.sid["codex"]
    _tick_rule(stack)                                                        # an ordinary approval does cover it
    workstreams._backoff(env.board).clear()
    workstreams.tick(env.board, env.p["human"])
    assert current(stack, post)["assigned_session"] == env.sid["claude"]


def test_git_runs_outside_the_write_transaction_and_blocked_ticks_back_off(stack, monkeypatch):
    from agent_comms import workstreams
    env = stack["env"]
    _tick_rule(stack)
    post = create(stack)
    (stack["owner_path"] / "unfinished").write_text("keep me\n")    # a lasting blocker
    env.clock.advance(121)
    probe(stack, "codex")
    probe(stack, "claude")
    real, calls = workstreams._run_git, []

    def spy(path, *args):
        assert not env.board.conn.in_transaction, "git ran inside the write transaction"
        calls.append(args)
        return real(path, *args)
    monkeypatch.setattr(workstreams, "_run_git", spy)
    workstreams.tick(env.board, env.p["human"])
    assert "unfinished changes" in workstreams.get_for_post(env.board, post["id"])["blocker"]
    first = len(calls)
    assert first > 0
    seq = env.board.get_post(env.p["human"], post["id"])["seq"]
    passes = 0
    for _ in range(60):                                      # five minutes of 5-second dispatcher ticks
        before = len(calls)
        env.clock.advance(5)
        probe(stack, "codex")                                # the owner stays idle: the same blocker throughout
        workstreams.tick(env.board, env.p["human"])
        passes += len(calls) > before
    assert passes <= 7, passes                               # 5, 10, 20, 40, 80, 160 s ...: not 60 passes
    assert env.board.get_post(env.p["human"], post["id"])["seq"] == seq
    assert current(stack, post)["assigned_session"] == env.sid["codex"]


def test_takeover_rechecks_git_live_when_the_checkout_changed_after_inspection(stack, monkeypatch):
    from agent_comms import workstreams
    env = stack["env"]
    post = create(stack)
    env.clock.advance(121)
    probe(stack, "codex")
    probe(stack, "claude")
    real = workstreams._inspect

    def inspect_then_dirty(board, post_id):
        real(board, post_id)                                  # recorded: the owner checkout is clean
        (stack["owner_path"] / "unfinished").write_text("written after the inspection\n")
    monkeypatch.setattr(workstreams, "_inspect", inspect_then_dirty)
    route(stack, post)
    assert current(stack, post)["assigned_session"] == env.sid["codex"]      # no takeover on stale results
    assert "unfinished changes" in workstreams.get_for_post(env.board, post["id"])["blocker"]
    assert (stack["owner_path"] / "unfinished").exists()


def test_simultaneous_takeover_has_one_winner_and_fences_old_owner(stack):
    post = create(stack)
    env = stack["env"]
    env.clock.advance(121)
    probe(stack, "codex")
    probe(stack, "claude")
    version = current(stack, post)["version"]
    barrier = threading.Barrier(2)
    successes, errors = [], []
    def attempt():
        barrier.wait()
        try:
            successes.append(route(stack, post, version=version))
        except Exception as exc:
            errors.append(exc)
    threads = [threading.Thread(target=attempt) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
        assert not thread.is_alive()
    assert len(successes) == 1
    assert len(errors) == 1 and isinstance(errors[0], Conflict)
    assert current(stack, post)["assigned_session"] == env.sid["claude"]
    with pytest.raises((Conflict, Forbidden)):
        progress(stack, post, "started", expected_version=version)


def test_finished_requires_all_descendants_and_same_head_checks(stack):
    post = create(stack)
    progress(stack, post, "started")
    for mutation in ("missing_descendant", "missing_check", "failed_check", "stale_check", "stale_ref", "false_ancestry"):
        payload = deepcopy(completion(stack))
        descendant = payload["descendants"][0]
        if mutation == "missing_descendant":
            payload["descendants"] = []
        elif mutation == "missing_check":
            descendant["checks"] = {}
        elif mutation == "failed_check":
            descendant["checks"]["test"]["status"] = "failed"
        elif mutation == "stale_check":
            descendant["checks"]["test"]["head"] = stack["base"]
        elif mutation == "stale_ref":
            descendant["head"] = stack["fix"]
        else:
            descendant["contains_fix"] = False
        with pytest.raises((Invalid, Conflict)):
            finish(stack, post, completion=payload)
        assert current(stack, post)["state"] == "started"
    assert finish(stack, post)["state"] == "finished"


def test_completion_checks_actual_ancestry_not_claimed_boolean(stack):
    post = create(stack)
    progress(stack, post, "started")
    git(stack["owner_path"], "switch", "--detach")
    git(stack["repo"], "branch", "-f", "child", stack["base"])
    stack["head"] = stack["base"]
    with pytest.raises((Invalid, Conflict)):
        finish(stack, post)
    assert current(stack, post)["state"] == "started"


def test_completion_requires_exact_version_owner_and_evidence(stack):
    post = create(stack)
    initial = current(stack, post)["version"]
    progress(stack, post, "started")
    with pytest.raises(Conflict):
        finish(stack, post, expected_version=initial)
    with pytest.raises((Invalid, Conflict)):
        finish(stack, post, expected_version=None)
    with pytest.raises((Invalid, Conflict)):
        finish(stack, post, evidence_post_ids=[])
    with pytest.raises((Forbidden, Conflict)):
        finish(stack, post, actor="human")
    assert current(stack, post)["state"] == "started"


def test_unrelated_agent_and_wrong_project_cannot_take_over(stack):
    post = create(stack)
    env = stack["env"]
    env.clock.advance(121)
    probe(stack, "codex")
    probe(stack, "claude")
    with pytest.raises(Forbidden):
        route(stack, post, actor="grok")
    with pytest.raises(Conflict, match="pins this session environment"):
        env.board.register_session(env.p["claude"], "/different/project", resume_session_id=env.sid["claude"])
    assert current(stack, post)["assigned_session"] == env.sid["codex"]


def dispatch_record(stack, status, **kw):
    import json
    env = stack["env"]
    record = dict(agent="codex", thread_id=stack["thread"], status=status, cwd=str(stack["owner_path"]), **kw)
    env.board.conn.execute("INSERT OR REPLACE INTO board_state(key,value,updated_at) VALUES (?,?,?)",
                           ("dispatch.run.owner-run", json.dumps(record), env.clock()))


@pytest.mark.parametrize("status", ["starting", "running", "orphaned"])
def test_dispatch_active_or_unresolved_run_blocks_idle_takeover(stack, status):
    post = create(stack)
    stack["env"].clock.advance(121)
    probe(stack, "codex")
    probe(stack, "claude")
    dispatch_record(stack, status)
    route(stack, post)
    assert current(stack, post)["assigned_session"] == stack["env"].sid["codex"]


def test_server_confirmed_ended_dispatch_can_replace_missing_idle_attestation(stack):
    env = stack["env"]
    dispatch_record(stack, "running")
    earlier = env.sid["codex"]                    # the codex session already registered at the owner checkout
    env.sid["codex"] = env.board.register_session(env.p["codex"], str(stack["repo"]), str(stack["owner_path"]),
                                                 dispatch_run_id="owner-run")["session_id"]
    stack["spec"]["owner_session"] = env.sid["codex"]
    post = create(stack)
    env.clock.advance(121)
    dispatch_record(stack, "exited", ended_at=env.clock())
    probe(stack, "claude")
    # The ended run vouches only for its own session: the earlier session at the same checkout must be shown
    # inactive too (finding: one ended session must not hide a possibly active one).
    assert route(stack, post)["assigned_session"] == env.sid["codex"]
    env.board.heartbeat(env.p["codex"], earlier)
    capabilities.register(env.board, env.p["codex"], earlier, ["git:write"], "Idle at the owner checkout",
                          activity="idle")
    routed = route(stack, post)
    assert routed["assigned_session"] == env.sid["claude"]


def test_blocked_request_can_route_before_ack_deadline(stack):
    post = create(stack)
    progress(stack, post, "blocked", reason="Cannot inspect shell here")
    probe(stack, "codex")
    probe(stack, "claude")
    assert route(stack, post)["assigned_session"] == stack["env"].sid["claude"]


def test_revoked_grant_cannot_be_replaced_by_capability_probe(stack):
    env = stack["env"]
    env.settings.require_human_accept = True
    permission = env.board.create_grant(env.p["human"], project=str(stack["repo"]), category="review",
                                        agents=["codex", "claude"], purpose="Propagate this stack fix")
    root = env.board.create_task(env.p["codex"], env.sid["codex"], stack["thread"],
                                 title="Approved scoped fix", category="review", continuation_scope=root_scope())["id"]
    env.board.claim_task(env.p["codex"], env.sid["codex"], root)
    env.board.transition_task(env.p["codex"], env.sid["codex"], root, "done")
    post = create(stack, author="codex", root_task_id=root)
    env.board.revoke_grant(env.p["human"], permission["id"])
    env.clock.advance(121)
    probe(stack, "codex")
    probe(stack, "claude")
    try:
        route(stack, post)
    except Forbidden:
        pass
    assert current(stack, post)["assigned_session"] == env.sid["codex"]
    with pytest.raises(Forbidden):
        progress(stack, post, "started")


def test_duplicate_contract_cannot_replace_frozen_fallback(stack):
    post = create(stack)
    env = stack["env"]
    new_sid = env.board.register_session(env.p["claude"], str(stack["repo"]), str(stack["fallback_path"]))["session_id"]
    with pytest.raises(Conflict):
        create(stack, fallback_session=new_sid)
    assert env.board.get_post(env.p["human"], post["id"])["continuation"]["fallback_session"] == stack["spec"]["fallback_session"]


def test_capable_other_session_cannot_substitute_for_recorded_fallback(stack):
    post = create(stack)
    env = stack["env"]
    env.clock.advance(121)
    probe(stack, "codex")
    new_sid = env.board.register_session(env.p["claude"], str(stack["repo"]), str(stack["fallback_path"]))["session_id"]
    capabilities.register(env.board, env.p["claude"], new_sid, ["git:write"], "Ready in alternate session", activity="idle")
    route(stack, post)
    assert current(stack, post)["assigned_session"] == env.sid["codex"]


def test_completion_requires_live_task_lease_and_finishes_task_atomically(stack):
    post = create(stack)
    with pytest.raises(Conflict, match="lease"):
        finish(stack, post)
    progress(stack, post, "started")
    finish(stack, post)
    env = stack["env"]
    task = env.board.get_task(env.p["human"], post["task_id"])
    assert task["status"] == "done"
    assert task["owner_session"] is None
    assert current(stack, post)["state"] == "finished"


def test_managed_task_done_cannot_bypass_descendant_evidence(stack):
    post = create(stack)
    progress(stack, post, "started")
    env = stack["env"]
    with pytest.raises((Conflict, Forbidden)):
        env.board.transition_task(env.p["codex"], env.sid["codex"], post["task_id"], "done")
    assert current(stack, post)["state"] == "started"


@pytest.mark.parametrize("approval", ["active", "none", "owner_only", "revoked", "paused"])
def test_dispatch_tick_only_reconciles_explicitly_approved_workstream(stack, tmp_path, monkeypatch, approval):
    from agent_comms import dispatch
    from test_dispatch import FakeSpawner, FakeProcs
    env = stack["env"]
    monkeypatch.setattr(dispatch.shutil, "which", lambda executable, **kw: "/fake/" + executable)
    config = dispatch.DispatchConfig.from_dict({
        "runners": {"codex": ["codex-cli-fake", "exec", "{prompt}"],
                    "claude": ["claude-fake", "-p", "{prompt}"]},
        "worktrees": {str(stack["repo"]): str(stack["repo"])},
        "live_minutes": 2, "timeout_minutes": 30, "kill_grace_seconds": 10, "max_concurrent": 2,
    })
    spawner = FakeSpawner()
    procs = FakeProcs(spawner)
    worker = dispatch.Dispatcher(env.board, env.p["human"], config, spawner=spawner,
                                 log_dir=tmp_path / "logs", probe=procs.probe, process_start=procs.start)
    worker.acquire_loop()
    worker.tick()
    post = create(stack)
    if approval != "none":
        rule = env.board.create_dispatch_rule(env.p["human"], thread_id=stack["thread"],
            agents=["codex"] if approval == "owner_only" else ["codex", "claude"],
            purpose="Propagate this approved stack fix", max_launches=2)
        if approval == "revoked":
            env.board.revoke_dispatch_rule(env.p["human"], rule["id"])
    env.clock.advance(121)
    probe(stack, "codex")
    probe(stack, "claude")
    if approval == "paused":
        env.board.set_paused(env.p["human"], True)
    worker.tick()
    expected = "claude" if approval == "active" else "codex"
    assert current(stack, post)["assigned_session"] == env.sid[expected]
    assert spawner.calls == []  # both sessions are live; takeover must not launch duplicate processes


@pytest.mark.parametrize("checkout_state", ["dirty", "unregistered", "idle"])
def test_takeover_inspects_every_checked_out_descendant(stack, tmp_path, checkout_state):
    env = stack["env"]
    peer_path = tmp_path / "other-descendant"
    git(stack["repo"], "worktree", "add", "-b", "child-two", str(peer_path), "child")
    post = create(stack, descendants=["refs/heads/child", "refs/heads/child-two"])
    env.clock.advance(121)
    probe(stack, "codex")
    probe(stack, "claude")
    if checkout_state != "unregistered":
        env.board.register_session(env.p["grok"], str(stack["repo"]), str(peer_path),
                                   resume_session_id=env.sid["grok"])
        probe(stack, "grok")
    if checkout_state == "dirty":
        (peer_path / "unfinished").write_text("Keep peer edits\n")
    before = git(peer_path, "status", "--porcelain")
    route(stack, post)
    expected = "claude" if checkout_state == "idle" else "codex"
    assert current(stack, post)["assigned_session"] == env.sid[expected]
    assert git(peer_path, "status", "--porcelain") == before


def test_one_idle_session_cannot_vouch_for_another_possibly_active_one_at_a_checkout(stack, tmp_path):
    env = stack["env"]
    peer_path = tmp_path / "other-descendant"
    git(stack["repo"], "worktree", "add", "-b", "child-two", str(peer_path), "child")
    post = create(stack, descendants=["refs/heads/child", "refs/heads/child-two"])
    # A second grok session at the checkout, quiet for five minutes with unknown activity: possibly still working.
    quiet = env.board.register_session(env.p["grok"], str(stack["repo"]), str(peer_path))["session_id"]
    env.board.register_session(env.p["grok"], str(stack["repo"]), str(peer_path), resume_session_id=env.sid["grok"])
    env.clock.advance(300)
    for name in ("codex", "claude", "grok"):
        probe(stack, name)                                    # the other grok session is freshly idle
    route(stack, post)
    assert current(stack, post)["assigned_session"] == env.sid["codex"]
    assert "Owner activity is unknown" in env.board.conn.execute(
        "SELECT blocker FROM continuations WHERE post_id=?", (post["id"],)).fetchone()[0]
    # Once that session has been silent for a whole lease TTL it is long ended and no longer blocks.
    env.clock.advance(env.settings.lease_ttl_minutes * 60)
    for name in ("codex", "claude", "grok"):
        probe(stack, name)
    route(stack, post)
    assert current(stack, post)["assigned_session"] == env.sid["claude"]
    assert quiet != env.sid["grok"]


def test_unscoped_root_cannot_expand_into_agent_created_continuation(stack):
    env = stack["env"]
    root = env.board.create_task(env.p["human"], env.sid["human"], stack["thread"], title="Unrelated approval")["id"]
    with pytest.raises(Forbidden, match="scope"):
        create(stack, author="codex", root_task_id=root)
    assert env.board.conn.execute("SELECT COUNT(*) FROM continuations").fetchone()[0] == 0


@pytest.mark.parametrize("change", [
    {"descendants": ["refs/heads/main"]}, {"required_checks": ["other-check"]},
    {"required_capabilities": ["deploy:production"]},
])
def test_agent_continuation_stays_within_recorded_root_scope(stack, change):
    with pytest.raises(Forbidden, match="authorized root"):
        create(stack, author="codex", **change)
    assert stack["env"].board.conn.execute("SELECT COUNT(*) FROM continuations").fetchone()[0] == 0


@pytest.mark.parametrize("name", ["codex", "claude"])
@pytest.mark.parametrize("change", ["project", "worktree"])
def test_unfinished_continuation_pins_both_owner_environments(stack, tmp_path, name, change):
    post = create(stack)
    env = stack["env"]
    sid = env.sid[name]
    original = dict(env.board.conn.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone())
    replacement = tmp_path / "clean-sibling"
    git(stack["repo"], "worktree", "add", "--detach", str(replacement), "main")
    project = str(replacement) if change == "project" else original["project"]
    worktree = str(replacement) if change == "worktree" else original["worktree"]
    probe(stack, name)
    with pytest.raises(Conflict, match="pins this session environment"):
        env.board.register_session(env.p[name], project, worktree, resume_session_id=sid)
    stored = env.board.conn.execute("SELECT project,worktree FROM sessions WHERE id=?", (sid,)).fetchone()
    assert (stored["project"], stored["worktree"]) == (original["project"], original["worktree"])
    assert current(stack, post)["state"] == "queued"
    # A heartbeat-like resume of the same environment remains valid.
    assert env.board.register_session(env.p[name], original["project"], original["worktree"],
                                      resume_session_id=sid)["session_id"] == sid


@pytest.mark.parametrize("name", ["codex", "claude"])
def test_finished_continuation_releases_environment_pin_and_invalidates_idle(stack, tmp_path, name):
    post = create(stack)
    progress(stack, post, "started")
    finish(stack, post)
    env = stack["env"]
    probe(stack, name)
    assert env.board.conn.execute("SELECT state FROM session_activity WHERE session_id=?",
                                  (env.sid[name],)).fetchone()["state"] == "idle"
    replacement = tmp_path / "next-task-worktree"
    git(stack["repo"], "worktree", "add", "--detach", str(replacement), "main")
    resumed = env.board.register_session(env.p[name], str(stack["repo"]), str(replacement),
                                         resume_session_id=env.sid[name])
    assert resumed["session_id"] == env.sid[name]
    assert resumed["worktree"] == str(replacement)
    assert env.board.conn.execute("SELECT 1 FROM session_activity WHERE session_id=?",
                                  (env.sid[name],)).fetchone() is None
    assert not capabilities.eligible(env.board, env.sid[name], str(stack["repo"]), ["git:write"])


def test_dirty_detached_owner_cannot_hide_behind_clean_sibling_session(stack, tmp_path):
    post = create(stack)
    env = stack["env"]
    git(stack["owner_path"], "switch", "--detach")
    unfinished = stack["owner_path"] / "unfinished"
    unfinished.write_text("Unsaved detached work must survive\n")
    replacement = tmp_path / "clean-new-session"
    git(stack["repo"], "worktree", "add", "--detach", str(replacement), "main")
    with pytest.raises(Conflict, match="pins this session environment"):
        env.board.register_session(env.p["codex"], str(stack["repo"]), str(replacement),
                                   resume_session_id=env.sid["codex"])
    new_sid = env.board.register_session(env.p["codex"], str(stack["repo"]), str(replacement))["session_id"]
    assert new_sid != env.sid["codex"]
    env.clock.advance(121)
    probe(stack, "codex")
    probe(stack, "claude")
    capabilities.register(env.board, env.p["codex"], new_sid, ["git:write"],
                          "Clean sibling is idle", activity="idle")
    route(stack, post)
    assert current(stack, post)["assigned_session"] == env.sid["codex"]
    assert unfinished.read_text() == "Unsaved detached work must survive\n"
    assert git(replacement, "status", "--porcelain") == ""
