"""Schema upgrades preserve history; only explicit reconciliation completes work."""
import pytest

from agent_comms import db, requests, unstick
from conftest import ASK


def request(env, post):
    return env.board.get_post(env.p['human'], post['id'])['requests'][0]


def upgrade(env, version=10):
    env.board.conn.execute(f'PRAGMA user_version={version}')
    db.init_schema(env.board.conn)


def begin_tracking(env, thread):
    env.clock.advance(60)
    post = env.post('human', thread, type='request', to=['claude'])
    requests.progress(env.board, env.p['claude'], env.sid['claude'], post['id'], 'claude', 'started')


@pytest.mark.parametrize('version', [6, 10])
@pytest.mark.parametrize('body', [
    'Blocked: missing credentials; work has not started',
    'An unrelated review is complete',
    'Review done',
])
def test_later_post_never_proves_legacy_completion(env, version, body):
    thread = env.thread()
    source = env.post('human', thread, 'Implement fix and verify tests', type='request', to=['codex'])
    env.post('codex', thread, body)
    begin_tracking(env, thread)
    upgrade(env, version)
    assert request(env, source)['state'] == 'queued'
    assert not env.board.conn.execute('SELECT 1 FROM request_progress WHERE post_id=?', (source['id'],)).fetchone()
    reasons = unstick.stuck_agents(env.board, thread)[1]
    assert any(r['kind'] == 'unanswered' and source['id'] in r['post_ids'] for r in reasons)


@pytest.mark.parametrize('status', ['done', 'declined'])
def test_terminal_linked_task_does_not_complete_separate_request(env, status):
    thread = env.thread()
    task = env.accepted_task(thread)
    source = env.post('human', thread, 'Verify this specific follow-up', type='request', to=['codex'], task_id=task)
    env.board.conn.execute('UPDATE tasks SET status=? WHERE id=?', (status, task))
    begin_tracking(env, thread)
    upgrade(env)
    assert request(env, source)['state'] == 'queued'


def test_sealed_and_full_history_preserved(env):
    thread = env.thread()
    source = env.post('human', thread, 'Unfinished request', type='request', to=['codex'])
    env.post('codex', thread, 'Private partial work', sealed=True)
    env.post('human', thread, 'Historical decision', type='decision')
    env.post('codex', thread, 'Unrelated work finished')
    begin_tracking(env, thread)
    # Schema initialization must not rewrite post bodies, sequence numbers, visibility,
    # decisions, or existing audit records, even when run repeatedly.
    tables = ('posts', 'request_progress', 'request_events', 'answer_links', 'legacy_attention_answers')
    before = {name: [tuple(r) for r in env.board.conn.execute(f'SELECT * FROM {name}')] for name in tables}
    upgrade(env)
    upgrade(env)
    after = {name: [tuple(r) for r in env.board.conn.execute(f'SELECT * FROM {name}')] for name in tables}
    assert after == before
    assert request(env, source)['state'] == 'queued'


def test_explicit_evidence_closeout_survives_upgrade_without_closing_other_work(env):
    thread = env.thread()
    finished = env.post('human', thread, 'Verified work', type='request', to=['codex'])
    unfinished = env.post('human', thread, 'Separate unfinished work', type='request', to=['codex'])
    proof = env.post('codex', thread, 'Verified exact requested outcome, checks passed')
    requests.progress(env.board, env.p['codex'], env.sid['codex'], finished['id'], 'codex',
                      'finished', reason='Verified this request only', evidence_post_ids=[proof['id']])
    before = request(env, finished)
    upgrade(env)
    assert request(env, finished) == before
    assert request(env, unfinished)['state'] == 'queued'


def test_board_without_events_remains_unresolved(env):
    thread = env.thread()
    source = env.post('human', thread, type='request', to=['codex'])
    env.post('codex', thread, 'reply')
    upgrade(env)
    assert request(env, source)['state'] == 'queued'
    assert not env.board.conn.execute('SELECT 1 FROM request_events').fetchone()


def test_exact_human_answer_is_authorization_not_execution(env):
    thread = env.thread()
    source = env.post('codex', thread, 'May I implement this change?', type='question', needs_response=True,
                      decision_question=ASK)
    approved = env.post('human', thread, 'Approved; implement it', to=['codex'], answer_to=[source['id']])
    env.post('codex', thread, 'Acknowledged; implementation still pending')
    begin_tracking(env, thread)
    upgrade(env)
    assert request(env, approved)['state'] == 'queued'
    assert env.board.get_post(env.p['human'], approved['id'])['answer_to'] == [source['id']]
