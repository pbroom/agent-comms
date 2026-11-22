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

    def start(self, cwd: Any, token: Hashable, spawn: Callable[[], Any]) -> tuple[bool, Any]:
        """Start a process group in `cwd` unless one being stopped is there: (False, None) and nothing is spawned;
        else the group is recorded live and `spawn()` runs under the lock: (True, what it returned). A spawn that
        raises records nothing."""
        k = key(cwd)
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
