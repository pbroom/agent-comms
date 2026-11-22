"""Which process is which, from the OS rather than from names a process can set (`board wait --client codex`).

`ps -o comm` reports a process's argv[0] on macOS, which any launcher can choose, and a bare name on Linux. The
Codex waiter needs the real thing twice: to watch the Codex process that ran its hook (a pid alone may be reused, so
the pid and its start time), and to run that same Codex binary for `codex queue` (so the queue is written by the
build that reads it, and nothing else is ever run). So:

- `info(pid)`: the parent pid, the start time and the real path of the executable image. macOS: libproc's
  `proc_pidinfo(PROC_PIDTBSDINFO)` and `proc_pidpath`. Linux: /proc/<pid>/stat and /proc/<pid>/exe.
- `image(pid)`: the device and inode of the image the process is running. macOS: the region of the process mapped
  from its executable (`proc_pidinfo(PROC_PIDREGIONPATHINFO)`). Linux: stat of /proc/<pid>/exe, which follows the
  running image even after the file on disk was replaced.
- `codex_ancestor()`: the nearest ancestor whose executable's real name is `codex`.
- `runnable(proc, cwd)`: the path to execute for that process, or None. It is the resolved path of its image,
  refused when it lies in the hook's directory, in any git worktree of it, or in a temporary directory (places a
  sandboxed turn or another user can write), and refused unless the file at that path is still the image the
  process runs (same device and inode).
Anything that cannot be read gives None: the caller then does nothing rather than trust a guess.
"""
from __future__ import annotations

import ctypes
import ctypes.util
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass


@dataclass(frozen=True)
class Proc:
    pid: int
    ppid: int
    start: float
    exe: str


_lib = None


def _libproc():
    global _lib
    if _lib is None:
        _lib = ctypes.CDLL(ctypes.util.find_library("proc") or "/usr/lib/libproc.dylib", use_errno=True)
    return _lib


class _BSDInfo(ctypes.Structure):          # struct proc_bsdinfo (sys/proc_info.h)
    _fields_ = [("flags", ctypes.c_uint32), ("status", ctypes.c_uint32), ("xstatus", ctypes.c_uint32),
                ("pid", ctypes.c_uint32), ("ppid", ctypes.c_uint32), ("uid", ctypes.c_uint32),
                ("gid", ctypes.c_uint32), ("ruid", ctypes.c_uint32), ("rgid", ctypes.c_uint32),
                ("svuid", ctypes.c_uint32), ("svgid", ctypes.c_uint32), ("rfu", ctypes.c_uint32),
                ("comm", ctypes.c_char * 16), ("name", ctypes.c_char * 32), ("nfiles", ctypes.c_uint32),
                ("pgid", ctypes.c_uint32), ("pjobc", ctypes.c_uint32), ("tdev", ctypes.c_uint32),
                ("tpgid", ctypes.c_uint32), ("nice", ctypes.c_int32), ("start_sec", ctypes.c_uint64),
                ("start_usec", ctypes.c_uint64)]


class _RegionInfo(ctypes.Structure):       # struct proc_regioninfo
    _fields_ = [("protection", ctypes.c_uint32), ("max_protection", ctypes.c_uint32),
                ("inheritance", ctypes.c_uint32), ("flags", ctypes.c_uint32), ("offset", ctypes.c_uint64),
                ("behavior", ctypes.c_uint32), ("user_wired_count", ctypes.c_uint32), ("user_tag", ctypes.c_uint32),
                ("pages_resident", ctypes.c_uint32), ("pages_shared_now_private", ctypes.c_uint32),
                ("pages_swapped_out", ctypes.c_uint32), ("pages_dirtied", ctypes.c_uint32),
                ("ref_count", ctypes.c_uint32), ("shadow_depth", ctypes.c_uint32), ("share_mode", ctypes.c_uint32),
                ("private_pages_resident", ctypes.c_uint32), ("shared_pages_resident", ctypes.c_uint32),
                ("obj_id", ctypes.c_uint32), ("depth", ctypes.c_uint32), ("address", ctypes.c_uint64),
                ("size", ctypes.c_uint64)]


class _VinfoStat(ctypes.Structure):        # struct vinfo_stat
    _fields_ = [("dev", ctypes.c_uint32), ("mode", ctypes.c_uint16), ("nlink", ctypes.c_uint16),
                ("ino", ctypes.c_uint64), ("uid", ctypes.c_uint32), ("gid", ctypes.c_uint32),
                ("atime", ctypes.c_int64), ("atimensec", ctypes.c_int64), ("mtime", ctypes.c_int64),
                ("mtimensec", ctypes.c_int64), ("ctime", ctypes.c_int64), ("ctimensec", ctypes.c_int64),
                ("birthtime", ctypes.c_int64), ("birthtimensec", ctypes.c_int64), ("size", ctypes.c_int64),
                ("blocks", ctypes.c_int64), ("blksize", ctypes.c_int32), ("flags", ctypes.c_uint32),
                ("gen", ctypes.c_uint32), ("rdev", ctypes.c_uint32), ("qspare", ctypes.c_int64 * 2)]


class _VnodeInfo(ctypes.Structure):
    _fields_ = [("stat", _VinfoStat), ("type", ctypes.c_int), ("pad", ctypes.c_int), ("fsid", ctypes.c_int32 * 2)]


class _RegionWithPath(ctypes.Structure):   # struct proc_regionwithpathinfo
    _fields_ = [("prinfo", _RegionInfo), ("vi", _VnodeInfo), ("path", ctypes.c_char * 1024)]


PROC_PIDTBSDINFO = 3
PROC_PIDREGIONPATHINFO = 8


def info(pid: int) -> Proc | None:
    if type(pid) is not int or pid <= 1:
        return None
    try:
        if sys.platform == "darwin":
            lib = _libproc()
            b = _BSDInfo()
            if lib.proc_pidinfo(pid, PROC_PIDTBSDINFO, ctypes.c_uint64(0), ctypes.byref(b),
                                ctypes.sizeof(b)) != ctypes.sizeof(b):
                return None
            buf = ctypes.create_string_buffer(4096)
            if lib.proc_pidpath(pid, buf, 4096) <= 0:
                return None
            return Proc(pid, int(b.ppid), b.start_sec + b.start_usec / 1e6, buf.value.decode())
        if sys.platform.startswith("linux"):
            stat = open(f"/proc/{pid}/stat").read()
            fields = stat[stat.rindex(")") + 2:].split()        # after "pid (comm) "
            return Proc(pid, int(fields[1]), float(fields[19]), os.readlink(f"/proc/{pid}/exe"))
    except (OSError, ValueError, IndexError, AttributeError):
        return None
    return None


def image(pid: int, exe: str) -> tuple[int, int] | None:
    """(device, inode) of the executable image the process `pid` runs (its path is `exe`), or None."""
    try:
        if sys.platform == "darwin":
            lib = _libproc()
            r, addr = _RegionWithPath(), 0
            for _ in range(4096):
                if lib.proc_pidinfo(pid, PROC_PIDREGIONPATHINFO, ctypes.c_uint64(addr), ctypes.byref(r),
                                    ctypes.sizeof(r)) <= 0:
                    return None
                if r.path.decode(errors="replace") == exe:
                    return int(r.vi.stat.dev), int(r.vi.stat.ino)
                addr = r.prinfo.address + max(r.prinfo.size, 1)
            return None
        if sys.platform.startswith("linux"):
            st = os.stat(f"/proc/{pid}/exe")
            return st.st_dev, st.st_ino
    except (OSError, ValueError, AttributeError):
        return None
    return None


def alive(proc: Proc) -> bool:
    """The same process is still running: the pid exists and started when it did (a pid can be reused)."""
    now = info(proc.pid)
    return now is not None and abs(now.start - proc.start) < 0.01


def codex_ancestor(pid: int | None = None, depth: int = 6) -> Proc | None:
    """The nearest ancestor of `pid` (default: this process) whose executable's real name is `codex`."""
    p = info(os.getpid() if pid is None else pid)
    for _ in range(depth):
        if p is None or p.ppid <= 1:
            return None
        p = info(p.ppid)
        if p is not None and os.path.basename(os.path.realpath(p.exe)) == "codex":
            return p
    return None


def _temp_dirs(environ: dict) -> list[str]:
    dirs = ["/tmp", "/private/tmp", "/var/tmp", "/private/var/tmp", "/var/folders", "/private/var/folders",
            tempfile.gettempdir()]
    if environ.get("TMPDIR"):
        dirs.append(environ["TMPDIR"])
    return dirs


def _worktrees(cwd: str) -> list[str]:
    """The hook directory, the top of its checkout, its git checkout's worktrees (the main one first) and its main
    repository. The top comes from `git rev-parse --show-toplevel` run there (review round 3, S2): with a separate
    git directory, `git worktree list` can name the metadata directory as the main worktree, and the hook may run
    from a subdirectory."""
    out = [cwd]
    try:
        r = subprocess.run(["git", "-C", cwd, "rev-parse", "--show-toplevel"], capture_output=True, text=True,
                           timeout=5)
        if r.returncode == 0 and r.stdout.strip():
            out.append(r.stdout.strip())
        r = subprocess.run(["git", "-C", cwd, "worktree", "list", "--porcelain"], capture_output=True, text=True,
                           timeout=5)
        out += [line[9:] for line in r.stdout.splitlines() if line.startswith("worktree ")]
        r = subprocess.run(["git", "-C", cwd, "rev-parse", "--path-format=absolute", "--git-common-dir"],
                           capture_output=True, text=True, timeout=5)
        common = r.stdout.strip()
        if r.returncode == 0 and common:
            out.append(os.path.dirname(common) if common.endswith("/.git") else common)
    except (OSError, subprocess.SubprocessError):
        pass
    return out


def _identity(path: str) -> tuple[int, int] | None:
    try:
        st = os.stat(path)
    except OSError:
        return None
    return st.st_dev, st.st_ino


def _ancestors(path: str):
    """The directories that hold `path`, from its own up to the root: (name, stat). Each is looked up by its real
    path once; identities, not spellings, are what is compared."""
    d = os.path.dirname(path)
    while True:
        try:
            yield d, os.stat(d)
        except OSError:
            yield d, None
        parent = os.path.dirname(d)
        if parent == d:
            return
        d = parent


def refused(path: str, cwd: str | None, environ: dict) -> str | None:
    """Why the binary at the resolved `path` may not be run for the waiter, else None (review round 2, S2).

    Places are compared by identity (device and inode of each directory), never by spelling: on macOS a case
    variant or a Data-volume alias (/System/Volumes/Data/...) names the same directory under another string, before
    and after realpath. Refused: a binary inside the hook's directory, a git worktree of its checkout, its main
    repository, or a temporary directory (each root's identity is taken once); a binary that is group- or
    world-writable; and one under any directory, up to the root, that is world-writable without the sticky bit.
    This is defence in depth, not an allowlist: the binary is the image of the Codex process that is already running
    this thread with the owner's rights (runnable checks it is still that file), so running it again gives nobody
    anything that process does not already have; the rules refuse the cases where that Codex was itself started
    from a place a sandboxed turn, another user or a stray script could have written."""
    if not os.path.isabs(path) or os.path.basename(path) != "codex":
        return "not an absolute path to a binary named codex"
    try:
        st = os.stat(path)
    except OSError:
        return "the binary is gone"
    if st.st_mode & 0o022:
        return "the binary is group- or world-writable"
    temp = {i for i in map(_identity, _temp_dirs(environ)) if i}
    work = {i for i in map(_identity, _worktrees(cwd)) if i} if cwd else set()
    for d, dst in _ancestors(path):
        if dst is None:
            return f"cannot inspect {d}"
        ident = (dst.st_dev, dst.st_ino)
        if ident in temp:
            return "in a temporary directory"
        if ident in work:
            return "in the thread's directory or a git worktree of it"
        if dst.st_mode & 0o002 and not dst.st_mode & 0o1000:
            return f"under a world-writable directory without the sticky bit ({d})"
    return None


def runnable(proc: Proc, cwd: str | None, environ: dict) -> tuple[str | None, str]:
    """(the path to run, "") for the Codex process `proc`, or (None, why not). Checked right before each use."""
    if not alive(proc):
        return None, "the Codex process is gone"
    path = os.path.realpath(proc.exe)
    why = refused(path, cwd, environ)
    if why:
        return None, why
    running = image(proc.pid, proc.exe) or image(proc.pid, path)
    if running is None:
        return None, "the running image cannot be identified"
    try:
        st = os.stat(path)
    except OSError:
        return None, "the binary is gone"
    if ((st.st_dev & 0xFFFFFFFF), st.st_ino) != ((running[0] & 0xFFFFFFFF), running[1]):
        return None, "the file at that path is not the image the Codex process runs"
    return path, ""
