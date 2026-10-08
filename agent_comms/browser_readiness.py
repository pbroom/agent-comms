"""Browser probe reports are routing evidence, never host permission grants.

No browser/network/process operations live here. Callers must use their supported
browser adapter and report its result from the context that will execute the work.
"""
from __future__ import annotations

import json
from urllib.parse import urlsplit

from . import db, requests
from .core import Conflict, Forbidden, Invalid

PROBE_TTL = 300
LIVE_SECONDS = 90
MAX_RECONNECTS = 2
SCHEMA = """
CREATE TABLE IF NOT EXISTS browser_requirements (
 post_id INTEGER NOT NULL REFERENCES posts(id), recipient TEXT NOT NULL,
 target_url TEXT NOT NULL, origin TEXT NOT NULL, PRIMARY KEY(post_id,recipient)
);
CREATE TABLE IF NOT EXISTS browser_probes (
 session_id INTEGER NOT NULL REFERENCES sessions(id), target_url TEXT NOT NULL,
 project TEXT NOT NULL, worktree TEXT, execution_key TEXT NOT NULL,
 context TEXT NOT NULL, status TEXT NOT NULL, evidence TEXT NOT NULL,
 verified_at REAL NOT NULL, expires_at REAL NOT NULL, reconnects INTEGER NOT NULL DEFAULT 0,
 permission_epoch INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(session_id,target_url)
);
CREATE TABLE IF NOT EXISTS browser_permission_gates (
 project TEXT NOT NULL, origin TEXT NOT NULL, denied INTEGER NOT NULL,
 epoch INTEGER NOT NULL, reason TEXT NOT NULL, PRIMARY KEY(project,origin)
);
CREATE TABLE IF NOT EXISTS browser_events (
 id INTEGER PRIMARY KEY AUTOINCREMENT, project TEXT NOT NULL, origin TEXT NOT NULL,
 session_id INTEGER NOT NULL REFERENCES sessions(id), actor TEXT NOT NULL,
 action TEXT NOT NULL, evidence TEXT NOT NULL, created_at REAL NOT NULL
);
"""


def target(value):
    if not isinstance(value, str) or not value or len(value) > 2048 or any(c.isspace() for c in value):
        raise Invalid('target_url must be an absolute HTTP(S) URL without whitespace')
    try:
        u = urlsplit(value)
        if u.scheme not in ('http', 'https') or not u.hostname or u.username or u.password:
            raise ValueError()
        port = u.port or (443 if u.scheme == 'https' else 80)
        host = u.hostname.lower()
        if ':' in host:
            host = '[' + host + ']'
        origin = f'{u.scheme}://{host}:{port}'
    except ValueError:
        raise Invalid('invalid browser target URL') from None
    return value, origin


def _text(value, name, limit=4096):
    if not isinstance(value, str) or not value.strip() or len(value.encode()) > limit:
        raise Invalid(f'{name} is required and must be within {limit} bytes')
    return value.strip()


def _key(session):
    # A desktop connection is never inherited by a subsequently dispatched process.
    return session['dispatch_run_id'] or session['client_session_id'] or f"session:{session['id']}"


def _gate(board, project, origin):
    row = board.conn.execute('SELECT * FROM browser_permission_gates WHERE project=? AND origin=?',
                             (project, origin)).fetchone()
    return dict(row) if row else {'denied': False, 'epoch': 0}


def _event(board, p, sid, project, origin, action, evidence):
    board.conn.execute('INSERT INTO browser_events(project,origin,session_id,actor,action,evidence,created_at) VALUES (?,?,?,?,?,?,?)',
                       (project, origin, sid, p.name, action, evidence, board.now()))


def _request(board, p, sid, post_id, recipient):
    post, row = requests._context(board, p, sid, post_id, recipient)
    if not p.is_human and p.name not in (post['agent'], row['assigned_agent']):
        raise Forbidden('only the author or assigned recipient may configure browser readiness')
    if row['state'] == 'finished':
        raise Conflict('request is already finished')
    return post, row


def bind_request(board, p, session_id, post_id, recipient, target_url):
    url, origin = target(target_url)
    with db.write_tx(board.conn):
        post, row = _request(board, p, session_id, post_id, recipient)
        old = requirement(board, post_id, recipient)
        if old and old['target_url'] != url:
            raise Conflict('browser target is immutable; create a new request for another target')
        if row['state'] == 'started':
            raise Conflict('block running work before binding browser requirements')
        board.conn.execute('INSERT OR IGNORE INTO browser_requirements VALUES (?,?,?,?)',
                           (post_id, recipient, url, origin))
    return dict(requirement(board, post_id, recipient))


def requirement(board, post_id, recipient):
    return board.conn.execute('SELECT * FROM browser_requirements WHERE post_id=? AND recipient=?',
                              (post_id, recipient)).fetchone()


def _context(value, session):
    if not isinstance(value, dict) or set(value) != {'kind', 'transport', 'connection_id'}:
        raise Invalid('context needs kind, transport and connection_id only')
    if value['kind'] not in ('desktop', 'headless'):
        raise Invalid('context kind must be desktop or headless')
    if session['dispatch_run_id'] and value['kind'] == 'desktop':
        raise Invalid('dispatched process cannot attest another desktop context')
    return {k: _text(v, k, 200) for k, v in value.items()}


def report_probe(board, p, session_id, target_url, context, evidence):
    url, origin = target(target_url)
    with db.write_tx(board.conn):
        board._check_agent_write(p)
        s = board._session(p, session_id)
        ctx = _context(context, s)
        gate = _gate(board, s['project'], origin)
        if gate['denied']:
            raise Conflict('browser permission denied; supported human permission change must be recorded first')
        if not isinstance(evidence, dict) or set(evidence) != {'http_status', 'rendered_url', 'rendered_identity', 'interaction', 'interaction_result'}:
            raise Invalid('probe requires HTTP status, rendered URL/identity and harmless interaction/result')
        if type(evidence['http_status']) is not int or not 200 <= evidence['http_status'] < 300:
            raise Invalid('probe must report a successful HTTP response')
        if evidence['rendered_url'] != url:
            raise Invalid('rendered target must match the requested URL exactly')
        for k in ('rendered_identity', 'interaction', 'interaction_result'):
            _text(evidence[k], k, 1000)
        now = board.now()
        board.conn.execute('''INSERT INTO browser_probes VALUES (?,?,?,?,?,?,?,?,?,?,0,?)
            ON CONFLICT(session_id,target_url) DO UPDATE SET project=excluded.project,worktree=excluded.worktree,
            execution_key=excluded.execution_key,context=excluded.context,status=excluded.status,evidence=excluded.evidence,
            verified_at=excluded.verified_at,expires_at=excluded.expires_at,reconnects=0,permission_epoch=excluded.permission_epoch''',
            (session_id,url,s['project'],s['worktree'],_key(s),json.dumps(ctx),'ready',json.dumps(evidence),now,now+PROBE_TTL,gate['epoch']))
        _event(board,p,session_id,s['project'],origin,'probe',json.dumps(evidence))
    return {'status': 'ready', 'session_id': session_id, 'target_url': url,
            'expires_at': now + PROBE_TTL, 'authority': 'self_reported_probe_not_authorization'}


def report_failure(board, p, session_id, target_url, context, failure, evidence):
    url, origin = target(target_url)
    if failure not in ('policy_denied', 'disconnected', 'unreachable', 'browser_missing', 'host_permission', 'render_failed', 'interaction_failed'):
        raise Invalid('unknown browser failure kind')
    detail = _text(evidence, 'failure evidence')
    with db.write_tx(board.conn):
        board._check_agent_write(p)
        s = board._session(p,session_id)
        ctx = _context(context,s)
        gate = _gate(board,s['project'],origin)
        if failure in ('policy_denied', 'host_permission'):
            board.conn.execute('''INSERT INTO browser_permission_gates VALUES (?,?,1,1,?)
                ON CONFLICT(project,origin) DO UPDATE SET denied=1,epoch=epoch+1,reason=excluded.reason''',
                (s['project'],origin,detail))
        board.conn.execute('''INSERT INTO browser_probes VALUES (?,?,?,?,?,?,?,?,?,?,0,?)
            ON CONFLICT(session_id,target_url) DO UPDATE SET project=excluded.project,worktree=excluded.worktree,
            execution_key=excluded.execution_key,context=excluded.context,status=excluded.status,evidence=excluded.evidence,expires_at=0''',
            (session_id,url,s['project'],s['worktree'],_key(s),json.dumps(ctx),failure,detail,board.now(),0,gate['epoch']))
        _event(board,p,session_id,s['project'],origin,failure,detail)
        # Invalidate running work for this context; a denial affects every context at this origin.
        affected = board.conn.execute('''SELECT b.post_id,b.recipient FROM browser_requirements b
            JOIN posts p ON p.id=b.post_id JOIN threads t ON t.id=p.thread_id
            WHERE t.project=? AND b.origin=?''', (s['project'],origin)).fetchall()
        for req in affected:
            raw = board.conn.execute('SELECT * FROM posts WHERE id=?',(req['post_id'],)).fetchone()
            row = next(r for r in requests.for_post(board,raw) if r['recipient']==req['recipient'])
            if row['state'] != 'finished' and (failure in ('policy_denied','host_permission') or row['assigned_session']==session_id):
                requests._save(board,p,session_id,row,'blocked',f'Browser {failure}: {detail}',[],row['assigned_agent'],row['assigned_session'])
    return {'status': 'blocked', 'failure': failure, 'human_action_required': failure in ('policy_denied','host_permission')}


def record_permission_change(board, p, session_id, project, origin_url, evidence, expected_epoch):
    """Human records an actual host change. This function cannot change host policy."""
    _, origin = target(origin_url)
    detail = _text(evidence, 'supported host permission change evidence')
    if not p.is_human:
        raise Forbidden('only the human can record a supported browser permission change')
    with db.write_tx(board.conn):
        board._session(p,session_id)
        gate = _gate(board,project,origin)
        if type(expected_epoch) is not int or gate['epoch'] != expected_epoch or not gate['denied']:
            raise Conflict('permission gate changed; reread before recording the host change')
        board.conn.execute('UPDATE browser_permission_gates SET denied=0,epoch=epoch+1,reason=? WHERE project=? AND origin=?',
                           (detail,project,origin))
        _event(board,p,session_id,project,origin,'permission_change',detail)
    return {'status': 'fresh_probe_required', 'permission_granted_by_board': False}


def readiness(board, session_id, target_url):
    url, origin = target(target_url)
    s = board.conn.execute('SELECT s.*,a.active FROM sessions s JOIN agents a ON a.name=s.agent WHERE s.id=?',(session_id,)).fetchone()
    if not s:
        return 'missing_session'
    if _gate(board,s['project'],origin)['denied']:
        return 'policy_denied'
    probe = board.conn.execute('SELECT * FROM browser_probes WHERE session_id=? AND target_url=?',(session_id,url)).fetchone()
    if not probe:
        return 'missing_probe'
    if probe['project'] != s['project'] or probe['worktree'] != s['worktree'] or probe['execution_key'] != _key(s):
        return 'context_changed'
    if probe['permission_epoch'] != _gate(board,s['project'],origin)['epoch']:
        return 'fresh_probe_required'
    if probe['status'] != 'ready':
        return probe['status']
    now = board.now()
    if not s['active'] or not now-LIVE_SECONDS <= s['last_seen'] <= now:
        return 'owner_unavailable'
    if not now-PROBE_TTL <= probe['verified_at'] <= now < probe['expires_at']:
        return 'stale_probe'
    return 'ready'


def eligible(board, session_id, post_id, recipient):
    req = requirement(board,post_id,recipient)
    return not req or readiness(board,session_id,req['target_url']) == 'ready'


def assert_request_ready(board, post_id, recipient, session_id):
    req = requirement(board,post_id,recipient)
    if req:
        status = readiness(board,session_id,req['target_url'])
        if status != 'ready':
            raise Conflict(f'browser preflight blocked: {status}; exact executing context needs a fresh complete probe')


def request_blocker(board, post_id, recipient):
    """Check before candidate selection; denial must never be rerouted around."""
    req = requirement(board,post_id,recipient)
    if not req:
        return None
    project = board.conn.execute('SELECT t.project FROM posts p JOIN threads t ON t.id=p.thread_id WHERE p.id=?',(post_id,)).fetchone()[0]
    gate = _gate(board,project,req['origin'])
    return 'browser policy denied; supported human permission change required' if gate['denied'] else None


def claim_reconnect(board, p, session_id, target_url, context):
    """Reserve at most two supported reconnects in the SAME context. No tool is run."""
    url, origin = target(target_url)
    with db.write_tx(board.conn):
        board._check_agent_write(p)
        s = board._session(p,session_id)
        ctx = _context(context,s)
        status = readiness(board,session_id,url)
        if status not in ('disconnected','stale_probe'):
            raise Conflict(f'reconnect not allowed for {status}')
        probe = board.conn.execute('SELECT * FROM browser_probes WHERE session_id=? AND target_url=?',(session_id,url)).fetchone()
        if json.loads(probe['context']) != ctx or probe['reconnects'] >= MAX_RECONNECTS:
            raise Conflict('same-context reconnect limit reached or context changed')
        board.conn.execute('UPDATE browser_probes SET reconnects=reconnects+1,status=\'disconnected\',expires_at=0 WHERE session_id=? AND target_url=?',
                           (session_id,url))
        _event(board,p,session_id,s['project'],origin,'reconnect_reserved',json.dumps(ctx))
    return {'attempt': probe['reconnects']+1, 'limit': MAX_RECONNECTS, 'fresh_probe_required': True}


def status(board, p, session_id, target_url):
    url, origin = target(target_url)
    s = board._session(p,session_id)
    return {'readiness': readiness(board,session_id,url), 'gate': _gate(board,s['project'],origin),
            'target_url': url, 'session_id': session_id, 'execution_key': _key(s)}
