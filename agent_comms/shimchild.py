"""(Ownership C3-1) The node's handle on one group supervisor (agent_comms/groupshim.py): what a spawn site will hold in
place of the CLI's `Popen` once C3-2 wires it in. NOT used by any spawn site yet.

- `ShimChild.start(...)` spawns the shim (cwd "/" by default, outside every checkout; only its two control fds passed
  besides the CLI's future stdio) and returns BEFORE `GO`; `go(...)` writes `GO`. C3-2 fsyncs the durable record
  between the two. Until `go`, a `terminate()`/`kill()`/`close()` closes the command pipe, which the shim takes as "never
  start": it publishes its bound marker and exits.
- It has no thread. Events are pumped, non-blocking, in `poll()`, `empty()` and `lost()`; before a shim exit is
  classified, the event pipe is drained to EOF.
- Three facts, never conflated: the CLI's exit (`poll()`: the `LEADER_EXIT` code, or the exit code a valid marker
  carries, or None), the shim's exit (`shim_poll()`: the node's own child, `Popen.poll()`), and the group's absence
  (`empty()`: a valid, complete, VISIBLE marker for this launch's nonce and this boot, never the `EMPTY` event alone).
  `empty()` is proof of absence for the living node that reads it. Durability across a crash is the shim's best effort
  (a directory sync that failed is said by `UNSYNCED`): a marker lost in a crash leaves the checkout held at the next
  start, the conservative direction. A LOST shim (`lost()`) leaves `poll()` at None forever: callers must ask `lost()`
  on their own, before they read None as "still running" (C3-2: every reap and shutdown path).
- `terminate()`/`kill()` write `TERM`/`KILL`: one non-blocking write each, under the handle's own lock, which also
  guards closing the pipe, so a concurrent retirement cannot close the fd and have its number reused for another
  shim's pipe mid-write. A closed or broken pipe raises `BrokenPipeError`; a full one `BlockingIOError` (both
  `OSError`, the shape today's callers handle for a failed signal).
- The I/O contract (`write`, `read_lines`, `close_stdin`, `close`, `eof`, `bytes_read`) is dispatch.PipedChild's own
  code, over the shim's stdin and stdout pipes, which the CLI inherits as its stdio (a hosted session's shape,
  `spawn_piped`). `piped=False` gives a dispatched run's shape instead (`spawn_process`: stdin /dev/null, stdout and
  stderr to one new log), with no I/O methods.
- `pid` is the SHIM's pid (the node's own unreaped child, so pinned until `shim_poll()` reaps it); `cli_pid` is the
  CLI's, for diagnostics only: nothing signals or probes it by number.

It never logs: it reports by return value and exception.
"""
from __future__ import annotations

import errno
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
MARKER_MAX = 4096                   # bytes a marker may have (the shim's are about 200): larger is no proof
# The shim runs THIS process's own agent_comms (the directory that holds the package imported here), never whatever
# `-m agent_comms.groupshim` would find on the interpreter's path: `-I` (isolated: no PYTHON* variables, no user site,
# no current directory on the path), then the package's own root first. Equivalent to `python -m agent_comms.groupshim`.
_PACKAGE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(groupshim.__file__)))
_BOOT = ("import sys; sys.path.insert(0, sys.argv.pop(1)); from agent_comms.groupshim import entry; "
         "entry(sys.argv[1:])")
EVENTS_PER_PUMP = 256 * 1024        # bytes one pump takes at most (non-blocking: never waits)


def wait_writable(fd: int, timeout: float) -> None:
    """Wait, at most `timeout` seconds, for `fd` to take a write. `poll`, not `select`: a node can hold more than
    FD_SETSIZE (1024) descriptors, and `select` refuses any fd at or above it (found by the serial suite)."""
    p = select.poll()
    p.register(fd, select.POLLOUT)
    p.poll(max(0, int(timeout * 1000)))


_MARKER_KEYS = frozenset({"v", "nonce", "boot", "outcome", "cli_pid", "leader_exit"})


def read_marker(path: str, nonce: str, boot: str) -> dict | None:
    """THE one marker reader (`empty()`, `lost()` and `poll()` all use it): the marker at `path` when it proves the
    group of launch `nonce`, on boot `boot`, absent (or never made); else None, never an exception.

    It must be a REGULAR file: `lstat` first (no symlink, FIFO, socket, device or directory is opened), then an open with
    O_NONBLOCK | O_NOFOLLOW, and `fstat` of what was opened must be the same regular file (a special file swapped in
    between is opened without waiting, and refused unread). At most MARKER_MAX bytes, one line ending in a newline, a
    JSON object with exactly the shim's keys and exact types: `v` an int (never a bool) equal to the protocol; the nonce
    and the boot exactly these; the outcome `empty` (with an int `cli_pid` and an int or null `leader_exit`) or
    `never_started` (both null). The scope of "never waits": the reader never opens or reads a special file and never
    waits on one; O_NONBLOCK does not bound ordinary filesystem I/O on a regular file (a stalled filesystem can still
    stall the read)."""
    try:
        if type(nonce) is not str or not nonce or type(boot) is not str or not boot:
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
        if type(rec["v"]) is not int or rec["v"] != groupshim.PROTOCOL:
            return None
        if type(rec["nonce"]) is not str or rec["nonce"] != nonce or type(rec["boot"]) is not str or rec["boot"] != boot:
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
        return rec
    except Exception:
        return None


def marker_proves(path: str, nonce: str, boot: str) -> bool:
    """Whether `read_marker` accepts the marker at `path` for this launch and boot."""
    return read_marker(path, nonce, boot) is not None


class ShimChild:
    def __init__(self, proc: subprocess.Popen, cmd_w: int, evt_r: int, *, nonce: str, boot: str, marker: str):
        self.proc = proc
        self.pid = proc.pid             # the shim's (see the module text)
        self.nonce, self.boot, self.marker = nonce, boot, marker
        # (piped: its stdin/stdout pipes, non-blocking: today's I/O contract; else no I/O, as spawn_process's child)
        self._io = PipedChild(proc) if proc.stdin is not None else None
        self._wlock = threading.Lock()  # pins the command endpoint through each write, and its close
        self._rlock = threading.Lock()  # the same for the event endpoint: each read, and its close
        self._cmd: int | None = cmd_w
        self._evt: int | None = evt_r
        self._evt_buf = b""
        self._went = False
        self._proved = False
        self.events: list[tuple[str, ...]] = []     # every event seen, in order (diagnostics and tests)
        self.cli_pid: int | None = None
        self.start_errno: int | None = None     # START_FAILED: positive evidence the CLI never exec'd
        # START_UNKNOWN: the child was released and ended with no exec seen and no failure said: it MAY have run (and
        # touched its checkout); never read it as "never ran". The proof (`empty()`) is unaffected.
        self.start_unknown = False
        self.leader_code: int | None = None
        self.nogo: str | None = None

    # -------- start and GO

    @classmethod
    def start(cls, *, marker: str, log: str, stderr_log: str, nonce: str | None = None, boot: str | None = None,
              cwd: str = "/", python: str | None = None, test_gate: str | None = None,
              piped: bool = True) -> "ShimChild":
        """Spawn the shim; return before GO. `marker`: where it publishes its proof (its directory must exist);
        `log`: its own diagnostics. The CLI's stdio, exactly today's: `piped` (a hosted session, as `spawn_piped`):
        stdin and stdout are pipes to the node and stderr is appended to `stderr_log`, mode 600; not `piped` (a
        dispatched run, as `spawn_process`): stdin is /dev/null and stdout and stderr go to `stderr_log`, a NEW file
        (O_EXCL), mode 600."""
        nonce = nonce if nonce is not None else secrets.token_hex(16)
        boot = boot if boot is not None else boot_id()
        if boot is None:
            raise OSError(errno.ENOTSUP, "this boot's identity cannot be read")
        cmd_r, cmd_w = os.pipe()        # (close-on-exec: no other child of the node inherits either end)
        evt_r = evt_w = err_fd = -1
        try:
            evt_r, evt_w = os.pipe()
            flags = os.O_APPEND if piped else os.O_EXCL
            err_fd = os.open(stderr_log, os.O_WRONLY | os.O_CREAT | flags | getattr(os, "O_NOFOLLOW", 0), 0o600)
            os.fchmod(err_fd, 0o600)
            argv = [python or sys.executable, "-I", "-c", _BOOT, _PACKAGE_ROOT, "--version", str(groupshim.PROTOCOL),
                    "--nonce", nonce, "--boot", boot, "--marker", marker, "--cmd-fd", str(cmd_r),
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
        return cls(proc, cmd_w, evt_r, nonce=nonce, boot=boot, marker=marker)

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

    def _drained_exit(self) -> int | None:
        """The shim's exit status, with every event it wrote taken first (its write end is closed once it exited, and
        no other process holds it, so the drain ends at EOF)."""
        code = self.proc.poll()
        if code is None:
            self._pump()
            return None
        for _ in range(1024):
            if self._pump():
                break
        return code

    # -------- the three facts

    def poll(self) -> int | None:
        """The CLI's exit: the `LEADER_EXIT` code (as `Popen.returncode`: negative for a signal), else the exit code a
        valid marker for this launch carries (read by itself on every call: an event dropped by backpressure is not
        needed), else None. None also when the shim is LOST (the CLI's exit is unknown, and never made up): ask
        `lost()` (DESIGN_NOTES C3-1 review round 1 has the whole table)."""
        self._pump()
        if self.leader_code is None:
            rec = read_marker(self.marker, self.nonce, self.boot)
            if rec is not None:
                self._proved = True
                if type(rec["leader_exit"]) is int:
                    self.leader_code = rec["leader_exit"]
        return self.leader_code

    def shim_poll(self) -> int | None:
        """The shim's own exit status (the node's own child: `Popen.poll()`, which reaps it), events drained first."""
        return self._drained_exit()

    def empty(self) -> bool:
        """The PROOF that this launch's group is absent (or was never made), for the living node: a valid, complete,
        visible marker for this launch's nonce and this boot (`read_marker`). Not a claim that it is durable (see the
        module text). The `EMPTY` event alone is advisory: a prompt to look, never the proof."""
        self._pump()
        if not self._proved:
            self._proved = read_marker(self.marker, self.nonce, self.boot) is not None
        return self._proved

    def lost(self) -> bool:
        """The shim has exited and, every event drained, there is no matching marker: the anchor is gone without a
        proof (held, for a human). False while the shim runs (the marker is published before its exit, so it cannot
        appear after)."""
        if self._drained_exit() is None:
            return False
        return not self.empty()

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
