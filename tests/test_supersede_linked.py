"""An agent retires its own obsolete linked request through request_reply (DESIGN_NOTES "Superseding linked
requests"): human answer lineage, routed successors and recovery acknowledgements. Only that exact row finishes; the
human's answer links, attention records and other rows stay as they are; the board's automatic machinery stops acting
for it. Managed continuations and shared-issue posts keep refusing and name their own workflow."""
import json
import subprocess

import pytest

from agent_comms import autorecover, capabilities, db, issues, obligations, recovery, request_replies, requests, unstick
from agent_comms.core import Board, Conflict, Forbidden
from conftest import ASK, PROJECT
from test_autorecover import aenv  # noqa: F401
from test_decision_actions import choose, proposal
from test_delivery import WINDOW, alerts, denv  # noqa: F401
from test_delivery import needs_you as delivery_needs_you
from test_recovery_wait import auto_posts, blocked, blocked_handoff, go_quiet, wait_of
from test_workstreams import create as create_continuation, current as continuation_row, stack  # noqa: F401

REASON = 'Obsolete: the requested work shipped weeks ago'


def row(e, post, recipient='codex'):
    return next(r for r in e.board.get_post(e.p['human'], post['id'])['requests'] if r['recipient'] == recipient)


def supersede(e, tid, post, agent='codex', recipient='codex', key='obsolete-1', session_id=None, version=None):
    envelope = dict(post_id=post['id'], recipient=recipient, state='finished', reason=REASON, disposition='superseded',
                    expected_version=row(e, post, recipient)['version'] if version is None else version)
    return e.board.create_post(e.p[agent], session_id or e.sid[agent], thread_id=tid, type='status',
                               body='Retiring as obsolete: ' + REASON, idempotency_key=key, request_reply=envelope)


def human_records(e):
    """Everything that says the human answered or handled something: none of it may change."""
    c = e.board.conn
    return {t: [tuple(r) for r in c.execute(f'SELECT * FROM {t} ORDER BY rowid')]
            for t in ('answer_links', 'attention_resolutions', 'legacy_attention_answers', 'issue_links',
                      'issue_answer_links')}


def needs_you(e):
    return [r[0] for r in e.board.conn.execute(f'SELECT p.id FROM posts p WHERE {Board.NEEDS_YOU} ORDER BY p.id')]


def post_count(e):
    return e.board.conn.execute('SELECT COUNT(*) FROM posts').fetchone()[0]


# ---------------------------------------------------------------- human answer lineage


def test_superseding_a_human_answer_retires_only_that_row_and_keeps_the_answer(env):
    tid = env.thread()
    question = env.post('codex', tid, 'Ship the old plan?', 'question', needs_response=True, decision_question=ASK)
    answer = env.post('human', tid, 'Yes, ship it', 'status', to=['codex', 'claude'], answer_to=[question['id']])
    before, attention = human_records(env), needs_you(env)
    reply = supersede(env, tid, answer)
    done = row(env, answer)
    assert (done['state'], done['disposition'], done['evidence_post_ids']) == ('finished', 'superseded', [reply['id']])
    assert row(env, answer, 'claude')['state'] == 'queued'
    assert human_records(env) == before, 'answer links and attention records are untouched'
    assert needs_you(env) == attention, 'the question stays answered and nothing new asks the human'
    assert env.board.get_post(env.p['human'], answer['id'])['body'] == 'Yes, ship it'
    # The reply post with its reason is the audit record, and superseded never counts as completion.
    assert env.board.get_post(env.p['human'], reply['id'])['body'] == 'Retiring as obsolete: ' + REASON
    assert requests.history(env.board, env.p['codex'], answer['id'], 'codex')[-1]['reason'] == REASON
    # Claude completes its row with evidence: everything else in the thread is settled, and the superseded row alone
    # keeps the board from inferring that the answered work is done.
    proof = env.post('claude', tid, 'Shipped and verified', 'status')
    requests.progress(env.board, env.p['claude'], env.sid['claude'], answer['id'], 'claude', 'finished', 'done',
                      [proof['id']], terminal_disposition='completed')
    assert not issues._completed_answer_work(env.board, tid)
    assert env.board._thread_row(tid)['status'] == 'open'


def test_superseding_a_request_the_human_later_answered(env):
    tid = env.thread()
    ask = env.post('human', tid, 'old objective', 'request', to=['codex'])
    follow = env.post('human', tid, 'about that objective', 'status', to=['codex'], answer_to=[ask['id']])
    before = human_records(env)
    supersede(env, tid, ask)
    assert row(env, ask)['disposition'] == 'superseded'
    assert row(env, follow)['state'] == 'queued', 'the answer is its own obligation'
    assert human_records(env) == before


def test_only_the_assigned_agent_may_supersede_a_linked_request(env):
    tid = env.thread()
    ask = env.post('codex', tid, 'please review', 'request', to=['claude'])
    env.post('human', tid, 'go ahead', 'status', to=['claude', 'codex'], answer_to=[ask['id']])
    before = post_count(env)
    # Its author may still cancel an unlinked request, but not one in the human's answer lineage.
    with pytest.raises(Forbidden, match='only its assigned agent'):
        supersede(env, tid, ask, agent='codex', recipient='claude')
    with pytest.raises(Forbidden, match='only its assigned agent'):
        requests.progress(env.board, env.p['human'], env.sid['human'], ask['id'], 'claude', 'finished', REASON,
                          terminal_disposition='superseded')
    # An agent the request never named cannot either.
    with pytest.raises(Forbidden):
        supersede(env, tid, ask, agent='grok', recipient='claude')
    assert post_count(env) == before and row(env, ask, 'claude')['state'] == 'queued'
    supersede(env, tid, ask, agent='claude', recipient='claude')
    assert row(env, ask, 'claude')['disposition'] == 'superseded'


def routed_to_author(env):
    """codex asks claude and itself; the human answers; claude's row is then routed to a codex session, so the post's
    author becomes the assignee of another recipient's linked row."""
    tid = env.thread()
    ask = env.post('codex', tid, 'please review', 'request', to=['claude', 'codex'])
    env.post('human', tid, 'go ahead', 'status', to=['claude', 'codex'], answer_to=[ask['id']])
    target = env.session('codex')
    capabilities.register(env.board, env.p['codex'], target, ['git:write'], 'Checked git write access')
    requests.assign(env.board, env.p['human'], env.sid['human'], ask['id'], 'claude', target, 0, 'route',
                    ['git:write'])
    assert (row(env, ask, 'claude')['assigned_agent'], row(env, ask, 'claude')['assigned_session']) == ('codex', target)
    return tid, ask, target


@pytest.mark.parametrize('session', ['assigned', 'other'])
def test_the_author_cannot_supersede_a_linked_row_routed_to_it(env, session):
    tid, ask, target = routed_to_author(env)
    sid = target if session == 'assigned' else env.sid['codex']
    before = post_count(env)
    with pytest.raises(Forbidden, match="not the human or the post's author"):
        supersede(env, tid, ask, agent='codex', recipient='claude', session_id=sid)
    with pytest.raises(Forbidden, match="not the human or the post's author"):
        requests.progress(env.board, env.p['codex'], sid, ask['id'], 'claude', 'finished', REASON,
                          terminal_disposition='superseded')
    assert post_count(env) == before and row(env, ask, 'claude')['state'] == 'queued'


def test_the_human_cannot_supersede_a_linked_row_assigned_to_it(env):
    tid = env.thread()
    ask = env.post('codex', tid, 'please review', 'request', to=['claude'])
    env.post('human', tid, 'go ahead', 'status', to=['claude', 'codex'], answer_to=[ask['id']])
    # Whatever routed it there, a human assignee (not the author here) never retires a linked row.
    env.board.conn.execute("INSERT INTO request_progress(post_id,recipient,state,assigned_agent,assigned_session,"
                           "reason,evidence_post_ids,version,updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                           (ask['id'], 'claude', 'queued', 'human', None, 'routed', '[]', 1, env.clock()))
    with pytest.raises(Forbidden, match="not the human"):
        requests.progress(env.board, env.p['human'], env.sid['human'], ask['id'], 'claude', 'finished', REASON,
                          terminal_disposition='superseded')
    assert row(env, ask, 'claude')['state'] == 'queued'


def test_a_failed_recovery_settlement_rolls_back_the_whole_supersede(env):
    import sqlite3
    tid, (source,), ack = recovery_setup(env, count=1)
    link, before = link_of(env, ack), post_count(env)
    env.board.conn.execute("CREATE TRIGGER fail_link BEFORE UPDATE ON board_state WHEN OLD.key LIKE 'request.recovery.%' "
                           "BEGIN SELECT RAISE(ABORT,'link unavailable'); END")
    with pytest.raises(sqlite3.IntegrityError, match='link unavailable'):
        supersede(env, tid, source)
    assert row(env, source)['state'] == 'queued' and row(env, source)['version'] == 0
    assert requests.history(env.board, env.p['codex'], source['id'], 'codex') == []
    assert post_count(env) == before and link_of(env, ack) == link
    assert env.board.conn.execute('SELECT COUNT(*) FROM request_reply_operations').fetchone()[0] == 0
    env.board.conn.execute('DROP TRIGGER fail_link')
    supersede(env, tid, source)   # the same key, once the settlement can be written
    assert row(env, source)['disposition'] == 'superseded' and link_of(env, ack)['sources'][0]['superseded']


def test_an_unlinked_request_keeps_the_existing_author_rule(env):
    tid = env.thread()
    ask = env.post('codex', tid, 'please review', 'request', to=['claude'])
    supersede(env, tid, ask, agent='codex', recipient='claude')
    assert row(env, ask, 'claude')['disposition'] == 'superseded'


def test_a_retried_supersede_is_idempotent_and_a_changed_one_conflicts(env):
    tid = env.thread()
    question = env.post('codex', tid, 'Ship?', 'question', needs_response=True, decision_question=ASK)
    answer = env.post('human', tid, 'Yes', 'status', to=['codex'], answer_to=[question['id']])
    first = supersede(env, tid, answer, version=0)
    again = supersede(env, tid, answer, version=0)
    assert again['id'] == first['id'] and again['request_reply'] == first['request_reply']
    assert len(requests.history(env.board, env.p['codex'], answer['id'], 'codex')) == 1
    with pytest.raises(Conflict):
        supersede(env, tid, answer, version=1)
    with pytest.raises(Conflict, match='already terminal'):
        supersede(env, tid, answer, key='obsolete-2')


# ---------------------------------------------------------------- recovery acknowledgements


def link_of(e, ack):
    return json.loads(e.board.conn.execute('SELECT value FROM board_state WHERE key=?',
                                           (recovery.PREFIX + f"{ack['id']}.codex",)).fetchone()[0])


def recovery_setup(e, count=2, diagnostics=False):
    tid = e.thread()
    originals = [e.post('human', tid, 'stalled objective', 'request', to=['codex']) for _ in range(count)]
    ack = e.post('human', tid, 'Unstick: recover stalled work', 'request', to=['codex'])
    with db.write_tx(e.board.conn):
        recovery.record(e.board, e.p['human'], ack['id'], 'codex',
                        [{'post_id': p['id'], 'recipient': 'codex', 'version': 0} for p in originals],
                        requires_diagnostics=diagnostics)
    return tid, originals, ack


def test_superseding_a_recovered_request_after_ownership_recovery(env, tmp_path):
    """The live case: an Unstick linked the request, its old dispatcher session ended, the agent recovered ownership
    (board_recover_request_owner), and the work turned out obsolete."""
    project = str(tmp_path / 'repo')
    subprocess.run(['git', 'init', project], check=True, capture_output=True)
    old, new = env.session('codex', project=project), env.session('codex', project=project)
    tid = env.board.create_thread(env.p['human'], env.sid['human'], 'stalled', project)['id']
    source = env.post('human', tid, 'stalled objective', 'request', to=['codex'])
    requests.progress(env.board, env.p['codex'], old, source['id'], 'codex', 'blocked', 'runner ended')
    env.board.conn.execute('UPDATE sessions SET dispatch_run_id=? WHERE id=?', ('ended', old))
    env.board.conn.execute('INSERT INTO board_state(key,value,updated_by,updated_at) VALUES (?,?,?,?)',
        ('dispatch.run.ended', json.dumps({'agent': 'codex', 'thread_id': tid, 'status': 'exited',
                                           'ended_at': env.clock()}), 'codex', env.clock()))
    env.board.conn.execute('INSERT INTO session_activity(session_id,state,recorded_at) VALUES (?,?,?)',
                           (new, 'idle', env.clock()))
    ack = env.post('human', tid, 'Unstick: recover stalled work', 'request', to=['codex'])
    with db.write_tx(env.board.conn):
        recovery.record(env.board, env.p['human'], ack['id'], 'codex',
                        [{'post_id': source['id'], 'recipient': 'codex', 'version': 1}], requires_diagnostics=True)
    recovery.transfer_ended_owner(env.board, env.p['codex'], new, source['id'], 'codex', 1)
    assert link_of(env, ack)['sources'][0]['version'] == 2
    with pytest.raises(Conflict, match='another session'):
        supersede(env, tid, source, session_id=old, key='stale-owner')
    reply = supersede(env, tid, source, session_id=new)
    assert (row(env, source)['disposition'], row(env, source)['assigned_session']) == ('superseded', new)
    assert link_of(env, ack)['sources'][0]['superseded'] == {'session_id': new, 'evidence_post_ids': [reply['id']]}
    assert row(env, ack)['state'] == 'queued', 'the acknowledgement is its own obligation'
    assert not link_of(env, ack).get('retired')
    # The acknowledgement itself is obsolete too: retiring it retires its link, with no receipt of its own.
    before = post_count(env)
    supersede(env, tid, ack, session_id=new, key='obsolete-ack')
    assert row(env, ack)['disposition'] == 'superseded'
    assert link_of(env, ack)['retired']['superseded'] is True
    assert post_count(env) == before + 1, 'only the reply post'


def test_a_superseded_source_counts_as_settled_for_recovery_retirement(env):
    tid, (gone, live), ack = recovery_setup(env)
    supersede(env, tid, gone)
    assert row(env, ack)['state'] == 'queued'
    requests.progress(env.board, env.p['codex'], env.sid['codex'], live['id'], 'codex', 'started')
    assert row(env, ack)['state'] == 'finished', 'the remaining pickup retires the acknowledgement'
    receipt = env.board.get_post(env.p['human'], row(env, ack)['evidence_post_ids'][0])
    assert (f"retired: verified pickup of 1 linked request(s) (#{live['id']}/codex); 1 superseded as obsolete by their "
            f"recipient (#{gone['id']}/codex)") in receipt['body']
    assert row(env, ack)['reason'] == (f"Obsolete recovery acknowledgement; verified pickup of 1 (#{live['id']}/codex), "
                                       f"1 superseded (#{gone['id']}/codex)")
    assert row(env, gone)['disposition'] == 'superseded' and row(env, live)['state'] == 'started'


@pytest.mark.parametrize('limit,count', [(256, 20), (700, 18), (4096, 12)])
def test_a_mixed_retirement_receipt_fits_the_configured_body_limit(tmp_path, limit, count):
    """Many linked sources at a supported low limit: the final pickup still retires the acknowledgement, with counts
    and as many ids as fit; the recovery record keeps every source's exact outcome."""
    from conftest import make_env
    env = make_env(tmp_path, body_max_bytes=limit)
    tid, sources, ack = recovery_setup(env, count=count)
    supersede(env, tid, sources[0])
    for source in sources[1:]:
        requests.progress(env.board, env.p['codex'], env.sid['codex'], source['id'], 'codex', 'started')
    assert row(env, ack)['state'] == 'finished'
    receipt = env.board.get_post(env.p['human'], row(env, ack)['evidence_post_ids'][0])
    assert len(receipt['body'].encode()) <= limit and len(row(env, ack)['reason'].encode()) <= limit
    assert f'verified pickup of {count - 1} linked request(s)' in receipt['body']
    assert '1 superseded as obsolete' in receipt['body']
    if limit == 4096:   # room for the first ids: the rest are counted
        assert f"#{sources[1]['id']}/codex" in receipt['body'] and f'and {count - 1 - 5} more' in receipt['body']
    else:
        assert 'more' in receipt['body'] or ', ' not in receipt['body']
    link = link_of(env, ack)
    assert [bool(s.get('superseded')) for s in link['sources']] == [True] + [False] * (count - 1)
    assert all(s['picked_up'] for s in link['sources'][1:])


def test_the_counts_only_receipt_fits_the_smallest_supported_limit_in_the_worst_case():
    from types import SimpleNamespace
    board = SimpleNamespace(s=SimpleNamespace(body_max_bytes=256))
    ack, ids = '#9999999999/' + 'a' * 32, [f'#{9999999999 - i}/' + 'a' * 32 for i in range(100)]
    body, reason = recovery._mixed_receipt(board, ack, ids[:99], ids[99:])
    assert len(body.encode()) <= 256 and len(reason.encode()) <= 256
    assert 'verified pickup of 99 linked request(s); 1 superseded' in body


def test_a_superseded_acknowledgement_is_left_alone_by_pickup_and_routing(env):
    tid, (source,), ack = recovery_setup(env, count=1)
    supersede(env, tid, ack, key='obsolete-ack')
    link = link_of(env, ack)
    other = env.session('codex')
    capabilities.register(env.board, env.p['codex'], other, ['git:write'], 'Checked git write access')
    requests.assign(env.board, env.p['codex'], env.sid['codex'], source['id'], 'codex', other, 0, 'move',
                    ['git:write'])
    requests.progress(env.board, env.p['codex'], other, source['id'], 'codex', 'started')
    assert link_of(env, ack) == link, 'a retired link is neither re-versioned nor picked up'
    assert row(env, ack)['disposition'] == 'superseded'


def test_superseded_requests_leave_unstick_and_open_obligations(env):
    tid, (source,), ack = recovery_setup(env, count=1)
    assert unstick.stuck_agents(env.board, tid)[1][0]['post_ids'] == [source['id'], ack['id']]
    supersede(env, tid, source)
    supersede(env, tid, ack, key='obsolete-ack')
    assert unstick.stuck_agents(env.board, tid) == ([], [])
    listed = [i['post_id'] for i in obligations.open_requests(env.board, env.p['codex'], env.sid['codex'])[0]]
    assert source['id'] not in listed and ack['id'] not in listed


# ---------------------------------------------------------------- routed successors


def routed(env):
    tid = env.thread()
    req = env.post('human', tid, 'Approved objective', 'request', to=['codex', 'claude'])
    target = env.thread('destination')
    action = {'type': 'repost', 'post_id': req['id'], 'recipient': 'codex', 'expected_version': 0,
              'target_thread_id': target}
    successor = choose(env, proposal(env, tid, action))['action_result']['reposted_post_id']
    return tid, req, target, env.board.get_post(env.p['human'], successor)


def test_an_original_waits_for_its_open_successor_then_both_can_be_retired(env):
    tid, req, target, successor = routed(env)
    assert row(env, req)['state'] == 'blocked'
    before = post_count(env)
    with pytest.raises(Conflict, match=f"successor #{successor['id']}/codex, which is still open"):
        supersede(env, tid, req)
    assert post_count(env) == before and row(env, req)['state'] == 'blocked'
    supersede(env, target, successor, key='obsolete-successor')
    assert row(env, successor)['disposition'] == 'superseded'
    # A superseded successor never completes its original: that stays blocked until settled itself.
    assert (row(env, req)['state'], row(env, req)['version']) == ('blocked', 1)
    supersede(env, tid, req, key='obsolete-original')
    assert row(env, req)['disposition'] == 'superseded'
    assert row(env, req, 'claude')['state'] == 'queued'


def test_a_successor_in_a_closed_thread_still_holds_its_original(env):
    """The human can reopen the thread, and the successor would then be due but unable to start."""
    tid, req, target, successor = routed(env)
    env.board.set_thread_status(env.p['human'], target, 'closed')
    assert request_replies.open_successor(env.board, req['id'], 'codex') == (successor['id'], 'codex')
    with pytest.raises(Conflict, match='still open'):
        supersede(env, tid, req)
    assert row(env, req)['state'] == 'blocked'


URL = 'https://tenant.example/sites/catalog'


def access_routed(env):
    """The dispatcher hands a signed-in browser request to grok, whose runner holds the sign-in (access.py): the
    successor goes to another agent and has no stored request row (requests.for_post gives it a virtual one)."""
    from agent_comms import access
    tid = env.thread()
    original = env.post('human', tid, 'Install the package', 'request', to=['codex'],
                        browser_requirement={'recipient': 'codex', 'target_url': URL, 'execution_context': 'desktop'})
    successor_id = access.route_successor(env.board, env.p['human'], original['id'], 'codex', 'grok',
                                          ['https://tenant.example:443'], 'laptop')
    assert env.board.conn.execute('SELECT 1 FROM request_progress WHERE post_id=?', (successor_id,)).fetchone() is None
    return tid, original, env.board.get_post(env.p['human'], successor_id)


def test_an_access_routed_successor_without_a_stored_row_holds_its_original(env):
    tid, original, successor = access_routed(env)
    with pytest.raises(Conflict, match=f"successor #{successor['id']}/grok, which is still open"):
        supersede(env, tid, original)
    # Its own agent retires it without a browser probe: retirement executes nothing. Completing it still needs one.
    with pytest.raises(Conflict, match='browser preflight'):
        env.board.create_post(env.p['grok'], env.sid['grok'], thread_id=tid, type='status', body='done',
                              idempotency_key='grok-done', request_reply=dict(
                                  post_id=successor['id'], recipient='grok', expected_version=0, state='finished',
                                  reason='done', disposition='completed'))
    supersede(env, tid, successor, agent='grok', recipient='grok', key='obsolete-successor')
    assert row(env, successor, 'grok')['disposition'] == 'superseded'
    supersede(env, tid, original)
    assert row(env, original)['disposition'] == 'superseded'


def test_board_request_progress_supersedes_a_linked_request_without_a_probe(env):
    from fastapi.testclient import TestClient
    from agent_comms.api import create_app
    from test_issues_api import headers
    tid, original, successor = access_routed(env)
    evidence = env.post('grok', tid, 'This install is obsolete: the package shipped', 'status')
    client = TestClient(create_app(env.board))
    result = client.post(f"/api/posts/{successor['id']}/request-progress", headers=headers(env, 'grok'), json=dict(
        recipient='grok', state='finished', disposition='superseded', expected_version=0, reason=REASON,
        evidence_post_ids=[evidence['id']]))
    assert result.status_code == 200, result.text
    assert (result.json()['state'], result.json()['disposition']) == ('finished', 'superseded')
    assert requests.history(env.board, env.p['grok'], successor['id'], 'grok')[-1]['evidence_post_ids'] == [evidence['id']]
    # The same entry point keeps the browser gate for a completion.
    blocked_tid = env.thread()
    other = env.post('human', blocked_tid, 'Install it', 'request', to=['codex'],
                     browser_requirement={'recipient': 'codex', 'target_url': URL, 'execution_context': 'desktop'})
    proof = env.post('codex', blocked_tid, 'Installed', 'status')
    result = client.post(f"/api/posts/{other['id']}/request-progress", headers=headers(env), json=dict(
        recipient='codex', state='finished', disposition='completed', expected_version=0, reason='done',
        evidence_post_ids=[proof['id']]))
    assert result.status_code == 409 and 'browser preflight' in result.text


def test_a_successor_of_another_recipient_does_not_hold_this_one(env):
    tid, req, target, successor = routed(env)
    supersede(env, tid, req, agent='claude', recipient='claude')
    assert row(env, req, 'claude')['disposition'] == 'superseded'


@pytest.mark.parametrize('kind', ['answer', 'answered', 'successor', 'original', 'acknowledgement', 'source'])
def test_the_human_cannot_supersede_any_kind_of_linked_request(env, kind):
    """The rule is for the recipient agent; the human keeps the dashboard's explicit actions."""
    if kind in ('answer', 'answered'):
        tid = env.thread()
        ask = env.post('human', tid, 'objective', 'request', to=['codex'])
        answer = env.post('human', tid, 'about it', 'status', to=['codex'], answer_to=[ask['id']])
        target = answer if kind == 'answer' else ask
    elif kind in ('successor', 'original'):
        tid, req, _, successor = routed(env)
        target = successor if kind == 'successor' else req
    else:
        tid, (source,), ack = recovery_setup(env, count=1)
        target = ack if kind == 'acknowledgement' else source
    with pytest.raises(Forbidden, match='only its assigned agent'):
        requests.progress(env.board, env.p['human'], env.sid['human'], target['id'], 'codex', 'finished', REASON,
                          terminal_disposition='superseded')
    assert row(env, target)['state'] != 'finished'


def test_recovery_settlement_is_scoped_to_the_exact_recipient(env):
    tid = env.thread()
    source = env.post('human', tid, 'stalled objective', 'request', to=['codex', 'claude'])
    ack = env.post('human', tid, 'Unstick', 'request', to=['codex', 'claude'])
    with db.write_tx(env.board.conn):
        recovery.record(env.board, env.p['human'], ack['id'], 'codex',
                        [{'post_id': source['id'], 'recipient': 'codex', 'version': 0}])
    link = link_of(env, ack)
    supersede(env, tid, source, agent='claude', recipient='claude')
    supersede(env, tid, ack, agent='claude', recipient='claude', key='obsolete-ack')
    assert link_of(env, ack) == link, "claude's rows are not codex's linked ones"
    assert row(env, ack)['state'] == 'queued' and row(env, source)['state'] == 'queued'


# ---------------------------------------------------------------- automatic machinery


def test_the_dispatcher_no_longer_picks_up_superseded_linked_requests(aenv):
    tid = aenv.tid
    question = aenv.post('codex', tid, 'Ship?', 'question', needs_response=True, decision_question=ASK)
    answer = aenv.post('human', tid, 'Yes', 'status', to=['codex'], answer_to=[question['id']])
    source = aenv.post('human', tid, 'stalled', 'request', to=['codex'])
    ack = aenv.post('human', tid, 'Unstick', 'request', to=['codex'])
    with db.write_tx(aenv.board.conn):
        recovery.record(aenv.board, aenv.p['human'], ack['id'], 'codex',
                        [{'post_id': source['id'], 'recipient': 'codex', 'version': 0}])
    for post in (answer, ack):
        assert aenv.d._request_due(post['id'], 'codex')
    supersede(aenv, tid, answer)
    supersede(aenv, tid, ack, key='obsolete-ack')
    for post in (answer, ack):
        assert not aenv.d._request_due(post['id'], 'codex')
    assert aenv.d._request_due(source['id'], 'codex'), 'the still-open source is untouched'


def test_a_recovery_wait_stops_retrying_once_its_request_is_superseded(aenv, tmp_path):
    h = blocked_handoff(aenv, tmp_path)
    note = aenv.post('human', h.tid, 'about the audit', 'status', to=[])
    aenv.board.conn.execute('INSERT INTO answer_links(source_post_id,answer_post_id,created_at) VALUES (?,?,?)',
                            (h.ask['id'], note['id'], aenv.clock()))
    blocked(aenv, h)
    assert recovery.active_wait(aenv.board, 'codex', h.tid) is not None
    # The old owner comes back and retires its own request as obsolete.
    supersede(aenv, h.tid, h.ask, session_id=h.old)
    assert not recovery.wait_live(aenv.board, wait_of(aenv, h))
    assert recovery.active_wait(aenv.board, 'codex', h.tid) is None
    go_quiet(aenv)
    aenv.d.tick()
    autorecover.tick(aenv.board, aenv.p['human'], runner_for=lambda a: ['x'], fence=aenv.d._fence())
    assert wait_of(aenv, h)['state'] == 'resolved'
    assert auto_posts(aenv) == [] and aenv.spawner.calls == [], 'nobody is relaunched for it'


def test_an_undelivered_alert_closes_once_its_linked_request_is_superseded(denv):
    question = denv.post('codex', denv.tid, 'Ship?', 'question', needs_response=True, decision_question=ASK)
    answer = denv.post('human', denv.tid, 'Yes', 'status', to=['claude', 'codex'], answer_to=[question['id']])
    denv.clock.advance(WINDOW + 1)
    denv.d.tick()
    [alert] = alerts(denv)
    assert alert['id'] in delivery_needs_you(denv)
    session = denv.session('claude', PROJECT)
    supersede(denv, denv.tid, answer, agent='claude', recipient='claude', session_id=session)
    denv.d.tick()
    assert alert['id'] in delivery_needs_you(denv), "codex's row is still undelivered"
    supersede(denv, denv.tid, answer, key='obsolete-codex')
    denv.d.tick()
    assert alert['id'] not in delivery_needs_you(denv)
    assert denv.spawner.calls == []


# ---------------------------------------------------------------- refusals that stay


def test_a_managed_continuation_still_refuses_and_names_its_completion_path(stack):  # noqa: F811
    env = stack['env']
    post = create_continuation(stack)
    with pytest.raises(Forbidden, match="managed continuation cannot be superseded.*completion receipts"):
        supersede(env, stack['thread'], post)
    assert continuation_row(stack, post)['state'] == 'queued'


def test_issue_linked_posts_still_refuse_and_name_the_issue_flow(env):
    tid = env.thread()
    ask = env.post('human', tid, 'shared objective', 'request', to=['codex'])
    issues.create_issue(env.board, env.p['codex'], env.sid['codex'], title='shared', body='shared work',
                        thread_id=tid, post_id=ask['id'], needs_human=False)
    with pytest.raises(Forbidden, match='issue-linked request cannot be superseded.*board_comment_issue'):
        supersede(env, tid, ask)
    question = env.post('codex', tid, 'Which way?', 'question', needs_response=True, decision_question=ASK)
    issue = issues.create_issue(env.board, env.p['codex'], env.sid['codex'], title='q', body='question',
                                thread_id=tid, post_id=question['id'])
    issues.decide_issue(env.board, env.p['human'], env.sid['human'], issue['id'], 'This way', [tid])
    answer_id = env.board.conn.execute('SELECT answer_post_id FROM issue_answer_links').fetchone()[0]
    answer = env.board.get_post(env.p['human'], answer_id)
    with pytest.raises(Forbidden, match='issue answer cannot be superseded.*board_comment_issue'):
        supersede(env, tid, answer, key='obsolete-answer')
    assert row(env, ask)['state'] == 'queued' and row(env, answer)['state'] == 'queued'
