"""Ownership C3-2b-1, F: static rules over `agent_comms/` (AST; every platform), in tests/test_spawn_discipline.py's style.

1. The node's children are reaped only by their own handles: no `signal.signal(signal.SIGCHLD, signal.SIG_IGN)`, no
   `SA_NOCLDWAIT`, no `os.wait()`, no `os.wait3`, no `os.waitpid`/`os.wait4` of pid -1 or 0 (or any pid literal at or
   below 0), no `waitid` of `P_ALL`/`P_PGID`. The handle's `os.waitpid(self.pid, ...)` (its own shim's pid) is the
   allowed form. Any of these would have the kernel or another waiter reap a shim behind its handle: its status would be
   UNKNOWN, and every supervised launch held (DESIGN_NOTES C3-2a, R3). (Not covered: a disposition set through a
   VARIABLE signal number, as the shim's forked child restores the node's inherited ignores for the CLI before its exec:
   that is the CLI's own disposition, never the node's.)
2. The tripwire: nothing in the product can start a shim in this PR. No module of the package imports the supervisor's
   handle (`shimchild`) or the launch helpers (`shimlaunch`) but those two (the helpers import the handle); `groupshim`
   is imported only by the handle; the journal (`shimjournal`) only by `node.py` and the helpers; and `node.py` uses
   nothing of the journal but its start-up `restore`. So the journal is always empty in production, and a node behaves
   exactly as before."""
from __future__ import annotations

import ast
from pathlib import Path

PACKAGE = Path(__file__).resolve().parent.parent / "agent_comms"
GUARDED = {"shimchild": {"shimlaunch.py"}, "shimlaunch": set(), "groupshim": {"shimchild.py"},
           "shimjournal": {"node.py", "shimlaunch.py"}}


def _modules():
    for path in sorted(PACKAGE.rglob("*.py")):
        yield path.relative_to(PACKAGE).as_posix(), path.read_text()


def reaping_violations(source: str, rel: str) -> list[tuple[str, int, str]]:
    tree = ast.parse(source)
    out = []

    def text(n) -> str:
        return ast.unparse(n)

    def at_or_below_zero(n) -> bool:
        if isinstance(n, ast.Constant) and isinstance(n.value, int) and not isinstance(n.value, bool):
            return n.value <= 0
        return isinstance(n, ast.UnaryOp) and isinstance(n.op, ast.USub)
    for n in ast.walk(tree):
        line = getattr(n, "lineno", 0)
        named = n.id if isinstance(n, ast.Name) else n.attr if isinstance(n, ast.Attribute) else None
        if named == "SA_NOCLDWAIT":
            out.append((rel, line, "SA_NOCLDWAIT"))
        if named in ("P_ALL", "P_PGID"):
            out.append((rel, line, "a waitid of every child or of a group"))
        if not isinstance(n, ast.Call):
            continue
        f = text(n.func)
        name = f.rsplit(".", 1)[-1]
        args = n.args
        if name == "signal" and len(args) > 1 and "SIGCHLD" in text(args[0]) and (
                "SIG_IGN" in text(args[1]) or isinstance(args[1], ast.Constant) and args[1].value == 1):
            out.append((rel, line, "SIGCHLD ignored"))
        if name == "sigaction":
            out.append((rel, line, "sigaction (SA_NOCLDWAIT is set through it)"))
        if f == "os.wait":
            out.append((rel, line, "os.wait()"))
        if f in ("os.wait3", "wait3"):
            out.append((rel, line, "os.wait3 (any child)"))
        if f in ("os.waitpid", "os.wait4", "waitpid", "wait4"):
            pid = args[0] if args else next((k.value for k in n.keywords if k.arg == "pid"), None)
            if pid is None or at_or_below_zero(pid):
                out.append((rel, line, f"{f} of pid {text(pid) if pid is not None else '?'}"))
    return out


def test_no_module_reaps_any_child_but_its_own_or_has_the_kernel_reap_them():
    found = [v for rel, src in _modules() for v in reaping_violations(src, rel)]
    assert found == [], found


def test_the_handles_own_waitpid_is_the_allowed_form():
    src = (PACKAGE / "shimchild.py").read_text()
    assert "os.waitpid(self.pid, os.WNOHANG)" in src
    assert reaping_violations(src, "shimchild.py") == []


def test_the_reaping_scan_sees_each_banned_form():
    for bad in ["import signal\nsignal.signal(signal.SIGCHLD, signal.SIG_IGN)\n",
                "import signal\nsignal.signal(signal.SIGCHLD, signal.Handlers.SIG_IGN)\n",
                "from signal import signal, SIGCHLD, SIG_IGN\nsignal(SIGCHLD, SIG_IGN)\n",
                "import signal\nsignal.signal(signal.SIGCHLD, 1)\n",
                "x = SA_NOCLDWAIT\n", "import ctypes\nlib.sigaction(20, act, None)\n",
                "import os\nos.wait()\n", "import os\nos.wait3(0)\n",
                "import os\nos.waitpid(-1, 0)\n", "import os\nos.waitpid(0, os.WNOHANG)\n",
                "import os\nos.wait4(-1, 0)\n", "import os\nos.wait4(0, 0)\n", "import os\nos.waitpid(-pgid, 0)\n",
                "import os\nos.waitid(os.P_ALL, 0, os.WEXITED)\n", "lib.waitid(P_PGID, 7, info, 4)\n"]:
        assert reaping_violations(bad, "fixture.py"), bad
    for good in ["import os\nos.waitpid(self.pid, os.WNOHANG)\n", "import os\nos.waitpid(pid, 0)\n",
                 "import signal\nsignal.signal(signal.SIGCHLD, signal.SIG_DFL)\n",
                 "lib.waitid(_P_PID, pid, info, 4)\n"]:
        assert reaping_violations(good, "fixture.py") == [], good


def _imports(source: str) -> list[tuple[str, int]]:
    """(module name, line) for every import of an agent_comms module: `from . import x`, `from .x import`, `import
    agent_comms.x`, `from agent_comms import x`, `from agent_comms.x import`, and `importlib.import_module` /
    `__import__` of a constant name."""
    tree = ast.parse(source)
    out = []
    for n in ast.walk(tree):
        line = getattr(n, "lineno", 0)
        if isinstance(n, ast.ImportFrom):
            mod = n.module or ""
            if n.level and not mod or mod == "agent_comms":
                out += [(a.name, line) for a in n.names]
            elif n.level or mod.startswith("agent_comms."):
                out.append((mod.split(".")[-1] if not n.level else mod.split(".")[0], line))
        elif isinstance(n, ast.Import):
            out += [(a.name.split(".")[1], line) for a in n.names if a.name.startswith("agent_comms.")]
        elif isinstance(n, ast.Call) and ast.unparse(n.func) in ("importlib.import_module", "__import__",
                                                                  "import_module"):
            if n.args and isinstance(n.args[0], ast.Constant) and isinstance(n.args[0].value, str):
                out.append((n.args[0].value.lstrip(".").split(".")[-1], line))
    return out


def test_nothing_in_the_product_can_start_a_shim_in_this_pr():
    """The tripwire (see the module text)."""
    found = []
    for rel, src in _modules():
        for mod, line in _imports(src):
            if mod in GUARDED and rel not in GUARDED[mod] and rel != f"{mod}.py":
                found.append((rel, line, mod))
    assert found == [], found
    importers = {mod: {rel for rel, src in _modules() if any(m == mod for m, _ in _imports(src))} for mod in GUARDED}
    assert importers == {"shimchild": {"shimlaunch.py"}, "shimlaunch": set(), "groupshim": {"shimchild.py"},
                         "shimjournal": {"node.py", "shimlaunch.py"}}, importers


def test_node_uses_nothing_of_the_journal_but_its_restore():
    tree = ast.parse((PACKAGE / "node.py").read_text())
    used = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)
            and n.value.id == "shimjournal"}
    assert used == {"restore"}, used
    assert not any(isinstance(n, ast.Name) and n.id in ("commit", "Journal", "launch") for n in ast.walk(tree))


def test_the_tripwire_scan_sees_each_import_form():
    for src, want in [("from . import shimlaunch\n", "shimlaunch"), ("from .shimchild import ShimChild\n", "shimchild"),
                      ("import agent_comms.shimlaunch\n", "shimlaunch"),
                      ("from agent_comms import shimchild\n", "shimchild"),
                      ("from agent_comms.shimlaunch import launch\n", "shimlaunch"),
                      ("import importlib\nimportlib.import_module('agent_comms.shimlaunch')\n", "shimlaunch"),
                      ("__import__('agent_comms.shimchild')\n", "shimchild"),
                      ("def f():\n    from . import groupshim\n", "groupshim")]:
        assert want in [m for m, _ in _imports(src)], src
