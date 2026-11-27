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
    """Review round 1, R4: `group_absent` and `group_members` against this test's own child processes only (its own
    temporary directory): a live member is listed; a zombie leader is listed; a group with no member is absent; and
    a listing that fills its buffer is no proof (the truncation rule)."""
    import os
    import signal
    import subprocess
    import sys
    import time
    leader = subprocess.Popen([sys.executable, "-c", "import subprocess, time; "
                               "m = subprocess.Popen(['sleep', '30']); print(m.pid, flush=True); time.sleep(30)"],
                              cwd=tmp_path, start_new_session=True, stdout=subprocess.PIPE, text=True,
                              stdin=subprocess.DEVNULL)
    member = int(leader.stdout.readline())
    pg = leader.pid
    try:
        assert set(node_groups.group_members(pg)) == {pg, member} and not node_groups.group_absent(pg)
        os.kill(pg, signal.SIGKILL)
        time.sleep(0.3)
        assert pg in node_groups.group_members(pg), "a zombie leader is listed"
        leader.wait()
        assert node_groups.group_members(pg) == (member,), "the member alone, once the leader is reaped"

        class Small:        # (the kernel's estimate made one slot: the real listing fills it)
            real = node_groups._libproc()

            def proc_listpids(self, kind, pgid, buf, size):
                return 4 if buf is None else self.real.proc_listpids(kind, pgid, buf, size)
        monkeypatch.setattr(node_groups, "_LIBPROC", [Small()])
        monkeypatch.setattr(node_groups, "_LISTING_ROOM", (0,))
        assert node_groups.group_members(pg) is None and not node_groups.group_absent(pg), "truncated: no proof"
        monkeypatch.undo()
        os.kill(member, signal.SIGKILL)
        deadline = time.monotonic() + 5
        while node_groups.group_members(pg) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert node_groups.group_members(pg) == () and node_groups.group_absent(pg), "no member: absent"
    finally:
        for pid in (member, pg):
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if leader.poll() is None:
            leader.wait()


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
