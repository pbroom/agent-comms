"""The human's Settings page: read effective settings with their sources, and save edits to board.local.toml.

Only a fixed list of scalar settings is editable (EDITABLE, with bounds). Everything else stays file-only:
host, port and paths need a restart, and the dispatcher's runners, env and worktrees are executable command
templates and process environment, which a browser must never be able to change (DESIGN_NOTES "Settings page").

Writes go to board.local.toml beside board.toml (gitignored), never to board.toml. The file is edited line by
line, so every line we do not touch stays byte for byte (comments, runners, other tables); a key we change has
its value replaced in place (keeping a trailing comment), a new key is added to the end of its table, and a
missing table is appended. The result is parsed back and must equal exactly the old file plus the requested
changes, or nothing is written. Then it is written atomically (temp file, fsync, rename) with mode 600.

Every running process picks the change up by itself: Board.reload_settings re-reads both files when either
changes (the way agents.toml is reloaded), and the dispatcher applies its scalars each pass.
"""

from __future__ import annotations

import copy
import json
import math
import os
import re
import secrets
import threading
import tomllib
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .config import LOCAL_SETTINGS, SECTIONS, Settings, deep_merge
from .core import Board, Conflict, Invalid, Principal, check_reloadable

AUDIT_FILE = "settings-audit.jsonl"   # in the data directory, beside the database
LOCK_FILE = "settings.lock"
AUDIT_SHOWN = 20


@dataclass(frozen=True)
class Spec:
    section: str
    name: str
    kind: type          # int, float (accepts whole numbers too) or bool
    lo: float | None
    hi: float | None
    default: Any
    label: str

    @property
    def key(self) -> str:
        return f"{self.section}.{self.name}"


_d = Settings()
EDITABLE: dict[str, Spec] = {s.key: s for s in (
    Spec("limits", "lease_ttl_minutes", int, 1, 1440, _d.lease_ttl_minutes, "Task lease length (minutes)"),
    Spec("limits", "max_agent_posts_per_thread_without_human", int, 1, 1000,
         _d.max_agent_posts_per_thread_without_human, "Agent posts per thread before a human post is needed"),
    Spec("limits", "daily_post_cap_per_agent", int, 1, 100_000, _d.daily_post_cap_per_agent,
         "Posts per agent per rolling 24 hours"),
    Spec("limits", "body_max_bytes", int, 256, 65_536, _d.body_max_bytes, "Post body limit (bytes)"),
    Spec("limits", "max_refs", int, 1, 100, _d.max_refs, "Refs per post"),
    Spec("tasks", "require_human_accept", bool, None, None, _d.require_human_accept,
         "Agent-proposed tasks need the human (or a matching grant) before anyone can claim them"),
    # live_minutes >= 1: a session blocked in a long-poll refreshes last_seen at least every 30 s, and the
    # dispatcher must keep seeing it as live.
    Spec("dispatch", "live_minutes", float, 1, 120, 2.0, "A session seen this recently counts as live (minutes)"),
    Spec("dispatch", "poll_seconds", float, 1, 300, 5.0, "Dispatcher poll interval (seconds)"),
    Spec("dispatch", "timeout_minutes", float, 1, 1440, 30.0, "Wall-clock limit per run (minutes)"),
    Spec("dispatch", "kill_grace_seconds", float, 1, 300, 10.0, "SIGTERM to SIGKILL grace (seconds)"),
    Spec("dispatch", "max_concurrent", int, 1, 20, 2, "Runs at once across all agents"),
)}
del _d

# Recognised but deliberately not editable from a browser.
FILE_ONLY = {"host", "port", "db_path", "agents_path", "runners", "env", "worktrees"}
FILE_ONLY_WHY = ("is not editable from the dashboard. host, port and paths need a restart, and the dispatcher's "
                 "runners, env and worktrees decide what runs on this machine; edit board.local.toml by hand")
# [web] sign-in session lifetimes: a signed-in browser must not be able to extend its own sign-in.
WEB_FILE_ONLY = {"session_days", "session_max_days"}
WEB_FILE_ONLY_WHY = ("is not editable from the dashboard: a signed-in browser must not be able to lengthen its own "
                     "sign-in. Edit [web] in board.local.toml by hand")

_lock = threading.Lock()


class SettingsFileError(Exception):
    """board.local.toml has a layout the line editor will not touch safely."""


# ---------------------------------------------------------------- validation


def validate_changes(changes: Any) -> dict[str, Any]:
    """{"section.name": value | None} -> the same with checked values. None removes the board.local.toml
    override (the value falls back to board.toml or the default)."""
    if not isinstance(changes, dict) or not changes:
        raise Invalid("send a JSON object of settings to change, e.g. {\"limits.daily_post_cap_per_agent\": 100}")
    out: dict[str, Any] = {}
    for key, value in changes.items():
        parts = key.split(".") if isinstance(key, str) else []
        if any(x in FILE_ONLY for x in parts) or (parts and parts[0] == "server"):
            raise Invalid(f"{key} {FILE_ONLY_WHY}")
        if (parts and parts[0] == "web") or any(x in WEB_FILE_ONLY for x in parts):
            raise Invalid(f"{key} {WEB_FILE_ONLY_WHY}")
        spec = EDITABLE.get(key)
        if spec is None:
            raise Invalid(f"unknown setting {key!r}; editable settings are: {', '.join(EDITABLE)}")
        if value is None:
            out[key] = None
            continue
        if spec.kind is bool:
            if not isinstance(value, bool):
                raise Invalid(f"{key} must be true or false")
        elif spec.kind is int:
            if isinstance(value, bool) or not isinstance(value, int):
                raise Invalid(f"{key} must be a whole number")
        else:
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise Invalid(f"{key} must be a number")
        if spec.lo is not None and not spec.lo <= value <= spec.hi:
            raise Invalid(f"{key} must be between {_num(spec.lo)} and {_num(spec.hi)}")
        out[key] = value
    return out


def _num(x: float) -> str:
    return str(int(x)) if float(x).is_integer() else str(x)


# ---------------------------------------------------------------- the TOML line editor


def toml_value(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float) and math.isfinite(v):
        return repr(v)
    raise ValueError(f"cannot write {v!r}")


def _top_level_starts(lines: list[str]) -> list[bool]:
    """For each line (and one past the end): does it start outside any string, comment or open bracket?
    A minimal TOML scanner: strings (basic, literal, multi-line), comments, [] and {} nesting."""
    starts: list[bool] = []
    mode, depth = None, 0   # mode: None | '"' | "'" | '"""' | "'''"
    for line in lines:
        starts.append(mode is None and depth == 0)
        i, n = 0, len(line)
        while i < n:
            ch = line[i]
            if mode == '"""':
                if ch == "\\":
                    i += 2
                    continue
                if line.startswith('"""', i):
                    mode, i = None, i + 3
                    continue
            elif mode == "'''":
                if line.startswith("'''", i):
                    mode, i = None, i + 3
                    continue
            elif mode == '"':
                if ch == "\\":
                    i += 2
                    continue
                if ch == '"' or ch == "\n":
                    mode = None
            elif mode == "'":
                if ch == "'" or ch == "\n":
                    mode = None
            else:
                if ch == "#":
                    break
                if line.startswith('"""', i) or line.startswith("'''", i):
                    mode, i = line[i:i + 3], i + 3
                    continue
                if ch in "\"'":
                    mode = ch
                elif ch in "[{":
                    depth += 1
                elif ch in "]}":
                    depth = max(0, depth - 1)
            i += 1
        if mode in ('"', "'"):   # single-line strings end at the newline
            mode = None
    starts.append(mode is None and depth == 0)
    return starts


_HEADER = re.compile(r"^\s*\[(?!\[)\s*(?P<name>[^\[\]#]+?)\s*\]\s*(?:#.*)?$")
_ANY_HEADER = re.compile(r"^\s*\[")


def _header_name(line: str) -> str | None:
    m = _HEADER.match(line.rstrip("\r\n"))
    if not m:
        return None
    parts = [p.strip() for p in m.group("name").split(".")]
    return ".".join(p[1:-1] if len(p) >= 2 and p[0] == p[-1] and p[0] in "\"'" else p for p in parts)


def _key_line(name: str) -> re.Pattern:
    k = re.escape(name)
    return re.compile(rf"^(?P<pre>\s*(?:{k}|\"{k}\"|'{k}')\s*=\s*)(?P<val>[^\s#]+)(?P<post>\s*(?:#.*)?)$")


def _key_start(name: str) -> re.Pattern:
    k = re.escape(name)
    return re.compile(rf"^\s*(?:{k}|\"{k}\"|'{k}')\s*=")


def _table_region(lines: list[str], starts: list[bool], section: str) -> tuple[int, int] | None:
    """(header line index, end index exclusive) of the [section] table, or None."""
    headers = [i for i, line in enumerate(lines) if starts[i] and _ANY_HEADER.match(line)]
    mine = [i for i in headers if _header_name(lines[i]) == section]
    if not mine:
        return None
    if len(mine) > 1:
        raise SettingsFileError(f"[{section}] appears more than once")
    nxt = [i for i in headers if i > mine[0]]
    return mine[0], (nxt[0] if nxt else len(lines))


def edit_toml(text: str, section: str, name: str, value: Any) -> str:
    """Set (or with value None, remove) one scalar key in one table, touching no other line."""
    lines = text.splitlines(keepends=True)
    starts = _top_level_starts(lines)
    region = _table_region(lines, starts, section)
    if region is not None:
        head, end = region
        for i in range(head + 1, end):
            if not starts[i] or not _key_start(name).match(lines[i]):
                continue
            body, nl = (lines[i][:-1], "\n") if lines[i].endswith("\n") else (lines[i], "")
            if body.endswith("\r"):
                body, nl = body[:-1], "\r" + nl
            m = _key_line(name).match(body)
            if m is None or not starts[i + 1]:
                raise SettingsFileError(f"[{section}] {name} is not a single-line value")
            if value is None:
                del lines[i]
            else:
                lines[i] = m.group("pre") + toml_value(value) + m.group("post") + nl
            return "".join(lines)
        if value is None:
            return text
        last = head
        for i in range(head + 1, end):
            stripped = lines[i].strip()
            if stripped and not stripped.startswith("#") and starts[i + 1]:
                last = i
        if not lines[last].endswith("\n"):
            lines[last] += "\n"
        lines.insert(last + 1, f"{name} = {toml_value(value)}\n")
        return "".join(lines)
    if value is None:
        return text
    if text and not text.endswith("\n"):
        text += "\n"
    sep = "\n" if text.strip() else ""
    return text + sep + f"[{section}]\n{name} = {toml_value(value)}\n"


def apply_changes(text: str, changes: dict[str, Any]) -> tuple[str, dict]:
    """The new file text, after checking that it parses back to exactly the old content plus `changes`."""
    try:
        old = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise SettingsFileError(f"it is not valid TOML ({e})") from None
    expected = copy.deepcopy(old)
    for key, value in changes.items():
        spec = EDITABLE[key]
        table = expected.get(spec.section)
        if table is not None and not isinstance(table, dict):
            raise SettingsFileError(f"{spec.section} is not a table")
        text = edit_toml(text, spec.section, spec.name, value)
        if value is None:
            if table is not None:
                table.pop(spec.name, None)
        else:
            expected.setdefault(spec.section, {})[spec.name] = value
    try:
        new = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise SettingsFileError(f"the edit would not parse ({e})") from None
    if new != expected:
        raise SettingsFileError("the edit would change more than the requested keys")
    return text, new


def atomic_write(path: Path, text: str) -> None:
    """Write beside the target, fsync, then rename over it: readers see the old file or the new one."""
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(6)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------- reading


def _get(data: dict, spec: Spec) -> tuple[bool, Any]:
    table = data.get(spec.section)
    if isinstance(table, dict) and spec.name in table:
        return True, table[spec.name]
    return False, None


def _value(data: dict, spec: Spec) -> Any:
    found, v = _get(data, spec)
    return v if found else spec.default


def _layers(path: Path) -> tuple[dict, str | None, dict, str | None]:
    base, base_err, local, local_err = {}, None, {}, None
    try:
        base = Settings.read_layer(path)
    except Exception as e:
        base_err = f"{type(e).__name__}: {e}"
    try:
        local = Settings.read_layer(path.with_name(LOCAL_SETTINGS))
    except Exception as e:
        local_err = f"{type(e).__name__}: {e}"
    return base, base_err, local, local_err


def _effective(board: Board, spec: Spec) -> Any:
    if spec.section == "dispatch":
        d = board.s.dispatch if isinstance(board.s.dispatch, dict) else {}
        return d.get(spec.name, spec.default)
    return getattr(board.s, spec.name)


def _audit_path(board: Board) -> Path:
    return board.s.db_path.parent / AUDIT_FILE


def read_audit(board: Board, limit: int = AUDIT_SHOWN) -> list[dict]:
    path = _audit_path(board)
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out = []
    for line in reversed(lines):
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if isinstance(d, dict):
            out.append(d)
        if len(out) >= limit:
            break
    return out


def get_settings(board: Board, p: Principal) -> dict:
    """Effective values (what this process is running with), where each comes from, bounds, and recent changes."""
    board._require_human(p, "view board settings")
    path = board.s.config_path
    base, base_err, local, local_err = _layers(path) if path is not None else ({}, None, {}, None)
    items = []
    for spec in EDITABLE.values():
        if path is None:
            source = "process"
        elif _get(local, spec)[0] and not local_err:
            source = LOCAL_SETTINGS
        elif _get(base, spec)[0] and not base_err:
            source = "board.toml"
        else:
            source = "default"
        items.append({"key": spec.key, "section": spec.section, "name": spec.name, "label": spec.label,
                      "type": spec.kind.__name__, "min": spec.lo, "max": spec.hi, "default": spec.default,
                      "value": _effective(board, spec), "source": source})
    return {"settings": items,
            "files": {"board_toml": "board.toml", "local": LOCAL_SETTINGS,
                      "local_exists": path is not None and path.with_name(LOCAL_SETTINGS).exists(),
                      "writable": path is not None, "board_toml_error": base_err, "local_error": local_err,
                      "reload_error": board.settings_error},
            "paused": board.is_paused(),
            "agents": [dict(r) | {"is_human": bool(r["is_human"])} for r in board.conn.execute(
                "SELECT name, runtime, is_human FROM agents WHERE active = 1 ORDER BY is_human DESC, name")],
            "audit": read_audit(board)}


# ---------------------------------------------------------------- writing


def update_settings(board: Board, p: Principal, changes: Any) -> dict:
    """Validate, write the changed keys to board.local.toml, audit them, and apply them to this Board now."""
    board._require_human(p, "change board settings")
    changes = validate_changes(changes)
    path = board.s.config_path
    if path is None:
        raise Conflict("this board's settings were not loaded from board.toml, so there is nowhere to save them")
    local_path = path.with_name(LOCAL_SETTINGS)
    with _lock, _file_lock(board):
        base, base_err, _, _ = _layers(path)
        if base_err:
            raise Conflict(f"board.toml does not load ({base_err}); fix it first. Nothing was written.")
        try:
            old_text = local_path.read_text(encoding="utf-8") if local_path.exists() else ""
            old_local = tomllib.loads(old_text)
            new_text, new_local = apply_changes(old_text, changes)
        except (SettingsFileError, tomllib.TOMLDecodeError, UnicodeDecodeError) as e:
            raise Conflict(f"{LOCAL_SETTINGS} was not changed: {e}. Edit it by hand.") from None
        old_merged, new_merged = deep_merge(base, old_local), deep_merge(base, new_local)
        try:  # the whole result must load, exactly as every process will load it
            for section in SECTIONS:
                table = new_local.get(section, {})
                if not isinstance(table, dict):
                    raise ValueError(f"[{section}] must be a table")
                unknown = set(table) - Settings.known_keys()
                if unknown:
                    raise ValueError(f"unknown setting [{section}] {sorted(unknown)[0]}")
            check_reloadable(Settings.from_data(new_merged))
        except ValueError as e:
            raise Conflict(f"the settings files would not load after this change ({e}); nothing was written") from None
        entries = []
        now = board.now()
        for key in changes:
            spec = EDITABLE[key]
            if _get(old_local, spec) == _get(new_local, spec):
                continue   # already so in board.local.toml: nothing to write or record
            entries.append({"at": datetime.fromtimestamp(now, UTC).isoformat(timespec="seconds"), "by": p.name,
                            "key": key, "old": _value(old_merged, spec), "new": _value(new_merged, spec),
                            "file": LOCAL_SETTINGS})
        if new_text != old_text:
            atomic_write(local_path, new_text)
            _append_audit(board, entries)
    if not board.reload_settings(force=True):
        # Never report success for settings this board is not running with.
        raise Conflict("the settings were saved to " + LOCAL_SETTINGS + " but this board could not apply them ("
                       + (board.settings_error or "unknown error") + "); the last valid settings remain active")
    out = get_settings(board, p)
    out["changed"] = [e["key"] for e in entries]
    return out


def notifier_deliverer(board: Board):
    """The board's own notification deliverer when it has one (tests inject a fake), else macOS delivery."""
    from .notify import MacOSDeliverer

    d = getattr(board.notifier, "deliverer", None)
    return d if callable(d) and callable(getattr(d, "available", None)) else MacOSDeliverer()


def _append_audit(board: Board, entries: list[dict]) -> None:
    if not entries:
        return
    path = _audit_path(board)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as f:
        f.write("".join(json.dumps(e, sort_keys=True) + "\n" for e in entries))


class _file_lock:
    """Serializes writers across processes (flock on a lock file in the data directory)."""

    def __init__(self, board: Board):
        self.path = board.s.db_path.parent / LOCK_FILE
        self.fd: int | None = None

    def __enter__(self):
        import fcntl

        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.fd = os.open(self.path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        fcntl.flock(self.fd, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        import fcntl

        if self.fd is not None:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
            os.close(self.fd)
            self.fd = None
