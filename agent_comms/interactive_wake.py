"""Waking a session the human opened himself when a board post is addressed to its agent (DESIGN_NOTES "Waking an
interactive session", and its review rounds 1 and 2).

A hosted session (hosted_wake.py) is the hub's own process, so the hub can write a wake into it. A session the human
opened in a terminal or the Claude desktop app is not: the hub cannot reach it. What it can do is answer a call that
the session's side keeps open. Claude Code runs a `Stop` hook with `"asyncRewake": true` in the background when a turn
ends, and wakes the idle session at once when that hook exits with code 2, showing it the hook's stderr. That hook is
`board wait` (waiter.py), which long-polls `POST /api/wait` here and exits 2 only when this module says wake. Nothing
here assumes Claude: a waiter is any client that keeps such a call open for one conversation (`client`,
`client_session_id`), so a Codex equivalent can use the same route.

Records (`board_state`)
- *Waiter* `wake.waiter.<agent>.<conversation id>`: the conversation's current process (`nonce`, `armed_at`, `seen`),
  its project, its marks over the posts table and its `pending` posts (hosted_wake._look's fields), the posts it was
  delivered and was shown afterwards (`delivered`, `read`), the posts the dispatcher claimed, and at most one `offer`.
- *Pair* `wake.pair.<agent>.<hash of the project>`: the agent's wake limits in the project (`sent`, `threads`:
  hosted_wake._lift's and `_thread`'s fields), the posts delivered to any of its waiters (`delivered`, and to which
  conversation: `delivered_to`), the reads of them by a session registered in the project (`read`, the first, and
  `readers`, the board sessions that read each), the requests the
  dispatcher reserved (`claimed`, also when no waiter is left in the project), and one hold deadline per request
  (`holds`). A new conversation id resets none of it.

The offer: a state machine (every transition is ONE write transaction; `now` is read inside it)
- *Every write transaction* in this module reads the live setting right after it holds the lock, before it changes
  anything: off, it writes nothing (the dispatcher's clean-up of records left from when it was on excepted).
- *States.* NONE (no `offer`), and an offer {id, nonce, at, entries}, which is STANDING or STALE. It STANDS while all
  of these hold (`_stands`): the feature is on (the live setting, read inside the transaction); the board is not
  paused; `now - at < ACK_SECONDS`; its nonce is the
  waiter's current nonce; the waiter is the one of its pair to wake (`_chosen`: connected, armed last among the
  connected waiters of the pair, and the agent has no hosted root session in the project). Otherwise it is STALE.
  An offer is ISSUED once its text and id were handed to a process (O2). An issued offer's `entries` never change:
  its id always names the batch whose text it carried. One not issued yet may lose posts (O2, `claim`).
- *O1 offer* NONE -> STANDING (`_offer`, from `_advance`: a waiter's call, or a dispatcher pass). Re-checked inside:
  not paused; no other offer stands in the pair (stale ones are taken back first); the waiter is chosen; the pair's
  interval and hourly limit allow it; the posts still qualify (hosted_wake._qualifying) and none was delivered in
  the pair; threads at their per-thread maximum are left out. Sets the request's hold deadline once (`holds`).
- *O2 hand-over* STANDING -> STANDING, its text returned (`_hand`, a call of the offer's own nonce). Re-checked
  inside: it stands, and its posts still qualify. If one does not: an offer not issued yet just loses that post
  (none left: NONE); an issued one is taken back (O4), and the rest wait for a new offer with a new id.
- *O3 acknowledge* STANDING -> NONE, delivered (`_ack`, a call with `ack`). Re-checked inside, before anything
  changes: the id is the offer's, the caller's nonce is the offer's and the waiter's current one, the offer stands
  (the setting, the deadline, the pause, a hosted root, the waiter still chosen), and every post of its batch still
  qualifies (another session may have read past one since the hand-over). Accepted: the human's new posts lift
  their threads first (hosted_wake._lift), then the wake is charged once (`sent`, and +1 for each thread of its
  entries, no other), its posts are delivered on the waiter and on the pair, the offer is gone, and the id is kept
  as the waiter's last acknowledged one (`acked`: id, nonce, time). Otherwise the answer says why: "off",
  "superseded" (that process was replaced) or "rejected" (`board wait` then goes on waiting with the same nonce);
  nothing changes, except that a batch that no longer qualifies whole is taken back (O4: its id must not be
  acknowledged for fewer posts).
- *O3' replay* (`_ack`): the same nonce sending the same id again within REPLAY_SECONDS of its acceptance (its
  reply was lost or unreadable) is answered "acked" again, and nothing is charged or changed. On purpose (review
  round 4, S24 and S25 declined), this holds also after a newer process took the conversation over, and also once
  the board was paused after the acceptance: the hub had already committed that ack (its posts are delivered, the
  wake is charged), and refusing the replay would leave nobody woken for a charged wake (the newer process is never
  offered those posts). The replaced process wakes the same conversation, once; it is the answer it was owed. No
  double wake comes this way: a process whose first reply it could read has exited and never replays, and a replay
  changes and charges nothing. Only the nonce that made the ack, of that conversation's record, gets it.
- A delivered post is never offered again (review round 5 removed the earlier unread return). If Claude Code starts
  no turn for a wake, the session's own board check shows the post at its next prompt, and the dispatcher's grace
  applies to a request it could run (DESIGN_NOTES "Known Claude Code behaviour").
- *O4 take back* STALE (or STANDING, below) -> NONE (`_take_back`). Its posts go back to `pending` (except the
  claimed ones), nothing is charged. Made by: `_advance` for any offer of the pair that no longer stands; a nonce
  take-over (before the waiter is rebound to another project); `_hand` (above); `claim` of a post of an issued
  offer (its id must not stay valid for a smaller batch; an offer not issued yet just loses the post); a
  dispatcher pass for an offer that no longer stands.

Other rules
- *Off by default* (`[dispatch.sessions.interactive] wake`). Off, the route answers "off" before it reads or writes
  any board state, `holds`, `claim` and `handed` do nothing (a bare connection counts as off: callers pass the
  Board), and records left from when it was on are dropped by the dispatcher's next pass.
- *Arming.* A waiter is made on its first call, paused or not: it is woken only for posts made from then on. A call
  with a new nonce for the same conversation takes the record over; the older process is answered "superseded".
- *What wakes* is what wakes a hosted session (hosted_wake's helpers over channel.eligible): a post visible to the
  agent, addressed to it, by someone else who is an active identity, in a thread of the waiter's project, new since
  its mark, not read yet, not a managed continuation's, not reserved by the dispatcher.
- *Limits* (`[dispatch.sessions]`, the hosted numbers, per pair). At a maximum the posts wait: a thread's stop lifts
  when the human posts there, the hourly one when the hour has room. No Needs-you item is posted.
- *The dispatcher* (`holds`, `claim`, `handed`). A request is held from its first offer until `wake_grace_seconds`
  later (one deadline per request, kept across take-backs and re-offers), and after that only while a board session
  that can be the waiter's, and read it after it was delivered while registered in the post's project (the readers
  `handed` records; a delivery receipt alone does not count), is still live there: seen within the dispatcher's
  live_minutes, as `_policy_held` requires of its own readers. A waiter never offered the request holds nothing.
- *Text.* hosted_wake.wake_text: counts, thread ids and agent names only, plus the fixed note.
- *What it cannot stop.* Any process holding the agent's token can call as a waiter of that agent: the hub cannot
  tell it from the agent's own session. It can take that agent's wakes and hold a request back for one grace
  period, plus as long as a board session it registered in the project, and that read the request, stays live (what
  a live session of the agent that reads a request already does without this feature); it cannot reset the pair's
  limits or wait as another agent.
- *A limitation (PR review F2, left for later).* A waiter's pending posts come from its own marks. A post that
  waiter A holds back under a limit is never offered while a newer conversation B, armed after that post, is the
  chosen waiter (B's marks start above it), possibly for a day. It holds nothing, so the dispatcher treats a request
  among such posts as without the feature, and the prompt check reports it. The fix: merge the pair's pending posts
  into the chosen waiter at O1.
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
ACK_SECONDS = 10.0               # an offer not acknowledged in this long is taken back

OFF = {"state": "off"}
IDLE = {"state": "idle"}
SUPERSEDED = {"state": "superseded"}
ACKED = {"state": "acked"}
REJECTED = {"state": "rejected"}
REPLAY_SECONDS = 60.0            # the last accepted ack id is answered "acked" again for this long (its reply was lost)
MAX_READERS = 64                 # a hard cap on a delivered post's readers (overflow: the least recently seen go)
LIVE_MINUTES = 2.0               # [dispatch] live_minutes when the hub's value is missing or not a positive number


def enabled(board: Board) -> bool:
    """`[dispatch.sessions.interactive] wake` as the hub has it now (a table that does not validate: off)."""
    return bool(hosted_wake.limits(board).interactive.wake)


def live_seconds(board: Board) -> float:
    """`[dispatch] live_minutes` as the hub has it now, in seconds: how recently a board session must have been seen
    to count as live (the dispatcher's `_live` and `_policy_held` use the same window)."""
    v = (getattr(board.s, "dispatch", None) or {}).get("live_minutes", LIVE_MINUTES)
    return 60.0 * (float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0 else LIVE_MINUTES)


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
    a = raw.get("acked")
    if not (isinstance(a, dict) and isinstance(a.get("id"), str) and isinstance(a.get("nonce"), str)
            and _num(a.get("at"))):
        a = None
    return w | t | {
        "mark": raw["mark"], "post": raw["post"] if type(raw.get("post")) is int else None,
        "sealed": [i for i in raw.get("sealed") or [] if type(i) is int] if isinstance(raw.get("sealed"), list) else [],
        "pending": raw["pending"] if isinstance(raw.get("pending"), dict) else {},
        "claimed": _times(raw.get("claimed")), "delivered": _times(raw.get("delivered")),
        "read": _times(raw.get("read")), "offer": o, "acked": a,
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
    """The agent's limits in this project (the fields hosted_wake._lift and `_thread` read), the posts delivered to
    any of its waiters, and its holds: post id -> the deadline set by that request's first offer."""
    raw = _get(conn, _pair_key(agent, project))
    raw = raw if isinstance(raw, dict) and raw.get("agent") == agent and raw.get("project") == project else {}
    return {"agent": agent, "project": project,
            "sent": [t for t in raw.get("sent") or [] if _num(t)] if isinstance(raw.get("sent"), list) else [],
            "threads": raw["threads"] if isinstance(raw.get("threads"), dict) else {},
            "closing": [], "hour_ack": raw.get("hour_ack") if _num(raw.get("hour_ack")) else None,
            "delivered": _times(raw.get("delivered")), "holds": _times(raw.get("holds")),
            "read": _times(raw.get("read")), "claimed": _times(raw.get("claimed")),
            "readers": {k: [s for s in v if type(s) is int] for k, v in raw["readers"].items() if isinstance(v, list)}
            if isinstance(raw.get("readers"), dict) else {},
            "delivered_to": {k: v for k, v in raw["delivered_to"].items() if isinstance(v, str)}
            if isinstance(raw.get("delivered_to"), dict) else {},
            "seen": raw.get("seen") if _num(raw.get("seen")) else 0.0}


def _save_pair(c, pair: dict, now: float) -> None:
    pair["sent"] = hosted_wake._recent(pair["sent"], now, hosted_wake.HOUR)
    pair["delivered"] = hosted_wake._recent(pair["delivered"], now, hosted_wake.KEEP_WOKEN_SECONDS,
                                            hosted_wake.MAX_WOKEN)
    pair["holds"] = {k: t for k, t in pair["holds"].items() if t > now - hosted_wake.KEEP_WOKEN_SECONDS}
    for table in ("read", "claimed"):
        pair[table] = hosted_wake._recent(pair[table], now, hosted_wake.KEEP_WOKEN_SECONDS, hosted_wake.MAX_WOKEN)
    pair["delivered_to"] = {k: v for k, v in pair["delivered_to"].items()
                            if k in pair["delivered"] or k in pair["read"]}
    pair["readers"] = {k: v[-MAX_READERS:] for k, v in pair["readers"].items() if k in pair["read"] and v}
    pair["closing"], pair["seen"] = [], now
    _put(c, _pair_key(pair["agent"], pair["project"]), pair, HUB, now)


def _new(c, p: Principal, body: dict, now: float) -> dict:
    top, top_id = hosted_wake._tops(c)
    return {"agent": p.name, "client": body["client"], "client_session_id": body["client_session_id"],
            "project": body["projects"][0], "nonce": body["nonce"], "started_at": now, "armed_at": now, "seen": now,
            "mark": top, "post": top_id, "sealed": [], "pending": {}, "claimed": {}, "delivered": {}, "read": {},
            "offer": None, "acked": None, "retired": []}


def _present(rec: dict, now: float) -> bool:
    return now - rec["seen"] <= PRESENT_SECONDS


# ---------------------------------------------------------------- who is woken, and whether an offer stands


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


def _stands(board: Board, c, key: str, rec: dict, now: float, recs: dict | None = None) -> bool:
    """The state machine's STANDING (module docstring), on what is stored now and the setting as it is now."""
    o = rec["offer"]
    return (o is not None and now - o["at"] < ACK_SECONDS and o["nonce"] == rec["nonce"] and enabled(board)
            and not board.is_paused() and _chosen(c, key, rec, now, recs))


def _take_back(c, rec: dict) -> None:
    """O4: the offer never reached a session. Its posts wait again in `pending` (re-qualified before the next offer,
    as hosted_wake._set_aside does); it was never charged, so nothing is uncounted."""
    o, rec["offer"] = rec["offer"], None
    for k, e in (o or {}).get("entries", {}).items():
        if k not in rec["claimed"] and isinstance(e, list):
            rec["pending"][k] = e
    if o:
        log.info("waiter: an offered wake was not taken up; its %d post(s) wait again", len(o.get("entries", {})))


# ---------------------------------------------------------------- the request


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


# ---------------------------------------------------------------- the transitions


def _advance(board: Board, c, key: str, rec: dict, p: Principal, cfg, now: float) -> None:
    """Inside the caller's write transaction, not paused: take in the posts above the waiter's marks, lift the stop of
    threads the human posted in, take back (O4) every offer of the pair that no longer stands, and make the offer that
    is due (O1). Saves the pair and the other waiters it changes, not `rec`."""
    top, top_id = hosted_wake._tops(c)
    pair = _pair(c, rec["agent"], rec["project"])
    if top > rec["mark"]:
        hosted_wake._look(board, c, rec, {"project": rec["project"]}, p, top, top_id)
        hosted_wake._lift(c, pair, max(pair["sent"], default=None))
    recs = _records(c, rec["agent"], rec["project"]) | {key: rec}
    for k, r in recs.items():
        if r["offer"] is not None and not _stands(board, c, k, r, now, recs):
            _take_back(c, r)
            if k != key:
                _save(c, k, r, now)
    if _hosted_root(c, rec["agent"], rec["project"]):
        rec["pending"] = {}     # the hosted session's posts: they never wake a waiter, also once it has ended
    elif rec["pending"] and enabled(board) and not any(r["offer"] is not None for r in recs.values()):
        sent = hosted_wake._recent(pair["sent"], now, hosted_wake.HOUR)
        if (_chosen(c, key, rec, now, recs) and len(sent) < cfg.max_wakes_per_hour
                and not (sent and now - max(sent) < cfg.wake_min_interval_seconds)):
            _offer(board, c, key, rec, pair, p, cfg, now)
    _save_pair(c, pair, now)


def _offer(board: Board, c, key: str, rec: dict, pair: dict, p: Principal, cfg, now: float) -> None:
    """O1. Never a post delivered in the pair or reserved by the dispatcher there."""
    rec["pending"] = {k: e for k, e in hosted_wake._qualifying(
        board, p, rec["project"], [int(k) for k in rec["pending"]], rec).items()
        if k not in pair["delivered"] and k not in pair["claimed"]}
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
        pair["holds"].setdefault(k, now + float(cfg.wake_grace_seconds))    # one deadline per request
    log.info("waiter %s: wake offered for %d post(s)", key, len(batch))


def _hand(board: Board, c, key: str, rec: dict, p: Principal, now: float) -> dict | None:
    """O2: the answer that hands the waiter's standing offer to its process. A post that no longer qualifies takes the
    whole offer back (O4): the id must keep naming the batch its text counts."""
    o = rec["offer"]
    keep = hosted_wake._qualifying(board, p, rec["project"], [int(k) for k in o["entries"]], rec)
    if set(keep) != set(o["entries"]):
        if o.get("issued"):
            _take_back(c, rec)
            rec["pending"] = {k: e for k, e in rec["pending"].items() if k in keep or k not in o["entries"]}
            return None
        o["entries"] = {k: e for k, e in o["entries"].items() if k in keep}
        if not o["entries"]:
            rec["offer"] = None
            return None
    o["issued"] = True
    return {"state": "wake", "text": hosted_wake.wake_text(rec["agent"], list(o["entries"].values())), "id": o["id"]}


def _ack(board: Board, p: Principal, key: str, body: dict) -> dict:
    """O3 (and O3'), all of it inside one write transaction with the clock and the setting read there."""
    with db.write_tx(board.conn) as c:
        now = board.now()
        if not enabled(board):
            return OFF          # off: no ack is answered "acked", not even a replay
        rec = _load(c, key)
        a = rec["acked"] if rec is not None else None
        if a and a["id"] == body["ack"] and a["nonce"] == body["nonce"] and now - a["at"] < REPLAY_SECONDS:
            return ACKED        # O3': the reply to this very ack was lost; nothing is charged again
        if rec is not None and body["nonce"] in rec["retired"]:
            return SUPERSEDED
        o = rec["offer"] if rec is not None else None
        if (o is None or o["id"] != body["ack"] or o["nonce"] != body["nonce"] or rec["nonce"] != body["nonce"]
                or not _stands(board, c, key, rec, now)):
            return REJECTED
        keep = hosted_wake._qualifying(board, p, rec["project"], [int(k) for k in o["entries"]], rec)
        if set(keep) != set(o["entries"]):
            # Read past (or otherwise out) since the hand-over: this id no longer names a batch that may wake.
            _take_back(c, rec)
            rec["pending"] = {k: e for k, e in rec["pending"].items() if k in keep or k not in o["entries"]}
            _save(c, key, rec, now)
            return REJECTED
        pair = _pair(c, rec["agent"], rec["project"])
        hosted_wake._lift(c, pair, max(pair["sent"], default=None))     # the human's new posts first
        pair["sent"].append(now)
        for tid in sorted({e[0] for e in o["entries"].values()}):
            hosted_wake._thread(c, pair, tid)["count"] += 1
        for k in o["entries"]:
            rec["delivered"][k] = pair["delivered"][k] = now
            pair["delivered_to"][k] = rec["client_session_id"]
        rec["offer"], rec["acked"] = None, {"id": o["id"], "nonce": o["nonce"], "at": now}
        _save(c, key, rec, now)
        _save_pair(c, pair, now)
    log.info("waiter %s: wake delivered", key)
    return ACKED


def poll(board: Board, p: Principal, body: Any) -> dict:
    """One look for this waiter: "wake" with the text and its id, "idle" (nothing yet), "superseded" (a newer process
    of the same conversation took over) or "off"; with `ack`, "acked", "rejected" or "superseded". Also its
    heartbeat."""
    if not enabled(board):
        return OFF
    if p.is_human:
        raise Forbidden("only an agent's own session waits for wakes")
    body = _checked(body)
    key, conn = _key(p.name, body["client_session_id"]), board.conn
    if body["ack"] is not None:
        return _ack(board, p, key, body)
    seen_at = board.now()
    rec = _load(conn, key)
    if rec is not None and body["nonce"] in rec["retired"]:
        return SUPERSEDED
    if (rec is not None and rec["nonce"] == body["nonce"] and seen_at - rec["seen"] < HEARTBEAT_SECONDS
            and (board.is_paused() or (rec["offer"] is None and not rec["pending"]
                                       and hosted_wake._tops(conn)[0] <= rec["mark"]))):
        return IDLE         # nothing new since the last look: no write
    cfg = hosted_wake.limits(board)
    with db.write_tx(conn) as c:
        if not enabled(board):
            return OFF          # turned off while this call waited for the lock: nothing is written
        now = board.now()
        rec = _load(c, key)
        if rec is None:
            # The mark is taken now, paused or not: posts made from here on are this session's.
            rec = _new(c, p, body, now)
            _evict(c, p.name, now)
        elif body["nonce"] in rec["retired"]:
            return SUPERSEDED
        elif rec["nonce"] != body["nonce"]:
            # A take-over: the old process's offer goes back first (O4), then the waiter may be rebound.
            _take_back(c, rec)
            rec["retired"].append(rec["nonce"])
            rec |= {"nonce": body["nonce"], "armed_at": now}
            if rec["project"] != body["projects"][0]:
                # Another project: a new mark there (its history is not this session's), the old pending posts go.
                top, top_id = hosted_wake._tops(c)
                rec |= {"project": body["projects"][0], "pending": {}, "mark": top, "post": top_id, "sealed": []}
        rec["seen"] = now
        out = None
        if not board.is_paused():       # paused: nothing is decided; the posts are found after the unpause
            _advance(board, c, key, rec, p, cfg, now)
            if rec["offer"] is not None and rec["offer"]["nonce"] == body["nonce"] and _stands(board, c, key, rec, now):
                out = _hand(board, c, key, rec, p, now)
        _save(c, key, rec, now)
    return out or IDLE


def _evict(c, agent: str, now: float) -> None:
    recs = _records(c, agent)
    for key, _ in sorted(recs.items(), key=lambda kv: kv[1]["seen"])[:max(0, len(recs) + 1 - MAX_WAITERS)]:
        c.execute("DELETE FROM board_state WHERE key = ?", (key,))


async def wait(board: Board, p: Principal, body: Any, gone: Callable[[], Any] | None = None) -> dict:
    """`poll`, again every POLL_SECONDS for up to `wait` seconds (at most WAIT_MAX) until it is not "idle". The
    deadline is wall time, not the board's clock (hosted._poll's rule); `gone` ends the wait early when it can tell,
    checked before each sleep and again after it: a caller gone meanwhile gets no further `poll` (no heartbeat, no
    offer). It cannot always tell (a proxy may hold the connection), so the ack and WAIT_MAX stay the safety net."""
    import anyio
    from anyio import to_thread

    seconds = hosted._wait_seconds(body.get("wait", 0) if isinstance(body, dict) else 0, WAIT_MAX)
    deadline = time.monotonic() + seconds
    out = await to_thread.run_sync(poll, board, p, body)
    while out == IDLE and time.monotonic() < deadline and not (gone is not None and await gone()):
        await anyio.sleep(max(0.0, min(POLL_SECONDS, deadline - time.monotonic())))
        if gone is not None and await gone():
            break
        out = await to_thread.run_sync(poll, board, p, body)
    return out


# ---------------------------------------------------------------- the dispatcher's side


def tick(board: Board) -> None:
    """Each dispatcher pass, just before it decides what to launch: make the offers that are due to connected waiters
    (so that `holds` sees them), take back offers that no longer stand, drop waiters and pairs gone for KEEP_SECONDS.
    Off: records left from when it was on are dropped (none: one read, no write). Never raises."""
    try:
        recs = _records(board.conn)
        pairs = [k for (k,) in board.conn.execute("SELECT key FROM board_state WHERE key >= ? AND key < ?",
                                                   hosted._range(PAIR))]
        if not recs and not pairs:
            return
        if not enabled(board):
            with db.write_tx(board.conn) as c:
                if not enabled(board):          # still off now that the lock is held
                    for key in list(recs) + pairs:
                        c.execute("DELETE FROM board_state WHERE key = ?", (key,))
            return
        cfg = hosted_wake.limits(board)
        for key in recs:
            with db.write_tx(board.conn) as c:
                if not enabled(board):
                    return          # turned off meanwhile: this pass writes nothing more
                now = board.now()
                rec = _load(c, key)
                if rec is None:
                    continue
                if now - rec["seen"] > KEEP_SECONDS:
                    c.execute("DELETE FROM board_state WHERE key = ?", (key,))
                    continue
                if board.is_paused():
                    continue        # nothing is decided while paused (checked in this transaction)
                if rec["offer"] is None and (not _present(rec, now) or (
                        hosted_wake._tops(c)[0] <= rec["mark"] and not rec["pending"])):
                    continue        # nobody to offer it to, or nothing new: no write
                p = hosted_wake._agent(c, rec["agent"])
                if p is None:
                    continue
                _advance(board, c, key, rec, p, cfg, now)
                _save(c, key, rec, now)
        live = {_pair_key(r["agent"], r["project"]) for r in _records(board.conn).values()}
        for key in pairs:
            if key not in live:
                with db.write_tx(board.conn) as c:
                    if not enabled(board):
                        return
                    pr = _get(c, key)
                    if not isinstance(pr, dict) or board.now() - float(pr.get("seen") or 0) > KEEP_SECONDS:
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


def _pair_reader(conn, session_id: Any, agent: str, project: str, convs: set[str]) -> bool:
    """A board session that can be the own session of a waiter the post was delivered to: hosted_wake._own_reader
    (the agent's, registered in the pair's project, not a dispatched run's), and not one that recorded a conversation
    other than those."""
    if not hosted_wake._own_reader(conn, session_id, agent, project):
        return False
    row = conn.execute("SELECT client_session_id FROM sessions WHERE id = ?", (session_id,)).fetchone()
    return row is None or row["client_session_id"] is None or row["client_session_id"] in convs


def _delivery(conn, agent: str, project: str, k: str) -> tuple[float | None, dict, dict]:
    """When the post was first delivered in the pair, the pair, and the waiter records it was delivered to. Every
    project's waiters count: one that moved on to another project keeps what it was delivered and what it read."""
    pair = _pair(conn, agent, project)
    recs = {key: r for key, r in _records(conn, agent).items() if k in r["delivered"]}
    ats = [r["delivered"][k] for r in recs.values()] + ([pair["delivered"][k]] if k in pair["delivered"] else [])
    return (min(ats) if ats else None), pair, recs


def _eligible_readers(conn, agent: str, project: str, sessions: list[int], since: float) -> list[int]:
    """Of a post's readers, those that can still hold it: the agent's, registered in the project, not a dispatched
    run's, seen at or after `since`; least recently seen first. A reader that still can is never dropped for a new
    one (S33): only those that no longer can are pruned, here on every write of the list, and the list keeps the
    post's own retention. Past MAX_READERS, which only that many live sessions reading one post reach, the least
    recently seen go."""
    seen = {}
    for sid in dict.fromkeys(sessions):
        row = conn.execute("SELECT last_seen FROM sessions WHERE id = ?", (sid,)).fetchone()
        if row is not None and row["last_seen"] >= since and hosted_wake._own_reader(conn, sid, agent, project):
            seen[sid] = row["last_seen"]
    return sorted(seen, key=lambda s: seen[s])[-MAX_READERS:]


def read_after_delivery(conn, agent: str, project: str, post_id: int, now: float, window: float) -> bool:
    """A board session that can be the own session of a waiter the post was delivered to was shown it after that
    delivery, and is live: one of the post's readers on the pair, which `handed` records only for a reader registered
    in the post's project at the time of the read, is still the agent's in this project, not a dispatched run's, and
    was seen within `window` (the dispatcher's live_minutes; PR review F1, as `_policy_held` bounds its own
    read-based hold). A session that read the post and went away holds nothing once it is no longer live; another
    live session of the agent that did not read it holds nothing either. A delivery receipt alone is no evidence
    (review round 6, S30): it names the session, not the project the session was registered in when it read. Both
    read paths that write receipts also call `handed`."""
    k = str(post_id)
    at, pair, _recs = _delivery(conn, agent, project, k)
    if at is None or hosted_wake._shadowed(conn, {"agent": agent, "project": project}, at):
        return False
    if pair["read"].get(k, -1.0) < at:
        return False
    return any(hosted_wake._own_reader(conn, sid, agent, project) and conn.execute(
        "SELECT 1 FROM sessions WHERE id = ? AND last_seen >= ?", (sid, now - window)).fetchone() is not None
        for sid in pair["readers"].get(k, []))


def holds(board_or_conn: Any, agent: str, project: Any, post_id: Any, now: float) -> bool:
    """A waiter of `agent` in `project` has this request (hosted_wake.holds' rule, on the pair and its waiters):
    until the deadline its first offer set, and after that only while a board session that can be a waiter's own,
    and was shown it after it was delivered, stays live (`read_after_delivery`). A request the dispatcher reserved,
    and one no waiter was ever offered, is never held. Off, or without a pair: nothing."""
    conn, on = _live(board_or_conn)
    if not on or type(post_id) is not int or not isinstance(project, str):
        return False
    project, k = project.rstrip("/") or "/", str(post_id)
    pair = _pair(conn, agent, project)
    until = pair["holds"].get(k)
    if until is None or k in pair["claimed"]:
        return False        # never offered in the pair, or the dispatcher's
    recs = _records(conn, agent, project)
    if any(k in r["claimed"] for r in recs.values()):
        return False
    return now < until or read_after_delivery(conn, agent, project, post_id, now, live_seconds(board_or_conn))


def claim(board_or_conn: Any, agent: str, project: Any, post_id: Any, now: float) -> None:
    """Inside the dispatcher's reservation (hosted_wake.reserve): the request is the dispatcher's from here on. It
    leaves every waiter of the pair (waiting, delivered, read) and the pair's holds, and is recorded on the pair as
    claimed (also when no waiter is left there); an offer that covers it is taken back whole (O4), so that its id is
    never acknowledged for a batch it no longer is. Off, or without a waiter or pair: nothing is written. It opens
    no transaction of its own: the setting is read here, inside the caller's."""
    conn, on = _live(board_or_conn)
    if not on or type(post_id) is not int or not isinstance(project, str):
        return
    project, k = project.rstrip("/") or "/", str(post_id)
    recs = _records(conn, agent, project)
    for key, rec in recs.items():
        rec["claimed"][k] = now
        if rec["offer"] is not None and k in rec["offer"]["entries"]:
            if rec["offer"].get("issued"):
                _take_back(conn, rec)
            else:
                del rec["offer"]["entries"][k]
                if not rec["offer"]["entries"]:
                    rec["offer"] = None
        for table in ("pending", "delivered", "read"):
            rec[table].pop(k, None)
        _save(conn, key, rec, now)
    if recs or _get(conn, _pair_key(agent, project)) is not None:
        # On the pair too, also when no waiter is left in the project (the one that was offered it moved on): a
        # reserved request is never held or offered again in the pair.
        pair = _pair(conn, agent, project)
        pair["holds"].pop(k, None)
        pair["claimed"][k] = now
        _save_pair(conn, pair, now)


def handed(board: Board, p: Principal, session_id: int, posts: list[dict]) -> None:
    """Where the board returns posts to a reader (beside hosted_wake.handed): note, for `holds`, that a board session
    that can be the own session of a waiter a post was delivered to was shown it. Only a post of a thread in the
    project the reader is registered in counts (hosted_wake._own_reader's rule): a read in another project is no sign
    the request is being worked on where it belongs. The mark is kept on the pair (and on the waiter, while the
    waiter is still in that project), so it survives the waiter moving on. Off, or without a delivered post among
    them: one settings read and a few reads, no write. Never raises."""
    try:
        if p.is_human or not enabled(board):
            return
        ids = [str(d["id"]) for d in posts if p.name in (d.get("to") or [])]
        row = board.conn.execute("SELECT project FROM sessions WHERE id = ?", (session_id,)).fetchone() \
            if ids and type(session_id) is int else None
        if row is None:
            return
        project = row["project"].rstrip("/") or "/"
        ids = [k for k in ids if (board.conn.execute(
            "SELECT t.project FROM posts p JOIN threads t ON t.id = p.thread_id WHERE p.id = ?", (int(k),)).fetchone()
            or [None])[0] == project]
        todo = []
        for k in ids:
            at, pair, recs = _delivery(board.conn, p.name, project, k)
            convs = {r["client_session_id"] for r in recs.values()} | (
                {pair["delivered_to"][k]} if k in pair["delivered_to"] else set())
            if at is not None and (pair["read"].get(k, -1.0) < at or session_id not in pair["readers"].get(k, [])) \
                    and _pair_reader(board.conn, session_id, p.name, project, convs):
                todo.append(k)
        if not todo:
            return
        with db.write_tx(board.conn) as c:
            if not enabled(board):
                return          # turned off while this read waited for the lock: no read mark
            now, window = board.now(), live_seconds(board)
            pair = _pair(c, p.name, project)
            marked = [k for k in todo if not hosted_wake._shadowed(c, pair, _delivery(c, p.name, project, k)[0])]
            for k in marked:
                if pair["read"].get(k, -1.0) < _delivery(c, p.name, project, k)[0]:
                    pair["read"][k] = now
                # Who read it: `holds` after the deadline asks whether one of them is still live (PR review F1).
                pair["readers"][k] = _eligible_readers(
                    c, p.name, project, [s for s in pair["readers"].get(k, []) if s != session_id] + [session_id],
                    now - window)
            for key, rec in _records(c, p.name, project).items():
                mine = [k for k in marked if k in rec["delivered"] and rec["read"].get(k, -1.0) < rec["delivered"][k]
                        and _reader(c, session_id, p.name, rec)]
                for k in mine:
                    rec["read"][k] = now
                if mine:
                    _save(c, key, rec, now)
            if marked:
                _save_pair(c, pair, now)
    except Exception:
        hosted_wake._complain("interactive wakes: could not note what a session was shown")
