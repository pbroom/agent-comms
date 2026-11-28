"""(Ownership C3-2b-1) The lifecycle of ONE supervised launch: start it (`launch`), release it on evidence only
(`release`), and send it commands (`request_term`, `request_kill`, then `classify` when a command finds the pipe
closed). DESIGN_NOTES "Ownership C3-2b-1: the journal, restore-and-reserve, and the launch helpers (nothing can start
a shim yet)" and its "review round 1" have the crash windows, the launch states and the results as tables.

UNREACHABLE in this PR: no config path, spawn site, session host, runner, provider or node path imports or calls this
module (tests/test_shim_static.py pins it). The next PR makes a supervised spawn reachable, behind a switch, with the
complete stop, shutdown, release and recovery-fence paths.

Locks and I/O (a rule the wiring is bound by). Every helper here is called WITHOUT any host lock held. `launch` commits
the journal (two full syncs, three the first time) and writes GO, and `release` reads the marker
(`ShimChild.empty()`/`refused()`) and syncs the journal's directory: proof reads, journal syncs and GO stay OFF
deadline-critical paths and OUTSIDE the node's runner lock. The registry's lock is taken only for in-memory steps
(`Groups.admit_provisional`, `confirm_live`, `stopping`, `restore_held`, and `Groups.release`'s own): NO I/O, and never
GO, under it (review round 1, R1). A launch's own lock (`Launch.state_lock`, a host lock) serialises its lifecycle,
its release and its `accept` (review round 2, R6). No logging: results are returned (and a failure is raised).
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Hashable

from . import hostlog, node_groups, shimjournal
from .dispatch import process_start
from .shimchild import ABANDON_SECONDS, STATUS_UNKNOWN, ShimChild
from .shimjournal import Journal, Record

# `launch` outcomes
STARTED = "started"                         # GO confirmed whole; the entry still PROVISIONAL (fencing); the record
#                                             durable; HANDED_OFF to the caller, who stores it, then `accept()`s: (c)
NOT_ADMITTED = "not_admitted"               # the checkout was fenced (or admission closed) at the first look: nothing
REFUSED_AFTER_COMMIT = "refused_after_commit"   # fenced at the admission after the commit: no GO; record + HELD: (b)
GO_FAILED = "go_failed"                     # GO failed or is ambiguous: record + entry (STOPPING) retained: (b)
FAILED = "failed"                           # raised (an interrupt included): (a) or (b), as `launch` says; `error`
# `release` results
RELEASED = "released"                       # THIS call removed the record and lifted the entry (exactly one call does)
ALREADY_RELEASED = "already_released"       # an earlier (or a concurrent, waited-for) call released it
KEPT = "kept"                               # no evidence yet, or the record's removal failed: nothing lifted, HELD
# command results
QUEUED, PENDING, CLOSED = "queued", "pending", "closed"
# `classify`
RUNNING, PROVED, REFUSED, LOST, UNKNOWN = "running", "proved", "refused", "lost", "unknown"

# How far a launch got (its handler's one question): before the commit was entered; inside the commit; after it.
_BEFORE_COMMIT, _COMMITTING, _COMMITTED = 0, 1, 2


@dataclass
class Launch:
    """ONE supervised launch, and its cleanup OWNER (review round 1, R4): from before its shim can exist until
    `release` lets it go, this object holds the shim's handle; while it is in state (b) it is parked in the node's
    `Owners`, where a later `release` finds it whether or not `launch` returned it.

    (Review round 2, R6) Its LIFECYCLE, changed only under its own `state_lock`: ACTIVE (being launched by the thread
    that created it: findable, never eligible for cleanup) -> HANDED_OFF (returned STARTED to its receiver) or
    ABANDONED (the launch failed, or nothing was admitted: cleanup may take it) -> RELEASED (set only by the one
    `release` call that removed the record and lifted the reservation, or found nothing to remove: never committed,
    its shim reaped). `release` acts only on HANDED_OFF and ABANDONED launches; `accept` only on HANDED_OFF ones."""
    ACTIVE, HANDED_OFF, ABANDONED, RELEASED = "active", "handed_off", "abandoned", "released"
    launch_id: str
    token: tuple                            # (node_groups.SUPERVISED, launch_id)
    key: Hashable                           # the checkout's identity, taken once (node_groups.key)
    cwd: str
    handle: ShimChild | None = None
    record: Record | None = None
    outcome: str | None = None
    error: BaseException | None = None      # GO's failure (GO_FAILED), or what `launch` raised (FAILED)
    lifecycle: str = "active"               # (R6) ACTIVE, HANDED_OFF, ABANDONED, RELEASED: under `state_lock` only
    # (Review rounds 1 and 2, R4.2 and R6) This launch's own lock: its lifecycle, its release and its `accept`. A host
    # lock (a native RLock, entered only as `with hostlog.BOUNDARY, launch.state_lock:`); I/O (the proof's reads, the
    # record's removal) runs under it, never under the registry's; the registry's lock is taken inside it, never the
    # reverse.
    state_lock: Any = field(default_factory=lambda: threading.RLock(), repr=False, compare=False)

    def accept(self, groups: node_groups.Groups) -> bool:
        """(Review round 2, R5) The RECEIVER's last step, after it has stored this launch where its own stop and
        shutdown paths will find it: the entry, PROVISIONAL since the admission (the checkout fenced, while the CLI may
        already run), becomes LIVE (`Groups.confirm_live`), and from then on C1's sharing rule applies as for a direct
        start. True only when that took effect. False, with nothing changed, when the launch is not HANDED_OFF or its
        entry is no longer PROVISIONAL (made STOPPING or HELD meanwhile, or gone): the caller then treats the launch
        as STOPPING, never as started. An exception or an interrupt inside makes the entry HELD again before it is
        raised: until `accept` has returned True, the checkout is never admissible beside this launch's CLI."""
        with hostlog.BOUNDARY, self.state_lock:
            if self.lifecycle != Launch.HANDED_OFF:
                return False
            try:
                return groups.confirm_live(self.token)
            except BaseException:
                groups.restore_held(self.token, [self.key], self.cwd)
                raise


def _move(launch: Launch, to: str, frm: tuple[str, ...]) -> bool:
    """(R6) The launch's lifecycle moved to `to` from one of `frm`, under its own lock; False (unchanged) otherwise."""
    with hostlog.BOUNDARY, launch.state_lock:
        if launch.lifecycle not in frm:
            return False
        launch.lifecycle = to
        return True


class Owners:
    """(Review round 1, R4) Where the node finds every launch it must still ask for proof: each `Launch` is parked
    here BEFORE its shim can exist, and stays while it is in state (b) (committed and fenced) or while its shim,
    abandoned before GO, could not be reaped; it leaves when `launch` hands it to its caller (STARTED, (c)) or reaches
    state (a) (nothing exists), or when `release` lets it go. One dict keyed by launch id: one store, one pop, one
    copy each (atomic under the GIL), no lock and no I/O. The node keeps ONE `Owners`, beside its journal (the wiring
    PR), and asks `release` for each parked launch on its passes.

    (Review round 2, R6) This table only STORES: being parked makes a launch FINDABLE, not eligible. A launch's
    lifecycle (ACTIVE, HANDED_OFF, ABANDONED, RELEASED) is serialised by each launch's own lock (`Launch.state_lock`),
    never by this table: a sweep that finds an ACTIVE launch here is answered KEPT, and nothing is done to it."""

    def __init__(self) -> None:
        self._by_id: dict[str, Launch] = {}

    def park(self, launch: Launch) -> None:
        self._by_id[launch.launch_id] = launch

    def unpark(self, launch: Launch) -> None:
        self._by_id.pop(launch.launch_id, None)

    def parked(self) -> list[Launch]:
        return list(self._by_id.values())


def _pause(pause: Callable[[str, Launch], None] | None, step: str, out: Launch) -> None:
    if pause is not None:
        pause(step, out)


def launch(*, groups: node_groups.Groups, journal: Journal, owners: Owners, marker_dir: str, cwd: str,
           argv: list[str], env: dict[str, str], kill_grace: float, log: str, stderr_log: str, piped: bool = True,
           python: str | None = None, go_timeout: float = 5.0, start_of: Callable[[int], Any] = process_start,
           test_gate: str | None = None, pause: Callable[[str, Launch], None] | None = None) -> Launch:
    """Start one supervised launch, TRANSACTIONALLY. Call it with no host lock held.

    1. A first look (`groups.blocked`): a fenced checkout (or closed admission) gets NOT_ADMITTED, nothing done.
    2. The owner (the returned `Launch`) is parked in `owners`; then `ShimChild.start` hands it the handle the moment
       the handle exists (`adopt`): the shim, its cwd "/" (outside every checkout), a fresh marker in the node's
       private marker directory, the secret sent. It waits for GO and can start nothing without it.
    3. The record (`Record`), then `journal.commit`: durable before anything is reserved or started. NO registry entry
       exists for the checkout before this commit.
    4. `groups.admit_provisional`: under the registry lock, the admission check and a PROVISIONAL entry that FENCES
       the checkout; no I/O. Refused: REFUSED_AFTER_COMMIT (no GO; the writer closed; a HELD reservation for the
       record; the owner parked).
    5. GO, OUTSIDE the registry lock (bounded by `go_timeout`). When GO is CONFIRMED whole the launch is STARTED and
       handed to the caller (unparked, HANDED_OFF) with its entry STILL PROVISIONAL: nothing in `launch` makes it
       LIVE (review round 2, R5). The receiver stores it where its own stop and shutdown paths will find it, THEN
       calls `Launch.accept()`, the last transition (LIVE); an `accept()` that answers False means "stopping". A GO
       that failed or is ambiguous: GO_FAILED (the entry STOPPING, the record kept, the writer closed so the shim
       cleans up on EOF: never started, or its CLI stopped; the owner parked, ABANDONED).

    Every exit, at any line (an exception or an interrupt included), leaves exactly one of (DESIGN_NOTES, review
    rounds 1 and 2, the launch-state table): (a) nothing exists (no unreaped shim, no record, no entry; the owner
    unparked; ABANDONED); (b) committed and fenced (the record, a fencing entry: PROVISIONAL, STOPPING or HELD; the
    writer closed; the owner parked in `owners`, ABANDONED, and returned when `launch` returns); (c) GO confirmed,
    PROVISIONAL, HANDED_OFF and returned. Raised before the commit was entered: the shim abandoned before GO and
    reaped by its handle, (a). Raised by the commit: (b) if anything may be at the record's path (`Journal.exists`: a
    failed commit removes nothing, R3), else (a). Raised after the commit: the entry is made HELD (or installed HELD)
    under the registry lock, never dropped, the writer closed, (b). While `launch` runs the launch is ACTIVE: a
    concurrent `release` (a sweep over `owners`) finds it and does nothing (R6). (One stated limit: a shim that does
    not exit even after its SIGKILL within the bound stays parked, unreaped; no GO was ever attempted for it, so no
    CLI exists.)

    `pause(step, launch)`: TEST-ONLY (the node never passes it), at "started", "committed", "admitted", "go", "went"
    and "handed"."""
    k = node_groups.key(cwd)
    lid = shimjournal.new_launch_id()
    out = Launch(launch_id=lid, token=shimjournal.token_of(lid), key=k, cwd=cwd)
    if groups.blocked(cwd):
        out.outcome = NOT_ADMITTED
        _move(out, Launch.ABANDONED, (Launch.ACTIVE,))     # (nothing exists: a release finds nothing to remove)
        return out
    owners.park(out)        # (R4: the owner is findable before its shim can exist)
    step = _BEFORE_COMMIT
    try:
        ShimChild.start(marker_dir=marker_dir, log=log, stderr_log=stderr_log, cwd="/", python=python,
                        test_gate=test_gate, piped=piped, adopt=lambda h: setattr(out, "handle", h))
        _pause(pause, "started", out)
        try:
            start = start_of(out.handle.pid)
        except Exception:
            start = None
        out.record = Record(launch_id=lid, checkout_key=k, cwd=cwd, boot=out.handle.boot, marker=out.handle.marker,
                            shim_pid=out.handle.pid, shim_start=start if isinstance(start, str) else None)
        step = _COMMITTING
        journal.commit(out.record)
        step = _COMMITTED
        # ---- after the commit: the record is retained whatever happens, and the entry is never dropped
        _pause(pause, "committed", out)
        if not groups.admit_provisional(cwd, out.token, k):
            groups.restore_held(out.token, [k], cwd)
            out.handle.close_commands()
            out.outcome = REFUSED_AFTER_COMMIT
            _move(out, Launch.ABANDONED, (Launch.ACTIVE,))
            return out
        _pause(pause, "admitted", out)
        _pause(pause, "go", out)
        try:
            out.handle.go(argv, cwd=cwd, env=env, kill_grace=kill_grace, timeout=go_timeout)
        except Exception as e:
            out.handle.close_commands()
            groups.stopping(out.token)
            out.outcome, out.error = GO_FAILED, e
            _move(out, Launch.ABANDONED, (Launch.ACTIVE,))
            return out
        _pause(pause, "went", out)
        out.outcome = STARTED   # (R5: the entry stays PROVISIONAL; the receiver's `accept()` makes it LIVE)
        owners.unpark(out)      # (c): unparked while still ACTIVE, so no sweep can take it as it is handed off
        _move(out, Launch.HANDED_OFF, (Launch.ACTIVE,))
        _pause(pause, "handed", out)
        return out
    except BaseException as e:
        _unwind(groups, journal, owners, out, step, e)
        raise


def _unwind(groups: node_groups.Groups, journal: Journal, owners: Owners, out: Launch, step: int,
            e: BaseException) -> None:
    """`launch`'s one handler: settle the launch in state (a) or (b) (see `launch`), then the caller raises. The
    launch stays ACTIVE (no sweep acts on it) while it is settled, and becomes ABANDONED (eligible for cleanup) after,
    even if this is cut short (R6): it never reached its receiver, so a HANDED_OFF one becomes ABANDONED too."""
    owners.park(out)        # (again: an interrupt may have come after `launch` unparked it)
    out.outcome, out.error = FAILED, e
    try:
        _settle(groups, journal, owners, out, step)
    finally:
        _move(out, Launch.ABANDONED, (Launch.ACTIVE, Launch.HANDED_OFF))


def _settle(groups: node_groups.Groups, journal: Journal, owners: Owners, out: Launch, step: int) -> None:
    handle = out.handle
    if step == _BEFORE_COMMIT or (step == _COMMITTING and not journal.exists(out.launch_id)):
        # (a): no record exists (and no entry ever did): no GO was ever attempted, so no CLI can exist; the shim is
        # abandoned and reaped by its handle, and the owner let go once it is
        if handle is None or handle.abandon_before_go(ABANDON_SECONDS) is not None:
            owners.unpark(out)
        return
    # (b): a record is (or may be) durable. FIRST the fence (in memory, under the registry lock: an existing entry,
    # PROVISIONAL, LIVE or STOPPING, becomes HELD; with none, one is installed), THEN the writer closed (the shim
    # cleans up on EOF: never started, or its CLI stopped); the owner stays parked for the proof (`release`)
    groups.restore_held(out.token, [out.key], out.cwd)
    if handle is not None:
        handle.close_commands()


def release(groups: node_groups.Groups, journal: Journal, owners: Owners, launch: Launch,
            pause: Callable[[str, Launch], None] | None = None) -> str:
    """THE only way a supervised launch's reservation and record go. Call it with no host lock held: it reads the
    marker and syncs the journal's directory (off deadline-critical paths, outside the runner's lock).

    (Review rounds 1 and 2, R4.2 and R6) SERIALISED per launch, under the launch's own lock (never the registry's), and
    only for a launch that is HANDED_OFF (its receiver's) or ABANDONED: an ACTIVE launch (still being launched by its
    own thread) is answered KEPT and nothing is done to it. Exactly one call removes the record, lifts the entry, sets
    RELEASED and answers RELEASED; a concurrent call waits for it, and it and any later call answer ALREADY_RELEASED,
    which is never the answer while the launch's record or reservation exists. KEPT: nothing lifted (ACTIVE, no
    evidence yet, or the record could not be removed).

    Evidence, and only this: the handle's `empty()` (the authenticated release pair: the shim's own observed terminal
    status and its authentic marker) or its observed `refused()` (the shim exited 2 or 7 before any fork: nothing was
    ever started). On evidence, through `Groups.release` (its admission fence first): the durable record is removed
    FIRST (`journal.remove`, inside the proof: a removal that fails is no proof, and the caller asks again), THEN the
    in-memory reservation is lifted. `lost()`, an UNKNOWN shim status, or no evidence yet (the shim still running, a
    member of its group still alive): nothing is released, the registry entry is HELD (fenced, proof-only), and the
    caller keeps asking. A launch with no record (NOT_ADMITTED, or one whose abandoned shim was not yet reaped) is
    released once its shim, if any, is reaped by its handle. The released owner is unparked.

    `pause(step, launch)`: TEST-ONLY, at "record-removed" (the record gone, the reservation still installed)."""
    with hostlog.BOUNDARY, launch.state_lock:
        if launch.lifecycle == Launch.RELEASED:
            return ALREADY_RELEASED
        if launch.lifecycle == Launch.ACTIVE:
            return KEPT
        handle = launch.handle
        if launch.record is None:
            if handle is not None and handle.shim_poll() is None:
                return KEPT
            launch.lifecycle = Launch.RELEASED
            owners.unpark(launch)
            return RELEASED

        def proof() -> bool:
            if not (handle.empty() or handle.refused()):
                return False
            journal.remove(launch.launch_id)
            return True

        def forget() -> None:
            _pause(pause, "record-removed", launch)
        if not groups.release(launch.token, proof, forget, fence=(launch.key, launch.cwd)):
            return KEPT
        launch.lifecycle = Launch.RELEASED     # (R6: only here, by the call that retired the record and the entry)
        owners.unpark(launch)
        return RELEASED


def _command(handle: ShimChild, word: str) -> str:
    if not handle.went:
        handle.close_commands()             # (before GO a stop is "never start")
        return CLOSED
    try:
        if word == "TERM":
            handle.terminate()
        else:
            handle.kill()
    except BlockingIOError:
        return PENDING
    except Exception:
        return CLOSED
    return QUEUED


def request_term(handle: ShimChild) -> str:
    """One TERM command, never an exception. QUEUED: the write succeeded (the command is queued; nothing more is
    implied). PENDING: backpressure, the pipe is full: retry on the next pass. CLOSED: the pipe is broken or closed:
    CLASSIFY through the handle (`classify`), since a normal, proof-bearing exit can race the write."""
    return _command(handle, "TERM")


def request_kill(handle: ShimChild) -> str:
    """One KILL command, with `request_term`'s three results. KILL repetition has ONE owner, the shim: after its first
    KILL it repeats KILL every grace while its anchor holds. So a QUEUED KILL is NEVER sent again by the node; only a
    PENDING one is retried (it was not queued)."""
    return _command(handle, "KILL")


def classify(handle: ShimChild) -> str:
    """What the handle says of a launch, as a command's CLOSED asks: RUNNING (the shim has not exited), PROVED
    (`empty()`), REFUSED (`refused()`), UNKNOWN (someone else reaped the shim: no status was observed) or LOST (exited
    with no proof). Reads the marker: off deadline-critical paths, no host lock held."""
    status = handle.shim_poll()
    if status is None:
        return RUNNING
    if status == STATUS_UNKNOWN:
        return UNKNOWN
    if handle.empty():
        return PROVED
    if handle.refused():
        return REFUSED
    return LOST
