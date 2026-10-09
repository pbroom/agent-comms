"""Explicit proposal closeout must remain separate from task and request completion."""
import asyncio
import json

import pytest
from mcp import Client

from agent_comms import attention
from agent_comms.core import Forbidden
from agent_comms.mcp_server import build_mcp
from conftest import ASK


def pending(env):
    return {post['id'] for post in env.board.snapshot(env.p['human'])['needs_you']}


def completed_proposal(env):
    thread = env.thread()
    task = env.accepted_task(thread, title='Authorized repair')
    source = env.post('codex', thread, 'Implement the authorized repair', 'proposal',
                      task_id=task, needs_response=False, decision_question=ASK)
    neighbor = env.post('codex', thread, 'Also change the release policy?', 'proposal', task_id=task, decision_question=ASK)
    foreign = env.post('claude', thread, 'Change the deployment target?', 'proposal', task_id=task, decision_question=ASK)
    decision = env.post('codex', thread, 'Choose the next rollout', 'decision', task_id=task, decision_question=ASK)
    env.board.claim_task(env.p['codex'], env.sid['codex'], task)
    env.board.transition_task(env.p['codex'], env.sid['codex'], task, 'done')
    proof = env.post('codex', thread, 'Exact authorized repair merged and activated; policy unchanged',
                     task_id=task)
    return thread, task, source, neighbor, foreign, decision, proof


def test_task_done_and_later_evidence_leave_proposals_until_exact_closeout(env):
    thread, task, source, neighbor, foreign, decision, proof = completed_proposal(env)
    assert pending(env) == {p['id'] for p in (source, neighbor, foreign, decision)}
    task_before = env.board.get_task(env.p['human'], task)
    posts_before = env.board.list_posts(env.p['human'], thread)['posts']
    result = attention.close_attention(env.board, env.p['codex'], env.sid['codex'], source['id'],
                                       'Previously authorized repair verified complete; no new decision.',
                                       [proof['id']])
    assert pending(env) == {p['id'] for p in (neighbor, foreign, decision)}
    assert result['attention_resolution'] == {
        'resolved_by': 'codex', 'session_id': env.sid['codex'],
        'reason': 'Previously authorized repair verified complete; no new decision.',
        'evidence_post_ids': [proof['id']], 'resolved_at': result['revised_at'],
    }
    for field in ('id', 'body', 'type', 'agent', 'session_id', 'task_id', 'needs_response', 'created_at'):
        assert result[field] == source[field]
    assert env.board.get_task(env.p['human'], task) == task_before
    posts_after = env.board.list_posts(env.p['human'], thread)['posts']
    assert len(posts_after) == len(posts_before)
    assert [p for p in posts_after if p['id'] != source['id']] == [
        p for p in posts_before if p['id'] != source['id']]
    assert env.board._thread_row(thread)['status'] == 'open'


def test_completed_task_does_not_authorize_foreign_proposal_or_human_decision_closeout(env):
    _, _, source, neighbor, foreign, decision, proof = completed_proposal(env)
    for post, error in ((foreign, 'own authored'), (decision, 'decision')):
        with pytest.raises(Forbidden, match=error):
            attention.close_attention(env.board, env.p['codex'], env.sid['codex'], post['id'],
                                       'Task was completed', [proof['id']])
    assert pending(env) == {p['id'] for p in (source, neighbor, foreign, decision)}


def test_fresh_mcp_session_closes_only_authored_proposal_with_exact_evidence(env, monkeypatch):
    _, task, source, neighbor, foreign, decision, proof = completed_proposal(env)
    monkeypatch.setenv('AGENT_COMMS_TOKEN', env.tokens['codex'])

    async def run():
        async with Client(build_mcp(env.board, 'stdio')) as client:
            registered = await client.call_tool('board_register', {'project': '/work/repo'})
            session = json.loads(registered.content[0].text)['session_id']
            result = await client.call_tool('board_resolve_attention', {
                'post_id': source['id'], 'reason': 'Authorized repair verified merged and activated.',
                'evidence_post_ids': [proof['id']],
            })
            return session, result

    session, response = asyncio.run(run())
    assert not response.is_error
    result = json.loads(response.content[0].text)
    assert session != env.sid['codex']
    assert result['attention_resolution']['session_id'] == session
    assert result['attention_resolution']['resolved_by'] == 'codex'
    assert result['attention_resolution']['evidence_post_ids'] == [proof['id']]
    assert result['task_id'] == task and result['type'] == 'proposal'
    assert pending(env) == {p['id'] for p in (neighbor, foreign, decision)}
