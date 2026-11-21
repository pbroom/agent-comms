"""`board node`'s hosted sessions: the node side of hosted.py, driving session_host.SessionHost.

The node's run loop (node.py) sleeps `poll_seconds` between passes, far too long for a message to start a turn
"at once". Sessions therefore have their own loop, on their own thread, and only that thread touches the SessionHost
(which stays single-threaded):

- Nothing to do: one long-poll of `GET /api/nodes/sessions?wait=20&seen=<version>`. The hub answers as soon as
  anything this node acts on changes (a new message, a new session, a close), so an idle session's message is written
  to its process within a fraction of a second.
- Something in flight: `SessionHost.tick()` every TICK_SECONDS and a plain fetch every SYNC_SECONDS.
- What the host reports (`HostEvent`s) is posted to the hub in order, per session; a report the hub cannot take now
  is kept and sent again. The hub applies reports idempotently.

Rules the hub relies on:
- One message at a time. The hub holds the queue; this loop hands the host a session's next message only once the one
  before it is over. A message waits at the hub while its session's process cannot start (`max_live` session
  processes are up and none is idle), and nothing running is ever stopped to make room: only an idle process is.
- Never twice. The number of the last message handed to each session is on disk before the host gets it
  (HANDED_FILE). A message at or below that number which the hub still lists and the host no longer holds was lost
  (the node restarted): it is reported failed, never written again. That number says nothing else: in particular
  not whether the provider's session exists.
- Start-up: `SessionHost.recover()` first (stop what an earlier node process left), then the hub's sessions for this
  node are opened again, resuming exactly those the provider confirmed (the hub's `resume`, or the last host's
  record), and a turn that was in flight is reported failed. Where that guess is wrong the host corrects itself
  after one refused start (session_host._correct_start_mode).
- A hub call failed (unreachable, paused, an error): turns already running go on, but nothing new is handed over
  until a fetch has succeeded again, because the queue and the stop flags this node holds may be out of date.
  No hub for `self_stop_seconds` (node.SELF_STOP_SECONDS): turns in flight are stopped and reported failed once the
  hub answers; idle processes stay.
- A session the hub no longer lists is closed here. Shutdown stops every process (`stop_all`); the sessions go on
  at the next start. From the moment shutdown begins, shutdown alone reports: the loop posts nothing more.
- `[dispatch.sessions]`: `idle_hours` (a process idle that long is stopped; its session stays and the next message
  resumes it) and `max_live`.
- A provider's own id for a session (Codex's thread id) is the host's to keep (session_host.PROVIDER_FILE). This
  loop reports it to the hub with the status, for the record and the human, and never takes one from the hub.
- Child sessions (hosted_tree.py). A hub that has them says more about a session: an `access` word (None on a
  root: "as your runner has it"), and for a child its `parent`, `isolation` and `effort`. The host turns the word
  into flags from this machine's runner and settings (session_access.prepare) and this loop reports the access the
  session really has. A child with `isolation: worktree` runs in a git worktree of its own
  (session_worktrees.py, recorded in WORKTREES_FILE), made when it is first opened and removed once the session has
  ended and it holds nothing unique. A hub that says none of this gets none of it: the session is opened and
  reported exactly as before.
- This node says it speaks that protocol on every fetch (`tree=1`), and only then does the hub list children to it.
- A child runs only under its parent AS THE PARENT IS HERE NOW. Roots are reconciled first; a child is opened, and
  handed a turn, only while its parent is open on this node and has at least the child's access (`_parent_problem`).
  The hub's stored words are not enough: this machine's runner may have been edited since. Otherwise the child is
  ended with the reason.
- `full` under a parent needs the parent's LIVE process to have said it runs that way (session_host: `ceiling`).
  While the parent has no process, one that is going down, or a new one that has not said so, a `full` child is
  held: it stays open, its messages stay queued at the hub, and no turn is handed to it. That is decided from the
  parent's process as it is at the moment the child is considered, so a parent that is about to be started in a
  pass never lends an earlier process's word to a child in that pass. The hub is told the ceiling with every
  status, and refuses to make such a child meanwhile. A parent whose process a waiting or working full child
  depends on is not stopped to make room, nor for being idle (`_lends_full`).
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable

import httpx

from . import session_access, session_worktrees
from .dispatch import DispatchConfig
from .session_host import HostEvent, SessionHost

log = logging.getLogger("agent_comms.node_sessions")

TICK_SECONDS = 0.2          # between passes while any session has work
SYNC_SECONDS = 1.0          # between fetches of the hub's list while any session has work
LONG_POLL_SECONDS = 20      # how long one fetch may wait at the hub when nothing has work
RETRY_SECONDS = 2.0         # after a hub call failed, with nothing else to do
JOIN_SECONDS = 1.0          # shutdown waits this long for a pass in progress; a loop waiting at the hub is not waited for
REPORT_WAIT_SECONDS = 25.0  # ... and this long for a report the loop is in the middle of posting (CALL_TIMEOUT is 20 s)
MAX_BATCH = 100             # events in one report
HANDED_FILE = "node-session-handed.json"   # session id -> number of the last message handed to its host
SEQ_FILE = "node-session-seq.json"         # {"seq": the highest report stamp this node has issued}
WORKTREES_FILE = "node-session-worktrees.json"   # session id -> {path, checkout}: a child session's own worktree
SWEEP_SECONDS = 3600.0      # how often the worktrees of sessions that are gone are looked at again
PARENT_ENDED = "its parent session ended"
PARENT_GONE = ("its parent session is not open on this node (it could not be opened here, or has ended): a child "
               "session runs only under its parent")
PARENT_UNKNOWN = "its parent session's access is not known on this node"
ABOVE_PARENT = ("its access ({have}) is above what its parent session has on this node now ({limit}): a child session "
                "never has more access than its parent")
LOST = "the node restarted while this turn was in flight; it was not replayed"
FRESH_START = (". Before that the agent refused to resume this session's conversation: the next message starts a NEW "
               "one, without the earlier turns")


class SessionRunner:
    def __init__(self, client: httpx.Client, config: DispatchConfig, log_dir: Path, *, host: SessionHost | None = None,
                 clock: Callable[[], float] = time.monotonic, self_stop_seconds: float = 90.0,
                 long_poll: float = LONG_POLL_SECONDS, sync_seconds: float = SYNC_SECONDS, **host_kw):
        self.client, self.log_dir, self.clock = client, Path(log_dir), clock
        self.config = config
        self.host = host or SessionHost(config, self.log_dir, clock=clock, **host_kw)
        self.host.turn_gate = self._turn_allowed      # a child's turn is held to its parent when it is WRITTEN, too
        # session id -> {access, worktree}: what a hub with child sessions is also told about it (never an older one)
        self.tree: dict[str, dict] = {}
        self.parent_of: dict[str, str] = {}           # child session id -> its parent's, as the hub listed it
        self.worktrees: dict[str, dict] = self._load_worktrees()
        self.swept = float("-inf")
        self.max_live, self.idle_seconds = config.sessions.max_live, config.sessions.idle_hours * 3600
        self.self_stop_seconds, self.long_poll, self.sync_seconds = self_stop_seconds, long_poll, sync_seconds
        self.pending: list[tuple[str, dict]] = []     # (session id, event) not yet taken by the hub, oldest first
        self.queue: dict[str, list[dict]] = {}        # session id -> the hub's queued messages not yet handed over
        self.handed: dict[str, int] = self._load_handed()
        self.ending: set[str] = set()                 # sessions this node reported (or is reporting) ended
        self.dropped: set[str] = set()                # sessions the hub no longer lists: closed here, not reported
        # session id -> (state, pid, resume, the provider's own id when it is not the session id) as of the last pass
        self.status: dict[str, tuple] = {}
        self.reported: dict[str, tuple] = {}          # ... as the hub last took it
        self.last_busy: dict[str, float] = {}         # session id -> when it last had a turn or a message
        self.recovered: dict[str, dict] = {}
        self.version: str | None = None
        self.last_sync = float("-inf")
        self.retry_at = float("-inf")
        self.fresh = False                            # the last hub call was a fetch that succeeded: `queue` is current
        self.last_contact = clock()
        self.lock = threading.Lock()                  # the host, between the loop and shutdown()
        self.report_lock = threading.Lock()           # the report queue: one flush at a time, shutdown's last
        self.seq = self._load_seq()                   # the last report's stamp (`_stamp`), an earlier process's included
        self.seq_unsaved = False                      # the last attempt to put it on disk failed (said once)
        self.hub_seq = 0                              # the highest stamp the hub says it applied, for any session
        self.sent: dict[str, int] = {}                # session id -> stamp of this process's last status the hub took
        self.stopping = threading.Event()
        self.thread: threading.Thread | None = None

    # ------------------------------------------------------------ local state (survives a node restart)

    def _handed_path(self) -> Path:
        return self.log_dir / HANDED_FILE

    def _load_handed(self) -> dict[str, int]:
        try:
            data = json.loads(self._handed_path().read_text())
        except (OSError, ValueError):
            return {}
        return {k: v for k, v in data.items() if isinstance(k, str) and type(v) is int} if isinstance(data, dict) else {}

    def _write_private(self, path: Path, data: Any) -> None:
        """Atomically, mode 600 (as session_host's state file)."""
        self.log_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp = path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            os.fchmod(fd, 0o600)
            os.write(fd, json.dumps(data).encode())
        finally:
            os.close(fd)
        os.replace(tmp, path)

    def _save_handed(self) -> None:
        self._write_private(self._handed_path(), self.handed)

    def _load_worktrees(self) -> dict[str, dict]:
        try:
            data = json.loads((self.log_dir / WORKTREES_FILE).read_text())
        except (OSError, ValueError):
            return {}
        if not isinstance(data, dict):
            return {}
        return {k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, dict)
                and isinstance(v.get("path"), str) and isinstance(v.get("checkout"), str)}

    def _save_worktrees(self) -> None:
        try:
            self._write_private(self.log_dir / WORKTREES_FILE, self.worktrees)
        except OSError as e:
            log.warning("could not record the session worktrees in %s (%s)", WORKTREES_FILE, e)

    def _load_seq(self) -> int:
        try:
            seq = json.loads((self.log_dir / SEQ_FILE).read_text()).get("seq")
        except (OSError, ValueError, AttributeError):
            return 0
        return seq if type(seq) is int and seq > 0 else 0

    def recover(self) -> None:
        """Once, before any hub contact: stop the session processes an earlier node process left here."""
        self.recovered = self.host.recover()
        for sid, d in self.recovered.items():
            turn = d.get("turn")
            if isinstance(turn, str) and turn.isdigit() and int(turn) > self.handed.get(sid, 0):
                self.handed[sid] = int(turn)
                self._save_handed()

    # ------------------------------------------------------------ hub calls

    def _call(self, method: str, path: str, **kw) -> Any:
        r = self.client.request(method, path, **kw)
        r.raise_for_status()
        self.last_contact = self.clock()
        return r.json()

    def _add(self, session_id: str, event: dict) -> None:
        if session_id not in self.dropped:
            self.pending.append((session_id, event))

    @staticmethod
    def _event(e: HostEvent) -> dict:
        """A HostEvent as the hub takes it (hosted.report). Message ids are the hub's message numbers."""
        d: dict[str, Any] = {"kind": e.kind}
        if e.message_id is not None and e.message_id.isdigit():
            d["message"] = int(e.message_id)
        if e.kind == "turn_ended":
            d |= {"ok": bool(e.ok), "delivered": bool(e.delivered), "detail": e.detail[:300],
                  "denials": list(e.denials), "meta": dict(e.meta), "text": e.text}
        elif e.kind == "approval_requested":
            d["denials"] = list(e.denials)
        elif e.kind == "ended":
            d["detail"] = e.detail[:300]
        return d

    def _emit(self, events: list[HostEvent]) -> None:
        for e in events:
            if e.kind == "ended":
                self.ending.add(e.session_id)
                self.queue.pop(e.session_id, None)
            self._add(e.session_id, self._event(e))

    def _flush(self, final: bool = False) -> bool:
        """Post what the hub has not taken yet, per session and in order. False: the hub could not be reached (the
        rest is kept). A report the hub refuses for good (it does not know the session, or cannot read the report)
        is dropped, so one bad event never holds the others back. Once shutdown has begun only shutdown's own flush
        (`final`) posts: the loop's would race it, and an older status sent after shutdown's would bring a dead pid
        back to life on the hub."""
        with self.report_lock:
            if self.stopping.is_set() and not final:
                return False
            return self._post_pending()

    wall_ns = staticmethod(time.time_ns)

    def _stamp(self) -> int:
        """An id for the next report that is above every earlier one of this node: the hub ignores the status of a
        report that is not newer than the last it applied, so one that arrives late (its call timed out here, then it
        committed there) cannot undo a later one. Above the last stamp issued, the wall clock, and the highest stamp
        the hub says it applied (it comes with every fetch).

        The last stamp issued is on disk (SEQ_FILE) before the report that carries it is sent, and the next node
        process starts from it. So whatever the clock did across a restart, the new process never issues a stamp an
        old one issued, nor one below it: an old report can neither tie with a new one and win by arriving first, nor
        overwrite the final report of a shutdown after the node has stopped fetching. If the file cannot be written
        the report goes out all the same (the hub's floor still orders what the hub has applied)."""
        self.seq = max(self.seq + 1, self.wall_ns(), self.hub_seq + 1)
        try:
            self._write_private(self.log_dir / SEQ_FILE, {"seq": self.seq})
            self.seq_unsaved = False
        except OSError as e:
            if not self.seq_unsaved:
                log.warning("could not record the report stamp in %s (%s); reporting goes on", SEQ_FILE, e)
            self.seq_unsaved = True
        return self.seq

    def _post_pending(self) -> bool:
        order: list[str] = []
        for sid, _ in self.pending:
            if sid not in order:
                order.append(sid)
        order += [sid for sid, st in self.status.items() if sid not in order and self.reported.get(sid) != st]
        for sid in order:
            events = [e for s, e in self.pending if s == sid][:MAX_BATCH]
            body: dict[str, Any] = {"events": events, "seq": self._stamp()}
            status = self.status.get(sid)
            if status is not None:
                body |= {"state": status[0], "pid": status[1], "resume": status[2]}
                if status[3] is not None:
                    # Codex's thread id: for the hub's record, never read back. (A hub from before the field refuses
                    # the report; hubs are upgraded before nodes.)
                    body["provider_id"] = status[3]
                # The access the session really has here and a child's own worktree, for a hub that asked.
                body |= {k: v for k, v in self.tree.get(sid, {}).items() if v is not None}
            try:
                self._call("POST", f"/api/nodes/sessions/{sid}", json=body)
            except Exception as e:
                code = getattr(getattr(e, "response", None), "status_code", None)
                if code not in (400, 404, 422):
                    log.warning("could not report session %s: %s", sid, e)
                    return False
                log.warning("hub refused the report for session %s (%s); dropping it", sid, code)
            if status is not None:
                self.reported[sid] = status
                self.sent[sid] = body["seq"]
            taken = len(events)
            kept = []
            for item in self.pending:       # drop exactly the events that were sent, keep their order
                if item[0] == sid and taken:
                    taken -= 1
                else:
                    kept.append(item)
            self.pending = kept
        return True

    def _sync(self, wait: float) -> dict:
        params: dict[str, Any] = {"wait": wait, "tree": 1}     # this node speaks the child-session protocol
        if wait and self.version:
            params["seen"] = self.version
        if not wait:
            return self._call("GET", "/api/nodes/sessions", params=params)
        return self._call("GET", "/api/nodes/sessions", params=params, timeout=httpx.Timeout(wait + 20, connect=5))

    # ------------------------------------------------------------ one pass

    def active(self) -> bool:
        """Something needs the short cadence: a turn, a message to hand over, a process going down, a report."""
        return bool(self.pending or any(self.queue.values())
                    or any(self.reported.get(sid) != st for sid, st in self.status.items())
                    or any(s.busy or s.inbox or s.ended or (s.child is not None and s.stopping_since is not None)
                           for s in self.host.sessions.values()))

    def pass_once(self, wait: float | None = None) -> bool:
        """One pass: report, fetch the hub's list (waiting there when nothing has work), reconcile, hand over the next
        messages, tick the host, report. Returns False when the hub could not be reached; the host is ticked and held
        to its timeouts either way."""
        reach = self.clock() >= self.retry_at     # after a call failed, the hub is left alone for RETRY_SECONDS
        ok = reach and self._flush()
        if wait is None:
            wait = self.long_poll if ok and not self.active() else 0
        data = None
        if ok and (wait or not self.fresh or self.clock() - self.last_sync >= self.sync_seconds):
            try:
                data = self._sync(wait)
                self.last_sync = self.clock()
            except Exception as e:
                log.warning("could not fetch hosted sessions: %s", e)
                ok = False
        if data is not None:
            self.fresh = True
        elif not ok:
            # The queue and the stop flags held here may be out of date (the human may just have closed a session):
            # turns already running go on, nothing new is handed over until a fetch has succeeded again.
            self.fresh = False
        with self.lock:
            if self.stopping.is_set():
                return ok
            now = self.clock()
            if data is not None:
                self._reconcile(data)
            if now - self.last_contact >= self.self_stop_seconds:
                self._emit(self.host.stop_turns(
                    f"the node had no hub contact for {self.self_stop_seconds:g} seconds and stopped the turn"))
            if self.fresh:
                self._hand_over(now)
            self._park_idle(now)
            events = self.host.tick()
            self._emit(events)
            if self.fresh and any(e.kind == "turn_ended" for e in events):
                self._hand_over(now)        # the message behind a turn that just ended is written in this same pass
                self._emit(self.host.tick())
            self._after_tick(now)
        if ok and (self.pending or any(self.reported.get(sid) != st for sid, st in self.status.items())):
            ok = self._flush()
        if reach and not ok:
            self.fresh = False
            self.retry_at = self.clock() + RETRY_SECONDS
        return ok

    def _held(self, s) -> set[int]:
        ids = [m.id for m in s.inbox] + [m.id for m in (s.writing, s.in_flight) if m is not None]
        return {int(i) for i in ids if i.isdigit()}

    def _reconcile(self, data: dict) -> None:
        if not isinstance(data, dict) or not isinstance(data.get("sessions"), list):
            log.warning("the hub's session list is not one; ignoring it")     # never read as "no sessions"
            return
        listed = {v["session_id"]: v for v in data["sessions"]
                  if isinstance(v, dict) and isinstance(v.get("session_id"), str)}
        # Roots first: a child is only opened under a parent that is open here (`_parent_problem`).
        for sid, view in sorted(listed.items(), key=lambda kv: isinstance(kv[1].get("parent"), str)):
            applied = view.get("status_seq")
            if type(applied) is int:
                self.hub_seq = max(self.hub_seq, applied)
                if applied > self.sent.get(sid, applied):
                    # The hub applied a status that is not this process's last one (an earlier node process's
                    # report arrived late, with a stamp from a clock that ran ahead): say again how it stands.
                    self.reported.pop(sid, None)
            s = self.host.sessions.get(sid)
            if sid in self.ending:
                if not any(x == sid and e["kind"] == "ended" for x, e in self.pending):
                    self._add(sid, {"kind": "ended", "detail": "the session ended on its node"})
                continue
            if view.get("stop"):
                why = PARENT_ENDED if view.get("stop") == "parent" else "closed by the human"
                if s is not None:
                    self._emit(self.host.close(sid, why))
                else:
                    self.ending.add(sid)
                    self._add(sid, {"kind": "ended", "detail": why})
                continue
            parent = view.get("parent") if isinstance(view.get("parent"), str) else None
            if parent is not None:
                self.parent_of[sid] = parent
                if self._parent_problem(parent, None) is not None:     # before anything is opened or made for it
                    self._end_child(sid, self._parent_problem(parent, None))
                    continue
            if s is None:
                left = self.recovered.pop(sid, None) or {}
                # Resume only a session the provider confirmed: the hub's record of it (the host's own `resume`,
                # reported with every status) or the last host's state file. That a message was handed over, or
                # even written, proves nothing: Claude refuses `--resume` for a conversation that does not exist
                # just as it refuses `--session-id` for one that does. A wrong guess heals in the host after one
                # refused start (session_host._correct_start_mode). For a provider with an id of its own (Codex) the
                # host's own record overrules this flag either way (`SessionHost.open`): a recorded id is resumed,
                # and a recorded fresh start is not, even when the hub (which may not have heard) says resume.
                resume = view.get("resume") is True or left.get("resume") is True
                try:
                    s = self.host.open(sid, agent=view.get("agent"), project=view.get("project"),
                                       runtime=view.get("runtime"), model=view.get("model"), resume=resume,
                                       **self._tree_open(sid, view))
                except Exception as e:
                    log.warning("cannot host session %s: %s", sid, e)
                    self.ending.add(sid)
                    self._add(sid, {"kind": "ended",
                                    "detail": f"this node cannot host the session: {type(e).__name__}: {e}"[:300]})
                    continue
                self.last_busy[sid] = self.clock()
            if parent is not None and self._parent_problem(parent, s) is not None:
                self._end_child(sid, self._parent_problem(parent, s))   # the access it has here is above its parent's
                continue
            self._tree_status(sid, view, s)
            held, queue = self._held(s), []
            for m in sorted((m for m in view.get("messages", []) if isinstance(m, dict) and type(m.get("n")) is int),
                            key=lambda m: m["n"]):
                n = m["n"]
                if n > self.handed.get(sid, 0) and m.get("state") == "queued":
                    queue.append(m)
                elif n not in held and not any(x == sid and e.get("message") == n for x, e in self.pending):
                    # Handed to a host that no longer has it, and no result on its way: the turn was lost.
                    self.handed[sid] = max(self.handed.get(sid, 0), n)
                    self._save_handed()
                    # If the host found its own record that the provider refused to resume this session (the node
                    # died before that failure reached the hub), the human is told here what follows from it.
                    self._add(sid, {"kind": "turn_ended", "message": n, "ok": False,
                                    "detail": LOST + (FRESH_START if s.fresh_start else ""),
                                    "delivered": m.get("state") == "delivered"})
            self.queue[sid] = queue
        for sid, s in self.host.sessions.items():
            if sid not in listed and not s.ended:
                # The hub ended it (closed while this node was away, or its record is gone): it must not go on.
                self.dropped.add(sid)
                self.status.pop(sid, None)
                self.reported.pop(sid, None)
                self.host.close(sid, "the hub no longer lists this session")
        for sid in [x for x in self.queue if x not in listed]:
            del self.queue[sid]
        self.ending &= set(listed)
        self.pending = [(sid, e) for sid, e in self.pending if sid not in self.dropped]
        if set(self.handed) - set(listed):
            self.handed = {sid: n for sid, n in self.handed.items() if sid in listed}
            self._save_handed()
        self.host.keep_provider_ids(set(listed))
        if self.worktrees and self.clock() - self.swept >= SWEEP_SECONDS:
            # Worktrees of child sessions that are gone here (ended while this node was away, or kept because they
            # held something): each is removed once it holds nothing unique.
            self.swept = self.clock()
            for sid in [x for x in self.worktrees if x not in listed and x not in self.host.sessions]:
                self._drop_worktree(sid)
        self.version = data.get("version") if isinstance(data.get("version"), str) else None

    # ------------------------------------------------------------ child sessions (hosted_tree.py)

    def _tree_open(self, sid: str, view: dict) -> dict:
        """What a hub with child sessions adds to opening a session: its access word and effort, and for a child
        with its own worktree that directory, made now if it is not there. Nothing for an entry without the keys:
        the session is then opened exactly as before."""
        if "access" not in view and "parent" not in view:
            return {}
        kw: dict[str, Any] = {"access": view.get("access"), "effort": view.get("effort")}
        isolation = view.get("isolation") if "parent" in view else None
        if isolation == "worktree":
            checkout = self.config.checkout_for(view.get("project"))
            if not checkout:
                raise FileNotFoundError(f"this machine has no run directory for project {view.get('project')!r}")
            path = session_worktrees.path_for(self.config, checkout, sid)
            known = sid in self.worktrees
            self.worktrees[sid] = {"path": path, "checkout": checkout}
            self._save_worktrees()      # on record before it exists: a crash in between leaves nothing unknown
            try:
                kw["cwd"] = session_worktrees.ensure(self.config, checkout, sid)
            except Exception:
                if not known:
                    del self.worktrees[sid]
                    self._save_worktrees()
                raise
        elif isolation not in (None, "shared"):
            raise ValueError("unknown isolation for a child session")
        return kw

    def _parent_problem(self, parent_id: str, s) -> str | None:
        """Why a child may not be opened or take a turn under its parent as the parent is on this node NOW, or None.
        `s`: the child once it is open; its access here is then held against the parent's. Both are computed from
        the command lines this machine's runners give today (session_host): valid before either process has said
        anything, and after any restart or change of a runner."""
        parent = self.host.sessions.get(parent_id)
        if parent is None or parent.ended or parent_id in self.ending or parent_id in self.dropped:
            return PARENT_GONE
        if s is None:
            return None
        if parent.access not in session_access.LEVELS or s.access not in session_access.LEVELS:
            return PARENT_UNKNOWN
        if not session_access.within(s.access, parent.access):
            return ABOVE_PARENT.format(have=s.access, limit=parent.access)
        return None

    def _awaits_parent(self, parent_id: str, s) -> bool:
        """The child's access is within what its parent was STARTED as, but above what the parent's LIVE process
        has confirmed (`ceiling`: `full` only while the process whose `init`, or whose answer to thread/start, said
        so is the parent's process and is up). It is held, not ended: no turn until then."""
        parent = self.host.sessions.get(parent_id)
        return (parent is not None and s.access in session_access.LEVELS and parent.ceiling in session_access.LEVELS
                and not session_access.within(s.access, parent.ceiling))

    def _end_child(self, sid: str, why: str) -> None:
        """End a child here and tell the hub why: it is not opened, or is closed if it was open."""
        log.warning("child session %s: %s", sid, why)
        if sid in self.host.sessions:
            self._emit(self.host.close(sid, why))
        else:
            self.ending.add(sid)
            self._add(sid, {"kind": "ended", "detail": why[:300]})

    def _tree_status(self, sid: str, view: dict, s) -> None:
        """Whether this session's reports carry its access and worktree: only for a hub that asked. The access
        is the one computed from the session's command line when it was opened (session_host): it is there from the
        first report on."""
        if "access" not in view and "parent" not in view:
            self.tree.pop(sid, None)
            return
        self.tree.setdefault(sid, {})
        self._tree_now(sid, s, root="parent" not in view)

    def _tree_now(self, sid: str, s, root: bool | None = None) -> None:
        """What this session's reports carry beyond its state, as it is NOW (called on every fetch and after every
        tick, for a session whose hub asked): its access; for a root also its `ceiling` (the most a child may have
        under it now: `full` only while its live process has confirmed it) and its `checkout` (where it runs, which
        a runner that names its own directory is held against when a child is asked for); a child's own worktree."""
        if sid not in self.tree:
            return
        root = sid not in self.parent_of if root is None else root
        tree = {"access": s.access, "worktree": self.worktrees.get(sid, {}).get("path"),
                "ceiling": s.ceiling if root else None, "checkout": s.cwd if root else None}
        if self.tree.get(sid) != tree:
            self.tree[sid] = tree
            self.reported.pop(sid, None)        # say how it stands again, with these

    def _drop_worktree(self, sid: str) -> None:
        """The session is over here: remove its own worktree if nothing unique is in it, and tell the hub. One that
        holds uncommitted work, or commits on no branch, stays where it is and is looked at again later."""
        w = self.worktrees.get(sid)
        if w is None:
            return
        try:
            outcome = session_worktrees.remove_if_safe(w["checkout"], w["path"])
        except Exception:
            log.exception("session %s: could not look at its worktree", sid)
            outcome = "kept"
        if outcome == "kept":
            log.info("session %s: its worktree %s is kept (uncommitted work, or commits on no branch)", sid, w["path"])
            return
        del self.worktrees[sid]
        self._save_worktrees()
        self._add(sid, {"kind": "worktree_removed"})

    def _turn_allowed(self, s) -> bool:
        """The host asks this as it is about to write a turn. A message is handed to a child only under a parent
        that allows it (`_hand_over`), but it can wait in the child's inbox for a handshake, and the parent's
        process can go in that time: so the same is asked again here, of the parent as it is now. False leaves the
        message where it is; a child whose parent is gone is ended at the next fetch."""
        parent_id = self.parent_of.get(s.session_id)
        if parent_id is None:
            return True
        parent = self.host.sessions.get(parent_id)
        return parent is not None and not parent.ended and not self._awaits_parent(parent_id, s)

    def _lends_full(self, sid: str) -> bool:
        """This session's live process is what lets a `full` child of it take turns, and such a child has a turn,
        a message, or one queued at the hub. Stopping that process would hold the child until the parent's next
        turn, so it is not stopped to make room or for being idle."""
        parent = self.host.sessions.get(sid)
        if parent is None or parent.ceiling != "full":
            return False
        for kid, pid in self.parent_of.items():
            k = self.host.sessions.get(kid)
            if (pid == sid and k is not None and not k.ended and k.access == "full"
                    and (k.busy or k.inbox or self.queue.get(kid))):
                return True
        return False

    def _admit(self, s) -> bool:
        """May this session's process start now? Not while `max_live` processes are up: then the longest-idle one is
        stopped (one at a time) and the message waits; with none idle it just waits."""
        if s.child is not None:
            return True
        live = [x for x in self.host.sessions.values() if x.child is not None or x.inbox]
        if len(live) < self.max_live:
            return True
        if not any(x.child is not None and x.stopping_since is not None for x in live):
            # (Not one whose provider runs a turn of its own in it: by the host's books it is idle, but it is working.)
            idle = [x for x in live if x.child is not None and not x.busy and not x.inbox and not x.ended
                    and not self.host.holding(x) and not self._lends_full(x.session_id)]
            if idle:
                self.host.park(min(idle, key=lambda x: self.last_busy.get(x.session_id, 0.0)).session_id)
        return False

    def _hand_over(self, now: float) -> None:
        for sid, queue in list(self.queue.items()):     # ending a child here drops its queue
            s = self.host.sessions.get(sid)
            if (not queue or s is None or s.ended or s.busy or s.inbox or s.stopping_since is not None
                    or not isinstance(queue[0].get("text"), str)):
                continue
            if sid in self.parent_of and self._parent_problem(self.parent_of[sid], s) is not None:
                # Not a turn under a parent that is gone here, or that has less access now than this child.
                self._end_child(sid, self._parent_problem(self.parent_of[sid], s))
                continue
            if sid in self.parent_of and self._awaits_parent(self.parent_of[sid], s):
                continue        # a full child whose parent's process has not confirmed full access (yet, or again)
            if not self._admit(s):
                continue
            m = queue.pop(0)
            self.handed[sid] = m["n"]
            self._save_handed()         # on disk first: after a crash this message is lost, never written twice
            try:
                self.host.send(sid, m["text"], message_id=str(m["n"]))
            except ValueError as e:
                self._add(sid, {"kind": "turn_ended", "message": m["n"], "ok": False, "delivered": False,
                                "detail": f"the node refused the message: {e}"[:300]})
            self.last_busy[sid] = now

    def _park_idle(self, now: float) -> None:
        for sid, s in self.host.sessions.items():
            if s.busy or s.inbox or self._lends_full(sid):
                self.last_busy[sid] = now
            elif s.child is not None and now - self.last_busy.setdefault(sid, now) >= self.idle_seconds:
                if self.host.park(sid):
                    log.info("session %s: idle for %g hours, its process was stopped", sid, self.idle_seconds / 3600)

    def _after_tick(self, now: float) -> None:
        for sid, s in list(self.host.sessions.items()):
            if s.busy or s.inbox:
                self.last_busy[sid] = now
            if s.ended and s.child is None:
                self.host.forget(sid)
                for d in (self.status, self.reported, self.last_busy, self.queue, self.sent, self.tree,
                          self.parent_of):
                    d.pop(sid, None)
                self._drop_worktree(sid)        # a child's own worktree, now that its process is gone
                self.dropped.discard(sid)
            elif not s.ended and sid not in self.dropped:
                self.status[sid] = (s.state, getattr(s.child, "pid", None), bool(s.resume), self._own_id(s))
                self._tree_now(sid, s)      # a root's ceiling changes when its process confirms full access

    @staticmethod
    def _own_id(s) -> str | None:
        """The provider's id for the session when it issued one of its own (Codex's thread id). Claude's is the
        session id, which the hub has: nothing is added to its reports."""
        return s.provider_id if s.provider_id != s.session_id else None

    # ------------------------------------------------------------ the thread

    def run_forever(self) -> None:
        while not self.stopping.is_set():
            try:
                ok = self.pass_once()
            except Exception:
                log.exception("hosted sessions: a pass failed")
                ok = False
            if self.active():
                self.stopping.wait(TICK_SECONDS)
            elif not ok:
                self.stopping.wait(RETRY_SECONDS)   # otherwise the next pass waits at the hub (long-poll)

    def start(self) -> None:
        self.thread = threading.Thread(target=self.run_forever, name="hosted-sessions", daemon=True)
        self.thread.start()

    def signal(self) -> None:
        """The first step of a shutdown: SIGTERM to every session process, now. No waiting and no reporting, so the
        node can go on to stop its ordinary runs at once (node.run); `shutdown()` does the rest."""
        self.stopping.set()
        with self.lock:
            for s in self.host.sessions.values():
                if s.child is not None:
                    try:
                        s.child.terminate()
                    except Exception:
                        log.exception("could not signal session %s", s.session_id)

    def _going(self, s) -> bool:
        return s.child is not None and (s.child.poll() is None or self.host.group_alive(s.child.pid))

    def alive(self) -> bool:
        """After `signal()`: is any session process, or anything in its process group, still there?"""
        with self.lock:
            return any(self._going(s) for s in self.host.sessions.values())

    def kill(self) -> None:
        """After `signal()` and the grace period: SIGKILL for every session process group that is still there. No
        reporting; `shutdown()` then finds them gone."""
        with self.lock:
            for s in self.host.sessions.values():
                if self._going(s):
                    s.child.kill()

    def shutdown(self) -> None:
        """Stop the loop and every session process; turns in flight are reported failed. The loop may be waiting at
        the hub: it is given JOIN_SECONDS, then the host is taken from it (it checks `stopping` before touching the
        host again). From `stopping` on the loop posts nothing (`_flush`), so what it had not reported yet and what
        the shutdown adds go to the hub from here, in order, as one queue."""
        self.stopping.set()
        if self.thread is not None:
            self.thread.join(JOIN_SECONDS)
        with self.lock:
            # stop_all reports the turn in flight and says nothing of a message still in a session's inbox. Such a
            # message never reached a process, so it is given back: the hub still lists it as queued, and with its
            # number no longer recorded as handed the next node process delivers it (once) instead of failing it.
            back = {sid: min(int(m.id) for m in s.inbox if m.id.isdigit()) - 1
                    for sid, s in self.host.sessions.items() if any(m.id.isdigit() for m in s.inbox)}
            for sid, n in back.items():
                self.host.sessions[sid].inbox.clear()
                self.handed[sid] = min(self.handed.get(sid, n), n)
            if back:
                self._save_handed()
            events = self.host.stop_all()
        # A flush the loop began before `stopping` may still be on its way: wait for it (a hub call is bounded by the
        # client's timeout) rather than post beside it. If it never returns, nothing is reported from here and the
        # next node process reports the lost turns.
        if not self.report_lock.acquire(timeout=REPORT_WAIT_SECONDS):
            log.warning("a report to the hub is still under way; the session shutdown is not reported")
            return
        self.report_lock.release()
        with self.lock:             # the loop is past its last pass: `pending` and `status` are shutdown's now
            self._emit(events)
            self.status = {sid: (s.state, None, bool(s.resume), self._own_id(s)) for sid, s in self.host.sessions.items()
                           if not s.ended and sid not in self.dropped}
            for sid, s in self.host.sessions.items():
                self._tree_now(sid, s)      # no process is left to have confirmed anything: the ceiling says so
        try:
            if not self._flush(final=True):
                log.warning("could not report the session shutdown")
        except Exception as e:
            log.warning("could not report the session shutdown: %s", e)
