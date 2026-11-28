"""Host-side logging (DESIGN_NOTES "Host-side logging", and "Host-side logging: one writer", which supersedes the
caller-thread drain): the package-owned logger of the session host, the node's session runner, the process-group
registry, the node and the providers.

THE guarantee: for every line logged through hostlog's loggers (`hostlog.get`), a log handler that blocks forever,
or raises, changes nothing but log output: every signal, report, save and return of the host-side code happens at
its time. Its scope is exactly that (review round 3, R19): lines logged through plain `logging` are outside it: the
rest of `agent_comms.dispatch` (the node hands its hostlog logger to the two dispatch helpers it calls,
`remove_browser_output` and `with_managed_env`), every module not converted here, and third-party libraries' own
loggers (httpx and the like), which still reach `logging`'s handlers on the calling thread. Nothing process-wide is
patched: `logging` is used as it is, through its loggers' own level checks and handlers, under the same logger names.

- A line is a deferred record, made AT THE CALL (`HostLogger`): a snapshot of reviewed fields only (`_Record`): the
  wall time, the thread, the logger's name, the level (a line the logger would not emit is not kept: the level is
  checked at the call), the message template, the caller as (file, line, function) from its frame with no source
  lookup, and the arguments reduced there: `str`, `int`, `float`, `bool` and `None` kept (also as the values of a
  copied `%`-mapping), any other object replaced by its type's name; a value rendered later (`facts`: an
  exception's reviewed integers; `later`: a reviewed function of reduced values) is kept as it is and rendered by the
  writer, never at the call (only `later()` and `facts()` make one). An exception is kept as its chain (its causes and
  contexts, bounded: CHAIN_MAX, FRAMES_MAX) of type names and traceback (file, line, function) triples: no exception
  text, no source lines, no locals. Nothing is formatted at the call.
- ONE queue (bounded: the oldest goes first when it is full, the writer says once how many went) and ONE writer: a
  daemon thread ("hostlog-writer"). It is started ONCE in production (`start_writer`, by `node.run` before
  recovery, outside every host lock; nothing restarts it: a writer that never started, or died, is a documented
  loss of log lines) and lazily only by a standalone caller at depth 0; a lifecycle thread's enqueue, or any enqueue
  under a host lock, never starts it. A start is claimed under the queue's lock by publishing a start-attempt token,
  and made outside it; the WORKER registers itself, consuming that token, before it dequeues anything; a start that
  fails or is interrupted cancels its token unless the worker already registered (no log call raises an ordinary
  failure into the host; an interrupt propagates). It is the
  ONLY thread that ever enters a handler, a filter or a formatter (`_write`), in queue order (the enqueue order). It
  holds no lock of the host-side modules, ever. A handler that raises is skipped (per handler); nothing a handler
  raises ends the writer; the record in flight counts as attempted and is never written again.
- Who waits for the writer. A LIFECYCLE thread never does (`ENQUEUE_ONLY`, registered with `enqueue_only()`; ONE
  predicate, `may_wait`): the node's main thread for the whole of `node.run` (registered before anything is built or
  recovered), the session loop, the deadline watchdog, the checkpoint sender, and the writer itself (a handler that
  logs, or calls back into the host, enqueues and returns). A STANDALONE caller (a test, a command calling the host
  directly) waits, when it leaves its outermost boundary at depth 0 (never while it holds a lock of the host-side
  modules: every such lock is entered as `with hostlog.BOUNDARY, lock:`, so an interrupt cannot leave it held; the
  two inherited exceptions are named in the R18 comment above `_Record`), for the writer to have attempted
  ITS OWN last record, for at most ACK_SECONDS (`_acknowledge`): a public call returns with its lines with the handlers (caplog) in the
  uncontended case. Best effort by statement: a stall latch (a wait that times out marks the writer stalled at the
  completion sequence it saw; nobody waits again until a LATER completion), and a record the bound evicted is
  resolved as dropped at once.
- `flush(timeout)`: a bounded wait for the queue to be empty and the writer idle, entering no handler. `node.run` makes
  ONE final bounded wait after the last signal and report (`final_flush`). It starts nothing; it SEALS first (every
  later record rejected, every pending start cancelled), then waits for the registered writer to finish what it
  accepted, so idle-and-sealed is never followed by a handler entry; a node whose writer is still stuck then ends
  without waiting for the handler (`node.run`). A standalone process's `atexit` makes the same seal and wait.
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
# a wait on anything else (a wait on this condition lets it go). A RAW lock, entered only as `with _LOCK:` (its
# acquire and release are the `with` statement's own C calls, so an interrupt on the main thread can never leave it
# held: R18), never a boundary. `_cond` is built on it for the writer's own idle wait (not the main thread: no signal
# is ever handled there) and for the notifies; the main thread never waits on it (`_wait_for`).
_LOCK = threading.Lock()
_cond = threading.Condition(_LOCK)
_queue: deque = deque()
# seq: the last record queued; done: the last one the writer attempted; evicted: the last one the bound let go;
# dropped: how many went since the writer last said so; busy: the record the writer is writing, or None; stalled_at:
# the completion sequence a timed-out wait saw (the stall latch), or None;
# exit_deadline: `final_flush`'s bound, shared with `atexit`. sealed: the final wait began (`final_flush`): every
# later record is rejected and every pending start cancelled (one-way; tests reset it). token: the current start
# attempt's token, published by its one claimant and consumed by its worker's own registration (`_register`), or
# None. `_writer[0]`: THE registered writer (set only by the worker itself), or None. (DESIGN_NOTES "Host-side
# logging: one writer, review round 2" has the writer's whole life as a state table.)
_state: dict = {"seq": 0, "done": 0, "evicted": 0, "dropped": 0, "busy": None, "stalled_at": None,
                "exit_deadline": None, "sealed": False, "token": None}
_writer: list = [None]
_TYPE_NAME = type.__dict__["__name__"]      # a type's own name, read with no code of the object's own


def _type_name(v: Any) -> str:
    return _TYPE_NAME.__get__(type(v))


# ---------------------------------------------------------------- the boundary (the per-thread depth)


class _Boundary:
    """Entered and left in this one place: the depth goes up on entry and down on exit; leaving the outermost one
    (depth 0 again) is where a standalone caller waits for its own lines (`_acknowledge`), whether its body returned
    or raised. A lifecycle thread waits for nothing there.

    (A4) An interrupt can land in this Python code (only on the main thread: signals are handled nowhere else) and
    leave the depth one too HIGH, never too low (the decrement is the last step before the acknowledgement). That
    fails closed: a depth above 0 means "never waits". So does a stale `enqueue_only()` registration. Neither is
    reset in production (the node's main thread is enqueue-only for the rest of its process); the tests' `_reset`,
    run between tests with no lock held, is the one reset."""

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


# (Review round 3, R18) A lock of the host-side modules under which a line can be logged (the runner lock, the cutoff
# lock, the registry lock, the report lock, the acknowledgement lock, a steer attempt's record lock) is a NATIVE lock,
# entered as `with hostlog.BOUNDARY, lock:` (the boundary FIRST: the lock's own acquire and release are C code, made
# inside the boundary, so the release always comes before the boundary's exit, and no acknowledgement is ever made
# while the lock is held). The few explicit `acquire()`/`release()` pairs are the native methods, inside a
# `with hostlog.BOUNDARY:`, in main's own shape. No Python wrapper and no generator holds one of these locks.
# So these converted locks, and hostlog's own `_LOCK`, cannot be left held by an interrupt. NOT converted, inherited
# from main and still entered through Python code (an interrupt there could strand their own lock, as on main):
# the stdlib's `Event` internals, which `_Stopping.set()` enters on the main thread at shutdown, and
# `checkpoint_cond`, a `threading.Condition` entered through stdlib Python code on the session loop and checkpoint
# sender threads.


# ---------------------------------------------------------------- the record, made at the call


class _Record:
    __slots__ = ("seq", "created", "thread", "thread_name", "name", "level", "msg", "args", "caller", "exc", "extra")


_MINT = object()        # (R22) held by `later()` and `facts()` only: a construction guard, nothing more


class _Later:
    """A value the WRITER renders (`__str__`, in a `%s`): a reviewed function and reduced values taken at the call.
    Nothing of it is rendered at the call. Only `later()` and `facts()` make one the writer renders (R22: one made
    any other way is reduced to its type's name at the call, `_reduce`)."""

    __slots__ = ("fn", "args", "minted")

    def __init__(self, fn: Callable[..., str], args: tuple, mint: object = None) -> None:
        self.fn, self.args, self.minted = fn, args, mint is _MINT

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
    return _Later(fn, tuple(_reduce(a) for a in args), _MINT)


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
    return _Later(_render_facts, (_type_name(exc), status, errno, returncode), _MINT)


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
    if t is str or t is int or t is float or t is bool or v is None:
        return v
    if t is _Later:
        return v if v.minted is True else _type_name(v)     # (R22: only `later()`/`facts()` make a rendered value)
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


CHAIN_MAX = 8       # (R20) the exceptions kept from one chain: the logged one, then its causes and contexts
FRAMES_MAX = 200    # (R20) the frames kept from one chain, all its exceptions together
_CAUSE, _CONTEXT, _SUPPRESS, _TRACEBACK = (BaseException.__dict__[k] for k in (
    "__cause__", "__context__", "__suppress_context__", "__traceback__"))     # (A6: BaseException's own descriptors)


# (R20, Sol's should) Why a chain was cut, as the snapshot keeps it: a REASON only (never a formatted label, which the
# writer renders, `_exc_text`), and only when something was actually left out.
CUT_CYCLE, CUT_CHAIN, CUT_FRAMES = "cycle", "chain", "frames"
_CUTS = (CUT_CYCLE, CUT_CHAIN, CUT_FRAMES)


def _frames(tb: Any, room: int) -> tuple[list, bool]:
    """(the frames kept, at most `room`; whether any frame was left out)."""
    frames = []
    while tb is not None and len(frames) < room:
        code = tb.tb_frame.f_code
        frames.append((code.co_filename, tb.tb_lineno, code.co_name))
        tb = tb.tb_next
    return frames, tb is not None


def _exception(exc_info: Any) -> tuple | None:
    """(type name, frames, chain, cut) of the exception, or None: nothing formatted, no text, no source. `frames` are
    the logged exception's own (file, line, function) triples; `chain` is every exception kept, the logged one first,
    as (type name, frames, how the NEXT one is linked to it: "cause", "context" or None); `cut` is the tuple of the
    reasons it was cut (`_CUTS`: a cycle; an exception left out past CHAIN_MAX; a frame left out past FRAMES_MAX),
    empty when nothing was: reasons only, the label is the writer's. R20 (A6): walked through BaseException's own
    descriptors (`__cause__`, `__context__`, `__suppress_context__`, `__traceback__`), so no code of the exception's
    own runs; a cycle is found by identity; at most CHAIN_MAX exceptions and FRAMES_MAX frames in all."""
    if exc_info is True or (exc_info and not isinstance(exc_info, (BaseException, tuple))):
        exc_info = sys.exc_info()
    if isinstance(exc_info, BaseException):
        value, tb = exc_info, _TRACEBACK.__get__(exc_info)
    elif isinstance(exc_info, tuple) and len(exc_info) == 3:
        value, tb = exc_info[1], exc_info[2]
    else:
        return None
    if not isinstance(value, BaseException):
        return None
    chain, seen, room, cut, e = [], set(), FRAMES_MAX, [], value
    while e is not None:
        if id(e) in seen:
            cut.append(CUT_CYCLE)
            break
        if len(chain) == CHAIN_MAX:
            cut.append(CUT_CHAIN)       # (an exception is left out: `e` is a further one)
            break
        seen.add(id(e))
        frames, left_out = _frames(tb if e is value else _TRACEBACK.__get__(e), room)
        room -= len(frames)
        if left_out and CUT_FRAMES not in cut:
            cut.append(CUT_FRAMES)
        cause = _CAUSE.__get__(e)
        if cause is not None:
            nxt, link = cause, "cause"
        elif not _SUPPRESS.__get__(e):
            nxt, link = _CONTEXT.__get__(e), "context"
        else:
            nxt, link = None, None
        chain.append((_type_name(e), tuple(frames), link if nxt is not None else None))
        e = nxt
    return chain[0][0], chain[0][1], tuple(chain), tuple(cut)


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
    with _LOCK:
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


_LINKS = {"cause": "\nThe above exception was the direct cause of the following exception:\n\n",
          "context": "\nDuring handling of the above exception, another exception occurred:\n\n"}


def _exc_text(exc: tuple) -> str:
    """The chain in the order stock `logging` prints it (the innermost first, each followed by how the next one
    relates to it), each as its frames and its type's name only: no exception text, no source lines."""
    chain, cut = exc[2], exc[3]
    labels = {CUT_CYCLE: "a cycle", CUT_CHAIN: f"more than {CHAIN_MAX} exceptions",
              CUT_FRAMES: f"more than {FRAMES_MAX} frames"}       # (rendered here, by the writer: R20)
    parts = [f"(the chain is cut here: {'; '.join(labels[c] for c in cut)})\n"] if cut else []
    for i in range(len(chain) - 1, -1, -1):
        name, frames, _ = chain[i]
        parts += ["Traceback (frames only: no exception text, no source lines):\n",
                  *(f'  File "{fn}", line {ln}, in {fu}\n' for fn, ln, fu in frames), name]
        if i > 0:
            parts.append(_LINKS[chain[i - 1][2]])
    return "".join(parts)


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
    if type(args) is dict:
        args = (args,)      # (as `Logger._log` hands a mapping over: logging's own rule then makes it the args)
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
    """(Under `_LOCK`.) A writer is REGISTERED (it registered itself: `_register`) and its thread runs."""
    w = _writer[0]
    return w is not None and w.is_alive()


def _pending() -> bool:
    """(Under `_LOCK`.) A start is pending: its start-attempt token is published and not yet consumed by its worker's
    registration nor cancelled by its starter; the seal cancels every pending start (a worker that wakes after it
    leaves without touching the queue)."""
    return _state["token"] is not None and not _state["sealed"]


def _can_dequeue() -> bool:
    """(Under `_LOCK`.) Some worker can dequeue: a registered writer, or a pending start whose worker may yet
    register. "Writer-less" is its negation: no worker CAN dequeue (a cancelled worker may still wake up, and leaves)."""
    return _writer_alive() or _pending()


def _start_writer(lazy: bool = False) -> None:
    """THE start (A1): claim under `_LOCK`, make and start the writer's `Thread` outside it.
    - The claim is refused after the seal, while a writer is registered and while a start is pending (concurrent
      claimants return); `lazy` (a standalone caller) claims only when records wait. The claimant publishes a new
      start-attempt TOKEN.
    - The worker accounts for itself (`_register`, its first act): nothing this thread does after `start()` is needed
      for the worker to be tracked.
    - If making the `Thread` or starting it fails (an ordinary failure: contained) or an interrupt arrives (it
      propagates), the token is cancelled under `_LOCK` UNLESS the worker has already consumed it by registering: a
      cleanup never undoes a registration the worker won, and a late worker whose token was cancelled leaves without
      touching the queue.
    (One residual, bounded: an interrupt inside the claim's own statements, before `token` is bound here, or inside
    the cancellation, leaves a start pending that no worker consumes; the final wait seals, which cancels it.)"""
    token, started = None, False
    try:
        with _LOCK:
            if not _state["sealed"] and not _can_dequeue() and (not lazy or _queue or _state["dropped"]):
                token = _state["token"] = object()
        if token is None:
            return
        _local.token = token        # (this thread's own start: its acknowledgement may wait for its worker)
        w = threading.Thread(target=_write_loop, args=(token,), name=WRITER_NAME, daemon=True)
        w.start()
        started = True
    except Exception:
        pass        # (the OS refused the thread, or it could not be made: no writer; the token is cancelled below)
    finally:
        if token is not None and not started:
            with _LOCK:
                if _state["token"] is token:
                    _state["token"] = None      # (cancelled: not consumed, so the worker had not registered)
                _cond.notify_all()


def _lazy_start() -> None:
    """(A standalone caller at depth 0, so never under a host lock, never on a lifecycle thread.) Start the writer if
    records wait and no worker can dequeue: the standalone start point (caplog); production never reaches it."""
    _start_writer(lazy=True)


def start_writer() -> bool:
    """`node.run`'s ONE eager start (before recovery, outside every host lock). Nothing restarts the writer in a node
    process: a writer that never started, or died, is a documented loss of log lines for the rest of that process.
    True: a writer is registered or its start is pending. It never waits."""
    _start_writer()
    with _LOCK:
        return _can_dequeue()


def _register(token: object) -> bool:
    """The worker's FIRST act, under `_LOCK` (A1): consume its still-current token and register as THE writer. A
    sealed queue, or a token that is not current (cancelled by its starter, or superseded): it leaves at once without
    touching the queue. True: registered."""
    with _LOCK:
        if _state["sealed"] or _state["token"] is not token:
            return False
        _state["token"] = None
        _writer[0] = threading.current_thread()
        _cond.notify_all()
        return True


def _write_loop(token: object) -> None:
    """The writer: registers itself first (`_register`); then every record, in queue order, handed to the handlers;
    never waits for anyone (a lifecycle thread); holds no lock of the host-side modules. Nothing a handler raises
    ends it. If its own code fails outside a handler it dies: it deregisters and wakes every waiter as it goes (A3,
    registered → dead), and nothing restarts it in production. Let go (the tests' reset): it leaves without dequeuing
    again."""
    me = threading.current_thread()
    ENQUEUE_ONLY.add(threading.get_ident())
    try:
        if not _register(token):
            return
        while True:
            with _LOCK:
                while _writer[0] is me and not _queue and not _state["dropped"]:
                    _cond.wait()
                if _writer[0] is not me:
                    return
                dropped, _state["dropped"] = _state["dropped"], 0
                if dropped:
                    r = _Record()
                    r.seq, r.created, r.name, r.level = 0, time.time(), __name__, logging.WARNING
                    r.thread, r.thread_name = me.ident, me.name
                    r.msg = ("%d deferred log line(s) were dropped "
                             "(the queue was full, or a line could not be made a record)")
                    r.args = (dropped,)
                    r.caller, r.exc, r.extra = ("(hostlog)", 0, "writer"), None, None
                else:
                    r = _queue.popleft()
                _state["busy"] = r.seq      # (published in the same step as the dequeue)
            lost = False
            try:
                _write(r)
            except BaseException:       # (the writer is not the main thread: nothing it is handed ends it)
                lost = r.seq != 0       # (it failed before any handler, which `_write` contains each on its own: counted
                #                          as dropped and said once, like the bound's drops; never the notice itself)
            finally:
                with _LOCK:
                    if _writer[0] is me:
                        if lost:
                            _state["dropped"] += 1
                        _state["busy"] = None
                        if r.seq > _state["done"]:
                            _state["done"] = r.seq
                        if _state["stalled_at"] is not None and _state["done"] > _state["stalled_at"]:
                            _state["stalled_at"] = None     # (a LATER completion: the latch clears)
                        _cond.notify_all()
    finally:
        with _LOCK:
            if _writer[0] is me:        # (registered → dead)
                _writer[0] = None
                _state["busy"] = None
            _cond.notify_all()          # (every waiter re-checks: no writer, so no wait)
        ENQUEUE_ONLY.discard(threading.get_ident())


def _acknowledge() -> bool:
    """A standalone caller at depth 0: wait, at most ACK_SECONDS, for the writer to have attempted this thread's own
    last record. Not on a lifecycle thread, not inside a boundary, not while the writer is latched stalled; released at
    once when that record was evicted by the bound, and when no writer is registered (unless the start pending is this
    thread's own, whose worker it waits for, within the same bound). True: it was attempted (or dropped)."""
    if not may_wait():
        return False
    _lazy_start()
    target = getattr(_local, "last", 0)
    own = getattr(_local, "token", None)
    deadline = time.monotonic() + ACK_SECONDS

    def check():
        if _state["done"] >= target or _state["evicted"] >= target:
            return True
        if _state["stalled_at"] is not None:
            return False        # (the latch: nobody waits until the writer has made progress)
        if not _writer_alive() and not (own is not None and _state["token"] is own and _pending()):
            return False        # (R12: never a wait for a writer that is not running, nor for another's start)
        return None
    out = _wait_for(check, deadline)
    if out is None:
        with _LOCK:
            out = check()
            if out is None:
                _state["stalled_at"] = _state["done"]
                return False
    return out


def _idle() -> bool:
    """(Under `_LOCK`.) Nothing taken (`busy`: set when the writer dequeues a record, cleared after its last handler
    returned), nothing queued, no drop notice owed."""
    return not _queue and not _state["dropped"] and _state["busy"] is None


def _wait_for(check: Callable[[], Any], deadline: float) -> Any:
    """Wait until `check()` (called under `_LOCK`) returns something other than None, at most until `deadline`
    (None then). On the MAIN thread, where signals are handled, it polls OUTSIDE the lock (A3): one short C-level
    `with _LOCK:` per look, then a sleep; never `Condition.wait`, so an interrupt there can neither leave the lock held
    nor be masked. On any other thread (no signal is ever handled there): `Condition.wait`."""
    if threading.current_thread() is threading.main_thread():
        step = 0.0005
        while True:
            with _LOCK:
                out = check()
            if out is not None:
                return out
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            time.sleep(min(step, remaining))
            step = min(step * 2, 0.01)
    with _LOCK:
        while True:
            out = check()
            if out is not None:
                return out
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            _cond.wait(remaining)


def _settled(writerless_ok: bool = False) -> Callable[[], Any]:
    """(A check for `_wait_for`.) True once idle; with `writerless_ok` (the final wait), also as soon as no worker can
    dequeue (`_can_dequeue`): no handler of ours can then be entered or stuck, so the process may exit normally (what
    is queued is lost) instead of waiting out a budget nothing can use."""
    def check():
        if _idle():
            return True
        if writerless_ok and not _can_dequeue():
            return True     # (writer-less: a writer that never started or died costs only its queued lines)
        return None
    return check


def flush(timeout: float) -> bool:
    """A bounded wait for the queue to be empty and the writer idle, on any thread; it enters no handler, so the bound
    is real. True: idle."""
    return _wait_for(_settled(), time.monotonic() + max(0.0, timeout)) is True


def final_flush(budget: float = 2.0) -> bool:
    """`node.run`'s ONE final bounded wait, after the last signal and report (and `atexit`'s, when nothing made it).
    It starts nothing (R15). Its deadline is taken first; then, under `_LOCK`, it SEALS first (every later record
    rejected, every pending start cancelled: only the registered writer finishes the records accepted before) and
    waits, at most until the deadline, for idle or writer-less. True: no handler of ours is running, so the process
    may exit normally (idle; or writer-less, what was queued is lost); False: the writer is still in a handler at the
    deadline (the stalled exit)."""
    deadline = time.monotonic() + budget        # (first: before anything that can block)
    with _LOCK:
        _state["sealed"] = True     # (first: from here on nothing is accepted and nothing starts)
        _state["exit_deadline"] = deadline
        _cond.notify_all()
    return _wait_for(_settled(writerless_ok=True), deadline) is True     # (A3: on the main thread, a poll)


def _at_exit() -> None:
    """At interpreter exit, when no final wait was made (a standalone process): the same seal and wait, at most 2 s.
    (A seal cannot rescue a handler already stuck: `logging.shutdown()` then waits for its lock, as on main.)"""
    try:
        if not _state["sealed"]:
            final_flush(2.0)
    except Exception:
        pass


atexit.register(_at_exit)


def _reset(idle_seconds: float = 5.0) -> None:
    """(Tests) The writer idle (bounded), then an empty queue and clear counters (the seal and a pending start
    included), this thread at depth 0, and a writer registered (bounded). A writer still busy after the bound (a test
    left a handler blocked) is let go: it never dequeues again, and a new one starts. (No worker that can dequeue: no
    wait at all.)"""
    idle = _wait_for(_settled(writerless_ok=True), time.monotonic() + idle_seconds) is True
    with _LOCK:
        _queue.clear()
        if not idle:
            _writer[0] = None
        _state.update(done=_state["seq"], evicted=_state["seq"], dropped=0, busy=None, stalled_at=None,
                      exit_deadline=None, sealed=False, token=None)
        _cond.notify_all()
    _local.depth = _local.last = 0
    _local.token = None
    ENQUEUE_ONLY.discard(threading.get_ident())     # (A4: a stale registration, as a stale depth, is reset here only)
    start_writer()
    _wait_for(lambda: True if _writer_alive() or not _pending() else None, time.monotonic() + 5.0)
