"""(Ownership C3-2b-1) The supervised-launch journal, and the node's restore of it at start. DESIGN_NOTES "Ownership
C3-2b-1: the journal, restore-and-reserve, and the launch helpers (nothing can start a shim yet)" has the rules as
tables.

A durable, versioned record per supervised launch (agent_comms/shimlaunch.py writes it, between the shim's start and
its GO), so that a node that restarts knows which checkouts a launch of an earlier node process may still hold. ONE
FILE PER LAUNCH, `<launch id>.json`, in a private directory (0700, this user's, never a symlink) under the node's data
directory, beside the session state and NOT inside it: ordinary session saves rewrite the session state, and a held
incarnation must outlive its session.

The record: the record version, the launch id, the registry token in a serialisable form, the checkout's key (its
identity, `node_groups.key`) and its directory, the boot identity (`kern.bootsessionuuid`), the marker path, and the
shim's pid and start time (DIAGNOSTIC ONLY: they confer no signalling or release authority, and nothing probes or
signals them). NOT the launch secret: it stays in the node's memory (proof after a restart is a later PR's).

- `commit(record)`: creates the file exclusively (never replacing one), writes it whole, syncs the file, then the
  directory (and the directory's parent until one parent sync has returned in this process: review round 1, R2). On
  a failure it removes NOTHING and raises (review round 1, R3): a failed or partial record is left in place.
- `remove(launch_id)`: unlinks, then syncs the directory (a record already gone is no error). After proof only: the
  only unlink in the module.
- `load()`: every record, or `JournalUnreadable` if the directory or ANY entry in it cannot be read, parsed or
  validated (a bad file is never skipped; a partial record a failed commit left is one: the reason names its path).
  An absent directory is an empty journal, and nothing is created.

`restore(groups, data_dir)` is the node's start-up step (agent_comms/node.py `Node.__init__`, before any thread, hub
contact or admission): every record becomes a HELD reservation in the shared registry (`Groups.restore_held`); an
unreadable journal closes admission for the whole node (`Groups.close_admission`). Nothing is released from the
journal by a restarted node (release by evidence after a restart is a later PR's).

No logging here: errors are returned or raised (the node logs the one WARNING).
"""
from __future__ import annotations

import json
import os
import re
import secrets
import stat
from dataclasses import dataclass
from typing import Any, Hashable

from . import node_groups
from .node_groups import SUPERVISED

RECORD_VERSION = 1
JOURNAL_DIR = "shim-journal"        # under the node's data directory (its log directory), beside node-sessions.json
RECORD_MAX = 64 * 1024              # bytes a record may have (they are about 400)
# (C3-2b-2, Part 0.5) macOS Finder writes this file into any directory a person merely looked at: `load` ignores
# EXACTLY this name (never opens it, never removes it), so it cannot close admission for the whole node. Every other
# entry that is not a record still makes the journal unreadable.
FINDER_FILE = ".DS_Store"
_ID = re.compile(r"[0-9a-f]{32}")
_NAME = re.compile(r"([0-9a-f]{32})\.json")
_KEYS = frozenset({"v", "launch_id", "token", "checkout_key", "cwd", "boot", "marker", "shim_pid", "shim_start"})


class JournalUnreadable(Exception):
    """The journal's directory, or one of its entries, cannot be read, parsed or validated: `reason` says which."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def new_launch_id() -> str:
    """A fresh launch id: 32 lowercase hex digits, drawn apart from the launch secret and the marker's name."""
    return secrets.token_hex(16)


def token_of(launch_id: str) -> tuple:
    """A supervised launch's registry token: its own kind, never a session's or a run's."""
    return (SUPERVISED, launch_id)


def _uuid_shaped(value: Any) -> bool:
    if type(value) is not str or len(value) != 36:
        return False
    parts = value.split("-")
    return [len(p) for p in parts] == [8, 4, 4, 4, 12] and all(c in "0123456789abcdefABCDEF" for p in parts for c in p)


def _key_of(value: Any) -> tuple | None:
    """A checkout key from its JSON form: [device, inode] (two non-negative ints) or ["path", an absolute path]."""
    if type(value) is not list or len(value) != 2:
        return None
    a, b = value
    if type(a) is int and type(b) is int and a >= 0 and b >= 0:
        return (a, b)
    if a == "path" and type(b) is str and os.path.isabs(b) and "\0" not in b:
        return ("path", b)
    return None


@dataclass(frozen=True)
class Record:
    launch_id: str
    checkout_key: tuple             # node_groups.key(cwd) at the launch: (device, inode), or ("path", real path)
    cwd: str
    boot: str
    marker: str
    shim_pid: int | None            # diagnostic only
    shim_start: str | None          # diagnostic only

    @property
    def token(self) -> tuple:
        return token_of(self.launch_id)

    def to_json(self) -> dict:
        return {"v": RECORD_VERSION, "launch_id": self.launch_id, "token": list(self.token),
                "checkout_key": list(self.checkout_key), "cwd": self.cwd, "boot": self.boot, "marker": self.marker,
                "shim_pid": self.shim_pid, "shim_start": self.shim_start}

    def check(self) -> None:
        """Raise ValueError unless every field is what `load` accepts (so a commit never writes what load refuses)."""
        _from_json(self.to_json(), f"{self.launch_id}.json")


def _from_json(rec: Any, name: str) -> Record:
    def bad(why: str):
        return ValueError(f"{name}: {why}")
    m = _NAME.fullmatch(name)
    if m is None:
        raise bad("not a record's name")
    if type(rec) is not dict or set(rec) != _KEYS:
        raise bad("not a record (its fields)")
    if type(rec["v"]) is not int or rec["v"] != RECORD_VERSION:
        raise bad("an unknown record version")
    launch_id = rec["launch_id"]
    if type(launch_id) is not str or not _ID.fullmatch(launch_id) or launch_id != m.group(1):
        raise bad("its launch id is not its name's")
    if rec["token"] != [SUPERVISED, launch_id]:
        raise bad("its token is not this launch's supervised token")
    k = _key_of(rec["checkout_key"])
    if k is None:
        raise bad("its checkout key is malformed")
    for field in ("cwd", "marker"):
        v = rec[field]
        if type(v) is not str or not os.path.isabs(v) or "\0" in v:
            raise bad(f"its {field} is not an absolute path")
    if not _uuid_shaped(rec["boot"]):
        raise bad("its boot identity is not a UUID")
    pid, start = rec["shim_pid"], rec["shim_start"]
    if not (pid is None or (type(pid) is int and pid > 1)):
        raise bad("its shim pid is malformed")
    if not (start is None or type(start) is str):
        raise bad("its shim start time is malformed")
    return Record(launch_id=launch_id, checkout_key=k, cwd=rec["cwd"], boot=rec["boot"], marker=rec["marker"],
                  shim_pid=pid, shim_start=start)


def _fullsync(fd: int) -> None:
    """fsync, with macOS's F_FULLFSYNC (through the drive's cache) where it is taken."""
    try:
        import fcntl
        fcntl.fcntl(fd, fcntl.F_FULLFSYNC)
        return
    except (ImportError, AttributeError, OSError):
        pass
    os.fsync(fd)


def _sync_dir(path: str) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        _fullsync(fd)
    finally:
        os.close(fd)


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        n = os.write(fd, view)
        if n <= 0:
            raise OSError(5, "a write wrote nothing")
        view = view[n:]


def _check_private(path: str) -> None:
    """Raise OSError unless `path` is a real directory (not a symlink), owned by this user, with no group or other
    permission bit. Nothing is changed."""
    st = os.lstat(path)
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        raise NotADirectoryError(20, "the journal is not a real directory", path)
    if st.st_uid != os.geteuid():
        raise PermissionError(1, "the journal is owned by another user", path)
    if stat.S_IMODE(st.st_mode) & 0o077:
        raise PermissionError(1, "the journal is open to its group or to others", path)


class Journal:
    def __init__(self, directory: Any):
        self.directory = os.path.abspath(os.fspath(directory))
        # (Review round 1, R2) Whether THIS process has synced the journal directory's parent, so that the directory's
        # own entry is durable: set only after a parent sync RETURNED (`make`). Until then every commit syncs it.
        self._parent_synced = False

    def _path(self, launch_id: str) -> str:
        if type(launch_id) is not str or not _ID.fullmatch(launch_id):
            raise ValueError("not a launch id")
        return os.path.join(self.directory, f"{launch_id}.json")

    def make(self) -> None:
        """The journal's directory: made 0700 if absent (its parent too, 0700, as the node makes its data directory)
        and checked private (an unsafe one is refused, never repaired). (Review round 1, R2) The parent is synced
        (the directory's own entry made durable) on every call until a parent sync has returned in this process,
        whether or not this call made the directory: a first `mkdir` whose parent sync failed (or was interrupted)
        is completed by the next commit, never skipped because the directory now exists. A sync that fails raises:
        the commit that called this fails before it creates anything."""
        parent = os.path.dirname(self.directory)
        os.makedirs(parent, mode=0o700, exist_ok=True)
        try:
            os.mkdir(self.directory, 0o700)
        except FileExistsError:
            pass
        if not self._parent_synced:
            _sync_dir(parent)
            self._parent_synced = True
        _check_private(self.directory)

    def commit(self, record: Record) -> None:
        """Make `record` durable: created exclusively (O_EXCL | O_NOFOLLOW, 0600; an existing record is never
        replaced: FileExistsError), written whole, the file synced, then the directory (and, until it has been synced
        once in this process, the directory's parent first: `make`). Only a commit that RETURNS is durable.

        (Review round 1, R3) A commit NEVER removes anything. Exclusive creation establishes ownership of the open
        inode only, not of the pathname, which a same-user process can substitute before an unlink. So a failure
        after the create LEAVES the file in place (partial, or whole but not known durable) and raises; the caller
        abandons the launch before GO and fences that record until its proof (shimlaunch.launch). A partial record
        found at a later start cannot be told from damage to a committed one: `load` raises JournalUnreadable for it,
        admission stays closed, and the node's one WARNING names the file's path for a person to check and remove.
        `remove` (after proof) is the only unlink in this module (tests/test_shim_journal.py pins it)."""
        record.check()
        data = (json.dumps(record.to_json(), sort_keys=True, separators=(",", ":"), ensure_ascii=True)
                + "\n").encode("ascii")
        if len(data) > RECORD_MAX:
            raise ValueError("the record is too large")
        path = self._path(record.launch_id)
        self.make()
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        try:
            _write_all(fd, data)
            _fullsync(fd)
        finally:
            os.close(fd)
        _sync_dir(self.directory)

    def exists(self, launch_id: str) -> bool:
        """(Review round 1, R3) Whether anything may be at this launch's record path: True unless an lstat says there
        is nothing there (any other failure to look is True: the conservative answer). Asked after a commit that
        raised, to tell "nothing was created" from "a record, whole or partial, may be left"."""
        try:
            os.lstat(self._path(launch_id))
        except FileNotFoundError:
            return False
        except OSError:
            return True
        return True

    def remove(self, launch_id: str) -> None:
        """Remove a record: unlink, then sync the directory. A record already gone is no error (the directory is
        synced all the same, so a removal whose sync failed is completed by a retry), and so is one whose journal
        directory does not exist (C3-2b-2, Part 0.2: nothing to sync). Raises OSError otherwise. Called
        only after the launch's proof (shimlaunch.release): the only unlink in this module."""
        path = self._path(launch_id)
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        try:
            _sync_dir(self.directory)
        except FileNotFoundError:
            # (C3-2b-2, Part 0.2) No journal directory at all: no record of it was ever made durable (a commit that
            # failed before it created anything), so it is already gone, and there is nothing to sync.
            return

    def load(self) -> list[Record]:
        """Every record, or JournalUnreadable. Absent directory: []. The directory must be this user's private one,
        and EVERY entry in it but Finder's `.DS_Store` (FINDER_FILE, exactly that name: Part 0.5 of C3-2b-2) a
        regular file named `<launch id>.json` holding one valid record line (no symlink, FIFO,
        subdirectory, stray or temporary name, partial or empty file is skipped: any of them makes the whole journal
        unreadable). (Review round 1, R3) A partial record, which a commit that failed leaves in place, is
        indistinguishable from damage to a committed one: it too is unreadable, and the reason names the file's full
        path, so the node's one WARNING says which file a person must check (no CLI of that launch running) and
        remove before the node, restarted, admits work again."""
        try:
            os.lstat(self.directory)
        except FileNotFoundError:
            return []
        except OSError as e:
            raise JournalUnreadable(f"the journal directory {self.directory!r} cannot be looked at "
                                    f"(errno {e.errno})") from None
        try:
            _check_private(self.directory)
            names = sorted(os.listdir(self.directory))
        except OSError as e:
            raise JournalUnreadable(f"the journal directory {self.directory!r} is not readable as the node's own "
                                    f"(errno {e.errno})") from None
        out = []
        for name in names:
            if name == FINDER_FILE:
                continue        # (C3-2b-2, Part 0.5: exactly this name, never opened; any other stray name is unreadable)
            try:
                out.append(self._read(name))
            except (OSError, ValueError, UnicodeDecodeError, RecursionError) as e:
                what = f"errno {e.errno}" if isinstance(e, OSError) else str(e)
                raise JournalUnreadable(f"journal entry {os.path.join(self.directory, name)!r}: {what} (a commit that "
                                        f"failed, or damage: once no process of that launch runs, a person removes "
                                        f"this file)") from None
        return out

    def _read(self, name: str) -> Record:
        if _NAME.fullmatch(name) is None:
            raise ValueError("not a record's name")
        path = os.path.join(self.directory, name)
        st = os.lstat(path)
        if not stat.S_ISREG(st.st_mode) or st.st_size > RECORD_MAX:
            raise ValueError("not a regular file of a record's size")
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            got = os.fstat(fd)
            if not stat.S_ISREG(got.st_mode) or (got.st_dev, got.st_ino) != (st.st_dev, st.st_ino):
                raise ValueError("not the file that was looked at")
            data = os.read(fd, RECORD_MAX + 1)
        finally:
            os.close(fd)
        if len(data) > RECORD_MAX or not data.endswith(b"\n") or data.count(b"\n") != 1:
            raise ValueError("not one complete record line")
        return _from_json(json.loads(data.decode("ascii")), name)


def journal_for(data_dir: Any) -> Journal:
    """The node's journal: `<data dir>/shim-journal` (the data dir is the node's log directory)."""
    return Journal(os.path.join(os.fspath(data_dir), JOURNAL_DIR))


def restore(groups: "node_groups.Groups", data_dir: Any) -> str | None:
    """(C3-2b-1) The node's start-up restore: the journal loaded, and for every record a HELD reservation installed in
    the shared registry under its supervised token, under the checkout's recorded key AND its key now (a checkout
    replaced since is fenced too), so that neither a hosted session nor a dispatched run is admitted there. Called by
    `Node.__init__` before any thread starts, any hub contact and any admission, whether or not hosting or
    supervision is on. Nothing is released (a restored reservation stays for the life of the process). Unreadable:
    admission is closed for the WHOLE node and the reason is returned (the caller logs it once); else None."""
    try:
        records = journal_for(data_dir).load()
    except JournalUnreadable as e:
        groups.close_admission(e.reason)
        return e.reason
    for r in records:
        keys: list[Hashable] = [r.checkout_key]
        now = node_groups.key(r.cwd)
        if now != r.checkout_key:
            keys.append(now)
        groups.restore_held(r.token, keys, r.cwd)
    return None
