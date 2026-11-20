"""A stand-in for `claude -p --input-format stream-json --output-format stream-json`: no model, no network.

It speaks the part of Claude Code's stream the session host reads (DESIGN_NOTES "Session host (PR 1)"): nothing until
the first stdin line, then per turn one `system`/`init` and one `result`, both with the session id it was given. The
tests and scripts/session-host-e2e.py install it as an executable named `claude` (the adapter goes by that name).

What a turn does is in its text:
- `sleep N ...`: the turn takes N seconds.
- `fail ...`: the result is an error.
- `deny ...`: the result lists a refused Write (the turn itself succeeds, as Claude's does).
- `big ...`: the reply is 40 KB.
The reply is otherwise `turn <k of this process>: <TEXT IN CAPITALS>`.
With STAND_IN_ARGV set (an env var name the runner passes on), each start appends its argv to that file.
"""
import json
import os
import re
import sys
import time


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
        print(json.dumps({"type": "result", "subtype": "error_during_execution" if failed else "success",
                          "is_error": failed, "num_turns": 1, "session_id": sid, "result": reply,
                          "stop_reason": "end_turn", "permission_denials": denials}), flush=True)


if __name__ == "__main__":
    main()
