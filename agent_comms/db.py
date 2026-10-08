"""SQLite schema and connections (stdlib sqlite3, WAL mode)."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

SCHEMA_VERSION = 11  # v11: audited backfill of answered pre-tracking requests

SCHEMA = """
CREATE TABLE IF NOT EXISTS managed_write_permit (id INTEGER PRIMARY KEY CHECK(id=1));
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
    continuation_scope TEXT,
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

CREATE TABLE IF NOT EXISTS answer_links (
 source_post_id INTEGER NOT NULL REFERENCES posts(id),
 answer_post_id INTEGER NOT NULL REFERENCES posts(id),
 created_at REAL NOT NULL,
 PRIMARY KEY(source_post_id,answer_post_id)
);
CREATE TABLE IF NOT EXISTS issue_answer_links (
 decision_comment_id INTEGER NOT NULL REFERENCES issue_comments(id),
 issue_link_id INTEGER NOT NULL REFERENCES issue_links(id),
 answer_post_id INTEGER NOT NULL REFERENCES posts(id),
 question_version INTEGER NOT NULL,
 PRIMARY KEY(decision_comment_id,issue_link_id)
);
CREATE TABLE IF NOT EXISTS legacy_attention_answers (
 source_post_id INTEGER PRIMARY KEY REFERENCES posts(id),
 recorded_at REAL NOT NULL,
 reason TEXT NOT NULL DEFAULT 'Pre-v10 attention suppression retained; not completion evidence'
);

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

CREATE TABLE IF NOT EXISTS session_activity (
 session_id INTEGER PRIMARY KEY REFERENCES sessions(id),
 state TEXT NOT NULL CHECK(state IN ('idle','active','unknown')), recorded_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS continuations (
 post_id INTEGER PRIMARY KEY REFERENCES posts(id),
 thread_id INTEGER NOT NULL REFERENCES threads(id),
 task_id INTEGER NOT NULL UNIQUE REFERENCES tasks(id),
 root_task_id INTEGER NOT NULL REFERENCES tasks(id),
 owner_session INTEGER NOT NULL REFERENCES sessions(id),
 fallback_session INTEGER NOT NULL REFERENCES sessions(id),
 recipient TEXT NOT NULL, fix_commit TEXT NOT NULL, descendants TEXT NOT NULL,
 required_checks TEXT NOT NULL, required_capabilities TEXT NOT NULL,
 ack_seconds INTEGER NOT NULL, deadline REAL NOT NULL, epoch INTEGER NOT NULL DEFAULT 0,
 blocker TEXT NOT NULL DEFAULT '', completion TEXT, dispatch_run_id TEXT, created_at REAL NOT NULL,
 UNIQUE(thread_id,fix_commit)
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
    from . import browser_readiness
    if conn.execute("PRAGMA user_version").fetchone()[0] > SCHEMA_VERSION:
        raise RuntimeError("database schema is newer than this server")
    conn.executescript(SCHEMA + browser_readiness.SCHEMA)
    with write_tx(conn):
        if conn.execute("PRAGMA user_version").fetchone()[0] > SCHEMA_VERSION:
            raise RuntimeError("database schema is newer than this server")
        if conn.execute('PRAGMA user_version').fetchone()[0] < 10:
            # Snapshot only sources the old rule had already removed from human attention.
            # This boundary never creates an answer request or marks any work complete.
            conn.execute('''INSERT OR IGNORE INTO legacy_attention_answers(source_post_id,recorded_at)
                SELECT p.id, CAST(strftime('%s','now') AS REAL) FROM posts p
                WHERE ((p.needs_response=1 AND (p.to_agents='[]' OR EXISTS (
                    SELECT 1 FROM json_each(p.to_agents) j JOIN agents a ON a.name=j.value WHERE a.is_human=1)))
                    OR (p.type='decision' AND p.final=0)
                    OR (p.type='proposal' AND p.task_id IS NULL AND EXISTS (
                        SELECT 1 FROM agents a WHERE a.name=p.agent AND a.is_human=0)
                        AND (p.to_agents='[]' OR EXISTS (SELECT 1 FROM json_each(p.to_agents) j
                            JOIN agents a ON a.name=j.value WHERE a.is_human=1))))
                AND EXISTS (SELECT 1 FROM posts h JOIN agents a ON a.name=h.agent
                    WHERE a.is_human=1 AND h.thread_id=p.thread_id AND h.id>p.id)''')
        for table, additions in {
            "issues": {"decision_question": "TEXT", "question_version": "INTEGER NOT NULL DEFAULT 0"},
            "issue_comments": {"decision": "TEXT"},
            # v8: a post that asks the human to choose carries the same structured question as an issue.
            "posts": {"decision_question": "TEXT"},
            "tasks": {"continuation_scope": "TEXT"},
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
        for column in ("client_kind", "client_session_id", "dispatch_run_id"):
            if column not in session_columns:
                conn.execute(f"ALTER TABLE sessions ADD COLUMN {column} TEXT")
        if conn.execute('PRAGMA user_version').fetchone()[0] < 11:
            _backfill_legacy_requests(conn)
        _install_managed_writer_fence(conn)
        conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")


def _backfill_legacy_requests(conn: sqlite3.Connection) -> None:
    """v11: finish pre-tracking requests that the recipient visibly answered, with an audit event.

    Before v7 a reply in the thread was how a request got answered, so those posts have no
    request_progress rows and read as queued forever. Only posts older than tracking are touched:
    all of them when upgrading from before v7, otherwise those created before the first explicit
    request event (a v7+ board with no events is left alone). A recipient is finished only if it
    later posted unsealed in the same thread or the linked task is terminal. Unanswered requests
    keep their virtual queued row, so they stay strict and spend no routing attempt. Post seq is
    unchanged, so nothing is redelivered.
    """
    if conn.execute('PRAGMA user_version').fetchone()[0] < 7:
        cutoff = float('inf')
    else:
        cutoff = conn.execute('SELECT MIN(created_at) FROM request_events').fetchone()[0]
        if cutoff is None:
            return
    now = conn.execute("SELECT CAST(strftime('%s','now') AS REAL)").fetchone()[0]
    humans = {r[0]: r[1] for r in conn.execute('SELECT name,is_human FROM agents')}
    managed = {r[0] for r in conn.execute('SELECT post_id FROM continuations')}
    posts = conn.execute('''SELECT p.id,p.thread_id,p.agent,p.type,p.needs_response,p.to_agents,p.task_id,
            t.status AS task_status FROM posts p LEFT JOIN tasks t ON t.id=p.task_id
            WHERE p.created_at<? ORDER BY p.id''', (cutoff,)).fetchall()
    for post in posts:
        if post['id'] in managed or post['type'] == 'decision':
            continue
        if not (post['needs_response'] or post['type'] in ('request', 'handoff', 'question')
                or humans.get(post['agent'])):
            continue
        for recipient in dict.fromkeys(json.loads(post['to_agents'])):
            if recipient == post['agent'] or humans.get(recipient, 1):
                continue
            if conn.execute('SELECT 1 FROM request_progress WHERE post_id=? AND recipient=?',
                            (post['id'], recipient)).fetchone():
                continue
            reply = conn.execute('SELECT MIN(id) FROM posts WHERE thread_id=? AND agent=? AND id>? AND sealed=0',
                                 (post['thread_id'], recipient, post['id'])).fetchone()[0]
            if reply is not None:
                reason, evidence = f'legacy: answered by #{reply}', [reply]
            elif post['task_status'] in ('done', 'declined'):
                reason, evidence = f"legacy: task {post['task_id']} is {post['task_status']}", []
            else:
                continue
            conn.execute('''INSERT INTO request_progress
                (post_id,recipient,state,assigned_agent,assigned_session,reason,evidence_post_ids,version,updated_at)
                VALUES (?,?,'finished',?,NULL,?,?,1,?)''',
                (post['id'], recipient, recipient, reason, json.dumps(evidence), now))
            conn.execute('''INSERT INTO request_events
                (post_id,recipient,actor,session_id,event_source,state,assigned_agent,assigned_session,
                 reason,evidence_post_ids,version,created_at)
                VALUES (?,?,NULL,NULL,'migration','finished',?,NULL,?,?,1,?)''',
                (post['id'], recipient, recipient, reason, json.dumps(evidence), now))


def _install_managed_writer_fence(conn: sqlite3.Connection) -> None:
    """Old live servers may read the new schema, but cannot mutate managed work.

    A permit exists only inside a new writer's uncommitted transaction. SQLite's
    single-writer lock prevents an old connection from borrowing that permit.
    This is a compatibility fence, not protection from arbitrary SQL access.
    """
    no_permit = "NOT EXISTS (SELECT 1 FROM managed_write_permit WHERE id=1)"
    for operation in ('UPDATE', 'DELETE'):
        conn.execute(f"""CREATE TRIGGER IF NOT EXISTS managed_tasks_{operation.lower()}
            BEFORE {operation} ON tasks
            WHEN {no_permit} AND EXISTS (
                SELECT 1 FROM continuations WHERE task_id=OLD.id OR root_task_id=OLD.id)
            BEGIN SELECT RAISE(ABORT, 'managed work requires an updated server'); END""")
    for operation in ('INSERT', 'UPDATE', 'DELETE'):
        keys = ['NEW.post_id'] if operation == 'INSERT' else ['OLD.post_id']
        if operation == 'UPDATE':
            keys.append('NEW.post_id')
        conn.execute(f"""CREATE TRIGGER IF NOT EXISTS managed_requests_{operation.lower()}
            BEFORE {operation} ON request_progress
            WHEN {no_permit} AND EXISTS (
                SELECT 1 FROM continuations WHERE post_id IN ({','.join(keys)}))
            BEGIN SELECT RAISE(ABORT, 'managed work requires an updated server'); END""")
    for operation in ('INSERT', 'UPDATE', 'DELETE'):
        versions = ['NEW'] if operation == 'INSERT' else ['OLD']
        if operation == 'UPDATE':
            versions.append('NEW')
        matches = ' OR '.join(f'(post_id={v}.post_id AND recipient={v}.recipient)' for v in versions)
        conn.execute(f"""CREATE TRIGGER IF NOT EXISTS managed_browser_requests_{operation.lower()}
            BEFORE {operation} ON request_progress
            WHEN {no_permit} AND EXISTS (
                SELECT 1 FROM browser_requirements WHERE {matches})
            BEGIN SELECT RAISE(ABORT, 'browser work requires an updated server'); END""")
    conn.execute(f"""CREATE TRIGGER IF NOT EXISTS managed_browser_session_identity
        BEFORE UPDATE OF project,worktree,dispatch_run_id,client_session_id ON sessions
        WHEN {no_permit}
          AND (NEW.project IS NOT OLD.project OR NEW.worktree IS NOT OLD.worktree
               OR NEW.dispatch_run_id IS NOT OLD.dispatch_run_id
               OR NEW.client_session_id IS NOT OLD.client_session_id)
          AND (EXISTS (SELECT 1 FROM browser_probes WHERE session_id=OLD.id)
               OR EXISTS (SELECT 1 FROM browser_probe_attempts WHERE session_id=OLD.id)
               OR EXISTS (SELECT 1 FROM browser_requirements b JOIN request_progress r
                   ON r.post_id=b.post_id AND r.recipient=b.recipient
                   WHERE r.assigned_session=OLD.id AND r.state!='finished'))
        BEGIN SELECT RAISE(ABORT, 'browser work requires an updated server'); END""")
    conn.execute(f"""CREATE TRIGGER IF NOT EXISTS managed_session_environment
        BEFORE UPDATE OF project,worktree,dispatch_run_id ON sessions
        WHEN {no_permit}
          AND (NEW.project IS NOT OLD.project OR NEW.worktree IS NOT OLD.worktree
               OR NEW.dispatch_run_id IS NOT OLD.dispatch_run_id)
          AND EXISTS (SELECT 1 FROM continuations w JOIN request_progress r
              ON r.post_id=w.post_id AND r.recipient=w.recipient
              WHERE r.state!='finished' AND
                  (w.owner_session=OLD.id OR w.fallback_session=OLD.id OR r.assigned_session=OLD.id))
        BEGIN SELECT RAISE(ABORT, 'managed work requires an updated server'); END""")


@contextmanager
def write_tx(conn: sqlite3.Connection):
    """Serialize checks and writes; fence managed mutations by obsolete servers."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("INSERT INTO managed_write_permit(id) VALUES (1)")
        yield conn
        conn.execute("DELETE FROM managed_write_permit WHERE id=1")
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
