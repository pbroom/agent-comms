"""Shared approvals deliver each covered source only to its recorded implementer."""
import pytest
from fastapi.testclient import TestClient

from agent_comms import approval_owners, db, issues
from agent_comms.api import create_app
from agent_comms.core import Invalid


def source(env, tid, owner='codex', question=None):
    task = env.accepted_task(tid)
    env.board.claim_task(env.p[owner], env.sid[owner], task)
    return env.post('claude', tid, 'Please approve', 'question', task_id=task,
                    needs_response=True, decision_question=question)


def issue_for(env, *posts):
    first = posts[0]
    result = issues.create_issue(env.board, env.p['claude'], env.sid['claude'],
        title='Approve work', body='Bounded scope', thread_id=first['thread_id'], post_id=first['id'])
    for post in posts[1:]:
        issues.link_issue(env.board, env.p['claude'], env.sid['claude'], result['id'],
                          post['thread_id'], post['id'])
    return result


def decide(env, issue, tids, **kw):
    return issues.decide_issue(env.board, env.p['human'], env.sid['human'], issue['id'],
                               'Proceed', tids, outcome=kw.pop('outcome','approved'), **kw)


def answers(env):
    return [env.board.get_post(env.p['human'], row['id']) for row in env.board.conn.execute(
        "SELECT id FROM posts WHERE agent='human' ORDER BY id")]


def test_covered_sources_route_separately_to_actual_owners(env):
    tid = env.thread()
    a, b = source(env,tid), source(env,tid,'grok')
    issue = issue_for(env,a,b)
    decided = decide(env,issue,[tid])
    actual = answers(env)
    assert [(p['to'],p['answer_to']) for p in actual] == [(['codex'],[a['id']]),(['grok'],[b['id']])]
    assert all([r['recipient'] for r in p['requests']] == p['to'] for p in actual)
    mappings = env.board.conn.execute('''SELECT l.post_id,a.answer_post_id FROM issue_answer_links a
        JOIN issue_links l ON l.id=a.issue_link_id ORDER BY l.id''').fetchall()
    assert [tuple(row) for row in mappings] == [(a['id'],actual[0]['id']), (b['id'],actual[1]['id'])]
    assert not decided['needs_human']
    assert env.board.get_task(env.p['human'],a['task_id'])['status'] == 'working'


def test_same_owner_sources_share_one_exact_answer(env):
    tid = env.thread()
    a,b = source(env,tid),source(env,tid)
    issue = issue_for(env,a,b)
    decide(env,issue,[tid])
    actual=answers(env)
    assert len(actual)==1 and actual[0]['answer_to']==[a['id'],b['id']]
    assert actual[0]['to']==['codex']
    # A retry after ownership changes must not duplicate an accepted decision.
    env.board.release_task(env.p['codex'],env.sid['codex'],a['task_id'])
    decide(env,issue,[tid])
    assert len(answers(env))==1


def test_ambiguity_rolls_back_every_source_until_explicit_choice(env):
    tid=env.thread()
    a,b=source(env,tid),source(env,tid)
    issue=issue_for(env,a,b)
    with db.write_tx(env.board.conn) as c:
        c.execute('UPDATE posts SET to_agents=? WHERE id=?', ('["grok","human"]', b['id']))
        c.execute("INSERT INTO request_progress(post_id,recipient,state,assigned_agent,assigned_session,version,updated_at) VALUES(?,'grok','started','grok',?,1,?)",
                  (b['id'],env.sid['grok'],env.clock()))
    before=env.board.conn.execute('SELECT COUNT(*) FROM issue_comments').fetchone()[0]
    with pytest.raises(Invalid,match='Recorded owners differ'):
        decide(env,issue,[tid])
    assert answers(env)==[]
    assert env.board.conn.execute('SELECT COUNT(*) FROM issue_comments').fetchone()[0]==before
    assert issues.get_issue(env.board,env.p['human'],issue['id'])['needs_human']
    result=decide(env,issue,[tid],delivery_agents={str(b['id']):'grok'})
    assert [p['to'] for p in answers(env)]==[['codex'],['grok']]
    assert result['decisions'][-1]['delivery_agents']=={str(b['id']):'grok'}


def test_override_cannot_expand_selected_scope(env):
    t1,t2=env.thread(),env.thread()
    a,b=source(env,t1),source(env,t2)
    issue=issue_for(env,a,b)
    with pytest.raises(Invalid,match='approved scope'):
        decide(env,issue,[t1],delivery_agents={str(b['id']):'grok'})
    with pytest.raises(Invalid,match='approved scope'):
        decide(env,issue,[t1],outcome='declined',delivery_agents={str(a['id']):'grok'})
    assert answers(env)==[]
    decide(env,issue,[t1],delivery_agents={str(a['id']):'grok'})
    assert answers(env)[0]['to']==['grok']
    assert issues.get_issue(env.board,env.p['human'],issue['id'])['needs_human']


@pytest.mark.parametrize('mapping',[{'0':'codex'},{'01':'codex'},{'1':'human'},{'1':'missing'},[],{1:'codex'}])
def test_invalid_override_rejected_without_answer(env,mapping):
    tid=env.thread();a=source(env,tid);issue=issue_for(env,a)
    with pytest.raises(Invalid):
        decide(env,issue,[tid],delivery_agents=mapping)
    assert answers(env)==[]


def test_uncovered_question_keeps_original_attention_and_notification(env):
    tid=env.thread()
    a=source(env,tid)
    question={'question':'Separate?', 'context':'','recommended_option_id':'yes',
              'options':[{'id':'yes','label':'Yes'},{'id':'no','label':'No'}]}
    separate=source(env,tid,'grok',question)
    issue=issue_for(env,a,separate)
    decide(env,issue,[tid])
    actual=answers(env)
    assert [(p['to'],p['answer_to']) for p in actual]==[(['codex'],[a['id']]),(['claude'],[])]
    assert separate['id'] in [p['id'] for p in env.board.snapshot(env.p['human'])['needs_you']]


def test_declined_response_still_goes_to_proposer(env):
    tid=env.thread();a=source(env,tid);issue=issue_for(env,a)
    decide(env,issue,[tid],outcome='declined')
    assert answers(env)[0]['to']==['claude']


def test_owner_lookup_is_inside_issue_write_transaction(env,monkeypatch):
    tid=env.thread();a=source(env,tid);issue=issue_for(env,a)
    original=approval_owners.delivery
    observed=[]
    def check(board,post,explicit_recipient=None):
        if post=={'id':a['id']}:
            observed.append(board.conn.in_transaction)
        return original(board,post,explicit_recipient)
    monkeypatch.setattr(approval_owners,'delivery',check)
    decide(env,issue,[tid])
    assert observed==[True]


def test_api_override_and_human_only_link_metadata(env):
    tid=env.thread();a=source(env,tid);issue=issue_for(env,a)
    human=issues.get_issue(env.board,env.p['human'],issue['id'])
    assert human['links'][0]['source_post']['approval_delivery']['recipient']=='codex'
    assert 'source_post' not in issues.get_issue(env.board,env.p['claude'],issue['id'])['links'][0]
    client=TestClient(create_app(env.board))
    response=client.post(f"/api/issues/{issue['id']}/decisions",
        headers={'Authorization':f"Bearer {env.tokens['human']}"},
        json={'thread_ids':[tid],'body':'Proceed','outcome':'approved','delivery_agents':{str(a['id']):'grok'}})
    assert response.status_code==200,response.text
    assert answers(env)[0]['to']==['grok']
