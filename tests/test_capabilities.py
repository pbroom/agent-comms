"""Capability attestations do not grant authority or survive environment changes."""
import pytest

from agent_comms import capabilities
from agent_comms.core import Forbidden, Invalid, Paused
from conftest import PROJECT


def probe(env, name="codex", **kw):
    return capabilities.register(env.board, env.p[name], env.sid[name],
                                 kw.pop("capabilities", ["browser:desktop"]),
                                 kw.pop("evidence", "Opened the project page successfully"), **kw)


def test_attestation_stamps_own_environment_and_no_grants(env):
    result = probe(env)
    assert result["agent"] == "codex"
    assert result["project"] == PROJECT
    assert result["authority"] == "self_reported_probe_not_authorization"
    assert capabilities.eligible(env.board, env.sid["codex"], PROJECT, ["browser:desktop"])
    assert not capabilities.eligible(env.board, env.sid["codex"], PROJECT, ["git:write"])
    assert env.board.conn.execute("SELECT COUNT(*) FROM authorization_grants").fetchone()[0] == 0
    with pytest.raises(Forbidden):
        capabilities.register(env.board, env.p["claude"], env.sid["codex"], ["git:write"], "probe")


@pytest.mark.parametrize("changes", [{"ttl_seconds": 1801}, {"ttl_seconds": float("nan")},
    {"ttl_seconds": True}, {"ttl_seconds": 0}, {"evidence": " "},
    {"capabilities": []}, {"capabilities": [" browser:desktop"]}])
def test_invalid_probes(env, changes):
    with pytest.raises(Invalid):
        probe(env, **changes)


def test_staleness_expiry_inactive_and_environment_binding(env):
    probe(env)
    env.clock.advance(91)
    assert not capabilities.eligible(env.board, env.sid["codex"], PROJECT, ["browser:desktop"])
    env.board.heartbeat(env.p["codex"], env.sid["codex"])
    assert capabilities.eligible(env.board, env.sid["codex"], PROJECT, ["browser:desktop"])
    env.clock.advance(1710)
    env.board.heartbeat(env.p["codex"], env.sid["codex"])
    assert not capabilities.eligible(env.board, env.sid["codex"], PROJECT, ["browser:desktop"])
    probe(env)
    env.board.register_session(env.p["codex"], PROJECT, "/different-worktree", resume_session_id=env.sid["codex"])
    assert not capabilities.eligible(env.board, env.sid["codex"], PROJECT, ["browser:desktop"])
    probe(env)
    assert not capabilities.eligible(env.board, env.sid["codex"], "/other-project", ["browser:desktop"])
    env.board.conn.execute("UPDATE agents SET active=0 WHERE name='codex'")
    assert not capabilities.eligible(env.board, env.sid["codex"], PROJECT, ["browser:desktop"])


def test_pause_prevents_probe_writes(env):
    env.board.set_paused(env.p["human"], True)
    with pytest.raises(Paused):
        probe(env)


def request(env, to=None):
    return env.post("human", env.thread(), type="request", to=to or ["codex"], body="Review this project")


def route(env, post, version=0, as_="codex"):
    return capabilities.route(env.board, env.p[as_], env.sid[as_], post["id"], "codex",
                              ["browser:desktop"], version)


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
