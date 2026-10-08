"""Explicit request acknowledgements. Progress records never confer authorization."""
from __future__ import annotations

import json

from . import db
from .core import Conflict, Forbidden, Invalid, iso

STATES = ('queued', 'started', 'blocked', 'finished')


def for_post(board, post):
    """Legacy requests have virtual queued rows until their first explicit update."""
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


def progress(board, p, session_id, post_id, recipient, state, reason='', evidence_post_ids=None, expected_version=None):
    if expected_version is not None and (type(expected_version) is not int or expected_version < 0):
        raise Invalid('expected_version must be a nonnegative integer')
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
        if not p.is_human and p.name not in (post['agent'],row['assigned_agent']):
            raise Forbidden('only the author or assigned recipient may update a request')
        if not p.is_human and p.name == post['agent'] and p.name != row['assigned_agent'] and state not in ('finished','blocked'):
            raise Forbidden('only the assigned recipient may acknowledge execution')
        if not p.is_human and p.name == row['assigned_agent'] and row['assigned_session'] not in (None,session_id) and not (p.name == post['agent'] and state == 'finished'):
            raise Conflict('request is owned by another session')
        if row['state'] == 'started' and state == 'blocked' and (p.name != row['assigned_agent'] or session_id != row['assigned_session']):
            raise Conflict('only the executing session may release a started request as blocked')
        for pid in evidence:
            item = board.get_post(p,pid)
            if pid == post_id or item['thread_id'] != post['thread_id'] or item['sealed']:
                raise Invalid('evidence must be another unsealed post in the same thread')
        if row['state']==state and row['reason']==reason and row['evidence_post_ids']==evidence:
            return row
        if expected_version is not None and expected_version != row['version']:
            raise Conflict('request changed; reread before updating')
        if row['state']=='finished':
            raise Conflict('request is already finished; create a new request')
        if state == 'queued' and row['state'] != 'queued':
            raise Conflict('use explicit reassignment to queue a request again')
        owner = row['assigned_session']
        if p.name == row['assigned_agent']:
            owner = session_id
        _save(board,p,session_id,row,state,reason,evidence,row['assigned_agent'],owner)
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
        if row['assigned_session']==target_session_id and row['state']=='queued':
            return row
        attempts=board.conn.execute("SELECT COUNT(*) FROM request_events WHERE post_id=? AND recipient=? AND state='queued'",(post_id,recipient)).fetchone()[0]
        if attempts >= 3:
            raise Conflict('routing attempt limit reached; human review required')
        _save(board,p,session_id,row,'queued',reason.strip(),[],target['agent'],target_session_id)
    return next(r for r in board.get_post(p,post_id)['requests'] if r['recipient']==recipient)
