"""Parent and child hosted sessions (hosted_tree.py, session_access.py, session_worktrees.py): the gate, the rules
for making a child, access words and what each becomes for Claude and for Codex, isolation, the cascade, roots-only
wakes, and `board session`'s tree. Fakes as in test_hosted_sessions; git is real where a worktree is made. Never a
real agent CLI."""

import json
import os
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent_comms import cli, delivery, hosted, hosted_tree, hosted_wake, nodes, runner_preflight as rp
from agent_comms import session_access as access, session_worktrees
from agent_comms.core import Conflict, Forbidden
from agent_comms.dispatch import DispatchConfig
from agent_comms.node_sessions import WORKTREES_FILE
from agent_comms.providers.claude import ClaudeSession
from agent_comms.providers.codex import BYPASS_REFUSED, CodexSession
from agent_comms.session_host import SessionHost

from conftest import PROJECT
import test_providers_codex as cx
from test_hosted_sessions import (CODEX_RUNNER, P2, P3, UNKNOWN, children, client, clienv, codex_handshake,  # noqa: F401
                                  codex_turn, make_node, msg, node_config, send, session_env, show, start,
                                  tick, written)
from test_hosted_wake import GRACE, ask, later, record, wakes
from test_session_host import SHIPPED, init, result, value_after

ROOT = Path(__file__).resolve().parents[1]
TREE = {"nodes": {"laptop": {"projects": [PROJECT, P2, P3], "agents": ["claude", "codex", "grok"]}},
        "sessions": {"spawn": {"enabled": True}}}
BYPASS = ["claude", "-p", "{prompt}", "--dangerously-skip-permissions"]
TRIAGE = ["claude", "-p", "{prompt}", "--model", "haiku", "--strict-mcp-config", "--mcp-config", "/x/haiku.mcp.json",
          "--permission-mode", "dontAsk",
          "--allowedTools=mcp__agent-comms,Read(./**),Grep(./**),Glob(./**),Bash(git log *),Bash(git show *)",
          "--disallowedTools=Edit,Write,NotebookEdit,Bash(git *--output*),Read(~/.config/agent-comms/**),"
          "Read(~/.ssh/**),Read(./data/**)"]


def tree_config(env, sessions=None, runners=None, **over) -> DispatchConfig:
    return node_config(env, runners={"claude-code": SHIPPED, "codex-cli": CODEX_RUNNER} | (runners or {}),
                       sessions={"enabled": True, "codex": True} | (sessions or {}), **over)


@pytest.fixture
def senv(tmp_path):
    """A hub with child sessions on, and its node hosting Claude and Codex. (Named as test_hosted_sessions' fixture,
    so that its `clienv` drives the CLI against this hub.)"""
    return session_env(tmp_path, dispatch=TREE, config=tree_config)


def root(env, node=None, **kw) -> str:
    """A root session its node has opened (one pass), so the hub knows its access."""
    sid = start(env, **kw)["session_id"]
    tick(env, node)
    return sid


def child(env, parent, text="go", expect=200, **kw) -> dict:
    r = env.human.post(f"/api/hosted-sessions/{parent}/children", json={"message": text, **kw})
    assert r.status_code == expect, r.text
    return r.json()["session"] if expect == 200 else r.json()


def stored(env, sid) -> dict:
    return hosted._session(env.board.conn, sid)


def call_for(env, sid, node=None) -> dict:
    """The spawn of the Claude process of this session."""
    calls = (node or env.node).session_spawner.calls
    return next(c for c in reversed(calls) if sid in c["argv"])


def answer(env, sid, text="ok", node=None) -> None:
    """The session's Claude process answers the turn it was given; the node reports it. (Like run_turn, which
    cannot tell a Codex process's argv from a Claude one's.)"""
    node = node or env.node
    i = max(i for i, c in enumerate(node.session_spawner.calls) if sid in c["argv"])
    node.session_spawner.children[i].emit(init(sid), result(sid, text=text))
    tick(env, node)


def codex_proc(env, node=None):
    """The (one) Codex process the node started."""
    node = node or env.node
    [i] = [i for i, c in enumerate(node.session_spawner.calls) if c["argv"][:2] == ["codex", "app-server"]]
    return node.session_spawner.children[i]


def codex_call(env, node=None) -> dict:
    """The spawn of the (one) Codex process the node started."""
    node = node or env.node
    [call] = [c for c in node.session_spawner.calls if c["argv"][:2] == ["codex", "app-server"]]
    return call


def git(cwd, *args) -> str:
    r = subprocess.run(["git", "-C", str(cwd), "-c", "user.email=t@example.com", "-c", "user.name=t",
                        "-c", "commit.gpgsign=false", *args], check=True, capture_output=True, text=True)
    return r.stdout.strip()


class Spy:
    """The node's client, remembering what it reported."""

    def __init__(self, inner):
        self.inner, self.posts = inner, []

    def request(self, method, path, **kw):
        if method == "POST":
            self.posts.append((path, kw.get("json")))
        return self.inner.request(method, path, **kw)


# ------------------------------------------------------------ settings and the gate


def test_the_spawn_settings_are_parsed_strictly_and_ship_off():
    c = DispatchConfig.from_dict({})
    assert (c.sessions.spawn.enabled, c.sessions.spawn.max_children, c.sessions.allow_full_access) == (False, 5, False)
    c = DispatchConfig.from_dict({"sessions": {"allow_full_access": True, "spawn": {"enabled": True, "max_children": 2}}})
    assert (c.sessions.spawn.enabled, c.sessions.spawn.max_children, c.sessions.allow_full_access) == (True, 2, True)
    for bad, why in (({"spawn": {"enabled": "yes"}}, "enabled must be true or false"),
                     ({"spawn": {"max_children": 0}}, "max_children"), ({"spawn": {"max_children": True}}, "max_children"),
                     ({"spawn": {"max_children": 51}}, "max_children"), ({"spawn": "on"}, "must be a table"),
                     ({"spawn": {"max_kids": 3}}, r"unknown setting \[dispatch.sessions.spawn\]"),
                     ({"allow_full_access": 1}, "allow_full_access must be true or false")):
        with pytest.raises(ValueError, match=why):
            DispatchConfig.from_dict({"sessions": bad})
    shipped = DispatchConfig.load(ROOT / "board.toml", local=False)
    assert (shipped.sessions.spawn.enabled, shipped.sessions.spawn.max_children) == (False, 5)
    assert shipped.sessions.allow_full_access is False and shipped.sessions.max_live == 4


def test_with_spawn_off_no_child_can_be_started_and_a_session_is_what_it_was(tmp_path):
    env = session_env(tmp_path, config=tree_config)        # the hub says nothing about spawn
    spy = env.node.sessions.client = Spy(env.node.sessions.client)
    sid = start(env)["session_id"]
    [listed] = env.laptop.get("/api/nodes/sessions").json()["sessions"]
    assert set(listed) == {"session_id", "agent", "runtime", "project", "model", "stop", "resume", "messages",
                           "status_seq"}, "the node is told what it was always told"
    tick(env)
    out = child(env, sid, expect=409)
    assert out["message"] == hosted_tree.OFF and "spawn" in out["message"]
    with pytest.raises(Conflict, match="child sessions are off"):
        hosted_tree.create_child(env.board, "human", sid, "go", allowed=TREE["nodes"])
    assert not {"parent", "root", "depth", "access", "child_counts"} & set(stored(env, sid))
    view = show(env, sid)
    assert not {"parent", "access", "children", "depth"} & set(view)
    assert all(set(body) <= {"events", "seq", "state", "pid", "resume"} for _, body in spy.posts), \
        "and reports what it always reported"
    assert [k for (k,) in env.board.conn.execute("SELECT key FROM board_state WHERE key LIKE 'host.session.%'")] == [
        hosted.SESSION_PREFIX + sid]
    # A [dispatch.sessions] table that does not validate leaves spawn off.
    env.settings.dispatch = {**TREE, "sessions": {"spawn": {"enabled": True}, "max_lives": 3}}
    assert hosted_tree.spawn_config(env.board).enabled is False


def test_a_root_started_with_spawn_on_reports_its_access_and_older_roots_catch_up(tmp_path):
    env = session_env(tmp_path, config=tree_config)
    old = root(env)                                     # started and opened while spawn was off
    assert "access" not in stored(env, old)
    env.settings.dispatch = TREE                        # the human turns child sessions on
    [listed] = env.laptop.get("/api/nodes/sessions").json()["sessions"]
    assert listed["access"] is None and "parent" not in listed, "a root is asked for its access, with no word of its own"
    assert child(env, old, expect=409)["message"].startswith(f"the access level of session {old} is not known yet")
    tick(env)
    assert stored(env, old)["access"] == "workspace" and show(env, old)["access"] == "workspace"
    assert child(env, old)["access"] == "workspace"
    new = start(env, project=P2)
    assert (new["parent"], new["root"], new["depth"], new["access"]) == (None, new["session_id"], 0, None)


# ------------------------------------------------------------ making a child


def test_a_child_runs_on_its_parents_node_in_its_project_and_has_a_name(senv):
    sid = root(senv)
    c1 = child(senv, sid, "look at this", agent="codex", role="review", title="Review the diff", model="gpt-6-luna",
               effort="low", isolation="shared", access="read-only", request_id="r-1")
    assert (c1["parent"], c1["root"], c1["depth"], c1["name"]) == (sid, sid, 1, "review-1")
    assert (c1["agent"], c1["runtime"], c1["node"], c1["project"]) == ("codex", "codex-cli", "laptop", PROJECT)
    assert (c1["title"], c1["role"], c1["model"], c1["effort"], c1["isolation"], c1["access"], c1["request_id"]) == (
        "Review the diff", "review", "gpt-6-luna", "low", "shared", "read-only", "r-1")
    assert (c1["state"], c1["queued"]) == ("idle", 1) and msg(senv, c1["session_id"], 1)["kind"] == "user"
    c2 = child(senv, sid)       # the defaults: the parent's agent and access, the shared checkout, `general`
    assert (c2["agent"], c2["access"], c2["isolation"], c2["role"], c2["name"], c2["title"], c2["effort"]) == (
        "claude", "workspace", "shared", "general", "general-1", None, None)
    assert child(senv, sid, role="review")["name"] == "review-2"
    listed = {s["session_id"]: s for s in senv.laptop.get("/api/nodes/sessions").json()["sessions"]}
    assert {k: listed[c1["session_id"]][k] for k in ("parent", "access", "isolation", "effort", "agent", "model")} == {
        "parent": sid, "access": "read-only", "isolation": "shared", "effort": "low", "agent": "codex",
        "model": "gpt-6-luna"}
    assert not {"title", "name", "role", "request_id"} & set(listed[c1["session_id"]]), "words the node needs, no more"
    assert [k["name"] for k in show(senv, sid)["children"]] == ["review-1", "general-1", "review-2"]
    assert "reply" not in show(senv, sid)["children"][0]


def test_a_name_is_never_reused_under_a_parent(senv):
    sid = root(senv)
    first = child(senv, sid, role="test")
    assert senv.human.post(f"/api/hosted-sessions/{first['session_id']}/close").status_code == 200
    tick(senv, times=2)
    assert show(senv, first["session_id"])["state"] == "ended"
    assert child(senv, sid, role="test")["name"] == "test-2"


def test_the_parent_must_exist_be_open_and_be_a_root(senv):
    assert child(senv, UNKNOWN, expect=404)["message"] == "no such hosted session"
    assert senv.human.post("/api/hosted-sessions/not-a-uuid/children", json={"message": "x"}).status_code == 404
    sid = root(senv)
    kid = child(senv, sid)
    out = child(senv, kid["session_id"], expect=409)
    assert "a child cannot have children" in out["message"] and sid in out["message"], "depth 1"
    assert senv.human.post(f"/api/hosted-sessions/{sid}/close").status_code == 200
    assert "is being closed" in child(senv, sid, expect=409)["message"]
    tick(senv, times=2)
    assert show(senv, sid)["state"] == "ended"
    assert "has ended" in child(senv, sid, expect=409)["message"]


def test_the_childs_agent_must_be_one_the_parents_node_may_and_can_host(senv, tmp_path):
    sid = root(senv)
    assert child(senv, sid, agent="nobody", expect=404)["message"] == "no agent named 'nobody'"
    out = child(senv, sid, agent="grok", expect=409)      # granted on the hub, but the node has no runner for it
    assert "not connected as a session host with a runner for grok" in out["message"]
    senv.settings.dispatch = {**TREE, "nodes": {"laptop": {"projects": [PROJECT], "agents": ["claude"]}}}
    out = child(senv, sid, agent="codex", expect=409)     # the node could, the hub's grant does not list the agent
    assert "node laptop may not run codex" in out["message"] and "[dispatch.nodes.laptop]" in out["message"]
    senv.settings.dispatch = {**TREE, "nodes": {"desktop": TREE["nodes"]["laptop"]}}
    assert "node laptop may not run claude" in child(senv, sid, expect=409)["message"], "only the parent's node counts"
    senv.settings.dispatch = TREE
    senv.clock.advance(nodes.NODE_STALE_SECONDS + 1)
    assert "is not connected as a session host" in child(senv, sid, expect=409)["message"]
    senv.node.hello()
    assert child(senv, sid, agent="codex")["node"] == "laptop"


@pytest.mark.parametrize("field,value,why", [
    ("role", "boss", "role must be one of"), ("isolation", "container", "isolation must be one of"),
    ("access", "root", "access must be one of read-only, workspace, full"),
    ("effort", "ludicrous", "effort must be one of low, medium, high, xhigh, max"),
    ("effort", "--dangerously-skip-permissions", "effort must be one of"),
    ("model", "--model x", "plain model name"), ("title", "two\nlines", "title must be one line"),
    ("title", "x" * 121, "title must be one line"), ("request_id", "has space", "request_id must be"),
    ("request_id", "x" * 65, "request_id must be"), ("message", "   ", "message text is empty")])
def test_what_a_child_is_asked_with_is_checked_at_the_hub(senv, field, value, why):
    sid = root(senv)
    assert why in child(senv, sid, expect=400, **{field: value})["message"]
    assert "children" not in show(senv, sid), "nothing was made"
    r = senv.human.post(f"/api/hosted-sessions/{sid}/children", json={"message": "x", "command": ["rm", "-rf"]})
    assert r.status_code == 422, "nothing but the words is taken"


def test_max_children_counts_the_children_of_a_root_that_have_not_ended(senv):
    senv.settings.dispatch = {**TREE, "sessions": {"spawn": {"enabled": True, "max_children": 2}}}
    sid = root(senv)
    a, b = child(senv, sid), child(senv, sid, agent="codex")
    out = child(senv, sid, expect=409)
    assert "already has 2 child sessions" in out["message"] and "max_children is 2" in out["message"]
    assert senv.human.post(f"/api/hosted-sessions/{a['session_id']}/close").status_code == 200
    assert "already has 2" in child(senv, sid, expect=409)["message"], "one being closed has not ended yet"
    tick(senv, times=2)
    assert child(senv, sid)["name"] == "general-3"
    other = root(senv, project=P2)
    assert child(senv, other)["parent"] == other, "the limit is per root"
    assert b["state"] == "idle"


def test_the_same_request_id_under_the_same_parent_returns_the_child_it_made(senv):
    sid = root(senv)
    first = senv.human.post(f"/api/hosted-sessions/{sid}/children", json={"message": "go", "request_id": "job-7"}).json()
    again = senv.human.post(f"/api/hosted-sessions/{sid}/children",
                            json={"message": "go again", "request_id": "job-7", "agent": "codex"}).json()
    assert first["created"] is True and again["created"] is False
    assert again["session"]["session_id"] == first["session"]["session_id"] and again["session"]["agent"] == "claude"
    assert again["message"]["n"] == 1 and len(show(senv, first["session"]["session_id"])["messages"]) == 1
    assert len(show(senv, sid)["children"]) == 1
    assert child(senv, sid, request_id="job-8")["session_id"] != first["session"]["session_id"]
    other = root(senv, project=P2)
    assert child(senv, other, request_id="job-7")["parent"] == other, "another parent, another child"
    # Still the same answer after it ended, and once the limit is reached.
    senv.settings.dispatch = {**TREE, "sessions": {"spawn": {"enabled": True, "max_children": 1}}}
    assert senv.human.post(f"/api/hosted-sessions/{first['session']['session_id']}/close").status_code == 200
    tick(senv, times=2)
    again = senv.human.post(f"/api/hosted-sessions/{sid}/children", json={"message": "x", "request_id": "job-7"}).json()
    assert (again["created"], again["session"]["state"]) == (False, "ended")


def test_one_session_per_agent_and_project_is_for_roots_only(senv):
    sid = root(senv)
    a, b = child(senv, sid), child(senv, sid)
    assert a["agent"] == b["agent"] == "claude" and a["project"] == b["project"] == PROJECT
    r = senv.human.post("/api/hosted-sessions", json={"project": PROJECT, "message": "x"})
    assert r.status_code == 409 and sid in r.text, "a second ROOT of the agent in the project is still refused"
    kid = child(senv, sid, agent="codex")
    codex_root = start(senv, agent="codex")         # a child of that agent in the project does not stand in its way
    assert codex_root["session_id"] != kid["session_id"] and codex_root["parent"] is None


# ------------------------------------------------------------ access: the hub orders the words


def test_a_childs_access_defaults_to_its_parents_and_is_never_above_it(senv):
    sid = root(senv)
    assert stored(senv, sid)["access"] == "workspace"
    assert child(senv, sid)["access"] == "workspace"
    assert child(senv, sid, access="read-only")["access"] == "read-only"
    out = child(senv, sid, access="full", expect=409)
    assert out["message"] == ("access full is above the parent's (workspace): a child session never gets more access "
                              "than its parent")
    assert access.LEVELS == ("read-only", "workspace", "full")
    assert [access.within(a, "workspace") for a in access.LEVELS] == [True, True, False]


def test_the_hub_never_takes_a_childs_reported_access_above_what_was_asked(senv):
    sid = root(senv)
    kid = child(senv, sid, access="read-only")["session_id"]
    r = senv.laptop.post(f"/api/nodes/sessions/{kid}", json={"events": [], "state": "idle", "access": "full"})
    assert r.status_code == 200 and "access" not in r.json()
    assert (stored(senv, kid)["access"], stored(senv, kid).get("granted")) == ("read-only", None)
    assert senv.laptop.post(f"/api/nodes/sessions/{kid}", json={"events": [], "access": "root"}).status_code == 400
    assert senv.laptop.post(f"/api/nodes/sessions/{kid}", json={"events": [], "worktree": "relative"}).status_code == 400


# ------------------------------------------------------------ access: the node maps the words (Claude)


def host_for(env, name, **over) -> SessionHost:
    return SessionHost(tree_config(env, **over), env.tmp / f"host-{name}")


def claude_argv(env, name, runner=SHIPPED, access_word=None, **over) -> tuple[list[str], str]:
    host = host_for(env, name, runners={"claude-code": runner}, **over)
    s = host.open(UNKNOWN, agent="claude", runtime="claude-code", project=PROJECT, access=access_word)
    return host._argv(s), s.access


def test_claude_workspace_is_the_runner_as_configured(senv):
    plain, granted = claude_argv(senv, "none")
    asked, granted2 = claude_argv(senv, "workspace", access_word="workspace")
    assert plain == asked and (granted, granted2) == ("workspace", "workspace")
    assert plain == ["claude", "-p", "--permission-mode", "dontAsk", "--allowedTools=mcp__agent-comms",
                     "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
                     "--permission-prompts", "none", "--session-id", UNKNOWN]
    scoped, _ = claude_argv(senv, "scoped", access_word="workspace", claude_tool_projects=[PROJECT])
    assert "--allowedTools=" + ",".join(rp.ALLOWED) in scoped, "with the opt-in scoping a session already gets"
    # `workspace` asked of a runner that is read-only as configured: as configured, and reported as what it is.
    triage, granted = claude_argv(senv, "triage", runner=TRIAGE, access_word="workspace")
    assert granted == "read-only" and TRIAGE[-2] in triage and TRIAGE[-1] in triage


def test_claude_read_only_is_the_triage_runners_narrowing_for_any_runner(senv):
    runner = ["claude", "-p", "{prompt}", "--model", "sonnet", "--permission-mode", "acceptEdits",
              "--allowedTools", "mcp__agent-comms__board_post", "Edit", "Bash(npm test *)",
              "--disallowedTools=Bash(curl *),WebFetch", "--append-system-prompt", "Be brief."]
    argv, granted = claude_argv(senv, "ro", runner=runner, access_word="read-only")
    assert granted == "read-only"
    assert value_after(argv, "--permission-mode") == "dontAsk" and argv.count("--permission-mode") == 1
    allowed = next(a for a in argv if a.startswith("--allowedTools="))
    assert allowed == ("--allowedTools=mcp__agent-comms__board_post,Read(./**),Grep(./**),Glob(./**),Bash(git log *),"
                       "Bash(git show *),Bash(git status *),Bash(git rev-parse *)"), \
        "read tools, read-only git and the board tools the runner named; its own Edit and Bash grants are gone"
    denied = next(a for a in argv if a.startswith("--disallowedTools="))
    assert denied == ("--disallowedTools=Edit,Write,NotebookEdit,Bash(git *--output*),Read(~/.config/agent-comms/**),"
                      "Read(~/.ssh/**),Bash(curl *),WebFetch"), "the runner's deny rules are kept"
    assert not any(a in argv for a in ("acceptEdits", "Edit", "Bash(npm test *)"))
    with_dirs = access.claude_read_only(runner + ["--add-dir", "/extra/one", "/extra/two", "--add-dir=/extra/three"])
    assert not any("extra" in a or a == "--add-dir" for a in with_dirs), "nor an extra directory"
    assert value_after(argv, "--append-system-prompt") == "Be brief." and value_after(argv, "--model") == "sonnet"
    assert argv[-4:] == ["--permission-prompts", "none", "--session-id", UNKNOWN]

    # The shipped runner (board tools only): all of the board's tools, and the same narrowing.
    argv, _ = claude_argv(senv, "ro-shipped", access_word="read-only", claude_tool_projects=[PROJECT])
    assert "--allowedTools=" + ",".join(access.READ_ONLY_ALLOWED) in argv
    assert "--append-system-prompt" not in argv, "narrowed, not scoped: the scoped tools would widen it"
    # With an --mcp-config of its own the result is a runner runner_preflight.read_only recognises.
    own = ["claude", "-p", "{prompt}", "--mcp-config", "/x/board.mcp.json", "--dangerously-skip-permissions"]
    narrowed = access.claude_read_only(own)
    assert rp.read_only(narrowed) and "--strict-mcp-config" in narrowed and "--dangerously-skip-permissions" not in narrowed
    assert set(access.READ_ONLY_ALLOWED) <= set(rp.READ_ONLY_TOOLS) and set(rp.READ_ONLY_DENIES) <= set(access.READ_ONLY_DENIED)
    # A triage runner asked for read-only is used as written.
    triage, granted = claude_argv(senv, "ro-triage", runner=TRIAGE, access_word="read-only")
    assert granted == "read-only" and TRIAGE[-2] in triage and TRIAGE[-1] in triage
    # A --settings file can grant what nobody sees: such a runner is not narrowed.
    with pytest.raises(ValueError, match="cannot be narrowed to read-only"):
        claude_argv(senv, "ro-settings", runner=SHIPPED + ["--settings", "/x/settings.json"], access_word="read-only")


def test_read_only_gets_no_extra_writable_roots(senv, tmp_path):
    extra = tmp_path / "extra"
    extra.mkdir()
    roots = {"writable_roots": {PROJECT: [str(extra)]}}
    plain, _ = claude_argv(senv, "roots", access_word="workspace", **roots)
    assert f"--add-dir={extra}" in plain
    narrowed, _ = claude_argv(senv, "roots-ro", access_word="read-only", **roots)
    assert not any(a.startswith("--add-dir") for a in narrowed)


def test_claude_full_needs_the_nodes_setting_and_is_bypass_permissions_with_the_deny_rules_kept(senv):
    with pytest.raises(ValueError) as e:
        claude_argv(senv, "full-off", access_word="full")
    assert str(e.value) == access.FULL_REFUSED and "allow_full_access" in str(e.value), "refused, never narrowed"
    runner = SHIPPED + ["--disallowedTools=Bash(git push *)"]
    argv, granted = claude_argv(senv, "full", runner=runner, access_word="full", sessions={"allow_full_access": True},
                                claude_tool_projects=[PROJECT])
    assert granted == "full"
    assert value_after(argv, "--permission-mode") == "bypassPermissions" and argv.count("--permission-mode") == 1
    assert "dontAsk" not in argv and "--disallowedTools=Bash(git push *)" in argv
    assert "--append-system-prompt" not in argv, "not scoped"
    assert argv[-4:] == ["--permission-prompts", "none", "--session-id", UNKNOWN], \
        "prompts stay off: verified live that bypassPermissions writes with --permission-prompts none"


@pytest.mark.parametrize("extra", [["--dangerously-skip-permissions"], ["--permission-mode", "bypassPermissions"],
                                   ["--permission-mode=bypassPermissions"], ["--allow-dangerously-skip-permissions"]])
def test_a_bypass_runner_is_a_full_root_only_where_the_node_allows_full_access(senv, extra):
    runner = ["claude", "-p", "{prompt}"] + extra
    with pytest.raises(ValueError, match="cannot be hosted as a session"):
        claude_argv(senv, "bypass-off", runner=runner)
    argv, granted = claude_argv(senv, "bypass-on", runner=runner, sessions={"allow_full_access": True})
    assert granted == "full" and all(x in argv for x in extra), "hosted as configured"
    assert access.claude_native(runner) == "full"
    # Narrower than it is configured: read-only is the narrowing; `workspace` is not something this node can say.
    argv, granted = claude_argv(senv, "bypass-ro", runner=runner, access_word="read-only",
                                sessions={"allow_full_access": True})
    assert granted == "read-only" and not any("bypass" in a or "dangerously" in a for a in argv)
    with pytest.raises(ValueError) as e:
        claude_argv(senv, "bypass-ws", runner=runner, access_word="workspace", sessions={"allow_full_access": True})
    assert str(e.value) == access.WORKSPACE_FROM_BYPASS


def test_a_root_derives_its_access_from_its_runner(senv):
    assert access.claude_native(SHIPPED) == "workspace" and access.claude_native(TRIAGE) == "read-only"
    assert access.claude_native(BYPASS) == "full"
    assert access.prepare(TRIAGE).granted == "read-only" and access.prepare(SHIPPED).granted == "workspace"
    assert access.prepare(BYPASS, allow_full=True).granted == "full"
    plain = access.prepare(SHIPPED)
    assert (plain.template, plain.configure, plain.as_configured) == (SHIPPED, None, True), "nothing is touched"
    assert access.prepare(["grok", "{prompt}"]).granted is None
    with pytest.raises(ValueError, match="no access levels"):
        access.prepare(["grok", "{prompt}"], "read-only")
    with pytest.raises(ValueError, match="access must be one of"):
        access.prepare(SHIPPED, "root")


def test_effort_becomes_claudes_flag_and_replaces_the_runners_own(senv):
    host = host_for(senv, "effort", runners={"claude-code": SHIPPED + ["--effort", "max"]})
    s = host.open(UNKNOWN, agent="claude", runtime="claude-code", project=PROJECT, effort="low")
    argv = host._argv(s)
    assert value_after(argv, "--effort") == "low" and argv.count("--effort") == 1 and "max" not in argv
    assert argv[-4:] == ["--effort", "low", "--session-id", UNKNOWN]
    kept = host_for(senv, "effort-own", runners={"claude-code": SHIPPED + ["--effort", "max"]})
    assert value_after(kept._argv(kept.open(UNKNOWN, agent="claude", runtime="claude-code", project=PROJECT)),
                       "--effort") == "max", "without a word the runner's own stays"
    with pytest.raises(ValueError, match="effort must be one of"):
        host_for(senv, "effort-bad").open(UNKNOWN, agent="claude", runtime="claude-code", project=PROJECT,
                                          effort="--model=x")
    assert access.EFFORTS == ("low", "medium", "high", "xhigh", "max")


# ------------------------------------------------------------ access: the node maps the words (Codex)


def codex_params(env, name, runner=CODEX_RUNNER, access_word=None, effort=None, **over) -> tuple[dict, str, SessionHost]:
    host = host_for(env, name, runners={"codex-cli": runner}, **over)
    s = host.open(UNKNOWN, agent="codex", runtime="codex-cli", project=PROJECT, access=access_word, effort=effort)
    host._argv(s)
    return s.provider._params, s.access, host


def test_codex_access_is_the_threads_sandbox(senv):
    ws = ["codex", "exec", "--cd", "{project}", "--sandbox", "workspace-write", "{prompt}"]
    ro = ["codex", "exec", "--cd", "{project}", "{prompt}"]                    # `codex exec` without --sandbox
    params, granted, _ = codex_params(senv, "ws", runner=ws)
    assert (params["sandbox"], params["approvalPolicy"], granted) == ("workspace-write", "never", "workspace")
    params, granted, _ = codex_params(senv, "ws-asked", runner=ws, access_word="workspace")
    assert (params["sandbox"], granted) == ("workspace-write", "workspace"), "the runner as configured"
    params, granted, _ = codex_params(senv, "ws-ro", runner=ws, access_word="read-only")
    assert (params["sandbox"], params["approvalPolicy"], granted) == ("read-only", "never", "read-only")
    params, granted, _ = codex_params(senv, "ro", runner=ro)
    assert (params["sandbox"], granted) == ("read-only", "read-only")
    params, granted, _ = codex_params(senv, "ro-ws", runner=ro, access_word="workspace")
    assert (params["sandbox"], granted) == ("read-only", "read-only"), "as configured, and reported as what it is"
    with pytest.raises(ValueError) as e:
        codex_params(senv, "full-off", runner=ws, access_word="full")
    assert str(e.value) == access.FULL_REFUSED
    params, granted, _ = codex_params(senv, "full", runner=ws, access_word="full", sessions={"allow_full_access": True})
    assert (params["sandbox"], params["approvalPolicy"], granted) == ("danger-full-access", "never", "full"), \
        "the schema's SandboxMode value; approvals stay off"


@pytest.mark.parametrize("extra", [["--sandbox", "danger-full-access"], ["-s", "danger-full-access"],
                                   ["--sandbox=danger-full-access"], ["-c", 'sandbox_mode="danger-full-access"'],
                                   ["--dangerously-bypass-approvals-and-sandbox"], ["--yolo"]])
def test_a_codex_bypass_runner_is_a_full_root_only_where_the_node_allows_full_access(senv, extra):
    runner = ["codex", "exec", "--cd", "{project}", *extra, "-c", cx.APPROVE, "{prompt}"]
    with pytest.raises(ValueError) as e:
        codex_params(senv, "off", runner=runner)
    assert str(e.value) == BYPASS_REFUSED, "as before there were access levels"
    on = {"sessions": {"allow_full_access": True}}
    params, granted, host = codex_params(senv, "on", runner=runner, **on)
    assert (params["sandbox"], params["approvalPolicy"], granted) == ("danger-full-access", "never", "full")
    argv = host._argv(host.sessions[UNKNOWN])
    assert argv[:2] == ["codex", "app-server"] and ["-c", cx.APPROVE] == argv[-4:-2]
    assert not any("danger" in a or "yolo" in a for a in argv), "nothing that bypasses is passed on as a flag"
    params, granted, _ = codex_params(senv, "ws", runner=runner, access_word="workspace", **on)
    assert (params["sandbox"], granted) == ("workspace-write", "workspace")
    params, granted, _ = codex_params(senv, "ro", runner=runner, access_word="read-only", **on)
    assert (params["sandbox"], granted) == ("read-only", "read-only")


def test_effort_goes_with_every_codex_turn(senv):
    p = CodexSession()
    p.configure(access="read-only", effort="low")
    p.argv(cx.RUNNER, session_id=cx.SID, resume=False, model=None)
    assert p._params["sandbox"] == "read-only"
    p.thread_id = cx.THREAD
    turn = json.loads(p.turn("hello")[0])
    assert turn["method"] == "turn/start" and turn["params"] == {
        "threadId": cx.THREAD, "input": [{"type": "text", "text": "hello"}], "effort": "low"}
    plain = CodexSession()
    plain.argv(cx.RUNNER, session_id=cx.SID, resume=False, model=None)
    plain.thread_id = cx.THREAD
    assert "effort" not in json.loads(plain.turn("hello")[0])["params"] and plain._params["sandbox"] == "workspace-write"
    with pytest.raises(ValueError, match="unknown access"):
        CodexSession().configure(access="root")
    full = ClaudeSession()
    full.configure(access="full")
    assert "--dangerously-skip-permissions" in full.argv(BYPASS, session_id=cx.SID, resume=False, model=None)
    with pytest.raises(ValueError, match="cannot be hosted as a session"):
        ClaudeSession().argv(BYPASS, session_id=cx.SID, resume=False, model=None)


# ------------------------------------------------------------ access, through the hub and a node


def test_the_node_opens_each_child_at_its_access_and_reports_what_it_really_has(senv):
    spy = senv.node.sessions.client = Spy(senv.node.sessions.client)
    sid = root(senv)
    assert [body["access"] for path, body in spy.posts if path.endswith(sid) and "state" in body] == ["workspace"]
    answer(senv, sid)
    ro = child(senv, sid, "read", access="read-only", effort="high", model="haiku")["session_id"]
    ws = child(senv, sid, "work")["session_id"]
    cdx = child(senv, sid, "count", agent="codex", access="read-only", effort="low")["session_id"]
    tick(senv)
    ro_argv, ws_argv = call_for(senv, ro)["argv"], call_for(senv, ws)["argv"]
    assert "--allowedTools=" + ",".join(access.READ_ONLY_ALLOWED) in ro_argv
    assert next(a for a in ro_argv if a.startswith("--disallowedTools=")).startswith("--disallowedTools=Edit,Write,")
    assert ro_argv[-8:] == ["--permission-prompts", "none", "--model", "haiku", "--effort", "high", "--session-id", ro]
    assert ws_argv == call_for(senv, sid)["argv"][:-1] + [ws], "a workspace child is started like its parent"
    assert call_for(senv, ro)["cwd"] == call_for(senv, ws)["cwd"] == call_for(senv, sid)["cwd"] == str(senv.work), \
        "shared: the parent's checkout"
    codex_child = codex_proc(senv)
    codex_handshake(senv, codex_child)
    thread_start, turn_start = written(codex_child)[2]["params"], written(codex_child)[3]["params"]
    assert thread_start == {"sandbox": "read-only", "approvalPolicy": "never"}, \
        "the sandbox of its access word and approvals off, and never a cwd (Codex would record it as trusted)"
    assert codex_call(senv)["cwd"] == str(senv.work), "its process is started in the parent's checkout instead"
    assert turn_start["effort"] == "low" and turn_start["input"] == [{"type": "text", "text": "count"}]
    assert [stored(senv, x).get("granted") for x in (ro, ws, cdx)] == ["read-only", "workspace", "read-only"]
    assert all(set(body) <= {"events", "seq", "state", "pid", "resume", "provider_id", "access"}
               for _, body in spy.posts), "words and nothing else"


def test_a_full_child_exists_only_under_a_full_root_on_a_node_that_allows_it(senv):
    cfg = tree_config(senv, runners={"claude-code": BYPASS}, sessions={"allow_full_access": True})
    node = make_node(senv, config=cfg, log_dir=senv.tmp / "full-logs")
    sid = root(senv, node=node)
    assert stored(senv, sid)["access"] == "full" and "--dangerously-skip-permissions" in call_for(senv, sid, node)["argv"]
    full = child(senv, sid)                              # the default: its parent's
    ro = child(senv, sid, access="read-only")
    ws = child(senv, sid, access="workspace")            # the hub allows the word; this node cannot say it for Claude
    cdx = child(senv, sid, agent="codex", access="full")
    tick(senv, node)
    assert full["access"] == "full" and "--dangerously-skip-permissions" in call_for(senv, full["session_id"], node)["argv"]
    ro_argv = call_for(senv, ro["session_id"], node)["argv"]
    assert value_after(ro_argv, "--permission-mode") == "dontAsk" and "--dangerously-skip-permissions" not in ro_argv
    view = show(senv, ws["session_id"])
    assert view["state"] == "ended" and "not at `workspace`" in view["error"], "refused with the reason, not narrowed"
    assert msg(senv, ws["session_id"], 1)["state"] == "failed"
    codex_child = codex_proc(senv, node)
    codex_handshake(senv, codex_child, node)
    assert written(codex_child)[2]["params"]["sandbox"] == "danger-full-access"
    assert stored(senv, cdx["session_id"])["granted"] == "full"

    # The same hub records, a node that does NOT allow full access: every full session is refused, loudly.
    strict = make_node(senv, config=tree_config(senv, runners={"claude-code": BYPASS}), log_dir=senv.tmp / "strict-logs")
    tick(senv, strict, times=3)
    assert show(senv, sid)["state"] == "ended" and "cannot be hosted as a session" in show(senv, sid)["error"]
    assert all(show(senv, x["session_id"])["state"] == "ended" for x in (full, ro, cdx)), "and its children with it"


def test_full_asked_of_a_node_that_does_not_allow_it_ends_the_child_with_the_reason(senv):
    sid = root(senv)
    # The hub's own record says the parent is full (as a node that allowed it would have reported).
    senv.laptop.post(f"/api/nodes/sessions/{sid}", json={"events": [], "state": "idle", "access": "full"})
    kid = child(senv, sid, access="full")["session_id"]
    tick(senv)
    view = show(senv, kid)
    assert view["state"] == "ended" and "allow_full_access" in view["error"] and "full access is not allowed" in view["error"]
    assert children(senv)[1:] == [], "nothing was started for it"
    assert show(senv, sid)["state"] != "ended"


def test_max_live_counts_children_too(senv):
    node = make_node(senv, config=tree_config(senv, sessions={"max_live": 2}), log_dir=senv.tmp / "two-logs")
    sid = root(senv, node=node)
    answer(senv, sid, node=node)                              # the root is idle now
    a = child(senv, sid, "a")["session_id"]
    tick(senv, node)
    assert msg(senv, a, 1)["state"] == "delivered" and len(children(senv, node)) == 2
    b = child(senv, sid, "b")["session_id"]
    tick(senv, node, times=3)                                   # the idle root's process makes room
    assert children(senv, node)[0].terminated == 1 and msg(senv, b, 1)["state"] == "delivered"
    c = child(senv, sid, "c")["session_id"]
    tick(senv, node, times=3)
    assert msg(senv, c, 1)["state"] == "queued" and len(children(senv, node)) == 3, "both are busy: it waits"


# ------------------------------------------------------------ isolation


def git_config(env, **over) -> DispatchConfig:
    """The node's settings with the checkout a real Git repository and a worktree directory beside it."""
    if not (env.work / ".git").exists():
        git(env.work, "init", "-q")
        (env.work / "README").write_text("hi\n")
        git(env.work, "add", "README")
        git(env.work, "commit", "-q", "-m", "init")
    config = tree_config(env, **over)
    config.worktree_dir = os.path.realpath(env.tmp / "wt")
    return config


@pytest.fixture
def wtenv(tmp_path):
    return session_env(tmp_path, dispatch=TREE, config=git_config)


def test_a_worktree_child_runs_in_a_new_worktree_from_the_parents_head(wtenv):
    sid = root(wtenv)
    (wtenv.work / "uncommitted.txt").write_text("the parent's work in progress\n")
    shared = child(wtenv, sid, "s")["session_id"]
    own = child(wtenv, sid, "w", isolation="worktree")
    assert own["isolation"] == "worktree" and own["worktree"] is None
    tick(wtenv)
    path = call_for(wtenv, own["session_id"])["cwd"]
    assert call_for(wtenv, shared)["cwd"] == str(wtenv.work)
    assert path == session_worktrees.path_for(wtenv.node.config, str(wtenv.work), own["session_id"])
    assert path.startswith(os.path.realpath(wtenv.tmp / "wt") + os.sep) and path.endswith("session-" + own["session_id"])
    assert git(path, "rev-parse", "HEAD") == git(wtenv.work, "rev-parse", "HEAD")
    assert os.path.exists(os.path.join(path, "README")) and not os.path.exists(os.path.join(path, "uncommitted.txt"))
    assert path in git(wtenv.work, "worktree", "list")
    view = show(wtenv, own["session_id"])
    assert view["worktree"] == path and view["worktree_removed_at"] is None
    assert f"worktree: {path} on node laptop" in cli._fmt_hosted_detail(view)
    record_file = json.loads((wtenv.tmp / "laptop-logs" / WORKTREES_FILE).read_text())
    assert record_file == {own["session_id"]: {"path": path, "checkout": str(wtenv.work)}}
    # A restarted node opens the session in the same worktree (Claude finds a conversation only from its directory).
    again = make_node(wtenv, config=git_config(wtenv), log_dir=wtenv.tmp / "laptop-logs")
    tick(wtenv, again)
    send(wtenv, own["session_id"], "more")
    tick(wtenv, again)
    assert call_for(wtenv, own["session_id"], again)["cwd"] == path


def test_a_codex_worktree_child_is_never_given_a_cwd_and_works_where_its_process_starts(wtenv):
    """`thread/start` with a `cwd` makes Codex record that directory as trusted in the user's config.toml. A child
    in its own worktree gets there the way every hosted Codex session gets to its directory: its process is
    started in it. Only `thread/resume` names it (the node's own path), so a resumed thread does not go on elsewhere."""
    sid = root(wtenv)
    answer(wtenv, sid)
    kid = child(wtenv, sid, "count", agent="codex", isolation="worktree")["session_id"]
    tick(wtenv)
    path = session_worktrees.path_for(wtenv.node.config, str(wtenv.work), kid)
    assert codex_call(wtenv)["cwd"] == path and os.path.isdir(path) and path != str(wtenv.work)
    assert "--cd" not in codex_call(wtenv)["argv"] and path not in codex_call(wtenv)["argv"]
    proc = codex_proc(wtenv)
    codex_handshake(wtenv, proc)
    assert written(proc)[2]["method"] == "thread/start"
    assert written(proc)[2]["params"] == {"sandbox": "workspace-write", "approvalPolicy": "never"}, "no cwd"
    assert show(wtenv, kid)["worktree"] == path
    codex_turn(wtenv, proc, "three")
    assert show(wtenv, kid)["reply"] == "three"

    again = make_node(wtenv, config=git_config(wtenv), log_dir=wtenv.tmp / "laptop-logs")
    tick(wtenv, again)
    send(wtenv, kid, "more")
    tick(wtenv, again)
    assert codex_call(wtenv, again)["cwd"] == path, "the same worktree after a node restart"
    proc = codex_proc(wtenv, again)
    proc.emit(cx.reply(1, cx.INIT_RESULT))
    tick(wtenv, again)
    resume = written(proc)[2]
    assert resume["method"] == "thread/resume" and resume["params"]["cwd"] == path
    assert resume["params"]["threadId"] == cx.THREAD and resume["params"]["sandbox"] == "workspace-write"


def test_a_worktree_childs_turn_does_not_make_the_project_directory_busy(wtenv):
    sid = root(wtenv)
    answer(wtenv, sid)
    now = wtenv.clock
    own = child(wtenv, sid, "w", isolation="worktree")["session_id"]
    tick(wtenv)
    assert stored(wtenv, own)["in_flight"] == 1
    assert hosted_wake.turn_projects(wtenv.board, now()) == set(), "its turn runs in another directory"
    shared = child(wtenv, sid, "s")["session_id"]
    tick(wtenv)
    assert stored(wtenv, shared)["in_flight"] == 1
    assert hosted_wake.turn_projects(wtenv.board, now()) == {PROJECT}, "a shared child counts exactly like its parent"
    answer(wtenv, shared)
    assert hosted_wake.turn_projects(wtenv.board, now()) == set()


def close(env, sid, node=None) -> dict:
    assert env.human.post(f"/api/hosted-sessions/{sid}/close").status_code == 200
    tick(env, node, times=3)
    return show(env, sid)


def test_a_worktree_is_removed_after_the_session_only_when_nothing_unique_is_in_it(wtenv):
    sid = root(wtenv)
    clean, dirty, ahead = (child(wtenv, sid, x, isolation="worktree")["session_id"] for x in ("a", "b", "c"))
    tick(wtenv)
    paths = {x: show(wtenv, x)["worktree"] for x in (clean, dirty, ahead)}
    assert all(os.path.isdir(p) for p in paths.values()) and len(set(paths.values())) == 3
    Path(paths[dirty], "notes.txt").write_text("not committed\n")
    Path(paths[ahead], "more.txt").write_text("committed on no branch\n")
    git(paths[ahead], "add", "more.txt")
    git(paths[ahead], "commit", "-q", "-m", "detached work")

    view = close(wtenv, clean)
    assert view["state"] == "ended" and view["worktree_removed_at"] is not None
    assert not os.path.exists(paths[clean]) and paths[clean] not in git(wtenv.work, "worktree", "list")
    assert "(removed by its node after the session ended)" in cli._fmt_hosted_detail(view)
    for kept in (dirty, ahead):
        view = close(wtenv, kept)
        assert view["state"] == "ended" and view["worktree_removed_at"] is None and os.path.isdir(paths[kept])
        assert f"worktree: {paths[kept]} on node laptop (it stays there while" in cli._fmt_hosted_detail(view)
    assert set(json.loads((wtenv.tmp / "laptop-logs" / WORKTREES_FILE).read_text())) == {dirty, ahead}

    # Later the human commits the one and puts the other's commit on a branch: the next sweep removes both.
    os.remove(os.path.join(paths[dirty], "notes.txt"))
    git(wtenv.work, "branch", "kept-work", git(paths[ahead], "rev-parse", "HEAD"))
    tick(wtenv)
    assert os.path.isdir(paths[dirty]), "not before the next sweep"
    wtenv.clock.advance(3601)
    wtenv.node.hello()
    tick(wtenv, times=2)
    assert not os.path.exists(paths[dirty]) and not os.path.exists(paths[ahead])
    assert show(wtenv, dirty)["worktree_removed_at"] is not None and show(wtenv, ahead)["worktree_removed_at"] is not None
    assert not (wtenv.tmp / "laptop-logs" / WORKTREES_FILE).exists() or json.loads(
        (wtenv.tmp / "laptop-logs" / WORKTREES_FILE).read_text()) == {}
    assert show(wtenv, sid)["state"] != "ended" and os.path.isdir(wtenv.work)


def test_a_worktree_child_that_cannot_get_its_worktree_ends_with_the_reason(senv):
    sid = root(senv)                                # this node's checkout is a plain folder, not a Git repository
    kid = child(senv, sid, isolation="worktree")["session_id"]
    tick(senv)
    view = show(senv, kid)
    assert view["state"] == "ended" and "not a Git repository" in view["error"]
    assert not (senv.tmp / "laptop-logs" / WORKTREES_FILE).exists() or json.loads(
        (senv.tmp / "laptop-logs" / WORKTREES_FILE).read_text()) == {}


def test_session_worktrees_keeps_to_its_own_directories(wtenv, tmp_path):
    config, checkout = wtenv.node.config, str(wtenv.work)
    path = session_worktrees.path_for(config, checkout, UNKNOWN)
    assert os.path.basename(path) == "session-" + UNKNOWN
    assert os.path.dirname(path) == os.path.dirname(
        session_worktrees.run_worktree_path(config.worktree_root(), checkout, "claude", 7)), "beside the run worktrees"
    with pytest.raises(ValueError):
        session_worktrees.path_for(config, checkout, "../../etc")
    for bad in (os.path.expanduser("~"), os.path.dirname(os.path.expanduser("~")), os.path.expanduser("~/.ssh/wt")):
        config.worktree_dir = os.path.realpath(bad)
        with pytest.raises(Conflict, match="worktree_dir may not be"):
            session_worktrees.path_for(config, checkout, UNKNOWN)
    config.worktree_dir = os.path.realpath(tmp_path / "wt")
    made = session_worktrees.ensure(config, checkout, UNKNOWN)
    assert session_worktrees.ensure(config, checkout, UNKNOWN) == made, "reused"
    # Only a directory named as a session's worktree, of this checkout, is ever removed.
    other = tmp_path / "wt" / "plain"
    other.mkdir()
    assert session_worktrees.remove_if_safe(checkout, str(other)) == "kept"
    assert session_worktrees.remove_if_safe(checkout, str(wtenv.work)) == "kept"
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    git(foreign, "init", "-q")
    assert session_worktrees.remove_if_safe(str(foreign), made) == "kept", "not a worktree of that checkout"
    assert session_worktrees.remove_if_safe(checkout, made) == "removed" and not os.path.exists(made)
    assert session_worktrees.remove_if_safe(checkout, made) == "gone"
    taken = Path(session_worktrees.path_for(config, checkout, UNKNOWN))
    taken.mkdir(parents=True)
    with pytest.raises(Conflict, match="not a worktree of this checkout"):
        session_worktrees.ensure(config, checkout, UNKNOWN)


# ------------------------------------------------------------ ending


def test_closing_a_parent_closes_its_children_and_closing_a_child_leaves_the_parent(senv):
    sid = root(senv)
    a, b, c = (child(senv, sid, x)["session_id"] for x in ("a", "b", "c"))
    tick(senv)
    assert len(children(senv)) == 4
    assert close(senv, a)["state"] == "ended"
    assert [show(senv, x)["state"] != "ended" for x in (sid, b, c)] == [True, True, True], "closing a child stops there"
    assert senv.human.post(f"/api/hosted-sessions/{sid}/close").status_code == 200
    assert [show(senv, x)["stop"] for x in (sid, b, c)] == ["close", "parent", "parent"], "marked together, at once"
    assert "is being closed" in senv.human.post(f"/api/hosted-sessions/{b}/messages", json={"message": "x"}).text
    tick(senv, times=3)
    views = [show(senv, x) for x in (sid, b, c)]
    assert [v["state"] for v in views] == ["ended"] * 3
    assert [v["error"] for v in views] == ["closed by the human", "its parent session ended", "its parent session ended"]
    assert all(ch.terminated == 1 for ch in children(senv)), "no process is left"
    assert senv.node.sessions.host.sessions == {}


def test_a_parent_that_ends_by_itself_takes_its_children_with_it(senv):
    sid = root(senv)
    kid = child(senv, sid)["session_id"]
    tick(senv)
    r = senv.laptop.post(f"/api/nodes/sessions/{sid}", json={"events": [{"kind": "ended", "detail": "host error"}]})
    assert r.status_code == 200 and show(senv, sid)["state"] == "ended"
    assert show(senv, kid)["stop"] == "parent"
    tick(senv, times=3)
    assert (show(senv, kid)["state"], show(senv, kid)["error"]) == ("ended", "its parent session ended")
    assert msg(senv, kid, 1)["state"] == "failed"


def test_with_the_node_away_a_closed_parent_and_its_children_end_on_the_hub_at_once(senv):
    sid = root(senv)
    kid = child(senv, sid)["session_id"]
    tick(senv)
    senv.clock.advance(nodes.NODE_STALE_SECONDS + 1)
    assert senv.human.post(f"/api/hosted-sessions/{sid}/close").status_code == 200
    assert [show(senv, x)["state"] for x in (sid, kid)] == ["ended", "ended"]
    assert "its parent session ended" in show(senv, kid)["error"]
    senv.node.hello()
    tick(senv, times=2)
    assert all(ch.terminated == 1 for ch in children(senv)), "the node closes what the hub no longer lists"


# ------------------------------------------------------------ wakes are for roots


def test_a_child_is_never_woken_and_has_no_watcher_record(senv):
    sid = root(senv)
    answer(senv, sid)
    same = child(senv, sid, "same agent")["session_id"]
    other = child(senv, sid, "another agent", agent="codex")["session_id"]
    tick(senv)
    answer(senv, same)
    tid = senv.thread()
    ask(senv, tid, as_="grok", to=["claude"])
    ask(senv, tid, as_="grok", to=["codex"])
    tick(senv, times=2)
    hosted_wake.tick(senv.board)
    assert [m["kind"] for m in show(senv, sid)["messages"]] == ["user", "wake"], "the root is woken"
    assert wakes(senv, same) == [] and wakes(senv, other) == [], "a child is not, whoever the post is for"
    keys = [k for (k,) in senv.board.conn.execute("SELECT key FROM board_state WHERE key LIKE 'host.wake.%'")]
    assert keys == [hosted_wake.WAKE_PREFIX + sid], "only the root has a watcher record"
    assert record(senv, same) is None and record(senv, other) is None
    assert not hosted.holds(senv.board, "codex", PROJECT, 2, senv.clock()), "a child holds nothing for the dispatcher"


def woken_root(env) -> tuple[str, dict]:
    """A root that was woken for a request and did not read it; the grace is over."""
    sid = root(env)
    answer(env, sid)
    post = ask(env, env.thread())
    tick(env)
    answer(env, sid)
    later(env, GRACE)
    assert not hosted.holds(env.board, "claude", PROJECT, post["id"], env.clock())
    return sid, post


def test_a_childs_board_read_never_counts_as_its_roots(senv):
    sid, post = woken_root(senv)
    kid = child(senv, sid, "look around")["session_id"]         # the same agent, in the shared checkout
    reader = senv.session("claude")                             # the board session the CHILD's process registers
    assert hosted_wake._own_reader(senv.board.conn, reader, "claude", PROJECT), \
        "nothing in a board session's row tells it from the root's own"
    now = senv.clock
    out = senv.board.read_updates(senv.p["claude"], reader, only="addressed")
    assert [p["id"] for p in out["posts"]] == [post["id"]]
    assert record(senv, sid)["read"] == {}, "no read mark while a child of the same agent is around"
    assert not hosted.holds(senv.board, "claude", PROJECT, post["id"], now())
    senv.board.read_updates(senv.p["claude"], reader)           # an unfiltered read: a delivery receipt
    assert delivery.receipt(senv.board.conn, post["id"], "claude") is not None
    assert not hosted.holds(senv.board, "claude", PROJECT, post["id"], now()), "nor does its receipt count"
    close(senv, kid)
    assert not hosted.holds(senv.board, "claude", PROJECT, post["id"], now()), \
        "a child that was alive after the wake may have made that receipt"


def test_a_child_of_another_agent_or_one_that_ended_before_the_wake_changes_nothing(senv):
    sid = root(senv)
    answer(senv, sid)
    gone = child(senv, sid, "early")["session_id"]
    close(senv, gone)
    later(senv, 5)
    post = ask(senv, senv.thread())
    tick(senv)
    answer(senv, sid)
    later(senv, GRACE)
    child(senv, sid, "another identity", agent="codex")
    reader = senv.session("claude")
    senv.board.read_updates(senv.p["claude"], reader, only="addressed")
    assert record(senv, sid)["read"] == {str(post["id"]): senv.clock()}
    assert hosted.holds(senv.board, "claude", PROJECT, post["id"], senv.clock()), "the root's own read counts as ever"


def test_a_read_mark_from_before_the_child_existed_still_holds(senv):
    sid, post = woken_root(senv)
    senv.board.read_updates(senv.p["claude"], senv.session("claude"), only="addressed")     # the root reads it
    assert hosted.holds(senv.board, "claude", PROJECT, post["id"], senv.clock())
    child(senv, sid, "later")
    assert hosted.holds(senv.board, "claude", PROJECT, post["id"], senv.clock()), "the mark was the root's own"


# ------------------------------------------------------------ who may


def test_only_the_human_can_start_a_child(senv):
    sid = root(senv)
    for who in ("claude", "codex", "laptop"):
        r = client(senv, who).post(f"/api/hosted-sessions/{sid}/children", json={"message": "x"})
        assert r.status_code == 403, (who, r.text)
    anonymous = TestClient(senv.app, base_url="http://127.0.0.1:8787")
    assert anonymous.post(f"/api/hosted-sessions/{sid}/children", json={"message": "x"}).status_code == 401
    with pytest.raises(Forbidden):
        hosted_tree.start_child(senv.board, senv.p["claude"], sid, "x", allowed=TREE["nodes"])
    assert "children" not in show(senv, sid)
    kid = child(senv, sid)["session_id"]
    for who in ("claude", "codex", "laptop"):       # and a child is a session like any other: the human's alone
        c = client(senv, who)
        assert c.get(f"/api/hosted-sessions/{kid}").status_code == 403
        assert c.post(f"/api/hosted-sessions/{kid}/messages", json={"message": "x"}).status_code == 403
        assert c.post(f"/api/hosted-sessions/{kid}/close").status_code == 403
    assert (show(senv, kid)["queued"], show(senv, kid)["stop"]) == (1, None)


def test_no_mcp_tool_and_no_gateway_route_reaches_child_sessions():
    from agent_comms import gateway

    source = (ROOT / "agent_comms" / "mcp_server.py").read_text()
    assert "hosted" not in source and "session_access" not in source and "host.session" not in source
    for method in ("GET", "POST", "PUT", "DELETE"):
        assert not gateway.allowed(method, f"/api/hosted-sessions/{UNKNOWN}/children")


# ------------------------------------------------------------ the CLI


def test_board_session_shows_the_tree(clienv, capsys):
    cli.main(["session", "start", PROJECT, "hello", "--wait"])
    capsys.readouterr()
    cli.main(["--json", "session", "list"])
    [parent] = json.loads(capsys.readouterr().out)["sessions"]
    sid = parent["session_id"]
    cli.main(["session", "start", "--parent", sid, "--role", "review", "--title", "Check the diff", "--access",
              "read-only", "--model", "haiku", "--effort", "low", "--request-id", "r1", "review this", "--wait"])
    out, err = capsys.readouterr()
    assert out == "REVIEW THIS\n" and "child review-1 " in err and f"of {sid}; message 1 queued" in err
    cli.main(["session", "start", "--parent", sid, "--isolation", "shared", "build it"])
    assert capsys.readouterr().out.startswith("child general-1 ")
    cli.main(["--json", "session", "list"])
    kids = {s["name"]: s for s in json.loads(capsys.readouterr().out)["sessions"] if s.get("parent")}
    review, general = kids["review-1"]["session_id"], kids["general-1"]["session_id"]

    cli.main(["session", "list"])
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 3 and lines[0].startswith(f"{sid} [idle] claude in {PROJECT} on node laptop")
    assert lines[1] == (f"    review-1 {review} [idle] claude, model haiku, effort low, access read-only, shared, "
                        "turns 1, queued 0")
    assert lines[2].startswith(f"    general-1 {general} [") and ", access workspace, shared, turns 0" in lines[2]

    cli.main(["session", "show", sid])
    shown = capsys.readouterr().out
    assert "  access workspace\n" in shown and "  children:\n" in shown
    assert f"    review-1 {review} [idle] claude, model haiku, effort low, access read-only, shared" in shown
    cli.main(["session", "show", review])
    shown = capsys.readouterr().out
    assert (f"  child review-1 of {sid}: role review, title 'Check the diff', access read-only, isolation shared, "
            "effort low, request id r1") in shown and "reply to message 1:\nREVIEW THIS" in shown

    cli.main(["session", "send", review, "and this", "--wait"])       # send, show and close take a child's id
    assert capsys.readouterr().out == "AND THIS\n"
    cli.main(["session", "start", "--parent", sid, "--request-id", "r1", "review this"])
    assert "already started for this request id" in capsys.readouterr().out
    cli.main(["session", "close", review])
    assert f"session {review} is closing" in capsys.readouterr().out
    cli.main(["session", "close", sid])
    capsys.readouterr()
    cli.main(["session", "list"])
    listed = capsys.readouterr().out
    assert listed.count("closing") >= 2 or "(no hosted sessions)" in listed


def test_board_session_start_keeps_parent_and_project_apart(clienv, capsys):
    with pytest.raises(SystemExit, match="with --parent give only the message"):
        cli.main(["session", "start", PROJECT, "hello", "--parent", UNKNOWN])
    with pytest.raises(SystemExit, match="say which project"):
        cli.main(["session", "start", "hello"])
    with pytest.raises(SystemExit, match="--access, --role apply to a child session only"):
        cli.main(["session", "start", PROJECT, "hello", "--access", "read-only", "--role", "test"])
    with pytest.raises(SystemExit, match="no such hosted session"):
        cli.main(["session", "start", "--parent", UNKNOWN, "hello"])
    with pytest.raises(SystemExit):
        cli.main(["session", "start", "--parent", UNKNOWN, "--access", "root", "hello"])
    assert clienv.requests[-1][1].endswith(f"/api/hosted-sessions/{UNKNOWN}/children")
