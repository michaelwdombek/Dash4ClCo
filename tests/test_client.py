"""Client scripts: settings merge + the real hook against a running server."""

import json
import os
import socket
import subprocess
import sys
import time

import httpx
import pytest

from conftest import ADMIN, FIXTURES, ROOT, load_fixture

HOOK = ROOT / "client" / "d4cc-hook"
CLIENT_VERSION = next(line.split('"')[1] for line in HOOK.read_text().splitlines()
                      if line.startswith("D4CC_CLIENT_VERSION="))
MERGE = ROOT / "client" / "d4cc-settings-merge"


# -- settings merge -----------------------------------------------------------

def merge(*args):
    return subprocess.run([str(MERGE), *map(str, args)], capture_output=True, text=True)


def test_merge_preserves_existing_settings_and_is_idempotent(tmp_path):
    f = tmp_path / "settings.json"
    f.write_text(json.dumps({
        "model": "opus",
        "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "notify-send done"}]}]},
    }))
    assert merge("install", f, "/opt/d4cc-hook").returncode == 0
    assert merge("install", f, "/opt/d4cc-hook").returncode == 0
    data = json.loads(f.read_text())
    assert data["model"] == "opus"
    stop_cmds = [h["command"] for e in data["hooks"]["Stop"] for h in e["hooks"]]
    assert stop_cmds == ["notify-send done", "/opt/d4cc-hook Stop"]
    assert len(data["hooks"]) == 9
    assert data["hooks"]["PreToolUse"][0]["matcher"] == "*"
    assert (tmp_path / "settings.json.d4cc-backup").exists()


def test_merge_uninstall_removes_only_ours(tmp_path):
    f = tmp_path / "settings.json"
    f.write_text(json.dumps({"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "keep-me"}]}]}}))
    merge("install", f)
    merge("uninstall", f)
    assert json.loads(f.read_text()) == {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "keep-me"}]}]}}


def test_merge_creates_missing_file_and_refuses_invalid_json(tmp_path):
    f = tmp_path / "sub" / "settings.json"
    assert merge("install", f).returncode == 0
    assert "SessionStart" in json.loads(f.read_text())["hooks"]
    bad = tmp_path / "bad.json"
    bad.write_text("{nope")
    r = merge("install", bad)
    assert r.returncode == 1 and bad.read_text() == "{nope"


# -- hook end-to-end ----------------------------------------------------------

def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("srv")
    port = _free_port()
    env = {**os.environ, "D4CC_DB": str(tmp / "d.db"), "D4CC_ADMIN_TOKEN": ADMIN}
    token = subprocess.run([sys.executable, "-m", "d4cc_server.cli", "add-host", "e2e"],
                           env=env, capture_output=True, text=True, check=True).stdout.strip()
    proc = subprocess.Popen([sys.executable, "-c", "from d4cc_server.cli import serve; serve()",
                             "--host", "127.0.0.1", "--port", str(port)], env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    url = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            httpx.get(url + "/healthz", timeout=0.5)
            break
        except httpx.HTTPError:
            time.sleep(0.1)
    yield {"url": url, "token": token, "tmp": tmp}
    proc.terminate()
    proc.wait(5)


def run_hook(server, event, payload, sync=True, extra_env=None):
    conf = server["tmp"] / "client.env"
    conf.write_text(f"D4CC_URL={server['url']}\nD4CC_TOKEN={server['token']}\n")
    env = {"PATH": os.environ["PATH"], "HOME": str(server["tmp"]), "D4CC_CONFIG": str(conf),
           "D4CC_SYNC": "1" if sync else "0", "TMUX_PANE": "%7", **(extra_env or {})}
    start = time.monotonic()
    r = subprocess.run([str(HOOK), event], input=json.dumps(payload), env=env,
                       capture_output=True, text=True, timeout=10)
    return r, time.monotonic() - start


def admin_client(server):
    c = httpx.Client(base_url=server["url"])
    c.post("/login", data={"token": ADMIN})
    return c


def test_hook_replays_fixture_to_board(server):
    raws = load_fixture("hook-payloads-2.1.330.jsonl")
    for raw in raws:
        r, _ = run_hook(server, raw["hook_event_name"], raw)
        assert r.returncode == 0 and r.stdout == ""
    board = admin_client(server).get("/api/board").json()
    s = next(x for x in board["sessions"] if x["id"] == raws[0]["session_id"])
    assert s["state"] == "ended"
    assert s["tmux_pane"] == "%7"
    assert s["subagents"][0]["agent_type"] == "Explore"
    assert s["subagents"][0]["description"] == "List files in cwd"
    assert board["hosts"][0]["client_version"] == CLIENT_VERSION


def test_hook_privacy_no_prompt_or_command_text(server):
    secret = "export AWS_SECRET=hunter2"
    run_hook(server, "UserPromptSubmit", {"session_id": "priv", "cwd": "/p", "prompt": secret})
    run_hook(server, "PreToolUse", {"session_id": "priv", "cwd": "/p", "tool_name": "Bash",
                                    "tool_input": {"command": secret}, "tool_use_id": "t"})
    db_bytes = b"".join(p.read_bytes() for p in server["tmp"].glob("d.db*"))
    assert b"hunter2" not in db_bytes
    s = next(x for x in admin_client(server).get("/api/board").json()["sessions"] if x["id"] == "priv")
    assert s["activity"] == "Bash: export"


def test_hook_relative_file_paths(server):
    run_hook(server, "PreToolUse", {"session_id": "rel", "cwd": "/home/u/proj", "tool_name": "Edit",
                                    "tool_input": {"file_path": "/home/u/proj/src/app.py"}})
    s = next(x for x in admin_client(server).get("/api/board").json()["sessions"] if x["id"] == "rel")
    assert s["activity"] == "Edit: src/app.py"


def test_hook_never_fails_or_blocks(server, tmp_path):
    # unreachable dashboard, async mode: must return immediately with exit 0
    conf = tmp_path / "c.env"
    conf.write_text("D4CC_URL=http://10.255.255.1:9\nD4CC_TOKEN=x\n")
    env = {"PATH": os.environ["PATH"], "HOME": str(tmp_path), "D4CC_CONFIG": str(conf)}
    start = time.monotonic()
    r = subprocess.run([str(HOOK), "Stop"], input='{"session_id":"x"}', env=env, capture_output=True,
                       text=True, timeout=10)
    assert r.returncode == 0 and time.monotonic() - start < 1.5
    # garbage input and missing config
    r = subprocess.run([str(HOOK), "Stop"], input="not json", env=env, capture_output=True, text=True)
    assert r.returncode == 0
    r = subprocess.run([str(HOOK), "Stop"], input="{}", env={"PATH": os.environ["PATH"], "HOME": str(tmp_path)},
                       capture_output=True, text=True)
    assert r.returncode == 0


def test_hook_async_mode_delivers(server):
    run_hook(server, "SessionStart", {"session_id": "async1", "cwd": "/p", "source": "startup"}, sync=False)
    c = admin_client(server)
    for _ in range(50):
        if any(x["id"] == "async1" for x in c.get("/api/board").json()["sessions"]):
            break
        time.sleep(0.1)
    else:
        pytest.fail("async event never arrived")


HEARTBEAT = ROOT / "client" / "d4cc-heartbeat"


def test_heartbeat_print_mode(tmp_path):
    r = subprocess.run([str(HEARTBEAT), "--print"], env={"PATH": os.environ["PATH"], "HOME": str(tmp_path),
                       "D4CC_CONFIG": "/dev/null"}, capture_output=True, text=True, timeout=20)
    assert r.returncode == 0, r.stderr
    data = json.loads(r.stdout)
    assert data["client_version"] == CLIENT_VERSION
    assert isinstance(data["processes"], list)
    assert all(isinstance(p["pid"], int) for p in data["processes"])


def test_heartbeat_reaches_server(server):
    conf = server["tmp"] / "client.env"
    conf.write_text(f"D4CC_URL={server['url']}\nD4CC_TOKEN={server['token']}\n")
    r = subprocess.run([str(HEARTBEAT)], env={"PATH": os.environ["PATH"], "HOME": str(server["tmp"]),
                       "D4CC_CONFIG": str(conf)}, capture_output=True, text=True, timeout=20)
    assert r.returncode == 0, r.stderr
    host = admin_client(server).get("/api/board").json()["hosts"][0]
    assert host["last_heartbeat_at"] and host["os_family"]


def test_heartbeat_fails_loudly_without_config(tmp_path):
    r = subprocess.run([str(HEARTBEAT)], env={"PATH": os.environ["PATH"], "HOME": str(tmp_path)},
                       capture_output=True, text=True, timeout=20)
    assert r.returncode == 1 and "not configured" in r.stderr


def test_merge_reports_change_and_pin_handling(tmp_path):
    f = tmp_path / "settings.json"
    f.write_text(json.dumps({"env": {"FOO": "bar"}}))
    assert merge("install", f).stdout.startswith("changed")
    mtime = f.stat().st_mtime_ns
    assert merge("install", f).stdout.startswith("unchanged")
    assert f.stat().st_mtime_ns == mtime  # not rewritten

    def pin(value):
        return subprocess.run([str(MERGE), "install", str(f)], capture_output=True, text=True,
                              env={**os.environ, "D4CC_PIN": value})

    assert pin("1").stdout.startswith("changed")
    env = json.loads(f.read_text())["env"]
    assert env == {"FOO": "bar", "DISABLE_AUTOUPDATER": "1", "D4CC_PINNED": "1"}
    assert pin("1").stdout.startswith("unchanged")
    assert pin("0").stdout.startswith("changed")
    assert json.loads(f.read_text())["env"] == {"FOO": "bar"}


def test_merge_unpin_keeps_users_own_autoupdater_setting(tmp_path):
    f = tmp_path / "settings.json"
    f.write_text(json.dumps({"env": {"DISABLE_AUTOUPDATER": "1"}}))
    subprocess.run([str(MERGE), "install", str(f)], env={**os.environ, "D4CC_PIN": "0"}, check=True,
                   capture_output=True)
    assert json.loads(f.read_text())["env"] == {"DISABLE_AUTOUPDATER": "1"}
