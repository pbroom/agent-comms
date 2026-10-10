"""Shared plumbing for the dashboard's one-click human actions (Unstick, and resolving a "Needs you" item).

Both post fixed, server-built text as the human and may approve a one-shot dispatcher rule first. The pieces here:
- a cooldown stamp in `board_state`, checked and set in one write transaction (a double click cannot pass twice),
  optionally together with a caller's own check in that same transaction;
- `post_as_human`: the rule-before-post ordering (the dispatcher ignores posts older than a rule) with rollback;
- `launch_outlook`: what happens next for the agents (live, launchable, no runner, dispatcher running, paused).
Nothing here reads a post body, title or summary.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import nullcontext
from typing import Any, Callable

from . import db, dispatch, requeue
from .core import Board, Conflict, Principal

RULE_HOURS = 6      # one-shot rules approved by a click expire after this
MAX_SESSIONS = 20   # live session ids returned to the page


def reserve_cooldown(board: Board, p: Principal, key: str, seconds: float, refuse: Callable[[int], str],
                     check: Callable[[sqlite3.Connection], None] | None = None, _in_transaction: bool = False) -> None:
    """Refuse (409, `refuse(wait_seconds)`) when `key` was stamped less than `seconds` ago, else stamp it now. `check`
    runs first inside the same write transaction, so a precondition and the stamp cannot race. `_in_transaction`: use
    the caller's open write transaction instead of a new one."""
    now = board.now()
    if _in_transaction and not board.conn.in_transaction:
        raise Conflict("internal: a joined cooldown needs an open write transaction")
    with (nullcontext(board.conn) if _in_transaction else db.write_tx(board.conn)) as c:
        if check is not None:
            check(c)
        row = c.execute("SELECT value FROM board_state WHERE key = ?", (key,)).fetchone()
        try:
            last = float(json.loads(row["value"])) if row else None
        except (TypeError, ValueError):
            last = None
        if last is not None and now - last < seconds:
            raise Conflict(refuse(int(seconds - (now - last)) + 1))
        c.execute("""INSERT INTO board_state(key, value, updated_by, updated_at) VALUES (?, ?, ?, ?)
                     ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_by = excluded.updated_by,
                     updated_at = excluded.updated_at""", (key, json.dumps(now), p.name, now))


def release_cooldown(board: Board, key: str) -> None:
    with db.write_tx(board.conn) as c:
        c.execute("DELETE FROM board_state WHERE key = ?", (key,))


POST_RULE_PREFIX = "launch.post_rule."   # board_state: the one-shot rule a one-click post approved, by post id


def post_rule_id(board: Board, post_id: int) -> int | None:
    """The one-shot rule approved together with this human post (post_as_human with launch), or None."""
    row = board.conn.execute("SELECT value FROM board_state WHERE key = ?", (POST_RULE_PREFIX + str(post_id),)).fetchone()
    try:
        value = json.loads(row["value"]) if row else None
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def one_click_rule_ids(board: Board) -> set[int]:
    """Rules approved by a one-click action for one post: no other post may launch under them."""
    out = set()
    for (value,) in board.conn.execute("SELECT value FROM board_state WHERE key LIKE ?", (POST_RULE_PREFIX + "%",)):
        try:
            v = json.loads(value)
        except (TypeError, ValueError):
            continue
        if isinstance(v, int) and not isinstance(v, bool):
            out.add(v)
    return out


def prune_post_rules(board: Board, p: Principal) -> int:
    """Delete post -> rule bindings that can no longer matter. Call inside a write transaction. A binding goes when
    its rule is dead for good: revoked, expired, or spent with every launch accounted for by a run that started and
    has ended (a launch whose run may still fail to spawn is refunded, which would revive the rule; so may a run
    stopped for a maintenance restart whose requeue is not decided yet, requeue.undecided). It stays while
    an ordinary rule on the post's thread that is active, or exhausted (a refund can revive it), was created at or
    before the post: without the binding that rule could launch for the post, which a dead one-click rule must not allow (its post launches nothing). Live rules keep
    their bindings, so the dispatcher's "only that rule for that post" lookup is unchanged. Returns how many went."""
    entries = []
    for key, value in board.conn.execute("SELECT key, value FROM board_state WHERE key LIKE ?", (POST_RULE_PREFIX + "%",)):
        try:
            entries.append((key, int(key[len(POST_RULE_PREFIX):]), json.loads(value)))
        except (TypeError, ValueError):
            continue
    if not entries:
        return 0
    started: dict[int, int] = {}   # rule id -> launches that started a process and ended
    active_runs: set[int] = set()
    for (value,) in board.conn.execute("SELECT value FROM board_state WHERE key LIKE 'dispatch.run.%'"):
        try:
            run = json.loads(value)
        except (TypeError, ValueError):
            continue
        if not isinstance(run, dict) or not isinstance(run.get("rule_id"), int):
            continue
        if run.get("status") in dispatch.ACTIVE or requeue.undecided(run):
            # A run stopped for a maintenance restart whose requeue is not decided yet may still give its launch back
            # to this rule (requeue.refundable needs the binding): keep it like a run in flight.
            active_runs.add(run["rule_id"])
        elif run.get("pid") is not None:
            started[run["rule_id"]] = started.get(run["rule_id"], 0) + 1
    # Ordinary rules that could launch for a post that has no binding, now or later: active, or exhausted (a launch
    # still in flight may fail to spawn and be refunded, making the rule active again). Not revoked or expired, which
    # is final. Live one-click rules launch only for their own post, and their bindings are never pruned.
    board._require_human(p, "prune one-click launch bindings")
    one_click = one_click_rule_ids(board)
    live = [r for r in (board._dispatch_rule_out(x) for x in board._dispatch_rows(active_only=True))
            if r["state"] in ("active", "exhausted") and r["id"] not in one_click]
    pruned = 0
    for key, post_id, rule_id in entries:
        if not isinstance(rule_id, int) or isinstance(rule_id, bool):
            continue
        rows = board._dispatch_rows(rule_id)
        if not rows:
            continue   # unknown (e.g. the human identity is inactive): keep
        target = board._dispatch_target(rows[0])
        state = board._dispatch_state(rows[0], target)
        dead = state in ("revoked", "expired") or (
            state == "exhausted" and rule_id not in active_runs and started.get(rule_id, 0) >= target["max_launches"])
        if not dead:
            continue
        post = board.conn.execute("SELECT thread_id, created_at FROM posts WHERE id = ?", (post_id,)).fetchone()
        if post is not None and any(r["thread_id"] == post["thread_id"] and r["created_at_ts"] <= post["created_at"]
                                    for r in live):
            continue
        pruned += board.conn.execute("DELETE FROM board_state WHERE key = ?", (key,)).rowcount
    return pruned


def post_as_human(board: Board, p: Principal, *, thread_id: int, body: str, type: str, to: list[str],
                  needs_response: bool, launch: list[str] | None = None,
                  purpose: str | None = None, answer_to: list[int] | None = None, answer_recipient: str | None = None,
                  post_check: Callable[[], None] | None = None,
                  post_hook: Callable[[dict], None] | None = None, decision_question: dict | None = None,
                  _in_transaction: bool = False) -> tuple[dict, dict | None]:
    """Post fixed text as the human, in the human's own board session. With `launch`, first approve a fresh one-shot
    dispatcher rule (one launch each, RULE_HOURS) for exactly those agents, and record it against the post
    (POST_RULE_PREFIX): the dispatcher launches for this post only under this rule, so its purpose is the one quoted
    in the launch prompt. An existing rule is never counted as covering the launch (it may carry another purpose,
    or be picked for another post). The rule comes first because the dispatcher only triggers on posts created at or
    after a rule; if the post then fails, the rule is revoked. Returns (post, rule or None).

    `decision_question`: a structured question built from server-side facts only (autorecover's escalations).
    `_in_transaction`: run inside the caller's open write transaction (a decision action): the rule, the post and the
    binding then commit or roll back with the caller, so nothing is revoked here on failure."""
    board._require_human(p, "post as the human")
    if _in_transaction and not board.conn.in_transaction:
        raise Conflict("internal: a joined post needs an open write transaction")
    rule = None
    try:
        if launch:
            agents = list(dict.fromkeys(launch))
            rule = board.create_dispatch_rule(p, thread_id=thread_id, agents=agents, purpose=purpose or "",
                                              max_launches=len(agents), expires_at=board.now() + RULE_HOURS * 3600,
                                              _in_transaction=_in_transaction)
        human_sid = board.human_session(p)
        with (nullcontext(board.conn) if _in_transaction else db.write_tx(board.conn)):
            if post_check is not None:
                post_check()
            post = board.create_post(p, human_sid, body=body, type=type, thread_id=thread_id, to=to,
                                     needs_response=needs_response, answer_to=answer_to,
                                     decision_question=decision_question,
                                     _answer_recipient=answer_recipient, _in_transaction=True)
            if rule is not None:
                board.conn.execute("""INSERT INTO board_state(key, value, updated_by, updated_at) VALUES (?, ?, ?, ?)
                                      ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
                                   (POST_RULE_PREFIX + str(post["id"]), json.dumps(rule["id"]), p.name, board.now()))
                prune_post_rules(board, p)   # bindings only grow here, so this keeps them bounded
            if post_hook is not None:
                post_hook(post)
    except Exception:
        if rule is not None and not _in_transaction:
            board.revoke_dispatch_rule(p, rule["id"])
        raise
    return post, rule


def launch_outlook(board: Board, config: dispatch.DispatchConfig, agents: list[str]) -> dict[str, Any]:
    """What happens next for these agents: `live_agents` (a session inside the dispatcher's live window, or a
    dispatched run in progress: they see the post through their normal read path and are not launched),
    `sessions` (those live session ids, last seen first), `no_runner` (can never be launched),
    `dispatcher_running` and `paused`. Ids, names and times only."""
    status = dispatch.loop_status(board, config)
    now, window = board.now(), config.live_minutes * 60
    live = [a for a in agents if (board.conn.execute("SELECT MAX(last_seen) FROM sessions WHERE agent = ?", (a,))
                                  .fetchone()[0] or float("-inf")) >= now - window]
    if status.get("running"):
        live += [a for a in sorted({r.get("agent") for r in dispatch._active_records(board)} & set(agents))
                 if a not in live]
    runtimes = {r["name"]: r["runtime"] for r in board.conn.execute("SELECT name, runtime FROM agents")}
    sessions: list[int] = []
    if agents:
        marks = ",".join("?" * len(agents))
        sessions = [r["id"] for r in board.conn.execute(
            f"""SELECT id FROM sessions WHERE agent IN ({marks}) AND last_seen >= ?
                ORDER BY last_seen DESC, id DESC LIMIT {MAX_SESSIONS}""", (*agents, now - window))]
    return {"dispatcher_running": bool(status.get("running")), "paused": board.is_paused(),
            "live_agents": live, "sessions": sessions,
            "no_runner": [a for a in agents if config.runner_for(a, runtimes.get(a)) is None]}


def launchable_agents(board: Board, config: dispatch.DispatchConfig) -> list[str]:
    """Active non-human agents the dispatcher could launch now: a runner is configured and they are not live. For
    the dashboard's "Approve & launch" button (the server re-checks nothing from the page: it only decides whether
    the button is offered)."""
    names = [r["name"] for r in board.conn.execute(
        "SELECT name FROM agents WHERE active = 1 AND is_human = 0 ORDER BY name")]
    out = launch_outlook(board, config, names)
    return [a for a in names if a not in out["live_agents"] and a not in out["no_runner"]]
