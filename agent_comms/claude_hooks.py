"""Adding and removing the wake hooks in a Claude Code settings file (integrations/claude-code/install.sh --wake).

`python -m agent_comms.claude_hooks add --settings FILE --script PATH --home DIR --agent NAME [--timeout S]`
`python -m agent_comms.claude_hooks remove --settings FILE --script PATH`

The hooks are a `Stop` and a `SessionStart` command hook with `"asyncRewake": true` running `command()`; the
SessionStart one also has `"async": true` (`hook_for`). A hook is ours only when it sits under `Stop` or
`SessionStart` and is exactly one of the shapes this module writes or wrote (`SHAPES`): the keys `type` ("command"),
`command` (exactly `AGENT_COMMS_HOME='<dir>' AGENT_COMMS_AGENT='<name>' bash '<this script>'`), `asyncRewake` (true),
`timeout`, and possibly `async` (true). `add` replaces ours (idempotent, and a new agent or board home takes effect); `remove` takes out ours
and nothing else, and drops only a group, event list or `hooks` object that taking ours out left empty (a group with
anything beside `hooks`, such as a matcher, is kept). Every other setting and hook is kept as it was, also one whose
command merely mentions the script. The file is written atomically, with its mode kept (600 when new); a file that
is not a JSON object is refused and left alone.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from pathlib import Path

EVENTS = ("SessionStart", "Stop")
TIMEOUT = 86400     # seconds; `board wait` gives up 60 s before it (waiter.DEFAULT_MAX_SECONDS)
NAME_RE = re.compile(r"[a-z][a-z0-9_-]{0,31}")


def command(home: str, agent: str, script: str) -> str:
    if "'" in home + script or not NAME_RE.fullmatch(agent):
        raise ValueError("paths with a single quote, or a name that is not an agent name, are not supported")
    return f"AGENT_COMMS_HOME='{home}' AGENT_COMMS_AGENT='{agent}' bash '{script}'"


# The keys of a hook this module writes: SessionStart's now (with "async"), and the earlier shape, which every event
# had before and Stop still has. Both are recognised, so that --remove-wake and a re-run of --wake clean up an entry an
# earlier version wrote.
SHAPES = ({"type", "command", "asyncRewake", "timeout"}, {"type", "command", "async", "asyncRewake", "timeout"})


def hook_for(event: str, cmd: str, timeout: int = TIMEOUT) -> dict:
    """The hook entry for one event. SessionStart carries "async": true beside "asyncRewake": true: two readings of
    the Claude Code 2.1.322 build say a SessionStart hook with "asyncRewake" alone can be run synchronously in a
    stream-json session (its exit 2 then wakes nothing), and that an explicit "async" takes the background path
    (DESIGN_NOTES "Known Claude Code behaviour"). Stop is left as it was."""
    if event == "SessionStart":
        return {"type": "command", "command": cmd, "async": True, "asyncRewake": True, "timeout": timeout}
    return {"type": "command", "command": cmd, "asyncRewake": True, "timeout": timeout}


def _ours(hook, script: str) -> bool:
    if not (isinstance(hook, dict) and set(hook) in SHAPES and hook["type"] == "command"
            and hook["asyncRewake"] is True and hook.get("async", True) is True
            and isinstance(hook["command"], str) and type(hook["timeout"]) is int):
        return False
    shape = rf"AGENT_COMMS_HOME='[^']*' AGENT_COMMS_AGENT='{NAME_RE.pattern}' bash '{re.escape(script)}'"
    return re.fullmatch(shape, hook["command"]) is not None


def strip(settings: dict, script: str) -> dict:
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return settings
    took_any = False
    for event in EVENTS:
        groups = hooks.get(event)
        if not isinstance(groups, list):
            continue
        kept, took = [], False
        for g in groups:
            inner = g.get("hooks") if isinstance(g, dict) else None
            if not isinstance(inner, list) or not any(_ours(h, script) for h in inner):
                kept.append(g)
                continue
            took = True
            rest = [h for h in inner if not _ours(h, script)]
            if rest or set(g) != {"hooks"}:
                kept.append(g | {"hooks": rest})
        if took and not kept:
            del hooks[event]
        elif took:
            hooks[event] = kept
        took_any = took_any or took
    if took_any and not hooks:
        del settings["hooks"]
    return settings


def add(settings: dict, script: str, cmd: str, timeout: int = TIMEOUT) -> dict:
    hooks = settings.get("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError("the settings file's \"hooks\" is not an object")
    for event in EVENTS:
        if not isinstance(hooks.get(event, []), list):
            raise ValueError(f"the settings file's hooks.{event} is not a list")
    if not _ours(hook_for("Stop", cmd, timeout), script):
        raise ValueError("the command must run the script exactly as command() writes it")
    settings = strip(settings, script)
    hooks = settings.setdefault("hooks", {})
    for event in EVENTS:
        hooks.setdefault(event, []).append({"hooks": [hook_for(event, cmd, timeout)]})
    return settings


def _read(path: Path) -> dict:
    if not path.exists():
        return {}
    data = json.loads(path.read_text() or "{}")
    if not isinstance(data, dict):
        raise ValueError(f"{path} is not a JSON object")
    return data


def _write(path: Path, data: dict) -> None:
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


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="claude_hooks")
    ap.add_argument("action", choices=("add", "remove"))
    ap.add_argument("--settings", required=True)
    ap.add_argument("--script", required=True, help="the hook script's path")
    ap.add_argument("--home", help="the board home the hook names (add)")
    ap.add_argument("--agent", help="the agent the hook waits as (add)")
    ap.add_argument("--timeout", type=int, default=TIMEOUT)
    a = ap.parse_args(argv)
    path = Path(a.settings)
    try:
        before = _read(path)
        if a.action == "add":
            if not a.home or not a.agent:
                raise ValueError("add needs --home and --agent")
            after = add(json.loads(json.dumps(before)), a.script, command(a.home, a.agent, a.script), a.timeout)
        else:
            after = strip(json.loads(json.dumps(before)), a.script)
    except ValueError as e:
        print(f"Not changed: {e}", file=sys.stderr)
        return 1
    if after != before or (a.action == "add" and not path.exists()):
        _write(path, after)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
