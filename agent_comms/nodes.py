"""Machine nodes: the dispatcher on the hub launches agents on other machines (DESIGN_NOTES "Nodes").

The hub is the board's one dispatcher. It still decides everything about a launch: which rule covers it, the budget,
one run per agent and per run directory, max_concurrent, timeouts, requeue after a stop. What it cannot do is start a
process on another machine. A *node* (`board node`, node.py) runs on each other machine, signs in with its own token
(an agent identity whose runtime is `node`), and connects out to the hub (directly on the same machine, or through
`board gateway` over Tailscale). It never accepts a connection.

- `hello`: every few seconds the node reports what it can run: the projects it has checked out (the keys of its own
  `[dispatch] worktrees`) and its runner keys (agent names or runtimes, as in `[dispatch] runners`), plus its
  max_concurrent, and the runner keys it can give a scoped headless browser (its own `[dispatch.headless_browser]`,
  with the browser command on PATH and a Codex config that leaves the server name alone). Stored under NODE_PREFIX
  with `last_seen`. A node unseen for NODE_STALE_SECONDS offers nothing.
- The hub decides what each node may take: `[dispatch.nodes.<name>] projects = [...]` and `agents = [...]` in the
  hub's board.local.toml. A node with no entry gets nothing, and a node offering a project or agent outside its
  entry is not given it, whatever it claims.
- A node identity is only a node: core refuses it on every agent route and MCP tool (api.principal,
  mcp_server.principal); it can call nothing but `/api/nodes/*` and `/api/whoami` (which the gateway needs).
- When the hub would launch an agent for a project it has no run directory for, and a live node offers that project
  and a runner for the agent (by name, else by its runtime), the dispatcher records an *assignment* (RUN_PREFIX)
  instead of spawning: run id, agent, thread, project, the prompt the hub built, and the timeouts. The dispatcher
  tracks it through RemoteChild, which has the shape of a local child (poll / terminate / kill).
- The node picks up its assignments (`assignments`), starts the runner from ITS OWN config (template, env, run
  directory: nothing executable comes from the hub, only the prompt text the hub's dispatcher wrote), and reports
  `running` (pid) and `ended` (exit code). A browser-bound run goes only to a node that offers a headless browser
  for the runner it would use; the assignment then carries the bound origins (the hub's binding and deny gate decide
  them) and the node attaches its own browser server with exactly those origins. It enforces the timeout and kill grace itself and honours a stop the hub
  requested (terminate / kill).
- A node that goes silent while its run is unfinished: after NODE_LOST_SECONDS the run counts as ended (exit -1,
  error `node_lost`), so the dispatcher settles or requeues its requests as for any ended run. The node stops its own
  runs well before that (node.SELF_STOP_SECONDS without hub contact), and kills any local run the hub no longer
  lists, so a lost run never keeps going beside its replacement.
- A stop the hub requests for a run the node has not started yet ends it on the hub at once ("stopped before
  start"), and the node refuses to start an assignment that carries a stop.

Any agent CLI works: the node only needs a runner template for it, exactly as the local dispatcher does. The launched
agent reaches the board through its own MCP or HTTP client configuration on that machine (README "Reaching the board
from other machines").
"""
from __future__ import annotations

import json
import re
from typing import Any

from . import db
from .core import Board, Conflict, Forbidden, Invalid, NotFound, Principal

NODE_RUNTIME = "node"
NODE_PREFIX = "node.info."     # <node name>: {projects, runners, headless_browser, max_concurrent, last_seen}
RUN_PREFIX = "node.run."       # <run id>: the assignment and its state
NODE_STALE_SECONDS = 60        # a node unseen this long is offered no new work
NODE_LOST_SECONDS = 180        # ... and its unfinished runs count as ended
MAX_ITEMS = 200
STATES = ("assigned", "running", "ended")
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")


def _get(conn, key: str) -> Any:
    row = conn.execute("SELECT value FROM board_state WHERE key = ?", (key,)).fetchone()
    if row is None:
        return None
    try:
        return json.loads(row["value"])
    except (TypeError, ValueError):
        return None


def _put(conn, key: str, value: Any, by: str, now: float) -> None:
    conn.execute("""INSERT INTO board_state(key, value, updated_by, updated_at) VALUES (?, ?, ?, ?)
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_by = excluded.updated_by,
                    updated_at = excluded.updated_at""", (key, json.dumps(value), by, now))


def _require_node(board: Board, p: Principal) -> None:
    if p.is_human or p.runtime != NODE_RUNTIME:
        raise Forbidden("only a node identity (runtime 'node') may do this")
    board._check_agent_write(p)


def _strings(value: Any, what: str, check=None) -> list[str]:
    if (not isinstance(value, list) or len(value) > MAX_ITEMS
            or any(not isinstance(v, str) or not v or len(v) > 1024 for v in value)):
        raise Invalid(f"{what} must be a list of at most {MAX_ITEMS} non-empty strings")
    if check is not None and any(not check(v) for v in value):
        raise Invalid(f"{what} contains an invalid entry")
    return sorted(set(value))


def hello(board: Board, p: Principal, projects: Any, runners: Any, max_concurrent: Any,
          headless_browser: Any = None, checkout_roots: Any = None) -> dict:
    """A node's heartbeat: what it can run now. Returns the hub's time and the node's view of itself.
    `headless_browser`: the runner keys (a subset of `runners`) the node can give a scoped headless browser.
    `checkout_roots`: the node's own `[dispatch] checkout_roots`; it runs any Git checkout it finds inside them."""
    _require_node(board, p)
    projects = _strings(projects, "projects", lambda v: v.startswith("/"))
    runners = _strings(runners, "runners", lambda v: _NAME.fullmatch(v) is not None)
    headless = _strings([] if headless_browser is None else headless_browser, "headless_browser",
                        lambda v: v in runners)
    roots = _strings([] if checkout_roots is None else checkout_roots, "checkout_roots",
                     lambda v: v.startswith("/") and v != "/")
    if isinstance(max_concurrent, bool) or not isinstance(max_concurrent, int) or not 1 <= max_concurrent <= 50:
        raise Invalid("max_concurrent must be an integer from 1 to 50")
    now = board.now()
    info = {"projects": [x.rstrip("/") or "/" for x in projects], "runners": runners,
            "headless_browser": headless, "checkout_roots": [x.rstrip("/") for x in roots],
            "max_concurrent": max_concurrent, "last_seen": now}
    with db.write_tx(board.conn) as c:
        _put(c, NODE_PREFIX + p.name, info, p.name, now)
        # Ended runs are kept a day for the record, then dropped so polls never scan the whole history.
        c.execute("""DELETE FROM board_state WHERE key >= ? AND key < ? AND updated_at < ?
                     AND json_valid(value) AND json_extract(value, '$.state') = 'ended'""",
                  (RUN_PREFIX, RUN_PREFIX[:-1] + chr(ord(RUN_PREFIX[-1]) + 1), now - 86400))
    return {"node": p.name, "hub_time": now, **info}


def nodes(board: Board) -> list[dict]:
    out = []
    for r in board.conn.execute("SELECT key, value FROM board_state WHERE key >= ? AND key < ?",
                                (NODE_PREFIX, NODE_PREFIX[:-1] + chr(ord(NODE_PREFIX[-1]) + 1))):
        try:
            info = json.loads(r["value"])
        except (TypeError, ValueError):
            continue
        if isinstance(info, dict):
            out.append({"node": r["key"][len(NODE_PREFIX):], **info})
    return out


def _active_runs(conn, node: str) -> int:
    n = 0
    for (value,) in conn.execute("SELECT value FROM board_state WHERE key >= ? AND key < ?",
                                 (RUN_PREFIX, RUN_PREFIX[:-1] + chr(ord(RUN_PREFIX[-1]) + 1))):
        try:
            run = json.loads(value)
        except (TypeError, ValueError):
            continue
        if isinstance(run, dict) and run.get("node") == node and run.get("state") != "ended":
            n += 1
    return n


def under_roots(project: str, roots: list[str]) -> bool:
    """`project` lies strictly inside one of `roots`, by path (the hub cannot see a node's disk; the node checks the
    checkout itself before it starts, DispatchConfig.discovered_checkout)."""
    if ".." in project.split("/") or "//" in project:
        return False
    return any(project.startswith(r.rstrip("/") + "/") for r in roots)


def grant_covers(grant: dict, project: str) -> bool:
    """A hub `[dispatch.nodes.<name>]` entry allows this project: listed, or inside one of its checkout_roots."""
    project = project.rstrip("/") or "/"
    return project in grant.get("projects", []) or under_roots(project, grant.get("checkout_roots", []))


def runner_key(info: dict, agent: str, runtime: str | None) -> str | None:
    """The runner key a node would use for this agent: its name, else its runtime (DispatchConfig.runner_key)."""
    offered = info.get("runners", [])
    return agent if agent in offered else runtime if runtime and runtime in offered else None


def node_for(board: Board, project: str, agent: str, runtime: str | None,
             allowed: dict[str, dict] | None = None, *, headless_browser: bool = False,
             room: bool = True) -> str | None:
    """The live node that offers this project and a runner for this agent (its name, else its runtime), that the hub
    allows to take both (`allowed`: [dispatch.nodes]), and that has room; the most recently seen first. With
    `headless_browser`, only a node that offers a scoped headless browser for that runner. With `room=False`, a full
    node counts too."""
    now = board.now()
    project = project.rstrip("/") or "/"
    keys = {agent} | ({runtime} if runtime else set())
    for info in sorted(nodes(board), key=lambda i: -float(i.get("last_seen") or 0)):
        if now - float(info.get("last_seen") or 0) > NODE_STALE_SECONDS:
            continue
        grant = (allowed or {}).get(info["node"])
        if (not grant or not grant_covers(grant, project)
                or agent not in grant.get("agents", []) and not (runtime and runtime in grant.get("agents", []))):
            continue
        if ((project not in info.get("projects", []) and not under_roots(project, info.get("checkout_roots", [])))
                or not keys & set(info.get("runners", []))):
            continue
        if headless_browser and runner_key(info, agent, runtime) not in info.get("headless_browser", []):
            continue
        if room and _active_runs(board.conn, info["node"]) >= int(info.get("max_concurrent") or 1):
            continue
        return info["node"]
    return None


def assign(board: Board, node: str, run: dict) -> None:
    """Record an assignment for `node` (called by the hub's dispatcher, in its own process)."""
    now = board.now()
    record = {"node": node, "state": "assigned", "pid": None, "exit_code": None, "stop": None, "error": None,
              "created_at": now, "updated_at": now, **run}
    with db.write_tx(board.conn) as c:
        if _get(c, RUN_PREFIX + run["run_id"]) is not None:
            raise Conflict(f"run {run['run_id']} is already assigned")
        _put(c, RUN_PREFIX + run["run_id"], record, "dispatcher", now)


def run(board: Board, run_id: str) -> dict | None:
    value = _get(board.conn, RUN_PREFIX + run_id)
    return value if isinstance(value, dict) else None


def assignments(board: Board, p: Principal) -> dict:
    """This node's unfinished runs: new ones to start, and stop requests for running ones."""
    _require_node(board, p)
    out = []
    for (value,) in board.conn.execute("SELECT value FROM board_state WHERE key >= ? AND key < ?",
                                       (RUN_PREFIX, RUN_PREFIX[:-1] + chr(ord(RUN_PREFIX[-1]) + 1))):
        try:
            r = json.loads(value)
        except (TypeError, ValueError):
            continue
        if isinstance(r, dict) and r.get("node") == p.name and r.get("state") != "ended":
            out.append({k: r.get(k) for k in ("run_id", "agent", "runtime", "thread_id", "project", "prompt",
                                              "timeout_seconds", "kill_grace_seconds", "headless_browser", "state",
                                              "stop")})
    return {"node": p.name, "runs": out}


def report(board: Board, p: Principal, run_id: str, state: str, pid: Any = None, exit_code: Any = None,
           error: Any = None) -> dict:
    """The node reports a run `running` (with its pid) or `ended` (with its exit code, or an error)."""
    _require_node(board, p)
    if state not in ("running", "ended"):
        raise Invalid("state must be running or ended")
    for name, v in (("pid", pid), ("exit_code", exit_code)):
        if v is not None and (isinstance(v, bool) or not isinstance(v, int)):
            raise Invalid(f"{name} must be an integer")
    if error is not None and (not isinstance(error, str) or len(error) > 300):
        raise Invalid("error must be a string of at most 300 characters")
    now = board.now()
    with db.write_tx(board.conn) as c:
        r = _get(c, RUN_PREFIX + run_id)
        if not isinstance(r, dict) or r.get("node") != p.name:
            raise NotFound(f"run {run_id} is not assigned to this node")
        if r["state"] == "ended":
            return r                      # idempotent: a retried report changes nothing
        if state == "running" and r["state"] != "assigned":
            return r
        r |= {"state": state, "updated_at": now, "node_reported_at": now}
        if state == "running":
            r["pid"] = pid
        else:
            r |= {"exit_code": exit_code, "error": error}
        _put(c, RUN_PREFIX + run_id, r, p.name, now)
    return r


def request_stop(board: Board, run_id: str, how: str) -> None:
    """Ask the node to stop a run. One it has not started yet ends here and now: the node will refuse it."""
    now = board.now()
    with db.write_tx(board.conn) as c:
        r = _get(c, RUN_PREFIX + run_id)
        if not isinstance(r, dict) or r.get("state") == "ended":
            return
        if r.get("state") == "assigned":
            r |= {"state": "ended", "stop": how, "error": "stopped before start", "updated_at": now}
        elif r.get("stop") == "kill" or r.get("stop") == how:
            return      # already asked: a repeated request changes nothing (and never looks like node activity)
        else:
            r |= {"stop": how, "updated_at": now}
        _put(c, RUN_PREFIX + run_id, r, "dispatcher", now)


def lost(board: Board, r: dict) -> bool:
    """The run's node has been silent long enough that the run counts as ended. Only times the node itself caused
    count (its last hello, its last report on this run) and the assignment's creation, never a hub-side update such as
    a stop request."""
    info = _get(board.conn, NODE_PREFIX + r["node"])
    seen = float(info.get("last_seen") or 0) if isinstance(info, dict) else 0.0
    mark = max(seen, float(r.get("node_reported_at") or 0), float(r.get("created_at") or 0))
    return board.now() - mark > NODE_LOST_SECONDS


def end_lost(board: Board, run_id: str) -> None:
    now = board.now()
    with db.write_tx(board.conn) as c:
        r = _get(c, RUN_PREFIX + run_id)
        if isinstance(r, dict) and r.get("state") != "ended":
            _put(c, RUN_PREFIX + run_id, r | {"state": "ended", "error": "node_lost", "updated_at": now},
                 "dispatcher", now)


class RemoteChild:
    """A run on a node, with the shape of dispatch.Child: poll() is None while it runs, else its exit code (-1 when
    the node reported none, or went silent); terminate() and kill() ask the node to stop it."""

    def __init__(self, board: Board, run_id: str):
        self.board, self.run_id, self.pid = board, run_id, None

    def poll(self) -> int | None:
        r = run(self.board, self.run_id)
        if r is None:
            return -1
        if r.get("state") != "ended" and lost(self.board, r):
            end_lost(self.board, self.run_id)
            r = run(self.board, self.run_id) or {}   # the node may have reported its real exit just before
        if r.get("state") != "ended":
            return None
        code = r.get("exit_code")
        return code if isinstance(code, int) and not isinstance(code, bool) else -1

    def terminate(self) -> None:
        request_stop(self.board, self.run_id, "terminate")

    def kill(self) -> None:
        request_stop(self.board, self.run_id, "kill")


def probe(board: Board, run_id: str) -> str:
    """check_orphans' probe for a node run: 'dead' once ended (or its node is lost), else 'unknown' (counted, never
    signalled by a later dispatcher; the node enforces its own timeout)."""
    r = run(board, run_id)
    if r is None or r.get("state") == "ended":
        return "dead"
    if lost(board, r):
        end_lost(board, run_id)
        return "dead"
    return "unknown"
