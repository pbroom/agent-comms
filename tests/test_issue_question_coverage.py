"""Shared issues keep answering the posts they represent now that every asking post carries a decision_question.

An issue covers a linked post only when the post has no question or exactly the issue's (db.ISSUE_COVERS). Review of
#56: raising an issue from a post, then linking another post with the same question, left both posts uncovered, so
Needs you showed three items instead of one. Now an issue raised from a post without its own question adopts the
post's, an agent links posts that reuse it, and every link result says whether the issue covers the post and why."""

from agent_comms import issues, summary

from conftest import ASK

Q = {"question": "Pin the parser to v2?", "context": "Both threads hit the same regression.",
     "options": [{"id": "pin", "label": "Pin v2", "description": "Stops the regression; costs the v3 fixes.",
                  "outcome": "approved"},
                 {"id": "wait", "label": "Wait for v3.1", "description": "No pin; costs a week.",
                  "outcome": "declined"}],
     "recommended_option_id": "pin"}


def needs_you_count(env):
    return summary.needs_you_list(env.board, env.p["human"])["count"]


def pending_posts(env):
    return sorted(p["id"] for p in env.board.snapshot(env.p["human"])["needs_you"])


def test_an_issue_raised_from_a_post_adopts_its_question_and_covers_posts_that_reuse_it(env):
    a, b = env.thread("A"), env.thread("B")
    first = env.post("codex", a, "pin?", "question", needs_response=True, decision_question=Q)
    issue = issues.create_issue(env.board, env.p["codex"], env.sid["codex"], title="Parser regression", body="b",
                                thread_id=a, post_id=first["id"])
    assert issue["decision_question"] == first["decision_question"]
    assert issue["link"]["covers_post"] is True and "covers post" in issue["link"]["coverage"]
    # Another agent asks the same question (copied from board_get_issue) on its own thread and links it.
    second = env.post("claude", b, "pin here too?", "question", needs_response=True,
                      decision_question=issues.get_issue(env.board, env.p["claude"], issue["id"])["decision_question"])
    linked = issues.link_issue(env.board, env.p["claude"], env.sid["claude"], issue["id"], b, second["id"])
    assert linked["link"] == {"thread_id": b, "post_id": second["id"], "covers_post": True,
                              "coverage": f"This issue's question covers post #{second['id']}: the issue's answer "
                                          "answers it."}
    assert [l["covers_post"] for l in linked["links"]] == [True, True]
    assert pending_posts(env) == [] and needs_you_count(env) == 1      # one item: the issue
    issues.decide_issue(env.board, env.p["human"], env.sid["human"], issue["id"], "Pin it", [a, b])
    assert pending_posts(env) == [] and needs_you_count(env) == 0
    for post in (first, second):
        assert env.board.get_post(env.p["human"], post["id"])["id"] not in pending_posts(env)


def test_a_post_with_another_question_stays_its_own_item_and_the_link_says_why(env):
    a, b = env.thread("A"), env.thread("B")
    first = env.post("codex", a, "pin?", "question", needs_response=True, decision_question=Q)
    issue = issues.create_issue(env.board, env.p["codex"], env.sid["codex"], title="t", body="b", thread_id=a,
                                post_id=first["id"])
    other = env.post("claude", b, "something else", "question", needs_response=True, decision_question=ASK)
    linked = issues.link_issue(env.board, env.p["claude"], env.sid["claude"], issue["id"], b, other["id"])
    assert linked["link"]["covers_post"] is False
    assert "asks its own question" in linked["link"]["coverage"]
    assert "exactly the issue's decision_question" in linked["link"]["coverage"]
    assert pending_posts(env) == [other["id"]] and needs_you_count(env) == 2
    # Linking again is idempotent and reports the stored coverage.
    again = issues.link_issue(env.board, env.p["claude"], env.sid["claude"], issue["id"], b, other["id"])
    assert again["link"]["covers_post"] is False


def test_an_explicit_issue_question_wins_and_a_question_with_actions_is_never_adopted(env):
    a = env.thread("A")
    first = env.post("codex", a, "pin?", "question", needs_response=True, decision_question=Q)
    own = issues.create_issue(env.board, env.p["codex"], env.sid["codex"], title="t", body="b", thread_id=a,
                              post_id=first["id"], decision_question=ASK)
    assert own["decision_question"]["question"] == "Which way?" and own["link"]["covers_post"] is False
    task = env.accepted_task(a)
    action_q = dict(Q, options=[dict(Q["options"][0], action={"type": "decline_task", "task_id": task,
                                                               "expected_status": "accepted"}), Q["options"][1]])
    with_action = env.post("codex", a, "decline?", "question", needs_response=True, decision_question=action_q)
    issue = issues.create_issue(env.board, env.p["codex"], env.sid["codex"], title="t2", body="b", thread_id=a,
                                post_id=with_action["id"])
    assert issue["decision_question"] is None and issue["link"]["covers_post"] is False
    thread_only = issues.create_issue(env.board, env.p["codex"], env.sid["codex"], title="t3", body="b", thread_id=a)
    assert thread_only["link"]["covers_post"] is None
