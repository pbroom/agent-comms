"""Hosted sessions, hub side: the registry of the agent conversations a node keeps open (DESIGN_NOTES "Session host").

A hosted session is one long-lived agent process on a node (session_host.py) that takes turns from messages. The hub
keeps the record and the queue; the node (`board node`, node_sessions.py) fetches both, runs the turns from ITS OWN
runner settings and reports what happened. Nothing executable crosses the wire: the node takes the agent, the project,
an optional model name and the message text.

- No schema change: everything lives in `board_state`, like nodes.py's records.
  `host.session.<uuid>`: agent, project, node, state, pid, turn count, queue depth, the last result's facts, the LAST
  reply text, times, the stop flag. `host.msg.<uuid>.<n>`: kind, text, state (queued | delivered | done | failed),
  detail. A message's text is dropped from its record once its turn is over.
- The hub issues the session id (a UUID). One session per (agent, project) that has not ended.
- Placement mirrors a node run: the hub's `[dispatch.nodes.<name>]` entry must cover the project
  (`nodes.grant_covers`) and the agent, and the node must be live, offer the project and a runner for the agent, and
  say it hosts sessions (its `[dispatch.sessions] enabled`). The node checks its own checkout again before it starts.
- Only the human starts, messages, reads and closes a session (`/api/hosted-sessions`, `board session`). No MCP tool
  and no agent route reaches this module, and the reply text is returned to the human alone: never to an agent, and
  never to the node (which wrote it).
- Only the assigned node reports on a session (`/api/nodes/sessions`). Reports are idempotent: a message moves
  queued -> delivered -> done | failed and never back, so a repeated or late event changes nothing.
- The host never retries a lost turn. A turn that was lost (timeout, the process died, the node restarted or went
  silent) is `failed` with the reason; the human sends again if they want it again.
- `wake` is the one message kind that is not the human's: hosted_wake.py queues it, with text the hub builds itself,
  when a board post is addressed to the session's agent in the session's project.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from typing import Any, Callable

from . import db, nodes
from .core import Board, Conflict, Invalid, NotFound, Principal
from .nodes import _get, _put
from .providers import SESSION_ID_RE, cap_reply, validate_model

SESSION_PREFIX = "host.session."   # <uuid>: the session record
MSG_PREFIX = "host.msg."           # <uuid>.<n>: one message, n counting from 1 per session
KINDS = ("user", "wake")           # `user`: the human's (start, send); `wake`: the hub's own (hosted_wake.py)
MSG_STATES = ("queued", "delivered", "done", "failed")
SETTLED = ("done", "failed")
SESSION_STATES = ("starting", "idle", "running", "waiting", "ended")   # session_host.STATES, as the node reports them
EVENT_KINDS = ("delivered", "turn_started", "turn_ended", "approval_requested", "ended")
MESSAGE_MAX_BYTES = 64 * 1024      # one message's text
MAX_QUEUED = 50                    # messages waiting in one session
MAX_EVENTS = 200                   # events in one report
REPLY_LIMIT = 64 * 1024            # the reply text one event may carry; it is capped again here (providers.cap_reply)
SHOW_MESSAGES = 20                 # the most recent messages `show` lists
KEEP_ENDED_SECONDS = 7 * 86400     # an ended session's record is kept this long
KEEP_SETTLED_SECONDS = 86400       # a settled message's record, in a session that goes on
NODE_WAIT_MAX = 25                 # seconds one long-poll of a node may wait
HUMAN_WAIT_MAX = 30                # ... and one of the human's CLI
WAIT_POLL_SECONDS = 0.25           # how often a waiting call re-reads the database
_TOOL_NAME = re.compile(r"[A-Za-z0-9_.:-]{1,128}")
_SEEN = re.compile(r"[0-9a-f]{1,64}")


def _range(prefix: str) -> tuple[str, str]:
    return prefix, prefix[:-1] + chr(ord(prefix[-1]) + 1)


def _sid(session_id: Any) -> str:
    if not isinstance(session_id, str) or not SESSION_ID_RE.fullmatch(session_id):
        raise NotFound("no such hosted session")
    return session_id


def _msg_key(session_id: str, n: int) -> str:
    return f"{MSG_PREFIX}{session_id}.{n}"


def _sessions(conn) -> list[dict]:
    out = []
    for (value,) in conn.execute("SELECT value FROM board_state WHERE key >= ? AND key < ?", _range(SESSION_PREFIX)):
        try:
            r = json.loads(value)
        except (TypeError, ValueError):
            continue
        if isinstance(r, dict) and isinstance(r.get("session_id"), str):
            out.append(r)
    return out


def _messages(conn, session_id: str) -> list[dict]:
    """The session's message records, oldest first."""
    out = []
    for (value,) in conn.execute("SELECT value FROM board_state WHERE key >= ? AND key < ?",
                                 _range(f"{MSG_PREFIX}{session_id}.")):
        try:
            m = json.loads(value)
        except (TypeError, ValueError):
            continue
        if isinstance(m, dict) and type(m.get("n")) is int:
            out.append(m)
    return sorted(out, key=lambda m: m["n"])


def _session(conn, session_id: Any) -> dict:
    r = _get(conn, SESSION_PREFIX + _sid(session_id))
    if not isinstance(r, dict):
        raise NotFound("no such hosted session")
    return r


def _text(text: Any) -> str:
    if not isinstance(text, str) or not text.strip():
        raise Invalid("message text is empty")
    if len(text.encode("utf-8", "replace")) > MESSAGE_MAX_BYTES:
        raise Invalid(f"message text is over {MESSAGE_MAX_BYTES // 1024} KB")
    return text


def _project(project: Any) -> str:
    if (not isinstance(project, str) or not project.startswith("/") or len(project) > 1024
            or any(ord(ch) < 32 or ord(ch) == 127 for ch in project)):
        raise Invalid("project must be an absolute path")
    return project.rstrip("/") or "/"


def _agent(board: Board, name: Any) -> tuple[str, str]:
    """The agent a session is for, with its runtime. No name: the one Claude agent, when there is exactly one."""
    rows = {r["name"]: r["runtime"] for r in board.conn.execute(
        "SELECT name, runtime FROM agents WHERE active = 1 AND is_human = 0 ORDER BY name")}
    rows = {n: rt for n, rt in rows.items() if rt != nodes.NODE_RUNTIME}
    if name is None:
        claude = [n for n, rt in rows.items() if rt.startswith("claude")]
        if len(claude) != 1:
            raise Invalid("say which agent (--agent NAME): " + (
                "no agent with a Claude runtime exists" if not claude
                else "several agents have a Claude runtime (" + ", ".join(claude) + ")"))
        name = claude[0]
    if not isinstance(name, str) or name not in rows:
        raise NotFound(f"no agent named {name!r}")
    return name, rows[name]


def _node_seen(conn, node: str) -> float:
    """When the node was last seen AS A SESSION HOST. A node whose latest hello does not say it hosts sessions (it
    was restarted with [dispatch.sessions] off) keeps its heartbeat fresh through its run loop, but nothing there
    reads a session's messages or its stop flag any more: for hosted sessions it counts as never seen."""
    info = _get(conn, nodes.NODE_PREFIX + node)
    return float(info.get("last_seen") or 0) if isinstance(info, dict) and info.get("sessions") else 0.0


def _place(board: Board, project: str, agent: str, runtime: str, allowed: dict[str, dict] | None) -> str:
    """The node that will host this session, or a Conflict that says what is missing."""
    allowed = allowed or {}
    granted = sorted(n for n, g in allowed.items() if nodes.grant_covers(g, project)
                     and (agent in g.get("agents", []) or runtime in g.get("agents", [])))
    if not granted:
        raise Conflict(f"no node may run {agent} in {project}: the hub's [dispatch.nodes.<name>] entries must list "
                       "the project (or a checkout root above it) and the agent or its runtime")
    node = nodes.node_for(board, project, agent, runtime, allowed, room=False, sessions=True)
    if node is None:
        raise Conflict(f"no connected node hosts sessions for {agent} in {project} (allowed: {', '.join(granted)}). "
                       "That node needs `board node` running, [dispatch.sessions] enabled = true, a checkout of the "
                       "project and a runner for the agent")
    return node


# ---------------------------------------------------------------- what leaves this module

_SESSION_FIELDS = ("session_id", "agent", "runtime", "project", "node", "model", "state", "pid", "turns", "queued",
                   "in_flight", "last_result", "denials", "stop", "error", "created_at", "updated_at", "ended_at")
_MSG_FIELDS = ("n", "kind", "state", "ok", "detail", "denials", "created_at", "delivered_at", "ended_at")


def _human_view(board: Board, r: dict) -> dict:
    """The session as the human sees it: everything, the last reply included."""
    out = {k: r.get(k) for k in _SESSION_FIELDS}
    out |= {"reply": r.get("reply"), "reply_for": r.get("reply_for"),
            "node_live": board.now() - _node_seen(board.conn, r["node"]) <= nodes.NODE_STALE_SECONDS,
            "transcript": f"data/node-logs/sessions/{r['session_id']}.jsonl on node {r['node']}"}
    return out


def _msg_view(m: dict) -> dict:
    return {"session_id": m.get("session_id"), **{k: m.get(k) for k in _MSG_FIELDS}}


def _node_view(r: dict) -> dict:
    """What a node gets back for a report: never the reply."""
    return {k: r.get(k) for k in ("session_id", "state", "stop", "turns", "queued", "in_flight")}


# ---------------------------------------------------------------- the human's side


def _human(board: Board, p: Principal, what: str) -> None:
    board._require_human(p, what)


def _prune(c, now: float) -> None:
    for r in _sessions(c):
        if r.get("state") == "ended" and float(r.get("updated_at") or 0) < now - KEEP_ENDED_SECONDS:
            c.execute("DELETE FROM board_state WHERE key = ?", (SESSION_PREFIX + r["session_id"],))
            c.execute("DELETE FROM board_state WHERE key >= ? AND key < ?", _range(f"{MSG_PREFIX}{r['session_id']}."))


def _enqueue(c, r: dict, kind: str, text: str, by: str, now: float) -> dict:
    """Add a message to the session's queue (inside the caller's transaction). PR 3's wake calls this with `wake`."""
    if kind not in KINDS:
        raise Invalid("unknown message kind")
    if r["state"] == "ended":
        raise Conflict(f"session {r['session_id']} has ended" + (f" ({r['error']})" if r.get("error") else ""))
    if r.get("stop"):
        raise Conflict(f"session {r['session_id']} is being closed")
    if int(r.get("queued") or 0) >= MAX_QUEUED:
        raise Conflict(f"session {r['session_id']} already has {MAX_QUEUED} messages waiting")
    n = int(r["next_n"])
    m = {"session_id": r["session_id"], "n": n, "kind": kind, "text": text, "state": "queued", "ok": None,
         "detail": "", "denials": [], "created_at": now, "updated_at": now, "delivered_at": None, "ended_at": None}
    r |= {"next_n": n + 1, "queued": int(r.get("queued") or 0) + 1, "updated_at": now}
    _put(c, _msg_key(r["session_id"], n), m, by, now)
    _put(c, SESSION_PREFIX + r["session_id"], r, by, now)
    return m


def start(board: Board, p: Principal, project: Any, text: Any, *, agent: Any = None, model: Any = None,
          allowed: dict[str, dict] | None = None) -> dict:
    """Start a hosted session of `agent` in `project` with its first message. `allowed`: the hub's [dispatch.nodes]."""
    _human(board, p, "start a hosted session")
    project, text = _project(project), _text(text)
    try:
        model = validate_model(model)
    except ValueError as e:
        raise Invalid(str(e)) from None
    agent, runtime = _agent(board, agent)
    node = _place(board, project, agent, runtime, allowed)
    now = board.now()
    with db.write_tx(board.conn) as c:
        _prune(c, now)
        for other in _sessions(c):
            if other.get("agent") == agent and other.get("project") == project and other.get("state") != "ended":
                raise Conflict(f"{agent} already has a hosted session in {project}: {other['session_id']}. Send to "
                               f"it (`board session send {other['session_id']} ...`) or close it first")
        r = {"session_id": str(uuid.uuid4()), "agent": agent, "runtime": runtime, "project": project, "node": node,
             "model": model, "state": "idle", "pid": None, "resume": False, "turns": 0, "queued": 0,
             "in_flight": None, "next_n": 1, "last_result": None, "reply": None, "reply_for": None, "denials": [],
             "stop": None, "error": None, "created_by": p.name, "created_at": now, "updated_at": now,
             "node_reported_at": None, "ended_at": None}
        m = _enqueue(c, r, "user", text, p.name, now)
        from . import hosted_wake
        hosted_wake.begin(c, r["session_id"], now)      # woken for posts made from now on (PR 3)
    return {"session": _human_view(board, r), "message": _msg_view(m)}


def send(board: Board, p: Principal, session_id: Any, text: Any) -> dict:
    """Queue a message for the session: its next turn if it is idle, the turn after the ones ahead of it otherwise."""
    _human(board, p, "message a hosted session")
    text = _text(text)
    now = board.now()
    _settle(board, _session(board.conn, session_id))
    with db.write_tx(board.conn) as c:
        r = _session(c, session_id)
        m = _enqueue(c, r, "user", text, p.name, now)
    return {"session": _human_view(board, r), "message": _msg_view(m)}


def list_sessions(board: Board, p: Principal, include_ended: bool = False) -> dict:
    _human(board, p, "list hosted sessions")
    rows = [_settle(board, r) for r in _sessions(board.conn)]
    rows = [r for r in rows if include_ended or r.get("state") != "ended"]
    rows.sort(key=lambda r: -float(r.get("created_at") or 0))
    return {"sessions": [_human_view(board, r) for r in rows]}


def show(board: Board, p: Principal, session_id: Any) -> dict:
    _human(board, p, "read a hosted session")
    r = _settle(board, _session(board.conn, session_id))
    out = _human_view(board, r)
    out["messages"] = [_msg_view(m) for m in _messages(board.conn, r["session_id"])[-SHOW_MESSAGES:]]
    return out


def message(board: Board, p: Principal, session_id: Any, n: Any) -> dict:
    """One message and its session (the reply included): what `--wait` polls."""
    _human(board, p, "read a hosted session")
    _settle(board, _session(board.conn, session_id))
    # One snapshot for both reads: a turn that completes between them would otherwise show its message `done` beside
    # the session's previous reply, and `--wait` would exit without the reply it waited for.
    c = board.conn
    c.execute("BEGIN")
    try:
        r = _session(c, session_id)
        m = _get(c, _msg_key(r["session_id"], n)) if type(n) is int and n > 0 else None
    finally:
        c.execute("ROLLBACK")
    if not isinstance(m, dict):
        raise NotFound(f"session {r['session_id']} has no message {n}")
    return {"message": _msg_view(m), "session": _human_view(board, r)}


def close(board: Board, p: Principal, session_id: Any) -> dict:
    """End the session: its process is stopped, a turn in flight fails, queued messages are dropped. The node does
    it and confirms; a node that is not connected cannot, so the session ends here and the node closes it when it
    is back (a node closes every session the hub no longer lists)."""
    _human(board, p, "close a hosted session")
    now = board.now()
    with db.write_tx(board.conn) as c:
        r = _session(c, session_id)
        if r["state"] != "ended":
            if now - _node_seen(c, r["node"]) > nodes.NODE_STALE_SECONDS:   # away, or no longer hosting sessions
                _end(c, r, "closed by the human (its node was not connected)", p.name, now)
            elif not r.get("stop"):
                r |= {"stop": "close", "updated_at": now}
            _put(c, SESSION_PREFIX + r["session_id"], r, p.name, now)
    return _human_view(board, r)


def _end(c, r: dict, detail: str, by: str, now: float) -> None:
    """The session is over: every message that was not settled fails with the reason. The caller stores `r`."""
    detail = (detail or "ended")[:300]
    for m in _messages(c, r["session_id"]):
        if m.get("state") not in SETTLED:
            m |= {"state": "failed", "ok": False, "detail": f"session ended: {detail}"[:300], "text": "",
                  "ended_at": now, "updated_at": now}
            _put(c, _msg_key(r["session_id"], m["n"]), m, by, now)
    r |= {"state": "ended", "error": detail, "ended_at": now, "updated_at": now, "pid": None, "queued": 0,
          "in_flight": None}


def _lost(conn, r: dict, now: float) -> bool:
    """The session has a turn in flight or a close pending, and its node has been gone long enough (or no longer
    hosts sessions) that neither will be carried out. Only what the node itself did counts: its hellos as a session
    host and its reports on this session."""
    if r.get("state") == "ended" or not (r.get("stop") or r.get("in_flight") is not None):
        return False
    seen = _node_seen(conn, r["node"])
    return not seen or now - max(seen, float(r.get("node_reported_at") or 0)) > nodes.NODE_LOST_SECONDS


def _settle(board: Board, r: dict) -> dict:
    """On the human's reads: a session whose node went silent (nodes.NODE_LOST_SECONDS, like a node run) no longer
    has a turn in flight, and one being closed is closed. The node stops its own turns well before that
    (node_sessions.py), so the failed turn is not still running somewhere."""
    now = board.now()
    if not _lost(board.conn, r, now):
        return r
    with db.write_tx(board.conn) as c:
        # Decided again inside the transaction, on what is stored now: a node that reconnected and reported a
        # freshly delivered turn between the check above and here must not have that turn failed under it.
        r = _session(c, r["session_id"])
        if not _lost(c, r, now):
            return r
        if r.get("stop"):
            _end(c, r, "closed by the human (its node went silent)", "hub", now)
        else:
            for m in _messages(c, r["session_id"]):
                if m.get("state") == "delivered":
                    m |= {"state": "failed", "ok": False, "text": "", "ended_at": now, "updated_at": now,
                          "detail": "node_lost: the node went silent (or stopped hosting sessions) while the turn "
                                    "was in flight; it was not replayed"}
                    _put(c, _msg_key(r["session_id"], m["n"]), m, "hub", now)
                    r["last_result"] = {"message": m["n"], "ok": False, "detail": m["detail"], "denials": [],
                                        "meta": {}, "at": now}
            r |= {"in_flight": None, "state": "idle", "pid": None, "updated_at": now}
        _put(c, SESSION_PREFIX + r["session_id"], r, "hub", now)
    return r


# ---------------------------------------------------------------- the node's side


def node_sessions(board: Board, p: Principal) -> dict:
    """This node's sessions that have not ended, each with its unsettled messages (oldest first). Only the first
    queued message of a session carries its text: the node writes one at a time. `version` changes whenever
    anything the node acts on does, so the node's long-poll can wait on it."""
    nodes._require_node(board, p)
    from . import hosted_wake
    hosted_wake.hand_over(board, p.name)    # PR 3: queue the wakes that are due, re-check the one about to be handed
    out, mark = [], []
    for r in sorted(_sessions(board.conn), key=lambda r: r["session_id"]):
        if r.get("node") != p.name or r.get("state") == "ended":
            continue
        msgs, first = [], True
        for m in _messages(board.conn, r["session_id"]):
            if m.get("state") in SETTLED:
                continue
            item = {"n": m["n"], "kind": m.get("kind"), "state": m.get("state")}
            if m.get("state") == "queued" and first:
                item["text"], first = m.get("text"), False
            msgs.append(item)
        out.append({k: r.get(k) for k in ("session_id", "agent", "runtime", "project", "model", "stop")}
                   | {"resume": r.get("resume") is True, "messages": msgs,
                      # The stamp of the last status applied (`report`): the node stamps above it, whatever its
                      # own clock says. Not part of `version`: it changes with the node's own reports.
                      "status_seq": int(r.get("status_seq") or 0)})
        mark.append([r["session_id"], r.get("stop"), [[m["n"], m["state"]] for m in msgs]])
    version = hashlib.sha256(json.dumps(mark).encode()).hexdigest()[:16]
    return {"node": p.name, "sessions": out, "version": version}


def _events(events: Any) -> list[dict]:
    if not isinstance(events, list) or len(events) > MAX_EVENTS:
        raise Invalid(f"events must be a list of at most {MAX_EVENTS} events")
    out = []
    for ev in events:
        if not isinstance(ev, dict) or ev.get("kind") not in EVENT_KINDS:
            raise Invalid("each event needs a kind: " + ", ".join(EVENT_KINDS))
        n, detail, text = ev.get("message"), ev.get("detail", ""), ev.get("text", "")
        denials, meta, ok = ev.get("denials", []), ev.get("meta", {}), ev.get("ok")
        if n is not None and (type(n) is not int or n < 1):
            raise Invalid("message must be a message number")
        if not isinstance(detail, str) or len(detail) > 300:
            raise Invalid("detail must be a string of at most 300 characters")
        if not isinstance(text, str) or len(text.encode("utf-8", "replace")) > REPLY_LIMIT:
            raise Invalid("text is over the reply limit")
        if (not isinstance(denials, list) or len(denials) > 50
                or any(not isinstance(d, str) or not _TOOL_NAME.fullmatch(d) for d in denials)):
            raise Invalid("denials must be a list of at most 50 tool names")
        if (not isinstance(meta, dict) or len(meta) > 20 or any(
                not isinstance(k, str) or len(k) > 40 or not (
                    v is None or isinstance(v, (bool, int, float)) or (isinstance(v, str) and len(v) <= 200))
                for k, v in meta.items())):
            raise Invalid("meta must be a small table of scalars")
        if ok is not None and not isinstance(ok, bool):
            raise Invalid("ok must be true or false")
        out.append({"kind": ev["kind"], "message": n, "ok": ok, "delivered": ev.get("delivered") is not False,
                    "detail": detail, "text": cap_reply(text), "denials": denials, "meta": meta})
    return out


def _apply(c, r: dict, ev: dict, by: str, now: float) -> None:
    """One event from the node, applied to the session `r` (stored by the caller) and its message. A message only
    moves forward, so an event that arrives twice, late or for an unknown message changes nothing."""
    kind, n, sid = ev["kind"], ev["message"], r["session_id"]
    if kind == "ended":
        _end(c, r, ev["detail"] or "the node ended the session", by, now)
        return
    if kind == "approval_requested":
        r["denials"] = ev["denials"]
        return
    m = _get(c, _msg_key(sid, n)) if n is not None else None
    if not isinstance(m, dict) or m.get("state") in SETTLED:
        return
    if m["state"] == "queued":      # whichever event comes first takes it out of the queue
        r["queued"] = max(0, int(r.get("queued") or 0) - 1)
    if kind in ("delivered", "turn_started"):
        if m["state"] == "queued":
            m |= {"state": "delivered", "delivered_at": now, "text": ""}    # the node has it: the hub lets go of it
            r["in_flight"] = n
        if kind == "turn_started":
            r["resume"] = True      # the provider began a turn in it, so its session exists (a write proves nothing)
    else:
        ok = ev["ok"] is True
        detail = ev["detail"] or ("" if ok else "the agent reported that the turn failed"
                                  + (f" ({ev['meta']['subtype']})" if isinstance(ev["meta"].get("subtype"), str) else ""))
        m |= {"state": "done" if ok else "failed", "ok": ok, "detail": detail, "denials": ev["denials"], "text": "",
              "ended_at": now}
        if r.get("in_flight") == n:
            r["in_flight"] = None
        r["last_result"] = {"message": n, "ok": ok, "detail": detail, "denials": ev["denials"], "meta": ev["meta"],
                            "at": now}
        r["denials"] = ev["denials"]
        if ev["delivered"]:
            r["turns"] = int(r.get("turns") or 0) + 1
            if ok or ev["text"]:
                r |= {"reply": ev["text"], "reply_for": n}
        c.execute("""DELETE FROM board_state WHERE key >= ? AND key < ? AND updated_at < ? AND json_valid(value)
                     AND json_extract(value, '$.state') IN ('done', 'failed')""",
                  (*_range(f"{MSG_PREFIX}{sid}."), now - KEEP_SETTLED_SECONDS))
    m["updated_at"] = now
    _put(c, _msg_key(sid, n), m, by, now)


def report(board: Board, p: Principal, session_id: Any, events: Any = None, state: Any = None, pid: Any = None,
           resume: Any = None, seq: Any = None) -> dict:
    """The node reports what happened in one of its sessions, in order (`events`: delivered, turn_started,
    turn_ended, approval_requested, ended), and how the session stands now (`state`, `pid`, `resume`: the provider
    confirmed its session exists). Only the node the session is assigned to may; a repeated report changes nothing.
    `seq` orders a node's reports on one session (it grows with every report the node sends, across its restarts):
    events only ever move a message forward, so they are applied whatever the order, but the status is a snapshot,
    and one that is not newer than the last applied is ignored. Otherwise a report whose call timed out on the node
    and reached the hub late could put a dead pid and `running` back after the node's final report. A report without
    `seq` is applied as it comes."""
    nodes._require_node(board, p)
    events = _events([] if events is None else events)
    if state is not None and state not in SESSION_STATES:
        raise Invalid("state must be one of " + ", ".join(SESSION_STATES))
    if pid is not None and (isinstance(pid, bool) or not isinstance(pid, int)):
        raise Invalid("pid must be an integer")
    if resume is not None and not isinstance(resume, bool):
        raise Invalid("resume must be true or false")
    if seq is not None and (isinstance(seq, bool) or not isinstance(seq, int) or seq < 1):
        raise Invalid("seq must be a positive integer")
    now = board.now()
    with db.write_tx(board.conn) as c:
        r = _get(c, SESSION_PREFIX + _sid(session_id))
        if not isinstance(r, dict) or r.get("node") != p.name:
            raise NotFound(f"session {session_id} is not hosted on this node")
        if r["state"] == "ended":
            return _node_view(r)        # idempotent: nothing is reported into an ended session
        for ev in events:
            _apply(c, r, ev, p.name, now)
            if r["state"] == "ended":
                break
        if r["state"] != "ended" and (seq is None or seq > int(r.get("status_seq") or 0)):
            if seq is not None:
                r["status_seq"] = seq
            if state is not None and state != "ended":
                r |= {"state": state, "pid": pid}
            if resume is not None:
                # As the node's host knows it: confirmed by the provider, or corrected when a start was refused
                # (session_host._correct_start_mode). This is what a restarted node reopens the session with.
                r["resume"] = resume
        r |= {"updated_at": now, "node_reported_at": now}
        _put(c, SESSION_PREFIX + r["session_id"], r, p.name, now)
    return _node_view(r)


def holds(board_or_conn: Any, agent: str, project: Any, post_id: Any, now: float) -> bool:
    """A hosted session of `agent` in `project` was just woken for this request, or read it after being woken: the
    dispatcher and the undelivered alert leave the request to it (hosted_wake.holds)."""
    from . import hosted_wake
    return hosted_wake.holds(board_or_conn, agent, project, post_id, now)


# ---------------------------------------------------------------- waiting (long-poll, as Board.read_updates_async)


def _wait_seconds(wait: Any, most: int) -> float:
    if isinstance(wait, bool) or not isinstance(wait, (int, float)) or wait < 0:
        raise Invalid("wait must be a number of seconds, zero or more")
    return float(min(wait, most))


async def _poll(fetch: Callable[[], dict], done: Callable[[dict], bool], seconds: float,
                gone: Callable[[], Any] | None = None) -> dict:
    """`fetch()` now, then again every WAIT_POLL_SECONDS until `done` or the time is up. Each read runs briefly in a
    worker thread (sqlite blocks); the wait between reads holds neither a thread nor the event loop. The deadline is
    wall time, not the board's clock: a wait must end even when that clock is a test's. `gone` (awaited before each
    sleep) ends the wait early: the caller hung up, or the server is stopping and must not be held up by a waiter."""
    import anyio
    from anyio import to_thread

    deadline = time.monotonic() + seconds
    out = await to_thread.run_sync(fetch)
    while not done(out) and time.monotonic() < deadline and not (gone is not None and await gone()):
        await anyio.sleep(max(0.0, min(WAIT_POLL_SECONDS, deadline - time.monotonic())))
        out = await to_thread.run_sync(fetch)
    return out


async def node_sessions_wait(board: Board, p: Principal, wait: Any = 0, seen: Any = None, gone=None) -> dict:
    """node_sessions, held back for up to `wait` seconds while its version is still the one the node has `seen`."""
    seconds = _wait_seconds(wait, NODE_WAIT_MAX)
    if seen is not None and (not isinstance(seen, str) or not _SEEN.fullmatch(seen)):
        raise Invalid("seen must be a version this route returned")
    return await _poll(lambda: node_sessions(board, p), lambda out: seen is None or out["version"] != seen, seconds,
                       gone)


async def message_wait(board: Board, p: Principal, session_id: Any, n: Any, wait: Any = 0, gone=None) -> dict:
    """`message`, held back for up to `wait` seconds until the message's turn is over."""
    seconds = _wait_seconds(wait, HUMAN_WAIT_MAX)
    return await _poll(lambda: message(board, p, session_id, n), lambda out: out["message"]["state"] in SETTLED,
                       seconds, gone)
