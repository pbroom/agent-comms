"""Exact server-created recovery obligations, distinct from unfinished work.

Only human action code records these links, in the transaction that creates the
recovery request. A normal post, answer_to link, or prose cannot manufacture one.
"""
from __future__ import annotations

import json

from . import workstreams
from .core import Conflict, Forbidden, Invalid

PREFIX = 'request.recovery.'
# A session abandoned its work when its task lease expired at least this long ago and it has not been seen since the
# lease expired: ten minutes (the dashboard's GRACE_MINUTES) for a dispatcher session, whose run the dispatcher
# watches, and a full lease TTL for an interactive session, which may simply be quiet (abandon_grace).
ABANDON_GRACE_SECONDS = 10 * 60


def abandon_grace(board, session):
    """How long after its lease expired a silent session counts as abandoned: ten minutes for a session the dispatcher
    launched, a full lease TTL otherwise. A dispatcher session that was seen after its run ended (the human resumed
    that conversation interactively) is treated as interactive, and so is one whose run record is missing."""
    full = max(ABANDON_GRACE_SECONDS, board.s.lease_ttl_minutes * 60)
    if session is None or not session['dispatch_run_id']:
        return full
    record = board.conn.execute('SELECT value FROM board_state WHERE key=?',
                                ('dispatch.run.' + session['dispatch_run_id'],)).fetchone()
    try:
        run = json.loads(record['value']) if record else None
    except (TypeError, ValueError):
        run = None
    if not isinstance(run, dict):
        return full
    from .dispatch import ACTIVE
    if run.get('status') in ACTIVE:
        return ABANDON_GRACE_SECONDS
    ended = run.get('ended_at')
    if type(ended) in (int, float) and session['last_seen'] <= ended:
        return ABANDON_GRACE_SECONDS
    return full


RECLAIMED_PREFIX = 'session.abandoned.'   # board_state: <session id>.<task id> -> the lease a reclaim replaced


def note_reclaimed_lease(c, task, by, now):
    """Called inside claim_task's transaction when an expired lease of another session is reclaimed. Records the
    replaced lease (task, thread, expiry) only when the old session had not been seen since the lease expired, so
    abandonment() can still prove abandonment after the task changed owner. Server-stamped ids and times only."""
    old = c.execute('SELECT id, last_seen FROM sessions WHERE id=?', (task['owner_session'],)).fetchone()
    if old is None or old['last_seen'] > task['lease_expires_at']:
        return
    value = {'session_id': old['id'], 'task_id': task['id'], 'thread_id': task['thread_id'],
             'lease_expires_at': task['lease_expires_at']}
    c.execute('''INSERT INTO board_state(key,value,updated_by,updated_at) VALUES (?,?,?,?)
                 ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_by=excluded.updated_by,
                 updated_at=excluded.updated_at''',
              (f"{RECLAIMED_PREFIX}{old['id']}.{task['id']}", json.dumps(value), by, now))


def abandonment(board, old, thread_id):
    """Proof that session `old` abandoned its work in this thread, or None.

    Abandoned means all of: it holds no live task lease anywhere; a lease it held on a task in this thread (still
    held, or replaced by a reclaim, see note_reclaimed_lease) expired at least abandon_grace() ago; and the
    session has not been seen since that lease expired. A session that is still polling, or holds any live lease, is
    active, and one with no lease evidence is unknown: both stay protected. Metadata only."""
    if old is None:
        return None
    now = board.now()
    if board.conn.execute('SELECT 1 FROM tasks WHERE owner_session=? AND lease_expires_at>?', (old['id'], now)).fetchone():
        return None
    leases = [(r['id'], r['lease_expires_at']) for r in board.conn.execute(
        """SELECT id, lease_expires_at FROM tasks WHERE owner_session=? AND thread_id=?
           AND status IN ('working','blocked') AND lease_expires_at IS NOT NULL""", (old['id'], thread_id))]
    for (value,) in board.conn.execute('SELECT value FROM board_state WHERE key LIKE ?',
                                       (f"{RECLAIMED_PREFIX}{old['id']}.%",)):
        try:
            item = json.loads(value)
        except (TypeError, ValueError):
            continue
        if (isinstance(item, dict) and item.get('session_id') == old['id'] and item.get('thread_id') == thread_id
                and type(item.get('task_id')) is int and type(item.get('lease_expires_at')) in (int, float)):
            leases.append((item['task_id'], item['lease_expires_at']))
    grace = abandon_grace(board, old)
    for task_id, expired in sorted(leases, key=lambda x: x[1]):
        if expired + grace <= now and old['last_seen'] <= expired:
            return {'session_id': old['id'], 'task_id': task_id, 'lease_expires_at': expired}
    return None


def _ended_dispatcher(board, old, agent, thread_id):
    """The old session belongs to a dispatcher run of this agent on this thread that provably ended."""
    if not old['dispatch_run_id']:
        return False
    record = board.conn.execute('SELECT value FROM board_state WHERE key=?', ('dispatch.run.' + old['dispatch_run_id'],)).fetchone()
    try:
        run = json.loads(record['value']) if record else None
    except (TypeError, ValueError):
        run = None
    return (isinstance(run, dict) and run.get('agent') == agent and run.get('thread_id') == thread_id
            and run.get('status') in ('exited', 'gone', 'stopped', 'timeout', 'spawn_failed')
            and type(run.get('ended_at')) in (int, float)
            and old['last_seen'] <= run['ended_at'] <= board.now())


def record(board, p, recovery_post_id, recipient, sources, *, requires_diagnostics=False):
    """Link a new recovery acknowledgement to exact unfinished request versions."""
    from . import workstreams
    board._require_human(p, 'record recovery obligations')
    if type(requires_diagnostics) is not bool:
        raise Invalid('requires_diagnostics must be a boolean')
    if not board.conn.in_transaction:
        raise Invalid('recovery links require the creating human action transaction')
    post = board.get_post(p, recovery_post_id)
    identity = board.conn.execute('SELECT is_human FROM agents WHERE name=?', (post['agent'],)).fetchone()
    row = next((r for r in post['requests'] if r['recipient'] == recipient), None)
    if not identity or not identity['is_human'] or post['sealed'] or row is None or row['version'] != 0:
        raise Invalid('recovery must be a new unsealed human request')
    if not isinstance(sources, list) or not sources or len(sources) > 100:
        raise Invalid('recovery requires bounded exact source requests')
    seen = set()
    links = []
    for item in sources:
        if not isinstance(item, dict) or set(item) != {'post_id', 'recipient', 'version'}:
            raise Invalid('recovery source requires exact post, recipient and version')
        if type(item['post_id']) is not int or type(item['version']) is not int or item['version'] < 0:
            raise Invalid('invalid recovery source version or post')
        source = board.get_post(p, item['post_id'])
        key = (item['post_id'], item['recipient'])
        source_row = next((r for r in source['requests'] if r['recipient'] == item['recipient']), None)
        if (key in seen or source['id'] == post['id'] or source['thread_id'] != post['thread_id']
                or source['sealed'] or source_row is None or source_row['assigned_agent'] != row['assigned_agent']
                or source_row['state'] not in ('queued', 'blocked') or source_row['version'] != item['version']
                or workstreams.get_for_post(board, source['id']) is not None):
            raise Conflict('recovery source changed or is outside the exact recipient scope')
        # Recovery acknowledgements cannot themselves become work lineage.
        if board.conn.execute('SELECT 1 FROM board_state WHERE key LIKE ?', (PREFIX + str(source['id']) + '.%',)).fetchone():
            raise Conflict('recovery obligations cannot target other recovery obligations')
        seen.add(key)
        links.append(dict(item, picked_up=False))
    value = {'post_id': recovery_post_id, 'recipient': recipient, 'version': row['version'], 'sources': links, 'requires_diagnostics': requires_diagnostics}
    board.conn.execute('INSERT INTO board_state(key,value,updated_by,updated_at) VALUES (?,?,?,?)',
                       (PREFIX + str(recovery_post_id) + '.' + recipient, json.dumps(value), p.name, board.now()))


def _same_execution(board, item, row):
    """Follow a contiguous audited lifecycle without crossing reassignment."""
    if not item.get('picked_up') or row['assigned_session'] != item['pickup_session']:
        return False
    events = list(board.conn.execute(
        'SELECT * FROM request_events WHERE post_id=? AND recipient=? AND version>? AND version<=? ORDER BY version',
        (item['post_id'], item['recipient'], item['pickup_version'], row['version'])))
    return (len(events) == row['version'] - item['pickup_version'] and all(
        event['version'] == item['pickup_version'] + i + 1
        and event['assigned_session'] == item['pickup_session']
        and event['assigned_agent'] == row['assigned_agent']
        and event['state'] in ('started', 'blocked', 'finished')
        for i, event in enumerate(events)))


def after_pickup(board, p, session_id, source, previous):
    """Called after the guarded started transition under its existing write lock."""
    from . import requests, decision_actions
    if not board.conn.in_transaction:
        raise Invalid('recovery retirement requires an atomic pickup')
    current = next(r for r in board.get_post(p, source['id'])['requests'] if r['recipient'] == previous['recipient'])
    if (p.is_human or current['state'] != 'started' or current['assigned_agent'] != p.name
            or current['assigned_session'] != session_id or current['version'] != previous['version'] + 1):
        raise Forbidden('recovery retirement requires the verified executing session pickup')
    decision_actions.assert_execution_authorized(board, source['id'], p.name)
    for record in list(board.conn.execute('SELECT key,value FROM board_state WHERE key LIKE ?', (PREFIX + '%',))):
        link = json.loads(record['value'])
        if link.get('retired'):
            continue
        matching = [s for s in link['sources'] if s['post_id'] == source['id'] and s['recipient'] == previous['recipient']
                    and ((s['version'] == previous['version'] and not s['picked_up'])
                         or _same_execution(board, s, previous))]
        if not matching:
            continue
        raw = board.conn.execute('SELECT sealed FROM posts WHERE id=?', (link['post_id'],)).fetchone()
        if not raw or raw['sealed']:
            continue
        recovery = board.get_post(p, link['post_id'])
        row = next(r for r in recovery['requests'] if r['recipient'] == link['recipient'])
        if (recovery['sealed'] or recovery['thread_id'] != source['thread_id']
                or board._thread_row(recovery['thread_id'])['status'] != 'open'
                or row['version'] != link['version'] or row['state'] != 'queued' or row['assigned_session'] is not None):
            continue  # Explicit later action or another owner wins; never overwrite it.
        matching[0]['picked_up'] = True
        matching[0]['pickup_session'] = session_id
        matching[0]['pickup_version'] = current['version']
        ready = all(s['picked_up'] for s in link['sources'])
        for item in link['sources'] if ready else []:
            raw = board.conn.execute('SELECT sealed FROM posts WHERE id=?', (item['post_id'],)).fetchone()
            if not raw or raw['sealed']:
                ready = False
                break
            original = board.get_post(p, item['post_id'])
            work = next(r for r in original['requests'] if r['recipient'] == item['recipient'])
            if (original['sealed'] or work['state'] not in ('started', 'finished')
                    or not _same_execution(board, item, work)
                    or (work['state'] == 'finished' and not work['evidence_post_ids'])):
                ready = False
                break
            decision_actions.assert_execution_authorized(board, original['id'], work['assigned_agent'])
        diagnostics = _diagnostic_evidence(board, p, session_id, recovery, current['evidence_post_ids']) if link.get('requires_diagnostics') else []
        if ready and (not link.get('requires_diagnostics') or diagnostics):
            receipt = board.create_post(p, session_id, thread_id=source['thread_id'], type='status',
                body=f"Recovery acknowledgement #{recovery['id']}/{row['recipient']} retired after verified pickup of its explicitly linked requests. Their original work remains unfinished until separately evidenced completion.",
                refs=[{'kind': 'artifact', 'path': f"board:post/{s['post_id']}"} for s in link['sources']], _in_transaction=True)
            requests._save(board, p, session_id, row, 'finished', 'Obsolete recovery acknowledgement; verified original request pickup',
                           [receipt['id'], *diagnostics], row['assigned_agent'], row['assigned_session'])
            link['retired'] = {'receipt_post_id': receipt['id'], 'session_id': session_id, 'diagnostic_post_ids': diagnostics}
        board.conn.execute('UPDATE board_state SET value=?,updated_by=?,updated_at=? WHERE key=?',
                           (json.dumps(link), p.name, board.now(), record['key']))


class RecoveryWait(Conflict):
    """Ownership recovery is blocked for a transient reason (TRANSIENT_BLOCKERS): other work in the owner's checkout
    that ends by itself. Not a question for the human: the board recorded a recovery wait (WAIT_PREFIX) and the
    dispatcher relaunches the agent once the worktree is free (autorecover.py). `details` is machine-readable and
    goes into the HTTP error body and the MCP tool result."""

    def __init__(self, message: str, details: dict):
        super().__init__(message)
        self.details = details


class _PersistentBlock(Conflict):
    """A blocker only the human (or another explicit action) can clear: today's behavior, a plain conflict."""

    details = {'blocker_kind': 'persistent', 'retry': 'none'}


class _TransientBlock(Exception):
    def __init__(self, blocker: str, path: str, old_id: int):
        super().__init__(blocker)
        self.blocker, self.path, self.old_id = blocker, path, old_id


# Blockers that clear on their own: another session's activity or task lease in the owner's checkout, or an owner
# dispatcher run still going. Everything else _ownership_blocker returns (unfinished changes, an unfinished Git
# operation, another repository, an uninspectable checkout, unknown owner activity) and a browser denial is persistent.
SHARED_LEASE_ACTIVE = 'Old owner or another session still holds an active task lease'
TRANSIENT_BLOCKERS = frozenset({workstreams.OWNER_LEASE_ACTIVE, SHARED_LEASE_ACTIVE, workstreams.PEER_ACTIVITY,
                                workstreams.OWNER_RUN_ACTIVE})
WAIT_PREFIX = 'auto_recovery.wait.'     # <post id>.<recipient>: a recovery waiting for its owner worktree to be free
WAIT_RETRIES = 3                        # automatic relaunches per request per rolling day (autorecover.py)
WAIT_WINDOW_SECONDS = 24 * 3600
WAITING = ('waiting', 'relaunched')     # wait states that still wait on the board, not on the human
WAIT_NEXT_STEP = ('Do not ask the human: verifying an owner and taking over ownership within the existing scope is '
                  'routine and pre-authorized, and this blocker clears by itself. Mark the request you are working on '
                  'blocked with this reason and stop. The board relaunches you when the owner worktree is free.')


def blocker_kind(blocker: str) -> str:
    """'transient' for a blocker that clears by itself (TRANSIENT_BLOCKERS), else 'persistent'. Matches only the
    server's own constant texts, never agent-written text."""
    return 'transient' if blocker in TRANSIENT_BLOCKERS else 'persistent'


def wait_key(post_id: int, recipient: str) -> str:
    return f'{WAIT_PREFIX}{int(post_id)}.{recipient}'


def _get_state(conn, key):
    row = conn.execute('SELECT value FROM board_state WHERE key=?', (key,)).fetchone()
    try:
        value = json.loads(row['value']) if row else None
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _put_state(board, key, value, by):
    board.conn.execute("""INSERT INTO board_state(key,value,updated_by,updated_at) VALUES (?,?,?,?)
                          ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_by=excluded.updated_by,
                          updated_at=excluded.updated_at""", (key, json.dumps(value), by, board.now()))


def retries_used(wait: dict, now: float) -> list:
    """The automatic relaunches of this wait's request within the rolling window."""
    times = wait.get('retries') if isinstance(wait, dict) else None
    return ([t for t in times if type(t) in (int, float) and t > now - WAIT_WINDOW_SECONDS]
            if isinstance(times, list) else [])


def wait_live(board, wait: dict) -> bool:
    """The request this wait is about is still held by the same old session for the same agent, unfinished: nothing
    else recovered, reassigned or finished it."""
    from . import requests
    if not isinstance(wait, dict) or type(wait.get('post_id')) is not int:
        return False
    post = board.conn.execute('SELECT * FROM posts WHERE id=?', (wait['post_id'],)).fetchone()
    if post is None or post['sealed'] or post['thread_id'] != wait.get('thread_id'):
        return False
    row = next((r for r in requests.for_post(board, post) if r['recipient'] == wait.get('recipient')), None)
    return (row is not None and row['state'] != 'finished' and row['assigned_agent'] == wait.get('agent')
            and row['assigned_session'] == wait.get('old_session'))


def waits(conn):
    """Every recorded recovery wait, as (key, wait) pairs (an index range scan over board_state's primary key)."""
    end = WAIT_PREFIX[:-1] + chr(ord(WAIT_PREFIX[-1]) + 1)
    out = []
    for key, value in conn.execute('SELECT key, value FROM board_state WHERE key >= ? AND key < ? ORDER BY key',
                                   (WAIT_PREFIX, end)):
        try:
            wait = json.loads(value)
        except (TypeError, ValueError):
            continue
        if isinstance(wait, dict) and type(wait.get('post_id')) is int and isinstance(wait.get('agent'), str):
            out.append((key, wait))
    return out


def active_wait(board, agent: str, thread_id: int):
    """The live recovery wait of this agent on this thread (its latest recovery attempt returned a transient block
    and the board will retry), or None. Core uses it to refuse that agent's questions to the human meanwhile."""
    for _, wait in waits(board.conn):
        if (wait['agent'] == agent and wait.get('thread_id') == thread_id and wait.get('state') in WAITING
                and wait_live(board, wait)):
            return wait
    return None


def _record_wait(board, p, session_id, post_id, recipient, expected_version, old_id, blocker, path):
    """Durably record (or refresh) the recovery wait for this exact request version, in its own transaction (the
    recovery's own transaction rolled back). Keeps the retry history of an earlier wait on the same request, so the
    rolling budget (WAIT_RETRIES) holds across relaunches. Returns the stored wait."""
    from . import db, requests
    with db.write_tx(board.conn):
        post, row = requests._context(board, p, session_id, post_id, recipient)
        if row['version'] != expected_version or row['assigned_session'] != old_id or row['assigned_agent'] != p.name:
            raise Conflict('ownership recovery requires the exact current request version')
        now = board.now()
        key = wait_key(post_id, recipient)
        prev = _get_state(board.conn, key) or {}
        # What this blocked attempt leaves waiting too: the requests and tasks the requesting session holds on the
        # thread (it marks them blocked and stops) and the old owner's tasks there. The dashboard shows them as part
        # of the wait, not as separate stalls.
        held = [r['post_id'] for r in board.conn.execute(
            """SELECT q.post_id FROM request_progress q JOIN posts x ON x.id=q.post_id
               WHERE q.assigned_session=? AND x.thread_id=? AND q.state!='finished' AND q.post_id!=?
               ORDER BY q.post_id""", (session_id, post['thread_id'], post_id))]
        tasks = [t['id'] for t in board.conn.execute(
            """SELECT id FROM tasks WHERE thread_id=? AND owner_session IN (?,?) AND status IN ('working','blocked')
               ORDER BY id""", (post['thread_id'], session_id, old_id))]
        first = prev.get('first_recorded_at') if prev.get('state') in WAITING else None
        wait = {'post_id': post_id, 'recipient': recipient, 'version': row['version'], 'agent': p.name,
                'thread_id': post['thread_id'], 'old_session': old_id, 'session_id': session_id, 'worktree': path,
                'blocker': blocker, 'blocker_kind': 'transient', 'state': 'waiting', 'recorded_at': now,
                'first_recorded_at': first if type(first) in (int, float) else now,
                'retries': retries_used(prev, now), 'relaunch_post_ids': (prev.get('relaunch_post_ids') or [])[-10:],
                'covered_post_ids': held[:20], 'covered_task_ids': tasks[:20]}
        _put_state(board, key, wait, p.name)
    return wait


def _settle_wait(board, post_id, recipient, state, by, reason=None):
    """Mark a live wait for this request no longer waiting: 'resolved' (recovered) or 'persistent' (the latest
    attempt hit a persistent blocker, which goes to the human as before). Call inside a write transaction."""
    key = wait_key(post_id, recipient)
    wait = _get_state(board.conn, key)
    if wait is None or wait.get('state') not in WAITING:
        return
    wait.update(state=state, settled_at=board.now(), settled_reason=reason)
    _put_state(board, key, wait, by)


def transfer_ended_owner(board, p, session_id, post_id, recipient, expected_version):
    """Recover bookkeeping ownership only; browser binding and execution stay gated.

    Intended for an explicit supported recovery action, never implicit pickup.
    No capabilities, host permissions, browser probes or completion are created.

    The old owner must be a different session of the same agent that either belonged to a dispatcher run that
    provably ended, or provably abandoned its work (abandonment(): its task lease in this thread expired at least
    ABANDON_GRACE_SECONDS ago, it has not been seen since, and it holds no live lease). Only an abandoned owner's
    `started` request can be taken over; queued and blocked requests work for both. Active or unknown owners stay
    blocked. If an abandoned session comes back, it has lost the request the same way it lost its lease.

    A transient blocker (TRANSIENT_BLOCKERS: other work in the owner's checkout that ends by itself) raises
    RecoveryWait, only after every other check passed, and records a recovery wait the dispatcher retries
    (autorecover.py). A persistent blocker raises a plain Conflict, as before, and ends any wait for the request.
    """
    from . import db
    try:
        return _transfer(board, p, session_id, post_id, recipient, expected_version)
    except _TransientBlock as block:
        wait = _record_wait(board, p, session_id, post_id, recipient, expected_version, block.old_id,
                            block.blocker, block.path)
        used = len(retries_used(wait, board.now()))
        details = {'recovered': False, 'blocker': block.blocker, 'blocker_kind': 'transient', 'retry': 'automatic',
                   'recovery_wait': {'post_id': post_id, 'recipient': recipient, 'version': wait['version'],
                                     'retries_used': used, 'max_retries': WAIT_RETRIES},
                   'next_step': WAIT_NEXT_STEP}
        raise RecoveryWait(f'{block.blocker}. This blocker is transient: the board retries automatically '
                           f'(automatic retries used: {used} of {WAIT_RETRIES} per 24 hours). {WAIT_NEXT_STEP}',
                           details) from None
    except _PersistentBlock as exc:
        with db.write_tx(board.conn):
            _settle_wait(board, post_id, recipient, 'persistent', p.name, exc.message)
        raise


def _transfer(board, p, session_id, post_id, recipient, expected_version):
    import os
    from . import db, requests, decision_actions, browser_readiness
    with db.write_tx(board.conn):
        post, row = requests._context(board, p, session_id, post_id, recipient)
        if p.is_human or p.name != row['assigned_agent']:
            raise Forbidden('only the same assigned agent may recover ownership')
        if type(expected_version) is not int or row['version'] != expected_version:
            raise Conflict('ownership recovery requires the exact current request version')
        if row['state'] not in ('queued', 'blocked', 'started') or workstreams.get_for_post(board, post_id) is not None:
            raise Conflict('only queued, blocked or abandoned started unmanaged requests may recover ownership')
        old = board.conn.execute('SELECT * FROM sessions WHERE id=?', (row['assigned_session'],)).fetchone()
        if not old or old['id'] == session_id or old['agent'] != p.name:
            raise Conflict('ownership recovery requires a different ended dispatcher session or abandoned session '
                           'of the same agent')
        ended = row['state'] != 'started' and _ended_dispatcher(board, old, p.name, post['thread_id'])
        abandoned = None if ended else abandonment(board, old, post['thread_id'])
        if not ended and abandoned is None:
            raise Conflict('old owner is live, unknown, or outside this exact request: recovery needs an ended '
                           'dispatcher session, or a session whose task lease in this thread expired at least '
                           f'{int(abandon_grace(board, old)) // 60} minutes ago and that has not been seen since')
        blocker = _ownership_blocker(board, old, session_id, post, abandoned=abandoned is not None)
        transient = None
        if blocker:
            if blocker_kind(blocker) == 'persistent':
                raise _PersistentBlock(blocker)
            transient = blocker     # raised last: a relaunch must not run into a persistent blocker afterwards
        decision_actions.assert_execution_authorized(board, post_id, p.name)
        if post['task_id'] is not None:
            task = board._task_row(post['task_id'])
            if task['status'] in ('done', 'declined') or not board._task_authorization_active(task, p.name):
                raise Forbidden('linked task authorization is no longer active')
        # Sticky host denials are not ownership problems and cannot be cleared here.
        blocker = browser_readiness.request_blocker(board, post_id, recipient)
        if blocker:
            raise _PersistentBlock(blocker)
        if transient:
            # Nothing was written; the wait is recorded in its own transaction once this one rolled back.
            raise _TransientBlock(transient, os.path.realpath(old['worktree'] or old['project']), old['id'])
        how = 'abandoned' if abandoned else 'ended'
        requests._save(board, p, session_id, row, 'queued',
                       f'Bookkeeping ownership recovered from {how} session {old["id"]}; execution preflight still required',
                       row['evidence_post_ids'], row['assigned_agent'], session_id)
        after_route(board, p, post, row)
        _settle_wait(board, post_id, recipient, 'resolved', p.name, f'recovered by session {session_id}')
    return next(r for r in board.get_post(p, post_id)['requests'] if r['recipient'] == recipient)


def transient_blocker(board, wait: dict):
    """Read-only recheck for a recorded wait: the transient blocker that still holds the owner's worktree, or None
    once it is free. The same lease, peer-activity and dispatcher-run checks as _ownership_blocker, with no
    successor session to exclude (the relaunched run will be a new one): any live lease or active dispatcher run in
    the checkout, any live peer there without fresh idle evidence, or any active run of the same agent keeps it
    waiting. The full checks run again when the relaunched agent calls board_recover_request_owner."""
    import os
    from .dispatch import ACTIVE
    now = board.now()
    old = board.conn.execute('SELECT * FROM sessions WHERE id=?', (wait.get('old_session'),)).fetchone()
    path = wait.get('worktree')
    if old is None or not isinstance(path, str) or not path:
        return None
    for lease in board.conn.execute("""SELECT s.id,s.worktree,s.project FROM tasks t JOIN sessions s ON s.id=t.owner_session
                                       WHERE t.status IN ('working','blocked') AND t.lease_expires_at>?""", (now,)):
        if lease['id'] == old['id'] or os.path.realpath(lease['worktree'] or lease['project']) == path:
            return SHARED_LEASE_ACTIVE
    for peer in board.conn.execute('''SELECT s.*,a.state,a.recorded_at FROM sessions s
            JOIN agents identity ON identity.name=s.agent LEFT JOIN session_activity a ON a.session_id=s.id
            WHERE s.id!=? AND identity.active=1 AND identity.is_human=0 AND s.last_seen>=?''', (old['id'], now - 90)):
        if (os.path.realpath(peer['worktree'] or peer['project']) == path
                and (peer['state'] != 'idle' or peer['recorded_at'] is None or not now - 90 <= peer['recorded_at'] <= now)):
            return workstreams.PEER_ACTIVITY
    for (value,) in board.conn.execute("SELECT value FROM board_state WHERE key >= 'dispatch.run.' AND key < 'dispatch.run/'"):
        try:
            run = json.loads(value)
        except (TypeError, ValueError):
            continue
        if isinstance(run, dict) and run.get('status') in ACTIVE and (
                run.get('agent') == old['agent']
                or (isinstance(run.get('cwd'), str) and os.path.realpath(run['cwd']) == path)):
            return workstreams.OWNER_RUN_ACTIVE
    return None


def after_route(board, p, source, previous):
    """Carry exact source versions through guarded reassignment, never completion."""
    if not board.conn.in_transaction:
        raise Invalid('recovery lineage routing requires an atomic assignment')
    current = next(r for r in board.get_post(p, source['id'])['requests'] if r['recipient'] == previous['recipient'])
    if current['state'] != 'queued' or current['version'] != previous['version'] + 1:
        raise Conflict('recovery lineage requires the exact guarded assignment')
    for record in list(board.conn.execute('SELECT key,value FROM board_state WHERE key LIKE ?', (PREFIX + '%',))):
        link = json.loads(record['value'])
        if link.get('retired'):
            continue
        changed = False
        for item in link['sources']:
            if (item['post_id'] == source['id'] and item['recipient'] == previous['recipient']
                    and ((item['version'] == previous['version'] and not item['picked_up'])
                         or _same_execution(board, item, previous))):
                item['picked_up'] = False
                item['version'] = current['version']
                changed = True
        if changed:
            board.conn.execute('UPDATE board_state SET value=?,updated_by=?,updated_at=? WHERE key=?',
                               (json.dumps(link), p.name, board.now(), record['key']))


def _ownership_blocker(board, old, session_id, post, abandoned=False):
    """Allow this authorized successor's work, without treating peers as idle. An abandoned old owner counts as
    ended (it has been silent since its lease expired), and the successor's own dispatcher run is not the owner's."""
    import os
    from . import workstreams
    from .dispatch import ACTIVE
    current = board.conn.execute('SELECT * FROM sessions WHERE id=?', (session_id,)).fetchone()
    path = os.path.realpath(old['worktree'] or old['project'])
    same = path == os.path.realpath(current['worktree'] or current['project'])
    leases = list(board.conn.execute("SELECT * FROM tasks WHERE owner_session=? AND thread_id=? AND status='working' AND lease_expires_at>?",
                                     (session_id, post['thread_id'], board.now())))
    authorized = any(board._task_authorization_active(t, current['agent']) for t in leases)
    if not same or not authorized:
        if abandoned:
            return workstreams._inactive(board, old, {'post_id': post['id'], 'thread_id': post['thread_id']},
                                         abandoned=True, own_run_id=current['dispatch_run_id'])
        return workstreams._inactive(board, old, {'post_id': post['id'], 'thread_id': post['thread_id']})
    for lease in board.conn.execute("SELECT s.id,s.worktree,s.project FROM tasks t JOIN sessions s ON s.id=t.owner_session WHERE t.status IN ('working','blocked') AND t.lease_expires_at>?", (board.now(),)):
        if lease['id'] == old['id'] or (lease['id'] != session_id and os.path.realpath(lease['worktree'] or lease['project']) == path):
            return SHARED_LEASE_ACTIVE
    for peer in board.conn.execute('''SELECT s.*,a.state,a.recorded_at FROM sessions s
            JOIN agents identity ON identity.name=s.agent LEFT JOIN session_activity a ON a.session_id=s.id
            WHERE s.id NOT IN (?,?) AND identity.active=1 AND identity.is_human=0 AND s.last_seen>=?''',
            (old['id'],session_id,board.now()-90)):
        if (os.path.realpath(peer['worktree'] or peer['project']) == path
                and (peer['state'] != 'idle' or peer['recorded_at'] is None or not board.now()-90 <= peer['recorded_at'] <= board.now())):
            return workstreams.PEER_ACTIVITY
    for record in board.conn.execute("SELECT key,value FROM board_state WHERE key LIKE 'dispatch.run.%'"):
        run = json.loads(record['value'])
        current_run = (current['dispatch_run_id'] and record['key'] == 'dispatch.run.' + current['dispatch_run_id']
                       and run.get('agent') == current['agent'] and run.get('thread_id') == post['thread_id']
                       and isinstance(run.get('cwd'), str) and os.path.realpath(run['cwd']) == path)
        if current_run:
            continue
        if run.get('status') in ACTIVE and (run.get('agent') == old['agent'] or (isinstance(run.get('cwd'),str) and os.path.realpath(run['cwd']) == path)):
            return workstreams.OWNER_RUN_ACTIVE
    try:
        project = board._thread_row(post['thread_id'])['project']
        if os.path.realpath(workstreams._git(path,'rev-parse','--path-format=absolute','--git-common-dir')) != os.path.realpath(workstreams._git(project,'rev-parse','--path-format=absolute','--git-common-dir')):
            return 'Owner worktree belongs to another repository'
        # An owner that only went silent (not a proven-ended run) may have left work behind: never take over a
        # checkout with changes it did not commit, even as the authorized successor in the same worktree.
        if abandoned and workstreams._git(path,'status','--porcelain=v1','--untracked-files=all'):
            return 'Owner worktree contains unfinished changes; preserve it before takeover'
        gitdir = workstreams._git(path,'rev-parse','--absolute-git-dir')
        if any(os.path.exists(os.path.join(gitdir,name)) for name in ('MERGE_HEAD','CHERRY_PICK_HEAD','REVERT_HEAD','rebase-merge','rebase-apply','sequencer','index.lock')):
            return 'Owner worktree has an unfinished Git operation'
    except Conflict as exc:
        return str(exc)
    return None


def _diagnostic_evidence(board, p, session_id, recovery, evidence_ids):
    """Structured exact refs attest the recovery deliverables, never text matches."""
    found = {}
    created_at = board.conn.execute('SELECT created_at FROM posts WHERE id=?', (recovery['id'],)).fetchone()[0]
    for evidence_id in evidence_ids:
        raw = board.conn.execute('SELECT * FROM posts WHERE id=?', (evidence_id,)).fetchone()
        if (not raw or raw['sealed'] or raw['id'] <= recovery['id'] or raw['created_at'] < created_at
                or raw['thread_id'] != recovery['thread_id'] or raw['agent'] != p.name
                or raw['session_id'] != session_id or raw['type'] not in ('finding', 'proposal')):
            continue
        evidence = board.get_post(p, evidence_id)
        if any(ref.get('kind') == 'artifact' and ref.get('path') == f"board:post/{recovery['id']}"
               for ref in evidence['refs']):
            found[evidence['type']] = evidence_id
    return [found['finding'], found['proposal']] if set(found) == {'finding', 'proposal'} else []
