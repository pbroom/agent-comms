"""Short-lived probe attestations for routing, never permissions or authorization.

An agent reports its own successful probe. The server binds that report to the
session's identity and environment; it cannot independently verify the probe.
"""
from __future__ import annotations

import json
import math

from . import db
from .core import Conflict, Forbidden, Invalid, NotFound, iso

MAX_TTL_SECONDS = 1800
LIVE_SECONDS = 90


def requires_browser(names):
    return any(name.lower() == 'browser' or name.lower().startswith(('browser:', 'browser.')) for name in names)


def _names(value):
    if not isinstance(value, list) or not value or len(value) > 50:
        raise Invalid("capabilities must be a nonempty list of at most 50 names")
    if any(not isinstance(n, str) or not n.strip() or len(n) > 100 or n != n.strip() for n in value):
        raise Invalid("capability names must be nonempty strings of at most 100 characters")
    return sorted(set(value))


def register(board, p, session_id, capabilities, evidence, ttl_seconds=MAX_TTL_SECONDS, activity='unknown'):
    """Record the caller's successful probes without creating any authority."""
    names = _names(capabilities)
    if activity not in ('idle', 'active', 'unknown'):
        raise Invalid('activity must be idle, active, or unknown')
    if not isinstance(evidence, str) or not evidence.strip() or len(evidence.encode()) > board.s.body_max_bytes:
        raise Invalid("successful probe evidence is required within the body size limit")
    if (isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, (int, float))
            or not math.isfinite(ttl_seconds) or not 0 < ttl_seconds <= MAX_TTL_SECONDS):
        raise Invalid("ttl_seconds must be positive and at most 1800")
    with db.write_tx(board.conn) as c:
        board._check_agent_write(p)
        s = board._session(p, session_id)
        now = board.now()
        c.execute('''INSERT INTO session_activity(session_id,state,recorded_at) VALUES (?,?,?)
            ON CONFLICT(session_id) DO UPDATE SET state=excluded.state,recorded_at=excluded.recorded_at''',
            (session_id,activity,now))
        c.execute("""INSERT INTO session_capabilities
            (session_id,attested_agent,project,worktree,capabilities,evidence,verified_at,expires_at)
            VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(session_id) DO UPDATE SET
            attested_agent=excluded.attested_agent,project=excluded.project,worktree=excluded.worktree,
            capabilities=excluded.capabilities,evidence=excluded.evidence,
            verified_at=excluded.verified_at,expires_at=excluded.expires_at""",
            (session_id,s["agent"],s["project"],s["worktree"],json.dumps(names),evidence.strip(),now,now+ttl_seconds))
    return {"session_id": session_id, "agent": s["agent"], "project": s["project"], "worktree": s["worktree"],
            "capabilities": names, "evidence": evidence.strip(), "verified_at": iso(now),
            "expires_at": iso(now + ttl_seconds), "activity": activity,
            "authority": "self_reported_probe_not_authorization"}


def eligible(board, target_session_id, project, required_capabilities):
    """Rechecked by request assignment under its write lock, avoiding stale selection."""
    required = _names(required_capabilities)
    if requires_browser(required):
        from . import browser_readiness
        if not browser_readiness.has_ready_probe(board, target_session_id):
            return False
    r = board.conn.execute("""SELECT s.*, c.capabilities, c.verified_at, c.expires_at,
        c.attested_agent,c.project AS attested_project,c.worktree AS attested_worktree,a.active
        FROM sessions s JOIN agents a ON a.name=s.agent
        JOIN session_capabilities c ON c.session_id=s.id WHERE s.id=?""", (target_session_id,)).fetchone()
    now = board.now()
    return bool(r and r["active"] and r["project"] == project == r["attested_project"]
                and r["agent"] == r["attested_agent"] and r["worktree"] == r["attested_worktree"]
                and now - LIVE_SECONDS <= r["last_seen"] <= now
                and now - MAX_TTL_SECONDS <= r["verified_at"] <= now < r["expires_at"]
                and set(required).issubset(json.loads(r["capabilities"])))


def route(board, p, session_id, post_id, recipient, required_capabilities, expected_version):
    """Make one bounded assignment attempt; never launch work or retry execution.

    Only original addressees can be selected. The lifecycle owns authorization,
    assignment limits, compare-and-swap, and the final transactional preflight.
    """
    from . import requests, workstreams, browser_readiness
    required = _names(required_capabilities)
    if type(expected_version) is not int or expected_version < 0:
        raise Invalid("expected_version is required for routing")
    board._check_agent_write(p)
    caller = board._session(p, session_id)
    post = board.get_post(p, post_id)  # apply sealed visibility before reading recipients
    thread = board._thread_row(post["thread_id"])
    if caller["project"] != thread["project"]:
        raise Forbidden("routing session must belong to the request project")
    row = next((r for r in post["requests"] if r["recipient"] == recipient), None)
    if row is None:
        raise NotFound("request recipient not found")
    blocker = browser_readiness.request_blocker(board, post_id, recipient)
    if blocker:
        raise Conflict(blocker)
    if (requires_browser(required)
            and browser_readiness.requirement(board, post_id, recipient) is None):
        raise Conflict('bind the exact browser target before routing browser work')
    if workstreams.get_for_post(board, post_id) is not None:
        managed = workstreams.reconcile(board, p, session_id, post_id, expected_version)
        current = next(r for r in board.get_post(p, post_id)['requests'] if r['recipient'] == recipient)
        return {**current, 'continuation': managed}
    if not p.is_human and p.name not in (post["agent"], row["assigned_agent"]):
        raise Forbidden("only the author or assigned recipient may route a request")
    if (not p.is_human and p.name == row["assigned_agent"]
            and row["assigned_session"] not in (None, session_id)):
        raise Conflict("request is owned by another session")
    if row["version"] != expected_version:
        raise Conflict("request changed; reread before routing")
    if not p.is_human and thread["status"] != "open":
        raise Conflict("thread is closed")
    if row["state"] not in ("queued", "blocked"):
        raise Conflict("only queued or blocked requests may be routed")
    # Preserve any existing suitable assignment: repeat routing must not duplicate work.
    candidates = list(board.conn.execute("SELECT id,agent FROM sessions ORDER BY last_seen DESC,id DESC"))
    candidates.sort(key=lambda s: (s["id"] != row["assigned_session"], s["agent"] != recipient))
    source = board.conn.execute("SELECT to_agents FROM posts WHERE id=?", (post_id,)).fetchone()
    allowed = set(json.loads(source["to_agents"])) | {recipient}
    if post["task_id"] is not None:
        task = board.conn.execute("SELECT * FROM tasks WHERE id=?", (post["task_id"],)).fetchone()
        allowed = {agent for agent in allowed
                   if task is not None and board._task_authorizable(task, agent)}
        if not allowed:
            raise Forbidden("no original recipient has active authorization or a matching standing grant for the linked task")
    for candidate in candidates:
        if (candidate["agent"] in allowed and eligible(board, candidate["id"], thread["project"], required)
                and browser_readiness.eligible(board, candidate['id'], post_id, recipient)):
            return requests.assign(board, p, session_id, post_id, recipient, candidate["id"],
                                   expected_version=expected_version,
                                   reason="Capability preflight: " + ", ".join(required),
                                   required_capabilities=required)
    reason = "No eligible live session; request minimal missing access or a fresh successful probe for: " + ", ".join(required)
    result = requests.progress(board, p, session_id, post_id, recipient, "blocked", reason=reason,
                               expected_version=expected_version)
    return {**result, "blocker": "missing_capability", "required_capabilities": required}
