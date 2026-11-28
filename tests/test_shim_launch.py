"""Ownership C3-2b-1, C: the supervised launch's lifecycle helpers (agent_comms/shimlaunch.py), UNREACHABLE from any
config in this PR (tests/test_shim_static.py pins that). DESIGN_NOTES "Ownership C3-2b-1" and its "review round 1".

REAL processes and REAL signals (macOS): the shims are this test's own children, started by `shimlaunch.launch`; the
CLIs are tests/test_groupshim.py's stand-in. Nothing is signalled but through its own handle (and by the shims, which
signal only their own groups). Every wait is bounded; every shim is stopped and reaped through its handle at the end
(test_groupshim's `Kit.cleanup`)."""
from __future__ import annotations

import os
import signal
import stat
import sys
import threading
import time
from pathlib import Path

import pytest

if sys.platform != "darwin":
    pytest.skip("the shim is macOS-only (kqueue, libproc)", allow_module_level=True)

from agent_comms import groupshim, node_groups, shimchild, shimjournal, shimlaunch   # noqa: E402
from agent_comms.shimchild import STATUS_UNKNOWN, ShimChild   # noqa: E402

from test_groupshim import (ENV, WAIT, Kit, gate_open, gate_reach, kinfo, read_json, shim_exit, wait_event,  # noqa
                            wait_for)

HELD, LIVE, STOPPING, FENCED, PROVISIONAL = (node_groups.Groups.HELD, node_groups.Groups.LIVE,
                                             node_groups.Groups.STOPPING, node_groups.Groups.FENCED,
                                             node_groups.Groups.PROVISIONAL)
RELEASED, ALREADY, KEPT = shimlaunch.RELEASED, shimlaunch.ALREADY_RELEASED, shimlaunch.KEPT


class Crash(BaseException):
    """An interrupt at that point (the node's SIGTERM handler raises KeyboardInterrupt on the main thread): a
    BaseException, so only the launch's own handlers run after it."""


class Env:
    """One node's registry, journal, owners and marker directory, around test_groupshim's Kit (its stand-in and
    cleanup)."""

    def __init__(self, kit: Kit, groups: node_groups.Groups | None = None):
        self.kit = kit
        self.groups = groups or node_groups.Groups()
        self.journal = shimjournal.Journal(kit.tmp / "logs" / shimjournal.JOURNAL_DIR)
        self.owners = shimlaunch.Owners()
        self.markers = shimchild.make_marker_dir(str(kit.tmp / "markers"))
        self.n = 0

    @property
    def cwd(self) -> str:
        return str(self.kit.work)

    def track(self, launch, gate):
        h = launch.handle
        if h is not None and h not in self.kit.children:
            h.gate_dir = gate
            self.kit.children.append(h)

    def launch(self, argv, grace=1.0, gate_files=(), numbered=False, pause=None, cwd=None, **kw):
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
        try:
            launch = shimlaunch.launch(groups=self.groups, journal=self.journal, owners=self.owners,
                                       marker_dir=self.markers, cwd=cwd or self.cwd, argv=argv, env=ENV,
                                       kill_grace=grace,
                                       log=str(self.kit.tmp / f"shim{self.n}.log"),
                                       stderr_log=str(self.kit.tmp / f"stderr{self.n}.log"),
                                       test_gate=None if gate is None else str(gate), pause=hook, **kw)
        finally:
            for parked in self.owners.parked():
                self.track(parked, gate)
        self.track(launch, gate)
        return launch

    def ids(self):
        return [r.launch_id for r in self.journal.load()]

    def release(self, launch, pause=None):
        return shimlaunch.release(self.groups, self.journal, self.owners, launch, pause=pause)

    def fenced(self, cwd=None) -> bool:
        """The checkout admits nothing: `blocked`, and a direct start is refused with nothing spawned."""
        cwd, spawned = cwd or self.cwd, []
        refused = self.groups.start(cwd, ("run", "probe"), lambda: spawned.append(1)) == (False, None)
        if not refused:
            self.groups._gone(("run", "probe"))     # (the probe's own entry, if it was admitted)
        return self.groups.blocked(cwd) and refused and not spawned


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
    return not_our_child(h.pid)


def not_our_child(pid: int) -> bool:
    """`pid` is no process (live or zombie) whose parent is this one: looked at with sysctl (`kinfo`), which answers
    for a zombie too and NEVER reaps (a `waitpid` here would itself reap an unreaped shim, and hide the defect)."""
    k = kinfo(pid)
    return k is None or k[1] != os.getpid()


def within(fn, limit: float = 1.0):
    """`fn()` on another thread: (whether it returned within `limit` seconds, what it returned)."""
    got = []
    t = threading.Thread(target=lambda: got.append(fn()), daemon=True)
    t.start()
    t.join(limit)
    return (not t.is_alive(), got[0] if got else None)


def settled_b(env, launch) -> None:
    """Row (b) of the launch-state table: committed and fenced, the command writer closed, the owner parked; the shim
    then exits by itself on EOF, and `release` on its proof takes the record and the entry, once."""
    assert env.ids() == [launch.launch_id], "the record is kept"
    assert env.groups.state(launch.token) in (HELD, STOPPING) and env.fenced(), env.groups.state(launch.token)
    assert launch.handle._cmd is None, "the command writer is closed: the shim cleans up on EOF"
    assert launch in env.owners.parked(), "its owner is parked where a later release finds it"
    assert launch.lifecycle == shimlaunch.Launch.ABANDONED, "(R6) eligible for cleanup"
    assert launch.accept(env.groups) is False and env.fenced(), "(R5) never accepted"
    assert shim_exit(launch.handle) in (groupshim.EXIT_NOGO, groupshim.EXIT_EMPTY), launch.handle.events
    got = env.release(launch)
    assert got == RELEASED and released_means_gone(env, launch, got) and env.release(launch) == ALREADY
    assert env.ids() == [] and env.groups.entries == {} and env.owners.parked() == []


# ---------------------------------------------------------------- launch: before the commit, nothing is left (a)


@pytest.mark.parametrize("step", ["shim-start", "send-secret", "spawn-handoff", "after-start", "commit-before-create"])
def test_launch_a_failure_before_the_commit_leaves_no_record_no_entry_and_no_unreaped_shim(env, monkeypatch, step):
    """Row (a): inject a failure or an interrupt at each step before the durable commit is entered or creates
    anything: the start of the shim itself (no boot identity: nothing is spawned), an interrupt while its secret is
    sent (the handle exists but `start` has not returned), an interrupt between the spawn and its handle
    (`ShimChild._launch` makes one for it), right after the shim started, and a commit that fails before it creates
    its file (the journal's parent cannot be synced). The launch raises; no record, no registry entry, no owner left
    parked, the CLI never started, and a spawned shim is reaped (it got EOF before GO, or before its secret)."""
    up = env.kit.path("up")
    seen = []

    def pause(name, launch):
        if step == "after-start" and name == "started":
            raise OSError(5, "injected")
    if step == "shim-start":
        monkeypatch.setattr(shimchild, "boot_id", lambda: None)
    if step == "send-secret":
        real_send = ShimChild._send_secret

        def send(self):
            seen.append(self)
            real_send(self)
            raise Crash()
        monkeypatch.setattr(ShimChild, "_send_secret", send)
    if step == "spawn-handoff":
        real_init, cut = ShimChild.__init__, []

        def init(self, *a, **k):
            real_init(self, *a, **k)
            seen.append(self)
            if not cut:
                cut.append(True)
                raise Crash()       # (the spawn exists; the handle was never bound)
        monkeypatch.setattr(ShimChild, "__init__", init)
    if step == "commit-before-create":
        real = shimjournal._sync_dir

        def sync_dir(path):
            if path == os.path.dirname(env.journal.directory):
                raise OSError(5, "injected EIO")
            real(path)
        monkeypatch.setattr(shimjournal, "_sync_dir", sync_dir)
    try:
        with pytest.raises((OSError, Crash)):
            env.launch(env.kit.argv(started=up, wait_abort=True), pause=pause)
    finally:
        owner = seen[-1] if step == "send-secret" or len(seen) == 2 else None   # (the handle owning the pipes)
        if owner is not None and owner not in env.kit.children:
            owner.gate_dir = None
            env.kit.children.append(owner)      # (its pipes closed at the end, through it)
    assert env.ids() == [] and env.groups.entries == {} and env.groups.held == set()
    assert env.owners.parked() == [], "nothing exists: no owner is left"
    if step == "shim-start":
        assert not env.kit.children, "nothing was spawned"
    elif step == "spawn-handoff":
        pid = seen[0].pid
        assert wait_for(lambda: not_our_child(pid)), "the spawn was reaped (by the handle made for it)"
    else:
        h = env.kit.children[-1] if step != "send-secret" else seen[0]
        assert reaped_by_its_handle(h), (h.shim_poll(), h.events)
        assert h.shim_poll() in (groupshim.EXIT_NOGO, groupshim.EXIT_NOSECRET), (h.shim_poll(), h.events)
    assert not os.path.exists(up), "the CLI never started"


def test_launch_no_registry_entry_exists_before_the_durable_commit(env):
    """At injected pauses: right after the shim started there is no record and no entry; right after the commit the
    record is durable and there is STILL no entry; once admitted, and while GO is written, the entry is PROVISIONAL
    (it fences); it is STILL PROVISIONAL when the launch ends STARTED and is handed to its caller (not parked,
    HANDED_OFF), and LIVE only once the receiver `accept()`s it (review round 2, R5). Its whole life ends in a release
    by the proof."""
    at = {}

    def pause(name, launch):
        at[name] = (env.ids(), env.groups.state(launch.token))
    launch = env.launch(["/bin/sh", "-c", "exit 0"], pause=pause)
    assert launch.outcome == shimlaunch.STARTED and env.owners.parked() == []
    lid = launch.launch_id
    assert at["started"] == ([], None)
    assert at["committed"] == ([lid], None), "the durable record, and no registry entry yet"
    assert at["admitted"] == at["go"] == ([lid], PROVISIONAL), "provisional, fencing, before GO"
    assert at["went"] == at["handed"] == ([lid], PROVISIONAL)
    assert env.groups.state(launch.token) == PROVISIONAL, "returned still fencing"
    assert launch.accept(env.groups) is True and env.groups.state(launch.token) == LIVE
    assert shim_exit(launch.handle) == groupshim.EXIT_EMPTY
    assert env.release(launch) == RELEASED and env.ids() == [] and env.groups.entries == {}


# ---------------------------------------------------------------- launch: after the commit, fenced and owned (b)


def test_launch_an_interrupt_after_the_commit_and_before_the_registry_step_is_fenced_and_owned(env):
    """Row (b), R4: an interrupt after the commit and before the registry step: the record stays AND a HELD entry
    fences it (before: a record with no in-memory entry), the writer is closed (the shim publishes never-started), the
    owner is parked; the release by that proof takes both. (A node that DIES here leaves the record only: the next
    start restores it HELD, tests/test_shim_restore.py.)"""
    up, got = env.kit.path("up"), []

    def pause(name, launch):
        if name == "committed":
            got.append(launch)
            raise Crash()
    with pytest.raises(Crash):
        env.launch(env.kit.argv(started=up, wait_abort=True), pause=pause)
    [launch] = got
    assert env.groups.state(launch.token) == HELD and env.fenced(), env.groups.state(launch.token)
    assert env.owners.parked() == [launch]
    assert shim_exit(launch.handle) == groupshim.EXIT_NOGO and ("NOGO", "eof") in launch.handle.events
    settled_b(env, launch)
    assert not os.path.exists(up)


@pytest.mark.parametrize("where", ["file-sync", "dir-sync"])
def test_launch_a_commit_that_fails_after_its_file_exists_is_fenced_and_owned(env, monkeypatch, where):
    """Rows (b), R3: the commit fails after it created its file (the file's sync, or the directory's): the commit
    removes nothing and raises; the launch never writes GO, and the file it may have left is fenced (HELD) with its
    owner parked. The release by the shim's never-started proof is the one unlink."""
    up, got = env.kit.path("up"), []
    env.journal.make()                          # (the directory and its parent's sync: this commit's own syncs fail)
    real = shimjournal._fullsync

    def sync(fd):
        if (where == "file-sync") == stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(5, "injected EIO")
        real(fd)
    monkeypatch.setattr(shimjournal, "_fullsync", sync)
    with pytest.raises(OSError):
        env.launch(env.kit.argv(started=up, wait_abort=True), pause=lambda name, la: got.append(la))
    monkeypatch.undo()
    launch = got[0]
    assert env.ids() == [launch.launch_id], "the failed commit removed nothing"
    assert env.groups.state(launch.token) == HELD and env.fenced(), env.groups.state(launch.token)
    assert env.owners.parked() == [launch]
    assert shim_exit(launch.handle) == groupshim.EXIT_NOGO and not launch.handle.went
    settled_b(env, launch)
    assert not os.path.exists(up)


def test_launch_refused_at_the_atomic_check_after_the_commit_is_held_and_owned(env):
    """Row (b): another fence appears between the first look and the admission (after the commit): no GO, the writer
    closed, the record kept with a HELD reservation, the owner parked (and returned): REFUSED_AFTER_COMMIT."""
    other = (node_groups.SUPERVISED, "f" * 32)

    def pause(name, launch):
        if name == "committed":
            env.groups.restore_held(other, [launch.key], env.cwd)
    launch = env.launch(env.kit.argv(wait_abort=True), pause=pause)
    assert launch.outcome == shimlaunch.REFUSED_AFTER_COMMIT and not launch.handle.went
    assert env.groups.state(launch.token) == HELD
    env.groups._gone(other)
    settled_b(env, launch)


@pytest.mark.parametrize("how", ["failed", "ambiguous"])
def test_launch_an_ambiguous_or_failed_go_retains_the_record_and_the_reservation(env, monkeypatch, how):
    """GO fails before any byte (a zero timeout), or is AMBIGUOUS (the whole frame was written, then the write
    reported a failure: the CLI did start). Either way the record and the reservation are retained (STOPPING: the
    checkout admits nothing), the command writer is closed, so the shim cleans up on EOF (never started, or its CLI
    stopped), and the owner stays parked. The release by the proof then takes both."""
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
    status = shim_exit(launch.handle)
    if how == "failed":
        assert status == groupshim.EXIT_NOGO and not os.path.exists(up)
    else:                                       # (GO arrived: the shim forked; EOF stopped it, often before it was up)
        assert status == groupshim.EXIT_EMPTY and any(e[0] == "LEADER_EXIT" for e in launch.handle.events), \
            launch.handle.events
    settled_b(env, launch)


# ---------------------------------------------------------------- R1: the provisional fence, and GO outside the lock


def test_r1_nothing_holds_the_registry_lock_while_go_is_written_and_the_checkout_is_fenced_meanwhile(env):
    """R1: the shim is stopped (SIGSTOP, through its own handle) right after the commit, so it reads nothing, and GO
    (with a 256 KiB argument, more than a pipe holds) cannot complete. While GO is being written: another checkout's
    `stopping()` and a start there return at once (nothing holds the registry lock), and this checkout is fenced by
    the PROVISIONAL entry. Once the shim runs again GO completes, and only then is the entry LIVE."""
    other = env.kit.tmp / "other-checkout"
    other.mkdir()
    stopped, out = [], {}

    def pause(name, launch):
        if name == "committed":
            launch.handle._kill_shim(signal.SIGSTOP)
            stopped.append(launch)

    def run():
        try:
            out["launch"] = env.launch(env.kit.argv(wait_abort=True, pad="x" * (256 * 1024)), pause=pause,
                                       go_timeout=WAIT)
        except BaseException as e:      # (reported below)
            out["error"] = e
    t = threading.Thread(target=run, daemon=True)
    t.start()
    try:
        assert wait_for(lambda: stopped), out
        [launch] = stopped
        assert wait_for(lambda: launch.handle._go_attempted), "GO is being written"
        done, _ = within(lambda: env.groups.stopping(("run", "elsewhere")))
        assert done, "another checkout's stopping() returned at once"
        done, got = within(lambda: env.groups.start(str(other), ("run", "o"), lambda: "spawned"))
        assert done and got == (True, "spawned"), got
        done, got = within(lambda: env.groups.start(env.cwd, ("run", "same"), lambda: "spawned"))
        assert done and got == (False, None), "this checkout is fenced while GO is written"
        done, got = within(lambda: env.groups.state(launch.token))
        assert done and got == PROVISIONAL, got
        assert t.is_alive() and not launch.handle.went
    finally:
        for la in stopped:
            la.handle._kill_shim(signal.SIGCONT)
    t.join(WAIT)
    assert "error" not in out, out
    assert out["launch"].outcome == shimlaunch.STARTED and env.groups.state(out["launch"].token) == PROVISIONAL
    assert out["launch"].accept(env.groups) and env.groups.state(out["launch"].token) == LIVE


class CutRegistry(node_groups.Groups):
    """The registry with an interrupt injected inside its own admission or LIVE transition (before or after the
    change), after `probe()` looked at the registry from the launching thread."""

    def __init__(self):
        super().__init__()
        self.at, self.probe = None, None

    def admit_provisional(self, *a, **k):
        if self.at == "admit-before":
            raise Crash()
        got = super().admit_provisional(*a, **k)
        if self.at == "admit-after":
            self.probe()
            raise Crash()
        return got

    def confirm_live(self, *a, **k):
        if self.at == "live-before":
            self.probe()
            raise Crash()
        if self.at == "live-inside":            # (under the registry lock, before the change)
            with self.lock:
                self.probe()
                raise Crash()
        got = super().confirm_live(*a, **k)
        if self.at == "live-after":             # (the registry's lock released, the LIVE transition committed)
            self.probe()
            raise Crash()
        return got


# Every step of `launch` from the provisional install on (the registry's own code on both sides of it), to the last
# line before the return: after the owner was handed over (unparked, HANDED_OFF), an interrupt parks it again, (b).
# (Review round 2, R5: `launch` makes nothing LIVE; the points from its return to `accept` are test_r5_until_...)
POINTS = ["admit-before", "admit-after", "admitted", "go", "last-byte", "went", "handed"]
FENCED_AT = {"admit-after", "admitted", "go", "last-byte", "went", "handed"}


@pytest.mark.parametrize("point", POINTS)
def test_r1_an_interrupt_at_every_step_from_the_provisional_install_to_live_keeps_the_entry_fencing(kit, monkeypatch,
                                                                                                   point):
    """R1 (and R5): an interrupt injected at each step of `launch` from the provisional install to its return,
    including "the last GO byte was written, then an interrupt", inside the registry's own code, and after the hand-off.
    At each point the entry exists and fences: another start for the checkout is refused (`probe`, from the launching
    thread itself). After the interrupt: the entry still fences (HELD, never dropped), the record stays, the writer is
    closed (the shim cleans up on EOF: never started, or its CLI stopped), the owner is parked; release on the proof."""
    groups = CutRegistry()
    env = Env(kit, groups)
    probed, got = [], []

    def probe():
        probed.append((point, env.fenced()))
    groups.probe = probe
    if point in ("admit-before", "admit-after", "live-before", "live-after"):
        groups.at = point
    if point == "last-byte":
        real = ShimChild.go

        def go(self, *a, **k):
            real(self, *a, **k)
            assert self.went
            probe()
            raise Crash()
        monkeypatch.setattr(ShimChild, "go", go)

    def pause(name, launch):
        got.append(launch)
        if name == point:
            if point in FENCED_AT:
                probe()
            raise Crash()
    crashed = False
    try:
        env.launch(env.kit.argv(wait_abort=True), pause=pause)
    except Crash:
        crashed = True
    assert probed == ([(point, True)] if point in FENCED_AT else []), "fenced at each point: " + repr(probed)
    assert crashed
    launch = got[0]
    assert env.groups.state(launch.token) == HELD and env.fenced(), env.groups.state(launch.token)
    assert env.owners.parked() == [launch]
    settled_b(env, launch)


def test_r1_the_registrys_provisional_entry(tmp_path):
    """R1, the registry alone: `admit_provisional` refuses as a start does (a fenced checkout, closed admission) and
    otherwise installs a PROVISIONAL entry that fences (a start, and another provisional admission, are refused);
    `confirm_live` makes only a PROVISIONAL entry LIVE (after which C1's sharing rule admits a start beside it); a
    stop's bookkeeping meanwhile makes it STOPPING, and `confirm_live` then leaves it so. A supervised token only."""
    cwd = str(tmp_path)
    a, b = (node_groups.SUPERVISED, "a" * 32), (node_groups.SUPERVISED, "b" * 32)
    g = node_groups.Groups()
    with pytest.raises(ValueError):
        g.admit_provisional(cwd, ("run", "x"))
    assert g.admit_provisional(cwd, a) and g.state(a) == PROVISIONAL and g.blocked(cwd)
    assert g.start(cwd, ("run", "x"), lambda: 1) == (False, None) and not g.admit_provisional(cwd, b)
    assert g.confirm_live(a) and g.state(a) == LIVE and not g.blocked(cwd)
    assert g.start(cwd, ("run", "x"), lambda: 1) == (True, 1), "beside a LIVE entry, as C1 shares"
    assert not g.confirm_live(a) and g.state(a) == LIVE
    g2 = node_groups.Groups()
    assert g2.admit_provisional(cwd, a)
    g2.stopping(a)
    assert not g2.confirm_live(a) and g2.state(a) == STOPPING and g2.blocked(cwd)
    g3 = node_groups.Groups()
    g3.close_admission("unreadable")
    assert not g3.admit_provisional(cwd, a) and g3.entries == {}


def test_start_with_no_owner_abandons_the_shim_on_an_interrupt_while_its_secret_is_sent(kit, monkeypatch):
    """R4, the handle alone: `ShimChild.start` interrupted after the spawn, while it sends the secret, with no owner
    to hand the handle to: it abandons the shim before GO itself (reaped by its handle) before it raises."""
    seen, real = [], ShimChild._send_secret

    def send(self):
        seen.append(self)
        real(self)
        raise Crash()
    monkeypatch.setattr(ShimChild, "_send_secret", send)
    d = shimchild.make_marker_dir(str(kit.tmp / "m"))
    try:
        with pytest.raises(Crash):
            ShimChild.start(marker_dir=d, log=str(kit.tmp / "s.log"), stderr_log=str(kit.tmp / "e.log"))
    finally:
        for h in seen:
            h.gate_dir = None
            kit.children.append(h)
    [h] = seen
    assert reaped_by_its_handle(h) and h.shim_poll() == groupshim.EXIT_NOGO, (h.shim_poll(), h.events)


# ---------------------------------------------------------------- R4: the cleanup owner, and release serialised


def test_abandon_before_go_kills_a_shim_that_does_not_exit_and_reaps_it(kit):
    """`abandon_before_go`'s fallback: a shim that does not exit after its writer closed (stopped, through its own
    handle) is SIGKILLed on its own unreaped, pinned pid after `timeout`, and reaped by its handle; no CLI exists."""
    c = kit.start()
    c._kill_shim(signal.SIGSTOP)
    began = time.monotonic()
    status = c.abandon_before_go(0.5)
    took = time.monotonic() - began
    assert status == -signal.SIGKILL and reaped_by_its_handle(c) and c.cli_pid is None, (status, c.events)
    assert 0.5 <= took < WAIT, took


@pytest.mark.parametrize("how", ["no-byte", "whole"])
def test_abandon_before_go_refuses_once_any_go_byte_was_attempted(kit, how):
    """Once GO was ATTEMPTED (a zero timeout: the attempt is marked before the first byte, and none is written; or the
    whole frame), `abandon_before_go` refuses: a CLI may exist, and only the proof can say."""
    c = kit.start()
    if how == "no-byte":
        with pytest.raises(TimeoutError):
            c.go(kit.argv(wait_abort=True), cwd=str(kit.work), env=ENV, kill_grace=1.0, timeout=0)
    else:
        c.go(kit.argv(wait_abort=True), cwd=str(kit.work), env=ENV, kill_grace=1.0)
    with pytest.raises(RuntimeError):
        c.abandon_before_go(0.5)


def test_release_is_serialised_per_launch_exactly_one_caller_releases(env):
    """R4.2: two callers release the same proved launch at once: the second waits on the launch's own release lock
    while the first is between its two steps, then is told ALREADY_RELEASED; exactly one reports RELEASED. A later
    caller is told the same. Another launch's release meanwhile is not held up by it."""
    launch = env.launch(["/bin/sh", "-c", "exit 0"])
    assert shim_exit(launch.handle) == groupshim.EXIT_EMPTY
    hold, inside = threading.Event(), threading.Event()

    def pause(name, la):
        inside.set()
        assert hold.wait(WAIT)
    results = []
    first = threading.Thread(target=lambda: results.append(env.release(launch, pause=pause)), daemon=True)
    first.start()
    assert inside.wait(WAIT)
    second = threading.Thread(target=lambda: results.append(env.release(launch)), daemon=True)
    second.start()
    second.join(0.5)
    assert second.is_alive() and results == [], "the second caller waits for the first"
    hold.set()
    first.join(WAIT)
    second.join(WAIT)
    assert sorted(results) == sorted([RELEASED, ALREADY]), results
    assert env.release(launch) == ALREADY and env.ids() == [] and env.groups.entries == {}


def test_a_failed_record_removal_keeps_the_fence_and_reports_failure(env, monkeypatch):
    """R4.2: the proof holds but the record cannot be removed: nothing is lifted (HELD), the record stays, KEPT; the
    next release completes it (RELEASED), and the one after that is ALREADY_RELEASED."""
    launch = env.launch(["/bin/sh", "-c", "exit 0"])
    assert shim_exit(launch.handle) == groupshim.EXIT_EMPTY
    real = env.journal.remove

    def remove(launch_id):
        raise OSError(5, "injected EIO")
    monkeypatch.setattr(env.journal, "remove", remove)
    assert env.release(launch) == KEPT
    assert env.groups.state(launch.token) == HELD and env.fenced() and env.ids() == [launch.launch_id]
    monkeypatch.setattr(env.journal, "remove", real)
    assert env.release(launch) == RELEASED and env.release(launch) == ALREADY
    assert env.ids() == [] and env.groups.entries == {}


# ---------------------------------------------------------------- release: only on the two kinds of evidence


def test_release_takes_the_record_then_the_reservation_and_only_on_the_proof(env):
    """While the CLI runs there is no evidence: nothing is released (the entry becomes HELD; the record stays). After
    its whole life the release pair holds: at an injected pause between the two steps the record is already removed
    while the reservation is still installed (fenced); then the reservation is gone and the checkout admits again."""
    go_on = env.kit.path("go-on")
    launch = env.launch(env.kit.argv(until=go_on))
    assert wait_event(launch.handle, "STARTED")
    assert env.release(launch) == KEPT
    assert env.groups.state(launch.token) == HELD and env.ids() == [launch.launch_id]
    Path(go_on).write_text("")
    assert shim_exit(launch.handle) == groupshim.EXIT_EMPTY
    between = []
    assert env.release(launch, pause=lambda name, la: between.append((name, env.ids(), env.groups.state(la.token)))) \
        == RELEASED
    assert between == [("record-removed", [], HELD)], between
    assert env.groups.entries == {} and not env.groups.blocked(env.cwd)


def test_release_by_refused(env):
    """The shim exits 2 before any fork (its boot identity cannot be read: test gate `fail-boot`): `refused()`,
    nothing ever started. That is evidence: the release removes the record and the reservation."""
    launch = env.launch(env.kit.argv(wait_abort=True), gate_files=("fail-boot",))
    assert shim_exit(launch.handle) == groupshim.EXIT_USAGE
    assert launch.handle.refused() and not launch.handle.empty()
    assert shimlaunch.classify(launch.handle) == shimlaunch.REFUSED
    assert env.release(launch) == RELEASED and env.ids() == [] and env.groups.entries == {}


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
        assert env.release(launch) == KEPT
        assert env.groups.state(launch.token) == HELD and env.ids() == [launch.launch_id]
    Path(env.kit.abort).write_text("1")         # (an orphaned CLI stops by itself)


def test_a_cli_that_exits_while_a_member_of_its_group_survives_is_not_released_until_the_member_is_gone(env):
    member_go = env.kit.path("member-go")
    launch = env.launch(env.kit.argv(member={"until": member_go}))
    assert wait_for(lambda: launch.handle.poll() is not None), "the CLI exited"
    for _ in range(3):
        assert env.release(launch) == KEPT, "a member survives: no proof"
        assert env.groups.state(launch.token) == HELD and env.ids() == [launch.launch_id]
    Path(member_go).write_text("")
    assert shim_exit(launch.handle) == groupshim.EXIT_EMPTY
    assert env.release(launch) == RELEASED and env.ids() == [] and env.groups.entries == {}


# ---------------------------------------------------------------- review round 2: R5, the receiver accepts; R6, ACTIVE


def released_means_gone(env, launch, result) -> bool:
    """R6.2, the invariant: a launch reported RELEASED or ALREADY_RELEASED has no journal record and no registry entry
    (`exists` is an lstat: safe beside a concurrent commit, unlike `load`)."""
    if result not in (RELEASED, ALREADY):
        return True
    return not env.journal.exists(launch.launch_id) and env.groups.state(launch.token) is None


def stop_and_release(env, launch) -> None:
    """The receiver's stop (the writer closed: the shim stops its CLI), then the release on the proof, once."""
    launch.handle.close_commands()
    assert shim_exit(launch.handle) in (groupshim.EXIT_EMPTY, groupshim.EXIT_NOGO), launch.handle.events
    got = env.release(launch)
    assert got == RELEASED and released_means_gone(env, launch, got)
    assert env.release(launch) == ALREADY and not env.journal.exists(launch.launch_id)
    assert env.groups.state(launch.token) is None


def test_r5_launch_returns_with_the_entry_still_provisional_and_only_accept_makes_it_live(env):
    """R5: `launch` returns STARTED (GO confirmed, the CLI running) with the entry still PROVISIONAL: the checkout is
    fenced and a sibling start refused. Only the receiver's `accept()` makes it LIVE (True); then C1's sharing rule
    applies as for a direct start (a sibling start is admitted beside it). A second `accept()` changes nothing."""
    launch = env.launch(env.kit.argv(wait_abort=True))
    assert launch.outcome == shimlaunch.STARTED and launch.handle.went
    assert env.groups.state(launch.token) == PROVISIONAL and env.fenced(), env.groups.state(launch.token)
    assert launch.lifecycle == shimlaunch.Launch.HANDED_OFF and env.owners.parked() == []
    assert launch.accept(env.groups) is True and env.groups.state(launch.token) == LIVE
    assert env.groups.start(env.cwd, ("run", "sibling"), lambda: "spawned") == (True, "spawned"), "C1's sharing"
    env.groups._gone(("run", "sibling"))
    assert launch.accept(env.groups) is False and env.groups.state(launch.token) == LIVE
    stop_and_release(env, launch)


BEFORE_COMMIT = ["returned", "accepting", "live-before", "live-inside"]
AFTER_COMMIT = ["live-after", "accepted"]


@pytest.mark.parametrize("point", BEFORE_COMMIT + AFTER_COMMIT)
def test_r7_accept_has_two_outcomes_fenced_before_the_commit_and_live_for_good_after(kit, point):
    """R5.3 and R7: an interrupt at each point from `launch`'s return through `accept`: after the return, before
    `accept` is called (the receiver never accepts); in `accept` before `confirm_live` (`accepting`); inside it before
    the registry lock and under the lock before the change; right after the registry's unlock (the LIVE transition
    committed, `accept` has not recorded it); and after `accept` recorded the result (`accepted`). BEFORE the commit
    the checkout is fenced at the point and stays fenced (HELD after the interrupt: not LIVE, `is_live` False). FROM
    the commit on the entry is LIVE and STAYS LIVE: a sibling admitted at the point (C1's sharing) is not disturbed,
    no HELD appears, `is_live` is True; the receiver's normal stop path then stops it. (The points inside `launch`
    are `test_r1_an_interrupt_at_every_step_...`.)"""
    groups = CutRegistry()
    env = Env(kit, groups)
    probed, sibling = [], ("run", "sibling")

    def probe():
        if point in AFTER_COMMIT:
            probed.append(env.groups.start(env.cwd, sibling, lambda: "spawned") == (True, "spawned"))
        else:
            probed.append(env.fenced())
    groups.probe = probe
    launch = env.launch(env.kit.argv(wait_abort=True))
    assert launch.outcome == shimlaunch.STARTED
    if point == "returned":
        probe()
    else:
        if point in ("live-before", "live-inside", "live-after"):
            groups.at = point

        def pause(name, la):
            if name == point:
                probe()
                raise Crash()
        with pytest.raises(Crash):
            launch.accept(groups, **({"pause": pause} if point in ("accepting", "accepted") else {}))
    assert probed == [True], probed
    if point in AFTER_COMMIT:
        assert env.groups.state(launch.token) == LIVE and launch.token not in env.groups.held, \
            env.groups.state(launch.token)
        assert env.groups.state(sibling) == LIVE and launch.is_live(env.groups), "the sibling is not disturbed"
        env.groups._gone(sibling)
    else:
        assert env.groups.state(launch.token) in (PROVISIONAL, HELD) and env.fenced()
        assert env.groups.state(launch.token) == (PROVISIONAL if point == "returned" else HELD)
        assert not launch.is_live(env.groups)
    stop_and_release(env, launch)


@pytest.mark.parametrize("how", ["stopping", "held", "gone"])
def test_r5_accept_reports_a_refusal_when_the_entry_is_no_longer_provisional(env, how):
    """R5.2: the entry was fenced meanwhile (a stop marked it STOPPING; it was made HELD) or is gone: `accept()`
    answers False and changes nothing; the caller treats the launch as stopping, never as started."""
    launch = env.launch(env.kit.argv(wait_abort=True))
    if how == "stopping":
        env.groups.stopping(launch.token)
    elif how == "held":
        env.groups.restore_held(launch.token, [launch.key], env.cwd)
    else:
        env.groups._gone(launch.token)
    before = env.groups.state(launch.token)
    assert launch.accept(env.groups) is False and env.groups.state(launch.token) == before
    if how == "gone":
        env.groups.restore_held(launch.token, [launch.key], env.cwd)    # (the test's own: so release has its entry)
    stop_and_release(env, launch)


def test_r8_a_stale_sweep_crossing_acceptance_touches_nothing(env):
    """R8, real threads and a real shim: a sweeper takes its list while the launch is ACTIVE and parked (at the
    `started` pause) and then calls the abandoned-only operation on it in a loop, through the launch's hand-off and
    its `accept()`. Every answer is SKIPPED; `accept()` answers True; the entry is LIVE and stays LIVE, not HELD, and
    nothing is fenced (a sibling is admitted beside it). The receiver's own `release` then retires it on its proof."""
    stale, stop, answers, errors = [], threading.Event(), [], []

    def sweep():
        try:
            while not stop.is_set():
                for la in stale:
                    answers.append(shimlaunch.release_if_abandoned(env.groups, env.journal, env.owners, la))
        except BaseException as e:      # (reported below)
            errors.append(e)
    t = threading.Thread(target=sweep, daemon=True)

    def pause(name, la):
        if name == "started":
            stale.extend(env.owners.parked())
            t.start()
    try:
        launch = env.launch(env.kit.argv(wait_abort=True), pause=pause)
        assert stale == [launch] and launch.lifecycle == shimlaunch.Launch.HANDED_OFF
        assert launch.accept(env.groups) is True
        assert wait_for(lambda: len(answers) > 1000 or errors, WAIT)
    finally:
        stop.set()
        t.join(WAIT)
    assert errors == [] and answers and set(answers) == {shimlaunch.SKIPPED}, (errors, set(answers))
    assert env.groups.state(launch.token) == LIVE and launch.token not in env.groups.held
    assert env.groups.start(env.cwd, ("run", "sibling"), lambda: "spawned") == (True, "spawned"), "nothing fenced"
    env.groups._gone(("run", "sibling"))
    stop_and_release(env, launch)


def test_r8_the_sweep_releases_abandoned_launches_only(env):
    """R8: `sweep_abandoned` (the periodic sweep) releases each parked ABANDONED launch on its proof and touches
    nothing else; `release_if_abandoned` on a HANDED_OFF launch (accepted, LIVE) answers SKIPPED and changes nothing.
    The receiver's own `release` is the separate call for its own launch."""
    other = env.kit.tmp / "other-checkout"
    other.mkdir()
    failed = env.launch(env.kit.argv(wait_abort=True), go_timeout=0)            # GO_FAILED: ABANDONED, parked
    live = env.launch(env.kit.argv(wait_abort=True), cwd=str(other))
    assert failed.lifecycle == shimlaunch.Launch.ABANDONED and live.accept(env.groups)
    assert shim_exit(failed.handle) == groupshim.EXIT_NOGO
    assert shimlaunch.release_if_abandoned(env.groups, env.journal, env.owners, live) == shimlaunch.SKIPPED
    assert env.groups.state(live.token) == LIVE and live.token not in env.groups.held
    assert shimlaunch.sweep_abandoned(env.groups, env.journal, env.owners) == {failed.launch_id: RELEASED}
    assert released_means_gone(env, failed, RELEASED) and env.owners.parked() == []
    assert shimlaunch.sweep_abandoned(env.groups, env.journal, env.owners) == {}
    assert shimlaunch.release_if_abandoned(env.groups, env.journal, env.owners, failed) == ALREADY
    stop_and_release(env, live)


def sweeper(env, stop, seen):
    """R6: a cleanup sweep, as the node's passes will be: `release` on every parked launch, in a loop."""
    while not stop.is_set():
        for la in env.owners.parked():
            got = env.release(la)
            seen.append((la, got, released_means_gone(env, la, got)))


@pytest.mark.parametrize("ends", ["handed-off", "fails-before-the-commit"])
def test_r6_a_sweep_racing_a_launch_never_takes_an_active_one(env, ends):
    """R6, real threads and real shims: a sweeping thread calls `release` on every parked launch in a loop while this
    thread runs `launch()`, many times. An ACTIVE launch is findable but never eligible: each launch ends HANDED_OFF
    with its record and its fencing entry intact until the release on its proof (or, failing before its commit, with
    nothing left at all). Every RELEASED or ALREADY_RELEASED any caller got was for a launch with no record and no
    entry, and at most one caller per launch got RELEASED."""
    stop, seen, facts = threading.Event(), [], []
    t = threading.Thread(target=sweeper, args=(env, stop, seen), daemon=True)
    t.start()
    launches = []
    try:
        for i in range(12):
            if ends == "handed-off":
                cwd = env.kit.tmp / f"checkout-{i}"     # (its own checkout: a PROVISIONAL one fences its own)
                cwd.mkdir()
                launch = env.launch(env.kit.argv(wait_abort=True), cwd=str(cwd))
                assert launch.outcome == shimlaunch.STARTED
                assert env.journal.exists(launch.launch_id), "the record is intact"
                facts.append((env.groups.state(launch.token), env.fenced(str(cwd)),
                              getattr(launch, "lifecycle", None)))
            else:
                got = []

                def pause(name, la):
                    got.append(la)
                    if name == "started":
                        raise OSError(5, "injected")
                with pytest.raises(OSError):
                    env.launch(env.kit.argv(wait_abort=True), pause=pause)
                launch = got[0]
                assert reaped_by_its_handle(launch.handle)
                assert not env.journal.exists(launch.launch_id) and env.groups.state(launch.token) is None
            launches.append(launch)
    finally:
        stop.set()
        t.join(WAIT)
    for launch in launches:
        if ends == "handed-off":
            assert env.journal.exists(launch.launch_id) and env.groups.state(launch.token) is not None
            stop_and_release(env, launch)
    assert all(ok for _, _, ok in seen), [(la.launch_id, got) for la, got, ok in seen if not ok]
    for launch in launches:
        assert len([1 for la, got, _ in seen if la is launch and got == RELEASED]) <= 1
    for state, fenced, life in facts:            # (each launch as it was handed off, the sweep running)
        assert state in (PROVISIONAL, HELD) and fenced and life == "handed_off", (state, fenced, life)


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
    assert env.release(launch) == RELEASED


def test_a_launch_into_a_blocked_checkout_starts_nothing(env):
    """A checkout already fenced (a HELD reservation): nothing is started, committed, reserved or parked."""
    env.groups.restore_held((node_groups.SUPERVISED, "0" * 32), [node_groups.key(env.cwd)], env.cwd)
    launch = env.launch(["/bin/sh", "-c", "exit 0"])
    assert launch.outcome == shimlaunch.NOT_ADMITTED and launch.handle is None and env.ids() == []
    assert env.owners.parked() == []
