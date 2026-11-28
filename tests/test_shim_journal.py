"""Ownership C3-2b-1, A: the supervised-launch journal (agent_comms/shimjournal.py). DESIGN_NOTES "Ownership C3-2b-1".

One durable, versioned file per supervised launch, in a private directory under the node's data directory, beside the
session state (never inside it). `commit` creates exclusively and syncs the file and the directory; `remove` unlinks and
syncs the directory; `load` returns every record or raises `JournalUnreadable` (one bad file is never skipped). No
logging. Runs on every platform (no process is started here)."""
from __future__ import annotations

import ast
import json
import os
import stat
from pathlib import Path

import pytest

from agent_comms import shimjournal
from agent_comms.shimjournal import Journal, JournalUnreadable, Record

BOOT = "154793AE-2D88-4E53-AB60-D82BAD21E683"


def a_record(tmp_path, launch_id=None, **over) -> Record:
    cwd = tmp_path / "checkout"
    cwd.mkdir(exist_ok=True)
    st = os.stat(cwd)
    fields = dict(launch_id=launch_id or shimjournal.new_launch_id(), checkout_key=(st.st_dev, st.st_ino),
                  cwd=str(cwd), boot=BOOT, marker=str(tmp_path / "markers" / "ab.marker"), shim_pid=4242,
                  shim_start="Mon Nov 28 10:00:00 2026")
    fields.update(over)
    return Record(**fields)


@pytest.fixture
def journal(tmp_path):
    return Journal(tmp_path / "node-logs" / shimjournal.JOURNAL_DIR)


def test_a_commit_is_one_private_durable_file_per_launch_and_load_returns_it(journal, tmp_path, monkeypatch):
    """Commit creates `<launch id>.json` exclusively (0600) in a 0700 directory it makes (syncing its parent once it
    made it), writes the whole record and syncs the file, then the directory; load gives the record back exactly,
    token and checkout key included."""
    (tmp_path / "node-logs").mkdir()
    synced = []
    real = shimjournal._fullsync
    monkeypatch.setattr(shimjournal, "_fullsync", lambda fd: (synced.append(stat.S_IFMT(os.fstat(fd).st_mode)),
                                                               real(fd))[1])
    r1, r2 = a_record(tmp_path), a_record(tmp_path)
    journal.commit(r1)
    assert synced == [stat.S_IFDIR, stat.S_IFREG, stat.S_IFDIR], "the new directory's parent, the file, its directory"
    synced.clear()
    journal.commit(r2)
    assert synced == [stat.S_IFREG, stat.S_IFDIR], "the file, then its directory"
    d = Path(journal.directory)
    assert stat.S_IMODE(d.stat().st_mode) == 0o700
    assert sorted(os.listdir(d)) == sorted(f"{r.launch_id}.json" for r in (r1, r2))
    for r in (r1, r2):
        assert stat.S_IMODE((d / f"{r.launch_id}.json").stat().st_mode) == 0o600
    assert sorted(journal.load(), key=lambda r: r.launch_id) == sorted([r1, r2], key=lambda r: r.launch_id)
    assert r1.token == (shimjournal.SUPERVISED, r1.launch_id) and r1.checkout_key == (os.stat(r1.cwd).st_dev,
                                                                                         os.stat(r1.cwd).st_ino)


def test_the_record_format(journal, tmp_path):
    """The format, pinned: one JSON line, exactly these fields. NOT the launch secret (it stays in the node's memory)."""
    r = a_record(tmp_path)
    journal.commit(r)
    data = (Path(journal.directory) / f"{r.launch_id}.json").read_bytes()
    assert data.endswith(b"\n") and data.count(b"\n") == 1
    assert json.loads(data) == {"v": 1, "launch_id": r.launch_id, "token": ["supervised", r.launch_id],
                                "checkout_key": list(r.checkout_key), "cwd": r.cwd, "boot": BOOT,
                                "marker": r.marker, "shim_pid": 4242, "shim_start": "Mon Nov 28 10:00:00 2026"}
    assert "secret" not in data.decode() and "key\"" not in data.decode().replace("checkout_key", "")


def test_a_commit_never_replaces_a_record(journal, tmp_path):
    r = a_record(tmp_path)
    journal.commit(r)
    before = (Path(journal.directory) / f"{r.launch_id}.json").read_bytes()
    with pytest.raises(FileExistsError):
        journal.commit(a_record(tmp_path, launch_id=r.launch_id, shim_pid=99))
    assert (Path(journal.directory) / f"{r.launch_id}.json").read_bytes() == before


@pytest.mark.parametrize("where", ["write", "file-sync", "dir-sync"])
def test_a_commit_that_fails_removes_nothing_and_raises(journal, tmp_path, monkeypatch, where):
    """(Review round 1, R3) A commit that fails at any step after its create raises and removes NOTHING: exclusive
    creation established ownership of the open inode only, and a same-user process could have substituted the
    pathname before an unlink. The failed record is left in place (the launch then never writes GO, and fences it:
    shimlaunch). A partial one makes the journal unreadable, naming the file's path (a human removes it); a whole one
    loads (the next start restores it HELD)."""
    journal.commit(a_record(tmp_path))         # (makes the directory; this record is not the one that fails)
    kept = set(os.listdir(journal.directory))
    real_sync, real_write = shimjournal._fullsync, shimjournal._write_all
    calls, unlinked = [], []

    def sync(fd):
        calls.append(stat.S_IFMT(os.fstat(fd).st_mode))
        if (where == "file-sync" and calls[-1] == stat.S_IFREG) or (where == "dir-sync" and len(calls) == 2):
            raise OSError(5, "injected EIO")
        real_sync(fd)

    def write(fd, data):
        real_write(fd, data[:7])                # (a part of the record is on disk, then the write fails)
        raise OSError(28, "injected ENOSPC")
    monkeypatch.setattr(shimjournal, "_fullsync", sync)
    monkeypatch.setattr(shimjournal.os, "unlink", lambda *a, **k: unlinked.append(a))
    monkeypatch.setattr(shimjournal.os, "remove", lambda *a, **k: unlinked.append(a))
    if where == "write":
        monkeypatch.setattr(shimjournal, "_write_all", write)
    r = a_record(tmp_path)
    with pytest.raises(OSError):
        journal.commit(r)
    monkeypatch.undo()
    assert unlinked == [], "a failed commit removes nothing"
    path = os.path.join(journal.directory, f"{r.launch_id}.json")
    assert set(os.listdir(journal.directory)) == kept | {f"{r.launch_id}.json"}, "the failed record is left in place"
    if where == "write":
        with pytest.raises(JournalUnreadable) as e:
            journal.load()
        assert path in e.value.reason, "the reason names the offending file's path"
    else:
        assert r in journal.load()


def test_the_parent_is_synced_on_every_commit_until_this_process_has_synced_it(journal, tmp_path, monkeypatch):
    """(Review round 1, R2) The first `mkdir` succeeds and the parent's sync fails: that commit fails (nothing is
    created). The NEXT commit finds the directory already there and must still sync the parent (before: it skipped
    it, so a record could be trusted whose directory entry would not survive a crash). Once a parent sync has
    returned in this process (the journal's flag, set only after it), later commits sync the file and the directory
    only; a new process (a new Journal) syncs the parent again on its first commit."""
    (tmp_path / "node-logs").mkdir()
    parent = os.path.dirname(journal.directory)
    real = shimjournal._sync_dir
    synced, fail = [], [True]

    def sync_dir(path):
        if path == parent and fail:
            fail.clear()
            raise OSError(5, "injected EIO")
        synced.append(path)
        real(path)
    monkeypatch.setattr(shimjournal, "_sync_dir", sync_dir)
    with pytest.raises(OSError):
        journal.commit(a_record(tmp_path))
    assert os.path.isdir(journal.directory) and journal.load() == [], "made, but nothing committed"
    journal.commit(a_record(tmp_path))
    assert synced == [parent, journal.directory], "the parent is synced by the next commit"
    synced.clear()
    journal.commit(a_record(tmp_path))
    assert synced == [journal.directory], "once synced in this process, not again"
    synced.clear()
    Journal(journal.directory).commit(a_record(tmp_path))
    assert synced == [parent, journal.directory], "a new process syncs it once"
    assert len(journal.load()) == 3


FORBIDDEN_IN_COMMIT = {"unlink", "remove", "rename", "renames", "replace", "rmdir", "removedirs", "rmtree", "truncate",
                       "ftruncate"}


def test_commit_and_all_it_calls_never_unlink_remove_rename_or_replace():
    """(Review round 1, R3), static: `Journal.commit`, and every function or method of the module it calls
    (transitively, by name), names none of unlink, remove, rename, replace, rmdir (nor truncate, shutil); and
    module-wide `os.unlink` is called only in `Journal.remove` (after proof), with no `os.remove`, `os.rename`,
    `os.replace` or `os.rmdir` anywhere."""
    tree = ast.parse(Path(shimjournal.__file__).read_text())
    defs: dict[str, list] = {}
    for top in tree.body:
        if isinstance(top, ast.FunctionDef):
            defs.setdefault(top.name, []).append((top.name, top))
        elif isinstance(top, ast.ClassDef):
            for m in top.body:
                if isinstance(m, ast.FunctionDef):
                    defs.setdefault(m.name, []).append((f"{top.name}.{m.name}", m))
    todo, seen = [("Journal.commit", n) for q, n in defs["commit"] if q == "Journal.commit"], set()
    assert todo
    while todo:
        qual, fn = todo.pop()
        if qual in seen:
            continue
        seen.add(qual)
        for n in ast.walk(fn):
            name = n.id if isinstance(n, ast.Name) else n.attr if isinstance(n, ast.Attribute) else None
            assert name not in FORBIDDEN_IN_COMMIT and name != "shutil", (qual, ast.unparse(n))
            if isinstance(n, ast.Call):
                called = n.func.id if isinstance(n.func, ast.Name) else n.func.attr \
                    if isinstance(n.func, ast.Attribute) else None
                todo += defs.get(called, [])
    assert {"Journal.commit", "Journal.make", "Journal._path", "Record.check", "Record.to_json", "_from_json",
            "_sync_dir", "_fullsync", "_write_all", "_check_private"} <= seen, seen
    owners: dict[str, set] = {}
    for qual, fn in [x for v in defs.values() for x in v]:
        for n in ast.walk(fn):
            if isinstance(n, ast.Attribute) and ast.unparse(n) in ("os.unlink", "os.remove", "os.rename",
                                                                    "os.replace", "os.rmdir"):
                owners.setdefault(ast.unparse(n), set()).add(qual)
    assert owners == {"os.unlink": {"Journal.remove"}}, owners


def test_remove_unlinks_and_syncs_the_directory_and_is_idempotent(journal, tmp_path, monkeypatch):
    r = a_record(tmp_path)
    journal.commit(r)
    synced = []
    real = shimjournal._fullsync
    monkeypatch.setattr(shimjournal, "_fullsync", lambda fd: (synced.append(stat.S_IFMT(os.fstat(fd).st_mode)),
                                                               real(fd))[1])
    journal.remove(r.launch_id)
    assert journal.load() == [] and synced == [stat.S_IFDIR]
    journal.remove(r.launch_id)                 # (already gone: the directory is synced again, no error)
    with pytest.raises(ValueError):
        journal.remove("../escape")


def test_an_absent_journal_is_empty_and_nothing_is_created(tmp_path):
    j = Journal(tmp_path / "node-logs" / shimjournal.JOURNAL_DIR)
    assert j.load() == []
    assert not (tmp_path / "node-logs").exists()


def _bad(journal, tmp_path, case):
    d = Path(journal.directory)
    r = a_record(tmp_path)
    good = json.dumps(r.to_json(), separators=(",", ":")) + "\n"
    name = d / f"{r.launch_id}.json"

    def edit(**kw):
        rec = r.to_json()
        rec.update(kw)
        name.write_text(json.dumps(rec) + "\n")
    if case == "garbage":
        name.write_text("{not json\n")
    elif case == "empty-file":                  # (a crash between the create and the write)
        name.write_text("")
    elif case == "truncated":
        name.write_text(good[: len(good) // 2])
    elif case == "two-lines":
        name.write_text(good + good)
    elif case == "extra-field":
        edit(secret="00" * 32)
    elif case == "missing-field":
        rec = r.to_json()
        del rec["marker"]
        name.write_text(json.dumps(rec) + "\n")
    elif case == "version-2":
        edit(v=2)
    elif case == "version-bool":
        edit(v=True)
    elif case == "pid-bool":
        edit(shim_pid=True)
    elif case == "token-mismatch":
        edit(token=["session", r.launch_id])
    elif case == "name-mismatch":
        edit(launch_id=shimjournal.new_launch_id())
    elif case == "relative-cwd":
        edit(cwd="checkout")
    elif case == "bad-key":
        edit(checkout_key=[1])
    elif case == "bad-boot":
        edit(boot="1700000000.123456")
    elif case == "stray-name":
        (d / "notes.txt").write_text("hello\n")
    elif case == "temp-name":
        (d / f"{r.launch_id}.json.tmp").write_text(good)
    elif case == "symlink":
        other = tmp_path / "elsewhere.json"
        other.write_text(good)
        name.symlink_to(other)
    elif case == "subdirectory":
        name.mkdir()
    elif case == "fifo":
        os.mkfifo(name)
    elif case == "unreadable-file":
        name.write_text(good)
        name.chmod(0)
    elif case == "dir-open-to-group":
        d.chmod(0o750)
    elif case == "dir-symlink":
        real = tmp_path / "real-journal"
        d.rename(real)
        d.symlink_to(real)
    elif case == "dir-not-a-directory":
        for p in d.iterdir():
            p.unlink()
        d.rmdir()
        d.write_text("")
    elif case == "dir-unlistable":
        d.chmod(0o300)


BAD = ["garbage", "empty-file", "truncated", "two-lines", "extra-field", "missing-field", "version-2", "version-bool",
       "pid-bool", "token-mismatch", "name-mismatch", "relative-cwd", "bad-key", "bad-boot", "stray-name", "temp-name",
       "symlink", "subdirectory", "fifo", "unreadable-file", "dir-open-to-group", "dir-symlink",
       "dir-not-a-directory", "dir-unlistable"]


@pytest.mark.parametrize("case", BAD)
def test_any_file_or_directory_that_cannot_be_read_parsed_or_validated_makes_the_journal_unreadable(journal, tmp_path,
                                                                                                   case):
    """`load` never skips a bad file: one record that cannot be read, parsed or validated (or a directory that is not
    the node's private one) and the WHOLE journal is unreadable, with the good records beside it."""
    journal.commit(a_record(tmp_path))          # (a good record beside the bad one)
    try:
        _bad(journal, tmp_path, case)
        with pytest.raises(JournalUnreadable) as e:
            journal.load()
        assert e.value.reason
    finally:
        d = Path(journal.directory)
        for p in (d, *(d.iterdir() if d.is_dir() and os.access(d, os.R_OK) else ())):
            try:
                os.chmod(p, 0o700, follow_symlinks=False)
            except (OSError, NotImplementedError):
                pass


def test_the_journal_module_logs_nothing_and_imports_no_process_code():
    """No logging in the journal (errors are returned or raised), and it imports neither the supervisor nor its
    handle: the node imports it at start."""
    tree = ast.parse(Path(shimjournal.__file__).read_text())
    names = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            names |= {a.name for a in n.names}
        elif isinstance(n, ast.ImportFrom):
            names |= {f"{n.module or ''}:{a.name}" for a in n.names} | {n.module or ""}
    assert not {x for x in names if "logging" in x or "hostlog" in x or "shimchild" in x or "groupshim" in x
                or "shimlaunch" in x}, names
