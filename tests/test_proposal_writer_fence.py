"""Old loaded writers fail before producing proposals without current task provenance."""
import sqlite3

import pytest

from agent_comms import db
from conftest import ASK


def raw_post(c, env, thread, kind='proposal', task_id=None):
    c.execute('''INSERT INTO posts(seq,thread_id,session_id,agent,type,body,to_agents,
                 needs_response,task_id,refs,sealed,final,created_at)
                 VALUES ((SELECT COALESCE(MAX(seq),0)+1 FROM posts),?,?,?,?,?,'[]',0,?,'[]',0,0,?)''',
              (thread, env.sid['codex'], 'codex', kind, 'old runtime', task_id, env.clock()))


def test_current_propose_task_links_source_and_removes_permit(env):
    thread = env.thread()
    post = env.post('codex', thread, type='proposal', propose_task={'title': 'work', 'acceptance': 'verified'})
    assert env.board.conn.execute('SELECT proposed_by_post FROM tasks WHERE id=?',
                                  (post['task_id'],)).fetchone()[0] == post['id']
    assert not env.board.conn.execute('SELECT * FROM proposal_write_permit').fetchall()
    assert post['id'] not in {p['id'] for p in env.board.snapshot(env.p['human'])['needs_you']}
    human = env.post('human', thread, type='proposal')
    assert human['agent'] == 'human'
    assert not env.board.conn.execute('SELECT * FROM proposal_write_permit').fetchall()
    # Existing-task proposals still ask for a decision; no blanket suppression.
    another = env.post('codex', thread, type='proposal', task_id=post['task_id'], decision_question=ASK)
    assert another['id'] in {p['id'] for p in env.board.snapshot(env.p['human'])['needs_you']}


def test_old_task_and_proposal_transaction_rolls_back(env):
    thread = env.thread()
    c = db.connect(env.settings.db_path)
    before = env.board.conn.execute('SELECT COUNT(*) FROM tasks').fetchone()[0]
    try:
        with pytest.raises(sqlite3.IntegrityError, match='updated connection'):
            c.execute('BEGIN IMMEDIATE')
            try:
                # Older runtimes know the managed permit, but not proposal provenance.
                c.execute('INSERT INTO managed_write_permit(id) VALUES (1)')
                tid = env.board._insert_task(c, env.p['codex'], env.sid['codex'], thread,
                                            title='old task', acceptance='', intends_files=[], depends_on=[])
                raw_post(c, env, thread, task_id=tid)
                c.execute('COMMIT')
            except BaseException:
                c.execute('ROLLBACK')
                raise
        assert c.execute('SELECT COUNT(*) FROM tasks').fetchone()[0] == before
        assert not c.execute('SELECT * FROM proposal_write_permit').fetchall()
        assert not c.execute('SELECT * FROM managed_write_permit').fetchall()
        # Loaded clients can continue ordinary authorized coordination.
        for kind in ('status', 'request'):
            raw_post(c, env, thread, kind)
    finally:
        c.close()


def test_permit_requires_transaction_and_is_cleaned_on_failure(env):
    c = env.board.conn
    with pytest.raises(ValueError, match='active transaction'):
        with db.proposal_write(c):
            pass
    with db.write_tx(c):
        with pytest.raises(RuntimeError, match='failed'):
            with db.proposal_write(c):
                raise RuntimeError('failed')
        assert not c.execute('SELECT * FROM proposal_write_permit').fetchall()
    with pytest.raises(RuntimeError):
        with db.write_tx(c):
            with db.proposal_write(c):
                raise RuntimeError('rollback')
    assert not c.execute('SELECT * FROM proposal_write_permit').fetchall()


def test_other_connection_cannot_borrow_uncommitted_permit(env):
    thread = env.thread()
    other = db.connect(env.settings.db_path)
    other.execute('PRAGMA busy_timeout=1')
    try:
        with db.write_tx(env.board.conn):
            with db.proposal_write(env.board.conn):
                assert not other.execute('SELECT * FROM proposal_write_permit').fetchall()
                with pytest.raises(sqlite3.OperationalError, match='locked'):
                    raw_post(other, env, thread)
        with pytest.raises(sqlite3.IntegrityError, match='updated connection'):
            raw_post(other, env, thread)
    finally:
        other.close()


def test_v11_upgrade_installs_fence_without_reclassifying_sources(env):
    thread = env.thread()
    post = env.post('codex', thread, type='proposal', decision_question=ASK)
    c = env.board.conn
    c.execute('DROP TRIGGER proposal_writer_fence')
    c.execute('DROP TABLE proposal_write_permit')
    c.execute('PRAGMA user_version=11')
    old = db.connect(env.settings.db_path)  # already open before migration
    db.init_schema(c)
    db.init_schema(c)  # restart is idempotent
    assert c.execute('PRAGMA user_version').fetchone()[0] == db.SCHEMA_VERSION
    assert post['id'] in {p['id'] for p in env.board.snapshot(env.p['human'])['needs_you']}
    try:
        with pytest.raises(sqlite3.IntegrityError, match='updated connection'):
            raw_post(old, env, thread)
    finally:
        old.close()
    assert env.board.get_post(env.p['codex'], post['id'])['body'] == post['body']
