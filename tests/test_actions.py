import pytest

from d4cc_server import actions, board, settings
from d4cc_server.config import Config
from d4cc_server.health import Health

from conftest import ADMIN


def run(conn, fn, *args, now=2000.0):
    conn.execute("BEGIN")
    try:
        result = fn(conn, *args, now)
        conn.execute("COMMIT")
        return result
    except Exception:
        conn.execute("ROLLBACK")
        raise


@pytest.fixture
def session(conn, host, ingestor):
    ingestor.apply(host["id"], {"event": "SessionStart", "session_id": "s", "ts": 1000, "claude_pid": 7,
                                "source": "startup"}, 1000)
    ingestor.apply(host["id"], {"event": "PreToolUse", "session_id": "s", "ts": 1001, "tool_name": "Bash",
                                "tool_summary": "Run tests"}, 1001)
    return "s"


def kinds(conn, sid):
    return [r["kind"] for r in conn.execute("SELECT kind FROM annotations WHERE session_id = ? ORDER BY id", (sid,))]


def test_marker_set_and_clear(conn, session):
    run(conn, actions.set_marker, session, "  looking into it  ")
    s = board.get_session(conn, session)
    assert s["marker_note"] == "looking into it" and s["marker_since"] == 2000
    run(conn, actions.set_marker, session, "still on it", now=2100)
    assert board.get_session(conn, session)["marker_since"] == 2000  # keeps original start
    run(conn, actions.clear_marker, session, now=2200)
    s = board.get_session(conn, session)
    assert s["marker_note"] is None and s["marker_since"] is None
    assert kinds(conn, session) == ["marker_set", "marker_set", "marker_cleared"]


def test_note_requires_text(conn, session):
    with pytest.raises(actions.Conflict):
        run(conn, actions.add_note, session, "   ")
    run(conn, actions.add_note, session, "rebooted ws3")
    assert kinds(conn, session) == ["note"]


def test_unknown_session(conn):
    with pytest.raises(actions.NotFound):
        run(conn, actions.add_note, "nope", "x")


def test_mark_ended_resolves_dead_alert_and_blocks_revival(conn, host, session, ingestor):
    health = Health(conn, Config(db_path=":memory:", auth_disabled=True))
    for t in (1060, 1120):
        health.heartbeat(host["id"], {"processes": []}, t)
    health.reconcile(1120)
    assert board.get_session(conn, session)["alerts"][0]["kind"] == "dead"
    run(conn, actions.mark_ended, session, "pane killed")
    health.reconcile(2001)
    s = board.get_session(conn, session)
    assert s["state"] == "ended" and s["end_reason"] == "ended_manually" and s["alerts"] == []
    # a late event does not bring it back
    ingestor.apply(host["id"], {"event": "Stop", "session_id": session, "ts": 3000}, 3000)
    assert board.get_session(conn, session)["state"] == "ended"
    with pytest.raises(actions.Conflict):
        run(conn, actions.mark_ended, session, None)


def test_archive_only_ended_and_restore(conn, session):
    with pytest.raises(actions.Conflict):
        run(conn, actions.archive, session)
    run(conn, actions.mark_ended, session, None)
    run(conn, actions.archive, session)
    cfg = Config(db_path=":memory:", auth_disabled=True, ended_visible=10 ** 10)
    assert board.board(conn, cfg)["sessions"] == []
    run(conn, actions.unarchive, session)
    assert [s["id"] for s in board.board(conn, cfg)["sessions"]] == [session]


def test_ack_alert(conn, host, session):
    health = Health(conn, Config(db_path=":memory:", auth_disabled=True))
    health.heartbeat(host["id"], {"processes": [{"pid": 7}]}, 1000)
    health.reconcile(1000 + 3600)  # host offline
    alert_id = conn.execute("SELECT id FROM alerts").fetchone()["id"]
    row = run(conn, actions.ack_alert, alert_id, "VPN maintenance")
    assert row["host_id"] == host["id"]
    a = board.get_host(conn, host["id"])["alerts"][0]
    assert a["acked_at"] == 2000 and a["note"] == "VPN maintenance"
    # acked alerts stay open until the condition clears
    health.reconcile(5000)
    assert board.get_host(conn, host["id"])["alerts"]
    health.heartbeat(host["id"], {"processes": []}, 5001)
    health.reconcile(5001)
    assert board.get_host(conn, host["id"])["alerts"] == []
    with pytest.raises(actions.Conflict):
        run(conn, actions.ack_alert, alert_id, None)


def test_history_merges_events_annotations_and_alerts(conn, host, session):
    run(conn, actions.add_note, session, "hello")
    health = Health(conn, Config(db_path=":memory:", auth_disabled=True, stuck_after=60))
    health.heartbeat(host["id"], {"processes": [{"pid": 7}]}, 1500)
    health.reconcile(1500)
    hist = actions.history(conn, session)
    types = [i["type"] for i in hist["items"]]
    assert types == ["note", "alert_opened", "PreToolUse", "SessionStart"]  # newest first
    tool = hist["items"][2]
    assert tool["tool_name"] == "Bash" and tool["tool_summary"] == "Run tests"
    assert hist["truncated"] is False


def test_prune_respects_retention(conn, host, ingestor, session):
    day = 86400
    ingestor.apply(host["id"], {"event": "SessionStart", "session_id": "old", "ts": 10}, 10)
    run(conn, actions.mark_ended, "old", None, now=20)
    run(conn, actions.add_note, "old", "bye", now=21)
    counts = actions.prune(conn, 30 * day, now=30 * day + 500)  # cutoff at t=500
    assert counts["sessions"] == 1
    assert board.get_session(conn, "old") is None
    assert board.get_session(conn, session) is not None
    # events of the still-open session older than the cutoff are dropped, the session stays
    assert conn.execute("SELECT COUNT(*) FROM events WHERE session_id = ?", (session,)).fetchone()[0] == 2
    actions.prune(conn, 1, now=10 * day)
    assert conn.execute("SELECT COUNT(*) FROM events WHERE session_id = ?", (session,)).fetchone()[0] == 0
    assert board.get_session(conn, session)["state"] == "working"


# -- settings -----------------------------------------------------------------

def test_settings_roundtrip_and_persistence(conn):
    cfg = Config(db_path=":memory:", auth_disabled=True)
    assert settings.current(cfg)["stuck_after_min"] == 30
    settings.save(conn, cfg, {"stuck_after_min": 45, "offline_after_min": 20, "min_claude_version": "2.1.330"})
    assert cfg.stuck_after == 45 * 60 and cfg.offline_after == 20 * 60
    fresh = Config(db_path=":memory:", auth_disabled=True)
    settings.load(conn, fresh)
    assert fresh.stuck_after == 45 * 60 and fresh.min_claude_version == "2.1.330"
    settings.save(conn, cfg, {"min_claude_version": ""})
    settings.load(conn, fresh)
    assert fresh.min_claude_version is None


@pytest.mark.parametrize("bad", [
    {"stuck_after_min": 0}, {"stuck_after_min": "x"}, {"dead_after_heartbeats": 1.5},
    {"min_claude_version": "latest"}, {"bogus": 1},
])
def test_settings_validation_is_all_or_nothing(conn, bad):
    cfg = Config(db_path=":memory:", auth_disabled=True)
    with pytest.raises(settings.InvalidSetting):
        settings.save(conn, cfg, {"offline_after_min": 99, **bad})
    assert cfg.offline_after == 15 * 60
    assert conn.execute("SELECT COUNT(*) FROM settings").fetchone()[0] == 0


# -- API ------------------------------------------------------------------------

def login(client):
    client.post("/login", data={"token": ADMIN}, follow_redirects=False)


def ingest(client, token, ev):
    client.post("/api/events", headers={"Authorization": f"Bearer {token}"}, json=ev)


def test_action_endpoints(client, host_token):
    ingest(client, host_token, {"event": "SessionStart", "session_id": "s1", "ts": 1})
    assert client.post("/api/sessions/s1/notes", json={"text": "x"}).status_code == 401
    login(client)
    assert client.post("/api/sessions/s1/marker", json={"note": "on it"}).status_code == 200
    assert client.get("/api/board").json()["sessions"][0]["marker_note"] == "on it"
    assert client.delete("/api/sessions/s1/marker").status_code == 200
    assert client.post("/api/sessions/s1/notes", json={"text": ""}).status_code == 409
    assert client.post("/api/sessions/nope/notes", json={"text": "x"}).status_code == 404
    # form posts (no JSON content type) are rejected - CSRF hardening
    assert client.post("/api/sessions/s1/notes", data={"text": "x"}).status_code == 415
    assert client.post("/api/sessions/s1/end", json={"note": "gone"}).status_code == 200
    assert client.post("/api/sessions/s1/archive", json={}).status_code == 200
    assert client.get("/api/board").json()["sessions"] == []
    assert [s["id"] for s in client.get("/api/archived").json()["sessions"]] == ["s1"]
    hist = client.get("/api/sessions/s1/history").json()
    assert [i["type"] for i in hist["items"]][:3] == ["archived", "ended", "marker_cleared"]


def test_settings_endpoints(client, app):
    login(client)
    data = client.get("/api/settings").json()
    assert data["values"]["offline_after_min"] == 15
    r = client.put("/api/settings", json={"offline_after_min": 25})
    assert r.status_code == 200 and app.state.cfg.offline_after == 25 * 60
    assert client.put("/api/settings", json={"offline_after_min": -1}).status_code == 422
    assert client.get("/settings").status_code == 200
