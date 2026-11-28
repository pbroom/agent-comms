"""Ownership C3-2b-1: the mutation script's rows for this PR (scripts/steering-mutation-check.py) are well-formed on this
tree: each anchor occurs EXACTLY once in its file (the script reports a row STALE otherwise), each mutant differs from
the original and still parses (else the script reports it BROKEN), and every file key has its tests. The script itself
runs each mutant against its tests (the orchestrator runs it); this only checks the rows can run."""
from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("steering_mutation_check", ROOT / "scripts" / "steering-mutation-check.py")
check = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check)

MINE = [r for r in check.ROWS if "C3-2b-1" in r[1]]
# Rows anchored on text in the files this PR changes: they must not have gone stale with it
TOUCHED = [r for r in check.ROWS if r[0] in ("G", "N", "GS", "SC") and r not in MINE]


def test_this_prs_rows_exist():
    names = {r[1] for r in MINE}
    assert len(MINE) == len(names) == 22, sorted(names)     # (19 new, 3 re-anchored)
    for key in ("SJ", "SL", "GR", "NR", "SH"):
        assert key in check.FILES and key in check.ONLY and all((ROOT / t).exists() for t in check.ONLY[key])


@pytest.mark.parametrize("row", MINE + TOUCHED, ids=[r[1] for r in MINE + TOUCHED])
def test_each_anchor_occurs_once_and_its_mutant_parses(row):
    key, name, old, new = row
    text = (ROOT / check.FILES[key]).read_text()
    assert text.count(old) == 1, (name, text.count(old))
    assert new != old
    ast.parse(text.replace(old, new, 1))
