"""Host-side logging (DESIGN_NOTES "Host-side logging", and "Host-side logging: one writer", which supersedes the
caller-thread drain): the package-owned logger of the session host, the node's session runner, the process-group
registry, the node and the providers.

THE guarantee: a log handler that blocks forever, or raises, changes nothing but log output: every signal, report,
save and return of the host-side code happens at its time. Nothing process-wide is patched: `logging` is used as it
is, through its loggers' own level checks and handlers, under the same logger names.

- A line is a deferred record, made AT THE CALL (`HostLogger`): a snapshot of reviewed fields only (`_Record`): the
  wall time, the thread, the logger's name, the level (a line the logger would not emit is not kept: the level is
  checked at the call), the message template, the caller as (file, line, function) from its frame with no source
  lookup, and the arguments reduced there: `str`, `int`, `float`, `bool` and `None` kept (also as the values of a
  copied `%`-mapping), any other object replaced by its type's name; a value rendered later (`facts`: an
  exception's reviewed integers; `later`: a reviewed function of reduced values) is kept as it is and rendered by the
  writer, never at the call. An exception is kept as its type's name and its traceback's (file, line, function)
  triples: no exception text, no source lines, no locals. Nothing is formatted at the call.
- ONE queue (bounded: the oldest goes first when it is full, the writer says once how many went) and ONE writer: a
  daemon thread ("hostlog-writer"). It is started EAGERLY in production (`start_writer`, by `node.run` before
  recovery, outside every host lock; again at the node's pass boundary if it is not running) and lazily only by a
  standalone caller at depth 0; a lifecycle thread's enqueue, or any enqueue under a host lock, never starts it. A
  start is claimed under the queue's lock and made outside it; one that fails is contained (no log call raises an
  ordinary failure into the host; an interrupt propagates). It is the
  ONLY thread that ever enters a handler, a filter or a formatter (`_write`), in queue order (the enqueue order). It
  holds no lock of the host-side modules, ever. A handler that raises is skipped (per handler); nothing a handler
  raises ends the writer; the record in flight counts as attempted and is never written again.
- Who waits for the writer. A LIFECYCLE thread never does (`ENQUEUE_ONLY`, registered with `enqueue_only()`; ONE
  predicate, `may_wait`): the node's main thread for the whole of `node.run` (registered before anything is built or
  recovered), the session loop, the deadline watchdog, the checkpoint sender, and the writer itself (a handler that
  logs, or calls back into the host, enqueues and returns). A STANDALONE caller (a test, a command calling the host
  directly) waits, when it leaves its outermost boundary at depth 0 (never while it holds a lock of the host-side
  modules: every such lock is a boundary, `HostLock`), for the writer to have attempted ITS OWN last record, for at
  most ACK_SECONDS (`_acknowledge`): a public call returns with its lines with the handlers (caplog) in the
  uncontended case. Best effort by statement: a stall latch (a wait that times out marks the writer stalled at the
  completion sequence it saw; nobody waits again until a LATER completion), and a record the bound evicted is
  resolved as dropped at once.
- `flush(timeout)`: a bounded wait for the queue to be empty and the writer idle, entering no handler. `node.run` makes
  ONE final bounded wait after the last signal and report (`final_flush`), which SEALS the queue in the same critical
  section as its idle decision: every later record is rejected, so idle-and-sealed is never followed by a handler
  entry; a node whose writer is still stuck then ends without waiting for the handler (`node.run`). A standalone
  process's `atexit` makes the same wait and seal.
"""
from __future__ import annotations

import atexit
import functools
import logging
import sys
import threading
import time
from collections import deque
from contextlib import contextmanager
from typing import Any, Callable

# The lifecycle threads: they never wait for the writer (`enqueue_only`, `may_wait`). (session_host's WATCH_THREADS
# is another registry, for saves and transcript lines only.)
ENQUEUE_ONLY: set[int] = set()
QUEUE_MAX = 2000        # records kept at most; the oldest goes first (the writer says once how many went)
ACK_SECONDS = 2.0       # the most a standalone caller waits for its own last record (`_acknowledge`)
WRITER_NAME = "hostlog-writer"

_local = threading.local()      # `depth`: the boundaries this thread is inside; `last`: its last record's sequence
# The queue's own lock: held only to append, to pop and to note a completion, never across a handler, a host call or
# a wait on anything else (a wait on this condition lets it go). A native lock, never a boundary.
_cond = threading.Condition(threading.Lock())
_queue: deque = deque()
# seq: the last record queued; done: the last one the writer attempted; evicted: the last one the bound let go;
# dropped: how many went since the writer last said so; busy: the record the writer is writing, or None; stalled_at:
# the completion sequence a timed-out wait saw (the stall latch), or None; gen: the writer's generation (tests only
# change it, to leave a writer a test blocked for good); exit_deadline: `final_flush`'s bound, shared with `atexit`.
# sealed: the final wait made (`final_flush`): every later record is rejected (one-way; tests reset it); starting: a
# start of the writer is claimed and under way (outside `_cond`); start_failed_at: when the last start failed.
_state: dict = {"seq": 0, "done": 0, "evicted": 0, "dropped": 0, "busy": None, "stalled_at": None, "gen": 0,
                "exit_deadline": None, "sealed": False, "starting": False, "start_failed_at": None}
_writer: list = [None]
RESTART_SECONDS = 1.0       # a failed start of the writer is tried again at most this often
_TYPE_NAME = type.__dict__["__name__"]      # a type's own name, read with no code of the object's own


def _type_name(v: Any) -> str:
    return _TYPE_NAME.__get__(type(v))


# ---------------------------------------------------------------- the boundary (the per-thread depth)


class _Boundary:
    """Entered and left in this one place: the depth goes up on entry and down on exit; leaving the outermost one
    (depth 0 again) is where a standalone caller waits for its own lines (`_acknowledge`), whether its body returned
    or raised. A lifecycle thread waits for nothing there."""

    def __enter__(self) -> "_Boundary":
        _local.depth = getattr(_local, "depth", 0) + 1
        return self

    def __exit__(self, *exc) -> bool:
        self.leave()
        return False

    def leave(self) -> None:
        """The exit: at depth 0 again, a standalone caller's bounded acknowledgement of its own last record."""
        depth = getattr(_local, "depth", 0) - 1
        _local.depth = depth if depth > 0 else 0
        if depth <= 0:
            try:
                _acknowledge()
            except Exception:       # (never into the host's code)
                pass


BOUNDARY = _Boundary()


def depth() -> int:
    """The boundaries the calling thread is inside (0: none; it holds no host lock)."""
    return getattr(_local, "depth", 0)


def entry(fn: Callable) -> Callable:
    """A public entry point of the host: one boundary around the whole call."""
    @functools.wraps(fn)
    def wrapper(*a, **k):
        with BOUNDARY:
            return fn(*a, **k)
    wrapper.__hostlog_entry__ = True
    return wrapper


def entries(cls: type) -> type:
    """Every public method defined in `cls`'s body (a plain function whose name does not start with "_") becomes an
    entry point (`entry`). Properties, static and class methods are left as they are, and so is a function already
    decorated (`__wrapped__`: a context manager's factory, whose body runs in its caller's `with`)."""
    for name, value in list(vars(cls).items()):
        if not name.startswith("_") and type(value) is type(entries) and not hasattr(value, "__wrapped__"):
            setattr(cls, name, entry(value))
    return cls


@contextmanager
def enqueue_only():
    """The calling thread is a lifecycle thread while inside (a background thread's whole body; `node.run`'s): it
    only enqueues, and never waits for the writer."""
    me = threading.get_ident()
    was = me in ENQUEUE_ONLY
    ENQUEUE_ONLY.add(me)
    try:
        yield
    finally:
        if not was:
            ENQUEUE_ONLY.discard(me)


def may_wait() -> bool:
    """THE predicate: the calling thread may wait for the writer now: it is inside no boundary (so it holds no lock of
    the host-side modules) and it is not a lifecycle thread (the writer is one)."""
    return getattr(_local, "depth", 0) == 0 and threading.get_ident() not in ENQUEUE_ONLY


class HostLock:
    """A lock of the host-side modules under which a line can be logged (the runner lock, the cutoff lock, the
    registry lock, the report lock, the acknowledgement lock, a steer attempt's record lock): taking it enters a
    boundary, letting it go leaves that boundary, so no thread ever waits for the writer while it holds one.
    The lock's own semantics are the wrapped lock's (a Lock, or an RLock, re-entered as such: each acquisition enters
    the boundary once, each release leaves it once). It is NOT a general replacement for a lock: it has no
    `_release_save`/`_acquire_restore`, so a `threading.Condition` must not be built on it (a Condition of the
    host-side modules keeps its native lock, with a boundary around its use where a line can be logged under it)."""

    __slots__ = ("_lock",)

    def __init__(self, lock) -> None:
        self._lock = lock

    def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:
        BOUNDARY.__enter__()
        try:
            ok = self._lock.acquire(blocking, timeout)
        except BaseException:
            BOUNDARY.leave()
            raise
        if not ok:
            BOUNDARY.leave()
        return ok

    def release(self) -> None:
        self._lock.release()
        BOUNDARY.leave()

    def __enter__(self) -> bool:
        return self.acquire()

    def __exit__(self, *exc) -> bool:
        self.release()
        return False

    def locked(self) -> bool:
        """Held by any thread (an RLock held by the caller included)."""
        locked = getattr(self._lock, "locked", None)
        if locked is not None:
            return locked()
        if self._lock._is_owned():      # (an RLock before 3.14: no `locked`; re-acquiring would succeed for its owner)
            return True
        if self._lock.acquire(blocking=False):
            self._lock.release()
            return False
        return True

    def _is_owned(self) -> bool:
        return self._lock._is_owned()


# ---------------------------------------------------------------- the record, made at the call


class _Record:
    __slots__ = ("seq", "created", "thread", "thread_name", "name", "level", "msg", "args", "caller", "exc", "extra")


class _Later:
    """A value the WRITER renders (`__str__`, in a `%s`): a reviewed function and reduced values taken at the call.
    Nothing of it is rendered at the call."""

    __slots__ = ("fn", "args")

    def __init__(self, fn: Callable[..., str], args: tuple) -> None:
        self.fn, self.args = fn, args

    def __str__(self) -> str:
        try:
            out = self.fn(*self.args)
            return out if type(out) is str else _type_name(out)
        except Exception as e:
            return f"(unrenderable: {_type_name(e)})"

    __repr__ = __str__


def later(fn: Callable[..., str], *args) -> _Later:
    """`fn(*args)` rendered by the writer, outside every lock; the args are reduced at the call (`_reduce`). For a
    reviewed module-level function of plain values only (providers/claude `_shown`)."""
    return _Later(fn, tuple(_reduce(a) for a in args))


def _int(v: Any) -> int | None:
    return v if type(v) is int else None


def _render_facts(name: str, status: int | None, errno: int | None, returncode: int | None) -> str:
    parts = [name]
    if status is not None:
        parts.append("status=%d" % status)
    if errno is not None:
        parts.append("errno=%d" % errno)
    if returncode is not None:
        parts.append("returncode=%d" % returncode)
    return " ".join(parts)


def facts(exc: Any) -> _Later:
    """(Review round 1, R3; round 2, R7) An exception as reviewed facts, never its text: its type's name and, for a
    reviewed class, one integer read at the call with no code of the exception's own (no property, no `__dict__` or
    `__getattribute__` of a subclass, no `str`): an OSError's errno, through OSError's own member descriptor (so for
    any subclass too: the descriptor reads the C slot); an `httpx.HTTPStatusError`'s status code and a
    `subprocess.CalledProcessError`'s return code, only when the type is EXACTLY that class (and the response
    exactly `httpx.Response`), read from the instance's own dictionary; any other class, a subclass of those two
    included: its type's name only. Rendered by the writer ("HTTPStatusError status=502", "OSError errno=61")."""
    status = errno = returncode = None
    try:
        t = type(exc)
        if issubclass(t, OSError):
            errno = _int(OSError.__dict__["errno"].__get__(exc))
        import subprocess
        if t is subprocess.CalledProcessError:
            returncode = _int(_own_dict(exc).get("returncode"))
        import httpx
        if t is httpx.HTTPStatusError:
            response = _own_dict(exc).get("response")
            if type(response) is httpx.Response:
                status = _int(_own_dict(response).get("status_code"))
    except Exception:
        status = errno = returncode = None
    return _Later(_render_facts, (_type_name(exc), status, errno, returncode))


def _own_dict(obj: Any) -> dict:
    """The instance dictionary of an object of an EXACT reviewed class (its class's own `__dict__` descriptor)."""
    d = type(obj).__dict__.get("__dict__")
    if d is None:
        for base in type(obj).__mro__[1:]:
            d = base.__dict__.get("__dict__")
            if d is not None:
                break
    out = d.__get__(obj) if d is not None else None
    return out if type(out) is dict else {}


def _reduce(v: Any) -> Any:
    """A reviewed value: kept if it is a str, int, float, bool or None (a subclass of one as the base type's own value,
    read with no code of the subclass), or a value the writer renders (`_Later`); otherwise its type's name."""
    t = type(v)
    if t is str or t is int or t is float or t is bool or v is None or t is _Later:
        return v
    if isinstance(v, str):
        return str.__str__(v)
    if isinstance(v, int):
        return int.__int__(v)
    if isinstance(v, float):
        return float.__float__(v)
    return _type_name(v)


def _reduce_args(args: tuple) -> Any:
    if len(args) == 1 and type(args[0]) is dict and args[0]:
        # logging's own rule: a lone non-empty mapping is the record's args (for "%(key)s" templates), copied here
        return {k: _reduce(v) for k, v in args[0].items() if type(k) is str}
    return tuple(_reduce(v) for v in args)


def _exception(exc_info: Any) -> tuple[str, tuple] | None:
    """(type name, ((file, line, function), ...)) of the exception, or None: nothing formatted, no text, no source."""
    if exc_info is True or (exc_info and not isinstance(exc_info, (BaseException, tuple))):
        exc_info = sys.exc_info()
    if isinstance(exc_info, BaseException):
        value, tb = exc_info, exc_info.__traceback__
    elif isinstance(exc_info, tuple) and len(exc_info) == 3:
        value, tb = exc_info[1], exc_info[2]
    else:
        return None
    if value is None:
        return None
    frames = []
    while tb is not None:
        code = tb.tb_frame.f_code
        frames.append((code.co_filename, tb.tb_lineno, code.co_name))
        tb = tb.tb_next
    return _type_name(value), tuple(frames)


def _caller(stacklevel: int) -> tuple[str, int, str]:
    f = sys._getframe(1)
    while f is not None and f.f_globals.get("__name__") == __name__:
        f = f.f_back
    while f is not None and stacklevel > 1:
        f, stacklevel = f.f_back, stacklevel - 1
    if f is None:
        return "(unknown file)", 0, "(unknown function)"
    return f.f_code.co_filename, f.f_lineno, f.f_code.co_name


def _keep(name: str, level: int, msg: Any, args: tuple, exc_info: Any, extra: Any, stacklevel: int) -> None:
    """(One writer, review round 1, R12) The enqueue path as a whole never raises an ordinary failure (`Exception`)
    into the host; a shutdown interrupt (`KeyboardInterrupt`, `SystemExit`) that arrives in it propagates, leaving no
    lock held and no start claimed."""
    try:
        _enqueue(name, level, msg, args, exc_info, extra, stacklevel)
    except Exception:       # (a record that could not be kept is lost: logging, never the host's operation)
        pass


def _enqueue(name: str, level: int, msg: Any, args: tuple, exc_info: Any, extra: Any, stacklevel: int) -> None:
    r = _Record()
    r.name, r.level = name, level
    try:
        r.created = time.time()
        t = threading.current_thread()
        r.thread, r.thread_name = t.ident, t.name
        r.msg = msg if type(msg) is str else _reduce(msg)
        r.args = _reduce_args(args)
        r.caller = _caller(stacklevel)
        r.exc = _exception(exc_info) if exc_info else None
        r.extra = ({k: _reduce(v) for k, v in extra.items() if type(k) is str}
                   if type(extra) is dict and extra else None)
    except Exception as e:      # (a snapshot that fails keeps a line saying so, never raises into the host)
        r.created, r.thread, r.thread_name = getattr(r, "created", 0.0), None, "?"
        r.msg, r.args, r.exc, r.extra = "a log line could not be kept (%s)", (_type_name(e),), None, None
        r.caller = ("(unknown file)", 0, "(unknown function)")
    with _cond:
        if _state["sealed"]:
            return      # (R11: after the final wait, rejected: counted nowhere, no writer, no waiter)
        _state["seq"] += 1
        r.seq = _local.last = _state["seq"]
        if len(_queue) >= QUEUE_MAX:
            gone = _queue.popleft()
            _state["dropped"] += 1
            _state["evicted"] = max(_state["evicted"], gone.seq)
        _queue.append(r)
        _cond.notify_all()
    # (A4) An enqueue never starts the writer: production starts it eagerly (`start_writer`, from `node.run`); a
    # standalone caller starts it lazily at its depth-0 exit (`_acknowledge`), never under a host lock.


class HostLogger:
    """A host-side module's logger: `logging.getLogger(name)`'s level checks and handlers, with every line made into
    a deferred record at the call and written by the writer (the module text). The API is the part of
    `logging.Logger`'s the host uses."""

    def __init__(self, name: str) -> None:
        self.name, self.logger = name, logging.getLogger(name)

    def isEnabledFor(self, level: int) -> bool:
        return self.logger.isEnabledFor(level)

    def getEffectiveLevel(self) -> int:
        return self.logger.getEffectiveLevel()

    def log(self, level: int, msg: Any, *args, exc_info: Any = None, extra: Any = None, stack_info: bool = False,
            stacklevel: int = 1) -> None:
        if not isinstance(level, int):
            raise TypeError("level must be an integer")
        if self.logger.isEnabledFor(level):
            with BOUNDARY:          # (a line logged outside every boundary is its own boundary)
                _keep(self.name, level, msg, args, exc_info, extra, stacklevel)

    def debug(self, msg: Any, *args, **kw) -> None:
        self.log(logging.DEBUG, msg, *args, **kw)

    def info(self, msg: Any, *args, **kw) -> None:
        self.log(logging.INFO, msg, *args, **kw)

    def warning(self, msg: Any, *args, **kw) -> None:
        self.log(logging.WARNING, msg, *args, **kw)

    def error(self, msg: Any, *args, **kw) -> None:
        self.log(logging.ERROR, msg, *args, **kw)

    def exception(self, msg: Any, *args, exc_info: Any = True, **kw) -> None:
        self.log(logging.ERROR, msg, *args, exc_info=exc_info, **kw)

    def critical(self, msg: Any, *args, **kw) -> None:
        self.log(logging.CRITICAL, msg, *args, **kw)


def get(name: str) -> HostLogger:
    return HostLogger(name)


# ---------------------------------------------------------------- the drain: the writer


def _exc_text(exc: tuple[str, tuple]) -> str:
    name, frames = exc
    return "".join(["Traceback (frames only: no exception text, no source lines):\n",
                    *(f'  File "{fn}", line {ln}, in {fu}\n' for fn, ln, fu in frames), name])


def _write(r: _Record) -> None:
    """The record made a real `logging.LogRecord` (the call's time, thread and caller) and handed to the logger's
    handlers and its ancestors', as `Logger.handle` would, each handler on its own: one that raises is skipped."""
    logger = logging.getLogger(r.name)
    if logger.disabled:
        return
    fn, ln, func = r.caller
    args = r.args
    if type(args) is tuple and any(type(a) is _Later for a in args):
        args = tuple(str(a) if type(a) is _Later else a for a in args)     # (rendered here, once: never at the call)
    elif type(args) is dict and any(type(a) is _Later for a in args.values()):
        args = {k: str(a) if type(a) is _Later else a for k, a in args.items()}
    rec = logger.makeRecord(r.name, r.level, fn, ln, r.msg, args, None, func=func, extra=r.extra)
    rec.created = r.created
    rec.msecs = int((r.created - int(r.created)) * 1000) + 0.0
    rec.relativeCreated = (r.created - logging._startTime) * 1000
    rec.thread, rec.threadName = r.thread, r.thread_name
    rec.hostlog_seq = r.seq     # (its place in the queue: the write order is the queue's)
    if r.exc is not None:
        rec.exc_text = _exc_text(r.exc)
        rec.hostlog_exc = r.exc
    kept = logger.filter(rec)
    if not kept:
        return
    if isinstance(kept, logging.LogRecord):
        rec = kept
    c, found = logger, 0
    while c is not None:
        for h in list(c.handlers):
            found += 1
            if rec.levelno >= h.level:
                try:
                    h.handle(rec)
                except BaseException:   # (one handler's failure never stops the others: this is the writer's thread)
                    pass
        c = c.parent if c.propagate else None
    if found == 0 and logging.lastResort is not None and rec.levelno >= logging.lastResort.level:
        try:
            logging.lastResort.handle(rec)
        except BaseException:
            pass


def _writer_alive() -> bool:
    w = _writer[0]
    return w is not None and w.is_alive()


def _claim_start(force: bool) -> bool:
    """(Under `_cond`.) Claim THE one start of the writer: only if none is running and none is under way, and (unless
    `force`) not within RESTART_SECONDS of a failed one (no busy retry). True: the caller starts it, outside `_cond`."""
    if _writer_alive() or _state["starting"]:
        return False
    failed = _state["start_failed_at"]
    if not force and failed is not None and time.monotonic() - failed < RESTART_SECONDS:
        return False
    _state["starting"] = True
    return True


def _start_writer() -> None:
    """The claimed start, OUTSIDE `_cond` (A2): a `Thread.start()` that fails (the OS refuses a thread) is contained:
    the records stay queued up to the bound and are resolved as dropped by the usual rule; waiters are released (no
    writer: nobody waits); a later start point tries again. The claim is let go however it ends (an interrupt
    included, which propagates)."""
    with _cond:
        gen = _state["gen"]
    w = threading.Thread(target=_write_loop, args=(gen,), name=WRITER_NAME, daemon=True)
    started = False
    try:
        w.start()
        started = True
    except Exception:
        pass
    finally:
        with _cond:
            _state["starting"] = False
            if started:
                _writer[0], _state["start_failed_at"] = w, None
            else:
                _state["start_failed_at"] = time.monotonic()
            _cond.notify_all()


def _lazy_start() -> None:
    """(A standalone caller at depth 0, so never under a host lock, never on a lifecycle thread.) Start the writer if
    records wait and none is running. The claim is let go however this ends (an interrupt included)."""
    claimed = attempted = False
    try:
        with _cond:
            claimed = bool(_queue or _state["dropped"]) and _claim_start(force=False)
        if claimed:
            attempted = True
            _start_writer()
    finally:
        if claimed and not attempted:
            with _cond:
                _state["starting"] = False
                _cond.notify_all()


def start_writer(force: bool = False) -> bool:
    """Start the writer if it is not running: by `node.run` (eagerly, before recovery, outside every host lock) and at
    its pass boundary (the retry point after a failed start: on the main thread, at depth 0). True: it runs."""
    with _cond:
        claimed = _claim_start(force)
    if claimed:
        _start_writer()
    with _cond:
        return _writer_alive()


def _write_loop(gen: int) -> None:
    """The writer: every record, in queue order, handed to the handlers; never waits for anyone (a lifecycle thread);
    holds no lock of the host-side modules. Nothing a handler raises ends it."""
    ENQUEUE_ONLY.add(threading.get_ident())
    while True:
        with _cond:
            while gen == _state["gen"] and not _queue and not _state["dropped"]:
                _cond.wait()
            if gen != _state["gen"]:
                return
            dropped, _state["dropped"] = _state["dropped"], 0
            if dropped:
                r = _Record()
                r.seq, r.created, r.name, r.level = 0, time.time(), __name__, logging.WARNING
                me = threading.current_thread()
                r.thread, r.thread_name = me.ident, me.name
                r.msg, r.args = "%d deferred log line(s) were dropped: the queue was full", (dropped,)
                r.caller, r.exc, r.extra = ("(hostlog)", 0, "writer"), None, None
            else:
                r = _queue.popleft()
            _state["busy"] = r.seq
        try:
            _write(r)
        except BaseException:       # (the writer is not the main thread: nothing it is handed ends it)
            pass
        finally:
            with _cond:
                if gen == _state["gen"]:
                    _state["busy"] = None
                    if r.seq > _state["done"]:
                        _state["done"] = r.seq
                    if _state["stalled_at"] is not None and _state["done"] > _state["stalled_at"]:
                        _state["stalled_at"] = None     # (a LATER completion: the latch clears)
                    _cond.notify_all()


def _acknowledge() -> bool:
    """A standalone caller at depth 0: wait, at most ACK_SECONDS, for the writer to have attempted this thread's own
    last record. Not on a lifecycle thread, not inside a boundary, not while the writer is latched stalled; released at
    once when that record was evicted by the bound. True: it was attempted (or dropped)."""
    if not may_wait():
        return False
    _lazy_start()
    target = getattr(_local, "last", 0)
    deadline = time.monotonic() + ACK_SECONDS
    with _cond:
        while True:
            if _state["done"] >= target or _state["evicted"] >= target:
                return True
            if _state["stalled_at"] is not None:
                return False        # (the latch: nobody waits until the writer has made progress)
            if _state["starting"] or not _writer_alive():
                return False        # (R12: never a wait for a writer that is not running)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _state["stalled_at"] = _state["done"]
                return False
            _cond.wait(remaining)


def _idle() -> bool:
    """(Under `_cond`.) Nothing taken (`busy`: set when the writer dequeues a record, cleared after its last handler
    returned), nothing queued, no drop notice owed."""
    return not _queue and not _state["dropped"] and _state["busy"] is None


def _idle_by(deadline: float) -> bool:
    """(Under `_cond`.) Wait until idle, at most until `deadline`. True: idle."""
    while not _idle():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        _cond.wait(remaining)
    return True


def flush(timeout: float) -> bool:
    """A bounded wait for the queue to be empty and the writer idle, on any thread; it enters no handler, so the bound
    is real. True: idle."""
    deadline = time.monotonic() + max(0.0, timeout)
    with _cond:
        return _idle_by(deadline)


def final_flush(budget: float = 2.0) -> bool:
    """`node.run`'s ONE final bounded wait, after the last signal and report (and `atexit`'s, when nothing made it):
    the writer started if it is not running (the last safe start point), then the wait and the SEAL in one critical
    section (R11): from the moment "idle" is decided no record is accepted, so idle-and-sealed is never followed by a
    handler entry. True: idle (the process may exit normally); False: the writer is stuck (or cannot start)."""
    start_writer()
    deadline = time.monotonic() + budget
    with _cond:
        _state["exit_deadline"] = deadline
        ok = _idle_by(deadline)
        _state["sealed"] = True     # (one step with the idle decision)
    return ok


def _at_exit() -> None:
    """At interpreter exit, when no final wait was made (a standalone process): the same wait and seal, at most 2 s.
    (A seal cannot rescue a handler already stuck: `logging.shutdown()` then waits for its lock, as on main.)"""
    try:
        if not _state["sealed"]:
            final_flush(2.0)
    except Exception:
        pass


atexit.register(_at_exit)


def _reset(idle_seconds: float = 5.0) -> None:
    """(Tests) The writer idle (bounded), then an empty queue and clear counters (the seal and a failed start
    included), this thread at depth 0, and a writer running. A writer still busy after the bound (a test left a
    handler blocked) is left behind: a new generation starts a new one."""
    idle = flush(idle_seconds)
    with _cond:
        _queue.clear()
        if not idle:
            _state["gen"] += 1
            _writer[0] = None
        _state.update(done=_state["seq"], evicted=_state["seq"], dropped=0, busy=None, stalled_at=None,
                      exit_deadline=None, sealed=False, starting=False, start_failed_at=None)
        _cond.notify_all()
    _local.depth = _local.last = 0
    start_writer(force=True)
