"""Read side: assembles board data (hosts, sessions, subagents, alerts).

Version flags (outdated / restart pending) are derived in the browser, because
live updates deliver single sessions and hosts.
"""

from __future__ import annotations

import json
import sqlite3
import time

from .config import Config

SESSION_FIELDS = (
    "id", "host_id", "tmux_pane", "tmux_target", "cwd", "claude_pid", "claude_version",
    "model", "permission_mode", "source", "started_at", "last_event_at", "state",
    "state_since", "activity", "activity_since", "bg_tasks", "ended_at", "end_reason",
    "replaced_by", "marker_note", "marker_since", "archived_at",
)
HOST_FIELDS = (
    "id", "name", "created_at", "revoked_at", "last_seen_at", "last_heartbeat_at",
    "hostname_reported", "client_version", "claude_installed_version", "os_family",
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


def _alerts(conn: sqlite3.Connection, where: str, args: tuple) -> list[dict]:
    rows = conn.execute(
        f"SELECT id, kind, host_id, session_id, opened_at, acked_at, note FROM alerts"
        f" WHERE resolved_at IS NULL AND {where} ORDER BY opened_at",
        args,
    ).fetchall()
    return [dict(r) for r in rows]


def session_dict(conn: sqlite3.Connection, row: sqlite3.Row) -> dict:
    data = {k: row[k] for k in SESSION_FIELDS}
    data["subagents"] = _subagents(conn, row["id"])
    data["alerts"] = _alerts(conn, "session_id = ?", (row["id"],))
    return data


def get_session(conn: sqlite3.Connection, sid: str) -> dict | None:
    row = conn.execute("SELECT * FROM sessions WHERE id = ?", (sid,)).fetchone()
    return session_dict(conn, row) if row else None


def host_dict(conn: sqlite3.Connection, row: sqlite3.Row) -> dict:
    data = {k: row[k] for k in HOST_FIELDS}
    hb = json.loads(row["heartbeat_json"]) if row["heartbeat_json"] else {}
    data["processes"] = len(hb.get("processes", []))
    data["uptime_s"] = hb.get("uptime_s")
    data["alerts"] = _alerts(conn, "host_id = ? AND session_id IS NULL", (row["id"],))
    return data


def get_host(conn: sqlite3.Connection, host_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM hosts WHERE id = ?", (host_id,)).fetchone()
    return host_dict(conn, row) if row else None


def board(conn: sqlite3.Connection, cfg: Config) -> dict:
    now = time.time()
    host_rows = conn.execute("SELECT * FROM hosts WHERE revoked_at IS NULL ORDER BY name").fetchall()
    hosts = [host_dict(conn, r) for r in host_rows]
    rows = conn.execute(
        "SELECT * FROM sessions WHERE archived_at IS NULL AND (state != 'ended' OR ended_at > ?)"
        " ORDER BY started_at",
        (now - cfg.ended_visible,),
    ).fetchall()
    sessions = [session_dict(conn, r) for r in rows]

    return {
        "now": now,
        "min_claude_version": cfg.min_claude_version,
        "thresholds": {"stuck_after": cfg.stuck_after, "offline_after": cfg.offline_after},
        "hosts": hosts,
        "sessions": sessions,
    }
