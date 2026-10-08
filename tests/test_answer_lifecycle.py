"""Exact human answers hand work back to intended agents without guessing completion."""
import json

import pytest

from agent_comms import db, issues, requests, resolve
from agent_comms.core import Conflict, Forbidden, Invalid
from agent_comms.dispatch import DispatchConfig


def question(env, tid, author='codex', **kw):
    return env.post(author,tid,type='question',to=['human'],needs_response=True,**kw)


def pending(env):
    return {r['id'] for r in env.board.conn.execute(f'SELECT p.id FROM posts p WHERE {env.board.NEEDS_YOU_SOURCE}')}


def answer(env, source, **kw):
    return env.post('human',source['thread_id'],body='Proceed with this exact requested change',answer_to=[source['id']],**kw)


def finish(env, post, actor='codex', evidence=True):
    proof = env.post(actor,post['thread_id'],body='Verified this exact request is complete')
    row = next(r for r in env.board.get_post(env.p[actor],post['id'])['requests'] if r['recipient']==actor)
    result = requests.progress(env.board,env.p[actor],env.sid[actor],post['id'],actor,'finished',
        reason='Verified exact work',evidence_post_ids=[proof['id']] if evidence else [],expected_version=row['version'])
    with db.write_tx(env.board.conn):
        issues.reconcile_completed(env.board,env.p[actor],env.sid[actor],post['thread_id'])
    return result


def test_unrelated_human_post_cannot_clear_questions(env):
    tid=env.thread()
    one=question(env,tid)
    two=question(env,tid,'claude')
    env.post('human',tid,'Unrelated update')
    assert pending(env)=={one['id'],two['id']}
    linked=answer(env,one)
    assert linked['answer_to']==[one['id']]
    assert pending(env)=={two['id']}
    assert [(r['recipient'],r['state']) for r in linked['requests']]==[('codex','queued')]


def test_resolve_records_exact_link_without_finishing_work(env):
    tid=env.thread()
    one=question(env,tid)
    two=question(env,tid,'claude')
    result=resolve.resolve(env.board,env.p['human'],one['id'],'approve',None,DispatchConfig())
    linked=env.board.get_post(env.p['human'],result['post_id'])
    assert linked['answer_to']==[one['id']]
    assert linked['requests'][0]['state']=='queued'
    assert pending(env)=={two['id']}
    assert env.board.get_thread(env.p['human'],tid)['status']=='open'


def test_agents_cannot_forge_answer_links(env):
    source=question(env,env.thread())
    with pytest.raises(Forbidden):
        env.post('codex',source['thread_id'],answer_to=[source['id']])
    assert pending(env)=={source['id']}


@pytest.mark.parametrize('bad',['other_thread','missing','duplicate','bool','sealed','omitted_author'])
def test_invalid_answer_links_roll_back_post(env,bad):
    source=question(env,env.thread())
    ids=[source['id']]
    kw={}
    if bad=='other_thread': ids=[question(env,env.thread())['id']]
    if bad=='missing': ids=[9999]
    if bad=='duplicate': ids*=2
    if bad=='bool': ids=[True]
    if bad=='sealed': kw['sealed']=True
    if bad=='omitted_author': kw['to']=['claude']
    before=env.board.conn.execute('SELECT COUNT(*) FROM posts').fetchone()[0]
    with pytest.raises(Invalid):
        env.post('human',source['thread_id'],answer_to=ids,**kw)
    assert env.board.conn.execute('SELECT COUNT(*) FROM posts').fetchone()[0]==before


def test_sealed_source_reference_not_leaked_to_another_agent(env):
    source=question(env,env.thread(),sealed=True)
    linked=answer(env,source,to=['codex','claude'])
    assert env.board.get_post(env.p['codex'],linked['id'])['answer_to']==[source['id']]
    assert env.board.get_post(env.p['claude'],linked['id'])['answer_to']==[]


def test_issue_answer_fanout_snapshots_exact_links_and_version(env):
    first,second=env.thread(),env.thread()
    one,two=question(env,first),question(env,second,'claude')
    issue=issues.create_issue(env.board,env.p['codex'],env.sid['codex'],title='Shared choice',body='Choose scope',
                              thread_id=first,post_id=one['id'])
    issues.link_issue(env.board,env.p['claude'],env.sid['claude'],issue['id'],second,two['id'])
    result=issues.decide_issue(env.board,env.p['human'],env.sid['human'],issue['id'],'First only',[first])
    assert result['needs_human']
    assert two['id'] in pending(env) and one['id'] not in pending(env)
    snapshots=env.board.conn.execute('SELECT * FROM issue_answer_links').fetchall()
    assert len(snapshots)==1 and snapshots[0]['question_version']==issue['question_version']
    post=env.board.get_post(env.p['human'],snapshots[0]['answer_post_id'])
    assert post['to']==['codex'] and post['requests'][0]['state']=='queued'
    event=env.board.conn.execute('SELECT decision FROM issue_comments WHERE id=?',(snapshots[0]['decision_comment_id'],)).fetchone()
    assert json.loads(event[0])['issue_link_ids']==[snapshots[0]['issue_link_id']]


def test_issue_decision_and_answer_posts_roll_back_together(env,monkeypatch):
    tids=[env.thread(),env.thread()]
    issue=issues.create_issue(env.board,env.p['codex'],env.sid['codex'],title='Choice',body='Choose',thread_id=tids[0])
    issues.link_issue(env.board,env.p['claude'],env.sid['claude'],issue['id'],tids[1])
    original=env.board.create_post
    def fail_second(*args,**kw):
        if kw.get('thread_id')==tids[1]: raise Invalid('injected second answer failure')
        return original(*args,**kw)
    monkeypatch.setattr(env.board,'create_post',fail_second)
    with pytest.raises(Invalid):
        issues.decide_issue(env.board,env.p['human'],env.sid['human'],issue['id'],'Both',tids)
    assert not env.board.conn.execute('SELECT 1 FROM posts').fetchone()
    assert not env.board.conn.execute("SELECT 1 FROM issue_comments WHERE kind='decision'").fetchone()
    assert issues.get_issue(env.board,env.p['human'],issue['id'])['needs_human']


def test_multiple_recipients_must_all_finish_with_evidence(env):
    tid=env.thread()
    one,two=question(env,tid),question(env,tid,'claude')
    linked=env.post('human',tid,answer_to=[one['id'],two['id']])
    finish(env,linked)
    assert env.board.get_thread(env.p['human'],tid)['status']=='open'
    finish(env,linked,'claude')
    assert env.board.get_thread(env.p['human'],tid)['status']=='closed'


@pytest.mark.parametrize('blocker',['missing_evidence','task','other_question','legacy','unpicked_request'])
def test_completion_never_closes_uncertain_or_unfinished_thread(env,blocker):
    tid=env.thread()
    source=question(env,tid)
    linked=answer(env,source)
    if blocker=='task': env.accepted_task(tid)
    if blocker=='other_question': question(env,tid,'claude')
    if blocker=='legacy':
        old=question(env,tid,'claude')
        env.board.conn.execute('INSERT INTO legacy_attention_answers(source_post_id,recorded_at) VALUES (?,?)',(old['id'],env.clock()))
    if blocker=='unpicked_request': env.post('claude',tid,type='request',to=['codex'])
    finish(env,linked,evidence=blocker!='missing_evidence')
    assert env.board.get_thread(env.p['human'],tid)['status']=='open'


def test_issue_auto_resolution_requires_every_linked_answer_complete(env):
    first,second=env.thread(),env.thread()
    one,two=question(env,first),question(env,second,'claude')
    issue=issues.create_issue(env.board,env.p['codex'],env.sid['codex'],title='Choice',body='Choose',thread_id=first,post_id=one['id'])
    issues.link_issue(env.board,env.p['claude'],env.sid['claude'],issue['id'],second,two['id'])
    issues.decide_issue(env.board,env.p['human'],env.sid['human'],issue['id'],'Both',[first,second])
    posts=[env.board.get_post(env.p['human'],r[0]) for r in env.board.conn.execute('SELECT DISTINCT answer_post_id FROM issue_answer_links ORDER BY answer_post_id')]
    finish(env,posts[0])
    assert issues.get_issue(env.board,env.p['human'],issue['id'])['status']=='open'
    finish(env,posts[1],'claude')
    resolved=issues.get_issue(env.board,env.p['human'],issue['id'])
    assert resolved['status']=='resolved'
    assert resolved['resolution']['agent']=='claude'


def test_legacy_upgrade_preserves_attention_only_and_never_guesses_new_answers(env):
    tid=env.thread()
    old=question(env,tid)
    env.post('human',tid,'Old answer without an exact link')
    env.board.conn.execute('PRAGMA user_version=9')
    db.init_schema(env.board.conn)
    assert old['id'] not in pending(env)
    assert not env.board.conn.execute('SELECT 1 FROM answer_links').fetchone()
    fresh=question(env,tid,'claude')
    env.post('human',tid,'New unrelated comment')
    db.init_schema(env.board.conn)
    assert fresh['id'] in pending(env)
    assert env.board.get_thread(env.p['human'],tid)['status']=='open'


def test_empty_or_only_informational_thread_cannot_autoclose(env):
    tid=env.thread()
    env.post('codex',tid,'FYI')
    with db.write_tx(env.board.conn):
        result=issues.reconcile_completed(env.board,env.p['codex'],env.sid['codex'],tid)
    assert result=={'closed_thread_ids':[],'resolved_issue_ids':[]}


def test_issue_answer_reaches_a_closed_linked_thread_and_waits_there(env):
    first,second=env.thread(),env.thread()
    issue=issues.create_issue(env.board,env.p['codex'],env.sid['codex'],title='Choice',body='Choose',thread_id=first)
    issues.link_issue(env.board,env.p['claude'],env.sid['claude'],issue['id'],second)
    env.board.set_thread_status(env.p['human'],second,'closed')
    issues.decide_issue(env.board,env.p['human'],env.sid['human'],issue['id'],'Both',[first,second])
    answers={p['thread_id']:p for p in env.board.conn.execute("SELECT * FROM posts WHERE agent='human'")}
    assert set(answers)=={first,second}
    assert env.board.get_thread(env.p['human'],second)['status']=='closed'   # answering does not reopen it


def test_human_may_answer_on_a_closed_thread_and_its_request_waits_for_a_reopen(env):
    """Closed-thread Needs you items: the human answers (not only dismisses); agents still cannot post or act
    there, and the answer's request becomes actionable when the human reopens the thread."""
    source=question(env,env.thread())
    tid=source['thread_id']
    env.board.set_thread_status(env.p['human'],tid,'closed')
    assert source['id'] in pending(env)
    linked=answer(env,source)
    assert linked['answer_to']==[source['id']] and linked['to']==['codex']
    assert source['id'] not in pending(env)
    with pytest.raises(Conflict,match='closed'):
        env.post('codex',tid,'Working on it')
    with pytest.raises(Conflict,match='closed'):
        requests.progress(env.board,env.p['codex'],env.sid['codex'],linked['id'],'codex','started')
    env.board.set_thread_status(env.p['human'],tid,'open')
    assert requests.progress(env.board,env.p['codex'],env.sid['codex'],linked['id'],'codex','started')['state']=='started'


def test_resolve_on_a_closed_thread_answers_without_approving_a_launch(env):
    source=question(env,env.thread())
    env.board.set_thread_status(env.p['human'],source['thread_id'],'closed')
    out=resolve.resolve(env.board,env.p['human'],source['id'],'approve',None,DispatchConfig())
    assert out['to']==['codex'] and source['id'] not in pending(env)
    assert env.board.list_dispatch_rules(env.p['human'])==[]
    with pytest.raises(Conflict,match='closed'):
        env.post('codex',source['thread_id'],'agents still cannot post')


def test_last_task_completion_reconciles_previously_finished_answer(env):
    source=question(env,env.thread())
    linked=answer(env,source)
    task=env.accepted_task(source['thread_id'])
    env.board.claim_task(env.p['codex'],env.sid['codex'],task)
    finish(env,linked)
    assert env.board.get_thread(env.p['human'],source['thread_id'])['status']=='open'
    env.board.transition_task(env.p['codex'],env.sid['codex'],task,'done',note='Verified last required task')
    assert env.board.get_thread(env.p['human'],source['thread_id'])['status']=='closed'


def test_identical_concurrent_issue_decision_creates_one_answer(env):
    import threading
    tid=env.thread()
    source=question(env,tid)
    issue=issues.create_issue(env.board,env.p['codex'],env.sid['codex'],title='Choice',body='Choose',thread_id=tid,post_id=source['id'])
    barrier=threading.Barrier(2)
    results,errors=[],[]
    def decide():
        barrier.wait()
        try:
            results.append(issues.decide_issue(env.board,env.p['human'],env.sid['human'],issue['id'],'Yes',[tid],
                                              expected_question_version=issue['question_version']))
        except Exception as exc:
            errors.append(exc)
        finally:
            env.board.conn.close()
    workers=[threading.Thread(target=decide) for _ in range(2)]
    for worker in workers: worker.start()
    for worker in workers: worker.join(timeout=5)
    assert not any(worker.is_alive() for worker in workers)
    assert not errors and len(results)==2
    assert env.board.conn.execute("SELECT COUNT(*) FROM issue_comments WHERE kind='decision'").fetchone()[0]==1
    assert env.board.conn.execute('SELECT COUNT(DISTINCT answer_post_id) FROM issue_answer_links').fetchone()[0]==1


@pytest.mark.parametrize('change',['body','outcome','question_version','new_source','scope'])
def test_changed_issue_decision_is_not_mistaken_for_retry(env,change):
    tid=env.thread()
    source=question(env,tid)
    issue=issues.create_issue(env.board,env.p['codex'],env.sid['codex'],title='Choice',body='Choose',thread_id=tid,post_id=source['id'])
    issues.decide_issue(env.board,env.p['human'],env.sid['human'],issue['id'],'Yes',[tid])
    body,outcome,scope='Yes','answered',[tid]
    if change=='body': body='Different instruction'
    if change=='outcome': outcome='approved'
    if change=='question_version':
        issues.comment_issue(env.board,env.p['codex'],env.sid['codex'],issue['id'],'Please reconsider',kind='request')
    if change=='new_source':
        other=question(env,tid,'claude')
        issues.link_issue(env.board,env.p['claude'],env.sid['claude'],issue['id'],tid,other['id'])
    if change=='scope':
        other_tid=env.thread()
        issues.link_issue(env.board,env.p['claude'],env.sid['claude'],issue['id'],other_tid)
        scope.append(other_tid)
    issues.decide_issue(env.board,env.p['human'],env.sid['human'],issue['id'],body,scope,outcome)
    assert env.board.conn.execute("SELECT COUNT(*) FROM issue_comments WHERE kind='decision'").fetchone()[0]==2


def test_exact_retry_after_completed_thread_does_not_reopen_or_duplicate(env):
    tid=env.thread()
    source=question(env,tid)
    issue=issues.create_issue(env.board,env.p['codex'],env.sid['codex'],title='Choice',body='Choose',thread_id=tid,post_id=source['id'])
    issues.decide_issue(env.board,env.p['human'],env.sid['human'],issue['id'],'Yes',[tid])
    answer_id=env.board.conn.execute('SELECT answer_post_id FROM issue_answer_links').fetchone()[0]
    finish(env,env.board.get_post(env.p['human'],answer_id))
    result=issues.decide_issue(env.board,env.p['human'],env.sid['human'],issue['id'],'Yes',[tid])
    assert result['status']=='resolved'
    assert env.board.get_thread(env.p['human'],tid)['status']=='closed'
    assert env.board.conn.execute('SELECT COUNT(*) FROM issue_answer_links').fetchone()[0]==1


def test_same_label_different_selected_option_is_not_a_retry(env):
    tid=env.thread()
    question_spec={'question':'Choose','recommended_option_id':'one',
                   'options':[{'id':'one','label':'Same label'},{'id':'two','label':'Same label'}]}
    issue=issues.create_issue(env.board,env.p['codex'],env.sid['codex'],title='Choice',body='Choose',thread_id=tid,
                              decision_question=question_spec)
    for option in ('one','one','two'):
        issues.decide_issue(env.board,env.p['human'],env.sid['human'],issue['id'],'',[tid],
                            selected_option_id=option,expected_question_version=issue['question_version'])
    assert env.board.conn.execute("SELECT COUNT(*) FROM issue_comments WHERE kind='decision'").fetchone()[0]==2


def test_only_latest_decision_can_match_a_retry(env):
    tid=env.thread()
    issue=issues.create_issue(env.board,env.p['codex'],env.sid['codex'],title='Choice',body='Choose',thread_id=tid)
    for body in ('First instruction','Second instruction','First instruction'):
        issues.decide_issue(env.board,env.p['human'],env.sid['human'],issue['id'],body,[tid])
    assert env.board.conn.execute("SELECT COUNT(*) FROM issue_comments WHERE kind='decision'").fetchone()[0]==3


def test_proposal_about_an_existing_task_needs_the_human_but_propose_task_does_not(env):
    tid = env.thread()
    created = env.post('codex', tid, 'Let me do this', 'proposal', propose_task={'title': 'New task'})
    task_id = env.accepted_task(tid, title='Existing task')
    about = env.post('codex', tid, 'Change the approach on the existing task?', 'proposal', task_id=task_id)
    to_agents = env.post('codex', tid, 'Between us', 'proposal', task_id=task_id, to=['claude'])
    # The second proposal on the propose_task task is not the one that created it.
    again = env.post('codex', tid, 'And on the new one too?', 'proposal', task_id=created['task_id'])
    assert pending(env) == {about['id'], again['id']}
    assert created['id'] not in pending(env) and to_agents['id'] not in pending(env)


def test_migration_records_which_proposal_created_its_task(env):
    tid = env.thread()
    created = env.post('codex', tid, 'Let me do this', 'proposal', propose_task={'title': 'New task'})
    env.clock.advance(60)
    about = env.post('codex', tid, 'And this?', 'proposal', task_id=created['task_id'])
    c = env.board.conn
    c.execute('ALTER TABLE tasks DROP COLUMN proposed_by_post')
    db.init_schema(c)
    assert c.execute('SELECT proposed_by_post FROM tasks WHERE id=?', (created['task_id'],)).fetchone()[0] == created['id']
    assert pending(env) == {about['id']}
