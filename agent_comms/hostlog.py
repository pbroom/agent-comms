"""Host-side logging (DESIGN_NOTES "Host-side logging"): the package-owned logger of the session host, the node's
session runner, the process-group registry, the node and the providers.

THE invariant: no log handler runs, and nothing is formatted, while the calling thread holds a host lock (the runner
lock, the cutoff lock, the registry lock), on any thread, for any host-side line. Nothing process-wide is patched:
`logging` is used as it is, through its loggers' own level checks and handlers, under the same logger names.

- A line is a deferred record, made AT THE CALL (`HostLogger`): a snapshot of reviewed fields only (`_Record`): the
  wall time, the thread, the logger's name, the level (a line the logger would not emit is not kept: the level is
  checked at the call), the message template, the caller as (file, line, function) from its frame with no source
  lookup, and the arguments reduced there: `str`, `int`, `float`, `bool` and `None` kept (also as the values of a
  copied `%`-mapping), any other object replaced by its type's name. An exception is kept as its type's name and its
  traceback's (file, line, function) triples: no exception text, no source lines, no locals. Nothing of the caller's
  objects is held, so nothing is formatted later from an object that has changed since, or runs code of its own.
- The records go into ONE queue (bounded: the oldest goes first when it is full, and the next drain says once how
  many went) and are written, in the order they were queued, by THE drain (`drain`): on the calling thread, when it
  leaves its OUTERMOST boundary (`BOUNDARY`: depth 0 again), whether it returns or raises. The writer makes a real
  `logging.LogRecord` (its time and caller the call's) and hands it to the logger's handlers, one by one: a handler
  that raises is caught and the next goes on; nothing reaches the host's code.
- The boundaries, and so the per-thread depth, are kept in ONE place: `BOUNDARY` (a context manager, and `entry`, a
  decorator: every public method of the session host), entered by every host lock as it is taken (`HostLock`: the
  runner lock, the cutoff lock, the registry lock). A thread that holds a host lock is therefore always inside a
  boundary, and the drain, which runs only at depth 0, never runs under one. A line logged with no boundary open is
  its own: queued and drained at once. Leaving a host lock at depth 0 writes everything queued (so the loop writes
  the watchdog's lines when it lets go of the runner lock); any other exit writes only when the thread has a record
  of its own pending (an entry point that logged nothing writes and waits for nothing).
- A watchdog thread (`WATCH_THREADS`: node_sessions registers its deadline watchdog) never drains: its records wait
  for the loop. A drain on a thread that is already draining (a handler that logs, or that calls back into the host)
  leaves what it adds to that ACTIVE drainer, which writes until the queue is empty: it never waits on itself. A
  drain on another thread waits only while a record of its OWN is not yet written (by the active drainer, or by
  itself once that one is done; a thread with nothing of its own pending never waits), holding no host lock (it is at
  depth 0) and no lock of this module: no cycle is possible, and the watchdog, which never drains, never waits on a
  drainer. The serialization is the drainer's turn (`_state["drainer"]`), taken and handed back under the queue's own
  short lock; it is not a lock held while a host lock is taken, except that a handler may itself call back into the
  host from the active drainer's thread, which cannot deadlock for the reason just given.
"""
from __future__ import annotations

import atexit
import functools
import logging
import sys
import threading
import time
from collections import deque
from typing import Any, Callable

# The threads that run the node's deadline watchdog (node_sessions registers its own; session_host re-exports it).
# On them the host saves nothing and writes no transcript line (session_host `on_watch`), and no drain is made.
WATCH_THREADS: set[int] = set()
QUEUE_MAX = 2000        # records kept for the drain at most; the oldest goes first (said once at the next drain)

_local = threading.local()      # `depth`: the boundaries this thread is inside
# The queue's own lock: held only to append, to pop, and to hand the drainer's turn over, never across a handler, a
# host call or a wait on anything else (a wait on this condition lets it go).
_cond = threading.Condition(threading.Lock())
_queue: deque = deque()
_state = {"seq": 0, "done": 0, "dropped": 0, "drainer": None}
_TYPE_NAME = type.__dict__["__name__"]      # a type's own name, read with no code of the object's own


def _type_name(v: Any) -> str:
    return _TYPE_NAME.__get__(type(v))


# ---------------------------------------------------------------- the boundary (the per-thread depth)


class _Boundary:
    """Entered and left in this one place: the depth goes up on entry and down on exit; leaving the outermost one
    (depth 0 again) drains, on the thread that leaves it, whether its body returned or raised."""

    def __enter__(self) -> "_Boundary":
        _local.depth = getattr(_local, "depth", 0) + 1
        return self

    def __exit__(self, *exc) -> bool:
        self.leave()
        return False

    def leave(self, everything: bool = False) -> None:
        """The exit: at depth 0 again, the drain (`everything`: a host lock let go, whose holder writes what any
        thread queued, the watchdog's records included; otherwise the drain writes only when this thread has a record
        of its own not yet written, so an entry point that logged nothing, such as `attempts` under the node's ack
        lock, never writes or waits for other threads' lines)."""
        depth = getattr(_local, "depth", 0) - 1
        _local.depth = depth if depth > 0 else 0
        if depth <= 0:
            try:
                drain(everything=everything)
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


class HostLock:
    """A host lock (the runner lock, the cutoff lock, the registry lock): taking it enters a boundary, letting it go
    leaves that boundary, so no drain (no handler) can run on a thread while it holds one. The lock's own semantics
    are the wrapped lock's (a Lock, or an RLock, re-entered as such)."""

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
        BOUNDARY.leave(everything=True)

    def __enter__(self) -> bool:
        return self.acquire()

    def __exit__(self, *exc) -> bool:
        self.release()
        return False

    def locked(self) -> bool:
        locked = getattr(self._lock, "locked", None)
        if locked is not None:
            return locked()
        if self._lock.acquire(blocking=False):      # (an RLock before 3.14: no `locked`)
            self._lock.release()
            return False
        return True

    def _is_owned(self) -> bool:
        return self._lock._is_owned()


# ---------------------------------------------------------------- the record, made at the call


class _Record:
    __slots__ = ("seq", "created", "thread", "thread_name", "name", "level", "msg", "args", "caller", "exc", "extra")


def _reduce(v: Any) -> Any:
    """A reviewed value: kept if it is a str, int, float, bool or None (a subclass of one as the base type's own value,
    read with no code of the subclass); otherwise its type's name."""
    t = type(v)
    if t is str or t is int or t is float or t is bool or v is None:
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
    r = _Record()
    r.created, r.name, r.level = time.time(), name, level
    t = threading.current_thread()
    r.thread, r.thread_name = t.ident, t.name
    try:
        r.msg = msg if type(msg) is str else _reduce(msg)
        r.args = _reduce_args(args)
        r.caller = _caller(stacklevel)
        r.exc = _exception(exc_info) if exc_info else None
        r.extra = ({k: _reduce(v) for k, v in extra.items() if type(k) is str}
                   if type(extra) is dict and extra else None)
    except Exception as e:      # (a snapshot that fails keeps a line saying so, never raises into the host)
        r.msg, r.args, r.exc, r.extra = "a log line could not be kept (%s)", (_type_name(e),), None, None
        r.caller = ("(unknown file)", 0, "(unknown function)")
    with _cond:
        _state["seq"] += 1
        r.seq = _local.last = _state["seq"]
        if len(_queue) >= QUEUE_MAX:
            _queue.popleft()
            _state["dropped"] += 1
        _queue.append(r)


class HostLogger:
    """A host-side module's logger: `logging.getLogger(name)`'s level checks and handlers, with every line made into
    a deferred record at the call and written by the drain (the module text). The API is the part of
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
            with BOUNDARY:          # (a line logged outside every boundary is its own: drained when this exits)
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
    rec = logger.makeRecord(r.name, r.level, fn, ln, r.msg, r.args, None, func=func, extra=r.extra)
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
                except Exception:       # (one handler's failure never stops the others, nor the drain)
                    pass
        c = c.parent if c.propagate else None
    if found == 0 and logging.lastResort is not None and rec.levelno >= logging.lastResort.level:
        try:
            logging.lastResort.handle(rec)
        except Exception:
            pass


def drain(timeout: float | None = None, everything: bool = False) -> bool:
    """THE drain, and the one place that decides whether to drain: every queued record written, in queue order, on
    this thread; called when a thread leaves its outermost boundary (and by `atexit`, bounded). Nothing on a watchdog
    thread, inside a boundary, or on a thread that is already the active drainer (what is added meanwhile is that
    drainer's to write). What it waits for: its OWN records (`everything`: all records queued before the call); with
    none pending it does nothing. Another thread's drain under way: it waits only until those are written (`timeout`:
    at most so long), then leaves the rest to that drainer. Never raises. True: what it waited for is written."""
    me = threading.get_ident()
    if me in WATCH_THREADS or getattr(_local, "depth", 0) > 0:
        return False
    deadline = None if timeout is None else time.monotonic() + timeout
    with _cond:
        if _state["drainer"] == me:
            return False        # (nested: the active drainer, on this very thread, writes it)
        target = _state["seq"] if everything else getattr(_local, "last", 0)
        while _state["drainer"] is not None:
            if _state["done"] >= target:
                return True     # (its records are written: the active drainer writes the rest)
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                return False
            _cond.wait(remaining)
        if (not _queue and not _state["dropped"]) or _state["done"] >= target:
            return True
        _state["drainer"] = me
    try:
        while True:
            with _cond:
                dropped, _state["dropped"] = _state["dropped"], 0
                r = _queue.popleft() if _queue and not dropped else None
            if dropped:
                said = _Record()
                said.seq, said.created, said.name, said.level = 0, time.time(), __name__, logging.WARNING
                t = threading.current_thread()
                said.thread, said.thread_name = t.ident, t.name
                said.msg, said.args = "%d deferred log line(s) were dropped: the queue was full", (dropped,)
                said.caller, said.exc, said.extra = ("(hostlog)", 0, "drain"), None, None
                r = said
            if r is None:
                return True
            try:
                _write(r)
            except Exception:
                pass
            with _cond:
                if r.seq > _state["done"]:
                    _state["done"] = r.seq
                _cond.notify_all()
    finally:
        with _cond:
            _state["drainer"] = None
            _cond.notify_all()


def _at_exit() -> None:
    """Best effort at interpreter exit: what is queued is written, waiting for another drainer for 2 s at most."""
    try:
        drain(timeout=2.0, everything=True)
    except Exception:
        pass


atexit.register(_at_exit)


def _reset() -> None:
    """(Tests) An empty queue, no drainer, this thread at depth 0."""
    with _cond:
        _queue.clear()
        _state.update(done=_state["seq"], dropped=0, drainer=None)
        _cond.notify_all()
    _local.depth = _local.last = 0
