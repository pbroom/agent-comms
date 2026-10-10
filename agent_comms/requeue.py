"""Requests of a run stopped for a maintenance restart go back to the queue, once (DESIGN_NOTES "Requeue after a
dispatcher stop").

Stopping the dispatcher terminates running children; their runs end with status 'stopped'. Their unsettled requests
used to be marked blocked ("Runner ended without explicit request completion: stopped"). For a restart that is a
system artifact, not a real blocker, and the thread then showed as stalled (live #607). But a stop is also how the
human halts a runaway agent, so halting stays the default and requeueing is explicit.

Stop modes, recorded on each stopped run as `stop_mode`:
- HALT (the default): `board dispatch stop`, the Settings page's stop, Ctrl-C or SIGTERM to the loop, a takeover by
  another loop, and any stop while the board is paused. Requests stay blocked as before; nothing is refunded.
- REQUEUE: only `board dispatch stop --requeue` (a maintenance restart), and never while the board is paused at stop
  time. A run recorded before stop modes existed counts as halted.

Guardrails for REQUEUE:
- Only runs the dispatcher stopped (status 'stopped'). A run whose own exit was already known at the stop, that timed
  out, failed to start, or ended unwatched ('gone': its exit is unknown) keeps the blocked behavior.
- Only generic requests (never a managed continuation, which has its own delivery contract), and only rows the stopped
  run still held unsettled (queued, or started by that run's own session). A request the agent blocked or finished
  itself is never touched.
- Only while the human's board setting `tasks.auto_recover_stalled_work` is on, and only by the dispatcher loop that
  owns the board (its fence token is checked in the transaction).
- At most one automatic requeue per request (post and original recipient) per rolling WINDOW_SECONDS, recorded durably
  in board_state (KEY_PREFIX) in the same transaction as the requeue. A second stop within the window leaves the
  request blocked. With a standing rule and a daily maintenance restart, a request can therefore be relaunched about
  once a day; a run that keeps being stopped is relaunched at most once per 24 hours.
- The relaunch needs everything an ordinary launch needs: an active, unexpired, unrevoked dispatch rule covering the
  post (a one-click rule only for its own post), the board not paused, the agent not live or busy, the concurrency
  and run-directory limits, and the dispatcher's fence. The stopped run's launch is given back to its rule (never
  above max_launches) only when that rule may launch this very post again (refundable): a one-click rule still bound
  to it, or a rule recorded at launch as never one-click. Otherwise a one-click rule whose post binding was pruned
  would revive as a thread-wide rule. human_actions.prune_post_rules keeps a binding while one of its rule's runs is
  stopped for requeue and not yet decided (undecided), so the binding cannot vanish between the stop and the refund.
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
REASON = ("Requeued automatically: a maintenance restart (board dispatch stop --requeue) stopped run {run} before this "
          "request was settled (at most once a day per request, under the board setting auto_recover_stalled_work)")
USED = " (automatic requeue already used within 24 hours)"


def key(post_id: int, recipient: str) -> str:
    return f"{KEY_PREFIX}{int(post_id)}.{recipient}"


def requeue_stop(record: dict) -> bool:
    """A run the dispatcher stopped for a maintenance restart: its requests may be requeued."""
    return isinstance(record, dict) and record.get("status") == "stopped" and record.get("stop_mode") == REQUEUE


def mark_decided(record: dict, post_id: int) -> None:
    decided = record.get("requeue_decided") if isinstance(record.get("requeue_decided"), list) else []
    if post_id not in decided:
        record["requeue_decided"] = decided + [post_id]


def undecided(record: dict) -> bool:
    """Stopped for requeue, with a post whose requeue decision is not recorded yet."""
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
    """The requests requeued by maintenance stops of runs that ended at or after `since` (for the CLI's report)."""
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
    """The stopped run's attempt at this post was handed back: it no longer counts as the post's one attempt."""
    return any(pid == post_id for pid, _ in requeued_pairs(record))


def decide(board, c, fence: tuple[str, str] | None, post_id: int, recipient: str) -> str | None:
    """'requeue' when this stopped run's request may be requeued now, 'used' when only the once-a-day bound stops it,
    None when the setting is off or this loop does not own the board. Call inside the write transaction."""
    from . import autorecover
    if fence is None or not autorecover.enabled(board):
        return None
    owner = c.execute("SELECT value FROM board_state WHERE key = ?", (fence[0],)).fetchone()
    if owner is None or owner["value"] != fence[1]:
        return None
    row = c.execute("SELECT value FROM board_state WHERE key = ?", (key(post_id, recipient),)).fetchone()
    if row is None:
        return "requeue"
    try:
        last = json.loads(row["value"]).get("at")
    except (TypeError, ValueError, AttributeError):
        return "used"    # an unreadable record counts as used: never requeue twice
    ok = isinstance(last, (int, float)) and not isinstance(last, bool) and board.now() - last >= WINDOW_SECONDS
    return "requeue" if ok else "used"


def apply(board, c, row: dict, agent: str, run_id: str, record: dict) -> None:
    """Queue the request again (fresh version, unassigned session, server-written reason) and record the requeue in
    board_state and in the stopped run's record. The caller saves the record and gives back the launch."""
    now, version = board.now(), row["version"] + 1
    reason = REASON.format(run=run_id)
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
              (key(row["post_id"], row["recipient"]), json.dumps({"at": now, "run_id": run_id}), now))
    pairs = record.get("requeued") if isinstance(record.get("requeued"), list) else []
    record["requeued"] = pairs + [[row["post_id"], row["recipient"]]]
