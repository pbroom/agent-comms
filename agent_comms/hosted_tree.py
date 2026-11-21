"""Parent and child hosted sessions, hub side (DESIGN_NOTES "Session host: parent and child sessions").

A hosted session (hosted.py) can have CHILDREN: sessions started under it, possibly for another agent and model, with
their own isolation and access level. Everything here is off unless the hub says `[dispatch.sessions.spawn] enabled =
true`; with it off no child can be created and nothing else changes.

- *Records.* Still `board_state`, the same `host.session.<uuid>` records. A child's record also holds `parent` and
  `root` (the same id: the tree is one level deep), `depth` 1, `name` (`<role>-<n>`, unique under its parent),
  `title`, `role`, `isolation` (`shared`: the parent's checkout; `worktree`: a git worktree of its own on the node),
  `access` (the word asked of its node), `effort` and `request_id`. A root's `access` is what its node reported
  for it (derived from the node's runner). A record without `parent` is a root, whenever it was made.
- *Who.* `create_child` is the one function that makes a child, and it checks no caller: the human's route
  (`start_child`) does, and an agent-callable spawn tool will after its own grant check. In this PR only the human
  reaches it.
- *Rules.* The parent exists, has not ended, is not being closed and is itself a root (depth 1). The child runs on
  the parent's node, in the parent's project, for an agent that node offers a hostable runner for and the hub's
  `[dispatch.nodes.<node>]` lists (the check a root gets). Its access defaults to the parent's and may be narrower;
  above the parent's is refused. At most `max_children` children of one root that have not ended. The same
  (parent, request_id) returns the child it made the first time.
- *Not for children.* "One session per (agent, project)" (roots only), board wakes (hosted_wake.py: a child has no
  watcher record) and a project's busy directory when the child has its own worktree.
- *Ending.* A parent that ends or is closed takes its children with it (`cascade`); closing a child leaves its
  parent alone.
"""
from __future__ import annotations

import re
import uuid
from typing import Any

from . import db, hosted, nodes, session_access
from .core import Board, Conflict, Invalid, Principal
from .nodes import _get, _put
from .providers import validate_model

TITLE_MAX = 120
REQUEST_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,63}")
OFF = ("child sessions are off: the hub's settings need [dispatch.sessions.spawn] enabled = true before a session "
       "can be started under another")
PARENT_ENDED = "its parent session ended"
# What a child's record holds beyond a root's, as the human sees it.
CHILD_FIELDS = ("name", "child_no", "title", "role", "isolation", "effort", "request_id", "granted", "worktree",
                "worktree_removed_at")


def spawn_config(board: Board):
    """`[dispatch.sessions.spawn]` as the hub has it now. A `[dispatch.sessions]` table that does not validate
    leaves the defaults, which are off."""
    from .dispatch import SessionsConfig, SpawnConfig

    try:
        return SessionsConfig.from_dict((board.s.dispatch or {}).get("sessions", {})).spawn
    except (ValueError, AttributeError):
        return SpawnConfig()


def is_child(r: Any) -> bool:
    return isinstance(r, dict) and isinstance(r.get("parent"), str)


def children(conn, parent_id: str, live: bool = False) -> list[dict]:
    """The sessions started under this one, oldest first (`live`: only those that have not ended)."""
    out = [r for r in hosted._sessions(conn) if r.get("parent") == parent_id
           and not (live and r.get("state") == "ended")]
    return sorted(out, key=lambda r: (float(r.get("created_at") or 0), int(r.get("child_no") or 0)))


def view(r: dict) -> dict:
    """What the human's view of a session gains: nothing for a session from before there were trees."""
    if not ("depth" in r or "access" in r):
        return {}
    out = {"parent": r.get("parent") if is_child(r) else None, "root": r.get("root") or r["session_id"],
           "depth": 1 if is_child(r) else 0, "access": r.get("access")}
    if is_child(r):
        out |= {k: r.get(k) for k in CHILD_FIELDS}
    return out


def node_extra(r: dict, enabled: bool) -> dict:
    """What the node is told about a session beyond agent, project and model. Words only: the node turns `access`
    into flags from its own runner and settings. A root carries `access: None` when child sessions are on, which
    asks its node to report the access its runner has; with them off a root's entry is what it always was."""
    if is_child(r):
        return {"parent": r["parent"], "access": r.get("access"), "isolation": r.get("isolation") or "shared",
                "effort": r.get("effort")}
    return {"access": None} if enabled else {}


def _word(value: Any, allowed: tuple[str, ...], what: str, default: str) -> str:
    if value is None:
        return default
    if not isinstance(value, str) or value not in allowed:
        raise Invalid(f"{what} must be one of " + ", ".join(allowed))
    return value


def _title(title: Any) -> str | None:
    if title is None:
        return None
    if (not isinstance(title, str) or not title.strip() or len(title) > TITLE_MAX
            or any(ord(ch) < 32 or ord(ch) == 127 for ch in title)):
        raise Invalid(f"title must be one line of at most {TITLE_MAX} characters")
    return title.strip()


def _request_id(request_id: Any) -> str | None:
    if request_id is None:
        return None
    if not isinstance(request_id, str) or not REQUEST_ID_RE.fullmatch(request_id):
        raise Invalid("request_id must be a short plain token (letters, digits, . _ : -; at most 64 characters)")
    return request_id


def _existing(conn, parent_id: str, request_id: str | None) -> dict | None:
    """The child this (parent, request_id) already made, with its first message if that is still on record."""
    if request_id is None:
        return None
    for r in hosted._sessions(conn):
        if r.get("parent") == parent_id and r.get("request_id") == request_id:
            first = _get(conn, hosted._msg_key(r["session_id"], 1))
            return {"session": r, "message": first if isinstance(first, dict) else None, "created": False}
    return None


def node_supports(conn, node: str) -> bool:
    """The node's latest hello says it speaks the child-session protocol. One that does not (an earlier version,
    or one rolled back to it) is listed no child and can be given none."""
    info = _get(conn, nodes.NODE_PREFIX + node)
    hosting = info.get("session_host") if isinstance(info, dict) else None
    return isinstance(hosting, dict) and type(hosting.get("tree")) is int and hosting["tree"] >= 1


def _target(board: Board, parent: dict, agent: str, runtime: str, allowed: dict[str, dict] | None) -> None:
    """The child's agent on the PARENT'S node: the hub's grant for that node must cover the project and the agent
    (as for a root), and the node must be connected as a session host with a runner for the agent, speak the
    child-session protocol NOW (its latest hello), and be able to start a child session from that runner at all
    (its hello says at which access words: none for a Codex runner where Codex sessions are off, or one its
    session preparation refuses). The word itself is checked when the child is admitted (`_offers`)."""
    node, project = parent["node"], parent["project"]
    grant = (allowed or {}).get(node)
    if (not isinstance(grant, dict) or not nodes.grant_covers(grant, project)
            or (agent not in grant.get("agents", []) and runtime not in grant.get("agents", []))):
        raise Conflict(f"node {node} may not run {agent} in {project}: a child session runs on its parent's node, "
                       f"and the hub's [dispatch.nodes.{node}] must list the project and the agent or its runtime")
    if nodes.node_for(board, project, agent, runtime, {node: grant}, room=False, sessions=True) != node:
        raise Conflict(f"node {node}, which hosts the parent session, is not connected as a session host with a "
                       f"runner for {agent} (a child session runs on its parent's node)")
    if not node_supports(board.conn, node):
        raise Conflict(f"node {node} does not host child sessions: it runs a version of agent-comms from before "
                       "them (or was rolled back to one). Update it and start `board node` again")
    if not _offers(board.conn, node, agent, runtime):
        raise Conflict(f"node {node} cannot host {agent} as a session: it has a runner for it, but cannot start a "
                       "session from that runner (for a Codex runner the node's settings need [dispatch.sessions] "
                       "codex = true, and a runner its session host refuses, say one with --approve-for-me, is "
                       "never hosted)")


def _offers(conn, node: str, agent: str, runtime: str | None) -> list[str]:
    """The access words the node's latest hello says a child session of this agent's runner can be started at."""
    info = _get(conn, nodes.NODE_PREFIX + node)
    hosting = info.get("session_host") if isinstance(info, dict) else None
    offered = hosting.get("runners") if isinstance(hosting, dict) else None
    words = offered.get(nodes.runner_key(info, agent, runtime)) if isinstance(offered, dict) else None
    return [w for w in words if w in session_access.LEVELS] if isinstance(words, list) else []


def create_child(board: Board, by: str, parent_id: Any, text: Any, *, agent: Any = None, model: Any = None,
                 effort: Any = None, title: Any = None, role: Any = None, isolation: Any = None, access: Any = None,
                 request_id: Any = None, allowed: dict[str, dict] | None = None, spawn: Any = None) -> dict:
    """Start a session under `parent_id` with its first message. Returns {"session": its record, "message": its
    first message's record, "created": False when (parent, request_id) had made it already}. `by` is recorded as
    who created it; whether `by` MAY is the caller's to have decided (see the module text). `agent`: default the
    parent's. `allowed`: the hub's [dispatch.nodes]. `spawn`: the hub's [dispatch.sessions.spawn] (default: read
    from the board's settings)."""
    spawn = spawn if spawn is not None else spawn_config(board)
    if not spawn.enabled:
        raise Conflict(OFF)
    text = hosted._text(text)
    try:
        model, effort = validate_model(model), session_access.effort(effort)
        access = session_access.level(access)
    except ValueError as e:
        raise Invalid(str(e)) from None
    role = _word(role, session_access.ROLES, "role", "general")
    isolation = _word(isolation, session_access.ISOLATIONS, "isolation", "shared")
    title, request_id = _title(title), _request_id(request_id)
    parent = hosted._session(board.conn, parent_id)
    done = _existing(board.conn, parent["session_id"], request_id)
    if done is not None:
        return done
    agent, runtime = hosted._agent(board, agent if agent is not None else parent.get("agent"))
    _target(board, parent, agent, runtime, allowed)
    now = board.now()
    with db.write_tx(board.conn) as c:
        parent = hosted._session(c, parent_id)
        pid = parent["session_id"]
        done = _existing(c, pid, request_id)
        if done is not None:
            return done
        if parent["state"] == "ended":
            raise Conflict(f"session {pid} has ended: nothing can be started under it")
        if parent.get("stop"):
            raise Conflict(f"session {pid} is being closed: nothing can be started under it")
        if is_child(parent):
            raise Conflict(f"session {pid} is itself a child session: a child cannot have children (the tree is one "
                           f"level deep). Start it under its parent, {parent['parent']}")
        limit = parent.get("access")
        if limit not in session_access.LEVELS:
            raise Conflict(f"the access level of session {pid} is not known yet: its node reports it once it has "
                           "opened the session (a node from before child sessions never does). Try again in a moment")
        access = access or limit
        if not session_access.within(access, limit):
            raise Conflict(f"access {access} is above the parent's ({limit}): a child session never gets more access "
                           "than its parent")
        offered = _offers(c, parent["node"], agent, runtime)
        if access not in offered:
            # What the node itself says it can start from that runner: `full` only where it allows full access, not
            # `workspace` from a Claude runner that bypasses, and so on. Refused here, not made and then ended.
            raise Conflict(f"node {parent['node']} cannot start a session of {agent} at {access} access (it can at: "
                           f"{', '.join(offered) or 'none'}). `full` needs [dispatch.sessions] allow_full_access = "
                           "true on the node")
        live = children(c, pid, live=True)
        if len(live) >= spawn.max_children:
            raise Conflict(f"session {pid} already has {len(live)} child sessions that have not ended (the hub's "
                           f"[dispatch.sessions.spawn] max_children is {spawn.max_children}): close one first")
        counts = dict(parent.get("child_counts") or {})
        counts[role] = int(counts.get(role) or 0) + 1
        r = {"session_id": str(uuid.uuid4()), "agent": agent, "runtime": runtime, "project": parent["project"],
             "node": parent["node"], "model": model, "state": "idle", "pid": None, "resume": False, "turns": 0,
             "queued": 0, "in_flight": None, "next_n": 1, "last_result": None, "reply": None, "reply_for": None,
             "denials": [], "stop": None, "error": None, "created_by": by, "created_at": now, "updated_at": now,
             "node_reported_at": None, "ended_at": None,
             "parent": pid, "root": pid, "depth": 1, "child_no": sum(counts.values()),
             "name": f"{role}-{counts[role]}", "title": title, "role": role,
             "isolation": isolation, "access": access, "effort": effort, "request_id": request_id}
        m = hosted._enqueue(c, r, "user", text, by, now)
        parent["child_counts"] = counts         # a name is unique under the parent, also once a child's record is gone
        _put(c, hosted.SESSION_PREFIX + pid, parent, by, now)
    return {"session": r, "message": m, "created": True}


def start_child(board: Board, p: Principal, parent_id: Any, text: Any, **kw) -> dict:
    """The human starts a session under one of theirs (`board session start --parent`)."""
    hosted._human(board, p, "start a child session")
    out = create_child(board, p.name, parent_id, text, **kw)
    return {"session": hosted._human_view(board, out["session"]),
            "message": hosted._msg_view(out["message"]) if out["message"] is not None else None,
            "created": out["created"]}


def cascade(c, parent: dict, by: str, now: float) -> None:
    """Inside the transaction in which `parent` ended or was marked for closing: its children that have not ended
    are closed with it. A child whose node is connected gets the stop flag, which the node carries out and confirms
    (as for a session the human closes); one whose node is away ends here, and the node closes it when it is back."""
    if is_child(parent):
        return
    for child in children(c, parent["session_id"], live=True):
        if (now - hosted._node_seen(c, child["node"]) > nodes.NODE_STALE_SECONDS
                or not node_supports(c, child["node"])):        # nobody there will carry a stop flag out
            hosted._end(c, child, PARENT_ENDED + " (its node was not connected)", by, now)
        elif not child.get("stop"):
            child |= {"stop": "parent", "updated_at": now}
        else:
            continue
        _put(c, hosted.SESSION_PREFIX + child["session_id"], child, by, now)


def children_view(board: Board, r: dict) -> list[dict]:
    """A session's children as `show` lists them: each child's own view without its reply."""
    out = []
    for child in children(board.conn, r["session_id"]):
        v = hosted._human_view(board, child)
        v.pop("reply", None), v.pop("reply_for", None)
        out.append(v)
    return out


def reported(r: dict, access: Any, worktree: Any) -> None:
    """A node's report on one of its sessions, the tree's part: the access the session really has there, and where
    a child's own worktree is. A root's `access` is whatever its node says its runner has. A child's `access` stays
    the word that was asked; what the node granted is kept beside it (`granted`) and is never above it."""
    if access is not None:
        if not is_child(r):
            r["access"] = access
        elif r.get("access") in session_access.LEVELS and session_access.within(access, r["access"]):
            r["granted"] = access
    if worktree is not None and is_child(r) and r.get("isolation") == "worktree":
        r["worktree"] = worktree
