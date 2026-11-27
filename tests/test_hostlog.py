"""Host-side logging (DESIGN_NOTES "Host-side logging"): the package-owned adapter (`agent_comms.hostlog`).

THE invariant: no log handler runs, and nothing is formatted, while the calling thread holds a host lock (the runner
lock, the cutoff lock, the registry lock), on any thread, for any host-side line; nothing process-wide is patched.
Every line is a deferred record made at the call and written by one drain on the caller's thread at the outermost
boundary, after every host lock is let go. A watchdog thread never drains."""
import logging

import pytest

from hostlog_guard import Guard
from test_claude_steering import (_on_watchdog, _ordinary_turn_in_flight, claude_steered, cresult, env,  # noqa: F401
                                  tick)
from test_hosted_sessions import P2, start
from test_session_host import SID, henv, make_host, open_session  # noqa: F401


# ---------------------------------------------------------------- the guard, on the node's own routes


def n_start(env):
    """A session's first message: its process is spawned (under the registry lock) by the node's pass (under the
    runner lock)."""
    def act():
        start(env, "a turn")
        tick(env, times=2)
    return act


def n_timeout(env):
    """A Claude turn with a live steer attempt times out under the node: a claimed main route."""
    a, ca, n, s_uuid = claude_steered(env)
    runner = env.node.sessions

    def act():
        env.clock.advance(runner.host.timeout_minutes * 60 + 1)
        runner._renew_contact(env.clock())
        runner.pass_once()
        runner.pass_once()
    return act


def n_watchdog_hold_cut(env):
    """A held end whose contact ran out, cut by the watchdog's boundary (the reserved stop), then the loop."""
    a, ca, n, s_uuid = claude_steered(env)
    runner = env.node.sessions
    ca.emit(cresult(a, n, text="N's reply"))
    tick(env)
    sa = runner.host.sessions[a]
    assert sa.held is not None
    ca.exits_on_terminate = False
    sa.steer.cover_until = env.clock() - 1

    def act():
        t, done, ident, err = _on_watchdog(runner)
        assert done.wait(5) and not err, err
        runner.pass_once()
        env.clock.advance(runner.host.kill_grace + 1)
        t, done, ident, err = _on_watchdog(runner)
        assert done.wait(5) and not err, err
        runner.pass_once()
    return act


def n_self_stop(env):
    """The watchdog's self-stop (no hub contact), late (its line says so: written by the loop), then the loop's pass."""
    sid, child, m = _ordinary_turn_in_flight(env)
    runner = env.node.sessions

    def act():
        env.clock.advance(runner.self_stop_seconds + runner.watch_seconds + 60)
        t, done, ident, err = _on_watchdog(runner)
        assert done.wait(5) and not err, err
        runner._renew_contact(env.clock())
        runner.pass_once()
    return act


def n_close(env):
    """The human closes a session: the node's reconciliation closes it under the runner lock."""
    sid = start(env, "a turn")["session_id"]
    tick(env, times=2)

    def act():
        assert env.human.post(f"/api/hosted-sessions/{sid}/close").status_code == 200
        tick(env, times=3)
    return act


def n_shutdown(env):
    """Shutdown with a held Claude end and an ordinary sibling: `signal`, `kill`, `shutdown`."""
    a, ca, n, s_uuid = claude_steered(env)
    runner = env.node.sessions
    ca.emit(cresult(a, n, text="N's reply"))
    tick(env)
    b, cb, mb = _ordinary_turn_in_flight(env, project=P2)
    ca.exits_on_terminate = cb.exits_on_terminate = False

    def act():
        runner.signal()
        runner.kill()
        runner.shutdown()
    return act


NODE_ROUTES = [n_start, n_timeout, n_watchdog_hold_cut, n_self_stop, n_close, n_shutdown]


@pytest.mark.parametrize("mode", ["blocks", "raises"])
@pytest.mark.parametrize("route", NODE_ROUTES, ids=lambda f: f.__name__)
def test_no_handler_is_entered_under_a_host_lock_on_the_nodes_routes(env, route, mode):
    """The node's own routes (its pass, its watchdog, its shutdown): a handler that blocks or raises is never entered
    while the entering thread holds the runner lock, the cutoff lock or the registry lock; lines were written."""
    act = route(env)
    with Guard(runner=env.node.sessions, mode=mode) as g:
        act()
    if route is not n_close:
        assert g.seen, "the route wrote lines"
    assert not g.violations, g.violations


def test_a_spawn_callback_under_the_registry_lock_logs_no_handler_there(henv):
    """A line logged by a spawn callback, which runs under the registry lock (`Groups.start`), is written after the
    registry is let go."""
    from agent_comms import session_host
    real = henv.spawner

    def spawner(*a, **k):
        session_host.log.warning("a line from the spawn callback")
        return real(*a, **k)
    henv.host = make_host(henv, spawner=spawner)
    open_session(henv)
    henv.host.send(SID, "go")
    with Guard(henv.host) as g:
        henv.host.tick()
    assert [m for _, m, _ in g.seen if m == "a line from the spawn callback"]
    assert not g.violations, g.violations


def test_a_registry_callback_line_is_written_after_the_registry_is_let_go():
    """The registry's lock is a host lock: a dispatched run's spawn callback (node.py) that logs under it has its line
    written once `Groups.start` let go of it."""
    from agent_comms import node
    from agent_comms.node_groups import Groups
    from hostlog_guard import own
    g = Groups()
    lock = own(g, "lock")
    seen = []
    with _Records(act=lambda r: seen.append(lock.mine())) as rec:
        started, child = g.start("/nowhere", ("run", "r1"), lambda: node.log.info("spawning a run") or "child")
    assert started and child == "child"
    assert rec.messages() == ["spawning a run"] and seen == [False]


# ---------------------------------------------------------------- the mechanism


class _Records:
    """A handler on the package logger that keeps every record it is handed (and, optionally, does something with
    it: `act(record)`)."""

    def __init__(self, act=None, level=logging.DEBUG):
        self.records, self.act, self.level = [], act, level
        outer = self

        class H(logging.Handler):
            def emit(self, rec):
                outer.records.append(rec)
                if outer.act is not None:
                    outer.act(rec)
        self.handler = H(logging.DEBUG)
        self.logger = logging.getLogger("agent_comms")

    def messages(self):
        return [r.getMessage() for r in self.records]

    def __enter__(self):
        self.saved = self.logger.level
        self.logger.setLevel(self.level)
        self.logger.addHandler(self.handler)
        return self

    def __exit__(self, *a):
        self.logger.removeHandler(self.handler)
        self.logger.setLevel(self.saved)


def _on_watch_thread(fn):
    """`fn` on a thread registered as a watchdog's (it never drains); returns its ident once it is done."""
    t, done, ident, err = _on_watchdog(None, fn=fn)
    assert done.wait(10) and not err, err
    return ident[0]


@pytest.mark.parametrize("which", ["cutoff", "registry", "runner"])
def test_each_host_lock_is_a_boundary_of_its_own(env, which):
    """B4: each host lock, taken outside every entry point (an internal path a test drives directly), is a boundary:
    a line logged while it is held is written when it is let go, never while it is held."""
    from agent_comms import session_host
    from hostlog_guard import own
    runner = env.node.sessions
    obj, attr = {"cutoff": (runner.host, "_steer_stop_lock"), "registry": (runner.host.groups, "lock"),
                 "runner": (runner, "lock")}[which]
    lock = getattr(obj, attr)
    inner = own(obj, attr)
    seen = []
    with _Records(act=lambda r: seen.append(inner.mine())) as rec:
        with lock:
            session_host.log.warning("under the lock")
            assert rec.records == []
        assert rec.messages() == ["under the lock"] and seen == [False]


def test_the_host_side_modules_take_their_loggers_from_hostlog():
    """Static (B1): no host-side module logs through `logging` directly (no `logging.getLogger`, no module-level
    `logging.<level>` call, no `from logging import` of either); each logger is `hostlog.get(<its name>)`. The one
    exception is node.py's `log_to_stderr`, which configures the package logger's handler and logs nothing. (The
    dynamic guard above is what catches a helper that logs synchronously under a host lock.)"""
    import ast
    from pathlib import Path
    import agent_comms
    root = Path(agent_comms.__file__).parent
    names = {"session_host.py": "agent_comms.session_host", "node_sessions.py": "agent_comms.node_sessions",
             "node.py": "agent_comms.node", "providers/claude.py": "agent_comms.providers.claude",
             "providers/codex.py": "agent_comms.providers.codex", "node_groups.py": None,
             "providers/__init__.py": None}
    emitting = {"getLogger", "debug", "info", "warning", "warn", "error", "exception", "critical", "fatal", "log",
                "basicConfig"}
    for rel, name in names.items():
        tree = ast.parse((root / rel).read_text())
        allowed = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and rel == "node.py" and node.name == "log_to_stderr":
                allowed |= {id(n) for n in ast.walk(node)}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "logging":
                assert not {a.name for a in node.names} & emitting, (rel, node.lineno)
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Name) and node.func.value.id == "logging"
                    and node.func.attr in emitting and id(node) not in allowed):
                raise AssertionError(f"{rel}:{node.lineno}: logging.{node.func.attr}")
        if name is not None:
            assert f'log = hostlog.get("{name}")' in (root / rel).read_text(), rel


def test_logger_names_and_levels_are_loggings_own():
    """B1: the adapter keeps the logger names (so `caplog` and logger configuration work as before) and asks the real
    logger's level."""
    from agent_comms import node, node_sessions, session_host
    from agent_comms.providers import claude, codex
    for mod, name in [(session_host, "agent_comms.session_host"), (node_sessions, "agent_comms.node_sessions"),
                      (node, "agent_comms.node"), (claude, "agent_comms.providers.claude"),
                      (codex, "agent_comms.providers.codex")]:
        assert mod.log.name == name and mod.log.logger is logging.getLogger(name)
    real = logging.getLogger("agent_comms.session_host")
    level = real.level
    try:
        real.setLevel(logging.ERROR)
        assert not session_host.log.isEnabledFor(logging.WARNING)
    finally:
        real.setLevel(level)


def test_caplog_sees_a_lines_record_as_soon_as_the_public_call_returns(henv, caplog):
    """B4: a standalone host drains at the end of each public method: `caplog` has the line when `tick` returns."""
    caplog.set_level(logging.INFO, logger="agent_comms")
    open_session(henv)
    henv.host.send(SID, "go")
    henv.host.tick()
    assert [r for r in caplog.records if r.getMessage().startswith(f"started session {SID}")]


def test_a_line_the_logger_would_not_emit_is_not_kept():
    """B3: call-time level filtering: a line below the logger's level at the call is not queued (lowering the level
    before the drain does not bring it back); one at or above it is written even if the level is raised before the
    drain."""
    from agent_comms import hostlog, session_host
    real = logging.getLogger("agent_comms.session_host")
    level = real.level
    with _Records(level=logging.NOTSET) as rec:
        real.setLevel(logging.WARNING)

        def logs():
            session_host.log.info("an info line")
            session_host.log.warning("a warning line")
            real.setLevel(logging.ERROR)
        try:
            _on_watch_thread(logs)
            real.setLevel(logging.DEBUG)
            hostlog.drain(everything=True)
        finally:
            real.setLevel(level)
    assert rec.messages() == ["a warning line"]


def _line():
    import sys
    return sys._getframe(1).f_lineno


def test_a_deferred_record_is_the_calls_snapshot():
    """B3: the record is made at the call: its time, thread and caller are the call's (no source lookup), an
    exception is its type's name and its frames (no text), an object argument is its type's name (never formatted
    later), a `%`-mapping is copied with reduced values; nothing is written until a drain."""
    import threading
    import time as _time
    from agent_comms import hostlog, session_host

    class Loud:
        def __str__(self):
            raise AssertionError("formatted")

        __repr__ = __format__ = __str__

    class Marked(Exception):
        pass
    mutable = ["kept as its type"]
    at = {}

    def logs():
        at["before"] = _time.time()
        at["thread"] = threading.current_thread().name
        at["line"] = _line() + 1
        session_host.log.warning("an object %s and a list %s and %d and %s", Loud(), mutable, 7, None)
        session_host.log.info("a mapping %(a)s %(b)s", {"a": "text", "b": Loud()})
        try:
            raise Marked("PINEAPPLE-STEER-TEXT")
        except Marked:
            session_host.log.exception("it failed")
        at["after"] = _time.time()
    with _Records() as rec:
        _on_watch_thread(logs)
        assert rec.records == [], "nothing written on the watchdog's thread, nor before a drain"
        mutable.append("changed")
        _time.sleep(0.05)
        hostlog.drain(everything=True)
    first, mapping, failed = rec.records
    assert at["before"] <= first.created <= at["after"] < _time.time()
    assert first.threadName == at["thread"]
    assert first.pathname == __file__ and first.lineno == at["line"] and first.funcName == "logs"
    assert first.getMessage() == "an object Loud and a list list and 7 and None"
    assert mapping.getMessage() == "a mapping text Loud"
    assert failed.exc_info is None and failed.hostlog_exc[0] == "Marked"
    assert [f[2] for f in failed.hostlog_exc[1]] == ["logs"]
    text = logging.Formatter().format(failed)
    assert "Marked" in text and "in logs" in text and "PINEAPPLE" not in text, text


def test_enqueue_order_across_threads_is_the_write_order():
    """B7: two watchdog threads log in turn (each line after the other's): the drain writes them in that order, which
    is the queue's (`hostlog_seq`)."""
    import threading
    from agent_comms import hostlog, session_host
    turn = [threading.Event() for _ in range(8)]

    def side(k):
        for i in range(k, 8, 2):
            if i:
                assert turn[i - 1].wait(5)
            session_host.log.warning("line %d", i)
            turn[i].set()
    with _Records() as rec:
        a = threading.Thread(target=lambda: _on_watch_thread(lambda: side(0)))
        b = threading.Thread(target=lambda: _on_watch_thread(lambda: side(1)))
        a.start()
        b.start()
        a.join(10)
        b.join(10)
        hostlog.drain(everything=True)
    assert rec.messages() == [f"line {i}" for i in range(8)]
    seqs = [r.hostlog_seq for r in rec.records]
    assert seqs == sorted(seqs)


def test_an_inner_public_call_under_the_runner_lock_leaves_its_lines_to_the_outer_boundary(henv):
    """B4: a host call made under the runner lock (as the node makes them) returns with its lines still queued; the
    lock's release (the outer boundary) writes them, on a normal and on an exceptional exit."""
    from agent_comms import session_host
    from hostlog_guard import runner_lock
    lock, inner = runner_lock()
    open_session(henv)
    henv.host.send(SID, "go")
    with _Records() as rec:
        with lock:
            henv.host.tick()
            assert not [m for m in rec.messages() if m.startswith("started session")], "not under the lock"
        assert [m for m in rec.messages() if m.startswith("started session")], "at its release"
        with pytest.raises(RuntimeError):
            with lock:
                session_host.log.warning("a line before a raise")
                assert rec.messages()[-1] != "a line before a raise"
                raise RuntimeError("the pass failed")
        assert rec.messages()[-1] == "a line before a raise", "an exceptional exit drains too"


def test_a_handler_that_raises_is_caught_and_the_drain_goes_on():
    """B5: a handler that raises on one record: the call returns normally, the next handler still gets that record,
    and the later records are written."""
    from agent_comms import session_host

    def act(r):
        if r.getMessage() == "first":
            raise RuntimeError("a log handler failed")
    with _Records(act=act) as bad, _Records() as good:
        session_host.log.warning("first")
        session_host.log.warning("second")
    assert bad.messages() == ["first", "second"] and good.messages() == ["first", "second"]


def test_a_handler_that_logs_and_calls_back_into_the_host_does_not_deadlock(henv):
    """B5: a handler that logs and calls public host methods (whose lines are queued too) from inside the drain: no
    deadlock; the nested drains leave their records to the ACTIVE drainer, which writes them before the outer call
    returns."""
    import threading
    from agent_comms import session_host
    open_session(henv)
    henv.host.send(SID, "go")
    calls = []

    def act(r):
        if r.getMessage().startswith("started session") and not calls:
            calls.append(1)
            session_host.log.warning("from the handler")
            henv.host.send(SID, "again")        # (public methods: their own boundaries, nested in the drain)
            henv.host.tick()
    with _Records(act=act) as rec:
        done = threading.Event()
        t = threading.Thread(target=lambda: (henv.host.tick(), done.set()), daemon=True)
        t.start()
        assert done.wait(5), "no deadlock"
        assert calls and "from the handler" in rec.messages(), "written by the active drainer before it returned"


def test_a_blocked_drainer_does_not_delay_the_watchdogs_signals(env):
    """B4: the loop's drain is stalled by a handler (after it let go of the runner lock); meanwhile the watchdog's
    self-stop SIGTERMs the turn's group and, after the grace, SIGKILLs it: the watchdog never waits on a drainer."""
    import threading
    from agent_comms import node_sessions
    sid, child, m = _ordinary_turn_in_flight(env)
    runner = env.node.sessions
    child.exits_on_terminate = False
    stall, entered = threading.Event(), threading.Event()
    loop = []

    def act(r):
        if r.getMessage() == "a loop line" and threading.get_ident() in loop:
            entered.set()
            stall.wait(10)

    def the_loop():
        loop.append(threading.get_ident())
        with runner.lock:
            node_sessions.log.warning("a loop line")
    with _Records(act=act):
        t = threading.Thread(target=the_loop, daemon=True)
        t.start()
        try:
            assert entered.wait(5), "the loop's drain is stalled"
            env.clock.advance(runner.self_stop_seconds + 1)
            _on_watch_thread(runner.enforce_deadlines)
            assert child.terminated, "the self-stop's SIGTERM went"
            env.clock.advance(runner.host.kill_grace + 1)
            _on_watch_thread(runner.enforce_deadlines)
            assert child.killed, "and its SIGKILL"
        finally:
            stall.set()
            t.join(5)


def test_a_watchdog_thread_never_drains():
    """B4: lines logged on a watchdog thread, inside and outside a boundary, with an explicit drain there too, are
    written by the next drain elsewhere, never on that thread."""
    import threading
    from agent_comms import hostlog, session_host
    written = []

    def logs():
        session_host.log.warning("outside")
        with hostlog.BOUNDARY:
            session_host.log.warning("inside")
        hostlog.drain(everything=True)
    with _Records(act=lambda r: written.append(threading.get_ident())) as rec:
        ident = _on_watch_thread(logs)
        assert rec.records == []
        hostlog.drain(everything=True)
    assert rec.messages() == ["outside", "inside"] and all(r.thread == ident for r in rec.records)
    assert written and ident not in written


def test_no_drain_inside_a_boundary_even_when_asked():
    """B4: the drain itself refuses inside a boundary (depth above 0), whoever calls it: the boundary's exit writes."""
    from agent_comms import hostlog, session_host
    with _Records() as rec:
        with hostlog.BOUNDARY:
            session_host.log.warning("inside")
            assert hostlog.drain(everything=True) is False and rec.records == []
        assert rec.messages() == ["inside"]


def test_the_watchdogs_lines_are_written_at_the_runner_locks_release_not_by_an_entry_that_logged_nothing(env):
    """B4: what the watchdog queued is written by the loop when it lets go of the runner lock; an entry point that
    logged nothing itself (`attempts`, which the node's acknowledgement path calls under its ack lock) writes and
    waits for nothing."""
    from agent_comms import node_sessions
    runner = env.node.sessions
    with _Records() as rec:
        _on_watch_thread(lambda: node_sessions.log.warning("the watchdog's line"))
        with runner.ack_lock:
            runner.host.attempts()
        assert rec.records == [], "not by an entry that logged nothing"
        with runner.lock:
            pass
        assert rec.messages() == ["the watchdog's line"], "by the runner lock's release"


def test_a_drain_with_another_drainer_stalled_is_bounded_when_asked():
    """B5: `atexit`'s drain is best effort: with another thread's drain stalled it gives up after its timeout."""
    import threading
    import time as _time
    from agent_comms import hostlog, session_host
    stall, entered = threading.Event(), threading.Event()

    def act(r):
        if r.getMessage() == "stalls":
            entered.set()
            stall.wait(10)
    with _Records(act=act):
        t = threading.Thread(target=lambda: session_host.log.warning("stalls"), daemon=True)
        t.start()
        try:
            assert entered.wait(5)
            _on_watch_thread(lambda: session_host.log.warning("waits"))
            started = _time.monotonic()
            assert hostlog.drain(timeout=0.2, everything=True) is False
            assert _time.monotonic() - started < 2
        finally:
            stall.set()
            t.join(5)
