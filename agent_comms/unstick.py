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

from typing import Any

from . import dispatch, human_actions
from .core import TERMINAL, Board, Conflict, Principal

UNSTICK_COOLDOWN_SECONDS = 120
UNSTICK_RULE_HOURS = human_actions.RULE_HOURS
STATE_PREFIX = "unstick.thread."
MAX_IDS_PER_AGENT = 5    # post ids listed per agent in the body; the rest are counted
MAX_REASONS = 20         # reasons listed in the body (keeps it far below the body size limit)

PURPOSE = ("Unstick thread {thread}: diagnose why it stalled, resolve it, and propose a prevention; "
           "stay within the thread's existing request.")
BODY_INSTRUCTIONS = ("Find the root cause of the stall, resolve it now, and post a `finding` with the cause plus a "
                     "`proposal` for preventing it next time, with an empty `to` so it reaches the human. Stay within what this "
                     "thread already asked for.")


def stuck_agents(board: Board, thread_id: int) -> tuple[list[str], list[dict]]:
    """The active non-human agents this thread is waiting on, and why. Reads metadata only (ids, agents, flags,
    statuses, times), never bodies, titles or summaries. No age threshold: the human chose to ask."""
    c, now = board.conn, board.now()
    reasons: list[dict] = []
    # Completion belongs to each original request/recipient, never a later arbitrary reply.
    from . import requests
    asks: dict[str, list[int]] = {}
    active = {r["name"] for r in c.execute("SELECT name FROM agents WHERE active=1 AND is_human=0")}
    for post in c.execute("SELECT * FROM posts WHERE thread_id=? AND sealed=0 ORDER BY id", (thread_id,)):
        for request in requests.for_post(board, post):
            agent = request["assigned_agent"] or request["recipient"]
            if request["state"] != "finished" and agent in active:
                asks.setdefault(agent, []).append(post["id"])
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


def unstick(board: Board, p: Principal, thread_id: int, config: dispatch.DispatchConfig) -> dict[str, Any]:
    board._require_human(p, "unstick a thread")
    thread = board._thread_row(thread_id)
    if thread["status"] != "open":
        raise Conflict(f"thread {thread_id} is closed; reopen it first")
    agents, reasons = stuck_agents(board, thread_id)
    if not agents:
        raise Conflict("nothing here is waiting on an agent (no unanswered requests to an agent, blocked tasks or "
                       "expired leases); if the thread is waiting on you, reply to it")
    key = STATE_PREFIX + str(thread_id)
    human_actions.reserve_cooldown(
        board, p, key, UNSTICK_COOLDOWN_SECONDS,
        lambda wait: (f"thread {thread_id} was unstuck less than {UNSTICK_COOLDOWN_SECONDS // 60} minutes ago; "
                      f"give the agents a moment (try again in {wait} s)"))
    try:
        post, rule = human_actions.post_as_human(board, p, thread_id=thread_id, body=build_body(agents, reasons),
                                                 type="request", to=agents, needs_response=True, launch=agents,
                                                 purpose=PURPOSE.format(thread=thread_id))
    except Exception:
        human_actions.release_cooldown(board, key)
        raise
    # Where the request will be seen now (`sessions`): the target agents' sessions inside the dispatcher's live
    # window, most recently seen first. Sessions a launch registers later are found by the page from their start time.
    return {"post_id": post["id"], "thread_id": thread_id, "agents": agents,
            "rule_id": rule["id"] if rule else None,
            **human_actions.launch_outlook(board, config, agents), "reasons": reasons}
