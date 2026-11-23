"""Parent and child hosted sessions, hub side (DESIGN_NOTES "Session host: parent and child sessions").

A hosted session (hosted.py) can have CHILDREN: sessions started under it, possibly for another agent and model, with
their own isolation and access level. Everything here is off unless the hub says `[dispatch.sessions.spawn] enabled =
true`; with it off no child can be created and nothing else changes.

- *Records.* Still `board_state`, the same `host.session.<uuid>` records. A child's record also holds `parent` (its
  direct parent), `root` (its parent's root: the session at the top of the tree), `depth` (its parent's plus one),
  `name` (`<role>-<n>`, unique under its parent), `title`, `role`, `isolation` (`shared`: the project's checkout on
  the node, the one its root runs in, at every level; `worktree`: a git worktree of its own on the node),
  `access` (the word asked of its node), `effort` and `request_id`. A root's `access` is what its node reported
  for it (derived from the node's runner). A record without `parent` is a root, whenever it was made.
- *Who.* `create_child` is the one function that makes a child, and it checks no caller: the human's route
  (`start_child`) does, and the agent-callable spawn tool does after its own checks (hosted_spawn.py: who is
  calling, the human's grant, the tree's limits).
- *Rules.* The parent exists, has not ended and is not being closed, and the child's depth (its parent's plus one)
  is at most [dispatch.sessions.spawn] max_depth (1 to 4; 2 as shipped: a root, its children and theirs). The
  parent may itself be a child (a deeper tree, DESIGN_NOTES "Deeper trees"); the rules are the same at every
  level. The child runs on
  the parent's node, in the parent's project, for an agent that node offers a hostable runner for and the hub's
  `[dispatch.nodes.<node>]` lists (the check a root gets). Its access defaults to the parent's and may be narrower;
  above the parent's is refused, and so is `full` until the parent's process has confirmed full access (`_limit`).
  A runner that names its own directory is held against the directory a shared child runs in (`_fits`). At most
  `max_children` children of one PARENT that have not ended (each parent's own). The same (parent, request_id)
  returns the child it made the first time.
- *Accounts.* An agent the hub's `[dispatch.agent_projects]` lists is started as a child only in a project listed
  for it (agent_accounts.py): in `_target`, so neither capabilities nor the spawn frame offer it elsewhere, and
  again inside `create_child`'s transaction. A child asked for by an account pool's name (account_pools.py) is
  that pool's member chosen in the transaction that makes it (`start_child`; the spawn tool's own), and keeps `pool`.
- *Not for children.* "One session per (agent, project)" (roots only), board wakes (hosted_wake.py: a child has no
  watcher record) and a project's busy directory when the child has its own worktree.
- *Ending.* A parent that ends or is closed takes its whole subtree with it, every level marked in that one
  transaction (`cascade`); closing a child leaves its parent alone.
"""
from __future__ import annotations

import contextlib
import os
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
ORPHANED_HOLD = ("its node now runs a version of agent-comms that does not keep a full-access session's parent up "
                 "while the sessions under it work (its latest hello says session protocol 2 or earlier), so it may "
                 "not run a session under a full-access child: the hub ended it. A turn that was in flight there was "
                 "lost and is not replayed. Update the node and start `board node` again")
ORPHANED = ("its node no longer hosts child sessions (its latest hello is from a version of agent-comms before them, "
            "or one it was rolled back to), so it can neither run nor end this one: the hub ended it. A turn that "
            "was in flight there was lost and is not replayed")
# What a child's record holds beyond a root's, as the human sees it.
CHILD_FIELDS = ("name", "child_no", "title", "role", "isolation", "effort", "request_id", "granted", "worktree",
                "worktree_removed_at")
# ... and for a child an AGENT started (hosted_spawn.py), shown only when there: who started it, what it may start
# itself, and, for a child of an interactive session, that its parent is an interactive root.
SPAWN_FIELDS = ("spawned_by", "allowance", "parent_kind")


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
           "depth": int(r.get("depth") or 1) if is_child(r) else 0, "access": r.get("access")}
    if is_child(r):
        out |= {k: r.get(k) for k in CHILD_FIELDS} | {k: r[k] for k in SPAWN_FIELDS if r.get(k) is not None}
    elif r.get("access") in session_access.LEVELS:
        out["ceiling"] = _limit(r)      # what a child may have under it now
    return out


def _limit(parent: dict) -> str:
    """The most a child may have under this session NOW. A root: its access, except that `full` counts only while its
    node says its live process confirmed it (the node's `ceiling`); a full root whose node says nothing of the kind
    gives `workspace`. A session that is itself a child (a deeper tree, hosted_spawn.py): its own access, what its
    node gave when that is narrower, and `full` as `workspace` (nodes report a confirmation for roots only)."""
    if is_child(parent):
        words = [w for w in (parent.get("access"), parent.get("granted")) if w in session_access.LEVELS]
        if not words:
            return parent.get("access")
        word = min(words, key=lambda w: session_access.RANK[w])
        return "workspace" if word == "full" else word
    access, ceiling = parent.get("access"), parent.get("ceiling")
    if ceiling in session_access.LEVELS and session_access.within(ceiling, access):
        return ceiling
    return "workspace" if access == "full" else access


def node_extra(r: dict, enabled: bool, bind: str | None = None) -> dict:
    """What the node is told about a session beyond agent, project and model. Words only: the node turns `access`
    into flags from its own runner and settings. A root carries `access: None` when child sessions are on, which
    asks its node to report the access its runner has; with them off a root's entry is what it always was.
    A child of an INTERACTIVE session (hosted_spawn.py) has no parent on any node: it is listed with `parent: None`
    and `interactive: true`, and its access word is the hub's (the human's grant, held to the node's own offers).
    `bind` (for a node that speaks the spawn protocol, child sessions on): the key that proves a process is this
    session's (hosted_spawn.bind_key), which the node puts into the process's environment."""
    if is_child(r):
        out = {"parent": None if interactive(r) else r["parent"], "access": r.get("access"),
               "isolation": r.get("isolation") or "shared", "effort": r.get("effort")}
        out |= {"interactive": True} if interactive(r) else {}
    else:
        out = {"access": None} if enabled else {}
    return out | ({"bind": bind} if bind and enabled else {})


def interactive(r: Any) -> bool:
    """A child whose parent is an interactive root (hosted_spawn.py), not a hosted session."""
    return is_child(r) and r.get("parent_kind") == "interactive"


SPAWN_PROTOCOL = 2      # a node's `tree` version that knows bindings, interactive children and cancel
HOLD_PROTOCOL = 3       # ... that also keeps a full-access child's parent up while results wait for that child
RESTART_PROTOCOL = 4    # ... that also restarts a turn in flight (hosted.py, "Restarting a running turn")


def node_protocol(conn, node: str) -> int:
    """The session protocol (`tree`) the node's latest hello says it speaks; 0 for none."""
    info = _get(conn, nodes.NODE_PREFIX + node)
    hosting = info.get("session_host") if isinstance(info, dict) else None
    tree = hosting.get("tree") if isinstance(hosting, dict) else None
    return tree if type(tree) is int else 0


def node_holds(conn, node: str) -> bool:
    """The node's latest hello says it keeps a full-access session's parent up while the hub's listing says results
    wait for that session (session_host.tree 3 or later: node_sessions `results_waiting`). Only such a node gets a
    session under a full-access child: an earlier one would stop the parent while that session works, and the
    session's result would then wait as the full-access child's turn for the parent's next turn."""
    info = _get(conn, nodes.NODE_PREFIX + node)
    hosting = info.get("session_host") if isinstance(info, dict) else None
    return isinstance(hosting, dict) and type(hosting.get("tree")) is int and hosting["tree"] >= HOLD_PROTOCOL


def node_spawns(conn, node: str) -> bool:
    """The node's latest hello says it speaks the spawn protocol (session_host.tree 2 or later): it gives each
    session's process its binding, opens a child of an interactive session, and stops a turn its starter cancels.
    Only such a node gets a child an agent started."""
    info = _get(conn, nodes.NODE_PREFIX + node)
    hosting = info.get("session_host") if isinstance(info, dict) else None
    return isinstance(hosting, dict) and type(hosting.get("tree")) is int and hosting["tree"] >= SPAWN_PROTOCOL


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


def _existing(conn, parent_id: str, request_id: str | None, starter: dict | None = None) -> dict | None:
    """The child this (parent, request_id) already made, with its first message if that is still on record. Only
    one the same starter made: the human's request ids answer the human (children no agent started), an agent's
    answer that agent session (`starter`, its spawned_by), so neither gets the other's child back."""
    if request_id is None:
        return None
    for r in hosted._sessions(conn):
        by = r.get("spawned_by") if isinstance(r.get("spawned_by"), dict) else None
        mine = by is None if starter is None else (by is not None and by.get("session") == starter.get("session")
                                                    and by.get("agent") == starter.get("agent"))
        if r.get("parent") == parent_id and r.get("request_id") == request_id and mine:
            first = _get(conn, hosted._msg_key(r["session_id"], 1))
            return {"session": r, "message": first if isinstance(first, dict) else None, "created": False}
    return None


def node_supports(conn, node: str) -> bool:
    """The node's latest hello says it speaks the child-session protocol. One that does not (an earlier version,
    or one rolled back to it) is listed no child and can be given none."""
    info = _get(conn, nodes.NODE_PREFIX + node)
    hosting = info.get("session_host") if isinstance(info, dict) else None
    return isinstance(hosting, dict) and type(hosting.get("tree")) is int and hosting["tree"] >= 1


def orphan_reason(conn, r: Any) -> str | None:
    """Why a child that has not ended can no longer run on its node, as that node's latest hello says it, or None.
    Its node hosts no child sessions (an earlier version, or one rolled back to): such a node is listed no child
    (hosted._listed), so it will never report on this one or end it. Or the child is interactive (hosted_spawn.py)
    and its node does not speak the spawn protocol. Or it is under a full-access child and its node does not keep
    that child's parent up for it (`node_holds`: below session protocol 3), the rule a start is held to
    (`_target`). The hub settles such a child itself (`end_orphans` at that hello, hosted._settle on a read), and
    until it has, the child counts as lost (hosted._lost): nothing of it keeps a project busy."""
    if not is_child(r) or r.get("state") == "ended":
        return None
    if not node_supports(conn, r["node"]) or (interactive(r) and not node_spawns(conn, r["node"])):
        return ORPHANED
    if under_full(conn, r) and not node_holds(conn, r["node"]):
        return ORPHANED_HOLD
    return None


def orphaned(conn, r: Any) -> bool:
    return orphan_reason(conn, r) is not None


def under_full(conn, r: dict) -> bool:
    """A session above `r` (not `r` itself) is a full-access child."""
    cur, hops = r, 0
    while is_child(cur) and not interactive(cur) and hops < 10:
        hops += 1
        cur = _get(conn, hosted.SESSION_PREFIX + cur["parent"])
        if is_child(cur) and cur.get("access") == "full":
            return True
    return False


def under(sessions: list[dict], top_id: str) -> list[dict]:
    """The sessions below `top_id` (its children, theirs, and so on), whatever their state, from `sessions`."""
    kids: dict[str, list[dict]] = {}
    for r in sessions:
        if is_child(r):
            kids.setdefault(r["parent"], []).append(r)
    out, level = [], [top_id]
    while level:
        nxt = [x for pid in level for x in kids.get(pid, [])]
        out += nxt
        level = [x["session_id"] for x in nxt]
    return out


def subtree_open(board: Board, conn, k: dict, sessions: list[dict] | None = None) -> dict[str, list[str]]:
    """What keeps full child `k`'s subtree open, as the hub records it (empty lists: closed). `live`: a session under
    it not ended. `results`: a result for it or for a session under it not settled (pending, or in a turn whose
    start its provider has not confirmed: `hosted_results.outstanding`; settled: delivered once that turn started,
    claimed, or kept undelivered). `queued`: a message for it or for a session under it that is queued, or written
    and not settled yet (in flight at the hub, whether or not its start was confirmed).
    While any holds, a next turn of `k` may come, and `k` takes turns only under its parent's live process: the
    node spares that process (node_sessions `_lends_full`). The hub ends a session and makes the result of its end
    (and of the turn it cut) in one transaction, and a turn's result in the transaction that takes its report, so
    a session under `k` counts as `live` until what it produced counts as a result."""
    from . import hosted_results
    below = under(sessions if sessions is not None else hosted._sessions(conn), k["session_id"])
    everyone = [k] + below
    return {"live": [x["session_id"] for x in below if x.get("state") != "ended"],
            "results": [x["session_id"] for x in everyone if hosted_results.outstanding(conn, x["session_id"])],
            "queued": [x["session_id"] for x in everyone if x.get("state") != "ended"
                       and (int(x.get("queued") or 0) or x.get("in_flight") is not None)]}


def end_orphans(c, node: str, by: str, now: float, results: bool = False) -> None:
    """Inside the transaction of every hello from `node`: its children that can no longer run there
    (`orphan_reason`: the node does not declare the child protocol, or no longer the level a child needs) are ended
    here, each unsettled message failed with the reason. `results`: each end is a result for the child's parent
    (hosted_results.py)."""
    # Ancestors first, each record read again before it is decided: a parent's end has already ended (cascade) the
    # sessions under it, and a stale copy of one must not end it a second time.
    for sid in [r["session_id"] for r in sorted(hosted._sessions(c), key=lambda r: int(r.get("depth") or 0))]:
        r = _get(c, hosted.SESSION_PREFIX + sid)
        why = orphan_reason(c, r) if isinstance(r, dict) and r.get("node") == node else None
        if why is not None:
            hosted._end(c, r, why, by, now, results=results)
            _put(c, hosted.SESSION_PREFIX + r["session_id"], r, by, now)


def _target(board: Board, parent: dict, agent: str, runtime: str, allowed: dict[str, dict] | None,
            at: float | None = None) -> None:
    """The child's agent on the PARENT'S node: the hub's grant for that node must cover the project and the agent
    (as for a root), and the node must be connected as a session host with a runner for the agent, speak the
    child-session protocol NOW (its latest hello), and be able to start a child session from that runner at all
    (its hello says at which access words: none for a Codex runner where Codex sessions are off, or one its
    session preparation refuses). The word itself is checked when the child is admitted (`_offers`)."""
    node, project = parent["node"], parent["project"]
    from . import agent_accounts
    agent_accounts.check(board, agent, project)     # the hub's opt-in list (agent_accounts.py)
    grant = (allowed or {}).get(node)
    if (not isinstance(grant, dict) or not nodes.grant_covers(grant, project)
            or (agent not in grant.get("agents", []) and runtime not in grant.get("agents", []))):
        raise Conflict(f"node {node} may not run {agent} in {project}: a child session runs on its parent's node, "
                       f"and the hub's [dispatch.nodes.{node}] must list the project and the agent or its runtime")
    if nodes.node_for(board, project, agent, runtime, {node: grant}, room=False, sessions=True, at=at) != node:
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
    _holds_for(board.conn, parent)


def _holds_for(conn, parent: dict) -> None:
    """A session under a full-access child only on a node that keeps that child's parent up (`node_holds`). Asked
    before the start (`_target`) and again inside its write transaction (`create_child`), so a hello that lands in
    between, from a node rolled back, is seen."""
    node = parent["node"]
    if is_child(parent) and parent.get("access") == "full" and not node_holds(conn, node):
        raise Conflict(f"node {node} runs a version of agent-comms that does not keep a full-access session's parent "
                       "up while that session's children work, so nothing can be started under a full-access child "
                       f"there (session {parent.get('session_id')}). Update the node and start `board node` again")


def _max_live(conn, node: str) -> int:
    """How many session processes the node says it runs at once (its hello's `sessions.max_live`)."""
    info = _get(conn, nodes.NODE_PREFIX + node)
    sessions = info.get("sessions") if isinstance(info, dict) else None
    live = sessions.get("max_live") if isinstance(sessions, dict) else None
    return live if type(live) is int else 0


def place_interactive(board: Board, project: str, agent: str, runtime: str | None,
                      allowed: dict[str, dict] | None, at: float | None = None) -> str | None:
    """The node a child of an interactive session runs on: a live one the hub allows the project and the agent,
    that hosts sessions, has a runner for the agent it can start a child from, and speaks the spawn protocol. The
    most recently seen first. `at`: the moment liveness is judged at (nodes.node_for)."""
    allowed = allowed or {}
    for info in sorted(nodes.nodes(board), key=lambda i: -float(i.get("last_seen") or 0)):
        name = info["node"]
        grant = allowed.get(name)
        if (isinstance(grant, dict) and nodes.node_for(board, project, agent, runtime, {name: grant}, room=False,
                                                       sessions=True, at=at) == name
                and node_spawns(board.conn, name) and _offers(board.conn, name, agent, runtime)):
            return name
    return None


def _hosted_root(conn, r: dict) -> dict | None:
    """The hosted root of session `r`'s tree (`r` itself for a root), or None: its tree is an interactive root's
    (hosted_spawn.py), or a record on the way is gone."""
    cur, hops = r, 0
    while is_child(cur) and hops < 10:
        hops += 1
        cur = _get(conn, hosted.SESSION_PREFIX + cur["parent"]) if not interactive(cur) else None
    return cur if isinstance(cur, dict) and not is_child(cur) and cur.get("kind") != "interactive" else None


def shared_checkout(conn, r: dict) -> str | None:
    """Where a `shared` child of session `r` runs, as the nodes reported it: the checkout its ROOT runs in. `shared`
    is the project's checkout on the node at every level (the node opens a shared child there, not in a worktree its
    parent may have of its own), and only a root's node reports where it runs. None: not known (yet)."""
    top = _hosted_root(conn, r)
    return top.get("checkout") if top is not None and isinstance(top.get("checkout"), str) else None


def _fits(conn, parent: dict, agent: str, runtime: str | None, isolation: str) -> None:
    """A runner that names its own directory (the node's hello: `dirs`) can be a session in that directory only. A
    child of it must therefore share the parent's checkout, and that checkout (as the node reported it) must be the
    directory. Conflict otherwise: refused here, not made and then ended on the node."""
    info = _get(conn, nodes.NODE_PREFIX + parent["node"])
    hosting = info.get("session_host") if isinstance(info, dict) else None
    dirs = hosting.get("dirs") if isinstance(hosting, dict) else None
    only = dirs.get(nodes.runner_key(info, agent, runtime)) if isinstance(dirs, dict) else None
    if not isinstance(only, str):
        return
    where = (f"node {parent['node']}'s runner for {agent} names its own directory ({only}) and can be a session "
             "there only")
    if parent.get("kind") == "interactive" or (is_child(parent) and _hosted_root(conn, parent) is None):
        raise Conflict(f"{where}: it cannot be started in an interactive session's tree, whose checkout there is "
                       "not known")
    if isolation != "shared":
        raise Conflict(f"{where}: a child of it cannot have a worktree of its own")
    here = shared_checkout(conn, parent)
    if not isinstance(here, str):
        raise Conflict(f"{where}, and where session {parent['session_id']} runs on its node is not known yet (its "
                       "node reports it once it has opened the session). Try again in a moment")
    if os.path.normpath(here) != os.path.normpath(only):
        raise Conflict(f"{where}; session {parent['session_id']} runs in {here}")


def placement_problem(board: Board, conn, parent: dict, agent: str, runtime: str | None,
                      allowed: dict[str, dict] | None, isolation: str = "shared") -> str | None:
    """Why a child of `agent` could not be placed under `parent` (on the parent's node), or None: the same two checks
    `create_child` makes, `_target` (the node may run it, hosts sessions with a runner for it, speaks the protocol,
    offers a word for it) and `_fits` (a runner that names its own directory, held against the checkout a shared
    child runs in). For a caller that lists what could be started (hosted_spawn: capabilities and the frame), so that
    nothing is listed that the start would refuse. `parent` needs `node` and `project`, and `parent` (its own
    parent's id) or `kind` to say where it stands in its tree."""
    try:
        _target(board, parent, agent, runtime, allowed)
        _fits(conn, parent, agent, runtime, isolation)
    except Conflict as e:
        return e.message
    return None


# ---------------------------------------------------------------- one child's own checks (create_child's, in its order)
#
# `create_child` makes these checks for the agent it starts, and `member_problem` makes the very same ones for each
# member of an account pool (account_pools.choose): one definition of "this agent can be started here, at this
# access", so a member the choice passes is one the creation accepts, and one it would refuse is passed over with
# the reason the creation would give.


def _place_child(board: Board, parent: dict, agent: str, runtime: str | None, allowed: dict[str, dict] | None,
                 interactive: bool, spawned: bool, at: float | None = None, node: str | None = None) -> dict:
    """Before the write: the opt-in list (its reason beats placement's), the node (an interactive root's child is
    placed on one of its own: `node` when the caller placed it already, account_pools' choice), `_target`, and the
    spawn protocol for an agent-started child. Returns the parent with that node. `at`: the one moment every node's
    liveness is judged at (default: now)."""
    from . import agent_accounts
    agent_accounts.check(board, agent, parent["project"])   # the hub's opt-in list, before placement's reasons
    if interactive:
        node = node or place_interactive(board, parent["project"], agent, runtime, allowed, at)
        if node is None:
            raise Conflict(f"no connected node hosts child sessions for {agent} in {parent['project']}: it needs "
                           "`board node` running (this version or later), [dispatch.sessions] enabled = true, a "
                           "checkout of the project and a runner for the agent, and the hub's [dispatch.nodes.<name>] "
                           "must list the project and the agent")
        parent = parent | {"node": node}
    _target(board, parent, agent, runtime, allowed, at)
    if spawned and not node_spawns(board.conn, parent["node"]):
        raise Conflict(f"node {parent['node']} runs a version of agent-comms from before agent-started child sessions "
                       "(it would not give the child its binding, or stop a turn its starter cancels). Update it and "
                       "start `board node` again")
    return parent


def _recheck(board: Board, c, parent: dict, agent: str, spawned: bool) -> None:
    """In the write: the opt-in list again, the node's protocol on its latest hello as stored now (one rolled back
    between the check before the transaction and here would otherwise be given the child: the hello's orphan sweep
    ran before it), and the hold a session under a full-access child needs."""
    from . import agent_accounts
    agent_accounts.check(board, agent, parent["project"])   # ... and in the write that makes the child
    if not node_supports(c, parent["node"]) or (spawned and not node_spawns(c, parent["node"])):
        raise Conflict(f"node {parent['node']} no longer speaks the child-session protocol this session needs (its "
                       "latest hello came while the session was being started). Update it and start `board node` "
                       "again")
    _holds_for(c, parent)


def _admit(c, parent: dict, agent: str, runtime: str | None, access: str | None, isolation: str,
           ceiling: str | None, interactive: bool) -> str:
    """In the write: the child's EFFECTIVE access (the word asked, else the most its parent and `ceiling` allow) and
    whether its node admits it there: within the parent's, within what the parent's process confirmed, a full child
    on a node with room for its parent beside it, a runner held to its own directory (`_fits`), and the word on offer
    for that runner. Returns the access."""
    pid = parent.get("session_id")
    limit = ceiling if interactive else parent.get("access")
    if limit not in session_access.LEVELS:
        raise Conflict(f"the access level of session {pid} is not known yet: its node reports it once it has "
                       "opened the session (a node from before child sessions never does). Try again in a moment")
    if ceiling in session_access.LEVELS:
        limit = min(limit, ceiling, key=lambda w: session_access.RANK[w])
    access = access or limit
    if not session_access.within(access, limit):
        raise Conflict(f"access {access} is above the parent's ({limit}): a child session never gets more access "
                       "than its parent")
    if not interactive and not session_access.within(access, _limit(parent)):
        # The parent was STARTED as full; whether its CLI took that is known once its process says so.
        raise Conflict(f"session {pid} was started as a full-access session, but its process has not confirmed "
                       f"that (it has no process now, or one that has not said so yet), so a child can have at "
                       f"most {_limit(parent)} under it now. Its node confirms it when the session's next turn "
                       "starts: send it a message and try again, or give a narrower --access")
    if access == "full" and not interactive and _max_live(c, parent["node"]) < 2:
        # A full child takes turns only while its parent's process is up beside its own.
        raise Conflict(f"node {parent['node']} runs at most {_max_live(c, parent['node'])} session process at a "
                       "time ([dispatch.sessions] max_live there): a full-access child needs its parent's "
                       "process up beside its own, so max_live must be at least 2. Raise it, or give a "
                       "narrower --access")
    _fits(c, parent, agent, runtime, isolation)
    offered = _offers(c, parent["node"], agent, runtime)
    if access not in offered:
        # What the node itself says it can start from that runner: `full` only where it allows full access, not
        # `workspace` from a Claude runner that bypasses, and so on. Refused here, not made and then ended.
        raise Conflict(f"node {parent['node']} cannot start a session of {agent} at {access} access (it can at: "
                       f"{', '.join(offered) or 'none'}). `full` needs [dispatch.sessions] allow_full_access = "
                       "true on the node")
    return access


def member_problem(board: Board, conn, parent: dict, agent: str, runtime: str | None, *, access: str | None,
                   isolation: str, allowed: dict[str, dict] | None, ceiling: str | None = None,
                   interactive: bool = False, spawned: bool = False,
                   at: float | None = None) -> tuple[str | None, str | None, str | None]:
    """(the node, the effective access, None) when `create_child` would accept a child of `agent` under `parent` as
    asked (`access` and `isolation` already valid words; `access` None: its default), else (None, None, why not):
    exactly its own checks for the agent (`_place_child`, `_recheck`, `_admit`), in its order, on `conn`, every node
    judged live at `at`. A caller that creates the child after it passes that node, access and `at` to
    `create_child`, so the creation judges what the choice judged."""
    try:
        placed = _place_child(board, parent, agent, runtime, allowed, interactive, spawned, at)
        _recheck(board, conn, placed, agent, spawned)
        effective = _admit(conn, placed, agent, runtime, access, isolation, ceiling, interactive)
    except Conflict as e:
        return None, None, e.message
    return placed["node"], effective, None


def _offers(conn, node: str, agent: str, runtime: str | None) -> list[str]:
    """The access words the node's latest hello says a child session of this agent's runner can be started at."""
    info = _get(conn, nodes.NODE_PREFIX + node)
    hosting = info.get("session_host") if isinstance(info, dict) else None
    offered = hosting.get("runners") if isinstance(hosting, dict) else None
    words = offered.get(nodes.runner_key(info, agent, runtime)) if isinstance(offered, dict) else None
    return [w for w in words if w in session_access.LEVELS] if isinstance(words, list) else []


def _parent(conn, parent_id: Any, interactive_root: bool) -> dict:
    if interactive_root:
        from . import hosted_spawn
        return hosted_spawn.iroot(conn, parent_id)
    return hosted._session(conn, parent_id)


def create_child(board: Board, by: str, parent_id: Any, text: Any, *, agent: Any = None, model: Any = None,
                 effort: Any = None, title: Any = None, role: Any = None, isolation: Any = None, access: Any = None,
                 request_id: Any = None, allowed: dict[str, dict] | None = None, spawn: Any = None,
                 kind: str = "user", spawned_by: dict | None = None, ceiling: str | None = None,
                 interactive: bool = False, check=None, hook=None, tx=None, allowance: dict | None = None,
                 pool: dict | None = None, at: float | None = None, placed: str | None = None) -> dict:
    """Start a session under `parent_id` with its first message. Returns {"session": its record, "message": its
    first message's record, "created": False when (parent, request_id) had made it already}. `by` is recorded as
    who created it; whether `by` MAY is the caller's to have decided (see the module text). `agent`: default the
    parent's. `allowed`: the hub's [dispatch.nodes]. `spawn`: the hub's [dispatch.sessions.spawn] (default: read
    from the board's settings).
    The agent-callable path (hosted_spawn.py) adds: `kind` of the first message (`task`), `spawned_by` (recorded on
    the child, with the default allowance), `ceiling` (the most the caller and its grant allow), `interactive` (the
    parent is an interactive root, hosted_spawn.iroot: the child is placed on a node of its own and its access is
    held to `ceiling`), and `check(c)` / `hook(c, child, message, now)`, run inside the write transaction (the tree's
    limits, its counters and its audit queue). Depth: a child's is its parent's plus one, at most
    [dispatch.sessions.spawn] max_depth, and its root is its parent's root. `tx`: a write transaction of the
    caller's own (on `board.conn`), in which all of this then runs, reads included (hosted_spawn.spawn decides a
    spawn in one). `pool`: how `agent` was chosen from a pool (account_pools.Choice.record, decided by the caller in
    `tx`), kept on the child's record. `at` and `placed`: the moment and the node (an interactive child's) that
    choice judged the agent by, so that this start judges the same (hosted_tree.member_problem)."""
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
    parent = _parent(board.conn, parent_id, interactive)
    done = _existing(board.conn, parent["session_id"], request_id, spawned_by)
    if done is not None:
        return done
    agent, runtime = hosted._agent(board, agent if agent is not None else parent.get("agent"))
    parent = _place_child(board, parent, agent, runtime, allowed, interactive, spawned_by is not None, at, placed)
    node = parent["node"] if interactive else None
    with (contextlib.nullcontext(tx) if tx is not None else db.write_tx(board.conn)) as c:
        now = board.now()       # read under the write lock
        parent = _parent(c, parent_id, interactive) | ({"node": node} if interactive else {})
        pid = parent["session_id"]
        done = _existing(c, pid, request_id, spawned_by)
        if done is not None:
            return done
        if parent["state"] == "ended":
            raise Conflict(f"session {pid} has ended: nothing can be started under it")
        if parent.get("stop"):
            raise Conflict(f"session {pid} is being closed: nothing can be started under it")
        _recheck(board, c, parent, agent, spawned_by is not None)
        depth = 0 if interactive else int(parent.get("depth") or (1 if is_child(parent) else 0))
        if depth + 1 > spawn.max_depth:
            if spawn.max_depth == 1:
                raise Conflict(f"session {pid} is itself a child session: a child cannot have children (the tree is "
                               f"one level deep). Start it under its parent, {parent['parent']}")
            raise Conflict(f"depth limit: session {pid} is at depth {depth}, and [dispatch.sessions.spawn] "
                           f"max_depth is {spawn.max_depth}")
        access = _admit(c, parent, agent, runtime, access, isolation, ceiling, interactive)
        if check is not None:
            # The caller's checks (hosted_spawn: the grant and its limits, whose refusal raises the budget's Needs-you
            # item) before the per-parent cap below, so that a cap reached at the same time never hides a limit.
            check(c)
        # A child the hub ended while its node was away still holds its place until the node confirms it is gone
        # (hosted.holds_slot): its turn may still be running there.
        live = [x for x in children(c, pid) if hosted.holds_slot(c, x, now)]
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
             "parent": pid, "root": parent.get("root") or pid, "depth": depth + 1, "child_no": sum(counts.values()),
             "name": f"{role}-{counts[role]}", "title": title, "role": role,
             "isolation": isolation, "access": access, "effort": effort, "request_id": request_id}
        if interactive:
            r["parent_kind"] = "interactive"
        if pool is not None:
            r["pool"] = dict(pool)
        if spawned_by is not None:
            # What it may start itself: on, and (`allowance`, the spawn tool's) no share above its starter's own.
            r |= {"spawned_by": dict(spawned_by), "allowance": dict(allowance or {"may_spawn": True, "share": None})}
        m = hosted._enqueue(c, r, kind, text, by, now)
        parent["child_counts"] = counts         # a name is unique under the parent, also once a child's record is gone
        if interactive:
            from . import hosted_spawn
            parent.pop("node", None)
            hosted_spawn.put_iroot(c, parent, by, now)
        else:
            _put(c, hosted.SESSION_PREFIX + pid, parent, by, now)
        if hook is not None:
            hook(c, r, m, now)
    return {"session": r, "message": m, "created": True}


def start_child(board: Board, p: Principal, parent_id: Any, text: Any, **kw) -> dict:
    """The human starts a session under one of theirs (`board session start --parent`)."""
    hosted._human(board, p, "start a child session")
    from . import account_pools
    if not account_pools.maybe_pool(board, kw.get("agent")):
        out = create_child(board, p.name, parent_id, text, **kw)    # an agent (or none): main's path, untouched
    else:
        # Perhaps a pool's name (account_pools.py): recognised and decided in the write that makes the child, on ONE
        # reading of the settings taken there, and create_child runs in that write too. Each member is held to
        # create_child's own checks (`member_problem`), at the access it would really get.
        with db.write_tx(board.conn) as c:
            spawn = spawn_config(board)
            kw = kw | {"tx": c, "spawn": spawn}
            pool = account_pools.pool_for(board, kw.get("agent"), spawn) if spawn.enabled else None
            parent = hosted._session(c, parent_id) if pool is not None else None
            done = _existing(c, parent["session_id"], _request_id(kw.get("request_id"))) if parent else None
            if pool is not None and done is None:
                try:
                    access = session_access.level(kw.get("access"))
                except ValueError as e:
                    raise Invalid(str(e)) from None
                isolation = _word(kw.get("isolation"), session_access.ISOLATIONS, "isolation", "shared")

                at = board.now()    # the one moment the choice and the start judge the node's liveness by

                def eligible(a: str, rt: str) -> tuple[str | None, str | None, str | None]:
                    node, effective, why = member_problem(board, c, parent, a, rt, access=access,
                                                          isolation=isolation, allowed=kw.get("allowed"), at=at)
                    return node, why, effective
                choice = account_pools.choose(board, c, pool, parent["project"], eligible, spawn)
                kw = kw | {"agent": choice.agent, "pool": choice.record(), "at": at}
            out = done or create_child(board, p.name, parent_id, text, **kw)
    return {"session": hosted._human_view(board, out["session"]),
            "message": hosted._msg_view(out["message"]) if out["message"] is not None else None,
            "created": out["created"]}


def cascade(c, parent: dict, by: str, now: float) -> None:
    """Inside the transaction in which `parent` ended or was marked for closing: its children that have not ended
    are closed with it. A child whose node is connected gets the stop flag, which the node carries out and confirms
    (as for a session the human closes); one whose node is away ends here, and the node closes it when it is back.
    Any session's children: one that is itself a child (a deeper tree) takes its own with it when it ends, since
    hosted._end calls this for every session; an interactive root's too (hosted_spawn.close_iroot).
    Deeper trees: the WHOLE subtree is marked in this one transaction, each level under the one above it, not one
    level per confirmation from the node. So no descendant keeps running (or starts anything: a session being
    closed can have nothing started under it) while the level above it waits for its node, and each one's result
    and end tell nobody (`stop: "parent"`, hosted_results._cascaded). An end here (`hosted._end`) cascades by
    itself; a child marked here takes its own along below."""
    for child in children(c, parent["session_id"], live=True):
        if (now - hosted._node_seen(c, child["node"]) > nodes.NODE_STALE_SECONDS
                or not node_supports(c, child["node"])):        # nobody there will carry a stop flag out
            hosted._end(c, child, PARENT_ENDED + " (its node was not connected)", by, now)     # (cascades itself)
            _put(c, hosted.SESSION_PREFIX + child["session_id"], child, by, now)
            continue
        if not child.get("stop"):
            child |= {"stop": "parent", "updated_at": now}
            _put(c, hosted.SESSION_PREFIX + child["session_id"], child, by, now)
        cascade(c, child, by, now)      # its own children too, now (already marked ones are left as they are)


def children_view(board: Board, r: dict) -> list[dict]:
    """A session's children as `show` lists them: each child's own view without its reply."""
    out = []
    for child in children(board.conn, r["session_id"]):
        v = hosted._human_view(board, child)
        v.pop("reply", None), v.pop("reply_for", None)
        out.append(v)
    return out


def reported(r: dict, access: Any, worktree: Any, ceiling: Any = None, checkout: Any = None) -> None:
    """A node's report on one of its sessions, the tree's part: the access the session really has there, and where
    a child's own worktree is. A root's `access` is whatever its node says its runner has. A child's `access` stays
    the word that was asked; what the node granted is kept beside it (`granted`) and is never above it. A root's
    `ceiling` (the most a child may have under it now) and `checkout` (where it runs) are the node's to say, with
    every status: a status that names no ceiling takes the last one back."""
    if not is_child(r):
        if ceiling is not None or "ceiling" in r:       # (nothing is added to a root whose node says none of it)
            r["ceiling"] = ceiling
        if checkout is not None:
            r["checkout"] = checkout
    if access is not None:
        if not is_child(r):
            r["access"] = access
        elif r.get("access") in session_access.LEVELS and session_access.within(access, r["access"]):
            r["granted"] = access
    if worktree is not None and is_child(r) and r.get("isolation") == "worktree":
        r["worktree"] = worktree
