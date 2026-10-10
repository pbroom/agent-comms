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
- The rule is always a fresh one naming exactly the stuck agents, recorded against the post (the dispatcher launches
  for this post only under it, so its purpose is the one in the prompt), with a budget of one launch each, and expires after UNSTICK_RULE_HOURS. The rule is created before the post, because the dispatcher
  ignores posts older than a rule.
- One unstick per thread per UNSTICK_COOLDOWN_SECONDS, so a double click cannot post twice.
"""

from __future__ import annotations

import json
from typing import Any

from . import awaiting, dispatch, human_actions
from .core import TERMINAL, Board, Conflict, Principal

UNSTICK_COOLDOWN_SECONDS = 120
UNSTICK_RULE_HOURS = human_actions.RULE_HOURS
STATE_PREFIX = "unstick.thread."
MAX_IDS_PER_AGENT = 5    # post ids listed per agent in the body; the rest are counted
MAX_REASONS = 20         # reasons listed in the body (keeps it far below the body size limit)

PURPOSE = ("Unstick thread {thread}: diagnose why it stalled, resolve it, and propose a prevention; "
           "stay within the thread's existing request.")
BODY_INSTRUCTIONS = ("Find the root cause of the stall, resolve it now, and post a `finding` with the cause plus a "
                     "`proposal` for preventing it next time, with an empty `to` and a `decision_question` so it "
                     "reaches the human. Stay within what this thread already asked for.")
# With a prevention inbox ([unstick] prevention_owner / prevention_thread, prevention.py): the cause stays here, the
# prevention proposal goes to the inbox's owner, not to the human.
PREVENTION_PURPOSE = ("Unstick thread {thread}: diagnose why it stalled, resolve it, and send a prevention proposal to "
                      "the prevention inbox (thread {inbox}, {owner}); stay within the thread's existing request.")
BODY_INSTRUCTIONS_INBOX = ("Find the root cause of the stall, resolve it now, and post a `finding` with the cause in "
                           "this thread. {prevention} Stay within what this thread already asked for.")


def instructions(board: Board, thread_id: int) -> tuple[str, str]:
    """(body instructions, rule purpose) for an Unstick on this thread: today's wording, or the prevention inbox's."""
    from . import prevention
    cfg = prevention.active(board)
    if cfg is None:
        return BODY_INSTRUCTIONS, PURPOSE.format(thread=thread_id)
    return (BODY_INSTRUCTIONS_INBOX.format(prevention=prevention.instructions(board, cfg, thread_id)),
            PREVENTION_PURPOSE.format(thread=thread_id, inbox=cfg.thread_id, owner=cfg.owner))


def stuck_agents(board: Board, thread_id: int) -> tuple[list[str], list[dict]]:
    """The active non-human agents this thread is waiting on, and why: unanswered requests (not ones pickup treats as
    in progress), blocked tasks and expired leases (their owner), and accepted tasks nobody claimed (their creator). Reads metadata only (ids, agents, flags,
    statuses, times), never bodies, titles or summaries. No age threshold: the human chose to ask."""
    c, now = board.conn, board.now()
    reasons: list[dict] = []
    # Completion belongs to each original request/recipient, never a later arbitrary reply. A request pickup treats as
    # in progress (its assigned session acknowledged the start and was seen recently, or holds a live lease on the
    # linked task: pickup.classify, the one rule) is not silent, so its owner is not asked. Read-only: nothing here
    # finishes a request or renews a lease.
    from . import pickup, requests
    asks: dict[str, list[int]] = {}
    active = {r["name"] for r in c.execute("SELECT name FROM agents WHERE active=1 AND is_human=0")}
    for post in c.execute("SELECT * FROM posts WHERE thread_id=? AND sealed=0 ORDER BY id", (thread_id,)):
        for request in requests.for_post(board, post):
            agent = request["assigned_agent"] or request["recipient"]
            if (request["state"] != "finished" and agent in active
                    and not pickup.in_progress(board, post, request)):
                asks.setdefault(agent, []).append(post["id"])
    for agent, ids in asks.items():
        reasons.append({"kind": "unanswered", "agent": agent, "post_ids": ids})
    # (b) Tasks whose owner is blocked or let the lease expire. A task that waits on unfinished dependencies (in any
    # thread) is awaiting that work, not stalled: Unstick cannot help it (awaiting.py).
    marks = ",".join("?" * len(TERMINAL))
    for r in c.execute(
            f"""SELECT tk.id, tk.status, tk.owner_agent, tk.lease_expires_at FROM tasks tk
                JOIN agents a ON a.name = tk.owner_agent AND a.active = 1 AND a.is_human = 0
                WHERE tk.thread_id = ? AND tk.status NOT IN ({marks})
                  AND (tk.status = 'blocked' OR (tk.lease_expires_at IS NOT NULL AND tk.lease_expires_at <= ?))
                  AND NOT {awaiting.AWAITS.format(t='tk')}
                ORDER BY tk.id""", (thread_id, *TERMINAL, now)):
        kind = "blocked_task" if r["status"] == "blocked" else "expired_lease"
        reasons.append({"kind": kind, "agent": r["owner_agent"], "task_id": r["id"]})
    # (c) Accepted tasks nobody owns: the agent that created one is asked to claim it, or decline it when finished
    # work already covers it. A task still waiting on unfinished prerequisites cannot be claimed and is left out.
    for r in unclaimed_tasks(board, thread_id):
        reasons.append({"kind": "unclaimed_task", "agent": r["created_by"], "task_id": r["id"]})
    agents: list[str] = []
    for x in reasons:
        if x["agent"] not in agents:
            agents.append(x["agent"])
    return agents, reasons


def unclaimed_tasks(board: Board, thread_id: int | None = None, updated_before: float | None = None) -> list:
    """Accepted, unowned tasks created by an active non-human agent whose prerequisites are all done or declined (in
    any thread; awaiting.py), oldest first. Rows carry id, thread_id, created_by and updated_at only (never the
    title)."""
    q = f"""SELECT tk.id, tk.thread_id, tk.created_by, tk.updated_at, tk.depends_on FROM tasks tk
           JOIN agents a ON a.name = tk.created_by AND a.active = 1 AND a.is_human = 0
           WHERE tk.status = 'accepted' AND tk.owner_agent IS NULL AND NOT {awaiting.AWAITS.format(t='tk')}"""
    args: list = []
    if thread_id is not None:
        q += " AND tk.thread_id = ?"
        args.append(thread_id)
    if updated_before is not None:
        q += " AND tk.updated_at <= ?"
        args.append(updated_before)
    return list(board.conn.execute(q + " ORDER BY tk.id", args))


AGENT_PREFIX = "unstick.agent."      # <thread id>.<agent>: when an Unstick last asked this agent on this thread
SCOPED_PREFIX = "unstick.scoped."    # <thread id>: the thread stamp of the latest Unstick that recorded its agents


def _stamp(conn, key: str) -> float | None:
    row = conn.execute("SELECT value FROM board_state WHERE key = ?", (key,)).fetchone()
    try:
        value = json.loads(row["value"]) if row else None
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def record_coverage(board: Board, p: Principal, thread_id: int, agents: list[str]) -> None:
    """Record which agents this Unstick asked, stamped with the thread's cooldown stamp it just reserved. Call inside
    the post's write transaction. The thread stamp (STATE_PREFIX) stays the double-click cooldown for the whole thread;
    these per-agent stamps say whose stall the human already took up (unstuck_since), so a one-click Unstick
    for one agent does not silence automatic recovery for another agent's stall on the same thread."""
    at = _stamp(board.conn, STATE_PREFIX + str(thread_id))
    if at is None:
        return
    rows = [(AGENT_PREFIX + f"{thread_id}.{a}", at) for a in agents] + [(SCOPED_PREFIX + str(thread_id), at)]
    for key, value in rows:
        board.conn.execute("""INSERT INTO board_state(key, value, updated_by, updated_at) VALUES (?, ?, ?, ?)
                              ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_by = excluded.updated_by,
                              updated_at = excluded.updated_at""", (key, json.dumps(value), p.name, board.now()))


def unstuck_since(conn, thread_id: int, agent: str, since: float) -> bool:
    """An Unstick on this thread asked `agent` at or after `since`. A thread stamp written before agents were recorded
    (no SCOPED_PREFIX record for that stamp) counts for every agent, as it did."""
    at = _stamp(conn, STATE_PREFIX + str(thread_id))
    if at is None or at < since:
        return False
    if _stamp(conn, SCOPED_PREFIX + str(thread_id)) != at:
        return True     # the latest stamp is a legacy, unscoped one: it covered the whole thread
    asked = _stamp(conn, AGENT_PREFIX + f"{thread_id}.{agent}")
    return asked is not None and asked >= since


def _ids(ids: list[int]) -> str:
    shown = [f"#{i}" for i in ids[:MAX_IDS_PER_AGENT]]
    if len(ids) > MAX_IDS_PER_AGENT:
        shown.append(f"{len(ids) - MAX_IDS_PER_AGENT} more")
    return shown[0] if len(shown) == 1 else ", ".join(shown[:-1]) + " and " + shown[-1]


def build_body(agents: list[str], reasons: list[dict], instructions: str = BODY_INSTRUCTIONS) -> str:
    """The fixed request text. Only ids and agent names (server-stamped) vary; nothing an agent wrote."""
    parts = []
    for x in reasons:
        if x["kind"] == "unanswered":
            verb = "has" if len(x["post_ids"]) == 1 else "have"
            parts.append(f"{_ids(x['post_ids'])} {verb} had no reply from {x['agent']}")
        elif x["kind"] == "blocked_task":
            parts.append(f"task {x['task_id']} is blocked (owner {x['agent']})")
        elif x["kind"] == "unclaimed_task":
            parts.append(f"task {x['task_id']} is accepted but unclaimed (created by {x['agent']}): claim it or "
                         "decline it if finished work already covers it")
        else:
            parts.append(f"the lease on task {x['task_id']} expired (owner {x['agent']})")
    if len(parts) > MAX_REASONS:
        parts = parts[:MAX_REASONS] + [f"{len(parts) - MAX_REASONS} more"]
    who = "you" if len(agents) == 1 else ", ".join(agents)
    return f"Unstick: this thread is stalled on {who} ({'; '.join(parts)}). {instructions}"


def unstick(board: Board, p: Principal, thread_id: int, config: dispatch.DispatchConfig | None, *,
            only_agents: list[str] | None = None, _in_transaction: bool = False) -> dict[str, Any]:
    """`only_agents` (a decision action, decision_actions.py): ask and launch only those of these agents the thread
    still waits on, with only their reasons; refuse (409) when none of them is. This bounds what a question (an agent
    may write one) can launch to the agents it names. `_in_transaction`: run inside the caller's write transaction (the
    human's answer to that question), so the cooldown stamp, the rule and the post commit or roll back with the
    answer; the result then has no launch outlook (`config` is unused)."""
    board._require_human(p, "unstick a thread")
    thread = board._thread_row(thread_id)
    if thread["status"] != "open":
        raise Conflict(f"thread {thread_id} is closed; reopen it first")
    agents, reasons = stuck_agents(board, thread_id)
    if not agents:
        raise Conflict("nothing here is waiting on an agent (no unanswered requests to an agent, blocked tasks, "
                       "expired leases or unclaimed tasks); if the thread is waiting on you, reply to it")
    if only_agents is not None:
        agents = [a for a in agents if a in only_agents]
        reasons = [r for r in reasons if r["agent"] in agents]
        if not agents:
            raise Conflict(f"thread {thread_id} no longer waits on {', '.join(only_agents)}; reload before choosing")
    key = STATE_PREFIX + str(thread_id)
    human_actions.reserve_cooldown(
        board, p, key, UNSTICK_COOLDOWN_SECONDS,
        lambda wait: (f"thread {thread_id} was unstuck less than {UNSTICK_COOLDOWN_SECONDS // 60} minutes ago; "
                      f"give the agents a moment (try again in {wait} s)"), _in_transaction=_in_transaction)
    text, purpose = instructions(board, thread_id)

    def link_recovery(post):
        record_coverage(board, p, thread_id, agents)    # in the post's transaction: rolls back with it
        from . import prevention
        prevention.mark_unstick_post(board, post["id"], p.name)   # a prevention proposal may name it
        # Server-created links only, frozen with the post; never infer lineage from prose.
        from . import recovery, workstreams
        for agent in agents:
            own_reasons = [r for r in reasons if r['agent'] == agent]
            if any(r['kind'] != 'unanswered' for r in own_reasons):
                continue
            sources, eligible = [], True
            for reason in own_reasons:
                for source_id in dict.fromkeys(reason['post_ids']):
                    source = board.get_post(p, source_id)
                    rows = [r for r in source['requests'] if r['assigned_agent'] == agent and r['state'] != 'finished']
                    if (not rows or workstreams.get_for_post(board, source_id) is not None or
                            board.conn.execute('SELECT 1 FROM board_state WHERE key LIKE ?',
                                               (recovery.PREFIX + str(source_id) + '.%',)).fetchone()):
                        eligible = False
                        break
                    for row in rows:
                        if row['state'] not in ('queued', 'blocked'):
                            eligible = False
                            break
                        sources.append({'post_id': source_id, 'recipient': row['recipient'], 'version': row['version']})
            if eligible and sources and len(sources) <= 100:
                unique = {(r['post_id'], r['recipient']): r for r in sources}
                recovery.record(board, p, post['id'], agent, list(unique.values()), requires_diagnostics=True)

    try:
        post, rule = human_actions.post_as_human(board, p, thread_id=thread_id, body=build_body(agents, reasons, text),
                                                 type="request", to=agents, needs_response=True, launch=agents,
                                                 purpose=purpose, post_hook=link_recovery,
                                                 _in_transaction=_in_transaction)
    except Exception:
        if not _in_transaction:     # inside the caller's transaction, its rollback undoes the stamp
            human_actions.release_cooldown(board, key)
        raise
    if _in_transaction:
        return {"post_id": post["id"], "thread_id": thread_id, "agents": agents,
                "rule_id": rule["id"] if rule else None, "reasons": reasons}
    # Where the request will be seen now (`sessions`): the target agents' sessions inside the dispatcher's live
    # window, most recently seen first, and `sessions_detail`: those sessions in the snapshot's session shape (the
    # snapshot lists only the 30 most recently seen). Sessions a launch registers later are found by the page from
    # their start time.
    outlook = human_actions.launch_outlook(board, config, agents)
    return {"post_id": post["id"], "thread_id": thread_id, "agents": agents,
            "rule_id": rule["id"] if rule else None, **outlook,
            "sessions_detail": board.session_details(p, outlook["sessions"]), "reasons": reasons}
