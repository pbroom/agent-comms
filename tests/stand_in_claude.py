"""A stand-in for `claude -p --input-format stream-json --output-format stream-json`: no model, no network.

It speaks the part of Claude Code's stream the session host reads (DESIGN_NOTES "Session host (PR 1)"): nothing until
the first stdin line, then per turn one `system`/`init` and one `result`, both with the session id it was given. The
tests and scripts/session-host-e2e.py install it as an executable named `claude` (the adapter goes by that name).

What a turn does is in its text:
- `sleep N ...`: the turn takes N seconds.
- `hold PATH ...`: the turn lasts until the file PATH exists (at most 120 seconds): a turn held open for as long as
  a test needs, released by creating the file.
- `wait-init PATH REST`: the turn's `init` waits until the file PATH exists (stdin is read meanwhile: a control
  request is answered, before the `init`); then the turn is REST.
- `fail ...`: the result is an error.
- `crash ...`: the process exits (code 3) at once, before anything of the turn is written.
- `deny ...`: the result lists a refused Write (the turn itself succeeds, as Claude's does).
- `big ...`: the reply is 40 KB.
- a turn that asks for a result as data (its text has the frame's `<<<output-schema T>>>` line) and says `data-valid`,
  `data-invalid`, `data-two` or `data-none`: the reply ends with the result-data block of review data (`data_reply`).
- `usage N ...`: before its result the turn writes a `rate_limit_event` saying the account's five-hour window is at
  N percent (`rejected` at 100 or more), as Claude Code's SDK schema describes it.
- `write NAME`: the turn tries to create the file NAME in its working directory with the Write tool.
- `touch NAME`: the turn tries to run `touch NAME` with the Bash tool.
- `try A B`: both, one after the other: Write A, then `touch B`.
- `probe R A B`: the Read tool on the file R first, then as `try A B`.
  What comes of a tool call follows from what the process was started with, as it does for Claude Code (`grants`):
  the tool does not exist when `--tools` leaves it out or `--disallowedTools` names it (`NO SUCH TOOL: WRITE`, no
  denial listed); it runs in `bypassPermissions`, for Write also in `acceptEdits`, and when an allow rule names it,
  on the command line OR in a settings file (`WROTE NAME`, `TOUCHED NAME`, `READ NAME`); otherwise it is refused and
  the result lists the denial (`WRITE DENIED`, `BASH DENIED`, `READ DENIED`). A second passes between the turn's
  start and its first tool call, as a model takes.
Settings: the file (or JSON) given with `--settings` is loaded: its `permissions.allow` rules count like the command
line's, and its `permissions.defaultMode` is the mode when no `--permission-mode` is given (an explicit mode wins;
`manual` is reported as `default`, as Claude Code reports it). Its `hooks` RUN, as shell commands with the hook's
JSON on stdin, whatever tools the session has: `SessionStart` before the first turn, `PreToolUse` with a matcher
that names the tool before each call of it. A process started without `--settings` has no hooks. A project's own
`.claude/settings.json` is NOT loaded: Claude Code ignores an untrusted directory's in print mode, and nothing these
tests run in is trusted. Every turn's `init` says the `permissionMode` in force and the `tools` the session has.
More of what Claude Code 2.1.322 was seen to do (DESIGN_NOTES, tree review round 3):
- Of several `--settings` only the LAST is loaded. A settings file with a value of the wrong type is ignored WHOLE.
- `permissions.disableBypassPermissionsMode: "disable"` takes the bypass mode away: a process started in it runs in
  `default`.
- `--restricted` beside `--dangerously-skip-permissions`, `--allow-dangerously-skip-permissions` or the bypass mode:
  "Error: bypassPermissions not supported in restricted mode" on stderr, exit 1, before anything is read.
- A `control_request` line on stdin is answered at once with a `control_response`, and is no turn. `get_settings`:
  the settings in force (`response.effective`). `set_permission_mode` to `bypassPermissions`: an error,
  `bypass_disabled` with the key above, else `bypass_not_launched` unless the process was started with one of the
  skip flags or in the mode; otherwise the mode changes.
- `--plugin-dir DIR`: the command hooks in `DIR/hooks/hooks.json` run like a settings file's, whatever
  `--setting-sources` says.
The reply is otherwise `turn <k of this process>: <TEXT IN CAPITALS>`.
With STAND_IN_ARGV set (an env var name the runner passes on), each start appends its argv to that file.
Mid-turn input, as Claude Code 2.1.330 was measured to handle it (DESIGN_NOTES "Restarting a running turn"; the
host never steers, R6, but a restart's interrupt comes mid-turn): stdin is read all
the time, also while a turn runs. A user line that comes during a turn is queued; one with a `uuid` gets
`command_lifecycle` lines (queued, started, completed, cancelled), as does a turn's own line when it has one. A
`sleep` or `hold` turn is a TOOL call: at its end (the tool boundary) every queued line is taken into the turn
(`started` before the result) and the reply adds `; STEERED: <TEXT IN CAPITALS>` for each. A `think N ...` turn takes N
seconds with no tool, so a line queued during it runs as its own next turn after the result. A `control_request`
`interrupt` ends the turn at once: a `control_response` with `still_queued` (the queued lines' uuids), the turn's
result `error_during_execution` (terminal_reason `aborted_tools`), and the queued lines then run as their own turns.
An interrupt already behind a turn's line when that line is read (before the turn's `init`) is answered `success`
and interrupts nothing: the turn runs whole (measured on 2.1.330 too).
Every `init` also lists `capabilities` (`msg_lifecycle_v1` and `interrupt_receipt_v1`, as 2.1.330's does; with
STAND_IN_CAPS set, that comma-separated list instead) and a `claude_code_version`.
"""
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time


# A turn that asks for a result as data (DESIGN_NOTES "Structured results": the frame's `<<<output-schema T>>>` line)
# and names `data-valid`, `data-invalid`, `data-two` or `data-none` in its text: the reply ends as the frame asks
# (or deliberately does not), with the review data.
REVIEW_DATA = {"verdict": "changes", "findings": [{"file": "src/pager.py", "line": 3, "note": "off by one"}]}


def data_reply(text: str):
    asked = re.search(r"<<<output-schema ([0-9a-f]{16})>>>", text)
    want = re.search(r"\bdata-(valid|invalid|two|none)\b", text)
    if not asked or not want:
        return None
    t, kind = asked.group(1), want.group(1)
    good = json.dumps(REVIEW_DATA)
    bad = json.dumps({"verdict": "maybe", "findings": []})
    one = f"<<<result-data {t}>>>\n{{body}}\n<<<end result-data {t}>>>"
    return {"valid": "Reviewed: one bug.\n" + one.format(body=good),
            "invalid": "Reviewed.\n" + one.format(body=bad),
            "two": "Draft:\n" + one.format(body=good) + "\nFinal:\n" + one.format(body=good),
            "none": "Reviewed, but no block."}[kind]


def flag_values(args, *names):
    """Every value of these flags: `--flag=a,b` or `--flag a b` up to the next option."""
    out, i = [], 0
    while i < len(args):
        flag, eq, value = args[i].partition("=")
        i += 1
        if flag not in names:
            continue
        if eq:
            out += re.split(r"[, ]+", value)
            continue
        while i < len(args) and not args[i].startswith("-"):
            out += re.split(r"[, ]+", args[i])
            i += 1
    return [v for v in out if v]


BUILTINS = ("Bash", "Edit", "Glob", "Grep", "NotebookEdit", "Read", "Task", "Write")
MODEL_SECONDS = 1.0     # from a turn's start to its first tool call


def single_values(args, name) -> list[str]:
    """Every value of a flag that takes exactly one: `--flag value` or `--flag=value`."""
    out = []
    for i, arg in enumerate(args):
        flag, eq, value = arg.partition("=")
        if flag == name and (eq or i + 1 < len(args)):
            out.append(value if eq else args[i + 1])
    return out


def settings(args) -> dict:
    """The settings the process was given with its LAST `--settings` (a path, or the JSON itself), or nothing. A
    file with a value of the wrong type is ignored whole, as Claude Code ignores one that fails validation."""
    for value in single_values(args, "--settings")[-1:]:
        try:
            loaded = json.loads(value if value.lstrip().startswith("{") else open(value).read())
        except (OSError, ValueError):
            return {}
        if not isinstance(loaded, dict) or not isinstance(loaded.get("permissions", {}), dict) \
                or not isinstance(loaded.get("hooks", {}), dict) \
                or any(not isinstance(loaded["permissions"].get(k, []), list) for k in ("allow", "deny")
                       if "permissions" in loaded):
            return {}
        return loaded
    return {}


def bypass_disabled(args) -> bool:
    permissions = settings(args).get("permissions")
    return isinstance(permissions, dict) and permissions.get("disableBypassPermissionsMode") == "disable"


def launched_with_bypass(args) -> bool:
    """Started with a flag that selects the bypass mode or makes it selectable."""
    return ("--dangerously-skip-permissions" in args or "--allow-dangerously-skip-permissions" in args
            or "bypassPermissions" in single_values(args, "--permission-mode"))


SET_MODE = None     # a mode a `set_permission_mode` request put the session in


def grants(args) -> tuple[str, list[str], list[str]]:
    """(the permission mode in force, the built-in tools the session has, the allow rules in force), from the
    command line and the `--settings` file as Claude Code combines them."""
    permissions = settings(args).get("permissions")
    permissions = permissions if isinstance(permissions, dict) else {}
    modes = flag_values(args, "--permission-mode")
    mode = SET_MODE or (modes[-1] if modes else "bypassPermissions" if "--dangerously-skip-permissions" in args
                        else permissions.get("defaultMode") or "default")
    mode = "default" if mode == "manual" or (mode == "bypassPermissions" and bypass_disabled(args)) else mode
    listed = flag_values(args, "--tools")
    gone = set(flag_values(args, "--disallowedTools", "--disallowed-tools")) | set(permissions.get("deny") or [])
    tools = [t for t in (listed or BUILTINS) if t in BUILTINS and t not in gone]
    allow = flag_values(args, "--allowedTools", "--allowed-tools") + [a for a in permissions.get("allow") or []
                                                                    if isinstance(a, str)]
    return mode, tools, allow


def run_hooks(args, event: str, sid: str, tool: str | None = None) -> None:
    """Run the command hooks the settings file has for this event (and, for a tool event, this tool), each with the
    hook's JSON on its stdin. A hook is a command: it runs whether or not the session has any tool at all."""
    hooks = settings(args).get("hooks")
    groups = list((hooks.get(event) or []) if isinstance(hooks, dict) else [])
    for plugin in single_values(args, "--plugin-dir"):        # a plugin's hooks: no settings source decides them
        try:
            plugged = json.load(open(os.path.join(plugin, "hooks", "hooks.json"))).get("hooks")
        except (OSError, ValueError, AttributeError):
            continue
        groups += list((plugged.get(event) or []) if isinstance(plugged, dict) else [])
    for group in groups:
        if not isinstance(group, dict):
            continue
        matcher = group.get("matcher") or ""
        if tool is not None and matcher not in ("", "*") and tool not in matcher.split("|"):
            continue
        for hook in group.get("hooks") or []:
            if isinstance(hook, dict) and hook.get("type") == "command" and isinstance(hook.get("command"), str):
                payload = {"session_id": sid, "hook_event_name": event, "cwd": os.getcwd()}
                payload |= {"tool_name": tool} if tool is not None else {}
                subprocess.run(hook["command"], shell=True, input=json.dumps(payload), text=True, timeout=30,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def outcome(tool: str, args) -> str:
    """What a call of this tool comes to: "gone" (the session has no such tool), "yes" or "denied"."""
    mode, tools, allow = grants(args)
    if tool not in tools:
        return "gone"
    if mode == "bypassPermissions" or (mode == "acceptEdits" and tool in ("Write", "Edit")):
        return "yes"
    return "yes" if any(a in (tool, tool + "(*)") or a.startswith(tool + "(./") for a in allow) else "denied"


def control(request_id, request: dict, args) -> dict:
    """The answer to one `control_request`."""
    global SET_MODE
    kind = request.get("subtype")

    def error(text, code):
        return {"subtype": "error", "request_id": request_id, "error": text, "error_code": code}

    if kind == "get_settings":
        loaded = settings(args)
        return {"subtype": "success", "request_id": request_id,
                "response": {"effective": loaded, "sources": [{"source": "flagSettings", "settings": loaded}]}}
    if kind == "set_permission_mode" and request.get("mode") == "bypassPermissions":
        if bypass_disabled(args):
            return error("Cannot set permission mode to bypassPermissions because it is disabled by settings or "
                         "configuration", "bypass_disabled")
        if not launched_with_bypass(args):
            return error("Cannot set permission mode to bypassPermissions because the session was not launched with "
                         "--dangerously-skip-permissions", "bypass_not_launched")
    if kind == "set_permission_mode":
        SET_MODE = request.get("mode")
    return {"subtype": "success", "request_id": request_id}


def emit(obj: dict) -> None:
    print(json.dumps(obj), flush=True)


class Stdin:
    """Every stdin line, read on a thread of its own (so a running turn still hears an interrupt or a steer).
    `queued`: user lines that came while a turn ran, in order, until a turn takes them."""

    def __init__(self, sid: str, args):
        self.sid, self.args = sid, args
        self.lines: queue.Queue = queue.Queue()
        self.queued: list[dict] = []
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        for line in sys.stdin:
            self.lines.put(line)
        self.lines.put(None)

    def lifecycle(self, message: dict, state: str) -> None:
        if isinstance(message.get("uuid"), str):
            emit({"type": "command_lifecycle", "command_uuid": message["uuid"], "state": state,
                  "session_id": self.sid})

    def take(self, timeout: float | None = None, in_turn: bool = False):
        """The next line as a message, or None when the time ran out. Stdin closed: exit, except in a turn, which
        runs to its end first (as when stdin was read only between turns)."""
        try:
            line = self.lines.get(timeout=timeout)
        except queue.Empty:
            return None
        if line is None:
            if not in_turn:
                sys.exit(0)
            self.lines.put(None)
            time.sleep(timeout or 0.05)
            return None
        return json.loads(line)

    def answer(self, message: dict) -> bool:
        """A control request: answered at once. True for `interrupt` (the caller ends its turn)."""
        request = message.get("request") or {}
        if request.get("subtype") == "interrupt":
            emit({"type": "control_response", "response": {
                "subtype": "success", "request_id": message.get("request_id"),
                "response": {"still_queued": [m["uuid"] for m in self.queued if isinstance(m.get("uuid"), str)]}}})
            return True
        emit({"type": "control_response", "response": control(message.get("request_id"), request, self.args)})
        return False

    def wait(self, seconds: float, until=lambda: False) -> bool:
        """A turn's wait: lines go on being read. False once an interrupt came."""
        deadline = time.monotonic() + seconds
        while True:
            over = until() or time.monotonic() >= deadline
            # Once it is over, what has arrived already is still read (the process reads its stdin at once).
            message = self.take(timeout=0 if over else max(0.001, min(0.05, deadline - time.monotonic())),
                                in_turn=True)
            if message is None:
                if over:
                    break
                continue
            if message.get("type") == "control_request":
                if self.answer(message):
                    return False
                continue
            self.queued.append(message)
            self.lifecycle(message, "queued")
        return True


def main() -> None:
    args = sys.argv[1:]
    sid = args[args.index("--resume" if "--resume" in args else "--session-id") + 1]
    if "--restricted" in args and launched_with_bypass(args):
        sys.stderr.write("Error: bypassPermissions not supported in restricted mode\n")
        sys.exit(1)
    record = os.environ.get("STAND_IN_ARGV")
    if record:
        with open(record, "a") as f:
            f.write(json.dumps(args) + "\n")
    sys.stderr.write("stand-in started\n")
    turn = 0
    stdin = Stdin(sid, args)
    while True:
        if stdin.queued:
            message = stdin.queued.pop(0)       # a line that waited behind the last turn: its own turn now
        else:
            message = stdin.take()
            if message.get("type") == "control_request":
                stdin.answer(message)
                continue
            stdin.lifecycle(message, "queued")
            # What already came behind the turn's line is read before its `init` (measured on 2.1.330, DESIGN_NOTES
            # "Review follow-ups (2026-11-23)"): an interrupt there is answered `success` and interrupts nothing.
            while (early := stdin.take(timeout=0, in_turn=True)) is not None:
                if early.get("type") == "control_request":
                    stdin.answer(early)
                else:
                    stdin.queued.append(early)
                    stdin.lifecycle(early, "queued")
        text = message["message"]["content"]
        if text.startswith("crash"):
            sys.exit(3)
        gate = re.match(r"wait-init (/\S+) (.*)", text, re.S)
        if gate:
            # The turn's `init` waits for the file; what comes meanwhile is read before it (as measured: a control
            # request is answered, an interrupt applies to nothing). The rest of the text is the turn.
            while not os.path.exists(gate.group(1)):
                early = stdin.take(timeout=0.02, in_turn=True)
                if early is None:
                    continue
                if early.get("type") == "control_request":
                    stdin.answer(early)
                else:
                    stdin.queued.append(early)
                    stdin.lifecycle(early, "queued")
            text = gate.group(2)
        stdin.lifecycle(message, "started")
        turn += 1
        if turn == 1:
            run_hooks(args, "SessionStart", sid)
        mode, tools, _ = grants(args)
        print(json.dumps({"type": "system", "subtype": "init", "session_id": sid, "permissionMode": mode,
                          "tools": tools, "claude_code_version": "2.1.330-stand-in",
                          "capabilities": os.environ.get("STAND_IN_CAPS", "msg_lifecycle_v1,interrupt_receipt_v1")
                          .split(",")}), flush=True)
        going, tool = True, False
        pause = re.match(r"(sleep|think) (\d+(?:\.\d+)?)\b", text)
        if pause:
            tool = pause.group(1) == "sleep"
            going = stdin.wait(float(pause.group(2)))
        held = re.match(r"hold (/\S+)", text)
        if held:
            tool = True
            going = stdin.wait(120, until=lambda: os.path.exists(held.group(1)))
        if not going:
            stdin.lifecycle(message, "cancelled")
            emit({"type": "result", "subtype": "error_during_execution", "is_error": True, "num_turns": 1,
                  "session_id": sid, "result": None, "stop_reason": "tool_use", "terminal_reason": "aborted_tools",
                  "permission_denials": []})
            continue
        steered = []
        if tool:
            # The tool boundary: every line queued meanwhile is read into this turn.
            steered, stdin.queued = stdin.queued, []
            for m in steered:
                stdin.lifecycle(m, "started")
        usage = re.match(r"usage (\d+(?:\.\d+)?)\b", text)
        if usage:
            # The account's plan usage, as Claude Code 2.1.330's `rate_limit_event` carries it (DESIGN_NOTES "Account
            # pools"): the five-hour window at N percent, resetting in an hour; `rejected` at 100 or more.
            pct = float(usage.group(1))
            emit({"type": "rate_limit_event", "session_id": sid, "uuid": f"rl-{turn}", "rate_limit_info": {
                "status": "rejected" if pct >= 100 else "allowed_warning" if pct >= 75 else "allowed",
                "resetsAt": int(time.time()) + 3600, "rateLimitType": "five_hour", "utilization": pct / 100,
                "unifiedWindows": {"five_hour": {"utilization": pct / 100, "resetsAt": int(time.time()) + 3600}}}})
        failed = text.startswith("fail")
        reply = "x" * 40_000 if text.startswith("big") else data_reply(text) or f"turn {turn}: {text.upper()}"
        reply += "".join("; STEERED: " + m["message"]["content"].upper() for m in steered)
        denials = ([{"tool_name": "Write", "tool_use_id": "toolu_01", "tool_input": {"file_path": "/tmp/x"}}]
                   if text.startswith("deny") else [])
        acts = re.match(r"(write|touch|try|probe)((?: [A-Za-z0-9._-]+){1,3})$", text)
        if acts:
            time.sleep(MODEL_SECONDS)
            kind, names = acts.group(1), acts.group(2).split()
            order = {"write": ["Write"], "touch": ["Bash"], "try": ["Write", "Bash"],
                     "probe": ["Read", "Write", "Bash"]}[kind]
            said, denials = [], []
            for tool, name in zip(order, names):
                got = outcome(tool, args)
                if got != "gone":
                    run_hooks(args, "PreToolUse", sid, tool)
                if got == "yes" and tool == "Read":
                    said.append("READ " + name if os.path.isfile(os.path.join(os.getcwd(), name)) else "NO FILE " + name)
                elif got == "yes":
                    with open(os.path.join(os.getcwd(), name), "a") as f:
                        f.write("written by the stand-in\n" if tool == "Write" else "")
                    said.append(("WROTE " if tool == "Write" else "TOUCHED ") + name)
                elif got == "gone":
                    said.append("NO SUCH TOOL: " + tool.upper())
                else:
                    said.append(tool.upper() + " DENIED")
                    denials.append({"tool_name": tool, "tool_use_id": "toolu_02", "tool_input": {"name": name}})
            reply = f"turn {turn}: " + "; ".join(said)
        print(json.dumps({"type": "result", "subtype": "error_during_execution" if failed else "success",
                          "is_error": failed, "num_turns": 1, "session_id": sid, "result": reply,
                          "stop_reason": "end_turn", "permission_denials": denials}), flush=True)
        for m in steered + [message]:
            stdin.lifecycle(m, "completed")


if __name__ == "__main__":
    main()
