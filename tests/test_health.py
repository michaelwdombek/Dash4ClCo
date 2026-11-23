import pytest

from d4cc_server import board, db
from d4cc_server.config import Config
from d4cc_server.health import Health, InvalidHeartbeat

MIN = 60


@pytest.fixture
def cfg():
    return Config(db_path=":memory:", auth_disabled=True)


@pytest.fixture
def health(conn, cfg):
    return Health(conn, cfg)


def open_alerts(conn):
    return {(r["kind"], r["session_id"]) for r in
            conn.execute("SELECT kind, session_id FROM alerts WHERE resolved_at IS NULL")}


def start(ingestor, host, sid, t, pid=100, **kw):
    ingestor.apply(host["id"], {"event": "SessionStart", "session_id": sid, "ts": t, "claude_pid": pid,
                                "source": "startup", **kw}, received_at=t)


def hb(health, host, t, pids=(), **kw):
    health.heartbeat(host["id"], {"processes": [{"pid": p} for p in pids], **kw}, t)


def test_heartbeat_updates_host(conn, host, health):
    hb(health, host, 1000, pids=[1], claude_installed_version="2.1.330", os_family="debian",
       client_version="0.2.0", hostname="ws1.lan")
    h = board.get_host(conn, host["id"])
    assert h["last_heartbeat_at"] == 1000
    assert h["claude_installed_version"] == "2.1.330"
    assert h["os_family"] == "debian" and h["processes"] == 1


@pytest.mark.parametrize("bad", ["x", {"processes": "nope"}, {"processes": [{}] * 500}])
def test_invalid_heartbeat(health, host, bad):
    with pytest.raises(InvalidHeartbeat):
        health.heartbeat(host["id"], bad, 1)


def test_offline_after_threshold_and_auto_resolve(conn, host, health):
    hb(health, host, 1000)
    assert not health.reconcile(1000 + 14 * MIN)
    assert health.reconcile(1000 + 16 * MIN)
    assert open_alerts(conn) == {("offline", None)}
    hb(health, host, 1000 + 20 * MIN)  # VPN back
    assert health.reconcile(1000 + 20 * MIN)
    assert open_alerts(conn) == set()
    row = conn.execute("SELECT resolution FROM alerts").fetchone()
    assert row["resolution"] == "auto"


def test_host_without_heartbeat_is_never_offline(conn, host, health, ingestor):
    start(ingestor, host, "s", 1000)
    health.reconcile(1000 + 10 * 3600)
    assert ("offline", None) not in open_alerts(conn)


def test_stuck_session(conn, host, health, ingestor):
    start(ingestor, host, "s", 1000)
    ingestor.apply(host["id"], {"event": "PreToolUse", "session_id": "s", "ts": 1001, "tool_name": "Bash"}, 1001)
    hb(health, host, 1000 + 31 * MIN, pids=[100])
    health.reconcile(1000 + 29 * MIN)
    assert open_alerts(conn) == set()
    health.reconcile(1000 + 32 * MIN)
    assert open_alerts(conn) == {("stuck", "s")}
    # the session moves on -> resolved automatically
    ingestor.apply(host["id"], {"event": "PostToolUse", "session_id": "s", "ts": 1002, "tool_name": "Bash"},
                   1000 + 33 * MIN)
    health.reconcile(1000 + 33 * MIN)
    assert open_alerts(conn) == set()


def test_idle_session_is_never_stuck(conn, host, health, ingestor):
    start(ingestor, host, "s", 1000)
    hb(health, host, 1000 + 10 * 3600, pids=[100])
    health.reconcile(1000 + 10 * 3600)
    assert open_alerts(conn) == set()


def test_stuck_suppressed_while_host_offline(conn, host, health, ingestor):
    hb(health, host, 1000, pids=[100])
    start(ingestor, host, "s", 1000)
    ingestor.apply(host["id"], {"event": "UserPromptSubmit", "session_id": "s", "ts": 1001}, 1001)
    health.reconcile(1000 + 40 * MIN)
    assert open_alerts(conn) == {("offline", None)}


def test_dead_session_after_two_missing_heartbeats(conn, host, health, ingestor):
    start(ingestor, host, "s", 1000, pid=100)
    hb(health, host, 1060, pids=[100])
    hb(health, host, 1120, pids=[])
    health.reconcile(1120)
    assert open_alerts(conn) == set()          # one miss is tolerated
    hb(health, host, 1180, pids=[])
    health.reconcile(1180)
    assert open_alerts(conn) == {("dead", "s")}


def test_dead_alert_resolves_when_process_reappears(conn, host, health, ingestor):
    start(ingestor, host, "s", 1000, pid=100)
    for t in (1060, 1120):
        hb(health, host, t, pids=[])
    health.reconcile(1120)
    assert open_alerts(conn) == {("dead", "s")}
    hb(health, host, 1180, pids=[100])
    health.reconcile(1180)
    assert open_alerts(conn) == set()


def test_event_resets_missing_counter(conn, host, health, ingestor):
    start(ingestor, host, "s", 1000, pid=100)
    hb(health, host, 1060, pids=[])
    ingestor.apply(host["id"], {"event": "Stop", "session_id": "s", "ts": 1061}, 1061)
    hb(health, host, 1120, pids=[])
    health.reconcile(1120)
    assert open_alerts(conn) == set()


def test_ended_sessions_and_sessions_without_pid_ignored(conn, host, health, ingestor):
    start(ingestor, host, "ended", 1000, pid=100)
    ingestor.apply(host["id"], {"event": "SessionEnd", "session_id": "ended", "ts": 1001}, 1001)
    ingestor.apply(host["id"], {"event": "PreToolUse", "session_id": "nopid", "ts": 1002}, 1002)
    for t in (1060, 1120, 1180):
        hb(health, host, t, pids=[])
    health.reconcile(1180)
    assert open_alerts(conn) == set()


def test_session_end_resolves_dead_alert(conn, host, health, ingestor):
    start(ingestor, host, "s", 1000, pid=100)
    for t in (1060, 1120):
        hb(health, host, t, pids=[])
    health.reconcile(1120)
    ingestor.apply(host["id"], {"event": "SessionEnd", "session_id": "s", "ts": 1121}, 1121)
    health.reconcile(1121)
    assert open_alerts(conn) == set()


def test_alerts_exposed_on_board(conn, host, health, ingestor, cfg):
    start(ingestor, host, "s", 1000, pid=100)
    for t in (1060, 1120):
        hb(health, host, t, pids=[])
    health.reconcile(1120)
    s = board.get_session(conn, "s")
    assert [a["kind"] for a in s["alerts"]] == ["dead"]
    health.reconcile(1120 + 20 * MIN)
    h = board.get_host(conn, host["id"])
    assert [a["kind"] for a in h["alerts"]] == ["offline"]


def test_thresholds_configurable(conn, host, ingestor):
    health = Health(conn, Config(db_path=":memory:", auth_disabled=True, offline_after=5 * MIN))
    health.heartbeat(host["id"], {"processes": []}, 1000)
    health.reconcile(1000 + 6 * MIN)
    assert open_alerts(conn) == {("offline", None)}
