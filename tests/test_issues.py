import pytest

from agent_comms import issues, db
from agent_comms.core import Forbidden, Invalid, LimitExceeded, Paused

from conftest import ASK, legacy_plain


def plain_ask(env, who, thread_id):
    """A plain needs-response status to the human, as stored before every asking post had to carry a
    decision_question (live boards still hold them; an issue covers such a post)."""
    post = env.post(who, thread_id, "hi", "question", needs_response=True, decision_question=ASK)
    legacy_plain(env, post["id"], "status")
    return env.board.get_post(env.p[who], post["id"])


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
    post = plain_ask(env, "codex", first)
    issue = create(env, thread_id=first, post_id=post["id"])
    iid = issue["id"]
    second = env.thread("other")
    second_post = plain_ask(env, "claude", second)
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
    post = plain_ask(env, "codex", tid)
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
    post = plain_ask(env, "codex", first)
    issue = create(env, thread_id=first, post_id=post["id"], needs_human=False)
    assert issue["needs_human"]
    issues.decide_issue(env.board, env.p["human"], env.sid["human"], issue["id"], "Answer", [first])
    second = env.thread()
    post2 = plain_ask(env, "claude", second)
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
    decision = env.post("codex", thread, type="decision", to=["claude"], decision_question=ASK)
    issue = create(env, thread_id=thread, post_id=decision["id"], needs_human=False)
    assert issue["needs_human"]
    thread = env.thread()
    post = plain_ask(env, "codex", thread)
    env.post("human", thread, "Answered", answer_to=[post['id']])
    issue = create(env, thread_id=thread, post_id=post["id"], needs_human=False)
    assert not issue["needs_human"]
    thread = env.thread()
    post = env.post("codex", thread, type="proposal", propose_task={"title": "t"})   # left to the task flow
    issue = create(env, thread_id=thread, post_id=post["id"], needs_human=False)
    assert not issue["needs_human"]
    thread = env.thread()
    task = env.accepted_task(thread)
    post = env.post("codex", thread, type="proposal", task_id=task, decision_question=ASK)   # about an existing task: asks the human
    issue = create(env, thread_id=thread, post_id=post["id"], needs_human=False)
    assert issue["needs_human"]


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


def question():
    return {
        "question": "Which approach should we take?",
        "context": "The first approach preserves current behavior.",
        "options": [
            {"id": "keep", "label": "Keep current behavior", "description": "No migration", "outcome": "approved"},
            {"id": "change", "label": "Change the behavior"},
        ],
        "recommended_option_id": "keep",
    }


def test_preset_answer_uses_stored_choice_and_persists_snapshot(env):
    out = create(env, decision_question=question())
    iid, tid = out["id"], out["links"][0]["thread_id"]
    out = issues.decide_issue(env.board, env.p["human"], env.sid["human"], iid,
                             "Forged broader permission", [tid], "declined",
                             selected_option_id="keep", expected_question_version=1)
    decision = out["decisions"][0]
    assert decision["body"] == "Keep current behavior — No migration"
    assert decision["outcome"] == "approved"
    assert decision["selected_option_id"] == "keep"
    assert decision["question_version"] == 1
    assert decision["decision_question"] == out["decision_question"]
    assert out["status"] == "open" and not out["needs_human"]
    assert not env.board.list_grants(env.p["human"])
    issues.comment_issue(env.board, env.p["codex"], env.sid["codex"], iid, "Another decision", "request")
    reloaded = issues.get_issue(env.board, env.p["human"], iid)
    assert reloaded["decision_question"] is None
    assert reloaded["question_version"] == 2
    assert reloaded["decisions"][0] == decision


def test_requests_version_question_and_comments_cannot_replace_it(env):
    from agent_comms.core import Conflict
    out = create(env, decision_question=question())
    iid, tid = out["id"], out["links"][0]["thread_id"]
    issues.comment_issue(env.board, env.p["codex"], env.sid["codex"], iid, "Evidence")
    assert issues.get_issue(env.board, env.p["human"], iid)["question_version"] == 1
    with pytest.raises(Invalid):
        issues.comment_issue(env.board, env.p["codex"], env.sid["codex"], iid, "Replace", decision_question=question())
    for version, option, error in [(None, "keep", Invalid), (0, "keep", Conflict), (1, "bogus", Invalid)]:
        with pytest.raises(error):
            issues.decide_issue(env.board, env.p["human"], env.sid["human"], iid, None, [tid],
                                selected_option_id=option, expected_question_version=version)
    issues.comment_issue(env.board, env.p["codex"], env.sid["codex"], iid, "New question", "request", question())
    with pytest.raises(Conflict):
        issues.decide_issue(env.board, env.p["human"], env.sid["human"], iid, "Custom", [tid], expected_question_version=1)
    assert not issues.get_issue(env.board, env.p["human"], iid)["decisions"]
    out = issues.decide_issue(env.board, env.p["human"], env.sid["human"], iid, "My own choice", [tid], expected_question_version=2)
    assert out["decisions"][0]["body"] == "My own choice"
    assert out["decisions"][0]["selected_option_id"] is None


@pytest.mark.parametrize("change", [
    {"options": []}, {"recommended_option_id": "missing"}, {"question": " "},
    {"options": [{"id": "same", "label": "a"}, {"id": "same", "label": "b"}]},
    {"options": [{"id": "keep", "label": "a", "outcome": "resolved"}, {"id": "b", "label": "b"}]},
    {"context": 123},
])
def test_malformed_questions_rejected_without_creating_issue(env, change):
    with pytest.raises(Invalid):
        create(env, decision_question=question() | change)
    assert not issues.list_issues(env.board, env.p["human"])


def test_v4_migration_adds_questions_without_altering_history(env):
    issue = create(env)
    tid = issue["links"][0]["thread_id"]
    issue = issues.decide_issue(env.board, env.p["human"], env.sid["human"], issue["id"], "Legacy answer", [tid])
    c = env.board.conn
    for table, column in [("issues", "decision_question"), ("issues", "question_version"), ("issue_comments", "decision")]:
        c.execute(f"ALTER TABLE {table} DROP COLUMN {column}")
    c.execute("PRAGMA user_version=4")
    db.init_schema(c)
    out = issues.get_issue(env.board, env.p["human"], issue["id"])
    assert out["decision_question"] is None and out["question_version"] == 0
    assert out["comments"] == issue["comments"]
    assert out["decisions"][0]["body"] == "Legacy answer"
    assert out["decisions"][0]["thread_ids"] == [tid]


def test_issue_links_share_thread_cap_across_distinct_issues_and_posts(env):
    thread = env.thread()
    env.settings.max_agent_posts_per_thread_without_human = 3
    env.post('codex', thread, 'first')
    create(env, thread_id=thread)
    other = create(env)
    issues.link_issue(env.board, env.p['codex'], env.sid['codex'], other['id'], thread)
    assert env.board._agent_posts_since_human(thread) == 3
    # Repeating an existing join is idempotent, but neither new issues nor posts bypass the cap.
    issues.link_issue(env.board, env.p['codex'], env.sid['codex'], other['id'], thread)
    with pytest.raises(LimitExceeded):
        create(env, thread_id=thread)
    with pytest.raises(LimitExceeded):
        env.post('codex', thread, 'bypass')
    env.clock.advance(1)
    env.post('human', thread, 'continue')
    create(env, thread_id=thread)
    assert env.board._agent_posts_since_human(thread) == 1


def test_issue_search_includes_linked_project_and_thread_title(env):
    thread = env.thread('Distinctive linked workstream')
    issue = create(env, thread_id=thread)
    assert [i['id'] for i in issues.list_issues(env.board, env.p['codex'], query='distinctive LINKED')] == [issue['id']]
    assert [i['id'] for i in issues.list_issues(env.board, env.p['codex'], query='/WORK/repo')] == [issue['id']]
    assert not issues.list_issues(env.board, env.p['codex'], query='no match')
