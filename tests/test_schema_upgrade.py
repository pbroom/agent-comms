"""Schema upgrades: a backup first, and never from a development branch that holds the live board's data."""
import sqlite3
import subprocess

import pytest

from agent_comms import db


def _board_at(path, version):
    conn = db.connect(path)
    db.init_schema(conn)
    conn.execute(f"PRAGMA user_version={version}")
    return conn


def test_upgrade_backs_up_the_database_first(tmp_path):
    path = tmp_path / "data" / "board.db"
    conn = _board_at(path, db.SCHEMA_VERSION - 1)
    db.init_schema(conn)
    backup = tmp_path / "data" / f"board-pre-v{db.SCHEMA_VERSION}.db"
    assert backup.exists()
    assert sqlite3.connect(backup).execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION - 1
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION


def test_a_fresh_board_needs_no_backup(tmp_path):
    db.init_schema(db.connect(tmp_path / "board.db"))
    assert [p.name for p in tmp_path.iterdir() if p.suffix == ".db"] == ["board.db"]


def test_newer_schema_says_what_to_do(tmp_path):
    conn = _board_at(tmp_path / "board.db", db.SCHEMA_VERSION + 1)
    with pytest.raises(RuntimeError, match=r"newer than this server.*launchagents\.md"):
        db.init_schema(conn)


@pytest.mark.parametrize("on_main", [False, True])
def test_code_off_main_does_not_migrate_a_board(tmp_path, monkeypatch, on_main):
    monkeypatch.setattr(db, "_on_main", lambda root: on_main)
    monkeypatch.setattr(db, "_under_tmp", lambda path: False)
    monkeypatch.delenv("AGENT_COMMS_ALLOW_MIGRATE", raising=False)
    conn = _board_at(tmp_path / "home" / "data" / "board.db", db.SCHEMA_VERSION - 1)
    if on_main:
        db.init_schema(conn)
        return
    with pytest.raises(RuntimeError, match="refusing to migrate"):
        db.init_schema(conn)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION - 1
    assert not (tmp_path / "home" / "data" / f"board-pre-v{db.SCHEMA_VERSION}.db").exists()
    monkeypatch.setenv("AGENT_COMMS_ALLOW_MIGRATE", "1")
    db.init_schema(conn)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION


def test_boards_under_the_temp_directory_are_exempt(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "_on_main", lambda root: False)
    conn = _board_at(tmp_path / "board.db", db.SCHEMA_VERSION - 1)
    db.init_schema(conn)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION


def _git(root, *args):
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


def test_on_main_follows_the_commit_not_the_branch_name(tmp_path):
    assert db._on_main(tmp_path)                        # not a checkout: an installed runtime
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "one")
    assert db._on_main(tmp_path)
    _git(tmp_path, "checkout", "-q", "-b", "feature")
    assert db._on_main(tmp_path)                        # a branch at a commit already on main
    _git(tmp_path, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "two")
    assert not db._on_main(tmp_path)
    _git(tmp_path, "checkout", "-q", "--detach")
    assert not db._on_main(tmp_path)                    # detached at an unmerged commit


def test_an_existing_backup_is_never_replaced_and_no_temp_file_is_left(tmp_path):
    path = tmp_path / "data" / "board.db"
    conn = _board_at(path, db.SCHEMA_VERSION - 1)
    backup = tmp_path / "data" / f"board-pre-v{db.SCHEMA_VERSION}.db"
    backup.write_bytes(b"earlier")
    db.init_schema(conn)
    assert backup.read_bytes() == b"earlier"
    assert not list((tmp_path / "data").glob(".*.tmp"))
