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
def benv(denv):  # noqa: F811
    """denv with a real-shaped Codex runner (all approvals) given the headless browser."""
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
    ({"args": ["--extension"]}, "widen"),
    ({"args": ["--cdp-endpoint=http://127.0.0.1:9222"]}, "widen"),
    ({"args": ["--user-data-dir", "/x"]}, "widen"),
    ({"args": ["--config", "/x.json"]}, "widen"),
    ({"args": ["--allow-unrestricted-file-access"]}, "widen"),
    ({"args": ["--init-script", "/x.js"]}, "widen"),
    ({"args": ["--no-sandbox"]}, "widen"),
    ({"args": ["--caps=devtools"]}, "widen"),
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


def test_runner_that_configures_the_browser_server_itself_is_refused():
    own = CODEX[:-1] + ["-c", 'mcp_servers.headless_browser.tools.browser_run_code_unsafe.approval_mode="approve"',
                        "{prompt}"]
    with pytest.raises(ValueError, match="owns that server"):
        DispatchConfig.from_dict({"runners": {"codex": own}, "headless_browser": BROWSER})


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
    assert load("args") == ["--offline", "-y", "@playwright/mcp@0.0.83", "--headless", "--isolated", "--browser",
                            "chrome", "--allowed-origins", ORIGIN + ";http://localhost:6006", "--output-dir", out]
    assert load("enabled_tools") == list(dispatch.HEADLESS_BROWSER_TOOLS)
    approved = {k.split(".")[3] for k, v in o.items()
                if k.startswith("mcp_servers.headless_browser.tools.") and v == '"approve"'}
    assert approved == set(dispatch.HEADLESS_BROWSER_TOOLS)
    for unsafe in dispatch.HEADLESS_BROWSER_DENIED:
        assert unsafe not in approved and unsafe not in load("enabled_tools")
        assert not any(unsafe in x for x in argv)
    assert "browser_evaluate" in approved   # page-context JS only; see dispatch.HEADLESS_BROWSER_TOOLS
    assert dispatch.risky_flags(argv) == [] and dispatch.codex_unapproved_tools(argv) == []
    with pytest.raises(ValueError, match="bound origin"):
        dispatch.headless_browser_overrides(config().headless_browser, [], out)


def test_every_playwright_tool_is_classified():
    """@playwright/mcp 0.0.83 serves exactly these (listed from the real server). Each is approved or never
    enabled; a tool a newer version adds is neither, so `enabled_tools` keeps it hidden until someone decides."""
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


def test_unbound_follow_up_in_a_browser_bound_thread_gets_the_thread_origins(benv):
    """An Unstick or recovery run is triggered by a post that binds nothing; it still gets the thread's bound
    origins of unfinished requests, never a denied one or a finished one."""
    allow(benv, agents=["claude"])                       # the bound request waits for another agent
    bound_request(benv, to="claude")
    finished = bound_request(benv, url="http://localhost:7007/done", to="claude")
    requests.progress(benv.board, benv.p["human"], benv.sid["human"], finished["id"], "claude", "finished", "Done")
    denied = bound_request(benv, url=OTHER, to="claude")
    br.report_failure(benv.board, benv.p["claude"], benv.sid["claude"], OTHER, DESKTOP, "policy_denied",
                      "User declined the browser action")
    assert benv.board.get_post(benv.p["human"], denied["id"])["requests"][0]["state"] == "blocked"
    benv.config.runners["claude"] = ["claude", "-p", "{prompt}"]
    allow(benv, agents=["codex"])
    follow_up = human_post(benv, ["codex"], body="Unstick: resume the browser audit")
    benv.d.tick()
    call = next(c for c in benv.spawner.calls if c["argv"][0] == "codex")
    args = browser_args(call["argv"])
    assert args[args.index("--allowed-origins") + 1] == ORIGIN
    record = next(r for r in runs(benv) if r["agent"] == "codex")
    assert record["request_ids"] == [follow_up["id"]] and record["headless_browser"]["origins"] == [ORIGIN]


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
