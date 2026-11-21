"""A stand-in for `claude -p --input-format stream-json --output-format stream-json`: no model, no network.

It speaks the part of Claude Code's stream the session host reads (DESIGN_NOTES "Session host (PR 1)"): nothing until
the first stdin line, then per turn one `system`/`init` and one `result`, both with the session id it was given. The
tests and scripts/session-host-e2e.py install it as an executable named `claude` (the adapter goes by that name).

What a turn does is in its text:
- `sleep N ...`: the turn takes N seconds.
- `fail ...`: the result is an error.
- `deny ...`: the result lists a refused Write (the turn itself succeeds, as Claude's does).
- `big ...`: the reply is 40 KB.
- `write NAME`: the turn tries to create the file NAME in its working directory with the Write tool, and what
  happens follows from the argv as it does for Claude Code: with Write among the `--disallowedTools` there is no such
  tool (no file, no denial listed); with `--permission-mode bypassPermissions` (or `--dangerously-skip-permissions`),
  `acceptEdits`, or Write in `--allowedTools` the file is written; otherwise the call is refused and the result
  lists the denial. The reply says which: `WROTE NAME`, `NO SUCH TOOL: WRITE` or `WRITE DENIED`.
- `touch NAME`: the turn tries to run `touch NAME` with the Bash tool. With `--permission-mode bypassPermissions`
  (or `--dangerously-skip-permissions`), or Bash in `--allowedTools`, the file is made (`TOUCHED NAME`); otherwise
  the call is refused and the result lists the denial (`BASH DENIED`), as `dontAsk` refuses what is not allowed.
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


def write_outcome(args) -> str:
    """What a Write call comes to under this argv: "gone" (the tool is disallowed), "yes" or "denied"."""
    if "Write" in flag_values(args, "--disallowedTools", "--disallowed-tools"):
        return "gone"
    modes = flag_values(args, "--permission-mode")
    if "--dangerously-skip-permissions" in args or modes[-1:] in (["bypassPermissions"], ["acceptEdits"]):
        return "yes"
    allowed = flag_values(args, "--allowedTools", "--allowed-tools")
    return "yes" if any(t == "Write" or t.startswith("Write(") for t in allowed) else "denied"


def bash_outcome(args) -> str:
    """What a Bash call that changes something comes to under this argv: "yes" or "denied"."""
    if "Bash" in flag_values(args, "--disallowedTools", "--disallowed-tools"):
        return "denied"
    modes = flag_values(args, "--permission-mode")
    if "--dangerously-skip-permissions" in args or modes[-1:] == ["bypassPermissions"]:
        return "yes"
    return "yes" if "Bash" in flag_values(args, "--allowedTools", "--allowed-tools") else "denied"


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
        print(json.dumps({"type": "system", "subtype": "init", "session_id": sid}), flush=True)
        pause = re.match(r"sleep (\d+(?:\.\d+)?)\b", text)
        if pause:
            time.sleep(float(pause.group(1)))
        failed = text.startswith("fail")
        reply = "x" * 40_000 if text.startswith("big") else f"turn {turn}: {text.upper()}"
        denials = ([{"tool_name": "Write", "tool_use_id": "toolu_01", "tool_input": {"file_path": "/tmp/x"}}]
                   if text.startswith("deny") else [])
        wrote = re.match(r"write ([A-Za-z0-9._-]+)$", text)
        if wrote:
            outcome = write_outcome(args)
            if outcome == "yes":
                with open(os.path.join(os.getcwd(), wrote.group(1)), "w") as f:
                    f.write("written by the stand-in\n")
                reply = f"turn {turn}: WROTE {wrote.group(1)}"
            elif outcome == "gone":
                reply = f"turn {turn}: NO SUCH TOOL: WRITE"
            else:
                reply = f"turn {turn}: WRITE DENIED"
                denials = [{"tool_name": "Write", "tool_use_id": "toolu_02", "tool_input": {"file_path": wrote.group(1)}}]
        touched = re.match(r"touch ([A-Za-z0-9._-]+)$", text)
        if touched and bash_outcome(args) == "yes":
            open(os.path.join(os.getcwd(), touched.group(1)), "a").close()
            reply = f"turn {turn}: TOUCHED {touched.group(1)}"
        elif touched:
            reply = f"turn {turn}: BASH DENIED"
            denials = [{"tool_name": "Bash", "tool_use_id": "toolu_03", "tool_input": {"command": text}}]
        print(json.dumps({"type": "result", "subtype": "error_during_execution" if failed else "success",
                          "is_error": failed, "num_turns": 1, "session_id": sid, "result": reply,
                          "stop_reason": "end_turn", "permission_denials": denials}), flush=True)


if __name__ == "__main__":
    main()
