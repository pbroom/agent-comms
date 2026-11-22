"""Adding and removing the wake hooks in a Codex hooks file (integrations/codex/install.sh --wake).

`python -m agent_comms.codex_hooks add --hooks-file FILE --script PATH --home DIR --agent NAME --token-file PATH`
`python -m agent_comms.codex_hooks remove --hooks-file FILE --script PATH`
`codex mcp get agent-comms --json | python -m agent_comms.codex_hooks check-mcp --board-home DIR --agent NAME`

FILE is `$CODEX_HOME/hooks.json` (Codex 0.171 reads hooks from that file beside config.toml, so config.toml is never
touched). The hooks are a `SessionStart` and a `Stop` command hook with `"async": true` running `command()`:
`AGENT_COMMS_HOME='<dir>' AGENT_COMMS_AGENT='<name>' bash '<checkout>/integrations/codex/wait.sh' '<token file>'`
(wait.sh runs `board wait --client codex --token-file '<token file>'`). A hook is ours only when it sits under
`SessionStart` or `Stop` and has exactly the keys `type` ("command"), `command` (of that form, or of the earlier one
without the token file), `timeout` and `async` (true).

*The token (review round 2, S3).* The token file is the one the board MCP server of this Codex home reads, and
`check-mcp` (run by the installer before `add`) prints it only after checking that server: the stdio server
install.sh sets up, the same board home, a token file it can resolve itself (AGENT_COMMS_CODEX_TOKEN_FILE given as
an absolute path in the server's env, else ~/.config/agent-comms/codex.token), and a token that signs in as the
hooks' agent. A token, or a token file, the server would take from the environment Codex starts in (`env_vars`), and
a token written into its env, are refused: the waiter could not use the same source. The waiter then reads exactly
that file and nothing from its environment, so the wakes it is charged for and the board tools of the thread act as
one agent. The token itself is never printed or written anywhere.

*Positions (S5).* Codex keys its trust in a hook by the file, the event, the group index and the handler index, and by
a hash of its definition (config.toml `[hooks.state."<key>"] trusted_hash`, written when the human trusts it). So no
other handler's (event, group, handler) ever changes:
- Our handler always lives in a group with nothing else in it. `add` writes it into the group that holds it already
  (in place: a re-run with the same command changes nothing and keeps the trust; a changed command is replaced there,
  and Codex asks to trust it again); else into the event's last group when that group is exactly `{"hooks": []}` (as
  an earlier `remove` leaves it); else into a new group appended at the end. A second copy of ours, alone in another
  group, is emptied in place.
- `remove` empties our group and leaves it where it is (also the event list and the `hooks` object): an empty group
  stays in the file; Codex runs nothing for it and the next `add` reuses it when it is last.
- A group that holds ours together with someone else's handlers (an older install, or an edit by hand) is refused by
  `add`, and by `remove` unless ours come after every other handler there (taking those out moves nothing): the
  message says to move ours out by hand.
Every other hook, key and value is kept; the file is written as JSON with two-space indentation, so a file written
another way changes its layout (not its content) the first time this edits it. The path is resolved once (a
symlinked file is written through to its target) and refused when its target is not a `.json` file or is the Codex
config.toml beside it (S8); the file is written atomically with its mode kept (600 when new), and refused when it is
not a JSON object. When nothing changes the file is not written at all.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from pathlib import Path

from .claude_hooks import NAME_RE

EVENTS = ("SessionStart", "Stop")
TIMEOUT = 86400     # seconds; `board wait` gives up 60 s before it (waiter.DEFAULT_MAX_SECONDS)
KEYS = frozenset({"type", "command", "timeout", "async"})


def command(home: str, agent: str, script: str, token_file: str) -> str:
    if "'" in home + script + token_file or not NAME_RE.fullmatch(agent) or not os.path.isabs(token_file):
        raise ValueError("paths with a single quote, a token file that is not absolute, or a name that is not an "
                         "agent name, are not supported")
    return f"AGENT_COMMS_HOME='{home}' AGENT_COMMS_AGENT='{agent}' bash '{script}' '{token_file}'"


def hook_for(cmd: str, timeout: int = TIMEOUT) -> dict:
    return {"type": "command", "command": cmd, "timeout": timeout, "async": True}


def _ours(hook, script: str) -> bool:
    if not (isinstance(hook, dict) and set(hook) == KEYS and hook["type"] == "command" and hook["async"] is True
            and isinstance(hook["command"], str) and type(hook["timeout"]) is int):
        return False
    shape = rf"AGENT_COMMS_HOME='[^']*' AGENT_COMMS_AGENT='{NAME_RE.pattern}' bash '{re.escape(script)}'( '/[^']*')?"
    return re.fullmatch(shape, hook["command"]) is not None


def _lists(data: dict) -> dict:
    hooks = data.get("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError("the hooks file's \"hooks\" is not an object")
    for event in EVENTS:
        groups = hooks.get(event, [])
        if not isinstance(groups, list) or not all(isinstance(g, dict) for g in groups):
            raise ValueError(f"the hooks file's hooks.{event} is not a list of groups")
        for g in groups:
            if "hooks" in g and not isinstance(g["hooks"], list):
                raise ValueError(f"a group under hooks.{event} has a \"hooks\" that is not a list")
    return hooks


def _shared(event: str, gi: int) -> ValueError:
    return ValueError(f"an agent-comms wake hook shares hooks.{event}[{gi}] with other hooks; changing it would move "
                      f"them, and Codex would ask to trust them again. Move it into a group of its own, or out, by "
                      f"hand")


def strip(data: dict, script: str) -> dict:
    """Our handlers out; every group, event list and the `hooks` object stay where they are, and no other handler
    moves (S5)."""
    hooks = _lists(data)
    for event in EVENTS:
        for gi, g in enumerate(hooks.get(event) or []):
            inner = g.get("hooks") or []
            mine = [i for i, h in enumerate(inner) if _ours(h, script)]
            if not mine:
                continue
            others = [i for i, h in enumerate(inner) if not _ours(h, script)]
            if others and min(mine) < max(others):
                raise _shared(event, gi)
            g["hooks"] = [h for h in inner if not _ours(h, script)]
    return data


def add(data: dict, script: str, cmd: str, timeout: int = TIMEOUT) -> dict:
    if not _ours(hook_for(cmd, timeout), script):
        raise ValueError("the command must run the script exactly as command() writes it")
    hooks = _lists(data)
    want = hook_for(cmd, timeout)
    for event in EVENTS:
        groups = hooks.setdefault(event, [])
        holding = []
        for gi, g in enumerate(groups):
            inner = g.get("hooks") or []
            if any(_ours(h, script) for h in inner):
                if not all(_ours(h, script) for h in inner):
                    raise _shared(event, gi)
                holding.append(g)
        if holding:
            holding[0]["hooks"] = [want]            # in place, alone in its group
            for g in holding[1:]:
                g["hooks"] = []                     # another copy of ours: emptied where it is
        elif groups and groups[-1] == {"hooks": []}:
            groups[-1]["hooks"] = [want]            # the empty group an earlier remove left at the end
        else:
            groups.append({"hooks": [want]})
    data["hooks"] = hooks
    return data


def _read(path: Path) -> dict:
    if not path.exists():
        return {}
    data = json.loads(path.read_text() or "{}")
    if not isinstance(data, dict):
        raise ValueError(f"{path} is not a JSON object")
    return data


def _write(path: Path, data: dict) -> None:
    """Replace the file atomically: the temporary file goes beside `path` (the real path `main` resolved once,
    before reading) and replaces it, keeping its permissions."""
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o600
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".agent-comms-")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(data, fh, indent=2)
            fh.write("\n")
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        os.unlink(tmp)
        raise


# Every AGENT_COMMS_* setting the board MCP server (integrations/codex/stdio.sh, stdio.py, `board mcp`) or the `board`
# code it runs reads, and what the installer does when the server's configuration sets it (review round 4, S3; the
# table is in DESIGN_NOTES "Waking a Codex thread you opened: review round 4"). A setting that can change which
# board, hub, agent or token the server uses is never taken from the environment Codex starts in (`env_vars`), and a
# value written into the server's `env` must be the one the hooks will use. Only the names in HARMLESS may be
# forwarded; any other AGENT_COMMS_* name, also one added later, is refused until it is classified here.
HARMLESS = frozenset({
    "AGENT_COMMS_MANAGED",          # child_env's "a process the board launched": the waiter's "never wait" flag, and
                                    # spawn caller resolution reads it (hosted_spawn.Ident.managed: refused without a
                                    # binding); it only narrows, never names a board, hub, agent or token
    "AGENT_COMMS_CHANNEL",          # turns the MCP channel notifications on
    "AGENT_COMMS_ALLOW_MIGRATE",    # lets a checkout migrate its own local database; no board, hub or identity
    "AGENT_COMMS_WAIT_TRACE",       # board wait's trace file
})
VERIFIED = frozenset({              # allowed as a literal `env` value, checked against what the hooks will use
    "AGENT_COMMS_HOME",             # the board: must be the hooks' board home
    "AGENT_COMMS_HUB",              # the hub: must be the hub the board's settings name
    "AGENT_COMMS_CODEX_TOKEN_FILE",  # the token file: must be absolute; it becomes the hooks' token file
    "AGENT_COMMS_TOKEN_DIR",        # where agent token files live: must be the installer's token directory
    "AGENT_COMMS_AGENT",            # an agent name: must be the hooks' agent
})
# Refused in any form: a token in the configuration or environment (the hooks cannot share it), the human's token
# file, and every name not listed in HARMLESS or VERIFIED. REFUSED names the known ones (a test checks that every
# AGENT_COMMS_* name the code reads is in one of the three sets).
REFUSED = frozenset({"AGENT_COMMS_TOKEN", "AGENT_COMMS_CODEX_TOKEN", "AGENT_COMMS_TOKEN_FILE",
                     # a hosted session's binding (hosted_spawn: the node sets it for a hosted Codex session through
                     # `-c mcp_servers.agent-comms.env_vars=[...]`, never a user's configuration): an interactive
                     # thread's board tools must not carry one
                     "AGENT_COMMS_HOSTED_SESSION", "AGENT_COMMS_HOSTED_KEY"})
# Names outside agent-comms the server's `env` or `env_vars` may name (PR review F2: refused until classified, as for
# AGENT_COMMS_*). Anything else is refused: BASH_ENV (bash sources it before stdio.sh, and it can export any
# AGENT_COMMS_* value), PYTHONPATH (another agent_comms package), UV_PROJECT_ENVIRONMENT, and the rest.
OTHER_NAMES = frozenset({
    "PATH",     # how `bash` and `uv` are found; Codex passes the user's PATH to MCP servers by default anyway
    "HOME",     # checked below: a literal HOME must be the installer's (absolute)
})
STDIO = Path(__file__).resolve().parents[1] / "integrations" / "codex" / "stdio.sh"   # this install's server


def env_var_names(entries) -> list[str] | None:
    """The names an MCP server's `env_vars` forwards, or None when an entry has a shape this cannot read. Codex 0.171
    (codex_config McpServerEnvVar, as config/edit/document_helpers.rs writes it) accepts a bare name ("NAME") and a
    table {name = "NAME", source = "..."}; both forward the variable NAME."""
    if entries is None:
        return []
    if not isinstance(entries, list):
        return None
    names = []
    for e in entries:
        if isinstance(e, str):
            names.append(e)
        elif isinstance(e, dict) and isinstance(e.get("name"), str) and set(e) <= {"name", "source"}:
            names.append(e["name"])
        else:
            return None
    return names


def _same_file(a: str, b: Path) -> bool:
    """Both name one existing file (device and inode), whatever the spelling."""
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


def _same_dir(a: str, b: Path) -> bool:
    try:
        sa, sb = os.stat(os.path.expanduser(a)), os.stat(b)
    except OSError:
        return False
    return (sa.st_dev, sa.st_ino) == (sb.st_dev, sb.st_ino)


def check_mcp(server: dict, board_home: str, agent: str, home: str) -> tuple[str | None, str | None]:
    """(the token file, None) when the board MCP server this Codex home runs (`codex mcp get agent-comms --json`)
    uses the same board, hub and agent as the wake hooks would, from a token file that signs in as `agent`; else
    (None, why not). Only reads, and never puts a value read from the server's configuration or a response in a
    message beyond the board path (review round 1, S3; rounds 2 to 4, S3 and S9)."""
    from .config import token_dir
    from .token_check import check
    transport = server.get("transport") if isinstance(server, dict) else None
    if not isinstance(transport, dict) or transport.get("type") != "stdio":
        return None, "the agent-comms MCP server of this Codex home is not the stdio one install.sh sets up; its " \
                     "board and agent cannot be checked"
    # What Codex launches (review round 5, S3): exactly the entry install.sh adds, `bash <absolute
    # .../integrations/codex/stdio.sh>`. A relative script path, or a relative `cwd`, is resolved by Codex at launch
    # (from the server's cwd, else from wherever Codex runs), so a different wrapper, with other defaults for the
    # board or the token, could run; another command (`board mcp --agent ...`, a script of someone's own) reads its
    # token from places this check does not model. stdio.sh itself finds its checkout from its own path, so an
    # absolute `cwd` changes nothing it resolves; with the board home and token file pinned below as absolute
    # paths, nothing it reads depends on the directory it starts in.
    command, args, cwd = transport.get("command"), transport.get("args"), transport.get("cwd")
    if command not in ("bash", "/bin/bash") or not isinstance(args, list) or len(args) != 1 \
            or not isinstance(args[0], str) or not os.path.isabs(args[0]) or not _same_file(args[0], STDIO):
        return None, "the agent-comms MCP server of this Codex home is not `bash <this install's " \
                     "integrations/codex/stdio.sh>` as install.sh sets it up; what it would run cannot be checked"
    if cwd is not None and (not isinstance(cwd, str) or not os.path.isabs(cwd)):
        return None, "the agent-comms MCP server's cwd is not an absolute path"
    env = transport.get("env") if transport.get("env") is not None else {}
    if not isinstance(env, dict) or not all(isinstance(k, str) for k in env):
        return None, "the agent-comms MCP server's env cannot be read"
    forwarded = env_var_names(transport.get("env_vars"))
    if forwarded is None:
        return None, "the agent-comms MCP server's env_vars has an entry of a form this installer does not know"
    for name in forwarded:
        if not name.startswith("AGENT_COMMS_") and name not in OTHER_NAMES:
            return None, f"the agent-comms MCP server takes {name} from the environment Codex starts in; only " \
                         f"settings this installer knows to be harmless may be"
        if name.startswith("AGENT_COMMS_") and name not in HARMLESS:
            return None, f"the agent-comms MCP server takes {name} from the environment Codex starts in; that can " \
                         f"change which board, hub, agent or token it uses, so the hooks are not added"
    if "HOME" in env and (not isinstance(env["HOME"], str) or not os.path.isabs(env["HOME"])
                          or os.path.realpath(env["HOME"]) != os.path.realpath(home)):
        return None, "the agent-comms MCP server runs with another HOME than this installer (or one that is not " \
                     "an absolute path)"
    for name, value in env.items():
        if not name.startswith("AGENT_COMMS_") and name not in OTHER_NAMES:
            return None, f"the agent-comms MCP server's configuration sets {name}, which this installer cannot check"
        if not name.startswith("AGENT_COMMS_") or name in HARMLESS:
            continue
        if name in ("AGENT_COMMS_CODEX_TOKEN", "AGENT_COMMS_TOKEN"):
            return None, "the agent-comms MCP server has its token written into its configuration; give it a token " \
                         "file (AGENT_COMMS_CODEX_TOKEN_FILE, or the default) so the hooks can use the same one"
        if name not in VERIFIED:
            return None, f"the agent-comms MCP server's configuration sets {name}, which this installer cannot check"
        if not isinstance(value, str):
            return None, f"the agent-comms MCP server's {name} is not a string"
        if name == "AGENT_COMMS_AGENT" and value != agent:
            return None, f"the agent-comms MCP server is set to act as another agent than '{agent}'"
        if name in ("AGENT_COMMS_HOME", "AGENT_COMMS_TOKEN_DIR", "AGENT_COMMS_CODEX_TOKEN_FILE") \
                and not os.path.isabs(value):
            # Resolved by the server from the directory Codex starts it in, and by `~` against its HOME: not
            # necessarily what this installer would resolve (round 5).
            return None, f"the agent-comms MCP server's {name} is not an absolute path; the hooks cannot be sure " \
                         f"to use the same one"
        if name == "AGENT_COMMS_TOKEN_DIR" and not _same_dir(value, token_dir()):
            return None, "the agent-comms MCP server uses another token directory than this installer"
    server_home = env.get("AGENT_COMMS_HOME") or os.path.join(home, "agent-comms")
    if not isinstance(server_home, str) or os.path.realpath(server_home) != os.path.realpath(board_home):
        return None, f"the agent-comms MCP server uses another board than {board_home}"
    named = env.get("AGENT_COMMS_CODEX_TOKEN_FILE")
    # The hub (review round 3, S3): the waiter uses the board's own settings (board.toml and board.local.toml of
    # this board home, never an AGENT_COMMS_HUB it inherits), so the server must reach the same one.
    from . import remote
    from .config import Settings
    try:
        settings = Settings.load(Path(board_home) / "board.toml")
        board_hub = remote.hub_url(settings, {})
        server_hub = remote.hub_url(settings, {"AGENT_COMMS_HUB": env["AGENT_COMMS_HUB"]}) \
            if isinstance(env.get("AGENT_COMMS_HUB"), str) and env["AGENT_COMMS_HUB"] else board_hub
    except (OSError, ValueError):
        return None, "the board's hub setting, or the agent-comms MCP server's, is not a valid hub URL"
    if server_hub != board_hub:
        return None, "the agent-comms MCP server is set to reach another hub than this board's settings name; " \
                     "the hooks would wait on one hub while the thread's board tools use another"
    path = Path(named if named else os.path.join(home, ".config", "agent-comms", "codex.token"))
    why = check(agent, token_file=path, environ={})
    if why:
        return None, f"the agent-comms MCP server's token file does not sign in as '{agent}' on this board ({why}); " \
                     f"a wake would be charged to '{agent}' in a thread whose board tools act as someone else"
    return os.path.realpath(path), None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="codex_hooks")
    ap.add_argument("action", choices=("add", "remove", "check-mcp"))
    ap.add_argument("--board-home", help="(check-mcp) the board home the hooks would use")
    ap.add_argument("--hooks-file", help="$CODEX_HOME/hooks.json (add, remove)")
    ap.add_argument("--script", help="the hook script's path (add, remove)")
    ap.add_argument("--home", help="the board home the hook names (add)")
    ap.add_argument("--agent", help="the agent the hook waits as (add, check-mcp)")
    ap.add_argument("--token-file", help="(add) the token file check-mcp printed")
    ap.add_argument("--timeout", type=int, default=TIMEOUT)
    a = ap.parse_args(argv)
    if a.action == "check-mcp":         # `codex mcp get agent-comms --json` piped in (never in argv or a variable)
        try:
            server = json.loads(sys.stdin.read() or "null")
        except ValueError:
            server = None
        token_file, why = check_mcp(server, a.board_home or "", a.agent or "", os.path.expanduser("~")) \
            if isinstance(server, dict) else \
            (None, "no agent-comms MCP server is configured for this Codex home (run install.sh first)")
        if why:
            print(f"Not changed: {why}", file=sys.stderr)
            return 1
        print(token_file)
        return 0
    if not a.hooks_file or not a.script:
        ap.error("add and remove need --hooks-file and --script")
    path = Path(os.path.realpath(a.hooks_file))     # once: read and replace the same file
    try:
        if path.suffix != ".json" or path == Path(os.path.realpath(Path(a.hooks_file).parent / "config.toml")):
            raise ValueError(f"{a.hooks_file} resolves to {path}, which is not a hooks .json file")
        before = _read(path)
        if a.action == "add":
            if not a.home or not a.agent or not a.token_file:
                raise ValueError("add needs --home, --agent and --token-file")
            cmd = command(a.home, a.agent, a.script, a.token_file)
            after = add(json.loads(json.dumps(before)), a.script, cmd, a.timeout)
        else:
            after = strip(json.loads(json.dumps(before)), a.script)
    except ValueError as e:
        print(f"Not changed: {e}", file=sys.stderr)
        return 1
    if after != before:
        _write(path, after)
        print("changed")
    else:
        print("unchanged")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
