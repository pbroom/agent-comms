"""Fenced, explicitly authorized dependent-stack continuations.

Git observations are made by the server. Check receipts and idle declarations
remain explicit agent attestations, not independent proof of a remote CI run.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import threading

from . import capabilities, db, requests
from .core import Conflict, Forbidden, Invalid, iso


def get_for_post(board, post_id):
    return board.conn.execute('SELECT * FROM continuations WHERE post_id=?', (post_id,)).fetchone()


def get_for_task(board, task_id):
    return board.conn.execute('SELECT * FROM continuations WHERE task_id=?', (task_id,)).fetchone()


def out(row):
    if row is None:
        return None
    value = dict(row)
    for name in ('descendants', 'required_checks', 'required_capabilities', 'completion'):
        value[name] = json.loads(value[name]) if value[name] is not None else None
    for name in ('deadline', 'created_at'):
        value[name] = iso(value[name])
    value['check_evidence_authority'] = 'agent_attestation_not_independent_ci_verification'
    return value


def _strings(value, name):
    if (not isinstance(value, list) or not value or len(value) > 100
            or any(not isinstance(v, str) or not v.strip() or v != v.strip() or len(v) > 200 for v in value)
            or len(set(value)) != len(value)):
        raise Invalid(name + ' must be a nonempty list of distinct bounded strings')
    return sorted(value)


def validate_scope(scope):
    """Normalize immutable task scope without accessing a repository."""
    fields = {'fix_ref','descendants','agents','required_checks','required_capabilities'}
    if not isinstance(scope,dict) or set(scope) != fields:
        raise Invalid('continuation_scope requires exactly fix_ref, descendants, agents, required_checks and required_capabilities')
    value = {'fix_ref':scope['fix_ref']}
    for field in fields-{'fix_ref'}:
        value[field] = _strings(scope[field],field)
    for ref in [value['fix_ref'],*value['descendants']]:
        if (not isinstance(ref,str) or not ref.startswith('refs/heads/') or len(ref)>200
                or any(ord(char)<=32 or ord(char)==127 or char in '~^:?*[\\' for char in ref)
                or '..' in ref or '@{' in ref
                or any(not part or part.startswith('.') or part.endswith(('.', '.lock')) for part in ref.split('/'))):
            raise Invalid('continuation scope references must be full valid refs/heads/ branches')
    return value


# Reconciliation runs its Git inspections outside the write transaction (they can take seconds), recording each
# result, then re-evaluates under the lock replaying only those recorded results: the database checks see current
# state, and a decision that would need a Git call not recorded raises _NeedsInspection and is retried.
_git_memo = threading.local()


class _NeedsInspection(Exception):
    pass


def _git(path, *args):
    mode = getattr(_git_memo, 'mode', None)
    key = (str(path), args)
    if mode is not None and mode[0] == 'replay':
        if key not in mode[1]:
            raise _NeedsInspection()
        result = mode[1][key]
        if isinstance(result, Conflict):
            raise Conflict(result.message)
        return result
    try:
        out = _run_git(path, *args)
    except Conflict as exc:
        if mode is not None:
            mode[1][key] = exc
        raise
    if mode is not None:
        mode[1][key] = out
    return out


def _run_git(path, *args):
    try:
        result = subprocess.run(['git', '-C', path, *args], capture_output=True, text=True,
                                timeout=10, env={**os.environ, 'GIT_OPTIONAL_LOCKS': '0', 'GIT_TERMINAL_PROMPT': '0'})
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise Conflict('Git inspection unavailable') from exc
    if result.returncode:
        raise Conflict('Git inspection failed: ' + ' '.join(args[:2]))
    return result.stdout.strip()


def prepare(board, p, session_id, thread_id, config):
    """Validate inside the caller's post-creation transaction."""
    if not isinstance(config, dict):
        raise Invalid('continuation must be an object')
    required = {'owner_session', 'fallback_session', 'root_task_id', 'fix_commit', 'descendants',
                'required_checks', 'required_capabilities'}
    if set(config) - (required | {'ack_seconds'}) or required - set(config):
        raise Invalid('invalid continuation fields')
    value = dict(config)
    thread = board._thread_row(thread_id)
    caller = board._session(p, session_id)
    if caller['project'] != thread['project']:
        raise Forbidden('continuation must belong to the caller project')
    if thread['status'] != 'open':
        raise Conflict('thread is closed')
    for field in ('owner_session', 'fallback_session', 'root_task_id'):
        if type(value[field]) is not int or value[field] <= 0:
            raise Invalid(field + ' must be a positive integer')
    if value['owner_session'] == value['fallback_session']:
        raise Invalid('owner and fallback must be different sessions')
    if not isinstance(value['fix_commit'], str) or not re.fullmatch('[0-9a-f]{40}', value['fix_commit']):
        raise Invalid('fix_commit must be a full lowercase commit SHA')
    for field in ('descendants', 'required_checks', 'required_capabilities'):
        value[field] = _strings(value[field], field)
    for ref in value['descendants']:
        if not ref.startswith('refs/heads/') or _git(thread['project'], 'check-ref-format', ref):
            raise Invalid('descendants must be full refs/heads/ references')
    value['ack_seconds'] = value.get('ack_seconds', 120)
    if type(value['ack_seconds']) is not int or not 10 <= value['ack_seconds'] <= 3600:
        raise Invalid('ack_seconds must be between 10 and 3600')
    root = board._task_row(value['root_task_id'])
    if root['thread_id'] != thread_id:
        raise Forbidden('root task must belong to the continuation thread')
    if not board._task_authorization_active(root, p.name):
        raise Forbidden('caller lacks active root task authorization')
    for field in ('owner_session', 'fallback_session'):
        target = board.conn.execute('SELECT * FROM sessions WHERE id=?', (value[field],)).fetchone()
        if target is None or target['project'] != thread['project']:
            raise Forbidden('continuation targets must belong to the exact project')
        identity = board.conn.execute('SELECT * FROM agents WHERE name=?',(target['agent'],)).fetchone()
        if identity is None or not identity['active'] or identity['is_human']:
            raise Forbidden('continuation targets must be active nonhuman agents')
        if not board._task_authorization_active(root, target['agent']):
            raise Forbidden('continuation target lacks root task authorization')
    if not p.is_human:
        scope = json.loads(root['continuation_scope']) if root['continuation_scope'] else None
        if not isinstance(scope,dict):
            raise Forbidden('root task has no authorized structured continuation scope')
        for field in ('descendants','required_checks','required_capabilities'):
            if sorted(scope.get(field,[])) != value[field]:
                raise Forbidden('continuation exceeds authorized root '+field)
        target_agents = {board.conn.execute('SELECT agent FROM sessions WHERE id=?',(value[field],)).fetchone()[0]
                         for field in ('owner_session','fallback_session')}
        if not target_agents.issubset(set(scope.get('agents',[]))):
            raise Forbidden('continuation targets exceed authorized root agents')
        fix_ref = scope.get('fix_ref')
        if not isinstance(fix_ref,str) or not fix_ref.startswith('refs/heads/'):
            raise Forbidden('root scope has no valid fix reference')
        _git(thread['project'],'merge-base','--is-ancestor',value['fix_commit'],fix_ref)
    if _git(thread['project'], 'rev-parse', '--verify', value['fix_commit'] + '^{commit}') != value['fix_commit']:
        raise Invalid('fix commit is unavailable in the project')
    return value


def create(board, p, sid, post_id, config):
    """Create the dependent task and its single assignment in the post transaction."""
    post = board.conn.execute('SELECT * FROM posts WHERE id=?', (post_id,)).fetchone()
    root = board._task_row(config['root_task_id'])
    owner = board.conn.execute('SELECT * FROM sessions WHERE id=?', (config['owner_session'],)).fetchone()
    task_id = board._insert_task(board.conn, p, sid, post['thread_id'],
        title='Propagate fix ' + config['fix_commit'][:12],
        acceptance='Every declared descendant contains the fix and required checks pass at its exact head.',
        intends_files=[], depends_on=[root['id']], category=root['category'])
    board.conn.execute("UPDATE tasks SET status='accepted',authorization_source=?,authorization_grant_id=? WHERE id=?",
                       (root['authorization_source'], root['authorization_grant_id'], task_id))
    board.conn.execute('UPDATE posts SET task_id=? WHERE id=?', (task_id, post_id))
    now = board.now()
    board.conn.execute('''INSERT INTO continuations(post_id,thread_id,task_id,root_task_id,owner_session,
        fallback_session,recipient,fix_commit,descendants,required_checks,required_capabilities,ack_seconds,
        deadline,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
        (post_id,post['thread_id'],task_id,root['id'],config['owner_session'],config['fallback_session'],owner['agent'],
         config['fix_commit'],json.dumps(config['descendants']),json.dumps(config['required_checks']),
         json.dumps(config['required_capabilities']),config['ack_seconds'],now+config['ack_seconds'],now))
    row = dict(post_id=post_id,recipient=owner['agent'],version=0)
    requests._save(board,p,sid,row,'queued','Dependent restack assigned',[],owner['agent'],owner['id'])
    return out(get_for_post(board, post_id))


def guard_progress(board, p, sid, row, state, expected_version, completion=None):
    managed = get_for_post(board, row['post_id'])
    if managed is None:
        return
    from . import browser_readiness
    if (state in ('started','finished') and capabilities.requires_browser(json.loads(managed['required_capabilities']))
            and browser_readiness.requirement(board,row['post_id'],managed['recipient']) is None):
        raise Conflict('managed browser work requires an exact bound target')
    if type(expected_version) is not int or expected_version != row['version']:
        raise Conflict('managed continuation requires the current request version')
    if p.name != row['assigned_agent'] or sid != row['assigned_session']:
        raise Forbidden('only the assigned continuation session may report progress')
    if managed['dispatch_run_id'] is not None:
        session = board.conn.execute('SELECT dispatch_run_id FROM sessions WHERE id=?',(sid,)).fetchone()
        if session is None or session['dispatch_run_id'] != managed['dispatch_run_id']:
            raise Conflict('continuation is reserved for another dispatcher delivery')
    task = board._task_row(managed['task_id'])
    root = board._task_row(managed['root_task_id'])
    if not board._task_authorization_active(task, p.name) or not board._task_authorization_active(root,p.name):
        raise Forbidden('continuation authorization is inactive')
    if state != 'finished':
        return
    if (task['status'] != 'working' or task['owner_session'] != sid or task['lease_expires_at'] is None
            or task['lease_expires_at'] <= board.now()):
        raise Conflict('completion requires the current live dependent task lease')
    if not isinstance(completion, dict) or set(completion) != {'descendants'}:
        raise Invalid('completion requires structured descendant evidence')
    evidence = completion['descendants']
    refs = json.loads(managed['descendants'])
    if not isinstance(evidence, list) or len(evidence) != len(refs):
        raise Invalid('completion must cover every descendant exactly once')
    path = board._thread_row(managed['thread_id'])['project']
    seen = set()
    for item in evidence:
        if not isinstance(item, dict) or set(item) != {'ref','head','contains_fix','checks'}:
            raise Invalid('invalid descendant completion evidence')
        ref, head = item['ref'], item['head']
        if not isinstance(ref, str) or ref not in refs or ref in seen:
            raise Invalid('unexpected or duplicate descendant')
        seen.add(ref)
        if not isinstance(head, str) or not re.fullmatch('[0-9a-f]{40}',head) or item['contains_fix'] is not True:
            raise Invalid('descendant needs a full commit and explicit ancestry claim')
        if _git(path,'rev-parse','--verify',ref+'^{commit}') != head:
            raise Conflict('descendant head changed or is unavailable')
        _git(path,'merge-base','--is-ancestor',managed['fix_commit'],head)
        checks = item['checks']
        if not isinstance(checks,dict):
            raise Invalid('checks must be an object')
        for name in json.loads(managed['required_checks']):
            check = checks.get(name)
            if not isinstance(check,dict) or check.get('head') != head or check.get('status') != 'passed':
                raise Conflict('required check lacks a passing receipt at the descendant head')
    board.conn.execute('UPDATE continuations SET completion=?,blocker=? WHERE post_id=?',
                       (json.dumps(completion),'',row['post_id']))


def after_save(board, p, sid, row, state):
    managed = get_for_post(board,row['post_id'])
    if managed is None:
        return
    if state == 'started':
        board.conn.execute('DELETE FROM session_activity WHERE session_id=?',(sid,))
        task = board._task_row(managed['task_id'])
        deadline = max(board.now()+managed['ack_seconds'],task['lease_expires_at'] or 0)
        board.conn.execute("UPDATE continuations SET deadline=?,blocker='' WHERE post_id=?",(deadline,row['post_id']))
    if state == 'blocked':
        board.conn.execute('UPDATE continuations SET deadline=? WHERE post_id=?',(board.now(),row['post_id']))
    if state == 'finished':
        task = board._task_row(managed['task_id'])
        board.conn.execute("UPDATE tasks SET status='done',owner_agent=NULL,owner_session=NULL,lease_expires_at=NULL,updated_at=? WHERE id=?",
                           (board.now(),task['id']))
        board._event(board.conn,task['id'],'transition',task['status'],'done',p,sid,'Continuation descendants and check receipts accepted')


UNKNOWN_ACTIVITY = 'Owner activity is unknown; fresh explicit idle evidence is required'


def _inactive(board, session, managed):
    now = board.now()
    path = os.path.realpath(session['worktree'] or session['project'])
    active = board.conn.execute("""SELECT s.id,s.worktree,s.project FROM tasks t JOIN sessions s ON s.id=t.owner_session
        WHERE t.lease_expires_at>? AND t.status IN ('working','blocked')""",(now,))
    for lease in active:
        if lease['id']==session['id'] or os.path.realpath(lease['worktree'] or lease['project'])==path:
            return 'Owner still holds an active task lease'
    peers = board.conn.execute('''SELECT s.id,s.last_seen,s.worktree,s.project,a.state,a.recorded_at FROM sessions s
        LEFT JOIN session_activity a ON a.session_id=s.id
        JOIN agents identity ON identity.name=s.agent
        WHERE s.id!=? AND s.last_seen>=? AND identity.active=1 AND identity.is_human=0''',
        (session['id'],now-90))
    for peer in peers:
        if os.path.realpath(peer['worktree'] or peer['project']) != path:
            continue
        if (peer['state'] != 'idle' or peer['recorded_at'] is None
                or not now-90 <= peer['recorded_at'] <= now):
            return 'Another live session in the owner worktree has active or unknown activity'
    from .dispatch import ACTIVE
    for record in board.conn.execute("SELECT value FROM board_state WHERE key LIKE 'dispatch.run.%'"):
        run = json.loads(record['value'])
        if (run.get('status') in ACTIVE and
                (run.get('agent') == session['agent']
                 or (isinstance(run.get('cwd'),str) and os.path.realpath(run['cwd']) == path))):
            return 'Owner dispatcher run is active or unresolved'
    ended = False
    if session['dispatch_run_id']:
        record = board.conn.execute('SELECT value FROM board_state WHERE key=?',('dispatch.run.'+session['dispatch_run_id'],)).fetchone()
        if record:
            run = json.loads(record['value'])
            if run.get('status') in ACTIVE:
                return 'Owner dispatcher run is active or unresolved'
            ended = (run.get('status') in ('gone','stopped','timeout','exited')
                     and run.get('ended_at') is not None and session['last_seen'] <= run['ended_at'])
    activity = board.conn.execute('SELECT * FROM session_activity WHERE session_id=?',(session['id'],)).fetchone()
    last_start = board.conn.execute("SELECT MAX(created_at) FROM request_events WHERE post_id=? AND assigned_session=? AND state='started'",
                                    (managed['post_id'],session['id'])).fetchone()[0]
    last_work = board.conn.execute("SELECT MAX(at) FROM task_events WHERE session_id=? AND event IN ('claim','reclaim','renew')",
                                   (session['id'],)).fetchone()[0]
    minimum = max(last_start or 0,last_work or 0)
    idle = bool(activity and activity['state']=='idle' and max(now-90,minimum) <= activity['recorded_at'] <= now)
    if not ended and not idle:
        return UNKNOWN_ACTIVITY
    if not path or not os.path.isdir(path):
        return 'Owner worktree cannot be inspected'
    try:
        project = board._thread_row(managed['thread_id'])['project']
        common = _git(path,'rev-parse','--path-format=absolute','--git-common-dir')
        if os.path.realpath(common) != os.path.realpath(_git(project,'rev-parse','--path-format=absolute','--git-common-dir')):
            return 'Owner worktree belongs to another repository'
        if _git(path,'status','--porcelain=v1','--untracked-files=all'):
            return 'Owner worktree contains unfinished changes; preserve it before takeover'
        gitdir = _git(path,'rev-parse','--absolute-git-dir')
        if any(os.path.exists(os.path.join(gitdir,name)) for name in
               ('MERGE_HEAD','CHERRY_PICK_HEAD','REVERT_HEAD','rebase-merge','rebase-apply','sequencer','index.lock')):
            return 'Owner worktree has an unfinished Git operation'
    except Conflict as exc:
        return str(exc)
    return None


def _descendant_checkouts(board, managed):
    project = board._thread_row(managed['thread_id'])['project']
    refs = set(json.loads(managed['descendants']))
    try:
        listing = _git(project,'worktree','list','--porcelain','-z')
    except Conflict as exc:
        return str(exc)
    records, current = [], {}
    for field in listing.split('\0'):
        if not field:
            if current:
                records.append(current)
                current = {}
            continue
        key,_,value = field.partition(' ')
        current[key] = value
    if current:
        records.append(current)
    for record in records:
        if record.get('branch') not in refs:
            continue
        path = os.path.realpath(record.get('worktree',''))
        label = record['branch']+' at '+path+': '
        try:
            if not os.path.isdir(path):
                return label+'checkout is unavailable for inspection'
            if _git(path,'status','--porcelain=v1','--untracked-files=all'):
                return label+'checkout contains unfinished changes'
            gitdir = _git(path,'rev-parse','--absolute-git-dir')
            if any(os.path.exists(os.path.join(gitdir,name)) for name in
                   ('MERGE_HEAD','CHERRY_PICK_HEAD','REVERT_HEAD','rebase-merge','rebase-apply','sequencer','index.lock')):
                return label+'checkout has an unfinished Git operation'
        except Conflict as exc:
            return label+str(exc)
        sessions = [s for s in board.conn.execute('''SELECT s.* FROM sessions s JOIN agents a ON a.name=s.agent
                    WHERE s.project=? AND a.active=1 AND a.is_human=0''',(project,))
                    if os.path.realpath(s['worktree'] or s['project'])==path]
        if not sessions:
            return label+'checkout ownership is unknown; registered idle or ended owner evidence is required'
        # Every session at the checkout must be shown inactive: one idle or ended session says nothing about
        # another. The only ones skipped are long ended: unknown activity, yet not seen for a whole lease TTL
        # (so it holds no lease; _inactive has already ruled out a live lease, live peer or active run).
        stale_before = board.now() - board.s.lease_ttl_minutes * 60
        for s in sessions:
            reason = _inactive(board,s,managed)
            if reason == UNKNOWN_ACTIVITY and s['last_seen'] < stale_before:
                continue
            if reason:
                return label+reason
    return None


def reconcile(board, p, sid, post_id, expected_version, fence=None):
    """Route a due continuation: keep a healthy owner, record a blocker, or hand it to the verified fallback. The
    Git inspections run before the write transaction (see _git_memo); the decision is made under the lock."""
    memo: dict = {}
    for _ in range(3):
        _git_memo.mode = ('replay', memo)
        try:
            return _reconcile(board, p, sid, post_id, expected_version, fence)
        except _NeedsInspection:
            pass
        finally:
            _git_memo.mode = None
        memo.clear()   # each replay uses only the results of the inspection pass just before it
        _git_memo.mode = ('record', memo)
        try:
            _inspect(board, post_id)
        finally:
            _git_memo.mode = None
    return out(get_for_post(board, post_id))   # the state kept changing under inspection; the next pass retries


def _inspect(board, post_id):
    """Outside any transaction: run the Git inspections a reconciliation of this post can need (results recorded)."""
    managed = get_for_post(board, post_id)
    if managed is None:
        return
    row = board.conn.execute('SELECT assigned_session FROM request_progress WHERE post_id=? AND recipient=?',
                             (post_id, managed['recipient'])).fetchone()
    source = board.conn.execute('SELECT * FROM sessions WHERE id=?', (row['assigned_session'],)).fetchone() if row else None
    if source is not None:
        _inactive(board, source, managed)
    _descendant_checkouts(board, managed)


def _recheck_live(board, source, managed):
    """The owner and descendant Git checks again, live (not replayed), inside the caller's transaction. Only on the
    takeover path, which is rare; blocker outcomes rely on the recorded results and are re-inspected next pass."""
    mode, _git_memo.mode = getattr(_git_memo, 'mode', None), None
    try:
        return _inactive(board, source, managed) or _descendant_checkouts(board, managed)
    finally:
        _git_memo.mode = mode


def _healthy_owner(board, row, task):
    """The assigned session is working the task under a live lease: nothing to route or report."""
    return (row['state'] == 'started' and task['status'] == 'working' and row['assigned_session'] is not None
            and task['owner_session'] == row['assigned_session'] and task['lease_expires_at'] is not None
            and task['lease_expires_at'] > board.now())


def _reconcile(board, p, sid, post_id, expected_version, fence):
    with db.write_tx(board.conn):
        board._check_agent_write(p)
        if fence is not None:
            current = board.conn.execute('SELECT value FROM board_state WHERE key=?',(fence[0],)).fetchone()
            if current is None or current['value'] != fence[1]:
                raise Conflict('dispatcher ownership changed')
        managed = get_for_post(board,post_id)
        if managed is None:
            raise Invalid('post has no managed continuation')
        post = board.get_post(p,post_id)
        row = next(r for r in post['requests'] if r['recipient']==managed['recipient'])
        thread = board._thread_row(managed['thread_id'])
        if thread['status'] != 'open' or post['sealed']:
            raise Conflict('continuation thread must be open and visible')
        targets = [board.conn.execute('SELECT * FROM sessions WHERE id=?',(managed[name],)).fetchone()
                   for name in ('owner_session','fallback_session')]
        if not p.is_human:
            caller = board._session(p,sid)
            if caller['project'] != thread['project'] or p.name not in {post['agent'], *(s['agent'] for s in targets)}:
                raise Forbidden('only the project author or recorded owners may reconcile')
        elif sid is None:
            if board.is_paused():
                raise Conflict('automatic reconciliation is paused')
            agents = {s['agent'] for s in targets}
            if not any(rule['thread_id']==managed['thread_id'] and agents.issubset(set(rule['agents']))
                       for rule in _continuation_rules(board, p)):
                raise Forbidden('automatic reconciliation lacks current dispatch approval')
        if type(expected_version) is not int or row['version'] != expected_version:
            raise Conflict('continuation changed; reread before routing')
        if row['state']=='finished':
            return out(managed)
        if managed['dispatch_run_id']:
            from .dispatch import ACTIVE
            record = board.conn.execute('SELECT value FROM board_state WHERE key=?',
                ('dispatch.run.'+managed['dispatch_run_id'],)).fetchone()
            if record and json.loads(record['value']).get('status') in ACTIVE:
                # A reserved child can take longer than the acknowledgement window
                # to register. Its bounded dispatcher lifetime owns that timeout;
                # do not invalidate a live launch before it can bind its session.
                return out(managed)
        if row['state']!='blocked' and board.now() < managed['deadline']:
            return out(managed)
        task = board._task_row(managed['task_id'])
        if _healthy_owner(board, row, task):
            # Not a blocker and not news: follow the lease (no seq bump) and drop a stale blocker.
            board.conn.execute("UPDATE continuations SET deadline=?,blocker='' WHERE post_id=?",
                               (task['lease_expires_at'], post_id))
            return out(get_for_post(board, post_id))
        root = board._task_row(managed['root_task_id'])
        source = board.conn.execute('SELECT * FROM sessions WHERE id=?',(row['assigned_session'],)).fetchone()
        reason = _inactive(board,source,managed) if source else 'Assigned owner session is missing'
        if not reason:
            reason = _descendant_checkouts(board,managed)
        if not reason and managed['epoch'] >= 1:
            reason = 'Fallback acknowledgement deadline elapsed; no further automatic takeover is authorized'
        fallback = targets[1]
        from . import browser_readiness
        browser_blocker = browser_readiness.request_blocker(board,post_id,managed['recipient'])
        if (capabilities.requires_browser(json.loads(managed['required_capabilities']))
                and browser_readiness.requirement(board,post_id,managed['recipient']) is None):
            browser_blocker = 'Managed browser work requires an exact bound target before routing'
        if browser_blocker:
            reason = browser_blocker
        if not reason and not browser_readiness.eligible(board,fallback['id'],post_id,managed['recipient']):
            reason = 'Fallback requires a fresh browser probe for the exact request target'
        if not reason and (not board._task_authorization_active(task,fallback['agent'])
                           or not board._task_authorization_active(root,fallback['agent'])):
            reason = 'Fallback lacks active continuation authorization'
        if not reason and not capabilities.eligible(board,fallback['id'],thread['project'],json.loads(managed['required_capabilities'])):
            reason = 'Fallback needs fresh successful capability probes in the exact project'
        if not reason:
            # A takeover moves work off a checkout: the Git results recorded before the lock may be stale, so the
            # owner and descendant checks run once more, live, under the lock right before deciding.
            reason = _recheck_live(board, source, managed)
        if reason:
            if managed['blocker'] != reason:
                board.conn.execute('UPDATE continuations SET blocker=? WHERE post_id=?',(reason,post_id))
                seq = board.conn.execute('SELECT COALESCE(MAX(seq),0)+1 FROM posts').fetchone()[0]
                board.conn.execute('UPDATE posts SET seq=?,revised_at=? WHERE id=?',(seq,board.now(),post_id))
            if managed['epoch'] >= 1 and row['state'] == 'queued':
                requests._save(board,p,sid,row,'blocked',reason,[],row['assigned_agent'],row['assigned_session'])
            return out(get_for_post(board,post_id))
        # Both assignment and task fencing are in the same write transaction.
        board.conn.execute("UPDATE tasks SET owner_agent=NULL,owner_session=NULL,lease_expires_at=NULL,status='accepted',updated_at=? WHERE id=?",
                           (board.now(),task['id']))
        requests._save(board,p,sid,row,'queued','Owner unavailable; verified fallback assigned',[],fallback['agent'],fallback['id'])
        board.conn.execute("UPDATE continuations SET epoch=epoch+1,deadline=?,blocker='',completion=NULL WHERE post_id=?",
                           (board.now()+managed['ack_seconds'],post_id))
        return out(get_for_post(board,post_id))


def _continuation_rules(board, p):
    """Active dispatch approvals that can authorize continuation reconciliation. One-click rules (Unstick, Approve &
    launch; human_actions binds each to its post) authorize only the launch for their own post, never this."""
    from . import human_actions
    one_click = human_actions.one_click_rule_ids(board)
    return [rule for rule in board.active_dispatch_rules(p) if rule['id'] not in one_click]


# Automatic reconciliation of a continuation that stays blocked for the same reason backs off (per process): the
# dispatcher ticks every few seconds, and each pass inspects Git. A new reason, a takeover or a cleared blocker resets it.
TICK_BACKOFF_MIN, TICK_BACKOFF_MAX = 5, 300
def _backoff(board) -> dict:
    """post_id -> (next attempt, delay, blocker), kept on the Board (one per process and database)."""
    if not hasattr(board, "_continuation_backoff"):
        board._continuation_backoff = {}
    return board._continuation_backoff


def tick(board, p, fence=None):
    """Reconcile only continuations covered by current explicit dispatch approval."""
    if board.is_paused():
        return
    rules = _continuation_rules(board, p)
    now = board.now()
    due = board.conn.execute('SELECT * FROM continuations WHERE deadline<=? AND completion IS NULL',(now,)).fetchall()
    _tick_backoff = _backoff(board)
    for post_id in set(_tick_backoff) - {m["post_id"] for m in due}:
        del _tick_backoff[post_id]
    for managed in due:
        held = _tick_backoff.get(managed['post_id'])
        if held is not None and now < held[0] and managed['blocker'] == held[2]:
            continue
        agents = {r['agent'] for r in board.conn.execute('SELECT agent FROM sessions WHERE id IN (?,?)',
                  (managed['owner_session'],managed['fallback_session']))}
        if not any(rule['thread_id']==managed['thread_id'] and agents.issubset(set(rule['agents'])) for rule in rules):
            continue
        row = board.conn.execute('SELECT version FROM request_progress WHERE post_id=? AND recipient=?',
                                 (managed['post_id'],managed['recipient'])).fetchone()
        if row:
            try:
                result = reconcile(board,p,None,managed['post_id'],row['version'],fence=fence)
            except (Conflict,Forbidden):
                continue
            blocker = (result or {}).get('blocker') or ''
            if blocker and held is not None and held[2] == blocker:
                delay = min(held[1] * 2, TICK_BACKOFF_MAX)
            elif blocker:
                delay = TICK_BACKOFF_MIN
            else:
                _tick_backoff.pop(managed['post_id'], None)
                continue
            _tick_backoff[managed['post_id']] = (now + delay, delay, blocker)


def delivery(board, post_id, agent):
    """Read-only delivery preflight; reserve_delivery repeats it under the write lock."""
    managed = get_for_post(board, post_id)
    if managed is None or managed['epoch'] != 1 or managed['dispatch_run_id'] is not None:
        return None
    row = board.conn.execute('SELECT * FROM request_progress WHERE post_id=? AND recipient=?',
                             (post_id,managed['recipient'])).fetchone()
    if row['state'] != 'queued' or row['assigned_agent'] != agent:
        return None
    from . import browser_readiness
    if browser_readiness.request_blocker(board,post_id,managed['recipient']):
        return None
    if (capabilities.requires_browser(json.loads(managed['required_capabilities']))
            and browser_readiness.requirement(board,post_id,managed['recipient']) is None):
        return None
    session = board.conn.execute('SELECT * FROM sessions WHERE id=?',(row['assigned_session'],)).fetchone()
    thread = board._thread_row(managed['thread_id'])
    if (session is None or session['id'] != managed['fallback_session'] or thread['status'] != 'open'
            or not capabilities.eligible(board,session['id'],thread['project'],json.loads(managed['required_capabilities']))):
        return None
    if not browser_readiness.eligible(board,session['id'],post_id,managed['recipient']):
        return None
    for task_id in (managed['task_id'],managed['root_task_id']):
        if not board._task_authorization_active(board._task_row(task_id),agent):
            return None
    if _inactive(board,session,managed) or _descendant_checkouts(board,managed):
        return None
    return {'cwd': os.path.realpath(session['worktree'] or session['project']),
            'version': row['version'], 'epoch': managed['epoch']}


def _delivery_rule(board, managed, run):
    rules = board._dispatch_rows(run['rule_id'])
    if not rules:
        raise Forbidden('continuation dispatch approval was removed')
    rule = rules[0]
    from . import human_actions
    if rule['id'] in human_actions.one_click_rule_ids(board):
        raise Forbidden('a one-click launch approval covers only its own post')
    target = board._dispatch_target(rule)
    # The last launch can have spent the remaining budget; revocation and expiry
    # still prevent binding. No new budget is consumed by registration.
    if (board._dispatch_state(rule,target) not in ('active','exhausted')
            or rule['thread_id'] != managed['thread_id'] or run['agent'] not in target['agents']):
        raise Forbidden('continuation dispatch approval is no longer valid')
    agents = {s[0] for s in board.conn.execute('SELECT agent FROM sessions WHERE id IN (?,?)',
              (managed['owner_session'],managed['fallback_session']))}
    if not agents.issubset(set(target['agents'])):
        raise Forbidden('dispatch approval does not cover both recorded owners')


def reserve_delivery(board, p, post_id, run_record, fence):
    """Fence the existing fallback before spawning, recording the run atomically.

    If spawning crashes ambiguously the run stays unresolved, never relaunchable.
    The dispatcher recovers it through its existing process identity protocol.
    """
    board._require_human(p,'run the dispatcher')
    with db.write_tx(board.conn):
        if board.is_paused():
            raise Conflict('continuation delivery is paused')
        current = board.conn.execute('SELECT value FROM board_state WHERE key=?',(fence[0],)).fetchone()
        if current is None or current['value'] != fence[1]:
            raise Conflict('dispatcher ownership changed')
        eligible = delivery(board,post_id,run_record['agent'])
        if (eligible is None or eligible['version'] != run_record.get('continuation_version')
                or eligible['epoch'] != run_record.get('continuation_epoch')
                or eligible['cwd'] != os.path.realpath(run_record['cwd'])):
            raise Conflict('continuation delivery preflight changed')
        managed = get_for_post(board,post_id)
        _delivery_rule(board,managed,run_record)
        run_id = run_record['run_id']
        board.conn.execute("INSERT INTO board_state(key,value,updated_by,updated_at) VALUES (?,?,'dispatcher',?)",
                           ('dispatch.run.'+run_id,json.dumps(run_record),board.now()))
        board.conn.execute('UPDATE continuations SET dispatch_run_id=? WHERE post_id=?',(run_id,post_id))


def _run_record(board, run_id):
    record = board.conn.execute('SELECT value FROM board_state WHERE key=?', ('dispatch.run.'+run_id,)).fetchone()
    try:
        run = json.loads(record['value']) if record else None
    except (TypeError, ValueError):
        run = None
    return run if isinstance(run, dict) else None


def _reservation_ended(board, run_id):
    """A delivery reservation whose run is over (or has no record) and never registered a session: nothing can
    bind it any more, so it only locks every other session out of the continuation."""
    from .dispatch import ACTIVE
    run = _run_record(board, run_id)
    if run is not None and run.get('status') in ACTIVE:
        return False
    return board.conn.execute('SELECT 1 FROM sessions WHERE dispatch_run_id=?', (run_id,)).fetchone() is None


def release_ended_deliveries(board, p):
    """Dispatcher tick: clear delivery reservations whose worker exited (or failed to start) before registering, so
    the assigned fallback session is no longer refused as 'reserved for another dispatcher delivery'. It does not
    relaunch: the failed run still counts as this delivery's attempt (see reset_delivery)."""
    board._require_human(p, 'run the dispatcher')
    with db.write_tx(board.conn) as c:
        released = 0
        for managed in c.execute('SELECT post_id,dispatch_run_id FROM continuations WHERE dispatch_run_id IS NOT NULL').fetchall():
            if _reservation_ended(board, managed['dispatch_run_id']):
                released += c.execute('UPDATE continuations SET dispatch_run_id=NULL WHERE post_id=? AND dispatch_run_id=?',
                                      (managed['post_id'], managed['dispatch_run_id'])).rowcount
        return released


def _delivery_run_ids(board, managed):
    """The continuation's reserved run plus every recorded dispatcher run for its post."""
    post_id = managed['post_id']
    run_ids = [managed['dispatch_run_id']] if managed['dispatch_run_id'] else []
    for (value,) in board.conn.execute("SELECT value FROM board_state WHERE key LIKE 'dispatch.run.%'").fetchall():
        try:
            run = json.loads(value)
        except ValueError:
            continue
        if isinstance(run, dict) and post_id in run.get('request_ids', []) and run.get('run_id') not in run_ids:
            run_ids.append(run.get('run_id'))
    return run_ids


def delivery_reset(board, managed):
    """Human-only dashboard metadata for reset_delivery: {'reserved', 'resettable'}. `resettable` when a fallback
    delivery is stuck (its run is still reserved, or ended without registering and was not reset yet, so the
    dispatcher will not try again) and nothing for this post is active: when reset_delivery would act. Ids and
    states only; reset_delivery repeats every check under the write lock."""
    reserved = managed['dispatch_run_id'] is not None
    row = board.conn.execute('SELECT state FROM request_progress WHERE post_id=? AND recipient=?',
                             (managed['post_id'], managed['recipient'])).fetchone()
    thread = board.conn.execute('SELECT status FROM threads WHERE id=?', (managed['thread_id'],)).fetchone()
    if (managed['epoch'] < 1 or row is None or row['state'] == 'finished'
            or thread is None or thread['status'] != 'open'):
        return {'reserved': reserved, 'resettable': False}
    run_ids = [r for r in _delivery_run_ids(board, managed) if isinstance(r, str)]
    if not run_ids or any(not _reservation_ended(board, r) for r in run_ids):
        return {'reserved': reserved, 'resettable': False}
    stuck = reserved or any(run is not None and not run.get('reset_by_human')
                            for run in (_run_record(board, r) for r in run_ids))
    return {'reserved': reserved, 'resettable': stuck}


def reset_delivery(board, p, session_id, post_id, expected_version):
    """Human only: retry a failed fallback delivery. Refused while its run is still active. Clears the reservation,
    marks the failed run as reset (so the dispatcher may make one new delivery attempt under its existing approval
    and budget), and puts a blocked assignment back in the fallback's queue with a fresh acknowledgement deadline.
    Ownership, authorization, capability and browser checks all run again before any launch."""
    board._require_human(p, 'reset a continuation delivery')
    with db.write_tx(board.conn) as c:
        board._session(p, session_id)
        managed = get_for_post(board, post_id)
        if managed is None:
            raise Invalid('post has no managed continuation')
        row = dict(c.execute('SELECT * FROM request_progress WHERE post_id=? AND recipient=?',
                             (post_id, managed['recipient'])).fetchone())
        if type(expected_version) is not int or row['version'] != expected_version:
            raise Conflict('continuation changed; reread before resetting its delivery')
        if row['state'] == 'finished':
            raise Conflict('continuation is already finished')
        if board._thread_row(managed['thread_id'])['status'] != 'open':
            raise Conflict('continuation thread must be open')
        if managed['epoch'] < 1:
            raise Conflict('no fallback delivery has been assigned yet')
        run_ids = _delivery_run_ids(board, managed)
        for run_id in run_ids:
            if isinstance(run_id, str) and not _reservation_ended(board, run_id):
                raise Conflict('the delivery run is still active or registered a session; stop it or let it finish first')
        for run_id in run_ids:
            run = _run_record(board, run_id) if isinstance(run_id, str) else None
            if run is not None and not run.get('reset_by_human'):
                run['reset_by_human'] = True
                c.execute('UPDATE board_state SET value=?,updated_at=? WHERE key=?',
                          (json.dumps(run), board.now(), 'dispatch.run.' + run_id))
        c.execute("UPDATE continuations SET dispatch_run_id=NULL,blocker='',deadline=? WHERE post_id=?",
                  (board.now() + managed['ack_seconds'], post_id))
        if row['state'] == 'blocked':
            fallback = c.execute('SELECT agent FROM sessions WHERE id=?', (managed['fallback_session'],)).fetchone()
            requests._save(board, p, session_id, row, 'queued', 'Human reset the failed fallback delivery', [],
                           fallback['agent'], managed['fallback_session'])
    return out(get_for_post(board, post_id))


def bind_delivery(board, p, sid, run_id):
    """Called in register_session's transaction; only the reserved child can bind."""
    managed = board.conn.execute('SELECT * FROM continuations WHERE dispatch_run_id=?',(run_id,)).fetchone()
    if managed is None:
        return
    if board.is_paused():
        raise Conflict('continuation delivery is paused')
    record = board.conn.execute('SELECT value FROM board_state WHERE key=?',('dispatch.run.'+run_id,)).fetchone()
    run = json.loads(record['value'])
    _delivery_rule(board,managed,run)
    session = board.conn.execute('SELECT * FROM sessions WHERE id=?',(sid,)).fetchone()
    if os.path.realpath(session['worktree'] or session['project']) != os.path.realpath(run['cwd']):
        raise Forbidden('continuation worker must register the reserved environment')
    row = dict(board.conn.execute('SELECT * FROM request_progress WHERE post_id=? AND recipient=?',
               (managed['post_id'],managed['recipient'])).fetchone())
    if row['assigned_session'] == sid:
        return
    if (row['version'] != run['continuation_version'] or managed['epoch'] != run['continuation_epoch']
            or row['state'] != 'queued' or row['assigned_agent'] != p.name):
        raise Conflict('continuation assignment changed before worker registration')
    for task_id in (managed['task_id'],managed['root_task_id']):
        if not board._task_authorization_active(board._task_row(task_id),p.name):
            raise Forbidden('continuation authorization is inactive')
    requests._save(board,p,sid,row,'queued','Verified fallback worker registered; probe access before claiming',[],p.name,sid)
    board.conn.execute('UPDATE continuations SET deadline=? WHERE post_id=?',
                       (board.now()+managed['ack_seconds'],managed['post_id']))
