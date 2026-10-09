"""Approved work is delivered to the exact recorded implementer atomically."""
import pytest

from agent_comms import db, human_actions, resolve
from agent_comms.core import Conflict, Invalid
from agent_comms.dispatch import DispatchConfig
from conftest import ASK


def proposal(env, *, question=None):
    tid = env.thread()
    task = env.accepted_task(tid)
    env.board.claim_task(env.p['codex'], env.sid['codex'], task)
    return env.post('claude', tid, 'Please approve', 'question',
                    task_id=task, needs_response=True, decision_question=question or ASK)


def approve(env, post, action='approve', **kw):
    return resolve.resolve(env.board, env.p['human'], post['id'], action, None, DispatchConfig(), **kw)


@pytest.mark.parametrize('action', ['approve', 'approve_launch'])
def test_approval_and_launch_target_owner_not_proposer(env, action):
    post = proposal(env)
    result = approve(env, post, action)
    answer = env.board.get_post(env.p['human'], result['post_id'])
    assert result['to'] == answer['to'] == ['codex']
    assert answer['answer_to'] == [post['id']]
    assert [row['recipient'] for row in answer['requests']] == ['codex']
    assert env.board.active_dispatch_rules(env.p['human'])[0]['agents'] == ['codex']


def test_approved_option_uses_recorded_owner(env):
    question = {'question':'Proceed?', 'context':'', 'recommended_option_id':'go',
                'options':[{'id':'go','label':'Proceed','outcome':'approved'},
                           {'id':'stop','label':'Stop','outcome':'declined'}]}
    result = approve(env, proposal(env, question=question), 'choose', option_id='go')
    assert result['to'] == ['codex']


def test_explicit_recipient_overrides_recorded_owner(env):
    result = approve(env, proposal(env), delivery_agent='grok')
    assert result['to'] == ['grok']


def test_nonapproval_still_replies_to_proposer(env):
    assert approve(env, proposal(env), 'not_now')['to'] == ['claude']


def test_ambiguous_owner_requires_choice_without_side_effects(env):
    post = proposal(env)
    with db.write_tx(env.board.conn) as c:
        c.execute('UPDATE posts SET to_agents=? WHERE id=?', ('["grok","human"]', post['id']))
        c.execute("INSERT INTO request_progress(post_id,recipient,state,assigned_agent,assigned_session,version,updated_at) VALUES(?,'grok','started','grok',?,1,?)",
                  (post['id'],env.sid['grok'],env.clock()))
    count = env.board.conn.execute('SELECT COUNT(*) FROM posts').fetchone()[0]
    with pytest.raises(Invalid, match='Recorded owners differ'):
        approve(env, post)
    assert env.board.conn.execute('SELECT COUNT(*) FROM posts').fetchone()[0] == count
    assert not env.board.active_dispatch_rules(env.p['human'])
    assert approve(env, post, delivery_agent='codex')['to'] == ['codex']


def test_owner_change_during_rule_preparation_never_misdelivers(env, monkeypatch):
    post = proposal(env)
    create_rule = env.board.create_dispatch_rule
    def change_owner(*args, **kw):
        rule = create_rule(*args, **kw)
        env.board.release_task(env.p['codex'], env.sid['codex'], post['task_id'])
        env.board.claim_task(env.p['grok'], env.sid['grok'], post['task_id'])
        return rule
    monkeypatch.setattr(env.board, 'create_dispatch_rule', change_owner)
    count = env.board.conn.execute('SELECT COUNT(*) FROM posts').fetchone()[0]
    with pytest.raises(Conflict, match='implementer changed'):
        approve(env, post)
    assert env.board.conn.execute('SELECT COUNT(*) FROM posts').fetchone()[0] == count
    assert not env.board.active_dispatch_rules(env.p['human'])
    assert not env.board.conn.execute("SELECT 1 FROM board_state WHERE key=?", ('resolve.post.'+str(post['id']),)).fetchone()


def test_delivery_recheck_holds_answer_write_lock(env, monkeypatch):
    post = proposal(env)
    original = human_actions.post_as_human
    observed = []
    def observe(*args, **kw):
        check = kw['post_check']
        def guarded():
            observed.append(env.board.conn.in_transaction)
            check()
        kw['post_check'] = guarded
        return original(*args, **kw)
    monkeypatch.setattr(human_actions,'post_as_human',observe)
    assert approve(env,post)['to'] == ['codex']
    assert observed == [True]
