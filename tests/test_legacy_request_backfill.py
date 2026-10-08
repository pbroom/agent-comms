"""v11 backfill: answered pre-tracking requests are finished with an audit event; nothing else changes."""
from agent_comms import db, requests, unstick


def states(env, post):
    return {r['recipient']: r['state'] for r in env.board.get_post(env.p['human'], post['id'])['requests']}


def upgrade(env):
    env.board.conn.execute('PRAGMA user_version=10')
    db.init_schema(env.board.conn)


def legacy_board(env):
    """Pre-tracking posts, then tracking starts (first explicit event), then a strict post-cutoff request."""
    thread = env.thread()
    answered = env.post('codex', thread, type='request', to=['claude'])
    unanswered = env.post('human', thread, type='request', to=['codex'])
    env.clock.advance(1)
    env.post('claude', thread, 'review done')
    env.clock.advance(60)
    tracked = env.post('human', thread, type='request', to=['claude'])
    requests.progress(env.board, env.p['claude'], env.sid['claude'], tracked['id'], 'claude', 'started')
    env.clock.advance(1)
    strict = env.post('human', thread, type='request', to=['claude'])
    env.clock.advance(1)
    env.post('claude', thread, 'unrelated later reply')
    return thread, answered, unanswered, strict


def test_answered_legacy_request_is_finished_with_audit(env):
    thread, answered, unanswered, strict = legacy_board(env)
    assert 'claude' in unstick.stuck_agents(env.board, thread)[0]
    upgrade(env)
    assert states(env, answered) == {'claude': 'finished'}
    assert states(env, unanswered) == {'codex': 'queued'}
    assert states(env, strict) == {'claude': 'queued'}
    row = env.board.get_post(env.p['human'], answered['id'])['requests'][0]
    assert row['reason'].startswith('legacy: answered by #') and len(row['evidence_post_ids']) == 1
    [event] = env.board.conn.execute("SELECT * FROM request_events WHERE post_id=?", (answered['id'],)).fetchall()
    assert event['event_source'] == 'migration' and event['actor'] is None and event['state'] == 'finished'
    # The unanswered legacy request keeps its virtual row: no routing attempt is spent.
    assert not env.board.conn.execute('SELECT 1 FROM request_progress WHERE post_id=?', (unanswered['id'],)).fetchone()
    reasons = unstick.stuck_agents(env.board, thread)[1]
    stalled = {(x['agent'], i) for x in reasons if x['kind'] == 'unanswered' for i in x['post_ids']}
    assert (('claude', answered['id']) not in stalled and ('codex', unanswered['id']) in stalled
            and ('claude', strict['id']) in stalled)


def test_terminal_task_finishes_legacy_request(env):
    thread = env.thread()
    task = env.accepted_task(thread)
    post = env.post('codex', thread, type='request', to=['claude'], task_id=task)
    env.board.conn.execute("UPDATE tasks SET status='done' WHERE id=?", (task,))
    env.clock.advance(60)
    later = env.post('human', thread, type='request', to=['claude'])
    requests.progress(env.board, env.p['claude'], env.sid['claude'], later['id'], 'claude', 'started')
    upgrade(env)
    assert states(env, post) == {'claude': 'finished'}
    assert env.board.get_post(env.p['human'], post['id'])['requests'][0]['reason'] == f'legacy: task {task} is done'


def test_backfill_is_idempotent(env):
    thread, answered, *_ = legacy_board(env)
    upgrade(env)
    count = env.board.conn.execute('SELECT COUNT(*) FROM request_events').fetchone()[0]
    upgrade(env)
    db.init_schema(env.board.conn)
    assert env.board.conn.execute('SELECT COUNT(*) FROM request_events').fetchone()[0] == count


def test_v10_board_without_events_is_unchanged(env):
    thread = env.thread()
    post = env.post('codex', thread, type='request', to=['claude'])
    env.post('claude', thread, 'reply')
    upgrade(env)
    assert states(env, post) == {'claude': 'queued'}


def test_pre_v7_board_backfills_everything(env):
    thread = env.thread()
    post = env.post('codex', thread, type='request', to=['claude'])
    env.post('claude', thread, 'reply')
    env.board.conn.execute('PRAGMA user_version=6')
    db.init_schema(env.board.conn)
    assert states(env, post) == {'claude': 'finished'}
