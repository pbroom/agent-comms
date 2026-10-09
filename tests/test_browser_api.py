"""Exercise the installed HTTP/MCP entrypoints, without browser or network I/O."""
import asyncio
import json

from fastapi.testclient import TestClient
from mcp import Client

from agent_comms.api import create_app
from agent_comms.mcp_server import build_mcp
from conftest import PROJECT

URL = 'http://localhost:5185/about'
CONTEXT = {'kind': 'desktop', 'transport': 'iab', 'connection_id': 'tab-1'}
PROBE = {'target_url': URL, 'context': CONTEXT, 'evidence': {
    'http_status': 200, 'rendered_url': URL, 'rendered_identity': 'NEXUS About LOCAL 1.43.17',
    'interaction': 'select About', 'interaction_result': 'About selected'}}


def headers(env, actor='codex'):
    return {'Authorization': f'Bearer {env.tokens[actor]}',
            'X-Board-Session': str(env.sid[actor])}


def test_http_browser_auth_and_session_impersonation(env):
    client = TestClient(create_app(env.board))
    assert client.post('/api/browser/probe', json={**PROBE, 'attempt_id': 'unauthorized'}).status_code == 401
    assert client.get('/api/browser/status', params={'target_url': URL}).status_code == 401
    wrong = {**headers(env), 'X-Board-Session': str(env.sid['claude'])}
    assert client.post('/api/browser/probe', headers=wrong, json={**PROBE, 'attempt_id': 'impersonated'}).status_code == 403
    assert client.get('/api/browser/status', headers=wrong,
                      params={'target_url': URL}).status_code == 403
    impersonated = {**PROBE, 'attempt_id': 'impersonated', 'session_id': env.sid['claude']}
    assert client.post('/api/browser/probe', headers=headers(env), json=impersonated).status_code == 403


def test_http_browser_denial_human_clear_and_fresh_probe(env):
    client = TestClient(create_app(env.board))
    def report(actor='codex'):
        begun = client.post('/api/browser/begin-probe', headers=headers(env, actor),
                            json={'target_url': URL, 'context': CONTEXT})
        if begun.status_code != 200:
            return begun
        return client.post('/api/browser/probe', headers=headers(env, actor),
                           json={**PROBE, 'attempt_id': begun.json()['attempt_id']})
    response = report()
    assert response.status_code == 200, response.text
    denied = client.post('/api/browser/failure', headers=headers(env), json={
        'target_url': URL, 'context': CONTEXT, 'failure': 'policy_denied',
        'evidence': 'Browser adapter explicitly denied navigation'})
    assert denied.status_code == 200, denied.text
    assert denied.json()['human_action_required'] is True
    assert report('claude').status_code == 409
    change = {'project': PROJECT, 'target_url': URL, 'expected_epoch': 1,
              'evidence': 'Human changed permission through supported host UI'}
    assert client.post('/api/browser/permission-change', headers=headers(env), json=change).status_code == 403
    stale = client.post('/api/browser/permission-change', headers=headers(env, 'human'),
                         json={**change, 'expected_epoch': 0})
    assert stale.status_code == 409
    cleared = client.post('/api/browser/permission-change', headers=headers(env, 'human'), json=change)
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()['permission_granted_by_board'] is False
    status = client.get('/api/browser/status', headers=headers(env), params={'target_url': URL})
    assert status.json()['readiness'] == 'fresh_probe_required'
    assert report().status_code == 200
    assert client.get('/api/browser/status', headers=headers(env),
                      params={'target_url': URL}).json()['readiness'] == 'ready'


def test_mcp_browser_tools_and_report_protocol(env, monkeypatch):
    monkeypatch.setenv('AGENT_COMMS_TOKEN', env.tokens['codex'])
    mcp = build_mcp(env.board, 'stdio')

    async def go():
        async with Client(mcp) as client:
            tools = {tool.name for tool in (await client.list_tools()).tools}
            assert {'board_bind_browser_request', 'board_browser_probe', 'board_browser_failure',
                    'board_browser_reconnect', 'board_browser_status', 'board_browser_begin_probe'} <= tools
            assert 'board_browser_permission_change' not in tools

            async def call(name, arguments):
                result = await client.call_tool(name, arguments)
                assert not result.is_error, result
                return json.loads(result.content[0].text)

            await call('board_register', {'project': PROJECT})
            async def report():
                attempt = await call('board_browser_begin_probe', {'target_url': URL, 'context': CONTEXT})
                return await call('board_browser_probe', {**PROBE, 'attempt_id': attempt['attempt_id']})

            ready = await report()
            assert ready['status'] == 'ready'
            assert ready['authority'] == 'self_reported_probe_not_authorization'
            assert (await call('board_browser_status', {'target_url': URL}))['readiness'] == 'ready'
            bad = await client.call_tool('board_browser_probe', {**PROBE, 'attempt_id': 'impersonated', 'session_id': env.sid['claude']})
            assert bad.is_error
            await call('board_browser_failure', {'target_url': URL, 'context': CONTEXT,
                                                 'failure': 'disconnected', 'evidence': 'Adapter disconnected'})
            reconnect = await call('board_browser_reconnect', {'target_url': URL, 'context': CONTEXT})
            assert reconnect['attempt'] == 1 and reconnect['fresh_probe_required']
            await report()
            await call('board_browser_failure', {'target_url': URL, 'context': CONTEXT,
                                                 'failure': 'policy_denied', 'evidence': 'Adapter denied navigation'})
            assert (await call('board_browser_status', {'target_url': URL}))['readiness'] == 'policy_denied'
            blocked = await client.call_tool('board_browser_reconnect', {'target_url': URL, 'context': CONTEXT})
            assert blocked.is_error

    asyncio.run(go())


def test_http_gate_list_and_path_refusal_report(env):
    client = TestClient(create_app(env.board))
    refused = client.post('/api/browser/failure', headers=headers(env), json={
        'target_url': URL, 'context': CONTEXT, 'failure': 'policy_denied',
        'evidence': 'File access denied: /repo/x.png is outside allowed roots. Allowed roots: /out, /out'})
    assert refused.status_code == 400 and 'local file path' in refused.text
    assert client.get('/api/browser/gates', headers=headers(env, 'human')).json() == {'gates': []}
    assert client.post('/api/browser/failure', headers=headers(env), json={
        'target_url': URL, 'context': CONTEXT, 'failure': 'policy_denied',
        'evidence': 'User declined the site'}).status_code == 200
    assert client.get('/api/browser/gates', headers=headers(env)).status_code == 403
    assert client.get('/api/browser/gates').status_code == 401
    [gate] = client.get('/api/browser/gates', headers=headers(env, 'human')).json()['gates']
    assert gate['origin'] == 'http://localhost:5185' and gate['recorded_by'] == 'codex'
    # Exactly what the dashboard's "Allow again" sends (no session id: the human's own session is used).
    human = {'Authorization': f'Bearer {env.tokens["human"]}'}
    cleared = client.post('/api/browser/permission-change', headers=human, json={
        'project': gate['project'], 'target_url': gate['origin'], 'evidence': 'Checked the host setting',
        'expected_epoch': gate['epoch']})
    assert cleared.status_code == 200, cleared.text
    assert cleared.json() == {'status': 'fresh_probe_required', 'permission_granted_by_board': False}
    assert client.get('/api/browser/gates', headers=headers(env, 'human')).json() == {'gates': []}
