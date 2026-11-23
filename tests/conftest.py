import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from d4cc_server import db
from d4cc_server.app import create_app
from d4cc_server.config import Config
from d4cc_server.ingest import Ingestor

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures"
ADMIN = "admin-secret"


def load_fixture(name: str) -> list[dict]:
    return [json.loads(line) for line in (FIXTURES / name).read_text().splitlines() if line.strip()]


def to_event(raw: dict, ts: float, **extra) -> dict:
    """Approximates what d4cc-hook sends for a raw hook payload (server-side tests)."""
    ev = {
        "event": raw["hook_event_name"],
        "session_id": raw["session_id"],
        "ts": ts,
        "cwd": raw.get("cwd"),
    }
    for key in ("source", "reason", "tool_name", "tool_use_id", "agent_id", "agent_type",
                "notification_type", "model", "permission_mode"):
        if raw.get(key):
            ev[key] = raw[key]
    ti = raw.get("tool_input") or {}
    if raw.get("tool_name") in ("Agent", "Task"):
        ev["tool_summary"] = ti.get("description")
        ev["subagent_type"] = ti.get("subagent_type", "general-purpose")
    elif raw.get("tool_name") == "Bash":
        ev["tool_summary"] = ti.get("description")
    if isinstance(raw.get("background_tasks"), list):
        ev["background_task_count"] = len(raw["background_tasks"])
    if raw["hook_event_name"] == "UserPromptSubmit":
        ev["is_agent_message"] = raw.get("prompt", "").startswith("<agent-message")
    ev.update(extra)
    return ev


@pytest.fixture
def conn(tmp_path):
    c = db.connect(str(tmp_path / "test.db"))
    yield c
    c.close()


@pytest.fixture
def host(conn):
    token = db.create_host(conn, "ws1")
    return {"id": db.host_for_token(conn, token)["id"], "token": token}


@pytest.fixture
def ingestor(conn):
    return Ingestor(conn)


@pytest.fixture
def app(tmp_path):
    return create_app(Config(db_path=str(tmp_path / "app.db"), admin_token=ADMIN))


@pytest.fixture
def client(app):
    with TestClient(app) as c:
        yield c


@pytest.fixture
def host_token(app):
    return db.create_host(app.state.conn, "ws1")
