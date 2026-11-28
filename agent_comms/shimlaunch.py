"""(Ownership C3-2b-1) The lifecycle of ONE supervised launch: start it (`launch`), release it on evidence only
(`release`), and send it commands (`request_term`, `request_kill`, then `classify` when a command finds the pipe
closed). DESIGN_NOTES "Ownership C3-2b-1: the journal, restore-and-reserve, and the launch helpers (nothing can start
a shim yet)" has the crash windows and the results as tables.

UNREACHABLE in this PR: no config path, spawn site, session host, runner, provider or node path imports or calls this
module (tests/test_shim_static.py pins it). The next PR makes a supervised spawn reachable, behind a switch, with the
complete stop, shutdown, release and recovery-fence paths.

Locks and I/O (a rule the wiring is bound by). Every helper here is called WITHOUT any host lock held. `launch` commits
the journal (two full syncs) and `release` reads the marker (`ShimChild.empty()`/`refused()`) and syncs the journal's
directory: proof reads and journal syncs stay OFF deadline-critical paths and OUTSIDE the node's runner lock. The only
lock taken is the registry's, by `Groups.start` (the admission check, the reservation and GO, as one step: GO is the
"spawn" a direct start makes under that lock today, bounded by `go_timeout`) and by `Groups.release`. No logging:
results are returned (and a failure before the commit is raised).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Hashable

from . import node_groups, shimjournal
from .dispatch import process_start
from .shimchild import STATUS_UNKNOWN, ShimChild
from .shimjournal import Journal, Record

# `launch` outcomes
STARTED = "started"                         # GO written whole; the reservation LIVE; the record durable
NOT_ADMITTED = "not_admitted"               # the checkout was fenced (or admission closed): nothing was done at all
REFUSED_AFTER_COMMIT = "refused_after_commit"   # fenced at the atomic check after the commit: no GO; record + HELD
GO_FAILED = "go_failed"                     # GO failed or is ambiguous: record + reservation (STOPPING) retained
# command results
QUEUED, PENDING, CLOSED = "queued", "pending", "closed"
# `classify`
RUNNING, PROVED, REFUSED, LOST, UNKNOWN = "running", "proved", "refused", "lost", "unknown"

ABANDON_SECONDS = 5.0                       # before the commit: the bound on reaping a shim given up (twice, at most)


@dataclass
class Launch:
    launch_id: str
    token: tuple                            # (node_groups.SUPERVISED, launch_id)
    key: Hashable                           # the checkout's identity, taken once (node_groups.key)
    cwd: str
    handle: ShimChild | None = None
    record: Record | None = None
    outcome: str | None = None
    error: BaseException | None = None      # GO's failure (GO_FAILED)


def launch(*, groups: node_groups.Groups, journal: Journal, marker_dir: str, cwd: str, argv: list[str],
           env: dict[str, str], kill_grace: float, log: str, stderr_log: str, piped: bool = True,
           python: str | None = None, go_timeout: float = 5.0, start_of: Callable[[int], Any] = process_start,
           test_gate: str | None = None, pause: Callable[[str, Launch], None] | None = None) -> Launch:
    """Start one supervised launch, TRANSACTIONALLY. Call it with no host lock held.

    1. A first look (`groups.blocked`): a fenced checkout (or closed admission) gets NOT_ADMITTED, nothing done.
    2. `ShimChild.start`: the shim, its cwd "/" (outside every checkout), a fresh marker in the node's private marker
       directory, the secret sent; it waits for GO and can start nothing without it.
    3. The record (`Record`), then `journal.commit`: durable before anything is reserved or started.
       NO registry entry exists for the checkout before this commit. If anything fails BEFORE the commit (the record,
       the commit itself, an interrupt), the command writer is closed and the shim reaped by its handle
       (`ShimChild.abandon_before_go`: no GO byte was ever attempted, so no CLI exists); the commit removed its own
       file; nothing is left, and the failure is raised.
    4. `groups.start(cwd, token, GO)`: under the registry lock, the admission check, the reservation (LIVE, as a direct
       start's entry) and GO, as one step. So the reservation is installed AFTER the commit and BEFORE GO, atomically
       with the check: a stop of any group in that checkout is either before it (refused: no GO is ever written) or
       after it (this launch is already reserved). No CLI of a launch ever runs without both its durable record and
       its registry entry: before GO the shim cannot start one, and EOF before GO is "never start".
    5. AFTER the commit nothing is ever dropped: a GO that failed or is ambiguous (GO_FAILED) keeps the record and the
       reservation, marked STOPPING (the checkout admits nothing), with the command writer closed, so the shim cleans
       up on EOF (never started, or its CLI stopped). Refused at the atomic check (REFUSED_AFTER_COMMIT): no GO, the
       writer closed (the shim publishes never-started), the record kept and a HELD reservation installed for it. An
       interrupt in the registry's own code after GO may have been written: a HELD reservation, the writer closed,
       raised. Each of these leaves only through `release`, on evidence.

    `pause(step, launch)`: TEST-ONLY (the node never passes it), at "started", "committed" and "go"."""
    k = node_groups.key(cwd)
    lid = shimjournal.new_launch_id()
    out = Launch(launch_id=lid, token=shimjournal.token_of(lid), key=k, cwd=cwd)
    if groups.blocked(cwd):
        out.outcome = NOT_ADMITTED
        return out
    out.handle = handle = ShimChild.start(marker_dir=marker_dir, log=log, stderr_log=stderr_log, cwd="/",
                                          python=python, test_gate=test_gate, piped=piped)
    # ---- before the commit: any failure leaves nothing
    try:
        if pause is not None:
            pause("started", out)
        try:
            start = start_of(handle.pid)
        except Exception:
            start = None
        out.record = Record(launch_id=lid, checkout_key=k, cwd=cwd, boot=handle.boot, marker=handle.marker,
                            shim_pid=handle.pid, shim_start=start if isinstance(start, str) else None)
        journal.commit(out.record)
    except BaseException:
        handle.abandon_before_go(ABANDON_SECONDS)
        raise
    # ---- after the commit: the record is retained whatever happens
    go_attempted: list[bool] = []

    def go() -> BaseException | None:
        """GO, under the registry lock (Groups.start's spawn): never raises, so the reservation is never dropped."""
        go_attempted.append(True)
        try:
            if pause is not None:
                pause("go", out)
            handle.go(argv, cwd=cwd, env=env, kill_grace=kill_grace, timeout=go_timeout)
        except BaseException as e:
            return e
        return None
    try:
        if pause is not None:
            pause("committed", out)
        admitted, failure = groups.start(cwd, out.token, go, k=k)
    except BaseException:
        handle.close_commands()
        if go_attempted:        # (cut inside the registry's own code: GO may have been written; held, fenced)
            groups.restore_held(out.token, [k], cwd)
        raise
    if not admitted:
        handle.close_commands()
        groups.restore_held(out.token, [k], cwd)
        out.outcome = REFUSED_AFTER_COMMIT
        return out
    if failure is not None:
        handle.close_commands()
        groups.stopping(out.token)
        out.outcome, out.error = GO_FAILED, failure
        if not isinstance(failure, Exception):
            raise failure       # (an interrupt: retained first, then raised)
        return out
    out.outcome = STARTED
    return out


def release(groups: node_groups.Groups, journal: Journal, launch: Launch,
            pause: Callable[[str, Launch], None] | None = None) -> bool:
    """THE only way a supervised launch's reservation and record go. Call it with no host lock held: it reads the
    marker and syncs the journal's directory (off deadline-critical paths, outside the runner's lock).

    Evidence, and only this: the handle's `empty()` (the authenticated release pair: the shim's own observed terminal
    status and its authentic marker) or its observed `refused()` (the shim exited 2 or 7 before any fork: nothing was
    ever started). On evidence, through `Groups.release` (its admission fence first): the durable record is removed
    FIRST (`journal.remove`, inside the proof: a removal that fails is no proof, and the caller asks again), THEN the
    in-memory reservation is lifted. `lost()`, an UNKNOWN shim status, or no evidence yet (the shim still running, a
    member of its group still alive): nothing is released, the registry entry is HELD (fenced, proof-only), and the
    caller keeps asking. True when released. A launch with nothing recorded (NOT_ADMITTED) has nothing to release.

    `pause(step, launch)`: TEST-ONLY, at "record-removed" (the record gone, the reservation still installed)."""
    handle = launch.handle
    if handle is None or launch.record is None:
        return True

    def proof() -> bool:
        if not (handle.empty() or handle.refused()):
            return False
        journal.remove(launch.launch_id)
        return True

    def forget() -> None:
        if pause is not None:
            pause("record-removed", launch)
    return groups.release(launch.token, proof, forget, fence=(launch.key, launch.cwd))


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
