"""Shared issues count once in the menu, without leaking issue discussion into summary."""
import json

from agent_comms import issues, summary
from agent_comms.dispatch import DispatchConfig


def test_shared_issue_replaces_linked_posts_and_answer_is_not_resolution(env):
    b, p, sid = env.board, env.p, env.sid
    t1, t2 = env.thread(), env.thread()
    a = env.post('claude', t1, 'first blocker', needs_response=True)
    z = env.post('codex', t2, 'second blocker', needs_response=True)
    issue = issues.create_issue(b, p['claude'], sid['claude'], title='PRIVATE TITLE',
                               body='PRIVATE BODY', thread_id=t1, post_id=a['id'])
    issues.link_issue(b, p['codex'], sid['codex'], issue['id'], t2, z['id'])
    issues.comment_issue(b, p['codex'], sid['codex'], issue['id'], 'PRIVATE COMMENT', 'proposal')
    d = summary.human_summary(b, p['human'], DispatchConfig())
    assert d['needs_you']['count'] == 1
    assert len(d['needs_you']['items']) == 1
    assert d['needs_you']['items'][0]['issue_id'] == issue['id']
    assert 'PRIVATE' not in json.dumps(d)
    listing = summary.needs_you_list(b, p['human'])
    assert listing['count'] == 1
    assert listing['items'][0]['preview'] == 'PRIVATE TITLE'
    issues.decide_issue(b, p['human'], sid['human'], issue['id'], 'answer', [t1])
    assert summary.needs_you_list(b, p['human'])['count'] == 1
    assert summary.human_summary(b, p['human'], DispatchConfig())['needs_you']['count'] == 1
    issues.decide_issue(b, p['human'], sid['human'], issue['id'], 'remaining answer', [t2])
    assert summary.needs_you_list(b, p['human'])['count'] == 0
    assert issues.get_issue(b, p['human'], issue['id'])['status'] == 'open'
    issues.comment_issue(b, p['codex'], sid['codex'], issue['id'], 'new question', 'request')
    assert summary.needs_you_list(b, p['human'])['count'] == 1


def test_issue_without_post_and_existing_posts_share_limit(env):
    b, p, sid = env.board, env.p, env.sid
    thread = env.thread()
    for n in range(11):
        env.post('claude', thread, f'question {n}', needs_response=True)
    env.clock.advance(1)
    issue = issues.create_issue(b, p['codex'], sid['codex'], title='A\nclean\u202etitle',
                               body='details', thread_id=thread)
    d = summary.needs_you_list(b, p['human'])
    assert d['count'] == 12 and len(d['items']) == 10
    assert d['items'][0]['issue_id'] == issue['id']
    assert d['items'][0]['post_id'] is None
    assert '\n' not in d['items'][0]['preview'] and '\u202e' not in d['items'][0]['preview']
    s = summary.human_summary(b, p['human'], DispatchConfig())
    assert s['needs_you']['count'] == 12 and len(s['needs_you']['items']) == 5


def test_question_preview_is_sanitized_and_summary_keeps_text_private(env):
    from test_issues import question
    q = question()
    q['question'] = 'May we\nproceed\u202e?' + 'x' * 100
    issue = issues.create_issue(env.board, env.p['codex'], env.sid['codex'], title='Old title',
                               body='Context', thread_id=env.thread(), decision_question=q)
    listing = summary.needs_you_list(env.board, env.p['human'])
    text = listing['items'][0]['preview']
    assert text.startswith('May we') and 'Old title' not in text
    assert '\n' not in text and '\u202e' not in text and len(text) <= 80
    assert 'May we' not in json.dumps(summary.human_summary(env.board, env.p['human'], DispatchConfig()))
    issues.comment_issue(env.board, env.p['codex'], env.sid['codex'], issue['id'], 'Legacy request', 'request')
    assert summary.needs_you_list(env.board, env.p['human'])['items'][0]['preview'] == 'Old title'
