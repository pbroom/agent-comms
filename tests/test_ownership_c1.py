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
    keeps its entry; a new process is refused on that checkout. Shutdown returned within its bound (round 4, R9:
    plus at most the one post-KILL window, `node_groups.POST_KILL_SECONDS`)."""
    s, child = _a_turn(henv)
    _survives_its_kill(henv, child)
    token = s.group_token
    t0 = henv.clock()
    henv.host.stop_all()
    assert henv.clock() - t0 <= henv.host.kill_grace + node_groups.POST_KILL_SECONDS + 1, "within its bound"
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
    """Review round 3, S2: the release path's final reads raise after the fence: the token is HELD, and the one
    retention line is written on that path too; a later pass that still cannot prove the group gone says nothing
    more. (Round 4, R11: the line waits until the group has stayed unproved for the kill grace.)"""
    import logging
    s, child = _a_turn(henv)
    henv.host.group_absent = lambda pgid: False
    child.code = 0
    real = henv.host._drain_before_stop
    calls = []

    def drain(sess, now, out):
        calls.append(1)
        raise OSError("a read failed")      # (every pass: the line can only come from the raising path)
    henv.host._drain_before_stop = drain
    token = s.group_token
    with caplog.at_level(logging.WARNING, logger="agent_comms.session_host"):
        henv.host.tick()
        henv.clock.advance(henv.host.kill_grace)
        henv.host.tick()
        henv.clock.advance(henv.host.kill_grace)
        henv.host.tick()
    lines = [r for r in caplog.records if "could not be proved gone" in r.getMessage()]
    assert len(lines) == 1, caplog.text
    assert henv.host.groups.state(token) == henv.host.groups.HELD and henv.host.groups.blocked(s.cwd)


# ---------------------------------------------------------------- review round 4: libproc's failure is 0 with errno set


class _ErrnoLib:
    """A stand-in libproc whose `proc_listpids` answers (estimate, listing) as told: an int, or ("fail", errno) for a
    failed call as Apple's wrapper reports it (0, with the cause left in errno)."""

    def __init__(self, estimate, listing):
        self.estimate, self.listing = estimate, listing

    def proc_listpids(self, kind, pgid, buf, size):
        import ctypes
        what = self.estimate if buf is None else self.listing
        if isinstance(what, tuple):
            ctypes.set_errno(what[1])
            return 0
        return what


@pytest.mark.parametrize("failing", ["estimate", "listing", "both"])
def test_r4_a_listing_that_fails_with_zero_and_errno_is_no_proof(monkeypatch, failing):
    """Review round 4: Apple's `proc_listpids` returns 0, not -1, when its syscall fails, and leaves the cause in
    errno. A 0 with errno set, on the estimate call or the listing call, is a FAILURE: no listing (None), not
    absent; and a release whose proof rests on it retains, HELD (e500549 took it for an empty group)."""
    fail = ("fail", 14)
    lib = _ErrnoLib(fail if failing in ("estimate", "both") else 64, fail if failing in ("listing", "both") else 0)
    monkeypatch.setattr(node_groups, "_LIBPROC", [lib])
    assert node_groups.group_members(4242) is None and node_groups.group_absent(4242) is False
    g = node_groups.Groups()
    g.start("/w", "t1", lambda: object())
    assert g.release("t1", lambda: node_groups.group_absent(4242), lambda: None) is False
    assert g.state("t1") == g.HELD


def test_r4_zero_with_errno_clear_is_an_empty_group(monkeypatch):
    """Review round 4: a 0 with errno 0 is the only "no member" answer: () and absent."""
    monkeypatch.setattr(node_groups, "_LIBPROC", [_ErrnoLib(64, 0)])
    assert node_groups.group_members(4242) == () and node_groups.group_absent(4242) is True


def test_r4_an_errno_left_by_an_earlier_call_does_not_fail_a_clean_answer(monkeypatch):
    """Review round 4: errno is cleared before each call: a non-zero errno left by an earlier, unrelated call does
    not turn a clean empty answer into a failure."""
    import ctypes
    monkeypatch.setattr(node_groups, "_LIBPROC", [_ErrnoLib(64, 0)])
    ctypes.set_errno(22)
    assert node_groups.group_members(4242) == ()


@pytest.mark.skipif(__import__("sys").platform != "darwin", reason="macOS libproc only")
def test_r4_a_real_failed_listing_is_no_proof(monkeypatch):
    """Review round 4, against the REAL libproc: the listing call is made with an invalid buffer address (inside
    this process only), so the real syscall fails with EFAULT and the real wrapper returns 0 with errno set:
    `group_members` is None and `group_absent` False, for this process's own group."""
    import ctypes
    import os
    real = node_groups._libproc()

    class BadBuffer:
        def proc_listpids(self, kind, pgid, buf, size):
            if buf is None:
                return real.proc_listpids(kind, pgid, buf, size)
            return real.proc_listpids(kind, pgid, ctypes.c_void_p(1), size)    # (an address nothing maps)
    monkeypatch.setattr(node_groups, "_LIBPROC", [BadBuffer()])
    pg = os.getpgrp()
    assert node_groups.group_members(pg) is None and node_groups.group_absent(pg) is False


# ---------------------------------------------------------------- the PR's reviews (round 4): A1 of R8, and R9 to R12

darwin_only = pytest.mark.skipif(__import__("sys").platform != "darwin", reason="macOS libproc only")
POST_KILL = getattr(node_groups, "POST_KILL_SECONDS", 1.0)      # (the reproductions also run where it is not named)
STUBBORN = 'trap "" TERM; : > "$1"; exec sleep 60'      # a leader that ignores SIGTERM (kept across its exec)


class _Writes:
    """A stand-in libproc whose listing call writes `pids` into the buffer and answers `count` bytes."""

    def __init__(self, count, pids=()):
        self.count, self.pids = count, pids

    def proc_listpids(self, kind, pgid, buf, size):
        import ctypes
        if buf is None:
            return 64
        arr = ctypes.cast(buf, ctypes.POINTER(ctypes.c_int))
        for i, p in enumerate(self.pids):
            arr[i] = p
        return size + 4 if self.count == "over" else self.count


@pytest.mark.parametrize("count, pids", [(6, ()), (8, ()), (8, (4242,)), ("over", ())],
                         ids=["not-whole-pids", "zero-slots", "a-zero-slot", "beyond-the-buffer"])
def test_a1_a_malformed_byte_count_is_no_proof(monkeypatch, count, pids):
    """Amendment A1 (R8): a byte count that is not a whole number of pids, one larger than the buffer, or slots
    within it that hold no pid, is malformed: no listing (None), not absent. ("No member" is only a buffered call
    that answered 0 bytes with errno 0; e500549 read 6 bytes, or 8 bytes of zeros, as an empty group.)"""
    monkeypatch.setattr(node_groups, "_LIBPROC", [_Writes(count, pids)])
    assert node_groups.group_members(4242) is None and node_groups.group_absent(4242) is False


def test_a1_a_part_pid_after_a_real_one_is_no_proof(monkeypatch):
    """A1: 6 bytes, the first 4 a real pid: not a whole number of pids, so malformed (None), not that one member."""
    monkeypatch.setattr(node_groups, "_LIBPROC", [_Writes(6, (4242, 4243))])
    assert node_groups.group_members(4242) is None


def test_a1_a_malformed_answer_is_not_retried_into_proof(monkeypatch):
    """A1: a count beyond the buffer is malformed at once (None): it is not retried with a larger buffer until some
    later call answers 0."""
    class OverThenEmpty:
        listings = 0

        def proc_listpids(self, kind, pgid, buf, size):
            if buf is None:
                return 64
            self.listings += 1
            return size + 4 if self.listings == 1 else 0
    monkeypatch.setattr(node_groups, "_LIBPROC", [OverThenEmpty()])
    assert node_groups.group_members(4242) is None
    monkeypatch.setattr(node_groups, "_LIBPROC", [OverThenEmpty()])     # (a fresh one: its first listing again)
    assert node_groups.group_absent(4242) is False


def test_a1_a_negative_count_is_no_proof_whatever_errno_says(monkeypatch):
    """R8: a negative result is a failure even with errno clear (here minus the buffer's size, which would otherwise
    slice the listing to nothing and read as empty)."""
    class Negative:
        def proc_listpids(self, kind, pgid, buf, size):
            import ctypes
            ctypes.set_errno(0)
            return 64 if buf is None else -size
    monkeypatch.setattr(node_groups, "_LIBPROC", [Negative()])
    assert node_groups.group_members(4242) is None and node_groups.group_absent(4242) is False


def test_a1_a_short_listing_with_a_member_retains(monkeypatch):
    """Amendment A1: a short, well-formed listing with a member is that member: not absent."""
    monkeypatch.setattr(node_groups, "_LIBPROC", [_Writes(4, (4242,))])
    assert node_groups.group_members(4242) == (4242,) and node_groups.group_absent(4242) is False


def test_a1_only_the_buffered_call_answers_no_member(monkeypatch):
    """Amendment A1: an estimate of 0 bytes is no answer about membership: the buffered call is still made, and its
    failure (0 with errno set) is no proof."""
    monkeypatch.setattr(node_groups, "_LIBPROC", [_ErrnoLib(0, ("fail", 14))])
    assert node_groups.group_members(4242) is None and node_groups.group_absent(4242) is False


@darwin_only
def test_a1_a_stale_errno_is_not_the_listings_own_against_the_real_libproc(tmp_path):
    """Amendment A1, the REAL libproc: errno left set by an earlier call (stale) does not fail a clean answer for a
    group that does not exist (a child of this test, exited and reaped: `()`), while errno set by the listing call
    itself (an invalid buffer address: EFAULT) does (None)."""
    import ctypes
    import os
    import subprocess
    gone = subprocess.Popen(["/usr/bin/true"], cwd=tmp_path, process_group=0, stdin=subprocess.DEVNULL)
    gone.wait(timeout=10)
    ctypes.set_errno(14)                    # (stale: left by an earlier, unrelated call)
    assert node_groups.group_members(gone.pid) == () and node_groups.group_absent(gone.pid) is True
    real = node_groups._libproc()

    class OwnFailure:
        def proc_listpids(self, kind, pgid, buf, size):
            return real.proc_listpids(kind, pgid, None if buf is None else ctypes.c_void_p(1), size)
    node_groups._LIBPROC[:] = [OwnFailure()]
    try:
        assert node_groups.group_members(os.getpgrp()) is None
    finally:
        node_groups._LIBPROC[:] = [real]


def _wait_for(path, seconds=10.0):
    import os
    import time
    end = time.monotonic() + seconds
    while not os.path.exists(path):
        assert time.monotonic() < end, "the child never got ready"
        time.sleep(0.01)


class _RealRuns:
    """A node's spawner that starts REAL children of this test: a leader that ignores SIGTERM, in a process group of
    its own inside this test's session (so the test may put a member of its own into it). Each is signalled only
    through its own handle (PopenChild, or the test's own `kill` on a handle not yet waited for)."""

    def __init__(self, tmp_path, script=None):
        self.tmp, self.procs, self.script = tmp_path, [], script or STUBBORN

    def __call__(self, argv, *, cwd, env, log_path, **kw):
        import subprocess
        from agent_comms.dispatch import PopenChild
        ready = self.tmp / f"ready-{len(self.procs)}"
        p = subprocess.Popen(["/bin/sh", "-c", self.script, "sh", str(ready)], cwd=cwd, process_group=0,
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.procs.append(p)
        _wait_for(ready)
        return PopenChild(p)

    def cleanup(self):
        for p in self.procs:
            if p.returncode is None and p.poll() is None:
                p.kill()
            if p.returncode is None:
                p.wait(timeout=10)


def _real_run(nenv, tmp_path, script=None):
    """A dispatched run of the hub's, on a node whose clock, sleep and process questions are the REAL ones, and whose
    one child is `_RealRuns`'s: (node, run id, spawner)."""
    import time
    from agent_comms import session_host
    from test_nodes import allow, human_post
    node, spawner = nenv.node, _RealRuns(tmp_path, script)
    node.spawner, node.clock, node.sleep = spawner, time.monotonic, time.sleep
    node.group_alive, node.group_absent = session_host.group_alive, node_groups.group_absent
    node.config.kill_grace_seconds = 0.3
    node.hello()
    allow(nenv, agents=["grok"])
    human_post(nenv, ["grok"])
    nenv.d.tick()
    node.tick()
    [rid] = list(node.running)
    return node, rid, spawner


def _timed(node, marks, clock):
    """Wrap the shutdown's kill pass and its reports, to time the wait between them."""
    kill, report = node.kill_runs, node.report_stopped

    def kill_runs():
        out = kill()
        marks.setdefault("kill", clock())
        return out

    def report_stopped():
        marks.setdefault("report", clock())
        return report()
    node.kill_runs, node.report_stopped = kill_runs, report_stopped


HOLD_LINE = "could not be proved gone"


@darwin_only
def test_r9_a_run_killed_at_shutdown_is_reported_ended_when_nothing_survives(nenv, tmp_path, caplog):
    """R9 (the review's sequence, REAL children): the node shuts down while a run's leader ignores SIGTERM past the
    grace; its SIGKILL is sent; `poll()` is still None just after it. The node watches, with no signal, for the
    kill to finish (one shared, bounded window), then releases the run and reports it ended at shutdown exactly as
    main does ("node shut down"): the state file no longer has it, no retention line. (e500549 kept it HELD and
    never reported it.)"""
    import json as _json
    from agent_comms import nodes
    from agent_comms.node import shut_down
    node, rid, spawner = _real_run(nenv, tmp_path)
    try:
        with caplog.at_level("WARNING", logger="agent_comms"):
            shut_down(node)
        r = nodes.run(nenv.board, rid)
        assert (r["state"], r.get("error")) == ("ended", "node shut down"), r
        assert rid not in node.running and rid not in _json.loads(node._state_path().read_text())
        assert not node.groups.entries and not node.groups.held
        assert not [m for m in caplog.messages if HOLD_LINE in m]
    finally:
        spawner.cleanup()


@darwin_only
def test_r9_a_run_whose_group_keeps_a_member_is_retained_past_the_bounded_wait(nenv, tmp_path, caplog):
    """R9 and A6 (REAL children): a run's group keeps a member its SIGKILL cannot end within the window (a zombie:
    this test's own child, put into the leader's group, ended through its own handle and not reaped). The node waits
    the one bounded window, no longer; then the run is retained: HELD, kept in the SAVED state file, NOT reported to
    the hub (it is still running there), and its retention line is written once, at shutdown."""
    import json as _json
    import select
    import subprocess
    import time
    from agent_comms import nodes
    from agent_comms.node import shut_down
    node, rid, spawner = _real_run(nenv, tmp_path)
    leader = spawner.procs[0]
    member = subprocess.Popen(["sleep", "60"], cwd=tmp_path, process_group=leader.pid, stdin=subprocess.DEVNULL)
    kq = select.kqueue()
    try:
        kq.control([select.kevent(member.pid, filter=select.KQ_FILTER_PROC,
                                  flags=select.KQ_EV_ADD | select.KQ_EV_ONESHOT, fflags=select.KQ_NOTE_EXIT)], 0)
        member.kill()                       # (its own handle, not waited for: a zombie in the leader's group)
        assert kq.control(None, 1, 10), "the member's exit is observed"
        marks: dict = {}
        _timed(node, marks, time.monotonic)
        with caplog.at_level("WARNING", logger="agent_comms"):
            shut_down(node)
        waited = marks["report"] - marks["kill"]
        assert POST_KILL - 0.05 <= waited <= POST_KILL + 1.0, waited
        token = ("run", rid)
        assert rid in node.running and node.groups.state(token) == node.groups.HELD
        assert not node.groups.may_signal(token)
        assert rid in _json.loads(node._state_path().read_text()), "the SAVED record keeps it"
        assert nodes.run(nenv.board, rid)["state"] == "running", "the HUB was not told it ended"
        assert len([m for m in caplog.messages if HOLD_LINE in m]) == 1, caplog.messages
    finally:
        kq.close()
        if member.returncode is None:
            member.wait(timeout=10)         # (reaped by its own handle: no signal)
        spawner.cleanup()


def _fake_runs(nenv, n, exits):
    from agent_comms.node import _Local
    from test_dispatch import FakeChild
    node = nenv.node
    node.sleep = lambda s: nenv.clock.advance(s)
    kids = []
    for i in range(n):
        c = FakeChild(7000 + i, exits_on_terminate=exits)
        c.polls_after_kill = 10 ** 6        # (its exit is never seen within the window)
        node.running[f"run-{i}"] = _Local(f"run-{i}", c, node.clock(), 1800.0, 10.0, cwd=nenv.node_dir)
        kids.append(c)
    return node, kids


def test_r9_one_shared_post_kill_deadline_for_every_killed_run(nenv):
    """R9: ONE deadline for all of a shutdown's killed runs, not one per run: three runs whose kills never finish
    within it cost the window once; each is then retained, HELD."""
    from agent_comms.node import shut_down
    node, kids = _fake_runs(nenv, 3, exits=False)
    marks: dict = {}
    _timed(node, marks, nenv.clock)
    shut_down(node)
    assert all(c.killed == 1 for c in kids)
    step = getattr(node_groups, "POST_KILL_STEP", 0.05)
    assert POST_KILL <= marks["report"] - marks["kill"] <= POST_KILL + step / 2, "the deadline ends it"
    assert len(node.running) == 3 and all(node.groups.state(("run", r)) == node.groups.HELD for r in node.running)


def test_r9_a_shutdown_that_needed_no_kill_does_not_wait(nenv):
    """R9 and R13.3: a shutdown whose runs all went on their SIGTERM and are already proved gone sends no KILL and
    waits for nothing."""
    from agent_comms.node import shut_down
    node, kids = _fake_runs(nenv, 2, exits=True)
    marks: dict = {}
    _timed(node, marks, nenv.clock)
    shut_down(node)
    assert all(c.killed == 0 for c in kids) and marks["report"] == marks["kill"] and node.running == {}


def test_r9_the_stop_all_fallback_waits_for_its_kill_too(nenv):
    """R9.3: `Node.stop_all` (the fallback when the shutdown's own steps failed) watches its KILLs the same way: a
    run whose exit is seen a moment after its SIGKILL is reported ended, "node shut down"."""
    from agent_comms import nodes
    from test_nodes import allow, human_post
    nenv.node.sleep = lambda s: nenv.clock.advance(s)
    nenv.node.hello()
    allow(nenv, agents=["grok"])
    human_post(nenv, ["grok"])
    nenv.d.tick()
    nenv.node.tick()
    [rid] = list(nenv.node.running)
    child = nenv.node_spawner.children[0]
    child.exits_on_terminate, child.polls_after_kill = False, 3
    nenv.node.stop_all()
    assert child.killed == 1 and rid not in nenv.node.running
    r = nodes.run(nenv.board, rid)
    assert (r["state"], r.get("error")) == ("ended", "node shut down"), r


@darwin_only
def test_r9_a_hosted_session_killed_at_shutdown_is_released_when_nothing_survives(henv, tmp_path, caplog):
    """A2 (a REAL child as the session's process): the host's shutdown waits its grace from the SIGTERM, then sends
    its SIGKILL; `poll()` is still None just after it. The host watches the killed group, with no signal, for the
    one bounded window, then releases it as main does: no process kept, nothing in the state file, no line."""
    from agent_comms.dispatch import spawn_piped
    from agent_comms.session_host import SessionHost
    procs = []

    def real(argv, *, cwd, env, log_path):
        ready = tmp_path / "ready"
        child = spawn_piped(["/bin/sh", "-c", STUBBORN, "sh", str(ready)], cwd=cwd, env={"PATH": "/bin:/usr/bin"},
                            log_path=log_path)
        procs.append(child)
        _wait_for(ready)
        return child
    host = SessionHost(henv.config, henv.log_dir, spawner=real, kill_grace_seconds=0.3)
    s = host.open(SID, agent="claude", runtime="claude-code", project=open_session.__globals__["PROJECT"])
    host.send(SID, "a long turn", message_id="1")
    host.tick()
    try:
        assert s.child is procs[0]
        with caplog.at_level("WARNING", logger="agent_comms"):
            host.stop_all()
        assert s.child is None and SID not in _state(host) and not host.groups.entries
        assert not [m for m in caplog.messages if HOLD_LINE in m or "may still be running" in m], caplog.messages
    finally:
        for c in procs:
            if c.proc.returncode is None and c.proc.poll() is None:
                c.proc.kill()
            if c.proc.returncode is None:
                c.proc.wait(timeout=10)


def test_r9_the_nodes_watch_covers_the_sessions_it_killed(tmp_path):
    """A2: the node's one post-KILL watch covers the hosted sessions its `sessions.kill` killed: their exit is seen
    within it, before the runs' reports, not left to the host's later shutdown."""
    from agent_comms.node import shut_down
    from test_hosted_sessions import children, session_env, start, tick
    senv = session_env(tmp_path)
    node = senv.node
    sid = start(senv)["session_id"]
    tick(senv)
    [child] = children(senv)
    child.exits_on_terminate, child.polls_after_kill = False, 3
    seen = {}
    real = node.report_stopped
    node.report_stopped = lambda: (seen.update(code=child.code, at=senv.clock()), real())[1]
    marks: dict = {}
    kill = node.sessions.kill
    node.sessions.kill = lambda: (marks.setdefault("kill", senv.clock()), kill())[1]
    shut_down(node)
    assert child.killed == 1 and seen["code"] == -9, "its exit was seen in the node's watch"
    assert 0 < seen["at"] - marks["kill"] < POST_KILL
    assert node.sessions.host.sessions[sid].child is None


def test_r9_the_sessions_fallback_after_their_kill_raised_still_releases_them(tmp_path, monkeypatch):
    """A2: the sessions' initial KILL (the node's `sessions.kill`) raises. The node's wait (its killed run only) can
    delay the sessions' fallback by at most the window; the fallback (the host's own shutdown: its grace, its KILL
    pass, its own window) then kills the session's group and releases it, and the hub is told it has no process."""
    from agent_comms.node import _Local, shut_down
    from test_dispatch import FakeChild
    from test_hosted_sessions import children, msg, session_env, show, start, tick
    senv = session_env(tmp_path)
    node = senv.node
    sid = start(senv)["session_id"]
    tick(senv)
    [child] = children(senv)
    child.exits_on_terminate = False
    run = FakeChild(5000, exits_on_terminate=False)
    run.polls_after_kill = 2
    node.running["run-1"] = _Local("run-1", run, senv.clock(), 1800.0, 10.0)

    def boom():
        raise RuntimeError("the sessions' kill failed")
    monkeypatch.setattr(node.sessions, "kill", boom)
    marks: dict = {}
    _timed(node, marks, senv.clock)
    order = []
    report, kill = node.report_stopped, child.kill
    node.report_stopped = lambda: (order.append(("runs reported", senv.clock())), report())[1]
    child.kill = lambda: (order.append(("session KILL", senv.clock())), kill())[1]
    shut_down(node)
    step = getattr(node_groups, "POST_KILL_STEP", 0.05)
    assert marks["report"] - marks["kill"] <= POST_KILL + step / 2, "the watch delays the fallback by at most the window"
    assert [what for what, _ in order] == ["runs reported", "session KILL"], \
        "round 5, R14: the fallback's KILL still follows the runs' reports, as on main (the order is kept)"
    assert child.killed == 1, "the fallback killed the session's group"
    host = node.sessions.host
    assert host.sessions[sid].child is None and sid not in _state(host), "released, and not on record"
    assert show(senv, sid)["pid"] is None and msg(senv, sid, 1)["state"] == "failed"
    assert node.running == {}


# ---------------------------------------------------------------- R10: the fence revokes admission only


def test_r10_a_start_failed_read_at_the_reap_fails_only_its_own_message(henv):
    """R10 (the review's sequence): a Codex process writes `start_failed` and exits between the pass's read and the
    reap's poll. The final read fails message 1 and stops the process (main's bookkeeping: the exit is a stop), so
    message 2 stays queued and starts a new process, as on main. (e500549's fence made `_stop` return early, so the
    reap took it for an exit by itself and failed message 2 too.)"""
    import test_providers_codex as cx
    from test_session_host import codex_host
    host, s = codex_host(henv)
    first = host.send(SID, "first")
    second = host.send(SID, "second")
    host.tick()
    [child] = henv.spawner.children
    real_poll, armed = child.poll, []

    def poll():
        if not armed:       # (after the pass's read: it writes its refusal and exits before the reap's poll)
            armed.append(True)
            child.emit(cx.reply(1, error={"code": -32600, "message": "Already initialized"}))
            child.code = 1
        return real_poll()
    child.poll = poll
    events = host.tick()
    events += host.tick()
    failed = [e.message_id for e in events if e.kind == "turn_ended" and not e.ok]
    assert failed == [first], events
    assert len(henv.spawner.children) == 2 and s.child is henv.spawner.children[1], "a new process starts"
    assert [m.id for m in s.inbox] == [second], "for message 2, still queued: it waits for that one's handshake"


def test_r10_the_release_fence_refuses_admission_and_nothing_else():
    """R10 and A3: during the final reads (`prepare`) the token is FENCED: admission is refused (start, blocked,
    the stopping directories), `may_signal` is unchanged, a stop's bookkeeping moves it to STOPPING; a refused proof
    then makes it HELD, a True one removes it. It never clears an existing veto."""
    g = node_groups.Groups()
    g.start("/w", "t", lambda: object())
    seen = {}

    def prepare():
        seen.update(state=g.state("t"), may=g.may_signal("t"), blocked=g.blocked("/w"),
                    admitted=g.start("/w", "u", lambda: object())[0], dirs=g.stopping_dirs())
        g.stopping("t")
        seen["after_stop"] = g.state("t")
    assert g.release("t", lambda: False, lambda: None, prepare=prepare) is False
    assert seen == {"state": "fenced", "may": True, "blocked": True, "admitted": False, "dirs": {"/w"},
                    "after_stop": g.STOPPING}, seen
    assert g.state("t") == g.HELD and not g.may_signal("t")

    v = node_groups.Groups()
    v.start("/v", "k", lambda: object())
    v.veto("k")                             # (a KILL was attempted: proof-only from then on)
    during = []
    assert v.release("k", lambda: True, lambda: None, prepare=lambda: during.append(v.may_signal("k"))) is True
    assert during == [False], "the veto stands through the final reads"
    assert v.state("k") is None and v.may_signal("k"), "released with its proof"


def test_r10_the_fence_keeps_a_stopping_entry_and_fences_a_miss():
    """A3: the fence is installed only from LIVE or a missing entry; a STOPPING entry stays STOPPING; a missing one
    gets a FENCED entry under the known key; a stop-path reservation of a FENCED entry is main's (RESERVED)."""
    g = node_groups.Groups()
    g.start("/w", "s", lambda: object())
    g.stopping("s")
    states = []
    g.release("s", lambda: False, lambda: None, prepare=lambda: states.append(g.state("s")))
    assert states == [g.STOPPING] and g.state("s") == g.HELD
    m = node_groups.Groups()
    k = node_groups.key("/m")
    reserved = []
    m.release("x", lambda: False, lambda: None, fence=(k, "/m"),
              prepare=lambda: (states.append(m.state("x")), reserved.append(m.try_stopping("x"))))
    assert states[-1] == "fenced" and reserved == [m.RESERVED] and m.state("x") == m.HELD


def test_r10_final_reads_that_raise_leave_the_token_held():
    """A3: the final reads raise after the fence: the token is HELD (refused), never left FENCED."""
    g = node_groups.Groups()
    g.start("/w", "t", lambda: object())

    def bad():
        raise RuntimeError("the final read failed")
    with pytest.raises(RuntimeError):
        g.release("t", lambda: True, lambda: None, prepare=bad)
    assert g.state("t") == g.HELD and not g.may_signal("t")


def test_r10_a_session_killed_by_the_nodes_shutdown_reads_its_last_lines_as_main_does(tmp_path):
    """R10's audit, the shutdown's final reads: the node's `sessions.kill` vetoes the session's group first, so the
    host's shutdown stop of it sends nothing; it must still keep main's bookkeeping (the stop's clock), or its last
    reads decide differently: here a line naming another session (Claude's `fatal`) would END the session at
    shutdown, where main drops a stopping process's late events and the session stays to be resumed."""
    from agent_comms.node import shut_down
    from test_hosted_sessions import children, msg, session_env, show, start, tick
    from test_session_host import OTHER, init
    senv = session_env(tmp_path)
    sid = start(senv)["session_id"]
    tick(senv)
    [child] = children(senv)
    child.exits_on_terminate = False
    child.emit(init(OTHER))                 # (written before it went; read only by the shutdown's final reads)
    shut_down(senv.node)
    assert child.killed == 1
    view = show(senv, sid)
    assert view["state"] != "ended" and view["pid"] is None, view
    assert msg(senv, sid, 1)["state"] == "failed"


# ---------------------------------------------------------------- R11: only the line waits


def _exited_run(nenv):
    from test_nodes import allow, human_post
    nenv.node.hello()
    allow(nenv, agents=["grok"])
    human_post(nenv, ["grok"])
    nenv.d.tick()
    nenv.node.tick()
    [rid] = list(nenv.node.running)
    child = nenv.node_spawner.children[0]
    zombies = {child.pid}
    real = nenv.node.group_absent
    nenv.node.group_absent = lambda pg: pg not in zombies and real(pg)
    child.code = 0                          # (its leader exited; a zombie member is left, for now)
    return rid, child, zombies


def test_r11_a_runs_zombie_instant_logs_nothing(nenv, caplog):
    """R11: the leader is reaped and only a zombie member is left for an instant: the first release is refused
    (HELD at once), the next pass releases it; no retention line, as on main."""
    from agent_comms import nodes
    rid, child, zombies = _exited_run(nenv)
    with caplog.at_level("WARNING", logger="agent_comms"):
        nenv.node.tick()
        assert nenv.node.groups.state(("run", rid)) == nenv.node.groups.HELD, "HELD is immediate"
        zombies.clear()
        nenv.clock.advance(1)
        nenv.node.tick()
    assert rid not in nenv.node.running and nodes.run(nenv.board, rid)["state"] == "ended"
    assert not [m for m in caplog.messages if HOLD_LINE in m], caplog.messages


def test_r11_a_run_held_past_the_bound_logs_once(nenv, caplog):
    """R11 and A4: a group that stays unproved logs its retention line once, when it has been unproved for the
    run's kill grace (the bound), and never again."""
    rid, child, zombies = _exited_run(nenv)
    grace = nenv.node.running[rid].grace
    with caplog.at_level("WARNING", logger="agent_comms"):
        nenv.node.tick()
        nenv.clock.advance(grace - 1)
        nenv.node.tick()
        assert not [m for m in caplog.messages if HOLD_LINE in m], "not within the bound"
        nenv.clock.advance(2)
        nenv.node.tick()
        nenv.clock.advance(60)
        nenv.node.tick()
    assert len([m for m in caplog.messages if HOLD_LINE in m]) == 1, caplog.messages
    assert rid in nenv.node.running


def test_r11_a_sessions_zombie_instant_logs_nothing_and_past_the_bound_once(henv, caplog):
    """R11, a hosted session's group: a zombie instant says nothing; a group unproved past the kill grace says its
    line once."""
    s, child = _a_turn(henv)
    zombies = {child.pid}
    real = henv.host.group_absent
    henv.host.group_absent = lambda pg: pg not in zombies and real(pg)
    child.code = 0
    with caplog.at_level("WARNING", logger="agent_comms"):
        henv.host.tick()
        assert s.child is child and henv.host.groups.state(s.group_token) == henv.host.groups.HELD
        assert not [m for m in caplog.messages if HOLD_LINE in m]
        henv.clock.advance(henv.host.kill_grace + 1)
        henv.host.tick()
        henv.clock.advance(60)
        henv.host.tick()
    assert len([m for m in caplog.messages if HOLD_LINE in m]) == 1, caplog.messages
    zombies.clear()
    henv.host.tick()
    assert s.child is None


# ---------------------------------------------------------------- R12: off macOS, main's rule, stated


def test_r12_off_macos_the_membership_half_is_mains_liveness_probe(monkeypatch, tmp_path):
    """R12 and A5: where there is no complete listing (not macOS), `group_absent` is main's own liveness answer,
    `not session_host.group_alive(pgid)`, so a run's or a session's release happens as on main (a REAL child of this
    test: absent once it is reaped, present while it runs)."""
    import subprocess
    from types import SimpleNamespace
    from agent_comms import session_host
    monkeypatch.setattr(node_groups, "sys", SimpleNamespace(platform="linux"))
    monkeypatch.setattr(node_groups, "_LIBPROC", [])        # (as a fresh process off macOS would load it)
    asked = []
    real = session_host.group_alive
    monkeypatch.setattr(session_host, "group_alive", lambda pg: (asked.append(pg), real(pg))[1])
    p = subprocess.Popen(["sleep", "60"], cwd=tmp_path, process_group=0, stdin=subprocess.DEVNULL)
    try:
        assert node_groups.group_absent(p.pid) is False
        p.kill()                            # (its own handle)
        p.wait(timeout=10)
        assert node_groups.group_absent(p.pid) is True and asked == [p.pid, p.pid], "main's probe answered"
        g = node_groups.Groups()
        g.start(str(tmp_path), "t", lambda: object())
        assert g.release("t", lambda: node_groups.group_absent(p.pid), lambda: None) is True
    finally:
        if p.returncode is None:
            p.kill()
            p.wait(timeout=10)


@darwin_only
def test_r12_on_macos_a_libproc_load_failure_retains_and_says_so_once(monkeypatch, caplog):
    """A5: on macOS, a libproc that could not be loaded is NOT proof (retained, never downgraded to the weaker
    probe), and it is said once in the log."""
    import os
    from agent_comms import session_host
    monkeypatch.setattr(node_groups, "_LIBPROC", [None])
    monkeypatch.setattr(node_groups, "_LOAD_FAILED_SAID", [], raising=False)
    monkeypatch.setattr(session_host, "group_alive", lambda pg: pytest.fail("never the weaker probe"))
    with caplog.at_level("WARNING", logger="agent_comms"):
        assert node_groups.group_absent(424242) is False and node_groups.group_absent(os.getpgrp()) is False
    assert len([m for m in caplog.messages if "libproc could not be loaded" in m]) == 1, caplog.messages


# ---------------------------------------------------------------- review round 5: R13, R14, R15

TERM_EXITS = ': > "$1"; exec sleep 60'     # a leader that ends on SIGTERM


def _member(leader, tmp_path):
    """This test's own child, put into the leader's process group (a member the node did not start)."""
    import subprocess
    return subprocess.Popen(["sleep", "60"], cwd=tmp_path, process_group=leader.pid, stdin=subprocess.DEVNULL)


def _reap_later(proc, after):
    """Reap this test's own child through its own handle after `after` seconds (no signal: it is already dead)."""
    import threading
    t = threading.Timer(after, lambda: proc.wait(timeout=10))
    t.start()
    return t


def _zombie(pid):
    """Whether this pid is a zombie, as `ps` says (asked without reaping it or signalling it)."""
    import subprocess
    out = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
    return out.startswith("Z")


def _until(check, seconds=10.0):
    import time
    end = time.monotonic() + seconds
    while not check():
        assert time.monotonic() < end, "never happened"
        time.sleep(0.01)


def _killed_by_a_pass_before_shutdown(nenv, tmp_path):
    """R13's sequence (REAL children): the run's timeout pass stops it (its SIGTERM ends the member, this test's own
    child, which stays a zombie) and its grace pass KILLs the leader, just before the node shuts down; the leader is
    then reaped. No reap pass runs between that and the shutdown."""
    node, rid, spawner = _real_run(nenv, tmp_path)
    leader = spawner.procs[0]
    member = _member(leader, tmp_path)
    local = node.running[rid]
    local.timeout, local.grace = 0.0, 0.0
    node._reap()                            # (the timeout pass: SIGTERM)
    _until(lambda: _zombie(member.pid))     # (the member ended on it; never reaped here: it stays listed)
    node._reap()                            # (the grace pass: SIGKILL)
    assert local.killed
    _until(lambda: local.child.poll() is not None)      # (the leader is reaped through its own handle)
    assert member.returncode is None and node_groups.group_members(leader.pid) == (member.pid,), "a zombie member"
    return node, rid, spawner, member


@darwin_only
@pytest.mark.parametrize("reaped_in_time", [True, False], ids=["clears-in-the-window", "never-clears"])
def test_r13_a_run_killed_by_an_earlier_pass_is_watched_at_shutdown(nenv, tmp_path, caplog, reaped_in_time):
    """R13 (a) and (d), REAL children: a run whose KILL came from an ordinary pass just before the shutdown, its leader
    already reaped and a zombie member still listed. The shutdown's watch selects it (not yet proved gone), whoever
    sent its KILL. (a) the member is reaped inside the window: the run is reported "ended" ("node shut down"), gone
    from the state file, no line. (d) it never clears in time: the shutdown spends the one window, then the run is
    retained, HELD, on record, not reported, said once. (5f90d53 watched only what `kill_runs` killed just now.)"""
    import json as _json
    import time
    from agent_comms import nodes
    from agent_comms.node import shut_down
    node, rid, spawner, member = _killed_by_a_pass_before_shutdown(nenv, tmp_path)
    timer = _reap_later(member, 0.3) if reaped_in_time else None
    try:
        t0 = time.monotonic()
        with caplog.at_level("WARNING", logger="agent_comms"):
            shut_down(node)
        took = time.monotonic() - t0
        r = nodes.run(nenv.board, rid)
        lines = [m for m in caplog.messages if HOLD_LINE in m]
        if reaped_in_time:
            assert (r["state"], r.get("error")) == ("ended", "node shut down"), r
            assert rid not in node.running and rid not in _json.loads(node._state_path().read_text())
            assert not lines and took < POST_KILL + 1.0
        else:
            assert POST_KILL - 0.05 <= took <= POST_KILL + 1.0, took
            assert rid in node.running and node.groups.state(("run", rid)) == node.groups.HELD
            assert rid in _json.loads(node._state_path().read_text()) and r["state"] == "running"
            assert len(lines) == 1, caplog.messages
    finally:
        if timer is not None:
            timer.join(10)
        if member.returncode is None:
            member.wait(timeout=10)
        spawner.cleanup()


@darwin_only
def test_r13_a_run_that_ended_on_its_sigterm_is_watched_at_shutdown(nenv, tmp_path, caplog):
    """R13 (b), REAL children, no KILL at all: the shutdown's SIGTERM ends the leader and the member (this test's own
    child, a zombie until the test reaps it inside the window). The watch selects the run, not yet proved gone, and
    it is reported "ended" at shutdown, as on main. (5f90d53 sent no KILL, so watched nothing, and kept it.)"""
    import json as _json
    from agent_comms import nodes
    from agent_comms.node import shut_down
    node, rid, spawner = _real_run(nenv, tmp_path, script=TERM_EXITS)
    member = _member(spawner.procs[0], tmp_path)
    timer = _reap_later(member, 0.3)
    try:
        with caplog.at_level("WARNING", logger="agent_comms"):
            shut_down(node)
        r = nodes.run(nenv.board, rid)
        assert (r["state"], r.get("error")) == ("ended", "node shut down"), r
        assert rid not in node.running and rid not in _json.loads(node._state_path().read_text())
        assert not [m for m in caplog.messages if HOLD_LINE in m]
    finally:
        timer.join(10)
        spawner.cleanup()


@darwin_only
def test_r13_a_hosted_session_vetoed_by_an_earlier_kill_is_watched_at_shutdown(henv, tmp_path, caplog):
    """R13 (c), REAL children: a hosted session whose group was stopped and KILLed by an earlier pass (vetoed), its
    leader reaped, a zombie member (this test's own child) still listed. The host's own shutdown watch selects it,
    though its KILL pass sends nothing now; the member is reaped inside the window and the session is released."""
    import subprocess
    from agent_comms.dispatch import PipedChild
    from agent_comms.session_host import SessionHost
    procs = []

    def real(argv, *, cwd, env, log_path):
        ready = tmp_path / "ready"
        p = subprocess.Popen(["/bin/sh", "-c", STUBBORN, "sh", str(ready)], cwd=cwd, env={"PATH": "/bin:/usr/bin"},
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                             process_group=0, bufsize=0)
        procs.append(p)
        _wait_for(ready)
        return PipedChild(p)
    host = SessionHost(henv.config, henv.log_dir, spawner=real, kill_grace_seconds=0.3)
    s = host.open(SID, agent="claude", runtime="claude-code", project=open_session.__globals__["PROJECT"])
    host.send(SID, "a long turn", message_id="1")
    host.tick()
    member = None
    timer = None
    try:
        member = _member(procs[0], tmp_path)
        _until(lambda: len(node_groups.group_members(procs[0].pid) or ()) == 2)
        host._stop(s, host.clock())                     # (its SIGTERM: the member ends, a zombie)
        assert host._kill_if_due(s, host.clock() + 1)   # (the kill scan's SIGKILL: vetoed from here on)
        _until(lambda: s.child.poll() is not None)
        assert not host.groups.may_signal(s.group_token)
        timer = _reap_later(member, 0.3)
        with caplog.at_level("WARNING", logger="agent_comms"):
            host.stop_all()
        assert s.child is None and SID not in _state(host) and not host.groups.entries
        assert not [m for m in caplog.messages if HOLD_LINE in m or "may still be running" in m], caplog.messages
    finally:
        if timer is not None:
            timer.join(10)
        if member is not None and member.returncode is None:
            member.wait(timeout=10)
        for p in procs:
            if p.returncode is None and p.poll() is None:
                p.kill()
            if p.returncode is None:
                p.wait(timeout=10)


def test_r13_the_runners_watch_selects_every_session_not_yet_proved_gone(tmp_path):
    """R13 and A1: `SessionRunner.unproved` asks of EVERY session with a process handle (read under the runner's
    lock) whether it is proved gone, whoever sent its KILL: one whose leader exited by itself with a member left is
    unproved; once the member is gone it is not."""
    from test_hosted_sessions import children, session_env, start, tick
    senv = session_env(tmp_path)
    runner = senv.node.sessions
    start(senv)
    tick(senv)
    [child] = children(senv)
    child.code = 0                          # (its leader exited by itself; no KILL was sent)
    senv.groups.add(child.pid)              # (a member is left)
    assert runner.unproved(0.5) is True
    senv.groups.discard(child.pid)
    assert runner.unproved(0.5) is False


def test_r14_the_runners_watch_never_waits_past_its_budget_for_the_lock(tmp_path):
    """R14 and A2: the runner's lock is taken with the watch's remaining budget as its timeout (non-blocking with
    none left); held elsewhere, the look counts as "still unproved" and returns within that budget."""
    import threading
    import time
    from test_hosted_sessions import session_env
    runner = session_env(tmp_path).node.sessions
    held, done = threading.Event(), threading.Event()

    def hold():
        with runner.lock:
            held.set()
            done.wait(5)
    t = threading.Thread(target=hold)
    t.start()
    try:
        held.wait(5)
        t0 = time.monotonic()
        assert runner.unproved(0.2) is True
        assert time.monotonic() - t0 < 1.0
        t0 = time.monotonic()
        assert runner.unproved(0) is True
        assert time.monotonic() - t0 < 0.2, "no budget left: one non-blocking attempt"
    finally:
        done.set()
        t.join(5)


def test_r14_the_watch_hands_the_sessions_its_remaining_budget(nenv):
    """R14: the node's watch asks the sessions with what is left of its one deadline, and ends at that deadline."""
    node = nenv.node
    node.sleep = lambda s: nenv.clock.advance(s)
    budgets = []
    node.await_kills(lambda left: (budgets.append(left), True)[1])
    assert budgets and budgets[0] == pytest.approx(POST_KILL) and all(0 <= b <= POST_KILL for b in budgets)
    assert budgets == sorted(budgets, reverse=True) and budgets[-1] < budgets[0] / 2, "what is LEFT of the window"


def test_r14_a_session_group_that_never_clears_costs_the_shared_window_once(tmp_path):
    """R13.3 and A1: a session group that cannot be proved gone spends the one shared window once at the node's
    shutdown: the host's own watch, after the runs' reports, uses the node's deadline (already past), and adds
    nothing of its own when its KILL pass sends nothing."""
    from agent_comms.node import shut_down
    from test_hosted_sessions import children, session_env, start, tick
    senv = session_env(tmp_path)
    node = senv.node
    start(senv)
    tick(senv)
    [child] = children(senv)
    senv.groups.add(child.pid)              # (a member that never goes)
    watch = []
    real = node.await_kills

    def timed(*a, **k):
        t = senv.clock()
        out = real(*a, **k)
        watch.append(senv.clock() - t)
        return out
    node.await_kills = timed
    t0 = senv.clock()
    shut_down(node)
    grace = node.config.kill_grace_seconds
    assert watch and watch[0] == pytest.approx(POST_KILL)
    # (the node's grace, its one window, then the host's own grace wait for a group still alive, as on main)
    assert senv.clock() - t0 <= 2 * grace + POST_KILL + 0.5, "the window once, not twice"


class _InterruptingLock:
    """The registry's lock, which raises KeyboardInterrupt (as the node's SIGTERM handler does) right after its next
    release, once armed."""

    def __init__(self, lock):
        self.lock, self.armed = lock, False

    def __enter__(self):
        return self.lock.__enter__()

    def __exit__(self, *exc):
        self.lock.__exit__(*exc)
        if self.armed:
            self.armed = False
            raise KeyboardInterrupt

    def acquire(self, *a, **k):
        return self.lock.acquire(*a, **k)

    def release(self):
        self.lock.release()


def _live(token="t", cwd="/w"):
    g = node_groups.Groups()
    g.start(cwd, token, lambda: object())
    return g


def test_r15_an_interrupt_before_the_fence_is_installed_leaves_the_token_as_it_was(monkeypatch):
    """R15 and A3: an interrupt BEFORE the fence is installed: the token is exactly as it was (no HELD for a fence
    that was never set)."""
    g = _live()

    def before(*a):
        raise KeyboardInterrupt
    monkeypatch.setattr(g, "_admit_fence", before)
    with pytest.raises(KeyboardInterrupt):
        g.release("t", lambda: True, lambda: None)
    assert g.state("t") == g.LIVE and g.may_signal("t")


@pytest.mark.parametrize("where", ["inside-the-installation", "as-the-lock-is-released", "in-prepare", "in-the-proof"])
def test_r15_an_interrupt_after_the_fence_is_installed_never_leaves_it_fenced(monkeypatch, where):
    """R15 and A3: an interrupt after the fence is installed (inside `_admit_fence` once it installed, immediately
    after the registry's lock is released, during the final reads, during the proof): the token ends HELD (admission
    fenced AND numeric signalling vetoed), never FENCED. (5f90d53 left it FENCED for the first two.)"""
    g = _live()
    prepare = proof = None
    if where == "inside-the-installation":
        real = g._admit_fence

        def installed_then_interrupted(*a):
            real(*a)
            raise KeyboardInterrupt
        monkeypatch.setattr(g, "_admit_fence", installed_then_interrupted)
    elif where == "as-the-lock-is-released":
        g.lock = _InterruptingLock(g.lock)
        g.lock.armed = True
    elif where == "in-prepare":
        def prepare():
            raise KeyboardInterrupt
    else:
        def proof():
            raise KeyboardInterrupt
    with pytest.raises(KeyboardInterrupt):
        g.release("t", proof or (lambda: False), lambda: None, prepare=prepare)
    assert g.state("t") == g.HELD and not g.may_signal("t") and g.blocked("/w")


def test_r15_an_interrupt_after_forget_still_completes_the_registry_cleanup():
    """A3: the proof held and `forget()` dropped the owner's record, then an interrupt: the registry's removal still
    completes, so no proved-gone group's entry is left behind with no record to retry it. (5f90d53 made it HELD.)"""
    g = _live()
    record = {"t": object()}

    def forget():
        record.pop("t")
        raise KeyboardInterrupt
    with pytest.raises(KeyboardInterrupt):
        g.release("t", lambda: True, forget)
    assert record == {} and g.state("t") is None and g.may_signal("t") and not g.blocked("/w")
