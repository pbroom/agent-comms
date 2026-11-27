"""Agent accounts (agent_accounts.py, DESIGN_NOTES "Agent accounts"): a runner's account folder
([dispatch.runner_homes], read where it is launched) and the hub's opt-in project list per agent
([dispatch.agent_projects]). Every start path, the refusals and their reasons, back-compat byte for byte, the node's
report of the folder and the installers. Temp directories and fakes only: never a real agent CLI, never the real
~/.claude, ~/.claude-work or ~/.codex."""

import json
import os
import subprocess
from pathlib import Path

import httpx
import pytest

from agent_comms import agent_accounts, cli, dispatch, hosted, hosted_spawn as hs, hosted_wake, nodes, session_host
from agent_comms.config import create_agent
from agent_comms.core import Conflict
from agent_comms.dispatch import DispatchConfig, child_env

from conftest import PROJECT, make_env
from test_dispatch import FakeProcs, FakeSpawner, Recorder, human_post, new_dispatcher, runs
from test_hosted_sessions import P2, P3, send, session_env, show, start, tick
from test_hosted_spawn import bound, grant, interactive, kid_turns
from test_hosted_tree import CODEX_RUNNER, SHIPPED, answer, child, proc_of, root, stored, tree_config
from test_session_host import value_after

ROOT = Path(__file__).resolve().parents[1]
WORK = "claude-work"
NOT_IN = "claude-work is not opted in to "


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A private HOME with an account folder in it; the token folder and the board's home are elsewhere in tmp."""
    h = tmp_path / "home"
    (h / ".claude-work").mkdir(parents=True)
    (h / ".codex-work").mkdir()
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setenv("AGENT_COMMS_TOKEN_DIR", str(h / ".config" / "agent-comms"))
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    return h


def cfg(**d) -> DispatchConfig:
    return DispatchConfig.from_dict(d)


ENVIRON = {"HOME": "/Users/you", "PATH": "/usr/bin:/bin", "USER": "you", "CLAUDE_CONFIG_DIR": "/Users/you/.x",
           "CODEX_HOME": "/Users/you/.y", "SECRET_TOKEN": "s", "LANG": "C"}


# ------------------------------------------------------------ the runner's account folder: settings


def test_no_entry_leaves_the_environment_byte_for_byte_as_it_was(home):
    runners = {"claude-code": SHIPPED, WORK: SHIPPED, "codex-cli": CODEX_RUNNER}
    before = cfg(runners=runners)
    after = cfg(runners=runners, runner_homes={WORK: str(home / ".claude-work")})
    for agent, runtime in (("claude", "claude-code"), ("codex", "codex-cli"), ("grok", "grok")):
        a = child_env(agent, before, environ=ENVIRON, runtime=runtime)
        b = child_env(agent, after, environ=ENVIRON, runtime=runtime)
        assert json.dumps(a) == json.dumps(b), agent
        assert "CLAUDE_CONFIG_DIR" not in a and "CODEX_HOME" not in a, "the caller's own values are never passed on"
    assert child_env("claude", before, environ=ENVIRON, runtime="claude-code") == {
        "HOME": "/Users/you", "USER": "you", "PATH": "/usr/bin:/bin", "LANG": "C",
        "AGENT_COMMS_HOME": str(dispatch.home()), "AGENT_COMMS_MANAGED": "1"}
    assert (before.runner_homes, before.agent_projects) == ({}, {})
    assert before.runner_home("claude", "claude-code") is None and before.home_conflict("claude", "claude-code") is None


def test_the_cli_chooses_the_variable_claude_config_dir_or_codex_home(home):
    c = cfg(runners={WORK: SHIPPED, "codex-work": CODEX_RUNNER},
            runner_homes={WORK: str(home / ".claude-work"), "codex-work": str(home / ".codex-work")})
    env = child_env(WORK, c, environ=ENVIRON, runtime="claude-code")
    assert env["CLAUDE_CONFIG_DIR"] == str(home / ".claude-work") and "CODEX_HOME" not in env
    env = child_env("codex-work", c, environ=ENVIRON, runtime="codex-cli")
    assert env["CODEX_HOME"] == str(home / ".codex-work") and "CLAUDE_CONFIG_DIR" not in env
    assert c.runner_home(WORK, "claude-code") == ("CLAUDE_CONFIG_DIR", str(home / ".claude-work"))


def test_a_runtime_runner_with_a_folder_covers_each_agent_of_that_runtime_without_a_runner_of_its_own(home):
    c = cfg(runners={"claude-code": SHIPPED, "claude": ["claude", "-p", "{prompt}"]},
            runner_homes={"claude-code": str(home / ".claude-work")})
    assert child_env(WORK, c, runtime="claude-code")["CLAUDE_CONFIG_DIR"] == str(home / ".claude-work")
    assert "CLAUDE_CONFIG_DIR" not in child_env("claude", c, runtime="claude-code"), "its own runner has none"


def test_the_variable_is_never_named_by_config_and_only_claude_or_codex_runners_have_a_folder(home):
    w = str(home / ".claude-work")
    with pytest.raises(ValueError, match="only a claude or codex runner"):
        cfg(runners={"grok": ["grok", "{prompt}"]}, runner_homes={"grok": w})
    with pytest.raises(ValueError, match="no such runner"):
        cfg(runners={"claude-code": SHIPPED}, runner_homes={WORK: w})
    with pytest.raises(ValueError, match="must be a table"):
        cfg(runners={WORK: SHIPPED}, runner_homes=[w])


@pytest.mark.parametrize("path, why", [
    ("~/.claude-work", "literal absolute"), ("$HOME/.claude-work", "literal absolute"), (".claude-work", "literal"),
    ("{h}/x/../.claude-work", "normalized"), ("{h}/.claude-work/", "normalized"), ("{h}//.claude-work", "normalized"),
    ("{h}/missing", "not an existing folder"), ("{h}/afile", "not a folder"), ("{h}/link", "symlink"),
    ("/", "may not be / or the home folder"), ("{h}", "may not be / or the home folder"),
    ("{h}/.config/agent-comms", "token folder"), ("{h}/.config/agent-comms/inner", "token folder"),
    ("{h}/.config", "token folder"), ("{board}", "board's home"), ("{board}/data", "board's home"),
    ("{h}/.ssh", "token folder"), (7, "literal absolute"), ("{h}/.claude-work\n", "literal absolute"),
    ("{h}/tokens", "token folder or a token file"), ("{h}/tokens/inner", "token folder or a token file"),
    ("{h}/agentsdir", "token folder or a token file"),
])
def test_an_account_folder_is_a_literal_real_existing_folder_and_never_a_protected_one(home, tmp_path, monkeypatch,
                                                                                       path, why):
    board = tmp_path / "board"
    (board / "data").mkdir(parents=True)
    monkeypatch.setenv("AGENT_COMMS_HOME", str(board))
    # A relocated human token file (AGENT_COMMS_TOKEN_FILE) and its folder, and a relocated agents.toml.
    (home / "tokens" / "inner").mkdir(parents=True)
    monkeypatch.setenv("AGENT_COMMS_TOKEN_FILE", str(home / "tokens" / "human.token"))
    (home / "agentsdir").mkdir()
    monkeypatch.setattr(agent_accounts, "forbidden", _with_agents(agent_accounts.forbidden, home / "agentsdir"))
    (home / "afile").write_text("x")
    (home / "link").symlink_to(home / ".claude-work")
    for d in (home / ".config" / "agent-comms" / "inner", home / ".ssh"):
        d.mkdir(parents=True, exist_ok=True)
    if isinstance(path, str):
        path = path.format(h=home, board=board)
    with pytest.raises(ValueError, match=why) as e:
        cfg(runners={WORK: SHIPPED}, runner_homes={WORK: path})
    assert str(e.value).startswith(f"[dispatch.runner_homes] {WORK}"), "the error names the runner"


def _with_agents(real, directory):
    """`forbidden` as for settings whose agents.toml is in `directory`."""
    from types import SimpleNamespace

    return lambda settings=None: real(SimpleNamespace(db_path=None, agents_path=directory / "agents.toml"))


def test_an_account_folder_owned_by_another_user_is_refused(home, monkeypatch):
    monkeypatch.setattr(agent_accounts.os, "getuid", lambda: os.stat(home).st_uid + 1)
    with pytest.raises(ValueError, match="not owned by the user"):
        cfg(runners={WORK: SHIPPED}, runner_homes={WORK: str(home / ".claude-work")})


def test_a_folder_also_passed_through_dispatch_env_is_refused_at_load_and_at_launch(home):
    w = str(home / ".claude-work")
    with pytest.raises(ValueError, match="also passes CLAUDE_CONFIG_DIR; set the account folder one way only"):
        cfg(runners={WORK: SHIPPED}, env={WORK: ["CLAUDE_CONFIG_DIR"]}, runner_homes={WORK: w})
    # Keyed differently, so only the launch can tell: the runner by the agent's name, [dispatch.env] by its runtime.
    c = cfg(runners={WORK: SHIPPED}, env={"claude-code": ["CLAUDE_CONFIG_DIR"]}, runner_homes={WORK: w})
    why = c.home_conflict(WORK, "claude-code")
    assert why == ("[dispatch.env] claude-code passes CLAUDE_CONFIG_DIR while runner claude-work has an account "
                   "folder ([dispatch.runner_homes]): set it one way only")
    assert child_env(WORK, c, environ=ENVIRON, runtime="claude-code")["CLAUDE_CONFIG_DIR"] == w, \
        "and should anything start it anyway, the folder wins over the passed-through value"
    assert c.home_conflict("claude", "claude-code") is None, "an agent whose runner has no folder: nothing to refuse"


def test_a_writable_root_worktree_dir_or_discovered_checkout_never_overlaps_an_account_folder(home, tmp_path):
    w = home / ".claude-work"
    (w / "projects").mkdir()
    with pytest.raises(ValueError, match="overlaps an account folder"):
        cfg(runners={WORK: SHIPPED}, runner_homes={WORK: str(w)}, writable_roots={PROJECT: [str(w / "projects")]})
    with pytest.raises(ValueError, match="worktree_dir"):
        cfg(runners={WORK: SHIPPED}, runner_homes={WORK: str(w)}, parallel_worktrees=True, worktree_dir=str(w / "wt"))
    with pytest.raises(ValueError, match=r"worktree_dir \(where worktrees go\).*overlaps an account folder"):
        cfg(runners={WORK: SHIPPED}, runner_homes={WORK: str(w)}, worktree_dir=str(w / "wt")), \
            "a child session's own worktree goes there even with parallel_worktrees off"
    with pytest.raises(ValueError, match=r"\[dispatch.worktrees\] /work/repo: .* overlaps an account folder"):
        cfg(runners={WORK: SHIPPED}, runner_homes={WORK: str(w)}, worktrees={PROJECT: str(w / "projects")})
    with pytest.raises(ValueError, match="overlaps an account folder"):
        cfg(runners={WORK: SHIPPED}, runner_homes={WORK: str(w)}, worktrees={PROJECT: str(home)}), "around it too"
    (w / "repo" / ".git").mkdir(parents=True)
    c = cfg(runners={WORK: SHIPPED}, runner_homes={WORK: str(w)}, checkout_roots=[str(home)])
    assert not c.discovered_checkout(str(w / "repo")), "a checkout inside an account folder is never a run directory"
    assert cfg(checkout_roots=[str(home)]).discovered_checkout(str(w / "repo")), "(without the folder it would be)"


# ------------------------------------------------------------ the opt-in list: settings and comparison


def test_the_opt_in_table_is_parsed_strictly():
    assert cfg(agent_projects={WORK: ["/a/b/", "/a/b", "/c"]}).agent_projects == {WORK: ["/a/b", "/c"]}
    assert cfg(agent_projects={WORK: []}).agent_projects == {WORK: []}, "an empty list: nowhere"
    for bad, why in (([], "must be a table"), ({"Not An Agent": ["/a"]}, "agent names"),
                     ({WORK: "/a"}, "must be a list"), ({WORK: ["relative"]}, "absolute, normalized"),
                     ({WORK: ["/a/../b"]}, "absolute, normalized"), ({WORK: ["/a\nb"]}, "absolute, normalized")):
        with pytest.raises(ValueError, match=why):
            cfg(agent_projects=bad)


def test_projects_compare_by_the_exact_name_the_board_stores_and_nothing_on_disk(tmp_path, monkeypatch):
    table = agent_accounts.validate_projects({WORK: ["/Users/you/project-a", "/Users/you/project-b/"]})
    assert table == {WORK: ["/Users/you/project-a", "/Users/you/project-b"]}, "trailing slashes off, nothing else"
    for ok in ("/Users/you/project-a", "/Users/you/project-a/", "/Users/you/project-a//", "/Users/you/project-b"):
        assert agent_accounts.refusal(table, WORK, ok) is None, ok
    for no in ("/users/you/project-a", "/Users/you/project-a/sub", "/Users/you/./project-a", "/Users/you",
               "/private/Users/you/project-a", None, "relative", "", "/x" * 600,
               # What core._norm_path would trim but the stored project keeps (hosted._project keeps it, and the node
               # launches that very string): another checkout's name, never the listed one.
               "/Users/you/project-a ", "/Users/you/project-a\u00a0", " /Users/you/project-a", "/Users/you/project-a /"):
        assert agent_accounts.refusal(table, WORK, no) == agent_accounts.not_opted_in(WORK, no), \
            f"{no!r}: spelled otherwise is another project to the board"
    assert agent_accounts.refusal({WORK: ["/Users/you/project-a "]}, WORK, "/Users/you/project-a ") is None, \
        "an entry with a trailing space names exactly that project"
    for bad in (" /Users/you/project-a", "/a\nb"):
        with pytest.raises(ValueError, match="absolute, normalized"):
            agent_accounts.validate_projects({WORK: [bad]})
    assert agent_accounts.refusal(table, "claude", "/elsewhere") is None, "no entry: unrestricted"
    assert agent_accounts.refusal({WORK: []}, WORK, "/Users/you/project-a").startswith(NOT_IN)
    # Nothing on any disk is read to decide: not even a symlink to the opted-in project counts.
    link = tmp_path / "link"
    link.symlink_to(tmp_path)
    assert agent_accounts.refusal({WORK: [str(tmp_path)]}, WORK, str(link)) is not None
    for name in ("realpath", "isfile", "isdir", "exists", "stat", "lstat"):
        monkeypatch.setattr(agent_accounts.os.path if hasattr(agent_accounts.os.path, name) else agent_accounts.os,
                            name, lambda *a, **k: pytest.fail("the decision read the disk"))
    monkeypatch.setattr("builtins.open", lambda *a, **k: pytest.fail("the decision read a file"))
    assert agent_accounts.refusal(table, WORK, "/Users/you/project-a") is None
    assert agent_accounts.refusal(table, WORK, "/Users/you/project-c").startswith(NOT_IN)


def test_a_session_whose_project_ends_in_a_space_does_not_pass_the_entry_without_it(aenv):
    """hosted.start stores and launches the project as given (trailing slashes aside): "/work/repo " is another
    checkout's name than "/work/repo", and the list is held to the stored value."""
    r = aenv.human.post("/api/hosted-sessions", json={"project": PROJECT + " ", "message": "x", "agent": WORK})
    assert r.status_code == 409 and r.json()["message"].startswith(NOT_IN + PROJECT + " "), r.text
    assert hosted._sessions(aenv.board.conn) == []


def test_an_unreadable_table_on_the_hub_refuses_every_start(tmp_path):
    env = make_env(tmp_path, dispatch={"agent_projects": {WORK: "not a list"}})
    assert agent_accounts.hub_refusal(env.board, "claude", PROJECT) == agent_accounts.UNREADABLE
    env.board.s.dispatch = {}
    assert agent_accounts.hub_refusal(env.board, "claude", PROJECT) is None


def test_while_the_latest_settings_did_not_load_every_start_is_refused_through_the_real_reload(tmp_path):
    """Board.reload_settings keeps the last good settings when an edit does not load. Their opt-in table may be the
    one the owner is changing, so nothing is started until a valid one loads (fails closed, not open)."""
    from agent_comms.config import LOCAL_SETTINGS, Settings
    from agent_comms.core import Board

    toml = tmp_path / "board.toml"
    toml.write_text(f'[server]\ndb_path = "{tmp_path / "board.db"}"\nagents_path = "{tmp_path / "agents.toml"}"\n')
    local = toml.with_name(LOCAL_SETTINGS)
    local.write_text(f'[dispatch.agent_projects]\n"{WORK}" = ["{PROJECT}"]\n')
    board = Board(Settings.load(toml))
    assert agent_accounts.hub_refusal(board, WORK, PROJECT) is None
    assert agent_accounts.hub_refusal(board, WORK, P2).startswith(NOT_IN)
    for broken in (f'[dispatch.agent_projects]\n"{WORK}" = "{PROJECT}"\n',        # the table itself
                   f'[dispatch.agent_projects]\n"{WORK}" = [\n',                    # not TOML at all
                   '[dispatch]\nno_such_setting = 1\n'):                           # another key: the table is gone
        local.write_text(broken)
        assert board.reload_settings(force=True) is False and board.settings_error
        assert board.s.dispatch.get("agent_projects") == {WORK: [PROJECT]}, "the last good settings are kept..."
        for agent, project in ((WORK, PROJECT), (WORK, P2), ("claude", PROJECT)):
            assert agent_accounts.hub_refusal(board, agent, project) == agent_accounts.UNLOADED, (broken, agent)
        with pytest.raises(Conflict, match="settings did not load: fix board.toml/board.local.toml"):
            agent_accounts.check(board, "claude", PROJECT)
    local.write_text(f'[dispatch.agent_projects]\n"{WORK}" = ["{P2}"]\n')
    assert board.reload_settings(force=True) is True and board.settings_error is None
    assert agent_accounts.hub_refusal(board, WORK, P2) is None and agent_accounts.hub_refusal(board, "claude", P3) is None
    assert agent_accounts.hub_refusal(board, WORK, PROJECT).startswith(NOT_IN), "the new table, once it loads"


def _disk_board(tmp_path, local_text: str):
    """A Board whose settings come from a board.toml and board.local.toml on disk (so it reloads them), with a
    human, the claude agent and a node `laptop` that hosts sessions for PROJECT."""
    from agent_comms.config import LOCAL_SETTINGS, Settings
    from agent_comms.core import Board

    toml = tmp_path / "board.toml"
    toml.write_text(f'[server]\ndb_path = "{tmp_path / "board.db"}"\nagents_path = "{tmp_path / "agents.toml"}"\n')
    local = toml.with_name(LOCAL_SETTINGS)
    local.write_text(local_text)
    tokens = {n: create_agent(tmp_path / "agents.toml", n, rt, is_human=n == "human")
              for n, rt in (("human", "human"), ("claude", "claude-code"), ("laptop", "node"))}
    board = Board(Settings.load(toml))
    p = {n: board.authenticate(t) for n, t in tokens.items()}
    nodes.hello(board, p["laptop"], [PROJECT], ["claude-code"], 1, sessions={"max_live": 2})
    return board, p, toml, local


NODES = f'[dispatch.nodes.laptop]\nprojects = ["{PROJECT}"]\nagents = ["claude"]\n'


def test_a_decision_during_a_recovering_reload_never_sees_the_old_table_without_the_error(tmp_path):
    """Board.reload_settings clears `settings_error` before it publishes the new [dispatch] table. A decision read
    between those two assignments saw "no error" beside the OLD table (denied before, allowed during, denied after).
    The decision reads both as one snapshot under the reload's own lock: the writer is held exactly between the two
    assignments, and a reader then waits for it and sees the new table."""
    import dataclasses
    import threading
    board, p, toml, local = _disk_board(tmp_path, NODES + f'[dispatch.agent_projects]\n"{WORK}" = ["{PROJECT}"]\n')
    local.write_text(NODES + f'[dispatch.agent_projects]\n"{WORK}" = "{PROJECT}"\n')     # a broken edit
    assert board.reload_settings(force=True) is False
    assert agent_accounts.hub_refusal(board, WORK, PROJECT) == agent_accounts.UNLOADED, "denied before"
    local.write_text(NODES + f'[dispatch.agent_projects]\n"{WORK}" = ["{P2}"]\n')       # the fix: not PROJECT
    reached, go, out = threading.Event(), threading.Event(), {}

    class Gate(type(board.s)):
        def __setattr__(self, name, value):
            if name == "dispatch" and getattr(self, "_armed", False):
                assert board.settings_error is None, "(the error is cleared first, as on main)"
                reached.set()
                go.wait(10)
            object.__setattr__(self, name, value)
    gated = Gate(**{f.name: getattr(board.s, f.name) for f in dataclasses.fields(board.s)})
    object.__setattr__(gated, "_armed", True)
    board.s = gated
    writer = threading.Thread(target=lambda: out.update(applied=board.reload_settings(force=True)))
    writer.start()
    assert reached.wait(10), "the writer is between the two assignments"
    reader = threading.Thread(target=lambda: out.update(why=agent_accounts.hub_refusal(board, WORK, PROJECT)))
    reader.start()
    reader.join(0.3)
    assert reader.is_alive(), "the decision waits for the reload to finish"
    go.set()
    writer.join(10)
    reader.join(10)
    assert out["applied"] is True
    assert out["why"] is not None and out["why"].startswith(NOT_IN + PROJECT), \
        "never allowed in the gap: it saw the new table"


def test_a_board_without_the_list_keeps_mains_behaviour_under_a_broken_edit(tmp_path):
    """Neither the last good settings nor the files on disk say anything of the list: a broken edit keeps the last
    good settings and starts go on, exactly as on main."""
    board, p, toml, local = _disk_board(tmp_path, NODES)
    local.write_text(NODES + "[dispatch]\nno_such_setting = 1\n")
    assert board.reload_settings(force=True) is False and board.settings_error
    assert agent_accounts.hub_refusal(board, "claude", PROJECT) is None
    assert agent_accounts.hub_refusal(board, WORK, P2) is None
    allowed = DispatchConfig.from_dict(board.s.dispatch).nodes
    out = hosted.start(board, p["human"], PROJECT, "go", agent="claude", allowed=allowed)
    assert out["session"]["state"] == "idle", "a hosted session starts on the last good settings"


def test_a_broken_edit_that_mentions_the_list_refuses_starts_though_the_last_good_settings_had_none(tmp_path):
    board, p, toml, local = _disk_board(tmp_path, NODES)
    local.write_text(NODES + f'[dispatch.agent_projects]\n"{WORK}" = "{PROJECT}"\n')     # being added, wrongly
    assert board.reload_settings(force=True) is False and board.settings_error
    assert "agent_projects" not in board.s.dispatch, "the last good settings had no table"
    for agent, project in (("claude", PROJECT), (WORK, PROJECT)):
        assert agent_accounts.hub_refusal(board, agent, project) == agent_accounts.UNLOADED
    with pytest.raises(Conflict, match="settings did not load"):
        hosted.start(board, p["human"], PROJECT, "go", agent="claude",
                     allowed=DispatchConfig.from_dict(board.s.dispatch).nodes)
    # Decided from the parsed keys when the file parses: a quoted key with an escape is the same key, and the text
    # in a comment is not one.
    local.write_text(NODES + '[dispatch]\n"agent\\u005fprojects" = 5\n')
    assert b"agent_projects" not in local.read_bytes()
    assert board.reload_settings(force=True) is False
    assert agent_accounts.hub_refusal(board, "claude", PROJECT) == agent_accounts.UNLOADED, "an escaped spelling"
    local.write_text(NODES + "[dispatch]\nno_such_setting = 1\n# agent_projects comes next\n")
    assert board.reload_settings(force=True) is False
    assert agent_accounts.hub_refusal(board, "claude", PROJECT) is None, "a comment sets nothing"
    # The key may be in board.toml too: the loader reads both.
    local.write_text(NODES + "[dispatch]\nno_such_setting = 1\n")
    toml.write_text(toml.read_text() + f'[dispatch.agent_projects]\n"{WORK}" = ["{PROJECT}"]\n')
    assert board.reload_settings(force=True) is False
    assert agent_accounts.hub_refusal(board, "claude", PROJECT) == agent_accounts.UNLOADED
    toml.write_text(toml.read_text().replace(f'[dispatch.agent_projects]\n"{WORK}" = ["{PROJECT}"]\n', ""))
    assert board.reload_settings(force=True) is False
    assert agent_accounts.hub_refusal(board, "claude", PROJECT) is None, "no mention left: main's behaviour again"
    # A file that does not parse: the literal text decides (the fallback).
    local.write_text(NODES + "[dispatch\nagent_projects =\n")
    assert board.reload_settings(force=True) is False
    assert agent_accounts.hub_refusal(board, "claude", PROJECT) == agent_accounts.UNLOADED
    local.write_text(NODES + "[dispatch\nsomething = \n")
    assert board.reload_settings(force=True) is False
    assert agent_accounts.hub_refusal(board, "claude", PROJECT) is None


def test_an_unreadable_or_overlong_settings_file_counts_as_mentioning_the_list(tmp_path, monkeypatch):
    board, p, toml, local = _disk_board(tmp_path, NODES)
    local.write_text(NODES + "[dispatch]\nno_such_setting = 1\n")
    assert board.reload_settings(force=True) is False and board.settings_error
    assert agent_accounts.hub_refusal(board, "claude", PROJECT) is None
    local.chmod(0)
    try:
        assert agent_accounts.hub_refusal(board, "claude", PROJECT) == agent_accounts.UNLOADED, "cannot be read"
    finally:
        local.chmod(0o600)
    monkeypatch.setattr(agent_accounts, "MENTION_READ_MAX", 16)
    assert agent_accounts.hub_refusal(board, "claude", PROJECT) == agent_accounts.UNLOADED, "longer than the bound"
    monkeypatch.undo()
    local.unlink()
    toml.write_text(toml.read_text() + "[dispatch]\nno_such_setting = 1\n")
    assert board.reload_settings(force=True) is False
    assert agent_accounts.hub_refusal(board, "claude", PROJECT) is None, "an absent board.local.toml mentions nothing"
    built = make_env(tmp_path / "built").board       # settings built in code: no file to search
    built.settings_error = "set by hand"
    assert agent_accounts.hub_refusal(built, "claude", PROJECT) == agent_accounts.UNLOADED


# ------------------------------------------------------------ hosted sessions and child sessions


HUB = {"nodes": {"laptop": {"projects": [PROJECT, P2, P3], "agents": ["claude", "codex", WORK]}},
       "sessions": {"spawn": {"enabled": True}}, "agent_projects": {WORK: [PROJECT]}}


@pytest.fixture
def aenv(tmp_path, home):
    w = str(home / ".claude-work")
    env = session_env(tmp_path, dispatch=json.loads(json.dumps(HUB)),
                      config=lambda e: tree_config(e, runners={WORK: SHIPPED}, runner_homes={WORK: w},
                                                    sessions={"max_live": 12}))
    env.tokens[WORK] = create_agent(env.settings.agents_path, WORK, "claude-code")
    env.p[WORK] = env.board.authenticate(env.tokens[WORK])
    env.work_home = w
    return env


def opt_in(env, *projects) -> None:
    env.board.s.dispatch = {**env.board.s.dispatch, "agent_projects": {WORK: list(projects)}}


def env_of(env, sid) -> dict:
    return proc_of(env, sid)[0]["env"]


def argv_of(env, sid) -> list[str]:
    call = proc_of(env, sid)[0]
    return [x if x != sid else "<sid>" for x in call["argv"]]


def test_a_hosted_root_of_the_work_agent_gets_its_folder_and_otherwise_the_same_command_line(aenv):
    work = root(aenv, agent=WORK)
    mine = root(aenv, agent="claude")
    assert env_of(aenv, work)["CLAUDE_CONFIG_DIR"] == aenv.work_home
    assert "CLAUDE_CONFIG_DIR" not in env_of(aenv, mine), "the personal runner: no folder, as before"
    assert {k: v for k, v in env_of(aenv, work).items() if k != "CLAUDE_CONFIG_DIR"} == env_of(aenv, mine)
    assert argv_of(aenv, work) == argv_of(aenv, mine), "the folder is the environment's only: the argv is unchanged"
    assert show(aenv, work)["account_home"] == aenv.work_home and "account_home" not in show(aenv, mine)
    listed = {s["session_id"]: s for s in aenv.human.get("/api/hosted-sessions").json()["sessions"]}
    assert listed[work]["account_home"] == aenv.work_home and "account_home" not in listed[mine]
    assert f"{WORK} (account folder {aenv.work_home}) in {PROJECT}" in cli._fmt_hosted(listed[work])
    assert "account folder" not in cli._fmt_hosted(listed[mine])
    trees = aenv.human.get("/api/hosted-trees").json()
    tops = {t["root"]: t["session"] for t in trees["trees"]}
    assert tops[work]["account_home"] == aenv.work_home and "account_home" not in tops[mine], "the Sessions tab too"


def test_a_root_in_a_project_the_agent_is_not_opted_in_to_is_refused_with_the_reason_and_nothing_is_made(aenv):
    r = aenv.human.post("/api/hosted-sessions", json={"project": P2, "message": "x", "agent": WORK})
    assert r.status_code == 409 and r.json()["message"].startswith(NOT_IN + P2), r.text
    assert hosted._sessions(aenv.board.conn) == []
    assert root(aenv, agent="claude", project=P2), "an agent with no entry runs anywhere, as before"
    opt_in(aenv, PROJECT, P2)
    assert root(aenv, agent=WORK, project=P2), "once opted in, it starts"


def test_the_refusal_is_decided_again_inside_the_write_that_makes_the_session(aenv, monkeypatch):
    real, seen = agent_accounts.check, []

    def first_passes(board, agent, project):     # the list changes between the first look and the write
        seen.append(project)
        if len(seen) == 1:
            return None
        return real(board, agent, project)
    monkeypatch.setattr(agent_accounts, "check", first_passes)
    opt_in(aenv)        # nowhere
    with pytest.raises(Conflict, match=NOT_IN):
        hosted.start(aenv.board, aenv.p["human"], PROJECT, "x", agent=WORK, allowed=HUB["nodes"])
    assert len(seen) == 2 and hosted._sessions(aenv.board.conn) == []


def test_a_child_of_the_work_agent_runs_only_where_it_is_opted_in_and_gets_the_folder(aenv):
    there, elsewhere = root(aenv, agent="claude"), root(aenv, agent="claude", project=P2)
    answer(aenv, there)
    answer(aenv, elsewhere)
    out = child(aenv, elsewhere, agent=WORK, expect=409)
    assert out["message"].startswith(NOT_IN + P2)
    kid = child(aenv, there, agent=WORK, access="read-only")["session_id"]
    tick(aenv)
    assert env_of(aenv, kid)["CLAUDE_CONFIG_DIR"] == aenv.work_home
    same = child(aenv, there, agent="claude", access="read-only")["session_id"]
    tick(aenv)
    assert argv_of(aenv, kid) == argv_of(aenv, same), "a read-only child: the same narrowed command line"
    assert "CLAUDE_CONFIG_DIR" not in env_of(aenv, same)
    # A work root that has children of the personal agent: those never get the work folder.
    wroot = root(aenv, agent=WORK)
    answer(aenv, wroot)
    under = child(aenv, wroot, agent="claude")["session_id"]
    tick(aenv)
    assert "CLAUDE_CONFIG_DIR" not in env_of(aenv, under)


def test_the_childs_check_is_made_again_inside_create_childs_transaction(aenv, monkeypatch):
    sid = root(aenv, agent="claude")
    answer(aenv, sid)
    calls = []
    real = agent_accounts.check

    def changes(board, agent, project):
        calls.append(agent)
        if len(calls) >= 3:     # the first look and placement passed; the list changed before the write
            opt_in(aenv)
        return real(board, agent, project)
    monkeypatch.setattr(agent_accounts, "check", changes)
    out = child(aenv, sid, agent=WORK, expect=409)
    assert out["message"].startswith(NOT_IN) and len(calls) == 3
    assert stored(aenv, sid).get("child_counts") in (None, {}), "nothing was made"


def test_an_agent_spawn_offers_and_starts_the_work_agent_only_where_it_is_opted_in(aenv):
    there, elsewhere = root(aenv, agent="claude"), root(aenv, agent="claude", project=P2)
    answer(aenv, there)
    answer(aenv, elsewhere)
    grant(aenv, agents=("claude",))
    grant(aenv, agents=("claude",), project=P2)
    caps = hs.capabilities(aenv.board, aenv.p["claude"], bound(aenv, there))
    assert WORK in {t["agent"] for t in caps["targets"]}
    caps = hs.capabilities(aenv.board, aenv.p["claude"], bound(aenv, elsewhere))
    assert caps["may_spawn"] and {t["agent"] for t in caps["targets"]} == {"claude", "codex"}, \
        "never offered where it may not run"
    with pytest.raises(Conflict, match=NOT_IN + P2):
        hs.spawn(aenv.board, aenv.p["claude"], bound(aenv, elsewhere), task="x", agent=WORK)
    out = hs.spawn(aenv.board, aenv.p["claude"], bound(aenv, there), task="look", agent=WORK, access="read-only")
    kid = out["child"]["id"]
    tick(aenv)
    assert env_of(aenv, kid)["CLAUDE_CONFIG_DIR"] == aenv.work_home and kid_turns(aenv, kid)
    # The frame a child is given lists what it could start itself: the work agent only in its own project.
    helper = hs.spawn(aenv.board, aenv.p["claude"], bound(aenv, elsewhere), task="look")["child"]["id"]
    tick(aenv)
    [frame] = kid_turns(aenv, helper)
    line = frame.split("\n")[1]
    assert line.startswith("You may start helper sessions") and WORK not in line, line
    near = hs.spawn(aenv.board, aenv.p["claude"], bound(aenv, there), task="look")["child"]["id"]
    tick(aenv)
    assert WORK in kid_turns(aenv, near)[0].split("\n")[1]


def test_an_interactive_caller_in_another_project_is_held_to_the_list_too(aenv):
    grant(aenv, agents=("claude",), project=P2)
    caps = hs.capabilities(aenv.board, aenv.p["claude"], interactive(aenv, project=P2), project=P2)
    assert WORK not in {t["agent"] for t in caps.get("targets", [])}
    with pytest.raises(Conflict, match=NOT_IN + P2):
        hs.spawn(aenv.board, aenv.p["claude"], interactive(aenv, project=P2), task="x", agent=WORK, project=P2)


def test_the_work_agent_starts_the_personal_agents_helpers_and_the_other_way_round(aenv):
    """Sessions on the two accounts work together: each may start the other's helper within the grants."""
    w, p = root(aenv, agent=WORK), root(aenv, agent="claude")
    answer(aenv, w)
    answer(aenv, p)
    grant(aenv, agents=("claude", WORK))
    a = hs.spawn(aenv.board, aenv.p[WORK], bound(aenv, w), task="t", agent="claude")
    b = hs.spawn(aenv.board, aenv.p["claude"], bound(aenv, p), task="t", agent=WORK)
    tick(aenv)
    assert "CLAUDE_CONFIG_DIR" not in env_of(aenv, a["child"]["id"])
    assert env_of(aenv, b["child"]["id"])["CLAUDE_CONFIG_DIR"] == aenv.work_home


# ------------------------------------------------------------ wakes


def wake_row(env, sid):
    return env.board.conn.execute("SELECT value, updated_at FROM board_state WHERE key = ?",
                                  (hosted_wake.WAKE_PREFIX + sid,)).fetchone()


def kinds_of(env, sid):
    return [(m["kind"], m["state"]) for m in show(env, sid)["messages"]]


def later(env, seconds=120):
    env.clock.advance(seconds)
    env.node.hello()


def test_an_existing_hosted_session_is_still_woken_after_its_project_leaves_the_list(aenv):
    """The owner's ruling: a wake delivers a post to a session that already exists, so the list does not govern it,
    as it does not govern the human's sends or the session's children's results. Closing the session stops it."""
    sid = root(aenv, agent=WORK)
    answer(aenv, sid)
    later(aenv)
    opt_in(aenv, P2)
    tid = aenv.thread()
    aenv.post("codex", tid, "please look", "request", to=[WORK])
    tick(aenv)
    assert [k for k, _ in kinds_of(aenv, sid)] == ["user", "wake"], "woken as before"
    assert aenv.human.post(f"/api/hosted-sessions/{sid}/close").status_code == 200, "the human stops it"


def test_a_new_session_there_is_refused_while_the_existing_one_is_woken(aenv):
    sid = root(aenv, agent=WORK)
    answer(aenv, sid)
    later(aenv)
    opt_in(aenv, P2)
    r = aenv.human.post("/api/hosted-sessions", json={"project": PROJECT, "message": "x", "agent": WORK})
    assert r.status_code == 409 and NOT_IN + PROJECT in r.json()["message"], "one per (agent, project) aside: refused"
    aenv.human.post(f"/api/hosted-sessions/{sid}/close")
    tick(aenv)
    r = aenv.human.post("/api/hosted-sessions", json={"project": PROJECT, "message": "x", "agent": WORK})
    assert r.status_code == 409 and r.json()["message"].startswith(NOT_IN + PROJECT), r.text


def test_a_wake_queued_before_the_change_is_existing_work_and_goes(aenv):
    """The list decides when work is created: a wake already queued when the project is taken off goes."""
    sid = root(aenv, agent=WORK)
    answer(aenv, sid)
    later(aenv)
    aenv.post("codex", aenv.thread(), "look", "request", to=[WORK])
    hosted_wake.tick(aenv.board)                    # queued while opted in, not yet shown to the node
    [queued] = [m for m in hosted._messages(aenv.board.conn, sid) if m["state"] == "queued"]
    opt_in(aenv, P2)
    tick(aenv)
    m = next(m for m in show(aenv, sid)["messages"] if m["n"] == queued["n"])
    assert (m["kind"], m["state"]) == ("wake", "delivered")


def children_of(env):
    return env.node.session_spawner.children


def test_the_humans_own_message_to_an_existing_session_is_not_held_to_the_list(aenv):
    """Documented: the list decides what STARTS; a session that has taken a turn takes what the human sends it."""
    sid = root(aenv, agent=WORK)
    answer(aenv, sid)
    opt_in(aenv, P2)
    assert send(aenv, sid, "still mine")["n"] == 2
    tick(aenv)
    assert show(aenv, sid)["state"] != "ended" and kinds_of(aenv, sid)[-1] == ("user", "delivered")


def test_existing_work_proceeds_when_its_project_is_taken_off_the_list(aenv):
    """The owner's ruling: the list is decided when work is created, nowhere else. A root with no process yet, a
    child's queued first task (an agent's and the human's) all go; the human closes them to stop them."""
    sid = start(aenv, agent=WORK)["session_id"]         # made while opted in; its node has not been shown it
    parent = root(aenv, agent="claude")
    answer(aenv, parent)
    grant(aenv, agents=("claude",))
    kid = hs.spawn(aenv.board, aenv.p["claude"], bound(aenv, parent), task="look", agent=WORK)["child"]["id"]
    human_kid = child(aenv, parent, agent=WORK)["session_id"]
    opt_in(aenv, P2)
    tick(aenv, times=2)
    for k in (sid, kid, human_kid):
        assert [c for c in aenv.node.session_spawner.calls if k in c["argv"]], "its process was started"
        assert stored(aenv, k)["state"] != "ended"
        assert hosted._messages(aenv.board.conn, k)[0]["state"] == "delivered"
    assert aenv.human.post(f"/api/hosted-sessions/{sid}/close").status_code == 200, "the human stops it"
    with pytest.raises(Conflict, match=NOT_IN):         # while new work there is refused
        hs.spawn(aenv.board, aenv.p["claude"], bound(aenv, parent), task="more", agent=WORK)


# ------------------------------------------------------------ the node's report of the folder


def test_the_node_reports_its_account_folders_and_an_older_hub_still_takes_its_hello(aenv):
    sent = []
    real = aenv.node.client.request

    def spy(method, path, **kw):
        sent.append(kw.get("json"))
        return real(method, path, **kw)
    aenv.node.client.request = spy
    aenv.node.hello()
    assert sent[-1]["homes"] == {WORK: aenv.work_home}
    info = nodes._get(aenv.board.conn, nodes.NODE_PREFIX + "laptop")
    assert info["homes"] == {WORK: aenv.work_home}

    class OlderHub:
        """A hub from before account folders: a hello that carries `homes` is a 422."""

        def __init__(self):
            self.bodies = []

        def request(self, method, path, **kw):
            self.bodies.append(dict(kw.get("json") or {}))
            if path == "/api/nodes/hello" and "homes" in (kw.get("json") or {}):
                return httpx.Response(422, request=httpx.Request(method, "http://hub" + path), json={"detail": []})
            return real(method, path, **kw)
    older = OlderHub()
    aenv.node.client = older
    out = aenv.node.hello()
    assert out["node"] == "laptop" and [("homes" in b) for b in older.bodies] == [True, False]
    other = make_node_without_homes(aenv)
    assert "homes" not in other, "a node without folders sends the hello it always sent"


def make_node_without_homes(env) -> dict:
    from agent_comms.node import Node
    sent = []

    class Spy:
        def request(self, method, path, **kw):
            sent.append(kw.get("json"))
            return env.laptop.request(method, path, **kw)
    Node(Spy(), tree_config(env), env.tmp / "plain-logs").hello()
    return sent[-1]


def test_the_hub_takes_only_paths_for_runners_the_node_offers(aenv):
    p = aenv.p["laptop"]
    for bad in ({"nobody": "/x"}, {WORK: "relative"}, {WORK: 7}, {WORK: "/x\n"}, ["x"]):
        with pytest.raises(Exception, match="homes maps runner keys"):
            nodes.hello(aenv.board, p, [PROJECT], [WORK], 1, homes=bad)
    assert "homes" not in nodes.hello(aenv.board, p, [PROJECT], [WORK], 1), "no field: nothing stored"


# ------------------------------------------------------------ dispatched runs: the hub's own dispatcher and a node


@pytest.fixture
def denv(tmp_path, home, monkeypatch):
    monkeypatch.setattr(dispatch.shutil, "which", lambda executable, **kw: "/fake/" + executable)
    monkeypatch.setattr(dispatch, "git_run_directory_blocker", lambda cwd, env: None)
    env = make_env(tmp_path)
    workdir = tmp_path / "repo"
    workdir.mkdir()
    env.workdir, env.work_home = str(workdir), str(home / ".claude-work")
    env.config = DispatchConfig.from_dict({
        "runners": {"claude": ["claude", "-p", "{prompt}"], "grok": ["grok", "{prompt}"]},
        "runner_homes": {"claude": env.work_home}, "worktrees": {PROJECT: str(workdir)},
        "live_minutes": 2, "timeout_minutes": 30, "kill_grace_seconds": 10, "max_concurrent": 2})
    env.spawner = FakeSpawner()
    env.board.notifier = Recorder()
    env.log_dir = tmp_path / "dispatch-logs"
    env.procs = FakeProcs(env.spawner)
    env.d = new_dispatcher(env)
    env.tid = env.thread("t")
    env.d.tick()
    env.clock.advance(5 * 60)
    env.board.create_dispatch_rule(env.p["human"], thread_id=env.tid, agents=["claude", "grok"], purpose="p",
                                   max_launches=10)
    return env


def test_a_dispatched_run_here_gets_its_runners_folder_and_another_runner_its_old_environment(denv, tmp_path):
    other_dir = tmp_path / "second"
    other_dir.mkdir()
    denv.d.config.worktrees[P2] = str(other_dir)
    tid2 = denv.board.create_thread(denv.p["human"], denv.sid["human"], "second", P2)["id"]
    denv.board.create_dispatch_rule(denv.p["human"], thread_id=tid2, agents=["grok"], purpose="p", max_launches=5)
    human_post(denv, ["claude"])
    human_post(denv, ["grok"], thread_id=tid2)
    denv.d.tick()
    by = {c["argv"][0]: c for c in denv.spawner.calls}
    assert by["claude"]["env"]["CLAUDE_CONFIG_DIR"] == denv.work_home
    assert by["claude"]["argv"][:2] == ["claude", "-p"] and len(by["claude"]["argv"]) == 3, "argv unchanged"
    assert by["grok"]["env"] == child_env("grok", DispatchConfig.from_dict({"runners": {"grok": ["grok", "{prompt}"]}}),
                                          runtime="grok"), "the other runner's environment, as before"


def test_a_launch_in_a_project_the_agent_is_not_opted_in_to_is_refused_with_the_reason(denv):
    denv.board.s.dispatch = {"agent_projects": {"claude": ["/elsewhere"]}}
    post = human_post(denv, ["claude"])
    denv.d.tick()
    assert denv.spawner.calls == []
    [r] = runs(denv)
    assert r["status"] == "preflight_failed" and r["error"].startswith("claude is not opted in to " + PROJECT)
    [req] = denv.board.get_post(denv.p["human"], post["id"])["requests"]
    assert req["state"] == "blocked"
    human_post(denv, ["grok"])
    denv.d.tick()
    assert [c["argv"][0] for c in denv.spawner.calls] == ["grok"], "an agent with no entry: as before"


def test_the_launch_is_refused_again_inside_its_reservation(denv):
    denv.d._preflight = lambda *a, **k: None        # the list changed after preflight passed
    denv.board.s.dispatch = {"agent_projects": {"claude": []}}
    human_post(denv, ["claude"])
    denv.d.tick()
    assert denv.spawner.calls == [] and runs(denv) == []
    assert any(i["agent"] == "claude" for i in denv.d._get(dispatch.Dispatcher.PENDING_KEY).values()), \
        "it stays pending; the next pass's preflight records why"
    del denv.d._preflight
    denv.d.tick()
    assert runs(denv)[0]["error"].startswith("claude is not opted in to")


def watcher_rows(env):
    return env.board.conn.execute("SELECT key, value, updated_at FROM board_state WHERE key LIKE 'host.wake.%' "
                                  "OR key LIKE 'wake.%' OR key LIKE 'dispatch.checkout_claim.%' ORDER BY key").fetchall()


def test_a_refused_reservation_leaves_the_hosted_and_interactive_wake_watchers_untouched(aenv):
    from agent_comms.dispatch import Dispatcher
    from test_interactive_wake import on, wait
    on(aenv, spawn={"enabled": True})
    wait(aenv, who=WORK)                        # the human's own session of the work agent waits in PROJECT
    sid = root(aenv, agent=WORK)                # a hosted root, its first turn in flight: a post waits for it
    tid = aenv.thread()
    post = aenv.post("codex", tid, "please look", "request", to=[WORK])
    hosted_wake.tick(aenv.board)
    assert str(post["id"]) in hosted_wake._load(aenv.board.conn, sid)["pending"]
    d = Dispatcher(aenv.board, aenv.p["human"], DispatchConfig.from_dict({}), spawner=FakeSpawner(),
                   log_dir=aenv.tmp / "dlogs")
    item = {"agent": WORK, "post_id": post["id"], "thread_id": tid}
    before = watcher_rows(aenv)
    opt_in(aenv, P2)
    check = d._launch_check(WORK, item, aenv.clock(), PROJECT, str(aenv.work), thread_id=tid)["check"]
    assert check().startswith(NOT_IN + PROJECT)
    assert watcher_rows(aenv) == before, "refused before the hold claimed the request from either watcher"
    opt_in(aenv, PROJECT)
    assert d._launch_check(WORK, item, aenv.clock(), PROJECT, str(aenv.work), thread_id=tid)["check"]() is None
    assert str(post["id"]) in hosted_wake._load(aenv.board.conn, sid)["claimed"], "(allowed, it does claim it)"


def test_a_runner_passing_its_variable_through_dispatch_env_is_refused_at_preflight(denv):
    denv.d.config.env = {"claude-code": ["CLAUDE_CONFIG_DIR"]}
    denv.d.config.runners["claude-code"] = denv.d.config.runners.pop("claude")
    denv.d.config.runner_homes = {"claude-code": denv.work_home}
    human_post(denv, ["claude"])
    denv.d.tick()
    assert denv.spawner.calls == [] and "set it one way only" in runs(denv)[0]["error"]


def fake_os_host(spawner) -> dict:
    """A SessionHost's spawner and, for the fake children it hands out, FakeProcs's answers about their processes
    (identity, process group, signals) instead of the OS's: a fake pid can be any live process on the machine."""
    procs = FakeProcs(spawner)
    return {"spawner": spawner, "probe": procs.probe, "start_of": procs.start, "signal_group": procs.signal_group,
            "group_alive": procs.group_alive}


def node_env(tmp_path, home, monkeypatch):
    """A hub whose dispatcher sends codex's work in PROJECT to the node `laptop`, which has a Codex runner with an
    account folder (whose config.toml has a stdio board server)."""
    from agent_comms.node import Node
    from test_nodes import Clock, fake_os
    from fastapi.testclient import TestClient
    from agent_comms.api import create_app
    monkeypatch.setattr(dispatch.shutil, "which", lambda executable, **kw: "/fake/" + executable)
    monkeypatch.setattr("agent_comms.node.shutil.which", lambda executable, **kw: "/fake/" + executable)
    monkeypatch.setattr(dispatch, "git_run_directory_blocker", lambda cwd, env: None)
    env = make_env(tmp_path, dispatch={"agent_projects": {"codex": [PROJECT]}})
    env.board.notifier = Recorder()
    env.config = DispatchConfig.from_dict({"nodes": {"laptop": {"projects": [PROJECT], "agents": ["codex"]}}})
    env.spawner, env.log_dir = FakeSpawner(), tmp_path / "dispatch-logs"
    env.procs = FakeProcs(env.spawner)
    env.d = new_dispatcher(env)
    env.tid = env.thread("t")
    env.d.tick()
    env.clock.advance(5 * 60)
    node_dir = tmp_path / "laptop-checkout"
    node_dir.mkdir()
    token = create_agent(env.settings.agents_path, "laptop", "node")
    env.p["laptop"] = env.board.authenticate(token)
    client = TestClient(create_app(env.board), base_url="http://127.0.0.1:8787",
                        headers={"Authorization": "Bearer " + token})
    codex_home = home / ".codex-work"
    (codex_home / "config.toml").write_text('[mcp_servers.agent-comms]\ncommand = "bash"\n')
    node_config = DispatchConfig.from_dict({
        "runners": {"codex": ["codex", "exec", "{prompt}"]}, "runner_homes": {"codex": str(codex_home)},
        "worktrees": {PROJECT: str(node_dir)}, "max_concurrent": 1})
    spawner = FakeSpawner()
    node = Node(client, node_config, tmp_path / "node-logs", spawner=spawner, clock=Clock(env),
                **fake_os(FakeProcs(spawner)))
    node.hello()
    env.board.create_dispatch_rule(env.p["human"], thread_id=env.tid, agents=["codex"], purpose="p", max_launches=5)
    return env, node, spawner, codex_home


def test_a_node_run_gets_the_nodes_folder_and_its_codex_config_comes_from_there(tmp_path, home, monkeypatch):
    env, node, spawner, codex_home = node_env(tmp_path, home, monkeypatch)
    human_post(env, ["codex"])
    env.d.tick()
    node.tick()
    [call] = spawner.calls
    assert call["env"]["CODEX_HOME"] == str(codex_home)
    assert 'mcp_servers.agent-comms.env.AGENT_COMMS_MANAGED="1"' in call["argv"], \
        "the run's board server is found in the account's own config.toml (none under ~/.codex here)"
    # The same node with the variable also passed through [dispatch.env] by runtime: a failed start, with the reason.
    node.config.env = {"codex-cli": ["CODEX_HOME"]}
    spawner.children[0].code = 0
    node.tick()
    env.d.tick()
    env.clock.advance(5 * 60)
    node.hello()
    human_post(env, ["codex"])
    env.d.tick()
    node.tick()
    assert len(spawner.calls) == 1, "nothing started"
    ended = [nodes.run(env.board, x["run_id"]) for x in runs(env)]
    assert any("set it one way only" in (r.get("error") or "") for r in ended), ended


def test_an_assigned_run_is_existing_work_and_starts_after_its_project_is_taken_off(tmp_path, home, monkeypatch):
    env, node, spawner, _ = node_env(tmp_path, home, monkeypatch)
    human_post(env, ["codex"])
    env.d.tick()
    [r] = runs(env)
    assert nodes.run(env.board, r["run_id"])["state"] == "assigned", "assigned while codex was opted in"
    env.board.s.dispatch = {"agent_projects": {"codex": [P2]}}
    node.tick()
    assert len(spawner.calls) == 1 and nodes.run(env.board, r["run_id"])["state"] == "running"
    spawner.children[0].code = 0
    node.tick()
    env.d.tick()
    env.clock.advance(5 * 60)
    node.hello()
    human_post(env, ["codex"])
    env.d.tick()
    assert [x for x in runs(env) if x["status"] == "preflight_failed" and NOT_IN.replace(WORK, "codex") in
            x["error"]], "a new launch there is refused"


def test_a_running_node_run_is_never_touched_by_the_list(tmp_path, home, monkeypatch):
    env, node, spawner, _ = node_env(tmp_path, home, monkeypatch)
    human_post(env, ["codex"])
    env.d.tick()
    node.tick()
    [r] = runs(env)
    assert nodes.run(env.board, r["run_id"])["state"] == "running"
    env.board.s.dispatch = {"agent_projects": {"codex": []}}
    node.tick()
    assert nodes.run(env.board, r["run_id"])["state"] == "running" and spawner.children[0].terminated == 0


# ------------------------------------------------------------ the session host on the node


def test_a_hub_local_run_directory_inside_an_account_folder_is_refused_at_launch(denv, home):
    inside = home / ".claude-work" / "proj"
    inside.mkdir()
    tid = denv.board.create_thread(denv.p["human"], denv.sid["human"], "inside", str(inside))["id"]
    denv.board.create_dispatch_rule(denv.p["human"], thread_id=tid, agents=["grok"], purpose="p", max_launches=5)
    human_post(denv, ["grok"], thread_id=tid)       # no [dispatch.worktrees] entry: the project path is the directory
    denv.d.tick()
    assert denv.spawner.calls == []
    [r] = runs(denv)
    assert r["status"] == "preflight_failed" and "is, is inside or contains an account folder" in r["error"], \
        "any runner's: grok has no folder of its own"


def test_the_folder_is_checked_again_at_each_start(tmp_path, home):
    w = home / ".claude-work"
    c = cfg(runners={WORK: SHIPPED}, runner_homes={WORK: str(w)})
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    assert c.launch_blocker(WORK, "claude-code", str(elsewhere)) is None
    w.rename(home / ".claude-work-real")
    w.symlink_to(home / ".claude-work-real")
    assert "symlink" in c.launch_blocker(WORK, "claude-code", str(elsewhere)), "replaced since the settings were read"
    w.unlink()
    assert "not an existing folder" in c.launch_blocker(WORK, "claude-code", str(elsewhere))


def test_a_session_whose_run_directory_is_in_an_account_folder_is_refused(tmp_path, home):
    w = home / ".claude-work"
    (w / "wt").mkdir()
    work = tmp_path / "checkout"
    work.mkdir()
    c = DispatchConfig.from_dict({"runners": {"claude-code": SHIPPED, WORK: SHIPPED}, "runner_homes": {WORK: str(w)},
                                  "worktrees": {PROJECT: str(work)}})
    host = session_host.SessionHost(c, tmp_path / "logs", **fake_os_host(FakeSpawner()))
    with pytest.raises(LookupError, match="account folder"):     # a child's own worktree, say: any agent's session
        host.open("5b0c1f2e-7a6d-4e3b-9c8a-1d2e3f4a5b6c", agent="claude", runtime="claude-code", project=PROJECT,
                  cwd=str(w / "wt"))
    assert host.open("5b0c1f2e-7a6d-4e3b-9c8a-1d2e3f4a5b6c", agent="claude", runtime="claude-code", project=PROJECT)


def test_the_folder_and_run_directory_are_checked_again_when_the_hosted_process_starts(aenv):
    """`open` checks them; a process started later (a delayed first turn, a respawn) checks them again."""
    sid = root(aenv, agent=WORK)
    answer(aenv, sid)
    proc_of(aenv, sid)[1].code = 0          # its process exits: the next message starts another
    tick(aenv)
    w = aenv.work_home
    os.rename(w, w + "-real")
    os.symlink(w + "-real", w)
    send(aenv, sid, "again")
    tick(aenv, times=2)
    view = show(aenv, sid)
    assert view["state"] == "ended" and "symlink" in view["error"], view["error"]
    assert len([c for c in aenv.node.session_spawner.calls if sid in c["argv"]]) == 1, "no second process"


def test_a_hub_local_run_checks_the_folder_again_on_its_final_directory_before_it_spawns(denv, home):
    denv.d._preflight = lambda *a, **k: None         # preflight passed; the folder changed before the spawn
    w = denv.work_home
    os.rename(w, w + "-real")
    os.symlink(w + "-real", w)
    human_post(denv, ["claude"])
    denv.d.tick()
    assert denv.spawner.calls == []
    [r] = runs(denv)
    assert r["status"] == "spawn_failed" and "symlink" in r["error"]


def test_a_childs_own_worktree_is_never_made_in_an_account_folder(tmp_path, home):
    from agent_comms import session_worktrees
    c = cfg(runners={WORK: SHIPPED}, runner_homes={WORK: str(home / ".claude-work")},
            worktree_dir=str(tmp_path / "wt"))
    c.runner_homes = {WORK: str(tmp_path)}      # the folder now holds where worktrees go (changed since the load)
    with pytest.raises(Conflict, match="account folder"):
        session_worktrees.ensure(c, str(tmp_path / "checkout"), "5b0c1f2e-7a6d-4e3b-9c8a-1d2e3f4a5b6c")
    assert not (tmp_path / "wt").exists(), "nothing was made"


def test_the_human_token_file_is_protected_where_it_resolves(home, tmp_path, monkeypatch):
    real = home / ".claude-work" / "human.token"
    real.write_text("t\n")
    (tmp_path / "cfg").mkdir()
    (tmp_path / "cfg" / "human.token").symlink_to(real)
    monkeypatch.setenv("AGENT_COMMS_TOKEN_FILE", str(tmp_path / "cfg" / "human.token"))
    with pytest.raises(ValueError, match="token file"):
        cfg(runners={WORK: SHIPPED}, runner_homes={WORK: str(home / ".claude-work")})


def test_signed_in_work_is_never_routed_to_a_holder_the_list_bars_from_the_project(tmp_path, monkeypatch):
    from test_access_routing import URL, hub_checkout, laptop, signed_in_request
    from test_dispatch import allow
    from test_nodes import nenv as nenv_fixture
    from agent_comms import access
    nenv = nenv_fixture.__wrapped__(tmp_path, monkeypatch)
    hub_checkout(nenv, tmp_path)
    laptop(nenv, runner="grok")      # only grok, on the laptop, holds the sign-in
    allow(nenv, agents=["codex", "grok"])
    nenv.board.s.dispatch = {"agent_projects": {"grok": [P2]}}
    original = signed_in_request(nenv, "codex")
    tried = []
    real = access.route_successor
    monkeypatch.setattr(access, "route_successor", lambda *a, **k: (tried.append(a[4]), real(*a, **k))[1])
    nenv.d.tick()
    monkeypatch.setattr(access, "route_successor", real)
    assert tried == [], "grok may not run there: never even a candidate"
    assert not [r for r in runs(nenv) if r["status"] == "routed"]
    [row] = nenv.board.get_post(nenv.p["human"], original["id"])["requests"]
    assert "Routed by access" not in (row["reason"] or "")
    with pytest.raises(Conflict, match="grok is not opted in to " + PROJECT):     # and inside the transfer's write
        access.route_successor(nenv.board, nenv.p["human"], original["id"], "codex", "grok", [URL], "node laptop")
    assert access.routed_successor(nenv.board, original["id"]) is None
    nenv.board.s.dispatch = {}
    assert access.route_successor(nenv.board, nenv.p["human"], original["id"], "codex", "grok", [URL], "node laptop")


def allow_codex(env):
    env.board.create_dispatch_rule(env.p["human"], thread_id=env.tid, agents=["codex"], purpose="p", max_launches=5)


@pytest.mark.parametrize("spelling", [["--cd", "{w}"], ["--cd={w}"], ["-C", "{w}"], ["-C{w}"], ["-C=" "{w}"],
                                      ["--cd", "../home/.claude-work"]])
def test_a_dispatched_codex_run_whose_command_line_names_a_workspace_in_an_account_folder_is_refused(
        denv, home, monkeypatch, spelling):
    monkeypatch.setattr(dispatch, "codex_approval_reminder", lambda template: None)
    w = str(home / ".claude-work")
    denv.d.config.runners["codex"] = ["codex", "exec", *[x.format(w=w) for x in spelling], "{prompt}"]
    allow_codex(denv)
    human_post(denv, ["codex"])
    denv.d.tick()
    assert denv.spawner.calls == []
    [r] = runs(denv)
    assert r["status"] == "spawn_failed" and "the workspace its command line names" in r["error"], r["error"]


def test_a_codex_run_whose_workspace_is_its_project_is_not_refused(denv, monkeypatch):
    monkeypatch.setattr(dispatch, "codex_approval_reminder", lambda template: None)
    denv.d.config.runners["codex"] = ["codex", "exec", "--cd", "{project}", "{prompt}"]
    allow_codex(denv)
    human_post(denv, ["codex"])
    denv.d.tick()
    assert [c["argv"][0] for c in denv.spawner.calls] == ["codex"]


def test_a_writable_root_overlapping_an_account_folder_at_spawn_time_is_refused(denv, home):
    inside = home / ".claude-work" / "shared"
    inside.mkdir()
    denv.d.config.writable_roots = {PROJECT: [str(inside)]}        # (as if the settings in force said so)
    human_post(denv, ["claude"])
    denv.d.tick()
    failed = [r for r in runs(denv) if r["agent"] == "claude"]
    assert failed and failed[0]["status"] == "spawn_failed" and "the extra writable folder" in failed[0]["error"]
    assert not [c for c in denv.spawner.calls if c["argv"][0] == "claude"]


def test_a_node_run_whose_command_line_names_a_workspace_in_an_account_folder_is_refused(tmp_path, home,
                                                                                         monkeypatch):
    env, node, spawner, codex_home = node_env(tmp_path, home, monkeypatch)
    node.config.runners["codex"] = ["codex", "exec", "-C", str(codex_home / "ws"), "{prompt}"]
    human_post(env, ["codex"])
    env.d.tick()
    node.tick()
    assert spawner.calls == []
    [r] = [nodes.run(env.board, x["run_id"]) for x in runs(env)]
    assert r["state"] == "ended" and "the workspace its command line names" in r["error"], r["error"]
    node.config.runners["codex"] = ["codex", "exec", "{prompt}"]
    node.config.writable_roots = {PROJECT: [str(codex_home)]}      # a Codex run's extra roots, at the spawn
    env.d.tick()
    env.clock.advance(5 * 60)
    node.hello()
    human_post(env, ["codex"])
    env.d.tick()
    node.tick()
    assert spawner.calls == []
    assert any("the extra writable folder" in (nodes.run(env.board, x["run_id"]).get("error") or "")
               for x in runs(env)), [(x["run_id"], x["status"], x.get("error"), (nodes.run(env.board, x["run_id"]) or {}).get("error")) for x in runs(env)]


def _host_with_root(tmp_path, home):
    from test_session_host import FakeSessionChild
    w = home / ".claude-work"
    (w / "shared").mkdir()
    work = tmp_path / "checkout"
    work.mkdir()
    c = DispatchConfig.from_dict({"runners": {"claude-code": SHIPPED, WORK: SHIPPED}, "runner_homes": {WORK: str(w)},
                                  "worktrees": {PROJECT: str(work)}, "sessions": {"enabled": True}})
    c.writable_roots = {PROJECT: [str(w / "shared")]}       # (as if the settings in force said so)
    spawner = FakeSpawner(child=FakeSessionChild)
    return session_host.SessionHost(c, tmp_path / "logs", **fake_os_host(spawner)), spawner


def test_a_hosted_process_given_a_writable_root_in_an_account_folder_is_refused(tmp_path, home):
    host, spawner = _host_with_root(tmp_path, home)
    sid = "5b0c1f2e-7a6d-4e3b-9c8a-1d2e3f4a5b6c"
    host.open(sid, agent="claude", runtime="claude-code", project=PROJECT)
    assert any(x.startswith("--add-dir=") for x in host.sessions[sid].template), "the root is on its command line"
    host.send(sid, "hello")
    out = host.tick()
    assert spawner.calls == [] and any(e.kind == "ended" and "the extra writable folder" in e.detail for e in out)


def test_a_read_only_child_is_not_refused_over_a_root_it_is_not_given(tmp_path, home):
    host, spawner = _host_with_root(tmp_path, home)
    sid = "5b0c1f2e-7a6d-4e3b-9c8a-1d2e3f4a5b6c"
    host.open(sid, agent="claude", runtime="claude-code", project=PROJECT, access="read-only")
    assert not any("--add-dir" in x for x in host.sessions[sid].template), "a read-only child gets no extra roots"
    host.send(sid, "hello")
    host.tick()
    assert len(spawner.calls) == 1, "checked on what it is really given: nothing in the account folder"


def test_granted_roots_are_read_from_the_command_line_in_every_spelling():
    g = dispatch.granted_writable_roots
    assert g(["claude", "-p", "x", "--add-dir=/a", "--add-dir", "/b", "/c", "--model", "m"]) == ["/a", "/b", "/c"]
    assert g(["codex", "exec", "-c", 'sandbox_workspace_write.writable_roots=["/a", "/b"]', "x"]) == ["/a", "/b"]
    assert g(["codex", "exec", '--config=sandbox_workspace_write.writable_roots=["/c"]', "x"]) == ["/c"]
    assert g(["codex", "exec", "-c", 'sandbox_workspace_write={writable_roots=["/d"]}', "x"]) == ["/d"]
    assert g(["codex", "exec", "--", "--add-dir=/e"]) == [], "options end at --"
    with pytest.raises(ValueError):
        g(["codex", "exec", "-c", "sandbox_workspace_write.writable_roots=not toml [", "x"])


def _record_browser_mkdirs(monkeypatch) -> list:
    """Every browser output directory made from now on (a refused run removes one it made: so the making itself is
    what is watched)."""
    from pathlib import Path
    made, real = [], Path.mkdir

    def mkdir(self, *a, **k):
        if self.name.endswith("-browser"):
            made.append(str(self))
        return real(self, *a, **k)
    monkeypatch.setattr(Path, "mkdir", mkdir)
    return made


def test_a_signed_in_browser_output_directory_through_a_symlinked_log_folder_is_refused_before_it_is_made(
        tmp_path, home, monkeypatch):
    """The hub: the run's own browser output directory is a writable root the board generates. With the dispatcher's
    log folder a symlink into an account folder, the run is refused before that directory is made."""
    from test_access_routing import ORIGIN, hub_checkout, laptop, signed_in_request
    from test_dispatch import allow
    from test_nodes import nenv as nenv_fixture
    from agent_comms import access
    nenv = nenv_fixture.__wrapped__(tmp_path, monkeypatch)
    hub_checkout(nenv, tmp_path)
    profile = tmp_path / "profile"
    profile.mkdir()
    codex_home = home / ".codex-work"
    nenv.d.config = DispatchConfig.from_dict({
        "runners": {"codex": ["codex", "exec", "{prompt}"]}, "runner_homes": {"codex": str(codex_home)},
        "worktrees": {PROJECT: str(tmp_path / "hub-checkout")},
        "runner_access": {"codex": [access.browser(ORIGIN)]},
        "headless_browser": {"command": "npx", "args": ["--offline", "-y", "@playwright/mcp@0.0.83"], "runners": ["codex"]},
        "browser_profiles": {ORIGIN: {"user_data_dir": str(profile)}}})
    monkeypatch.setattr(dispatch, "codex_approval_reminder", lambda template: None)
    monkeypatch.setattr(dispatch, "codex_config_conflict", lambda env: None)
    (codex_home / "logs").mkdir()
    link = tmp_path / "dispatch-logs-link"
    link.symlink_to(codex_home / "logs")
    nenv.d.log_dir = link
    made = _record_browser_mkdirs(monkeypatch)
    allow(nenv, agents=["codex"])
    signed_in_request(nenv, "codex")
    nenv.d.tick()
    assert nenv.spawner.calls == []
    failed = [r for r in runs(nenv) if r["status"] == "spawn_failed"]
    assert failed and "the extra writable folder" in failed[0]["error"], [(r["status"], r.get("error")) for r in runs(nenv)]
    assert list((codex_home / "logs").glob("*-browser")) == [] and made == [], "the output directory was never made"


def test_a_node_runs_browser_output_through_a_symlinked_log_folder_is_refused_before_it_is_made(tmp_path, home,
                                                                                               monkeypatch):
    from test_access_routing import ORIGIN, hub_checkout, signed_in_request
    from test_dispatch import allow
    from test_nodes import nenv as nenv_fixture
    from agent_comms import access
    nenv = nenv_fixture.__wrapped__(tmp_path, monkeypatch)
    hub_checkout(nenv, tmp_path)
    nenv.d.config.nodes = {"laptop": {"projects": [PROJECT], "agents": ["grok", "codex"],
                                      "access": [access.browser(ORIGIN)]}}
    nenv.d.config.worktrees = {}            # only the node can run it
    profile = tmp_path / "profile"
    profile.mkdir()
    codex_home = home / ".codex-work"
    (codex_home / "logs").mkdir()
    link = tmp_path / "node-logs-link"
    link.symlink_to(codex_home / "logs")
    monkeypatch.setattr("agent_comms.node.shutil.which", lambda executable, **kw: "/fake/" + executable)
    monkeypatch.setattr("agent_comms.node.codex_config_conflict", lambda env: None)
    nenv.node.config = DispatchConfig.from_dict({
        "runners": {"codex": ["codex", "exec", "{prompt}"]}, "runner_homes": {"codex": str(codex_home)},
        "worktrees": {PROJECT: nenv.node_dir}, "runner_access": {"codex": [access.browser(ORIGIN)]},
        "headless_browser": {"command": "npx", "args": ["--offline", "-y", "@playwright/mcp@0.0.83"], "runners": ["codex"]},
        "browser_profiles": {ORIGIN: {"user_data_dir": str(profile)}}, "max_concurrent": 1})
    nenv.node.log_dir = link
    nenv.node.hello()
    allow(nenv, agents=["codex"])
    signed_in_request(nenv, "codex")
    nenv.d.tick()
    made = _record_browser_mkdirs(monkeypatch)
    nenv.node.tick()
    assert nenv.node_spawner.calls == []
    ended = [nodes.run(nenv.board, x["run_id"]) for x in runs(nenv)]
    assert any("the extra writable folder" in (r.get("error") or "") for r in ended), ended
    assert list((codex_home / "logs").glob("*-browser")) == [] and made == [], "the output directory was never made"


def _signed_in_hub(tmp_path, home, monkeypatch, *, overlap: bool):
    """The hub runs codex's signed-in browser work itself, in a log folder of its own; with `overlap`, the project's
    writable root is in the account folder (a refused start)."""
    from test_access_routing import ORIGIN, hub_checkout
    from test_dispatch import allow
    from test_nodes import nenv as nenv_fixture
    from agent_comms import access
    nenv = nenv_fixture.__wrapped__(tmp_path, monkeypatch)
    hub_checkout(nenv, tmp_path)
    profile = tmp_path / "profile"
    profile.mkdir()
    codex_home = home / ".codex-work"
    nenv.d.config = DispatchConfig.from_dict({
        "runners": {"codex": ["codex", "exec", "{prompt}"]}, "runner_homes": {"codex": str(codex_home)},
        "worktrees": {PROJECT: str(tmp_path / "hub-checkout")},
        "runner_access": {"codex": [access.browser(ORIGIN)]},
        "headless_browser": {"command": "npx", "args": ["--offline", "-y", "@playwright/mcp@0.0.83"], "runners": ["codex"]},
        "browser_profiles": {ORIGIN: {"user_data_dir": str(profile)}}})
    if overlap:
        nenv.d.config.writable_roots = {PROJECT: [str(codex_home)]}
    monkeypatch.setattr(dispatch, "codex_approval_reminder", lambda template: None)
    monkeypatch.setattr(dispatch, "codex_config_conflict", lambda env: None)
    allow(nenv, agents=["codex"])
    return nenv


@pytest.mark.parametrize("overlap", [False, True], ids=["leftover", "refused"])
def test_a_pre_existing_output_directory_survives_a_failed_or_refused_hub_launch(tmp_path, home, monkeypatch,
                                                                                 overlap):
    from test_access_routing import signed_in_request
    nenv = _signed_in_hub(tmp_path, home, monkeypatch, overlap=overlap)
    post = signed_in_request(nenv, "codex")
    there = nenv.d.log_dir / f"s{post['seq']}-codex-browser"      # not this launch's: it was there before
    there.mkdir(parents=True)
    (there / "keep").write_text("x")
    nenv.d.tick()
    [r] = runs(nenv)
    assert (there / "keep").read_text() == "x", "only what this launch made is ever removed"
    if overlap:
        assert r["status"] == "spawn_failed" and "account folder" in r["error"], r
        assert "signed_in_browser" not in r and "headless_browser" not in r, "nothing of it on the record"
    else:
        assert r["status"] == "running", "a leftover of that name never blocks the start: this attempt has its own"
        assert r["signed_in_browser"]["output_dir"] == str(there) + "-2"


@pytest.mark.parametrize("overlap", [False, True], ids=["leftover", "refused"])
def test_a_pre_existing_output_directory_survives_a_failed_or_refused_node_start(tmp_path, home, monkeypatch,
                                                                                 overlap):
    from test_access_routing import ORIGIN, hub_checkout, signed_in_request
    from test_dispatch import allow
    from test_nodes import nenv as nenv_fixture
    from agent_comms import access
    nenv = nenv_fixture.__wrapped__(tmp_path, monkeypatch)
    hub_checkout(nenv, tmp_path)
    nenv.d.config.nodes = {"laptop": {"projects": [PROJECT], "agents": ["grok", "codex"],
                                      "access": [access.browser(ORIGIN)]}}
    nenv.d.config.worktrees = {}
    profile = tmp_path / "profile"
    profile.mkdir()
    codex_home = home / ".codex-work"
    monkeypatch.setattr("agent_comms.node.shutil.which", lambda executable, **kw: "/fake/" + executable)
    monkeypatch.setattr("agent_comms.node.codex_config_conflict", lambda env: None)
    nenv.node.config = DispatchConfig.from_dict({
        "runners": {"codex": ["codex", "exec", "{prompt}"]}, "runner_homes": {"codex": str(codex_home)},
        "worktrees": {PROJECT: nenv.node_dir}, "runner_access": {"codex": [access.browser(ORIGIN)]},
        "headless_browser": {"command": "npx", "args": ["--offline", "-y", "@playwright/mcp@0.0.83"], "runners": ["codex"]},
        "browser_profiles": {ORIGIN: {"user_data_dir": str(profile)}}, "max_concurrent": 1})
    if overlap:
        nenv.node.config.writable_roots = {PROJECT: [str(codex_home)]}
    nenv.node.hello()
    allow(nenv, agents=["codex"])
    post = signed_in_request(nenv, "codex")
    nenv.d.tick()
    there = nenv.node.log_dir / f"s{post['seq']}-codex-browser"
    there.mkdir(parents=True)
    (there / "keep").write_text("x")
    nenv.node.tick()
    [r] = [nodes.run(nenv.board, x["run_id"]) for x in runs(nenv)]
    assert (there / "keep").read_text() == "x", "only what this start made is ever removed"
    if overlap:
        assert nenv.node_spawner.calls == [] and r["state"] == "ended" and "account folder" in r["error"]
    else:
        [call] = nenv.node_spawner.calls
        assert r["state"] == "running" and str(there) + "-2" in " ".join(call["argv"]), "its own fresh name"


def _signed_in_node(tmp_path, home, monkeypatch, log_dir=None, *, headless_only=False):
    """codex's browser work on the laptop node: signed-in (the default) or headless only."""
    from test_access_routing import ORIGIN, hub_checkout
    from test_dispatch import allow
    from test_nodes import nenv as nenv_fixture
    from agent_comms import access
    nenv = nenv_fixture.__wrapped__(tmp_path, monkeypatch)
    hub_checkout(nenv, tmp_path)
    nenv.d.config.nodes = {"laptop": {"projects": [PROJECT], "agents": ["grok", "codex"],
                                      **({} if headless_only else {"access": [access.browser(ORIGIN)]})}}
    nenv.d.config.worktrees = {}
    if headless_only:
        nenv.d.config.headless_browser = dispatch.HeadlessBrowserConfig.from_dict(
            {"command": "npx", "args": ["--offline", "-y", "@playwright/mcp@0.0.83"], "runners": ["codex"]})
    profile = tmp_path / "profile"
    profile.mkdir()
    codex_home = home / ".codex-work"
    monkeypatch.setattr("agent_comms.node.shutil.which", lambda executable, **kw: "/fake/" + executable)
    monkeypatch.setattr("agent_comms.node.codex_config_conflict", lambda env: None)
    nenv.node.config = DispatchConfig.from_dict({
        "runners": {"codex": ["codex", "exec", "{prompt}"]}, "runner_homes": {"codex": str(codex_home)},
        "worktrees": {PROJECT: nenv.node_dir},
        "headless_browser": {"command": "npx", "args": ["--offline", "-y", "@playwright/mcp@0.0.83"], "runners": ["codex"]},
        **({} if headless_only else {"runner_access": {"codex": [access.browser(ORIGIN)]},
                                     "browser_profiles": {ORIGIN: {"user_data_dir": str(profile)}}}),
        "max_concurrent": 1})
    if log_dir is not None:
        nenv.node.log_dir = log_dir
    nenv.node.hello()
    allow(nenv, agents=["codex"])
    return nenv


@pytest.mark.parametrize("linked", [False, True], ids=["real-parent", "symlinked-parent"])
def test_a_retry_of_the_same_assignment_after_a_failed_start_succeeds(tmp_path, home, monkeypatch, linked):
    """The node: a start that made its output directory and could not go on (its checkout still held, here) is tried
    again on a later pass, with the same assignment. Under a real log folder its directory was removed; under a
    symlinked one it is left (and logged once). Either way the retry takes a fresh name and starts."""
    from test_access_routing import signed_in_request
    target = tmp_path / "real-logs"
    target.mkdir()
    log_dir = None
    if linked:
        log_dir = tmp_path / "node-logs-link"
        log_dir.symlink_to(target)
    nenv = _signed_in_node(tmp_path, home, monkeypatch, log_dir)
    post = signed_in_request(nenv, "codex")
    nenv.d.tick()
    real_start, held = nenv.node.groups.start, [True]

    def start_once_held(cwd, token, spawn, **kw):
        if held:
            held.clear()
            return False, None          # a process group is still being stopped in that checkout: try again later
        return real_start(cwd, token, spawn, **kw)
    monkeypatch.setattr(nenv.node.groups, "start", start_once_held)
    nenv.node.tick()
    assert nenv.node_spawner.calls == []
    first = nenv.node.log_dir / f"s{post['seq']}-codex-browser"
    assert first.exists() is linked, "removed under a real folder, left under a symlinked one"
    nenv.node.tick()                    # the same assignment, again
    [call] = nenv.node_spawner.calls
    [r] = [nodes.run(nenv.board, x["run_id"]) for x in runs(nenv)]
    assert r["state"] == "running"
    used = str(first) + "-2" if linked else str(first)
    assert used in " ".join(call["argv"]) and os.path.isdir(used)
    nenv.node_spawner.children[0].code = 0
    nenv.node.tick()
    assert os.path.isdir(used) is linked, "the run's own directory goes when it ends (under a real folder)"


@pytest.mark.parametrize("linked", [False, True], ids=["real-parent", "symlinked-parent"])
def test_a_hub_launch_never_collides_with_a_leftover_of_its_name(tmp_path, home, monkeypatch, linked):
    """The hub gives every attempt a run id of its own, and every output directory a name not taken: a leftover (one
    a cleanup left under a symlinked log folder) never fails a later launch."""
    from test_access_routing import signed_in_request
    nenv = _signed_in_hub(tmp_path, home, monkeypatch, overlap=False)
    target = tmp_path / "real-dispatch-logs"
    target.mkdir()
    if linked:
        link = tmp_path / "dispatch-logs-link"
        link.symlink_to(target)
        nenv.d.log_dir = link
    else:
        nenv.d.log_dir = target
    post = signed_in_request(nenv, "codex")
    left = nenv.d.log_dir / f"s{post['seq']}-codex-browser"
    left.mkdir()
    nenv.d.tick()
    [r] = runs(nenv)
    assert r["status"] == "running" and r["signed_in_browser"]["output_dir"] == str(left) + "-2"
    assert left.is_dir(), "the leftover is not the launch's to remove"


def test_startup_recovery_removes_only_a_verified_runs_own_recorded_output(tmp_path, home, monkeypatch):
    """recover() cleans what main cleaned: the output of runs it verifies from the node's own state, at the exact
    directory such a run recorded. A directory no state names (another tool's `research-browser`, or a run's that
    was spawned just before a crash and never saved, possibly still in use) is left in the log folder."""
    nenv = _signed_in_node(tmp_path, home, monkeypatch)
    log = nenv.node.log_dir
    log.mkdir(parents=True, exist_ok=True)
    for name in ("s1-codex-browser-2", "s1-codex-browser", "research-browser", "s5-codex-browser"):
        (log / name).mkdir()
    (log / "node-runs.json").write_text(json.dumps({"s1-codex": {"pid": None, "proc_start": None, "cwd": None,
                                                                  "browser_out": str(log / "s1-codex-browser-2")}}))
    nenv.node.recover()
    left = sorted(p.name for p in log.iterdir() if p.is_dir())
    assert "s1-codex-browser-2" not in left, "the verified run's own recorded directory is removed"
    assert {"research-browser", "s5-codex-browser", "s1-codex-browser"} <= set(left), \
        "nothing no state names: an unrelated *-browser, an unsaved run's, another attempt's"


def test_an_unrecorded_leftover_is_left_and_a_retry_still_starts(tmp_path, home, monkeypatch):
    from test_access_routing import signed_in_request
    nenv = _signed_in_node(tmp_path, home, monkeypatch)
    nenv.node.recover()
    post = signed_in_request(nenv, "codex")
    nenv.d.tick()
    left = nenv.node.log_dir / f"s{post['seq']}-codex-browser"     # made before a crash, in no state file
    left.mkdir(parents=True)
    nenv.node.recover()
    assert left.is_dir()
    nenv.node.tick()
    [call] = nenv.node_spawner.calls
    assert str(left) + "-2" in " ".join(call["argv"]) and left.is_dir()


def test_a_running_runs_output_directory_is_in_the_state_file(tmp_path, home, monkeypatch):
    from test_access_routing import signed_in_request
    nenv = _signed_in_node(tmp_path, home, monkeypatch)
    post = signed_in_request(nenv, "codex")
    nenv.d.tick()
    nenv.node.tick()
    state = json.loads((nenv.node.log_dir / "node-runs.json").read_text())
    assert state[f"s{post['seq']}-codex"]["browser_out"] == str(nenv.node.log_dir / f"s{post['seq']}-codex-browser")


def test_a_headless_only_output_directory_in_an_account_folder_is_refused_before_it_is_made(tmp_path, home,
                                                                                            monkeypatch):
    """Playwright gets the headless browser's output directory as its cwd and --output-dir: not a writable root of
    the run, so only the one helper that makes such a directory holds it against the account folders."""
    from test_dispatch import human_post
    codex_home = home / ".codex-work"
    (codex_home / "logs").mkdir()
    # The node.
    link = tmp_path / "node-logs-link"
    link.symlink_to(codex_home / "logs")
    nenv = _signed_in_node(tmp_path, home, monkeypatch, link, headless_only=True)
    from test_nodes import _bound
    monkeypatch.setattr(dispatch, "git_run_directory_blocker", lambda cwd, env: None)
    _bound(nenv)
    nenv.d.tick()
    made = _record_browser_mkdirs(monkeypatch)
    nenv.node.tick()
    assert nenv.node_spawner.calls == [] and made == []
    ended = [nodes.run(nenv.board, x["run_id"]) for x in runs(nenv)]
    assert any("the browser output directory" in (r.get("error") or "") for r in ended), ended
    assert human_post


def test_a_headless_only_output_directory_in_an_account_folder_is_refused_on_the_hub(tmp_path, home, monkeypatch):
    from test_nodes import _bound
    from test_nodes import nenv as nenv_fixture
    from test_dispatch import allow
    nenv = nenv_fixture.__wrapped__(tmp_path, monkeypatch)
    codex_home = home / ".codex-work"
    (codex_home / "logs").mkdir()
    hub_dir = tmp_path / "hub-checkout"
    hub_dir.mkdir()
    nenv.d.config = DispatchConfig.from_dict({
        "runners": {"codex": ["codex", "exec", "{prompt}"]}, "runner_homes": {"codex": str(codex_home)},
        "worktrees": {PROJECT: str(hub_dir)},
        "headless_browser": {"command": "npx", "args": ["--offline", "-y", "@playwright/mcp@0.0.83"], "runners": ["codex"]}})
    monkeypatch.setattr(dispatch, "codex_approval_reminder", lambda template: None)
    monkeypatch.setattr(dispatch, "codex_config_conflict", lambda env: None)
    link = tmp_path / "dispatch-logs-link"
    link.symlink_to(codex_home / "logs")
    nenv.d.log_dir = link
    allow(nenv, agents=["codex"])
    _bound(nenv)
    made = _record_browser_mkdirs(monkeypatch)
    nenv.d.tick()
    assert nenv.spawner.calls == [] and made == []
    failed = [r for r in runs(nenv) if r["status"] == "spawn_failed"]
    assert failed and "the browser output directory" in failed[0]["error"], [(r["status"], r.get("error")) for r in runs(nenv)]


def test_the_output_cleanup_never_follows_a_symlinked_parent(tmp_path, caplog):
    real = tmp_path / "elsewhere"
    (real / "r1-browser").mkdir(parents=True)
    (real / "r1-browser" / "keep").write_text("x")
    link = tmp_path / "logs"
    link.symlink_to(real)
    for _ in range(2):
        dispatch.remove_browser_output({"run_id": "r1", "log": str(link / "r1.log"),
                                        "headless_browser": {"output_dir": str(link / "r1-browser")}})
    assert (real / "r1-browser" / "keep").exists(), "not removed through the symlinked folder"
    assert [r.getMessage() for r in caplog.records].count(
        f"left the browser output {link / 'r1-browser'} where it is: its folder is reached through a symlink") == 1, \
        "said once in the log"
    plain = tmp_path / "plain"
    (plain / "r2-browser").mkdir(parents=True)
    dispatch.remove_browser_output({"run_id": "r2", "log": str(plain / "r2.log"),
                                    "signed_in_browser": {"output_dir": str(plain / "r2-browser")}})
    assert not (plain / "r2-browser").exists(), "its own, in a real folder: removed"


def test_the_hosted_check_reads_the_argv_the_provider_will_execute(tmp_path, home, monkeypatch):
    """Checked and executed arguments cannot differ: the check reads the provider's final argv, right before spawn."""
    from test_session_host import FakeSessionChild
    w = home / ".claude-work"
    (w / "x").mkdir()
    work = tmp_path / "checkout"
    work.mkdir()
    c = DispatchConfig.from_dict({"runners": {"claude-code": SHIPPED, WORK: SHIPPED}, "runner_homes": {WORK: str(w)},
                                  "worktrees": {PROJECT: str(work)}})
    spawner = FakeSpawner(child=FakeSessionChild)
    host = session_host.SessionHost(c, tmp_path / "logs", **fake_os_host(spawner))
    sid = "5b0c1f2e-7a6d-4e3b-9c8a-1d2e3f4a5b6c"
    host.open(sid, agent="claude", runtime="claude-code", project=PROJECT)
    real = host._argv
    monkeypatch.setattr(host, "_argv", lambda s: real(s) + [f"--add-dir={w / 'x'}"])   # what the provider adds last
    host.send(sid, "hello")
    out = host.tick()
    assert spawner.calls == [] and any(e.kind == "ended" and "the extra writable folder" in e.detail for e in out)


def test_a_settings_file_saved_between_the_read_and_the_board_is_seen_by_the_first_refresh(tmp_path, monkeypatch):
    """The loader records the files' signature from BEFORE it read them, and the Board starts from that: a save in
    between (here forced exactly there) is newer, so the ordinary refresh takes it."""
    from agent_comms.config import LOCAL_SETTINGS, Settings
    from agent_comms.core import Board

    toml = tmp_path / "board.toml"
    toml.write_text(f'[server]\ndb_path = "{tmp_path / "board.db"}"\nagents_path = "{tmp_path / "agents.toml"}"\n')
    local = toml.with_name(LOCAL_SETTINGS)
    local.write_text(f'[dispatch.agent_projects]\n"{WORK}" = ["{PROJECT}"]\n')
    real = Settings.read_layers.__func__

    def read_then_save(cls, path=None, local_=True):
        data = real(cls, path, local_)
        local.write_text(f'[dispatch.agent_projects]\n"{WORK}" = ["{P2}", "/also/this"]\n')     # saved meanwhile
        return data
    monkeypatch.setattr(Settings, "read_layers", classmethod(read_then_save))
    s = Settings.load(toml)
    monkeypatch.undo()
    board = Board(s)
    assert agent_accounts.hub_refusal(board, WORK, PROJECT) is None, "(the settings as read: the old table)"
    assert board.reload_settings() is True, "the first ordinary refresh sees the save"
    assert agent_accounts.hub_refusal(board, WORK, PROJECT).startswith(NOT_IN), "and the new table is in force"
    assert board.reload_settings() is False, "and nothing more after that"


def test_a_runs_own_worktree_in_an_account_folder_is_refused_before_it_is_made(denv, home, monkeypatch):
    inside = str(home / ".claude-work" / "wt")
    monkeypatch.setattr(denv.d, "_run_dir_for", lambda *a, **k: inside)
    monkeypatch.setattr(dispatch, "ensure_run_worktree", lambda *a: pytest.fail("the worktree was made"))
    human_post(denv, ["grok"])
    denv.d.tick()
    [r] = runs(denv)
    assert r["status"] == "spawn_failed" and "is, is inside or contains an account folder" in r["error"]


def test_the_codex_workspace_parser_stops_at_the_end_of_options():
    assert agent_accounts.codex_workspaces(["codex", "exec", "--cd", "/a", "--", "-C", "/b"]) == ["/a"]


def test_the_codex_workspace_parser_reads_every_spelling_the_adapter_reads():
    argv = ["codex", "exec", "--cd", "/a", "--cd=/b", "-C", "/c", "-C/d", "-C=/e", "-c", "x=1", "--model", "m", "rel"]
    assert agent_accounts.codex_workspaces(argv) == ["/a", "/b", "/c", "/d", "/e"]
    assert agent_accounts.codex_workspaces(["codex", "exec", "{prompt}"]) == []


def test_the_session_host_refuses_a_runner_whose_variable_is_also_passed_through(tmp_path, home):
    w = str(home / ".claude-work")
    work = tmp_path / "checkout"
    work.mkdir()
    c = DispatchConfig.from_dict({"runners": {WORK: SHIPPED}, "runner_homes": {WORK: w},
                                  "env": {"claude-code": ["CLAUDE_CONFIG_DIR"]}, "worktrees": {PROJECT: str(work)}})
    host = session_host.SessionHost(c, tmp_path / "logs", **fake_os_host(FakeSpawner()))
    with pytest.raises(LookupError, match="set it one way only"):
        host.open("5b0c1f2e-7a6d-4e3b-9c8a-1d2e3f4a5b6c", agent=WORK, runtime="claude-code", project=PROJECT)


def test_a_scoped_session_keeps_its_scoped_command_line_and_gets_the_folder(tmp_path, home):
    from test_session_host import FakeSessionChild
    w = str(home / ".claude-work")
    work = tmp_path / "checkout"
    work.mkdir()
    out = {}
    for homes in ({}, {WORK: w}):
        c = DispatchConfig.from_dict({"runners": {WORK: SHIPPED}, "runner_homes": homes,
                                      "worktrees": {PROJECT: str(work)}, "claude_tool_projects": [PROJECT]})
        spawner = FakeSpawner(child=FakeSessionChild)
        host = session_host.SessionHost(c, tmp_path / f"logs{len(homes)}", **fake_os_host(spawner))
        sid = "5b0c1f2e-7a6d-4e3b-9c8a-1d2e3f4a5b6c"
        host.open(sid, agent=WORK, runtime="claude-code", project=PROJECT)
        host.send(sid, "hello")
        host.tick()
        [call] = spawner.calls
        out[bool(homes)] = call
    assert out[True]["argv"] == out[False]["argv"] and "--allowedTools" in " ".join(out[True]["argv"])
    assert out[True]["env"] == out[False]["env"] | {"CLAUDE_CONFIG_DIR": w}


def test_a_hosted_codex_session_reads_its_board_server_from_the_accounts_own_config(tmp_path, home):
    """A bound Codex session's board server gets its binding variables from the config.toml of the Codex home in
    force (session_host.bound_template, codex_config_of): with a folder, the account's own."""
    from test_session_host import FakeSessionChild
    codex_home = home / ".codex-work"
    (codex_home / "config.toml").write_text('[mcp_servers.agent-comms]\ncommand = "bash"\n')
    work = tmp_path / "checkout"
    work.mkdir()
    got = {}
    for homes in ({}, {"codex-cli": str(codex_home)}):
        c = DispatchConfig.from_dict({"runners": {"codex-cli": CODEX_RUNNER}, "runner_homes": homes,
                                      "worktrees": {PROJECT: str(work)}, "sessions": {"enabled": True, "codex": True}})
        spawner = FakeSpawner(child=FakeSessionChild)
        host = session_host.SessionHost(c, tmp_path / f"logs{len(homes)}", **fake_os_host(spawner))
        sid = "5b0c1f2e-7a6d-4e3b-9c8a-1d2e3f4a5b6c"
        host.open(sid, agent="codex", runtime="codex-cli", project=PROJECT, bind="a" * 64)
        host.send(sid, "hello")
        host.tick()
        [call] = spawner.calls
        got[bool(homes)] = call
    assert got[True]["env"]["CODEX_HOME"] == str(codex_home) and "CODEX_HOME" not in got[False]["env"]
    env_vars = [x for x in got[True]["argv"] if x.startswith("mcp_servers.agent-comms.env_vars=")]
    assert env_vars and "AGENT_COMMS_HOSTED_KEY" in env_vars[0]
    assert not [x for x in got[False]["argv"] if x.startswith("mcp_servers.agent-comms.env_vars=")], \
        "without the folder: ~/.codex here has no board server, so nothing is added"
    first = json.loads(spawner.children[0].stdin.split(b"\n")[0])
    assert first["method"] == "initialize", "the app-server handshake is the same"


# ------------------------------------------------------------ the installers


def stub(directory: Path, name: str, body: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    f = directory / name
    f.write_text("#!/bin/bash\n" + body)
    f.chmod(0o755)


def run_install(tmp_path, script, *args, **extra):
    bin_dir = tmp_path / "bin"
    log = tmp_path / "calls.log"
    stub(bin_dir, "uv", "exit 0\n")
    stub(bin_dir, "claude", f'echo "claude $* CLAUDE_CONFIG_DIR=${{CLAUDE_CONFIG_DIR:-}}" >> {log}\n'
                            '[[ "$1 $2" == "mcp get" ]] && exit 1\nexit 0\n')
    stub(bin_dir, "codex", f'echo "codex $* CODEX_HOME=${{CODEX_HOME:-}}" >> {log}\nexit 0\n')
    env = {"HOME": str(tmp_path / "home"), "PATH": f"{bin_dir}:/usr/bin:/bin", **extra}
    r = subprocess.run(["bash", str(ROOT / "integrations" / script), *args], env=env, capture_output=True, text=True,
                       timeout=60)
    return r, (log.read_text() if log.exists() else "")


@pytest.mark.parametrize("with_dir", [True, False])
def test_the_claude_installer_puts_the_skill_in_the_config_folder_in_force(tmp_path, with_dir):
    h = tmp_path / "home"
    (h / ".config" / "agent-comms").mkdir(parents=True)
    (h / ".config" / "agent-comms" / "claude-work.token").write_text("t\n")
    work = tmp_path / "claude-work-dir"
    extra = {"CLAUDE_CONFIG_DIR": str(work)} if with_dir else {}
    r, calls = run_install(tmp_path, "claude-code/install.sh", "--agent", WORK, "--home", str(tmp_path), **extra)
    assert r.returncode == 0, r.stderr
    where = work if with_dir else h / ".claude"
    assert (where / "skills" / "agent-comms" / "SKILL.md").read_text() == \
        (ROOT / "integrations" / "claude-code" / "skill" / "SKILL.md").read_text()
    if with_dir:
        assert not (h / ".claude").exists(), "nothing goes to ~/.claude when CLAUDE_CONFIG_DIR is set"
    assert f"Add these hooks to {where}/settings.json" in r.stdout
    assert f"claude mcp add agent-comms --scope user" in calls and \
        f"CLAUDE_CONFIG_DIR={work if with_dir else ''}\n" in calls, "the MCP server goes to the same folder"


def test_the_codex_installer_follows_codex_home_and_keeps_the_skill_where_codex_reads_it(tmp_path):
    codex_home = tmp_path / "codex-work"
    codex_home.mkdir()
    r, calls = run_install(tmp_path, "codex/install.sh", "--preapprove-board-tools", CODEX_HOME=str(codex_home),
                           AGENT_COMMS_HOME=str(tmp_path))
    assert r.returncode == 0, r.stderr
    assert (tmp_path / "home" / ".agents" / "skills" / "agent-comms" / "SKILL.md").exists(), \
        "~/.agents/skills: Codex reads it whatever its home is"
    assert f"codex mcp add agent-comms" in calls and f"CODEX_HOME={codex_home}" in calls
    assert f"add this to\n{codex_home}/config.toml yourself" in r.stdout
    assert "~/.codex" not in r.stdout
