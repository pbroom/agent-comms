"""Mechanical human choices preserve exact ownership and require completion proof."""
import pytest
from agent_comms import db, requests, resolve
from agent_comms.core import Conflict, Forbidden, Invalid
from agent_comms.dispatch import DispatchConfig


def proposal(e, tid, action):
    return e.post('codex', tid, 'Please choose', 'question', needs_response=True, decision_question={
        'question': 'Perform this exact mechanical action?', 'context': '',
        'options': [{'id': 'yes', 'label': 'Do it', 'description': 'Mechanical action only', 'outcome': 'approved', 'action': action},
                    {'id': 'no', 'label': 'Leave it', 'description': 'No mutation', 'outcome': 'declined'}],
        'recommended_option_id': 'yes'})


def choose(e, q, who='human'):
    return resolve.resolve(e.board, e.p[who], q['id'], 'choose', None, DispatchConfig(), option_id='yes')


def setup(e, kind='close'):
    tid = e.thread()
    req = e.post('human', tid, 'Approved objective', 'request', to=['codex','claude'])
    proof = e.post('codex', tid, 'Verified result')
    action = {'type': kind, 'post_id': req['id'], 'recipient': 'codex', 'expected_version': 0}
    if kind == 'close': action['evidence_post_ids'] = [proof['id']]
    return tid, req, proof, action


def test_exact_close_atomic_idempotent_keeps_other_recipient_and_history(env):
    tid, req, proof, a = setup(env)
    q = proposal(env, tid, a)
    out = choose(env, q)
    assert choose(env, q) == out
    rows = env.board.get_post(env.p['human'], req['id'])['requests']
    assert [(r['recipient'],r['state']) for r in rows] == [('codex','finished'),('claude','queued')]
    assert env.board.get_post(env.p['human'], req['id'])['body'] == 'Approved objective'
    answer = env.board.get_post(env.p['human'], out['post_id'])
    assert answer['requests'][0]['state'] == 'finished'
    assert answer['requests'][0]['evidence_post_ids'] == [out['receipt_post_id']]
    assert env.board._thread_row(tid)['status'] == 'open'
    assert env.board.list_dispatch_rules(env.p['human']) == []


def test_failed_receipt_rolls_back_close_and_attention(env, monkeypatch):
    tid, req, proof, a = setup(env); q = proposal(env, tid, a)
    before = env.board.conn.execute('SELECT COUNT(*) FROM posts').fetchone()[0]
    original = env.board.create_post
    def fail(*args, **kwargs):
        if kwargs.get('body','').startswith('Server executed'): raise RuntimeError('receipt unavailable')
        return original(*args, **kwargs)
    monkeypatch.setattr(env.board, 'create_post', fail)
    with pytest.raises(RuntimeError): choose(env, q)
    assert env.board.get_post(env.p['human'], req['id'])['requests'][0]['state'] == 'queued'
    assert env.board.conn.execute('SELECT COUNT(*) FROM posts').fetchone()[0] == before
    assert resolve._needs_you(env.board.conn, q['id'])


@pytest.mark.parametrize('gate',['stale','started','paused','closed','wrong_thread','foreign_evidence'])
def test_action_gates_are_atomic(env,gate):
    tid, req, proof, a = setup(env)
    if gate == 'wrong_thread': tid = env.thread()
    if gate == 'foreign_evidence': a['evidence_post_ids'] = [env.post('codex',env.thread())['id']]
    q = proposal(env,tid,a)
    if gate == 'stale': requests.progress(env.board,env.p['codex'],env.sid['codex'],req['id'],'codex','blocked','waiting')
    if gate == 'started': requests.progress(env.board,env.p['codex'],env.sid['codex'],req['id'],'codex','started')
    if gate == 'paused': env.board.set_paused(env.p['human'],True)
    if gate == 'closed': env.board.set_thread_status(env.p['human'],tid,'closed')
    with pytest.raises((Conflict, Forbidden, Invalid)): choose(env,q)
    assert env.board.get_post(env.p['human'],req['id'])['requests'][0]['state'] != 'finished'
    assert resolve._needs_you(env.board.conn,q['id'])


def test_agent_cannot_execute_or_supply_shell_payload(env):
    tid, req, proof, a = setup(env); q = proposal(env,tid,a)
    with pytest.raises(Forbidden): choose(env,q,'codex')
    with pytest.raises(Invalid): proposal(env,tid,a | {'command':'touch /tmp/unsafe'})
    with pytest.raises(Invalid): proposal(env,tid,a | {'evidence_post_ids':[]})


def test_human_exact_repost_across_projects_preserves_objective_and_unfinished_source(env):
    tid,req,proof,a = setup(env,'repost')
    target = env.board.create_thread(env.p['human'],env.sid['human'],'target','/work/other')['id']
    a['target_thread_id'] = target
    q = proposal(env,tid,a); out = choose(env,q)
    new = env.board.get_post(env.p['human'],out['action_result']['reposted_post_id'])
    assert new['thread_id'] == target and new['to'] == ['codex']
    assert 'no new scope or access' in new['body']
    assert new['requests'][0]['state'] == 'queued'
    assert env.board.get_post(env.p['human'],req['id'])['requests'][0]['state'] == 'blocked'
    assert choose(env,q) == out


def test_approved_choice_arranges_explicit_one_shot_delivery(env):
    tid = env.thread()
    q = env.post('codex',tid,'Approval needed','question',needs_response=True,decision_question={
        'question':'Proceed?', 'options':[{'id':'yes','label':'Proceed','outcome':'approved'},
                                        {'id':'no','label':'Stop','outcome':'declined'}], 'recommended_option_id':'yes'})
    out = choose(env,q)
    post = env.board.get_post(env.p['human'],out['post_id'])
    assert post['needs_response'] and post['requests'][0]['state'] == 'queued'
    rules = env.board.list_dispatch_rules(env.p['human'])
    assert len(rules)==1 and rules[0]['max_launches']==1


def test_agent_cross_project_repost_reuses_exact_human_scope_then_reconciles(env):
    from agent_comms import decision_actions, issues
    tid,req,proof,a = setup(env,'repost')
    target = env.board.create_thread(env.p['human'],env.sid['human'],'target','/work/other')['id']
    issue = issues.create_issue(env.board,env.p['codex'],env.sid['codex'],title='Authorized routing',body='same objective',thread_id=tid,post_id=req['id'])
    issues.link_issue(env.board,env.p['human'],env.sid['human'],issue['id'],target)
    issues.decide_issue(env.board,env.p['human'],env.sid['human'],issue['id'],'Route within this objective',[tid,target],'approved')
    unrelated = env.post('human',tid,'unrelated','request',to=['codex'])
    with pytest.raises(Forbidden):
        decision_actions.repost(env.board,env.p['codex'],env.sid['codex'],unrelated['id'],'codex',0,target)
    result = decision_actions.repost(env.board,env.p['codex'],env.sid['codex'],req['id'],'codex',0,target)
    next_session = env.session('codex',project='/work/other')
    successor = result['reposted_post_id']
    post = env.board.get_post(env.p['codex'],successor)
    assert post['requests'][0]['state'] == 'queued'
    with pytest.raises(Invalid):
        requests.progress(env.board,env.p['codex'],next_session,successor,'codex','finished','done')
    evidence = env.post('codex',target,'Verified exact successor result',session_id=next_session)
    requests.progress(env.board,env.p['codex'],next_session,successor,'codex','finished','done',
                      [evidence['id']],post['requests'][0]['version'])
    rows = env.board.get_post(env.p['human'],req['id'])['requests']
    assert [r['state'] for r in rows] == ['finished','queued']
    assert env.board.get_post(env.p['human'],unrelated['id'])['requests'][0]['state'] == 'queued'


def test_route_reuses_existing_verified_session(env):
    from agent_comms import capabilities
    tid,req,proof,a = setup(env,'route')
    new_session = env.session('codex')
    capabilities.register(env.board,env.p['codex'],new_session,['git_write'],'verified metadata writable')
    a.update(target_session_id=new_session,required_capabilities=['git_write'])
    q = proposal(env,tid,a)
    result = choose(env,q)
    assert result['action_result']['assigned_session'] == new_session
    assert result['action_result']['state'] == 'queued'


def test_human_assigns_approval_to_implementer_without_proposer_request(env):
    tid = env.thread()
    q = env.post('claude',tid,'Please implement','proposal',needs_response=True)
    out = resolve.resolve(env.board,env.p['human'],q['id'],'approve',None,DispatchConfig(),delivery_agent='codex')
    answer = env.board.get_post(env.p['human'],out['post_id'])
    assert answer['to'] == ['codex'] and answer['answer_to'] == [q['id']]
    assert [r['recipient'] for r in answer['requests']] == ['codex']
    assert answer['requests'][0]['state'] == 'queued'
    assert not resolve._needs_you(env.board.conn,q['id'])
    assert env.board.list_dispatch_rules(env.p['human'])[0]['agents'] == ['codex']


def test_approve_launch_launches_the_chosen_assignee_when_the_author_is_inactive(env):
    tid = env.thread()
    q = env.post('claude',tid,'Please implement','proposal',needs_response=True)
    with db.write_tx(env.board.conn) as c:
        c.execute("UPDATE agents SET active=0 WHERE name='claude'")
    with pytest.raises(Invalid, match='choose an active agent'):
        resolve.resolve(env.board,env.p['human'],q['id'],'approve_launch',None,DispatchConfig())
    out = resolve.resolve(env.board,env.p['human'],q['id'],'approve_launch',None,DispatchConfig(),delivery_agent='codex')
    assert out['agent'] == 'codex' and out['to'] == ['codex'] and out['rule_id'] is not None
    [rule] = env.board.list_dispatch_rules(env.p['human'])
    assert rule['agents'] == ['codex'] and rule['id'] == out['rule_id']
    assert env.board.get_post(env.p['human'],out['post_id'])['to'] == ['codex']


def test_route_rejects_active_owner_lease(env):
    tid,req,proof,a = setup(env)
    task = env.accepted_task(tid)
    env.board.claim_task(env.p['codex'],env.sid['codex'],task)
    requests.progress(env.board,env.p['codex'],env.sid['codex'],req['id'],'codex','blocked','awaiting evidence')
    a['expected_version'] = 1
    q = proposal(env,tid,a)
    with pytest.raises(Conflict,match='active task lease'): choose(env,q)


def test_repost_http_auth_and_guarded_payload(env):
    from fastapi.testclient import TestClient
    from agent_comms.api import create_app
    tid,req,proof,a = setup(env)
    client = TestClient(create_app(env.board))
    url = f"/api/posts/{req['id']}/request-repost"
    body = {'session_id':env.sid['codex'],'recipient':'codex','expected_version':0,'target_thread_id':env.thread()}
    assert client.post(url,json=body).status_code == 401
    assert client.post(url,json=body,headers={'Authorization':'Bearer '+env.tokens['codex']}).status_code == 403
    assert client.post(url,json=body | {'command':'unsafe'},headers={'Authorization':'Bearer '+env.tokens['codex']}).status_code == 422


def test_successor_cannot_be_finished_by_other_session_and_preserves_later_source_closure(env):
    tid,req,proof,a = setup(env,'repost')
    target = env.thread('destination')
    a['target_thread_id'] = target
    q = proposal(env,tid,a); result = choose(env,q)
    successor = result['action_result']['reposted_post_id']
    owner = env.session('codex')
    requests.progress(env.board,env.p['codex'],owner,successor,'codex','started')
    evidence = env.post('codex',target,'verified result',session_id=owner)
    with pytest.raises(Conflict,match='another session'):
        requests.progress(env.board,env.p['codex'],env.sid['codex'],successor,'codex','finished','done',[evidence['id']])
    env.board.set_thread_status(env.p['human'],tid,'closed')
    requests.progress(env.board,env.p['codex'],owner,successor,'codex','finished','done',[evidence['id']])
    assert env.board.get_post(env.p['human'],successor)['requests'][0]['state'] == 'finished'
    assert env.board.get_post(env.p['human'],req['id'])['requests'][0]['state'] == 'blocked'
    assert env.board._thread_row(tid)['status'] == 'closed'


def test_ended_successor_can_use_verified_blocked_recovery(env):
    import json
    from conftest import PROJECT
    tid,req,proof,a = setup(env,'repost')
    target = env.thread('destination'); a['target_thread_id']=target
    result = choose(env,proposal(env,tid,a))
    successor = result['action_result']['reposted_post_id']
    run = {'agent':'codex','thread_id':target,'status':'running','run_id':'successor-run'}
    def record():
        env.board.conn.execute('INSERT OR REPLACE INTO board_state(key,value,updated_at) VALUES (?,?,?)',
            ('dispatch.run.successor-run',json.dumps(run),env.clock()))
    record()
    old = env.board.register_session(env.p['codex'],PROJECT,dispatch_run_id='successor-run')['session_id']
    requests.progress(env.board,env.p['codex'],old,successor,'codex','started')
    requests.progress(env.board,env.p['codex'],old,successor,'codex','blocked','runner ended')
    env.clock.advance(1); run.update(status='exited',ended_at=env.clock()); record()
    env.clock.advance(1)
    evidence = env.post('codex',target,'Fresh result verification')
    result = requests.progress(env.board,env.p['codex'],env.sid['codex'],successor,'codex','finished','verified',
        [evidence['id']],3,recover_blocked=True)
    assert result['state']=='finished' and result['assigned_session']==old
    assert env.board.get_post(env.p['codex'],req['id'])['requests'][0]['state']=='finished'


def test_nested_successors_recheck_original_task_authorization_at_pickup(env):
    tid,req,proof,a = setup(env,'repost')
    task = env.accepted_task(tid)
    # Create the real linked request through the normal API.
    req = env.post('human',tid,'Authorized task','request',to=['codex'],task_id=task)
    a['post_id']=req['id']
    target = env.thread('B'); a['target_thread_id']=target
    first = choose(env,proposal(env,tid,a))['action_result']['reposted_post_id']
    third = env.thread('C')
    second_action = {'type':'repost','post_id':first,'recipient':'codex','expected_version':1,'target_thread_id':third}
    second = choose(env,proposal(env,target,second_action))['action_result']['reposted_post_id']
    # Revoke only authorization of A; B and C have no task of their own.
    env.board.conn.execute("UPDATE tasks SET authorization_source='none' WHERE id=?",(task,))
    with pytest.raises(Forbidden,match='original task authorization'):
        requests.progress(env.board,env.p['codex'],env.sid['codex'],second,'codex','started')
    requests.progress(env.board,env.p['codex'],env.sid['codex'],second,'codex','blocked','Authorization revoked')
    assert env.board.get_post(env.p['codex'],second)['requests'][0]['state']=='blocked'


def test_later_declined_scope_prevents_successor_pickup_but_not_truthful_bookkeeping(env):
    from agent_comms import decision_actions, issues
    tid,req,proof,a = setup(env,'repost'); target=env.thread('B')
    issue=issues.create_issue(env.board,env.p['codex'],env.sid['codex'],title='route',body='same objective',thread_id=tid,post_id=req['id'])
    issues.link_issue(env.board,env.p['human'],env.sid['human'],issue['id'],target)
    issues.decide_issue(env.board,env.p['human'],env.sid['human'],issue['id'],'approved',[tid,target],'approved')
    successor=decision_actions.repost(env.board,env.p['codex'],env.sid['codex'],req['id'],'codex',0,target)['reposted_post_id']
    issues.decide_issue(env.board,env.p['human'],env.sid['human'],issue['id'],'stop',[tid,target],'declined')
    with pytest.raises(Forbidden,match='routing authorization'):
        requests.progress(env.board,env.p['codex'],env.sid['codex'],successor,'codex','started')
    requests.progress(env.board,env.p['codex'],env.sid['codex'],successor,'codex','blocked','Stopped after authorization changed')


def test_closed_source_prevents_new_pickup_but_not_blocker_reporting(env):
    tid,req,proof,a=setup(env,'repost'); target=env.thread('destination'); a['target_thread_id']=target
    successor=choose(env,proposal(env,tid,a))['action_result']['reposted_post_id']
    env.board.set_thread_status(env.p['human'],tid,'closed')
    with pytest.raises(Conflict,match='thread is closed'):
        requests.progress(env.board,env.p['codex'],env.sid['codex'],successor,'codex','started')
    requests.progress(env.board,env.p['codex'],env.sid['codex'],successor,'codex','blocked','Original thread closed')


def test_assignment_checks_lineage_for_target_not_previous_assignee(env, monkeypatch):
    from agent_comms import capabilities, decision_actions
    tid,req,proof,a = setup(env,'route')
    target=env.session('claude')
    capabilities.register(env.board,env.p['claude'],target,['git_write'],'verified repo access')
    checked=[]
    def gate(board, post_id, executor):
        checked.append(executor)
        if executor=='codex': raise Forbidden('previous assignee lost authorization')
    monkeypatch.setattr(decision_actions,'assert_execution_authorized',gate)
    result=requests.assign(env.board,env.p['human'],env.sid['human'],req['id'],'codex',target,0,'Authorized target',['git_write'])
    assert checked==['claude'] and result['assigned_agent']=='claude'
    def revoked(board, post_id, executor):
        raise Forbidden('target authorization revoked')
    monkeypatch.setattr(decision_actions,'assert_execution_authorized',revoked)
    with pytest.raises(Forbidden,match='target authorization revoked'):
        requests.assign(env.board,env.p['human'],env.sid['human'],req['id'],'codex',target,1,'Retry',['git_write'])
