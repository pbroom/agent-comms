"""One registry, on a node, of every process group the node started and has not seen fully gone, per checkout
directory: hosted sessions' processes (session_host.SessionHost) and dispatched runs (node.Node) alike.

The invariant (review round 7, owner decision): nothing is started in a checkout directory that holds a process group
in the STOPPING (or HELD) state, and every stopping group stays registered until the whole group is verified gone,
leader reaped and no member left, however its leader ended. (Ownership until verified exit, C1) A group is let go
only by `release`, with a proof; one it refuses where main would have let it go becomes HELD: fenced, and never
signalled by number again (`may_signal`). The first SIGKILL attempt on a group, sent or raised, is its last numeric
signal: there are no retries (`veto`). This holds for a live node; across a restart, a group
whose leader is gone is not restored (C3). Live groups may share a directory (a parent
session and its shared-checkout children, a session beside a dispatched run the hub allowed): the rule is about
groups being stopped.

Every start goes through `start`, which checks and records under one lock, and spawns while holding it, so a stop
(`stopping`, under the same lock) is either before the check (the start waits) or after the start (the new group is
already live, and is not started beside a stopping one). Directories are compared by identity (device and inode),
not by the string that names them.
"""
from __future__ import annotations

import ctypes
import os
import sys
import threading
from typing import Any, Callable, Hashable

PROC_PGRP_ONLY = 2      # libproc's `proc_listpids` type: the processes of one process group (sys/proc_info.h)
_LISTING_ROOM = (64, 1024, 16384)   # slots added to the kernel's estimate, tried in turn until a listing is complete
_LIBPROC: list = []
_LOAD_FAILED_SAID: list = []        # (C1, review round 4, R12/A5) a libproc that would not load is said once

# (C1, review rounds 4 and 5, R9, A2, R13 and R14) After a shutdown's initial KILL passes, how long the owner watches,
# with no signal, every group it still tracks that is not yet proved gone (its leader's exit observed by its own handle
# AND `group_absent`) before it applies the release rule. SIGKILL is delivered at once, but the kernel finishes the
# exit asynchronously: `poll()` right after `killpg(SIGKILL)` answered None 40 times of 40 in the review's probe, and a
# killed group's members are reaped by launchd a moment later. One second covers that by a wide margin, ONE shared
# deadline for all of a shutdown's groups (not one per group; the hosted sessions' own watch shares it). Nothing
# unproved: no wait. A group that cannot be proved gone (a survivor, one HELD long before) spends the whole window.
# The watch sends no signal and reorders none, but a signal this shutdown sends AFTER it comes up to this long later:
# the sessions' fallback KILL when their first KILL pass raised. The most it adds to a shutdown is 2 x this (two
# serial polling windows, on either fallback route: when the sessions' first KILL pass raised, the node's watch and
# then the host's own after its fallback KILL; when stopping the runs itself failed, `Node.stop_all`'s watch and then
# the host's own, which is handed no deadline there), plus probe and
# scheduling overhead: far inside launchd's exit timeout for the node's LaunchAgent (60 s,
# scripts/install-launchagents.py; a shutdown that needs a KILL takes the 10 s kill grace first).
POST_KILL_SECONDS = 1.0
POST_KILL_STEP = 0.05               # between two looks within that window


def _libproc():
    """macOS's libproc, loaded once (None where there is none)."""
    if not _LIBPROC:
        lib = None
        if sys.platform == "darwin":
            try:
                lib = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
                lib.proc_listpids.argtypes = [ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_int]
                lib.proc_listpids.restype = ctypes.c_int
            except (OSError, AttributeError):
                lib = None
        _LIBPROC.append(lib)
    return _LIBPROC[0]


def _listpids(lib, pgid: int, buf, size: int) -> int | None:
    """(C1, review round 4) ONE `proc_listpids(PROC_PGRP_ONLY, pgid, buf, size)` call: its byte count, or None when it
    failed. Apple's libproc wrapper returns 0, NOT -1, when the underlying `__proc_info` syscall fails, and leaves the
    cause in errno, which the syscall stub sets on every failure (and it returns 0 with errno EINVAL for a type it does
    not take). So errno is cleared before the call and read after it (ctypes' copy: the library is loaded with
    `use_errno`): a result of 0 or less with errno set is a failure, and so is any negative result; 0 with errno 0 is
    the only "nothing" answer. What is relied on: that a failed syscall always sets errno. Nothing distinguishes
    further a 0 with errno 0, and no signal is used to probe it."""
    ctypes.set_errno(0)
    n = lib.proc_listpids(PROC_PGRP_ONLY, pgid, buf, size)
    err = ctypes.get_errno()
    if n < 0 or (n <= 0 and err != 0):
        return None
    return n


def group_members(pgid: int) -> tuple[int, ...] | None:
    """(Ownership until verified exit, C1) A COMPLETE listing of the processes in process group `pgid`, zombies
    included: macOS libproc's `proc_listpids(PROC_PGRP_ONLY)`, one call that the kernel answers from its process lists
    (live and zombie) under the process-list lock, so a coherent snapshot. None when no complete listing could be
    made: no libproc, a failed call (review round 4: 0 with errno set, `_listpids`), a malformed answer (A1: a byte
    count that is not a whole number of pids or exceeds the buffer, or a slot within it that is no pid), or a buffer
    the listing filled.

    "No member" (`()`) is only a successful BUFFERED call that answered 0 bytes with errno 0; the size estimate
    (`buf` None) never answers membership. The fill check, exactly (review round 4, nit): the kernel writes a pid
    for each member it finds and stops at the end of the buffer, so a count equal to the buffer's size may be a cut
    listing (measured: one slot for a group of two answers 4 bytes) and is no proof. With the buffer sized from the
    kernel's own estimate (every process on the machine, plus 20, at the time of the estimate) plus `_LISTING_ROOM`,
    it can be filled only by a group about as large as the whole process table, or one that grew past it between
    the two calls; the larger retries cover that, and a group still filling the largest is no proof."""
    lib = _libproc()
    if lib is None or isinstance(pgid, bool) or not isinstance(pgid, int) or pgid <= 1:
        return None
    size_of = ctypes.sizeof(ctypes.c_int)
    for extra in _LISTING_ROOM:
        need = _listpids(lib, pgid, None, 0)        # (an estimate, in bytes: every process's room)
        if need is None:
            return None
        n = need // size_of + extra
        buf = (ctypes.c_int * n)()
        size = ctypes.sizeof(buf)
        got = _listpids(lib, pgid, buf, size)
        if got is None or got % size_of or got > size:
            return None         # (A1: failed, or not a whole number of pids within the buffer: malformed)
        if got < size:          # (room was left: the kernel listed every member it found)
            members = tuple(buf[:got // size_of])
            if any(p <= 0 for p in members):
                return None     # (A1: a slot inside the count that holds no pid: malformed, never "no member")
            return members
    return None


def group_absent(pgid: int) -> bool:
    """(C1) The membership half of the proof that a group is gone: a complete, successful listing of the group shows
    no member at all (`group_members`). A member of any kind (a zombie included), an error, or a listing that may be
    truncated is NOT proof: False, and whoever asks retains. Not `killpg(pgid, 0)`: its ESRCH also answers for a
    group whose only members are zombies, and its EPERM for a group whose members this user may not signal (and, on
    macOS, briefly for one that is exiting), so its errno is a permission answer, not a membership observation.

    (Review round 4, R12 and A5) The verified-exit proof is macOS-only. OFF macOS (`sys.platform` is not darwin)
    there is no complete listing here, and the membership half is main's own liveness answer, `not group_alive(pgid)`
    (session_host: `killpg(pgid, 0)`, the probe main's reap asked at this release point): an explicit compatibility
    limit, with main's weakness (a zombie-only group, or one this user may not signal, counts as gone). ON macOS a
    libproc that will not load is NOT proof (False: the group is retained), said once in the log; and a listing that
    failed is never downgraded to that weaker probe."""
    if sys.platform != "darwin":
        from .session_host import group_alive       # (lazily: session_host imports this module)
        return not group_alive(pgid)
    if _libproc() is None:
        if not _LOAD_FAILED_SAID:
            _LOAD_FAILED_SAID.append(True)
            import logging
            logging.getLogger(__name__).warning(
                "macOS libproc could not be loaded: no process group can be proved gone, so every group this node "
                "stops is kept and its checkout stays held")
        return False
    members = group_members(pgid)
    return members is not None and not members


def key(cwd: Any) -> Hashable:
    """A directory's identity: (device, inode); its real path if it cannot be stat'ed."""
    try:
        st = os.stat(cwd)
        return (st.st_dev, st.st_ino)
    except (OSError, TypeError, ValueError):
        return ("path", os.path.realpath(cwd) if isinstance(cwd, str) else repr(cwd))


class Groups:
    # (Ownership until verified exit, C1, review round 1) HELD: a group the release rule refused to let go where
    # main would have (`release`). It fences its checkout as STOPPING does, but it is PROOF-ONLY: no numeric signal
    # is ever sent to it again (`may_signal`), and it leaves only through `release` with a True proof. It never goes
    # back to LIVE or STOPPING.
    LIVE, STOPPING, HELD = "live", "stopping", "held"
    # (Review round 4, R10 and A3) FENCED: the release rule's TEMPORARY admission fence, for the length of the
    # owner's final reads and the proof only (`release`). It refuses admission as HELD does, and for everything else
    # it is the LIVE entry it was: it is not proof-only (it is not in `held`, so `may_signal` is unchanged by it), a
    # stop's bookkeeping moves it to STOPPING as it would a LIVE one, and it never clears a veto. The proof turns it
    # into HELD when refused, or removes it with the entry when True.
    FENCED = "fenced"
    _FENCE = ("stopping", "held", "fenced")     # the states that admit nothing beside them

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.entries: dict[Hashable, dict[Hashable, list]] = {}     # dir key -> group token -> [state, cwd]
        # (C1, review round 1) the HELD tokens, the same set as the HELD entries: written under the lock, at the
        # transition and at the release, and read WITHOUT it by `may_signal`, so no signal site and no pass of the loop
        # waits on the registry (a spawn holds it across process creation: round 8, ruling C1)
        self.held: set = set()
        # (Review round 4, R11 and A4) token -> [when its release was first refused (the owner's monotonic clock),
        # whether its retention line was written]: the line waits; HELD and the fence do not (`unproved_line`)
        self.refused: dict[Hashable, list] = {}

    def start(self, cwd: Any, token: Hashable, spawn: Callable[[], Any], k: Hashable | None = None) -> tuple[bool, Any]:
        """Start a process group in `cwd` unless one being stopped is there: (False, None) and nothing is spawned;
        else the group is recorded live and `spawn()` runs under the lock: (True, what it returned). A spawn that
        raises records nothing. `k`: the directory's key, computed once by the caller (who keeps it with the process:
        session_host `_spawn`, review round 1 of the enabling PR, A3); else computed here."""
        k = key(cwd) if k is None else k
        with self.lock:
            if any(state in self._FENCE for state, _ in self.entries.get(k, {}).values()):
                return False, None
            self.entries.setdefault(k, {})[token] = [self.LIVE, cwd]
            try:
                return True, spawn()
            except BaseException:
                self._drop(k, token)
                raise

    def stopping(self, token: Hashable) -> None:
        """The group is being stopped (signalled, or its leader went on its own and members are left)."""
        with self.lock:
            for groups in self.entries.values():
                if token in groups and groups[token][0] != self.HELD:      # (C1: HELD never goes back)
                    groups[token][0] = self.STOPPING

    RESERVED, ALREADY, BUSY, MISSING = "reserved", "already", "busy", "missing"

    def try_stopping(self, token: Hashable, barrier: tuple[Hashable, Any] | None = None) -> str:
        """`stopping`, only if the registry is free at once (review rounds 8 and 9, rulings C1, D3 and E1: a steering-
        only stop never waits behind a spawn that holds the registry across process creation). What happened:
        RESERVED (this call marked the group STOPPING), ALREADY (it was STOPPING before: a barrier this call did not
        make), BUSY (the registry is held: nothing was recorded), MISSING (no entry for the token: a registry miss,
        not a verified exit; as main's `stopping`, nothing is recorded). (Enabling PR, ruling S3) `barrier`: (the
        checkout's key, its directory), given by a caller whose probe found the group alive: on a miss the normal
        STOPPING entry is inserted for `token` under that key (RESERVED: this call made it), so nothing is admitted on
        that checkout until the verified reap removes it (`gone`). Nothing is ever undone here: once reserved, the
        caller arms the stop's escalation before anything else (session_host `_reserved_stop`)."""
        if not self.lock.acquire(blocking=False):
            return self.BUSY
        try:
            for groups in self.entries.values():
                if token in groups:
                    if groups[token][0] in (self.STOPPING, self.HELD):     # (C1: a HELD one too, unchanged)
                        return self.ALREADY     # (R10/A3: a FENCED one is the LIVE one it was: reserved below)
                    groups[token][0] = self.STOPPING
                    return self.RESERVED
            if barrier is not None:
                k, cwd = barrier
                self.entries.setdefault(k, {})[token] = [self.STOPPING, cwd]
                return self.RESERVED
            return self.MISSING
        finally:
            self.lock.release()

    def veto(self, token: Hashable) -> None:
        """(C1, review round 2, R5 and A2) THE one place that makes a token proof-only after a KILL: called by every
        site that sends a SIGKILL to a tracked group, at the attempt, before the signal (so a KILL that raises is
        covered too), under the owner's serialization that also asks `may_signal` (a session's: the node's runner lock;
        a dispatched run's: the node's one loop thread). It writes only the lock-free HELD index, so the due-KILL path
        never waits on the registry; the group's entry is STOPPING already (that fences admission), and becomes HELD
        at the release rule, where the registry is taken anyway."""
        self.held.add(token)

    def _admit_fence(self, token: Hashable, fence: tuple[Hashable, Any] | None) -> None:
        """(The registry's lock held; review round 4, R10 and A3) The release rule's temporary admission fence: a
        LIVE entry becomes FENCED; a STOPPING or HELD one is kept as it is; with no entry, a FENCED one under `fence`.
        The HELD index is not touched: an existing veto stays, and no new one is made."""
        found = False
        for groups in self.entries.values():
            if token in groups:
                found = True
                if groups[token][0] == self.LIVE:
                    groups[token][0] = self.FENCED
        if not found and fence is not None:
            k, cwd = fence
            self.entries.setdefault(k, {})[token] = [self.FENCED, cwd]

    def _fence(self, token: Hashable, fence: tuple[Hashable, Any] | None) -> None:
        """(The registry's lock held.) The token's entry made HELD (with no entry, one under `fence`: A3), and the
        HELD index with it."""
        found = False
        for groups in self.entries.values():
            if token in groups:
                groups[token][0], found = self.HELD, True
        if not found and fence is not None:
            k, cwd = fence
            self.entries.setdefault(k, {})[token] = [self.HELD, cwd]
            found = True
        if found:
            self.held.add(token)

    def release(self, token: Hashable, proof: Callable[[], bool], forget: Callable[[], None],
                fence: tuple[Hashable, Any] | None = None, prepare: Callable[[], None] | None = None) -> bool:
        """(Ownership until verified exit, C1; review rounds 2 to 4, R6, A3, S4 and R10) THE one release rule. FIRST,
        under the registry's lock, the exact token's admission is fenced (FENCED, the temporary admission fence, or a
        FENCED entry under `fence` if it has none; a STOPPING or HELD entry stays as it is): admission is fenced when
        the release path installs the fence under the registry lock, before the final reads and the membership proof.
        The temporary fence revokes ADMISSION only (round 4, R10 and A3): the final reads (`prepare()`) run exactly
        as on main, `_stop`'s bookkeeping included, and only a veto (or main's own rule for a reaped leader) keeps a
        numeric signal from them. Then the proof, outside the lock. A True proof removes the entry and the fence in
        one step (`_gone`); anything else makes the token HELD (also when `prepare()` raises). Work admitted while the token was LIVE can coexist with a survivor
        discovered afterwards: a LIVE entry never fenced admission on main, and C1 does not change that (excluding it
        would need a LIVE entry to fence, which would stop runs sharing an idle session's checkout). The only way a group's registry entry, and its
        owner's tracking record (whose durable entry is that record's projection), go. `proof()` answers whether the
        group is proved gone; it is supplied by the caller, so how absence is proved (today: the leader's exit observed
        by its owning handle and `group_absent`; later an anchor's own word) never touches this rule or its callers.
        Not proved (False, None, or it raised): nothing is released and False; the caller keeps its handle, the exact
        token and its durable entry, and asks again on its next pass; and (review round 1, R1, R2, A2, A3) that exact
        token is made HELD before this returns: its checkout admits nothing, and it is never signalled by number
        again. Every caller is at main's release point (main would have let the group go here), so HELD marks exactly
        the state C1 adds. With no entry for the token, `fence` (its checkout's known key and directory) gets a HELD
        one, so admission sees the barrier anyway. Proved: `forget()` drops the tracking record, then the registry
        entry goes (`_gone`); True. Scope: this is the removal path of a live node's started groups. A spawn that
        fails removes its provisional registration in `start`, and a start never made or a claim lost has no group to
        release.

        (Review round 5, R15 and A3) An exception at ANY point, an interrupt included (the node's SIGTERM handler
        raises KeyboardInterrupt on the main thread, where a run's release and the shutdown's run), ends in one of
        three states, never FENCED, because the installation itself is inside the protected region:
        - before the fence is installed: the token is exactly as it was (no HELD for a fence never set);
        - after it is installed, during the final reads or the proof: HELD (admission fenced and numeric signals
          vetoed), as for a refused proof;
        - once the proof held: the registry's removal completes (`_gone`), even if `forget()` or an interrupt cut in
          after the owner's record went, so no proved-gone group's entry is left with no record to retry it.
        A SECOND interrupt inside that cleanup fails closed: the entry may stay (its checkout admits nothing) for the
        rest of this node process. The only interrupt source on a releasing thread is the node's own SIGTERM or
        Ctrl-C handler (Python delivers signals to the main thread only; the hosted sessions' loop thread gets none),
        so the process is then on its way out and the entry goes with it."""
        proved = begun = False
        try:
            with self.lock:
                self._admit_fence(token, fence)
            begun = True
            if prepare is not None:
                prepare()
            try:
                proved = proof() is True
            except Exception:
                proved = False
            if proved:
                forget()
        finally:
            if proved:
                self._gone(token)       # (also when `forget()` raised, or an interrupt came after it: see above)
            else:
                with self.lock:
                    if begun:
                        self._fence(token, fence)   # (refused, or the final reads or the proof raised: HELD)
                    else:
                        self._held_if_fenced(token)     # (cut at the installation: only a FENCED entry changes)
        return proved

    def _held_if_fenced(self, token: Hashable) -> None:
        """(The registry's lock held; review round 5, R15) An installation that an exception or an interrupt cut
        short: a FENCED entry for the token (only `release` makes one) becomes HELD, with the HELD index; any other
        state is left exactly as it was."""
        found = False
        for groups in self.entries.values():
            if token in groups and groups[token][0] == self.FENCED:
                groups[token][0], found = self.HELD, True
        if found:
            self.held.add(token)

    def _gone(self, token: Hashable) -> None:
        """The whole group is gone: leader reaped, no member left (only `release` says so)."""
        with self.lock:
            for k in list(self.entries):
                self._drop(k, token)
            self.held.discard(token)
            self.refused.pop(token, None)

    def unproved_line(self, token: Hashable, now: float, after: float, final: bool = False) -> bool:
        """(Review round 4, R11 and A4) Asked by an owner after `release` refused `token`: whether to write its
        retention line NOW. True exactly once per token: when it has stayed unproved for `after` seconds since its
        first refusal (`now` and that first time are the owner's monotonic clock), or at `final` (a shutdown that
        keeps it past the post-KILL wait). A group that is merely still exiting (its leader reaped, zombie members for
        an instant) is released by a later pass within that time and says nothing, as on main. HELD and the fence are
        not delayed by this: only the line is."""
        with self.lock:
            rec = self.refused.setdefault(token, [now, False])
            if rec[1] or not (final or now - rec[0] >= after):
                return False
            rec[1] = True
            return True

    def _drop(self, k: Hashable, token: Hashable) -> None:
        groups = self.entries.get(k)
        if groups is not None:
            groups.pop(token, None)
            if not groups:
                del self.entries[k]

    def state(self, token: Hashable) -> str | None:
        """The token's registry state (LIVE, STOPPING, HELD, FENCED), or None."""
        with self.lock:
            for groups in self.entries.values():
                if token in groups:
                    return groups[token][0]
        return None

    def may_signal(self, token: Hashable) -> bool:
        """(C1, review round 1, R2) THE one predicate of whether a group may be signalled by NUMBER: not once it is
        HELD (a group not proved gone where main would have let it go: its pgid may already be a stranger's). Every
        signal site a retained record can reach asks it, under the owner's serialization that also makes the HELD
        transition (a session's: the node's runner lock; a dispatched run's: the node's one loop thread), so a HELD
        transition can never come between the question and the signal. Read without the registry's lock (`held`)."""
        return token not in self.held

    def blocked(self, cwd: Any) -> bool:
        """A group being stopped, HELD, or FENCED (a release under way) is in this directory."""
        with self.lock:
            return any(state in self._FENCE for state, _ in self.entries.get(key(cwd), {}).values())

    def stopping_dirs(self) -> set[str]:
        """The directories (as recorded) that hold a group being stopped, HELD, or FENCED."""
        with self.lock:
            return {cwd for groups in self.entries.values() for state, cwd in groups.values() if state in self._FENCE}
