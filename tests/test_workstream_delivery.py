"""Exercise continuation ownership through real dispatcher reservation and binding."""
import pytest

from agent_comms import capabilities, dispatch, requests, workstreams
from agent_comms.core import Conflict, Forbidden, Invalid
from test_dispatch import FakeProcs, FakeSpawner, new_dispatcher
from test_workstreams import stack, create, current, probe, completion, git


@pytest.fixture
def delivery_env(stack, tmp_path, monkeypatch, request):
    env = stack['env']
    monkeypatch.setattr(dispatch.shutil, 'which', lambda executable, **kw: '/fake/' + executable)
    env.config = dispatch.DispatchConfig.from_dict({
        'runners': {'codex': ['codex-cli-fake', 'exec', '{prompt}'],
                    'claude': ['claude-fake', '-p', '{prompt}']},
        'worktrees': {str(stack['repo']): str(stack['fallback_path'])},
        'live_minutes': 2, 'timeout_minutes': 30, 'kill_grace_seconds': 10, 'max_concurrent': 2,
    })
    env.spawner = FakeSpawner()
    env.procs = FakeProcs(env.spawner)
    env.log_dir = tmp_path / 'logs'
    worker = new_dispatcher(env)
    worker.tick()
    rule = env.board.create_dispatch_rule(env.p['human'], thread_id=stack['thread'],
        agents=['codex', 'claude'], purpose='Propagate approved descendant fix', max_launches=3)
    extra = {}
    if getattr(request, 'param', None) == 'peer':
        stack['peer_path'] = tmp_path / 'peer-worktree'
        git(stack['repo'], 'worktree', 'add', '-b', 'child-two', str(stack['peer_path']), 'child')
        env.board.register_session(env.p['grok'], str(stack['repo']), str(stack['peer_path']),
                                   resume_session_id=env.sid['grok'])
        extra['descendants'] = ['refs/heads/child', 'refs/heads/child-two']
    post = create(stack, **extra)
    env.clock.advance(121)
    probe(stack, 'codex')
    probe(stack, 'claude')
    return stack, worker, rule, post


def bind(stack, worker, **overrides):
    env = stack['env']
    args = dict(project=str(stack['repo']), worktree=str(stack['fallback_path']),
                dispatch_run_id=worker.running['claude'].run_id)
    args.update(overrides)
    return env.board.register_session(env.p['claude'], **args)['session_id']


def test_live_idle_fallback_wakes_once_and_new_worker_completes(delivery_env):
    stack, worker, rule, post = delivery_env
    env = stack['env']
    old_fallback = env.sid['claude']
    assert worker._live('claude', env.clock())
    worker.tick()
    assert len(env.spawner.calls) == 1
    assert env.spawner.calls[0]['cwd'] == str(stack['fallback_path'])
    assert current(stack, post)['assigned_session'] == old_fallback
    for _ in range(3):
        worker.tick()
    assert len(env.spawner.calls) == 1
    with pytest.raises((Conflict, Forbidden)):
        env.board.claim_task(env.p['claude'], old_fallback, post['task_id'])
    with pytest.raises((Conflict, Forbidden)):
        requests.progress(env.board, env.p['claude'], old_fallback, post['id'], 'codex', 'started',
                          expected_version=current(stack, post)['version'])
    sid = bind(stack, worker)
    assert sid != old_fallback
    assert current(stack, post)['assigned_session'] == sid
    with pytest.raises((Conflict, Forbidden)):
        env.board.claim_task(env.p['claude'], old_fallback, post['task_id'])
    capabilities.register(env.board, env.p['claude'], sid, ['git:write'],
                          'Verified shell, clean worktree and git write access', activity='active')
    env.board.claim_task(env.p['claude'], sid, post['task_id'])
    requests.progress(env.board, env.p['claude'], sid, post['id'], 'codex', 'started',
                      expected_version=current(stack, post)['version'])
    evidence = env.board.create_post(env.p['claude'], sid, thread_id=stack['thread'], type='status',
                                    body='Verified exact descendant head ancestry and required test')
    requests.progress(env.board, env.p['claude'], sid, post['id'], 'codex', 'finished',
                      expected_version=current(stack, post)['version'], evidence_post_ids=[evidence['id']],
                      completion=completion(stack), reason='Verified all descendant checks')
    env.spawner.children[0].code = 0
    worker.tick()
    assert current(stack, post)['state'] == 'finished'
    assert env.board.get_task(env.p['human'], post['task_id'])['status'] == 'done'
    assert len(env.spawner.calls) == 1


@pytest.mark.parametrize('obstacle', ['dirty_owner', 'active_owner', 'dirty_peer', 'unknown_peer'])
def test_delivery_never_starts_over_unfinished_declared_work(delivery_env, tmp_path, obstacle):
    stack, worker, rule, post = delivery_env
    env = stack['env']
    if obstacle == 'dirty_owner':
        (stack['owner_path'] / 'tracked').write_text('unfinished owner work\n')
    elif obstacle == 'active_owner':
        probe(stack, 'codex', activity='active')
    else:
        # The declared child itself may have another registered session: unknown
        # or active activity in its checkout must prevent takeover.
        env.board.register_session(env.p['grok'], str(stack['repo']), str(stack['owner_path']),
                                   resume_session_id=env.sid['grok'])
        if obstacle == 'dirty_peer':
            probe(stack, 'grok')
            (stack['owner_path'] / 'peer-draft').write_text('preserve\n')
    before = git(stack['owner_path'], 'status', '--porcelain')
    worker.tick()
    assert not env.spawner.calls
    assert current(stack, post)['assigned_session'] == env.sid['codex']
    assert git(stack['owner_path'], 'status', '--porcelain') == before


@pytest.mark.parametrize('mismatch', ['project', 'worktree', 'epoch'])
def test_registration_rejects_wrong_environment_or_assignment_epoch(delivery_env, mismatch):
    stack, worker, rule, post = delivery_env
    env = stack['env']
    worker.tick()
    before = current(stack, post)
    overrides = {}
    if mismatch == 'epoch':
        env.board.conn.execute('UPDATE continuations SET epoch=epoch+1 WHERE post_id=?', (post['id'],))
    else:
        overrides[mismatch] = str(stack['owner_path'])
    with pytest.raises((Forbidden, Conflict, Invalid)):
        bind(stack, worker, **overrides)
    assert current(stack, post) == before


@pytest.mark.parametrize('change', ['pause', 'revoke', 'fence'])
def test_change_between_budget_reservation_and_delivery_prevents_spawn(delivery_env, monkeypatch, change):
    stack, worker, rule, post = delivery_env
    env = stack['env']
    original = workstreams.reserve_delivery
    def changed(*args, **kwargs):
        if change == 'pause':
            env.board.set_paused(env.p['human'], True)
        elif change == 'revoke':
            env.board.revoke_dispatch_rule(env.p['human'], rule['id'])
        else:
            key, value = worker._fence()
            env.board.conn.execute('UPDATE board_state SET value=? WHERE key=?', ('"new owner"', key))
        return original(*args, **kwargs)
    monkeypatch.setattr(workstreams, 'reserve_delivery', changed)
    worker.tick()
    assert env.spawner.calls == []
    assert workstreams.get_for_post(env.board, post['id'])['dispatch_run_id'] is None
    assert current(stack, post)['state'] == 'queued'


@pytest.mark.parametrize('change', ['pause', 'revoke'])
def test_change_after_spawn_prevents_worker_binding(delivery_env, change):
    stack, worker, rule, post = delivery_env
    env = stack['env']
    worker.tick()
    if change == 'pause':
        env.board.set_paused(env.p['human'], True)
    else:
        env.board.revoke_dispatch_rule(env.p['human'], rule['id'])
    with pytest.raises((Conflict, Forbidden)):
        bind(stack, worker)
    assert current(stack, post)['assigned_session'] == env.sid['claude']


@pytest.mark.parametrize('failure', ['spawn', 'unacknowledged_exit'])
def test_delivery_failure_is_explicit_and_does_not_duplicate_restack(delivery_env, failure):
    stack, worker, rule, post = delivery_env
    env = stack['env']
    if failure == 'spawn':
        env.spawner.fail_for.add('claude-fake')
    worker.tick()
    if failure == 'unacknowledged_exit':
        env.spawner.children[0].code = 0
        worker.tick()
    assert current(stack, post)['state'] == 'blocked'
    assert ('failed to start' if failure == 'spawn' else 'without explicit request completion') in current(stack, post)['reason']
    for _ in range(3):
        env.clock.advance(121)
        probe(stack, 'codex')
        probe(stack, 'claude')
        worker.tick()
    assert len(env.spawner.calls) == 1
    assert env.board.conn.execute('SELECT COUNT(*) FROM continuations').fetchone()[0] == 1
    assert env.board.conn.execute('SELECT COUNT(*) FROM tasks').fetchone()[0] == 2


def test_configured_environment_must_match_verified_fallback(delivery_env):
    stack, worker, rule, post = delivery_env
    worker.config.worktrees[str(stack['repo'])] = str(stack['repo'])
    worker.tick()
    assert not stack['env'].spawner.calls
    assert current(stack, post)['state'] == 'blocked'
    assert 'verified fallback environment' in current(stack, post)['reason']


@pytest.mark.parametrize('approval', ['owner_only', 'revoked', 'paused'])
def test_delivery_does_not_expand_dispatch_authority(delivery_env, approval):
    stack, worker, rule, post = delivery_env
    env = stack['env']
    if approval == 'paused':
        env.board.set_paused(env.p['human'], True)
    else:
        env.board.revoke_dispatch_rule(env.p['human'], rule['id'])
        if approval == 'owner_only':
            env.board.create_dispatch_rule(env.p['human'], thread_id=stack['thread'],
                agents=['codex'], purpose='Owner only', max_launches=3)
    worker.tick()
    assert not env.spawner.calls
    assert current(stack, post)['assigned_session'] == env.sid['codex']


@pytest.mark.parametrize('delivery_env', ['peer'], indirect=True)
@pytest.mark.parametrize('peer_state', ['idle', 'active', 'unknown', 'dirty'])
def test_dispatch_inspects_all_declared_branch_checkouts(delivery_env, peer_state):
    stack, worker, rule, post = delivery_env
    env = stack['env']
    if peer_state != 'unknown':
        probe(stack, 'grok', activity='active' if peer_state == 'active' else 'idle')
    if peer_state == 'dirty':
        (stack['peer_path'] / 'unfinished').write_text('preserve peer edits\n')
    before = git(stack['peer_path'], 'status', '--porcelain')
    worker.tick()
    assert len(env.spawner.calls) == (1 if peer_state == 'idle' else 0)
    assert git(stack['peer_path'], 'status', '--porcelain') == before
    assert git(stack['peer_path'], 'symbolic-ref', 'HEAD') == 'refs/heads/child-two'


def test_reserved_live_child_can_bind_after_acknowledgement_deadline(delivery_env):
    stack, worker, rule, post = delivery_env
    env = stack['env']
    worker.tick()
    run_id = worker.running['claude'].run_id
    version = current(stack, post)['version']
    env.clock.advance(stack['spec']['ack_seconds'] + 1)
    workstreams.reconcile(env.board, env.p['human'], None, post['id'], version,
                          fence=worker._fence())
    worker.tick()
    assert current(stack, post)['state'] == 'queued'
    assert current(stack, post)['version'] == version
    assert workstreams.get_for_post(env.board, post['id'])['dispatch_run_id'] == run_id
    sid = bind(stack, worker)
    assert current(stack, post)['assigned_session'] == sid
    assert current(stack, post)['state'] == 'queued'
    assert len(env.spawner.calls) == 1


def test_scan_recovers_lost_pending_trigger_past_mark_without_duplicate(delivery_env):
    stack, worker, rule, post = delivery_env
    env = stack['env']
    workstreams.tick(env.board, env.p['human'], fence=worker._fence())
    assert current(stack, post)['assigned_session'] == env.sid['claude']
    pending = worker._scan()
    assert any(item['post_id'] == post['id'] for item in pending.values())
    seq = env.board.get_post(env.p['human'], post['id'])['seq']
    assert worker._get(worker.MARK_KEY) >= seq
    # Crash after durable scan acknowledgement and pending removal, before a
    # delivery reservation exists: recovery must consult continuation ownership.
    worker._save(**{worker.PENDING_KEY: {}})
    assert workstreams.get_for_post(env.board, post['id'])['dispatch_run_id'] is None
    assert not worker._attempted(post['id'], 'claude')
    worker.tick()
    assert len(env.spawner.calls) == 1
    assert workstreams.get_for_post(env.board, post['id'])['dispatch_run_id'] is not None
    worker._save(**{worker.PENDING_KEY: {}})
    for _ in range(3):
        worker.tick()
    assert len(env.spawner.calls) == 1
    assert env.board.conn.execute('SELECT COUNT(*) FROM continuations').fetchone()[0] == 1
