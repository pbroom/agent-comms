"""Ownership until verified exit (C1; DESIGN_NOTES "Ownership until verified exit (C1)").

The invariant: no checkout or tracking record is released while the existing group may still have a live member. ONE
release rule (`Groups.release`), the only way a registry entry and its owner's tracking record (whose durable entry
is its projection) go: the session reap, `SessionHost.stop_all`, the dispatched run's reap and `report_stopped`. The
proof without an anchor: the leader's exit observed by its own handle AND a complete, successful group-membership
listing with no member (`node_groups.group_absent`). Anything else retains. C1 adds no signal, no thread and no
restart behaviour."""
import json

import pytest

from agent_comms import node_groups
from test_nodes import nenv  # noqa: F401 (the node with its fake processes)
from test_claude_steering import env  # noqa: F401 (the node-backed env)
from test_session_host import SID, SID2, henv, open_session  # noqa: F401


def _state(host):
    return json.loads(host._state_path().read_text())


def _entry(groups, token):
    return [(k, st) for k, g in groups.entries.items() for tok, (st, cwd) in g.items() if tok == token]


def _a_turn(henv, sid=SID):
    s = open_session(henv, sid=sid)
    henv.host.send(sid, "a long turn", message_id="1")
    henv.host.tick()
    child = henv.spawner.children[-1]
    assert s.child is child
    return s, child


def _survives_its_kill(henv, child):
    """The process ignores SIGTERM; its SIGKILL ends the leader but a member of its group survives."""
    child.exits_on_terminate = False
    henv.groups.add(child.pid)         # (a member of its group is left, whatever the leader does)


# ---------------------------------------------------------------- reproduction: shutdown and report_stopped


def test_c1_shutdown_keeps_a_group_that_survived_its_sigkill(henv):
    """Reproduction (e6ebf95: `_stop_all` let the group go once its SIGKILL was sent): the leader's SIGKILL returned
    with a member alive: the session keeps its process handle, the exact token and its STOPPING entry; the state file
    keeps its entry; a new process is refused on that checkout. Shutdown returned within its bound."""
    s, child = _a_turn(henv)
    _survives_its_kill(henv, child)
    token = s.group_token
    t0 = henv.clock()
    henv.host.stop_all()
    assert henv.clock() - t0 <= henv.host.kill_grace + 1, "within its existing bound"
    assert child.killed, "its SIGKILL was sent"
    assert s.child is child and s.group_token == token, "the handle and the exact token are kept"
    assert _entry(henv.host.groups, token) and henv.host.groups.blocked(s.cwd), "its STOPPING barrier is kept"
    assert SID in _state(henv.host), "its durable entry is kept"
    started, _ = henv.host.groups.start(s.cwd, ("session", "another", 1), lambda: object())
    assert not started, "nothing new is admitted beside the survivor"


def test_c1_report_stopped_keeps_a_run_whose_group_survived_its_kill(nenv):
    """Reproduction (e6ebf95: `report_stopped` let every run go and dropped it from its state file): a dispatched run
    whose SIGKILL returned with a member alive keeps its registry entry, its handle and its state entry, and is not
    reported ended; a new run is refused on that checkout. Then its group goes, and the release lets exactly that run
    go, once."""
    from test_nodes import allow, human_post
    nenv.node.hello()
    allow(nenv, agents=["grok"])
    human_post(nenv, ["grok"])
    nenv.d.tick()
    nenv.node.tick()
    [rid] = list(nenv.node.running)
    child = nenv.node_spawner.children[0]
    child.exits_on_terminate = False    # (it ignores its SIGTERM)
    nenv.node.signal_runs()
    nenv.node.kill_runs()
    assert child.killed, "its SIGKILL was sent"
    child.left_members = True           # (a member survived it)
    nenv.node.report_stopped()
    token = ("run", rid)
    assert rid in nenv.node.running and nenv.node.running[rid].child is child, "the handle is kept"
    assert _entry(nenv.node.groups, token), "its registry entry is kept"
    assert rid in json.loads(nenv.node._state_path().read_text()), "its state entry is kept"
    started, _ = nenv.node.groups.start(nenv.node_dir, ("run", "next"), lambda: object())
    assert not started, "no new run beside the survivor"
    child.left_members = False          # (the survivor is gone now)
    nenv.node.report_stopped()
    assert rid not in nenv.node.running and not _entry(nenv.node.groups, token), "released once its group is gone"
    assert rid not in json.loads(nenv.node._state_path().read_text())


# ---------------------------------------------------------------- the pin Sol named


def test_c1_a_kill_that_returns_with_a_survivor_then_exit_releases_exactly_that_reservation(henv):
    """Sol's pin: a SIGKILL returns while a member survives: the reservation, the handle and the durable entry are
    all still there; exit is then confirmed; exactly that reservation is released, once, and another session's is
    untouched."""
    a, ca = _a_turn(henv, SID)
    b, cb = _a_turn(henv, SID2)
    _survives_its_kill(henv, ca)
    ta, tb = a.group_token, b.group_token
    henv.host._stop(a, henv.clock())
    henv.clock.advance(henv.host.kill_grace + 1)
    henv.host.kill_due()
    assert ca.killed, "the SIGKILL returned"
    for _ in range(3):
        henv.host.tick()
    assert a.child is ca and a.group_token == ta and _entry(henv.host.groups, ta), "kept while a member lives"
    assert SID in _state(henv.host)
    henv.groups.discard(ca.pid)         # (the member is gone: exit confirmed)
    henv.host.tick()
    assert a.child is None and not _entry(henv.host.groups, ta), "exactly that reservation released"
    assert b.child is cb and _entry(henv.host.groups, tb), "the other session's untouched"
    assert SID not in _state(henv.host) and SID2 in _state(henv.host)
    released = []
    real = henv.host.groups._gone
    henv.host.groups._gone = lambda tok: (released.append(tok), real(tok))[1]
    henv.host.tick()
    assert ta not in released, "released once"


def test_c1_a_retained_process_is_left_alone_until_its_group_is_proved_gone(henv, monkeypatch):
    """The leader exited by itself and its group is neither seen alive nor proved gone (the listing cannot tell):
    the session keeps it, writes nothing to it and sends it no signal; once a later pass proves it gone, the
    session goes on as main's reap does (its turn failed: the process exited)."""
    s, child = _a_turn(henv)
    henv.host.group_absent = lambda pgid: False     # (no proof: a listing that could not be made)
    child.code = 0
    henv.host.send(SID, "next", message_id="2")
    for _ in range(3):
        out = henv.host.tick()
        assert not [e for e in out if e.kind == "turn_ended"]
    assert s.child is child and not child.terminated and not child.killed, "kept, untouched"
    assert child.turns() == ["a long turn"], "nothing more written to it"
    henv.clock.advance(henv.host.timeout_minutes * 60 + 1)      # (its turn's timeout, then a whole kill grace)
    henv.host.tick()
    henv.clock.advance(henv.host.kill_grace + 1)
    henv.host.tick()
    assert s.stopping_since is None and not child.terminated and not child.killed, \
        "no stop is armed and no SIGKILL sent for a process already gone, its group not proved gone"
    henv.host.group_absent = lambda pgid: True
    out = henv.host.tick()
    assert [e.detail for e in out if e.kind == "turn_ended" and e.message_id == "1"] == [
        "the process exited (code 0) before the turn ended"]


# ---------------------------------------------------------------- the proof's negative cases


class _FakeLib:
    """libproc's `proc_listpids` as a test says: (the size estimate, then what a filled call does)."""

    def __init__(self, filled):
        self.filled = filled

    def proc_listpids(self, kind, pgid, buf, size):
        assert kind == node_groups.PROC_PGRP_ONLY
        if buf is None:
            return 64
        return self.filled(buf, size)


@pytest.fixture
def fake_libproc(monkeypatch):
    def install(filled):
        monkeypatch.setattr(node_groups, "_LIBPROC", [_FakeLib(filled)])
    return install


def test_c1_a_complete_empty_listing_is_proof(fake_libproc):
    fake_libproc(lambda buf, size: 0)
    assert node_groups.group_absent(4242) is True


def test_c1_a_listing_with_any_member_is_not_proof(fake_libproc):
    def one(buf, size):
        buf[0] = 4243
        return 4
    fake_libproc(one)
    assert node_groups.group_members(4242) == (4243,) and node_groups.group_absent(4242) is False


def test_c1_a_truncated_listing_is_not_proof(fake_libproc):
    """A listing that filled its buffer may have been cut short, however large the buffer tried: not proof."""
    fake_libproc(lambda buf, size: size)
    assert node_groups.group_members(4242) is None and node_groups.group_absent(4242) is False


def test_c1_a_failed_or_refused_listing_is_not_proof(fake_libproc):
    """An error from the listing (a permission error, any failure): not proof."""
    fake_libproc(lambda buf, size: -1)
    assert node_groups.group_absent(4242) is False


def test_c1_no_libproc_is_not_proof(monkeypatch):
    monkeypatch.setattr(node_groups, "_LIBPROC", [None])
    assert node_groups.group_absent(4242) is False


@pytest.mark.parametrize("proof", ["false", "raises", "none"])
def test_c1_the_rule_releases_nothing_without_proof(proof):
    """`Groups.release`: a proof that says no, raises, or answers anything but True releases nothing and forgets
    nothing."""
    g = node_groups.Groups()
    started, _ = g.start("/w", "t1", lambda: object())
    forgot = []

    def answer():
        if proof == "raises":
            raise OSError("could not tell")
        return False if proof == "false" else None
    assert g.release("t1", answer, lambda: forgot.append(1)) is False
    assert not forgot and g.entries


def test_c1_the_leaders_exit_not_observed_retains(henv):
    """The group probe says gone but the leader's own handle has not observed its exit: not proof; kept."""
    s, child = _a_turn(henv)
    henv.host.group_absent = lambda pgid: True
    assert henv.host._release(s, child) is False and s.child is child
    child.code = 0
    assert henv.host._release(s, child) is True and s.child is None


def test_c1_release_is_the_only_way_out_of_the_registry():
    """The registry has no public way to drop an entry but the rule."""
    assert not hasattr(node_groups.Groups, "gone")


# ---------------------------------------------------------------- review round 1: HELD (R1: fenced; R2: proof-only)


def _retained_session(henv):
    """A session whose leader exited by itself, its group not seen alive (liveness false) and not proved gone (the
    listing cannot tell): retained where main would have released it."""
    s, child = _a_turn(henv)
    henv.host.group_absent = lambda pgid: False
    child.code = 0
    henv.host.tick()
    assert s.child is child, "retained"
    return s, child


def _retained_run(nenv):
    from test_nodes import allow, human_post
    nenv.node.hello()
    allow(nenv, agents=["grok"])
    human_post(nenv, ["grok"])
    nenv.d.tick()
    nenv.node.tick()
    [rid] = list(nenv.node.running)
    child = nenv.node_spawner.children[0]
    nenv.node.group_absent = lambda pgid: False
    child.code = 0
    nenv.node.tick()
    assert rid in nenv.node.running, "retained"
    return rid, child


def test_r1_a_retained_session_fences_its_checkout_until_the_proof(henv):
    """R1: a retained session's checkout admits no new group (e6ebf95's LIVE entry let one in beside a member that
    may still be there); once the proof is True the release lets it go and the checkout admits again."""
    s, child = _retained_session(henv)
    started, _ = henv.host.groups.start(s.cwd, ("session", "another", 1), lambda: object())
    assert not started, "refused while held"
    henv.host.group_absent = lambda pgid: True
    henv.host.tick()
    assert s.child is None
    started, _ = henv.host.groups.start(s.cwd, ("session", "another", 2), lambda: object())
    assert started, "admitted once proved gone"


def test_r1_a_retained_run_fences_its_checkout_until_the_proof(nenv):
    """R1, for a dispatched run."""
    rid, child = _retained_run(nenv)
    started, _ = nenv.node.groups.start(nenv.node_dir, ("run", "next"), lambda: object())
    assert not started, "refused while held"
    nenv.node.group_absent = lambda pgid: True
    nenv.node.tick()
    assert rid not in nenv.node.running
    started, _ = nenv.node.groups.start(nenv.node_dir, ("run", "next2"), lambda: object())
    assert started, "admitted once proved gone"


@pytest.mark.parametrize("site", ["reap", "kill_scan", "cancel", "close", "shutdown", "stop_turns"])
def test_r2_a_held_session_is_never_signalled_by_number_again(henv, site):
    """R2: after HELD, the pgid answers alive again (an unrelated group reused it: the stranger): no numeric signal
    goes to it from the reap, the kill scan, a cancel, a close, shutdown or a stop of every turn."""
    s, child = _retained_session(henv)
    henv.groups.add(child.pid)          # (the stranger)
    if site == "reap":
        out = []
        for _ in range(3):
            out += henv.host.tick()
        assert not [e for e in out if e.kind == "turn_ended"], "HELD: the reap decides nothing (no survivor stop)"
    elif site == "kill_scan":
        henv.clock.advance(henv.host.kill_grace + 1)
        henv.host.kill_due()
        henv.host.tick()
    elif site == "cancel":
        henv.host.cancel(SID, "1", "cancelled")
        henv.clock.advance(henv.host.kill_grace + 1)
        henv.host.kill_due()
    elif site == "close":
        henv.host.close(SID, "closed")
        henv.clock.advance(henv.host.kill_grace + 1)
        henv.host.kill_due()
    elif site == "shutdown":
        henv.host.stop_all()
    else:
        henv.host.stop_turns("the node lost the hub")
        henv.clock.advance(henv.host.kill_grace + 1)
        henv.host.kill_due()
    assert s.stopping_since is None, "no signal clock armed for it (shutdown does not re-arm it)"
    assert not child.terminated and not child.killed, "no numeric signal reached the stranger"
    assert not [sig for pid, sig in henv.signals if pid == child.pid]
    assert s.child is child and henv.host.groups.blocked(s.cwd), "still held"


@pytest.mark.parametrize("site", ["reap", "signal_runs", "kill_runs", "timeout"])
def test_r2_a_held_run_is_never_signalled_by_number_again(nenv, site):
    """R2, for a dispatched run: the stranger's group is never signalled by the reap, the shutdown's signal and
    kill steps, or the run's timeout."""
    rid, child = _retained_run(nenv)
    child.left_members = True           # (the stranger answers for that pgid now)
    if site == "reap":
        for _ in range(3):
            nenv.node.tick()
    elif site == "signal_runs":
        nenv.node.signal_runs()
    elif site == "kill_runs":
        nenv.node.kill_runs()
    else:
        nenv.clock.advance(60 * 60)
        nenv.node.tick()
        nenv.clock.advance(60)
        nenv.node.tick()
    assert not child.terminated and not child.killed, "no numeric signal reached the stranger"
    assert not [sig for pid, sig in nenv.node_procs.signals if pid == child.pid]
    assert rid in nenv.node.running


@pytest.mark.parametrize("proof", ["false", "raises", "none"])
def test_r2_held_is_sticky_until_the_proof_is_true(proof):
    """HELD never goes back to LIVE or STOPPING: a proof that says no, raises or answers None leaves it HELD; the
    stop marks change nothing; only a True proof releases it."""
    g = node_groups.Groups()
    g.start("/w", "t1", lambda: object())
    assert g.release("t1", lambda: False, lambda: None) is False
    assert g.state("t1") == g.HELD and not g.may_signal("t1")

    def answer():
        if proof == "raises":
            raise OSError("could not tell")
        return False if proof == "false" else None
    assert g.release("t1", answer, lambda: None) is False
    g.stopping("t1")
    assert g.try_stopping("t1") == g.ALREADY
    assert g.state("t1") == g.HELD and g.blocked("/w")
    assert g.release("t1", lambda: True, lambda: None) is True and g.state("t1") is None


def test_a3_a_refused_release_with_its_entry_missing_still_fences_the_checkout(henv):
    """Amendment A3: the registry has no entry for the token when the release is refused: the checkout is fenced
    under its known key (the cached `group_key`) anyway, HELD, and a new group is refused there."""
    s, child = _a_turn(henv)
    token = s.group_token
    henv.host.groups._gone(token)       # (the token lookup will miss)
    henv.host.group_absent = lambda pgid: False
    child.code = 0
    henv.host.tick()
    assert s.child is child and henv.host.groups.state(token) == henv.host.groups.HELD
    started, _ = henv.host.groups.start(s.cwd, ("session", "another", 1), lambda: object())
    assert not started, "refused on that checkout"


def test_a3_the_rules_fence_on_a_miss():
    """The rule itself: a refused release whose token has no entry gets a HELD one under the given fence."""
    g = node_groups.Groups()
    k = node_groups.key("/w")
    assert g.release("t9", lambda: False, lambda: None, (k, "/w")) is False
    assert g.state("t9") == g.HELD and g.blocked("/w")
    assert not g.start("/w", "t10", lambda: object())[0]


def test_a4_the_held_transition_cannot_come_between_the_signal_check_and_the_signal(env):
    """Amendment A4: a session's signal check and its signal run under the node's runner lock, and so does the HELD
    transition (the reap and the shutdown that call the rule hold it): a transition attempted between the check and
    the TERM waits for the lock, so the TERM goes for a record that was not HELD when asked, and HELD comes after."""
    import threading
    from test_hosted_sessions import start, tick
    from test_hosted_tree import proc_of
    runner = env.node.sessions
    host = runner.host
    sid = start(env, "a turn")["session_id"]
    tick(env)
    s = host.sessions[sid]
    child = proc_of(env, sid)[1]
    child.exits_on_terminate = False
    token = s.group_token
    order, done = [], threading.Event()
    real_may = host.groups.may_signal

    def transition():
        with runner.lock:
            host.groups.release(token, lambda: False, lambda: None)
            order.append("HELD")
        done.set()

    def may_signal(tok):
        got = real_may(tok)
        if tok == token and not order:
            order.append("check")
            threading.Thread(target=transition, daemon=True).start()
            assert not done.wait(0.3), "the transition waits for the runner lock"
        return got
    host.groups.may_signal = may_signal
    real_term = child.terminate
    child.terminate = lambda: (order.append("TERM"), real_term())[1]
    with runner.lock:
        host._stop(s, env.clock())
    assert done.wait(3)
    assert order == ["check", "TERM", "HELD"], order


# ---------------------------------------------------------------- R4: the real proof (macOS only)


@pytest.mark.skipif(__import__("sys").platform != "darwin", reason="macOS libproc only")
def test_r4_group_absent_against_real_children_of_this_test(monkeypatch, tmp_path):
    """Review rounds 1 and 2 (R4, R7): `group_absent` and `group_members` against this test's own child processes
    only, in its own temporary directory: a leader and a member it puts into the leader's group, both its direct
    children (so each is signalled through its own handle, and a handle that has been waited for is never signalled
    again). A live member is listed; the leader's exit is OBSERVED (kqueue `NOTE_EXIT`, which does not reap it) and its
    zombie is listed; the member alone once the leader is reaped; a listing that fills its buffer is no proof (the
    truncation rule); and no member at all is absence."""
    import select
    import subprocess
    # (a group of its own in this test's session: a process may join a group only in its own session)
    leader = subprocess.Popen(["sleep", "60"], cwd=tmp_path, process_group=0, stdin=subprocess.DEVNULL)
    member = None
    kq = select.kqueue()
    try:
        member = subprocess.Popen(["sleep", "60"], cwd=tmp_path, process_group=leader.pid, stdin=subprocess.DEVNULL)
        pg = leader.pid
        assert set(node_groups.group_members(pg)) == {pg, member.pid} and not node_groups.group_absent(pg)
        kq.control([select.kevent(pg, filter=select.KQ_FILTER_PROC, flags=select.KQ_EV_ADD | select.KQ_EV_ONESHOT,
                                  fflags=select.KQ_NOTE_EXIT)], 0)
        leader.kill()                       # (its own handle; not yet waited for)
        assert kq.control(None, 1, 10), "the leader's exit is observed, within ten seconds"
        assert pg in node_groups.group_members(pg), "a zombie leader is listed"
        leader.wait(timeout=10)
        assert node_groups.group_members(pg) == (member.pid,), "the member alone, once the leader is reaped"

        class Small:        # (the kernel's estimate made one slot: the real listing fills it)
            real = node_groups._libproc()

            def proc_listpids(self, kind, pgid, buf, size):
                return 4 if buf is None else self.real.proc_listpids(kind, pgid, buf, size)
        monkeypatch.setattr(node_groups, "_LIBPROC", [Small()])
        monkeypatch.setattr(node_groups, "_LISTING_ROOM", (0,))
        assert node_groups.group_members(pg) is None and not node_groups.group_absent(pg), "truncated: no proof"
        monkeypatch.undo()
        member.kill()
        member.wait(timeout=10)             # (observed and reaped by its own handle: no member is left)
        assert node_groups.group_members(pg) == () and node_groups.group_absent(pg), "no member: absent"
    finally:
        kq.close()
        for proc in (member, leader):
            if proc is None:
                continue
            if proc.returncode is None and proc.poll() is None:
                proc.kill()                 # (only a handle not yet waited for is signalled)
            if proc.returncode is None:
                proc.wait(timeout=10)


def test_r2_the_runners_shutdown_steps_never_signal_a_held_session(env):
    """R2 (and A4: under the runner lock): the node's own shutdown steps, `SessionRunner.signal` (TERM to every
    session process) and `SessionRunner.kill` (SIGKILL to every group still going), skip a HELD session, even when
    its pgid answers alive again (the stranger)."""
    from test_hosted_sessions import start, tick
    from test_hosted_tree import proc_of
    runner = env.node.sessions
    host = runner.host
    sid = start(env, "a turn")["session_id"]
    tick(env)
    s = host.sessions[sid]
    child = proc_of(env, sid)[1]
    host.group_absent = lambda pgid: False
    child.code = 0
    tick(env)
    assert s.child is child and host.groups.state(s.group_token) == host.groups.HELD
    env.groups.add(child.pid)           # (the stranger)
    runner.signal()
    runner.kill()
    assert not child.terminated and not child.killed, "no numeric signal from the runner's shutdown steps"


def test_r2_a_held_runs_reap_takes_no_survivor_stop(nenv, caplog):
    """R2, the run's reap: a HELD run whose pgid answers alive again (the stranger) is not taken for a leader that
    left members of its group: no survivor stop (its "stopping them" line), no signal, on any later pass."""
    import logging
    rid, child = _retained_run(nenv)
    child.left_members = True
    with caplog.at_level(logging.WARNING, logger="agent_comms.node"):
        for _ in range(3):
            nenv.node.tick()
    assert not [r for r in caplog.records if "left members of its group" in r.getMessage()], caplog.text
    assert not child.terminated and nenv.node.running[rid].terminated_at is None


def test_a3_a_runs_refused_release_with_its_entry_missing_still_fences_the_checkout(nenv):
    """Amendment A3, for a dispatched run: the registry has no entry for its token when the release is refused: its
    checkout is fenced under the key cached at its spawn, and a new run is refused there."""
    from test_nodes import allow, human_post
    nenv.node.hello()
    allow(nenv, agents=["grok"])
    human_post(nenv, ["grok"])
    nenv.d.tick()
    nenv.node.tick()
    [rid] = list(nenv.node.running)
    child = nenv.node_spawner.children[0]
    nenv.node.groups._gone(("run", rid))       # (the token lookup will miss)
    nenv.node.group_absent = lambda pgid: False
    child.code = 0
    nenv.node.tick()
    assert rid in nenv.node.running and nenv.node.groups.state(("run", rid)) == nenv.node.groups.HELD
    started, _ = nenv.node.groups.start(nenv.node_dir, ("run", "next"), lambda: object())
    assert not started, "refused on that checkout"


# ---------------------------------------------------------------- review round 2: R5 (the first KILL), R6 (fence first)


def _killed_with_a_survivor(henv, raising=False):
    """A session stopped by main's path (its turn timed out), its process ignoring SIGTERM; the kill scan's SIGKILL
    (or one that raises) leaves a member of its group alive: (session, child, the stop's clock)."""
    s, child = _a_turn(henv)
    child.exits_on_terminate = False
    henv.groups.add(child.pid)
    henv.host._stop(s, henv.clock())
    clock = s.stopping_since
    if raising:
        def bad():
            raise OSError("the signal failed")
        child.kill = bad
    henv.clock.advance(henv.host.kill_grace + 1)
    try:
        henv.host.kill_due()
    except OSError:
        assert raising      # (the watchdog's pass logs a raising kill and goes on)
    assert child.terminated == 1
    return s, child, clock


def _signals(child):
    return child.terminated + child.killed


@pytest.mark.parametrize("raising", [False, True])
@pytest.mark.parametrize("then", ["pass", "cancel", "close", "stop_all", "stop_turns"])
def test_r5_after_the_first_kill_no_numeric_signal_reaches_the_group_again(henv, then, raising):
    """R5: the FIRST KILL attempt (sent, or raised) makes the token proof-only: a member survives, and a later pass,
    a cancel, a close, shutdown (`stop_all`) or a stop of every turn sends no TERM and no KILL to it again (e6ebf95's
    STOPPING with `killed` latched let shutdown TERM and KILL it again: `terminate()` is a `killpg`)."""
    s, child, clock = _killed_with_a_survivor(henv, raising=raising)
    before = _signals(child)
    if then == "pass":
        for _ in range(3):
            henv.host.tick()
    elif then == "cancel":
        henv.host.cancel(SID, "1", "cancelled")
    elif then == "close":
        henv.host.close(SID, "closed")
    elif then == "stop_all":
        henv.host.stop_all()
    else:
        henv.host.stop_turns("the node lost the hub")
    henv.clock.advance(henv.host.kill_grace + 1)
    henv.host.kill_due()
    assert _signals(child) == before, "no numeric signal after the first KILL attempt"
    assert not henv.host.groups.may_signal(s.group_token)
    assert s.stopping_since == clock, "the stop's clock is kept"
    assert henv.host.groups.blocked(s.cwd), "the checkout stays fenced"


def test_r5_the_runners_shutdown_steps_send_nothing_after_the_first_kill(env):
    """R5: the node's own shutdown steps (`SessionRunner.signal`, `SessionRunner.kill`) send nothing to a group whose
    first KILL was attempted (the kill scan's), its member alive."""
    from test_hosted_sessions import start, tick
    from test_hosted_tree import proc_of
    runner = env.node.sessions
    host = runner.host
    sid = start(env, "a turn")["session_id"]
    tick(env)
    s = host.sessions[sid]
    child = proc_of(env, sid)[1]
    child.exits_on_terminate = False
    env.groups.add(child.pid)
    with runner.lock:
        host._stop(s, env.clock())
    env.clock.advance(host.kill_grace + 1)
    with runner.lock:
        host.kill_due()
    before = child.terminated + child.killed
    runner.signal()
    runner.kill()
    assert child.terminated + child.killed == before


def test_r5_the_runners_kill_step_vetoes_what_follows_it(env):
    """R5: the node's own shutdown KILL step (`SessionRunner.kill`) is a first KILL attempt: from it on, nothing more
    is sent by number, `stop_all`'s TERM and KILL included."""
    from test_hosted_sessions import start, tick
    from test_hosted_tree import proc_of
    runner = env.node.sessions
    host = runner.host
    sid = start(env, "a turn")["session_id"]
    tick(env)
    s = host.sessions[sid]
    child = proc_of(env, sid)[1]
    child.exits_on_terminate = False
    child.kill = lambda: setattr(child, "killed", child.killed + 1)    # (it returns; a member is left)
    env.groups.add(child.pid)
    runner.signal()
    runner.kill()
    assert child.killed == 1 and not host.groups.may_signal(s.group_token)
    before = child.terminated + child.killed
    with runner.lock:
        host.stop_all()
    assert child.terminated + child.killed == before, "nothing more after the first KILL attempt"


@pytest.mark.parametrize("then", ["reap", "signal_runs", "kill_runs", "report_stopped"])
def test_r5_after_a_runs_first_kill_nothing_more_is_sent(nenv, then):
    """R5, for a dispatched run: its SIGKILL (the reap's escalation after the grace) leaves a member; a later reap,
    the shutdown's signal and kill steps and `report_stopped` send nothing more."""
    from test_nodes import allow, human_post
    nenv.node.hello()
    allow(nenv, agents=["grok"])
    human_post(nenv, ["grok"])
    nenv.d.tick()
    nenv.node.tick()
    [rid] = list(nenv.node.running)
    child = nenv.node_spawner.children[0]
    child.exits_on_terminate = False
    nenv.node._stop(nenv.node.running[rid], "terminate")
    nenv.clock.advance(nenv.node.running[rid].grace + 1)
    nenv.node.tick()        # (the reap's escalation: its SIGKILL)
    assert child.killed == 1
    assert not nenv.node.groups.may_signal(("run", rid)), "proof-only from its first KILL attempt"
    child.left_members = True
    before = child.terminated + child.killed
    if then == "reap":
        for _ in range(3):
            nenv.node.tick()
    elif then == "signal_runs":
        nenv.node.signal_runs()
    elif then == "kill_runs":
        nenv.node.kill_runs()
    else:
        nenv.node.report_stopped()
    assert child.terminated + child.killed == before
    assert rid in nenv.node.running


def test_r5_a1_a_killed_held_group_keeps_its_stop_and_its_bounded_publication(henv, monkeypatch):
    """Amendment A1: HELD after the KILL revokes signal eligibility only. A Claude end decided and fenced by the cut,
    its group outliving the KILL: the bounded publication still publishes N's end at its deadline (two graces
    from the SIGTERM); the stop's clock is kept; and once the group is gone, the reap's release reads what the
    process wrote last and lets the group go."""
    from test_claude_steering import steered
    from test_claude_correlation import owned
    s, child, n = steered(henv, monkeypatch)
    s.steer.cover_until = henv.clock() + 10_000
    child.emit(owned(n, text="N's own"))
    henv.host.tick()
    assert s.held is not None
    child.exits_on_terminate = False
    henv.groups.add(child.pid)
    s.steer.cover_until = henv.clock() - 1
    henv.host.tick()            # (the cut: decided, fenced, its SIGTERM)
    assert s.held.decided is not None and s.held.fenced_at is not None and s.stopping_since is not None
    clock = s.stopping_since
    henv.clock.advance(henv.host.kill_grace + 1)
    henv.host.kill_due()
    assert child.killed == 1 and not henv.host.groups.may_signal(s.group_token)
    henv.clock.advance(henv.host.kill_grace + 1)
    out = henv.host.tick()
    assert [e.text for e in out if e.kind == "turn_ended" and e.message_id == "1"] == ["N's own"], \
        "the bounded publication is not skipped"
    assert s.stopping_since == clock
    child.emit(b'{"type": "system", "subtype": "note"}')
    henv.groups.discard(child.pid)
    child.code = -9
    henv.host.tick()
    assert s.child is None, "released at the proof"
    assert b"note" in open(henv.host.transcript_path(SID), "rb").read(), "the final read"


def test_r5_a1_a_killed_held_run_is_reported_ended_at_its_release(nenv):
    """Amendment A1: a run killed with a member left keeps its pending report: it is reported ended at the release,
    once the group is proved gone, as main reports it at its reap."""
    from agent_comms import nodes
    from test_nodes import allow, human_post
    nenv.node.hello()
    allow(nenv, agents=["grok"])
    post = human_post(nenv, ["grok"])
    nenv.d.tick()
    nenv.node.tick()
    [rid] = list(nenv.node.running)
    child = nenv.node_spawner.children[0]
    child.exits_on_terminate = False
    nenv.node._stop(nenv.node.running[rid], "terminate")
    nenv.clock.advance(nenv.node.running[rid].grace + 1)
    nenv.node.tick()
    child.left_members = True
    nenv.node.tick()
    assert nodes.run(nenv.board, rid)["state"] != "ended"
    child.left_members = False
    nenv.node.tick()
    assert rid not in nenv.node.running and nodes.run(nenv.board, rid)["state"] == "ended"


class _Gate:
    """A proof that waits until the test opens it, then answers `answer`."""

    def __init__(self, answer):
        import threading
        self.answer, self.entered, self.open = answer, threading.Event(), threading.Event()

    def __call__(self, pgid):
        self.entered.set()
        assert self.open.wait(10)
        if self.answer == "raises":
            raise OSError("could not tell")
        return self.answer


@pytest.mark.parametrize("answer", [True, False, None, "raises"])
def test_r6_the_checkout_is_fenced_while_the_proof_runs(henv, answer):
    """R6: the leader exited, liveness answers false, and the reap's proof is in progress (blocked here): a session
    and a run asking for that checkout are refused for the whole of it; after a True proof both are admitted, after
    False, None or a raising proof both are still refused (HELD)."""
    s, child = _a_turn(henv)
    gate = _Gate(answer)
    henv.host.group_absent = gate
    child.code = 0
    out, err, done = _run(lambda: henv.host.tick())
    assert gate.entered.wait(5), "the proof started"
    for tok in (("session", "another", 1), ("run", "r1")):
        started, _ = henv.host.groups.start(s.cwd, tok, lambda: object())
        assert not started, f"{tok} refused while the proof runs"
    gate.open.set()
    assert done.wait(5)
    started, _ = henv.host.groups.start(s.cwd, ("run", "r2"), lambda: object())
    assert started is (answer is True)


def test_r6_a3_the_fence_is_in_before_the_reaps_drain(henv):
    """Amendment A3: the reap's drain of what the process wrote comes after the fence (at the false liveness answer),
    not just the proof: an admission during that drain is refused."""
    s, child = _a_turn(henv)
    seen = []
    real = henv.host._drain_before_stop

    def drain(sess, now, out):
        if sess is s:
            seen.append(henv.host.groups.start(s.cwd, ("run", "during-drain"), lambda: object())[0])
        return real(sess, now, out)
    henv.host._drain_before_stop = drain
    henv.host.group_absent = lambda pgid: True
    child.code = 0
    henv.host.tick()
    assert seen and seen[0] is False, "refused during the drain"
    assert s.child is None, "then released by the True proof"


def _run(fn):
    import threading
    out, err, done = [], [], threading.Event()

    def go():
        try:
            out.extend(fn() or [])
        except BaseException as e:      # noqa: BLE001
            err.append(e)
        finally:
            done.set()
    threading.Thread(target=go, daemon=True).start()
    return out, err, done


def test_r6_a4_a_run_refused_by_a_clean_exits_fence_waits_and_is_admitted_next(nenv):
    """Amendment A4 (and R6.3): a run that meets a fence a clean exit's proof holds is deferred, as at a STOPPING
    barrier (its "waits" line), never failed; its next attempt, once the proof came back True, starts it."""
    from agent_comms import nodes
    from test_nodes import allow, human_post
    k = node_groups.key(nenv.node_dir)
    nenv.node.groups.release(("session", "clean", 1), lambda: False, lambda: None, (k, nenv.node_dir))
    nenv.node.hello()
    allow(nenv, agents=["grok"])
    human_post(nenv, ["grok"])
    nenv.d.tick()
    nenv.node.tick()
    assert nenv.node_spawner.calls == [], "deferred while fenced"
    [r] = [r for r in nodes.runs(nenv.board) if r.get("node") == "laptop"] if hasattr(nodes, "runs") else [None]
    nenv.node.groups.release(("session", "clean", 1), lambda: True, lambda: None)
    nenv.node.tick()
    assert len(nenv.node_spawner.calls) == 1, "admitted on its next attempt, not failed"


def test_r6_a4_a_session_refused_by_a_clean_exits_fence_waits_and_starts_next(henv):
    """Amendment A4: a session whose spawn meets that fence keeps its message in the inbox (deferred, as at a STOPPING
    barrier), and starts once the fence is lifted."""
    k = node_groups.key(henv.work)
    henv.host.groups.release(("session", "clean", 1), lambda: False, lambda: None, (k, henv.work))
    s = open_session(henv)
    henv.host.send(SID, "a turn", message_id="1")
    out = henv.host.tick()
    assert s.child is None and [m.id for m in s.inbox] == ["1"], "deferred, not failed"
    assert not [e for e in out if e.kind == "turn_ended"]
    henv.host.groups.release(("session", "clean", 1), lambda: True, lambda: None)
    henv.host.tick()
    assert s.child is not None


def test_r5_shutdowns_kill_pass_vetoes_at_its_attempt(henv):
    """R5: `_stop_all`'s KILL pass makes each token it signals proof-only at the attempt, before anything else of the
    shutdown runs (here: the first step of its traversal)."""
    s, child = _a_turn(henv)
    child.exits_on_terminate = False
    henv.groups.add(child.pid)
    seen = []
    real = henv.host._flush_tee
    henv.host._flush_tee = lambda sess: (seen.append(henv.host.groups.may_signal(sess.group_token)), real(sess))[1]
    henv.host.stop_all()
    assert child.killed == 1 and seen and seen[0] is False


def test_r5_a1_a_killed_group_that_outlives_its_kill_is_still_said_in_the_log(henv, caplog):
    """Amendment A1: a stopping session whose token the KILL made proof-only keeps main's steps: its group outliving
    the KILL is still said in the log (`_lingering`), and its registry entry stays STOPPING (main's survivor branch,
    which signals nothing) while the group is seen alive."""
    import logging
    s, child, clock = _killed_with_a_survivor(henv)
    child.code = -9         # (the leader is gone; a member lives on)
    henv.clock.advance(2 * henv.host.kill_grace + 1)
    with caplog.at_level(logging.WARNING, logger="agent_comms.session_host"):
        henv.host.tick()
    assert [r for r in caplog.records if "is still there" in r.getMessage()], caplog.text
    assert henv.host.groups.state(s.group_token) == henv.host.groups.STOPPING, "main's survivor branch"
    assert s.child is child and s.stopping_since == clock


# ---------------------------------------------------------------- review round 3: S1 (the shutdown precondition), S2


def test_s1_after_stopping_no_session_group_is_started(env):
    """Review round 3, S1: from `stopping` (set by `SessionRunner.signal`) on, nothing starts a session's group: a
    session waiting for its first process gets none from the loop's pass or the watchdog's (so a veto that the
    shutdown's KILL step puts on a token still LIVE leaves no admission beside it)."""
    from test_hosted_sessions import start
    runner = env.node.sessions
    host = runner.host
    starts = []
    real = host.groups.start
    host.groups.start = lambda *a, **k: (starts.append(a[1]), real(*a, **k))[1]
    start(env, "a turn")            # (on the hub, not yet given a process)
    runner.signal()
    runner.pass_once()
    runner.enforce_deadlines()
    assert starts == [], "no group started after stopping"


def test_s1_shutdown_starts_no_dispatched_run(nenv):
    """Review round 3, S1: a dispatched run assigned to this node when it shuts down is never started by the
    shutdown (its admission, `Node.tick`, runs on the thread that runs `shut_down`)."""
    from agent_comms.node import shut_down
    from test_nodes import allow, human_post
    nenv.node.hello()
    allow(nenv, agents=["grok"])
    human_post(nenv, ["grok"])
    nenv.d.tick()                   # (assigned to the node; the node has not ticked)
    starts = []
    real = nenv.node.groups.start
    nenv.node.groups.start = lambda *a, **k: (starts.append(a[1]), real(*a, **k))[1]
    shut_down(nenv.node)
    assert starts == [] and nenv.node_spawner.calls == []


def test_s2_a_failed_final_read_still_says_the_hold_once(henv, caplog):
    """Review round 3, S2: the release path's final reads raise after the fence: the fence stays (HELD), and the one
    retention line is written on that path too; a later pass that still cannot prove the group gone says nothing
    more."""
    import logging
    s, child = _a_turn(henv)
    henv.host.group_absent = lambda pgid: False
    child.code = 0
    real = henv.host._drain_before_stop
    calls = []

    def drain(sess, now, out):
        calls.append(1)
        if len(calls) == 1:
            raise OSError("a read failed")
        return real(sess, now, out)
    henv.host._drain_before_stop = drain
    token = s.group_token
    with caplog.at_level(logging.WARNING, logger="agent_comms.session_host"):
        henv.host.tick()
        henv.host.tick()
    lines = [r for r in caplog.records if "could not be proved gone" in r.getMessage()]
    assert len(lines) == 1, caplog.text
    assert henv.host.groups.state(token) == henv.host.groups.HELD and henv.host.groups.blocked(s.cwd)
