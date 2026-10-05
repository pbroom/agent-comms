"""Permission revocation and schema migration serialize across independent connections."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from agent_comms import db
from agent_comms.core import Board, Conflict, Forbidden


def test_parallel_v1_schema_upgrade(tmp_path):
    path = tmp_path / 'upgrade.db'
    conn = db.connect(path)
    # Preserve a real v1 task layout, with no authorization columns or grant table.
    schema = '\n'.join(line for line in db.SCHEMA.splitlines() if not any(
        line.strip().startswith(column) for column in
        ('category ', 'authorization_source ', 'authorization_grant_id ')))
    # Grant-table category is irrelevant to this old-schema fixture; remove that table entirely.
    start = schema.index('CREATE TABLE IF NOT EXISTS authorization_grants')
    end = schema.index('CREATE TABLE IF NOT EXISTS tasks', start)
    schema = schema[:start] + schema[end:]
    conn.executescript(schema)
    conn.execute('PRAGMA user_version=1')
    conn.close()
    gate = Barrier(6)

    def upgrade(_):
        connection = db.connect(path)
        try:
            gate.wait(timeout=10)
            db.init_schema(connection)
            return connection.execute('PRAGMA user_version').fetchone()[0]
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=6) as pool:
        assert list(pool.map(upgrade, range(6))) == [db.SCHEMA_VERSION] * 6
    conn = db.connect(path)
    try:
        columns = [r[1] for r in conn.execute('PRAGMA table_info(tasks)')]
        for column in ('category', 'authorization_source', 'authorization_grant_id'):
            assert columns.count(column) == 1
        assert conn.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        assert not conn.execute('PRAGMA foreign_key_check').fetchall()
    finally:
        conn.close()


def test_future_schema_rejected_without_mutation(tmp_path):
    conn = db.connect(tmp_path / 'future.db')
    try:
        conn.execute('CREATE TABLE sentinel (value TEXT)')
        conn.execute("INSERT INTO sentinel VALUES ('preserve')")
        conn.execute(f'PRAGMA user_version={db.SCHEMA_VERSION + 1}')
        before = list(conn.iterdump())
        with pytest.raises(RuntimeError, match='newer'):
            db.init_schema(conn)
        assert list(conn.iterdump()) == before
        assert conn.execute('PRAGMA user_version').fetchone()[0] == db.SCHEMA_VERSION + 1
    finally:
        conn.close()


@pytest.mark.parametrize('renew', [False, True])
@pytest.mark.parametrize('round_number', range(4))
def test_revoke_racing_claim_or_renew_leaves_no_authorized_owner(env, renew, round_number):
    env.settings.require_human_accept = True  # grants are what authorize claims when the gate is on
    permission = env.board.create_grant(
        env.p['human'], project='/work/repo', category='review', agents=['codex'],
        purpose='Review this integration only')
    task_id = env.board.create_task(
        env.p['codex'], env.sid['codex'], env.thread(), title='Review', category='review')['id']
    if renew:
        env.board.claim_task(env.p['codex'], env.sid['codex'], task_id)
    gate = Barrier(2)

    def run(revoke):
        board = Board(env.settings, clock=env.clock)
        try:
            gate.wait(timeout=10)
            if revoke:
                board.revoke_grant(env.p['human'], permission['id'])
                return 'revoked'
            try:
                operation = board.renew_task if renew else board.claim_task
                operation(env.p['codex'], env.sid['codex'], task_id)
                return 'claimed-before-revoke'
            except (Conflict, Forbidden):
                return 'denied-after-revoke'
        finally:
            board.conn.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run, [True, False]))
    assert results[0] == 'revoked'
    assert results[1] in {'claimed-before-revoke', 'denied-after-revoke'}
    task = env.board.get_task(env.p['codex'], task_id)
    assert task['status'] == 'proposed'
    assert task['owner_agent'] is None and task['owner_session'] is None
    assert not task['authorization']['active'] and not task['owner_may_work']
    with pytest.raises((Conflict, Forbidden)):
        env.board.claim_task(env.p['codex'], env.sid['codex'], task_id)
