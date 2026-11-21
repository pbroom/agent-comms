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
- A provider that issues its own id for a session (Codex's thread id) has it recorded in
  `<log dir>/node-session-providers.json` (mode 600) before the first turn is written to that session, and is
  handed it again (`adopt`) when the session is opened after a restart: a recorded id is resumed, whatever the
  caller knows. The id never comes from the hub. It is dropped when the provider refuses to resume it.
- A message waits while the provider runs something of its own in the session (`holds_turns`).
- A provider with a handshake (`waits_for_ready`) gets `handshake_seconds` for it, not the turn timeout.
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

from . import providers, runner_preflight, session_access
from .dispatch import (DispatchConfig, _signal_group, child_env, probe_process, process_start, render_argv,
                       spawn_piped, stop_verified, with_writable_roots)
from .providers import Event, ProviderSession, validate_model, validate_session_id

log = logging.getLogger("agent_comms.session_host")

STATE_FILE = "node-sessions.json"   # session id -> {pid, proc_start, ...}: what a restarted host must clean up
PROVIDER_FILE = "node-session-providers.json"   # session id -> the provider's own id for it (Codex's thread id)
HANDSHAKE_SECONDS = 60.0            # a process whose provider has a handshake must be ready within this
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
      process. `denials`: the tools the provider refused in it, names only. `text`: the reply, capped
      (providers.cap_reply); for the human who sent the message only.
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
    text: str = ""


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
    held_since: float | None = None     # since when the next message waits for a turn of the provider's own
    fresh_start: bool = False           # the provider refused to resume its session: the next process starts a new one
    interrupted_at: float | None = None
    stopping_since: float | None = None
    killed: bool = False
    waiting: bool = False
    denials: tuple[str, ...] = ()   # tools refused in the last turn (names only)
    ended: bool = False
    error: str = ""
    transcript_fd: int | None = None
    tee_failed: bool = False
    stderr_from: int = 0            # size of the stderr log when this process started: what it wrote comes after
    # Access (session_access): `asked`, the word it was opened with (None: a session of its own, which runs its
    # runner as configured); `access`, what it has as far as this host knows (what its command line is expected to
    # give, then what its process reported); `access_known`, once that is verified (at once for a CLI that reports
    # no mode).
    asked: str | None = None
    access: str | None = None
    access_known: bool = False
    effort: str | None = None

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
                 provider_for: Callable[[list[str]], Callable[[], ProviderSession] | None] | None = None,
                 handshake_seconds: float = HANDSHAKE_SECONDS):
        self.config, self.log_dir, self.spawner, self.clock = config, Path(log_dir), spawner, clock
        self.probe, self.start_of, self.signal_group, self.sleep = probe, start_of, signal_group, sleep
        # Codex has an adapter only on a machine that turned it on ([dispatch.sessions] codex = true).
        self.provider_for = provider_for or (
            lambda template: providers.provider_for(template, codex=config.sessions.codex))
        self.group_alive = group_alive
        self.handshake_seconds = float(handshake_seconds)
        if not self.handshake_seconds > 0:
            raise ValueError("handshake_seconds must be positive")
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

    def _provider_ids(self) -> dict[str, str]:
        """session id -> the provider's own id, as recorded when a process confirmed the session."""
        try:
            data = json.loads((self.log_dir / PROVIDER_FILE).read_text())
        except (OSError, ValueError):
            return {}
        return {k: v for k, v in data.items() if isinstance(k, str) and providers.SESSION_ID_RE.fullmatch(k)
                and (v is None or providers.valid_provider_id(v))} if isinstance(data, dict) else {}

    def _record_provider_id(self, session_id: str, provider_id: str | None) -> None:
        """Keep (or, with None, drop) the provider's id for a session. Only an id that is not the session id itself
        is worth a record. A record that cannot be written costs the conversation at the next restart, not now.
        Recording an id also replaces a fresh-start mark (below): the new session is the one to resume from now on."""
        ids = self._provider_ids()
        keep = providers.valid_provider_id(provider_id) if provider_id != session_id else None
        if keep is None:
            if session_id not in ids:
                return
            del ids[session_id]
        elif ids.get(session_id) == keep:
            return
        else:
            ids[session_id] = keep
        self._write_provider_ids(ids)

    def _record_fresh_start(self, session_id: str) -> None:
        """The provider refused to resume its session for this one: the next process must start a NEW one, whatever
        anybody else still believes. Kept as the session's entry with no id (JSON null) until the new session's id
        takes its place. `open` reads it after a restart and lets it overrule the caller's `resume`: the hub may
        never have heard of the refusal (the node died before its report), and would send the session back into the
        same refused resume."""
        ids = self._provider_ids()
        if session_id in ids and ids[session_id] is None:
            return
        ids[session_id] = None
        self._write_provider_ids(ids)

    def _write_provider_ids(self, ids: dict[str, str | None]) -> None:
        try:
            self.log_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            path = self.log_dir / PROVIDER_FILE
            tmp = path.with_suffix(".tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o600)
            try:
                os.fchmod(fd, 0o600)
                os.write(fd, json.dumps(ids).encode())
            finally:
                os.close(fd)
            os.replace(tmp, path)
        except OSError as e:
            log.warning("could not record the providers' session ids in %s (%s)", PROVIDER_FILE, e)

    def keep_provider_ids(self, session_ids) -> None:
        """Drop the recorded provider ids of every session not named here (the hub no longer lists them)."""
        ids = self._provider_ids()
        kept = {k: v for k, v in ids.items() if k in session_ids or k in self.sessions}
        if kept != ids:
            self._write_provider_ids(kept)

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
             resume: bool = False, thread_id: int = 0, access: str | None = None, effort: str | None = None,
             cwd: str | None = None) -> Session:
        """Register a session. Nothing starts until it has a message. `resume`: the provider already has this session
        (an earlier host ran it). `access`: the access word it is to run at (session_access; None: as its runner has
        it), `effort`: one of session_access.EFFORTS, `cwd`: its run directory when that is not the project's
        checkout (a child session's own worktree). Raises when this machine cannot host it: no runner, no adapter
        for the runner's CLI, no run directory, a runner or model that cannot be used, an access level this machine
        does not grant."""
        validate_session_id(session_id)
        validate_model(model)
        session_access.effort(effort)
        if session_id in self.sessions:
            raise ValueError("session is already open on this host")
        template = self.config.runner_for(agent, runtime)
        if template is None:
            raise LookupError(f"no runner configured on this machine for {agent}")
        factory = self.provider_for(template)
        if factory is None:
            raise LookupError(f"the runner for {agent} cannot be hosted as a session")
        checkout = self.config.checkout_for(project)
        if not checkout or not os.path.isabs(checkout) or not os.path.isdir(checkout):
            raise FileNotFoundError(f"this machine has no run directory for project {project!r}")
        if cwd is None:
            cwd = checkout
        elif not isinstance(cwd, str) or not os.path.isabs(cwd) or not os.path.isdir(cwd):
            raise FileNotFoundError("the session's own run directory does not exist")
        # The access word becomes flags here, from this machine's runner and settings (nothing executable came from
        # the hub). Without a word, and without allow_full_access, the template is untouched.
        prep = session_access.prepare(template, access, allow_full=self.config.sessions.allow_full_access, cwd=cwd,
                                      board_mcp=lambda configs: self._board_mcp(session_id, configs))
        template = prep.template
        if (prep.as_configured and prep.configure != "full"
                and runner_preflight.scoped(template, project, self.config.claude_tool_projects)):
            template = runner_preflight.with_denied_tool_instruction(runner_preflight.scoped_template(template))
        if prep.as_configured or prep.granted != "read-only":      # a session narrowed to read-only gets no extra roots
            template = with_writable_roots(template, self.config.extra_roots(template, project))
        s = Session(session_id, agent, runtime, project, cwd, template, factory(), model=model,
                    thread_id=int(thread_id), resume=bool(resume), asked=access, access=prep.granted,
                    effort=effort)
        # A CLI that reports the mode it really runs in (Claude) has its access verified when its first turn starts.
        s.access_known = not getattr(s.provider, "verifies_mode", False)
        runs_in = getattr(s.provider, "runs_in", None)
        if runs_in is not None:
            runs_in(cwd)        # a child's own worktree when it has one: the process, and so the thread, works there
        if prep.configure is not None or effort is not None:
            configure = getattr(s.provider, "configure", None)
            if configure is None:
                raise ValueError(f"the runner for {agent} takes no access level or effort as a hosted session")
            configure(access=prep.configure, effort=effort)
        adopt = getattr(s.provider, "adopt", None)
        if adopt is not None:
            # This machine's own record, never an id from the hub. It is written before the first turn goes to the
            # provider's session (`_write`), so its being there says a turn may be in that session, whatever the
            # caller's `resume` says: a node that crashed before it could report the turn must not start a NEW
            # conversation and silently lose that one.
            # An entry with no id is the other decision made here (`_record_fresh_start`): the provider refused to
            # resume, so a new session is started, even if the caller (the hub, which may not have heard) says resume.
            ids = self._provider_ids()
            s.provider_id = ids.get(session_id)
            adopt(s.provider_id)
            if session_id in ids:
                s.resume, s.fresh_start = s.provider_id is not None, s.provider_id is None
        self._argv(s)       # now, so a runner that cannot be used is refused here and not at the first message
        self.sessions[session_id] = s
        return s

    def _board_mcp(self, session_id: str, configs: list[str]) -> str | None:
        """For a read-only Claude session: a config holding ONLY the board's MCP server, as the runner's own
        `--mcp-config` values define it (each a file, or the JSON itself), written beside the session's transcript
        (mode 600; it names the launcher, never a token). None when the runner names no config with that server:
        the session then has no MCP server at all. Nothing is looked for in the user's own Claude configuration."""
        entry = None
        for value in configs:
            try:
                text = value if value.lstrip().startswith("{") else Path(value).read_text()
                servers = json.loads(text).get("mcpServers")
            except (OSError, ValueError, AttributeError):
                continue
            if isinstance(servers, dict) and isinstance(servers.get(session_access.BOARD_SERVER), dict):
                entry = servers[session_access.BOARD_SERVER]
        if entry is None:
            return None
        self.sessions_dir.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.sessions_dir.mkdir(exist_ok=True, mode=0o700)
        path = self.sessions_dir / f"{validate_session_id(session_id)}.mcp.json"
        tmp = path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            os.fchmod(fd, 0o600)
            os.write(fd, json.dumps({"mcpServers": {session_access.BOARD_SERVER: entry}}).encode())
        finally:
            os.close(fd)
        os.replace(tmp, path)
        return str(path)

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
        self._record_provider_id(session_id, None)
        try:
            (self.sessions_dir / f"{session_id}.mcp.json").unlink()     # a read-only session's board-only MCP config
        except OSError:
            pass

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
                # (Not for a provider with a handshake: its `ready` says only that an id was issued, see `_write`.)
                if any(ev.kind == "ready" for ev in events) and not s.resume and not s.provider.waits_for_ready:
                    s.resume = True
                    self._save_state()
                continue
            for data in replies:
                s.outbuf += data
            for ev in events:
                self._on_event(s, ev, now, out)

    def _on_event(self, s: Session, ev: Event, now: float, out: list[HostEvent]) -> None:
        if ev.kind == "ready":
            s.ready, s.provider_id = True, ev.provider_id
            if not s.provider.waits_for_ready:
                s.resume = True     # Claude: `ready` comes with the first turn, and the session exists from then on
            if not s.busy:
                s.busy_since = None
            self._save_state()
        elif ev.kind == "turn_started":
            # What the process says it really runs this turn with, against what the session may have. A mode from a
            # settings file is past this machine's runner: bypassed permission checks where they may not be, or a
            # session asked for at read-only that has more, end the session here, before the turn has done anything.
            # For a session of its own this is also where its access is read off: a label, not a limit.
            verified, problem = session_access.verify(s.asked, ev.mode, ev.tools,
                                                      allow_full=self.config.sessions.allow_full_access)
            if problem is not None:
                log.warning("session %s: %s", s.session_id, problem)
                self._end(s, problem, now, out)
                return
            if verified is not None and (verified != s.access or not s.access_known):
                s.access, s.access_known = verified, True
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
                                 detail=self._timeout_detail() if timed_out else ev.detail, text=ev.text))
            self._save_state()
        elif ev.kind == "approval_requested":
            s.denials, s.waiting = ev.denials, True
            out.append(HostEvent(s.session_id, "approval_requested", s.in_flight.id if s.in_flight else None,
                                 denials=ev.denials))
        elif ev.kind == "fatal":
            self._end(s, ev.detail or "the provider reported a fatal error", now, out)
        elif ev.kind == "start_failed":
            # This process cannot serve the session (its handshake was refused). The message that was waiting on it
            # fails with the reason, the process is stopped, the session stays: the next message starts another.
            # If the provider says its session cannot be resumed, that is put on record FIRST (the flag, the state
            # file, the provider id's record): a host that restarts right after the failure was reported must not
            # try the same resume again.
            self._correct_start_mode(s)
            self._save_state()
            self._fail_start(s, ev.detail or "the agent process refused to start the session", out)
            self._stop(s, now)

    def _reap(self, s: Session, now: float, out: list[HostEvent]) -> None:
        code = s.child.poll()
        if code is None:
            return
        if s.stopping_since is not None and self.group_alive(s.child.pid):
            return      # the host stopped it and its leader is gone, but not its group: nothing starts beside that
        self._read(s, now, out)     # what it wrote on its way out (a result that just made it still counts)
        if not s.ready:
            self._correct_start_mode(s)
        was_ready = s.ready
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
            elif s.inbox and s.provider.waits_for_ready and not was_ready:
                # It went during its handshake. The message that made it start fails with it: otherwise a process
                # that can never start (a runner flag Codex refuses) would be started again on every pass.
                self._fail_start(s, f"the process exited (code {code}) before it was ready", out)
            s.busy_since = None
        s.held_since = None
        self._save_state()

    def _correct_start_mode(self, s: Session) -> None:
        """A process that went without ever confirming the session may have said why (stderr): the session it was told
        to create exists, or the one it was told to resume does not. The next process then starts the other way, so
        a wrong guess (a crash between the provider creating the session and anyone recording it) costs one failed
        message instead of every later one. Nothing is retried here."""
        said = getattr(s.provider, "session_exists", None)
        if said is None:
            return
        try:
            with open(self.sessions_dir / f"{s.session_id}.stderr.log", "rb") as f:
                f.seek(s.stderr_from)
                stderr = f.read(65536).decode("utf-8", "replace")
        except OSError:
            stderr = ""     # a provider may know from its own protocol instead (Codex's refused thread/resume)
        exists = said(stderr)
        if exists is not None and exists != s.resume:
            log.warning("session %s: its process said the session %s; the next one %s", s.session_id,
                        "already exists" if exists else "does not exist", "resumes it" if exists else "creates it")
            s.resume = exists
            if not exists and s.provider.waits_for_ready:
                # The provider's own session (it issued the id) is not to be resumed again: its id is forgotten, and
                # the fresh start is put on record in its place, where a restarted host finds it.
                s.provider_id, s.fresh_start = None, True
                self._record_fresh_start(s.session_id)

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
        if not s.busy and not s.ready:
            # A handshake, not a turn. It has its own short limit, so a process that never becomes ready fails the
            # message waiting on it now and not after a whole turn's timeout.
            if now - s.busy_since < self.handshake_seconds:
                return
            self._fail_start(s, f"the agent process was not ready after {self.handshake_seconds:g} seconds", out)
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
        self._fail(s, self._timeout_detail(), out)
        self._stop(s, now)

    def _fail(self, s: Session, detail: str, out: list[HostEvent]) -> None:
        """Give up on the session's turn: failed, and never written again."""
        msg, delivered = (s.in_flight, True) if s.in_flight is not None else (s.writing, False)
        s.in_flight = s.writing = s.busy_since = None
        if msg is not None:
            out.append(HostEvent(s.session_id, "turn_ended", msg.id, ok=False, delivered=delivered, detail=detail))

    def _fail_start(self, s: Session, detail: str, out: list[HostEvent]) -> None:
        """A process did not get through its handshake (refused, timed out, exited, or would not take the bytes). The
        message that made it start fails with it, exactly once: it leaves the inbox here, so whichever path sees the
        failure first settles it and nothing starts a process for it again."""
        if not s.busy and s.inbox:
            s.writing = s.inbox.popleft()
        self._fail(s, detail, out)

    @staticmethod
    def holding(s: Session) -> bool:
        """The session's provider is running something of its own in it (`holds_turns`): a turn nobody here started.
        Its process is then not idle, whatever the host's own books say."""
        holds = getattr(s.provider, "holds_turns", None)
        return s.child is not None and holds is not None and bool(holds())

    def _held(self, s: Session, now: float, out: list[HostEvent]) -> bool:
        """The provider's session is busy with something of its own (`holds_turns`): the next message waits, for at
        most the turn timeout. Past that the message fails and the process is stopped (the next one resumes)."""
        if not self.holding(s):
            s.held_since = None
            return False
        if s.held_since is None:
            s.held_since = now
        if now - s.held_since >= self.timeout_minutes * 60:
            s.writing, s.held_since = s.inbox.popleft(), None
            self._fail(s, f"the agent was busy with a turn of its own for {self.timeout_minutes:g} minutes; the "
                          "message was not written", out)
            self._stop(s, now)
        return True

    def _stop(self, s: Session, now: float) -> None:
        if s.child is not None and s.stopping_since is None:
            s.stopping_since = now
            # The whole process group (PopenChild). Not once all of it is known to be gone (a caller may have
            # stopped it already, node.shut_down): its id could be another group's by now.
            if s.child.poll() is None or self.group_alive(s.child.pid):
                s.child.terminate()

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
            try:
                s.stderr_from = os.path.getsize(self.sessions_dir / f"{s.session_id}.stderr.log")
            except OSError:
                s.stderr_from = 0
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
        if not s.busy and s.inbox and (s.ready or not s.provider.waits_for_ready) and not self._held(s, now, out):
            s.writing = s.inbox.popleft()
            s.waiting, s.denials, s.busy_since = False, (), now
            if s.provider.waits_for_ready and not s.resume:
                # The first turn for a session the provider only issued an id for (Codex's thread). From the moment
                # its bytes may be in the pipe the provider may have recorded the turn, so that the session is to be
                # RESUMED is on record first: the flag and the id (PROVIDER_FILE, which `open` trusts after a
                # restart). If the provider never got the turn, the resume is refused and that is said out loud.
                s.resume, s.fresh_start = True, False
                self._record_provider_id(s.session_id, s.provider_id)
            for data in s.provider.turn(s.writing.text):
                s.outbuf += data
            self._save_state()      # the turn is on record before its first byte is written
        if s.outbuf:
            try:
                n = s.child.write(bytes(s.outbuf))
            except OSError as e:
                # The process no longer reads. A message that was not fully written never reached it; a turn in
                # flight (an answer to the process could not be written) is lost with the process. During a
                # handshake the message that made the process start is still in the inbox: it fails here too.
                s.outbuf.clear()
                detail = f"could not write to the process: {type(e).__name__}"
                if s.provider.waits_for_ready and not s.ready:
                    self._fail_start(s, detail, out)
                else:
                    self._fail(s, detail, out)
                self._stop(s, now)
                return
            del s.outbuf[:n]
        if not s.outbuf and s.writing is not None:
            s.in_flight, s.writing = s.writing, None
            out.append(HostEvent(s.session_id, "delivered", s.in_flight.id))

    # ------------------------------------------------------------ stopping a process, keeping the session

    def park(self, session_id: str) -> bool:
        """Stop an idle session's process (an idle limit, or room for another session). The session stays: its next
        message starts a process that resumes it. False, and nothing is touched, when it has a turn or a message
        waiting, no process, or one already going down, or when its provider is running a turn of its own in it:
        nothing running is interrupted."""
        s = self._session(session_id)
        if s.child is None or s.ended or s.busy or s.inbox or s.stopping_since is not None or self.holding(s):
            return False
        self._stop(s, self.clock())
        return True

    def stop_turns(self, detail: str) -> list[HostEvent]:
        """Give up on every turn in flight: each is reported failed and its process is stopped (the caller lost the
        hub, so nobody could see or stop the turn). Idle processes and the sessions themselves stay."""
        out: list[HostEvent] = []
        now = self.clock()
        for s in self.sessions.values():
            if s.child is not None and s.busy and not s.ended:
                self._fail(s, detail[:300], out)
                self._stop(s, now)
        if out:
            self._save_state()
        return out

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
