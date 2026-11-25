"""Mid-turn steering, the hub's side (DESIGN_NOTES "Mid-turn steering, loose contract: design (2026-11-24)", parts A,
G, H and J; step 2 as built: "Mid-turn steering, step 2: Codex steering, as built").

A steer S is sent with `mode: steer` by the sender of the turn N in flight and is bound to N. It is not a message: it
lives in its own record, `host.steer.<session>.<N>`, which no older build reads, so an older hub or node never sees
one. One steer per N, one unsettled per session.

- Accepted or refused at once, in the write transaction of the send (`target`, then `accept`), after the request-id
  lookup. The refusals are H's, each "cannot steer into session {id}: {why}".
- Offered to N's node only from the listing's write transaction (`offer`, from hosted.node_sessions' `deliver`), and
  EVERY offer revalidates first: S unsettled; N delivered to this node, not cancelled, not expired; no dispatched run
  or claim holds the checkout; the fetch speaks protocol 5. A failed check sets `withdrawn_at` (one-way: a withdrawn
  steer is never offered again). The first offer sets `listed_at`, the evidence that S may have been exposed. The
  listing shows S only from an offer committed in that very transaction (`shown`), on every path.
- Settled once, with N, on every path that settles N (`settle`): the node's end of N with or without the owning
  host's extension, a replacement node's LOST end, `_settle` for a lost node, `_end` for a close, cascade or orphan,
  the re-upgrade sweep. One precedence: (1) a valid extension from the owning host on N's own end decides (`in_turn`
  only if S was ever listed); (2) without one, `unconfirmed` if S was ever listed, else `not_delivered`. After that
  nothing revises N or S. N's native fields: from a committed `held` checkpoint (Claude's hold, step 3) when N's
  record has one, on every path; otherwise from the settling event or path (`native`).
- The checkpoint (`checkpoint`, step 3): N's native end while its host holds it, write-once, for the exact assigned
  node, a delivered N in flight and a listed, unsettled steer into it; refused once N is settled. Kept on N's record
  as `held`, out of every view, verdict and claim, and cleared in the transaction that settles N.
- Read by its sender only: the human through `show` and `GET .../messages/{N}?steer=1`, a parent through
  `board_session_status` (`view`).
"""
from __future__ import annotations

import json
import logging
import re
import uuid
from typing import Any

from . import hosted
from .core import Board, Conflict
from .nodes import _get, _put

log = logging.getLogger(__name__)

STEER_PREFIX = "host.steer."        # <session id>.<N>: the steer bound to message N (one per N)
STEER_PROTOCOL = 5                  # a node's `tree` that knows steers (node_sessions.SESSION_PROTOCOL)
STEER_MAX_BYTES = 12_288            # the steer's text as `json.dumps` writes it (after a parent's frame), part D
OUTCOMES = ("in_turn", "next_turn", "not_delivered", "unconfirmed")
# What a host may say on N's end (the extension). `next_turn` (step 3, Claude): S ran as a turn of its own after N, and
# the extension carries that turn's own result.
HOST_OUTCOMES = ("in_turn", "next_turn", "not_delivered", "unconfirmed")
REPLY_LIMIT = 16 * 1024 + 200       # a reply as a node sends it (providers.cap_reply), with the cut's note
WHY_RE = re.compile(r"[a-z_]{1,40}")
NEVER_LISTED = "never_listed"       # the hub's own word: rule (2), S never reached a node
NO_WORD = "no_word"                 # ... rule (2), listed, and no word of the owning host on N's end
FEATURE = "steer"                   # named in the listing's `features`: the node then sends the extension
STEER_WHAT = "A message sent while your turn was running"
STEER_LINE = ("If you are still working on that turn, take this into account there; if it reaches you as a new turn, "
              "do the work it asks.")
REFUSED = {
    "off": "steering is switched off on this hub ([dispatch.sessions] steer = false)",
    "unreadable": "steering is off on this hub: its [dispatch.sessions] settings could not be read",
    "misplaced": ("steering is off on this hub: its settings set steer directly under [dispatch]; the switch is "
                  "[dispatch.sessions] steer"),
    "rejected": ("steering is off on this hub: its settings were rejected at the last reload (fix board.toml or "
                 "board.local.toml; until they load, no new steer is taken)"),
    "ended": "the session has ended",
    "closing": "the session is being closed",
    "protocol": ("its node {node} speaks session protocol {tree}; steering needs {need} (update the node: `board node` "
                 "from this version)"),
    "cli": "this session's agent CLI cannot steer into a running turn (its node says so)",
    "foreign_human": ("the turn in flight (message {n}) was started by the session's parent; only a turn's own sender "
                      "may steer into it"),
    "data": "this turn was asked for data; restart it or queue instead",
    "queued": ("{k} message(s) are already queued for this session behind the turn in flight; a steer would reach the "
               "turn before them: cancel them or send with queue"),
    "unsettled": "a steer into message {n} of this session is not settled yet",
    "took": "the turn in flight (message {n}) already took a steer",
    "big": f"the steer's text is over {STEER_MAX_BYTES:,} bytes as it is written to the agent",
    "encoding": "the steer's text is not valid Unicode (it has an unpaired surrogate): it could never be sent as UTF-8",
    "schema": "a steer cannot ask for its result as data (output_schema): the reply that answers it is the turn's",
    "spawn": "may_spawn and spawn_share do not go with a steer",
}
WORDS = {"in_turn": "taken into it", "next_turn": "it ran as a turn of its own after it",
         "not_delivered": "not delivered: send it again if you still want it",
         "unconfirmed": "not confirmed: delivery could not be confirmed, and the agent may have read it"}
ACCEPTED_WHY = ("accepted for the turn in flight (message {n}): its node writes it into that turn if it still can; "
                "the outcome comes with that turn's result")
RETRY_WHY = "already sent for this request id; nothing new was sent (a retry never sends its own text)"
EXPIRED_WHY = "already sent for this request id; that steer is no longer kept on the hub"


def _key(session_id: str, n: int) -> str:
    return f"{STEER_PREFIX}{session_id}.{n}"


def steers(conn, session_id: str) -> list[dict]:
    """The session's steer records, oldest first."""
    out = []
    for (value,) in conn.execute("SELECT value FROM board_state WHERE key >= ? AND key < ?",
                                 hosted._range(f"{STEER_PREFIX}{session_id}.")):
        try:
            s = json.loads(value)
        except (TypeError, ValueError):
            continue
        if isinstance(s, dict) and type(s.get("into")) is int:
            out.append(s)
    return sorted(out, key=lambda s: s["into"])


def get(conn, session_id: str, n: Any) -> dict | None:
    s = _get(conn, _key(session_id, n)) if type(n) is int else None
    return s if isinstance(s, dict) else None


def unsettled(conn, session_id: str) -> dict | None:
    return next((s for s in steers(conn, session_id) if s.get("outcome") is None), None)


_unreadable_last: str | None = None     # the settings error last said in the hub's log (one line per change)


def off_reason(board: Board) -> str | None:
    """`[dispatch.sessions] steer` on the hub: None when steering is on, else why new steers are refused. Steers
    accepted before are offered and settled as ever (the listing reads no setting). Fails closed (pre-merge review,
    review round 11):
    - "rejected": the hub's latest settings reload was rejected (`Board.settings_error`, set by `reload_settings`
      and cleared by its next successful reload; it logs the rejection itself). The last good settings stay in force
      for everything else, as on main; the edit may be the very one that turns steering off.
    - "unreadable": the settings in force do not validate: `[dispatch]` or `[dispatch.sessions]` present but not a
      table (`dispatch = false`, `sessions = []`), a value of the wrong type, an unknown key. Main's other readers
      of that table fall back to their defaults, which are off. Said once in the hub's log per change of the error.
    - "misplaced": a `steer` key directly under `[dispatch]` (any value): the switch in the wrong place (follow-up
      after #115). Said in the hub's log like "unreadable".
    - "off": `steer = false`.
    Absent (no `[dispatch]`, or no `[dispatch.sessions]`), steering is on: the design's default."""
    import contextlib
    from .dispatch import SessionsConfig
    global _unreadable_last
    lock = getattr(board, "_settings_lock", None)
    with lock if lock is not None else contextlib.nullcontext():
        # ONE reading of the reload status and the table it goes with (as agent_accounts._snapshot reads them)
        rejected, d = getattr(board, "settings_error", None), getattr(board.s, "dispatch", None)
    if rejected is not None:
        return "rejected"
    why = "unreadable"
    try:
        if d is not None and not isinstance(d, dict):
            raise ValueError("[dispatch] must be a table")
        if isinstance(d, dict) and "steer" in d:
            # (Follow-up after #115) `steer` directly under [dispatch], whatever its value: the switch, plainly meant,
            # in the wrong place. Startup does not validate [dispatch], so without this a cold start would read no
            # [dispatch.sessions] and steer on. (A reload rejects the key itself: "rejected" above.)
            why = "misplaced"
            raise ValueError("steer is set directly under [dispatch]; the switch is [dispatch.sessions] steer")
        on = SessionsConfig.from_dict((d or {}).get("sessions", {})).steer
    except (ValueError, AttributeError) as e:
        what = str(e)
        if what != _unreadable_last:
            _unreadable_last = what
            log.warning("the hub's [dispatch.sessions] settings could not be read (%s): steering is off until they "
                        "can be", what)
        return why
    _unreadable_last = None     # (read again: a recurrence is said again)
    return None if on else "off"


def encoded_size(text: str) -> int:
    """The steer's text as both adapters write it into their line (`json.dumps`)."""
    return len(json.dumps(text).encode())


def utf8(text: str) -> bool:
    """The text can be sent as UTF-8 (no unpaired surrogate): the hub's answers are UTF-8 JSON (R2-08)."""
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def serialised(answer: Any) -> bool:
    """The listing's answer can be written as the HTTP layer writes it (UTF-8 JSON, no NaN): only then is an offer
    in it exposed (R2-08)."""
    try:
        json.dumps(answer, ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError):
        return False
    return True


def has_steers(r: dict) -> bool:
    """Without a query: the session ever took a steer (`accept` marks it). One that never did needs no steer read on
    an ordinary read (part K)."""
    return r.get("steered") is True


# ---------------------------------------------------------------- accepting


def target(board: Board, c, r: dict, by_parent: bool, text: str, *, schema: bool = False,
           spawn: bool = False) -> tuple[dict | None, str | None]:
    """N, the turn in flight a steer from this sender would go into, or (None, why not). In the caller's write
    transaction, after its request-id lookup, in H's order."""
    from . import hosted_tree
    off = off_reason(board)
    if off is not None:
        return None, REFUSED[off]
    if r.get("state") == "ended":
        return None, REFUSED["ended"]
    if r.get("stop"):
        return None, REFUSED["closing"]
    tree = hosted_tree.node_protocol(c, r["node"])
    if tree < STEER_PROTOCOL:
        return None, REFUSED["protocol"].format(node=r["node"], tree=tree, need=STEER_PROTOCOL)
    if "steer" not in (r.get("modes") or []):
        return None, REFUSED["cli"]
    n = r.get("in_flight")
    m = _get(c, hosted._msg_key(r["session_id"], n)) if type(n) is int else None
    if not isinstance(m, dict) or m.get("state") != "delivered":
        return None, hosted.REFUSED["idle"]
    if m.get("kind") in hosted.KIND_WORDS:
        return None, hosted.REFUSED["kind"].format(n=n, what=hosted.KIND_WORDS[m["kind"]])
    if by_parent and m.get("kind") != "task":
        return None, hosted.REFUSED["foreign"].format(n=n)
    if not by_parent and m.get("kind") != "user":
        return None, REFUSED["foreign_human"].format(n=n)
    if m.get("cancel") is not None:
        return None, hosted.REFUSED["cancelled"].format(n=n)
    if isinstance(m.get("output"), dict):
        return None, REFUSED["data"]
    if int(r.get("queued") or 0) > 0:
        return None, REFUSED["queued"].format(k=int(r["queued"]))
    open_ = unsettled(c, r["session_id"])
    if open_ is not None:
        return None, REFUSED["unsettled"].format(n=open_["into"])
    if get(c, r["session_id"], n) is not None:
        return None, REFUSED["took"].format(n=n)
    if not utf8(text):
        return None, REFUSED["encoding"]       # before anything is stored, on every path (R2-08)
    if encoded_size(text) > STEER_MAX_BYTES:
        return None, REFUSED["big"]
    if schema:
        return None, REFUSED["schema"]
    if spawn:
        return None, REFUSED["spawn"]
    return m, None


def refuse(r: dict, why: str) -> Conflict:
    return Conflict(f"cannot steer into session {r['session_id']}: {why}")


def accept(c, r: dict, m: dict, text: str, kind: str, by: dict, who: str, rid: str | None, now: float) -> dict:
    """Store the steer for N (`m`), in the caller's write transaction, with its receipt when it has a request id."""
    s = {"session_id": r["session_id"], "into": m["n"], "kind": kind, "by": by, "created_at": now,
         "uuid": str(uuid.uuid4()), "text": text, "listed_at": None, "cancel": None, "outcome": None, "why": None,
         "settled_at": None, "withdrawn_at": None, "updated_at": now}
    if rid is not None:
        s |= {"request_id": rid, "sent_by": by}
        keep = {k: v for k, v in (r.get("receipts") or {}).items()
                if isinstance(v, dict) and float(v.get("at") or 0) > now - hosted.RECEIPT_SECONDS}
        keep[hosted._receipt_key(rid, by)] = {"steer": m["n"], "at": now}
        r["receipts"] = keep
    r["steered"] = True         # (`has_steers`: a session that never took one is never read for steers)
    r["updated_at"] = now
    _put(c, _key(r["session_id"], m["n"]), s, who, now)
    _put(c, hosted.SESSION_PREFIX + r["session_id"], r, who, now)
    return s


def sent_before(c, r: dict, rid: str | None, by: dict, now: float) -> tuple[bool, dict | None]:
    """(True, the steer) when this sender already sent a steer to the session with this request id; (True, None)
    when its receipt is kept and the steer record is gone (pruned with N); (False, None) otherwise."""
    if rid is None:
        return False, None
    got = (r.get("receipts") or {}).get(hosted._receipt_key(rid, by))
    if not (isinstance(got, dict) and type(got.get("steer")) is int
            and float(got.get("at") or 0) > now - hosted.RECEIPT_SECONDS):
        return False, None
    return True, get(c, r["session_id"], got["steer"]) or {"session_id": r["session_id"], "into": got["steer"],
                                                           "expired": True}


def answer(s: dict, created: bool) -> dict:
    """The steer send answer (H): a steer has no message number of its own."""
    expired = s.get("expired") is True
    return {"steer": {"session": s["session_id"], "into": s["into"], "outcome": s.get("outcome"),
                      "state": "expired" if expired else "settled" if s.get("outcome") else "accepted"}
            | ({} if expired or not s.get("outcome") else {"why": s.get("why")}),
            "message": None, "mode": "steer", "created": created,
            "why": (EXPIRED_WHY if expired else RETRY_WHY) if not created else ACCEPTED_WHY.format(n=s["into"])}


def view(s: dict | None) -> dict | None:
    """The terminal steer status, as its sender reads it: never its text, never its uuid."""
    if not isinstance(s, dict):
        return None
    out = {"into": s.get("into"), "kind": s.get("kind"), "outcome": s.get("outcome"), "why": s.get("why"),
           "state": "settled" if s.get("outcome") else "accepted", "listed": s.get("listed_at") is not None,
           "created_at": s.get("created_at"), "settled_at": s.get("settled_at"),
           "words": WORDS.get(s.get("outcome")) if s.get("outcome") else None}
    r = s.get("result")
    if s.get("outcome") == "next_turn" and isinstance(r, dict):
        # S's own turn's result, for its sender (its reply is S's, not N's: the session's `reply` stays N's)
        out["result"] = {"ok": r.get("ok"), "detail": r.get("detail"), "denials": r.get("denials") or [],
                         "reply": r.get("reply") or ""}
    return out


# ---------------------------------------------------------------- offering (the listing's write transaction)


def due(conn, node: str) -> bool:
    """Read only: a session on this node has a steer that is not settled (to offer, withdraw or sweep): the listing
    then takes its write transaction, so that every offer is made there (G-1)."""
    try:
        for (value,) in conn.execute("""SELECT value FROM board_state WHERE key >= ? AND key < ? AND json_valid(value)
                                        AND json_extract(value, '$.outcome') IS NULL""", hosted._range(STEER_PREFIX)):
            s = json.loads(value)
            r = _get(conn, hosted.SESSION_PREFIX + str(s.get("session_id")))
            if not isinstance(r, dict) or r.get("node") == node:
                return True
        return False
    except Exception:
        return False


def offer(board: Board, c, node: str, tree: int, now: float, shown: dict) -> None:
    """Inside the listing's write transaction, before the answer is built: each unsettled steer of this node's
    sessions is settled if its N is (the sweep), withdrawn if a check fails, or a candidate (`shown`: session id ->
    the listing item). `listed_at` is set by the renderer, and only for an entry it renders (`stamp`, S2-08). All or
    nothing: a fault rolls back every write made here (a savepoint) and offers nothing. Never raises."""
    c.execute("SAVEPOINT steer_offer")
    try:
        for (key, value) in c.execute("""SELECT key, value FROM board_state WHERE key >= ? AND key < ?
                                         AND json_valid(value) AND json_extract(value, '$.outcome') IS NULL""",
                                      hosted._range(STEER_PREFIX)).fetchall():
            s = json.loads(value)
            r = _get(c, hosted.SESSION_PREFIX + str(s.get("session_id")))
            m = _get(c, hosted._msg_key(str(s.get("session_id")), s.get("into"))) if isinstance(r, dict) else None
            if not isinstance(r, dict) or not isinstance(m, dict) or m.get("state") in hosted.SETTLED:
                # The re-upgrade sweep (J): N was settled where its steer was not (by an older hub), or is gone.
                _settled(c, s, None, now)
                continue
            if r.get("node") != node:
                continue
            if s.get("withdrawn_at") is not None:
                continue
            why = _offer_problem(board, c, r, m, s, tree, now) or _malformed(s)
            if why is not None:
                s |= {"withdrawn_at": now, "withdrawn": why, "updated_at": now}
                _put(c, key, s, "hub", now)
                continue
            shown[r["session_id"]] = {"into": s["into"], "uuid": s["uuid"], "text": s["text"]}
        c.execute("RELEASE SAVEPOINT steer_offer")
    except Exception:
        hosted_wake_complain()
        shown.clear()
        c.execute("ROLLBACK TO SAVEPOINT steer_offer")
        c.execute("RELEASE SAVEPOINT steer_offer")


def _malformed(s: dict) -> str | None:
    """A stored steer that cannot be rendered as an offer (R3-09: a record from a prerelease build, or one damaged):
    withdrawn like any failed check, so the rest of the listing goes as ever and it settles by the rule at N's end
    (`not_delivered` when it was never listed)."""
    uuid_, text = s.get("uuid"), s.get("text")
    if not (isinstance(uuid_, str) and 0 < len(uuid_) <= 64 and isinstance(text, str) and text.strip()
            and utf8(text) and utf8(uuid_) and encoded_size(text) <= STEER_MAX_BYTES):
        return "malformed"
    return None


def renderable(c, r: dict, tree: int) -> bool:
    """The renderer may show this session's steer: the request speaks protocol 5, the node's recorded protocol is
    5, and the session's reported modes name `steer` (S2-08)."""
    from . import hosted_tree
    return (int(tree) >= STEER_PROTOCOL and hosted_tree.node_protocol(c, r.get("node") or "") >= STEER_PROTOCOL
            and "steer" in (r.get("modes") or []))


def stamp(c, session_id: str, n: int, now: float) -> None:
    """An offer rendered into the answer, in the listing's write transaction: its first exposure (`listed_at`)."""
    s = get(c, session_id, n)
    if s is not None and s.get("outcome") is None and s.get("listed_at") is None:
        s |= {"listed_at": now, "updated_at": now}
        _put(c, _key(session_id, n), s, "hub", now)


def hosted_wake_complain() -> None:
    from . import hosted_wake
    hosted_wake._complain("hosted sessions: could not check the steers to offer; none is offered")


def _offer_problem(board: Board, c, r: dict, m: dict, s: dict, tree: int, now: float) -> str | None:
    """Why this steer may not be offered now (each check made again at every offer), or None."""
    from . import hosted_tree
    if tree < STEER_PROTOCOL or hosted_tree.node_protocol(c, r.get("node") or "") < STEER_PROTOCOL:
        return "protocol"           # an older node must never be offered a steer (J: a fetch below 5 withdraws it)
    if "steer" not in (r.get("modes") or []):
        return "modes"              # the session's process no longer says it can steer
    if r.get("state") == "ended" or r.get("stop"):
        return "ended"
    if m.get("state") != "delivered" or r.get("in_flight") != s["into"]:
        return "not_in_flight"
    if m.get("cancel") is not None or s.get("cancel") is not None:
        return "cancelled"
    if hosted._lost(c, r, now) or hosted.may_still_run(c, r, now):
        return "expired"
    if hosted._checkout_busy(board, r):
        return "checkout"
    return None


# ---------------------------------------------------------------- settling, once, with N


def extension(ev: Any) -> dict | None:
    """The steer object of a node's turn_ended, validated on its own: {"outcome", "why"} (and for `next_turn`,
    S's own `result`), else None (a malformed one is ignored, never refused: N's own end is taken either way)."""
    if not isinstance(ev, dict) or set(ev) - {"outcome", "why", "result"}:
        return None
    if ev.get("outcome") not in HOST_OUTCOMES or not isinstance(ev.get("why"), str) or not WHY_RE.fullmatch(ev["why"]):
        return None
    if (ev["outcome"] == "next_turn") != ("result" in ev):
        return None
    out = {"outcome": ev["outcome"], "why": ev["why"]}
    if ev["outcome"] == "next_turn":
        result = turn_fields(ev["result"])
        if result is None:
            return None
        out["result"] = result
    return out


def claude(r: dict) -> bool:
    """The session's agent is Claude Code: only its steers can be held, or run as a turn of their own (step 3)."""
    return r.get("runtime") == "claude-code"


def _finite(meta: dict) -> dict:
    """Optional metadata with every non-finite number left out, so it can never break serialising a report."""
    import math
    return {k: v for k, v in meta.items() if not (isinstance(v, float) and not math.isfinite(v))}


def turn_fields(x: Any, *, answered: bool = False) -> dict | None:
    """A turn's end as a node sends it (ok, detail, denials, meta, text, answered), checked like an event's, else
    None. S's own result on `next_turn`, and the checkpoint (`checkpoint`: `answered`, true, is required there: N's
    native end is the provider's own). Non-finite numbers in `meta` are left out."""
    from .hosted import _TOOL_NAME
    if not isinstance(x, dict) or set(x) - {"ok", "detail", "denials", "meta", "text", "answered", "message"}:
        return None
    if answered and x.get("answered") is not True:
        return None
    ok, detail, text = x.get("ok"), x.get("detail", ""), x.get("text", "")
    denials, meta, answered = x.get("denials", []), x.get("meta", {}), x.get("answered", False)
    if not isinstance(ok, bool) or not isinstance(answered, bool):
        return None
    if not isinstance(detail, str) or len(detail) > 300:
        return None
    if not isinstance(text, str) or len(text.encode("utf-8", "replace")) > REPLY_LIMIT:
        return None
    if (not isinstance(denials, list) or len(denials) > 50
            or any(not isinstance(d, str) or not _TOOL_NAME.fullmatch(d) for d in denials)):
        return None
    if (not isinstance(meta, dict) or len(meta) > 20 or any(
            not isinstance(k, str) or len(k) > 40 or not (
                v is None or isinstance(v, (bool, int, float)) or (isinstance(v, str) and len(v) <= 200))
            for k, v in meta.items())):
        return None
    return {"ok": ok, "detail": detail, "denials": list(denials), "meta": _finite(dict(meta)), "text": text,
            "answered": answered}


def native(m: dict) -> dict | None:
    """N's committed checkpoint (`held`): N's native end (ok, detail, denials, meta, text, answered), or None."""
    h = m.get("held") if isinstance(m, dict) else None
    return h if isinstance(h, dict) else None


def held_end(m: dict) -> dict:
    """The end event N's committed checkpoint makes: N's native result (G's rule), with no host's word on S."""
    h = native(m)
    return {"kind": "turn_ended", "message": m["n"], "ok": h["ok"], "delivered": True, "detail": h["detail"],
            "text": h["text"], "denials": h["denials"], "meta": h["meta"], "answered": h["answered"],
            "restarted_by": None, "steer": None}


CHECKPOINT_REFUSED = "the checkpoint does not fit message {n}: {why}"


def checkpoint(c, r: dict, held: Any, now: float, by: str) -> None:
    """The node's checkpoint of N's held native end (G, O): taken once, only from the session's assigned node (the
    caller checked), for N delivered and in flight there, with a listed, unsettled steer into it, and never once N is
    settled. Conflict otherwise (the node logs and drops it; it is never resent)."""
    n = held.get("message") if isinstance(held, dict) else None
    fields = turn_fields(held, answered=True)
    if type(n) is not int or n < 1 or fields is None:
        raise Conflict(CHECKPOINT_REFUSED.format(n=n, why="malformed"))
    if not claude(r):
        raise Conflict(CHECKPOINT_REFUSED.format(n=n, why="only a Claude session's steer is ever held"))
    m = _get(c, hosted._msg_key(r["session_id"], n))
    s = get(c, r["session_id"], n) if has_steers(r) else None
    why = ("no such message" if not isinstance(m, dict) else
           "it is settled" if m.get("state") in hosted.SETTLED else
           "it is not the turn in flight" if m.get("state") != "delivered" or r.get("in_flight") != n else
           "it already has one" if native(m) is not None else
           "it took no steer" if s is None else
           "its steer is settled" if s.get("outcome") is not None else
           "its steer was never listed" if s.get("listed_at") is None else None)
    if why is not None:
        raise Conflict(CHECKPOINT_REFUSED.format(n=n, why=why))
    m |= {"held": fields, "updated_at": now}
    _put(c, hosted._msg_key(r["session_id"], n), m, by, now)


def settle(c, r: dict, n: Any, ext: dict | None, now: float) -> dict | None:
    """N (message `n` of session `r`) is being settled, in this transaction: its steer, if it has one, is settled by
    the one precedence rule. `ext`: the owning host's extension on N's own end (`extension`), or None on every other
    path. Returns the steer as settled (or None). None at once, with no read, for a session that never took one.
    `next_turn` is taken only for a Claude session's steer (an impossible shape otherwise: ignored, rule 2)."""
    if not has_steers(r):
        return None
    if ext is not None and ext.get("outcome") == "next_turn" and not claude(r):
        ext = None
    s = get(c, r["session_id"], n)
    if s is None or s.get("outcome") is not None:
        return s
    return _settled(c, s, ext, now)


def _clear_held(c, session_id: str, n: Any, now: float) -> None:
    """A checkpoint left on an N that is settled (an older hub settled it during a downgrade): cleared."""
    m = _get(c, hosted._msg_key(session_id, n)) if type(n) is int else None
    if isinstance(m, dict) and m.get("state") in hosted.SETTLED and "held" in m:
        m.pop("held", None)
        m["updated_at"] = now
        _put(c, hosted._msg_key(session_id, n), m, "hub", now)


def _settled(c, s: dict, ext: dict | None, now: float) -> dict:
    _clear_held(c, s["session_id"], s.get("into"), now)
    listed = s.get("listed_at") is not None
    if ext is not None and (ext["outcome"] == "not_delivered" or listed):
        outcome, why = ext["outcome"], ext["why"]
    else:
        outcome, why = ("unconfirmed", NO_WORD) if listed else ("not_delivered", NEVER_LISTED)
    if outcome == "next_turn":
        # S's own result, kept apart from N's and never added to it (G): its reply lives here, not in the session's.
        r = ext["result"]
        s["result"] = {"ok": r["ok"], "detail": r["detail"], "denials": r["denials"], "meta": r["meta"],
                       "reply": r["text"]}
    s |= {"outcome": outcome, "why": why, "settled_at": now, "text": "", "updated_at": now}
    _put(c, _key(s["session_id"], s["into"]), s, "hub", now)
    return s


def stale(conn, session_id: str, r: dict | None = None) -> bool:
    """Read only: the session has an unsettled steer whose N is settled or gone (the re-upgrade sweep's work). With
    its record `r`: no query at all for a session that never took a steer (`has_steers`), else one scan."""
    if r is not None and not has_steers(r):
        return False
    for s in steers(conn, session_id):
        if s.get("outcome") is None:
            m = _get(conn, hosted._msg_key(session_id, s["into"]))
            if not isinstance(m, dict) or m.get("state") in hosted.SETTLED:
                return True
    return False


def sweep(board: Board, r: dict) -> dict:
    """On a read: a steer whose N is already settled (an older hub settled N, or a path that left it) is settled by
    the one rule, whatever its node does (S2-11: an offline node never polls, so the listing's sweep never runs).
    Nothing else of the session is touched (R2-10: a parent's retry and wait read through it too). The session as
    it is stored now."""
    if not stale(board.conn, r["session_id"], r):
        return r
    from . import db
    with db.write_tx(board.conn) as c:
        settle_unsettled(c, hosted._session(c, r["session_id"]), board.now())
    return hosted._session(board.conn, r["session_id"])


def settle_unsettled(c, r: dict, now: float) -> None:
    """Every steer of the session whose N is settled or gone: settled by the rule (hosted._end, after it failed the
    session's messages)."""
    if not has_steers(r):
        return
    for s in steers(c, r["session_id"]):
        if s.get("outcome") is None:
            m = _get(c, hosted._msg_key(r["session_id"], s["into"]))
            if not isinstance(m, dict) or m.get("state") in hosted.SETTLED:
                _settled(c, s, None, now)


def cancel(c, session_id: str, n: int, now: float, by: str) -> None:
    """N's cancel (hosted_spawn.cancel): its steer is cancelled with it, and never offered again."""
    s = get(c, session_id, n)
    if s is not None and s.get("outcome") is None and s.get("cancel") is None:
        s |= {"cancel": now, "updated_at": now}
        _put(c, _key(session_id, n), s, by, now)


def prune_session(c, session_id: str) -> None:
    c.execute("DELETE FROM board_state WHERE key >= ? AND key < ?", hosted._range(f"{STEER_PREFIX}{session_id}."))


def prune_settled(c, session_id: str) -> None:
    """A settled steer goes with N's message: once N's record is pruned, so is its steer."""
    for s in steers(c, session_id):
        if s.get("outcome") is not None and _get(c, hosted._msg_key(session_id, s["into"])) is None:
            c.execute("DELETE FROM board_state WHERE key = ?", (_key(session_id, s["into"]),))


def prune_orphans(c) -> None:
    """A periodic pass: steer rows of sessions that are gone."""
    for (key,) in c.execute("SELECT key FROM board_state WHERE key >= ? AND key < ?",
                            hosted._range(STEER_PREFIX)).fetchall():
        sid = key[len(STEER_PREFIX):].rsplit(".", 1)[0]
        if _get(c, hosted.SESSION_PREFIX + sid) is None:
            c.execute("DELETE FROM board_state WHERE key = ?", (key,))
