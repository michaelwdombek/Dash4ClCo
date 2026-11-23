import io
import shutil
import subprocess
import tarfile

import pytest

from d4cc_server import deploy

from conftest import ADMIN


def login(client):
    client.post("/login", data={"token": ADMIN}, follow_redirects=False)


def untar(data: bytes) -> dict[str, tarfile.TarInfo | bytes]:
    out = {}
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        for m in tar.getmembers():
            out[m.name] = (m, tar.extractfile(m).read() if m.isfile() else None)
    return out


def create(client, **kw):
    body = {"name": "ws5", "ansible_host": "10.8.0.15", "ansible_user": "admin", "claude_user": "dev",
            "claude_version": "latest", "url": "http://10.8.0.1:8787", **kw}
    return client.post("/api/deploy/hosts", json=body)


def test_create_host_returns_bundle_with_working_token(client):
    login(client)
    r = create(client)
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/gzip"
    assert 'filename="d4cc-ws5.tar.gz"' in r.headers["content-disposition"]
    files = untar(r.content)
    names = set(files)
    for expected in ("d4cc-ws5/playbook.yml", "d4cc-ws5/ansible.cfg", "d4cc-ws5/inventory.ini",
                     "d4cc-ws5/README.md", "d4cc-ws5/host_vars/ws5.yml",
                     "d4cc-ws5/roles/d4cc_client/tasks/main.yml",
                     "d4cc-ws5/roles/d4cc_client/files/d4cc-hook",
                     "d4cc-ws5/roles/d4cc_client/files/d4cc-heartbeat.timer"):
        assert expected in names, expected
    assert all(not m.issym() for m, _ in files.values()), "symlinks must be resolved"
    hook_info, hook = files["d4cc-ws5/roles/d4cc_client/files/d4cc-hook"]
    assert hook.startswith(b"#!/usr/bin/env bash") and hook_info.mode & 0o111
    inv = files["d4cc-ws5/inventory.ini"][1].decode()
    assert "ws5 ansible_host=10.8.0.15 ansible_user=admin" in inv
    hv_info, hv = files["d4cc-ws5/host_vars/ws5.yml"]
    assert hv_info.mode == 0o600
    hv = hv.decode()
    assert 'd4cc_client_url: "http://10.8.0.1:8787"' in hv and 'd4cc_client_user: "dev"' in hv
    token = next(line.split('"')[1] for line in hv.splitlines() if line.startswith("d4cc_client_token"))
    r = client.post("/api/events", headers={"Authorization": f"Bearer {token}"},
                    json={"event": "SessionStart", "session_id": "x", "ts": 1})
    assert r.status_code == 202


def test_duplicate_and_invalid_input(client):
    login(client)
    assert create(client).status_code == 200
    assert create(client).status_code == 409
    assert create(client, name="bad name").status_code == 422
    assert create(client, name="ws6", claude_version="newest").status_code == 422
    assert create(client, name="ws6", ansible_user="Root;rm").status_code == 422
    assert create(client, name="ws6", url="ftp://x").status_code == 422
    assert create(client, name="ws6", ansible_host="a b").status_code == 422


def test_requires_admin(client):
    assert create(client).status_code == 401
    assert client.get("/api/deploy/update-bundle").status_code == 401


def test_new_bundle_rotates_token(client):
    login(client)
    files = untar(create(client).content)
    old = next(line.split('"')[1] for line in files["d4cc-ws5/host_vars/ws5.yml"][1].decode().splitlines()
               if line.startswith("d4cc_client_token"))
    host_id = client.get("/api/deploy/hosts").json()["hosts"][0]["id"]
    r = client.post(f"/api/deploy/hosts/{host_id}/bundle", json={"claude_version": "2.1.330"})
    assert r.status_code == 200
    hv = untar(r.content)["d4cc-ws5/host_vars/ws5.yml"][1].decode()
    assert 'd4cc_client_claude_version: "2.1.330"' in hv
    assert 'd4cc_client_user: "dev"' in hv  # earlier deploy settings are kept
    new = next(line.split('"')[1] for line in hv.splitlines() if line.startswith("d4cc_client_token"))
    ev = {"event": "Stop", "session_id": "x", "ts": 1}
    assert client.post("/api/events", headers={"Authorization": f"Bearer {old}"}, json=ev).status_code == 401
    assert client.post("/api/events", headers={"Authorization": f"Bearer {new}"}, json=ev).status_code == 202


def test_revoke_and_fleet_update_bundle(client, app):
    login(client)
    create(client)
    create(client, name="ws6", ansible_host="", ansible_user="", claude_user="")
    hosts = {h["name"]: h for h in client.get("/api/deploy/hosts").json()["hosts"]}
    assert client.post(f"/api/deploy/hosts/{hosts['ws6']['id']}/revoke", json={}).status_code == 200
    r = client.get("/api/deploy/update-bundle", params={"url": "http://10.8.0.1:8787"})
    assert r.status_code == 200
    files = untar(r.content)
    assert "d4cc-fleet-update/host_vars/ws5.yml" in files
    assert "d4cc-fleet-update/host_vars/ws6.yml" not in files  # revoked
    hv = files["d4cc-fleet-update/host_vars/ws5.yml"][1].decode()
    assert "d4cc_client_token" not in hv
    assert b"d4cc_" not in b"".join(v for k, (_, v) in files.items() if "host_vars" in k).replace(b"d4cc_client_", b"")


def test_spec_parsing():
    spec = deploy.DeploySpec.parse({"ansible_host": " 10.0.0.1 ", "claude_version": ""})
    assert spec.ansible_host == "10.0.0.1" and spec.claude_version == "latest"
    with pytest.raises(deploy.InvalidDeploy):
        deploy.DeploySpec.parse({"ansible_host": 5})


@pytest.mark.skipif(not shutil.which("ansible-playbook"), reason="ansible not installed")
def test_bundle_passes_ansible_syntax_check(client, tmp_path):
    login(client)
    with tarfile.open(fileobj=io.BytesIO(create(client).content), mode="r:gz") as tar:
        tar.extractall(tmp_path, filter="data")
    r = subprocess.run(["ansible-playbook", "--syntax-check", "playbook.yml"], cwd=tmp_path / "d4cc-ws5",
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr
    r = subprocess.run(["ansible-inventory", "--list"], cwd=tmp_path / "d4cc-ws5", capture_output=True, text=True,
                       timeout=120)
    assert r.returncode == 0 and '"d4cc_client_user": "dev"' in r.stdout
