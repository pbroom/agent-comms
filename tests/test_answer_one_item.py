"""Answering one Needs you item clears exactly that item: per post, per issue link and question, nothing else.

Regression for "when I submit one answer in a group of multiple answers, the whole group closes and is marked
answered". Each group shape the dashboard shows: several posts in one thread, an issue linking several posts (in
one thread and across threads, with and without their own questions), the sidebar/menu count and the agent brief.
"""
import pytest
from fastapi.testclient import TestClient

from agent_comms import attention, db, issues, summary
from agent_comms.api import create_app
from agent_comms.core import Conflict
from agent_comms.dispatch import DispatchConfig

from conftest import PROJECT


def q(n):
    return {"question": f"Question {n}?", "context": "", "recommended_option_id": "a",
            "options": [{"id": "a", "label": f"Yes {n}"}, {"id": "b", "label": f"No {n}"}]}


def ask(env, tid, who="codex", type="question", **kw):
    return env.post(who, tid, f"{type} from {who}", type, needs_response=type != "decision", **kw)["id"]


def needs_you(env):
    return sorted(p["id"] for p in env.board.snapshot(env.p["human"])["needs_you"])


def waiting_issues(env):
    return {i["id"]: sorted((l["thread_id"], l["post_id"]) for l in i["links"] if l["needs_human"])
            for i in env.board.snapshot(env.p["human"])["needs_you_issues"]}


def client(env):
    c = TestClient(create_app(env.board))
    h = {"Authorization": f"Bearer {env.tokens['human']}"}
    return lambda path, body=None: c.post(path, json=body, headers=h)


def decide(env, issue_id, thread_ids, **kw):
    version = issues.get_issue(env.board, env.p["human"], issue_id)["question_version"]
    return client(env)(f"/api/issues/{issue_id}/decisions",
                       {"thread_ids": thread_ids, "selected_option_id": "a", "expected_question_version": version} | kw)


def test_one_thread_several_posts_each_answered_alone(env):
    tid = env.thread()
    plain = ask(env, tid)
    structured = ask(env, tid, "claude", decision_question=q(1))
    decision = ask(env, tid, "codex", "decision")
    proposal = env.post("claude", tid, "proposal", "proposal")["id"]
    structured_decision = ask(env, tid, "claude", "decision", decision_question=q(2))
    every = sorted([plain, structured, decision, proposal, structured_decision])
    post = client(env)
    env.post("human", tid, "an unrelated human post")
    assert needs_you(env) == every
    assert post(f"/api/posts/{structured}/resolve", {"action": "choose", "option_id": "b"}).status_code == 200
    assert needs_you(env) == sorted(set(every) - {structured})
    assert post(f"/api/posts/{plain}/resolve", {"action": "reply", "text": "yes"}).status_code == 200
    assert post(f"/api/posts/{decision}/finalize").status_code == 200
    assert needs_you(env) == sorted([proposal, structured_decision])
    assert post(f"/api/posts/{proposal}/resolve", {"action": "not_now"}).status_code == 200
    assert post(f"/api/posts/{structured_decision}/resolve", {"action": "choose", "option_id": "a"}).status_code == 200
    assert needs_you(env) == []


def test_issue_answer_leaves_linked_posts_with_their_own_questions(env):
    t1, t2 = env.thread("one"), env.thread("two")
    own1 = ask(env, t1, "claude", decision_question=q(11))
    own2 = ask(env, t1, "codex", decision_question=q(12))
    plain = ask(env, t1, "codex")                   # no question of its own: the issue's question covers it
    same = ask(env, t1, "claude", decision_question=q(99))   # exactly the issue's question: covered
    other = ask(env, t2, "codex")
    unlinked = ask(env, t1, "claude")
    issue = issues.create_issue(env.board, env.p["claude"], env.sid["claude"], title="Shared", body="b",
                                thread_id=t1, post_id=own1, decision_question=q(99))["id"]
    for tid, pid in ((t1, own2), (t1, plain), (t1, same), (t2, other)):
        issues.link_issue(env.board, env.p["codex"], env.sid["codex"], issue, tid, pid)
    # The issue stands in for the posts it covers; posts asking their own question stay their own items.
    assert needs_you(env) == sorted([own1, own2, unlinked])
    # own1's link waits because the issue was opened asking the human (needs_human); own2's own attention is not
    # the issue's, so its link does not wait on the human.
    assert waiting_issues(env) == {issue: sorted([(t1, own1), (t1, plain), (t1, same), (t2, other)])}
    assert {l["post_id"]: l["covers_post"] for l in issues.get_issue(env.board, env.p["human"], issue)["links"]} == {
        own1: False, own2: False, plain: True, same: True, other: True}

    r = decide(env, issue, [t1])
    assert r.status_code == 200, r.text
    answer = next(p for p in env.board.list_posts(env.p["human"], t1)["posts"] if p["agent"] == "human")
    assert answer["answer_to"] == sorted([plain, same]), "only the source posts the issue's question covers"
    assert needs_you(env) == sorted([own1, own2, unlinked])
    assert waiting_issues(env) == {issue: [(t2, other)]}, "only the selected thread's links are answered"
    # Nothing in thread 1 is complete while its own questions are open.
    assert not issues._completed_answer_work(env.board, t1)

    post = client(env)
    assert post(f"/api/posts/{own1}/resolve", {"action": "choose", "option_id": "a"}).status_code == 200
    assert needs_you(env) == sorted([own2, unlinked])
    assert post(f"/api/posts/{plain}/resolve", {"action": "approve"}).status_code == 409, "already answered by the issue"
    assert post(f"/api/posts/{other}/resolve", {"action": "approve"}).status_code == 409, "the issue governs it"
    assert post(f"/api/posts/{own2}/resolve", {"action": "reply", "text": "no"}).status_code == 200
    assert post(f"/api/posts/{unlinked}/resolve", {"action": "approve"}).status_code == 200
    assert needs_you(env) == []
    assert decide(env, issue, [t2]).status_code == 200
    assert waiting_issues(env) == {}


def test_issue_across_threads_answers_only_the_selected_thread(env):
    t1, t2 = env.thread("one"), env.thread("two")
    a, b = ask(env, t1), ask(env, t2, "claude")
    issue = issues.create_issue(env.board, env.p["codex"], env.sid["codex"], title="Shared", body="b",
                                thread_id=t1, post_id=a)["id"]
    issues.link_issue(env.board, env.p["claude"], env.sid["claude"], issue, t2, b)
    r = client(env)(f"/api/issues/{issue}/decisions", {"thread_ids": [t2], "body": "only two"})
    assert r.status_code == 200, r.text
    assert waiting_issues(env) == {issue: [(t1, a)]}
    pending = {r["id"] for r in env.board.conn.execute(f"SELECT p.id FROM posts p WHERE {env.board.NEEDS_YOU_SOURCE}")}
    assert pending == {a}, "the other thread's source is still unanswered"
    assert client(env)(f"/api/issues/{issue}/decisions", {"thread_ids": [t1], "body": "and one"}).status_code == 200
    assert waiting_issues(env) == {}


def test_menu_count_and_agent_brief_count_each_item(env):
    tid = env.thread()
    first, second = ask(env, tid), ask(env, tid, "claude", decision_question=q(1))
    own = ask(env, tid, "claude", decision_question=q(2))
    issues.create_issue(env.board, env.p["codex"], env.sid["codex"], title="Shared", body="b", thread_id=tid,
                        post_id=own, decision_question=q(3))
    count = lambda: summary.needs_you_list(env.board, env.p["human"])["count"]
    brief = lambda: env.board.brief(env.p["codex"], [PROJECT])["open_questions_for_human"]
    assert count() == 4 and brief() == 3          # three posts and the issue; three posts ask the human
    client(env)(f"/api/posts/{second}/resolve", {"action": "choose", "option_id": "a"})
    assert count() == 3 and brief() == 2
    assert summary.human_summary(env.board, env.p["human"], DispatchConfig())["needs_you"]["count"] == 3
    client(env)(f"/api/posts/{first}/resolve", {"action": "approve"})
    client(env)(f"/api/posts/{own}/resolve", {"action": "approve"})
    assert count() == 1 and brief() == 0          # the issue still waits on its own decision


def links(env, issue):
    return {l["post_id"]: (l["covers_post"], l["needs_human"]) for l in issues.get_issue(env.board, env.p["human"], issue)["links"]}


def test_post_with_its_own_question_does_not_make_its_issue_wait(env):
    """Review P2: an issue opened without a question (needs_human=False) on a post that asks its own question must
    not wait on the human because of that post, before or after the post is answered."""
    t1, t2 = env.thread("one"), env.thread("two")
    own = ask(env, t1, decision_question=q(1))
    later = ask(env, t2, "claude", decision_question=q(2))
    issue = issues.create_issue(env.board, env.p["codex"], env.sid["codex"], title="Context", body="b",
                                thread_id=t1, post_id=own, needs_human=False)["id"]
    issues.link_issue(env.board, env.p["claude"], env.sid["claude"], issue, t2, later)
    assert links(env, issue) == {own: (False, False), later: (False, False)}
    assert waiting_issues(env) == {} and needs_you(env) == sorted([own, later])
    assert client(env)(f"/api/posts/{own}/resolve", {"action": "choose", "option_id": "a"}).status_code == 200
    assert waiting_issues(env) == {} and needs_you(env) == [later]
    assert summary.needs_you_list(env.board, env.p["human"])["count"] == 1


def test_refined_issue_question_keeps_the_posts_it_covered_when_linked(env):
    """Review P3: coverage is decided when a post is linked. Refining the issue's question (a request comment) asks
    every linked thread again, but neither releases a covered post nor swallows one that asks its own question."""
    tid = env.thread()
    plain, same, own = ask(env, tid), ask(env, tid, "claude", decision_question=q(1)), ask(env, tid, decision_question=q(2))
    issue = issues.create_issue(env.board, env.p["codex"], env.sid["codex"], title="Shared", body="b", thread_id=tid,
                                post_id=plain, decision_question=q(1))["id"]
    for pid in (same, own):
        issues.link_issue(env.board, env.p["codex"], env.sid["codex"], issue, tid, pid)
    assert needs_you(env) == [own]
    issues.comment_issue(env.board, env.p["codex"], env.sid["codex"], issue, "Narrower question", kind="request",
                         decision_question=q(2))      # now equal to `own`'s question, and no longer to `same`'s
    assert links(env, issue) == {plain: (True, True), same: (True, True), own: (False, True)}
    assert needs_you(env) == [own], "same stays covered; own stays its own item"
    assert decide(env, issue, [tid]).status_code == 200
    answer = next(p for p in env.board.list_posts(env.p["human"], tid)["posts"] if p["agent"] == "human")
    assert answer["answer_to"] == sorted([plain, same])
    assert needs_you(env) == [own]


def test_resolving_an_issue_brings_back_covered_posts_it_never_answered(env):
    """Review P3: resolve_issue answers nothing, so a covered post the issue never answered must not stay hidden (and
    block completion) for good: it is its own Needs you item again. Answered ones stay answered."""
    t1, t2 = env.thread("one"), env.thread("two")
    answered, unanswered = ask(env, t1), ask(env, t2, "claude")
    issue = issues.create_issue(env.board, env.p["codex"], env.sid["codex"], title="Shared", body="b",
                                thread_id=t1, post_id=answered)["id"]
    issues.link_issue(env.board, env.p["claude"], env.sid["claude"], issue, t2, unanswered)
    assert decide(env, issue, [t1], selected_option_id=None, body="Go ahead").status_code == 200
    assert needs_you(env) == []
    r = client(env)(f"/api/issues/{issue}/resolve", {"body": "Verified the shared fix"})
    assert r.status_code == 200, r.text
    assert needs_you(env) == [unanswered] and waiting_issues(env) == {}
    pending = {r["id"] for r in env.board.conn.execute(f"SELECT p.id FROM posts p WHERE {env.board.NEEDS_YOU_SOURCE}")}
    assert pending == {unanswered}
    assert client(env)(f"/api/posts/{unanswered}/resolve", {"action": "not_now"}).status_code == 200
    assert needs_you(env) == []


def test_agent_closeout_is_refused_only_for_attention_an_open_issue_covers(env):
    tid = env.thread()
    covered, own = ask(env, tid), ask(env, tid, decision_question=q(1))
    issue = issues.create_issue(env.board, env.p["codex"], env.sid["codex"], title="Shared", body="b", thread_id=tid,
                                post_id=covered)["id"]
    issues.link_issue(env.board, env.p["codex"], env.sid["codex"], issue, tid, own)
    evidence = env.post("codex", tid, "done")["id"]
    close = lambda pid: attention.close_attention(env.board, env.p["codex"], env.sid["codex"], pid, "Handled", [evidence])
    with pytest.raises(Conflict):
        close(covered)
    close(own)
    assert needs_you(env) == []
    client(env)(f"/api/issues/{issue}/resolve", {"body": "Verified"})
    assert needs_you(env) == [covered]
    close(covered)
    assert needs_you(env) == []


def test_upgrade_backfills_coverage_from_the_issue_question(env):
    tid = env.thread()
    plain, same, own = ask(env, tid), ask(env, tid, "claude", decision_question=q(1)), ask(env, tid, decision_question=q(2))
    issue = issues.create_issue(env.board, env.p["codex"], env.sid["codex"], title="Shared", body="b", thread_id=tid,
                                post_id=plain, decision_question=q(1))["id"]
    for pid in (same, own):
        issues.link_issue(env.board, env.p["codex"], env.sid["codex"], issue, tid, pid)
    issues.link_issue(env.board, env.p["codex"], env.sid["codex"], issue, tid)
    conn = env.board.conn
    conn.execute("ALTER TABLE issue_links DROP COLUMN covers_post")      # a board from before the column
    db.init_schema(conn)
    assert links(env, issue) == {plain: (True, True), same: (True, True), own: (False, False), None: (None, True)}
    assert needs_you(env) == [own]


def test_upgrade_backfill_does_not_cover_a_post_question_when_the_issue_has_none(env):
    # Review of #42: `p.q = i.q` is NULL when the issue has no question, and COALESCE(NULL, 1) marked the post covered.
    tid = env.thread()
    own = ask(env, tid, decision_question=q(2))
    issue = issues.create_issue(env.board, env.p["codex"], env.sid["codex"], title="No question", body="b",
                                thread_id=tid, post_id=own, needs_human=False)["id"]
    conn = env.board.conn
    conn.execute("ALTER TABLE issue_links DROP COLUMN covers_post")
    db.init_schema(conn)
    assert links(env, issue)[own][1] is False, "a post with its own question is not covered by a question-less issue"
    assert own in needs_you(env)
