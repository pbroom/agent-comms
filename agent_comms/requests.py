"""Explicit request acknowledgements. Progress records never confer authorization."""
from __future__ import annotations

import json

from . import db
from .core import Conflict, Forbidden, Invalid, iso

STATES = ('queued', 'started', 'blocked', 'finished')


def for_post(board, post):
    """Legacy requests have virtual queued rows until their first explicit update."""
    from . import workstreams
    managed = workstreams.get_for_post(board, post['id'])
    if managed is not None:
        row = dict(board.conn.execute('SELECT * FROM request_progress WHERE post_id=? AND recipient=?',
                                     (post['id'], managed['recipient'])).fetchone())
        row['evidence_post_ids'] = json.loads(row['evidence_post_ids'])
        row['updated_at'] = iso(row['updated_at'])
        return [row]
    author = board.conn.execute('SELECT is_human FROM agents WHERE name=?', (post['agent'],)).fetchone()
    if post['type'] == 'decision' or not (post['needs_response'] or post['type'] in ('request','handoff','question') or (author and author['is_human'])):
        return []
    rows = {r['recipient']: dict(r) for r in board.conn.execute(
        'SELECT * FROM request_progress WHERE post_id=?', (post['id'],))}
    result = []
    for recipient in dict.fromkeys(json.loads(post['to_agents'])):
        identity = board.conn.execute('SELECT is_human FROM agents WHERE name=?', (recipient,)).fetchone()
        if recipient == post['agent'] or not identity or identity['is_human']:
            continue
        row = rows.get(recipient, dict(post_id=post['id'], recipient=recipient, state='queued',
            assigned_agent=recipient, assigned_session=None, reason='', evidence_post_ids='[]',
            version=0, updated_at=post['created_at']))
        row['evidence_post_ids'] = json.loads(row['evidence_post_ids'])
        row['updated_at'] = iso(row['updated_at'])
        result.append(row)
    return result


def _context(board, p, session_id, post_id, recipient):
    board._check_agent_write(p)
    session = board._session(p, session_id)
    post = board.get_post(p, post_id)  # applies sealed visibility
    thread = board._thread_row(post['thread_id'])
    if not p.is_human and session['project'] != thread['project']:
        raise Forbidden('request session must belong to the source project')
    if not p.is_human and thread['status'] != 'open':
        raise Conflict('thread is closed')
    row = next((r for r in post['requests'] if r['recipient'] == recipient), None)
    if row is None:
        raise Invalid('post has no response request for that original recipient')
    return post, row


def _save(board, p, session_id, row, state, reason, evidence, assigned_agent, assigned_session):
    now = board.now()
    version = row['version'] + 1
    board.conn.execute('''INSERT INTO request_progress
        (post_id,recipient,state,assigned_agent,assigned_session,reason,evidence_post_ids,version,updated_at)
        VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(post_id,recipient) DO UPDATE SET
        state=excluded.state,assigned_agent=excluded.assigned_agent,assigned_session=excluded.assigned_session,
        reason=excluded.reason,evidence_post_ids=excluded.evidence_post_ids,version=excluded.version,updated_at=excluded.updated_at''',
        (row['post_id'],row['recipient'],state,assigned_agent,assigned_session,reason,json.dumps(evidence),version,now))
    board.conn.execute('''INSERT INTO request_events
        (post_id,recipient,actor,session_id,state,assigned_agent,assigned_session,reason,evidence_post_ids,version,created_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)''',
        (row['post_id'],row['recipient'],p.name,session_id,state,assigned_agent,assigned_session,reason,json.dumps(evidence),version,now))
    seq = board.conn.execute('SELECT COALESCE(MAX(seq),0)+1 FROM posts').fetchone()[0]
    board.conn.execute('UPDATE posts SET seq=?,revised_at=? WHERE id=?',(seq,now,row['post_id']))


def _recover_blocked(board, p, session_id, post, row, state, evidence, expected_version, managed):
    """Terminal reconciliation only; never move execution ownership or grant work."""
    if managed is not None:
        raise Forbidden('managed continuations cannot use blocked request recovery')
    if p.is_human or p.name != row['assigned_agent']:
        raise Forbidden('only the same assigned agent may recover its blocked request')
    if row['state'] != 'blocked' or state != 'finished':
        raise Conflict('recovery only permits blocked to finished')
    if type(expected_version) is not int or expected_version != row['version']:
        raise Conflict('recovery requires the exact current request version')
    if not evidence:
        raise Invalid('recovery requires explicit same-thread verification evidence')
    old = board.conn.execute('SELECT * FROM sessions WHERE id=?',(row['assigned_session'],)).fetchone()
    if old is None or old['id'] == session_id or old['agent'] != p.name or not old['dispatch_run_id']:
        raise Conflict('recovery requires another session bound to an ended dispatcher run')
    record = board.conn.execute('SELECT value FROM board_state WHERE key=?',('dispatch.run.'+old['dispatch_run_id'],)).fetchone()
    try:
        run = json.loads(record['value']) if record else None
    except (ValueError,TypeError):
        run = None
    if (not isinstance(run,dict) or run.get('agent') != p.name or run.get('thread_id') != post['thread_id']
            or run.get('status') not in ('exited','gone','stopped','timeout','spawn_failed')
            or type(run.get('ended_at')) not in (int,float)
            or not old['last_seen'] <= run['ended_at'] <= board.now()):
        raise Conflict('old dispatcher run is live, unknown, or does not match this request')
    if board.conn.execute('SELECT 1 FROM tasks WHERE owner_session=? AND lease_expires_at>?',
                          (old['id'],board.now())).fetchone():
        raise Conflict('old session still holds an active task lease')
    updated = board.conn.execute('SELECT updated_at FROM request_progress WHERE post_id=? AND recipient=?',
                                 (post['id'],row['recipient'])).fetchone()
    fresh = False
    for pid in evidence:
        item = board.conn.execute('SELECT * FROM posts WHERE id=?',(pid,)).fetchone()
        if (item and item['id'] != post['id'] and item['thread_id'] == post['thread_id'] and not item['sealed']
                and item['agent'] == p.name and item['session_id'] == session_id
                and updated and item['created_at'] >= updated['updated_at']):
            fresh = True
    if not fresh:
        raise Invalid('recovery requires new verification evidence posted by this current session after the block')


def progress(board, p, session_id, post_id, recipient, state, reason='', evidence_post_ids=None, expected_version=None,
             completion=None, recover_blocked=False):
    from . import workstreams
    if expected_version is not None and (type(expected_version) is not int or expected_version < 0):
        raise Invalid('expected_version must be a nonnegative integer')
    if type(recover_blocked) is not bool:
        raise Invalid('recover_blocked must be a boolean')
    if state not in STATES:
        raise Invalid('state must be queued, started, blocked, or finished')
    if not isinstance(reason, str) or len(reason.encode()) > board.s.body_max_bytes:
        raise Invalid('invalid request reason')
    reason = reason.strip()
    evidence = evidence_post_ids or []
    if not isinstance(evidence,list) or len(evidence)>20 or any(type(i) is not int or i<=0 for i in evidence) or len(set(evidence)) != len(evidence):
        raise Invalid('evidence must contain at most 20 distinct post IDs')
    if state in ('blocked','finished') and not reason:
        raise Invalid('blocked and finished require an explicit reason')
    with db.write_tx(board.conn):
        post,row = _context(board,p,session_id,post_id,recipient)
        if state == 'started':
            from . import browser_readiness
            browser_readiness.assert_request_ready(board,post_id,recipient,session_id)
        managed = workstreams.get_for_post(board, post_id)
        if recover_blocked:
            _recover_blocked(board,p,session_id,post,row,state,evidence,expected_version,managed)
            reason = 'Terminal recovery from ended session '+str(row['assigned_session'])+': '+reason
            if len(reason.encode()) > board.s.body_max_bytes:
                raise Invalid('recovery reason exceeds the body limit')
        if managed is not None and state == 'finished' and not evidence:
            raise Invalid('managed completion requires same-thread evidence post IDs')
        if not p.is_human and p.name not in (post['agent'],row['assigned_agent']):
            raise Forbidden('only the author or assigned recipient may update a request')
        if not p.is_human and p.name == post['agent'] and p.name != row['assigned_agent'] and state not in ('finished','blocked'):
            raise Forbidden('only the assigned recipient may acknowledge execution')
        if not recover_blocked and not p.is_human and p.name == row['assigned_agent'] and row['assigned_session'] not in (None,session_id) and not (p.name == post['agent'] and state == 'finished'):
            raise Conflict('request is owned by another session')
        if row['state'] == 'started' and state == 'blocked' and (p.name != row['assigned_agent'] or session_id != row['assigned_session']):
            raise Conflict('only the executing session may release a started request as blocked')
        for pid in evidence:
            item = board.get_post(p,pid)
            if pid == post_id or item['thread_id'] != post['thread_id'] or item['sealed']:
                raise Invalid('evidence must be another unsealed post in the same thread')
        if row['state']==state and row['reason']==reason and row['evidence_post_ids']==evidence:
            if managed is not None:
                if p.name != row['assigned_agent'] or session_id != row['assigned_session']:
                    raise Forbidden('only the assigned continuation session may report progress')
                if expected_version != row['version']:
                    raise Conflict('managed continuation requires the current request version')
                if state == 'finished' and completion != json.loads(managed['completion']):
                    raise Conflict('completion evidence differs from the recorded result')
            return row
        if expected_version is not None and expected_version != row['version']:
            raise Conflict('request changed; reread before updating')
        if row['state']=='finished':
            raise Conflict('request is already finished; create a new request')
        if state == 'queued' and row['state'] != 'queued':
            raise Conflict('use explicit reassignment to queue a request again')
        workstreams.guard_progress(board,p,session_id,row,state,expected_version,completion)
        owner = row['assigned_session']
        if p.name == row['assigned_agent'] and not recover_blocked:
            owner = session_id
        _save(board,p,session_id,row,state,reason,evidence,row['assigned_agent'],owner)
        workstreams.after_save(board,p,session_id,row,state)
        if state == 'finished':
            from . import issues
            issues.reconcile_completed(board,p,session_id,post['thread_id'])
    return next(r for r in board.get_post(p,post_id)['requests'] if r['recipient']==recipient)


def history(board,p,post_id,recipient):
    post = board.get_post(p,post_id)
    if not any(r['recipient']==recipient for r in post['requests']):
        raise Invalid('unknown original request recipient')
    result=[]
    for r in board.conn.execute('SELECT * FROM request_events WHERE post_id=? AND recipient=? ORDER BY version',(post_id,recipient)):
        item=dict(r)
        item['evidence_post_ids']=json.loads(item['evidence_post_ids'])
        item['created_at']=iso(item['created_at'])
        result.append(item)
    return result


def assign(board,p,session_id,post_id,recipient,target_session_id,expected_version,reason,required_capabilities):
    from . import capabilities
    from . import workstreams
    if workstreams.get_for_post(board, post_id) is not None:
        raise Conflict('managed continuations require ownership inspection through board_route_request')
    if type(expected_version) is not int or expected_version < 0:
        raise Invalid('expected_version is required for reassignment')
    if not isinstance(reason,str) or not reason.strip() or len(reason.encode())>board.s.body_max_bytes:
        raise Invalid('a bounded reassignment reason is required')
    with db.write_tx(board.conn):
        post,row = _context(board,p,session_id,post_id,recipient)
        if not p.is_human and p.name not in (post['agent'],row['assigned_agent']):
            raise Forbidden('only the author or assigned recipient may route a request')
        if not p.is_human and p.name==row['assigned_agent'] and row['assigned_session'] not in (None,session_id):
            raise Conflict('request is owned by another session')
        if row['version'] != expected_version:
            raise Conflict('request changed; reread before routing')
        if row['state'] not in ('queued','blocked'):
            raise Conflict('only queued or blocked requests may be routed')
        target=board.conn.execute('SELECT * FROM sessions WHERE id=?',(target_session_id,)).fetchone()
        if target is None or target['agent'] not in post['to']:
            raise Forbidden('route only to an originally addressed agent')
        if post['task_id'] is not None:
            task=board.conn.execute('SELECT * FROM tasks WHERE id=?',(post['task_id'],)).fetchone()
            if task is None or not board._task_authorization_active(task,target['agent']):
                raise Forbidden('target lacks active authorization for the linked task')
        project=board._thread_row(post['thread_id'])['project']
        if not capabilities.eligible(board,target_session_id,project,required_capabilities):
            raise Conflict('target lacks fresh verified capabilities in this project')
        from . import browser_readiness
        if (capabilities.requires_browser(required_capabilities)
                and browser_readiness.requirement(board,post_id,recipient) is None):
            raise Conflict('bind the exact browser target before assigning browser work')
        browser_readiness.assert_request_ready(board,post_id,recipient,target_session_id)
        if row['assigned_session']==target_session_id and row['state']=='queued':
            return row
        attempts=board.conn.execute("SELECT COUNT(*) FROM request_events WHERE post_id=? AND recipient=? AND state='queued'",(post_id,recipient)).fetchone()[0]
        if attempts >= 3:
            raise Conflict('routing attempt limit reached; human review required')
        _save(board,p,session_id,row,'queued',reason.strip(),[],target['agent'],target_session_id)
    return next(r for r in board.get_post(p,post_id)['requests'] if r['recipient']==recipient)
