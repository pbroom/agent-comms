"""Typed recovery retirement does not manufacture completion of original work."""
import pytest
from agent_comms import db, recovery, requests, unstick
from agent_comms.core import Conflict, Forbidden, Invalid


def setup(e, count=1):
    tid = e.thread()
    originals = [e.post('human', tid, 'unfinished objective', 'request', to=['codex']) for _ in range(count)]
    obligation = e.post('human', tid, 'recover stalled work', 'request', to=['codex'])
    sources = [{'post_id': p['id'], 'recipient': 'codex', 'version': 0} for p in originals]
    with db.write_tx(e.board.conn):
        recovery.record(e.board, e.p['human'], obligation['id'], 'codex', sources)
    return tid, originals, obligation


def pickup(e, source):
    # Exercise the intended internal hook in the same transaction as progress.
    with db.write_tx(e.board.conn):
        previous = e.board.get_post(e.p['codex'], source['id'])['requests'][0]
        requests.progress(e.board,e.p['codex'],e.sid['codex'],source['id'],'codex','started',_in_transaction=True)
        recovery.after_pickup(e.board,e.p['codex'],e.sid['codex'],source,previous)


def row(e,p):
    return e.board.get_post(e.p['human'],p['id'])['requests'][0]


def test_exact_recovery_retires_original_stays_unfinished(env):
    tid, originals, obligation = setup(env)
    unrelated = env.post('human',tid,'unrelated recovery text','request',to=['codex'])
    pickup(env, originals[0])
    assert row(env, originals[0])['state'] == 'started'
    assert row(env, obligation)['state'] == 'finished'
    assert row(env, unrelated)['state'] == 'queued'
    assert row(env, obligation)['evidence_post_ids']
    assert env.board.get_post(env.p['human'],obligation['id'])['body'] == 'recover stalled work'
    assert unstick.stuck_agents(env.board, tid)[1][0]['post_ids'] == [originals[0]['id'],unrelated['id']]


def test_every_linked_request_must_be_picked_up(env):
    _, originals, obligation = setup(env, 2)
    pickup(env,originals[0]); assert row(env,obligation)['state'] == 'queued'
    pickup(env,originals[1]); assert row(env,obligation)['state'] == 'finished'


def test_reblocked_original_does_not_retire_recovery(env):
    _, originals, obligation = setup(env, 2)
    pickup(env,originals[0])
    requests.progress(env.board,env.p['codex'],env.sid['codex'],originals[0]['id'],'codex','blocked','still stuck')
    pickup(env,originals[1])
    assert row(env,obligation)['state'] == 'queued'


@pytest.mark.parametrize('change',['claim','block','seal','version'])
def test_later_recovery_action_wins(env, change):
    _, originals, obligation = setup(env)
    if change == 'claim': requests.progress(env.board,env.p['codex'],env.sid['codex'],obligation['id'],'codex','started')
    elif change == 'block': requests.progress(env.board,env.p['codex'],env.sid['codex'],obligation['id'],'codex','blocked','needs attention')
    elif change == 'version': requests.progress(env.board,env.p['human'],env.sid['human'],obligation['id'],'codex','queued','changed')
    else: env.board.conn.execute('UPDATE posts SET sealed=1 WHERE id=?',(obligation['id'],))
    pickup(env,originals[0])
    assert row(env,obligation)['state'] != 'finished'


def test_text_and_answer_links_are_not_recovery_authority(env):
    tid=env.thread(); original=env.post('human',tid,'work','request',to=['codex'])
    other=env.post('human',tid,'Unstick exact original','request',to=['codex'],answer_to=[original['id']])
    pickup(env,original)
    assert row(env,other)['state']=='queued'


def test_record_requires_human_transaction_and_exact_version(env):
    tid=env.thread(); original=env.post('human',tid,'work','request',to=['codex'])
    obligation=env.post('human',tid,'recover','request',to=['codex'])
    sources=[{'post_id':original['id'],'recipient':'codex','version':0}]
    with pytest.raises(Invalid): recovery.record(env.board,env.p['human'],obligation['id'],'codex',sources)
    with db.write_tx(env.board.conn):
        with pytest.raises(Forbidden): recovery.record(env.board,env.p['codex'],obligation['id'],'codex',sources)
        with pytest.raises(Conflict): recovery.record(env.board,env.p['human'],obligation['id'],'codex',[dict(sources[0],version=1)])


def test_receipt_failure_rolls_back_pickup_and_retirement(env, monkeypatch):
    _, originals, obligation=setup(env)
    def fail(*a,**kw): raise RuntimeError('receipt unavailable')
    monkeypatch.setattr(env.board,'create_post',fail)
    with pytest.raises(RuntimeError): pickup(env,originals[0])
    assert row(env,originals[0])['state']=='queued'
    assert row(env,obligation)['state']=='queued'


def ended_owner(e, tmp_path):
    import json, subprocess
    project = str(tmp_path / 'repo')
    subprocess.run(['git','init',project],check=True,capture_output=True)
    sid = e.session('codex', project=project)
    other = e.session('codex',project=project)
    tid = e.board.create_thread(e.p['human'],e.sid['human'],'ended',project)['id']
    post = e.post('human',tid,'unfinished','request',to=['codex'])
    requests.progress(e.board,e.p['codex'],sid,post['id'],'codex','blocked','needs owner')
    e.board.conn.execute('UPDATE sessions SET dispatch_run_id=? WHERE id=?',('ended',sid))
    e.board.conn.execute('INSERT INTO board_state(key,value,updated_by,updated_at) VALUES (?,?,?,?)',
        ('dispatch.run.ended',json.dumps({'agent':'codex','thread_id':tid,'status':'exited','ended_at':e.clock()}),'codex',e.clock()))
    # An explicit idle attestation makes the shared checkout safe to inspect.
    e.board.conn.execute('INSERT INTO session_activity(session_id,state,recorded_at) VALUES (?,?,?)',(other,'idle',e.clock()))
    return post,sid,other,project


def test_ended_transfer_keeps_original_unfinished_and_requires_new_pickup(env,tmp_path):
    post,old,new,_=ended_owner(env,tmp_path)
    out=recovery.transfer_ended_owner(env.board,env.p['codex'],new,post['id'],'codex',1)
    assert out['assigned_session']==new and out['state']=='queued' and out['version']==2
    assert 'preflight still required' in out['reason']
    with pytest.raises(Conflict): recovery.transfer_ended_owner(env.board,env.p['codex'],new,post['id'],'codex',1)


@pytest.mark.parametrize('gate',['dirty','active','stale','unknown','denial'])
def test_ended_transfer_preserves_ownership_on_gates(env,tmp_path,monkeypatch,gate):
    import pathlib
    from agent_comms import browser_readiness
    post,old,new,project=ended_owner(env,tmp_path)
    version=1
    if gate=='dirty': pathlib.Path(project,'unfinished.txt').write_text('preserve')
    elif gate=='active': env.board.conn.execute("UPDATE session_activity SET state='active' WHERE session_id=?",(new,))
    elif gate=='stale': version=0
    elif gate=='unknown': env.board.conn.execute("DELETE FROM board_state WHERE key='dispatch.run.ended'")
    else: monkeypatch.setattr(browser_readiness,'request_blocker',lambda *a:'host denied')
    with pytest.raises(Conflict): recovery.transfer_ended_owner(env.board,env.p['codex'],new,post['id'],'codex',version)
    assert row(env,post)['assigned_session']==old and row(env,post)['state']=='blocked'


def test_exact_routing_carries_recovery_link_until_successor_pickup(env):
    from agent_comms import capabilities
    _, originals, obligation=setup(env)
    old=env.sid['codex']; new=env.session('codex')
    capabilities.register(env.board,env.p['codex'],new,['git'],'verified')
    with db.write_tx(env.board.conn):
        prior=row(env,originals[0])
        requests.assign(env.board,env.p['codex'],old,originals[0]['id'],'codex',new,0,'successor',['git'],_in_transaction=True)
        recovery.after_route(env.board,env.p['codex'],originals[0],prior)
    assert row(env,obligation)['state']=='queued'
    env.sid['codex']=new
    pickup(env,originals[0])
    assert row(env,obligation)['state']=='finished'
    assert row(env,originals[0])['state']=='started'


def test_repickup_after_block_can_retire(env):
    _, originals, obligation=setup(env,2)
    pickup(env,originals[0])
    requests.progress(env.board,env.p['codex'],env.sid['codex'],originals[0]['id'],'codex','blocked','temporary')
    pickup(env,originals[1]); pickup(env,originals[0])
    assert row(env,obligation)['state']=='finished'


def test_evidenced_finish_before_other_pickup_can_retire(env):
    tid, originals, obligation=setup(env,2)
    pickup(env,originals[0])
    evidence=env.post('codex',tid,'verified completion')
    requests.progress(env.board,env.p['codex'],env.sid['codex'],originals[0]['id'],'codex','finished','done',[evidence['id']])
    pickup(env,originals[1])
    assert row(env,obligation)['state']=='finished'
    assert row(env,originals[0])['state']=='finished'
    assert row(env,originals[1])['state']=='started'


def authorized_successor(e,tmp_path):
    post,old,new,project=ended_owner(e,tmp_path)
    task=e.accepted_task(post['thread_id'])
    e.board.claim_task(e.p['codex'],new,task)
    return post,old,new,project


def test_active_authorized_successor_preserves_own_dirty_artifacts(env,tmp_path):
    import pathlib
    post,old,new,project=authorized_successor(env,tmp_path)
    artifact=pathlib.Path(project,'audit-evidence.md'); artifact.write_text('ongoing')
    out=recovery.transfer_ended_owner(env.board,env.p['codex'],new,post['id'],'codex',1)
    assert out['assigned_session']==new and out['state']=='queued'
    assert artifact.read_text()=='ongoing'


@pytest.mark.parametrize('gate',['old_lease','old_live','other_peer','git_operation'])
def test_active_successor_exception_still_fences_old_owner_and_peers(env,tmp_path,gate):
    import pathlib
    post,old,new,project=authorized_successor(env,tmp_path)
    if gate=='old_lease':
        task=env.accepted_task(post['thread_id']); env.board.claim_task(env.p['codex'],old,task)
    elif gate=='old_live':
        env.clock.advance(1); env.board.conn.execute('UPDATE sessions SET last_seen=? WHERE id=?',(env.clock(),old))
    elif gate=='other_peer': env.session('claude',project=project)
    else: pathlib.Path(project,'.git','MERGE_HEAD').write_text('pending')
    with pytest.raises(Conflict): recovery.transfer_ended_owner(env.board,env.p['codex'],new,post['id'],'codex',1)
    assert row(env,post)['assigned_session']==old
