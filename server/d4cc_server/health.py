"""Health: host heartbeats and alert evaluation.

Alert kinds
- offline: no heartbeat from a host for more than `offline_after`.
- stuck:   session is `working` but sent no event for more than `stuck_after`.
- dead:    host heartbeats fine, but the session's claude process has been missing
           from `dead_after_heartbeats` consecutive heartbeats and no SessionEnd came.

`reconcile()` compares the conditions that currently hold with the open alerts:
missing alerts are opened, alerts whose condition cleared are resolved
automatically. It runs after every ingest/heartbeat and periodically.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass

from .config import Config

MAX_PROCESSES = 200


class InvalidHeartbeat(ValueError):
    pass


@dataclass(frozen=True)
class Condition:
    kind: str
    host_id: int
    session_id: str | None


class Health:
    def __init__(self, conn: sqlite3.Connection, cfg: Config):
        self.conn = conn
        self.cfg = cfg

    # -- heartbeat ----------------------------------------------------------

    def heartbeat(self, host_id: int, hb: dict, now: float) -> None:
        if not isinstance(hb, dict):
            raise InvalidHeartbeat("heartbeat must be an object")
        procs = hb.get("processes") or []
        if not isinstance(procs, list) or len(procs) > MAX_PROCESSES:
            raise InvalidHeartbeat("processes must be a list")
        alive: set[int] = set()
        clean_procs = []
        for p in procs:
            if isinstance(p, dict) and isinstance(p.get("pid"), int):
                alive.add(p["pid"])
                clean_procs.append({k: p[k] for k in ("pid", "tmux_pane", "cwd", "version")
                                    if isinstance(p.get(k), (str, int))})

        def s(key):
            v = hb.get(key)
            return v[:100] if isinstance(v, str) and v else None

        c = self.conn
        c.execute("BEGIN IMMEDIATE")
        try:
            c.execute(
                "UPDATE hosts SET last_heartbeat_at = ?, last_seen_at = ?,"
                " hostname_reported = COALESCE(?, hostname_reported),"
                " client_version = COALESCE(?, client_version),"
                " claude_installed_version = COALESCE(?, claude_installed_version),"
                " os_family = COALESCE(?, os_family), heartbeat_json = ? WHERE id = ?",
                (now, now, s("hostname"), s("client_version"), s("claude_installed_version"),
                 s("os_family"), json.dumps({"uptime_s": hb.get("uptime_s"), "processes": clean_procs}),
                 host_id),
            )
            rows = c.execute(
                "SELECT id, claude_pid FROM sessions WHERE host_id = ? AND state != 'ended'"
                " AND claude_pid IS NOT NULL",
                (host_id,),
            ).fetchall()
            for row in rows:
                if row["claude_pid"] in alive:
                    c.execute("UPDATE sessions SET missing_heartbeats = 0 WHERE id = ?", (row["id"],))
                else:
                    c.execute("UPDATE sessions SET missing_heartbeats = missing_heartbeats + 1 WHERE id = ?",
                              (row["id"],))
            c.execute("COMMIT")
        except Exception:
            c.execute("ROLLBACK")
            raise

    # -- alerts -------------------------------------------------------------

    def conditions(self, now: float) -> set[Condition]:
        c, cfg = self.conn, self.cfg
        found: set[Condition] = set()
        offline_hosts = set()
        for r in c.execute(
            "SELECT id FROM hosts WHERE revoked_at IS NULL AND last_heartbeat_at IS NOT NULL"
            " AND last_heartbeat_at < ?",
            (now - cfg.offline_after,),
        ):
            offline_hosts.add(r["id"])
            found.add(Condition("offline", r["id"], None))
        for r in c.execute(
            "SELECT id, host_id FROM sessions WHERE state = 'working' AND archived_at IS NULL"
            " AND last_event_at < ?",
            (now - cfg.stuck_after,),
        ):
            if r["host_id"] not in offline_hosts:
                found.add(Condition("stuck", r["host_id"], r["id"]))
        for r in c.execute(
            "SELECT id, host_id FROM sessions WHERE state != 'ended' AND archived_at IS NULL"
            " AND missing_heartbeats >= ?",
            (cfg.dead_after_heartbeats,),
        ):
            if r["host_id"] not in offline_hosts:
                found.add(Condition("dead", r["host_id"], r["id"]))
        return found

    def reconcile(self, now: float) -> bool:
        """Open/resolve alerts. Returns True if anything changed."""
        c = self.conn
        wanted = self.conditions(now)
        c.execute("BEGIN IMMEDIATE")
        try:
            open_rows = c.execute(
                "SELECT id, kind, host_id, session_id FROM alerts WHERE resolved_at IS NULL"
            ).fetchall()
            existing = {Condition(r["kind"], r["host_id"], r["session_id"]): r["id"] for r in open_rows}
            changed = False
            for cond, alert_id in existing.items():
                if cond not in wanted:
                    c.execute("UPDATE alerts SET resolved_at = ?, resolution = 'auto' WHERE id = ?",
                              (now, alert_id))
                    changed = True
            for cond in wanted - existing.keys():
                c.execute("INSERT INTO alerts(kind, host_id, session_id, opened_at) VALUES (?, ?, ?, ?)",
                          (cond.kind, cond.host_id, cond.session_id, now))
                changed = True
            c.execute("COMMIT")
        except Exception:
            c.execute("ROLLBACK")
            raise
        return changed
