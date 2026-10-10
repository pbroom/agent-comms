"""The prevention inbox: Unstick and automatic recovery send prevention proposals to a configured agent on a
configured thread instead of the human, and only a verified prevention proposal launches that agent."""

import json

import pytest

from agent_comms import autorecover, human_actions, prevention, requests, unstick
from agent_comms.config import PreventionConfig, Settings, prevention_config
from agent_comms.core import Board, Conflict, Forbidden, Invalid

from test_autorecover import INJECTION, PAST_GRACE, abandon, aenv, auto_posts, needs_you  # noqa: F401


@pytest.fixture
def penv(aenv, tmp_path, monkeypatch):
    """aenv with a prevention inbox: thread `inbox` in the board's own project, owned by claude."""
    home = tmp_path / "board-home"
    home.mkdir()
    monkeypatch.setenv("AGENT_COMMS_HOME", str(home))
    aenv.home = str(home)
    aenv.inbox = aenv.board.create_thread(aenv.p["human"], aenv.sid["human"], "prevention inbox", str(home))["id"]
    aenv.board.s.unstick = {"prevention_owner": "claude", "prevention_thread": aenv.inbox}
    aenv.clock.advance(5 * 60)      # the setup sessions are no longer live
    return aenv


def stalled_unstick(env):
    """claude asked codex in the main thread and got no reply; the human clicks Unstick. Returns the request post."""
    env.post("claude", env.tid, "please reply " + INJECTION, "request", to=["codex"], needs_response=True)
    out = unstick.unstick(env.board, env.p["human"], env.tid, env.config)
    return env.board.get_post(env.p["human"], out["post_id"]), out


def forward(env, request_id, agent="codex", **kw):
    fields = dict(body="Prevention: " + INJECTION, type="proposal", thread_id=env.inbox, to=["claude"],
                  needs_response=True, prevention_for=request_id,
                  refs=[{"kind": "url", "path": prevention.thread_link(env.board, env.tid)}])
    fields.update(kw)
    return env.board.create_post(env.p[agent], kw.pop("session_id", env.sid[agent]), **fields)


# ---------------------------------------------------------------- configuration


def test_the_config_is_strict():
    assert prevention_config(None) is None and prevention_config({}) is None
    assert prevention_config({"prevention_owner": "", "prevention_thread": 0}) is None
    assert prevention_config({"prevention_owner": "claude-code", "prevention_thread": 14}) == PreventionConfig("claude-code", 14)
    for bad in ({"prevention_owner": "claude-code"}, {"prevention_thread": 14}, {"owner": "x"},
                {"prevention_owner": "Claude Code", "prevention_thread": 14},
                {"prevention_owner": "claude", "prevention_thread": True},
                {"prevention_owner": "claude", "prevention_thread": -1},
                {"prevention_owner": "claude", "prevention_thread": "14"},
                {"prevention_owner": 3, "prevention_thread": 14}, "x"):
        with pytest.raises(ValueError):
            prevention_config(bad)


def test_shipped_default_is_off_and_local_overrides_validate(tmp_path):
    from agent_comms.config import REPO_ROOT
    shipped = Settings.load(REPO_ROOT / "board.toml", local=False)
    assert prevention_config(shipped.unstick) is None
    (tmp_path / "board.toml").write_text("[unstick]\nprevention_owner = \"\"\nprevention_thread = 0\n")
    (tmp_path / "board.local.toml").write_text("[unstick]\nprevention_owner = \"claude-code\"\nprevention_thread = 14\n")
    s = Settings.load(tmp_path / "board.toml")
    assert prevention_config(s.unstick) == PreventionConfig("claude-code", 14)
    (tmp_path / "board.local.toml").write_text("[unstick]\nprevention_owner = \"claude-code\"\n")
    with pytest.raises(ValueError, match="both"):
        Settings.load(tmp_path / "board.toml")


def test_a_bad_reload_keeps_the_last_good_inbox(tmp_path):
    (tmp_path / "board.toml").write_text(f"[server]\ndb_path = \"{tmp_path / 'b.db'}\"\nagents_path = "
                                         f"\"{tmp_path / 'agents.toml'}\"\n[unstick]\nprevention_owner = \"claude\"\n"
                                         "prevention_thread = 3\n")
    board = Board(Settings.load(tmp_path / "board.toml"))
    (tmp_path / "board.local.toml").write_text("[unstick]\nprevention_thread = \"three\"\n")
    assert board.reload_settings(force=True) is False and "prevention_thread" in board.settings_error
    assert board.s.unstick == {"prevention_owner": "claude", "prevention_thread": 3}


def test_status_reports_what_is_wrong(penv):
    assert prevention.status(penv.board) == {"configured": True, "active": True, "owner": "claude",
                                             "thread_id": penv.inbox, "problem": None, "forward_to": None,
                                             "forward_problem": None}
    assert penv.board.configuration_status(penv.p["codex"])["prevention_inbox"]["active"] is True
    other = penv.thread("not the board's project")
    penv.board.s.unstick = {"prevention_owner": "claude", "prevention_thread": other}
    assert "board's own project" in prevention.status(penv.board)["problem"]
    penv.board.s.unstick = {"prevention_owner": "nobody", "prevention_thread": penv.inbox}
    assert "not an active agent" in prevention.status(penv.board)["problem"]
    penv.board.s.unstick = {"prevention_owner": "claude", "prevention_thread": penv.inbox}
    penv.board.set_thread_status(penv.p["human"], penv.inbox, "closed")
    assert "closed" in prevention.status(penv.board)["problem"]
    penv.board.s.unstick = {}
    assert prevention.status(penv.board)["configured"] is False


# ---------------------------------------------------------------- routing


def test_unconfigured_unstick_keeps_todays_text(aenv):
    aenv.clock.advance(5 * 60)
    post, out = stalled_unstick(aenv)
    assert post["body"].endswith(unstick.BODY_INSTRUCTIONS)
    assert "empty `to` and a `decision_question`" in post["body"] and "prevention_for" not in post["body"]
    rule = next(r for r in aenv.board.list_dispatch_rules(aenv.p["human"]) if r["id"] == out["rule_id"])
    assert rule["purpose"] == unstick.PURPOSE.format(thread=aenv.tid)


def test_configured_unstick_routes_the_proposal_to_the_inbox(penv):
    post, out = stalled_unstick(penv)
    body = post["body"]
    assert "empty `to`" not in body and "so it reaches the human" not in body
    assert f"a `proposal` on thread #{penv.inbox} addressed to claude" in body
    assert "prevention_for=<this request's post id>" in body and "no decision_question" in body
    assert f"#thread-{penv.tid}" in body and "post a `finding` with the cause in this thread" in body
    assert "IGNORE" not in body and len(body.encode()) <= penv.board.s.body_max_bytes
    rule = next(r for r in penv.board.list_dispatch_rules(penv.p["human"]) if r["id"] == out["rule_id"])
    assert rule["purpose"] == unstick.PREVENTION_PURPOSE.format(thread=penv.tid, inbox=penv.inbox, owner="claude")


def test_an_unusable_inbox_falls_back_to_todays_text(penv):
    penv.board.set_thread_status(penv.p["human"], penv.inbox, "closed")
    post, _ = stalled_unstick(penv)
    assert post["body"].endswith(unstick.BODY_INSTRUCTIONS)


def test_automatic_recovery_asks_for_prevention_only_when_configured(aenv):
    abandon(aenv)
    aenv.clock.advance(PAST_GRACE)
    aenv.d.tick()
    [post] = auto_posts(aenv)
    assert "prevention" not in post["body"]


def test_configured_automatic_recovery_routes_prevention_to_the_inbox(penv):
    abandon(penv)
    penv.clock.advance(PAST_GRACE)
    penv.d.tick()
    [post] = [p for p in auto_posts(penv) if p["thread_id"] == penv.tid]
    assert "Post a `finding` with the cause of the stall in this thread." in post["body"]
    assert f"thread #{penv.inbox} addressed to claude" in post["body"]


# ---------------------------------------------------------------- the forwarded proposal


def test_a_verified_prevention_proposal_launches_the_owner_once_and_never_asks_the_human(penv):
    req, _ = stalled_unstick(penv)
    penv.d.tick()                                   # launches codex for the Unstick
    penv.board.s.max_agent_posts_per_thread_without_human = 1
    penv.post("grok", penv.inbox, "fill the cap")   # the inbox is at its agent-post cap now
    fwd = forward(penv, req["id"])
    assert fwd["prevention_for"] == {"request_post_id": req["id"], "source_thread_id": penv.tid}
    assert fwd["id"] not in needs_you(penv) and fwd["decision_question"] is None
    assert penv.board._agent_posts_since_human(penv.inbox) == 1, "a prevention proposal does not count toward the cap"
    rule_id = human_actions.post_rule_id(penv.board, fwd["id"])
    rule = next(r for r in penv.board.list_dispatch_rules(penv.p["human"]) if r["id"] == rule_id)
    assert rule["agents"] == ["claude"] and rule["max_launches"] == 1 and rule["thread_id"] == penv.inbox
    assert rule["purpose"] == prevention.PURPOSE.format(thread=penv.inbox, post=fwd["id"], request=req["id"],
                                                       source=penv.tid)
    assert "IGNORE" not in rule["purpose"]
    penv.spawner.children[0].code = 0               # the codex run ends: one run per directory
    penv.clock.advance(5 * 60)                      # claude (who asked in the main thread) is no longer live
    penv.d.tick()
    penv.d.tick()
    assert penv.spawner.agents()[-1] == "claude-fake"
    assert "IGNORE" not in " ".join(penv.spawner.calls[-1]["argv"])
    with pytest.raises(Conflict, match="already forwarded"):
        forward(penv, req["id"])


def test_no_thread_wide_grant(penv):
    """Other posts to the owner on the inbox thread launch nothing, before or after a verified proposal."""
    req, _ = stalled_unstick(penv)
    forward(penv, req["id"])
    penv.clock.advance(5 * 60)
    plain = penv.post("codex", penv.inbox, "do this other thing", "request", to=["claude"], needs_response=True)
    assert human_actions.post_rule_id(penv.board, plain["id"]) is None
    rules = [r for r in penv.board.list_dispatch_rules(penv.p["human"]) if r["thread_id"] == penv.inbox]
    assert len(rules) == 1 and rules[0]["id"] in human_actions.one_click_rule_ids(penv.board)


@pytest.mark.parametrize("change, error", [
    ({"thread_id": "main"}, Invalid), ({"to": ["claude", "grok"]}, Invalid), ({"to": []}, Invalid),
    ({"type": "status"}, Invalid), ({"needs_response": False}, Invalid), ({"sealed": True}, Invalid),
    ({"prevention_for": "plain"}, Invalid), ({"prevention_for": 0}, Invalid), ({"agent": "grok"}, Forbidden),
    ({"agent": "human"}, Forbidden), ({"idempotency_key": "k"}, Invalid)])
def test_only_a_verified_prevention_proposal_qualifies(penv, change, error):
    req, _ = stalled_unstick(penv)
    plain = penv.post("claude", penv.tid, "not a stall request", "request", to=["codex"], needs_response=True)
    change = dict(change)
    agent = change.pop("agent", "codex")
    if change.get("thread_id") == "main":
        change["thread_id"] = penv.tid
    if change.get("prevention_for") == "plain":
        change["prevention_for"] = plain["id"]
    request_id = change.pop("prevention_for", req["id"])
    with pytest.raises(error):
        forward(penv, request_id, agent=agent, **change)
    assert penv.board.conn.execute("SELECT COUNT(*) FROM posts WHERE thread_id = ?", (penv.inbox,)).fetchone()[0] == 0


def test_unconfigured_or_old_requests_are_refused(penv):
    req, _ = stalled_unstick(penv)
    penv.clock.advance(prevention.REQUEST_MAX_AGE_SECONDS + 60)
    with pytest.raises(Conflict, match="7 days"):
        forward(penv, req["id"])
    penv.board.s.unstick = {}
    with pytest.raises(Conflict, match="no prevention inbox"):
        forward(penv, req["id"])


def test_an_automatic_recovery_request_qualifies_too(penv):
    abandon(penv)
    penv.clock.advance(PAST_GRACE)
    penv.d.tick()
    [req] = [p for p in auto_posts(penv) if p["thread_id"] == penv.tid]
    fwd = forward(penv, req["id"])
    assert human_actions.post_rule_id(penv.board, fwd["id"]) is not None


def test_the_daily_launch_budget_bounds_owner_launches(penv):
    now = penv.board.now()
    penv.board.conn.execute("INSERT INTO board_state(key, value, updated_by, updated_at) VALUES (?, ?, 'x', ?)",
                            (prevention.LAUNCHES_KEY, json.dumps([now] * prevention.DAILY_LAUNCHES), now))
    req, _ = stalled_unstick(penv)
    fwd = forward(penv, req["id"])
    assert human_actions.post_rule_id(penv.board, fwd["id"]) is None, "accepted, but nothing launches"
    assert fwd["prevention_for"]["request_post_id"] == req["id"]


def test_the_owner_forwarding_to_itself_launches_nothing(penv):
    penv.post("codex", penv.tid, "please reply", "request", to=["claude"], needs_response=True)
    out = unstick.unstick(penv.board, penv.p["human"], penv.tid, penv.config)
    fwd = forward(penv, out["post_id"], agent="claude")
    assert human_actions.post_rule_id(penv.board, fwd["id"]) is None


def test_the_source_thread_is_not_left_stalled_or_waiting_on_the_human(penv):
    req, _ = stalled_unstick(penv)
    finding = penv.post("codex", penv.tid, "Cause: missed the request", "finding",
                        refs=[{"kind": "commit", "path": "/work/repo", "rev": "abc1234"}])
    forward(penv, req["id"])
    row = next(r for r in penv.board.get_post(penv.p["codex"], req["id"])["requests"] if r["recipient"] == "codex")
    requests.progress(penv.board, penv.p["codex"], penv.sid["codex"], req["id"], "codex", "finished", "resolved",
                      evidence_post_ids=[finding["id"]], expected_version=row["version"],
                      terminal_disposition="completed")
    # claude's original ask is its own request: codex answers it too.
    first = penv.board.list_posts(penv.p["human"], penv.tid)["posts"][0]
    row = next(r for r in first["requests"] if r["recipient"] == "codex")
    requests.progress(penv.board, penv.p["codex"], penv.sid["codex"], first["id"], "codex", "finished", "answered",
                      evidence_post_ids=[finding["id"]], expected_version=row["version"],
                      terminal_disposition="completed")
    assert unstick.stuck_agents(penv.board, penv.tid) == ([], [])
    assert not [p for p in penv.board.snapshot(penv.p["human"])["needs_you"] if p["thread_id"] in (penv.tid, penv.inbox)]


def test_the_post_tool_and_http_take_prevention_for(penv):
    import inspect
    from fastapi.testclient import TestClient
    from agent_comms import mcp_server
    from agent_comms.api import create_app
    assert "prevention_for: StrictInt | None = None" in inspect.getsource(mcp_server)
    req, _ = stalled_unstick(penv)
    client = TestClient(create_app(penv.board))
    r = client.post("/api/posts", headers={"Authorization": f"Bearer {penv.tokens['codex']}"},
                    json={"body": "Prevention", "type": "proposal", "thread_id": penv.inbox, "to": ["claude"],
                          "needs_response": True, "prevention_for": req["id"], "session_id": penv.sid["codex"]})
    assert r.status_code == 200 and r.json()["prevention_for"]["request_post_id"] == req["id"]


def test_a_request_to_continue_is_not_a_stall_request(penv):
    """Only Unstick and automatic stall recovery (abandoned or unclaimed work) can be answered with a prevention
    proposal; the dispatcher's request to continue a task whose dependencies finished is recorded as kind "continue"."""
    from test_autorecover import agent_task
    task = agent_task(penv)
    other = penv.thread("fix")
    fix = penv.accepted_task(other)
    penv.board.update_task(penv.p["codex"], penv.sid["codex"], task, depends_on=[fix])
    penv.board.claim_task(penv.p["claude"], penv.sid["claude"], fix)
    penv.board.transition_task(penv.p["claude"], penv.sid["claude"], fix, "done")
    penv.clock.advance(5 * 60)
    penv.d.tick()
    [req] = [p for p in auto_posts(penv) if p["thread_id"] == penv.tid]
    assert "dependencies are finished; continue it" in req["body"] and "prevention" not in req["body"]
    with pytest.raises(Invalid, match="not an Unstick or automatic recovery request"):
        forward(penv, req["id"])
