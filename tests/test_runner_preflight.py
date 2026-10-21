"""Real CLI receipts and temporal launch races, without launching an external CLI."""
import json
import subprocess
from pathlib import Path

import pytest

from agent_comms import runner_preflight as rp, requests, recovery, capabilities, db
from agent_comms.core import Board, Conflict
from agent_comms.dispatch import DispatchConfig
from test_dispatch import denv, allow, human_post
from conftest import PROJECT


def transcript(session, *, denied=False, failed=False):
    events = []
    for index, command in enumerate(rp.COMMANDS):
        events += [dict(type="assistant", session_id=session, message={"content": [dict(
            type="tool_use", id=str(index), name="Bash", input={"command": command})]}),
            dict(type="user", session_id=session, message={"content": [dict(type="tool_result",
                 tool_use_id=str(index), is_error=failed)]}, tool_use_result={"interrupted": False})]
    events.append(dict(type="result", session_id=session, is_error=False, subtype="success",
                       permission_denials=["Bash"] if denied else []))
    return "\n".join(json.dumps(e) for e in events)


def configure(env, project=PROJECT):
    env.d.config.runners['claude'] = ["claude", "-p", "{prompt}", "--permission-mode", "acceptEdits",
                                      "--allowedTools=Bash,Read"]
    env.d.config.claude_tool_projects = [project]
    allow(env, agents=['claude'], max_launches=1)
    post = human_post(env, ['claude'])
    env.d.tick()
    run = env.d.running['claude']
    record = env.d._get(env.d.RUN_PREFIX + run.run_id)
    return post, run, record


def complete_probe(env, run, record, **kwargs):
    Path(run.log).write_text(transcript(record['tool_preflight']['session_id'], **kwargs))
    run.child.code = 0
    env.d.tick()


def test_scoped_override_preserves_unrelated_runner_and_denies():
    original = ['claude', '-p', '{prompt}', '--permission-mode', 'acceptEdits', '--allowedTools=Bash',
                '--disallowedTools=Bash(git push *)']
    scoped = rp.scoped_template(original)
    assert original[-2] == '--allowedTools=Bash'
    assert '--allowedTools=Bash' not in scoped
    assert any(x.startswith('--disallowedTools=') and 'Bash(git push *)' in x for x in scoped)
    assert 'dontAsk' in scoped
    assert not any(x.startswith('--setting-sources') for x in scoped)
    assert 'Bash' not in rp.ALLOWED
    assert not any('git *' in x or 'gh *' in x for x in rp.ALLOWED)
    assert DispatchConfig.from_dict({}).claude_tool_projects == []
    with pytest.raises(ValueError):
        DispatchConfig.from_dict({'claude_tool_projects': ['relative']})
    with pytest.raises(ValueError):
        rp.scoped_template(original + ['--resume', 'old'])


def test_probe_then_same_session_resume_and_started_gate(denv):
    e = denv
    post, run, rec = configure(e)
    assert len(e.spawner.calls) == 1
    assert '--session-id' in e.spawner.calls[0]['argv']
    assert e.board.get_post(e.p['human'], post['id'])['requests'][0]['state'] == 'queued'
    with pytest.raises(Conflict, match='verified dispatcher'):
        requests.progress(e.board, e.p['claude'], e.sid['claude'], post['id'], 'claude', 'started')
    complete_probe(e, run, rec)
    assert len(e.spawner.calls) == 2
    argv = e.spawner.calls[1]['argv']
    assert argv[argv.index('--resume')+1] == rec['tool_preflight']['session_id']
    assert e.board.list_dispatch_rules(e.p['human'])[0]['launches_left'] == 0
    sid = e.board.register_session(e.p['claude'], PROJECT, e.workdir,
        client=('claude-code', rec['tool_preflight']['session_id']), dispatch_run_id=run.run_id)['session_id']
    requests.progress(e.board, e.p['claude'], sid, post['id'], 'claude', 'started')
    assert e.board.get_post(e.p['human'], post['id'])['requests'][0]['state'] == 'started'


@pytest.mark.parametrize('kind', ['denied', 'failed', 'prose', 'wrong_session'])
def test_missing_receipts_block_without_work_launch(denv, kind):
    e = denv
    post, run, rec = configure(e)
    contents = transcript(rec['tool_preflight']['session_id'], denied=kind=='denied', failed=kind=='failed')
    if kind == 'prose':
        contents = 'All tools succeeded, trust me.'
    if kind == 'wrong_session':
        contents = transcript('other')
    Path(run.log).write_text(contents)
    run.child.code = 0
    e.d.tick()
    assert len(e.spawner.calls) == 1
    assert e.board.get_post(e.p['human'], post['id'])['requests'][0]['state'] == 'blocked'
    e.clock.advance(500)
    e.d.tick()
    assert len(e.spawner.calls) == 1


@pytest.mark.parametrize('change', ['pause', 'revoke', 'close', 'finish', 'fence'])
def test_authority_or_request_change_during_probe_prevents_resume(denv, change):
    e = denv
    post, run, rec = configure(e)
    if change == 'pause':
        e.board.set_paused(e.p['human'], True)
    elif change == 'revoke':
        e.board.revoke_dispatch_rule(e.p['human'], run.rule_id)
    elif change == 'close':
        e.board.conn.execute("UPDATE threads SET status='closed' WHERE id=?", (e.tid,))
    elif change == 'finish':
        requests.progress(e.board, e.p['human'], e.sid['human'], post['id'], 'claude', 'finished', reason='Completed separately')
    else:
        e.d._save(**{e.d.OWNER_KEY:'another'})
    complete_probe(e, run, rec)
    assert len(e.spawner.calls) == 1


def test_preflight_timeout_is_bounded(denv):
    e = denv
    post, run, rec = configure(e)
    e.clock.advance(121)
    e.d.tick()
    assert run.child.terminated == 1
    e.d.tick()
    assert len(e.spawner.calls) == 1
    assert e.board.get_post(e.p['human'], post['id'])['requests'][0]['state'] == 'queued'   # retried (requeue.py)


def test_registered_session_cannot_start_before_verified_receipts(denv):
    e = denv
    post, run, rec = configure(e)
    sid = e.board.register_session(e.p['claude'], PROJECT, e.workdir,
        client=('claude-code', rec['tool_preflight']['session_id']), dispatch_run_id=run.run_id)['session_id']
    with pytest.raises(Conflict, match='preflight has not passed'):
        requests.progress(e.board, e.p['claude'], sid, post['id'], 'claude', 'started')


def test_registered_session_cannot_skip_the_gate_by_finishing_unstarted_work(denv):
    e = denv
    post, run, rec = configure(e)
    sid = e.board.register_session(e.p['claude'], PROJECT, e.workdir,
        client=('claude-code', rec['tool_preflight']['session_id']), dispatch_run_id=run.run_id)['session_id']
    proof = e.board.create_post(e.p['claude'], sid, thread_id=post['thread_id'], type='status', body='Did it all')
    with pytest.raises(Conflict, match='preflight has not passed'):
        requests.progress(e.board, e.p['claude'], sid, post['id'], 'claude', 'finished', reason='Done',
                          evidence_post_ids=[proof['id']])


@pytest.mark.parametrize('state', ['started', 'finished'])
def test_public_queued_reason_cannot_forge_recovery_proof(denv, state):
    e = denv
    post, run, rec = configure(e)
    complete_probe(e, run, rec)
    sid = e.board.register_session(e.p['claude'], PROJECT, e.workdir,
        client=('claude-code', rec['tool_preflight']['session_id']), dispatch_run_id=run.run_id)['session_id']
    other = human_post(e, ['claude'], body='older request this run did not launch for')
    assert other['id'] not in rec['request_ids']
    requests.progress(e.board, e.p['claude'], sid, other['id'], 'claude', 'queued',
                      reason='Bookkeeping ownership recovered from ended session 1; execution preflight still required')
    proof = e.board.create_post(e.p['claude'], sid, thread_id=post['thread_id'], type='status', body='Verified on main')
    with pytest.raises(Conflict, match='preflight has not passed'):
        requests.progress(e.board, e.p['claude'], sid, other['id'], 'claude', state,
                          reason='Done', evidence_post_ids=[proof['id']])
    assert e.board.get_post(e.p['human'], other['id'])['requests'][0]['state'] == 'queued'


def test_v12_reopen_does_not_promote_legacy_reason_to_recovery_proof(denv):
    e = denv
    _, run, rec = configure(e)
    complete_probe(e, run, rec)
    sid = e.board.register_session(e.p['claude'], PROJECT, e.workdir,
        client=('claude-code', rec['tool_preflight']['session_id']), dispatch_run_id=run.run_id)['session_id']
    other = human_post(e, ['claude'], body='legacy reason-only request')
    requests.progress(e.board, e.p['claude'], sid, other['id'], 'claude', 'queued',
                      reason='Bookkeeping ownership recovered from ended session 1; execution preflight still required')
    before = requests.history(e.board, e.p['human'], other['id'], 'claude')
    e.board.conn.execute('DROP TRIGGER invalidate_request_owner_recovery')
    e.board.conn.execute('DROP TABLE request_owner_recoveries')
    e.board.conn.execute('PRAGMA user_version=12')
    e.board.conn.close()
    e.board = Board(e.settings, clock=e.clock)
    db.init_schema(e.board.conn)
    db.init_schema(e.board.conn)
    assert e.board.conn.execute('PRAGMA user_version').fetchone()[0] == db.SCHEMA_VERSION
    for state in ('started', 'finished'):
        with pytest.raises(Conflict, match='preflight has not passed'):
            requests.progress(e.board, e.p['claude'], sid, other['id'], 'claude', state, 'Done')
    assert requests.history(e.board, e.p['human'], other['id'], 'claude') == before
    assert not e.board.conn.execute('SELECT 1 FROM request_owner_recoveries').fetchone()


def recovered_request(e):
    """A real clean checkout, verified launched conversation, and supported ended-owner transfer."""
    subprocess.run(['git', 'init', e.workdir], check=True, capture_output=True)
    e.board.conn.execute('UPDATE threads SET project=? WHERE id=?', (e.workdir, e.tid))
    e.d.config.worktrees = {e.workdir: e.workdir}
    post, run, rec = configure(e, e.workdir)
    complete_probe(e, run, rec)
    sid = e.board.register_session(e.p['claude'], e.workdir, e.workdir,
        client=('claude-code', rec['tool_preflight']['session_id']), dispatch_run_id=run.run_id)['session_id']
    old = e.session('claude', project=e.workdir, worktree=e.workdir)
    other = human_post(e, ['claude', 'codex'], body='unfinished work from an ended conversation')
    assert other['id'] not in rec['request_ids']
    requests.progress(e.board, e.p['claude'], old, other['id'], 'claude', 'blocked', 'old owner stopped')
    e.board.conn.execute('UPDATE sessions SET dispatch_run_id=? WHERE id=?', ('ended', old))
    e.d._save(**{'dispatch.run.ended': {'agent': 'claude', 'thread_id': e.tid, 'status': 'exited',
                                       'ended_at': e.clock()}})
    recovery.transfer_ended_owner(e.board, e.p['claude'], sid, other['id'], 'claude', 1)
    return other, sid, old


@pytest.mark.parametrize('start_first', [False, True])
def test_verified_run_can_execute_actual_recovered_request(denv, start_first):
    e = denv
    other, sid, _ = recovered_request(e)
    # Same-owner progress increments the version without invalidating the durable receipt.
    requests.progress(e.board, e.p['claude'], sid, other['id'], 'claude', 'queued', 'ready for pickup')
    e.board.conn.close()
    e.board = Board(e.settings, clock=e.clock)
    db.init_schema(e.board.conn)
    if start_first:
        requests.progress(e.board, e.p['claude'], sid, other['id'], 'claude', 'started')
    proof = e.board.create_post(e.p['claude'], sid, thread_id=e.tid, type='status', body='Verified result')
    requests.progress(e.board, e.p['claude'], sid, other['id'], 'claude', 'finished',
                      reason='Done', evidence_post_ids=[proof['id']])
    assert e.board.get_post(e.p['human'], other['id'])['requests'][0]['state'] == 'finished'


def test_recovered_request_still_requires_current_executing_session(denv):
    e = denv
    other, sid, _ = recovered_request(e)
    requests.progress(e.board, e.p['claude'], sid, other['id'], 'claude', 'started')
    wrong = e.session('claude', project=e.workdir, worktree=e.workdir)
    with pytest.raises(Conflict, match='owned by another session'):
        requests.progress(e.board, e.p['claude'], wrong, other['id'], 'claude', 'finished', 'Done')
    requests.progress(e.board, e.p['human'], e.sid['human'], other['id'], 'claude', 'finished', 'Author confirmed')
    assert e.board.get_post(e.p['human'], other['id'])['requests'][0]['assigned_session'] == sid


def test_recovery_proof_does_not_cover_another_recipient(denv):
    e = denv
    other, sid, _ = recovered_request(e)
    capabilities.register(e.board, e.p['claude'], sid, ['git'], 'verified')
    requests.assign(e.board, e.p['human'], e.sid['human'], other['id'], 'codex', sid, 0, 'route to claude', ['git'])
    with pytest.raises(Conflict, match='preflight has not passed'):
        requests.progress(e.board, e.p['claude'], sid, other['id'], 'codex', 'started')


def test_recovery_proof_does_not_follow_ownership_away_and_back(denv):
    e = denv
    other, sid, old = recovered_request(e)
    for target in (old, sid):
        capabilities.register(e.board, e.p['claude'], target, ['git'], 'verified')
    requests.assign(e.board, e.p['human'], e.sid['human'], other['id'], 'claude', old, 2, 'route away', ['git'])
    with pytest.raises(Conflict, match='preflight has not passed|owned by another session'):
        requests.progress(e.board, e.p['claude'], sid, other['id'], 'claude', 'started')
    requests.assign(e.board, e.p['human'], e.sid['human'], other['id'], 'claude', sid, 3, 'route back', ['git'])
    with pytest.raises(Conflict, match='preflight has not passed'):
        requests.progress(e.board, e.p['claude'], sid, other['id'], 'claude', 'finished', 'Done')


def test_scoped_runner_can_check_merges():
    for tool in ('Bash(git fetch origin)', 'Bash(git fetch origin main)', 'Bash(gh pr view *)', 'Bash(gh pr list *)', 'Bash(gh pr list)'):
        assert tool in rp.ALLOWED
    assert 'Bash(git fetch)' not in rp.ALLOWED
    assert not any(t.startswith(('Bash(gh pr merge', 'Bash(gh api', 'Bash(git push')) for t in rp.ALLOWED)


@pytest.mark.parametrize('mismatch', ['conversation', 'directory'])
def test_wrong_execution_context_cannot_use_verified_receipts(denv, mismatch):
    e = denv
    post, run, rec = configure(e)
    complete_probe(e, run, rec)
    client = rec['tool_preflight']['session_id'] if mismatch != 'conversation' else 'f7030b88-321d-470e-90f3-33d280a71735'
    directory = e.workdir if mismatch != 'directory' else str(Path(e.workdir)/'other')
    sid = e.board.register_session(e.p['claude'], PROJECT, directory,
        client=('claude-code', client), dispatch_run_id=run.run_id)['session_id']
    with pytest.raises(Conflict, match='preflight has not passed'):
        requests.progress(e.board, e.p['claude'], sid, post['id'], 'claude', 'started')


def test_revocation_between_verified_probe_and_pickup_is_enforced(denv):
    e = denv
    post, run, rec = configure(e)
    complete_probe(e, run, rec)
    sid = e.board.register_session(e.p['claude'], PROJECT, e.workdir,
        client=('claude-code', rec['tool_preflight']['session_id']), dispatch_run_id=run.run_id)['session_id']
    e.board.revoke_dispatch_rule(e.p['human'], run.rule_id)
    with pytest.raises(Conflict, match='authorization ended'):
        requests.progress(e.board, e.p['claude'], sid, post['id'], 'claude', 'started')
