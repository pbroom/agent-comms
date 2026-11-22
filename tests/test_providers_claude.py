"""The Claude adapter (providers/claude.py): the session argv from a runner template, and what each stream-json line
means. The event shapes are the ones Claude Code 2.1.322 wrote in the live checks (DESIGN_NOTES "Session host (PR 1)"),
cut down to the fields that matter."""

import json

import pytest

from agent_comms import providers
from agent_comms.providers import Event
from agent_comms.providers.claude import ClaudeSession

SID = "0a00c308-20b7-40fc-9a1c-19b8bc832d6d"
OTHER = "9e8d7c6b-5a49-4382-a1b0-c9d8e7f6a5b4"
SHIPPED = ["claude", "-p", "{prompt}", "--permission-mode", "dontAsk", "--allowedTools=mcp__agent-comms"]
STREAM = ["--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
          "--permission-prompts", "none"]

INIT = {"type": "system", "subtype": "init", "session_id": SID, "permissionMode": "default",
        "model": "claude-haiku-5-5", "tools": ["Bash", "Read", "Write"], "mcp_servers": [],
        "claude_code_version": "2.1.322"}
# The `result` of a turn whose Write call was refused under --permission-prompts none.
DENIED_RESULT = {
    "type": "result", "subtype": "success", "is_error": False, "num_turns": 2, "result": "DENIED",
    "session_id": SID, "stop_reason": "end_turn", "terminal_reason": "completed", "duration_ms": 5600,
    "total_cost_usd": 0.0036, "api_error_status": None, "queued_turn_count": 0,
    "permission_denials": [{"tool_name": "Write", "tool_use_id": "toolu_011eqdFuiXfm6zt599qhUdGP",
                            "tool_input": {"file_path": "/private/tmp/work/note.txt", "content": "hi"}}],
    "usage": {"input_tokens": 10}, "modelUsage": {}}
# Written mid-turn when the call is refused; the result above repeats it, so the adapter reads only the result.
PERMISSION_DENIED = {"type": "system", "subtype": "permission_denied", "tool_name": "Write", "session_id": SID,
                     "tool_use_id": "toolu_011eqdFuiXfm6zt599qhUdGP", "decision_reason_type": "asyncAgent",
                     "decision_reason": "no approval surface in this session; permission request denied automatically"}


def line(event) -> bytes:
    return json.dumps(event).encode()


def session(resume=False, model=None, template=SHIPPED):
    p = ClaudeSession()
    return p, p.argv(template, session_id=SID, resume=resume, model=model)


def test_the_session_argv_drops_the_prompt_and_adds_the_stream_flags():
    _, argv = session()
    assert argv == ["claude", "-p", "--permission-mode", "dontAsk", "--allowedTools=mcp__agent-comms"] + STREAM + [
        "--session-id", SID]
    _, argv = session(resume=True, model="haiku")
    assert argv[-4:] == ["--model", "haiku", "--resume", SID] and "--session-id" not in argv


def test_print_mode_is_added_when_the_runner_lacks_it_and_verbose_is_not_repeated():
    _, argv = session(template=["/opt/bin/claude", "{prompt}", "--verbose"])
    assert argv[:2] == ["/opt/bin/claude", "-p"] and argv.count("--verbose") == 1
    _, argv = session(template=["claude", "--print", "{prompt}"])
    assert argv[:2] == ["claude", "--print"] and "-p" not in argv


def test_only_a_claude_runner_and_a_real_session_id_make_an_argv():
    with pytest.raises(ValueError):
        ClaudeSession().argv(["codex", "exec", "{prompt}"], session_id=SID, resume=False, model=None)
    with pytest.raises(ValueError):
        ClaudeSession().argv(SHIPPED, session_id="--resume", resume=False, model=None)
    with pytest.raises(ValueError):
        ClaudeSession().argv(SHIPPED, session_id=SID, resume=False, model="--dangerously-skip-permissions")


def test_a_turn_is_one_user_line_and_nothing_is_written_on_open():
    p, _ = session()
    assert p.on_open() == [] and p.interrupt() is None and p.waits_for_ready is False
    [data] = p.turn("two\nlines")
    assert data.endswith(b"\n") and data.count(b"\n") == 1
    assert json.loads(data) == {"type": "user", "message": {"role": "user", "content": "two\nlines"}}


def test_init_makes_the_session_ready_once_and_starts_every_turn():
    p, _ = session()
    assert p.feed(line(INIT)) == ([Event("ready", provider_id=SID), Event("turn_started")], [])
    assert p.feed(line(INIT)) == ([Event("turn_started")], [])
    p.argv(SHIPPED, session_id=SID, resume=True, model=None)     # a restarted process confirms again
    assert p.feed(line(INIT))[0][0] == Event("ready", provider_id=SID)


def test_a_result_ends_the_turn_with_tool_names_only():
    p, _ = session()
    p.feed(line(INIT))
    assert p.feed(line(PERMISSION_DENIED)) == ([], [])
    [ev], writes = p.feed(line(DENIED_RESULT))
    assert writes == []
    assert (ev.kind, ev.ok, ev.denials) == ("turn_ended", True, ("Write",))
    assert ev.meta == {"subtype": "success", "is_error": False, "num_turns": 2, "duration_ms": 5600,
                       "total_cost_usd": 0.0036, "stop_reason": "end_turn", "terminal_reason": "completed"}
    assert "note.txt" not in repr(ev), "no tool input"
    # PR 2: the reply travels in its own field (capped, for the human only); `meta` and `denials` never hold it.
    assert ev.text == "DENIED" and "DENIED" not in repr(ev.meta) + repr(ev.denials)


@pytest.mark.parametrize("change", [{"subtype": "error_during_execution", "is_error": True},
                                    {"subtype": "error_max_turns"}, {"is_error": True}, {"is_error": None}])
def test_a_result_that_is_not_a_plain_success_is_a_failed_turn(change):
    p, _ = session()
    [ev], _ = p.feed(line({**DENIED_RESULT, "permission_denials": [], **change}))
    assert (ev.kind, ev.ok, ev.denials) == ("turn_ended", False, ())
    assert ev.answered is True, "Claude's own result, an error too: it took the turn (result delivery, round 3)"


def test_a_result_is_the_one_end_of_a_turn_that_says_claude_took_it():
    p, _ = session()
    [ev], _ = p.feed(line(DENIED_RESULT))
    assert (ev.ok, ev.answered) == (True, True)
    for raw in (line(INIT), line(PERMISSION_DENIED), b"not json", line({"type": "assistant"})):
        assert all(e.kind != "turn_ended" for e in p.feed(raw)[0]), "nothing else ends a turn"


@pytest.mark.parametrize("denials, names", [
    (None, ()), ("Write", ()), ([], ()), ([{"tool_name": "mcp__agent-comms__board_post"}],
                                          ("mcp__agent-comms__board_post",)),
    ([{"tool_name": "Bash"}, {"tool_name": "Bash"}, "junk", {"tool_name": 7}, {"tool_name": "Bash(rm -rf ~)"}],
     ("Bash", "unknown")),
    ([{"tool_name": f"T{i}"} for i in range(80)], tuple(f"T{i}" for i in range(50))),
])
def test_denials_are_plain_distinct_names_and_bounded(denials, names):
    p, _ = session()
    [ev], _ = p.feed(line({**DENIED_RESULT, "permission_denials": denials}))
    assert ev.denials == names


@pytest.mark.parametrize("raw", [b"", b"not json", b"[1, 2]", b'"text"', b"\xff\xfe", b"{",
                                 line({"type": "assistant", "session_id": SID, "message": {"content": []}}),
                                 line({"type": "rate_limit_event", "session_id": SID}),
                                 line({"type": "system", "subtype": "post_turn_summary", "session_id": SID}),
                                 line({"type": "system", "subtype": "task_summary", "detail": None}),
                                 line({"type": "control_request", "request": {"subtype": "initialize"}})])
def test_other_lines_mean_nothing_and_never_raise(raw):
    p, _ = session()
    assert p.feed(raw) == ([], [])


@pytest.mark.parametrize("event", [{**INIT, "session_id": OTHER}, {**DENIED_RESULT, "session_id": OTHER},
                                   {"type": "assistant", "session_id": OTHER},
                                   {k: v for k, v in INIT.items() if k != "session_id"},
                                   {**INIT, "session_id": None}])
def test_another_session_id_is_fatal_and_ends_the_adapter(event):
    p, _ = session()
    [ev], writes = p.feed(line(event))
    assert ev.kind == "fatal" and writes == [] and OTHER not in ev.detail
    assert p.feed(line(INIT)) == ([], []) and p.feed(line(DENIED_RESULT)) == ([], [])


def test_a_request_to_use_a_tool_is_reported_and_left_unanswered():
    p, _ = session()
    p.feed(line(INIT))
    events, writes = p.feed(line({"type": "control_request", "request_id": "r1", "request": {
        "subtype": "can_use_tool", "tool_name": "Bash", "input": {"command": "cat ~/.ssh/id_ed25519"}}}))
    assert events == [Event("approval_requested", denials=("Bash",))] and writes == []


def test_the_seam_picks_the_adapter_by_the_runners_cli():
    assert providers.provider_for(SHIPPED) is ClaudeSession
    assert providers.provider_for(["/usr/local/bin/claude", "-p", "{prompt}"]) is ClaudeSession
    assert providers.provider_for(["codex", "exec", "{prompt}"]) is None
    assert providers.provider_for(["claude-wrapper", "{prompt}"]) is None and providers.provider_for([]) is None
    with pytest.raises(ValueError):
        Event("made_up")
    assert providers.validate_model(None) is None and providers.validate_model("opus[1m]") == "opus[1m]"


def test_a_refused_start_says_whether_the_session_exists():
    """The two refusals as Claude Code 2.1.322 writes them to stderr before any turn (exit 1; DESIGN_NOTES "Session
    host (PR 2)", checked live). The host flips its start mode on them, so the wording is pinned here."""
    p, _ = session()
    assert p.session_exists(f"Error: Session ID {SID} is already in use.\n") is True
    assert p.session_exists(f"No conversation found with session ID: {SID}\n") is False
    assert p.session_exists("") is None and p.session_exists("Error: something else went wrong\n") is None
    assert p.session_exists(f"Error: Session ID {OTHER} is already in use.\n") is None, "another session's is not ours"
    # The `result` Claude also writes for a refused --resume carries no init: it ends a turn, it confirms nothing.
    refused = {"type": "result", "subtype": "error_during_execution", "is_error": True, "num_turns": 0,
               "session_id": SID, "permission_denials": [],
               "errors": [f"No conversation found with session ID: {SID}"]}
    [ev], _ = p.feed(line(refused))
    assert (ev.kind, ev.ok) == ("turn_ended", False)
