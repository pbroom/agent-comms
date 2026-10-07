"""Links from the human's dashboard to each agent session's own conversation in its desktop app.

A board session records which client conversation it belongs to (`sessions.client_kind`,
`sessions.client_session_id`), and the human's `/api/state` turns that into a deep link plus a CLI fallback:

- Claude Code: `claude://resume?session=<uuid>` (Claude desktop app) / `claude --resume <uuid>`.
- Codex: `codex://threads/<uuid>` (ChatGPT/Codex desktop app) / `codex resume <uuid>`.

Both URL schemes were found in the installed apps' bundles; neither is documented, so they may change.

Capture never trusts tool parameters or request bodies (DESIGN_NOTES "Conversation links"):
- Claude Code: the stdio MCP server inherits CLAUDE_CODE_SESSION_ID from the Claude Code process that launched it
  (`claude_client_from_env`, called by mcp_server's board_register).
- Codex sets no such variable. Codex writes every MCP tool call and its result to its rollout file
  `$CODEX_HOME/sessions/YYYY/MM/DD/rollout-<timestamp>-<thread uuid>.jsonl`, so `CodexResolver` finds the file that
  recorded the board_register result for a board session and takes the thread UUID from the file NAME. Rollout
  content never leaves this module: only that UUID does.

Every id is checked against UUID_RE before it is stored and again before a URL is built from it.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

log = logging.getLogger("agent_comms.conversations")

UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
CLAUDE_ENV = "CLAUDE_CODE_SESSION_ID"
CLAUDE, CODEX = "claude-code", "codex"
# kind -> (app name, deep link, resume command). Only ever formatted with an id that matched UUID_RE.
KINDS = {
    CLAUDE: ("Claude", "claude://resume?session={id}", "claude --resume {id}"),
    CODEX: ("ChatGPT", "codex://threads/{id}", "codex resume {id}"),
}

# CodexResolver bounds. A Codex session is looked up only while it is recent and still unlinked, at most once a
# minute, over at most MAX_FILES rollout files and the first MAX_BYTES of each.
RECENT_SECONDS = 24 * 3600      # sessions whose last_seen is older than this are not looked up
SLACK_SECONDS = 120             # a rollout file must have been written at or after started_at minus this
RETRY_SECONDS = 60              # per board session, after a miss
MAX_FILES = 60
MAX_BYTES = 1 << 20
LOOKBACK_DAYS = 7               # rollout day directories looked at before the earliest pending session start
MAX_DAYS = 60                   # never walk more day directories than this
ROLLOUT_RE = re.compile(r"rollout-(\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2})-(" + UUID_RE.pattern + r")\.jsonl")
# Fallback for a board_register result text that is not parseable JSON: tolerant of escaping, and anchored at the
# start of the result, where register's first two keys are. Anchoring is what keeps a JSON-looking string inside the
# result (the agent's own project path, say) from counting.
_REGISTER_TEXT = re.compile(r'\{\s*\\*"session_id\\*"\s*:\s*(\d{1,12})\s*,\s*\\*"agent\\*"\s*:\s*\\*"([a-z][a-z0-9_-]{0,31})\\*"')
_RESULT_KEYS = ("content", "text", "output", "result", "Ok", "structuredContent", "structured_content")


def normalize_uuid(value: Any) -> str | None:
    """The canonical lower-case UUID, or None for anything else (wrong type, length, characters, whitespace)."""
    if not isinstance(value, str) or len(value) != 36:
        return None
    v = value.lower()
    return v if UUID_RE.fullmatch(v) else None


def normalize_client(client: Any) -> tuple[str, str] | None:
    """(kind, uuid) when both are valid, else None: garbage is dropped, never stored."""
    if not isinstance(client, tuple) or len(client) != 2 or client[0] not in KINDS:
        return None
    cid = normalize_uuid(client[1])
    return (client[0], cid) if cid else None


def claude_client_from_env(environ: Mapping[str, str]) -> tuple[str, str] | None:
    """The Claude Code conversation that launched this stdio MCP server, from its inherited environment."""
    cid = normalize_uuid(environ.get(CLAUDE_ENV))
    return (CLAUDE, cid) if cid else None


def conversation(kind: str | None, client_id: str | None, cwd: str | None) -> dict | None:
    """What the human's dashboard shows for one session, built only from a re-validated UUID."""
    cid = normalize_uuid(client_id)
    if kind not in KINDS or cid is None:
        return None
    app, url, command = KINDS[kind]
    return {"app": app, "url": url.format(id=cid), "resume_command": command.format(id=cid), "cwd": cwd}


# ---------------------------------------------------------------- settings


@dataclass(frozen=True)
class ConversationConfig:
    """board.toml [conversations]. enabled=false turns off capture, the Codex lookup and the links."""
    enabled: bool = True
    codex_home: str = ""        # "" means $CODEX_HOME, else ~/.codex

    @classmethod
    def from_dict(cls, d: Any) -> "ConversationConfig":
        if not isinstance(d, dict):
            raise ValueError("[conversations] must be a table")
        unknown = set(d) - {"enabled", "codex_home"}
        if unknown:
            raise ValueError(f"unknown setting [conversations] {sorted(unknown)[0]}")
        enabled, home = d.get("enabled", True), d.get("codex_home", "")
        if not isinstance(enabled, bool):
            raise ValueError("[conversations] enabled must be true or false")
        if not isinstance(home, str) or len(home) > 1024 or (home and not Path(home).expanduser().is_absolute()):
            raise ValueError("[conversations] codex_home must be an absolute path, or \"\" for the default")
        return cls(enabled, home)

    def codex_dir(self, environ: Mapping[str, str] = os.environ) -> Path:
        home = self.codex_home or environ.get("CODEX_HOME") or "~/.codex"
        return Path(home).expanduser()


def config_of(settings: Any) -> ConversationConfig:
    """The settings' [conversations] table, or off (with a warning) when it does not validate."""
    try:
        return ConversationConfig.from_dict(getattr(settings, "conversations", {}))
    except ValueError as e:
        log.warning("conversation links are off: %s", e)
        return ConversationConfig(enabled=False)


# ---------------------------------------------------------------- Codex rollout lookup


def _is_register_name(name: Any) -> bool:
    return isinstance(name, str) and (name == "board_register" or name.endswith(("__board_register", ".board_register",
                                                                                   "/board_register")))


def _register_ids(x: Any, depth: int = 0) -> set[tuple[int, str]]:
    """(session_id, agent) of the board_register result documents in a recorded tool result. Follows only the
    containers results come in (content items, text, output, Ok/structured content) and JSON text inside them,
    never argument or free-text fields, so text an agent wrote cannot pose as a result."""
    if depth > 6:
        return set()
    if isinstance(x, str):
        s = x.strip()
        if not s.startswith(("{", "[")):
            return set()
        try:
            return _register_ids(json.loads(s), depth + 1)
        except ValueError:
            m = _REGISTER_TEXT.match(s)
            return {(int(m.group(1)), m.group(2))} if m else set()
    if isinstance(x, list):
        return set().union(*(_register_ids(i, depth + 1) for i in x[:20]))
    if isinstance(x, dict):
        sid, agent = x.get("session_id"), x.get("agent")
        if isinstance(sid, int) and not isinstance(sid, bool) and isinstance(agent, str) and "runtime" in x:
            return {(sid, agent)}
        return set().union(*(_register_ids(x[k], depth + 1) for k in _RESULT_KEYS if k in x))
    return set()


def register_results(lines: Iterable[str]) -> set[tuple[int, str]]:
    """Every board_register result recorded in a rollout file's lines. Two layouts are recognized:
    - one record per call with its result: `payload.item` (or `payload`, `payload.msg`) carrying `tool` (or
      `invocation.tool`) == board_register and `result` (Codex's McpToolCall item, mcp_tool_call_end);
    - a call record with `name` ending in board_register and a `call_id`, then an output record with that
      `call_id` and `output` (function_call / function_call_output)."""
    found: set[tuple[int, str]] = set()
    call_ids: set[str] = set()
    for line in lines:
        if "board_register" not in line and not (call_ids and '"call_id"' in line and any(c in line for c in call_ids)):
            continue
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if not isinstance(d, dict):
            continue
        payload = d.get("payload") if isinstance(d.get("payload"), dict) else {}
        for o in (d, payload, payload.get("item"), payload.get("msg")):   # fixed places only, never arguments
            if not isinstance(o, dict):
                continue
            inv = o.get("invocation") if isinstance(o.get("invocation"), dict) else {}
            if o.get("tool") == "board_register" or inv.get("tool") == "board_register":
                found |= _register_ids(o.get("result"))
            elif _is_register_name(o.get("name")) and isinstance(o.get("call_id"), str):
                call_ids.add(o["call_id"])
            elif o.get("call_id") in call_ids and "output" in o:
                found |= _register_ids(o.get("output"))
    return found


def _iso_ts(v: Any) -> float | None:
    if not isinstance(v, str):
        return None
    try:
        return datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


@dataclass
class _Rollout:
    path: Path
    thread_id: str
    mtime: float
    name_ts: str


class CodexResolver:
    """Finds the Codex thread behind a board session from Codex's own rollout files (lazy, bounded, throttled).

    `home` is the Codex home ($CODEX_HOME, ~/.codex); `clock` returns epoch seconds. Both are injectable for tests."""

    def __init__(self, home: Path, clock: Callable[[], float] = time.time):
        self.home = Path(home)
        self.clock = clock
        self._tried: dict[int, float] = {}   # board session id -> last lookup (a miss), for RETRY_SECONDS

    def due(self, session_ids: Iterable[int]) -> list[int]:
        now = self.clock()
        return [s for s in session_ids if now - self._tried.get(s, -1e18) >= RETRY_SECONDS]

    def resolve(self, sessions: list[dict]) -> dict[int, str]:
        """sessions: [{"id", "agent", "started_at"}] still unlinked. Returns {board session id: thread uuid} for the
        ones found. A session that is not found is not looked up again for RETRY_SECONDS."""
        now = self.clock()
        due = set(self.due(s["id"] for s in sessions))
        todo = [s for s in sessions if s["id"] in due]
        if not todo:
            return {}
        for s in todo:
            self._tried[s["id"]] = now
        if len(self._tried) > 1000:   # forget old misses
            self._tried = {k: v for k, v in self._tried.items() if now - v < RECENT_SECONDS}
        files = self._candidates(min(s["started_at"] for s in todo) - SLACK_SECONDS)
        parsed: dict[Path, set[tuple[int, str]]] = {}
        out: dict[int, str] = {}
        for s in todo:
            mine = sorted((f for f in files if f.mtime >= s["started_at"] - SLACK_SECONDS),
                          key=lambda f: f.mtime, reverse=True)[:MAX_FILES]
            hits = []
            for f in mine:
                if f.path not in parsed:
                    parsed[f.path] = self._read(f.path)
                if (s["id"], s["agent"]) in parsed[f.path]:
                    hits.append(f)
            if hits:
                out[s["id"]] = self._pick(hits, s["started_at"])
                self._tried.pop(s["id"], None)
        return out

    def _candidates(self, since: float) -> list[_Rollout]:
        """Rollout files written at or after `since`, from the day directories that can hold them. Files sit
        under the day their thread started (local time), so a thread resumed later is found only within
        LOOKBACK_DAYS of the session's start."""
        root = self.home / "sessions"
        today = date.fromtimestamp(self.clock()) + timedelta(days=1)
        first = max(date.fromtimestamp(since) - timedelta(days=LOOKBACK_DAYS), today - timedelta(days=MAX_DAYS))
        out: list[_Rollout] = []
        day = today
        while day >= first:
            d = root / f"{day:%Y}" / f"{day:%m}" / f"{day:%d}"
            day -= timedelta(days=1)
            try:
                entries = list(os.scandir(d))
            except OSError:
                continue
            for e in entries:
                m = ROLLOUT_RE.fullmatch(e.name)
                if not m:
                    continue
                try:
                    if not e.is_file(follow_symlinks=False):
                        continue
                    mtime = e.stat(follow_symlinks=False).st_mtime
                except OSError:
                    continue
                if mtime >= since:
                    out.append(_Rollout(Path(e.path), m.group(2), mtime, m.group(1)))
        return out

    @staticmethod
    def _read(path: Path) -> set[tuple[int, str]]:
        try:
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            with os.fdopen(fd, "rb") as f:
                data = f.read(MAX_BYTES + 1)
        except OSError:
            return set()
        lines = data[:MAX_BYTES].decode("utf-8", "replace").splitlines()
        if len(data) > MAX_BYTES and lines:
            lines.pop()   # cut mid-line
        return register_results(lines)

    def _pick(self, hits: list[_Rollout], started_at: float) -> str:
        """One match: it. Several (a resumed board session, or a copied thread): the thread that started closest
        before the board session did; if none started before it, the earliest."""
        starts = [(self._thread_start(f), f.thread_id) for f in hits]
        before = [x for x in starts if x[0] <= started_at]
        return (max(before) if before else min(starts))[1]

    @staticmethod
    def _thread_start(f: _Rollout) -> float:
        """The session_meta timestamp on the file's first line (UTC), else the file name's (local time), else mtime."""
        try:
            fd = os.open(f.path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            with os.fdopen(fd, "rb") as fh:
                first = fh.readline(64 * 1024).decode("utf-8", "replace")
            d = json.loads(first)
            if isinstance(d, dict):
                payload = d.get("payload") if isinstance(d.get("payload"), dict) else {}
                ts = _iso_ts(payload.get("timestamp")) or _iso_ts(d.get("timestamp"))
                if ts is not None:
                    return ts
        except (OSError, ValueError):
            pass
        try:
            return time.mktime(time.strptime(f.name_ts, "%Y-%m-%dT%H-%M-%S"))
        except (ValueError, OverflowError):
            return f.mtime
