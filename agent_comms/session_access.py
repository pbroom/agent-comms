"""Access levels of hosted sessions (DESIGN_NOTES "Session host: parent and child sessions").

The hub and a node speak of access in three WORDS, narrowest first: `read-only`, `workspace`, `full`. Nothing
executable crosses the wire: the hub orders the words (a child never gets more than its parent), and the node turns a
word into flags for the agent's CLI from ITS OWN runner template and settings (`prepare`).

- `workspace`: the runner as the node configured it (with the `claude_tool_projects` scoping a session already
  gets). A runner that is read-only as configured stays read-only: the node reports what the session really has.
- `read-only`: Claude with the read tools and the board tools only, in `dontAsk` mode, its own deny rules kept
  (`claude_read_only`, the narrowing runner_preflight.read_only recognises); Codex with `sandbox: read-only`.
- `full`: Claude with `--permission-mode bypassPermissions` (its deny rules kept); Codex with
  `sandbox: danger-full-access`. Only on a node that set `[dispatch.sessions] allow_full_access = true`; without it a
  request for `full` is refused, never narrowed.

A session opened without a word (a root) has what its runner template has: bypass flags are `full`, a read-only
runner is `read-only`, anything else `workspace` (`native`).
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

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
SETTINGS_REFUSED = ("this node's Claude runner for the agent loads a --settings file, whose grants cannot be seen, so "
                    "it cannot be narrowed to read-only")

# What a read-only Claude session may use: runner_preflight.READ_ONLY_TOOLS, in a fixed order.
READ_ONLY_ALLOWED = ("mcp__agent-comms", "Read(./**)", "Grep(./**)", "Glob(./**)", "Bash(git log *)",
                     "Bash(git show *)", "Bash(git status *)", "Bash(git rev-parse *)")
READ_ONLY_DENIED = ("Edit", "Write", "NotebookEdit", runner_preflight.GIT_OUTPUT_DENY,
                    "Read(~/.config/agent-comms/**)", "Read(~/.ssh/**)")
CLAUDE_FULL = ("--permission-mode", "bypassPermissions")
_CLAUDE_BYPASS = ("--dangerously-skip-permissions", "--allow-dangerously-skip-permissions")
_ALLOW = ("--allowedTools", "--allowed-tools")
_DENY = ("--disallowedTools", "--disallowed-tools")
_VARIADIC = frozenset((*_ALLOW, *_DENY, "--add-dir"))      # flags that take values up to the next option
_SINGLE = frozenset(("--permission-mode",))


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
    granted: str | None         # the access the session really has (None: a CLI this module does not know)
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


def claude_native(template: list[str]) -> str:
    from .providers.claude import bypasses_permissions

    if bypasses_permissions(template):
        return "full"
    return "read-only" if runner_preflight.read_only(template) else "workspace"


def claude_read_only(template: list[str]) -> list[str]:
    """Any Claude runner narrowed to read-only, the way the shipped triage runner is written: `dontAsk`, only the
    read tools inside the run directory, local read-only git and the board's tools allowed, and Edit, Write,
    NotebookEdit, git's --output, the token directory and ~/.ssh denied beside the runner's own deny rules. Its
    permission mode, allow list, bypass flags and extra directories go. The board tools the runner names are kept as
    named (a runner that allows only some of them is not widened to all). With an `--mcp-config` of its own it
    also gets `--strict-mcp-config`, and is then a runner `runner_preflight.read_only` recognises."""
    if any(a.split("=", 1)[0] == "--settings" for a in template):
        raise ValueError(SETTINGS_REFUSED)
    kept, values = _without(template, {"--permission-mode", "--add-dir", *_ALLOW, *_DENY, *_CLAUDE_BYPASS})
    board = [t for flag in _ALLOW for v in values.get(flag, []) for t in runner_preflight.split_tools(v)
             if t == "mcp__agent-comms" or t.startswith("mcp__agent-comms__")]
    allowed = list(dict.fromkeys(board or ["mcp__agent-comms"])) + list(READ_ONLY_ALLOWED[1:])
    denied = list(READ_ONLY_DENIED)
    for flag in _DENY:
        for v in values.get(flag, []):
            denied += [t for t in runner_preflight.split_tools(v) if t not in denied]
    if any(a.split("=", 1)[0] == "--mcp-config" for a in kept) and "--strict-mcp-config" not in kept:
        kept.append("--strict-mcp-config")
    return kept + ["--permission-mode", "dontAsk", "--allowedTools=" + ",".join(allowed),
                   "--disallowedTools=" + ",".join(denied)]


def claude_full(template: list[str]) -> list[str]:
    """A Claude runner with permission prompts bypassed: its own permission mode replaced by `bypassPermissions`.
    Its deny rules stay (a denied tool stays unavailable in that mode, DESIGN_NOTES)."""
    kept, _ = _without(template, {"--permission-mode", *_CLAUDE_BYPASS})
    return kept + list(CLAUDE_FULL)


def _codex_safe(template: list[str]) -> tuple[list[str], bool]:
    """A Codex runner template with everything that turns the sandbox or approvals off taken out (a
    `danger-full-access` sandbox becomes `workspace-write`), and whether there was anything to take out. The adapter
    refuses such flags by name; a session that may run at `full` gets the sandbox from its access word instead."""
    from .providers.codex import BYPASS_FLAGS, VALUE_FLAGS, _unquoted

    out, was, i = list(template[:1]), False, 1
    while i < len(template):
        arg = template[i]
        i += 1
        if arg.split("=", 1)[0] in BYPASS_FLAGS:
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


def prepare(template: list[str], access: Any = None, *, allow_full: bool = False,
            cwd: str | None = None) -> Prepared:
    """The runner template for a session that is to run at `access` (None: as its runner has it), what the session
    really gets, and what its adapter must be told. ValueError: this machine cannot grant it (`full` without
    `allow_full`; a level this runner cannot be brought to). `cwd`: the session's run directory, which is the only
    thing a Codex runner's `--cd` may name.

    Without a word and without `allow_full` nothing is changed at all, which is how a session was opened before
    there were access levels: a runner that bypasses permission checks is then refused by its adapter, as ever."""
    access = level(access)
    kind = os.path.basename(template[0]) if template else ""
    if kind not in ("claude", "codex"):
        if access is not None:
            raise ValueError("this agent's CLI has no access levels")
        return Prepared(list(template), None, None, True)
    if kind == "claude":
        native = claude_native(template)
        safe, was_bypass = list(template), native == "full"
    else:
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
    if kind == "claude":
        if want == native:
            return Prepared(safe, native, "full" if native == "full" else None, True)
        if want == "full":
            return Prepared(claude_full(template), "full", "full", False)
        if want == "read-only":
            return Prepared(claude_read_only(template), "read-only", None, False)
        if native == "full":
            raise ValueError(WORKSPACE_FROM_BYPASS)
        return Prepared(safe, native, None, True)       # `workspace` asked of a read-only runner: as configured
    # Codex: the word becomes the thread's sandbox. `safe` no longer carries anything its adapter refuses.
    if want == "full":
        return Prepared(safe, "full", "full", was_bypass)
    if want == "read-only":
        return Prepared(safe, "read-only", None if native == "read-only" else "read-only", native == "read-only")
    if native == "full":
        return Prepared(safe, "workspace", "workspace", False)      # a runner that bypasses, brought down to it
    return Prepared(safe, native, None, True)               # as configured: `workspace-write`, or read-only
