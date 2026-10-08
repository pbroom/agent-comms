"""Browser readiness uses local evidence only; these tests never open a browser."""
import json

import pytest

from agent_comms import browser_readiness as br, requests
from agent_comms.core import Board, Conflict, Forbidden, Invalid
from conftest import PROJECT

URL = 'http://localhost:5185/about'
CTX = {'kind': 'desktop', 'transport': 'iab', 'connection_id': 'tab-1'}
EVIDENCE = {'http_status': 200, 'rendered_url': URL,
            'rendered_identity': 'NEXUS About LOCAL 1.43.17',
            'interaction': 'open About tab', 'interaction_result': 'About tab selected'}


@pytest.fixture(autouse=True)
def browser_schema(env):
    env.board.conn.executescript(br.SCHEMA)


def probe(env, actor='codex', sid=None, context=None, evidence=None):
    return br.report_probe(env.board, env.p[actor], sid or env.sid[actor], URL,
                           context or CTX, EVIDENCE if evidence is None else evidence)


def fail(env, failure='disconnected', actor='codex', sid=None):
    return br.report_failure(env.board, env.p[actor], sid or env.sid[actor], URL,
                             CTX, failure, 'Adapter reported ' + failure)


def task(env, recipient='codex'):
    post = env.post('human', env.thread(), type='request', to=[recipient])
    br.bind_request(env.board, env.p['human'], env.sid['human'], post['id'], recipient, URL)
    return post


def test_complete_exact_context_probe_and_missing_context(env):
    post = task(env)
    assert not br.eligible(env.board, env.sid['codex'], post['id'], 'codex')
    with pytest.raises(Conflict, match='missing_probe'):
        br.assert_request_ready(env.board, post['id'], 'codex', env.sid['codex'])
    result = probe(env)
    assert result['authority'] == 'self_reported_probe_not_authorization'
    br.assert_request_ready(env.board, post['id'], 'codex', env.sid['codex'])
    assert br.eligible(env.board, env.sid['codex'], post['id'], 'codex')
    assert br.readiness(env.board, env.session('codex'), URL) == 'missing_probe'
    assert br.readiness(env.board, env.sid['codex'], URL + '/other') == 'missing_probe'


@pytest.mark.parametrize('field,value', [('http_status', 503), ('http_status', True),
    ('rendered_url', URL + '/other'), ('rendered_identity', ''),
    ('interaction', ''), ('interaction_result', '')])
def test_incomplete_or_wrong_target_evidence_rejected(env, field, value):
    with pytest.raises(Invalid):
        probe(env, evidence={**EVIDENCE, field: value})
    assert br.readiness(env.board, env.sid['codex'], URL) == 'missing_probe'


def test_owner_liveness_and_evidence_expiry_are_independent(env):
    probe(env)
    env.clock.advance(br.LIVE_SECONDS + 1)
    assert br.readiness(env.board, env.sid['codex'], URL) == 'owner_unavailable'
    env.board.heartbeat(env.p['codex'], env.sid['codex'])
    assert br.readiness(env.board, env.sid['codex'], URL) == 'ready'
    env.clock.advance(br.PROBE_TTL)
    env.board.heartbeat(env.p['codex'], env.sid['codex'])
    assert br.readiness(env.board, env.sid['codex'], URL) == 'stale_probe'


@pytest.mark.parametrize('column,value', [('worktree', '/other/tree'),
                                         ('client_session_id', 'other-conversation')])
def test_context_change_invalidates_probe(env, column, value):
    probe(env)
    env.board.conn.execute(f'UPDATE sessions SET {column}=? WHERE id=?', (value, env.sid['codex']))
    assert br.readiness(env.board, env.sid['codex'], URL) == 'context_changed'


def test_dispatched_worker_never_inherits_desktop_probe(env):
    probe(env)
    tid = env.thread()
    env.board.conn.execute('INSERT INTO board_state(key,value,updated_at) VALUES (?,?,?)',
        ('dispatch.run.browser-worker', json.dumps({'agent': 'codex', 'thread_id': tid,
         'status': 'running'}), env.clock()))
    sid = env.board.register_session(env.p['codex'], PROJECT,
                                     dispatch_run_id='browser-worker')['session_id']
    assert br.readiness(env.board, sid, URL) == 'missing_probe'
    with pytest.raises(Invalid, match='desktop'):
        probe(env, sid=sid)
    probe(env, sid=sid, context={**CTX, 'kind': 'headless', 'transport': 'supported-adapter'})
    assert br.readiness(env.board, sid, URL) == 'ready'


def test_disconnected_reconnect_is_bounded_and_same_context(env):
    probe(env)
    fail(env)
    with pytest.raises(Conflict):
        br.claim_reconnect(env.board, env.p['codex'], env.sid['codex'], URL,
                           {**CTX, 'connection_id': 'different-tab'})
    for attempt in (1, 2):
        result = br.claim_reconnect(env.board, env.p['codex'], env.sid['codex'], URL, CTX)
        assert result['attempt'] == attempt
        assert br.readiness(env.board, env.sid['codex'], URL) == 'disconnected'
        fail(env)  # Repeated adapter failures must not reset the retry budget.
    with pytest.raises(Conflict, match='limit'):
        br.claim_reconnect(env.board, env.p['codex'], env.sid['codex'], URL, CTX)
    probe(env)
    assert br.readiness(env.board, env.sid['codex'], URL) == 'ready'


@pytest.mark.parametrize('failure', ['unreachable', 'browser_missing', 'render_failed',
                                    'interaction_failed', 'policy_denied', 'host_permission'])
def test_failures_do_not_trigger_transport_fallback(env, failure):
    probe(env)
    fail(env, failure)
    with pytest.raises(Conflict, match='reconnect not allowed'):
        br.claim_reconnect(env.board, env.p['codex'], env.sid['codex'], URL, CTX)


def test_denial_survives_restart_new_session_and_identity(env):
    post = task(env)
    probe(env)
    fail(env, 'policy_denied')
    env.board.conn.close()
    env.board = Board(env.settings, clock=env.clock)
    for actor in ('codex', 'claude'):
        sid = env.session(actor)
        assert br.readiness(env.board, sid, URL) == 'policy_denied'
        assert not br.eligible(env.board, sid, post['id'], 'codex')
        with pytest.raises(Conflict, match='permission denied'):
            probe(env, actor=actor, sid=sid)
    assert 'human permission change' in br.request_blocker(env.board, post['id'], 'codex')
    with pytest.raises(Conflict, match='policy_denied'):
        br.assert_request_ready(env.board, post['id'], 'codex', env.sid['codex'])


def test_human_permission_change_requires_cas_and_a_new_probe(env):
    probe(env)
    fail(env, 'policy_denied')
    def change(actor, epoch):
        return br.record_permission_change(env.board, env.p[actor], env.sid[actor], PROJECT,
                                            URL, 'User changed supported host permission setting', epoch)
    with pytest.raises(Forbidden):
        change('codex', 1)
    with pytest.raises(Conflict):
        change('human', 0)
    assert change('human', 1)['permission_granted_by_board'] is False
    assert br.readiness(env.board, env.sid['codex'], URL) == 'fresh_probe_required'
    with pytest.raises(Conflict):
        change('human', 1)
    probe(env)
    assert br.readiness(env.board, env.sid['codex'], URL) == 'ready'


@pytest.mark.parametrize('failure,expected_other', [('disconnected', 'started'),
                                                    ('policy_denied', 'blocked')])
def test_running_failures_block_affected_requests(env, failure, expected_other):
    posts = {actor: task(env, actor) for actor in ('codex', 'claude')}
    for actor, post in posts.items():
        probe(env, actor)
        requests.progress(env.board, env.p[actor], env.sid[actor], post['id'], actor, 'started')
    fail(env, failure)
    for actor, expected in [('codex', 'blocked'), ('claude', expected_other)]:
        rows = env.board.get_post(env.p['human'], posts[actor]['id'])['requests']
        assert rows[0]['state'] == expected


def test_disconnect_in_changed_connection_cannot_reconnect_old_context(env):
    probe(env)
    changed = {**CTX, 'connection_id': 'replacement-tab'}
    try:
        br.report_failure(env.board, env.p['codex'], env.sid['codex'], URL,
                          changed, 'disconnected', 'Replacement connection disconnected')
    except Conflict:
        return  # Rejecting mismatched failure evidence is also safe.
    with pytest.raises(Conflict):
        br.claim_reconnect(env.board, env.p['codex'], env.sid['codex'], URL, CTX)
