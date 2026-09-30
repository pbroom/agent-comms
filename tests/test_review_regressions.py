"""Regression coverage for the integration-blocking review findings."""
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient

from agent_comms import db
from agent_comms.api import create_app
from agent_comms.core import Conflict, Forbidden, Invalid, Paused


def test_other_session_cannot_release_active_lease(env):
    task = env.accepted_task(env.thread())
    p, sid = env.p['codex'], env.sid['codex']
    env.board.claim_task(p, sid, task)
    other = env.session('codex')
    with pytest.raises(Forbidden):
        env.board.release_task(p, other, task)
    assert env.board.get_task(p, task)['owner_session'] == sid


@pytest.mark.parametrize('status', ['done', 'blocked', 'accepted'])
def test_expired_owner_must_explicitly_reclaim(env, status):
    task = env.accepted_task(env.thread())
    p, sid = env.p['codex'], env.sid['codex']
    env.board.claim_task(p, sid, task)
    env.clock.advance(1800)
    assert env.board.get_task(p, task)['lease_state'] == 'expired'
    with pytest.raises(Conflict, match='expired'):
        env.board.transition_task(p, sid, task, status)
    result = env.board.claim_task(p, sid, task)
    assert result['renewed'] is False
    assert env.board.get_task(p, task)['events'][-1]['event'] == 'reclaim'
    assert env.board.transition_task(p, sid, task, status)['status'] == status


def test_other_agent_can_reclaim_at_exact_expiry(env):
    task = env.accepted_task(env.thread())
    env.board.claim_task(env.p['codex'], env.sid['codex'], task)
    env.clock.advance(1800)
    assert env.board.claim_task(env.p['claude'], env.sid['claude'], task)['owner_agent'] == 'claude'


@pytest.mark.parametrize('mode', ['addressed', 'needs_response', 'history'])
def test_views_do_not_ack_unseen_posts(env, mode):
    tid = env.thread()
    env.post('codex', tid, 'first')
    env.post('codex', tid, 'second', to=['claude'], needs_response=True)
    p, sid = env.p['claude'], env.sid['claude']
    args = {'history': True, 'thread_id': tid} if mode == 'history' else {'only': mode}
    result = env.board.read_updates(p, sid, **args)
    assert result['ack_through'] is None
    with pytest.raises(Invalid, match='cannot acknowledge'):
        env.board.read_updates(p, sid, ack_through=2, **args)
    assert [post['body'] for post in env.board.read_updates(p, sid)['posts']] == ['first', 'second']


def test_http_filtered_read_has_no_ack_without_advancing(env):
    tid = env.thread()
    env.post('codex', tid, 'first')
    env.post('codex', tid, 'second', to=['claude'])
    client = TestClient(create_app(env.board))
    headers = {'Authorization': f"Bearer {env.tokens['claude']}", 'X-Board-Session': str(env.sid['claude'])}
    view = client.get('/api/updates?only=addressed', headers=headers)
    assert view.status_code == 200 and view.json()['ack_through'] is None
    assert len(client.get('/api/updates', headers=headers).json()['posts']) == 2


@pytest.mark.parametrize('operation', ['post', 'thread', 'close', 'summary', 'task', 'claim', 'release', 'transition'])
def test_pause_committed_before_write_lock_rejects_mutation(env, monkeypatch, operation):
    b, p, sid = env.board, env.p['codex'], env.sid['codex']
    tid = env.thread(as_='codex')
    task = env.accepted_task(tid)
    if operation in ('release', 'transition'):
        b.claim_task(p, sid, task)
    operations = {
        'post': lambda: env.post('codex', tid),
        'thread': lambda: b.create_thread(p, sid, 'new'),
        'close': lambda: b.set_thread_status(p, tid, 'closed'),
        'summary': lambda: b.set_summary(p, sid, tid, 'summary'),
        'task': lambda: b.create_task(p, sid, tid, title='new'),
        'claim': lambda: b.claim_task(p, sid, task),
        'release': lambda: b.release_task(p, sid, task),
        'transition': lambda: b.transition_task(p, sid, task, 'done'),
    }
    original = db.write_tx
    interleaved = False

    @contextmanager
    def pause_before_lock(conn):
        nonlocal interleaved
        if not interleaved:
            interleaved = True
            b.set_paused(env.p['human'], True)
        with original(conn) as transaction:
            yield transaction

    monkeypatch.setattr(db, 'write_tx', pause_before_lock)
    with pytest.raises(Paused):
        operations[operation]()
    assert interleaved and b.is_paused()


def test_mcp_filtered_read_rejects_ack(env, monkeypatch):
    import asyncio
    from mcp import Client
    from agent_comms.mcp_server import build_mcp

    tid = env.thread()
    env.post('codex', tid, 'first')
    env.post('codex', tid, 'second', to=['claude'])
    monkeypatch.setenv('AGENT_COMMS_TOKEN', env.tokens['claude'])

    async def check():
        async with Client(build_mcp(env.board, 'stdio')) as client:
            await client.call_tool('board_register', {'project': '/work/repo'})
            result = await client.call_tool('board_read_updates', {'only': 'addressed', 'ack_through': 2})
            assert result.is_error
            assert 'cannot acknowledge' in result.content[0].text
            result = await client.call_tool('board_read_updates', {})
            assert 'first' in result.content[0].text and 'second' in result.content[0].text

    asyncio.run(check())
