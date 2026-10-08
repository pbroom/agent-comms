"""Long-lived pre-migration servers cannot bypass managed-work guards."""
import sqlite3

import pytest

from agent_comms import db
from test_workstreams import stack, create, probe, progress  # noqa: F401


@pytest.fixture
def legacy(stack):
    post = create(stack)
    conn = db.connect(stack['env'].settings.db_path)
    yield conn, post
    conn.close()


@pytest.mark.parametrize('target', ['task_claim', 'task_done', 'task_delete', 'root_update',
                                    'root_delete', 'assign', 'finish', 'request_delete',
                                    'request_replace', 'session_project', 'session_worktree', 'session_run'])
def test_old_writer_cannot_mutate_managed_work(stack, legacy, target):
    conn, post = legacy
    statements = {
        'task_claim': ("UPDATE tasks SET owner_session=?, status='working' WHERE id=?", (stack['env'].sid['claude'], post['task_id'])),
        'task_done': ("UPDATE tasks SET status='done' WHERE id=?", (post['task_id'],)),
        'task_delete': ('DELETE FROM tasks WHERE id=?', (post['task_id'],)),
        'root_update': ("UPDATE tasks SET status='accepted' WHERE id=?", (stack['root'],)),
        'root_delete': ('DELETE FROM tasks WHERE id=?', (stack['root'],)),
        'assign': ('UPDATE request_progress SET assigned_session=? WHERE post_id=?', (stack['env'].sid['claude'], post['id'])),
        'finish': ("UPDATE request_progress SET state='finished' WHERE post_id=?", (post['id'],)),
        'request_delete': ('DELETE FROM request_progress WHERE post_id=?', (post['id'],)),
        'request_replace': ('INSERT OR REPLACE INTO request_progress SELECT * FROM request_progress WHERE post_id=?', (post['id'],)),
        'session_project': ("UPDATE sessions SET project='/other' WHERE id=?", (stack['env'].sid['codex'],)),
        'session_worktree': ("UPDATE sessions SET worktree='/other' WHERE id=?", (stack['env'].sid['claude'],)),
        'session_run': ("UPDATE sessions SET dispatch_run_id='other' WHERE id=?", (stack['env'].sid['claude'],)),
    }
    conn.execute('BEGIN IMMEDIATE')
    try:
        with pytest.raises(sqlite3.IntegrityError, match='updated server'):
            conn.execute(*statements[target])
    finally:
        conn.execute('ROLLBACK')


def test_old_writer_can_heartbeat_and_update_unrelated_work(stack, legacy):
    conn, post = legacy
    env = stack['env']
    unrelated = env.board.create_task(env.p['human'], env.sid['human'], stack['thread'], title='unrelated')['id']
    conn.execute('BEGIN IMMEDIATE')
    conn.execute('UPDATE sessions SET last_seen=last_seen+1 WHERE id=?', (env.sid['codex'],))
    conn.execute('UPDATE sessions SET worktree=worktree WHERE id=?', (env.sid['claude'],))
    conn.execute("UPDATE tasks SET status='done' WHERE id=?", (unrelated,))
    conn.execute('COMMIT')
    assert conn.execute('SELECT status FROM tasks WHERE id=?', (unrelated,)).fetchone()[0] == 'done'
    assert conn.execute('SELECT COUNT(*) FROM managed_write_permit').fetchone()[0] == 0


def test_updated_guarded_flow_succeeds_and_permit_is_never_committed(stack, legacy):
    conn, post = legacy
    probe(stack, 'codex')
    progress(stack, post, 'started')
    assert conn.execute('SELECT status FROM tasks WHERE id=?', (post['task_id'],)).fetchone()[0] == 'working'
    assert conn.execute('SELECT COUNT(*) FROM managed_write_permit').fetchone()[0] == 0
    with db.write_tx(stack['env'].board.conn):
        assert stack['env'].board.conn.execute('SELECT COUNT(*) FROM managed_write_permit').fetchone()[0] == 1
        assert conn.execute('SELECT COUNT(*) FROM managed_write_permit').fetchone()[0] == 0
    with pytest.raises(RuntimeError):
        with db.write_tx(stack['env'].board.conn):
            raise RuntimeError('simulate failed writer')
    assert conn.execute('SELECT COUNT(*) FROM managed_write_permit').fetchone()[0] == 0


def test_v8_upgrade_preserves_data_and_installs_fence(stack, legacy):
    conn, post = legacy
    for row in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'managed_%'").fetchall():
        conn.execute('DROP TRIGGER '+row[0])
    conn.execute('DROP TABLE managed_write_permit')
    conn.execute('PRAGMA user_version=8')
    db.init_schema(conn)
    assert conn.execute('SELECT COUNT(*) FROM managed_write_permit').fetchone()[0] == 0
    assert conn.execute('SELECT task_id FROM continuations WHERE post_id=?', (post['id'],)).fetchone()[0] == post['task_id']
    with pytest.raises(sqlite3.IntegrityError, match='updated server'):
        conn.execute("UPDATE tasks SET status='done' WHERE id=?", (post['task_id'],))


@pytest.fixture
def browser_legacy(env):
    from agent_comms import browser_readiness as br, requests
    post = env.post('human', env.thread(), type='request', to=['codex', 'claude'])
    br.bind_request(env.board, env.p['human'], env.sid['human'], post['id'], 'codex', 'https://example.com/target')
    conn = db.connect(env.settings.db_path)
    yield conn, post
    conn.close()


@pytest.mark.parametrize('operation', ['insert', 'update', 'delete', 'replace', 'move_recipient'])
def test_old_writer_cannot_bypass_browser_bound_request(env, browser_legacy, operation):
    from agent_comms import requests
    conn, post = browser_legacy
    if operation != 'insert':
        requests.progress(env.board, env.p['codex'], env.sid['codex'], post['id'], 'codex', 'queued', reason='waiting')
    statements = {
        'insert': ("INSERT INTO request_progress(post_id,recipient,state,assigned_agent,assigned_session,reason,evidence_post_ids,version,updated_at) VALUES (?,'codex','started','codex',?,'','[]',1,0)", (post['id'], env.sid['codex'])),
        'update': ("UPDATE request_progress SET state='started' WHERE post_id=?", (post['id'],)),
        'delete': ('DELETE FROM request_progress WHERE post_id=?', (post['id'],)),
        'replace': ('INSERT OR REPLACE INTO request_progress SELECT * FROM request_progress WHERE post_id=?', (post['id'],)),
        'move_recipient': ("UPDATE request_progress SET recipient='claude' WHERE post_id=?", (post['id'],)),
    }
    conn.execute('BEGIN IMMEDIATE')
    try:
        with pytest.raises(sqlite3.IntegrityError, match='updated server'):
            conn.execute(*statements[operation])
    finally:
        conn.execute('ROLLBACK')


@pytest.mark.parametrize('identity', ['project', 'worktree', 'dispatch_run_id', 'client_session_id'])
@pytest.mark.parametrize('binding', ['assignment', 'probe', 'attempt'])
def test_old_writer_cannot_change_browser_execution_identity(env, browser_legacy, identity, binding):
    from agent_comms import browser_readiness as br, requests
    from test_browser_routing import proof, URL, CTX
    conn, post = browser_legacy
    if binding == 'assignment':
        requests.progress(env.board, env.p['codex'], env.sid['codex'], post['id'], 'codex', 'queued', reason='waiting')
    elif binding == 'probe':
        proof(env)
    else:
        br.begin_probe(env.board, env.p['codex'], env.sid['codex'], URL, CTX)
    conn.execute('BEGIN IMMEDIATE')
    try:
        with pytest.raises(sqlite3.IntegrityError, match='updated server'):
            conn.execute(f'UPDATE sessions SET {identity}=? WHERE id=?', ('/changed', env.sid['codex']))
        conn.execute('UPDATE sessions SET last_seen=last_seen+1 WHERE id=?', (env.sid['codex'],))
        conn.execute('UPDATE sessions SET client_session_id=client_session_id WHERE id=?', (env.sid['codex'],))
    finally:
        conn.execute('ROLLBACK')


def test_legacy_unbound_recipient_and_unrelated_session_still_work(env, browser_legacy):
    conn, post = browser_legacy
    conn.execute('BEGIN IMMEDIATE')
    conn.execute("INSERT INTO request_progress(post_id,recipient,state,assigned_agent,reason,evidence_post_ids,version,updated_at) VALUES (?,'claude','started','claude','','[]',1,0)", (post['id'],))
    conn.execute("UPDATE sessions SET client_session_id='unrelated' WHERE id=?", (env.sid['claude'],))
    conn.execute('COMMIT')
    assert conn.execute('SELECT COUNT(*) FROM managed_write_permit').fetchone()[0] == 0
