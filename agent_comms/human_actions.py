"""Shared plumbing for the dashboard's one-click human actions (Unstick, and resolving a "Needs you" item).

Both post fixed, server-built text as the human and may approve a one-shot dispatcher rule first. The pieces here:
- a cooldown stamp in `board_state`, checked and set in one write transaction (a double click cannot pass twice),
  optionally together with a caller's own check in that same transaction;
- `post_as_human`: the rule-before-post ordering (the dispatcher ignores posts older than a rule) with rollback;
- `launch_outlook`: what happens next for the agents (live, launchable, no runner, dispatcher running, paused).
Nothing here reads a post body, title or summary.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Callable

from . import db, dispatch
from .core import Board, Conflict, Principal

RULE_HOURS = 6      # one-shot rules approved by a click expire after this
MAX_SESSIONS = 20   # live session ids returned to the page


def reserve_cooldown(board: Board, p: Principal, key: str, seconds: float, refuse: Callable[[int], str],
                     check: Callable[[sqlite3.Connection], None] | None = None) -> None:
    """Refuse (409, `refuse(wait_seconds)`) when `key` was stamped less than `seconds` ago, else stamp it now. `check`
    runs first inside the same write transaction, so a precondition and the stamp cannot race."""
    now = board.now()
    with db.write_tx(board.conn) as c:
        if check is not None:
            check(c)
        row = c.execute("SELECT value FROM board_state WHERE key = ?", (key,)).fetchone()
        try:
            last = float(json.loads(row["value"])) if row else None
        except (TypeError, ValueError):
            last = None
        if last is not None and now - last < seconds:
            raise Conflict(refuse(int(seconds - (now - last)) + 1))
        c.execute("""INSERT INTO board_state(key, value, updated_by, updated_at) VALUES (?, ?, ?, ?)
                     ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_by = excluded.updated_by,
                     updated_at = excluded.updated_at""", (key, json.dumps(now), p.name, now))


def release_cooldown(board: Board, key: str) -> None:
    with db.write_tx(board.conn) as c:
        c.execute("DELETE FROM board_state WHERE key = ?", (key,))


def post_as_human(board: Board, p: Principal, *, thread_id: int, body: str, type: str, to: list[str],
                  needs_response: bool, launch: list[str] | None = None,
                  purpose: str | None = None) -> tuple[dict, dict | None]:
    """Post fixed text as the human, in the human's own board session. With `launch`, first approve a one-shot
    dispatcher rule (one launch each, RULE_HOURS) for those agents that no active rule on this thread already
    covers. The rule comes first because the dispatcher only triggers on posts created at or after a rule; if the
    post then fails, the rule is revoked. Returns (post, rule or None)."""
    board._require_human(p, "post as the human")
    rule = None
    try:
        if launch:
            covered = {a for r in board.active_dispatch_rules(p) if r["thread_id"] == thread_id for a in r["agents"]}
            uncovered = [a for a in launch if a not in covered]
            if uncovered:
                rule = board.create_dispatch_rule(p, thread_id=thread_id, agents=uncovered, purpose=purpose or "",
                                                  max_launches=len(uncovered),
                                                  expires_at=board.now() + RULE_HOURS * 3600)
        post = board.create_post(p, board.human_session(p), body=body, type=type, thread_id=thread_id, to=to,
                                 needs_response=needs_response)
    except Exception:
        if rule is not None:
            board.revoke_dispatch_rule(p, rule["id"])
        raise
    return post, rule


def launch_outlook(board: Board, config: dispatch.DispatchConfig, agents: list[str]) -> dict[str, Any]:
    """What happens next for these agents: `live_agents` (a session inside the dispatcher's live window, or a
    dispatched run in progress: they see the post through their normal read path and are not launched),
    `sessions` (those live session ids, last seen first), `no_runner` (can never be launched),
    `dispatcher_running` and `paused`. Ids, names and times only."""
    status = dispatch.loop_status(board, config)
    now, window = board.now(), config.live_minutes * 60
    live = [a for a in agents if (board.conn.execute("SELECT MAX(last_seen) FROM sessions WHERE agent = ?", (a,))
                                  .fetchone()[0] or float("-inf")) >= now - window]
    if status.get("running"):
        live += [a for a in sorted({r.get("agent") for r in dispatch._active_records(board)} & set(agents))
                 if a not in live]
    runtimes = {r["name"]: r["runtime"] for r in board.conn.execute("SELECT name, runtime FROM agents")}
    sessions: list[int] = []
    if agents:
        marks = ",".join("?" * len(agents))
        sessions = [r["id"] for r in board.conn.execute(
            f"""SELECT id FROM sessions WHERE agent IN ({marks}) AND last_seen >= ?
                ORDER BY last_seen DESC, id DESC LIMIT {MAX_SESSIONS}""", (*agents, now - window))]
    return {"dispatcher_running": bool(status.get("running")), "paused": board.is_paused(),
            "live_agents": live, "sessions": sessions,
            "no_runner": [a for a in agents if config.runner_for(a, runtimes.get(a)) is None]}


def launchable_agents(board: Board, config: dispatch.DispatchConfig) -> list[str]:
    """Active non-human agents the dispatcher could launch now: a runner is configured and they are not live. For
    the dashboard's "Approve & launch" button (the server re-checks nothing from the page: it only decides whether
    the button is offered)."""
    names = [r["name"] for r in board.conn.execute(
        "SELECT name FROM agents WHERE active = 1 AND is_human = 0 ORDER BY name")]
    out = launch_outlook(board, config, names)
    return [a for a in names if a not in out["live_agents"] and a not in out["no_runner"]]
