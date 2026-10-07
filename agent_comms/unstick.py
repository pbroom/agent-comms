"""Unstick a stalled thread (`POST /api/threads/{id}/unstick`, human only).

The dashboard marks a thread stalled when, among other things, an agent was asked for a reply and has not posted
since, or a task is blocked or its lease expired. Unstick is the human's one-click answer: the server works out
which agents the thread is waiting on (from the database, never from the client), posts a fixed `request` to them
as the human, and approves a one-shot dispatcher rule so an agent without a live session is launched at once.

Guardrails (DESIGN_NOTES "Unstick"):
- The human's click is the approval. Agents cannot call this route; core and the route both check.
- The post body and the rule purpose are fixed server-side text. Their only variable parts are the thread id,
  post ids, task ids and agent names (all server-stamped). No post body, title, summary or other agent-written text
  is read or copied.
- The rule names exactly the stuck agents that no active rule for the thread already covers, with a budget of one
  launch each, and expires after UNSTICK_RULE_HOURS. The rule is created before the post, because the dispatcher
  ignores posts older than a rule.
- One unstick per thread per UNSTICK_COOLDOWN_SECONDS, so a double click cannot post twice.
"""

from __future__ import annotations

import json
from typing import Any

from . import db, dispatch
from .core import TERMINAL, Board, Conflict, Principal

UNSTICK_COOLDOWN_SECONDS = 120
UNSTICK_RULE_HOURS = 6
STATE_PREFIX = "unstick.thread."
MAX_IDS_PER_AGENT = 5    # post ids listed per agent in the body; the rest are counted
MAX_REASONS = 20         # reasons listed in the body (keeps it far below the body size limit)

PURPOSE = ("Unstick thread {thread}: diagnose why it stalled, resolve it, and propose a prevention; "
           "stay within the thread's existing request.")
BODY_INSTRUCTIONS = ("Find the root cause of the stall, resolve it now, and post a `finding` with the cause plus a "
                     "`proposal` for preventing it next time. Stay within what this thread already asked for.")


def stuck_agents(board: Board, thread_id: int) -> tuple[list[str], list[dict]]:
    """The active non-human agents this thread is waiting on, and why. Reads metadata only (ids, agents, flags,
    statuses, times), never bodies, titles or summaries. No age threshold: the human chose to ask."""
    c, now = board.conn, board.now()
    reasons: list[dict] = []
    # (a) Unsealed needs-response posts addressed to an agent that has not posted in the thread since.
    asks: dict[str, list[int]] = {}
    for r in c.execute(
            """SELECT p.id, j.value AS agent FROM posts p, json_each(p.to_agents) j
               JOIN agents a ON a.name = j.value AND a.active = 1 AND a.is_human = 0
               WHERE p.thread_id = :t AND p.needs_response = 1 AND p.sealed = 0 AND j.value != p.agent
                 AND NOT EXISTS (SELECT 1 FROM posts q WHERE q.thread_id = p.thread_id AND q.agent = j.value
                                 AND q.id > p.id)
               ORDER BY p.id""", {"t": thread_id}):
        asks.setdefault(r["agent"], []).append(r["id"])
    # (a') The thread's last unsealed post went to an agent (any type), which therefore has not answered it.
    last = c.execute("SELECT id FROM posts WHERE thread_id = ? AND sealed = 0 ORDER BY id DESC LIMIT 1",
                     (thread_id,)).fetchone()
    if last is not None:
        for r in c.execute(
                """SELECT p.id, j.value AS agent FROM posts p, json_each(p.to_agents) j
                   JOIN agents a ON a.name = j.value AND a.active = 1 AND a.is_human = 0
                   WHERE p.id = ? AND j.value != p.agent""", (last["id"],)):
            if r["id"] not in asks.get(r["agent"], []):
                asks.setdefault(r["agent"], []).append(r["id"])
    for agent, ids in asks.items():
        reasons.append({"kind": "unanswered", "agent": agent, "post_ids": ids})
    # (b) Tasks whose owner is blocked or let the lease expire.
    marks = ",".join("?" * len(TERMINAL))
    for r in c.execute(
            f"""SELECT tk.id, tk.status, tk.owner_agent, tk.lease_expires_at FROM tasks tk
                JOIN agents a ON a.name = tk.owner_agent AND a.active = 1 AND a.is_human = 0
                WHERE tk.thread_id = ? AND tk.status NOT IN ({marks})
                  AND (tk.status = 'blocked' OR (tk.lease_expires_at IS NOT NULL AND tk.lease_expires_at <= ?))
                ORDER BY tk.id""", (thread_id, *TERMINAL, now)):
        kind = "blocked_task" if r["status"] == "blocked" else "expired_lease"
        reasons.append({"kind": kind, "agent": r["owner_agent"], "task_id": r["id"]})
    agents: list[str] = []
    for x in reasons:
        if x["agent"] not in agents:
            agents.append(x["agent"])
    return agents, reasons


def _ids(ids: list[int]) -> str:
    shown = [f"#{i}" for i in ids[:MAX_IDS_PER_AGENT]]
    if len(ids) > MAX_IDS_PER_AGENT:
        shown.append(f"{len(ids) - MAX_IDS_PER_AGENT} more")
    return shown[0] if len(shown) == 1 else ", ".join(shown[:-1]) + " and " + shown[-1]


def build_body(agents: list[str], reasons: list[dict]) -> str:
    """The fixed request text. Only ids and agent names (server-stamped) vary; nothing an agent wrote."""
    parts = []
    for x in reasons:
        if x["kind"] == "unanswered":
            verb = "has" if len(x["post_ids"]) == 1 else "have"
            parts.append(f"{_ids(x['post_ids'])} {verb} had no reply from {x['agent']}")
        elif x["kind"] == "blocked_task":
            parts.append(f"task {x['task_id']} is blocked (owner {x['agent']})")
        else:
            parts.append(f"the lease on task {x['task_id']} expired (owner {x['agent']})")
    if len(parts) > MAX_REASONS:
        parts = parts[:MAX_REASONS] + [f"{len(parts) - MAX_REASONS} more"]
    who = "you" if len(agents) == 1 else ", ".join(agents)
    return f"Unstick: this thread is stalled on {who} ({'; '.join(parts)}). {BODY_INSTRUCTIONS}"


def _reserve(board: Board, p: Principal, thread_id: int) -> None:
    """Check and stamp the per-thread cooldown in one write transaction (two clicks cannot both pass)."""
    key, now = STATE_PREFIX + str(thread_id), board.now()
    with db.write_tx(board.conn) as c:
        row = c.execute("SELECT value FROM board_state WHERE key = ?", (key,)).fetchone()
        try:
            last = float(json.loads(row["value"])) if row else None
        except (TypeError, ValueError):
            last = None
        if last is not None and now - last < UNSTICK_COOLDOWN_SECONDS:
            wait = int(UNSTICK_COOLDOWN_SECONDS - (now - last)) + 1
            raise Conflict(f"thread {thread_id} was unstuck less than {UNSTICK_COOLDOWN_SECONDS // 60} minutes ago; "
                           f"give the agents a moment (try again in {wait} s)")
        c.execute("""INSERT INTO board_state(key, value, updated_by, updated_at) VALUES (?, ?, ?, ?)
                     ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_by = excluded.updated_by,
                     updated_at = excluded.updated_at""", (key, json.dumps(now), p.name, now))


def _release(board: Board, thread_id: int) -> None:
    with db.write_tx(board.conn) as c:
        c.execute("DELETE FROM board_state WHERE key = ?", (STATE_PREFIX + str(thread_id),))


def unstick(board: Board, p: Principal, thread_id: int, config: dispatch.DispatchConfig) -> dict[str, Any]:
    board._require_human(p, "unstick a thread")
    thread = board._thread_row(thread_id)
    if thread["status"] != "open":
        raise Conflict(f"thread {thread_id} is closed; reopen it first")
    agents, reasons = stuck_agents(board, thread_id)
    if not agents:
        raise Conflict("nothing here is waiting on an agent (no unanswered requests to an agent, blocked tasks or "
                       "expired leases); if the thread is waiting on you, reply to it")
    _reserve(board, p, thread_id)
    rule = None
    try:
        covered = {a for r in board.active_dispatch_rules(p) if r["thread_id"] == thread_id for a in r["agents"]}
        uncovered = [a for a in agents if a not in covered]
        if uncovered:
            # Before the post: the dispatcher only triggers on posts created at or after a rule.
            rule = board.create_dispatch_rule(p, thread_id=thread_id, agents=uncovered,
                                              purpose=PURPOSE.format(thread=thread_id), max_launches=len(uncovered),
                                              expires_at=board.now() + UNSTICK_RULE_HOURS * 3600)
        post = board.create_post(p, board.human_session(p), body=build_body(agents, reasons), type="request",
                                 thread_id=thread_id, to=agents, needs_response=True)
    except Exception:
        if rule is not None:
            board.revoke_dispatch_rule(p, rule["id"])
        _release(board, thread_id)
        raise
    status = dispatch.loop_status(board, config)
    now, window = board.now(), config.live_minutes * 60
    live = [a for a in agents if (board.conn.execute("SELECT MAX(last_seen) FROM sessions WHERE agent = ?", (a,))
                                  .fetchone()[0] or float("-inf")) >= now - window]
    if status.get("running"):
        live += [a for a in sorted({r.get("agent") for r in dispatch._active_records(board)} & set(agents))
                 if a not in live]
    runtimes = {r["name"]: r["runtime"] for r in board.conn.execute("SELECT name, runtime FROM agents")}
    return {"post_id": post["id"], "thread_id": thread_id, "agents": agents,
            "rule_id": rule["id"] if rule else None,
            "dispatcher_running": bool(status.get("running")), "paused": board.is_paused(),
            "live_agents": live,
            "no_runner": [a for a in agents if config.runner_for(a, runtimes.get(a)) is None],
            "reasons": reasons}
