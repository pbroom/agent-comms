"""Browser proof must be enforced by actual routing, lifecycle and dispatcher."""
import pytest

from agent_comms import browser_readiness as br, capabilities, db, requests
from agent_comms.core import Conflict
from conftest import PROJECT
from test_dispatch import denv, allow, human_post  # noqa: F401 - shared fake dispatcher fixture

URL = 'http://localhost:5185/about'
CTX = {'kind': 'desktop', 'transport': 'iab', 'connection_id': 'tab-1'}


def schema(env):
    env.board.conn.executescript(br.SCHEMA)


def task(env, recipients=None):
    schema(env)
    post = env.post('human', env.thread(), type='request', to=recipients or ['codex'])
    br.bind_request(env.board, env.p['human'], env.sid['human'], post['id'], 'codex', URL)
    return post


def proof(env, actor='codex', url=URL):
    sid = env.sid[actor]
    attempt = br.begin_probe(env.board, env.p[actor], sid, url, CTX)
    return br.report_probe(env.board, env.p[actor], sid, url, CTX,
        {'http_status': 200, 'rendered_url': url, 'rendered_identity': 'About LOCAL 1.43.17',
         'interaction': 'select About', 'interaction_result': 'About selected'}, attempt['attempt_id'])


def capability(env, actor='codex'):
    return capabilities.register(env.board, env.p[actor], env.sid[actor], ['browser:desktop'],
                                 'Browser adapter available in this context')


def route(env, post):
    row = next(r for r in env.board.get_post(env.p['human'], post['id'])['requests']
               if r['recipient'] == 'codex')
    return capabilities.route(env.board, env.p['human'], env.sid['human'], post['id'],
                              'codex', ['browser:desktop'], row['version'])


def start(env, post):
    return requests.progress(env.board, env.p['codex'], env.sid['codex'],
                              post['id'], 'codex', 'started')


def test_browser_bound_request_cannot_start_without_complete_probe(env):
    post = task(env)
    with pytest.raises(Conflict, match='browser'):
        start(env, post)
    attempt = br.begin_probe(env.board, env.p['codex'], env.sid['codex'], URL, CTX)
    assert attempt['attempt_id']
    with pytest.raises(Conflict, match='browser'):
        start(env, post)


def test_generic_browser_attestation_cannot_route_unproven_context(env):
    post = task(env)
    capability(env)
    assert not capabilities.eligible(env.board, env.sid['codex'], PROJECT, ['browser:desktop'])
    blocked = route(env, post)
    assert blocked['state'] == 'blocked'
    assert blocked['assigned_session'] is None


def test_exact_target_proof_allows_assignment_and_start(env):
    post = task(env)
    capability(env)
    proof(env, url='http://localhost:5185/settings')
    assert route(env, post)['state'] == 'blocked'
    proof(env)
    assigned = route(env, post)
    assert assigned['assigned_session'] == env.sid['codex']
    assert start(env, post)['state'] == 'started'


@pytest.mark.parametrize('change', ['stale', 'context'])
def test_start_rechecks_proof_after_assignment(env, change):
    post = task(env)
    capability(env)
    proof(env)
    assert route(env, post)['assigned_session'] == env.sid['codex']
    if change == 'stale':
        env.clock.advance(br.PROBE_TTL + 1)
        env.board.heartbeat(env.p['codex'], env.sid['codex'])
    else:
        with db.write_tx(env.board.conn):
            env.board.conn.execute('UPDATE sessions SET client_session_id=? WHERE id=?',
                                   ('replacement-conversation', env.sid['codex']))
    with pytest.raises(Conflict, match='browser'):
        start(env, post)


def test_denied_origin_never_routes_to_other_original_identity_and_clear_requires_probe(env):
    post = task(env, ['codex', 'claude'])
    for actor in ('codex', 'claude'):
        capability(env, actor)
        proof(env, actor)
    br.report_failure(env.board, env.p['codex'], env.sid['codex'], URL, CTX,
                       'policy_denied', 'Explicit browser policy denial')
    with pytest.raises(Conflict, match='denied'):
        route(env, post)
    blocked = env.board.get_post(env.p['human'],post['id'])['requests'][0]
    assert blocked['state'] == 'blocked'
    assert blocked['assigned_agent'] == 'codex'
    assert blocked['assigned_session'] is None
    br.record_permission_change(env.board, env.p['human'], env.sid['human'], PROJECT,
                                URL, 'Human changed permission in supported host UI', 1)
    assert route(env, post)['state'] == 'blocked'
    proof(env)
    assert route(env, post)['assigned_session'] == env.sid['codex']
    assert start(env, post)['state'] == 'started'


def test_dispatch_does_not_launch_generic_cli_for_bound_browser_request(denv):
    schema(denv)
    allow(denv, agents=('codex',))
    post = human_post(denv, ['codex'], body='Inspect the rendered About page')
    br.bind_request(denv.board, denv.p['human'], denv.sid['human'], post['id'], 'codex', URL)
    denv.d.tick()
    assert denv.spawner.calls == []
    rows = denv.board.get_post(denv.p['human'], post['id'])['requests']
    assert rows[0]['state'] != 'started'


def test_browser_routing_requires_bound_target_even_with_other_ready_probe(env):
    schema(env)
    post = env.post('human', env.thread(), type='request', to=['codex'])
    capability(env)
    proof(env)
    with pytest.raises(Conflict, match='target|bind|browser'):
        route(env,post)
