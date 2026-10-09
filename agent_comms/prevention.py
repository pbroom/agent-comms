"""The prevention inbox (DESIGN_NOTES "Prevention inbox").

Unstick (and the dispatcher's automatic recovery) ask the stalled agent for the cause of the stall and a proposal for
preventing it. Without configuration that proposal goes to the human (a Needs you item on the stalled thread). With
board.toml (or board.local.toml)

    [unstick]
    prevention_owner = "claude-code"   # the agent that handles prevention proposals
    prevention_thread = 14             # an open thread in the board's own project

the request text asks for the proposal on the prevention thread instead, addressed to the owner (an agent-to-agent
proposal: no Needs you item, no decision_question), with `prevention_for` naming the Unstick or recovery request.

Launch authorization: the human's configuration is a standing rule, but a narrow one. A post launches the owner only
when the server can verify all of this, from its own records and never from post text:
- it is on the prevention thread, by an agent, a `proposal` or `request` with needs_response, addressed to exactly
  the prevention owner, unsealed;
- `prevention_for` names an Unstick request (marked UNSTICK_POST_PREFIX when posted) or an automatic recovery request
  (autorecover.POST_PREFIX, kind "recovery") made in the last REQUEST_MAX_AGE_SECONDS, addressed to the author;
- it is the author's first prevention proposal for that request (FORWARDED_PREFIX, a plain INSERT).
Then, in the post's transaction, the server approves a fresh one-shot dispatcher rule for the owner alone on the
prevention thread (one launch, human_actions.RULE_HOURS) bound to that post, exactly like Unstick's: the dispatcher
launches for that post only under it, with a fixed purpose (ids only). At most DAILY_LAUNCHES such rules a rolling day.
No other post on the thread, and no other agent, gains anything: there is no thread-wide grant. A qualifying post
also does not count toward the thread's agent-post cap (each one answers a human or automatic request, so they are
bounded by those), so the inbox does not fill up and park the human again.

Forwarding (a triage owner, DESIGN_NOTES "Triage agent"): with `prevention_forward_to = "claude-code"` the owner may
forward one verified prevention proposal to that agent when adopting it needs a code change. The forward is a post
with `prevention_for` naming the *proposal*, checked the same way from server records only:
- the author is the configured owner (nobody else may forward), on the prevention thread, a `request` or `proposal`
  with needs_response, addressed to exactly prevention_forward_to, unsealed;
- `prevention_for` names a verified prevention proposal (PREVENTION_POST_PREFIX, not itself a forward) addressed to
  the owner, made in the last REQUEST_MAX_AGE_SECONDS;
- it is the first forward of that proposal (FORWARD_PREFIX, a plain INSERT).
Then the server approves a one-shot rule for prevention_forward_to alone, bound to the forward post (FORWARD_PURPOSE,
ids only), at most DAILY_FORWARD_LAUNCHES a rolling day. A forward does not count toward the thread's agent-post cap
(one per proposal, so it is bounded by them).
"""

from __future__ import annotations

import json
import logging
import os

from .config import PreventionConfig, prevention_config
from .core import Board, Conflict, Forbidden, Invalid, Principal

log = logging.getLogger("agent_comms.prevention")

UNSTICK_POST_PREFIX = "unstick.post."          # <post id>: an Unstick request (unstick.py)
FORWARDED_PREFIX = "unstick.prevention."       # <request post id>.<agent>: that agent's prevention proposal id
PREVENTION_POST_PREFIX = "unstick.prevention_post."   # <post id>: a verified prevention proposal (cap exemption)
LAUNCHES_KEY = "unstick.prevention_launches"   # launch-rule times in the rolling day
FORWARD_PREFIX = "unstick.prevention_forward."    # <proposal post id>: the owner's forward of it (post id)
FORWARD_LAUNCHES_KEY = "unstick.prevention_forward_launches"   # forward launch-rule times in the rolling day
REQUEST_MAX_AGE_SECONDS = 7 * 24 * 3600
DAILY_LAUNCHES = 10
DAILY_FORWARD_LAUNCHES = 10
WINDOW_SECONDS = 24 * 3600
PURPOSE = ("Prevention inbox (thread {thread}): review prevention proposal #{post} (for request #{request} on thread "
           "{source}) and handle it within your existing authorization; ask the human on this thread only if adopting "
           "it needs a new decision.")
FORWARD_PURPOSE = ("Prevention inbox (thread {thread}): {owner} forwarded prevention proposal #{proposal} (for request "
                   "#{request} on thread {source}) to you in #{post} because adopting it needs a code change; handle it "
                   "within your existing authorization; ask the human on this thread only if adopting it needs a new "
                   "decision.")

# The SQL test for "a verified prevention proposal" (core.Board._agent_posts_since_human: not counted toward the cap).
NOT_PREVENTION = "NOT EXISTS (SELECT 1 FROM board_state bs WHERE bs.key = 'unstick.prevention_post.' || {post}.id)"


def configured(board: Board) -> PreventionConfig | None:
    try:
        return prevention_config(getattr(board.s, "unstick", None) or None)
    except ValueError:
        return None     # settings are validated when read; a hand-built Settings with bad values counts as off


def board_home(board: Board) -> str:
    from .config import home
    path = board.s.config_path.parent if board.s.config_path is not None else home()
    return os.path.realpath(str(path))


def problem(board: Board, cfg: PreventionConfig) -> str | None:
    """Why the configured inbox cannot be used now (the request text then falls back to today's wording), or None."""
    agent = board.conn.execute("SELECT active, is_human FROM agents WHERE name = ?", (cfg.owner,)).fetchone()
    if agent is None or not agent["active"] or agent["is_human"]:
        return f"prevention_owner {cfg.owner!r} is not an active agent"
    thread = board.conn.execute("SELECT status, project FROM threads WHERE id = ?", (cfg.thread_id,)).fetchone()
    if thread is None:
        return f"prevention_thread {cfg.thread_id} does not exist"
    if thread["status"] != "open":
        return f"prevention_thread {cfg.thread_id} is closed"
    if os.path.realpath(thread["project"] or "") != board_home(board):
        return f"prevention_thread {cfg.thread_id} is not in the board's own project ({board_home(board)})"
    return None


def active(board: Board) -> PreventionConfig | None:
    """The prevention inbox when it is configured and usable now, else None (today's behavior)."""
    cfg = configured(board)
    if cfg is None:
        return None
    why = problem(board, cfg)
    if why is not None:
        log.warning("prevention inbox not used: %s", why)
        return None
    return cfg


def forward_problem(board: Board, cfg: PreventionConfig) -> str | None:
    """Why the owner cannot forward a proposal now (prevention_forward_to unset or unusable), or None."""
    if not cfg.forward_to:
        return "no prevention_forward_to is configured"
    agent = board.conn.execute("SELECT active, is_human FROM agents WHERE name = ?", (cfg.forward_to,)).fetchone()
    if agent is None or not agent["active"] or agent["is_human"]:
        return f"prevention_forward_to {cfg.forward_to!r} is not an active agent"
    return None


def status(board: Board) -> dict:
    """For configuration_status: whether the inbox is configured and usable (names and ids only)."""
    cfg = configured(board)
    if cfg is None:
        return {"configured": False, "active": False, "owner": None, "thread_id": None, "problem": None,
                "forward_to": None, "forward_problem": None}
    why = problem(board, cfg)
    return {"configured": True, "active": why is None, "owner": cfg.owner, "thread_id": cfg.thread_id,
            "problem": why, "forward_to": cfg.forward_to,
            "forward_problem": forward_problem(board, cfg) if cfg.forward_to else None}


def thread_link(board: Board, thread_id: int) -> str:
    return f"http://{board.s.host}:{board.s.port}/#thread-{int(thread_id)}"


def instructions(board: Board, cfg: PreventionConfig, thread_id: int) -> str:
    """The fixed request text that routes the prevention proposal to the inbox (ids and names only)."""
    return (f"Then send your proposal for preventing it next time to the prevention inbox, not to the human: a "
            f"`proposal` on thread #{cfg.thread_id} addressed to {cfg.owner} (to=[\"{cfg.owner}\"], "
            "needs_response=true, prevention_for=<this request's post id>, no decision_question), with a url ref to "
            f"this thread ({thread_link(board, thread_id)}). {cfg.owner} handles it. Once the stall itself is "
            "resolved, finish this request (request_reply, finished, citing your finding): the forwarded proposal "
            "leaves nothing on this thread for the human.")


def mark_unstick_post(board: Board, post_id: int, by: str) -> None:
    """Inside an Unstick post's transaction: mark it, so a prevention proposal can name it."""
    board.conn.execute("INSERT OR IGNORE INTO board_state(key, value, updated_by, updated_at) VALUES (?, ?, ?, ?)",
                       (UNSTICK_POST_PREFIX + str(post_id), json.dumps({"kind": "unstick"}), by, board.now()))


def _is_stall_request(c, request_id: int) -> bool:
    if c.execute("SELECT 1 FROM board_state WHERE key = ?", (UNSTICK_POST_PREFIX + str(request_id),)).fetchone():
        return True
    row = c.execute("SELECT value FROM board_state WHERE key = ?", ("auto_recovery.post." + str(request_id),)).fetchone()
    try:
        return row is not None and json.loads(row["value"]).get("kind") == "recovery"
    except (TypeError, ValueError, AttributeError):
        return False


def check(board: Board, c, p: Principal, *, prevention_for: object, thread_id: int | None, post_type: str,
          to: list[str], needs_response: bool, sealed: bool, now: float) -> dict:
    """Inside create_post's transaction, before the post is written: verify a post that says it is a prevention
    proposal (`prevention_for`). Raises Invalid/Forbidden/Conflict with what to fix. Returns what `record` needs."""
    if type(prevention_for) is not int or prevention_for <= 0:
        raise Invalid("prevention_for must be the post id of the Unstick or automatic recovery request")
    if p.is_human:
        raise Forbidden("prevention_for is for agents forwarding a prevention proposal")
    cfg = active(board)
    if cfg is None:
        raise Conflict("no prevention inbox is configured ([unstick] prevention_owner and prevention_thread); "
                       "propose prevention as the request asked, without prevention_for")
    proposal = _marker(c, prevention_for)
    if proposal is not None:
        return _check_forward(board, c, p, cfg, prevention_for, proposal, thread_id=thread_id, post_type=post_type,
                              to=to, needs_response=needs_response, sealed=sealed, now=now)
    if thread_id != cfg.thread_id:
        raise Invalid(f"a prevention proposal goes on the prevention thread #{cfg.thread_id}")
    if post_type not in ("proposal", "request") or not needs_response or sealed:
        raise Invalid("a prevention proposal is an unsealed `proposal` (or `request`) with needs_response=true")
    if to != [cfg.owner]:
        raise Invalid(f"address a prevention proposal to the prevention owner only: to=[\"{cfg.owner}\"]")
    req = c.execute("SELECT id, thread_id, to_agents, created_at FROM posts WHERE id = ?", (prevention_for,)).fetchone()
    if req is None or not _is_stall_request(c, prevention_for):
        raise Invalid(f"post #{prevention_for} is not an Unstick or automatic recovery request")
    if p.name not in json.loads(req["to_agents"] or "[]"):
        raise Forbidden(f"request #{prevention_for} was not addressed to you")
    if req["created_at"] < now - REQUEST_MAX_AGE_SECONDS:
        raise Conflict(f"request #{prevention_for} is more than 7 days old")
    if c.execute("SELECT 1 FROM board_state WHERE key = ?",
                 (f"{FORWARDED_PREFIX}{prevention_for}.{p.name}",)).fetchone():
        raise Conflict(f"you already forwarded a prevention proposal for request #{prevention_for}")
    return {"cfg": cfg, "request_id": prevention_for, "source_thread_id": req["thread_id"]}


def _marker(c, post_id: int) -> dict | None:
    """The PREVENTION_POST_PREFIX marker of a verified prevention proposal (or forward), else None."""
    row = c.execute("SELECT value FROM board_state WHERE key = ?", (PREVENTION_POST_PREFIX + str(post_id),)).fetchone()
    try:
        v = json.loads(row["value"]) if row else None
    except (TypeError, ValueError):
        return None
    return v if isinstance(v, dict) and type(v.get("request_post_id")) is int else None


def _check_forward(board: Board, c, p: Principal, cfg: PreventionConfig, proposal_id: int, proposal: dict, *,
                   thread_id: int | None, post_type: str, to: list[str], needs_response: bool, sealed: bool,
                   now: float) -> dict:
    """The owner forwards verified prevention proposal `proposal_id` to prevention_forward_to (module docstring)."""
    if p.name != cfg.owner:
        raise Forbidden(f"only the prevention owner ({cfg.owner}) can forward a prevention proposal")
    why = forward_problem(board, cfg)
    if why is not None:
        raise Conflict(f"cannot forward: {why} ([unstick] prevention_forward_to); handle the proposal yourself or ask "
                       "the human on the prevention thread")
    if "forward_of" in proposal:
        raise Invalid(f"post #{proposal_id} is already a forward; forward the original prevention proposal")
    if thread_id != cfg.thread_id:
        raise Invalid(f"a forwarded prevention proposal goes on the prevention thread #{cfg.thread_id}")
    if post_type not in ("proposal", "request") or not needs_response or sealed:
        raise Invalid("a forwarded prevention proposal is an unsealed `request` (or `proposal`) with "
                      "needs_response=true")
    if to != [cfg.forward_to]:
        raise Invalid(f"address a forwarded prevention proposal to prevention_forward_to only: "
                      f"to=[\"{cfg.forward_to}\"]")
    row = c.execute("SELECT thread_id, to_agents, created_at FROM posts WHERE id = ?", (proposal_id,)).fetchone()
    if row is None or row["thread_id"] != cfg.thread_id or cfg.owner not in json.loads(row["to_agents"] or "[]"):
        raise Forbidden(f"prevention proposal #{proposal_id} was not sent to you on the prevention thread")
    if row["created_at"] < now - REQUEST_MAX_AGE_SECONDS:
        raise Conflict(f"prevention proposal #{proposal_id} is more than 7 days old")
    if c.execute("SELECT 1 FROM board_state WHERE key = ?", (FORWARD_PREFIX + str(proposal_id),)).fetchone():
        raise Conflict(f"prevention proposal #{proposal_id} was already forwarded")
    return {"cfg": cfg, "forward_of": proposal_id, "request_id": proposal["request_post_id"],
            "source_thread_id": proposal.get("source_thread_id")}


def _spend(c, key: str, limit: int, now: float) -> list[float] | None:
    """The rule times in the rolling day under `key` when one more fits under `limit`, else None."""
    row = c.execute("SELECT value FROM board_state WHERE key = ?", (key,)).fetchone()
    try:
        times = [t for t in json.loads(row["value"]) if isinstance(t, (int, float)) and t > now - WINDOW_SECONDS] \
            if row else []
    except (TypeError, ValueError):
        times = []
    return times if len(times) < limit else None


def _human(board: Board) -> Principal | None:
    row = board.conn.execute("SELECT name, runtime FROM agents WHERE is_human = 1 AND active = 1 LIMIT 1").fetchone()
    return Principal(row["name"], row["runtime"], True) if row else None


def record(board: Board, c, p: Principal, post_id: int, checked: dict, now: float) -> int | None:
    """Inside the post's transaction, after it is written: mark it, and approve the owner's one-shot launch rule
    bound to it (unless the author is the owner, the human identity is missing, or the daily budget is spent). A
    forward (`forward_of`) approves prevention_forward_to's rule instead, from its own daily budget.
    Returns the rule id or None."""
    from . import human_actions
    cfg = checked["cfg"]
    forward_of = checked.get("forward_of")
    marker = {"request_post_id": checked["request_id"], "source_thread_id": checked["source_thread_id"],
              "agent": p.name}
    if forward_of is not None:
        marker["forward_of"] = forward_of
    c.execute("INSERT INTO board_state(key, value, updated_by, updated_at) VALUES (?, ?, ?, ?)",
              (PREVENTION_POST_PREFIX + str(post_id), json.dumps(marker), p.name, now))
    once = FORWARD_PREFIX + str(forward_of) if forward_of is not None else \
        f"{FORWARDED_PREFIX}{checked['request_id']}.{p.name}"
    c.execute("INSERT INTO board_state(key, value, updated_by, updated_at) VALUES (?, ?, ?, ?)",
              (once, json.dumps(post_id), p.name, now))
    human = _human(board)
    if human is None or (forward_of is None and p.name == cfg.owner):
        return None
    if forward_of is not None:
        target, key, limit = cfg.forward_to, FORWARD_LAUNCHES_KEY, DAILY_FORWARD_LAUNCHES
        purpose = FORWARD_PURPOSE.format(thread=cfg.thread_id, owner=cfg.owner, proposal=forward_of,
                                         request=checked["request_id"], source=checked["source_thread_id"],
                                         post=post_id)
    else:
        target, key, limit = cfg.owner, LAUNCHES_KEY, DAILY_LAUNCHES
        purpose = PURPOSE.format(thread=cfg.thread_id, post=post_id, request=checked["request_id"],
                                 source=checked["source_thread_id"])
    times = _spend(c, key, limit, now)
    if times is None:
        log.warning("prevention post #%s: the daily budget of %s launches is spent; not launching %s",
                    post_id, limit, target)
        return None
    rule = board.create_dispatch_rule(
        human, thread_id=cfg.thread_id, agents=[target], max_launches=1, purpose=purpose,
        expires_at=now + human_actions.RULE_HOURS * 3600, _in_transaction=True, _created_at=now)
    c.execute("""INSERT INTO board_state(key, value, updated_by, updated_at) VALUES (?, ?, ?, ?)
                 ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
              (human_actions.POST_RULE_PREFIX + str(post_id), json.dumps(rule["id"]), human.name, now))
    c.execute("""INSERT INTO board_state(key, value, updated_by, updated_at) VALUES (?, ?, ?, ?)
                 ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at""",
              (key, json.dumps(times + [now]), human.name, now))
    human_actions.prune_post_rules(board, human)
    return rule["id"]


def for_post(board: Board, post_id: int) -> dict | None:
    """Post output: {request_post_id, source_thread_id} for a verified prevention proposal (plus `forward_of`, the
    proposal's id, for the owner's forward of one), else None."""
    v = _marker(board.conn, post_id)
    if v is None:
        return None
    out = {"request_post_id": v["request_post_id"], "source_thread_id": v.get("source_thread_id")}
    if type(v.get("forward_of")) is int:
        out["forward_of"] = v["forward_of"]
    return out
