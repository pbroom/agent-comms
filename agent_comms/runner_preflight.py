"""The scoped and read-only tool sets for dispatched Claude runs."""
from __future__ import annotations


# Opted-in repository implementation. No arbitrary Bash, git reset/clean/push,
# gh API/merge/delete, shell, or permission bypass. Host/managed denies still win.
ALLOWED = (
    "mcp__agent-comms", "Edit(./**)", "Write(./**)",
    "Bash(git status *)", "Bash(git diff *)", "Bash(git log *)", "Bash(git show *)",
    "Bash(git rev-parse *)", "Bash(git add *)", "Bash(git commit *)",
    "Bash(git fetch origin)", "Bash(git fetch origin main)",
    "Bash(git worktree add -b codex/*)", "Bash(git switch -c codex/*)",
    "Bash(uv run pytest *)", "Bash(pytest *)", "Bash(gh --version)",
    "Bash(gh pr view)", "Bash(gh pr view *)", "Bash(gh pr list)", "Bash(gh pr list *)", "Bash(gh pr diff *)", "Bash(gh pr checks *)", "Bash(gh run view *)",
)
DENIED = ("Bash(git *--force*)", "Bash(git *--discard-changes*)", "Bash(git * -f*)")
# What a scoped agent does when a command it needs is refused. A dispatched run was launched for board request
# posts, so it blocks them. A hosted session's turn may be either: a human's message or a child's task (no post to
# block), or, for a root woken by a board post (hosted_wake.py), request posts addressed to its agent, which the
# session holds once it has been shown them (hosted_wake.holds: no relaunch, no undelivered alert), so it must block
# those or they never reach the human.
DENIED_TOOL_INSTRUCTION = (
    "If a tool you need is denied, do not work around it: mark the request blocked (board_request_progress "
    "state=blocked) naming the denied tool, and stop.")
HOSTED_DENIED_TOOL_INSTRUCTION = (
    "If a command you need is refused, do not work around it: if you are working a board request, mark it blocked "
    "(board_request_progress state=blocked) naming the command; otherwise say in your reply which one. Then stop.")


# ---------------------------------------------------------------- the notice of what a scoped agent may run
#
# Built on the node from the scoped command line, never from a task or the board. It always lists the shipped forms,
# read from the allow rules this code writes (ALLOWED), so the text and the rules cannot drift; an allow rule of a
# shape it has no words for raises (a change to this file, caught by tests/test_scoped_notice.py). It does not model
# how a runner's own restrictions interact with that list (each attempt met another interaction it got wrong): one
# coarse test, own_restrictions(), erring toward yes, and when it says yes the same list comes with fixed words that
# any form may be refused and nothing is stated as available. The test is an allowlist, not a list of restricting
# options (such a list is always one hidden option behind the CLI): the plain text only for a command line whose every
# token is known not to take tools away. No text of the runner's own reaches it.
#
# Seen on Claude Code 2.1.322 (DESIGN_NOTES "Scoped sessions are told their commands"): a trailing ` *` matches the
# bare command; `Edit(path)` decides writes by both Edit and Write, and `Write(path)` grants nothing;
# `Bash(git *--force*)` refuses a commit whose message holds `--force`.

_GH_READS = frozenset(("--version", "view", "list", "diff", "checks"))   # gh verbs that only read
# The shipped DENIED rules, in fixed words (a test holds each rule's option against this line).
DENIED_WORDS = ("A git command line holding --force, --discard-changes or a word starting with -f anywhere, a commit "
                "message included, is refused.")
OPENING = "Use the forms below. Other commands may be refused without asking."
OWN_OPENING = ("This runner has restrictions of its own, so any of the forms below may also be refused. Other "
               "commands may be refused without asking.")
FILES = "You may edit and create files in your run directory."
OWN_FILES = "You may be able to edit and create files in your run directory."
# Options known not to take tools away, as the shipped runners (board.toml), scoped_template and the session host put
# them on a Claude command line, with how many values each takes (None: one or more, up to the next option). A value
# is consumed, never read. Anything else on a scoped command line counts as a restriction of the runner's own.
SAFE_OPTIONS = {
    "-p": 0, "--print": 0, "--verbose": 0, "--strict-mcp-config": 0,
    "--model": 1, "--effort": 1, "--append-system-prompt": 1, "--input-format": 1, "--output-format": 1,
    "--session-id": 1, "--resume": 1, "--permission-prompts": 1,
    "--system-prompt-snapshot": 1,      # the machine owner's documented option (DESIGN_NOTES, resumed sessions)
    "--mcp-config": None,
}


def _command_form(body: str) -> str:
    """A Bash rule's command as the agent should type it: `X *` is `X ...`, `p/*` is `p/<name> ...`."""
    if body.endswith(" *") and "*" not in body[:-2]:
        return body[:-2] + " ..."
    if body.endswith("/*") and "*" not in body[:-1]:
        return body[:-1] + "<name> ..."
    if "*" not in body:
        return body
    raise ValueError(f"no plain-words form for the allow rule Bash({body})")


def own_restrictions(template: list[str]) -> bool:
    """Whether a scoped command line (scoped_template) may carry anything of the runner's own that takes tools away.
    No only when every token is accounted for: the `claude` executable, the `{prompt}` placeholder, the allow and deny
    lists exactly as scoped_template writes them (the deny list holding DENIED alone), `--permission-mode dontAsk`, the
    host's own `--settings` value, and SAFE_OPTIONS with their values. Anything else is a yes: an option this code has
    never heard of, a known one in another spelling or with an unexpected value shape, a stray positional, a runner's
    own settings. A wrong yes only softens the words; a wrong no would promise a refused form."""
    import os
    from .session_access import NO_BYPASS_JSON
    exact = {"--allowedTools=" + ",".join(ALLOWED), "--disallowedTools=" + ",".join(DENIED)}
    if not template or os.path.basename(template[0]) != "claude":
        return True
    i = 1
    while i < len(template):
        arg = template[i]
        i += 1
        if arg in exact or arg == "{prompt}":
            continue
        following = template[i] if i < len(template) else None
        if (arg, following) in (("--permission-mode", "dontAsk"), ("--settings", NO_BYPASS_JSON)):
            i += 1
            continue
        flag, eq, _ = arg.partition("=")
        if flag not in SAFE_OPTIONS:
            return True
        n = SAFE_OPTIONS[flag]
        if eq:
            if n == 0:
                return True                 # a switch given a value: not a shape this code writes
            continue
        if n == 0:
            continue
        if following is None or following.startswith("-") or following == "{prompt}":
            return True                     # a value that may be another option: Claude may read it either way
        i += 1
        while n is None and i < len(template) and not template[i].startswith("-") and template[i] != "{prompt}":
            i += 1
    return False


def tools_notice(template: list[str]) -> str:
    """The command forms a scoped Claude agent is offered, in plain words, from its scoped command line."""
    allowed = [t for v in (_flag_values(template, {"--allowedTools", "--allowed-tools"}) or []) for t in split_tools(v)]
    denied = [t for v in (_flag_values(template, {"--disallowedTools", "--disallowed-tools"}) or [])
              for t in split_tools(v)]
    own = own_restrictions(template)
    out = [OWN_OPENING if own else OPENING]
    commands, files = [], False
    for t in allowed:
        if t == "Edit(./**)":
            files = True                    # Edit(path) decides writes by Edit and Write alike
        elif t.startswith("Bash(") and t.endswith(")"):
            commands.append(t[len("Bash("):-1])
        elif not (t in ("mcp__agent-comms", "Write(./**)") or t.startswith("mcp__agent-comms__")):
            raise ValueError(f"no plain-words form for the allow rule {t}")
    if files:
        out.append(OWN_FILES if own else FILES)
    git, tests, gh, rest = [], [], [], []
    for s in commands:
        if s + " *" in commands:
            continue                        # `X *` matches a bare `X` as well
        form = "`" + _command_form(s) + "`"
        (git if s.split(" ", 1)[0] == "git" else tests if "pytest" in s.split()
         else gh if s.split(" ", 1)[0] == "gh" else rest).append(form)
    parts = []
    if git:
        parts.append("git: " + ", ".join(git))
    if tests:
        parts.append("tests: " + ", ".join(tests) + " (not `python -m pytest`)")
    if gh:
        read_only = all(f.strip("`").removesuffix(" ...").split()[-1] in _GH_READS for f in gh)
        parts.append(("GitHub, read-only: " if read_only else "GitHub: ") + ", ".join(gh))
    if rest:
        parts.append("other: " + ", ".join(rest))
    if parts:
        out += ["Commands: " + "; ".join(parts) + ".", "`...` is any arguments, or none."]
        branches = [s for s in commands
                    if s.endswith("/*") and s.startswith(("git switch -c ", "git worktree add -b "))]
        if branches:
            prefixes = dict.fromkeys(s.split()[-1][:-1] for s in branches)
            example = next((s for s in branches if s.startswith("git switch -c ")), branches[0])[:-1] + "<name>"
            out.append("A new branch must be named " + " or ".join(f"`{p}<name>`" for p in prefixes)
                       + f": `{example}`.")
        if git and set(DENIED) <= set(denied):
            out.append(DENIED_WORDS)
        out.append("Run each command on its own, not chained to another or wrapped in one.")
    return " ".join(out)


def dispatched_instruction(template: list[str]) -> str:
    """What a dispatched scoped run's prompt ends with (dispatch.py, node.py): the notice, then block the request."""
    return tools_notice(template) + " " + DENIED_TOOL_INSTRUCTION


def with_denied_tool_instruction(template: list[str]) -> list[str]:
    """A scoped Claude runner for a hosted session (session_host.py): the notice of its commands and
    HOSTED_DENIED_TOOL_INSTRUCTION through `--append-system-prompt`, since a session has no launch prompt to carry
    them. A runner that already appends its own system prompt keeps it, with the notice after it (Claude Code takes
    the flag once). Nothing else changes: a runner's own `--system-prompt-snapshot` stays as written. (Claude Code
    records a conversation's system prompt at its first request and sends that record on every resume until it is
    compacted, so a session started before a change to this text keeps the old one until then; a new session is the
    remedy, and `--system-prompt-snapshot off` in the runner the machine owner's choice of freshness over cache reuse.
    DESIGN_NOTES "Scoped sessions are told their commands".)"""
    text = tools_notice(template) + " " + HOSTED_DENIED_TOOL_INSTRUCTION
    out, i, found = [], 0, False
    while i < len(template):
        arg = template[i]
        i += 1
        if arg == "--append-system-prompt" and i < len(template) and template[i] != "{prompt}":
            out += [arg, template[i] + "\n\n" + text]
            i, found = i + 1, True
        elif arg.startswith("--append-system-prompt="):
            out.append(arg + "\n\n" + text)
            found = True
        else:
            out.append(arg)
    return out if found else out + ["--append-system-prompt", text]


# A read-only Claude runner (the triage agent's): board tools, reading files inside its run directory and local
# read-only git, with Edit and Write explicitly denied. An opted-in project keeps it as configured (no scoped override, no
# denied-tool instruction): replacing its tools with ALLOWED would widen it to commits and test runs.
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


def scoped(template: list[str], project: str | None, claude_tool_projects: list[str]) -> bool:
    """Whether a run of this runner in this thread project gets the scoped tools (scoped_template): a Claude runner in
    an opted-in project ([dispatch] claude_tool_projects), unless it declares itself read-only (the scoped set would
    widen it)."""
    import os
    return (project in claude_tool_projects and os.path.basename(template[0]) == "claude"
            and not read_only(template))


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
