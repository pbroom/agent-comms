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


def r_partial_write_unverified(henv, monkeypatch):
    """(After #119, A2) The partial frame of an attempt whose adapter then lost its verification (it no longer
    advertises `holds`): the gate closes and the frame is cut; the attempt's recorded Claude identity (`slot.holds`)
    still takes the reserved path (round 10, H2, as behaviour, not only the predicate)."""
    s, child = _attempt(henv, monkeypatch)
    child.accept = 7
    henv.host.tick()
    child.accept = 0
    assert s.steer is not None and 0 < s.steer.sent < s.steer.length

    def arm():
        s.provider._verified = False        # (verification lost: the adapter no longer says `holds`)
        assert not s.provider.holds and s.steer.holds
    return s, child, arm, lambda: henv.host.tick(), lambda: henv.host.tick()


RESERVED = [r_hold_cut, r_held_timeout, r_partial_write, r_steer_pipe, r_cancel_held, r_partial_write_unverified]


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
    with the registry busy the stop ENTERS main's wait (`Groups.stopping`, its positive signal: review round 1, R5)
    and waits there, as on main; once it frees the stop is committed in that same call, and the sibling's SIGKILL goes
    when main would send it (the pass after)."""
    s, child, arm, act = route(henv, monkeypatch)
    child.exits_on_terminate = False
    sb, cb = _sibling_kill_due(henv)
    arm()
    henv.clock.advance(henv.host.kill_grace + 1)
    entered = threading.Event()
    real_stopping = henv.host.groups.stopping

    def stopping(token):
        if token == s.group_token:
            entered.set()       # (main's `_stop` reached main's registry call for A's group)
        return real_stopping(token)
    henv.host.groups.stopping = stopping
    release = _busy(henv.host)
    try:
        out, err, done = _run(act)
        assert entered.wait(3), "the stop entered main's registry wait"
        assert not done.wait(0.3), "and waits there, as on main"
        assert not cb.killed, "the sibling's SIGKILL is not sent from inside that wait, as on main"
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


# ---------------------------------------------------------------- enabling PR, review round 1


CLAIMED = [m_close_live, m_fatal, m_host_error, m_reap_survivors, m_cancel_live, m_reply_pipe, m_live_timeout]


class _Sink:
    """A log handler on the package's logger that blocks (until released) or raises on a steer diagnostic, and says
    what it saw when it was entered."""

    def __init__(self, s, child, mode):
        import logging
        self.s, self.child, self.mode = s, child, mode
        self.entered, self.release, self.seen = threading.Event(), threading.Event(), []
        self.h = logging.Handler(logging.DEBUG)
        self.h.emit = self.emit
        self.logger = logging.getLogger("agent_comms")

    def emit(self, rec):
        if "the steer into message" not in rec.getMessage():
            return
        self.seen.append((self.child.terminated, self.s.stopping_since))
        self.entered.set()
        if self.mode == "raises":
            raise RuntimeError("a log handler failed")
        self.release.wait(10)

    def __enter__(self):
        self.level = self.logger.level
        self.logger.setLevel(10)
        self.logger.addHandler(self.h)
        return self

    def __exit__(self, *a):
        self.release.set()
        self.logger.removeHandler(self.h)
        self.logger.setLevel(self.level)


@pytest.mark.parametrize("mode", ["blocks", "raises"])
@pytest.mark.parametrize("route", CLAIMED, ids=lambda f: f.__name__)
def test_r1_a_claimed_main_route_signals_before_any_handler_and_writes_its_lines_at_the_flush(henv, monkeypatch,
                                                                                                route, mode):
    """Review round 1, R1 and A2: on a claimed main route the claim makes the decision (memory only), the route's own
    main stop runs next, and the claim's lines go to the deferred queue: no log handler is entered by the call
    (under the runner lock); the group is signalled and its kill clock armed before any is; the lines are written at
    the loop's flush (`flush_watch_logs`), a handler that blocks or raises there changing nothing of the stop. A
    sibling's due SIGKILL goes from another thread meanwhile (the host takes no lock for it); under the node the
    watchdog's kill waits for the runner lock as for any loop work, which no claim line now holds."""
    from agent_comms import session_host
    s, child, arm, act = route(henv, monkeypatch)
    child.exits_on_terminate = False
    sb, cb = _sibling_kill_due(henv)
    arm()
    henv.clock.advance(henv.host.kill_grace + 1)
    with _Sink(s, child, mode) as sink:
        out, err, done = _run(act)
        assert done.wait(5), "the call returned: no handler held it"
        assert not sink.entered.is_set(), "no log handler entered by the call"
        assert not err, err
        assert child.terminated >= 1 and s.stopping_since is not None, "signalled, its kill clock armed"
        henv.host.kill_due()
        assert cb.killed, "the sibling's due SIGKILL"
        flushed, ferr, fdone = _run(lambda: session_host.flush_watch_logs())
        assert sink.entered.wait(3), "the line, at the loop's flush"
        assert sink.seen[0][0] >= 1 and sink.seen[0][1] is not None, "entered only after the signal and the clock"
        sink.release.set()
        assert fdone.wait(5) and not ferr, ferr


def test_r1_a1_a_decision_that_raises_after_the_latch_still_reaches_the_stop(henv, monkeypatch):
    """Review round 1, A1: once the claim is made (the latch checked), the route's stop runs whatever the decision
    raises; the group is signalled, its kill clock armed, and the decision's exception (the first) propagates."""
    s, child, n = steered(henv, monkeypatch)
    s.steer.cover_until = henv.clock() + 10_000
    child.exits_on_terminate = False

    def bad(*a, **k):
        raise RuntimeError("the decision failed")
    monkeypatch.setattr(henv.host, "_fail", bad)
    with pytest.raises(RuntimeError, match="the decision failed"):
        henv.host.cancel(SID, "1", "cancelled")
    assert child.terminated == 1 and s.stopping_since is not None


def test_r1_a1_the_first_exception_propagates_when_the_stop_raises_too(henv, monkeypatch):
    """Review round 1, A1: the decision raises and the stop raises too: the decision's exception is the one that
    propagates (the stop's never replaces it), and the kill clock is armed (main's `_stop`: a raising SIGTERM keeps
    it)."""
    s, child, n = steered(henv, monkeypatch)
    s.steer.cover_until = henv.clock() + 10_000

    def bad(*a, **k):
        raise RuntimeError("the decision failed")

    def term():
        raise OSError("the signal failed")
    monkeypatch.setattr(henv.host, "_fail", bad)
    child.terminate = term
    with pytest.raises(RuntimeError, match="the decision failed"):
        henv.host.cancel(SID, "1", "cancelled")
    assert s.stopping_since is not None


def test_r5_a_reap_after_the_latch_registers_its_survivors_and_signals_nothing(henv, monkeypatch):
    """Review round 1, R5 (and R4's row): the leader went, members of its group are left, and the node's stop latch is
    set: the reap registers the group STOPPING (so nothing starts beside it) and parks the session; no SIGTERM, no
    kill clock: the bulk stop sends both."""
    from agent_comms.node_groups import Groups
    s, child, n = steered(henv, monkeypatch)
    henv.host.steer_stopping()
    child.code = 0
    henv.groups.add(child.pid)
    henv.host.tick()
    states = [st for g in henv.host.groups.entries.values() for tok, (st, cwd) in g.items() if tok == s.group_token]
    assert states == [Groups.STOPPING] and s.parked
    assert not child.terminated and s.stopping_since is None


# ---------------------------------------------------------------- R2 / A3: the reserved path makes no filesystem call


class _NoFilesystem:
    """`os.stat`, `os.path.realpath` and `node_groups.key` raise (and are recorded) if called inside."""

    def __init__(self, monkeypatch):
        self.mp, self.calls = monkeypatch, []

    def __enter__(self):
        import os

        def trap(name, real):
            def f(*a, **k):
                self.calls.append(name)
                raise AssertionError(f"{name} called on the reserved path")
            return f
        self.saved = [(os, "stat", os.stat), (os.path, "realpath", os.path.realpath),
                      (node_groups, "key", node_groups.key)]
        for obj, name, real in self.saved:
            setattr(obj, name, trap(name, real))
        return self

    def __exit__(self, *a):
        for obj, name, real in self.saved:
            setattr(obj, name, real)


@pytest.mark.parametrize("branch", ["token", "missing", "no_token"])
def test_r2_a_reserved_watchdog_cut_makes_no_filesystem_call(henv, monkeypatch, branch):
    """Review round 1, R2 and A3: an expired held N cut by the watchdog's boundary (`steer_deadlines`): with a valid
    registry token, with its entry missing (the probe alive), and with a live child that has no token: no `os.stat`,
    no `realpath`, no `node_groups.key`; the stop completes; a barrier inserted on a miss is under the very key the
    group was registered under, so admission on that checkout is refused."""
    s, child = _held(henv, monkeypatch)
    child.exits_on_terminate = False
    registered = node_groups.key(s.cwd)
    assert registered in henv.host.groups.entries
    if branch != "token":
        henv.host.groups.gone(s.group_token)
    if branch == "no_token":
        s.group_token = None
    s.steer.cover_until = henv.clock() - 1
    with _NoFilesystem(monkeypatch) as fs:
        henv.host.steer_deadlines()
    assert fs.calls == [], fs.calls
    assert child.terminated == 1 and s.stopping_since is not None, "the stop completed"
    assert list(henv.host.groups.entries) == [registered], "the barrier is under the key the group was registered under"
    started, _ = henv.host.groups.start(s.cwd, ("run", 1), lambda: object())
    assert not started, "admission on that checkout is refused"


def test_a3_the_key_is_cached_at_every_attach_and_cleared_at_detach(henv, monkeypatch):
    """Review round 1, A3: the production attach point is `_spawn` (a respawn on resume included): after it the
    session's cached key is set and equals the registry's key for its token; after the detach (the verified reap)
    it is cleared, with the token."""
    s, child, n = steered(henv, monkeypatch)

    def registry_key(token):
        return [k for k, g in henv.host.groups.entries.items() if token in g]
    assert s.group_key is not None and registry_key(s.group_token) == [s.group_key]
    child.code = 0
    henv.host.tick()
    assert s.child is None and s.group_token is None and s.group_key is None, "cleared at the detach"
    henv.host.send(SID, "again", message_id="2")
    henv.host.tick()
    assert s.child is not None and s.child is not child, "respawned (resume)"
    assert s.group_key is not None and registry_key(s.group_token) == [s.group_key]


def test_a3_a_live_child_with_no_key_takes_mains_path_without_a_filesystem_call(henv, monkeypatch):
    """Review round 1, A3: a hand-made live child with no cached key (an invariant violation, unreachable in
    production) is decided in the one predicate: not reserved; main's path (claimed, owned, main's registry call);
    no `os.stat` or `realpath` from the watchdog's boundary; the checkout keeps its STOPPING barrier."""
    from agent_comms.node_groups import Groups
    s, child = _held(henv, monkeypatch)
    child.exits_on_terminate = False
    s.group_key = None
    assert not henv.host._reserves(s, "hold_cut")
    s.steer.cover_until = henv.clock() - 1
    with _NoFilesystem(monkeypatch) as fs:
        henv.host.steer_deadlines()
    assert fs.calls == [], fs.calls
    assert child.terminated == 1 and s.stopping_since is not None
    states = [st for g in henv.host.groups.entries.values() for tok, (st, cwd) in g.items() if tok == s.group_token]
    assert states == [Groups.STOPPING], "never released without a barrier"


def test_a2_under_the_node_a_claims_lines_are_written_outside_the_runner_lock(env):
    """Review round 1, A2: under the node, a claimed main route's lines (N's timeout with a live attempt, before its
    native end) are written by the loop's flush at the start of a pass, BEFORE it takes the runner lock
    (node_sessions `pass_once`); a node's host writes none at its tick (`keep_outbox`): no handler is entered while
    the runner lock is held, and the group was signalled before."""
    import logging
    from test_claude_steering import claude_steered
    a, ca, n, s_uuid = claude_steered(env)
    runner = env.node.sessions
    host = runner.host
    sa = host.sessions[a]
    seen = []

    class H(logging.Handler):
        def emit(self, rec):
            if "the steer into message" in rec.getMessage():
                seen.append((runner.lock.locked(), ca.terminated))
    h, lg = H(logging.DEBUG), logging.getLogger("agent_comms")
    level = lg.level
    lg.addHandler(h)
    lg.setLevel(logging.DEBUG)
    try:
        env.clock.advance(host.timeout_minutes * 60 + 1)
        runner._renew_contact(env.clock())
        runner.pass_once()
        assert ca.terminated >= 1 and sa.in_flight is None, "the timeout failed N and stopped its process"
        runner.pass_once()
        assert seen, "the line, at the loop's flush"
        assert all(not locked for locked, _ in seen), "never written under the runner lock"
        assert all(term >= 1 for _, term in seen), "only after the signal"
    finally:
        lg.removeHandler(h)
        lg.setLevel(level)


@pytest.mark.parametrize("mode", ["blocks", "raises"])
@pytest.mark.parametrize("route", [r_partial_write, r_steer_pipe], ids=lambda f: f.__name__)
def test_a2_a_reserved_stops_lines_go_to_the_deferred_queue_too(henv, monkeypatch, route, mode):
    """Review round 1, A2 (the same order for the reserved stop): its commit's lines (N's steer outcome) are handed to
    the deferred queue; no log handler is entered by the call; the group is signalled first; the loop's flush writes
    them."""
    from agent_comms import session_host
    s, child, arm, act, retry = route(henv, monkeypatch)
    child.exits_on_terminate = False
    arm()
    with _Sink(s, child, mode) as sink:
        out, err, done = _run(act)
        assert done.wait(5) and not err, err
        assert not sink.entered.is_set(), "no log handler entered by the call"
        assert child.terminated == 1 and s.stopping_since is not None
        flushed, ferr, fdone = _run(lambda: session_host.flush_watch_logs())
        assert sink.entered.wait(3), "the line, at the loop's flush"
        sink.release.set()
        assert fdone.wait(5) and not ferr, ferr
