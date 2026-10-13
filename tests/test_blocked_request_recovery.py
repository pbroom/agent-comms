"""Explicit terminal recovery never grants execution ownership."""
import json
import subprocess

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


@pytest.mark.parametrize('disposition', [None, 'completed'])
def test_http_recovery_is_explicit_and_authenticated(env,blocked,disposition):
    from fastapi.testclient import TestClient
    from agent_comms.api import create_app
    from test_issues_api import headers
    client = TestClient(create_app(env.board))
    path = f"/api/posts/{blocked['post']['id']}/request-progress"
    payload = dict(recipient='codex',state='finished',reason='Verified actual restack result',
                   evidence_post_ids=[blocked['evidence']['id']],expected_version=2,recover_blocked=True)
    if disposition is not None:
        payload['disposition'] = disposition
    assert client.post(path,json=payload).status_code==401
    result = client.post(path,json=payload,headers=headers(env))
    assert result.status_code==200,result.text
    assert result.json()['assigned_session']==blocked['old']
    assert result.json()['disposition'] == disposition


@pytest.mark.parametrize('disposition', [None, 'completed', 'superseded'])
def test_mcp_recovery_option_is_callable(env,blocked,monkeypatch,disposition):
    import asyncio
    from mcp import Client
    from agent_comms.mcp_server import build_mcp
    monkeypatch.setenv('AGENT_COMMS_TOKEN',env.tokens['codex'])
    async def go():
        async with Client(build_mcp(env.board,'stdio')) as client:
            tools = {tool.name:tool for tool in (await client.list_tools()).tools}
            assert 'recover_blocked' in tools['board_request_progress'].input_schema['properties']
            assert 'disposition' in tools['board_request_progress'].input_schema['properties']
            payload = dict(post_id=blocked['post']['id'],
                session_id=env.sid['codex'],recipient='codex',state='finished',reason='Verified actual restack',
                evidence_post_ids=[blocked['evidence']['id']],expected_version=2,recover_blocked=True,
                disposition=disposition)
            if disposition is None:
                before = requests.history(env.board, env.p['codex'], blocked['post']['id'], 'codex')
                rejected = await client.call_tool('board_request_progress', dict(payload, disposition='unknown'))
                assert rejected.is_error
                assert requests.history(env.board, env.p['codex'], blocked['post']['id'], 'codex') == before
            result = await client.call_tool('board_request_progress', payload)
            assert not result.is_error,result
            record = json.loads(result.content[0].text)
            assert record['state'] == 'finished' and record['disposition'] == disposition
            assert record['assigned_session'] == blocked['old']
            assert requests.history(env.board, env.p['codex'], blocked['post']['id'], 'codex')[-1]['disposition'] == disposition
    asyncio.run(go())


def test_http_superseded_recovery_preserves_dirty_owner_and_skips_completion(env, blocked, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from agent_comms.api import create_app
    from agent_comms import issues, decision_actions
    from test_issues_api import headers
    project = str(tmp_path / 'original-owner')
    subprocess.run(['git', 'init', project], check=True, capture_output=True)
    unfinished = tmp_path / 'original-owner' / 'unfinished.txt'
    unfinished.write_text('uncommitted work to preserve')
    env.board.conn.execute('UPDATE threads SET project=? WHERE id=?', (project, blocked['post']['thread_id']))
    env.board.conn.execute('UPDATE sessions SET project=?,worktree=? WHERE id=?', (project, project, blocked['old']))
    env.board.register_session(env.p['codex'], project, resume_session_id=env.sid['codex'])
    evidence = env.post('codex', blocked['post']['thread_id'], body='Verified this exact generic request is obsolete')
    old_session = dict(env.board.conn.execute('SELECT * FROM sessions WHERE id=?', (blocked['old'],)).fetchone())
    def no_completion(*args):
        raise AssertionError('retirement cannot propagate completion')
    monkeypatch.setattr(issues, 'reconcile_completed', no_completion)
    monkeypatch.setattr(decision_actions, 'reconcile_successor', no_completion)
    client = TestClient(create_app(env.board))
    result = client.post(f"/api/posts/{blocked['post']['id']}/request-progress", headers=headers(env), json=dict(
        recipient='codex', state='finished', disposition='superseded', recover_blocked=True, expected_version=2,
        reason='Obsolete after verified replacement', evidence_post_ids=[evidence['id']]))
    assert result.status_code == 200, result.text
    assert result.json()['disposition'] == 'superseded'
    assert result.json()['assigned_session'] == blocked['old']
    assert dict(env.board.conn.execute('SELECT * FROM sessions WHERE id=?', (blocked['old'],)).fetchone()) == old_session
    assert unfinished.read_text() == 'uncommitted work to preserve'
    event = requests.history(env.board, env.p['codex'], blocked['post']['id'], 'codex')[-1]
    assert event['disposition'] == 'superseded' and event['assigned_session'] == blocked['old']
    assert event['session_id'] == env.sid['codex'] and event['evidence_post_ids'] == [evidence['id']]


@pytest.mark.parametrize('change,status', [({'disposition': 'unknown'}, 422), ({'state': 'started'}, 400),
    ({'evidence_post_ids': []}, 400), ({'expected_version': 1}, 409), ({'recover_blocked': False}, 409)])
def test_http_superseded_recovery_rejects_invalid_inputs_without_mutation(env, blocked, change, status):
    from fastapi.testclient import TestClient
    from agent_comms.api import create_app
    from test_issues_api import headers
    payload = dict(recipient='codex', state='finished', disposition='superseded', recover_blocked=True,
                   expected_version=2, reason='Explicitly obsolete', evidence_post_ids=[blocked['evidence']['id']])
    payload.update(change)
    before = requests.history(env.board, env.p['codex'], blocked['post']['id'], 'codex')
    result = TestClient(create_app(env.board)).post(f"/api/posts/{blocked['post']['id']}/request-progress",
                                                   headers=headers(env), json=payload)
    assert result.status_code == status, result.text
    assert requests.history(env.board, env.p['codex'], blocked['post']['id'], 'codex') == before


@pytest.mark.parametrize('gate', ['old_evidence', 'linked'])
def test_superseded_terminal_recovery_retains_evidence_and_lineage_guards(env, blocked, gate):
    if gate == 'old_evidence':
        env.board.conn.execute('UPDATE posts SET created_at=created_at-20 WHERE id=?', (blocked['evidence']['id'],))
        error = Invalid
    else:
        env.board.conn.execute('INSERT INTO board_state(key,value,updated_at) VALUES (?,?,?)',
            ('request.successor.999', json.dumps({'source_post_id': blocked['post']['id']}), env.clock()))
        error = Forbidden
    with pytest.raises(error):
        recover(env, blocked, terminal_disposition='superseded')
    assert env.board.get_post(env.p['codex'], blocked['post']['id'])['requests'][0]['state'] == 'blocked'
