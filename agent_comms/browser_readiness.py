"""Browser probe reports are routing evidence, never host permission grants.

No browser/network/process operations live here. Callers must use their supported
browser adapter and report its result from the context that will execute the work.
"""
from __future__ import annotations

import json
import re
import secrets
import ipaddress
from urllib.parse import unquote, urlsplit, urlunsplit

import idna

from . import db, requests
from .core import Conflict, Forbidden, Invalid, LimitExceeded, iso

PROBE_TTL = 300
LIVE_SECONDS = 90
MAX_RECONNECTS = 2
# Bounds on what agents can make the board store. Denial gates an agent records per project (a human-recorded
# change lifts a gate but never frees the agent's quota until the gate is gone); probe results and events
# are kept for a retention window. Attempts are keyed by origin + path, so query strings cannot multiply them.
MAX_GATES_PER_AGENT_PROJECT = 50
EVENT_RETENTION_SECONDS = 30 * 86400
PROBE_RETENTION_SECONDS = 7 * 86400
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
 epoch INTEGER NOT NULL, reason TEXT NOT NULL, created_by TEXT, PRIMARY KEY(project,origin)
);
CREATE TABLE IF NOT EXISTS browser_probe_attempts (
 session_id INTEGER NOT NULL REFERENCES sessions(id), target_url TEXT NOT NULL,
 attempt_id TEXT NOT NULL, context TEXT NOT NULL, execution_key TEXT NOT NULL,
 project TEXT NOT NULL, worktree TEXT, permission_epoch INTEGER NOT NULL, started_at REAL NOT NULL,
 PRIMARY KEY(session_id,target_url)
);
CREATE TABLE IF NOT EXISTS browser_events (
 id INTEGER PRIMARY KEY AUTOINCREMENT, project TEXT NOT NULL, origin TEXT NOT NULL,
 session_id INTEGER NOT NULL REFERENCES sessions(id), actor TEXT NOT NULL,
 action TEXT NOT NULL, evidence TEXT NOT NULL, created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS browser_events_time ON browser_events(created_at);
"""


# One domain label as stored: letters, digits, underscore and hyphen only (punycode `xn--` labels included), never a
# hyphen at either end. Anything else (`*`, `{`, `}`, `,`, `;`, `?`, DEL and other controls) is refused: a bound
# origin becomes a Playwright URL glob in a dispatched run's --allowed-origins, where `*` and `{a,b}` would widen it.
_LABEL = re.compile(r'[a-z0-9_](?:[a-z0-9_-]{0,61}[a-z0-9_])?')
_ORIGIN = re.compile(r'(https?)://(\[[0-9a-f:]+\]|[a-z0-9_.-]+)(?::([0-9]{1,5}))?')


def is_plain_origin(value):
    """True only for `http(s)://host[:port]` whose host is dotted plain labels, a canonical IPv4 address or a
    canonical bracketed IPv6 address. Used to drop anything else before it can reach a browser allowlist."""
    if not isinstance(value, str):
        return False
    m = _ORIGIN.fullmatch(value)
    if not m:
        return False
    host, port = m.group(2), m.group(3)
    if port is not None and int(port) > 65535:
        return False
    if host.startswith('['):
        try:
            return '[' + _ipv6_text(ipaddress.IPv6Address(host[1:-1])) + ']' == host
        except ValueError:
            return False
    labels = host.split('.')
    if labels[-1].isdigit():
        try:
            return str(ipaddress.IPv4Address(host)) == host
        except ValueError:
            return False
    return all(_LABEL.fullmatch(label) for label in labels)


def _ipv4_number(part):
    """One WHATWG IPv4 part (decimal, 0x hex or 0-prefixed octal), or None when it is not a number."""
    if part.startswith(('0x', '0X')):
        digits, base = part[2:], 16
    elif len(part) > 1 and part.startswith('0'):
        digits, base = part[1:], 8
    else:
        digits, base = part, 10
    if digits == '':
        return 0
    try:
        allowed = {8: '01234567', 10: '0123456789', 16: '0123456789abcdefABCDEF'}[base]
        return int(digits, base) if all(c in allowed for c in digits) else None
    except ValueError:
        return None


def _ipv6_text(addr):
    """WHATWG IPv6 serialization: lowercase hex pieces, the first longest run of two or more zero pieces as `::`,
    and no embedded dotted IPv4 (Python keeps `::ffff:127.0.0.1`; a browser shows `::ffff:7f00:1`)."""
    pieces = [(int(addr) >> (16 * (7 - i))) & 0xffff for i in range(8)]
    best, start = (0, -1), None
    for i, v in enumerate(pieces + [1]):
        if v == 0 and start is None:
            start = i
        elif v != 0 and start is not None:
            if i - start > best[0]:
                best = (i - start, start)
            start = None
    text = [format(v, 'x') for v in pieces]
    if best[0] < 2:
        return ':'.join(text)
    return ':'.join(text[:best[1]]) + '::' + ':'.join(text[best[1] + best[0]:])


def canonical_host(raw):
    """The host as a browser resolves it, so a deny gate cannot be sidestepped by spelling the same host differently:
    percent-decoded, IDNA-encoded the way browsers do (UTS #46, non-transitional: `faß.de` is `xn--fa-hia.de`, not
    `fass.de`; Unicode, full-width and mixed-case labels become one ASCII form; a non-ASCII host the `idna` package
    refuses is refused here too), lowercased,
    without a trailing dot, and IP literals in their one canonical form (WHATWG: `127.1`, `0x7f.0.0.1`,
    `2130706433` and `0177.0.0.1` are all 127.0.0.1; IPv6 is compressed and bracketed). Raises ValueError."""
    host = unquote(raw)
    if ':' in host:                                     # urlsplit strips the brackets of an IPv6 literal
        if '%' in host:
            raise ValueError('zone ids are not allowed')
        return '[' + _ipv6_text(ipaddress.IPv6Address(host)) + ']'
    if any(c in host for c in '\x00/\\?#@[]<>^|%') or any(ord(c) < 0x21 for c in host):
        raise ValueError('forbidden host character')
    if host.isascii():
        host = host.lower()
    else:
        try:
            host = idna.encode(host, uts46=True, transitional=False).decode('ascii').lower()
        except (idna.IDNAError, UnicodeError):
            raise ValueError('invalid international host name') from None
        if any(c in host for c in '\x00/\\?#@[]<>^|%:') or any(ord(c) < 0x21 for c in host):
            raise ValueError('forbidden host character')
    if host.endswith('.'):
        host = host[:-1]
    labels = host.split('.')
    if not host or '' in labels:
        raise ValueError('empty host label')
    last = labels[-1]
    if (last.isascii() and last.isdigit()) or _ipv4_number(last) is not None:   # WHATWG "ends in a number":
        # an IPv4 address, or an invalid host (`1.2.3.09`: 09 is not octal), never a domain name
        if len(labels) > 4:
            raise ValueError('invalid IPv4 address')
        nums = [_ipv4_number(x) for x in labels]
        if any(n is None for n in nums) or any(n > 255 for n in nums[:-1]) or nums[-1] >= 256 ** (5 - len(nums)):
            raise ValueError('invalid IPv4 address')
        value = nums[-1] + sum(n * 256 ** (3 - i) for i, n in enumerate(nums[:-1]))
        return str(ipaddress.IPv4Address(value))
    if not all(_LABEL.fullmatch(label) for label in labels):
        raise ValueError('host must be plain letters, digits, hyphens and dots')
    return host


def target(value):
    if not isinstance(value, str) or not value or len(value) > 2048 or any(c.isspace() for c in value):
        raise Invalid('target_url must be an absolute HTTP(S) URL without whitespace')
    try:
        u = urlsplit(value)
        if u.scheme not in ('http', 'https') or not u.hostname or u.username or u.password:
            raise ValueError()
        port = u.port if u.port is not None else (443 if u.scheme == 'https' else 80)   # :0 is not the default
        host = canonical_host(u.hostname)
        origin = f'{u.scheme}://{host}:{port}'
    except ValueError:
        raise Invalid('invalid browser target URL') from None
    default_port = 443 if u.scheme == 'https' else 80
    netloc = host if port == default_port else f'{host}:{port}'
    return urlunsplit((u.scheme,netloc,u.path or '/',u.query,u.fragment)), origin


def canonicalize_stored(conn):
    """Upgrade: re-key permission gates and browser requirements stored before hosts were canonicalized, so a deny
    recorded under one spelling (`EXAMPLE.com.`) still applies to the host. Merged gates keep the strictest state
    (denied if either was) and the newest epoch. Also adds the gates' created_by column (older gates have no
    recorded creator and count against no agent's quota). Runs inside init_schema's write transaction; idempotent."""
    if 'created_by' not in {r[1] for r in conn.execute('PRAGMA table_info(browser_permission_gates)')}:
        conn.execute('ALTER TABLE browser_permission_gates ADD COLUMN created_by TEXT')
    for row in conn.execute('SELECT * FROM browser_permission_gates').fetchall():
        try:
            origin = target(row['origin'])[1]
        except Invalid:
            continue
        if origin == row['origin']:
            continue
        conn.execute('''INSERT INTO browser_permission_gates(project,origin,denied,epoch,reason,created_by)
            VALUES (?,?,?,?,?,?) ON CONFLICT(project,origin) DO UPDATE SET denied=MAX(denied,excluded.denied),
            epoch=MAX(epoch,excluded.epoch)+1, reason=CASE WHEN excluded.denied THEN excluded.reason ELSE reason END''',
            (row['project'], origin, row['denied'], row['epoch'], row['reason'], row['created_by']))
        conn.execute('DELETE FROM browser_permission_gates WHERE project=? AND origin=?', (row['project'], row['origin']))
    for row in conn.execute('SELECT post_id, recipient, origin FROM browser_requirements').fetchall():
        try:
            origin = target(row['origin'])[1]
        except Invalid:
            continue
        if origin != row['origin']:
            conn.execute('UPDATE browser_requirements SET origin=? WHERE post_id=? AND recipient=?',
                         (origin, row['post_id'], row['recipient']))


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


def attempt_key(url):
    """The canonical origin + path a probe attempt is keyed on (query and fragment dropped)."""
    u = urlsplit(url)
    return urlunsplit((u.scheme, u.netloc, u.path or '/', '', ''))


def prune(board):
    """Retention, inside the caller's write transaction: events and finished probe results past their window,
    and attempts that can no longer be reported (older than PROBE_TTL)."""
    now = board.now()
    board.conn.execute('DELETE FROM browser_events WHERE created_at < ?', (now - EVENT_RETENTION_SECONDS,))
    board.conn.execute('DELETE FROM browser_probes WHERE verified_at < ? AND expires_at < ?',
                       (now - PROBE_RETENTION_SECONDS, now))
    board.conn.execute('DELETE FROM browser_probe_attempts WHERE started_at < ?', (now - PROBE_TTL,))


def _event(board, p, sid, project, origin, action, evidence):
    board.conn.execute('INSERT INTO browser_events(project,origin,session_id,actor,action,evidence,created_at) VALUES (?,?,?,?,?,?,?)',
                       (project, origin, sid, p.name, action, evidence, board.now()))


def _request(board, p, sid, post_id, recipient):
    post, row = requests._context(board, p, sid, post_id, recipient)
    if not p.is_human and p.name not in (post['agent'], row['assigned_agent']):
        raise Forbidden('only the author or assigned recipient may configure browser readiness')
    if not p.is_human and p.name == row['assigned_agent'] and row['assigned_session'] not in (None,sid):
        raise Conflict('request is owned by another session')
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
    return {k: _text(value[k], k, 200) for k in sorted(value)}


def begin_probe(board, p, session_id, target_url, context):
    """Issue a short-lived attempt fence before any authorized browser observation."""
    url, origin = target(target_url)
    with db.write_tx(board.conn):
        board._check_agent_write(p)
        s = board._session(p,session_id)
        ctx = _context(context,s)
        gate = _gate(board,s['project'],origin)
        if gate['denied']:
            raise Conflict('browser permission denied; do not probe or reconnect')
        attempt = secrets.token_hex(16)
        now = board.now()
        prune(board)
        board.conn.execute('INSERT OR REPLACE INTO browser_probe_attempts VALUES (?,?,?,?,?,?,?,?,?)',
            (session_id,attempt_key(url),attempt,json.dumps(ctx),_key(s),s['project'],s['worktree'],gate['epoch'],now))
    return {'attempt_id': attempt, 'expires_at': now+PROBE_TTL, 'permission_granted_by_board': False}


def report_probe(board, p, session_id, target_url, context, evidence, attempt_id):
    url, origin = target(target_url)
    with db.write_tx(board.conn):
        board._check_agent_write(p)
        s = board._session(p, session_id)
        ctx = _context(context, s)
        gate = _gate(board, s['project'], origin)
        if gate['denied']:
            raise Conflict('browser permission denied; supported human permission change must be recorded first')
        attempt = board.conn.execute('SELECT * FROM browser_probe_attempts WHERE session_id=? AND target_url=?',
                                     (session_id,attempt_key(url))).fetchone()
        now = board.now()
        if (not attempt or attempt['attempt_id'] != attempt_id
                or attempt['permission_epoch'] != gate['epoch']
                or attempt['execution_key'] != _key(s) or attempt['project'] != s['project']
                or attempt['worktree'] != s['worktree'] or json.loads(attempt['context']) != ctx
                or not now-PROBE_TTL < attempt['started_at'] <= now):
            raise Conflict('probe attempt superseded, expired or context changed; begin a new authorized probe')
        if not isinstance(evidence, dict) or set(evidence) != {'http_status', 'rendered_url', 'rendered_identity', 'interaction', 'interaction_result'}:
            raise Invalid('probe requires HTTP status, rendered URL/identity and harmless interaction/result')
        if type(evidence['http_status']) is not int or not 200 <= evidence['http_status'] < 300:
            raise Invalid('probe must report a successful HTTP response')
        if target(evidence['rendered_url'])[0] != url:
            raise Invalid('rendered target must match the requested URL exactly')
        for k in ('rendered_identity', 'interaction', 'interaction_result'):
            _text(evidence[k], k, 1000)
        expires = attempt['started_at'] + PROBE_TTL
        board.conn.execute('''INSERT INTO browser_probes VALUES (?,?,?,?,?,?,?,?,?,?,0,?)
            ON CONFLICT(session_id,target_url) DO UPDATE SET project=excluded.project,worktree=excluded.worktree,
            execution_key=excluded.execution_key,context=excluded.context,status=excluded.status,evidence=excluded.evidence,
            verified_at=excluded.verified_at,expires_at=excluded.expires_at,reconnects=0,permission_epoch=excluded.permission_epoch''',
            (session_id,url,s['project'],s['worktree'],_key(s),json.dumps(ctx),'ready',json.dumps(evidence),now,expires,gate['epoch']))
        board.conn.execute('UPDATE browser_probes SET reconnects=0 WHERE session_id=? AND context=?',(session_id,json.dumps(ctx)))
        board.conn.execute('DELETE FROM browser_probe_attempts WHERE session_id=? AND target_url=?',(session_id,attempt_key(url)))
        _event(board,p,session_id,s['project'],origin,'probe',json.dumps(evidence))
    return {'status': 'ready', 'session_id': session_id, 'target_url': url,
            'expires_at': expires, 'authority': 'self_reported_probe_not_authorization'}


# The two failure kinds that write a sticky, project-wide gate for the origin. They mean only that the browser or its
# host refused the bound origin itself (a site permission prompt declined, a host policy blocking that origin).
GATE_FAILURES = ('policy_denied', 'host_permission')
# Playwright MCP's refusal to write a file outside its output directory and workspace ("File access denied: <path> is
# outside allowed roots. Allowed roots: ..."). It is about a local file path, never about the origin, so it can
# never be a policy denial or host-permission failure. The phrase is Playwright's own and does not occur in a host
# denial; a report that quotes it is refused before it can block every launch to that origin.
FILE_PATH_REFUSAL = re.compile(r'outside\s+(?:the\s+)?allowed\s+roots', re.IGNORECASE)


def report_failure(board, p, session_id, target_url, context, failure, evidence):
    url, origin = target(target_url)
    if failure not in ('policy_denied', 'disconnected', 'unreachable', 'browser_missing', 'host_permission', 'render_failed', 'interaction_failed'):
        raise Invalid('unknown browser failure kind')
    detail = _text(evidence, 'failure evidence')
    if failure in GATE_FAILURES and FILE_PATH_REFUSAL.search(detail):
        raise Invalid(f'not a {failure}: "outside allowed roots" is the browser tool refusing a local file path, not '
                      'the browser or host refusing this origin. Save with a bare file name, copy the file from the '
                      'path the tool reports, and do not report it as a browser failure')
    with db.write_tx(board.conn):
        board._check_agent_write(p)
        s = board._session(p,session_id)
        ctx = _context(context,s)
        gate = _gate(board,s['project'],origin)
        if failure in GATE_FAILURES:
            exists = board.conn.execute('SELECT 1 FROM browser_permission_gates WHERE project=? AND origin=?',
                                        (s['project'], origin)).fetchone()
            if not exists and not p.is_human and board.conn.execute(
                    'SELECT COUNT(*) FROM browser_permission_gates WHERE project=? AND created_by=?',
                    (s['project'], p.name)).fetchone()[0] >= MAX_GATES_PER_AGENT_PROJECT:
                raise LimitExceeded(f'this agent already recorded {MAX_GATES_PER_AGENT_PROJECT} browser permission '
                                    'gates in this project; ask the human to review them')
            board.conn.execute('''INSERT INTO browser_permission_gates(project,origin,denied,epoch,reason,created_by)
                VALUES (?,?,1,1,?,?)
                ON CONFLICT(project,origin) DO UPDATE SET denied=1,epoch=epoch+1,reason=excluded.reason''',
                (s['project'],origin,detail,p.name))
        board.conn.execute('''INSERT INTO browser_probes VALUES (?,?,?,?,?,?,?,?,?,?,0,?)
            ON CONFLICT(session_id,target_url) DO UPDATE SET project=excluded.project,worktree=excluded.worktree,
            execution_key=excluded.execution_key,context=excluded.context,status=excluded.status,evidence=excluded.evidence,expires_at=0,permission_epoch=excluded.permission_epoch''',
            (session_id,url,s['project'],s['worktree'],_key(s),json.dumps(ctx),failure,detail,board.now(),0,gate['epoch']))
        board.conn.execute('DELETE FROM browser_probe_attempts WHERE session_id=? AND target_url=?',(session_id,attempt_key(url)))
        if failure in ('disconnected','browser_missing','host_permission'):
            board.conn.execute('UPDATE browser_probes SET status=?,evidence=?,expires_at=0 WHERE session_id=?',
                               (failure,detail,session_id))
            board.conn.execute('DELETE FROM browser_probe_attempts WHERE session_id=?',(session_id,))
        _event(board,p,session_id,s['project'],origin,failure,detail)
        # Invalidate running work for this context; a denial affects every context at this origin.
        affected = board.conn.execute('''SELECT b.post_id,b.recipient,b.origin FROM browser_requirements b
            JOIN posts p ON p.id=b.post_id JOIN threads t ON t.id=p.thread_id
            WHERE t.project=? AND (b.origin=? OR ?)''',
            (s['project'],origin,failure in ('disconnected','browser_missing','host_permission'))).fetchall()
        for req in affected:
            raw = board.conn.execute('SELECT * FROM posts WHERE id=?',(req['post_id'],)).fetchone()
            row = next(r for r in requests.for_post(board,raw) if r['recipient']==req['recipient'])
            if row['state'] != 'finished' and ((failure in ('policy_denied','host_permission') and req['origin']==origin) or row['assigned_session']==session_id):
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


def denied_gates(board, p):
    """Every sticky denied gate, for the human's dashboard: project, origin, the stored reason (agent-written evidence,
    untrusted text), who first recorded the gate, and the latest denial event (who and when), with the epoch the
    permission-change route expects."""
    if not p.is_human:
        raise Forbidden('only the human can list browser permission gates')
    out = []
    for g in board.conn.execute('SELECT * FROM browser_permission_gates WHERE denied=1 ORDER BY project, origin'):
        ev = board.conn.execute(f"""SELECT actor, action, created_at FROM browser_events WHERE project=? AND origin=?
            AND action IN ({",".join("?" * len(GATE_FAILURES))}) ORDER BY id DESC LIMIT 1""",
            (g['project'], g['origin'], *GATE_FAILURES)).fetchone()
        out.append({'project': g['project'], 'origin': g['origin'], 'reason': g['reason'], 'epoch': g['epoch'],
                    'created_by': g['created_by'], 'failure': ev['action'] if ev else None,
                    'recorded_by': ev['actor'] if ev else g['created_by'],
                    'recorded_at': iso(ev['created_at']) if ev else None})
    return {'gates': out}


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
    if not req:
        return True
    project = board.conn.execute('SELECT t.project FROM posts p JOIN threads t ON t.id=p.thread_id WHERE p.id=?',(post_id,)).fetchone()[0]
    s = board.conn.execute('SELECT project FROM sessions WHERE id=?',(session_id,)).fetchone()
    return bool(s and s['project'] == project and readiness(board,session_id,req['target_url']) == 'ready')


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
        used = board.conn.execute('SELECT MAX(reconnects) FROM browser_probes WHERE session_id=? AND context=?',
                                  (session_id,json.dumps(ctx))).fetchone()[0]
        if json.loads(probe['context']) != ctx or used >= MAX_RECONNECTS:
            raise Conflict('same-context reconnect limit reached or context changed')
        board.conn.execute('UPDATE browser_probes SET reconnects=?,status=\'disconnected\',expires_at=0 WHERE session_id=? AND context=?',
                           (used+1,session_id,json.dumps(ctx)))
        board.conn.execute('DELETE FROM browser_probe_attempts WHERE session_id=?',(session_id,))
        _event(board,p,session_id,s['project'],origin,'reconnect_reserved',json.dumps(ctx))
    return {'attempt': used+1, 'limit': MAX_RECONNECTS, 'fresh_probe_required': True}


def status(board, p, session_id, target_url):
    url, origin = target(target_url)
    s = board._session(p,session_id)
    return {'readiness': readiness(board,session_id,url), 'gate': _gate(board,s['project'],origin),
            'target_url': url, 'session_id': session_id, 'execution_key': _key(s)}


def has_ready_probe(board, session_id):
    """Generic browser capability names require actual context evidence too."""
    return any(readiness(board,session_id,row['target_url']) == 'ready' for row in
               board.conn.execute('SELECT target_url FROM browser_probes WHERE session_id=?',(session_id,)))
