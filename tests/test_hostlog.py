"""Host-side logging (DESIGN_NOTES "Host-side logging"): the package-owned adapter (`agent_comms.hostlog`).

THE invariant: no log handler runs, and nothing is formatted, while the calling thread holds a host lock (the runner
lock, the cutoff lock, the registry lock), on any thread, for any host-side line; nothing process-wide is patched.
Every line is a deferred record made at the call and written by one drain on the caller's thread at the outermost
boundary, after every host lock is let go. A watchdog thread never drains."""
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
