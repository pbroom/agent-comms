"""Pickup is evidence-based and does not mutate lifecycle or read cursors."""
from agent_comms import pickup, requests


def request(env, thread=None, **kw):
    return env.post('human', thread or env.thread(), type='request', to=['codex', 'claude'], **kw)


def project(env, post, actor='human'):
    return pickup.for_thread(env.board, env.p[actor], post['thread_id'])


def progress(env, post, agent, state, **kw):
    return requests.progress(env.board, env.p[agent], env.sid[agent], post['id'], agent, state, **kw)


def test_waiting_keeps_every_recipient_and_stable_deadline(env):
    post = request(env)
    initial = project(env, post)
    assert [r['recipient'] for r in initial['waiting']] == ['codex', 'claude']
    assert not initial['complete']
    env.clock.advance(pickup.PICKUP_WAIT_SECONDS)
    env.post('human', post['thread_id'], body='unrelated read or reply', to=[])
    later = project(env, post)
    assert all(r['overdue'] for r in later['waiting'])
    assert [r['deadline_at'] for r in later['waiting']] == [r['deadline_at'] for r in initial['waiting']]


def test_processing_requires_current_assigned_session_acknowledgement(env):
    post = request(env)
    progress(env, post, 'codex', 'started')
    state = project(env, post)
    assert [r['recipient'] for r in state['processing']] == ['codex']
    assert [r['recipient'] for r in state['waiting']] == ['claude']
    env.board.conn.execute("UPDATE request_events SET actor='human' WHERE post_id=?", (post['id'],))
    state = project(env, post)
    assert not state['processing']
    assert len(state['waiting']) == 2


def test_different_assignment_cannot_reuse_old_start(env):
    post = request(env)
    progress(env, post, 'codex', 'started')
    other = env.session('codex')
    env.board.conn.execute('UPDATE request_progress SET assigned_session=? WHERE post_id=?', (other, post['id']))
    assert not project(env, post)['processing']


def test_human_block_and_explicit_human_finish_are_retained(env):
    post = request(env)
    requests.progress(env.board, env.p['human'], env.sid['human'], post['id'], 'codex', 'blocked', reason='Permission gate')
    assert project(env, post)['blocked'][0]['reason'] == 'Permission gate'
    for recipient in ('codex', 'claude'):
        requests.progress(env.board, env.p['human'], env.sid['human'], post['id'], recipient, 'finished', reason='Verified result')
    assert project(env, post)['complete']


def test_finished_without_matching_event_is_uncertain(env):
    post = request(env)
    progress(env, post, 'codex', 'finished', reason='Verified')
    env.board.conn.execute('DELETE FROM request_events WHERE post_id=?', (post['id'],))
    assert any(r['state'] == 'finished' for r in project(env, post)['waiting'])
    assert not project(env, post)['complete']


def test_old_visible_requests_are_not_truncated_and_sealed_remain_private(env):
    post = request(env)
    for _ in range(65):
        env.post('human', post['thread_id'])
    sealed = env.post('claude', post['thread_id'], type='request', to=['codex'], sealed=True)
    state = project(env, post, 'codex')
    assert len(state['waiting']) == 2
    assert all(r['post_id'] != sealed['id'] for r in state['waiting'])
    assert len(project(env, post)['waiting']) == 3


def test_unfinished_task_prevents_complete_and_empty_thread_is_not_complete(env):
    tid = env.thread()
    assert not pickup.for_thread(env.board, env.p['human'], tid)['complete']
    task = env.accepted_task(tid)
    assert not pickup.for_thread(env.board, env.p['human'], tid)['complete']
    env.board.transition_task(env.p['human'], env.sid['human'], task, 'done')
    assert pickup.for_thread(env.board, env.p['human'], tid)['complete']


def test_projection_is_read_only(env):
    post = request(env)
    before = env.board.conn.total_changes
    project(env, post)
    assert env.board.conn.total_changes == before


def test_read_cursor_and_process_launch_do_not_acknowledge_pickup(env):
    post = request(env)
    env.board.ack(env.p['codex'], env.sid['codex'], post['seq'], thread_id=post['thread_id'])
    env.board.conn.execute("INSERT INTO board_state(key,value,updated_at) VALUES ('dispatch.run.test',?,?)",
        ('{"agent":"codex","status":"running","thread_id":'+str(post['thread_id'])+'}', env.clock()))
    assert len(project(env, post)['waiting']) == 2
    assert not project(env, post)['processing']


def test_only_exact_owner_heartbeat_sustains_processing(env):
    post = request(env)
    progress(env, post, 'codex', 'started')
    env.clock.advance(pickup.PICKUP_WAIT_SECONDS + 1)
    other = env.session('codex')
    env.board.heartbeat(env.p['codex'], other)
    state = project(env, post)
    assert not state['processing']
    assert state['blocked'][0]['recipient'] == 'codex'
    assert 'stale' in state['blocked'][0]['reason']
    env.board.heartbeat(env.p['codex'], env.sid['codex'])
    assert project(env, post)['processing'][0]['recipient'] == 'codex'


def test_relevant_live_task_lease_sustains_processing_without_recent_heartbeat(env):
    tid = env.thread()
    task = env.accepted_task(tid)
    post = request(env, tid, task_id=task)
    progress(env, post, 'codex', 'started')
    env.clock.advance(pickup.PICKUP_WAIT_SECONDS + 1)
    env.board.conn.execute("UPDATE tasks SET status='working',owner_agent='codex',owner_session=?,lease_expires_at=? WHERE id=?",
        (env.sid['codex'], env.clock()+60, task))
    assert project(env, post)['processing'][0]['recipient'] == 'codex'
    env.board.conn.execute('UPDATE tasks SET lease_expires_at=? WHERE id=?', (env.clock()-1, task))
    assert project(env, post)['blocked'][0]['recipient'] == 'codex'


def test_reason_only_queue_updates_do_not_reset_deadline(env):
    post = request(env)
    progress(env, post, 'codex', 'queued', reason='Assigned and waiting')
    deadline = project(env, post)['waiting'][0]['deadline_at']
    env.clock.advance(pickup.PICKUP_WAIT_SECONDS)
    progress(env, post, 'codex', 'queued', reason='Still waiting')
    item = project(env, post)['waiting'][0]
    assert item['deadline_at'] == deadline
    assert item['overdue']


def test_new_assignment_starts_new_waiting_window(env):
    post = request(env)
    env.clock.advance(pickup.PICKUP_WAIT_SECONDS)
    progress(env, post, 'codex', 'queued', reason='First explicit assignment')
    item = project(env, post)['waiting'][0]
    assert not item['overdue']
    env.clock.advance(pickup.PICKUP_WAIT_SECONDS)
    assert project(env, post)['waiting'][0]['overdue']
