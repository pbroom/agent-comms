"""(Ownership C3-1) The per-group supervisor ("the shim"): ONE small process per process group the node starts, run as
`python -I -m agent_comms.groupshim <args>` (agent_comms/shimchild.py starts it). NOT wired into any spawn site yet
(C3-2 does that); DESIGN_NOTES "Ownership C3-1: the group supervisor (unwired)" and its "code review round 1" have the
protocol as tables.

What it is for. The node used to signal a process group by number: between a probe and a `killpg`, the last member can
exit and the group id be reused, and after a node crash nobody stops the orphans or can prove a checkout free. The shim
is the group's ANCHOR: it is the CLI's parent and keeps the CLI's zombie unreaped until the group is proved empty, so
the CLI's pid, and so its pgid, cannot be reused while it may still signal. It stops the group when the node dies (EOF
on its command pipe). It leaves a proof (the marker) when the group is gone, or when none was ever made.

The life of one shim:
1. Arguments, before anything durable exists: the launch nonce, the boot identity the node read, the marker path, the
   protocol version, the two control fds and its own diagnostics log. Any bad argument: exit 2, nothing published.
2. `setsid()`. fd 0/1/2 are the CLI's future stdio: they are put aside (close-on-exec copies) and replaced by /dev/null.
   The control fds are made close-on-exec.
3. `GO`: one bounded, newline-terminated, versioned frame, `GO 1 <json>\\n`. Anything else (EOF first, a partial frame
   then EOF, an over-long, unparseable or unknown-version frame) never starts the CLI: the bound marker is published
   (`never_started`) and the shim exits 3.
4. Spawn: fork. From the fork on, everything is in the single event loop: the command pipe, the launch handshake and
   the child's exit. The child calls `setsid()` and says so (GROUP ESTABLISHED), restores a direct `Popen` child's
   signal dispositions, takes back fds 0/1/2, closes every other fd, `chdir`s, waits for the shim's release byte (sent
   only once its exit and exec are watched) and `execvpe`s (EXEC CONFIRMED: kqueue NOTE_EXEC). Nothing reaps it early.
5. Watch: kqueue `EVFILT_PROC` `NOTE_EXIT | NOTE_EXITSTATUS | NOTE_EXEC`, never reaping: the zombie pins the pgid.
6. `TERM` / `KILL`, from the fork on, only while the anchor holds (the child unreaped, signalling not latched off): to
   the child's pid until its group is observed established, to its group after (and then any pid-only TERM or KILL is
   replayed to the group once). A KILL is sent no earlier than `kill_grace` after the shim's first TERM attempt.
7. EOF on the command pipe (the node died or let go): TERM at once, KILL at first TERM + `kill_grace`, KILL again every
   `kill_grace` while the anchor holds. Neither event writes nor diagnostics can block or abort this.
8. Empty: a complete listing (`node_groups.group_members`) holding nothing but the exited, unreaped leader; then latch
   signalling OFF, reap, list again and require ABSENT; then publish the marker atomically, `EMPTY`, exit 0. Anything
   else after the reap: no marker, `UNPROVED`, exit 1.
9. If the shim itself is killed, launchd reaps the CLI's zombie and the anchor is lost: nothing is published (held, for
   a human). Descendants that leave the group (`setsid`/`setpgid`) are outside group-based ownership.

It never opens the board, reads config or imports the session host, and it never logs through `logging`. Its
diagnostics are OFF the escalation path: the loop only appends lines to a bounded in-memory queue; one writer thread,
started only after the single fork, alone opens and writes the log (`Diag`).
"""
from __future__ import annotations

import ctypes
import json
import os
import select
import signal
import sys
import threading
import time

from . import node_groups

PROTOCOL = 1
# <sys/event.h>: ask EVFILT_PROC's NOTE_EXIT to carry the wait(2) status in `data`. Python 3.12's select module does not
# name it. Pinned HERE only, with its self-check: the kernel echoes the flag in the event's fflags when it filled `data`
# (measured on this Mac: fflags 0x84000000), and the decoded status is cross-checked against `waitid(WNOWAIT)` at every
# leader exit (`Shim._leader_exit`), which wins on any disagreement.
NOTE_EXITSTATUS = 0x04000000
assert NOTE_EXITSTATUS & select.KQ_NOTE_EXIT == 0 and NOTE_EXITSTATUS == 1 << 26
PROC_FFLAGS = select.KQ_NOTE_EXIT | NOTE_EXITSTATUS | select.KQ_NOTE_EXEC

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
EXIT_CRASH = 5          # an unexpected exception (its traceback is in the log): no marker
EXIT_UNSYNCED = 6       # the marker is published and visible, but its directory's sync failed: not known durable

SIGNAMES = {signal.SIGTERM: "TERM", signal.SIGKILL: "KILL"}


# ---------------------------------------------------------------- small system helpers (no logging, no side effects)

class _Timeval(ctypes.Structure):
    _fields_ = [("sec", ctypes.c_long), ("usec", ctypes.c_int32)]


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


def boot_id() -> str | None:
    """This boot's identity: `kern.boottime` (seconds.microseconds), or None if it cannot be read. A pid or a start time
    from another boot proves nothing; a marker carries the boot it was made on."""
    lib = _libc()
    if lib is None or sys.platform != "darwin":
        return None
    tv = _Timeval()
    size = ctypes.c_size_t(ctypes.sizeof(tv))
    try:
        if lib.sysctlbyname(b"kern.boottime", ctypes.byref(tv), ctypes.byref(size), None, 0) != 0:
            return None
    except (AttributeError, OSError):
        return None
    if tv.sec <= 0 or not 0 <= tv.usec < 1_000_000:
        return None
    return f"{tv.sec}.{tv.usec:06d}"


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


def write_marker(path: str, record: dict) -> tuple[str, int]:
    """Publish `record` at `path` atomically: a temp file in the destination directory (exclusive, no symlink), its
    sync, the rename, the directory's sync. (outcome, errno):
    - PUBLISHED, 0: visible and synced (as durable as this machine's F_FULLFSYNC makes it);
    - NOT_PUBLISHED, errno: a failure up to and including the rename (the temp file's write or sync among them): the
      temp file is removed and NOTHING is visible at `path`;
    - UNSYNCED, errno: the rename happened, so the complete marker is VISIBLE (and valid proof of absence for a living
      node), but the directory's sync failed: whether it survives a crash is not known. A marker lost in a crash leaves
      the checkout held at the next start (C3-3), the conservative direction."""
    tmp = f"{path}.{os.getpid()}.tmp"
    data = (json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n").encode()
    fd = -1
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view):]
        _fullsync(fd)
        os.close(fd)
        fd = -1
        os.rename(tmp, path)
    except OSError as e:
        if fd >= 0:
            os.close(fd)
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return NOT_PUBLISHED, e.errno or -1
    try:
        dfd = os.open(os.path.dirname(path) or ".", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            _fullsync(dfd)
        finally:
            os.close(dfd)
    except OSError as e:
        return UNSYNCED, e.errno or -1
    return PUBLISHED, 0


# ---------------------------------------------------------------- diagnostics, off the escalation path

class Diag:
    """The shim's diagnostics. `put` only appends to a bounded in-memory list (full: the line is dropped and counted)
    and never opens, writes or waits on the log. One daemon thread alone opens the log and writes it; a log that blocks
    (a FIFO nobody reads, a stalled filesystem) stalls only that thread. The thread is started only after the shim's
    single fork (or at its exit, if it never forks), so the fork is made single-threaded. The exit waits for it at most
    DIAG_FLUSH seconds."""

    def __init__(self, path: str | None, clock):
        self.path, self.clock = path, clock
        self.cond = threading.Condition(threading.Lock())
        self.lines: list[str] = []
        self.dropped = 0
        self.busy = False
        self.thread: threading.Thread | None = None

    def put(self, msg: str) -> None:
        line = f"{self.clock():.6f} shim {os.getpid()} {msg}\n"
        with self.cond:         # (the writer holds this lock only to swap the list, never across I/O)
            if len(self.lines) >= DIAG_CAPACITY:
                self.dropped += 1
                return
            self.lines.append(line)
            self.cond.notify()

    def start(self) -> None:
        if self.thread is None and self.path is not None:
            self.thread = threading.Thread(target=self._run, name="shim-diag", daemon=True)
            self.thread.start()

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
            data = "".join(batch).encode("utf-8", "replace")
            try:
                while data:
                    data = data[os.write(fd, data):]
            except OSError:
                pass

    def flush(self, bound: float = DIAG_FLUSH) -> None:
        self.start()
        deadline = time.monotonic() + bound
        while time.monotonic() < deadline:
            with self.cond:
                if not self.lines and not self.busy:
                    return
            time.sleep(0.005)


# ---------------------------------------------------------------- the GO frame

class NoGo(Exception):
    """The GO frame was not a valid one: `reason` is eof, partial, overlong, invalid or version."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


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

    def __init__(self, *, nonce: str, boot: str, marker: str, cmd_fd: int, event_fd: int | None,
                 sink: Diag | None = None, gate: str | None = None):
        self.nonce, self.boot, self.marker = nonce, boot, marker
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
        self.start_said = False         # STARTED or START_FAILED sent
        self.pid_only: list[int] = []   # signals sent to the pid before establishment was observed (replayed once)
        self.hs_fd: int | None = None   # the handshake's read end, non-blocking, read in the loop
        self.hs_said = b""
        self.hs_eof = False
        self.hs_deferred = False        # (test-only: `defer-handshake` in the gate dir)
        self.release_fd: int | None = None
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

    # -------- diagnostics and events

    def diag(self, msg: str) -> None:
        """A diagnostic line: queued, never written here (`Diag`); it cannot raise into, wait in or reorder the loop."""
        if self.sink is None:
            return
        try:
            self.sink.put(msg)
        except Exception:
            pass

    def emit(self, line: str) -> None:
        """One event to the node: non-blocking and atomic (shorter than PIPE_BUF). A full pipe drops it; a closed one
        (EPIPE, the node is gone) stops events for good. Neither ever delays or aborts what the shim does next: the
        marker, not an event, is the proof."""
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
        if self.gate_dir is None or not os.path.exists(os.path.join(self.gate_dir, "numbered")):
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
        """Publish the bound marker. None when it is published and synced; else the exit status to end with:
        EXIT_MARKER_FAILED (nothing visible) or EXIT_UNSYNCED (visible, valid, its durability unknown)."""
        record = {"v": PROTOCOL, "nonce": self.nonce, "boot": self.boot, "outcome": outcome,
                  "cli_pid": self.pgid, "leader_exit": self.leader_code}
        result, err = write_marker(self.marker, record)
        if result == NOT_PUBLISHED:
            self.emit(f"MARKER_FAILED {err}")
            self.diag(f"the marker could not be published (errno {err}): no proof")
            return EXIT_MARKER_FAILED
        if result == UNSYNCED:
            self.emit(f"UNSYNCED {err}")
            self.diag(f"marker published ({outcome}) and visible, but its directory's sync failed (errno {err})")
            return EXIT_UNSYNCED
        self.diag(f"marker published: {outcome}")
        return None

    def never_started(self, reason: str) -> int:
        """Every outcome in which the CLI was never forked: the bound marker (the group provably never existed)."""
        self.emit(f"NOGO {reason}")
        failed = self.publish("never_started")
        if failed == EXIT_MARKER_FAILED:
            return failed
        self.emit("EMPTY")
        return EXIT_NOGO if failed is None else failed

    # -------- GO

    def read_go(self) -> dict:
        """Block (kqueue on the command fd) until one complete GO line; NoGo otherwise. Bytes after it stay buffered as
        commands."""
        kq = select.kqueue()
        try:
            kq.control([select.kevent(self.cmd_fd, select.KQ_FILTER_READ, select.KQ_EV_ADD)], 0)
            while True:
                nl = self.buf.find(b"\n")
                if nl >= 0:
                    if nl + 1 > MAX_GO:
                        raise NoGo("overlong")
                    line, self.buf = self.buf[:nl], self.buf[nl + 1:]
                    return parse_go(line)
                if len(self.buf) >= MAX_GO:
                    raise NoGo("overlong")
                chunk = self._read_cmd()
                if chunk is None:
                    kq.control(None, 1, None)
                    continue
                if not chunk:
                    self.eof = True
                    raise NoGo("partial" if self.buf else "eof")
                self.buf += chunk
        finally:
            kq.close()

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
        the loop (`run`). An exit status when the child was never forked (fork failed)."""
        self.grace = spec["kill_grace"]
        hs_r, hs_w = os.pipe()          # (close-on-exec: EOF at the child's exec or death)
        rel_r, rel_w = os.pipe()        # the release byte: the child execs only after the shim watches it
        try:
            pid = os.fork()
        except OSError as e:
            for fd in (hs_r, hs_w, rel_r, rel_w, *stdio):
                os.close(fd)
            self.emit(f"START_FAILED {e.errno}")
            self.diag(f"fork failed (errno {e.errno})")
            return self.never_started("fork")
        if pid == 0:
            _child(spec, stdio, hs_w, rel_r, ignored, self.gate_dir)      # never returns
        for fd in (hs_w, rel_r, *stdio):
            os.close(fd)                # the CLI's stdio is the CLI's alone now
        self.pgid, self.hs_fd, self.release_fd = pid, hs_r, rel_w
        os.set_blocking(hs_r, False)
        if self.sink is not None:
            self.sink.start()           # (after the one fork: the fork was single-threaded)
        self.kq = select.kqueue()
        self.gate("register", str(pid))
        try:
            self.kq.control([select.kevent(pid, select.KQ_FILTER_PROC, select.KQ_EV_ADD | select.KQ_EV_CLEAR,
                                           PROC_FFLAGS)], 0)
            self.watching = True
        except OSError as e:
            # ESRCH: it is already a zombie (it cannot be gone: nothing reaped it); it never got the release byte, so it
            # never exec'd. Its status, without reaping.
            self.diag(f"exit registration refused (errno {e.errno}): the child exited before it")
        if self.watching:
            try:
                os.write(rel_w, b"G")       # the child may exec now: its exec and exit are watched
            except OSError:
                pass
        os.close(rel_w)                 # (unwatched: EOF instead, and the child never execs)
        self.release_fd = None
        self.diag(f"forked the child, pid {pid}")
        self.hs_deferred = self.gate_dir is not None and os.path.exists(os.path.join(self.gate_dir, "defer-handshake"))
        if not self.hs_deferred:
            self.kq.control([select.kevent(hs_r, select.KQ_FILTER_READ, select.KQ_EV_ADD)], 0)
        if not self.watching:
            self._leader_exit(None)
        return None

    # -------- the loop

    def run(self) -> int:
        assert self.kq is not None and self.pgid is not None
        if not self.eof:
            self.kq.control([select.kevent(self.cmd_fd, select.KQ_FILTER_READ, select.KQ_EV_ADD)], 0)
        self._take_commands()           # (anything sent behind GO)
        while True:
            if self.hs_deferred and os.path.exists(os.path.join(self.gate_dir, "defer-handshake.go")):
                self.hs_deferred = False
                self.kq.control([select.kevent(self.hs_fd, select.KQ_FILTER_READ, select.KQ_EV_ADD)], 0)
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
            if self.hs_deferred:
                timeout = 0.05 if timeout is None else min(timeout, 0.05)
            for ev in self.kq.control(None, 8, timeout):
                if ev.filter == select.KQ_FILTER_PROC:
                    if ev.fflags & select.KQ_NOTE_EXEC:
                        self._exec_seen()
                    if ev.fflags & select.KQ_NOTE_EXIT:
                        self._leader_exit(ev)
                elif ev.filter == select.KQ_FILTER_READ and ev.ident == self.cmd_fd:
                    self._pump_commands()
                elif ev.filter == select.KQ_FILTER_READ and ev.ident == self.hs_fd:
                    self._pump_handshake()

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
        """STARTED at EXEC CONFIRMED (what today's callers have once Popen returns: the CLI runs); START_FAILED with the
        child's errno when it gave up (exec, chdir...), or 0 when it died before exec without saying why."""
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
        elif self.hs_eof and self.leader_exited:
            self.start_said = True
            self.emit("START_FAILED 0")
            self.diag("the child died before its exec")

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
        """A KILL command, or EOF: TERM now if none was attempted yet; KILL at the first TERM attempt + the grace."""
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
        """THE only KILL sender (but for the replay). Never before the first TERM attempt + the grace; after EOF,
        repeated every grace."""
        now = self.clock()
        if self.first_term_at is None or now < self.first_term_at + self.grace:
            self.kill_due = None if self.first_term_at is None else self.first_term_at + self.grace
            return
        self.kill_due = None
        if self._signal(signal.SIGKILL) and self.eof:
            self.kill_due = self.clock() + self.grace

    # -------- the leader's exit, and the proof

    def _leader_exit(self, ev) -> None:
        if self.leader_exited:
            return
        self._pump_handshake()          # (what it said before it died comes first: START_FAILED's errno)
        code = None
        if ev is not None and ev.fflags & NOTE_EXITSTATUS:
            code = decode_status(ev.data)
        seen = self.waitid(self.pgid)       # (the self-check: the status of our unreaped child, without reaping)
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
        members = self.members(self.pgid)
        # Only a COMPLETE, successful listing (None: failed, malformed or possibly truncated) holding nothing but the
        # exited, unreaped leader. (Empty would mean the child never formed the group: no group by that id can exist
        # while the child's pid is pinned.)
        if members is None or not set(members) <= {self.pgid} or self.waitid(self.pgid) is None:
            self.check_due = self.clock() + self.check_step
            self.check_step = min(CHECK_MAX, self.check_step * 2)
            return None
        self.latched = True             # signalling OFF, for good, BEFORE the anchor goes
        self._reap()
        after = self.members(self.pgid)
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
        except ChildProcessError:
            self.diag("the CLI was already reaped (it is gone either way)")
        self.reaped = True
        self.diag(f"reaped the CLI, pid {self.pgid}")


def _child_gate(gate_dir: str | None, name: str, member_spec: list) -> None:
    """TEST-ONLY, in the child (`--test-gate DIR` and a `pause.<name>` file there; the node never passes either): pause
    at `name`, announcing `<name>.at` (its pid, and a member's), until `<name>.go` (bounded). The pause file may ask to
    ignore SIGTERM and to fork a member (which notes a TERM in `member_term_file`)."""
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
        member = os.fork()
        if member == 0:
            try:
                path = opts["member_term_file"]
                signal.signal(signal.SIGTERM, lambda *_: open(path, "w").close())
                open(path + ".ready", "w").close()
                end = time.monotonic() + GATE_WAIT
                while time.monotonic() < end and not os.path.exists(os.path.join(gate_dir, "abort")):
                    time.sleep(0.01)
            finally:
                os._exit(0)
        member_spec.append(member)
    at = os.path.join(gate_dir, f"{name}.at")
    with open(at + ".tmp", "w") as f:
        f.write(json.dumps({"pid": os.getpid(), "member": member}))
    os.rename(at + ".tmp", at)
    end = time.monotonic() + GATE_WAIT
    while time.monotonic() < end and not os.path.exists(os.path.join(gate_dir, f"{name}.go")):
        time.sleep(0.005)


def _child(spec: dict, stdio: tuple[int, int, int], hs_w: int, rel_r: int, ignored: frozenset,
           gate_dir: str | None) -> None:
    """In the forked child: become the CLI, or say why not on the handshake pipe and `_exit(127)`."""
    stage = "setsid"
    try:
        _child_gate(gate_dir, "before-setsid", [])
        os.setsid()
        os.write(hs_w, b"S")
        _child_gate(gate_dir, "after-setsid", [])
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
            if fd > 2 and fd not in (hs_w, rel_r):
                try:
                    os.close(fd)
                except OSError:
                    pass
        stage = "chdir"
        os.chdir(spec["cwd"])
        stage = "release"
        if os.read(rel_r, 1) != b"G":       # the shim watches us now (or it is gone: then never exec)
            raise OSError(0, "not released")
        os.close(rel_r)
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

USAGE_KEYS = {"--version", "--nonce", "--boot", "--marker", "--cmd-fd", "--event-fd", "--log", "--test-gate"}


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
    nonce, marker, log = args["--nonce"], args["--marker"], args["--log"]
    if version != PROTOCOL or cmd_fd < 3 or event_fd < 3 or cmd_fd == event_fd:
        return None
    if not (8 <= len(nonce) <= 128 and all(c.isascii() and (c.isalnum() or c in "-_") for c in nonce)):
        return None
    if not (os.path.isabs(marker) and os.path.isdir(os.path.dirname(marker)) and os.path.isabs(log)):
        return None
    gate = args.get("--test-gate")
    if gate is not None and not os.path.isdir(gate):
        return None
    return {"nonce": nonce, "boot": args["--boot"], "marker": marker, "cmd_fd": cmd_fd, "event_fd": event_fd,
            "log": log, "gate": gate}


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    if args is None:
        return EXIT_USAGE
    sink = Diag(args["log"], time.monotonic)
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
    shim = Shim(nonce=args["nonce"], boot=args["boot"], marker=args["marker"], cmd_fd=args["cmd_fd"],
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
    if args["boot"] != boot_id() or _libc() is None:
        shim.diag("the boot identity given is not this boot's (or waitid is missing): exit, nothing published")
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
    shim.diag(f"ready (protocol {PROTOCOL}, nonce {shim.nonce}, boot {shim.boot})")
    try:
        try:
            spec = shim.read_go()
        except NoGo as e:
            for fd in stdio:
                os.close(fd)
            return shim.never_started(e.reason)
        shim.diag(f"GO: argv[0] {spec['argv'][0]!r}, cwd {spec['cwd']!r}, grace {spec['kill_grace']}")
        done = shim.spawn(spec, stdio, ignored)
        if done is not None:
            return done
        code = shim.run()
    except Exception:
        import traceback
        shim.diag("crashed, no marker:\n" + traceback.format_exc())
        return EXIT_CRASH
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
