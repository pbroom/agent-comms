"""Capability attestations do not grant authority or survive environment changes."""
import pytest

from agent_comms import capabilities
from agent_comms.core import Forbidden, Invalid, Paused
from conftest import PROJECT


def probe(env, name="codex", **kw):
    return capabilities.register(env.board, env.p[name], env.sid[name],
                                 kw.pop("capabilities", ["files:read"]),
                                 kw.pop("evidence", "Opened the project page successfully"), **kw)


def test_attestation_stamps_own_environment_and_no_grants(env):
    result = probe(env)
    assert result["agent"] == "codex"
    assert result["project"] == PROJECT
    assert result["authority"] == "self_reported_probe_not_authorization"
    assert capabilities.eligible(env.board, env.sid["codex"], PROJECT, ["files:read"])
    assert not capabilities.eligible(env.board, env.sid["codex"], PROJECT, ["git:write"])
    assert env.board.conn.execute("SELECT COUNT(*) FROM authorization_grants").fetchone()[0] == 0
    with pytest.raises(Forbidden):
        capabilities.register(env.board, env.p["claude"], env.sid["codex"], ["git:write"], "probe")


@pytest.mark.parametrize("changes", [{"ttl_seconds": 1801}, {"ttl_seconds": float("nan")},
    {"ttl_seconds": True}, {"ttl_seconds": 0}, {"evidence": " "},
    {"capabilities": []}, {"capabilities": [" files:read"]}])
def test_invalid_probes(env, changes):
    with pytest.raises(Invalid):
        probe(env, **changes)


def test_staleness_expiry_inactive_and_environment_binding(env):
    probe(env)
    env.clock.advance(91)
    assert not capabilities.eligible(env.board, env.sid["codex"], PROJECT, ["files:read"])
    env.board.heartbeat(env.p["codex"], env.sid["codex"])
    assert capabilities.eligible(env.board, env.sid["codex"], PROJECT, ["files:read"])
    env.clock.advance(1710)
    env.board.heartbeat(env.p["codex"], env.sid["codex"])
    assert not capabilities.eligible(env.board, env.sid["codex"], PROJECT, ["files:read"])
    probe(env)
    env.board.register_session(env.p["codex"], PROJECT, "/different-worktree", resume_session_id=env.sid["codex"])
    assert not capabilities.eligible(env.board, env.sid["codex"], PROJECT, ["files:read"])
    probe(env)
    assert not capabilities.eligible(env.board, env.sid["codex"], "/other-project", ["files:read"])
    env.board.conn.execute("UPDATE agents SET active=0 WHERE name='codex'")
    assert not capabilities.eligible(env.board, env.sid["codex"], PROJECT, ["files:read"])


def test_pause_prevents_probe_writes(env):
    env.board.set_paused(env.p["human"], True)
    with pytest.raises(Paused):
        probe(env)


def request(env, to=None):
    return env.post("human", env.thread(), type="request", to=to or ["codex"], body="Review this project")


def route(env, post, version=0, as_="codex"):
    return capabilities.route(env.board, env.p[as_], env.sid[as_], post["id"], "codex",
                              ["files:read"], version)


def test_routing_uses_only_original_identities_and_fresh_environment(env):
    post = request(env)
    probe(env, "claude")
    blocked = route(env, post)
    assert blocked["state"] == "blocked"
    assert blocked["blocker"] == "missing_capability"
    assert blocked["assigned_agent"] == "codex"
    probe(env)
    queued = route(env, post, blocked["version"])
    assert queued["state"] == "queued"
    assert queued["assigned_session"] == env.sid["codex"]
    repeated = route(env, post, queued["version"])
    assert repeated["version"] == queued["version"]


def test_routing_fallback_same_project_original_addressee(env):
    post = request(env, ["codex", "claude"])
    probe(env, "claude")
    queued = route(env, post)
    assert queued["assigned_agent"] == "claude"
    assert queued["assigned_session"] == env.sid["claude"]


def test_failed_preflight_is_idempotent(env):
    post = request(env)
    first = route(env, post)
    second = route(env, post, first["version"])
    assert second["version"] == first["version"]


def test_routing_pause_and_project_boundary(env):
    post = request(env)
    probe(env)
    env.board.register_session(env.p["codex"], "/other", resume_session_id=env.sid["codex"])
    with pytest.raises(Forbidden):
        route(env, post)
    env.board.set_paused(env.p["human"], True)
    with pytest.raises(Paused):
        route(env, post)


def test_no_route_through_sealed_post(env):
    from agent_comms.core import NotFound
    post = env.post("claude", env.thread(), type="request", to=["codex"], sealed=True)
    probe(env)
    with pytest.raises(NotFound):
        route(env, post)


def test_no_parallel_execution_or_unbounded_reassignment(env):
    from agent_comms import requests
    from agent_comms.core import Conflict
    post = request(env)
    probe(env)
    current = route(env, post)
    started = requests.progress(env.board, env.p["codex"], env.sid["codex"], post["id"], "codex",
                                "started", expected_version=current["version"])
    with pytest.raises(Conflict):
        route(env, post, started["version"])
    for i in range(3):
        blocked = requests.progress(env.board, env.p["codex"], env.sid["codex"], post["id"], "codex",
                                    "blocked", reason=f"Probe failed {i}")
        if i < 2:
            current = route(env, post, blocked["version"])
        else:
            with pytest.raises(Conflict, match="limit"):
                route(env, post, blocked["version"])


def test_preflight_rechecked_at_assignment(env, monkeypatch):
    from agent_comms import requests
    from agent_comms.core import Conflict
    post = request(env)
    probe(env)
    original = requests.assign
    def expired(*args, **kwargs):
        env.clock.advance(1801)
        return original(*args, **kwargs)
    monkeypatch.setattr(requests, "assign", expired)
    with pytest.raises(Conflict, match="capabilit"):
        route(env, post)
    assert env.board.conn.execute("SELECT COUNT(*) FROM request_events").fetchone()[0] == 0


def test_author_can_record_failed_preflight(env):
    post = env.post("claude", env.thread(), type="request", to=["codex"])
    blocked = route(env, post, as_="claude")
    assert blocked["state"] == "blocked"
    assert blocked["assigned_session"] is None
    assert blocked["blocker"] == "missing_capability"


def test_capability_does_not_override_task_authorization(env):
    post = env.post("codex", env.thread(), type="proposal", to=["claude"], needs_response=True,
                    propose_task={"title": "Unapproved task"})
    probe(env, "claude")
    with pytest.raises(Forbidden, match="authoriz"):
        capabilities.route(env.board, env.p["codex"], env.sid["codex"], post["id"], "claude",
                           ["files:read"], 0)


def test_route_skips_capable_but_unauthorized_preferred_recipient(env):
    env.board.create_grant(env.p["human"], project=PROJECT, category="review", agents=["claude"],
                           purpose="Review this project")
    post = env.post("grok", env.thread(), type="proposal", to=["codex", "claude"], needs_response=True,
                    propose_task={"title": "Review", "category": "review"})
    env.board.claim_task(env.p["claude"], env.sid["claude"], post["task_id"])
    env.board.release_task(env.p["claude"], env.sid["claude"], post["task_id"])
    probe(env, "codex")
    probe(env, "claude")
    queued = route(env, post, as_="grok")
    assert queued["assigned_agent"] == "claude"
    assert queued["assigned_session"] == env.sid["claude"]


def test_route_honours_a_matching_standing_grant_before_the_task_is_claimed(env):
    from agent_comms import requests
    env.board.create_grant(env.p["human"], project=PROJECT, category="review", agents=["codex"],
                           purpose="Review this project")
    post = env.post("grok", env.thread(), type="proposal", to=["codex"], needs_response=True,
                    propose_task={"title": "Review", "category": "review"})
    probe(env, "codex")
    queued = route(env, post, as_="grok")                      # unclaimed: the grant still authorizes codex
    assert (queued["assigned_agent"], queued["assigned_session"]) == ("codex", env.sid["codex"])
    other = env.post("grok", env.thread(), type="proposal", to=["codex"], needs_response=True,
                     propose_task={"title": "Review 2", "category": "review"})
    out = requests.assign(env.board, env.p["grok"], env.sid["grok"], other["id"], "codex", env.sid["codex"],
                          expected_version=0, reason="route", required_capabilities=["files:read"])
    assert out["assigned_session"] == env.sid["codex"]
    # A grant for another category, or a revoked grant, does not count.
    tests = env.post("grok", env.thread(), type="proposal", to=["codex"], needs_response=True,
                     propose_task={"title": "Tests", "category": "tests"})
    with pytest.raises(Forbidden, match="authoriz"):
        route(env, tests, as_="grok")
    for g in env.board.list_grants(env.p["human"]):
        env.board.revoke_grant(env.p["human"], g["id"])
    third = env.post("grok", env.thread(), type="proposal", to=["codex"], needs_response=True,
                     propose_task={"title": "Review 3", "category": "review"})
    with pytest.raises(Forbidden, match="authoriz"):
        route(env, third, as_="grok")


def test_unrelated_caller_denied_before_candidate_selection(env):
    post = request(env)
    with pytest.raises(Forbidden, match="author or assigned"):
        route(env, post, as_="grok")
    assert env.board.conn.execute("SELECT COUNT(*) FROM request_events").fetchone()[0] == 0


def test_author_cannot_block_started_execution_with_failed_route(env):
    from agent_comms import requests
    from agent_comms.core import Conflict
    post = env.post("claude", env.thread(), type="request", to=["codex"])
    started = requests.progress(env.board, env.p["codex"], env.sid["codex"], post["id"], "codex", "started")
    with pytest.raises(Conflict):
        route(env, post, started["version"], as_="claude")
    current = env.board.get_post(env.p["claude"], post["id"])["requests"][0]
    assert current["state"] == "started"
