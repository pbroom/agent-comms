import pytest
from agent_comms import requests
from agent_comms.core import Conflict, Forbidden, Invalid, NotFound, Paused


def request(env, **kw):
    return env.post('human',env.thread(),to=['codex','claude'],type='request',**kw)


def update(env, post, state, as_='codex', **kw):
    return requests.progress(env.board,env.p[as_],env.sid[as_],post['id'],'codex',state,**kw)


def test_unrelated_reply_and_independent_recipients(env):
    post=request(env)
    env.post('codex',post['thread_id'],'unrelated response')
    assert [r['state'] for r in env.board.get_post(env.p['human'],post['id'])['requests']]==['queued','queued']
    update(env,post,'started')
    evidence=env.post('codex',post['thread_id'],'implemented the requested fix')
    update(env,post,'finished',reason='Verified fix',evidence_post_ids=[evidence['id']])
    assert [r['state'] for r in env.board.get_post(env.p['human'],post['id'])['requests']]==['finished','queued']
    assert len(requests.history(env.board,env.p['human'],post['id'],'codex'))==2


def test_idempotent_terminal_and_version(env):
    post=request(env)
    first=update(env,post,'finished',reason='Verified complete')
    assert update(env,post,'finished',reason='Verified complete',expected_version=0)==first
    with pytest.raises(Conflict): update(env,post,'started')
    with pytest.raises(Invalid): update(env,post,'finished')


def test_session_ownership_and_author_close(env):
    post=request(env)
    update(env,post,'started')
    other=env.session('codex')
    with pytest.raises(Conflict): requests.progress(env.board,env.p['codex'],other,post['id'],'codex','finished',reason='done')
    update(env,post,'finished',as_='human',reason='Confirmed completion')


def test_project_pause_visibility_and_actor(env):
    post=request(env)
    with pytest.raises(Forbidden): update(env,post,'started',as_='claude')
    other=env.session('codex','/other')
    with pytest.raises(Forbidden): requests.progress(env.board,env.p['codex'],other,post['id'],'codex','started')
    env.board.set_paused(env.p['human'],True)
    with pytest.raises(Paused): update(env,post,'started')
    env.board.set_paused(env.p['human'],False)
    hidden=request(env,sealed=True)
    with pytest.raises(NotFound): update(env,hidden,'started')


def test_fyi_and_self_are_not_requests(env):
    post=env.post('codex',env.thread(),to=['codex','human','claude'],body='all done')
    assert post['requests']==[]
    post=env.post('codex',post['thread_id'],type='request',to=['codex','human','claude'])
    assert [r['recipient'] for r in post['requests']]==['claude']


def test_evidence_boundaries_and_stale_version(env):
    post=request(env)
    update(env,post,'started',expected_version=0)
    with pytest.raises(Conflict): update(env,post,'blocked',reason='missing access',expected_version=0)
    wrong=env.post('codex',env.thread())
    with pytest.raises(Invalid): update(env,post,'finished',reason='done',evidence_post_ids=[wrong['id']])


def test_author_and_human_cannot_release_executing_session(env):
    post=env.post('claude',env.thread(),type='request',to=['codex'])
    update(env,post,'started')
    for actor in ('claude','human'):
        with pytest.raises(Conflict):
            update(env,post,'blocked',as_=actor,reason='reroute')
    assert update(env,post,'blocked',reason='I stopped execution')['state']=='blocked'


def test_dispatch_binding_requires_exact_active_identity_project_and_one_session(env):
    import json
    from conftest import PROJECT
    tid=env.thread()
    def record(status='running'):
        env.board.conn.execute('INSERT OR REPLACE INTO board_state(key,value,updated_at) VALUES (?,?,?)',
            ('dispatch.run.run-1',json.dumps(dict(agent='codex',thread_id=tid,status=status)),env.clock()))
    record()
    sid=env.board.register_session(env.p['codex'],PROJECT,dispatch_run_id='run-1')['session_id']
    assert env.board.conn.execute('SELECT dispatch_run_id FROM sessions WHERE id=?',(sid,)).fetchone()[0]=='run-1'
    with pytest.raises(Conflict): env.board.register_session(env.p['codex'],PROJECT,dispatch_run_id='run-1')
    with pytest.raises(Forbidden): env.board.register_session(env.p['claude'],PROJECT,dispatch_run_id='run-1')
    with pytest.raises(Forbidden): env.board.register_session(env.p['codex'],'/other',dispatch_run_id='run-1')
    with pytest.raises(Conflict): env.board.register_session(env.p['codex'],'/other',resume_session_id=sid)
    record('finished')
    with pytest.raises(Forbidden): env.board.register_session(env.p['codex'],PROJECT,resume_session_id=sid,dispatch_run_id='run-1')
