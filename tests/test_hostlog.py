"""Host-side logging (DESIGN_NOTES "Host-side logging"): the package-owned adapter (`agent_comms.hostlog`).

THE guarantee (DESIGN_NOTES "Host-side logging: one writer"): a log handler that blocks forever, or raises, changes
nothing but log output: every signal, report, save and return happens at its time. Every line is a deferred record
made at the call; ONE writer thread is the only thread that ever enters a handler; a lifecycle thread (the node's
main thread in `run`, the session loop, the watchdog, the checkpoint sender, the writer) never waits for it; a
standalone caller (a test) waits, bounded, for its own last line at depth 0."""
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
    """The node's own routes (its pass, its watchdog, its shutdown), run under the production policy: a handler that
    blocks or raises is entered only on the writer thread, never by a thread holding a lock of the host-side modules;
    lines were written."""
    from agent_comms import hostlog
    act = route(env)
    with Guard(runner=env.node.sessions, mode=mode) as g, hostlog.enqueue_only():
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


def _line():
    import sys
    return sys._getframe(1).f_lineno


# ---------------------------------------------------------------- review round 1: R1 (every lock) and R2 (threads)


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


# ---------------------------------------------------------------- R3: an exception's reviewed facts


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


# ---------------------------------------------------------------- review round 2: R6 (the construction forms), R7


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


# ---------------------------------------------------------------- helpers


class _Records:
    """A handler on the package logger that keeps every record it is handed (and, optionally, does something with
    it: `act(record)`), with the thread that entered it. Leaving it waits (bounded) for the writer to be idle first,
    so what was queued meanwhile is there."""

    def __init__(self, act=None, level=logging.DEBUG):
        import threading
        self.records, self.threads, self.act, self.level = [], [], act, level
        outer = self

        class H(logging.Handler):
            def emit(self, rec):
                outer.records.append(rec)
                outer.threads.append(threading.current_thread().name)
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
        from agent_comms import hostlog
        if hasattr(hostlog, "flush"):
            hostlog.flush(10)
        else:   # (before the one writer, for a reproduction run: the caller-thread drain)
            hostlog.drain(everything=True)
        self.logger.removeHandler(self.handler)
        self.logger.setLevel(self.saved)


def _on_watch_thread(fn):
    """`fn` on a thread registered as the watchdog's (a lifecycle thread); returns its ident once it is done."""
    t, done, ident, err = _on_watchdog(None, fn=fn)
    assert done.wait(10) and not err, err
    return ident[0]


def _wait(pred, seconds=5.0):
    import time as _time
    deadline = _time.monotonic() + seconds
    while _time.monotonic() < deadline:
        if pred():
            return True
        _time.sleep(0.02)
    return pred()


def _production(fn):
    """`fn` on a thread under the production policy (`hostlog.enqueue_only`, as `node.run` registers its thread),
    asserting it is registered: (done, errors, results)."""
    import threading
    from agent_comms import hostlog
    done, err, out = threading.Event(), [], []

    def run():
        try:
            with hostlog.enqueue_only():
                assert threading.get_ident() in hostlog.ENQUEUE_ONLY
                assert not getattr(hostlog, "may_wait", hostlog.__dict__.get("may_drain"))()
                out.append(fn())
        except BaseException as e:      # noqa: BLE001 (the test reads it)
            err.append(e)
        finally:
            done.set()
    threading.Thread(target=run, daemon=True).start()
    return done, err, out


class _Blocked:
    """A handler action that blocks the writer on its first record matching `text` (until released, at most 30 s),
    or raises on it (`mode` "raises"); `state()` is recorded at entry. Released by the test's teardown (`with`)."""

    def __init__(self, text, mode="blocks", state=lambda: None):
        import threading
        self.text, self.mode, self.state = text, mode, state
        self.release, self.entered, self.at_entry, self.where = threading.Event(), threading.Event(), [], []

    def __call__(self, r):
        import threading
        if self.text not in r.getMessage() or self.entered.is_set():
            return
        self.at_entry.append(self.state())
        self.where.append(threading.current_thread().name)
        self.entered.set()
        if self.mode == "raises":
            raise RuntimeError("a log handler failed")
        self.release.wait(30)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.release.set()


def _queue_from_another_thread(text="a line queued by another thread"):
    """A line queued beforehand by another lifecycle thread (as the checkpoint sender's or the watchdog's)."""
    import threading
    from agent_comms import hostlog, node_sessions

    def queue():
        with hostlog.enqueue_only():
            node_sessions.log.warning(text)
    t = threading.Thread(target=queue)
    t.start()
    t.join(5)


# ---------------------------------------------------------------- the mechanism


def test_handlers_are_entered_only_on_the_writer_thread_at_depth_0(henv):
    """W1: lines logged on the test thread, a watchdog thread, inside a host call and under a host lock are all handed
    to the handlers by the writer thread, at its depth 0, holding no lock; it is a daemon thread, and a lifecycle one."""
    import threading
    from agent_comms import hostlog, session_host
    depths = []
    open_session(henv)
    henv.host.send(SID, "go")
    with _Records(act=lambda r: depths.append((hostlog.depth(), threading.get_ident() in hostlog.ENQUEUE_ONLY))) as rec:
        session_host.log.warning("from the test thread")
        _on_watch_thread(lambda: session_host.log.warning("from a watchdog thread"))
        henv.host.tick()
        with henv.host._steer_stop_lock:
            session_host.log.warning("under the cutoff lock")
    assert {"from the test thread", "from a watchdog thread", "under the cutoff lock"} <= set(rec.messages())
    assert set(rec.threads) == {hostlog.WRITER_NAME}
    assert set(depths) == {(0, True)}
    [w] = [t for t in threading.enumerate() if t.name == hostlog.WRITER_NAME]
    assert w.daemon


def test_a_line_the_logger_would_not_emit_is_not_kept():
    """B3: call-time level filtering: a line below the logger's level at the call is not queued (lowering the level
    later does not bring it back); one at or above it is written even if the level is raised before the writer gets
    to it."""
    from agent_comms import session_host
    real = logging.getLogger("agent_comms.session_host")
    level = real.level
    with _Records(level=logging.NOTSET) as rec:
        real.setLevel(logging.WARNING)
        try:
            session_host.log.info("an info line")
            session_host.log.warning("a warning line")
            real.setLevel(logging.ERROR)
        finally:
            real.setLevel(level)
    assert "an info line" not in rec.messages() and "a warning line" in rec.messages()


def _line():
    import sys
    return sys._getframe(1).f_lineno


def test_a_deferred_record_is_the_calls_snapshot():
    """B3: the record is made at the call: its time, thread and caller are the call's (no source lookup), an
    exception is its type's name and its frames (no text), an object argument is its type's name (never formatted),
    a `%`-mapping is copied with reduced values."""
    import threading
    import time as _time
    from agent_comms import session_host

    class Loud:
        def __str__(self):
            raise AssertionError("formatted")

        __repr__ = __format__ = __str__

    class Marked(Exception):
        pass
    at = {}

    def logs():
        at["before"] = _time.time()
        at["thread"] = threading.current_thread().name
        at["line"] = _line() + 1
        session_host.log.warning("an object %s and a list %s and %d and %s", Loud(), [1], 7, None)
        session_host.log.info("a mapping %(a)s %(b)s", {"a": "text", "b": Loud()})
        try:
            raise Marked("PINEAPPLE-STEER-TEXT")
        except Marked:
            session_host.log.exception("it failed")
        at["after"] = _time.time()
    with _Records() as rec:
        _on_watch_thread(logs)
    first, mapping, failed = [r for r in rec.records if r.name == "agent_comms.session_host"]
    assert at["before"] <= first.created <= at["after"]
    assert first.threadName == at["thread"]
    assert first.pathname == __file__ and first.lineno == at["line"] and first.funcName == "logs"
    assert first.getMessage() == "an object Loud and a list list and 7 and None"
    assert mapping.getMessage() == "a mapping text Loud"
    assert failed.exc_info is None and failed.hostlog_exc[0] == "Marked"
    assert [f[2] for f in failed.hostlog_exc[1]] == ["logs"]
    text = logging.Formatter().format(failed)
    assert "Marked" in text and "in logs" in text and "PINEAPPLE" not in text, text


def test_enqueue_order_across_threads_is_the_write_order():
    """W1: two threads log in turn (each line after the other's): the writer writes them in that order, the queue's
    (`hostlog_seq`)."""
    import threading
    from agent_comms import session_host
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
    assert [m for m in rec.messages() if m.startswith("line ")] == [f"line {i}" for i in range(8)]
    seqs = [r.hostlog_seq for r in rec.records]
    assert seqs == sorted(seqs)


def test_a_lifecycle_thread_never_waits_for_a_blocked_writer(env, monkeypatch):
    """W2 and C1: with the writer blocked in a handler, every kind of lifecycle thread returns at once from its log
    calls and boundary exits: a thread under `enqueue_only` (the node's main thread in `run`, the checkpoint sender), a
    watchdog thread, and the writer itself (a handler that logs)."""
    import time as _time
    from agent_comms import hostlog, session_host
    monkeypatch.setattr(hostlog, "ACK_SECONDS", 5.0)
    took = []

    def timed():
        t0 = _time.monotonic()
        for i in range(20):
            session_host.log.warning("lifecycle %d", i)
        with hostlog.BOUNDARY:
            pass
        took.append(_time.monotonic() - t0)
    with _Blocked("blocks the writer") as blocked, _Records(act=blocked):
        session_host.log.warning("blocks the writer")
        assert blocked.entered.wait(5)
        done, err, _ = _production(timed)
        assert done.wait(5) and not err, err
        _on_watch_thread(timed)
        assert all(t < 0.5 for t in took), took
        blocked.release.set()


def test_a_standalone_wait_is_bounded_latched_and_clears_on_progress(monkeypatch):
    """W3 and C3: with the writer blocked, a standalone caller's own line is waited for at most ACK_SECONDS; the stall
    latch then spares every later wait; once the writer has made progress beyond the latch, waits are normal again."""
    import time as _time
    from agent_comms import hostlog, session_host
    monkeypatch.setattr(hostlog, "ACK_SECONDS", 0.3)
    with _Blocked("blocks the writer") as blocked, _Records(act=blocked) as rec:
        _queue_from_another_thread("blocks the writer")
        assert blocked.entered.wait(5)
        t0 = _time.monotonic()
        session_host.log.warning("waited for")
        first = _time.monotonic() - t0
        t0 = _time.monotonic()
        session_host.log.warning("not waited for (latched)")
        second = _time.monotonic() - t0
        assert 0.25 <= first < 2 and second < 0.1, (first, second)
        assert hostlog._state["stalled_at"] is not None
        blocked.release.set()
        assert _wait(lambda: hostlog._state["stalled_at"] is None), "a later completion clears the latch"
        monkeypatch.setattr(hostlog, "ACK_SECONDS", 5.0)
        session_host.log.warning("synchronous again")
        assert "synchronous again" in rec.messages()


def test_a_waiter_on_a_record_the_bound_evicted_is_released_at_once(monkeypatch):
    """C3 and C6: with the writer blocked, the queue overflows: the oldest go; a standalone caller waiting for its own
    line that was evicted returns at once; when the writer resumes it says once how many went, then writes the rest
    in order."""
    import threading
    import time as _time
    from agent_comms import hostlog, session_host
    monkeypatch.setattr(hostlog, "ACK_SECONDS", 10.0)
    with _Blocked("blocks the writer") as blocked, _Records(act=blocked) as rec:
        _queue_from_another_thread("blocks the writer")
        assert blocked.entered.wait(5)
        with hostlog.BOUNDARY:
            session_host.log.warning("mine, to be evicted")

            def flood():
                for i in range(hostlog.QUEUE_MAX + 6):
                    session_host.log.warning("flood %d", i)
            t = threading.Thread(target=lambda: _on_watch_thread(flood))
            t.start()
            t.join(20)
            t0 = _time.monotonic()
        took = _time.monotonic() - t0
        assert took < 1, took
        blocked.release.set()
    said = rec.messages()
    assert "mine, to be evicted" not in said
    assert said[1] == ("7 deferred log line(s) were dropped "
                       "(the queue was full, or a line could not be made a record)"), said[:3]
    assert said[2] == "flood 6" and said[-1] == f"flood {hostlog.QUEUE_MAX + 5}"


def test_a_handler_that_raises_anything_is_skipped_and_the_writer_goes_on():
    """W1: a handler that raises (an Exception, a SystemExit, a KeyboardInterrupt) on a record: the next handler still
    gets that record, the writer goes on with the next records, and it is the same writer."""
    from agent_comms import hostlog, session_host
    kinds = iter([RuntimeError("a log handler failed"), SystemExit(3), KeyboardInterrupt()])

    def act(r):
        if r.getMessage().startswith("raise"):
            raise next(kinds)
    with _Records(act=act) as bad, _Records() as good:
        for i in range(3):
            session_host.log.warning("raise %d", i)
        session_host.log.warning("after")
        writer = hostlog._writer[0]
    assert good.messages()[-4:] == ["raise 0", "raise 1", "raise 2", "after"] and bad.messages()[-1] == "after"
    assert writer.is_alive() and hostlog._writer[0] is writer


def test_a_dead_writer_is_restarted_and_there_is_only_ever_one(monkeypatch):
    """W1 and C6: a writer found dead is replaced at the next standalone record (a lazy start, at depth 0); many
    standalone threads logging their first lines at once start exactly ONE writer."""
    import threading
    from agent_comms import hostlog, session_host
    dead = threading.Thread(target=lambda: None, name=hostlog.WRITER_NAME)
    dead.start()
    dead.join()
    with hostlog._cond:
        hostlog._writer[0] = dead       # (the running writer is let go and leaves: a dead one is registered)
        hostlog._cond.notify_all()
    gate = threading.Barrier(16)

    def first():
        gate.wait(5)
        session_host.log.warning("first line")
    with _Records() as rec:
        threads = [threading.Thread(target=first) for _ in range(16)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
        assert _wait(lambda: len([t for t in threading.enumerate() if t.name == hostlog.WRITER_NAME]) == 1)
    assert rec.messages().count("first line") == 16
    assert hostlog._writer[0] is not dead and hostlog._writer[0].is_alive()


def test_flush_is_bounded_with_a_blocked_writer(monkeypatch):
    """W5: `flush` waits for the queue to be empty and the writer idle, at most its bound, entering no handler."""
    import time as _time
    from agent_comms import hostlog
    with _Blocked("blocks the writer") as blocked, _Records(act=blocked):
        _queue_from_another_thread("blocks the writer")
        assert blocked.entered.wait(5)
        t0 = _time.monotonic()
        assert hostlog.flush(0.3) is False
        assert 0.25 <= _time.monotonic() - t0 < 2
        blocked.release.set()
        assert hostlog.flush(5) is True


def test_a_handler_that_logs_or_calls_back_into_the_host_neither_waits_nor_deadlocks(henv):
    """C1 and C6: a handler (on the writer) that logs through hostlog and calls public host methods: the writer waits
    for nothing, nothing deadlocks, and the inner lines are written after the outer one."""
    from agent_comms import session_host
    open_session(henv)
    henv.host.send(SID, "go")
    calls = []

    def act(r):
        if r.getMessage().startswith("started session") and not calls:
            calls.append(1)
            session_host.log.warning("from the handler")
            henv.host.send(SID, "again")
            henv.host.state(SID)
    with _Records(act=act) as rec:
        done, err, _ = _production(henv.host.tick)
        assert done.wait(5) and not err, err
        assert _wait(lambda: "from the handler" in rec.messages())
    said = rec.messages()
    outer = next(i for i, m in enumerate(said) if m.startswith("started session"))
    assert said.index("from the handler") > outer


def test_a_thread_holding_a_host_lock_never_waits(env, monkeypatch):
    """W3/W4: a standalone caller holding a lock of the host-side modules (the runner lock, the cutoff lock, the
    registry lock) does not wait for a blocked writer when it logs; it waits (bounded) only once it is at depth 0."""
    import time as _time
    from agent_comms import hostlog, session_host
    monkeypatch.setattr(hostlog, "ACK_SECONDS", 0.3)
    runner = env.node.sessions
    with _Blocked("blocks the writer") as blocked, _Records(act=blocked):
        _queue_from_another_thread("blocks the writer")
        assert blocked.entered.wait(5)
        for lock in (runner.lock, runner.host._steer_stop_lock, runner.host.groups.lock):
            with hostlog._cond:
                hostlog._state["stalled_at"] = None
            t0 = _time.monotonic()
            with lock:
                session_host.log.warning("under the lock")
                inside = _time.monotonic() - t0
            assert inside < 0.1, inside
            assert _time.monotonic() - t0 >= 0.25, "at depth 0, the bounded wait"
        with hostlog._cond:
            hostlog._state["stalled_at"] = None
        with hostlog.BOUNDARY:      # (the predicate itself, whoever asks it: nobody waits inside a boundary)
            session_host.log.warning("inside a boundary")
            t0 = _time.monotonic()
            assert not hostlog.may_wait() and hostlog._acknowledge() is False
            assert _time.monotonic() - t0 < 0.1
        blocked.release.set()


# ---------------------------------------------------------------- static: the lock, thread and construction checks


def _lock_problems(rel, tree, reviewed=None):
    """THE checker (R1, R6, R10): every problem with how a module of the host-side modules builds its locks, as
    (line, what): a lock, condition, event or semaphore built outside an assignment, `hostlog.HostLock(...)` or a base
    class, or built in an assignment not on the reviewed list (`reviewed`, default NOT_BOUNDARIES); `threading`
    aliased or used as a value; `from threading import` of a lock kind or `*`; a bare lock name the module does not
    define or import from elsewhere; a `return` of a lock built in the function (directly, or through a local it was
    assigned to)."""
    import ast
    reviewed = NOT_BOUNDARIES if reviewed is None else reviewed
    up = _parents(tree)
    problems = []
    elsewhere = {a.asname or a.name for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)
                 and n.module != "threading" for a in n.names}
    elsewhere |= {n.name for n in ast.walk(tree) if isinstance(n, (ast.ClassDef, ast.FunctionDef))}

    def is_lock(n):
        return (isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id == "threading"
                and n.attr in LOCK_KINDS)
    for node in ast.walk(tree):
        line = getattr(node, "lineno", 0)
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name == "threading" and a.asname:
                    problems.append((line, f"threading imported as {a.asname}"))
        if isinstance(node, ast.ImportFrom) and node.module == "threading":
            bad = {a.name for a in node.names} & (LOCK_KINDS | {"*"})
            if bad:
                problems.append((line, f"from threading import {sorted(bad)}"))
        if isinstance(node, ast.Name) and node.id in LOCK_KINDS - elsewhere:
            problems.append((line, f"a bare {node.id}"))
        if isinstance(node, ast.Name) and node.id == "threading" and not (
                isinstance(up.get(node), ast.Attribute) and up[node].value is node):
            problems.append((line, "threading used as a value"))
        if is_lock(node):
            parent = up.get(node)
            call = parent if isinstance(parent, ast.Call) and parent.func is node else None
            wrapper = up.get(call) if call is not None else None
            if isinstance(wrapper, ast.Call) and call in wrapper.args and ast.unparse(wrapper.func) in (
                    "hostlog.HostLock", "HostLock", "threading.Condition"):
                continue
            owner = _owner(node, up)
            if (rel, owner) not in reviewed:
                problems.append((line, f"{owner} is a lock outside the invariant"))
        if isinstance(node, (ast.FunctionDef, ast.Lambda)):
            built = {ast.unparse(t) for n in ast.walk(node) if isinstance(n, ast.Assign)
                     and any(is_lock(x) for x in ast.walk(n.value)) for t in n.targets}
            for n in ast.walk(node):
                if isinstance(n, ast.Return) and n.value is not None and (
                        any(is_lock(x) for x in ast.walk(n.value)) or ast.unparse(n.value) in built):
                    problems.append((getattr(n, "lineno", 0), "a function that returns a lock"))
    return problems


def test_every_lock_of_the_host_side_modules_is_built_as_reviewed():
    """R1, R6 (and R10's checker): in every host-side module, every lock is a boundary (`hostlog.HostLock`) or on the
    reviewed list of those under which no line can be logged, built in the allowed forms only; the list has no stale
    entry."""
    import ast
    found = set()
    for rel in HOST_SIDE:
        tree, path = _module(rel)
        problems = _lock_problems(rel, tree)
        assert not problems, [f"{path.name}:{ln}: {what}" for ln, what in problems]
        up = _parents(tree)
        for node in ast.walk(tree):
            if (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "threading"
                    and node.attr in LOCK_KINDS and (rel, _owner(node, up)) in NOT_BOUNDARIES):
                found.add((rel, _owner(node, up)))
    assert found == set(NOT_BOUNDARIES), set(NOT_BOUNDARIES) - found


@pytest.mark.parametrize("src", [
    "import threading as t\nlock = t.Lock()\n",
    "from threading import Lock\nlock = Lock()\n",
    "from threading import *\n",
    "import threading\nL = threading\nlock = L.RLock()\n",
    "import threading\ndef make():\n    return threading.Lock()\n",
    "import threading\ndef make():\n    lock = threading.Lock()\n    return lock\n",
    "import threading\nclass A:\n    def __init__(self):\n        self.x = threading.Lock()\n",
    "import threading\nqueue = [threading.Condition()]\n",
], ids=["alias", "from-import", "star", "value", "factory", "factory-via-local", "unreviewed-attr", "in-a-list"])
def test_the_checker_refuses_each_way_around_it(src):
    """R10 (review round 3): the SAME checker the modules are held to refuses each bypass, as source text: an aliased
    module, `from threading import`, a star import, `threading` as a value, a factory (returning the lock directly or
    through a local), an unreviewed lock, a lock kept anywhere but an assignment's own value."""
    import ast
    assert _lock_problems("fixture.py", ast.parse(src)), src


def test_the_checker_accepts_the_reviewed_forms():
    """R10: what the modules do passes: a boundary, a dataclass factory of one, a reviewed native lock."""
    import ast
    src = ("import threading\nfrom . import hostlog\nfrom dataclasses import field\n"
           "lock = hostlog.HostLock(threading.RLock())\n"
           "class A:\n    slot: object = field(default_factory=lambda: hostlog.HostLock(threading.RLock()))\n"
           "    def __init__(self):\n        self.latch = threading.Lock()\n")
    assert _lock_problems("fixture.py", ast.parse(src), reviewed={("fixture.py", "self.latch")}) == []


THREADS = {"hosted-sessions": ("run_forever", "the session loop"),
           "hosted-sessions-deadlines": ("_watch", "the watchdog"),
           "hosted-sessions-checkpoints": ("_send_checkpoints", "the checkpoint sender"),
           "WRITER_NAME": ("_write_loop", "the writer")}


def test_every_thread_the_host_side_modules_start_is_a_lifecycle_thread():
    """W2 and C1: every `threading.Thread(...)` in the host-side modules is on the reviewed list and registers as a
    lifecycle thread (it never waits for the writer): the loop, the watchdog and the checkpoint sender run their
    bodies inside `hostlog.enqueue_only()`, the writer registers itself; and `node.run` registers the node's main
    thread before anything is built or recovered."""
    import ast
    found, bodies = {}, {}
    for rel in HOST_SIDE:
        tree, path = _module(rel)
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                bodies[node.name] = node
            if isinstance(node, ast.Call) and ast.unparse(node.func) == "threading.Thread":
                kw = {k.arg: k.value for k in node.keywords}
                name = kw["name"].value if isinstance(kw["name"], ast.Constant) else ast.unparse(kw["name"])
                found[name] = ast.unparse(kw["target"]).rsplit(".", 1)[-1]
    assert {n: t for n, (t, _) in THREADS.items()} == found
    for name, (target, _) in THREADS.items():
        src = ast.unparse(bodies[target])
        assert "hostlog.enqueue_only()" in src or "ENQUEUE_ONLY.add(threading.get_ident())" in src, target
    run = bodies["run"]
    first = run.body[1] if isinstance(run.body[0], ast.Expr) else run.body[0]
    assert isinstance(first, ast.With) and ast.unparse(first.items[0].context_expr) == "hostlog.enqueue_only()", \
        "node.run registers its thread first"
    assert ast.unparse(first.body[0]) == "hostlog.start_writer()", "and starts the writer eagerly (A4)"


def test_no_fork_without_exec_in_the_host_side_modules():
    """W7: no `os.fork` (nor a fork-starting multiprocessing context) in the host-side modules: the writer thread
    never has to be accounted for in a forked child (`os.register_at_fork` is not needed)."""
    for rel in HOST_SIDE:
        text = _module(rel)[1].read_text()
        assert "os.fork" not in text and "multiprocessing" not in text, rel


# ---------------------------------------------------------------- R3: facts rendered by the writer


def test_facts_are_reviewed_integers_rendered_by_the_writer(monkeypatch):
    """R3 (review round 1; its A3): `hostlog.facts` keeps an exception's type name and, for a known class, an integer
    read at the call; the WRITER renders them (on its thread, never at the call), and no exception text is read."""
    import errno as _errno
    import subprocess
    import threading
    import httpx
    from agent_comms import hostlog, session_host
    rendered = []
    real = hostlog._render_facts
    monkeypatch.setattr(hostlog, "_render_facts",
                        lambda *a: (rendered.append(threading.current_thread().name), real(*a))[1])
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
    with _Records() as rec:
        for e, _ in cases:
            session_host.log.warning("it failed: %s", hostlog.facts(e))
    assert rec.messages() == [f"it failed: {want}" for _, want in cases]
    assert rendered == [hostlog.WRITER_NAME] * len(cases), "rendered once each, by the writer"
    assert not any("SECRET" in m for m in rec.messages())


# ---------------------------------------------------------------- W7: the acceptance matrix (the production policy)
#
# Every case runs the node's code under the production policy (`_production`: the registration `node.run` makes), with
# a line queued beforehand by another lifecycle thread, and a handler that blocks forever (until the test's teardown)
# or raises on it. The guarantee: the handler changes nothing but log output.


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
def test_w7_1_a_refused_reports_retry_and_final_report_go_whatever_a_handler_does(env, kind, code, mode):
    """W7.1 (R1's matrix): a report refused with 400 or 422 is sent again at its next rung and the final report goes,
    whatever the handler does with the refusal's line (blocks, raises, re-enters `_flush` from the writer); every
    handler entry is on the writer thread, holding no lock of the host-side modules."""
    runner, seen, sids = _refused_once(env, kind, code)
    reentered = []
    with _Blocked("the hub refused the report", "raises" if mode == "raises" else "blocks") as blocked:
        act = blocked
        if mode == "reenters":
            def act(r):
                if "the hub refused the report" in r.getMessage() and not reentered:
                    reentered.append(runner._flush())
        with Guard(runner=runner) as g, _Records(act=act):
            _queue_from_another_thread()
            done, err, out = _production(runner._flush)
            assert done.wait(10) and not err, err
            assert out == [True]
            if mode == "reenters":
                assert _wait(lambda: reentered == [True]), "a handler that re-enters `_flush` does not deadlock"
            blocked.release.set()
    assert [p for p, _ in seen].count(next(iter(sids))) == 2, "the retry went"
    assert not [x for x, _ in runner.pending if x in sids], "the final report went"
    assert not g.violations, g.violations


def test_w7_2_a_failed_checkpoint_neither_holds_the_sender_nor_the_loop(env):
    """W7.2 (the amendment's A4, now for the loop too): the checkpoint POST fails with a blocking handler installed:
    the sender returns without entering it; the loop's passes go on; the watchdog's SIGTERM and SIGKILL go; the
    handler is entered by the writer only."""
    import threading
    import httpx
    runner = env.node.sessions
    a, ca, n, s_uuid = claude_steered(env)
    b, cb, mb = _ordinary_turn_in_flight(env, project=P2)
    cb.exits_on_terminate = False
    real = runner.client.request
    failed = threading.Event()

    def request(method, url, *x, **kw):
        if (kw.get("json") or {}).get("held") is not None:
            failed.set()
            raise httpx.ConnectError("the hub is away")
        return real(method, url, *x, **kw)
    runner.client.request = request
    with _Blocked("was not taken") as blocked, _Records(act=blocked):
        runner.start_checkpoint_sender()
        try:
            ca.emit(cresult(a, n, text="N's reply"))
            for _ in range(2):
                done, err, _ = _production(lambda: tick(env))
                assert done.wait(10) and not err, err
            assert failed.wait(5) and blocked.entered.wait(5)
            assert blocked.where == ["hostlog-writer"], "entered by the writer, not the sender"
            assert _wait(lambda: runner.checkpoint_thread.is_alive() and not runner.checkpoint_queue)
            done, err, _ = _production(lambda: tick(env))
            assert done.wait(5) and not err, "the loop's next pass goes on with the handler blocked"
            env.clock.advance(runner.self_stop_seconds + 1)
            _on_watch_thread(runner.enforce_deadlines)
            assert cb.terminated, "the watchdog's SIGTERM"
            env.clock.advance(runner.host.kill_grace + 1)
            _on_watch_thread(runner.enforce_deadlines)
            assert cb.killed, "and its SIGKILL"
        finally:
            blocked.release.set()
            runner.stopping.set()
            runner.checkpoint_thread.join(5)


def _stubborn_pair(senv):
    """A session process and an ordinary run that both ignore SIGTERM; their SIGKILL times on the node's clock from
    now: (node, session child, run child, kills)."""
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
    return node, session_child, run_child, kills


@pytest.mark.parametrize("mode", ["blocks", "raises"])
@pytest.mark.parametrize("when", ["normal", "fallback", "kill_raises"])
def test_w7_3_shutdown_signals_at_its_times_whatever_a_handler_does(senv, when, mode):
    """W7.3 (R5, R9): shutdown with a session process and an ordinary run that ignore SIGTERM and a line queued
    beforehand, under the production policy, with a handler that blocks or raises on it: both SIGTERMs go, and both
    SIGKILLs AT the grace deadline (10 s of the node's clock, within one 0.2 s wait step). `fallback`: a step raises
    between TERM and KILL (the `stop_all` fallback; the error propagates). `kill_raises` (R9, the LAST possible KILL):
    `sessions.kill()` raises before it signals; `quietly` says so, and the sessions' own shutdown kills the
    survivor."""
    from agent_comms.node import shut_down
    node, session_child, run_child, kills = _stubborn_pair(senv)
    if when == "fallback":
        real_alive, calls = node.runs_alive, []

        def runs_alive():
            calls.append(1)
            if len(calls) == 2:
                raise RuntimeError("a shutdown step failed")
            return real_alive()
        node.runs_alive = runs_alive
    if when == "kill_raises":
        def kill():
            raise RuntimeError("the sessions' kill failed")
        node.sessions.kill = kill
    with _Blocked("a line queued by another thread", mode) as blocked, _Records(act=blocked) as rec:
        _queue_from_another_thread()
        done, err, _ = _production(lambda: shut_down(node))
        assert done.wait(10), "shutdown ended"
        assert session_child.killed and run_child.killed, ("both SIGKILLs", kills)
        blocked.release.set()
    assert session_child.terminated >= 1 and run_child.terminated >= 1
    assert set(rec.threads) <= {"hostlog-writer"}
    if when == "fallback":
        assert [type(e) for e in err] == [RuntimeError], err
    else:
        assert not err, err
    if when == "normal":
        assert kills["session"] == pytest.approx(10, abs=0.25) and kills["run"] == pytest.approx(10, abs=0.25), kills
    if when == "kill_raises":
        assert kills["run"] == pytest.approx(10, abs=0.25), kills
        assert "hosted sessions: a shutdown step failed" in rec.messages(), "quietly's own line"


# ---------------------------------------------------------------- W7.4 to W7.6: the node's own loop (`node.run`)
#
# `node.run` is driven on a thread with its outside world replaced: its settings and client, the `Node` it builds
# (the fixture's), its SIGTERM handler (not installed: the test process keeps its own) and the sessions' threads (not
# started); its exit (`os._exit`, which the stalled case takes) is recorded instead. SIGTERM is `run`'s `on_term`,
# which only raises KeyboardInterrupt in the thread running the loop, at the point it is executing: the test raises
# it at that same point (the hub call the loop waits on, the wait of the shutdown), which is what the signal does.


class _Hub:
    def __init__(self, real, act):
        self.real, self.act = real, act

    def request(self, method, path, **kw):
        out = self.act(method, path)
        if out is not None:
            return out
        return self.real.request(method, path, **kw)


def _run_node(senv, monkeypatch, act, poll=0.01):
    """`node.run` on its own thread with the fixture's node, a hub client doing `act(method, path)`: (done, errors,
    exits)."""
    import threading
    from types import SimpleNamespace
    import httpx
    from agent_comms import config as config_mod
    from agent_comms import hostlog
    from agent_comms import node as node_mod
    node = senv.node
    node.client = _Hub(node.client, act)
    monkeypatch.setattr(node_mod, "log_to_stderr", lambda: None)
    monkeypatch.setattr(config_mod.Settings, "load", classmethod(lambda cls, *a, **k: SimpleNamespace(dispatch={})))
    monkeypatch.setattr(config_mod, "home", lambda: senv.tmp)
    monkeypatch.setattr(node_mod, "DispatchConfig", SimpleNamespace(from_dict=lambda *a, **k: node.config))

    class Client:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False
    monkeypatch.setattr(node_mod, "httpx", SimpleNamespace(Client=Client, HTTPError=httpx.HTTPError,
                                                           HTTPStatusError=httpx.HTTPStatusError,
                                                           Timeout=httpx.Timeout))
    monkeypatch.setattr(node_mod, "Node", lambda *a, **k: node)
    monkeypatch.setattr(node_mod.signal, "signal", lambda *a, **k: None)
    monkeypatch.setattr(node_mod, "EXIT_FLUSH_SECONDS", 0.3, raising=False)
    exits = []
    monkeypatch.setattr(node_mod.os, "_exit", lambda status: exits.append(status))
    if node.sessions is not None:
        monkeypatch.setattr(node.sessions, "start", lambda: None)
    done, err, registered = threading.Event(), [], []

    def run():
        try:
            real_recover = node.recover

            def recover():
                registered.append(threading.get_ident() in hostlog.ENQUEUE_ONLY)
                return real_recover()
            node.recover = recover
            node_mod.run("http://hub", "token", poll_seconds=poll)
        except BaseException as e:      # noqa: BLE001 (the test reads it)
            err.append(e)
        finally:
            done.set()
    threading.Thread(target=run, daemon=True).start()
    return done, err, exits, registered


def _assignments(path):
    return path.startswith("/api/nodes/assignments")


@pytest.mark.parametrize("mode", ["blocks", "raises"])
@pytest.mark.parametrize("where", ["hub_call", "hub_unreachable"])
def test_w7_4_a_sigterm_that_ends_the_nodes_loop_starts_its_shutdown(senv, monkeypatch, where, mode):
    """W7.4 (R8): SIGTERM arrives (a) while a pass waits on a hub call, with a line of that pass queued, or (c)
    between passes, right after the loop's own `hub unreachable` line, with a handler that blocks forever or raises
    on that line: `run` registered its thread before recovery, `shut_down` starts, both processes get their SIGTERM
    and their SIGKILL AT the grace deadline, and the loop's thread never entered the handler."""
    import httpx
    from agent_comms import node as node_mod
    node, session_child, run_child, kills = _stubborn_pair(senv)
    calls = []

    def act(method, path):
        if method != "GET" or not _assignments(path):
            return None
        calls.append(1)
        if where == "hub_call":
            node_mod.log.warning("a line of the pass")
            raise KeyboardInterrupt         # (SIGTERM, while the pass waits on this call)
        if len(calls) == 1:
            raise httpx.ConnectError("the hub is away")      # ("hub unreachable", the loop's own line)
        raise KeyboardInterrupt             # (SIGTERM, at the next pass)
    text = "a line of the pass" if where == "hub_call" else "hub unreachable"
    with _Blocked(text, mode) as blocked, _Records(act=blocked) as rec:
        _queue_from_another_thread()
        done, err, exits, registered = _run_node(senv, monkeypatch, act)
        assert done.wait(15), "run ended"
        assert session_child.killed and run_child.killed, kills
        blocked.release.set()
    assert registered == [True], "the node's thread is a lifecycle thread from before its recovery"
    assert not err, err
    assert kills["session"] == pytest.approx(10, abs=0.25) and kills["run"] == pytest.approx(10, abs=0.25), kills
    assert set(rec.threads) <= {"hostlog-writer"}
    assert exits in ([], [0]), exits


def _second_sigterm(senv, monkeypatch, blocked_mode):
    """A first SIGTERM at a pass's hub call, a second one during the shutdown's grace wait: what is signalled, and
    when (the node's clock), with the writer blocked or free."""
    node, session_child, run_child, kills = _stubborn_pair(senv)
    real_sleep, slept = node.sleep, []

    def sleep(s):
        slept.append(s)
        if len(slept) == 3:
            raise KeyboardInterrupt         # (the second SIGTERM, during the grace wait)
        return real_sleep(s)
    node.sleep = sleep

    def act(method, path):
        if method == "GET" and _assignments(path):
            raise KeyboardInterrupt         # (the first SIGTERM)
        return None
    with _Blocked("a line queued by another thread", "blocks" if blocked_mode else "raises") as blocked, \
            _Records(act=blocked if blocked_mode else None):
        _queue_from_another_thread()
        done, err, exits, _ = _run_node(senv, monkeypatch, act)
        assert done.wait(15)
        blocked.release.set()
    return ((session_child.terminated, session_child.killed, run_child.terminated, run_child.killed), dict(kills),
            [type(e) for e in err])


def test_w7_4_a_second_sigterm_during_cleanup_behaves_the_same_with_a_blocked_writer(senv, monkeypatch, tmp_path):
    """W7.4 and C6: a second SIGTERM during the shutdown's grace wait: what is signalled, when, and what escapes
    `run` are the same with the writer blocked in a handler as with it free (no more than main asks)."""
    blocked = _second_sigterm(senv, monkeypatch, True)
    from test_hosted_sessions import session_env
    from agent_comms import hostlog
    hostlog._reset()
    (tmp_path / "again").mkdir()
    other = session_env(tmp_path / "again")
    monkeypatch.undo()
    free = _second_sigterm(other, monkeypatch, False)
    assert blocked == free, (blocked, free)
    signals, kills, err = blocked
    assert signals[1] and signals[3], "both SIGKILLs"


def test_w7_4_the_loops_other_exits_stop_the_sessions_whatever_a_handler_does(senv, monkeypatch):
    """W7.4: the loop ends by `SystemExit` (or any exception but an interrupt) at a pass's hub call, with a line of
    that pass queued and a handler that blocks forever on it: the sessions' processes are stopped (SIGTERM, then
    SIGKILL at the grace deadline) and the exception escapes `run` as on main."""
    from agent_comms import node as node_mod
    node, session_child, run_child, kills = _stubborn_pair(senv)

    def act(method, path):
        if method == "GET" and _assignments(path):
            node_mod.log.warning("a line of the pass")
            raise SystemExit(4)
        return None
    with _Blocked("a line of the pass") as blocked, _Records(act=blocked):
        done, err, exits, _ = _run_node(senv, monkeypatch, act)
        assert done.wait(15)
        assert session_child.killed, kills
        blocked.release.set()
    assert [type(e) for e in err] == [SystemExit] and err[0].code == 4
    assert kills["session"] == pytest.approx(10, abs=0.25), kills


def test_w7_5_an_ordinary_run_that_ignores_sigterm_is_killed_at_its_grace_across_passes(senv, monkeypatch):
    """W7.5 (cross-pass escalation): an ordinary run times out and ignores its SIGTERM; between passes the loop writes
    its `hub unreachable` line, and the writer is blocked on a line another thread queued: the next passes go on and
    the run's SIGKILL goes at its grace deadline (one pass is 5 s of the node's clock here)."""
    import httpx
    from agent_comms.node import _Local
    from test_dispatch import FakeChild
    node = senv.node
    child = FakeChild(5000, exits_on_terminate=False)
    node.running["run-1"] = _Local("run-1", child, senv.clock(), 1.0, 10.0)
    term = []
    real_term = child.terminate
    child.terminate = lambda: (term.append(senv.clock()), real_term())[1]

    def act(method, path):
        if method == "GET" and _assignments(path):
            if child.killed:
                raise KeyboardInterrupt
            senv.clock.advance(5)
            raise httpx.ConnectError("the hub is away")
        return None
    with _Blocked("a line queued by another thread") as blocked, _Records(act=blocked):
        _queue_from_another_thread()
        killed_at = []
        real_kill = child.kill
        child.kill = lambda: (killed_at.append(senv.clock()), real_kill())[1]
        done, err, exits, _ = _run_node(senv, monkeypatch, act)
        assert _wait(lambda: child.killed, 10), "the run's SIGKILL went with the writer blocked"
        assert done.wait(15)
        blocked.release.set()
    assert term and killed_at and killed_at[0] - term[0] == pytest.approx(10, abs=5.01), (term, killed_at)


def test_w7_6_recovery_and_start_up_reach_the_sessions_and_the_loop(senv, monkeypatch):
    """W7.6: a handler blocks forever on a line another thread queued, and the node logs during its own recovery: the
    sessions' recovery is reached, and the loop's first pass runs."""
    import threading
    from agent_comms import node as node_mod
    node = senv.node
    reached, passes = threading.Event(), []
    real_save = node._save_state

    def save():
        node_mod.log.warning("a line of the recovery")
        return real_save()
    node._save_state = save
    real_recover = node.sessions.recover
    node.sessions.recover = lambda: (reached.set(), real_recover())[1]

    def act(method, path):
        if method == "GET" and _assignments(path):
            passes.append(1)
            raise KeyboardInterrupt
        return None
    with _Blocked("a line queued by another thread") as blocked, _Records(act=blocked):
        _queue_from_another_thread()
        assert blocked.entered.wait(5)
        done, err, exits, registered = _run_node(senv, monkeypatch, act)
        assert reached.wait(5), "the sessions' recovery was reached"
        assert _wait(lambda: passes, 5), "and the loop's first pass"
        assert done.wait(15)
        blocked.release.set()
    assert registered == [True] and not err, err


def test_w7_7_the_session_loops_join_expires_and_shutdown_still_signals(env, monkeypatch):
    """W7.7: at a standalone shutdown the session loop's thread does not end (here: it waits on a hub call); the
    join expires and every session process still gets its SIGTERM and SIGKILL, with a handler blocked on a line
    another thread queued."""
    import threading
    from agent_comms import node_sessions
    monkeypatch.setattr(node_sessions, "JOIN_SECONDS", 0.2)
    runner = env.node.sessions
    sid, child, m = _ordinary_turn_in_flight(env)
    child.exits_on_terminate = False
    stuck = threading.Event()
    runner.thread = threading.Thread(target=lambda: stuck.wait(30), daemon=True)
    runner.thread.start()
    with _Blocked("a line queued by another thread") as blocked, _Records(act=blocked):
        _queue_from_another_thread()
        done, err, _ = _production(runner.shutdown)
        assert done.wait(10) and not err, err
        assert child.terminated and child.killed
        blocked.release.set()
    stuck.set()


# ---------------------------------------------------------------- C4: bounded process exit


_EXIT_SCRIPT = r"""
import logging, sys, threading
from agent_comms import hostlog, node
stalled = sys.argv[1] == "stalled"
gate = threading.Event()

class Stuck(logging.Handler):
    def emit(self, record):
        if stalled:
            gate.wait()             # holds the handler's own lock for good (Handler.handle takes it)

    def close(self):
        print("closed by logging.shutdown", flush=True)
        super().close()

logging.getLogger("agent_comms").addHandler(Stuck())
logging.getLogger("agent_comms").setLevel(logging.INFO)
with hostlog.enqueue_only():
    hostlog.start_writer()          # (as `node.run` does, first: the final wait starts nothing)
    node.log.warning("a line")
    print("signals sent", flush=True)
    node._exit_if_stalled(3 if stalled else 0)
print("returned normally", flush=True)
"""


@pytest.mark.parametrize("case", ["stalled", "free"])
def test_c4_the_nodes_exit_is_bounded_with_a_handler_stuck_holding_its_lock(tmp_path, case):
    """C4: a SUBPROCESS runs the node's exit path (`node._exit_if_stalled`, after its signals) with a handler that
    blocks forever while holding its own lock: it ends within the bound with the status it would have had (here 3),
    skipping `logging.shutdown()`; with nothing stalled it returns and exits normally, `logging.shutdown()` closing the
    handler."""
    import subprocess
    import sys
    import time as _time
    script = tmp_path / "exit.py"
    script.write_text(_EXIT_SCRIPT)
    t0 = _time.monotonic()
    r = subprocess.run([sys.executable, str(script), case], capture_output=True, text=True, timeout=30,
                       cwd=str(tmp_path), env={**__import__("os").environ,
                                               "PYTHONPATH": str(__import__("pathlib").Path(__file__).parents[1])})
    took = _time.monotonic() - t0
    lines = r.stdout.splitlines()
    if case == "stalled":
        assert r.returncode == 3 and lines == ["signals sent"], (r.returncode, r.stdout, r.stderr)
        assert took < 10, took
    else:
        assert r.returncode == 0 and lines == ["signals sent", "returned normally", "closed by logging.shutdown"], (
            r.returncode, r.stdout, r.stderr)


# ---------------------------------------------------------------- one writer, review round 1: R11 (the seal)


_LATE_SCRIPT = r"""
import logging, sys, threading, time
from agent_comms import hostlog, node, node_sessions
late, late_done = threading.Event(), threading.Event()

class Stuck(logging.Handler):
    def emit(self, record):
        if "was not taken" in record.getMessage():
            threading.Event().wait()        # blocks for good, holding the handler's own lock

    def close(self):
        print("closed by logging.shutdown", flush=True)
        super().close()

logging.getLogger("agent_comms").addHandler(Stuck())
logging.getLogger("agent_comms").setLevel(logging.INFO)

def checkpoint_sender():
    with hostlog.enqueue_only():
        late.wait(10)
        node_sessions.log.warning("session %s: the checkpoint of message %s was not taken (%s); it is not sent "
                                  "again", "s", 1, "ConnectError")
        late_done.set()

threading.Thread(target=checkpoint_sender, daemon=True).start()
real = hostlog.final_flush

def final_flush(budget):
    ok = real(budget)
    late.set()                      # (the checkpoint POST fails just after the final wait saw the queue idle)
    late_done.wait(5)
    time.sleep(0.3)
    return ok
hostlog.final_flush = final_flush
with hostlog.enqueue_only():
    hostlog.start_writer()
    node.log.warning("the last report")
    print("signals sent", flush=True)
    node._exit_if_stalled(0)
print("returned normally", flush=True)
"""


def _child(tmp_path, script, *args, timeout=30):
    import os
    import subprocess
    import sys
    from pathlib import Path
    path = tmp_path / "child.py"
    path.write_text(script)
    return subprocess.run([sys.executable, str(path), *args], capture_output=True, text=True, timeout=timeout,
                          cwd=str(tmp_path), env={**os.environ, "PYTHONPATH": str(Path(__file__).parents[1])})


def test_r11_a_line_queued_after_the_final_wait_is_rejected_and_the_exit_is_normal(tmp_path):
    """R11 (Sol's sequence): the checkpoint sender's POST fails just AFTER the node's final wait saw the queue idle, and
    its warning would enter a handler that blocks forever holding its lock: the queue was sealed in the same step as
    the idle decision, so the warning is rejected, the process takes the normal exit, and `logging.shutdown()` runs
    (closing the handler) within the bound."""
    import time as _time
    t0 = _time.monotonic()
    r = _child(tmp_path, _LATE_SCRIPT)
    assert r.returncode == 0, (r.returncode, r.stdout, r.stderr[-2000:])
    assert r.stdout.splitlines() == ["signals sent", "returned normally", "closed by logging.shutdown"], r.stdout
    assert _time.monotonic() - t0 < 20


def test_r11_an_enqueue_after_the_seal_raises_nothing_starts_nothing_and_blocks_nobody(monkeypatch):
    """R11: after the final wait (the seal), a log call from a standalone caller at depth 0 returns at once, raises
    nothing, starts no thread, and its record is counted nowhere; the seal is one-way (only the tests' reset lifts
    it)."""
    import threading
    import time as _time
    from agent_comms import hostlog, session_host
    assert hostlog.final_flush(2.0) is True and hostlog._state["sealed"]
    seq, threads = hostlog._state["seq"], threading.active_count()
    with hostlog._cond:
        hostlog._writer[0] = None           # (even with no writer running, nothing starts one now)
        hostlog._cond.notify_all()
    _time.sleep(0.1)
    threads = threading.active_count()
    t0 = _time.monotonic()
    session_host.log.warning("after the seal")
    assert _time.monotonic() - t0 < 0.1
    assert hostlog._state["seq"] == seq and threading.active_count() == threads and hostlog._writer[0] is None
    assert hostlog.final_flush(0.1) is True, "still idle, still sealed"
    hostlog._reset()
    assert not hostlog._state["sealed"] and hostlog._writer[0] is not None


def test_r11_the_seal_and_the_idle_decision_are_one_step(monkeypatch):
    """R11 (the atomicity): a producer that tries to enqueue exactly when the final wait has decided "idle" cannot get
    in before the seal: its record is rejected and no handler is entered.

    Deterministic: `_cond` is wrapped so that, once "idle" has been decided, any LATER entry of the queue's lock by the
    deciding thread first lets the producer in (and waits until it has enqueued or been rejected). A seal made in a
    second critical section is therefore always preceded by the producer's record; the seal made in the deciding
    critical section never re-enters, and the producer runs only after it."""
    import threading
    from agent_comms import hostlog, session_host
    real_idle_by, real_cond = hostlog._idle_by, hostlog._cond
    go, done, decided, main = threading.Event(), threading.Event(), [False], threading.get_ident()

    class Gate:
        def __getattr__(self, name):
            return getattr(real_cond, name)

        def __enter__(self):
            if decided[0] and threading.get_ident() == main:
                decided[0] = False
                go.set()                    # (the producer runs now, with the queue's lock free)
                assert done.wait(5)
            return real_cond.__enter__()

        def __exit__(self, *a):
            return real_cond.__exit__(*a)

    def idle_by(deadline, **k):
        ok = real_idle_by(deadline, **k)
        decided[0] = ok
        return ok

    def producer():
        go.wait(10)
        session_host.log.warning("between the idle decision and the seal")
        done.set()
    with _Records() as rec:
        monkeypatch.setattr(hostlog, "_idle_by", idle_by)
        monkeypatch.setattr(hostlog, "_cond", Gate())
        threading.Thread(target=lambda: _on_watch_thread(producer), daemon=True).start()
        seq = hostlog._state["seq"]
        try:
            assert hostlog.final_flush(2.0) is True
        finally:
            decided[0] = False
            go.set()                        # (the seal is made: the producer runs now, and is rejected)
            assert done.wait(5)
            monkeypatch.setattr(hostlog, "_cond", real_cond)
        assert hostlog._state["sealed"]
        assert hostlog._state["seq"] == seq, "the late enqueue never advances the sequence (R16)"
    assert "between the idle decision and the seal" not in rec.messages()


def test_r11_a_line_logged_during_the_final_wait_is_rejected(monkeypatch):
    """A3 (any state → sealed): the final wait seals FIRST, then waits for the registered writer to finish the records
    it accepted before: a lifecycle thread's line logged while the writer is still busy with an earlier one is
    rejected (the sequence does not advance), the earlier one is finished, and the wait ends idle."""
    import threading
    from agent_comms import hostlog, session_host
    release, logged = threading.Event(), threading.Event()

    def act(r):
        if r.getMessage() == "earlier":
            release.wait(10)
    with _Records(act=act) as rec:
        with hostlog.enqueue_only():
            session_host.log.warning("earlier")
        assert _wait(lambda: hostlog._state["busy"] is not None)
        seq = hostlog._state["seq"]

        def during():
            assert _wait(lambda: hostlog._state["sealed"], 5)
            session_host.log.warning("during the final wait")
            logged.set()
            release.set()
        threading.Thread(target=lambda: _on_watch_thread(during), daemon=True).start()
        try:
            assert hostlog.final_flush(5.0) is True
        finally:
            release.set()
        assert logged.wait(5)
        assert hostlog._state["seq"] == seq, "rejected: the sequence did not advance"
    assert "earlier" in rec.messages() and "during the final wait" not in rec.messages()


# ---------------------------------------------------------------- R12 (a writer that cannot start), A4, A5


def _no_writer():
    """As in a process whose writer is not running (and none starting): the registered writer is let go (it leaves
    without dequeuing again)."""
    from agent_comms import hostlog
    with hostlog._cond:
        if "gen" in hostlog._state:     # (079306b, for the reproduction run)
            hostlog._state["gen"] += 1
        hostlog._writer[0] = None
        for k, v in (("starting", False), ("start_failed_at", None), ("token", None)):
            if k in hostlog._state:
                hostlog._state[k] = v
        hostlog._cond.notify_all()


def _refuse_the_writer(monkeypatch, error=RuntimeError("can't start new thread")):
    """`Thread.start` raises for the writer's thread (the OS refuses it); other threads start as ever: the calls."""
    import threading
    from agent_comms import hostlog
    real, calls = threading.Thread.start, []

    def start(self):
        if self.name == hostlog.WRITER_NAME:
            calls.append(threading.get_ident())
            raise error
        return real(self)
    monkeypatch.setattr(threading.Thread, "start", start)
    return calls


def test_r12_a_writer_that_cannot_start_never_fails_a_log_call(monkeypatch):
    """R12: a standalone caller's record tries to start the writer (the lazy start, at its depth-0 exit) and the OS
    refuses: the log call returns at once and raises nothing; nobody waits for a writer that is not running; no start
    is left pending. (A2: no cooldown: each standalone exit with records waiting tries once; production makes ONE
    start.)"""
    import time as _time
    from agent_comms import hostlog, session_host
    monkeypatch.setattr(hostlog, "ACK_SECONDS", 5.0)
    _no_writer()
    calls = _refuse_the_writer(monkeypatch)
    t0 = _time.monotonic()
    session_host.log.warning("the first line")
    session_host.log.warning("a second line")
    assert _time.monotonic() - t0 < 0.5
    assert len(calls) == 2, "one start attempt per standalone exit"
    assert hostlog._writer[0] is None and hostlog._state["token"] is None
    # (the production start point, refused too: `node.run`'s eager start raises nothing)
    assert hostlog.start_writer() is False and len(calls) == 3
    assert hostlog._state["token"] is None


def test_r12_the_final_wait_does_not_wait_for_a_writer_that_cannot_start(monkeypatch):
    """R12 at the exit: lines are queued and the OS refuses the writer, so no handler of ours can be entered or stuck.
    The final wait returns True at once (the node exits normally, its traceback and `logging.shutdown()` kept) instead
    of spending its budget and taking the stalled `os._exit`; the queue is sealed; the queued lines are lost."""
    import time as _time
    from agent_comms import hostlog, session_host
    _no_writer()
    calls = _refuse_the_writer(monkeypatch)
    with hostlog.enqueue_only():
        session_host.log.warning("queued, never written")
    assert hostlog._queue, "the line is queued"
    t0 = _time.monotonic()
    assert hostlog.final_flush(2.0) is True
    assert _time.monotonic() - t0 < 0.5, "no wait for a writer that is not running"
    assert calls == [] and hostlog._state["sealed"], "and the final wait tried no start (R15)"


def test_r12_a_stop_and_a_save_complete_when_the_writer_cannot_start(henv, monkeypatch):
    """R12: with the writer refused by the OS, a cancel of a live steer attempt under the runner lock (which logs)
    still signals its process, and a shutdown whose SIGKILL step raises (which logs) still saves the state file;
    nothing raises; once a start succeeds, the writer says once how many lines the bound dropped, then the rest."""
    from agent_comms import hostlog, session_host
    from hostlog_guard import runner_lock
    from test_claude_steering import steered
    monkeypatch.setattr(hostlog, "ACK_SECONDS", 5.0)
    s, child, n = steered(henv, monkeypatch)
    _no_writer()
    monkeypatch.setattr(hostlog, "QUEUE_MAX", 3)
    calls = _refuse_the_writer(monkeypatch)
    lock, inner = runner_lock()
    with lock:
        henv.host.cancel(SID, "1", "cancelled")
    assert child.terminated, "the stop's signal went"
    child.exits_on_terminate, child.code = False, None      # (still going at shutdown: its SIGKILL is due)

    def bad():
        raise OSError("kill failed")
    child.kill = bad
    henv.host._state_path().unlink(missing_ok=True)
    henv.host.stop_all()
    assert henv.host._state_path().exists(), "the save went"
    for i in range(5):
        session_host.log.warning("queued while no writer runs %d", i)
    assert calls, "standalone exits at depth 0 tried to start it"
    monkeypatch.undo()
    monkeypatch.setattr(hostlog, "ACK_SECONDS", 5.0)
    with _Records() as rec:
        assert hostlog.start_writer(), "a start succeeds"
        session_host.log.warning("a start succeeds")
    said = rec.messages()
    drops = [m for m in said if m.endswith("deferred log line(s) were dropped "
                                           "(the queue was full, or a line could not be made a record)")]
    assert len(drops) == 1 and said[0] == drops[0] and said[-1] == "a start succeeds", said[:4]


def test_a4_no_enqueue_under_a_host_lock_or_on_a_lifecycle_thread_starts_the_writer(env, monkeypatch):
    """A4: with no writer running, a record logged under a host lock (the runner lock, the cutoff lock, the registry
    lock) or on a lifecycle thread never calls `Thread.start()`; a standalone caller at depth 0 does (the lazy start),
    and so does `start_writer` (production's one eager start)."""
    import threading
    from agent_comms import hostlog, session_host
    runner = env.node.sessions
    _no_writer()
    real, calls = threading.Thread.start, []

    def start(self):
        if self.name == hostlog.WRITER_NAME:
            calls.append("start")
        return real(self)
    monkeypatch.setattr(threading.Thread, "start", start)
    monkeypatch.setattr(hostlog, "ACK_SECONDS", 0.2)
    for lock in (runner.lock, runner.host._steer_stop_lock, runner.host.groups.lock):
        _no_writer()
        with lock:
            session_host.log.warning("under a host lock")
            assert calls == [], lock    # (no start while the lock is held ...)
        assert calls == ["start"], lock     # (... the lazy start is the standalone caller's, at depth 0)
        calls.clear()
    _no_writer()
    _on_watch_thread(lambda: session_host.log.warning("on a lifecycle thread"))
    done, err, _ = _production(lambda: session_host.log.warning("on a lifecycle thread"))
    assert done.wait(5) and not err and calls == []
    assert hostlog.start_writer() and calls == ["start"]


def test_a5_an_interrupt_during_an_enqueue_propagates_and_leaves_no_claim(monkeypatch):
    """A5: "no log call raises into the host" is for ordinary failures: an interrupt arriving inside the enqueue (here
    inside the writer's start) propagates out of the log call, and leaves the queue's lock free and no start
    claimed (its token cancelled); the next start succeeds."""
    import pytest as _pytest
    from agent_comms import hostlog, session_host
    _no_writer()
    _refuse_the_writer(monkeypatch, error=KeyboardInterrupt())
    with _pytest.raises(KeyboardInterrupt):
        session_host.log.warning("interrupted")
    assert hostlog._state["token"] is None
    assert hostlog._cond.acquire(blocking=False)
    hostlog._cond.release()
    monkeypatch.undo()
    assert hostlog.start_writer()


def test_a5_an_interrupt_inside_the_snapshot_propagates_and_leaves_nothing_held(monkeypatch):
    """A5, the enqueue proper (on a lifecycle thread, so no start is involved): a `SystemExit` arriving while the
    record is snapshotted propagates out of the log call; the queue's lock is free, no start is claimed, the depth is
    back to 0, nothing is queued, and the next line is written as ever."""
    import pytest as _pytest
    from agent_comms import hostlog, session_host

    def interrupted(*a, **k):
        raise SystemExit(7)
    monkeypatch.setattr(hostlog, "_caller", interrupted)
    with hostlog.enqueue_only():
        with _pytest.raises(SystemExit):
            session_host.log.warning("interrupted in the snapshot")
    assert hostlog._state["token"] is None and not hostlog._queue
    assert hostlog._local.depth == 0
    assert hostlog._cond.acquire(blocking=False)
    hostlog._cond.release()
    monkeypatch.undo()
    with _Records() as rec:
        session_host.log.warning("the next line")
    assert rec.messages() == ["the next line"]


# ---------------------------------------------------------------- R14, R15 (review round 2): the writer's start


def _list_handler():
    """A plain handler on the package logger (no flush on leaving): (records as (message, thread ident), remove)."""
    import threading
    got = []
    h = logging.Handler(logging.DEBUG)
    h.emit = lambda r: got.append((r.getMessage(), threading.get_ident()))
    lg = logging.getLogger("agent_comms")
    saved = lg.level
    lg.setLevel(logging.DEBUG)
    lg.addHandler(h)

    def remove():
        lg.removeHandler(h)
        lg.setLevel(saved)
    return got, remove


def _gate_the_worker(monkeypatch):
    """The writer's thread, once launched, waits at a gate before it runs the writer's code (so before it can
    register): (gate, the launched threads)."""
    import threading
    from agent_comms import hostlog
    real, gate, launched = hostlog._write_loop, threading.Event(), []

    def gated(*a):
        launched.append(threading.current_thread())
        gate.wait(10)
        return real(*a)
    monkeypatch.setattr(hostlog, "_write_loop", gated)
    return gate, launched


def _release_the_gate(gate, launched):
    """(A test's `finally`) Open the gate and JOIN, bounded, every gated worker that is not THE registered writer (a
    late worker leaves at once), so a failing test's teardown never races the fixture's reset."""
    from agent_comms import hostlog
    gate.set()
    for t in launched:
        if t is not hostlog._writer[0]:
            t.join(5)


def _assert_consumed(writer):
    """Under the queue's lock: `writer` is THE registered writer and its registration consumed its token (no start is
    left pending)."""
    from agent_comms import hostlog
    with hostlog._cond:
        registered, token = hostlog._writer[0], hostlog._state["token"]
    assert registered is writer and token is None, (registered, token)


def _interrupt_after_the_launch(monkeypatch, before=None):
    """`Thread.start()` for the writer's thread launches it as ever, and THEN an interrupt arrives (as a SIGTERM's
    `KeyboardInterrupt` at the call's return, before the starter's next statement); `before(thread)` runs first.
    Once only: later starts (the teardown's reset) are as ever."""
    import threading
    from agent_comms import hostlog
    real, once = threading.Thread.start, []

    def start(self):
        real(self)
        if self.name == hostlog.WRITER_NAME and not once:
            once.append(self)
            if before is not None:
                before(self)
            raise KeyboardInterrupt
    monkeypatch.setattr(threading.Thread, "start", start)


def _count_the_writers_made(monkeypatch, error=None):
    """Every construction of the writer's `Thread` is counted (and refused with `error`, if given): the list."""
    import threading
    from agent_comms import hostlog
    real, made = threading.Thread.__init__, []

    def init(self, *a, **k):
        if k.get("name") == hostlog.WRITER_NAME:
            made.append(threading.get_ident())
            if error is not None:
                raise error
        return real(self, *a, **k)
    monkeypatch.setattr(threading.Thread, "__init__", init)
    return made


def test_r14_an_interrupt_after_the_launch_cancels_the_start_and_the_late_worker_touches_nothing(monkeypatch):
    """R14 (A1, cancellation wins): the starter is interrupted after `Thread.start()` launched the worker and before
    the worker registered (it is held at a gate): the starter's cleanup cancels the start; the late worker then exits
    without touching the queue; no handler is entered; the final wait sees no worker that can dequeue (writer-less),
    at once."""
    import time as _time
    import pytest as _pytest
    from agent_comms import hostlog, session_host
    _no_writer()
    gate, launched = _gate_the_worker(monkeypatch)
    _interrupt_after_the_launch(monkeypatch)
    got, remove = _list_handler()
    try:
        with _pytest.raises(KeyboardInterrupt):
            hostlog.start_writer()
        assert _wait(lambda: len(launched) == 1), "the worker was launched"
        with hostlog.enqueue_only():
            session_host.log.warning("queued after the cancelled start")
        gate.set()
        assert _wait(lambda: not launched[0].is_alive()), "the late worker exits"
        assert got == [] and len(hostlog._queue) == 1, (got, len(hostlog._queue))
        assert hostlog._writer[0] is None
        t0 = _time.monotonic()
        assert hostlog.final_flush(2.0) is True and _time.monotonic() - t0 < 0.5
    finally:
        _release_the_gate(gate, launched)
        remove()


def test_r14_a_registration_the_worker_won_is_never_undone_by_the_starters_cleanup(monkeypatch):
    """R14 (A1, registration wins; the race pinned): the worker registers itself BEFORE the interrupt reaches its
    starter. The starter's cleanup leaves that registration alone: it is THE writer, exactly one thread ever enters a
    handler, a second start makes no thread, and the final wait sees it (a handler it is stuck in holds the exit:
    False, never "writer-less")."""
    import threading
    import pytest as _pytest
    from agent_comms import hostlog, session_host
    _no_writer()
    won = []
    _interrupt_after_the_launch(monkeypatch, before=lambda t: won.append(_wait(lambda: hostlog._writer[0] is t, 2)))
    release = threading.Event()
    got, remove = _list_handler()
    try:
        with _pytest.raises(KeyboardInterrupt):
            hostlog.start_writer()
        assert won == [True], "the worker registered itself before the interrupt"
        writer = hostlog._writer[0]
        assert writer is not None and writer.is_alive()
        _assert_consumed(writer)
        made = _count_the_writers_made(monkeypatch)
        assert hostlog.start_writer() is True and made == [], "a second start makes no thread"
        with hostlog.enqueue_only():
            for i in range(3):
                session_host.log.warning("line %d", i)
        assert _wait(lambda: len(got) == 3) and {t for _, t in got} == {writer.ident}

        def blocks(r):
            if r.getMessage() == "blocks":
                release.wait(10)
        h = logging.Handler(logging.DEBUG)
        h.emit = blocks
        logging.getLogger("agent_comms").addHandler(h)
        try:
            with hostlog.enqueue_only():
                session_host.log.warning("blocks")
            assert _wait(lambda: hostlog._state["busy"] is not None)
            assert hostlog.final_flush(0.3) is False, "a writer stuck in a handler holds the exit: never writer-less"
        finally:
            release.set()
            logging.getLogger("agent_comms").removeHandler(h)
    finally:
        release.set()
        remove()


def test_r14_a_worker_that_registers_after_the_seal_touches_nothing(monkeypatch):
    """R14 (A3, any state → sealed): a start is pending (its worker launched, held at a gate) when the final wait
    seals: the seal cancels the pending start, so the final wait is writer-less at once; the late worker then exits
    without dequeuing the line queued before the seal; no handler is entered."""
    import time as _time
    from agent_comms import hostlog, session_host
    _no_writer()
    gate, launched = _gate_the_worker(monkeypatch)
    got, remove = _list_handler()
    try:
        assert hostlog.start_writer() is True, "a start is pending"
        assert _wait(lambda: len(launched) == 1)
        with hostlog.enqueue_only():
            session_host.log.warning("queued before the seal")
        t0 = _time.monotonic()
        assert hostlog.final_flush(0.5) is True and _time.monotonic() - t0 < 0.3
        gate.set()
        assert _wait(lambda: not launched[0].is_alive()), "the late worker exits"
        assert got == [] and hostlog._writer[0] is None, got
    finally:
        _release_the_gate(gate, launched)
        remove()


def test_r14_concurrent_claimants_make_one_thread(monkeypatch):
    """A3 (not started → pending): with a start pending (its worker held at a gate), another start point returns
    without making a thread; once the worker registers, exactly one writer runs."""
    import threading
    from agent_comms import hostlog
    _no_writer()
    gate, launched = _gate_the_worker(monkeypatch)
    try:
        assert hostlog.start_writer() is True
        assert _wait(lambda: len(launched) == 1)
        made = _count_the_writers_made(monkeypatch)
        assert hostlog.start_writer() is True and made == []
        gate.set()
        assert _wait(lambda: hostlog._writer[0] is launched[0])
        _assert_consumed(launched[0])
        assert _wait(lambda: len([t for t in threading.enumerate() if t.name == hostlog.WRITER_NAME]) == 1), \
            "one writer (the one let go by `_no_writer` has left)"
    finally:
        _release_the_gate(gate, launched)


@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")    # (the death's own traceback)
def test_a3_a_registered_writer_that_dies_releases_every_waiter(monkeypatch):
    """A3 (registered → dead): the writer's own code fails outside a handler and the thread ends. It deregisters as it
    goes and wakes every waiter: a standalone caller waiting for its line returns at once, not after ACK_SECONDS; the
    process has no writer from then on (in production nothing restarts it: a documented logging loss)."""
    import collections
    import time as _time
    from agent_comms import hostlog, session_host
    monkeypatch.setattr(hostlog, "ACK_SECONDS", 5.0)
    assert hostlog.flush(5)
    _no_writer()        # (a FRESH writer, started and registered here: its registration is the one under test)
    assert hostlog.start_writer()
    assert _wait(lambda: hostlog._writer[0] is not None)
    writer = hostlog._writer[0]
    _assert_consumed(writer)

    class Dies(collections.deque):
        def popleft(self):
            raise RuntimeError("the writer's own code failed")
    monkeypatch.setattr(hostlog, "_queue", Dies())
    t0 = _time.monotonic()
    session_host.log.warning("the writer dies on this one")
    assert _time.monotonic() - t0 < 1.0, "the waiter was released by the death"
    assert _wait(lambda: not writer.is_alive()) and hostlog._writer[0] is None
    # (no start is left pending by the registration that the death undid: the final wait is writer-less at once)
    with hostlog._cond:
        token = hostlog._state["token"]
    assert token is None, token
    t0 = _time.monotonic()
    assert hostlog.final_flush(1.0) is True and _time.monotonic() - t0 < 0.3


def test_r15_the_final_wait_starts_nothing_and_returns_within_its_bound(monkeypatch):
    """R15 (A2): no writer runs, a line is queued, and nothing holds a start back (no cooldown): the final wait makes
    no `Thread`, and returns within its bound (writer-less: at once)."""
    import time as _time
    from agent_comms import hostlog, session_host
    _no_writer()
    with hostlog.enqueue_only():
        session_host.log.warning("queued with no writer")
    made = _count_the_writers_made(monkeypatch)
    t0 = _time.monotonic()
    ok = hostlog.final_flush(0.5)
    assert made == [], "the final wait started a writer"
    assert ok is True and _time.monotonic() - t0 < 0.5


def test_r15_no_start_after_the_seal(monkeypatch):
    """R15: after the seal every start claim is refused: the eager start point and a standalone caller's lazy start
    make no `Thread`."""
    from agent_comms import hostlog, session_host
    assert hostlog.final_flush(2.0) is True
    _no_writer()
    made = _count_the_writers_made(monkeypatch)
    assert not hostlog.start_writer()
    session_host.log.warning("a standalone line after the seal")
    assert made == []


def test_r15_a_thread_that_cannot_be_made_is_contained(monkeypatch):
    """R15: `threading.Thread(...)` raising on construction (an ordinary failure) is contained as a failed `start()`
    is: a standalone log call and the eager start raise nothing, no start is left pending, and the next start point
    tries again."""
    from agent_comms import hostlog, session_host
    _no_writer()
    made = _count_the_writers_made(monkeypatch, error=RuntimeError("can't make a thread"))
    session_host.log.warning("a standalone line")
    assert len(made) == 1
    assert hostlog.start_writer() is False and len(made) == 2
    assert hostlog._writer[0] is None and hostlog._state.get("token") is None


def test_r15_the_start_points_are_exactly_two():
    """R15 (A2), statically: the writer is started from exactly two places: `node.run`'s eager start (once, its first
    statement inside `enqueue_only`) and a standalone caller's `_lazy_start` (from `_acknowledge`). The tests' own
    `_reset` is the only other caller of `start_writer`. No pass retry, and the final wait starts nothing."""
    import ast
    from pathlib import Path
    names = {"start_writer", "_start_writer", "_lazy_start"}
    found = set()
    for path in sorted((Path(__file__).parents[1] / "agent_comms").glob("*.py")):
        tree = ast.parse(path.read_text())
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for node in ast.walk(fn):
                if isinstance(node, ast.Call):
                    f = node.func
                    name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", None)
                    if name in names:
                        found.add((path.name, fn.name, name))
    assert found == {
        ("node.py", "run", "start_writer"),             # the one production start
        ("hostlog.py", "start_writer", "_start_writer"),
        ("hostlog.py", "_acknowledge", "_lazy_start"),  # a standalone caller, at depth 0
        ("hostlog.py", "_lazy_start", "_start_writer"),
        ("hostlog.py", "_reset", "start_writer"),       # (the tests' reset)
    }, found


# ---------------------------------------------------------------- after the PR's automated review: mapping args


class _Probe(logging.Handler):
    """A real handler as the stdlib's own are written: `emit` formats, and a failure goes to `handleError` (recorded
    here instead of printed). Every record handed to it is in `written` or in `errors`."""

    def __init__(self):
        super().__init__(logging.DEBUG)
        self.written, self.errors = [], []

    def emit(self, record):
        try:
            self.written.append(self.format(record))
        except Exception:
            self.handleError(record)

    def handleError(self, record):
        import sys
        self.errors.append((record.msg, type(sys.exc_info()[1]).__name__))


def test_pr_a_single_key_mapping_line_is_written_with_its_value():
    """The PR's automated review: `log.info("one %(id)s", {"id": "abc"})` was silently dropped (the writer handed the
    mapping itself to `makeRecord`, whose `args[0]` raised KeyError inside our own containment). A one-key mapping, a
    one-key mapping whose key is the string "0", a two-key one, and a `later` value in a one-key mapping are each
    written with their values through a real handler."""
    import collections
    from agent_comms import hostlog, session_host
    probe = _Probe()
    lg = logging.getLogger("agent_comms")
    saved = lg.level
    lg.setLevel(logging.DEBUG)
    lg.addHandler(probe)
    try:
        session_host.log.info("one %(id)s", {"id": "abc"})
        session_host.log.info("zero %(0)s", {"0": "nought"})
        session_host.log.info("two %(a)s %(b)s", {"a": 1, "b": 2})
        session_host.log.info("later %(v)s", {"v": hostlog.later(str.upper, "rendered")})
        session_host.log.info("ordered %s", collections.OrderedDict(id=1))
        assert hostlog.flush(5)
    finally:
        lg.removeHandler(probe)
        lg.setLevel(saved)
    assert probe.written == ["one abc", "zero nought", "two 1 2", "later RENDERED", "ordered OrderedDict"], \
        (probe.written, probe.errors)
    assert probe.errors == []


def test_pr_a_mapping_that_cannot_be_formatted_fails_as_stock_logging_does():
    """The neighbouring cells: each record reaches the handler, and where it cannot be formatted it fails there the
    way stock `logging` fails for the same call (in `format`, so the handler's `handleError`), never vanishing inside
    our own code:
    - a mapping left EMPTY once its non-str keys are dropped at the call (`{0: "x"}`): handed over as `({},)`, which
      logging keeps as a tuple, so `%(id)s` fails in `format` with TypeError (stock, with the key kept, fails there
      with KeyError: the same path, `handleError`);
    - a mapping missing the template's key: KeyError, as stock;
    - an EMPTY mapping: stock keeps `({},)` as the args, so `%(id)s` of a tuple, TypeError, as stock;
    - a one-element tuple whose element is a dict SUBCLASS (an OrderedDict): reduced to its type name at the call (no
      code of the subclass runs), so `%(id)s` of a tuple, TypeError (stock would have used the mapping)."""
    import collections
    from agent_comms import hostlog, session_host
    probe = _Probe()
    lg = logging.getLogger("agent_comms")
    saved = lg.level
    lg.setLevel(logging.DEBUG)
    lg.addHandler(probe)
    try:
        session_host.log.info("intkey %(id)s", {0: "x"})
        session_host.log.info("missing %(id)s", {"other": 1})
        session_host.log.info("empty %(id)s", {})
        session_host.log.info("subclass %(id)s", collections.OrderedDict(id=1))
        assert hostlog.flush(5)
    finally:
        lg.removeHandler(probe)
        lg.setLevel(saved)
    assert probe.written == [], probe.written
    assert probe.errors == [("intkey %(id)s", "TypeError"), ("missing %(id)s", "KeyError"),
                            ("empty %(id)s", "TypeError"), ("subclass %(id)s", "TypeError")], probe.errors


def test_pr_a_record_that_cannot_be_made_is_counted_not_lost_silently():
    """The general case: a record that fails between the dequeue and the first handler (here `makeRecord` refusing an
    `extra` key that would overwrite a record attribute, which stock logging raises to the caller; and a logger filter
    that raises) is counted as dropped, and the writer says once how many, as for the queue bound's drops."""
    from agent_comms import hostlog, session_host
    probe = _Probe()
    lg = logging.getLogger("agent_comms")
    saved = lg.level
    lg.setLevel(logging.DEBUG)
    lg.addHandler(probe)

    class Raises(logging.Filter):
        def filter(self, record):
            if record.msg == "the filter raises":
                raise RuntimeError("a filter failed")
            return True
    flt = Raises()
    sh = logging.getLogger("agent_comms.session_host")
    sh.addFilter(flt)
    try:
        session_host.log.info("an extra that clashes", extra={"message": "overwrites"})
        session_host.log.info("the filter raises")
        assert hostlog.flush(5)
        session_host.log.info("the next line")
        assert hostlog.flush(5)
    finally:
        sh.removeFilter(flt)
        lg.removeHandler(probe)
        lg.setLevel(saved)
    notice = "1 deferred log line(s) were dropped (the queue was full, or a line could not be made a record)"
    assert probe.written[-3:] == [notice, notice, "the next line"], (probe.written, probe.errors)  # (a notice owed
    # is written before the next queued line: one per loss here)


# ---------------------------------------------------------------- R13: the real exit path, a real SIGTERM


_NODE_SCRIPT = r"""
import logging, sys, threading
from agent_comms import node
entered = threading.Event()

class Stuck(logging.Handler):
    def emit(self, record):
        if "hub unreachable" in record.getMessage():
            print("entered", flush=True)
            threading.Event().wait()        # blocks for good, holding the handler's own lock

logging.getLogger("agent_comms").addHandler(Stuck())
logging.getLogger("agent_comms").setLevel(logging.INFO)
node.run(sys.argv[1], "a-test-token", poll_seconds=0.1)
print("run returned", flush=True)
"""


def test_r13_a_real_sigterm_ends_the_node_within_the_bound_with_mains_status(tmp_path):
    """R13 (and A3): the test's OWN child process runs `node.run` against a local stub hub that answers the hello and
    fails every assignment fetch (500), so the loop says `hub unreachable` into a handler that blocks forever holding
    its lock; once that handler is entered the test sends the child a REAL SIGTERM: the child shuts down and ends
    within the bound with status 0, which is main's status for a SIGTERM'd node (`on_term` raises KeyboardInterrupt,
    `run` shuts down and returns, `board` exits 0). (On main the same child would never end: its loop is stuck in that
    handler.)"""
    import json
    import os
    import signal
    import subprocess
    import sys
    import threading
    import time as _time
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from pathlib import Path

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
    home = tmp_path / "home"
    home.mkdir()
    script = tmp_path / "node_child.py"
    script.write_text(_NODE_SCRIPT)
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).parents[1]), "AGENT_COMMS_HOME": str(home)}
    child = subprocess.Popen([sys.executable, str(script), f"http://127.0.0.1:{hub.server_address[1]}"],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env, cwd=str(tmp_path))
    import queue as _queue
    lines: _queue.Queue = _queue.Queue()

    def read():     # (the handshake is bounded: a reader thread, and a timed wait for its lines)
        for line in child.stdout:
            lines.put(line)
        lines.put(None)
    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    try:
        deadline, ready = _time.monotonic() + 30, False
        while not ready:
            try:
                line = lines.get(timeout=max(0.0, deadline - _time.monotonic()))
            except _queue.Empty:
                child.kill()                        # (through its own handle)
                child.wait(5)
                raise AssertionError("the child never entered the blocked handler within 30 s")
            assert line is not None, child.stderr.read()[-2000:]
            ready = "entered" in line
        t0 = _time.monotonic()
        child.send_signal(signal.SIGTERM)       # (the test's own child, and only it)
        status = child.wait(20)
        took = _time.monotonic() - t0
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(5)
        reader.join(5)          # (bounded: the child is gone, so its stdout is at EOF)
        child.stdout.close()
        child.stderr.close()
        hub.shutdown()
        hub.server_close()      # (the stub's socket, on every path)
    rest = []
    while not lines.empty():
        x = lines.get_nowait()
        if x is not None:
            rest.append(x)
    rest = "".join(rest)
    assert status == 0, (status, rest)
    assert took < 10, took
    assert "run returned" not in rest, "the stalled exit ends the process at the final wait"
