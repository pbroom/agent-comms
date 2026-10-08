"""An explicit reply and its exact recipient transition succeed or fail together."""
import json
import pytest
from agent_comms import requests
from agent_comms.core import Conflict, Forbidden, Invalid


def setup(e):
    tid=e.thread()
    source=e.post('human',tid,'do work','request',to=['codex','claude'])
    return tid,source


def reply(e,tid,source,state='finished',key='reply-1',**changes):
    envelope=dict(post_id=source['id'],recipient='codex',expected_version=0,state=state,reason='Explicit result')
    if state=='finished': envelope['disposition']='completed'
    envelope.update(changes)
    return e.post('codex',tid,'Here is my response','status',request_reply=envelope,idempotency_key=key)


def rows(e,source): return e.board.get_post(e.p['human'],source['id'])['requests']


def test_unlinked_post_does_not_finish_request(env):
    tid,source=setup(env)
    env.post('codex',tid,'done!','status',needs_response=False)
    assert rows(env,source)[0]['state']=='queued'


def test_partial_and_exact_completed_reply_keep_other_recipient(env):
    tid,source=setup(env)
    partial=reply(env,tid,source,'started')
    assert rows(env,source)[0]['state']=='started'
    assert rows(env,source)[1]['state']=='queued'
    done=reply(env,tid,source,key='finish',expected_version=1)
    assert rows(env,source)[0]['evidence_post_ids']==[done['id']]
    assert rows(env,source)[0]['disposition']=='completed'
    assert rows(env,source)[1]['state']=='queued'
    assert env.board.get_post(env.p['codex'],done['id'])['request_reply']==done['request_reply']
    assert done['request_reply']['post_id']==source['id']
    assert partial['request_reply']['state']=='started'


def test_superseded_is_explicit_audited_not_completion_reconciliation(env,monkeypatch):
    from agent_comms import issues,decision_actions
    tid,source=setup(env)
    def forbidden(*a): raise AssertionError('superseded cannot reconcile completion')
    monkeypatch.setattr(issues,'reconcile_completed',forbidden)
    monkeypatch.setattr(decision_actions,'reconcile_successor',forbidden)
    result=reply(env,tid,source,disposition='superseded')
    assert rows(env,source)[0]['disposition']=='superseded'
    history=requests.history(env.board,env.p['codex'],source['id'],'codex')
    assert history[-1]['disposition']=='superseded'
    assert result['request_reply']['disposition']=='superseded'


def test_reply_replay_has_one_post_event_and_notification_even_after_cap(env,monkeypatch):
    tid,source=setup(env); notified=[]
    monkeypatch.setattr(env.board,'_notify',lambda *a: notified.append(a))
    first=reply(env,tid,source)
    env.settings.daily_post_cap_per_agent=0
    again=reply(env,tid,source)
    assert again['id']==first['id']
    assert len(requests.history(env.board,env.p['codex'],source['id'],'codex'))==1
    assert len(notified)==1
    with pytest.raises(Conflict): reply(env,tid,source,reason='changed')


def test_same_key_other_session_does_not_replay_or_leak(env):
    tid,source=setup(env); reply(env,tid,source)
    env.sid['codex']=env.session('codex')
    with pytest.raises(Conflict): reply(env,tid,source)


@pytest.mark.parametrize('gate',['version','thread','recipient','sealed','ownership','paused','preflight'])
def test_failure_rolls_back_post_and_transition(env,monkeypatch,gate):
    from agent_comms import browser_readiness
    tid,source=setup(env); changes={}
    if gate=='version': changes['expected_version']=1
    elif gate=='thread': tid=env.thread()
    elif gate=='recipient': changes['recipient']='grok'
    elif gate=='sealed': env.board.conn.execute('UPDATE posts SET sealed=1 WHERE id=?',(source['id'],))
    elif gate=='ownership':
        owner=env.session('codex'); requests.progress(env.board,env.p['codex'],owner,source['id'],'codex','blocked','waiting'); changes['expected_version']=1
    elif gate=='paused': env.board.set_paused(env.p['human'],True)
    else:
        def deny(*a): raise Conflict('host denied')
        monkeypatch.setattr(browser_readiness,'assert_request_ready',deny)
    before=env.board.conn.execute('SELECT COUNT(*) FROM posts').fetchone()[0]
    from agent_comms.core import BoardError
    with pytest.raises(BoardError): reply(env,tid,source,**changes)
    assert env.board.conn.execute('SELECT COUNT(*) FROM posts').fetchone()[0]==before
    assert env.board.conn.execute('SELECT COUNT(*) FROM request_reply_operations').fetchone()[0]==0


def test_receipt_storage_failure_rolls_back_everything(env):
    tid,source=setup(env)
    env.board.conn.execute("CREATE TRIGGER fail_reply BEFORE INSERT ON request_reply_operations BEGIN SELECT RAISE(ABORT,'no receipt'); END")
    import sqlite3
    before=env.board.conn.execute('SELECT COUNT(*) FROM posts').fetchone()[0]
    with pytest.raises(sqlite3.IntegrityError): reply(env,tid,source)
    assert len(requests.history(env.board,env.p['codex'],source['id'],'codex'))==0
    assert env.board.conn.execute('SELECT COUNT(*) FROM posts').fetchone()[0]==before


def test_superseded_rejects_exact_linked_lineage(env):
    tid,source=setup(env)
    env.board.conn.execute('INSERT INTO board_state(key,value,updated_by,updated_at) VALUES (?,?,?,?)',
        ('request.successor.999',json.dumps({'source_post_id':source['id']}),'human',env.clock()))
    with pytest.raises(Forbidden): reply(env,tid,source,disposition='superseded')


def test_generic_legacy_events_have_no_invented_disposition(env):
    tid,source=setup(env)
    requests.progress(env.board,env.p['codex'],env.sid['codex'],source['id'],'codex','finished','explicit old API')
    assert rows(env,source)[0]['disposition'] is None
    assert requests.history(env.board,env.p['codex'],source['id'],'codex')[0]['disposition'] is None


def test_concurrent_identical_replies_have_one_effect(env):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    from agent_comms.core import Board
    tid,source=setup(env); barrier=threading.Barrier(2)
    def call():
        board=Board(env.settings,clock=env.clock)
        p=board.authenticate(env.tokens['codex'])
        barrier.wait(timeout=5)
        result=board.create_post(p,env.sid['codex'],thread_id=tid,body='result',type='status',idempotency_key='concurrent',
            request_reply=dict(post_id=source['id'],recipient='codex',expected_version=0,state='finished',reason='verified',disposition='completed'))
        board.conn.close()
        return result
    with ThreadPoolExecutor(max_workers=2) as executor:
        a,b=list(executor.map(lambda _:call(),range(2)))
    assert a['id']==b['id']
    assert len(requests.history(env.board,env.p['codex'],source['id'],'codex'))==1
    assert env.board.conn.execute('SELECT COUNT(*) FROM request_reply_operations').fetchone()[0]==1


def test_same_key_competing_payloads_only_one_wins(env):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    from agent_comms.core import Board
    tid,source=setup(env); barrier=threading.Barrier(2)
    def call(body):
        board=Board(env.settings,clock=env.clock); p=board.authenticate(env.tokens['codex'])
        barrier.wait(timeout=5)
        try:
            board.create_post(p,env.sid['codex'],thread_id=tid,body=body,type='status',idempotency_key='same-key',
                request_reply=dict(post_id=source['id'],recipient='codex',expected_version=0,state='started',reason='working'))
            return 'ok'
        except Conflict: return 'conflict'
        finally: board.conn.close()
    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes=list(executor.map(call,['one','two']))
    assert sorted(outcomes)==['conflict','ok']
    assert len(requests.history(env.board,env.p['codex'],source['id'],'codex'))==1


def test_replay_does_not_touch_heartbeat(env):
    tid,source=setup(env);reply(env,tid,source)
    before=env.board.conn.execute('SELECT last_seen FROM sessions WHERE id=?',(env.sid['codex'],)).fetchone()[0]
    env.clock.advance(10);reply(env,tid,source)
    after=env.board.conn.execute('SELECT last_seen FROM sessions WHERE id=?',(env.sid['codex'],)).fetchone()[0]
    assert after==before


def test_later_unrelated_answer_completion_does_not_complete_superseded_work(env):
    from agent_comms import issues,pickup
    tid=env.thread(); source=env.post('human',tid,'Original work','request',to=['codex'])
    reply(env,tid,source,disposition='superseded')
    question=env.post('codex',tid,'Approve separate work?',type='question',needs_response=True,to=['human'])
    answer=env.post('human',tid,'Approved separate work',answer_to=[question['id']],to=['codex'])
    evidence=env.post('codex',tid,'Separate work verified')
    requests.progress(env.board,env.p['codex'],env.sid['codex'],answer['id'],'codex','finished',
                      'Verified separate work',[evidence['id']])
    assert not issues._completed_answer_work(env.board,tid)
    assert env.board._thread_row(tid)['status']=='open'
    assert rows(env,source)[0]['disposition']=='superseded'
    # Settled visual pickup status is separate from completion propagation.
    assert pickup.for_thread(env.board,env.p['human'],tid)['complete']


def test_human_answer_link_added_after_supersession_cannot_make_original_complete(env):
    from agent_comms import issues
    tid=env.thread(); source=env.post('human',tid,'Original work','request',to=['codex'])
    reply(env,tid,source,disposition='superseded')
    answer=env.post('human',tid,'Newly linked answer',answer_to=[source['id']],to=['codex'])
    evidence=env.post('codex',tid,'Answer complete')
    requests.progress(env.board,env.p['codex'],env.sid['codex'],answer['id'],'codex','finished',
                      'Verified answer',[evidence['id']])
    assert not issues._completed_answer_work(env.board,tid)
    assert env.board._thread_row(tid)['status']=='open'


def superseded_thread(e):
    tid=e.thread(); source=e.post('human',tid,'Original work','request',to=['codex'])
    reply(e,tid,source,disposition='superseded')
    return tid


def test_legacy_thread_autoclose_is_fenced_and_prior_writes_roll_back(env):
    import sqlite3
    from agent_comms import db
    tid=superseded_thread(env)
    old=db.connect(env.settings.db_path)  # Already-running writer: deliberately no schema init.
    with pytest.raises(sqlite3.IntegrityError,match='refresh this client'):
        with db.write_tx(old):  # v10 also used managed_write_permit; it cannot bypass the new fence.
            old.execute("UPDATE threads SET pinned_summary='unsafe completion inference' WHERE id=?",(tid,))
            old.execute("UPDATE threads SET status='closed' WHERE id=?",(tid,))
    assert env.board._thread_row(tid)['status']=='open'
    assert env.board._thread_row(tid)['pinned_summary'] is None
    assert old.execute('SELECT COUNT(*) FROM supersession_close_permit').fetchone()[0]==0
    # Ordinary old-client traffic on unaffected threads keeps working.
    other=env.thread()
    with db.write_tx(old): old.execute("UPDATE threads SET status='closed' WHERE id=?",(other,))
    assert env.board._thread_row(other)['status']=='closed'
    old.close()


def test_legacy_issue_resolve_is_fenced_even_when_other_issue_prevents_thread_close(env):
    import sqlite3
    from agent_comms import db,issues
    tid=superseded_thread(env)
    first=issues.create_issue(env.board,env.p['codex'],env.sid['codex'],title='one',body='one',thread_id=tid,needs_human=False)
    other=issues.create_issue(env.board,env.p['codex'],env.sid['codex'],title='two',body='two',thread_id=tid,needs_human=True)
    old=db.connect(env.settings.db_path)
    with pytest.raises(sqlite3.IntegrityError,match='refresh this client'):
        with db.write_tx(old):
            # Old auto-reconciliation resolves an eligible issue before considering
            # thread closure. Another open issue would suppress that later close.
            old.execute("UPDATE issues SET title='incorrectly completed' WHERE id=?",(first['id'],))
            old.execute("UPDATE issues SET status='resolved',needs_human=0 WHERE id=?",(first['id'],))
            if not old.execute("SELECT 1 FROM issue_links l JOIN issues i ON i.id=l.issue_id WHERE l.thread_id=? AND i.status!='resolved'",(tid,)).fetchone():
                old.execute("UPDATE threads SET status='closed' WHERE id=?",(tid,))
    assert issues.get_issue(env.board,env.p['human'],first['id'])['status']=='open'
    assert issues.get_issue(env.board,env.p['human'],first['id'])['title']=='one'
    assert issues.get_issue(env.board,env.p['human'],other['id'])['status']=='open'
    assert old.execute('SELECT COUNT(*) FROM supersession_close_permit').fetchone()[0]==0
    old.close()


def test_explicit_human_close_and_resolution_get_scoped_transient_permits(env):
    from agent_comms import issues
    tid=superseded_thread(env)
    issue=issues.create_issue(env.board,env.p['codex'],env.sid['codex'],title='one',body='one',thread_id=tid)
    resolved=issues.resolve_issue(env.board,env.p['human'],env.sid['human'],issue['id'],'Human accepts retirement')
    assert resolved['status']=='resolved'
    assert env.board.conn.execute('SELECT COUNT(*) FROM supersession_close_permit').fetchone()[0]==0
    assert env.board.set_thread_status(env.p['human'],tid,'closed')['status']=='closed'
    assert env.board.conn.execute('SELECT COUNT(*) FROM supersession_close_permit').fetchone()[0]==0


def test_human_permit_rolls_back_on_failure_and_does_not_cover_other_entity(env):
    import sqlite3
    from agent_comms import db
    tid=superseded_thread(env)
    other=env.thread(); source=env.post('human',other,'work','request',to=['codex'])
    reply(env,other,source,disposition='superseded',key='other-thread')
    with pytest.raises(sqlite3.IntegrityError):
        with db.write_tx(env.board.conn):
            with db.explicit_supersession_close(env.board.conn,'thread',tid):
                env.board.conn.execute("UPDATE threads SET status='closed' WHERE id=?",(other,))
    assert env.board.conn.execute('SELECT COUNT(*) FROM supersession_close_permit').fetchone()[0]==0
    env.board.conn.execute("CREATE TRIGGER fail_manual_close BEFORE UPDATE OF status ON threads BEGIN SELECT RAISE(ABORT,'fail'); END")
    with pytest.raises(sqlite3.IntegrityError): env.board.set_thread_status(env.p['human'],tid,'closed')
    assert env.board._thread_row(tid)['status']=='open'
    assert env.board.conn.execute('SELECT COUNT(*) FROM supersession_close_permit').fetchone()[0]==0


def test_existing_authorized_thread_creator_can_explicitly_close_superseded_thread(env):
    tid=env.thread(as_='codex'); source=env.post('human',tid,'work','request',to=['codex'])
    reply(env,tid,source,disposition='superseded')
    with pytest.raises(Forbidden): env.board.set_thread_status(env.p['claude'],tid,'closed')
    assert env.board.set_thread_status(env.p['codex'],tid,'closed')['status']=='closed'
    assert env.board.conn.execute('SELECT COUNT(*) FROM supersession_close_permit').fetchone()[0]==0
