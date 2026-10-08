"""Read-only pickup state derived from explicit per-recipient lifecycle evidence."""
from . import requests
from .core import iso

# Match the dashboard's thirty-minute stall threshold plus ten-minute grace.
PICKUP_WAIT_SECONDS = 40 * 60


def waiting_since(board, post, row):
    """Assignment/state changes start a window; reason-only updates never do."""
    since = post['created_at']
    identity = ('queued', row['recipient'], None)
    events = board.conn.execute(
        'SELECT state,assigned_agent,assigned_session,created_at FROM request_events '
        'WHERE post_id=? AND recipient=? AND version<=? ORDER BY version',
        (post['id'], row['recipient'], row['version']))
    for event in events:
        current = (event['state'], event['assigned_agent'], event['assigned_session'])
        if current != identity:
            since = event['created_at']
            identity = current
    return since


def for_thread(board, p, thread_id):
    """Project every visible request, independently of cursors and display limits."""
    result = {'waiting': [], 'processing': [], 'blocked': [], 'complete': False}
    sources = 0
    posts = board.conn.execute(
        f'SELECT p.* FROM posts p WHERE p.thread_id=:thread AND {board.VISIBLE} ORDER BY p.id',
        {'thread': thread_id, **board._vis(p)})
    for post in posts:
        for row in requests.for_post(board, post):
            sources += 1
            event = board.conn.execute('''SELECT * FROM request_events
                WHERE post_id=? AND recipient=? AND version=?''',
                (post['id'], row['recipient'], row['version'])).fetchone()
            matches = bool(event and event['state'] == row['state']
                           and event['assigned_agent'] == row['assigned_agent']
                           and event['assigned_session'] == row['assigned_session'])
            if row['state'] == 'finished' and matches:
                continue
            bucket = 'waiting'
            reason = row['reason']
            if row['state'] == 'blocked':
                bucket = 'blocked'
            elif (row['state'] == 'started' and matches and row['assigned_session'] is not None
                  and event['actor'] == row['assigned_agent']
                  and event['session_id'] == row['assigned_session']):
                owner = board.conn.execute('SELECT last_seen FROM sessions WHERE id=?',
                                           (row['assigned_session'],)).fetchone()
                lease = board.conn.execute('''SELECT 1 FROM tasks WHERE id=? AND owner_session=?
                    AND owner_agent=? AND status IN ('working','blocked') AND lease_expires_at>?''',
                    (post['task_id'], row['assigned_session'], row['assigned_agent'], board.now())).fetchone()
                if (owner is None or owner['last_seen'] < board.now() - PICKUP_WAIT_SECONDS) and not lease:
                    bucket = 'blocked'
                    reason = 'Owner acknowledgement is stale; no current task lease confirms continued work'
                else:
                    bucket = 'processing'
            elif row['state'] in ('started', 'finished'):
                reason = 'Current assignment lacks explicit '+row['state']+' lifecycle evidence'
            deadline = None
            if bucket == 'waiting':
                managed = board.conn.execute('SELECT deadline FROM continuations WHERE post_id=?',
                                             (post['id'],)).fetchone()
                deadline = managed['deadline'] if managed else waiting_since(board, post, row) + PICKUP_WAIT_SECONDS
            result[bucket].append({
                'post_id': post['id'], 'recipient': row['recipient'],
                'assigned_agent': row['assigned_agent'], 'assigned_session': row['assigned_session'],
                'state': row['state'], 'reason': reason, 'deadline_at': iso(deadline),
                'overdue': deadline is not None and board.now() >= deadline,
            })
    tasks = board.conn.execute('SELECT status FROM tasks WHERE thread_id=?', (thread_id,)).fetchall()
    result['complete'] = bool(sources or tasks) and not any(result[key] for key in
        ('waiting', 'processing', 'blocked')) and all(t['status'] in ('done', 'declined') for t in tasks)
    return result
