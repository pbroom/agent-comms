"""Browser requirements are declared before dispatch, without granting browser access.

All environments and spawners are fake; no browser or external agent is started.
"""
import asyncio
import json
import os
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest

from agent_comms import browser_readiness as br, db, requests
from agent_comms.core import Conflict, Invalid
from conftest import PROJECT
from test_dispatch import allow, denv, human_post, runs  # noqa: F401
from test_dispatch_headless_browser import benv, browser_args  # noqa: F401

URL = "https://tenant.example/sites/dev/about"
ORIGIN = "https://tenant.example:443"
DESKTOP = {"kind": "desktop", "transport": "iab", "connection_id": "tab-1"}
HEADLESS = {"kind": "headless", "transport": "playwright-mcp", "connection_id": "run-browser"}
EVIDENCE = {"http_status": 200, "rendered_url": URL, "rendered_identity": "DEV About 1.43.23",
            "interaction": "Open About", "interaction_result": "Version displayed"}


def requirement(kind="desktop", **fields):
    return {"recipient": "codex", "target_url": URL, "execution_context": kind, **fields}


def new_request(env, declaration):
    return env.board.create_post(env.p["human"], env.sid["human"], body="Verify the existing DEV release",
                                 type="request", to=["codex"], new_thread_title="Browser contract",
                                 browser_requirement=declaration)


def counts(env):
    return {table: env.board.conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in ("posts", "threads", "request_progress", "browser_requirements")}


@pytest.mark.parametrize("kind", ["desktop", "headless", "either"])
def test_creation_binds_the_browser_requirement_before_the_request_is_visible(env, kind):
    post = new_request(env, requirement(kind))
    bound = dict(br.requirement(env.board, post["id"], "codex"))
    assert bound == {"post_id": post["id"], "recipient": "codex", "target_url": URL,
                     "origin": ORIGIN, "execution_context": kind}
    row = env.board.get_post(env.p["human"], post["id"])["requests"][0]
    assert row["state"] == "queued" and row["assigned_session"] is None
    assert not br.eligible(env.board, env.sid["codex"], post["id"], "codex")


@pytest.mark.parametrize("declaration", [
    {}, [], "desktop", requirement("browser"), requirement(execution_context=None),
    requirement(recipient="claude"), requirement(recipient="nobody"), requirement(recipient=""),
    requirement(target_url="https://*.example.com/"), requirement(target_url="file:///private/file"),
    requirement(target_url=None), {"recipient": "codex", "target_url": URL},
    {**requirement(), "permission_granted": True},
])
def test_invalid_creation_contract_leaves_no_post_thread_or_request(env, declaration):
    before = counts(env)
    with pytest.raises(Invalid):
        new_request(env, declaration)
    assert counts(env) == before


def test_browser_declaration_is_refused_on_non_request_posts(env):
    before = counts(env)
    with pytest.raises(Invalid):
        env.board.create_post(env.p["human"], env.sid["human"], body="A status is not a request",
                              type="status", to=["codex"], new_thread_title="No request",
                              browser_requirement=requirement())
    assert counts(env) == before


@pytest.mark.parametrize('author,recipient', [('codex', 'codex'), ('codex', 'human')])
def test_declaration_requires_a_real_nonhuman_original_obligation(env, author, recipient):
    before = counts(env)
    with pytest.raises(Invalid, match='nonhuman original'):
        env.board.create_post(env.p[author], env.sid[author], body='No valid browser obligation',
                              type='request', to=[recipient], new_thread_title='No obligation',
                              browser_requirement=requirement(recipient=recipient))
    assert counts(env) == before


def test_binding_does_not_offer_rerouting_around_a_real_policy_denial(env):
    post = new_request(env, requirement())
    br.report_failure(env.board, env.p['codex'], env.sid['codex'], URL, DESKTOP,
                      'policy_denied', 'The browser host refused this origin')
    result = br.bind_request(env.board, env.p['human'], env.sid['human'], post['id'], 'codex', URL, 'desktop')
    assert result['human_action_required'] is True and result['reroute_required'] is False
    assert 'Do not reroute' in result['routing_reason']


def test_failure_while_storing_the_binding_rolls_back_the_entire_new_thread(env, monkeypatch):
    before = counts(env)
    def failed_binding(*args, **kwargs):
        raise RuntimeError("simulated persistence failure")
    monkeypatch.setattr(br, "store_requirement", failed_binding)
    with pytest.raises(RuntimeError, match="persistence failure"):
        new_request(env, requirement())
    assert counts(env) == before


@pytest.mark.parametrize("replacement", [requirement("headless"), requirement(target_url=URL + "/other")])
def test_bound_target_and_execution_context_cannot_be_rewritten(env, replacement):
    post = new_request(env, requirement())
    with pytest.raises(Conflict, match="immutable"):
        br.bind_request(env.board, env.p["human"], env.sid["human"], post["id"], "codex",
                        replacement["target_url"], replacement["execution_context"])
    assert dict(br.requirement(env.board, post["id"], "codex"))["execution_context"] == "desktop"
    assert br.requirement(env.board, post["id"], "codex")["target_url"] == URL


def test_rebinding_the_same_requirement_is_idempotent(env):
    post = new_request(env, requirement())
    result = br.bind_request(env.board, env.p["human"], env.sid["human"], post["id"], "codex", URL, "desktop")
    assert result["reroute_required"] is False
    assert result["human_action_required"] is False
    assert env.board.conn.execute("SELECT count(*) FROM browser_requirements").fetchone()[0] == 1


@pytest.mark.parametrize("kind,attached,reroute", [
    ("desktop", [ORIGIN], True), ("headless", [], True), ("either", [], True),
    ("headless", [ORIGIN], False), ("either", [ORIGIN], False),
])
def test_late_binding_reports_missing_execution_context_without_requesting_new_authority(env, kind, attached, reroute):
    tid = env.thread()
    post = env.post("human", tid, type="request", to=["codex"])
    env.board.conn.execute("INSERT INTO board_state(key,value,updated_at) VALUES (?,?,?)",
        ("dispatch.run.contract-worker", json.dumps({"agent": "codex", "thread_id": tid, "status": "running",
                                                    "headless_browser": {"origins": attached}}), env.clock()))
    sid = env.board.register_session(env.p["codex"], PROJECT, dispatch_run_id="contract-worker")["session_id"]
    result = br.bind_request(env.board, env.p["codex"], sid, post["id"], "codex", URL, kind)
    assert result["reroute_required"] is reroute and result["human_action_required"] is False
    assert env.board.conn.execute("SELECT count(*) FROM browser_permission_gates").fetchone()[0] == 0
    if reroute:
        assert result["routing_reason"]


@pytest.mark.parametrize("inherited", [False, True])
def test_desktop_work_cannot_consume_a_launch_or_receive_a_headless_browser(benv, inherited):
    if not inherited:
        rule = allow(benv, agents=["codex"], max_launches=3)
    original = human_post(benv, ["codex"], browser_requirement=requirement())
    if inherited:
        benv.clock.advance(1)
        rule = allow(benv, agents=["codex"], max_launches=3)
        wrapper = human_post(benv, ["codex"], body="Unstick the original request")
        from agent_comms import recovery
        with db.write_tx(benv.board.conn):
            recovery.record(benv.board, benv.p['human'], wrapper['id'], 'codex',
                            [{'post_id': original['id'], 'recipient': 'codex', 'version': 0}])
    benv.d.tick()
    assert not benv.spawner.calls
    assert any(r["status"] == "preflight_failed" and "desktop" in r["error"].lower() for r in runs(benv))
    [stored] = benv.board.list_dispatch_rules(benv.p["human"])
    assert stored["id"] == rule["id"] and stored["launches_left"] == 3
    assert benv.board.get_post(benv.p["human"], original["id"])["requests"][0]["state"] != "started"
    assert not list(benv.log_dir.glob("*-browser"))


def test_unrelated_code_request_does_not_inherit_a_desktop_restriction_from_thread_text(benv):
    human_post(benv, ['codex'], browser_requirement=requirement())
    benv.clock.advance(1)
    allow(benv, agents=['codex'], max_launches=3)
    follow_up = human_post(benv, ['codex'], body='Unstick or ignore the browser; this text creates no lineage')
    benv.d.tick()
    assert len(benv.spawner.calls) == 1
    record = runs(benv)[0]
    assert record['request_ids'] == [follow_up['id']] and 'headless_browser' not in record


@pytest.mark.parametrize('kind', ['headless', 'either'])
def test_recovery_source_requires_a_browser_even_when_no_headless_runner_is_configured(benv, kind):
    from agent_comms import recovery
    original = human_post(benv, ['codex'], browser_requirement=requirement(kind))
    benv.clock.advance(1)
    allow(benv, agents=['codex'], max_launches=3)
    wrapper = human_post(benv, ['codex'])
    with db.write_tx(benv.board.conn):
        recovery.record(benv.board, benv.p['human'], wrapper['id'], 'codex',
                        [{'post_id': original['id'], 'recipient': 'codex', 'version': 0}])
    benv.config.headless_browser.runners = []
    benv.d.tick()
    assert not benv.spawner.calls
    assert 'generic CLI' in runs(benv)[0]['error']
    assert benv.board.list_dispatch_rules(benv.p['human'])[0]['launches_left'] == 3


def test_recovery_wrapper_cannot_erase_an_exact_source_policy_denial(benv):
    from agent_comms import recovery
    original = human_post(benv, ['codex'], browser_requirement=requirement('headless'))
    benv.clock.advance(1)
    allow(benv, agents=['codex'], max_launches=3)
    wrapper = human_post(benv, ['codex'])
    with db.write_tx(benv.board.conn):
        recovery.record(benv.board, benv.p['human'], wrapper['id'], 'codex',
                        [{'post_id': original['id'], 'recipient': 'codex', 'version': 0}])
    br.report_failure(benv.board, benv.p['claude'], benv.sid['claude'], URL, DESKTOP,
                      'policy_denied', 'Browser host explicitly denied this origin')
    benv.d.tick()
    assert not benv.spawner.calls
    assert 'policy denied' in runs(benv)[0]['error']
    assert benv.board.list_dispatch_rules(benv.p['human'])[0]['launches_left'] == 3


@pytest.mark.parametrize("kind", ["headless", "either"])
def test_compatible_contract_injects_only_its_bound_origin(benv, kind):
    allow(benv, agents=["codex"], max_launches=3)
    post = human_post(benv, ["codex"], browser_requirement=requirement(kind))
    benv.d.tick()
    assert len(benv.spawner.calls) == 1
    args = browser_args(benv.spawner.calls[0]["argv"])
    assert args[args.index("--allowed-origins") + 1] == ORIGIN
    assert "--headless" in args and "--isolated" in args
    assert runs(benv)[0]["request_ids"] == [post["id"]]


@pytest.mark.parametrize("required,context", [("desktop", HEADLESS), ("headless", DESKTOP)])
def test_successful_wrong_kind_probe_cannot_start_or_finish_a_bound_request(env, required, context):
    post = new_request(env, requirement(required))
    sid = env.sid["codex"]
    attempt = br.begin_probe(env.board, env.p["codex"], sid, URL, context)
    br.report_probe(env.board, env.p["codex"], sid, URL, context, EVIDENCE, attempt["attempt_id"])
    assert br.readiness(env.board, sid, URL) == "ready"  # Real proof, but for the wrong execution kind.
    assert not br.eligible(env.board, sid, post["id"], "codex")
    for state in ("started", "finished"):
        with pytest.raises(Conflict, match="execution_context_mismatch"):
            requests.progress(env.board, env.p["codex"], sid, post["id"], "codex", state,
                              reason="Browser proof reported")
    correct = {**context, "kind": required}
    attempt = br.begin_probe(env.board, env.p["codex"], sid, URL, correct)
    br.report_probe(env.board, env.p["codex"], sid, URL, correct, EVIDENCE, attempt["attempt_id"])
    assert br.eligible(env.board, sid, post["id"], "codex")
    assert requests.progress(env.board, env.p["codex"], sid, post["id"], "codex", "started")["state"] == "started"


def test_schema13_upgrade_preserves_existing_requests_and_defaults_to_either(env):
    post = env.post("human", env.thread(), body="Existing browser work", type="request", to=["codex"])
    br.bind_request(env.board, env.p["human"], env.sid["human"], post["id"], "codex", URL)
    c = env.board.conn
    original_post = env.board.get_post(env.p["human"], post["id"])
    for operation in ("insert", "update", "delete"):
        for table in ("requests", "bindings"):
            c.execute(f"DROP TRIGGER IF EXISTS browser_context_{table}_{operation}")
    c.execute("ALTER TABLE browser_requirements DROP COLUMN execution_context")
    c.execute("PRAGMA user_version=13")
    for _ in range(2):
        db.init_schema(c)
        assert c.execute("PRAGMA user_version").fetchone()[0] == 14
        assert dict(br.requirement(env.board, post["id"], "codex")) == {
            "post_id": post["id"], "recipient": "codex", "target_url": URL,
            "origin": ORIGIN, "execution_context": "either"}
        assert env.board.get_post(env.p["human"], post["id"]) == original_post
    assert c.execute("SELECT count(*) FROM browser_requirements").fetchone()[0] == 1


@pytest.mark.parametrize("mutation", [
    "UPDATE request_progress SET state='started' WHERE post_id=?",
    "DELETE FROM request_progress WHERE post_id=?",
    "UPDATE browser_requirements SET execution_context='either' WHERE post_id=?",
    "DELETE FROM browser_requirements WHERE post_id=?",
])
def test_old_schema13_writer_cannot_ignore_or_erase_the_new_context_contract(env, mutation):
    post = new_request(env, requirement())
    requests.progress(env.board, env.p["codex"], env.sid["codex"], post["id"], "codex", "blocked",
                      reason="Awaiting the required execution context")
    c = env.board.conn
    c.execute("BEGIN IMMEDIATE")
    try:
        # A v13 process has the existing writer permit, but cannot enforce execution_context.
        c.execute("INSERT INTO managed_write_permit(id) VALUES (1)")
        with pytest.raises(sqlite3.IntegrityError, match="browser context requires an updated server"):
            c.execute(mutation, (post["id"],))
    finally:
        c.rollback()
    assert br.requirement(env.board, post["id"], "codex")["execution_context"] == "desktop"
    assert env.board.get_post(env.p["human"], post["id"])["requests"][0]["state"] == "blocked"
    assert c.execute("SELECT count(*) FROM browser_context_write_permit").fetchone()[0] == 0


def test_http_post_and_bind_preserve_execution_contract(env):
    from fastapi.testclient import TestClient
    from agent_comms.api import create_app
    client = TestClient(create_app(env.board))
    headers = {"Authorization": "Bearer " + env.tokens["human"], "X-Board-Session": str(env.sid["human"])}
    fields = {"body": "Verify existing browser release", "type": "request", "to": ["codex"],
              "new_thread_title": "HTTP browser contract", "browser_requirement": requirement()}
    response = client.post("/api/posts", headers=headers, json=fields)
    assert response.status_code == 200, response.text
    post_id = response.json()["id"]
    assert br.requirement(env.board, post_id, "codex")["execution_context"] == "desktop"
    before = counts(env)
    invalid = client.post("/api/posts", headers=headers, json={**fields,
                         "browser_requirement": requirement(recipient="claude")})
    assert invalid.status_code == 400, invalid.text
    assert counts(env) == before
    rebound = client.post(f"/api/requests/{post_id}/browser", headers=headers, json={"recipient": "codex",
                          "target_url": URL, "execution_context": "headless"})
    assert rebound.status_code == 409, rebound.text


def test_mcp_creation_and_bind_forward_the_browser_execution_contract(env, monkeypatch):
    from mcp import Client
    from agent_comms.mcp_server import build_mcp
    monkeypatch.setenv("AGENT_COMMS_TOKEN", env.tokens["codex"])
    server = build_mcp(env.board, "stdio")
    async def run():
        async with Client(server) as client:
            async def call(name, arguments):
                result = await client.call_tool(name, arguments)
                assert not result.is_error, result
                return json.loads(result.content[0].text)
            await call("board_register", {"project": PROJECT})
            post = await call("board_post", {"body": "Inspect in a desktop browser", "type": "request",
                              "to": ["claude"], "new_thread_title": "MCP browser contract",
                              "browser_requirement": requirement(recipient="claude")})
            assert br.requirement(env.board, post["id"], "claude")["execution_context"] == "desktop"
            rebound = await call("board_bind_browser_request", {"post_id": post["id"], "recipient": "claude",
                                 "target_url": URL, "execution_context": "desktop"})
            assert rebound["execution_context"] == "desktop" and rebound["human_action_required"] is False
    asyncio.run(run())


def test_git_execution_directory_validation_checks_the_actual_checkout(tmp_path):
    from agent_comms import dispatch
    git = shutil.which("git")
    if not git:
        pytest.skip("git is unavailable")
    group = tmp_path / "projects"
    checkout = group / "release"
    checkout.mkdir(parents=True)
    subprocess.run([git, "init", "--quiet", str(checkout)], check=True, capture_output=True)
    execution_env = {**os.environ, "PATH": str(Path(git).parent)}
    assert dispatch.git_run_directory_blocker(str(checkout), execution_env) is None
    assert dispatch.git_run_directory_blocker(str(group), execution_env)
    assert dispatch.git_run_directory_blocker(str(tmp_path / "missing"), execution_env)


def test_git_preflight_failure_does_not_spend_budget_or_spawn(benv, monkeypatch):
    from agent_comms import dispatch
    checked = []
    def refuse(cwd, execution_env):
        checked.append(cwd)
        return "Actual execution directory is not a trusted Git checkout"
    monkeypatch.setattr(dispatch, "git_run_directory_blocker", refuse)
    allow(benv, agents=["codex"], max_launches=3)
    human_post(benv, ["codex"])
    benv.d.tick()
    assert checked == [benv.workdir]
    assert not benv.spawner.calls
    assert runs(benv)[0]["status"] == "preflight_failed" and "Git checkout" in runs(benv)[0]["error"]
    assert benv.board.list_dispatch_rules(benv.p["human"])[0]["launches_left"] == 3
