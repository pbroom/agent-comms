"""Independent browser-target enforcement across the integrated owner lifecycle."""
import pytest

from agent_comms import browser_readiness as br, capabilities, requests, workstreams
from agent_comms.core import Conflict
from test_workstreams import stack, create, current, route  # noqa: F401

URL = 'http://localhost:5185/about'
CONTEXT = {'kind': 'desktop', 'transport': 'iab', 'connection_id': 'test-tab'}


def ready(stack, actor, url=URL):
    env = stack['env']
    sid = env.sid[actor]
    env.board.heartbeat(env.p[actor], sid)
    capabilities.register(env.board, env.p[actor], sid, ['git:write', 'browser:desktop'],
                          'Local adapter and git preflight', activity='idle')
    attempt = br.begin_probe(env.board, env.p[actor], sid, url, CONTEXT)
    br.report_probe(env.board, env.p[actor], sid, url, CONTEXT,
        {'http_status': 200, 'rendered_url': url, 'rendered_identity': 'Test About',
         'interaction': 'select About', 'interaction_result': 'About selected'}, attempt['attempt_id'])


def managed(stack):
    return create(stack, required_capabilities=['git:write', 'browser:desktop'])


def test_direct_assignment_requires_bound_browser_target_even_with_unrelated_proof(stack):
    env = stack['env']
    ready(stack, 'codex', 'http://localhost:5185/unrelated')
    post = env.board.create_post(env.p['human'], env.sid['human'], thread_id=stack['thread'],
                                 body='Browser audit', type='request', to=['codex'])
    with pytest.raises(Conflict, match='browser target'):
        requests.assign(env.board, env.p['human'], env.sid['human'], post['id'], 'codex',
                        env.sid['codex'], expected_version=0, reason='Browser preflight',
                        required_capabilities=['browser:desktop'])
    assert current(stack, post)['assigned_session'] is None


def test_persisted_browser_requirement_cannot_be_downgraded_by_git_route(stack):
    env = stack['env']
    post = managed(stack)
    env.clock.advance(121)
    for actor in ('codex', 'claude'):
        ready(stack, actor)
    # Existing helper deliberately supplies only git:write; persisted requirements win.
    routed = route(stack, post)
    assert routed['assigned_session'] == env.sid['codex']
    persisted = workstreams.get_for_post(env.board, post['id'])
    assert persisted['epoch'] == 0
    assert 'exact bound target' in persisted['blocker']
    assert workstreams.delivery(env.board, post['id'], 'claude') is None


@pytest.mark.parametrize('operation', ['start', 'claim'])
def test_unbound_managed_browser_work_cannot_start_or_claim(stack, operation):
    env = stack['env']
    post = managed(stack)
    ready(stack, 'codex')
    with pytest.raises(Conflict, match='browser'):
        if operation == 'start':
            requests.progress(env.board, env.p['codex'], env.sid['codex'], post['id'],
                              'codex', 'started', expected_version=current(stack, post)['version'])
        else:
            env.board.claim_task(env.p['codex'], env.sid['codex'], post['task_id'])
    assert current(stack, post)['state'] == 'queued'
    task = env.board.get_task(env.p['human'], post['task_id'])
    assert task['owner_session'] is None


def test_delivery_rechecks_bound_target_for_preexisting_transferred_work(stack):
    env = stack['env']
    post = managed(stack)
    br.bind_request(env.board, env.p['human'], env.sid['human'], post['id'], 'codex', URL)
    env.clock.advance(121)
    for actor in ('codex', 'claude'):
        ready(stack, actor)
    routed = route(stack, post)
    assert routed['assigned_session'] == env.sid['claude']
    assert workstreams.delivery(env.board, post['id'], 'claude') is not None
    # Model a persisted legacy/incomplete row encountered after restart. No hooks are mocked.
    env.board.conn.execute('DELETE FROM browser_requirements WHERE post_id=?', (post['id'],))
    assert workstreams.delivery(env.board, post['id'], 'claude') is None
