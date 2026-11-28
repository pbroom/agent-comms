"""(Ownership C3-1, C3-2a) The node's handle on one group supervisor (agent_comms/groupshim.py): what a spawn site will
hold in place of the CLI's `Popen` once the wiring PRs wire it in. NOT used by any spawn site yet.

- `ShimChild.start(...)` spawns the shim (cwd "/" by default, outside every checkout; only its two control fds passed
  besides the CLI's future stdio), sends it this launch's secret in one `NONCE` frame on the command pipe (never on its
  argv or in its environment), and returns BEFORE `GO`; `go(...)` writes `GO`. The wiring fsyncs the durable record
  between the two. Until `go`, a `terminate()`/`kill()`/`close()` closes the command pipe, which the shim takes as
  "never start": it publishes its bound marker and exits. The marker's path is drawn afresh for each launch in the
  node's private marker directory (`make_marker_dir`); an existing path is refused, never overwritten.
- It has no thread. Events are pumped, non-blocking, in `poll()`, `empty()` and `lost()`; before a shim exit is
  classified, the event pipe is drained to EOF.
- Three facts, never conflated: the CLI's exit (`poll()`: the `LEADER_EXIT` code, or the exit code the release pair's
  marker carries, or None), the shim's exit (`shim_poll()`: the node's own child, reaped in ONE place, the handle's
  own `waitpid` (`_observe`); STATUS_UNKNOWN if anyone else reaped it; its `Popen` is neutralised and never reaps or
  signals it, review round 1 R3), and the group's
  absence (`empty()`: the RELEASE PAIR, C3-2a: the shim's own terminal exit status, observed by this handle, AND an
  authentic marker (HMAC under the launch secret) whose outcome belongs with that status; never a marker alone, never
  the `EMPTY` event). `empty()` is proof of absence for the living node that reads it; proof after a node restart is
  not decided (DESIGN_NOTES C3-2a). Durability across a crash is the shim's best effort (a directory sync that failed
  is said by `UNSYNCED`): a marker lost in a crash leaves the checkout held at the next start, the conservative
  direction. A LOST shim (`lost()`) leaves `poll()` at None forever: callers must ask `lost()` on their own, before
  they read None as "still running" (every reap and shutdown path). `refused()`: the shim exited before any fork
  (an OBSERVED exit 2 or 7), so nothing was ever started. An UNKNOWN status is never proof and never a refusal: LOST.
- `terminate()`/`kill()` write `TERM`/`KILL`: one non-blocking write each, under the handle's own lock, which also
  guards closing the pipe, so a concurrent retirement cannot close the fd and have its number reused for another
  shim's pipe mid-write. A closed or broken pipe raises `BrokenPipeError`; a full one `BlockingIOError` (both
  `OSError`, the shape today's callers handle for a failed signal).
- The I/O contract (`write`, `read_lines`, `close_stdin`, `close`, `eof`, `bytes_read`) is dispatch.PipedChild's own
  code, over the shim's stdin and stdout pipes, which the CLI inherits as its stdio (a hosted session's shape,
  `spawn_piped`). `piped=False` gives a dispatched run's shape instead (`spawn_process`: stdin /dev/null, stdout and
  stderr to one new log), with no I/O methods.
- `pid` is the SHIM's pid (the node's own unreaped child, so pinned until this handle reaps it: `shim_poll()`, and
  `empty()`, `poll()`, `lost()`, `refused()`, which all observe its status); `cli_pid` is the CLI's, for diagnostics
  only: nothing signals or probes it by number.

It never logs: it reports by return value and exception.
"""
from __future__ import annotations

import errno
import hmac
import json
import os
import secrets
import select
import stat
import subprocess
import sys
import threading
import time

from . import groupshim
from .dispatch import PipedChild

boot_id = groupshim.boot_id
MARKER_MAX = groupshim.MARKER_MAX   # bytes a marker may have (4096; the shim's are about 250): larger is no proof
# The shim runs THIS process's own agent_comms (the directory that holds the package imported here), never whatever
# `-m agent_comms.groupshim` would find on the interpreter's path: `-I` (isolated: no PYTHON* variables, no user site,
# no current directory on the path) and (C3-2b-1, E) `-S` (no `site`: no site-packages and no `.pth` file is
# processed), then the package's own root APPENDED, after the standard library, so a file named like a standard
# library module at the package root is never what the shim imports (the shim imports nothing outside the standard
# library and `agent_comms`). What remains is the owner's (DESIGN_NOTES C3-2b-1): the node's code, virtualenv and
# interpreter are writable by the same user as the supervised CLI.
_PACKAGE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(groupshim.__file__)))
_BOOT = ("import sys; sys.path.append(sys.argv.pop(1)); from agent_comms.groupshim import entry; "
         "entry(sys.argv[1:])")
EVENTS_PER_PUMP = 256 * 1024        # bytes one pump takes at most (non-blocking: never waits)


def wait_writable(fd: int, timeout: float) -> None:
    """Wait, at most `timeout` seconds, for `fd` to take a write. `poll`, not `select`: a node can hold more than
    FD_SETSIZE (1024) descriptors, and `select` refuses any fd at or above it (found by the serial suite)."""
    p = select.poll()
    p.register(fd, select.POLLOUT)
    p.poll(max(0, int(timeout * 1000)))


_MARKER_KEYS = frozenset({"v", "outcome", "cli_pid", "leader_exit", "boot", "mac"})
_HEX = frozenset("0123456789abcdef")
# (C3-2a, F1.4) The release pairs: the shim's own terminal exit status, and the outcome of the authenticated marker
# that belongs with it. Every other pair proves nothing.
PROOF_PAIRS = frozenset({(groupshim.EXIT_EMPTY, "empty"), (groupshim.EXIT_NOGO, "never_started"),
                         (groupshim.EXIT_UNSYNCED, "empty"), (groupshim.EXIT_UNSYNCED, "never_started")})
# The statuses the shim returns only before any fork, with no marker: nothing was ever started.
REFUSED_EXITS = frozenset({groupshim.EXIT_USAGE, groupshim.EXIT_NOSECRET})


def check_marker_dir(path: str) -> None:
    """(C3-2a, F3) Raise unless `path` is a trusted marker directory: a real directory (not a symlink), owned by this
    process's effective user, with no group or other permission bit. Nothing is changed."""
    st = os.lstat(path)
    if stat.S_ISLNK(st.st_mode):
        raise PermissionError(errno.EPERM, "the marker directory is a symlink", path)
    if not stat.S_ISDIR(st.st_mode):
        raise NotADirectoryError(errno.ENOTDIR, "the marker directory is not a directory", path)
    if st.st_uid != os.geteuid():
        raise PermissionError(errno.EPERM, "the marker directory is owned by another user", path)
    if stat.S_IMODE(st.st_mode) & 0o077:
        raise PermissionError(errno.EPERM, "the marker directory is open to its group or to others", path)


def make_marker_dir(path: str) -> str:
    """(C3-2a, F3) The node's marker directory: created mode 0700 if absent (its parent must exist), and in either case
    checked (`check_marker_dir`); an unsafe one is refused, never repaired. Its absolute path."""
    path = os.path.abspath(path)
    try:
        os.mkdir(path, 0o700)
    except FileExistsError:
        pass
    check_marker_dir(path)
    return path


def _new_marker_name() -> str:
    """A fresh marker name for one launch, drawn apart from the launch secret (so neither tells anything of the other)
    and never reused."""
    return f"{secrets.token_hex(16)}.marker"


def read_marker(path: str, key: bytes, boot: str) -> dict | None:
    """THE one marker reader: the marker at `path` when it is an AUTHENTIC record of the launch whose secret is `key`,
    on boot `boot`; else None, never an exception. It is half of the proof only: `ShimChild.empty()` also needs the
    shim's own terminal exit status (`PROOF_PAIRS`).

    (Review round 2, R5) The shim writes its marker in place, with no staging file, so this reader may see a file
    that is incomplete, or one a same-user process substituted. Atomicity comes from the release pair and the MAC, not
    from the filesystem: an incomplete record fails the parse or the MAC; a substituted one fails the MAC; a complete,
    authentic one whose write the shim did not finish to a proof-bearing exit fails the pair.

    It must be a REGULAR file: `lstat` first (no symlink, FIFO, socket, device or directory is opened), then an open with
    O_NONBLOCK | O_NOFOLLOW, and `fstat` of what was opened must be the same regular file (a special file swapped in
    between is opened without waiting, and refused unread). At most MARKER_MAX bytes, one line ending in a newline, a
    JSON object with exactly the shim's keys and exact types: `v` an int (never a bool) equal to MARKER_VERSION; the boot
    exactly this one; the outcome `empty` (with an int `cli_pid` and an int or null `leader_exit`) or `never_started`
    (both null); `mac` 64 lowercase hex digits equal (compared in constant time) to `marker_mac(key, the other
    fields)`, which the reader recomputes. The scope of "never waits": the reader never opens or reads a special file
    and never waits on one; O_NONBLOCK does not bound ordinary filesystem I/O on a regular file (a stalled filesystem
    can still stall the read)."""
    try:
        if type(key) is not bytes or len(key) != groupshim.KEY_BYTES or type(boot) is not str or not boot:
            return None
        st = os.lstat(path)
        if not stat.S_ISREG(st.st_mode) or st.st_size > MARKER_MAX:
            return None
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            got = os.fstat(fd)
            if not stat.S_ISREG(got.st_mode) or (got.st_dev, got.st_ino) != (st.st_dev, st.st_ino):
                return None
            data = os.read(fd, MARKER_MAX + 1)
        finally:
            os.close(fd)
        if len(data) > MARKER_MAX or not data.endswith(b"\n") or data.count(b"\n") != 1:
            return None
        rec = json.loads(data.decode("utf-8"))
        if type(rec) is not dict or set(rec) != _MARKER_KEYS:
            return None
        if type(rec["v"]) is not int or rec["v"] != groupshim.MARKER_VERSION:
            return None
        if type(rec["boot"]) is not str or rec["boot"] != boot:
            return None
        pid, code = rec["cli_pid"], rec["leader_exit"]
        if rec["outcome"] == "empty":
            if type(pid) is not int or pid <= 1 or not (code is None or type(code) is int):
                return None
        elif rec["outcome"] == "never_started":
            if pid is not None or code is not None:
                return None
        else:
            return None
        mac = rec["mac"]
        if type(mac) is not str or len(mac) != 64 or not set(mac) <= _HEX:
            return None
        if not hmac.compare_digest(groupshim.marker_mac(key, rec), mac):
            return None
        return rec
    except Exception:
        return None


def marker_proves(path: str, key: bytes, boot: str) -> bool:
    """Whether `read_marker` accepts the marker at `path` as authentic for this launch and boot. NOT release
    authority on its own: that is `ShimChild.empty()` (the shim's status too)."""
    return read_marker(path, key, boot) is not None


# (C3-2a review round 1, R3) The shim's wait status is the HANDLE's: `ShimChild._observe` is the one place it is reaped.
STATUS_UNKNOWN = "unknown"          # the shim was reaped by someone else (or the kernel): no status was observed


class _HandleOwnsStatus:
    """Put in the shim's `Popen.returncode` at once: `Popen.poll()`, `wait()`, `send_signal()` and `__del__` (and
    subprocess's `_cleanup` of collected Popens) all act only while `returncode` is None, so none of them ever calls
    `waitpid` on the shim, or signals it. Only `ShimChild._observe` reaps it."""

    def __repr__(self) -> str:
        return "<the shim's status is owned by its ShimChild>"


_OWNED = _HandleOwnsStatus()


class ShimChild:
    def __init__(self, proc: subprocess.Popen, cmd_w: int, evt_r: int, *, key: bytes, boot: str, marker: str):
        proc.returncode = _OWNED        # (R3: before anything else can poll it; see `_HandleOwnsStatus`)
        self._popen = proc              # (for its stdio pipes only; never polled, waited on or signalled)
        self.pid = proc.pid             # the shim's (see the module text)
        self._wait_lock = threading.Lock()
        self._status: int | str | None = None   # None while running; then the observed status, or STATUS_UNKNOWN
        # The launch secret (C3-2a): sent once, in the NONCE frame; never in an argv, an environment, an event or a log.
        self._key = key
        self.boot, self.marker = boot, marker
        self._proof: dict | None = None     # the release pair, once both halves held (it cannot change after)
        # (piped: its stdin/stdout pipes, non-blocking: today's I/O contract; else no I/O, as spawn_process's child)
        self._io = PipedChild(proc) if proc.stdin is not None else None
        self._wlock = threading.Lock()  # pins the command endpoint through each write, and its close
        self._rlock = threading.Lock()  # the same for the event endpoint: each read, and its close
        self._cmd: int | None = cmd_w
        self._evt: int | None = evt_r
        self._evt_buf = b""
        self._went = False
        self.events: list[tuple[str, ...]] = []     # every event seen, in order (diagnostics and tests)
        self.cli_pid: int | None = None
        self.start_errno: int | None = None     # START_FAILED: positive evidence the CLI never exec'd
        # START_UNKNOWN: the child was released and ended with no exec seen and no failure said: it MAY have run (and
        # touched its checkout); never read it as "never ran". The proof (`empty()`) is unaffected.
        self.start_unknown = False
        self.leader_code: int | None = None
        self.nogo: str | None = None
        self.nosecret: str | None = None    # NOSECRET <reason>: the shim got no single valid NONCE frame

    # -------- start and GO

    @classmethod
    def start(cls, *, marker_dir: str, log: str, stderr_log: str, cwd: str = "/", python: str | None = None,
              test_gate: str | None = None, piped: bool = True) -> "ShimChild":
        """Spawn the shim and send it this launch's secret; return before GO. `marker_dir`: the node's private marker
        directory (`make_marker_dir`; checked again here); the marker's path in it is drawn afresh for this launch
        (`self.marker`), and an existing path, in any form, is refused before anything is spawned, never overwritten or
        removed. `log`: the shim's own diagnostics. The boot identity is read here (`boot_id`); if it cannot be read or
        validated, nothing is started (OSError). The CLI's stdio, exactly today's: `piped` (a hosted session, as
        `spawn_piped`): stdin and stdout are pipes to the node and stderr is appended to `stderr_log`, mode 600; not
        `piped` (a dispatched run, as `spawn_process`): stdin is /dev/null and stdout and stderr go to `stderr_log`, a
        NEW file (O_EXCL), mode 600.

        The secret (fresh random bytes, `secrets.token_bytes`) goes only into ONE `NONCE` frame on the command pipe,
        written here, before GO: it is never on the shim's argv, in its environment, or in any file. If that write
        fails, the command pipe is closed: the shim sees no valid frame, starts nothing and exits 7 (`refused()`)."""
        boot = boot_id()
        if boot is None:
            raise OSError(errno.ENOTSUP, "this boot's identity (kern.bootsessionuuid) cannot be read or validated: "
                                         "nothing is started")
        check_marker_dir(marker_dir)
        marker = os.path.join(os.path.abspath(marker_dir), _new_marker_name())
        try:
            os.lstat(marker)
        except FileNotFoundError:
            pass
        else:
            raise FileExistsError(errno.EEXIST, "the marker path exists: refused, and left as it is", marker)
        child = cls._launch(marker=marker, key=secrets.token_bytes(groupshim.KEY_BYTES), boot=boot, log=log,
                            stderr_log=stderr_log, cwd=cwd, python=python, test_gate=test_gate, piped=piped)
        child._send_secret()
        return child

    def _send_secret(self) -> None:
        frame = b"NONCE %d %s\n" % (groupshim.PROTOCOL, self._key.hex().encode("ascii"))
        with self._wlock:
            if self._cmd is None:
                return
            try:
                if os.write(self._cmd, frame) != len(frame):     # (< PIPE_BUF on a fresh pipe: all or nothing)
                    self._close_cmd()
            except OSError:
                self._close_cmd()       # (the shim then sees EOF or a partial frame: NOSECRET, nothing starts)

    @classmethod
    def _launch(cls, *, marker: str, key: bytes, boot: str, log: str, stderr_log: str, cwd: str = "/",
                python: str | None = None, test_gate: str | None = None, piped: bool = True) -> "ShimChild":
        """Spawn the shim with nothing sent yet (`start` sends the secret; tests call this directly to send their own
        frames)."""
        cmd_r, cmd_w = os.pipe()        # (close-on-exec: no other child of the node inherits either end)
        evt_r = evt_w = err_fd = -1
        try:
            evt_r, evt_w = os.pipe()
            flags = os.O_APPEND if piped else os.O_EXCL
            err_fd = os.open(stderr_log, os.O_WRONLY | os.O_CREAT | flags | getattr(os, "O_NOFOLLOW", 0), 0o600)
            os.fchmod(err_fd, 0o600)
            # (C3-2a) No secret on the argv, nor in the environment below: the CLI, the shim's child, can read both.
            argv = [python or sys.executable, "-I", "-S", "-c", _BOOT, _PACKAGE_ROOT,
                    "--version", str(groupshim.PROTOCOL), "--boot", boot, "--marker", marker, "--cmd-fd", str(cmd_r),
                    "--event-fd", str(evt_w), "--log", log]
            if test_gate is not None:
                argv += ["--test-gate", test_gate]
            proc = subprocess.Popen(argv, shell=False, cwd=cwd, env={"LC_ALL": "C", "PATH": "/usr/bin:/bin"},
                                    stdin=subprocess.PIPE if piped else subprocess.DEVNULL,
                                    stdout=subprocess.PIPE if piped else err_fd,
                                    stderr=err_fd if piped else subprocess.STDOUT, close_fds=True,
                                    pass_fds=(cmd_r, evt_w), start_new_session=True, bufsize=0)
        except BaseException:
            for fd in (cmd_w, evt_r):
                if fd >= 0:
                    os.close(fd)
            raise
        finally:
            for fd in (cmd_r, evt_w, err_fd):       # the shim's ends are the shim's alone now
                if fd >= 0:
                    os.close(fd)
        os.set_blocking(cmd_w, False)
        os.set_blocking(evt_r, False)
        return cls(proc, cmd_w, evt_r, key=key, boot=boot, marker=marker)

    def go(self, argv: list[str], *, cwd: str, env: dict[str, str], kill_grace: float, timeout: float = 5.0) -> None:
        """Write GO: the CLI's argv, checkout cwd, its WHOLE environment (the shim runs with a minimal one of its own,
        so the CLI's is exactly what the node built) and the kill grace. Not complete within `timeout`, or failed: the
        command pipe is closed (the shim then sees a partial frame and EOF, and never starts the CLI) and it raises."""
        body = json.dumps({"cwd": cwd, "argv": list(argv), "env": dict(env), "kill_grace": kill_grace},
                          ensure_ascii=True, separators=(",", ":"))
        frame = b"GO %d %s\n" % (groupshim.PROTOCOL, body.encode("ascii"))
        if len(frame) > groupshim.MAX_GO:
            raise ValueError(f"the GO frame is {len(frame)} bytes, over {groupshim.MAX_GO}")
        with self._wlock:
            if self._went or self._cmd is None:
                raise BrokenPipeError(errno.EPIPE, "GO was already written, or the command pipe is closed")
            view, deadline = memoryview(frame), time.monotonic() + timeout
            try:
                while view:
                    left = deadline - time.monotonic()
                    if left <= 0:
                        raise TimeoutError(errno.ETIMEDOUT, "the shim did not take GO in time")
                    try:
                        view = view[os.write(self._cmd, view):]
                    except BlockingIOError:
                        wait_writable(self._cmd, min(left, 0.05))
            except BaseException:
                self._close_cmd()
                raise
            self._went = True

    # -------- commands

    def _command(self, word: bytes) -> None:
        with self._wlock:
            if self._cmd is None:
                raise BrokenPipeError(errno.EPIPE, "the shim's command pipe is closed")
            if not self._went:
                self._close_cmd()       # before GO, a stop is "never start": EOF, the bound marker
                return
            os.write(self._cmd, word + b"\n")     # (< PIPE_BUF, non-blocking: all of it, or BlockingIOError)

    def terminate(self) -> None:
        self._command(b"TERM")

    def kill(self) -> None:
        self._command(b"KILL")

    def close_commands(self) -> None:
        """Close the command pipe: the shim takes it as the node letting go (TERM, then KILL after the grace)."""
        with self._wlock:
            self._close_cmd()

    def _close_cmd(self) -> None:       # (the lock held)
        if self._cmd is not None:
            fd, self._cmd = self._cmd, None
            os.close(fd)

    # -------- events

    def _pump(self) -> bool:
        """Take the events available now (bounded, non-blocking). True once the event pipe is at EOF."""
        with self._rlock:
            return self._pump_locked()

    def _pump_locked(self) -> bool:
        if self._evt is None:
            return True
        taken = 0
        while taken < EVENTS_PER_PUMP:
            try:
                chunk = os.read(self._evt, 65536)
            except BlockingIOError:
                return False
            except OSError:
                chunk = b""
            if not chunk:
                os.close(self._evt)
                self._evt = None
                self._take(final=True)
                return True
            taken += len(chunk)
            self._evt_buf += chunk
            self._take()
        return False

    def _take(self, final: bool = False) -> None:
        *lines, self._evt_buf = self._evt_buf.split(b"\n")
        if final:
            self._evt_buf = b""         # (a cut last line is no event)
        for line in lines:
            parts = tuple(line.decode("ascii", "replace").split())
            if not parts:
                continue
            self.events.append(parts)
            kind, args = parts[0], parts[1:]
            if kind == "STARTED" and args and args[0].isdigit():
                self.cli_pid = int(args[0])
            elif kind == "START_FAILED" and args and args[0].isdigit():
                self.start_errno = int(args[0])
            elif kind == "START_UNKNOWN":
                self.start_unknown = True
            elif kind == "LEADER_EXIT" and args and args[0].lstrip("-").isdigit():
                if self.leader_code is None:
                    self.leader_code = int(args[0])
            elif kind == "NOGO" and args:
                self.nogo = args[0]
            elif kind == "NOSECRET" and args:
                self.nosecret = args[0]

    def _observe(self) -> int | str | None:
        """(C3-2a review round 1, R3) THE one place the shim is reaped: `waitpid(pid, WNOHANG)` called here, the real
        status recorded exactly once (as `Popen.returncode` reads: negative for a signal). ECHILD (someone else reaped
        it, or the kernel did, for a process that ignores SIGCHLD or sets SA_NOCLDWAIT) is STATUS_UNKNOWN: no status was
        observed, and none is ever made up. None while it runs."""
        with self._wait_lock:
            if self._status is not None:
                return self._status
            try:
                pid, raw = os.waitpid(self.pid, os.WNOHANG)
            except ChildProcessError:
                self._status = STATUS_UNKNOWN
                return self._status
            except OSError:
                return None
            if pid != self.pid:
                return None
            self._status = os.waitstatus_to_exitcode(raw)
            return self._status

    def _kill_shim(self, sig: int = 9) -> None:     # (9: SIGKILL)
        """TEST-ONLY: signal the shim itself, only while this handle has not reaped it (its pid is pinned until then)."""
        with self._wait_lock:
            if self._status is None:
                os.kill(self.pid, sig)

    def _drained_exit(self) -> int | str | None:
        """The shim's exit status (`_observe`), with every event it wrote taken first once it is over (its write end
        is closed once it exited, and no other process holds it, so the drain ends at EOF)."""
        code = self._observe()
        if code is None:
            self._pump()
            return None
        for _ in range(1024):
            if self._pump():
                break
        return code

    # -------- the three facts

    def _release_pair(self) -> dict | None:
        """(C3-2a, F1.4) The proof, while the node lives: BOTH the shim's own terminal exit status, OBSERVED by this
        handle's own `waitpid` (`_observe`; the shim is the node's own unreaped child: the CLI cannot forge it; an
        UNKNOWN status is no status), AND an authentic marker (`read_marker`) whose outcome belongs with that status
        (`PROOF_PAIRS`). A marker alone, with the shim running or exited otherwise, proves nothing. Once both held, the
        pair is kept (the reaped status cannot change)."""
        if self._proof is not None:
            return self._proof
        status = self._drained_exit()
        rec = read_marker(self.marker, self._key, self.boot)
        if type(status) is not int or rec is None or (status, rec["outcome"]) not in PROOF_PAIRS:
            return None
        self._proof = rec
        return rec

    def poll(self) -> int | None:
        """The CLI's exit: the `LEADER_EXIT` code (as `Popen.returncode`: negative for a signal), else the exit code the
        release pair's marker carries (looked at by itself on every call, so an event dropped by backpressure is not
        needed; and only with the shim's matching exit status, never from a marker alone), else None. None also when
        the shim is LOST (the CLI's exit is unknown, and never made up): ask `lost()` (DESIGN_NOTES C3-2a has the
        table)."""
        self._pump()
        if self.leader_code is None:
            rec = self._release_pair()
            if rec is not None and type(rec["leader_exit"]) is int:
                self.leader_code = rec["leader_exit"]
        return self.leader_code

    def shim_poll(self) -> int | str | None:
        """The shim's own exit status, as this handle observed it (`_observe`, which reaps it), events drained first:
        None while it runs, then its status, or STATUS_UNKNOWN if someone else reaped it."""
        return self._drained_exit()

    def empty(self) -> bool:
        """The PROOF that this launch's group is absent (or was never made), for the living node: the release pair
        (`_release_pair`). Not a claim that the marker is durable (see the module text). The `EMPTY` event alone is
        advisory: a prompt to look, never the proof."""
        self._pump()
        return self._release_pair() is not None

    def refused(self) -> bool:
        """(C3-2a) The shim exited before any fork, publishing nothing: no single valid NONCE frame (exit 7) or bad
        arguments, an unreadable or foreign boot identity (exit 2). Its own exit status (the node's unreaped child)
        says nothing was ever started; no file is trusted for it. Only an OBSERVED status: a signal exit or
        STATUS_UNKNOWN is never a refusal. Neither `empty()` nor `lost()`."""
        status = self._drained_exit()
        return type(status) is int and status in REFUSED_EXITS

    def lost(self) -> bool:
        """The shim is over (exited, or its status UNKNOWN), every event drained, it did not refuse the launch, and
        there is no release pair: the anchor is gone without a proof (held, for a human). False while the shim runs."""
        if self._drained_exit() is None:
            return False
        return not self.refused() and not self.empty()

    # -------- today's I/O contract (dispatch.PipedChild's code, over the shim's stdio pipes; `piped` only)

    def _piped(self) -> PipedChild:
        if self._io is None:
            raise BrokenPipeError(errno.EPIPE, "this CLI's stdio is not piped to the node")
        return self._io

    def write(self, data: bytes) -> int:
        return self._piped().write(data)

    def read_lines(self) -> list[bytes]:
        return self._piped().read_lines()

    def close_stdin(self) -> None:
        if self._io is not None:
            self._io.close_stdin()

    @property
    def eof(self) -> bool:
        return self._io is None or self._io.eof

    @property
    def bytes_read(self) -> int:
        return 0 if self._io is None else self._io.bytes_read

    def close(self) -> None:
        """Release every pipe: stdin, stdout, the event pipe and the command pipe. Meant for after the shim's exit; a
        shim still running takes the command pipe's close as the node letting go, and stops its group."""
        if self._io is not None:
            self._io.close()
        with self._wlock:
            self._close_cmd()
        with self._rlock:
            if self._evt is not None:
                fd, self._evt = self._evt, None
                os.close(fd)
