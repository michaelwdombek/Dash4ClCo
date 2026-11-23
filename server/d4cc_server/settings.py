"""Runtime-editable settings.

Environment variables provide the defaults; values saved from the settings page
are stored in the `settings` table and override them (also after a restart).
Values are exposed in user-facing units (minutes, hours, days).
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass

from .config import Config


class InvalidSetting(ValueError):
    pass


@dataclass(frozen=True)
class Field:
    attr: str          # Config attribute
    scale: float       # Config value = user value * scale
    lo: float
    hi: float
    label: str
    unit: str
    integer: bool = False


FIELDS: dict[str, Field] = {
    "stuck_after_min": Field("stuck_after", 60, 1, 24 * 60, "Stuck after", "minutes"),
    "offline_after_min": Field("offline_after", 60, 1, 24 * 60, "Host offline after", "minutes"),
    "dead_after_heartbeats": Field("dead_after_heartbeats", 1, 1, 60, "Dead after missed heartbeats", "heartbeats",
                                   integer=True),
    "ended_visible_hours": Field("ended_visible", 3600, 0, 24 * 30, "Keep ended sessions on the board", "hours"),
    "retention_days": Field("retention", 86400, 1, 3650, "Keep history for", "days"),
}
VERSION_KEY = "min_claude_version"
VERSION_RE = re.compile(r"^\d+(\.\d+){0,3}$")


def current(cfg: Config) -> dict:
    out = {}
    for key, f in FIELDS.items():
        value = getattr(cfg, f.attr) / f.scale
        out[key] = int(value) if f.integer or value == int(value) else round(value, 2)
    out[VERSION_KEY] = cfg.min_claude_version or ""
    return out


def describe() -> list[dict]:
    fields = [{"key": k, "label": f.label, "unit": f.unit, "min": f.lo, "max": f.hi, "integer": f.integer}
              for k, f in FIELDS.items()]
    fields.append({"key": VERSION_KEY, "label": "Minimum Claude Code version", "unit": "empty = newest in fleet"})
    return fields


def _apply(cfg: Config, key: str, raw) -> str:
    """Validate one value, set it on cfg and return its storable form."""
    if key == VERSION_KEY:
        value = (raw or "").strip() if isinstance(raw, str) or raw is None else None
        if value is None or (value and not VERSION_RE.match(value)):
            raise InvalidSetting(f"{key}: expected a version like 2.1.330")
        cfg.min_claude_version = value or None
        return value
    f = FIELDS.get(key)
    if f is None:
        raise InvalidSetting(f"unknown setting: {key}")
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise InvalidSetting(f"{key}: not a number")
    if f.integer and value != int(value):
        raise InvalidSetting(f"{key}: must be a whole number")
    if not f.lo <= value <= f.hi:
        raise InvalidSetting(f"{key}: must be between {f.lo:g} and {f.hi:g}")
    setattr(cfg, f.attr, int(value) if f.integer else value * f.scale)
    return repr(value)


def load(conn: sqlite3.Connection, cfg: Config) -> None:
    for row in conn.execute("SELECT key, value FROM settings"):
        try:
            _apply(cfg, row["key"], row["value"])
        except InvalidSetting:
            pass  # stale/unknown key: keep the env default


def save(conn: sqlite3.Connection, cfg: Config, values: dict) -> None:
    """Validates all values first; applies and stores them only if all are valid."""
    if not isinstance(values, dict):
        raise InvalidSetting("expected an object")
    trial = Config(**vars(cfg))
    stored = {key: _apply(trial, key, raw) for key, raw in values.items()}
    for key, value in stored.items():
        conn.execute("INSERT INTO settings(key, value) VALUES (?, ?)"
                     " ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))
    for name, value in vars(trial).items():
        setattr(cfg, name, value)
