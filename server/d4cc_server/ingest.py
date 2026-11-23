"""Event ingestion: turns hook events into session / subagent state.

Ordering: the client sends events fire-and-forget in the background, so two
events fired milliseconds apart may arrive out of order. Every event is stored
in the history, but session *state* is only changed by events whose client
timestamp is not older than the last applied one. Durations shown in the UI
use the server's receive time, so clock skew between hosts does not matter.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field

KNOWN_EVENTS = {
    "SessionStart",
    "UserPromptSubmit",
    "PreToolUse",
    "PostToolUse",
    "Notification",
    "Stop",
    "SubagentStart",
    "SubagentStop",
    "SessionEnd",
}

AGENT_TOOLS = {"Agent", "Task"}
NEEDS_YOU_NOTIFICATIONS = {"permission_prompt", "elicitation_dialog"}

# Fields copied from the event onto the session row when present.
SESSION_META = (
    "cwd",
    "tmux_pane",
    "tmux_target",
    "claude_pid",
    "claude_version",
    "model",
    "permission_mode",
)

MAX_STR = 500
# Agent tool call and SubagentStart fire within milliseconds of each other and
# may arrive in either order; they are paired within this window (seconds).
PAIRING_WINDOW = 10.0


class InvalidEvent(ValueError):
    pass


@dataclass
class Result:
    sessions: set[str] = field(default_factory=set)


def _clean(ev: dict) -> dict:
    """Keep only scalar fields and cap string lengths before storing."""
    out = {}
    for key, value in ev.items():
        if isinstance(value, str):
            out[key] = value[:MAX_STR]
        elif isinstance(value, (int, float, bool)) or value is None:
            out[key] = value
    return out


def _activity(ev: dict) -> str:
    tool = ev.get("tool_name") or "tool"
    summary = ev.get("tool_summary")
    return f"{tool}: {summary}" if summary else tool


class Ingestor:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    # -- public ---------------------------------------------------------

    def apply(self, host_id: int, raw: dict, received_at: float) -> Result:
        if not isinstance(raw, dict):
            raise InvalidEvent("event must be an object")
        etype = raw.get("event")
        sid = raw.get("session_id")
        if etype not in KNOWN_EVENTS:
            raise InvalidEvent(f"unknown event type: {etype!r}")
        if not isinstance(sid, str) or not sid or len(sid) > 100:
            raise InvalidEvent("missing or invalid session_id")
        ev = _clean(raw)
        try:
            ts = float(ev.get("ts") or received_at)
        except (TypeError, ValueError):
            ts = received_at

        result = Result({sid})
        c = self.conn
        c.execute("BEGIN IMMEDIATE")
        try:
            c.execute(
                "INSERT INTO events(session_id, host_id, ts, received_at, type, data)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (sid, host_id, ts, received_at, etype, json.dumps(ev)),
            )
            c.execute("UPDATE hosts SET last_seen_at = ?, hostname_reported = COALESCE(?, hostname_reported),"
                      " client_version = COALESCE(?, client_version) WHERE id = ?",
                      (received_at, ev.get("hostname"), ev.get("client_version"), host_id))
            session = self._get(sid)
            if session is None:
                self._create(sid, host_id, ev, received_at)
                session = self._get(sid)
            if session["host_id"] != host_id:
                # Same session id reported by another host: keep history, ignore state.
                c.execute("COMMIT")
                return result
            self._handle(session, etype, ev, ts, received_at, result)
            c.execute("COMMIT")
        except Exception:
            c.execute("ROLLBACK")
            raise
        return result

    # -- helpers --------------------------------------------------------

    def _get(self, sid: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM sessions WHERE id = ?", (sid,)).fetchone()

    def _create(self, sid: str, host_id: int, ev: dict, now: float) -> None:
        source = ev.get("source") if ev.get("event") == "SessionStart" else "observed"
        self.conn.execute(
            "INSERT INTO sessions(id, host_id, source, started_at, last_event_at, state, state_since)"
            " VALUES (?, ?, ?, ?, ?, 'idle', ?)",
            (sid, host_id, source, now, now, now),
        )

    def _update(self, sid: str, **fields) -> None:
        if not fields:
            return
        cols = ", ".join(f"{k} = ?" for k in fields)
        self.conn.execute(f"UPDATE sessions SET {cols} WHERE id = ?", (*fields.values(), sid))

    def _handle(self, s: sqlite3.Row, etype: str, ev: dict, ts: float, now: float, result: Result) -> None:
        sid = s["id"]
        fresh = ts >= (s["last_client_ts"] or 0)
        # Any event proves the session's process is alive.
        upd: dict = {"last_event_at": now, "missing_heartbeats": 0}
        if fresh:
            upd["last_client_ts"] = ts
        for key in SESSION_META:
            if ev.get(key) not in (None, "") and (fresh or s[key] is None):
                upd[key] = ev[key]

        def set_state(state: str, activity=..., **extra) -> None:
            if not fresh:
                return
            if state != s["state"]:
                upd["state"] = state
                upd["state_since"] = now
            if activity is not ...:
                if activity != s["activity"]:
                    upd["activity_since"] = now
                upd["activity"] = activity
            upd.update(extra)

        agent_id = ev.get("agent_id")
        ended = s["state"] == "ended"

        if etype == "SessionStart":
            source = ev.get("source")
            if ev.get("tmux_pane") and source in ("startup", "clear", "resume"):
                self._replace_pane_sessions(s, ev["tmux_pane"], now, result)
            if source in ("startup", "clear"):
                upd["started_at"] = now
            if source:
                upd["source"] = source
            if ended or fresh:
                # A resume revives an ended card.
                fresh = True
                set_state("idle", None, ended_at=None, end_reason=None, replaced_by=None, archived_at=None)

        elif ended:
            pass  # late events of an ended session only go to the history

        elif etype == "UserPromptSubmit":
            set_state("working", "Processing subagent result" if ev.get("is_agent_message") else "Thinking")

        elif etype == "PreToolUse":
            if agent_id:
                self._subagent_activity(sid, agent_id, _activity(ev))
            else:
                set_state("working", _activity(ev))
                if ev.get("tool_name") in AGENT_TOOLS:
                    self._agent_tool_call(sid, ev, now)

        elif etype == "PostToolUse":
            # Note: an Agent tool call does not end its subagent here. Subagents
            # run in the background and end on SubagentStop.
            if agent_id:
                self._subagent_activity(sid, agent_id, None)
            elif s["state"] in ("working", "needs_you"):
                set_state("working", "Thinking")

        elif etype == "Notification":
            ntype = ev.get("notification_type")
            if ntype in NEEDS_YOU_NOTIFICATIONS:
                set_state("needs_you")
            elif ntype == "idle_prompt":
                set_state("idle", None)

        elif etype == "Stop":
            set_state("idle", None, bg_tasks=ev.get("background_task_count"))
            # Agent tool calls whose subagent never started (e.g. denied) are closed at turn end.
            self.conn.execute(
                "UPDATE subagents SET ended_at = ? WHERE session_id = ? AND agent_id IS NULL"
                " AND ended_at IS NULL AND started_at < ?",
                (now, sid, now - PAIRING_WINDOW),
            )

        elif etype == "SubagentStart":
            self._subagent_start(sid, ev, now)

        elif etype == "SubagentStop":
            if agent_id:
                self.conn.execute(
                    "UPDATE subagents SET ended_at = ?, activity = NULL"
                    " WHERE session_id = ? AND agent_id = ? AND ended_at IS NULL",
                    (now, sid, agent_id),
                )

        elif etype == "SessionEnd":
            fresh = True  # SessionEnd is final regardless of ordering
            set_state("ended", None, ended_at=now, end_reason=ev.get("reason"))
            self._end_subagents(sid, now)

        self._update(sid, **upd)

    def _replace_pane_sessions(self, s: sqlite3.Row, pane: str, now: float, result: Result) -> None:
        rows = self.conn.execute(
            "SELECT id FROM sessions WHERE host_id = ? AND tmux_pane = ? AND id != ? AND state != 'ended'",
            (s["host_id"], pane, s["id"]),
        ).fetchall()
        for row in rows:
            self._update(row["id"], state="ended", state_since=now, activity=None, ended_at=now,
                         end_reason="replaced", replaced_by=s["id"])
            self._end_subagents(row["id"], now)
            result.sessions.add(row["id"])

    def _end_subagents(self, sid: str, now: float) -> None:
        self.conn.execute(
            "UPDATE subagents SET ended_at = ?, activity = NULL WHERE session_id = ? AND ended_at IS NULL",
            (now, sid),
        )

    def _subagent_activity(self, sid: str, agent_id: str, activity: str | None) -> None:
        self.conn.execute(
            "UPDATE subagents SET activity = ? WHERE session_id = ? AND agent_id = ? AND ended_at IS NULL",
            (activity, sid, agent_id),
        )

    def _agent_tool_call(self, sid: str, ev: dict, now: float) -> None:
        """Agent tool call (carries the description). Its SubagentStart may already have arrived."""
        agent_type = ev.get("subagent_type")
        early = self.conn.execute(
            "SELECT id FROM subagents WHERE session_id = ? AND agent_id IS NOT NULL AND tool_use_id IS NULL"
            " AND (? IS NULL OR agent_type = ?) AND started_at >= ? ORDER BY id LIMIT 1",
            (sid, agent_type, agent_type, now - PAIRING_WINDOW),
        ).fetchone()
        if early:
            self.conn.execute(
                "UPDATE subagents SET description = ?, tool_use_id = ? WHERE id = ?",
                (ev.get("tool_summary"), ev.get("tool_use_id"), early["id"]),
            )
            return
        self.conn.execute(
            "INSERT INTO subagents(session_id, agent_type, description, tool_use_id, started_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (sid, agent_type, ev.get("tool_summary"), ev.get("tool_use_id"), now),
        )

    def _subagent_start(self, sid: str, ev: dict, now: float) -> None:
        agent_id, agent_type = ev.get("agent_id"), ev.get("agent_type")
        if not agent_id or not agent_type:
            return  # internal helper agents report an empty type and are not shown
        exists = self.conn.execute(
            "SELECT 1 FROM subagents WHERE session_id = ? AND agent_id = ?", (sid, agent_id)
        ).fetchone()
        if exists:
            return
        # Attach to the oldest pending Agent tool call of the same type (it carries the description).
        placeholder = self.conn.execute(
            "SELECT id FROM subagents WHERE session_id = ? AND agent_id IS NULL AND ended_at IS NULL"
            " AND (agent_type IS NULL OR agent_type = ?) ORDER BY id LIMIT 1",
            (sid, agent_type),
        ).fetchone()
        if placeholder:
            self.conn.execute(
                "UPDATE subagents SET agent_id = ?, agent_type = ?, started_at = ? WHERE id = ?",
                (agent_id, agent_type, now, placeholder["id"]),
            )
        else:
            self.conn.execute(
                "INSERT INTO subagents(session_id, agent_id, agent_type, started_at) VALUES (?, ?, ?, ?)",
                (sid, agent_id, agent_type, now),
            )
