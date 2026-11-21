"""Waking a session the human opened himself when a board post is addressed to its agent (DESIGN_NOTES "Waking an
interactive session").

A hosted session (hosted_wake.py) is the hub's own process, so the hub can write a wake into it. A session the human
opened in a terminal or the Claude desktop app is not: the hub cannot reach it. What it can do is answer a call that
the session's side keeps open. Claude Code runs a `Stop` hook with `"asyncRewake": true` in the background when a turn
ends, and wakes the idle session at once when that hook exits with code 2, showing it the hook's stderr. That hook is
`board wait` (cli.py), which long-polls `POST /api/wait` here and exits 2 only when this module says wake. Nothing in
this module assumes Claude: a waiter is any client that keeps such a call open for one conversation (`client`,
`client_session_id`), so a Codex equivalent can use the same route.

- *Off by default* (`[dispatch.sessions.interactive] wake`). Off, the route answers "off" before it reads or writes
  anything else, no pass runs, nothing holds a request, and records left from when it was on are dropped by the
  dispatcher's next pass.
- *A waiter* is one record per agent and conversation (`wake.waiter.<agent>.<conversation id>` in `board_state`),
  made on its first call: it is woken only for posts made from then on. It is connected while a call is open (`seen`
  within PRESENT_SECONDS). Each `board wait` process sends a fresh `nonce`; a call with a new nonce for the same
  conversation takes the record over (`armed_at`), and the older process is told "superseded" and exits. The record
  outlives the process (the hook runs again after every turn), so the limits and the read marks carry over.
- *What wakes it* is exactly what wakes a hosted session: hosted_wake's own record format and helpers (`_look`,
  `_qualifying`, `_lift`, `_thread`, `wake_text`), over channel.eligible. A post visible to the agent, addressed to it,
  by someone else who is an active identity, in a thread of the waiter's project, new since the waiter's mark, not
  read yet, not a managed continuation's, not reserved by the dispatcher. Nothing while the board is paused: the posts
  stay above the mark and are found after the unpause.
- *Whom it wakes.* One waiter per (agent, project): the connected one that was armed last, i.e. the session whose
  turn ended most recently. None while the agent has a hosted root session in that project (that one is woken as
  before). A post another waiter of the agent was woken for wakes nobody else.
- *Limits* (`[dispatch.sessions]`, the hosted numbers, per waiter): the interval, the rolling hour and the
  per-thread count since the human's last new post there. At a maximum the posts wait: a thread's stop lifts when the
  human posts in that thread, the hourly one when the hour has room. No Needs-you item is posted (the human is at that
  session; its prompt hook shows the posts when he next types).
- *Queued, then handed.* A pass (the route's own, or each dispatcher pass) decides a wake and records it (`queued`,
  with the posts it covers: `woken`, so `holds` sees it before the dispatcher launches). The route hands it to the
  waiter's open call after checking its posts again (`_qualifying`); with none left it is dropped and not counted.
- *The dispatcher* (`holds`, `claim`, `handed`): as for a hosted wake. For `wake_grace_seconds` after a wake, or once
  a board session of the agent in the project read the post after it (a read mark or a delivery receipt; never a
  dispatched run's session, nor one whose recorded conversation is another), the dispatcher launches nothing for the
  request. A launch the dispatcher reserves takes the request from every waiter.
- *Text.* hosted_wake.wake_text: counts, thread ids and agent names only, plus the fixed note. Never a post body,
  title, summary, task title or ref.
"""
from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, Callable

from . import db, hosted, hosted_wake
from .conversations import UUID_RE
from .core import Board, Forbidden, Invalid, Principal, _norm_path
from .hosted_tree import is_child
from .nodes import _get, _put

log = logging.getLogger("agent_comms.interactive_wake")

PREFIX = "wake.waiter."          # <agent>.<conversation id>: one waiter's record
HUB = hosted_wake.HUB
PRESENT_SECONDS = 20.0           # a waiter seen this recently is connected (its call re-reads every POLL_SECONDS)
HEARTBEAT_SECONDS = 5.0          # an open call writes `seen` at most this often while nothing else changes
POLL_SECONDS = 1.0               # how often an open call re-reads the database (core.WAIT_POLL_SECONDS)
WAIT_MAX = 55                    # seconds one call may stay open (the gateway allows 300)
KEEP_SECONDS = 2 * 86400         # a waiter not seen for this long is dropped (its limits and marks with it)
MAX_WAITERS = 50                 # per agent; the one seen longest ago goes first
MAX_RETIRED = 20                 # nonces of superseded processes remembered
MAX_PROJECTS = 8
CLIENTS = ("claude-code", "codex")
NONCE_RE = re.compile(r"[0-9a-f]{32}")

OFF = {"state": "off"}
IDLE = {"state": "idle"}
SUPERSEDED = {"state": "superseded"}


def enabled(board: Board) -> bool:
    """`[dispatch.sessions.interactive] wake` as the hub has it now (a table that does not validate: off)."""
    return bool(hosted_wake.limits(board).interactive.wake)


# ---------------------------------------------------------------- the record


def _key(agent: str, client_session_id: str) -> str:
    return f"{PREFIX}{agent}.{client_session_id}"


def _num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _clean(raw: Any) -> dict | None:
    """hosted_wake's record (its own `_clean`), plus the waiter's fields. Anything malformed: no record."""
    rec = hosted_wake._clean(raw)
    if rec is None:
        return None
    w = {k: raw.get(k) for k in ("agent", "client", "client_session_id", "project", "nonce")}
    t = {k: raw.get(k) for k in ("started_at", "armed_at", "seen")}
    if not all(isinstance(v, str) for v in w.values()) or not all(_num(v) for v in t.values()):
        return None
    q = raw.get("queued")
    if not (isinstance(q, dict) and isinstance(q.get("posts"), list) and isinstance(q.get("threads"), list)
            and _num(q.get("created_at"))):
        q = None
    retired = [x for x in raw.get("retired") or [] if isinstance(x, str)] if isinstance(raw.get("retired"), list) \
        else []
    return rec | w | t | {"queued": q, "retired": retired}


def _load(conn, key: str) -> dict | None:
    return _clean(_get(conn, key))


def _records(conn, agent: str | None = None) -> dict[str, dict]:
    prefix = PREFIX + (agent + "." if agent else "")
    out = {}
    for key, value in conn.execute("SELECT key, value FROM board_state WHERE key >= ? AND key < ?",
                                   hosted._range(prefix)):
        try:
            rec = _clean(json.loads(value))
        except (TypeError, ValueError):
            rec = None
        if rec is not None:
            out[key] = rec
    return out


def _save(c, key: str, rec: dict, now: float) -> None:
    """As hosted_wake._save, under this record's own key."""
    rec["sent"] = hosted_wake._recent(rec["sent"], now, hosted_wake.HOUR)
    for table in ("woken", "handed", "read", "claimed"):
        rec[table] = hosted_wake._recent(rec[table], now, hosted_wake.KEEP_WOKEN_SECONDS, hosted_wake.MAX_WOKEN)
    if len(rec["pending"]) > hosted_wake.MAX_PENDING:
        rec["pending"] = dict(sorted(rec["pending"].items(), key=lambda kv: kv[1][3])[-hosted_wake.MAX_PENDING:])
    rec["retired"] = rec["retired"][-MAX_RETIRED:]
    _put(c, key, rec, HUB, now)


def _new(c, p: Principal, body: dict, project: str, now: float) -> dict:
    return hosted_wake._new(*hosted_wake._tops(c)) | {
        "agent": p.name, "client": body["client"], "client_session_id": body["client_session_id"],
        "project": project, "nonce": body["nonce"], "started_at": now, "armed_at": now, "seen": now,
        "queued": None, "retired": []}


def _present(rec: dict, now: float) -> bool:
    return now - rec["seen"] <= PRESENT_SECONDS


# ---------------------------------------------------------------- who is woken


def _hosted_root(conn, agent: str, project: str) -> bool:
    """The agent has a hosted root session in this project that has not ended: that one is woken, no waiter is."""
    return any(r.get("agent") == agent and r.get("project") == project and r.get("state") != "ended"
               and not r.get("stop") and not is_child(r) for r in hosted._sessions(conn))


def _chosen(conn, key: str, rec: dict, now: float) -> bool:
    """This waiter is the one of its agent and project to wake: connected, and armed last among the connected ones
    (the session whose turn ended most recently); and the agent has no hosted root session there."""
    if not _present(rec, now) or _hosted_root(conn, rec["agent"], rec["project"]):
        return False
    rivals = [(r["armed_at"], k) for k, r in _records(conn, rec["agent"]).items()
              if r["project"] == rec["project"] and _present(r, now)]
    return max(rivals, default=(rec["armed_at"], key)) <= (rec["armed_at"], key)


def _woken_elsewhere(conn, key: str, rec: dict) -> set[str]:
    """Posts another waiter of the agent in the project was woken for: they wake nobody else."""
    return {k for other, r in _records(conn, rec["agent"]).items()
            if other != key and r["project"] == rec["project"] for k in r["woken"]}


def _pick(conn, candidates: list[str]) -> str:
    """The waiter's project: the first candidate that some thread is in (the client sends its checkout, its main
    repository and their logical forms, as `board brief` does), else the first."""
    for q in candidates:
        if conn.execute("SELECT 1 FROM threads WHERE project = ? LIMIT 1", (q,)).fetchone():
            return q
    return candidates[0]


# ---------------------------------------------------------------- one waiter, one pass


def _advance(board: Board, c, key: str, rec: dict, p: Principal, cfg, now: float) -> None:
    """Take in the posts above the record's marks, lift the stop of threads the human posted in, and queue the wake
    that is due (hosted_wake._advance's steps, on a waiter). Inside the caller's write transaction."""
    top, top_id = hosted_wake._tops(c)
    if rec["post"] is None:
        rec["post"] = top_id
    rec["grace"] = float(cfg.wake_grace_seconds)
    sent = hosted_wake._recent(rec["sent"], now, hosted_wake.HOUR)
    if top > rec["mark"]:
        hosted_wake._look(board, c, rec, {"project": rec["project"]}, p, top, top_id)
        hosted_wake._lift(c, rec, max(sent, default=None))
    if _hosted_root(c, rec["agent"], rec["project"]):
        rec["pending"] = {}     # the hosted session's posts: they never wake a waiter, also once it has ended
        return
    if (rec["queued"] is not None or not rec["pending"] or not _chosen(c, key, rec, now)
            or (sent and now - max(sent) < cfg.wake_min_interval_seconds)
            or len(sent) >= cfg.max_wakes_per_hour):
        return      # at the hourly maximum the posts simply wait until the hour has room
    elsewhere = _woken_elsewhere(c, key, rec)
    rec["pending"] = {k: e for k, e in hosted_wake._qualifying(
        board, p, rec["project"], [int(k) for k in rec["pending"]], rec).items() if k not in elsewhere}
    batch = {k: e for k, e in rec["pending"].items() if not hosted_wake._stopped(rec, e[0])}
    for tid in sorted({e[0] for e in batch.values()}):
        t = hosted_wake._thread(c, rec, tid)
        if t["count"] >= cfg.max_wakes_without_human:
            t |= {"stopped": "thread", "limit": cfg.max_wakes_without_human}
    batch = {k: e for k, e in batch.items() if not hosted_wake._stopped(rec, e[0])}
    if not batch:
        return
    rec["queued"] = {"posts": sorted(int(k) for k in batch), "threads": sorted({e[0] for e in batch.values()}),
                     "created_at": now}
    rec["sent"].append(now)
    for tid in rec["queued"]["threads"]:
        hosted_wake._thread(c, rec, tid)["count"] += 1
    for k in batch:
        rec["woken"][k] = now
        del rec["pending"][k]
    log.info("waiter %s: wake queued for %d post(s)", key, len(batch))


def _hand(board: Board, c, rec: dict, p: Principal, now: float) -> str | None:
    """The queued wake, checked again as it is handed (hosted_wake._recheck's rule): posts that stopped qualifying are
    taken off it and lose their coverage; with none left it is not sent and not counted. Returns its text."""
    m, rec["queued"] = rec["queued"], None
    keep = {} if _hosted_root(c, rec["agent"], rec["project"]) else \
        hosted_wake._qualifying(board, p, rec["project"], [i for i in m["posts"] if type(i) is int], rec)
    for k in (str(i) for i in m["posts"]):
        if k in keep:
            rec["handed"][k] = now      # the grace period counts from here
        else:
            for table in ("woken", "handed", "read"):
                rec[table].pop(k, None)
    if not keep:
        hosted_wake._uncount(rec, m)
        return None
    return hosted_wake.wake_text(rec["agent"], list(keep.values()))


def _checked(body: Any) -> dict:
    if not isinstance(body, dict):
        raise Invalid("a JSON object is required")
    sid, nonce, client = body.get("client_session_id"), body.get("nonce"), body.get("client") or CLIENTS[0]
    if not isinstance(sid, str) or not UUID_RE.fullmatch(sid):
        raise Invalid("client_session_id must be the conversation's UUID")
    if not isinstance(nonce, str) or not NONCE_RE.fullmatch(nonce):
        raise Invalid("nonce must be 32 lowercase hex digits, new for each waiting process")
    if client not in CLIENTS:
        raise Invalid("client must be one of " + ", ".join(CLIENTS))
    projects = body.get("projects")
    if (not isinstance(projects, list) or not 1 <= len(projects) <= MAX_PROJECTS
            or not all(isinstance(x, str) and x.strip().startswith("/") for x in projects)):
        raise Invalid(f"projects must list 1 to {MAX_PROJECTS} absolute paths")
    return {"client_session_id": sid, "nonce": nonce, "client": client,
            "projects": list(dict.fromkeys(_norm_path(x) for x in projects))}


def poll(board: Board, p: Principal, body: Any) -> dict:
    """One look for this waiter: "wake" with the text, "idle" (nothing yet), "superseded" (a newer process of the
    same conversation took over) or "off". Also its heartbeat: the record says the waiter is connected."""
    if not enabled(board):
        return OFF
    if p.is_human:
        raise Forbidden("only an agent's own session waits for wakes")
    body = _checked(body)
    key, conn = _key(p.name, body["client_session_id"]), board.conn
    now = board.now()
    rec = _load(conn, key)
    if rec is not None and body["nonce"] in rec["retired"]:
        return SUPERSEDED
    if board.is_paused():
        return IDLE         # nothing is decided or written while paused; the posts are found after the unpause
    if (rec is not None and rec["nonce"] == body["nonce"] and rec["queued"] is None and not rec["pending"]
            and now - rec["seen"] < HEARTBEAT_SECONDS and hosted_wake._tops(conn)[0] <= rec["mark"]):
        return IDLE         # nothing new since the last look: no write
    cfg = hosted_wake.limits(board)
    with db.write_tx(conn) as c:
        rec = _load(c, key)
        if board.is_paused():
            return IDLE     # paused meanwhile
        if rec is None:
            rec = _new(c, p, body, _pick(c, body["projects"]), now)
            _evict(c, p.name, now)
        elif body["nonce"] in rec["retired"]:
            return SUPERSEDED
        elif rec["nonce"] != body["nonce"]:
            rec["retired"].append(rec["nonce"])
            rec |= {"nonce": body["nonce"], "armed_at": now, "project": _pick(c, body["projects"])}
        rec["seen"] = now
        _advance(board, c, key, rec, p, cfg, now)
        text = _hand(board, c, rec, p, now) if rec["queued"] is not None else None
        _save(c, key, rec, now)
    return {"state": "wake", "text": text} if text else IDLE


def _evict(c, agent: str, now: float) -> None:
    recs = _records(c, agent)
    for key, _ in sorted(recs.items(), key=lambda kv: kv[1]["seen"])[:max(0, len(recs) + 1 - MAX_WAITERS)]:
        c.execute("DELETE FROM board_state WHERE key = ?", (key,))


async def wait(board: Board, p: Principal, body: Any, gone: Callable[[], Any] | None = None) -> dict:
    """`poll`, again every POLL_SECONDS for up to `wait` seconds (at most WAIT_MAX) until it is not "idle". The
    deadline is wall time, not the board's clock (hosted._poll's rule); `gone` ends the wait early."""
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
    """Each dispatcher pass, just before it decides what to launch: queue the wakes that are due for connected
    waiters, so that `holds` sees them; drop waiters gone for KEEP_SECONDS. Off: records left from when it was on
    are dropped (none: one read, no write). Never raises."""
    try:
        recs = _records(board.conn)
        if not recs:
            return
        now, on = board.now(), enabled(board)
        if on and board.is_paused():
            return
        cfg, top = hosted_wake.limits(board), hosted_wake._tops(board.conn)[0]
        for key, rec in recs.items():
            if not on or now - rec["seen"] > KEEP_SECONDS:
                with db.write_tx(board.conn) as c:
                    c.execute("DELETE FROM board_state WHERE key = ?", (key,))
                continue
            if not _present(rec, now) or rec["queued"] is not None or (top <= rec["mark"] and not rec["pending"]):
                continue    # nobody to hand it to, a wake already waits, or nothing new: no write
            with db.write_tx(board.conn) as c:
                rec = _load(c, key)
                p = hosted_wake._agent(c, rec["agent"]) if rec else None
                if rec is None or p is None:
                    continue
                _advance(board, c, key, rec, p, cfg, now)
                _save(c, key, rec, now)
    except Exception:
        hosted_wake._complain("interactive wakes: the dispatcher's pass failed")


def _reader(conn, session_id: Any, agent: str, rec: dict) -> bool:
    """A board session that can be this waiter's own: hosted_wake._own_reader (the agent's, registered in the
    project, not a dispatched run's), and not one that recorded another conversation as its own."""
    if not hosted_wake._own_reader(conn, session_id, agent, rec["project"]):
        return False
    row = conn.execute("SELECT client_session_id FROM sessions WHERE id = ?", (session_id,)).fetchone()
    return row is None or row["client_session_id"] in (None, rec["client_session_id"])


def holds(conn, agent: str, project: Any, post_id: Any, now: float) -> bool:
    """A waiter of `agent` in `project` has this request (hosted_wake.holds' rule, on a waiter's record): a wake
    covering it was queued or handed less than `wake_grace_seconds` ago, or one of its board sessions was shown it
    after that wake (a read mark or a delivery receipt). A request the dispatcher reserved is never held. Without
    waiter records: one read."""
    if type(post_id) is not int or not isinstance(project, str):
        return False
    project, k = project.rstrip("/") or "/", str(post_id)
    for rec in _records(conn, agent).values():
        if rec["project"] != project or k in rec["claimed"]:
            continue
        at = rec["woken"].get(k)
        if not _num(at):
            continue
        handed_at = rec["handed"].get(k)
        if now - max(at, handed_at if _num(handed_at) else at) < rec["grace"]:
            return True
        if hosted_wake._shadowed(conn, {"agent": agent, "project": project}, at):
            continue
        read = rec["read"].get(k)
        if _num(read) and read >= at:
            return True
        from . import delivery
        if delivery.read_since(conn, post_id, agent, at, lambda sid, rec=rec: _reader(conn, sid, agent, rec)):
            return True
    return False


def claim(conn, agent: str, project: Any, post_id: Any, now: float) -> None:
    """Inside the dispatcher's reservation (hosted_wake.reserve): the request is the dispatcher's from here on. It
    leaves the waiting posts and the coverage of every waiter of the agent in the project. Writes nothing without
    one."""
    if type(post_id) is not int or not isinstance(project, str):
        return
    project, k = project.rstrip("/") or "/", str(post_id)
    for key, rec in _records(conn, agent).items():
        if rec["project"] != project:
            continue
        for table in ("pending", "woken", "handed", "read"):
            rec[table].pop(k, None)
        rec["claimed"][k] = now
        _save(conn, key, rec, now)


def handed(board: Board, p: Principal, session_id: int, posts: list[dict]) -> None:
    """Where the board returns posts to a reader (beside hosted_wake.handed): note, for `holds`, that a board session
    that can be a waiter's own was shown a post a wake of that waiter covered. Never raises; without a waiter of the
    reader it reads one key range and writes nothing."""
    try:
        ids = [] if p.is_human else [d["id"] for d in posts if p.name in (d.get("to") or [])]
        recs = _records(board.conn, p.name) if ids else {}
        now = board.now()
        for key, rec in recs.items():
            if not _reader(board.conn, session_id, p.name, rec) or not hosted_wake._unmarked(rec, ids):
                continue
            with db.write_tx(board.conn) as c:
                rec = _load(c, key)
                todo = hosted_wake._unmarked(rec, ids) if rec else []
                todo = [k for k in todo if not hosted_wake._shadowed(c, rec, rec["woken"][k])]
                for k in todo:
                    rec["read"][k] = now
                if todo:
                    _save(c, key, rec, now)
    except Exception:
        hosted_wake._complain("interactive wakes: could not note what a session was shown")
