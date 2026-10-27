"""Delivery receipts and the undelivered-request alert (DESIGN_NOTES "Delivery receipts").

A request can sit on the board with nobody able to read it: its recipient has no session in the thread's project, no
approved dispatch rule would launch one, and the agent that sent it cannot tell (live case: thread 27, a Codex handoff
to claude-code for a project no Claude session had ever opened, while the Claude the human was talking to ran in a
cloud container that cannot reach this board). Two things close that gap:

- *Receipts*. The first time a session of a request's recipient is handed the post (board_read_updates, or the
  post_ids view a dispatched run uses), the server records {session_id, at} under RECEIPT_PREFIX. Each request row
  then carries `delivered_at` and `delivered_session` (requests.for_post), so the sender can see whether its request
  reached anyone. A receipt is evidence of delivery only: it never starts, acknowledges or finishes the request.
- *The alert*. On each dispatcher pass (fenced, not while paused, only when `[dispatch] undelivered_alert_minutes` is
  above zero), a request that is still queued and unassigned, has no receipt for that recipient after that many
  minutes, is not one the dispatcher would launch for (Dispatcher._rule_for: a workstream rule, an inbox policy, or
  the one-click rule of that very post; its pickup path handles those), and whose
  recipient has no session in the thread's project seen within the same window, is reported once to the human with a
  needs-response question in its thread (Needs you). The question recommends launching the agent once (Unstick for
  that agent only) when it has a runner, and otherwise opening a session by hand. The item closes on its own once
  every request it names is delivered, answered or gone (close_delivered), also after the alert is switched off.
  Receipts, alert records and daily counters older than KEEP_SECONDS are pruned hourly.

Guardrails, as in autorecover.py: fixed server-side text naming only thread and post ids and agent names; nothing an
agent wrote (bodies, titles, project paths) reaches the post. Only posts created after the first pass with the alert
on (WATERMARK_KEY) and younger than MAX_AGE_SECONDS are considered, so turning it on never floods Needs you with old
history. Each (post, recipient) is alerted at most once (ALERT_PREFIX), at most ALERTS_PER_PASS posts per pass and
DAILY_CAP posts per recipient per rolling day. The post is marked automatic (autorecover.POST_PREFIX), so it does not
lift the agent-post cap and the dashboard labels it.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Callable

from . import awaiting, db, human_actions, issues, requests
from .autorecover import POST_PREFIX, _get, _insert, _owner_ok, _range, _times, _upsert
from .core import Board, Conflict, Principal, iso

log = logging.getLogger("agent_comms.delivery")

RECEIPT_PREFIX = "delivery.receipt."   # <post id>.<recipient>: {session_id, at}
ALERT_PREFIX = "delivery.alert."       # <post id>.<recipient>: {alert_post_id, at}
DAILY_PREFIX = "delivery.daily."       # <recipient>: times of alert posts naming it, rolling day
WATERMARK_KEY = "delivery.watermark"   # posts created before the first pass with the alert on never alert
MAX_AGE_SECONDS = 24 * 3600
ALERTS_PER_PASS = 5
DAILY_CAP = 5
DAY = 24 * 3600
MAX_NAMED = 20

HEADER = ("Undelivered request (sent by the dispatcher because [dispatch] undelivered_alert_minutes is on; not a "
          "human click): {items}. No session of {them} has read {it}, none was seen in this thread's project in the "
          "last {minutes} minutes, and no approved dispatch rule would launch one. This needs you: pick an option "
          "below. This item closes on its own once {it} {verb} read.")
RESOLVED = ("Closed automatically by the dispatcher: every request this item named was delivered, answered or is "
            "gone, so nothing here waits on you any more.")


def _key(prefix: str, post_id: int, recipient: str) -> str:
    return f"{prefix}{post_id}.{recipient}"


# ---------------------------------------------------------------- receipts


def record(board: Board, p: Principal, session_id: int, posts: list[dict]) -> None:
    """Record a receipt for each request in `posts` addressed to the reading agent that has none yet. Never raises:
    a read must not fail because a receipt could not be written."""
    if p.is_human:
        return
    keys = [_key(RECEIPT_PREFIX, d["id"], p.name) for d in posts
            if any(r.get("recipient") == p.name for r in d.get("requests") or [])]
    keys = [k for k in dict.fromkeys(keys)
            if board.conn.execute("SELECT 1 FROM board_state WHERE key = ?", (k,)).fetchone() is None]
    if not keys:
        return   # a re-read before ack, or a long-poll wake, takes no write lock
    try:
        now = board.now()
        with db.write_tx(board.conn) as c:
            for key in keys:
                c.execute("""INSERT INTO board_state(key, value, updated_by, updated_at) VALUES (?, ?, ?, ?)
                             ON CONFLICT(key) DO NOTHING""",
                          (key, json.dumps({"session_id": session_id, "at": now}), p.name, now))
    except Exception:
        log.warning("could not record delivery receipts for session %s", session_id, exc_info=True)


def receipt(conn, post_id: int, recipient: str) -> dict | None:
    value = _get(conn, _key(RECEIPT_PREFIX, post_id, recipient))
    return value if isinstance(value, dict) else None


def annotate(conn, post_id: int, row: dict) -> None:
    """Add `delivered_at` / `delivered_session` to one request row (requests.for_post)."""
    got = receipt(conn, post_id, row["recipient"])
    row["delivered_at"] = iso(got["at"]) if got else None
    row["delivered_session"] = got.get("session_id") if got else None


# ---------------------------------------------------------------- the alert


def tick(board: Board, human: Principal, *, fence: tuple[str, str], alert_minutes: float,
         runner_for: Callable[[str], Any], rule_for: Callable[[list[dict], dict], dict | None]) -> list[int]:
    """One dispatcher pass: close alerts whose requests settled (also with the alert switched off, so an open item
    never strands), then alert new undelivered requests. `rule_for` is the dispatcher's own rule matching
    (Dispatcher._rule_for), so "covered by a rule" means exactly "the dispatcher would launch for it".
    Returns the ids of alert posts made."""
    if board.is_paused() or not _owner_ok(board.conn, fence):
        return []
    close_delivered(board, human)
    _prune(board)
    if alert_minutes <= 0:
        return []
    now = board.now()
    if _get(board.conn, WATERMARK_KEY) is None:
        with db.write_tx(board.conn) as c:
            c.execute("""INSERT INTO board_state(key, value, updated_by, updated_at) VALUES (?, ?, 'dispatcher', ?)
                         ON CONFLICT(key) DO NOTHING""", (WATERMARK_KEY, json.dumps(now), now))
        return []
    window = alert_minutes * 60
    by_thread = _undelivered(board, human, now, window, rule_for)
    made = []
    for thread_id, entries in sorted(by_thread.items()):
        if len(made) >= ALERTS_PER_PASS:
            break
        entries = [e for e in entries if len(_times(board.conn, DAILY_PREFIX + e[1], now)) < DAILY_CAP]
        if not entries:
            continue
        try:
            post_id = _alert(board, human, fence, thread_id, entries, int(alert_minutes), runner_for)
        except Conflict as e:
            log.info("undelivered alert on thread %s skipped: %s", thread_id, e)
            continue
        made.append(post_id)
    return made


def _undelivered(board: Board, human: Principal, now: float, window: float,
                 rule_for: Callable[[list[dict], dict], dict | None]) -> dict[int, list[tuple[int, str]]]:
    c = board.conn
    watermark = _get(c, WATERMARK_KEY)
    rules = board.active_dispatch_rules(human)
    from . import workstreams
    out: dict[int, list[tuple[int, str]]] = {}
    for post in c.execute("""SELECT p.*, t.project AS thread_project FROM posts p JOIN threads t ON t.id = p.thread_id
                             WHERE t.status = 'open' AND p.sealed = 0 AND p.created_at >= ? AND p.created_at <= ?
                               AND p.created_at >= ? ORDER BY p.id""",
                          (watermark, now - window, now - MAX_AGE_SECONDS)):
        for row in requests.for_post(board, post):
            agent = row["recipient"]
            if (row["state"] != "queued" or row["assigned_session"] is not None or row["assigned_agent"] != agent
                    or receipt(c, post["id"], agent) is not None
                    or _get(c, _key(ALERT_PREFIX, post["id"], agent)) is not None):
                continue
            if c.execute("SELECT 1 FROM agents WHERE name = ? AND active = 1 AND is_human = 0", (agent,)).fetchone() \
                    is None:
                continue
            item = {"agent": agent, "thread_id": post["thread_id"], "post_id": post["id"],
                    "post_created_at": post["created_at"],
                    "managed": workstreams.get_for_post(board, post["id"]) is not None}
            if rule_for(rules, item) is not None:
                continue   # the dispatcher would launch for it: its pickup path launches and reports it
            if c.execute("SELECT 1 FROM sessions WHERE agent = ? AND project = ? AND last_seen >= ?",
                         (agent, post["thread_project"], now - window)).fetchone():
                continue   # a session in the project is live and will read it; it may only be busy
            out.setdefault(post["thread_id"], []).append((post["id"], agent))
    return out


def _question(thread_id: int, entries: list[tuple[int, str]], runnable: list[str]) -> dict:
    agents = list(dict.fromkeys(a for _, a in entries))
    names = ", ".join(agents)
    manual = {"id": "open-session", "label": f"I'll open a {names} session myself"[:200], "outcome": "answered",
              "description": (f"Launches nothing now. Open {names} on the machine that has thread #{thread_id}'s "
                              "project checked out (or connect it to this board); it finds the request on its first "
                              "board read, and this item closes on its own. Costs nothing until you open it.")}
    if runnable:
        launch = {"id": "launch", "label": f"Launch {', '.join(runnable)} once (Unstick)"[:200], "outcome": "approved",
                  "description": (f"Runs Unstick on thread #{thread_id}, as if you clicked it, for "
                                  f"{', '.join(runnable)} only: posts a request as you to read and handle what the "
                                  "thread waits on, and allows one launch of each in the thread's project. Costs an "
                                  "agent run (tokens); the server refuses it if the thread no longer waits on them."),
                  "action": {"type": "unstick", "thread_id": thread_id, "agents": runnable[:20]}}
        options, recommended = [launch, manual], "launch"
    else:
        leave = {"id": "leave", "label": "Leave it for now", "outcome": "answered",
                 "description": ("Launches nothing and takes this item out of Needs you. Costs nothing; the request "
                                 "stays unread until a session of its recipient opens the board.")}
        options, recommended = [manual, leave], "open-session"
    ids = ", ".join(f"#{p}" for p, _ in entries[:MAX_NAMED])
    return {"question": (f"Nobody has read request{'s' if len(entries) > 1 else ''} {ids} on thread #{thread_id} "
                         f"addressed to {names}. " + ("Launch it once?" if runnable else "Open a session for it?")),
            "context": ("No session of the recipient was seen in this thread's project and no approved dispatch "
                        "rule covers it." + ("" if runnable else f" No dispatcher runner is configured for {names}.")),
            "options": options, "recommended_option_id": recommended}


def _alert(board: Board, human: Principal, fence: tuple[str, str], thread_id: int, entries: list[tuple[int, str]],
           minutes: int, runner_for: Callable[[str], Any]) -> int:
    agents = list(dict.fromkeys(a for _, a in entries))
    runnable = [a for a in agents if runner_for(a)]
    keys = [_key(ALERT_PREFIX, p, a) for p, a in entries]
    now = board.now()

    def check() -> None:
        if not _owner_ok(board.conn, fence):
            raise Conflict("another dispatcher owns this board now")
        if board.is_paused():
            raise Conflict("the board is paused")
        for (post_id, agent), key in zip(entries, keys):
            if _get(board.conn, key) is not None or receipt(board.conn, post_id, agent) is not None:
                raise Conflict(f"{key} changed meanwhile")

    def hook(post: dict) -> None:
        _insert(board.conn, POST_PREFIX + str(post["id"]),
                {"kind": "undelivered", "thread_id": thread_id, "entries": [list(e) for e in entries]}, now)
        for key in keys:
            _insert(board.conn, key, {"alert_post_id": post["id"], "at": now}, now)
        for agent in agents:
            _upsert(board.conn, DAILY_PREFIX + agent, _times(board.conn, DAILY_PREFIX + agent, now) + [now], now)

    question = _question(thread_id, entries, runnable)
    issues._question(board, question)
    shown = entries[:MAX_NAMED]
    items = "; ".join(f"request #{p} to {a}" for p, a in shown)
    if len(entries) > len(shown):
        items += f"; and {len(entries) - len(shown)} more"
    many = len(entries) > 1
    body = HEADER.format(items=items, them=", ".join(agents), it="them" if many else "it",
                         verb="are" if many else "is", minutes=minutes)
    post, _ = human_actions.post_as_human(board, human, thread_id=thread_id, body=body, type="question", to=[],
                                          needs_response=True, decision_question=question, post_check=check,
                                          post_hook=hook)
    log.warning("requests %s on thread %s were not delivered; told the human in post %s", entries, thread_id,
                post["id"])
    return post["id"]


def _settled(board: Board, c, post_id: int, agent: str) -> bool:
    """A request named by an alert no longer waits on delivery: it was read, it moved on, or it is gone."""
    if receipt(c, post_id, agent) is not None:
        return True
    post = c.execute("SELECT p.*, t.status AS thread_status FROM posts p JOIN threads t ON t.id = p.thread_id "
                     "WHERE p.id = ?", (post_id,)).fetchone()
    if post is None or post["sealed"] or post["thread_status"] != "open":
        return True
    row = next((r for r in requests.for_post(board, post) if r["recipient"] == agent), None)
    return row is None or row["state"] != "queued" or row["assigned_session"] is not None


PRUNED_KEY = "delivery.pruned_at"
KEEP_SECONDS = 30 * DAY   # receipts and alert records older than this are dropped (hourly)


def _prune(board: Board) -> None:
    now = board.now()
    last = _get(board.conn, PRUNED_KEY)
    if isinstance(last, (int, float)) and now - last < 3600:
        return
    try:
        with db.write_tx(board.conn) as c:
            for prefix in (RECEIPT_PREFIX, ALERT_PREFIX, DAILY_PREFIX):
                c.execute("DELETE FROM board_state WHERE key >= ? AND key < ? AND updated_at < ?",
                          (*_range(prefix), now - KEEP_SECONDS))
            _upsert(c, PRUNED_KEY, now, now)
    except Exception:
        log.exception("could not prune delivery records")


def close_delivered(board: Board, human: Principal) -> list[int]:
    """Close every undelivered alert still in Needs you whose requests have all settled."""
    conn, closed = board.conn, []
    candidates = []
    for r in conn.execute("""SELECT key, value FROM board_state WHERE key >= ? AND key < ?
                             AND json_valid(value) AND json_extract(value, '$.kind') = 'undelivered'
                             AND json_type(value, '$.closed_at') IS NULL""", _range(POST_PREFIX)):
        candidates.append((int(r["key"][len(POST_PREFIX):]), json.loads(r["value"])))
    if not candidates:
        return []
    try:
        human_sid = board.human_session(human)
        now = board.now()
        with db.write_tx(conn) as c:
            for post_id, marker in candidates:
                if c.execute(f"SELECT 1 FROM posts p WHERE p.id = ? AND {Board.NEEDS_YOU}", (post_id,)).fetchone() \
                        is None:
                    continue
                if not all(_settled(board, c, p, a) for p, a in marker.get("entries") or []):
                    continue
                c.execute("""INSERT INTO attention_resolutions(post_id, resolved_by, session_id, reason,
                             evidence_post_ids, resolved_at) VALUES (?, ?, ?, ?, '[]', ?)""",
                          (post_id, human.name, human_sid, RESOLVED, now))
                c.execute("UPDATE board_state SET value = ?, updated_by = 'dispatcher', updated_at = ? WHERE key = ?",
                          (json.dumps(marker | {"closed_at": now}), now, POST_PREFIX + str(post_id)))
                awaiting._bump(c, post_id, now)
                closed.append(post_id)
    except Exception:
        log.exception("could not close delivered alerts")
        return []
    return closed
