"""Tasks that wait on other tasks, in other threads (DESIGN_NOTES "Awaiting another thread").

`tasks.depends_on` holds task ids. A task is *awaiting* while it has a dependency that **counts** (COUNTS below) and is
unfinished (neither `done` nor `declined`) in an open thread. An awaiting task is not stalled: the dashboard shows
"Awaiting #<thread>" instead of Unstick, automatic recovery and Unstick leave it alone, and the Needs you escalations
about it close while it waits. Claims are unchanged: core.claim_task still needs every dependency `done`.

Which dependencies count (so an agent cannot silence its own stall with a dependency it controls): one the human set,
or one on a task created by the human or by an agent other than the one that set the dependency. Who set each
dependency is recorded (STATE_PREFIX `setters`); dependencies given at task creation were set by the task's creator.
A dependency in a thread that is not open never counts as awaiting: the dashboard shows it as "blocking thread closed".

`depends_on` can be set after creation (`board_update_task(depends_on=[...])`, HTTP `/api/tasks/{id}/transition`,
the dashboard's "Waits on task #" control) by the task's creator, its owner or the human; while someone holds a live
lease only that owner or the human. Agents cannot remove a dependency the human set. Each dependency must exist, must
not be the task itself, must be in the task's project or the board's own project, must be in an open thread unless
finished, and must not wait (directly or through others) on this task. When the last dependency of a task that was set
to wait this way is done or declined (a *satisfaction event*), the dispatcher asks the agent that should continue it
(autorecover._continue_item), once per STATE_PREFIX generation.

Closed escalations come back: if a task stops awaiting (its dependencies were cleared, changed, declined or closed
away), every escalation closed for it is reopened (reconcile_closures), unless its dependencies all finished and the
dispatcher took the task over for that generation (a request to continue it, or a new question to the human). If
nobody can be asked to continue it, or automatic recovery is switched off, the escalation comes back too.
reconcile_closures runs on every dependency update, task status change and thread close or reopen, and once per
dispatcher pass as a backstop.

Only ids, statuses and agent names are read here, plus a dependency's title for the dashboard's tooltip (rendered as
text). Nothing an agent wrote reaches a post, a rule purpose or a launch prompt.
"""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import nullcontext
from typing import Any

from . import db
from .core import TERMINAL, Board, Conflict, Forbidden, Invalid, NotFound, Principal

MAX_DEPS = 20
STATE_PREFIX = "awaiting.task."     # <task id>: {deps, setters, generation, set_at, set_by, waiting}
CLOSED_PREFIX = "awaiting.closed."  # <post id>: an escalation this module closed {task_ids, by, at}
CLOSE_REASON = ("Closed automatically: task {task} now waits on task{s} {deps} (dependency set by {by}), so it is "
                "awaiting other work, not stalled. If it stops waiting before {it} finish{es}, this item comes back.")

# Who set dependency d.value of task {t}: its STATE_PREFIX record, else the task's creator (given at creation).
_SETTER = ("COALESCE((SELECT json_extract(sbs.value, '$.setters.\"' || d.value || '\"') FROM board_state sbs "
           "WHERE sbs.key = 'awaiting.task.' || {t}.id), {t}.created_by)")
# The dependency (task dt) counts: the human set it, or the human or another agent created the task it names.
COUNTS = ("(EXISTS (SELECT 1 FROM agents ah WHERE ah.is_human = 1 AND (ah.name = dt.created_by OR ah.name = "
          + _SETTER + ")) OR dt.created_by != " + _SETTER + ")")
# SQL: task {t} is awaiting (a dependency that counts, unfinished, in an open thread).
AWAITS = ("EXISTS (SELECT 1 FROM json_each({t}.depends_on) d JOIN tasks dt ON dt.id = d.value "
          "JOIN threads dth ON dth.id = dt.thread_id WHERE dt.status NOT IN ('done', 'declined') "
          "AND dth.status = 'open' AND " + COUNTS + ")")


def deps_of(row) -> list[int]:
    try:
        deps = json.loads(row["depends_on"] or "[]")
    except (TypeError, ValueError):
        return []
    return [d for d in deps if type(d) is int] if isinstance(deps, list) else []


def state(conn: sqlite3.Connection, task_id: int) -> dict | None:
    row = conn.execute("SELECT value FROM board_state WHERE key = ?", (STATE_PREFIX + str(task_id),)).fetchone()
    try:
        value = json.loads(row["value"]) if row else None
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _humans(conn) -> set[str]:
    return {r[0] for r in conn.execute("SELECT name FROM agents WHERE is_human = 1")}


def setters(conn: sqlite3.Connection, row) -> dict[int, str]:
    """Who set each current dependency of this task (its creator for those given at creation)."""
    recorded = (state(conn, row["id"]) or {}).get("setters") or {}
    return {d: recorded.get(str(d)) if isinstance(recorded.get(str(d)), str) else row["created_by"]
            for d in deps_of(row)}


def dependencies(conn: sqlite3.Connection, row) -> list[dict]:
    """Every dependency of the task, in order: id, thread_id, status, title, thread_status, set_by, counts."""
    deps = deps_of(row)
    if not deps:
        return []
    rows = {r["id"]: r for r in conn.execute(
        f"""SELECT tk.id, tk.thread_id, tk.status, tk.title, tk.created_by, t.status AS thread_status FROM tasks tk
            JOIN threads t ON t.id = tk.thread_id WHERE tk.id IN ({','.join('?' * len(deps))})""", deps)}
    by, humans = setters(conn, row), _humans(conn)
    out = []
    for d in deps:
        r = rows.get(d)
        if r is None:
            continue
        counts = by[d] in humans or r["created_by"] in humans or r["created_by"] != by[d]
        out.append({"id": d, "thread_id": r["thread_id"], "status": r["status"], "title": r["title"],
                    "thread_status": r["thread_status"], "set_by": by[d], "counts": counts})
    return out


def waiting_on(conn: sqlite3.Connection, row) -> list[dict]:
    """The dependencies the task awaits (they count, are unfinished, and their thread is open): the dashboard's
    "Awaiting #<thread>"."""
    return [_public(d) for d in dependencies(conn, row)
            if d["counts"] and d["status"] not in TERMINAL and d["thread_status"] == "open"]


def blocked_by_closed(conn: sqlite3.Connection, row) -> list[dict]:
    """Unfinished dependencies in a thread that is not open: not awaiting (the task is stalled), shown as "blocking
    thread closed"."""
    return [_public(d) for d in dependencies(conn, row) if d["status"] not in TERMINAL and d["thread_status"] != "open"]


def _public(d: dict) -> dict:
    return {k: d[k] for k in ("id", "thread_id", "status", "title", "thread_status")}


def awaits(conn: sqlite3.Connection, task_id: int) -> bool:
    return conn.execute(f"SELECT 1 FROM tasks tk WHERE tk.id = ? AND {AWAITS.format(t='tk')}",
                        (task_id,)).fetchone() is not None


def all_finished(conn: sqlite3.Connection, deps: list[int]) -> bool:
    """Every one of these dependencies is done or declined (a satisfaction event, once they were awaited)."""
    if not deps:
        return False
    n = conn.execute(f"SELECT COUNT(*) FROM tasks WHERE id IN ({','.join('?' * len(deps))}) "
                     "AND status IN ('done', 'declined')", deps).fetchone()[0]
    return n == len(set(deps))


def satisfied(conn: sqlite3.Connection, task_id: int, row=None) -> bool:
    """A satisfaction event happened for the task's current dependencies: they were set while the task waited, they
    are still its dependencies, and every one is now done or declined."""
    rec = state(conn, task_id)
    row = row or conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    return (row is not None and isinstance(rec, dict) and bool(rec.get("waiting"))
            and rec.get("deps") == deps_of(row) and all_finished(conn, deps_of(row)))


def _real(path: str | None) -> str:
    return os.path.realpath(path or "/nonexistent-project")


def check_projects(board: Board, conn, thread_id: int, deps: list[int]) -> None:
    """A dependency's thread must be in the task's own project or in the board's own project (a board fix)."""
    if not deps:
        return
    from .prevention import board_home
    own = conn.execute("SELECT project FROM threads WHERE id = ?", (thread_id,)).fetchone()
    allowed = {_real(own["project"] if own else None), board_home(board)}
    for r in conn.execute(f"""SELECT tk.id, t.project FROM tasks tk JOIN threads t ON t.id = tk.thread_id
                              WHERE tk.id IN ({','.join('?' * len(deps))})""", deps):
        if _real(r["project"]) not in allowed:
            raise Invalid(f"depends_on task {r['id']} is in another project; a task can wait only on tasks in its own "
                          "project or in the board's own project")


def _validate(board: Board, conn: sqlite3.Connection, task, value: Any) -> list[int]:
    task_id = task["id"]
    if not isinstance(value, (list, tuple)) or any(type(v) is not int for v in value):
        raise Invalid("depends_on must be a list of task ids (whole numbers)")
    deps = list(dict.fromkeys(value))
    if len(deps) > MAX_DEPS:
        raise Invalid(f"depends_on takes at most {MAX_DEPS} task ids")
    if task_id in deps:
        raise Invalid(f"task {task_id} cannot depend on itself")
    for dep in deps:
        row = conn.execute("""SELECT tk.status, t.status AS thread_status FROM tasks tk JOIN threads t
                              ON t.id = tk.thread_id WHERE tk.id = ?""", (dep,)).fetchone()
        if row is None:
            raise NotFound(f"depends_on task {dep} not found")
        if row["status"] not in TERMINAL and row["thread_status"] != "open":
            raise Conflict(f"depends_on task {dep} is in a closed thread and unfinished; reopen that thread first")
    check_projects(board, conn, task["thread_id"], deps)
    # No cycles: walk everything the new dependencies wait on (directly or through others); this task must not appear.
    seen: set[int] = set()
    todo = list(deps)
    while todo:
        cur = todo.pop()
        if cur == task_id:
            raise Invalid(f"depends_on would create a cycle: a task in {sorted(deps)} already waits (directly or "
                          f"through other tasks) on task {task_id}")
        if cur in seen:
            continue
        seen.add(cur)
        row = conn.execute("SELECT depends_on FROM tasks WHERE id = ?", (cur,)).fetchone()
        if row is not None:
            todo.extend(deps_of(row))
    return deps


def _upsert(conn, key: str, value: Any, by: str, now: float) -> None:
    conn.execute("""INSERT INTO board_state(key, value, updated_by, updated_at) VALUES (?, ?, ?, ?)
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_by = excluded.updated_by,
                    updated_at = excluded.updated_at""", (key, json.dumps(value), by, now))


def set_dependencies(board: Board, p: Principal, session_id: int, task_id: int, depends_on: Any, *,
                     _in_transaction: bool = False, _notes: list | None = None) -> dict:
    """Replace the task's depends_on. Validates ids, projects and cycles, records who set each dependency, closes the
    Needs you escalations about tasks that now all await, and reopens the ones closed for this task if it stopped
    awaiting without a satisfaction event. `_in_transaction`: inside the caller's write (Board.update_task), with the
    notifications appended to `_notes` instead of sent."""
    board._check_agent_write(p)
    board._session(p, session_id)
    if _in_transaction and not board.conn.in_transaction:
        raise Invalid("internal: a joined dependency update needs an open write transaction")
    with (nullcontext(board.conn) if _in_transaction else db.write_tx(board.conn)) as c:
        board._check_agent_write(p)
        t = board._task_row(task_id, c)
        now = board.now()
        if not p.is_human:
            live_owner = t["owner_agent"] is not None and (t["lease_expires_at"] or 0) > now
            if live_owner and p.name != t["owner_agent"]:
                raise Forbidden(f"task {task_id} is leased by {t['owner_agent']}; only that owner or the human can "
                                "change its dependencies now")
            if p.name not in (t["created_by"], t["owner_agent"]):
                raise Forbidden(f"only task {task_id}'s creator ({t['created_by']}), its owner or the human can set "
                                "its dependencies")
        if t["status"] in TERMINAL:
            raise Conflict(f"task {task_id} is {t['status']}; its dependencies no longer matter")
        thread = board._thread_row(t["thread_id"])
        if thread["status"] != "open" and not p.is_human:
            raise Conflict("thread is closed")
        from . import workstreams
        if workstreams.get_for_task(board, task_id) is not None:
            raise Forbidden("a managed continuation task's prerequisites are fixed by its continuation")
        deps = _validate(board, c, t, depends_on)
        before = setters(c, t)
        humans = _humans(c)
        if not p.is_human:
            removed = [d for d, who in before.items() if who in humans and d not in deps]
            if removed:
                raise Forbidden(f"the human set the dependency on task{'s' if len(removed) > 1 else ''} "
                                f"{', '.join(map(str, removed))}; only the human can remove it")
        by = {str(d): before.get(d, p.name) for d in deps}
        c.execute("UPDATE tasks SET depends_on = ?, updated_at = ? WHERE id = ?", (json.dumps(deps), now, task_id))
        board._event(c, task_id, "depends_on", t["status"], t["status"], p, session_id,
                     "depends_on: " + (", ".join(str(d) for d in deps) if deps else "none"))
        previous = state(c, task_id) or {}
        generation = previous.get("generation") if type(previous.get("generation")) is int else 0
        _upsert(c, STATE_PREFIX + str(task_id), {"deps": deps, "setters": by, "generation": generation + 1,
                                                 "set_at": now, "set_by": p.name, "waiting": False}, p.name, now)
        waiting = awaits(c, task_id)
        if waiting:
            rec = state(c, task_id)
            _upsert(c, STATE_PREFIX + str(task_id), rec | {"waiting": True}, p.name, now)
        closed = _close_escalations(board, c, p, session_id, task_id, now) if waiting else []
        reopened = reconcile_closures(board, c, now)
    notes = [("task.dependencies", {"task_id": task_id, "depends_on": deps, "agent": p.name})]
    notes += [("attention.resolved", {"post_id": i, "thread_id": t["thread_id"]}) for i in closed]
    notes += [("attention.reopened", {"post_id": i}) for i in reopened]
    if _notes is not None:
        _notes.extend(notes)
    else:
        for event, payload in notes:
            board._notify(event, payload)
    out = board.get_task(p, task_id, events=False)
    out["closed_escalation_post_ids"] = closed
    if reopened:
        out["reopened_escalation_post_ids"] = reopened
    return out


def _close_escalations(board: Board, c, p: Principal, session_id: int, task_id: int, now: float) -> list[int]:
    """Close the automatic-recovery escalations (Needs you posts the dispatcher made) that name this task, when every
    task they name now awaits. The closure is an attention resolution in the actor's name, with fixed server text, and
    a CLOSED_PREFIX marker so reconcile_closures can reopen it. Called inside the write."""
    from . import autorecover
    lo, hi = autorecover._range(autorecover.POST_PREFIX)
    open_deps = [d["id"] for d in waiting_on(c, board._task_row(task_id, c))]
    closed = []
    for key, value in c.execute(
            """SELECT bs.key, bs.value FROM board_state bs WHERE bs.key >= ? AND bs.key < ?
               AND json_valid(bs.value) AND json_extract(bs.value, '$.kind') = 'escalation'
               AND EXISTS (SELECT 1 FROM json_each(json_extract(bs.value, '$.task_ids')) j WHERE j.value = ?)""",
            (lo, hi, task_id)).fetchall():
        try:
            post_id = int(key[len(autorecover.POST_PREFIX):])
            task_ids = [i for i in json.loads(value).get("task_ids") or [] if type(i) is int]
        except (TypeError, ValueError, AttributeError):
            continue
        if not task_ids or not all(awaits(c, i) for i in task_ids):
            continue
        if c.execute(f"SELECT 1 FROM posts p WHERE p.id = ? AND {Board.NEEDS_YOU}", (post_id,)).fetchone() is None:
            continue
        many = len(open_deps) > 1
        reason = CLOSE_REASON.format(task=task_id, s="s" if many else "", deps=", ".join(str(d) for d in open_deps),
                                     by=p.name, it="they" if many else "it", es="" if many else "es")
        c.execute("""INSERT INTO attention_resolutions(post_id, resolved_by, session_id, reason, evidence_post_ids,
                     resolved_at) VALUES (?, ?, ?, ?, '[]', ?)""", (post_id, p.name, session_id, reason, now))
        _upsert(c, CLOSED_PREFIX + str(post_id), {"task_ids": task_ids, "by": p.name, "at": now}, p.name, now)
        _bump(c, post_id, now)
        closed.append(post_id)
    return closed


def _bump(c, post_id: int, now: float) -> None:
    seq = c.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM posts").fetchone()[0]
    c.execute("UPDATE posts SET seq = ?, revised_at = ? WHERE id = ?", (seq, now, post_id))


CONTINUE_TAKES_OVER = ("sent", "escalated", "recovered", "resolved")   # continue-record states that answer a closure


def continue_record(c, task_id: int, generation: Any) -> dict | None:
    """The dispatcher's record of the request to continue this task for this depends_on generation
    (autorecover.CONTINUE_PREFIX), or None while there is none."""
    if type(generation) is not int:
        return None
    row = c.execute("SELECT value FROM board_state WHERE key = ?",
                    (f"auto_recovery.continue.{int(task_id)}.{generation}",)).fetchone()
    try:
        value = json.loads(row["value"]) if row else None
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def reconcile_closures(board: Board, c, now: float, *, no_continue: bool = False) -> list[int]:
    """Inside a write: reopen every escalation this module closed whose tasks no longer all await, unless each task
    that stopped awaiting is finished, or its dependencies finished (a satisfaction event) AND the dispatcher has taken
    it over with a request to continue it (or a new question to the human) for that generation. A satisfied task with
    no continue record yet keeps the closure pending (the dispatcher's next pass decides); one whose continue record
    says nobody can be asked (`none`) or whose question was suppressed reopens it, as does every satisfied task when
    `no_continue` (automatic recovery is switched off, so no request will come). Reopening deletes exactly the
    resolution written when it was closed, so the post is back in Needs you and its automatic-recovery record (still
    `escalated`) shows again. A closure whose tasks are all finished or taken over is kept and its marker dropped.
    Called from set_dependencies, every task status change, thread close/reopen, and each dispatcher pass. Returns
    the reopened post ids."""
    lo, hi = CLOSED_PREFIX, CLOSED_PREFIX[:-1] + chr(ord(CLOSED_PREFIX[-1]) + 1)
    reopened = []
    for key, value in c.execute("SELECT key, value FROM board_state WHERE key >= ? AND key < ?", (lo, hi)).fetchall():
        try:
            post_id = int(key[len(CLOSED_PREFIX):])
            marker = json.loads(value)
            task_ids = [i for i in marker.get("task_ids") or [] if type(i) is int]
        except (TypeError, ValueError, AttributeError):
            c.execute("DELETE FROM board_state WHERE key = ?", (key,))
            continue
        settled, back = True, False
        for i in task_ids:
            row = c.execute("SELECT * FROM tasks WHERE id = ?", (i,)).fetchone()
            if row is None or row["status"] in TERMINAL:
                continue
            if satisfied(c, i, row):
                rec = continue_record(c, i, (state(c, i) or {}).get("generation"))
                if rec is not None and rec.get("state") in CONTINUE_TAKES_OVER:
                    continue
                if rec is not None or no_continue:
                    back = True
                else:
                    settled = False     # pending: the dispatcher has not looked at it yet
                continue
            if awaits(c, i):
                settled = False
                continue
            back = True
        if back:
            c.execute("DELETE FROM attention_resolutions WHERE post_id = ? AND resolved_at = ? AND resolved_by = ?",
                      (post_id, marker.get("at"), marker.get("by")))
            c.execute("DELETE FROM board_state WHERE key = ?", (key,))
            _bump(c, post_id, now)
            reopened.append(post_id)
        elif settled:
            c.execute("DELETE FROM board_state WHERE key = ?", (key,))
    return reopened
