"""A glanceable summary of the board for the human's menu bar app (`GET /api/summary`, human only).

It returns counts and server-stamped identifiers only: post and thread ids, agent names (checked against the
agent-name rule), post types and task statuses (both enums the core validates), rule ids, budgets and times.
It never returns post bodies, thread titles, summaries, task titles, refs, rule purposes or any other free text,
so nothing an agent wrote can reach the menu bar. The one exception is a thread's project basename (e.g.
`spfx-kit`): the project path comes from the session that opened the thread, so the basename is passed only
when it is a short plain identifier and is null otherwise.
"""

from __future__ import annotations

import re
from typing import Any

from . import dispatch
from .config import NAME_RE
from .core import TASK_STATUSES, TERMINAL, Board, Principal, iso

SUMMARY_VERSION = 1
NEEDS_YOU_ITEMS = 5
LIVE_SESSION_MINUTES = 10          # a session seen this recently counts as live in the menu
PROJECT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def project_basename(project: str | None) -> str | None:
    """`/Users/me/code/spfx-kit` -> `spfx-kit`; None unless the last path component is a plain identifier."""
    if not project:
        return None
    name = project.rstrip("/").rsplit("/", 1)[-1]
    return name if PROJECT_NAME_RE.match(name) else None


def _agent(name: Any) -> str | None:
    return name if isinstance(name, str) and NAME_RE.match(name) else None


def _int(v: Any) -> int | None:
    return v if isinstance(v, int) and not isinstance(v, bool) else None


def human_summary(board: Board, p: Principal, config: dispatch.DispatchConfig) -> dict:
    board._require_human(p, "view the board summary")
    conn, now = board.conn, board.now()

    needs_count = conn.execute(f"SELECT COUNT(*) FROM posts p WHERE {board.NEEDS_YOU}").fetchone()[0]
    needs_items = [{"post_id": r["id"], "thread_id": r["thread_id"], "agent": r["agent"], "type": r["type"]}
                   for r in conn.execute(f"""SELECT p.id, p.thread_id, p.agent, p.type FROM posts p
                                             WHERE {board.NEEDS_YOU} ORDER BY p.id DESC LIMIT ?""",
                                         (NEEDS_YOU_ITEMS,))]

    # Where a newly registered human session would start reading (the furthest any of the human's sessions
    # acked in each thread), never counting the human's own posts, as `board read` does.
    unread = conn.execute(
        """SELECT COUNT(*) FROM posts p WHERE p.agent != :me AND p.seq > COALESCE(
               (SELECT MAX(c.last_seq) FROM cursors c WHERE c.agent = :me AND c.thread_id = p.thread_id), 0)""",
        {"me": p.name}).fetchone()[0]

    open_threads = conn.execute("SELECT COUNT(*) FROM threads WHERE status = 'open'").fetchone()[0]
    marks = ",".join("?" * len(TERMINAL))
    by_status = {s: 0 for s in TASK_STATUSES if s not in TERMINAL}
    for r in conn.execute(f"SELECT status, COUNT(*) n FROM tasks WHERE status NOT IN ({marks}) GROUP BY status",
                          TERMINAL):
        by_status[r["status"]] = r["n"]

    live = [{"agent": r["agent"], "count": r["n"]} for r in conn.execute(
        """SELECT s.agent, COUNT(*) n FROM sessions s JOIN agents a ON a.name = s.agent
           WHERE a.is_human = 0 AND a.active = 1 AND s.last_seen >= ? GROUP BY s.agent ORDER BY s.agent""",
        (now - LIVE_SESSION_MINUTES * 60,))]

    status = dispatch.loop_status(board, config)
    runs = []
    for d in sorted(dispatch._active_records(board), key=lambda d: d.get("started_at") or 0):
        started = d.get("started_at")
        if not isinstance(started, (int, float)) or isinstance(started, bool):
            started = None
        runs.append({"agent": _agent(d.get("agent")), "thread_id": _int(d.get("thread_id")),
                     "rule_id": _int(d.get("rule_id")), "status": d.get("status"),
                     "started_at": iso(started),
                     "elapsed_seconds": max(0, int(now - started)) if started is not None else None})

    approvals = [{"rule_id": r["id"], "thread_id": r["thread_id"], "agents": r["agents"],
                  "launches_left": r["launches_left"], "max_launches": r["max_launches"],
                  "expires_at": r["expires_at"]}
                 for r in board.list_dispatch_rules(p) if r["state"] == "active"]

    thread_ids = sorted({i["thread_id"] for i in needs_items} | {r["thread_id"] for r in runs if r["thread_id"]}
                        | {a["thread_id"] for a in approvals})
    projects: dict[str, str | None] = {}
    if thread_ids:
        rows = conn.execute(f"SELECT id, project FROM threads WHERE id IN ({','.join('?' * len(thread_ids))})",
                            thread_ids)
        projects = {str(r["id"]): project_basename(r["project"]) for r in rows}

    return {"summary_version": SUMMARY_VERSION, "generated_at": iso(now), "paused": board.is_paused(),
            "needs_you": {"count": needs_count, "items": needs_items},
            "unread_for_human": unread,
            "threads": {"open": open_threads},
            "tasks": {"open": sum(by_status.values()), "by_status": by_status},
            "live_sessions": {"window_minutes": LIVE_SESSION_MINUTES, "agents": live},
            "dispatcher": {"running": bool(status["running"]),
                           "heartbeat_seconds_ago": status.get("heartbeat_seconds_ago"),
                           "runs": runs},
            "approvals": approvals,
            "projects": projects}
