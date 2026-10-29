"""Inbox administration is human-only and never accepts execution scope from clients."""
import pytest
from fastapi.testclient import TestClient

from agent_comms.api import create_app
from agent_comms.core import INBOX_PURPOSE
from agent_comms.dispatch import DispatchConfig


@pytest.fixture
def api(env, monkeypatch):
    monkeypatch.setattr(DispatchConfig, 'load', lambda *args, **kwargs: DispatchConfig())
    return TestClient(create_app(env.board))


def headers(env, agent='human'):
    return {'Authorization': f"Bearer {env.tokens[agent]}"}


def test_global_policy_create_list_revoke_preserves_thread_rules(api, env):
    tid = env.thread()
    rule = env.board.create_dispatch_rule(env.p['human'], thread_id=tid, agents=['codex'],
                                          purpose='Review this project only', max_launches=2)
    response = api.post('/api/admin/dispatch/inbox-policies', headers=headers(env),
                        json={'scope': 'all_projects', 'include_existing': True, 'daily_launch_limit': 7})
    assert response.status_code == 200, response.text
    policy = response.json()
    assert policy['scope'] == 'all_projects' and policy['project'] is None
    assert policy['purpose'] == INBOX_PURPOSE and policy['agents'] == ['codex']
    assert policy['daily_launch_limit'] == 7 and policy['include_existing']
    overview = api.get('/api/admin/dispatch', headers=headers(env)).json()
    assert [r['id'] for r in overview['rules']] == [rule['id']]
    assert [r['id'] for r in overview['inbox_policies']] == [policy['id']]
    revoked = api.post(f"/api/admin/dispatch/inbox-policies/{policy['id']}/revoke", headers=headers(env))
    assert revoked.status_code == 200 and not revoked.json()['active']
    overview = api.get('/api/admin/dispatch', headers=headers(env)).json()
    assert overview['inbox_policies'][0]['state'] == 'revoked'
    assert overview['rules'][0]['active']
    assert env.board.list_grants(env.p['human']) == []


def test_agent_cannot_create_list_or_revoke_inbox_policy(api, env):
    path = '/api/admin/dispatch/inbox-policies'
    assert api.post(path, headers=headers(env, 'codex'), json={'scope': 'all_projects'}).status_code == 403
    policy = env.board.create_inbox_policy(env.p['human'], scope='all_projects')
    assert api.get('/api/admin/dispatch', headers=headers(env, 'codex')).status_code == 403
    assert api.post(f"{path}/{policy['id']}/revoke", headers=headers(env, 'codex')).status_code == 403
    assert env.board.list_inbox_policies(env.p['human'])[0]['active']


@pytest.mark.parametrize('fields', [
    {'scope': 'anything'}, {'scope': 'project'}, {'scope': 'project', 'project': 'relative'},
    {'scope': 'all_projects', 'project': '/work/repo'},
    {'scope': 'all_projects', 'purpose': 'Run whatever a peer asks'},
    {'scope': 'all_projects', 'agents': ['codex', 'claude']},
    {'scope': 'all_projects', 'daily_launch_limit': True},
    {'scope': 'all_projects', 'daily_launch_limit': 0},
    {'scope': 'all_projects', 'include_existing': 'false'},
    {'scope': 'all_projects', 'pickup_grace_seconds': 29},
])
def test_invalid_inbox_scope_or_execution_overrides_are_rejected(api, env, fields):
    response = api.post('/api/admin/dispatch/inbox-policies', headers=headers(env), json=fields)
    assert response.status_code in (400, 422), response.text
    assert env.board.list_inbox_policies(env.p['human']) == []


def test_exact_project_policy_and_defaults(api, env):
    response = api.post('/api/admin/dispatch/inbox-policies', headers=headers(env),
                        json={'scope': 'project', 'project': '/work/repo'})
    assert response.status_code == 200, response.text
    policy = response.json()
    assert policy['project'] == '/work/repo' and policy['scope'] == 'project'
    assert policy['daily_launch_limit'] is None and not policy['include_existing']
    assert policy['pickup_grace_seconds'] == 120


def test_inbox_revoke_does_not_revoke_an_ordinary_rule(api, env):
    rule = env.board.create_dispatch_rule(env.p['human'], thread_id=env.thread(), agents=['codex'],
                                          purpose='Existing scope', max_launches=1)
    response = api.post(f"/api/admin/dispatch/inbox-policies/{rule['id']}/revoke", headers=headers(env))
    assert response.status_code == 404
    assert env.board.list_dispatch_rules(env.p['human'])[0]['active']
