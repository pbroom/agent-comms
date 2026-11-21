"""The Codex adapter (providers/codex.py): the app-server command and thread parameters from a `codex exec` runner
template, and what each line of the app-server protocol means. The messages are the ones codex-cli 0.171.0 wrote in
the spike (DESIGN_NOTES "Session host: Codex adapter"), cut down to the fields that matter. No Codex is started."""

import json
from pathlib import Path

import pytest

from agent_comms import providers
from agent_comms.dispatch import DispatchConfig, with_writable_roots
from agent_comms.providers import Event
from agent_comms.providers.codex import BYPASS_REFUSED, NEW_THREAD, OPT_OUT, CodexSession, session_command

ROOT = Path(__file__).resolve().parents[1]
SID = "0a00c308-20b7-40fc-9a1c-19b8bc832d6d"            # the hub's session id
THREAD = "01a1fe39-5514-77e3-ba96-db73b26019fe"         # Codex's own id for it (a UUID it issues)
TURN = "01a1fe39-5945-7782-b043-ba8a65280ee8"
CHILD, CHILD_TURN = "01a1fe3c-8ce8-7290-90e5-c94c043adeaa", "01a1fe3c-8cf0-7583-b267-81b961155e03"
WORK = "/Users/someone/work/checkout"
APPROVE = 'mcp_servers.agent-comms.tools.board_post.approval_mode="approve"'
RUNNER = ["codex", "exec", "--cd", "{project}", "--sandbox", "workspace-write", "-c", APPROVE, "{prompt}"]

INIT_RESULT = {"userAgent": "agent-comms/0.171.0 (Mac OS 27.0.1; arm64) unknown (agent-comms; 0.1.0)",
               "codexHome": "/Users/someone/.codex", "platformFamily": "unix", "platformOs": "macos"}
THREAD_RESULT = {
    "thread": {"id": THREAD, "sessionId": THREAD, "preview": "", "ephemeral": False, "status": {"type": "idle"},
               "path": f"/Users/someone/.codex/sessions/2026/11/21/rollout-2026-11-21T08-08-52-{THREAD}.jsonl",
               "cwd": WORK, "cliVersion": "0.171.0", "originator": "agent-comms", "source": "vscode", "turns": []},
    "model": "gpt-6-luna", "modelProvider": "openai", "cwd": WORK, "approvalPolicy": "never",
    "approvalsReviewer": "user", "sandbox": {"type": "readOnly", "networkAccess": False}, "reasoningEffort": "high"}
# What follows thread/start without being asked for: none of it carries this thread's id at the top level.
THREAD_STARTED = {"method": "thread/started", "params": {"thread": THREAD_RESULT["thread"]}}
TURN_REPLY = {"turn": {"id": TURN, "rootTurnId": TURN, "items": [], "itemsView": "notLoaded", "status": "inProgress",
                       "error": None, "startedAt": None, "completedAt": None, "durationMs": None}}
TURN_STARTED = {"method": "turn/started", "params": {"threadId": THREAD, "turn": TURN_REPLY["turn"]}}
NO_ROLLOUT = {"code": -32600, "message": f"no rollout found for thread id {THREAD}"}


def line(message) -> bytes:
    return json.dumps(message).encode()


def reply(rid, result=None, error=None) -> bytes:
    return line({"id": rid, "result": result} if error is None else {"id": rid, "error": error})


def message(text, phase="final_answer", thread=THREAD, turn=TURN) -> dict:
    return {"method": "item/completed", "params": {"threadId": thread, "turnId": turn, "completedAtMs": 1795266534914,
                                                   "item": {"type": "agentMessage", "id": "msg_0541684f", "text": text,
                                                            "phase": phase, "memoryCitation": None}}}


def completed(status="completed", items=(), error=None, thread=THREAD, turn=TURN) -> dict:
    return {"method": "turn/completed", "emittedAtMs": 1795266534937, "params": {"threadId": thread, "turn": {
        "id": turn, "rootTurnId": turn, "items": list(items), "itemsView": "summary" if items else "notLoaded",
        "status": status, "error": error, "startedAt": 1795266533, "completedAt": 1795266534, "durationMs": 1227}}}


def sent(writes) -> list[dict]:
    assert all(w.endswith(b"\n") and w.count(b"\n") == 1 for w in writes), "one message is exactly one line"
    return [json.loads(w) for w in writes]


def session(resume=False, model=None, template=RUNNER, thread=None):
    p = CodexSession()
    p.runs_in(WORK)         # the host starts the process there (SessionHost.open)
    p.adopt(thread)
    return p, p.argv(template, session_id=SID, resume=resume, model=model)


def ready(**kw) -> CodexSession:
    """A session whose process finished its handshake: initialize, initialized, thread/start."""
    p, _ = session(**kw)
    p.on_open()
    p.feed(reply(1, INIT_RESULT))
    events, _ = p.feed(reply(2, THREAD_RESULT))
    assert events == [Event("ready", provider_id=THREAD)]
    return p


def in_turn(text="hello", **kw) -> CodexSession:
    """... and with a turn in flight that Codex has named (request 3)."""
    p = ready(**kw)
    p.turn(text)
    assert p.feed(reply(3, TURN_REPLY)) == ([], [])
    assert p.feed(line(TURN_STARTED)) == ([Event("turn_started")], [])
    return p


# ------------------------------------------------------------ the command


def test_the_session_command_is_app_server_with_every_override_kept_and_the_rest_as_thread_parameters():
    argv, params = session_command(RUNNER)
    assert argv == ["codex", "app-server", "-c", APPROVE, "--listen", "stdio://"]
    assert params == {"sandbox": "workspace-write", "approvalPolicy": "never"}
    assert "exec" not in argv and "{prompt}" not in argv and "{project}" not in argv and "--cd" not in argv


def test_the_shipped_codex_runner_can_be_hosted_with_all_its_board_tool_approvals():
    shipped = DispatchConfig.load(ROOT / "board.toml", local=False).runners["codex-cli"]
    assert shipped[:6] == ["codex", "exec", "--cd", "{project}", "--sandbox", "workspace-write"]
    argv, params = session_command(shipped)
    overrides = [shipped[i + 1] for i, a in enumerate(shipped) if a == "-c"]
    assert len(overrides) > 20 and [argv[i + 1] for i, a in enumerate(argv) if a == "-c"] == overrides
    assert argv[:2] == ["codex", "app-server"] and argv[-2:] == ["--listen", "stdio://"]
    assert len(argv) == 2 + 2 * len(overrides) + 2, "nothing else is passed on"
    assert params == {"sandbox": "workspace-write", "approvalPolicy": "never"}, "no cwd, no instructions of ours"


def test_model_and_sandbox_are_read_in_every_spelling_and_the_hubs_model_wins():
    for own in (["-m", "gpt-6-sol"], ["--model", "gpt-6-sol"], ["--model=gpt-6-sol"], ["-mgpt-6-sol"]):
        assert session_command(["codex", "exec", *own, "{prompt}"])[1]["model"] == "gpt-6-sol"
        argv, params = session_command(["codex", "exec", *own, "{prompt}"], "gpt-6-luna")
        assert params["model"] == "gpt-6-luna" and not any("sol" in a for a in argv)
    assert "model" not in session_command(["codex", "exec", "{prompt}"])[1], "Codex's own default"
    for flags in (["-s", "workspace-write"], ["--sandbox=workspace-write"], ["-c", 'sandbox_mode="workspace-write"'],
                  ["-c", "sandbox_mode=read-only", "--sandbox", "workspace-write"]):
        assert session_command(["codex", "exec", *flags, "{prompt}"])[1]["sandbox"] == "workspace-write"
    assert session_command(["codex", "exec", "{prompt}"])[1]["sandbox"] == "read-only", "as `codex exec` without it"


def test_the_run_directory_is_never_sent_on_thread_start_and_cd_may_only_name_it():
    """A `cwd` on thread/start makes Codex 0.171.0 record that directory as trusted in the user's config.toml under
    the workspace-write sandbox (seen live, in a temporary Codex home). `codex exec` sends none; neither do we: the
    process is started in the run directory, which is then the thread's."""
    for flags in (["-C", "{project}"], ["--cd={project}"], ["--cd", "{project}"], ["--cd", WORK], ["-C", WORK + "/"],
                  []):
        argv, params = session_command(["codex", "exec", *flags, "--sandbox", "workspace-write", "{prompt}"], cwd=WORK)
        assert "cwd" not in params and WORK not in argv and "--cd" not in argv and "-C" not in argv, flags
    for flags, cwd in ((["--cd", "/Users/someone/elsewhere"], WORK), (["--cd", WORK], None),
                       (["--cd", "relative/dir"], WORK), (["-C", "{thread}"], WORK)):
        with pytest.raises(ValueError, match=r"--cd must be \{project\}"):
            session_command(["codex", "exec", *flags, "{prompt}"], cwd=cwd)
    p, _ = session()
    p.on_open()
    start = sent(p.feed(reply(1, INIT_RESULT))[1])[1]
    assert start["method"] == "thread/start" and "cwd" not in start["params"]


def test_features_strict_config_and_every_spelling_of_an_override_are_kept_and_exec_only_flags_dropped():
    argv, params = session_command(["/opt/bin/codex", "--disable", "plugins", "exec", "--enable=goals", "-ca=1",
                                    "--config", "b=2", "--config=c=3", "--strict-config", "--skip-git-repo-check",
                                    "--json", "--color", "never", "{prompt}"])
    assert argv == ["/opt/bin/codex", "app-server", "--disable", "plugins", "--enable", "goals", "-c", "a=1",
                    "-c", "b=2", "-c", "c=3", "--strict-config", "--listen", "stdio://"]
    assert params["approvalPolicy"] == "never"


def test_no_developer_instructions_are_injected_so_the_node_owners_own_stay_in_force():
    """`developerInstructions` on thread/start REPLACES the `developer_instructions` of the owner's Codex config.
    The approval policy and the refusals already keep the session unattended; nothing is added to the prompt."""
    for template in (RUNNER, ["codex", "exec", "-c", 'developer_instructions="mine"', "{prompt}"]):
        argv, params = session_command(template, cwd=WORK)
        assert "developerInstructions" not in params and "baseInstructions" not in params
    assert 'developer_instructions="mine"' in argv, "the runner's own override is passed on like any other"
    for resume in (False, True):
        p, _ = session(resume=resume, thread=THREAD)
        p.on_open()
        assert "developerInstructions" not in sent(p.feed(reply(1, INIT_RESULT))[1])[1]["params"]


def test_writable_roots_reach_a_hosted_codex_session_as_the_same_override_a_dispatched_run_gets():
    template = with_writable_roots(RUNNER, ["/Users/someone/extra"])
    argv, _ = session_command(template)
    assert 'sandbox_workspace_write.writable_roots=["/Users/someone/extra"]' in argv


@pytest.mark.parametrize("extra", [["--dangerously-bypass-approvals-and-sandbox"], ["--approve-for-me"],
                                   ["--sandbox", "danger-full-access"], ["-s=danger-full-access"],
                                   ["-c", 'sandbox_mode="danger-full-access"'], ["--config=sandbox_mode=danger-full-access"]])
def test_a_runner_that_turns_the_sandbox_or_approvals_off_cannot_be_hosted(extra):
    with pytest.raises(ValueError) as e:
        session_command(["codex", "exec", *extra, "{prompt}"])
    assert str(e.value) == BYPASS_REFUSED


@pytest.mark.parametrize("extra, why", [
    (["--profile", "work"], "--profile"), (["-p", "work"], "-p"), (["--add-dir", "/tmp/x"], "--add-dir"),
    (["--ephemeral"], "--ephemeral"), (["--oss"], "--oss"), (["-i", "shot.png"], "-i"),
    (["--output-last-message", "/tmp/out"], "--output-last-message"), (["--remote", "ws://x"], "--remote"),
    (["--ignore-user-config"], "--ignore-user-config"), (["--worktree"], "--worktree"),
    (["resume", "--last"], "extra positional"), (["some words"], "extra positional"),
    (["--sandbox", "bogus"], "sandbox must be one of"), (["--sandbox"], "has no value"),
    (["-c", "novalue"], "not key=value"), (["--enable", "a b"], "does not name a feature"),
    (["--cd", "relative/dir"], "--cd must be"), (["-m", "--dangerously-bypass"], "plain model name")])
def test_a_flag_the_adapter_does_not_understand_is_refused_never_passed_on(extra, why):
    with pytest.raises(ValueError, match=why):
        session_command(["codex", "exec", "{prompt}", *extra])


def test_a_refusal_names_the_flag_and_never_its_value():
    with pytest.raises(ValueError) as e:
        session_command(["codex", "exec", "--output-last-message=/Users/someone/secret/path", "{prompt}"])
    assert "--output-last-message" in str(e.value) and "secret" not in str(e.value)


def test_only_a_codex_exec_runner_and_a_real_session_id_make_a_command():
    for template in (["claude", "-p", "{prompt}"], [], ["codex", "{prompt}"], ["codex", "review", "{prompt}"],
                     ["codex-wrapper", "exec", "{prompt}"]):
        with pytest.raises(ValueError):
            CodexSession().argv(template, session_id=SID, resume=False, model=None)
    with pytest.raises(ValueError):
        CodexSession().argv(RUNNER, session_id="--listen", resume=False, model=None)
    with pytest.raises(ValueError):
        CodexSession().argv(RUNNER, session_id=SID, resume=False, model="--dangerously-bypass-approvals-and-sandbox")


def test_the_seam_offers_the_codex_adapter_only_where_the_machine_turned_it_on():
    assert providers.provider_for(RUNNER) is None, "off by default: a Codex runner cannot be hosted, as before"
    assert providers.provider_for(RUNNER, codex=False) is None
    assert providers.provider_for(RUNNER, codex=True) is CodexSession
    assert providers.provider_for(["/usr/local/bin/codex", "exec", "{prompt}"], codex=True) is CodexSession
    assert providers.provider_for(["codex-wrapper", "exec", "{prompt}"], codex=True) is None
    from agent_comms.providers.claude import ClaudeSession
    assert providers.provider_for(["claude", "-p", "{prompt}"], codex=True) is ClaudeSession
    assert Event("start_failed", detail="x").kind == "start_failed"


def test_the_codex_switch_is_parsed_strictly_and_ships_off():
    assert DispatchConfig.from_dict({}).sessions.codex is False
    assert DispatchConfig.from_dict({"sessions": {"codex": True}}).sessions.codex is True
    for bad in ("yes", 1, None, ["codex"]):
        with pytest.raises(ValueError, match="codex must be true or false"):
            DispatchConfig.from_dict({"sessions": {"codex": bad}})
    assert DispatchConfig.load(ROOT / "board.toml", local=False).sessions.codex is False


# ------------------------------------------------------------ the handshake


def test_the_handshake_is_initialize_then_initialized_and_thread_start():
    p, argv = session(model="gpt-6-luna")
    assert p.waits_for_ready is True and argv[:2] == ["codex", "app-server"]
    [init] = sent(p.on_open())
    assert init == {"id": 1, "method": "initialize", "params": {
        "clientInfo": {"name": "agent-comms", "version": "0.1.0"},
        "capabilities": {"optOutNotificationMethods": list(OPT_OUT)}}}
    assert "jsonrpc" not in init and "item/agentMessage/delta" in OPT_OUT
    events, writes = p.feed(reply(1, INIT_RESULT))
    assert events == []
    initialized, start = sent(writes)
    assert initialized == {"method": "initialized"}
    assert start == {"id": 2, "method": "thread/start", "params": {
        "sandbox": "workspace-write", "approvalPolicy": "never", "model": "gpt-6-luna"}}
    assert p.feed(reply(2, THREAD_RESULT)) == ([Event("ready", provider_id=THREAD)], [])
    assert p.feed(line(THREAD_STARTED)) == ([], []) and p.thread_id == THREAD


def test_a_restarted_process_resumes_the_thread_it_was_handed_and_never_starts_one():
    p, _ = session(resume=True, thread=THREAD)
    p.on_open()
    _, writes = p.feed(reply(1, INIT_RESULT))
    resume = sent(writes)[1]
    assert resume["method"] == "thread/resume"
    # The directory this node resolves NOW: without it Codex goes on where the thread was created (seen live), which
    # may be an old checkout. On a resume a `cwd` records no trust (seen live, both sandboxes).
    assert resume["params"] == {"threadId": THREAD, "excludeTurns": True, "sandbox": "workspace-write",
                                "approvalPolicy": "never", "cwd": WORK}
    assert p.feed(reply(2, THREAD_RESULT)) == ([Event("ready", provider_id=THREAD)], [])
    assert p.session_exists("") is None
    # The same adapter starts the next process too: per-process state starts over, the thread stays.
    p.argv(RUNNER, session_id=SID, resume=True, model=None)
    assert sent(p.on_open())[0]["id"] == 1
    assert sent(p.feed(reply(1, INIT_RESULT))[1])[1]["params"]["threadId"] == THREAD


def test_a_thread_codex_has_no_record_of_fails_the_start_loudly_and_the_next_one_is_new():
    p, _ = session(resume=True, thread=THREAD)
    p.on_open()
    p.feed(reply(1, INIT_RESULT))
    [ev], writes = p.feed(reply(2, error=NO_ROLLOUT))
    assert ev.kind == "start_failed" and writes == []
    assert THREAD in ev.detail and NEW_THREAD in ev.detail and "no rollout found" in ev.detail
    assert p.session_exists("") is False and p.thread_id is None, "the host flips to a fresh start"
    assert p.feed(reply(2, THREAD_RESULT)) == ([], []), "nothing more of this process is read"
    p.argv(RUNNER, session_id=SID, resume=False, model=None)
    assert p.session_exists("") is None
    p.on_open()
    assert sent(p.feed(reply(1, INIT_RESULT))[1])[1]["method"] == "thread/start"


def test_resume_without_a_recorded_thread_id_is_the_same_loud_failure():
    for handed in (None, "not-a-thread-id", "", 7):
        p, _ = session(resume=True, thread=handed)
        p.on_open()
        [ev], writes = p.feed(reply(1, INIT_RESULT))
        assert ev.kind == "start_failed" and "no record of the Codex thread" in ev.detail and "NEW thread" in ev.detail
        assert writes == [] and p.session_exists("") is False, "no thread/resume is written for an id it does not have"


@pytest.mark.parametrize("error, why", [
    ({"code": -32600, "message": "invalid session id: invalid character: expected an optional prefix"}, "invalid"),
    ({"code": -32603, "message": "failed to read the rollout:\n  disk error"}, "failed to read the rollout: disk"),
    ({"code": -32603, "message": "x" * 500}, "x" * 120),
    ({"code": -32603}, "no reason given"), ("boom", "no reason given")])
def test_any_refused_resume_gives_the_thread_up_and_says_the_next_message_starts_a_new_one(error, why):
    """Not only "no rollout found": a refusal that does not go away would fail every later message the same way."""
    p, _ = session(resume=True, thread=THREAD)
    p.on_open()
    p.feed(reply(1, INIT_RESULT))
    [ev], _ = p.feed(reply(2, error=error))
    assert ev.kind == "start_failed" and why in ev.detail and THREAD in ev.detail and len(ev.detail) <= 300
    assert ev.detail.endswith(NEW_THREAD), "never silently"
    assert p.session_exists("") is False and p.thread_id is None, "decided before the failure is reported"
    p.argv(RUNNER, session_id=SID, resume=False, model=None)
    p.on_open()
    assert sent(p.feed(reply(1, INIT_RESULT))[1])[1]["method"] == "thread/start"


def test_a_refused_handshake_or_thread_start_fails_the_start():
    p, _ = session()
    p.on_open()
    [ev], writes = p.feed(reply(1, error={"code": -32600, "message": "Already initialized"}))
    assert (ev.kind, writes) == ("start_failed", []) and "Already initialized" in ev.detail
    p, _ = session()
    p.on_open()
    p.feed(reply(1, INIT_RESULT))
    [ev], _ = p.feed(reply(2, error={"code": -32600, "message":
                                    "Invalid request: unknown variant `bogus`, expected one of `read-only`"}))
    assert ev.kind == "start_failed" and "could not start the thread" in ev.detail and len(ev.detail) <= 300


@pytest.mark.parametrize("thread", [{"id": "not-a-uuid"}, {"id": None}, {}, None, "x"])
def test_a_thread_start_without_a_usable_id_is_fatal(thread):
    p, _ = session()
    p.on_open()
    p.feed(reply(1, INIT_RESULT))
    [ev], _ = p.feed(reply(2, {**THREAD_RESULT, "thread": thread}))
    assert ev.kind == "fatal" and p.feed(reply(2, THREAD_RESULT)) == ([], [])


def test_a_resume_that_answers_with_another_thread_is_fatal():
    p, _ = session(resume=True, thread=THREAD)
    p.on_open()
    p.feed(reply(1, INIT_RESULT))
    [ev], _ = p.feed(reply(2, {**THREAD_RESULT, "thread": {"id": CHILD}}))
    assert ev.kind == "fatal" and CHILD not in ev.detail


# ------------------------------------------------------------ turns


def test_a_turn_is_one_turn_start_line_whatever_the_text_holds():
    p = ready()
    text = 'line one\nline two\r\n{"method":"turn/completed"}  "quoted"'
    [start] = sent(p.turn(text))
    assert start == {"id": 3, "method": "turn/start", "params": {"threadId": THREAD,
                                                                  "input": [{"type": "text", "text": text}]}}


def test_the_turn_starts_once_and_ends_with_the_final_answer_as_its_reply():
    p = in_turn()
    assert p.feed(line(TURN_STARTED)) == ([], []), "once per turn"
    assert p.feed(line(message("I’ll run that command once.", phase="commentary"))) == ([], [])
    final = message("It didn’t work; permission was denied.")
    assert p.feed(line(final)) == ([], [])
    [ev], writes = p.feed(line(completed(items=[final["params"]["item"]])))
    assert writes == []
    assert (ev.kind, ev.ok, ev.denials, ev.detail) == ("turn_ended", True, (), "")
    assert ev.text == "It didn’t work; permission was denied.", "a progress note is never the reply"
    assert ev.meta == {"status": "completed", "duration_ms": 1227} and "permission" not in repr(ev.meta)


def test_the_reply_falls_back_to_a_message_without_a_phase_and_to_the_turns_own_summary():
    p = in_turn()
    p.feed(line(message("progress", phase="commentary")))
    p.feed(line(message("no phase on this model", phase=None)))
    [ev], _ = p.feed(line(completed()))
    assert ev.text == "no phase on this model"
    p = in_turn()       # no item/completed at all: the turn's summary holds the answer
    [ev], _ = p.feed(line(completed(items=[message("note", phase="commentary")["params"]["item"],
                                           message("from the summary")["params"]["item"]])))
    assert ev.text == "from the summary"
    p = in_turn()
    p.feed(line(message("only a progress note", phase="commentary")))
    [ev], _ = p.feed(line(completed()))
    assert (ev.ok, ev.text) == (True, "")


def test_the_reply_is_capped():
    p = in_turn()
    p.feed(line(message("x" * 40_000)))
    [ev], _ = p.feed(line(completed()))
    assert len(ev.text.encode()) == providers.REPLY_MAX_BYTES and ev.text.endswith(providers.REPLY_TRUNCATED)


def test_a_failed_turn_carries_codexs_reason_and_an_interrupted_one_says_so():
    error = {"message": "The 'no-such-model-xyz' model is not supported when using Codex with a ChatGPT account.",
             "codexErrorInfo": "modelNotFound", "additionalDetails": None, "misalignment": None}
    p = in_turn()
    assert p.feed(line({"method": "thread/status/changed", "params": {"threadId": THREAD,
                                                                      "status": {"type": "systemError"}}})) == ([], [])
    assert p.feed(line({"method": "error", "params": {"error": error, "willRetry": False, "threadId": THREAD,
                                                      "turnId": TURN}})) == ([], []), "the turn's end says it"
    [ev], _ = p.feed(line(completed("failed", error=error)))
    assert (ev.kind, ev.ok, ev.text) == ("turn_ended", False, "") and ev.detail == error["message"]
    assert ev.meta == {"status": "failed", "duration_ms": 1227, "error_info": "modelNotFound"}

    p = in_turn()
    [ev], _ = p.feed(line(completed("failed", error={"message": "x", "codexErrorInfo": {
        "responseStreamDisconnected": {"httpStatusCode": 502}}})))
    assert ev.meta["error_info"] == "responseStreamDisconnected"

    p = in_turn()
    [ev], _ = p.feed(line(completed("interrupted")))
    assert (ev.ok, ev.detail, ev.meta["status"]) == (False, "the turn was interrupted", "interrupted")
    p = in_turn()
    assert p.feed(line(completed("inProgress"))) == ([], []), "not an end"


def test_a_refused_turn_start_ends_the_turn_failed():
    p = ready()
    p.turn("hello")
    [ev], writes = p.feed(reply(3, error={"code": -32600, "message": f"thread not found: {THREAD}"}))
    assert (ev.kind, ev.ok, writes) == ("turn_ended", False, []) and "thread not found" in ev.detail
    assert p.interrupt() is None and p.feed(line(completed())) == ([], []), "there is no turn left to end"


def test_the_interrupt_is_in_band_once_codex_has_named_the_turn():
    p = ready()
    assert p.interrupt() is None, "no turn"
    p.turn("hello")
    assert p.interrupt() is None, "Codex has not named the turn yet: the host stops the process instead"
    p.feed(reply(3, TURN_REPLY))
    [stop] = sent(p.interrupt())
    assert stop == {"id": 4, "method": "turn/interrupt", "params": {"threadId": THREAD, "turnId": TURN}}
    assert p.feed(reply(4, {})) == ([], [])
    [ev], _ = p.feed(line(completed("interrupted")))
    assert (ev.kind, ev.ok) == ("turn_ended", False)
    assert p.interrupt() is None
    assert p.feed(reply(5, error={"code": -32600, "message": "no active turn to interrupt"})) == ([], [])


def test_turn_started_names_the_turn_when_it_comes_before_the_reply():
    p = ready()
    p.turn("hello")
    assert p.feed(line(TURN_STARTED)) == ([Event("turn_started")], [])
    assert sent(p.interrupt())[0]["params"]["turnId"] == TURN
    assert p.feed(reply(3, TURN_REPLY)) == ([], [])
    assert p.feed(line(completed()))[0][0].kind == "turn_ended"


def test_a_late_answer_to_an_earlier_turns_request_never_names_the_next_turn():
    """Turn A is named by its `turn/started` and ends before the answer to its `turn/start` arrives. Turn B is sent.
    A's answer must not make A's id B's: B's own end would be ignored and its interrupt would name A."""
    p = ready()
    p.turn("A")                                                             # request 3
    assert p.feed(line(TURN_STARTED)) == ([Event("turn_started")], [])
    p.feed(line(message("A's answer")))
    assert p.feed(line(completed()))[0][0].text == "A's answer"
    p.turn("B")                                                             # request 4
    assert p.feed(reply(3, TURN_REPLY)) == ([], []), "A's request was retired with A"
    assert p.interrupt() is None, "B has no id yet: A's is not taken for it"
    b = {"turn": {**TURN_REPLY["turn"], "id": CHILD_TURN}}
    assert p.feed(reply(4, b)) == ([], [])
    assert sent(p.interrupt())[0]["params"]["turnId"] == CHILD_TURN
    p.feed(line(message("B's answer", turn=CHILD_TURN)))
    [ev], _ = p.feed(line(completed(turn=CHILD_TURN)))
    assert (ev.kind, ev.ok, ev.text) == ("turn_ended", True, "B's answer")


def test_a_refusal_of_an_earlier_turns_request_does_not_fail_the_next_turn():
    p = ready()
    p.turn("A")
    p.feed(line(TURN_STARTED))
    p.feed(line(completed("interrupted")))
    p.turn("B")
    assert p.feed(reply(3, error={"code": -32600, "message": "too late"})) == ([], [])
    assert p.feed(reply(4, {"turn": {**TURN_REPLY["turn"], "id": CHILD_TURN}})) == ([], [])
    assert p.feed(line(completed(turn=CHILD_TURN)))[0][0].ok is True


def test_the_answer_to_the_turns_own_request_is_what_names_it():
    """Also when a `turn/started` came first: a `turn/start` written while Codex ran a turn of its own is answered
    with THAT turn, and it is that turn's end the message then waits for."""
    p = ready()
    p.turn("hello")
    p.feed(line(TURN_STARTED))
    assert p.feed(reply(3, {"turn": {**TURN_REPLY["turn"], "id": CHILD_TURN}})) == ([], [])
    assert sent(p.interrupt())[0]["params"]["turnId"] == CHILD_TURN


# ------------------------------------------------------------ a turn Codex begins by itself


def own_turn(method, turn=CHILD_TURN, status="inProgress") -> bytes:
    return line({"method": method, "params": {"threadId": THREAD, "turn": {"id": turn, "status": status, "items": []}}})


def test_a_turn_codex_begins_in_the_sessions_thread_holds_the_next_message_until_it_is_over():
    """Between two messages Codex may run a turn nobody here started. A `turn/start` written then is added to THAT
    turn and answered with it (seen live), so the adapter says to hold, and reports nothing of the turn."""
    p = ready()
    assert p.holds_turns() is False
    assert p.feed(own_turn("turn/started")) == ([], []) and p.holds_turns() is True
    assert p.feed(line(message("not a reply to anyone", turn=CHILD_TURN))) == ([], [])
    assert p.feed(own_turn("turn/completed", turn=TURN, status="completed")) == ([], []) and p.holds_turns() is True, \
        "another turn's end is not this one's"
    assert p.feed(own_turn("turn/completed", status="inProgress")) == ([], []) and p.holds_turns() is True
    assert p.feed(own_turn("turn/completed", status="completed")) == ([], []) and p.holds_turns() is False
    p.turn("hello")
    p.feed(reply(3, TURN_REPLY))
    p.feed(line(message("the answer")))
    assert p.feed(line(completed()))[0][0].text == "the answer", "the held turn's message left nothing behind"


@pytest.mark.parametrize("params", [
    {}, {"turn": None}, {"turn": "junk"}, {"turn": []}, {"turn": 7}, {"turn": {}}, {"turn": {"id": None}},
    {"turn": {"id": 7, "status": "completed"}}, {"turn": {"status": "completed"}}, {"status": "idle"},
    {"status": None}, {"status": ["idle"]}, {"turn": {"id": ["x"]}}])
def test_a_malformed_notification_between_turns_is_ignored_and_never_raises(params):
    """An exception in `feed` ends the whole session (the host's pass catches it and closes the session)."""
    for method in ("turn/completed", "turn/started", "thread/status/changed", "item/completed"):
        p = ready()         # idle: no turn of its own running
        assert p.feed(line({"method": method, "params": {"threadId": THREAD, **params}})) == ([], [])
        assert p.holds_turns() is False
        p.feed(own_turn("turn/started"))    # ... and with one running: a malformed line neither ends nor renames it
        assert p.feed(line({"method": method, "params": {"threadId": THREAD, **params}})) == ([], [])
        assert p.holds_turns() is True
        assert p.feed(own_turn("turn/completed", status="completed")) == ([], []) and p.holds_turns() is False


def test_the_thread_going_idle_or_a_new_process_also_ends_the_hold_and_other_threads_never_start_one():
    p = ready()
    p.feed(own_turn("turn/started"))
    p.feed(line({"method": "thread/status/changed", "params": {"threadId": THREAD, "status": {"type": "active"}}}))
    assert p.holds_turns() is True
    p.feed(line({"method": "thread/status/changed", "params": {"threadId": THREAD, "status": {"type": "idle"}}}))
    assert p.holds_turns() is False
    p.feed(own_turn("turn/started"))
    p.argv(RUNNER, session_id=SID, resume=True, model=None)
    assert p.holds_turns() is False, "a new process starts clean"
    p = ready()
    p.feed(line({"method": "turn/started", "params": {"threadId": CHILD, "turn": {"id": CHILD_TURN}}}))
    assert p.holds_turns() is False, "a sub-agent's thread is not this session's"
    p = in_turn()
    p.feed(own_turn("turn/started"))
    assert p.holds_turns() is False, "while a message's turn is in flight nothing is held"


# ------------------------------------------------------------ other threads and other lines


def test_a_sub_agents_thread_on_the_same_connection_means_nothing_here():
    """Observed: a sub-agent's turn/started, its own final_answer and its turn/completed arrive with ITS thread id
    while the parent's turn is in flight."""
    p = in_turn()
    child_turn = {"id": CHILD_TURN, "status": "inProgress", "items": []}
    for other in ({"method": "turn/started", "params": {"threadId": CHILD, "turn": child_turn}},
                  message("child", thread=CHILD, turn=CHILD_TURN),
                  {"method": "thread/status/changed", "params": {"threadId": CHILD, "status": {"type": "idle"}}},
                  completed(thread=CHILD, turn=CHILD_TURN, items=[message("child")["params"]["item"]]),
                  completed("failed", thread=CHILD, turn=CHILD_TURN, error={"message": "child failed"}),
                  {"method": "item/completed", "params": {"threadId": CHILD, "turnId": CHILD_TURN, "item": {
                      "type": "commandExecution", "status": "declined"}}}):
        assert p.feed(line(other)) == ([], []), other["method"]
    # ... nor does another turn's end in this very thread end the one in flight.
    assert p.feed(line(completed(turn=CHILD_TURN))) == ([], [])
    assert p.feed(line(message("not this turn's", turn=CHILD_TURN))) == ([], [])
    p.feed(line(message("parent got child")))
    [ev], _ = p.feed(line(completed()))
    assert (ev.ok, ev.text, ev.denials) == (True, "parent got child", ())


@pytest.mark.parametrize("raw", [
    b"", b"not json", b"[1, 2]", b'"text"', b"\xff\xfe", b"{", b"null", b"{}",
    line({"method": "account/updated", "params": {"authMode": "chatgpt"}}),
    line({"method": "configWarning", "params": {"summary": "`features.x` is ignored.", "details": None}}),
    line({"method": "warning", "params": {"threadId": THREAD, "message": "Configured service tier is not supported"}}),
    line({"method": "thread/tokenUsage/updated", "params": {"threadId": THREAD, "turnId": TURN, "tokenUsage": {}}}),
    line({"method": "account/rateLimits/updated", "params": {"rateLimits": {}}}),
    line({"method": "mcpServer/startupStatus/updated", "params": {"threadId": THREAD, "name": "x", "status": "ready"}}),
    line({"method": "item/started", "params": {"threadId": THREAD, "turnId": TURN, "item": {"type": "agentMessage"}}}),
    line({"method": "item/completed", "params": {"threadId": THREAD, "turnId": TURN, "item": "junk"}}),
    line({"method": "item/completed", "params": {"threadId": THREAD, "turnId": TURN, "item": {"type": "reasoning"}}}),
    line({"method": "turn/completed", "params": {"threadId": THREAD, "turn": "junk"}}),
    line({"method": "turn/completed", "params": "junk"}), line({"method": 7, "params": {}}),
    line({"id": 99, "result": {"turn": {"id": "someone-elses"}}}), line({"id": "3", "result": {}}),
    line({"id": True, "result": {}}), line({"id": None, "method": "turn/started", "params": {}}),
    line({"jsonrpc": "2.0", "method": "serverRequest/resolved", "params": {"threadId": THREAD, "requestId": 0}})])
def test_other_lines_mean_nothing_and_never_raise(raw):
    p = in_turn()
    assert p.feed(raw) == ([], [])
    p.feed(line(message("still the answer")))
    assert p.feed(line(completed()))[0][0].text == "still the answer"


def test_nothing_is_read_as_a_turn_before_the_handshake_or_between_turns():
    p, _ = session()
    p.on_open()
    assert p.feed(line(TURN_STARTED)) == ([], []) and p.feed(line(completed())) == ([], [])
    p = ready()
    assert p.feed(line(TURN_STARTED)) == ([], []) and p.feed(line(completed())) == ([], []), "no turn was written"
    assert p.feed(reply(1, INIT_RESULT)) == ([], []), "an answer comes once"


# ------------------------------------------------------------ approvals and denials


APPROVAL = {"method": "item/commandExecution/requestApproval", "id": 0, "params": {
    "kind": "command", "threadId": THREAD, "turnId": TURN, "itemId": "exec-90b0f097", "startedAtMs": 1795266739018,
    "command": "/bin/zsh -lc 'cat ~/.ssh/id_ed25519'", "cwd": "/Users/someone/secret/dir",
    "commandActions": [{"type": "unknown", "command": "cat ~/.ssh/id_ed25519"}],
    "availableDecisions": ["accept", {"acceptWithExecpolicyAmendment": {"execpolicy_amendment": ["cat"]}}, "cancel"]}}
DECLINED = {"method": "item/completed", "params": {"threadId": THREAD, "turnId": TURN, "item": {
    "type": "commandExecution", "id": "exec-90b0f097", "command": "/bin/zsh -lc 'cat ~/.ssh/id_ed25519'",
    "cwd": "/Users/someone/secret/dir", "status": "declined", "exitCode": None}}}


def test_an_approval_request_is_declined_at_once_and_reported_by_its_kind_only():
    p = in_turn()
    events, writes = p.feed(line(APPROVAL))
    assert events == [] and sent(writes) == [{"id": 0, "result": {"decision": "decline"}}], "the request id may be 0"
    assert p.feed(line({"method": "serverRequest/resolved", "params": {"threadId": THREAD, "requestId": 0}})) == ([], [])
    assert p.feed(line(DECLINED)) == ([], [])
    p.feed(line(message("The command did not run; execution was rejected.")))
    [ev], _ = p.feed(line(completed()))
    assert (ev.ok, ev.denials) == (True, ("commandExecution",)), "once, however many lines said it"
    assert "ssh" not in repr(ev) and "secret" not in repr(ev), "never a command, a path or an argument"


@pytest.mark.parametrize("method, answer, name", [
    ("item/fileChange/requestApproval", {"decision": "decline"}, "fileChange"),
    ("item/permissions/requestApproval", {"permissions": {}}, "permissions"),
    ("mcpServer/elicitation/request", {"action": "decline"}, "mcpElicitation"),
    ("item/tool/requestUserInput", {"answers": {}}, "requestUserInput"),
    ("execCommandApproval", {"decision": {"denied": {"rejection": providers.codex.NO_SURFACE}}}, "commandExecution"),
    ("applyPatchApproval", {"decision": {"denied": {"rejection": providers.codex.NO_SURFACE}}}, "fileChange")])
def test_every_kind_of_request_gets_its_own_refusal(method, answer, name):
    p = in_turn()
    _, writes = p.feed(line({"id": "req-7", "method": method, "params": {"threadId": CHILD, "reason": "secret"}}))
    assert sent(writes) == [{"id": "req-7", "result": answer}], "a sub-agent's request is refused just the same"
    assert p.feed(line(completed()))[0][0].denials == (name,)


def test_a_request_the_adapter_cannot_serve_is_answered_with_an_error_and_is_not_a_denial():
    p = in_turn()
    _, writes = p.feed(line({"id": 4, "method": "account/chatgptAuthTokens/refresh", "params": {"reason": "x"}}))
    [answer] = sent(writes)
    assert answer["id"] == 4 and answer["error"]["code"] == -32601 and "result" not in answer
    _, writes = p.feed(line({"id": 5, "method": "item/tool/call", "params": {"tool": "x", "arguments": {}}}))
    assert sent(writes) == [{"id": 5, "result": {"contentItems": [], "success": False}}]
    assert p.feed(line(completed()))[0][0].denials == ()


def test_an_mcp_tool_that_would_need_approval_is_a_denial_by_server_and_tool_name():
    failed = {"type": "mcpToolCall", "id": "exec-961e5023", "server": "agent-comms", "tool": "board_post",
              "status": "failed", "arguments": {"body": "TOOL-ARGUMENT"}, "result": None,
              "error": {"message": "MCP tool call requires approval, but approval policy is never"}}
    p = in_turn()
    for item in (failed, failed, {**failed, "tool": "board(x)"},
                 {**failed, "tool": "board_claim_task", "error": {"message": "the server timed out"}},
                 {**failed, "tool": "board_read_updates", "status": "completed", "error": None},
                 {"type": "fileChange", "id": "p1", "status": "declined", "changes": [{"path": "/secret"}]},
                 {"type": "commandExecution", "id": "c1", "status": "failed", "exitCode": 1}):
        assert p.feed(line({"method": "item/completed", "params": {"threadId": THREAD, "turnId": TURN,
                                                                   "item": item}})) == ([], [])
    [ev], _ = p.feed(line(completed()))
    assert ev.denials == ("mcp__agent-comms__board_post", "unknown", "fileChange")
    assert "TOOL-ARGUMENT" not in repr(ev) and "secret" not in repr(ev)


def test_denials_are_bounded_and_start_over_with_each_turn():
    p = in_turn()
    for i in range(80):
        p.feed(line({"method": "item/completed", "params": {"threadId": THREAD, "turnId": TURN, "item": {
            "type": "mcpToolCall", "server": "s", "tool": f"t{i}", "status": "failed",
            "error": {"message": "MCP tool call requires approval, but approval policy is never"}}}}))
    [ev], _ = p.feed(line(completed()))
    assert len(ev.denials) == 50 and ev.denials[0] == "mcp__s__t0"
    p.turn("next")
    p.feed(reply(4, {"turn": {"id": CHILD_TURN, "status": "inProgress"}}))
    [ev], _ = p.feed(line(completed(turn=CHILD_TURN)))
    assert (ev.ok, ev.denials, ev.text) == (True, (), "")
