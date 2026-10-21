"""A run that ends before settling its requests hands them back to the queue (DESIGN_NOTES "Requeue after a
dispatcher stop", widened to every ended run).

The dispatcher, not the agent's session, owns liveness: when a dispatched run ends (it exited, timed out, was lost, or
was stopped for a maintenance restart) and a request it held is still queued or started by that run's own session,
the request goes back to the queue unassigned and the dispatcher launches it again, at most MAX_RETRIES times per
request in a rolling WINDOW_SECONDS. Only after that is it marked blocked, which is what reaches the human. This
replaces "Runner ended without explicit request completion" blocks as the first answer to a run that died mid-task.

Stop modes, recorded on each stopped run as `stop_mode`:
- HALT (the default): `board dispatch stop`, the Settings page's stop, Ctrl-C or SIGTERM to the loop, a takeover by
  another loop, and any stop while the board is paused. A stop is how the human halts a runaway agent, so its
  requests stay blocked and nothing is refunded.
- REQUEUE: `board dispatch stop --requeue` (a maintenance restart), never while the board is paused at stop time.

Guardrails:
- Only runs that ended by themselves ('exited', 'timeout', 'gone') or were stopped for requeue. A run that failed to
  start, failed its tool preflight, or was halted keeps the blocked behavior: relaunching cannot fix those.
- Only generic requests (never a managed continuation, which has its own delivery contract), and only rows the run
  still held unsettled (queued, or started by that run's own session). A request the agent blocked or finished
  itself is never touched.
- Only while the human's board setting `tasks.auto_recover_stalled_work` is on, and only by the dispatcher loop that
  owns the board (its fence token is checked in the transaction).
- At most MAX_RETRIES requeues per request (post and original recipient) per rolling WINDOW_SECONDS, recorded durably
  in board_state (KEY_PREFIX) in the same transaction as the requeue. The next end within the window blocks it.
- The relaunch needs everything an ordinary launch needs: an active, unexpired, unrevoked dispatch rule covering the
  post (a one-click rule only for its own post), the board not paused, the agent not live or busy, the concurrency
  and run-directory limits, and the dispatcher's fence. The ended run's launch is given back to its rule (never
  above max_launches) only when that rule may launch this very post again (refundable): a one-click rule still bound
  to it, or a rule recorded at launch as never one-click. human_actions.prune_post_rules keeps a binding while one of
  its rule's ended runs is not yet decided (undecided), so the binding cannot vanish between the end and the refund.
- The decision for each of the run's posts is recorded on the run (`requeue_decided`) in the same transaction as the
  requeue or block.
- Nothing is finished, no task lease is renewed or released, and no text an agent wrote is read.
"""
from __future__ import annotations

import json

HALT = "halt"
REQUEUE = "requeue"
KEY_PREFIX = "dispatch.requeue."
WINDOW_SECONDS = 24 * 3600
MAX_RETRIES = 2
ENDED = ("exited", "timeout", "gone")
REASON = ("Requeued automatically: run {run} ended ({status}) before this request was settled; retry {n} of "
          f"{MAX_RETRIES} in 24 hours, under the board setting auto_recover_stalled_work")
USED = f" (automatic retries used: {MAX_RETRIES} within 24 hours)"


def key(post_id: int, recipient: str) -> str:
    return f"{KEY_PREFIX}{int(post_id)}.{recipient}"


def requeue_stop(record: dict) -> bool:
    """A run that ended by itself, or that the dispatcher stopped for a maintenance restart: its unsettled requests
    may be requeued."""
    if not isinstance(record, dict):
        return False
    return record.get("status") in ENDED or (record.get("status") == "stopped" and record.get("stop_mode") == REQUEUE)


def mark_decided(record: dict, post_id: int) -> None:
    decided = record.get("requeue_decided") if isinstance(record.get("requeue_decided"), list) else []
    if post_id not in decided:
        record["requeue_decided"] = decided + [post_id]


def undecided(record: dict) -> bool:
    """Ended or stopped for requeue, with a post whose requeue decision is not recorded yet."""
    if not requeue_stop(record):
        return False
    decided = record.get("requeue_decided") if isinstance(record.get("requeue_decided"), list) else []
    return any(i not in decided for i in record.get("request_ids", []) if type(i) is int)


def refundable(board, record: dict, post_id: int) -> bool:
    """Whether the stopped run's launch may go back to its rule: the rule may launch this post again. Call inside the
    refund's write transaction."""
    from . import human_actions
    rule_id = record.get("rule_id")
    if type(rule_id) is not int:
        return False
    if human_actions.post_rule_id(board, post_id) == rule_id:
        return True             # a one-click rule still bound to this very post
    # Never one-click, as recorded at launch and still now. A record without the field (launched before it existed)
    # is not trusted to be an ordinary rule.
    return record.get("one_click") is False and rule_id not in human_actions.one_click_rule_ids(board)


def requeued_since(board, since: float) -> list[dict]:
    """The requests requeued from runs that ended at or after `since` (for the CLI's report)."""
    out = []
    for (value,) in board.conn.execute("SELECT value FROM board_state WHERE key LIKE 'dispatch.run.%'"):
        try:
            run = json.loads(value)
        except (TypeError, ValueError):
            continue
        if (requeue_stop(run) and isinstance(run.get("ended_at"), (int, float)) and run["ended_at"] >= since):
            out += [{"post_id": pid, "recipient": who, "run_id": run.get("run_id")}
                    for pid, who in sorted(requeued_pairs(run))]
    return sorted(out, key=lambda x: (x["post_id"], x["recipient"]))


def requeued_pairs(record: dict) -> set[tuple[int, str]]:
    pairs = record.get("requeued") if isinstance(record, dict) else None
    out = set()
    for item in pairs if isinstance(pairs, list) else []:
        if isinstance(item, list) and len(item) == 2 and type(item[0]) is int and isinstance(item[1], str):
            out.add((item[0], item[1]))
    return out


def requeued_post(record: dict, post_id: int) -> bool:
    """The ended run's attempt at this post was handed back: it no longer counts as the post's one attempt."""
    return any(pid == post_id for pid, _ in requeued_pairs(record))


def _times(c, post_id: int, recipient: str) -> list[float] | None:
    """Earlier automatic requeues of this request; None when the record is unreadable (counts as used up)."""
    row = c.execute("SELECT value FROM board_state WHERE key = ?", (key(post_id, recipient),)).fetchone()
    if row is None:
        return []
    try:
        value = json.loads(row["value"])
    except (TypeError, ValueError):
        return None
    times = value.get("times", [value.get("at")]) if isinstance(value, dict) else None
    if not isinstance(times, list):
        return None
    return [t for t in times if isinstance(t, (int, float)) and not isinstance(t, bool)]


def decide(board, c, fence: tuple[str, str] | None, post_id: int, recipient: str) -> str | None:
    """'requeue' when this ended run's request may be requeued now, 'used' when only the retry bound stops it, None
    when the setting is off or this loop does not own the board. Call inside the write transaction."""
    from . import autorecover
    if fence is None or not autorecover.enabled(board):
        return None
    owner = c.execute("SELECT value FROM board_state WHERE key = ?", (fence[0],)).fetchone()
    if owner is None or owner["value"] != fence[1]:
        return None
    times = _times(c, post_id, recipient)
    if times is None:
        return "used"    # an unreadable record counts as used: never requeue without a bound
    recent = [t for t in times if board.now() - t < WINDOW_SECONDS]
    return "requeue" if len(recent) < MAX_RETRIES else "used"


def apply(board, c, row: dict, agent: str, run_id: str, record: dict) -> None:
    """Queue the request again (fresh version, unassigned session, server-written reason) and record the requeue in
    board_state and in the stopped run's record. The caller saves the record and gives back the launch."""
    now, version = board.now(), row["version"] + 1
    times = [t for t in (_times(c, row["post_id"], row["recipient"]) or []) if now - t < WINDOW_SECONDS] + [now]
    reason = REASON.format(run=run_id, status=record.get("status", "ended"), n=len(times))
    c.execute("""INSERT INTO request_progress
        (post_id,recipient,state,assigned_agent,assigned_session,reason,evidence_post_ids,version,updated_at)
        VALUES (?,?,'queued',?,NULL,?,'[]',?,?) ON CONFLICT(post_id,recipient) DO UPDATE SET
        state='queued',assigned_session=NULL,reason=excluded.reason,evidence_post_ids='[]',version=excluded.version,
        updated_at=excluded.updated_at""", (row["post_id"], row["recipient"], agent, reason, version, now))
    c.execute("""INSERT INTO request_events
        (post_id,recipient,actor,session_id,state,assigned_agent,assigned_session,reason,evidence_post_ids,version,
         created_at,event_source) VALUES (?,?,NULL,NULL,'queued',?,NULL,?,'[]',?,?,'dispatcher')""",
              (row["post_id"], row["recipient"], agent, reason, version, now))
    c.execute("""INSERT INTO board_state(key, value, updated_by, updated_at) VALUES (?, ?, 'dispatcher', ?)
                 ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_by = excluded.updated_by,
                 updated_at = excluded.updated_at""",
              (key(row["post_id"], row["recipient"]), json.dumps({"at": now, "times": times, "run_id": run_id}), now))
    pairs = record.get("requeued") if isinstance(record.get("requeued"), list) else []
    record["requeued"] = pairs + [[row["post_id"], row["recipient"]]]
