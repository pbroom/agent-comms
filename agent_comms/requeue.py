"""Requests of a run the dispatcher itself stopped go back to the queue, once (DESIGN_NOTES "Requeue after a
dispatcher stop").

`board dispatch stop` (or a restart through it) terminates running children; their runs end with status 'stopped'.
Their unsettled requests used to be marked blocked ("Runner ended without explicit request completion: stopped"),
which is a system artifact, not a real blocker, and the thread then showed as stalled until the human clicked
Unstick. Now such a request goes back to `queued` (a fresh version; the reason is in its history), and a following
dispatcher relaunches it through the ordinary trigger path.

Guardrails:
- Only runs the dispatcher stopped (status 'stopped': stop_children, or `board dispatch stop` with no loop running).
  A run that exited on its own, timed out, failed to start, or ended unwatched ('gone': its exit is unknown) keeps
  today's behavior: its requests are blocked.
- Only generic requests (never a managed continuation, which has its own delivery contract), and only rows the stopped
  run still held unsettled (queued, or started by that run's own session). A request the agent blocked or finished
  itself is never touched.
- Only while the human's board setting `tasks.auto_recover_stalled_work` is on (the standing approval for automatic
  recovery), and only by the dispatcher loop that owns the board (its fence token is checked in the transaction).
- At most one automatic requeue per request (post and original recipient) per rolling WINDOW_SECONDS, recorded durably
  in board_state (KEY_PREFIX) in the same transaction as the requeue. A second stop within the window leaves the
  request blocked, as before.
- The relaunch needs everything an ordinary launch needs: an active, unexpired, unrevoked dispatch rule covering the
  post (a one-click rule only for its own post), the board not paused, the agent not live or busy, the concurrency
  and run-directory limits, and the dispatcher's fence. The stopped run's launch is given back to its rule
  (never above max_launches) when its request is requeued, because that launch never got to do the work; this is what
  lets a one-shot rule (Unstick, Approve & launch, automatic recovery) relaunch it. The once-per-window bound caps it.
- Nothing is finished, no task lease is renewed or released, and no text an agent wrote is read.
"""
from __future__ import annotations

import json

KEY_PREFIX = "dispatch.requeue."
WINDOW_SECONDS = 24 * 3600
REASON = ("Requeued automatically: the dispatcher stopped run {run} before this request was settled (at most once a "
          "day per request, under the board setting auto_recover_stalled_work)")
USED = " (automatic requeue already used within 24 hours)"


def key(post_id: int, recipient: str) -> str:
    return f"{KEY_PREFIX}{int(post_id)}.{recipient}"


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
