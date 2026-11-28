"""Shutdown: a repeated signal is recorded, not raised (DESIGN_NOTES, the section of that name).

The node's ONE handler for SIGTERM and SIGINT (`node.ShutdownLatch.on_signal`) latches "shutting down" on the FIRST
signal before it raises KeyboardInterrupt; every later signal only counts and returns. The same latch is set on every
other route into cleanup, and never cleared. These tests drive REAL signals in child processes of the test (each
signals only its own child, through its handle, or the child signals itself; every wait is bounded; the child is
killed and reaped through its handle on every path), plus unit tests of the latch and a static pin of the handler."""
import ast
import os
import queue
import signal
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

from test_hosted_sessions import senv  # noqa: F401 (the fixture)

ROOT = Path(__file__).parents[1]


# ---------------------------------------------------------------- the stub hub and the child

@contextmanager
def _stub_hub():
    """A local hub that answers every POST with a hello (so the node starts) and fails every GET (500): the node's
    loop says `hub unreachable` and the session loop's fetch fails, so neither ever gets work."""
    import json
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Hub(BaseHTTPRequestHandler):
        def _send(self, code, body):
            data = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            self._send(200, {"node": "stub", "projects": [], "runners": []})

        def do_GET(self):
            self._send(500, {"detail": "unavailable"})

        def log_message(self, *a):
            pass
    hub = ThreadingHTTPServer(("127.0.0.1", 0), Hub)
    threading.Thread(target=hub.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{hub.server_address[1]}"
    finally:
        hub.shutdown()
        hub.server_close()


def _node_child(tmp_path, script, *args, ready, first, wait, sessions):
    """Run `script` as the test's OWN child against a stub hub (argv[1] is the hub's URL); once it printed `ready`,
    send it `first` (a real signal, through its handle). Every wait is bounded and the child is killed and reaped
    through its handle on every path. Returns (status, or None if it did not end within `wait`; its stdout lines;
    the tail of its stderr)."""
    status, out, err, _ = _drive(tmp_path, script, *args, steps=[(lambda out, mark: ready in out, first)], wait=wait,
                                 sessions=sessions)
    return status, out, err


def _ticks_since(n):
    """A step's condition: at least `n` loop ticks printed since the previous signal was sent."""
    return lambda out, mark: sum(1 for x in out[mark:] if x == "tick") >= n


def _drive(tmp_path, script, *args, steps, wait, sessions, root=ROOT):
    """`_node_child` with several steps: for each (until, sig), read the child's lines until `until(out, mark)` holds
    (`mark`: how many lines had been read when the previous signal was sent; at most 60 s per step), then send it
    `sig` (a real signal, through its handle). After the last step, wait at most `wait` for it to end. `root` is the
    checkout the child imports `agent_comms` from. Returns (status or None, its stdout lines, the tail of its stderr,
    how many steps' conditions were met)."""
    home = tmp_path / "home"
    home.mkdir()
    if sessions:
        (home / "board.toml").write_text("[dispatch.sessions]\nenabled = true\n")
    path = tmp_path / "node_child.py"
    path.write_text(script)
    env = {**os.environ, "PYTHONPATH": str(root), "AGENT_COMMS_HOME": str(home)}
    status, out, reached, mark = None, [], 0, 0
    with _stub_hub() as url:
        child = subprocess.Popen([sys.executable, str(path), url, *args], stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, text=True, env=env, cwd=str(tmp_path))
        lines: queue.Queue = queue.Queue()

        def read():
            for line in child.stdout:
                lines.put(line.rstrip("\n"))
            lines.put(None)
        reader = threading.Thread(target=read, daemon=True)
        reader.start()
        try:
            ended = False
            for until, sig in steps:
                deadline = time.monotonic() + 60
                while not until(out, mark):
                    try:
                        line = lines.get(timeout=max(0.0, deadline - time.monotonic()))
                    except queue.Empty:
                        break
                    if line is None:
                        ended = True
                        break
                    out.append(line)
                if not until(out, mark):
                    break
                reached += 1
                mark = len(out)
                child.send_signal(sig)          # (the test's own child, and only it)
            if reached == len(steps) or ended:
                try:
                    status = child.wait(wait)
                except subprocess.TimeoutExpired:
                    status = None
        finally:
            if child.poll() is None:
                child.kill()                    # (through its own handle)
                child.wait(10)
            reader.join(10)                     # (bounded: the child is gone, so its stdout is at EOF)
            err = child.stderr.read()[-3000:]
            child.stdout.close()
            child.stderr.close()
    while not lines.empty():
        x = lines.get_nowait()
        if x is not None:
            out.append(x)
    return status, out, err, reached


# ---------------------------------------------------------------- (a), (b): a second signal in `unproved`'s window

_UNPROVED_SCRIPT = r"""
import logging, os, signal, sys, threading, time
from agent_comms import node, node_sessions, session_host

# As in a terminal: the test runner may have left SIGINT ignored for its children (the node installs its own).
signal.signal(signal.SIGINT, signal.default_int_handler)
second = getattr(signal, sys.argv[2])
R, H = node_sessions.SessionRunner, session_host.SessionHost


def say(*words):
    print(*words, flush=True)


class Lines(logging.Handler):
    def emit(self, record):
        if "further signal" in record.getMessage():
            say("line:", record.getMessage())
logging.getLogger("agent_comms").addHandler(Lines())
logging.getLogger("agent_comms").setLevel(logging.INFO)

real_tick = node.Node.tick


def tick(self):
    if not getattr(tick, "said", False):
        tick.said = True
        say("ticking")
    return real_tick(self)
node.Node.tick = tick

real_kill = R.kill


def kill(self):
    # The shutdown's KILL pass for the sessions is made; the post-KILL watch (Node.await_kills ->
    # SessionRunner.unproved) is next. A helper thread takes the runner lock first, so main blocks in unproved's timed
    # acquire; the helper lets it go and at once sends this process its SECOND signal (the reviewer's probe).
    real_kill(self)
    held = threading.Event()

    def other():
        self.lock.acquire()
        held.set()
        time.sleep(0.3)             # main is blocked in unproved's timed acquire by now
        self.lock.release()
        os.kill(os.getpid(), second)        # this process's own second signal
        say("second signal sent")
    threading.Thread(target=other, daemon=True).start()
    held.wait(10)
R.kill = kill

real_save = H._save_state


def save(self):
    real_save(self)
    say("sessions saved")
H._save_state = save

real_stop_all = H.stop_all


def stop_all(self, *a, **k):
    out = real_stop_all(self, *a, **k)
    say("sessions bookkeeping done")
    return out
H.stop_all = stop_all

real_flush = R._flush


def flush(self, final=False):
    out = real_flush(self, final=final)
    if final:
        say("final reports made")
    return out
R._flush = flush

real_shutdown = R.shutdown


def shutdown(self, *a, **k):
    real_shutdown(self, *a, **k)
    say("sessions shutdown returned; runner lock", "held" if self.lock.locked() else "free")
R.shutdown = shutdown

node.run(sys.argv[1], "a-test-token", poll_seconds=0.1)
say("run returned")
"""


@pytest.mark.parametrize("first,second", [("SIGTERM", "SIGTERM"), ("SIGTERM", "SIGINT"), ("SIGINT", "SIGTERM")])
def test_a_second_signal_in_unproveds_acquire_window_leaves_the_shutdown_whole(tmp_path, first, second):
    """(a) and (b), the reviewer's sequence through the node's REAL shutdown path: the test's own child runs
    `node.run` with hosted sessions on (a real `SessionRunner`) against a stub hub. Once its loop ticks, the test sends
    it the FIRST signal (SIGTERM, or SIGINT as Ctrl-C does), which enters `shut_down`. Right after the sessions' KILL
    pass a helper thread holds the runner lock, so main blocks in `SessionRunner.unproved`'s timed acquire (called by
    `Node.await_kills`); the helper lets the lock go and sends the child its SECOND signal (SIGTERM or SIGINT), which
    lands between that acquire's success and its `try`. The shutdown completes: the sessions' bookkeeping, final save
    and final reports run, the runner lock is free afterwards, the further signal is said once (numbers only), and the
    exit status is main's (0: `run` returns). On 9388477 the second signal raised KeyboardInterrupt with the lock held
    and `sessions.shutdown()` blocked on it for good: the child never ended."""
    status, out, err = _node_child(tmp_path, _UNPROVED_SCRIPT, second, ready="ticking",
                                   first=getattr(signal, first), wait=90, sessions=True)
    assert status == 0, (status, out, err)
    assert "second signal sent" in out, (out, err)
    for step in ("sessions bookkeeping done", "sessions saved", "final reports made"):
        assert step in out, (step, out, err)
    assert "sessions shutdown returned; runner lock free" in out, (out, err)
    assert [x for x in out if x.startswith("line:")] == [
        "line: 1 further signal(s) arrived during shutdown and were not acted on"], (out, err)
    assert out[-1] == "run returned", (out, err)
    assert "Traceback" not in err, err


# ---------------------------------------------------------------- (c): a second signal before the final wait

_FINAL_SCRIPT = r"""
import logging, os, signal, sys, threading
from agent_comms import node

signal.signal(signal.SIGINT, signal.default_int_handler)        # (as in a terminal; the node installs its own)
second = getattr(signal, sys.argv[2])


class Stuck(logging.Handler):
    def emit(self, record):
        if "hub unreachable" in record.getMessage():
            print("entered", flush=True)
            threading.Event().wait()        # blocks for good, holding the handler's own lock

logging.getLogger("agent_comms").addHandler(Stuck())
logging.getLogger("agent_comms").setLevel(logging.INFO)
real = node._exit_if_stalled


def exit_if_stalled(status):
    # `run` calls this once `_run` has returned (or raised), with the status it selected: the signal is sent here,
    # so it is handled before the real final wait's `try` (here, or at the real function's entry).
    print("selected", status, flush=True)
    os.kill(os.getpid(), second)            # this process's own second signal
    return real(status)
node._exit_if_stalled = exit_if_stalled
node.run(sys.argv[1], "a-test-token", poll_seconds=0.1)
print("run returned", flush=True)
"""


@pytest.mark.parametrize("second", ["SIGTERM", "SIGINT"])
def test_a_second_signal_before_the_final_wait_takes_the_bounded_exit(tmp_path, second):
    """(c): the test's own child runs `node.run` against the stub hub, with a log handler that blocks forever
    (holding its lock) on the loop's `hub unreachable` line, so the writer is stuck. Once the handler is entered the
    test sends the child a real SIGTERM (the first signal): the node shuts down, `_run` returns, and `run` selects
    status 0. The child then gets a SECOND signal after `_run` returned and before the final wait's `try` (sent by a
    stand-in for `_exit_if_stalled` that then calls the real one). The exit is bounded and has the selected status
    (0): the second signal is counted, not raised, and the final wait finds the writer stuck and ends the process.
    On 9388477 the second signal raised out of `run` into the interpreter's exit, whose `logging.shutdown()` blocked
    on the stuck handler's lock: the child never ended."""
    status, out, err = _node_child(tmp_path, _FINAL_SCRIPT, second, ready="entered", first=signal.SIGTERM, wait=60,
                                   sessions=False)
    assert status == 0, (status, out, err)
    assert out[-1] == "selected 0", (out, err)
    assert "run returned" not in out, "the stalled exit ends the process at the final wait"
    assert "Traceback" not in err, err


# ---------------------------------------------------------------- review round 1, R1: the inherited SIGINT

_SIGINT_SCRIPT = r"""
import logging, os, signal, sys, threading, time
from agent_comms import node, node_sessions

mode, second = sys.argv[2], sys.argv[3]
# The SIGINT disposition this process hands the node, set before `node.run` (as a launcher would leave it):
if mode == "ignored":           # a background shell job, a nohup-style launcher, some supervisors
    signal.signal(signal.SIGINT, signal.SIG_IGN)
elif mode == "default":         # Python's own interrupt handler, as in a terminal (what main has)
    signal.signal(signal.SIGINT, signal.default_int_handler)
elif mode == "own":             # another handler, installed by whatever embeds the node
    signal.signal(signal.SIGINT, lambda signum, frame: print("own handler", flush=True))
elif mode == "dfl":             # the default action: the process ends
    signal.signal(signal.SIGINT, signal.SIG_DFL)
R = node_sessions.SessionRunner


def say(*words):
    print(*words, flush=True)


class Lines(logging.Handler):
    def emit(self, record):
        if "further signal" in record.getMessage():
            say("line:", record.getMessage())
logging.getLogger("agent_comms").addHandler(Lines())
logging.getLogger("agent_comms").setLevel(logging.INFO)

real_tick = node.Node.tick


def tick(self):
    say("tick")
    return real_tick(self)
node.Node.tick = tick

if second != "none":
    real_kill = R.kill

    def kill(self):
        # (as in _UNPROVED_SCRIPT) a SECOND signal lands in `unproved`'s timed acquire window during the shutdown
        real_kill(self)
        held = threading.Event()

        def other():
            self.lock.acquire()
            held.set()
            time.sleep(0.3)
            self.lock.release()
            os.kill(os.getpid(), getattr(signal, second))
            say("second signal sent")
        threading.Thread(target=other, daemon=True).start()
        held.wait(10)
    R.kill = kill

real_shutdown = R.shutdown


def shutdown(self, *a, **k):
    real_shutdown(self, *a, **k)
    say("sessions shutdown returned; runner lock", "held" if self.lock.locked() else "free")
R.shutdown = shutdown

node.run(sys.argv[1], "a-test-token", poll_seconds=0.1)
say("run returned")
"""

# The exit status main (9388477) gives a node whose FIRST signal is a SIGINT with Python's default interrupt handler:
# KeyboardInterrupt in the loop, `shut_down`, `run` returns, the process exits 0 (measured: DESIGN_NOTES "Shutdown: a
# repeated signal is recorded, not raised", review round 1).
MAIN_SIGINT_STATUS = 0


def test_an_inherited_ignored_sigint_stays_ignored(tmp_path):
    """R1 (a): the test's own child hands the node SIGINT IGNORED (as a background job or a nohup-style launcher
    does). A real SIGINT changes nothing: the loop goes on ticking. Then a real SIGTERM shuts it down cleanly, and a
    SECOND SIGTERM, landing in `unproved`'s acquire window during that shutdown, is only counted: exit 0, the runner
    lock free, the further signal said once. On 564aaae the node replaced the ignored disposition, so the SIGINT shut
    it down (no more ticks)."""
    status, out, err, reached = _drive(tmp_path, _SIGINT_SCRIPT, "ignored", "SIGTERM", sessions=True, wait=90, steps=[
        (_ticks_since(1), signal.SIGINT), (_ticks_since(5), signal.SIGTERM)])
    assert reached == 2, ("the node did not keep running after its SIGINT", out, err)
    assert status == 0, (status, out, err)
    assert "second signal sent" in out and "sessions shutdown returned; runner lock free" in out, (out, err)
    assert [x for x in out if x.startswith("line:")] == [
        "line: 1 further signal(s) arrived during shutdown and were not acted on"], (out, err)
    assert out[-1] == "run returned", (out, err)


def test_the_default_sigint_shuts_down_as_on_main(tmp_path):
    """R1 (b): with Python's default interrupt handler (what main has, as in a terminal) the FIRST real SIGINT shuts
    the node down as on main: the sessions are shut down, `run` returns, and the exit status is main's for it
    (MAIN_SIGINT_STATUS, measured on 9388477); no further signal is said. (Unchanged from 564aaae: this pins it.)"""
    status, out, err, reached = _drive(tmp_path, _SIGINT_SCRIPT, "default", "none", sessions=True, wait=90,
                                       steps=[(_ticks_since(1), signal.SIGINT)])
    assert reached == 1 and status == MAIN_SIGINT_STATUS, (status, out, err)
    assert "sessions shutdown returned; runner lock free" in out and out[-1] == "run returned", (out, err)
    assert not [x for x in out if x.startswith("line:")], out


def test_another_inherited_sigint_handler_is_left_as_it_was(tmp_path):
    """R1, the other dispositions: a SIGINT handler installed by whatever embeds the node is left in place, as on
    main. A real SIGINT runs THAT handler ("own handler") and the node goes on ticking; a real SIGTERM then shuts it
    down, exit 0. On 564aaae the node replaced it, so the SIGINT shut the node down instead."""
    status, out, err, reached = _drive(tmp_path, _SIGINT_SCRIPT, "own", "none", sessions=True, wait=90, steps=[
        (_ticks_since(1), signal.SIGINT), (_ticks_since(5), signal.SIGTERM)])
    assert reached == 2, ("the node did not keep running after its SIGINT", out, err)
    assert "own handler" in out, (out, err)
    assert status == 0 and out[-1] == "run returned", (status, out, err)


def test_an_inherited_default_action_sigint_still_ends_the_process(tmp_path):
    """R1, the other dispositions: SIGINT left at the default ACTION (`SIG_DFL`) is left so, as on main: a real
    SIGINT ends the process by that signal (status -SIGINT), with no shutdown run. On 564aaae the node replaced it,
    so the SIGINT ran the shutdown and the process exited 0."""
    status, out, err, reached = _drive(tmp_path, _SIGINT_SCRIPT, "dfl", "none", sessions=True, wait=90,
                                       steps=[(_ticks_since(1), signal.SIGINT)])
    assert reached == 1 and status == -signal.SIGINT, (status, out, err)
    assert "run returned" not in out, out


# ---------------------------------------------------------------- (d): the latch, at unit level

FURTHER = "{} further signal(s) arrived during shutdown and were not acted on"


def test_the_first_signal_latches_before_it_raises_and_later_ones_only_count():
    """(d) The handler's first call (SIGTERM or SIGINT) latches, then raises KeyboardInterrupt: the latch is set when
    the interrupt is seen (set after the raise, it would never be). Every later call, of either signal, returns
    without raising and only counts. Nothing clears it."""
    from agent_comms.node import ShutdownLatch
    for first in (signal.SIGTERM, signal.SIGINT):
        latch = ShutdownLatch()
        assert (latch.latched, latch.further) == (False, 0)
        with pytest.raises(KeyboardInterrupt):
            latch.on_signal(first, None)
        assert (latch.latched, latch.further) == (True, 0)
        for n, later in enumerate((signal.SIGTERM, signal.SIGINT, signal.SIGTERM, signal.SIGINT), 1):
            assert latch.on_signal(later, None) is None
            assert (latch.latched, latch.further) == (True, n)


def test_a_latch_set_by_another_route_makes_even_the_first_signal_count_only():
    """(d) `latch()` (the routes into cleanup that do not come through the handler) is the same latch: a signal after
    it is counted, not raised. Latching again changes nothing."""
    from agent_comms.node import ShutdownLatch
    latch = ShutdownLatch()
    latch.latch()
    latch.latch()
    assert latch.on_signal(signal.SIGTERM, None) is None
    assert latch.on_signal(signal.SIGINT, None) is None
    assert (latch.latched, latch.further) == (True, 2)


def test_further_signals_are_said_once_with_the_count_outside_the_handler():
    """(d) The handler writes nothing (no record is made by any call of it); `say()` writes ONE line with the count
    of further signals (numbers only), once however often it is called; with no further signal it writes nothing."""
    from agent_comms import hostlog
    from agent_comms.node import ShutdownLatch
    from test_hostlog import _Records
    latch = ShutdownLatch()
    with _Records() as rec:
        with pytest.raises(KeyboardInterrupt):
            latch.on_signal(signal.SIGTERM, None)
        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGTERM):
            latch.on_signal(sig, None)
        assert hostlog.flush(5)
        assert rec.messages() == [], "the handler logs nothing"
        latch.say()
        latch.say()
    assert rec.messages() == [FURTHER.format(3)]
    quiet = ShutdownLatch()
    with _Records() as rec:
        with pytest.raises(KeyboardInterrupt):
            quiet.on_signal(signal.SIGINT, None)
        quiet.say()
    assert rec.messages() == []


def _latches(monkeypatch):
    """Every latch `node.run` makes, recorded (a subclass, so the handler and the routes are the real ones)."""
    from agent_comms import node as node_mod
    made = []

    class Recorded(node_mod.ShutdownLatch):
        __slots__ = ()

        def __init__(self):
            super().__init__()
            made.append(self)
    monkeypatch.setattr(node_mod, "ShutdownLatch", Recorded)
    return made


def test_an_interrupt_raised_directly_latches_before_shut_down_and_further_signals_are_said(senv, monkeypatch):
    """(d) The route of a KeyboardInterrupt raised directly (not through the handler) at the loop's hub call: the
    latch is set before `shut_down` is entered; two signals during the shutdown are counted, not raised (the
    shutdown completes and `run` returns); the line saying them is written once, with the count; the latch is still
    set when `run` has returned."""
    from agent_comms import node as node_mod
    from test_hostlog import _assignments, _Records, _run_node
    made, seen = _latches(monkeypatch), []
    real = node_mod.shut_down

    def shut_down(node):
        seen.append(made[0].latched)
        assert made[0].on_signal(signal.SIGTERM, None) is None      # (as the handler is called for a later signal)
        assert made[0].on_signal(signal.SIGINT, None) is None
        real(node)
    monkeypatch.setattr(node_mod, "shut_down", shut_down)

    def act(method, path):
        if method == "GET" and _assignments(path):
            raise KeyboardInterrupt         # (raised directly, at the hub call the loop waits on)
        return None
    with _Records() as rec:
        done, err, exits, _ = _run_node(senv, monkeypatch, act)
        assert done.wait(15), "run ended"
    assert not err, err
    assert exits in ([], [0]), exits
    assert len(made) == 1 and seen == [True]
    assert made[0].latched and made[0].further == 2
    assert [m for m in rec.messages() if "further signal" in m] == [FURTHER.format(2)]


def test_an_ordinary_exception_out_of_the_loop_latches_before_the_sessions_stop(senv, monkeypatch):
    """(d) The route of an ordinary exception out of the node's loop: the latch is set before `sessions.shutdown()`
    is entered, the exception escapes `run` as before, and the latch stays set."""
    from test_hostlog import _assignments, _run_node
    made, seen = _latches(monkeypatch), []
    real = senv.node.sessions.shutdown

    def shutdown(*a, **k):
        seen.append(made[0].latched)
        return real(*a, **k)
    monkeypatch.setattr(senv.node.sessions, "shutdown", shutdown)

    def act(method, path):
        if method == "GET" and _assignments(path):
            raise RuntimeError("an ordinary failure")
        return None
    done, err, exits, _ = _run_node(senv, monkeypatch, act)
    assert done.wait(15), "run ended"
    assert [type(e) for e in err] == [RuntimeError]
    assert len(made) == 1 and seen == [True] and made[0].latched


def test_an_exception_before_the_loop_latches_before_the_final_wait(senv, monkeypatch):
    """(d) The route of an exception that ends `_run` before its loop (here the hello): `run` sets the latch before
    its final wait (`_exit_if_stalled`), and the exception escapes as before."""
    from agent_comms import node as node_mod
    from test_hostlog import _run_node
    made, seen = _latches(monkeypatch), []
    real = node_mod._exit_if_stalled

    def exit_if_stalled(status):
        seen.append((made[0].latched, status))
        return real(status)
    monkeypatch.setattr(node_mod, "_exit_if_stalled", exit_if_stalled)

    def act(method, path):
        if method == "POST" and path.startswith("/api/nodes/hello"):
            raise RuntimeError("the hello failed")
        return None
    done, err, exits, _ = _run_node(senv, monkeypatch, act)
    assert done.wait(15), "run ended"
    assert [type(e) for e in err] == [RuntimeError]
    assert seen == [(True, 1)] and made[0].latched


# ---------------------------------------------------------------- (e): the static pin of the handler

HANDLER_STATEMENTS = (ast.If, ast.Assign, ast.AugAssign, ast.Return, ast.Raise)


def _handler_problems(tree):
    """(e) THE pin of the node's signal handler, over node.py's source: what `ShutdownLatch.on_signal` may do.
    (what) for each departure from:
    - its body is only `if`, assignment, `+=`, `return` and `raise` statements (a docstring aside): no call of any
      kind (so no log line, no lock, no I/O, no print), no `with`, `try`, loop, import, `global`, lambda,
      comprehension, f-string, subscript, `await` or `yield`;
    - every name it reads is `self` or `KeyboardInterrupt`, and every attribute is `self.latched` or `self.further`;
    - its only assignment is `self.latched = True`, its only `+=` is `self.further += 1`, its `if` tests
      `self.latched` itself, its `raise` is the bare class `KeyboardInterrupt` (no call, no `from`), and its `return`
      returns nothing;
    - `ShutdownLatch` has `__slots__`, no base class, no decorator and no `__setattr__`, `__getattr__` or
      `__getattribute__`: so reading and setting those two attributes runs no Python code;
    - nothing in node.py sets `.latched` to anything but True, outside `ShutdownLatch.__init__` (the latch is never
      cleared);
    - node.py installs the handler exactly twice, both in `_run`: `signal.signal(signal.SIGTERM, latch.on_signal)`
      unconditionally, and `signal.signal(signal.SIGINT, latch.on_signal)` as the ONLY statement of an
      `if signal.getsignal(signal.SIGINT) is signal.default_int_handler:` with no `else` (review round 1, R1: any
      other inherited SIGINT disposition, ignored included, is left as it was). No other `signal.signal` call: no
      other signal, no unconditional or otherwise-guarded SIGINT replacement."""
    problems = []
    cls = next((n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "ShutdownLatch"), None)
    if cls is None:
        return ["no ShutdownLatch class"]
    if cls.bases or cls.keywords or cls.decorator_list:
        problems.append("ShutdownLatch has a base class, a metaclass or a decorator")
    names = {t.id for n in cls.body if isinstance(n, ast.Assign) for t in n.targets if isinstance(t, ast.Name)}
    if "__slots__" not in names:
        problems.append("ShutdownLatch has no __slots__")
    methods = {n.name: n for n in cls.body if isinstance(n, ast.FunctionDef)}
    for hook in ("__setattr__", "__getattr__", "__getattribute__"):
        if hook in methods:
            problems.append(f"ShutdownLatch defines {hook}")
    fn = methods.get("on_signal")
    if fn is None:
        return problems + ["no ShutdownLatch.on_signal"]
    body = fn.body
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
        body = body[1:]                         # (the docstring)

    def statements(stmts):
        for s in stmts:
            yield s
            if isinstance(s, ast.If):
                yield from statements(s.body)
                yield from statements(s.orelse)
    for s in statements(body):
        if not isinstance(s, HANDLER_STATEMENTS):
            problems.append(f"a {type(s).__name__} statement: {ast.unparse(s).splitlines()[0]}")
    for n in ast.walk(ast.Module(body=body, type_ignores=[])):
        what = ast.unparse(n).splitlines()[0] if isinstance(n, (ast.expr, ast.stmt)) else type(n).__name__
        if isinstance(n, (ast.Call, ast.Await, ast.Yield, ast.YieldFrom, ast.Lambda, ast.JoinedStr, ast.Subscript,
                          ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp, ast.NamedExpr)):
            problems.append(f"{type(n).__name__}: {what}")
        if isinstance(n, ast.Name) and n.id not in ("self", "KeyboardInterrupt"):
            problems.append(f"the name {n.id}")
        if isinstance(n, ast.Attribute) and not (ast.unparse(n.value) == "self" and n.attr in ("latched", "further")):
            problems.append(f"the attribute {what}")
        if isinstance(n, ast.Assign) and not (len(n.targets) == 1 and ast.unparse(n.targets[0]) == "self.latched"
                                              and isinstance(n.value, ast.Constant) and n.value.value is True):
            problems.append(f"the assignment {what}")
        if isinstance(n, ast.AugAssign) and not (ast.unparse(n.target) == "self.further"
                                                 and isinstance(n.op, ast.Add) and isinstance(n.value, ast.Constant)
                                                 and n.value.value == 1):
            problems.append(f"the update {what}")
        if isinstance(n, ast.If) and ast.unparse(n.test) != "self.latched":
            problems.append(f"the test {ast.unparse(n.test)}")
        if isinstance(n, ast.Raise) and not (isinstance(n.exc, ast.Name) and n.exc.id == "KeyboardInterrupt"
                                             and n.cause is None):
            problems.append(f"the raise {what}")
        if isinstance(n, ast.Return) and n.value is not None:
            problems.append(f"the return {what}")
    init = methods.get("__init__")
    for n in ast.walk(tree):
        if isinstance(n, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
            targets = n.targets if isinstance(n, ast.Assign) else [n.target]
            clears = any(isinstance(t, ast.Attribute) and t.attr == "latched" for t in targets) and not (
                isinstance(n, ast.Assign) and isinstance(n.value, ast.Constant) and n.value.value is True)
            if clears and not (init is not None and any(n is x for x in ast.walk(init))):
                problems.append(f"the latch is cleared: {ast.unparse(n)}")
    installs = []
    for fn_ in ast.walk(tree):
        if isinstance(fn_, ast.FunctionDef):
            up = {c: p for p in ast.walk(fn_) for c in ast.iter_child_nodes(p)}
            for n in ast.walk(fn_):
                if isinstance(n, ast.Call) and ast.unparse(n.func) == "signal.signal":
                    stmt = up.get(n)            # (the call's own statement, then what holds it)
                    holder = up.get(stmt)
                    guard = (ast.unparse(holder.test) if isinstance(holder, ast.If) and stmt in holder.body
                             and len(holder.body) == 1 and not holder.orelse else None)
                    installs.append((fn_.name, ast.unparse(n), guard if holder is not fn_ else "-"))
    if sorted(installs) != sorted([
            ("_run", "signal.signal(signal.SIGTERM, latch.on_signal)", "-"),
            ("_run", "signal.signal(signal.SIGINT, latch.on_signal)",
             "signal.getsignal(signal.SIGINT) is signal.default_int_handler")]):
        problems.append(f"the handlers installed: {installs}")
    return problems


def _node_tree():
    return ast.parse((ROOT / "agent_comms" / "node.py").read_text())


def test_the_handler_does_nothing_but_latch_count_and_raise_once():
    """(e) Statically, over node.py: the handler's body makes no call at all (no log line, no lock, no I/O), touches
    only its own two slots, is installed for SIGTERM, and for SIGINT only over Python's default interrupt handler,
    and for nothing else, and nothing clears the latch."""
    problems = _handler_problems(_node_tree())
    assert not problems, problems


_GOOD = '''
class ShutdownLatch:
    __slots__ = ("latched", "further")

    def __init__(self):
        self.latched = False
        self.further = 0

    def on_signal(self, signum, frame):
        """the docstring"""
        if self.latched:
            self.further += 1
            return
        self.latched = True
        raise KeyboardInterrupt

def _run(latch):
    signal.signal(signal.SIGTERM, latch.on_signal)
    if signal.getsignal(signal.SIGINT) is signal.default_int_handler:
        signal.signal(signal.SIGINT, latch.on_signal)
'''

_SIGINT_GUARD = ("    if signal.getsignal(signal.SIGINT) is signal.default_int_handler:\n"
                 "        signal.signal(signal.SIGINT, latch.on_signal)\n")


@pytest.mark.parametrize("old,new", [
    ("        self.latched = True\n        raise", "        log.warning('shutting down')\n        self.latched = True\n        raise"),
    ("        self.latched = True\n        raise", "        with self.lock:\n            self.latched = True\n        raise"),
    ("            self.further += 1", "            print('again')\n            self.further += 1"),
    ("        raise KeyboardInterrupt", "        raise KeyboardInterrupt()"),
    ("            self.further += 1", "            self.counts[signum] = 1"),
    ("            self.further += 1", "            self.further += signum"),
    ("        if self.latched:", "        if self.latched or os.getpid():"),
    ("            return\n", "            return None if self.latched else self.further\n"),
    ('    __slots__ = ("latched", "further")\n', ""),
    ("class ShutdownLatch:", "class ShutdownLatch(Base):"),
    ("    def __init__(self):", "    def __setattr__(self, k, v):\n        pass\n\n    def __init__(self):"),
    ("def _run(latch):", "def clear(latch):\n    latch.latched = False\n\ndef _run(latch):"),
    (_SIGINT_GUARD, ""),
    (_SIGINT_GUARD, _SIGINT_GUARD + "    signal.signal(signal.SIGHUP, latch.on_signal)\n"),
    (_SIGINT_GUARD, "    signal.signal(signal.SIGINT, latch.on_signal)\n"),
    (_SIGINT_GUARD, _SIGINT_GUARD.replace("is signal.default_int_handler", "is not signal.SIG_IGN")),
    (_SIGINT_GUARD, _SIGINT_GUARD.replace("is signal.default_int_handler", "is signal.default_int_handler or True")),
    (_SIGINT_GUARD, _SIGINT_GUARD + "    else:\n        signal.signal(signal.SIGINT, signal.SIG_IGN)\n"),
    ("    signal.signal(signal.SIGTERM, latch.on_signal)\n",
     "    if signal.getsignal(signal.SIGTERM) is signal.SIG_DFL:\n"
     "        signal.signal(signal.SIGTERM, latch.on_signal)\n"),
], ids=["logs", "locks", "prints", "raises-a-call", "subscript", "counts-other", "tests-a-call", "returns-a-value",
        "no-slots", "a-base", "setattr", "cleared", "no-sigint", "another-signal", "unconditional-sigint",
        "sigint-unless-ignored", "sigint-guard-widened", "sigint-replaced-otherwise", "sigterm-conditional"])
def test_the_handler_pin_refuses_each_unsafe_form(old, new):
    """(e) The SAME pin accepts the reviewed form and refuses each departure, as source text."""
    assert _handler_problems(ast.parse(_GOOD)) == []
    assert old in _GOOD, old
    assert _handler_problems(ast.parse(_GOOD.replace(old, new, 1))), new
