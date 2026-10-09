"""Tasks that wait on other tasks, in any thread (DESIGN_NOTES "Awaiting another thread").

`tasks.depends_on` holds task ids. A dependency counts as satisfied once it is `done` or `declined`; a task with an
unsatisfied dependency is *awaiting*: it is not stalled, so the dashboard shows "Awaiting #<thread>" instead of
Unstick, automatic recovery and Unstick leave it alone, and the Needs you escalations about it close.

`depends_on` can be set after creation (`board_update_task(depends_on=[...])`, HTTP `/api/tasks/{id}/transition`,
the dashboard's "Waits on task #" control) by the task's creator, its owner or the human. Each dependency must exist,
must not be the task itself, and must not wait (directly or through others) on this task. When the last dependency of
a task that was set to wait this way is satisfied, the dispatcher asks the agent that should continue it
(autorecover._continue_items): one automatic request per dependency-satisfaction event (STATE_PREFIX generation).

Only ids, statuses and agent names are read here, plus a dependency's title for the dashboard's tooltip (rendered as
text). Nothing an agent wrote reaches a post, a rule purpose or a launch prompt.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from . import db
from .core import TERMINAL, Board, Conflict, Forbidden, Invalid, NotFound, Principal

MAX_DEPS = 20
STATE_PREFIX = "awaiting.task."   # <task id>: {deps, generation, set_at, set_by, waiting} (the last depends_on set)
CLOSE_REASON = ("Closed automatically: task {task} now waits on task{s} {deps} (dependency set by {by}), so it is "
                "awaiting other work, not stalled. When {it} finish{es}, the dispatcher asks the agent to continue it.")

# SQL: the task aliased {t} has a dependency that is neither done nor declined (a dependency id with no task row,
# which core never writes, counts as satisfied).
AWAITS = ("EXISTS (SELECT 1 FROM json_each({t}.depends_on) d JOIN tasks dt ON dt.id = d.value "
          "WHERE dt.status NOT IN ('done', 'declined'))")


def deps_of(row) -> list[int]:
    try:
        deps = json.loads(row["depends_on"] or "[]")
    except (TypeError, ValueError):
        return []
    return [d for d in deps if type(d) is int] if isinstance(deps, list) else []


def unsatisfied(conn: sqlite3.Connection, deps: list[int]) -> list[sqlite3.Row]:
    """The dependencies (id, thread_id, status, title, thread_status) that are not done or declined, in order."""
    if not deps:
        return []
    rows = {r["id"]: r for r in conn.execute(
        f"""SELECT tk.id, tk.thread_id, tk.status, tk.title, t.status AS thread_status FROM tasks tk
            JOIN threads t ON t.id = tk.thread_id WHERE tk.id IN ({','.join('?' * len(deps))})""", deps)}
    return [rows[d] for d in deps if d in rows and rows[d]["status"] not in TERMINAL]


def awaits(conn: sqlite3.Connection, task_id: int) -> bool:
    """The task has a dependency that is neither done nor declined."""
    return conn.execute(f"SELECT 1 FROM tasks tk WHERE tk.id = ? AND {AWAITS.format(t='tk')}",
                        (task_id,)).fetchone() is not None


def waiting_on(conn: sqlite3.Connection, row) -> list[dict]:
    """For task output: the unsatisfied dependencies, with their thread (the dashboard's "Awaiting #<thread>")."""
    return [{"id": r["id"], "thread_id": r["thread_id"], "status": r["status"], "title": r["title"],
             "thread_status": r["thread_status"]} for r in unsatisfied(conn, deps_of(row))]


def _validate(conn: sqlite3.Connection, task_id: int, value: Any) -> list[int]:
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


def state(conn: sqlite3.Connection, task_id: int) -> dict | None:
    row = conn.execute("SELECT value FROM board_state WHERE key = ?", (STATE_PREFIX + str(task_id),)).fetchone()
    try:
        value = json.loads(row["value"]) if row else None
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def set_dependencies(board: Board, p: Principal, session_id: int, task_id: int, depends_on: Any) -> dict:
    """Replace the task's depends_on (the creator, the owner or the human). Validates ids, refuses cycles, records
    the change (task event, STATE_PREFIX generation) and closes Needs you escalations about tasks that now all await."""
    board._check_agent_write(p)
    board._session(p, session_id)
    closed: list[int] = []
    with db.write_tx(board.conn) as c:
        board._check_agent_write(p)
        t = board._task_row(task_id, c)
        if not p.is_human and p.name not in (t["created_by"], t["owner_agent"]):
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
        deps = _validate(c, task_id, depends_on)
        now = board.now()
        c.execute("UPDATE tasks SET depends_on = ?, updated_at = ? WHERE id = ?", (json.dumps(deps), now, task_id))
        board._event(c, task_id, "depends_on", t["status"], t["status"], p, session_id,
                     "depends_on: " + (", ".join(str(d) for d in deps) if deps else "none"))
        previous = state(c, task_id) or {}
        generation = previous.get("generation") if type(previous.get("generation")) is int else 0
        open_deps = [r["id"] for r in unsatisfied(c, deps)]
        _upsert(c, STATE_PREFIX + str(task_id), {"deps": deps, "generation": generation + 1, "set_at": now,
                                                 "set_by": p.name, "waiting": bool(open_deps)}, p.name, now)
        if open_deps:
            closed = _close_escalations(board, c, p, session_id, task_id, open_deps, now)
    board._notify("task.dependencies", {"task_id": task_id, "depends_on": deps, "agent": p.name})
    for post_id in closed:
        board._notify("attention.resolved", {"post_id": post_id, "thread_id": t["thread_id"]})
    out = board.get_task(p, task_id, events=False)
    out["closed_escalation_post_ids"] = closed
    return out


def _close_escalations(board: Board, c, p: Principal, session_id: int, task_id: int, open_deps: list[int],
                       now: float) -> list[int]:
    """Close the automatic-recovery escalations (Needs you posts the dispatcher made) that name this task, when every
    task they name now awaits other work. The closure is an attention resolution recorded in the actor's name, with
    fixed server text; the post stays, and the dashboard shows who closed it and why. Called inside the write."""
    from . import autorecover
    lo, hi = autorecover._range(autorecover.POST_PREFIX)
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
        if not task_ids or not all(i == task_id or awaits(c, i) for i in task_ids):
            continue
        if c.execute(f"SELECT 1 FROM posts p WHERE p.id = ? AND {Board.NEEDS_YOU}", (post_id,)).fetchone() is None:
            continue
        many = len(open_deps) > 1
        reason = CLOSE_REASON.format(task=task_id, s="s" if many else "", deps=", ".join(str(d) for d in open_deps),
                                     by=p.name, it="they" if many else "it", es="" if many else "es")
        c.execute("""INSERT INTO attention_resolutions(post_id, resolved_by, session_id, reason, evidence_post_ids,
                     resolved_at) VALUES (?, ?, ?, ?, '[]', ?)""", (post_id, p.name, session_id, reason, now))
        seq = c.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM posts").fetchone()[0]
        c.execute("UPDATE posts SET seq = ?, revised_at = ? WHERE id = ?", (seq, now, post_id))
        closed.append(post_id)
    return closed
