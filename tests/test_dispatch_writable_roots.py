"""Dispatcher: per-project extra writable roots ([dispatch.writable_roots]) and the declared-output preflight.
Never spawns a real agent CLI."""

import os
import tomllib
from pathlib import Path

import pytest

from agent_comms import board_settings, dispatch
from agent_comms.config import Settings
from agent_comms.core import Invalid
from agent_comms.dispatch import DispatchConfig, validate_runner, validate_writable_roots

from conftest import PROJECT
from test_dispatch import ROOT, allow, denv, human_post, runs  # noqa: F401  (denv is a fixture)
from test_dispatch_headless_browser import CODEX

OTHER = "/work/other"


def real(p) -> Path:
    return Path(os.path.realpath(p))


@pytest.fixture
def homes(tmp_path, monkeypatch):
    """A fake user home, token directory and board home, all under one real temporary directory."""
    base = real(tmp_path) / "machine"
    home = base / "home"
    for d in (home / ".ssh", home / ".config" / "agent-comms", home / ".codex", home / "nexus-work",
              base / "board" / "data", base / "elsewhere"):
        d.mkdir(parents=True)
    (home / ".config" / "agent-comms" / "codex.token").write_text("x")
    (base / "board" / "agents.toml").write_text("")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("AGENT_COMMS_HOME", str(base / "board"))
    monkeypatch.delenv("AGENT_COMMS_TOKEN_DIR", raising=False)
    monkeypatch.delenv("AGENT_COMMS_TOKEN_FILE", raising=False)
    monkeypatch.setenv("CODEX_HOME", str(home / ".codex"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home / ".claude"))
    return base


# ---------------------------------------------------------------- configuration


def test_shipped_config_has_no_writable_roots():
    shipped = DispatchConfig.load(ROOT / "board.toml", local=False)
    assert shipped.writable_roots == {}
    assert "[dispatch.writable_roots]" in (ROOT / "board.toml").read_text()


def test_valid_roots_are_kept_per_project(homes):
    nexus = str(homes / "home" / "nexus-work")
    c = DispatchConfig.from_dict({"writable_roots": {PROJECT + "/": [nexus + "/", str(homes / "elsewhere")]}})
    assert c.writable_roots == {PROJECT: [nexus, str(homes / "elsewhere")]}
    assert c.extra_roots(CODEX, PROJECT) == [nexus, str(homes / "elsewhere")]
    assert c.extra_roots(CODEX, OTHER) == []                     # other projects get nothing


def bad_roots(homes, tmp_path):
    home = homes / "home"
    (homes / "afile").write_text("")
    os.symlink(home / "nexus-work", homes / "link")
    many = []
    for i in range(11):
        (homes / f"m{i}").mkdir()
        many.append(str(homes / f"m{i}"))
    return [
        ({PROJECT: ["relative/dir"]}, "absolute, normalized"),
        ({PROJECT: [str(home / "nexus-work" / ".." / "nexus-work")]}, "absolute, normalized"),
        ({PROJECT: [str(home) + "//nexus-work"]}, "absolute, normalized"),
        ({PROJECT: [str(homes / "missing")]}, "not an existing directory"),
        ({PROJECT: [str(homes / "afile")]}, "not an existing directory"),
        ({PROJECT: [str(homes / "link")]}, "symlink"),
        ({PROJECT: ["/"]}, "home directory"),
        ({PROJECT: [str(home)]}, "home directory"),
        ({PROJECT: [str(homes)]}, "home directory"),                       # contains the home directory
        ({PROJECT: [str(home / ".ssh")]}, "protected location"),
        ({PROJECT: [str(home / ".config")]}, "protected location"),         # contains ~/.config/agent-comms
        ({PROJECT: [str(home / ".config" / "agent-comms")]}, "protected location"),
        ({PROJECT: [str(home / ".codex")]}, "protected location"),
        ({PROJECT: [str(homes / "board")]}, "protected location"),         # the board's home
        ({PROJECT: [str(homes / "board" / "data")]}, "protected location"),
        ({PROJECT: many}, "more than 10"),
        ({PROJECT: [str(home / "nexus-work"), str(home / "nexus-work") + "/"]}, "repeats"),
        ({PROJECT: str(home / "nexus-work")}, "list of absolute"),
        ({"relative": [str(home / "nexus-work")]}, "absolute project paths"),
        ("nope", "must be a table"),
    ]


def test_every_refusal(homes, tmp_path):
    for table, match in bad_roots(homes, tmp_path):
        with pytest.raises(ValueError, match=match):
            DispatchConfig.from_dict({"writable_roots": table})


def test_inside_ssh_and_case_variants_are_refused(homes):
    inner = homes / "home" / ".ssh" / "keys"
    inner.mkdir()
    with pytest.raises(ValueError, match="protected location"):
        validate_writable_roots({PROJECT: [str(inner)]})
    upper = str(homes / "home" / ".SSH")
    if os.path.isdir(upper):   # case-insensitive filesystem (macOS default): same directory, other spelling
        with pytest.raises(ValueError, match="protected location"):
            validate_writable_roots({PROJECT: [upper]})


def test_configured_data_dir_and_token_files_are_protected(homes, tmp_path):
    data = homes / "elsewhere" / "data"
    data.mkdir()
    settings = Settings(db_path=data / "board.db", agents_path=homes / "elsewhere" / "agents.toml")
    with pytest.raises(ValueError, match="protected location"):
        validate_writable_roots({PROJECT: [str(homes / "elsewhere")]}, settings)
    assert validate_writable_roots({PROJECT: [str(homes / "elsewhere")]}) == {PROJECT: [str(homes / "elsewhere")]}
    # a token file kept outside the protected directories
    tokens = homes / "tokens"
    tokens.mkdir()
    (tokens / "human.token").write_text("x")
    os.environ["AGENT_COMMS_TOKEN_FILE"] = str(tokens / "human.token")
    try:
        with pytest.raises(ValueError, match="protected file"):
            validate_writable_roots({PROJECT: [str(tokens)]})
    finally:
        del os.environ["AGENT_COMMS_TOKEN_FILE"]


@pytest.mark.parametrize("template", [
    ["codex", "exec", "--add-dir", "/x", "{prompt}"],
    ["codex", "exec", "--add-dir=/x", "{prompt}"],
    ["codex", "exec", "-c", 'sandbox_workspace_write.writable_roots=["/x"]', "{prompt}"],
    ["codex", "exec", "--config", "sandbox_workspace_write.writable_roots=[]", "{prompt}"],
    ["codex", "exec", "--config=sandbox_workspace_write.writable_roots=[]", "{prompt}"],
    ["codex", "exec", "-c", 'sandbox_workspace_write."writable_roots"=[]', "{prompt}"],
    ["codex", "exec", "-c", 'profiles.p.sandbox_workspace_write.writable_roots=[]', "{prompt}"],
    ["claude", "-p", "{prompt}", "--add-dir", "/x"],
    ["claude", "-p", "{prompt}", "--add-dir=/x"],
])
def test_runner_setting_writable_roots_itself_is_refused(template):
    with pytest.raises(ValueError, match=r"\[dispatch.writable_roots\]"):
        validate_runner("codex", template)


def test_other_sandbox_overrides_are_still_allowed():
    validate_runner("codex", ["codex", "exec", "-c", "sandbox_workspace_write.network_access=true", "{prompt}"])
    validate_runner("claude", ["claude", "-c", "-p", "{prompt}"])   # claude's -c is --continue, not a config


def test_settings_page_cannot_edit_writable_roots():
    for key in ("dispatch.writable_roots", "dispatch.writable_roots./work/repo"):
        with pytest.raises(Invalid, match="not editable"):
            board_settings.validate_changes({key: ["/tmp"]})


def test_config_reload_checks_the_configured_data_dir(homes, tmp_path):
    from agent_comms.core import check_reloadable
    data = homes / "elsewhere" / "data"
    data.mkdir()
    s = Settings(db_path=data / "board.db", agents_path=homes / "board" / "agents.toml",
                 dispatch={"writable_roots": {PROJECT: [str(homes / "elsewhere")]}})
    with pytest.raises(ValueError, match="protected location"):
        check_reloadable(s)


# ---------------------------------------------------------------- argv injection


@pytest.fixture
def wenv(denv, tmp_path, monkeypatch):  # noqa: F811
    """denv with a real-shaped Codex runner, a plain Claude runner and one extra root for PROJECT."""
    monkeypatch.setenv("HOME", str(real(tmp_path) / "fakehome"))
    extra = real(tmp_path) / "nexus-work"
    extra.mkdir()
    denv.extra = str(extra)
    denv.outside = real(tmp_path) / "outside"
    denv.outside.mkdir()
    denv.config.runners["codex"] = CODEX
    denv.config.runners["claude"] = ["claude", "-p", "{prompt}", "--permission-mode", "dontAsk"]
    denv.config.writable_roots = validate_writable_roots({PROJECT: [denv.extra]})
    return denv


def overrides(argv):
    return {argv[i + 1].partition("=")[0]: argv[i + 1].partition("=")[2] for i, x in enumerate(argv) if x == "-c"}


def test_codex_run_gets_the_roots_as_one_override_before_the_prompt(wenv):
    allow(wenv, agents=["codex"])
    human_post(wenv, ["codex"])
    wenv.d.tick()
    (call,) = wenv.spawner.calls
    argv = call["argv"]
    value = overrides(argv)["sandbox_workspace_write.writable_roots"]
    assert tomllib.loads("v = " + value)["v"] == [wenv.extra]
    i = argv.index("sandbox_workspace_write.writable_roots=" + value)
    assert argv[i - 1] == "-c" and i < len(argv) - 1          # an option, before the prompt (the last element)
    assert argv[argv.index("--sandbox") + 1] == "workspace-write"
    assert call["cwd"] == wenv.workdir


def test_claude_run_gets_add_dir(wenv):
    allow(wenv, agents=["claude"])
    human_post(wenv, ["claude"])
    wenv.d.tick()
    (call,) = wenv.spawner.calls
    assert call["argv"][-1] == "--add-dir=" + wenv.extra
    assert call["argv"].count("--add-dir=" + wenv.extra) == 1


def test_scoped_claude_run_gets_no_roots(wenv):
    wenv.config.claude_tool_projects = [PROJECT]
    allow(wenv, agents=["claude"])
    human_post(wenv, ["claude"])
    wenv.d.tick()
    (call,) = wenv.spawner.calls
    assert not any(a.startswith("--add-dir") for a in call["argv"])
    assert wenv.config.extra_roots(wenv.config.runners["claude"], PROJECT) == []


def test_other_projects_get_nothing(wenv, tmp_path):
    other_dir = real(tmp_path) / "other-repo"
    other_dir.mkdir()
    wenv.config.worktrees[OTHER] = str(other_dir)
    sid = wenv.session("human", project=OTHER)
    tid = wenv.board.create_thread(wenv.p["human"], sid, "other", OTHER)["id"]
    allow(wenv, agents=["codex"], thread_id=tid)
    wenv.post("human", tid, "your turn", "request", to=["codex"], session_id=sid)
    wenv.d.tick()
    (call,) = wenv.spawner.calls
    assert call["cwd"] == str(other_dir)
    assert "sandbox_workspace_write.writable_roots" not in overrides(call["argv"])


def test_no_roots_configured_leaves_argv_unchanged(wenv):
    wenv.config.writable_roots = {}
    allow(wenv, agents=["codex"])
    human_post(wenv, ["codex"])
    wenv.d.tick()
    (call,) = wenv.spawner.calls
    assert "sandbox_workspace_write.writable_roots" not in overrides(call["argv"])
    assert call["argv"][:len(CODEX) - 1] == [wenv.workdir if a == "{project}" else a for a in CODEX[:-1]]


# ---------------------------------------------------------------- declared outputs


def output(path):
    return [{"kind": "output", "path": str(path)}]


@pytest.mark.parametrize("path", ["relative/out", "/a/../b", "/a/./b", "/a/\x01b", "~/nexus-work"])
def test_output_refs_must_be_plain_absolute_paths(wenv, path):
    with pytest.raises(Invalid, match="output refs"):
        human_post(wenv, ["codex"], refs=output(path))


def test_output_ref_with_rev_is_refused(wenv):
    with pytest.raises(Invalid, match="output refs"):
        human_post(wenv, ["codex"], refs=[{"kind": "output", "path": "/x", "rev": "abc"}])


@pytest.mark.parametrize("where", ["run_dir", "extra_root", "extra_root_new_subdir"])
def test_output_inside_writable_roots_launches(wenv, where):
    target = {"run_dir": Path(wenv.workdir) / "evidence",
              "extra_root": Path(wenv.extra),
              "extra_root_new_subdir": Path(wenv.extra) / "fix" / "after" / "screens"}[where]
    allow(wenv, agents=["codex"])
    human_post(wenv, ["codex"], refs=output(target))
    wenv.d.tick()
    assert len(wenv.spawner.calls) == 1
    assert runs(wenv)[0]["status"] == "running"


def blocked(env, post, n=10):
    assert not env.spawner.calls
    (run,) = runs(env)
    assert run["status"] == "preflight_failed"
    assert env.board.list_dispatch_rules(env.p["human"])[0]["launches_left"] == n   # no budget spent
    request = env.board.get_post(env.p["human"], post["id"])["requests"][0]
    assert request["state"] == "blocked"
    assert request["reason"].startswith("Preflight failed: ")
    return run["error"]


def test_output_outside_fails_preflight_without_spending_budget(wenv):
    allow(wenv, agents=["codex"])
    post = human_post(wenv, ["codex"], refs=output(wenv.outside / "screens"))
    wenv.d.tick()
    wenv.d.tick()
    error = blocked(wenv, post)
    assert f"output {wenv.outside / 'screens'} is outside this run's writable roots" in error
    assert f"run dir {wenv.workdir}" in error and f"extra roots {wenv.extra}" in error
    assert "[dispatch.writable_roots]" in error and "retarget the output" in error


def test_other_project_cannot_use_this_projects_root(wenv, tmp_path):
    other_dir = real(tmp_path) / "other-repo"
    other_dir.mkdir()
    wenv.config.worktrees[OTHER] = str(other_dir)
    sid = wenv.session("human", project=OTHER)
    tid = wenv.board.create_thread(wenv.p["human"], sid, "other", OTHER)["id"]
    allow(wenv, agents=["codex"], thread_id=tid)
    post = wenv.post("human", tid, "your turn", "request", to=["codex"], session_id=sid, refs=output(wenv.extra))
    wenv.d.tick()
    assert "no extra roots configured for this project" in blocked(wenv, post)


def test_scoped_claude_output_in_extra_root_fails_with_reason(wenv):
    wenv.config.claude_tool_projects = [PROJECT]
    allow(wenv, agents=["claude"])
    post = human_post(wenv, ["claude"], refs=output(Path(wenv.extra) / "x"))
    wenv.d.tick()
    assert "this runner cannot be given [dispatch.writable_roots]" in blocked(wenv, post)


def test_symlink_out_of_the_run_dir_is_caught(wenv):
    os.symlink(wenv.outside, Path(wenv.workdir) / "escape")
    allow(wenv, agents=["codex"])
    post = human_post(wenv, ["codex"], refs=output(Path(wenv.workdir) / "escape" / "screens"))
    wenv.d.tick()
    assert "outside this run's writable roots" in blocked(wenv, post)


def test_unwritable_destination_fails(wenv):
    locked = Path(wenv.extra) / "locked"
    locked.mkdir()
    locked.chmod(0o500)
    try:
        allow(wenv, agents=["codex"])
        post = human_post(wenv, ["codex"], refs=output(locked / "screens"))
        wenv.d.tick()
        if os.geteuid() != 0:
            assert f"{locked} is not a directory the dispatcher's user can write" in blocked(wenv, post)
    finally:
        locked.chmod(0o700)


def test_root_removed_after_start_fails_preflight(wenv):
    os.rmdir(wenv.extra)
    allow(wenv, agents=["codex"])
    post = human_post(wenv, ["codex"])
    wenv.d.tick()
    assert "no longer exists" in blocked(wenv, post)


def test_paths_in_post_text_are_ignored(wenv):
    allow(wenv, agents=["codex"])
    human_post(wenv, ["codex"], body=f"Write the screenshots to {wenv.outside} please")
    wenv.d.tick()
    assert len(wenv.spawner.calls) == 1
    assert str(wenv.outside) not in " ".join(wenv.spawner.calls[0]["argv"])


def test_every_declared_output_is_checked(wenv):
    allow(wenv, agents=["codex"])
    post = human_post(wenv, ["codex"], refs=output(wenv.extra) + output(wenv.outside))
    wenv.d.tick()
    assert f"output {wenv.outside} is outside" in blocked(wenv, post)
