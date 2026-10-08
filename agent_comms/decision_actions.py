"""Bounded mechanical actions selected by the human, never commands from post text."""
from __future__ import annotations

import json
from . import requests, workstreams
from .core import Conflict, Forbidden, Invalid


def validate(value):
    if not isinstance(value, dict):
        raise Invalid('option action must be an object')
    kind = value.get('type')
    fields = {'type', 'post_id', 'recipient', 'expected_version'}
    extras = {'close': {'evidence_post_ids'}, 'route': {'target_session_id', 'required_capabilities'},
              'repost': {'target_thread_id'}}
    if kind not in extras or set(value) != fields | extras[kind]:
        raise Invalid('action must be close, route, or repost with its exact fields')
    for field in ('post_id', 'target_session_id', 'target_thread_id'):
        if field in value and (type(value[field]) is not int or value[field] <= 0):
            raise Invalid(f'{field} must be a positive integer')
    if type(value['expected_version']) is not int or value['expected_version'] < 0:
        raise Invalid('action requires an exact nonnegative request version')
    if not isinstance(value['recipient'], str) or not value['recipient'].strip() or len(value['recipient']) > 32:
        raise Invalid('action requires an original request recipient')
    if kind == 'close':
        ids = value['evidence_post_ids']
        if not isinstance(ids, list) or not 1 <= len(ids) <= 20 or any(type(i) is not int or i <= 0 for i in ids) or len(set(ids)) != len(ids):
            raise Invalid('close requires distinct evidence post IDs')
    if kind == 'route':
        caps = value['required_capabilities']
        if not isinstance(caps, list) or not caps or len(caps) > 20 or any(not isinstance(c, str) or not c or len(c) > 100 for c in caps):
            raise Invalid('route requires explicit capabilities')
    return dict(value)


def execute(board, p, session_id, question, action, *, _authorization_decision_id=None):
    """Caller holds the transaction covering action and exact human answer."""
    if _authorization_decision_id is None:
        board._require_human(p, 'execute a decision action')
    if not board.conn.in_transaction:
        raise Invalid('decision action requires an atomic human answer')
    if board.is_paused():
        raise Conflict('board is paused')
    action = validate(action)
    source, row = requests._context(board, p, session_id, action['post_id'], action['recipient'])
    if source['thread_id'] != question['thread_id']:
        raise Forbidden('action source must belong to the question thread')
    if source['sealed'] or workstreams.get_for_post(board, source['id']) is not None:
        raise Forbidden('sealed or managed continuation requests need their dedicated workflow')
    if row['version'] != action['expected_version'] or row['state'] == 'finished':
        raise Conflict('request changed; propose a fresh decision with the current version')
    if board._thread_row(source['thread_id'])['status'] != 'open':
        raise Conflict('source thread is closed')
    if row['state'] == 'started':
        raise Conflict('executing requests must be released by their owner first')
    if row['assigned_session'] and board.conn.execute(
            'SELECT 1 FROM tasks WHERE owner_session=? AND lease_expires_at>?',
            (row['assigned_session'], board.now())).fetchone():
        raise Conflict('request owner still holds an active task lease')
    common = dict(board=board, p=p, session_id=session_id, post_id=source['id'], recipient=action['recipient'],
                  expected_version=action['expected_version'], _in_transaction=True)
    reason = (f"Authorized routing under human issue decision #{_authorization_decision_id}" if _authorization_decision_id is not None
              else f"Human selected mechanical {action['type']} from decision #{question['id']}")
    if action['type'] == 'close':
        return requests.progress(**common, state='finished', reason=reason, evidence_post_ids=action['evidence_post_ids'])
    if action['type'] == 'repost':
        assert_execution_authorized(board, source['id'], row['assigned_agent'])
    if action['type'] == 'route':
        return requests.assign(**common, target_session_id=action['target_session_id'], reason=reason,
                               required_capabilities=action['required_capabilities'])
    if row['state'] not in ('queued', 'blocked'):
        raise Conflict('only queued or blocked requests may be reposted')
    target = board._thread_row(action['target_thread_id'])
    original = board._thread_row(source['thread_id'])
    if target['status'] != 'open' or target['id'] == original['id']:
        raise Conflict('repost requires a different open target thread')
    # The signed-in human selects the exact source/destination shown in the UI.
    # This authorizes this move, never execution outside the source objective.
    # Agent-initiated routing still uses requests.assign and its existing gates.
    if source['task_id'] is not None:
        task = board.conn.execute('SELECT * FROM tasks WHERE id=?', (source['task_id'],)).fetchone()
        if not task or not board._task_authorizable(task, row['assigned_agent']):
            raise Forbidden('linked task authorization is no longer active')
    target_post = board.create_post(p, session_id, thread_id=target['id'], type='request', to=[row['assigned_agent']],
        needs_response=True, body=f"Routed request #{source['id']}/{action['recipient']} from thread #{source['thread_id']}. {reason}. Read that exact source and its existing authorization; no new scope or access is granted.",
        refs=[{'kind': 'artifact', 'path': f"board:post/{source['id']}"}], _in_transaction=True)
    target_row = {'post_id': target_post['id'], 'recipient': row['assigned_agent'], 'version': 0}
    requests._save(board, p, session_id, target_row, 'queued', 'Routed from exact source', [], row['assigned_agent'], None)
    # Routing is not completion. Preserve the original as explicitly blocked with
    # an audited destination, so separate work and old decisions remain intact.
    requests.progress(**common, state='blocked', reason=reason + f"; successor post #{target_post['id']} on thread #{target['id']}")
    link = {'source_post_id': source['id'], 'recipient': action['recipient'],
            'source_version': row['version'] + 1, 'successor_recipient': row['assigned_agent'],
            'authorization_decision_id': _authorization_decision_id, 'action_post_id': question['id']}
    board.conn.execute('INSERT INTO board_state(key,value,updated_by,updated_at) VALUES (?,?,?,?)',
        ('request.successor.' + str(target_post['id']), json.dumps(link), p.name, board.now()))
    return {'reposted_post_id': target_post['id'], 'target_thread_id': target['id']}


def repost(board, p, session_id, post_id, recipient, expected_version, target_thread_id):
    """Agent routing reuses a persisted exact human scope; peer prose is never authority."""
    from . import db
    with db.write_tx(board.conn):
        source, row = requests._context(board, p, session_id, post_id, recipient)
        if p.is_human:
            raise Forbidden('use the human decision action to select an exact move')
        if p.name != row['assigned_agent'] or row['assigned_session'] not in (None, session_id):
            raise Forbidden('only the current assigned agent may repost its request')
        target = board._thread_row(target_thread_id)
        origin = board._thread_row(source['thread_id'])
        authorized = None
        for event in board.conn.execute("SELECT id,scope,decision FROM issue_comments e WHERE kind='decision' AND outcome='approved' AND id=(SELECT MAX(x.id) FROM issue_comments x WHERE x.issue_id=e.issue_id AND x.kind='decision')"):
            scope = json.loads(event['scope'] or '[]')
            decision = json.loads(event['decision'] or '{}')
            if not {target['id'], origin['id']} <= {s['thread_id'] for s in scope}:
                continue
            ids = decision.get('issue_link_ids', [])
            if ids and board.conn.execute('SELECT 1 FROM issue_links WHERE id IN (SELECT value FROM json_each(?)) AND post_id=?',
                    (json.dumps(ids), post_id)).fetchone():
                authorized = event['id']
                break
        if not authorized:
            raise Forbidden('repost requires existing human scope covering this exact source and destination')
        return execute(board, p, session_id, source, {'type':'repost', 'post_id':post_id, 'recipient':recipient,
            'expected_version':expected_version, 'target_thread_id':target_thread_id}, _authorization_decision_id=authorized)


def reconcile_successor(board, p, session_id, successor, recipient):
    """Only explicit evidenced successor completion closes the linked original."""
    record = board.conn.execute('SELECT value FROM board_state WHERE key=?',
                               ('request.successor.' + str(successor['id']),)).fetchone()
    if not record:
        return
    link = json.loads(record['value'])
    if recipient != link['successor_recipient']:
        return
    raw = board.conn.execute('SELECT * FROM posts WHERE id=?', (link['source_post_id'],)).fetchone()
    if raw is None or raw['sealed'] or board._thread_row(raw['thread_id'])['status'] != 'open':
        return  # preserve later human closure/sealing; successor completion is independent
    source = board.get_post(p, link['source_post_id'])
    row = next(r for r in source['requests'] if r['recipient'] == link['recipient'])
    if row['version'] != link['source_version'] or row['state'] != 'blocked':
        return  # someone explicitly changed the original; never overwrite their decision
    receipt = board.create_post(p, session_id, thread_id=source['thread_id'], type='status',
        body=f"Successor request #{successor['id']}/{recipient} has explicit completion evidence; routed source #{source['id']}/{row['recipient']} is complete.",
        refs=[{'kind':'artifact','path':f"board:post/{successor['id']}"}], _in_transaction=True)
    requests._save(board,p,session_id,row,'finished',f"Completed through successor #{successor['id']}",
                   [receipt['id']],row['assigned_agent'],row['assigned_session'])
    reconcile_successor(board,p,session_id,source,row['recipient'])


def assert_execution_authorized(board, post_id, executor):
    """Recheck every carried scope at pickup; bookkeeping may still report reality."""
    seen = set()
    while True:
        if post_id in seen:
            raise Conflict('cyclic request lineage')
        seen.add(post_id)
        record = board.conn.execute('SELECT value FROM board_state WHERE key=?',
                                    ('request.successor.' + str(post_id),)).fetchone()
        if not record:
            return
        link = json.loads(record['value'])
        successor = board.conn.execute('SELECT * FROM posts WHERE id=?', (post_id,)).fetchone()
        source = board.conn.execute('SELECT * FROM posts WHERE id=?', (link['source_post_id'],)).fetchone()
        if source is None or source['sealed'] or board._thread_row(source['thread_id'])['status'] != 'open':
            raise Conflict('original request is sealed or its thread is closed')
        row = board.conn.execute('SELECT * FROM request_progress WHERE post_id=? AND recipient=?',
                                (source['id'], link['recipient'])).fetchone()
        if row is None or row['version'] != link['source_version'] or row['state'] != 'blocked':
            raise Conflict('original routed request changed; reread its current instructions')
        if source['task_id'] is not None:
            task = board.conn.execute('SELECT * FROM tasks WHERE id=?', (source['task_id'],)).fetchone()
            if not task or task['status'] in ('done', 'declined') or not board._task_authorization_active(task, executor):
                raise Forbidden('original task authorization is no longer active')
        approval = link.get('authorization_decision_id')
        if approval is not None:
            event = board.conn.execute('''SELECT * FROM issue_comments WHERE id=(
                SELECT MAX(id) FROM issue_comments WHERE kind='decision' AND issue_id=(
                    SELECT issue_id FROM issue_comments WHERE id=?))''', (approval,)).fetchone()
            scope = json.loads(event['scope'] or '[]') if event else []
            decision = json.loads(event['decision'] or '{}') if event else {}
            ids = decision.get('issue_link_ids', [])
            if (not event or event['outcome'] != 'approved'
                    or not {source['thread_id'], successor['thread_id']} <= {s['thread_id'] for s in scope}
                    or not ids or not board.conn.execute(
                        'SELECT 1 FROM issue_links WHERE id IN (SELECT value FROM json_each(?)) AND post_id=?',
                        (json.dumps(ids), source['id'])).fetchone()):
                raise Forbidden('original routing authorization is no longer active')
        post_id = source['id']
