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

import os
import threading
from typing import Any, Callable, Hashable


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

    def gone(self, token: Hashable) -> None:
        """The whole group is gone: leader reaped, no member left."""
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
