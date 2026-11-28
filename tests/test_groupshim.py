"""Ownership C3-1: the per-group supervisor (agent_comms/groupshim.py) and its node-side handle
(agent_comms/shimchild.py), NOT wired in. DESIGN_NOTES "Ownership C3-1: the group supervisor (unwired)".

Most of these tests use REAL processes and REAL signals on macOS (two defects of an earlier PR were hidden by fakes):
the shims are this test's own children, the CLIs and their members are stand-ins this test wrote, and nothing is
ever signalled but through its own handle (a shim's Popen, a node stand-in's Popen) or by the shims, which signal
only their own groups. Stand-ins stop by themselves: on a release file, on the shared `abort` file the cleanup
writes, or after their own bounded lifetime. Every wait is bounded. The decision tests at the end run the shim's
`Shim` object in this process with fake system calls (and a fake pgid that is never signalled)."""
from __future__ import annotations

import ctypes
import json
import os
import select
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from agent_comms import groupshim, node_groups, shimchild
from agent_comms.dispatch import spawn_piped
from agent_comms.shimchild import ShimChild, marker_proves

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="the shim is macOS-only (kqueue, libproc)")

PY = sys.executable
ROOT = str(Path(groupshim.__file__).resolve().parent.parent)
ENV = {"PATH": "/usr/bin:/bin", "LC_ALL": "C", "SHIM_TEST": "été ☃", "EMPTY_VALUE": ""}
SZOMB = 5
WAIT = 20.0         # the bound on any one wait below (honest headroom on a loaded machine; typical is < 1 s)

STANDIN = r'''
import json, os, signal, sys, time
spec = json.loads(sys.argv[1])
T_END = time.monotonic() + spec.get("max", 60)
ABORT = spec.get("abort")

def stop(path=None):
    return time.monotonic() >= T_END or (ABORT and os.path.exists(ABORT)) or (path and os.path.exists(path))

def until(path):
    while not stop(path):
        time.sleep(0.005)

def put(path, value):
    with open(path + ".tmp", "w") as f:
        f.write(json.dumps(value))
    os.rename(path + ".tmp", path)

def fds():
    out = []
    for fd in range(3, 1024):
        try:
            os.fstat(fd)
        except OSError:
            continue
        out.append(fd)
    return out

def dispositions():
    out = {}
    for s in signal.Signals:
        if s in (signal.SIGKILL, signal.SIGSTOP, signal.SIGPIPE, signal.SIGXFSZ, signal.SIGINT):
            continue            # (the last three: Python itself sets them at start-up, in every process)
        h = signal.getsignal(s)
        out[s.name] = "ign" if h == signal.SIG_IGN else "dfl" if h == signal.SIG_DFL else "other"
    return out

def report(path, **extra):
    put(path, dict({"pid": os.getpid(), "ppid": os.getppid(), "sid": os.getsid(0), "pgid": os.getpgid(0),
                    "cwd": os.getcwd(), "fds": fds(), "env": dict(os.environ), "signals": dispositions()}, **extra))

def heartbeat(path, until_path=None):
    while not stop(until_path):
        put(path, time.monotonic())
        time.sleep(0.01)

if spec.get("ignore_term"):
    def on_term(*_):
        if spec.get("term_file") and not os.path.exists(spec["term_file"]):
            put(spec["term_file"], time.monotonic())
    signal.signal(signal.SIGTERM, on_term)
if spec.get("started"):
    put(spec["started"], os.getpid())       # (after its TERM handler: "started" means "ready")
member = None
m = spec.get("member")
if m is not None:
    member = os.fork()
    if member == 0:
        try:
            if m.get("ignore_term"):
                signal.signal(signal.SIGTERM, signal.SIG_IGN)
            if m.get("report"):
                report(m["report"])
            if m.get("close_stdio"):
                null = os.open(os.devnull, os.O_RDWR)
                for fd in (0, 1, 2):
                    os.dup2(null, fd)
                os.close(null)
            if m.get("heartbeat"):
                heartbeat(m["heartbeat"], m.get("until"))
            else:
                until(m.get("until"))
        finally:
            os._exit(0)
if spec.get("report"):
    report(spec["report"], member=member)
if spec.get("echo"):
    data = sys.stdin.buffer.read()          # to EOF: the node's close of stdin must reach us
    sys.stdout.buffer.write(b"got %d\n" % len(data) + data + b"\nend\n")
    sys.stdout.buffer.flush()
if spec.get("stderr"):
    sys.stderr.write(spec["stderr"])
    sys.stderr.flush()
if spec.get("heartbeat"):
    heartbeat(spec["heartbeat"], spec.get("until"))
elif spec.get("until") or spec.get("wait_abort"):
    until(spec.get("until"))
if spec.get("self_signal"):
    os.kill(os.getpid(), spec["self_signal"])
    time.sleep(5)
sys.exit(spec.get("exit", 0))
'''


# ---------------------------------------------------------------- helpers

def wait_for(pred, timeout: float = WAIT, step: float = 0.01):
    deadline = time.monotonic() + timeout
    while True:
        v = pred()
        if v or time.monotonic() >= deadline:
            return v
        time.sleep(step)


_LIBC = ctypes.CDLL(None, use_errno=True)


def kinfo(pid: int):
    """(p_stat, ppid, pgid, sigignore) of `pid` from sysctl kern.proc.pid, which answers for a zombie too (libproc's
    pidinfo does not), or None when there is no such process. Offsets measured on this Mac (struct kinfo_proc, 648
    bytes): p_stat 36, p_pid 40, p_sigignore 232, e_ppid 560, e_pgid 564."""
    mib = (ctypes.c_int * 4)(1, 14, 1, pid)
    buf = ctypes.create_string_buffer(648)
    size = ctypes.c_size_t(648)
    if _LIBC.sysctl(mib, 4, buf, ctypes.byref(size), None, 0) != 0 or size.value != 648:
        return None
    raw = buf.raw
    if struct.unpack_from("<i", raw, 40)[0] != pid:
        return None
    return raw[36], struct.unpack_from("<i", raw, 560)[0], struct.unpack_from("<i", raw, 564)[0], \
        struct.unpack_from("<I", raw, 232)[0]


def read_json(path):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None


def wait_event(c: ShimChild, kind: str, timeout: float = WAIT):
    def seen():
        c.poll()
        return next((e for e in c.events if e[0] == kind), None)
    return wait_for(seen, timeout)


def shim_exit(c: ShimChild, timeout: float = WAIT):
    """The shim's exit status once it has exited (None if it has not within the bound)."""
    wait_for(lambda: c.shim_poll() is not None, timeout)
    return c.shim_poll()


def kinds(c: ShimChild) -> list[str]:
    return [e[0] for e in c.events]


def diag_times(log: Path, needle: str) -> list[float]:
    return [float(line.split(" ", 1)[0]) for line in log.read_text().splitlines() if needle in line]


def raw_write(c: ShimChild, data: bytes, timeout: float = WAIT) -> None:
    """Bytes straight into the shim's command pipe (the pre-GO and flood tests), non-blocking with a bound."""
    view, deadline = memoryview(data), time.monotonic() + timeout
    while view and time.monotonic() < deadline:
        try:
            view = view[os.write(c._cmd, view):]
        except BlockingIOError:
            shimchild.wait_writable(c._cmd, 0.05)
        except BrokenPipeError:
            return


class Kit:
    """This test's shims: each gets its own marker, log and stderr log under the test's directory; all are stopped
    and reaped through their own handles at the end."""

    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.work = tmp / "checkout"
        self.work.mkdir()
        self.abort = str(tmp / "abort")
        self.standin = tmp / "standin.py"
        self.standin.write_text(STANDIN)
        self.children: list[ShimChild] = []
        self.procs: list[subprocess.Popen] = []
        self.n = 0

    def path(self, name: str) -> str:
        return str(self.tmp / name)

    def argv(self, **spec) -> list[str]:
        spec.setdefault("abort", self.abort)
        return [PY, str(self.standin), json.dumps(spec)]

    def start(self, gate: bool = False, pauses: dict | None = None, defer: bool = False, **kw) -> ShimChild:
        """`pauses`: the child's test-only pauses ({"before-setsid" | "after-setsid": options}); `defer`: the shim reads
        the launch handshake only once `defer-handshake.go` exists. Both imply the test gate."""
        self.n += 1
        d = self.tmp / f"shim{self.n}"
        d.mkdir()
        g = None
        if gate or pauses or defer:
            g = d / "gate"
            g.mkdir()
            if gate:
                (g / "numbered").write_text("")     # (the shim's numbered gates: register, every signal)
            for name, opts in (pauses or {}).items():
                (g / f"pause.{name}").write_text(json.dumps(opts))
            if defer:
                (g / "defer-handshake").write_text("")
        kw.setdefault("stderr_log", str(d / "stderr.log"))
        kw.setdefault("log", str(d / "shim.log"))
        c = ShimChild.start(marker=str(d / "marker.json"), test_gate=None if g is None else str(g), **kw)
        c.dir, c.gate_dir = d, g
        self.children.append(c)
        return c

    def run(self, argv, grace: float = 1.0, gate: bool = False, **kw) -> ShimChild:
        c = self.start(gate=gate, **kw)
        c.go(argv, cwd=str(self.work), env=ENV, kill_grace=grace)
        return c

    def cleanup(self) -> None:
        Path(self.abort).write_text("1")
        for c in self.children:
            if c.gate_dir is not None:          # (release any gate it may be waiting at)
                for i in range(1, 64):
                    (c.gate_dir / f"go.{i}").write_text("")
                for name in ("before-setsid.go", "after-setsid.go", "defer-handshake.go", "abort"):
                    (c.gate_dir / name).write_text("")
            if c.shim_poll() is None:
                c.close_commands()              # (EOF: the shim stops its own group, then exits)
                if not wait_for(lambda: c.shim_poll() is not None, 15):
                    c.proc.kill()               # (its own handle; not yet reaped)
            if c.proc.returncode is None:
                c.proc.wait(timeout=10)
            c.close()
        for p in self.procs:
            if p.poll() is None:
                p.kill()
            p.wait(timeout=10)


@pytest.fixture
def kit(tmp_path):
    k = Kit(tmp_path)
    try:
        yield k
    finally:
        k.cleanup()


def gate_pass(c: ShimChild, n: int, timeout: float = WAIT) -> str:
    at = c.gate_dir / f"at.{n}"
    assert wait_for(at.exists, timeout), f"the shim reached gate {n}"
    text = at.read_text().strip()
    (c.gate_dir / f"go.{n}").write_text("")
    return text


def gate_reach(c: ShimChild, n: int, timeout: float = WAIT) -> str:
    at = c.gate_dir / f"at.{n}"
    assert wait_for(at.exists, timeout), f"the shim reached gate {n}"
    return at.read_text().strip()


def gate_open(c: ShimChild, n: int) -> None:
    (c.gate_dir / f"go.{n}").write_text("")


def assert_anchor(c: ShimChild) -> None:
    """The CLI's pid is a zombie whose parent is the shim and which still leads its own group: the pgid is pinned."""
    k = kinfo(c.cli_pid)
    assert k is not None and k[0] == SZOMB, f"the leader is an unreaped zombie ({k})"
    assert k[1] == c.pid and k[2] == c.cli_pid, f"its parent is the shim and it leads group {c.cli_pid} ({k})"


# ---------------------------------------------------------------- C1: exit status, never reaped early

@pytest.mark.parametrize("how,expect", [("exit", 7), ("self_signal", -signal.SIGUSR1), ("term", -signal.SIGTERM)])
def test_c1_the_leaders_exit_status_is_reported_and_it_is_not_reaped_early(kit, how, expect):
    """A normal exit, a signalled exit (its own signal) and a signalled exit by the shim's TERM: LEADER_EXIT carries
    `Popen.returncode`'s value; while a member keeps the group, the leader is still the shim's unreaped zombie (the
    pgid pinned) and no marker exists; once the member is gone, EMPTY and the marker, exit 0."""
    member_go, mrep = kit.path("member-go"), kit.path("member-report")
    spec = {"member": {"until": member_go, "close_stdio": True, "ignore_term": True, "report": mrep}}
    if how == "exit":
        spec["exit"] = 7
    elif how == "self_signal":
        spec["self_signal"] = signal.SIGUSR1
    else:
        spec["wait_abort"] = True
    c = kit.run(kit.argv(**spec))
    assert wait_event(c, "STARTED") and c.cli_pid
    if how == "term":
        assert wait_for(lambda: read_json(mrep)), "the member ignores TERM by now"
        c.terminate()
    assert wait_event(c, "LEADER_EXIT"), c.events
    assert c.poll() == expect
    assert_anchor(c)
    time.sleep(0.3)
    assert c.shim_poll() is None and not c.empty() and not os.path.exists(c.marker), "a member: no proof yet"
    Path(member_go).write_text("")
    assert shim_exit(c) == groupshim.EXIT_EMPTY, c.events
    assert c.empty() and not c.lost()
    assert kinds(c)[-1] == "EMPTY" and kinds(c).index("STARTED") < kinds(c).index("LEADER_EXIT")
    assert marker_proves(c.marker, c.nonce, c.boot)


def test_c1_a_fast_exit_is_reported(kit):
    c = kit.run(["/bin/sh", "-c", "exit 5"])
    assert shim_exit(c) == groupshim.EXIT_EMPTY
    assert c.poll() == 5 and kinds(c) == ["STARTED", "LEADER_EXIT", "EMPTY"] and c.empty()


def test_c1_an_exit_before_its_registration_is_read_without_reaping(kit):
    """The test-only gate holds the shim between the fork and the kqueue registration until the child is already a
    zombie: the registration is refused (ESRCH), and the status is read by waitid(WNOWAIT), without reaping. (Since
    review round 1 a child execs only after the shim watches it, so only a child that gives up before its release, here
    at a `chdir` into a missing directory, can exit before the registration.)"""
    c = kit.start(gate=True)
    c.go(["/bin/sh", "-c", "exit 5"], cwd=str(kit.tmp / "missing"), env=ENV, kill_grace=1.0)
    name, pid = gate_reach(c, 1).split()
    assert name == "register"
    pid = int(pid)
    assert wait_for(lambda: (kinfo(pid) or (0,))[0] == SZOMB), "the child exited before its registration"
    gate_open(c, 1)
    assert shim_exit(c) == groupshim.EXIT_EMPTY, c.events
    assert c.poll() == 127 and c.start_errno == 2 and c.cli_pid is None and c.empty()
    assert kinds(c) == ["START_FAILED", "LEADER_EXIT", "EMPTY"]
    log = (c.dir / "shim.log").read_text()
    assert "exit registration refused (errno 3)" in log and "chdir failed (errno 2)" in log


def test_c1_an_exec_failure_is_said_and_then_proved_empty(kit):
    c = kit.run(["/nonexistent/agent-cli"])
    assert shim_exit(c) == groupshim.EXIT_EMPTY, c.events
    assert c.start_errno == 2 and c.poll() == 127 and kinds(c) == ["START_FAILED", "LEADER_EXIT", "EMPTY"]
    assert c.empty() and c.cli_pid is None
    assert "exec failed (errno 2)" in (c.dir / "shim.log").read_text()


# ---------------------------------------------------------------- C2: the deterministic interleaving

def test_c2_at_the_gate_before_its_killpg_the_zombie_still_anchors_the_pgid(kit):
    """The other members exit while the shim is held between its decision (the anchor holds) and its real killpg:
    the group is then the leader's zombie alone, its parent the shim, so the pgid cannot be anyone else's when the
    signal goes. Then the proof follows."""
    member_go, cli_go, rep = kit.path("member-go"), kit.path("cli-go"), kit.path("report")
    c = kit.run(kit.argv(member={"until": member_go, "ignore_term": True, "close_stdio": True}, until=cli_go,
                         report=rep), gate=True)
    assert gate_pass(c, 1).startswith("register")
    assert wait_event(c, "STARTED") and wait_for(lambda: read_json(rep))
    member = read_json(rep)["member"]
    pg = c.cli_pid
    assert set(node_groups.group_members(pg)) == {pg, member}
    Path(cli_go).write_text("")
    assert wait_event(c, "LEADER_EXIT") and c.poll() == 0
    c.terminate()
    assert gate_reach(c, 2) == "signal TERM", "held between the decision and the killpg"
    Path(member_go).write_text("")      # (the member exits by itself: nobody signals it)
    assert wait_for(lambda: node_groups.group_members(pg) == (pg,)), "only the leader is left in the group"
    assert_anchor(c)
    assert node_groups.group_members(pg) == (pg,), "still pinned: the zombie holds the pid and so the pgid"
    gate_open(c, 2)
    assert shim_exit(c) == groupshim.EXIT_EMPTY, c.events
    assert ("SENT", "TERM", "group") in c.events or any(e[:2] == ("FAILED", "TERM") for e in c.events)
    assert kinds(c)[-1] == "EMPTY" and c.empty()
    log = (c.dir / "shim.log").read_text()
    assert f"killpg({pg}, TERM)" in log and log.index(f"killpg({pg}, TERM)") < log.index("reaped the CLI")


# ---------------------------------------------------------------- C3: the node dies

NODE = r'''
import json, os, socket, subprocess, sys, threading, time
spec = json.loads(sys.argv[1])
sys.path.insert(0, spec["root"])
from agent_comms.shimchild import ShimChild
children, errors = {}, []
barrier = threading.Barrier(len(spec["shims"]) + 1)

def one(name, s):
    try:
        barrier.wait(10)
        c = ShimChild.start(marker=s["marker"], log=s["log"], stderr_log=s["stderr"], test_gate=s.get("gate"))
        c.go(s["argv"], cwd=s["cwd"], env=s["env"], kill_grace=s["grace"])
        children[name] = c
    except BaseException as e:
        errors.append(repr(e))

noise = []
def other_children():
    # Other children of this "node", spawned the way the node spawns (subprocess's default close_fds=True, as
    # dispatch.spawn_piped and spawn_process) while the shims' pipes are made: if any held a shim's command writer,
    # that shim would not see EOF until it exited (they live 8 s). (A fork that KEEPS descriptors, close_fds=False,
    # can inherit one: macOS has no pipe2, so os.pipe() is not atomically close-on-exec. DESIGN_NOTES, C3-1's limits.)
    barrier.wait(10)
    for _ in range(12):
        noise.append(subprocess.Popen(["/bin/sleep", "8"], stdin=subprocess.DEVNULL))

threads = [threading.Thread(target=one, args=(n, s)) for n, s in spec["shims"].items()]
threads.append(threading.Thread(target=other_children))
for t in threads:
    t.start()
for t in threads:
    t.join(30)
deadline = time.monotonic() + 30
while time.monotonic() < deadline and (errors or any(c.cli_pid is None for n, c in children.items()
                                                     if spec["shims"][n].get("wait_started", True))):
    if errors:
        break
    for c in children.values():
        c.poll()
    time.sleep(0.01)
out = {"errors": errors, "noise": [p.pid for p in noise],
       "shims": {n: {"shim": c.pid, "cli": c.cli_pid, "nonce": c.nonce, "boot": c.boot} for n, c in children.items()}}
flood = spec.get("flood")
if flood and not errors:
    c = children[flood]
    sock = socket.socket(fileno=spec["sock"])
    socket.send_fds(sock, [b"e"], [os.dup(c._evt)])     # the test holds the event pipe open, and never reads it
    sent, end = 0, time.monotonic() + 15
    while sent < 30000 and time.monotonic() < end:     # each is answered UNKNOWN: far more than the pipe holds
        try:
            os.write(c._cmd, b"NOP\n")
            sent += 1
        except BlockingIOError:
            time.sleep(0.001)
    out["flooded"] = sent
with open(spec["report"] + ".tmp", "w") as f:
    json.dump(out, f)
os.rename(spec["report"] + ".tmp", spec["report"])
time.sleep(spec.get("max", 120))
'''


def node_spec(kit: Kit, shims: dict, **extra) -> dict:
    out = {"root": ROOT, "report": kit.path("node-report.json"), "shims": {}}
    for name, (argv, grace) in shims.items():
        d = kit.tmp / f"node-{name}"
        d.mkdir()
        out["shims"][name] = {"marker": str(d / "marker.json"), "log": str(d / "shim.log"),
                              "stderr": str(d / "stderr.log"), "argv": argv, "cwd": str(kit.work), "env": ENV,
                              "grace": grace}
    out.update(extra)
    return out


def start_node(kit: Kit, spec: dict, pass_fds=()) -> subprocess.Popen:
    script = kit.tmp / "node.py"
    script.write_text(NODE)
    node = subprocess.Popen([PY, str(script), json.dumps(spec)], cwd=str(kit.tmp), stdin=subprocess.DEVNULL,
                            pass_fds=pass_fds)
    kit.procs.append(node)
    return node


def gone(pid: int) -> bool:
    k = kinfo(pid)
    return k is None or k[1] != 1 and k[0] != SZOMB


def test_c3_the_node_dies_and_its_shims_stop_their_groups_with_a_sibling_and_a_closed_event_pipe(kit):
    """The test's own node stand-in starts two shims at once in two threads (and other children beside them), then
    is SIGKILLed through its own handle. (a) Its event pipes are closed with it. A: its CLI ignores TERM; its shim
    sees EOF, attempts TERM at once, KILL at TERM + its 0.5 s grace, and publishes its marker, in seconds. (c) B, the
    sibling, lives on (its CLI ignores TERM, its grace is 60 s) and so do the other children (8 s): had either held
    A's command writer, A would not have seen EOF. Then B's CLI is released and B proves its own group gone."""
    b_go, a_term, a_up, b_up = kit.path("b-go"), kit.path("a-term"), kit.path("a-up"), kit.path("b-up")
    spec = node_spec(kit, {"A": (kit.argv(ignore_term=True, term_file=a_term, started=a_up, wait_abort=True), 0.5),
                           "B": (kit.argv(ignore_term=True, started=b_up, until=b_go), 60.0)})
    node = start_node(kit, spec)
    rep = wait_for(lambda: read_json(spec["report"]), 40)
    assert rep and not rep["errors"] and all(s["cli"] for s in rep["shims"].values()), rep
    assert wait_for(lambda: os.path.exists(a_up) and os.path.exists(b_up)), "both CLIs ignore TERM by now"
    a, b = rep["shims"]["A"], rep["shims"]["B"]
    a_marker, b_marker = spec["shims"]["A"]["marker"], spec["shims"]["B"]["marker"]
    t_kill = time.monotonic()
    node.kill()                             # (its own handle)
    node.wait(timeout=10)
    assert wait_for(lambda: marker_proves(a_marker, a["nonce"], a["boot"]), WAIT), "A proved its group gone"
    took = time.monotonic() - t_kill
    assert took < 6.0, f"A saw EOF at once: nobody else held its command writer ({took:.2f} s)"
    assert os.path.exists(a_term), "A's CLI got the TERM (and ignored it)"
    a_log = Path(spec["shims"]["A"]["log"])
    assert wait_for(lambda: "exit 0" in a_log.read_text()), "A's log, written by its own thread, is complete"
    log = a_log.read_text()
    term_at = diag_times(Path(spec["shims"]["A"]["log"]), f"killpg({a['cli']}, TERM) sent")
    kill_at = diag_times(Path(spec["shims"]["A"]["log"]), f"killpg({a['cli']}, KILL) sent")
    assert term_at and kill_at and kill_at[0] - term_at[0] >= 0.5, (term_at, kill_at)
    assert "EOF on the command pipe" in log and "event channel closed" in log, "(a): events stopped, cleanup went on"
    assert not os.path.exists(b_marker) and any(not gone(p) for p in rep["noise"]), "B and the others still live"
    assert wait_for(lambda: "EOF on the command pipe" in Path(spec["shims"]["B"]["log"]).read_text())
    b_log = Path(spec["shims"]["B"]["log"]).read_text()
    assert f"killpg({b['cli']}, TERM) sent" in b_log
    assert f"killpg({b['cli']}, KILL)" not in b_log, "B's KILL waits for its own grace"
    Path(b_go).write_text("")
    assert wait_for(lambda: marker_proves(b_marker, b["nonce"], b["boot"])), "B proved its group gone"
    assert wait_for(lambda: gone(a["shim"]) and gone(b["shim"])), "both shims exited"


def test_c3_the_node_dies_with_its_event_pipe_full_and_cleanup_still_completes(kit):
    """(b) The node stand-in hands this test a copy of its shim's event read end (SCM_RIGHTS), which the test holds
    and never reads, floods the shim with commands until their answers fill the pipe, and is SIGKILLed. The shim's
    TERM, its KILL after the grace and its marker all happen with every event dropped."""
    mine, theirs = socket.socketpair()
    held = []
    try:
        c_up = kit.path("c-up")
        spec = node_spec(kit, {"C": (kit.argv(ignore_term=True, started=c_up, wait_abort=True), 0.5)}, flood="C",
                         sock=theirs.fileno())
        node = start_node(kit, spec, pass_fds=(theirs.fileno(),))
        theirs.close()
        mine.settimeout(40)
        _, fds, _, _ = socket.recv_fds(mine, 16, 1)
        held.extend(fds)
        rep = wait_for(lambda: read_json(spec["report"]), 40)
        assert rep and not rep["errors"] and rep.get("flooded", 0) > 1000, rep
        c = rep["shims"]["C"]
        assert wait_for(lambda: os.path.exists(c_up)), "its CLI ignores TERM by now"
        log_path = Path(spec["shims"]["C"]["log"])
        assert wait_for(lambda: "event dropped: the event pipe is full" in log_path.read_text()), "the pipe is full"
        pending = ctypes.c_int()
        import fcntl
        import termios
        fcntl.ioctl(held[0], termios.FIONREAD, pending)
        assert pending.value >= 16384, f"the event pipe holds {pending.value} unread bytes"
        node.kill()
        node.wait(timeout=10)
        assert wait_for(lambda: marker_proves(spec["shims"]["C"]["marker"], c["nonce"], c["boot"])), \
            "the marker, with the event pipe full"
        assert wait_for(lambda: gone(c["shim"]))
        log = log_path.read_text()
        assert f"killpg({c['cli']}, TERM)" in log and f"killpg({c['cli']}, KILL) sent" in log
        dropped = [line for line in log.splitlines() if "events dropped:" in line]
        assert dropped and int(dropped[-1].split("after EOF: ")[1].rstrip(")")) > 0, dropped
    finally:
        mine.close()
        for fd in held:
            os.close(fd)


# ---------------------------------------------------------------- C4: the pre-GO window

def _frame(argv, cwd) -> bytes:
    body = json.dumps({"cwd": cwd, "argv": argv, "env": ENV, "kill_grace": 1.0}, separators=(",", ":"))
    return b"GO 1 " + body.encode() + b"\n"


@pytest.mark.parametrize("case,reason", [("eof", "eof"), ("terminate", "eof"), ("kill", "eof"),
                                         ("partial", "partial"), ("no_newline", "partial"),
                                         ("invalid", "invalid"), ("command", "invalid"), ("version", "version"),
                                         ("overlong", "overlong"), ("bad_field", "invalid")])
def test_c4_nothing_starts_before_a_whole_valid_go_and_the_bound_marker_says_so(kit, case, reason):
    started = kit.path("started")
    c = kit.start()
    frame = _frame(kit.argv(started=started), str(kit.work))
    if case == "eof":
        c.close_commands()
    elif case == "terminate":
        c.terminate()
    elif case == "kill":
        c.kill()
    elif case == "partial":
        raw_write(c, frame[:len(frame) // 2])
        c.close_commands()
    elif case == "no_newline":
        raw_write(c, frame[:-1])            # the whole frame but its newline, then EOF
        c.close_commands()
    elif case == "invalid":
        raw_write(c, b"GO 1 {not json\n")
    elif case == "command":
        raw_write(c, b"TERM\n")
    elif case == "version":
        raw_write(c, frame.replace(b"GO 1 ", b"GO 2 "))
    elif case == "overlong":
        raw_write(c, b"GO 1 " + b"x" * groupshim.MAX_GO)
    elif case == "bad_field":
        raw_write(c, frame.replace(b'"kill_grace":1.0', b'"kill_grace":-1'))
    assert shim_exit(c) == groupshim.EXIT_NOGO, c.events
    assert c.events == [("NOGO", reason), ("EMPTY",)] and c.nogo == reason
    assert c.empty() and not c.lost() and c.poll() is None and c.cli_pid is None
    assert read_json(c.marker)["outcome"] == "never_started"
    time.sleep(0.2)
    assert not os.path.exists(started), "the CLI never started"


# ---------------------------------------------------------------- C5: survivors, and no KILL before the grace

def test_c5_a_survivor_keeps_the_marker_back_until_it_is_gone(kit):
    hb, mrep = kit.path("member-hb"), kit.path("member-report")
    c = kit.run(kit.argv(member={"ignore_term": True, "heartbeat": hb, "close_stdio": True, "report": mrep}),
                grace=1.0)
    assert wait_event(c, "LEADER_EXIT") and c.poll() == 0
    assert wait_for(lambda: read_json(mrep)), "the member ignores TERM by now"
    c.terminate()
    assert wait_event(c, "SENT")
    for _ in range(10):
        time.sleep(0.1)
        assert c.shim_poll() is None and not os.path.exists(c.marker), "the survivor holds the proof back"
    beat = read_json(hb)
    assert beat and time.monotonic() - beat < 0.5, "the member outlived the leader and its TERM"
    c.kill()
    assert shim_exit(c) == groupshim.EXIT_EMPTY, c.events
    assert ("SENT", "KILL", "group") in c.events and c.empty()


@pytest.mark.parametrize("route", ["term_then_early_kill", "kill_without_term"])
def test_c5_no_kill_before_the_grace_after_the_first_term_attempt(kit, route):
    """An early KILL command is remembered and fires at the first TERM attempt + the grace; a KILL with no TERM makes
    the shim attempt TERM first. Measured twice: the shim's own clock (its log), and the victim's (it ignores TERM,
    notes when it got it, and beats until the KILL)."""
    term, hb = kit.path("term"), kit.path("hb")
    grace = 1.0
    c = kit.run(kit.argv(ignore_term=True, term_file=term, heartbeat=hb), grace=grace)
    assert wait_event(c, "STARTED") and wait_for(lambda: read_json(hb))
    if route == "term_then_early_kill":
        c.terminate()
    c.kill()
    assert wait_event(c, "DEFERRED"), c.events
    assert shim_exit(c) == groupshim.EXIT_EMPTY, c.events
    assert c.poll() == -signal.SIGKILL
    sent = [e for e in c.events if e[0] in ("SENT", "FAILED", "DEFERRED")]
    assert sent[0] == ("SENT", "TERM", "group") and sent[1][:2] == ("DEFERRED", "KILL") \
        and sent[2] == ("SENT", "KILL", "group"), sent
    assert 0 < int(sent[1][2]) <= grace * 1000
    log = c.dir / "shim.log"
    t_term = diag_times(log, f"killpg({c.cli_pid}, TERM)")
    t_kill = diag_times(log, f"killpg({c.cli_pid}, KILL)")
    assert len(t_term) == 1 and len(t_kill) == 1 and t_kill[0] - t_term[0] >= grace, (t_term, t_kill)
    got_term, last_beat = read_json(term), read_json(hb)
    assert last_beat - got_term >= grace - 0.3, (got_term, last_beat)


# ---------------------------------------------------------------- C6: stdio, environment, session

def _direct_and_shimmed(kit, spec_for):
    data = bytes(range(256)) * 800 + b"\x00tail"
    out = {}
    for how in ("direct", "shim"):
        rep, mrep = kit.path(f"{how}-report"), kit.path(f"{how}-member")
        argv = kit.argv(**spec_for(rep, mrep))
        err = kit.tmp / f"{how}-stderr.log"
        if how == "direct":
            child = spawn_piped(argv, cwd=str(kit.work), env=ENV, log_path=err)
            kit.procs.append(child.proc)
        else:
            child = kit.start()
            err = child.dir / "stderr.log"
            child.go(argv, cwd=str(kit.work), env=ENV, kill_grace=1.0)
        view = memoryview(data)
        lines: list[bytes] = []
        deadline = time.monotonic() + WAIT
        while view and time.monotonic() < deadline:
            view = view[child.write(view):]
            lines += child.read_lines()
            time.sleep(0.001)
        child.close_stdin()
        while not child.eof and time.monotonic() < deadline:
            lines += child.read_lines()
            time.sleep(0.005)
        out[how] = {"child": child, "lines": lines, "eof": child.eof, "err": err, "report": rep, "member": mrep}
    return data, out


def test_c6_stdio_env_and_session_are_what_a_direct_spawn_gives(kit):
    """The same stand-in, spawned directly (dispatch.spawn_piped, today's spawn) and through the shim: the bytes on
    stdout and in the stderr log, the environment, the cwd, the open descriptors (none beyond 0-2: no control fd) of
    the CLI and of its child, and the session (each CLI leads its own; its child is in it). Stdin EOF reaches the CLI
    (it reads to EOF before answering)."""
    def spec(rep, mrep):
        return {"echo": True, "stderr": "to the log ☃\n", "report": rep, "exit": 3,
                "member": {"report": mrep, "close_stdio": True}}
    data, out = _direct_and_shimmed(kit, spec)
    d, s = out["direct"], out["shim"]
    assert d["eof"] and s["eof"], "stdout EOF after the CLI's exit, both ways"
    assert s["lines"] == d["lines"] and b"\n".join(s["lines"]).startswith(b"got %d" % len(data))
    c = s["child"]
    assert wait_event(c, "LEADER_EXIT") and c.poll() == 3
    assert wait_for(lambda: d["child"].poll() is not None) and d["child"].poll() == 3
    assert Path(s["err"]).read_bytes() == Path(d["err"]).read_bytes() == "to the log ☃\n".encode()
    rd, rs = read_json(d["report"]), read_json(s["report"])
    md, ms = read_json(d["member"]), read_json(s["member"])
    assert rs["env"] == rd["env"]       # (macOS's Python adds __CF_USER_TEXT_ENCODING to its own environ: see below)
    assert rs["cwd"] == rd["cwd"] == os.path.realpath(kit.work)
    assert rs["fds"] == rd["fds"] == [] and ms["fds"] == md["fds"] == [], "no descriptor beyond stdio"
    assert rs["sid"] == rs["pgid"] == rs["pid"] == c.cli_pid and rd["sid"] == rd["pgid"] == rd["pid"]
    assert ms["sid"] == ms["pgid"] == rs["pid"] and md["sid"] == md["pgid"] == rd["pid"]
    assert rs["ppid"] == c.pid and rd["ppid"] == os.getpid()
    assert rs["signals"] == rd["signals"]
    # The exec'ed environment itself, byte for byte: /usr/bin/env, which adds nothing.
    got = {}
    for how in ("direct", "shim"):
        if how == "direct":
            child = spawn_piped(["/usr/bin/env", "-0"], cwd=str(kit.work), env=ENV, log_path=kit.tmp / "env.log")
            kit.procs.append(child.proc)
        else:
            child = kit.run(["/usr/bin/env", "-0"])
        lines: list[bytes] = []
        assert wait_for(lambda: lines.extend(child.read_lines()) or child.eof)
        got[how] = b"\n".join(lines)
    assert got["shim"] == got["direct"]
    assert sorted(got["shim"].rstrip(b"\0").split(b"\0")) == sorted(f"{k}={v}".encode() for k, v in ENV.items())


def test_c6_a_dispatched_runs_stdio_is_what_spawn_process_gives(kit):
    """`piped=False`: a dispatched run's shape. The same stand-in through dispatch.spawn_process (today) and through
    the shim: stdin is /dev/null (it reads EOF at once), stdout and stderr land in one new log, byte for byte."""
    from agent_comms.dispatch import spawn_process
    argv = kit.argv(echo=True, stderr="err line\n", exit=6)
    direct = spawn_process(argv, cwd=str(kit.work), env=ENV, log_path=kit.tmp / "run-direct.log")
    kit.procs.append(direct.proc)
    c = kit.start(piped=False)
    c.go(argv, cwd=str(kit.work), env=ENV, kill_grace=1.0)
    assert shim_exit(c) == groupshim.EXIT_EMPTY and c.poll() == 6
    assert wait_for(lambda: direct.poll() is not None) and direct.poll() == 6
    got = (c.dir / "stderr.log").read_bytes()
    assert got == (kit.tmp / "run-direct.log").read_bytes() == b"got 0\n\nend\nerr line\n"
    assert c.eof and c.bytes_read == 0
    with pytest.raises(BrokenPipeError):
        c.write(b"x")
    with pytest.raises(FileExistsError):
        kit.start(piped=False, stderr_log=str(c.dir / "stderr.log"))      # (a run's log is always a new file)


def test_c6_the_shim_keeps_no_copy_of_the_clis_stdio(kit):
    """The CLI exits at once, never reading stdin; a member (its own stdio closed) keeps the group, and so the shim,
    alive. As with a direct spawn, the node gets EOF on stdout and EPIPE on a stdin write: the shim holds neither."""
    member = {"close_stdio": True}
    direct = spawn_piped(kit.argv(member=member), cwd=str(kit.work), env=ENV, log_path=kit.tmp / "d.log")
    kit.procs.append(direct.proc)
    c = kit.run(kit.argv(member=member))
    for child in (direct, c):
        assert wait_for(lambda: child.read_lines() == [] and child.eof), "stdout EOF with the CLI gone"
        assert wait_for(lambda: child.poll() is not None) and child.poll() == 0
    assert c.shim_poll() is None and not c.empty(), "the member keeps the shim (and the proof) back"
    for child in (direct, c):
        with pytest.raises(BrokenPipeError):
            for _ in range(64):
                child.write(b"x" * 4096)


SIGNODE = r'''
import json, os, signal, subprocess, sys, time
spec = json.loads(sys.argv[1])
sys.path.insert(0, spec["root"])
from agent_comms.shimchild import ShimChild
from agent_comms.dispatch import spawn_piped
import ctypes, struct
libc = ctypes.CDLL(None, use_errno=True)
def kinfo(pid):
    mib = (ctypes.c_int * 4)(1, 14, 1, pid)
    buf = ctypes.create_string_buffer(648); size = ctypes.c_size_t(648)
    if libc.sysctl(mib, 4, buf, ctypes.byref(size), None, 0) != 0:
        return None
    r = buf.raw
    return [r[36], struct.unpack_from("<i", r, 560)[0], struct.unpack_from("<I", r, 232)[0]]
for s in (signal.SIGHUP, signal.SIGUSR2, signal.SIGCHLD):      # what this "node" ignores
    signal.signal(s, signal.SIG_IGN)
d = spec["dir"]
out = {}
def wait(pred, t=20):
    end = time.monotonic() + t
    while time.monotonic() < end:
        v = pred()
        if v:
            return v
        time.sleep(0.01)
    return pred()
# 1. a Python stand-in, directly and through a shim: its dispositions as Python sees them
direct = spawn_piped(spec["py_direct"], cwd=spec["cwd"], env=spec["env"], log_path=d + "/direct-py.log")
c1 = ShimChild.start(marker=d + "/m1.json", log=d + "/s1.log", stderr_log=d + "/c1.log")
c1.go(spec["py_shim"], cwd=spec["cwd"], env=spec["env"], kill_grace=0.5)
wait(lambda: os.path.exists(spec["rep_direct"]) and os.path.exists(spec["rep_shim"]))
# 2. /bin/sleep (it changes no disposition but SIGINFO), directly and through a shim: the kernel's ignored mask
sd = subprocess.Popen(["/bin/sleep", "30"], cwd=spec["cwd"], env=spec["env"], stdin=subprocess.PIPE,
                      stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, close_fds=True, start_new_session=True)
c2 = ShimChild.start(marker=d + "/m2.json", log=d + "/s2.log", stderr_log=d + "/c2.log")
c2.go(["/bin/sleep", "30"], cwd=spec["cwd"], env=spec["env"], kill_grace=0.5)
wait(lambda: c2.poll() is None and c2.cli_pid)
time.sleep(0.3)
out["sleep_direct"] = kinfo(sd.pid)
out["sleep_shim"] = kinfo(c2.cli_pid)
out["shim_pid"], out["shim_cli"] = c1.pid, c1.cli_pid
sd.kill()
c2.terminate()
# 3. with SIGCHLD ignored here, the shim still anchors: its CLI's zombie stays until its member is gone
wait(lambda: (c1.poll() is not None))
out["leader_exit"] = c1.poll()
out["zombie"] = kinfo(c1.cli_pid)
open(spec["member_go"], "w").close()
out["m1"] = bool(wait(lambda: os.path.exists(d + "/m1.json")))
out["m2"] = bool(wait(lambda: os.path.exists(d + "/m2.json")))
with open(spec["report"] + ".tmp", "w") as f:
    json.dump(out, f)
os.rename(spec["report"] + ".tmp", spec["report"])
'''


def test_c6_signal_dispositions_are_what_a_direct_spawn_gives_and_an_ignored_sigchld_does_not_unanchor(kit):
    """A node stand-in that ignores SIGHUP, SIGUSR2 and SIGCHLD. Its directly spawned CLI and its shimmed one see the
    same dispositions: as Python reads them (every signal Python does not itself set), and in the kernel's ignored
    mask of /bin/sleep (SIGPIPE and SIGXFSZ at their default, as Popen's restore_signals leaves them; SIGHUP ignored,
    as inherited). And the shim still anchors: it resets SIGCHLD for itself, so its CLI's zombie is kept (not
    auto-reaped) until the group is empty, and both markers are published."""
    member_go = kit.path("member-go")
    rep_d, rep_s = kit.path("rep-direct"), kit.path("rep-shim")
    spec = {"root": ROOT, "dir": str(kit.tmp), "cwd": str(kit.work), "env": ENV, "report": kit.path("sig-report"),
            "member_go": member_go, "rep_direct": rep_d, "rep_shim": rep_s,
            "py_direct": kit.argv(report=rep_d, wait_abort=True),
            "py_shim": kit.argv(report=rep_s, member={"until": member_go, "close_stdio": True}, exit=4)}
    script = kit.tmp / "signode.py"
    script.write_text(SIGNODE)
    node = subprocess.Popen([PY, str(script), json.dumps(spec)], cwd=str(kit.tmp), stdin=subprocess.DEVNULL)
    kit.procs.append(node)
    assert node.wait(timeout=60) == 0
    out = read_json(spec["report"])
    rd, rs = read_json(rep_d), read_json(rep_s)
    assert rs["signals"] == rd["signals"]
    assert rd["signals"]["SIGHUP"] == rd["signals"]["SIGUSR2"] == rd["signals"]["SIGCHLD"] == "ign"
    ign_d, ign_s = out["sleep_direct"][2], out["sleep_shim"][2]
    assert ign_s == ign_d, (hex(ign_s), hex(ign_d))
    bit = lambda s: 1 << (s - 1)        # noqa: E731
    assert not ign_s & bit(signal.SIGPIPE) and not ign_s & bit(signal.SIGXFSZ) and ign_s & bit(signal.SIGHUP)
    assert out["leader_exit"] == 4
    assert out["zombie"][0] == SZOMB and out["zombie"][1] == out["shim_pid"], out
    assert out["m1"] and out["m2"]


# ---------------------------------------------------------------- C7: the handle's unit matrix

def test_c7_command_writes_raise_when_the_pipe_is_full_closed_or_broken(kit):
    """Full: the shim is held at its register gate, not reading commands; NOPs fill the pipe and a TERM raises
    BlockingIOError. Broken: after the shim's exit a TERM raises BrokenPipeError. Closed: after close_commands()."""
    c = kit.run(kit.argv(wait_abort=True), gate=True)
    gate_reach(c, 1)
    with pytest.raises(BlockingIOError):
        for _ in range(100000):
            os.write(c._cmd, b"NOP\n")
    with pytest.raises(BlockingIOError):
        c.terminate()
    gate_open(c, 1)     # (its answers to the NOPs fill the event pipe: STARTED may be dropped, so it is not awaited)
    c.close_commands()
    with pytest.raises(BrokenPipeError):
        c.terminate()
    c2 = kit.run(["/bin/sh", "-c", "exit 0"])
    assert shim_exit(c2) == groupshim.EXIT_EMPTY
    with pytest.raises(BrokenPipeError):
        c2.kill()


def test_c7_go_waits_for_a_command_fd_above_fd_setsize(kit):
    """Found by the serial suite: a node can hold more than FD_SETSIZE (1024) descriptors, and `select` refuses such
    an fd. With this test's own descriptors pushing the pipes past 1024, a GO larger than the pipe holds (it must wait
    for the shim to read) is written, and the CLI gets that environment."""
    import resource
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    want = 1400
    if soft < want:
        if hard != resource.RLIM_INFINITY and hard < want:
            pytest.skip(f"the descriptor limit is {hard}")
        resource.setrlimit(resource.RLIMIT_NOFILE, (want, hard))
    filler = []
    try:
        null = os.open(os.devnull, os.O_RDONLY)
        filler.append(null)
        while len(filler) < 1100:
            filler.append(os.dup(null))
        c = kit.start()
        assert c._cmd > 1024 and c._evt > 1024, (c._cmd, c._evt)
        env = dict(ENV, BIG="v" * 300_000)          # (far more than the 16-64 KiB a pipe holds)
        c.go(["/bin/sh", "-c", 'test "${#BIG}" -eq 300000'], cwd=str(kit.work), env=env, kill_grace=1.0)
        assert shim_exit(c) == groupshim.EXIT_EMPTY and c.poll() == 0, c.events
    finally:
        for fd in filler:
            os.close(fd)
        if soft < want:
            resource.setrlimit(resource.RLIMIT_NOFILE, (soft, hard))


def test_c7_a_concurrent_retirement_cannot_close_the_pipe_under_a_write(kit, monkeypatch):
    """The command endpoint is pinned through each write: a close_commands() from another thread, attempted while a
    TERM is inside its write, waits for the write to finish, so the fd number cannot be closed and reused under it."""
    c = kit.run(kit.argv(wait_abort=True))
    assert wait_event(c, "STARTED")
    real, seen = os.write, {}

    def slow_write(fd, data):
        if fd == c._cmd and data == b"TERM\n":
            closer = threading.Thread(target=c.close_commands)
            closer.start()
            closer.join(0.3)
            seen["closer_waited"] = closer.is_alive()
            seen["fd_open"] = c._cmd == fd and os.fstat(fd) is not None
            seen["closer"] = closer
        return real(fd, data)
    monkeypatch.setattr(shimchild.os, "write", slow_write)
    c.terminate()
    monkeypatch.undo()
    seen["closer"].join(5)
    assert seen["closer_waited"] and seen["fd_open"], seen
    assert wait_event(c, "SENT"), "the TERM reached its own shim"
    assert c._cmd is None, "the close went through after the write"


def test_c7_a_shim_that_exits_without_a_marker_is_lost(kit):
    """The shim killed through its own handle: the anchor is lost, nothing is published. lost() only once it has
    exited (False while it runs), empty() never, poll() None (the CLI's exit was never seen)."""
    c = kit.run(kit.argv(wait_abort=True))
    assert wait_event(c, "STARTED")
    assert not c.lost() and not c.empty()
    c.proc.kill()
    assert shim_exit(c) == -signal.SIGKILL
    assert c.lost() and not c.empty() and c.poll() is None
    Path(kit.abort).write_text("1")         # (the orphaned CLI stops by itself)


def test_c7_a_marker_of_another_launch_or_boot_is_no_proof(kit, tmp_path):
    nonce, boot = "a" * 32, groupshim.boot_id()
    good = {"v": 1, "nonce": nonce, "boot": boot, "outcome": "empty", "cli_pid": 4242, "leader_exit": 0}
    p = tmp_path / "m.json"
    for bad in ({**good, "nonce": "b" * 32}, {**good, "boot": "1.000000"}, {**good, "outcome": "held"},
                {**good, "v": 2}, {k: v for k, v in good.items() if k != "nonce"}, [good]):
        p.write_text(json.dumps(bad) + "\n")
        assert not marker_proves(str(p), nonce, boot), bad
    p.write_text("{not json")
    assert not marker_proves(str(p), nonce, boot)
    real = tmp_path / "real.json"
    real.write_text(json.dumps(good) + "\n")
    p.unlink()
    p.symlink_to(real)
    assert not marker_proves(str(p), nonce, boot), "no symlink"
    assert marker_proves(str(real), nonce, boot) and not marker_proves(str(real), nonce, "")
    # and through a real handle, before its own shim publishes
    c = kit.start()
    for other in ({**good, "nonce": "c" * 32, "boot": c.boot}, {**good, "nonce": c.nonce, "boot": "1.000000"}):
        Path(c.marker).write_text(json.dumps(other))
        assert not c.empty()
    c.close_commands()
    assert shim_exit(c) == groupshim.EXIT_NOGO
    assert c.empty()


def test_c7_the_empty_event_alone_is_no_proof(kit):
    c = kit.run(["/bin/sh", "-c", "exit 0"])
    assert wait_for(lambda: c.proc.poll() is not None) and c.proc.returncode == 0   # (reaped; nothing pumped yet)
    Path(c.marker).write_text(json.dumps({"v": 1, "nonce": "x" * 32, "boot": c.boot, "outcome": "empty"}))
    assert c.shim_poll() == 0 and kinds(c) == ["STARTED", "LEADER_EXIT", "EMPTY"], "drained before the exit is said"
    assert not c.empty(), "EMPTY was said, but the marker is not this launch's"


def test_c7_a_stale_handle_against_its_successor(kit):
    """Two launches for the same group token write the same marker path; a stale handle never takes its successor's
    marker as its own proof, and its commands never reach the successor."""
    first = kit.run(kit.argv(wait_abort=True))
    assert wait_event(first, "STARTED")
    second = ShimChild.start(marker=first.marker, log=str(first.dir / "second.log"),
                             stderr_log=str(first.dir / "second-err.log"))
    second.dir, second.gate_dir = first.dir, None
    kit.children.append(second)
    first.proc.kill()                       # (the first shim: lost, no marker; its CLI stops at `abort`)
    assert shim_exit(first) == -signal.SIGKILL
    second.go(["/bin/sh", "-c", "exit 0"], cwd=str(kit.work), env=ENV, kill_grace=1.0)
    assert shim_exit(second) == groupshim.EXIT_EMPTY
    assert second.empty() and not first.empty() and first.lost()
    with pytest.raises(BrokenPipeError):
        first.terminate()
    assert not any(e[0] in ("SENT", "FAILED", "REFUSED") for e in second.events)
    Path(kit.abort).write_text("1")


# ---------------------------------------------------------------- the shim's decisions, in this process

class Fakes:
    def __init__(self, shim: groupshim.Shim, members):
        self.calls: list[tuple] = []
        self.members = list(members)
        self.now = 100.0
        shim.members = self._members
        shim.killpg = self._killpg
        shim.kill = self._kill
        shim.waitpid = self._waitpid
        shim.waitid = lambda pid: 0
        shim.clock = lambda: self.now
        self.shim = shim
        self.raise_on: set = set()

    def _members(self, pgid):
        self.calls.append(("members", self.shim.latched, self.shim.reaped))
        return self.members.pop(0) if self.members else None

    def _killpg(self, pgid, sig):
        self.calls.append(("killpg", sig, self.shim.latched, self.shim.reaped))
        if sig in self.raise_on:
            raise ProcessLookupError(3, "no such process")

    def _kill(self, pid, sig):
        self.calls.append(("kill", sig, self.shim.latched, self.shim.reaped))
        if sig in self.raise_on:
            raise ProcessLookupError(3, "no such process")

    def _waitpid(self, pid, flags):
        self.calls.append(("waitpid", self.shim.latched))
        return pid, 0


class ListSink:
    """A diagnostics sink that records, in order, beside the fakes' calls (when given their list)."""

    def __init__(self, calls=None):
        self.lines, self.calls = [], calls

    def put(self, msg):
        self.lines.append(msg)
        if self.calls is not None:
            self.calls.append(("diag", msg))

    def start(self):
        pass


@pytest.fixture
def shim(tmp_path):
    cmd_r, cmd_w = os.pipe()
    evt_r, evt_w = os.pipe()
    os.set_blocking(evt_w, False)
    os.set_blocking(evt_r, False)
    s = groupshim.Shim(nonce="n" * 32, boot="1.000001", marker=str(tmp_path / "marker.json"), cmd_fd=cmd_r,
                       event_fd=evt_w, sink=ListSink())
    s.pgid, s.grace = 987654321, 1.0      # (a fake pid: the fake kill and killpg below are all that "signal" it)
    s.established = True                # (most decisions are about an established group; the launch tests set it)
    s.evt_r = evt_r
    try:
        yield s
    finally:
        for fd in (cmd_r, cmd_w, evt_r) + ((s.event_fd,) if s.event_fd is not None else ()):
            try:
                os.close(fd)
            except OSError:
                pass


def events_of(s) -> list[str]:
    try:
        return os.read(s.evt_r, 65536).decode().split()
    except BlockingIOError:
        return []


def test_unit_the_latch_comes_before_the_reap_and_absence_is_required_after_it(shim):
    f = Fakes(shim, [(shim.pgid,), ()])
    shim.leader_exited, shim.leader_code = True, 0
    assert shim._check_empty() == groupshim.EXIT_EMPTY
    assert f.calls == [("members", False, False), ("waitpid", True), ("members", True, True)]
    assert marker_proves(shim.marker, shim.nonce, shim.boot)
    assert not shim._signal(signal.SIGTERM) and not any(c[0] == "killpg" for c in f.calls), "never after the latch"


@pytest.mark.parametrize("after", [(987654321,), (4242,), None], ids=["leader-pid-reused", "member", "failed"])
def test_unit_unproved_after_the_reap(shim, after):
    f = Fakes(shim, [(shim.pgid,), after])
    shim.leader_exited = True
    assert shim._check_empty() == groupshim.EXIT_UNPROVED
    assert shim.latched and shim.reaped and not os.path.exists(shim.marker)
    assert "UNPROVED" in events_of(shim)
    assert not shim._signal(signal.SIGKILL) and not any(c[0] == "killpg" for c in f.calls)


@pytest.mark.parametrize("listing", [None, (987654321, 4242), (4242,)], ids=["failed", "with-member", "other"])
def test_unit_a_failed_or_nonempty_listing_is_never_empty(shim, listing):
    f = Fakes(shim, [listing])
    shim.leader_exited = True
    assert shim._check_empty() is None
    assert not shim.latched and not shim.reaped and shim.check_due is not None
    assert [c[0] for c in f.calls] == ["members"]


def test_unit_a_leader_not_yet_exited_is_never_empty(shim):
    f = Fakes(shim, [(shim.pgid,)])
    assert shim._check_empty() is None and f.calls == [] and not shim.latched


def test_unit_an_unconfirmed_zombie_is_never_empty(shim):
    f = Fakes(shim, [(shim.pgid,)])
    shim.waitid = lambda pid: None
    shim.leader_exited = True
    assert shim._check_empty() is None and not shim.latched and [c[0] for c in f.calls] == ["members"]


@pytest.mark.parametrize("state", ["no_cli", "latched", "reaped"])
def test_unit_no_signal_without_the_anchor(shim, state):
    f = Fakes(shim, [])
    if state == "no_cli":
        shim.pgid = None
    elif state == "latched":
        shim.latched = True
    else:
        shim.reaped = True
    assert not shim._signal(signal.SIGTERM) and not shim._signal(signal.SIGKILL)
    shim._kill_request()
    assert f.calls == [] and events_of(shim) == ["REFUSED", "TERM", "REFUSED", "KILL", "REFUSED", "TERM"]


def test_unit_kill_never_before_the_grace_and_repeated_after_eof(shim):
    f = Fakes(shim, [])
    f.raise_on = {signal.SIGTERM}           # (a TERM attempt that fails still starts the grace)
    shim._kill_request()
    assert f.calls == [("killpg", signal.SIGTERM, False, False)] and shim.first_term_at == 100.0
    assert shim.kill_due == 101.0
    f.now = 100.9
    shim.kill_due = 100.5                   # (even when the timer is due too early, the one sender refuses)
    shim._kill_now()
    assert [c for c in f.calls if c[1] == signal.SIGKILL] == [] and shim.kill_due == 101.0
    f.now = 101.0
    shim._kill_now()
    assert [c[:2] for c in f.calls].count(("killpg", signal.SIGKILL)) == 1 and shim.kill_due is None
    shim.eof = True                         # after EOF: repeated every grace
    f.now = 101.5
    shim._kill_request()
    assert [c[:2] for c in f.calls].count(("killpg", signal.SIGKILL)) == 2 and shim.kill_due == 102.5
    f.now = 102.5
    shim._kill_now()
    assert [c[:2] for c in f.calls].count(("killpg", signal.SIGKILL)) == 3 and shim.kill_due == 103.5
    assert [c[:2] for c in f.calls].count(("killpg", signal.SIGTERM)) == 1, "one TERM: the first attempt only"


def test_unit_the_go_frame_parser():
    good = {"cwd": "/x", "argv": ["a", "b"], "env": {"K": "V"}, "kill_grace": 2}
    line = lambda spec, v=1: b"GO %d " % v + json.dumps(spec).encode()      # noqa: E731
    assert groupshim.parse_go(line(good)) == {**good, "kill_grace": 2.0}
    for bad, reason in [({**good, "cwd": "rel"}, "invalid"), ({**good, "argv": []}, "invalid"),
                        ({**good, "argv": ["a\0"]}, "invalid"), ({**good, "env": {"A=B": "c"}}, "invalid"),
                        ({**good, "kill_grace": True}, "invalid"), ({**good, "kill_grace": 0}, "invalid"),
                        ({**good, "extra": 1}, "invalid"), ({k: v for k, v in good.items() if k != "env"}, "invalid")]:
        with pytest.raises(groupshim.NoGo) as e:
            groupshim.parse_go(line(bad))
        assert e.value.reason == reason, bad
    with pytest.raises(groupshim.NoGo) as e:
        groupshim.parse_go(line(good, 2))
    assert e.value.reason == "version"
    for raw in (b"GO", b"GO 1", b"TERM", b"GO x {}", b"GO 1 \xff"):
        with pytest.raises(groupshim.NoGo):
            groupshim.parse_go(raw)


def test_unit_the_exit_status_flag_is_the_one_pinned():
    assert groupshim.NOTE_EXITSTATUS == 0x04000000
    assert groupshim.decode_status(7 << 8) == 7 and groupshim.decode_status(signal.SIGKILL) == -signal.SIGKILL
    assert groupshim.boot_id() == shimchild.boot_id() and groupshim.boot_id().count(".") == 1


# ---------------------------------------------------------------- code review round 1 (Sol on ae9866f)

def child_at(c: ShimChild, name: str, timeout: float = WAIT):
    """The child's test-only pause `name` is reached: what it announced (its pid, and a member's)."""
    return wait_for(lambda: read_json(c.gate_dir / f"{name}.at"), timeout)


def bounded(fn, timeout: float = 3.0):
    """(finished within `timeout`, its value): for a call that must never wait."""
    out: dict = {}
    t = threading.Thread(target=lambda: out.setdefault("v", fn()), daemon=True)
    t.start()
    t.join(timeout)
    return not t.is_alive(), out.get("v")


def fionread(fd: int) -> int:
    import fcntl
    import termios
    n = ctypes.c_int()
    fcntl.ioctl(fd, termios.FIONREAD, n)
    return n.value


@pytest.mark.parametrize("where", ["after-setsid", "before-setsid"])
def test_r1_the_node_dies_while_its_child_is_paused_in_the_launch_and_the_shim_still_stops_it(kit, where):
    """R1: the child pauses (a test-only pause in the child) before or after its setsid(), so it never reaches its exec
    and holds the handshake open. The node stand-in dies. The shim reads the handshake in its loop, so it sees the EOF
    at once: a TERM, a KILL at the grace (the paused child ignores TERM), the proof and the marker. Before setsid() both
    go to the child's pid (no group by its id exists yet); after, to its group. (On ae9866f the shim blocks reading the
    handshake and never looks at the command pipe.)"""
    spec = node_spec(kit, {"P": (kit.argv(), 0.5)})
    gate = kit.tmp / "node-P" / "gate"
    gate.mkdir()
    (gate / f"pause.{where}").write_text(json.dumps({"ignore_term": True}))
    spec["shims"]["P"].update(gate=str(gate), wait_started=False)
    node = start_node(kit, spec)
    try:
        rep = wait_for(lambda: read_json(spec["report"]), 40)
        assert rep and not rep["errors"], rep
        child = wait_for(lambda: read_json(gate / f"{where}.at"))
        assert child, "the child paused in its launch"
        pid, p = child["pid"], rep["shims"]["P"]
        node.kill()                         # (its own handle)
        node.wait(timeout=10)
        assert wait_for(lambda: marker_proves(spec["shims"]["P"]["marker"], p["nonce"], p["boot"])), "the marker"
        log_path = Path(spec["shims"]["P"]["log"])
        assert wait_for(lambda: "exit 0" in log_path.read_text())
        call = "killpg" if where == "after-setsid" else "kill"
        t_term = diag_times(log_path, f"{call}({pid}, TERM) sent")
        t_kill = diag_times(log_path, f"{call}({pid}, KILL) sent")
        assert t_term and t_kill and t_kill[0] - t_term[0] >= 0.5, (t_term, t_kill)
        log = log_path.read_text()
        assert "EOF on the command pipe" in log and "event STARTED" not in log and "event START_FAILED 0" in log
        assert wait_for(lambda: gone(p["shim"]))
    finally:
        for name in ("abort", f"{where}.go"):
            (gate / name).write_text("")


def test_r1_a_term_sent_to_the_pid_is_replayed_to_the_group_once_established_and_the_grace_is_unchanged(kit):
    """A1: the child has done its setsid() and forked a member (a test-only pause after setsid), but the shim has not
    read its `S` yet (a test-only deferral of the handshake). A TERM then goes to the pid only and does not reach the
    member. Once the shim reads `S`, the TERM is replayed to the group (the member gets it), and a KILL asked for after
    the replay still fires at the FIRST TERM attempt + the grace, not the replay's."""
    mt, grace = kit.path("member-term"), 2.0
    c = kit.start(pauses={"after-setsid": {"ignore_term": True, "member_term_file": mt}}, defer=True)
    c.go(kit.argv(), cwd=str(kit.work), env=ENV, kill_grace=grace)
    child = child_at(c, "after-setsid")
    assert child and wait_for(lambda: os.path.exists(mt + ".ready")), "the child paused, its member is up"
    pid = child["pid"]
    c.terminate()
    assert wait_event(c, "SENT") == ("SENT", "TERM", "pid")
    time.sleep(0.3)
    assert not os.path.exists(mt), "a TERM to the pid alone does not reach the member"
    time.sleep(1.2)
    (c.gate_dir / "defer-handshake.go").write_text("")
    assert wait_for(lambda: os.path.exists(mt)), "the replay reached the member"
    assert wait_for(lambda: c.poll() is None and ("SENT", "TERM", "group") in c.events)
    c.kill()
    assert shim_exit(c) == groupshim.EXIT_EMPTY, c.events
    seq = [e[:2] if e[0] == "DEFERRED" else e for e in c.events if e[0] in ("SENT", "DEFERRED", "FAILED")]
    assert seq == [("SENT", "TERM", "pid"), ("SENT", "TERM", "group"), ("DEFERRED", "KILL"),
                   ("SENT", "KILL", "group")], seq
    log = c.dir / "shim.log"
    first = diag_times(log, f"kill({pid}, TERM) sent")
    replay = diag_times(log, f"killpg({pid}, TERM) sent")
    killed = diag_times(log, f"killpg({pid}, KILL) sent")
    assert first and replay and killed
    assert killed[0] - first[0] >= grace, "no KILL before the first TERM attempt + the grace"
    assert killed[0] < replay[0] + grace - 0.3, "the replay did not restart the grace"


def test_r1_a_kill_sent_to_the_pid_is_replayed_to_the_group_once_established(kit):
    """A1: as above, with the KILL already sent to the pid before the shim read `S`: the child dies, its member (in the
    child's group, ignoring nothing but surviving a KILL to the pid alone) keeps the proof back; once `S` is read, the
    TERM and the KILL are replayed to the group, the member dies, and the marker follows."""
    mt = kit.path("member-term")
    c = kit.start(pauses={"after-setsid": {"ignore_term": True, "member_term_file": mt}}, defer=True)
    c.go(kit.argv(), cwd=str(kit.work), env=ENV, kill_grace=0.5)
    child = child_at(c, "after-setsid")
    assert child and wait_for(lambda: os.path.exists(mt + ".ready"))
    pid, member = child["pid"], child["member"]
    c.kill()
    assert wait_event(c, "LEADER_EXIT") and c.poll() == -signal.SIGKILL
    assert [e for e in c.events if e[0] == "SENT"] == [("SENT", "TERM", "pid"), ("SENT", "KILL", "pid")]
    time.sleep(0.5)
    k = kinfo(member)
    assert k is not None and k[0] != SZOMB and k[2] == pid, "the member survived a KILL to the pid alone"
    assert c.shim_poll() is None and not c.empty(), "and keeps the proof back"
    (c.gate_dir / "defer-handshake.go").write_text("")
    assert shim_exit(c) == groupshim.EXIT_EMPTY, c.events
    assert ("SENT", "TERM", "group") in c.events and ("SENT", "KILL", "group") in c.events
    assert wait_for(lambda: (kinfo(member) or (SZOMB, 0, 0))[2] != pid or kinfo(member) is None)


def test_r2_the_marker_reader_takes_only_a_complete_record_in_a_regular_file_and_never_waits(tmp_path):
    """R2 and A5: one reader for every use. A FIFO (with no writer, or with one that supplies a matching record), a
    directory and a device are refused without waiting; so are a truncated or doubled record, a version that is
    `true`, `1.0` or `"1"`, an over-size file, another nonce or boot, and any key or type that is not exactly the
    shim's. (On ae9866f the FIFO with no writer blocks the reader, and the FIFO with a writer and `true` are proof.)"""
    nonce, boot = "a" * 32, groupshim.boot_id()
    good = {"v": 1, "nonce": nonce, "boot": boot, "outcome": "empty", "cli_pid": 4242, "leader_exit": 0}
    line = json.dumps(good).encode() + b"\n"
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    try:
        assert bounded(lambda: marker_proves(str(fifo), nonce, boot)) == (True, False), "a FIFO with no writer"
    finally:
        try:        # (a reader the old code left blocked in its open gets a writer, and returns)
            os.close(os.open(fifo, os.O_WRONLY | os.O_NONBLOCK))
        except OSError:
            pass
    fd = os.open(fifo, os.O_RDWR)
    try:
        os.write(fd, line)
        assert bounded(lambda: marker_proves(str(fifo), nonce, boot)) == (True, False), "a FIFO with a matching record"
    finally:
        os.close(fd)
    (tmp_path / "dir").mkdir()
    assert bounded(lambda: marker_proves(str(tmp_path / "dir"), nonce, boot)) == (True, False)
    assert bounded(lambda: marker_proves("/dev/null", nonce, boot)) == (True, False), "a device"
    dump = lambda rec: json.dumps(rec).encode() + b"\n"       # noqa: E731
    cases = {
        "truncated": line[:-6], "no newline": line[:-1], "two records": line + line,
        "v true": dump({**good, "v": True}), "v 1.0": dump({**good, "v": 1.0}), "v '1'": dump({**good, "v": "1"}),
        "v 2": dump({**good, "v": 2}), "other nonce": dump({**good, "nonce": "b" * 32}),
        "other boot": dump({**good, "boot": "1.000000"}), "extra key": dump({**good, "x": 1}),
        "missing key": dump({k: v for k, v in good.items() if k != "leader_exit"}),
        "outcome held": dump({**good, "outcome": "held"}), "bool pid": dump({**good, "cli_pid": True}),
        "no pid": dump({**good, "cli_pid": None}), "float exit": dump({**good, "leader_exit": 1.0}),
        "never_started with a pid": dump({**good, "outcome": "never_started"}),
        "over-size": line.replace(b",", b"," + b" " * 5000, 1), "a list": dump([good]),
    }
    p = tmp_path / "marker.json"
    for name, data in cases.items():
        p.write_bytes(data)
        assert not marker_proves(str(p), nonce, boot), name
    p.write_bytes(dump({**good, "outcome": "never_started", "cli_pid": None, "leader_exit": None}))
    assert marker_proves(str(p), nonce, boot) and not marker_proves(str(p), nonce, "")
    p.unlink()
    assert groupshim.write_marker(str(p), good) == (groupshim.PUBLISHED, 0) and marker_proves(str(p), nonce, boot)
    assert shimchild.read_marker(str(p), nonce, boot) == good


def test_r3_a_poll_only_consumer_learns_the_exit_from_the_marker_when_leader_exit_was_dropped(kit):
    """R3: the event pipe is full (unknown commands flood it, nobody reads), so the CLI's LEADER_EXIT and EMPTY are
    dropped. A consumer that only ever calls poll() still gets the CLI's exit code, from the marker, once it is there.
    (On ae9866f poll() looked at the marker only after something else had reaped the shim: None for ever.)"""
    go_file = kit.path("cli-go")
    c = kit.run(kit.argv(until=go_file, exit=7))
    assert wait_event(c, "STARTED")
    log = c.dir / "shim.log"
    end = time.monotonic() + WAIT
    while time.monotonic() < end and "event dropped" not in log.read_text():
        for _ in range(2000):
            try:
                os.write(c._cmd, b"NOP\n")
            except BlockingIOError:
                time.sleep(0.001)
    assert "event dropped" in log.read_text(), "the event pipe is full"
    Path(go_file).write_text("")
    assert wait_for(lambda: os.path.exists(c.marker)), "the shim proved the group gone"
    assert wait_for(c.poll) == 7, "poll() alone gives the exit code"
    assert shim_exit(c) == groupshim.EXIT_EMPTY
    assert not any(e[0] == "LEADER_EXIT" for e in c.events), "(LEADER_EXIT really was dropped)"


@pytest.mark.parametrize("which", ["file", "dir"])
def test_r4_a_failed_sync_file_publishes_nothing_and_dir_leaves_a_visible_marker_said_as_unsynced(shim, monkeypatch,
                                                                                                  which):
    """A3: the temp file's sync fails: nothing is published (no marker, no temp file), MARKER_FAILED, exit 4. The
    directory's sync fails: the complete marker is visible and valid (the living node's proof: `empty()` takes it), and
    the shim says `UNSYNCED <errno>` before `EMPTY` and exits 6, not 0."""
    calls = []
    real = groupshim._fullsync

    def fake(fd):
        calls.append(fd)
        if len(calls) == (1 if which == "file" else 2):
            raise OSError(5, "injected EIO")
        real(fd)
    monkeypatch.setattr(groupshim, "_fullsync", fake)
    Fakes(shim, [(shim.pgid,), ()])
    shim.leader_exited, shim.leader_code = True, 0
    code = shim._check_empty()
    said = events_of(shim)
    if which == "file":
        assert code == groupshim.EXIT_MARKER_FAILED and not os.path.exists(shim.marker)
        assert not [f for f in os.listdir(os.path.dirname(shim.marker)) if f.endswith(".tmp")]
        assert "MARKER_FAILED" in said and "EMPTY" not in said
    else:
        assert code == groupshim.EXIT_UNSYNCED and marker_proves(shim.marker, shim.nonce, shim.boot)
        assert said[said.index("UNSYNCED") + 1] == "5" and said.index("UNSYNCED") < said.index("EMPTY")


def test_r4_the_signal_goes_before_its_diagnostic_and_a_failing_sink_cannot_stop_it(shim):
    f = Fakes(shim, [])
    shim.sink = ListSink(f.calls)
    shim._on_eof()
    assert f.calls[0] == ("killpg", signal.SIGTERM, False, False), f.calls
    assert any(c[0] == "diag" and c[1].startswith("EOF on the command pipe") for c in f.calls[1:])

    class Broken:
        def put(self, msg):
            raise RuntimeError("a diagnostics sink that fails")

        def start(self):
            pass
    shim.sink, f.calls[:] = Broken(), []
    f.now += 5
    shim._kill_now()
    assert f.calls == [("killpg", signal.SIGKILL, False, False)], "the KILL went, the sink's failure stayed out"


@pytest.mark.parametrize("stall", ["open_blocks", "accepts_then_stalls"])
def test_r4_a_stalled_diagnostics_sink_never_holds_up_the_escalation(kit, stall):
    """A4: the log is a FIFO. (1) Nobody ever opens its read end, so opening it blocks. (2) A reader holds it open and
    never reads, so it takes some 8 KiB (a FIFO's buffer, measured) and then every write blocks. In both, the shim
    still does its TERM, its KILL at the grace, the proof and the marker, on time: only its diagnostics thread waits.
    (On ae9866f the shim's own loop opened and wrote the log.)"""
    fifo = kit.tmp / "log.fifo"
    os.mkfifo(fifo)
    held = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK) if stall == "accepts_then_stalls" else None
    up = kit.path("up")
    try:
        c = kit.run(kit.argv(ignore_term=True, started=up, wait_abort=True), grace=0.5, log=str(fifo))
        assert wait_event(c, "STARTED") and wait_for(lambda: os.path.exists(up))
        if held is not None:
            sent = 0
            end = time.monotonic() + WAIT
            while sent < 400 and time.monotonic() < end:
                try:
                    c.terminate()
                    sent += 1
                except BlockingIOError:
                    time.sleep(0.005)
                c.poll()
            assert wait_for(lambda: fionread(held) >= 4096), \
                f"the sink took bytes, and now holds them unread ({sent} TERMs, {fionread(held)} bytes, {c.events[-3:]})"
        t0 = time.monotonic()
        c.close_commands()
        assert shim_exit(c) == groupshim.EXIT_EMPTY, c.events
        assert time.monotonic() - t0 < 6.0
        assert ("SENT", "KILL", "group") in c.events and c.empty()
    finally:
        if held is not None:
            os.close(held)


def test_unit_the_signal_target_follows_establishment_and_the_replay_keeps_the_clock(shim):
    """Before establishment is observed, the child's pid (`kill`), never `killpg`; at establishment, each pid-only signal
    once more to the group; after, the group. The replay changes neither the first TERM time nor the KILL deadline."""
    f = Fakes(shim, [])
    shim.established = False
    shim._kill_request()
    assert f.calls == [("kill", signal.SIGTERM, False, False)] and shim.first_term_at == 100.0
    assert shim.kill_due == 101.0
    f.now = 100.6
    shim._established()
    assert f.calls[1:] == [("killpg", signal.SIGTERM, False, False)] and shim.pid_only == []
    assert shim.first_term_at == 100.0 and shim.kill_due == 101.0
    f.now = 101.0
    shim._kill_now()
    assert f.calls[2:] == [("killpg", signal.SIGKILL, False, False)]
    # a KILL sent to the pid is replayed too
    shim.established, shim.pid_only, f.calls[:] = False, [], []
    shim._signal(signal.SIGKILL)
    shim._established()
    assert f.calls == [("kill", signal.SIGKILL, False, False), ("killpg", signal.SIGKILL, False, False)]
