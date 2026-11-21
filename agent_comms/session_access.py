"""Access levels of hosted sessions (DESIGN_NOTES "Session host: parent and child sessions").

The hub and a node speak of access in three WORDS, narrowest first: `read-only`, `workspace`, `full`. Nothing
executable crosses the wire: the hub orders the words (a child never gets more than its parent), and the node turns a
word into flags for the agent's CLI from ITS OWN runner template and settings (`prepare`).

Access is decided BEFORE a process is started, from its command line alone, and what the process says afterwards can
only end it (`verify`): it never changes what the session is taken to be.

- Every hosted Claude session has an explicit `--permission-mode` on its command line, because an explicit mode
  beats a settings file's `permissions.defaultMode` (seen live: without one, a `defaultMode` of `bypassPermissions`
  took effect). A runner that names a mode keeps it. A runner that names none gets the CLI's standard mode,
  `manual`, pinned (`PINNED_MODE`; Claude reports it as `default`). That is the one thing ever added to the command
  line of a session of its own (a root); the shipped runners all name a mode, so theirs is unchanged.
- A session's access is COMPUTED from that command line (`claude_label`), at any time and without the process:
  bypass selected (`--permission-mode bypassPermissions`, `--dangerously-skip-permissions`) is `full`; `dontAsk`
  with a `--tools` list within the read tools is `read-only`; anything else `workspace`. For Codex it is the
  thread's sandbox.
- `workspace`: the runner as the node configured it (with the `claude_tool_projects` scoping a session already
  gets, and the mode pinned if it names none).
- `read-only`, ASKED FOR BY WORD (a child): Claude with a HARD list of built-in tools (`--tools=Read,Grep,Glob`:
  nothing that writes or runs a command exists in the session), `dontAsk`, NO settings at all (`--setting-sources=`
  and no `--settings`: a settings file brings hooks, which run commands whatever tools the session has, as well as
  grants), no MCP server but the board's (`--strict-mcp-config` and a config holding only that one), its own deny
  rules kept (`claude_read_only`); Codex with `sandbox: read-only`.
- `full`: Claude with `--permission-mode bypassPermissions` (its deny rules kept); Codex with
  `sandbox: danger-full-access`. Only on a node that set `[dispatch.sessions] allow_full_access = true`; without it a
  request for `full`, and a runner that selects bypass, are refused when the session is opened, never narrowed.

`verify` is the kill switch. Claude names, at the start of every turn, the permission mode it is really in and the
tools it really has. A session that reports `bypassPermissions` and was not started as `full`, one started as `full`
that is not bypassed, and one asked for at `read-only` that is not in `dontAsk` or has a tool beyond the hard list,
are ended. So is a session asked for at `read-only` or `full` whose process reports no mode at all: where the policy
depends on the mode, a mode that cannot be verified is not accepted.
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
BYPASS_MODE = ("its process runs with permission checks bypassed (Claude reports permission mode bypassPermissions) "
               "although it was not started as a full-access session: its command line names another mode, so "
               "something past this node's runner decided otherwise. The session was ended; nothing is run narrower "
               "instead")
NOT_BYPASS = ("it was started as a full-access session, but its process reports permission mode {mode}: the session "
              "was ended rather than run narrower than asked")
NOT_DONT_ASK = ("it is a read-only session, but its process reports permission mode {mode} and not dontAsk: the "
                "session was ended")
EXTRA_TOOLS = ("it is a read-only session, but its process reports tools beyond the read tools and the board's "
               "({tools}): the session was ended")
UNVERIFIABLE = ("it was asked for at {asked} access, but its process did not report {what}: what cannot be verified "
                "is not accepted where the access depends on it, and the session was ended")

BOARD_SERVER = "agent-comms"                    # the board's MCP server, as every runner and config here names it
# Everything a read-only Claude session has: these built-in tools (a hard list, `--tools`) and the board's.
READ_ONLY_BUILTINS = ("Read", "Grep", "Glob")
READ_ONLY_ALLOWED = ("mcp__" + BOARD_SERVER, "Read(./**)", "Grep(./**)", "Glob(./**)")
READ_ONLY_DENIED = ("Edit", "Write", "NotebookEdit", "Bash", "Read(~/.config/agent-comms/**)", "Read(~/.ssh/**)")
NO_SETTINGS = "--setting-sources="              # the single-token empty form: no user, project or local settings
CLAUDE_FULL = ("--permission-mode", "bypassPermissions")
# The mode pinned on a Claude runner that names none: the CLI's standard one, in which whatever would ask is
# refused (the host always adds `--permission-prompts none`). Claude's `init` calls it `default`.
PINNED_MODE = "manual"
_CLAUDE_ALLOW_BYPASS = "--allow-dangerously-skip-permissions"   # makes the mode selectable; it does not select it
_CLAUDE_BYPASS = ("--dangerously-skip-permissions", _CLAUDE_ALLOW_BYPASS)
_ALLOW = ("--allowedTools", "--allowed-tools")
_DENY = ("--disallowedTools", "--disallowed-tools")
# Flags that take values up to the next option.
_VARIADIC = frozenset((*_ALLOW, *_DENY, "--add-dir", "--mcp-config", "--tools"))
_SINGLE = frozenset(("--permission-mode", "--settings", "--setting-sources"))
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
    granted: str | None         # the access a session started from it has (None: a CLI this module does not know)
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


def claude_mode(template: list[str]) -> str | None:
    """The permission mode this Claude command line NAMES: its last `--permission-mode`, or `bypassPermissions` for
    `--dangerously-skip-permissions`. None: it names none, and the mode would be whatever a settings file says."""
    _, values = _without(template, {"--permission-mode"})
    if any(a.split("=", 1)[0] == "--dangerously-skip-permissions" for a in template):
        return "bypassPermissions"
    modes = values.get("--permission-mode", [])
    return modes[-1] if modes else None


def claude_runs_bypass(template: list[str]) -> bool:
    """The command line SELECTS the mode without permission checks. `--allow-dangerously-skip-permissions` alone
    does not: it only makes the mode selectable (the adapter still refuses such a runner where full access is not
    allowed)."""
    return (claude_mode(template) or "").lower() == "bypasspermissions"


def claude_pinned(template: list[str]) -> list[str]:
    """The template with an explicit permission mode: its own, or PINNED_MODE when it names none."""
    return list(template) if claude_mode(template) is not None else list(template) + ["--permission-mode", PINNED_MODE]


def claude_label(template: list[str]) -> str:
    """The access of a Claude session started from this command line, computed from it alone: selecting bypass is
    `full`; `dontAsk` with a `--tools` list within the read tools is `read-only`; anything else `workspace`. (A
    runner that only calls itself read-only, with allow rules for `git log` and so a shell, is `workspace`.) Valid
    before the process has said anything, and after any restart or change of the runner."""
    if claude_runs_bypass(template):
        return "full"
    _, values = _without(template, {"--tools"})
    tools = [t for v in values.get("--tools", []) for t in runner_preflight.split_tools(v)]
    hard = "--tools" in values and bool(tools) and set(tools) <= set(READ_ONLY_BUILTINS)
    return "read-only" if claude_mode(template) == "dontAsk" and hard else "workspace"


def claude_read_only(template: list[str],
                     board_mcp: Callable[[list[str]], str | None] | None = None) -> list[str]:
    """Any Claude runner as a session asked for at read-only.

    - `--tools=Read,Grep,Glob`: the built-in tools the session HAS. Without it `--allowedTools` only adds
      pre-approvals, and a grant in a settings file (`Bash(*)`, `Write`) stays in force under `dontAsk` (seen live on
      2.1.322). With it there is no tool that writes or runs a command, whatever is allowed anywhere.
    - `--setting-sources=` and no `--settings`: it loads no Claude settings at all. A settings file also brings
      HOOKS, and a command hook runs whatever tools the session has (seen live: a SessionStart hook and a
      PreToolUse hook on Read, from a `--settings` file, both ran in print mode).
    - `--permission-mode dontAsk`, given explicitly.
    - `--strict-mcp-config`, and for `--mcp-config` only what `board_mcp` returns: the path of a config holding the
      board's server alone, made from the configs the runner itself names (which it is given). None, or no
      `board_mcp`: no MCP server at all, and so no board tools.
    - Allowed: the board tools the runner names (a runner that allows only some is not widened to all; one that
      names none gets the server) and reads inside the run directory. Denied: Edit, Write, NotebookEdit, Bash, the
      token directory and ~/.ssh, beside the runner's own deny rules.
    Its permission mode, allow list, bypass flags, extra directories, tool list, settings and MCP configs go;
    everything else stays."""
    kept, values = _without(template, {"--permission-mode", "--add-dir", "--tools", "--mcp-config", "--settings",
                                       "--setting-sources", "--strict-mcp-config", *_ALLOW, *_DENY, *_CLAUDE_BYPASS})
    board = [t for flag in _ALLOW for v in values.get(flag, []) for t in runner_preflight.split_tools(v)
             if t == READ_ONLY_ALLOWED[0] or t.startswith(READ_ONLY_ALLOWED[0] + "__")]
    allowed = list(dict.fromkeys(board or [READ_ONLY_ALLOWED[0]])) + list(READ_ONLY_ALLOWED[1:])
    denied = list(READ_ONLY_DENIED)
    for flag in _DENY:
        for v in values.get(flag, []):
            denied += [t for t in runner_preflight.split_tools(v) if t not in denied]
    config = board_mcp(values.get("--mcp-config", [])) if board_mcp is not None else None
    return (kept + [NO_SETTINGS, "--strict-mcp-config"] + (["--mcp-config", config] if config else [])
            + ["--tools=" + ",".join(READ_ONLY_BUILTINS), "--permission-mode", "dontAsk",
               "--allowedTools=" + ",".join(allowed), "--disallowedTools=" + ",".join(denied)])


def claude_full(template: list[str]) -> list[str]:
    """A Claude runner with permission prompts bypassed: its own permission mode replaced by `bypassPermissions`.
    Its deny rules stay (a denied tool stays unavailable in that mode, DESIGN_NOTES)."""
    kept, _ = _without(template, {"--permission-mode", *_CLAUDE_BYPASS})
    return kept + list(CLAUDE_FULL)


def verify(asked: str | None, launched: str | None, mode: Any, tools: Any = ()) -> str | None:
    """The kill switch: what a Claude process reported when a turn started (`permissionMode`, `tools`), held against
    the session. `asked`: the access word it was opened with (None: a session of its own). `launched`: the access
    computed from its command line (`prepare`). Returns why the session must be ended, or None. It never changes
    what the session's access is taken to be.

    - Reports `bypassPermissions`, and was not started as `full`: ended.
    - Started as `full`, and does not report `bypassPermissions`: ended, not run narrower.
    - Asked for at `read-only`: must report `dontAsk`, and tools, none of them beyond READ_ONLY_BUILTINS and the
      board's; otherwise ended.
    - Reports no mode (not a name): nothing can be verified. That is accepted for a session whose command line
      names a mode that is not bypass and that was asked for at nothing in particular (or at `workspace`); a
      session asked for at `read-only` or `full` is ended."""
    if not isinstance(mode, str) or not _MODE.fullmatch(mode):
        if asked in ("read-only", "full"):
            return UNVERIFIABLE.format(asked=asked, what="the permission mode it runs in")
        return None
    if mode == "bypassPermissions":
        return None if launched == "full" else BYPASS_MODE
    if launched == "full":
        return NOT_BYPASS.format(mode=mode)
    if asked == "read-only":
        if mode != "dontAsk":
            return NOT_DONT_ASK.format(mode=mode)
        named = [t for t in tools if isinstance(t, str)] if isinstance(tools, (list, tuple)) else []
        if not named:
            return UNVERIFIABLE.format(asked=asked, what="the tools it has")
        extra = [t for t in named if t not in READ_ONLY_BUILTINS and not t.startswith(READ_ONLY_ALLOWED[0] + "__")]
        if extra:
            return EXTRA_TOOLS.format(tools=", ".join(extra[:5]) + (" ..." if len(extra) > 5 else ""))
    return None


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
    """The runner template for a session that is to run at `access` (None: a session of its own, which runs its
    runner as it is), the access a session started from it HAS (computed from that command line), and what its
    adapter must be told. ValueError: this machine cannot grant it (`full` without `allow_full`; a level this
    runner cannot be brought to). `cwd`: the session's run directory, which is the only thing a Codex runner's
    `--cd` may name. `board_mcp`: see `claude_read_only`.

    Without a word a Claude template is changed in one case only: a runner that names no permission mode gets
    PINNED_MODE. A runner that turns permission checks or the sandbox off is refused by its adapter, as ever,
    unless this machine allows full access."""
    access = level(access)
    kind = os.path.basename(template[0]) if template else ""
    if kind not in ("claude", "codex"):
        if access is not None:
            raise ValueError("this agent's CLI has no access levels")
        return Prepared(list(template), None, None, True)
    if kind == "claude":
        from .providers.claude import bypasses_permissions

        if access is None:
            if bypasses_permissions(template) and not allow_full:
                return Prepared(list(template), claude_label(template), None, True)     # its adapter refuses it
            # As configured, with an explicit mode. The adapter refuses bypass flags unless it is told this machine
            # allows full access.
            pinned = claude_pinned(template)
            return Prepared(pinned, claude_label(pinned), "full" if bypasses_permissions(template) else None, True)
        if access == "full":
            if not allow_full:
                raise ValueError(FULL_REFUSED)
            runs = claude_runs_bypass(template)
            return Prepared(list(template) if runs else claude_full(template), "full", "full", runs)
        if access == "read-only":
            return Prepared(claude_read_only(template, board_mcp), "read-only", None, False)
        if claude_runs_bypass(template):
            raise ValueError(WORKSPACE_FROM_BYPASS)
        # `workspace`: as configured, less the flag that would only make bypassing selectable (a child asked for at
        # workspace is not a full session), with an explicit mode.
        pinned = claude_pinned(_without(template, {_CLAUDE_ALLOW_BYPASS})[0])
        return Prepared(pinned, claude_label(pinned), None, True)
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
