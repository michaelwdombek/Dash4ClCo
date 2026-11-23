"""Read side: assembles board data (sessions with subagents, hosts) as JSON-able dicts."""

from __future__ import annotations

import sqlite3
import time

SESSION_FIELDS = (
    "id", "host_id", "tmux_pane", "tmux_target", "cwd", "claude_pid", "claude_version",
    "model", "permission_mode", "source", "started_at", "last_event_at", "state",
    "state_since", "activity", "activity_since", "bg_tasks", "ended_at", "end_reason",
    "replaced_by",
)
RECENT_SUBAGENTS = 5


def _subagents(conn: sqlite3.Connection, sid: str) -> list[dict]:
    rows = conn.execute(
        "SELECT agent_id, agent_type, description, activity, started_at, ended_at FROM subagents"
        " WHERE session_id = ? AND (ended_at IS NULL OR id IN ("
        "   SELECT id FROM subagents WHERE session_id = ? AND ended_at IS NOT NULL"
        "   ORDER BY ended_at DESC LIMIT ?))"
        " ORDER BY started_at",
        (sid, sid, RECENT_SUBAGENTS),
    ).fetchall()
    return [dict(r) for r in rows]


def session_dict(conn: sqlite3.Connection, row: sqlite3.Row) -> dict:
    data = {k: row[k] for k in SESSION_FIELDS}
    data["subagents"] = _subagents(conn, row["id"])
    return data


def get_session(conn: sqlite3.Connection, sid: str) -> dict | None:
    row = conn.execute("SELECT * FROM sessions WHERE id = ?", (sid,)).fetchone()
    return session_dict(conn, row) if row else None


def hosts(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT id, name, created_at, revoked_at, last_seen_at, last_heartbeat_at, hostname_reported,"
        " client_version, claude_installed_version, os_family FROM hosts ORDER BY name"
    ).fetchall()
    return [dict(r) for r in rows]


def board(conn: sqlite3.Connection, ended_visible: float) -> dict:
    now = time.time()
    rows = conn.execute(
        "SELECT * FROM sessions WHERE archived_at IS NULL AND (state != 'ended' OR ended_at > ?)"
        " ORDER BY started_at",
        (now - ended_visible,),
    ).fetchall()
    return {
        "now": now,
        "hosts": hosts(conn),
        "sessions": [session_dict(conn, r) for r in rows],
    }
