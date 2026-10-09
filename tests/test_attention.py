"""Selective recovery closeout must not hide neighboring proposals or complete audits."""
import pytest
from fastapi.testclient import TestClient

from agent_comms import attention, db
from agent_comms.api import create_app
from agent_comms.core import Conflict, Forbidden, Invalid, NotFound, Paused

from conftest import ASK


def close(env, post, evidence, who="codex", **kw):
    return attention.close_attention(env.board, env.p[who], kw.pop("session_id", env.sid[who]),
                                     post["id"], kw.pop("reason", "Browser access recovered; audit unfinished."),
                                     kw.pop("evidence_post_ids", [evidence["id"]]), **kw)


def setup(env, who="codex"):
    thread = env.thread()
    post = env.post(who, thread, "Browser unavailable", "question", needs_response=True, decision_question=ASK)
    evidence = env.post("codex", thread, "Verified desktop browser interaction; old workers unverified")
    return thread, post, evidence


def test_selective_three_thread_recovery_preserves_proposals_and_audits(env):
    cases = []
    for _ in range(3):
        thread = env.thread()
        audit = env.post("claude", thread, "Audit all controls", "request", to=["codex"], needs_response=True)
        proposal = env.post("codex", thread, "Add a preflight", "proposal", decision_question=ASK)
        blocker = env.post("codex", thread, "Browser unavailable", "question", needs_response=True,
                           decision_question=ASK)
        proof = env.post("codex", thread, "Browser interaction verified; audit incomplete")
        cases.append((thread, audit, proposal, blocker, proof))
    for thread, audit, proposal, blocker, proof in cases:
        result = close(env, blocker, proof)
        assert result["needs_response"] is True  # immutable source intent retained
        assert result["body"] == blocker["body"] and result["seq"] > blocker["seq"]
        assert result["attention_resolution"] == {
            "resolved_by": "codex", "session_id": env.sid["codex"],
            "reason": "Browser access recovered; audit unfinished.",
            "evidence_post_ids": [proof["id"]], "resolved_at": result["revised_at"]}
        assert env.board.get_post(env.p["human"], audit["id"])["needs_response"]
        posts = env.board.list_posts(env.p["human"], thread)["posts"]
        assert len(posts) == 4 and all(p["agent"] != "human" for p in posts)
        assert env.board._thread_row(thread)["status"] == "open"
    assert {p["id"] for p in env.board.snapshot(env.p["human"])["needs_you"]} == {c[2]["id"] for c in cases}
    # Additive migration/restart keeps the closeout and its evidence.
    db.init_schema(env.board.conn)
    assert env.board.get_post(env.p["human"], cases[0][3]["id"])["attention_resolution"]


def test_authorship_project_session_pause_and_decision_guards(env):
    thread, post, evidence = setup(env)
    with pytest.raises(Forbidden, match="own authored"):
        close(env, post, evidence, who="claude")
    with pytest.raises(Forbidden, match="different agent"):
        close(env, post, evidence, session_id=env.sid["claude"])
    wrong_project = env.session("codex", project="/other")
    with pytest.raises(Forbidden, match="source project"):
        close(env, post, evidence, session_id=wrong_project)
    decision = env.post("codex", thread, "Choose an approach", "decision", decision_question=ASK)
    with pytest.raises(Forbidden, match="decision"):
        close(env, decision, evidence)
    env.board.set_paused(env.p["human"], True)
    with pytest.raises(Paused):
        close(env, post, evidence)
    env.board.set_paused(env.p["human"], False)
    # A different session of the authentic author may recover an old blocked session.
    close(env, post, evidence, session_id=env.session("codex"))
    with pytest.raises(Conflict):
        close(env, post, evidence)
    result = close(env, decision, evidence, who="human")
    assert result["decision_status"].startswith("proposal")  # not an approval


def test_evidence_scope_and_sealed_visibility(env):
    thread, post, evidence = setup(env)
    other = env.post("codex", env.thread(), "other thread")
    for e in (post, other):
        with pytest.raises(Invalid, match="source thread"):
            close(env, post, e)
    hidden = env.post("claude", thread, "secret", sealed=True)
    with pytest.raises(NotFound):
        close(env, post, hidden)
    with pytest.raises(Forbidden, match="unseal"):
        close(env, post, hidden, who="human")
    private = env.post("codex", thread, "private source", "question", sealed=True, needs_response=True,
                       decision_question=ASK)
    close(env, private, evidence)
    with pytest.raises(NotFound):
        env.board.get_post(env.p["claude"], private["id"])
    assert private["id"] not in {p["id"] for p in env.board.list_posts(env.p["claude"], thread)["posts"]}


@pytest.mark.parametrize("ids", [[], [True], [0], [-1], [1, 1], ["1"], list(range(1, 22))])
def test_evidence_ids_strict(env, ids):
    _, post, proof = setup(env)
    with pytest.raises(Invalid):
        close(env, post, proof, evidence_post_ids=ids)


def test_nonattention_and_linked_sources_cannot_be_closed(env):
    from agent_comms import issues
    thread, post, proof = setup(env)
    with pytest.raises(Conflict):
        close(env, proof, post)
    issues.create_issue(env.board, env.p["codex"], env.sid["codex"], title="Browser", body="blocked",
                        thread_id=thread, post_id=post["id"], decision_question=ASK)
    with pytest.raises(Conflict, match="shared issue"):
        close(env, post, proof)


def test_http_requires_auth_and_returns_authentic_audit(env):
    _, post, proof = setup(env)
    c = TestClient(create_app(env.board))
    url = f'/api/posts/{post["id"]}/attention/resolve'
    body = {"session_id": env.sid["codex"], "reason": "Verified browser; audit remains open",
            "evidence_post_ids": [proof["id"]]}
    assert c.post(url, json=body).status_code == 401
    headers = {"Authorization": f'Bearer {env.tokens["codex"]}'}
    assert c.post(url, json=body | {"resolved_by": "human"}, headers=headers).status_code == 422
    assert c.post(url, json=body | {"evidence_post_ids": [True]}, headers=headers).status_code == 422
    r = c.post(url, json=body, headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["attention_resolution"]["resolved_by"] == "codex"
    assert c.post(url, json=body, headers=headers).status_code == 409


def test_mcp_closeout_uses_credential_and_reports_audit(env, monkeypatch):
    import asyncio
    import json
    from mcp import Client
    from agent_comms.mcp_server import build_mcp
    _, post, proof = setup(env)
    monkeypatch.setenv("AGENT_COMMS_TOKEN", env.tokens["codex"])

    async def run():
        async with Client(build_mcp(env.board, "stdio")) as client:
            await client.call_tool("board_register", {"project": "/work/repo"})
            return await client.call_tool("board_resolve_attention", {
                "post_id": post["id"], "reason": "Verified desktop browser; audit remains open",
                "evidence_post_ids": [proof["id"]]})
    result = asyncio.run(run())
    assert not result.is_error
    assert json.loads(result.content[0].text)["attention_resolution"]["resolved_by"] == "codex"
