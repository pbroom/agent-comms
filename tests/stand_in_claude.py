"""A stand-in for `claude -p --input-format stream-json --output-format stream-json`: no model, no network.

It speaks the part of Claude Code's stream the session host reads (DESIGN_NOTES "Session host (PR 1)"): nothing until
the first stdin line, then per turn one `system`/`init` and one `result`, both with the session id it was given. The
tests and scripts/session-host-e2e.py install it as an executable named `claude` (the adapter goes by that name).

What a turn does is in its text:
- `sleep N ...`: the turn takes N seconds.
- `fail ...`: the result is an error.
- `crash ...`: the process exits (code 3) at once, before anything of the turn is written.
- `deny ...`: the result lists a refused Write (the turn itself succeeds, as Claude's does).
- `big ...`: the reply is 40 KB.
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
"""
import json
import os
import re
import subprocess
import sys
import time


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
    for line in sys.stdin:
        message = json.loads(line)
        if message.get("type") == "control_request":
            print(json.dumps({"type": "control_response",
                              "response": control(message.get("request_id"), message.get("request") or {}, args)}),
                  flush=True)
            continue
        text = message["message"]["content"]
        if text.startswith("crash"):
            sys.exit(3)
        turn += 1
        if turn == 1:
            run_hooks(args, "SessionStart", sid)
        mode, tools, _ = grants(args)
        print(json.dumps({"type": "system", "subtype": "init", "session_id": sid, "permissionMode": mode,
                          "tools": tools}), flush=True)
        pause = re.match(r"sleep (\d+(?:\.\d+)?)\b", text)
        if pause:
            time.sleep(float(pause.group(1)))
        failed = text.startswith("fail")
        reply = "x" * 40_000 if text.startswith("big") else f"turn {turn}: {text.upper()}"
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


if __name__ == "__main__":
    main()
