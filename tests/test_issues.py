import pytest

from agent_comms import issues, db
from agent_comms.core import Forbidden, Invalid, LimitExceeded, Paused


def create(env, **kw):
    return issues.create_issue(
        env.board,
        env.p["codex"],
        env.sid["codex"],
        title="Git metadata blocked",
        body="Need access",
        thread_id=kw.pop("thread_id", env.thread()),
        **kw,
    )


def test_collaborative_issue_and_scoped_decision(env):
    first = env.thread()
    post = env.post("codex", first, needs_response=True)
    issue = create(env, thread_id=first, post_id=post["id"])
    iid = issue["id"]
    second = env.thread("other")
    second_post = env.post("claude", second, needs_response=True)
    issues.link_issue(env.board, env.p["claude"], env.sid["claude"], iid, second, second_post["id"])
    issues.comment_issue(
        env.board, env.p["claude"], env.sid["claude"], iid, "Observed same permission failure", "evidence"
    )
    issues.comment_issue(env.board, env.p["codex"], env.sid["codex"], iid, "Proposed narrow fix", "proposal")
    snap = env.board.snapshot(env.p["human"])
    assert not snap["needs_you"]
    assert len(snap["needs_you_issues"]) == 1
    out = issues.decide_issue(
        env.board, env.p["human"], env.sid["human"], iid, "Approve fix for first only", [first], "approved"
    )
    assert out["status"] == "open" and out["needs_human"]
    assert {link["thread_id"] for link in out["links"] if link["needs_human"]} == {second}
    assert out["decisions"][0]["thread_ids"] == [first]
    third = env.thread("new join")
    out = issues.link_issue(env.board, env.p["codex"], env.sid["codex"], iid, third)
    assert out["decisions"][0]["thread_ids"] == [first]
    assert not env.board.list_grants(env.p["codex"])
    out = issues.resolve_issue(env.board, env.p["human"], env.sid["human"], iid, "Verified implementation")
    assert out["status"] == "resolved"
    out = issues.comment_issue(
        env.board, env.p["claude"], env.sid["claude"], iid, "New scope needs a decision", "request"
    )
    assert out["status"] == "open" and out["needs_human"]
    assert len(issues.list_issues(env.board, env.p["codex"], query="metadata", thread_id=second)) == 1


def test_sealed_and_mismatched_links_rejected_atomically(env):
    thread = env.thread()
    sealed = env.post("codex", thread, sealed=True)
    with pytest.raises(Forbidden):
        create(env, thread_id=thread, post_id=sealed["id"])
    assert not issues.list_issues(env.board, env.p["human"])
    other = env.thread()
    with pytest.raises(Invalid):
        create(env, thread_id=other, post_id=sealed["id"])


def test_permissions_pause_identity_and_no_task_authorization(env):
    issue = create(env)
    iid = issue["id"]
    with pytest.raises(Forbidden):
        issues.comment_issue(env.board, env.p["claude"], env.sid["codex"], iid, "spoof")
    for fn, args in [
        (issues.decide_issue, ("yes", [issue["links"][0]["thread_id"]])),
        (issues.resolve_issue, ("done",)),
    ]:
        with pytest.raises(Forbidden):
            fn(env.board, env.p["codex"], env.sid["codex"], iid, *args)
    env.board.set_paused(env.p["human"], True)
    with pytest.raises(Paused):
        issues.comment_issue(env.board, env.p["codex"], env.sid["codex"], iid, "hi")


def test_caps_shared_with_posts_and_issue_discussion(env):
    env.settings.daily_post_cap_per_agent = 2
    iid = create(env)["id"]
    issues.comment_issue(env.board, env.p["codex"], env.sid["codex"], iid, "one")
    with pytest.raises(LimitExceeded):
        issues.comment_issue(env.board, env.p["codex"], env.sid["codex"], iid, "two")
    with pytest.raises(LimitExceeded):
        env.post("codex", env.thread())
    env.settings.daily_post_cap_per_agent = 100
    env.settings.max_agent_posts_per_thread_without_human = 2
    with pytest.raises(LimitExceeded):
        issues.comment_issue(env.board, env.p["codex"], env.sid["codex"], iid, "two")


def test_additive_migration_keeps_posts_and_link_idempotence(env):
    tid = env.thread()
    post = env.post("codex", tid, needs_response=True)
    env.board.conn.execute("PRAGMA user_version=3")
    db.init_schema(env.board.conn)
    assert env.board.conn.execute("SELECT body FROM posts WHERE id=?", (post["id"],)).fetchone()[0] == "hi"
    issue = create(env, thread_id=tid)
    for _ in range(2):
        issue = issues.link_issue(env.board, env.p["codex"], env.sid["codex"], issue["id"], tid, post["id"])
    assert len(issue["links"]) == 2
    assert len([c for c in issue["comments"] if c["kind"] == "linked"]) == 1


def test_linking_preserves_attention_and_updates_include_decision(env):
    first = env.thread()
    post = env.post("codex", first, needs_response=True)
    issue = create(env, thread_id=first, post_id=post["id"], needs_human=False)
    assert issue["needs_human"]
    issues.decide_issue(env.board, env.p["human"], env.sid["human"], issue["id"], "Answer", [first])
    second = env.thread()
    post2 = env.post("claude", second, needs_response=True)
    issue = issues.link_issue(env.board, env.p["claude"], env.sid["claude"], issue["id"], second, post2["id"])
    assert issue["needs_human"]
    assert issue["decisions"][0]["thread_ids"] == [first]
    updates = env.board.read_updates(env.p["codex"], env.sid["codex"])
    assert updates["issues"][0]["decisions"][0]["body"] == "Answer"


def test_partial_scope_and_new_links_keep_pending_attention(env):
    first = env.thread()
    second = env.thread()
    out = create(env, thread_id=first)
    iid = out["id"]
    issues.link_issue(env.board, env.p["claude"], env.sid["claude"], iid, second)
    out = issues.decide_issue(env.board, env.p["human"], env.sid["human"], iid, "First only", [first])
    assert out["needs_human"]
    assert len(env.board.snapshot(env.p["human"])["needs_you_issues"]) == 1
    out = issues.decide_issue(env.board, env.p["human"], env.sid["human"], iid, "Second now", [second])
    assert not out["needs_human"]
    third = env.thread()
    out = issues.link_issue(env.board, env.p["codex"], env.sid["codex"], iid, third)
    assert out["needs_human"]
    assert {l["thread_id"] for l in out["links"] if l["needs_human"]} == {third}
    out = issues.comment_issue(env.board, env.p["claude"], env.sid["claude"], iid, "Reconsider", "request")
    assert all(l["needs_human"] for l in out["links"])


def test_needs_you_not_hidden_by_newer_resolved_issues(env):
    pending = create(env)
    for n in range(201):
        env.board.conn.execute(
            "INSERT INTO issues(title,body,status,needs_human,created_by,created_at,updated_at) VALUES(?,?,'resolved',0,'human',?,?)",
            (str(n), "old", env.clock(), env.clock() + n + 1),
        )
    snapshot = env.board.snapshot(env.p["human"])
    assert pending["id"] not in [i["id"] for i in snapshot["issues"]]
    assert [i["id"] for i in snapshot["needs_you_issues"]] == [pending["id"]]


def test_source_attention_uses_exact_existing_rules(env):
    thread = env.thread()
    decision = env.post("codex", thread, type="decision", to=["claude"])
    issue = create(env, thread_id=thread, post_id=decision["id"], needs_human=False)
    assert issue["needs_human"]
    thread = env.thread()
    post = env.post("codex", thread, needs_response=True)
    env.post("human", thread, "Answered")
    issue = create(env, thread_id=thread, post_id=post["id"], needs_human=False)
    assert not issue["needs_human"]
    thread = env.thread()
    task = env.accepted_task(thread)
    post = env.post("codex", thread, type="proposal", task_id=task)
    issue = create(env, thread_id=thread, post_id=post["id"], needs_human=False)
    assert not issue["needs_human"]


def test_resolution_history_survives_reopen_and_resolve(env):
    issue = create(env)
    iid = issue["id"]
    issues.resolve_issue(env.board, env.p["human"], env.sid["human"], iid, "First verification")
    issues.comment_issue(env.board, env.p["codex"], env.sid["codex"], iid, "Failure recurred", "request")
    out = issues.resolve_issue(env.board, env.p["human"], env.sid["human"], iid, "Second verification")
    assert [r["body"] for r in out["resolutions"]] == ["First verification", "Second verification"]
    assert out["resolution"] == out["resolutions"][-1]
    out = issues.comment_issue(
        env.board, env.p["codex"], env.sid["codex"], iid, "Further evidence", "request"
    )
    assert out["status"] == "open"
    assert len(out["resolutions"]) == 2
