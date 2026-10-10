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
    "Bash(git fetch origin)", "Bash(git fetch origin main)",
    "Bash(git worktree add -b codex/*)", "Bash(git switch -c codex/*)",
    "Bash(uv run pytest *)", "Bash(pytest *)", "Bash(gh --version)",
    "Bash(gh pr view *)", "Bash(gh pr diff *)", "Bash(gh pr checks *)", "Bash(gh run view *)",
)
DENIED = ("Bash(git *--force*)", "Bash(git *--discard-changes*)", "Bash(git * -f*)")
PROMPT = (
    "Tool preflight only. Run exactly these three separate Bash commands, without combining or changing them: "
    + "; ".join(COMMANDS) + ". Do not read or edit files, call board tools, or start work. "
    "If a command is denied or fails, report the failure and stop. This checks current access only."
)


# A read-only Claude runner (the triage agent's): board tools, reading files inside its run directory and local
# read-only git, with Edit and Write explicitly denied. An opted-in project keeps it as configured (no scoped override, no
# tool preflight): replacing its tools with ALLOWED would widen it to commits and test runs.
# File reads are folder-scoped: a bare Read/Grep/Glob grant reads any file the user can (verified on Claude Code
# 2.1.282 in dontAsk mode), e.g. ~/.config/agent-comms/*.token. `git diff` is left out: `git diff --no-index` reads
# arbitrary files. No `gh` command qualifies: `gh pr view <url>` and `gh pr list -R host/owner/repo` connect to any
# host the run names (review of #60: a local listener received the connection on gh 2.101.0), so the host name and
# the request can carry data the run chose. Coverage checks use local git history and the board instead.
READ_ONLY_TOOLS = frozenset((
    "mcp__agent-comms", "Read(./**)", "Grep(./**)", "Glob(./**)",
    "Bash(git log *)", "Bash(git show *)", "Bash(git status *)", "Bash(git rev-parse *)",
))
# Denies a read-only runner must carry: no edits, never the board's token files or SSH keys.
READ_ONLY_DENIES = frozenset(("Edit", "Write", "Read(~/.config/agent-comms/**)", "Read(~/.ssh/**)"))
GIT_OUTPUT_DENY = "Bash(git *--output*)"   # git log/show --output=<file> writes a file
_READ_ONLY_FORBIDDEN = {"--settings", "--add-dir", "--permission-prompt-tool", "--allow-dangerously-skip-permissions",
                        "--dangerously-skip-permissions"}


def split_tools(value: str) -> list[str]:
    """A Claude --allowedTools value: names separated by commas or spaces, except inside parentheses."""
    out, cur, depth = [], "", 0
    for ch in value:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(depth - 1, 0)
        if depth == 0 and ch in ", ":
            if cur:
                out.append(cur)
            cur = ""
            continue
        cur += ch
    if cur:
        out.append(cur)
    return out


def _flag_values(template: list[str], names: set[str]) -> list[str] | None:
    """Every value given to these flags (`--flag=v` or `--flag v1 v2 ...` up to the next option or {prompt})."""
    found, i = None, 0
    while i < len(template):
        arg = template[i]
        flag = arg.split("=", 1)[0]
        i += 1
        if flag not in names:
            continue
        found = found or []
        if "=" in arg:
            found.append(arg.split("=", 1)[1])
            continue
        while i < len(template) and not template[i].startswith("-") and template[i] != "{prompt}":
            found.append(template[i])
            i += 1
    return found


def read_only(template: list[str]) -> bool:
    """A `claude` runner that declares itself read-only:
    - exactly `--permission-mode dontAsk`, and `--strict-mcp-config` (so no user- or project-scope MCP server, whose
      write tools the user's settings may allow, is loaded);
    - `--allowedTools` naming only READ_ONLY_TOOLS (folder-scoped reads, local read-only git) or agent-comms tools,
      and never a `gh` command (it can reach any host);
    - `--disallowedTools` naming every READ_ONLY_DENIES entry (Edit, Write, the token directory, ~/.ssh), plus
      GIT_OUTPUT_DENY whenever a `git` command is allowed;
    - no flag that could add grants or bypass permissions.
    The explicit denials are the declaration: the shipped claude-code runner (board tools only, nothing denied) is not
    read-only, so an opted-in project still gives it the scoped tools. Allow rules in the user's or the project's
    Claude Code settings files still apply under dontAsk and cannot be seen from the argv (DESIGN_NOTES)."""
    import os
    if not template or os.path.basename(template[0]) != "claude" or "--strict-mcp-config" not in template:
        return False
    if any(a.split("=", 1)[0] in _READ_ONLY_FORBIDDEN or "bypass" in a.lower() or "dangerously" in a.lower()
           for a in template):
        return False
    modes = _flag_values(template, {"--permission-mode"})
    if modes != ["dontAsk"]:
        return False
    denied = {t for v in (_flag_values(template, {"--disallowedTools", "--disallowed-tools"}) or [])
              for t in split_tools(v)}
    tools = [t for v in (_flag_values(template, {"--allowedTools", "--allowed-tools"}) or []) for t in split_tools(v)]
    if any(t.replace(" ", "").startswith(("Bash(gh", "Bash(*gh")) or t.startswith("Bash(") and "gh " in t
           for t in tools):
        return False
    required = set(READ_ONLY_DENIES) | ({GIT_OUTPUT_DENY} if any(t.startswith("Bash(git ") for t in tools) else set())
    if not required <= denied:
        return False
    return bool(tools) and all(t in READ_ONLY_TOOLS or t.startswith("mcp__agent-comms__") for t in tools)


def scoped_template(template: list[str]) -> list[str]:
    """Replace additive broad grants for this opted-in launch; keep existing deny rules."""
    replaced = {"--allowedTools", "--allowed-tools", "--permission-mode"}
    forbidden = {"--session-id", "--resume", "-r", "--continue", "-c", "--fork-session",
                 "--output-format", "--max-turns", "--settings", "--permission-prompt-tool", "--setting-sources", "--add-dir", "--no-session-persistence"}
    out, i, denied = [], 0, list(DENIED)
    while i < len(template):
        arg = template[i]
        flag = arg.split("=", 1)[0]
        if flag in forbidden or any(x in arg for x in ("bypass", "dangerously")):
            raise ValueError("scoped Claude runner has incompatible session, settings or bypass flags")
        if flag in {"--disallowedTools", "--disallowed-tools"}:
            if "=" in arg:
                denied.append(arg.split("=", 1)[1])
                i += 1
            else:
                i += 1
                while i < len(template) and not template[i].startswith("-") and template[i] != "{prompt}":
                    denied.append(template[i])
                    i += 1
            continue
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
    return out + ["--permission-mode", "dontAsk", "--allowedTools=" + ",".join(ALLOWED),
                  "--disallowedTools=" + ",".join(denied)]


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
    if proof is not None:
        rules = board._dispatch_rows(record.get("rule_id"))
        if (record.get("status") not in ("starting", "running") or not rules
                or board._dispatch_state(rules[0], board._dispatch_target(rules[0])) not in ("active", "exhausted")):
            raise Conflict("runner launch authorization ended before request pickup")
    if proof is not None and (proof.get("state") != "verified" or record.get("phase") != "work"
                              or post_id not in record.get("request_ids", [])
                              or session["client_kind"] != "claude-code"
                              or session["client_session_id"] != proof.get("session_id")
                              or str(Path(session["worktree"] or session["project"]).resolve()) != str(Path(record["cwd"]).resolve())):
        raise Conflict("runner tool preflight has not passed for this request; report the access blocker")
