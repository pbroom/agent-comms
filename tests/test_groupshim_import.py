"""Ownership C3-1: the supervisor's modules import on a platform without kqueue (they are macOS-only to RUN, but the
node-side handle is imported by code that must load anywhere, and a test run off macOS must be able to collect).
Runs on every platform: a child interpreter removes every kqueue name from `select` before importing them."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = str(Path(__file__).resolve().parent.parent)

CHILD = r"""
import select
for name in [n for n in dir(select) if n.startswith("KQ_") or n in ("kqueue", "kevent")]:
    delattr(select, name)
assert not hasattr(select, "KQ_NOTE_EXIT") and not hasattr(select, "kqueue")
from agent_comms import groupshim, shimchild
assert groupshim.PROC_FFLAGS == 0x80000000 | 0x04000000 | 0x20000000, hex(groupshim.PROC_FFLAGS)
assert shimchild.ShimChild is not None
print("imported")
"""


def test_the_supervisor_modules_import_without_kqueue():
    r = subprocess.run([sys.executable, "-c", CHILD], capture_output=True, text=True, timeout=60, cwd=ROOT,
                       env={**os.environ, "PYTHONPATH": ROOT})
    assert r.returncode == 0 and r.stdout.strip() == "imported", (r.returncode, r.stdout, r.stderr[-800:])


def test_the_kqueue_flags_read_at_import_are_this_platforms_own_where_it_has_them():
    import select
    from agent_comms import groupshim
    if hasattr(select, "KQ_NOTE_EXIT"):
        assert groupshim._KQ_NOTE_EXIT == select.KQ_NOTE_EXIT and groupshim._KQ_NOTE_EXEC == select.KQ_NOTE_EXEC
    assert groupshim.PROC_FFLAGS & groupshim.NOTE_EXITSTATUS
