"""Enabling Claude steering (DESIGN_NOTES "Enabling Claude steering"; the enabling PR's rulings S1 to S5).

S1: the stop's CAUSE decides, in the one predicate (`SessionHost._reserves(s, cause)`). A stop main e6b421f would not
perform in the same state (the hold's deadline cut, the held turn's own timeout, a broken pipe or partial write while
writing STEER bytes, a cancel of held N after its native end) reserves the checkout without waiting: with the registry
busy nothing is committed and the next tick (or the hub's next listing of the cancel) retries. A stop main also performs
in that state (an explicit close, a fatal error, a host error, the reap of a dead leader with survivors, parking, a
cancel before N's native end, a broken pipe while writing an ordinary reply, a live attempt's own turn timeout) is
main's path, with main's registry wait. Each route runs here with the registry held by another thread and a sibling's
SIGKILL due: the sibling's KILL goes when main would send it.
S2: preparation has no side effects (on BUSY every field is as it was), and escalation is armed before any callback,
publication or diagnostic (a raising one leaves a group that still escalates to SIGKILL).
S3: a live group the registry does not know gets the normal STOPPING barrier, bound to that child."""
import threading

import pytest

from agent_comms import node_groups
from agent_comms.providers import Event, claude
from agent_comms.session_host import STEER_CUT, STEER_PARTIAL_SECONDS
from test_claude_correlation import V, idle_host, owned, uuid_of, vinit, emit
from test_claude_steering import S, TEXT, _claude_steering_on, ends, env, steered  # noqa: F401 (the autouse fixture: on)
from test_session_host import SID, SID2, henv, make_host, open_session  # noqa: F401

SIB = SID2


# ---------------------------------------------------------------- the contention harness


def _sibling_kill_due(henv):
    """A sibling session in the same host whose process ignores SIGTERM and outlives its SIGKILL, stopped by main's
    `_stop` (its grace runs from here), made without a pass of the host (A's state is left as its route made it):
    (session, child)."""
    from test_session_host import FakeSessionChild
    host = henv.host
    sb = open_session(henv, sid=SIB)
    cb = FakeSessionChild(4242, exits_on_terminate=False)
    cb.kill = lambda: setattr(cb, "killed", cb.killed + 1)
    token = ("session", SIB, "sibling")
    started, _ = host.groups.start(sb.cwd, token, lambda: cb)
    assert started
    sb.child, sb.group_token = cb, token
    host._stop(sb, henv.clock())
    return sb, cb


def _busy(host):
    """Another thread holds the group registry (a spawn across process creation): its release event."""
    release, held = threading.Event(), threading.Event()

    def hold():
        with host.groups.lock:
            held.set()
            release.wait(15)
    threading.Thread(target=hold, daemon=True).start()
    assert held.wait(3)
    return release


def _free(host):
    """Wait until the thread that held the registry has let it go."""
    import time
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        if host.groups.lock.acquire(blocking=False):
            host.groups.lock.release()
            return True
        time.sleep(0.01)
    return False


def _state(s):
    """Everything a stop's preparation must leave as it was (ruling S2): the buffer's bytes, the turn, the stop's
    clock, the slot, the hold, the adapter's attempt and retired set."""
    slot, held = s.steer, s.held
    return (bytes(s.outbuf), s.in_flight.id if s.in_flight else None, s.stopping_since,
            None if slot is None else (slot.mark, slot.sent, slot.outcome, slot.revoked, slot.fenced, slot.frame),
            None if held is None else (held.decided, held.fenced_at, held.s_turn),
            repr(s.provider._attempt), frozenset(s.provider._retired))


def _run(fn):
    out, err, done = [], [], threading.Event()

    def run():
        try:
            out.extend(fn() or [])
        except BaseException as e:      # noqa: BLE001 (the test reads it)
            err.append(e)
        finally:
            done.set()
    threading.Thread(target=run, daemon=True).start()
    return out, err, done


# ---------------------------------------------------------------- the routes


def _held(henv, monkeypatch):
    s, child, n = steered(henv, monkeypatch)
    s.steer.cover_until = henv.clock() + 10_000
    child.emit(owned(n, text="N's own"))
    henv.host.tick()
    assert s.held is not None
    return s, child


def r_hold_cut(henv, monkeypatch):
    s, child = _held(henv, monkeypatch)

    def arm():
        s.steer.cover_until = henv.clock() - 1      # (the steer's contact with the hub ran out)
        assert s.steer.contact_ran_out(henv.clock())    # (its expiry latches, for good: the cause, not a preparation)
    return s, child, arm, lambda: henv.host.tick(), lambda: henv.host.tick()


def r_held_timeout(henv, monkeypatch):
    s, child = _held(henv, monkeypatch)

    def arm():
        henv.clock.advance(henv.host.timeout_minutes * 60 + 1)
    return s, child, arm, lambda: henv.host.tick(), lambda: henv.host.tick()


def _attempt(henv, monkeypatch):
    """A verified session, N in flight, S offered and authorized, nothing of it written yet: (session, child)."""
    monkeypatch.setattr(claude, "VERIFIED", frozenset({V}))
    henv.host = make_host(henv, timeout_minutes=1)
    henv.host.steer_authority = lambda sess: True
    s, child = idle_host(henv)
    henv.host.send(SID, "a long turn", message_id="1")
    henv.host.tick()
    n = uuid_of(child)
    emit(henv, child, vinit(n))
    henv.host.steer_offer(SID, "1", S, TEXT, authorized=True, cover_until=henv.clock() + 10_000)
    return s, child


def r_partial_write(henv, monkeypatch):
    s, child = _attempt(henv, monkeypatch)
    child.accept = 7
    henv.host.tick()
    child.accept = 0        # (the pipe is full now: the frame stays taken in part)
    assert s.steer is not None and 0 < s.steer.sent < s.steer.length

    def arm():
        henv.clock.advance(STEER_PARTIAL_SECONDS + 1)
    return s, child, arm, lambda: henv.host.tick(), lambda: henv.host.tick()


def r_steer_pipe(henv, monkeypatch):
    s, child = _attempt(henv, monkeypatch)

    def arm():
        child.accept = 0
        henv.host.tick()        # (S's frame queued, not a byte of it taken yet)
        assert s.steer.mark is not None and s.steer.sent == 0
        child.accept = None
        child.broken = True     # (the process no longer reads: the steer frame's write fails)
    return s, child, arm, lambda: henv.host.tick(), lambda: henv.host.tick()


def r_cancel_held(henv, monkeypatch):
    s, child = _held(henv, monkeypatch)
    # (the hub lists the cancel again on each listing until the node reports it: the retry is the next listing's)
    return (s, child, lambda: None, lambda: henv.host.cancel(SID, "1", "cancelled"),
            lambda: henv.host.cancel(SID, "1", "cancelled"))


RESERVED = [r_hold_cut, r_held_timeout, r_partial_write, r_steer_pipe, r_cancel_held]


def m_close_live(henv, monkeypatch):
    s, child, n = steered(henv, monkeypatch)
    s.steer.cover_until = henv.clock() + 10_000
    return s, child, lambda: None, lambda: henv.host.close(SID)


def m_close_held(henv, monkeypatch):
    s, child = _held(henv, monkeypatch)
    return s, child, lambda: None, lambda: henv.host.close(SID)


def m_fatal(henv, monkeypatch):
    s, child, n = steered(henv, monkeypatch)
    s.steer.cover_until = henv.clock() + 10_000
    real = s.provider.feed

    def arm():
        s.provider.feed = lambda line: (([Event("fatal", detail="the provider gave up")], [])
                                        if line == b"FATAL" else real(line))
        child.emit(b"FATAL")
    return s, child, arm, lambda: henv.host.tick()


def m_host_error(henv, monkeypatch):
    s, child, n = steered(henv, monkeypatch)
    s.steer.cover_until = henv.clock() + 10_000

    def arm():
        def broken():
            raise RuntimeError("a read failed")
        child.read_lines = broken
    return s, child, arm, lambda: henv.host.tick()


def m_reap_survivors(henv, monkeypatch):
    s, child, n = steered(henv, monkeypatch)
    s.steer.cover_until = henv.clock() + 10_000

    def arm():
        child.code = 0
        henv.groups.add(child.pid)      # (its leader went; members of its group are left)
    return s, child, arm, lambda: henv.host.tick()


def m_park(henv, monkeypatch):
    monkeypatch.setattr(claude, "VERIFIED", frozenset({V}))
    henv.host = make_host(henv, timeout_minutes=1)
    s, child = idle_host(henv)
    return s, child, lambda: None, lambda: [henv.host.park(SID)] and []


def m_cancel_live(henv, monkeypatch):
    s, child, n = steered(henv, monkeypatch)
    s.steer.cover_until = henv.clock() + 10_000
    return s, child, lambda: None, lambda: henv.host.cancel(SID, "1", "cancelled")


def m_reply_pipe(henv, monkeypatch):
    s, child, n = steered(henv, monkeypatch)
    s.steer.cover_until = henv.clock() + 10_000

    def arm():
        s.outbuf += b'{"type":"control_response"}\n'    # (an ordinary reply to the process, no steer byte)
        child.broken = True
    return s, child, arm, lambda: henv.host.tick()


def m_live_timeout(henv, monkeypatch):
    s, child, n = steered(henv, monkeypatch)
    s.steer.cover_until = henv.clock() + 10_000

    def arm():
        henv.clock.advance(henv.host.timeout_minutes * 60 + 1)      # (N's own turn timeout, before its native end)
    return s, child, arm, lambda: henv.host.tick()


MAINS = [m_close_live, m_close_held, m_fatal, m_host_error, m_reap_survivors, m_park, m_cancel_live, m_reply_pipe,
         m_live_timeout]


# ---------------------------------------------------------------- S1: one test per route under contention


@pytest.mark.parametrize("route", RESERVED, ids=lambda f: f.__name__)
def test_s1_a_steering_only_stop_never_waits_and_the_siblings_kill_goes(henv, monkeypatch, route):
    """Ruling S1 (reserved routes) and S2 (preparation has no side effects): with the registry busy and a sibling's
    SIGKILL due, the pass does not wait; the sibling's KILL goes in it, as main sends it; nothing of A is committed or
    changed; once the registry frees, the retry commits the stop once."""
    s, child, arm, act, retry = route(henv, monkeypatch)
    child.exits_on_terminate = False
    sb, cb = _sibling_kill_due(henv)
    arm()
    henv.clock.advance(henv.host.kill_grace + 1)
    before = _state(s)
    release = _busy(henv.host)
    try:
        out, err, done = _run(lambda: act() + henv.host.tick())
        assert done.wait(3), "a steering-only stop never waits on the registry"
        assert not err, err
        assert cb.killed, "the sibling's SIGKILL, due, went in that pass"
        assert not child.terminated and s.stopping_since is None, "nothing committed while the registry is busy"
        assert _state(s) == before, "preparation changed nothing"
        assert not ends(out), "N not settled"
    finally:
        release.set()
    assert _free(henv.host)
    out = retry()
    assert child.terminated == 1 and s.stopping_since is not None, "the retry committed the stop, once"
    out += henv.host.tick()
    assert child.terminated == 1


@pytest.mark.parametrize("route", MAINS, ids=lambda f: f.__name__)
def test_s1_a_stop_main_also_performs_is_mains_with_its_registry_wait(henv, monkeypatch, route):
    """Ruling S1 (main's routes): main's path, unchanged, with main's registry wait (inherited, for the hardening PR):
    with the registry busy the stop waits for it, as on main; once it frees the stop is committed in that same call,
    and the sibling's SIGKILL goes when main would send it (the pass after)."""
    s, child, arm, act = route(henv, monkeypatch)
    child.exits_on_terminate = False
    sb, cb = _sibling_kill_due(henv)
    arm()
    henv.clock.advance(henv.host.kill_grace + 1)
    release = _busy(henv.host)
    try:
        out, err, done = _run(act)
        assert not done.wait(0.5), "main's stop waits on the registry, as on main"
    finally:
        release.set()
    assert done.wait(5) and not err, err
    assert s.stopping_since is not None, "committed once the registry let go"
    henv.host.tick()
    assert cb.killed, "the sibling's SIGKILL goes when main would send it"


# ---------------------------------------------------------------- S2: escalation is armed before any callback


@pytest.mark.parametrize("route", RESERVED, ids=lambda f: f.__name__)
def test_s2_a_raising_commit_after_the_reservation_still_escalates(henv, monkeypatch, route):
    """Ruling S2: after the reservation, escalation is armed FIRST (the kill clock and the SIGTERM); a callback,
    publication or diagnostic that raises after it leaves a group that still escalates to SIGKILL after the grace."""
    s, child, arm, act, retry = route(henv, monkeypatch)
    child.exits_on_terminate = False
    arm()

    def bad(*a, **k):
        raise RuntimeError("the commit failed")
    monkeypatch.setattr(henv.host, "_fail", bad)
    try:
        act()
    except RuntimeError:
        pass
    assert s.stopping_since is not None and child.terminated == 1, "escalation armed before the commit"
    henv.clock.advance(henv.host.kill_grace + 1)
    henv.host.kill_due()
    assert child.killed, "the group still escalates to SIGKILL"


# ---------------------------------------------------------------- S3: a live group the registry does not know


def _cut_with_the_entry_missing(henv, monkeypatch, token):
    s, child = _held(henv, monkeypatch)
    child.exits_on_terminate = False
    henv.host.groups.gone(s.group_token)        # (the registry has no entry for this live group)
    if token == "none":
        s.group_token = None
    s.steer.cover_until = henv.clock() - 1
    henv.host.tick()
    return s, child


@pytest.mark.parametrize("token", ["kept", "none"])
def test_s3_a_live_group_missing_from_the_registry_gets_a_barrier(henv, monkeypatch, token):
    """Ruling S3: MISSING with the probe alive, or a live child with `group_token is None`: the normal STOPPING entry
    is inserted under the checkout's key, bound to that child; admission of a run on that checkout is refused while it
    stands; the entry goes at the verified reap."""
    s, child = _cut_with_the_entry_missing(henv, monkeypatch, token)
    assert child.terminated == 1 and s.group_token is not None
    assert henv.host.groups.blocked(s.cwd), "the barrier stands"
    started, _ = henv.host.groups.start(s.cwd, ("run", 1), lambda: object())
    assert not started, "nothing is admitted on that checkout while it stands"
    child.code = -15
    henv.host.tick()
    assert s.child is None and not henv.host.groups.blocked(s.cwd), "gone at the verified reap"


def test_s3_a_lost_claim_leaves_no_entry(henv, monkeypatch):
    """Ruling S3: the claim is lost (the latch came) with the entry missing: no entry is made."""
    s, child = _held(henv, monkeypatch)
    henv.host.groups.gone(s.group_token)
    s.steer.cover_until = henv.clock() - 1
    henv.host.steer_drain_hook = lambda sess: henv.host.steer_stopping()
    henv.host._hold_cut(s, henv.clock(), henv.host._outbox)
    assert not henv.host.groups.entries and not child.terminated


def test_s3_a_verified_exit_needs_no_entry(henv, monkeypatch):
    """Ruling S3: the entry is missing and the probe says the group is gone: no barrier, no SIGTERM."""
    s, child = _held(henv, monkeypatch)
    henv.host.groups.gone(s.group_token)
    child.code = 0
    s.steer.cover_until = henv.clock() - 1
    henv.host._hold_cut(s, henv.clock(), henv.host._outbox, drain=False)
    assert not henv.host.groups.entries and not child.terminated


def test_s3_try_stopping_inserts_the_barrier_only_on_a_miss():
    """Ruling S3 (the registry): with a barrier given, a miss inserts the normal STOPPING entry under that key (it
    reports RESERVED: this call made it); an entry that exists is reserved as before; BUSY records nothing."""
    g = node_groups.Groups()
    k = node_groups.key("/w")
    assert g.try_stopping("t1", (k, "/w")) == g.RESERVED
    assert g.entries == {k: {"t1": [g.STOPPING, "/w"]}} and g.blocked("/w")
    g.gone("t1")
    assert g.try_stopping("t1") == g.MISSING and not g.entries


# ---------------------------------------------------------------- S5 (c): the late lane follows the reconciliation phase


def _watched_terminal_then_a_listing(env, close_first):
    """Two Claude roots; the one listed first is idle, the other has its turn in flight. The watchdog's self-stop
    fails that turn into its own lane (`watched`); then a listing comes, in which the first is closed (or not): the
    record of the second's turn afterwards."""
    from agent_comms import hosted
    from test_claude_steering import cinit, cresult
    from test_hosted_sessions import P2, start, tick, written
    from test_hosted_tree import proc_of
    runner = env.node.sessions
    x = start(env, "one")["session_id"]
    tick(env)
    y = start(env, "two", project=P2)["session_id"]
    tick(env)
    first, second = sorted([x, y])      # (the hub lists sessions by id: the first is reconciled first)
    cf = proc_of(env, first)[1]
    nf = written(cf)[-1]["uuid"]
    cf.emit(cinit(first, nf), cresult(first, nf, text="done"))
    tick(env, times=2)
    assert hosted._get(env.board.conn, hosted._msg_key(first, 1))["state"] == "done"
    with runner.lock:
        runner.host.stop_turns(runner._self_stop_detail(), own=True, into=runner.watched)
    assert [e for e in runner.watched if e.session_id == second and e.kind == "turn_ended"]
    if close_first:
        assert env.human.post(f"/api/hosted-sessions/{first}/close").status_code == 200
    tick(env, times=3)
    return hosted._get(env.board.conn, hosted._msg_key(second, 1))


@pytest.mark.parametrize("close_first", [False, True])
def test_s5c_a_handoff_inside_reconciliation_keeps_lost_ahead_of_a_watched_terminal(env, close_first):
    """Ruling S5(c): the watchdog's own lane (`watched`) is handed off only after the listing is reconciled, as on
    main, whichever host method returned events meanwhile: a close made inside the reconciliation (returning the
    whole outbox) must not put a watched terminal ahead of the LOST the reconciliation reports. The second turn's
    record is the same with and without the first session's close."""
    from agent_comms.node_sessions import LOST
    m = _watched_terminal_then_a_listing(env, close_first)
    assert m["state"] == "failed" and m["detail"].startswith(LOST), m


# ---------------------------------------------------------------- S5 (a): the 400 fallback, a difference from main


def test_s5a_a_report_whose_steer_extension_gets_a_400_is_sent_again_at_once_without_it(env):
    """Ruling S5(a), kept and pinned (a difference from main e6b421f, which retries a refused steer report only on a
    422): a report carrying a steer extension that the hub refuses with a 400 is sent again at once, in the same
    call, without the extension; the hub's acknowledgement of that payload is the report's."""
    import httpx
    runner = env.node.sessions
    sent = []

    def call(method, path, json=None, gen=None, spare=False, **kw):
        sent.append(json)
        if any("steer" in e for e in json["events"]):
            req = httpx.Request(method, "http://hub" + path)
            raise httpx.HTTPStatusError("refused", request=req, response=httpx.Response(400, request=req))
        return {"ok": True}
    runner._call = call
    body = {"events": [{"kind": "turn_ended", "message": 1, "ok": True, "delivered": True, "detail": "",
                        "steer": {"outcome": "in_turn", "why": "answered"}}], "state": "idle"}
    _, answer, payload = runner._post_report("5b0c1f2e-7a6d-4e3b-9c8a-1d2e3f4a5b6c", body, [object()])
    assert len(sent) == 2, "sent again at once, in the same call"
    assert "steer" in sent[0]["events"][0] and "steer" not in sent[1]["events"][0]
    assert payload == sent[1] and answer == {"ok": True}
