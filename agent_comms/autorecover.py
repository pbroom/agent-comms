"""Automatic recovery of abandoned and unclaimed work (DESIGN_NOTES "Automatic recovery").

Two kinds of stall used to wait for the human to click Unstick, sometimes for hours:
- *Abandoned work*: a task in `working` or `blocked` whose lease expired more than GRACE_SECONDS ago while its owner
  session has not been seen since the lease expired (an interactive session that went away, a crashed run). Its
  requests stay `started` by a session that will never come back.
- *An unclaimed task*: `accepted`, nobody owns it, unchanged for longer than the dashboard's stall window
  (UNCLAIMED_AFTER_SECONDS, the dashboard's 30 minutes plus 10 minutes grace), created by an agent.

The running dispatcher asks the owner (or the creator of an unclaimed task) to recover it, with the Unstick
machinery: a fixed `request` posted as the human and a fresh one-shot dispatcher rule bound to that post, so an agent
without a live session is launched once (human_actions.post_as_human).

Authorization: the human's board setting `tasks.auto_recover_stalled_work` (human-only, Settings page, default on)
is the standing approval that a click on Unstick would otherwise give, and only for these two stalls. Guardrails:
- Only the dispatcher loop that owns the board (its fence token is checked inside the post's write transaction),
  never while the board is paused, only while the setting is on, only on open threads not at their agent-post cap,
  and only for an active non-human agent that has a configured runner.
- Fixed server-side text. The body and the rule purpose vary only in thread, task, post and session ids and agent
  names; nothing an agent wrote (bodies, titles, summaries, request reasons) is read into them. The body says plainly
  that it is an automatic recovery under the human's setting, not a human click. The post is marked automatic
  (POST_PREFIX), so it does not lift the agent-post cap (Board.NOT_AUTOMATIC) and the dashboard can label it.
- Once only. At most one automatic recovery per (task id, lease_expires_at) for abandoned work, one per task for an
  unclaimed task, and MAX_PER_TASK over a task's lifetime. Each is recorded durably in `board_state` in the same
  transaction as the post, so a restarted dispatcher never repeats it.
- Escalation. A recovery that does not take (its request ends `blocked`, or the task is not reclaimed, settled or
  claimed within one lease TTL of the launch, or of the post when nothing launched) gets no further automatic launch:
  the human is told with a needs-response post addressed to nobody (Board.NEEDS_YOU, the dashboard's "Needs you"),
  and the dashboard shows the precise reason. A request held by a sticky browser denial
  (browser_readiness.request_blocker) is never relaunched around: it goes straight to the human.
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
MAX_PER_TASK = 3          # automatic recoveries of one task, over its lifetime
MAX_ITEMS = 20            # items named in one post (the rest are counted)
LIST_MAX = 100            # records sent to the dashboard

PREFIX = "auto_recovery."
TASK_PREFIX = PREFIX + "task."              # <task id>.<lease_expires_at>: abandoned work
UNCLAIMED_PREFIX = PREFIX + "unclaimed."    # <task id>: an unclaimed task
POST_PREFIX = PREFIX + "post."              # <post id>: a post made automatically (core.Board.NOT_AUTOMATIC)

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


def enabled(board: Board) -> bool:
    return getattr(board.s, SETTING, True) is True


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


def _update(conn, key: str, value: Any, now: float) -> None:
    conn.execute("UPDATE board_state SET value = ?, updated_by = 'dispatcher', updated_at = ? WHERE key = ?",
                 (json.dumps(value), now, key))


def _records(conn) -> list[tuple[str, dict]]:
    out = []
    for key, value in conn.execute("SELECT key, value FROM board_state WHERE key LIKE ? OR key LIKE ? ORDER BY key",
                                   (TASK_PREFIX + "%", UNCLAIMED_PREFIX + "%")):
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
    return conn.execute("SELECT COUNT(*) FROM board_state WHERE key LIKE ? OR key = ?",
                        (f"{TASK_PREFIX}{task_id}.%", unclaimed_key(task_id))).fetchone()[0]


def _ids(ids: list[int]) -> str:
    return unstick._ids(ids)


def _unstuck_since(conn, thread_id: int, since: float) -> bool:
    """The human clicked Unstick on this thread after the stall began: they already asked, so the dispatcher does not
    ask (and launch) a second time for the same stall."""
    at = _get(conn, unstick.STATE_PREFIX + str(thread_id))
    return isinstance(at, (int, float)) and not isinstance(at, bool) and at >= since


# ---------------------------------------------------------------- one pass


def tick(board: Board, human: Principal, *, runner_for: Callable[[str], Any], fence: tuple[str, str]) -> dict:
    """One pass, called by the dispatcher loop that owns the board, after its own pause check. Checks earlier
    recoveries first (settled, or escalated to the human), then sends new ones. Returns what it did (ids only)."""
    board._require_human(human, "run automatic recovery")
    if not enabled(board) or board.is_paused() or not _owner_ok(board.conn, fence):
        return {"sent": [], "escalated": []}
    escalated = _evaluate(board, human, fence)
    sent = _detect_and_send(board, human, runner_for, fence)
    return {"sent": sent, "escalated": escalated}


def _held_requests(conn, session_id: int | None, thread_id: int) -> list:
    if session_id is None:
        return []
    return list(conn.execute(
        """SELECT q.post_id, q.recipient FROM request_progress q JOIN posts p ON p.id = q.post_id
           WHERE q.assigned_session = ? AND p.thread_id = ? AND q.state != 'finished' ORDER BY q.post_id""",
        (session_id, thread_id)))


def _detect_and_send(board: Board, human: Principal, runner_for: Callable[[str], Any],
                     fence: tuple[str, str]) -> list[int]:
    conn, now = board.conn, board.now()
    groups: dict[tuple[int, str], list[dict]] = {}
    direct: list[tuple[str, dict]] = []      # escalated without a launch (browser denial, attempt limit)
    cap: dict[int, bool] = {}

    def at_cap(thread_id: int) -> bool:
        if thread_id not in cap:
            cap[thread_id] = _at_cap(board, thread_id)
        return cap[thread_id]

    # Abandoned work: the lease expired over GRACE_SECONDS ago and the owner session has not been seen since.
    for r in conn.execute(
            """SELECT tk.id, tk.thread_id, tk.owner_agent, tk.owner_session, tk.lease_expires_at, s.last_seen
               FROM tasks tk JOIN threads t ON t.id = tk.thread_id AND t.status = 'open'
               JOIN agents a ON a.name = tk.owner_agent AND a.active = 1 AND a.is_human = 0
               LEFT JOIN sessions s ON s.id = tk.owner_session
               WHERE tk.status IN ('working', 'blocked') AND tk.lease_expires_at IS NOT NULL
                 AND tk.lease_expires_at <= ? ORDER BY tk.id""", (now - GRACE_SECONDS,)).fetchall():
        if r["last_seen"] is not None and r["last_seen"] > r["lease_expires_at"]:
            continue    # seen since its lease expired: alive, not abandoned (the dashboard still shows the stall)
        if r["owner_session"] is not None and conn.execute(
                "SELECT 1 FROM tasks WHERE owner_session = ? AND lease_expires_at > ?", (r["owner_session"], now)).fetchone():
            continue    # it still holds a live lease elsewhere: active
        key = task_key(r["id"], r["lease_expires_at"])
        if _get(conn, key) is not None or workstreams.get_for_task(board, r["id"]) is not None:
            continue    # already handled once, or a managed continuation with its own reconciliation
        if (runner_for(r["owner_agent"]) is None or at_cap(r["thread_id"])
                or _unstuck_since(conn, r["thread_id"], r["lease_expires_at"])):
            continue
        held = _held_requests(conn, r["owner_session"], r["thread_id"])
        item = {"kind": "abandoned", "key": key, "task_id": r["id"], "thread_id": r["thread_id"],
                "agent": r["owner_agent"], "owner_session": r["owner_session"],
                "lease_expires_at": r["lease_expires_at"], "held_request_ids": sorted({h["post_id"] for h in held})}
        denied = next((h["post_id"] for h in held
                       if browser_readiness.request_blocker(board, h["post_id"], h["recipient"])), None)
        if denied is not None:
            direct.append((f"request #{denied} is held by a browser policy denial; a human permission change is "
                           "needed, so nothing was launched", item))
        elif _attempts(conn, r["id"]) >= MAX_PER_TASK:
            direct.append((f"the limit of {MAX_PER_TASK} automatic recoveries for this task is reached", item))
        else:
            groups.setdefault((r["thread_id"], r["owner_agent"]), []).append(item)

    # Unclaimed tasks: accepted, unowned and unchanged for the dashboard's stall window, created by an agent.
    for r in unstick.unclaimed_tasks(board, updated_before=now - UNCLAIMED_AFTER_SECONDS):
        thread = conn.execute("SELECT status FROM threads WHERE id = ?", (r["thread_id"],)).fetchone()
        key = unclaimed_key(r["id"])
        if (thread is None or thread["status"] != "open" or _get(conn, key) is not None
                or workstreams.get_for_task(board, r["id"]) is not None):
            continue
        if (runner_for(r["created_by"]) is None or at_cap(r["thread_id"])
                or _unstuck_since(conn, r["thread_id"], r["updated_at"])):
            continue
        item = {"kind": "unclaimed", "key": key, "task_id": r["id"], "thread_id": r["thread_id"],
                "agent": r["created_by"], "unclaimed_since": r["updated_at"]}
        if _attempts(conn, r["id"]) >= MAX_PER_TASK:
            direct.append((f"the limit of {MAX_PER_TASK} automatic recoveries for this task is reached", item))
        else:
            groups.setdefault((r["thread_id"], r["created_by"]), []).append(item)

    sent = []
    for (thread_id, agent), items in groups.items():
        try:
            sent.append(_send(board, human, fence, thread_id, agent, items))
        except Exception:
            log.exception("automatic recovery for %s on thread %s failed", agent, thread_id)
    by_thread: dict[int, list[tuple[str, dict]]] = {}
    for reason, item in direct:
        by_thread.setdefault(item["thread_id"], []).append((reason, item))
    for thread_id, items in by_thread.items():
        try:
            _escalate_new(board, human, fence, thread_id, items)
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


def _guard(board: Board, fence: tuple[str, str], keys: list[str]) -> Callable[[], None]:
    def check() -> None:
        # Inside the post's write transaction: the loop still owns the board, nothing paused or switched off since
        # the scan, and no other pass recorded these keys meanwhile.
        if not _owner_ok(board.conn, fence):
            raise Conflict("another dispatcher owns this board now")
        if board.is_paused() or not enabled(board):
            raise Conflict("automatic recovery is paused or switched off")
        for key in keys:
            if board.conn.execute("SELECT 1 FROM board_state WHERE key = ?", (key,)).fetchone():
                raise Conflict(f"{key} was already recovered")
    return check


def _send(board: Board, human: Principal, fence: tuple[str, str], thread_id: int, agent: str,
          items: list[dict]) -> int:
    keys = [x["key"] for x in items]
    now = board.now()

    def record(post: dict) -> None:
        _insert(board.conn, POST_PREFIX + str(post["id"]), {"kind": "recovery", "thread_id": thread_id,
                                                           "agent": agent, "task_ids": [x["task_id"] for x in items]}, now)
        rule_id = human_actions.post_rule_id(board, post["id"])   # bound to the post just before this hook
        for x in items:
            rec = {k: v for k, v in x.items() if k != "key"}
            rec.update(post_id=post["id"], rule_id=rule_id, sent_at=now, state="sent")
            _insert(board.conn, x["key"], rec, now)

    post, _ = human_actions.post_as_human(
        board, human, thread_id=thread_id, body=build_body(items), type="request", to=[agent], needs_response=True,
        launch=[agent], purpose=PURPOSE.format(thread=thread_id), post_check=_guard(board, fence, keys),
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
    for (value,) in board.conn.execute("SELECT value FROM board_state WHERE key LIKE 'dispatch.run.%'"):
        try:
            run = json.loads(value)
        except (TypeError, ValueError):
            continue
        if (isinstance(run, dict) and run.get("agent") == agent and post_id in (run.get("request_ids") or [])
                and run.get("pid") is not None and isinstance(run.get("started_at"), (int, float))):
            latest = max(latest or run["started_at"], run["started_at"])
    return latest


def _request_row(board: Board, post_id: int, agent: str) -> dict | None:
    post = board.conn.execute("SELECT * FROM posts WHERE id = ?", (post_id,)).fetchone()
    if post is None:
        return None
    return next((r for r in requests.for_post(board, post) if r["recipient"] == agent), None)


def _evaluate(board: Board, human: Principal, fence: tuple[str, str]) -> list[str]:
    conn, now = board.conn, board.now()
    ttl = board.s.lease_ttl_minutes * 60
    settled: list[tuple[str, dict]] = []
    failing: dict[int, list[tuple[str, dict, str | None]]] = {}
    for key, rec in _records(conn):
        if rec.get("state") not in ("sent", "escalated"):
            continue
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
            start = _launched_at(board, post_id, rec["agent"]) if isinstance(post_id, int) else None
            start = max(start or 0, rec.get("sent_at") or 0)
            if now >= start + ttl:
                minutes = board.s.lease_ttl_minutes
                reason = (f"the task was not reclaimed or settled within {minutes} minutes of the automatic recovery"
                          if rec["kind"] == "abandoned" else
                          f"the task was not claimed or declined within {minutes} minutes of the automatic recovery")
        if reason:
            failing.setdefault(rec["thread_id"], []).append((key, rec, reason, detail))
    if settled:
        with db.write_tx(conn) as c:
            for key, rec in settled:
                _update(c, key, rec, now)
    escalated = []
    for thread_id, items in failing.items():
        try:
            _escalate(board, human, fence, thread_id, items)
            escalated += [key for key, *_ in items]
        except Exception:
            log.exception("automatic recovery escalation on thread %s failed", thread_id)
    return escalated


# ---------------------------------------------------------------- escalation to the human


def _escalation_body(entries: list[tuple[dict, str]]) -> str:
    parts = []
    for rec, reason in entries[:MAX_ITEMS]:
        what = "abandoned" if rec["kind"] == "abandoned" else "unclaimed"
        parts.append(f"task {rec['task_id']} ({what}, {rec['agent']}): {reason}")
    if len(entries) > MAX_ITEMS:
        parts.append(f"{len(entries) - MAX_ITEMS} more")
    them = "this task" if len(entries) == 1 else "these tasks"
    return ESCALATION.format(items="; ".join(parts), them=them)


def _post_escalation(board: Board, human: Principal, fence: tuple[str, str], thread_id: int,
                     entries: list[tuple[str, dict, str, str | None]], new: bool) -> int:
    """One needs-response post to nobody (Needs you) for these records, and the records marked escalated, in one
    transaction. `new` records (escalated without a launch) are inserted; others must still be `sent`."""
    now = board.now()
    keys = [key for key, *_ in entries]

    def check() -> None:
        if not _owner_ok(board.conn, fence):
            raise Conflict("another dispatcher owns this board now")
        if board.is_paused() or not enabled(board):
            raise Conflict("automatic recovery is paused or switched off")
        for key in keys:
            current = _get(board.conn, key)
            if (current is not None) if new else (not isinstance(current, dict) or current.get("state") != "sent"):
                raise Conflict(f"{key} changed meanwhile")

    def record(post: dict) -> None:
        _insert(board.conn, POST_PREFIX + str(post["id"]), {"kind": "escalation", "thread_id": thread_id,
                                                           "task_ids": [rec["task_id"] for _, rec, *_ in entries]}, now)
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


def _escalate(board, human, fence, thread_id, items) -> int:
    return _post_escalation(board, human, fence, thread_id, items, new=False)


def _escalate_new(board, human, fence, thread_id, items: list[tuple[str, dict]]) -> int:
    entries = [(item["key"], item, reason, None) for reason, item in items]
    return _post_escalation(board, human, fence, thread_id, entries, new=True)


# ---------------------------------------------------------------- for the dashboard


def list_records(board: Board, p: Principal) -> list[dict]:
    """Pending and escalated automatic recoveries on open threads, newest first (human only): ids, agent names,
    states, times, the server's reason, and for a blocked recovery request its recorded reason (`detail`, text the
    agent or the dispatcher wrote; render it as text)."""
    board._require_human(p, "view automatic recoveries")
    out = []
    for key, rec in _records(board.conn):
        if rec.get("state") not in ("sent", "escalated"):
            continue
        thread = board.conn.execute("SELECT status FROM threads WHERE id = ?", (rec.get("thread_id"),)).fetchone()
        if thread is None or thread["status"] != "open":
            continue
        out.append({"kind": rec["kind"], "task_id": rec["task_id"], "thread_id": rec["thread_id"],
                    "agent": rec.get("agent"), "owner_session": rec.get("owner_session"),
                    "post_id": rec.get("post_id"), "state": rec["state"], "reason": rec.get("reason"),
                    "detail": rec.get("detail"), "escalation_post_id": rec.get("escalation_post_id"),
                    "held_request_ids": rec.get("held_request_ids") or [],
                    "sent_at": iso(rec.get("sent_at")), "escalated_at": iso(rec.get("escalated_at")),
                    "_order": rec.get("escalated_at") or rec.get("sent_at") or 0})
    out.sort(key=lambda r: r.pop("_order"), reverse=True)
    return out[:LIST_MAX]
