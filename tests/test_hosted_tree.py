"""Parent and child hosted sessions (hosted_tree.py, session_access.py, session_worktrees.py): the gate, the rules
for making a child, access words and what each becomes for Claude and for Codex, isolation, the cascade, roots-only
wakes, and `board session`'s tree. Fakes as in test_hosted_sessions; git is real where a worktree is made. Never a
real agent CLI."""

import json
import os
import re
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent_comms import cli, db, delivery, dispatch, hosted, hosted_results, hosted_tree, hosted_wake, nodes
from agent_comms import runner_preflight as rp
from agent_comms import session_access as access, session_worktrees
from agent_comms.core import Conflict, Forbidden
from agent_comms.dispatch import DispatchConfig
from agent_comms.node_sessions import ABOVE_PARENT, PARENT_GONE, WORKTREES_FILE
from agent_comms.providers import Event
from agent_comms.providers.claude import ClaudeSession
from agent_comms.providers.codex import BYPASS_REFUSED, CodexSession
from agent_comms.session_host import SessionHost

from conftest import PROJECT
import test_providers_codex as cx
from test_hosted_sessions import (CODEX_RUNNER, P2, P3, UNKNOWN, children, client, clienv, codex_handshake,  # noqa: F401
                                  codex_turn, make_node, msg, node_config, send, session_env, show, start,
                                  tick, written)
from test_hosted_wake import GRACE, ask, later, record, wakes
from test_session_host import SHIPPED, result, value_after

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


WORKSPACE_TOOLS = ("Bash", "Edit", "Glob", "Grep", "Read", "Write")     # what an ordinary Claude process reports
# What every Claude launch that is not a full-access one carries: the settings key that switches bypass off.
SEAL = ["--settings", '{"permissions":{"disableBypassPermissionsMode":"disable"}}']


def began(sid, mode="dontAsk", tools=WORKSPACE_TOOLS) -> dict:
    """What a Claude process writes when a turn starts: the mode it REALLY runs in and the tools it really has.
    `mode` None: a process that does not say which mode it is in."""
    said = {"type": "system", "subtype": "init", "session_id": sid, "tools": list(tools)}
    return said if mode is None else said | {"permissionMode": mode}


def as_started(argv) -> tuple[str, tuple]:
    """(mode, built-in tools) a Claude process started with this command line reports when nothing interferes: the
    mode it names (`manual` is reported as `default`), and the tools `--tools=` leaves it (else the ordinary
    ones)."""
    mode = ("bypassPermissions" if "--dangerously-skip-permissions" in argv
            else value_after(argv, "--permission-mode") if "--permission-mode" in argv else "default")
    listed = [a.split("=", 1)[1].split(",") for a in argv if a.startswith("--tools=")]
    return "default" if mode == "manual" else mode, tuple(listed[-1]) if listed else WORKSPACE_TOOLS


@pytest.fixture(autouse=True)
def _reports_follow_the_command_line(request, monkeypatch):
    """test_hosted_sessions' fake turns (its `clienv` among them) always report `dontAsk` and the one tool Read.
    Here what a process reports is held against how it was started, so those turns report what the process's own
    command line gives (`as_started`)."""
    import test_hosted_sessions

    def reported(sid):
        for name in ("senv", "wtenv"):
            if name in request.fixturenames:
                calls = request.getfixturevalue(name).node.session_spawner.calls
                for call in reversed(calls):
                    if sid in call["argv"]:
                        return began(sid, *as_started(call["argv"]))
        return began(sid)

    monkeypatch.setattr(test_hosted_sessions, "init", reported)


def proc_of(env, sid, node=None):
    """(the latest spawn of this session's Claude process, the process)."""
    node = node or env.node
    i = max(i for i, c in enumerate(node.session_spawner.calls) if sid in c["argv"])
    return node.session_spawner.calls[i], node.session_spawner.children[i]


SAME = object()


def begin(env, sid, node=None, mode=SAME, tools=SAME) -> None:
    """The session's Claude process starts the turn it was given and says what it runs with: what its command line
    gives, unless the test has it say otherwise (a settings file, a process that is not what was asked for)."""
    call, proc = proc_of(env, sid, node)
    started = as_started(call["argv"])
    proc.emit(began(sid, started[0] if mode is SAME else mode, started[1] if tools is SAME else tools))
    tick(env, node)


def root(env, node=None, **kw) -> str:
    """A root session its node has opened (one pass): the hub knows its access, which the node computed from the
    session's command line. Its process has said nothing yet."""
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
    call, proc = proc_of(env, sid, node)
    proc.emit(began(sid, *as_started(call["argv"])), result(sid, text=text))
    tick(env, node or env.node)


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
        self.inner, self.posts, self.gets = inner, [], []

    def request(self, method, path, **kw):
        if method == "GET":
            self.gets.append((path, kw.get("params")))
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
    assert not {"parent", "root", "depth", "access", "child_counts", "ceiling", "checkout"} & set(stored(env, sid))
    view = show(env, sid)
    assert not {"parent", "access", "children", "depth", "ceiling"} & set(view)
    assert all(set(body) <= {"events", "seq", "state", "pid", "resume", "modes"} for _, body in spy.posts), \
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
    [listed] = env.laptop.get("/api/nodes/sessions?tree=1").json()["sessions"]
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
    listed = {s["session_id"]: s for s in senv.laptop.get("/api/nodes/sessions?tree=1").json()["sessions"]}
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


def test_the_parent_must_exist_be_open_and_within_the_depth_limit(senv):
    assert child(senv, UNKNOWN, expect=404)["message"] == "no such hosted session"
    assert senv.human.post("/api/hosted-sessions/not-a-uuid/children", json={"message": "x"}).status_code == 404
    sid = root(senv)
    kid = child(senv, sid)
    tick(senv)
    grand = child(senv, kid["session_id"])          # max_depth 2 as shipped: a grandchild
    assert (grand["parent"], grand["root"], grand["depth"]) == (kid["session_id"], sid, 2)
    out = child(senv, grand["session_id"], expect=409)
    assert "depth limit" in out["message"] and "max_depth is 2" in out["message"], "depth 2"
    senv.board.s.dispatch = {**TREE, "sessions": {"spawn": {"enabled": True, "max_depth": 1}}}
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
    assert plain == ["claude", "-p", "--permission-mode", "dontAsk", "--allowedTools=mcp__agent-comms", *SEAL,
                     "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
                     "--permission-prompts", "none", "--session-id", UNKNOWN]
    assert SEAL == ["--settings", access.NO_BYPASS_JSON]
    scoped, _ = claude_argv(senv, "scoped", access_word="workspace", claude_tool_projects=[PROJECT])
    assert "--allowedTools=" + ",".join(rp.ALLOWED) in scoped, "with the opt-in scoping a session already gets"
    assert scoped[scoped.index("--settings"):][:2] == SEAL, "scoped first, then bypass is switched off"


BOARD_ENTRY = {"type": "stdio", "command": "bash", "args": ["/x/stdio.sh"], "env": {"AGENT_COMMS_AGENT": "claude"}}


def test_claude_read_only_is_a_hard_tool_list_dont_ask_and_the_board_server_alone(senv, tmp_path):
    """S2. `--allowedTools` only adds approvals: a grant in a settings file stays in force under dontAsk. So
    read-only is a hard list of built-in tools (`--tools`), an explicit mode, and no MCP server but the board's."""
    mixed = tmp_path / "mixed.mcp.json"
    mixed.write_text(json.dumps({"mcpServers": {"agent-comms": BOARD_ENTRY, "other": {"command": "/x/other"}}}))
    runner = ["claude", "-p", "{prompt}", "--model", "sonnet", "--permission-mode", "acceptEdits",
              "--tools", "Bash", "Edit", "Read", "--mcp-config", str(mixed),
              "--allowedTools", "mcp__agent-comms__board_post", "Edit", "Bash(npm test *)",
              "--disallowedTools=Bash(curl *),WebFetch", "--settings", "/x/settings.json",
              "--append-system-prompt", "Be brief."]
    host = host_for(senv, "ro", runners={"claude-code": runner})
    opened = host.open(UNKNOWN, agent="claude", runtime="claude-code", project=PROJECT, access="read-only")
    argv = host._argv(opened)
    assert (opened.asked, opened.access) == ("read-only", "read-only"), "decided here, before anything is started"
    assert "--tools=Read,Grep,Glob" in argv and "--tools" not in argv and "Bash" not in argv, \
        "the built-in tools it HAS: nothing that writes or runs a command exists in the session"
    assert value_after(argv, "--permission-mode") == "dontAsk" and argv.count("--permission-mode") == 1
    assert "--strict-mcp-config" in argv and argv.count("--mcp-config") == 1 and str(mixed) not in argv
    config = Path(value_after(argv, "--mcp-config"))
    assert json.loads(config.read_text()) == {"mcpServers": {"agent-comms": BOARD_ENTRY}}, "the board's server alone"
    assert config.parent == host.sessions_dir and (config.stat().st_mode & 0o777) == 0o600
    assert next(a for a in argv if a.startswith("--allowedTools=")) == (
        "--allowedTools=mcp__agent-comms__board_post,Read(./**),Grep(./**),Glob(./**)"), \
        "reads in its directory and the board tools the runner named; its own Edit and Bash grants are gone"
    assert next(a for a in argv if a.startswith("--disallowedTools=")) == (
        "--disallowedTools=Edit,Write,NotebookEdit,Bash,Read(~/.config/agent-comms/**),Read(~/.ssh/**),Bash(curl *),"
        "WebFetch"), "the runner's deny rules are kept"
    assert not any(a in argv for a in ("acceptEdits", "Edit", "Bash(npm test *)"))
    assert "/x/settings.json" not in argv and "--setting-sources=" in argv, \
        "it loads NO settings: a settings file brings hooks, and a hook runs commands whatever tools there are"
    assert argv.count("--settings") == 1 and value_after(argv, "--settings") == access.NO_BYPASS_JSON, \
        "the one thing it is given with --settings is the key that switches bypass off"
    assert access.claude_read_only(runner + ["--setting-sources", "user,project"]).count("--setting-sources=") == 1
    assert "user,project" not in access.claude_read_only(runner + ["--setting-sources", "user,project"])
    assert value_after(argv, "--append-system-prompt") == "Be brief." and value_after(argv, "--model") == "sonnet"
    assert argv[-4:] == ["--permission-prompts", "none", "--session-id", UNKNOWN]
    assert opened.template[-2:] == SEAL and rp.read_only(opened.template[:-2]), \
        "a runner runner_preflight.read_only recognises, and the key"
    host.close(UNKNOWN)
    host.forget(UNKNOWN)
    assert not config.exists(), "the config goes with the session"

    # A runner that names no config with the board's server: no MCP server at all (nothing is looked for in the
    # user's own Claude configuration), and not the scoped tools either.
    argv, _ = claude_argv(senv, "ro-shipped", access_word="read-only", claude_tool_projects=[PROJECT])
    assert "--strict-mcp-config" in argv and "--mcp-config" not in argv and "--tools=Read,Grep,Glob" in argv
    assert "--allowedTools=" + ",".join(access.READ_ONLY_ALLOWED) in argv and "--append-system-prompt" not in argv
    stripped = access.claude_read_only(runner + ["--add-dir", "/extra/one", "/extra/two", "--add-dir=/extra/three",
                                                 "--dangerously-skip-permissions",
                                                 "--allow-dangerously-skip-permissions"])
    assert not any("extra" in a or a == "--add-dir" or "dangerously" in a for a in stripped)

    # S2 (round 3). Whatever loads a plugin, an agent definition or more tools goes too: a plugin's hooks run
    # commands in a session that loads no settings at all (seen live with --plugin-dir).
    loaded = ["--plugin-dir", "/x/plug", "--plugin-dir=/x/more.zip", "--plugin-url", "https://x.example/p.zip",
              "--agents", '{"helper": {"description": "d", "prompt": "p"}}', "--agent", "helper", "--chrome", "--ide",
              "--brief"]
    kept = access.claude_read_only(runner + loaded)
    assert kept == access.claude_read_only(runner), "nothing of them is left"
    assert not any("plug" in a or "agent" in a.split("=")[0] or "helper" in a for a in kept)
    # ... and an option this list does not know is not passed on: the session is refused.
    for unknown in (["--worktree"], ["--debug-file", "/x/debug.log"], ["--some-future-loader", "/x"], ["stray"],
                    ["--plugin-marketplace=/x"]):
        with pytest.raises(ValueError) as e:
            access.claude_read_only(runner + unknown)
        assert "a read-only session does not take" in str(e.value) and "cannot be dropped safely" in str(e.value)
    assert access.claude_read_only(["claude", "-p", "{prompt}", "--append-system-prompt", "- a dash is a value",
                                    "--model=haiku", "-n", "name", "--restricted", "--verbose"])[:9] == [
        "claude", "-p", "{prompt}", "--append-system-prompt", "- a dash is a value", "--model=haiku", "-n", "name",
        "--restricted"], "what is known to load nothing stays, values and all"
    assert access.READ_ONLY_BUILTINS == ("Read", "Grep", "Glob")
    assert set(rp.READ_ONLY_DENIES) <= set(access.READ_ONLY_DENIED) and "Bash" in access.READ_ONLY_DENIED
    assert set(access.READ_ONLY_ALLOWED) <= set(rp.READ_ONLY_TOOLS)


def step_one_argv(runner, sid=UNKNOWN) -> list[str]:
    """The command line step 1 built for a hosted session of this runner (not scoped, no model override): the
    runner without its {prompt}, the stream flags, `--permission-prompts none`, the session id. Nothing else."""
    return [a for a in runner if a != "{prompt}"] + ["--input-format", "stream-json", "--output-format",
                                                     "stream-json", "--verbose", "--permission-prompts", "none",
                                                     "--session-id", sid]


def session_argv(runner, sid=UNKNOWN) -> list[str]:
    """The command line a hosted session of this runner gets NOW: what step 1 built, and behind the runner's own
    arguments the settings key that switches bypass off (SEAL), unless the runner selects bypass."""
    seal = [] if access.claude_runs_bypass(runner) else SEAL
    return step_one_argv([a for a in runner] + seal, sid)


@pytest.mark.parametrize("spawn", [False, True])
def test_a_root_runs_a_read_only_labelled_runner_exactly_as_its_owner_configured_it(tmp_path, spawn):
    """A session of its own gets NO added `--tools`, `--strict-mcp-config`, MCP config or permission mode: the hard
    read-only mapping is for a session ASKED for read-only by word. With child sessions off (and on), a hosted
    triage-style runner's command line is what step 1 built, its `git log` allowance included, plus the one
    settings key that switches bypass off (round 3, S3: restrictive, and the same for every launch that is not
    full)."""
    assert rp.read_only(TRIAGE), "a runner the dispatcher takes for read-only"
    expected = ["claude", "-p", "--model", "haiku", "--strict-mcp-config", "--mcp-config", "/x/haiku.mcp.json",
                "--permission-mode", "dontAsk",
                "--allowedTools=mcp__agent-comms,Read(./**),Grep(./**),Glob(./**),Bash(git log *),Bash(git show *)",
                "--disallowedTools=Edit,Write,NotebookEdit,Bash(git *--output*),Read(~/.config/agent-comms/**),"
                "Read(~/.ssh/**),Read(./data/**)",
                "--settings", '{"permissions":{"disableBypassPermissionsMode":"disable"}}',
                "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
                "--permission-prompts", "none", "--session-id"]
    assert session_argv(TRIAGE, "SID") == expected + ["SID"]
    assert step_one_argv(TRIAGE, "SID") == expected[:11] + expected[13:] + ["SID"], "step 1's, but for that key"
    env = session_env(tmp_path, dispatch=TREE if spawn else {"nodes": TREE["nodes"]},
                      config=lambda e: tree_config(e, runners={"claude-code": TRIAGE}, claude_tool_projects=[PROJECT]))
    sid = start(env)["session_id"]
    tick(env)
    assert call_for(env, sid)["argv"] == expected + [sid], "through the hub and the node"
    assert not list((env.tmp / "laptop-logs").rglob("*.json.tmp")) and not [
        x for x in (env.tmp / "laptop-logs").rglob("*.json") if x.name.endswith((".mcp.json", ".settings.json"))], \
        "no MCP config and no settings file was made for it"
    host = host_for(env, "direct", runners={"claude-code": TRIAGE}, sessions={"allow_full_access": True})
    opened = host.open(UNKNOWN, agent="claude", runtime="claude-code", project=PROJECT)
    assert host._argv(opened) == expected + [UNKNOWN] and opened.template == TRIAGE + SEAL
    assert opened.seal == "inline"
    assert access.prepare(TRIAGE).template == TRIAGE and access.prepare(TRIAGE, allow_full=True).template == TRIAGE

    # Its label is what its process reports. It has a shell (for `git log`), so it is `workspace`, the honest word.
    begin(env, sid, tools=("Bash", "Glob", "Grep", "Read"))
    if spawn:
        assert stored(env, sid)["access"] == "workspace"
        assert child(env, sid)["access"] == "workspace"
        assert "above the parent's (workspace)" in child(env, sid, access="full", expect=409)["message"]
    else:
        assert "access" not in stored(env, sid), "nobody asked: nothing more is reported"
    assert env.node.sessions.host.sessions[sid].access == "workspace"
    assert show(env, sid)["state"] != "ended"


HARD = ["claude", "-p", "{prompt}", "--permission-mode", "dontAsk", "--tools", "Read,Grep,Glob"]
ALLOW_ONLY = ["claude", "-p", "{prompt}", "--allow-dangerously-skip-permissions"]


def test_the_shipped_runners_name_a_mode_so_their_command_lines_are_what_step_one_built():
    """A. `prepare` adds one thing to a root's command line, a permission mode, for a runner that names none. Every
    shipped Claude runner names one. The host then adds the settings key that switches bypass off (round 3, S3):
    a deliberate, restrictive addition to every launch that is not full. Pinned here: the command line the shipped
    runner gets as a hosted session."""
    shipped = DispatchConfig.load(ROOT / "board.toml", local=False)
    claude = {k: t for k, t in shipped.runners.items() if os.path.basename(t[0]) == "claude"}
    assert claude == {"claude-code": SHIPPED}, "the shipped Claude runner (the triage runner ships commented out)"
    for runner in (*claude.values(), TRIAGE):
        assert access.claude_mode(runner) == "dontAsk"
        for allow in (False, True):
            got = access.prepare(runner, allow_full=allow)
            assert (got.template, got.granted, got.configure, got.as_configured) == (runner, "workspace", None, True)
    host = SessionHost(shipped, ROOT / "data" / "never-written")
    shipped.worktrees[PROJECT] = str(ROOT)
    opened = host.open(UNKNOWN, agent="claude", runtime="claude-code", project=PROJECT)
    assert host._argv(opened) == ["claude", "-p", "--permission-mode", "dontAsk", "--allowedTools=mcp__agent-comms",
                                  "--settings", '{"permissions":{"disableBypassPermissionsMode":"disable"}}',
                                  "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
                                  "--permission-prompts", "none", "--session-id", UNKNOWN]
    assert opened.seal == "inline" and opened.provider.on_open() == [], "nothing is written, nothing is asked"
    assert host._argv(host.open(UNKNOWN[:-1] + "1", agent="claude", runtime="claude-code", project=PROJECT,
                                access="workspace")) == host._argv(opened)[:-1] + [UNKNOWN[:-1] + "1"]


def test_a_runner_that_names_no_permission_mode_gets_the_standard_one_pinned(senv):
    """A. An explicit mode beats a settings file's defaultMode (seen live), so every hosted Claude session has one.
    A runner that names none gets `manual`, the CLI's standard mode; one that names any keeps it."""
    assert access.PINNED_MODE == "manual"
    plain = ["claude", "-p", "{prompt}", "--model", "haiku"]
    assert access.claude_mode(plain) is None
    argv, label = claude_argv(senv, "pin", runner=plain)
    assert argv == session_argv(plain + ["--permission-mode", "manual"]) and label == "workspace", \
        "the mode and the key; everything else as its owner configured it"
    argv, label = claude_argv(senv, "pin-child", runner=plain, access_word="workspace")
    assert argv == session_argv(plain + ["--permission-mode", "manual"]) and label == "workspace"
    for mode in ("acceptEdits", "auto", "manual", "dontAsk", "plan"):
        named = ["claude", "-p", "{prompt}", "--permission-mode", mode]
        assert claude_argv(senv, f"named-{mode}", runner=named)[0] == session_argv(named), mode
        assert claude_argv(senv, f"named-eq-{mode}", runner=["claude", "-p", "{prompt}", f"--permission-mode={mode}"])[0] \
            == session_argv(["claude", "-p", "{prompt}", f"--permission-mode={mode}"])
    scoped, _ = claude_argv(senv, "pin-scoped", runner=["claude", "-p", "{prompt}"], claude_tool_projects=[PROJECT])
    assert value_after(scoped, "--permission-mode") == "dontAsk" and scoped.count("--permission-mode") == 1, \
        "an opted-in project still gets the scoped mode, as it always did"
    assert claude_argv(senv, "pin-allow", runner=ALLOW_ONLY, sessions={"allow_full_access": True})[0] == \
        session_argv(ALLOW_ONLY + ["--permission-mode", "manual"])


def test_the_access_of_a_session_is_computed_from_its_command_line():
    """A. `claude_label`: deterministic, from the command line alone."""
    label = access.claude_label
    assert label(SHIPPED) == "workspace" and label(["claude", "-p", "{prompt}"]) == "workspace"
    assert label(BYPASS) == "full" and label(["claude", "-p", "{prompt}", "--permission-mode=bypassPermissions"]) == "full"
    assert label(ALLOW_ONLY) == "workspace", "it selects nothing"
    assert label(TRIAGE) == "workspace", "it calls itself read-only, but allows `git log`: it has a shell"
    assert label(HARD) == "read-only" and label(HARD[:-2] + ["--tools=Read,Glob"]) == "read-only"
    assert label(HARD[:-2] + ["--tools", "Read,Bash"]) == "workspace"
    assert label(["claude", "-p", "{prompt}", "--tools=Read,Glob"]) == "workspace", "not dontAsk"
    assert label(["claude", "-p", "{prompt}", "--permission-mode", "acceptEdits", "--tools=Read"]) == "workspace"
    assert label(access.claude_read_only(SHIPPED)) == "read-only" and label(access.claude_full(SHIPPED)) == "full"
    assert access.claude_mode(BYPASS) == "bypassPermissions" and access.claude_mode(HARD) == "dontAsk"


def test_a_roots_access_is_known_when_it_is_opened_and_never_changes_with_what_its_process_says(senv):
    """A. Decided before launch: the node reports a root's access when it opens it, before its process has written
    a line, and a child can be started under it at once. Later reports can only end a session; they relabel
    nothing."""
    on = {"sessions": {"allow_full_access": True}}
    cases = [(["claude", "-p", "{prompt}"], {}, "workspace"), (TRIAGE, {}, "workspace"), (HARD, {}, "read-only"),
             (BYPASS, on, "full"), (ALLOW_ONLY, on, "workspace")]
    for i, (runner, over, label) in enumerate(cases):
        node = make_node(senv, config=tree_config(senv, runners={"claude-code": runner}, **over),
                         log_dir=senv.tmp / f"case-{i}")
        node.hello()
        sid = start(senv)["session_id"]
        assert "not known yet" in child(senv, sid, expect=409)["message"], "no node has opened it yet"
        tick(senv, node)
        call, proc = proc_of(senv, sid, node)
        assert stored(senv, sid)["access"] == label and not proc.stdout, (runner, "its process has said nothing")
        assert node.sessions.host.sessions[sid].access == label
        if label == "full":
            # Started as full, which is what `show` says. For a CHILD it counts once the process has said so.
            assert "has not confirmed" in child(senv, sid, "go", expect=409)["message"]
            kid = child(senv, sid, "go", access="read-only")
        else:
            kid = child(senv, sid, "go")
            assert kid["access"] == label, "a child at once, at most at its parent's access"
            wider = access.LEVELS[access.RANK[label] + 1]
            assert f"above the parent's ({label})" in child(senv, sid, access=wider, expect=409)["message"]
        begin(senv, sid, node, tools=("Read",) if label == "workspace" else SAME)       # says less than it may: so?
        assert stored(senv, sid)["access"] == label and show(senv, sid)["state"] != "ended", "nothing is relabelled"
        assert show(senv, sid)["ceiling"] == label, "... and a full root's process has now said it is one"
        assert senv.human.post(f"/api/hosted-sessions/{sid}/close").status_code == 200
        tick(senv, node, times=3)
        assert show(senv, sid)["state"] == "ended" and show(senv, kid["session_id"])["state"] == "ended"
    senv.node.hello()


@pytest.mark.parametrize("word", ["read-only", "workspace"])
def test_a_child_of_a_read_only_labelled_runner(senv, tmp_path, word):
    """Asked for at `read-only`, a child gets the hard limits whatever its runner calls itself. Asked for at
    `workspace`, it runs the runner as configured, like a root: nothing is added."""
    own = tmp_path / "haiku.mcp.json"
    own.write_text(json.dumps({"mcpServers": {"agent-comms": BOARD_ENTRY}}))
    runner = [str(own) if a == "/x/haiku.mcp.json" else a for a in TRIAGE]
    host = host_for(senv, f"triage-{word}", runners={"claude-code": runner}, claude_tool_projects=[PROJECT])
    opened = host.open(UNKNOWN, agent="claude", runtime="claude-code", project=PROJECT, access=word)
    argv = host._argv(opened)
    if word == "workspace":
        assert argv == session_argv(runner) and opened.access == "workspace"
        return
    assert opened.access == "read-only" and "--tools=Read,Grep,Glob" in argv and "--strict-mcp-config" in argv
    assert next(a for a in argv if a.startswith("--allowedTools=")) == (
        "--allowedTools=mcp__agent-comms,Read(./**),Grep(./**),Glob(./**)"), "its git allowances need Bash: gone"
    denied = next(a for a in argv if a.startswith("--disallowedTools="))
    assert denied.startswith("--disallowedTools=Edit,Write,NotebookEdit,Bash,") and "Read(./data/**)" in denied
    assert json.loads(Path(value_after(argv, "--mcp-config")).read_text()) == {"mcpServers": {"agent-comms": BOARD_ENTRY}}
    assert value_after(argv, "--permission-mode") == "dontAsk" and "--append-system-prompt" not in argv


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
                                   ["--permission-mode=bypassPermissions"]])
def test_a_bypass_runner_is_a_full_root_only_where_the_node_allows_full_access(senv, extra):
    runner = ["claude", "-p", "{prompt}"] + extra
    with pytest.raises(ValueError, match="cannot be hosted as a session"):
        claude_argv(senv, "bypass-off", runner=runner)
    argv, granted = claude_argv(senv, "bypass-on", runner=runner, sessions={"allow_full_access": True})
    assert granted == "full" and all(x in argv for x in extra), "hosted as configured"
    assert access.claude_label(runner) == "full"
    # Narrower than it is configured: read-only is the narrowing; `workspace` is not something this node can say.
    argv, granted = claude_argv(senv, "bypass-ro", runner=runner, access_word="read-only",
                                sessions={"allow_full_access": True})
    assert granted == "read-only" and not any("bypass" in a or "dangerously" in a for a in argv)
    with pytest.raises(ValueError) as e:
        claude_argv(senv, "bypass-ws", runner=runner, access_word="workspace", sessions={"allow_full_access": True})
    assert str(e.value) == access.WORKSPACE_FROM_BYPASS


def test_the_flag_that_only_makes_bypass_selectable_is_not_full_access(senv):
    """S3. `--allow-dangerously-skip-permissions` selects nothing. Such a runner is still refused where full access
    is not allowed (as ever). Where it is, a root runs it as configured, with the standard mode pinned, and is a
    workspace session; a child asked for at workspace is started without the flag."""
    assert access.claude_label(ALLOW_ONLY) == "workspace" and not access.claude_runs_bypass(ALLOW_ONLY)
    with pytest.raises(ValueError, match="cannot be hosted as a session"):
        claude_argv(senv, "allow-off", runner=ALLOW_ONLY)
    argv, label = claude_argv(senv, "allow-on", runner=ALLOW_ONLY, sessions={"allow_full_access": True})
    assert label == "workspace" and argv == session_argv(ALLOW_ONLY + ["--permission-mode", "manual"])
    assert value_after(argv, "--settings") == access.NO_BYPASS_JSON, "selectable on its command line, switched off"
    argv, label = claude_argv(senv, "allow-ws", runner=ALLOW_ONLY, access_word="workspace")
    assert label == "workspace" and "--allow-dangerously-skip-permissions" not in argv
    assert value_after(argv, "--permission-mode") == "manual"


def test_prepare_without_a_word_adds_a_mode_or_nothing(senv):
    for runner in (SHIPPED, TRIAGE, HARD):
        for allow in (False, True):
            got = access.prepare(runner, allow_full=allow)
            assert (got.template, got.as_configured) == (runner, True), "nothing is touched"
    assert access.prepare(["claude", "-p", "{prompt}"]).template == ["claude", "-p", "{prompt}", "--permission-mode",
                                                                     "manual"]
    assert access.prepare(BYPASS).template == BYPASS and access.prepare(BYPASS).configure is None, \
        "a bypass runner is left for its adapter to refuse"
    assert access.prepare(ALLOW_ONLY).template == ALLOW_ONLY, "... and so is one that could select it"
    full = access.prepare(BYPASS, allow_full=True)
    assert (full.template, full.granted, full.configure) == (BYPASS, "full", "full")
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


@pytest.mark.parametrize("flag", ["--approve-for-me", "--dangerously-bypass-hook-trust"])
def test_codex_flags_that_do_not_turn_the_sandbox_off_are_not_full_access(senv, flag):
    """S4. Auto-approval and hook trust are not `danger-full-access`. Such a runner is never a full session: its
    adapter refuses it as before, and a node that allows full access does not take the flag for a request for it."""
    runner = ["codex", "exec", "--cd", "{project}", "--sandbox", "workspace-write", flag, "{prompt}"]
    assert access._codex_safe(runner) == (runner, False), "nothing that turns the sandbox off: nothing is taken out"
    with pytest.raises(ValueError) as e:
        codex_params(senv, "off", runner=runner)
    assert str(e.value) == BYPASS_REFUSED, "the adapter's refusal, as before"
    on = {"sessions": {"allow_full_access": True}}
    for word in (None, "read-only", "workspace", "full"):
        with pytest.raises(ValueError) as e:
            codex_params(senv, f"on-{word}", runner=runner, access_word=word, **on)
        assert str(e.value) == access.CODEX_UNSUPPORTED.format(flag=flag), word
    assert access.CODEX_SANDBOX_OFF == {"--dangerously-bypass-approvals-and-sandbox", "--yolo"}


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
    sid = start(senv)["session_id"]
    assert child(senv, sid, expect=409)["message"].startswith(f"the access level of session {sid} is not known yet")
    tick(senv)
    assert [body.get("access") for path, body in spy.posts if path.endswith(sid) and "state" in body] == ["workspace"]
    assert stored(senv, sid)["access"] == "workspace", "reported when it was opened, from its command line"
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
    assert [stored(senv, x).get("granted") for x in (ro, ws, cdx)] == ["read-only", "workspace", "read-only"], \
        "what each was started with, reported when it was opened"
    begin(senv, ro, tools=("Read", "Grep", "Glob", "mcp__agent-comms__board_post"))
    begin(senv, ws)
    assert [show(senv, x)["state"] for x in (ro, ws)] == ["running", "running"], "and their processes agree"
    assert all(set(body) <= {"events", "seq", "state", "pid", "resume", "provider_id", "access", "ceiling", "checkout",
                             "modes",              # what the session's adapter can do with a turn in flight
                             "shutdown_bound"}     # (spawn review round 5, S25: how long the node may take to stop one)
               for _, body in spy.posts), "words, and where the root runs"
    roots = [body for path, body in spy.posts if path.endswith(sid) and "state" in body]
    assert all(b["ceiling"] == "workspace" and b["checkout"] == str(senv.work) for b in roots)
    assert not any("ceiling" in body or "checkout" in body for path, body in spy.posts if not path.endswith(sid)), \
        "a child has no ceiling: nothing is started under it"


def test_a_full_child_exists_only_under_a_full_root_on_a_node_that_allows_it(senv):
    cfg = tree_config(senv, runners={"claude-code": BYPASS}, sessions={"allow_full_access": True})
    node = make_node(senv, config=cfg, log_dir=senv.tmp / "full-logs")
    node.hello()
    sid = root(senv, node=node)
    assert stored(senv, sid)["access"] == "full" and "--dangerously-skip-permissions" in call_for(senv, sid, node)["argv"]
    assert "--settings" not in call_for(senv, sid, node)["argv"], "a full launch: bypass is not switched off in it"
    answer(senv, sid, node=node)                         # its process says it runs bypassed: now it counts
    full = child(senv, sid)                              # the default: its parent's
    ro = child(senv, sid, access="read-only")
    out = child(senv, sid, access="workspace", expect=409)      # this node cannot say that word for this runner
    assert out["message"].startswith("node laptop cannot start a session of claude at workspace access (it can at: "
                                     "read-only, full)"), "refused when it is asked for, not made and then ended"
    cdx = child(senv, sid, agent="codex", access="full")
    tick(senv, node)
    assert full["access"] == "full" and "--dangerously-skip-permissions" in call_for(senv, full["session_id"], node)["argv"]
    ro_argv = call_for(senv, ro["session_id"], node)["argv"]
    assert value_after(ro_argv, "--permission-mode") == "dontAsk" and "--dangerously-skip-permissions" not in ro_argv
    codex_child = codex_proc(senv, node)
    codex_child.emit(cx.reply(1, cx.INIT_RESULT))
    tick(senv, node)
    assert written(codex_child)[2]["params"]["sandbox"] == "danger-full-access"
    codex_child.emit(cx.reply(2, cx.THREAD_RESULT | {"sandbox": {"type": "dangerFullAccess"}}))
    tick(senv, node)
    assert stored(senv, cdx["session_id"])["granted"] == "full" and show(senv, cdx["session_id"])["state"] != "ended"

    # The same hub records, a node that does NOT allow full access: every full session is refused, loudly.
    strict = make_node(senv, config=tree_config(senv, runners={"claude-code": BYPASS}), log_dir=senv.tmp / "strict-logs")
    tick(senv, strict, times=3)
    assert show(senv, sid)["state"] == "ended" and "cannot be hosted as a session" in show(senv, sid)["error"]
    assert all(show(senv, x["session_id"])["state"] == "ended" for x in (full, ro, cdx)), "and its children with it"


def test_full_is_refused_when_it_is_asked_of_a_node_that_does_not_offer_it(senv):
    """S8. The node's hello says at which words a child of each runner can be started; `full` is among them only
    where the node allows full access. The hub refuses the rest when the child is admitted."""
    sid = root(senv)
    # The hub's own record says the parent is full (as a node that allowed it would have reported).
    senv.laptop.post(f"/api/nodes/sessions/{sid}", json={"events": [], "state": "idle", "access": "full",
                                                         "ceiling": "full"})
    out = child(senv, sid, access="full", expect=409)
    assert out["message"].startswith("node laptop cannot start a session of claude at full access (it can at: "
                                     "read-only, workspace)") and "allow_full_access" in out["message"]
    assert "children" not in show(senv, sid) and len(children(senv)) == 1, "nothing was made or started"
    assert child(senv, sid, access="workspace")["access"] == "workspace"


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
    with pytest.raises(Conflict, match="not a linked worktree of this checkout"):
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
    tick(senv)
    answer(senv, sid)                   # the root's turn with its child's result (hosted_results.py)
    tid = senv.thread()
    ask(senv, tid, as_="grok", to=["claude"])
    ask(senv, tid, as_="grok", to=["codex"])
    tick(senv, times=2)
    hosted_wake.tick(senv.board)
    assert [m["kind"] for m in show(senv, sid)["messages"]] == ["user", "child", "wake"], "the root is woken"
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
    assert msg(senv, sid, 2)["kind"] == "child"
    answer(senv, sid)                   # the root's turn with that child's end (hosted_results.py)
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


def test_a_read_mark_no_longer_holds_once_a_child_of_the_agent_is_around(senv):
    """S7. Also a mark from BEFORE the child existed: from the wake to now, a child of the agent in the project
    shadows marks and receipts alike (the child may be the one that goes on with the request)."""
    sid, post = woken_root(senv)
    senv.board.read_updates(senv.p["claude"], senv.session("claude"), only="addressed")     # the root reads it
    assert hosted.holds(senv.board, "claude", PROJECT, post["id"], senv.clock())
    later(senv, 5)
    kid = child(senv, sid, "later")["session_id"]
    assert not hosted.holds(senv.board, "claude", PROJECT, post["id"], senv.clock()), "a child started after the mark"
    close(senv, kid)
    assert not hosted.holds(senv.board, "claude", PROJECT, post["id"], senv.clock()), "it was around since the wake"


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
    # The board tools reach hosted sessions only through hosted_spawn, which checks who is calling (an agent's own
    # children only: tests/test_hosted_spawn.py); nothing else of the session host is imported there.
    assert set(re.findall(r"\bhosted\w*", source)) <= {"hosted_spawn"}
    assert "session_access" not in source and "host.session" not in source
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
    with pytest.raises(SystemExit) as e:        # one positional with --parent: argparse says the second is too many
        cli.main(["session", "start", PROJECT, "hello", "--parent", UNKNOWN])
    assert e.value.code == 2 and "unrecognized arguments: hello" in capsys.readouterr().err
    with pytest.raises(SystemExit) as e:        # two without it
        cli.main(["session", "start", "hello"])
    assert e.value.code == 2 and "required: message" in capsys.readouterr().err
    with pytest.raises(SystemExit, match="written in full"):
        cli.main(["session", "start", PROJECT, "hello", "--paren", UNKNOWN])
    with pytest.raises(SystemExit, match="--access, --role apply to a child session only"):
        cli.main(["session", "start", PROJECT, "hello", "--access", "read-only", "--role", "test"])
    with pytest.raises(SystemExit, match="no such hosted session"):
        cli.main(["session", "start", "--parent", UNKNOWN, "hello"])
    with pytest.raises(SystemExit):
        cli.main(["session", "start", "--parent", UNKNOWN, "--access", "root", "hello"])
    assert clienv.requests[-1][1].endswith(f"/api/hosted-sessions/{UNKNOWN}/children")


# ------------------------------------------------------------ review round 1


def test_a_child_is_not_resumed_above_what_its_parent_has_on_the_node_now(senv):
    """S1. The hub's stored words say what was true when the child was made. The node's owner has since made the
    parent's runner read-only and restarted the node: a workspace child may not go on under that parent."""
    sid = root(senv)
    answer(senv, sid)
    kid = child(senv, sid, "work", agent="codex")["session_id"]      # workspace: its Codex runner is workspace-write
    tick(senv)
    assert codex_call(senv)["cwd"] == str(senv.work) and stored(senv, kid)["granted"] == "workspace"
    hard = ["claude", "-p", "{prompt}", "--permission-mode", "dontAsk", "--tools=Read,Grep,Glob"]
    edited = make_node(senv, config=tree_config(senv, runners={"claude-code": hard}), log_dir=senv.tmp / "laptop-logs")
    send(senv, kid, "more")
    tick(senv, edited, times=2)
    assert edited.sessions.host.sessions[sid].access == "read-only", "the parent as this machine's runner has it now"
    view = show(senv, kid)
    assert view["state"] == "ended" and view["error"] == ABOVE_PARENT.format(have="workspace", limit="read-only")
    assert msg(senv, kid, 2)["state"] == "failed"
    assert [c for c in edited.session_spawner.calls if c["argv"][:2] == ["codex", "app-server"]] == [], \
        "no process was started for it"
    assert show(senv, sid)["state"] != "ended", "its parent goes on, read-only"


def test_a_queued_child_does_not_start_in_the_pass_its_parent_cannot_be_reopened(senv):
    """S1. Roots are reconciled first, and a child is opened only under a parent that is open on the node."""
    sid = root(senv)
    answer(senv, sid)
    kid = child(senv, sid, "work", agent="codex")["session_id"]      # queued: no node has seen it yet
    broken = make_node(senv, config=tree_config(senv, runners={"claude-code": BYPASS}),
                       log_dir=senv.tmp / "laptop-logs")              # the parent's runner can no longer be hosted here
    tick(senv, broken)                                               # ONE pass
    assert show(senv, sid)["state"] == "ended" and "cannot be hosted as a session" in show(senv, sid)["error"]
    view = show(senv, kid)
    assert (view["state"], view["error"]) == ("ended", PARENT_GONE) and msg(senv, kid, 1)["state"] == "failed"
    assert broken.session_spawner.calls == [], "nothing was started, in that pass or after"
    tick(senv, broken, times=2)
    assert broken.session_spawner.calls == []


def test_a_child_takes_no_turn_once_its_parent_has_less_access_here(senv):
    """S1. Checked again before every turn: the parent's access here can change while the child is open."""
    sid = root(senv)
    answer(senv, sid)
    kid = child(senv, sid, "one")["session_id"]
    tick(senv)
    answer(senv, kid)
    senv.node.sessions.host.sessions[sid].access = "read-only"      # what its process reported at a later turn
    send(senv, kid, "two")
    tick(senv, times=2)
    view = show(senv, kid)
    assert view["state"] == "ended" and view["error"] == ABOVE_PARENT.format(have="workspace", limit="read-only")
    proc = senv.node.session_spawner.children[[i for i, c in enumerate(senv.node.session_spawner.calls)
                                               if kid in c["argv"]][-1]]
    assert proc.turns() == ["one"] and proc.terminated == 1, "the second message was never written"


def test_the_parent_is_checked_again_at_the_moment_a_turn_is_handed_over(senv):
    """S1. Between the fetch and the hand-over the parent may have gone, or reported less access (its turn started):
    the message is then not handed to the child, and the child is ended."""
    sid = root(senv)
    answer(senv, sid)
    kid = child(senv, sid, "one")["session_id"]
    tick(senv)
    answer(senv, kid)
    send(senv, kid, "two")
    runner = senv.node.sessions
    runner._reconcile(runner._sync(0))                              # the fetch: all is well, the message is listed
    assert [m["n"] for m in runner.queue[kid]] == [2]
    runner.host.sessions[sid].access = "read-only"                  # ... and then the parent's turn starts
    runner._hand_over(senv.clock())
    assert kid not in runner.queue and runner.host.sessions[kid].ended and not runner.host.sessions[kid].inbox
    assert [(x, e["kind"], e.get("detail")) for x, e in runner.pending if x == kid][-1] == (
        kid, "ended", ABOVE_PARENT.format(have="workspace", limit="read-only"))
    tick(senv, times=2)
    assert show(senv, kid)["state"] == "ended" and msg(senv, kid, 2)["state"] == "failed"


def test_verify_is_a_kill_switch_for_what_a_process_reports():
    """S3. session_access.verify(asked, launched, reported mode, reported tools) -> why the session is ended, or
    None. It decides nothing about what the session's access is."""
    v = access.verify
    board = ("mcp__agent-comms__board_post", "mcp__agent-comms__board_read_updates")
    # Nothing to object to: a session started in a mode that is not bypass, saying so.
    for asked in (None, "workspace"):
        for mode in ("default", "dontAsk", "acceptEdits", "plan", "auto"):
            assert v(asked, "workspace", mode, ("Bash", "Read")) is None
        assert v(asked, "read-only", "dontAsk", ("Bash",)) is None, "a root's own hard list is not ours to police"
    # bypassPermissions, and not started as full: ended, whoever asked for what.
    for asked, launched in ((None, "workspace"), (None, "read-only"), ("workspace", "workspace"),
                            ("read-only", "read-only")):
        assert v(asked, launched, "bypassPermissions", ("Read",)) == access.BYPASS_MODE, (asked, launched)
    assert v(None, "full", "bypassPermissions") is None and v("full", "full", "bypassPermissions") is None
    # started as full, and not bypassed: ended, not run narrower.
    assert v("full", "full", "default") == access.NOT_BYPASS.format(mode="default")
    assert v(None, "full", "dontAsk") == access.NOT_BYPASS.format(mode="dontAsk")
    # asked for read-only: dontAsk, tools reported, none beyond the hard list and the board's.
    assert v("read-only", "read-only", "dontAsk", ("Read", "Grep", "Glob", *board)) is None
    assert v("read-only", "read-only", "default", ("Read",)) == access.NOT_DONT_ASK.format(mode="default")
    for extra in ("Bash", "Write", "Task", "mcp__other__tool", "mcp__agent-comms-evil__x", "unknown"):
        assert extra in v("read-only", "read-only", "dontAsk", ("Read", extra)), extra
    assert v("read-only", "read-only", "dontAsk", ()) == access.UNVERIFIABLE.format(asked="read-only",
                                                                                     what="the tools it has")
    # no mode reported: accepted only where nothing depends on it.
    for mode in (None, "", {"not": "a name"}, "two words"):
        assert v(None, "workspace", mode) is None and v("workspace", "workspace", mode) is None
        assert v(None, "full", mode) is None, "a root started as full claims the most there is"
        for asked in ("read-only", "full"):
            assert v(asked, asked, mode, ("Read",)) == access.UNVERIFIABLE.format(
                asked=asked, what="the permission mode it runs in"), (asked, mode)


def test_a_session_that_reports_bypass_without_being_started_as_full_is_ended(senv):
    """S3, the kill switch. Its command line names a mode (pinned: the runner names none), so no settings file
    should be able to put it into bypassPermissions. If its process reports that mode all the same, it is ended."""
    plain = ["claude", "-p", "{prompt}", "--model", "haiku"]
    node = make_node(senv, config=tree_config(senv, runners={"claude-code": plain}), log_dir=senv.tmp / "plain-logs")
    sid = start(senv)["session_id"]
    tick(senv, node)
    argv = call_for(senv, sid, node)["argv"]
    assert value_after(argv, "--permission-mode") == "manual" and not any("bypass" in a or "dangerously" in a for a in argv)
    assert stored(senv, sid)["access"] == "workspace"
    begin(senv, sid, node, mode="bypassPermissions", tools=("Bash", "Write", "Read"))
    tick(senv, node, times=2)
    view = show(senv, sid)
    assert view["state"] == "ended" and view["error"] == access.BYPASS_MODE[:300]
    assert "not started as a full-access session" in view["error"]
    assert msg(senv, sid, 1)["state"] == "failed" and children(senv, node)[0].terminated == 1
    assert node.sessions.host.sessions == {}
    # ... also on a node that allows full access: this session was not started as one.
    allowing = make_node(senv, config=tree_config(senv, runners={"claude-code": plain},
                                                  sessions={"allow_full_access": True}), log_dir=senv.tmp / "allow-logs")
    other = start(senv, project=P2)["session_id"]
    tick(senv, allowing)
    begin(senv, other, allowing, mode="bypassPermissions")
    tick(senv, allowing, times=2)
    assert show(senv, other)["state"] == "ended" and show(senv, other)["error"] == access.BYPASS_MODE[:300]


def test_a_child_whose_process_does_not_run_as_asked_is_ended(senv):
    """S3, the kill switch. A read-only child not in dontAsk, with a tool beyond the hard list, or that cannot be
    verified; a workspace child that reports bypass; a full child that is not bypassed or cannot be verified: each
    is ended. A workspace child that reports no mode at all is not: nothing depends on it."""
    node = make_node(senv, config=tree_config(senv, sessions={"allow_full_access": True}), log_dir=senv.tmp / "v-logs")
    node.hello()
    sid = root(senv, node=node)
    answer(senv, sid, node=node)
    cases = [("read-only", "default", ("Read",), "reports permission mode default and not dontAsk"),
             ("read-only", "dontAsk", ("Read", "Bash"), "tools beyond the read tools and the board's (Bash)"),
             ("read-only", None, ("Read",), "did not report the permission mode it runs in"),
             ("read-only", "dontAsk", (), "did not report the tools it has"),
             ("workspace", "bypassPermissions", ("Read",), "not started as a full-access session")]
    for word, mode, tools, why in cases:
        kid = child(senv, sid, "go", access=word)["session_id"]
        tick(senv, node)
        begin(senv, kid, node, mode=mode, tools=tools)
        tick(senv, node, times=2)
        view = show(senv, kid)
        assert view["state"] == "ended" and why in view["error"], (word, mode, view["error"])
        assert msg(senv, kid, 1)["state"] == "failed"
    quiet = child(senv, sid, "go", access="workspace")["session_id"]
    tick(senv, node)
    begin(senv, quiet, node, mode=None)
    assert show(senv, quiet)["state"] == "running", "a workspace session with a named mode needs no report"
    assert show(senv, sid)["state"] != "ended"

    full_node = make_node(senv, config=tree_config(senv, runners={"claude-code": BYPASS},
                                                   sessions={"allow_full_access": True}), log_dir=senv.tmp / "f-logs")
    full_node.hello()
    top = root(senv, node=full_node, project=P2)
    answer(senv, top, node=full_node)
    for mode, why in (("dontAsk", access.NOT_BYPASS.format(mode="dontAsk")),
                      (None, access.UNVERIFIABLE.format(asked="full", what="the permission mode it runs in"))):
        kid = child(senv, top, "go")["session_id"]
        assert stored(senv, kid)["access"] == "full"
        tick(senv, full_node)
        begin(senv, kid, full_node, mode=mode)
        tick(senv, full_node, times=2)
        assert show(senv, kid)["state"] == "ended" and show(senv, kid)["error"] == why[:300]
    ok = child(senv, top, "go")["session_id"]
    tick(senv, full_node)
    begin(senv, ok, full_node)
    assert show(senv, ok)["state"] == "running" and show(senv, top)["state"] != "ended"
    senv.node.hello()


def test_a_full_child_waits_after_a_restart_until_its_parents_process_has_confirmed_full_access(senv):
    """S1 (round 3). After a node restart the parent's access is there at once, computed from its command line, so
    its children are not ended. But `full` counts for a child only once the parent's process has said, in THIS node
    process, that it runs bypassed: until then a full child is held (open, its message queued, no turn)."""
    config = lambda: tree_config(senv, runners={"claude-code": BYPASS}, sessions={"allow_full_access": True})  # noqa: E731
    node = make_node(senv, config=config(), log_dir=senv.tmp / "r-logs")
    node.hello()
    sid = root(senv, node=node)
    answer(senv, sid, node=node)
    kid = child(senv, sid, "one")["session_id"]
    tick(senv, node)
    answer(senv, kid, node=node)
    claimed(senv, sid)
    again = make_node(senv, config=config(), log_dir=senv.tmp / "r-logs")       # the node is restarted
    tick(senv, again)
    parent = again.sessions.host.sessions[sid]
    assert (parent.access, parent.child, parent.ceiling) == ("full", None, "workspace"), \
        "no process, nothing reported: its access is known, and nothing has confirmed it"
    assert show(senv, sid)["ceiling"] == "workspace" and "has not confirmed" in child(senv, sid, expect=409)["message"]
    send(senv, kid, "two")
    tick(senv, again, times=3)
    assert show(senv, kid)["state"] != "ended" and msg(senv, kid, 2)["state"] == "queued", "held, not ended"
    assert again.session_spawner.calls == [], "nothing was started for it"
    send(senv, sid, "wake up")                                                  # the parent's next turn
    tick(senv, again)
    assert msg(senv, kid, 2)["state"] == "queued", "its process is up, and has said nothing yet"
    begin(senv, sid, again)
    assert again.sessions.host.sessions[sid].ceiling == "full" and show(senv, sid)["ceiling"] == "full"
    tick(senv, again)
    assert msg(senv, kid, 2)["state"] == "delivered", "now the child's turn is handed over"
    senv.node.hello()


def claimed(env, parent) -> None:
    """Take every result waiting for this parent (hosted_results.claim), so that none becomes a turn of its own: for
    the tests below, which are about the parent's process and count its turns."""
    for _ in range(hosted_results.MAX_WAITING + 1):
        if hosted_results.claim(env.board, parent, "test") is None:
            return
    raise AssertionError("a claimed result is still waiting")


def full_node(env, name, **sessions):
    node = make_node(env, config=tree_config(env, runners={"claude-code": BYPASS},
                                             sessions={"allow_full_access": True} | sessions),
                     log_dir=env.tmp / f"{name}-logs")
    node.hello()
    return node


def test_full_counts_for_children_only_while_the_process_that_confirmed_it_is_alive(senv):
    """S1 (rounds 3 and 4). What a session was started as is not yet what its CLI accepted. A root labelled `full`
    gives its children at most `workspace` until its process reports bypassPermissions, and the confirmation is
    THAT process's: with no process, or a new one that has said nothing, the ceiling is `workspace` again. It is
    read off the parent's process at the moment a child is considered, so a parent that is started in a pass never
    lends its earlier process's word to a child in that pass."""
    node = full_node(senv, "c")
    sid = root(senv, node=node)
    host = node.sessions.host
    assert (host.sessions[sid].access, host.sessions[sid].ceiling) == ("full", "workspace")
    view = show(senv, sid)
    assert (view["access"], view["ceiling"]) == ("full", "workspace")
    assert cli._fmt_access(view) == ("full (not confirmed by a live process of its own: a child can have at most "
                                     "workspace)")
    for word in (None, "full"):
        out = child(senv, sid, expect=409, **({"access": word} if word else {}))
        assert "was started as a full-access session, but its process has not confirmed that" in out["message"]
    assert "children" not in show(senv, sid), "nothing was made"
    begin(senv, sid, node)                               # `init`: bypassPermissions
    assert host.sessions[sid].ceiling == "full" and show(senv, sid)["ceiling"] == "full"
    assert cli._fmt_access(show(senv, sid)) == "full"
    kid = child(senv, sid, "one")["session_id"]
    proc_of(senv, sid, node)[1].emit(result(sid))
    tick(senv, node, times=2)
    answer(senv, kid, node=node)
    claimed(senv, sid)
    assert msg(senv, kid, 1)["state"] == "done"
    # The parent's process is stopped (as the idle limit would): the word was that process's, and it is gone.
    assert host.park(sid)
    tick(senv, node, times=2)
    assert host.sessions[sid].child is None and host.sessions[sid].ceiling == "workspace"
    assert show(senv, sid)["ceiling"] == "workspace"
    assert "has not confirmed" in child(senv, sid, expect=409)["message"]
    # The parent has no process and a queued message; its full child has a queued message. ONE pass: the parent's
    # turn goes out (a new process), the child's does not.
    send(senv, kid, "two")
    send(senv, sid, "again")
    kid_proc = proc_of(senv, kid, node)[1]
    tick(senv, node)
    assert msg(senv, sid, 2)["state"] == "delivered" and msg(senv, kid, 2)["state"] == "queued"
    assert host.sessions[sid].child is not None and host.sessions[sid].ceiling == "workspace"
    assert len(written(kid_proc)) == 1 and not host.sessions[kid].inbox, "nothing was handed to the child, let alone written"
    tick(senv, node, times=3)
    assert msg(senv, kid, 2)["state"] == "queued" and show(senv, kid)["state"] != "ended", "held, not ended"
    begin(senv, sid, node)                               # the NEW process says it runs bypassed
    tick(senv, node)
    assert msg(senv, kid, 2)["state"] == "delivered" and len(written(kid_proc)) == 2
    # A process that has exited and is not yet reaped lends nothing either.
    parent_proc = proc_of(senv, sid, node)[1]
    assert host.sessions[sid].ceiling == "full"
    parent_proc.code = 1
    assert host.sessions[sid].ceiling == "workspace"
    parent_proc.code = None
    # A parent whose new process turns out NOT to run bypassed is ended (the kill switch), its children with it.
    parent_proc.emit(result(sid))
    tick(senv, node, times=2)
    assert host.park(sid)
    tick(senv, node, times=2)
    send(senv, sid, "once more")
    tick(senv, node)
    begin(senv, sid, node, mode="default")
    tick(senv, node, times=3)
    assert show(senv, sid)["state"] == "ended" and show(senv, sid)["error"] == access.NOT_BYPASS.format(mode="default")
    assert show(senv, kid)["state"] == "ended"
    senv.node.hello()


def test_a_full_childs_turn_is_not_written_once_its_parents_process_is_gone(senv):
    """S1b (round 4). A message is handed to a child under a parent that allows it, but it can wait in the child's
    inbox for a handshake. If the parent's process goes in that time, the turn is not written: the host asks again
    at the moment of writing (`turn_gate`)."""
    node = full_node(senv, "g")
    sid = root(senv, node=node)
    answer(senv, sid, node=node)                         # live, and it said bypassPermissions
    kid = child(senv, sid, "count", agent="codex", access="full")["session_id"]
    tick(senv, node)
    host, proc = node.sessions.host, codex_proc(senv, node)
    assert [m.id for m in host.sessions[kid].inbox] == ["1"], "handed over: its parent allowed it; Codex is starting"
    proc_of(senv, sid, node)[1].code = 1                 # the parent's process dies
    proc.emit(cx.reply(1, cx.INIT_RESULT))
    tick(senv, node)
    proc.emit(cx.reply(2, cx.THREAD_RESULT | {"sandbox": {"type": "dangerFullAccess"}}))
    tick(senv, node, times=3)
    assert host.sessions[sid].child is None and host.sessions[kid].ready
    assert not any(m.get("method") == "turn/start" for m in written(proc)), "ready, and still nothing is written"
    assert msg(senv, kid, 1)["state"] == "queued" and show(senv, kid)["state"] != "ended"
    send(senv, sid, "again")
    tick(senv, node)
    assert not any(m.get("method") == "turn/start" for m in written(proc)), "the parent's new process has said nothing"
    begin(senv, sid, node)
    tick(senv, node)
    assert [m["params"]["input"][0]["text"] for m in written(proc) if m.get("method") == "turn/start"] == ["count"]
    assert msg(senv, kid, 1)["state"] == "delivered"
    senv.node.hello()


def test_a_message_held_in_a_full_childs_inbox_does_not_keep_the_node_busy(senv):
    """R4, the inbox case. A message handed to a full child that then waits in its inbox, because the parent's
    process went during the child's handshake, used to count as activity until the parent's next turn: the node
    kept its short cadence all that time. It counts while the child's process is in its handshake (that needs the
    passes), not once the child only waits; the parent's next turn brings the cadence back and the turn goes."""
    node = full_node(senv, "h")
    runner = node.sessions
    sid = root(senv, node=node)
    answer(senv, sid, node=node)
    kid = child(senv, sid, "count", agent="codex", access="full")["session_id"]
    tick(senv, node)
    host, proc = runner.host, codex_proc(senv, node)
    proc_of(senv, sid, node)[1].code = 1                 # the parent's process dies during the child's handshake
    proc.emit(cx.reply(1, cx.INIT_RESULT))
    tick(senv, node)
    assert [m.id for m in host.sessions[kid].inbox] == ["1"] and not host.sessions[kid].ready
    assert runner.active(), "a handshake under way is work"
    proc.emit(cx.reply(2, cx.THREAD_RESULT | {"sandbox": {"type": "dangerFullAccess"}}))
    tick(senv, node, times=3)
    assert host.sessions[kid].ready and [m.id for m in host.sessions[kid].inbox] == ["1"]
    assert not runner.active(), "held in its inbox: nothing to do until the parent has a turn"
    send(senv, sid, "again")
    tick(senv, node)
    assert runner.active()
    begin(senv, sid, node)
    tick(senv, node)
    assert [m["params"]["input"][0]["text"] for m in written(proc) if m.get("method") == "turn/start"] == ["count"]
    assert msg(senv, kid, 1)["state"] == "delivered" and runner.active()
    senv.node.hello()


def test_a_parent_whose_process_a_full_child_waits_on_is_not_stopped_to_make_room(senv):
    """S1b (round 4). With the confirmation tied to the parent's live process, stopping that process to make room
    for its own full child would hold the child until the parent's next turn. The node stops another idle process
    instead, though the parent has been idle longest."""
    node = full_node(senv, "m", max_live=2)
    sid = root(senv, node=node)
    answer(senv, sid, node=node)
    first = child(senv, sid, "one")["session_id"]
    tick(senv, node)
    answer(senv, first, node=node)                       # two processes are up, both idle; the parent the longer
    second = child(senv, sid, "two")["session_id"]
    tick(senv, node, times=4)
    parent_proc, first_proc = proc_of(senv, sid, node)[1], proc_of(senv, first, node)[1]
    assert (parent_proc.terminated, first_proc.terminated) == (0, 1), "the other child made room, not the parent"
    assert msg(senv, second, 1)["state"] == "delivered" and node.sessions.host.sessions[sid].ceiling == "full"
    senv.node.hello()


def test_codex_confirms_full_access_with_its_answer_to_thread_start(senv):
    """S1 (round 3). The node itself sends `danger-full-access` as the thread's sandbox; Codex's answer names the
    sandbox the thread has, and that is the confirmation (no turn is needed). An answer that names another sandbox
    ends a session started as full; one that names dangerFullAccess ends a session that was not."""
    v = access.verify_codex
    assert v("full", "dangerFullAccess") == (None, True)
    assert v("full", None) == (None, False), "an answer that names no sandbox confirms nothing (and ends nothing)"
    assert v("full", "workspaceWrite") == (access.CODEX_NOT_FULL.format(sandbox="workspaceWrite"), False)
    assert v("workspace", "workspaceWrite") == (None, False) == v("read-only", "readOnly") == v("workspace", None)
    assert v("workspace", "dangerFullAccess") == (access.CODEX_FULL, False) == v(None, "dangerFullAccess")
    p = CodexSession()
    p.argv(CODEX_RUNNER, session_id=UNKNOWN, resume=False, model=None)
    p.on_open()
    p.feed(cx.reply(1, cx.INIT_RESULT))
    [ready], _ = p.feed(cx.reply(2, cx.THREAD_RESULT | {"sandbox": {"type": "dangerFullAccess"}}))
    assert (ready.kind, ready.mode) == ("ready", "dangerFullAccess") and CodexSession.reports_sandbox

    full_codex = ["codex", "exec", "--cd", "{project}", "--sandbox", "danger-full-access", "{prompt}"]
    node = make_node(senv, config=tree_config(senv, runners={"codex-cli": full_codex},
                                              sessions={"allow_full_access": True}), log_dir=senv.tmp / "cx-logs")
    node.hello()
    sid = root(senv, node=node, agent="codex")
    assert stored(senv, sid)["access"] == "full" and show(senv, sid)["ceiling"] == "workspace"
    proc = codex_proc(senv, node)
    proc.emit(cx.reply(1, cx.INIT_RESULT))
    tick(senv, node)
    assert written(proc)[2]["params"]["sandbox"] == "danger-full-access"
    proc.emit(cx.reply(2, cx.THREAD_RESULT | {"sandbox": {"type": "dangerFullAccess"}}))
    tick(senv, node)
    assert node.sessions.host.sessions[sid].ceiling == "full" and show(senv, sid)["ceiling"] == "full"
    assert child(senv, sid, agent="claude")["access"] == "full", "a full child, now that Codex has said so"

    # The same root on a restarted node, and this time Codex's answer names NO sandbox: the session runs on (the
    # node itself sent the parameter), but nothing has confirmed it: no child may be full under it.
    again = make_node(senv, config=tree_config(senv, runners={"codex-cli": full_codex},
                                               sessions={"allow_full_access": True}), log_dir=senv.tmp / "cx-logs")
    send(senv, sid, "more")
    tick(senv, again)
    proc = codex_proc(senv, again)
    proc.emit(cx.reply(1, cx.INIT_RESULT))
    tick(senv, again)
    proc.emit(cx.reply(2, {k: v for k, v in cx.THREAD_RESULT.items() if k != "sandbox"}))
    tick(senv, again, times=2)
    assert show(senv, sid)["state"] != "ended" and again.sessions.host.sessions[sid].ready
    assert again.sessions.host.sessions[sid].ceiling == "workspace" and show(senv, sid)["ceiling"] == "workspace"
    assert "has not confirmed" in child(senv, sid, agent="claude", expect=409)["message"]

    # ... and on a node restarted once more Codex answers with another sandbox: ended, not run narrower.
    again = make_node(senv, config=tree_config(senv, runners={"codex-cli": full_codex},
                                               sessions={"allow_full_access": True}), log_dir=senv.tmp / "cx-logs")
    send(senv, sid, "and more")
    tick(senv, again)
    proc = codex_proc(senv, again)
    proc.emit(cx.reply(1, cx.INIT_RESULT))
    tick(senv, again)
    proc.emit(cx.reply(2, cx.THREAD_RESULT | {"sandbox": {"type": "workspaceWrite"}}))
    tick(senv, again, times=3)
    assert show(senv, sid)["state"] == "ended"
    assert show(senv, sid)["error"] == access.CODEX_NOT_FULL.format(sandbox="workspaceWrite")
    senv.node.hello()


def test_a_launch_claude_would_refuse_is_refused_when_the_session_is_opened(senv, tmp_path):
    """S1a (round 3). The label is computed from the command line, so a command line Claude will not run as
    labelled must not be opened at all. Seen live on 2.1.322: `--restricted` beside the bypass mode or either skip
    flag exits with "bypassPermissions not supported in restricted mode"; a launch in the bypass mode whose settings
    switch it off runs in `default`."""
    from agent_comms.session_host import child_words

    on = {"sessions": {"allow_full_access": True}}
    for extra in (["--permission-mode", "bypassPermissions"], ["--permission-mode=bypassPermissions"],
                  ["--dangerously-skip-permissions"], ["--allow-dangerously-skip-permissions"]):
        runner = ["claude", "-p", "{prompt}", "--restricted"] + extra
        assert access.claude_conflict(runner) == access.RESTRICTED_BYPASS
        with pytest.raises(ValueError) as e:
            claude_argv(senv, "restricted-" + extra[0].strip("-")[:9] + str(len(extra)), runner=runner, **on)
        assert str(e.value) == access.RESTRICTED_BYPASS and "not supported in restricted mode" in str(e.value)
    restricted = SHIPPED + ["--restricted"]
    assert access.claude_conflict(restricted) is None and claude_argv(senv, "restricted-ok", runner=restricted)[1] == "workspace"
    with pytest.raises(ValueError) as e:        # a child asked for at full from it: the same refusal
        claude_argv(senv, "restricted-full", runner=restricted, access_word="full", **on)
    assert str(e.value) == access.RESTRICTED_BYPASS
    assert child_words(tree_config(senv, **on), restricted) == ["read-only", "workspace"], "so the hub is never told `full`"
    assert child_words(tree_config(senv, **on), SHIPPED) == ["read-only", "workspace", "full"]

    off = tmp_path / "no-bypass.json"
    off.write_text(json.dumps({"permissions": {"disableBypassPermissionsMode": "disable", "allow": ["Read"]}}))
    assert access.claude_conflict(BYPASS + ["--settings", access.NO_BYPASS_JSON]) == access.FULL_DISABLED
    assert access.claude_conflict(BYPASS + ["--settings", str(off)]) == access.FULL_DISABLED
    assert access.claude_conflict(BYPASS + [f"--settings={off}", "--settings", '{"env": {}}']) is None, "the LAST one counts"
    with pytest.raises(ValueError) as e:
        claude_argv(senv, "disabled", runner=BYPASS + ["--settings", str(off)], **on)
    assert str(e.value) == access.FULL_DISABLED
    with pytest.raises(ValueError) as e:
        claude_argv(senv, "disabled-child", runner=SHIPPED + ["--settings", str(off)], access_word="full", **on)
    assert str(e.value) == access.FULL_DISABLED
    assert child_words(tree_config(senv, **on), SHIPPED + ["--settings", str(off)]) == ["read-only", "workspace"]
    fine = tmp_path / "fine.json"
    fine.write_text(json.dumps({"permissions": {"allow": ["Read"]}}))
    argv, label = claude_argv(senv, "full-own-settings", runner=BYPASS + ["--settings", str(fine)], **on)
    assert label == "full" and value_after(argv, "--settings") == str(fine), "a full launch keeps its settings as they are"


def merged_host(env, name, runner, **over):
    host = host_for(env, name, runners={"claude-code": runner}, **over)
    opened = host.open(UNKNOWN, agent="claude", runtime="claude-code", project=PROJECT)
    return host, opened


def test_bypass_is_switched_off_for_every_claude_launch_that_is_not_full(senv, tmp_path):
    """S3 (round 3). `permissions.disableBypassPermissionsMode = "disable"`, given with `--settings`. A runner with
    no settings of its own gets the key as JSON on its command line. Claude takes only its LAST `--settings`, so a
    runner that names one gets a file this node writes: the runner's settings and the key."""
    assert json.loads(access.NO_BYPASS_JSON) == {"permissions": {"disableBypassPermissionsMode": "disable"}}
    assert " " not in access.NO_BYPASS_JSON, "one token"
    for word, runner in ((None, SHIPPED), ("workspace", SHIPPED), ("read-only", SHIPPED), ("read-only", BYPASS),
                         (None, TRIAGE), (None, HARD), (None, ["claude", "-p", "{prompt}"])):
        argv, label = claude_argv(senv, f"sealed-{word}-{len(runner)}", runner=runner, access_word=word,
                                  sessions={"allow_full_access": True})
        assert label != "full" and argv.count("--settings") == 1 and value_after(argv, "--settings") == access.NO_BYPASS_JSON
    for word, runner in ((None, BYPASS), ("full", SHIPPED), ("full", BYPASS)):
        argv, label = claude_argv(senv, f"open-{word}-{len(runner)}", runner=runner, access_word=word,
                                  sessions={"allow_full_access": True})
        assert label == "full" and "--settings" not in argv, "a full launch is the one that is left as it is"

    own = tmp_path / "own.json"
    content = {"permissions": {"allow": ["Bash(ls *)"], "defaultMode": "bypassPermissions"},
               "hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "true"}]}]}, "env": {"A": "b"}}
    own.write_text(json.dumps(content))
    first = tmp_path / "first.json"
    first.write_text(json.dumps({"permissions": {"allow": ["Write"]}}))
    runner = ["claude", "-p", "{prompt}", "--settings", str(first), "--model", "haiku", f"--settings={own}"]
    host, opened = merged_host(senv, "merged", runner)
    argv = host._argv(opened)
    assert opened.seal == "merged" and opened.access == "workspace"
    assert argv.count("--settings") == 1 and str(own) not in argv and str(first) not in argv
    assert not any(a.startswith("--settings=") for a in argv)
    path = Path(value_after(argv, "--settings"))
    assert path == host.sessions_dir / f"{UNKNOWN}.settings.json" and (path.stat().st_mode & 0o777) == 0o600
    assert json.loads(path.read_text()) == content | {"permissions": content["permissions"] | {
        "disableBypassPermissionsMode": "disable"}}, "the runner's LAST settings (the ones Claude takes), and the key"
    assert json.loads(own.read_text()) == content, "the runner's own file is not touched"
    assert argv[:argv.index("--settings")] == ["claude", "-p", "--model", "haiku", "--permission-mode", "manual"]
    host.close(UNKNOWN)
    host.forget(UNKNOWN)
    assert not path.exists(), "the file goes with the session"

    # A path relative to the run directory, and the JSON itself (which a configured runner cannot carry: the
    # settings refuse braces in a runner's arguments; the function takes it all the same).
    (senv.work / "team.json").write_text(json.dumps({"permissions": {"deny": ["WebFetch"]}}))
    host, opened = merged_host(senv, "merged-relative", ["claude", "-p", "{prompt}", "--settings", "team.json"])
    assert json.loads(Path(value_after(host._argv(opened), "--settings")).read_text())["permissions"] == {
        "deny": ["WebFetch"], "disableBypassPermissionsMode": "disable"}
    made = []
    sealed, how = access.claude_sealed(["claude", "-p", "--settings", '{"env": {"A": "b"}}'],
                                       write=lambda data: made.append(data) or "/written/here.json")
    assert (sealed, how) == (["claude", "-p", "--settings", "/written/here.json"], "merged")
    assert made == [{"env": {"A": "b"}, "permissions": {"disableBypassPermissionsMode": "disable"}}]
    # A dry run (the words a node offers) writes nothing: a placeholder stands for the file.
    assert access.claude_sealed(["claude", "-p", "--settings", str(own)]) == (
        ["claude", "-p", "--settings", access.DRY_SETTINGS], "merged")
    assert access.claude_sealed(["claude", "-p", "--settings", "team.json"])[1] == "merged", "no directory to read it in"
    # A runner whose settings cannot be read is not started: not without them, and not without the key.
    for bad in ("/x/none.json", "[1, 2]", '{"permissions": "all"}', '{"permissions": null}', '{"broken": '):
        if not bad.startswith("/"):
            (tmp_path / "bad.json").write_text(bad)
        runner = ["claude", "-p", "{prompt}", "--settings", bad if bad.startswith("/") else str(tmp_path / "bad.json")]
        host = host_for(senv, f"unreadable-{len(bad)}", runners={"claude-code": runner})
        with pytest.raises(ValueError, match="cannot be read as a JSON object"):
            host.open(UNKNOWN, agent="claude", runtime="claude-code", project=PROJECT)
        assert not host.sessions_dir.exists() or not list(host.sessions_dir.iterdir()), "nothing is left behind"


def sealed_line(request_id, effective) -> dict:
    return {"type": "control_response", "response": {"subtype": "success", "request_id": request_id,
                                                     "response": {"effective": effective, "sources": []}}}


def test_a_session_started_from_a_merged_settings_file_must_say_bypass_is_off(senv, tmp_path):
    """S3 (rounds 3 and 4). Claude ignores a settings file it finds invalid, whole and silently (seen live: one
    value of the wrong type beside the key, and the key was not in force). So a process started from a file this
    node merged is asked what is in force (`get_settings`, answered before any turn: seen live) and NOTHING else is
    written to it until it has said the key is: the question is a handshake. The session is ended if the key is
    not in force, or if no answer comes within the handshake's limit; no turn is ever written to, or recorded for,
    a process that has not answered. A session given the key as JSON is asked nothing."""
    p = ClaudeSession()
    p.argv(SHIPPED + SEAL, session_id=UNKNOWN, resume=False, model=None)
    assert p.on_open() == [], "the key as JSON: nothing to ask"
    full = ClaudeSession()
    full.configure(access="full")
    full.argv(BYPASS + ["--settings", "/x/own.json"], session_id=UNKNOWN, resume=False, model=None)
    assert full.on_open() == [], "a full launch keeps its own settings file and is asked nothing"
    p.argv(SHIPPED + ["--settings", "/x/merged.json"], session_id=UNKNOWN, resume=False, model=None)
    [asked] = p.on_open()
    request = json.loads(asked)
    assert request == {"type": "control_request", "request_id": "settings-9e8d7c6b-1", "request": {"subtype": "get_settings"}}
    assert asked.endswith(b"\n") and asked.count(b"\n") == 1
    off = {"permissions": {"disableBypassPermissionsMode": "disable", "allow": ["Read"]}}
    assert p.feed(json.dumps(sealed_line("another", off)).encode()) == ([], []), "not the answer to its question"
    assert p.feed(json.dumps(sealed_line("settings-9e8d7c6b-1", off)).encode()) == ([Event("sealed", ok=True)], [])
    assert p.feed(json.dumps(sealed_line("settings-9e8d7c6b-1", off)).encode()) == ([], []), "answered once"
    for effective in ({}, {"permissions": {}}, {"permissions": {"disableBypassPermissionsMode": "enable"}}, None, "x"):
        p.argv(SHIPPED + ["--settings", "/x/merged.json"], session_id=UNKNOWN, resume=False, model=None)
        rid = json.loads(p.on_open()[0])["request_id"]
        assert p.feed(json.dumps(sealed_line(rid, effective)).encode()) == ([Event("sealed", ok=False)], []), effective
    p.argv(SHIPPED + ["--settings", "/x/merged.json"], session_id=UNKNOWN, resume=False, model=None)
    rid = json.loads(p.on_open()[0])["request_id"]
    refused = {"type": "control_response", "response": {"subtype": "error", "request_id": rid, "error": "no"}}
    assert p.feed(json.dumps(refused).encode()) == ([Event("sealed", ok=False)], [])

    own = tmp_path / "own.json"
    own.write_text(json.dumps({"permissions": {"allow": ["Read"]}}))
    runner = ["claude", "-p", "{prompt}", "--permission-mode", "dontAsk", "--settings", str(own)]
    node = make_node(senv, config=tree_config(senv, runners={"claude-code": runner}), log_dir=senv.tmp / "m-logs")

    host = node.sessions.host

    def opened(project):
        sid = start(senv, project=project)["session_id"]
        tick(senv, node)
        call, proc = proc_of(senv, sid, node)
        [first] = written(proc)
        assert first["request"] == {"subtype": "get_settings"}, "the question, alone: the turn waits for the answer"
        assert host.sessions[sid].seal == "merged" and host.sessions[sid].checking
        assert show(senv, sid)["state"] == "starting" and msg(senv, sid, 1)["state"] == "queued"
        tick(senv, node, times=2)
        assert len(written(proc)) == 1 and msg(senv, sid, 1)["state"] == "queued", "and it goes on waiting"
        return sid, proc, first["request_id"]

    # It says the key is in force: now the turn is written. Later turns of the same process need no new answer.
    sid, proc, rid = opened(PROJECT)
    proc.emit(sealed_line(rid, off))
    tick(senv, node)
    assert [x.get("type") for x in written(proc)] == ["control_request", "user"] and not host.sessions[sid].checking
    assert msg(senv, sid, 1)["state"] == "delivered"
    proc.emit(began(sid), result(sid))
    tick(senv, node, times=2)
    assert show(senv, sid)["state"] == "idle" and msg(senv, sid, 1)["state"] == "done"
    send(senv, sid, "again")
    tick(senv, node)
    proc.emit(began(sid), result(sid))
    tick(senv, node, times=2)
    assert show(senv, sid)["state"] == "idle" and [x.get("type") for x in written(proc)] == [
        "control_request", "user", "user"]
    # A NEW process of the same session is asked again, and waits again.
    assert host.park(sid)
    tick(senv, node, times=2)
    send(senv, sid, "after a rest")
    tick(senv, node, times=2)
    fresh = proc_of(senv, sid, node)[1]
    assert fresh is not proc and [x.get("type") for x in written(fresh)] == ["control_request"]
    fresh.emit(sealed_line(written(fresh)[0]["request_id"], off))
    tick(senv, node)
    assert [x.get("type") for x in written(fresh)] == ["control_request", "user"]
    # It says the key is NOT in force (the merged file was ignored): ended at once. Its turn was never written.
    sid, proc, rid = opened(P2)
    proc.emit(sealed_line(rid, {"model": "haiku"}))
    tick(senv, node, times=3)
    assert show(senv, sid)["state"] == "ended" and show(senv, sid)["error"] == access.NOT_SEALED[:300]
    assert msg(senv, sid, 1)["state"] == "failed" and msg(senv, sid, 1)["delivered_at"] is None
    assert len(written(proc)) == 1 and proc.terminated == 1
    # It never says, and writes what looks like a whole turn instead: nothing of that is taken for a turn (none was
    # written), and when the handshake's limit is up the session is ended.
    sid, proc, rid = opened(P3)
    proc.emit(began(sid), result(sid, text="done"))
    tick(senv, node, times=2)
    assert show(senv, sid)["state"] == "starting" and msg(senv, sid, 1)["state"] == "queued"
    senv.clock.advance(58)
    tick(senv, node)
    assert show(senv, sid)["state"] == "starting", "the limit is the handshake's (60 seconds), not the turn's"
    senv.clock.advance(3)
    tick(senv, node, times=3)
    assert show(senv, sid)["state"] == "ended" and show(senv, sid)["error"] == access.SEAL_UNVERIFIED[:300]
    failed = msg(senv, sid, 1)
    assert failed["state"] == "failed" and failed["delivered_at"] is None and show(senv, sid)["turns"] == 0, \
        "no turn was written, delivered or recorded as done"
    assert len(written(proc)) == 1 and proc.terminated == 1
    # Its process dies on the question (a CLI that cannot take it): the message fails, once, and nothing is started
    # again for it. The next message asks the next process the same.
    node.hello()                                         # (a minute has passed: the node says it is still there)
    sid, proc, rid = opened(P3)
    started = len(node.session_spawner.calls)
    proc.code = 1
    tick(senv, node, times=4)
    failed = msg(senv, sid, 1)
    assert failed["state"] == "failed" and "the process exited (code 1) before it was ready" in json.dumps(failed)
    assert len(node.session_spawner.calls) == started and show(senv, sid)["state"] == "idle"
    send(senv, sid, "again")
    tick(senv, node)
    assert len(node.session_spawner.calls) == started + 1
    assert [x.get("type") for x in written(node.session_spawner.children[-1])] == ["control_request"]


def test_the_settings_a_process_reports_never_reach_the_transcript_the_hub_or_a_log(senv, tmp_path, caplog):
    """S16 (round 4). The answer to `get_settings` is every setting in force, and settings can hold credentials
    (`env`). Every line a process writes goes to the session's transcript; this one must not. The adapter takes the
    one bit out of it, and the transcript gets a line of the adapter's own in its place."""
    secret = "SECRET-TOKEN-7f3a-9c21"
    own = tmp_path / "own.json"
    own.write_text(json.dumps({"permissions": {"allow": ["Read"]}}))
    runner = ["claude", "-p", "{prompt}", "--permission-mode", "dontAsk", "--settings", str(own)]
    node = make_node(senv, config=tree_config(senv, runners={"claude-code": runner}), log_dir=senv.tmp / "s16-logs")
    spy = node.sessions.client = Spy(node.sessions.client)
    sid = start(senv)["session_id"]
    tick(senv, node)
    proc = proc_of(senv, sid, node)[1]
    rid = written(proc)[0]["request_id"]
    said = sealed_line(rid, {"permissions": {"disableBypassPermissionsMode": "disable", "allow": ["Read"]},
                             "env": {"ANTHROPIC_API_KEY": secret}, "apiKeyHelper": f"echo {secret}"})
    said["response"]["response"]["sources"] = [{"source": "userSettings", "settings": {"env": {"KEY": secret}}}]
    proc.emit(said)
    tick(senv, node)
    proc.emit(began(sid), {"type": "assistant", "session_id": sid, "message": {"content": [
        {"type": "text", "text": "a control_response is a kind of line"}]}}, result(sid, text="done"))
    tick(senv, node, times=3)
    assert msg(senv, sid, 1)["state"] == "done" and show(senv, sid)["state"] == "idle"
    transcript = (senv.tmp / "s16-logs" / "sessions" / f"{sid}.jsonl").read_text()
    lines = [json.loads(x) for x in transcript.splitlines()]
    assert lines[0] == {"type": "host", "subtype": "control_response_withheld", "request_id": rid,
                        "answer": "success", "bypass_disabled": True}, "the adapter's own record of the answer"
    assert [x.get("type") for x in lines[1:]] == ["system", "assistant", "result"], "everything else as it was written"
    assert lines[2]["message"]["content"][0]["text"] == "a control_response is a kind of line"
    for needle in (secret, "ANTHROPIC_API_KEY", "apiKeyHelper", "effective", "userSettings"):
        assert needle not in transcript, needle
    # ... nor anywhere else: no file under the hub's and the node's directories, no report, no view, no log line.
    for path in senv.tmp.rglob("*"):
        if path.is_file():
            assert secret.encode() not in path.read_bytes(), path
    assert secret not in json.dumps(spy.posts) and secret not in json.dumps(show(senv, sid))
    assert secret not in caplog.text and "ANTHROPIC_API_KEY" not in caplog.text
    # The record says what the answer was worth, also when it is not the one hoped for.
    never_asked = ClaudeSession()
    never_asked.argv(SHIPPED + SEAL, session_id=UNKNOWN, resume=False, model=None)
    keep = never_asked.transcript_line
    plain = json.dumps(began(sid)).encode()
    assert keep(plain) is plain and keep(b"a line that is not JSON") == b"a line that is not JSON"
    refused = {"type": "control_response", "response": {"subtype": "error", "request_id": "settings-1",
                                                        "error": f"no: {secret}"}}
    assert json.loads(keep(json.dumps(refused).encode())) == {
        "type": "host", "subtype": "control_response_withheld", "request_id": "settings-1", "answer": "error",
        "bypass_disabled": False}
    odd = {"type": "control_response", "response": {"subtype": secret, "request_id": {"x": secret}, "response": secret}}
    assert secret.encode() not in keep(json.dumps(odd).encode())


def test_an_answer_cut_short_never_reaches_the_transcript_either(senv, tmp_path, caplog):
    """S16 (round 5). A line that does not parse may be the answer to `get_settings` cut short: the process died
    in the middle of it, or its last bytes came with no line end (the pipe reader hands such a tail on as a line).
    Half a settings body is as secret as a whole one. For a process that was asked, nothing unparseable is kept,
    only its length; for any process, nothing unparseable that names a control response or one of the adapter's
    questions."""
    secret, later = "SECRET-TOKEN-51c0-aa9e", "SECRET-TOKEN-88d2-07bf"
    own = tmp_path / "own.json"
    own.write_text(json.dumps({"permissions": {"allow": ["Read"]}}))
    runner = ["claude", "-p", "{prompt}", "--permission-mode", "dontAsk", "--settings", str(own)]
    node = make_node(senv, config=tree_config(senv, runners={"claude-code": runner}), log_dir=senv.tmp / "cut-logs")
    spy = node.sessions.client = Spy(node.sessions.client)
    sid = start(senv)["session_id"]
    tick(senv, node)
    proc = proc_of(senv, sid, node)[1]
    rid = written(proc)[0]["request_id"]
    whole = json.dumps(sealed_line(rid, {"permissions": {"disableBypassPermissionsMode": "disable"},
                                         "env": {"ANTHROPIC_API_KEY": secret}})).encode()
    cut = whole[:whole.index(secret.encode()) + len(secret)]                 # mid-stream, cut after the secret
    no_marker = b'{"response": {"response": {"effective": {"env": {"KEY": "' + secret.encode() + b'"'  # no type yet
    proc.emit(cut, no_marker)
    tick(senv, node, times=2)
    transcript = senv.tmp / "cut-logs" / "sessions" / f"{sid}.jsonl"
    assert [json.loads(x) for x in transcript.read_text().splitlines()] == [
        {"type": "host", "subtype": "line_withheld", "bytes": len(cut), "why": "not a complete JSON line"},
        {"type": "host", "subtype": "line_withheld", "bytes": len(no_marker), "why": "not a complete JSON line"}]
    assert node.sessions.host.sessions[sid].checking and len(written(proc)) == 1, "no answer yet: still no turn"
    # The complete answer, then more that does not parse: a process that was asked keeps none of it, before or after.
    proc.emit(whole)
    tick(senv, node)
    proc.emit(began(sid), b'{"effective": {"env": {"KEY": "' + later.encode(), result(sid))
    tick(senv, node, times=3)
    assert msg(senv, sid, 1)["state"] == "done"
    kinds = [(x.get("type"), x.get("subtype")) for x in map(json.loads, transcript.read_text().splitlines())]
    assert kinds[2:] == [("host", "control_response_withheld"), ("system", "init"), ("host", "line_withheld"),
                         ("result", "success")]
    # ... and as the tail a dying process leaves: the same answer, cut, with nothing after it.
    send(senv, sid, "again")
    tick(senv, node)
    proc.emit(began(sid), cut.replace(secret.encode(), later.encode()))
    proc.code = 1
    tick(senv, node, times=3)
    assert msg(senv, sid, 2)["state"] == "failed"
    last = json.loads(transcript.read_text().splitlines()[-1])
    assert (last["type"], last["subtype"]) == ("host", "line_withheld")
    for value in (secret, later, "ANTHROPIC_API_KEY"):
        for path in senv.tmp.rglob("*"):
            if path.is_file():
                assert value.encode() not in path.read_bytes(), (value, path)
        assert value not in json.dumps(spy.posts) and value not in json.dumps(show(senv, sid))
        assert value not in caplog.text
    # A process that was never asked (the key as JSON) keeps what does not parse, unless it names a control
    # response or a question of the adapter's.
    plain = ClaudeSession()
    plain.argv(SHIPPED + SEAL, session_id=UNKNOWN, resume=False, model=None)
    assert plain.transcript_line(b"some text, not JSON") == b"some text, not JSON"
    for line in (cut, b'{"type":"control_response","resp', b'... get_settings ...',
                 b'{"request_id": "settings-' + UNKNOWN[:8].encode() + b'-1", "response": {"effe'):
        assert json.loads(plain.transcript_line(line)) == {"type": "host", "subtype": "line_withheld",
                                                           "bytes": len(line), "why": "not a complete JSON line"}


def test_a_turn_half_written_when_its_parents_process_goes_is_withdrawn(senv):
    """S1 (round 5). The gate was asked before a turn was buffered, and never again: a pipe that takes a turn in
    pieces got the rest of it in a later pass, and the turn was reported delivered, though the parent's process had
    gone in between. Now it is asked before every write of a pending turn. A half-written line is never completed:
    the child's process is stopped and the message fails as undelivered. The child stays open and held."""
    from agent_comms.session_host import TURN_WITHDRAWN

    node = full_node(senv, "w")
    sid = root(senv, node=node)
    answer(senv, sid, node=node)
    kid = child(senv, sid, "one")["session_id"]
    tick(senv, node)
    answer(senv, kid, node=node)
    claimed(senv, sid)
    host, kid_proc, parent_proc = node.sessions.host, proc_of(senv, kid, node)[1], proc_of(senv, sid, node)[1]
    first = len(kid_proc.stdin)
    kid_proc.accept = 12                                 # the pipe takes twelve bytes of a write, no more
    send(senv, kid, "two, which is longer than twelve bytes")
    tick(senv, node)
    assert len(kid_proc.stdin) == first + 12 and host.sessions[kid].writing is not None
    assert msg(senv, kid, 2)["state"] == "queued", "partly written is not delivered"
    parent_proc.code = 1                                 # the parent's process dies before the next write
    kid_proc.accept = None

    def kid_starts() -> int:
        return sum(1 for c in node.session_spawner.calls if kid in c["argv"])
    started = kid_starts()
    tick(senv, node, times=4)
    # The parent is told (hosted_results.py): that result is its next turn, in a process of its own.
    [res] = [x for x in show(senv, sid)["results"] if x["state"] != "claimed"]
    assert (res["message"], res["outcome"], msg(senv, sid, 2)["kind"]) == (2, "withdrawn", "child")
    assert TURN_WITHDRAWN[:60] in res["detail"]
    assert len(kid_proc.stdin) == first + 12, "not one byte more: a half-written line is never completed"
    assert kid_proc.terminated == 1 and host.sessions[kid].child is None, "its process was stopped"
    failed = msg(senv, kid, 2)
    assert failed["state"] == "failed" and failed["delivered_at"] is None and "was not delivered" in json.dumps(failed)
    assert TURN_WITHDRAWN[:60] in json.dumps(failed)
    view = show(senv, kid)
    assert view["state"] == "idle" and view["turns"] == 1, "the child itself stays open; one turn is all it had"
    assert kid_starts() == started, "and nothing is started again for that message"
    # Held like any full child whose parent has no process, until the parent's next turn says it runs bypassed.
    send(senv, kid, "three")
    tick(senv, node, times=3)
    assert msg(senv, kid, 3)["state"] == "queued" and kid_starts() == started
    send(senv, sid, "again")
    tick(senv, node)
    begin(senv, sid, node)
    tick(senv, node, times=2)
    fresh = proc_of(senv, kid, node)[1]
    assert fresh is not kid_proc and msg(senv, kid, 3)["state"] == "delivered"
    assert [x["message"]["content"] for x in written(fresh)] == ["three"], "a fresh process, and only the new message"
    senv.node.hello()


def test_two_parents_and_two_waiting_full_children_in_two_slots_make_progress(senv):
    """S18 (round 5). A parent was spared from being stopped whenever a full child of it had a message queued. With
    `max_live = 2`, two idle confirmed parents and a waiting full child each, every slot was pinned by a parent
    waiting for a child that could never get a slot. Now such a pin holds only while something else can be
    stopped: one parent makes room, the other pair runs, and the first pair runs when its parent has had a turn
    again. No child is ever started under a parent whose live process has not confirmed full access."""
    node = full_node(senv, "s18", max_live=2)
    host, runner = node.sessions.host, node.sessions
    parents = [root(senv, node=node), root(senv, node=node, project=P2)]
    for sid in parents:
        answer(senv, sid, node=node)
    assert [host.sessions[sid].ceiling for sid in parents] == ["full", "full"]
    under, real_spawn = [], host._spawn

    def spawn(s, now, out):
        parent = runner.parent_of.get(s.session_id)
        if parent is not None:
            under.append((s.session_id, host.sessions[parent].ceiling))
        return real_spawn(s, now, out)

    host._spawn = spawn
    kids = [child(senv, sid, f"go {i}")["session_id"] for i, sid in enumerate(parents)]
    tick(senv, node, times=5)
    states = [msg(senv, k, 1)["state"] for k in kids]
    assert sorted(states) == ["delivered", "queued"], "one pair runs; the other child waits"
    first, second = (0, 1) if states[0] == "delivered" else (1, 0)
    assert host.sessions[parents[first]].child is not None and host.sessions[parents[second]].child is None, \
        "the OTHER pair's parent made room; the running child's own parent was not touched"
    assert show(senv, kids[second])["state"] != "ended" and show(senv, parents[second])["ceiling"] == "workspace"
    answer(senv, kids[first], node=node)
    claimed(senv, parents[first])
    tick(senv, node, times=3)
    assert msg(senv, kids[first], 1)["state"] == "done" and msg(senv, kids[second], 1)["state"] == "queued"
    assert [k for k, _ in under] == [kids[first]], "nothing was started for the child whose parent has no process"
    # The second pair: its parent's next turn brings its process back and confirms it; then its child runs.
    send(senv, parents[second], "again")
    tick(senv, node, times=3)
    begin(senv, parents[second], node)
    tick(senv, node, times=4)
    assert msg(senv, kids[second], 1)["state"] == "delivered" and host.sessions[parents[second]].ceiling == "full"
    assert [k for k, _ in under] == [kids[first], kids[second]] and all(c == "full" for _, c in under), \
        "each child was started under a parent whose live process had confirmed full access"
    senv.node.hello()


def test_a_node_with_one_slot_cannot_run_a_full_child(senv):
    """S18 (round 5). A full child takes turns only while its parent's process is up beside its own: `max_live`
    below 2 can never fit that. The hub knows the number from the node's hello and refuses the child when it is
    asked for; a child that exists already when the node comes back with one slot is ended there with the reason."""
    from agent_comms.node_sessions import NO_ROOM_FOR_FULL

    two = full_node(senv, "one-slot", max_live=2)
    sid = root(senv, node=two)
    answer(senv, sid, node=two)
    kid = child(senv, sid, "one")["session_id"]
    one = full_node(senv, "one-slot", max_live=1)        # the node is restarted with one slot
    tick(senv, one, times=3)
    assert show(senv, kid)["state"] == "ended" and show(senv, kid)["error"] == NO_ROOM_FOR_FULL.format(n=1)
    assert msg(senv, kid, 1)["state"] == "failed" and show(senv, sid)["state"] != "ended"
    send(senv, sid, "again")
    tick(senv, one)
    begin(senv, sid, one)
    assert show(senv, sid)["ceiling"] == "full"
    out = child(senv, sid, expect=409)
    assert "runs at most 1 session process at a time" in out["message"] and "max_live must be at least 2" in out["message"]
    assert child(senv, sid, access="read-only")["access"] == "read-only", "a narrower child needs no such room"
    senv.node.hello()


def test_a_board_config_inside_a_checkout_or_a_worktree_is_not_used_for_a_read_only_session(senv, tmp_path):
    """S2 (round 4). A read-only session's one MCP server is the board's, as the runner's own `--mcp-config`
    defines it: a command this session starts. A config FILE where sessions write (the run directory, any project
    directory of this machine, the worktree directory) could have been written by another session, so it is not
    read; such a runner's read-only child has no board tools, like one whose runner names no config."""
    worktrees = tmp_path / "wt"
    (worktrees / "repo" / "session-x").mkdir(parents=True)
    safe, inside = tmp_path / "ok.mcp.json", senv.work / "board.mcp.json"
    in_worktree, link = worktrees / "repo" / "session-x" / "board.mcp.json", tmp_path / "link.mcp.json"
    for path in (safe, inside, in_worktree):
        path.write_text(json.dumps({"mcpServers": {"agent-comms": BOARD_ENTRY}}))
    link.symlink_to(inside)
    for i, (path, used) in enumerate(((safe, True), (inside, False), (in_worktree, False), (link, False))):
        runner = ["claude", "-p", "{prompt}", "--permission-mode", "dontAsk", "--mcp-config", str(path),
                  "--allowedTools=mcp__agent-comms"]
        host = host_for(senv, f"mcp-{i}", runners={"claude-code": runner}, worktree_dir=str(worktrees))
        opened = host.open(UNKNOWN, agent="claude", runtime="claude-code", project=PROJECT, access="read-only")
        argv = host._argv(opened)
        assert opened.access == "read-only" and "--strict-mcp-config" in argv and str(path) not in argv
        if used:
            assert json.loads(Path(value_after(argv, "--mcp-config")).read_text()) == {
                "mcpServers": {"agent-comms": BOARD_ENTRY}}
        else:
            assert "--mcp-config" not in argv, f"{path}: where sessions write, so not read"
            assert not list(host.sessions_dir.glob("*.mcp.json")) if host.sessions_dir.exists() else True
        # The same runner as its owner configured it (a workspace session) keeps its own config.
        ws = host_for(senv, f"mcp-ws-{i}", runners={"claude-code": runner}, worktree_dir=str(worktrees))
        assert value_after(ws._argv(ws.open(UNKNOWN, agent="claude", runtime="claude-code", project=PROJECT)),
                           "--mcp-config") == str(path)
    host = host_for(senv, "mcp-direct", worktree_dir=str(worktrees))
    assert host._board_mcp(UNKNOWN, ["board.mcp.json"], str(senv.work)) is None, "a relative path is the run directory's"
    assert host._board_mcp(UNKNOWN, [str(inside), str(safe)], str(senv.work)).endswith(f"{UNKNOWN}.mcp.json")
    assert host._board_mcp(UNKNOWN, [json.dumps({"mcpServers": {"agent-comms": BOARD_ENTRY}})], str(senv.work)), \
        "the JSON itself is the runner's own argument"
    assert host._session_writable(str(safe), str(senv.work)) is False
    (tmp_path / "elsewhere").mkdir()
    assert host._session_writable(str(tmp_path / "elsewhere" / "x.json"), str(tmp_path / "elsewhere")), \
        "a child's own run directory (its worktree) counts too"


def test_a_runner_that_names_its_own_directory_is_held_against_where_the_parent_runs(senv, tmp_path):
    """S8 (round 3). A Codex runner whose `--cd` is a path can be a session in that directory only. The dry run
    behind the node's hello has no directory, so it used to offer nothing for such a runner. Now the hello offers
    the words the runner has IN its directory and names the directory (`dirs`), every root reports where it runs
    (`checkout`), and the hub holds a child against both when it is asked for."""
    from agent_comms.providers.codex import named_directory
    from agent_comms.session_host import child_words, runner_dir

    here, other = senv.work, tmp_path / "elsewhere"
    other.mkdir()
    bound = ["codex", "exec", "--cd", str(here), "--sandbox", "workspace-write", "{prompt}"]
    assert named_directory(bound) == str(here) == runner_dir(bound)
    assert named_directory(["codex", "exec", f"-C{here}/", "{prompt}"]) == str(here)
    assert named_directory(["codex", "exec", f"--cd={here}", "-C", "{project}", "{prompt}"]) is None, "the last one counts"
    assert named_directory(CODEX_RUNNER) is None and runner_dir(SHIPPED) is None and runner_dir(CODEX_RUNNER) is None
    config = tree_config(senv, runners={"codex-cli": bound}, worktrees={PROJECT: str(here), P2: str(other)})
    assert child_words(config, bound) == ["read-only", "workspace"], "the words it has in its own directory"
    node = make_node(senv, config=config, log_dir=senv.tmp / "b-logs")
    node.hello()
    [info] = [n for n in nodes.nodes(senv.board) if n["node"] == "laptop"]
    assert info["session_host"] == {"tree": 4, "runners": {"claude-code": ["read-only", "workspace"],
                                                           "codex-cli": ["read-only", "workspace"]},
                                    "dirs": {"codex-cli": str(here)}}
    sid = start(senv)["session_id"]
    # (a status that says the access and not yet where the session runs, as an earlier node version's would)
    senv.laptop.post(f"/api/nodes/sessions/{sid}", json={"events": [], "state": "idle", "access": "workspace",
                                                         "ceiling": "workspace"})
    assert f"where session {sid} runs on its node is not known yet" in child(senv, sid, agent="codex", expect=409)["message"]
    tick(senv, node)
    assert stored(senv, sid)["checkout"] == str(here)
    out = child(senv, sid, agent="codex", isolation="worktree", expect=409)
    assert f"names its own directory ({here})" in out["message"] and "cannot have a worktree of its own" in out["message"]
    kid = child(senv, sid, "count", agent="codex")
    tick(senv, node)
    assert codex_call(senv, node)["cwd"] == str(here) and show(senv, kid["session_id"])["state"] != "ended"
    elsewhere = root(senv, node=node, project=P2)
    assert stored(senv, elsewhere)["checkout"] == str(other)
    out = child(senv, elsewhere, agent="codex", expect=409)
    assert f"session {elsewhere} runs in {other}" in out["message"] and "children" not in show(senv, elsewhere), \
        "refused when it is asked for, not made and then ended on the node"
    assert child(senv, elsewhere, isolation="worktree")["agent"] == "claude", \
        "a runner that names no directory is not held to one"
    senv.node.hello()
    assert "dirs" not in [n for n in nodes.nodes(senv.board) if n["node"] == "laptop"][0]["session_host"]


def test_a_malformed_hello_is_a_400_whatever_is_in_its_word_lists(senv):
    """S15 (round 3). An object or an array among the access words used to reach `set()` and come back as a 500."""
    hello = {"projects": [PROJECT, P2, P3], "runners": ["claude-code", "codex-cli"], "sessions": {"max_live": 4}}
    strict = TestClient(senv.app, base_url="http://127.0.0.1:8787", raise_server_exceptions=False,
                        headers={"Authorization": "Bearer " + senv.tokens["laptop"]})
    for words in ([{}], [["workspace"]], [1], [None], ["workspace", {"a": 1}], [[]], ["workspace", "workspace"],
                  ["read-only", "workspace", "full", "full"]):
        r = strict.post("/api/nodes/hello", json=hello | {"session_host": {"tree": 1, "runners": {"claude-code": words}}})
        assert r.status_code == 400, (words, r.status_code, r.text)
    ok = {"tree": 1, "runners": {"codex-cli": ["workspace"]}}
    for dirs in ("x", {"claude-code": "/x"}, {"codex-cli": "relative"}, {"codex-cli": 5}, {"codex-cli": ["/x"]},
                 {"codex-cli": "/x\n"}, {"codex-cli": {}}):
        r = strict.post("/api/nodes/hello", json=hello | {"session_host": ok | {"dirs": dirs}})
        assert r.status_code == 400, (dirs, r.status_code, r.text)
    assert strict.post("/api/nodes/hello", json=hello | {"session_host": ok | {"dirs": {"codex-cli": "/x/y"}}}).status_code == 200
    senv.node.hello()


def test_a_read_only_child_of_a_runner_with_an_option_that_cannot_be_dropped_is_refused_when_asked_for(senv):
    """S2 (round 3). Such a runner is not offered at `read-only` (the node's hello), so the hub refuses the child
    when it is asked for; a workspace child of it runs the runner as its owner configured it."""
    from agent_comms.session_host import child_words

    odd = SHIPPED + ["--some-future-loader", "/x/ext"]
    plugged = SHIPPED + ["--plugin-dir", "/x/plug"]
    assert child_words(tree_config(senv), odd) == ["workspace"]
    assert child_words(tree_config(senv), plugged) == ["read-only", "workspace"], "a plugin directory is simply dropped"
    node = make_node(senv, config=tree_config(senv, runners={"claude-code": odd}), log_dir=senv.tmp / "odd-logs")
    node.hello()
    sid = root(senv, node=node)
    out = child(senv, sid, access="read-only", expect=409)
    assert "cannot start a session of claude at read-only access (it can at: workspace)" in out["message"]
    assert child(senv, sid, access="workspace")["access"] == "workspace"
    argv, label = claude_argv(senv, "plugged-ro", runner=plugged, access_word="read-only")
    assert label == "read-only" and "--plugin-dir" not in argv and "/x/plug" not in argv
    argv, label = claude_argv(senv, "plugged-ws", runner=plugged, access_word="workspace")
    assert value_after(argv, "--plugin-dir") == "/x/plug", "as configured"
    senv.node.hello()


def test_a_node_that_does_not_declare_the_child_protocol_is_listed_no_child(senv):
    """S5. Declared on every fetch (`tree=1`), as this version's node does. A node rolled back to the version
    before would open a child as an ordinary session, in the shared checkout, with its runner's own permissions."""
    spy = senv.node.sessions.client = Spy(senv.node.sessions.client)
    sid = root(senv)
    kid = child(senv, sid, access="read-only", isolation="worktree")["session_id"]
    plain = senv.laptop.get("/api/nodes/sessions").json()["sessions"]
    assert [x["session_id"] for x in plain] == [sid], "an undeclared fetch: the root only"
    assert set(plain[0]) == {"session_id", "agent", "runtime", "project", "model", "stop", "resume", "messages",
                             "status_seq"}, "... and that as before there were trees"
    declared = {x["session_id"]: x for x in senv.laptop.get("/api/nodes/sessions", params={"tree": 1}).json()["sessions"]}
    assert set(declared) == {sid, kid} and declared[kid]["parent"] == sid and declared[sid]["access"] is None
    assert spy.gets and all(params.get("tree") == 4 for _, params in spy.gets), "this node says so every time"
    assert msg(senv, kid, 1)["state"] == "queued"


def test_children_need_a_node_whose_latest_hello_declares_the_protocol(senv):
    """S5. Creation requires current support, and a child whose node no longer has it is not left closing for ever."""
    [info] = [n for n in nodes.nodes(senv.board) if n["node"] == "laptop"]
    assert info["session_host"] == {"tree": 4, "runners": {"claude-code": ["read-only", "workspace"],
                                                           "codex-cli": ["read-only", "workspace"]}}
    sid = root(senv)
    kid = child(senv, sid)["session_id"]
    hello = {"projects": [PROJECT, P2, P3], "runners": ["claude-code", "codex-cli"], "sessions": {"max_live": 4}}
    for bad in ({"tree": 0, "runners": {}}, {"tree": 1}, {"tree": 1, "runners": {"grok": ["workspace"]}},
                {"tree": True, "runners": {}}, {"tree": 1, "runners": {}, "extra": 1}, "yes", {"runners": {}},
                {"tree": 1, "runners": ["claude-code"]}, {"tree": 1, "runners": {"claude-code": ["root"]}},
                {"tree": 1, "runners": {"claude-code": []}}, {"tree": 1, "runners": {"claude-code": "workspace"}}):
        r = senv.laptop.post("/api/nodes/hello", json=hello | {"session_host": bad})
        assert r.status_code in (400, 422), bad
    r = senv.laptop.post("/api/nodes/hello", json={k: v for k, v in hello.items() if k != "sessions"}
                         | {"session_host": {"tree": 1, "runners": {}}})
    assert r.status_code == 400, "only beside sessions"
    assert senv.laptop.post("/api/nodes/hello", json=hello).status_code == 200     # the node was rolled back
    assert "session_host" not in [n for n in nodes.nodes(senv.board) if n["node"] == "laptop"][0]
    out = child(senv, sid, expect=409)
    assert "node laptop does not host child sessions" in out["message"]
    assert senv.human.post(f"/api/hosted-sessions/{kid}/close").status_code == 200
    assert show(senv, kid)["state"] == "ended", "nobody there would carry a stop flag out"
    senv.node.hello()
    kid2 = child(senv, sid)["session_id"]
    assert senv.laptop.post("/api/nodes/hello", json=hello).status_code == 200
    assert senv.human.post(f"/api/hosted-sessions/{sid}/close").status_code == 200
    assert (show(senv, sid)["stop"], show(senv, kid2)["state"]) == ("close", "ended"), "the cascade ends it at once too"


def test_a_node_advertises_only_what_its_session_preparation_accepts(senv):
    """S8. Not "there is an adapter for its CLI": for each runner, the words a child of it can really be started
    at here, found by doing what opening one does (session_host.child_words). The hub checks the runner when a
    child is asked for, and the word when it is admitted."""
    from agent_comms.session_host import child_words

    sid = root(senv)
    approve = ["codex", "exec", "--cd", "{project}", "--approve-for-me", "{prompt}"]
    config = tree_config(senv)
    assert child_words(config, SHIPPED) == ["read-only", "workspace"] == child_words(config, CODEX_RUNNER)
    assert child_words(config, BYPASS) == ["read-only"], "not workspace; full only where the node allows it"
    assert child_words(config, approve) == [] == child_words(config, ["grok", "{prompt}"])
    allowing = tree_config(senv, sessions={"allow_full_access": True})
    assert child_words(allowing, SHIPPED) == ["read-only", "workspace", "full"]
    assert child_words(allowing, BYPASS) == ["read-only", "full"] and child_words(allowing, approve) == []
    assert child_words(tree_config(senv, sessions={"codex": False}), CODEX_RUNNER) == []

    def offered() -> dict:
        return [n for n in nodes.nodes(senv.board) if n["node"] == "laptop"][0]["session_host"]["runners"]

    off = make_node(senv, config=tree_config(senv, sessions={"codex": False}), log_dir=senv.tmp / "off-logs")
    off.hello()
    assert offered() == {"claude-code": ["read-only", "workspace"]}
    out = child(senv, sid, agent="codex", expect=409)
    assert "node laptop cannot host codex as a session" in out["message"] and "codex = true" in out["message"]
    # A Codex runner there is an adapter for, and that preparation refuses at every word: not advertised either.
    refused = make_node(senv, config=tree_config(senv, runners={"codex-cli": approve},
                                                 sessions={"allow_full_access": True}), log_dir=senv.tmp / "ref-logs")
    refused.hello()
    assert offered() == {"claude-code": ["read-only", "workspace", "full"]}
    assert "codex-cli" in [n for n in nodes.nodes(senv.board) if n["node"] == "laptop"][0]["runners"]
    out = child(senv, sid, agent="codex", expect=409)
    assert "node laptop cannot host codex as a session" in out["message"] and "--approve-for-me" in out["message"]
    assert "children" not in show(senv, sid), "nothing was made"
    assert child(senv, sid)["agent"] == "claude"
    senv.node.hello()
    assert child(senv, sid, agent="codex")["agent"] == "codex"


def test_a_worktree_with_ignored_files_is_kept(wtenv):
    """S6. `git status` and an unforced `git worktree remove` both pass over ignored files, and removal deletes
    them. A build output or a local env file is ignored and is still somebody's work."""
    (wtenv.work / ".gitignore").write_text("*.log\nbuild/\n")
    git(wtenv.work, "add", ".gitignore")
    git(wtenv.work, "commit", "-q", "-m", "ignore")
    sid = root(wtenv)
    kid = child(wtenv, sid, "w", isolation="worktree")["session_id"]
    tick(wtenv)
    path = Path(show(wtenv, kid)["worktree"])
    (path / "debug.log").write_text("hours of output\n")
    (path / "build").mkdir()
    (path / "build" / "out.bin").write_text("built\n")
    assert git(path, "status", "--porcelain") == "" and dispatch.removable_run_worktree(str(path)), \
        "clean by the dispatcher's rule"
    assert not session_worktrees.holds_nothing(str(path))
    view = close(wtenv, kid)
    assert view["state"] == "ended" and view["worktree_removed_at"] is None
    assert (path / "debug.log").is_file() and (path / "build" / "out.bin").is_file(), "nothing was deleted"
    assert "it stays there" in cli._fmt_hosted_detail(view)
    (path / "debug.log").unlink()
    wtenv.clock.advance(3601)
    wtenv.node.hello()
    tick(wtenv, times=2)
    assert path.is_dir(), "one ignored file is left"
    (path / "build" / "out.bin").unlink()
    (path / "build").rmdir()
    wtenv.clock.advance(3601)
    wtenv.node.hello()
    tick(wtenv, times=2)
    assert not path.exists() and show(wtenv, kid)["worktree_removed_at"] is not None


def test_only_a_registered_linked_worktree_at_its_canonical_path_is_reused_or_removed(wtenv, tmp_path):
    """S10. Being inside the repository is not being its worktree (git finds the checkout's own .git from any
    folder under it), and a symlink on the way is another path."""
    config, checkout = wtenv.node.config, str(wtenv.work)
    config.worktree_dir = os.path.join(checkout, "wt")              # a worktree directory INSIDE the checkout
    nested = Path(session_worktrees.path_for(config, checkout, UNKNOWN))
    nested.mkdir(parents=True)
    (nested / "keep.txt").write_text("somebody's\n")
    assert dispatch._git_common_dir(str(nested)) == dispatch._git_common_dir(checkout), "git's parent discovery"
    with pytest.raises(Conflict, match="not a linked worktree of this checkout"):
        session_worktrees.ensure(config, checkout, UNKNOWN)
    assert session_worktrees.remove_if_safe(checkout, str(nested)) == "kept" and (nested / "keep.txt").is_file()
    assert not session_worktrees.registered(checkout, checkout), "the main worktree is never a session's"

    real = Path(os.path.realpath(tmp_path)) / "real-wt"
    real.mkdir()
    link = Path(os.path.realpath(tmp_path)) / "link-wt"
    link.symlink_to(real)
    config.worktree_dir = str(real)
    made = session_worktrees.ensure(config, checkout, UNKNOWN)
    assert session_worktrees.registered(checkout, made) and session_worktrees.ensure(config, checkout, UNKNOWN) == made
    via_link = made.replace(str(real), str(link))
    assert os.path.isdir(via_link) and not session_worktrees.registered(checkout, via_link)
    config.worktree_dir = str(link)
    assert session_worktrees.path_for(config, checkout, UNKNOWN) == via_link
    with pytest.raises(Conflict, match="not a linked worktree of this checkout"):
        session_worktrees.ensure(config, checkout, UNKNOWN)         # the reuse path, through a symlink
    assert session_worktrees.remove_if_safe(checkout, via_link) == "kept" and os.path.isdir(made)
    assert session_worktrees.remove_if_safe(checkout, made) == "removed"
    with pytest.raises(Conflict, match="symlink"):
        session_worktrees.ensure(config, checkout, UNKNOWN)         # the create path, through a symlink


def test_a_child_under_another_root_supplies_no_read_for_a_root_of_its_agent(senv):
    """S7. A Codex child under a Claude root is, to the board, the same reader as a separate Codex root in the
    project. Neither its read mark nor its receipt counts for that root, and a stored mark is checked too."""
    claude_root = root(senv)
    answer(senv, claude_root)
    codex_root = start(senv, agent="codex")["session_id"]
    tick(senv)
    proc = codex_proc(senv)
    codex_handshake(senv, proc)
    codex_turn(senv, proc, "ready")
    assert show(senv, codex_root)["in_flight"] is None
    post = ask(senv, senv.thread(), as_="grok", to=("codex",))
    tick(senv)
    assert len(wakes(senv, codex_root)) == 1 and record(senv, codex_root)["woken"] == {str(post["id"]): senv.clock()}
    later(senv, GRACE)
    now = senv.clock
    assert not hosted.holds(senv.board, "codex", PROJECT, post["id"], now())
    child(senv, claude_root, "look around", agent="codex")      # the same identity, the same project, another root
    assert hosted_tree.children(senv.board.conn, codex_root) == []
    reader = senv.session("codex")                              # the board session the CHILD's process registers
    assert hosted_wake._own_reader(senv.board.conn, reader, "codex", PROJECT)
    out = senv.board.read_updates(senv.p["codex"], reader, only="addressed")
    assert post["id"] in [x["id"] for x in out["posts"]]
    assert record(senv, codex_root)["read"] == {}, "no mark: a child of that agent is around in the project"
    senv.board.read_updates(senv.p["codex"], reader)
    assert delivery.receipt(senv.board.conn, post["id"], "codex") is not None
    assert not hosted.holds(senv.board, "codex", PROJECT, post["id"], now()), "nor its receipt"
    with db.write_tx(senv.board.conn) as c:                     # a mark that is on record all the same
        rec = hosted_wake._load(c, codex_root)
        rec["read"][str(post["id"])] = now()
        hosted_wake._save(c, codex_root, rec, now())
    assert not hosted.holds(senv.board, "codex", PROJECT, post["id"], now()), "a stored mark is checked as well"
    # A child of the agent in ANOTHER project is nobody's reader here.
    assert hosted_wake._shadowed(senv.board.conn, stored(senv, codex_root), 0)
    assert not hosted_wake._shadowed(senv.board.conn, stored(senv, codex_root) | {"project": P2}, 0)


ROOT_FORMS = [["--agent", "claude", "--model", "haiku", PROJECT, "hello"],
              [PROJECT, "--agent", "claude", "hello"],
              [PROJECT, "--agent", "claude", "--model", "haiku", "hello", "--json"],
              [PROJECT, "hello", "--agent", "claude", "--model", "haiku"],
              ["--agent=claude", PROJECT, "--model=haiku", "hello"]]


@pytest.mark.parametrize("form", ROOT_FORMS, ids=["before", "between", "between-and-after", "after", "equals"])
def test_a_root_takes_its_two_positionals_with_options_before_between_and_after(clienv, capsys, form):
    """S9. `board session start /repo --agent codex hello` took `hello` for a stray argument once the project had
    become an optional positional. Two required positionals again."""
    cli.main(["session", "start", *form])
    out = capsys.readouterr().out
    if "--json" in form:
        data = json.loads(out)
        assert (data["session"]["project"], data["session"]["agent"], data["message"]["n"]) == (PROJECT, "claude", 1)
    else:
        assert f"] claude in {PROJECT} on node laptop" in out and "message 1 queued" in out
    assert clienv.requests[-1] == ("POST", "http://127.0.0.1:8787/api/hosted-sessions")
    assert [m["state"] for m in show(clienv, hosted._sessions(clienv.board.conn)[0]["session_id"])["messages"]] == ["queued"]


@pytest.mark.parametrize("form", ["before", "between", "after", "equals"])
def test_a_child_takes_its_one_positional_wherever_its_options_stand(clienv, capsys, form):
    """S9. The one-positional form, only with --parent."""
    cli.main(["session", "start", PROJECT, "--agent", "claude", "hello", "--wait"])
    capsys.readouterr()
    sid = hosted._sessions(clienv.board.conn)[0]["session_id"]
    argv = {"before": ["--parent", sid, "--role", "test", "--access", "read-only", "do it"],
            "between": ["--role", "test", "do it", "--parent", sid, "--access", "read-only"],
            "after": ["do it", "--parent", sid, "--role", "test", "--access", "read-only"],
            "equals": [f"--parent={sid}", "--role=test", "do it", "--access=read-only"]}[form]
    cli.main(["session", "start", *argv])
    out = capsys.readouterr().out
    assert out.startswith("child test-1 ") and "access read-only" in out and f"of {sid}; message 1 queued" in out
    [kid] = show(clienv, sid)["children"]
    assert clienv.requests[-1] == ("POST", f"http://127.0.0.1:8787/api/hosted-sessions/{sid}/children")
    assert (kid["role"], kid["access"]) == ("test", "read-only")


def test_a_stale_worktree_entry_does_not_make_a_folder_at_its_path_a_worktree(wtenv):
    """S10. git keeps listing a worktree whose directory was deleted until it is pruned, and calls it `prunable`.
    A folder made at that path later is nobody's worktree; nor is a directory git lists that answers as another
    repository."""
    import shutil

    config, checkout = wtenv.node.config, str(wtenv.work)
    made = session_worktrees.ensure(config, checkout, UNKNOWN)
    assert session_worktrees.registered(checkout, made)
    shutil.rmtree(made)
    listing = git(checkout, "worktree", "list", "--porcelain")
    assert f"worktree {made}" in listing and "prunable" in listing, "still listed, and stale"
    os.mkdir(made)
    Path(made, "keep.txt").write_text("somebody's\n")
    assert not session_worktrees.registered(checkout, made)
    with pytest.raises(Conflict, match="not a linked worktree of this checkout"):
        session_worktrees.ensure(config, checkout, UNKNOWN)
    assert session_worktrees.remove_if_safe(checkout, made) == "kept" and Path(made, "keep.txt").is_file()

    other = session_worktrees.ensure(config, checkout, cx.SID)         # listed, not stale ...
    os.remove(os.path.join(other, ".git"))
    git(other, "init", "-q")                                            # ... and now a repository of its own
    assert f"worktree {other}" in git(checkout, "worktree", "list", "--porcelain")
    assert not session_worktrees.registered(checkout, other), "its own git directory is not the checkout's worktree"
    assert session_worktrees.remove_if_safe(checkout, other) == "kept" and os.path.isdir(other)


@pytest.mark.parametrize("argv", [["--parent=", "hello"], ["--parent", "", "hello"], ["hello", "--parent=   "]])
def test_an_empty_parent_id_is_refused_when_the_arguments_are_parsed(clienv, capsys, argv):
    """S13. `--parent= hello` used to fall through to the two-positional form with no project (a TypeError)."""
    before = len(clienv.requests)
    with pytest.raises(SystemExit) as e:
        cli.main(["session", "start", *argv])
    assert e.value.code == 2 and "--parent" in capsys.readouterr().err
    assert len(clienv.requests) == before, "nothing was sent"


# ------------------------------------------------------------ PR review (#89)


def test_a_child_whose_node_was_rolled_back_is_settled_by_the_hub(senv):
    """B1. A node whose latest hello no longer says it hosts child sessions is listed no child, so it can neither
    report a child's lost turn nor end the child; and its ordinary hellos used to count as "the child's node is
    there", so the turn never settled and a shared child kept its project busy for the dispatcher. The hub settles
    such children itself: the turn fails as lost, the child is ended, the project is free."""
    sid = root(senv)
    answer(senv, sid)
    kid = child(senv, sid, "work")["session_id"]
    idle = child(senv, sid, "wait", access="read-only")["session_id"]
    tick(senv)
    begin(senv, kid)                                     # its turn is running, in the shared checkout
    answer(senv, idle)
    claimed(senv, sid)                                   # (no turn of the parent's for that result)
    assert msg(senv, kid, 1)["state"] == "delivered" and hosted_wake.turn_projects(senv.board, senv.clock()) == {PROJECT}
    hello = {"projects": [PROJECT, P2, P3], "runners": ["claude-code", "codex-cli"], "sessions": {"max_live": 4}}
    assert senv.laptop.post("/api/nodes/hello", json=hello).status_code == 200      # rolled back: no session_host
    assert [x["session_id"] for x in senv.laptop.get("/api/nodes/sessions").json()["sessions"]] == [sid]
    assert [r["session_id"] for r in hosted_wake.turn_sessions(senv.board, senv.clock())] == [sid], \
        "the children no longer hold the project; only the parent's turn with their results, reserved once listed " \
        "(review round 1 of deeper trees, D2), until its node reports it"
    for x in (kid, idle):
        view = show(senv, x)
        assert view["state"] == "ended" and view["error"] == hosted_tree.ORPHANED[:300], x
    failed = msg(senv, kid, 1)
    assert failed["state"] == "failed" and "no longer hosts child sessions" in failed["detail"]
    assert show(senv, sid)["state"] != "ended", "its parent, an ordinary session to that node, goes on"
    assert sorted((x["child"], x["outcome"]) for x in show(senv, sid)["results"] if x["state"] != "claimed") == \
        sorted([(kid, "failed"), (kid, "ended"), (idle, "ended")]), \
        "and is told of the turn that was lost and that both ended (hosted_results.py)"
    # The same when the hub only finds out on a read (the hello's own settling is taken out here).
    senv.node.hello()
    late = child(senv, sid, "more")["session_id"]
    tick(senv)
    answer(senv, sid)                                    # the parent's turn with those results
    begin(senv, late)
    with db.write_tx(senv.board.conn) as c:
        info = nodes._get(c, nodes.NODE_PREFIX + "laptop")
        info.pop("session_host")
        nodes._put(c, nodes.NODE_PREFIX + "laptop", info, "laptop", senv.clock())
    assert hosted_wake.turn_projects(senv.board, senv.clock()) == set()
    assert show(senv, late)["state"] == "ended" and msg(senv, late, 1)["state"] == "failed"
    senv.node.hello()


def test_the_hub_hears_that_a_worktree_is_gone_also_for_a_child_it_ended_itself(wtenv):
    """B2. The hub ended the child while its node was away. Back, the node closes what the hub no longer lists
    and removes the child's worktree; but it marked the session as dropped ("report nothing more"), and that also
    swallowed the one thing still worth saying: the worktree is gone."""
    sid = root(wtenv)
    kid = child(wtenv, sid, "a", isolation="worktree")["session_id"]
    tick(wtenv)
    path = show(wtenv, kid)["worktree"]
    assert os.path.isdir(path)
    wtenv.clock.advance(nodes.NODE_STALE_SECONDS + 1)
    assert wtenv.human.post(f"/api/hosted-sessions/{kid}/close").status_code == 200
    assert show(wtenv, kid)["state"] == "ended" and show(wtenv, kid)["worktree_removed_at"] is None
    wtenv.node.hello()
    tick(wtenv, times=4)
    assert not os.path.exists(path), "the node removed it"
    view = show(wtenv, kid)
    assert view["worktree_removed_at"] is not None, "... and the hub knows"
    assert "(removed by its node after the session ended)" in cli._fmt_hosted_detail(view)
    assert kid not in wtenv.node.sessions.dropped and show(wtenv, sid)["state"] != "ended"


def test_a_node_refuses_an_access_word_on_a_session_that_has_no_parent(senv):
    """R1. The parent and ceiling checks are for an entry that names a parent; the access word was applied either
    way. This hub never sends one for a root, but the node is where it is enforced: an entry with a word and no
    parent is refused. A root runs as its own runner says."""
    from agent_comms.node_sessions import ROOT_ACCESS

    node = full_node(senv, "r1")
    for word in ("full", "workspace", "read-only"):
        sid = start(senv)["session_id"]
        runner = node.sessions
        for malformed in ({"access": word}, {"access": word, "parent": None}, {"access": word, "parent": 7}):
            data = runner._sync(0)
            [entry] = [x for x in data["sessions"] if x["session_id"] == sid]
            entry.pop("parent", None)
            entry |= malformed
            runner._reconcile(data)
            assert sid not in runner.host.sessions, (word, malformed)
            runner.ending.discard(sid)
            runner.pending = []
        tick(senv, node, times=2)                        # the hub's own entry: no word, so it opens
        assert node.sessions.host.sessions[sid].access == "full" and not node.session_spawner.calls[:0]
        data = runner._sync(0)
        assert [x for x in data["sessions"] if x["session_id"] == sid][0]["access"] is None
        assert senv.human.post(f"/api/hosted-sessions/{sid}/close").status_code == 200
        tick(senv, node, times=3)
    # What the node says when it refuses: the session ends with the reason, and nothing was started.
    plain = make_node(senv, config=tree_config(senv, sessions={"allow_full_access": True}), log_dir=senv.tmp / "r1b")
    plain.hello()
    sid = start(senv)["session_id"]
    data = plain.sessions._sync(0)
    data["sessions"][0]["access"] = "full"
    plain.sessions._reconcile(data)
    assert [(x, e["kind"], e["detail"]) for x, e in plain.sessions.pending] == [
        (sid, "ended", f"this node cannot host the session: ValueError: {ROOT_ACCESS}"[:300])]
    assert plain.session_spawner.calls == [] and sid not in plain.sessions.host.sessions
    senv.node.hello()


def test_a_codex_child_whose_thread_has_a_wider_sandbox_than_it_was_started_at_is_ended(senv):
    """R2. Codex's answer to thread/start names the sandbox the thread has. For a session started at an access
    word, a sandbox wider than that word ends it (read-only < workspace < full), as Claude's kill switch does. A
    session with no word of its own (a root) is held to what it always was: full or not full."""
    v = access.verify_codex
    wider = access.CODEX_WIDER
    assert v("read-only", "workspaceWrite", "read-only") == (wider.format(sandbox="workspaceWrite", launched="read-only"), False)
    assert v("read-only", "dangerFullAccess", "read-only")[0] is not None
    assert v("workspace", "dangerFullAccess", "workspace")[0] is not None
    assert v("read-only", "readOnly", "read-only") == (None, False) == v("workspace", "readOnly", "workspace")
    assert v("workspace", "workspaceWrite", "workspace") == (None, False) == v("read-only", None, "read-only")
    assert v("full", "dangerFullAccess", "full") == (None, True)
    assert v("read-only", "workspaceWrite") == (None, False), "a root: as before"
    assert v("read-only", "externalSandbox", "read-only") == (None, False), "a sandbox this code has no word for"
    sid = root(senv)
    answer(senv, sid)
    kid = child(senv, sid, "count", agent="codex", access="read-only")["session_id"]
    tick(senv)
    proc = codex_proc(senv)
    proc.emit(cx.reply(1, cx.INIT_RESULT))
    tick(senv)
    assert written(proc)[2]["params"]["sandbox"] == "read-only"
    proc.emit(cx.reply(2, cx.THREAD_RESULT | {"sandbox": {"type": "workspaceWrite"}}))
    tick(senv, times=3)
    view = show(senv, kid)
    assert view["state"] == "ended" and view["error"] == wider.format(sandbox="workspaceWrite", launched="read-only")
    assert msg(senv, kid, 1)["state"] == "failed" and msg(senv, kid, 1)["delivered_at"] is None
    assert not any(m.get("method") == "turn/start" for m in written(proc)), "its turn was never written"


def test_private_files_of_sessions_the_hub_no_longer_lists_are_removed(senv, tmp_path):
    """R3. `<id>.settings.json` holds the runner's settings (credentials, possibly) and `<id>.mcp.json` the board's
    launcher. They went with the session when it was forgotten here; a session closed while its node was down is
    never opened again, so its copies stayed for good. Now the node removes those of every session the hub does
    not list for it. Never a file of a listed or open session, never anything but those two kinds."""
    own = tmp_path / "own.json"
    own.write_text(json.dumps({"permissions": {"allow": ["Read"]}, "env": {"KEY": "value"}}))
    runner = ["claude", "-p", "{prompt}", "--permission-mode", "dontAsk", "--settings", str(own)]
    config = lambda: tree_config(senv, runners={"claude-code": runner})    # noqa: E731
    node = make_node(senv, config=config(), log_dir=senv.tmp / "r3-logs")
    node.hello()
    gone, kept = start(senv)["session_id"], start(senv, project=P2)["session_id"]
    tick(senv, node)
    sessions = senv.tmp / "r3-logs" / "sessions"
    assert {x.name for x in sessions.glob("*.settings.json")} == {f"{gone}.settings.json", f"{kept}.settings.json"}
    senv.clock.advance(nodes.NODE_STALE_SECONDS + 1)      # the node is down; the human closes one session
    assert senv.human.post(f"/api/hosted-sessions/{gone}/close").status_code == 200
    assert show(senv, gone)["state"] == "ended"
    (sessions / f"{gone}.mcp.json").write_text("{}")      # (as a read-only session of it would have had)
    bystanders = [sessions / f"{gone}.jsonl", sessions / f"{gone}.stderr.log", sessions / "notes.settings.json",
                  sessions / f"{UNKNOWN}.other.json", sessions / f"x{gone}.settings.json"]
    for path in bystanders:
        path.write_text("kept")
    back = make_node(senv, config=config(), log_dir=senv.tmp / "r3-logs")      # the node starts again
    back.hello()
    tick(senv, back)
    assert not (sessions / f"{gone}.settings.json").exists() and not (sessions / f"{gone}.mcp.json").exists()
    assert (sessions / f"{kept}.settings.json").exists(), "a session the hub lists keeps its file"
    assert all(path.exists() for path in bystanders), "and nothing else is touched"
    assert (own.read_text(), kept in back.sessions.host.sessions) == (json.dumps(
        {"permissions": {"allow": ["Read"]}, "env": {"KEY": "value"}}), True)
    senv.node.hello()


def test_a_child_that_only_waits_for_its_parent_does_not_keep_the_node_busy(senv):
    """R4. A held full child has a message queued at the hub, for as long as its parent has no turn: hours,
    possibly. That used to count as work, so the node ticked five times a second and fetched every second all that
    time. A child that only waits is not activity; the parent's own turn is, and brings the child's with it."""
    node = full_node(senv, "r4")
    sid = root(senv, node=node)
    answer(senv, sid, node=node)
    kid = child(senv, sid, "one")["session_id"]
    tick(senv, node)
    answer(senv, kid, node=node)
    claimed(senv, sid)
    runner, host = node.sessions, node.sessions.host
    assert host.park(sid)                                # the parent's process is stopped (the idle limit)
    tick(senv, node, times=3)
    send(senv, kid, "two")
    tick(senv, node, times=3)
    assert msg(senv, kid, 2)["state"] == "queued" and [m["n"] for m in runner.queue[kid]] == [2]
    assert not runner.active(), "held: nothing to do until the parent has a turn"
    send(senv, sid, "again")
    tick(senv, node)
    assert runner.active()
    begin(senv, sid, node)
    tick(senv, node)
    assert msg(senv, kid, 2)["state"] == "delivered", "and then promptly"
    # A queued message that is NOT held is work, as ever.
    other = child(senv, sid, "three", access="read-only")["session_id"]
    runner._reconcile(runner._sync(0))
    assert runner.queue[other] and runner.active()
    senv.node.hello()
