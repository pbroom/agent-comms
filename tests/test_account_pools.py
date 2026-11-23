"""Account pools (account_pools.py, DESIGN_NOTES "Account pools"): a pool name for interchangeable agents, per-project
rules, headroom from what nodes report, and every creation path (an agent's spawn, the human's start and start
--parent). The settings and their refusals, the reasons the hub records, capabilities and the spawn frame, the audit,
the providers' usage parsing, the node's report and back-compat (no pools: nothing changes; an older node or hub).
Temp directories and fakes only: never a real agent CLI or account folder."""

import json
from pathlib import Path

import httpx
import pytest

from agent_comms import account_pools, cli, db, dispatch, hosted, hosted_spawn as hs, nodes
from agent_comms.config import create_agent
from agent_comms.core import Conflict, NotFound
from agent_comms.dispatch import DispatchConfig
from agent_comms.providers import claude as claude_provider, codex as codex_provider

from conftest import PROJECT
from test_hosted_sessions import P2, P3, session_env, show, start, tick
from test_hosted_spawn import bound, grant, interactive, kid_turns, posts_in, tree_rec
from test_hosted_tree import SHIPPED, answer, child, proc_of, root, stored, tree_config

WORK = "claude-work"
POOL = "claudes"
HUB = {"nodes": {"laptop": {"projects": [PROJECT, P2, P3], "agents": ["claude", "codex", WORK]}},
       "sessions": {"spawn": {"enabled": True, "pools": {POOL: [WORK, "claude"]}}}}


@pytest.fixture
def penv(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    env = session_env(tmp_path, dispatch=json.loads(json.dumps(HUB)),
                      config=lambda e: tree_config(e, runners={WORK: SHIPPED}, sessions={"max_live": 12}))
    env.tokens[WORK] = create_agent(env.settings.agents_path, WORK, "claude-code")
    env.p[WORK] = env.board.authenticate(env.tokens[WORK])
    return env


def spawn_cfg(env, **over) -> None:
    """Change the hub's [dispatch.sessions.spawn] (read at every decision)."""
    d = json.loads(json.dumps(env.board.s.dispatch))
    d["sessions"]["spawn"] |= over
    env.board.s.dispatch = d


def seen(env, agent, percent=None, resets_in=None, exhausted=False, age=0.0, window="five_hour", node="laptop"):
    """What `node` reported about the account of `agent`'s runner there (as its hello's `usage` would)."""
    info = nodes._get(env.board.conn, nodes.NODE_PREFIX + node)
    runtime = env.board.conn.execute("SELECT runtime FROM agents WHERE name = ?", (agent,)).fetchone()[0]
    key = nodes.runner_key(info, agent, runtime)
    now = env.board.now()
    obs = {"percent": percent, "resets_at": now + resets_in if resets_in is not None else None, "age": age,
           "exhausted": exhausted, "window": window}
    with db.write_tx(env.board.conn) as c:
        account_pools.store_usage(c, node, account_pools.clean_report({key: [obs]}, info["runners"]), info["runners"],
                                  now, account_pools._spawn(env.board).pool_headroom)


def kept(env, key, window="five_hour", node="laptop") -> dict:
    return account_pools.usage_view(env.board.conn, node).get(key, {}).get(window, {})


def pooled(env, sid) -> dict:
    return stored(env, sid).get("pool")


def a_parent(env, project=PROJECT, agent="claude") -> str:
    sid = root(env, agent=agent, project=project)
    answer(env, sid)
    return sid


# ------------------------------------------------------------ settings


def cfg_of(spawn: dict) -> DispatchConfig:
    return DispatchConfig.from_dict({"sessions": {"spawn": spawn}})


def test_the_settings_are_parsed_strictly():
    c = cfg_of({"pools": {POOL: [WORK, "claude"]}, "pool_rules": {"/w/site/": {"prefer": ["claude"]}},
                "pool_headroom": {"skip_at_percent": 80}}).sessions.spawn
    assert c.pools == {POOL: [WORK, "claude"]} and c.pool_rules == {"/w/site": {"prefer": ["claude"]}}
    assert c.pool_headroom == {"skip_at_percent": 80, "stale_minutes": 60, "cooldown_minutes": 30}
    assert cfg_of({}).sessions.spawn.pools == {} and cfg_of({}).sessions.spawn.pool_headroom["skip_at_percent"] == 90
    for bad, why in (
            ({"pools": {POOL: []}}, "1 to 10 distinct agent names"),
            ({"pools": {POOL: [WORK, WORK]}}, "distinct"),
            ({"pools": {"Bad Name": [WORK]}}, "written like an agent name"),
            ({"pools": {POOL: ["Not-An-Agent!"]}}, "agent names"),
            ({"pools": {POOL: [WORK], WORK: ["claude"]}}, "both a pool and a member"),
            ({"pools": ["x"]}, "must be a table"),
            ({"pools": {POOL: [WORK]}, "pool_rules": {"relative": {"only": [WORK]}}}, "absolute, normalized"),
            ({"pools": {POOL: [WORK]}, "pool_rules": {"/a/../b": {"only": [WORK]}}}, "absolute, normalized"),
            ({"pools": {POOL: [WORK]}, "pool_rules": {"/p": {"sometimes": [WORK]}}}, "prefer, only and/or never"),
            ({"pools": {POOL: [WORK]}, "pool_rules": {"/p": {}}}, "prefer, only and/or never"),
            ({"pools": {POOL: [WORK]}, "pool_rules": {"/p": {"only": ["claude"]}}}, "not a member of any pool"),
            ({"pools": {POOL: [WORK]}, "pool_rules": {"/p": {"only": [WORK]}, "/p/": {"only": [WORK]}}},
             "given twice"),
            ({"pool_headroom": {"skip_at_percent": 0}}, "from 1 to 100"),
            ({"pool_headroom": {"skip_at_percent": True}}, "from 1 to 100"),
            ({"pool_headroom": {"stale_minutes": float("nan")}}, "a number"),
            ({"pool_headroom": {"often": 1}}, "unknown setting")):
        with pytest.raises(ValueError, match=why):
            cfg_of(bad)


def test_a_contradictory_rule_loads_and_never_wins():
    c = cfg_of({"pools": {POOL: [WORK, "claude"]}, "pool_rules": {
        "/a": {"only": [WORK, "claude"], "never": [WORK]}, "/b": {"prefer": [WORK], "never": [WORK]}}}).sessions.spawn
    assert account_pools.ordered(c, POOL, "/a") == (["claude"], ["only claude-work, claude", "never claude-work"])
    assert account_pools.ordered(c, POOL, "/b")[0] == ["claude"]


def test_no_pools_is_the_default_and_nothing_names_one(penv):
    spawn_cfg(penv, pools={})
    assert account_pools.pool_for(penv.board, POOL) is None and not account_pools.configured(penv.board)
    r = penv.human.post("/api/hosted-sessions", json={"project": PROJECT, "message": "x", "agent": POOL})
    assert r.status_code == 404 and "no agent named 'claudes'" in r.json()["message"], "an unknown name, as on main"


# ------------------------------------------------------------ the human's start: a root


def test_a_root_asked_for_by_the_pool_is_its_first_member_and_says_so(penv):
    s = start(penv, agent=POOL)
    assert s["agent"] == WORK and s["pool"] == {"name": POOL, "why": f"{WORK} from pool {POOL}: first choice"}
    assert stored(penv, s["session_id"])["agent"] == WORK and pooled(penv, s["session_id"])["name"] == POOL
    tick(penv)
    assert proc_of(penv, s["session_id"])[0]["argv"][0] == "claude", "the member's own runner starts it"
    detail = cli._fmt_hosted_detail(show(penv, s["session_id"]))
    assert f"asked for as pool {POOL}; the hub chose {WORK} from pool {POOL}: first choice" in detail
    assert "cannot verify which subscription" in detail
    trees = penv.human.get("/api/hosted-trees").json()
    assert {t["session"]["agent"] for t in trees["trees"]} == {WORK}, "the lineage view names the real agent"


def test_a_member_at_95_percent_is_skipped_for_the_next(penv):
    seen(penv, WORK, percent=95, resets_in=3600)
    s = start(penv, agent=POOL)
    assert s["agent"] == "claude"
    why = s["pool"]["why"]
    assert why.startswith(f"claude from pool {POOL}: overflow, because {WORK} was at 95% of its five_hour window "
                          "until ") and "(skipped at 90% or when exhausted)" in why, why


def test_below_the_threshold_the_first_member_keeps_the_work(penv):
    seen(penv, WORK, percent=89.9, resets_in=3600)
    assert start(penv, agent=POOL)["agent"] == WORK


def test_at_exactly_the_threshold_a_member_is_skipped(penv):
    seen(penv, WORK, percent=90)
    assert start(penv, agent=POOL)["agent"] == "claude"


def test_usage_is_kept_for_the_runner_the_agent_runs_with(penv):
    """The agent `claude` has no runner of its own name on the node: it runs with its runtime's, `claude-code`, and
    what its sessions see is that runner's (the one `choose` looks up for it)."""
    sid = start(penv, agent="claude")["session_id"]
    tick(penv)
    _, proc = proc_of(penv, sid)
    proc.emit({"type": "rate_limit_event", "session_id": sid,
               "rate_limit_info": {"status": "allowed", "utilization": 0.99, "rateLimitType": "five_hour"}})
    tick(penv)
    assert set(penv.node.sessions.host.usage) == {"claude-code"}
    penv.node.hello()
    penv.node.hello()
    spawn_cfg(penv, pools={POOL: ["claude", WORK]})
    assert start(penv, agent=POOL, project=P2)["agent"] == WORK


def test_the_threshold_is_a_setting(penv):
    spawn_cfg(penv, pool_headroom={"skip_at_percent": 50})
    seen(penv, WORK, percent=55)
    s = start(penv, agent=POOL)
    assert s["agent"] == "claude" and "(skipped at 50% or when exhausted)" in s["pool"]["why"]


def test_unknown_usage_counts_as_room(penv):
    seen(penv, WORK, percent=None)      # a report with no percent: unknown
    assert start(penv, agent=POOL)["agent"] == WORK


def test_stale_usage_is_ignored_after_its_age(penv):
    seen(penv, WORK, percent=99, age=61 * 60)       # seen 61 minutes ago; stale_minutes is 60
    assert start(penv, agent=POOL)["agent"] == WORK
    spawn_cfg(penv, pool_headroom={"stale_minutes": 120})
    seen(penv, WORK, percent=99, age=61 * 60)
    assert start(penv, agent=POOL, project=P2)["agent"] == "claude", "within a longer age it counts"


def test_usage_whose_window_has_reset_counts_as_room(penv):
    seen(penv, WORK, percent=99, resets_in=60)
    penv.clock.advance(61)
    penv.node.hello()
    assert start(penv, agent=POOL)["agent"] == WORK


def test_an_exhausted_member_is_skipped_until_its_reset_then_has_room(penv):
    seen(penv, WORK, exhausted=True, resets_in=4 * 3600, age=0)
    s = start(penv, agent=POOL)
    assert s["agent"] == "claude" and f"{WORK} hit its five_hour limit (until " in s["pool"]["why"], s["pool"]["why"]
    assert "cool-down" not in s["pool"]["why"]
    penv.clock.advance(3 * 3600)        # still before the reset (even though stale by age)
    penv.node.hello()
    assert start(penv, agent=POOL, project=P2)["agent"] == "claude"
    penv.clock.advance(3600 + 1)
    penv.node.hello()
    assert start(penv, agent=POOL, project=P3)["agent"] == WORK, "past its reset"


def test_an_exhausted_member_without_a_reset_time_waits_out_the_cooldown(penv):
    seen(penv, WORK, exhausted=True)
    s = start(penv, agent=POOL)
    assert s["agent"] == "claude" and "a cool-down" in s["pool"]["why"]
    penv.clock.advance(31 * 60)
    penv.node.hello()
    assert start(penv, agent=POOL, project=P2)["agent"] == WORK
    spawn_cfg(penv, pool_headroom={"cooldown_minutes": 120})
    seen(penv, WORK, exhausted=True)
    penv.clock.advance(31 * 60)
    penv.node.hello()
    assert start(penv, agent=POOL, project=P3)["agent"] == "claude", "a longer cool-down, kept for as long"


def test_a_reset_time_past_the_longest_window_is_not_believed(penv):
    seen(penv, WORK, exhausted=True, resets_in=30 * 86400)
    penv.clock.advance(31 * 60)
    penv.node.hello()
    assert start(penv, agent=POOL)["agent"] == WORK, "the cool-down, not a month"


def test_when_every_member_is_near_its_limit_the_first_is_taken_anyway(penv):
    seen(penv, WORK, percent=97, resets_in=600)
    seen(penv, "claude", exhausted=True, resets_in=600)
    s = start(penv, agent=POOL)
    assert s["agent"] == WORK
    why = s["pool"]["why"]
    assert "every member that can be started is near its limit" in why and "headroom never refuses work" in why
    assert f"{WORK} was at 97%" in why and "claude hit its five_hour limit" in why


def test_an_ineligible_member_is_passed_over_with_its_reason_and_usage_never_makes_it_eligible(penv):
    spawn_cfg(penv)
    penv.board.s.dispatch = {**penv.board.s.dispatch, "agent_projects": {WORK: [P2]}}
    seen(penv, "claude", percent=99)        # near its limit, but the only one that may run here
    s = start(penv, agent=POOL)
    assert s["agent"] == "claude"
    assert f"{WORK} cannot be started here ({WORK} is not opted in to {PROJECT}" in s["pool"]["why"]
    assert "every member that can be started is near its limit" in s["pool"]["why"]
    penv.board.s.dispatch = {**penv.board.s.dispatch, "agent_projects": {WORK: [P2], "claude": [P2]}}
    r = penv.human.post("/api/hosted-sessions", json={"project": P3, "message": "x", "agent": POOL})
    assert r.status_code == 409 and r.json()["message"].startswith(f"no member of pool {POOL} can be started in {P3}")
    assert "claude is not opted in" in r.json()["message"] and f"{WORK} is not opted in" in r.json()["message"]


def test_a_member_whose_node_is_not_granted_is_passed_over(penv):
    penv.board.s.dispatch = {**penv.board.s.dispatch, "nodes": {"laptop": {"projects": [PROJECT], "agents": ["claude"]}}}
    r = penv.human.post("/api/hosted-sessions", json={"project": PROJECT, "message": "x", "agent": POOL})
    assert r.status_code == 200, r.text
    assert r.json()["session"]["agent"] == "claude" and f"no node may run {WORK}" in r.json()["session"]["pool"]["why"]


def test_the_decision_is_made_in_the_write_that_creates_the_session(penv, monkeypatch):
    real, calls = account_pools.choose, []

    def spy(board, conn, *a, **kw):
        calls.append(conn.in_transaction)
        return real(board, conn, *a, **kw)
    monkeypatch.setattr(account_pools, "choose", spy)
    start(penv, agent=POOL)
    assert calls == [True]


def test_a_pool_whose_name_is_an_agents_means_that_agent(penv):
    spawn_cfg(penv, pools={"claude": [WORK, "codex"]})
    s = start(penv, agent="claude")
    assert s["agent"] == "claude" and "pool" not in s, "the agent, exactly as before"
    assert account_pools.problems(penv.board) == [
        f"pool claude has the name of an agent, so claude means that agent; rename the pool in {account_pools.POOLS}"]


def test_a_member_that_is_not_an_agent_is_passed_over_and_a_pool_mixing_runtimes_is_refused(penv):
    spawn_cfg(penv, pools={POOL: ["ghost", WORK]})
    s = start(penv, agent=POOL)
    assert s["agent"] == WORK and "ghost cannot be started here (ghost is not an active agent on this board)" in \
        s["pool"]["why"]
    spawn_cfg(penv, pools={POOL: ["ghost", "claude"]}, pool_rules={P2: {"only": ["claude"]}})
    s = start(penv, agent=POOL, project=P2)
    assert s["agent"] == "claude" and "ghost" not in s["pool"]["why"], "the rule first: ghost is not even tried"
    spawn_cfg(penv, pools={POOL: ["ghost"]}, pool_rules={})
    r = penv.human.post("/api/hosted-sessions", json={"project": P3, "message": "x", "agent": POOL})
    assert r.status_code == 409 and "ghost cannot be started here (ghost is not an active agent" in r.json()["message"]
    with db.write_tx(penv.board.conn) as c:
        c.execute("DELETE FROM board_state WHERE key LIKE 'host.session.%'")
    spawn_cfg(penv, pools={POOL: [WORK, "codex"]})
    r = penv.human.post("/api/hosted-sessions", json={"project": PROJECT, "message": "x", "agent": POOL})
    assert r.status_code == 409 and "mixes runtimes (claude, codex)" in r.json()["message"]
    assert hosted._sessions(penv.board.conn) == []


def test_the_one_root_per_agent_rule_still_applies_to_the_chosen_member(penv):
    start(penv, agent=WORK)
    r = penv.human.post("/api/hosted-sessions", json={"project": PROJECT, "message": "x", "agent": POOL})
    assert r.status_code == 409 and f"{WORK} already has a hosted session in {PROJECT}" in r.json()["message"]
    assert r.json()["message"].endswith(f"({WORK} was the member chosen from pool {POOL}: {WORK} from pool {POOL}: "
                                        "first choice)"), "a refusal that says which member was chosen, not a move on"
    r = penv.human.post("/api/hosted-sessions", json={"project": PROJECT, "message": "x", "agent": WORK})
    assert r.status_code == 409 and "chosen from pool" not in r.json()["message"], "a named agent: as before"


def test_a_pool_root_start_works_with_child_sessions_off(penv):
    spawn_cfg(penv, enabled=False)
    seen(penv, WORK, percent=95)
    s = start(penv, agent=POOL)
    assert s["agent"] == "claude" and "overflow" in s["pool"]["why"]


# ------------------------------------------------------------ project rules


def test_prefer_puts_a_member_first_in_that_project_only(penv):
    spawn_cfg(penv, pool_rules={P2: {"prefer": ["claude"]}})
    assert start(penv, agent=POOL)["agent"] == WORK, "elsewhere, the pool's own order"
    s = start(penv, agent=POOL, project=P2)
    assert s["agent"] == "claude" and s["pool"]["why"] == (
        f"claude from pool {POOL}: by the project rule for {P2} (prefer claude)")
    seen(penv, "claude", percent=96)
    spawn_cfg(penv, pool_rules={P3: {"prefer": ["claude"]}})
    s = start(penv, agent=POOL, project=P3)
    assert s["agent"] == WORK and "overflow, because claude was at 96%" in s["pool"]["why"], \
        "the preferred member overflows to the rest of the pool"


def test_only_cuts_the_pool_and_refuses_when_the_one_left_cannot_start(penv):
    spawn_cfg(penv, pool_rules={PROJECT: {"only": [WORK]}})
    seen(penv, WORK, percent=99)
    s = start(penv, agent=POOL)
    assert s["agent"] == WORK and "only claude-work" in s["pool"]["why"], "never the other, even near the limit"
    assert "headroom never refuses work" in s["pool"]["why"]
    penv.board.s.dispatch = {**penv.board.s.dispatch, "agent_projects": {WORK: [P3]}}
    r = penv.human.post("/api/hosted-sessions", json={"project": PROJECT, "message": "x", "agent": POOL})
    assert r.status_code == 409 and r.json()["message"].startswith(f"no member of pool {POOL} can be started in")


def test_never_takes_a_member_out(penv):
    spawn_cfg(penv, pool_rules={PROJECT: {"never": [WORK]}})
    s = start(penv, agent=POOL)
    assert s["agent"] == "claude" and f"never {WORK}" in s["pool"]["why"]


def test_a_rule_that_leaves_no_member_refuses_with_the_reason(penv):
    spawn_cfg(penv, pool_rules={PROJECT: {"never": [WORK, "claude"]}})
    r = penv.human.post("/api/hosted-sessions", json={"project": PROJECT, "message": "x", "agent": POOL})
    assert r.status_code == 409
    assert r.json()["message"] == (f"no member of pool {POOL} may be started in {PROJECT}: the project rule for "
                                   f"{PROJECT} (never {WORK}, claude) in {account_pools.RULES} leaves none")


def test_an_only_naming_no_member_of_this_pool_does_not_apply_to_it(penv):
    spawn_cfg(penv, pools={POOL: [WORK, "claude"], "other": ["codex"]}, pool_rules={PROJECT: {"only": ["codex"]}})
    assert start(penv, agent=POOL)["agent"] == WORK
    assert account_pools.ordered(account_pools._spawn(penv.board), "other", PROJECT) == (["codex"], ["only codex"])


def test_rules_compare_projects_by_the_boards_own_string(penv):
    spawn_cfg(penv, pool_rules={PROJECT + "/": {"never": [WORK]}})
    assert start(penv, agent=POOL)["agent"] == "claude", "a trailing slash aside, the same project"
    spawn_cfg(penv, pool_rules={PROJECT.upper(): {"never": [WORK]}})
    assert start(penv, agent=POOL, project=P2)["agent"] == WORK, "another spelling is another project"


# ------------------------------------------------------------ the human's start --parent


def test_a_child_asked_for_by_the_pool_overflows_and_records_how(penv):
    parent = a_parent(penv)
    first = child(penv, parent, agent=POOL, access="read-only")
    assert first["agent"] == WORK and first["pool"]["why"] == f"{WORK} from pool {POOL}: first choice"
    seen(penv, WORK, percent=95)
    second = child(penv, parent, agent=POOL, access="read-only")
    assert second["agent"] == "claude" and "overflow, because claude-work was at 95%" in second["pool"]["why"]
    tick(penv)
    assert stored(penv, second["session_id"])["pool"]["name"] == POOL
    out = show(penv, parent)
    assert {k["agent"] for k in out["children"]} == {WORK, "claude"}


def test_a_childs_request_id_returns_the_same_child_whatever_the_usage_now(penv):
    parent = a_parent(penv)
    a = child(penv, parent, agent=POOL, request_id="r1", access="read-only")
    seen(penv, WORK, percent=99)
    r = penv.human.post(f"/api/hosted-sessions/{parent}/children",
                        json={"message": "go", "agent": POOL, "request_id": "r1", "access": "read-only"})
    assert r.status_code == 200 and r.json()["created"] is False and r.json()["session"]["session_id"] == a["session_id"]
    penv.board.s.dispatch = {**penv.board.s.dispatch, "agent_projects": {WORK: [P2], "claude": [P2]}}
    r = penv.human.post(f"/api/hosted-sessions/{parent}/children",
                        json={"message": "go", "agent": POOL, "request_id": "r1", "access": "read-only"})
    assert r.status_code == 200 and r.json()["session"]["session_id"] == a["session_id"], \
        "found before any choice: a pool that could start no member now still returns it"


def test_a_child_member_that_cannot_run_at_the_access_asked_is_passed_over(penv):
    parent = a_parent(penv)
    info = nodes._get(penv.board.conn, nodes.NODE_PREFIX + "laptop")
    info["session_host"]["runners"][WORK] = ["read-only"]
    with db.write_tx(penv.board.conn) as c:
        nodes._put(c, nodes.NODE_PREFIX + "laptop", info, "laptop", penv.board.now())
    out = child(penv, parent, agent=POOL, access="workspace")
    assert out["agent"] == "claude" and f"cannot start a session of {WORK} at workspace access" in out["pool"]["why"]


def test_with_child_sessions_off_a_pool_child_is_refused_as_any_child(penv):
    spawn_cfg(penv, enabled=False)
    parent = root(penv, agent="claude")
    out = child(penv, parent, agent=POOL, expect=409)
    assert "child sessions are off" in out["message"]
    penv.board.s.dispatch = {**penv.board.s.dispatch, "agent_projects": {WORK: [P2], "claude": [P2]}}
    out = child(penv, parent, agent=POOL, expect=409)
    assert out["message"].startswith("child sessions are off"), "never a pool's reason: off is off"


# ------------------------------------------------------------ an agent's spawn


def test_an_agents_spawn_of_the_pool_gets_a_member_and_the_audit_says_why(penv):
    parent = a_parent(penv)
    grant(penv, agents=("claude",))
    out = hs.spawn(penv.board, penv.p["claude"], bound(penv, parent), task="look", agent=POOL, access="read-only")
    assert out["child"]["agent"] == WORK and out["child"]["pool"] == POOL
    seen(penv, WORK, percent=95, resets_in=600)
    nxt = hs.spawn(penv.board, penv.p["claude"], bound(penv, parent), task="look", agent=POOL, access="read-only")
    assert nxt["child"]["agent"] == "claude"
    thread = tree_rec(penv, parent)["thread"]
    bodies = [p["body"] for p in posts_in(penv, thread)]
    assert f"agent {WORK}" in bodies[0] and (f"Its agent was asked for as pool {POOL} and chosen by the hub (first "
                                             "choice); `board session show` gives the full reason.") in bodies[0]
    assert "(overflow past 1 member(s) near the limit)" in bodies[1] and "agent claude," in bodies[1]
    assert "was at 95%" not in bodies[1], "the post has the summary; the full reason is the record's"
    assert "overflow, because claude-work was at 95%" in stored(penv, nxt["child"]["id"])["pool"]["why"]
    assert "the board cannot verify which subscription a process used" in bodies[1]
    budget = hs._budget(penv.board.conn, "claude", PROJECT)
    assert len(budget["spawned"]) == 2, "charged to the caller's grant, as ever"
    assert hs._budget(penv.board.conn, WORK, PROJECT).get("spawned") in (None, []), "never to the member's"


def test_an_agents_spawn_passes_over_a_member_the_targets_list_bars(penv):
    parent = a_parent(penv)
    grant(penv, agents=("claude",))
    spawn_cfg(penv, targets=["claude"])
    out = hs.spawn(penv.board, penv.p["claude"], bound(penv, parent), task="t", agent=POOL, access="read-only")
    assert out["child"]["agent"] == "claude"
    assert f"{WORK} cannot be started here ({WORK} is not among the targets" in stored(penv, out["child"]["id"])[
        "pool"]["why"]


def test_an_agents_spawn_passes_over_a_member_its_node_cannot_start_at_the_access_asked(penv):
    parent = a_parent(penv)
    grant(penv, agents=("claude",))
    info = nodes._get(penv.board.conn, nodes.NODE_PREFIX + "laptop")
    info["session_host"]["runners"][WORK] = ["read-only"]
    with db.write_tx(penv.board.conn) as c:
        nodes._put(c, nodes.NODE_PREFIX + "laptop", info, "laptop", penv.board.now())
    out = hs.spawn(penv.board, penv.p["claude"], bound(penv, parent), task="t", agent=POOL, access="workspace")
    assert out["child"]["agent"] == "claude"
    assert f"cannot start a session of {WORK} at workspace access" in stored(penv, out["child"]["id"])["pool"]["why"]


def test_an_agents_spawn_refused_by_the_grant_or_the_budget_makes_nothing(penv, monkeypatch):
    parent = a_parent(penv)
    real, called = account_pools.choose, []

    def spy(*a, **k):
        called.append(1)
        return real(*a, **k)
    monkeypatch.setattr(account_pools, "choose", spy)
    with pytest.raises(Exception, match="no live spawn grant"):
        hs.spawn(penv.board, penv.p["claude"], bound(penv, parent), task="t", agent=POOL)
    assert called == [], "no grant: refused before any choice"
    grant(penv, agents=("claude",))
    spawn_cfg(penv, max_live_children=1)
    hs.spawn(penv.board, penv.p["claude"], bound(penv, parent), task="t", agent=POOL, access="read-only")
    before = len(hosted._sessions(penv.board.conn))
    with pytest.raises(hs.LimitReached, match="max_live_children is 1"):
        hs.spawn(penv.board, penv.p["claude"], bound(penv, parent), task="t", agent=POOL, access="read-only")
    assert len(hosted._sessions(penv.board.conn)) == before, "the budget refuses it, whatever the choice: nothing made"
    assert len(hs._budget(penv.board.conn, "claude", PROJECT)["spawned"]) == 1, "the caller's budget, charged once"


def test_an_agents_spawn_replayed_after_a_usage_change_returns_the_same_child_and_member(penv):
    parent = a_parent(penv)
    grant(penv, agents=("claude",))
    a = hs.spawn(penv.board, penv.p["claude"], bound(penv, parent), task="t", agent=POOL, access="read-only",
                 request_id="r1")
    seen(penv, WORK, percent=99)
    b = hs.spawn(penv.board, penv.p["claude"], bound(penv, parent), task="t", agent=POOL, access="read-only",
                 request_id="r1")
    assert b["created"] is False and b["child"]["id"] == a["child"]["id"] and b["child"]["agent"] == WORK


def _node_offers(env, **runners) -> None:
    info = nodes._get(env.board.conn, nodes.NODE_PREFIX + "laptop")
    for key, words in runners.items():
        if key == "max_live":
            info["sessions"]["max_live"] = words
        else:
            info["session_host"]["runners"][key] = words
    with db.write_tx(env.board.conn) as c:
        nodes._put(c, nodes.NODE_PREFIX + "laptop", info, "laptop", env.board.now())


def test_a_human_child_without_access_skips_a_member_at_the_access_it_would_really_get(penv):
    """AP-1: no --access means the parent's (workspace); a member whose node offers it read-only only would be refused
    by the creation, so the choice passes it over with that reason."""
    parent = a_parent(penv)
    _node_offers(penv, **{WORK: ["read-only"]})
    out = child(penv, parent, agent=POOL)
    assert out["agent"] == "claude" and out["access"] == "workspace"
    assert f"cannot start a session of {WORK} at workspace access" in out["pool"]["why"]


def test_an_agents_spawn_skips_a_member_whose_default_access_its_node_would_not_admit(penv):
    """AP-1: the spawn would give the work member `full` (the most its node offers within the ceiling), which a
    one-slot node refuses under a hosted parent; the other member defaults to workspace and is taken."""
    parent = a_parent(penv)
    rec = stored(penv, parent) | {"access": "full", "ceiling": "full"}
    with db.write_tx(penv.board.conn) as c:
        nodes._put(c, hosted.SESSION_PREFIX + parent, rec, "laptop", penv.board.now())
    grant(penv, agents=("claude",), access="full")
    _node_offers(penv, max_live=1, **{WORK: ["read-only", "workspace", "full"],
                                      "claude-code": ["read-only", "workspace"]})
    out = hs.spawn(penv.board, penv.p["claude"], bound(penv, parent), task="t", agent=POOL)
    assert out["child"]["agent"] == "claude" and out["child"]["access"] == "workspace"
    assert "a full-access child needs its parent's process up beside its own" in stored(
        penv, out["child"]["id"])["pool"]["why"]
    caps = hs.capabilities(penv.board, penv.p["claude"], bound(penv, parent))
    pool = caps["pools"][0]
    assert pool["agents"] == [WORK, "claude"] and pool["access"] == ["read-only", "workspace"], \
        "AP-11: listed for the words that work (an explicit workspace passes for the work member), never full"


def test_a_pool_removed_by_a_reload_before_the_write_is_an_unknown_name_and_never_a_crash(penv, monkeypatch):
    """AP-8: recognition and choice read the settings once, inside the write."""
    from agent_comms import hosted_tree
    real, calls = hosted_tree.spawn_config, []

    def reload_between(board):
        calls.append(1)
        out = real(board)
        if len(calls) == 1:     # the look before the write saw the pool; a reload drops it before the write
            spawn_cfg(penv, pools={})
        return out
    monkeypatch.setattr(hosted_tree, "spawn_config", reload_between)
    r = penv.human.post("/api/hosted-sessions", json={"project": PROJECT, "message": "x", "agent": POOL})
    assert r.status_code == 404 and "no agent named 'claudes'" in r.json()["message"], r.text
    parent = None
    spawn_cfg(penv, pools={POOL: [WORK, "claude"]})
    calls.clear()
    monkeypatch.setattr(hosted_tree, "spawn_config", real)
    parent = a_parent(penv, project=P2)
    monkeypatch.setattr(hosted_tree, "spawn_config", reload_between)
    out = child(penv, parent, agent=POOL, expect=404)
    assert "no agent named 'claudes'" in out["message"]


def test_a_human_child_uses_the_rule_in_force_in_the_write_not_the_one_read_before_it(penv, monkeypatch):
    from agent_comms import hosted_tree
    parent = a_parent(penv)
    real, calls = hosted_tree.spawn_config, []

    def rule_changes(board):
        calls.append(1)
        out = real(board)
        if len(calls) == 1:
            spawn_cfg(penv, pool_rules={PROJECT: {"never": [WORK]}})
        return out
    monkeypatch.setattr(hosted_tree, "spawn_config", rule_changes)
    out = child(penv, parent, agent=POOL, access="read-only")
    assert out["agent"] == "claude" and f"never {WORK}" in out["pool"]["why"]


def test_a_frame_never_makes_a_valid_task_fail_however_long_the_pools_lines(penv, monkeypatch):
    """AP-10: the pool words are fitted to what the 64 KiB message has left after a 48 KiB task and the frame."""
    parent = a_parent(penv)
    grant(penv, agents=("claude",))
    monkeypatch.setattr(account_pools, "describe", lambda pool, members: f"{pool}: " + "x" * 20000)
    task = "t" * hs.TASK_MAX_BYTES
    kid = hs.spawn(penv.board, penv.p["claude"], bound(penv, parent), task=task, agent="claude",
                   access="read-only")["child"]["id"]
    tick(penv)
    [frame] = kid_turns(penv, kid)
    assert len(frame.encode()) <= hosted.MESSAGE_MAX_BYTES and task in frame
    assert f"a pool of accounts (board_session_capabilities gives each one's order): {POOL}" in frame
    assert "x" * 1000 not in frame
    short = hs.spawn(penv.board, penv.p["claude"], bound(penv, parent), task="small", agent="claude",
                     access="read-only")["child"]["id"]
    tick(penv)
    assert "x" * 20000 in kid_turns(penv, short)[0], "with room, the full lines"
    assert hs._fit_pools([{"pool": POOL, "about": "a" * 50}], 10) == ""
    assert hs._fit_pools([{"pool": POOL, "about": "a" * 50}], -5) == ""
    assert "board_session_capabilities lists them" in hs._fit_pools([{"pool": "p" * 30, "about": "a" * 500}], 100)


def test_an_interactive_pool_spawn_starts_on_the_node_the_choice_checked(penv, monkeypatch):
    """AP-1 remainder: the choice's node, access and placement moment are carried through the start, so a heartbeat
    that ages out between the two does not refuse a member the choice found eligible."""
    grant(penv, agents=("claude",))
    real = account_pools.choose

    def then_time_passes(*a, **k):
        out = real(*a, **k)
        penv.clock.advance(nodes.NODE_STALE_SECONDS + 5)    # the node's last hello is now too old to place on
        return out
    monkeypatch.setattr(account_pools, "choose", then_time_passes)
    out = hs.spawn(penv.board, penv.p["claude"], interactive(penv), task="t", agent=POOL, project=PROJECT)
    assert out["child"]["agent"] == WORK and out["child"]["node"] == "laptop"
    assert out["child"]["access"] == "workspace", "the access the choice judged"


def test_the_start_takes_the_choices_node_and_access_and_judges_nothing_again(penv, monkeypatch):
    """AP-1 remainder: an interactive pool spawn places each member once (in the choice) and computes its access
    once; the start uses what the choice found. Its frame is written for that node (the child may start helpers)."""
    from agent_comms import hosted_tree
    grant(penv, agents=("claude", WORK))
    placed, defaults = [], []
    real_place, real_default = hosted_tree.place_interactive, hs._default_access
    monkeypatch.setattr(hosted_tree, "place_interactive", lambda *a, **k: placed.append(a[2]) or real_place(*a, **k))
    monkeypatch.setattr(hs, "_default_access", lambda *a, **k: defaults.append(a[2]) or real_default(*a, **k))
    real_choose, at_choice = account_pools.choose, {}

    def choose(*a, **k):
        out = real_choose(*a, **k)
        at_choice.update(placed=len(placed), defaults=len(defaults))
        return out
    monkeypatch.setattr(account_pools, "choose", choose)
    out = hs.spawn(penv.board, penv.p["claude"], interactive(penv), task="t", agent=POOL, project=PROJECT)
    assert out["child"]["agent"] == WORK and out["child"]["access"] == "workspace"
    assert placed[at_choice["placed"]:].count(WORK) == 0 and defaults[at_choice["defaults"]:].count(WORK) == 0, \
        "after the choice, the start places the chosen member no more and computes its access no more"
    tick(penv)
    assert kid_turns(penv, out["child"]["id"])[0].split("\n")[1].startswith("You may start helper sessions")


def test_a_human_pool_child_is_started_on_the_moment_the_choice_judged(penv, monkeypatch):
    parent = a_parent(penv)
    real = account_pools.choose

    def then_time_passes(*a, **k):
        out = real(*a, **k)
        penv.clock.advance(nodes.NODE_STALE_SECONDS + 5)
        return out
    monkeypatch.setattr(account_pools, "choose", then_time_passes)
    out = child(penv, parent, agent=POOL, access="read-only")
    assert out["agent"] == WORK


def test_an_audit_line_queued_before_the_limit_was_lowered_still_posts_within_it(penv, monkeypatch):
    """AP-2 remainder: the pool words are fitted again to the limit in force when the post is made."""
    parent = a_parent(penv)
    grant(penv, agents=("claude",))
    real_flush = hs.flush
    monkeypatch.setattr(hs, "flush", lambda board: None)
    out = hs.spawn(penv.board, penv.p["claude"], bound(penv, parent), task="t", agent=POOL, access="read-only")
    [entry] = tree_rec(penv, parent)["audit"]
    assert "Its agent was asked for as pool" in entry["pool"] and "pool" not in entry["base"]
    penv.board.s.body_max_bytes = len(entry["base"].encode()) + 20
    real_flush(penv.board)
    assert tree_rec(penv, parent)["audit"] == [], "posted: the queue is not held"
    body = posts_in(penv, tree_rec(penv, parent)["thread"])[-1]["body"]
    assert body == entry["base"] and len(body.encode()) <= penv.board.s.body_max_bytes


def test_a_title_holding_the_pool_words_never_moves_or_loses_the_hubs_own(penv, monkeypatch):
    """AP-13: the hub's pool words are their own field of the queued entry; the base (with the title and its
    untrusted marker) is never edited, and nothing searches the text for the hub's words."""
    parent = a_parent(penv)
    grant(penv, agents=("claude",))
    real_flush = hs.flush
    monkeypatch.setattr(hs, "flush", lambda board: None)
    title = " Its agent was asked for as pool fake and chosen by the hub (by fiat"
    out = hs.spawn(penv.board, penv.p["claude"], bound(penv, parent), task="t", agent=POOL, access="read-only",
                   title=title)
    [entry] = tree_rec(penv, parent)["audit"]
    marker = "(the title is that agent's own words: untrusted data)"
    for limit, expect in ((4096, "pool"), (len((entry["base"] + entry["pool_short"]).encode()), "pool_short"),
                          (len(entry["base"].encode()), None)):
        body = hs.compose_audit(entry, limit)
        assert body.startswith(entry["base"]) and marker in body and len(body.encode()) <= limit
        assert body == entry["base"] + (entry[expect] if expect else "")
        assert ("pool claudes" in body) == (expect is not None) and "pool fake" in body, "the title stays as written"
    penv.board.s.body_max_bytes = len((entry["base"] + entry["pool_short"]).encode())
    real_flush(penv.board)
    body = posts_in(penv, tree_rec(penv, parent)["thread"])[-1]["body"]
    assert body == entry["base"] + entry["pool_short"] and marker in body and out["child"]["id"] in body


def test_the_audit_post_stays_within_the_boards_body_limit_and_show_keeps_the_full_reason(penv):
    """AP-2: a long reason (every member passed over with a long targets list) never makes the post too long."""
    parent = a_parent(penv)
    grant(penv, agents=("claude",))
    targets = ["claude"] + [f"t{i:02d}/" + "m" * 90 for i in range(48)]
    spawn_cfg(penv, targets=targets)
    out = hs.spawn(penv.board, penv.p["claude"], bound(penv, parent), task="t", agent=POOL, access="read-only")
    why = stored(penv, out["child"]["id"])["pool"]["why"]
    assert why.endswith("...)") and len(why) <= account_pools.WHY_MAX, "each member's reason is bounded too"
    body = posts_in(penv, tree_rec(penv, parent)["thread"])[-1]["body"]
    assert len(body.encode()) <= penv.board.s.body_max_bytes and "1 member(s) could not be started" in body
    assert why in show(penv, out["child"]["id"])["pool"]["why"]
    small = {"session_id": "s", "name": "n", "pool": {"name": POOL, "summary": "x" * 5000, "why": "y"}}
    caller = hs.Caller("hosted", "p", "claude", PROJECT, 1, "p", {}, "laptop", "workspace")
    entry = hs._audit_spawn(caller, small)
    base = hs._audit_spawn(caller, {"session_id": "s", "name": "n"})
    assert entry["base"] == base, "the base is main's line, apart from the pool words"
    for limit in (4096, 700, 500, 100):
        line_ = hs.compose_audit(entry, limit)
        assert line_.startswith(base) and len(line_.encode()) <= max(limit, len(base))
    assert len(hs.compose_audit(entry, None)) < 1200, "a summary is a few words, whatever a record holds"


def test_an_interactive_caller_may_name_the_pool_too(penv):
    grant(penv, agents=("claude",))
    out = hs.spawn(penv.board, penv.p["claude"], interactive(penv), task="t", agent=POOL, access="read-only",
                   project=PROJECT)
    assert out["child"]["agent"] == WORK and stored(penv, out["child"]["id"])["pool"]["name"] == POOL


def test_an_agents_spawn_in_a_project_with_only_refuses_the_other(penv):
    parent = a_parent(penv)
    grant(penv, agents=("claude",))
    spawn_cfg(penv, pool_rules={PROJECT: {"only": [WORK]}})
    penv.board.s.dispatch = {**penv.board.s.dispatch, "agent_projects": {WORK: [P2]}}
    with pytest.raises(Conflict, match=f"no member of pool {POOL} can be started in {PROJECT}: {WORK} cannot"):
        hs.spawn(penv.board, penv.p["claude"], bound(penv, parent), task="t", agent=POOL, access="read-only")


# ------------------------------------------------------------ capabilities and the frame


def test_capabilities_list_a_pool_only_where_a_member_could_be_placed(penv):
    parent = a_parent(penv)
    grant(penv, agents=("claude",))
    caps = hs.capabilities(penv.board, penv.p["claude"], bound(penv, parent))
    assert caps["pools"] == [{"pool": POOL, "agents": [WORK, "claude"],
                              "about": f"{POOL}: {WORK} first, claude when it is near its limit",
                              "access": ["read-only", "workspace"]}]
    assert "pool_problems" not in caps
    penv.board.s.dispatch = {**penv.board.s.dispatch, "agent_projects": {WORK: [P2]}}
    caps = hs.capabilities(penv.board, penv.p["claude"], bound(penv, parent))
    assert caps["pools"] == [{"pool": POOL, "agents": ["claude"],
                              "about": f"{POOL}: claude (the only member that can be started here)",
                              "access": ["read-only", "workspace"]}]
    penv.board.s.dispatch = {**penv.board.s.dispatch, "agent_projects": {WORK: [P2], "claude": [P2]}}
    caps = hs.capabilities(penv.board, penv.p["claude"], bound(penv, parent))
    assert caps["pools"] == [], "no member here: no pool"


def test_capabilities_follow_the_project_rule_and_never_promise_a_member(penv):
    parent = a_parent(penv)
    grant(penv, agents=("claude",))
    spawn_cfg(penv, pool_rules={PROJECT: {"prefer": ["claude"]}})
    seen(penv, "claude", percent=99)
    caps = hs.capabilities(penv.board, penv.p["claude"], bound(penv, parent))
    assert caps["pools"][0]["about"] == f"{POOL}: claude first, {WORK} when it is near its limit", \
        "the order, whatever usage says now"


def test_capabilities_say_what_is_wrong_with_a_pool(penv):
    parent = a_parent(penv)
    grant(penv, agents=("claude",))
    spawn_cfg(penv, pools={POOL: [WORK, "ghost"]})
    caps = hs.capabilities(penv.board, penv.p["claude"], bound(penv, parent))
    assert [p["agents"] for p in caps["pools"]] == [[WORK]] and "pool_problems" not in caps, \
        "a member that is not an agent is only not offered"
    spawn_cfg(penv, pools={POOL: [WORK, "codex"]})
    caps = hs.capabilities(penv.board, penv.p["claude"], bound(penv, parent))
    assert caps["pools"] == [] and "mixes runtimes" in caps["pool_problems"][0]


def test_the_spawn_frame_offers_the_pool_in_one_line(penv):
    parent = a_parent(penv)
    grant(penv, agents=("claude", WORK))
    kid = hs.spawn(penv.board, penv.p["claude"], bound(penv, parent), task="look", agent="claude")["child"]["id"]
    tick(penv)
    line = kid_turns(penv, kid)[0].split("\n")[1]
    assert line.startswith("You may start helper sessions") and (
        f"; or, as child_agent, a pool of accounts, whose member the board picks when the session starts (the order "
        f"is not a promise): {POOL}: {WORK} first, claude when it is near its limit;") in line, line


# ------------------------------------------------------------ back-compat


def test_without_pools_every_answer_record_and_report_is_as_before(penv):
    spawn_cfg(penv, pools={})
    parent = a_parent(penv)
    grant(penv, agents=("claude",))
    caps = hs.capabilities(penv.board, penv.p["claude"], bound(penv, parent))
    assert "pools" not in caps and "pool_problems" not in caps
    out = hs.spawn(penv.board, penv.p["claude"], bound(penv, parent), task="look", agent=WORK, access="read-only")
    assert "pool" not in out["child"] and "pool" not in stored(penv, out["child"]["id"])
    tick(penv)
    line = kid_turns(penv, out["child"]["id"])[0]
    assert "pool" not in line
    body = posts_in(penv, tree_rec(penv, parent)["thread"])[0]["body"]
    assert "pool" not in body and body.endswith(f"`board session show {out['child']['id']}` shows the session.")
    s = start(penv, agent=WORK, project=P2)
    assert "pool" not in s and "pool" not in show(penv, s["session_id"])
    # The node's hello: the hub does not ask for usage, and the node sends none, though its host has seen some.
    penv.node.sessions.host.note_usage("claude-code", [{"percent": 50.0, "window": "five_hour"}], penv.node.clock())
    penv.node.hello()       # (the answer to the fixture's hello, from a hub that had pools then, asked once more)
    with db.write_tx(penv.board.conn) as c:
        c.execute("DELETE FROM board_state WHERE key = ?", (account_pools.USAGE_PREFIX + "laptop",))
    sent = []
    real = penv.node.client.request

    def spy(method, path, **kw):
        sent.append(kw.get("json") or {})
        return real(method, path, **kw)
    penv.node.client.request = spy
    answer_ = penv.node.hello()
    penv.node.hello()
    assert "report_usage" not in answer_ and sent and all("usage" not in b for b in sent)
    assert account_pools.usage_view(penv.board.conn, "laptop") == {}


# ------------------------------------------------------------ what the node reports


def report_of(**kw) -> dict:
    return {"window": "five_hour", "label": None, "percent": None, "resets_at": None, "exhausted": False} | kw


def test_the_node_reports_usage_to_a_hub_that_asks_and_the_hub_keeps_it_in_its_own_time(penv):
    host = penv.node.sessions.host
    host.note_usage(WORK, [report_of(percent=95.0, resets_at=penv.board.now() + 600, window="five_hour")],
                    penv.node.clock() - 30)
    sent = []
    real = penv.node.client.request

    def spy(method, path, **kw):
        sent.append(kw.get("json"))
        return real(method, path, **kw)
    penv.node.client.request = spy
    penv.node.report_usage = False
    assert penv.node.hello()["report_usage"] is True and "usage" not in sent[-1], "sent only once asked"
    penv.node.hello()
    assert sent[-1]["usage"] == {WORK: [report_of(percent=95.0, resets_at=penv.board.now() + 600, window="five_hour",
                                                  age=30.0)]}
    assert kept(penv, WORK)["observed_at"] == penv.board.now() - 30 and kept(penv, WORK)["percent"] == 95.0
    assert start(penv, agent=POOL)["agent"] == "claude"


def test_an_older_report_never_replaces_a_newer_one(penv):
    seen(penv, WORK, percent=10, age=0)
    seen(penv, WORK, percent=99, age=600)
    assert kept(penv, WORK)["percent"] == 10


def test_each_window_keeps_its_own_age_so_a_sparse_update_never_freshens_another(penv):
    """AP-6: an old 95% weekly window is not made fresh by later updates of the five-hour one."""
    seen(penv, WORK, percent=95, window="seven_day", age=0)
    for _ in range(3):
        penv.clock.advance(25 * 60)
        seen(penv, WORK, percent=10, window="five_hour", age=0)
    penv.node.hello()       # (the node is still there)
    assert kept(penv, WORK, "seven_day") == {}, "the weekly 95% is 75 minutes old: stale, and pruned as expired"
    assert kept(penv, WORK, "five_hour")["observed_at"] == penv.board.now()
    assert start(penv, agent=POOL)["agent"] == WORK
    # ... and on the node: each window's own time goes out as its own age.
    host = penv.node.sessions.host
    host.note_usage(WORK, [report_of(percent=95.0, window="seven_day")], penv.node.clock())
    penv.clock.advance(600)
    host.note_usage(WORK, [report_of(percent=10.0, window="five_hour")], penv.node.clock())
    ages = {o["window"]: o["age"] for o in penv.node.sessions.usage_report()[WORK]}
    assert ages == {"seven_day": 600.0, "five_hour": 0.0}


def test_the_worst_window_decides(penv):
    seen(penv, WORK, percent=10, window="five_hour")
    seen(penv, WORK, percent=93, window="seven_day")
    s = start(penv, agent=POOL)
    assert s["agent"] == "claude" and "was at 93% of its seven_day window" in s["pool"]["why"]


def test_a_reset_already_past_when_seen_is_believed_and_means_room(penv):
    """AP-5: a window whose reset had already passed when the observation arrived has reset: room, and an exhausted
    mark with such a reset starts no cool-down."""
    seen(penv, WORK, percent=95, resets_in=-60)
    assert start(penv, agent=POOL)["agent"] == WORK
    seen(penv, WORK, exhausted=True, resets_in=-60, window="seven_day")
    assert start(penv, agent=POOL, project=P2)["agent"] == WORK


def test_the_hub_takes_what_it_can_use_of_any_usage_and_never_refuses_the_hello(penv):
    """AP-4 and AP-7: odd values of every kind are dropped, the rest kept, and the hello (the heartbeat) goes on."""
    p = penv.p["laptop"]
    runners = ["claude-code", WORK, "codex-cli"]
    good = report_of(percent=50, age=0, window="five_hour")
    odd = [None, "x", 7, [], [1], {"percent": 10 ** 400, "age": 0}, {"percent": float("nan"), "age": 0},
           {"percent": float("inf"), "age": 0}, {"percent": -5, "age": 0}, {"percent": "high", "age": 0},
           {"percent": 1, "age": 10 ** 400}, {"percent": 1, "age": -1}, {"percent": 1, "age": float("nan")},
           {"percent": 1}, {"percent": 1, "age": 0, "resets_at": 10 ** 400}, {"percent": 1, "age": 0, "window": {}},
           {"percent": 1, "age": 0, "window": "x" * 500}, {"percent": {"deep": [1, {"x": 2}]}, "age": 0},
           {"percent": True, "age": False, "exhausted": "yes"}]
    for bad in (odd, {"nobody": [good]}, {WORK: odd}, {WORK: "junk"}, {WORK: [good] * 500},
                {f"r{i}": [good] for i in range(500)}, ["x"], "junk", 10 ** 400, float("nan")):
        out = nodes.hello(penv.board, p, [PROJECT], runners, 1, usage=bad)
        assert out["node"] == "laptop", bad
    r = penv.laptop.post("/api/nodes/hello", json={"projects": [PROJECT], "runners": runners, "max_concurrent": 1,
                                                   "usage": {WORK: [{"percent": 10 ** 400, "age": 0}, good]}})
    assert r.status_code == 200, r.text
    assert kept(penv, WORK)["percent"] == 50.0, "the usable one is kept"
    assert set(account_pools.usage_view(penv.board.conn, "laptop")) == {WORK}


def test_every_usage_value_goes_through_one_sanitiser_that_cannot_raise():
    from agent_comms import providers
    weird = [None, True, "x", 10 ** 400, -10 ** 400, float("nan"), float("inf"), -1, [], {}, {"a": [1]}, object()]
    for v in weird:
        assert providers.number(v) is None or isinstance(providers.number(v), float)
        for k in ("percent", "resets_at", "window", "label", "exhausted", "age"):
            out = providers.clean_usage({"window": "w", k: v, "age": 0})
            assert out is None or set(out) == {"percent", "resets_at", "window", "label", "exhausted"}
            providers.clean_usage({"window": "w", k: v} | ({} if k == "age" else {"age": 0}), age=True)
        assert providers.clean_usage(v) is None or isinstance(v, dict)
    # A malformed percent drops the reading (it replaces nothing); a malformed reset time is only left out.
    assert providers.clean_usage({"window": "w", "percent": 10 ** 400}) is None
    assert providers.clean_usage({"window": "w", "percent": 1, "resets_at": 10 ** 400}) == report_of(
        window="w", percent=1.0)
    assert providers.clean_usage({"window": "w", "percent": None}) == report_of(window="w"), "the intentional clear"
    assert providers.clean_usage({"window": "w", "age": 10 ** 400}, age=True) is None
    assert providers.clean_usage({"percent": 1}) is None, "a reading is of a window"
    for v in (float("inf"), float("-inf"), float("nan"), 10 ** 400, True, "1"):
        assert providers.number(v) is None, v
    assert providers.number(5) == 5.0 and providers.number(-2.5) == -2.5


def test_odd_provider_lines_never_affect_the_turn_or_the_session(penv):
    """AP-4 on the node: the session's process writes rate-limit lines with values no float can hold, NaN, nested
    junk; nothing raises, the turn and the session go on, and the hello still goes out."""
    sid = start(penv, agent=WORK)["session_id"]
    tick(penv)
    call, proc = proc_of(penv, sid)
    big = 10 ** 400
    for info in ({"status": "allowed", "utilization": big}, {"status": "rejected", "resetsAt": big},
                 {"unifiedWindows": {"five_hour": {"utilization": big, "resetsAt": big}}},
                 {"unifiedWindows": {"five_hour": {"utilization": "x"}, 7: {"utilization": 0.5}}},
                 {"status": ["rejected"], "rateLimitType": {"x": 1}}, [1, 2], "junk", None):
        proc.emit('{"type": "rate_limit_event", "session_id": "%s", "rate_limit_info": %s}' % (
            sid, json.dumps(info) if not isinstance(info, dict) or "utilization" not in info or info["utilization"]
            is not big else '{"status": "allowed", "utilization": 1' + "0" * 400 + "}"))
    proc.emit({"type": "rate_limit_event", "session_id": sid, "rate_limit_info": {
        "unifiedWindows": {"five_hour": {"utilization": 1e308 * 10}}}})
    from test_hosted_tree import as_started, began
    from test_session_host import result
    proc.emit(began(sid, *as_started(call["argv"])), result(sid, text="still fine"))
    tick(penv)
    assert show(penv, sid)["reply"] == "still fine" and stored(penv, sid)["state"] != "ended"
    assert penv.node.hello()["node"] == "laptop"
    pc = codex_ready()
    for snap in ({"primary": {"usedPercent": big, "resetsAt": big, "windowDurationMins": big}},
                 {"primary": "x", "secondary": [1]}, {"limitId": {"x": 1}, "primary": {"usedPercent": 5}}, "junk"):
        events, _ = pc.feed(('{"method": "account/rateLimits/updated", "params": {"rateLimits": %s}}'
                             % json.dumps(snap)).encode())
        assert all(e.kind == "usage" for e in events)


def test_runners_the_node_no_longer_offers_and_old_observations_are_pruned(penv):
    """AP-7: the hub's record cannot grow without bound."""
    p = penv.p["laptop"]
    for i in range(3):
        nodes.hello(penv.board, p, [PROJECT], [f"r{i}"], 1, usage={f"r{i}": [report_of(percent=1, age=0)]})
    assert set(account_pools.usage_view(penv.board.conn, "laptop")) == {"r2"}, "only runners it offers now"
    runners = ["claude-code", WORK]
    nodes.hello(penv.board, p, [PROJECT], runners, 1, usage={
        WORK: [report_of(percent=1, age=0, window=f"w{i}") for i in range(40)]})
    from agent_comms.providers import USAGE_WINDOWS_MAX
    assert len(account_pools.usage_view(penv.board.conn, "laptop")[WORK]) == USAGE_WINDOWS_MAX
    penv.clock.advance(86401)
    nodes.hello(penv.board, p, [PROJECT], runners, 1, usage={"claude-code": [report_of(percent=1, age=0)]})
    assert set(account_pools.usage_view(penv.board.conn, "laptop")) == {"claude-code"}, "a day old: gone"


def test_the_hub_keeps_at_most_sixteen_windows_per_runner_across_reports(penv):
    from agent_comms.providers import USAGE_WINDOWS_MAX
    p, runners = penv.p["laptop"], ["claude-code", WORK]
    for batch in range(3):
        penv.clock.advance(1)
        nodes.hello(penv.board, p, [PROJECT], runners, 1, usage={
            WORK: [report_of(percent=1, age=0, window=f"w{batch}_{i}") for i in range(10)]})
    windows = account_pools.usage_view(penv.board.conn, "laptop")[WORK]
    assert len(windows) == USAGE_WINDOWS_MAX and all(w.startswith(("w2_", "w1_")) for w in windows), "the newest"
    assert account_pools.clean_report({"nobody": [report_of(age=0)], WORK: [report_of(age=0)]}, runners) == {
        WORK: [report_of(age=0.0)]}, "only runners the node offers"


def test_a_spawn_naming_a_pool_with_an_agents_name_gets_that_agent(penv):
    parent = a_parent(penv)
    grant(penv, agents=("claude",))
    spawn_cfg(penv, pools={"claude": [WORK, "codex"]})
    out = hs.spawn(penv.board, penv.p["claude"], bound(penv, parent), task="t", agent="claude", access="read-only")
    assert out["child"]["agent"] == "claude" and "pool" not in out["child"]


def test_a_choice_reason_is_bounded_however_many_members_and_rules(penv):
    from types import SimpleNamespace
    names = [f"acct{i}" for i in range(10)]
    for n in names:
        create_agent(penv.settings.agents_path, n, "claude-code")
    penv.board.authenticate(penv.tokens["human"])      # (agents.toml reloaded)
    project = "/" + "p" * 1000
    cfg = SimpleNamespace(pools={POOL: names}, pool_rules={project: {"prefer": names[::-1]}},
                          pool_headroom=dict(account_pools.HEADROOM_DEFAULTS))
    choice = account_pools.choose(penv.board, penv.board.conn, POOL, project,
                                  lambda a, rt: (None, "x" * 5000) if a != names[0] else ("laptop", None), cfg)
    assert choice.agent == names[0] and len(choice.why) <= account_pools.WHY_MAX and choice.why.endswith("...")


def test_with_no_pool_configured_nothing_is_stored(penv):
    spawn_cfg(penv, pools={})
    out = nodes.hello(penv.board, penv.p["laptop"], [PROJECT], [WORK], 1, usage={WORK: [report_of(percent=1, age=0)]})
    assert "report_usage" not in out and account_pools.usage_view(penv.board.conn, "laptop") == {}


def test_a_node_whose_hub_refuses_usage_sends_its_hello_again_without_it(penv):
    real = penv.node.client.request
    penv.node.sessions.host.note_usage(WORK, [report_of(percent=1.0)], penv.node.clock())
    penv.node.report_usage = True

    class RolledBack:
        """A hub from before pools: a hello that carries `usage` is a 422; it never asks for usage."""

        def __init__(self):
            self.bodies = []

        def request(self, method, path, **kw):
            self.bodies.append(dict(kw.get("json") or {}))
            if path == "/api/nodes/hello":
                if "usage" in (kw.get("json") or {}):
                    return httpx.Response(422, request=httpx.Request(method, "http://hub" + path), json={"detail": []})
                out = real(method, path, **kw)
                data = out.json()
                data.pop("report_usage", None)
                return httpx.Response(200, request=out.request, json=data)
            return real(method, path, **kw)
    older = RolledBack()
    penv.node.client = older
    assert penv.node.hello()["node"] == "laptop"
    assert [("usage" in b) for b in older.bodies] == [True, False] and penv.node.report_usage is False
    penv.node.hello()
    assert "usage" not in older.bodies[-1], "none until a hub asks again"


def test_a_node_with_folders_and_usage_drops_usage_first_then_folders_for_an_older_hub(penv):
    real = penv.node.client.request
    penv.node.config.runner_homes = {WORK: "/x/.claude-work"}
    penv.node.sessions.host.note_usage(WORK, [report_of(percent=1.0)], penv.node.clock())
    penv.node.report_usage = True

    class Oldest:
        def __init__(self):
            self.bodies = []

        def request(self, method, path, **kw):
            body = kw.get("json") or {}
            self.bodies.append(sorted(k for k in ("usage", "homes") if k in body))
            if path == "/api/nodes/hello" and ("usage" in body or "homes" in body):
                return httpx.Response(422, request=httpx.Request(method, "http://hub" + path), json={"detail": []})
            return real(method, path, **kw)
    oldest = Oldest()
    penv.node.client = oldest
    assert penv.node.hello()["node"] == "laptop"
    assert oldest.bodies == [["homes", "usage"], ["homes"], []]


def test_an_older_node_reports_no_usage_and_the_pool_works_on_order_alone(penv):
    # A node from before pools never sends `usage`: nothing is stored, every member has room, the first is taken.
    assert account_pools.usage_view(penv.board.conn, "laptop") == {}
    assert start(penv, agent=POOL)["agent"] == WORK


def test_a_session_processes_rate_limit_event_reaches_the_hub_and_moves_the_next_start(penv):
    """The whole chain with the fake Claude process: its stream says the work account is at 95%, the host keeps it
    for that runner, the node's hello carries it to a hub that asked, and the next start overflows."""
    sid = start(penv, agent=WORK, project=P2)["session_id"]
    tick(penv)
    call, proc = proc_of(penv, sid)
    proc.emit({"type": "rate_limit_event", "session_id": sid, "uuid": "u1",
               "rate_limit_info": {"status": "allowed_warning", "resetsAt": int(penv.board.now()) + 3600,
                                   "rateLimitType": "five_hour", "utilization": 0.95}})
    tick(penv)
    assert penv.node.sessions.host.usage[WORK]["five_hour"]["percent"] == 95.0
    penv.node.hello()       # learns that the hub asks
    penv.node.hello()
    s = start(penv, agent=POOL)
    assert s["agent"] == "claude" and "was at 95% of its five_hour window" in s["pool"]["why"]


# ------------------------------------------------------------ the providers


def tight(observations):
    """The tightest of a usage event's observations (the highest percent)."""
    return max(observations, key=lambda o: -1 if o["percent"] is None else o["percent"])


def test_claudes_rate_limit_info_gives_one_observation_per_window():
    u = claude_provider.usage_of
    both = u({"status": "allowed", "resetsAt": 100, "rateLimitType": "five_hour", "utilization": 0.4,
              "unifiedWindows": {"five_hour": {"utilization": 0.4, "resetsAt": 100},
                                 "seven_day": {"utilization": 0.92, "resetsAt": 900}}})
    assert both == [report_of(percent=40.0, resets_at=100.0, window="five_hour"),
                    report_of(percent=92.0, resets_at=900.0, window="seven_day")]
    assert u({"status": "allowed", "resetsAt": 100, "rateLimitType": "five_hour", "utilization": 0.4}) == [
        report_of(percent=40.0, resets_at=100.0, window="five_hour")]
    assert u({"status": "allowed_warning", "surpassedThreshold": 0.9, "rateLimitType": "seven_day"})[0][
        "percent"] == 90.0
    assert u({"status": "rejected", "resetsAt": 500, "rateLimitType": "five_hour"}) == [
        report_of(resets_at=500.0, window="five_hour", exhausted=True)]
    assert u({"status": "allowed"}) is None, "a status alone says nothing of usage"
    assert u({"status": "allowed", "utilization": None}) == [report_of(window="limiting")], \
        "an explicit null is the provider's clear"
    for junk in (None, "x", {}, {"status": "odd"}, {"utilization": True}, {"unifiedWindows": "x"}):
        assert u(junk) is None
    assert u({"status": "allowed", "utilization": float("inf")}) is None, "malformed: no information"
    assert u({"status": "allowed", "rateLimitType": "Five Hour!", "utilization": 0.1}) is None, \
        "a window it cannot name is no reading"
    assert u({"status": "rejected", "resetsAt": 10 ** 12})[0]["resets_at"] is None, "not a time in epoch seconds"
    assert u({"status": "rejected", "resetsAt": -5})[0]["resets_at"] is None
    assert u({"status": "allowed", "utilization": 30}) is None, "3000%: malformed, dropped"


def test_a_claude_rejection_uses_the_rejected_windows_own_reset():
    """AP-5: without a top-level reset, the five-hour rejection is until the five-hour reset, never the weekly."""
    u = claude_provider.usage_of
    got = u({"status": "rejected", "rateLimitType": "five_hour", "unifiedWindows": {
        "five_hour": {"utilization": 1.0, "resetsAt": 1000, "status": "rejected"},
        "seven_day": {"utilization": 0.5, "resetsAt": 9000}}})
    assert got == [report_of(percent=100.0, resets_at=1000.0, window="five_hour", exhausted=True),
                   report_of(percent=50.0, resets_at=9000.0, window="seven_day")]
    got = u({"status": "rejected", "unifiedWindows": {
        "five_hour": {"utilization": 1.0, "resetsAt": 1000, "status": "rejected"},
        "seven_day": {"utilization": 0.5, "resetsAt": 9000}}})
    assert [o["exhausted"] for o in got] == [True, False] and got[0]["resets_at"] == 1000.0, "naming no type"
    # AP-5 remainder: the rejected window carries no utilization; its reset is read on its own.
    got = u({"status": "rejected", "rateLimitType": "five_hour", "unifiedWindows": {
        "five_hour": {"resetsAt": 1000, "status": "rejected"}, "seven_day": {"utilization": 0.5, "resetsAt": 9000}}})
    assert report_of(resets_at=1000.0, window="five_hour", exhausted=True) in got


def claude_session():
    p = claude_provider.ClaudeSession()
    p.argv(["claude", "-p", "{prompt}"], session_id="00000000-0000-4000-8000-000000000001", resume=False, model=None)
    return p, "00000000-0000-4000-8000-000000000001"


def line(obj) -> bytes:
    return json.dumps(obj).encode()


def test_which_claude_window_a_rejection_marks_and_until_when():
    u = claude_provider.usage_of
    got = u({"status": "rejected", "unifiedWindows": {
        "seven_day": {"utilization": 0.5, "resetsAt": 9000},
        "five_hour": {"utilization": 1.0, "resetsAt": 1000, "status": "rejected"}}})
    assert {o["window"]: o["exhausted"] for o in got} == {"seven_day": False, "five_hour": True}, \
        "naming no type: the window that says rejected, wherever it is"
    got = u({"status": "rejected", "rateLimitType": "five_hour", "resetsAt": 2000, "unifiedWindows": {
        "five_hour": {"utilization": 1.0, "resetsAt": 1000}}})
    assert got[0]["resets_at"] == 1000.0, "the window's own reset first"
    got = u({"status": "allowed", "unifiedWindows": {"five_hour": {"utilization": 1.0, "status": "rejected"}}})
    assert got[0]["exhausted"] is False, "a window's own rejected alone does not mean refused (the schema)"


def test_a_malformed_claude_utilization_erases_nothing_and_a_rejection_on_it_still_counts(penv):
    """AP-4 remainder: a number, an explicit null and anything else are three things, end to end."""
    u = claude_provider.usage_of
    assert u({"status": "allowed", "unifiedWindows": {"five_hour": {"utilization": "high"}}}) is None
    assert u({"status": "allowed", "unifiedWindows": {"five_hour": {"utilization": None}}}) == [
        report_of(window="five_hour")], "the provider's explicit null: its clear"
    got = u({"status": "rejected", "rateLimitType": "five_hour", "resetsAt": 5000,
             "unifiedWindows": {"five_hour": {"utilization": "high", "resetsAt": 5000}}})
    assert got == [report_of(window="five_hour", resets_at=5000.0, exhausted=True)], "the refusal on its own"
    # End to end: a valid 95% reading, then an event with a malformed utilization, through host, node and hub.
    sid = start(penv, agent=WORK, project=P2)["session_id"]
    tick(penv)
    _, proc = proc_of(penv, sid)
    proc.emit({"type": "rate_limit_event", "session_id": sid, "rate_limit_info": {
        "status": "allowed", "unifiedWindows": {"five_hour": {"utilization": 0.95}}}})
    proc.emit({"type": "rate_limit_event", "session_id": sid, "rate_limit_info": {
        "status": "allowed", "unifiedWindows": {"five_hour": {"utilization": "garbled"}}, "utilization": [1]}})
    tick(penv)
    assert penv.node.sessions.host.usage[WORK]["five_hour"]["percent"] == 95.0
    penv.node.hello()
    penv.node.hello()
    assert kept(penv, WORK)["percent"] == 95.0
    assert start(penv, agent=POOL)["agent"] == "claude"
    assert account_pools.clean_report({WORK: [{"window": "five_hour", "age": 0}]}, [WORK]) == {}, \
        "an omitted percent on the wire: no reading"


def test_a_codex_null_and_a_malformed_percent_are_kept_apart():
    p = codex_ready()
    [ev], _ = p.feed(line({"method": "account/rateLimits/updated", "params": {"rateLimits": {
        "primary": {"usedPercent": None}, "secondary": {"usedPercent": "x"}}}}))
    assert ev.usage == [report_of(window="codex_primary")], "null: the clear; malformed: nothing"


def test_a_task_the_validators_take_is_framed_whatever_its_code_points(penv, monkeypatch):
    """F1: the frame is measured with the validators' own UTF-8 policy (errors replaced), so a task they accept, a
    lone surrogate in it, is framed and fits; no exception."""
    parent = a_parent(penv)
    grant(penv, agents=("claude",))
    monkeypatch.setattr(account_pools, "describe", lambda pool, members: f"{pool}: " + "x" * 10000)
    task = "look \ud800 here " + "t" * (hs.TASK_MAX_BYTES - 20)
    hs._task_text(task)         # the validator takes it
    out = hs.spawn(penv.board, penv.p["claude"], bound(penv, parent), task=task, agent="claude", access="read-only")
    r = stored(penv, out["child"]["id"])
    m = hosted._get(penv.board.conn, hosted._msg_key(r["session_id"], 1))
    assert len(m["text"].encode("utf-8", "replace")) <= hosted.MESSAGE_MAX_BYTES and "\ud800" in m["text"]


def test_the_frame_is_built_from_one_evaluation(penv, monkeypatch):
    """AP-10 remainder: the spawn line is evaluated once and the pool words are fitted to that very line, whatever
    the budget does in between (a frame never asks the budget twice)."""
    parent = a_parent(penv)
    grant(penv, agents=("claude",))
    # A line that fits the room a mis-measured frame would see, and not the real one.
    monkeypatch.setattr(account_pools, "describe", lambda pool, members: f"{pool}: " + "x" * 10000)
    calls, real_line, real_usage = [], hs._spawn_line, hs._usage
    monkeypatch.setattr(hs, "_spawn_line", lambda *a, **k: calls.append(1) or real_line(*a, **k))
    flips = iter(range(100))

    def budget(conn, agent, project, now):
        out = real_usage(conn, agent, project, now)
        # every other read of the budget: none left (a short "you cannot" line), then all of it
        return out | {"live": 10 ** 6} if next(flips) % 2 else out
    monkeypatch.setattr(hs, "_usage", budget)
    task = "t" * hs.TASK_MAX_BYTES
    kid = hs.spawn(penv.board, penv.p["claude"], bound(penv, parent), task=task, agent="claude",
                   access="read-only")["child"]["id"]
    tick(penv)
    [frame] = kid_turns(penv, kid)
    assert calls == [1] and len(frame.encode()) <= hosted.MESSAGE_MAX_BYTES and task in frame
    # The fit is measured on the whole of that line: a long line leaves less room, and the frame still fits.
    pools = [{"pool": POOL, "about": f"{POOL}: " + "y" * 18000}]
    line_ = "A" * 5000 + hs.POOL_SLOT + "B" * 5000
    framed = hs._frame("a session", "t" * 40000, line_, pools)
    assert len(framed.encode()) <= hosted.MESSAGE_MAX_BYTES and "y" * 18000 not in framed
    assert f"(board_session_capabilities gives each one's order): {POOL}" in framed
    plain = hs._frame("a session", "t", "no slot here", pools)
    assert "no slot here" in plain and len(plain) == len(hs.render_task("a session", "t", "no slot here")), \
        "a line without the slot is rendered as it is"


def test_a_parser_that_meets_anything_unexpected_says_nothing(monkeypatch):
    from agent_comms import providers

    def boom(x):
        raise RuntimeError("unexpected")
    monkeypatch.setattr(claude_provider, "number", boom)
    assert claude_provider.usage_of({"status": "allowed", "utilization": 0.5}) is None
    monkeypatch.setattr(codex_provider, "number", boom)
    p = codex_ready()
    assert p.feed(line({"method": "account/rateLimits/updated", "params": {"rateLimits": {
        "primary": {"usedPercent": 5}}}})) == ([], [])
    assert providers.clean_usage({"window": "w", "percent": 1, "age": 2 * 86400}, age=True) is None, "a day old"
    assert providers.clean_usage({"window": "w", "percent": 1, "age": -1}, age=True) is None
    assert providers.clean_usage({"window": "w", "percent": 1, "age": 86400}, age=True)["age"] == 86400.0


def test_a_codex_window_without_its_percentage_is_no_observation():
    p = codex_ready()
    [ev], _ = p.feed(line({"method": "account/rateLimits/updated", "params": {"rateLimits": {
        "primary": {"resetsAt": 5, "windowDurationMins": 300}, "secondary": {"usedPercent": 9}}}}))
    assert ev.usage == [report_of(percent=9.0, window="codex_secondary")]


def test_claudes_stream_gives_usage_events_and_a_rate_limit_error_leaves_a_failed_turn_mark():
    p, sid = claude_session()
    [ev], _ = p.feed(line({"type": "rate_limit_event", "session_id": sid,
                           "rate_limit_info": {"status": "rejected", "resetsAt": 777, "rateLimitType": "five_hour"}}))
    assert ev.kind == "usage" and ev.usage == [report_of(resets_at=777.0, window="five_hour", exhausted=True)]
    [ev], _ = p.feed(line({"type": "assistant", "session_id": sid, "error": "rate_limit", "message": {"content": []}}))
    assert ev.kind == "usage" and ev.usage == [report_of(window="failed_turn", label="five_hour", resets_at=777.0,
                                                         exhausted=True)]
    q, sid = claude_session()
    [ev], _ = q.feed(line({"type": "assistant", "session_id": sid, "error": "rate_limit", "message": {"content": []}}))
    assert ev.usage == [report_of(window="failed_turn", exhausted=True)], "no reset seen"
    assert q.feed(line({"type": "assistant", "session_id": sid, "error": "overloaded", "message": {}})) == ([], [])


def codex_ready():
    from test_providers_codex import in_turn
    return in_turn()


def test_codexs_rate_limit_updates_give_each_window_it_carries_and_never_an_exhausted_mark():
    """AP-3: a high percentage, or rateLimitReachedType, is only a high usage. AP-6: a window is its bucket and slot;
    its length is a label."""
    p = codex_ready()
    note = {"method": "account/rateLimits/updated", "params": {"rateLimits": {
        "primary": {"usedPercent": 40, "resetsAt": 100, "windowDurationMins": 300},
        "secondary": {"usedPercent": 70, "resetsAt": 900, "windowDurationMins": 10080}}}}
    [ev], _ = p.feed(line(note))
    assert ev.kind == "usage" and ev.usage == [
        report_of(window="codex_primary", label="five_hour", percent=40.0, resets_at=100.0),
        report_of(window="codex_secondary", label="seven_day", percent=70.0, resets_at=900.0)]
    [ev], _ = p.feed(line({"method": "account/rateLimits/updated", "params": {"rateLimits": {
        "primary": {"usedPercent": 100, "resetsAt": 120}, "rateLimitReachedType": "rate_limit_reached"}}}))
    assert ev.usage == [report_of(window="codex_primary", percent=100.0, resets_at=120.0)], \
        "sparse, the same window though it names no length, and not exhausted"
    assert p.feed(line({"method": "account/rateLimits/updated", "params": {"rateLimits": {
        "rateLimitReachedType": "rate_limit_reached"}}})) == ([], [])
    [ev], _ = p.feed(line({"method": "account/rateLimits/updated", "params": {"rateLimits": {
        "limitId": "Other Bucket!", "primary": {"usedPercent": 5, "windowDurationMins": 45}}}}))
    assert ev.usage[0]["window"] == "other_bucket__primary" and ev.usage[0]["label"] == "45_min", \
        "another bucket's window stays apart"
    assert p.feed(line({"method": "account/rateLimits/updated", "params": {"rateLimits": {
        "primary": {"usedPercent": "x"}}}})) == ([], []), "malformed: no reading"


def test_a_codex_turn_that_fails_on_the_usage_limit_leaves_a_failed_turn_mark():
    from test_providers_codex import TURN, THREAD
    done = {"method": "turn/completed", "params": {"threadId": THREAD, "turn": {
        "id": TURN, "status": "failed", "error": {"message": "You've hit your usage limit.",
                                                  "codexErrorInfo": "usageLimitExceeded"}}}}
    p = codex_ready()
    events, _ = p.feed(line(done))
    assert [e.kind for e in events] == ["usage", "turn_ended"] and events[0].usage == [
        report_of(window="failed_turn", exhausted=True)]
    assert events[1].ok is False and events[1].meta["error_info"] == "usageLimitExceeded"
    p = codex_ready()
    p.feed(line({"method": "account/rateLimits/updated", "params": {"rateLimits": {
        "primary": {"usedPercent": 100, "resetsAt": 4242, "windowDurationMins": 300}}}}))
    events, _ = p.feed(line(done))
    assert events[0].usage == [report_of(window="failed_turn", label="five_hour", resets_at=4242.0, exhausted=True)], \
        "with the reset of the window it last saw full (the hub decides whether it is still ahead)"
    other = done | {"params": done["params"] | {"turn": done["params"]["turn"] | {"error": {
        "message": "slow down", "codexErrorInfo": "rateLimitExceeded"}}}}
    p = codex_ready()
    assert [e.kind for e in p.feed(line(other))[0]] == ["turn_ended"], "rateLimitExceeded marks nothing"


# Lines recorded from real hosted sessions on 2026-11-22/23 (Claude Code 2.1.3xx, codex-cli 0.171.0), ids and the
# credits balance removed, as they are: what each parses to (window, label, percent, resets_at) for its tightest
# window.
SAMPLES = [json.loads(x) for x in (Path(__file__).parent / "fixtures" / "rate_limit_samples.jsonl").read_text()
           .splitlines() if x.strip()]
CODEX_57 = ("codex_primary", "seven_day", 57.0, 1795619140.0)
EXPECTED = [("seven_day", None, 41.0, 1795503600.0), ("seven_day", None, 40.0, 1795503600.0),
            ("five_hour", None, 0.0, 1795456800.0), ("five_hour", None, 0.0, 1795456800.0),
            CODEX_57, CODEX_57, CODEX_57, CODEX_57,
            ("seven_day", None, 76.0, 1795503600.0), ("seven_day", None, 77.0, 1795503600.0)]


def _parsed(sample):
    if sample["cli"] == "claude":
        p, _ = claude_session()
    else:
        p = codex_ready()
    events, replies = p.feed(line(sample["line"]))
    assert replies == [] and [e.kind for e in events] == ["usage"], sample
    return events[0].usage


@pytest.mark.parametrize("i", range(len(EXPECTED)))
def test_each_recorded_real_line_parses_to_its_percent_and_reset(i):
    assert len(SAMPLES) == len(EXPECTED) == 10
    window, label, pct, resets = EXPECTED[i]
    assert tight(_parsed(SAMPLES[i])) == report_of(window=window, label=label, percent=pct, resets_at=resets)


def test_a_recorded_line_with_two_windows_gives_both_with_their_own_resets():
    assert _parsed(SAMPLES[2]) == [report_of(percent=0.0, resets_at=1795456800.0, window="five_hour"),
                                   report_of(percent=0.0, resets_at=1795831200.0, window="seven_day")]


def test_both_recorded_shapes_of_claudes_event_work_and_without_its_internal_windows_too():
    claude = [s["line"]["rate_limit_info"] for s in SAMPLES if s["cli"] == "claude"]
    with_top = [i for i in claude if "utilization" in i]
    without_top = [i for i in claude if "utilization" not in i]
    assert len(with_top) == 2 and len(without_top) == 4, "the two shapes the recorded lines have"
    assert all(i.get("unifiedWindows") for i in claude)
    u = claude_provider.usage_of
    # unifiedWindows is internal: should it disappear, the top-level fields are read.
    bare = {k: v for k, v in with_top[0].items() if k != "unifiedWindows"}
    assert u(bare) == [report_of(percent=76.0, resets_at=1795503600.0, window="seven_day")]
    bare = {k: v for k, v in without_top[0].items() if k != "unifiedWindows"}
    assert u(bare) is None, "no utilization at all: no information, not a clear"
    # Nothing missing, null, unknown or new makes it fail.
    odd = {"status": "allowed", "rateLimitType": "seven_day", "resetsAt": None, "utilization": 0.5, "novel": [1],
           "unifiedWindows": {"five_hour": None, "seven_day": {"utilization": None}, "brand_new_window":
                              {"utilization": 0.9, "resetsAt": 1795000000, "extra": {}}}}
    assert u(odd) == [report_of(window="seven_day"),
                      report_of(percent=90.0, resets_at=1795000000.0, window="brand_new_window")], \
        "seven_day's explicit null is its clear; five_hour's null window says nothing"
    assert u({"status": "allowed", "rateLimitType": "seven_day", "resetsAt": 1795503600,
              "unifiedWindows": {"seven_day": {"utilization": 0.3}}})[0]["resets_at"] == 1795503600.0, \
        "a window without its own reset: the top-level one, for the window it names"
    assert u({"status": "allowed", "rateLimitType": "seven_day", "resetsAt": 1795503600,
              "unifiedWindows": {"five_hour": {"utilization": 0.3}}})[0]["resets_at"] is None, "never another window's"
    assert u({"status": "rejected", "rateLimitType": "five_hour", "resetsAt": None,
              "unifiedWindows": {"five_hour": {"utilization": 1.0, "resetsAt": 1795456800}}}) == [
        report_of(percent=100.0, resets_at=1795456800.0, window="five_hour", exhausted=True)]
    assert u({"status": "rejected", "resetsAt": 1795456800, "unifiedWindows": "junk", "rateLimitType": None}) == [
        report_of(resets_at=1795456800.0, window="limiting", exhausted=True)]


def test_codex_windows_may_each_be_null():
    p = codex_ready()
    assert p.feed(line({"method": "account/rateLimits/updated", "params": {"rateLimits": {
        "limitId": "codex", "primary": None, "secondary": None, "rateLimitReachedType": None}}})) == ([], [])
    [ev], _ = p.feed(line({"method": "account/rateLimits/updated", "params": {"rateLimits": {
        "limitId": "codex", "primary": None, "secondary": {"usedPercent": 12, "resetsAt": 9, "windowDurationMins": None},
        "brandNew": True}}}))
    assert ev.usage == [report_of(percent=12.0, resets_at=9.0, window="codex_secondary")]


def test_a_null_window_in_a_rolling_update_clears_nothing_the_host_keeps(penv):
    host = penv.node.sessions.host
    host.note_usage(WORK, [report_of(percent=95.0, window="codex_secondary")], penv.node.clock())
    p = codex_ready()
    [ev], _ = p.feed(line({"method": "account/rateLimits/updated", "params": {"rateLimits": {
        "primary": {"usedPercent": 3, "windowDurationMins": 300}, "secondary": None}}}))
    host.note_usage(WORK, ev.usage, penv.node.clock())
    assert {k: v["percent"] for k, v in host.usage[WORK].items()} == {"codex_secondary": 95.0, "codex_primary": 3.0}


def test_a_zero_utilization_right_after_a_reset_counts_as_room(penv):
    zero = tight(_parsed(SAMPLES[2]))
    now = penv.board.now()
    obs = zero | {"resets_at": now + 3600, "observed_at": now}
    assert account_pools.standing([obs], now, account_pools.HEADROOM_DEFAULTS)[0] == "room"
    seen(penv, WORK, percent=zero["percent"], resets_in=3600, window=zero["window"])
    s = start(penv, agent=POOL)
    assert s["agent"] == WORK and s["pool"]["why"].endswith("first choice")


# ------------------------------------------------------------ the usage record's rules (DESIGN_NOTES, the table)

H, M = 3600, 60
R = WORK        # the runner the readings are of (the work agent's own runner)


def o(window="w", percent=None, reset=None, exhausted=False, age=0, label=None):
    """A reading in a hello: `reset` is seconds from the start of the case (the hub's clock), as a time."""
    return {"window": window, "label": label, "percent": percent, "resets_at": reset, "exhausted": exhausted,
            "age": age}


# (rule, case, the hellos [(seconds after the start, runners offered, usage)], when it is judged (seconds after the
# start), the standing then, the windows kept for R then[, the hub's pool_headroom for the case])
USAGE_RULES = [
    ("identity", "a window keeps its identity when an update leaves its length out",
     [(0, [R], {R: [o("codex_primary", 98, label="five_hour")]}), (60, [R], {R: [o("codex_primary", 3)]})],
     61, "room", {"codex_primary"}),
    ("identity", "a sparse update of one window leaves another as it was",
     [(0, [R], {R: [o("codex_primary", 95)]}), (60, [R], {R: [o("codex_secondary", 3)]})],
     61, "over", {"codex_primary", "codex_secondary"}),
    ("shape", "a malformed percent replaces nothing",
     [(0, [R], {R: [o("w", 95)]}), (60, [R], {R: [o("w", "x"), o("w", 10 ** 400), o("w", -1), o("w", 2000)]})],
     61, "over", {"w"}),
    ("shape", "an omitted percent is no information and replaces nothing",
     [(0, [R], {R: [o("w", 95)]}), (60, [R], {R: [{"window": "w", "age": 0}]})], 61, "over", {"w"}),
    ("shape", "a null percent is the intentional clear",
     [(0, [R], {R: [o("w", 95)]}), (60, [R], {R: [o("w", None)]})], 61, "unknown", {"w"}),
    ("replacing", "an older reading never replaces a newer one",
     [(0, [R], {R: [o("w", 10)]}), (60, [R], {R: [o("w", 99, age=600)]})], 61, "room", {"w"}),
    ("expiry", "a reading older than stale_minutes is unknown",
     [(0, [R], {R: [o("w", 95)]}), (61 * M, [R], {})], 61 * M, "unknown", set()),
    ("expiry", "within stale_minutes it counts", [(0, [R], {R: [o("w", 95)]})], 59 * M, "over", {"w"}),
    ("expiry", "a window whose reset has passed has room",
     [(0, [R], {R: [o("w", 95, reset=60)]})], 120, "unknown", {"w"}),
    ("expiry", "... and the next hello prunes it", [(0, [R], {R: [o("w", 95, reset=60)]}), (120, [R], {})],
     120, "unknown", set()),
    ("expiry", "a reset already past when it was seen means room",
     [(0, [R], {R: [o("w", 95, reset=-60)]})], 1, "unknown", set()),
    ("exhausted", "a provider's refusal holds until its window's reset",
     [(0, [R], {R: [o("w", reset=2 * H, exhausted=True)]})], H, "exhausted", {"w"}),
    ("exhausted", "... and not after it", [(0, [R], {R: [o("w", reset=2 * H, exhausted=True)]}), (2 * H + 1, [R], {})],
     2 * H + 1, "unknown", set()),
    ("exhausted", "a provider's refusal whose reset was already past: room",
     [(0, [R], {R: [o("w", reset=-60, exhausted=True)]})], 1, "unknown", set()),
    ("exhausted", "a refusal with no reset: the cool-down", [(0, [R], {R: [o("w", exhausted=True)]})],
     29 * M, "exhausted", {"w"}),
    ("exhausted", "... and not after it", [(0, [R], {R: [o("w", exhausted=True)]})], 31 * M, "unknown", {"w"}),
    ("exhausted", "a failed turn with an inherited reset already past: the cool-down",
     [(0, [R], {R: [o("failed_turn", reset=-60, exhausted=True)]})], 10 * M, "exhausted", {"failed_turn"}),
    ("exhausted", "a failed turn with a reset still ahead: until it",
     [(0, [R], {R: [o("failed_turn", reset=2 * H, exhausted=True)]})], H, "exhausted", {"failed_turn"}),
    ("exhausted", "a high percent is not exhaustion", [(0, [R], {R: [o("w", 100)]})], 1, "over", {"w"}),
    ("standing", "the worst window decides", [(0, [R], {R: [o("a", 10), o("b", 93)]})], 1, "over", {"a", "b"}),
    ("standing", "below the threshold is room", [(0, [R], {R: [o("a", 10), o("b", 89)]})], 1, "room", {"a", "b"}),
    ("pruning", "a runner withdrawn on a hello without usage is gone",
     [(0, [R, "other"], {R: [o("w", 95)], "other": [o("w", 95)]}), (60, ["other"], {})], 61, "unknown", set()),
    ("pruning", "a reading over a day old is gone, on a hello without usage",
     [(0, [R], {R: [o("w", 50)]}), (86401, [R], {})], 86401, "unknown", set()),
    ("pruning", "an exhausted mark whose reset is still ahead stays past a day",
     [(0, [R], {R: [o("w", reset=3 * 86400, exhausted=True)]}), (86401, [R], {})], 86401, "exhausted", {"w"}),
    ("pruning", "at most sixteen windows, the newest",
     [(i, [R], {R: [o(f"w{i}", 1)]}) for i in range(20)], 20, "room", {f"w{i}" for i in range(4, 20)}),
    ("pruning", "a record is kept until its readings expire by their own rule: a three-day cool-down outlives a day",
     [(0, [R], {R: [o("w", exhausted=True)]}), (86401, [R], {})], 86401, "exhausted", {"w"},
     {"cooldown_minutes": 3 * 24 * 60}),
    ("pruning", "... and goes when it is over",
     [(0, [R], {R: [o("w", exhausted=True)]}), (3 * 86400 + 1, [R], {})], 3 * 86400 + 1, "unknown", set(),
     {"cooldown_minutes": 3 * 24 * 60}),
    ("pruning", "a longer stale_minutes keeps a reading for as long",
     [(0, [R], {R: [o("w", 95)]}), (2 * H, [R], {})], 2 * H, "over", {"w"}, {"stale_minutes": 3 * 60}),
]


@pytest.mark.parametrize("rule, case, hellos, when, state, windows, headroom",
                         [r if len(r) == 7 else r + ({},) for r in USAGE_RULES],
                         ids=[f"{r[0]}: {r[1]}" for r in USAGE_RULES])
def test_the_usage_record_follows_its_rules(penv, rule, case, hellos, when, state, windows, headroom):
    spawn_cfg(penv, pool_headroom=headroom)
    rules = account_pools.HEADROOM_DEFAULTS | headroom
    p, start_at = penv.p["laptop"], penv.board.now()
    for at, runners, usage in hellos:
        penv.clock.t = start_at + at
        usage = {k: [x | ({"resets_at": start_at + x["resets_at"]} if x.get("resets_at") is not None else {})
                     for x in v] for k, v in usage.items()}
        nodes.hello(penv.board, p, [PROJECT], runners, 1, usage=usage)
    penv.clock.t = start_at + when
    assert set(account_pools.usage_view(penv.board.conn, "laptop").get(R, {})) == windows
    observations = account_pools._observations(penv.board.conn, "laptop", R, None)
    assert account_pools.standing(observations, penv.board.now(), rules)[0] == state


def test_the_host_keeps_each_windows_latest_usage_per_runner_and_the_node_reports_its_age(penv):
    sid = start(penv, agent=WORK)["session_id"]
    tick(penv)
    _, proc = proc_of(penv, sid)
    proc.emit({"type": "rate_limit_event", "session_id": sid,
               "rate_limit_info": {"status": "allowed", "utilization": 0.3, "rateLimitType": "five_hour"}})
    tick(penv)
    penv.clock.advance(12)
    rep = penv.node.sessions.usage_report()
    assert rep == {WORK: [report_of(percent=30.0, window="five_hour", age=12.0)]}
    penv.clock.advance(86401)
    assert penv.node.sessions.usage_report() == {}, "at most a day old"
    assert stored(penv, sid)["state"] != "ended" and not show(penv, sid)["error"], "the session is untouched"


def test_choose_never_reads_anything_but_the_hubs_own_records(penv, monkeypatch):
    """Usage is advisory: a member a node says has room but that the checks refuse is still refused."""
    seen(penv, "claude", percent=0)
    seen(penv, WORK, percent=99)
    penv.board.s.dispatch = {**penv.board.s.dispatch, "agent_projects": {"claude": [P2]}}
    s = start(penv, agent=POOL)
    assert s["agent"] == WORK


def test_unknown_names_behave_as_before(penv):
    with pytest.raises(NotFound):
        hosted.start(penv.board, penv.p["human"], PROJECT, "x", agent="nobody", allowed=HUB["nodes"])


def test_the_readmes_example_loads_and_does_what_it_says():
    import re
    import tomllib
    from pathlib import Path
    text = (Path(__file__).resolve().parents[1] / "README.md").read_text()
    section = text.split("## Balancing between accounts", 1)[1].split("\n## ", 1)[0]
    [block] = re.findall(r"```toml\n(.*?)```", section, re.S)
    spawn = tomllib.loads(block)["dispatch"]["sessions"]["spawn"]
    c = DispatchConfig.from_dict({"sessions": {"spawn": spawn}}).sessions.spawn
    assert c.pools == {"claude": ["claude-work", "claude-code"]}
    order = {p: account_pools.ordered(c, "claude", p)[0] for p in
             ("/Users/you/project-b", "/Users/you/personal-app", "/Users/you/project-a", "/Users/you/open-source")}
    assert order == {"/Users/you/project-b": ["claude-work", "claude-code"],
                     "/Users/you/personal-app": ["claude-code", "claude-work"],
                     "/Users/you/project-a": ["claude-work"], "/Users/you/open-source": ["claude-code"]}
