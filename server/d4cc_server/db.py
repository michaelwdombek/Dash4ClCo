"""SQLite storage: connection handling and schema migrations."""

from __future__ import annotations

import hashlib
import os
import secrets
import sqlite3
import time

MIGRATIONS: list[str] = [
    # 1 - initial schema
    """
    CREATE TABLE hosts (
        id                       INTEGER PRIMARY KEY,
        name                     TEXT NOT NULL UNIQUE,
        token_hash               TEXT NOT NULL UNIQUE,
        created_at               REAL NOT NULL,
        revoked_at               REAL,
        last_seen_at             REAL,
        last_heartbeat_at        REAL,
        hostname_reported        TEXT,
        client_version           TEXT,
        claude_installed_version TEXT,
        os_family                TEXT,
        heartbeat_json           TEXT
    );

    CREATE TABLE sessions (
        id              TEXT PRIMARY KEY,
        host_id         INTEGER NOT NULL REFERENCES hosts(id),
        tmux_pane       TEXT,
        tmux_target     TEXT,
        cwd             TEXT,
        claude_pid      INTEGER,
        claude_version  TEXT,
        model           TEXT,
        permission_mode TEXT,
        source          TEXT,
        started_at      REAL NOT NULL,
        last_event_at   REAL NOT NULL,
        last_client_ts  REAL,
        state           TEXT NOT NULL,
        state_since     REAL NOT NULL,
        activity        TEXT,
        activity_since  REAL,
        bg_tasks        INTEGER,
        ended_at        REAL,
        end_reason      TEXT,
        replaced_by     TEXT,
        archived_at     REAL
    );
    CREATE INDEX sessions_host_pane ON sessions(host_id, tmux_pane);

    CREATE TABLE subagents (
        id          INTEGER PRIMARY KEY,
        session_id  TEXT NOT NULL REFERENCES sessions(id),
        agent_id    TEXT,
        agent_type  TEXT,
        description TEXT,
        tool_use_id TEXT,
        activity    TEXT,
        started_at  REAL NOT NULL,
        ended_at    REAL
    );
    CREATE INDEX subagents_session ON subagents(session_id);
    CREATE INDEX subagents_agent ON subagents(agent_id);

    CREATE TABLE events (
        id          INTEGER PRIMARY KEY,
        session_id  TEXT NOT NULL,
        host_id     INTEGER NOT NULL,
        ts          REAL NOT NULL,
        received_at REAL NOT NULL,
        type        TEXT NOT NULL,
        data        TEXT NOT NULL
    );
    CREATE INDEX events_session_ts ON events(session_id, ts);
    CREATE INDEX events_received ON events(received_at);
    """,
    # 2 - heartbeat liveness + alerts
    """
    ALTER TABLE sessions ADD COLUMN missing_heartbeats INTEGER NOT NULL DEFAULT 0;

    CREATE TABLE alerts (
        id          INTEGER PRIMARY KEY,
        kind        TEXT NOT NULL,          -- stuck | dead | offline
        host_id     INTEGER NOT NULL REFERENCES hosts(id),
        session_id  TEXT REFERENCES sessions(id),
        opened_at   REAL NOT NULL,
        acked_at    REAL,
        note        TEXT,
        resolved_at REAL,
        resolution  TEXT                    -- auto | manual
    );
    CREATE INDEX alerts_open ON alerts(resolved_at);
    CREATE INDEX alerts_session ON alerts(session_id);
    """,
    # 3 - card interactions, runtime settings
    """
    ALTER TABLE sessions ADD COLUMN marker_note TEXT;
    ALTER TABLE sessions ADD COLUMN marker_since REAL;

    CREATE TABLE annotations (
        id          INTEGER PRIMARY KEY,
        session_id  TEXT REFERENCES sessions(id),
        host_id     INTEGER REFERENCES hosts(id),
        alert_id    INTEGER REFERENCES alerts(id),
        kind        TEXT NOT NULL,   -- marker_set | marker_cleared | note | ack | ended | archived | unarchived
        text        TEXT,
        created_at  REAL NOT NULL
    );
    CREATE INDEX annotations_session ON annotations(session_id, created_at);

    CREATE TABLE settings (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );
    """,
]


def connect(path: str) -> sqlite3.Connection:
    if path != ":memory:":
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    migrate(conn)
    return conn


def migrate(conn: sqlite3.Connection) -> None:
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    for version, sql in enumerate(MIGRATIONS[current:], start=current + 1):
        conn.execute("BEGIN")
        for statement in _split(sql):
            conn.execute(statement)
        conn.execute(f"PRAGMA user_version={version}")
        conn.execute("COMMIT")


def _split(sql: str) -> list[str]:
    return [s.strip() for s in sql.split(";") if s.strip()]


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_host(conn: sqlite3.Connection, name: str) -> str:
    """Create a host and return its (plain) token. Only the hash is stored."""
    token = "d4cc_" + secrets.token_urlsafe(32)
    conn.execute(
        "INSERT INTO hosts(name, token_hash, created_at) VALUES (?, ?, ?)",
        (name, hash_token(token), time.time()),
    )
    return token


def host_for_token(conn: sqlite3.Connection, token: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM hosts WHERE token_hash = ? AND revoked_at IS NULL",
        (hash_token(token),),
    ).fetchone()
