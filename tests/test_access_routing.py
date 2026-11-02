"""Routing signed-in browser work by access ([dispatch.runner_access], access.py): a request that needs a browser signed
in to a site goes to whichever machine and agent declares that sign-in, not only to the agent it was addressed to.
Never spawns a real agent CLI or browser."""

import json

import pytest

from agent_comms import access, browser_readiness as br, dispatch, nodes, requests
from agent_comms.core import Invalid
from agent_comms.dispatch import SIGNED_IN_PROMPT, DispatchConfig

from conftest import PROJECT
from test_dispatch import allow, human_post, runs
from test_nodes import nenv  # noqa: F401  (fixture)

URL = "https://tenant.example/sites/M-EX/AppCatalog"
ORIGIN = "https://tenant.example:443"
SIGNED_IN = ["browser:https://tenant.example"]
DESKTOP = {"kind": "desktop", "transport": "playwright-mcp", "connection_id": "agent-profile"}
EVIDENCE = {"http_status": 200, "rendered_url": URL + "/Forms/AllItems.aspx", "rendered_identity": "Apps for SharePoint",
            "interaction": "open the catalog", "interaction_result": "catalog list shown"}


def signed_in_request(env, to="codex"):
    return human_post(env, [to], body="Install the preview package",
                      browser_requirement={"recipient": to, "target_url": URL, "execution_context": "desktop"})


def laptop(nenv, *, runner="codex", grant_access=True, agents=("grok", "codex")):
    """The laptop node: `runner` there holds a signed-in browser for the tenant; the hub grant lists that access."""
    nenv.d.config.nodes = {"laptop": {"projects": [PROJECT], "agents": list(agents),
                                      **({"access": [access.browser(ORIGIN)]} if grant_access else {})}}
    nenv.node.config = DispatchConfig.from_dict({
        "runners": {runner: [f"{runner}-fake", "--prompt", "{prompt}"]}, "worktrees": {PROJECT: nenv.node_dir},
        "runner_access": {runner: SIGNED_IN}, "max_concurrent": 2})
    return nenv.node.hello()


def hub_checkout(nenv, tmp_path):
    """Like the real server: the hub has the project's checkout and a codex runner, but no signed-in browser."""
    hub_dir = tmp_path / "hub-checkout"
    hub_dir.mkdir()
    nenv.d.config.worktrees = {PROJECT: str(hub_dir)}


# ---------------------------------------------------------------- configuration


def test_runner_access_is_parsed_canonical_and_tied_to_runners():
    c = DispatchConfig.from_dict({"runners": {"codex": ["codex", "{prompt}"]},
                                  "runner_access": {"codex": ["browser:https://Tenant.example/"]}})
    assert c.access_for("codex", "codex-cli") == [access.browser(ORIGIN)]
    assert c.access_for("claude", "claude-code") == []
    with pytest.raises(ValueError, match="not under"):
        DispatchConfig.from_dict({"runners": {}, "runner_access": {"codex": SIGNED_IN}})
    for bad in (["https://tenant.example"], ["browser:https://tenant.example/sites"], ["browser:https://*.x.com"], []):
        with pytest.raises(ValueError):
            DispatchConfig.from_dict({"runners": {"codex": ["codex"]}, "runner_access": {"codex": bad}})


def test_hub_grant_may_list_access():
    c = DispatchConfig.from_dict({"nodes": {"laptop": {"projects": [PROJECT], "agents": ["codex"],
                                                       "access": SIGNED_IN}}})
    assert c.nodes["laptop"]["access"] == [access.browser(ORIGIN)]


def test_a_node_reports_access_only_for_its_own_runners(nenv):
    out = laptop(nenv)
    assert out["access"] == {"codex": [access.browser(ORIGIN)]}
    with pytest.raises(Invalid):
        nodes.hello(nenv.board, nenv.p["laptop"], [PROJECT], ["grok"], 1, access={"codex": SIGNED_IN})


def test_a_node_without_access_sends_the_hello_an_older_hub_accepts(nenv):
    sent = []
    real = nenv.node.client.request
    nenv.node.client.request = lambda method, path, **kw: (sent.append(kw.get("json")), real(method, path, **kw))[1]
    nenv.node.hello()
    assert "access" not in sent[0]


# ---------------------------------------------------------------- routing to the machine that holds the sign-in


def test_signed_in_work_goes_to_the_node_that_holds_it_even_when_the_hub_has_the_checkout(nenv, tmp_path):
    hub_checkout(nenv, tmp_path)
    laptop(nenv)
    rule = allow(nenv, agents=["codex"])
    post = signed_in_request(nenv)
    nenv.d.tick()
    assert nenv.spawner.calls == [], "the hub has the checkout but not the sign-in"
    [r] = runs(nenv)
    assert r["node"] == "laptop" and r["browser_access"] == [ORIGIN] and "headless_browser" not in r, r
    nenv.node.tick()
    [call] = nenv.node_spawner.calls
    assert call["argv"][0] == "codex-fake" and SIGNED_IN_PROMPT.strip() in call["argv"][2]
    assert not any("headless_browser" in a for a in call["argv"][:2])
    # The run's session may attest a desktop probe for that origin, and then start the request.
    sid = nenv.board.register_session(nenv.p["codex"], PROJECT, dispatch_run_id=r["run_id"])["session_id"]
    attempt = br.begin_probe(nenv.board, nenv.p["codex"], sid, URL, DESKTOP)
    br.report_probe(nenv.board, nenv.p["codex"], sid, URL, DESKTOP, EVIDENCE, attempt["attempt_id"])
    requests.progress(nenv.board, nenv.p["codex"], sid, post["id"], "codex", "started", reason="installing")
    assert rule


def test_a_dispatched_run_attests_desktop_only_for_its_signed_in_origins(nenv, tmp_path):
    hub_checkout(nenv, tmp_path)
    laptop(nenv)
    allow(nenv, agents=["codex"])
    signed_in_request(nenv)
    nenv.d.tick()
    [r] = runs(nenv)
    sid = nenv.board.register_session(nenv.p["codex"], PROJECT, dispatch_run_id=r["run_id"])["session_id"]
    with pytest.raises(Invalid, match="desktop"):
        br.begin_probe(nenv.board, nenv.p["codex"], sid, "https://other.example/x", DESKTOP)


def test_the_hub_does_not_route_access_its_grant_does_not_list(nenv, tmp_path):
    hub_checkout(nenv, tmp_path)
    laptop(nenv, grant_access=False)
    allow(nenv, agents=["codex"])
    post = signed_in_request(nenv)
    nenv.d.tick()
    assert all(r.get("node") is None for r in runs(nenv))
    assert any("signed-in desktop browser" in (r.get("error") or "") for r in runs(nenv))
    [req] = nenv.board.get_post(nenv.p["human"], post["id"])["requests"]
    assert req["state"] == "blocked" and "runner_access" in req["reason"]


def test_a_node_that_lost_the_sign_in_refuses_the_assignment(nenv, tmp_path):
    hub_checkout(nenv, tmp_path)
    laptop(nenv)
    allow(nenv, agents=["codex"])
    post = signed_in_request(nenv)
    nenv.d.tick()
    nenv.node.config.runner_access = {}    # removed from the node's config meanwhile
    nenv.node.tick()
    assert nenv.node_spawner.calls == []
    run = nodes.run(nenv.board, f"s{post['seq']}-codex")
    assert run["state"] == "ended" and "runner_access" in run["error"]


def test_the_hubs_own_runner_with_the_sign_in_runs_it_locally(nenv, tmp_path):
    hub_checkout(nenv, tmp_path)
    nenv.d.config.runner_access = {"codex": [access.browser(ORIGIN)]}
    allow(nenv, agents=["codex"])
    signed_in_request(nenv)
    nenv.d.tick()
    [call] = nenv.spawner.calls
    [r] = runs(nenv)
    assert r.get("node") is None and r["browser_access"] == [ORIGIN]
    assert SIGNED_IN_PROMPT.strip() in call["argv"][-1]


# ---------------------------------------------------------------- routing to another agent that holds the sign-in


def test_work_goes_to_another_agent_that_holds_the_sign_in_and_its_completion_closes_the_original(nenv, tmp_path):
    hub_checkout(nenv, tmp_path)
    laptop(nenv, runner="grok")      # only grok, on the laptop, holds the sign-in
    allow(nenv, agents=["codex", "grok"])
    original = signed_in_request(nenv, "codex")
    nenv.d.tick()
    [routed] = runs(nenv)
    assert routed["status"] == "routed" and routed["routed_to"] == "grok"
    [row] = nenv.board.get_post(nenv.p["human"], original["id"])["requests"]
    assert row["state"] == "blocked" and "Routed by access to grok on node laptop" in row["reason"]
    successor = nenv.board.get_post(nenv.p["human"], routed["successor_post_id"])
    assert successor["to"] == ["grok"] and f"#{original['id']}" in successor["body"]
    bound = br.requirement(nenv.board, successor["id"], "grok")
    assert bound["execution_context"] == "desktop" and bound["target_url"] == URL
    # The next pass launches grok on the laptop for the successor.
    nenv.d.tick()
    launched = [r for r in runs(nenv) if r["agent"] == "grok"]
    assert len(launched) == 1 and launched[0]["node"] == "laptop" and launched[0]["browser_access"] == [ORIGIN]
    assert launched[0]["request_ids"] == [successor["id"]]
    # grok does it and finishes with evidence: the original is finished through the successor.
    sid = nenv.board.register_session(nenv.p["grok"], PROJECT, dispatch_run_id=launched[0]["run_id"])["session_id"]
    attempt = br.begin_probe(nenv.board, nenv.p["grok"], sid, URL, DESKTOP)
    br.report_probe(nenv.board, nenv.p["grok"], sid, URL, DESKTOP, EVIDENCE, attempt["attempt_id"])
    requests.progress(nenv.board, nenv.p["grok"], sid, successor["id"], "grok", "started", reason="installing")
    proof = nenv.post("grok", nenv.tid, "Installed and verified", "status")
    requests.progress(nenv.board, nenv.p["grok"], sid, successor["id"], "grok", "finished", reason="done",
                      evidence_post_ids=[proof["id"]])
    [row] = nenv.board.get_post(nenv.p["human"], original["id"])["requests"]
    assert row["state"] == "finished" and f"successor #{successor['id']}" in row["reason"]


def test_a_routed_request_is_never_routed_again(nenv, tmp_path):
    hub_checkout(nenv, tmp_path)
    laptop(nenv, runner="grok")
    allow(nenv, agents=["codex", "grok"])
    signed_in_request(nenv, "codex")
    nenv.d.tick()
    [routed] = runs(nenv)
    nenv.node.config.runner_access = {}
    nenv.node.hello()                  # grok's sign-in is gone before its run starts
    nenv.d.tick()
    grok = [r for r in runs(nenv) if r["agent"] == "grok"]
    assert len(grok) == 1 and grok[0]["status"] == "preflight_failed"
    assert not [r for r in runs(nenv) if r["status"] == "routed" and r["run_id"] != routed["run_id"]]
    successor = nenv.board.get_post(nenv.p["human"], routed["successor_post_id"])
    assert access.routed_successor(nenv.board, successor["id"])["from_agent"] == "codex"
    assert json.loads(nenv.board.conn.execute("SELECT value FROM board_state WHERE key=?", (
        "request.successor." + str(successor["id"]),)).fetchone()[0])["successor_recipient"] == "grok"


def test_no_agent_anywhere_holds_the_sign_in_blocks_with_the_reason(nenv, tmp_path):
    hub_checkout(nenv, tmp_path)
    allow(nenv, agents=["codex", "grok"])
    post = signed_in_request(nenv, "codex")
    nenv.d.tick()
    assert not [r for r in runs(nenv) if r["status"] == "routed"]
    [row] = nenv.board.get_post(nenv.p["human"], post["id"])["requests"]
    assert row["state"] == "blocked" and "signed-in desktop browser" in row["reason"]
    assert dispatch  # imported for the fixtures' monkeypatching


def test_two_signed_in_runs_on_one_node_take_turns_in_its_checkout(nenv, tmp_path):
    # The hub has its own checkout, but both runs go to the laptop: they share its checkout, so one waits.
    hub_checkout(nenv, tmp_path)
    nenv.d.config.nodes = {"laptop": {"projects": [PROJECT], "agents": ["grok", "codex"],
                                      "access": [access.browser(ORIGIN)]}}
    nenv.node.config = DispatchConfig.from_dict({
        "runners": {"codex": ["codex-fake", "{prompt}"], "grok": ["grok-fake", "{prompt}"]},
        "worktrees": {PROJECT: nenv.node_dir}, "runner_access": {"codex": SIGNED_IN, "grok": SIGNED_IN},
        "max_concurrent": 2})
    nenv.node.hello()
    other = nenv.thread("second")
    allow(nenv, agents=["codex"])
    allow(nenv, agents=["grok"], thread_id=other)
    signed_in_request(nenv, "codex")
    human_post(nenv, ["grok"], body="Install it too", thread_id=other,
               browser_requirement={"recipient": "grok", "target_url": URL, "execution_context": "desktop"})
    nenv.d.tick()
    started = [r for r in runs(nenv) if r.get("node") == "laptop"]
    assert len(started) == 1, [(r["agent"], r["status"], r.get("node")) for r in runs(nenv)]


def test_a_routed_successor_carries_the_declared_outputs(nenv, tmp_path):
    hub_checkout(nenv, tmp_path)
    laptop(nenv, runner="grok")
    allow(nenv, agents=["codex", "grok"])
    out = str(tmp_path / "preview-out")
    human_post(nenv, ["codex"], body="Install and save evidence", refs=[{"kind": "output", "path": out}],
               browser_requirement={"recipient": "codex", "target_url": URL, "execution_context": "desktop"})
    nenv.d.tick()
    [routed] = runs(nenv)
    successor = nenv.board.get_post(nenv.p["human"], routed["successor_post_id"])
    assert [r["path"] for r in successor["refs"] if r["kind"] == "output"] == [out]
    assert nenv.d._declared_outputs([successor["id"]]) == [out]


def test_no_hand_off_to_an_agent_the_linked_task_does_not_authorize(nenv, tmp_path, monkeypatch):
    hub_checkout(nenv, tmp_path)
    laptop(nenv, runner="grok")
    allow(nenv, agents=["codex", "grok"])
    task = nenv.board.create_task(nenv.p["human"], nenv.sid["human"], nenv.tid, title="Install preview")
    real = nenv.board._task_authorization_active
    monkeypatch.setattr(nenv.board, "_task_authorization_active",
                        lambda t, agent=None: agent != "grok" and real(t, agent))
    post = human_post(nenv, ["codex"], body="Install the preview package", task_id=task["id"],
                      browser_requirement={"recipient": "codex", "target_url": URL, "execution_context": "desktop"})
    nenv.d.tick()
    assert not [r for r in runs(nenv) if r["status"] == "routed"]
    [row] = nenv.board.get_post(nenv.p["human"], post["id"])["requests"]
    assert row["state"] == "blocked" and "Routed by access" not in row["reason"]
