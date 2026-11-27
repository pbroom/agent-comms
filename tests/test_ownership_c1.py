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
