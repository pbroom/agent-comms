"""Automatic recovery of abandoned and unclaimed work (DESIGN_NOTES "Automatic recovery").

Two kinds of stall used to wait for the human to click Unstick, sometimes for hours:
- *Abandoned work*: a task in `working` whose lease expired while its owner session has not been seen since, for
  longer than the session's grace (recovery.abandon_grace: ten minutes for a dispatcher session, a full lease TTL
  for an interactive one, which may only be quiet). Its requests stay `started` by a session that is gone.
- *An unclaimed task*: `accepted`, nobody owns it, unchanged for longer than the dashboard's stall window
  (UNCLAIMED_AFTER_SECONDS: 30 minutes plus 10 minutes grace), created by an agent.

The running dispatcher asks the owner (or the creator of an unclaimed task) to recover it, with the Unstick
machinery: a fixed `request` posted as the human and a fresh one-shot dispatcher rule bound to that post, so an agent
without a live session is launched once (human_actions.post_as_human).

Authorization: the human's board setting `tasks.auto_recover_stalled_work` (human-only, Settings page, default on)
is the standing approval that a click on Unstick would otherwise give, and only for these two stalls. Guardrails:
- Only the dispatcher loop that owns the board (its fence token is checked inside the post's write transaction),
  never while the board is paused, only while the setting is on, only on open threads not at their agent-post cap,
  and only for an active non-human agent that has a configured runner.
- Only recent stalls (MAX_STALL_AGE_SECONDS), at most MAX_SENDS_PER_PASS launches per pass, and durable launch
  budgets per rolling day across both kinds: AGENT_BUDGET per (agent, thread) and AGENT_DAILY_BUDGET per agent over
  all threads, so an agent cannot get itself launched over and over.
- Only work the human authorized launches. Abandoned work: the task is human- or grant-authorized, or the human
  sent that agent a request or handoff on the thread (not an automatic one) before the task was claimed; otherwise an agent could open a thread,
  claim its own task, go silent and be launched, again and again. An unclaimed task: the human accepted it or an
  active standing grant covers it. Anything else goes to the human instead. A `blocked` task, and any stall on a
  thread that already waits on the human (a Needs you post, or a shared issue awaiting the human), also goes to the
  human, never to a launch. Posts to the human are capped at ESCALATION_DAILY_CAP a day per responsible agent;
  stalls over the cap are recorded `suppressed`, silently (no post, no launch).
- Fixed server-side text. The body and the rule purpose vary only in thread, task, post and session ids and agent
  names; nothing an agent wrote (bodies, titles, summaries, request reasons) is read into them. The body says plainly
  that it is an automatic recovery under the human's setting, not a human click. The post is marked automatic
  (POST_PREFIX), so it does not lift the agent-post cap (Board.NOT_AUTOMATIC) and the dashboard can label it.
- Once only. At most one automatic recovery per (task id, lease_expires_at) for abandoned work, one per task for an
  unclaimed task, and MAX_PER_TASK until the task is finished or declined (ATTEMPTS_PREFIX counters). Each is recorded durably in `board_state` in the same
  transaction as the post, so a restarted dispatcher never repeats it. Not after the human clicked Unstick on the
  stall (checked again inside the transaction).
- Escalation. A recovery that does not take (its request ends `blocked`, or the task has not moved within one lease
  TTL of the actual launch, or of the post when the agent has a live interactive session and no launch is needed)
  gets no further automatic launch: its one-shot rule is revoked and the human is told with a needs-response
  `question` addressed to nobody (Board.NEEDS_YOU, the dashboard's "Needs you"), and the dashboard shows the precise
  reason. The question carries a decision_question built from server facts only (escalation_question): a recommended
  option and an alternative, most of them one-click decision actions (Unstick, decline or release the task). A
  request held by a sticky browser denial (browser_readiness.request_blocker) is never relaunched around: it goes
  straight to the human.

A continuation (DESIGN_NOTES "Awaiting another thread"): a task whose depends_on was set while a dependency was
unfinished (awaiting.STATE_PREFIX) is awaiting that work, so it is never treated as stalled here. When its last
dependency is done or declined, the dispatcher asks the agent that should continue it (its owner if that holds a live
lease, else its creator) with the fixed text "task N's dependencies are finished; continue it", once per
dependency-satisfaction event (CONTINUE_PREFIX, keyed by the depends_on generation), under the same guards, budgets
and authorization rules as an unclaimed or abandoned task; otherwise it escalates the same way.

A third case (DESIGN_NOTES "Automatic owner handoff"): a *recovery wait*. board_recover_request_owner hit a transient
blocker (another session or run still busy in the owner's checkout) and recorded a wait (recovery.WAIT_PREFIX). Under
the same setting, fence and pause guards, the dispatcher relaunches that agent once the worktree is free, at most
recovery.WAIT_RETRIES times per request per rolling day, and only then asks the human (_process_waits).
"""

from __future__ import annotations

import json
import logging
from typing import Any, Callable

from . import awaiting, browser_readiness, db, human_actions, issues, recovery, requests, unstick, workstreams
from .core import TERMINAL, Board, Conflict, Principal, iso

log = logging.getLogger("agent_comms.autorecover")

SETTING = "auto_recover_stalled_work"
GRACE_SECONDS = recovery.ABANDON_GRACE_SECONDS        # the dashboard's GRACE_MINUTES
STALL_SECONDS = 30 * 60                               # the dashboard's STALL_MINUTES
UNCLAIMED_AFTER_SECONDS = STALL_SECONDS + GRACE_SECONDS
MAX_STALL_AGE_SECONDS = 24 * 3600   # older stalls are left to the human (no flood of launches after an upgrade)
MAX_SENDS_PER_PASS = 3              # recovery requests (each may launch an agent) per dispatcher pass
MAX_ESCALATIONS_PER_PASS = 5        # posts to the human per pass
ESCALATION_DAILY_CAP = 5            # posts to the human per responsible agent per rolling day; the rest are silent
AGENT_BUDGET = 2                    # automatic launches per (agent, thread) ...
AGENT_DAILY_BUDGET = 6              # ... and per agent across all threads ...
BUDGET_WINDOW_SECONDS = 24 * 3600   # ... per rolling day
MAX_PER_TASK = 3                    # automatic recoveries of one task, until it is finished or declined
RECORD_TTL_SECONDS = 7 * 24 * 3600  # settled records and replaced-lease evidence are pruned after this
PRUNE_EVERY_SECONDS = 3600
MAX_ITEMS = 20                      # items named in one post (the rest are counted)
LIST_MAX = 100                      # records sent to the dashboard

PREFIX = "auto_recovery."
TASK_PREFIX = PREFIX + "task."              # <task id>.<lease_expires_at>: abandoned work
UNCLAIMED_PREFIX = PREFIX + "unclaimed."    # <task id>: an unclaimed task
CONTINUE_PREFIX = PREFIX + "continue."      # <task id>.<depends_on generation>: its dependencies finished
POST_PREFIX = PREFIX + "post."              # <post id>: a post made automatically (core.Board.NOT_AUTOMATIC)
BUDGET_PREFIX = PREFIX + "budget."          # <agent>.<thread id>: launch times in the rolling window
AGENT_BUDGET_PREFIX = PREFIX + "agent_budget."  # <agent>: launch times in the rolling window, all threads
ATTEMPTS_PREFIX = PREFIX + "attempts."      # <task id>: automatic recoveries sent, kept until the task is terminal
ESCALATIONS_PREFIX = PREFIX + "escalations."  # <agent>: times of escalation posts about this agent's stalls
PRUNED_KEY = PREFIX + "pruned_at"

HEADER = ("Automatic recovery: the dispatcher sent this under the human's board setting auto_recover_stalled_work; "
          "it is not a human click.")
INSTRUCTIONS = ("Reclaim an abandoned task with board_claim_task first, then take over the requests its old session "
                "still holds with board_recover_request_owner (reread each request's version), and finish the work or "
                "release it. Claim an unclaimed task, or decline it (board_update_task status=declined) if finished "
                "work already covers it. Stay within what this thread already asked for, and reply to this request "
                "(request_reply) when you are done or blocked.")
PURPOSE = ("Automatic recovery on thread {thread} (the human's board setting auto_recover_stalled_work): reclaim, "
           "finish, release or decline the abandoned or unclaimed tasks named in the recovery request; stay within "
           "the thread's existing request.")
CONTINUE_INSTRUCTIONS = ("Claim (or reclaim) it with board_claim_task, then finish it, or release it with a `status` "
                         "saying what remains. A claim needs every dependency done: if one was declined, first remove "
                         "it with board_update_task(depends_on=[...]) or decline the task if nothing is left to do. "
                         "Stay within what this thread already asked for, and reply to this request (request_reply) "
                         "when you are done or blocked.")
CONTINUE_PURPOSE = ("Automatic recovery on thread {thread} (the human's board setting auto_recover_stalled_work): "
                    "continue the tasks named in the request, whose dependencies are finished; stay within the "
                    "thread's existing request.")
# With a prevention inbox (prevention.py), a stall recovery also asks for the cause and a prevention proposal there.
PREVENTION_INSTRUCTIONS = "Post a `finding` with the cause of the stall in this thread. {prevention}"
ESCALATION = ("Automatic recovery did not take (sent by the dispatcher under the human's board setting "
              "auto_recover_stalled_work; not a human click): {items}. No further automatic launches will be made "
              "for {them}. This needs you: {next}")
ESCALATION_NEXT = "pick one of the options below, or open the thread and check the named request and task."
# Only when its question could not be built (escalation_question failed): then there are no options to pick.
ESCALATION_NEXT_PLAIN = ("open the thread and check the named request and task, then Unstick, reassign or decline "
                         "the task.")


def task_key(task_id: int, lease_expires_at: float) -> str:
    return f"{TASK_PREFIX}{int(task_id)}.{float(lease_expires_at)!r}"


def unclaimed_key(task_id: int) -> str:
    return f"{UNCLAIMED_PREFIX}{int(task_id)}"


def continue_key(task_id: int, generation: int) -> str:
    return f"{CONTINUE_PREFIX}{int(task_id)}.{int(generation)}"


def budget_key(agent: str, thread_id: int) -> str:
    return f"{BUDGET_PREFIX}{agent}.{int(thread_id)}"


def agent_budget_key(agent: str) -> str:
    return f"{AGENT_BUDGET_PREFIX}{agent}"


def escalations_key(agent: str) -> str:
    return f"{ESCALATIONS_PREFIX}{agent}"


def attempts_key(task_id: int) -> str:
    return f"{ATTEMPTS_PREFIX}{int(task_id)}"


def enabled(board: Board) -> bool:
    return getattr(board.s, SETTING, True) is True


def _range(prefix: str) -> tuple[str, str]:
    """[prefix, end) for an index range scan over board_state's primary key (LIKE would scan every key)."""
    return prefix, prefix[:-1] + chr(ord(prefix[-1]) + 1)


def _keys(conn, prefix: str, columns: str = "key, value"):
    return conn.execute(f"SELECT {columns} FROM board_state WHERE key >= ? AND key < ? ORDER BY key", _range(prefix))


def _get(conn, key: str) -> Any:
    row = conn.execute("SELECT value FROM board_state WHERE key = ?", (key,)).fetchone()
    if row is None:
        return None
    try:
        return json.loads(row["value"])
    except (TypeError, ValueError):
        return None


def _insert(conn, key: str, value: Any, now: float) -> None:
    # A plain INSERT: a second writer of the same key fails, so a recovery can never be recorded twice.
    conn.execute("INSERT INTO board_state(key, value, updated_by, updated_at) VALUES (?, ?, 'dispatcher', ?)",
                 (key, json.dumps(value), now))


def _upsert(conn, key: str, value: Any, now: float) -> None:
    conn.execute("""INSERT INTO board_state(key, value, updated_by, updated_at) VALUES (?, ?, 'dispatcher', ?)
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_by = excluded.updated_by,
                    updated_at = excluded.updated_at""", (key, json.dumps(value), now))


def _update(conn, key: str, value: Any, now: float) -> None:
    conn.execute("UPDATE board_state SET value = ?, updated_by = 'dispatcher', updated_at = ? WHERE key = ?",
                 (json.dumps(value), now, key))


def _records(conn) -> list[tuple[str, dict]]:
    out = []
    for prefix in (TASK_PREFIX, UNCLAIMED_PREFIX, CONTINUE_PREFIX):
        for key, value in _keys(conn, prefix):
            try:
                rec = json.loads(value)
            except (TypeError, ValueError):
                continue
            if isinstance(rec, dict) and type(rec.get("task_id")) is int:
                out.append((key, rec))
    return out


def _owner_ok(conn, fence: tuple[str, str]) -> bool:
    key, value = fence
    row = conn.execute("SELECT value FROM board_state WHERE key = ?", (key,)).fetchone()
    return row is not None and row["value"] == value


def _at_cap(board: Board, thread_id: int) -> bool:
    return board._agent_posts_since_human(thread_id) >= board.s.max_agent_posts_per_thread_without_human


def _attempts(conn, task_id: int) -> int:
    """Automatic recoveries sent for this task. The counter outlives the per-stall records (pruned after a week) and is
    dropped only when the task is finished, declined or deleted, so MAX_PER_TASK holds for the task's whole life."""
    count = _get(conn, attempts_key(task_id))
    return count if isinstance(count, int) and not isinstance(count, bool) else 0


def _times(conn, key: str, now: float) -> list[float]:
    times = _get(conn, key)
    return [t for t in times if isinstance(t, (int, float)) and not isinstance(t, bool)
            and t > now - BUDGET_WINDOW_SECONDS] if isinstance(times, list) else []


def _budget_used(conn, agent: str, thread_id: int, now: float) -> list[float]:
    return _times(conn, budget_key(agent, thread_id), now)


def _budget_reason(conn, agent: str, thread_id: int, now: float) -> str | None:
    """Why the agent may not be launched automatically now (a spent launch budget), or None."""
    if len(_budget_used(conn, agent, thread_id, now)) >= AGENT_BUDGET:
        return (f"the automatic launch budget for {agent} on this thread ({AGENT_BUDGET} per 24 hours) is spent, so "
                "nothing was launched")
    if len(_times(conn, agent_budget_key(agent), now)) >= AGENT_DAILY_BUDGET:
        return (f"the daily automatic launch budget for {agent} ({AGENT_DAILY_BUDGET} per 24 hours across all "
                "threads) is spent, so nothing was launched")
    return None


def _ids(ids: list[int]) -> str:
    return unstick._ids(ids)


def _unstuck_since(conn, thread_id: int, agent: str | None, since: float) -> bool:
    """The human's Unstick on this thread asked this stall's agent after the stall began: they already asked, so the
    dispatcher does not ask (and launch) a second time for the same stall. An Unstick limited to other agents (a
    one-click decision action) leaves this agent's stall to automatic recovery (unstick.unstuck_since)."""
    return unstick.unstuck_since(conn, thread_id, agent or "", since)


def _waits_on_human(board: Board, thread_id: int) -> str | None:
    """Why this thread already waits on the human (a Needs you post, or a shared issue awaiting a decision), or None."""
    row = board.conn.execute(f"SELECT p.id FROM posts p WHERE p.thread_id = ? AND {Board.NEEDS_YOU} ORDER BY p.id LIMIT 1",
                             (thread_id,)).fetchone()
    if row is not None:
        return f"the thread is waiting on you (post #{row['id']}), so nothing was launched"
    row = board.conn.execute("""SELECT i.id FROM issue_links l JOIN issues i ON i.id = l.issue_id
                                WHERE l.thread_id = ? AND l.needs_human = 1 AND i.needs_human = 1 AND i.status = 'open'
                                ORDER BY i.id LIMIT 1""", (thread_id,)).fetchone()
    if row is not None:
        return f"the thread is waiting on your decision on issue #{row['id']}, so nothing was launched"
    return None


def _human_asked_first(board: Board, task_id: int, thread_id: int, owner_session: int | None, agent: str) -> bool:
    """The human asked this agent for work on this thread before the task's current claim: a `request` or `handoff`
    by the human identity, addressed to the agent, that is not an automatic one (Board.NOT_AUTOMATIC), created at or
    before the claim (or before the task, if no claim event is found). An agent cannot write such a post, so it
    cannot open a thread, claim, go silent and get launched; and a human post about something else, or to another
    agent, does not count."""
    claimed = board.conn.execute(
        """SELECT MAX(at) FROM task_events WHERE task_id = ? AND event IN ('claim', 'reclaim')
           AND (session_id = ? OR ? IS NULL)""", (task_id, owner_session, owner_session)).fetchone()[0]
    if claimed is None:
        claimed = board.conn.execute("SELECT created_at FROM tasks WHERE id = ?", (task_id,)).fetchone()[0]
    return board.conn.execute(
        f"""SELECT 1 FROM posts p JOIN agents a ON a.name = p.agent AND a.is_human = 1
            WHERE p.thread_id = ? AND p.created_at <= ? AND p.type IN ('request', 'handoff')
              AND EXISTS (SELECT 1 FROM json_each(p.to_agents) j WHERE j.value = ?)
              AND {Board.NOT_AUTOMATIC.format(post='p')} LIMIT 1""",
        (thread_id, claimed, agent)).fetchone() is not None


def _human_authorized(board: Board, task_id: int, agent: str) -> bool:
    """The human accepted this task, or an active standing grant covers it for this agent. A task its own creator
    accepted (require_human_accept off) is not enough to launch that creator automatically."""
    task = board._task_row(task_id)
    if task["authorization_source"] == "human":
        return True
    if task["authorization_source"] == "grant" and board._task_authorization_active(task, agent):
        return True
    return board._matching_grant(agent, task) is not None


# ---------------------------------------------------------------- one pass


def tick(board: Board, human: Principal, *, runner_for: Callable[[str], Any], fence: tuple[str, str],
         live_seconds: float = 120.0) -> dict:
    """One pass, called by the dispatcher loop that owns the board, after its own pause check. Prunes (hourly), checks
    earlier recoveries (settled, or escalated to the human), then sends new ones. Returns what it did (ids only)."""
    board._require_human(human, "run automatic recovery")
    if not enabled(board) or board.is_paused() or not _owner_ok(board.conn, fence):
        return {"sent": [], "escalated": []}
    try:
        prune(board)
    except Exception:
        log.exception("could not prune automatic recovery records")
    # Both kinds of launch (stall recoveries and recovery-wait relaunches) share one per-pass send cap.
    budget = {"escalations": MAX_ESCALATIONS_PER_PASS, "sends": MAX_SENDS_PER_PASS}
    escalated = _evaluate(board, human, fence, live_seconds, budget)
    sent = _detect_and_send(board, human, runner_for, fence, budget)
    waits = _process_waits(board, human, runner_for, fence, live_seconds, budget)
    return {"sent": sent, "escalated": escalated, "retried": waits["retried"], "wait_escalated": waits["escalated"]}


def _held_requests(conn, session_id: int | None, thread_id: int) -> list:
    if session_id is None:
        return []
    return list(conn.execute(
        """SELECT q.post_id, q.recipient FROM request_progress q JOIN posts p ON p.id = q.post_id
           WHERE q.assigned_session = ? AND p.thread_id = ? AND q.state != 'finished' ORDER BY q.post_id""",
        (session_id, thread_id)))


def _abandoned_item(board: Board, r, runner_for, now: float, at_cap) -> tuple[dict, str | None] | None:
    """(item, reason to go straight to the human or None) for one expired lease, or None when it is not an automatic
    recovery case (alive, already handled, out of scope)."""
    conn = board.conn
    session = conn.execute("SELECT id, last_seen, dispatch_run_id FROM sessions WHERE id = ?",
                           (r["owner_session"],)).fetchone() if r["owner_session"] is not None else None
    if r["lease_expires_at"] + recovery.abandon_grace(board, session) > now:
        return None     # inside the grace: an interactive session gets a full lease TTL, a dispatched one 10 minutes
    if session is not None and session["last_seen"] > r["lease_expires_at"]:
        return None     # seen since its lease expired: alive, not abandoned (the dashboard still shows the stall)
    if r["owner_session"] is not None and conn.execute(
            "SELECT 1 FROM tasks WHERE owner_session = ? AND lease_expires_at > ?", (r["owner_session"], now)).fetchone():
        return None     # it still holds a live lease elsewhere: active
    if awaiting.awaits(conn, r["id"]) or _continuing(conn, r["id"]):
        return None     # it waits on unfinished dependencies (awaiting, not stalled), or was just asked to continue
    key = task_key(r["id"], r["lease_expires_at"])
    if _get(conn, key) is not None or workstreams.get_for_task(board, r["id"]) is not None:
        return None     # already handled once, or a managed continuation with its own reconciliation
    if (runner_for(r["owner_agent"]) is None or at_cap(r["thread_id"])
            or _unstuck_since(conn, r["thread_id"], r["owner_agent"], r["lease_expires_at"])):
        return None
    held = _held_requests(conn, r["owner_session"], r["thread_id"])
    item = {"kind": "abandoned", "key": key, "task_id": r["id"], "thread_id": r["thread_id"],
            "agent": r["owner_agent"], "owner_session": r["owner_session"], "task_status": r["status"],
            "lease_expires_at": r["lease_expires_at"], "stalled_since": r["lease_expires_at"],
            "held_request_ids": sorted({h["post_id"] for h in held})}
    denied = next((h["post_id"] for h in held
                   if browser_readiness.request_blocker(board, h["post_id"], h["recipient"])), None)
    if denied is not None:
        item["browser_denied"] = True     # the escalation's question then recommends a release, not a relaunch
        return item, (f"request #{denied} is held by a browser policy denial; a human permission change is needed, "
                      "so nothing was launched")
    if r["status"] == "blocked":
        return item, "the task was blocked when its owner went silent and may be waiting on you, so nothing was launched"
    if _attempts(conn, r["id"]) >= MAX_PER_TASK:
        return item, f"the limit of {MAX_PER_TASK} automatic recoveries for this task is reached"
    if not (_human_authorized(board, r["id"], r["owner_agent"])
            or _human_asked_first(board, r["id"], r["thread_id"], r["owner_session"], r["owner_agent"])):
        return item, (f"neither you nor a standing grant authorized this work (no request of yours to {r['owner_agent']} "
                      "on the thread before the task was claimed), so nothing was launched")
    return item, _waits_on_human(board, r["thread_id"])


def _unclaimed_item(board: Board, r, runner_for, now: float, at_cap) -> tuple[dict, str | None] | None:
    conn = board.conn
    thread = conn.execute("SELECT status FROM threads WHERE id = ?", (r["thread_id"],)).fetchone()
    key = unclaimed_key(r["id"])
    if (thread is None or thread["status"] != "open" or _get(conn, key) is not None
            or workstreams.get_for_task(board, r["id"]) is not None or _continuing(conn, r["id"])):
        return None
    if (runner_for(r["created_by"]) is None or at_cap(r["thread_id"])
            or _unstuck_since(conn, r["thread_id"], r["created_by"], r["updated_at"])):
        return None
    if conn.execute("SELECT 1 FROM tasks WHERE thread_id = ? AND owner_agent = ? AND lease_expires_at > ?",
                    (r["thread_id"], r["created_by"], now)).fetchone():
        return None     # the creator is working another task here: a sequential backlog, not an orphan
    item = {"kind": "unclaimed", "key": key, "task_id": r["id"], "thread_id": r["thread_id"],
            "agent": r["created_by"], "unclaimed_since": r["updated_at"], "stalled_since": r["updated_at"]}
    if _attempts(conn, r["id"]) >= MAX_PER_TASK:
        return item, f"the limit of {MAX_PER_TASK} automatic recoveries for this task is reached"
    if not _human_authorized(board, r["id"], r["created_by"]):
        return item, ("its creator accepted it, not you or a standing grant, so nothing was launched; claim it, "
                      "decline it, or Unstick")
    return item, _waits_on_human(board, r["thread_id"])


def _continuing(conn, task_id: int) -> bool:
    """An automatic request to continue this task (its dependencies finished) is still pending."""
    return any(isinstance(rec, dict) and rec.get("state") == "sent"
               for rec in (_get(conn, key) for key, _ in _keys(conn, f"{CONTINUE_PREFIX}{int(task_id)}.").fetchall()))


def _satisfied_at(conn, deps: list[int]) -> float | None:
    """When the last of these dependencies became done or declined (task events, else the task's last change)."""
    latest = None
    for dep in deps:
        at = conn.execute("""SELECT MAX(at) FROM task_events WHERE task_id = ? AND to_status IN ('done', 'declined')""",
                          (dep,)).fetchone()[0]
        if at is None:
            row = conn.execute("SELECT updated_at FROM tasks WHERE id = ?", (dep,)).fetchone()
            at = row["updated_at"] if row else None
        if at is not None:
            latest = at if latest is None else max(latest, at)
    return latest


def _continue_item(board: Board, key: str, value: str, runner_for, now: float,
                   at_cap) -> tuple[dict, str | None] | None:
    """(item, reason to go straight to the human or None) for a task whose dependencies just finished, or None.
    Only a depends_on set while a dependency was unfinished counts (awaiting.STATE_PREFIX `waiting`), once per
    generation; the satisfaction must be recent (MAX_STALL_AGE_SECONDS)."""
    conn = board.conn
    try:
        rec = json.loads(value)
        task_id = int(key[len(awaiting.STATE_PREFIX):])
    except (TypeError, ValueError):
        return None
    if not isinstance(rec, dict) or not rec.get("waiting") or type(rec.get("generation")) is not int:
        return None
    task = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if task is None or task["status"] in TERMINAL:
        return None
    deps = awaiting.deps_of(task)
    if not awaiting.satisfied(conn, task_id, task):
        return None     # still waiting, or its dependencies were changed since
    ckey = continue_key(task_id, rec["generation"])
    if _get(conn, ckey) is not None:
        return None     # this satisfaction was already handled
    satisfied = _satisfied_at(conn, deps)
    if satisfied is None or satisfied < now - MAX_STALL_AGE_SECONDS:
        return None
    thread = conn.execute("SELECT status FROM threads WHERE id = ?", (task["thread_id"],)).fetchone()
    if thread is None or thread["status"] != "open" or workstreams.get_for_task(board, task_id) is not None:
        return None
    live_owner = (task["owner_agent"] is not None and task["lease_expires_at"] is not None
                  and task["lease_expires_at"] > now)
    agent = task["owner_agent"] if live_owner else task["created_by"]
    identity = conn.execute("SELECT active, is_human FROM agents WHERE name = ?", (agent,)).fetchone()
    if identity is None or not identity["active"] or identity["is_human"]:
        return None     # the human (or an inactive agent) continues it: nothing to launch
    if runner_for(agent) is None or at_cap(task["thread_id"]) or _unstuck_since(conn, task["thread_id"], agent,
                                                                                  satisfied):
        return None
    item = {"kind": "continue", "key": ckey, "task_id": task_id, "thread_id": task["thread_id"], "agent": agent,
            "owner_session": task["owner_session"], "task_status": task["status"],
            "lease_expires_at": task["lease_expires_at"], "generation": rec["generation"], "depends_on": deps,
            "satisfied_at": satisfied, "stalled_since": satisfied}
    if _attempts(conn, task_id) >= MAX_PER_TASK:
        return item, f"the limit of {MAX_PER_TASK} automatic recoveries for this task is reached"
    owner_session = task["owner_session"] if agent == task["owner_agent"] else None
    if not (_human_authorized(board, task_id, agent)
            or _human_asked_first(board, task_id, task["thread_id"], owner_session, agent)):
        return item, (f"neither you nor a standing grant authorized this work (no request of yours to {agent} on the "
                      "thread before the task was claimed), so nothing was launched")
    return item, _waits_on_human(board, task["thread_id"])


def _detect_and_send(board: Board, human: Principal, runner_for: Callable[[str], Any],
                     fence: tuple[str, str], budget: dict) -> list[int]:
    conn, now = board.conn, board.now()
    found: list[tuple[dict, str | None]] = []
    cap: dict[int, bool] = {}

    def at_cap(thread_id: int) -> bool:
        if thread_id not in cap:
            cap[thread_id] = _at_cap(board, thread_id)
        return cap[thread_id]

    # Abandoned work: a lease that expired within MAX_STALL_AGE_SECONDS, past the shortest grace.
    for r in conn.execute(
            """SELECT tk.id, tk.thread_id, tk.status, tk.owner_agent, tk.owner_session, tk.lease_expires_at
               FROM tasks tk JOIN threads t ON t.id = tk.thread_id AND t.status = 'open'
               JOIN agents a ON a.name = tk.owner_agent AND a.active = 1 AND a.is_human = 0
               WHERE tk.status IN ('working', 'blocked') AND tk.lease_expires_at IS NOT NULL
                 AND tk.lease_expires_at >= ? AND tk.lease_expires_at <= ? ORDER BY tk.lease_expires_at, tk.id""",
            (now - MAX_STALL_AGE_SECONDS, now - GRACE_SECONDS)).fetchall():
        try:
            out = _abandoned_item(board, r, runner_for, now, at_cap)
        except Exception:
            log.exception("automatic recovery skipped task %s", r["id"])
            continue
        if out is not None:
            found.append(out)
    # Unclaimed tasks: accepted, unowned and unchanged for the dashboard's stall window, created by an agent.
    for r in unstick.unclaimed_tasks(board, updated_before=now - UNCLAIMED_AFTER_SECONDS):
        if r["updated_at"] < now - MAX_STALL_AGE_SECONDS:
            continue
        try:
            out = _unclaimed_item(board, r, runner_for, now, at_cap)
        except Exception:
            log.exception("automatic recovery skipped task %s", r["id"])
            continue
        if out is not None:
            found.append(out)
    # Continuations: tasks set to wait on other work whose last dependency is now done or declined.
    for key, value in _keys(conn, awaiting.STATE_PREFIX).fetchall():
        try:
            out = _continue_item(board, key, value, runner_for, now, at_cap)
        except Exception:
            log.exception("automatic continuation skipped %s", key)
            continue
        if out is not None:
            found.append(out)

    # One item per task: a continuation (its dependencies just finished) is the more precise event, so it wins over
    # an expired lease or an unclaimed task found for the same task in this pass.
    continuing = {item["task_id"] for item, _ in found if item["kind"] == "continue"}
    found = [(item, reason) for item, reason in found if item["kind"] == "continue" or item["task_id"] not in continuing]
    groups: dict[tuple[int, str], list[dict]] = {}
    direct: list[tuple[str, dict]] = []
    for item, reason in found:
        if reason is None:
            # A continuation is its own post (kind "continue"), never grouped with stall recoveries: only a stall
            # request can be answered with a prevention proposal (prevention._is_stall_request).
            groups.setdefault((item["thread_id"], item["agent"], item["kind"] == "continue"), []).append(item)
        else:
            direct.append((reason, item))
    sent: list[int] = []
    for (thread_id, agent, _), items in sorted(groups.items(), key=lambda g: min(x["stalled_since"] for x in g[1])):
        spent = _budget_reason(conn, agent, thread_id, board.now())
        if spent:
            direct += [(spent, x) for x in items]
            continue
        if budget.get("sends", MAX_SENDS_PER_PASS) <= 0:
            continue    # the next pass picks it up
        try:
            sent.append(_send(board, human, fence, thread_id, agent, items))
            budget["sends"] = budget.get("sends", MAX_SENDS_PER_PASS) - 1
        except Exception:
            log.exception("automatic recovery for %s on thread %s failed", agent, thread_id)
    by_thread: dict[int, list[tuple[str, dict]]] = {}
    for reason, item in direct:
        by_thread.setdefault(item["thread_id"], []).append((reason, item))
    for thread_id, items in by_thread.items():
        if budget["escalations"] <= 0:
            break
        try:
            if _escalate_new(board, human, fence, thread_id, items) is not None:
                budget["escalations"] -= 1
        except Exception:
            log.exception("automatic recovery escalation on thread %s failed", thread_id)
    return sent


def _describe(item: dict) -> str:
    if item["kind"] == "continue":
        return f"task {item['task_id']}'s dependencies are finished; continue it"
    if item["kind"] == "abandoned":
        text = (f"task {item['task_id']} was abandoned: its lease expired and owner session "
                f"{item['owner_session']} has not been seen since")
        if item.get("held_request_ids"):
            ids = item["held_request_ids"]
            text += f"; that session still holds request{'s' if len(ids) > 1 else ''} {_ids(ids)}"
        return text
    return (f"task {item['task_id']} is accepted but unclaimed (created by {item['agent']}): claim it or decline it "
            "if finished work already covers it")


def build_body(items: list[dict], prevention: str | None = None) -> str:
    """The fixed request text. Only ids and agent names (server-stamped) vary; nothing an agent wrote. `prevention`:
    the prevention inbox's routing text (prevention.instructions), asked for stalls only."""
    stalls = [x for x in items if x["kind"] != "continue"]
    continues = [x for x in items if x["kind"] == "continue"]
    out = [HEADER]
    if continues:
        parts = [_describe(x) for x in continues][:MAX_ITEMS]
        if len(continues) > MAX_ITEMS:
            parts.append(f"{len(continues) - MAX_ITEMS} more")
        text = "; ".join(parts)
        out.append(text[0].upper() + text[1:] + ". " + CONTINUE_INSTRUCTIONS)
    if stalls:
        parts = [_describe(x) for x in stalls]
        if len(parts) > MAX_ITEMS:
            parts = parts[:MAX_ITEMS] + [f"{len(parts) - MAX_ITEMS} more"]
        out.append(f"This thread is stalled on you ({'; '.join(parts)}). {INSTRUCTIONS}")
        if prevention:
            out.append(PREVENTION_INSTRUCTIONS.format(prevention=prevention))
    return " ".join(out)


def _guard(board: Board, fence: tuple[str, str], items: list[dict], agent: str, thread_id: int) -> Callable[[], None]:
    def check() -> None:
        # Inside the post's write transaction: the loop still owns the board, nothing paused or switched off since
        # the scan, no other pass recorded these keys, the human has not clicked Unstick on the stall meanwhile, and
        # the agent's launch budget on this thread is not spent.
        conn = board.conn
        if not _owner_ok(conn, fence):
            raise Conflict("another dispatcher owns this board now")
        if board.is_paused() or not enabled(board):
            raise Conflict("automatic recovery is paused or switched off")
        for x in items:
            if conn.execute("SELECT 1 FROM board_state WHERE key = ?", (x["key"],)).fetchone():
                raise Conflict(f"{x['key']} was already recovered")
            if _unstuck_since(conn, thread_id, agent, x["stalled_since"]):
                raise Conflict(f"the human unstuck thread {thread_id} meanwhile")
            if x["kind"] == "continue":
                # Its dependencies must still be the finished ones it was found with (same depends_on generation).
                current = awaiting.state(conn, x["task_id"]) or {}
                if current.get("generation") != x["generation"] or not awaiting.satisfied(conn, x["task_id"]):
                    raise Conflict(f"task {x['task_id']}'s dependencies changed meanwhile")
        if _budget_reason(conn, agent, thread_id, board.now()):
            raise Conflict(f"the automatic launch budget for {agent} is spent")
    return check


def _send(board: Board, human: Principal, fence: tuple[str, str], thread_id: int, agent: str,
          items: list[dict]) -> int:
    now = board.now()

    def record(post: dict) -> None:
        conn = board.conn
        kind = "continue" if all(x["kind"] == "continue" for x in items) else "recovery"
        _insert(conn, POST_PREFIX + str(post["id"]), {"kind": kind, "thread_id": thread_id,
                                                     "agent": agent, "task_ids": [x["task_id"] for x in items]}, now)
        rule_id = human_actions.post_rule_id(board, post["id"])   # bound to the post just before this hook
        for x in items:
            rec = {k: v for k, v in x.items() if k != "key"}
            rec.update(post_id=post["id"], rule_id=rule_id, sent_at=now, state="sent")
            _insert(conn, x["key"], rec, now)
        _upsert(conn, budget_key(agent, thread_id), _budget_used(conn, agent, thread_id, now) + [now], now)
        _upsert(conn, agent_budget_key(agent), _times(conn, agent_budget_key(agent), now) + [now], now)
        for x in items:
            _upsert(conn, attempts_key(x["task_id"]), _attempts(conn, x["task_id"]) + 1, now)

    from . import prevention
    inbox = prevention.active(board) if any(x["kind"] != "continue" for x in items) else None
    body = build_body(items, prevention.instructions(board, inbox, thread_id) if inbox else None)
    purpose = (CONTINUE_PURPOSE if all(x["kind"] == "continue" for x in items) else PURPOSE).format(thread=thread_id)
    post, _ = human_actions.post_as_human(
        board, human, thread_id=thread_id, body=body, type="request", to=[agent], needs_response=True,
        launch=[agent], purpose=purpose, post_check=_guard(board, fence, items, agent, thread_id),
        post_hook=record)
    log.info("automatic recovery: asked %s on thread %s about tasks %s (post %s)", agent, thread_id,
             [x["task_id"] for x in items], post["id"])
    return post["id"]


# ---------------------------------------------------------------- did it take?


def _settled(rec: dict, task) -> bool:
    """The task moved on since the recovery was recorded: reclaimed or renewed, released, finished or declined
    (abandoned work); claimed, finished or declined (an unclaimed task)."""
    if task is None:
        return True
    if rec["kind"] == "continue":
        return (task["status"] in TERMINAL or task["status"] != rec.get("task_status")
                or task["lease_expires_at"] != rec.get("lease_expires_at")
                or task["owner_session"] != rec.get("owner_session"))
    if rec["kind"] == "abandoned":
        return (task["status"] not in ("working", "blocked") or task["lease_expires_at"] != rec.get("lease_expires_at")
                or task["owner_session"] != rec.get("owner_session"))
    return task["status"] != "accepted" or task["owner_agent"] is not None


def _superseded_by_continue(conn, rec: dict, task) -> bool:
    """The task's dependencies, set after this stall record, have all finished: the automatic request to continue
    it (a "continue" record) takes over, so the older stall record is settled rather than escalated again."""
    if rec.get("kind") == "continue" or task is None or not awaiting.satisfied(conn, task["id"], task):
        return False
    st = awaiting.state(conn, task["id"]) or {}
    at = st.get("set_at")
    return type(at) in (int, float) and at >= (rec.get("escalated_at") or rec.get("sent_at") or 0)


def _launched_at(board: Board, post_id: int, agent: str) -> float | None:
    latest = None
    for (value,) in board.conn.execute("SELECT value FROM board_state WHERE key >= 'dispatch.run.' AND key < 'dispatch.run/'"):
        try:
            run = json.loads(value)
        except (TypeError, ValueError):
            continue
        if (isinstance(run, dict) and run.get("agent") == agent and post_id in (run.get("request_ids") or [])
                and run.get("pid") is not None and isinstance(run.get("started_at"), (int, float))):
            latest = max(latest or run["started_at"], run["started_at"])
    return latest


def _queued(board: Board, rec: dict, live_seconds: float) -> bool:
    """The launch for this recovery is still waiting in the dispatcher (behind max_concurrent or one run per agent)
    and no interactive session of the agent is live to see the request meanwhile: its clock has not started."""
    from .dispatch import Dispatcher
    pending = _get(board.conn, Dispatcher.PENDING_KEY)
    if not isinstance(pending, dict) or f"{rec['agent']}:{rec['thread_id']}:{rec['post_id']}" not in pending:
        return False
    return board.conn.execute("""SELECT 1 FROM sessions WHERE agent = ? AND dispatch_run_id IS NULL AND last_seen >= ?""",
                              (rec["agent"], board.now() - live_seconds)).fetchone() is None


def _request_row(board: Board, post_id: int, agent: str) -> dict | None:
    post = board.conn.execute("SELECT * FROM posts WHERE id = ?", (post_id,)).fetchone()
    if post is None:
        return None
    return next((r for r in requests.for_post(board, post) if r["recipient"] == agent), None)


def _evaluate(board: Board, human: Principal, fence: tuple[str, str], live_seconds: float, budget: dict) -> list[str]:
    conn, now = board.conn, board.now()
    ttl = board.s.lease_ttl_minutes * 60
    settled: list[tuple[str, dict]] = []
    failing: dict[int, list[tuple[str, dict, str, str | None]]] = {}
    for key, rec in _records(conn):
        if rec.get("state") not in ("sent", "escalated", "suppressed"):
            continue
        try:
            task = conn.execute("SELECT * FROM tasks WHERE id = ?", (rec["task_id"],)).fetchone()
            if awaiting.awaits(conn, rec["task_id"]):
                continue    # awaiting other work: neither settled nor escalated; it resumes if the wait is dropped
            if _settled(rec, task) or _superseded_by_continue(conn, rec, task):
                settled.append((key, rec | {"state": "recovered" if rec["state"] == "sent" else "resolved",
                                            "resolved_at": now}))
                continue
            if rec["state"] != "sent":
                continue
            thread = conn.execute("SELECT status FROM threads WHERE id = ?", (rec["thread_id"],)).fetchone()
            if thread is None or thread["status"] != "open":
                continue    # nothing is launched on a closed thread; reopening it resumes the check
            post_id = rec.get("post_id")
            row = _request_row(board, post_id, rec["agent"]) if isinstance(post_id, int) else None
            reason = detail = None
            if row is not None and row["state"] == "blocked":
                reason = f"its recovery request #{post_id} to {rec['agent']} is blocked"
                detail = (row.get("reason") or "")[:300] or None
            else:
                launched = _launched_at(board, post_id, rec["agent"]) if isinstance(post_id, int) else None
                if launched is not None:
                    start = max(launched, rec.get("sent_at") or 0)
                elif isinstance(post_id, int) and _queued(board, rec, live_seconds):
                    start = None    # the launch has not happened yet: the clock starts when it does
                else:
                    start = rec.get("sent_at") or 0
                if start is not None and now >= start + ttl:
                    minutes = board.s.lease_ttl_minutes
                    reason = (f"the task was not reclaimed or settled within {minutes} minutes of the automatic "
                              "recovery" if rec["kind"] == "abandoned" else
                              f"the task was not claimed or continued within {minutes} minutes of the automatic "
                              "request to continue it" if rec["kind"] == "continue" else
                              f"the task was not claimed or declined within {minutes} minutes of the automatic recovery")
            if reason:
                failing.setdefault(rec["thread_id"], []).append((key, rec, reason, detail))
        except Exception:
            log.exception("could not check automatic recovery %s", key)
    if settled:
        with db.write_tx(conn) as c:
            for key, rec in settled:
                _update(c, key, rec, now)
    escalated = []
    for thread_id, items in failing.items():
        if budget["escalations"] <= 0:
            break
        try:
            if _escalate(board, human, fence, thread_id, items) is not None:
                budget["escalations"] -= 1
            escalated += [key for key, *_ in items]
        except Exception:
            log.exception("automatic recovery escalation on thread %s failed", thread_id)
            continue
        # The escalation says no further launch will be made: revoke the one-shot rules, so a launch still waiting
        # behind max_concurrent or one run per agent is dropped too.
        for rule_id in {rec.get("rule_id") for _, rec, *_ in items}:
            if isinstance(rule_id, int) and not isinstance(rule_id, bool):
                try:
                    board.revoke_dispatch_rule(human, rule_id)
                except Exception:
                    log.exception("could not revoke automatic recovery rule %s", rule_id)
    return escalated


# ---------------------------------------------------------------- escalation to the human


def _escalation_body(entries: list[tuple[dict, str]], structured: bool = True) -> str:
    parts = []
    for rec, reason in entries[:MAX_ITEMS]:
        what = {"abandoned": "abandoned", "continue": "dependencies finished"}.get(rec["kind"], "unclaimed")
        parts.append(f"task {rec['task_id']} ({what}, {rec['agent']}): {reason}")
    if len(entries) > MAX_ITEMS:
        parts.append(f"{len(entries) - MAX_ITEMS} more")
    them = "this stall" if len(entries) == 1 else "these stalls"
    return ESCALATION.format(items="; ".join(parts), them=them,
                             next=ESCALATION_NEXT if structured else ESCALATION_NEXT_PLAIN)


def _option(id_: str, label: str, description: str, action: dict | None = None) -> dict:
    out = {"id": id_, "label": label, "description": description, "outcome": "approved" if action else "answered"}
    if action:
        out["action"] = action
    return out


def _unstick_option(id_: str, label: str, thread_id: int, agents: list[str], what: str) -> dict:
    return _option(id_, label, (
        f"Runs Unstick on thread #{thread_id}, as if you clicked it, for {', '.join(agents)} only: posts a request as you to "
        f"whichever of them the thread still waits on to {what}, and allows one launch of each. Costs an agent run "
        "(tokens); the server refuses it if the thread no longer waits on them or was unstuck in the last two minutes."),
        {"type": "unstick", "thread_id": thread_id, "agents": agents})


def escalation_question(board: Board, thread_id: int, entries: list[tuple[dict, str]]) -> dict:
    """The structured question on an escalation post, so the human answers it in one click (Recommended, Alternative,
    or their own reply). Built only from server-side facts: thread, task, post and session ids, agent names, the
    task's current status and the server's own reason text; never anything an agent wrote (the recorded request
    reason, `detail`, is left out). Each option either runs a bounded decision action (decision_actions.py: unstick,
    decline_task, release_task, rechecked against the current state when chosen) or only answers.

    One question per post. Several stalls on one thread (a grouped escalation) get one Unstick for the agents the post
    names, which asks each of them about all of its stalled tasks on the thread at once, rather than an action on the
    first task alone, which would leave the others hidden behind an answered item."""
    if len(entries) > 1:
        ids = [rec["task_id"] for rec, _ in entries]
        agents = list(dict.fromkeys(rec["agent"] for rec, _ in entries))
        shown = ", ".join(str(i) for i in ids[:MAX_ITEMS]) + (f" and {len(ids) - MAX_ITEMS} more" if len(ids) > MAX_ITEMS else "")
        return {
            "question": f"Automatic recovery did not take for {len(ids)} tasks on thread #{thread_id} (tasks {shown}). "
                        "Unstick the thread?",
            "context": "No further automatic launches will be made for them. Each task's reason is listed in the post.",
            "options": [
                _unstick_option("unstick", f"Unstick thread #{thread_id} (asks {', '.join(agents)})"[:200], thread_id,
                                agents[:20], "reclaim, finish, release or decline each stalled task"),
                _option("leave", "Leave them for now", (
                    "Launches nothing and takes this item out of Needs you. Costs nothing now; the tasks stay stalled "
                    "until you act on the thread (Unstick, or decline or release a task there)."))],
            "recommended_option_id": "unstick"}
    rec, reason = entries[0]
    task_id, agent = rec["task_id"], rec["agent"]
    task = board.conn.execute("SELECT status, owner_session, owner_agent FROM tasks WHERE id = ?", (task_id,)).fetchone()
    status = task["status"] if task else rec.get("task_status")
    kind = rec["kind"]
    if kind == "continue":
        # A task whose dependencies finished: unowned, it is asked of its creator like an unclaimed task; owned, it
        # is its owner's to continue like abandoned work.
        kind = "unclaimed" if task is None or task["owner_agent"] is None else "abandoned"
    context = (f"Thread #{thread_id}, task {task_id} ({status or 'unknown status'}), "
               f"{'created by' if kind == 'unclaimed' else 'owned by'} {agent}. Why it came to you: {reason}. "
               "No further automatic launches will be made for it.")
    if kind == "unclaimed":
        # Recommend asking the creator, never declining: an unclaimed task is often real unfinished work (live post
        # #592 recommended declining an unfinished audit continuation). The creator checks the finished work and
        # declines the task only with evidence that it is covered; declining blind would drop work.
        options = [_option("ask-creator", f"Ask {agent} to claim it or decline it if covered", (
                       f"Runs Unstick on thread #{thread_id}, as if you clicked it, for {agent} only: posts a request "
                       f"as you asking {agent} to check task {task_id} against the finished work and decide with "
                       "evidence: claim it and finish it if work remains, or decline it citing the work that already "
                       f"covers it. Allows one launch of {agent}. Costs an agent run (tokens); the server refuses it "
                       "if the thread no longer waits on them or was unstuck in the last two minutes."),
                       {"type": "unstick", "thread_id": thread_id, "agents": [agent]}),
                   _option("decline", f"Decline task {task_id}", (
                       f"Marks task {task_id} declined now, without anyone checking it, so the thread no longer waits "
                       "on it. Costs nothing to run, but if work remains it is dropped and has to be proposed again."),
                       {"type": "decline_task", "task_id": task_id,
                        "expected_status": status if status in ("proposed", "accepted") else "accepted"})]
        lead = (f"Task {task_id}'s dependencies are finished but nobody continued it"
                if rec["kind"] == "continue" else f"Task {task_id} is accepted but nobody claimed it")
        return {"question": f"{lead}, and automatic recovery did not take. "
                            f"Ask {agent} to claim it or decline it if finished work covers it?",
                "context": context, "options": options, "recommended_option_id": "ask-creator"}
    if status == "blocked" and not rec.get("browser_denied"):
        return {"question": f"Task {task_id} is blocked and its owner {agent} went silent. Relaunch {agent} to report "
                            "what it needs?",
                "context": context,
                "options": [_unstick_option("relaunch", f"Relaunch {agent} to report what it needs", thread_id, [agent],
                                            f"find out why task {task_id} is blocked and say exactly what it needs "
                                            "and from whom"),
                            _option("keep-blocked", "Keep it blocked", (
                                f"Leaves task {task_id} blocked, launches nothing, and takes this item out of Needs "
                                f"you. Costs nothing now; the task stays stalled until you or {agent} act on it."))],
                "recommended_option_id": "relaunch"}
    session = task["owner_session"] if task else rec.get("owner_session")
    if isinstance(session, int) and not isinstance(session, bool) and session > 0:
        release = _option("release", f"Release task {task_id}", (
            f"Clears the lease of {agent}'s silent session {session} and returns task {task_id} to accepted, "
            "unowned, so any agent can claim it. Launches nothing; requests that session still holds stay as they are "
            "until an agent takes them over. Refused if the task changed hands since."),
            {"type": "release_task", "task_id": task_id, "expected_owner_session": session})
    else:
        release = _option("leave", "Leave it for now", (
            f"Launches nothing and takes this item out of Needs you; task {task_id} stays stalled until you act on it."))
    denied = bool(rec.get("browser_denied"))
    relaunch = _unstick_option("relaunch", f"Relaunch {agent} (Unstick)", thread_id, [agent],
                               f"reclaim task {task_id} and finish or release it"
                               + ("; change the browser permission first, or it stops at the same denial" if denied else ""))
    lead = (f"Task {task_id}'s dependencies are finished but {agent} did not continue it" if rec["kind"] == "continue"
            else f"Task {task_id} was abandoned by {agent}")
    return {"question": f"{lead} and automatic recovery did not take. "
                        + (f"Release it, or relaunch {agent}?" if denied else f"Relaunch {agent}, or release the task?"),
            "context": context, "options": [relaunch, release],
            "recommended_option_id": release["id"] if denied else "relaunch"}


def _escalations_left(conn, agent: str, now: float) -> bool:
    return len(_times(conn, escalations_key(agent), now)) < ESCALATION_DAILY_CAP


def _post_escalation(board: Board, human: Principal, fence: tuple[str, str], thread_id: int,
                     entries: list[tuple[str, dict, str, str | None]], new: bool) -> int | None:
    """One needs-response post to nobody (Needs you) for these records, and the records marked escalated, in one
    transaction. `new` records (escalated without a launch) are inserted; others must still be `sent`.

    Posts about one responsible agent's stalls are capped (ESCALATION_DAILY_CAP per rolling day), so an agent that
    opens many threads and abandons a self-claimed task in each cannot flood the human's Needs you. Entries over the
    cap are recorded `suppressed` without a post (and without a launch); the dashboard still shows those stalls with
    their ordinary labels. Returns the post id, or None when nothing was posted."""
    now = board.now()
    allowed = [e for e in entries if _escalations_left(board.conn, e[1]["agent"], now)]
    quiet = [e for e in entries if e not in allowed]
    if quiet:
        with db.write_tx(board.conn) as c:
            for key, rec, reason, detail in quiet:
                current = _get(c, key)
                if (current is not None) if new else (not isinstance(current, dict) or current.get("state") != "sent"):
                    continue
                out = {k: v for k, v in rec.items() if k != "key"}
                out.update(state="suppressed", reason=reason, detail=detail, escalated_at=now)
                if new:
                    out.setdefault("sent_at", None)
                    out.setdefault("post_id", None)
                    _insert(c, key, out, now)
                else:
                    _update(c, key, out, now)
        log.warning("automatic recovery on thread %s: tasks %s need the human, but the daily cap of escalation posts "
                    "for %s is reached; not posting", thread_id, [rec["task_id"] for _, rec, *_ in quiet],
                    sorted({rec["agent"] for _, rec, *_ in quiet}))
    entries = allowed
    if not entries:
        return None
    keys = [key for key, *_ in entries]
    agents = sorted({rec["agent"] for _, rec, *_ in entries})

    def check() -> None:
        if not _owner_ok(board.conn, fence):
            raise Conflict("another dispatcher owns this board now")
        if board.is_paused() or not enabled(board):
            raise Conflict("automatic recovery is paused or switched off")
        for key in keys:
            current = _get(board.conn, key)
            if (current is not None) if new else (not isinstance(current, dict) or current.get("state") != "sent"):
                raise Conflict(f"{key} changed meanwhile")
        for agent in agents:
            if not _escalations_left(board.conn, agent, board.now()):
                raise Conflict(f"the daily cap of escalation posts for {agent} is reached")

    def record(post: dict) -> None:
        _insert(board.conn, POST_PREFIX + str(post["id"]), {"kind": "escalation", "thread_id": thread_id,
                                                           "task_ids": [rec["task_id"] for _, rec, *_ in entries]}, now)
        for agent in agents:
            _upsert(board.conn, escalations_key(agent), _times(board.conn, escalations_key(agent), now) + [now], now)
        for key, rec, reason, detail in entries:
            out = {k: v for k, v in rec.items() if k != "key"}
            out.update(state="escalated", reason=reason, detail=detail, escalated_at=now,
                       escalation_post_id=post["id"])
            if new:
                out.setdefault("sent_at", None)
                out.setdefault("post_id", None)
                _insert(board.conn, key, out, now)
            else:
                _update(board.conn, key, out, now)

    pairs = [(rec, reason) for _, rec, reason, _ in entries]
    try:
        question = escalation_question(board, thread_id, pairs)
        issues._question(board, question)       # validated now, so a bad question cannot block the escalation itself
    except Exception:
        log.exception("could not build the question for the escalation on thread %s; posting it without one",
                      thread_id)
        question = None
    post, _ = human_actions.post_as_human(
        board, human, thread_id=thread_id, body=_escalation_body(pairs, question is not None),
        type="question" if question else "status",
        to=[], needs_response=True, decision_question=question, post_check=check, post_hook=record)
    log.warning("automatic recovery did not take on thread %s (tasks %s); told the human in post %s", thread_id,
                [rec["task_id"] for _, rec, *_ in entries], post["id"])
    return post["id"]


def _escalate(board, human, fence, thread_id, items) -> int | None:
    return _post_escalation(board, human, fence, thread_id, items, new=False)


def _escalate_new(board, human, fence, thread_id, items: list[tuple[str, dict]]) -> int | None:
    entries = [(item["key"], item, reason, None) for reason, item in items]
    return _post_escalation(board, human, fence, thread_id, entries, new=True)


# ---------------------------------------------------------------- recovery waits (transient ownership blockers)
#
# board_recover_request_owner records a wait (recovery.WAIT_PREFIX) when ownership recovery hit a transient blocker:
# another session or dispatcher run still busy in the owner's checkout. Verifying an owner and handing work over within
# the existing scope is routine (the human, 2026-10-09: "this should always be 'yes'"), so the agent stops and the
# dispatcher relaunches it once the worktree is free, at most recovery.WAIT_RETRIES times per request per rolling day.
# Only then, or after MAX_WAIT_SECONDS, does it ask the human.

MAX_WAIT_SECONDS = 24 * 3600
WAIT_BODY = (HEADER + " The owner worktree of request #{post} (recipient {recipient}) is free now: claim (or reclaim) "
             "the request's task first if it has one with board_claim_task, then recover the request from session "
             "{old} with board_recover_request_owner (reread its version first) and resume the work it "
             "asked for.{also} Automatic retry {n} of {max}. Verifying the owner and taking over ownership within the "
             "request's existing scope is routine and pre-authorized: do not ask the human about it. If recovery is "
             "blocked again for a transient reason, mark this request blocked with the returned reason and stop; the "
             "board retries. Stay within what this thread already asked for, and reply to this request "
             "(request_reply) when you are done or blocked.")
WAIT_PURPOSE = ("Automatic recovery on thread {thread} (the human's board setting auto_recover_stalled_work): the owner "
                "worktree is free; recover request #{post} and resume it; stay within the thread's existing request.")
WAIT_ESCALATION = ("Automatic recovery did not take (sent by the dispatcher under the human's board setting "
                   "auto_recover_stalled_work; not a human click): request #{post} ({agent}) is still held by session "
                   "{old}: {reason}. No further automatic retries will be made for it. This needs you: {next}")
WAIT_ESCALATION_NEXT = "pick one of the options below, or open the thread and check the named request."
WAIT_ESCALATION_NEXT_PLAIN = "open the thread and check the named request, then Unstick it or reassign the request."


def _wait_authorized(board: Board, human: Principal, wait: dict) -> bool:
    """The human asked for this request: they wrote it (by hand: a post the dispatcher made as the human, marked
    automatic, does not count, as in Board.NOT_AUTOMATIC), its task is human- or grant-authorized, or a dispatch
    approval of theirs (not a one-click one) covers this agent on the thread. Otherwise no automatic launch."""
    post = board.conn.execute(f"""SELECT p.task_id, a.is_human, {Board.NOT_AUTOMATIC.format(post='p')} AS by_hand
                                  FROM posts p JOIN agents a ON a.name = p.agent WHERE p.id = ?""",
                              (wait["post_id"],)).fetchone()
    if post is None:
        return False
    if post["is_human"] and post["by_hand"]:
        return True
    if post["task_id"] is not None and _human_authorized(board, post["task_id"], wait["agent"]):
        return True
    one_click = human_actions.one_click_rule_ids(board)
    return any(r["thread_id"] == wait["thread_id"] and wait["agent"] in r["agents"] and r["id"] not in one_click
               and r["state"] in ("active", "exhausted")
               for r in board.list_dispatch_rules(human, include_inactive=True))


def _relaunch_in_flight(board: Board, wait: dict, live_seconds: float) -> bool:
    """The last automatic relaunch is still under way: its launch waits in the dispatcher, or its request is not
    blocked or finished and one lease TTL has not passed since the launch (or the post)."""
    post_id, sent = wait.get("relaunch_post_id"), wait.get("relaunched_at")
    if type(post_id) is not int or type(sent) not in (int, float):
        return False
    row = _request_row(board, post_id, wait["agent"])
    if row is None or row["state"] in ("blocked", "finished"):
        return False
    launched = _launched_at(board, post_id, wait["agent"])
    if launched is None and _queued(board, {"agent": wait["agent"], "thread_id": wait["thread_id"],
                                            "post_id": post_id}, live_seconds):
        return True
    return board.now() < max(launched or 0, sent) + board.s.lease_ttl_minutes * 60


def _wait_action(board: Board, human: Principal, runner_for, wait: dict, now: float, live_seconds: float,
                 at_cap) -> tuple[str, Any] | None:
    """What to do with one live wait now: ("settle", (state, why)), ("update", fields), ("send", None),
    ("escalate", reason), or None (keep waiting)."""
    conn, agent, thread_id = board.conn, wait["agent"], wait["thread_id"]
    if not recovery.wait_live(board, wait):
        return "settle", ("resolved", "the request was recovered, reassigned or finished")
    thread = conn.execute("SELECT status FROM threads WHERE id = ?", (thread_id,)).fetchone()
    if thread is None or thread["status"] != "open":
        return None     # nothing is launched on a closed thread; reopening it resumes the wait
    if _unstuck_since(conn, thread_id, agent, wait.get("recorded_at") or 0):
        return "settle", ("resolved", "the human unstuck the thread")
    if wait["state"] == "relaunched" and _relaunch_in_flight(board, wait, live_seconds):
        return None
    blocker = recovery.transient_blocker(board, wait)
    first = wait.get("first_recorded_at")
    if type(first) in (int, float) and now - first >= MAX_WAIT_SECONDS:
        return "escalate", ("the owner worktree did not become free within 24 hours" + (f" ({blocker})" if blocker else
                            ", or the agent could not be relaunched in that time"))
    if blocker is not None:
        if wait["state"] != "waiting" or wait.get("blocker") != blocker:
            return "update", {"state": "waiting", "blocker": blocker}
        return None
    if len(recovery.retries_used(wait, now)) >= recovery.WAIT_RETRIES:
        return "escalate", (f"{recovery.WAIT_RETRIES} automatic retries in 24 hours did not recover it, although the "
                            "owner worktree was free")
    identity = conn.execute("SELECT active, is_human FROM agents WHERE name = ?", (agent,)).fetchone()
    if identity is None or not identity["active"] or identity["is_human"]:
        return None
    if runner_for(agent) is None:
        return "escalate", f"no runner is configured for {agent}, so the dispatcher cannot relaunch it"
    if not _wait_authorized(board, human, wait):
        return "escalate", (f"neither you, a standing grant nor a dispatch approval of yours asked {agent} for request "
                            f"#{wait['post_id']}, so nothing was launched")
    if at_cap(thread_id) or len(_times(conn, agent_budget_key(agent), now)) >= AGENT_DAILY_BUDGET:
        return None     # the thread needs a human post first, or the agent's daily launch budget frees up later
    return "send", None


def _process_waits(board: Board, human: Principal, runner_for: Callable[[str], Any], fence: tuple[str, str],
                   live_seconds: float, budget: dict) -> dict:
    """One pass over the recorded recovery waits: settle the ones that moved on, relaunch an agent whose owner worktree
    is free (bounded per pass and per request), escalate the ones whose retries ran out."""
    conn = board.conn
    out: dict[str, list] = {"retried": [], "escalated": []}
    cap: dict[int, bool] = {}

    def at_cap(thread_id: int) -> bool:
        if thread_id not in cap:
            cap[thread_id] = _at_cap(board, thread_id)
        return cap[thread_id]

    for key, wait in recovery.waits(conn):
        if wait.get("state") not in recovery.WAITING:
            continue
        now = board.now()
        try:
            action = _wait_action(board, human, runner_for, wait, now, live_seconds, at_cap)
            if action is None:
                continue
            kind, arg = action
            if kind in ("settle", "update"):
                fields = ({"state": arg[0], "settled_at": now, "settled_reason": arg[1]} if kind == "settle" else arg)
                with db.write_tx(conn) as c:
                    if _get(c, key) == wait:
                        _update(c, key, wait | fields, now)
            elif kind == "send" and budget.get("sends", MAX_SENDS_PER_PASS) > 0:
                out["retried"].append(_send_wait_retry(board, human, fence, key, wait))
                budget["sends"] = budget.get("sends", MAX_SENDS_PER_PASS) - 1
            elif kind == "escalate" and budget["escalations"] > 0:
                if _escalate_wait(board, human, fence, key, wait, arg) is not None:
                    budget["escalations"] -= 1
                    out["escalated"].append(key)
        except Exception:
            log.exception("could not handle recovery wait %s", key)
    return out


def _send_wait_retry(board: Board, human: Principal, fence: tuple[str, str], key: str, wait: dict) -> int:
    """Relaunch the agent once with the fixed automatic-recovery request (ids and names only), under a fresh one-shot
    rule, and record the retry in the same transaction."""
    now = board.now()
    agent, thread_id, post_id = wait["agent"], wait["thread_id"], wait["post_id"]
    used = recovery.retries_used(wait, now)

    def check() -> None:
        conn = board.conn
        if not _owner_ok(conn, fence):
            raise Conflict("another dispatcher owns this board now")
        if board.is_paused() or not enabled(board):
            raise Conflict("automatic recovery is paused or switched off")
        if _get(conn, key) != wait:
            raise Conflict(f"{key} changed meanwhile")
        if len(recovery.retries_used(wait, board.now())) >= recovery.WAIT_RETRIES:
            raise Conflict(f"the automatic retries for request #{post_id} are spent")
        if len(_times(conn, agent_budget_key(agent), board.now())) >= AGENT_DAILY_BUDGET:
            raise Conflict(f"the daily automatic launch budget for {agent} is spent")

    def record(post: dict) -> None:
        conn = board.conn
        _insert(conn, POST_PREFIX + str(post["id"]), {"kind": "recovery_wait_retry", "thread_id": thread_id,
                                                     "agent": agent, "request_post_id": post_id}, now)
        _update(conn, key, wait | {"state": "relaunched", "relaunch_post_id": post["id"], "relaunched_at": now,
                                   "rule_id": human_actions.post_rule_id(board, post["id"]), "retries": used + [now],
                                   "relaunch_post_ids": ((wait.get("relaunch_post_ids") or []) + [post["id"]])[-10:]},
                now)
        _upsert(conn, agent_budget_key(agent), _times(conn, agent_budget_key(agent), now) + [now], now)

    covered = [i for i in wait.get("covered_post_ids") or [] if type(i) is int][:MAX_ITEMS]
    also = (f" The blocked attempt (session {wait['session_id']}) left request{'s' if len(covered) > 1 else ''} "
            f"{_ids(covered)} waiting: recover and resume {'them' if len(covered) > 1 else 'it'} the same way."
            if covered and type(wait.get("session_id")) is int else "")
    body = WAIT_BODY.format(post=post_id, recipient=wait["recipient"], old=wait["old_session"], also=also,
                            n=len(used) + 1, max=recovery.WAIT_RETRIES)
    post, _ = human_actions.post_as_human(
        board, human, thread_id=thread_id, body=body, type="request", to=[agent], needs_response=True, launch=[agent],
        purpose=WAIT_PURPOSE.format(thread=thread_id, post=post_id), post_check=check, post_hook=record)
    log.info("automatic recovery: the owner worktree of request %s is free; relaunching %s (retry %s, post %s)",
             post_id, agent, len(used) + 1, post["id"])
    return post["id"]


def wait_question(wait: dict, reason: str) -> dict:
    """The structured question when a wait's retries ran out: relaunch the agent with a one-click Unstick (recommended)
    or leave it. Server facts only (ids, agent names, the server's reason)."""
    post, agent, thread, old = wait["post_id"], wait["agent"], wait["thread_id"], wait["old_session"]
    return {
        "question": f"Request #{post} is still held by {agent}'s old session {old}, and automatic recovery did not "
                    f"take. Relaunch {agent} to recover it?",
        "context": f"Thread #{thread}, request #{post} (recipient {wait['recipient']}), assigned to {agent}, held by "
                   f"session {old}. Why it came to you: {reason}. No further automatic retries will be made for it.",
        "options": [
            _unstick_option("relaunch", f"Relaunch {agent} to recover request #{post} (Unstick)"[:200], thread, [agent],
                            f"recover request #{post} from session {old} and resume it"),
            _option("leave", "Leave it for now", (
                f"Launches nothing and takes this item out of Needs you. Costs nothing now; request #{post} stays with "
                f"session {old} until you act on the thread."))],
        "recommended_option_id": "relaunch"}


def _escalate_wait(board: Board, human: Principal, fence: tuple[str, str], key: str, wait: dict,
                   reason: str) -> int | None:
    """Tell the human, once, with a one-click question; the wait is then `escalated` (or `suppressed`, silently, over
    the per-agent daily escalation cap). Revokes the last relaunch's one-shot rule. Returns the post id or None."""
    now, agent, thread_id = board.now(), wait["agent"], wait["thread_id"]
    if not _escalations_left(board.conn, agent, now):
        with db.write_tx(board.conn) as c:
            if _get(c, key) == wait:
                _update(c, key, wait | {"state": "suppressed", "reason": reason, "escalated_at": now}, now)
        log.warning("recovery wait %s needs the human, but the daily cap of escalation posts for %s is reached", key,
                    agent)
        return None

    def check() -> None:
        if not _owner_ok(board.conn, fence):
            raise Conflict("another dispatcher owns this board now")
        if board.is_paused() or not enabled(board):
            raise Conflict("automatic recovery is paused or switched off")
        if _get(board.conn, key) != wait:
            raise Conflict(f"{key} changed meanwhile")
        if not _escalations_left(board.conn, agent, board.now()):
            raise Conflict(f"the daily cap of escalation posts for {agent} is reached")

    def record(post: dict) -> None:
        _insert(board.conn, POST_PREFIX + str(post["id"]), {"kind": "escalation", "thread_id": thread_id,
                                                           "request_post_ids": [wait["post_id"]]}, now)
        _upsert(board.conn, escalations_key(agent), _times(board.conn, escalations_key(agent), now) + [now], now)
        _update(board.conn, key, wait | {"state": "escalated", "reason": reason, "escalated_at": now,
                                         "escalation_post_id": post["id"]}, now)

    try:
        question = wait_question(wait, reason)
        issues._question(board, question)
    except Exception:
        log.exception("could not build the question for recovery wait %s; posting it without one", key)
        question = None
    body = WAIT_ESCALATION.format(post=wait["post_id"], agent=agent, old=wait["old_session"], reason=reason,
                                  next=WAIT_ESCALATION_NEXT if question else WAIT_ESCALATION_NEXT_PLAIN)
    post, _ = human_actions.post_as_human(
        board, human, thread_id=thread_id, body=body, type="question" if question else "status", to=[],
        needs_response=True, decision_question=question, post_check=check, post_hook=record)
    rule_id = wait.get("rule_id")
    if type(rule_id) is int:
        try:
            board.revoke_dispatch_rule(human, rule_id)
        except Exception:
            log.exception("could not revoke recovery wait rule %s", rule_id)
    log.warning("recovery wait %s: %s; told the human in post %s", key, reason, post["id"])
    return post["id"]


# ---------------------------------------------------------------- pruning


def prune(board: Board, force: bool = False) -> int:
    """At most hourly: drop recovery records of finished, declined or deleted tasks and any older than
    RECORD_TTL_SECONDS, spent launch budgets and escalation caps, attempt counters of finished, declined or deleted tasks, and
    replaced-lease evidence that is used up (the old session holds no unfinished request) or stale. Post markers stay:
    they keep automatic posts from lifting the agent-post cap."""
    conn, now = board.conn, board.now()
    last = _get(conn, PRUNED_KEY)
    if not force and isinstance(last, (int, float)) and now - last < PRUNE_EVERY_SECONDS:
        return 0
    gone: list[str] = []
    for key, _ in _keys(conn, awaiting.STATE_PREFIX).fetchall():
        # A depends_on record matters until its task is finished or declined (it may wait longer than a week).
        try:
            task_id = int(key[len(awaiting.STATE_PREFIX):])
        except ValueError:
            gone.append(key)
            continue
        task = conn.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if task is None or task["status"] in ("done", "declined"):
            gone.append(key)
    for prefix in (TASK_PREFIX, UNCLAIMED_PREFIX, CONTINUE_PREFIX):
        for key, value, updated in _keys(conn, prefix, "key, value, updated_at"):
            try:
                rec = json.loads(value)
            except (TypeError, ValueError):
                gone.append(key)
                continue
            task = conn.execute("SELECT status FROM tasks WHERE id = ?",
                                (rec.get("task_id") if isinstance(rec, dict) else None,)).fetchone()
            if task is None or task["status"] in ("done", "declined") or updated < now - RECORD_TTL_SECONDS:
                gone.append(key)
    for prefix in (BUDGET_PREFIX, AGENT_BUDGET_PREFIX, ESCALATIONS_PREFIX):
        for key, value in _keys(conn, prefix):
            try:
                times = json.loads(value)
            except (TypeError, ValueError):
                times = []
            if not isinstance(times, list) or not any(
                    isinstance(t, (int, float)) and t > now - BUDGET_WINDOW_SECONDS for t in times):
                gone.append(key)
    for key, _ in _keys(conn, ATTEMPTS_PREFIX):
        try:
            task_id = int(key[len(ATTEMPTS_PREFIX):])
        except ValueError:
            gone.append(key)
            continue
        task = conn.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if task is None or task["status"] in ("done", "declined"):
            gone.append(key)
    for key, value, updated in _keys(conn, recovery.WAIT_PREFIX, "key, value, updated_at"):
        # Settled waits (and their retry history, which only matters for a day) go after RECORD_TTL_SECONDS; a live
        # one escalates within MAX_WAIT_SECONDS, so it never sits here that long.
        try:
            wait = json.loads(value)
        except (TypeError, ValueError):
            wait = None
        post = wait.get("post_id") if isinstance(wait, dict) else None
        if (not isinstance(wait, dict) or conn.execute("SELECT 1 FROM posts WHERE id = ?", (post,)).fetchone() is None
                or updated < now - RECORD_TTL_SECONDS):
            gone.append(key)
    for key, value, updated in _keys(conn, recovery.RECLAIMED_PREFIX, "key, value, updated_at"):
        try:
            sid = json.loads(value).get("session_id")
        except (TypeError, ValueError, AttributeError):
            sid = None
        held = sid is not None and conn.execute(
            "SELECT 1 FROM request_progress WHERE assigned_session = ? AND state != 'finished'", (sid,)).fetchone()
        if not held or updated < now - RECORD_TTL_SECONDS:
            gone.append(key)
    with db.write_tx(conn) as c:
        for key in gone:
            c.execute("DELETE FROM board_state WHERE key = ?", (key,))
        _upsert(c, PRUNED_KEY, now, now)
    return len(gone)


# ---------------------------------------------------------------- for the dashboard


def list_records(board: Board, p: Principal) -> list[dict]:
    """Pending automatic recoveries, and escalated ones the human has not handled yet (their Needs you post is still
    open and the thread was not unstuck since), on open threads, newest first (human only): ids, agent names, states,
    times, the server's reason, and for a blocked recovery request its recorded reason (`detail`, text the agent or
    the dispatcher wrote; render it as text)."""
    board._require_human(p, "view automatic recoveries")
    conn = board.conn
    out = []
    for key, rec in _records(conn):
        if rec.get("state") not in ("sent", "escalated"):
            continue
        thread = conn.execute("SELECT status FROM threads WHERE id = ?", (rec.get("thread_id"),)).fetchone()
        if thread is None or thread["status"] != "open":
            continue
        if awaiting.awaits(conn, rec["task_id"]):
            continue    # the task now waits on other work: not stalled, nothing waits on the human here
        if rec["state"] == "escalated":
            note = rec.get("escalation_post_id")
            if (not isinstance(note, int) or conn.execute(
                    f"SELECT 1 FROM posts p WHERE p.id = ? AND {Board.NEEDS_YOU}", (note,)).fetchone() is None
                    or _unstuck_since(conn, rec["thread_id"], rec.get("agent"), rec.get("escalated_at") or 0)):
                continue    # the human answered it or unstuck the thread: nothing waits on them here any more
        out.append({"kind": rec["kind"], "task_id": rec["task_id"], "thread_id": rec["thread_id"],
                    "agent": rec.get("agent"), "owner_session": rec.get("owner_session"),
                    "post_id": rec.get("post_id"), "state": rec["state"], "reason": rec.get("reason"),
                    "detail": rec.get("detail"), "escalation_post_id": rec.get("escalation_post_id"),
                    "held_request_ids": rec.get("held_request_ids") or [],
                    "sent_at": iso(rec.get("sent_at")), "escalated_at": iso(rec.get("escalated_at")),
                    "_order": rec.get("escalated_at") or rec.get("sent_at") or 0})
    # Recovery waits (kind "recovery_wait"): the board waits for the owner worktree of request_post_id to be free and
    # retries by itself (`waiting`, `relaunched`: not the human's turn), or its retries ran out (`escalated`, shown
    # until the human handles that Needs you post). post_id is the latest automatic relaunch request, if any.
    now = board.now()
    for _, wait in recovery.waits(conn):
        state = wait.get("state")
        if state not in ("waiting", "relaunched", "escalated"):
            continue
        thread = conn.execute("SELECT status FROM threads WHERE id = ?", (wait.get("thread_id"),)).fetchone()
        if thread is None or thread["status"] != "open":
            continue
        if state == "escalated":
            note = wait.get("escalation_post_id")
            if (type(note) is not int or conn.execute(
                    f"SELECT 1 FROM posts p WHERE p.id = ? AND {Board.NEEDS_YOU}", (note,)).fetchone() is None
                    or _unstuck_since(conn, wait["thread_id"], wait["agent"], wait.get("escalated_at") or 0)):
                continue
        elif not recovery.wait_live(board, wait):
            continue
        ints = lambda v: [x for x in v if type(x) is int] if isinstance(v, list) else []   # noqa: E731
        out.append({"kind": "recovery_wait", "task_id": None, "thread_id": wait["thread_id"], "agent": wait["agent"],
                    "owner_session": wait.get("old_session"), "request_post_id": wait["post_id"],
                    "recipient": wait.get("recipient"), "post_id": wait.get("relaunch_post_id"), "state": state,
                    "reason": wait.get("reason") if state == "escalated" else wait.get("blocker"), "detail": None,
                    "escalation_post_id": wait.get("escalation_post_id"), "held_request_ids": [wait["post_id"]],
                    "covered_post_ids": ints(wait.get("covered_post_ids")),
                    "covered_task_ids": ints(wait.get("covered_task_ids")),
                    "retries_used": len(recovery.retries_used(wait, now)), "max_retries": recovery.WAIT_RETRIES,
                    "sent_at": iso(wait.get("relaunched_at")), "escalated_at": iso(wait.get("escalated_at")),
                    "_order": wait.get("escalated_at") or wait.get("relaunched_at") or wait.get("recorded_at") or 0})
    out.sort(key=lambda r: r.pop("_order"), reverse=True)
    return out[:LIST_MAX]
