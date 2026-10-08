"""Exact request lifecycle replies cross authenticated HTTP and MCP unchanged."""
import asyncio
import json

import pytest
from fastapi.testclient import TestClient
from mcp import Client

from agent_comms.api import create_app
from agent_comms.mcp_server import INSTRUCTIONS, build_mcp
from test_identity import EXPECTED_TOOLS
from test_issues_api import headers
from test_workstreams import stack, completion, create, probe


def request(env):
    return env.post('human',env.thread(),'Perform the approved check',type='request',to=['codex'],needs_response=True)


def payload(source, *, state='started',version=0,key='pickup-1',disposition=None):
    reply={'post_id':source['id'],'recipient':'codex','expected_version':version,
           'state':state,'reason':'Verified exact source lifecycle'}
    if disposition is not None:
        reply['disposition']=disposition
    return {'thread_id':source['thread_id'],'type':'status','body':'Evidence for the exact requested check',
            'request_reply':reply,'idempotency_key':key}


def current(env,source):
    return env.board.get_post(env.p['human'],source['id'])['requests'][0]


def post_count(env):
    return env.board.conn.execute('SELECT COUNT(*) FROM posts').fetchone()[0]


def test_http_ordinary_reply_never_acknowledges_then_exact_reply_is_idempotent(env):
    source=request(env);client=TestClient(create_app(env.board))
    plain=client.post('/api/posts',headers=headers(env),json={
        'thread_id':source['thread_id'],'type':'status','body':f"Request #{source['id']} is complete"})
    assert plain.status_code==200,plain.text
    assert current(env,source)['state']=='queued'
    work=payload(source)
    first=client.post('/api/posts',headers=headers(env),json=work)
    assert first.status_code==200,first.text
    result=first.json()
    assert result['agent']=='codex'
    assert result['request_reply']['post_id']==source['id']
    assert result['request_reply']['state']=='started'
    assert current(env,source)['assigned_session']==env.sid['codex']
    before=post_count(env)
    retry=client.post('/api/posts',headers=headers(env),json=work)
    assert retry.status_code==200 and retry.json()['id']==result['id']
    assert retry.json()['request_reply']==result['request_reply']
    assert post_count(env)==before
    changed=client.post('/api/posts',headers=headers(env),json=work|{'body':'Different work'})
    assert changed.status_code==409 and post_count(env)==before
    stale=client.post('/api/posts',headers=headers(env),json=work|{'idempotency_key':'different-attempt'})
    assert stale.status_code==409 and post_count(env)==before


def test_http_finish_has_explicit_disposition_and_atomic_evidence(env):
    source=request(env);client=TestClient(create_app(env.board))
    started=client.post('/api/posts',headers=headers(env),json=payload(source)).json()
    finish=payload(source,state='finished',version=started['request_reply']['version'],key='finish-1',disposition='completed')
    response=client.post('/api/posts',headers=headers(env),json=finish)
    assert response.status_code==200,response.text
    post=response.json()
    assert post['request_reply']['disposition']=='completed'
    row=current(env,source)
    assert row['state']=='finished' and post['id'] in row['evidence_post_ids']
    assert env.board.get_post(env.p['human'],post['id'])['body']==finish['body']


def test_http_authentication_source_identity_and_answer_to_remain_guarded(env):
    source=request(env);client=TestClient(create_app(env.board));before=post_count(env)
    assert client.post('/api/posts',json=payload(source)).status_code==401
    assert client.post('/api/posts',headers=headers(env,'claude'),json=payload(source)).status_code==403
    different_thread=env.thread()
    mismatch=client.post('/api/posts',headers=headers(env),json=payload(source)|{'thread_id':different_thread})
    assert mismatch.status_code in (400,403),mismatch.text
    human_link=client.post('/api/posts',headers=headers(env),json=payload(source)|{'answer_to':[source['id']]})
    assert human_link.status_code in (400,403),human_link.text
    assert post_count(env)==before and current(env,source)['state']=='queued'


@pytest.mark.parametrize('field,value',[('post_id',True),('post_id',0),('post_id',1.5),
    ('post_id','1'),('expected_version',False),('expected_version',-1),('expected_version','0'),
    ('state','queued'),('state','read'),('unexpected','unsafe')])
def test_http_reply_envelope_rejects_coercion_and_unknown_fields(env,field,value):
    source=request(env);client=TestClient(create_app(env.board));work=payload(source)
    work['request_reply'][field]=value
    response=client.post('/api/posts',headers=headers(env),json=work)
    assert response.status_code==422,response.text
    assert post_count(env)==1 and current(env,source)['state']=='queued'


@pytest.mark.parametrize('change',[lambda p:p.pop('idempotency_key'),lambda p:p.pop('request_reply'),
    lambda p:p['request_reply'].update(state='finished'),
    lambda p:p['request_reply'].update(disposition='completed')])
def test_http_pair_and_finish_contract_fail_without_partial_post(env,change):
    source=request(env);client=TestClient(create_app(env.board));work=payload(source);change(work)
    response=client.post('/api/posts',headers=headers(env),json=work)
    assert response.status_code==400,response.text
    assert post_count(env)==1 and current(env,source)['state']=='queued'


def test_mcp_existing_post_tool_carries_explicit_lifecycle_and_preserves_identity(env,monkeypatch):
    source=request(env)
    monkeypatch.setenv('AGENT_COMMS_TOKEN',env.tokens['codex'])
    async def go():
        async with Client(build_mcp(env.board,'stdio')) as client:
            tools={tool.name:tool for tool in (await client.list_tools()).tools}
            assert set(tools)==EXPECTED_TOOLS
            assert {'request_reply','idempotency_key','answer_to'} <= set(tools['board_post'].input_schema['properties'])
            assert 'ordinary replies' in tools['board_post'].description.lower()
            data=payload(source)|{'session_id':env.sid['codex']}
            first=await client.call_tool('board_post',data)
            assert not first.is_error,first
            post=json.loads(first.content[0].text)
            assert post['agent']=='codex' and post['request_reply']['post_id']==source['id']
            repeated=await client.call_tool('board_post',data)
            assert not repeated.is_error and json.loads(repeated.content[0].text)['id']==post['id']
            assert post_count(env)==2
            finish=payload(source,state='finished',version=post['request_reply']['version'],
                           key='mcp-finish',disposition='completed')|{'session_id':env.sid['codex']}
            done=await client.call_tool('board_post',finish)
            assert not done.is_error,done
            result=json.loads(done.content[0].text)
            assert result['request_reply']['disposition']=='completed'
            assert result['id'] in current(env,source)['evidence_post_ids']
    asyncio.run(go())


def test_mcp_strict_source_version_and_human_only_link_have_no_side_effects(env,monkeypatch):
    source=request(env);monkeypatch.setenv('AGENT_COMMS_TOKEN',env.tokens['codex'])
    async def go():
        async with Client(build_mcp(env.board,'stdio')) as client:
            for field,value in [('post_id',True),('expected_version','0'),('expected_version',-1)]:
                data=payload(source)|{'session_id':env.sid['codex']};data['request_reply'][field]=value
                result=await client.call_tool('board_post',data)
                assert result.is_error,result
            forbidden=await client.call_tool('board_post',payload(source)|{
                'session_id':env.sid['codex'],'answer_to':[source['id']]})
            assert forbidden.is_error,forbidden
    asyncio.run(go())
    assert post_count(env)==1 and current(env,source)['state']=='queued'


def test_runtime_protocol_distinguishes_lifecycle_from_cursor_and_text():
    assert 'ordinary replies never acknowledge or complete' in INSTRUCTIONS
    assert "state='started'" in INSTRUCTIONS and "state='blocked'" in INSTRUCTIONS
    assert "disposition='completed'" in INSTRUCTIONS and "disposition='superseded'" in INSTRUCTIONS
    assert 'idempotency_key' in INSTRUCTIONS and 'answer_to remains\nhuman-only' in INSTRUCTIONS


def test_http_superseded_reply_closes_only_exact_recipient_without_finishing_task(env):
    tid=env.thread();task=env.accepted_task(tid)
    env.board.claim_task(env.p['codex'],env.sid['codex'],task)
    source=env.post('human',tid,'Obsolete acknowledgement only',type='request',
                    to=['codex','claude'],needs_response=True,task_id=task)
    client=TestClient(create_app(env.board))
    response=client.post('/api/posts',headers=headers(env),json=payload(
        source,state='finished',key='obsolete-1',disposition='superseded'))
    assert response.status_code==200,response.text
    post=response.json()
    assert post['request_reply']['disposition']=='superseded'
    rows=env.board.get_post(env.p['human'],source['id'])['requests']
    assert {r['recipient']:r['state'] for r in rows}=={'codex':'finished','claude':'queued'}
    assert env.board.get_task(env.p['human'],task)['status']=='working'


def test_http_managed_completion_payload_keeps_existing_evidence_gates(stack):
    env=stack['env'];source=create(stack);probe(stack,'codex',activity='active')
    client=TestClient(create_app(env.board))
    started=client.post('/api/posts',headers=headers(env),json=payload(
        source,version=current(env,source)['version']))
    assert started.status_code==200,started.text
    env.board.claim_task(env.p['codex'],env.sid['codex'],source['task_id'])
    data=payload(source,state='finished',version=started.json()['request_reply']['version'],
                 key='managed-finish',disposition='completed')
    before=post_count(env)
    missing=client.post('/api/posts',headers=headers(env),json=data)
    assert missing.status_code in (400,409),missing.text
    assert post_count(env)==before and current(env,source)['state']=='started'
    data['request_reply']['completion']=completion(stack)
    response=client.post('/api/posts',headers=headers(env),json=data)
    assert response.status_code==200,response.text
    assert response.json()['request_reply']['state']=='finished'
    assert env.board.get_task(env.p['human'],source['task_id'])['status']=='done'
