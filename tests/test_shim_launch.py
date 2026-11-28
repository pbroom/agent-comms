"""Ownership C3-2b-1, C: the supervised launch's lifecycle helpers (agent_comms/shimlaunch.py), UNREACHABLE from any
config in this PR (tests/test_shim_static.py pins that). DESIGN_NOTES "Ownership C3-2b-1".

REAL processes and REAL signals (macOS): the shims are this test's own children, started by `shimlaunch.launch`; the
CLIs are tests/test_groupshim.py's stand-in. Nothing is signalled but through its own handle (and by the shims, which
signal only their own groups). Every wait is bounded; every shim is stopped and reaped through its handle at the end
(test_groupshim's `Kit.cleanup`)."""
from __future__ import annotations

import os
import signal
import stat
import sys
from pathlib import Path

import pytest

if sys.platform != "darwin":
    pytest.skip("the shim is macOS-only (kqueue, libproc)", allow_module_level=True)

from agent_comms import groupshim, node_groups, shimchild, shimjournal, shimlaunch   # noqa: E402
from agent_comms.shimchild import STATUS_UNKNOWN, ShimChild   # noqa: E402

from test_groupshim import ENV, WAIT, Kit, gate_open, gate_reach, read_json, shim_exit, wait_event, wait_for  # noqa

HELD, LIVE, STOPPING, FENCED = (node_groups.Groups.HELD, node_groups.Groups.LIVE, node_groups.Groups.STOPPING,
                                node_groups.Groups.FENCED)


class Crash(BaseException):
    """The node process dying at that point (in process: nothing of the launch runs after it but its own handlers)."""


class Env:
    """One node's registry, journal and marker directory, around test_groupshim's Kit (its stand-in and cleanup)."""

    def __init__(self, kit: Kit):
        self.kit = kit
        self.groups = node_groups.Groups()
        self.journal = shimjournal.Journal(kit.tmp / "logs" / shimjournal.JOURNAL_DIR)
        self.markers = shimchild.make_marker_dir(str(kit.tmp / "markers"))
        self.n = 0

    def track(self, launch, gate):
        h = launch.handle
        if h is not None and h not in self.kit.children:
            h.gate_dir = gate
            self.kit.children.append(h)

    def launch(self, argv, grace=1.0, gate_files=(), numbered=False, pause=None, **kw):
        self.n += 1
        gate = None
        if gate_files or numbered:
            gate = self.kit.tmp / f"gate{self.n}"
            gate.mkdir()
            for name in gate_files:
                (gate / name).write_text("")
            if numbered:
                (gate / "numbered").write_text("")

        def hook(step, launch):
            self.track(launch, gate)
            if pause is not None:
                pause(step, launch)
        launch = shimlaunch.launch(groups=self.groups, journal=self.journal, marker_dir=self.markers,
                                   cwd=str(self.kit.work), argv=argv, env=ENV, kill_grace=grace,
                                   log=str(self.kit.tmp / f"shim{self.n}.log"),
                                   stderr_log=str(self.kit.tmp / f"stderr{self.n}.log"),
                                   test_gate=None if gate is None else str(gate), pause=hook, **kw)
        self.track(launch, gate)
        return launch

    def ids(self):
        return [r.launch_id for r in self.journal.load()]

    def release(self, launch, pause=None):
        return shimlaunch.release(self.groups, self.journal, launch, pause=pause)


@pytest.fixture
def env(kit):
    return Env(kit)


@pytest.fixture
def kit(tmp_path):
    k = Kit(tmp_path)
    try:
        yield k
    finally:
        k.cleanup()


def reaped_by_its_handle(h: ShimChild) -> bool:
    """The shim's status was observed by its own handle (not UNKNOWN), and it is no child of this process any more."""
    status = h.shim_poll()
    if status is None or status == STATUS_UNKNOWN:
        return False
    try:
        os.waitpid(h.pid, os.WNOHANG)
    except ChildProcessError:
        return True
    return False


# ---------------------------------------------------------------- launch: before the commit, nothing is left


@pytest.mark.parametrize("step", ["shim-start", "after-start", "commit"])
def test_launch_a_failure_before_the_commit_leaves_no_record_no_entry_and_no_unreaped_shim(env, monkeypatch, step):
    """Inject a failure at each step before the durable commit: the start of the shim itself (no boot identity: nothing
    is spawned), right after the shim started, and the commit (its file sync fails: the commit removes its file). The
    launch raises; no record, no registry entry, the CLI never started, and a started shim is reaped by its handle
    (it got EOF before GO: never started)."""
    up = env.kit.path("up")
    seen = []

    def pause(name, launch):
        seen.append(launch.handle)
        if step == "after-start" and name == "started":
            raise OSError(5, "injected")
    if step == "shim-start":
        monkeypatch.setattr(shimchild, "boot_id", lambda: None)
    if step == "commit":
        real = shimjournal._fullsync

        def sync(fd):                           # (the record file's sync fails; a directory's goes through)
            if stat.S_ISREG(os.fstat(fd).st_mode):
                raise OSError(5, "injected EIO")
            real(fd)
        monkeypatch.setattr(shimjournal, "_fullsync", sync)
    with pytest.raises(OSError):
        env.launch(env.kit.argv(started=up, wait_abort=True), pause=pause)
    assert env.ids() == [] and env.groups.entries == {} and env.groups.held == set()
    handles = {id(h): h for h in seen if h is not None}.values()
    if step == "shim-start":
        assert not handles, "nothing was spawned"
    else:
        [h] = handles
        assert reaped_by_its_handle(h) and h.shim_poll() == groupshim.EXIT_NOGO, (h.shim_poll(), h.events)
        assert ("NOGO", "eof") in h.events
    assert not os.path.exists(up), "the CLI never started"


def test_launch_no_registry_entry_exists_before_the_durable_commit(env):
    """At injected pauses: right after the shim started there is no record and no entry; right after the commit the
    record is durable and there is STILL no entry; at the GO (inside the registry's atomic admission) the entry is
    LIVE. The launch ends STARTED, and its whole life ends in a release by the proof."""
    at = {}

    def pause(name, launch):
        at[name] = (env.ids(), env.groups.state(launch.token))
    launch = env.launch(["/bin/sh", "-c", "exit 0"], pause=pause)
    assert launch.outcome == shimlaunch.STARTED
    lid = launch.launch_id
    assert at["started"] == ([], None)
    assert at["committed"] == ([lid], None), "the durable record, and no registry entry yet"
    assert at["go"] == ([lid], LIVE), "the reservation, installed with the admission check, before GO"
    assert shim_exit(launch.handle) == groupshim.EXIT_EMPTY
    assert env.release(launch) and env.ids() == [] and env.groups.entries == {}


# ---------------------------------------------------------------- launch: after the commit, the record is retained


def test_launch_a_crash_after_the_commit_and_before_go_leaves_the_record_and_the_shim_publishes_never_started(env):
    """After the commit and before GO the node "dies": the test drops the handle's writer. The record stays (with no
    entry: the registry was not reached; a restarted node restores the record as HELD). The shim sees EOF and
    publishes its never-started marker, which proves the launch: the release then removes the record."""
    up = env.kit.path("up")
    got = []

    def pause(name, launch):
        if name == "committed":
            got.append(launch)
            launch.handle.close_commands()
            raise Crash()
    with pytest.raises(Crash):
        env.launch(env.kit.argv(started=up, wait_abort=True), pause=pause)
    [launch] = got
    assert env.ids() == [launch.launch_id] and env.groups.state(launch.token) is None
    assert shim_exit(launch.handle) == groupshim.EXIT_NOGO and ("NOGO", "eof") in launch.handle.events
    assert launch.handle.empty() and not os.path.exists(up)
    assert env.release(launch) and env.ids() == [] and env.groups.entries == {}


@pytest.mark.parametrize("how", ["failed", "ambiguous"])
def test_launch_an_ambiguous_or_failed_go_retains_the_record_and_the_reservation(env, monkeypatch, how):
    """GO fails before any byte (a zero timeout), or is AMBIGUOUS (the whole frame was written, then the write
    reported a failure: the CLI did start). Either way the record and the reservation are retained (STOPPING: the
    checkout admits nothing) and the command writer is closed, so the shim cleans up on EOF (never started, or its
    CLI stopped). The release by the proof then takes both."""
    up = env.kit.path("up")
    kw = {}
    if how == "failed":
        kw["go_timeout"] = 0
    else:
        real = ShimChild.go

        def go(self, *a, **k):
            real(self, *a, **k)
            raise TimeoutError("injected after the whole frame")
        monkeypatch.setattr(ShimChild, "go", go)
    launch = env.launch(env.kit.argv(started=up, wait_abort=True), **kw)
    assert launch.outcome == shimlaunch.GO_FAILED and isinstance(launch.error, TimeoutError)
    assert env.ids() == [launch.launch_id] and env.groups.state(launch.token) == STOPPING
    assert env.groups.blocked(str(env.kit.work)) and launch.handle._cmd is None
    status = shim_exit(launch.handle)
    if how == "failed":
        assert status == groupshim.EXIT_NOGO and not os.path.exists(up)
    else:                                       # (GO arrived: the shim forked; EOF stopped it, often before it was up)
        assert status == groupshim.EXIT_EMPTY and any(e[0] == "LEADER_EXIT" for e in launch.handle.events), \
            launch.handle.events
    assert env.release(launch) and env.ids() == [] and env.groups.entries == {}


# ---------------------------------------------------------------- release: only on the two kinds of evidence


def test_release_takes_the_record_then_the_reservation_and_only_on_the_proof(env):
    """While the CLI runs there is no evidence: nothing is released (the entry becomes HELD; the record stays). After
    its whole life the release pair holds: at an injected pause between the two steps the record is already removed
    while the reservation is still installed (fenced); then the reservation is gone and the checkout admits again."""
    go_on = env.kit.path("go-on")
    launch = env.launch(env.kit.argv(until=go_on))
    assert wait_event(launch.handle, "STARTED")
    assert not env.release(launch)
    assert env.groups.state(launch.token) == HELD and env.ids() == [launch.launch_id]
    Path(go_on).write_text("")
    assert shim_exit(launch.handle) == groupshim.EXIT_EMPTY
    between = []
    assert env.release(launch, pause=lambda name, la: between.append((name, env.ids(), env.groups.state(la.token))))
    assert between == [("record-removed", [], HELD)], between
    assert env.groups.entries == {} and not env.groups.blocked(str(env.kit.work))


def test_release_by_refused(env):
    """The shim exits 2 before any fork (its boot identity cannot be read: test gate `fail-boot`): `refused()`,
    nothing ever started. That is evidence: the release removes the record and the reservation."""
    launch = env.launch(env.kit.argv(wait_abort=True), gate_files=("fail-boot",))
    assert shim_exit(launch.handle) == groupshim.EXIT_USAGE
    assert launch.handle.refused() and not launch.handle.empty()
    assert shimlaunch.classify(launch.handle) == shimlaunch.REFUSED
    assert env.release(launch) and env.ids() == [] and env.groups.entries == {}


@pytest.mark.parametrize("how", ["lost", "unknown"])
def test_nothing_is_released_on_lost_or_unknown(env, how):
    """LOST (the shim killed through its own handle: the anchor is gone without a proof) and UNKNOWN (another waiter
    reaped the shim: no status was observed): no evidence, nothing released, the entry HELD, the record kept."""
    if how == "lost":
        launch = env.launch(env.kit.argv(wait_abort=True))
        assert wait_event(launch.handle, "STARTED")
        launch.handle._kill_shim()
        assert shim_exit(launch.handle) == -signal.SIGKILL
        assert shimlaunch.classify(launch.handle) == shimlaunch.LOST
    else:
        launch = env.launch(["/bin/sh", "-c", "exit 0"])
        assert wait_for(lambda: groupshim.waitid_nowait(launch.handle.pid) == 0)
        os.waitpid(launch.handle.pid, 0)        # (another waiter: this test, on its own child)
        assert launch.handle.shim_poll() == STATUS_UNKNOWN
        assert shimlaunch.classify(launch.handle) == shimlaunch.UNKNOWN
    for _ in range(2):
        assert not env.release(launch)
        assert env.groups.state(launch.token) == HELD and env.ids() == [launch.launch_id]
    Path(env.kit.abort).write_text("1")         # (an orphaned CLI stops by itself)


def test_a_cli_that_exits_while_a_member_of_its_group_survives_is_not_released_until_the_member_is_gone(env):
    member_go = env.kit.path("member-go")
    launch = env.launch(env.kit.argv(member={"until": member_go}))
    assert wait_for(lambda: launch.handle.poll() is not None), "the CLI exited"
    for _ in range(3):
        assert not env.release(launch), "a member survives: no proof"
        assert env.groups.state(launch.token) == HELD and env.ids() == [launch.launch_id]
    Path(member_go).write_text("")
    assert shim_exit(launch.handle) == groupshim.EXIT_EMPTY
    assert env.release(launch) and env.ids() == [] and env.groups.entries == {}


# ---------------------------------------------------------------- commands: QUEUED, PENDING, CLOSED


def test_a_full_command_pipe_is_pending_and_a_later_retry_is_queued(env):
    """The shim held at its register gate does not read commands; NOPs fill its pipe: TERM is PENDING (nothing
    raised). Once the shim reads again a retry is QUEUED, and the TERM reaches the group."""
    launch = env.launch(env.kit.argv(wait_abort=True), numbered=True)
    h = launch.handle
    gate_reach(h, 1)
    with pytest.raises(BlockingIOError):
        for _ in range(100000):
            os.write(h._cmd, b"NOP\n")
    assert shimlaunch.request_term(h) == shimlaunch.PENDING
    assert shimlaunch.request_kill(h) == shimlaunch.PENDING
    gate_open(h, 1)
    results = []

    def queued():
        h.poll()
        results.append(shimlaunch.request_term(h))
        return results[-1] == shimlaunch.QUEUED
    assert wait_for(queued, WAIT, 0.02), results[-5:]
    assert set(results) <= {shimlaunch.PENDING, shimlaunch.QUEUED}
    for n in range(2, 4):
        gate_open(h, n)                         # (the shim's TERM gate(s): open whatever it reaches)
    assert wait_for(lambda: (h.poll(), any(e[:2] == ("SENT", "TERM") for e in h.events))[1]), h.events


def test_a_closed_command_pipe_is_closed_and_classification_finds_the_proof(env):
    """The CLI ended by itself and the shim published its proof and exited: the command pipe is broken. TERM and KILL
    are CLOSED, never an exception; classifying through the handle finds the proof (a normal exit can race a write)."""
    launch = env.launch(["/bin/sh", "-c", "exit 0"])
    assert shim_exit(launch.handle) == groupshim.EXIT_EMPTY
    assert shimlaunch.request_term(launch.handle) == shimlaunch.CLOSED
    assert shimlaunch.request_kill(launch.handle) == shimlaunch.CLOSED
    assert shimlaunch.classify(launch.handle) == shimlaunch.PROVED
    assert env.release(launch)


def test_a_launch_into_a_blocked_checkout_starts_nothing(env):
    """A checkout already fenced (a HELD reservation): nothing is started, committed or reserved."""
    env.groups.restore_held((node_groups.SUPERVISED, "0" * 32), [node_groups.key(str(env.kit.work))],
                            str(env.kit.work))
    launch = env.launch(["/bin/sh", "-c", "exit 0"])
    assert launch.outcome == shimlaunch.NOT_ADMITTED and launch.handle is None and env.ids() == []
