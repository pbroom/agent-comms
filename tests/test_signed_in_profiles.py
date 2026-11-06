"""Signed-in browser profiles ([dispatch.browser_profiles]) do not satisfy a desktop-bound request: a dispatched run would
drive the profile through a headless Playwright server, not the human's signed-in desktop browser, and only land on a
sign-in page. Such a request is held at dispatch with "needs desktop pickup". Never spawns a real agent CLI or
browser."""

import pytest

from agent_comms import access, nodes
from agent_comms.dispatch import SIGNED_IN_BROWSER_SERVER, DispatchConfig, signed_in_profile

from conftest import PROJECT
from test_access_routing import ORIGIN, URL, hub_checkout, signed_in_request
from test_dispatch import allow, human_post, runs
from test_dispatch_headless_browser import BROWSER, CODEX
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


def headless_overrides(argv):
    return [a for a in argv if f"mcp_servers.{SIGNED_IN_BROWSER_SERVER}" in a or "--headless" in a]


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


def test_a_desktop_bound_request_is_held_not_launched_with_the_headless_profile_on_the_node(laptop):
    """The node's Codex runner has only a [dispatch.browser_profiles] profile for the origin: it never gets the run."""
    assert laptop.node.config.access_for("codex", "codex-cli") == []
    post = signed_in_request(laptop)
    laptop.d.tick()
    laptop.node.tick()
    assert laptop.node_spawner.calls == [] and laptop.spawner.calls == []
    assert all(r.get("node") is None for r in runs(laptop))
    [req] = laptop.board.get_post(laptop.p["human"], post["id"])["requests"]
    assert req["state"] == "blocked" and "needs desktop pickup" in req["reason"]


def test_ordinary_work_never_gets_the_profile(laptop):
    human_post(laptop, ["codex"], body="Refactor the parser")
    laptop.d.tick()
    laptop.node.tick()
    for call in laptop.node_spawner.calls:
        assert not any(f"mcp_servers.{SIGNED_IN_BROWSER_SERVER}" in a for a in call["argv"])


def test_without_a_profile_the_runner_runs_with_its_own_config(laptop):
    laptop.node.config = node_config(laptop, profiles=False)
    laptop.node.hello()
    signed_in_request(laptop)
    laptop.d.tick()
    laptop.node.tick()
    [call] = laptop.node_spawner.calls
    assert headless_overrides(call["argv"]) == []


@pytest.fixture
def hub(nenv, tmp_path, monkeypatch):
    """The hub's own Codex runner declares the sign-in and has the profile."""
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


def test_the_hub_holds_a_desktop_bound_request_instead_of_launching_it_with_the_headless_profile(hub):
    allow(hub, agents=["codex"], max_launches=2)
    post = signed_in_request(hub)
    hub.d.tick()
    assert hub.spawner.calls == [], "no Codex run is launched with the headless signed_in_browser override"
    assert hub.board.list_dispatch_rules(hub.p["human"])[0]["launches_left"] == 2
    assert runs(hub)[0]["status"] == "preflight_failed" and "needs desktop pickup" in runs(hub)[0]["error"]
    [req] = hub.board.get_post(hub.p["human"], post["id"])["requests"]
    assert req["state"] == "blocked" and "needs desktop pickup" in req["reason"]
    hub.clock.advance(600)
    hub.d.tick()
    assert hub.spawner.calls == [], "the preflight retry pass keeps it held"


def test_signed_in_runs_on_one_machine_take_turns(hub, monkeypatch):
    """Even in separate run directories (parallel worktrees), one signed-in run per machine at a time. Here the
    runner's own config provides the signed-in browser (no profile)."""
    hub.d.config.browser_profiles = {}
    monkeypatch.setattr(type(hub.d), "_busy_dirs", lambda self, foreign: set())
    hub.d.config.max_runs_per_agent = 2
    other = hub.thread("second install")
    allow(hub, agents=["codex"])
    allow(hub, agents=["codex"], thread_id=other)
    signed_in_request(hub)
    human_post(hub, ["codex"], body="Install the other package", thread_id=other,
               browser_requirement={"recipient": "codex", "target_url": URL, "execution_context": "desktop"})
    hub.d.tick()
    assert len(hub.spawner.calls) == 1, "the second signed-in run waits for the first"
    hub.spawner.children[0].code = 0
    hub.d.tick()
    hub.clock.advance(600)
    hub.d.tick()
    assert len(hub.spawner.calls) == 2
