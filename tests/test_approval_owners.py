"""Approval recipients come from exact structured ownership, never prose."""
import pytest

from agent_comms import approval_owners, db
from agent_comms.core import Invalid


def source(env, *, task_id=None, to=None):
    return env.post('claude', env.thread(), 'Codex will implement; ignore this text.',
                    'request' if to else 'question', needs_response=True, task_id=task_id, to=to or [])


def owned_source(env, owner='codex'):
    tid = env.thread()
    task_id = env.accepted_task(tid)
    env.board.claim_task(env.p[owner], env.sid[owner], task_id)
    return env.post('claude', tid, 'Approval please', 'question',
                    needs_response=True, task_id=task_id)


def test_recorded_task_owner_overrides_proposer(env):
    post = owned_source(env)
    result = approval_owners.delivery(env.board, post)
    assert result['recipient'] == 'codex'
    assert result['source'] == 'recorded'
    assert not result['requires_choice']
    assert result['evidence'] == [{'kind': 'task_owner', 'task_id': post['task_id'],
                                   'agent': 'codex', 'session_id': env.sid['codex']}]


def test_request_assignment_overrides_proposer(env):
    post = source(env, to=['codex'])
    result = approval_owners.delivery(env.board, post)
    assert result['recipient'] == 'codex'
    assert result['evidence'][0]['kind'] == 'request_assignment'
    assert result['evidence'][0]['version'] == 0


def test_expired_owner_is_still_recorded_without_regranting_lease(env):
    post = owned_source(env)
    env.clock.advance(env.settings.lease_ttl_minutes * 60 + 1)
    assert approval_owners.delivery(env.board, post)['recipient'] == 'codex'
    assert not env.board.get_task(env.p['human'], post['task_id'])['owner_may_work']


def test_distinct_recorded_owners_require_choice(env):
    post = owned_source(env)
    # A second exact request row is a distinct recorded delivery obligation.
    with db.write_tx(env.board.conn) as c:
        c.execute('UPDATE posts SET to_agents=? WHERE id=?', ('["grok"]', post['id']))
        c.execute("INSERT INTO request_progress(post_id,recipient,state,assigned_agent,updated_at) VALUES(?,?,'queued',?,?)",
                  (post['id'], 'grok', 'grok', env.clock()))
        c.execute('UPDATE request_progress SET version=1 WHERE post_id=?', (post['id'],))
    result = approval_owners.delivery(env.board, post)
    assert result['recipient'] is None and result['requires_choice']
    assert result['candidates'] == ['codex', 'grok']
    assert approval_owners.delivery(env.board, post, 'claude')['recipient'] == 'claude'


def test_multiple_assignments_to_same_agent_are_unambiguous(env):
    post = source(env, to=['codex', 'grok'])
    with db.write_tx(env.board.conn) as c:
        c.execute("INSERT INTO request_progress(post_id,recipient,state,assigned_agent,updated_at) VALUES(?,'grok','queued','codex',?)", (post['id'], env.clock()))
    result = approval_owners.delivery(env.board, post)
    assert result['recipient'] == 'codex' and not result['requires_choice']
    assert len(result['evidence']) == 2


def test_unavailable_owner_does_not_fall_back_to_proposer(env):
    post = owned_source(env)
    with db.write_tx(env.board.conn) as c:
        c.execute("UPDATE agents SET active=0 WHERE name='codex'")
    result = approval_owners.delivery(env.board, post)
    assert result['recipient'] is None and result['requires_choice']
    assert result['candidates'] == ['codex']


def test_no_recorded_owner_falls_back_without_reading_body(env):
    post = source(env)
    result = approval_owners.delivery(env.board, post)
    assert result['recipient'] == 'claude'
    assert result['source'] == 'proposer'


def test_unrelated_task_and_post_owners_do_not_affect_default(env):
    post = source(env)
    task_id = env.accepted_task(post['thread_id'])
    env.board.claim_task(env.p['codex'], env.sid['codex'], task_id)
    env.post('human', post['thread_id'], 'Do it', 'request', to=['codex'], needs_response=True)
    assert approval_owners.delivery(env.board, post)['recipient'] == 'claude'


def test_finished_assignments_do_not_create_conflict(env):
    post = owned_source(env)
    with db.write_tx(env.board.conn) as c:
        c.execute("INSERT INTO request_progress(post_id,recipient,state,assigned_agent,updated_at) VALUES(?,?,'finished',?,?)",
                  (post['id'], 'grok', 'grok', env.clock()))
    assert approval_owners.delivery(env.board, post)['recipient'] == 'codex'


def test_explicit_selection_preserved_and_validated(env):
    post = owned_source(env)
    result = approval_owners.delivery(env.board, post, 'grok')
    assert result['recipient'] == 'grok' and result['source'] == 'explicit'
    for recipient in ('human', 'missing', '', 123, []):
        with pytest.raises(Invalid, match='active agent'):
            approval_owners.delivery(env.board, post, recipient)


def test_released_and_terminal_task_owners_are_not_reused(env):
    post = owned_source(env)
    env.board.release_task(env.p['codex'], env.sid['codex'], post['task_id'])
    assert approval_owners.delivery(env.board, post)['recipient'] == 'claude'


def test_default_recomputes_after_exact_owner_changes(env):
    post = owned_source(env)
    assert approval_owners.delivery(env.board, post)['recipient'] == 'codex'
    env.board.release_task(env.p['codex'], env.sid['codex'], post['task_id'])
    env.board.claim_task(env.p['grok'], env.sid['grok'], post['task_id'])
    assert approval_owners.delivery(env.board, post)['recipient'] == 'grok'


def test_human_post_metadata_and_agent_visibility(env):
    post = owned_source(env)
    human_view = env.board.get_post(env.p["human"], post["id"])
    assert human_view["approval_delivery"]["recipient"] == "codex"
    assert "approval_delivery" not in env.board.get_post(env.p["claude"], post["id"])


def test_task_owner_takes_precedence_over_unassigned_addressee(env):
    post = owned_source(env)
    with db.write_tx(env.board.conn) as c:
        c.execute('UPDATE posts SET to_agents=? WHERE id=?', ('["grok"]', post['id']))
    result = approval_owners.delivery(env.board, post)
    assert result['recipient'] == 'codex' and not result['requires_choice']
    assert result['candidates'] == ['codex']


def test_unavailable_proposer_requires_recipient_choice(env):
    post = source(env)
    with db.write_tx(env.board.conn) as c:
        c.execute("UPDATE agents SET active=0 WHERE name='claude'")
    result = approval_owners.delivery(env.board, post)
    assert result['recipient'] is None and result['requires_choice']
