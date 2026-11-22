"""Atomic explicit request replies; post text alone never changes execution state."""
from __future__ import annotations

import hashlib
import json

from . import db, requests, workstreams
from .core import Conflict, Forbidden, Invalid


def validate(value, key):
    if not isinstance(key, str) or not key.strip() or key != key.strip() or len(key) > 128:
        raise Invalid('request_reply requires a bounded nonempty idempotency_key')
    required = {'post_id', 'recipient', 'expected_version', 'state', 'reason'}
    if not isinstance(value, dict) or not required <= set(value) or set(value) - required - {'disposition', 'completion'}:
        raise Invalid('request_reply requires exact source, recipient, version, state and reason')
    if type(value['post_id']) is not int or value['post_id'] <= 0:
        raise Invalid('reply source must be a positive integer')
    if type(value['expected_version']) is not int or value['expected_version'] < 0:
        raise Invalid('reply requires an exact nonnegative request version')
    if not isinstance(value['recipient'], str) or not value['recipient'].strip() or len(value['recipient']) > 32:
        raise Invalid('reply requires an original recipient')
    if value['state'] not in ('started', 'blocked', 'finished') or not isinstance(value['reason'], str):
        raise Invalid('reply state must be started, blocked or finished with an explicit reason')
    if value['state'] == 'finished':
        if value.get('disposition') not in ('completed', 'superseded'):
            raise Invalid('finished reply requires completed or superseded disposition')
    elif 'disposition' in value:
        raise Invalid('only finished replies carry a disposition')
    if 'completion' in value and not isinstance(value['completion'], dict):
        raise Invalid('completion must be the managed completion object')
    return dict(value)


SUCCESSOR_PREFIX = 'request.successor.'
# The two kinds that still refuse, each naming the path its own workflow offers (DESIGN_NOTES "Superseding linked
# requests").
MANAGED_REFUSAL = ("managed continuation cannot be superseded through a reply: its workflow closes it only with "
                   "disposition='completed' and its completion receipts (every declared descendant contains the fix, "
                   "required checks passed)")
ISSUE_REFUSAL = ("cannot be superseded through a reply: its shared issue governs it. Record why it is obsolete on "
                 "that issue with board_comment_issue for the human; resolving the issue leaves this request open")


def _linked(board, post_id):
    """The post belongs to a human answer, a routed successor hand-off or a recovery acknowledgement."""
    if board.conn.execute('SELECT 1 FROM answer_links WHERE source_post_id=? OR answer_post_id=?', (post_id,post_id)).fetchone():
        return True
    for record in board.conn.execute("SELECT key,value FROM board_state WHERE key LIKE 'request.successor.%' OR key LIKE 'request.recovery.%'"):
        link = json.loads(record['value'])
        if (record['key'] == SUCCESSOR_PREFIX + str(post_id) or link.get('source_post_id') == post_id
                or link.get('post_id') == post_id or any(s.get('post_id') == post_id for s in link.get('sources', []))):
            return True
    return False


def open_successor(board, post_id, recipient):
    """(successor post id, recipient) of a routed hand-off of this exact request that is still open, or None. The
    successor carries this request's authorization (decision_actions.assert_execution_authorized): retiring the
    original under it would leave a live request that can never start. Only a durably finished successor row
    releases the original: an access-routed successor has no stored row until its first update (requests.for_post
    gives it a virtual queued one), and a closed thread can be reopened."""
    for record in list(board.conn.execute("SELECT key,value FROM board_state WHERE key LIKE 'request.successor.%'")):
        link = json.loads(record['value'])
        if link.get('source_post_id') != post_id or link.get('recipient') != recipient:
            continue
        successor = int(record['key'][len(SUCCESSOR_PREFIX):])
        post = board.conn.execute('SELECT * FROM posts WHERE id=?', (successor,)).fetchone()
        rows = requests.for_post(board, post) if post is not None else []
        if any(r['recipient'] == link.get('successor_recipient') and r['state'] != 'finished' for r in rows):
            return successor, link['successor_recipient']
    return None


def guard_superseded(board, p, post_id, row):
    """Who may retire this exact request as obsolete (DESIGN_NOTES "Superseding linked requests").

    Generic requests keep the ordinary update rules. Human answer lineage, routed successors and recovery
    acknowledgements may be superseded by their assigned agent only, and an original only once its routed successor
    is settled. Managed continuations and shared-issue posts keep their own workflows."""
    if workstreams.get_for_post(board, post_id) is not None:
        raise Forbidden(MANAGED_REFUSAL)
    if board.conn.execute('SELECT 1 FROM issue_links WHERE post_id=?', (post_id,)).fetchone():
        raise Forbidden('issue-linked request ' + ISSUE_REFUSAL)
    if board.conn.execute('SELECT 1 FROM issue_answer_links WHERE answer_post_id=?', (post_id,)).fetchone():
        raise Forbidden('issue answer ' + ISSUE_REFUSAL)
    if _linked(board, post_id):
        # Routing (requests.assign) can make the post's author, or in principle the human, the assignee of another
        # recipient's row; neither retires a linked request (the author would also skip the session ownership check
        # that requests.progress waives for an author's finish).
        author = board.conn.execute('SELECT agent FROM posts WHERE id=?', (post_id,)).fetchone()['agent']
        if p.is_human or p.name == author or p.name != row['assigned_agent']:
            raise Forbidden('only its assigned agent, not the human or the post\'s author, may supersede a linked '
                            'request')
    successor = open_successor(board, post_id, row['recipient'])
    if successor is not None:
        raise Conflict(f'request was routed to successor #{successor[0]}/{successor[1]}, which is still open: '
                       'settle the successor first (it carries this request\'s authorization)')


def create(board, p, session_id, request_reply, idempotency_key, post_fields, *, nested=False, answer_recipient=None):
    reply = validate(request_reply, idempotency_key)
    board._session(p, session_id, touch=False)  # authentic session ownership, including replay
    if nested or answer_recipient is not None:
        raise Invalid('request replies require their own atomic posting operation')
    if (post_fields['thread_id'] is None or post_fields['new_thread_title'] is not None or post_fields['sealed']
            or post_fields['answer_to'] is not None or post_fields['continuation'] is not None
            or post_fields['propose_task'] is not None):
        raise Invalid('request reply requires an unsealed post in an existing thread without another lifecycle action')
    if post_fields.get('browser_requirement') is not None:
        raise Invalid('declare browser requirements on their own request, not a request reply')
    # Normalize API defaults, not content: a retry must describe the very same post.
    fields = dict(post_fields, to=post_fields['to'] or [], refs=post_fields['refs'] or [])
    try:
        payload_hash = hashlib.sha256(json.dumps({'post': fields, 'request_reply': reply},
                                               sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()
    except (TypeError, ValueError) as exc:
        raise Invalid('request reply must contain finite JSON values') from exc
    with db.write_tx(board.conn):
        prior = board.conn.execute('SELECT * FROM request_reply_operations WHERE actor=? AND session_id=? AND idempotency_key=?',
                                   (p.name, session_id, idempotency_key)).fetchone()
        if prior:
            if prior['payload_hash'] != payload_hash:
                raise Conflict('idempotency key already records a different reply')
            out = board.get_post(p, prior['reply_post_id'])  # visibility is rechecked; no replay writes or notifications
            metadata = json.loads(prior['result'])
            board.get_post(p, metadata['post_id'])
            out['request_reply'] = metadata
            return out
        source, row = requests._context(board, p, session_id, reply['post_id'], reply['recipient'])
        if source['thread_id'] != fields['thread_id'] or source['sealed']:
            raise Invalid('request reply must be unsealed and in the exact source thread')
        if row['version'] != reply['expected_version']:
            raise Conflict('request changed; reread its exact version before replying')
        if row['state'] == 'finished':
            raise Conflict('request is already terminal; replay its original key or create new work')
        if reply.get('completion') is not None and (reply['state'] != 'finished' or workstreams.get_for_post(board,source['id']) is None):
            raise Invalid('completion is only valid for finishing a managed continuation')
        if reply.get('disposition') == 'superseded':
            guard_superseded(board, p, source['id'], row)
        post = board.create_post(p, session_id, **fields, _in_transaction=True)
        result = requests.progress(board, p, session_id, source['id'], reply['recipient'], reply['state'],
            reason=reply['reason'], evidence_post_ids=[post['id']], expected_version=reply['expected_version'],
            completion=reply.get('completion'), terminal_disposition=reply.get('disposition'), _in_transaction=True)
        metadata = {'post_id': source['id'], 'recipient': reply['recipient'], 'state': result['state'],
                    'version': result['version'], 'expected_version': reply['expected_version'], 'disposition': result.get('disposition')}
        board.conn.execute('INSERT INTO request_reply_operations(actor,session_id,idempotency_key,payload_hash,reply_post_id,result,created_at) VALUES (?,?,?,?,?,?,?)',
            (p.name,session_id,idempotency_key,payload_hash,post['id'],json.dumps(metadata),board.now()))
    board._notify('post.created', {'post_id': post['id'], 'thread_id': fields['thread_id'], 'agent': p.name,
                                  'to': fields['to'], 'needs_response': bool(fields['needs_response']), 'sealed': False})
    post['request_reply'] = metadata
    return post
