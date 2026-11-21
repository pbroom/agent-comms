"""Access levels of hosted sessions (DESIGN_NOTES "Session host: parent and child sessions").

The hub and a node speak of access in three WORDS, narrowest first: `read-only`, `workspace`, `full`. Nothing
executable crosses the wire: the hub orders the words (a child never gets more than its parent), and the node turns a
word into flags for the agent's CLI from ITS OWN runner template and settings (`prepare`).

- `workspace`: the runner as the node configured it (with the `claude_tool_projects` scoping a session already
  gets).
- `read-only`: Claude with a HARD list of built-in tools (`--tools=Read,Grep,Glob`: nothing that writes or runs a
  command exists in the session, whatever any settings file allows), `dontAsk`, no MCP server but the board's
  (`--strict-mcp-config` and a config holding only that one), its own deny rules kept (`claude_read_only`); Codex
  with `sandbox: read-only`. A runner that already calls itself read-only gets the same.
- `full`: Claude with `--permission-mode bypassPermissions` (its deny rules kept); Codex with
  `sandbox: danger-full-access`. Only on a node that set `[dispatch.sessions] allow_full_access = true`; without it a
  request for `full` is refused, never narrowed.

Flags are what a session is ASKED to run with. What Claude really runs with can also come from settings files the
argv does not show (a `permissions.defaultMode`), so the mode and the tools its process reports when a turn starts
are checked against what was asked (`verify`), and a session that runs with permission checks bypassed where it may
not is ended. A session's access as the hub hears it is the verified one.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any, Callable

from . import runner_preflight

LEVELS = ("read-only", "workspace", "full")     # narrowest first
RANK = {name: i for i, name in enumerate(LEVELS)}
ISOLATIONS = ("shared", "worktree")             # the parent's checkout, or a git worktree of its own
ROLES = ("implementation", "research", "review", "design", "test", "general")
# One word, mapped per CLI: Claude Code's `--effort` (2.1.322 lists low, medium, high, xhigh, max) and the `effort`
# of Codex's `turn/start` (a value the model advertises: every model codex-cli 0.171.0 lists has these five, some
# also `ultra`). Only what both take is offered.
EFFORTS = ("low", "medium", "high", "xhigh", "max")

FULL_REFUSED = ("full access is not allowed on this node: its settings need [dispatch.sessions] allow_full_access = "
                "true before a session may run with permission checks bypassed")
WORKSPACE_FROM_BYPASS = ("this node's Claude runner for the agent bypasses permission checks, so it can run at `full` "
                         "(as configured) or `read-only`, not at `workspace`: nothing on the node says what its "
                         "workspace permissions would be")
CODEX_UNSUPPORTED = ("this node's Codex runner for the agent carries {flag}, which a hosted session does not take "
                     "(approvals are always off here, and hook trust is not for a session to waive), with or without "
                     "allow_full_access")
BYPASS_MODE = ("its process runs with permission checks bypassed (Claude reports permission mode bypassPermissions, "
               "which here comes from a settings file and not from this node's runner), and {why}. The session was "
               "ended; nothing is run narrower instead")
NOT_BYPASS = ("it was asked for at full access, but its process reports permission mode {mode}: the session was ended "
              "rather than run narrower than asked")
NOT_DONT_ASK = ("it is a read-only session, but its process reports permission mode {mode} and not dontAsk: the "
                "session was ended")
EXTRA_TOOLS = ("it is a read-only session, but its process reports tools beyond the read tools and the board's "
               "({tools}): the session was ended")

BOARD_SERVER = "agent-comms"                    # the board's MCP server, as every runner and config here names it
# Everything a read-only Claude session has: these built-in tools (a hard list, `--tools`) and the board's.
READ_ONLY_BUILTINS = ("Read", "Grep", "Glob")
READ_ONLY_ALLOWED = ("mcp__" + BOARD_SERVER, "Read(./**)", "Grep(./**)", "Glob(./**)")
READ_ONLY_DENIED = ("Edit", "Write", "NotebookEdit", "Bash", "Read(~/.config/agent-comms/**)", "Read(~/.ssh/**)")
CLAUDE_FULL = ("--permission-mode", "bypassPermissions")
_CLAUDE_ALLOW_BYPASS = "--allow-dangerously-skip-permissions"   # makes the mode selectable; it does not select it
_CLAUDE_BYPASS = ("--dangerously-skip-permissions", _CLAUDE_ALLOW_BYPASS)
_ALLOW = ("--allowedTools", "--allowed-tools")
_DENY = ("--disallowedTools", "--disallowed-tools")
# Flags that take values up to the next option.
_VARIADIC = frozenset((*_ALLOW, *_DENY, "--add-dir", "--mcp-config", "--tools"))
_SINGLE = frozenset(("--permission-mode",))
# The Codex flags that really turn the sandbox off. The adapter refuses more than these (BYPASS_FLAGS): the others
# are not full access, and stay refused whatever this machine allows.
CODEX_SANDBOX_OFF = frozenset(("--dangerously-bypass-approvals-and-sandbox", "--yolo"))
_MODE = re.compile(r"[A-Za-z]{1,40}")


def level(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or value not in LEVELS:
        raise ValueError("access must be one of " + ", ".join(LEVELS))
    return value


def effort(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or value not in EFFORTS:
        raise ValueError("effort must be one of " + ", ".join(EFFORTS))
    return value


def within(access: str, limit: str) -> bool:
    """`access` is no more than `limit`."""
    return RANK[access] <= RANK[limit]


@dataclass(frozen=True)
class Prepared:
    template: list[str]         # the runner template to start the session from
    granted: str | None         # the access the session is started with (None: a CLI this module does not know)
    configure: str | None       # the access word the CLI's adapter must be told (its `configure`), or None
    as_configured: bool         # the runner is used as this machine configured it


def _without(template: list[str], drop: set[str]) -> tuple[list[str], dict[str, list[str]]]:
    """A Claude template without these flags and their values, and the values each one had."""
    kept: list[str] = []
    values: dict[str, list[str]] = {}
    i = 0
    while i < len(template):
        arg = template[i]
        i += 1
        flag, eq, value = arg.partition("=")
        if not arg.startswith("--") or flag not in drop:
            kept.append(arg)
            continue
        got = values.setdefault(flag, [])
        if eq:
            got.append(value)
        elif flag in _VARIADIC:
            while i < len(template) and not template[i].startswith("-") and template[i] != "{prompt}":
                got.append(template[i])
                i += 1
        elif flag in _SINGLE and i < len(template) and template[i] != "{prompt}":
            got.append(template[i])
            i += 1
    return kept, values


def claude_runs_bypass(template: list[str]) -> bool:
    """The runner SELECTS the mode without permission checks: `--dangerously-skip-permissions`, or
    `--permission-mode bypassPermissions`. `--allow-dangerously-skip-permissions` alone does not: it only makes the
    mode selectable (the adapter still refuses such a runner where full access is not allowed)."""
    for i, arg in enumerate(template):
        flag, eq, value = arg.partition("=")
        if flag == "--dangerously-skip-permissions":
            return True
        if flag == "--permission-mode":
            mode = value if eq else (template[i + 1] if i + 1 < len(template) else "")
            if mode.lower() == "bypasspermissions":
                return True
    return False


def claude_native(template: list[str]) -> str:
    """What a Claude runner asks for as it is written. What its process really runs with is checked at each turn's
    start (`verify`)."""
    if claude_runs_bypass(template):
        return "full"
    return "read-only" if runner_preflight.read_only(template) else "workspace"


def claude_read_only(template: list[str],
                     board_mcp: Callable[[list[str]], str | None] | None = None) -> list[str]:
    """Any Claude runner as a read-only session.

    - `--tools=Read,Grep,Glob`: the built-in tools the session HAS. Without it `--allowedTools` only adds
      pre-approvals, and a grant in a settings file (`Bash(*)`, `Write`) stays in force under `dontAsk` (seen live on
      2.1.322). With it there is no tool that writes or runs a command, whatever is allowed anywhere.
    - `--permission-mode dontAsk`, given explicitly, so that a settings file's `defaultMode` does not apply.
    - `--strict-mcp-config`, and for `--mcp-config` only what `board_mcp` returns: the path of a config holding the
      board's server alone, made from the configs the runner itself names (which it is given). None, or no
      `board_mcp`: no MCP server at all, and so no board tools.
    - Allowed: the board tools the runner names (a runner that allows only some is not widened to all; one that
      names none gets the server) and reads inside the run directory. Denied: Edit, Write, NotebookEdit, Bash, the
      token directory and ~/.ssh, beside the runner's own deny rules.
    Its permission mode, allow list, bypass flags, extra directories, tool list and MCP configs go; everything else
    stays. With a board config the result is a runner `runner_preflight.read_only` recognises."""
    kept, values = _without(template, {"--permission-mode", "--add-dir", "--tools", "--mcp-config",
                                       "--strict-mcp-config", *_ALLOW, *_DENY, *_CLAUDE_BYPASS})
    board = [t for flag in _ALLOW for v in values.get(flag, []) for t in runner_preflight.split_tools(v)
             if t == READ_ONLY_ALLOWED[0] or t.startswith(READ_ONLY_ALLOWED[0] + "__")]
    allowed = list(dict.fromkeys(board or [READ_ONLY_ALLOWED[0]])) + list(READ_ONLY_ALLOWED[1:])
    denied = list(READ_ONLY_DENIED)
    for flag in _DENY:
        for v in values.get(flag, []):
            denied += [t for t in runner_preflight.split_tools(v) if t not in denied]
    config = board_mcp(values.get("--mcp-config", [])) if board_mcp is not None else None
    return (kept + ["--strict-mcp-config"] + (["--mcp-config", config] if config else [])
            + ["--tools=" + ",".join(READ_ONLY_BUILTINS), "--permission-mode", "dontAsk",
               "--allowedTools=" + ",".join(allowed), "--disallowedTools=" + ",".join(denied)])


def claude_full(template: list[str]) -> list[str]:
    """A Claude runner with permission prompts bypassed: its own permission mode replaced by `bypassPermissions`.
    Its deny rules stay (a denied tool stays unavailable in that mode, DESIGN_NOTES)."""
    kept, _ = _without(template, {"--permission-mode", *_CLAUDE_BYPASS})
    return kept + list(CLAUDE_FULL)


def verify(asked: str | None, started: str | None, mode: Any, tools: Any = (), *,
           allow_full: bool = False) -> tuple[str | None, str | None]:
    """What a Claude process reported when a turn started (`permissionMode`, `tools`), held against the session:
    `asked` is the access word it was opened with (None: a session of its own, which has what its runner has) and
    `started` what `prepare` started it with. Returns (the access it really has, None), or (None, why it must be
    ended). A mode that was not reported (not a name) verifies nothing: (None, None).

    - `bypassPermissions` is full access. It stands only where this machine allows full access and the session was
      not asked for at less. Otherwise the session is ended: the mode came from a settings file, past the runner.
    - A session asked for at `full` that does not run that way is ended too, not run narrower.
    - A read-only session must report `dontAsk` and no tool beyond READ_ONLY_BUILTINS and the board's.
    - Anything else is `workspace`: the runner as configured, in whatever mode the machine's owner chose."""
    if not isinstance(mode, str) or not _MODE.fullmatch(mode):
        return None, None
    if mode == "bypassPermissions":
        if not allow_full:
            return None, BYPASS_MODE.format(why="this node does not allow full access ([dispatch.sessions] "
                                                "allow_full_access is not true)")
        if asked not in (None, "full"):
            return None, BYPASS_MODE.format(why=f"the session was asked for at {asked} access")
        return "full", None
    if asked == "full":
        return None, NOT_BYPASS.format(mode=mode)
    if started == "read-only":
        if mode != "dontAsk":
            return None, NOT_DONT_ASK.format(mode=mode)
        extra = [t for t in tools if isinstance(t, str) and t not in READ_ONLY_BUILTINS
                 and not t.startswith(READ_ONLY_ALLOWED[0] + "__")] if isinstance(tools, (list, tuple)) else []
        if extra:
            return None, EXTRA_TOOLS.format(tools=", ".join(extra[:5]) + (" ..." if len(extra) > 5 else ""))
        return "read-only", None
    return "workspace", None


def _codex_safe(template: list[str]) -> tuple[list[str], bool]:
    """A Codex runner template with what turns the SANDBOX off taken out (CODEX_SANDBOX_OFF; a `danger-full-access`
    sandbox becomes `workspace-write`), and whether there was anything to take out. Only that is full access. The
    adapter refuses such flags by name; a session that may run at `full` gets the sandbox from its access word
    instead. Any other flag the adapter refuses is left in, for it to refuse."""
    from .providers.codex import VALUE_FLAGS, _unquoted

    out, was, i = list(template[:1]), False, 1
    while i < len(template):
        arg = template[i]
        i += 1
        if arg.split("=", 1)[0] in CODEX_SANDBOX_OFF:
            was = True
            continue
        if arg.startswith("--"):
            flag, eq, value = arg.partition("=")
        elif arg.startswith("-") and len(arg) >= 2:
            flag, eq, value = arg[:2], "=" if len(arg) > 2 else "", arg[2:].removeprefix("=")
        else:
            out.append(arg)
            continue
        kind = VALUE_FLAGS.get(flag)
        if kind is None or (not eq and i >= len(template)):
            out.append(arg)
            continue
        if not eq:
            value, i = template[i], i + 1
        if kind == "sandbox" and value == "danger-full-access":
            was, value = True, "workspace-write"
        elif kind == "config":
            key, sep, val = value.partition("=")
            if sep and key.strip() == "sandbox_mode" and _unquoted(val) == "danger-full-access":
                was, value = True, 'sandbox_mode="workspace-write"'
        out += [flag, value]
    return (out, True) if was else (list(template), False)


def _codex_sandbox(template: list[str], cwd: str | None) -> str:
    from .providers.codex import session_command

    return session_command(template, None, cwd)[1]["sandbox"]


def prepare(template: list[str], access: Any = None, *, allow_full: bool = False, cwd: str | None = None,
            board_mcp: Callable[[list[str]], str | None] | None = None) -> Prepared:
    """The runner template for a session that is to run at `access` (None: as its runner has it), what the session
    is started with, and what its adapter must be told. ValueError: this machine cannot grant it (`full` without
    `allow_full`; a level this runner cannot be brought to). `cwd`: the session's run directory, which is the only
    thing a Codex runner's `--cd` may name. `board_mcp`: see `claude_read_only`.

    A runner that turns permission checks or the sandbox off, opened without a word on a machine that does not
    allow full access, is returned untouched: its adapter refuses it, as it did before there were access levels."""
    access = level(access)
    kind = os.path.basename(template[0]) if template else ""
    if kind not in ("claude", "codex"):
        if access is not None:
            raise ValueError("this agent's CLI has no access levels")
        return Prepared(list(template), None, None, True)
    if kind == "claude":
        from .providers.claude import bypasses_permissions

        native = claude_native(template)
        if access is None and not allow_full and bypasses_permissions(template):
            return Prepared(list(template), native, None, True)
        want = access or native
        if want == "full":
            if not allow_full:
                raise ValueError(FULL_REFUSED)
            return Prepared(list(template) if native == "full" else claude_full(template), "full", "full",
                            native == "full")
        if want == "read-only" or native == "read-only":
            # Also a runner that is read-only as written, and `workspace` asked of one: the same hard limits.
            return Prepared(claude_read_only(template, board_mcp), "read-only", None, False)
        if native == "full":
            raise ValueError(WORKSPACE_FROM_BYPASS)
        # As configured, less the flag that would only make bypassing selectable: this is not a full session.
        return Prepared(_without(template, {_CLAUDE_ALLOW_BYPASS})[0], "workspace", None, True)
    from .providers.codex import BYPASS_FLAGS

    other = sorted(a.split("=", 1)[0] for a in template if a.split("=", 1)[0] in BYPASS_FLAGS - CODEX_SANDBOX_OFF)
    if other and (allow_full or access is not None):
        raise ValueError(CODEX_UNSUPPORTED.format(flag=other[0]))
    safe, was_bypass = _codex_safe(template)
    try:
        native = "full" if was_bypass else ("workspace" if _codex_sandbox(safe, cwd) == "workspace-write"
                                            else "read-only")
    except ValueError:
        if access is not None:
            raise
        return Prepared(list(template), None, None, True)   # its adapter says what is wrong with it, as before
    if access is None and not (native == "full" and allow_full):
        return Prepared(list(template), native, None, True)
    want = access or native
    if want == "full" and not allow_full:
        raise ValueError(FULL_REFUSED)
    # The word becomes the thread's sandbox. `safe` no longer carries anything that turns the sandbox off.
    if want == "full":
        return Prepared(safe, "full", "full", was_bypass)
    if want == "read-only":
        return Prepared(safe, "read-only", None if native == "read-only" else "read-only", native == "read-only")
    if native == "full":
        return Prepared(safe, "workspace", "workspace", False)      # a runner that bypasses, brought down to it
    return Prepared(safe, native, None, True)               # as configured: `workspace-write`, or read-only
