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


def group_members(pgid: int) -> tuple[int, ...] | None:
    """(Ownership until verified exit, C1) A COMPLETE listing of the processes in process group `pgid`, zombies
    included: macOS libproc's `proc_listpids(PROC_PGRP_ONLY)`, one call that the kernel answers from its process lists
    under their lock, so a coherent snapshot. None when no complete listing could be made: no libproc, an error, or a
    buffer the listing filled (it may have been truncated: a larger one is tried, a few times)."""
    lib = _libproc()
    if lib is None or isinstance(pgid, bool) or not isinstance(pgid, int) or pgid <= 1:
        return None
    for extra in _LISTING_ROOM:
        need = lib.proc_listpids(PROC_PGRP_ONLY, pgid, None, 0)     # (an estimate, in bytes: every process's room)
        if need < 0:
            return None
        n = need // ctypes.sizeof(ctypes.c_int) + extra
        buf = (ctypes.c_int * n)()
        got = lib.proc_listpids(PROC_PGRP_ONLY, pgid, buf, ctypes.sizeof(buf))
        if got < 0:
            return None
        if got < ctypes.sizeof(buf):        # (room was left: the listing is complete)
            return tuple(p for p in buf[:got // ctypes.sizeof(ctypes.c_int)] if p)
    return None


def group_absent(pgid: int) -> bool:
    """(C1) The membership half of the proof that a group is gone: a complete, successful listing of the group shows
    no member at all (`group_members`). A member of any kind (a zombie included), an error, or a listing that may be
    truncated is NOT proof: False, and whoever asks retains. Not `killpg(pgid, 0)`: its ESRCH also answers for a
    group whose only members are zombies, and its EPERM for a group whose members this user may not signal (and, on
    macOS, briefly for one that is exiting), so its errno is a permission answer, not a membership observation."""
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
    _FENCE = ("stopping", "held")       # the states that admit nothing beside them

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.entries: dict[Hashable, dict[Hashable, list]] = {}     # dir key -> group token -> [state, cwd]
        # (C1, review round 1) the HELD tokens, the same set as the HELD entries: written under the lock, at the
        # transition and at the release, and read WITHOUT it by `may_signal`, so no signal site and no pass of the loop
        # waits on the registry (a spawn holds it across process creation: round 8, ruling C1)
        self.held: set = set()

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
                    if groups[token][0] in self._FENCE:     # (C1: a HELD one too, unchanged)
                        return self.ALREADY
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
        """(Ownership until verified exit, C1; review rounds 2 and 3, R6, A3 and S4) THE one release rule. FIRST,
        under the registry's lock, the exact token is fenced (HELD, or a HELD entry under `fence` if it has none):
        admission is fenced when the release path installs the fence under the registry lock, before the final reads
        and the membership proof. Then `prepare()` (the caller's final reads), then the proof, outside the lock. A
        True proof removes the entry and the fence in one step (`_gone`); anything else leaves the token HELD (also
        when `prepare()` raises: the fence stays). Work admitted while the token was LIVE can coexist with a survivor
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
        release."""
        with self.lock:
            self._fence(token, fence)
        if prepare is not None:
            prepare()
        try:
            proved = proof() is True
        except Exception:
            proved = False
        if not proved:
            return False
        forget()
        self._gone(token)
        return True

    def _gone(self, token: Hashable) -> None:
        """The whole group is gone: leader reaped, no member left (only `release` says so)."""
        with self.lock:
            for k in list(self.entries):
                self._drop(k, token)
            self.held.discard(token)

    def _drop(self, k: Hashable, token: Hashable) -> None:
        groups = self.entries.get(k)
        if groups is not None:
            groups.pop(token, None)
            if not groups:
                del self.entries[k]

    def state(self, token: Hashable) -> str | None:
        """The token's registry state (LIVE, STOPPING, HELD), or None."""
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
        """A group being stopped, or HELD, is in this directory."""
        with self.lock:
            return any(state in self._FENCE for state, _ in self.entries.get(key(cwd), {}).values())

    def stopping_dirs(self) -> set[str]:
        """The directories (as recorded) that hold a group being stopped, or HELD."""
        with self.lock:
            return {cwd for groups in self.entries.values() for state, cwd in groups.values() if state in self._FENCE}
