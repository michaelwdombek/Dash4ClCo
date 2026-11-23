from conftest import ADMIN


def login(client):
    r = client.post("/login", data={"token": ADMIN}, follow_redirects=False)
    assert r.status_code == 303


def test_healthz_is_public(client):
    assert client.get("/healthz").json()["ok"] is True


def test_ingest_requires_valid_token(client, host_token):
    ev = {"event": "SessionStart", "session_id": "s1", "ts": 1}
    assert client.post("/api/events", json=ev).status_code == 401
    assert client.post("/api/events", json=ev, headers={"Authorization": "Bearer nope"}).status_code == 401
    r = client.post("/api/events", json=ev, headers={"Authorization": f"Bearer {host_token}"})
    assert r.status_code == 202 and r.json()["accepted"] == 1


def test_revoked_token_rejected(client, app, host_token):
    app.state.conn.execute("UPDATE hosts SET revoked_at = 1")
    r = client.post("/api/events", json={"event": "Stop", "session_id": "s"},
                    headers={"Authorization": f"Bearer {host_token}"})
    assert r.status_code == 401


def test_ingest_batch_and_partial_errors(client, host_token):
    auth = {"Authorization": f"Bearer {host_token}"}
    r = client.post("/api/events", headers=auth, json=[
        {"event": "SessionStart", "session_id": "s1", "ts": 1},
        {"event": "Nope", "session_id": "s1"},
    ])
    assert r.status_code == 202
    assert r.json()["accepted"] == 1 and r.json()["errors"][0]["index"] == 1
    r = client.post("/api/events", headers=auth, json={"event": "Nope", "session_id": "s1"})
    assert r.status_code == 400
    r = client.post("/api/events", headers={**auth, "Content-Type": "application/json"}, content=b"{broken")
    assert r.status_code == 400


def test_board_requires_login(client):
    assert client.get("/api/board").status_code == 401
    assert client.get("/", follow_redirects=False).headers["location"] == "/login"
    r = client.post("/login", data={"token": "wrong"})
    assert r.status_code == 401


def test_board_shows_ingested_session(client, host_token):
    client.post("/api/events", headers={"Authorization": f"Bearer {host_token}"}, json={
        "event": "SessionStart", "session_id": "s1", "ts": 1, "cwd": "/home/u/proj",
        "hostname": "ws1.local", "client_version": "0.1.0", "claude_version": "2.1.330",
    })
    login(client)
    data = client.get("/api/board").json()
    assert data["hosts"][0]["name"] == "ws1"
    assert data["hosts"][0]["client_version"] == "0.1.0"
    s = data["sessions"][0]
    assert s["id"] == "s1" and s["cwd"] == "/home/u/proj" and s["claude_version"] == "2.1.330"
    assert client.get("/").status_code == 200


def test_ingest_publishes_to_hub(client, app, host_token):
    queue = app.state.hub.subscribe()
    client.post("/api/events", headers={"Authorization": f"Bearer {host_token}"},
                json={"event": "UserPromptSubmit", "session_id": "s9", "ts": 1})
    kind, data = queue.get_nowait()
    assert kind == "session" and data["id"] == "s9" and data["state"] == "working"


def test_heartbeat_endpoint(client, host_token):
    auth = {"Authorization": f"Bearer {host_token}"}
    assert client.post("/api/heartbeat", json={"processes": []}).status_code == 401
    assert client.post("/api/heartbeat", headers=auth, json={"processes": "x"}).status_code == 400
    r = client.post("/api/heartbeat", headers=auth, json={
        "claude_installed_version": "2.1.330", "os_family": "redhat",
        "processes": [{"pid": 42, "tmux_pane": "%1", "cwd": "/p", "version": "2.1.330"}]})
    assert r.status_code == 202
    login(client)
    h = client.get("/api/board").json()["hosts"][0]
    assert h["os_family"] == "redhat" and h["processes"] == 1 and h["last_heartbeat_at"]


def test_heartbeat_publishes_host(client, app, host_token):
    queue = app.state.hub.subscribe()
    client.post("/api/heartbeat", headers={"Authorization": f"Bearer {host_token}"}, json={"processes": []})
    kind, data = queue.get_nowait()
    assert kind == "host" and data["name"] == "ws1"
