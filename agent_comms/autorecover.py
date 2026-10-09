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
  gets no further automatic launch: its one-shot rule is revoked and the human is told with a needs-response post
  addressed to nobody (Board.NEEDS_YOU, the dashboard's "Needs you"), and the dashboard shows the precise reason. A
  request held by a sticky browser denial (browser_readiness.request_blocker) is never relaunched around: it goes
  straight to the human.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Callable

from . import browser_readiness, db, human_actions, recovery, requests, unstick, workstreams
from .core import Board, Conflict, Principal, iso

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
ESCALATION = ("Automatic recovery did not take (sent by the dispatcher under the human's board setting "
              "auto_recover_stalled_work; not a human click): {items}. No further automatic launches will be made "
              "for {them}. This needs you: open the thread and check the named request and task, then Unstick, "
              "reassign or decline the task.")


def task_key(task_id: int, lease_expires_at: float) -> str:
    return f"{TASK_PREFIX}{int(task_id)}.{float(lease_expires_at)!r}"


def unclaimed_key(task_id: int) -> str:
    return f"{UNCLAIMED_PREFIX}{int(task_id)}"


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
    for prefix in (TASK_PREFIX, UNCLAIMED_PREFIX):
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


def _unstuck_since(conn, thread_id: int, since: float) -> bool:
    """The human clicked Unstick on this thread after the stall began: they already asked, so the dispatcher does not
    ask (and launch) a second time for the same stall."""
    at = _get(conn, unstick.STATE_PREFIX + str(thread_id))
    return isinstance(at, (int, float)) and not isinstance(at, bool) and at >= since


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
    budget = {"escalations": MAX_ESCALATIONS_PER_PASS}
    escalated = _evaluate(board, human, fence, live_seconds, budget)
    sent = _detect_and_send(board, human, runner_for, fence, budget)
    return {"sent": sent, "escalated": escalated}


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
    key = task_key(r["id"], r["lease_expires_at"])
    if _get(conn, key) is not None or workstreams.get_for_task(board, r["id"]) is not None:
        return None     # already handled once, or a managed continuation with its own reconciliation
    if (runner_for(r["owner_agent"]) is None or at_cap(r["thread_id"])
            or _unstuck_since(conn, r["thread_id"], r["lease_expires_at"])):
        return None
    held = _held_requests(conn, r["owner_session"], r["thread_id"])
    item = {"kind": "abandoned", "key": key, "task_id": r["id"], "thread_id": r["thread_id"],
            "agent": r["owner_agent"], "owner_session": r["owner_session"], "task_status": r["status"],
            "lease_expires_at": r["lease_expires_at"], "stalled_since": r["lease_expires_at"],
            "held_request_ids": sorted({h["post_id"] for h in held})}
    denied = next((h["post_id"] for h in held
                   if browser_readiness.request_blocker(board, h["post_id"], h["recipient"])), None)
    if denied is not None:
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
            or workstreams.get_for_task(board, r["id"]) is not None):
        return None
    if (runner_for(r["created_by"]) is None or at_cap(r["thread_id"])
            or _unstuck_since(conn, r["thread_id"], r["updated_at"])):
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

    groups: dict[tuple[int, str], list[dict]] = {}
    direct: list[tuple[str, dict]] = []
    for item, reason in found:
        if reason is None:
            groups.setdefault((item["thread_id"], item["agent"]), []).append(item)
        else:
            direct.append((reason, item))
    sent: list[int] = []
    for (thread_id, agent), items in sorted(groups.items(), key=lambda g: min(x["stalled_since"] for x in g[1])):
        spent = _budget_reason(conn, agent, thread_id, board.now())
        if spent:
            direct += [(spent, x) for x in items]
            continue
        if len(sent) >= MAX_SENDS_PER_PASS:
            continue    # the next pass picks it up
        try:
            sent.append(_send(board, human, fence, thread_id, agent, items))
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
    if item["kind"] == "abandoned":
        text = (f"task {item['task_id']} was abandoned: its lease expired and owner session "
                f"{item['owner_session']} has not been seen since")
        if item.get("held_request_ids"):
            ids = item["held_request_ids"]
            text += f"; that session still holds request{'s' if len(ids) > 1 else ''} {_ids(ids)}"
        return text
    return (f"task {item['task_id']} is accepted but unclaimed (created by {item['agent']}): claim it or decline it "
            "if finished work already covers it")


def build_body(items: list[dict]) -> str:
    """The fixed request text. Only ids and agent names (server-stamped) vary; nothing an agent wrote."""
    parts = [_describe(x) for x in items]
    if len(parts) > MAX_ITEMS:
        parts = parts[:MAX_ITEMS] + [f"{len(parts) - MAX_ITEMS} more"]
    return f"{HEADER} This thread is stalled on you ({'; '.join(parts)}). {INSTRUCTIONS}"


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
            if _unstuck_since(conn, thread_id, x["stalled_since"]):
                raise Conflict(f"the human unstuck thread {thread_id} meanwhile")
        if _budget_reason(conn, agent, thread_id, board.now()):
            raise Conflict(f"the automatic launch budget for {agent} is spent")
    return check


def _send(board: Board, human: Principal, fence: tuple[str, str], thread_id: int, agent: str,
          items: list[dict]) -> int:
    now = board.now()

    def record(post: dict) -> None:
        conn = board.conn
        _insert(conn, POST_PREFIX + str(post["id"]), {"kind": "recovery", "thread_id": thread_id,
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

    post, _ = human_actions.post_as_human(
        board, human, thread_id=thread_id, body=build_body(items), type="request", to=[agent], needs_response=True,
        launch=[agent], purpose=PURPOSE.format(thread=thread_id), post_check=_guard(board, fence, items, agent, thread_id),
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
    if rec["kind"] == "abandoned":
        return (task["status"] not in ("working", "blocked") or task["lease_expires_at"] != rec.get("lease_expires_at")
                or task["owner_session"] != rec.get("owner_session"))
    return task["status"] != "accepted" or task["owner_agent"] is not None


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
            if _settled(rec, task):
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


def _escalation_body(entries: list[tuple[dict, str]]) -> str:
    parts = []
    for rec, reason in entries[:MAX_ITEMS]:
        what = "abandoned" if rec["kind"] == "abandoned" else "unclaimed"
        parts.append(f"task {rec['task_id']} ({what}, {rec['agent']}): {reason}")
    if len(entries) > MAX_ITEMS:
        parts.append(f"{len(entries) - MAX_ITEMS} more")
    them = "this stall" if len(entries) == 1 else "these stalls"
    return ESCALATION.format(items="; ".join(parts), them=them)


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

    post, _ = human_actions.post_as_human(
        board, human, thread_id=thread_id, body=_escalation_body([(rec, reason) for _, rec, reason, _ in entries]),
        type="status", to=[], needs_response=True, post_check=check, post_hook=record)
    log.warning("automatic recovery did not take on thread %s (tasks %s); told the human in post %s", thread_id,
                [rec["task_id"] for _, rec, *_ in entries], post["id"])
    return post["id"]


def _escalate(board, human, fence, thread_id, items) -> int | None:
    return _post_escalation(board, human, fence, thread_id, items, new=False)


def _escalate_new(board, human, fence, thread_id, items: list[tuple[str, dict]]) -> int | None:
    entries = [(item["key"], item, reason, None) for reason, item in items]
    return _post_escalation(board, human, fence, thread_id, entries, new=True)


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
    for prefix in (TASK_PREFIX, UNCLAIMED_PREFIX):
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
        if rec["state"] == "escalated":
            note = rec.get("escalation_post_id")
            if (not isinstance(note, int) or conn.execute(
                    f"SELECT 1 FROM posts p WHERE p.id = ? AND {Board.NEEDS_YOU}", (note,)).fetchone() is None
                    or _unstuck_since(conn, rec["thread_id"], rec.get("escalated_at") or 0)):
                continue    # the human answered it or unstuck the thread: nothing waits on them here any more
        out.append({"kind": rec["kind"], "task_id": rec["task_id"], "thread_id": rec["thread_id"],
                    "agent": rec.get("agent"), "owner_session": rec.get("owner_session"),
                    "post_id": rec.get("post_id"), "state": rec["state"], "reason": rec.get("reason"),
                    "detail": rec.get("detail"), "escalation_post_id": rec.get("escalation_post_id"),
                    "held_request_ids": rec.get("held_request_ids") or [],
                    "sent_at": iso(rec.get("sent_at")), "escalated_at": iso(rec.get("escalated_at")),
                    "_order": rec.get("escalated_at") or rec.get("sent_at") or 0})
    out.sort(key=lambda r: r.pop("_order"), reverse=True)
    return out[:LIST_MAX]
