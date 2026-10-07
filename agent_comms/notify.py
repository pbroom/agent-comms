"""Human notifications: the default `Board.notifier`.

The board stays pull-only for agents. This module only tells the *human* (via macOS Notification
Center) that something on the board is waiting for them, so a question or an unfinalized decision
does not sit unseen. It never wakes, runs or messages an agent. (The separate, human-approved
dispatcher in dispatch.py does launch agents; it reports each launch here as `agent-launched`.)

Configuration lives in the `subscriptions` table: rows owned by the human with `channel='macos'`,
an `events` json list (see NOTIFY_EVENTS) and optional `project` / `thread_id` filters. With no such
row nothing happens, which is the default.

Safety properties (see DESIGN_NOTES "Wake hook"):
- Runs after the write has committed, in whichever process made it (HTTP server, stdio MCP, CLI).
  Every failure is swallowed: a notifier problem never fails a board write.
- Post text reaches `osascript` only as argv to a fixed `on run argv` script, never as AppleScript
  source, and never through a shell. The child is spawned without waiting; a daemon thread reaps it.
- Content is server-stamped metadata (agent name, post type, thread id) plus at most ~100 chars of
  post body with control/format characters removed. Sealed posts contribute no text at all.
- At most one notification per post (per-process dedupe; a post's `post.created` fires in exactly one
  process). Bursts are coalesced through a cross-process rate gate kept in `board_state`.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
import unicodedata
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Callable

log = logging.getLogger("agent_comms.notify")

CHANNEL = "macos"
NOTIFY_EVENTS = ("needs-response", "to-human", "decision", "idle-agent", "agent-launched")
DEFAULT_NOTIFY_EVENTS = ("needs-response", "to-human", "decision", "agent-launched")
DEFAULT_IDLE_MINUTES = 30
TITLE = "agent-comms"
SNIPPET_CHARS = 100
MIN_FLUSH_DELAY = 1.0  # seconds; keeps a coalescing flush from spinning

LABELS = {  # most important first; a post gets one label
    "needs-response": "Needs your response",
    "decision": "Decision awaiting finalize",
    "to-human": "Addressed to you",
}

OSASCRIPT = "/usr/bin/osascript"
# Fixed AppleScript. Untrusted text arrives only as `argv` items: it is data, never source.
APPLESCRIPT = (
    "on run argv",
    "display notification (item 3 of argv) with title (item 1 of argv) subtitle (item 2 of argv)",
    "end run",
)
# Environment passed to osascript: enough for a GUI session, and nothing else (no board tokens).
_ENV_KEYS = ("HOME", "USER", "LOGNAME", "TMPDIR", "LANG", "LC_ALL", "LC_CTYPE", "__CF_USER_TEXT_ENCODING")


def clean(text: Any, limit: int = SNIPPET_CHARS) -> str:
    """Single line, no control/format/separator characters (incl. bidi overrides), at most `limit` chars."""
    chars = [" " if unicodedata.category(ch) in ("Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp") else ch
             for ch in str(text)]
    s = " ".join("".join(chars).split())
    return s if len(s) <= limit else s[: limit - 1].rstrip() + "…"


@dataclass
class Notification:
    title: str
    subtitle: str
    message: str
    agents: tuple[str, ...] = ()
    thread_ids: tuple[int, ...] = ()
    kind: str = ""

    def argv_fields(self) -> list[str]:
        return [clean(self.title, 60), clean(self.subtitle, 120), clean(self.message, 2 * SNIPPET_CHARS)]


# ---------------------------------------------------------------- delivery


def osascript_argv(n: Notification, osascript: str = OSASCRIPT) -> list[str]:
    """The exact argv. The first positional argument is always the constant title, so osascript's
    option parsing stops before any untrusted text; the script itself never contains post text."""
    argv = [osascript]
    for line in APPLESCRIPT:
        argv += ["-e", line]
    return argv + n.argv_fields()


class MacOSDeliverer:
    """Spawns `osascript` and returns immediately. Does nothing useful off macOS."""

    def __init__(self, popen: Callable[..., Any] = subprocess.Popen, platform: str | None = None,
                 osascript: str = OSASCRIPT):
        self.popen = popen
        self.platform = sys.platform if platform is None else platform
        self.osascript = osascript

    def available(self) -> bool:
        return self.platform == "darwin" and os.path.isfile(self.osascript) and os.access(self.osascript, os.X_OK)

    def __call__(self, n: Notification) -> None:
        proc = self.popen(osascript_argv(n, self.osascript), shell=False, stdin=subprocess.DEVNULL,
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True,
                          start_new_session=True,
                          env={k: os.environ[k] for k in _ENV_KEYS if k in os.environ} | {"PATH": "/usr/bin:/bin"})
        # Reap off the write path so no zombie lingers in a long-lived server; if this process exits
        # first, the orphan is reaped by launchd.
        threading.Thread(target=_reap, args=(proc,), daemon=True, name="agent-comms-notify-reap").start()


def _reap(proc: Any, timeout: float = 30.0) -> None:
    try:
        proc.wait(timeout=timeout)
    except Exception:
        try:
            proc.kill()
            proc.wait(timeout=5)
        except Exception:
            pass


def _timer(delay: float, fn: Callable[[], None]) -> None:
    t = threading.Timer(delay, fn)
    t.daemon = True
    t.start()


def sample_notification() -> Notification:
    return Notification(TITLE, "test notification", "Board notifications are working.", kind="test")


# ---------------------------------------------------------------- notifier


@dataclass
class _Pending:
    items: list[Notification] = field(default_factory=list)
    scheduled: bool = False
    attempts: int = 0


class HumanNotifier:
    """Callable with the `Board.notifier` signature: (event, payload) -> None. Never raises."""

    GATE_KEY = "notify.macos.last_sent"
    IDLE_KEY = "notify.macos.idle."

    def __init__(self, board: Any, deliverer: Callable[[Notification], None] | None = None,
                 min_interval: float = 30.0, schedule: Callable[[float, Callable[[], None]], None] = _timer,
                 max_flush_attempts: int = 3):
        self.board = board
        self.deliverer = deliverer if deliverer is not None else MacOSDeliverer()
        self.min_interval = min_interval
        self.schedule = schedule
        self.max_flush_attempts = max_flush_attempts
        self._lock = threading.Lock()
        self._seen: OrderedDict[Any, None] = OrderedDict()
        self._pending = _Pending()
        self._gate_conn = None
        self._available: bool | None = None

    # ------------------------------------------------------------ entry point

    def __call__(self, event: str, payload: dict[str, Any]) -> None:
        try:
            if event == "post.created":
                self._post_created(int(payload["post_id"]))
            elif event == "dispatch.launched":
                self._dispatch_launched(payload)
        except Exception:  # a notifier failure must never surface on the write path
            log.debug("notifier failed for %s", event, exc_info=True)

    def available(self) -> bool:
        if self._available is None:
            check = getattr(self.deliverer, "available", None)
            self._available = bool(check()) if callable(check) else True
        return self._available

    # ------------------------------------------------------------ evaluation

    def _subscriptions(self) -> list[dict]:
        rows = self.board.conn.execute(
            """SELECT s.* FROM subscriptions s JOIN agents a ON a.name = s.agent
               WHERE s.channel = ? AND s.active = 1 AND a.is_human = 1 AND a.active = 1""", (CHANNEL,)).fetchall()
        subs = []
        for r in rows:
            try:
                events = {e for e in json.loads(r["events"]) if e in NOTIFY_EVENTS}
                target = json.loads(r["target"]) if r["target"] else {}
                idle = int(target.get("idle_minutes", DEFAULT_IDLE_MINUTES)) if isinstance(target, dict) \
                    else DEFAULT_IDLE_MINUTES
            except (ValueError, TypeError):
                continue
            subs.append({"project": r["project"], "thread_id": r["thread_id"], "events": events,
                         "idle_minutes": max(1, idle)})
        return subs

    def _post_created(self, post_id: int) -> None:
        if not self.available():
            return
        subs = self._subscriptions()
        if not subs:
            return  # the default: nothing configured, one cheap query and out
        with self._lock:
            if post_id in self._seen:
                return
            self._seen[post_id] = None
            while len(self._seen) > 2000:
                self._seen.popitem(last=False)
        c = self.board.conn
        post = c.execute(
            """SELECT p.*, t.project AS project, a.is_human AS author_is_human
               FROM posts p JOIN threads t ON t.id = p.thread_id JOIN agents a ON a.name = p.agent
               WHERE p.id = ?""", (post_id,)).fetchone()
        if post is None or post["author_is_human"]:
            return  # the human never needs telling about their own post
        humans = {r[0] for r in c.execute("SELECT name FROM agents WHERE is_human = 1 AND active = 1")}
        to = set(json.loads(post["to_agents"]))
        kinds = set()
        if post["needs_response"] and (not to or to & humans):
            kinds.add("needs-response")
        if to & humans:
            kinds.add("to-human")
        if post["type"] == "decision" and not post["final"]:
            kinds.add("decision")
        matching = [s for s in subs
                    if (s["project"] is None or s["project"] == post["project"])
                    and (s["thread_id"] is None or s["thread_id"] == post["thread_id"])]
        wanted = kinds & set().union(*(s["events"] for s in matching)) if matching else set()
        if wanted:
            self._submit(self._human_notification(post, wanted))
            return
        idle_subs = [s for s in matching if "idle-agent" in s["events"]]
        if idle_subs:
            n = self._idle_notification(post, to - humans - {post["agent"]},
                                        min(s["idle_minutes"] for s in idle_subs))
            if n is not None:
                self._submit(n)

    def _dispatch_launched(self, payload: dict[str, Any]) -> None:
        """The dispatcher started an agent. Server-side metadata only: agent, thread, rule, budget left."""
        if not self.available():
            return
        subs = [s for s in self._subscriptions() if "agent-launched" in s["events"]]
        if not subs:
            return
        run_key = ("run", str(payload["run_id"]))
        with self._lock:
            if run_key in self._seen:
                return
            self._seen[run_key] = None
            while len(self._seen) > 2000:
                self._seen.popitem(last=False)
        thread_id, rule_id, left = int(payload["thread_id"]), int(payload["rule_id"]), int(payload["launches_left"])
        row = self.board.conn.execute("SELECT project FROM threads WHERE id = ?", (thread_id,)).fetchone()
        project = row["project"] if row else None
        if not any((s["project"] is None or s["project"] == project)
                   and (s["thread_id"] is None or s["thread_id"] == thread_id) for s in subs):
            return
        agent = clean(payload["agent"], 32)
        self._submit(Notification(TITLE, f"dispatcher · thread {thread_id}",
                                  f"Started {agent} (rule {rule_id}, {left} launch(es) left)",
                                  agents=(agent,), thread_ids=(thread_id,), kind="agent-launched"))

    def _human_notification(self, post, kinds: set[str]) -> Notification:
        kind = next(k for k in LABELS if k in kinds)
        sealed = bool(post["sealed"] or post["was_sealed"])
        snippet = "sealed post" if sealed else clean(post["body"])
        return Notification(TITLE, f"{post['agent']} · {post['type']} · thread {post['thread_id']}",
                            f"{LABELS[kind]}: {snippet}", agents=(post["agent"],),
                            thread_ids=(post["thread_id"],), kind=kind)

    def _idle_notification(self, post, agents: set[str], idle_minutes: int) -> Notification | None:
        c = self.board.conn
        cutoff = self.board.now() - idle_minutes * 60
        parts, nudged = [], []
        for agent in sorted(agents):
            last = c.execute("SELECT MAX(last_seen) FROM sessions WHERE agent = ?", (agent,)).fetchone()[0]
            if last is not None and last >= cutoff:
                continue
            unread = c.execute(
                """SELECT COUNT(*) FROM posts p
                   WHERE (p.sealed = 0 OR p.agent = :me) AND p.agent != :me
                     AND EXISTS (SELECT 1 FROM json_each(p.to_agents) j WHERE j.value = :me)
                     AND p.seq > COALESCE((SELECT MAX(c.last_seq) FROM cursors c
                                           WHERE c.agent = :me AND c.thread_id = p.thread_id), 0)""",
                {"me": agent}).fetchone()[0]
            # Once per idle stretch, across processes: keyed by the agent's last activity. Claimed only
            # when there is something to report, so a post the agent cannot see (sealed) burns nothing.
            if unread and self._claim(self.IDLE_KEY + agent, "never" if last is None else repr(last),
                                      only_if_changed=True):
                parts.append(f"{agent} has {unread} unread post(s) addressed to it")
                nudged.append(agent)
        if not parts:
            return None
        return Notification(TITLE, f"idle {idle_minutes}+ min · thread {post['thread_id']}", "; ".join(parts),
                            agents=tuple(nudged), thread_ids=(post["thread_id"],), kind="idle-agent")

    # ------------------------------------------------------------ rate gate + coalescing

    def _gate(self):
        if self._gate_conn is None:
            import sqlite3
            # timeout=0: if another process holds the write lock we coalesce instead of waiting.
            self._gate_conn = sqlite3.connect(self.board.s.db_path, timeout=0, isolation_level=None,
                                              check_same_thread=False)
        return self._gate_conn

    def _claim(self, key: str, value: str, only_if_changed: bool = False) -> bool:
        """Atomic cross-process test-and-set in board_state. False when denied or the DB is busy."""
        now = self.board.now()
        cond = ("board_state.value != excluded.value" if only_if_changed
                else "CAST(board_state.value AS REAL) <= ?")
        args: tuple = (key, value, now) if only_if_changed else (key, value, now, now - self.min_interval)
        try:
            with self._lock:
                cur = self._gate().execute(
                    f"""INSERT INTO board_state(key, value, updated_by, updated_at) VALUES (?, ?, 'notifier', ?)
                        ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
                        WHERE {cond}""", args)
            return cur.rowcount == 1
        except Exception:
            log.debug("notify gate unavailable", exc_info=True)
            return False

    def _flush_delay(self) -> float:
        """Seconds until the shared rate window (last send + min_interval) ends, never below a small floor."""
        remaining = self.min_interval
        try:
            with self._lock:
                row = self._gate().execute("SELECT value FROM board_state WHERE key = ?", (self.GATE_KEY,)).fetchone()
            if row is not None:
                remaining = float(row[0]) + self.min_interval - self.board.now()
        except Exception:
            log.debug("notify gate unreadable; using the full window", exc_info=True)
        return max(MIN_FLUSH_DELAY, min(remaining, self.min_interval))

    def _submit(self, n: Notification) -> None:
        with self._lock:
            queued = bool(self._pending.items)
        if not queued and self._claim(self.GATE_KEY, repr(self.board.now())):
            self._deliver(n)
            return
        with self._lock:
            self._pending.items.append(n)
            if self._pending.scheduled:
                return
            self._pending.scheduled = True
        self.schedule(self._flush_delay(), self.flush)

    def flush(self) -> None:
        """Deliver whatever was coalesced during the window (one notification). Called by the timer."""
        try:
            with self._lock:
                self._pending.scheduled = False
                if not self._pending.items:
                    return
            if not self._claim(self.GATE_KEY, repr(self.board.now())):
                with self._lock:
                    self._pending.attempts += 1
                    if self._pending.attempts >= self.max_flush_attempts:
                        self._pending = _Pending()  # another process notified recently; give up quietly
                        return
                    self._pending.scheduled = True
                self.schedule(self._flush_delay(), self.flush)
                return
            with self._lock:
                items, self._pending = self._pending.items, _Pending()
            self._deliver(items[0] if len(items) == 1 else coalesce(items))
        except Exception:
            log.debug("notifier flush failed", exc_info=True)

    def _deliver(self, n: Notification) -> None:
        try:
            self.deliverer(n)
        except Exception:
            log.debug("notification delivery failed", exc_info=True)


def coalesce(items: list[Notification]) -> Notification:
    agents = sorted({a for n in items for a in n.agents})
    threads = sorted({t for n in items for t in n.thread_ids})
    return Notification(TITLE, f"{len(items)} board items need you",
                        f"from {', '.join(agents)} in thread(s) {', '.join(map(str, threads))}",
                        agents=tuple(agents), thread_ids=tuple(threads), kind="coalesced")
