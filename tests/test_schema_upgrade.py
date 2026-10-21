"""Schema upgrades: a backup first, and never from a development branch that holds the live board's data."""
import sqlite3

import pytest

from agent_comms import config, db


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


@pytest.mark.parametrize("branch, allowed", [("feature/x", False), ("main", True), (None, True)])
def test_a_development_branch_does_not_migrate_the_board_it_holds(tmp_path, monkeypatch, branch, allowed):
    monkeypatch.setattr(config, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(db, "_checkout_branch", lambda root: branch)
    monkeypatch.delenv("AGENT_COMMS_ALLOW_MIGRATE", raising=False)
    conn = _board_at(tmp_path / "data" / "board.db", db.SCHEMA_VERSION - 1)
    if allowed:
        db.init_schema(conn)
        return
    with pytest.raises(RuntimeError, match="refusing to migrate"):
        db.init_schema(conn)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION - 1
    monkeypatch.setenv("AGENT_COMMS_ALLOW_MIGRATE", "1")
    db.init_schema(conn)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION


def test_a_board_outside_the_checkout_migrates_from_any_branch(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "REPO_ROOT", tmp_path / "code")
    monkeypatch.setattr(db, "_checkout_branch", lambda root: "feature/x")
    conn = _board_at(tmp_path / "home" / "data" / "board.db", db.SCHEMA_VERSION - 1)
    db.init_schema(conn)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
