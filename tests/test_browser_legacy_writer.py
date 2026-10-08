"""Obsolete writers must not bypass ordinary browser request preflight.

Every test uses an isolated board and a separate legacy-style transaction.
There are no continuations, browsers, network calls, or live-board changes.
"""
import sqlite3

import pytest

from agent_comms import browser_readiness as br, db, requests


@pytest.fixture
def browser_request(env):
    post = env.post('human', env.thread(), type='request', to=['codex'])
    br.bind_request(env.board, env.p['human'], env.sid['human'],
                    post['id'], 'codex', 'http://localhost:5185/about')
    assert env.board.conn.execute('SELECT COUNT(*) FROM continuations').fetchone()[0] == 0
    return post


@pytest.fixture
def legacy(env):
    conn = db.connect(env.settings.db_path)
    yield conn
    if conn.in_transaction:
        conn.execute('ROLLBACK')
    conn.close()


def blocked_owner(env, post):
    requests.progress(env.board, env.p['codex'], env.sid['codex'],
                      post['id'], 'codex', 'blocked', reason='Awaiting browser preflight')


def test_legacy_cannot_insert_started_browser_request(env, browser_request, legacy):
    legacy.execute('BEGIN IMMEDIATE')
    with pytest.raises(sqlite3.IntegrityError, match='updated server'):
        legacy.execute('''INSERT INTO request_progress
            (post_id,recipient,state,assigned_agent,assigned_session,reason,
             evidence_post_ids,version,updated_at)
            VALUES (?,?,'started',?,?,'','[]',1,?)''',
            (browser_request['id'], 'codex', 'codex', env.sid['codex'], env.clock()))


def test_legacy_cannot_update_browser_request_to_started(env, browser_request, legacy):
    blocked_owner(env, browser_request)
    legacy.execute('BEGIN IMMEDIATE')
    with pytest.raises(sqlite3.IntegrityError, match='updated server'):
        legacy.execute("UPDATE request_progress SET state='started' WHERE post_id=? AND recipient='codex'",
                       (browser_request['id'],))


@pytest.mark.parametrize('column,value', [
    ('project', '/other/project'), ('worktree', '/other/worktree'),
    ('dispatch_run_id', 'different-process'), ('client_session_id', 'different-conversation'),
])
def test_legacy_cannot_change_browser_owner_identity(env, browser_request, legacy, column, value):
    blocked_owner(env, browser_request)
    legacy.execute('BEGIN IMMEDIATE')
    with pytest.raises(sqlite3.IntegrityError, match='updated server'):
        legacy.execute(f'UPDATE sessions SET {column}=? WHERE id=?', (value, env.sid['codex']))


def test_legacy_browser_owner_can_still_heartbeat(env, browser_request, legacy):
    blocked_owner(env, browser_request)
    legacy.execute('BEGIN IMMEDIATE')
    legacy.execute('UPDATE sessions SET last_seen=last_seen+1 WHERE id=?', (env.sid['codex'],))
    legacy.execute('COMMIT')
    assert legacy.execute('SELECT COUNT(*) FROM managed_write_permit').fetchone()[0] == 0
