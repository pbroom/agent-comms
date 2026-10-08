"""Explicit terminal recovery never grants execution ownership."""
import json

import pytest

from agent_comms import requests, workstreams
from agent_comms.core import Conflict, Forbidden, Invalid
from conftest import PROJECT


@pytest.fixture
def blocked(env):
    thread = env.thread()
    post = env.post('human',thread,type='request',to=['codex'])
    run = dict(agent='codex',thread_id=thread,status='running',run_id='recovery-run')
    def record():
        env.board.conn.execute('INSERT OR REPLACE INTO board_state(key,value,updated_at) VALUES (?,?,?)',
            ('dispatch.run.recovery-run',json.dumps(run),env.clock()))
    record()
    old = env.board.register_session(env.p['codex'],PROJECT,dispatch_run_id='recovery-run')['session_id']
    requests.progress(env.board,env.p['codex'],old,post['id'],'codex','started')
    requests.progress(env.board,env.p['codex'],old,post['id'],'codex','blocked',reason='Runner could not continue')
    env.clock.advance(1)
    run.update(status='exited',ended_at=env.clock())
    record()
    env.clock.advance(1)
    evidence = env.post('codex',thread,body='I verified completion against the actual resulting heads and checks')
    return dict(post=post,old=old,run=run,record=record,evidence=evidence)


def recover(env, b, **changes):
    values = dict(reason='Verified completed restack',evidence_post_ids=[b['evidence']['id']],
                  expected_version=2,recover_blocked=True)
    values.update(changes)
    return requests.progress(env.board,env.p[values.pop('actor','codex')],values.pop('sid',env.sid['codex']),
        b['post']['id'],'codex',values.pop('state','finished'),**values)


def test_recovery_finishes_with_audit_but_preserves_execution_assignment(env,blocked):
    result = recover(env,blocked)
    assert result['state']=='finished'
    assert result['assigned_session']==blocked['old']
    assert result['reason'].startswith('Terminal recovery from ended session ')
    event = requests.history(env.board,env.p['codex'],blocked['post']['id'],'codex')[-1]
    assert event['session_id']==env.sid['codex']
    assert event['assigned_session']==blocked['old']
    assert event['evidence_post_ids']==[blocked['evidence']['id']]


def test_explicit_flag_required(env,blocked):
    with pytest.raises(Conflict,match='another session'):
        recover(env,blocked,recover_blocked=False)


@pytest.mark.parametrize('status',['running','starting','orphaned','unknown'])
def test_live_or_unknown_dispatcher_cannot_recover(env,blocked,status):
    blocked['run']['status']=status
    blocked['record']()
    with pytest.raises(Conflict,match='live, unknown'):
        recover(env,blocked)


@pytest.mark.parametrize('field,value',[('agent','claude'),('thread_id',999),('ended_at',None)])
def test_mismatched_or_incomplete_dispatcher_record_rejected(env,blocked,field,value):
    blocked['run'][field]=value
    blocked['record']()
    with pytest.raises(Conflict):
        recover(env,blocked)


def test_missing_dispatcher_record_rejected(env,blocked):
    env.board.conn.execute("DELETE FROM board_state WHERE key='dispatch.run.recovery-run'")
    with pytest.raises(Conflict):
        recover(env,blocked)


def test_old_session_activity_after_exit_rejected(env,blocked):
    env.board.heartbeat(env.p['codex'],blocked['old'])
    with pytest.raises(Conflict):
        recover(env,blocked)


def test_active_old_task_lease_rejected(env,blocked):
    task = env.accepted_task(blocked['post']['thread_id'])
    env.board.claim_task(env.p['codex'],blocked['old'],task)
    # Retain a terminal record after the task claim so this specifically checks the lease.
    blocked['run']['ended_at']=env.clock()
    blocked['record']()
    with pytest.raises(Conflict,match='active task lease'):
        recover(env,blocked)


@pytest.mark.parametrize('version',[None,0,1,3])
def test_recovery_requires_exact_version(env,blocked,version):
    with pytest.raises(Conflict,match='exact current'):
        recover(env,blocked,expected_version=version)


def test_recovery_requires_evidence(env,blocked):
    with pytest.raises(Invalid,match='evidence'):
        recover(env,blocked,evidence_post_ids=[])


@pytest.mark.parametrize('kind',['other_session','other_agent','other_thread','sealed','before_block'])
def test_evidence_must_include_new_current_session_same_thread_verification(env,blocked,kind):
    tid = env.thread() if kind=='other_thread' else blocked['post']['thread_id']
    agent = 'claude' if kind=='other_agent' else 'codex'
    sid = env.session('codex') if kind=='other_session' else env.sid[agent]
    if kind=='before_block':
        env.clock.advance(-20)
    evidence = env.post(agent,tid,session_id=sid,sealed=kind=='sealed')
    if kind=='before_block':
        env.clock.advance(20)
    with pytest.raises(Invalid,match='verification evidence'):
        recover(env,blocked,evidence_post_ids=[evidence['id']])


@pytest.mark.parametrize('actor',['claude','human'])
def test_other_identity_cannot_recover(env,blocked,actor):
    with pytest.raises(Forbidden):
        recover(env,blocked,actor=actor,sid=env.sid[actor])


def test_other_project_cannot_recover(env,blocked):
    sid = env.session('codex','/other')
    with pytest.raises(Forbidden,match='source project'):
        recover(env,blocked,sid=sid)


@pytest.mark.parametrize('state',['queued','started','blocked'])
def test_recovery_never_authorizes_execution_transition(env,blocked,state):
    with pytest.raises(Conflict,match='blocked to finished'):
        recover(env,blocked,state=state)


@pytest.mark.parametrize('state',['queued','started'])
def test_only_blocked_source_allowed(env,blocked,state):
    env.board.conn.execute('UPDATE request_progress SET state=? WHERE post_id=?',(state,blocked['post']['id']))
    with pytest.raises(Conflict,match='blocked to finished'):
        recover(env,blocked)


def test_managed_continuation_cannot_bypass_completion_guard(env,blocked,monkeypatch):
    context = requests._context(env.board,env.p['codex'],env.sid['codex'],blocked['post']['id'],'codex')
    monkeypatch.setattr(requests,'_context',lambda *args: context)
    original = workstreams.get_for_post
    monkeypatch.setattr(workstreams,'get_for_post',lambda board,pid: {} if pid==blocked['post']['id'] else original(board,pid))
    with pytest.raises(Forbidden,match='managed continuations'):
        recover(env,blocked)


def test_invalid_recovery_flag_rejected(env,blocked):
    with pytest.raises(Invalid,match='boolean'):
        recover(env,blocked,recover_blocked='true')


def test_http_recovery_is_explicit_and_authenticated(env,blocked):
    from fastapi.testclient import TestClient
    from agent_comms.api import create_app
    from test_issues_api import headers
    client = TestClient(create_app(env.board))
    path = f"/api/posts/{blocked['post']['id']}/request-progress"
    payload = dict(recipient='codex',state='finished',reason='Verified actual restack result',
                   evidence_post_ids=[blocked['evidence']['id']],expected_version=2,recover_blocked=True)
    assert client.post(path,json=payload).status_code==401
    result = client.post(path,json=payload,headers=headers(env))
    assert result.status_code==200,result.text
    assert result.json()['assigned_session']==blocked['old']


def test_mcp_recovery_option_is_callable(env,blocked,monkeypatch):
    import asyncio
    from mcp import Client
    from agent_comms.mcp_server import build_mcp
    monkeypatch.setenv('AGENT_COMMS_TOKEN',env.tokens['codex'])
    async def go():
        async with Client(build_mcp(env.board,'stdio')) as client:
            tools = {tool.name:tool for tool in (await client.list_tools()).tools}
            assert 'recover_blocked' in tools['board_request_progress'].input_schema['properties']
            result = await client.call_tool('board_request_progress',dict(post_id=blocked['post']['id'],
                session_id=env.sid['codex'],recipient='codex',state='finished',reason='Verified actual restack',
                evidence_post_ids=[blocked['evidence']['id']],expected_version=2,recover_blocked=True))
            assert not result.is_error,result
            assert json.loads(result.content[0].text)['state']=='finished'
    asyncio.run(go())
