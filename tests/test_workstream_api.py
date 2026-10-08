"""Managed continuation contracts across authenticated HTTP and MCP surfaces."""
import asyncio
import json

from fastapi.testclient import TestClient
from mcp import Client

from agent_comms import db
from agent_comms.api import create_app
from agent_comms.mcp_server import build_mcp
from test_issues_api import headers
from test_workstreams import stack, completion


def test_http_continuation_completes_with_exact_evidence(stack):
    env = stack['env']
    client = TestClient(create_app(env.board))
    payload = dict(body='Propagate approved fix',type='handoff',thread_id=stack['thread'],continuation=stack['spec'])
    assert client.post('/api/posts',json=payload).status_code == 401
    created = client.post('/api/posts',headers=headers(env),json=payload)
    assert created.status_code == 200, created.text
    post = created.json()
    assert len(post['requests']) == 1
    assert client.post('/api/posts',headers=headers(env),json=payload).json()['id'] == post['id']
    probes = client.post('/api/sessions/capabilities',headers=headers(env),json={
        'capabilities':['git:write'],'evidence':'Verified local git access','activity':'active'})
    assert probes.status_code == 200, probes.text
    path = f"/api/posts/{post['id']}/request-progress"
    started = client.post(path,headers=headers(env),json=dict(recipient='codex',state='started',expected_version=1))
    assert started.status_code == 200, started.text
    env.board.claim_task(env.p['codex'],env.sid['codex'],post['task_id'])
    proof = env.board.create_post(env.p['codex'],env.sid['codex'],thread_id=stack['thread'],
                                  type='status',body='Checks passed at the exact descendant head')
    finished = client.post(path,headers=headers(env),json=dict(recipient='codex',state='finished',reason='Verified',
        expected_version=started.json()['version'],evidence_post_ids=[proof['id']],completion=completion(stack)))
    assert finished.status_code == 200, finished.text
    assert finished.json()['state'] == 'finished'
    assert env.board.get_task(env.p['codex'],post['task_id'])['status'] == 'done'


def test_mcp_continuation_fields_are_callable_without_new_tool_names(stack,monkeypatch):
    env=stack['env']
    monkeypatch.setenv('AGENT_COMMS_TOKEN',env.tokens['codex'])
    async def go():
        async with Client(build_mcp(env.board,'stdio')) as client:
            toolset={t.name:t for t in (await client.list_tools()).tools}
            assert 'continuation' in toolset['board_post'].input_schema['properties']
            assert 'activity' in toolset['board_register_capabilities'].input_schema['properties']
            assert 'completion' in toolset['board_request_progress'].input_schema['properties']
            result=await client.call_tool('board_post',dict(body='Propagate scoped fix',type='handoff',
                thread_id=stack['thread'],session_id=env.sid['codex'],continuation=stack['spec']))
            assert not result.is_error,result
            post=json.loads(result.content[0].text)
            assert post['continuation']['owner_session']==env.sid['codex']
    asyncio.run(go())


def test_v8_upgrade_preserves_existing_requests_sessions_and_grants(stack):
    env=stack['env']
    conn=env.board.conn
    before={name:[tuple(r) for r in conn.execute('SELECT * FROM '+name)]
            for name in ('posts','request_progress','sessions','authorization_grants')}
    for row in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'managed_%'").fetchall():
        conn.execute('DROP TRIGGER '+row[0])
    conn.execute('DROP TABLE continuations')
    conn.execute('DROP TABLE session_activity')
    conn.execute('DROP TABLE managed_write_permit')
    conn.execute('ALTER TABLE tasks DROP COLUMN continuation_scope')
    conn.execute('PRAGMA user_version=8')
    db.init_schema(conn)
    db.init_schema(conn)
    assert conn.execute('PRAGMA user_version').fetchone()[0]==db.SCHEMA_VERSION
    for name,rows in before.items():
        assert [tuple(r) for r in conn.execute('SELECT * FROM '+name)]==rows
    assert conn.execute('SELECT COUNT(*) FROM continuations').fetchone()[0]==0


def test_direct_assignment_cannot_replace_target_binding_with_generic_probe(env):
    import pytest
    from agent_comms import requests
    from agent_comms.core import Conflict
    from test_browser_routing import capability, proof
    post=env.post('human',env.thread(),type='request',to=['codex'])
    capability(env)
    proof(env)
    with pytest.raises(Conflict,match='target'):
        requests.assign(env.board,env.p['human'],env.sid['human'],post['id'],'codex',env.sid['codex'],
                        0,'verified browser',['browser:desktop'])


def test_managed_browser_contract_cannot_be_downgraded_by_caller(stack):
    import pytest
    from agent_comms import capabilities, workstreams
    from agent_comms.core import Conflict
    from test_workstreams import create,current,probe
    from test_browser_routing import proof
    env=stack['env']
    post=create(stack,required_capabilities=['browser:desktop'])
    env.clock.advance(121)
    for name in ('codex','claude'):
        proof(env,name)
        capabilities.register(env.board,env.p[name],env.sid[name],['browser:desktop','git:write'],
                              'Probe in executing browser context',activity='idle')
    routed=capabilities.route(env.board,env.p['human'],env.sid['human'],post['id'],'codex',
                              ['git:write'],current(stack,post)['version'])
    assert routed['assigned_session']==env.sid['codex']
    assert 'bound target' in routed['continuation']['blocker']
    assert workstreams.delivery(env.board,post['id'],'claude') is None
    with pytest.raises(Conflict,match='bound target'):
        env.board.claim_task(env.p['codex'],env.sid['codex'],post['task_id'])
