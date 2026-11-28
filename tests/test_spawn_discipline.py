"""Ownership C3-2a, F6 (and review round 1, R4): every spawn in the package closes inherited descriptors.

macOS has no `pipe2`, so a shim's command pipe is made in two steps (`pipe()`, then close-on-exec). A fork elsewhere
in the node that KEEPS descriptors could inherit the node's command writer in that window, and the shim would then
never see EOF when the node dies. So, across `agent_comms/` (static, by AST; runs on every platform), only REVIEWED
DIRECT spawn forms:
- a subprocess spawn function (`Popen`, `run`, `call`, `check_call`, `check_output`, `getoutput`, `getstatusoutput`)
  is used only as the callee of a direct call, `subprocess.<f>(...)` (the module imported as `subprocess`, never
  renamed) or `<f>(...)` imported by `from subprocess import <f>` (never renamed, never `*`);
- at that call: exactly ONE positional argument (the argv; no `*args`), no `**kwargs`, and `close_fds` either absent or
  the literal `True` given by keyword;
- no other reference hands a spawn function on (an assignment, an alias, a default argument, an attribute lookup, a
  container, `getattr(subprocess, ...)`, `subprocess.__dict__`), except the two ALLOW-LISTED sites below, each pinned
  by its file and the exact text of the statement, and each with its one spawning call pinned by exact text too;
- a reference inside a TYPE ANNOTATION is exempt (it names the type, it calls nothing): a node is in an annotation when
  it lies in the subtree of a parameter's `annotation`, a function's `returns`, or an annotated assignment's
  `annotation` (`proc: subprocess.Popen`); a parameter's DEFAULT is not an annotation;
- any call anywhere that passes `close_fds` passes the literal `True`;
- no `os.fork`, `os.forkpty`, `os.posix_spawn*`, `os.spawn*`, `os.system`, `os.popen`, `pty`, `multiprocessing`,
  `_posixsubprocess` or `ProcessPoolExecutor`, outside the shim's own single fork (`Shim.spawn` in groupshim.py).
It cannot see a third-party library that forks (DESIGN_NOTES "Ownership C3-2a")."""
from __future__ import annotations

import ast
from pathlib import Path

PACKAGE = Path(__file__).resolve().parent.parent / "agent_comms"
SPAWN_FUNCS = {"Popen", "run", "call", "check_call", "check_output", "getoutput", "getstatusoutput"}
OS_FORKS = {"fork", "forkpty", "posix_spawn", "posix_spawnp", "system", "popen", "spawnl", "spawnle", "spawnlp",
            "spawnlpe", "spawnv", "spawnve", "spawnvp", "spawnvpe"}
BANNED_MODULES = {"multiprocessing", "pty", "_posixsubprocess", "concurrent.futures.process"}
# (file, function, what): the one fork the package may make. The shim forks the CLI once, in a single-threaded
# process, and the child closes every descriptor but its stdio and the handshake before its exec.
ALLOWED_FORKS = {("agent_comms/groupshim.py", "Shim.spawn", "os.fork")}
# (Review round 1, R4: the owner's ruling) The two existing sites that hand a spawn function on, pinned by file and the
# exact statement text (`ast.unparse`; for a parameter default, "<parameter> = <default>"). Removing them (and these
# entries) is a later cleanup; nothing else may join them.
ALLOWED_HANDOFFS = {
    # an injectable default for tests; the call through it passes close_fds=True by keyword (pinned below)
    ("agent_comms/notify.py", "popen: Callable[..., Any] = subprocess.Popen"),
    # an injectable runner for tests; the call through it does not pass close_fds: subprocess's default True (pinned)
    ("agent_comms/waiter.py", "run = run or subprocess.run"),
}
# The one call that spawns through each allow-listed alias, by exact text; `close_fds` checked structurally as well.
PINNED_ALIAS_CALLS = {
    "agent_comms/notify.py": (
        "self.popen",
        "self.popen(osascript_argv(n, self.osascript), shell=False, stdin=subprocess.DEVNULL, "
        "stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True, start_new_session=True, "
        "env={k: os.environ[k] for k in _ENV_KEYS if k in os.environ} | {'PATH': '/usr/bin:/bin'})",
        True),
    "agent_comms/waiter.py": (
        "run",
        "run([path, 'queue', '--thread', thread, '--message', text], stdin=subprocess.DEVNULL, capture_output=True, "
        "text=True, timeout=QUEUE_SECONDS, env=dict(environ))",
        False),
}


def _annotation_nodes(tree) -> set[int]:
    """ids of every node inside a type annotation: a parameter's `annotation`, a function's `returns`, an annotated
    assignment's `annotation`. (A parameter's default is NOT an annotation.)"""
    roots = []
    for n in ast.walk(tree):
        if isinstance(n, ast.arg) and n.annotation is not None:
            roots.append(n.annotation)
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.returns is not None:
            roots.append(n.returns)
        elif isinstance(n, ast.AnnAssign):
            roots.append(n.annotation)
    return {id(x) for r in roots for x in ast.walk(r)}


def _handoff_text(node, parents: dict) -> str:
    """The pinned text of the statement that hands `node` on: for a parameter default, "<parameter> = <default>";
    otherwise the nearest enclosing statement, unparsed."""
    child, up = node, parents.get(id(node))
    while up is not None:
        if isinstance(up, ast.arguments):
            pos = up.posonlyargs + up.args
            for arg, default in zip(pos[len(pos) - len(up.defaults):], up.defaults):
                if default is child:
                    return f"{ast.unparse(arg)} = {ast.unparse(default)}"
            for arg, default in zip(up.kwonlyargs, up.kw_defaults):
                if default is child:
                    return f"{ast.unparse(arg)} = {ast.unparse(default)}"
        if isinstance(up, ast.stmt):
            return ast.unparse(up)
        child, up = up, parents.get(id(up))
    return ast.unparse(node)


def violations(source: str, rel: str, handoffs_seen: list | None = None) -> list[tuple[str, str, str]]:
    """(file, where, what) for every spawn form that is not a reviewed direct one, or a fork outside the allow-list.
    `handoffs_seen` collects the allow-listed hand-offs met (so the caller can require each exactly once)."""
    tree = ast.parse(source)
    parents = {id(c): p for p in ast.walk(tree) for c in ast.iter_child_nodes(p)}
    in_annotation = _annotation_nodes(tree)
    os_names, sub_mods, sub_funcs = {"os"}, set(), set()
    out: list[tuple[str, str, str]] = []

    def where_of(node) -> str:
        names, up = [], parents.get(id(node))
        while up is not None:
            if isinstance(up, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.append(up.name)
            up = parents.get(id(up))
        return ".".join(reversed(names)) or "<module>"

    for n in ast.walk(tree):               # (imports first: the names the rest is judged by)
        if isinstance(n, ast.Import):
            for a in n.names:
                if a.name == "os":
                    os_names.add(a.asname or "os")
                if a.name == "subprocess":
                    sub_mods.add(a.asname or "subprocess")
                    if a.asname and a.asname != "subprocess":
                        out.append((rel, where_of(n), f"import subprocess as {a.asname}"))
                if a.name in BANNED_MODULES or a.name.split(".")[0] in BANNED_MODULES:
                    out.append((rel, where_of(n), f"import {a.name}"))
        elif isinstance(n, ast.ImportFrom):
            mod = n.module or ""
            if mod in BANNED_MODULES or mod.split(".")[0] in BANNED_MODULES:
                out.append((rel, where_of(n), f"from {mod} import"))
            for a in n.names:
                if mod == "os" and a.name in OS_FORKS:
                    out.append((rel, where_of(n), f"from os import {a.name}"))
                if mod == "subprocess" and a.name == "*":
                    out.append((rel, where_of(n), "from subprocess import *"))
                if mod == "subprocess" and a.name in SPAWN_FUNCS:
                    if a.asname and a.asname != a.name:
                        out.append((rel, where_of(n), f"from subprocess import {a.name} as {a.asname}"))
                    else:
                        sub_funcs.add(a.name)
                if mod == "concurrent.futures" and a.name == "ProcessPoolExecutor":
                    out.append((rel, where_of(n), "ProcessPoolExecutor"))

    def is_spawn_ref(n) -> bool:
        return ((isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id in sub_mods
                 and n.attr in SPAWN_FUNCS) or (isinstance(n, ast.Name) and n.id in sub_funcs))

    callees = set()
    for n in ast.walk(tree):
        if not isinstance(n, ast.Call):
            continue
        for kw in n.keywords:
            if kw.arg == "close_fds" and not (isinstance(kw.value, ast.Constant) and kw.value.value is True):
                out.append((rel, where_of(n), f"close_fds={ast.unparse(kw.value)}"))
        f = n.func
        if is_spawn_ref(f):
            callees.add(id(f))
            name = ast.unparse(f)
            if len(n.args) != 1 or any(isinstance(a, ast.Starred) for a in n.args):
                out.append((rel, where_of(n), f"{name}(...) with {len(n.args)} positional or starred arguments"))
            if any(kw.arg is None for kw in n.keywords):
                out.append((rel, where_of(n), f"{name}(**...)"))
        if isinstance(f, ast.Name) and f.id in ("getattr", "vars") and n.args and isinstance(n.args[0], ast.Name) \
                and n.args[0].id in sub_mods:
            out.append((rel, where_of(n), f"{f.id}(subprocess, ...)"))
    for n in ast.walk(tree):
        if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name):
            if n.value.id in os_names and n.attr in OS_FORKS:
                what = f"os.{n.attr}"
                if (rel, where_of(n), what) not in ALLOWED_FORKS:
                    out.append((rel, where_of(n), what))
            if n.value.id in sub_mods and n.attr in ("__dict__", "__getattribute__"):
                out.append((rel, where_of(n), f"subprocess.{n.attr}"))
        if is_spawn_ref(n) and id(n) not in callees and id(n) not in in_annotation:
            text = _handoff_text(n, parents)
            if (rel, text) in ALLOWED_HANDOFFS:
                if handoffs_seen is not None:
                    handoffs_seen.append((rel, text))
            else:
                out.append((rel, where_of(n), f"{ast.unparse(n)} handed on: {text}"))
    if rel in PINNED_ALIAS_CALLS:
        out += alias_call_violations(tree, rel)
    return out


def alias_call_violations(tree, rel: str) -> list[tuple[str, str, str]]:
    """The allow-listed alias's spawning calls in `rel`: exactly one, its text exactly the pinned one, no `**`, and
    `close_fds=True` by keyword (notify) or no `close_fds` at all (waiter)."""
    callee, pinned, needs_close_fds = PINNED_ALIAS_CALLS[rel]
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and ast.unparse(n.func) == callee]
    out = []
    if [ast.unparse(c) for c in calls] != [pinned]:
        out.append((rel, callee, f"the call through the alias is not the pinned one: {[ast.unparse(c) for c in calls]}"))
    for c in calls:
        kws = {kw.arg: kw.value for kw in c.keywords}
        if None in kws:
            out.append((rel, callee, "**kwargs through the alias"))
        has_true = isinstance(kws.get("close_fds"), ast.Constant) and kws["close_fds"].value is True
        if needs_close_fds and not has_true:
            out.append((rel, callee, "the call through the alias does not pass close_fds=True"))
        if not needs_close_fds and "close_fds" in kws:
            out.append((rel, callee, "the call through the alias passes close_fds"))
    return out


def test_no_spawn_in_the_package_keeps_inherited_descriptors():
    found, seen, forks_seen = [], [], set()
    for path in sorted(PACKAGE.rglob("*.py")):
        rel = path.relative_to(PACKAGE.parent).as_posix()
        source = path.read_text()
        found += violations(source, rel, seen)
        for (frel, func, what) in ALLOWED_FORKS:
            if frel == rel and what in source:
                forks_seen.add((frel, func, what))
    assert found == [], found
    assert sorted(seen) == sorted(ALLOWED_HANDOFFS), "each allow-listed hand-off exactly once, with its exact text"
    assert forks_seen == ALLOWED_FORKS, "the allow-list names a fork that still exists"


def test_the_allowed_fork_is_the_shims_single_one():
    """The fork allow-list is exactly one: `Shim.spawn` holds one `os.fork()` call, and nothing else in groupshim.py
    forks."""
    source = (PACKAGE / "groupshim.py").read_text()
    tree = ast.parse(source)
    forks = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and ast.unparse(n.func) == "os.fork"]
    assert len(forks) == 1, [ast.unparse(n) for n in forks]
    spawn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "spawn")
    assert forks[0] in list(ast.walk(spawn))


def whats(source: str, rel: str = "agent_comms/x.py") -> list[str]:
    return [w for _, _, w in violations(source, rel)]


def test_the_scan_refuses_each_kind_of_spawn_that_could_keep_descriptors():
    """The scan is not vacuous: each banned or unreviewed form, in a synthetic module, is refused."""
    cases = {
        # close_fds not the literal True, by keyword (whatever is called)
        "import subprocess\nsubprocess.Popen(['x'], close_fds=False)\n": ["close_fds=False"],
        "import subprocess\nsubprocess.run(['x'], close_fds=flag)\n": ["close_fds=flag"],
        "popen = make()\npopen(['x'], close_fds=0)\n": ["close_fds=0"],
        # close_fds=False given POSITIONALLY (Popen's eighth parameter)
        "import subprocess\nsubprocess.Popen(['x'], -1, None, None, None, None, None, False)\n":
            ["subprocess.Popen(...) with 8 positional or starred arguments"],
        "import subprocess\nsubprocess.run(*a)\n": ["subprocess.run(...) with 1 positional or starred arguments"],
        # **kwargs (could carry close_fds=False)
        "import subprocess\nsubprocess.call(['x'], **kw)\n": ["subprocess.call(**...)"],
        "from subprocess import check_output\ncheck_output(['x'], **kw)\n": ["check_output(**...)"],
        # an alias not on the allow-list, an attribute hand-off, a container, getattr, vars, __dict__, renames
        "import subprocess\np = subprocess.Popen\np(['x'])\n": ["subprocess.Popen handed on: p = subprocess.Popen"],
        "import subprocess\nclass C:\n    runner = subprocess.run\n": ["subprocess.run handed on: runner = subprocess.run"],
        "import subprocess\nfs = [subprocess.call]\n": ["subprocess.call handed on: fs = [subprocess.call]"],
        "import subprocess\ndef f(popen=subprocess.Popen):\n    pass\n":
            ["subprocess.Popen handed on: popen = subprocess.Popen"],
        "from subprocess import run\nf = run\n": ["run handed on: f = run"],
        "import subprocess\ngetattr(subprocess, 'Popen')(['x'])\n": ["getattr(subprocess, ...)"],
        "import subprocess\nvars(subprocess)['Popen'](['x'])\n": ["vars(subprocess, ...)"],
        "import subprocess\nsubprocess.__dict__['run'](['x'])\n": ["subprocess.__dict__"],
        "from subprocess import Popen as P\nP(['x'])\n": ["from subprocess import Popen as P"],
        "from subprocess import *\n": ["from subprocess import *"],
        "import subprocess as sp\nsp.run(['x'])\n": ["import subprocess as sp"],
        # the fork family
        "import os\nos.fork()\n": ["os.fork"],
        "import os as o\no.forkpty()\n": ["os.forkpty"],
        "import os\nos.posix_spawn('x', [], {})\n": ["os.posix_spawn"],
        "import os\nos.spawnv(0, 'x', [])\n": ["os.spawnv"],
        "import os\nos.system('x')\n": ["os.system"],
        "import os\nos.popen('x')\n": ["os.popen"],
        "from os import fork\n": ["from os import fork"],
        "import multiprocessing\n": ["import multiprocessing"],
        "from multiprocessing import Process\n": ["from multiprocessing import"],
        "import pty\n": ["import pty"],
        "import _posixsubprocess\n": ["import _posixsubprocess"],
        "from concurrent.futures import ProcessPoolExecutor\n": ["ProcessPoolExecutor"],
    }
    for source, want in cases.items():
        assert whats(source) == want, (source, violations(source, "agent_comms/x.py"))
    # reviewed direct forms, and annotations, pass
    assert whats("import subprocess\nsubprocess.run(['x'], close_fds=True)\nsubprocess.run(['x'], text=True)\n") == []
    assert whats("from subprocess import Popen\nPopen(['x'], stdin=None)\n") == []
    assert whats("import subprocess\ndef f(p: subprocess.Popen) -> subprocess.Popen:\n    q: subprocess.Popen = p\n"
                 "    return q\n") == [], "annotations name the type; they hand nothing on"
    assert whats("import os\nclass Shim:\n    def spawn(self):\n        os.fork()\n", "agent_comms/groupshim.py") == []
    assert whats("import os\ndef other():\n    os.fork()\n", "agent_comms/groupshim.py") != []


NOTIFY = '''import os, subprocess
from typing import Any, Callable
class MacOSDeliverer:
    def __init__(self, popen: {ann} = {default}, platform: str | None = None):
        self.popen = popen
    def __call__(self, n):
        proc = self.popen(osascript_argv(n, self.osascript), shell=False, stdin=subprocess.DEVNULL,
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, {close_fds}start_new_session=True,
                          env={{k: os.environ[k] for k in _ENV_KEYS if k in os.environ}} | {{"PATH": "/usr/bin:/bin"}})
'''
WAITER = '''def codex_queue(path, thread, text, environ, run=None):
    import subprocess
    {assign}
    r = run([path, "queue", "--thread", thread, "--message", text], stdin=subprocess.DEVNULL,
            capture_output=True, text=True, timeout=QUEUE_SECONDS, env=dict(environ){extra})
'''


def test_the_two_allow_listed_hand_offs_hold_only_with_their_exact_text():
    """The allow-list is by file AND exact statement text, and each alias's one spawning call is pinned too: the
    statements as they stand pass; each differing by one token, or the call losing `close_fds=True` (notify) or gaining
    a `close_fds` (waiter), is refused."""
    n_rel, w_rel = "agent_comms/notify.py", "agent_comms/waiter.py"
    ok = dict(ann="Callable[..., Any]", default="subprocess.Popen", close_fds="close_fds=True, ")
    assert whats(NOTIFY.format(**ok), n_rel) == []
    assert whats(WAITER.format(assign="run = run or subprocess.run", extra=""), w_rel) == []
    assert whats(NOTIFY.format(**ok), "agent_comms/other.py"), "the same text in another file is not allow-listed"
    for bad in (dict(ok, ann="Callable[..., object]"), dict(ok, default="subprocess.call"),
                dict(ok, close_fds="")):
        assert whats(NOTIFY.format(**bad), n_rel), bad
    for assign in ("run = run and subprocess.run", "run = run or subprocess.call", "runner = run or subprocess.run"):
        assert whats(WAITER.format(assign=assign, extra=""), w_rel), assign
    assert whats(WAITER.format(assign="run = run or subprocess.run", extra=", close_fds=True"), w_rel)
    assert whats(WAITER.format(assign="run = run or subprocess.run", extra=", **kw"), w_rel)
