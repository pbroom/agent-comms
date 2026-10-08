"""Generic pickup delivery remains bounded without taking over assigned work."""
import threading

import pytest

from agent_comms import requests
from agent_comms.core import Conflict
from conftest import PROJECT
from test_dispatch import denv, allow, human_post, new_dispatcher


def progress(env, post):
    return next(row for row in env.board.get_post(env.p['human'], post['id'])['requests']
                if row['recipient'] == 'codex')


def test_recovers_virtual_human_answer_past_scan_mark(denv):
    env = denv
    allow(env, agents=['codex'])
    post = human_post(env, ['codex'])
    assert env.d._scan()
    env.d._save(**{env.d.PENDING_KEY: {}})
    assert env.d._get(env.d.MARK_KEY) >= post['seq']
    env.d.tick()
    assert len(env.spawner.calls) == 1
    assert progress(env, post)['state'] == 'queued'
    run_id = env.d.running['codex'].run_id
    env.board.register_session(env.p['codex'], PROJECT, env.workdir, dispatch_run_id=run_id)

    assert progress(env, post)['state'] == 'queued'
    for _ in range(3):
        env.d.tick()
    assert len(env.spawner.calls) == 1


def test_unrelated_heartbeat_cannot_hide_pickup_forever(denv):
    env = denv
    allow(env, agents=['codex'])
    post = human_post(env, ['codex'])
    sid = env.session('codex', project='/unrelated')
    for delay in (0, 2399):
        env.clock.advance(delay)
        env.board.heartbeat(env.p['codex'], sid)
        env.d.tick()
        assert progress(env, post)['state'] == 'queued'
    env.clock.advance(1)
    env.board.heartbeat(env.p['codex'], sid)
    env.d.tick()
    row = progress(env, post)
    assert row['state'] == 'blocked'
    assert 'Pickup overdue' in row['reason']
    assert not env.spawner.calls
    event = env.board.conn.execute('SELECT * FROM request_events WHERE post_id=? ORDER BY version DESC',
                                   (post['id'],)).fetchone()
    assert event['actor'] is None and event['event_source'] == 'dispatcher'


def test_assigned_session_is_never_commandeered_or_forgotten(denv):
    env = denv
    allow(env, agents=['codex'])
    post = human_post(env, ['codex'])
    requests.progress(env.board, env.p['codex'], env.sid['codex'], post['id'], 'codex',
                      'queued', reason='Assigned, not yet acknowledged')
    env.d.tick()
    assert not env.spawner.calls
    env.clock.advance(2400)
    env.d.tick()
    row = progress(env, post)
    assert row['state'] == 'blocked'
    assert row['assigned_session'] == env.sid['codex']
    assert not env.spawner.calls


def test_reason_only_update_does_not_reset_pickup_deadline(denv):
    env = denv
    allow(env, agents=['codex'])
    post = human_post(env, ['codex'])
    env.clock.advance(2300)
    requests.progress(env.board, env.p['human'], env.sid['human'], post['id'], 'codex',
                      'queued', reason='Still awaiting pickup')
    env.clock.advance(100)
    env.d.tick()
    assert progress(env, post)['state'] == 'blocked'
    assert not env.spawner.calls


@pytest.mark.parametrize('first', ['acknowledgement', 'deadline'])
def test_deadline_and_exact_acknowledgement_are_serialized(denv, first):
    env = denv
    allow(env, agents=['codex'])
    post = human_post(env, ['codex'])
    env.clock.advance(2400)
    def acknowledge():
        return requests.progress(env.board, env.p['codex'], env.sid['codex'], post['id'],
                                 'codex', 'started', expected_version=0)
    if first == 'acknowledgement':
        acknowledge()
        env.d.tick()
        assert progress(env, post)['state'] == 'started'
    else:
        env.d.tick()
        with pytest.raises(Conflict):
            acknowledge()
        assert progress(env, post)['state'] == 'blocked'
    assert not env.spawner.calls


def test_matching_live_child_uses_process_timeout_not_pickup_deadline(denv):
    env = denv
    env.config.timeout_minutes = 60
    allow(env, agents=['codex'])
    post = human_post(env, ['codex'])
    env.d.tick()
    env.clock.advance(2401)
    env.d.tick()
    assert progress(env, post)['state'] == 'queued'
    assert len(env.spawner.calls) == 1
    env.spawner.children[0].code = 0
    env.d.tick()
    assert progress(env, post)['state'] == 'blocked'
    env.d._save(**{env.d.PENDING_KEY: {}})
    env.clock.advance(5000)
    env.d.tick()
    assert len(env.spawner.calls) == 1


@pytest.mark.parametrize('gate', ['none', 'revoked', 'paused', 'wrong_agent'])
def test_recovery_never_broadens_existing_dispatch_approval(denv, gate):
    env = denv
    rule = None if gate == 'none' else allow(env, agents=['claude'] if gate == 'wrong_agent' else ['codex'])
    post = human_post(env, ['codex'])
    env.d._scan()
    env.d._save(**{env.d.PENDING_KEY: {}})
    if gate == 'revoked':
        env.board.revoke_dispatch_rule(env.p['human'], rule['id'])
    elif gate == 'paused':
        env.board.set_paused(env.p['human'], True)
    env.clock.advance(2401)
    env.d.tick()
    assert progress(env, post)['state'] == 'queued'
    assert not env.spawner.calls


def test_recovery_uses_assigned_agent_and_preserves_original_recipient(denv):
    env = denv
    allow(env, agents=['claude'])
    post = human_post(env, ['claude', 'codex'])
    requests.progress(env.board, env.p['claude'], env.sid['claude'], post['id'], 'claude',
                      'finished', reason='Separate original recipient request finished')
    # An unassigned queued row keeps its original request identity after routing.
    # Exercise the durable representation directly, without claiming an owner.
    row = progress(env, post)
    requests._save(env.board, env.p['human'], env.sid['human'], row, 'queued',
                   'Route this request to its other original addressee', [], 'claude', None)
    env.d._scan()
    env.d._save(**{env.d.PENDING_KEY: {}})
    env.clock.advance(121)
    env.d.tick()
    assert len(env.spawner.calls) == 1
    assert env.spawner.agents() == ['claude-fake']
    assert progress(env, post)['recipient'] == 'codex'
    assert progress(env, post)['state'] == 'queued'
    env.spawner.children[0].code = 0
    env.d.tick()
    assert progress(env, post)['state'] == 'blocked'
    rows = env.board.get_post(env.p['human'], post['id'])['requests']
    assert next(row for row in rows if row['recipient'] == 'claude')['state'] == 'finished'


def test_simultaneous_deadline_and_acknowledgement_have_one_winner(denv):
    env = denv
    allow(env, agents=['codex'])
    post = human_post(env, ['codex'])
    env.clock.advance(2400)
    barrier = threading.Barrier(2)
    results, failures = [], []
    def acknowledge():
        barrier.wait()
        try:
            results.append(requests.progress(env.board, env.p['codex'], env.sid['codex'],
                post['id'], 'codex', 'started', expected_version=0))
        except Exception as exc:
            failures.append(exc)
    def expire():
        barrier.wait()
        try:
            env.d._expire_generic_pickups()
        except Exception as exc:
            failures.append(exc)
    workers = [threading.Thread(target=acknowledge), threading.Thread(target=expire)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=5)
        assert not worker.is_alive()
    row = progress(env, post)
    assert row['version'] == 1
    assert row['state'] in ('started', 'blocked')
    assert len(results) == (1 if row['state'] == 'started' else 0)
    assert len(failures) == (0 if results else 1)
    assert all(isinstance(error, Conflict) for error in failures)
    assert env.board.conn.execute('SELECT COUNT(*) FROM request_events WHERE post_id=?',
                                  (post['id'],)).fetchone()[0] == 1



def test_coalesced_delivery_keeps_both_original_obligations(denv):
    env = denv
    allow(env, agents=['claude'])
    post = human_post(env, ['claude', 'codex'])
    requests._save(env.board, env.p['human'], env.sid['human'], progress(env, post), 'queued',
                   'Route other original request to claude', [], 'claude', None)
    env.d.tick()
    assert len(env.spawner.calls) == 1
    record = env.d._get(env.d.RUN_PREFIX + env.d.running['claude'].run_id)
    assert set(record['request_recipients']) == {'codex', 'claude'}
    env.spawner.children[0].code = 0
    env.d.tick()
    rows = env.board.get_post(env.p['human'], post['id'])['requests']
    assert {row['recipient'] for row in rows} == {'codex', 'claude'}
    assert all(row['state'] == 'blocked' for row in rows)
    env.d.tick()
    assert len(env.spawner.calls) == 1


def test_pickup_deadline_ignores_malformed_historical_run_record(denv):
    env = denv
    allow(env, agents=['codex'])
    post = human_post(env, ['codex'])
    env.board.conn.execute('INSERT INTO board_state(key,value,updated_at) VALUES (?,?,?)',
                           ('dispatch.run.broken', '{broken', env.clock()))
    env.clock.advance(2400)
    env.d._expire_generic_pickups()
    assert progress(env, post)['state'] == 'blocked'
