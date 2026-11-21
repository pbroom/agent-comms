"""A stand-in for `claude -p --input-format stream-json --output-format stream-json`: no model, no network.

It speaks the part of Claude Code's stream the session host reads (DESIGN_NOTES "Session host (PR 1)"): nothing until
the first stdin line, then per turn one `system`/`init` and one `result`, both with the session id it was given. The
tests and scripts/session-host-e2e.py install it as an executable named `claude` (the adapter goes by that name).

What a turn does is in its text:
- `sleep N ...`: the turn takes N seconds.
- `fail ...`: the result is an error.
- `deny ...`: the result lists a refused Write (the turn itself succeeds, as Claude's does).
- `big ...`: the reply is 40 KB.
- `write NAME`: the turn tries to create the file NAME in its working directory with the Write tool.
- `touch NAME`: the turn tries to run `touch NAME` with the Bash tool.
- `try A B`: both, one after the other: Write A, then `touch B`.
  What comes of a tool call follows from what the process was started with, as it does for Claude Code (`grants`):
  the tool does not exist when `--tools` leaves it out or `--disallowedTools` names it (`NO SUCH TOOL: WRITE`, no
  denial listed); it runs in `bypassPermissions`, for Write also in `acceptEdits`, and when an allow rule names it,
  on the command line OR in a settings file (`WROTE NAME`, `TOUCHED NAME`); otherwise it is refused and the result
  lists the denial (`WRITE DENIED`, `BASH DENIED`). A second passes between the turn's start and its first tool
  call, as a model takes.
Settings: the file (or JSON) given with `--settings` is loaded: its `permissions.allow` rules count like the command
line's, and its `permissions.defaultMode` is the mode when no `--permission-mode` is given. A project's own
`.claude/settings.json` is NOT loaded: Claude Code ignores an untrusted directory's in print mode, and nothing these
tests run in is trusted. Every turn's `init` says the `permissionMode` in force and the `tools` the session has.
The reply is otherwise `turn <k of this process>: <TEXT IN CAPITALS>`.
With STAND_IN_ARGV set (an env var name the runner passes on), each start appends its argv to that file.
"""
import json
import os
import re
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


def grants(args) -> tuple[str, list[str], list[str]]:
    """(the permission mode in force, the built-in tools the session has, the allow rules in force), from the
    command line and the `--settings` file as Claude Code combines them."""
    permissions = {}
    for value in flag_values(args, "--settings")[:1]:
        try:
            loaded = json.loads(value if value.lstrip().startswith("{") else open(value).read())
            permissions = loaded.get("permissions") if isinstance(loaded.get("permissions"), dict) else {}
        except (OSError, ValueError, AttributeError):
            pass
    modes = flag_values(args, "--permission-mode")
    mode = (modes[-1] if modes else "bypassPermissions" if "--dangerously-skip-permissions" in args
            else permissions.get("defaultMode") or "default")
    listed = flag_values(args, "--tools")
    gone = set(flag_values(args, "--disallowedTools", "--disallowed-tools")) | set(permissions.get("deny") or [])
    tools = [t for t in (listed or BUILTINS) if t in BUILTINS and t not in gone]
    allow = flag_values(args, "--allowedTools", "--allowed-tools") + [a for a in permissions.get("allow") or []
                                                                    if isinstance(a, str)]
    return mode, tools, allow


def outcome(tool: str, args) -> str:
    """What a call of this tool comes to: "gone" (the session has no such tool), "yes" or "denied"."""
    mode, tools, allow = grants(args)
    if tool not in tools:
        return "gone"
    if mode == "bypassPermissions" or (mode == "acceptEdits" and tool in ("Write", "Edit")):
        return "yes"
    return "yes" if any(a in (tool, tool + "(*)") or a.startswith(tool + "(./") for a in allow) else "denied"


def main() -> None:
    args = sys.argv[1:]
    sid = args[args.index("--resume" if "--resume" in args else "--session-id") + 1]
    record = os.environ.get("STAND_IN_ARGV")
    if record:
        with open(record, "a") as f:
            f.write(json.dumps(args) + "\n")
    sys.stderr.write("stand-in started\n")
    turn = 0
    for line in sys.stdin:
        text = json.loads(line)["message"]["content"]
        turn += 1
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
        acts = re.match(r"(write|touch|try) ([A-Za-z0-9._-]+)(?: ([A-Za-z0-9._-]+))?$", text)
        if acts:
            time.sleep(MODEL_SECONDS)
            kind, first, second = acts.groups()
            calls = [("Write", first), ("Bash", second)] if kind == "try" else [("Write" if kind == "write" else "Bash",
                                                                                 first)]
            said, denials = [], []
            for tool, name in calls:
                if name is None:
                    continue
                got = outcome(tool, args)
                if got == "yes":
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
