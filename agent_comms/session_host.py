"""Session host: agent processes this machine owns, so a message can start a turn in them.

A dispatched run (dispatch.py, node.py) is one process per prompt: it cannot be told anything once it is going. A hosted
session is one long-lived process per conversation that takes turns on its stdin. A message for an idle session starts
a turn on the next pass; a message for a busy one waits in the session's inbox and becomes the next turn. Nothing
running is ever interrupted to make room.

This module is local only (PR 1): no hub registry, no node protocol, no routes, no CLI. A caller opens sessions, sends
messages and drives `tick()`; everything the host learned in a pass comes back as `HostEvent`s.

- One process per session, started from this machine's own runner template for the agent (`[dispatch.runners]`), with
  the same scoping, writable roots and minimal environment a dispatched run gets. The wire protocol is the provider's
  (providers/): the host only moves bytes and keeps time.
- The session id is issued by the caller (the hub, later) and handed to the provider, which refuses a process that
  reports another one. That ends the one session, never the host.
- `tick()` is synchronous and does all the work: there are no threads, and the clock, the spawner and the process
  probes are injected. Pipes are non-blocking, so a child that stops reading or writing delays nothing.
- One turn in flight per session. A message counts as delivered once its bytes are in the child's stdin, not before.
- A turn that outlives the timeout gets its process group SIGTERMed (SIGKILL after the grace period) and is reported
  failed. Once the whole group is gone (not just its leader: a tool process may outlive it) the session is dormant
  (no process); the next message starts a process that resumes the provider's session. A turn that was cut short is
  never written again by the host.
- Every stdout line is appended to `<log dir>/sessions/<session id>.jsonl` (mode 600). The live processes are listed
  in `<log dir>/node-sessions.json`, so a host that starts after a crash stops what the last one left (`recover`,
  verified by pid and start time like node.py's runs) before it starts anything.
"""
from __future__ import annotations

import json
import logging
import os
import signal
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import providers, runner_preflight
from .dispatch import (DispatchConfig, _signal_group, child_env, probe_process, process_start, render_argv,
                       spawn_piped, stop_verified, with_writable_roots)
from .providers import Event, ProviderSession, validate_model, validate_session_id

log = logging.getLogger("agent_comms.session_host")

STATE_FILE = "node-sessions.json"   # session id -> {pid, proc_start, ...}: what a restarted host must clean up
SESSIONS_DIR = "sessions"           # <session id>.jsonl (stdout) and <session id>.stderr.log
STATES = ("starting", "idle", "running", "waiting", "ended")
MESSAGE_ID_MAX = 200


def group_alive(pgid: int) -> bool:
    """Whether any process is left in this process group (signal 0: nothing is sent). A group this user may not
    signal is not one of ours; macOS also answers EPERM for the instant in which a killed group holds only
    processes that have exited and are not yet reaped."""
    if isinstance(pgid, bool) or not isinstance(pgid, int) or pgid <= 1:
        return False
    try:
        os.killpg(pgid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    return True


@dataclass(frozen=True)
class HostEvent:
    """What happened to a session in one pass.
    - `delivered`: the message's bytes are in the process's stdin; its turn is in flight.
    - `turn_started`: the provider began the turn.
    - `turn_ended`: the turn is over. `ok` False: the provider reported it failed, or the host gave up on it
      (`detail`: timed out, the process exited, the session ended). `delivered` False: the message never reached the
      process. `denials`: the tools the provider refused in it, names only.
    - `approval_requested`: the provider is waiting for a tool to be allowed (`denials` names it).
    - `ended`: the session is over (`detail`); `undelivered` lists the queued messages that were never written."""
    session_id: str
    kind: str
    message_id: str | None = None
    ok: bool | None = None
    delivered: bool = True
    denials: tuple[str, ...] = ()
    meta: dict[str, Any] = field(default_factory=dict)
    detail: str = ""
    undelivered: tuple[str, ...] = ()


@dataclass
class _Message:
    id: str
    text: str


@dataclass
class Session:
    session_id: str
    agent: str
    runtime: str | None
    project: str
    cwd: str
    template: list[str]             # the runner template, scoped and with writable roots; the provider makes the argv
    provider: ProviderSession
    model: str | None = None
    thread_id: int = 0
    resume: bool = False            # the provider's session exists: the next process resumes it
    provider_id: str | None = None
    inbox: deque = field(default_factory=deque)
    child: Any = None
    proc_start: str | None = None
    ready: bool = False             # this process confirmed the session
    writing: _Message | None = None     # its turn is being written (not yet delivered)
    in_flight: _Message | None = None   # delivered; its turn has not ended
    outbuf: bytearray = field(default_factory=bytearray)
    busy_since: float | None = None     # when the turn (or a handshake the provider waits for) began
    interrupted_at: float | None = None
    stopping_since: float | None = None
    killed: bool = False
    waiting: bool = False
    denials: tuple[str, ...] = ()   # tools refused in the last turn (names only)
    ended: bool = False
    error: str = ""
    transcript_fd: int | None = None
    tee_failed: bool = False

    @property
    def dormant(self) -> bool:
        """No process: the next message starts one."""
        return self.child is None and not self.ended

    @property
    def busy(self) -> bool:
        return self.writing is not None or self.in_flight is not None

    @property
    def state(self) -> str:
        if self.ended:
            return "ended"
        if self.waiting:
            return "waiting"
        if self.busy:
            return "running" if self.ready else "starting"
        if self.child is not None and self.stopping_since is None and not self.ready and self.provider.waits_for_ready:
            return "starting"
        return "idle"


class SessionHost:
    def __init__(self, config: DispatchConfig, log_dir: Path, *, timeout_minutes: float | None = None,
                 kill_grace_seconds: float | None = None, spawner: Callable[..., Any] = spawn_piped,
                 clock: Callable[[], float] = time.monotonic, probe: Callable[[Any, Any], str] = probe_process,
                 start_of: Callable[[int], Any] = process_start,
                 signal_group: Callable[[int, int], None] = _signal_group,
                 sleep: Callable[[float], None] = time.sleep,
                 group_alive: Callable[[int], bool] = group_alive,
                 provider_for: Callable[[list[str]], Callable[[], ProviderSession] | None] = providers.provider_for):
        self.config, self.log_dir, self.spawner, self.clock = config, Path(log_dir), spawner, clock
        self.probe, self.start_of, self.signal_group, self.sleep = probe, start_of, signal_group, sleep
        self.provider_for, self.group_alive = provider_for, group_alive
        self.timeout_minutes = float(config.timeout_minutes if timeout_minutes is None else timeout_minutes)
        if not self.timeout_minutes > 0:
            raise ValueError("timeout_minutes must be positive")
        self.kill_grace = float(config.kill_grace_seconds if kill_grace_seconds is None else kill_grace_seconds)
        self.sessions: dict[str, Session] = {}
        self._recovered = False
        self._next_message = 0

    # ------------------------------------------------------------ local state (survives a host restart)

    @property
    def sessions_dir(self) -> Path:
        return self.log_dir / SESSIONS_DIR

    def transcript_path(self, session_id: str) -> Path:
        return self.sessions_dir / f"{validate_session_id(session_id)}.jsonl"

    def _state_path(self) -> Path:
        return self.log_dir / STATE_FILE

    def _load_state(self) -> dict:
        try:
            data = json.loads(self._state_path().read_text())
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save_state(self) -> None:
        self.log_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        data = {s.session_id: {"pid": getattr(s.child, "pid", None), "proc_start": s.proc_start, "cwd": s.cwd,
                               "resume": s.resume, "turn": (s.in_flight or s.writing).id if s.busy else None}
                for s in self.sessions.values() if s.child is not None}
        tmp = self._state_path().with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            os.fchmod(fd, 0o600)
            os.write(fd, json.dumps(data).encode())
        finally:
            os.close(fd)
        os.replace(tmp, self._state_path())

    def recover(self) -> dict[str, dict]:
        """Once, before this host starts anything: stop the session processes an earlier host on this machine left.
        Only a process whose pid still leads its own group and whose start time matches the record is signalled
        (SIGTERM, then SIGKILL after the grace period), and a group one of those led is killed even when its leader
        went on SIGTERM and a member did not. Returns what was recorded, per session id: `stopped` (a live process
        was signalled), `turn` (the message whose turn was in flight: it is lost, never replayed) and `resume` (the
        provider's session exists, so reopening it should resume)."""
        if self._recovered:
            return {}
        self._recovered = True
        left = {k: d for k, d in self._load_state().items()
                if isinstance(k, str) and providers.SESSION_ID_RE.fullmatch(k) and isinstance(d, dict)}
        stopped = set(stop_verified([(d.get("pid"), d.get("proc_start")) for d in left.values()], probe=self.probe,
                                    signal_group=self.signal_group, sleep=self.sleep, grace=self.kill_grace))
        # stop_verified kills only a pid that still verifies after the grace period. A leader that exited on SIGTERM
        # no longer does, yet a tool process in its group may have ignored the signal: a resumed session must not
        # start beside it. Only groups whose leader was verified and signalled above are touched.
        lingering = [pid for pid in stopped if self.group_alive(pid)]
        for pid in lingering:
            self.signal_group(pid, signal.SIGKILL)
        deadline = self.clock() + self.kill_grace
        while any(self.group_alive(pid) for pid in lingering) and self.clock() < deadline:
            self.sleep(0.2)
        if left or self._state_path().exists():
            self._save_state()
        return {k: {"stopped": d.get("pid") in stopped, "resume": d.get("resume") is True,
                    "turn": d["turn"] if isinstance(d.get("turn"), str) else None} for k, d in left.items()}

    # ------------------------------------------------------------ the caller's side

    def open(self, session_id: str, *, agent: str, project: str, runtime: str | None = None, model: str | None = None,
             resume: bool = False, thread_id: int = 0) -> Session:
        """Register a session. Nothing starts until it has a message. `resume`: the provider already has this session
        (an earlier host ran it). Raises when this machine cannot host it: no runner, no adapter for the runner's
        CLI, no run directory, a runner or model that cannot be used."""
        validate_session_id(session_id)
        validate_model(model)
        if session_id in self.sessions:
            raise ValueError("session is already open on this host")
        template = self.config.runner_for(agent, runtime)
        if template is None:
            raise LookupError(f"no runner configured on this machine for {agent}")
        factory = self.provider_for(template)
        if factory is None:
            raise LookupError(f"the runner for {agent} cannot be hosted as a session")
        cwd = self.config.checkout_for(project)
        if not cwd or not os.path.isabs(cwd) or not os.path.isdir(cwd):
            raise FileNotFoundError(f"this machine has no run directory for project {project!r}")
        if runner_preflight.scoped(template, project, self.config.claude_tool_projects):
            template = runner_preflight.scoped_template(template)
        template = with_writable_roots(template, self.config.extra_roots(template, project))
        s = Session(session_id, agent, runtime, project, cwd, template, factory(), model=model,
                    thread_id=int(thread_id), resume=bool(resume))
        self._argv(s)       # now, so a runner that cannot be used is refused here and not at the first message
        self.sessions[session_id] = s
        return s

    def send(self, session_id: str, text: str, *, message_id: str | None = None) -> str:
        """Queue a message for the session (first in, first out). Returns its id."""
        s = self._session(session_id)
        if s.ended:
            raise ValueError("session has ended")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("message text is empty")
        if message_id is None:
            self._next_message += 1
            message_id = f"m{self._next_message}"
        if not isinstance(message_id, str) or not 0 < len(message_id) <= MESSAGE_ID_MAX:
            raise ValueError("message id must be a short string")
        s.inbox.append(_Message(message_id, text))
        return message_id

    def state(self, session_id: str) -> str:
        return self._session(session_id).state

    def close(self, session_id: str, detail: str = "closed") -> list[HostEvent]:
        """End a session now: its process is terminated, its turn in flight fails, its queue is dropped."""
        out: list[HostEvent] = []
        s = self._session(session_id)
        if not s.ended:
            self._end(s, detail, self.clock(), out)
        return out

    def forget(self, session_id: str) -> None:
        """Drop an ended session whose process is gone, so its id no longer counts as open here."""
        s = self._session(session_id)
        if not s.ended or s.child is not None:
            raise ValueError("session has not ended, or its process is still stopping")
        del self.sessions[session_id]

    def _session(self, session_id: str) -> Session:
        s = self.sessions.get(session_id)
        if s is None:
            raise LookupError("no such session on this host")
        return s

    # ------------------------------------------------------------ one pass

    def tick(self) -> list[HostEvent]:
        """One pass over every session: read what its process wrote, notice an exit, hold the turn to its timeout,
        then write the next message if nothing is in flight. One session's failure never stops the others."""
        if not self._recovered:
            self.recover()
        out: list[HostEvent] = []
        now = self.clock()
        for s in list(self.sessions.values()):
            try:
                self._pass(s, now, out)
            except Exception as e:
                log.exception("session %s failed", s.session_id)
                if not s.ended:
                    self._end(s, f"host error: {type(e).__name__}", now, out)
        return out

    def _pass(self, s: Session, now: float, out: list[HostEvent]) -> None:
        if s.child is not None:
            self._read(s, now, out)
            self._reap(s, now, out)
        if s.child is not None and s.stopping_since is None and not s.ended:
            self._hold_to_timeout(s, now, out)
        if s.child is not None and s.stopping_since is not None:
            if not s.killed and now - s.stopping_since >= self.kill_grace:
                s.killed = True
                s.child.kill()
            self._reap(s, now, out)
            if s.child is not None:
                return      # still going down: nothing new starts beside it
        if not s.ended:
            self._write(s, now, out)
            if s.child is not None and s.stopping_since is not None:
                self._reap(s, now, out)     # a write that failed stopped it

    def _read(self, s: Session, now: float, out: list[HostEvent]) -> None:
        for line in s.child.read_lines():
            self._tee(s, line)
            if s.ended:
                continue
            events, replies = s.provider.feed(line)
            if s.stopping_since is not None:
                # This process's turn was already given up: its late turn events are dropped. That the provider's
                # session exists still counts, or the next process would start it anew and be refused.
                if any(ev.kind == "ready" for ev in events) and not s.resume:
                    s.resume = True
                    self._save_state()
                continue
            for data in replies:
                s.outbuf += data
            for ev in events:
                self._on_event(s, ev, now, out)

    def _on_event(self, s: Session, ev: Event, now: float, out: list[HostEvent]) -> None:
        if ev.kind == "ready":
            s.ready, s.resume, s.provider_id = True, True, ev.provider_id
            if not s.busy:
                s.busy_since = None
            self._save_state()
        elif ev.kind == "turn_started":
            if s.in_flight is not None:
                out.append(HostEvent(s.session_id, "turn_started", s.in_flight.id))
        elif ev.kind == "turn_ended":
            if s.in_flight is None:
                log.warning("session %s: a turn ended that the host did not start", s.session_id)
                return
            msg, s.in_flight, s.busy_since = s.in_flight, None, None
            timed_out, s.interrupted_at = s.interrupted_at is not None, None
            s.denials, s.waiting = ev.denials, bool(ev.denials)
            out.append(HostEvent(s.session_id, "turn_ended", msg.id, ok=bool(ev.ok) and not timed_out,
                                 denials=ev.denials, meta=dict(ev.meta),
                                 detail=self._timeout_detail() if timed_out else ev.detail))
            self._save_state()
        elif ev.kind == "approval_requested":
            s.denials, s.waiting = ev.denials, True
            out.append(HostEvent(s.session_id, "approval_requested", s.in_flight.id if s.in_flight else None,
                                 denials=ev.denials))
        elif ev.kind == "fatal":
            self._end(s, ev.detail or "the provider reported a fatal error", now, out)

    def _reap(self, s: Session, now: float, out: list[HostEvent]) -> None:
        code = s.child.poll()
        if code is None:
            return
        if s.stopping_since is not None and self.group_alive(s.child.pid):
            return      # the host stopped it and its leader is gone, but not its group: nothing starts beside that
        self._read(s, now, out)     # what it wrote on its way out (a result that just made it still counts)
        child, s.child, s.proc_start, s.ready = s.child, None, None, False
        stopped, s.stopping_since, s.killed, s.interrupted_at = s.stopping_since is not None, None, False, None
        s.outbuf.clear()
        close = getattr(child, "close", None)
        if close is not None:
            close()
        if s.transcript_fd is not None:
            os.close(s.transcript_fd)
            s.transcript_fd = None
        if not stopped and not s.ended:
            # It went by itself. The turn it had is lost; the next message starts a fresh process.
            if s.in_flight is not None:
                self._fail(s, f"the process exited (code {code}) before the turn ended", out)
            elif s.writing is not None:
                self._fail(s, f"the process exited (code {code}) before the message was written", out)
            s.busy_since = None
        self._save_state()

    def _timeout_detail(self) -> str:
        return f"timed out after {self.timeout_minutes:g} minutes"

    def _hold_to_timeout(self, s: Session, now: float, out: list[HostEvent]) -> None:
        if s.busy_since is None:
            return
        if s.interrupted_at is not None:
            if now - s.interrupted_at >= self.kill_grace:     # the provider's own interrupt did not end the turn
                self._fail(s, self._timeout_detail(), out)
                self._stop(s, now)
            return
        if now - s.busy_since < self.timeout_minutes * 60:
            return
        data = s.provider.interrupt() if s.in_flight is not None and not s.outbuf else None
        if data is not None:
            for b in data:
                s.outbuf += b
            s.interrupted_at = now
            return
        if not s.busy and s.inbox:
            s.writing = s.inbox.popleft()   # a handshake that never finished: the message waiting on it fails
        self._fail(s, self._timeout_detail(), out)
        self._stop(s, now)

    def _fail(self, s: Session, detail: str, out: list[HostEvent]) -> None:
        """Give up on the session's turn: failed, and never written again."""
        msg, delivered = (s.in_flight, True) if s.in_flight is not None else (s.writing, False)
        s.in_flight = s.writing = s.busy_since = None
        if msg is not None:
            out.append(HostEvent(s.session_id, "turn_ended", msg.id, ok=False, delivered=delivered, detail=detail))

    def _stop(self, s: Session, now: float) -> None:
        if s.child is not None and s.stopping_since is None:
            s.stopping_since = now
            s.child.terminate()     # the whole process group (PopenChild)

    def _end(self, s: Session, detail: str, now: float, out: list[HostEvent]) -> None:
        s.ended, s.error, s.waiting = True, detail[:300], False
        self._fail(s, f"session ended: {detail}"[:300], out)
        undelivered = tuple(m.id for m in s.inbox)
        s.inbox.clear()
        self._stop(s, now)
        out.append(HostEvent(s.session_id, "ended", detail=s.error, undelivered=undelivered))

    # ------------------------------------------------------------ starting and writing

    def _argv(self, s: Session) -> list[str]:
        argv = s.provider.argv(s.template, session_id=s.session_id, resume=s.resume, model=s.model)
        return render_argv(argv, prompt="", project=s.cwd, thread_id=s.thread_id)

    def _spawn(self, s: Session, now: float, out: list[HostEvent]) -> None:
        try:
            argv = self._argv(s)
            self.sessions_dir.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            self.sessions_dir.mkdir(exist_ok=True, mode=0o700)
            child = self.spawner(argv, cwd=s.cwd, env=child_env(s.agent, self.config, runtime=s.runtime),
                                 log_path=self.sessions_dir / f"{s.session_id}.stderr.log")
        except Exception as e:
            log.warning("could not start session %s: %s", s.session_id, e)
            self._end(s, f"could not start: {type(e).__name__}: {e}", now, out)
            return
        s.child, s.ready, s.stopping_since, s.killed = child, False, None, False
        try:
            s.proc_start = self.start_of(child.pid)
        except Exception:
            s.proc_start = None
        s.outbuf = bytearray(b"".join(s.provider.on_open()))
        s.busy_since = now if s.provider.waits_for_ready else None
        try:
            fd = os.open(self.transcript_path(s.session_id),
                         os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0), 0o600)
            os.fchmod(fd, 0o600)
            s.transcript_fd = fd
        except OSError as e:
            log.warning("session %s: no transcript (%s)", s.session_id, e)
        self._save_state()
        log.info("started session %s for %s (pid %s)", s.session_id, s.agent, getattr(child, "pid", None))

    def _tee(self, s: Session, line: bytes) -> None:
        if s.transcript_fd is None:
            return
        try:
            os.write(s.transcript_fd, line + b"\n")
        except OSError as e:
            if not s.tee_failed:
                s.tee_failed = True
                log.warning("session %s: could not write its transcript (%s)", s.session_id, e)

    def _write(self, s: Session, now: float, out: list[HostEvent]) -> None:
        if s.child is None:
            if not s.inbox:
                return
            self._spawn(s, now, out)
            if s.child is None:
                return
        if not s.busy and s.inbox and (s.ready or not s.provider.waits_for_ready):
            s.writing = s.inbox.popleft()
            s.waiting, s.denials, s.busy_since = False, (), now
            for data in s.provider.turn(s.writing.text):
                s.outbuf += data
            self._save_state()      # the turn is on record before its first byte is written
        if s.outbuf:
            try:
                n = s.child.write(bytes(s.outbuf))
            except OSError as e:
                # The process no longer reads. A message that was not fully written never reached it; a turn in
                # flight (an answer to the process could not be written) is lost with the process.
                s.outbuf.clear()
                self._fail(s, f"could not write to the process: {type(e).__name__}", out)
                self._stop(s, now)
                return
            del s.outbuf[:n]
        if not s.outbuf and s.writing is not None:
            s.in_flight, s.writing = s.writing, None
            out.append(HostEvent(s.session_id, "delivered", s.in_flight.id))

    # ------------------------------------------------------------ shutdown

    def stop_all(self) -> list[HostEvent]:
        """Shutdown: terminate every session's process group, wait the grace period, kill what remains of each group
        (its leader may be gone while a member is not). A turn in flight is reported failed. Sessions are not ended:
        a later host may reopen them with resume."""
        out: list[HostEvent] = []
        now = self.clock()
        live = [s for s in self.sessions.values() if s.child is not None]
        for s in live:
            self._fail(s, "the session host shut down", out)
            self._stop(s, now)
        def going(s: Session) -> bool:
            return s.child.poll() is None or self.group_alive(s.child.pid)
        deadline = self.clock() + self.kill_grace
        while any(going(s) for s in live) and self.clock() < deadline:
            self.sleep(0.2)
        for s in live:
            if going(s):
                s.child.kill()
            self._read(s, now, out)     # the transcript keeps what was written until the end
            close = getattr(s.child, "close", None)
            if close is not None:
                close()
            if s.transcript_fd is not None:
                os.close(s.transcript_fd)
                s.transcript_fd = None
            s.child, s.proc_start, s.ready, s.stopping_since, s.killed = None, None, False, None, False
            s.outbuf.clear()
        self._save_state()
        return out
