"""(Ownership C3-1, C3-2a) The per-group supervisor ("the shim"): ONE small process per process group the node starts, run
as `python -I -S -c <boot> <package root> <args>` (agent_comms/shimchild.py starts it; C3-2b-1: no site, the package
root after the standard library). NOT wired into any spawn site yet;
DESIGN_NOTES "Ownership C3-1: the group supervisor (unwired)", its review rounds, and "Ownership C3-2a: the proof is
authenticated (still unwired)" have the protocol as tables.

What it is for. The node used to signal a process group by number: between a probe and a `killpg`, the last member can
exit and the group id be reused, and after a node crash nobody stops the orphans or can prove a checkout free. The shim
is the group's ANCHOR: it is the CLI's parent and keeps the CLI's zombie unreaped until the group is proved empty, so
the CLI's pid, and so its pgid, cannot be reused while it may still signal. It stops the group when the node dies (EOF
on its command pipe). It leaves a proof (the marker) when the group is gone, or when none was ever made.

The life of one shim:
1. Arguments, before anything durable exists: the boot identity the node read (`kern.bootsessionuuid`), the marker
   path, the protocol version, the two control fds and its own diagnostics log. NO secret: the CLI, the shim's child
   and the same user, can read its parent's argv and environment. Any bad argument, or a boot identity that is not this
   boot's or cannot be read: exit 2, nothing started, nothing published.
2. `setsid()`. fd 0/1/2 are the CLI's future stdio: they are put aside (close-on-exec copies) and replaced by /dev/null.
   The control fds are made close-on-exec.
3. (C3-2a) `NONCE`: the launch secret, in ONE bounded frame, `NONCE 1 <64 hex>\\n`, the first thing on the command pipe.
   Missing, partial, over-long, malformed, or followed by a second one: `NOSECRET <reason>`, exit 7, nothing started and
   no marker (none could be authenticated). The secret is in no argv, environment, file, event or diagnostic.
4. `GO`: one bounded, newline-terminated, versioned frame, `GO 1 <json>\\n`. Anything else (EOF first, a partial frame
   then EOF, an over-long, unparseable or unknown-version frame) never starts the CLI: the bound marker is published
   (`never_started`) and the shim exits 3.
5. Spawn: fork (the package's only one, C3-2a F6). From the fork on, everything is in the single event loop: the command
   pipe, the launch handshake and the child's exit. The child, every statement of it inside one failure boundary that
   leaves only by `os._exit` (F5), calls `setsid()` and says so (GROUP ESTABLISHED), reads the shim's RELEASE byte
   before any checkout work (it is written only once the child's exit and exec watch is INSTALLED; at EOF the child
   never enters the checkout), restores a direct `Popen` child's signal dispositions, takes back fds 0/1/2, closes
   every other fd, `chdir`s and `execvpe`s (EXEC CONFIRMED: kqueue NOTE_EXEC). Nothing reaps it early. RELEASE cannot
   keep the watcher alive: a shim killed after writing it can still let the child exec (that launch is LOST).
6. Watch: kqueue `EVFILT_PROC` `NOTE_EXIT | NOTE_EXITSTATUS | NOTE_EXEC`, never reaping: the zombie pins the pgid.
7. `TERM` / `KILL`, from the fork on, only while the anchor holds (the child unreaped, signalling not latched off): to
   the child's pid until its group is observed established, to its group after (and then any pid-only TERM or KILL is
   replayed to the group once). A KILL is sent no earlier than `kill_grace` after the shim's first TERM attempt.
   (C3-2b-1) The shim alone repeats KILL: after its first KILL, asked by a command or by EOF, it sends KILL again
   every `kill_grace` while the anchor holds. The node never resends a KILL command that was queued.
8. EOF on the command pipe (the node died or let go): TERM at once, KILL at first TERM + `kill_grace`, KILL again every
   `kill_grace` while the anchor holds. Neither event writes nor diagnostics can block or abort this.
9. Empty: a complete listing (`node_groups.group_members`) holding nothing but the exited, unreaped leader; then latch
   signalling OFF, reap, list again and require ABSENT; then publish the marker, every field authenticated by
   HMAC-SHA256 under the launch secret (`marker_mac`), created exclusively at its path and written in place with no
   staging file (nothing is ever replaced or unlinked; atomicity for the reader is the release pair and the MAC),
   `EMPTY`, exit 0. Anything else after the reap: no marker, `UNPROVED`, exit 1.
10. (C3-2a, F2) An unexpected exception after the fork: the degraded stop (`Shim.stop_after_crash`): TERM, KILL at the
   first TERM + the grace, repeated, and the same proof; if the kernel cannot confirm the anchor, no signal and no
   marker (exit 5).
11. If the shim itself is killed, launchd reaps the CLI's zombie and the anchor is lost: nothing is published (held, for
   a human). Descendants that leave the group (`setsid`/`setpgid`) are outside group-based ownership.

It never opens the board, reads config or imports the session host, and it never logs through `logging`. Its
diagnostics are OFF the escalation path: the loop only appends lines to a bounded in-memory queue; one writer thread,
started only after the single fork, alone opens and writes the log (`Diag`).
"""
from __future__ import annotations

import ctypes
import hashlib
import hmac
import json
import os
import select
import signal
import sys
import threading
import time

from . import node_groups

PROTOCOL = 1                    # the command pipe's frames: `NONCE 1 <hex>\n`, then `GO 1 <json>\n`
# (C3-2a) The marker record's version. 2: every field authenticated by HMAC-SHA256 under the launch secret (1 was
# C3-1's unauthenticated record, which the reader refuses). A later, stronger verifier takes a new number.
MARKER_VERSION = 2
KEY_BYTES = 32                  # the launch secret: fresh random bytes per launch, carried only by the NONCE frame
MAX_NONCE = 128                 # bytes of the NONCE frame at most, newline included (a valid one is 73)
MARKER_MAC_DOMAIN = b"agent-comms groupshim marker v2\0"
# (C3-2a, F4) This boot's identity: the UUID the kernel draws at every boot. Never a wall-clock value.
BOOT_SYSCTL = b"kern.bootsessionuuid"
# <sys/event.h>: ask EVFILT_PROC's NOTE_EXIT to carry the wait(2) status in `data`. Python 3.12's select module does not
# name it. Pinned HERE only, with its self-check: the kernel echoes the flag in the event's fflags when it filled `data`
# (measured on this Mac: fflags 0x84000000), and the decoded status is cross-checked against `waitid(WNOWAIT)` at every
# leader exit (`Shim._leader_exit`), which wins on any disagreement.
NOTE_EXITSTATUS = 0x04000000
# The module must IMPORT on any platform (the node-side handle imports it, and a test run off macOS collects it): the
# kqueue names exist only where the OS has kqueue, so the two read at import fall back to macOS's own values
# (<sys/event.h>: NOTE_EXIT 0x80000000, NOTE_EXEC 0x20000000). Everything else that touches kqueue is inside the shim's
# functions, which run on macOS only.
_KQ_NOTE_EXIT = getattr(select, "KQ_NOTE_EXIT", 0x80000000)
_KQ_NOTE_EXEC = getattr(select, "KQ_NOTE_EXEC", 0x20000000)
assert NOTE_EXITSTATUS & _KQ_NOTE_EXIT == 0 and NOTE_EXITSTATUS == 1 << 26
PROC_FFLAGS = _KQ_NOTE_EXIT | NOTE_EXITSTATUS | _KQ_NOTE_EXEC

MAX_GO = 4 * 1024 * 1024        # bytes of the GO frame, newline included (macOS ARG_MAX is 1 MiB; JSON escapes grow it)
MAX_COMMAND = 256               # a longer command line is answered UNKNOWN and dropped
EVENT_MAX = 400                 # every event line is shorter than PIPE_BUF (512): a non-blocking write is all or nothing
READ_CHUNK = 65536
CHECK_FIRST, CHECK_MAX = 0.02, 0.5      # the empty check's cadence after the leader's exit (doubling, capped)
GATE_WAIT = 60.0                # the test-only gates' bound (never used unless `--test-gate` is given)
DIAG_CAPACITY = 4096            # diagnostic lines queued at most; more are dropped (and counted)
DIAG_FLUSH = 0.5                # the most the exit waits for the diagnostics writer, in seconds

EXIT_EMPTY = 0          # the group was made, then proved absent: marker `empty`
EXIT_UNPROVED = 1       # after the reap the group was not proved absent: no marker
EXIT_USAGE = 2          # bad arguments (or no waitid): nothing started, nothing published
EXIT_NOGO = 3           # the CLI was never forked: marker `never_started`
EXIT_MARKER_FAILED = 4  # the marker could not be published: no proof
EXIT_CRASH = 5          # an unexpected exception (its frames are in the log): no marker
EXIT_UNSYNCED = 6       # the marker is published and visible, but its directory's sync failed: not known durable
EXIT_NOSECRET = 7       # (C3-2a) no single valid NONCE frame: nothing started, and no marker (none could be authenticated)

SIGNAMES = {signal.SIGTERM: "TERM", signal.SIGKILL: "KILL"}


# ---------------------------------------------------------------- small system helpers (no logging, no side effects)

_LIBC: list = []


def _libc():
    if not _LIBC:
        try:
            lib = ctypes.CDLL(None, use_errno=True)
            lib.waitid.argtypes = [ctypes.c_int, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_int]
            lib.waitid.restype = ctypes.c_int
        except (OSError, AttributeError):
            lib = None
        _LIBC.append(lib)
    return _LIBC[0]


def _sysctl_string(name: bytes) -> str | None:
    """A string sysctl by name, or None if it cannot be read or is not one NUL-terminated ASCII string."""
    lib = _libc()
    if lib is None or sys.platform != "darwin":
        return None
    buf = ctypes.create_string_buffer(128)
    size = ctypes.c_size_t(ctypes.sizeof(buf))
    try:
        if lib.sysctlbyname(name, buf, ctypes.byref(size), None, 0) != 0:
            return None
    except (AttributeError, OSError, ctypes.ArgumentError):
        return None
    raw = buf.raw[:size.value] if 0 < size.value <= ctypes.sizeof(buf) else b""
    if not raw.endswith(b"\0") or b"\0" in raw[:-1]:
        return None
    try:
        return raw[:-1].decode("ascii")
    except UnicodeDecodeError:
        return None


def valid_boot(value) -> bool:
    """A boot identity's shape: a UUID in its 36-character text form (8-4-4-4-12 hex digits)."""
    if type(value) is not str or len(value) != 36:
        return False
    parts = value.split("-")
    return [len(p) for p in parts] == [8, 4, 4, 4, 12] and all(c in "0123456789abcdefABCDEF" for p in parts for c in p)


def boot_id() -> str | None:
    """(C3-2a, F4) This boot's identity: `kern.bootsessionuuid`, a UUID the kernel draws at every boot, validated for
    its shape; None if it cannot be read or is not a UUID (the launch is then refused: there is no fallback, and never
    a wall-clock value). A pid or a start time from another boot proves nothing; a marker carries the boot it was made
    on."""
    value = _sysctl_string(BOOT_SYSCTL)
    return value if valid_boot(value) else None


def marker_mac(key: bytes, fields: dict) -> str:
    """(C3-2a, F1) The marker's authenticator: HMAC-SHA256 under the launch secret over a domain tag and the canonical
    JSON of every field but `mac` (sorted keys, no spaces, ASCII), as 64 lowercase hex digits. The secret itself is
    never written anywhere."""
    body = {k: v for k, v in fields.items() if k != "mac"}
    data = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    return hmac.new(key, MARKER_MAC_DOMAIN + data, hashlib.sha256).hexdigest()


def decode_status(status: int) -> int | None:
    """A wait(2) status as `Popen.returncode` gives it: the exit code, or the negative signal number."""
    if os.WIFEXITED(status):
        return os.WEXITSTATUS(status)
    if os.WIFSIGNALED(status):
        return -os.WTERMSIG(status)
    return None


_P_PID, _WEXITED, _WNOHANG, _WNOWAIT = 1, 0x04, 0x01, 0x20      # <sys/wait.h> on macOS
_CLD_EXITED, _CLD_KILLED, _CLD_DUMPED = 1, 2, 3


def waitid_nowait(pid: int) -> int | None:
    """The exit of `pid`, an exited and UNREAPED child of this process, WITHOUT reaping it (`waitid(P_PID, pid,
    WEXITED | WNOHANG | WNOWAIT)`; Python 3.12 has no `os.waitid` on macOS), decoded as `Popen.returncode`; None when it
    has not exited, is not our child, or the call failed."""
    lib = _libc()
    if lib is None:
        return None
    info = ctypes.create_string_buffer(128)     # siginfo_t is 104 bytes on macOS
    ctypes.set_errno(0)
    if lib.waitid(_P_PID, pid, info, _WEXITED | _WNOHANG | _WNOWAIT) != 0:
        return None
    signo, _err, code, got, _uid, status = (int.from_bytes(info.raw[i:i + 4], "little", signed=True)
                                            for i in range(0, 24, 4))
    if got != pid or signo != signal.SIGCHLD:
        return None
    if code == _CLD_EXITED:
        return status
    if code in (_CLD_KILLED, _CLD_DUMPED):
        return -status
    return None


def child_state(pid: int) -> str | None:
    """(C3-2a, F2) Whether `pid` is still this process's own UNREAPED child, asked of the kernel without reaping
    (`waitid(P_PID, pid, WEXITED | WNOHANG | WNOWAIT)`): "running", "exited" (a zombie, its pid pinned), or None when it
    is not (ECHILD: never ours, or already reaped) or the call failed."""
    lib = _libc()
    if lib is None:
        return None
    info = ctypes.create_string_buffer(128)     # (zeroed: with WNOHANG and nothing to report, si_pid stays 0)
    ctypes.set_errno(0)
    if lib.waitid(_P_PID, pid, info, _WEXITED | _WNOHANG | _WNOWAIT) != 0:
        return None
    got = int.from_bytes(info.raw[12:16], "little", signed=True)
    if got == 0:
        return "running"
    return "exited" if got == pid else None


def _fullsync(fd: int) -> None:
    """fsync, with macOS's F_FULLFSYNC (through the drive's cache) where it is taken."""
    try:
        import fcntl
        fcntl.fcntl(fd, fcntl.F_FULLFSYNC)
        return
    except (ImportError, AttributeError, OSError):
        pass
    os.fsync(fd)


PUBLISHED, UNSYNCED, NOT_PUBLISHED = "published", "unsynced", "not_published"


MARKER_MAX = 4096                   # bytes a marker record may have (the shim's are about 250)


def write_marker(path: str, record: dict, fail_dir_sync: bool = False, fail_file_sync: bool = False,
                 after_create=None) -> tuple[str, int]:
    """Publish `record` at `path` with NO staging file (C3-2a review round 2, R5): the shim creates `path` ITSELF,
    exclusively (`O_CREAT | O_EXCL | O_NOFOLLOW`, mode 0600: anything already there, a dangling symlink included, is
    EEXIST and is never replaced), writes the whole record through that descriptor (a loop that completes a short
    write), syncs the DESCRIPTOR (F_FULLFSYNC), closes it, then syncs the directory. No temp file, no link, no rename,
    and nothing is EVER unlinked, on success or on any error: a same-user process can enter any directory the shim can,
    so no other name could be protected anyway.

    Atomicity for the READER does not come from the filesystem: a reader may see the file before it is complete. It
    comes from the release pair and the MAC (`shimchild.read_marker`, `ShimChild.empty()`): a marker counts only with
    the shim's own observed terminal status in a proof-bearing pair AND a MAC that verifies over the complete record.
    A partial or substituted file fails one or the other, and the launch stays held. (outcome, errno):
    - PUBLISHED, 0: written and synced, directory synced (as durable as this machine's F_FULLFSYNC makes it);
    - NOT_PUBLISHED, errno: the create failed (EEXIST: whatever is there is left exactly as it was; nothing of ours
      exists), or the write or the file's sync failed after the create: the file is LEFT as it is, possibly partial,
      possibly complete and authentic, and the caller must end in a status that is in no release pair;
    - UNSYNCED, errno: the file is written and synced, so the complete marker is VISIBLE (and valid proof of absence
      for a living node), but the directory's sync failed: whether it survives a crash is not known.
    TEST-ONLY (the test gate dir; the node never asks them): `fail_dir_sync` / `fail_file_sync` make that sync fail
    with EIO; `after_create` is called right after the create."""
    data = (json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n").encode()
    if len(data) > MARKER_MAX:
        return NOT_PUBLISHED, 27        # (EFBIG; never: the shim's records are about 250 bytes)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    except OSError as e:
        return NOT_PUBLISHED, e.errno or -1     # (nothing of ours exists; whatever was there is untouched)
    try:
        if after_create is not None:
            after_create()
        view = memoryview(data)
        while view:
            n = os.write(fd, view)
            if n <= 0:
                raise OSError(5, "a write wrote nothing")
            view = view[n:]             # (a short write: the loop writes the rest)
        if fail_file_sync:
            raise OSError(5, "injected EIO (test-only)")
        _fullsync(fd)
    except OSError as e:
        return NOT_PUBLISHED, e.errno or -1     # the file is left as it is, possibly partial: never unlinked
    finally:
        _close(fd)
    try:
        dfd = os.open(os.path.dirname(path) or ".", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            if fail_dir_sync:
                raise OSError(5, "injected EIO (test-only)")
            _fullsync(dfd)
        finally:
            os.close(dfd)
    except OSError as e:
        return UNSYNCED, e.errno or -1
    return PUBLISHED, 0


def _close(fd: int) -> None:
    try:
        os.close(fd)
    except OSError:
        pass


# ---------------------------------------------------------------- a crash's description, with no source lookup

CRASH_FRAMES = 32               # the innermost frames a crash's description keeps


def crash_frames(exc: BaseException) -> tuple[str, tuple]:
    """(C3-2a review round 1, R2) What a crash's diagnostic says, taken WITHOUT any source lookup or I/O (no
    `traceback.format_*`, no `linecache`): the exception's type name and the innermost CRASH_FRAMES (file, line,
    function) triples, read from the traceback objects' code objects."""
    frames: list = []
    tb = getattr(exc, "__traceback__", None)
    walked = 0
    while tb is not None and walked < 100_000:
        code = tb.tb_frame.f_code
        frames.append((code.co_filename, tb.tb_lineno, code.co_name))
        if len(frames) > CRASH_FRAMES:
            del frames[0]
        tb, walked = tb.tb_next, walked + 1
    return type(exc).__name__, tuple(frames)


def render_crash(what: str, name: str, frames: tuple) -> str:
    """The crash's diagnostic text. Called only by the diagnostics writer thread (`Diag.put_lazy`), never on the
    escalation path."""
    return f"{what}: {name}" + "".join(f"\n    at {f}:{line} in {fn}" for f, line, fn in frames)


# ---------------------------------------------------------------- diagnostics, off the escalation path

class Diag:
    """The shim's diagnostics. `put` only appends to a bounded in-memory list (full: the line is dropped and counted)
    and never opens, writes or waits on the log. One daemon thread alone opens the log and writes it; a log that blocks
    (a FIFO nobody reads, a stalled filesystem) stalls only that thread. The thread is started only after the shim's
    single fork (or at its exit, if it never forks), so the fork is made single-threaded. The exit waits for it at most
    DIAG_FLUSH seconds. (Review round 2, R5) Starting the thread is BEST-EFFORT: if it cannot be made or started (the
    system refuses a thread), the diagnostics are off: every line is dropped and counted, and supervision goes on."""

    def __init__(self, path: str | None, clock, fail_start: bool = False):
        self.path, self.clock = path, clock
        self.fail_start = fail_start    # (TEST-ONLY: `fail-diag-start` in the test gate dir; the node never asks it)
        self.cond = threading.Condition(threading.Lock())
        self.lines: list[str] = []
        self.dropped = 0
        self.busy = False
        self.failed = False             # the thread could not be started: diagnostics are off
        self.thread: threading.Thread | None = None

    def put(self, msg: str) -> None:
        line = f"{self.clock():.6f} shim {os.getpid()} {msg}\n"
        with self.cond:         # (the writer holds this lock only to swap the list, never across I/O)
            if self.failed or len(self.lines) >= DIAG_CAPACITY:
                self.dropped += 1
                return
            self.lines.append(line)
            self.cond.notify()

    def put_lazy(self, render, *args) -> None:
        """(C3-2a review round 1, R2) A line whose TEXT is made by the writer thread, `render(*args)`, never by the
        caller: whatever rendering costs (or stalls on) is the writer's alone. Queued like `put`, stamped now."""
        item = (f"{self.clock():.6f} shim {os.getpid()} ", render, args)
        with self.cond:
            if self.failed or len(self.lines) >= DIAG_CAPACITY:
                self.dropped += 1
                return
            self.lines.append(item)
            self.cond.notify()

    @staticmethod
    def _text(item) -> str:
        if isinstance(item, str):
            return item
        prefix, render, args = item
        try:
            body = str(render(*args))
        except Exception:
            body = "(a line that could not be rendered)"
        return prefix + body + "\n"

    def start(self) -> None:
        if self.thread is not None or self.failed or self.path is None:
            return
        try:
            if self.fail_start:
                raise RuntimeError("can't start new thread (injected, test-only)")
            thread = threading.Thread(target=self._run, name="shim-diag", daemon=True)
            thread.start()
            self.thread = thread
        except Exception:
            self.failed = True
            with self.cond:
                self.dropped += len(self.lines)
                self.lines = []

    def _run(self) -> None:
        try:
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        except OSError:
            fd = None
        while True:
            with self.cond:
                while not self.lines:
                    self.busy = False
                    self.cond.wait()
                batch, self.lines, self.busy = self.lines, [], True
            if fd is None:
                continue
            data = "".join(self._text(item) for item in batch).encode("utf-8", "replace")
            try:
                while data:
                    data = data[os.write(fd, data):]
            except OSError:
                pass

    def flush(self, bound: float = DIAG_FLUSH) -> None:
        self.start()
        if self.thread is None:
            return
        deadline = time.monotonic() + bound
        while time.monotonic() < deadline:
            with self.cond:
                if not self.lines and not self.busy:
                    return
            time.sleep(0.005)


# ---------------------------------------------------------------- the NONCE and GO frames

class NoGo(Exception):
    """The GO frame was not a valid one: `reason` is eof, partial, overlong, invalid or version."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class NoSecret(Exception):
    """(C3-2a) No single valid NONCE frame: `reason` is eof, partial, overlong, invalid, version or duplicate."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def parse_nonce(line: bytes) -> bytes:
    """One NONCE line (without its newline), `NONCE 1 <64 lowercase hex digits>`, -> the launch secret's bytes;
    NoSecret("version" | "invalid") otherwise."""
    parts = line.split(b" ")
    if len(parts) != 3 or parts[0] != b"NONCE" or not parts[1].isdigit():
        raise NoSecret("invalid")
    if int(parts[1]) != PROTOCOL:
        raise NoSecret("version")
    value = parts[2]
    if len(value) != 2 * KEY_BYTES or any(c not in b"0123456789abcdef" for c in value):
        raise NoSecret("invalid")
    return bytes.fromhex(value.decode("ascii"))


def parse_go(line: bytes) -> dict:
    """One GO line (without its newline) -> {cwd, argv, env, kill_grace}; NoGo("version" | "invalid") otherwise."""
    head, sep, rest = line.partition(b" ")
    if head != b"GO" or not sep:
        raise NoGo("invalid")
    ver, sep, body = rest.partition(b" ")
    if not ver.isdigit() or not sep:
        raise NoGo("invalid")
    if int(ver) != PROTOCOL:
        raise NoGo("version")
    try:
        spec = json.loads(body.decode("ascii"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise NoGo("invalid") from None
    if not isinstance(spec, dict) or set(spec) != {"cwd", "argv", "env", "kill_grace"}:
        raise NoGo("invalid")
    cwd, argv, env, grace = spec["cwd"], spec["argv"], spec["env"], spec["kill_grace"]

    def text(v) -> bool:
        return isinstance(v, str) and "\0" not in v
    if not (text(cwd) and os.path.isabs(cwd)):
        raise NoGo("invalid")
    if not (isinstance(argv, list) and argv and all(text(a) for a in argv) and argv[0]):
        raise NoGo("invalid")
    if not (isinstance(env, dict) and all(text(k) and k and "=" not in k and text(v) for k, v in env.items())):
        raise NoGo("invalid")
    if isinstance(grace, bool) or not isinstance(grace, (int, float)) or not 0 < grace <= 3600:
        raise NoGo("invalid")
    return {"cwd": cwd, "argv": argv, "env": env, "kill_grace": float(grace)}


# ---------------------------------------------------------------- the shim

class Shim:
    """One group's supervisor. `run()` (after `read_go` and `spawn`) is its single-threaded loop. The system calls it
    decides with are attributes (`members`, `killpg`, `kill`, `waitpid`, `waitid`, `clock`) so the decisions can be
    pinned in process; the real process uses the real ones."""

    def __init__(self, *, key: bytes | None, boot: str, marker: str, cmd_fd: int, event_fd: int | None,
                 sink: Diag | None = None, gate: str | None = None):
        # The launch secret (C3-2a): None until the NONCE frame is read. Never in an event, a diagnostic or a file.
        self.key, self.boot, self.marker = key, boot, marker
        self.cmd_fd, self.event_fd, self.sink = cmd_fd, event_fd, sink
        self.gate_dir, self.gate_seq = gate, 0
        self.members = node_groups.group_members
        self.killpg = os.killpg
        self.kill = os.kill
        self.waitpid = os.waitpid
        self.waitid = waitid_nowait
        self.clock = time.monotonic
        self.buf = b""                  # command bytes read and not yet taken
        self.eof = False                # the command pipe reached EOF (the node died or let go)
        self.discarding = False         # inside an over-long command line, dropped up to its newline
        self.pgid: int | None = None    # the child's pid: its pgid once established (child-side setsid); set at fork
        self.established = False       # GROUP ESTABLISHED: the child said it has done setsid() (the `S` byte read)
        self.exec_confirmed = False     # EXEC CONFIRMED: kqueue NOTE_EXEC (registered before the release byte)
        self.start_said = False         # STARTED, START_FAILED or START_UNKNOWN sent
        self.released = False           # RELEASE was written: the child may exec from then on
        self.pid_only: list[int] = []   # signals sent to the pid before establishment was observed (replayed once)
        self.hs_fd: int | None = None   # the handshake's read end, non-blocking, read in the loop
        self.hs_said = b""
        self.hs_eof = False
        self.hs_deferred = False        # (test-only: `defer-handshake` in the gate dir)
        self.hs_polled = False          # the handshake could not be registered: read on a short cadence instead
        self.degraded = False           # the kqueue wait failed: everything is polled on a short cadence
        self.grace = 0.0
        self.leader_exited = False
        self.leader_code: int | None = None
        self.latched = False            # signalling OFF, for good (set just before the reap)
        self.reaped = False
        self.first_term_at: float | None = None     # the shim's own first actual TERM attempt (sent or failed)
        self.kill_due: float | None = None          # when the next KILL may be sent (never before first TERM + grace)
        self.check_due: float | None = None
        self.check_step = CHECK_FIRST
        self.dropped = self.dropped_after_eof = self.unknown = 0
        self.kq: select.kqueue | None = None
        self.watching = False           # the child's exit (and exec) are registered with the kqueue
        self.rel_w: int | None = None   # RELEASE's write end, from the fork until it is written (or not) and closed
        self.crash: BaseException | None = None     # (the degraded stop's exception, described once, after its TERM)

    # -------- diagnostics and events

    def diag(self, msg: str) -> None:
        """A diagnostic line: queued, never written here (`Diag`); it cannot raise into, wait in or reorder the loop."""
        if self.sink is None:
            return
        try:
            self.sink.put(msg)
        except Exception:
            pass

    def diag_crash(self, what: str, exc: BaseException) -> None:
        """A crash's diagnostic (R2): its frames taken now from the code objects (memory only), its text rendered by the
        diagnostics thread. Contained: it cannot raise into, wait in or reorder the caller."""
        if self.sink is None:
            return
        try:
            self.sink.put_lazy(render_crash, what, *crash_frames(exc))
        except Exception:
            pass

    def emit(self, line: str) -> None:
        """One event to the node: non-blocking and atomic (shorter than PIPE_BUF). A full pipe drops it; a closed one
        (EPIPE, the node is gone) stops events for good. Neither ever delays or aborts what the shim does next: the
        marker, not an event, is the proof. (Review round 2, R5) Nothing in it can raise into the caller."""
        try:
            self._emit(line)
        except Exception:
            pass

    def _emit(self, line: str) -> None:
        if self.event_fd is not None:
            data = (line[:EVENT_MAX - 1] + "\n").encode("ascii", "replace")
            try:
                os.write(self.event_fd, data)
            except BlockingIOError:
                self.dropped += 1
                if self.eof:
                    self.dropped_after_eof += 1
                if self.dropped == 1:
                    self.diag("event dropped: the event pipe is full (counting from here)")
            except OSError as e:
                try:
                    os.close(self.event_fd)
                except OSError:
                    pass
                self.event_fd = None
                self.diag(f"event channel closed (errno {e.errno}): no more events")
        if line != "UNKNOWN" or self.unknown < 3:   # (a flood of unknown commands is not copied into the log)
            self.diag(f"event {line}")
        if line == "UNKNOWN":
            self.unknown += 1

    def gate(self, name: str, detail: str = "") -> None:
        """TEST-ONLY (`--test-gate DIR` with a `numbered` file in it; the node never passes it): announce `at.<n>` and
        wait, bounded, for `go.<n>`."""
        if self.gate_dir is None:
            return
        try:
            self._gate(name, detail)
        except Exception:
            pass

    def _gate(self, name: str, detail: str) -> None:
        if not os.path.exists(os.path.join(self.gate_dir, "numbered")):
            return
        self.gate_seq += 1
        n = self.gate_seq
        at = os.path.join(self.gate_dir, f"at.{n}")
        with open(at + ".tmp", "w") as f:
            f.write(f"{name} {detail}\n")
        os.rename(at + ".tmp", at)
        deadline = self.clock() + GATE_WAIT
        while not os.path.exists(os.path.join(self.gate_dir, f"go.{n}")) and self.clock() < deadline:
            time.sleep(0.005)

    # -------- the marker

    def publish(self, outcome: str) -> int | None:
        """Publish the bound marker: every field authenticated under the launch secret (`marker_mac`), the secret
        itself never written. None when it is published and synced; else the exit status to end with:
        EXIT_MARKER_FAILED (no proof: nothing of ours at the path, or a file left as it is after a failed write or sync,
        which no release pair takes) or EXIT_UNSYNCED (visible, valid, its durability unknown)."""
        if self.key is None:            # (never: the shim publishes only after a valid NONCE frame)
            self.emit("MARKER_FAILED 0")
            return EXIT_MARKER_FAILED
        record = {"v": MARKER_VERSION, "outcome": outcome, "cli_pid": self.pgid, "leader_exit": self.leader_code,
                  "boot": self.boot}
        record["mac"] = marker_mac(self.key, record)
        result, err = write_marker(self.marker, record, fail_dir_sync=self._gate_file("fail-dir-sync"),
                                   fail_file_sync=self._gate_file("fail-file-sync"),
                                   after_create=self._marker_pause if self._gate_file("pause-after-create") else None)
        if result == NOT_PUBLISHED:
            self.emit(f"MARKER_FAILED {err}")
            self.diag(f"the marker could not be published (errno {err}): no proof (any file there is left as it is)")
            return EXIT_MARKER_FAILED
        if result == UNSYNCED:
            self.emit(f"UNSYNCED {err}")
            self.diag(f"marker published ({outcome}) and visible, but its directory's sync failed (errno {err})")
            return EXIT_UNSYNCED
        self.diag(f"marker published: {outcome}")
        return None

    def _marker_pause(self) -> None:
        """TEST-ONLY (`pause-after-create` in the test gate dir): right after the marker path is created, announce
        `marker-created.at` (created exclusively, never renamed) and wait, bounded, for `marker-created.go`."""
        try:
            _close(os.open(os.path.join(self.gate_dir, "marker-created.at"),
                           os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o600))
            deadline = self.clock() + GATE_WAIT
            while not os.path.exists(os.path.join(self.gate_dir, "marker-created.go")) and self.clock() < deadline:
                time.sleep(0.005)
        except Exception:
            pass

    def never_started(self, reason: str) -> int:
        """Every outcome in which the CLI was never forked: the bound marker (the group provably never existed)."""
        self.emit(f"NOGO {reason}")
        failed = self.publish("never_started")
        if failed == EXIT_MARKER_FAILED:
            return failed
        self.emit("EMPTY")
        return EXIT_NOGO if failed is None else failed

    def no_secret(self, reason: str) -> int:
        """(C3-2a) No single valid NONCE frame: nothing was forked, and no marker is published, since none could be
        authenticated. The node tells "never started" from this exit status (its own unreaped child's), not from a
        file."""
        self.emit(f"NOSECRET {reason}")
        self.diag(f"no valid secret ({reason}): nothing started, no marker")
        return EXIT_NOSECRET

    # -------- NONCE, then GO

    def _read_frame(self, limit: int, fail) -> bytes:
        """Block (kqueue on the command fd) until one complete line of at most `limit` bytes, newline included, and
        return it without its newline; `fail(reason)` is raised otherwise (eof, partial, overlong). Bytes after it stay
        buffered."""
        kq = select.kqueue()
        try:
            kq.control([select.kevent(self.cmd_fd, select.KQ_FILTER_READ, select.KQ_EV_ADD)], 0)
            while True:
                nl = self.buf.find(b"\n")
                if nl >= 0:
                    if nl + 1 > limit:
                        raise fail("overlong")
                    line, self.buf = self.buf[:nl], self.buf[nl + 1:]
                    return line
                if len(self.buf) >= limit:
                    raise fail("overlong")
                chunk = self._read_cmd()
                if chunk is None:
                    kq.control(None, 1, None)
                    continue
                if not chunk:
                    self.eof = True
                    raise fail("partial" if self.buf else "eof")
                self.buf += chunk
        finally:
            kq.close()

    def read_secret(self) -> None:
        """(C3-2a, F1) The launch secret: the first frame on the command pipe, one bounded `NONCE` line; NoSecret
        otherwise. It never travels on the argv, in the environment or in a file."""
        self.key = parse_nonce(self._read_frame(MAX_NONCE, NoSecret))

    def read_go(self) -> dict:
        """The GO frame, after the secret: one complete GO line; NoGo otherwise, and NoSecret("duplicate") for a
        second NONCE frame. Bytes after it stay buffered as commands."""
        line = self._read_frame(MAX_GO, NoGo)
        if line.split(b" ", 1)[0] == b"NONCE":
            raise NoSecret("duplicate")
        return parse_go(line)

    def _read_cmd(self) -> bytes | None:
        """One non-blocking read of the command pipe: bytes, b"" at EOF, None when nothing is there now."""
        try:
            return os.read(self.cmd_fd, READ_CHUNK)
        except BlockingIOError:
            return None
        except OSError as e:
            self.diag(f"the command pipe failed (errno {e.errno}): taken as EOF")
            return b""

    # -------- spawn

    def spawn(self, spec: dict, stdio: tuple[int, int, int], ignored: frozenset) -> int | None:
        """Fork the child and return at once (None): the handshake, the child's exit and the commands are all read in
        the loop (`run`). An exit status when the child was never forked.

        (Review round 2, R5) Everything that can fail for ordinary reasons and is needed to supervise (the pipes, the
        kqueue, the command pipe's registration, the handshake's non-blocking mode) is made BEFORE the fork, where a
        failure is a never-started launch with its marker. After the fork nothing may end the shim while it holds the
        unreaped child: every step is either essential and answered by an existing outcome, or contained (DESIGN_NOTES
        C3-1 review round 2 has the table)."""
        self.grace = spec["kill_grace"]
        made: list[int] = []
        try:
            hs_r, hs_w = os.pipe()          # (close-on-exec: EOF at the child's exec or death)
            made += [hs_r, hs_w]
            rel_r, rel_w = os.pipe()        # RELEASE: the child does no checkout work before reading it
            made += [rel_r, rel_w]
            os.set_blocking(hs_r, False)
            self.kq = select.kqueue()
            if not self.eof:
                self.kq.control([select.kevent(self.cmd_fd, select.KQ_FILTER_READ, select.KQ_EV_ADD)], 0)
        except OSError as e:
            for fd in (*made, *stdio):
                _close(fd)
            self.emit(f"START_FAILED {e.errno}")
            self.diag(f"could not prepare the launch (errno {e.errno})")
            return self.never_started("fork")
        try:
            pid = os.fork()
        except OSError as e:
            for fd in (*made, *stdio):
                _close(fd)
            self.emit(f"START_FAILED {e.errno}")
            self.diag(f"fork failed (errno {e.errno})")
            return self.never_started("fork")
        if pid == 0:
            try:                        # (F5: the child's every statement inside its boundary, out by os._exit only)
                _child(spec, stdio, hs_w, rel_r, ignored, self.gate_dir, (hs_r, rel_w))      # never returns
            finally:
                os._exit(127)
        # ---- from here the shim holds an unreaped child: nothing below may end it (R5); a crash is stopped (F2)
        self.pgid, self.hs_fd, self.rel_w = pid, hs_r, rel_w
        for fd in (hs_w, rel_r, *stdio):
            _close(fd)                # the CLI's stdio is the CLI's alone now
        if self.sink is not None:
            try:
                self.sink.start()       # (after the one fork: the fork was single-threaded; best-effort)
            except Exception:
                pass
        self.gate("register", str(pid))
        try:
            self.kq.control([select.kevent(pid, select.KQ_FILTER_PROC, select.KQ_EV_ADD | select.KQ_EV_CLEAR,
                                           PROC_FFLAGS)], 0)
            self.watching = True
        except OSError as e:
            # ESRCH: it is already a zombie (nothing reaped it); without RELEASE it never exec'd. Otherwise its exit is
            # polled (waitid) in the loop, and it gets no RELEASE: it never execs.
            self.diag(f"exit registration refused (errno {e.errno}): the child is not released")
        if self.watching:
            # RELEASE (review round 2, R6): written only AFTER the exit-and-exec watch is INSTALLED, so no exec can
            # happen unwatched. It cannot keep the watcher alive: a shim killed after this write can still let the
            # child exec, and that CLI then runs unsupervised. That launch is LOST (`ShimChild.lost()`), and its
            # checkout stays retained.
            try:
                os.write(rel_w, b"G")
                self.released = True    # (from here an exec may happen: only NOTE_EXEC or a failure report settles it)
            except OSError:
                pass
        self.rel_w = None
        _close(rel_w)                   # (not written: EOF, and the child never enters the checkout)
        self.diag(f"forked the child, pid {pid}")
        self.hs_deferred = self._gate_file("defer-handshake")
        if not self.hs_deferred:
            self._watch_handshake()
        if not self.watching and self._exited() is not None:
            self._leader_exit(None)
        return None

    def _gate_file(self, name: str) -> bool:
        """TEST-ONLY: whether the gate dir has `name` (False without a gate dir, or on any error)."""
        try:
            return self.gate_dir is not None and os.path.exists(os.path.join(self.gate_dir, name))
        except Exception:
            return False

    def _watch_handshake(self) -> None:
        try:
            self.kq.control([select.kevent(self.hs_fd, select.KQ_FILTER_READ, select.KQ_EV_ADD)], 0)
        except (OSError, TypeError):
            self.hs_polled = True       # (contained: the loop reads it on a short cadence instead)

    def _exited(self) -> int | None:
        """`waitid(WNOWAIT)` on our child, contained: None when it has not exited or the call failed."""
        try:
            return self.waitid(self.pgid)
        except Exception:
            return None

    def _members(self) -> tuple | None:
        """The group's complete listing, contained: None (no proof) on any failure."""
        try:
            return self.members(self.pgid)
        except Exception:
            return None

    # -------- the loop

    def run(self) -> int:
        """The loop. The kqueue wait itself is contained: if it fails, the loop goes on at a short cadence and polls
        everything (the command pipe, the handshake, the child's exit by `waitid`)."""
        assert self.kq is not None and self.pgid is not None
        self._take_commands()           # (anything sent behind GO)
        while True:
            if self.gate_dir is not None:
                self._test_crash()
            if self.hs_deferred and self._gate_file("defer-handshake.go"):
                self.hs_deferred = False
                self._watch_handshake()
            now = self.clock()
            if self.kill_due is not None and now >= self.kill_due:
                self._kill_now()
            if self.check_due is not None and self.clock() >= self.check_due:
                done = self._check_empty()
                if done is not None:
                    return done
            now = self.clock()
            dues = [d for d in (self.kill_due, self.check_due) if d is not None]
            timeout = max(0.0, min(dues) - now) if dues else None
            polling = self.hs_deferred or self.hs_polled or self.degraded or (not self.watching
                                                                               and not self.leader_exited)
            if polling:
                timeout = 0.05 if timeout is None else min(timeout, 0.05)
            try:
                events = self.kq.control(None, 8, timeout)
            except OSError as e:
                if not self.degraded:
                    self.degraded = True
                    self.diag(f"the kqueue wait failed (errno {e.errno}): polling from here")
                events = []
                time.sleep(0.05 if timeout is None else min(timeout, 0.05))
            for ev in events:
                if ev.filter == select.KQ_FILTER_PROC:
                    if ev.fflags & select.KQ_NOTE_EXEC:
                        self._exec_seen()
                    if ev.fflags & select.KQ_NOTE_EXIT:
                        self._leader_exit(ev)
                elif ev.filter == select.KQ_FILTER_READ and ev.ident == self.cmd_fd:
                    self._pump_commands()
                elif ev.filter == select.KQ_FILTER_READ and ev.ident == self.hs_fd:
                    self._pump_handshake()
            if self.degraded and not self.eof:
                self._pump_commands()
            if (self.degraded or self.hs_polled) and not self.hs_deferred:
                self._pump_handshake()
            if (self.degraded or not self.watching) and not self.leader_exited and self._exited() is not None:
                self._leader_exit(None)

    def _test_crash(self) -> None:
        """TEST-ONLY (`--test-gate DIR`; the node never passes it): an unexpected exception in the loop, after the
        fork. `crash-in-loop`: raise. `crash-reap`: first reap the child if it has exited (so its anchor is gone), then
        raise."""
        if self._gate_file("crash-reap"):
            try:
                os.waitpid(self.pgid, os.WNOHANG)
            except OSError:
                pass
            raise RuntimeError("injected (test-only): a crash after the child was reaped")
        if self._gate_file("crash-in-loop"):
            raise RuntimeError("injected (test-only): an unexpected exception in the loop")

    # -------- (C3-2a, F2) after an unexpected exception past the fork: the degraded stop

    def stop_after_crash(self, exc: BaseException | None = None) -> int:
        """An unexpected exception after the fork (a bug) must not leave the group running unsupervised. A minimal
        loop, with as little of the normal machinery as it can: no kqueue, no commands. By the existing anchor and
        target rules (`_signal`): a TERM now (the grace still counted from the FIRST TERM attempt, kept if one was
        made), a KILL at that attempt + the grace (`_kill_now`, the one KILL sender), repeated every grace; the child's
        exit polled by `waitid`; then the SAME verified release (`_check_empty`: listing, latch, reap, second listing,
        authenticated marker). Before every signal and before the proof, the kernel must confirm the anchor (our own
        unreaped child, `child_state`); if it cannot, nothing more is signalled and nothing is published: exit 5, no
        marker (the node keeps the checkout held). A crash inside this loop ends it the same way.

        (Review round 1, R2) It is the FIRST thing that runs after the exception: the crash is described only after
        the first TERM (or the refusal), from its code objects alone, and rendered by the diagnostics thread."""
        self.crash = exc
        try:
            return self._stop_after_crash()
        except BaseException:
            self.latched = True
            self.diag("the degraded stop itself failed: no more signals, no marker")
            return EXIT_CRASH

    def _describe_crash(self) -> None:
        if self.crash is not None:
            exc, self.crash = self.crash, None
            self.diag_crash("crashed after the fork; the degraded stop took over", exc)

    def _anchor_confirmed(self) -> bool:
        return self.anchored() and child_state(self.pgid) is not None

    def _unanchored(self) -> int:
        self.latched = True             # (no signal from here, for good)
        self.emit("UNPROVED")
        self.diag("after the crash the anchor cannot be established: no signal, no marker")
        self._describe_crash()
        return EXIT_CRASH

    def _take_establishment(self) -> None:
        """The child's `S`, read without the loop (non-blocking, contained): its group is the target from then on."""
        if self.established or self.hs_fd is None:
            return
        try:
            chunk = os.read(self.hs_fd, 256)
        except Exception:
            return
        self.hs_said += chunk
        if self.hs_said.startswith(b"S"):
            self._established()

    def _stop_after_crash(self) -> int:
        self.emit("CRASHED")
        self.eof = True                 # (as after the node's EOF: a KILL every grace while the anchor holds)
        self.check_due = None
        if self.rel_w is not None:      # (crashed before RELEASE was written: the child never enters the checkout)
            _close(self.rel_w)
            self.rel_w = None
        if not self._anchor_confirmed():
            return self._unanchored()
        self._take_establishment()
        self._signal(signal.SIGTERM)    # (sets the first TERM attempt only if none was made: the grace is not reset)
        if self.first_term_at is None:
            return self._unanchored()
        self.kill_due = self.first_term_at + self.grace     # the ORIGINAL first TERM attempt's grace
        self._describe_crash()          # (after the first TERM: queued, rendered by the diagnostics thread)
        while True:
            if self.kill_due is not None and self.clock() >= self.kill_due:
                if not self._anchor_confirmed():
                    return self._unanchored()
                self._kill_now()
            if not self.leader_exited:
                code = self._exited()
                if code is not None:
                    self.leader_exited, self.leader_code = True, code
                    self.emit(f"LEADER_EXIT {code}")
            if self.leader_exited:
                if not self._anchor_confirmed():
                    return self._unanchored()
                done = self._check_empty()
                if done is not None:
                    return done
            self._take_establishment()
            time.sleep(0.02)

    def _pump_commands(self) -> None:
        for _ in range(16):             # bounded per wake: timers are looked at between
            chunk = self._read_cmd()
            if chunk is None:
                break
            if not chunk:
                self._take_commands()
                self._on_eof()
                return
            self.buf += chunk
            self._take_commands()

    def _take_commands(self) -> None:
        while True:
            nl = self.buf.find(b"\n")
            if nl < 0:
                if len(self.buf) > MAX_COMMAND:
                    self.buf, self.discarding = b"", True
                    self.emit("UNKNOWN")
                return
            line, self.buf = self.buf[:nl], self.buf[nl + 1:]
            if self.discarding:
                self.discarding = False
                continue
            word = line.strip()
            if word == b"TERM":
                self._signal(signal.SIGTERM)
            elif word == b"KILL":
                self._kill_request()
            else:
                self.emit("UNKNOWN")

    def _on_eof(self) -> None:
        self.eof = True
        try:
            self.kq.control([select.kevent(self.cmd_fd, select.KQ_FILTER_READ, select.KQ_EV_DELETE)], 0)
        except (OSError, AttributeError):
            pass
        self._kill_request()            # (the signal first; its diagnostic after)
        self.diag("EOF on the command pipe: stopping the group (TERM now, KILL after the grace, repeated)")

    # -------- the launch: the handshake, establishment and exec

    def _pump_handshake(self) -> None:
        """Read what the child said (non-blocking): `S` once its setsid() is done, `F<stage> <errno>` if it gave up; EOF
        at its exec or its death."""
        if self.hs_fd is None or self.hs_deferred:
            return
        while True:
            try:
                chunk = os.read(self.hs_fd, 256)
            except BlockingIOError:
                return
            except OSError:
                chunk = b""
            if not chunk:
                try:
                    self.kq.control([select.kevent(self.hs_fd, select.KQ_FILTER_READ, select.KQ_EV_DELETE)], 0)
                except (OSError, AttributeError):
                    pass
                os.close(self.hs_fd)
                self.hs_fd, self.hs_eof = None, True
                self._settle_start()
                return
            self.hs_said += chunk
            if not self.established and self.hs_said.startswith(b"S"):
                self._established()

    def _established(self) -> None:
        """GROUP ESTABLISHED: the child leads its own group (and session) now. Every signal from here goes to the group;
        each signal already sent to the pid alone is sent to the group once (members may exist by now). The replay
        changes no clock: the grace still runs from the first TERM attempt, and a KILL is replayed only if one was sent."""
        self.established = True
        self.diag(f"the child's group {self.pgid} is established")
        for sig in list(self.pid_only):
            self._signal(sig)
        self.pid_only.clear()

    def _exec_seen(self) -> None:
        if not self.exec_confirmed:
            self.exec_confirmed = True
            self._settle_start()

    def _settle_start(self) -> None:
        """The launch's outcome, said once:
        - STARTED at EXEC CONFIRMED (NOTE_EXEC; what today's callers have once Popen returns: the CLI runs);
        - START_FAILED only on POSITIVE evidence that the exec did not happen: the child's own `F<stage> <errno>`
          report (`START_FAILED <errno>`), or its death while RELEASE was never written (`START_FAILED 0`: it could not
          exec);
        - START_UNKNOWN when the child, RELEASED, ended with neither a NOTE_EXEC seen nor a failure report (the kqueue
          failed and the loop polled, for instance): it may have exec'd, run, and touched its checkout. Never read as
          "never ran". Cleanup and the marker proof do not depend on which of the three it is."""
        if self.start_said:
            return
        failed = self.hs_said[1:] if self.hs_said.startswith(b"S") else self.hs_said
        if self.exec_confirmed:
            self.start_said = True
            self.emit(f"STARTED {self.pgid}")
        elif self.hs_eof and failed.startswith(b"F"):
            stage, _, err = failed[1:].strip().partition(b" ")
            self.start_said = True
            self.emit(f"START_FAILED {int(err) if err.isdigit() else 0}")
            self.diag(f"the CLI did not start: {stage.decode('ascii', 'replace')} failed (errno "
                      f"{err.decode('ascii', 'replace')})")
        elif self.hs_eof and self.leader_exited and not self.released:
            self.start_said = True
            self.emit("START_FAILED 0")
            self.diag("the child died before RELEASE was written: it never exec'd")
        elif self.hs_eof and self.leader_exited:
            self.start_said = True
            self.emit("START_UNKNOWN")
            self.diag("the child ended, released, with no exec seen and no failure said: its start is unknown")

    # -------- signals: only while the anchor holds

    def anchored(self) -> bool:
        """The anchor holds: the child was forked, is NOT reaped (its zombie pins its pid and so its pgid), and signalling
        is not latched off. The one predicate every signal asks, right before its system call."""
        return self.pgid is not None and not self.reaped and not self.latched

    def _signal(self, sig: int) -> bool:
        """The one signal sender. Its target: the child's GROUP once establishment is observed (a group led by our
        unreaped child: no other group can have its id); before that, the child's PID (our unreaped child: no other
        process can have it; `killpg` would be wrong there, since the group may not exist yet). A signal sent to the pid
        alone is remembered and replayed to the group at establishment (`_established`)."""
        name = SIGNAMES[sig]
        if not self.anchored():
            self.emit(f"REFUSED {name}")
            return False
        group = self.established
        self.gate("signal", name)       # (test-only: between the decision and the real system call)
        at = self.clock()
        if sig == signal.SIGTERM and self.first_term_at is None:
            self.first_term_at = at     # the ATTEMPT starts the grace, whatever it answers
        scope = "group" if group else "pid"
        call = f"{'killpg' if group else 'kill'}({self.pgid}, {name})"
        try:
            if group:
                self.killpg(self.pgid, sig)
            else:
                self.kill(self.pgid, sig)
        except OSError as e:
            self.emit(f"FAILED {name} {e.errno} {scope}")
            self.diag(f"{call} failed (errno {e.errno}; attempted at {at:.6f})")
        else:
            self.emit(f"SENT {name} {scope}")       # attempted successfully: never a claim that anything exited
            self.diag(f"{call} sent (attempted at {at:.6f})")   # (the clock the grace is counted on)
        if not group and sig not in self.pid_only:
            self.pid_only.append(sig)
        if self.leader_exited:
            self.check_due, self.check_step = self.clock(), CHECK_FIRST
        return True

    def _kill_request(self) -> None:
        """A KILL command, or EOF: TERM now if none was attempted yet; KILL at the first TERM attempt + the grace, then
        every grace while the anchor holds (`_kill_now`)."""
        if self.first_term_at is None:
            self._signal(signal.SIGTERM)
            if self.first_term_at is None:
                return          # (no anchor: nothing to escalate)
        due = self.first_term_at + self.grace
        if self.clock() >= due:
            self._kill_now()
        else:
            self.kill_due = due if self.kill_due is None else min(self.kill_due, due)
            self.emit(f"DEFERRED KILL {max(0, int((due - self.clock()) * 1000))}")

    def _kill_now(self) -> None:
        """THE only KILL sender (but for the replay). Never before the first TERM attempt + the grace. (C3-2b-1) KILL
        repetition has ONE owner, the shim: once it has sent a KILL, asked by a `KILL` command or by EOF, it repeats
        KILL every grace for as long as the anchor holds (`_signal` refuses, and the timer ends, once it does not). The
        node never retries a KILL command that was queued."""
        now = self.clock()
        if self.first_term_at is None or now < self.first_term_at + self.grace:
            self.kill_due = None if self.first_term_at is None else self.first_term_at + self.grace
            return
        self.kill_due = None
        if self._signal(signal.SIGKILL):
            self.kill_due = self.clock() + self.grace

    # -------- the leader's exit, and the proof

    def _leader_exit(self, ev) -> None:
        if self.leader_exited:
            return
        self._pump_handshake()          # (what it said before it died comes first: START_FAILED's errno)
        code = None
        if ev is not None and ev.fflags & NOTE_EXITSTATUS:
            code = decode_status(ev.data)
        seen = self._exited()       # (the self-check: the status of our unreaped child, without reaping)
        if seen is not None and seen != code:
            if code is not None:
                self.diag(f"kqueue's exit status {code} disagrees with waitid's {seen}: waitid's is taken")
            code = seen
        self.watching = False
        self.leader_exited = True
        self.leader_code = code
        self._settle_start()
        if code is not None:
            self.emit(f"LEADER_EXIT {code}")
        self.check_due, self.check_step = self.clock(), CHECK_FIRST

    def _check_empty(self) -> int | None:
        """The proof. An exit status when the shim is done (EMPTY or UNPROVED), None to keep waiting."""
        self.check_due = None
        if not self.leader_exited or self.reaped:
            return None
        members = self._members()
        # Only a COMPLETE, successful listing (None: failed, malformed or possibly truncated) holding nothing but the
        # exited, unreaped leader. (Empty would mean the child never formed the group: no group by that id can exist
        # while the child's pid is pinned.)
        if members is None or not set(members) <= {self.pgid} or self._exited() is None:
            self.check_due = self.clock() + self.check_step
            self.check_step = min(CHECK_MAX, self.check_step * 2)
            return None
        self.latched = True             # signalling OFF, for good, BEFORE the anchor goes
        self._reap()
        after = self._members()
        if after is None or after != ():
            self.emit("UNPROVED")
            self.diag(f"after the reap the group is not proved absent ({after!r}): no marker, no more signals")
            return EXIT_UNPROVED
        failed = self.publish("empty")
        if failed == EXIT_MARKER_FAILED:
            return failed
        self.emit("EMPTY")
        return EXIT_EMPTY if failed is None else failed

    def _reap(self) -> None:
        try:
            _, status = self.waitpid(self.pgid, 0)
            code = decode_status(status)
            if self.leader_code is None and code is not None:
                self.leader_code = code
                self.emit(f"LEADER_EXIT {code}")
        except OSError as e:
            # (ECHILD: already reaped, it is gone either way. Any other failure: signalling is latched off already and
            # the listing that follows decides; a zombie still listed there is UNPROVED.)
            self.diag(f"the reap answered errno {e.errno}")
        self.reaped = True
        self.diag(f"reaped the CLI, pid {self.pgid}")


_GATE_MEMBER = ("import os, signal, sys, time\n"
                "path, gate, wait = sys.argv[1], sys.argv[2], float(sys.argv[3])\n"
                "signal.signal(signal.SIGTERM, lambda *_: open(path, 'w').close())\n"
                "open(path + '.ready', 'w').close()\n"
                "end = time.monotonic() + wait\n"
                "while time.monotonic() < end and not os.path.exists(os.path.join(gate, 'abort')):\n"
                "    time.sleep(0.01)\n")


def _child_gate(gate_dir: str | None, name: str, member_spec: list) -> None:
    """TEST-ONLY, in the child (`--test-gate DIR` and a `pause.<name>` file there; the node never passes either): pause
    at `name`, announcing `<name>.at` (its pid, and a member's), until `<name>.go` (bounded). The pause file may ask to
    ignore SIGTERM, to start a member in the child's group (which notes a TERM in `member_term_file`; started with
    `close_fds=True`, so the shim's one fork stays the package's only one, C3-2a F6), to exit there (`exit`), or only
    to announce (`wait: false`)."""
    if gate_dir is None:
        return
    pause = os.path.join(gate_dir, f"pause.{name}")
    if not os.path.exists(pause):
        return
    with open(pause) as f:
        opts = json.loads(f.read() or "{}")
    if opts.get("ignore_term"):
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    member = None
    if opts.get("member_term_file"):
        import subprocess
        member = subprocess.Popen([sys.executable, "-I", "-c", _GATE_MEMBER, opts["member_term_file"], gate_dir,
                                   str(GATE_WAIT)], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL, close_fds=True).pid
        member_spec.append(member)
    at = os.path.join(gate_dir, f"{name}.at")
    with open(at + ".tmp", "w") as f:
        f.write(json.dumps({"pid": os.getpid(), "member": member, "cwd": os.getcwd()}))
    os.rename(at + ".tmp", at)
    if "exit" in opts:
        os._exit(int(opts["exit"]))
    if opts.get("wait") is False:
        return
    end = time.monotonic() + GATE_WAIT
    while time.monotonic() < end and not os.path.exists(os.path.join(gate_dir, f"{name}.go")):
        time.sleep(0.005)


def _child(spec: dict, stdio: tuple[int, int, int], hs_w: int, rel_r: int, ignored: frozenset,
           gate_dir: str | None, shims_ends: tuple[int, ...] = ()) -> None:
    """In the forked child: become the CLI, or say why not on the handshake pipe and `_exit(127)`. `shims_ends`: the
    shim's ends of the two pipes (the handshake's read end and RELEASE's write end), closed first: while the child
    held its copy of RELEASE's write end, a shim gone before writing it would never give it EOF (found in review
    round 2). (C3-2a, F5) Every statement, from the first, is inside the one `try`; every way out is `os._exit` in its
    `finally`, never a Python exit handler (tests pin it by AST)."""
    try:
        stage = "close"
        for fd in shims_ends:
            _close(fd)
        stage = "setsid"
        _child_gate(gate_dir, "before-setsid", [])
        os.setsid()
        os.write(hs_w, b"S")
        _child_gate(gate_dir, "after-setsid", [])
        stage = "release"
        # RELEASE, before any checkout work (review round 2, R5): the shim writes it once our exec and exit are
        # watched. At EOF (the shim never wrote it: refused, or gone before it) we never enter the checkout.
        if os.read(rel_r, 1) != b"G":
            raise OSError(0, "not released")
        os.close(rel_r)
        _child_gate(gate_dir, "after-release", [])
        stage = "signals"
        for s in (signal.SIGPIPE, signal.SIGXFSZ):      # what Popen's restore_signals resets (Python ignores them)
            signal.signal(s, signal.SIG_DFL)
        for s in ignored:                               # ignored when the node started the shim: ignored for a direct child
            signal.signal(s, signal.SIG_IGN)
        stage = "stdio"
        for target, fd in enumerate(stdio):
            os.dup2(fd, target)
        stage = "fds"
        for name in os.listdir("/dev/fd"):
            fd = int(name)
            if fd > 2 and fd != hs_w:
                try:
                    os.close(fd)
                except OSError:
                    pass
        stage = "chdir"
        os.chdir(spec["cwd"])
        _child_gate(gate_dir, "after-chdir", [])
        stage = "exec"
        os.execvpe(spec["argv"][0], spec["argv"], spec["env"])
    except OSError as e:
        try:
            os.write(hs_w, b"F%s %d\n" % (stage.encode(), e.errno or 0))
        except OSError:
            pass
    except BaseException:
        try:
            os.write(hs_w, b"F%s 0\n" % stage.encode())
        except OSError:
            pass
    finally:
        os._exit(127)


# ---------------------------------------------------------------- the process

# (C3-2a) No secret among them: the launch secret arrives only in the NONCE frame on the command pipe.
USAGE_KEYS = {"--version", "--boot", "--marker", "--cmd-fd", "--event-fd", "--log", "--test-gate"}


def parse_args(argv: list[str]) -> dict | None:
    """`--key value` pairs, all required but `--test-gate`; None if anything is off (no argparse: start-up time)."""
    if len(argv) % 2:
        return None
    args = dict(zip(argv[::2], argv[1::2]))
    if len(args) != len(argv) // 2 or not set(args) <= USAGE_KEYS or not USAGE_KEYS - {"--test-gate"} <= set(args):
        return None
    try:
        version, cmd_fd, event_fd = int(args["--version"]), int(args["--cmd-fd"]), int(args["--event-fd"])
    except ValueError:
        return None
    boot, marker, log = args["--boot"], args["--marker"], args["--log"]
    if version != PROTOCOL or cmd_fd < 3 or event_fd < 3 or cmd_fd == event_fd or not valid_boot(boot):
        return None
    if not (os.path.isabs(marker) and os.path.isdir(os.path.dirname(marker)) and os.path.isabs(log)):
        return None
    gate = args.get("--test-gate")
    if gate is not None and not os.path.isdir(gate):
        return None
    return {"boot": boot, "marker": marker, "cmd_fd": cmd_fd, "event_fd": event_fd, "log": log, "gate": gate}


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    if args is None:
        return EXIT_USAGE
    gate = args["gate"]
    sink = Diag(args["log"], time.monotonic,
                fail_start=gate is not None and os.path.exists(os.path.join(gate, "fail-diag-start")))
    try:
        return _main(args, sink)
    finally:
        sink.flush()


def _main(args: dict, sink: Diag) -> int:
    # The CLI's future stdio, put aside (close-on-exec copies); the shim's own 0/1/2 become /dev/null, so nothing it
    # (or Python) says ever reaches the CLI's stdio, and nothing the loop does ever waits on a log.
    stdio = (os.dup(0), os.dup(1), os.dup(2))
    devnull = os.open(os.devnull, os.O_RDWR)
    for fd in (0, 1, 2):
        os.dup2(devnull, fd)
    os.close(devnull)
    shim = Shim(key=None, boot=args["boot"], marker=args["marker"], cmd_fd=args["cmd_fd"],
                event_fd=args["event_fd"], sink=sink, gate=args["gate"])
    try:
        for fd in (shim.cmd_fd, shim.event_fd):
            os.fstat(fd)
            os.set_inheritable(fd, False)
            os.set_blocking(fd, False)
    except OSError as e:
        shim.diag(f"a control fd is not usable (errno {e.errno}): exit")
        return EXIT_USAGE
    if os.getsid(0) != os.getpid():
        os.setsid()
    own = None if shim._gate_file("fail-boot") else boot_id()      # (fail-boot: TEST-ONLY, an unreadable identity)
    if own is None or args["boot"] != own or _libc() is None:
        shim.diag("the boot identity given is not this boot's, or this boot's cannot be read (or waitid is missing): "
                  "exit, nothing started, nothing published")
        return EXIT_USAGE
    # The dispositions a direct Popen child would get: what was ignored when the node started the shim (Popen's
    # restore_signals resets SIGPIPE and SIGXFSZ, which Python then ignores again in the shim). The shim itself keeps
    # SIGCHLD at its default: a SIGCHLD ignored IN THIS PROCESS would have the kernel reap the CLI behind its back, and
    # nothing would anchor. (Measured on macOS: an ignore merely inherited across exec still leaves zombies, and the
    # shim sets none itself, so this reset is defence in depth; its mutation row is equivalent, DESIGN_NOTES C3-1.)
    ignored = frozenset(s for s in signal.Signals if s not in (signal.SIGKILL, signal.SIGSTOP, signal.SIGPIPE,
                                                               signal.SIGXFSZ)
                        and signal.getsignal(s) == signal.SIG_IGN)
    if signal.SIGCHLD in ignored:
        signal.signal(signal.SIGCHLD, signal.SIG_DFL)
    shim.diag(f"ready (protocol {PROTOCOL}, boot {shim.boot})")     # (C3-2a: no secret in any diagnostic)
    try:
        try:
            shim.read_secret()
            shim.diag("the launch secret is received")
            if shim._gate_file("crash-before-go"):      # (TEST-ONLY: a crash before the fork)
                raise RuntimeError("injected (test-only): a crash before the fork")
            spec = shim.read_go()
        except (NoSecret, NoGo) as e:
            for fd in stdio:
                os.close(fd)
            if isinstance(e, NoSecret):
                return shim.no_secret(e.reason)
            return shim.never_started(e.reason)
        shim.diag(f"GO: argv[0] {spec['argv'][0]!r}, cwd {spec['cwd']!r}, grace {spec['kill_grace']}")
        done = shim.spawn(spec, stdio, ignored)
        if done is not None:
            return done
        code = shim.run()
    except BaseException as e:
        if shim.pgid is None:
            shim.diag_crash("crashed before the fork, no marker", e)
            return EXIT_CRASH
        # (C3-2a, F2; review round 1, R2) After the fork a crash must still stop the group, and NOTHING runs before the
        # degraded stop: no traceback formatting, no source lookup. It describes the crash itself, after its TERM.
        code = shim.stop_after_crash(e)
    if shim.dropped:
        shim.diag(f"events dropped: {shim.dropped} (after EOF: {shim.dropped_after_eof})")
    if sink.dropped:
        shim.diag(f"diagnostic lines dropped: {sink.dropped}")
    shim.diag(f"exit {code}")
    return code


def entry(argv: list[str]) -> None:
    """The process: `main`, then `os._exit` (never a wait on the diagnostics writer beyond DIAG_FLUSH)."""
    try:
        code = main(argv)
    except BaseException:
        code = EXIT_CRASH
    os._exit(code)


if __name__ == "__main__":
    entry(sys.argv[1:])
