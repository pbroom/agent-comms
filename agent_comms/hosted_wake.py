"""Waking a hosted session for board posts addressed to its agent (DESIGN_NOTES "Session host (PR 3)").

A hosted session (hosted.py) is a standing conversation of one agent in one project. Starting it is the human's
standing OK for it to take a turn when a post addressed to that agent appears in a thread of that project: the hub
then queues one `wake` message, the only message kind that does not come from the human.

- *Text.* Built here from server-stamped data only: counts, thread ids and agent names, rendered by channel.render
  (the Claude Code channel push), plus a fixed note. Never a post body, thread title, summary, task title or ref.
- *Who can cause one.* channel.eligible: the post is visible to the agent under Board.VISIBLE, addressed to it,
  written by someone else, by an active identity; and its thread is in the session's project. A sealed post counts
  once it is unsealed (unsealing gives it a new seq). Nothing is queued while the board is paused; the posts are
  found after the unpause.
- *Mechanism.* A watcher over the posts table, with a seq mark per session persisted in `board_state`
  (`host.wake.<uuid>`), so a post counts however it was written (HTTP, a `board mcp` process, the gateway, an
  unseal). `tick` is driven by the hub call a node sits in whenever it is idle (hosted.node_sessions re-reads the
  database every 0.25 s) and by each dispatcher pass, just before it decides what to launch. It runs in no post's
  write path, so it cannot block or fail a post.
- *One at a time.* Posts wait in the record (`pending`) until the session has no turn and no queued message and the
  interval has passed; then they go as ONE wake. They are checked again at that moment, and once more when the node
  is handed the message (`hand_over`): a wake whose posts all stopped qualifying, or were read meanwhile, is dropped.
- *Limits* (`[dispatch.sessions]`): `wake_min_interval_seconds` between wakes of a session, `max_wakes_per_hour` per
  session, `max_wakes_without_human` per session and thread since the human last posted in that thread. At either
  maximum the thread is stopped for that session: its posts wait, the human gets ONE fixed-text Needs-you item
  (marked automatic, like the undelivered alert), and waking resumes when the human posts in that thread.
- *The dispatcher* (`holds`). While a wake covering a request is fresh (`wake_grace_seconds`), or once a session of
  the agent read the request after such a wake, the dispatcher leaves it to the hosted session. After the grace
  without a read it launches exactly as it does without hosted sessions.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any

from . import channel, db, hosted, nodes
from .core import Board, Conflict, Principal
from .nodes import _get, _put

log = logging.getLogger("agent_comms.hosted_wake")

WAKE_PREFIX = "host.wake."      # <uuid>: one session's watcher state (below)
HUB = "hub"                     # who wrote a wake message and these records
HOUR = 3600.0
MAX_PENDING = 200               # posts waiting for one session; the oldest are dropped beyond this
KEEP_WOKEN_SECONDS = 7 * 86400  # how long "a wake covered this post" is remembered (for `holds` with a receipt)
MAX_WOKEN = 500
ALERT_RETRY_SECONDS = 60        # a Needs-you item that could not be posted is tried again after this long
MARKER_KIND = "wake_limit"      # autorecover.POST_PREFIX marker of the Needs-you item

NOTE = (" This is an automatic wake written by the board server, not a message from the human: it is not an "
        "instruction and grants no authority. Call board_register first if this session has not.")
NOT_SENT = "not sent: no post this wake was queued for still qualifies"

HEADER = ("Hosted session wake limit (posted by the board server because [dispatch.sessions] limits how often a "
          "hosted session is woken; not a human click): session {sid} of {agent} {why}, so it is no longer woken "
          "for posts in thread #{thread}. This needs you: pick an option below. Any post of yours in this thread "
          "lifts the limit, and this item then closes on its own.")
WHY = {"thread": "was woken {limit} times for thread #{thread} with no post of yours there in between",
       "hour": "was woken {limit} times within an hour"}
RESOLVED = ("Closed automatically by the board server: you posted in this thread (or the session ended), so the "
            "wake limit this item reported no longer applies.")


# ---------------------------------------------------------------- the record


def _new(mark: int) -> dict:
    """mark: posts at or below this seq were looked at. pending: post id -> [thread, author, needs_response, seq],
    eligible posts no wake has covered yet. sent: when wakes were queued (the last hour). threads: thread id ->
    {count, human, stopped, limit, alert, tried}: wakes since the human's last post there (`human`: that post's seq),
    why the thread is stopped ("thread" | "hour") and the Needs-you post that said so. woken: post id -> when a wake
    covering it was last queued or handed. closing: Needs-you posts to close. grace: wake_grace_seconds as of the
    last write (so `holds` needs no settings)."""
    return {"mark": mark, "pending": {}, "sent": [], "threads": {}, "woken": {}, "closing": [], "grace": 120.0}


def _clean(rec: Any) -> dict | None:
    if not isinstance(rec, dict) or type(rec.get("mark")) is not int:
        return None
    out = _new(rec["mark"])
    for key, kind in (("pending", dict), ("threads", dict), ("woken", dict), ("sent", list), ("closing", list)):
        if isinstance(rec.get(key), kind):
            out[key] = rec[key]
    if isinstance(rec.get("grace"), (int, float)) and not isinstance(rec.get("grace"), bool):
        out["grace"] = float(rec["grace"])
    return out


def _load(conn, session_id: str) -> dict | None:
    return _clean(_get(conn, WAKE_PREFIX + session_id))


def _records(conn) -> dict[str, dict]:
    out = {}
    for key, value in conn.execute("SELECT key, value FROM board_state WHERE key >= ? AND key < ?",
                                   hosted._range(WAKE_PREFIX)):
        try:
            rec = _clean(json.loads(value))
        except (TypeError, ValueError):
            rec = None
        out[key[len(WAKE_PREFIX):]] = rec or _new(0)
    return out


def _save(c, session_id: str, rec: dict, now: float) -> None:
    rec["sent"] = [t for t in rec["sent"] if isinstance(t, (int, float)) and t > now - HOUR]
    woken = {k: t for k, t in rec["woken"].items() if isinstance(t, (int, float)) and t > now - KEEP_WOKEN_SECONDS}
    rec["woken"] = dict(sorted(woken.items(), key=lambda kv: kv[1])[-MAX_WOKEN:])
    if len(rec["pending"]) > MAX_PENDING:
        rec["pending"] = dict(sorted(rec["pending"].items(), key=lambda kv: kv[1][3])[-MAX_PENDING:])
    _put(c, WAKE_PREFIX + session_id, rec, HUB, now)


def begin(c, session_id: str, now: float) -> None:
    """Inside hosted.start's transaction: the new session is woken only for posts made from now on."""
    top = c.execute("SELECT COALESCE(MAX(seq), 0) FROM posts").fetchone()[0]
    _save(c, session_id, _new(top), now)


def limits(board: Board):
    """`[dispatch.sessions]` as the hub has it now (the numbers apply on the hub; `enabled` is a node's own switch).
    A table that does not validate leaves the defaults: the dispatcher and the API already report it."""
    from .dispatch import SessionsConfig

    try:
        return SessionsConfig.from_dict((board.s.dispatch or {}).get("sessions", {}))
    except (ValueError, AttributeError):
        return SessionsConfig()


# ---------------------------------------------------------------- who and what


def _agent(conn, name: str) -> Principal | None:
    row = conn.execute("SELECT name, runtime FROM agents WHERE name = ? AND active = 1 AND is_human = 0",
                       (name,)).fetchone()
    return Principal(row["name"], row["runtime"], False) if row else None


def _human(conn) -> Principal | None:
    row = conn.execute("SELECT name, runtime FROM agents WHERE is_human = 1 AND active = 1 LIMIT 1").fetchone()
    return Principal(row["name"], row["runtime"], True) if row else None


def _human_seq(conn, thread_id: int) -> int:
    """The seq of the human's newest post in the thread that the human made (not an automatic one)."""
    return conn.execute(
        f"""SELECT COALESCE(MAX(p.seq), 0) FROM posts p JOIN agents a ON a.name = p.agent AND a.is_human = 1
            WHERE p.thread_id = ? AND {Board.NOT_AUTOMATIC.format(post='p')}""", (thread_id,)).fetchone()[0]


def _entry(row) -> list:
    return [row["thread_id"], row["agent"], bool(row["needs_response"]), row["seq"]]


def _qualifying(board: Board, p: Principal, project: str, ids: list[int]) -> dict[str, list]:
    """Of these posts, the ones a wake may still be for: eligible as when they were found (channel.eligible), and
    not read yet (no session of the agent has acknowledged past them)."""
    out, acked = {}, {}
    for row in channel.eligible(board, p, ids=ids, project=project) if ids else []:
        tid = row["thread_id"]
        if tid not in acked:
            acked[tid] = board.conn.execute(
                "SELECT COALESCE(MAX(last_seq), 0) FROM cursors WHERE agent = ? AND thread_id = ?",
                (p.name, tid)).fetchone()[0]
        if row["seq"] > acked[tid]:
            out[str(row["id"])] = _entry(row)
    return out


def wake_text(agent: str, entries: list[list]) -> str:
    """The whole text of a wake turn. Every part is the server's: channel.render takes counts, thread ids and agent
    names (re-checked against the name rule) and nothing an agent wrote."""
    return channel.render(agent, [channel._Pending(*e) for e in entries])["content"] + NOTE


def _stopped(rec: dict, thread_id: int) -> str | None:
    t = rec["threads"].get(str(thread_id))
    return t.get("stopped") if isinstance(t, dict) else None


def _thread(c, rec: dict, thread_id: int) -> dict:
    t = rec["threads"].get(str(thread_id))
    if not isinstance(t, dict):
        t = rec["threads"][str(thread_id)] = {"count": 0, "human": _human_seq(c, thread_id), "stopped": None,
                                              "limit": None, "alert": None, "tried": None}
    return t


def _free(r: dict) -> bool:
    return r.get("in_flight") is None and not int(r.get("queued") or 0)


# ---------------------------------------------------------------- one pass


def tick(board: Board, node: str | None = None) -> None:
    """Look for new posts and queue the wakes that are due, for the sessions on `node` (all without one). Safe to call
    from any process on the hub's database, as often as wanted; never raises."""
    try:
        _tick(board, node)
    except Exception:
        _complain("hosted sessions: the wake pass failed")


_complained: dict[str, float] = {}


def _complain(what: str) -> None:
    """Log the exception being handled, at most once a minute per message: a pass runs several times a second."""
    now = time.monotonic()
    if now - _complained.get(what, float("-inf")) >= 60:
        _complained[what] = now
        log.exception(what)


def _tick(board: Board, node: str | None) -> None:
    conn = board.conn
    rows, recs = hosted._sessions(conn), _records(conn)
    if not (rows or recs) or board.is_paused():
        return      # no hosted session: two reads that find nothing, and no write
    now, cfg = board.now(), limits(board)
    live = {r["session_id"]: r for r in rows if r.get("state") != "ended"}
    for sid, rec in recs.items():
        if sid not in live:
            _retire(board, sid, now)
    top = conn.execute("SELECT COALESCE(MAX(seq), 0) FROM posts").fetchone()[0]
    for sid, r in live.items():
        if ((node is not None and r.get("node") != node) or r.get("stop")
                or now - hosted._node_seen(conn, r["node"]) > nodes.NODE_STALE_SECONDS):
            continue    # a session nobody can hand a wake to keeps its mark: its posts are found when its node is back
        rec = recs.get(sid)
        if rec is None or _due(rec, r, top, now, cfg):
            rec = _advance(board, sid, cfg, now)
        if rec is not None and (rec["closing"] or any(isinstance(t, dict) and t.get("stopped") and not t.get("alert")
                                                      for t in rec["threads"].values())):
            _tell(board, r, rec, now)


def _due(rec: dict, r: dict, top: int, now: float, cfg) -> bool:
    if top > rec["mark"]:
        return True
    return (_free(r) and now - max(rec["sent"], default=float("-inf")) >= cfg.wake_min_interval_seconds
            and any(not _stopped(rec, e[0]) for e in rec["pending"].values()))


def _advance(board: Board, session_id: str, cfg, now: float) -> dict | None:
    """One session, in one write transaction: take in the posts above its mark, lift the limit of threads the human
    posted in, and queue the wake that is due. Returns the record as stored."""
    with db.write_tx(board.conn) as c:
        r = _get(c, hosted.SESSION_PREFIX + session_id)
        if not isinstance(r, dict) or r.get("state") == "ended" or r.get("stop") or board.is_paused():
            return None
        top = c.execute("SELECT COALESCE(MAX(seq), 0) FROM posts").fetchone()[0]
        rec = _load(c, session_id) or _new(top)     # a session without a record yet starts at the newest post
        rec["grace"] = float(cfg.wake_grace_seconds)
        p = _agent(c, r["agent"])
        if p is None:       # the agent's identity was revoked: nothing wakes its session
            rec |= {"mark": top, "pending": {}}
            _save(c, session_id, rec, now)
            return rec
        if top > rec["mark"]:
            for row in channel.eligible(board, p, after=rec["mark"], top=top, project=r["project"]):
                rec["pending"][str(row["id"])] = _entry(row)    # by post id: a post revised again counts once
            rec["mark"] = top
            for tid, t in list(rec["threads"].items()):
                if not isinstance(t, dict) or _human_seq(c, int(tid)) > int(t.get("human") or 0):
                    # The human posted there: the count starts over, the item that reported the stop is closed, and
                    # a stop for the hourly maximum clears that hour (the human stepping in is what the limit asks).
                    if isinstance(t, dict) and t.get("alert"):
                        rec["closing"].append(t["alert"])
                    if isinstance(t, dict) and t.get("stopped") == "hour":
                        rec["sent"] = []
                    del rec["threads"][tid]
        sent = [t for t in rec["sent"] if isinstance(t, (int, float)) and t > now - HOUR]
        if (_free(r) and now - max(sent, default=float("-inf")) >= cfg.wake_min_interval_seconds
                and any(not _stopped(rec, e[0]) for e in rec["pending"].values())):
            # Checked again now, not only when found: the batch may have waited (a turn, the interval, a pause).
            rec["pending"] = _qualifying(board, p, r["project"], [int(k) for k in rec["pending"]])
            batch = {k: e for k, e in rec["pending"].items() if not _stopped(rec, e[0])}
            for tid in sorted({e[0] for e in batch.values()}):
                t = _thread(c, rec, tid)
                if t["count"] >= cfg.max_wakes_without_human:
                    t |= {"stopped": "thread", "limit": cfg.max_wakes_without_human}
            batch = {k: e for k, e in batch.items() if not _stopped(rec, e[0])}
            if batch and len(sent) >= cfg.max_wakes_per_hour:
                for tid in sorted({e[0] for e in batch.values()}):
                    _thread(c, rec, tid).update(stopped="hour", limit=cfg.max_wakes_per_hour)
                batch = {}
            if batch:
                m = hosted._enqueue(c, r, "wake", wake_text(r["agent"], list(batch.values())), HUB, now)
                m["posts"] = sorted(int(k) for k in batch)      # checked again when the node is handed the message
                _put(c, hosted._msg_key(session_id, m["n"]), m, HUB, now)
                rec["sent"].append(now)
                for tid in {e[0] for e in batch.values()}:
                    _thread(c, rec, tid)["count"] += 1
                for k in batch:
                    rec["woken"][k] = now
                    del rec["pending"][k]
                log.info("session %s: wake %s queued for %d post(s)", session_id, m["n"], len(batch))
        _save(c, session_id, rec, now)
        return rec


# ---------------------------------------------------------------- the node is handed a wake


def hand_over(board: Board, node: str) -> None:
    """Called where the hub lists a node's sessions for it (hosted.node_sessions): queue what is due, then check the
    wake each session is about to be handed. Never raises: the node's fetch must not fail because of a wake."""
    tick(board, node)
    try:
        for r in hosted._sessions(board.conn):
            if r.get("node") == node and r.get("state") != "ended" and int(r.get("queued") or 0):
                _recheck(board, r)
    except Exception:
        _complain("hosted sessions: could not re-check a queued wake")


def _next_queued(conn, session_id: str) -> dict | None:
    """The message the node is handed next: the session's first queued one (the only one listed with its text)."""
    return next((m for m in hosted._messages(conn, session_id) if m.get("state") == "queued"), None)


def _recheck(board: Board, r: dict) -> None:
    """Eligibility at the moment of hand-over. A wake is for the posts recorded on it; those that stopped qualifying
    since it was queued (author revoked, sealed again, read meanwhile) are taken off it, and with none left the wake
    is not sent at all. The first hand-over is also when its grace period starts anew (`holds`)."""
    conn, sid = board.conn, r["session_id"]
    m = _next_queued(conn, sid)
    if m is None or m.get("kind") != "wake" or not isinstance(m.get("posts"), list):
        return
    p = _agent(conn, r["agent"])
    keep = _qualifying(board, p, r["project"], m["posts"]) if p is not None else {}
    if m.get("handed_at") is not None and sorted(int(k) for k in keep) == m["posts"]:
        return      # nothing changed since the last fetch: no write
    now = board.now()
    with db.write_tx(conn) as c:
        r, m = _get(c, hosted.SESSION_PREFIX + sid), _next_queued(c, sid)
        if (not isinstance(r, dict) or r.get("state") == "ended" or m is None or m.get("kind") != "wake"
                or not isinstance(m.get("posts"), list)):
            return
        rec = _load(c, sid) or _new(c.execute("SELECT COALESCE(MAX(seq), 0) FROM posts").fetchone()[0])
        keep = _qualifying(board, p, r["project"], m["posts"]) if p is not None else {}
        for k in (str(i) for i in m["posts"]):
            if k not in keep:
                rec["woken"].pop(k, None)   # no wake reached anyone for it: the dispatcher is not held back
            elif m.get("handed_at") is None:
                rec["woken"][k] = now       # handed now: the grace period counts from here
        if keep:
            m |= {"posts": sorted(int(k) for k in keep), "text": wake_text(r["agent"], list(keep.values())),
                  "handed_at": m.get("handed_at") or now, "updated_at": now}
        else:
            m |= {"state": "failed", "ok": False, "detail": NOT_SENT, "text": "", "ended_at": now, "updated_at": now}
            r |= {"queued": max(0, int(r.get("queued") or 0) - 1), "updated_at": now}
            _put(c, hosted.SESSION_PREFIX + sid, r, HUB, now)
        _put(c, hosted._msg_key(sid, m["n"]), m, HUB, now)
        _save(c, sid, rec, now)


# ---------------------------------------------------------------- the dispatcher's side


def holds(board_or_conn: Any, agent: str, project: Any, post_id: Any, now: float) -> bool:
    """A hosted session has this request, so nothing else should be started for it: a session of `agent` in `project`
    that has not ended, on a node seen within nodes.NODE_STALE_SECONDS, and either a wake covering the post was
    queued or handed less than `wake_grace_seconds` ago, or a session of the agent was handed the request (its
    delivery receipt) at or after such a wake. A session that was never woken for the post, or was woken and did not
    read it within the grace, holds nothing."""
    conn = getattr(board_or_conn, "conn", board_or_conn)
    if type(post_id) is not int or not isinstance(project, str):
        return False
    project = project.rstrip("/") or "/"
    for r in hosted._sessions(conn):
        if (r.get("agent") != agent or r.get("project") != project or r.get("state") == "ended" or r.get("stop")
                or now - hosted._node_seen(conn, r.get("node") or "") > nodes.NODE_STALE_SECONDS):
            continue
        rec = _load(conn, r["session_id"])
        at = rec["woken"].get(str(post_id)) if rec else None
        if not isinstance(at, (int, float)):
            continue
        if now - at < rec["grace"]:
            return True
        from . import delivery
        if delivery.read_since(conn, post_id, agent, at):
            return True
    return False


def turn_projects(board_or_conn: Any, now: float) -> set[str]:
    """The projects in which a hosted session has a turn running now (Dispatcher._busy_dirs): the hub saw a message
    reach the session's process and has not seen its turn end, and the node is not lost (hosted._lost: silent for
    nodes.NODE_LOST_SECONDS, or no longer hosting sessions; the same test that fails such a turn on the human's
    next read)."""
    conn = getattr(board_or_conn, "conn", board_or_conn)
    return {r["project"] for r in hosted._sessions(conn)
            if r.get("state") != "ended" and r.get("in_flight") is not None and isinstance(r.get("project"), str)
            and not hosted._lost(conn, r, now)}


# ---------------------------------------------------------------- telling the human


def _question(session_id: str, agent: str, thread_id: int, limit: int) -> dict:
    go = {"id": "go-on", "label": f"Keep waking {agent} for thread #{thread_id}"[:200], "outcome": "answered",
          "description": (f"Your answer is a post in this thread, which lifts the limit: the session is woken again "
                          f"for posts addressed to {agent} here, the ones that waited included, until the limit is "
                          "reached again and this item comes back. Costs agent turns (tokens).")}
    close = {"id": "close-session", "label": "I'll close the session first", "outcome": "answered",
             "description": (f"Run `board session close {session_id}`, then answer here. A closed session is never "
                             f"woken; later requests to {agent} in that project are handled as without a hosted "
                             "session. Costs nothing. Answering without closing it lifts the limit like the other "
                             "option.")}
    return {"question": f"{agent}'s hosted session reached its wake limit on thread #{thread_id}. Keep waking it?",
            "context": (f"The limit is {limit}; it exists so that agents cannot keep each other's sessions taking "
                        "turns without you looking in. Recommended: keep waking it, since the limit comes back."),
            "options": [go, close], "recommended_option_id": "go-on"}


def _tell(board: Board, r: dict, rec: dict, now: float) -> None:
    """Outside the pass's transaction: close the items of threads the human posted in, and post the ONE Needs-you
    item of each newly stopped thread. A failure here is logged and tried again; it never undoes a stop."""
    human = _human(board.conn)
    if human is None:
        return
    sid = r["session_id"]
    for post_id in list(rec["closing"]):
        try:
            _close(board, human, sid, post_id, now)
        except Exception:
            log.exception("session %s: could not close wake-limit item %s", sid, post_id)
    for tid, t in sorted(rec["threads"].items()):
        if (isinstance(t, dict) and t.get("stopped") and not t.get("alert")
                and now - float(t.get("tried") or float("-inf")) >= ALERT_RETRY_SECONDS):
            try:
                _alert(board, human, r, int(tid), now)
            except Exception as e:
                log.warning("session %s: could not post the wake-limit item on thread %s: %s", sid, tid, e)
                with db.write_tx(board.conn) as c:
                    cur = _load(c, sid)
                    if cur and isinstance(cur["threads"].get(tid), dict) and not cur["threads"][tid].get("alert"):
                        cur["threads"][tid]["tried"] = now
                        _save(c, sid, cur, now)


def _alert(board: Board, human: Principal, r: dict, thread_id: int, now: float) -> int:
    from . import human_actions, issues
    from .autorecover import POST_PREFIX, _insert

    sid, agent, key = r["session_id"], r["agent"], str(thread_id)
    t = (_load(board.conn, sid) or _new(0))["threads"].get(key) or {}
    reason, limit = t.get("stopped"), int(t.get("limit") or 0)
    if reason not in WHY or not channel.NAME_RE.match(agent):
        raise Conflict("nothing to report")

    def check() -> None:
        if board.is_paused():
            raise Conflict("the board is paused")
        cur = (_load(board.conn, sid) or _new(0))["threads"].get(key)
        if not isinstance(cur, dict) or cur.get("stopped") != reason or cur.get("alert"):
            raise Conflict("the wake limit changed meanwhile")

    def hook(post: dict) -> None:
        _insert(board.conn, POST_PREFIX + str(post["id"]),
                {"kind": MARKER_KIND, "thread_id": thread_id, "session_id": sid, "agent": agent}, now)
        cur = _load(board.conn, sid)
        cur["threads"][key]["alert"] = post["id"]
        _save(board.conn, sid, cur, now)

    question = _question(sid, agent, thread_id, limit)
    issues._question(board, question)
    body = HEADER.format(sid=sid, agent=agent, thread=thread_id,
                         why=WHY[reason].format(limit=limit, thread=thread_id))
    post, _ = human_actions.post_as_human(board, human, thread_id=thread_id, body=body, type="question", to=[],
                                          needs_response=True, decision_question=question, post_check=check,
                                          post_hook=hook)
    log.warning("session %s of %s reached its wake limit (%s) on thread %s; told the human in post %s", sid, agent,
                reason, thread_id, post["id"])
    return post["id"]


def _close(board: Board, human: Principal, session_id: str, post_id: Any, now: float) -> None:
    """Take a wake-limit item out of Needs you (if it is still there) and off the record's closing list."""
    from . import awaiting
    from .autorecover import POST_PREFIX, _update

    human_sid = board.human_session(human)
    with db.write_tx(board.conn) as c:
        marker = _get(c, POST_PREFIX + str(post_id)) if type(post_id) is int else None
        if (isinstance(marker, dict) and marker.get("kind") == MARKER_KIND and marker.get("closed_at") is None
                and c.execute(f"SELECT 1 FROM posts p WHERE p.id = ? AND {Board.NEEDS_YOU}", (post_id,)).fetchone()):
            c.execute("""INSERT INTO attention_resolutions(post_id, resolved_by, session_id, reason,
                         evidence_post_ids, resolved_at) VALUES (?, ?, ?, ?, '[]', ?)""",
                      (post_id, human.name, human_sid, RESOLVED, now))
            _update(c, POST_PREFIX + str(post_id), marker | {"closed_at": now}, now)
            awaiting._bump(c, post_id, now)
        rec = _load(c, session_id)
        if rec is not None:
            rec["closing"] = [x for x in rec["closing"] if x != post_id]
            _save(c, session_id, rec, now)


def _retire(board: Board, session_id: str, now: float) -> None:
    """The session ended (or its record is gone): its wake-limit items are closed and its watcher state dropped."""
    rec = _load(board.conn, session_id)
    human = _human(board.conn)
    if rec is not None and human is not None:
        for post_id in rec["closing"] + [t.get("alert") for t in rec["threads"].values()
                                         if isinstance(t, dict) and t.get("alert")]:
            try:
                _close(board, human, session_id, post_id, now)
            except Exception:
                log.exception("session %s: could not close wake-limit item %s", session_id, post_id)
    with db.write_tx(board.conn) as c:
        c.execute("DELETE FROM board_state WHERE key = ?", (WAKE_PREFIX + session_id,))
