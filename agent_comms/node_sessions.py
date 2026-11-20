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
LOST = "the node restarted while this turn was in flight; it was not replayed"


class SessionRunner:
    def __init__(self, client: httpx.Client, config: DispatchConfig, log_dir: Path, *, host: SessionHost | None = None,
                 clock: Callable[[], float] = time.monotonic, self_stop_seconds: float = 90.0,
                 long_poll: float = LONG_POLL_SECONDS, sync_seconds: float = SYNC_SECONDS, **host_kw):
        self.client, self.log_dir, self.clock = client, Path(log_dir), clock
        self.host = host or SessionHost(config, self.log_dir, clock=clock, **host_kw)
        self.max_live, self.idle_seconds = config.sessions.max_live, config.sessions.idle_hours * 3600
        self.self_stop_seconds, self.long_poll, self.sync_seconds = self_stop_seconds, long_poll, sync_seconds
        self.pending: list[tuple[str, dict]] = []     # (session id, event) not yet taken by the hub, oldest first
        self.queue: dict[str, list[dict]] = {}        # session id -> the hub's queued messages not yet handed over
        self.handed: dict[str, int] = self._load_handed()
        self.ending: set[str] = set()                 # sessions this node reported (or is reporting) ended
        self.dropped: set[str] = set()                # sessions the hub no longer lists: closed here, not reported
        self.status: dict[str, tuple] = {}            # session id -> (state, pid, resume) as of the last pass
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
        self.seq = 0                                  # the last report's stamp (`_stamp`)
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

    def _save_handed(self) -> None:
        self.log_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp = self._handed_path().with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            os.fchmod(fd, 0o600)
            os.write(fd, json.dumps(self.handed).encode())
        finally:
            os.close(fd)
        os.replace(tmp, self._handed_path())

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
        committed there) cannot undo a later one. Above this process's own last stamp, the wall clock, and the highest
        stamp the hub says it applied (it comes with every fetch): the hub is the source of truth, so a clock that
        was set back across a node restart can neither let an old report win nor get the new process ignored."""
        self.seq = max(self.seq + 1, self.wall_ns(), self.hub_seq + 1)
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
        params: dict[str, Any] = {"wait": wait}
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
        for sid, view in listed.items():
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
                if s is not None:
                    self._emit(self.host.close(sid, "closed by the human"))
                else:
                    self.ending.add(sid)
                    self._add(sid, {"kind": "ended", "detail": "closed by the human"})
                continue
            if s is None:
                left = self.recovered.pop(sid, None) or {}
                # Resume only a session the provider confirmed: the hub's record of it (the host's own `resume`,
                # reported with every status) or the last host's state file. That a message was handed over, or
                # even written, proves nothing: Claude refuses `--resume` for a conversation that does not exist
                # just as it refuses `--session-id` for one that does. A wrong guess heals in the host after one
                # refused start (session_host._correct_start_mode).
                resume = view.get("resume") is True or left.get("resume") is True
                try:
                    s = self.host.open(sid, agent=view.get("agent"), project=view.get("project"),
                                       runtime=view.get("runtime"), model=view.get("model"), resume=resume)
                except Exception as e:
                    log.warning("cannot host session %s: %s", sid, e)
                    self.ending.add(sid)
                    self._add(sid, {"kind": "ended",
                                    "detail": f"this node cannot host the session: {type(e).__name__}: {e}"[:300]})
                    continue
                self.last_busy[sid] = self.clock()
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
                    self._add(sid, {"kind": "turn_ended", "message": n, "ok": False, "detail": LOST,
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
        self.version = data.get("version") if isinstance(data.get("version"), str) else None

    def _admit(self, s) -> bool:
        """May this session's process start now? Not while `max_live` processes are up: then the longest-idle one is
        stopped (one at a time) and the message waits; with none idle it just waits."""
        if s.child is not None:
            return True
        live = [x for x in self.host.sessions.values() if x.child is not None or x.inbox]
        if len(live) < self.max_live:
            return True
        if not any(x.child is not None and x.stopping_since is not None for x in live):
            idle = [x for x in live if x.child is not None and not x.busy and not x.inbox and not x.ended]
            if idle:
                self.host.park(min(idle, key=lambda x: self.last_busy.get(x.session_id, 0.0)).session_id)
        return False

    def _hand_over(self, now: float) -> None:
        for sid, queue in self.queue.items():
            s = self.host.sessions.get(sid)
            if (not queue or s is None or s.ended or s.busy or s.inbox or s.stopping_since is not None
                    or not isinstance(queue[0].get("text"), str) or not self._admit(s)):
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
            if s.busy or s.inbox:
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
                for d in (self.status, self.reported, self.last_busy, self.queue, self.sent):
                    d.pop(sid, None)
                self.dropped.discard(sid)
            elif not s.ended and sid not in self.dropped:
                self.status[sid] = (s.state, getattr(s.child, "pid", None), bool(s.resume))

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
            self.status = {sid: (s.state, None, bool(s.resume)) for sid, s in self.host.sessions.items()
                           if not s.ended and sid not in self.dropped}
        try:
            if not self._flush(final=True):
                log.warning("could not report the session shutdown")
        except Exception as e:
            log.warning("could not report the session shutdown: %s", e)
