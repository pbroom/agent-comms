"""SQLite schema and connections (stdlib sqlite3, WAL mode)."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path

SCHEMA_VERSION = 6   # v6: auditable per-source attention closeout

SCHEMA = """
CREATE TABLE IF NOT EXISTS agents (
    name        TEXT PRIMARY KEY,
    runtime     TEXT NOT NULL,
    token_hash  TEXT NOT NULL UNIQUE,
    is_human    INTEGER NOT NULL DEFAULT 0,
    active      INTEGER NOT NULL DEFAULT 1,   -- 0 once removed from agents.toml
    created_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    agent       TEXT NOT NULL REFERENCES agents(name),
    runtime     TEXT NOT NULL,
    project     TEXT NOT NULL,
    worktree    TEXT,
    started_at  REAL NOT NULL,
    last_seen   REAL NOT NULL,
    client_kind        TEXT,   -- 'claude-code' | 'claude-code-subagent' | 'codex': whose conversation this session is (conversations.py)
    client_session_id  TEXT    -- that conversation's UUID, validated; human-only in /api/state
);

CREATE TABLE IF NOT EXISTS threads (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    project         TEXT NOT NULL,
    title           TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','closed')),
    pinned_summary  TEXT,
    summary_by      TEXT,
    summary_at      REAL,
    created_by      TEXT NOT NULL REFERENCES agents(name),
    created_at      REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS threads_project ON threads(project, status);

CREATE TABLE IF NOT EXISTS posts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    seq             INTEGER NOT NULL UNIQUE,     -- cursor position; re-assigned when the post is unsealed/finalized
    thread_id       INTEGER NOT NULL REFERENCES threads(id),
    session_id      INTEGER NOT NULL REFERENCES sessions(id),
    agent           TEXT NOT NULL REFERENCES agents(name),
    type            TEXT NOT NULL CHECK (type IN ('question','proposal','status','finding','handoff','request','decision')),
    body            TEXT NOT NULL,
    to_agents       TEXT NOT NULL DEFAULT '[]',  -- json array of agent names ("to")
    needs_response  INTEGER NOT NULL DEFAULT 0,
    task_id         INTEGER REFERENCES tasks(id),
    refs            TEXT NOT NULL DEFAULT '[]',  -- json array of {kind, path, rev}
    sealed          INTEGER NOT NULL DEFAULT 0,  -- 1 = currently sealed (author + human only)
    was_sealed      INTEGER NOT NULL DEFAULT 0,
    unsealed_at     REAL,
    unsealed_by     TEXT,                        -- human agent name, or 'auto:reviewers'
    final           INTEGER NOT NULL DEFAULT 0,  -- decisions only; human-set
    finalized_at    REAL,
    revised_at      REAL,
    created_at      REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS posts_thread_seq ON posts(thread_id, seq);
CREATE INDEX IF NOT EXISTS posts_agent_time ON posts(agent, created_at);
CREATE INDEX IF NOT EXISTS posts_task ON posts(task_id);

CREATE TABLE IF NOT EXISTS attention_resolutions (
    post_id INTEGER PRIMARY KEY REFERENCES posts(id),
    resolved_by TEXT NOT NULL REFERENCES agents(name),
    session_id INTEGER NOT NULL REFERENCES sessions(id),
    reason TEXT NOT NULL,
    evidence_post_ids TEXT NOT NULL,
    resolved_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS authorization_grants (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project TEXT NOT NULL,
    category TEXT NOT NULL,
    agents TEXT NOT NULL,
    purpose TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES agents(name),
    created_at REAL NOT NULL,
    expires_at REAL,
    revoked_by TEXT REFERENCES agents(name),
    revoked_at REAL
);

CREATE TABLE IF NOT EXISTS tasks (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    thread_id         INTEGER NOT NULL REFERENCES threads(id),
    title             TEXT NOT NULL,
    acceptance        TEXT NOT NULL DEFAULT '',
    category          TEXT,
    authorization_source TEXT NOT NULL DEFAULT 'none',
    authorization_grant_id INTEGER REFERENCES authorization_grants(id),
    status            TEXT NOT NULL DEFAULT 'proposed'
                      CHECK (status IN ('proposed','accepted','working','blocked','done','declined')),
    owner_agent       TEXT REFERENCES agents(name),
    owner_session     INTEGER REFERENCES sessions(id),
    lease_expires_at  REAL,
    intends_files     TEXT NOT NULL DEFAULT '[]',
    depends_on        TEXT NOT NULL DEFAULT '[]',
    created_by        TEXT NOT NULL REFERENCES agents(name),
    created_at        REAL NOT NULL,
    updated_at        REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS tasks_thread ON tasks(thread_id);

CREATE TABLE IF NOT EXISTS task_events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id      INTEGER NOT NULL REFERENCES tasks(id),
    event        TEXT NOT NULL,        -- create | claim | reclaim | renew | release | transition
    from_status  TEXT,
    to_status    TEXT,
    agent        TEXT NOT NULL,
    session_id   INTEGER,
    note         TEXT,
    at           REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS task_events_task ON task_events(task_id);

CREATE TABLE IF NOT EXISTS cursors (
    session_id INTEGER NOT NULL REFERENCES sessions(id),  -- per session, so parallel sessions of one agent don't steal reads
    thread_id  INTEGER NOT NULL REFERENCES threads(id),
    agent      TEXT NOT NULL REFERENCES agents(name),
    last_seq   INTEGER NOT NULL DEFAULT 0,   -- "last acked post" expressed as a post seq (see DESIGN_NOTES)
    updated_at REAL NOT NULL,
    PRIMARY KEY (session_id, thread_id)
);

-- v1 is pull-only. Reserved so a dispatcher can be added without a schema change.
CREATE TABLE IF NOT EXISTS subscriptions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    agent       TEXT NOT NULL REFERENCES agents(name),
    session_id  INTEGER REFERENCES sessions(id),
    project     TEXT,
    thread_id   INTEGER REFERENCES threads(id),
    events      TEXT NOT NULL DEFAULT '["post.created"]',   -- json array of event names
    channel     TEXT NOT NULL DEFAULT 'none',              -- e.g. 'command', 'webhook' (future)
    target      TEXT,                                      -- channel-specific (future)
    active      INTEGER NOT NULL DEFAULT 1,
    created_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS issues (
 id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, body TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','resolved')),
 needs_human INTEGER NOT NULL DEFAULT 1, created_by TEXT NOT NULL REFERENCES agents(name),
 created_at REAL NOT NULL, updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS issue_links (
 id INTEGER PRIMARY KEY AUTOINCREMENT, issue_id INTEGER NOT NULL REFERENCES issues(id),
 thread_id INTEGER NOT NULL REFERENCES threads(id), post_id INTEGER REFERENCES posts(id),
 needs_human INTEGER NOT NULL DEFAULT 0,
 agent TEXT NOT NULL REFERENCES agents(name), created_at REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS issue_link_unique ON issue_links(issue_id,thread_id,COALESCE(post_id,0));
CREATE INDEX IF NOT EXISTS issue_links_post ON issue_links(post_id);
CREATE TABLE IF NOT EXISTS issue_comments (
 id INTEGER PRIMARY KEY AUTOINCREMENT, issue_id INTEGER NOT NULL REFERENCES issues(id),
 session_id INTEGER NOT NULL REFERENCES sessions(id), agent TEXT NOT NULL REFERENCES agents(name),
 kind TEXT NOT NULL CHECK(kind IN ('comment','evidence','proposal','request','decision','resolution','created','linked')),
 body TEXT NOT NULL, outcome TEXT, scope TEXT, created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS issue_comments_issue ON issue_comments(issue_id,id);
CREATE INDEX IF NOT EXISTS issue_comments_agent ON issue_comments(agent,created_at);

CREATE TABLE IF NOT EXISTS session_capabilities (
 session_id INTEGER PRIMARY KEY REFERENCES sessions(id), capabilities TEXT NOT NULL,
 evidence TEXT NOT NULL, verified_at REAL NOT NULL, expires_at REAL NOT NULL,
 attested_agent TEXT NOT NULL, project TEXT NOT NULL, worktree TEXT
);
CREATE TABLE IF NOT EXISTS request_progress (
 post_id INTEGER NOT NULL REFERENCES posts(id), recipient TEXT NOT NULL REFERENCES agents(name),
 state TEXT NOT NULL CHECK(state IN ('queued','started','blocked','finished')),
 assigned_agent TEXT NOT NULL REFERENCES agents(name), assigned_session INTEGER REFERENCES sessions(id),
 reason TEXT NOT NULL DEFAULT '', evidence_post_ids TEXT NOT NULL DEFAULT '[]',
 version INTEGER NOT NULL DEFAULT 0, updated_at REAL NOT NULL, PRIMARY KEY(post_id,recipient)
);
CREATE TABLE IF NOT EXISTS request_events (
 id INTEGER PRIMARY KEY AUTOINCREMENT, post_id INTEGER NOT NULL REFERENCES posts(id),
 recipient TEXT NOT NULL, actor TEXT REFERENCES agents(name), session_id INTEGER REFERENCES sessions(id),
 event_source TEXT NOT NULL DEFAULT 'agent',
 state TEXT NOT NULL, assigned_agent TEXT NOT NULL, assigned_session INTEGER REFERENCES sessions(id),
 reason TEXT NOT NULL, evidence_post_ids TEXT NOT NULL, version INTEGER NOT NULL, created_at REAL NOT NULL,
 UNIQUE(post_id,recipient,version)
);

CREATE TABLE IF NOT EXISTS board_state (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    updated_by  TEXT,
    updated_at  REAL NOT NULL
);
"""


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    if conn.execute("PRAGMA user_version").fetchone()[0] > SCHEMA_VERSION:
        raise RuntimeError("database schema is newer than this server")
    conn.executescript(SCHEMA)
    with write_tx(conn):
        if conn.execute("PRAGMA user_version").fetchone()[0] > SCHEMA_VERSION:
            raise RuntimeError("database schema is newer than this server")
        for table, additions in {
            "issues": {"decision_question": "TEXT", "question_version": "INTEGER NOT NULL DEFAULT 0"},
            "issue_comments": {"decision": "TEXT"},
        }.items():
            existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
            for name, definition in additions.items():
                if name not in existing:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
        link_columns = {row[1] for row in conn.execute("PRAGMA table_info(issue_links)")}
        if "needs_human" not in link_columns:
            conn.execute("ALTER TABLE issue_links ADD COLUMN needs_human INTEGER NOT NULL DEFAULT 0")
            conn.execute("UPDATE issue_links SET needs_human=(SELECT needs_human FROM issues WHERE id=issue_id)")
        columns = {row[1] for row in conn.execute("PRAGMA table_info(tasks)")}
        if "category" not in columns:
            conn.execute("ALTER TABLE tasks ADD COLUMN category TEXT")
        if "authorization_source" not in columns:
            conn.execute("ALTER TABLE tasks ADD COLUMN authorization_source TEXT NOT NULL DEFAULT 'none'")
            conn.execute("""UPDATE tasks SET authorization_source = CASE
                WHEN created_by IN (SELECT name FROM agents WHERE is_human=1)
                  OR id IN (SELECT task_id FROM task_events WHERE to_status='accepted'
                            AND agent IN (SELECT name FROM agents WHERE is_human=1)) THEN 'human'
                WHEN status != 'proposed' THEN 'legacy' ELSE 'none' END""")
        if "authorization_grant_id" not in columns:
            conn.execute("ALTER TABLE tasks ADD COLUMN authorization_grant_id INTEGER REFERENCES authorization_grants(id)")
        # v3: additive; existing sessions simply have no conversation link.
        session_columns = {row[1] for row in conn.execute("PRAGMA table_info(sessions)")}
        for column in ("client_kind", "client_session_id"):
            if column not in session_columns:
                conn.execute(f"ALTER TABLE sessions ADD COLUMN {column} TEXT")
        conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")


@contextmanager
def write_tx(conn: sqlite3.Connection):
    """BEGIN IMMEDIATE: take the write lock up front so check-then-write is serialized across processes."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")
