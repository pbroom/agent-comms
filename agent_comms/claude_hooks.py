"""Adding and removing the wake hooks in a Claude Code settings file (integrations/claude-code/install.sh --wake).

`python -m agent_comms.claude_hooks add|remove --settings FILE --script PATH [--command CMD] [--timeout S]`

The hooks are a `Stop` and a `SessionStart` command hook with `"asyncRewake": true` that run the given command
(`.../wait.sh`). Ours are recognised by the script's path in their command, so `add` replaces them (idempotent, and a
new agent or board home takes effect) and `remove` takes out only them, with any group or event left empty. Every
other setting and hook is kept as it was. The file is written atomically, with its mode kept (600 when new); a file
that is not a JSON object is refused and left alone.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

EVENTS = ("SessionStart", "Stop")
TIMEOUT = 86400     # seconds; `board wait` gives up 60 s before it (waiter.DEFAULT_MAX_SECONDS)


def _ours(hook, script: str) -> bool:
    return isinstance(hook, dict) and isinstance(hook.get("command"), str) and script in hook["command"]


def strip(settings: dict, script: str) -> dict:
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return settings
    for event in list(hooks):
        groups = hooks[event]
        if not isinstance(groups, list):
            continue
        kept = []
        for g in groups:
            if isinstance(g, dict) and isinstance(g.get("hooks"), list):
                inner = [h for h in g["hooks"] if not _ours(h, script)]
                if inner:
                    kept.append(g | {"hooks": inner})
                elif not g["hooks"]:
                    kept.append(g)
            else:
                kept.append(g)
        if kept:
            hooks[event] = kept
        else:
            del hooks[event]
    if not hooks:
        del settings["hooks"]
    return settings


def add(settings: dict, script: str, command: str, timeout: int = TIMEOUT) -> dict:
    settings = strip(settings, script)
    hooks = settings.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError("the settings file's \"hooks\" is not an object")
    for event in EVENTS:
        groups = hooks.setdefault(event, [])
        if not isinstance(groups, list):
            raise ValueError(f"the settings file's hooks.{event} is not a list")
        groups.append({"hooks": [{"type": "command", "command": command, "asyncRewake": True, "timeout": timeout}]})
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
    ap.add_argument("--script", required=True, help="the hook script's path; recognises our hooks")
    ap.add_argument("--command", help="the hook command (add)")
    ap.add_argument("--timeout", type=int, default=TIMEOUT)
    a = ap.parse_args(argv)
    path = Path(a.settings)
    try:
        before = _read(path)
    except ValueError as e:
        print(f"Not changed: {e}", file=sys.stderr)
        return 1
    if a.action == "add":
        if not a.command or a.script not in a.command:
            print("add needs --command, and it must run --script", file=sys.stderr)
            return 1
        try:
            after = add(json.loads(json.dumps(before)), a.script, a.command, a.timeout)
        except ValueError as e:
            print(f"Not changed: {e}", file=sys.stderr)
            return 1
    else:
        after = strip(json.loads(json.dumps(before)), a.script)
    if after != before or (a.action == "add" and not path.exists()):
        _write(path, after)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
