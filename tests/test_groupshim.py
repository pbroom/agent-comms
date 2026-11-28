"""Ownership C3-1: the per-group supervisor (agent_comms/groupshim.py) and its node-side handle
(agent_comms/shimchild.py), NOT wired in. DESIGN_NOTES "Ownership C3-1: the group supervisor (unwired)".

Most of these tests use REAL processes and REAL signals on macOS (two defects of an earlier PR were hidden by fakes):
the shims are this test's own children, the CLIs and their members are stand-ins this test wrote, and nothing is
ever signalled but through its own handle (a shim's Popen, a node stand-in's Popen) or by the shims, which signal
only their own groups. Stand-ins stop by themselves: on a release file, on the shared `abort` file the cleanup
writes, or after their own bounded lifetime. Every wait is bounded. The decision tests at the end run the shim's
`Shim` object in this process with fake system calls (and a fake pgid that is never signalled)."""
from __future__ import annotations

import ast
import ctypes
import json
import os
import select
import signal
import socket
import stat
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

if sys.platform != "darwin":    # (before anything macOS-only is touched: a skip mark cannot stop an import-time failure)
    pytest.skip("the shim is macOS-only (kqueue, libproc)", allow_module_level=True)

from agent_comms import groupshim, node_groups, shimchild   # noqa: E402
from agent_comms.dispatch import spawn_piped   # noqa: E402
from agent_comms.shimchild import ShimChild, marker_proves   # noqa: E402

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
            if m.get("term_file"):          # (it notes every TERM, and lives on)
                signal.signal(signal.SIGTERM, lambda *_: put(m["term_file"], time.monotonic()))
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
    """When the matching log lines happened, on the shim's own clock: a signal's ATTEMPT time where the line carries it
    (the clock its grace is counted on), else the line's own stamp."""
    out = []
    for line in log.read_text().splitlines():
        if needle in line:
            _, at, rest = line.partition("attempted at ")
            out.append(float(rest.rstrip(")").split(")")[0]) if at else float(line.split(" ", 1)[0]))
    return out


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

    def start(self, gate: bool = False, pauses: dict | None = None, defer: bool = False, gate_files=(),
              send_secret: bool = True, **kw) -> ShimChild:
        """`pauses`: the child's test-only pauses ({"before-setsid" | "after-setsid": options}); `defer`: the shim reads
        the launch handshake only once `defer-handshake.go` exists. Both imply the test gate. Each shim's directory is
        its private marker directory (`make_marker_dir`), which also holds its logs and gate. `send_secret=False`:
        the shim is spawned with no NONCE frame sent (the test sends its own frames)."""
        self.n += 1
        d = Path(shimchild.make_marker_dir(str(self.tmp / f"shim{self.n}")))
        g = None
        if gate or pauses or defer or gate_files:
            g = d / "gate"
            g.mkdir()
            for name in gate_files:
                (g / name).write_text("")
            if gate:
                (g / "numbered").write_text("")     # (the shim's numbered gates: register, every signal)
            for name, opts in (pauses or {}).items():
                (g / f"pause.{name}").write_text(json.dumps(opts))
            if defer:
                (g / "defer-handshake").write_text("")
        kw.setdefault("stderr_log", str(d / "stderr.log"))
        kw.setdefault("log", str(d / "shim.log"))
        gate_dir = None if g is None else str(g)
        if send_secret:
            c = ShimChild.start(marker_dir=str(d), test_gate=gate_dir, **kw)
        else:
            c = ShimChild._launch(marker=str(d / "unsent.marker"), key=os.urandom(groupshim.KEY_BYTES),
                                  boot=groupshim.boot_id(), test_gate=gate_dir, **kw)
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
                for name in ("before-setsid.go", "after-setsid.go", "after-release.go", "defer-handshake.go", "abort"):
                    (c.gate_dir / name).write_text("")
            if c.shim_poll() is None:
                c.close_commands()              # (EOF: the shim stops its own group, then exits)
                if not wait_for(lambda: c.shim_poll() is not None, 15):
                    c._kill_shim()              # (its own handle; only while it has not reaped it)
            wait_for(lambda: c.shim_poll() is not None, 10)
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
    assert marker_proves(c.marker, c._key, c.boot)


def test_c1_a_fast_exit_is_reported(kit):
    c = kit.run(["/bin/sh", "-c", "exit 5"])
    assert shim_exit(c) == groupshim.EXIT_EMPTY
    assert c.poll() == 5 and kinds(c) == ["STARTED", "LEADER_EXIT", "EMPTY"] and c.empty()


def test_c1_an_exit_before_its_registration_is_read_without_reaping(kit):
    """The test-only gate holds the shim between the fork and the kqueue registration until the child is already a
    zombie: the registration is refused (ESRCH), and the status is read by waitid(WNOWAIT), without reaping. (Since
    review rounds 1 and 2 a child does nothing in the checkout, let alone exec, before RELEASE, which the shim writes
    only after the registration; so only a child that ends before its RELEASE can exit before the registration: here
    it exits 3 at a test-only pause after its setsid().)"""
    c = kit.start(gate=True, pauses={"after-setsid": {"exit": 3}})
    c.go(["/bin/sh", "-c", "exit 5"], cwd=str(kit.work), env=ENV, kill_grace=1.0)
    name, pid = gate_reach(c, 1).split()
    assert name == "register"
    pid = int(pid)
    assert wait_for(lambda: (kinfo(pid) or (0,))[0] == SZOMB), "the child exited before its registration"
    gate_open(c, 1)
    assert shim_exit(c) == groupshim.EXIT_EMPTY, c.events
    assert c.poll() == 3 and c.start_errno == 0 and c.cli_pid is None and c.empty()
    assert kinds(c) == ["START_FAILED", "LEADER_EXIT", "EMPTY"]
    assert "exit registration refused (errno 3)" in (c.dir / "shim.log").read_text()


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
        c = ShimChild.start(marker_dir=s["dir"], log=s["log"], stderr_log=s["stderr"], test_gate=s.get("gate"))
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
# (The launch secret goes to the TEST, this stand-in's caller, so that it can check each marker's authenticity once
# this "node" is dead: that is not release authority, only evidence of what the shim published.)
out = {"errors": errors, "noise": [p.pid for p in noise],
       "shims": {n: {"shim": c.pid, "cli": c.cli_pid, "marker": c.marker, "key": c._key.hex(), "boot": c.boot}
                 for n, c in children.items()}}
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
        d = shimchild.make_marker_dir(str(kit.tmp / f"node-{name}"))
        out["shims"][name] = {"dir": d, "log": f"{d}/shim.log", "stderr": f"{d}/stderr.log", "argv": argv,
                              "cwd": str(kit.work), "env": ENV, "grace": grace}
    out.update(extra)
    return out


def published(s: dict) -> bool:
    """The marker a dead node stand-in's shim published is authentic for its launch (evidence, not release authority:
    with the node gone nobody holds the shim's status; DESIGN_NOTES C3-2a, proof after a restart)."""
    return marker_proves(s["marker"], bytes.fromhex(s["key"]), s["boot"])


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
    b_marker = b["marker"]
    t_kill = time.monotonic()
    node.kill()                             # (its own handle)
    node.wait(timeout=10)
    assert wait_for(lambda: published(a), WAIT), "A proved its group gone"
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
    assert wait_for(lambda: published(b)), "B proved its group gone"
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
        assert wait_for(lambda: published(c)), "the marker, with the event pipe full"
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
c1 = ShimChild.start(marker_dir=spec["markers"], log=d + "/s1.log", stderr_log=d + "/c1.log")
c1.go(spec["py_shim"], cwd=spec["cwd"], env=spec["env"], kill_grace=0.5)
wait(lambda: os.path.exists(spec["rep_direct"]) and os.path.exists(spec["rep_shim"]))
# 2. /bin/sleep (it changes no disposition but SIGINFO), directly and through a shim: the kernel's ignored mask
sd = subprocess.Popen(["/bin/sleep", "30"], cwd=spec["cwd"], env=spec["env"], stdin=subprocess.PIPE,
                      stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, close_fds=True, start_new_session=True)
c2 = ShimChild.start(marker_dir=spec["markers"], log=d + "/s2.log", stderr_log=d + "/c2.log")
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
from agent_comms.shimchild import marker_proves
out["m1"] = bool(wait(lambda: marker_proves(c1.marker, c1._key, c1.boot)))
out["m2"] = bool(wait(lambda: marker_proves(c2.marker, c2._key, c2.boot)))
# This "node" ignores SIGCHLD: the kernel reaps the shims itself and Popen says 0 for a status nobody saw, so the
# handles must not take the release pair (C3-2a).
wait(lambda: c1.shim_poll() is not None and c2.shim_poll() is not None)
out["shim_status"] = [c1.shim_poll(), c2.shim_poll()]
out["empty"] = [c1.empty(), c2.empty()]
with open(spec["report"] + ".tmp", "w") as f:
    json.dump(out, f)
os.rename(spec["report"] + ".tmp", spec["report"])
'''


def test_c6_signal_dispositions_are_what_a_direct_spawn_gives_and_an_ignored_sigchld_does_not_unanchor(kit):
    """A node stand-in that ignores SIGHUP, SIGUSR2 and SIGCHLD. Its directly spawned CLI and its shimmed one see the
    same dispositions: as Python reads them (every signal Python does not itself set), and in the kernel's ignored
    mask of /bin/sleep (SIGPIPE and SIGXFSZ at their default, as Popen's restore_signals leaves them; SIGHUP ignored,
    as inherited). And the shim still anchors: it resets SIGCHLD for itself, so its CLI's zombie is kept (not
    auto-reaped) until the group is empty, and both markers are published. (C3-2a) But a node that ignores SIGCHLD
    has the KERNEL reap its own children, the shims, and Popen then reports exit 0 for a status nobody observed: its
    handles refuse the release pair (`empty()` False), though both markers are authentic."""
    member_go = kit.path("member-go")
    rep_d, rep_s = kit.path("rep-direct"), kit.path("rep-shim")
    spec = {"root": ROOT, "dir": str(kit.tmp), "markers": shimchild.make_marker_dir(kit.path("sig-markers")),
            "cwd": str(kit.work), "env": ENV, "report": kit.path("sig-report"),
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
    assert out["shim_status"] == ["unknown", "unknown"] and out["empty"] == [False, False], out


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
    c._kill_shim()
    assert shim_exit(c) == -signal.SIGKILL
    assert c.lost() and not c.empty() and c.poll() is None
    Path(kit.abort).write_text("1")         # (the orphaned CLI stops by itself)


def record(key: bytes, on_boot: str, **fields) -> dict:
    """A marker record authenticated under `key` (as the shim makes it), with `fields` changed BEFORE the MAC."""
    rec = {"v": groupshim.MARKER_VERSION, "outcome": "empty", "cli_pid": 4242, "leader_exit": 0, "boot": on_boot}
    rec.update(fields)
    rec["mac"] = groupshim.marker_mac(key, rec)
    return rec


def test_c7_a_marker_of_another_launch_or_boot_is_no_proof(kit, tmp_path):
    """Each record below is AUTHENTIC under the key it was made with: one under another launch's key, one for another
    boot (under this launch's key), an unknown outcome or version, C3-1's unauthenticated record, a missing field, a
    list, garbage and a symlink are no proof for this launch on this boot."""
    key, boot = b"k" * groupshim.KEY_BYTES, groupshim.boot_id()
    good = record(key, boot)
    p = tmp_path / "m.json"
    other_boot = "00000000-0000-4000-8000-000000000000"
    v1 = {"v": 1, "nonce": "a" * 32, "boot": boot, "outcome": "empty", "cli_pid": 4242, "leader_exit": 0}
    for bad in (record(b"o" * groupshim.KEY_BYTES, boot), record(key, other_boot), record(key, boot, outcome="held"),
                record(key, boot, v=1), record(key, boot, v=3), v1, {k: v for k, v in good.items() if k != "boot"},
                [good]):
        p.write_text(json.dumps(bad) + "\n")
        assert not marker_proves(str(p), key, boot), bad
    p.write_text("{not json")
    assert not marker_proves(str(p), key, boot)
    real = tmp_path / "real.json"
    real.write_text(json.dumps(good) + "\n")
    p.unlink()
    p.symlink_to(real)
    assert not marker_proves(str(p), key, boot), "no symlink"
    assert marker_proves(str(real), key, boot) and not marker_proves(str(real), key, other_boot)
    assert not marker_proves(str(real), b"o" * groupshim.KEY_BYTES, boot) and not marker_proves(str(real), key[:-1], boot)
    # and through a real handle, before its own shim publishes
    c = kit.start()
    for other in (record(b"o" * groupshim.KEY_BYTES, c.boot), record(c._key, other_boot)):
        Path(c.marker).write_text(json.dumps(other) + "\n")
        assert not c.empty()
    os.unlink(c.marker)                     # (the test's own file: the shim never replaces one, review round 1 R1)
    c.close_commands()
    assert shim_exit(c) == groupshim.EXIT_NOGO
    assert c.empty()


def test_c7_the_empty_event_alone_is_no_proof(kit):
    c = kit.run(["/bin/sh", "-c", "exit 0"])
    assert wait_for(lambda: groupshim.waitid_nowait(c.pid) == 0)    # (exited, not reaped; nothing pumped yet)
    put_marker(c, json.dumps(record(b"x" * groupshim.KEY_BYTES, c.boot)).encode() + b"\n")
    assert c.shim_poll() == 0 and kinds(c) == ["STARTED", "LEADER_EXIT", "EMPTY"], "drained before the exit is said"
    assert not c.empty(), "EMPTY was said, but the marker is not this launch's"


def test_c7_a_stale_handle_against_its_successor(kit):
    """A stale handle (its shim killed: lost) and its successor for the same checkout, each with its own fresh marker
    path (C3-2a, F3): the successor's marker, even copied onto the stale one's path, is never the stale handle's
    proof, and the stale handle's commands never reach the successor."""
    first = kit.run(kit.argv(wait_abort=True))
    assert wait_event(first, "STARTED")
    second = kit.start()
    assert second.marker != first.marker
    first._kill_shim()                      # (the first shim: lost, no marker; its CLI stops at `abort`)
    assert shim_exit(first) == -signal.SIGKILL
    second.go(["/bin/sh", "-c", "exit 0"], cwd=str(kit.work), env=ENV, kill_grace=1.0)
    assert shim_exit(second) == groupshim.EXIT_EMPTY
    put_marker(first, Path(second.marker).read_bytes())
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
    s = groupshim.Shim(key=b"n" * groupshim.KEY_BYTES, boot="00000000-0000-4000-8000-000000000001",
                       marker=str(tmp_path / "marker.json"), cmd_fd=cmd_r, event_fd=evt_w, sink=ListSink())
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
    assert marker_proves(shim.marker, shim.key, shim.boot)
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
    assert groupshim.boot_id() == shimchild.boot_id() and groupshim.valid_boot(groupshim.boot_id())


def test_unit_the_nonce_frame_parser():
    """(C3-2a) `NONCE 1 <64 lowercase hex>` and nothing else."""
    good = b"NONCE 1 " + b"0f" * groupshim.KEY_BYTES
    assert groupshim.parse_nonce(good) == bytes([0x0f]) * groupshim.KEY_BYTES
    for raw, reason in [(b"NONCE 2 " + b"0f" * 32, "version"), (b"NONCE 1 " + b"0F" * 32, "invalid"),
                        (b"NONCE 1 " + b"0f" * 31, "invalid"), (b"NONCE 1 " + b"0f" * 33, "invalid"),
                        (b"NONCE 1 " + b"0g" * 32, "invalid"), (b"NONCE 1  " + b"0f" * 32, "invalid"),
                        (b"NONCE x " + b"0f" * 32, "invalid"), (good + b" x", "invalid"), (b"NONCE", "invalid"),
                        (b"GO 1 {}", "invalid"), (b"", "invalid")]:
        with pytest.raises(groupshim.NoSecret) as e:
            groupshim.parse_nonce(raw)
        assert e.value.reason == reason, raw


def test_unit_the_marker_mac_is_hmac_sha256_over_the_canonical_fields():
    """(C3-2a) A known answer, computed here by the standard library on the literal canonical bytes: the encoding (sorted
    keys, no spaces, the domain tag) is pinned, and `mac` is never part of what is authenticated."""
    import hashlib
    import hmac as hmac_
    key = bytes(range(32))
    fields = {"v": 2, "outcome": "empty", "cli_pid": 4242, "leader_exit": -9,
              "boot": "01234567-89AB-CDEF-0123-456789ABCDEF"}
    canonical = (b'agent-comms groupshim marker v2\x00{"boot":"01234567-89AB-CDEF-0123-456789ABCDEF","cli_pid":4242,'
                 b'"leader_exit":-9,"outcome":"empty","v":2}')
    want = hmac_.new(key, canonical, hashlib.sha256).hexdigest()
    assert groupshim.marker_mac(key, fields) == want
    assert groupshim.marker_mac(key, {**fields, "mac": "ignored"}) == want
    assert groupshim.marker_mac(bytes(32), fields) != want


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
        assert wait_for(lambda: published(p)), "the marker"
        log_path = Path(spec["shims"]["P"]["log"])
        assert wait_for(lambda: "exit 0" in log_path.read_text())
        call = "killpg" if where == "after-setsid" else "kill"
        t_term = diag_times(log_path, f"{call}({pid}, TERM) sent")
        t_kill = diag_times(log_path, f"{call}({pid}, KILL) sent")
        assert t_term and t_kill and t_kill[0] - t_term[0] >= 0.5, (t_term, t_kill)
        log = log_path.read_text()
        assert "EOF on the command pipe" in log and "event STARTED" not in log and "event START_UNKNOWN" in log
        assert "event START_FAILED" not in log, "killed after RELEASE was written, saying nothing: unknown, not failed"
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
    assert c.start_unknown and c.start_errno is None, "killed after RELEASE, saying nothing: the handle says unknown"
    assert wait_for(lambda: (kinfo(member) or (SZOMB, 0, 0))[2] != pid or kinfo(member) is None)


def test_r2_the_marker_reader_takes_only_a_complete_record_in_a_regular_file_and_never_waits(tmp_path):
    """R2 and A5: one reader for every use. A FIFO (with no writer, or with one that supplies a matching record), a
    directory and a device are refused without waiting; so are a truncated or doubled record, a version that is
    `true`, `2.0` or `"2"`, an over-size file, another key or boot, and any key or type that is not exactly the
    shim's (each of those AUTHENTIC under this launch's key: the type checks stand on their own); and a MAC that is not
    64 lowercase hex digits. (On ae9866f the FIFO with no writer blocks the reader, and the FIFO with a writer and
    `true` are proof.)"""
    nonce, boot = b"a" * groupshim.KEY_BYTES, groupshim.boot_id()      # (the launch secret, C3-2a)
    good = record(nonce, boot)
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
    signed = lambda **f: dump(record(nonce, boot, **f))       # noqa: E731
    missing = {k: v for k, v in good.items() if k not in ("leader_exit", "mac")}
    missing["mac"] = groupshim.marker_mac(nonce, missing)
    cases = {
        "truncated": line[:-6], "no newline": line[:-1], "two records": line + line,
        "v true": signed(v=True), "v 2.0": signed(v=2.0), "v '2'": signed(v="2"), "v 1": signed(v=1),
        "v 3": signed(v=3), "other key": dump(record(b"b" * groupshim.KEY_BYTES, boot)),
        "other boot": signed(boot="00000000-0000-4000-8000-000000000000"), "extra key": signed(x=1),
        "missing key": dump(missing), "outcome held": signed(outcome="held"), "bool pid": signed(cli_pid=True),
        "no pid": signed(cli_pid=None), "float exit": signed(leader_exit=1.0),
        "never_started with a pid": signed(outcome="never_started"),
        "mac upper-case": dump({**good, "mac": good["mac"].upper()}), "mac short": dump({**good, "mac": good["mac"][:-1]}),
        "mac not a string": dump({**good, "mac": 7}), "no mac": dump({k: v for k, v in good.items() if k != "mac"}),
        "over-size": line.replace(b",", b"," + b" " * 5000, 1), "a list": dump([good]),
    }
    p = tmp_path / "marker.json"
    for name, data in cases.items():
        p.write_bytes(data)
        assert not marker_proves(str(p), nonce, boot), name
    p.write_bytes(signed(outcome="never_started", cli_pid=None, leader_exit=None))
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
        assert code == groupshim.EXIT_UNSYNCED and marker_proves(shim.marker, shim.key, shim.boot)
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


# ---------------------------------------------------------------- code review round 2 (Sol on 7dee459)

class _VnodePath(ctypes.Structure):     # struct proc_vnodepathinfo: pvi_cdir (vnode_info 152 bytes + path 1024), ...
    _fields_ = [("cdir_info", ctypes.c_char * 152), ("cdir_path", ctypes.c_char * 1024),
                ("rdir_info", ctypes.c_char * 152), ("rdir_path", ctypes.c_char * 1024)]


def cwd_of(pid: int) -> str | None:
    """A live process's current directory (libproc PROC_PIDVNODEPATHINFO), or None."""
    from agent_comms import procinfo
    v = _VnodePath()
    n = procinfo._libproc().proc_pidinfo(pid, 9, ctypes.c_uint64(0), ctypes.byref(v), ctypes.sizeof(v))
    return v.cdir_path.decode(errors="replace") if n == ctypes.sizeof(v) else None


@pytest.mark.parametrize("where", ["after-setsid", "after-release"])
def test_r5_a_failed_diagnostics_start_never_ends_supervision(kit, where):
    """R5: the diagnostics thread cannot be started (injected through the test gate: `fail-diag-start`) while the
    child is paused before RELEASE (just after its setsid) or after it (before its chdir). The node lets go (EOF): the
    shim still sends TERM, KILL at the grace (the paused child ignores TERM), proves the group gone and publishes the
    marker. The diagnostics are off: no log is ever opened. (On 7dee459 a refused thread ended the shim: EXIT_CRASH,
    the child unsupervised.)"""
    c = kit.start(pauses={where: {"ignore_term": True}}, gate_files=("fail-diag-start",))
    c.go(kit.argv(), cwd=str(kit.work), env=ENV, kill_grace=0.5)
    assert child_at(c, where), "the child paused"
    c.close_commands()
    assert shim_exit(c) == groupshim.EXIT_EMPTY, c.events
    seq = [e for e in c.events if e[0] in ("SENT", "FAILED", "EMPTY")]
    assert seq == [("SENT", "TERM", "group"), ("SENT", "KILL", "group"), ("EMPTY",)], seq
    assert c.empty() and not (c.dir / "shim.log").exists(), "supervised to the proof, with no diagnostics at all"


def test_r5_a_child_whose_shim_dies_before_release_never_enters_the_checkout(kit):
    """R5: the shim is held (its test gate) before it registers the watch and writes RELEASE; its child, already past
    setsid(), waits for RELEASE with its cwd still the shim's ("/"), not the checkout. The shim is killed (its own
    handle): the child reads EOF and exits without ever changing directory (its `after-chdir` announcement never
    appears). A control launch shows the announcement does appear, with the checkout as its cwd, once released. (On
    7dee459 the child did its chdir before waiting for RELEASE.)"""
    ok = kit.start(pauses={"after-chdir": {"wait": False}})
    ok.go(["/bin/sh", "-c", "exit 0"], cwd=str(kit.work), env=ENV, kill_grace=1.0)
    assert shim_exit(ok) == groupshim.EXIT_EMPTY
    seen = read_json(ok.gate_dir / "after-chdir.at")
    assert seen and seen["cwd"] == os.path.realpath(kit.work), "the control: released, it entered the checkout"
    c = kit.start(gate=True, pauses={"after-chdir": {"wait": False}})
    c.go(kit.argv(), cwd=str(kit.work), env=ENV, kill_grace=1.0)
    name, pid = gate_reach(c, 1).split()
    pid = int(pid)
    time.sleep(0.5)                         # (time for the child to reach its read of RELEASE)
    assert cwd_of(pid) == "/", f"waiting for RELEASE, the child is not in the checkout ({cwd_of(pid)!r})"
    c._kill_shim()                          # (its own handle; the shim never wrote RELEASE)
    assert shim_exit(c) == -signal.SIGKILL
    assert wait_for(lambda: kinfo(pid) is None), "the child exited by itself (and launchd reaped it)"
    assert not (c.gate_dir / "after-chdir.at").exists(), "it never entered the checkout"
    assert c.lost() and not c.empty(), "that launch is LOST: no proof, the checkout stays retained"


def test_unit_r5_the_loop_survives_every_non_essential_failure(shim):
    """R5.3, in process: the kqueue wait fails on every call (the loop polls instead), the test gate cannot work, the
    diagnostics sink raises, the event fd is unusable. The node's EOF still brings TERM, KILL at the grace, the child's
    exit (polled by waitid), the proof and the marker."""
    f = Fakes(shim, [(shim.pgid,), ()])
    exited = {"code": None}
    shim.waitid = lambda pid: exited["code"]

    class BrokenKq:
        def control(self, *a, **k):
            raise OSError(9, "injected EBADF")

    class Broken:
        def put(self, msg):
            raise RuntimeError("injected")

        def start(self):
            raise RuntimeError("injected")
    real_kill = f._killpg

    def killpg(pid, sig):
        real_kill(pid, sig)
        if sig == signal.SIGKILL:
            exited["code"] = -signal.SIGKILL       # (the fake child dies of the KILL)
    shim.killpg = killpg
    shim.kq, shim.sink, shim.gate_dir = BrokenKq(), Broken(), 12345   # (a gate dir that cannot even be joined)
    os.close(shim.event_fd)
    shim.event_fd = 99999                  # (an fd that is not open: EBADF on every event)
    shim.watching, shim.hs_fd = True, None
    shim.eof = True
    shim.first_term_at = None
    shim._kill_request()                   # (the node's EOF, as `_on_eof` makes it)
    ticks = iter(range(1000))
    shim.clock = lambda: 100.0 + next(ticks) * 0.05
    assert shim.run() == groupshim.EXIT_EMPTY
    sigs = [c[1] for c in f.calls if c[0] == "killpg"]
    assert sigs[:2] == [signal.SIGTERM, signal.SIGKILL] and marker_proves(shim.marker, shim.key, shim.boot)
    assert shim.degraded and shim.leader_code == -signal.SIGKILL


# ---------------------------------------------------------------- Sol's should on 05fbde5: an unobserved exec is UNKNOWN

def _degraded_launch(shim, said: bytes, released: bool):
    """In process: a child whose handshake said `said` and then closed (its exec, or its death), RELEASE written or
    not, and a kqueue whose every wait fails (so NOTE_EXEC can never be seen); the child's exit is polled by waitid
    (exit 0), and the group listing then proves it empty."""
    Fakes(shim, [(shim.pgid,), ()])
    r, w = os.pipe()
    os.write(w, said)
    os.close(w)                         # (the child's end closed: at its exec, or at its death)
    os.set_blocking(r, False)

    class BrokenKq:
        def control(self, *a, **k):
            raise OSError(9, "injected EBADF")
    shim.kq, shim.hs_fd, shim.established = BrokenKq(), r, False
    shim.watching, shim.released, shim.eof = True, released, True   # (no command pipe here: nothing is read from it)
    shim.waitid = lambda pid: 0
    ticks = iter(range(1000))
    shim.clock = lambda: 100.0 + next(ticks) * 0.05
    assert shim.run() == groupshim.EXIT_EMPTY
    return events_of(shim)


def test_unit_an_exec_whose_outcome_was_not_observed_is_unknown_not_failed(shim):
    """With the kqueue failing after RELEASE, a child that said `S`, then closed its handshake (it exec'd) and exited is
    reported START_UNKNOWN: not STARTED (NOTE_EXEC was never seen), and never START_FAILED (nothing shows it did not
    run: its checkout may have been touched). Cleanup and the proof are unaffected. (On 05fbde5: START_FAILED 0.)"""
    said = _degraded_launch(shim, b"S", released=True)
    assert "START_UNKNOWN" in said and "START_FAILED" not in said and "STARTED" not in said, said
    assert said.index("START_UNKNOWN") < said.index("LEADER_EXIT") < said.index("EMPTY")
    assert marker_proves(shim.marker, shim.key, shim.boot)


@pytest.mark.parametrize("said,released,errno", [(b"Sexec 2\n", True, "2"), (b"Schdir 13\n", True, "13"),
                                                 (b"S", False, "0")],
                         ids=["the child said exec failed", "the child said chdir failed", "RELEASE never written"])
def test_unit_start_failed_needs_positive_evidence(shim, said, released, errno):
    """START_FAILED only on positive evidence that the exec did not happen: the child's own `F<stage> <errno>` report
    (after RELEASE too), or a death while RELEASE was never written (it could not exec)."""
    said = _degraded_launch(shim, said.replace(b"Sexec", b"SFexec").replace(b"Schdir", b"SFchdir"), released)
    assert said[said.index("START_FAILED") + 1] == errno and "START_UNKNOWN" not in said, said


# ---------------------------------------------------------------- C3-2a: the proof is authenticated (still unwired)

def cred(c: ShimChild) -> bytes:
    """The handle's launch secret, as the marker reader takes it. TEST-ONLY: nothing in the node shows it anywhere."""
    return c._key


def secret_text(c: ShimChild) -> str:
    return c._key.hex()


def valid_record(c: ShimChild, outcome: str, cli_pid=None, leader_exit=None) -> bytes:
    """A marker this launch's reader accepts (written by the TEST, which holds the secret: a stand-in for the shim)."""
    return json.dumps(record(c._key, c.boot, outcome=outcome, cli_pid=cli_pid, leader_exit=leader_exit)).encode() + b"\n"


def put_marker(c: ShimChild, data: bytes) -> None:
    tmp = c.marker + ".test.tmp"
    Path(tmp).write_bytes(data)
    os.rename(tmp, c.marker)


FORGER = r'''
import hashlib, hmac, json, os, re, subprocess, sys, time
spec = json.loads(sys.argv[1])
sys.path.insert(0, spec["root"])
T_END = time.monotonic() + spec.get("max", 60)

def stop(path=None):
    return time.monotonic() >= T_END or os.path.exists(spec["abort"]) or (path and os.path.exists(path))

def put(path, data):
    with open(path + ".f.tmp", "wb") as f:
        f.write(data)
    os.rename(path + ".f.tmp", path)

# Everything a same-user CLI can read about its supervisor: its argv and environment (ps), and every file under the
# directory that holds its marker, its diagnostics log and its stderr log.
ppid = os.getppid()
def ps(*args):
    return subprocess.run(["/bin/ps", *args, "-p", str(ppid)], capture_output=True, text=True, timeout=10).stdout
seen = {"ppid": ppid, "argv": ps("-ww", "-o", "args="), "env": ps("-wwE", "-o", "command="), "files": {}}
for top, _dirs, names in os.walk(spec["look"]):
    for name in names:
        p = os.path.join(top, name)
        try:
            with open(p, "rb") as f:
                seen["files"][p] = f.read(1 << 20).decode("utf-8", "replace")
        except OSError:
            pass
words = seen["argv"].split()
def arg(name):
    return words[words.index(name) + 1] if name in words[:-1] else None
marker, boot, nonce = arg("--marker"), arg("--boot"), arg("--nonce")
text = "\n".join([seen["argv"], seen["env"], *seen["files"].values()])
cands = sorted(set(re.findall(r"[A-Za-z0-9_-]{8,128}", text)), key=lambda s: (-len(s), s))[:300]
keys = []
for c in cands:
    keys.append(c.encode())
    if len(c) % 2 == 0 and re.fullmatch(r"[0-9a-fA-F]+", c):
        keys.append(bytes.fromhex(c))
try:
    from agent_comms.groupshim import marker_mac, MARKER_VERSION     # (the algorithm is no secret)
except ImportError:
    marker_mac = None
pid = os.getpid()
forged = []
if nonce:           # (06866a5: the launch nonce is on the shim's command line, so this one is valid there)
    forged.append({"v": 1, "nonce": nonce, "boot": boot, "outcome": "empty", "cli_pid": pid, "leader_exit": 0})
if marker_mac is not None:
    for k in keys:
        rec = {"v": MARKER_VERSION, "outcome": "empty", "cli_pid": pid, "leader_exit": 0, "boot": boot}
        rec["mac"] = marker_mac(k, rec)
        forged.append(rec)
    for c in cands:
        if re.fullmatch(r"[0-9a-f]{64}", c):        # (a MAC-shaped value seen anywhere, copied as the MAC)
            forged.append({"v": MARKER_VERSION, "outcome": "empty", "cli_pid": pid, "leader_exit": 0, "boot": boot,
                           "mac": c})
for c in cands[:100]:
    forged.append({"v": 1, "nonce": c, "boot": boot, "outcome": "empty", "cli_pid": pid, "leader_exit": 0})
lines = [json.dumps(r).encode() + b"\n" for r in forged]
best = lines[0]
if not spec.get("escape"):
    put(marker, best)
if spec.get("escape"):
    # A descendant that leaves the group (as Claude Code's own tool commands do) and outlives the CLI: once the shim
    # has published its own marker, it puts the forgery back in its place.
    child = os.fork()
    if child == 0:
        try:
            null = os.open(os.devnull, os.O_RDWR)
            for fd in (0, 1, 2):
                os.dup2(null, fd)
            os.setsid()
            while not stop():
                try:
                    with open(marker, "rb") as f:
                        now = f.read()
                except OSError:
                    now = None
                if now is not None and now != best:
                    put(marker, best)
                    put(spec["escape"], now)
                    break
                time.sleep(0.002)
        finally:
            os._exit(0)
with open(spec["report"] + ".tmp", "w") as f:
    json.dump(dict(seen, marker=marker, forged=[x.decode() for x in lines]), f)
os.rename(spec["report"] + ".tmp", spec["report"])
while spec.get("until") and not stop(spec["until"]):
    time.sleep(0.005)
sys.exit(0)
'''


def forger_argv(kit: Kit, **spec) -> list[str]:
    script = kit.tmp / "forger.py"
    if not script.exists():
        script.write_text(FORGER)
    return [PY, str(script), json.dumps(dict({"root": ROOT, "abort": kit.abort}, **spec))]


def test_f1_the_supervised_cli_cannot_forge_the_proof(kit):
    """F1 (the reviewer's probe, generalised): the CLI reads its shim's argv and environment with `ps`, and every file
    under the directory that holds its marker and the shim's logs, and writes the best marker it can (06866a5: the
    nonce from the argv; now: an HMAC keyed by every token it saw). While it runs, `empty()` is False and `poll()` None;
    every forgery it made is refused by the authenticator ALONE; the secret is in none of what it read, in no event and
    in no diagnostic line. Once it exits, the shim proves the group gone but (review round 1, R1) does not replace the
    forgery left at the marker path: nothing published, exit 4, `lost()` (held), the file untouched. (On 06866a5
    `empty()` is True while the forger still runs.)"""
    c = kit.start()
    rep, go = kit.path("forger-report"), kit.path("forger-go")
    c.go(forger_argv(kit, look=str(c.dir), report=rep, until=go), cwd=str(kit.work), env=ENV, kill_grace=1.0)
    seen = wait_for(lambda: read_json(rep))
    assert seen, "the forger looked and wrote its best marker"
    assert "--marker" in seen["argv"] and "LC_ALL=C" in seen["env"] and seen["marker"] == c.marker, \
        "it read its shim's argv and environment"
    assert any(p.endswith("shim.log") for p in seen["files"]), "and its diagnostics log"
    for _ in range(5):
        assert c.shim_poll() is None and not c.empty() and c.poll() is None and not c.lost(), \
            "a marker the CLI wrote proves nothing"
        time.sleep(0.05)
    key = secret_text(c)
    for name, text in [("argv", seen["argv"]), ("env", seen["env"]), *seen["files"].items()]:
        assert key not in text and key.upper() not in text, f"the secret is not in {name}"
    assert seen["forged"], "it made forgeries"
    for line in seen["forged"]:
        put_marker(c, line.encode())
        assert shimchild.read_marker(c.marker, cred(c), c.boot) is None, line
        assert not c.empty()
    last = Path(c.marker).read_bytes()
    Path(go).write_text("")
    # (review round 1, R1) A forgery left AT the marker path is never replaced: the shim publishes nothing, `lost()`.
    assert shim_exit(c) == groupshim.EXIT_MARKER_FAILED, c.events
    assert Path(c.marker).read_bytes() == last and ("MARKER_FAILED", "17") in c.events
    assert not c.empty() and c.lost() and c.poll() == 0
    log = c.dir / "shim.log"
    assert wait_for(lambda: "exit 4" in log.read_text())
    assert key not in log.read_text() and key not in " ".join(" ".join(e) for e in c.events)


def test_f1_a_forgery_put_back_by_an_escaped_descendant_is_no_proof(kit):
    """F1: the CLI leaves a descendant outside its group (`setsid`, as Claude Code's tool commands run) that waits for
    the shim's own marker and puts its forgery back in its place. The shim proved the group gone and exited 0, but
    the marker it is read with is no longer authentic: `empty()` is False and the launch is `lost()` (held). (On
    06866a5 the forgery, carrying the nonce from the argv, is proof.)"""
    c = kit.start()
    rep, done = kit.path("forger-report"), kit.path("escaped-done")
    c.go(forger_argv(kit, look=str(c.dir), report=rep, escape=done, max=30), cwd=str(kit.work), env=ENV,
         kill_grace=1.0)
    assert wait_for(lambda: os.path.exists(done)), "the escaped descendant replaced the shim's marker"
    assert shim_exit(c) == groupshim.EXIT_EMPTY, c.events
    assert not c.empty() and c.lost() and c.poll() == 0
    assert shimchild.read_marker(c.marker, cred(c), c.boot) is None


def test_f1_a_marker_replayed_with_one_field_changed_is_no_proof(kit):
    """F1: the shim's own marker for this launch, with ONE field changed (the CLI's exit code, its pid, the boot, or
    one digit of the MAC), is refused; put back unchanged, it is proof again. (On 06866a5 a changed exit code is
    accepted.)"""
    c = kit.run(["/bin/sh", "-c", "exit 5"])
    assert shim_exit(c) == groupshim.EXIT_EMPTY     # (its status observed; the proof not asked for yet)
    good = Path(c.marker).read_bytes()
    rec = json.loads(good)
    assert shimchild.read_marker(c.marker, cred(c), c.boot) == rec and rec["leader_exit"] == 5
    other_boot = "00000000-0000-4000-8000-000000000000"
    changes = [("leader_exit", 0), ("cli_pid", rec["cli_pid"] + 1), ("boot", other_boot)]
    if "mac" in rec:
        changes.append(("mac", ("0" if rec["mac"][0] != "0" else "1") + rec["mac"][1:]))
    for field, value in changes:
        put_marker(c, json.dumps({**rec, field: value}).encode() + b"\n")
        assert shimchild.read_marker(c.marker, cred(c), c.boot) is None, field
    put_marker(c, json.dumps({**rec, "leader_exit": 0}).encode() + b"\n")
    assert not c.empty() and c.lost(), "a changed exit code is no proof"
    put_marker(c, good)
    assert c.empty() and not c.lost() and c.poll() == 5


def test_f1_a_marker_copied_from_another_launch_is_no_proof(kit):
    """F1: two launches on the same boot, both proved; A's marker copied over B's is no proof for B. (This already
    held on 06866a5, by the nonce: a regression pin.)"""
    a, b = kit.run(["/bin/sh", "-c", "exit 0"]), kit.run(["/bin/sh", "-c", "exit 0"])
    assert shim_exit(a) == shim_exit(b) == groupshim.EXIT_EMPTY
    put_marker(b, Path(a.marker).read_bytes())
    assert not b.empty() and b.lost() and a.empty()
    assert shimchild.read_marker(b.marker, cred(b), b.boot) is None


@pytest.mark.parametrize("case", ["empty", "never_started", "unsynced-empty", "unsynced-never_started"])
def test_f1_each_proof_bearing_pair_is_accepted(kit, case):
    """F1.4: the release pairs, each with a real shim: exit 0 with `empty`; exit 3 (never started) with
    `never_started`; exit 6 (the directory's sync failed, injected through the test gate) with a visible `empty` or
    `never_started`. (On 06866a5 the injection does not exist: the status is 0 or 3, never 6.)"""
    unsynced = case.startswith("unsynced")
    c = kit.start(gate_files=("fail-dir-sync",) if unsynced else ())
    outcome = case.split("-")[-1]
    if outcome == "never_started":
        c.close_commands()
        status = groupshim.EXIT_UNSYNCED if unsynced else groupshim.EXIT_NOGO
    else:
        c.go(["/bin/sh", "-c", "exit 4"], cwd=str(kit.work), env=ENV, kill_grace=1.0)
        status = groupshim.EXIT_UNSYNCED if unsynced else groupshim.EXIT_EMPTY
    assert shim_exit(c) == status, c.events
    assert c.empty() and not c.lost()
    assert shimchild.read_marker(c.marker, cred(c), c.boot)["outcome"] == outcome
    assert c.poll() == (4 if outcome == "empty" else None)
    if unsynced:
        assert any(e[0] == "UNSYNCED" for e in c.events), c.events


@pytest.mark.parametrize("case", ["alive", "crashed", "killed", "nogo-with-empty", "empty-with-never_started"])
def test_f1_every_other_pair_is_refused(kit, case):
    """F1.4: a valid, authenticated marker for this launch (written by the test, which holds the secret) is no proof
    without the shim's own terminal status of the matching kind: with the shim still running; after a crash before the
    fork (exit 5, injected through the test gate); after the shim was killed; with exit 3 and `empty`; with exit 0 and
    `never_started`. `poll()` takes no exit code from it either. (On 06866a5 the marker alone is proof.)"""
    if case == "alive":
        c = kit.run(kit.argv(wait_abort=True))
        assert wait_event(c, "STARTED")
        put_marker(c, valid_record(c, "empty", c.cli_pid, 7))
        for _ in range(5):
            assert not c.empty() and c.poll() is None and not c.lost() and c.shim_poll() is None
            time.sleep(0.05)
        return
    if case == "crashed":
        c = kit.start(gate_files=("crash-before-go",))
        assert shim_exit(c) == groupshim.EXIT_CRASH, c.events
        put_marker(c, valid_record(c, "never_started"))
    elif case == "killed":
        c = kit.run(kit.argv(wait_abort=True))
        assert wait_event(c, "STARTED")
        c._kill_shim()                      # (its own handle)
        assert shim_exit(c) == -signal.SIGKILL
        Path(kit.abort).write_text("1")     # (the orphaned CLI stops by itself)
        put_marker(c, valid_record(c, "empty", c.cli_pid, 7))
    elif case == "nogo-with-empty":
        c = kit.start()
        c.close_commands()
        assert shim_exit(c) == groupshim.EXIT_NOGO
        put_marker(c, valid_record(c, "empty", 4242, 7))
    else:
        c = kit.run(["/bin/sh", "-c", "exit 0"])
        assert shim_exit(c) == groupshim.EXIT_EMPTY
        put_marker(c, valid_record(c, "never_started"))
    assert shimchild.read_marker(c.marker, cred(c), c.boot) is not None, "the marker itself is authentic"
    assert not c.empty() and c.lost()
    assert c.poll() != 7


NONCE_OK = b"NONCE 1 " + b"ab" * 32 + b"\n"


@pytest.mark.parametrize("case,reason", [
    ("missing", "eof"), ("go-first", "overlong"), ("command-first", "invalid"), ("partial", "partial"),
    ("overlong", "overlong"),
    ("not-hex", "invalid"), ("short", "invalid"), ("upper-case", "invalid"), ("version", "version"),
    ("extra-field", "invalid"), ("duplicate", "duplicate")])
def test_f1_a_missing_duplicate_overlong_or_malformed_nonce_starts_nothing(kit, case, reason):
    """F1.1: the secret arrives in ONE bounded `NONCE` frame before `GO`. Without exactly one valid frame nothing is
    forked: `NOSECRET <reason>`, exit 7, and NO marker (none could be authenticated). The node tells "never started"
    from the shim's own exit status (its unreaped child: the CLI cannot forge it): `refused()`, neither `empty()` nor
    `lost()`. (On 06866a5 there is no NONCE frame.)"""
    started = kit.path("started")
    c = kit.start(send_secret=False)
    go = _frame(kit.argv(started=started), str(kit.work))
    frames = {"missing": b"", "go-first": go, "command-first": b"TERM\n" + NONCE_OK + go, "partial": NONCE_OK[:20],
              "overlong": b"NONCE 1 " + b"a" * 200, "not-hex": b"NONCE 1 " + b"zz" * 32 + b"\n" + go,
              "short": b"NONCE 1 " + b"ab" * 31 + b"\n" + go, "upper-case": b"NONCE 1 " + b"AB" * 32 + b"\n" + go,
              "version": b"NONCE 2 " + b"ab" * 32 + b"\n" + go, "extra-field": NONCE_OK[:-1] + b" x\n" + go,
              "duplicate": NONCE_OK + NONCE_OK + go}
    raw_write(c, frames[case])
    c.close_commands()
    assert shim_exit(c) == groupshim.EXIT_NOSECRET, c.events
    assert c.events == [("NOSECRET", reason)] and c.nosecret == reason
    assert c.refused() and not c.empty() and not c.lost() and c.poll() is None
    assert not os.path.exists(c.marker) and not [n for n in os.listdir(c.dir) if "marker" in n]
    time.sleep(0.2)
    assert not os.path.exists(started), "the CLI never started"


def test_f2_a_crash_after_the_fork_still_stops_the_group_and_proves_it(kit):
    """F2: an unexpected exception in the shim's loop after the fork (injected through the test gate), while the CLI
    and its member both ignore TERM and a TERM was already sent 1.2 s before. The degraded stop sends TERM again, KILL
    at the ORIGINAL first TERM + the grace (not the second's), and goes the same verified way to the marker: exit 0,
    `empty()`. (On 06866a5 the shim exits 5 at once, the group untouched; there the injection does not exist, and the
    group is never stopped within the bound.)"""
    grace = 2.0
    up, cli_term, member_term, mrep = (kit.path(n) for n in ("up", "cli-term", "member-term", "member-report"))
    c = kit.start(gate_files=("armed",))
    c.go(kit.argv(ignore_term=True, term_file=cli_term, started=up, wait_abort=True,
                  member={"term_file": member_term, "report": mrep, "close_stdio": True}),
         cwd=str(kit.work), env=ENV, kill_grace=grace)
    assert wait_event(c, "STARTED") and wait_for(lambda: os.path.exists(up) and read_json(mrep))
    c.terminate()
    assert wait_event(c, "SENT") == ("SENT", "TERM", "group")
    time.sleep(1.2)
    (c.gate_dir / "crash-in-loop").write_text("")
    raw_write(c, b"NOP\n")                  # (wakes the loop: it crashes at its next turn)
    assert shim_exit(c, 30) == groupshim.EXIT_EMPTY, c.events
    assert ("CRASHED",) in c.events and c.empty() and not c.lost() and c.poll() == -signal.SIGKILL
    log = c.dir / "shim.log"
    assert wait_for(lambda: "exit 0" in log.read_text())
    assert "crashed after the fork" in log.read_text()
    terms = diag_times(log, f"killpg({c.cli_pid}, TERM) sent")
    kills = diag_times(log, f"killpg({c.cli_pid}, KILL) sent")
    assert len(terms) == 2 and kills, (terms, kills)
    assert kills[0] - terms[0] >= grace, "no KILL before the ORIGINAL first TERM + the grace"
    assert kills[0] < terms[1] + grace - 0.3, "the crash did not restart the grace"
    assert os.path.exists(member_term), "the TERM reached the member"
    assert not node_groups.group_members(c.cli_pid), "the group is gone"


def test_f2_a_crash_that_leaves_no_anchor_signals_nothing_and_publishes_nothing(kit):
    """F2: the CLI has exited and its member (which notes every TERM) keeps the group. An injected crash (test gate)
    REAPS the leader first: the anchor is gone, so the degraded stop cannot establish it. It signals nothing (the
    member never gets a TERM and lives on) and publishes no marker: exit 5, `lost()`. (On 06866a5 the injection does
    not exist: the shim keeps waiting for the member.)"""
    mt, mrep = kit.path("member-term"), kit.path("member-report")
    c = kit.start(gate_files=("armed",))
    c.go(kit.argv(member={"term_file": mt, "report": mrep, "close_stdio": True}), cwd=str(kit.work), env=ENV,
         kill_grace=0.5)
    assert wait_event(c, "LEADER_EXIT") and wait_for(lambda: read_json(mrep))
    member = read_json(mrep)["pid"]
    (c.gate_dir / "crash-reap").write_text("")
    assert shim_exit(c) == groupshim.EXIT_CRASH, c.events
    time.sleep(0.5)
    assert not os.path.exists(mt), "no TERM reached the member"
    k = kinfo(member)
    assert k is not None and k[0] != SZOMB, "the member lives on, unsignalled"
    assert not os.path.exists(c.marker) and c.lost() and not c.empty()
    assert ("CRASHED",) in c.events and not any(e[0] in ("SENT", "FAILED") for e in c.events), c.events
    log = c.dir / "shim.log"
    assert wait_for(lambda: "anchor cannot be established" in log.read_text())


@pytest.mark.parametrize("kind", ["file", "dir", "symlink", "dangling-symlink", "fifo"])
def test_f3_start_refuses_an_existing_marker_path_and_leaves_it_alone(kit, monkeypatch, kind):
    """F3: the handle names each launch's marker afresh in the node's private marker directory; if that path exists in
    any form, `start()` refuses it before anything is spawned, and neither overwrites nor deletes it. (On 06866a5 there
    is no marker directory: a caller's path was taken as given, and the shim's rename replaced whatever was there.)"""
    d = Path(shimchild.make_marker_dir(str(kit.tmp / "markers")))
    path = d / "taken.marker"
    if kind == "file":
        path.write_text("someone else's\n")
    elif kind == "dir":
        path.mkdir()
    elif kind == "symlink":
        (kit.tmp / "target").write_text("target\n")
        path.symlink_to(kit.tmp / "target")
    elif kind == "dangling-symlink":
        path.symlink_to(kit.tmp / "nowhere")
    else:
        os.mkfifo(path)
    before = os.lstat(path)
    content = path.read_text() if kind == "file" else None
    monkeypatch.setattr(shimchild, "_new_marker_name", lambda: "taken.marker")
    spawned = []
    monkeypatch.setattr(shimchild.subprocess, "Popen", lambda *a, **k: spawned.append(a))
    with pytest.raises(FileExistsError):
        ShimChild.start(marker_dir=str(d), log=kit.path("log"), stderr_log=kit.path("err"))
    assert spawned == [], "nothing was spawned"
    after = os.lstat(path)
    assert (after.st_ino, after.st_mode, after.st_mtime_ns) == (before.st_ino, before.st_mode, before.st_mtime_ns)
    if content is not None:
        assert path.read_text() == content
    assert not (kit.tmp / "nowhere").exists()


def test_f3_the_marker_directory_is_private_to_this_user(tmp_path, monkeypatch):
    """F3: `make_marker_dir` creates the directory 0700, accepts an existing private one, and refuses (changing
    nothing) a symlink, a non-directory, one any group or other bit opens, and one owned by another user (that user
    played by this test, which reports a different euid to the check). `start()` takes only such a directory."""
    d = shimchild.make_marker_dir(str(tmp_path / "markers"))
    st = os.lstat(d)
    assert stat.S_ISDIR(st.st_mode) and stat.S_IMODE(st.st_mode) == 0o700 and st.st_uid == os.geteuid()
    assert shimchild.make_marker_dir(d) == d
    link = tmp_path / "link"
    link.symlink_to(d)
    with pytest.raises(OSError):
        shimchild.make_marker_dir(str(link))
    (tmp_path / "file").write_text("")
    with pytest.raises(OSError):
        shimchild.make_marker_dir(str(tmp_path / "file"))
    for mode in (0o750, 0o705, 0o701, 0o710, 0o777):
        p = tmp_path / f"open-{mode:o}"
        p.mkdir()
        os.chmod(p, mode)
        with pytest.raises(PermissionError):
            shimchild.make_marker_dir(str(p))
        assert stat.S_IMODE(os.lstat(p).st_mode) == mode, "left as it was"
        with pytest.raises(PermissionError):
            ShimChild.start(marker_dir=str(p), log=str(tmp_path / "l"), stderr_log=str(tmp_path / "e"))
    with monkeypatch.context() as m:
        m.setattr(shimchild.os, "geteuid", lambda: os.getuid() + 1)
        with pytest.raises(PermissionError):
            shimchild.make_marker_dir(d)


def _uuid_shaped(value) -> bool:
    if type(value) is not str or len(value) != 36:
        return False
    parts = value.split("-")
    return [len(p) for p in parts] == [8, 4, 4, 4, 12] and all(c in "0123456789abcdefABCDEF" for p in parts for c in p)


def test_f4_the_boot_identity_is_this_boots_session_uuid():
    """F4: the boot identity is `kern.bootsessionuuid`: UUID-shaped, the same on two reads, the same as the system's
    own `sysctl -n kern.bootsessionuuid`, and the same in the shim and the handle. (On 06866a5 it is `kern.boottime`,
    seconds.microseconds.)"""
    a, b = groupshim.boot_id(), groupshim.boot_id()
    assert _uuid_shaped(a) and a == b, (a, b)
    r = subprocess.run(["/usr/sbin/sysctl", "-n", "kern.bootsessionuuid"], capture_output=True, text=True, timeout=10)
    assert r.returncode == 0 and r.stdout.strip() == a, (r.stdout, a)
    assert shimchild.boot_id() == a


@pytest.mark.parametrize("how", ["unreadable", "wall-clock-shaped", "not-a-string"])
def test_f4_a_boot_identity_that_cannot_be_read_or_validated_refuses_the_launch(tmp_path, monkeypatch, how):
    """F4: the read fails (an unknown sysctl name injected), or gives something that is not a UUID (a boottime-shaped
    value, or bytes that do not decode): `boot_id()` is None, never a fallback, and `start()` raises before spawning
    anything."""
    if how == "unreadable":
        monkeypatch.setattr(groupshim, "BOOT_SYSCTL", b"kern.no_such_name_c32a")
    elif how == "wall-clock-shaped":
        monkeypatch.setattr(groupshim, "_sysctl_string", lambda name: "1764300000.123456")
    else:
        monkeypatch.setattr(groupshim, "_sysctl_string", lambda name: None)
    assert groupshim.boot_id() is None
    spawned = []
    monkeypatch.setattr(shimchild.subprocess, "Popen", lambda *a, **k: spawned.append(a))
    d = shimchild.make_marker_dir(str(tmp_path / "markers"))
    with pytest.raises(OSError):
        ShimChild.start(marker_dir=d, log=str(tmp_path / "l"), stderr_log=str(tmp_path / "e"))
    assert spawned == [] and os.listdir(d) == []


def test_f4_a_shim_that_cannot_read_the_boot_identity_starts_nothing(kit):
    """F4, in the shim: its own read of the boot identity fails (injected through the test gate): exit 2 before any
    frame is read, nothing started, no marker; `refused()`."""
    started = kit.path("started")
    c = kit.start(gate_files=("fail-boot",))
    try:
        c.go(kit.argv(started=started), cwd=str(kit.work), env=ENV, kill_grace=1.0)
    except OSError:
        pass                                # (the shim may be gone already: EPIPE)
    assert shim_exit(c) == groupshim.EXIT_USAGE, c.events
    assert c.refused() and not c.empty() and not c.lost() and not os.path.exists(c.marker)
    time.sleep(0.2)
    assert not os.path.exists(started)


def _is_os_exit(stmts) -> bool:
    return (len(stmts) == 1 and isinstance(stmts[0], ast.Expr) and isinstance(stmts[0].value, ast.Call)
            and ast.unparse(stmts[0].value.func) == "os._exit")


def _body(fn):
    body = fn.body
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
        body = body[1:]                     # (the docstring)
    return body


def test_f5_every_statement_of_the_forked_child_is_inside_its_failure_boundary():
    """F5 (static): from the fork to the exec, the child runs only inside one `try` whose handlers take every exception
    (`BaseException`) and whose `finally` is `os._exit(...)` alone, in `_child` and around its call in `Shim.spawn`;
    and nothing in the child (`_child`, `_child_gate`) can leave through a Python exit path (`sys.exit`, `exit`,
    `quit`, `raise SystemExit`). (On 06866a5 `_child` assigns its stage and closes the shim's ends BEFORE its `try`.)"""
    tree = ast.parse(Path(groupshim.__file__).read_text())
    funcs = {n.name: n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
    body = _body(funcs["_child"])
    assert len(body) == 1 and isinstance(body[0], ast.Try), [ast.unparse(s)[:60] for s in body]
    boundary = body[0]
    assert _is_os_exit(boundary.finalbody), ast.unparse(boundary)[-200:]
    assert any(h.type is not None and ast.unparse(h.type) == "BaseException" for h in boundary.handlers)
    for name in ("_child", "_child_gate"):
        for node in ast.walk(funcs[name]):
            if isinstance(node, ast.Call):
                assert ast.unparse(node.func) not in ("sys.exit", "exit", "quit", "os.abort"), (name, ast.unparse(node))
            if isinstance(node, ast.Raise) and node.exc is not None:
                assert "SystemExit" not in ast.unparse(node.exc), (name, ast.unparse(node))
    spawn = next(n for n in ast.walk(funcs["spawn"]) if isinstance(n, ast.If) and ast.unparse(n.test) == "pid == 0")
    assert len(spawn.body) == 1 and isinstance(spawn.body[0], ast.Try), ast.unparse(spawn)
    inner = spawn.body[0]
    assert len(inner.body) == 1 and ast.unparse(inner.body[0]).startswith("_child(")
    assert _is_os_exit(inner.finalbody)


# ---------------------------------------------------------------- C3-2a review round 1 (Sol on 7047f3a)

@pytest.mark.parametrize("outcome", ["empty", "never_started"])
def test_rr1_publishing_never_replaces_a_file_at_the_marker_path(kit, outcome):
    """R1: a same-user process learns the marker path (it is on the shim's argv) and puts a file there while the CLI
    runs (or before GO). The shim's publish does not replace it: the file is there byte for byte afterwards, no marker
    of ours exists, `MARKER_FAILED 17` (EEXIST) and exit 4, in no release pair: `empty()` False, `lost()` (held). No
    temp file of ours is left. (On 7047f3a the shim's rename replaces the file and exits 0: proof.)"""
    squat = b"someone else's file\n"
    if outcome == "never_started":
        c = kit.start()
        Path(c.marker).write_bytes(squat)
        c.close_commands()
    else:
        go = kit.path("cli-go")
        c = kit.run(kit.argv(until=go))
        assert wait_event(c, "STARTED")
        Path(c.marker).write_bytes(squat)
        Path(go).write_text("")
    assert shim_exit(c) == groupshim.EXIT_MARKER_FAILED, c.events
    assert Path(c.marker).read_bytes() == squat, "not replaced"
    assert ("MARKER_FAILED", "17") in c.events and ("EMPTY",) not in c.events, c.events
    assert not c.empty() and c.lost() and not c.refused()
    assert not [n for n in os.listdir(c.dir) if n.endswith(".tmp")], "no temp file of ours left behind"


def test_rr1_a_temp_name_the_shim_did_not_create_is_never_unlinked(kit):
    """R1: a file sits at the shim's own temp name (`<marker>.<shim pid>.tmp`, guessable) when it publishes. The
    exclusive create fails, and the shim leaves that file alone: it is there byte for byte, nothing is published,
    exit 4, `lost()`. (On 7047f3a the shim unlinks it after its failed create.)"""
    go = kit.path("cli-go")
    c = kit.run(kit.argv(until=go))
    assert wait_event(c, "STARTED")
    tmp = Path(f"{c.marker}.{c.pid}.tmp")
    tmp.write_bytes(b"not the shim's\n")
    Path(go).write_text("")
    assert shim_exit(c) == groupshim.EXIT_MARKER_FAILED, c.events
    assert tmp.read_bytes() == b"not the shim's\n", "not unlinked"
    assert not os.path.exists(c.marker) and not c.empty() and c.lost()


STALLING_PYTHON = r'''#!/bin/sh
# TEST-ONLY: the real interpreter for the shim, with every way of describing a crash made to stall (until `abort`):
# the traceback module's formatters, linecache, and the shim's own rendering function.
shift 2
exec PY -I -c '
import os, sys, time
sys.path.insert(0, sys.argv[2])
def stall(*a, **k):
    end = time.monotonic() + 60
    while time.monotonic() < end and not os.path.exists(ABORT):
        time.sleep(0.05)
    return ""
import traceback, linecache
for name in ("format_exc", "format_exception", "format_exception_only", "format_tb", "format_stack", "extract_tb",
             "extract_stack", "print_exc", "print_exception", "print_tb"):
    setattr(traceback, name, stall)
linecache.getline = linecache.getlines = linecache.updatecache = linecache.checkcache = stall
from agent_comms import groupshim
groupshim.render_crash = stall
exec(compile(sys.argv.pop(1), "<boot>", "exec"))
' "$@"
'''


def test_rr2_a_stalled_crash_description_never_holds_up_the_degraded_stop(kit):
    """R2: the shim runs under an interpreter whose every crash formatter stalls (the traceback module, linecache, and
    the shim's own `render_crash`). After an injected crash past the fork, with the CLI and its member ignoring TERM,
    the degraded stop's TERM comes at once, the KILL at the grace, then the proof and the marker: exit 0 within
    seconds. (On 7047f3a `traceback.format_exc()` runs first and stalls: no TERM for a minute.)"""
    wrapper = kit.tmp / "stalling-python"
    wrapper.write_text(STALLING_PYTHON.replace("PY", PY, 1).replace("ABORT", json.dumps(kit.abort)))
    os.chmod(wrapper, 0o755)
    up, cli_term, member_term, mrep = (kit.path(n) for n in ("up", "cli-term", "member-term", "member-report"))
    c = kit.start(gate_files=("armed",), python=str(wrapper))
    c.go(kit.argv(ignore_term=True, term_file=cli_term, started=up, wait_abort=True,
                  member={"term_file": member_term, "report": mrep, "close_stdio": True}),
         cwd=str(kit.work), env=ENV, kill_grace=1.0)
    assert wait_event(c, "STARTED") and wait_for(lambda: os.path.exists(up) and read_json(mrep))
    (c.gate_dir / "crash-in-loop").write_text("")
    t0 = time.monotonic()
    raw_write(c, b"NOP\n")                  # (wakes the loop: it crashes at its next turn)
    assert wait_for(lambda: os.path.exists(cli_term), 5), "the degraded stop's TERM came at once"
    assert shim_exit(c, 10) == groupshim.EXIT_EMPTY, c.events
    assert time.monotonic() - t0 < 8.0
    assert ("CRASHED",) in c.events and ("SENT", "KILL", "group") in c.events and c.empty()
    assert os.path.exists(member_term)


@pytest.mark.parametrize("case", ["proved", "refused"])
def test_rr3_a_shim_reaped_by_another_waiter_has_an_unknown_status_and_proves_nothing(kit, case):
    """R3: the shim exits (0 with its marker, or 7 refused) and ANOTHER waiter in this process reaps it before the
    handle looks. The handle never saw a status: UNKNOWN, which is no release pair and no refusal: `empty()` False,
    `refused()` False, `lost()` True (held). (On 7047f3a `Popen.poll()` turns ECHILD into exit 0: `empty()` True.)"""
    if case == "proved":
        c = kit.run(["/bin/sh", "-c", "exit 0"])
    else:
        c = kit.start(send_secret=False)
        c.close_commands()
    assert wait_for(lambda: groupshim.waitid_nowait(c.pid) is not None), "the shim exited (unreaped)"
    if case == "proved":
        assert wait_for(lambda: os.path.exists(c.marker))
    assert os.waitpid(c.pid, os.WNOHANG)[0] == c.pid        # (another waiter takes the status)
    assert not c.empty() and not c.refused() and c.lost()
    assert c.shim_poll() == shimchild.STATUS_UNKNOWN
    assert shimchild.read_marker(c.marker, c._key, c.boot) is not None or case == "refused"


def test_rr3_popen_cannot_reap_the_shim_behind_the_handle(kit):
    """R3: the shim has exited, unreaped. Its `Popen`'s own `poll()` and `wait()` do not reap it (the handle owns the
    wait status): it is still this process's unreaped child afterwards, and the handle then observes its real status.
    (On 7047f3a `Popen.poll()` reaps it.)"""
    c = kit.run(["/bin/sh", "-c", "exit 0"])
    assert wait_for(lambda: groupshim.waitid_nowait(c.pid) is not None), "the shim exited (unreaped)"
    p = c._popen
    p.poll()
    p.wait(timeout=1)
    p.kill()                                # (a no-op too: Popen signals nothing it thinks is over)
    assert groupshim.waitid_nowait(c.pid) == 0, "still unreaped: Popen did not take its status"
    assert c.shim_poll() == groupshim.EXIT_EMPTY and c.empty()
