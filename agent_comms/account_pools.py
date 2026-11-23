"""Account pools: one name for interchangeable agents, balanced by headroom (DESIGN_NOTES "Account pools").

Two subscriptions of one CLI work through the board as two agents (agent_accounts.py: `claude-work` beside
`claude-code`). A POOL names such agents in preference order, and a NEW session may be asked for by the pool's name
wherever its agent is named: `board_session_spawn(child_agent=...)`, the human's `board session start --agent` and
`start --parent ... --agent`. Three hub settings, all under `[dispatch.sessions.spawn]` and read on the hub only
(pools apply to the human's root starts whether or not child sessions are enabled):

- `pools`: pool name -> member agents, in preference order. A member appears once; a pool name is never a member of
  any pool. A pool whose name is also an agent's is void: the name means that agent, exactly as before, and
  capabilities say why the pool is not offered. Its active members must be of one runtime family (Claude or Codex),
  else a start naming it is refused with the reason; a member that is not an active agent is passed over, with that
  reason, like any member that cannot be started.
- `pool_rules`: project (as the board names it: agent_accounts.project_id) -> optional `prefer`, `only` and `never`
  lists of agent names (members of some pool). For a pool started in that project: `only` cuts it down to the listed
  members (an `only` that names none of THIS pool's members does not apply to it), `never` takes members out, and
  `prefer` puts the listed members first, in that order, ahead of the pool's own order. `never` wins over the other
  two (a name in both is simply out). They narrow and reorder and never add a member; nothing they say overrides
  [dispatch.agent_projects], what a node offers, access, the grant or any other check. A rule that leaves no member
  refuses the start with the reason.
- `pool_headroom`: `skip_at_percent` (90: a member with a usage window at or above this is skipped),
  `stale_minutes` (60: an observation older than this is ignored) and `cooldown_minutes` (30: how long an account that
  hit its limit counts as exhausted when no reset time is known).

Two rules hold everything here together:
(a) ELIGIBLE has one definition: the creating path's own checks for that agent. `choose` takes them as `eligible`
    from the path itself (hosted._root_problem, hosted_tree.member_problem, hosted_spawn._member_problem), which are
    the very functions the creation runs, so a member the choice passes is one the creation accepts, and one the
    creation would refuse is passed over with the reason it would give.
(b) USAGE IS ADVISORY AND TOTAL: every usage value, wherever it is read (a provider's stream, the host, the node's
    report, the hub's hello, the hub's own record), goes through `providers.clean_usage`, which cannot raise; what it
    cannot use is dropped. Usage can change which eligible member is taken, never whether one is, and nothing about
    it can make a turn, a session, a heartbeat or a request fail.

`choose` decides inside the write that creates the session, on the settings read once in that write: the members in
rule order; those that fail `eligible` are dropped with their reasons; of the rest the first with room is taken, a
member with a window at or over the threshold, or exhausted, being skipped ("overflow"). Unknown usage counts as room.
If every eligible member is near its limit the first eligible one is taken anyway and the reason says so: headroom
never refuses work. The session is then that member's, and its record keeps how it was chosen (`pool`: the name, the
reason and a short summary), for `board session show` and the spawn audit (which posts the summary only).

Usage comes from nodes (`store_usage`, from a hello that carries `usage`, which a node sends only after the hub's
hello answer asked for it: `report_usage`, said only while pools are configured): per runner, one observation per
window, each with its own age. Nothing here can tell which subscription a process really used: the board sets the
account folder, the CLI decides what it does with it.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Any, Callable

from .config import NAME_RE
from .providers import FAILED_TURN, USAGE_WINDOWS_MAX, clean_usage, number

log = logging.getLogger("agent_comms.account_pools")

POOLS = "[dispatch.sessions.spawn.pools]"
RULES = "[dispatch.sessions.spawn.pool_rules]"
HEADROOM = "[dispatch.sessions.spawn.pool_headroom]"
RULE_KEYS = ("prefer", "only", "never")
MAX_POOLS, MAX_MEMBERS, MAX_RULES = 20, 10, 200
HEADROOM_DEFAULTS = {"skip_at_percent": 90, "stale_minutes": 60, "cooldown_minutes": 30}
HEADROOM_BOUNDS = {"skip_at_percent": (1, 100), "stale_minutes": (1, 7 * 24 * 60), "cooldown_minutes": (1, 7 * 24 * 60)}
USAGE_PREFIX = "host.usage."       # <node>: runner key -> window -> the latest observation that node reported
RESET_HORIZON = 8 * 86400          # a reset time further out than this (the longest window is 7 days) is not believed
REASON_MAX = 300                   # characters of one member's reason kept in a choice's `why`
WHY_MAX = 4000                     # ... and of the whole `why`


# ---------------------------------------------------------------- settings (static checks: DispatchConfig loads them)


def _names(v: Any, what: str) -> list[str]:
    if (not isinstance(v, list) or not v or len(v) > MAX_MEMBERS
            or any(not isinstance(n, str) or not NAME_RE.match(n) for n in v) or len(set(v)) != len(v)):
        raise ValueError(f"{what} must be a list of 1 to {MAX_MEMBERS} distinct agent names")
    return list(v)


def parse_pools(v: Any) -> dict[str, list[str]]:
    if not isinstance(v, dict) or len(v) > MAX_POOLS:
        raise ValueError(f'{POOLS} must be a table of at most {MAX_POOLS} pool = ["agent", ...]')
    out = {}
    for name, members in v.items():
        if not isinstance(name, str) or not NAME_RE.match(name):
            raise ValueError(f"{POOLS}: a pool's name must be written like an agent name (lowercase, a-z0-9_-)")
        out[name] = _names(members, f"{POOLS} {name}")
    taken = {m for ms in out.values() for m in ms}
    both = sorted(taken & set(out))
    if both:
        raise ValueError(f"{POOLS}: {both[0]} is both a pool and a member of one; a pool's members are agents")
    return out


def parse_rules(v: Any) -> dict[str, dict[str, list[str]]]:
    """Each project's lists as written. A name in `never` and in `prefer` or `only` too is not an error: `never`
    wins (`ordered`)."""
    from .agent_accounts import project_id

    if not isinstance(v, dict) or len(v) > MAX_RULES:
        raise ValueError(f'{RULES} must be a table of at most {MAX_RULES} "/absolute/project" = {{prefer, only, '
                         'never}}')
    out = {}
    for project, rule in v.items():
        q = project_id(project)
        if (q is None or not q.startswith("/") or q.startswith("//") or len(q) > 1024
                or any(ord(ch) < 32 or ord(ch) == 127 for ch in q) or q != _normal(q)):
            raise ValueError(f"{RULES}: each project must be an absolute, normalized path, as the board shows it")
        if not isinstance(rule, dict) or not rule or set(rule) - set(RULE_KEYS):
            raise ValueError(f"{RULES} {project} must be a table of prefer, only and/or never (lists of agent names)")
        if q in out:
            raise ValueError(f"{RULES}: {q} is given twice")
        out[q] = {k: _names(rule[k], f"{RULES} {project} {k}") for k in RULE_KEYS if k in rule}
    return out


def _normal(path: str) -> str:
    import os
    return os.path.normpath(path)


def parse_headroom(v: Any) -> dict[str, float]:
    if not isinstance(v, dict):
        raise ValueError(f"{HEADROOM} must be a table")
    out = dict(HEADROOM_DEFAULTS)
    for k, x in v.items():
        if k not in HEADROOM_BOUNDS:
            raise ValueError(f"unknown setting {HEADROOM} {k}")
        lo, hi = HEADROOM_BOUNDS[k]
        f = number(x)
        if f is None or not lo <= f <= hi:
            raise ValueError(f"{HEADROOM} {k} must be a number from {lo} to {hi}")
        out[k] = x
    return out


def cross_check(pools: dict[str, list[str]], rules: dict[str, dict[str, list[str]]]) -> None:
    """Every name a rule gives is a member of some pool (a rule never adds a member, so another name is a typo)."""
    members = {m for ms in pools.values() for m in ms}
    for project, rule in rules.items():
        for k, names in rule.items():
            for n in names:
                if n not in members:
                    raise ValueError(f"{RULES} {project} {k}: {n} is not a member of any pool in {POOLS}")


# ---------------------------------------------------------------- the hub's view


def _spawn(board: Any):
    from .hosted_tree import spawn_config
    return spawn_config(board)


def _agents(conn) -> dict[str, str]:
    from . import nodes
    return {r["name"]: r["runtime"] for r in conn.execute(
        "SELECT name, runtime FROM agents WHERE active = 1 AND is_human = 0 ORDER BY name")
        if r["runtime"] != nodes.NODE_RUNTIME}


def configured(board: Any, spawn: Any = None) -> bool:
    return bool((spawn if spawn is not None else _spawn(board)).pools)


def maybe_pool(board: Any, name: Any) -> bool:
    """Whether a creating path must look at `name` as a pool's, in its write (with a fresh reading of the settings
    there): pools are configured and `name` is not an agent's. An agent's name (or none) takes main's path untouched,
    whatever the settings say."""
    return isinstance(name, str) and configured(board) and name not in _agents(board.conn)


def pool_for(board: Any, name: Any, spawn: Any = None) -> str | None:
    """`name` as a pool to resolve on this reading of the settings (`spawn`), or None: no pools, not a pool's name,
    or also an agent's name, which then means that agent as it always did."""
    cfg = spawn if spawn is not None else _spawn(board)
    if not cfg.pools or not isinstance(name, str) or name not in cfg.pools:
        return None
    return None if name in _agents(board.conn) else name


def family(runtime: str) -> str:
    """The CLI an agent's runtime is: `claude-code` and every other claude* is Claude, codex* is Codex."""
    for f in ("claude", "codex"):
        if runtime.startswith(f):
            return f
    return runtime


def problem(board: Any, pool: str, spawn: Any = None) -> str | None:
    """What is wrong with the pool as a whole as the board stands now, or None: it has an agent's name, or its
    active members mix runtime families. (A member that is not an active agent is that member's problem only.)"""
    cfg = spawn if spawn is not None else _spawn(board)
    agents = _agents(board.conn)
    if pool in agents:
        return (f"pool {pool} has the name of an agent, so {pool} means that agent; rename the pool in {POOLS}")
    kinds = sorted({family(agents[m]) for m in cfg.pools[pool] if m in agents})
    if len(kinds) > 1:
        return (f"pool {pool} mixes runtimes ({', '.join(kinds)}); a pool's members must all be one CLI's accounts "
                f"(fix {POOLS})")
    return None


def ordered(cfg: Any, pool: str, project: Any) -> tuple[list[str], list[str]]:
    """(the pool's members in the order the project's rule leaves them, what the rule did, in words). `only` cuts
    (when it names a member of this pool), `never` removes (and wins over the others), `prefer` moves to the front."""
    from .agent_accounts import project_id

    members = list(cfg.pools[pool])
    rule = cfg.pool_rules.get(project_id(project) or "") or {}
    notes = []
    never = [n for n in rule.get("never", []) if n in members]
    only = [n for n in rule.get("only", []) if n in members]
    if only:
        members = [m for m in members if m in only]
        notes.append("only " + ", ".join(only))
    if never:
        members = [m for m in members if m not in never]
        notes.append("never " + ", ".join(never))
    prefer = [n for n in rule.get("prefer", []) if n in members]
    if prefer and prefer != members[:len(prefer)]:
        notes.append("prefer " + ", ".join(prefer))
    members = prefer + [m for m in members if m not in prefer]
    return members, notes


def describe(pool: str, members: list[str]) -> str:
    """One line for capabilities and the spawn frame: the order, never a promise of a member."""
    if len(members) == 1:
        return f"{pool}: {members[0]} (the only member that can be started here)"
    if len(members) == 2:
        return f"{pool}: {members[0]} first, {members[1]} when it is near its limit"
    return (f"{pool}: {members[0]} first, then " + ", then ".join(members[1:])
            + ", each when the ones before it are near their limits")


def offered(board: Any, project: Any, placeable: list[str], spawn: Any = None) -> list[dict]:
    """The pools a caller could name: each with at least one member in `placeable` (agents that pass the start's own
    checks, as the caller found them), in rule order, and its line. Nothing when no pools are set."""
    cfg = spawn if spawn is not None else _spawn(board)
    out = []
    for pool in cfg.pools:
        if problem(board, pool, cfg) is not None:
            continue
        members = [m for m in ordered(cfg, pool, project)[0] if m in placeable]
        if members:
            out.append({"pool": pool, "agents": members, "about": describe(pool, members)})
    return out


def problems(board: Any, spawn: Any = None) -> list[str]:
    cfg = spawn if spawn is not None else _spawn(board)
    return [p for p in (problem(board, pool, cfg) for pool in cfg.pools) if p]


# ---------------------------------------------------------------- headroom


def _observations(conn, node: str | None, agent: str, runtime: str | None) -> list[dict]:
    """What the hub keeps for the runner this agent runs with on `node`: every observation, each cleaned again on the
    way out (rule (b)), with the hub time it was seen. Never raises; anything unusable is left out."""
    from . import nodes
    try:
        if not node:
            return []
        info = nodes._get(conn, nodes.NODE_PREFIX + node)
        record = nodes._get(conn, USAGE_PREFIX + node)
        key = nodes.runner_key(info, agent, runtime) if isinstance(info, dict) else None
        windows = record.get(key) if isinstance(record, dict) and key else None
        out = []
        for obs in (windows.values() if isinstance(windows, dict) else ()):
            clean = clean_usage(obs)
            seen = number(obs.get("observed_at")) if isinstance(obs, dict) else None
            if clean is not None and seen is not None:
                out.append(clean | {"observed_at": seen})
        return out
    except Exception:
        _once("could not read a node's usage record; taking it as unknown")
        return []


def _at(ts: float) -> str:
    from .core import iso
    try:
        return (iso(ts) or "").replace("+00:00", "Z")
    except (OverflowError, OSError, ValueError):
        return "an unreadable time"


RANK = {"unknown": 0, "room": 1, "over": 2, "exhausted": 3}


def _reset(obs: dict) -> float | None:
    """The reading's reset time, when it is believed: at most RESET_HORIZON after the reading was seen."""
    resets = obs.get("resets_at")
    return float(resets) if resets is not None and resets <= float(obs["observed_at"]) + RESET_HORIZON else None


def expires(obs: dict, headroom: dict) -> tuple[float, bool]:
    """(when this reading stops counting, whether it is an exhausted mark), by its own rule (the usage table): an
    exhausted mark at its window's reset (a failed turn's only if that reset was ahead when the mark was seen), else at
    the end of the cool-down; any other reading at the earlier of its window's reset and `stale_minutes` after it was
    seen. A reset time is believed up to RESET_HORIZON after the observation. The one rule both the standing and the
    record's retention (`store_usage`) follow."""
    seen, resets = float(obs["observed_at"]), _reset(obs)
    if obs.get("exhausted") is True:
        cooldown = seen + float(headroom["cooldown_minutes"]) * 60
        if obs.get("window") == FAILED_TURN:
            # A turn that failed now: until a reset still ahead when it was seen, else the cool-down (a reset it
            # inherited from an earlier refusal, already past, says nothing about this one).
            return (resets if resets is not None and resets > seen else cooldown), True
        # The provider's own refusal of a window: until that window's reset (one already past when it was seen means
        # the window has reset: room), else the cool-down.
        return (resets if resets is not None else cooldown), True
    stale = seen + float(headroom["stale_minutes"]) * 60
    return (min(stale, resets) if resets is not None else stale), False


def standing_of(obs: dict, now: float, headroom: dict) -> tuple[str, str]:
    """One observation: ("room" | "unknown" | "over" | "exhausted", what was seen, in words), by the usage record's
    rules (DESIGN_NOTES "Account pools", the table; `USAGE_RULES` in the tests). Unknown and stale count as room."""
    until, marked = expires(obs, headroom)
    name = obs.get("label") or (obs.get("window") if obs.get("window") != FAILED_TURN else None)
    window = f" of its {name} window" if name else ""
    if marked:
        if now < until:
            which = f"its {name} limit" if name else "its limit"
            timed = until == _reset(obs)
            return "exhausted", f"hit {which} (until {_at(until)}" + ("" if timed else ", a cool-down") + ")"
        return "unknown", "its limit has reset since it was hit"
    if now >= until:
        return "unknown", "its usage window has reset since, or was last seen too long ago"
    pct = obs.get("percent")
    if pct is None:
        return "unknown", "its usage is not known"
    resets = _reset(obs)
    shown = f"{pct:g}%{window}" + (f" until {_at(resets)}" if resets is not None else "")
    return ("over" if pct >= float(headroom["skip_at_percent"]) else "room"), f"at {shown}"


def standing(observations: list[dict], now: float, headroom: dict) -> tuple[str, str]:
    """A member's standing: the worst of its observations (each window has its own), exhausted > over > room >
    unknown. No observation: unknown."""
    best = ("unknown", "no usage seen")
    for obs in observations:
        got = standing_of(obs, now, headroom)
        if RANK[got[0]] > RANK[best[0]]:
            best = got
    return best


@dataclass
class Choice:
    pool: str
    agent: str
    runtime: str
    node: str | None
    why: str
    summary: str
    access: str | None = None   # the access the creating path's checks judged this member at (when it says one)

    def record(self) -> dict:
        return {"name": self.pool, "why": self.why, "summary": self.summary}


def _short(text: str) -> str:
    text = text.rstrip(".")
    return text if len(text) <= REASON_MAX else text[:REASON_MAX - 3] + "..."


def choose(board: Any, conn, pool: str, project: Any,
           eligible: Callable[[str, str], tuple[str | None, str | None]], spawn: Any) -> Choice:
    """The member of `pool` a new session in `project` is for, on this reading of the settings (`spawn`, the one the
    caller recognised the pool on, in its write). `eligible(agent, runtime)` -> (its node, None[, the access it
    judged]) when the creating path's own checks pass for that agent, else (None, why not[, None]) (rule (a)); the
    chosen member's node and access are carried to the start (`Choice.node`, `Choice.access`). Read in the caller's write transaction
    (`conn`). Conflict with the reason when the pool is broken, the project's rule leaves no member, or no member can
    be started; never because of headroom."""
    from .core import Conflict

    cfg = spawn
    broken = problem(board, pool, cfg)
    if broken:
        raise Conflict(broken)
    agents = _agents(conn)
    members, notes = ordered(cfg, pool, project)
    rule = f"the project rule for {project} ({'; '.join(notes)})" if notes else None
    if not members:
        raise Conflict(f"no member of pool {pool} may be started in {project}: {rule or 'the project rule'} in "
                       f"{RULES} leaves none")
    can, cannot = [], []
    for m in members:
        if m not in agents:
            node, why, access = None, f"{m} is not an active agent on this board", None
        else:
            got = eligible(m, agents[m])
            node, why, access = got[0], got[1], got[2] if len(got) > 2 else None
        if why is None:
            can.append((m, node, access))
        else:
            cannot.append(f"{m} cannot be started here ({_short(why)})")
    if not can:
        raise Conflict(f"no member of pool {pool} can be started in {project}: " + "; ".join(cannot))
    now, headroom = board.now(), cfg.pool_headroom
    skipped, pick = [], None
    for m, node, access in can:
        state, seen = standing(_observations(conn, node, m, agents[m]), now, headroom)
        if state in ("room", "unknown"):
            pick = (m, node, access)
            break
        skipped.append(f"{m} was {seen}" if state == "over" else f"{m} {seen}")
    parts = [f"by {rule}"] if rule else []
    parts += cannot
    summary = (["by a project rule"] if rule else []) + ([f"{len(cannot)} member(s) could not be started"]
                                                         if cannot else [])
    if pick is None:
        pick = can[0]
        parts.append("every member that can be started is near its limit (" + "; ".join(skipped) + f"), so the "
                     f"first of them was taken anyway: headroom never refuses work")
        summary.append("every member near its limit, the first taken anyway")
    elif skipped:
        parts.append("overflow, because " + "; ".join(skipped)
                     + f" (skipped at {float(headroom['skip_at_percent']):g}% or when exhausted)")
        summary.append(f"overflow past {len(skipped)} member(s) near the limit")
    # Nothing to say (no rule changed the order, no member was passed over): the pool's own first member.
    why = f"{pick[0]} from pool {pool}: " + ("; ".join(parts) if parts else "first choice")
    if len(why) > WHY_MAX:
        why = why[:WHY_MAX - 3] + "..."
    return Choice(pool, pick[0], agents[pick[0]], pick[1], why, "; ".join(summary) or "first choice", pick[2])


# ---------------------------------------------------------------- what nodes report


_said: set[str] = set()


def _once(what: str) -> None:
    if what not in _said:
        _said.add(what)
        log.warning(what)


def clean_report(usage: Any, runners: list[str]) -> dict[str, list[dict]]:
    """A hello's `usage` as the hub takes it: runner key (one the node offers) -> its observations, each cleaned
    (`clean_usage` with its age: rule (b)). It never raises and never refuses the hello: what is not usable (an
    unknown runner, a value of the wrong kind, a number out of range, too many) is dropped, and said once in the log.
    The limits are a hello's own (nodes.MAX_ITEMS runners) and USAGE_WINDOWS_MAX observations per runner."""
    from .nodes import MAX_ITEMS

    out: dict[str, list[dict]] = {}
    dropped = False
    try:
        items = list(usage.items()) if isinstance(usage, dict) else []
        dropped = usage is not None and not isinstance(usage, dict)
        for key, observations in items[:MAX_ITEMS]:
            if not isinstance(key, str) or key not in runners or not isinstance(observations, list):
                dropped = True
                continue
            kept = [c for c in (clean_usage(o, age=True) for o in observations[:USAGE_WINDOWS_MAX]) if c is not None]
            dropped = dropped or len(kept) != len(observations)
            if kept:
                out[key] = kept
        dropped = dropped or len(items) > MAX_ITEMS
    except Exception:
        out, dropped = {}, True
    if dropped:
        _once("a node's usage report held values the hub cannot use; they were dropped (usage is advisory)")
    return out


def store_usage(c, node: str, usage: dict[str, list[dict]], runners: list[str], now: float,
                headroom: dict | None = None) -> None:
    """Inside EVERY hello's transaction while pools are configured (the caller asks; `usage` may be empty), by the usage
    record's rules (DESIGN_NOTES table): each window's newest reading per runner, keyed by the window's identity, in
    the hub's time (`observed_at`: now less the age the node gave, so the node's clock never matters); an older one
    than the hub has is ignored. Pruned each time: runners the node no longer offers, readings that have expired by
    their own rule (`expires`, on `headroom`: the hub's pool_headroom), and beyond USAGE_WINDOWS_MAX windows per
    runner, the newest kept. Nothing is written when there is nothing, and was nothing, to keep."""
    from .nodes import _get, _put

    stored = _get(c, USAGE_PREFIX + node)
    if not usage and not stored:
        return
    record = dict(stored) if isinstance(stored, dict) else {}
    for key, observations in usage.items():
        windows = dict(record.get(key)) if isinstance(record.get(key), dict) else {}
        for obs in observations:
            seen = now - obs["age"]
            old = windows.get(obs["window"])
            if isinstance(old, dict) and (number(old.get("observed_at")) or 0) > seen:
                continue
            windows[obs["window"]] = {k: obs[k] for k in ("window", "label", "percent", "resets_at", "exhausted")} | {
                "observed_at": seen}
        record[key] = windows

    rules = headroom if headroom is not None else HEADROOM_DEFAULTS

    def keep(o: Any) -> bool:
        """Until the reading has expired by its own rule (`expires`: stale, its reset passed, exhausted-until or the
        cool-down over), so a configured multi-day cool-down outlives any fixed age."""
        seen = number(o.get("observed_at")) if isinstance(o, dict) else None
        clean = clean_usage(o) if seen is not None else None
        return clean is not None and now < expires(clean | {"observed_at": seen}, rules)[0]
    pruned = {}
    for key, windows in record.items():
        if key not in runners or not isinstance(windows, dict):
            continue
        live = sorted(((s, o) for s, o in windows.items() if keep(o)), key=lambda x: -number(x[1]["observed_at"]))
        if live:
            pruned[key] = dict(live[:USAGE_WINDOWS_MAX])
    _put(c, USAGE_PREFIX + node, pruned, node, now)


def usage_view(conn, node: str) -> dict:
    from .nodes import _get
    record = _get(conn, USAGE_PREFIX + node)
    return record if isinstance(record, dict) else {}
