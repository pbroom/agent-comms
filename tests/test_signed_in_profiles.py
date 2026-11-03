"""Signed-in browser profiles ([dispatch.browser_profiles]): a Codex run launched for signed-in work on an origin gets
the persistent profile the human signed in to, as the `signed_in_browser` MCP server, for that run only. Never spawns a
real agent CLI or browser."""

import tomllib

import pytest

from agent_comms import access, nodes
from agent_comms.dispatch import (SIGNED_IN_BROWSER_SERVER, SIGNED_IN_BROWSER_TOOLS, DispatchConfig, WRITABLE_ROOTS_KEY,
                                  signed_in_profile)

from conftest import PROJECT
from test_access_routing import ORIGIN, URL, hub_checkout, signed_in_request
from test_dispatch import allow, human_post, runs
from test_dispatch_headless_browser import BROWSER, CODEX, overrides_of
from test_nodes import nenv  # noqa: F401  (fixture)

PROFILE = "/Users/you/Library/Application Support/Agent Comms/browser-profiles/tenant"


def node_config(nenv, *, profiles=True):
    return DispatchConfig.from_dict({
        "runners": {"codex": CODEX}, "worktrees": {PROJECT: nenv.node_dir}, "max_concurrent": 2,
        "headless_browser": BROWSER, "runner_access": {"codex": ["browser:https://tenant.example"]},
        **({"browser_profiles": {"https://tenant.example": {"user_data_dir": PROFILE}}} if profiles else {})})


@pytest.fixture
def laptop(nenv, tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.setattr("agent_comms.node.shutil.which", lambda executable, **kw: "/fake/" + executable)
    hub_checkout(nenv, tmp_path)
    nenv.d.config.nodes = {"laptop": {"projects": [PROJECT], "agents": ["codex"], "access": [access.browser(ORIGIN)]}}
    nenv.node.config = node_config(nenv)
    nenv.node.hello()
    allow(nenv, agents=["codex"])
    return nenv


def server_args(argv):
    o = overrides_of(argv)
    return tomllib.loads("v = " + o[f"mcp_servers.{SIGNED_IN_BROWSER_SERVER}.args"])["v"], o


def test_profiles_are_validated_and_need_the_browser_server_command():
    c = DispatchConfig.from_dict({"runners": {"codex": CODEX}, "headless_browser": BROWSER,
                                  "browser_profiles": {"https://Tenant.example/": {"user_data_dir": PROFILE}}})
    assert c.browser_profiles == {ORIGIN: PROFILE}
    assert signed_in_profile(c, "codex", "codex-cli", [ORIGIN]) == PROFILE
    assert signed_in_profile(c, "codex", "codex-cli", [ORIGIN, "https://other.example:443"]) is None
    with pytest.raises(ValueError, match="headless_browser"):
        DispatchConfig.from_dict({"browser_profiles": {"https://tenant.example": {"user_data_dir": PROFILE}}})
    for bad in ({"user_data_dir": "relative/dir"}, {"user_data_dir": "/a/../b"}, {"user_data_dir": "/"}, {},
                {"user_data_dir": PROFILE, "args": ["--x"]}):
        with pytest.raises(ValueError):
            DispatchConfig.from_dict({"headless_browser": BROWSER, "browser_profiles": {"https://tenant.example": bad}})
    with pytest.raises(ValueError):
        DispatchConfig.from_dict({"headless_browser": BROWSER,
                                  "browser_profiles": {"https://tenant.example/sites": {"user_data_dir": PROFILE}}})


def test_a_signed_in_run_on_the_node_gets_the_profile_for_that_run_only(laptop, tmp_path):
    post = signed_in_request(laptop)
    laptop.d.tick()
    laptop.node.tick()
    [call] = laptop.node_spawner.calls
    args, o = server_args(call["argv"])
    out_dir = tmp_path / "node-logs" / f"s{post['seq']}-codex-browser"
    assert args[args.index("--user-data-dir") + 1] == PROFILE and "--isolated" not in args
    assert "--headless" in args and args[args.index("--output-dir") + 1] == str(out_dir) and out_dir.is_dir()
    assert tomllib.loads("v = " + o[f"mcp_servers.{SIGNED_IN_BROWSER_SERVER}.enabled_tools"])["v"] == list(
        SIGNED_IN_BROWSER_TOOLS)
    assert "browser_file_upload" in SIGNED_IN_BROWSER_TOOLS and "browser_evaluate" not in SIGNED_IN_BROWSER_TOOLS
    assert str(out_dir) in tomllib.loads("v = " + o[WRITABLE_ROOTS_KEY])["v"]
    assert "mcp_servers.headless_browser.args" not in o
    laptop.node_spawner.children[0].code = 0
    laptop.node.tick()
    assert not out_dir.exists(), "the browser output goes once the run has ended"


def test_ordinary_work_never_gets_the_profile(laptop):
    human_post(laptop, ["codex"], body="Refactor the parser")
    laptop.d.tick()
    laptop.node.tick()
    for call in laptop.node_spawner.calls:
        assert not any(f"mcp_servers.{SIGNED_IN_BROWSER_SERVER}" in a for a in call["argv"])


def test_without_a_profile_the_runner_runs_with_its_own_config(laptop):
    laptop.node.config = node_config(laptop, profiles=False)
    signed_in_request(laptop)
    laptop.d.tick()
    laptop.node.tick()
    [call] = laptop.node_spawner.calls
    assert not any(f"mcp_servers.{SIGNED_IN_BROWSER_SERVER}" in a for a in call["argv"])


def test_a_codex_config_that_defines_the_server_name_refuses_the_run(laptop, tmp_path):
    codex = tmp_path / "home" / ".codex"
    codex.mkdir()
    (codex / "config.toml").write_text(f'[mcp_servers.{SIGNED_IN_BROWSER_SERVER}]\ncommand = "evil"\n')
    post = signed_in_request(laptop)
    laptop.d.tick()
    laptop.node.tick()
    assert laptop.node_spawner.calls == []
    run = nodes.run(laptop.board, f"s{post['seq']}-codex")
    assert run["state"] == "ended" and SIGNED_IN_BROWSER_SERVER in run["error"]
    assert URL


def test_a_node_without_the_browser_command_refuses_the_signed_in_run(laptop, monkeypatch):
    monkeypatch.setattr("agent_comms.node.shutil.which", lambda executable, **kw: None)
    post = signed_in_request(laptop)
    laptop.d.tick()
    laptop.node.tick()
    assert laptop.node_spawner.calls == []
    assert "browser command is unavailable" in nodes.run(laptop.board, f"s{post['seq']}-codex")["error"]


@pytest.fixture
def hub(nenv, tmp_path, monkeypatch):
    """The hub's own Codex runner holds the sign-in and has the profile."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.setattr("agent_comms.dispatch.shutil.which", lambda executable, **kw: "/fake/" + executable)
    hub_checkout(nenv, tmp_path)
    c = node_config(nenv)
    nenv.d.config.runners, nenv.d.config.headless_browser = c.runners, c.headless_browser
    nenv.d.config.runner_access, nenv.d.config.browser_profiles = c.runner_access, c.browser_profiles
    return nenv


def test_the_hub_does_not_launch_signed_in_work_without_the_browser_command(hub, monkeypatch):
    monkeypatch.setattr("agent_comms.dispatch.shutil.which",
                        lambda executable, **kw: None if executable == "npx" else "/fake/" + executable)
    allow(hub, agents=["codex"], max_launches=2)
    signed_in_request(hub)
    hub.d.tick()
    assert hub.spawner.calls == []
    assert hub.board.list_dispatch_rules(hub.p["human"])[0]["launches_left"] == 2
    assert runs(hub)[0]["error"] == "required signed-in browser command is unavailable"


def test_a_recovery_linking_signed_in_and_headless_work_shares_one_browser_directory(hub):
    from agent_comms import browser_readiness as br, recovery
    desktop = signed_in_request(hub)
    headless = human_post(hub, ["codex"], body="Check the local preview")
    br.bind_request(hub.board, hub.p["human"], hub.sid["human"], headless["id"], "codex", "http://127.0.0.1:5185/about")
    hub.clock.advance(1)
    allow(hub, agents=["codex"])
    wrapper = human_post(hub, ["codex"], body="Recover the install")
    hub.board.conn.execute("INSERT INTO board_state(key, value, updated_at) VALUES (?, ?, 0)", (
        recovery.PREFIX + f"{wrapper['id']}.codex", '{"sources": [{"post_id": %d, "recipient": "codex"}, '
        '{"post_id": %d, "recipient": "codex"}]}' % (desktop["id"], headless["id"])))
    hub.board.conn.commit()
    hub.d.tick()
    [call] = hub.spawner.calls
    [r] = [r for r in runs(hub) if r["status"] == "running"]
    o = overrides_of(call["argv"])
    assert f"mcp_servers.{SIGNED_IN_BROWSER_SERVER}.args" in o and "mcp_servers.headless_browser.args" in o
    assert r["signed_in_browser"]["output_dir"] == r["headless_browser"]["output_dir"]
