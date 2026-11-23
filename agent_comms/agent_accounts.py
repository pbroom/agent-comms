"""Agent accounts: two subscriptions of one CLI on one machine (DESIGN_NOTES "Agent accounts").

Claude Code keeps each config folder's sign-in separate (`CLAUDE_CONFIG_DIR`, default `~/.claude`), and Codex each
home's (`CODEX_HOME`, default `~/.codex`). The board gives each account an identity of its own (say `claude-work`
next to `claude-code`). Two settings make such identities work together through the board:

- `[dispatch.runner_homes]` (read on the machine that LAUNCHES: a node, or the hub's own dispatcher): runner key ->
  the account folder that runner's processes use. The variable is chosen by the runner's CLI (`claude` ->
  CLAUDE_CONFIG_DIR, `codex` -> CODEX_HOME), never named by config, and set by `dispatch.child_env`, which every
  start path uses (a dispatched run here or on a node, a hosted session, a child session). The folder is a literal
  absolute path to an existing directory of the current user's, written as its real path, and never `/`, the home
  folder, the agents' token folder or token files, the board's home or its data directory (or inside or around one
  of them); no directory a run can write may overlap it (DispatchConfig.launch_blocker, asked again at each start).
  A runner whose variable is also passed through `[dispatch.env]` is refused: one way only.
- `[dispatch.agent_projects]` (read on the HUB): agent name -> the projects it may run in, as the board names
  projects (exactly the project string the creating path stores and launches with, trailing slashes aside). An agent
  with an entry runs only there; an agent without one is unrestricted. Decided when work is CREATED, inside the
  write that creates it, and nowhere else (`refusal`): a dispatcher launch, a routing of a request to another agent,
  a hosted session and a child session (the human's or an agent's); capabilities and the spawn frame never offer an
  agent where it may not run. Work that already exists when a project is taken off the list proceeds, and an
  existing session is still woken (a wake delivers a post to a session that exists: hosted_wake.py and
  interactive_wake.py are not governed by the list); the human closes the session or stops the run to stop it. While the hub's latest settings did
  not load and the list is or may be in play (an entry in the last good settings, or `agent_projects` in the files on
  disk), every start is refused (`UNLOADED`): the last good table may be the one the owner is changing. Without
  either, a broken edit changes nothing here, as on main.

Nothing here reads anything inside an account folder, and nothing can tell which subscription a process really
used: the board sets the variable, the CLI decides what it does with it.
"""
from __future__ import annotations

import os
import stat
from typing import Any

from .config import NAME_RE

HOMES = "[dispatch.runner_homes]"
PROJECTS = "[dispatch.agent_projects]"
HOME_VARS = {"claude": "CLAUDE_CONFIG_DIR", "codex": "CODEX_HOME"}   # the runner's CLI (its argv[0]'s name) -> variable
MAX_PROJECTS = 200


def _control(value: str) -> bool:
    return any(ord(ch) < 32 or ord(ch) == 127 for ch in value)


# ---------------------------------------------------------------- runner homes (the launching machine)


def home_var(template: list[str] | None) -> str | None:
    """The variable that names the account folder of this runner's CLI, or None: a CLI the board knows no folder of."""
    return HOME_VARS.get(os.path.basename(template[0])) if template else None


def _overlaps(path: str, other: str) -> bool:
    from .dispatch import _within

    return _within(path, other) or _within(other, path)


def forbidden(settings: Any = None) -> tuple[list[str], list[str]]:
    """(directories, files) an account folder may neither be, be inside nor contain: the agents' token folder
    (wherever AGENT_COMMS_TOKEN_DIR puts it, and the default one), the folder of the human's token file (wherever
    AGENT_COMMS_TOKEN_FILE puts it), the board's home (with the default agents.toml), its data directory and SSH keys;
    and the files it may not contain: the human's token file as it RESOLVES (`board` reads it through a symlink, so
    the file it lands on, wherever that is) and a relocated agents.toml (`settings.agents_path`)."""
    from .config import home, human_token_file

    user = os.path.expanduser("~")
    token_file = str(human_token_file())
    dirs = [os.path.expanduser(os.environ.get("AGENT_COMMS_TOKEN_DIR", "~/.config/agent-comms")),
            os.path.join(user, ".config", "agent-comms"), os.path.dirname(token_file), str(home()),
            str(home() / "data"), os.path.join(user, ".ssh")]
    files: list[str] = [token_file]     # held where it RESOLVES: the check (`_within`) follows a symlink
    if settings is not None and getattr(settings, "db_path", None) is not None:
        dirs.append(str(settings.db_path.parent))
    if settings is not None and getattr(settings, "agents_path", None) is not None:
        files.append(str(settings.agents_path))
    return [os.path.abspath(d) for d in dirs], [os.path.abspath(f) for f in files]


def check_home(key: str, path: Any, var: str, protected: tuple[list[str], list[str]]) -> str:
    """`path` as runner `key`'s account folder, or ValueError naming the runner (never anything read from inside
    the folder). Asked when the settings are read and again before each start (the folder may have changed)."""
    bad = f"{HOMES} {key}"
    user = os.path.realpath(os.path.expanduser("~"))
    if (not isinstance(path, str) or not path or _control(path) or not os.path.isabs(path)
            or os.path.normpath(path) != path or path.startswith("//")):
        raise ValueError(f"{bad} must be a literal absolute, normalized folder path (no ~, $VARS, '.', '..' "
                         "or '//')")
    if path == "/" or os.path.realpath(path) == user:
        raise ValueError(f"{bad} may not be / or the home folder itself; name the account's own folder")
    try:
        st = os.stat(path)
    except OSError:
        raise ValueError(f"{bad} is not an existing folder (sign in once with that {var} first, or remove "
                         "the entry)") from None
    if not stat.S_ISDIR(st.st_mode):
        raise ValueError(f"{bad} is not a folder")
    if os.path.realpath(path) != path:
        raise ValueError(f"{bad} goes through a symlink; write its real path")
    if st.st_uid != os.getuid():
        raise ValueError(f"{bad} is not owned by the user this process runs as")
    dirs, files = protected
    if any(_overlaps(path, d) for d in dirs) or any(_within_path(f, path) for f in files):
        raise ValueError(f"{bad} is, is inside or contains the agents' token folder or a token file, the board's "
                         "home or its data directory, or SSH keys; pick the account's own folder")
    return path


def _within_path(path: str, root: str) -> bool:
    from .dispatch import _within

    return _within(path, root)


def codex_workspaces(argv: list[str]) -> list[str]:
    """Every directory a Codex command line names as its workspace (`--cd`/`-C`, in each spelling the Codex
    adapter reads: `--cd X`, `--cd=X`, `-C X`, `-CX`, `-C=X`; providers.codex.VALUE_FLAGS), as written: a relative one
    is the caller's to resolve against the process's cwd. `{project}` stays as it is (the caller renders it)."""
    from .providers.codex import VALUE_FLAGS

    out, i = [], 1
    while i < len(argv):
        arg = argv[i]
        i += 1
        if arg == "--":
            break       # options end here, as the Codex adapter reads them: what follows is the prompt
        if arg.startswith("--"):
            flag, eq, value = arg.partition("=")
        elif arg.startswith("-") and len(arg) >= 2:
            flag, eq, value = arg[:2], "=" if len(arg) > 2 else "", arg[2:].removeprefix("=")
        else:
            continue
        if VALUE_FLAGS.get(flag) is None:
            continue
        if not eq:
            if i >= len(argv):
                break
            value, i = argv[i], i + 1
        if VALUE_FLAGS[flag] == "cd" and value:
            out.append(value)
    return out


def validate_homes(table: Any, runners: dict[str, list[str]], env: dict[str, list[str]],
                   settings: Any = None) -> dict[str, str]:
    """`[dispatch.runner_homes]`: runner key (under [dispatch.runners]) -> account folder. Errors name the runner,
    never anything read from the folder."""
    if not isinstance(table, dict):
        raise ValueError(f"{HOMES} must be a table of runner = \"/absolute/folder\"")
    protected = forbidden(settings)
    out: dict[str, str] = {}
    for key, path in table.items():
        bad = f"{HOMES} {key}"
        if key not in runners:
            raise ValueError(f"{bad}: there is no such runner under [dispatch.runners]")
        var = home_var(runners[key])
        if var is None:
            raise ValueError(f"{bad}: only a claude or codex runner has an account folder (its CLI is "
                             f"{os.path.basename(runners[key][0])!r})")
        if var in env.get(key, []):
            raise ValueError(f"{bad}: [dispatch.env] {key} also passes {var}; set the account folder one way only")
        out[key] = check_home(key, path, var, protected)
    return out


# ---------------------------------------------------------------- the opt-in list (the hub)


def validate_projects(table: Any) -> dict[str, list[str]]:
    """`[dispatch.agent_projects]`: agent name -> absolute project paths (as the hub's threads name them; they need
    not exist on the hub). An empty list is allowed and means nowhere."""
    if not isinstance(table, dict):
        raise ValueError(f"{PROJECTS} must be a table of agent = [\"/absolute/project\", ...]")
    out: dict[str, list[str]] = {}
    for agent, projects in table.items():
        if not isinstance(agent, str) or not NAME_RE.match(agent):
            raise ValueError(f"{PROJECTS} keys must be agent names")
        if (not isinstance(projects, list) or len(projects) > MAX_PROJECTS
                or not all(isinstance(p, str) for p in projects)):
            raise ValueError(f"{PROJECTS} {agent} must be a list of at most {MAX_PROJECTS} absolute project paths")
        clean: list[str] = []
        for p in projects:
            q = project_id(p)
            if (q is None or not q.startswith("/") or _control(q) or len(q) > 1024 or os.path.normpath(q) != q
                    or q.startswith("//")):
                raise ValueError(f"{PROJECTS} {agent}: each project must be an absolute, normalized path, as the "
                                 "board shows it (board threads, the dashboard)")
            if q not in clean:
                clean.append(q)
        out[agent] = clean
    return out


def project_id(project: Any) -> str | None:
    """A project as the board names it: the very string the creating path stores and launches with (a thread's
    project, a hosted session's, a child's, which is its parent's), with trailing slashes off as those paths take them
    off, and nothing else changed (no space trimmed, no case folded, no path resolved). The list's entries are
    normalized the same way and no further, so that only that string passes. Nothing on any disk is read: the hub
    often has no checkout of the project at all. A project spelled otherwise is another project."""
    if not isinstance(project, str) or not project:
        return None
    return project.rstrip("/") or "/"


def refusal(table: dict[str, list[str]], agent: str, project: Any) -> str | None:
    """Why `agent` may not run in `project` under this opt-in table, or None (no entry: unrestricted)."""
    if agent not in table:
        return None
    q = project_id(project)
    if q is not None and q in table[agent]:
        return None
    return not_opted_in(agent, project)


def not_opted_in(agent: str, project: Any) -> str:
    return (f"{agent} is not opted in to {project}: the hub's [dispatch.agent_projects] lists the projects {agent} "
            "may run in")


MENTION = "agent_projects"
MENTION_READ_MAX = 4 * 1024 * 1024     # bytes of one settings file searched for MENTION; a longer file counts as one


def mentioned_on_disk(board: Any) -> bool:
    """The settings files the loader reads (board.toml and board.local.toml beside it), as they are on disk now, may
    set the list: a file that parses as TOML has a `[dispatch]` table with the key MENTION (decided from the parsed
    keys, so a quoted or escaped spelling of it counts), or a `dispatch` that is not a table; one that does not parse
    contains the literal MENTION (a plain byte search, the fallback). At most MENTION_READ_MAX bytes of each are
    read; a file that cannot be read, a longer one, or a Board with no settings files counts as setting it. An
    absent board.local.toml does not (the loader takes it as empty)."""
    import tomllib
    from .config import LOCAL_SETTINGS

    path = getattr(board.s, "config_path", None)
    if path is None:
        return True
    for f, optional in ((path, False), (path.with_name(LOCAL_SETTINGS), True)):
        try:
            with open(f, "rb") as fh:
                data = fh.read(MENTION_READ_MAX + 1)
        except FileNotFoundError:
            if optional:
                continue
            return True
        except OSError:
            return True
        if len(data) > MENTION_READ_MAX:
            return True
        try:
            parsed = tomllib.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, tomllib.TOMLDecodeError):
            if MENTION.encode() in data:
                return True
            continue
        d = parsed.get("dispatch", {})
        if not isinstance(d, dict) or MENTION in d:
            return True
    return False


def _snapshot(board: Any) -> tuple[Any, Any]:
    """(the reload status, the [dispatch] table) as ONE consistent reading: under the lock `Board.reload_settings`
    holds for its whole update, so a decision never sees a cleared error beside the table it replaces (or the
    reverse). A Board without that lock (one built in code) is read as it is."""
    import contextlib

    lock = getattr(board, "_settings_lock", None)
    with lock if lock is not None else contextlib.nullcontext():
        return getattr(board, "settings_error", None), getattr(board.s, "dispatch", None)


def hub_table(board: Any, snapshot: tuple[Any, Any] | None = None) -> dict[str, list[str]] | None:
    """The hub's `[dispatch.agent_projects]` as its settings have it now, or None when it cannot be read (the caller
    then refuses: fail closed). While the latest settings on disk did not load (`Board.settings_error`), the Board keeps
    the last good settings, whose table may be the very one the owner is changing: then None (nothing is started
    until a valid table loads) whenever the list is or may be in play, that is the last good settings have an entry
    in it or the files on disk mention it (`mentioned_on_disk`). Otherwise the last good settings stand, as on main."""
    error, d = snapshot if snapshot is not None else _snapshot(board)
    try:
        d = d or {}
        table = validate_projects(d.get("agent_projects", {})) if isinstance(d, dict) else None
    except (ValueError, AttributeError):
        return None
    if error is not None and (table is None or table or mentioned_on_disk(board)):
        return None
    return table


UNREADABLE = ("the hub's [dispatch.agent_projects] cannot be read, so no agent is started anywhere until it is "
              "fixed")
UNLOADED = ("the hub's settings did not load: fix board.toml/board.local.toml. Until they load, no agent is started "
            "anywhere (the [dispatch.agent_projects] in force may be the one being changed)")


def hub_refusal(board: Any, agent: str, project: Any) -> str | None:
    """Why the hub may not start `agent` in `project` now, or None."""
    snapshot = _snapshot(board)         # read once: the table and the reload status together
    table = hub_table(board, snapshot)
    if table is None:
        return UNLOADED if snapshot[0] is not None else UNREADABLE
    return refusal(table, agent, project)


def check(board: Any, agent: str, project: Any) -> None:
    """Raise Conflict with the reason when the hub may not start `agent` in `project`."""
    why = hub_refusal(board, agent, project)
    if why:
        from .core import Conflict
        raise Conflict(why)
