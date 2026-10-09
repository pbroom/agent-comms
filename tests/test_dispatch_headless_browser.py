"""Dispatcher: approvals and the scoped headless browser for dispatched Codex runs ([dispatch.headless_browser]).
Never spawns a real agent CLI or browser."""

import stat
import tomllib
from pathlib import Path

import pytest

from agent_comms import browser_readiness as br, dispatch, requests
from agent_comms.core import Invalid
from agent_comms.dispatch import DispatchConfig, HeadlessBrowserConfig, build_prompt

from conftest import PROJECT
from test_dispatch import ROOT, allow, denv, human_post, runs  # noqa: F401  (denv is a fixture)

URL = "http://127.0.0.1:5185/about"
ORIGIN = "http://127.0.0.1:5185"
OTHER = "http://localhost:6006/iframe.html"
HEADLESS = {"kind": "headless", "transport": "playwright-mcp", "connection_id": "run-browser"}
DESKTOP = {"kind": "desktop", "transport": "iab", "connection_id": "tab-1"}
EVIDENCE = {"http_status": 200, "rendered_url": URL, "rendered_identity": "NEXUS About LOCAL 1.43.17",
            "interaction": "open About", "interaction_result": "About dialog shows the version"}
CODEX = (["codex", "exec", "--cd", "{project}", "--sandbox", "workspace-write"]
         + [a for t in dispatch.CODEX_PREAPPROVED_TOOLS for a in ("-c", dispatch.codex_approval_override(t))]
         + ["{prompt}"])
BROWSER = {"runners": ["codex"], "command": "npx", "args": ["--offline", "-y", "@playwright/mcp@0.0.83"]}


def config(**browser) -> DispatchConfig:
    return DispatchConfig.from_dict({"runners": {"codex": CODEX, "claude": ["claude", "-p", "{prompt}"]},
                                     "headless_browser": {**BROWSER, **browser}})


@pytest.fixture
def benv(denv, tmp_path, monkeypatch):  # noqa: F811
    """denv with a real-shaped Codex runner (all approvals) given the headless browser, and an empty HOME (the
    dispatcher reads the run's Codex config)."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("CODEX_HOME", raising=False)
    denv.home = home
    fresh = config()
    denv.config.runners = fresh.runners
    denv.config.headless_browser = fresh.headless_browser
    return denv


def bound_request(env, url=URL, to="codex"):
    post = human_post(env, [to], body="Browser audit of the About dialog")
    br.bind_request(env.board, env.p["human"], env.sid["human"], post["id"], to, url)
    return post


def overrides_of(argv: list[str]) -> dict[str, str]:
    """{key: raw TOML value} for every -c pair, each parsed the way Codex parses it (as TOML)."""
    out = {}
    for i, x in enumerate(argv):
        if x == "-c":
            key, _, value = argv[i + 1].partition("=")
            tomllib.loads(f"v = {value}")   # every value must be valid TOML
            out[key] = value
    return out


def browser_args(argv: list[str]) -> list[str]:
    return tomllib.loads("v = " + overrides_of(argv)["mcp_servers.headless_browser.args"])["v"]


# ---------------------------------------------------------------- configuration


def test_shipped_config_is_off_and_has_no_personal_paths():
    shipped = DispatchConfig.load(ROOT / "board.toml", local=False)
    assert shipped.headless_browser == HeadlessBrowserConfig()
    assert shipped.headless_browser.runners == [] and not shipped.headless_browser_for("codex", "codex-cli")
    assert str(Path.home()) not in (ROOT / "board.toml").read_text()


def test_valid_config_and_lookup_by_runner_key():
    c = config(browser="msedge")
    assert c.headless_browser.command == "npx" and c.headless_browser.browser == "msedge"
    assert c.headless_browser_for("codex", "codex-cli")
    assert not c.headless_browser_for("claude", "claude-code")
    # by runtime: an agent without its own entry uses its runtime's runner key
    by_runtime = DispatchConfig.from_dict({"runners": {"codex-cli": CODEX},
                                           "headless_browser": {**BROWSER, "runners": ["codex-cli"]}})
    assert by_runtime.headless_browser_for("codex", "codex-cli")
    assert not by_runtime.headless_browser_for("other", "grok")
    assert DispatchConfig.from_dict({"headless_browser": {"runners": []}}).headless_browser.command == ""


@pytest.mark.parametrize("browser,match", [
    ({"runners": ["claude"]}, "not a Codex runner"),
    ({"runners": ["nobody"]}, "not under"),
    ({"runners": "codex"}, "list of distinct"),
    ({"runners": ["codex", "codex"]}, "list of distinct"),
    ({"command": ""}, "command"),
    ({"command": "bash"}, "never a shell"),
    ({"command": "/bin/zsh"}, "never a shell"),
    ({"command": "{prompt}"}, "command"),
    ({"args": "--headless"}, "list of strings"),
    ({"args": ["--allowed-origins", "*"]}, "set by the dispatcher"),
    ({"args": ["--allowed-origins=*"]}, "set by the dispatcher"),
    ({"args": ["--headless"]}, "set by the dispatcher"),
    ({"args": ["--output-dir", "/tmp/x"]}, "set by the dispatcher"),
    ({"args": ["--browser=firefox"]}, "set by the dispatcher"),
    ({"args": ["--block-service-workers"]}, "set by the dispatcher"),
    ({"args": ["--offline", "\x7f"]}, "not allowed"),
    ({"args": ["@playwright/mcp@0.0.83\n--extension"]}, "not allowed"),
    ({"command": "npx\x7f"}, "command"),
    *[({"args": [flag]}, "not a known-harmless option|is not allowed") for flag in (
        "--extension", "--cdp-endpoint=http://127.0.0.1:9222", "--user-data-dir", "--config", "--endpoint",
        "--allow-unrestricted-file-access", "--init-script", "--init-page", "--no-sandbox", "--caps=devtools",
        "--executable-path", "--ignore-https-errors", "--secrets", "--storage-state", "--proxy-server=http://p:1",
        "--proxy-bypass", "--grant-permissions", "--save-session", "--shared-browser-context", "--port", "--host",
        "--allowed-hosts", "--profile-dir-name", "--cdp-header", "-p", "--package=@evil/mcp", "--some-future-flag")],
    ({"args": ["{prompt}"]}, "not allowed"),
    ({"args": ["--dangerously-bypass-approvals-and-sandbox"]}, "not allowed"),
    ({"browser": "lynx"}, "browser must be"),
    ({"executable": "x"}, "unknown setting"),
])
def test_invalid_config_is_refused(browser, match):
    with pytest.raises(ValueError, match=match):
        config(**browser)


def test_command_required_when_enabled_and_errors_do_not_echo_values():
    with pytest.raises(ValueError, match="needs a command"):
        DispatchConfig.from_dict({"runners": {"codex": CODEX}, "headless_browser": {"runners": ["codex"]}})
    with pytest.raises(ValueError) as e:
        config(args=["--offline", "--secrets=/home/someone/.env"])
    assert "someone" not in str(e.value) and "element 1" in str(e.value)


def test_allowlisted_options_are_accepted():
    c = config(args=["--offline", "-y", "@playwright/mcp@0.0.83", "--viewport-size", "1280x720",
                     "--timeout-navigation=30000", "--console-level", "error", "--no-webmcp"])
    assert c.headless_browser.args[-1] == "--no-webmcp"
    assert config(command="node", args=["/opt/pw/node_modules/@playwright/mcp/cli.js"]).headless_browser.command == "node"


@pytest.mark.parametrize("override", [
    'mcp_servers.headless_browser.tools.browser_run_code_unsafe.approval_mode="approve"',
    'mcp_servers.headless_browser={command="node", args=["x.js"]}',
    'mcp_servers."headless_browser".tools.browser_evaluate.approval_mode="approve"',
    "mcp_servers.'headless_browser'.env={PLAYWRIGHT_MCP_ALLOWED_ORIGINS=\"*\"}",
    '"mcp_servers"."headless_browser".enabled_tools=["browser_evaluate"]',
    'mcp_servers . headless_browser . env_vars=["PLAYWRIGHT_MCP_CONFIG"]',
    'mcp_servers={headless_browser={command="node"}}',
])
@pytest.mark.parametrize("form", ["-c", "--config", "joined"])
def test_runner_that_configures_the_browser_server_itself_is_refused(override, form):
    extra = {"-c": ["-c", override], "--config": ["--config", override], "joined": ["--config=" + override]}[form]
    own = CODEX[:-1] + extra + ["{prompt}"]
    # Inline-table forms contain braces, which validate_runner already refuses in any runner element.
    with pytest.raises(ValueError, match="owns that server|placeholders must be whole"):
        DispatchConfig.from_dict({"runners": {"codex": own}, "headless_browser": BROWSER})
    if "{" not in override:   # fine while that runner is not given the browser (its config is the human's)
        assert DispatchConfig.from_dict({"runners": {"codex": own}}).runners["codex"] == own


def test_local_overlay_enables_it(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_COMMS_HOME", str(tmp_path))
    (tmp_path / "board.toml").write_text((ROOT / "board.toml").read_text())
    (tmp_path / "board.local.toml").write_text(
        '[dispatch.headless_browser]\nruners = []\n')
    with pytest.raises(ValueError, match="unknown setting"):
        DispatchConfig.load()
    (tmp_path / "board.local.toml").write_text(
        '[dispatch.headless_browser]\nrunners = ["codex-cli"]\ncommand = "npx"\n'
        'args = ["--offline", "-y", "@playwright/mcp@0.0.83"]\n')
    c = DispatchConfig.load()
    assert c.headless_browser.runners == ["codex-cli"] and c.headless_browser.browser == "chrome"
    assert c.headless_browser_for("codex", "codex-cli")


def test_dashboard_cannot_edit_it(env):
    from agent_comms import board_settings
    with pytest.raises(Invalid, match="edit board.local.toml by hand"):
        board_settings.validate_changes({"dispatch.headless_browser.runners": ["codex-cli"]})


# ---------------------------------------------------------------- the injected server


def test_overrides_are_scoped_and_never_approve_unsafe_tools(tmp_path):
    out = str(tmp_path / 'run "1" ü-browser')
    argv = dispatch.with_headless_browser(CODEX, dispatch.headless_browser_overrides(
        config().headless_browser, [ORIGIN, "http://localhost:6006"], out))
    assert argv[-1] == "{prompt}" and argv[:len(CODEX) - 1] == CODEX[:-1]
    o = overrides_of(argv)
    load = lambda k: tomllib.loads("v = " + o["mcp_servers.headless_browser." + k])["v"]  # noqa: E731
    assert load("command") == "npx" and load("cwd") == out
    assert load("args") == ["--offline", "-y", "@playwright/mcp@0.0.83", "--headless", "--isolated",
                            "--block-service-workers", "--browser", "chrome",
                            "--allowed-origins", ORIGIN + ";http://localhost:6006", "--output-dir", out]
    assert load("env_vars") == [] and "mcp_servers.headless_browser.env" not in o   # see codex_config_conflict
    assert load("enabled_tools") == list(dispatch.HEADLESS_BROWSER_TOOLS)
    approved = {k.split(".")[3] for k, v in o.items()
                if k.startswith("mcp_servers.headless_browser.tools.") and v == '"approve"'}
    assert approved == set(dispatch.HEADLESS_BROWSER_TOOLS)
    for unsafe in dispatch.HEADLESS_BROWSER_DENIED:
        assert unsafe not in approved and unsafe not in load("enabled_tools")
        assert not any(unsafe in x for x in argv)
    assert "browser_evaluate" not in approved   # page JS could open a WebSocket the origin routing does not cover
    assert dispatch.risky_flags(argv) == [] and dispatch.codex_unapproved_tools(argv) == []
    with pytest.raises(ValueError, match="bound origin"):
        dispatch.headless_browser_overrides(config().headless_browser, [], out)


def test_every_playwright_tool_is_classified():
    """A hand-pinned copy of what @playwright/mcp 0.0.83 listed when run on 2026-10-08 (this test does not start the
    server, so it cannot notice a newer version's additions). It only checks that our two lists cover that
    inventory without overlap. The real control is Codex's `enabled_tools`, which exposes only
    HEADLESS_BROWSER_TOOLS whatever the server offers."""
    inventory = {"browser_close", "browser_resize", "browser_console_messages", "browser_handle_dialog",
                 "browser_emulate_media", "browser_evaluate", "browser_file_upload", "browser_drop", "browser_find",
                 "browser_fill_form", "browser_press_key", "browser_type", "browser_navigate",
                 "browser_navigate_back", "browser_network_requests", "browser_network_request",
                 "browser_run_code_unsafe", "browser_take_screenshot", "browser_snapshot", "browser_click",
                 "browser_drag", "browser_hover", "browser_select_option", "browser_tabs", "browser_wait_for"}
    approved, denied = set(dispatch.HEADLESS_BROWSER_TOOLS), set(dispatch.HEADLESS_BROWSER_DENIED)
    assert len(approved) == len(dispatch.HEADLESS_BROWSER_TOOLS) and not approved & denied
    assert approved | denied == inventory


def test_prompt_sentence_is_fixed():
    plain = build_prompt(12, 4, "Audit the About dialog", [7], "s9-codex")
    with_browser = build_prompt(12, 4, "Audit the About dialog", [7], "s9-codex", headless_browser=True)
    assert with_browser == plain + dispatch.HEADLESS_PROMPT
    assert "headless_browser tools" in with_browser and 'context kind "headless"' in with_browser
    assert "board_browser_begin_probe" in with_browser and "http" not in dispatch.HEADLESS_PROMPT


# ---------------------------------------------------------------- launching browser-bound work


def test_without_headless_browser_a_bound_request_still_cannot_launch(denv):  # noqa: F811
    denv.config.runners = {"codex": CODEX}
    allow(denv, agents=["codex"], max_launches=3)
    post = bound_request(denv)
    denv.d.tick()
    assert not denv.spawner.calls
    assert runs(denv)[0]["status"] == "preflight_failed" and "generic CLI" in runs(denv)[0]["error"]
    assert denv.board.get_post(denv.p["human"], post["id"])["requests"][0]["state"] == "blocked"


def test_bound_request_launches_codex_with_a_scoped_headless_browser(benv):
    allow(benv, agents=["codex"])
    post = bound_request(benv)
    benv.d.tick()
    assert len(benv.spawner.calls) == 1
    argv = benv.spawner.calls[0]["argv"]
    args = browser_args(argv)
    assert args[args.index("--allowed-origins") + 1] == ORIGIN
    assert "--headless" in args and "--isolated" in args and args[args.index("--browser") + 1] == "chrome"
    out = Path(args[args.index("--output-dir") + 1])
    assert out.is_dir() and stat.S_IMODE(out.stat().st_mode) == 0o700 and not any(out.iterdir())
    assert out.parent == benv.log_dir
    prompt = argv[-1]
    assert prompt.endswith(dispatch.HEADLESS_PROMPT) and "Register your session with dispatch_run_id=" in prompt
    assert URL not in prompt and "5185" not in prompt and "Browser audit" not in prompt   # no board text
    record = runs(benv)[0]
    assert record["status"] == "running" and record["request_ids"] == [post["id"]]
    assert record["headless_browser"] == {"origins": [ORIGIN], "output_dir": str(out)}


def test_each_run_gets_a_fresh_output_directory(benv):
    allow(benv, agents=["codex"])
    bound_request(benv)
    bound_request(benv)
    benv.d.tick()
    benv.spawner.children[0].code = 0
    benv.d.tick()
    benv.clock.advance(180)
    benv.d.tick()
    dirs = [browser_args(c["argv"])[browser_args(c["argv"]).index("--output-dir") + 1] for c in benv.spawner.calls]
    assert len(dirs) == 2 and dirs[0] != dirs[1]


# ---------------------------------------------------------------- origins: plain only, at bind time and at launch


@pytest.mark.parametrize("url", ["https://*:443/x", "https://*.example.com/", "https://{a,b}.example/",
                                 "http://a,b.example/", "http://a;b.example/", "http://a\x7fb.example/",
                                 "http://a\x01b.example/", "http://a[b]/", "http://-a.example/"])
def test_bind_refuses_hosts_that_are_not_plain(benv, url):
    post = human_post(benv, ["codex"])
    with pytest.raises(Invalid):
        br.bind_request(benv.board, benv.p["human"], benv.sid["human"], post["id"], "codex", url)
    assert br.requirement(benv.board, post["id"], "codex") is None


def test_plain_origin_filter():
    good = ["http://127.0.0.1:5185", "https://xn--bcher-kva.de:443", "http://[::1]:80", "http://localhost",
            "https://a_b.example:443"]
    bad = ["https://*:443", "https://{a,b}:443", "http://a,b:80", "http://a;b:80", "http://a\x7f:80", "http://a b:80",
           "ftp://x:21", "http://x:99999", "http://1.2.3:80", "http://[::FFFF:127.0.0.1]:80", "http://x/", "", None]
    assert dispatch.plain_origins(good + bad) == sorted(good)
    with pytest.raises(ValueError, match="bound origin"):
        dispatch.headless_browser_overrides(config().headless_browser, bad[:-1], "/tmp/x")


def _raw_bind(env, post_id, recipient, origin):
    """A requirement written before bind-time validation (or by anything else): bypasses target()."""
    from agent_comms import db
    with db.write_tx(env.board.conn) as c:
        c.execute("INSERT INTO browser_requirements VALUES (?,?,?,?)", (post_id, recipient, origin + "/", origin))


@pytest.mark.parametrize("origin", ["https://*:443", "http://a\x7fb:80", "https://{a,b}:443"])
def test_launch_drops_a_stored_wildcard_or_control_origin(benv, origin):
    """A malformed stored origin never reaches the argv (no glob, no invalid TOML for Codex to die on), and is
    refused at preflight, before a launch is reserved, rather than as a spawn failure."""
    allow(benv, agents=["codex"], max_launches=3)
    post = human_post(benv, ["codex"])
    _raw_bind(benv, post["id"], "codex", origin)
    benv.d.tick()
    assert not benv.spawner.calls
    assert runs(benv)[0]["status"] == "preflight_failed" and "plain http(s) origin" in runs(benv)[0]["error"]
    assert benv.board.list_dispatch_rules(benv.p["human"])[0]["launches_left"] == 3
    assert benv.d._headless_origins("codex", benv.tid, [post["id"]]) == []


def test_thread_fallback_drops_malformed_origins_and_keeps_plain_ones(benv):
    early = human_post(benv, ["codex"])                   # before the approval: never triggers by itself
    _raw_bind(benv, early["id"], "codex", "https://*:443")
    plain = bound_request(benv)
    benv.clock.advance(1)
    allow(benv, agents=["codex"])
    follow_up = human_post(benv, ["codex"], body="Unstick")
    benv.d.tick()
    assert runs(benv)[0]["request_ids"] == [follow_up["id"]]
    argv = benv.spawner.calls[0]["argv"]
    args = browser_args(argv)
    assert args[args.index("--allowed-origins") + 1] == ORIGIN and "*" not in " ".join(argv[:-1])
    assert plain["id"] != early["id"]


@pytest.mark.parametrize("denied_first", [True, False])
def test_policy_denied_gate_still_blocks_headless_launch(benv, denied_first):
    """The sticky gate is checked before the headless path, whichever came first; it is never routed around."""
    allow(benv, agents=["codex"], max_launches=3)
    deny = lambda: br.report_failure(benv.board, benv.p["claude"], benv.sid["claude"], URL, DESKTOP,  # noqa: E731
                                     "policy_denied", "User declined the browser action for this site")
    if denied_first:
        deny()
    post = bound_request(benv)
    if not denied_first:
        deny()   # blocks the bound request at once
    benv.d.tick()
    assert not benv.spawner.calls
    if denied_first:   # the request was still queued: the dispatcher's own gate check refused it
        assert runs(benv)[0]["status"] == "preflight_failed" and "policy denied" in runs(benv)[0]["error"]
    request = benv.board.get_post(benv.p["human"], post["id"])["requests"][0]
    assert request["state"] == "blocked" and "denied" in request["reason"]
    assert benv.board.list_dispatch_rules(benv.p["human"])[0]["launches_left"] == 3


def test_missing_browser_command_blocks_instead_of_launching_blind(benv, monkeypatch):
    monkeypatch.setattr(dispatch.shutil, "which", lambda exe, **kw: None if exe == "npx" else "/fake/" + exe)
    allow(benv, agents=["codex"])
    bound_request(benv)
    benv.d.tick()
    assert not benv.spawner.calls
    assert runs(benv)[0]["error"] == "required headless browser command is unavailable"


def test_unbound_thread_gets_no_browser_server(benv):
    allow(benv, agents=["codex"])
    human_post(benv, ["codex"], body="Plain code task")
    benv.d.tick()
    argv = benv.spawner.calls[0]["argv"]
    assert not any("headless_browser" in x for x in argv[:-1])
    assert dispatch.HEADLESS_PROMPT not in argv[-1] and "headless_browser" not in runs(benv)[0]


def test_unbound_follow_up_in_a_browser_bound_thread_gets_this_agents_thread_origins(benv):
    """An Unstick or recovery run is triggered by a post that binds nothing; it gets the origins bound for this
    agent's unfinished requests in the thread: never another agent's, a finished one's or a denied one's."""
    # Written before the approval, so none of these triggers a launch by itself.
    mine = bound_request(benv)                                                 # codex, queued: included
    bound_request(benv, url="http://localhost:6007/other", to="claude")        # another agent's: excluded
    finished = bound_request(benv, url="http://localhost:7007/done")
    requests.progress(benv.board, benv.p["human"], benv.sid["human"], finished["id"], "codex", "finished", "Done")
    denied = bound_request(benv, url=OTHER)
    br.report_failure(benv.board, benv.p["claude"], benv.sid["claude"], OTHER, DESKTOP, "policy_denied",
                      "User declined the browser action")
    assert benv.board.get_post(benv.p["human"], denied["id"])["requests"][0]["state"] == "blocked"
    benv.clock.advance(1)
    allow(benv, agents=["codex"])
    follow_up = human_post(benv, ["codex"], body="Unstick: resume the browser audit")
    benv.d.tick()
    assert len(benv.spawner.calls) == 1
    args = browser_args(benv.spawner.calls[0]["argv"])
    assert args[args.index("--allowed-origins") + 1] == ORIGIN
    record = runs(benv)[0]
    assert record["request_ids"] == [follow_up["id"]] and record["headless_browser"]["origins"] == [ORIGIN]
    assert mine["id"] not in record["request_ids"]


def test_other_agents_bound_requests_give_no_browser(benv):
    bound_request(benv, to="claude")
    benv.clock.advance(1)
    allow(benv, agents=["codex"])
    human_post(benv, ["codex"], body="Unrelated code task")
    benv.d.tick()
    assert not any("headless_browser" in x for x in benv.spawner.calls[0]["argv"][:-1])


def test_missing_browser_command_also_blocks_a_thread_fallback_launch(benv, monkeypatch):
    bound_request(benv)                                   # before the approval
    benv.clock.advance(1)
    monkeypatch.setattr(dispatch.shutil, "which", lambda exe, **kw: None if exe == "npx" else "/fake/" + exe)
    allow(benv, agents=["codex"], max_launches=2)
    human_post(benv, ["codex"], body="Unstick")
    benv.d.tick()
    assert not benv.spawner.calls
    assert runs(benv)[0]["error"] == "required headless browser command is unavailable"
    assert benv.board.list_dispatch_rules(benv.p["human"])[0]["launches_left"] == 2


@pytest.mark.parametrize("text", [
    '[mcp_servers.headless_browser.env]\nPLAYWRIGHT_MCP_CDP_ENDPOINT = "http://127.0.0.1:9222"\n',
    '[mcp_servers.headless_browser.tools.browser_run_code_unsafe]\napproval_mode = "approve"\n',
    '[profiles.work.mcp_servers.headless_browser]\ncommand = "node"\n',
    'not = valid = toml\n',
])
def test_codex_config_defining_the_browser_server_blocks_the_launch(benv, text):
    (benv.home / ".codex").mkdir()
    (benv.home / ".codex" / "config.toml").write_text(text)
    allow(benv, agents=["codex"], max_launches=2)
    bound_request(benv)
    benv.d.tick()
    assert not benv.spawner.calls
    assert "mcp_servers.headless_browser" in runs(benv)[0]["error"]
    assert benv.board.list_dispatch_rules(benv.p["human"])[0]["launches_left"] == 2


def test_codex_config_check_follows_codex_home_and_ignores_other_servers(benv, tmp_path):
    (benv.home / ".codex").mkdir()
    (benv.home / ".codex" / "config.toml").write_text('[mcp_servers.agent-comms]\ncommand = "bash"\n')
    other = tmp_path / "codex-work"
    other.mkdir()
    (other / "config.toml").write_text('[mcp_servers.headless_browser]\ncommand = "node"\n')
    assert dispatch.codex_config_conflict({"HOME": str(benv.home)}) is None
    assert "headless_browser" in dispatch.codex_config_conflict({"HOME": str(benv.home), "CODEX_HOME": str(other)})
    assert dispatch.codex_config_conflict({"HOME": str(tmp_path / "nobody")}) is None


# ---------------------------------------------------------------- browser output is removed when the run ends


def test_browser_output_is_removed_when_the_run_ends(benv):
    allow(benv, agents=["codex"])
    bound_request(benv)
    benv.d.tick()
    out = Path(runs(benv)[0]["headless_browser"]["output_dir"])
    (out / "page.yml").write_text("snapshot")
    (out / "sub").mkdir()
    benv.spawner.children[0].code = 0
    benv.d.tick()
    assert runs(benv)[0]["status"] == "exited" and not out.exists()
    assert benv.log_dir.is_dir()                          # only the run's own folder goes


def test_browser_output_is_removed_after_a_spawn_failure(benv):
    benv.spawner.fail_for = {"codex"}
    allow(benv, agents=["codex"])
    bound_request(benv)
    benv.d.tick()
    record = runs(benv)[0]
    assert record["status"] == "spawn_failed"
    assert not Path(record["headless_browser"]["output_dir"]).exists()


def test_browser_output_of_an_orphan_is_removed_once_it_is_gone(benv):
    allow(benv, agents=["codex"])
    bound_request(benv)
    benv.d.tick()
    out = Path(runs(benv)[0]["headless_browser"]["output_dir"])
    benv.d.release_loop()                                 # the dispatcher dies; its child is now an orphan
    benv.spawner.children[0].code = 0
    dispatch.check_orphans(benv.board, benv.procs.probe)
    assert runs(benv)[0]["status"] == "gone" and not out.exists()


@pytest.mark.parametrize("record", [
    {"run_id": "r1", "log": "/tmp/x/r1.log", "headless_browser": {"output_dir": "/tmp/x/other-browser"}},
    {"run_id": "r1", "log": "/tmp/x/r1.log", "headless_browser": {"output_dir": "/tmp/y/r1-browser"}},
    {"run_id": "r1", "log": "/tmp/x/r1.log", "headless_browser": {"output_dir": "/tmp/x/../r1-browser"}},
    {"run_id": "r1", "log": "/tmp/x/r1.log"},
])
def test_remove_browser_output_only_touches_the_runs_own_folder(record, monkeypatch):
    removed = []
    monkeypatch.setattr(dispatch.shutil, "rmtree", lambda p, **kw: removed.append(p))
    dispatch.remove_browser_output(record)
    assert removed == []


def test_dispatched_headless_probe_reaches_ready_and_starts_the_bound_request(benv):
    """End to end: the relaunched run registers with its dispatch_run_id, cannot attest a desktop context, makes a
    fresh headless probe of the bound target, and may then acknowledge the request as started."""
    allow(benv, agents=["codex"])
    post = bound_request(benv)
    # An earlier desktop probe by another session is never inherited by the dispatched run.
    attempt = br.begin_probe(benv.board, benv.p["codex"], benv.sid["codex"], URL, DESKTOP)
    br.report_probe(benv.board, benv.p["codex"], benv.sid["codex"], URL, DESKTOP, EVIDENCE, attempt["attempt_id"])
    benv.clock.advance(3 * 60)   # that session is no longer live, so the dispatcher may launch
    benv.d.tick()
    record = runs(benv)[0]
    assert record["status"] == "running"
    sid = benv.board.register_session(benv.p["codex"], PROJECT, dispatch_run_id=record["run_id"])["session_id"]
    assert br.readiness(benv.board, sid, URL) == "missing_probe"
    with pytest.raises(Exception, match="browser preflight blocked"):
        requests.progress(benv.board, benv.p["codex"], sid, post["id"], "codex", "started")
    with pytest.raises(Invalid, match="desktop"):
        br.begin_probe(benv.board, benv.p["codex"], sid, URL, DESKTOP)
    attempt = br.begin_probe(benv.board, benv.p["codex"], sid, URL, HEADLESS)
    br.report_probe(benv.board, benv.p["codex"], sid, URL, HEADLESS, EVIDENCE, attempt["attempt_id"])
    assert br.readiness(benv.board, sid, URL) == "ready"
    assert br.status(benv.board, benv.p["codex"], sid, URL)["execution_key"] == record["run_id"]
    row = requests.progress(benv.board, benv.p["codex"], sid, post["id"], "codex", "started", "Probe passed")
    assert row["state"] == "started" and row["assigned_session"] == sid
