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
  its pipe taking none of the staged line), kept across unstages, restagings and a downgrade (`_decide`, `_spent`).
  A first spent budget that was earned under a verified process is one restart, the message never written and kept
  for the next process; a second one fails it. Host-side waits (no process, one going down, the turn gate,
  `may_start`, a stopping group, no slot, a binding to take) count nothing and are main's, without limit.
- A provider with a handshake (`waits_for_ready`) gets `handshake_seconds` for it, not the turn timeout.
- Restart (DESIGN_NOTES "Restarting a running turn"): the caller asks for the turn in flight to be interrupted in-band
  for the message that replaces it (`interrupt_for`); that message itself is the caller's next ordinary message and
  comes through `send` like any other, once the turn has ended. The interrupt is written only while the turn it names
  is the one in flight here and the provider says it can (`providers.modes`); otherwise nothing happens and the turn
  ends by itself. The turn's end says `restarted_by` (labelled INTERRUPTED) only when the interrupt was written
  whole, the provider did not refuse it, and the provider says it ended the turn.
- Mid-turn steering (DESIGN_NOTES "Mid-turn steering, loose contract", parts D-F; step 2: Codex only, Claude's adapter
  says it cannot). One steer slot per session (`SteerSlot`), bound to the turn in flight N, written at most once, by
  this host, into this process: the caller offers it (`steer_offer`: the hub's offer, with whether the node's write
  authority held for it) and revokes it for good (`steer_revoke`: an authoritative omission, or the node's contact
  lost). It is written while N is in flight here, the process is live and not stopping, its binding is the
  session's, the turn gate and `may_start` allow it, the node's authority holds at write time (`steer_authority`)
  and the provider has proved the turn N's (Codex: `owns`). A frame the pipe took in part is completed while all of
  that lasts, for at most STEER_PARTIAL_SECONDS; otherwise the host stops writing, reads what the process wrote and
  stops it (the cut: STEER_CUT). N's end, however it comes, carries the steer's outcome (`HostEvent.steer`), decided
  there once: `in_turn` (Codex's valid answer to that very `turn/steer` before N's own end), `not_delivered` (not one
  byte written, a frame not one byte of which went is taken back, or Codex refused it), `unconfirmed` (anything
  else, also every end the host makes itself once a byte was written).
- Claude steering (DESIGN_NOTES "Mid-turn steering, step 3: Claude steering, as built"): the same slot, write
  boundary, authority and outcomes, for a verified Claude incarnation of a version that passed gate M8 (the adapter
  says so: `steers`). The line is S's own (`steer_named`). If N's native result does not settle a steer written whole,
  N's end is HELD (`Session.held`): N stays in flight here (nothing else is written; the node reports nothing of N),
  its native end is kept unaltered and sent to the hub once as a checkpoint (`HostEvent` kind "held"), and one end of
  N is published later: N's native result plus S's outcome, and for `next_turn` S's own result. The bound is the turn
  timeout from N's native end, and a fresh one from the `init` that opens S's own turn. Any other end the host makes
  of N meanwhile (its timeout, an exit, a cancel, a close, the cut, a deviation) is the finalizer: what was read
  decides S (`next_turn` failed if S's own turn was proved and cut, else `unconfirmed`), the process is stopped, and
  N's end is published once its group is gone or 2 x kill_grace after the stop (the bounded publication; main's
  stopping-group barrier holds whatever lingers). A stop of every turn and shutdown publish at main's point, as
  "Shutdown simplified" says, reading nothing more.
- Stops (DESIGN_NOTES "Shutdown simplified"): a stop of every turn (`stop_turns`), shutdown (`stop_all`) and an
  unexpected error (`_end` from `tick`) are main's, whether or not a steer is live: they read nothing more, and S's
  outcome is what was already read (N's native end with its evidence, else `unconfirmed` once a byte was written and
  `not_delivered` with none). Only a single-session stop of a live attempt's turn (reap, the turn timeout, a broken
  pipe, close, a cancel of the live turn, the cut) first reads what it wrote (`_drain_before_stop`), inline, where
  main settles that process, within one shared STEER_DRAIN_SECONDS per hold of the node's lock. The stop latch
  (`steer_stopping`) is the evidence cutoff for every steer: no cut begins, no drain reads and no native end counts
  for S once it is set.
"""
from __future__ import annotations

import json
import logging
import os
import signal
import threading
import time
import dataclasses
import itertools
from collections import deque
from contextlib import contextmanager
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
STEER_PARTIAL_SECONDS = 5.0         # the most a steer frame the pipe took in part is given to be completed (part D)
STEER_CUT = "the session's process was stopped: a steer to it could not be written whole"
# Before a single-session stop that would discard it (and in the cut), what a steering session's process wrote is read
# while its reads make progress (lines, or bytes taken into a line not yet complete), for at most this long on the
# host's clock IN ALL for one host pass, however many sessions it drains (R2-05; the shutdown simplification) ...
STEER_DRAIN_SECONDS = 2.0
STEER_DRAIN_READS = 4096    # ... and at most this many reads (a clock that does not move)
STEER_CONTACT_SECONDS = 75.0    # an attempt's contact: each acknowledgement counts from its receipt to 75 s after its send
STEER_UNATTRIBUTED = "the turn's result could not be attributed after a steer"  # (part A's third exception)
HOLD = {"outcome": None, "why": "held"}     # `_steer_end`'s word for a held end (never published as such)
STEER_S_CUT = "its own turn was cut: {why}"     # S's result when its proved own turn is cut (part D)


@contextmanager
def received_under(lock, clock):
    """Part E's receipt rule, in one place (R4-02): a receipt time is the clock read AFTER `lock` is taken, never one
    sampled before waiting for it. Used for an acknowledgement in an attempt's record (`SteerSlot.ack`, the record's
    lock) and for a fetch's answer (node_sessions, the lock that freezes the fetch's history)."""
    with lock:
        yield clock()


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
    # turn_ended: the outcome of the steer into this turn, decided here once ({"outcome", "why"}); None: it took none
    steer: dict | None = None


@dataclass
class SteerSlot:
    """The one steer attempt of a session (the module text). `mark`: where its frame sits in `outbuf` while not all
    of it reached the process; `sent`: its bytes the pipe took. Once N's outcome is decided (`outcome`) nothing about
    it changes; a frame that was taken in part is still completed, or the process stopped."""
    uuid: str
    into: str                       # the message id of N, the turn it is bound to
    text: str
    authorized: bool = False        # an offer the node's write authority held for (part E)
    revoked: str | None = None      # its authority is lost for good (why): it is never written from then on
    refused: str | None = None      # never writable here (why): the node's own cap
    proved: bool = False            # the provider proved N's turn its own at some pass (Codex: `owns`)
    mark: Any = None
    sent: int = 0
    length: int = 0
    partial_since: float | None = None
    outcome: dict | None = None
    fenced: bool = False            # the last listing did not name `steer`: nothing is written meanwhile (S2-05)
    frame: bytes | None = None      # the frame's bytes once made (one request: a fenced frame goes again as it was)
    # Review round 3, structure 1: this slot IS the attempt's one authority record, keyed by (session, N, uuid). Its
    # contact clock (`cover_until`: authority holds while now is before it; None for a host driven without a node),
    # its break latch (`revoked`), fence, gate losses and bytes written live here and nowhere else, under `lock`,
    # which the write boundary holds across its fresh check and its non-blocking write, and which an
    # acknowledgement takes to count or to latch a break (`ack`, `contact_ok`).
    session_id: str = ""
    cover_until: float | None = None
    stopping: bool = False          # the node is stopping: set under `lock` (`steer_stopping`), never written then
    lock: Any = field(default_factory=threading.RLock, repr=False, compare=False)
    # (R9-02) The process this attempt was taken for. The slot goes when that process is let go of
    # (`_release_steer`); a cut is only ever made for the current process's own slot.
    process: Any = field(default=None, repr=False, compare=False)
    # Claude steering (review round 1, ruling A): what the one adjudicator (`_steer_adjudicate`) has taken as
    # evidence for this attempt, before N's end: a deviation while it was live (`lost`: no S turn can be proved).
    lost: bool = False
    # (Review round 2, ruling G) The contact clock's expiry, latched on its own: write revocation (`revoked`, whose
    # first reason stays) and contact expiry are tracked apart, so an authoritative omission never masks the contact
    # running out later (a fully written attempt's hold is still cut then).
    expired: bool = False
    # (Round 3, step 2 parity) The attempt is a Claude one (its adapter holds N's end): only its contact clock is kept
    # after an omission revoked its writing; a Codex attempt's is main's.
    holds: bool = False

    def _expire(self, now: float) -> None:
        """(Under `lock`.) Past the contact clock: expired for good, and never written from then on."""
        if self.cover_until is not None and not self.expired and now >= self.cover_until:
            self.expired = True
            if self.revoked is None:
                self.revoked = "contact"

    def contact_ran_out(self, now: float) -> bool:
        """The attempt's own contact clock has run out (whatever else revoked its writing): latched, for good."""
        with self.lock:
            self._expire(now)
            return self.expired

    @property
    def key(self) -> tuple[str, str, str]:
        return self.session_id, self.into, self.uuid

    def ack(self, sent: float, clock: Callable[[], float], about: str | None, n: Any) -> None:
        """An acknowledgement, SENT at `sent`, received now: its receipt time is `clock` read under this record's
        lock (`received_under`, R4-02). A hello (`about` None), or a report on `about` whose answer named `n` in
        flight. It counts for this attempt only if it is evidence for exactly this session and N; received after the
        contact ran out, it latches the break for good instead (S2-03, R2-07)."""
        with received_under(self.lock, clock) as received:
            if self.cover_until is None or self.expired or (self.revoked is not None and not self.holds):
                return      # (round 3, step 2 parity: a revoked Codex attempt's contact is main's: not extended)
            if about is not None and not (about == self.session_id and type(n) is int and str(n) == self.into):
                return
            if received >= self.cover_until:
                self._expire(received)
                return
            # (ruling G) an acknowledgement keeps the contact of an attempt whose writing an omission revoked: the
            # contact clock is its own, never masked by the revocation
            self.cover_until = max(self.cover_until, sent + STEER_CONTACT_SECONDS)

    def contact_ok(self, now: float) -> bool:
        """The contact holds at `now` (read under this lock); run out, the break is latched for good."""
        with self.lock:
            self._expire(now)
            return self.revoked is None



class _Outbox(list):
    """(Review round 3, ruling M) The host's outbox: published events in order, each tagged with the incarnation
    of its session when it was published, so an id opened again never receives an old event, and a forgotten
    session's entries are purged. A list of HostEvent to every reader."""

    def __init__(self, host: "SessionHost"):
        super().__init__()
        self._host, self._gens = host, []

    def _gen(self, ev: HostEvent):
        s = self._host.sessions.get(ev.session_id)
        return s.gen if s is not None else None

    def append(self, ev: HostEvent) -> None:
        super().append(ev)
        self._gens.append(self._gen(ev))

    def extend(self, evs) -> None:
        for ev in evs:
            self.append(ev)

    def purge(self, session_id: str) -> None:
        keep = [(e, g) for e, g in zip(self, self._gens) if e.session_id != session_id]
        self[:] = [e for e, _ in keep]
        self._gens = [g for _, g in keep]

    def current(self) -> list[HostEvent]:
        return [e for e, g in zip(self, self._gens) if g is None or g == self._gen(e)]


@dataclass
class _Held:
    """N's end, held while a steer written into N is unresolved (Claude steering, the module text): N's native end
    exactly as it came (`end`, with no steer outcome on it), when it came (`since`), whether S's own turn was proved
    (`s_turn`), and, once the finalizer stopped the process, when (`fenced_at`), S's decided outcome (`decided`) and
    what waits behind N's end (`after`: a close's `ended`)."""
    end: HostEvent
    since: float
    s_turn: bool = False
    fenced_at: float | None = None
    decided: dict | None = None
    after: list = field(default_factory=list)
    lost: bool = False              # evidence says nothing later can prove S quiet: the finalizer, at once


@dataclass
class _Message:
    id: str
    text: str
    kind: str = "user"      # providers.TURN_KINDS: whose turn it is, passed on to the provider with the text
    # Turn correlation's no-progress budget (`_decide`, `_spent`), for a message not one byte of whose line was
    # accepted yet. `counted`: seconds of COUNTED time since it was EARNED (None: not earned, no budget). It is
    # earned at a decision point at which the session's process was verified right then and the message was in a
    # counted state (only then does it have a budget at all; nothing is remembered from reading lines: review
    # rounds 14 and 15); `counted` set is the one record of that. `counted_at`: the
    # time of the last pass that ENDED with it in a counted state (None: the last one did not). `recovered`: its one
    # 7b restart is spent. On the message itself, so they go when it goes (and survive an unstage, a restage, a
    # downgrade and a replacement process).
    counted: float | None = field(default=None, compare=False)
    counted_at: float | None = field(default=None, compare=False)
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
    gen: int = 0                        # (ruling M) this session's incarnation on this host (`_Outbox`)
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
    # Mid-turn steering (the module text): the slot of the steer bound to the turn in flight, and every steer uuid
    # this host ever took (a repeated offer of one is never a second attempt).
    steer: SteerSlot | None = None
    steered: set = field(default_factory=set)
    held: _Held | None = None       # N's end, held (Claude steering): N stays `in_flight` meanwhile

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
        # The same question with no side effect (node_sessions: `may_start` itself sets the node's latch at the
        # self-stop deadline): read by turn correlation's budget accounting only, which must change nothing main
        # would not (review round 13). Not set: `may_start` is read instead (a test's, with no side effect).
        self.may_start_query: Callable[[], bool] | None = None
        self._in_tick = False       # `tick` is not re-entrant
        # Asked before every write of a steer's bytes (node_sessions sets it: the node's write authority, part E, for
        # this session and its turn in flight). False: nothing of the steer is written now.
        self.steer_authority: Callable[[Session], bool] | None = None
        # A test's hook at the steer write boundary (`_steer_write`): called with "before" and "after" and the
        # session, outside the attempt's lock. None in production.
        self.steer_write_hook: Callable[[str, Session], None] | None = None
        # The drain's budget for the host pass under way (`tick`, `steer_deadlines`): None outside one, else the
        # deadline (None until the pass's first drain) every drain in it shares (`_drain_before_stop`).
        self._drain_pass = False
        self._drain_deadline: float | None = None
        # Review round 5, R5-01: THE steering stop latch. Set once, with every held record marked, under
        # `_steer_stop_lock` (`steer_stopping`); a slot is installed under the same lock (`steer_offer`), so one
        # installed after the latch is born stopped. The write boundary reads it inside the record's critical section.
        self.steer_stopped = False
        # (R7-04) A stop has begun (`steer_stopping` was called, its latch perhaps not set yet): a drain takes no next
        # batch, so a waiting stop never loses the latch's lock to it.
        self._stop_pending = False
        # A stop of every turn (`stop_turns`, `stop_all`), or an unexpected error's end of a session (rule 5), is under
        # way: a held end (Claude steering) is published at its point, at once, reading nothing more.
        self._at_once = False
        # A test's hook at a slot's installation (`steer_offer`): "before" and "after" outside the latch's lock,
        # "inside" under it. None in production.
        self.steer_install_hook: Callable[[str, Session], None] | None = None
        # Tests' hooks: at a cut, before it takes ownership under the latch's lock (`_steer_cut`), and before each of a
        # drain's reads (`_drain_reads`). None in production.
        self.steer_cut_hook: Callable[[Session], None] | None = None
        self.steer_drain_hook: Callable[[Session], None] | None = None
        # (Review round 2, ruling J) asked at every write of a steer's bytes, in the write gate only: the hub can take
        # this session's steer (a Claude one: the hub names `held`). Never part of the attempt's authority.
        self.steer_capable: Callable[[Session], bool] | None = None
        self._recovered = False
        self._next_message = 0
        # Runner key -> window -> the latest plan usage a session of that runner's provider reported for it (`usage`
        # events), with when (`at`, this host's clock). Read by the node's hello from another thread: under
        # `usage_lock` (`note_usage`).
        import threading
        self.usage: dict[str, dict] = {}
        self.usage_lock = threading.Lock()
        self._steer_stop_lock = threading.RLock()       # the steering stop latch's (above)
        # (Review round 2, ruling I) Every event the host publishes goes here at the moment of publication; `tick`,
        # `close`, `cancel`, `stop_turns` and `stop_all` return it, emptied, when they return. An exception that
        # escapes one of them leaves it as it is, and the next of them delivers it: exactly once.
        self._outbox: _Outbox = _Outbox(self)
        self._defer_logs: list | None = None      # (ruling T) log lines held while a claim holds the cutoff lock
        self._claimer: int | None = None            # ... and the thread making that claim
        self._gens = itertools.count(1)     # (ruling M) each opened session's incarnation, for the outbox's entries
        self._defer_save = False    # (ruling H) a stop of every turn: held publications persist after every SIGTERM
        self._save_due = False      # (round 4, ruling T) a save the watchdog's path left to the loop's next tick

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
        s.gen = next(self._gens)
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

    def steer_offer(self, session_id: str, into: str, uuid: str, text: str, *, authorized: bool,
                    refused: str | None = None, cover_until: float | None = None) -> str | None:
        """The hub offers steer `uuid` into the turn in flight `into` (part G). A slot is made only for the turn in
        flight here, of a provider that steers, and only for a steer this host never took; a repeated offer of the
        same steer adds only that an offer held authority (never once it was revoked)."""
        s = self._session(session_id)
        for v in (into, uuid):
            if not isinstance(v, str) or not 0 < len(v) <= MESSAGE_ID_MAX:
                raise ValueError("a steer's ids must be short strings")
        slot = s.steer
        if slot is not None:
            if slot.key != (s.session_id, into, uuid):
                # Not this attempt (another steer, or this uuid for another N): rejected before it touches anything
                # of the record (R3-02). Said by the caller, once per held attempt, outside its locks (R8-04).
                return "not the held attempt"
            with slot.lock:
                if authorized and slot.revoked is None and slot.refused is None and slot.outcome is None:
                    # Only an offer the node's authority held for (fresh, within the cap), of this very steer for this
                    # very N, adds authority or lifts a fence (S2-05, R2-02); a stale one changes nothing.
                    slot.authorized = True
                    slot.fenced = False
            return
        if uuid in s.steered or s.ended or s.in_flight is None or s.in_flight.id != into:
            return
        if "steer" not in providers.modes(s.provider) or not isinstance(text, str):
            return
        hook = self.steer_install_hook
        if hook is not None:
            hook("before", s)
        with self._steer_stop_lock:
            # (R5-01) serialized with the stop latch: installed before it (and then marked by it), or born stopped.
            slot = SteerSlot(uuid, into, text, authorized=bool(authorized) and refused is None, refused=refused,
                             session_id=s.session_id, cover_until=cover_until, process=s.child)
            slot.stopping = self.steer_stopped
            slot.holds = getattr(s.provider, "steer_named", None) is not None     # (round 3, step 2 parity)
            s.steer = slot
            s.steered.add(uuid)
            if hook is not None:
                hook("inside", s)
        if hook is not None:
            hook("after", s)

    def steer_revoke(self, session_id: str, uuid: str, why: str, into: str | None = None) -> None:
        """Steer `uuid` (for N `into`, when given) loses its authority for good (part E): an authoritative listing no
        longer lists it. Revalidated against the held attempt's key when applied (R3-02): it never touches another
        attempt. Nothing more of it is written; one fully written keeps its outcome to be decided."""
        s = self.sessions.get(session_id)
        slot = s.steer if s is not None else None
        if slot is None or slot.uuid != uuid or (into is not None and slot.into != into):
            return
        with slot.lock:
            if slot.revoked is None:
                slot.revoked = why[:40]

    def steer_stopping(self) -> None:
        """The node is stopping (R4-01, R5-01): the steering stop latch is set and every held attempt's record marked,
        as one step under the latch's lock, so no slot can be installed between the two (one installed later is born
        stopped). Each record is marked under its own lock, so a write already inside its critical section has
        returned when this does. The node publishes its stopping event only after this returns (`_Stopping`)."""
        self._stop_pending = True
        with self._steer_stop_lock:
            self.steer_stopped = True
            for slot in self.attempts():
                with slot.lock:
                    slot.stopping = True

    def _steer_stopped(self, slot: SteerSlot) -> bool:
        """(R5-02) The stop latch is set, or this record is marked: a gate that fails for it does nothing else."""
        return self.steer_stopped or slot.stopping

    def attempts(self) -> list:
        """Every held steer attempt (its record), for an acknowledgement to be counted in. Read without the host's
        lock (a dict's values listed at once, each session's `steer` read once): an acknowledgement never waits on
        the host's work (R2-09). An attempt that ends meanwhile takes it harmlessly: nothing reads it again."""
        return [slot for slot in [s.steer for s in list(self.sessions.values())] if slot is not None]

    def steer_slot(self, session_id: str):
        s = self.sessions.get(session_id)
        return s.steer if s is not None else None

    def steer_fence(self, session_id: str) -> None:
        """A listing that does not name `steer` (an older hub, or one whose capability is unknown): nothing of the
        held steer is written until an authoritative listing lists it again (S2-05). Nothing is revoked."""
        s = self.sessions.get(session_id)
        if s is not None and s.steer is not None:
            with s.steer.lock:
                s.steer.fenced = True

    def steer_live(self, session_id: str) -> tuple[str, str] | None:
        """(N, the steer's uuid) while the session holds a steer whose outcome is open or whose frame is still being
        written, else None."""
        s = self.sessions.get(session_id)
        if s is None or s.steer is None:
            return None
        return s.steer.into, s.steer.uuid

    def steer_deadlines(self) -> list[HostEvent]:
        """A partial steer frame whose authority is gone or whose time is up is cut now (`_steer_pass`), also between
        passes: the node's deadline watchdog calls this."""
        out = self._outbox
        now = self.clock()
        before = len(out)
        self._defer_save = True     # (ruling T) the watchdog's path persists nothing: the loop's next tick does
        try:
            self._steer_deadlines(now, out)
        finally:
            self._defer_save = False
            if len(self._outbox) != before or out is not self._outbox:
                self._save_due = True
        return self._take_outbox()

    def _steer_deadlines(self, now: float, out: list[HostEvent]) -> None:
        with self.one_drain_budget():      # one drain deadline for every cut in this call
            for s in list(self.sessions.values()):
                slot = s.steer
                if (slot is not None and slot.mark is not None and slot.sent > 0 and s.child is not None
                        and s.stopping_since is None and not self._steer_stopped(slot)
                        and not self._steer_gate(s, slot, now)):
                    self._steer_cut(s, self.clock(), out)
                elif s.held is not None:
                    # (Review round 2, ruling G) the same boundary cuts a fully written hold whose contact ran out
                    # or whose authority is gone, also while the loop is blocked on hub I/O; and (round 3, ruling
                    # N) publishes a fenced one whose deadline passed, into the outbox, with no loop tick needed
                    self._hold_cut(s, self.clock(), out)
                    if s.held is not None:
                        self._hold_deadline(s, self.clock(), out)

    def live_attempt(self, s: Session) -> bool:
        """The session holds a live steer attempt: N's outcome open, its process up and not stopping."""
        return (s.steer is not None and s.steer.outcome is None and s.child is not None and s.stopping_since is None
                and not s.ended)

    def _drain_deadline_now(self) -> float:
        """The deadline a drain starting now has: the host pass's shared one (from its first drain), or its own."""
        if not self._drain_pass:
            return self.clock() + STEER_DRAIN_SECONDS
        if self._drain_deadline is None:
            self._drain_deadline = self.clock() + STEER_DRAIN_SECONDS
        return self._drain_deadline

    def _drain_reads(self, s: Session, into: str, now: float, out: list[HostEvent], deadline: float) -> None:
        """The drain's reads of the session's process: while N is in flight, they make progress and the deadline has
        not passed. Each read is made under the stop latch's lock, after a look at the latch (review round 6, R6-01):
        once the node began to stop the drain reads no more, so nothing read after that cutoff is evidence for S (and
        the latch is never set in the middle of a read). Never raises."""
        try:
            for _ in range(STEER_DRAIN_READS):
                if s.child is None or s.in_flight is None or s.in_flight.id != into:
                    break       # N's end is read (or N is no longer in flight): nothing more is needed
                if self.clock() >= deadline:
                    break       # the pass's deadline has passed
                hook = self.steer_drain_hook
                if hook is not None:
                    hook(s)
                with self._steer_stop_lock:
                    # A batch is admitted only here, under the latch's lock: not once the node began to stop
                    # (R6-01), nor once a stop is waiting for this lock (R7-04, R8-03: read inside it, so a stop that
                    # announces itself before this lock is taken is never overtaken by a fresh batch).
                    if self.steer_stopped or self._stop_pending:
                        break
                    before = getattr(s.child, "bytes_read", None)
                    lines = s.child.read_lines()        # (R7-04) only the read itself under the lock
                got = self._take_lines(s, now, out, lines)      # the bookkeeping after it (transcript, state)
                after = getattr(s.child, "bytes_read", None) if s.child is not None else before
                if (not got and (before is None or after == before)) or self.clock() >= deadline:
                    break
        except Exception as e:
            # (R8-04) The type only: no exception text (which could carry a path or a line the process wrote).
            log.error("session %s: could not read what its process wrote before stopping it (%s)", s.session_id,
                      type(e).__name__)

    def _drain_before_stop(self, s: Session, now: float, out: list[HostEvent]) -> float:
        """The drain, called only on the single-session paths that end a live steer attempt's turn (reap, the turn
        timeout, a broken pipe, close, a cancel of the live turn, the cut), before N is failed; each caller rechecks
        N after it. Never on a stop of every turn, shutdown or an unexpected error: those read nothing more (the
        shutdown simplification). It reads while the reads make progress (a line, or bytes into one not complete yet:
        `bytes_read`), and stops at once when N's end is read, when a read makes no progress, or at the deadline: ONE
        for the whole host pass (`_drain_deadline`, STEER_DRAIN_SECONDS from the pass's first drain; outside a pass,
        from now), so an attempt that cannot be drained in what is left is stopped without one (at most
        STEER_DRAIN_READS reads). Never raises: an error ends the reading and what was read stays in `out`. Returns the
        time to date what follows by: `now` for a session holding no live attempt (not read here at all: main's path
        is unchanged), else the clock after the drain, so no signal after it is backdated."""
        if not self.live_attempt(s):
            return now
        self._drain_reads(s, s.steer.into, now, out, self._drain_deadline_now())
        return self.clock()     # (R6-04) what follows, a signal included, is dated when it is done

    @contextmanager
    def one_drain_budget(self):
        """A host pass (`tick`, `steer_deadlines`, and the node's reconciliation of a listing, whose closes and cancels
        are each a single-session stop): every drain in it shares one deadline (`_drain_before_stop`). Nested calls
        share the outer."""
        outer = not self._drain_pass
        if outer:
            self._drain_pass, self._drain_deadline = True, None
        try:
            yield
        finally:
            if outer:
                self._drain_pass, self._drain_deadline = False, None

    def holds(self, session_id: str) -> bool:
        """The session's adapter is one whose steers are held (Claude's, `steer_named`): its steering is offered and
        written only to a hub that names `held` (review round 2, ruling J). Whether its process steers now is
        `modes`'s."""
        s = self.sessions.get(session_id)
        return s is not None and getattr(s.provider, "steer_named", None) is not None

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
        out = self._outbox
        s = self._session(session_id)
        if not s.ended:
            live = self.live_attempt(s)
            self._end(s, detail, self._drain_before_stop(s, self.clock(), out), out, at_signal=live)
        return self._take_outbox()

    def forget(self, session_id: str) -> None:
        """Drop an ended session whose process is gone, so its id no longer counts as open here."""
        s = self._session(session_id)
        if not s.ended or s.child is not None:
            raise ValueError("session has not ended, or its process is still stopping")
        del self.sessions[session_id]
        self._outbox.purge(session_id)      # (ruling M) nothing of it is delivered once it is forgotten
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
        if self._in_tick:
            raise RuntimeError("SessionHost.tick is not re-entrant")
        self._in_tick = True
        try:
            if not self._recovered:
                self.recover()
            if self._save_due:      # (ruling T) what the watchdog's path published is persisted here, by the loop
                self._save_due = False
                self._save_state()
            self._tick(self.clock(), self._outbox)
        finally:
            self._in_tick = False
        return self._take_outbox()

    def _take_outbox(self) -> list[HostEvent]:
        """What was published since the last delivery, in order, handed over once (ruling I); an entry whose session
        is gone or was opened again since (another incarnation of the id) is never delivered (ruling M)."""
        out, self._outbox = self._outbox, _Outbox(self)
        return out.current()

    def take_outbox(self) -> list[HostEvent]:
        """(Review round 3, ruling M) What waits in the outbox now, for the node's ONE dispatcher: drained before a
        reconciliation can synthesise LOST for a session, and before a session is forgotten."""
        return self._take_outbox()

    def drain_outbox(self, take: Callable[[HostEvent], None]) -> None:
        """(Review round 4, rulings S and V) The handoff of what the outbox retains, event by event: each leaves
        the outbox only once `take` has owned it (returned). If `take` raises, that event and every one after it stay
        in the outbox, in order, for the next handoff: none is lost, none taken twice."""
        current = self._outbox.current()
        self._outbox = _Outbox(self)
        for i, ev in enumerate(current):
            try:
                take(ev)
            except BaseException:
                self.requeue(current[i:])
                raise

    def requeue(self, events: list[HostEvent]) -> None:
        """(Ruling V) Events handed over and not owned (the taker raised) go back to the FRONT of the outbox, in
        order, ahead of anything published since."""
        later = self._outbox
        self._outbox = _Outbox(self)
        self._outbox.extend(events)
        self._outbox.extend(later.current())

    def _tick(self, now: float, out: list[HostEvent]) -> None:
        # Main's pass, in main's order, at the pass's `now`, for every session. A live steer attempt's turn end is
        # settled inline, where main settles that process (review round 6): its bounded drain there, then main's own
        # handling. Every drain in the pass shares one deadline (`one_drain_budget`; in the node's pass, the one of the
        # whole hold of its lock).
        with self.one_drain_budget():
            # (Claude steering, review round 1, ruling B) A held end the finalizer decided is published once its
            # deadline passes, checked here first, before any read can fail.
            for s in list(self.sessions.values()):
                if s.held is not None:
                    self._hold_deadline(s, now, out)
            # Every exit first: a leader that went by itself leaving members of its group is registered as stopping
            # (`_reap`) before this pass starts any process, so nothing it starts goes beside such a group.
            for s in list(self.sessions.values()):
                if s.child is not None and s.stopping_since is None:
                    try:
                        self._read(s, now, out)
                        self._reap(s, now, out)
                    except Exception as e:
                        log.exception("session %s failed", s.session_id)
                        self._host_error(s, e, now, out)    # (rule 2: no drain; rule 5: a held end at once)
            for s in list(self.sessions.values()):
                try:
                    self._pass(s, now, out)
                except Exception as e:
                    log.exception("session %s failed", s.session_id)
                    self._host_error(s, e, now, out)

    def _hold_deadline(self, s: Session, now: float, out: list[HostEvent]) -> None:
        """Ruling B: the bounded publication's deadline, independent of any read (an error here is logged; the held
        end is then published at once: rule 5)."""
        try:
            self._hold_fence(s, now, out)
        except Exception:
            log.exception("session %s: could not publish a held end at its deadline", s.session_id)
            self._publish_now(s, out)

    def _host_error(self, s: Session, e: Exception, now: float, out: list[HostEvent]) -> None:
        """An unexpected error in a session's pass: main ends the session (`_end`). "Shutdown simplified" rule 5: a
        held end (Claude steering) is published at once from the evidence in hand, nothing deferred and nothing read;
        and whatever happened, no held end and no `ended` is ever stranded behind a later read (ruling B)."""
        try:
            if not s.ended:
                self._at_once = True
                try:
                    self._end(s, f"host error: {type(e).__name__}", now, out)
                finally:
                    self._at_once = False
        finally:
            self._publish_now(s, out)

    def _publish_now(self, s: Session, out: list[HostEvent]) -> None:
        """A held end still there (whatever failed): published now, S by what was adjudicated."""
        held = s.held
        if held is None:
            return
        if held.decided is None and self.steer_stopped and not self._at_once:
            return      # (ruling T) after the latch no new decision here: the bulk stop owns it
        if held.decided is None:
            held.decided = ({"outcome": "next_turn", "why": "own_turn_cut", "result": {
                "ok": False, "detail": STEER_S_CUT.format(why="a host error")[:300], "denials": [], "meta": {},
                "text": "", "answered": False}} if held.s_turn else {"outcome": "unconfirmed", "why": "turn_cut"})
        self._publish_hold(s, out, held.decided)

    def _pass(self, s: Session, now: float, out: list[HostEvent]) -> None:
        gates = self._steps(s, now, out)
        self._mark(s, now, gates)

    def _steps(self, s: Session, now: float, out: list[HostEvent]) -> tuple[bool, bool] | None:
        if s.child is not None:
            self._read(s, now, out)
            self._reap(s, now, out)
        if (s.child is not None and s.stopping_since is None and not s.ended and s.writing is not None
                and s.turn_at is not None and s.writing.counted is None):
            # A deadline is decided before anything that can discard its input (review round 16): for a staged line
            # with no budget (not earned), main's write-timer check (main's clock, no gates: it acts only if due)
            # comes before the take-back, which would clear that clock (and let the decision earn it afresh). An
            # earned one keeps the take-back: its budget is its clock, not this one. (After the decision it runs
            # again, as on main: at the same time, what was not due here is not due there.)
            self._hold_to_timeout(s, now, out)
        if s.held is not None:
            self._hold_pass(s, now, out)        # (Claude steering: nothing for a session holding no end)
        if s.child is not None and s.stopping_since is None and not s.ended:
            self._take_back(s, now, out)
        # The session's ONE decision point in this tick (review round 14): after every read and reap that precede it
        # (`tick`'s first drain of every session, then the passes of the sessions before this one, then this one's
        # second drain, the check above and the take-back), before any other timeout or write. Its one gate reading (`gates`, None if none was
        # needed) serves the earning, the budget, the write timer's choice of clock and the pass's end mark.
        acted, gates = (False, None) if s.ended else self._decide(s, now, out)
        if s.child is not None and s.stopping_since is None and not s.ended:
            self._hold_to_timeout(s, now, out, gates)
        if s.child is not None and s.stopping_since is not None:
            self._kill_if_due(s, now)
            self._reap(s, now, out)
            if s.held is not None:
                self._hold_fence(s, now, out)   # (Claude steering: a held end the finalizer decided)
            if s.child is not None:
                self._lingering(s, now)
                return gates    # still going down: nothing new starts beside it
        if acted:
            return gates    # the budget stopped its process (or failed it) in this pass: what comes next, the next
        if not s.ended:
            self._restart_pass(s, now, out)
            self._steer_pass(s, now, out)
            if s.child is not None and s.stopping_since is not None:
                return gates    # a steer frame was cut: the process goes down, nothing more is written to it
            self._write(s, now, out)
            if s.child is not None and s.stopping_since is not None:
                self._reap(s, now, out)     # a write that failed stopped it
        return gates

    def _read(self, s: Session, now: float, out: list[HostEvent]) -> int:
        return self._take_lines(s, now, out, s.child.read_lines())

    def _take_lines(self, s: Session, now: float, out: list[HostEvent], lines: list) -> int:
        """What `_read` does with the lines read (main's body of it): a drain reads under the stop latch's lock and
        does this after (R7-04)."""
        keep = getattr(s.provider, "transcript_line", None)
        for line in lines:
            # The transcript keeps what the process wrote, except what the provider says must not be kept (Claude's
            # answer to `get_settings`: the settings in force, which can hold credentials): its own record instead.
            self._tee(s, line if keep is None else keep(line))
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
        return len(lines)       # (how many lines it read: a drain reads on while there are any)

    def _on_event(self, s: Session, ev: Event, now: float, out: list[HostEvent]) -> None:
        if (ev.steer is not None or ev.turn == "steer") and ev.steer not in ("n_with_s", "n_without_s", "s_only"):
            if self._steer_adjudicate(s, ev, now, out):
                if ev.kind == "turn_started":
                    self._check_access(s, ev, now, out)
                return
            if ev.kind == "usage" and not ev.usage:
                return      # (evidence alone)
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
            if not self._check_access(s, ev, now, out):
                return
            if ev.turn == "steer":
                return      # (S's own turn: `_steer_adjudicate`)
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
            if ev.turn == "steer":
                return      # (S's own turn: `_steer_adjudicate`)
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
            end = HostEvent(s.session_id, "turn_ended", msg.id, ok=ok, denials=ev.denials, meta=dict(ev.meta),
                            detail=detail, text=ev.text,
                            answered=bool(ev.answered),     # the adapter's word: the provider took the turn
                            restarted_by=s.restart_by if by_restart else None)
            steer = self._steer_end(s, msg.id, native=bool(ev.answered), evidence=ev.steer)
            if steer is HOLD:
                s.in_flight = msg       # held: N stays in flight here until its end is published
                self._hold(s, end, now, out)
            else:
                out.append(dataclasses.replace(end, steer=steer) if steer is not None else end)
            self._forget_restart(s)     # the old turn has settled: the restart's message is the next ordinary one
            self._save_state()
        elif ev.kind == "approval_requested":
            if s.held is not None:
                # (Claude steering) N's end is held: an approval request now is the steer's own turn's, or the CLI's,
                # never N's (N is over); it is said in the log, and nothing of N changes.
                log.info("session %s: an approval request while the end of message %s is held (not that turn's)",
                         s.session_id, s.held.end.message_id)
                return
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

    def _check_access(self, s: Session, ev: Event, now: float, out: list[HostEvent]) -> bool:
        """The kill switch, on every `init` (whatever turn it starts: a message's, the CLI's own, a steer's own). The
        session's access was decided from its command line before it started; what the process says it really runs
        this turn with can only end it: bypassed where it was not started as full, not bypassed where it was, more
        than read-only where that was asked, or nothing verifiable where the access asked for depends on it. False:
        the session ended."""
        problem = (session_access.verify(s.asked, s.access, ev.mode, ev.tools)
                   if getattr(s.provider, "verifies_mode", False) else None)
        if problem is not None:
            log.warning("session %s: %s", s.session_id, problem)
            self._end(s, problem, now, out)
            return False
        if s.access == "full" and ev.mode == "bypassPermissions":
            s.confirmed_by = s.child    # Claude took the mode: while THIS process lives a child may be `full`
        return True

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
            if s.stopping_since is None and self.steer_stopped and self._steering(s):
                # (ruling T) after the latch the bulk stop owns a steering session: nothing decided or signalled
                # here; its group is only registered as stopping, so nothing starts beside it
                s.stopping_since = now
                self.groups.stopping(s.group_token)
                return
            if s.stopping_since is None:
                # It went by itself, leaving members of its group behind (a tool it started, say).
                self._read(s, now, out)
                live = self.live_attempt(s)
                now = self._drain_before_stop(s, now, out)      # a live steer attempt: all it wrote first (R3-05)
                if not s.ready:
                    self._correct_start_mode(s)
                if self._steering(s):
                    if not self._claim(s, lambda: self._went_by_itself(s, code, s.ready, s.checking, out)):
                        s.stopping_since = now      # (the latch came meanwhile: registered only, as above)
                        self.groups.stopping(s.group_token)
                        return
                else:
                    self._went_by_itself(s, code, s.ready, s.checking, out)
                log.warning("session %s: its process exited (code %s) and left members of its group: stopping them",
                            s.session_id, code)
                self._stop_from(s, now, live)
            return      # its leader is gone, but not its group: nothing starts beside that, nothing is reported reaped
        self._read(s, now, out)     # what it wrote on its way out (a result that just made it still counts)
        self._drain_before_stop(s, now, out)      # a live steer attempt: all it wrote, across reads (R3-05)
        if not s.ready:
            self._correct_start_mode(s)
        was_ready, was_checking = s.ready, s.checking
        child, s.child, s.proc_start, s.ready = s.child, None, None, False
        if s.held is not None and s.held.fenced_at is not None:
            self._publish_hold(s, out, s.held.decided)     # its group is gone: published before any cleanup (B)
        self.groups.gone(s.group_token)      # the whole group is gone
        s.group_token = None
        s.sealed_by = s.confirmed_by = None     # what that process said went with it
        stopped, s.stopping_since, s.killed, s.interrupted_at = s.stopping_since is not None, None, False, None
        s.outbuf.clear()
        if s.steer is not None and s.steer.outcome is not None:
            s.steer = None          # its turn is decided and its process is gone: nothing more of it is written
        close = getattr(child, "close", None)
        if close is not None:
            close()
        if s.transcript_fd is not None:
            os.close(s.transcript_fd)
            s.transcript_fd = None
        if not stopped:
            self._went_by_itself(s, code, was_ready, was_checking, out)
        self._release_steer(s)
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
            # after the pass's reads and, for a line with no budget, main's write-timer check (review round 16), and
            # before its budget, any other timeout or any write is looked at (`_steps`).
            self._unstage(s, now, out)

    def _hold_to_timeout(self, s: Session, now: float, out: list[HostEvent],
                         gates: tuple[bool, bool] | None = None) -> None:
        if (s.writing is not None and s.turn_at is not None and s.writing.counted is not None and gates is not None
                and self._counting(s, s.writing, gates)):
            # ONE clock at every pass for a message staged with not one byte accepted (review rounds 11 and 12): an
            # EARNED message's budget while its counted state holds (`_decide`, earlier in this pass; not this
            # staging's write timer, C5-01, C6-01); otherwise main's write timer from the staging, below, exactly as
            # on main: while the host withholds it (`may_start` false: a latch the watchdog does not see), or with
            # no budget (not earned: a candidate's, an other version's: main's path, entirely, C7-01). The write
            # timer never restarts while the line stays staged, so it never gets more than that timer plus its
            # counted budgets; and the gates are the decision point's one reading (`_decide`).
            return
        if s.busy_since is None:
            return
        if s.interrupted_at is not None:
            if now - s.interrupted_at >= self.kill_grace:     # the provider's own interrupt did not end the turn
                live = self.live_attempt(s)
                if live:
                    now = self._drain_before_stop(s, now, out)      # (R3-05)
                    if s.in_flight is None and s.writing is None:
                        return
                if self._steering(s):
                    self._cutoff_stop(s, self._timeout_detail(), now, out)      # (ruling T)
                    return
                self._fail(s, self._timeout_detail(), out)
                self._stop_from(s, now, live)
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
        live = self.live_attempt(s)
        if live:
            now = self._drain_before_stop(s, now, out)      # a native end behind buffered output is N's own (R3-05)
            if s.in_flight is None and s.writing is None:
                return
            if (s.held is not None and s.busy_since is not None
                    and now - s.busy_since < self.timeout_minutes * 60):
                return      # the drain found S's own turn begun: its fresh clock holds (part D)
        if s.held is None and s.steer is not None and s.steer.lost and s.steer.outcome is None:
            detail = STEER_UNATTRIBUTED     # (part A's third exception: N's result was never attributed)
        else:
            detail = self._timeout_detail()
        if live or s.held is not None:
            self._cutoff_stop(s, detail, now, out)      # (ruling N: the held timeout too; nothing after the latch)
            return
        self._fail(s, detail, out)
        self._stop_from(s, now, live)

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
        if s.steer is not None and s.steer.mark is not None and s.steer.mark[0] > start:
            s.steer.mark = (s.steer.mark[0] - length, s.steer.mark[1])

    @staticmethod
    def _forget_restart(s: Session) -> None:
        s.restart_by = s.restart_into = s.restart_mark = None
        s.restart_sent = s.restart_written = False

    def _fail(self, s: Session, detail: str, out: list[HostEvent]) -> None:
        """Give up on the session's turn: failed, and never written again (a restart asked for it: no link). A held
        end (Claude steering) is not failed: the finalizer decides S and publishes N's native end (`_finalize_hold`)."""
        if s.held is not None and s.in_flight is not None and s.in_flight.id == s.held.end.message_id:
            self._finalize_hold(s, detail, out)
            self._forget_restart(s)
            return
        msg, delivered = (s.in_flight, True) if s.in_flight is not None else (s.writing, False)
        s.in_flight = s.writing = s.busy_since = None
        if msg is not None:
            out.append(HostEvent(s.session_id, "turn_ended", msg.id, ok=False, delivered=delivered, detail=detail,
                                 steer=self._steer_end(s, msg.id, native=False) if delivered else None))
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
        bounded by the message's one no-progress budget, in counted time (`_decide`, `_spent`), also on the binding
        path (`unbound`), where only the CLI's own turn stands between the message and the restart it needs."""
        return self.holding(s)

    def _holder(self, s: Session) -> _Message | None:
        """The message whose budget can run now: the one staged with not one byte of its line accepted, or the first
        in the inbox with nothing being written or in flight."""
        if s.writing is not None:
            return s.writing if s.turn_at is not None else None
        return s.inbox[0] if s.inbox and s.in_flight is None else None

    def _counting(self, s: Session, msg: _Message, gates: tuple[bool, bool]) -> bool:
        """Is `msg` in a COUNTED state now, one in which the agent's process is what holds it back? Exactly two, in a
        live process and with nothing on the host side withholding it (`_withheld`, ONE list for both): (b) it is
        staged with not one byte of its line accepted, and its process was started with the binding the session has
        now (on a new binding main withdraws the staged line, BINDING_CHANGED, later in this pass); (a) it is first in
        the inbox and the CLI's own turn is open (`holding`). Nothing else counts: no process, one going down (the
        restart for a new binding included), the turn gate closed, `may_start` false, a handshake or settings check
        pending, a stopping group, no slot. A new binding while the CLI's turn runs is (a): the host would restart
        the process for it at once, and waits only because the agent runs a turn of its own (and main's Claude path
        waits for no such turn at all). `gates`: the decision point's one reading (`_decide`)."""
        if s.child is None or s.stopping_since is not None or s.ended:
            return False
        if s.writing is not None:
            if s.writing is not msg or s.turn_at is None or s.unbound:
                return False
        elif s.in_flight is not None or not s.inbox or s.inbox[0] is not msg or not self.holding(s):
            return False
        return not self._withheld(s, gates)

    @staticmethod
    def _withheld(s: Session, gates: tuple[bool, bool]) -> bool:
        """The host-side conditions under which the host itself withholds a session's next (or staged) message, for
        both counted states (`_counting`): a handshake or a settings check pending, the turn gate closed (main
        withdraws a staged line then, TURN_WITHDRAWN, later in this pass), `may_start` false."""
        turn_gate, may_start = gates
        return bool(s.checking or (s.provider.waits_for_ready and not s.ready) or not turn_gate or not may_start)

    def _read_gates(self, s: Session) -> tuple[bool, bool]:
        """The turn gate and `may_start`, read at a decision point (`_decide`), only for a message with a budget, or
        one that could earn one there (its process verified right then and not stopping): `may_start` through `may_start_query` (the
        node's `may_start` sets its latch at the self-stop deadline; the query never does). The turn gate is the
        node's own question (node_sessions `_turn_allowed`; for a `full` child it polls the parent's process): so
        for an off-version or no-init process with no budgeted message, main's sequence of those calls, exactly."""
        may = self.may_start_query if self.may_start_query is not None else self.may_start
        return (self.turn_gate is None or bool(self.turn_gate(s)), may is None or bool(may()))

    def _decide(self, s: Session, now: float, out: list[HostEvent]) -> tuple[bool, tuple[bool, bool] | None]:
        """THE session's one decision point in a tick (`_steps`). Nothing is remembered from reading lines (review
        round 15): what decides is the state as it is here, after the tick's reads, reaps and the take-back. For the
        message in the budget position (first in the inbox with nothing written or in flight, or staged with not one
        byte accepted):
        - no budget, and the process not verified right now (`_correlated`) or going down (review round 17), or the
          line staged with main's write timer due (`_overdue`: earning cannot rescue an overdue line, review round
          15): nothing at all, not even a gate read (main's path: its write timer, `_hold_to_timeout`);
        - otherwise the gates are read once (`_read_gates`), and with that reading: with no budget yet it is EARNED
          if its counted state holds now (the process verified, live, not stopping, nothing host-side withholding),
          and its budget starts here, from zero (`counted`; the first interval counts from this pass's end, `_mark`);
        - with a budget: the interval since the last pass's end is added if the message was in a counted state at
          both ends (the end of the last pass, `counted_at`; now, in this reading), and `_spent` compares.
        Conservative: no time in which the host may have withheld it, or the CLI's turn may have ended, is counted; it
        loses at most one pass interval each time the message enters or leaves a counted state. A budget is acted
        on at the decision at which it is reached, in its counted state, so it never stops a process nor fails a
        message while the host withholds it. Returns (whether it acted, the gate reading or None); when it acted,
        nothing else is written in that pass."""
        msg = self._holder(s)
        if msg is None or (msg.counted is None and (not self._correlated(s) or s.stopping_since is not None
                                                     or self._overdue(s, msg, now))):
            return False, None      # (a stopping process earns nothing: no reading could decide anything, round 17)
        gates = self._read_gates(s)
        counting = self._counting(s, msg, gates)
        if msg.counted is None:
            if counting:
                msg.counted = 0.0       # earned: counting starts, from zero, here (`_mark` sets its end)
            return False, gates
        if not counting:
            return False, gates
        if msg.counted_at is not None:
            msg.counted += now - msg.counted_at
        return self._spent(s, msg, now, out), gates

    def _overdue(self, s: Session, msg: _Message, now: float) -> bool:
        """`msg` is the line staged with main's write timer due now (as `_hold_to_timeout` will find it)."""
        return s.writing is msg and s.busy_since is not None and now - s.busy_since >= self.timeout_minutes * 60

    def _mark(self, s: Session, now: float, gates: tuple[bool, bool] | None) -> None:
        """The end of every pass: one end of the next interval (`_decide`): a message with a budget is in a counted
        state as the pass ends, in the decision point's reading (none taken: it is not), or it is not."""
        msg = self._holder(s)
        if msg is not None and msg.counted is not None:
            msg.counted_at = now if gates is not None and self._counting(s, msg, gates) else None

    def _spent(self, s: Session, msg: _Message, now: float, out: list[HostEvent]) -> bool:
        """THE one comparison of a message's no-progress budget with the turn timeout, made only by `_decide`, in a
        counted state (a live process exists). A message has a budget only once it is EARNED (`_decide`: its process
        verified and its counted state, both at a decision point; review rounds 14 and 15: no provisional budget, no
        evidence carried), and from then on that budget governs it until the first byte of its line is
        accepted (`_write`), whatever the process's verification state is meanwhile; with VERIFIED empty no message
        ever has one, and its path is main's. It counts only COUNTED time (`_counting`), from its earning, kept
        across unstages, restagings, the end of the CLI's turns and a downgrade. When it reaches the timeout:
        - a first budget is the 7b restart: True; the message stays (or goes back, unstaged) first in the
          inbox, its second budget starts from nothing, and its process is stopped;
        - a second budget: True; it fails as main's `_held` fails it, never written.
        So an unwritten budgeted message accumulates at most two timeouts of counted time (each plus at most one
        pass); the budget never fails it at its first expiry (that is the 7b), and a host-side wait never makes the
        BUDGET act (it is not a counted state). Main's write timer still applies to a staged line the host withholds
        with `may_start` false (latched, not silent: it stays staged), and can fail it then, as on main. Its wall
        time is unbounded only by host-side waits, as on main."""
        if msg.counted is None or msg.counted < self.timeout_minutes * 60:
            return False
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

    def _stop(self, s: Session, now: float, at_signal: bool = False) -> None:
        """`at_signal` (review round 8, R8-01): passed True only by a single-session steering caller, for the stop
        that directly follows a live attempt's drain in that same call, or the contact cut's: the stop is then dated
        by a clock read immediately before its SIGTERM (after the registry, `poll` and the group probe, R8-02), so no
        SIGKILL comes before the grace from it. Every other caller passes nothing: main's `now`, exactly as on main.
        Nothing about the dating is stored or inferred from a slot or a past drain."""
        if s.child is not None and s.stopping_since is None:
            s.stopping_since = now
            self.groups.stopping(s.group_token)     # from now on nothing starts in its directory until it is gone
            # The whole process group (PopenChild). Not once all of it is known to be gone (a caller may have
            # stopped it already, node.shut_down): its id could be another group's by now.
            if s.child.poll() is None or self.group_alive(s.child.pid):
                if at_signal:
                    s.stopping_since = self.clock()
                if s.held is not None and s.held.fenced_at is not None:
                    s.held.fenced_at = self.clock()     # (shoulds) the bounded publication counts from the SIGTERM
                s.child.terminate()

    def _release_steer(self, s: Session) -> None:
        """(Review round 9, R9-02) Releasing a process releases its steer slot: the attempt was taken for that
        process, and N's end (settled before this on every path that lets a process go) decided it. Only the uuid
        stays, in `steered`, so the same steer is never taken again; a replacement process starts with no slot."""
        s.steer = None      # (one process at a time: any slot held now was taken for the one let go of)

    def _stop_from(self, s: Session, now: float, steering: bool) -> None:
        """A single-session path's stop (R8-01): dated at its SIGTERM when `steering` (this call drained a live
        attempt, or is the cut); otherwise main's own call, `_stop(s, now)`, unchanged in shape."""
        if steering:
            self._stop(s, now, at_signal=True)
        else:
            self._stop(s, now)

    def _end(self, s: Session, detail: str, now: float, out: list[HostEvent], at_signal: bool = False) -> None:
        def decide():
            s.ended, s.error, s.waiting = True, detail[:300], False
            self._fail(s, f"session ended: {detail}"[:300], out)
        if self._steering(s):
            # (ruling T) a close, a fatal frame or a host error of a steering session: claimed under the cutoff lock;
            # after the latch, nothing (the bulk stop owns it)
            if not self._claim(s, decide):
                return
        else:
            decide()
        undelivered = tuple(m.id for m in s.inbox)
        s.inbox.clear()
        try:
            self._stop_from(s, now, at_signal)
        finally:
            # (Review round 3, ruling M) the decided `ended` is transferred whatever the stop did (it may raise)
            ended = HostEvent(s.session_id, "ended", detail=s.error, undelivered=undelivered)
            if s.held is not None and s.held.fenced_at is not None:
                s.held.after.append(ended)      # N's held end comes first, once the bounded stop is over (part D)
            else:
                out.append(ended)

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
        s, out = self._session(session_id), self._outbox
        now = self.clock()
        drained = False
        if (s.in_flight is not None and s.in_flight.id == message_id) or (
                s.writing is not None and s.writing.id == message_id):
            # Only a cancel of the live turn stops the process: what it wrote is read first, so a native end already
            # in the pipe settles N as N's own (S2-06). A cancel of a message behind it reads nothing (R2-04).
            drained = self.live_attempt(s)
            now = self._drain_before_stop(s, now, out)
        # A restart's message that is cancelled no longer asks for the interrupt: one not written yet never is, and the
        # turn keeps its own outcome either way. (The message itself is the caller's, not here.)
        if s.restart_by == message_id:
            if not s.restart_written and s.restart_mark is not None:
                self._unwritten(s)
            self._forget_restart(s)
            return self._take_outbox()
        if (s.in_flight is not None and s.in_flight.id == message_id) or (
                s.writing is not None and s.writing.id == message_id):
            if self._steering(s):
                # (ruling T) a steering session: the claim under the cutoff lock; after the latch, nothing
                if not self._claim(s, lambda: (s.outbuf.clear() if s.writing is not None else None,
                                               self._fail(s, detail, out))):
                    return self._take_outbox()
            else:
                if s.writing is not None:
                    s.outbuf = bytearray()
                self._fail(s, detail, out)
            self._stop_from(s, now, drained)
            return self._take_outbox()
        for box in (s.inbox,):
            for m in list(box):
                if m.id == message_id:
                    box.remove(m)
                    out.append(HostEvent(s.session_id, "turn_ended", m.id, ok=False, delivered=False, detail=detail))
        return self._take_outbox()

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

    def _steer_gate(self, s: Session, slot: SteerSlot, now: float, fence: bool = True) -> bool:
        """Every gate of a write of the steer's bytes, asked at the write itself (review round 1, S2-02): exactly
        while N's attempt may run in this process (part D's authority): not revoked, refused or fenced, N's outcome
        open, N the turn in flight and not ended, the process live and not stopping, its binding the session's, the
        CLI able to steer, `may_start`, the parent's turn gate, the node's authority now, and a frame taken in part
        still within STEER_PARTIAL_SECONDS of its first byte. That limit is asked LAST, on the clock read after
        every callback above (R2-03): a callback that takes time cannot carry the write past it. `fence`: False asks
        whether only the fence stands in the way. Under the attempt's lock (re-entrant: the write boundary holds it
        across this check and its write, R3-01), the contact read on a clock taken under that lock."""
        with slot.lock:
            return self._steer_gate_locked(s, slot, fence)

    def _steer_gate_locked(self, s: Session, slot: SteerSlot, fence: bool) -> bool:
        """(The attempt's lock held.) Every rule; the two clocks LAST, on one reading taken after every callback has
        returned (R2-03, R3-01): the contact (whose expiry latches the break) and the 5 s of a frame taken in part."""
        if not (slot.revoked is None and slot.refused is None
                and (not fence or (not slot.fenced and (self.steer_capable is None or self.steer_capable(s))))
                and slot.outcome is None and not slot.stopping and not self.steer_stopped
                and s.in_flight is not None and s.in_flight.id == slot.into and s.child is not None
                and s.stopping_since is None and not s.ended and not s.unbound
                and "steer" in providers.modes(s.provider)
                and (self.may_start is None or self.may_start())
                and (self.turn_gate is None or self.turn_gate(s))
                and (self.steer_authority is not None and self.steer_authority(s))):
            return False
        now = self.clock()
        return slot.contact_ok(now) and (slot.partial_since is None or now - slot.partial_since < STEER_PARTIAL_SECONDS)

    def _steer_writable(self, s: Session, slot: SteerSlot, now: float) -> bool:
        """The steer's first byte may be queued NOW: an offer held authority for it, and every gate holds."""
        return slot.authorized and self._steer_gate(s, slot, now)

    def _steer_hold(self, s: Session, slot: SteerSlot, now: float, out: list[HostEvent]) -> bool:
        """At a write boundary, with the steer's frame queued: True when its bytes may go now. Otherwise, a frame
        taken in part is cut (S2-02); one not a byte of which went is taken out of the buffer, and, unless the
        fence of a listing that does not name `steer` is all that stands in the way (S2-05: kept, to be queued
        again once an authoritative listing still lists S), revoked for good before that (S2-04)."""
        if self._steer_gate(s, slot, now):
            return True
        self._steer_hold_failed(s, slot, now, out)
        return False

    def _steer_hold_failed(self, s: Session, slot: SteerSlot, now: float, out: list[HostEvent]) -> None:
        """A gate failed with the frame queued: cut it if taken in part; keep it out of the buffer if only the fence
        stands in the way; else revoke it for good and take it out. Once the node is stopping (R5-02): nothing at all,
        no cut, no drain, no read, no signal; the frame stays as it is (a partial frame stays partial) and the stop of
        every turn decides N by what was already read."""
        if self._steer_stopped(slot):
            return
        if slot.sent > 0:
            self._steer_cut(s, now, out)
        elif slot.fenced and self._steer_gate(s, slot, now, fence=False):
            self._steer_unqueue(s, slot, keep=True)
        else:
            with slot.lock:
                slot.revoked = slot.revoked or "gate"
            self._steer_unqueue(s, slot)

    def _steer_write(self, s: Session, now: float, out: list[HostEvent]):
        """Review round 3, structure 1: THE write boundary for a buffer that holds steer bytes. The buffer is prepared
        first; then, holding the attempt's lock, the clock is read fresh, every rule is evaluated
        (`_steer_gate_locked`: the contact clock and its latch, the fence, every gate, N, the 5 s) and the
        non-blocking pipe write is made; then the lock is released. An acknowledgement that latches a break takes the
        same lock (SteerSlot.ack), so a break is either seen by this check or comes after the write returned (R3-01).
        The bytes the pipe took or the OSError; None when a rule failed (the frame then held, cut or revoked)."""
        slot = s.steer
        data = bytes(s.outbuf)
        hook = self.steer_write_hook
        if hook is not None:
            hook("before", s)
        with slot.lock:
            ok = self._steer_gate_locked(s, slot, True)
            n = None
            if ok:
                try:
                    n = s.child.write(data)
                except OSError as e:
                    n = e
                if not isinstance(n, OSError):
                    self._steer_count(s, slot, n)
        if hook is not None:
            hook("after", s)
        if not ok:
            self._steer_hold_failed(s, slot, now, out)
        return n

    def _steer_count(self, s: Session, slot: SteerSlot, n: int) -> None:
        """(The attempt's lock held.) What a write of `n` bytes did to the frame: written whole, or taken in part."""
        self._steer_count_bytes(s, slot, n)
        facts = getattr(s.provider, "steer_facts", None)
        if facts is not None and slot.sent > 0:
            facts(sent=slot.sent)       # (Claude: no order fallback for N in THIS attempt once a byte went)

    def _steer_count_bytes(self, s: Session, slot: SteerSlot, n: int) -> None:
        start, length = slot.mark
        start -= n
        if start + length <= 0:
            slot.mark, slot.sent = None, length     # the whole frame is in the pipe
            written = getattr(s.provider, "steer_written", None)
            if written is not None:
                written()           # only an answer read from now on is evidence (S2-01)
        else:
            slot.mark = (start, length)
            if start < 0:       # some of it is in the pipe: completed within STEER_PARTIAL_SECONDS, or cut
                slot.sent = -start
                if slot.partial_since is None:
                    slot.partial_since = self.clock()

    def _steer_pass(self, s: Session, now: float, out: list[HostEvent]) -> None:
        """Write the steer while it may be written (`_steer_writable`), once; complete a frame taken in part, or cut
        it; take back a frame not one byte of which went, once its authority is lost."""
        slot = s.steer
        if slot is None or s.child is None or s.stopping_since is not None:
            return
        owns = getattr(s.provider, "owns", None)
        if slot.outcome is None and owns is not None and owns():
            slot.proved = True
        if self._steer_stopped(slot):
            return      # (R5-02) the node is stopping: the frame stays as it is; nothing is cut, dropped or read
        if slot.mark is not None:
            if slot.outcome is None or slot.sent > 0:
                self._steer_hold(s, slot, now, out)
            return
        if (slot.authorized and slot.sent == 0 and slot.outcome is None and slot.revoked is None
                and not self._steer_gate(s, slot, now, fence=False)):
            # Once authorized, a gate other than the fence that is seen closed is latched, as for a frame in the
            # buffer (S2-04, R2-02), whether a frame was made yet or not (R4-01): never written from then on.
            with slot.lock:
                slot.revoked = slot.revoked or "gate"      # (the node's authority may have said "contact" just now)
            if slot.frame is not None:
                self._steer_drop_frame(s, slot)
            return
        if slot.sent > 0 or slot.outcome is not None or not self._steer_writable(s, slot, now):
            return
        named = getattr(s.provider, "steer_named", None)
        data = ([slot.frame] if slot.frame is not None else named(slot.text, slot.uuid) if named is not None
                else s.provider.steer(slot.text))
        if not data:
            return      # the provider cannot yet (Codex: N's turn is not proved its own)
        start = len(s.outbuf)
        for b in data:
            s.outbuf += b
        slot.mark, slot.length = (start, len(s.outbuf) - start), len(s.outbuf) - start
        slot.frame = bytes(s.outbuf[start:])

    def _steer_unqueue(self, s: Session, slot: SteerSlot, keep: bool = False) -> None:
        """A queued steer frame not one byte of which reached the process is taken out of the buffer. Its request is
        retired (it is never written), unless `keep` (a fence: the very same frame may go later, S2-05)."""
        start, length = slot.mark
        del s.outbuf[start:start + length]
        slot.mark = None
        if s.restart_mark is not None and s.restart_mark[0] > start:
            s.restart_mark = (s.restart_mark[0] - length, s.restart_mark[1])
        if keep:
            return
        self._steer_drop_frame(s, slot)

    def _steer_drop_frame(self, s: Session, slot: SteerSlot) -> None:
        """The steer's frame, never written, is given up: its request is retired (never answered, never sent)."""
        slot.frame = None
        retire = getattr(s.provider, "steer_retire", None)
        if retire is not None:
            retire()

    def _steer_cut(self, s: Session, now: float, out: list[HostEvent]) -> None:
        """The cut (part D): a steer frame taken in part cannot be completed. Writing stops, what the process wrote
        is read (N's own end may be there), N is reported honestly (its native result if it had one, else failed with
        STEER_CUT) and the process is stopped."""
        slot = s.steer
        if slot is None or slot.process is not s.child:
            # (R9-02) Not this process's attempt (its own went when it was let go of): nothing to cut here.
            if slot is not None:
                s.steer = None
            return
        hook = self.steer_cut_hook
        if hook is not None:
            hook(s)
        with self._steer_stop_lock:
            # (R6-01) The cut takes ownership under the stop latch's lock: it cannot begin once the node began to stop
            # (the stop of every turn decides N then, nothing else is done here), and the latch is never set in the
            # middle of this check and the cut's first act. Its drain stops reading at the latch.
            if self.steer_stopped:
                return
            s.outbuf.clear()
        log.warning("session %s: a steer to its process could not be written whole: the process is stopped",
                    s.session_id)       # (R7-04: outside the latch's lock)
        now = self._drain_before_stop(s, now, out)    # across reads while they make progress (R2-05); never raises
        if s.in_flight is not None and slot is not None and s.in_flight.id == slot.into:
            if not self._claim(s, lambda: self._fail(s, STEER_CUT, out)):
                return      # (ruling T) the latch came during the drain: the bulk stop owns it now
        s.steer = None
        self._stop(s, now, at_signal=True)      # (R8-01) the cut's own stop, with or without a drain
        if self._defer_save:
            self._save_due = True       # (ruling T) the watchdog's boundary persists nothing: the loop's next tick does
        else:
            self._save_state()

    def _steer_end(self, s: Session, msg_id: str, native: bool, evidence: str | None = None) -> dict | None:
        """N (`msg_id`) ended, natively (the provider's own end of it) or not (the host gave it up): the outcome of the
        steer into it, decided now, once (part F's table; for Claude, from the typed evidence of N's end, `evidence`,
        as the one adjudicator's rules say: `_steer_adjudicate`)."""
        slot = s.steer
        if slot is None or slot.into != msg_id or slot.outcome is not None:
            return None
        with self._steer_stop_lock:
            # (R7-01) S's evidence eligibility is the ONE global latch, read under its lock, so the decision is
            # linearized with `steer_stopping` setting it: evidence processed once it is set never counts for S,
            # whether or not this slot was marked yet (rule 2). N's own end is handled as on main either way.
            if slot.stopping or self.steer_stopped:
                native = False
            if slot.mark is not None and slot.sent == 0:
                self._steer_unqueue(s, slot)
                outcome = {"outcome": "not_delivered", "why": "not_written"}
            elif slot.sent == 0:
                outcome = {"outcome": "not_delivered", "why": "not_written" if slot.proved else "not_owned"}
            elif slot.mark is not None:
                outcome = {"outcome": "unconfirmed", "why": "partial"}
            elif not native:
                outcome = {"outcome": "unconfirmed", "why": "turn_cut"}     # N ended with the process, or given up
            elif getattr(s.provider, "steer_named", None) is not None:
                # Claude (review round 1, ruling A): N's own result with exactly [N, S] is `in_turn`; the S-only
                # result inside N's own bracket is N's, S `unconfirmed`; any other native end of N with S written
                # whole holds N's end (S is queued in the CLI): `_hold`.
                if evidence == "n_with_s":
                    outcome = {"outcome": "in_turn", "why": "answered"}
                elif evidence == "s_only":
                    outcome = {"outcome": "unconfirmed", "why": "s_only"}
                elif evidence == "n_unproved":
                    # (ruling L) N's own result with N's identity intact, in a shape that proves nothing about S
                    outcome = {"outcome": "unconfirmed", "why": "unproved"}
                else:
                    return HOLD
            else:
                said = getattr(s.provider, "steer_result", lambda: None)()
                outcome = ({"outcome": "in_turn", "why": "answered"} if said == "ok" else
                           {"outcome": "not_delivered", "why": "refused"} if said == "refused" else
                           {"outcome": "unconfirmed", "why": "no_answer" if said is None else "other_answer"})
            slot.outcome = outcome
            done = getattr(s.provider, "steer_done", None)
            if done is not None:
                done()      # (Claude: the attempt is retired before any further frame is read; ruling H: atomically)
        if slot.mark is None or s.child is None:
            s.steer = None      # decided, and nothing of it is left to write
        self._say(logging.INFO, "session %s: the steer into message %s: %s (%s)", s.session_id, msg_id,
                  outcome["outcome"], outcome["why"])
        return dict(outcome)

    def _hold(self, s: Session, end: HostEvent, now: float, out: list[HostEvent]) -> None:
        """N's native end came and a steer written whole into N is not settled by it: N's end is held (module text).
        N stays in flight (nothing else is written; the node reports nothing of N), on the bound of a turn timeout
        from now, and the checkpoint (`held`: N's native end, as it came) goes to the node once. In an incarnation
        already unverified, or after a deviation in this attempt, nothing can prove S quiet: the finalizer, at once
        (`_hold_pass`)."""
        slot = s.steer
        s.held = _Held(end, now, lost=bool(slot is not None and slot.lost) or not getattr(s.provider, "verified", True))
        s.busy_since = now
        facts = getattr(s.provider, "steer_facts", None)
        if facts is not None:
            facts(phase="held")
        out.append(HostEvent(s.session_id, "held", end.message_id, ok=end.ok, denials=end.denials,
                             meta=dict(end.meta), detail=end.detail, text=end.text, answered=end.answered))
        log.info("session %s: the end of message %s is held while the steer into it is unresolved", s.session_id,
                 end.message_id)

    def _steer_adjudicate(self, s: Session, ev: Event, now: float, out: list[HostEvent]) -> bool:
        """THE one adjudicator of the evidence for a Claude steer S after N's native end, and of a deviation before it
        (review round 1, ruling A; N's end itself is `_steer_end`'s, called from here's caller under the same latch).
        Inside the stop latch's lock: evidence read once the node began to stop never counts (rule 2), so the stop of
        every turn decides S by what was adjudicated before. It decides, records and publishes; the adapter only
        classified. True: the event was S's own (no message's), and nothing more of it is handled."""
        slot, held = s.steer, s.held
        own = ev.turn == "steer"
        if ev.steer is None or slot is None or slot.outcome is not None:
            return own
        retired = None
        with self._steer_stop_lock:
            # (Review round 2, ruling H) Only the decision and the adapter's retirement are inside the cutoff lock;
            # the log line, the event and the state saved come after it.
            if slot.stopping or self.steer_stopped:
                return own      # after the latch: no evidence counts for S
            if ev.steer == "lost":
                slot.lost = True
                if held is not None:
                    held.lost = True
                return own
            if held is None or held.fenced_at is not None:
                return own      # N's end (n_with_s, n_without_s, s_only) is decided at N's end (`_steer_end`)
            if ev.steer == "s_open" and not held.s_turn:
                # S's own turn: a fresh clock (part D), from the moment its `init` was read (a drain's read is later
                # than the pass's `now`)
                held.s_turn, s.busy_since = True, self.clock()
                facts = getattr(s.provider, "steer_facts", None)
                if facts is not None:
                    facts(phase="s_turn")
                return True
            if ev.steer == "s_end" and held.s_turn:
                retired = self._retire_hold(s, {"outcome": "next_turn", "why": "own_turn", "result": {
                    "ok": bool(ev.ok), "detail": ev.detail[:300], "denials": list(ev.denials), "meta": dict(ev.meta),
                    "text": ev.text, "answered": bool(ev.answered)}})
                own = True
            elif ev.steer == "s_consumed" and not held.s_turn:
                retired = self._retire_hold(s, {"outcome": "unconfirmed", "why": "consumed"})
        if retired is not None:
            self._emit_hold(s, out, *retired)
        return own

    def _publish_hold(self, s: Session, out: list[HostEvent], steer: dict) -> None:
        """N's end, once: its native end unaltered plus S's outcome (`steer`), then what waited behind it. The
        attempt is retired in the adapter FIRST, before any further frame is read (ruling A)."""
        self._emit_hold(s, out, *self._retire_hold(s, steer))

    def _retire_hold(self, s: Session, steer: dict) -> tuple:
        """The decision's state, atomically (under the cutoff lock where the adjudicator calls it, ruling H): the
        adapter's attempt retired, the hold taken, the slot's outcome set, N no longer in flight. No log, no I/O."""
        done = getattr(s.provider, "steer_done", None)
        if done is not None:
            done()
        held, s.held = s.held, None
        slot = s.steer
        if slot is not None and slot.into == held.end.message_id:
            with slot.lock:
                slot.outcome = {"outcome": steer["outcome"], "why": steer["why"]}
            if slot.mark is None or s.child is None:
                s.steer = None
        if s.in_flight is not None and s.in_flight.id == held.end.message_id:
            s.in_flight, s.busy_since = None, None
        return held, steer

    def _emit_hold(self, s: Session, out: list[HostEvent], held: "_Held", steer: dict) -> None:
        """The decided hold made public: the log line, N's end and what waited behind it into the host's outbox
        (ruling I), and the state saved, outside the cutoff lock (ruling H); during a stop of every turn the save
        waits until every session has its SIGTERM (`stop_turns`, `stop_all`)."""
        out.append(dataclasses.replace(held.end, steer=dict(steer)))
        out.extend(held.after)      # (ruling M: the events first; a raising log handler or save loses nothing)
        try:
            self._say(logging.INFO, "session %s: the steer into message %s: %s (%s)", s.session_id,
                      held.end.message_id, steer["outcome"], steer["why"])
            if self._defer_logs is not None and self._claimer == threading.get_ident():
                self._save_due = True       # (ruling T: nothing persisted under the cutoff lock; the loop saves)
            elif not self._defer_save:
                self._save_state()
        except Exception:
            log.exception("session %s: could not record the sessions' state after a held end", s.session_id)

    def _finalize_hold(self, s: Session, why: str, out: list[HostEvent]) -> None:
        """The finalizer (part D) on any end the host makes of a held N: S as what was adjudicated decides it
        (`next_turn` failed if S's own turn was proved, else `unconfirmed`). Published at once when the process is
        gone, at a stop of every turn, at shutdown, at an unexpected error's end (rule 5), or once the node began to
        stop; otherwise after the caller's stop, when the group is gone or 2 x kill_grace has passed (`_hold_fence`,
        checked first in every tick, before any read: ruling B)."""
        held = s.held
        if held.decided is None:
            if held.s_turn:
                held.decided = {"outcome": "next_turn", "why": "own_turn_cut",
                                "result": {"ok": False, "detail": STEER_S_CUT.format(why=why)[:300], "denials": [],
                                           "meta": {}, "text": "", "answered": False}}
            else:
                held.decided = {"outcome": "unconfirmed", "why": "turn_cut"}
        if s.child is None or self._at_once or self.steer_stopped:
            self._publish_hold(s, out, held.decided)
            return
        if held.fenced_at is None:
            held.fenced_at = self.clock()

    def _hold_fence(self, s: Session, now: float, out: list[HostEvent]) -> None:
        """A held end the finalizer decided: published once the process is gone or the bound of the stop has
        passed (2 x kill_grace from it); a member that lingers longer stays under main's stopping-group barrier.
        Called first for every session in every tick, before any read (ruling B), and after each reap."""
        held = s.held
        if held is None or held.fenced_at is None:
            return
        if s.child is None or now - held.fenced_at >= 2 * self.kill_grace:
            self._publish_hold(s, out, held.decided)

    def _hold_pass(self, s: Session, now: float, out: list[HostEvent]) -> None:
        """After the pass's reads, for a held end: the finalizer at once when the adjudicated evidence says nothing
        can prove S quiet (`lost`), or when the attempt's own contact clock ran out or the node's write authority is
        gone (the cut, as for any live attempt: ruling A). The watchdog's boundary makes the same cut
        (`steer_deadlines`, ruling G)."""
        self._hold_cut(s, now, out)

    def _hold_cut(self, s: Session, now: float, out: list[HostEvent]) -> None:
        """THE cut of a held end (review round 2, ruling G; callers: `_hold_pass` in the loop's pass, and
        `steer_deadlines`, the deadline watchdog's boundary). After the global stop latch it initiates nothing: the
        latch is checked under its lock, which is held across the decision and the SIGTERM, so a stop of every turn
        (Shutdown simplified) publishes the held end from the evidence already processed."""
        held, slot = s.held, s.steer
        if held is None or held.fenced_at is not None or s.child is None or s.stopping_since is not None:
            return
        if self.steer_stopped or (slot is not None and slot.stopping):
            return
        with self._steer_stop_lock:
            # (Review round 4, ruling T) the latch check that matters is this one, under the cutoff lock, with the
            # expiry's own state change: a latch that came after the check above changes nothing here
            if self.steer_stopped or (slot is not None and slot.stopping):
                return
            why = None
            if held.lost:
                why = "the process could not show that the steer was settled"
            elif slot is not None and (slot.contact_ran_out(now) or (
                    self.steer_authority is not None and not self.steer_authority(s))):
                why = "the steer's contact with the hub ran out"
        if why is None:
            return
        now = self._drain_before_stop(s, now, out)
        if s.held is held:
            self._cutoff_stop(s, why, now, out)

    def _steering(self, s: Session) -> bool:
        """The session holds a Claude steer whose outcome is open, or a held end: from the latch on the bulk stop
        owns it (ruling T). A Codex attempt keeps step 2's rule (R6-01: a close whose drain met the latch decides N by
        what was read before it), pinned by tests/test_codex_steering.py, which this round leaves unchanged."""
        return s.held is not None or (s.steer is not None and s.steer.outcome is None and s.steer.holds)

    def _claim(self, s: Session, decide: Callable[[], None]) -> bool:
        """(Review round 4, ruling T) THE claim of a single-session stop of a steering session: under the cutoff
        lock, the latch checked and the decision made (`decide`: N failed, S decided, the fence stamped), frozen;
        nothing logged or persisted there (`_defer_logs`). After the latch it claims nothing and decides nothing: the
        bulk stop owns the session. The caller signals after it returns True (a stop claimed before the latch may
        signal after it: the same SIGTERM shutdown sends; its grace from that SIGTERM)."""
        with self._steer_stop_lock:
            if self.steer_stopped or (s.steer is not None and s.steer.stopping):
                return False
            self._defer_logs, self._claimer = [], threading.get_ident()
            try:
                decide()
            finally:
                logs, self._defer_logs, self._claimer = self._defer_logs, None, None
        for level, msg, args in logs:
            log.log(level, msg, *args)
        return True

    def _say(self, level: int, msg: str, *args) -> None:
        """A log line, deferred while a claim holds the cutoff lock (ruling T: nothing logged under it)."""
        if self._defer_logs is not None and self._claimer == threading.get_ident():
            self._defer_logs.append((level, msg, args))
        else:
            log.log(level, msg, *args)

    def _cutoff_stop(self, s: Session, why: str, now: float, out: list[HostEvent]) -> bool:
        """THE single-session stop of a session holding a live steer attempt or a held end (review round 3, ruling N;
        callers: `_hold_cut`, and the turn timeout `_hold_to_timeout`, the held one included). The decision and the
        fence are made under the cutoff lock; after the global latch it does nothing (the stop of every turn is the
        one that acts, Shutdown simplified). The group registry and the SIGTERM come after the lock is let go (a
        stop decided before the latch whose signal lands after it is the same signal shutdown sends). False: it did
        nothing."""
        if not self._claim(s, lambda: self._fail(s, why, out)):
            return False
        self._stop(s, now, at_signal=True)      # (dated at its SIGTERM: R8-01)
        return True

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
                and not (s.steer is not None and s.steer.mark is not None)     # a steer frame first, whole
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
        n = None
        steered = None
        if s.steer is not None and s.steer.mark is not None and s.outbuf:
            # The steer's bytes go only through the one boundary (`_steer_write`, R3-01).
            steered = s.steer
            n = self._steer_write(s, now, out)
            if n is None and (s.child is None or s.stopping_since is not None):
                return      # the cut stopped the process
            if n is None and s.steer is not None and s.steer.mark is not None:
                return      # (R5-02) the node is stopping: the frame stays in the buffer, and nothing of it goes
        if n is None and s.outbuf:
            steered = None
            try:
                n = s.child.write(bytes(s.outbuf))
            except OSError as e:
                n = e
        if isinstance(n, OSError):
            # The process no longer reads. A message that was not fully written never reached it; a turn in
            # flight (an answer to the process could not be written) is lost with the process. During a
            # handshake the message that made the process start is still in the inbox: it fails here too.
            s.outbuf.clear()
            detail = f"could not write to the process: {type(n).__name__}"
            live = self.live_attempt(s)
            if live:
                now = self._drain_before_stop(s, now, out)      # a native end behind buffered output is N's own
            if (s.provider.waits_for_ready and not s.ready) or s.checking:
                self._fail_start(s, detail, out)
            elif self._steering(s):
                self._cutoff_stop(s, detail, now, out)      # (ruling T: nothing after the latch)
                return
            else:
                self._fail(s, detail, out)
            self._stop_from(s, now, live)
            return
        if n is not None:
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
            if s.steer is not None and s.steer.mark is not None and steered is None:
                with s.steer.lock:
                    self._steer_count(s, s.steer, n)    # (a write that held no steer byte: only the mark moves)
            if s.steer is not None and s.steer.mark is None and s.steer.outcome is not None:
                s.steer = None      # its turn was already decided: the frame is whole, nothing is left
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

    def stop_turns(self, detail: str, own: bool = False) -> list[HostEvent]:
        """Give up on every turn in flight: each is reported failed and its process is stopped (the caller lost the
        hub, so nobody could see or stop the turn). Idle processes and the sessions themselves stay. (Ruling H) Every
        session gets its SIGTERM before anything a held publication records is persisted: the state is saved once,
        after the loop."""
        # (Review round 4, ruling S) `own` (the watchdog's self-stop): the split is made at publication: what this
        # stop publishes goes into an outbox of its own and is all it returns; what was already in the outbox stays
        # there for the loop's pre-reconciliation handoff (never behind a LOST).
        retained = None
        if own:
            retained, self._outbox = self._outbox, _Outbox(self)
        out = self._outbox
        now = self.clock()
        self._at_once = self._defer_save = True     # a held end is published here, at main's point (shutdown simplified)
        try:
            for s in list(self.sessions.values()):
                if s.child is not None and s.busy and not s.ended:
                    try:        # (ruling N) one session's fault never skips the others
                        self._fail(s, detail[:300], out)
                        self._stop(s, now)
                    except Exception:
                        log.exception("session %s: could not stop its turn", s.session_id)
        except BaseException:
            if own:     # (ruling V) what this stop published is kept, after what was there, for the next handoff
                mine, self._outbox = self._outbox, retained
                self._outbox.extend(mine.current())
                own = False
            raise
        finally:
            self._at_once = self._defer_save = False
            if own:
                mine, self._outbox = self._outbox, retained
                if out:
                    self._save_due = True       # (ruling T) no save on the watchdog's path: the loop saves
            elif out:
                self._save_state()
        if own:
            return mine.current()
        return self._take_outbox()

    # ------------------------------------------------------------ shutdown

    def stop_all(self) -> list[HostEvent]:
        """Shutdown: terminate every session's process group, wait the grace period, kill what remains of each group
        (its leader may be gone while a member is not). A turn in flight is reported failed. Sessions are not ended:
        a later host may reopen them with resume. (Ruling H) Every SIGTERM goes before anything is persisted."""
        out = self._outbox
        now = self.clock()
        live = [s for s in self.sessions.values() if s.child is not None]
        self._at_once = self._defer_save = True     # a held end is published here, at main's point (shutdown simplified)
        try:
            for s in live:
                try:        # (ruling N) one session's fault never skips the others
                    self._fail(s, "the session host shut down", out)
                    self._stop(s, now)
                except Exception:
                    log.exception("session %s: could not stop it at shutdown", s.session_id)
        finally:
            self._at_once = self._defer_save = False
        def going(s: Session) -> bool:
            try:
                return s.child.poll() is None or self.group_alive(s.child.pid)
            except Exception:       # (ruling T) a probe that fails is taken as "still going": it is killed below
                return True
        try:
            deadline = self.clock() + self.kill_grace
            while any(going(s) for s in live) and self.clock() < deadline:
                self.sleep(0.2)
            for s in live:
                # (Review round 4, ruling T) every step of the traversal per session: one session's fault (its
                # kill, read, close or cleanup) skips no other, and the final save runs in the `finally`
                steps = (lambda: s.child.kill() if going(s) else None,
                         lambda: self._read(s, now, out),     # the transcript keeps what was written until the end
                         lambda: (getattr(s.child, "close", None) or (lambda: None))(),
                         lambda: (os.close(s.transcript_fd) if s.transcript_fd is not None else None))
                for step in steps:
                    try:
                        step()
                    except Exception:
                        log.exception("session %s: a step of its shutdown failed", s.session_id)
                s.transcript_fd = None
                s.child, s.proc_start, s.ready, s.stopping_since, s.killed = None, None, False, None, False
                try:
                    self.groups.gone(s.group_token)
                except Exception:
                    log.exception("session %s: its group's record could not be cleared", s.session_id)
                s.group_token = None
                s.outbuf.clear()
                self._release_steer(s)
            for s in self.sessions.values():
                s.sealed_by = s.confirmed_by = None     # this host is done: what its processes said goes with it
        finally:
            self._save_state()
        return self._take_outbox()
