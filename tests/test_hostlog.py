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
from test_hosted_sessions import P2, senv, start  # noqa: F401 (the fixture)
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
    from agent_comms import hostlog, session_host
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
        # (Bounded below WAIT_SECONDS: a nested drain that waited on itself would wait that long at each nested
        # exit, so its mutant FAILS here instead of hanging the run.)
        assert done.wait(min(1.0, hostlog.WAIT_SECONDS / 2)), "no deadlock, no wait on itself"
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
    logged nothing itself (`attempts`) writes and waits for nothing."""
    from agent_comms import node_sessions
    runner = env.node.sessions
    with _Records() as rec:
        _on_watch_thread(lambda: node_sessions.log.warning("the watchdog's line"))
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
            assert hostlog.drain(everything=True, timeout=0.2, wait_all=True) is False
            assert _time.monotonic() - started < 2
        finally:
            stall.set()
            t.join(5)


# ---------------------------------------------------------------- review round 1: R1 (every lock) and R2 (threads)


def _refused_once(env, kind, code):
    """A report refused once with `code` at its first rung and taken at the next. `ordinary`: a Claude session's
    status report, whose modes name the steer pair (no event carries a steer outcome); `steer`: a Claude `next_turn`
    and a Codex `in_turn`, each in its own report. (runner, the POSTs made, the sessions refused)."""
    from test_claude_steering import _claude_next_turn_and_codex_steer_pending, _posts, cinit, written
    from test_hosted_tree import proc_of
    runner = env.node.sessions
    if kind == "steer":
        a, b, runner = _claude_next_turn_and_codex_steer_pending(env)
        sids = {a, b}
    else:
        sid = start(env, "a turn")["session_id"]
        tick(env)
        child = proc_of(env, sid)[1]
        child.emit(cinit(sid, written(child)[-1]["uuid"]))
        tick(env)
        assert "held" in runner.host.modes(sid)
        runner.reported.pop(sid, None)      # (its status goes again)
        sids = {sid}
    refused = set()

    def refuse(path, body):
        sid = path.rsplit("/", 1)[-1]
        if sid in sids and sid not in refused and (kind == "steer" or "held" in (body.get("modes") or [])):
            refused.add(sid)
            return code
        return None
    return runner, _posts(runner, refuse), sids


@pytest.mark.parametrize("mode", ["blocks", "raises", "reenters"])
@pytest.mark.parametrize("code", [400, 422])
@pytest.mark.parametrize("kind", ["ordinary", "steer"])
def test_a_refused_reports_fallback_writes_its_line_after_the_report_lock(env, kind, code, mode):
    """R1 (review round 1): a report refused with 400 or 422 and sent again at its next rung (`_post_report`, under
    `_flush`'s report lock): its line is written once no lock of the host-side modules is held, after the retry was
    sent; a handler that blocks holds up neither the retry nor the final report, one that raises changes nothing, and
    one that re-enters `_flush` does not deadlock."""
    import threading
    runner, seen, sids = _refused_once(env, kind, code)
    release, entered, at_entry, reentered = threading.Event(), threading.Event(), [], []

    def act(r):
        if "the hub refused the report" not in r.getMessage() or entered.is_set():
            return
        at_entry.append(len(seen))
        entered.set()
        if mode == "raises":
            raise RuntimeError("a log handler failed")
        if mode == "reenters":
            reentered.append(runner._flush())
        else:
            release.wait(10)
    with Guard(runner=runner) as g, _Records(act=act):
        done = threading.Event()
        t = threading.Thread(target=lambda: (runner._flush(), done.set()), daemon=True)
        t.start()
        try:
            assert entered.wait(5), "the refusal's line was written"
            assert mode == "blocks" or done.wait(5), "no deadlock"
            assert at_entry[0] == len(sids) * 2, "the retries were sent before any handler was entered"
        finally:
            release.set()
        assert done.wait(5)
    assert not g.violations, g.violations
    assert [p for p, _ in seen].count(next(iter(sids))) == 2
    assert not [x for x, _ in runner.pending if x in sids], "the final report went"
    if mode == "reenters":
        assert reentered == [True]


def test_a_failed_checkpoint_is_written_by_the_loop_and_never_holds_it(env):
    """R2 (review round 1; the amendment's A4): the checkpoint sender's POST fails with a blocking handler installed:
    (i) the sender returns without entering the handler (it is enqueue-only) and the loop is not held by it; (ii) the
    loop's next pass writes the line into that blocked handler holding no lock of the host-side modules, and the
    watchdog's SIGTERM and SIGKILL still go meanwhile."""
    import threading
    import httpx
    runner = env.node.sessions
    a, ca, n, s_uuid = claude_steered(env)
    b, cb, mb = _ordinary_turn_in_flight(env, project=P2)
    cb.exits_on_terminate = False
    real = runner.client.request
    posting, go, failed = threading.Event(), threading.Event(), threading.Event()

    def request(method, url, *x, **kw):
        if (kw.get("json") or {}).get("held") is not None:
            posting.set()
            go.wait(10)         # (it fails once the loop's passes are over: no loop pass writes it meanwhile)
            failed.set()
            raise httpx.ConnectError("the hub is away")
        return real(method, url, *x, **kw)
    runner.client.request = request
    release, entered, where = threading.Event(), threading.Event(), []

    def act(r):
        if "was not taken" in r.getMessage():
            where.append(threading.current_thread())
            entered.set()
            release.wait(10)
    with Guard(runner=runner) as g, _Records(act=act):
        runner.start_checkpoint_sender()
        try:
            ca.emit(cresult(a, n, text="N's reply"))
            tick(env, times=2)
            assert posting.wait(5), "the sender is posting the checkpoint"
            go.set()
            assert failed.wait(5), "the checkpoint POST failed"
            assert _wait(lambda: runner.checkpoint_thread.is_alive() and not runner.checkpoint_queue)
            assert not entered.wait(0.5), ("(i) the sender entered no handler: it is enqueue-only", where)
            loop = threading.Thread(target=lambda: tick(env), daemon=True)
            loop.start()
            assert entered.wait(5) and where == [loop], "(ii) the loop's drain writes it"
            env.clock.advance(runner.self_stop_seconds + 1)
            ident = _on_watch_thread(runner.enforce_deadlines)
            assert cb.terminated, "the watchdog's SIGTERM, while the loop's handler is blocked"
            env.clock.advance(runner.host.kill_grace + 1)
            _on_watch_thread(runner.enforce_deadlines)
            assert cb.killed, "and its SIGKILL"
            assert ident not in [t.ident for t in where]
        finally:
            release.set()
            runner.stopping.set()
            runner.checkpoint_thread.join(5)
    assert not g.violations, g.violations


def _wait(pred, seconds=5.0):
    import time as _time
    deadline = _time.monotonic() + seconds
    while _time.monotonic() < deadline:
        if pred():
            return True
        _time.sleep(0.02)
    return pred()


HOST_SIDE = ["session_host.py", "node_sessions.py", "node.py", "node_groups.py", "providers/__init__.py",
             "providers/claude.py", "providers/codex.py", "hostlog.py"]
LOCK_KINDS = {"Lock", "RLock", "Condition", "Semaphore", "BoundedSemaphore", "Event", "Barrier"}
# (Review round 1, R1) The locks of the host-side modules under which no line can be logged, each with why; every
# other lock constructed there is a boundary (`hostlog.HostLock`). DESIGN_NOTES "Host-side logging: review round 1"
# has the whole table.
NOT_BOUNDARIES = {
    ("session_host.py", "self.usage_lock"): "held only to copy or update the usage dict (clean_usage logs nothing)",
    ("node_sessions.py", "self.latch_lock"): "held only for a comparison and an assignment",
    ("node_sessions.py", "self.checkpoint_cond"): "held only to queue, pop or clear checkpoints, and to wait or notify",
    ("node_sessions.py", "class _Stopping"): "an Event: its lock is held only inside set/wait (steer_stopping first)",
    ("hostlog.py", "_cond"): "the queue's own lock: never held across a handler, a log call or foreign code",
}


def _module(rel):
    import ast
    from pathlib import Path
    import agent_comms
    path = Path(agent_comms.__file__).parent / rel
    return ast.parse(path.read_text()), path


def _parents(tree):
    import ast
    up = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            up[child] = node
    return up


def _owner(node, up):
    """Where a lock is constructed: the assignment's target, or `class X` for a base class."""
    import ast
    at = node
    while at in up:
        parent = up[at]
        if isinstance(parent, ast.ClassDef) and at in parent.bases:
            return f"class {parent.name}"
        if isinstance(parent, ast.Assign):
            return ast.unparse(parent.targets[0])
        if isinstance(parent, ast.AnnAssign):
            return ast.unparse(parent.target)
        at = parent
    return None


def test_every_lock_of_the_host_side_modules_is_a_boundary_or_reviewed():
    """R1 (review round 1): every `threading` lock, condition, event or semaphore constructed in the host-side modules
    is either wrapped as a boundary (`hostlog.HostLock(...)`) or on the reviewed list of those under which no line can
    be logged; a lock added later cannot fall outside the invariant silently, and the list has no stale entry."""
    import ast
    found = set()
    for rel in HOST_SIDE:
        tree, path = _module(rel)
        up = _parents(tree)
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
                    and node.value.id == "threading" and node.attr in LOCK_KINDS):
                continue
            parent = up.get(node)
            call = parent if isinstance(parent, ast.Call) and parent.func is node else None
            wrapper = up.get(call) if call is not None else None
            if isinstance(wrapper, ast.Call) and call in wrapper.args:
                name = ast.unparse(wrapper.func)
                if name in ("hostlog.HostLock", "HostLock"):
                    continue        # a boundary
                if name == "threading.Condition":
                    continue        # (a Condition's own lock: the Condition is what is reviewed)
            key = (rel, _owner(node, up))
            assert key in NOT_BOUNDARIES, f"{path.name}:{node.lineno}: {key[1]} is a lock outside the invariant"
            found.add(key)
    assert found == set(NOT_BOUNDARIES), set(NOT_BOUNDARIES) - found


def test_the_locks_a_line_can_be_logged_under_are_boundaries_at_run_time(env):
    """R1 (review round 1): the runner's lock, report lock and ack lock, the host's cutoff lock, the registry's lock and
    a steer attempt's record lock are boundaries (`HostLock`) as constructed."""
    from agent_comms import hostlog
    from agent_comms.session_host import SteerSlot
    runner = env.node.sessions
    for lock in (runner.lock, runner.report_lock, runner.ack_lock, runner.host._steer_stop_lock,
                 runner.host.groups.lock, SteerSlot("u", "1", "t").lock):
        assert isinstance(lock, hostlog.HostLock)


# (Review round 1, R2) Every thread the host-side modules start: the loop drains; every other one is enqueue-only.
THREADS = {"hosted-sessions": ("run_forever", "the loop: it drains"),
           "hosted-sessions-deadlines": ("_watch", "enqueue-only"),
           "hosted-sessions-checkpoints": ("_send_checkpoints", "enqueue-only")}


def test_every_thread_the_host_side_modules_start_is_reviewed_and_every_background_one_is_enqueue_only():
    """R2 (review round 1): every `threading.Thread(...)` in the host-side modules is on the reviewed list; each one
    other than the loop runs its whole body inside `hostlog.enqueue_only()`."""
    import ast
    found, bodies = {}, {}
    for rel in HOST_SIDE:
        tree, path = _module(rel)
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                bodies[node.name] = node
            if isinstance(node, ast.Call) and ast.unparse(node.func) == "threading.Thread":
                kw = {k.arg: k.value for k in node.keywords}
                assert "name" in kw and isinstance(kw["name"], ast.Constant), f"{path.name}:{node.lineno}: unnamed"
                found[kw["name"].value] = ast.unparse(kw["target"]).rsplit(".", 1)[-1]
    assert {n: t for n, (t, _) in THREADS.items()} == found
    for name, (target, kind) in THREADS.items():
        if kind != "enqueue-only":
            continue
        withs = [w for w in ast.walk(bodies[target]) if isinstance(w, ast.With)
                 and any(ast.unparse(i.context_expr) == "hostlog.enqueue_only()" for i in w.items)]
        assert withs, f"{target} is not enqueue-only"


def test_the_background_threads_register_as_enqueue_only_when_they_run(env):
    """R2: the deadline watchdog and the checkpoint sender, started as the node starts them, are in
    `hostlog.ENQUEUE_ONLY` while they run, and out of it once they end."""
    import threading
    from agent_comms import hostlog
    runner = env.node.sessions
    runner.watch_seconds = 0.05
    runner.start_checkpoint_sender()
    watch = threading.Thread(target=runner._watch, daemon=True)
    watch.start()
    try:
        assert _wait(lambda: runner.checkpoint_thread.ident in hostlog.ENQUEUE_ONLY
                     and watch.ident in hostlog.ENQUEUE_ONLY)
    finally:
        runner.stopping.set()
        runner.checkpoint_thread.join(5)
        watch.join(5)
        runner.stopping.clear()
    assert runner.checkpoint_thread.ident not in hostlog.ENQUEUE_ONLY and watch.ident not in hostlog.ENQUEUE_ONLY


def test_a_thread_waits_for_its_own_records_behind_a_stalled_drainer_only_up_to_the_deadline(monkeypatch):
    """R2 (the amendment's A2): while another thread's drain is stalled in a handler, a thread with nothing of its own
    pending waits for nothing; one with a record of its own waits for it at most WAIT_SECONDS, then returns; the
    active drainer writes that record once its handler returns."""
    import threading
    import time as _time
    from agent_comms import hostlog, session_host
    monkeypatch.setattr(hostlog, "WAIT_SECONDS", 0.3)
    stall, entered = threading.Event(), threading.Event()

    def act(r):
        if r.getMessage() == "stalls":
            entered.set()
            stall.wait(10)
    with _Records(act=act) as rec:
        t = threading.Thread(target=lambda: session_host.log.warning("stalls"), daemon=True)
        t.start()
        try:
            assert entered.wait(5)
            t0 = _time.monotonic()
            with hostlog.BOUNDARY:
                pass
            assert _time.monotonic() - t0 < 0.2, "nothing of its own: no wait"
            t0 = _time.monotonic()
            session_host.log.warning("mine")
            waited = _time.monotonic() - t0
            assert 0.25 <= waited < 2, waited
            assert "mine" not in rec.messages()
        finally:
            stall.set()
            t.join(5)
    assert rec.messages() == ["stalls", "mine"], "written by the active drainer"


# ---------------------------------------------------------------- R3: an exception's reviewed facts


def test_facts_are_reviewed_integers_rendered_by_the_writer(monkeypatch):
    """R3 (review round 1; the amendment's A3): `hostlog.facts` keeps an exception's type name and, for a known class,
    an integer read at the call (an HTTP status error's status code, an OSError's errno, a subprocess's return code);
    the WRITER renders them; nothing is rendered at the call, and no exception text is ever read."""
    import errno as _errno
    import subprocess
    import httpx
    from agent_comms import hostlog, session_host
    rendered = []
    real = hostlog._render_facts
    monkeypatch.setattr(hostlog, "_render_facts", lambda *a: (rendered.append(a), real(*a))[1])
    req = httpx.Request("POST", "http://secret-host.example/api/nodes/sessions/x?token=SECRET")
    refused = _errno.ECONNREFUSED
    cases = [
        (httpx.HTTPStatusError("SECRET text", request=req, response=httpx.Response(502, request=req)),
         "HTTPStatusError status=502"),
        (ConnectionRefusedError(refused, "SECRET text"), f"ConnectionRefusedError errno={refused}"),
        (OSError("SECRET text"), "OSError"),
        (subprocess.CalledProcessError(3, ["SECRET", "argv"], output="SECRET"), "CalledProcessError returncode=3"),
        (httpx.ConnectError("SECRET text", request=req), "ConnectError"),
        (ValueError("SECRET text"), "ValueError"),
    ]

    def logs():
        for e, _ in cases:
            session_host.log.warning("it failed: %s", hostlog.facts(e))
    with _Records() as rec:
        _on_watch_thread(logs)
        assert rendered == [], "nothing rendered at the call"
        hostlog.drain(everything=True)
    assert rec.messages() == [f"it failed: {want}" for _, want in cases]
    assert len(rendered) == len(cases) and not any("SECRET" in m for m in rec.messages())


def test_facts_never_call_a_hostile_exceptions_own_code():
    """R3: an exception whose `__str__`, `__repr__` and attributes raise or return secrets: `facts` reads none of them
    (its type's name and its errno, read with no code of its own); the line is written."""
    from agent_comms import hostlog, session_host
    touched = []

    class Hostile(OSError):
        def __str__(self):
            touched.append("str")
            return "SECRET"

        __repr__ = __str__

        def __getattribute__(self, name):
            touched.append(name)
            raise AssertionError("read")

        @property
        def response(self):
            touched.append("response")
            return "SECRET"
    e = Hostile(7, "SECRET")
    touched.clear()
    with _Records() as rec:
        session_host.log.warning("it failed: %s", hostlog.facts(e))
    assert rec.messages() == ["it failed: Hostile errno=7"] and touched == []


# ---------------------------------------------------------------- R4: nothing formatted at the call


LOG_METHODS = {"debug", "info", "warning", "error", "exception", "critical"}
ALLOWED_CALLS = {"getattr", "len", "type", "isinstance", "hostlog.facts", "hostlog.later"}


def _formats(node):
    """What in a log call's argument would format at the call: an f-string, `%` or `+` on text, `.format`, `.join`,
    `str`/`repr`/`format`, any call not on the reviewed list."""
    import ast
    for n in ast.walk(node):
        if isinstance(n, ast.JoinedStr):
            return "an f-string"
        if isinstance(n, ast.BinOp) and isinstance(n.op, ast.Mod):
            return "%-formatting"
        if isinstance(n, ast.BinOp) and isinstance(n.op, ast.Add) and any(
                isinstance(x, ast.Constant) and isinstance(x.value, str) for x in (n.left, n.right)):
            return "text concatenation"
        if isinstance(n, ast.Call):
            f = ast.unparse(n.func)
            if f in ALLOWED_CALLS or (isinstance(n.func, ast.Attribute) and n.func.attr == "get"):
                continue
            return f"a call to {f}"
    return None


def test_nothing_is_formatted_at_a_log_call():
    """R4 (review round 1): in the migrated modules every log call's message is a literal (or a template passed
    through a wrapper) and no argument formats at the call; the wrappers' own call sites (`_say_held`, `_say_wait`)
    are checked the same way."""
    import ast
    wrappers = {"_say_held": 4, "_say_wait": 1}
    checked = 0
    for rel in ["session_host.py", "node_sessions.py", "node.py", "providers/claude.py", "providers/codex.py"]:
        tree, path = _module(rel)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            f = node.func
            if isinstance(f.value, ast.Name) and f.value.id == "log" and f.attr in LOG_METHODS | {"log"}:
                at = 1 if f.attr == "log" else 0
            elif f.attr in wrappers:
                at = wrappers[f.attr]
            else:
                continue
            msg, args = node.args[at], node.args[at + 1:]
            where = f"{path.name}:{node.lineno}"
            assert (isinstance(msg, ast.Constant) and isinstance(msg.value, str)) or isinstance(msg, ast.Name), where
            for a in args + [k.value for k in node.keywords]:
                bad = _formats(a)
                assert bad is None, f"{where}: {bad}: {ast.unparse(a)}"
            checked += 1
    assert checked > 100


def test_a_later_value_is_rendered_by_the_writer_only(monkeypatch):
    """R4: the Claude version is sliced and counted at the call and escaped by the writer (`hostlog.later`)."""
    from agent_comms import hostlog
    from agent_comms.providers import claude
    calls = []
    real = claude.re.sub
    monkeypatch.setattr(claude.re, "sub", lambda *a, **k: (calls.append(1), real(*a, **k))[1])
    value = hostlog.later(claude._shown, "2.1.330\n"[:64], 8)
    assert calls == []
    assert str(value) == claude.shown_version("2.1.330\n") and calls == [1, 1]


# ---------------------------------------------------------------- review round 2: R5 (no drain inside a signal sequence)


def _stubborn_shutdown(senv, when):
    """A session process and an ordinary run that both ignore SIGTERM, and a warning queued by an enqueue-only thread
    (the checkpoint sender's), on the node's own fixture: (node, session child, run child, kill times, TERM time)."""
    from agent_comms import hostlog, node_sessions
    from agent_comms.node import _Local
    from test_dispatch import FakeChild
    from test_hosted_sessions import children
    from test_session_host import FakeSessionChild
    node = senv.node
    node.session_spawner.child = lambda pid: FakeSessionChild(pid, exits_on_terminate=False)
    start(senv, "a turn")
    tick(senv)
    [session_child] = children(senv)
    run_child = FakeChild(5000, exits_on_terminate=False)
    node.running["run-1"] = _Local("run-1", run_child, senv.clock(), 1800.0, 10.0)
    t0, kills = senv.clock(), {}
    for name, child in (("session", session_child), ("run", run_child)):
        real = child.kill
        child.kill = lambda _r=real, _n=name: (kills.setdefault(_n, senv.clock() - t0), _r())[1]
    if when == "fallback":
        # an exception between the SIGTERMs and the SIGKILLs: the `stop_all` fallback (and the sessions' own stop)
        real_alive, calls = node.runs_alive, []

        def runs_alive():
            calls.append(1)
            if len(calls) == 2:
                raise RuntimeError("a shutdown step failed")
            return real_alive()
        node.runs_alive = runs_alive

    def queue():
        with hostlog.enqueue_only():
            node_sessions.log.warning("session %s: the checkpoint of message %s was not taken (%s); it is not sent "
                                      "again", "s", 1, "ConnectError")
    import threading
    t = threading.Thread(target=queue)
    t.start()
    t.join(5)
    return node, session_child, run_child, kills


@pytest.mark.parametrize("mode", ["blocks", "raises"])
@pytest.mark.parametrize("when", ["normal", "fallback"])
def test_a_line_queued_before_shutdown_is_written_after_its_last_signal(senv, when, mode):
    """R5 (review round 2; the amendment's A1 to A3): shutdown's signal sequence (the runs' and the sessions' SIGTERM,
    the grace, both SIGKILLs, and the `stop_all` fallback when a step raises between them) is ONE boundary: a warning
    the checkpoint sender queued before it is written only after the last signal, so a handler that blocks or raises
    on it delays no SIGKILL: both go AT the grace deadline (10 s of the node's clock from the SIGTERMs, within one
    0.2 s wait step), and the line is written after them."""
    import threading
    from agent_comms.node import shut_down
    node, session_child, run_child, kills = _stubborn_shutdown(senv, when)
    release, entered, at_entry = threading.Event(), threading.Event(), []

    def act(r):
        if "was not taken" not in r.getMessage() or entered.is_set():
            return
        at_entry.append((session_child.killed, run_child.killed))
        entered.set()
        if mode == "raises":
            raise RuntimeError("a log handler failed")
        release.wait(10)
    with _Records(act=act):
        done, err = threading.Event(), []

        def run():
            try:
                shut_down(node)
            except BaseException as e:      # noqa: BLE001 (the test reads it)
                err.append(e)
            finally:
                done.set()
        threading.Thread(target=run, daemon=True).start()
        try:
            assert _wait(lambda: session_child.killed and run_child.killed, 5), (
                "both SIGKILLs went while the handler was blocked", kills, at_entry)
        finally:
            release.set()
        assert done.wait(10), "shutdown ended"
    assert entered.is_set() and at_entry[0] == (1, 1), ("the line, written after both SIGKILLs", at_entry)
    assert session_child.terminated >= 1 and run_child.terminated >= 1
    if when == "normal":
        assert kills["session"] == pytest.approx(10, abs=0.25) and kills["run"] == pytest.approx(10, abs=0.25), kills
        assert not err, err


# ---------------------------------------------------------------- review round 2: R6 (the construction forms), R7


def test_locks_are_built_only_in_the_reviewed_forms():
    """R6 (review round 2; the amendment's A4): in the host-side modules a lock, condition, event or semaphore is built
    only as `threading.X(...)` (or passed as `default_factory`) or wrapped as `hostlog.HostLock(...)`: `threading` is
    imported only as itself and used only as `threading.<name>`; `from threading import` of those names, any alias of
    the module, and a bare use of one of those names are failures. A helper that returns a lock would build it outside
    an assignment, so the lock scan above already refuses it; the reviewed list of such factories is empty."""
    import ast
    factories = {}      # (module, function) -> why it may return a lock: none is reviewed
    for rel in HOST_SIDE:
        tree, path = _module(rel)
        up = _parents(tree)
        # (a bare name is one of threading's unless the module imports it from elsewhere: providers' `Event`)
        elsewhere = {a.asname or a.name for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)
                     and n.module != "threading" for a in n.names}
        elsewhere |= {n.name for n in ast.walk(tree) if isinstance(n, (ast.ClassDef, ast.FunctionDef))}
        for node in ast.walk(tree):
            where = f"{path.name}:{getattr(node, 'lineno', '?')}"
            if isinstance(node, ast.Import):
                for a in node.names:
                    assert a.name != "threading" or a.asname is None, f"{where}: threading imported as {a.asname}"
            if isinstance(node, ast.ImportFrom) and node.module == "threading":
                bad = {a.name for a in node.names} & (LOCK_KINDS | {"*"})
                assert not bad, f"{where}: from threading import {bad}"
            if isinstance(node, ast.Name) and node.id in LOCK_KINDS - elsewhere:
                raise AssertionError(f"{where}: a bare {node.id}")
            if isinstance(node, ast.Name) and node.id == "threading":
                parent = up.get(node)
                assert isinstance(parent, ast.Attribute) and parent.value is node, f"{where}: threading used as a value"
            if (isinstance(node, ast.Return) and node.value is not None
                    and any(isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id == "threading"
                            and n.attr in LOCK_KINDS for n in ast.walk(node.value))):
                fn = next((a for a in _ancestors(node, up) if isinstance(a, (ast.FunctionDef, ast.Lambda))), None)
                assert (rel, getattr(fn, "name", None)) in factories, f"{where}: an unreviewed lock factory"


def _ancestors(node, up):
    while node in up:
        node = up[node]
        yield node


def test_a_lock_built_through_an_alias_a_bare_name_or_a_factory_is_refused(tmp_path):
    """R6: the scans above refuse each way around them, as source text: an aliased module, `from threading import`, a
    bare name, a helper that returns a lock."""
    import ast
    cases = ["import threading as t\nlock = t.Lock()\n",
             "from threading import Lock\nlock = Lock()\n",
             "from threading import *\n",
             "import threading\nL = threading\nlock = L.RLock()\n",
             "import threading\ndef make():\n    return threading.Lock()\nlock = make()\n"]
    for src in cases:
        tree = ast.parse(src)
        up = _parents(tree)
        refused = False
        for node in ast.walk(tree):
            if isinstance(node, ast.Import) and any(a.name == "threading" and a.asname for a in node.names):
                refused = True
            if isinstance(node, ast.ImportFrom) and node.module == "threading":
                refused = True
            if isinstance(node, ast.Name) and node.id in LOCK_KINDS:
                refused = True
            if isinstance(node, ast.Name) and node.id == "threading" and not (
                    isinstance(up.get(node), ast.Attribute) and up[node].value is node):
                refused = True
            if isinstance(node, ast.Return) and node.value is not None and any(
                    isinstance(n, ast.Attribute) and n.attr in LOCK_KINDS for n in ast.walk(node.value)):
                refused = True
        assert refused, src


def test_facts_run_no_code_of_a_subclass_of_a_reviewed_class():
    """R7 (review round 2): a hostile subclass of each reviewed class, with a `__dict__` property, a
    `__getattribute__` and a `__str__` that record and raise: `facts` enters none of them. A subclass of
    `HTTPStatusError` or `CalledProcessError` gives its type's name only; an OSError subclass still gives its errno,
    read through OSError's own member descriptor."""
    import subprocess
    import httpx
    from agent_comms import hostlog, session_host
    touched = []

    def hostile(base):
        class Hostile(base):
            @property
            def __dict__(self):
                touched.append("__dict__")
                raise AssertionError("read")

            def __getattribute__(self, name):
                touched.append(name)
                raise AssertionError("read")

            def __str__(self):
                touched.append("str")
                return "SECRET"
            __repr__ = __str__
        Hostile.__name__ = "Hostile" + base.__name__
        return Hostile
    req = httpx.Request("GET", "http://h/x")
    made = [hostile(httpx.HTTPStatusError)("SECRET", request=req, response=httpx.Response(502, request=req)),
            hostile(subprocess.CalledProcessError)(3, ["SECRET"]),
            hostile(OSError)(7, "SECRET")]
    touched.clear()
    with _Records() as rec:
        for e in made:
            session_host.log.warning("it failed: %s", hostlog.facts(e))
    assert rec.messages() == ["it failed: HostileHTTPStatusError", "it failed: HostileCalledProcessError",
                              "it failed: HostileOSError errno=7"]
    assert touched == [], touched


def _queue_a_line(text="a queued line"):
    """A line queued by an enqueue-only thread (as the checkpoint sender's or the watchdog's)."""
    import threading
    from agent_comms import hostlog, node_sessions

    def queue():
        with hostlog.enqueue_only():
            node_sessions.log.warning(text)
    t = threading.Thread(target=queue)
    t.start()
    t.join(5)


def _blocking_on(text, state):
    """A handler action that blocks (until released) on `text`, saying `state()` when entered."""
    import threading
    release, entered, at_entry = threading.Event(), threading.Event(), []

    def act(r):
        if r.getMessage() == text and not entered.is_set():
            at_entry.append(state())
            entered.set()
            release.wait(10)
    return act, release, entered, at_entry


def _in_thread(fn):
    import threading
    done = threading.Event()
    threading.Thread(target=lambda: (fn(), done.set()), daemon=True).start()
    return done


def test_the_nodes_own_stop_all_writes_no_line_between_its_signals(senv):
    """R5 (review round 2): `Node.stop_all` on its own (two ordinary runs that ignore SIGTERM, a queued line, a
    blocking handler): both SIGTERMs and both SIGKILLs go, the KILLs at the grace deadline, and the line is written
    after the last of them (the registry's release between two runs' signals writes nothing)."""
    from agent_comms.node import _Local
    from test_dispatch import FakeChild
    node = senv.node
    a, b = FakeChild(5001, exits_on_terminate=False), FakeChild(5002, exits_on_terminate=False)
    node.running["run-a"] = _Local("run-a", a, senv.clock(), 1800.0, 10.0)
    node.running["run-b"] = _Local("run-b", b, senv.clock(), 1800.0, 10.0)
    _queue_a_line()
    act, release, entered, at_entry = _blocking_on("a queued line", lambda: (a.terminated, b.terminated, a.killed,
                                                                             b.killed))
    with _Records(act=act):
        done = _in_thread(node.stop_all)
        try:
            assert _wait(lambda: a.killed and b.killed, 5), "both SIGKILLs went while the handler was blocked"
        finally:
            release.set()
        assert done.wait(10)
    assert at_entry == [(1, 1, 1, 1)], at_entry


def test_a_node_pass_writes_no_line_between_the_runs_it_signals(senv):
    """R5 (review round 2): a node pass that stops two runs the hub no longer lists (a queued line, a blocking
    handler): both get their SIGTERM in that pass; the line is not written between them (nor by the pass at all: it
    has nothing of its own pending), and the next drain that writes everything writes it."""
    from agent_comms.node import _Local
    from test_dispatch import FakeChild
    node = senv.node
    a, b = FakeChild(5001, exits_on_terminate=False), FakeChild(5002, exits_on_terminate=False)
    node.running["run-a"] = _Local("run-a", a, senv.clock(), 1800.0, 10.0)
    node.running["run-b"] = _Local("run-b", b, senv.clock(), 1800.0, 10.0)
    _queue_a_line()
    act, release, entered, at_entry = _blocking_on("a queued line", lambda: (a.terminated, b.terminated))
    with _Records(act=act):
        done = _in_thread(node.tick)
        try:
            assert _wait(lambda: a.terminated and b.terminated, 5), "both SIGTERMs went while the handler was blocked"
        finally:
            release.set()
        assert done.wait(10)
        assert at_entry in ([], [(1, 1)]), at_entry
        from agent_comms import hostlog
        hostlog.drain(everything=True)
    assert at_entry and at_entry[-1] == (1, 1), at_entry
