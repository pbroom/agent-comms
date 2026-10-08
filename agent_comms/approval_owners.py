"""Recorded delivery defaults for human approvals, never authorization to execute.

Only the exact source's task owner and request assignments are considered. Post
text, neighbouring posts, task titles and agent runtime names are not evidence.
Callers must check source visibility and recompute within their write transaction.
"""
from __future__ import annotations

from .core import Invalid


def delivery(board, post, explicit_recipient: str | None = None) -> dict:
    """Return a displayable default or require a human recipient choice.

`post` is a visible, persisted post row/serialized post, not client-supplied data.
An expired lease still identifies the recorded owner, but confers no permission to
work: normal task, request, host and dispatcher checks remain responsible for that.
"""
    def active_agent(name):
        return bool(board.conn.execute(
            'SELECT 1 FROM agents WHERE name=? AND active=1 AND is_human=0', (name,)).fetchone())

    if explicit_recipient is not None:
        if not isinstance(explicit_recipient, str) or not active_agent(explicit_recipient):
            raise Invalid('assign to requires an active agent')
        return {'recipient': explicit_recipient, 'candidates': [explicit_recipient],
                'source': 'explicit', 'requires_choice': False,
                'reason': 'Recipient selected by the human', 'evidence': []}

    # Reload exact persisted fields; serialized metadata is not an ownership input.
    from .requests import for_post
    post = board.conn.execute("SELECT * FROM posts WHERE id=?", (post["id"],)).fetchone()
    if post is None:
        raise Invalid("approval source no longer exists")
    evidence = []
    if post['task_id'] is not None:
        task = board.conn.execute(
            "SELECT * FROM tasks WHERE id=? AND thread_id=? AND status NOT IN ('done','declined')",
            (post['task_id'], post['thread_id'])).fetchone()
        if task and task['owner_agent']:
            evidence.append({'kind': 'task_owner', 'task_id': task['id'],
                             'agent': task['owner_agent'], 'session_id': task['owner_session']})
    has_task_owner = bool(evidence)
    for row in for_post(board, post):
        if row["state"] == "finished":
            continue
        # A default queued addressee has not claimed execution ownership. A
        # recorded task owner is stronger evidence than that initial envelope.
        if has_task_owner and row["version"] == 0 and row["assigned_session"] is None:
            continue
        evidence.append({'kind': 'request_assignment', 'post_id': post['id'],
                         'recipient': row['recipient'], 'agent': row['assigned_agent'],
                         'session_id': row['assigned_session'], 'version': row['version']})
    candidates = sorted({entry['agent'] for entry in evidence})
    if candidates:
        if len(candidates) > 1:
            recipient, reason = None, 'Recorded owners differ; choose who should carry out this approval'
        elif not active_agent(candidates[0]):
            recipient, reason = None, 'Recorded owner is unavailable; choose an active agent'
        else:
            recipient, reason = candidates[0], 'Recorded implementer'
        return {'recipient': recipient, 'candidates': candidates, 'source': 'recorded',
                'requires_choice': recipient is None, 'reason': reason, 'evidence': evidence}

    recipient = post['agent'] if active_agent(post['agent']) else None
    author = board.conn.execute('SELECT is_human FROM agents WHERE name=?', (post['agent'],)).fetchone()
    unavailable = recipient is None and author is not None and not author['is_human']
    return {'recipient': recipient, 'candidates': [recipient] if recipient else [],
            'source': 'proposer' if recipient else 'none', 'requires_choice': unavailable,
            'reason': ('Proposer; no implementer recorded' if recipient else
                       'No active agent recorded; choose an active agent' if unavailable else 'No active agent recorded'),
            'evidence': []}
