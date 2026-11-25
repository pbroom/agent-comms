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
- A message waits while the provider runs something of its own in the session (`holds_turns`): Codex's own turn,
  or a turn Claude Code started itself in a process the adapter verified (DESIGN_NOTES "Claude turn correlation").
  A turn's frames that the adapter says are no message's (`Event.turn` "other") never settle the turn in flight.
  Every other behaviour of turn correlation is scoped to a VERIFIED Claude incarnation (`_correlated`), the write
  barrier's own rules (`_barrier`) included: with no Claude version verified, and for Codex always, the host is
  main's. In a verified incarnation a message is never held while staged: the inbox owns the hold. A staged turn
  not one byte of which was accepted when the CLI is seen to begin a turn of its own is taken back (`_unstage`:
  only its own bytes leave the buffer) and put first in the inbox again; once any byte of it is accepted the write
  owns it, as on main. The wait is bounded by the message's own no-progress budget, in COUNTED time: only time in
  which the agent's process is what holds it back (its own turn while nothing on the host side withholds it, or
  its pipe taking none of the staged line), kept across unstages, restagings and a downgrade (`_budget`, `_spent`).
  A first spent budget that was earned under a verified process is one restart, the message never written and kept
  for the next process; a second one fails it. Host-side waits (no process, one going down, the turn gate,
  `may_start`, a stopping group, no slot, a binding to take) count nothing and are main's, without limit.
- A provider with a handshake (`waits_for_ready`) gets `handshake_seconds` for it, not the turn timeout.
- Restart (DESIGN_NOTES "Restarting a running turn"): the caller asks for the turn in flight to be interrupted in-band
  for the message that replaces it (`interrupt_for`); that message itself is the caller's next ordinary message and
  comes through `send` like any other, once the turn has ended. The interrupt is written only while the turn it names
  is the one in flight here and the provider says it can (`providers.modes`); otherwise nothing happens and the turn
  ends by itself. The turn's end says `restarted_by` (labelled INTERRUPTED) only when the interrupt was written
  whole, the provider did not refuse it, and the provider says it ended the turn. Mid-turn steering is not offered
  (R6): no adapter steers.
"""
from __future__ import annotations

import json
import logging
import os
import signal
import time
import itertools
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import providers, runner_preflight, session_access
from .dispatch import (DispatchConfig, _signal_group, _within, child_env, probe_process, process_start,
                       render_argv, spawn_piped, stop_verified, with_writable_roots)
from .providers import Event, ProviderSession, validate_model, validate_session_id

log = logging.getLogger("agent_comms.session_host")

STATE_FILE = "node-sessions.json"   # session id -> {pid, proc_start, ...}: what a restarted host must clean up
PROVIDER_FILE = "node-session-providers.json"   # session id -> the provider's own id for it (Codex's thread id)
HANDSHAKE_SECONDS = 60.0            # a process whose provider has a handshake must be ready within this
SESSIONS_DIR = "sessions"           # <session id>.jsonl (stdout) and <session id>.stderr.log
STATES = ("starting", "idle", "running", "waiting", "ended")
TURN_WITHDRAWN = ("the message was not delivered: before all of it was written to the agent's process, the session "
                  "lost what it needs to take a turn (a child session's parent no longer vouches for it). The "
                  "process was stopped, so the part already written is never completed or acted on")
MESSAGE_ID_MAX = 200
INTERRUPTED = "interrupted by a restart: message {n} replaced it"


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
    - `ended`: the session is over (`detail`); `undelivered` lists the queued messages that were never written.
    A `turn_ended` with `restarted_by`: the interrupt asked for that message (`interrupt_for`) ended the turn, as the
    provider said."""
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
    # `turn_ended` only: the provider TOOK the turn and ended it itself, as its adapter says (providers.Event.answered:
    # a success or an error of its own; not Codex refusing to start it). False: the provider did not take it, or the
    # host gave up on it (the process went, a timeout with no answer, a write that failed), and whether the provider
    # ever read it is not known. Never read off `ok`.
    answered: bool = False
    restarted_by: str | None = None     # turn_ended: the restart whose interrupt the provider said ended this turn


@dataclass
class _Message:
    id: str
    text: str
    kind: str = "user"      # providers.TURN_KINDS: whose turn it is, passed on to the provider with the text
    # Turn correlation's no-progress budget (`_budget`, `_spent`), for a message not one byte of whose line was
    # accepted yet: `counted`, the seconds of COUNTED time so far (None: it has no budget), and `counted_at`, the
    # time of the last pass that ENDED with it in a counted state (None: the last one did not); whether it was in a
    # counted
    # state, during its first budget, under a process that was verified at the time (`earned`: only then is a spent
    # first budget the 7b restart, C7-01); and whether its one 7b restart is spent. On the message itself, so they go
    # when it goes (and survive an unstage, a restage and a downgrade).
    counted: float | None = field(default=None, compare=False)
    counted_at: float | None = field(default=None, compare=False)
    earned: bool = field(default=False, compare=False)
    recovered: bool = field(default=False, compare=False)


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
    # The host's gates (turn gate open, `may_start`) as read ONCE in this pass, for turn correlation's budget and the
    # write timer alike (`_gates`; None: not read yet in this pass). Only ever read for a message with a budget.
    gates: tuple[bool, bool] | None = None
    # Turn correlation (a verified Claude incarnation only): where the staged turn starts in `outbuf` while none of
    # it is accepted, and how many bytes it is (only for an adapter that asks, `barrier_before_first_byte`).
    turn_at: int | None = None
    turn_len: int = 0
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
    # runner as configured); `access`, what it HAS, computed from its command line when it was opened and never
    # changed by anything its process says afterwards (that can only end it).
    asked: str | None = None
    access: str | None = None
    effort: str | None = None
    # How bypass was switched off on its command line (session_access.claude_sealed): None (a `full` launch, or not
    # Claude), "inline" or "merged" (a settings file this host wrote from the runner's own: then every process of
    # the session must first say the key is in force, and no turn is written to it before it has: `checking`).
    seal: str | None = None
    # What a process said is that process's, so each is kept as THE PROCESS that said it and counts only while that
    # one is the session's live process: `sealed_by` (the key is in force), `confirmed_by` (it runs with full
    # access: Claude's `init` says bypassPermissions, Codex's answer to thread/start names dangerFullAccess).
    sealed_by: Any = None
    confirmed_by: Any = None
    # The key that proves a process is this session's (hosted_spawn.bind_key), from the hub with child sessions on:
    # every process of the session gets it in its environment (BIND_ENV), which the board's MCP server inherits.
    # `proc_bind`: the binding the LIVE process was started with. A turn is written only to a process started with
    # the binding the session has now (`unbound`, `_write`): one given or withdrawn since restarts the process first.
    bind: str | None = None
    proc_bind: str | None = None
    linger_logged: float = float("-inf")     # when a group that outlived its SIGKILL was last said in the log
    group_token: Any = None                  # its live process's entry in the node's registry (node_groups)
    # Restart (the module text). `restart_by`: the message the caller asked to interrupt the turn `restart_into` for.
    # `restart_mark`: where the interrupt's bytes sit in `outbuf` (start, length) until all of them reached the
    # process (`restart_written`).
    restart_by: str | None = None
    restart_into: str | None = None
    restart_sent: bool = False
    restart_written: bool = False
    restart_mark: Any = None
    begun: bool = False         # the provider said the turn in flight began (`turn_started`)

    @property
    def unbound(self) -> bool:
        """Its live process was started with another binding than the session has now (given, or withdrawn)."""
        return self.child is not None and self.proc_bind != self.bind

    @property
    def sealed(self) -> bool:
        return self.child is not None and self.sealed_by is self.child

    @property
    def checking(self) -> bool:
        """Its process has been asked whether bypass is switched off in it and has not said so: no turn yet."""
        return self.seal == "merged" and self.child is not None and not self.sealed

    @property
    def full_confirmed(self) -> bool:
        """The session's LIVE process said it runs with full access. Not one that is gone, going down or has
        exited, and not the one before it: nothing has to be cleared for that, the process is simply another."""
        return (self.child is not None and self.confirmed_by is self.child and self.stopping_since is None
                and self.child.poll() is None)

    @property
    def ceiling(self) -> str | None:
        """The most a CHILD of this session may have now. Its own access, except that `full` counts only while the
        process that confirmed it is alive: what a session was started as is not yet what the CLI accepted, and
        what one process accepted says nothing about the next."""
        return "workspace" if self.access == "full" and not self.full_confirmed else self.access

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
        if self.child is not None and self.stopping_since is None and (
                self.checking or (not self.ready and self.provider.waits_for_ready)):
            return "starting"
        return "idle"


DRY_RUN_ID = "00000000-0000-4000-8000-000000000000"
BINDING_CHANGED = ("the session's binding changed while this turn was being written to its process, so it was not "
                   "delivered (the process was stopped; send the message again)")
# The environment variables of a session's binding (hosted_spawn.HOSTED_ENV, KEY_ENV), named here so that the node
# does not import the hub's module.
BIND_ENV = ("AGENT_COMMS_HOSTED_SESSION", "AGENT_COMMS_HOSTED_KEY")


def _toml_item(v: Any) -> str | None:
    """One entry of an `env_vars` list as TOML (a name, or a table of strings), or None when it is neither."""
    if isinstance(v, str):
        return json.dumps(v)
    if isinstance(v, dict) and all(isinstance(k, str) and k.replace("_", "").isalnum() and isinstance(x, str)
                                   for k, x in v.items()):
        return "{" + ", ".join(f"{k} = {json.dumps(x)}" for k, x in v.items()) + "}"
    return None


def bound_template(template: list[str], bind: str | None, codex_config: dict | None = None) -> list[str]:
    """A Codex runner of a session with a binding: Codex starts an MCP server with a small fixed environment, not
    its own, so it is told to pass the two binding variables on to the board's server: `-c
    mcp_servers.agent-comms.env_vars=[...]`, the list in force for that server with the two names added. In force:
    the runner's own last `-c` of that key, else the user's Codex configuration (`codex_config`, its config.toml,
    read only). Seen with codex-cli 0.171.0 (`codex mcp get --json` in a private CODEX_HOME): such a `-c` replaces
    that one key of a server the TOML defines and keeps the rest; a `-c` for a server Codex has no entry for makes an
    entry with no transport, and Codex refuses to start ("invalid transport"). So the server's entry is first
    resolved as Codex would use it (`_effective_server`: config.toml with the runner's `-c` values over it), and the
    list is added only to a stdio server (`command`, no `url`): never to a URL server, also not one a runner's `-c`
    names for something else (a tool's approval), and never to a server that is not there. Without
    it the session's board server has no binding, and the spawn tools refuse it as hosted (never taking it for
    anything else when it can be seen inside the session: hosted_spawn.LOOKS_HOSTED). A Claude runner needs nothing:
    Claude Code gives an MCP server its own environment."""
    if bind is None or not template or os.path.basename(template[0]) != "codex":
        return template
    entry = _effective_server(template, codex_config)
    if entry is None or not isinstance(entry.get("command"), str) or "url" in entry:
        return template         # no such server, one this host cannot read, or not a stdio one: nothing to pass on
    current: Any = entry.get("env_vars", [])
    if not isinstance(current, list):
        return template
    names = {v if isinstance(v, str) else v.get("name") for v in current if isinstance(v, (str, dict))}
    merged = list(current) + [n for n in BIND_ENV if n not in names]
    items = [_toml_item(v) for v in merged]
    if any(x is None for x in items):
        return template
    return [*template, "-c", f"mcp_servers.{session_access.BOARD_SERVER}.env_vars=[{', '.join(items)}]"]


def _effective_server(template: list[str], codex_config: dict | None) -> dict | None:
    """The board server's entry as Codex would use it: the user's config.toml entry (read only), with the runner's
    own `-c mcp_servers.agent-comms...` values (in every form the Codex adapter takes) applied over it in order, as Codex applies them (a value it cannot read
    as TOML is a string). None when there is no such server, or when this host cannot tell what it is (a value it
    cannot place). Its transport is then known: `command` (stdio) or `url` (HTTP), whatever key a `-c` set (a tool's
    approval, say)."""
    import copy
    import tomllib
    servers = (codex_config or {}).get("mcp_servers") if isinstance(codex_config, dict) else None
    base = servers.get(session_access.BOARD_SERVER) if isinstance(servers, dict) else None
    entry: Any = copy.deepcopy(base) if isinstance(base, dict) else None
    name = f"mcp_servers.{session_access.BOARD_SERVER}"
    from .providers.codex import config_values
    try:
        given = config_values(template)     # the adapter's own reading: `-c k=v`, `-ck=v`, `--config=k=v`, ...
    except ValueError:
        return None                         # a runner the adapter refuses anyway: nothing is added to it
    for value in given:
        key, sep, raw = value.partition("=")
        key = key.strip()
        if not sep or not (key == name or key.startswith(name + ".")):
            continue
        try:
            val = tomllib.loads("x = " + raw)["x"]
        except (tomllib.TOMLDecodeError, KeyError):
            val = raw
        path = [p for p in key[len(name):].split(".") if p]
        if not path:
            if not isinstance(val, dict):
                return None
            entry = copy.deepcopy(val)
            continue
        entry = entry if isinstance(entry, dict) else {}
        cur = entry
        for part in path[:-1]:
            nxt = cur.get(part)
            if not isinstance(nxt, dict):
                nxt = cur[part] = {}
            cur = nxt
        cur[path[-1]] = val
    return entry if isinstance(entry, dict) else None


def codex_config_of(env: dict[str, str]) -> dict | None:
    """The Codex configuration a process with this environment would load (CODEX_HOME, else ~/.codex), read only:
    what `bound_template` merges with. None when there is none, or it cannot be read."""
    import tomllib
    home = env.get("CODEX_HOME") or (os.path.join(env["HOME"], ".codex") if env.get("HOME") else None)
    if not home:
        return None
    try:
        with open(os.path.join(home, "config.toml"), "rb") as f:
            data = tomllib.load(f)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def runner_dir(template: list[str]) -> str | None:
    """The one directory a runner can be a session in, when it names one itself (a Codex runner's `--cd` with a
    path: a hosted session works in the directory its node starts it in, so that path is the only one it may be).
    None: any run directory."""
    if template and os.path.basename(template[0]) == "codex":
        from .providers.codex import named_directory
        return named_directory(template)
    return None


def child_words(config: DispatchConfig, template: list[str]) -> list[str]:
    """The access words a CHILD session of this runner can be started at on this machine: for each word, what
    opening such a session does to the template (session_access.prepare and claude_launch, the adapter's own argv),
    tried here without starting or writing anything. Empty: no adapter for its CLI (Codex with `[dispatch.sessions]
    codex` off), or a runner the adapter refuses at every word (a Codex runner with `--approve-for-me`). `full` is
    in it only where this machine allows full access. For a runner that names its own directory (`runner_dir`) the
    words are those it has IN that directory: whether a child would run there is checked when one is asked for."""
    factory = providers.provider_for(template, codex=config.sessions.codex)
    if factory is None:
        return []
    words, cwd = [], runner_dir(template)
    for word in session_access.LEVELS:
        try:
            prep = session_access.prepare(template, word, allow_full=config.sessions.allow_full_access, cwd=cwd)
            launch = prep.template
            if os.path.basename(launch[0]) == "claude":
                launch, _ = session_access.claude_launch(launch, cwd)
            provider = factory()
            if cwd is not None and getattr(provider, "runs_in", None) is not None:
                provider.runs_in(cwd)
            if prep.configure is not None:
                provider.configure(access=prep.configure, effort=None)
            provider.argv(launch, session_id=DRY_RUN_ID, resume=False, model=None)
        except Exception:
            continue
        words.append(word)
    return words


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
        # Asked at the moment a turn is about to be written, and again before every later write of the same turn
        # (a pipe can take it in pieces). False before anything of it is buffered keeps the message in the session's
        # inbox; False once it is on its way withdraws it (TURN_WITHDRAWN). Its caller's rule: a child session's
        # parent must still be what the child needs.
        self.turn_gate: Callable[[Session], bool] | None = None
        # Every process group this host starts, and every one it stops, is in the node's one registry (node_groups:
        # nothing starts in a directory holding a group being stopped). node.Node shares it with its dispatched runs.
        from .node_groups import Groups
        self.groups = Groups()
        self._group_seq = itertools.count(1)
        # Asked at the moment a session's process would be started, for every start (node_sessions sets it: a slot
        # within max_live). False: the message waits in the inbox and the start is asked again on a later pass.
        self.admit: Callable[[Session], bool] | None = None
        # Asked before every process start and every new turn written to a process (node_sessions sets it: not while
        # the node is past its self-stop deadline, until a listing fetched since has been applied). False: nothing
        # starts and no turn is taken from the inbox; the messages wait there, and nothing is failed.
        self.may_start: Callable[[], bool] | None = None
        self._recovered = False
        self._next_message = 0
        # Runner key -> window -> the latest plan usage a session of that runner's provider reported for it (`usage`
        # events), with when (`at`, this host's clock). Read by the node's hello from another thread: under
        # `usage_lock` (`note_usage`).
        import threading
        self.usage: dict[str, dict] = {}
        self.usage_lock = threading.Lock()

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
             cwd: str | None = None, bind: str | None = None) -> Session:
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
        conflict = self.config.launch_blocker(agent, runtime, cwd)     # account folders (agent_accounts.py)
        if conflict:
            raise LookupError(conflict)
        if bind is not None and (not isinstance(bind, str) or len(bind) != 64
                                 or any(ch not in "0123456789abcdef" for ch in bind)):
            raise ValueError("a session's binding key must be 64 hex characters")
        try:
            s = self._open(session_id, agent, runtime, project, cwd, template, factory, model, thread_id, resume,
                           access, effort, bind)
        except Exception:
            self._drop_files(session_id)        # what was written for a session that is not opened after all
            raise
        return s

    def _open(self, session_id, agent, runtime, project, cwd, template, factory, model, thread_id, resume, access,
              effort, bind=None) -> Session:
        # The access word becomes flags here, from this machine's runner and settings (nothing executable came from
        # the hub). Without a word the template is as configured; a Claude runner that names no permission mode
        # gets the standard one pinned, so that no settings file decides it.
        prep = session_access.prepare(template, access, allow_full=self.config.sessions.allow_full_access, cwd=cwd,
                                      board_mcp=lambda configs: self._board_mcp(session_id, configs, cwd))
        template = prep.template
        if (prep.as_configured and prep.configure != "full"
                and runner_preflight.scoped(template, project, self.config.claude_tool_projects)):
            template = runner_preflight.with_denied_tool_instruction(runner_preflight.scoped_template(template))
        if prep.as_configured or prep.granted != "read-only":      # a session narrowed to read-only gets no extra roots
            template = with_writable_roots(template, self.config.extra_roots(template, project))
        # What the session has is computed from the command line it is started with, here and now: no process
        # has to say anything first. (For Claude from the final template, scoping included.)
        label, seal = prep.granted, None
        if os.path.basename(template[0]) == "claude":
            label = session_access.claude_label(template)
            # Last of all, on the command line as it will be run: refused if Claude would not run it as labelled
            # (--restricted beside bypass), and bypass switched off by a settings key unless it is a `full` launch.
            # A runner's own --settings becomes a file written here: Claude takes only its last one.
            template, seal = session_access.claude_launch(
                template, cwd, write=lambda data: self._private_json(session_id, "settings", data))
        s = Session(session_id, agent, runtime, project, cwd, template, factory(), model=model,
                    thread_id=int(thread_id), resume=bool(resume), asked=access, access=label, effort=effort,
                    seal=seal, bind=bind)
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

    PRIVATE_FILES = ("mcp", "settings")     # <session id>.<kind>.json beside the transcript, each mode 600

    def _private_json(self, session_id: str, kind: str, data: dict) -> str:
        """Write `<sessions dir>/<session id>.<kind>.json` (mode 600, atomically) and return its path: a file this
        host makes for one session's command line. Removed when the session is forgotten."""
        self.sessions_dir.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.sessions_dir.mkdir(exist_ok=True, mode=0o700)
        path = self.sessions_dir / f"{validate_session_id(session_id)}.{kind}.json"
        tmp = path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            os.fchmod(fd, 0o600)
            os.write(fd, json.dumps(data).encode())
        finally:
            os.close(fd)
        os.replace(tmp, path)
        return str(path)

    def keep_private_files(self, session_ids) -> None:
        """Remove the files this host made for sessions that are neither named here (the hub no longer lists them)
        nor open: `<session id>.<kind>.json` for the PRIVATE_FILES kinds, and nothing else in the directory. A
        session's files go with it when it is forgotten (`forget`), but one that was closed while this machine's
        node was down is never opened again, and a merged settings file can hold the runner's credentials. Looked
        at when the set of sessions changes (so also once when the node starts)."""
        wanted = frozenset(session_ids) | frozenset(self.sessions)
        if wanted == getattr(self, "_private_kept_for", None):
            return
        self._private_kept_for = wanted
        try:
            names = os.listdir(self.sessions_dir)
        except OSError:
            return
        for name in names:
            sid, _, rest = name.partition(".")
            if (rest in {f"{kind}.json" for kind in self.PRIVATE_FILES} and providers.SESSION_ID_RE.fullmatch(sid)
                    and sid not in wanted):
                try:
                    (self.sessions_dir / name).unlink()
                    log.info("removed %s: its session is gone", name)
                except OSError as e:
                    log.warning("could not remove %s (%s)", name, e)

    def _drop_files(self, session_id: str) -> None:
        for kind in self.PRIVATE_FILES:
            try:
                (self.sessions_dir / f"{session_id}.{kind}.json").unlink()
            except OSError:
                pass

    def _session_writable(self, path: str, cwd: str) -> bool:
        """`path` lies where sessions are meant to write: this session's run directory, any directory this machine
        runs a project in (`[dispatch.worktrees]`, `checkout_roots`), or the directory session and run worktrees
        are made in. By filesystem identity, symlinks resolved."""
        places = [cwd, *self.config.worktrees.values(), *self.config.checkout_roots, self.config.worktree_root()]
        return any(_within(path, place) for place in places if isinstance(place, str) and place)

    def _board_mcp(self, session_id: str, configs: list[str], cwd: str) -> str | None:
        """For a read-only Claude session: a config holding ONLY the board's MCP server, as the runner's own
        `--mcp-config` values define it (each a file, or the JSON itself), written beside the session's transcript
        (mode 600; it names the launcher, never a token). None when the runner names no config with that server:
        the session then has no MCP server at all. Nothing is looked for in the user's own Claude configuration.

        A config FILE inside a checkout or a worktree is not read (`_session_writable`; a relative path is one in
        the run directory): that is where other sessions write, and the entry names a command this session would
        start. Such a runner's read-only child gets no board tools, like one whose runner names no config."""
        entry = None
        for value in configs:
            try:
                if not value.lstrip().startswith("{") and (not os.path.isabs(value)
                                                           or self._session_writable(value, cwd)):
                    log.warning("session %s: the runner's MCP config lies in a checkout or worktree, where sessions "
                                "write; it is not used for a read-only session", session_id)
                    continue
                text = value if value.lstrip().startswith("{") else Path(value).read_text()
                servers = json.loads(text).get("mcpServers")
            except (OSError, ValueError, AttributeError):
                continue
            if isinstance(servers, dict) and isinstance(servers.get(session_access.BOARD_SERVER), dict):
                entry = servers[session_access.BOARD_SERVER]
        if entry is None:
            return None
        return self._private_json(session_id, "mcp", {"mcpServers": {session_access.BOARD_SERVER: entry}})

    def send(self, session_id: str, text: str, *, message_id: str | None = None, kind: str = "user") -> str:
        """Queue a message for the session (first in, first out). Returns its id. `kind` (providers.TURN_KINDS):
        whose turn it is, for the provider (`turn`)."""
        s = self._session(session_id)
        if s.ended:
            raise ValueError("session has ended")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("message text is empty")
        if kind not in providers.TURN_KINDS:
            raise ValueError("unknown message kind")
        if message_id is None:
            self._next_message += 1
            message_id = f"m{self._next_message}"
        if not isinstance(message_id, str) or not 0 < len(message_id) <= MESSAGE_ID_MAX:
            raise ValueError("message id must be a short string")
        s.inbox.append(_Message(message_id, text, kind))
        return message_id

    def interrupt_for(self, session_id: str, into: str, by: str) -> None:
        """Interrupt the turn in flight `into` in-band, for message `by` (which the caller sends afterwards as its
        next ordinary message): written on a pass while `into` is the turn in flight here and the provider can (the
        module text). One at a time; asking again for the same message changes nothing."""
        s = self._session(session_id)
        for v in (into, by):
            if not isinstance(v, str) or not 0 < len(v) <= MESSAGE_ID_MAX:
                raise ValueError("message id must be a short string")
        if s.ended or s.restart_by is not None:
            return
        s.restart_by, s.restart_into, s.restart_sent, s.restart_written = by, into, False, False

    def modes(self, session_id: str) -> tuple[str, ...]:
        """What this session's LIVE process can do with a turn in flight (providers.MODES), as its adapter says now
        (Claude: what this process's `init` said). Nothing while it has no process, or one going down."""
        s = self._session(session_id)
        if s.child is None or s.stopping_since is not None or s.ended:
            return ()
        return providers.modes(s.provider)

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
        self._drop_files(session_id)    # a read-only session's board-only MCP config, a merged settings file

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
        # Every exit first: a leader that went by itself leaving members of its group is registered as stopping
        # (`_reap`) before this pass starts any process, so nothing it starts goes beside such a group.
        for s in list(self.sessions.values()):
            if s.child is not None and s.stopping_since is None:
                try:
                    self._read(s, now, out)
                    self._reap(s, now, out)
                except Exception as e:
                    log.exception("session %s failed", s.session_id)
                    if not s.ended:
                        self._end(s, f"host error: {type(e).__name__}", now, out)
        for s in list(self.sessions.values()):
            try:
                self._pass(s, now, out)
            except Exception as e:
                log.exception("session %s failed", s.session_id)
                if not s.ended:
                    self._end(s, f"host error: {type(e).__name__}", now, out)
        return out

    def _pass(self, s: Session, now: float, out: list[HostEvent]) -> None:
        s.gates = None      # read afresh, once, in this pass (`_gates`)
        self._steps(s, now, out)
        self._mark(s, now)

    def _steps(self, s: Session, now: float, out: list[HostEvent]) -> None:
        if s.child is not None:
            self._read(s, now, out)
            self._reap(s, now, out)
        if s.child is not None and s.stopping_since is None and not s.ended:
            self._take_back(s, now, out)
        acted = not s.ended and self._budget(s, now, out)   # after the reads and the take-back, before any timeout
        if s.child is not None and s.stopping_since is None and not s.ended:
            self._hold_to_timeout(s, now, out)
        if s.child is not None and s.stopping_since is not None:
            self._kill_if_due(s, now)
            self._reap(s, now, out)
            if s.child is not None:
                self._lingering(s, now)
                return      # still going down: nothing new starts beside it
        if acted:
            return      # the budget stopped its process (or failed it) in this pass: what comes next, the next pass
        if not s.ended:
            self._restart_pass(s, now, out)
            self._write(s, now, out)
            if s.child is not None and s.stopping_since is not None:
                self._reap(s, now, out)     # a write that failed stopped it

    def _read(self, s: Session, now: float, out: list[HostEvent]) -> None:
        keep = getattr(s.provider, "transcript_line", None)
        for line in s.child.read_lines():
            # The transcript keeps what the process wrote, except what the provider says must not be kept (Claude's
            # answer to `get_settings`: the settings in force, which can hold credentials): its own record instead.
            self._tee(s, line if keep is None else keep(line))
            if s.ended:
                continue
            events, replies = s.provider.feed(line)
            self._earn(s)       # the budgeted message is under a process verified now (C7-01)
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
            if getattr(s.provider, "reports_sandbox", False):
                # Codex: the answer to thread/start (or resume) names the thread's sandbox. It ends a session
                # whose sandbox is not what it was started as, and is what confirms a full one for its children.
                problem, confirmed = session_access.verify_codex(s.access, ev.mode, s.asked)
                if problem is not None:
                    log.warning("session %s: %s", s.session_id, problem)
                    self._end(s, problem, now, out)
                    return
                s.confirmed_by = s.child if confirmed else None
            s.ready, s.provider_id = True, ev.provider_id
            if not s.provider.waits_for_ready:
                s.resume = True     # Claude: `ready` comes with the first turn, and the session exists from then on
            if not s.busy and not s.checking:   # (a process still being asked about its settings stays on the clock)
                s.busy_since = None
            self._save_state()
        elif ev.kind == "turn_started":
            # The kill switch. The session's access was decided from its command line before it started; what the
            # process says it really runs this turn with can only end it: bypassed where it was not started as
            # full, not bypassed where it was, more than read-only where that was asked, or nothing verifiable where
            # the access asked for depends on it.
            problem = (session_access.verify(s.asked, s.access, ev.mode, ev.tools)
                       if getattr(s.provider, "verifies_mode", False) else None)
            if problem is not None:
                log.warning("session %s: %s", s.session_id, problem)
                self._end(s, problem, now, out)
                return
            if s.access == "full" and ev.mode == "bypassPermissions":
                s.confirmed_by = s.child    # Claude took the mode: while THIS process lives a child may be `full`
            if ev.turn == "other":
                # Not the turn in flight (the adapter's evidence, providers.OWNERS: a turn the CLI started itself, or
                # another `init` inside the turn's own bracket): its access was checked above, and that is all.
                log.info("session %s: an init that is no message's turn (checked, nothing started)", s.session_id)
                return
            if s.in_flight is not None:
                s.begun = True      # the provider began the turn in flight (its `init`: Claude's capabilities)
                out.append(HostEvent(s.session_id, "turn_started", s.in_flight.id))
        elif ev.kind == "sealed":
            # The process's own word on whether bypass is switched off in it (asked when it started, for a session
            # started from a settings file this host merged: Claude ignores a file it finds invalid, whole). Until
            # it says yes nothing else is written to it (`checking`); if it says no the session ends, and the
            # message that was waiting is reported as never delivered.
            if not ev.ok:
                log.warning("session %s: %s", s.session_id, session_access.NOT_SEALED)
                self._end(s, session_access.NOT_SEALED, now, out)
                return
            s.sealed_by = s.child
            if not s.busy:
                s.busy_since = None     # the check is over; the turn's own clock starts when it is written
        elif ev.kind == "turn_ended":
            if ev.turn == "other":
                # A turn the CLI started itself ended, or a result that is no message's (providers.OWNERS): the turn
                # in flight, if any, is not settled by it (Claude turn correlation).
                log.info("session %s: a turn that is no message's ended; the turn in flight goes on", s.session_id)
                return
            if s.in_flight is None:
                log.warning("session %s: a turn ended that the host did not start", s.session_id)
                return
            msg, s.in_flight, s.busy_since = s.in_flight, None, None
            timed_out, s.interrupted_at = s.interrupted_at is not None, None
            s.denials, s.waiting = ev.denials, bool(ev.denials)
            ok = bool(ev.ok) and not timed_out
            self._unwritten(s)      # an interrupt not one byte of which reached the process is never written
            # Interrupted by the restart only when its interrupt was written whole and the provider says that ended
            # the turn (an abort it did not refuse, `interrupted`): a turn that finished first, or failed for another
            # reason, keeps its own outcome and the restart's message simply runs next.
            check = getattr(s.provider, "interrupted", None)
            by_restart = (s.restart_by is not None and s.restart_into == msg.id and s.restart_written
                          and not timed_out and check is not None and bool(check(ev)))
            detail = (self._timeout_detail() if timed_out else INTERRUPTED.format(n=s.restart_by) if by_restart
                      else ev.detail)
            out.append(HostEvent(s.session_id, "turn_ended", msg.id, ok=ok, denials=ev.denials, meta=dict(ev.meta),
                                 detail=detail, text=ev.text,
                                 answered=bool(ev.answered),     # the adapter's word: the provider took the turn
                                 restarted_by=s.restart_by if by_restart else None))
            self._forget_restart(s)     # the old turn has settled: the restart's message is the next ordinary one
            self._save_state()
        elif ev.kind == "approval_requested":
            s.denials, s.waiting = ev.denials, True
            out.append(HostEvent(s.session_id, "approval_requested", s.in_flight.id if s.in_flight else None,
                                 denials=ev.denials))
        elif ev.kind == "usage":
            # What the provider said about its plan's usage (providers.KINDS): each window's latest per runner, with
            # its own time, for the node's hello (account_pools.py). Advisory: nothing about the session or its turn
            # changes, and nothing here can fail (`note_usage`).
            self.note_usage(self.config.runner_key(s.agent, s.runtime), ev.usage, now)
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

    def note_usage(self, key: str | None, observations, now: float) -> None:
        """Keep each window's newest observation for runner `key` (`usage`: key -> window -> observation | {at}),
        each through providers.clean_usage, at most providers.USAGE_WINDOWS_MAX windows (the newest). It never
        raises: usage is advisory (account_pools.py, rule (b))."""
        try:
            if key is None or not isinstance(observations, list):
                return
            with self.usage_lock:
                windows = dict(self.usage.get(key) or {})
                for obs in observations[:providers.USAGE_WINDOWS_MAX]:
                    clean = providers.clean_usage(obs)
                    if clean is not None:
                        windows[clean["window"] or "-"] = clean | {"at": now}
                newest = sorted(windows.items(), key=lambda kv: -kv[1]["at"])[:providers.USAGE_WINDOWS_MAX]
                self.usage[key] = dict(newest)
        except Exception:
            log.debug("a usage observation could not be kept; it is advisory and was dropped", exc_info=True)

    def _reap(self, s: Session, now: float, out: list[HostEvent]) -> None:
        """The process is let go of only once its whole process group is gone, however its leader ended: stopped by
        the host (a close, a timeout, a withdrawn turn, a failed write), or on its own (it exited, or crashed). A
        leader that exits on its own while members of its group are left is treated as an exit at once (its work
        fails as below), and its group is then stopped the way the stop path stops one (SIGTERM, SIGKILL after the
        grace): the handle is kept until the group is gone, and so is its entry in the node's registry (node_groups):
        nothing this node starts goes into that directory meanwhile, a session's process or a dispatched run."""
        code = s.child.poll()
        if code is None:
            return
        if self.group_alive(s.child.pid):
            if s.stopping_since is None:
                # It went by itself, leaving members of its group behind (a tool it started, say).
                self._read(s, now, out)
                if not s.ready:
                    self._correct_start_mode(s)
                self._went_by_itself(s, code, s.ready, s.checking, out)
                log.warning("session %s: its process exited (code %s) and left members of its group: stopping them",
                            s.session_id, code)
                self._stop(s, now)
            return      # its leader is gone, but not its group: nothing starts beside that, nothing is reported reaped
        self._read(s, now, out)     # what it wrote on its way out (a result that just made it still counts)
        if not s.ready:
            self._correct_start_mode(s)
        was_ready, was_checking = s.ready, s.checking
        child, s.child, s.proc_start, s.ready = s.child, None, None, False
        self.groups.gone(s.group_token)      # the whole group is gone
        s.group_token = None
        s.sealed_by = s.confirmed_by = None     # what that process said went with it
        stopped, s.stopping_since, s.killed, s.interrupted_at = s.stopping_since is not None, None, False, None
        s.outbuf.clear()
        close = getattr(child, "close", None)
        if close is not None:
            close()
        if s.transcript_fd is not None:
            os.close(s.transcript_fd)
            s.transcript_fd = None
        if not stopped:
            self._went_by_itself(s, code, was_ready, was_checking, out)
        self._forget_restart(s)     # nothing is left to interrupt
        s.held_since = None
        self._save_state()

    LINGER_LOG_SECONDS = 60.0       # how often a process group that outlives its SIGKILL is said again

    def _lingering(self, s: Session, now: float) -> None:
        """A process group that is still there well after its SIGKILL (a member in uninterruptible sleep, say) keeps
        this session's next process, and any run this node would start in its directory, waiting. That shows here, in
        the node's log, not on the hub (an owner decision, review round 6): one line, repeated at intervals."""
        if not s.killed or now - (s.stopping_since or now) < 2 * self.kill_grace:
            return
        if now - s.linger_logged >= self.LINGER_LOG_SECONDS:
            s.linger_logged = now
            log.warning("session %s: its process group (pid %s) in %s is still there %.0f s after it was stopped and "
                        "killed; nothing starts in that directory on this node until it is gone", s.session_id,
                        getattr(s.child, "pid", None), s.cwd, now - (s.stopping_since or now))

    def _went_by_itself(self, s: Session, code: int, was_ready: bool, was_checking: bool,
                        out: list[HostEvent]) -> None:
        """Its process went by itself. The turn it had is lost; the next message starts a fresh process."""
        if s.ended:
            return
        if s.in_flight is not None:
            self._fail(s, f"the process exited (code {code}) before the turn ended", out)
        elif s.writing is not None:
            self._fail(s, f"the process exited (code {code}) before the message was written", out)
        elif s.inbox and ((s.provider.waits_for_ready and not was_ready) or was_checking):
            # It went during its handshake (or before it answered the question it was asked at its start). The
            # message that made it start fails with it: otherwise a process that can never start (a runner flag
            # Codex refuses) would be started again on every pass.
            self._fail_start(s, f"the process exited (code {code}) before it was ready", out)
        s.busy_since = None

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

    def _take_back(self, s: Session, now: float, out: list[HostEvent]) -> None:
        if s.writing is not None and s.turn_at is not None and self.holding(s):
            # A message is never held while staged (a verified Claude incarnation; `turn_at` is tracked for no other):
            # not one byte of it accepted and the CLI runs a turn of its own, so it goes back to the inbox. Seen here,
            # after the pass's reads and before its budget, any timeout or any write is looked at (`_steps`).
            self._unstage(s, now, out)

    def _hold_to_timeout(self, s: Session, now: float, out: list[HostEvent]) -> None:
        if (s.writing is not None and s.turn_at is not None and s.writing.counted is not None and s.writing.earned
                and self._counting(s, s.writing)):
            # ONE clock at every pass for a message staged with not one byte accepted (review rounds 11 and 12): an
            # EARNED message's budget while its counted state holds (`_budget`, earlier in this pass; not this
            # staging's write timer, C5-01, C6-01); otherwise main's write timer from the staging, below, exactly as
            # on main: while the host withholds it (`may_start` false: a latch the watchdog does not see), with no
            # budget, or with one not earned (a candidate's, an other version's: main's path, entirely, C7-01). The
            # write timer never restarts while the line stays staged, so it never gets more than that timer plus its
            # counted budgets; and the gates are the pass's one reading (`_gates`), the budget's too.
            return
        if s.busy_since is None:
            return
        if s.interrupted_at is not None:
            if now - s.interrupted_at >= self.kill_grace:     # the provider's own interrupt did not end the turn
                self._fail(s, self._timeout_detail(), out)
                self._stop(s, now)
            return
        if not s.busy and (not s.ready or s.checking):
            # A handshake, not a turn. It has its own short limit, so a process that never becomes ready fails the
            # message waiting on it now and not after a whole turn's timeout.
            if now - s.busy_since < self.handshake_seconds:
                return
            if s.checking:
                # It never said whether bypass is switched off in it: not accepted where it may not be. Nothing
                # was written to it but the question.
                log.warning("session %s: %s", s.session_id, session_access.SEAL_UNVERIFIED)
                self._end(s, session_access.SEAL_UNVERIFIED, now, out)
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

    def _unwritten(self, s: Session) -> None:
        """At the end of the turn a restart's interrupt was for: if not one byte of it reached the process (a pipe that
        took nothing, `restart_mark`), it is taken out of the buffer and never written, and the turn keeps its own
        outcome."""
        mark = s.restart_mark
        if mark is None or mark[0] < 0:
            return
        start, length = mark
        del s.outbuf[start:start + length]
        s.restart_mark = None
        s.restart_written = False

    @staticmethod
    def _forget_restart(s: Session) -> None:
        s.restart_by = s.restart_into = s.restart_mark = None
        s.restart_sent = s.restart_written = False

    def _fail(self, s: Session, detail: str, out: list[HostEvent]) -> None:
        """Give up on the session's turn: failed, and never written again (a restart asked for it: no link)."""
        msg, delivered = (s.in_flight, True) if s.in_flight is not None else (s.writing, False)
        s.in_flight = s.writing = s.busy_since = None
        if msg is not None:
            out.append(HostEvent(s.session_id, "turn_ended", msg.id, ok=False, delivered=delivered, detail=detail))
        self._forget_restart(s)

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

    @staticmethod
    def _correlated(s: Session) -> bool:
        """The session's live process is a Claude incarnation its adapter verified (turn correlation): the only one
        whose host behaviour differs from main's."""
        return s.child is not None and getattr(s.provider, "verified", False) is True

    # What was observed, not why (review round 8, C8-03): a held message and a pipe that takes nothing look alike.
    HELD = ("not one byte of the message was accepted by the agent's process in two periods of {m:g} minutes; the "
            "message was not written")

    def _barrier(self, s: Session, now: float, out: list[HostEvent]) -> bool:
        """The write barrier of a verified Claude incarnation (`holds_turns`: a turn the CLI started itself runs),
        owned by the inbox: True, its first message waits (never one being written: `_unstage`). The wait is
        bounded by the message's one no-progress budget, in counted time (`_budget`, `_spent`), also on the binding
        path (`unbound`), where only the CLI's own turn stands between the message and the restart it needs."""
        if not self.holding(s):
            return False
        msg = s.inbox[0]
        if msg.counted is None:
            msg.counted = 0.0
        self._earn(s)
        return True

    def _holder(self, s: Session) -> _Message | None:
        """The message whose budget can run now: the one staged with not one byte of its line accepted, or the first
        in the inbox with nothing being written or in flight."""
        if s.writing is not None:
            return s.writing if s.turn_at is not None else None
        return s.inbox[0] if s.inbox and s.in_flight is None else None

    def _counting(self, s: Session, msg: _Message) -> bool:
        """Is `msg` in a COUNTED state now, one in which the agent's process is what holds it back? Exactly two, in a
        live process and with nothing on the host side withholding it (`_withheld`, ONE list for both): (b) it is
        staged with not one byte of its line accepted, and its process was started with the binding the session has
        now (on a new binding main withdraws the staged line, BINDING_CHANGED, later in this pass); (a) it is first in
        the inbox and the CLI's own turn is open (`holding`). Nothing else counts: no process, one going down (the
        restart for a new binding included), the turn gate closed, `may_start` false, a handshake or settings check
        pending, a stopping group, no slot. A new binding while the CLI's turn runs is (a): the host would restart
        the process for it at once, and waits only because the agent runs a turn of its own (and main's Claude path
        waits for no such turn at all). (The host's gates are asked last, and only for a message with a budget:
        never with VERIFIED empty.)"""
        if s.child is None or s.stopping_since is not None or s.ended:
            return False
        if s.writing is not None:
            if s.writing is not msg or s.turn_at is None or s.unbound:
                return False
        elif s.in_flight is not None or not s.inbox or s.inbox[0] is not msg or not self.holding(s):
            return False
        return not self._withheld(s)

    def _withheld(self, s: Session) -> bool:
        """The host-side conditions under which the host itself withholds a session's next (or staged) message, for
        both counted states (`_counting`): a handshake or a settings check pending, the turn gate closed (main
        withdraws a staged line then, TURN_WITHDRAWN, later in this pass), `may_start` false."""
        turn_gate, may_start = self._gates(s)
        return bool(s.checking or (s.provider.waits_for_ready and not s.ready) or not turn_gate or not may_start)

    def _gates(self, s: Session) -> tuple[bool, bool]:
        """The turn gate and `may_start`, read once per pass (review round 12): the budget, its end mark and the
        write timer's choice of clock all see the same reading, so a gate that closes in the middle of a pass cannot
        let the budget count before the write timer is asked."""
        if s.gates is None:
            s.gates = (self.turn_gate is None or bool(self.turn_gate(s)),
                       self.may_start is None or bool(self.may_start()))
        return s.gates

    def _budget(self, s: Session, now: float, out: list[HostEvent]) -> bool:
        """THE one place that adds counted time, once every pass, after its reads and before any timeout or write
        decision (and after a staged message the CLI's new turn takes back, `_take_back`, so the hold, not the
        staging, is what is judged). The interval since the last pass counts only if the message was in a counted
        state at BOTH its
        ends: at the end of the last pass (`counted_at`, set by `_mark`) and now, after this pass's reads
        (`_counting`). Conservative: time in which the host may have withheld it, or the CLI's turn may have ended
        (the pass that first reads its result sees it ended), is never counted; it loses at most one pass interval
        each time the message enters or leaves a counted state. Then `_spent`, the one comparison, in that counted
        state (a live process exists): a budget is acted on at the pass at which it is reached, and only in a
        counted state, so the budget never stops a process nor fails a message while the host withholds it. True
        when it acted (the 7b restart, or the failure): nothing else is written in that pass."""
        msg = self._holder(s)
        if msg is None or msg.counted is None or not self._counting(s, msg):
            return False
        if msg.counted_at is not None:
            msg.counted += now - msg.counted_at
        self._earn(s)
        return self._spent(s, msg, now, out)

    def _mark(self, s: Session, now: float) -> None:
        """The end of every pass: one end of the next interval (`_budget`). The message whose budget can run is in a
        counted state as the pass ends, or it is not; a message with no budget, or none, leaves nothing."""
        msg = self._holder(s)
        if msg is not None and msg.counted is not None:
            msg.counted_at = now if self._counting(s, msg) else None

    def _earn(self, s: Session) -> None:
        """Where a process's verification is observed (each line it writes, the hold, a staging, each pass): the
        message whose budget runs is in a counted state under a process that is verified now, so a spent first
        budget of it is the 7b restart (C7-01). A later downgrade does not take that away; a candidate that never
        verifies, or turns out another version, never gives it; nor does a wait the host imposes (not counted)."""
        if not self._correlated(s):
            return
        msg = self._holder(s)
        if msg is not None and msg.counted is not None and self._counting(s, msg):
            msg.earned = True

    def _spent(self, s: Session, msg: _Message, now: float, out: list[HostEvent]) -> bool:
        """THE one comparison of a message's no-progress budget with the turn timeout, made only by `_budget`, in a
        counted state (a live process exists). A message gets a budget once it is held, or staged, in a process
        that can be verified (`counted`, from 0), and from then on that budget governs it until the first byte of
        its line is accepted (`_write`), whatever the process's verification state is meanwhile; with VERIFIED
        empty no message ever has one, and its path is main's. It counts only COUNTED time (`_counting`), kept
        across unstages, restagings, the end of the CLI's turns and a downgrade. When it reaches the timeout:
        - a budget not EARNED (`earned`: never in a counted state under a process verified at the time; a
          candidate that never verified, or reported another version) is never acted on: False. Its message is on
          main's path, entirely (main's write timer is its one clock, `_hold_to_timeout`; review round 12): the
          budget only keeps its byte span, so the line can be taken back should the process verify, and its count
          (C7-01's drop at the deadline is a consequence of that and is gone);
        - an earned first budget is the 7b restart: True; the message stays (or goes back, unstaged) first in the
          inbox, its second budget starts from nothing, and its process is stopped;
        - a second budget: True; it fails as main's `_held` fails it, never written.
        So an unwritten budgeted message accumulates at most two timeouts of counted time (each plus at most one
        pass), and the first one never fails it; its wall time is unbounded only by host-side waits, as on main."""
        if msg.counted is None or msg.counted < self.timeout_minutes * 60:
            return False
        if not msg.earned:
            return False        # main's path, entirely: its write timer decides (C7-01, review round 12)
        if s.writing is msg:
            self._unstage(s, now, out)      # exactly its own bytes leave the buffer: it is first in the inbox again
        if not msg.recovered:
            msg.recovered, msg.counted, msg.counted_at = True, 0.0, None
            log.warning("session %s: message %s made no progress for %g minutes (not one byte of it accepted on "
                        "stdin); its process is stopped and the message, never written, waits for the next one",
                        s.session_id, msg.id, self.timeout_minutes)
            self._stop(s, now)
            self._save_state()
            return True
        log.warning("session %s: message %s made no progress for a second %g minutes (not one byte of it accepted "
                    "on stdin); it fails, never written", s.session_id, msg.id, self.timeout_minutes)
        s.writing = s.inbox.popleft()
        self._fail(s, self.HELD.format(m=self.timeout_minutes), out)
        self._stop(s, now)
        return True

    def _unstage(self, s: Session, now: float, out: list[HostEvent]) -> None:
        """A staged turn not one byte of which was accepted, and the CLI is seen to run a turn of its own: the
        message goes back FIRST in the inbox, ahead of any successor, and exactly its own bytes leave the buffer
        (`turn_at`, `turn_len`: the rest of a partly written control frame before it, and anything queued after
        it, stay exactly as they were, and still go). The adapter keeps no trace of it (`unstaged`), so a later
        staging is a first one, with its own write clock; the inbox owns the wait from now (`_barrier`, at the
        pass's write decision, as for any message in the inbox), within the message's one budget (`_spent`)."""
        msg = s.writing
        del s.outbuf[s.turn_at:s.turn_at + s.turn_len]
        s.writing = s.busy_since = s.turn_at = None
        s.inbox.appendleft(msg)
        s.provider.unstaged()
        log.info("session %s: message %s, not one byte of it written, waits again: the agent began a turn of its own",
                 s.session_id, msg.id)
        self._save_state()

    def _stop(self, s: Session, now: float) -> None:
        if s.child is not None and s.stopping_since is None:
            s.stopping_since = now
            self.groups.stopping(s.group_token)     # from now on nothing starts in its directory until it is gone
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
        codex = s.bind is not None and os.path.basename(s.template[0]) == "codex"
        argv = s.provider.argv(bound_template(s.template, s.bind, codex_config_of(self._env(s)) if codex else None),
                               session_id=s.session_id, resume=s.resume, model=s.model)
        return render_argv(argv, prompt="", project=s.cwd, thread_id=s.thread_id)

    def _env(self, s: Session) -> dict[str, str]:
        """The process's environment: a dispatched run's (dispatch.child_env), and the session's binding when it has
        one (hosted_spawn: the session id and its key, which the board's MCP server inherits and passes on)."""
        env = child_env(s.agent, self.config, runtime=s.runtime)
        if s.bind is not None:
            env |= {BIND_ENV[0]: s.session_id, BIND_ENV[1]: s.bind}
        return env

    def cancel(self, session_id: str, message_id: str, detail: str) -> list[HostEvent]:
        """The session that started this turn cancelled it (hosted_spawn.cancel): a turn in flight, or one being
        written, fails with `detail` and the process is stopped (the next message starts one that resumes the
        conversation; a turn half written is never completed); one still in the inbox is dropped, never written.
        Anything else (the turn is over, or another one) is left alone."""
        s, out = self._session(session_id), []
        now = self.clock()
        # A restart's message that is cancelled no longer asks for the interrupt: one not written yet never is, and the
        # turn keeps its own outcome either way. (The message itself is the caller's, not here.)
        if s.restart_by == message_id:
            if not s.restart_written and s.restart_mark is not None:
                self._unwritten(s)
            self._forget_restart(s)
            return out
        if (s.in_flight is not None and s.in_flight.id == message_id) or (
                s.writing is not None and s.writing.id == message_id):
            if s.writing is not None:
                s.outbuf = bytearray()
            self._fail(s, detail, out)
            self._stop(s, now)
            return out
        for box in (s.inbox,):
            for m in list(box):
                if m.id == message_id:
                    box.remove(m)
                    out.append(HostEvent(s.session_id, "turn_ended", m.id, ok=False, delivered=False, detail=detail))
        return out

    def _restart_pass(self, s: Session, now: float, out: list[HostEvent]) -> None:
        """Write the interrupt asked for (`interrupt_for`; the module text): while its turn is the one in flight here,
        once the provider can. A turn no longer in flight, or a CLI that cannot, interrupts nothing: the request goes,
        and the turn ends by itself."""
        if s.restart_by is None or s.restart_sent:
            return
        if s.writing is not None and s.writing.id == s.restart_into:
            return      # its turn is still being written
        if s.in_flight is None or s.in_flight.id != s.restart_into:
            log.info("session %s: message %s is not in flight here: nothing to interrupt for message %s",
                     s.session_id, s.restart_into, s.restart_by)
            self._forget_restart(s)
            return
        write = getattr(s.provider, "interrupt_turn", None) if "restart" in providers.modes(s.provider) else None
        if write is None:
            if s.begun:     # what this process can do is known once it began the turn (Claude: its `init`)
                log.info("session %s: its agent CLI cannot interrupt a turn in-band: message %s runs after it",
                         s.session_id, s.restart_by)
                self._forget_restart(s)
            return
        if s.child is None or s.stopping_since is not None:
            return
        takes = getattr(s.provider, "takes_interrupt", None)
        if takes is not None and not takes():
            return      # not yet, as the provider says (Claude: before this turn's `init`; providers.takes_interrupt)
        data = write()
        if data is None:
            return      # the provider cannot take it yet (Codex before it named the turn)
        start = len(s.outbuf)
        for b in data:
            s.outbuf += b
        s.restart_sent, s.restart_mark = True, (start, len(s.outbuf) - start)

    def _spawn(self, s: Session, now: float, out: list[HostEvent]) -> None:
        try:
            # Account folders at every process start, not only at `open`: the folder as it is now, and the directory
            # the process starts in (agent_accounts.py). A start refused here ends the session with the reason.
            argv = self._argv(s)
            # Account folders at every process start, on the very argv the provider will execute (its writable roots
            # included) and the directory it starts in, with the folder as it is now (agent_accounts.py).
            blocked = self.config.launch_blocker(s.agent, s.runtime, s.cwd, argv=argv)
            if blocked:
                raise LookupError(blocked)
            self.sessions_dir.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            self.sessions_dir.mkdir(exist_ok=True, mode=0o700)
            try:
                s.stderr_from = os.path.getsize(self.sessions_dir / f"{s.session_id}.stderr.log")
            except OSError:
                s.stderr_from = 0
            token = ("session", s.session_id, next(self._group_seq))
            started, child = self.groups.start(s.cwd, token, lambda: self.spawner(
                argv, cwd=s.cwd, env=self._env(s), log_path=self.sessions_dir / f"{s.session_id}.stderr.log"))
            if not started:
                # A process group of this node is being stopped in that directory (a session's, or a dispatched
                # run's): nothing starts beside it. The message waits in the inbox.
                return
            s.group_token = token
        except Exception as e:
            log.warning("could not start session %s: %s", s.session_id, e)
            self._end(s, f"could not start: {type(e).__name__}: {e}", now, out)
            return
        s.child, s.ready, s.stopping_since, s.killed = child, False, None, False
        s.proc_bind = s.bind
        try:
            s.proc_start = self.start_of(child.pid)
        except Exception:
            s.proc_start = None
        s.outbuf = bytearray(b"".join(s.provider.on_open()))
        s.busy_since = now if s.provider.waits_for_ready or s.checking else None     # a handshake has its own limit
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
            if self.may_start is not None and not self.may_start():
                return      # the node is past its self-stop deadline: nothing starts (node_sessions `_may_start`)
            if self.groups.blocked(s.cwd):
                return      # its checkout holds a stopping group: it waits for that first (and stops nobody for a slot)
            if self.admit is not None and not self.admit(s):
                return      # no slot now (max_live): it waits, and asks again
            self._spawn(s, now, out)
            if s.child is None:
                return
        if not s.busy and s.inbox and s.unbound:
            # The write boundary for the binding: a message is never written to a process started without the binding
            # the session has now (or with one it no longer has), also one that came into the inbox during the
            # process's handshake. The process is stopped (not while its provider runs a turn of its own) and the
            # message stays in the inbox for the next one, which gets the binding as it is then.
            if self._correlated(s):
                if not self._barrier(s, now, out):      # with its deadline (main's path below has none)
                    log.info("session %s: its process is restarted to take its binding as it is now", s.session_id)
                    self._stop(s, now)
            elif not self.holding(s):
                log.info("session %s: its process is restarted to take its binding as it is now", s.session_id)
                self._stop(s, now)
            return
        if (not s.busy and s.inbox and (s.ready or not s.provider.waits_for_ready) and not s.checking
                and (self.may_start is None or self.may_start())      # ... and no new turn is written
                and (self.turn_gate is None or self.turn_gate(s))
                and not (self._barrier(s, now, out) if self._correlated(s) else self._held(s, now, out))):
            s.writing = s.inbox.popleft()
            s.waiting, s.denials, s.busy_since = False, (), now
            if s.provider.waits_for_ready and not s.resume:
                # The first turn for a session the provider only issued an id for (Codex's thread). From the moment
                # its bytes may be in the pipe the provider may have recorded the turn, so that the session is to be
                # RESUMED is on record first: the flag and the id (PROVIDER_FILE, which `open` trusts after a
                # restart). If the provider never got the turn, the resume is refused and that is said out loud.
                s.resume, s.fresh_start = True, False
                self._record_provider_id(s.session_id, s.provider_id)
            # Where the turn starts, while none of it is written: only for an adapter that asks (Claude while it
            # can be verified), so main's write path is untouched otherwise.
            # ... and for a message with a budget, whatever the process's verification is now: its budget governs it
            # until a byte of it is accepted (C6-01).
            track = getattr(s.provider, "barrier_before_first_byte", False) or s.writing.counted is not None
            s.turn_at = len(s.outbuf) if track else None
            for data in s.provider.turn(s.writing.text, kind=s.writing.kind):
                s.outbuf += data
            if s.turn_at is not None:
                s.turn_len = len(s.outbuf) - s.turn_at
                if s.writing.counted is None:
                    s.writing.counted = 0.0             # its no-progress budget starts (`_budget`)
                self._earn(s)
            self._save_state()      # the turn is on record before its first byte is written
        if s.writing is not None and s.unbound:
            # The binding changed between two writes of one turn (a pipe can take a turn in pieces, over several
            # passes): the rest never goes to the process started with the old one. As for the turn gate below: the
            # half-written line is not completed, the process is stopped, and the message fails as never delivered
            # (BINDING_CHANGED), for its sender to send again; it is not replayed, since part of it reached a process.
            log.warning("session %s: its binding changed while a turn was being written", s.session_id)
            s.outbuf.clear()
            self._fail(s, BINDING_CHANGED, out)
            self._stop(s, now)
            return
        if s.writing is not None and self.turn_gate is not None and not self.turn_gate(s):
            # The gate is asked before EVERY write of a turn, not only before the first: a pipe may take a turn in
            # pieces, over several passes, and what the session needs can go between two of them. A half-written
            # line must not be completed later, nor left for the process to act on: the process is stopped and the
            # message fails as never delivered. The session stays, with no process; it starts another for its next
            # message, when it may take one again.
            log.warning("session %s: a turn was withdrawn while it was being written", s.session_id)
            s.outbuf.clear()
            self._fail(s, TURN_WITHDRAWN, out)
            self._stop(s, now)
            return
        if s.outbuf:
            try:
                n = s.child.write(bytes(s.outbuf))
            except OSError as e:
                # The process no longer reads. A message that was not fully written never reached it; a turn in
                # flight (an answer to the process could not be written) is lost with the process. During a
                # handshake the message that made the process start is still in the inbox: it fails here too.
                s.outbuf.clear()
                detail = f"could not write to the process: {type(e).__name__}"
                if (s.provider.waits_for_ready and not s.ready) or s.checking:
                    self._fail_start(s, detail, out)
                else:
                    self._fail(s, detail, out)
                self._stop(s, now)
                return
            del s.outbuf[:n]
            if s.turn_at is not None:
                # None once any byte of the turn is accepted: from then on the write owns it, as on main (and its
                # no-progress budget is over)
                s.turn_at = s.turn_at - n if n <= s.turn_at else None
                if s.turn_at is None and s.writing is not None:
                    s.writing.counted = s.writing.counted_at = None
            if s.restart_mark is not None:
                start, length = s.restart_mark
                s.restart_mark = (start - n, length)    # where its bytes are now; below zero: some are written
        if s.restart_mark is not None and s.restart_mark[0] + s.restart_mark[1] <= 0:
            s.restart_written = True    # the whole interrupt is in the pipe: an abort from now on is its doing
            s.restart_mark = None
        if not s.outbuf and s.writing is not None:
            s.in_flight, s.writing, s.begun, s.turn_at = s.writing, None, False, None
            if getattr(s.provider, "barrier_before_first_byte", False):
                s.provider.delivered()      # every byte of it is in the pipe: only now can rule 10 settle it
            out.append(HostEvent(s.session_id, "delivered", s.in_flight.id))

    # ------------------------------------------------------------ stopping a process, keeping the session

    def park(self, session_id: str) -> bool:
        """Stop an idle session's process (an idle limit, or room for another session). The session stays: its next
        message starts a process that resumes it. False, and nothing is touched, when it has a turn or a message
        waiting, no process, or one already going down, or when its provider is running a turn of its own in it:
        nothing running is interrupted."""
        s = self._session(session_id)
        if (s.child is None or s.ended or s.busy or s.inbox or s.stopping_since is not None or self.holding(s)):
            return False
        self._stop(s, self.clock())
        return True

    def _kill_if_due(self, s: Session, now: float) -> bool:
        if s.child is not None and s.stopping_since is not None and not s.killed \
                and now - s.stopping_since >= self.kill_grace:
            s.killed = True
            s.child.kill()
            return True
        return False

    def kill_due(self) -> list[tuple[str, float]]:
        """SIGKILL for every process group whose grace after its SIGTERM is over, as a pass does (`_pass`). Called
        between passes too (node_sessions' deadline watchdog), so that a kill never waits for a pass that is held up
        by a hub call. Returns (session id, when its kill was due) for each one killed."""
        now = self.clock()
        return [(s.session_id, s.stopping_since + self.kill_grace) for s in list(self.sessions.values())
                if self._kill_if_due(s, now)]

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
            self.groups.gone(s.group_token)
            s.group_token = None
            s.outbuf.clear()
        for s in self.sessions.values():
            s.sealed_by = s.confirmed_by = None     # this host is done: what its processes said goes with it
        self._save_state()
        return out
