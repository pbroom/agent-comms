"""A Claude launch's real tool receipts, never an agent's capability assertion."""
from __future__ import annotations

import json
from pathlib import Path

from .core import Conflict

COMMANDS = ("git status --short", "uv run pytest --version", "gh --version")
# Opted-in repository implementation. No arbitrary Bash, git reset/clean/push,
# gh API/merge/delete, shell, or permission bypass. Host/managed denies still win.
ALLOWED = (
    "mcp__agent-comms", "Edit(./**)", "Write(./**)",
    "Bash(git status *)", "Bash(git diff *)", "Bash(git log *)", "Bash(git show *)",
    "Bash(git rev-parse *)", "Bash(git add *)", "Bash(git commit *)",
    "Bash(uv run pytest *)", "Bash(pytest *)", "Bash(gh --version)",
    "Bash(gh pr view *)", "Bash(gh pr diff *)", "Bash(gh pr checks *)", "Bash(gh run view *)",
)
PROMPT = (
    "Tool preflight only. Run exactly these three separate Bash commands, without combining or changing them: "
    + "; ".join(COMMANDS) + ". Do not read or edit files, call board tools, or start work. "
    "If a command is denied or fails, report the failure and stop. This checks current access only."
)


def scoped_template(template: list[str]) -> list[str]:
    """Replace additive broad grants for this opted-in launch; keep existing deny rules."""
    replaced = {"--allowedTools", "--allowed-tools", "--permission-mode"}
    forbidden = {"--session-id", "--resume", "-r", "--continue", "-c", "--fork-session",
                 "--output-format", "--max-turns", "--settings", "--permission-prompt-tool", "--setting-sources", "--add-dir", "--no-session-persistence"}
    out, i = [], 0
    while i < len(template):
        arg = template[i]
        flag = arg.split("=", 1)[0]
        if flag in forbidden or any(x in arg for x in ("bypass", "dangerously")):
            raise ValueError("scoped Claude runner has incompatible session, settings or bypass flags")
        if flag in replaced:
            i += 1
            if "=" not in arg:
                if flag in {"--allowedTools", "--allowed-tools"}:
                    while i < len(template) and not template[i].startswith("-") and template[i] != "{prompt}":
                        i += 1
                elif i < len(template):
                    i += 1
            continue
        out.append(arg)
        i += 1
    return out + ["--permission-mode", "dontAsk", "--allowedTools=" + ",".join(ALLOWED)]


def verify(path: Path, session_id: str) -> dict:
    """Require CLI-emitted tool results tied to exact command IDs and session, not prose."""
    if path.stat().st_size > 4 * 1024 * 1024:
        raise ValueError("tool preflight log exceeds 4 MiB")
    calls, successful = {}, set()
    result = None
    for line in path.read_text().splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue  # CLI diagnostics aren't evidence
        if not isinstance(event, dict) or event.get("session_id") != session_id:
            continue
        if event.get("type") == "result":
            result = event
        if event.get("parent_tool_use_id") is not None:
            continue
        for part in event.get("message", {}).get("content", []):
            if not isinstance(part, dict):
                continue
            if event.get("type") == "assistant" and part.get("type") == "tool_use":
                command = part.get("input", {}).get("command")
                if part.get("name") != "Bash" or command not in COMMANDS:
                    raise ValueError("preflight attempted an unexpected tool or command")
                calls[part["id"]] = command
            if event.get("type") == "user" and part.get("type") == "tool_result":
                command = calls.get(part.get("tool_use_id"))
                receipt = event.get("tool_use_result", {})
                if (command and part.get("is_error") is False and isinstance(receipt, dict)
                        and receipt.get("interrupted") is False):
                    successful.add(command)
    if (result is None or result.get("is_error") is not False or result.get("subtype") != "success"
            or result.get("permission_denials") or successful != set(COMMANDS)):
        raise ValueError("missing successful git, pytest or gh tool receipts (or permission denied)")
    return {"state": "verified", "session_id": session_id, "commands": list(COMMANDS), "log": str(path)}


def assert_ready(board, session_id: int, post_id: int) -> None:
    session = board.conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
    if session is None:
        return
    if not session["dispatch_run_id"]:
        for (value,) in board.conn.execute("SELECT value FROM board_state WHERE key LIKE 'dispatch.run.%'"):
            active = json.loads(value)
            if (active.get("tool_preflight") and active.get("agent") == session["agent"]
                    and active.get("status") in ("starting", "running")
                    and post_id in active.get("request_ids", [])):
                raise Conflict("register the verified dispatcher session before starting this request")
        return
    row = board.conn.execute("SELECT value FROM board_state WHERE key=?",
                             ("dispatch.run." + session["dispatch_run_id"],)).fetchone()
    record = json.loads(row[0]) if row else {}
    proof = record.get("tool_preflight")
    if proof is not None and (proof.get("state") != "verified" or record.get("phase") != "work"
                              or post_id not in record.get("request_ids", [])
                              or session["client_kind"] != "claude-code"
                              or session["client_session_id"] != proof.get("session_id")
                              or str(Path(session["worktree"] or session["project"]).resolve()) != str(Path(record["cwd"]).resolve())):
        raise Conflict("runner tool preflight has not passed for this request; report the access blocker")
