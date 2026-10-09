"""Browser readiness uses local evidence only; these tests never open a browser."""
import json

import pytest

from agent_comms import browser_readiness as br, db, requests
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
    attempt = br.begin_probe(env.board, env.p[actor], sid or env.sid[actor], URL, context or CTX)
    return br.report_probe(env.board, env.p[actor], sid or env.sid[actor], URL,
                           context or CTX, EVIDENCE if evidence is None else evidence, attempt['attempt_id'])


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
    with db.write_tx(env.board.conn):
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


def test_post_permission_change_disconnect_can_reconnect_without_old_epoch(env):
    probe(env)
    fail(env, 'policy_denied')
    br.record_permission_change(env.board, env.p['human'], env.sid['human'], PROJECT,
                                URL, 'Supported host permission changed', 1)
    fail(env, 'disconnected')
    assert br.readiness(env.board, env.sid['codex'], URL) == 'disconnected'
    result = br.claim_reconnect(env.board, env.p['codex'], env.sid['codex'], URL, CTX)
    assert result['fresh_probe_required'] is True
    assert br.readiness(env.board, env.sid['codex'], URL) == 'disconnected'
    probe(env)
    assert br.readiness(env.board, env.sid['codex'], URL) == 'ready'


@pytest.mark.parametrize('invalidation', ['disconnect', 'permission_epoch', 'expired', 'replayed', 'superseded'])
def test_delayed_or_replayed_probe_cannot_restore_readiness(env, invalidation):
    sid = env.sid['codex']
    attempt = br.begin_probe(env.board, env.p['codex'], sid, URL, CTX)['attempt_id']
    if invalidation == 'disconnect':
        fail(env)
    elif invalidation == 'permission_epoch':
        fail(env, 'policy_denied')
        br.record_permission_change(env.board, env.p['human'], env.sid['human'], PROJECT,
                                    URL, 'Supported host permission changed', 1)
    elif invalidation == 'expired':
        env.clock.advance(br.PROBE_TTL)
        env.board.heartbeat(env.p['codex'], sid)
    elif invalidation == 'replayed':
        br.report_probe(env.board, env.p['codex'], sid, URL, CTX, EVIDENCE, attempt)
    else:
        br.begin_probe(env.board, env.p['codex'], sid, URL, CTX)
    with pytest.raises(Conflict, match='attempt'):
        br.report_probe(env.board, env.p['codex'], sid, URL, CTX, EVIDENCE, attempt)


def probe_url(env, url):
    attempt = br.begin_probe(env.board, env.p['codex'], env.sid['codex'], url, CTX)
    return br.report_probe(env.board, env.p['codex'], env.sid['codex'], url, CTX,
                           {**EVIDENCE, 'rendered_url': url}, attempt['attempt_id'])


@pytest.mark.parametrize('failure', ['disconnected', 'browser_missing', 'host_permission'])
def test_connection_failure_invalidates_other_urls_and_inflight_probes(env, failure):
    other = 'http://localhost:5185/settings'
    pending_url = 'http://localhost:5185/profile'
    probe(env)
    probe_url(env, other)
    attempt = br.begin_probe(env.board, env.p['codex'], env.sid['codex'], pending_url, CTX)
    assert br.has_ready_probe(env.board, env.sid['codex'])
    fail(env, failure)
    assert br.readiness(env.board, env.sid['codex'], other) != 'ready'
    assert not br.has_ready_probe(env.board, env.sid['codex'])
    with pytest.raises(Conflict):
        br.report_probe(env.board, env.p['codex'], env.sid['codex'], pending_url, CTX,
                        {**EVIDENCE, 'rendered_url': pending_url}, attempt['attempt_id'])


def test_reconnect_budget_shared_between_targets_and_success_does_not_restore_others(env):
    other = 'http://localhost:5185/settings'
    probe(env)
    probe_url(env, other)
    fail(env)
    for expected, url in enumerate((URL, other), 1):
        attempt = br.claim_reconnect(env.board, env.p['codex'], env.sid['codex'], url, CTX)
        assert attempt['attempt'] == expected
    for url in (URL, other):
        with pytest.raises(Conflict, match='limit'):
            br.claim_reconnect(env.board, env.p['codex'], env.sid['codex'], url, CTX)
    probe_url(env, other)
    assert br.readiness(env.board, env.sid['codex'], other) == 'ready'
    assert br.readiness(env.board, env.sid['codex'], URL) == 'disconnected'
    fail(env)
    assert br.claim_reconnect(env.board, env.p['codex'], env.sid['codex'], URL, CTX)['attempt'] == 1


def test_reconnect_invalidates_other_target_inflight_attempt(env):
    other = 'http://localhost:5185/settings'
    probe(env)
    fail(env)
    attempt = br.begin_probe(env.board, env.p['codex'], env.sid['codex'], other, CTX)
    br.claim_reconnect(env.board, env.p['codex'], env.sid['codex'], URL, CTX)
    with pytest.raises(Conflict, match='attempt'):
        br.report_probe(env.board, env.p['codex'], env.sid['codex'], other, CTX,
                        {**EVIDENCE, 'rendered_url': other}, attempt['attempt_id'])


@pytest.mark.parametrize('spelling,origin', [
    ('https://Example.COM/x', 'https://example.com:443'),
    ('https://example.com./x', 'https://example.com:443'),
    ('https://example.com:443/x', 'https://example.com:443'),
    ('https://ex%61mple.com/x', 'https://example.com:443'),
    ('https://ｅxample.com/x', 'https://example.com:443'),          # full-width e
    ('https://example。com/x', 'https://example.com:443'),          # ideographic full stop
    ('https://Bücher.de/x', 'https://xn--bcher-kva.de:443'),
    ('https://xn--bcher-kva.de/x', 'https://xn--bcher-kva.de:443'),
    ('http://127.1/x', 'http://127.0.0.1:80'),
    ('http://0x7f.0.0.1/x', 'http://127.0.0.1:80'),
    ('http://0177.0.0.1/x', 'http://127.0.0.1:80'),
    ('http://2130706433/x', 'http://127.0.0.1:80'),
    ('http://127.0.0.1./x', 'http://127.0.0.1:80'),
    ('http://[0:0:0:0:0:0:0:1]/x', 'http://[::1]:80'),
    ('http://[::FFFF:127.0.0.1]:80/x', 'http://[::ffff:7f00:1]:80'),
    # UTS #46 non-transitional, as browsers resolve them (Python's built-in IDNA 2003 codec maps ß to ss).
    ('https://faß.de/x', 'https://xn--fa-hia.de:443'),
    ('https://FAß.de/x', 'https://xn--fa-hia.de:443'),
    ('https://ς.gr/x', 'https://xn--3xa.gr:443'),
    ('https://a_b.example/x', 'https://a_b.example:443'),
    ('http://example.com:0/x', 'http://example.com:0'),                # port 0 is not the default port
])
def test_target_canonicalizes_host_spellings(spelling, origin):
    assert br.target(spelling)[1] == origin


@pytest.mark.parametrize('bad', ['http://256.0.0.1/', 'http://1.2.3.4.5/', 'http://0x1g.0.0.1/', 'http://a..b/',
                                 'http://[::1%25en0]/', 'http://ex%2fample.com/', 'http://%00x/', 'http://4294967296/',
                                 'http://1.2.3.09/', 'http://example.09/', 'http://a‍b.com/',
                                 'http://*/', 'https://*.example.com/', 'https://{a,b}.example/', 'http://a,b/',
                                 'http://a\x7fb/', 'http://-a.example/'])
def test_target_refuses_hosts_a_browser_would_refuse(bad):
    with pytest.raises(Invalid):
        br.target(bad)


def test_ss_and_sharp_s_hosts_keep_separate_gates(env):
    br.report_failure(env.board, env.p['codex'], env.sid['codex'], 'https://faß.de/', CTX, 'policy_denied', 'denied')
    assert br.readiness(env.board, env.sid['codex'], 'https://xn--fa-hia.de/') == 'policy_denied'
    assert br.readiness(env.board, env.sid['codex'], 'https://fass.de/') != 'policy_denied'
    assert br.target('http://example.com:0/')[0] == 'http://example.com:0/'


def test_unstarted_browser_work_cannot_be_finished_without_a_ready_probe(env):
    post = task(env)
    proof = env.post('codex', post['thread_id'], 'I checked the page myself')
    with pytest.raises(Conflict, match='browser preflight blocked'):
        requests.progress(env.board, env.p['codex'], env.sid['codex'], post['id'], 'codex', 'finished',
                          reason='Done', evidence_post_ids=[proof['id']])
    probe(env)
    out = requests.progress(env.board, env.p['codex'], env.sid['codex'], post['id'], 'codex', 'finished',
                            reason='Done', evidence_post_ids=[proof['id']])
    assert out['state'] == 'finished'


def test_deny_gate_cannot_be_bypassed_by_respelling_the_host(env):
    deny = 'https://example.com/app'
    br.report_failure(env.board, env.p['codex'], env.sid['codex'], deny, CTX, 'policy_denied', 'denied by host')
    for spelling in ('https://EXAMPLE.com./app', 'https://ｅxample.com/app', 'https://example.com:443/app'):
        assert br.readiness(env.board, env.sid['codex'], spelling) == 'policy_denied'
        with pytest.raises(Conflict, match='permission denied'):
            br.begin_probe(env.board, env.p['codex'], env.sid['codex'], spelling, CTX)


def test_upgrade_rekeys_gates_and_requirements_stored_under_other_spellings(env):
    post = env.post('human', env.thread(), type='request', to=['codex'])
    c = env.board.conn
    with db.write_tx(c):
        c.execute('ALTER TABLE browser_permission_gates DROP COLUMN created_by')   # an older database
        c.execute("INSERT INTO browser_permission_gates VALUES (?,?,1,3,'old deny')", (PROJECT, 'http://localhost.:5185'))
        c.execute('INSERT INTO browser_requirements VALUES (?,?,?,?)',
                  (post['id'], 'codex', 'http://LOCALHOST.:5185/about', 'http://localhost.:5185'))
    db.init_schema(c)
    assert [tuple(r) for r in c.execute('SELECT origin, denied, epoch FROM browser_permission_gates')] == \
        [('http://localhost:5185', 1, 3)]
    assert br.requirement(env.board, post['id'], 'codex')['origin'] == 'http://localhost:5185'
    assert br.readiness(env.board, env.sid['codex'], URL) == 'policy_denied'
    assert 'human permission change' in br.request_blocker(env.board, post['id'], 'codex')


def test_denial_gates_are_capped_per_agent_and_project(env, monkeypatch):
    from agent_comms.core import LimitExceeded
    monkeypatch.setattr(br, 'MAX_GATES_PER_AGENT_PROJECT', 3)
    for i in range(3):
        br.report_failure(env.board, env.p['codex'], env.sid['codex'], f'https://h{i}.example/', CTX,
                          'policy_denied', 'denied')
    with pytest.raises(LimitExceeded, match='permission gates'):
        br.report_failure(env.board, env.p['codex'], env.sid['codex'], 'https://h9.example/', CTX,
                          'policy_denied', 'denied')
    # An existing gate can still be re-denied, and another agent has its own quota.
    br.report_failure(env.board, env.p['codex'], env.sid['codex'], 'https://h0.example/', CTX, 'host_permission', 'again')
    br.report_failure(env.board, env.p['claude'], env.sid['claude'], 'https://h9.example/', CTX, 'policy_denied', 'denied')
    assert env.board.conn.execute('SELECT COUNT(*) FROM browser_permission_gates').fetchone()[0] == 4


def test_probe_attempts_are_keyed_on_origin_and_path(env):
    for i in range(20):
        br.begin_probe(env.board, env.p['codex'], env.sid['codex'], f'{URL}?v={i}#f{i}', CTX)
    assert env.board.conn.execute('SELECT COUNT(*) FROM browser_probe_attempts').fetchone()[0] == 1
    attempt = br.begin_probe(env.board, env.p['codex'], env.sid['codex'], URL + '?v=1', CTX)
    with pytest.raises(Conflict, match='attempt'):   # the earlier attempt for this path was superseded
        br.report_probe(env.board, env.p['codex'], env.sid['codex'], URL + '?v=2', CTX,
                        {**EVIDENCE, 'rendered_url': URL + '?v=2'}, 'stale-id')
    out = br.report_probe(env.board, env.p['codex'], env.sid['codex'], URL + '?v=1', CTX,
                          {**EVIDENCE, 'rendered_url': URL + '?v=1'}, attempt['attempt_id'])
    assert out['status'] == 'ready'


def test_events_probes_and_attempts_are_pruned_after_retention(env):
    probe(env)
    fail(env, 'unreachable')
    br.begin_probe(env.board, env.p['codex'], env.sid['codex'], 'http://localhost:5185/other', CTX)
    c = env.board.conn
    assert c.execute('SELECT COUNT(*) FROM browser_events').fetchone()[0] >= 2
    env.clock.advance(br.EVENT_RETENTION_SECONDS + 1)
    br.begin_probe(env.board, env.p['codex'], env.sid['codex'], URL, CTX)   # any new attempt prunes
    assert c.execute('SELECT COUNT(*) FROM browser_events').fetchone()[0] == 0
    assert c.execute('SELECT COUNT(*) FROM browser_probes').fetchone()[0] == 0
    assert [r[0] for r in c.execute('SELECT target_url FROM browser_probe_attempts')] == [URL]
