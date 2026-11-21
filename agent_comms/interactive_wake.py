"""Waking a session the human opened himself when a board post is addressed to its agent (DESIGN_NOTES "Waking an
interactive session", and its "Review round 1").

A hosted session (hosted_wake.py) is the hub's own process, so the hub can write a wake into it. A session the human
opened in a terminal or the Claude desktop app is not: the hub cannot reach it. What it can do is answer a call that
the session's side keeps open. Claude Code runs a `Stop` hook with `"asyncRewake": true` in the background when a turn
ends, and wakes the idle session at once when that hook exits with code 2, showing it the hook's stderr. That hook is
`board wait` (waiter.py), which long-polls `POST /api/wait` here and exits 2 only when this module says wake. Nothing
here assumes Claude: a waiter is any client that keeps such a call open for one conversation (`client`,
`client_session_id`), so a Codex equivalent can use the same route.

- *Off by default* (`[dispatch.sessions.interactive] wake`). Off, the route answers "off" before it reads or writes
  any board state, `holds`, `claim` and `handed` do nothing, and records left from when it was on are dropped by the
  dispatcher's next pass.
- *A waiter* is one record per agent and conversation (`wake.waiter.<agent>.<conversation id>`), made on its first
  call, paused or not: it is woken only for posts made from then on. It is connected while a call is open (`seen`
  within PRESENT_SECONDS). Each `board wait` process sends a fresh `nonce`; a call with a new nonce for the same
  conversation takes the record over (`armed_at`), and the older process is told "superseded".
- *A pair* is one record per agent and project (`wake.pair.<agent>.<hash of the project>`): the wake limits and the
  dispatcher's per-request deadlines live there, so a new conversation id resets nothing.
- *What wakes it* is what wakes a hosted session, through hosted_wake's helpers (`_look`, `_qualifying`, `_lift`,
  `_thread`, `wake_text`) over channel.eligible: a post visible to the agent, addressed to it, by someone else who is
  an active identity, in a thread of the waiter's project, new since the waiter's mark, not read yet, not a managed
  continuation's, not reserved by the dispatcher. Nothing is decided while the board is paused.
- *Whom.* One waiter per pair at a time: the connected one armed last (the session whose turn ended most recently),
  re-checked whenever a wake is offered or handed. None while the agent has a hosted root session in the project.
- *Offered, then acknowledged.* A wake is OFFERED to one process (bound to its nonce) by the open call's own look or
  by a dispatcher pass, and returned by that process's open call. The hub cannot see a caller hang up (a killed
  process's open call goes on until its wait is up), so the offer counts only once `board wait` acknowledges it
  (`ack`, checked against the offer's id, nonce and deadline before anything changes). An offer not acknowledged in
  ACK_SECONDS, or whose waiter was superseded, went away or is no longer the one to wake, is taken back: its posts
  wait again. Only an acknowledged wake is charged to the limits, and only for the threads it covered.
- *Limits* (`[dispatch.sessions]`, the hosted numbers, per pair): the interval, the rolling hour and the per-thread
  count since the human's last new post there. At a maximum the posts wait: a thread's stop lifts when the human posts
  there, the hourly one when the hour has room. No Needs-you item is posted.
- *The dispatcher* (`holds`, `claim`, `handed`). A request held only through a wake actually offered: from its first
  offer until `wake_grace_seconds` later (one deadline per request, kept across take-backs and re-offers), and after
  that only if a board session that can be the waiter's read it after an acknowledged wake. A waiter that was never
  offered a wake holds nothing. A launch the dispatcher reserves takes the request from every waiter of the pair.
- *Text.* hosted_wake.wake_text: counts, thread ids and agent names only, plus the fixed note.
- *What it cannot stop.* Any process holding the agent's token can call as a waiter of that agent: the hub cannot
  tell it from the agent's own session. What it can gain is bounded above: one grace period per request, the
  agent's own wakes, and the pair's limits, which it cannot reset.
"""
from __future__ import annotations

import hashlib
import json
import logging
import posixpath
import re
import secrets
import time
from typing import Any, Callable

from . import db, hosted, hosted_wake
from .conversations import UUID_RE
from .core import Board, Forbidden, Invalid, Principal
from .hosted_tree import is_child
from .nodes import _get, _put

log = logging.getLogger("agent_comms.interactive_wake")

PREFIX = "wake.waiter."          # <agent>.<conversation id>: one waiter's record
PAIR = "wake.pair."              # <agent>.<sha-256 of the project, 24 hex>: one agent and project's limits and holds
HUB = hosted_wake.HUB
PRESENT_SECONDS = 20.0           # a waiter seen this recently is connected (its call re-reads every POLL_SECONDS;
#                                  the open call of a process that died goes on for up to WAIT_MAX)
HEARTBEAT_SECONDS = 5.0          # an open call writes `seen` at most this often while nothing else changes
POLL_SECONDS = 1.0               # how often an open call re-reads the database (core.WAIT_POLL_SECONDS)
WAIT_MAX = 25                    # seconds one call may stay open: also how long a call whose caller is gone goes on
KEEP_SECONDS = 2 * 86400         # a waiter (and a pair with none) not seen for this long is dropped
MAX_WAITERS = 50                 # per agent; the one seen longest ago goes first
MAX_RETIRED = 20                 # nonces of superseded processes remembered
MAX_PROJECTS = 8
CLIENTS = ("claude-code", "codex")
NONCE_RE = re.compile(r"[0-9a-f]{32}")
ACK_RE = re.compile(r"[0-9a-f]{16}")
ACK_SECONDS = 10.0               # an offered wake not acknowledged in this long is taken back

OFF = {"state": "off"}
IDLE = {"state": "idle"}
SUPERSEDED = {"state": "superseded"}
ACKED = {"state": "acked"}
REJECTED = {"state": "rejected"}


def enabled(board: Board) -> bool:
    """`[dispatch.sessions.interactive] wake` as the hub has it now (a table that does not validate: off)."""
    return bool(hosted_wake.limits(board).interactive.wake)


def _live(board_or_conn: Any) -> tuple[Any, bool]:
    """The connection, and whether the feature is on. Only a Board knows its settings: a bare connection counts as
    off, so every caller that needs a hold, a claim or a read mark passes the Board."""
    if isinstance(board_or_conn, Board):
        return board_or_conn.conn, enabled(board_or_conn)
    return board_or_conn, False


# ---------------------------------------------------------------- records


def _key(agent: str, client_session_id: str) -> str:
    return f"{PREFIX}{agent}.{client_session_id}"


def _pair_key(agent: str, project: str) -> str:
    return f"{PAIR}{agent}.{hashlib.sha256(project.encode()).hexdigest()[:24]}"


def _num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _times(v: Any) -> dict:
    return {k: t for k, t in v.items() if isinstance(k, str) and _num(t)} if isinstance(v, dict) else {}


def _clean(raw: Any) -> dict | None:
    """A waiter: its identity and process, and its marks over the posts table (the fields hosted_wake._look and
    `_qualifying` read). Anything malformed: no record."""
    if not isinstance(raw, dict) or type(raw.get("mark")) is not int:
        return None
    w = {k: raw.get(k) for k in ("agent", "client", "client_session_id", "project", "nonce")}
    t = {k: raw.get(k) for k in ("started_at", "armed_at", "seen")}
    if not all(isinstance(v, str) for v in w.values()) or not all(_num(v) for v in t.values()):
        return None
    o = raw.get("offer")
    if not (isinstance(o, dict) and isinstance(o.get("id"), str) and isinstance(o.get("nonce"), str)
            and _num(o.get("at")) and isinstance(o.get("entries"), dict)):
        o = None
    return w | t | {
        "mark": raw["mark"], "post": raw["post"] if type(raw.get("post")) is int else None,
        "sealed": [i for i in raw.get("sealed") or [] if type(i) is int] if isinstance(raw.get("sealed"), list) else [],
        "pending": raw["pending"] if isinstance(raw.get("pending"), dict) else {},
        "claimed": _times(raw.get("claimed")), "delivered": _times(raw.get("delivered")),
        "read": _times(raw.get("read")), "offer": o,
        "retired": [x for x in raw.get("retired") or [] if isinstance(x, str)] if isinstance(raw.get("retired"),
                                                                                             list) else []}


def _load(conn, key: str) -> dict | None:
    return _clean(_get(conn, key))


def _records(conn, agent: str | None = None, project: str | None = None) -> dict[str, dict]:
    prefix = PREFIX + (agent + "." if agent else "")
    out = {}
    for key, value in conn.execute("SELECT key, value FROM board_state WHERE key >= ? AND key < ?",
                                   hosted._range(prefix)):
        try:
            rec = _clean(json.loads(value))
        except (TypeError, ValueError):
            rec = None
        if rec is not None and (project is None or rec["project"] == project):
            out[key] = rec
    return out


def _save(c, key: str, rec: dict, now: float) -> None:
    for table in ("claimed", "delivered", "read"):
        rec[table] = hosted_wake._recent(rec[table], now, hosted_wake.KEEP_WOKEN_SECONDS, hosted_wake.MAX_WOKEN)
    if len(rec["pending"]) > hosted_wake.MAX_PENDING:
        rec["pending"] = dict(sorted(rec["pending"].items(), key=lambda kv: kv[1][3])[-hosted_wake.MAX_PENDING:])
    rec["retired"] = rec["retired"][-MAX_RETIRED:]
    _put(c, key, rec, HUB, now)


def _pair(conn, agent: str, project: str) -> dict:
    """The agent's limits in this project (the fields hosted_wake._lift and `_thread` read) and its holds: post id
    -> the deadline set by that request's first offer."""
    raw = _get(conn, _pair_key(agent, project))
    raw = raw if isinstance(raw, dict) and raw.get("agent") == agent and raw.get("project") == project else {}
    return {"agent": agent, "project": project,
            "sent": [t for t in raw.get("sent") or [] if _num(t)] if isinstance(raw.get("sent"), list) else [],
            "threads": raw["threads"] if isinstance(raw.get("threads"), dict) else {},
            "closing": [], "hour_ack": raw.get("hour_ack") if _num(raw.get("hour_ack")) else None,
            "holds": _times(raw.get("holds")), "seen": raw.get("seen") if _num(raw.get("seen")) else 0.0}


def _save_pair(c, pair: dict, now: float) -> None:
    pair["sent"] = hosted_wake._recent(pair["sent"], now, hosted_wake.HOUR)
    pair["holds"] = {k: t for k, t in pair["holds"].items() if t > now - hosted_wake.KEEP_WOKEN_SECONDS}
    pair["closing"], pair["seen"] = [], now
    _put(c, _pair_key(pair["agent"], pair["project"]), pair, HUB, now)


def _new(c, p: Principal, body: dict, now: float) -> dict:
    top, top_id = hosted_wake._tops(c)
    return {"agent": p.name, "client": body["client"], "client_session_id": body["client_session_id"],
            "project": body["projects"][0], "nonce": body["nonce"], "started_at": now, "armed_at": now, "seen": now,
            "mark": top, "post": top_id, "sealed": [], "pending": {}, "claimed": {}, "delivered": {}, "read": {},
            "offer": None, "retired": []}


def _present(rec: dict, now: float) -> bool:
    return now - rec["seen"] <= PRESENT_SECONDS


# ---------------------------------------------------------------- who is woken


def _hosted_root(conn, agent: str, project: str) -> bool:
    """The agent has a hosted root session in this project that has not ended: that one is woken, no waiter is."""
    return any(r.get("agent") == agent and r.get("project") == project and r.get("state") != "ended"
               and not r.get("stop") and not is_child(r) for r in hosted._sessions(conn))


def _chosen(conn, key: str, rec: dict, now: float, recs: dict | None = None) -> bool:
    """This waiter is the one of its pair to wake: connected, armed last among the connected ones (the session whose
    turn ended most recently), and the agent has no hosted root session in the project."""
    if not _present(rec, now) or _hosted_root(conn, rec["agent"], rec["project"]):
        return False
    recs = recs if recs is not None else _records(conn, rec["agent"], rec["project"])
    rivals = [(r["armed_at"], k) for k, r in recs.items() if _present(r, now)]
    return max(rivals, default=(rec["armed_at"], key)) <= (rec["armed_at"], key)


def _offer_stands(conn, key: str, rec: dict, now: float, recs: dict | None = None) -> bool:
    """The waiter's offer may still be taken up: not past its deadline, made to the process that holds the record
    now, and that waiter is still the one to wake."""
    o = rec["offer"]
    return (o is not None and now - o["at"] < ACK_SECONDS and o["nonce"] == rec["nonce"]
            and _chosen(conn, key, rec, now, recs))


def _take_back(c, rec: dict) -> None:
    """An offer nobody acknowledged never reached a session: its posts wait again in `pending` (re-qualified before
    the next offer, as hosted_wake._set_aside does). It was never charged, so nothing is uncounted."""
    o, rec["offer"] = rec["offer"], None
    for k, e in (o or {}).get("entries", {}).items():
        if k not in rec["claimed"] and isinstance(e, list):
            rec["pending"][k] = e
    if o:
        log.info("waiter: an offered wake was not taken up; its %d post(s) wait again", len(o.get("entries", {})))


def _canonical(x: Any) -> str:
    """One project path as the hub keeps projects (core._norm_path), and nothing a path could hide: absolute,
    normalized (no `.`, `..` or doubled slash), printable, at most 1024 characters."""
    if not isinstance(x, str) or not x.startswith("/") or len(x) > 1024 or not x.isprintable():
        raise Invalid("each project must be an absolute, normalized path")
    q = x.rstrip("/") or "/"
    if posixpath.normpath(q) != q or q.startswith("//"):
        raise Invalid("each project must be an absolute, normalized path")
    return q


def _checked(body: Any) -> dict:
    if not isinstance(body, dict):
        raise Invalid("a JSON object is required")
    sid, nonce, client = body.get("client_session_id"), body.get("nonce"), body.get("client") or CLIENTS[0]
    if not isinstance(sid, str) or not UUID_RE.fullmatch(sid):
        raise Invalid("client_session_id must be the conversation's UUID (lowercase)")
    if not isinstance(nonce, str) or not NONCE_RE.fullmatch(nonce):
        raise Invalid("nonce must be 32 lowercase hex digits, new for each waiting process")
    if client not in CLIENTS:
        raise Invalid("client must be one of " + ", ".join(CLIENTS))
    ack = body.get("ack")
    if ack is not None and (not isinstance(ack, str) or not ACK_RE.fullmatch(ack)):
        raise Invalid("ack must be the id of the wake this process was offered")
    projects = body.get("projects")
    if not isinstance(projects, list) or not 1 <= len(projects) <= MAX_PROJECTS:
        raise Invalid(f"projects must list 1 to {MAX_PROJECTS} absolute paths, the canonical one first")
    return {"client_session_id": sid, "nonce": nonce, "client": client, "ack": ack,
            "projects": list(dict.fromkeys(_canonical(x) for x in projects))}


# ---------------------------------------------------------------- one waiter, one pass


def _release_stale(c, pair_recs: dict, now: float, skip: str | None = None) -> None:
    """Take back the offers of this pair that can no longer be taken up (past the deadline, superseded, the waiter
    gone or no longer the one to wake)."""
    for k, r in pair_recs.items():
        if k != skip and r["offer"] is not None and not _offer_stands(c, k, r, now, pair_recs):
            _take_back(c, r)
            _save(c, k, r, now)


def _advance(board: Board, c, key: str, rec: dict, p: Principal, cfg, now: float) -> None:
    """Take in the posts above the waiter's marks, lift the stop of threads the human posted in, take back offers that
    no longer stand, and offer the wake that is due (hosted_wake._advance's steps, on a waiter and its pair). Inside
    the caller's write transaction; saves the pair and the other waiters it changes, not `rec`."""
    top, top_id = hosted_wake._tops(c)
    pair = _pair(c, rec["agent"], rec["project"])
    sent = hosted_wake._recent(pair["sent"], now, hosted_wake.HOUR)
    if top > rec["mark"]:
        hosted_wake._look(board, c, rec, {"project": rec["project"]}, p, top, top_id)
        hosted_wake._lift(c, pair, max(sent, default=None))
    recs = _records(c, rec["agent"], rec["project"]) | {key: rec}
    _release_stale(c, recs, now, skip=key)
    if rec["offer"] is not None and not _offer_stands(c, key, rec, now, recs):
        _take_back(c, rec)
    if _hosted_root(c, rec["agent"], rec["project"]):
        rec["pending"] = {}     # the hosted session's posts: they never wake a waiter, also once it has ended
    elif (rec["offer"] is None and rec["pending"] and _chosen(c, key, rec, now, recs)
          and not any(r["offer"] is not None for k, r in recs.items() if k != key)
          and not (sent and now - max(sent) < cfg.wake_min_interval_seconds)
          and len(sent) < cfg.max_wakes_per_hour):      # at the hourly maximum the posts wait until there is room
        _offer(board, c, key, rec, pair, recs, p, cfg, now)
    _save_pair(c, pair, now)


def _offer(board: Board, c, key: str, rec: dict, pair: dict, recs: dict, p: Principal, cfg, now: float) -> None:
    delivered = {k for other, r in recs.items() if other != key for k in r["delivered"]}
    rec["pending"] = {k: e for k, e in hosted_wake._qualifying(
        board, p, rec["project"], [int(k) for k in rec["pending"]], rec).items() if k not in delivered}
    batch = {k: e for k, e in rec["pending"].items() if not hosted_wake._stopped(pair, e[0])}
    for tid in sorted({e[0] for e in batch.values()}):
        t = hosted_wake._thread(c, pair, tid)
        if t["count"] >= cfg.max_wakes_without_human:
            t |= {"stopped": "thread", "limit": cfg.max_wakes_without_human}
    batch = {k: e for k, e in batch.items() if not hosted_wake._stopped(pair, e[0])}
    if not batch:
        return
    rec["offer"] = {"id": secrets.token_hex(8), "nonce": rec["nonce"], "at": now, "entries": batch}
    for k in batch:
        del rec["pending"][k]
        pair["holds"].setdefault(k, now + float(cfg.wake_grace_seconds))    # one deadline per request (S7)
    log.info("waiter %s: wake offered for %d post(s)", key, len(batch))


def _handed_over(board: Board, c, rec: dict, p: Principal) -> dict | None:
    """The answer that hands the waiter's standing offer to its process: re-checked now (`_qualifying`); posts that
    stopped qualifying leave it, and with none left it is taken back."""
    o = rec["offer"]
    keep = hosted_wake._qualifying(board, p, rec["project"], [int(k) for k in o["entries"]], rec)
    o["entries"] = {k: e for k, e in o["entries"].items() if k in keep}
    if not o["entries"]:
        rec["offer"] = None
        return None
    return {"state": "wake", "text": hosted_wake.wake_text(rec["agent"], list(o["entries"].values())), "id": o["id"]}


def _ack(board: Board, key: str, body: dict, now: float) -> dict:
    """The process that was offered a wake says it has it. Checked first, changing nothing unless it is accepted:
    the offer's id, the process's nonce (the record's current one) and the deadline. Accepted: the wake is
    delivered, and charged to the pair's limits for the threads it covered (and no other)."""
    with db.write_tx(board.conn) as c:
        rec = _load(c, key)
        if rec is not None and body["nonce"] in rec["retired"]:
            return SUPERSEDED
        o = rec["offer"] if rec is not None else None
        if (o is None or o["id"] != body["ack"] or o["nonce"] != body["nonce"] or rec["nonce"] != body["nonce"]
                or now - o["at"] >= ACK_SECONDS):
            return REJECTED
        pair = _pair(c, rec["agent"], rec["project"])
        pair["sent"].append(now)
        for tid in sorted({e[0] for e in o["entries"].values()}):
            hosted_wake._thread(c, pair, tid)["count"] += 1
        for k in o["entries"]:
            rec["delivered"][k] = now
        rec["offer"] = None
        _save(c, key, rec, now)
        _save_pair(c, pair, now)
    log.info("waiter %s: wake delivered", key)
    return ACKED


def poll(board: Board, p: Principal, body: Any) -> dict:
    """One look for this waiter: "wake" with the text and its id, "idle" (nothing yet), "superseded" (a newer process
    of the same conversation took over) or "off"; with `ack`, "acked" or "rejected". Also its heartbeat."""
    if not enabled(board):
        return OFF
    if p.is_human:
        raise Forbidden("only an agent's own session waits for wakes")
    body = _checked(body)
    key, conn = _key(p.name, body["client_session_id"]), board.conn
    now = board.now()
    if body["ack"] is not None:
        return _ack(board, key, body, now)
    rec = _load(conn, key)
    if rec is not None and body["nonce"] in rec["retired"]:
        return SUPERSEDED
    paused = board.is_paused()
    if (rec is not None and rec["nonce"] == body["nonce"] and now - rec["seen"] < HEARTBEAT_SECONDS
            and (paused or (rec["offer"] is None and not rec["pending"]
                            and hosted_wake._tops(conn)[0] <= rec["mark"]))):
        return IDLE         # nothing new since the last look: no write
    cfg = hosted_wake.limits(board)
    with db.write_tx(conn) as c:
        rec = _load(c, key)
        if rec is None:
            # The mark is taken now, paused or not: posts made from here on are this session's (S3).
            rec = _new(c, p, body, now)
            _evict(c, p.name, now)
        elif body["nonce"] in rec["retired"]:
            return SUPERSEDED
        elif rec["nonce"] != body["nonce"]:
            rec["retired"].append(rec["nonce"])
            rec |= {"nonce": body["nonce"], "armed_at": now}
            if rec["project"] != body["projects"][0] and rec["offer"] is None:
                # Another project: a new mark there (its history is not this session's), the old pending posts go.
                rec |= {"project": body["projects"][0], "pending": {}, "mark": hosted_wake._tops(c)[0],
                        "post": hosted_wake._tops(c)[1], "sealed": []}
        rec["seen"] = now
        out = None
        if not board.is_paused():       # paused: nothing is decided; the posts are found after the unpause
            _advance(board, c, key, rec, p, cfg, now)
            if rec["offer"] is not None and rec["offer"]["nonce"] == body["nonce"]:
                out = _handed_over(board, c, rec, p)
        _save(c, key, rec, now)
    return out or IDLE


def _evict(c, agent: str, now: float) -> None:
    recs = _records(c, agent)
    for key, _ in sorted(recs.items(), key=lambda kv: kv[1]["seen"])[:max(0, len(recs) + 1 - MAX_WAITERS)]:
        c.execute("DELETE FROM board_state WHERE key = ?", (key,))


async def wait(board: Board, p: Principal, body: Any, gone: Callable[[], Any] | None = None) -> dict:
    """`poll`, again every POLL_SECONDS for up to `wait` seconds (at most WAIT_MAX) until it is not "idle". The
    deadline is wall time, not the board's clock (hosted._poll's rule); `gone` ends the wait early when it can tell."""
    import anyio
    from anyio import to_thread

    seconds = hosted._wait_seconds(body.get("wait", 0) if isinstance(body, dict) else 0, WAIT_MAX)
    deadline = time.monotonic() + seconds
    out = await to_thread.run_sync(poll, board, p, body)
    while out == IDLE and time.monotonic() < deadline and not (gone is not None and await gone()):
        await anyio.sleep(max(0.0, min(POLL_SECONDS, deadline - time.monotonic())))
        out = await to_thread.run_sync(poll, board, p, body)
    return out


# ---------------------------------------------------------------- the dispatcher's side


def tick(board: Board) -> None:
    """Each dispatcher pass, just before it decides what to launch: offer the wakes that are due to connected waiters
    (so that `holds` sees them), take back offers that no longer stand, drop waiters and pairs gone for KEEP_SECONDS.
    Off: records left from when it was on are dropped (none: one read, no write). Never raises."""
    try:
        recs = _records(board.conn)
        pairs = [k for (k,) in board.conn.execute("SELECT key FROM board_state WHERE key >= ? AND key < ?",
                                                   hosted._range(PAIR))]
        if not recs and not pairs:
            return
        now, on = board.now(), enabled(board)
        if not on:
            with db.write_tx(board.conn) as c:
                for key in list(recs) + pairs:
                    c.execute("DELETE FROM board_state WHERE key = ?", (key,))
            return
        if board.is_paused():
            return
        cfg, top = hosted_wake.limits(board), hosted_wake._tops(board.conn)[0]
        for key, rec in recs.items():
            if now - rec["seen"] > KEEP_SECONDS:
                with db.write_tx(board.conn) as c:
                    c.execute("DELETE FROM board_state WHERE key = ?", (key,))
                continue
            if rec["offer"] is None and (not _present(rec, now) or (top <= rec["mark"] and not rec["pending"])):
                continue    # nobody to offer it to, or nothing new: no write
            with db.write_tx(board.conn) as c:
                rec = _load(c, key)
                p = hosted_wake._agent(c, rec["agent"]) if rec else None
                if rec is None or p is None:
                    continue
                _advance(board, c, key, rec, p, cfg, now)
                _save(c, key, rec, now)
        live = {_pair_key(r["agent"], r["project"]) for r in _records(board.conn).values()}
        for key in pairs:
            if key not in live:
                p = _get(board.conn, key)
                if not isinstance(p, dict) or now - float(p.get("seen") or 0) > KEEP_SECONDS:
                    with db.write_tx(board.conn) as c:
                        c.execute("DELETE FROM board_state WHERE key = ?", (key,))
    except Exception:
        hosted_wake._complain("interactive wakes: the dispatcher's pass failed")


def _reader(conn, session_id: Any, agent: str, rec: dict) -> bool:
    """A board session that can be this waiter's own: hosted_wake._own_reader (the agent's, registered in the
    project, not a dispatched run's), and not one that recorded another conversation as its own."""
    if not hosted_wake._own_reader(conn, session_id, agent, rec["project"]):
        return False
    row = conn.execute("SELECT client_session_id FROM sessions WHERE id = ?", (session_id,)).fetchone()
    return row is None or row["client_session_id"] in (None, rec["client_session_id"])


def holds(board_or_conn: Any, agent: str, project: Any, post_id: Any, now: float) -> bool:
    """A waiter of `agent` in `project` has this request (hosted_wake.holds' rule, on the pair and its waiters):
    until the deadline its first offer set, or once a board session that can be a waiter's own was shown it after
    an acknowledged wake (a read mark or a delivery receipt). A request the dispatcher reserved, and one no waiter
    was ever offered, is never held. Off, or without a pair: nothing."""
    conn, on = _live(board_or_conn)
    if not on or type(post_id) is not int or not isinstance(project, str):
        return False
    project, k = project.rstrip("/") or "/", str(post_id)
    recs = _records(conn, agent, project)
    if not recs or any(k in r["claimed"] for r in recs.values()):
        return False
    until = _pair(conn, agent, project)["holds"].get(k)
    if until is None:
        return False
    if now < until:
        return True
    for rec in recs.values():
        at = rec["delivered"].get(k)
        if at is None or hosted_wake._shadowed(conn, {"agent": agent, "project": project}, at):
            continue
        if rec["read"].get(k, -1.0) >= at:
            return True
        from . import delivery
        if delivery.read_since(conn, post_id, agent, at, lambda sid, rec=rec: _reader(conn, sid, agent, rec)):
            return True
    return False


def claim(board_or_conn: Any, agent: str, project: Any, post_id: Any, now: float) -> None:
    """Inside the dispatcher's reservation (hosted_wake.reserve): the request is the dispatcher's from here on. It
    leaves every waiter of the pair (waiting, offered or delivered) and the pair's holds. Off, or without a waiter:
    nothing is written."""
    conn, on = _live(board_or_conn)
    if not on or type(post_id) is not int or not isinstance(project, str):
        return
    project, k = project.rstrip("/") or "/", str(post_id)
    recs = _records(conn, agent, project)
    for key, rec in recs.items():
        for table in ("pending", "delivered", "read"):
            rec[table].pop(k, None)
        if rec["offer"] is not None:
            rec["offer"]["entries"].pop(k, None)
            if not rec["offer"]["entries"]:
                rec["offer"] = None
        rec["claimed"][k] = now
        _save(conn, key, rec, now)
    if recs:
        pair = _pair(conn, agent, project)
        pair["holds"].pop(k, None)
        _save_pair(conn, pair, now)


def handed(board: Board, p: Principal, session_id: int, posts: list[dict]) -> None:
    """Where the board returns posts to a reader (beside hosted_wake.handed): note, for `holds`, that a board session
    that can be a waiter's own was shown a post an acknowledged wake of that waiter covered. Off, or without a waiter
    of the reader: one settings read or one key range, and no write. Never raises."""
    try:
        if p.is_human or not enabled(board):
            return
        ids = [str(d["id"]) for d in posts if p.name in (d.get("to") or [])]
        recs = _records(board.conn, p.name) if ids else {}
        now = board.now()
        for key, rec in recs.items():
            todo = [k for k in ids if k in rec["delivered"] and rec["read"].get(k, -1.0) < rec["delivered"][k]]
            if not todo or not _reader(board.conn, session_id, p.name, rec):
                continue
            with db.write_tx(board.conn) as c:
                rec = _load(c, key)
                if rec is None:
                    continue
                todo = [k for k in todo if k in rec["delivered"]
                        and not hosted_wake._shadowed(c, rec, rec["delivered"][k])]
                for k in todo:
                    rec["read"][k] = now
                if todo:
                    _save(c, key, rec, now)
    except Exception:
        hosted_wake._complain("interactive wakes: could not note what a session was shown")
