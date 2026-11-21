"""Waking a hosted session for board posts addressed to its agent (DESIGN_NOTES "Session host (PR 3)").

A hosted session (hosted.py) is a standing conversation of one agent in one project. Starting it is the human's
standing OK for it to take a turn when a post addressed to that agent appears in a thread of that project: the hub
then queues one `wake` message, the only message kind that does not come from the human.

- *Text.* Built here from server-stamped data only: counts, thread ids and agent names, rendered by channel.render
  (the Claude Code channel push), plus a fixed note. Never a post body, thread title, summary, task title or ref.
- *Who can cause one.* channel.eligible: the post is visible to the agent under Board.VISIBLE, addressed to it,
  written by someone else, by an active identity; and its thread is in the session's project. Only a NEW post counts
  (by post id), or one that was sealed when it was looked at and is not now. A revision of a post already looked at
  (request progress, a closed item: a new seq on the same id) wakes nobody. The post of a managed continuation
  (the `continuations` table) is the dispatcher's verified path and wakes nobody either. Nothing is queued while the
  board is paused; the posts are found after the unpause.
- *Mechanism.* A watcher over the posts table, with a mark per session persisted in `board_state`
  (`host.wake.<uuid>`), so a post counts however it was written (HTTP, a `board mcp` process, the gateway, an
  unseal). `tick` is driven by the hub call a node sits in whenever it is idle (hosted.node_sessions re-reads the
  database every 0.25 s) and by each dispatcher pass, just before it decides what to launch. It runs in no post's
  write path, so it cannot block or fail a post.
- *One at a time.* Posts wait in the record (`pending`) until the session has no turn and no queued message and the
  interval has passed; then they go as ONE wake. They are checked again at that moment, and once more in the
  transaction that shows the node the message (`listing`): a wake whose posts all stopped qualifying, or were read
  meanwhile, is dropped, and one that a message of the human's has caught up with steps back behind it.
- *Limits* (`[dispatch.sessions]`): `wake_min_interval_seconds` between wakes of a session, `max_wakes_per_hour` per
  session, `max_wakes_without_human` per session and thread since the human's last NEW post in that thread. At a
  maximum the posts wait and the human gets a fixed-text Needs-you item (marked automatic, like the undelivered
  alert): one per thread for a thread's own maximum, ONE per session for the hourly one, and few in all (capped per
  pass and per day as delivery.py caps its alerts). A new post of the human's in a stopped thread lifts the stop. It
  lifts nothing else: the interval and the rolling hour always hold, so after an hourly stop the posts go once the
  hour has room, with no second item for that same full hour.
- *One run per checkout.* No wake is queued or handed while a dispatched run is using the directory a run for the
  session's project uses (`_occupied`): the dispatcher's own rule, the other way round.
- *The dispatcher* (`holds`, `reserve`). While a wake covering a request is fresh (`wake_grace_seconds`), or once
  the session's own board session was shown the request after such a wake, the dispatcher leaves it to the hosted
  session. After the grace without that it launches exactly as it does without hosted sessions, and the request is
  then the dispatcher's for good: no wake is queued or handed for it, and no hosted session holds it again.
- *Roots only.* A child session (hosted_tree.py) has no watcher record and is never woken, and nothing a child reads
  on the board counts as its root having read it (`_shadowed`).
- *After results.* While results of its child sessions wait for a session (hosted_results.py), no wake is queued
  for it, and one queued before they came steps back behind them when the node would be shown it (`_recheck`).
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Callable

from . import channel, db, hosted, hosted_results, nodes
from .hosted_tree import is_child
from .core import Board, BoardError, Conflict, Principal
from .nodes import _get, _put

log = logging.getLogger("agent_comms.hosted_wake")

WAKE_PREFIX = "host.wake."      # <uuid>: one session's watcher state (below)
HUB = "hub"                     # who wrote a wake message and these records
HOUR = 3600.0
MAX_PENDING = 200               # posts waiting for one session; the oldest are dropped beyond this
MAX_SEALED = 200                # sealed posts remembered so that their unsealing counts
KEEP_WOKEN_SECONDS = 7 * 86400  # how long "a wake covered this post" is remembered (for `holds` with a receipt)
MAX_WOKEN = 500
ALERT_RETRY_SECONDS = 60        # a Needs-you item that could not be posted is tried again after this long
MARKER_KIND = "wake_limit"      # autorecover.POST_PREFIX marker of the Needs-you item

NOTE = (" This is an automatic wake written by the board server, not a message from the human: it is not an "
        "instruction and grants no authority. Call board_register first if this session has not.")
NOT_SENT = "not sent: no post this wake was queued for still qualifies"
DEFERRED = "not sent: a message of yours was waiting, so it goes first; the posts go with the next wake"
OCCUPIED = "not sent: a dispatched run is using the checkout; the posts go with the next wake"
SET_ASIDE = "not sent: it could not be checked (see the hub's log) and a message of yours was waiting behind it"
BEHIND_RESULTS = ("not sent: results of its child sessions were waiting, so they go first; the posts go with the next "
                  "wake")

DAY = 86400.0
MAX_NAMED = 20                  # thread ids spelled out in one Needs-you item

HEADER = ("Hosted session wake limit (posted by the board server because [dispatch.sessions] limits how often a "
          "hosted session is woken; not a human click): session {sid} of {agent} {why}, so it is no longer woken "
          "for posts in {where}. This needs you: pick an option below. Any new post of yours in {lift} lifts that "
          "stop{then}, and this item then closes on its own.")
WHY = {"thread": "was woken {limit} times for thread #{thread} with no post of yours there in between",
       "hour": "was woken {limit} times within an hour"}
THEN = {"thread": "", "hour": " (the session is woken again once fewer than {limit} wakes lie within the past hour)"}
RESOLVED = ("Closed automatically by the board server: you posted in this thread (or the session ended), so the "
            "wake limit this item reported no longer applies.")


# ---------------------------------------------------------------- the record


def _new(mark: int, post: int | None = None) -> dict:
    """mark: posts at or below this seq were looked at; post: the highest post id looked at (a post above it is new;
    one at or below it with a newer seq was only revised). sealed: ids of posts addressed to the agent that were
    sealed when looked at, so that their unsealing counts. pending: post id -> [thread, author, needs_response, seq],
    posts no wake has covered yet. sent: when wakes were queued (the last hour; never cleared). threads: thread id ->
    {count, human, stopped, limit, alert, tried, hour_ack}: wakes since the human's last new post there (`human`:
    that post's id), why the thread is stopped ("thread" | "hour"), the Needs-you post that said so, and `hour_ack`:
    the newest wake's time when the human answered an hourly stop (no second item while that is still the newest).
    woken: post id -> when the wake covering it was QUEUED (what read evidence is compared with); handed: post
    id -> when the node was first shown that wake (the grace period counts from the later of the two). read: post
    id -> when the board last showed that post, at or after the wake was queued, to a board session of the agent in
    the session's project (`handed()`).
    claimed: post id -> when the dispatcher reserved a launch for it (no wake for it after that). closing: Needs-you
    posts to close. grace: wake_grace_seconds as of the last write (so `holds` needs no settings). hour_ack: the
    newest wake's time when the human last answered an hourly stop (while that is still the newest wake and the
    hour is full, posts are held with no stop and no item). items: when Needs-you items were posted for this
    session (the last day)."""
    return {"mark": mark, "post": post, "sealed": [], "pending": {}, "sent": [], "threads": {}, "woken": {},
            "handed": {}, "read": {}, "claimed": {}, "closing": [], "grace": 120.0, "hour_ack": None, "items": []}


def _clean(rec: Any) -> dict | None:
    if not isinstance(rec, dict) or type(rec.get("mark")) is not int:
        return None
    out = _new(rec["mark"], rec["post"] if type(rec.get("post")) is int else None)
    for key, kind in (("pending", dict), ("threads", dict), ("woken", dict), ("handed", dict), ("read", dict),
                      ("claimed", dict), ("sent", list), ("closing", list), ("sealed", list), ("items", list)):
        if isinstance(rec.get(key), kind):
            out[key] = rec[key]
    if isinstance(rec.get("hour_ack"), (int, float)) and not isinstance(rec.get("hour_ack"), bool):
        out["hour_ack"] = rec["hour_ack"]
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


def _recent(times: dict | list, now: float, keep: float, most: int | None = None):
    """Only the entries newer than `keep` seconds (and the newest `most` of a table)."""
    if isinstance(times, list):
        return [t for t in times if isinstance(t, (int, float)) and not isinstance(t, bool) and t > now - keep]
    kept = {k: t for k, t in times.items() if isinstance(t, (int, float)) and t > now - keep}
    return dict(sorted(kept.items(), key=lambda kv: kv[1])[-(most or len(kept) or 1):])


def _save(c, session_id: str, rec: dict, now: float) -> None:
    rec["sent"] = _recent(rec["sent"], now, HOUR)
    rec["items"] = _recent(rec["items"], now, DAY)
    rec["woken"] = _recent(rec["woken"], now, KEEP_WOKEN_SECONDS, MAX_WOKEN)
    rec["handed"] = _recent(rec["handed"], now, KEEP_WOKEN_SECONDS, MAX_WOKEN)
    rec["read"] = _recent(rec["read"], now, KEEP_WOKEN_SECONDS, MAX_WOKEN)
    rec["claimed"] = _recent(rec["claimed"], now, KEEP_WOKEN_SECONDS, MAX_WOKEN)
    if len(rec["pending"]) > MAX_PENDING:
        rec["pending"] = dict(sorted(rec["pending"].items(), key=lambda kv: kv[1][3])[-MAX_PENDING:])
    _put(c, WAKE_PREFIX + session_id, rec, HUB, now)


def _tops(c) -> tuple[int, int]:
    return tuple(c.execute("SELECT COALESCE(MAX(seq), 0), COALESCE(MAX(id), 0) FROM posts").fetchone())


def begin(c, session_id: str, now: float) -> None:
    """Inside hosted.start's transaction: the new session is woken only for posts made from now on."""
    _save(c, session_id, _new(*_tops(c)), now)


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


def _human_post(conn, thread_id: int) -> int:
    """The id of the newest post in the thread that the human made: not an automatic one (Board.NOT_AUTOMATIC),
    and not a request the dispatcher routed to another agent in the human's name (access.route_successor, which the
    server marks under access.ROUTED_PREFIX in the transaction that posts it). An id, not a seq: an agent's progress
    on one of the human's requests gives that post a new seq, and that is not the human posting."""
    from .access import ROUTED_PREFIX
    return conn.execute(
        f"""SELECT COALESCE(MAX(p.id), 0) FROM posts p JOIN agents a ON a.name = p.agent AND a.is_human = 1
            WHERE p.thread_id = ? AND {Board.NOT_AUTOMATIC.format(post='p')}
              AND NOT EXISTS (SELECT 1 FROM board_state br WHERE br.key = ? || p.id)""",
        (thread_id, ROUTED_PREFIX)).fetchone()[0]


def _managed(conn, post_id: int) -> bool:
    """The post belongs to a managed continuation (workstreams.get_for_post, the dispatcher's own test): that work
    is delivered on the dispatcher's verified path, which a hosted hold does not stop, so no wake is for it."""
    return conn.execute("SELECT 1 FROM continuations WHERE post_id = ?", (post_id,)).fetchone() is not None


def _entry(row) -> list:
    return [row["thread_id"], row["agent"], bool(row["needs_response"]), row["seq"]]


def _qualifying(board: Board, p: Principal, project: str, ids: list[int], rec: dict) -> dict[str, list]:
    """Of these posts, the ones a wake may still be for: eligible as when they were found (channel.eligible), not a
    managed continuation's, not read yet (no session of the agent has acknowledged past them), and not reserved by
    the dispatcher for a run of its own (`reserve`)."""
    out, acked = {}, {}
    for row in channel.eligible(board, p, ids=ids, project=project) if ids else []:
        tid = row["thread_id"]
        if tid not in acked:
            acked[tid] = board.conn.execute(
                "SELECT COALESCE(MAX(last_seq), 0) FROM cursors WHERE agent = ? AND thread_id = ?",
                (p.name, tid)).fetchone()[0]
        if (row["seq"] > acked[tid] and str(row["id"]) not in rec["claimed"]
                and not _managed(board.conn, row["id"])):
            out[str(row["id"])] = _entry(row)
    return out


def wake_text(agent: str, entries: list[list]) -> str:
    """The whole text of a wake turn. Every part is the server's: channel.render takes counts, thread ids and agent
    names (re-checked against the name rule) and nothing an agent wrote."""
    return channel.render(agent, [channel._Pending(*e) for e in entries])["content"] + NOTE


def _stopped(rec: dict, thread_id: int) -> str | None:
    t = rec["threads"].get(str(thread_id))
    return t.get("stopped") if isinstance(t, dict) else None


def _fresh(human: int) -> dict:
    return {"count": 0, "human": human, "stopped": None, "limit": None, "alert": None, "tried": None}


def _thread(c, rec: dict, thread_id: int) -> dict:
    t = rec["threads"].get(str(thread_id))
    if not isinstance(t, dict):
        t = rec["threads"][str(thread_id)] = _fresh(_human_post(c, thread_id))
    return t


def _free(r: dict) -> bool:
    return r.get("in_flight") is None and not int(r.get("queued") or 0)


def _waiting(rec: dict, cfg, now: float) -> bool:
    """Some waiting post can be acted on now: sent, or its thread stopped. Not while the hour is full and the human
    already answered for it (`hour_ack`): then everything is held, silently, until the hour has room."""
    sent = _recent(rec["sent"], now, HOUR)
    if len(sent) >= cfg.max_wakes_per_hour and rec.get("hour_ack") == max(sent, default=None):
        return False
    return any(not _stopped(rec, e[0]) for e in rec["pending"].values())


def _run_dir(board: Board, project: str) -> str:
    """Where the dispatcher runs something for this project on the hub's settings (DispatchConfig.run_dir, what
    Dispatcher._busy_dirs asks for a hosted session's turn). Settings that do not validate leave the project path."""
    from .dispatch import DispatchConfig

    key = (id(board), board.settings_generation, id(board.s.dispatch))
    if _run_dir_cache.get("key") != key:
        try:
            cfg = DispatchConfig.from_dict(board.s.dispatch, settings=board.s)
        except Exception:
            cfg = None
        _run_dir_cache.update(key=key, cfg=cfg)
    cfg = _run_dir_cache["cfg"]
    return cfg.run_dir(project) if cfg is not None else project


_run_dir_cache: dict = {}


def _occupied(board: Board, r: dict) -> bool:
    """A dispatched run that may still be going is using the directory a run for this session's project uses: the
    dispatcher's own rule of one run per run directory (dispatch.occupied_run_dirs, the directories its `_busy_dirs`
    holds), applied the other way round, so that no automatic turn starts beside a run in the same checkout. A run
    in its own parallel worktree has another directory and does not count. Only wakes ask this: a message of the
    human's is the human's call."""
    from . import dispatch

    return os.path.realpath(_run_dir(board, r["project"])) in dispatch.occupied_run_dirs(board)


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
    from .delivery import ALERTS_PER_PASS

    now, cfg, budget = board.now(), limits(board), [ALERTS_PER_PASS]    # Needs-you items this pass may still post
    live = {r["session_id"]: r for r in rows if r.get("state") != "ended" and not is_child(r)}   # roots only
    for sid, rec in recs.items():
        if sid not in live:
            _retire(board, sid, now)
    top = conn.execute("SELECT COALESCE(MAX(seq), 0) FROM posts").fetchone()[0]
    for sid, r in live.items():
        if ((node is not None and r.get("node") != node) or r.get("stop")
                or now - hosted._node_seen(conn, r["node"]) > nodes.NODE_STALE_SECONDS):
            continue    # a session nobody can hand a wake to keeps its mark: its posts are found when its node is back
        rec = recs.get(sid)
        if rec is None or _due(board, rec, r, top, now, cfg):
            rec = _advance(board, sid, cfg, now)
        if rec is not None and (rec["closing"] or any(isinstance(t, dict) and t.get("stopped") and not t.get("alert")
                                                      for t in rec["threads"].values())):
            _tell(board, r, rec, now, budget)


def _due(board: Board, rec: dict, r: dict, top: int, now: float, cfg) -> bool:
    if top > rec["mark"]:
        return True
    return (_free(r) and now - max(rec["sent"], default=float("-inf")) >= cfg.wake_min_interval_seconds
            and _waiting(rec, cfg, now) and not _occupied(board, r))


def _look(board: Board, c, rec: dict, r: dict, p: Principal, top: int, top_id: int) -> None:
    """Take in what is new above the record's marks: a post made since the last look (by id), or one that was sealed
    then and is visible now. A post already looked at that only got a new seq (request progress, a closed item, a
    finalized decision) is not new: it neither wakes anyone nor counts as anybody posting."""
    was_sealed = {i for i in rec["sealed"] if type(i) is int}
    for row in channel.eligible(board, p, after=rec["mark"], top=top, project=r["project"]):
        if ((row["id"] > rec["post"] or row["id"] in was_sealed) and str(row["id"]) not in rec["claimed"]
                and not _managed(c, row["id"])):
            rec["pending"][str(row["id"])] = _entry(row)
    rec["sealed"] = sorted(row["id"] for row in c.execute(
        """SELECT p.id FROM posts p WHERE p.sealed = 1 AND p.agent != :me
           AND (p.id > :post OR p.id IN (SELECT value FROM json_each(:old)))
           AND EXISTS (SELECT 1 FROM json_each(p.to_agents) j WHERE j.value = :me)
           AND p.thread_id IN (SELECT id FROM threads WHERE project = :project)""",
        {"me": p.name, "post": rec["post"], "old": json.dumps(sorted(was_sealed)),
         "project": r["project"]}))[-MAX_SEALED:]
    rec |= {"mark": top, "post": max(rec["post"], top_id)}


def _lift(c, rec: dict, last: float | None) -> None:
    """A NEW post of the human's in a thread lifts that thread's stop and starts its count over; the item that
    reported the stop is closed. Nothing else is lifted: `sent` stays, so the interval and the rolling hour hold.
    The hourly maximum is the session's, and so is its stop: the human's post in any thread it stopped lifts it for
    all of them, and the record remembers which wake was the newest then (`hour_ack`): while that hour is still
    full, posts are held without another stop or item."""
    def drop(tid: str, t: Any) -> None:
        if isinstance(t, dict) and t.get("alert") and t["alert"] not in rec["closing"]:
            rec["closing"].append(t["alert"])
        del rec["threads"][tid]

    hourly = False
    for tid, t in list(rec["threads"].items()):
        if not isinstance(t, dict) or _human_post(c, int(tid)) > int(t.get("human") or 0):
            hourly = hourly or (isinstance(t, dict) and t.get("stopped") == "hour")
            drop(tid, t)
    if hourly:
        rec["hour_ack"] = last
        for tid, t in list(rec["threads"].items()):
            if isinstance(t, dict) and t.get("stopped") == "hour":
                drop(tid, t)


def _advance(board: Board, session_id: str, cfg, now: float) -> dict | None:
    """One session, in one write transaction: take in the posts above its marks, lift the stop of threads the human
    posted in, and queue the wake that is due. Returns the record as stored."""
    with db.write_tx(board.conn) as c:
        r = _get(c, hosted.SESSION_PREFIX + session_id)
        if (not isinstance(r, dict) or r.get("state") == "ended" or r.get("stop") or board.is_paused()
                or is_child(r)):
            return None
        top, top_id = _tops(c)
        rec = _load(c, session_id) or _new(top, top_id)     # a session without a record yet starts at the newest post
        if rec["post"] is None:
            rec["post"] = top_id
        rec["grace"] = float(cfg.wake_grace_seconds)
        p = _agent(c, r["agent"])
        if p is None:       # the agent's identity was revoked: nothing wakes its session
            rec |= {"mark": top, "post": max(rec["post"], top_id), "pending": {}}
            _save(c, session_id, rec, now)
            return rec
        sent = _recent(rec["sent"], now, HOUR)
        last = max(sent, default=None)
        if top > rec["mark"]:
            _look(board, c, rec, r, p, top, top_id)
            _lift(c, rec, last)
        if (_free(r) and (last is None or now - last >= cfg.wake_min_interval_seconds)
                and _waiting(rec, cfg, now) and not _occupied(board, r)
                and not hosted_results.waiting(board, c, session_id)):     # a child's results go before a wake
            # Checked again now, not only when found: the batch may have waited (a turn, the interval, a pause).
            rec["pending"] = _qualifying(board, p, r["project"], [int(k) for k in rec["pending"]], rec)
            batch = {k: e for k, e in rec["pending"].items() if not _stopped(rec, e[0])}
            for tid in sorted({e[0] for e in batch.values()}):
                t = _thread(c, rec, tid)
                if t["count"] >= cfg.max_wakes_without_human:
                    t |= {"stopped": "thread", "limit": cfg.max_wakes_without_human}
            batch = {k: e for k, e in batch.items() if not _stopped(rec, e[0])}
            if batch and len(sent) >= cfg.max_wakes_per_hour:
                # One hourly stop per session, and so one item: a thread the full hour catches later joins the item
                # that is already out (`_tell` posts one for all the threads that have none yet).
                out = next((t["alert"] for t in rec["threads"].values()
                            if isinstance(t, dict) and t.get("stopped") == "hour" and t.get("alert")), None)
                for tid in sorted({e[0] for e in batch.values()}):
                    _thread(c, rec, tid).update(stopped="hour", limit=cfg.max_wakes_per_hour, alert=out)
                batch = {}
            if batch:
                m = hosted._enqueue(c, r, "wake", wake_text(r["agent"], list(batch.values())), HUB, now)
                # Checked again when the node is shown the message; the threads are what a deferral counts back.
                m |= {"posts": sorted(int(k) for k in batch), "threads": sorted({e[0] for e in batch.values()})}
                _put(c, hosted._msg_key(session_id, m["n"]), m, HUB, now)
                rec["sent"].append(now)
                for tid in m["threads"]:
                    _thread(c, rec, tid)["count"] += 1
                for k in batch:
                    rec["woken"][k] = now
                    del rec["pending"][k]
                log.info("session %s: wake %s queued for %d post(s)", session_id, m["n"], len(batch))
        _save(c, session_id, rec, now)
        return rec


# ---------------------------------------------------------------- the node is shown a wake


def listing(board: Board, node: str, build: Callable[[], dict], due: Callable[[Any], bool] | None = None,
            deliver: Callable[[Any, float], None] | None = None) -> dict:
    """hosted.node_sessions' answer (`build` reads it from the connection), made so that a wake the node is shown
    with its text was validated in the very transaction that showed it: queue what is due, then, in one read
    snapshot, look at the wake each session would be handed next. If each is already handed and still stands, the
    answer is built in that snapshot. Otherwise the re-check (`_recheck`) and the answer share one write transaction.
    `due(conn)` / `deliver(c, now)` (hosted_results.py): results wait for a free session of this node; then the
    write transaction is taken too. Whenever it is taken, after the re-check (a wake that waited may step back
    behind results, which frees its session) the waiting results become a turn in it, so that turn is shown in the
    transaction that made it.
    `build` checks the pause itself, inside either transaction, and its refusals pass through. A failure of the
    wake code never fails the node's fetch and never keeps a message of the human's from it (`_fallback`)."""
    tick(board, node)
    conn = board.conn
    conn.execute("BEGIN")
    try:
        try:
            todo = _unsettled(board, node)
        except Exception:
            _complain("hosted sessions: could not check the queued wakes")
            todo = None
        results = due is not None and deliver is not None and due(conn)
        if todo == [] and not results:
            return build()
    finally:
        conn.execute("ROLLBACK")
    if todo is not None:
        try:
            with db.write_tx(conn) as c:
                now = board.now()
                # Worked out again here, on what is stored now: a wake queued since the snapshot above (by a
                # dispatcher pass, for any of this node's sessions) is re-checked too before the answer can show it.
                for sid in _unsettled(board, node):
                    _recheck(board, c, sid, now)
                if deliver is not None:
                    deliver(c, now)     # never raises; checks everything again inside this transaction
                return build()
        except BoardError:
            raise
        except Exception:
            _complain("hosted sessions: could not re-check a queued wake")
    return _fallback(board, node, build)


def _fallback(board: Board, node: str, build: Callable[[], dict]) -> dict:
    """The wake code failed (its transaction, if any, was rolled back). The node still gets its answer, with no
    wake's text, so no unchecked wake is handed. And a message of the human's never waits behind such a wake: only
    a session's first queued message carries its text, so a wake that was never handed and has a message of the
    human's behind it is set aside here (settled, not sent), with nothing but the session's own message records.
    That wake never ran, so its posts must not stay covered: `_set_aside` takes them back into `pending`, where the
    ordinary pass re-qualifies them after the human's turn; and if even that fails, the session's watcher record is
    dropped, which leaves nothing covered (the next pass starts a new record, and those posts are the dispatcher's).
    The answer is built inside the same transaction, so a fetch refused there (the pause) sets nothing aside."""
    try:
        with db.write_tx(board.conn) as c:
            now = board.now()
            for r in hosted._sessions(c):
                if r.get("node") != node or r.get("state") == "ended":
                    continue
                queued = [m for m in hosted._messages(c, r["session_id"]) if m.get("state") == "queued"]
                if (len(queued) > 1 and queued[0].get("kind") == "wake" and queued[0].get("handed_at") is None
                        and any(m.get("kind") == "user" for m in queued[1:])):
                    m = queued[0] | {"state": "failed", "ok": False, "detail": SET_ASIDE, "text": "",
                                     "ended_at": now, "updated_at": now}
                    r |= {"queued": max(0, int(r.get("queued") or 0) - 1), "updated_at": now}
                    _put(c, hosted._msg_key(r["session_id"], m["n"]), m, HUB, now)
                    _put(c, hosted.SESSION_PREFIX + r["session_id"], r, HUB, now)
                    try:
                        _set_aside(c, r["session_id"], m, now)
                    except Exception:
                        _complain("hosted sessions: could not uncover a wake that was set aside; dropping its record")
                        c.execute("DELETE FROM board_state WHERE key = ?", (WAKE_PREFIX + r["session_id"],))
            return _withheld(build())
    except BoardError:
        raise
    except Exception:
        _complain("hosted sessions: could not set an unchecked wake aside")
    return _withheld(build())


def _uncount(rec: dict, m: dict) -> None:
    """A wake that was queued and never sent no longer counts toward the interval, the hour or its threads."""
    if m.get("created_at") in rec["sent"]:
        rec["sent"].remove(m["created_at"])
    for tid in m.get("threads") or []:
        t = rec["threads"].get(str(tid))
        if isinstance(t, dict) and int(t.get("count") or 0) > 0:
            t["count"] -= 1


def _set_aside(c, session_id: str, m: dict, now: float) -> None:
    """The record's side of setting a wake aside, kept apart from the code that failed (no eligibility is judged
    here): the wake's posts lose their coverage and go back to `pending` as the posts table has them now. They are
    not trusted as they are: the ordinary pass re-qualifies everything in `pending` before it queues a wake."""
    rec = _load(c, session_id)
    if rec is None:
        return
    ids = [i for i in m.get("posts") or [] if type(i) is int]
    for k in (str(i) for i in ids):
        for table in ("woken", "handed", "read"):
            rec[table].pop(k, None)
    for row in c.execute("""SELECT id, thread_id, agent, needs_response, seq FROM posts
                            WHERE id IN (SELECT value FROM json_each(?))""", (json.dumps(ids),)):
        if str(row["id"]) not in rec["claimed"]:
            rec["pending"][str(row["id"])] = _entry(row)
    _uncount(rec, m)
    _save(c, session_id, rec, now)


def _withheld(out: dict) -> dict:
    for s in out.get("sessions", []):
        for m in s.get("messages", []):
            if m.get("kind") == "wake":
                m.pop("text", None)
    return out


def _next_queued(conn, session_id: str) -> tuple[dict | None, bool]:
    """The message the node is handed next (the session's first queued one, the only one listed with its text), and
    whether a message of the human's is queued behind it."""
    queued = [m for m in hosted._messages(conn, session_id) if m.get("state") == "queued"]
    return (queued[0] if queued else None), any(m.get("kind") == "user" for m in queued[1:])


def _unsettled(board: Board, node: str) -> list[str]:
    """This node's sessions whose next message is a wake the node was never shown: it needs the re-check, and a
    write, before it may be. A wake that was shown once is the node's: it may be writing it already, so it is
    neither checked nor changed again (settling it then would leave its turn running with nobody tracking it)."""
    out = []
    for r in hosted._sessions(board.conn):
        if r.get("node") != node or r.get("state") == "ended" or not int(r.get("queued") or 0):
            continue
        m, _ = _next_queued(board.conn, r["session_id"])
        if (m is not None and m.get("kind") == "wake" and isinstance(m.get("posts"), list)
                and m.get("handed_at") is None):
            out.append(r["session_id"])
    return out


def _recheck(board: Board, c, session_id: str, now: float) -> None:
    """Inside the transaction that shows the node its sessions: eligibility at the moment of hand-over. A wake is
    for the posts recorded on it; those that stopped qualifying since it was queued (author revoked, read meanwhile,
    reserved by the dispatcher) are taken off it, and with none left the wake is not sent at all. A wake that was
    never handed and now has a message of the human's behind it is not sent either: its posts go back to `pending`,
    uncovered, and it is not counted, so the human's message is the next turn. The first hand-over is also when the
    grace period starts anew (`holds`)."""
    r = _get(c, hosted.SESSION_PREFIX + session_id)
    m, human_waiting = _next_queued(c, session_id)
    if (not isinstance(r, dict) or r.get("state") == "ended" or m is None or m.get("kind") != "wake"
            or not isinstance(m.get("posts"), list) or m.get("handed_at") is not None):
        return      # nothing to check, or already the node's (never settled as not sent once it was shown)
    rec = _load(c, session_id) or _new(*_tops(c))
    p = _agent(c, r["agent"])
    keep = _qualifying(board, p, r["project"], m["posts"], rec) if p is not None else {}
    behind = bool(keep) and not human_waiting and hosted_results.waiting(board, c, session_id)
    occupied = bool(keep) and not human_waiting and not behind and _occupied(board, r)
    defer = bool(keep) and (human_waiting or behind or occupied)
    for k in (str(i) for i in m["posts"]):
        if k not in keep or defer:
            # No wake reached anyone for it (dropped, or stepping back behind the human's message): its coverage
            # goes, whether or not an entry is still there, so the dispatcher is not held back by a wake that was
            # never delivered. Of a wake that steps back, only the posts that still qualify wait for the next one.
            for table in ("woken", "handed", "read"):
                rec[table].pop(k, None)
            if k in keep:
                rec["pending"][k] = keep[k]
        elif m.get("handed_at") is None:
            # Shown to the node now: the grace period counts from here. `woken` keeps the time the wake was queued,
            # so that a read between then and now (a mark or a dated receipt) still counts as after the wake.
            rec["handed"][k] = now
    if defer:
        _uncount(rec, m)
    if keep and not defer:
        m |= {"posts": sorted(int(k) for k in keep), "text": wake_text(r["agent"], list(keep.values())),
              "handed_at": m.get("handed_at") or now, "updated_at": now}
    else:
        m |= {"state": "failed", "ok": False, "text": "", "ended_at": now, "updated_at": now,
              "detail": OCCUPIED if occupied else BEHIND_RESULTS if behind else DEFERRED if defer else NOT_SENT}
        r |= {"queued": max(0, int(r.get("queued") or 0) - 1), "updated_at": now}
        _put(c, hosted.SESSION_PREFIX + session_id, r, HUB, now)
    _put(c, hosted._msg_key(session_id, m["n"]), m, HUB, now)
    _save(c, session_id, rec, now)


# ---------------------------------------------------------------- the dispatcher's side


def _matching(conn, agent: str, project: Any, now: float) -> list[dict]:
    """The hosted sessions of `agent` in `project` that can take a wake now: not ended, not being closed, on a node
    seen as a session host within nodes.NODE_STALE_SECONDS."""
    if not isinstance(project, str):
        return []
    project = project.rstrip("/") or "/"
    return [r for r in hosted._sessions(conn)
            if r.get("agent") == agent and r.get("project") == project and r.get("state") != "ended"
            and not r.get("stop") and not is_child(r)       # a child is never woken, so it holds nothing
            and now - hosted._node_seen(conn, r.get("node") or "") <= nodes.NODE_STALE_SECONDS]


def holds(board_or_conn: Any, agent: str, project: Any, post_id: Any, now: float) -> bool:
    """A hosted session has this request, so nothing else should be started for it: a session of `agent` in `project`
    that has not ended, on a node seen within nodes.NODE_STALE_SECONDS, and either a wake covering the post was
    queued or handed less than `wake_grace_seconds` ago, or the board showed the post, at or after that wake was
    queued, to a board session that can be the hosted one: a session of the agent registered in this project that is
    not a dispatched run's (`_own_reader`). Shown means the record's `read` mark (any view, `handed`) or a delivery
    receipt of such a session. A session that was never woken for the post, or was woken and was not shown it,
    holds nothing once the grace has passed; and a post the dispatcher reserved a run for is the dispatcher's
    (`reserve`): whatever that run or anybody else reads afterwards, no hosted session holds it."""
    conn = getattr(board_or_conn, "conn", board_or_conn)
    if type(post_id) is not int:
        return False
    for r in _matching(conn, agent, project, now):
        rec = _load(conn, r["session_id"])
        at = rec["woken"].get(str(post_id)) if rec and str(post_id) not in rec["claimed"] else None
        if not isinstance(at, (int, float)):
            continue
        handed_at = rec["handed"].get(str(post_id))
        if now - max(at, handed_at if isinstance(handed_at, (int, float)) else at) < rec["grace"]:
            return True
        read = rec["read"].get(str(post_id))
        if _shadowed(conn, r, at):
            # Neither a read mark nor a receipt can be told to be the root's own, whenever it was made: a child of
            # the agent in the project that has been around since the wake may be its reader, or may be the one
            # that goes on to work from it. After the grace such a root holds nothing.
            continue
        if isinstance(read, (int, float)) and read >= at:
            return True
        from . import delivery
        if delivery.read_since(conn, post_id, agent, at, lambda sid: _own_reader(conn, sid, agent, r["project"])):
            return True
    from . import interactive_wake      # a session the human opened himself, woken the same way (its own records)
    return interactive_wake.holds(conn, agent, project, post_id, now)


def _own_reader(conn, session_id: Any, agent: str, project: str) -> bool:
    """This board session can be the hosted session's own: the agent's, registered in the hosted session's project,
    and not the session of a dispatched run (which registers with its run id). A read by any other session, the
    dispatcher's cold run or the agent in another project, is no sign that the hosted session has the request."""
    row = conn.execute("SELECT agent, project, dispatch_run_id FROM sessions WHERE id = ?", (session_id,)).fetchone() \
        if type(session_id) is int else None
    return (row is not None and row["agent"] == agent and row["dispatch_run_id"] is None
            and (row["project"].rstrip("/") or "/") == project)


def _shadowed(conn, r: dict, since: float) -> bool:
    """Some CHILD session of this root's agent in this root's project has been alive at or after `since` (to this
    day), whichever root it was started under. Such a child signs in to the board with the same identity,
    and nothing in a board session's row says which hosted session opened it: registered in the project (a shared
    child runs there; a worktree child may name it too) it passes `_own_reader` exactly as the root's own board
    session does. A Codex child under a Claude root is, to the board, the same reader as a separate Codex root in
    that project. So while one was around, no read is taken for the root's: a child's read must never keep the
    dispatcher off a request the root did not read. A child for another agent, or in another project, has another
    identity or place and is never mistaken (`_own_reader` asks for the root's agent and project)."""
    return any(is_child(x) and x.get("agent") == r.get("agent") and x.get("project") == r.get("project")
               and (x.get("state") != "ended" or float(x.get("ended_at") or 0) >= since)
               for x in hosted._sessions(conn))


def _unmarked(rec: dict, ids: list[int]) -> list[str]:
    """Of these posts, the ones a wake covered that have no `read` mark at or after that wake."""
    out = []
    for k in (str(i) for i in ids):
        woken, read = rec["woken"].get(k), rec["read"].get(k)
        if isinstance(woken, (int, float)) and not (isinstance(read, (int, float)) and read >= woken):
            out.append(k)
    return out


def handed(board: Board, p: Principal, session_id: int, posts: list[dict]) -> None:
    """Called where the board returns posts to a reader (Board.read_updates, every view): note, for `holds`, that a
    board session of a hosted session's agent, registered in that session's project, was really shown a post a wake
    had covered (`_own_reader`: never a dispatched run's session). This is not a delivery receipt (delivery.py
    keeps its own rule: an unfiltered read or the post_ids
    view); it exists because a woken session may read with `only="addressed"`, which the wake's wording invites and
    which leaves no receipt. Only what is in `posts`, the response itself, is noted. Never raises, and without a
    hosted session of the reader it reads one key range and writes nothing."""
    try:
        ids = [] if p.is_human else [d["id"] for d in posts if p.name in (d.get("to") or [])]
        mine = [r for r in hosted._sessions(board.conn)
                if r.get("agent") == p.name and r.get("state") != "ended" and not is_child(r)] if ids else []
        if not mine:
            return
        now = board.now()
        for r in mine:
            rec = (_load(board.conn, r["session_id"])
                   if _own_reader(board.conn, session_id, p.name, r.get("project")) else None)
            if rec is None or not _unmarked(rec, ids):
                continue        # nothing to note: no write (a re-read, or a post no wake covered)
            with db.write_tx(board.conn) as c:
                rec = _load(c, r["session_id"])
                todo = _unmarked(rec, ids) if rec else []
                # Not while the reader may be a child session of the same agent (`_shadowed`): no mark is written.
                todo = [k for k in todo if not _shadowed(c, r, rec["woken"][k])]
                for k in todo:
                    rec["read"][k] = now
                if todo:
                    _save(c, r["session_id"], rec, now)
    except Exception:
        _complain("hosted sessions: could not note what a session was shown")


def reserve(conn, agent: str, project: Any, post_id: Any, now: float) -> bool:
    """Inside the write transaction in which the dispatcher reserves a launch for this request
    (Board.reserve_dispatch_launch): False when a hosted session holds it after all (a wake was queued since the
    dispatcher looked), and the launch is then not reserved. Otherwise the request is the dispatcher's from here on:
    it leaves the waiting posts of every session of the agent in the project that has not ended (also one whose
    node is not connected), and a wake already queued for it loses it when the node is shown that wake
    (`_qualifying`). Writes nothing without a hosted session of the agent in the project."""
    if holds(conn, agent, project, post_id, now):
        return False
    project = (project.rstrip("/") or "/") if isinstance(project, str) else None
    for r in hosted._sessions(conn) if type(post_id) is int else []:
        # Every session of the agent in the project that has not ended, connected or not: whether a session HOLDS a
        # request depends on its node being there, but a claim must also reach the wake of a session whose node is
        # away, or that wake would be handed over beside the run when the node is back.
        if r.get("agent") != agent or r.get("project") != project or r.get("state") == "ended" or is_child(r):
            continue
        rec = _load(conn, r["session_id"])
        if rec is not None:
            for table in ("pending", "woken", "handed", "read"):    # no longer waiting here, no longer covered
                rec[table].pop(str(post_id), None)
            rec["claimed"][str(post_id)] = now
            _save(conn, r["session_id"], rec, now)
    from . import interactive_wake
    interactive_wake.claim(conn, agent, project, post_id, now)
    return True


def turn_projects(board_or_conn: Any, now: float) -> set[str]:
    """The projects in which a hosted session has a turn running now (Dispatcher._busy_dirs): the hub saw a message
    reach the session's process and has not seen its turn end, and the node is not lost (hosted._lost: silent for
    nodes.NODE_LOST_SECONDS, or no longer hosting sessions; the same test that fails such a turn on the human's
    next read). A child session in the shared checkout counts exactly like its parent (its project is the
    parent's); a child in its own worktree runs in another directory and makes nothing busy."""
    conn = getattr(board_or_conn, "conn", board_or_conn)
    return {r["project"] for r in hosted._sessions(conn)
            if r.get("state") != "ended" and r.get("in_flight") is not None and isinstance(r.get("project"), str)
            and not (is_child(r) and r.get("isolation") == "worktree")
            and not hosted._lost(conn, r, now)}


# ---------------------------------------------------------------- telling the human


def _named(tids: list[int]) -> str:
    """Thread ids, and nothing else of the threads."""
    shown = ", ".join(f"#{t}" for t in tids[:MAX_NAMED])
    more = f" and {len(tids) - MAX_NAMED} more" if len(tids) > MAX_NAMED else ""
    return ("thread " if len(tids) == 1 else "threads ") + shown + more


def item_body(session_id: str, agent: str, tids: list[int], limit: int, reason: str) -> str:
    """The fixed text of a wake-limit item: the session id, the agent's name, thread ids and the limit."""
    return HEADER.format(sid=session_id, agent=agent, where=_named(tids), then=THEN[reason].format(limit=limit),
                         why=WHY[reason].format(limit=limit, thread=tids[0]),
                         lift="this thread" if len(tids) == 1 else "any of those threads (for all of them)")


def _question(session_id: str, agent: str, tids: list[int], limit: int, reason: str) -> dict:
    hour = reason == "hour"
    when = (f" as soon as fewer than {limit} wakes lie within the past hour (the hourly limit itself stays)"
            if hour else "")
    where = "here" if len(tids) == 1 else f"in {_named(tids)}"
    label = f"Keep waking {agent}'s hosted session" if hour else f"Keep waking {agent} for thread #{tids[0]}"
    go = {"id": "go-on", "label": label[:200], "outcome": "answered",
          "description": (f"Your answer is a post in this thread, which lifts the stop: the session is woken again "
                          f"for posts addressed to {agent} {where}, the ones that waited included{when}, until a "
                          "limit is reached again and an item like this comes back. Costs agent turns (tokens).")}
    close = {"id": "close-session", "label": "I'll close the session first", "outcome": "answered",
             "description": (f"Run `board session close {session_id}`, then answer here. A closed session is never "
                             f"woken; later requests to {agent} in that project are handled as without a hosted "
                             "session. Costs nothing. Answering without closing it lifts the stop like the other "
                             "option.")}
    asked = (f"{agent}'s hosted session reached its hourly wake limit ({len(tids)} thread(s) held). Keep waking it?"
             if hour else f"{agent}'s hosted session reached its wake limit on thread #{tids[0]}. Keep waking it?")
    return {"question": asked,
            "context": (f"The limit is {limit}; it exists so that agents cannot keep each other's sessions taking "
                        "turns without you looking in. Recommended: keep waking it, since the limit comes back."),
            "options": [go, close], "recommended_option_id": "go-on"}


def _tell(board: Board, r: dict, rec: dict, now: float, budget: list[int]) -> None:
    """Outside the pass's transaction: close the items of threads the human posted in, and post the Needs-you items
    that are due. ONE per session for an hourly stop, naming the threads it holds; one per thread for a thread's own
    limit. And few: at most delivery.ALERTS_PER_PASS items a pass over all sessions (`budget`, what is left of it)
    and delivery.DAILY_CAP a rolling day per session, as the undelivered alert is capped. A stop whose item has to
    wait stands all the same and gets its item on a later pass. A failure here is logged and tried again."""
    from .delivery import DAILY_CAP

    human = _human(board.conn)
    if human is None:
        return
    sid = r["session_id"]
    for post_id in list(rec["closing"]):
        try:
            _close(board, human, sid, post_id, now)
        except Exception:
            log.exception("session %s: could not close wake-limit item %s", sid, post_id)
    due = {int(tid): t["stopped"] for tid, t in rec["threads"].items()
           if isinstance(t, dict) and t.get("stopped") in WHY and not t.get("alert")
           and now - float(t.get("tried") or float("-inf")) >= ALERT_RETRY_SECONDS}
    hourly = sorted(tid for tid, why in due.items() if why == "hour")
    posted = len(_recent(rec["items"], now, DAY))
    for tids in ([hourly] if hourly else []) + [[tid] for tid in sorted(due) if due[tid] == "thread"]:
        if budget[0] <= 0 or posted >= DAILY_CAP:
            break
        try:
            _alert(board, human, r, tids, now)
            budget[0] -= 1
            posted += 1
        except Exception as e:
            log.warning("session %s: could not post the wake-limit item on thread(s) %s: %s", sid, tids, e)
            with db.write_tx(board.conn) as c:
                cur = _load(c, sid)
                for tid in (str(t) for t in tids) if cur else []:
                    if isinstance(cur["threads"].get(tid), dict) and not cur["threads"][tid].get("alert"):
                        cur["threads"][tid]["tried"] = now
                if cur:
                    _save(c, sid, cur, now)


def _alert(board: Board, human: Principal, r: dict, tids: list[int], now: float) -> int:
    """One Needs-you item for these stopped threads of the session (one thread, or all an hourly stop holds), posted
    in the first of them."""
    from . import human_actions, issues
    from .autorecover import POST_PREFIX, _insert

    sid, agent, keys = r["session_id"], r["agent"], [str(t) for t in tids]
    t = (_load(board.conn, sid) or _new(0))["threads"].get(keys[0]) or {}
    reason, limit = t.get("stopped"), int(t.get("limit") or 0)
    if reason not in WHY or not channel.NAME_RE.match(agent):
        raise Conflict("nothing to report")

    def check() -> None:
        if board.is_paused():
            raise Conflict("the board is paused")
        cur = (_load(board.conn, sid) or _new(0))["threads"]
        if any(not isinstance(cur.get(k), dict) or cur[k].get("stopped") != reason or cur[k].get("alert")
               for k in keys):
            raise Conflict("the wake limit changed meanwhile")

    def hook(post: dict) -> None:
        _insert(board.conn, POST_PREFIX + str(post["id"]),
                {"kind": MARKER_KIND, "thread_id": tids[0], "thread_ids": tids, "session_id": sid, "agent": agent},
                now)
        cur = _load(board.conn, sid)
        for k in keys:
            cur["threads"][k]["alert"] = post["id"]
        cur["items"].append(now)
        _save(board.conn, sid, cur, now)

    question = _question(sid, agent, tids, limit, reason)
    issues._question(board, question)
    post, _ = human_actions.post_as_human(board, human, thread_id=tids[0], type="question", to=[],
                                          body=item_body(sid, agent, tids, limit, reason),
                                          needs_response=True, decision_question=question, post_check=check,
                                          post_hook=hook)
    log.warning("session %s of %s reached its wake limit (%s) on thread(s) %s; told the human in post %s", sid, agent,
                reason, tids, post["id"])
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
    """The session ended (or its record is gone): its watcher state is dropped and its wake-limit items are closed.
    Decided inside the write transaction, on what is stored now: a session that `start` committed after this pass
    read the session list is not an orphan, and its record is its starting mark."""
    with db.write_tx(board.conn) as c:
        r = _get(c, hosted.SESSION_PREFIX + session_id)
        if isinstance(r, dict) and r.get("state") != "ended":
            return
        rec = _load(c, session_id)
        c.execute("DELETE FROM board_state WHERE key = ?", (WAKE_PREFIX + session_id,))
    human = _human(board.conn)
    if rec is not None and human is not None:
        for post_id in dict.fromkeys(rec["closing"] + [t.get("alert") for t in rec["threads"].values()
                                                       if isinstance(t, dict) and t.get("alert")]):
            try:
                _close(board, human, session_id, post_id, now)
            except Exception:
                log.exception("session %s: could not close wake-limit item %s", session_id, post_id)
