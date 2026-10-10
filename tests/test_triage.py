"""The triage agent (DESIGN_NOTES "Triage agent"): a distinct Haiku identity with a read-only runner that owns the
prevention inbox and forwards proposals that need code to the maintainer, under a one-shot rule. Never spawns a real
agent CLI."""

import json
import os
import re
import stat
import sys

import pytest

from agent_comms import cli, dispatch, human_actions, prevention, runner_preflight as rp
from agent_comms.config import (REPO_ROOT, PreventionConfig, Settings, agent_mcp_config, create_agent,
                                load_agent_token, prevention_config)
from agent_comms.core import Conflict, Forbidden, Invalid
from agent_comms.dispatch import DispatchConfig

from conftest import PROJECT
from test_autorecover import INJECTION, aenv  # noqa: F401
from test_dispatch import allow, denv, human_post  # noqa: F401
from test_prevention import forward, penv, stalled_unstick  # noqa: F401

TRIAGE = "claude-haiku"


def shipped_triage_runner() -> list[str]:
    """The commented "claude-haiku" runner template in board.toml, uncommented and parsed."""
    import tomllib
    text = (REPO_ROOT / "board.toml").read_text()
    start = text.index('# "claude-haiku" = [')
    lines = []
    for line in text[start:].splitlines():
        assert line.startswith("#"), "the template is one commented block"
        lines.append(line[2:] if line.startswith("# ") else line[1:])
        if line.strip() == "# ]":
            break
    return tomllib.loads("\n".join(lines))[TRIAGE]


# ---------------------------------------------------------------- the runner


def test_the_shipped_triage_runner_is_valid_narrow_and_off_by_default():
    template = shipped_triage_runner()
    assert dispatch.validate_runner(TRIAGE, template) == template
    assert dispatch.risky_flags(template) == []
    # "haiku" is the alias Claude Code 2.1.282 recognizes (it warns unrecognized_model for "claude-haiku-5-5").
    assert template[:5] == ["claude", "-p", "{prompt}", "--model", "haiku"]
    assert template[template.index("--permission-mode") + 1] == "dontAsk"
    assert "--strict-mcp-config" in template and "--mcp-config" in template
    allowed = next(a for a in template if a.startswith("--allowedTools="))
    assert rp.split_tools(allowed.split("=", 1)[1]) == [
        "mcp__agent-comms", "Read(./**)", "Grep(./**)", "Glob(./**)", "Bash(git log *)", "Bash(git show *)",
        "Bash(gh pr view *)", "Bash(gh pr list *)"]
    denied = set(rp.split_tools(next(a for a in template if a.startswith("--disallowedTools=")).split("=", 1)[1]))
    assert {"Edit", "Write", "NotebookEdit", "Bash(git *--output*)", "Read(~/.config/agent-comms/**)",
            "Read(~/.ssh/**)", "Read(./data/**)"} <= denied
    assert not any(t in allowed for t in ("Edit", "Write", "Bash(git commit", "Bash(git push", "Bash(gh pr merge"))
    assert rp.read_only(template)
    # Shipped commented out: the committed runners are unchanged.
    shipped = Settings.load(REPO_ROOT / "board.toml", local=False).dispatch["runners"]
    assert TRIAGE not in shipped and set(shipped) == {"codex-cli", "claude-code"}


def test_it_is_keyed_by_the_agents_own_name_so_the_runtime_default_never_launches_it():
    template = shipped_triage_runner()
    default = ["claude", "-p", "{prompt}", "--permission-mode", "dontAsk", "--allowedTools=mcp__agent-comms"]
    config = DispatchConfig.from_dict({"runners": {"claude-code": default, TRIAGE: template}})
    assert config.runner_for(TRIAGE, "claude-code") == template
    assert config.runner_for("claude-code", "claude-code") == default
    assert config.runner_key(TRIAGE, "claude-code") == TRIAGE


@pytest.mark.parametrize("change, expected", [
    (lambda t: t, True),
    (lambda t: [x for x in t if not x.startswith("--disallowedTools")], False),      # not declared read-only
    (lambda t: [x.replace(",Glob", ",Glob,Edit") if x.startswith("--allowedTools") else x for x in t], False),
    (lambda t: [x.replace(",Glob", ",Bash") if x.startswith("--allowedTools") else x for x in t], False),
    (lambda t: [x.replace(",Glob", ",Bash(git commit *)") if x.startswith("--allowedTools") else x for x in t], False),
    (lambda t: [("acceptEdits" if x == "dontAsk" else x) for x in t], False),
    (lambda t: t + ["--settings", "/tmp/s.json"], False),
    (lambda t: t + ["--dangerously-skip-permissions"], False),
    (lambda t: ["claude-fake"] + t[1:], False),
    (lambda t: [x for x in t if not x.startswith("--allowedTools")], False),
    # Review of #60: bare reads reach any file; the token and ssh denies, the git --output deny and
    # --strict-mcp-config are required.
    (lambda t: [x.replace("Read(./**)", "Read") if x.startswith("--allowedTools") else x for x in t], False),
    (lambda t: [x.replace("Grep(./**)", "Grep") if x.startswith("--allowedTools") else x for x in t], False),
    (lambda t: [x.replace("Glob(./**)", "Glob(/**)") if x.startswith("--allowedTools") else x for x in t], False),
    (lambda t: [x.replace(",Read(~/.config/agent-comms/**)", "") for x in t], False),
    (lambda t: [x.replace(",Read(~/.ssh/**)", "") for x in t], False),
    (lambda t: [x.replace(",Bash(git *--output*)", "") for x in t], False),
    (lambda t: [x for x in t if x != "--strict-mcp-config"], False),
    (lambda t: [x.replace(",Bash(git *--output*)", "").replace(",Bash(git log *),Bash(git show *)", "")
                for x in t], True),       # no git allowed: the --output deny is not needed
    (lambda t: [x.replace(",Glob(./**)", ",Glob(./**),Bash(git diff *)") if x.startswith("--allowedTools") else x
                for x in t], False),      # git diff --no-index reads any file
])
def test_read_only_is_an_explicit_narrow_declaration(change, expected):
    assert rp.read_only(change(shipped_triage_runner())) is expected


def test_the_shipped_claude_code_runner_is_not_read_only():
    """Its board-only tools are widened on purpose in an opted-in project (claude_tool_projects)."""
    shipped = Settings.load(REPO_ROOT / "board.toml", local=False).dispatch["runners"]["claude-code"]
    assert rp.read_only(shipped) is False


def test_split_tools_keeps_parenthesized_rules_whole():
    assert rp.split_tools("a,Bash(git log *), Read Glob") == ["a", "Bash(git log *)", "Read", "Glob"]


def test_an_opted_in_project_keeps_the_triage_runners_narrow_tools(denv):
    """claude_tool_projects replaces a Claude runner's tools with the scoped implementation set and runs a tool
    preflight first. A read-only runner is launched exactly as written instead: no Edit/Write, no commits."""
    template = shipped_triage_runner()
    denv.d.config.runners["claude"] = template
    denv.d.config.claude_tool_projects = [PROJECT]
    allow(denv, agents=["claude"], max_launches=1)
    human_post(denv, ["claude"])
    denv.d.tick()
    [call] = denv.spawner.calls
    argv = call["argv"]
    assert "--session-id" not in argv and "--resume" not in argv and "--max-turns" not in argv
    assert next(a for a in argv if a.startswith("--allowedTools=")) == next(
        a for a in template if a.startswith("--allowedTools="))
    assert not any("Edit(" in a or "Write(" in a or "git commit" in a for a in argv)
    rec = denv.d._get(denv.d.RUN_PREFIX + denv.d.running["claude"].run_id)
    assert "tool_preflight" not in rec


def test_an_opted_in_project_still_scopes_an_ordinary_claude_runner(denv):
    denv.d.config.runners["claude"] = ["claude", "-p", "{prompt}", "--permission-mode", "dontAsk",
                                       "--allowedTools=mcp__agent-comms"]
    denv.d.config.claude_tool_projects = [PROJECT]
    allow(denv, agents=["claude"], max_launches=1)
    human_post(denv, ["claude"])
    denv.d.tick()
    assert "--session-id" in denv.spawner.calls[0]["argv"]


# ---------------------------------------------------------------- identity and token


def test_one_token_maps_to_one_identity(tmp_path):
    path = tmp_path / "agents.toml"
    create_agent(path, "human", "human", is_human=True)
    maintainer = create_agent(path, "claude-code", "claude-code")
    triage = create_agent(path, TRIAGE, "claude-code")
    from agent_comms.core import Board
    board = Board(Settings(db_path=tmp_path / "b.db", agents_path=path))
    assert board.authenticate(triage).name == TRIAGE
    assert board.authenticate(maintainer).name == "claude-code"
    assert board.authenticate(triage).runtime == "claude-code"
    hashes = [r[0] for r in board.conn.execute("SELECT token_hash FROM agents")]
    assert len(hashes) == len(set(hashes))


def run_cli(monkeypatch, tmp_path, *args):
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    (home / "board.toml").write_text(f'[server]\ndb_path = "{tmp_path / "b.db"}"\n'
                                     f'agents_path = "{tmp_path / "agents.toml"}"\n')
    monkeypatch.setenv("AGENT_COMMS_HOME", str(home))
    monkeypatch.setenv("AGENT_COMMS_TOKEN_DIR", str(tmp_path / "tokens"))
    monkeypatch.setattr(sys, "argv", ["board", *args])
    cli.main(list(args))


def test_create_agent_can_save_the_token_where_board_mcp_reads_it(monkeypatch, tmp_path, capsys):
    run_cli(monkeypatch, tmp_path, "create-agent", TRIAGE, "--runtime", "claude-code", "--save-token")
    out = capsys.readouterr().out
    token_file = tmp_path / "tokens" / f"{TRIAGE}.token"
    token = load_agent_token(TRIAGE)
    assert token.startswith("ac_") and token not in out and str(token_file) in out
    assert stat.S_IMODE(token_file.stat().st_mode) == 0o600
    assert stat.S_IMODE(token_file.parent.stat().st_mode) == 0o700
    with pytest.raises(SystemExit, match="already exists"):
        run_cli(monkeypatch, tmp_path, "create-agent", TRIAGE, "--runtime", "claude-code", "--save-token")
    assert load_agent_token(TRIAGE) == token
    run_cli(monkeypatch, tmp_path, "create-agent", TRIAGE, "--runtime", "claude-code", "--save-token", "--rotate")
    assert load_agent_token(TRIAGE) != token


def test_save_token_keeps_the_old_token_valid_when_agents_toml_cannot_be_updated(monkeypatch, tmp_path, capsys):
    """Review of #60, P3-5: the new token is staged privately first and replaces the token file only after agents.toml
    accepted it; an existing token directory is tightened to 700."""
    from agent_comms import config
    tokens = tmp_path / "tokens"
    tokens.mkdir(mode=0o755)
    os.chmod(tokens, 0o755)
    run_cli(monkeypatch, tmp_path, "create-agent", TRIAGE, "--runtime", "claude-code", "--save-token")
    assert stat.S_IMODE(tokens.stat().st_mode) == 0o700
    old = load_agent_token(TRIAGE)
    agents_before = (tmp_path / "agents.toml").read_text()

    def broken(path, agents):
        raise OSError("disk full")
    monkeypatch.setattr(config, "write_agents", broken)
    with pytest.raises(OSError, match="disk full"):
        run_cli(monkeypatch, tmp_path, "create-agent", TRIAGE, "--runtime", "claude-code", "--save-token", "--rotate")
    assert load_agent_token(TRIAGE) == old
    assert (tmp_path / "agents.toml").read_text() == agents_before
    assert sorted(x.name for x in tokens.iterdir()) == [f"{TRIAGE}.token"], "no staged copy left behind"
    from agent_comms.core import Board
    board = Board(Settings(db_path=tmp_path / "check.db", agents_path=tmp_path / "agents.toml"))
    assert board.authenticate(old).name == TRIAGE


def test_mcp_config_signs_the_run_in_as_that_agent_without_secrets(monkeypatch, tmp_path, capsys):
    run_cli(monkeypatch, tmp_path, "create-agent", TRIAGE, "--runtime", "claude-code", "--save-token")
    capsys.readouterr()
    run_cli(monkeypatch, tmp_path, "mcp-config", "--agent", TRIAGE)
    out = capsys.readouterr().out
    path = tmp_path / "tokens" / f"{TRIAGE}.mcp.json"
    data = json.loads(path.read_text())
    server = data["mcpServers"]["agent-comms"]
    assert server["env"] == {"AGENT_COMMS_HOME": str(tmp_path / "home"), "AGENT_COMMS_AGENT": TRIAGE}
    assert server["command"] == "bash" and server["args"][0].endswith("integrations/claude-code/stdio.sh")
    assert os.path.isabs(server["args"][0])
    assert load_agent_token(TRIAGE) not in path.read_text()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert f'"--mcp-config", "{path}"' in out
    assert data == agent_mcp_config(TRIAGE)
    with pytest.raises(SystemExit):
        run_cli(monkeypatch, tmp_path, "mcp-config", "--agent", "nobody")
    with pytest.raises(SystemExit, match="already exists"):
        run_cli(monkeypatch, tmp_path, "mcp-config", "--agent", TRIAGE)
    run_cli(monkeypatch, tmp_path, "mcp-config", "--agent", TRIAGE, "--force")


def test_the_stdio_launcher_honors_the_agent_named_in_the_config():
    text = (REPO_ROOT / "integrations" / "claude-code" / "stdio.sh").read_text()
    assert re.search(r'--agent "\$\{AGENT_COMMS_AGENT:-claude\}"', text)


# ---------------------------------------------------------------- configuration


def test_prevention_forward_to_is_validated_strictly():
    assert prevention_config({"prevention_owner": TRIAGE, "prevention_thread": 14,
                              "prevention_forward_to": "claude-code"}) == PreventionConfig(TRIAGE, 14, "claude-code")
    assert prevention_config({"prevention_owner": TRIAGE, "prevention_thread": 14,
                              "prevention_forward_to": ""}) == PreventionConfig(TRIAGE, 14)
    for bad, match in (({"prevention_forward_to": "claude-code"}, "needs prevention_owner"),
                       ({"prevention_owner": TRIAGE, "prevention_thread": 14, "prevention_forward_to": TRIAGE},
                        "another agent"),
                       ({"prevention_owner": TRIAGE, "prevention_thread": 14, "prevention_forward_to": "Claude Code"},
                        "agent name"),
                       ({"prevention_owner": TRIAGE, "prevention_thread": 14, "prevention_forward_to": 3},
                        "agent name")):
        with pytest.raises(ValueError, match=match):
            prevention_config(bad)


def test_the_shipped_inbox_stays_off():
    shipped = Settings.load(REPO_ROOT / "board.toml", local=False)
    assert shipped.unstick == {"prevention_owner": "", "prevention_thread": 0, "prevention_forward_to": ""}
    assert prevention_config(shipped.unstick) is None


# ---------------------------------------------------------------- the triage owner and forwarding


@pytest.fixture
def tenv(penv):
    """penv with a claude-haiku identity owning the inbox and forwarding to claude (the maintainer)."""
    penv.tokens[TRIAGE] = create_agent(penv.settings.agents_path, TRIAGE, "claude-code")
    penv.board.sync_agents(force=True)
    penv.p[TRIAGE] = penv.board.authenticate(penv.tokens[TRIAGE])
    penv.sid[TRIAGE] = penv.session(TRIAGE)
    penv.d.config.runners[TRIAGE] = ["claude-haiku-fake", "-p", "{prompt}"]
    penv.board.s.unstick = {"prevention_owner": TRIAGE, "prevention_thread": penv.inbox,
                            "prevention_forward_to": "claude"}
    penv.clock.advance(5 * 60)
    return penv


def proposal(env):
    """codex, Unstuck by the human, sends its prevention proposal to the triage owner, which is launched once."""
    req, _ = stalled_unstick(env)
    env.d.tick()                                     # launches codex for the Unstick
    prop = forward(env, req["id"], to=[TRIAGE])
    env.spawner.children[-1].code = 0
    env.clock.advance(5 * 60)
    env.d.tick()
    env.d.tick()
    assert env.spawner.agents()[-1] == "claude-haiku-fake"
    return req, prop


def send_on(env, prop, agent=TRIAGE, **kw):
    fields = dict(body="Needs a code change: " + INJECTION, type="request", thread_id=env.inbox, to=["claude"],
                  needs_response=True, prevention_for=prop["id"])
    fields.update(kw)
    return env.board.create_post(env.p[agent], kw.pop("session_id", env.sid[agent]), **fields)


def test_the_owner_forwards_once_and_the_maintainer_is_launched_once(tenv):
    req, prop = proposal(tenv)
    assert prevention.status(tenv.board)["forward_to"] == "claude"
    assert prevention.status(tenv.board)["forward_problem"] is None
    tenv.board.s.max_agent_posts_per_thread_without_human = 1
    tenv.post("grok", tenv.inbox, "fill the cap")     # the inbox is at its agent-post cap now
    fwd = send_on(tenv, prop)
    assert fwd["prevention_for"] == {"request_post_id": req["id"], "source_thread_id": tenv.tid,
                                     "forward_of": prop["id"]}
    assert fwd["id"] not in [p["id"] for p in tenv.board.snapshot(tenv.p["human"])["needs_you"]]
    rule_id = human_actions.post_rule_id(tenv.board, fwd["id"])
    rule = next(r for r in tenv.board.list_dispatch_rules(tenv.p["human"]) if r["id"] == rule_id)
    assert rule["agents"] == ["claude"] and rule["max_launches"] == 1 and rule["thread_id"] == tenv.inbox
    assert rule["purpose"] == prevention.FORWARD_PURPOSE.format(
        thread=tenv.inbox, owner=TRIAGE, proposal=prop["id"], request=req["id"], source=tenv.tid, post=fwd["id"])
    assert "IGNORE" not in rule["purpose"]
    tenv.spawner.children[-1].code = 0               # the triage run ends
    tenv.clock.advance(5 * 60)
    tenv.d.tick()
    tenv.d.tick()
    assert tenv.spawner.agents()[-1] == "claude-fake"
    launches = len(tenv.spawner.calls)
    tenv.spawner.children[-1].code = 0
    tenv.clock.advance(5 * 60)
    for _ in range(3):
        tenv.d.tick()
    assert len(tenv.spawner.calls) == launches, "one launch, never repeated"
    assert "IGNORE" not in " ".join(tenv.spawner.calls[-1]["argv"])
    with pytest.raises(Conflict, match="already forwarded"):
        send_on(tenv, prop)


@pytest.mark.parametrize("agent", ["codex", "claude", "grok", "human"])
def test_only_the_owner_can_forward(tenv, agent):
    _, prop = proposal(tenv)
    with pytest.raises(Forbidden):
        send_on(tenv, prop, agent=agent)
    assert not [r for r in tenv.board.list_dispatch_rules(tenv.p["human"]) if r["agents"] == ["claude"]
                and r["thread_id"] == tenv.inbox]


@pytest.mark.parametrize("change, error", [
    ({"thread_id": "main"}, Invalid), ({"to": ["claude", "codex"]}, Invalid), ({"to": ["codex"]}, Invalid),
    ({"to": []}, Invalid), ({"type": "status"}, Invalid), ({"needs_response": False}, Invalid),
    ({"sealed": True}, Invalid)])
def test_only_a_well_formed_forward_qualifies(tenv, change, error):
    _, prop = proposal(tenv)
    change = dict(change)
    if change.get("thread_id") == "main":
        change["thread_id"] = tenv.tid
    before = tenv.board.conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0]
    with pytest.raises(error):
        send_on(tenv, prop, **change)
    assert tenv.board.conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0] == before


def test_a_forward_cannot_be_forwarded_again(tenv):
    _, prop = proposal(tenv)
    fwd = send_on(tenv, prop)
    with pytest.raises(Invalid, match="already a forward"):
        send_on(tenv, fwd)


def test_no_forwarding_unless_configured(tenv):
    _, prop = proposal(tenv)
    tenv.board.s.unstick = {"prevention_owner": TRIAGE, "prevention_thread": tenv.inbox}
    with pytest.raises(Conflict, match="no prevention_forward_to"):
        send_on(tenv, prop)
    tenv.board.s.unstick = {"prevention_owner": TRIAGE, "prevention_thread": tenv.inbox,
                            "prevention_forward_to": "nobody"}
    assert "not an active agent" in prevention.status(tenv.board)["forward_problem"]
    with pytest.raises(Conflict, match="not an active agent"):
        send_on(tenv, prop, to=["codex"])


def test_an_old_proposal_is_not_forwarded(tenv):
    _, prop = proposal(tenv)
    tenv.clock.advance(prevention.REQUEST_MAX_AGE_SECONDS + 60)
    with pytest.raises(Conflict, match="7 days"):
        send_on(tenv, prop)


def test_a_spent_forward_budget_refuses_the_forward_so_it_can_be_sent_later(tenv):
    """Review of #60, P3-7: past the daily budget the forward is refused (nothing posted, nothing consumed)."""
    _, prop = proposal(tenv)
    now = tenv.board.now()
    tenv.board.conn.execute("INSERT INTO board_state(key, value, updated_by, updated_at) VALUES (?, ?, 'x', ?)",
                            (prevention.FORWARD_LAUNCHES_KEY, json.dumps([now] * prevention.DAILY_FORWARD_LAUNCHES),
                             now))
    before = tenv.board.conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0]
    with pytest.raises(Conflict, match="budget of 10 forward launches is spent"):
        send_on(tenv, prop)
    assert tenv.board.conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0] == before
    assert tenv.board.conn.execute("SELECT 1 FROM board_state WHERE key = ?",
                                   (prevention.FORWARD_PREFIX + str(prop["id"]),)).fetchone() is None
    tenv.clock.advance(prevention.WINDOW_SECONDS + 60)
    tenv.board.s.unstick = dict(tenv.board.s.unstick)
    fwd = send_on(tenv, prop)
    assert human_actions.post_rule_id(tenv.board, fwd["id"]) is not None
    assert fwd["prevention_for"]["forward_of"] == prop["id"]


def test_a_forward_grants_nothing_thread_wide(tenv):
    _, prop = proposal(tenv)
    send_on(tenv, prop)
    plain = tenv.post(TRIAGE, tenv.inbox, "also look at this", "request", to=["claude"], needs_response=True)
    assert human_actions.post_rule_id(tenv.board, plain["id"]) is None


def test_a_stall_request_is_still_answered_with_a_proposal_not_a_forward(tenv):
    """prevention_for naming an Unstick request keeps its meaning: the stalled agent's own proposal."""
    req, _ = stalled_unstick(tenv)
    prop = forward(tenv, req["id"], to=[TRIAGE])
    assert "forward_of" not in prop["prevention_for"]
    assert human_actions.post_rule_id(tenv.board, prop["id"]) is not None
