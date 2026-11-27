"""The dynamic guard of host-side logging (DESIGN_NOTES "Host-side logging: one writer"): a log handler is entered only
on the writer thread, and never by a thread that holds a lock of the host-side modules (the runner lock, the cutoff
lock, the registry lock, the report lock, the ack lock, a steer attempt's record lock).

`Guard` instruments the three locks of a host (and of its runner, when there is one) so each knows whether the CALLING
thread owns it, and puts a handler on the package's logger that records, at every entry, which of them the entering
thread holds. A static check that the host-side modules take their loggers from `hostlog` cannot catch a helper that
logs synchronously; this does, on whatever route it runs across."""
import logging
import threading
import time

try:
    from agent_comms import hostlog
except ImportError:         # (main e6ebf95, before this PR: the reproduction run)
    hostlog = None


class OwnerLock:
    """A lock (or an RLock) that knows which thread owns it."""

    def __init__(self, inner):
        self._inner, self._owner, self._count = inner, None, 0

    def acquire(self, blocking=True, timeout=-1):
        ok = self._inner.acquire(blocking, timeout)
        if ok:
            self._owner = threading.get_ident()
            self._count += 1
        return ok

    def release(self):
        self._count -= 1
        if self._count == 0:
            self._owner = None
        self._inner.release()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *a):
        self.release()

    def locked(self):
        return self._count > 0

    def _is_owned(self):
        return self._owner == threading.get_ident()

    mine = _is_owned


def own(obj, attr):
    """Replace `obj.attr` (a host lock) by one that knows its owner, keeping the host's own wrapper (`HostLock`) when
    there is one: the instrumented lock is the wrapper's inner lock."""
    lock = getattr(obj, attr)
    if hostlog is not None and isinstance(lock, hostlog.HostLock):
        if not isinstance(lock._lock, OwnerLock):
            lock._lock = OwnerLock(lock._lock)
        return lock._lock
    if not isinstance(lock, OwnerLock):
        lock = OwnerLock(lock)
        setattr(obj, attr, lock)
    return lock


def runner_lock():
    """A stand-in for the node's runner lock, as the node holds it around its host calls."""
    inner = OwnerLock(threading.Lock())
    return (hostlog.HostLock(inner) if hostlog is not None else inner), inner


class _Owned:
    """Whether the calling thread owns `lock`: an RLock or a Condition says so itself (`_is_owned`); a plain Lock is
    instrumented (`own`)."""

    def __init__(self, obj, attr):
        lock = getattr(obj, attr)
        inner = lock._lock if hostlog is not None and isinstance(lock, hostlog.HostLock) else lock
        if hasattr(inner, "_is_owned"):
            self.mine = lock._is_owned
        else:
            self.mine = own(obj, attr).mine


# Every lock of the host-side modules (DESIGN_NOTES "Host-side logging: review round 1", the lock table): the runner's
# and the host's, by attribute; each held steer attempt's record lock is looked at when a handler is entered.
RUNNER_LOCKS = ("lock", "report_lock", "latch_lock", "ack_lock", "checkpoint_cond")
HOST_LOCKS = ("_steer_stop_lock", "usage_lock")


class Guard:
    """The handler: `mode` "record" (records only), "blocks" (stalls a little at every record) or "raises". It
    records, at every entry, which lock of the host-side modules the ENTERING thread holds."""

    def __init__(self, host=None, runner=None, mode="record", extra=()):
        self.locks = list(extra)
        if runner is not None:
            names = {"lock": "runner", "report_lock": "report", "latch_lock": "latch", "ack_lock": "ack",
                     "checkpoint_cond": "checkpoint"}
            self.locks += [(names[a], _Owned(runner, a)) for a in RUNNER_LOCKS if hasattr(runner, a)]
            host = runner.host
        if host is not None:
            names = {"_steer_stop_lock": "cutoff", "usage_lock": "usage"}
            self.locks += [(names[a], _Owned(host, a)) for a in HOST_LOCKS if hasattr(host, a)]
            self.locks.append(("registry", _Owned(host.groups, "lock")))
        self.host = host
        self.mode, self.seen, self.violations = mode, [], []
        guard = self

        class H(logging.Handler):
            def emit(self, rec):
                held = [name for name, lock in guard.locks if lock.mine()]
                if guard.host is not None and any(s.steer is not None and s.steer.lock._is_owned()
                                                  for s in list(guard.host.sessions.values())):
                    held.append("slot")
                if hostlog is not None and hasattr(hostlog, "WRITER_NAME") and (
                        threading.current_thread().name != hostlog.WRITER_NAME):
                    held.append("not the writer thread")    # (one writer: the only thread that enters a handler)
                guard.seen.append((rec.name, rec.getMessage(), held))
                if held:
                    guard.violations.append((rec.getMessage(), held))
                if guard.mode == "raises":
                    raise RuntimeError("a log handler failed")
                if guard.mode == "blocks":
                    time.sleep(0.005)
        self.handler = H(logging.DEBUG)
        self.logger = logging.getLogger("agent_comms")

    def __enter__(self):
        self.level = self.logger.level
        self.logger.setLevel(logging.DEBUG)
        self.logger.addHandler(self.handler)
        return self

    def __exit__(self, *a):
        if hostlog is not None and hasattr(hostlog, "flush"):
            hostlog.flush(10)       # (the writer has written what the route queued)
        self.logger.removeHandler(self.handler)
        self.logger.setLevel(self.level)
