"""Ownership C3-2a, F6: every spawn in the package closes inherited descriptors.

macOS has no `pipe2`, so a shim's command pipe is made in two steps (`pipe()`, then close-on-exec). A fork elsewhere
in the node that KEEPS descriptors could inherit the node's command writer in that window, and the shim would then
never see EOF when the node dies. So, across `agent_comms/` (static, by AST; runs on every platform):
- no call passes `close_fds` as anything but the literal `True` (whatever it calls: a `Popen` held in a variable too),
  and no `subprocess` spawn call takes `**kwargs` (which could carry `close_fds=False`);
- no `os.fork`, `os.forkpty`, `os.posix_spawn*`, `os.spawn*`, `os.system`, `os.popen`, `pty`, `multiprocessing` or
  `_posixsubprocess`, outside the allow-list: the shim's own single fork (`Shim.spawn` in groupshim.py).
It cannot see a third-party library that forks (DESIGN_NOTES "Ownership C3-2a")."""
from __future__ import annotations

import ast
from pathlib import Path

PACKAGE = Path(__file__).resolve().parent.parent / "agent_comms"
SPAWN_CALLS = {"Popen", "run", "call", "check_call", "check_output"}
OS_FORKS = {"fork", "forkpty", "posix_spawn", "posix_spawnp", "system", "popen", "spawnl", "spawnle", "spawnlp",
            "spawnlpe", "spawnv", "spawnve", "spawnvp", "spawnvpe"}
BANNED_MODULES = {"multiprocessing", "pty", "_posixsubprocess", "concurrent.futures.process"}
# (file, function, what): the one fork the package may make. The shim forks the CLI once, in a single-threaded
# process, and the child closes every descriptor but its stdio and the handshake before its exec.
ALLOWED = {("agent_comms/groupshim.py", "Shim.spawn", "os.fork")}


def violations(source: str, rel: str) -> list[tuple[str, str, str]]:
    """(file, function, what) for every spawn that may keep descriptors, or is not on the allow-list."""
    tree = ast.parse(source)
    os_names, sub_names, sub_funcs = {"os"}, {"subprocess"}, {}
    out: list[tuple[str, str, str]] = []

    def visit(node, where: str) -> None:
        for child in ast.iter_child_nodes(node):
            inner = where
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                inner = f"{where}.{child.name}" if where else child.name
            check(child, inner or "<module>")
            visit(child, inner)

    def check(node, where: str) -> None:
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name == "os":
                    os_names.add(a.asname or "os")
                if a.name == "subprocess":
                    sub_names.add(a.asname or "subprocess")
                if a.name in BANNED_MODULES or a.name.split(".")[0] in BANNED_MODULES:
                    out.append((rel, where, f"import {a.name}"))
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if mod in BANNED_MODULES or mod.split(".")[0] in BANNED_MODULES:
                out.append((rel, where, f"from {mod} import"))
            for a in node.names:
                if mod == "os" and a.name in OS_FORKS:
                    out.append((rel, where, f"from os import {a.name}"))
                if mod == "subprocess" and a.name in SPAWN_CALLS:
                    sub_funcs[a.asname or a.name] = a.name
                if mod == "concurrent.futures" and a.name == "ProcessPoolExecutor":
                    out.append((rel, where, "ProcessPoolExecutor"))
        elif isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            if node.value.id in os_names and node.attr in OS_FORKS:
                what = f"os.{node.attr}"
                if (rel, where, what) not in ALLOWED:
                    out.append((rel, where, what))
        elif isinstance(node, ast.Call):
            for kw in node.keywords:
                if kw.arg == "close_fds" and not (isinstance(kw.value, ast.Constant) and kw.value.value is True):
                    out.append((rel, where, f"close_fds={ast.unparse(kw.value)}"))
            f = node.func
            spawn = (isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name) and f.value.id in sub_names
                     and f.attr in SPAWN_CALLS) or (isinstance(f, ast.Name) and f.id in sub_funcs)
            if spawn and any(kw.arg is None for kw in node.keywords):
                out.append((rel, where, f"{ast.unparse(f)}(**...)"))

    visit(tree, "")
    return out


def test_no_spawn_in_the_package_keeps_inherited_descriptors():
    found = []
    allowed_seen = set()
    for path in sorted(PACKAGE.rglob("*.py")):
        rel = path.relative_to(PACKAGE.parent).as_posix()
        source = path.read_text()
        found += violations(source, rel)
        for (frel, func, what) in ALLOWED:
            if frel == rel and what in source:
                allowed_seen.add((frel, func, what))
    assert found == [], found
    assert allowed_seen == ALLOWED, "the allow-list names a fork that still exists"


def test_the_allowed_fork_is_the_shims_single_one():
    """The allow-list is exactly one fork: `Shim.spawn` holds one `os.fork()` call, and nothing else in groupshim.py
    forks."""
    source = (PACKAGE / "groupshim.py").read_text()
    tree = ast.parse(source)
    forks = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and ast.unparse(n.func) == "os.fork"]
    assert len(forks) == 1, [ast.unparse(n) for n in forks]
    spawn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "spawn")
    assert forks[0] in list(ast.walk(spawn))


def test_the_scan_sees_each_kind_of_spawn_that_keeps_descriptors():
    """The scan is not vacuous: each banned form, in a synthetic module, is found."""
    cases = {
        "import subprocess\nsubprocess.Popen(['x'], close_fds=False)\n": "close_fds=False",
        "import subprocess as sp\nsp.run(['x'], close_fds=flag)\n": "close_fds=flag",
        "from subprocess import check_output as co\nco(['x'], **kw)\n": "co(**...)",
        "import subprocess\nsubprocess.call(['x'], **kw)\n": "subprocess.call(**...)",
        "popen = make()\npopen(['x'], close_fds=0)\n": "close_fds=0",
        "import os\nos.fork()\n": "os.fork",
        "import os as o\no.forkpty()\n": "os.forkpty",
        "import os\nos.posix_spawn('x', [], {})\n": "os.posix_spawn",
        "import os\nos.spawnv(0, 'x', [])\n": "os.spawnv",
        "import os\nos.system('x')\n": "os.system",
        "import os\nos.popen('x')\n": "os.popen",
        "from os import fork\n": "from os import fork",
        "import multiprocessing\n": "import multiprocessing",
        "from multiprocessing import Process\n": "from multiprocessing import",
        "import pty\n": "import pty",
        "import _posixsubprocess\n": "import _posixsubprocess",
        "from concurrent.futures import ProcessPoolExecutor\n": "ProcessPoolExecutor",
    }
    for source, what in cases.items():
        found = violations(source, "agent_comms/x.py")
        assert [w for _, _, w in found] == [what], (source, found)
    assert violations("import subprocess\nsubprocess.run(['x'], close_fds=True)\nsubprocess.run(['x'])\n",
                      "agent_comms/x.py") == []
    assert violations("import os\nclass Shim:\n    def spawn(self):\n        os.fork()\n",
                      "agent_comms/groupshim.py") == []
    assert violations("import os\ndef other():\n    os.fork()\n", "agent_comms/groupshim.py") != []
