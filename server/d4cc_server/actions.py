"""User actions on cards (markers, notes, acks, archive, end) plus history and retention."""

from __future__ import annotations

import json
import sqlite3

MAX_TEXT = 1000
HISTORY_LIMIT = 500


class NotFound(LookupError):
    pass


class Conflict(ValueError):
    pass


def _text(value) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise Conflict("text must be a string")
    value = value.strip()
    return value[:MAX_TEXT] or None


def _session(conn: sqlite3.Connection, sid: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM sessions WHERE id = ?", (sid,)).fetchone()
    if row is None:
        raise NotFound("session not found")
    return row


def _annotate(conn, now, kind, session_id=None, host_id=None, alert_id=None, text=None) -> None:
    conn.execute(
        "INSERT INTO annotations(session_id, host_id, alert_id, kind, text, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (session_id, host_id, alert_id, kind, text, now),
    )


# -- session actions --------------------------------------------------------

def set_marker(conn, sid: str, note, now: float) -> None:
    s = _session(conn, sid)
    note = _text(note)
    conn.execute("UPDATE sessions SET marker_note = ?, marker_since = COALESCE(marker_since, ?) WHERE id = ?",
                 (note, now, sid))
    _annotate(conn, now, "marker_set", sid, s["host_id"], text=note)


def clear_marker(conn, sid: str, now: float) -> None:
    s = _session(conn, sid)
    if s["marker_since"] is None:
        return
    conn.execute("UPDATE sessions SET marker_note = NULL, marker_since = NULL WHERE id = ?", (sid,))
    _annotate(conn, now, "marker_cleared", sid, s["host_id"])


def add_note(conn, sid: str, text, now: float) -> None:
    s = _session(conn, sid)
    text = _text(text)
    if not text:
        raise Conflict("note is empty")
    _annotate(conn, now, "note", sid, s["host_id"], text=text)


def mark_ended(conn, sid: str, note, now: float) -> None:
    """Manually end a session (e.g. confirmed dead). Late events will not revive it."""
    s = _session(conn, sid)
    if s["state"] == "ended":
        raise Conflict("session already ended")
    conn.execute(
        "UPDATE sessions SET state = 'ended', state_since = ?, activity = NULL, ended_at = ?,"
        " end_reason = 'ended_manually' WHERE id = ?",
        (now, now, sid),
    )
    conn.execute("UPDATE subagents SET ended_at = ?, activity = NULL WHERE session_id = ? AND ended_at IS NULL",
                 (now, sid))
    _annotate(conn, now, "ended", sid, s["host_id"], text=_text(note))


def archive(conn, sid: str, now: float) -> None:
    s = _session(conn, sid)
    if s["state"] != "ended":
        raise Conflict("only ended sessions can be archived")
    conn.execute("UPDATE sessions SET archived_at = ? WHERE id = ?", (now, sid))
    _annotate(conn, now, "archived", sid, s["host_id"])


def unarchive(conn, sid: str, now: float) -> None:
    s = _session(conn, sid)
    conn.execute("UPDATE sessions SET archived_at = NULL WHERE id = ?", (sid,))
    _annotate(conn, now, "unarchived", sid, s["host_id"])


# -- alerts -----------------------------------------------------------------

def ack_alert(conn, alert_id: int, note, now: float) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM alerts WHERE id = ?", (alert_id,)).fetchone()
    if row is None:
        raise NotFound("alert not found")
    if row["resolved_at"] is not None:
        raise Conflict("alert already resolved")
    note = _text(note)
    conn.execute("UPDATE alerts SET acked_at = COALESCE(acked_at, ?), note = COALESCE(?, note) WHERE id = ?",
                 (now, note, alert_id))
    _annotate(conn, now, "ack", row["session_id"], row["host_id"], alert_id, note)
    return row


# -- history ----------------------------------------------------------------

def history(conn, sid: str, limit: int = HISTORY_LIMIT) -> dict:
    """Merged timeline (newest first): hook events, annotations, alert lifecycle."""
    _session(conn, sid)
    items = []
    for r in conn.execute(
        "SELECT received_at, type, data FROM events WHERE session_id = ? ORDER BY ts DESC LIMIT ?", (sid, limit)
    ):
        data = json.loads(r["data"])
        items.append({"at": r["received_at"], "source": "event", "type": r["type"],
                      **{k: data[k] for k in ("tool_name", "tool_summary", "agent_id", "agent_type", "source",
                                              "reason", "notification_type", "is_agent_message",
                                              "prompt_excerpt", "background_task_count") if k in data}})
    for r in conn.execute(
        "SELECT created_at, kind, text, alert_id FROM annotations WHERE session_id = ?"
        " ORDER BY created_at DESC LIMIT ?", (sid, limit)
    ):
        items.append({"at": r["created_at"], "source": "annotation", "type": r["kind"], "text": r["text"],
                      "alert_id": r["alert_id"]})
    for r in conn.execute("SELECT * FROM alerts WHERE session_id = ?", (sid,)):
        items.append({"at": r["opened_at"], "source": "alert", "type": "alert_opened", "kind": r["kind"],
                      "alert_id": r["id"]})
        if r["resolved_at"]:
            items.append({"at": r["resolved_at"], "source": "alert", "type": "alert_resolved", "kind": r["kind"],
                          "alert_id": r["id"], "resolution": r["resolution"]})
    items.sort(key=lambda i: i["at"], reverse=True)
    total = conn.execute("SELECT COUNT(*) FROM events WHERE session_id = ?", (sid,)).fetchone()[0]
    return {"items": items[:limit], "truncated": total > limit}


# -- retention --------------------------------------------------------------

def prune(conn, retention: float, now: float) -> dict:
    """Delete history older than `retention` seconds. Returns counts."""
    cutoff = now - retention
    c = conn
    c.execute("BEGIN IMMEDIATE")
    try:
        old_sessions = [r["id"] for r in c.execute(
            "SELECT id FROM sessions WHERE state = 'ended' AND ended_at < ?", (cutoff,))]
        for sid in old_sessions:
            c.execute("DELETE FROM annotations WHERE session_id = ?", (sid,))
            c.execute("DELETE FROM alerts WHERE session_id = ?", (sid,))
            c.execute("DELETE FROM subagents WHERE session_id = ?", (sid,))
            c.execute("DELETE FROM events WHERE session_id = ?", (sid,))
            c.execute("DELETE FROM sessions WHERE id = ?", (sid,))
        events = c.execute("DELETE FROM events WHERE received_at < ?", (cutoff,)).rowcount
        c.execute("DELETE FROM annotations WHERE created_at < ? AND (alert_id IS NULL OR alert_id IN"
                  " (SELECT id FROM alerts WHERE resolved_at IS NOT NULL))", (cutoff,))
        c.execute("UPDATE annotations SET alert_id = NULL WHERE alert_id IN"
                  " (SELECT id FROM alerts WHERE resolved_at < ?)", (cutoff,))
        alerts = c.execute("DELETE FROM alerts WHERE resolved_at < ?", (cutoff,)).rowcount
        c.execute("COMMIT")
    except Exception:
        c.execute("ROLLBACK")
        raise
    return {"sessions": len(old_sessions), "events": events, "alerts": alerts}
