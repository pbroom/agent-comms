"""One registry, on a node, of every process group the node started and has not seen fully gone, per checkout
directory: hosted sessions' processes (session_host.SessionHost) and dispatched runs (node.Node) alike.

The invariant (review round 7, owner decision): nothing is started in a checkout directory that holds a process group
in the STOPPING state, and every stopping group stays registered (and keeps receiving its kill) until the whole group
is gone, leader reaped and no member left, however its leader ended. Live groups may share a directory (a parent
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
    for extra in (64, 1024, 16384):
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
    LIVE, STOPPING = "live", "stopping"

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.entries: dict[Hashable, dict[Hashable, list]] = {}     # dir key -> group token -> [state, cwd]

    def start(self, cwd: Any, token: Hashable, spawn: Callable[[], Any], k: Hashable | None = None) -> tuple[bool, Any]:
        """Start a process group in `cwd` unless one being stopped is there: (False, None) and nothing is spawned;
        else the group is recorded live and `spawn()` runs under the lock: (True, what it returned). A spawn that
        raises records nothing. `k`: the directory's key, computed once by the caller (who keeps it with the process:
        session_host `_spawn`, review round 1 of the enabling PR, A3); else computed here."""
        k = key(cwd) if k is None else k
        with self.lock:
            if any(state == self.STOPPING for state, _ in self.entries.get(k, {}).values()):
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
                if token in groups:
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
                    if groups[token][0] == self.STOPPING:
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

    def release(self, token: Hashable, proof: Callable[[], bool], forget: Callable[[], None]) -> bool:
        """(Ownership until verified exit, C1) THE one release rule: the only way a group's registry entry, and its
        owner's tracking record (whose durable entry is that record's projection), go. `proof()` answers whether the
        group is proved gone; it is supplied by the caller, so how absence is proved (today: the leader's exit observed
        by its owning handle and `group_absent`; later an anchor's own word) never touches this rule or its callers.
        Not proved (False, or it raised): nothing is released and False; the caller keeps its handle, the exact token
        and its durable entry, and asks again on its next pass. Proved: `forget()` drops the tracking record, then the
        registry entry goes (`_gone`); True."""
        try:
            if proof() is not True:
                return False
        except Exception:
            return False
        forget()
        self._gone(token)
        return True

    def _gone(self, token: Hashable) -> None:
        """The whole group is gone: leader reaped, no member left (only `release` says so)."""
        with self.lock:
            for k in list(self.entries):
                self._drop(k, token)

    def _drop(self, k: Hashable, token: Hashable) -> None:
        groups = self.entries.get(k)
        if groups is not None:
            groups.pop(token, None)
            if not groups:
                del self.entries[k]

    def blocked(self, cwd: Any) -> bool:
        """A group being stopped is in this directory."""
        with self.lock:
            return any(state == self.STOPPING for state, _ in self.entries.get(key(cwd), {}).values())

    def stopping_dirs(self) -> set[str]:
        """The directories (as recorded) that hold a group being stopped."""
        with self.lock:
            return {cwd for groups in self.entries.values() for state, cwd in groups.values() if state == self.STOPPING}
