"""Hosted sessions, hub side: the registry of the agent conversations a node keeps open (DESIGN_NOTES "Session host").

A hosted session is one long-lived agent process on a node (session_host.py) that takes turns from messages. The hub
keeps the record and the queue; the node (`board node`, node_sessions.py) fetches both, runs the turns from ITS OWN
runner settings and reports what happened. Nothing executable crosses the wire: the node takes the agent, the project,
an optional model name and the message text.

- No schema change: everything lives in `board_state`, like nodes.py's records.
  `host.session.<uuid>`: agent, project, node, state, pid, turn count, queue depth, the last result's facts, the LAST
  reply text, times, the stop flag. `host.msg.<uuid>.<n>`: kind, text, state (queued | delivered | done | failed),
  detail. A message's text is dropped from its record once its turn is over.
- The hub issues the session id (a UUID). One session per (agent, project) that has not ended. A provider that
  issues an id of its own for the conversation (Codex's thread id) has it on the record too (`provider_id`), as
  the node reported it: for the human to see, never sent to a node.
- An agent the hub's `[dispatch.agent_projects]` lists runs only in the projects listed for it (agent_accounts.py):
  decided when a session is CREATED, inside the write that creates it, and nowhere else. Taking a project off the
  list stops new sessions there; what already exists proceeds (and is woken, hosted_wake.py) until the human
  closes it.
- Placement mirrors a node run: the hub's `[dispatch.nodes.<name>]` entry must cover the project
  (`nodes.grant_covers`) and the agent, and the node must be live, offer the project and a runner for the agent, and
  say it hosts sessions (its `[dispatch.sessions] enabled`). The node checks its own checkout again before it starts.
- Only the human starts, messages, reads and closes a session (`/api/hosted-sessions`, `board session`). No agent
  route reaches this module, and the reply text is returned to the human alone: never to an agent, and never to the
  node (which wrote it). The exceptions, with child sessions on: a child session's reply goes to its parent session
  as untrusted data inside a turn the hub writes (hosted_results.py); and an agent session with the human's spawn
  grant may start, message, see and stop child sessions of ITS OWN through the board tools (hosted_spawn.py, which
  checks who is calling and calls `close_session` and the tree's functions here).
- Only the assigned node reports on a session (`/api/nodes/sessions`). Reports are idempotent: a message moves
  queued -> delivered -> done | failed and never back, so a repeated or late event changes nothing.
- The host never retries a lost turn. A turn that was lost (timeout, the process died, the node restarted or went
  silent) is `failed` with the reason; the human sends again if they want it again.
- `wake` is the one message kind that is not the human's: hosted_wake.py queues it, with text the hub builds itself,
  when a board post is addressed to the session's agent in the session's project.
- A session may have children (hosted_tree.py, off unless `[dispatch.sessions.spawn] enabled = true` on the hub):
  the same records with a `parent`. "One session per (agent, project)" and wakes are for roots only, and a parent
  that ends takes its children with it.
- `child` is the other message kind that is not the human's (hosted_results.py, with child sessions on): a child's
  results, delivered to its parent as a turn whose text the hub writes.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
import uuid
from typing import Any, Callable

from . import db, nodes
from .core import Board, Conflict, Invalid, NotFound, Principal
from .nodes import _get, _put
from .providers import SESSION_ID_RE, cap_reply, valid_provider_id, validate_model

SESSION_PREFIX = "host.session."   # <uuid>: the session record
MSG_PREFIX = "host.msg."           # <uuid>.<n>: one message, n counting from 1 per session
KINDS = ("user", "wake", "child", "task")  # `user`: the human's (start, send); `wake`, `child`: the hub's own
                                    # (hosted_wake.py, hosted_results.py); `task`: from the agent session that started
                                    # this one, in a frame the hub writes (hosted_spawn.py)
MSG_STATES = ("queued", "delivered", "done", "failed")
SETTLED = ("done", "failed")
SESSION_STATES = ("starting", "idle", "running", "waiting", "ended")   # session_host.STATES, as the node reports them
EVENT_KINDS = ("delivered", "turn_started", "turn_ended", "approval_requested", "ended")
# The one thing a node may still say about a session that has ended: it removed the child's own worktree.
WORKTREE_REMOVED = "worktree_removed"
MESSAGE_MAX_BYTES = 64 * 1024      # one message's text
MAX_QUEUED = 50                    # messages waiting in one session
MAX_EVENTS = 200                   # events in one report
REPLY_LIMIT = 64 * 1024            # the reply text one event may carry; it is capped again here (providers.cap_reply)
SHOW_MESSAGES = 20                 # the most recent messages `show` lists
KEEP_ENDED_SECONDS = 7 * 86400     # an ended session's record is kept this long
KEEP_SETTLED_SECONDS = 86400       # a settled message's record, in a session that goes on
NODE_WAIT_MAX = 25                 # seconds one long-poll of a node may wait
NODE_REPORT_FEATURES = ("answered",)    # report fields this hub takes beyond the first ones (`_listed`)
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
                   "in_flight", "last_result", "denials", "stop", "error", "created_at", "updated_at", "ended_at",
                   "provider_id")    # the provider's own id for it (Codex's thread id), as its node reported it
_MSG_FIELDS = ("n", "kind", "state", "ok", "detail", "denials", "created_at", "delivered_at", "ended_at")


def _human_view(board: Board, r: dict) -> dict:
    """The session as the human sees it: everything, the last reply included."""
    out = {k: r.get(k) for k in _SESSION_FIELDS}
    out |= {"reply": r.get("reply"), "reply_for": r.get("reply_for"),
            "node_live": board.now() - _node_seen(board.conn, r["node"]) <= nodes.NODE_STALE_SECONDS,
            "transcript": f"data/node-logs/sessions/{r['session_id']}.jsonl on node {r['node']}"}
    home = account_home(board.conn, r)
    if home is not None:        # the account folder its node says this agent's runner uses (agent_accounts.py)
        out["account_home"] = home
    from . import hosted_tree
    return out | hosted_tree.view(r)        # parent, access and the rest; nothing for a session from before trees


def account_home(conn, r: dict) -> str | None:
    """The account folder ([dispatch.runner_homes]) the session's node says its agent's runner uses, from that
    node's latest hello: a path, never anything read from inside it. None: the runner has none, or the node does not
    say (one from before account folders)."""
    info = _get(conn, nodes.NODE_PREFIX + str(r.get("node")))
    homes = info.get("homes") if isinstance(info, dict) else None
    if not isinstance(homes, dict):
        return None
    home = homes.get(nodes.runner_key(info, r.get("agent"), r.get("runtime")))
    return home if isinstance(home, str) else None


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
            from . import hosted_results       # what its children sent it, kept for the human until now
            c.execute("DELETE FROM board_state WHERE key >= ? AND key < ?",
                      _range(f"{hosted_results.RESULT_PREFIX}{r['session_id']}."))
            c.execute("DELETE FROM board_state WHERE key = ?", (hosted_results.OUTBOX_PREFIX + r["session_id"],))


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
    from . import agent_accounts
    agent_accounts.check(board, agent, project)     # the hub's opt-in list, first: its reason beats placement's
    node = _place(board, project, agent, runtime, allowed)
    with db.write_tx(board.conn) as c:
        now = board.now()       # read under the write lock
        agent_accounts.check(board, agent, project)     # ... and in the write that makes the session
        _prune(c, now)
        for other in _sessions(c):
            if (other.get("agent") == agent and other.get("project") == project and other.get("state") != "ended"
                    and not other.get("parent")):       # one ROOT per (agent, project): children do not count
                raise Conflict(f"{agent} already has a hosted session in {project}: {other['session_id']}. Send to "
                               f"it (`board session send {other['session_id']} ...`) or close it first")
        r = {"session_id": str(uuid.uuid4()), "agent": agent, "runtime": runtime, "project": project, "node": node,
             "model": model, "state": "idle", "pid": None, "resume": False, "turns": 0, "queued": 0,
             "in_flight": None, "next_n": 1, "last_result": None, "reply": None, "reply_for": None, "denials": [],
             "stop": None, "error": None, "created_by": p.name, "created_at": now, "updated_at": now,
             "node_reported_at": None, "ended_at": None}
        from . import hosted_tree
        if hosted_tree.spawn_config(board).enabled:
            # A root that may get children. Its access is its node's to say (derived from the runner there).
            r |= {"parent": None, "root": r["session_id"], "depth": 0, "access": None}
        m = _enqueue(c, r, "user", text, p.name, now)
        from . import hosted_spawn, hosted_wake
        hosted_wake.begin(c, r["session_id"], now)      # woken for posts made from now on (PR 3)
        hosted_spawn.human_messaged(c, r, now)     # the agent's count of child turns without the human starts over
    return {"session": _human_view(board, r), "message": _msg_view(m)}


def send(board: Board, p: Principal, session_id: Any, text: Any) -> dict:
    """Queue a message for the session: its next turn if it is idle, the turn after the ones ahead of it otherwise."""
    _human(board, p, "message a hosted session")
    text = _text(text)
    _settle(board, _session(board.conn, session_id))
    with db.write_tx(board.conn) as c:
        now = board.now()       # read under the write lock
        r = _session(c, session_id)
        m = _enqueue(c, r, "user", text, p.name, now)
        from . import hosted_spawn
        hosted_spawn.human_messaged(c, r, now)     # a tree's count of child turns without the human starts over
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
    from . import hosted_tree
    kids = hosted_tree.children_view(board, r)
    if kids:
        out["children"] = kids
    from . import hosted_results
    results, dropped = hosted_results.view(board.conn, r["session_id"], board)
    if results:
        out["results"] = results        # what its children sent it (hosted_results.py)
    if dropped:
        out["results_dropped"] = dropped    # undelivered results the retention limit took away: how many, when
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
    r = close_session(board, p.name, session_id)
    return _human_view(board, r)


def close_session(board: Board, by: str, session_id: Any, closed_by: str | None = None) -> dict:
    """`close`, without the caller check: the human's route, and the agent-callable cancel for a child that agent
    started (hosted_spawn.py, `closed_by="caller"`, which the child's record keeps for the audit)."""
    who = "the session that started it" if closed_by == "caller" else "the human"
    with db.write_tx(board.conn) as c:
        now = board.now()       # read under the write lock
        r = _session(c, session_id)
        if r["state"] != "ended":
            from . import hosted_tree
            if closed_by is not None:
                r["closed_by"] = closed_by
            if (now - _node_seen(c, r["node"]) > nodes.NODE_STALE_SECONDS   # away, or no longer hosting sessions
                    or (hosted_tree.is_child(r) and not hosted_tree.node_supports(c, r["node"]))):   # ... or children
                _end(c, r, f"closed by {who} (its node was not connected)", by, now,
                     results=_results_on(board))
            elif not r.get("stop"):
                r |= {"stop": "close", "updated_at": now}
                hosted_tree.cascade(c, r, by, now)      # its children are closed with it, now
            _put(c, SESSION_PREFIX + r["session_id"], r, by, now)
    from . import hosted_spawn
    hosted_spawn.flush(board)
    return r


def _results_on(board: Board) -> bool:
    from . import hosted_results
    return hosted_results.enabled(board)


SHUTDOWN_BOUND_MAX = 3600.0      # seconds: the most a node may say it takes to stop a process (node_sessions)


def may_still_run(conn, r: dict, now: float) -> bool:
    """May this ENDED session's process still be running on its node? Decided from current facts at every
    evaluation, nothing stored for it. No, once there is proof that the node's SESSION loop has reconciled past the
    end: the node ended it itself (its own `ended`), or the loop has applied a listing built after the end (`applied`
    on its fetch: the session was absent from it, so the loop closed it; a node process that restarted does the same
    with its first listings, after its startup recovery). A hello is liveness only: it proves nothing about the loop.
    Otherwise, until the node's own shutdown bound has passed since it was last able to run work (`last_able`; the
    bound is its self-stop time and kill grace with what its deadline watchdog may add, node_sessions.shutdown_bound,
    which its session loop states with every fetch, before any work is listed to it; until it has, the cap): by then
    it has stopped every turn of its own (limit e: unless its session loop held its lock for longer). A node from before the spawn protocol: no (as before).
    Owner decision, review round 6: once the loop has reconciled, the slot is free, though the old process may still
    be inside its kill grace (signalled, given no work, gone within that grace); the node itself starts nothing in
    that checkout meanwhile (node_groups)."""
    if r.get("state") != "ended" or r.get("ended_by") == "node" or r.get("node_ended_at") is not None:
        return False
    from . import hosted_tree
    node = r.get("node") or ""
    if not hosted_tree.node_spawns(conn, node):
        return False
    loop = _loop(conn, node)
    if float(loop.get("applied_at") or 0) > float(r.get("ended_at") or 0):
        return False
    heard = last_able(conn, node)
    bound = loop.get("bound")
    bound = (min(float(bound), SHUTDOWN_BOUND_MAX) if isinstance(bound, (int, float)) and not isinstance(bound, bool)
             else SHUTDOWN_BOUND_MAX)
    return now < heard + bound


LOOP_CONTACT_EVERY = 5.0     # seconds: how often the session loop's contact time is written (`note_contact`)


def last_able(conn, node: str) -> float:
    """When this node's session loop was last able to run work: its last successful call to the hub (`note_contact`,
    recorded at most every LOOP_CONTACT_EVERY seconds, which is added here, so the time is never early), or its last
    hello."""
    contact = _loop(conn, node).get("contact")
    contact = float(contact) + LOOP_CONTACT_EVERY if isinstance(contact, (int, float)) else 0.0
    return max(_node_seen(conn, node), contact)


def note_contact(board: Board, p: Principal) -> None:
    """Called in ONE place, by the routes of the session loop's two calls (its listing fetch and its session report)
    once the call has succeeded: whatever path the call took inside, an ended session, a duplicate stamp, an empty
    report, this is a successful call to the hub, which resets the node's self-stop clock, so `last_able` counts from
    it. A call refused (4xx) or not authenticated does neither."""
    if p.is_human or p.runtime != nodes.NODE_RUNTIME:
        return
    now = board.now()
    if now - float(_loop(board.conn, p.name).get("contact") or float("-inf")) < LOOP_CONTACT_EVERY:
        return
    with db.write_tx(board.conn) as c:
        loop = _loop(c, p.name)
        loop["contact"] = max(float(loop.get("contact") or 0), board.now())
        _put(c, LOOP_PREFIX + p.name, loop, "hub", board.now())


def holds_slot(conn, r: dict, now: float) -> bool:
    """The session counts against the limits on live sessions (hosted_spawn: max_live_children; hosted_tree:
    max_children) and occupies its checkout for the hub's decisions (hosted_wake.turn_sessions): it has not ended,
    or it ended and `may_still_run`."""
    return r.get("state") != "ended" or may_still_run(conn, r, now)


def _end(c, r: dict, detail: str, by: str, now: float, results: bool = False) -> None:
    """The session is over: every message that was not settled fails with the reason. The caller stores `r`.
    `results`: child sessions' results are on (hosted_results.py), so a child that ends tells its parent.
    Idempotent: a session that has already ended as stored (a cascade above ended it while the caller held an older
    copy) is not ended again. `r` becomes the stored record, so the caller stores it unchanged: one end, one audit
    line, one result, the first reason kept."""
    stored = _get(c, SESSION_PREFIX + r["session_id"])
    if isinstance(stored, dict) and stored.get("state") == "ended":
        r.clear()
        r.update(stored)
        return
    detail = (detail or "ended")[:300]
    cut = []        # the messages this end fails, as they were (a child's parent is told of each)
    for m in _messages(c, r["session_id"]):
        if m.get("state") not in SETTLED:
            cut.append(dict(m))
            m |= {"state": "failed", "ok": False, "detail": f"session ended: {detail}"[:300], "text": "",
                  "ended_at": now, "updated_at": now}
            _put(c, _msg_key(r["session_id"], m["n"]), m, by, now)
    r |= {"state": "ended", "error": detail, "ended_at": now, "updated_at": now, "pid": None, "queued": 0,
          "in_flight": None}
    from . import hosted_tree
    hosted_tree.cascade(c, r, by, now)      # however a parent ended, its children are closed
    from . import hosted_results
    hosted_results.ended(c, r, detail, now, record=results, cut=cut)
    from . import hosted_spawn
    hosted_spawn.ended(c, r, now)           # an agent-started child: one line for its tree's audit thread


def _lost(conn, r: dict, now: float) -> bool:
    """The session has a turn in flight or a close pending, and its node has been gone long enough (or no longer
    hosts sessions) that neither will be carried out. Only what the node itself did counts: its hellos as a session
    host and its reports on this session."""
    if r.get("state") == "ended" or not (r.get("stop") or r.get("in_flight") is not None):
        return False
    from . import hosted_tree
    if hosted_tree.orphaned(conn, r):
        return True     # a child on a node that hosts no children: that node's hellos say nothing about it
    seen = _node_seen(conn, r["node"])
    return not seen or now - max(seen, float(r.get("node_reported_at") or 0)) > nodes.NODE_LOST_SECONDS


def _settle(board: Board, r: dict) -> dict:
    """On the human's reads: a session whose node went silent (nodes.NODE_LOST_SECONDS, like a node run) no longer
    has a turn in flight, and one being closed is closed. The node stops its own turns well before that
    (node_sessions.py), so the failed turn is not still running somewhere."""
    now = board.now()
    from . import hosted_tree
    if not hosted_tree.orphaned(board.conn, r) and not _lost(board.conn, r, now):
        return r
    with db.write_tx(board.conn) as c:
        # Decided again inside the transaction, on what is stored now: a node that reconnected and reported a
        # freshly delivered turn between the check above and here must not have that turn failed under it.
        r = _session(c, r["session_id"])
        if hosted_tree.orphaned(c, r):
            # A child whose node hosts no child sessions (any more), or no longer the level it needs: nobody there
            # will run it, report on it or end it. Ended here, its turn in flight failed as lost. (The node's next
            # hello does the same.)
            _end(c, r, hosted_tree.orphan_reason(c, r), "hub", now, results=_results_on(board))
            _put(c, SESSION_PREFIX + r["session_id"], r, "hub", now)
            return r
        if not _lost(c, r, now):
            return r
        results = _results_on(board)
        if r.get("stop"):
            _end(c, r, "closed by the human (its node went silent)", "hub", now, results=results)
        else:
            for m in _messages(c, r["session_id"]):
                if m.get("state") == "delivered":
                    m |= {"state": "failed", "ok": False, "text": "", "ended_at": now, "updated_at": now,
                          "detail": "node_lost: the node went silent (or stopped hosting sessions) while the turn "
                                    "was in flight; it was not replayed"}
                    _put(c, _msg_key(r["session_id"], m["n"]), m, "hub", now)
                    r["last_result"] = {"message": m["n"], "ok": False, "detail": m["detail"], "denials": [],
                                        "meta": {}, "at": now}
                    from . import hosted_results
                    if m.get("kind") == hosted_results.KIND:
                        # Not confirmed (a turn its provider started had its results delivered then): they go once
                        # more, or are kept undelivered.
                        hosted_results.carried(c, r, m, False, now)
                    if results and hosted_results.hosted_tree.is_child(r):
                        hosted_results.turn(c, r, m, ok=False, delivered=True, detail=m["detail"], text="",
                                            denials=[], now=now)
            r |= {"in_flight": None, "state": "idle", "pid": None, "updated_at": now}
        _put(c, SESSION_PREFIX + r["session_id"], r, "hub", now)
    return r


# ---------------------------------------------------------------- the node's side


def node_sessions(board: Board, p: Principal, tree: bool = False, applied: Any = None, bound: Any = None) -> dict:
    """This node's sessions that have not ended, each with its unsettled messages (oldest first). Only the first
    queued message of a session carries its text: the node writes one at a time. `version` changes whenever
    anything the node acts on does, so the node's long-poll can wait on it.
    `tree`: the node said, on this request, that it speaks the child-session protocol (hosted_tree.py). Only then
    is a child session listed to it, or a root asked for its access: a node that does not know the protocol (one
    rolled back to an earlier version) would open a child as an ordinary session of its agent, in the shared
    checkout and with its runner's own permissions, whatever access and isolation the child was made with."""
    nodes._require_node(board, p)
    from . import hosted_results, hosted_spawn, hosted_tree, hosted_wake
    t0 = board.now()
    _note_session_loop(board, p.name, applied, bound)      # before anything runnable is listed to it
    if tree >= hosted_tree.SPAWN_PROTOCOL and hosted_tree.spawn_config(board).enabled:
        hosted_spawn.ensure_secret(board)      # the listing gives each session its binding (read-only there)
    hosted_spawn.flush(board)                  # audit lines and Needs-you items of agent-started child sessions
    # PR 3: queue the wakes that are due; a wake is validated in the transaction that shows it (hosted_wake.listing).
    # A child's results waiting for a free parent here become its next turn in that same transaction, after the
    # wakes were checked (hosted_results.deliver): so they are made and shown on what is stored then.
    # An agent's task for a child is shown with its text only once it is reserved here too (`_reserve_tasks`, in the
    # same write transaction): from then on the dispatcher counts its checkout busy (hosted_wake.turn_projects).
    out = hosted_wake.listing(board, p.name, lambda: _listed(board, p, tree),
                              due=lambda conn: (hosted_results.due(board, conn, p.name)
                                                or _tasks_due(board, conn, p.name)),
                              deliver=lambda c, now: (hosted_results.deliver(board, c, p.name, now),
                                                      _reserve_tasks(board, c, p.name, now)))
    if tree >= hosted_tree.SPAWN_PROTOCOL:      # (only a node of the protocol says what it applied)
        _note_listing(board, p.name, out.get("version"), t0)
    return out


LOOP_PREFIX = "host.node_loop."      # <node>: what the hub knows of a node's SESSION loop (not of its hellos)
LOOP_VERSIONS = 32


def _loop(conn, node: str) -> dict:
    v = _get(conn, LOOP_PREFIX + node)
    return v if isinstance(v, dict) else {}


def _note_listing(board: Board, node: str, version: Any, at: float) -> None:
    """A listing was built for this node's session loop at (or after) `at`: kept by version, so that when the loop
    says it applied that version (`applied`), the hub knows the loop has reconciled everything that happened before
    `at` (a session ended by then is absent from it, and the loop closed it)."""
    if not isinstance(version, str):
        return
    known = _loop(board.conn, node).get("versions", {}).get(version)
    if isinstance(known, (int, float)) and not _waiting_since(board.conn, node, known):
        return      # (the same listing again, and no end of this node's waits on a newer one: nothing to write)
    with db.write_tx(board.conn) as c:
        loop = _loop(c, node)
        versions = loop.get("versions") if isinstance(loop.get("versions"), dict) else {}
        versions[version] = max(at, float(versions.get(version) or 0))      # the latest time it was built
        loop["versions"] = dict(sorted(versions.items(), key=lambda kv: kv[1])[-LOOP_VERSIONS:])
        _put(c, LOOP_PREFIX + node, loop, "hub", board.now())


def _waiting_since(conn, node: str, at: float) -> bool:
    """A session of this node ended after `at` and waits for proof that the session loop has reconciled past it."""
    return any(r.get("node") == node and r.get("state") == "ended" and r.get("ended_by") != "node"
               and float(r.get("ended_at") or 0) >= at for r in _sessions(conn))


def _note_session_loop(board: Board, node: str, applied: Any, bound: Any) -> None:
    """What the node's session loop says with its fetch: the listing version it last applied (so `applied_at`, when
    that listing was built) and its shutdown bound. Written only when it changes."""
    if bound is not None and (isinstance(bound, bool) or not isinstance(bound, (int, float))
                              or not 0 <= bound <= SHUTDOWN_BOUND_MAX):
        raise Invalid(f"bound must be a number of seconds, at most {SHUTDOWN_BOUND_MAX:g}")
    if applied is not None and (not isinstance(applied, str) or not _SEEN.fullmatch(applied)):
        raise Invalid("applied must be a version this route returned")
    loop = _loop(board.conn, node)
    at = (loop.get("versions") or {}).get(applied) if applied is not None else None
    new_at = isinstance(at, (int, float)) and at > float(loop.get("applied_at") or 0)
    new_bound = bound is not None and float(bound) != loop.get("bound")
    if not (new_at or new_bound):
        return
    with db.write_tx(board.conn) as c:
        loop = _loop(c, node)
        if new_at:
            loop["applied_at"] = max(float(loop.get("applied_at") or 0), float(at))
        if new_bound:
            loop["bound"] = float(bound)
        _put(c, LOOP_PREFIX + node, loop, "hub", board.now())


def _listed(board: Board, p: Principal, tree: bool = False) -> dict:
    """node_sessions' answer, read inside the transaction hosted_wake.listing opened."""
    nodes._require_node(board, p)       # again, in that transaction: a pause may have landed since the first check
    from . import hosted_spawn, hosted_tree
    spawn = hosted_tree.spawn_config(board).enabled
    # A node that speaks the spawn protocol (`tree` 2): each session's binding, a child of an interactive session,
    # and the messages whose turn their starter cancelled. Nothing of it for an earlier node, or with spawn off.
    spawns = spawn and int(tree) >= hosted_tree.SPAWN_PROTOCOL
    out, mark = [], []
    every = _sessions(board.conn)
    for r in sorted(every, key=lambda r: r["session_id"]):
        if (r.get("node") != p.name or r.get("state") == "ended" or (hosted_tree.is_child(r) and not tree)
                or (hosted_tree.interactive(r) and not spawns)):
            continue
        msgs, first = [], True
        for m in _messages(board.conn, r["session_id"]):
            if m.get("state") in SETTLED:
                continue
            item = {"n": m["n"], "kind": m.get("kind"), "state": m.get("state")}
            if spawns and m.get("cancel") is not None:
                item["cancel"] = True       # its starter cancelled it: the node stops it, or never hands it over
            elif m.get("state") == "queued" and first:
                first = False
                if m.get("kind") == "task" and m.get("shown_at") is None:
                    # An agent's message to its child is an automatic turn, like a wake or a result turn: it is shown
                    # without its text (so the node does not hand it over) until it is reserved (`_reserve_tasks`),
                    # which waits while a dispatched run uses the checkout the child works in. The human's own never
                    # waits.
                    item["held"] = "checkout" if _checkout_busy(board, r) else "reserving"
                else:
                    item["text"] = m.get("text")
            msgs.append(item)
        # A full child's parent is spared on its node while the child's subtree is open (node_sessions
        # `_lends_full`, hosted_tree.subtree_open): the hub's own record of it, said only to a node of the spawn
        # protocol (an older node does not read it), and only for a child at `full`, the one kind that needs its
        # parent's live process.
        held = bool(spawns and hosted_tree.is_child(r) and r.get("access") == "full"
                    and any(hosted_tree.subtree_open(board, board.conn, r, every).values()))
        out.append({k: r.get(k) for k in ("session_id", "agent", "runtime", "project", "model", "stop")}
                   | {"resume": r.get("resume") is True, "messages": msgs,
                      # The stamp of the last status applied (`report`): the node stamps above it, whatever its
                      # own clock says. Not part of `version`: it changes with the node's own reports.
                      "status_seq": int(r.get("status_seq") or 0)}
                   # A child's parent, access word, isolation and effort; `access: None` on a root when child
                   # sessions are on. Nothing otherwise: the entry is then what it was before there were trees.
                   | hosted_tree.node_extra(r, bool(spawn and tree), hosted_spawn.bind_key(board.conn, r["session_id"])
                                            if spawns and hosted_spawn.gives_binding(board.conn, r, board.now())
                                            else None)
                   | ({"subtree_open": True} if held else {}))
        mark.append([r["session_id"], r.get("stop"), [[m["n"], m["state"]] + (["cancel"] if m.get("cancel") else [])
                                                      + ([m["held"]] if m.get("held") else []) for m in msgs],
                     "bind" in out[-1], held])  # a binding given or withdrawn, a subtree open or not: news
    version = hashlib.sha256(json.dumps(mark).encode()).hexdigest()[:16]
    # What this hub takes in a node's reports beyond what every hub took: a node sends such a field only to a hub
    # that names it here, so a newer node's reports reach an older hub exactly as before (its strict model refuses an
    # unknown field, and the report would be lost).
    return {"node": p.name, "sessions": out, "version": version, "features": list(NODE_REPORT_FEATURES)}


def checkout_runs(board: Board) -> tuple[list[dict], list[dict]]:
    """(the dispatcher's checkout claims, its active run records): what `_checkout_busy` reads, read once."""
    from . import dispatch
    return dispatch.checkout_claims(board.conn, board.now()), dispatch._active_records(board)


def _checkout_busy(board: Board, r: dict, runs: tuple[list[dict], list[dict]] | None = None) -> bool:
    """A dispatched run may still be using the checkout this session's turns run in: one run per checkout, both
    ways round. The identity is the node's: a run on a node is recorded with the project path as its directory
    (dispatch._run_dir_for), so it holds this session when it is on the same node and for the same project; a run on
    the hub holds it when its directory is the one the hub's settings give the project (as for a wake, and on the
    safe side when the session's node is another machine). A child with a worktree of its own runs elsewhere and is
    never held. Unreadable records hold it (safe side). `runs`: `checkout_runs` read once by a caller that asks
    for many sessions (the dashboard's lineage view)."""
    if r.get("isolation") == "worktree":
        return False
    from . import dispatch, hosted_wake
    try:
        hub_dir = os.path.realpath(hosted_wake._run_dir(board, r["project"]))
        # The runs on record, and the launches the dispatcher reserved whose run is not on record yet (its claims,
        # made in its launch transaction): one identity for both.
        claims = runs[0] if runs is not None else dispatch.checkout_claims(board.conn, board.now())
        if any(d.get("unreadable") for d in claims):
            return True
        for d in (runs[1] if runs is not None else dispatch._active_records(board)) + claims:
            cwd = d.get("cwd")
            if not isinstance(cwd, str) or not cwd:
                continue
            if d.get("node"):
                if d["node"] == r.get("node") and os.path.normpath(cwd) == os.path.normpath(r["project"]):
                    return True
            elif os.path.realpath(cwd) == hub_dir:
                return True
        return False
    except Exception:
        return True


def _next_task(conn, r: dict) -> dict | None:
    """The session's next message to hand over, when it is an agent's task not reserved yet."""
    for m in _messages(conn, r["session_id"]):
        if m.get("state") == "queued" and m.get("cancel") is None:
            return m if m.get("kind") == "task" and m.get("shown_at") is None else None
    return None


def _tasks_due(board: Board, conn, node: str) -> bool:
    """Read only: a session on this node has an agent's task to reserve now (its checkout is free)."""
    try:
        return any(r.get("node") == node and r.get("state") != "ended" and int(r.get("queued") or 0)
                   and _next_task(conn, r) is not None and not _checkout_busy(board, r) for r in _sessions(conn))
    except Exception:
        return False


def _reserve_tasks(board: Board, c, node: str, now: float) -> None:
    """Inside the write transaction that shows this node its sessions: each agent's task that is next for its session
    and whose checkout no dispatched run uses is reserved (`shown_at`), so it is shown with its text in this same
    transaction, and the dispatcher counts its checkout busy from now on (hosted_wake.turn_projects)."""
    for r in _sessions(c):
        if r.get("node") != node or r.get("state") == "ended" or not int(r.get("queued") or 0):
            continue
        m = _next_task(c, r)
        if m is not None and not _checkout_busy(board, r):
            m |= {"shown_at": now, "updated_at": now}
            _put(c, _msg_key(r["session_id"], m["n"]), m, "hub", now)


def shown_task(conn, r: dict) -> bool:
    """An automatic turn for this session was reserved and shown to its node, and the node has not reported on it
    yet (its turn in flight, or its end): its checkout is taken all the same (hosted_wake.turn_projects). An agent's
    task (`_reserve_tasks`), and a turn that carries child sessions' results (hosted_results._deliver, made and
    shown in the listing's transaction). Also once a task's starter cancelled it: the node may have it already, and
    the reservation holds until the node says it ended (its `turn_ended`, which settles it) or the node is lost
    (turn_projects)."""
    if not int(r.get("queued") or 0):
        return False
    return any(m.get("state") == "queued" and m.get("kind") in ("task", "child") and m.get("shown_at") is not None
               for m in _messages(conn, r["session_id"]))


def _events(events: Any) -> list[dict]:
    if not isinstance(events, list) or len(events) > MAX_EVENTS:
        raise Invalid(f"events must be a list of at most {MAX_EVENTS} events")
    out = []
    for ev in events:
        if not isinstance(ev, dict) or ev.get("kind") not in (*EVENT_KINDS, WORKTREE_REMOVED):
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
        answered = ev.get("answered", False)
        if not isinstance(answered, bool):
            raise Invalid("answered must be true or false")
        out.append({"kind": ev["kind"], "message": n, "ok": ok, "delivered": ev.get("delivered") is not False,
                    "detail": detail, "text": cap_reply(text), "denials": denials, "meta": meta,
                    # turn_ended: the provider itself ended the turn (session_host.HostEvent.answered); a node
                    # from before the field says nothing, which counts as not known
                    "answered": answered})
    return out


def _apply(c, r: dict, ev: dict, by: str, now: float, results: bool = False) -> None:
    """One event from the node, applied to the session `r` (stored by the caller) and its message. A message only
    moves forward, so an event that arrives twice, late or for an unknown message changes nothing. `results`: child
    sessions' results are on (hosted_results.py): a child's settled turn, or its end, is a result for its parent."""
    from . import hosted_results
    kind, n, sid = ev["kind"], ev["message"], r["session_id"]
    if kind == WORKTREE_REMOVED:
        return      # only once the session has ended (`report`)
    if kind == "ended":
        _end(c, r, ev["detail"] or "the node ended the session", by, now, results=results)
        r["ended_by"] = "node"      # its session loop ended it itself: nothing of it is left for the hub to wait for
        return
    if kind == "approval_requested":
        r["denials"] = ev["denials"]
        return
    m = _get(c, _msg_key(sid, n)) if n is not None else None
    if not isinstance(m, dict):
        return
    if m.get("state") in SETTLED:
        if (m.get("kind") == hosted_results.KIND
                and (kind == "turn_started" or (kind == "turn_ended" and ev["answered"]))):
            # The hub settled this result turn without its provider's word (node lost), and the word comes now:
            # the results it carried that wait to go again are delivered after all. The message stays settled.
            hosted_results.confirmed_late(c, r, m, now)
        return
    was = m["state"]
    if m["state"] == "queued":      # whichever event comes first takes it out of the queue
        r["queued"] = max(0, int(r.get("queued") or 0) - 1)
    if kind in ("delivered", "turn_started"):
        if m["state"] == "queued":
            m |= {"state": "delivered", "delivered_at": now, "text": ""}    # the node has it: the hub lets go of it
            r["in_flight"] = n
        if kind == "turn_started":
            r["resume"] = True      # the provider began a turn in it, so its session exists (a write proves nothing)
            if m.get("kind") == hosted_results.KIND and m.get("started_at") is None:
                # Written is not read: the results it carries count as delivered only now that the parent's own
                # provider says the turn started.
                m["started_at"] = now
                hosted_results.carried(c, r, m, True, now)
    else:
        ok = ev["ok"] is True
        detail = ev["detail"] or ("" if ok else "the agent reported that the turn failed"
                                  + (f" ({ev['meta']['subtype']})" if isinstance(ev["meta"].get("subtype"), str) else ""))
        m |= {"state": "done" if ok else "failed", "ok": ok, "detail": detail, "denials": ev["denials"], "text": "",
              "ended_at": now}
        reached = was == "delivered" or ev["delivered"]
        if m.get("kind") == hosted_results.KIND:
            # A turn its provider started was confirmed then (above); one the provider itself ended, with a success
            # or an error of its own (`answered`), is confirmed now. One the host gave up on was not: its results go
            # once more, or are kept undelivered.
            hosted_results.carried(c, r, m, ev["answered"], now)
        if results and hosted_results.hosted_tree.is_child(r):
            hosted_results.turn(c, r, m, ok=ok, delivered=reached, detail=detail, text=ev["text"],
                                denials=ev["denials"], now=now)
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
           resume: Any = None, seq: Any = None, provider_id: Any = None, access: Any = None,
           worktree: Any = None, ceiling: Any = None, checkout: Any = None) -> dict:
    """The node reports what happened in one of its sessions, in order (`events`: delivered, turn_started,
    turn_ended, approval_requested, ended), and how the session stands now (`state`, `pid`, `resume`: the provider
    confirmed its session exists). Only the node the session is assigned to may; a repeated report changes nothing.
    `seq` orders a node's reports on one session (it grows with every report the node sends, across its restarts):
    events only ever move a message forward, so they are applied whatever the order, but the status is a snapshot,
    and one that is not newer than the last applied is ignored. Otherwise a report whose call timed out on the node
    and reached the hub late could put a dead pid and `running` back after the node's final report. A report without
    `seq` is applied as it comes.
    `provider_id`: the provider's own id for the session where it issues one (Codex's thread id). It is kept for the
    record and shown to the human; the hub never sends it back to a node, which resumes from its own record only.
    `access`: the access the session really has on the node (session_access; a root's is derived from the node's
    runner). `worktree`: where a child's own worktree is on the node. Both are for the record and the human, and a
    node sends them only to a hub that asked (the listing's `access`). `ceiling` (a root's): the most a child may
    have under it now, which is `full` only once the session's process has confirmed full access; `checkout` (a
    root's): the directory it runs in there. A `worktree_removed` event is the one thing applied to a session that
    has ended: the node removes a child's worktree after its process is gone."""
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
    if provider_id is not None and valid_provider_id(provider_id) is None:
        raise Invalid("provider_id must be a short plain id")
    from . import hosted_tree, session_access
    if access is not None and access not in session_access.LEVELS:
        raise Invalid("access must be one of " + ", ".join(session_access.LEVELS))
    if ceiling is not None and ceiling not in session_access.LEVELS:
        raise Invalid("ceiling must be one of " + ", ".join(session_access.LEVELS))
    for what, path in (("worktree", worktree), ("checkout", checkout)):
        if path is not None and (not isinstance(path, str) or not path.startswith("/") or len(path) > 1024
                                 or any(ord(ch) < 32 or ord(ch) == 127 for ch in path)):
            raise Invalid(f"{what} must be an absolute path")
    with db.write_tx(board.conn) as c:
        now = board.now()       # read under the write lock
        r = _get(c, SESSION_PREFIX + _sid(session_id))
        if not isinstance(r, dict) or r.get("node") != p.name:
            raise NotFound(f"session {session_id} is not hosted on this node")
        removed = (any(ev["kind"] == WORKTREE_REMOVED for ev in events) and r.get("isolation") == "worktree"
                   and r.get("worktree_removed_at") is None)
        if r["state"] == "ended":
            # Idempotent: nothing is reported into an ended session. But its node removes a child's own worktree
            # only once the process is gone, which is after the session ended here: that one fact is still taken.
            # And the node's own end of a session the hub had already ended: its session loop has reconciled past
            # the end (hosted.may_still_run), recorded once; no end is repeated (no result, cascade or audit).
            ended_here = any(ev["kind"] == "ended" for ev in events) and r.get("node_ended_at") is None
            if removed:
                r["worktree_removed_at"] = now
            if ended_here:
                r["node_ended_at"] = now
            if removed or ended_here:
                _put(c, SESSION_PREFIX + r["session_id"], r, p.name, now)
            return _node_view(r)
        results = _results_on(board)
        for ev in events:
            _apply(c, r, ev, p.name, now, results=results)
            if r["state"] == "ended":
                break
        if removed and r["state"] == "ended":
            r["worktree_removed_at"] = now
        if r["state"] != "ended" and (seq is None or seq > int(r.get("status_seq") or 0)):
            if seq is not None:
                r["status_seq"] = seq
            if state is not None and state != "ended":
                r |= {"state": state, "pid": pid}
            if resume is not None:
                # As the node's host knows it: confirmed by the provider, or corrected when a start was refused
                # (session_host._correct_start_mode). This is what a restarted node reopens the session with.
                r["resume"] = resume
            if provider_id is not None:
                r["provider_id"] = provider_id
            elif resume is False:
                r.pop("provider_id", None)      # the node starts the provider's session anew: the old id is history
            hosted_tree.reported(r, access, worktree, ceiling, checkout)
        r |= {"updated_at": now, "node_reported_at": now}
        _put(c, SESSION_PREFIX + r["session_id"], r, p.name, now)
    if r["state"] == "ended":
        from . import hosted_spawn
        hosted_spawn.flush(board)       # an agent-started child that ended: its audit line, and a limit that lifted
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
    sleep and again after it) ends the wait early: the caller hung up, or the server is stopping and must not be held
    up by a waiter. A caller gone during the sleep gets no further `fetch`: a fetch can hand something over (a wake's
    `handed_at`, hosted_wake.listing), and nobody would receive it."""
    import anyio
    from anyio import to_thread

    deadline = time.monotonic() + seconds
    out = await to_thread.run_sync(fetch)
    while not done(out) and time.monotonic() < deadline and not (gone is not None and await gone()):
        await anyio.sleep(max(0.0, min(WAIT_POLL_SECONDS, deadline - time.monotonic())))
        if gone is not None and await gone():
            break
        out = await to_thread.run_sync(fetch)
    return out


async def node_sessions_wait(board: Board, p: Principal, wait: Any = 0, seen: Any = None, gone=None,
                             tree: bool = False, applied: Any = None, bound: Any = None) -> dict:
    """node_sessions, held back for up to `wait` seconds while its version is still the one the node has `seen`."""
    seconds = _wait_seconds(wait, NODE_WAIT_MAX)
    if seen is not None and (not isinstance(seen, str) or not _SEEN.fullmatch(seen)):
        raise Invalid("seen must be a version this route returned")
    return await _poll(lambda: node_sessions(board, p, tree, applied, bound),
                       lambda out: seen is None or out["version"] != seen, seconds, gone)


async def message_wait(board: Board, p: Principal, session_id: Any, n: Any, wait: Any = 0, gone=None) -> dict:
    """`message`, held back for up to `wait` seconds until the message's turn is over."""
    seconds = _wait_seconds(wait, HUMAN_WAIT_MAX)
    return await _poll(lambda: message(board, p, session_id, n), lambda out: out["message"]["state"] in SETTLED,
                       seconds, gone)
