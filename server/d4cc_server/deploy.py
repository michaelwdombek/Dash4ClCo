"""Deployment bundles: ready-to-run Ansible projects for one or many hosts.

A bundle contains the `d4cc_client` role (same version as this server), a
playbook, ansible.cfg, an inventory and host_vars. Single-host bundles carry the
host's freshly issued token; the fleet update bundle carries no tokens (the
role keeps the existing client.env on each host).
"""

from __future__ import annotations

import io
import json
import os
import re
import sqlite3
import tarfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from . import __version__

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$")
ADDR_RE = re.compile(r"^[A-Za-z0-9\[][A-Za-z0-9.:_\[\]-]{0,252}$")
USER_RE = re.compile(r"^[a-z_][a-z0-9_.-]{0,31}$")
VERSION_RE = re.compile(r"^(latest|stable|\d+\.\d+\.\d+)$")
URL_RE = re.compile(r"^https?://[A-Za-z0-9.\-\[\]:]+(/[A-Za-z0-9._~/-]*)?$")
SKIP = {"__pycache__", ".git", ".pytest_cache"}


class InvalidDeploy(ValueError):
    pass


@dataclass
class DeploySpec:
    ansible_host: str = ""     # SSH address; empty = inventory name
    ansible_user: str = ""     # SSH user; empty = ssh config / current user
    claude_user: str = ""      # account running Claude Code; empty = ansible_user
    claude_version: str = "latest"

    @classmethod
    def parse(cls, data: dict | None) -> "DeploySpec":
        data = data or {}

        def get(key, regex, default=""):
            value = data.get(key, default)
            if value is None:
                value = default
            if not isinstance(value, str):
                raise InvalidDeploy(f"{key} must be a string")
            value = value.strip()
            if value and not regex.match(value):
                raise InvalidDeploy(f"invalid {key}: {value!r}")
            return value

        return cls(
            ansible_host=get("ansible_host", ADDR_RE),
            ansible_user=get("ansible_user", USER_RE),
            claude_user=get("claude_user", USER_RE),
            claude_version=get("claude_version", VERSION_RE, "latest") or "latest",
        )


def validate_name(name) -> str:
    if not isinstance(name, str) or not NAME_RE.match(name.strip()):
        raise InvalidDeploy("host name: letters, digits, '.', '_' and '-' only (max 63)")
    return name.strip()


def validate_url(url) -> str:
    if not isinstance(url, str) or not URL_RE.match(url.strip()):
        raise InvalidDeploy("dashboard URL must look like http://10.8.0.1:8787")
    return url.strip().rstrip("/")


def assets_dir() -> Path:
    """Directory containing `ansible/` (repo root in development, /app in the container)."""
    env = os.environ.get("D4CC_ASSETS_DIR")
    candidates = [Path(env)] if env else []
    candidates.append(Path(__file__).resolve().parents[2])
    for c in candidates:
        if (c / "ansible" / "roles" / "d4cc_client" / "tasks" / "main.yml").is_file():
            return c
    raise InvalidDeploy("Ansible role not found; set D4CC_ASSETS_DIR to the directory containing ansible/")


def save_spec(conn: sqlite3.Connection, host_id: int, spec: DeploySpec) -> None:
    conn.execute("UPDATE hosts SET deploy_json = ? WHERE id = ?", (json.dumps(asdict(spec)), host_id))


def load_spec(row: sqlite3.Row) -> DeploySpec:
    try:
        return DeploySpec(**json.loads(row["deploy_json"])) if row["deploy_json"] else DeploySpec()
    except (TypeError, ValueError):
        return DeploySpec()


# -- bundle content -------------------------------------------------------------

def _yaml_value(value) -> str:
    return json.dumps(value)  # JSON scalars are valid YAML


def _host_vars(name: str, url: str, spec: DeploySpec, token: str | None) -> str:
    lines = [
        "---",
        f"# Dash4ClCo client settings for {name} (generated {time.strftime('%Y-%m-%d %H:%M')})",
        f"d4cc_client_url: {_yaml_value(url)}",
        f"d4cc_client_claude_version: {_yaml_value(spec.claude_version)}",
    ]
    if spec.claude_user:
        lines.append(f"d4cc_client_user: {_yaml_value(spec.claude_user)}")
    if token:
        lines += ["# Secret: encrypt with `ansible-vault encrypt` before committing this anywhere.",
                  f"d4cc_client_token: {_yaml_value(token)}"]
    else:
        lines.append("# No token: the existing ~/.config/d4cc/client.env on the host is kept.")
    return "\n".join(lines) + "\n"


def _inventory(hosts: list[tuple[str, DeploySpec]]) -> str:
    lines = ["[d4cc_clients]"]
    for name, spec in hosts:
        parts = [name]
        if spec.ansible_host:
            parts.append(f"ansible_host={spec.ansible_host}")
        if spec.ansible_user:
            parts.append(f"ansible_user={spec.ansible_user}")
        lines.append(" ".join(parts))
    return "\n".join(lines) + "\n"


def _readme(title: str, hosts: list[str], with_token: bool) -> str:
    token_note = (
        "`host_vars/` contains the host token. Keep this bundle private, or encrypt it:\n\n"
        "    ansible-vault encrypt host_vars/*.yml\n\n"
        "and add `--ask-vault-pass` to the command below. The token is shown only in this\n"
        "bundle; issuing a new bundle from the dashboard invalidates it.\n"
        if with_token else
        "This bundle contains no tokens. It updates hosts that already have the client\n"
        "configured (`~/.config/d4cc/client.env` is kept).\n"
    )
    return f"""# Dash4ClCo client deployment: {title}

Generated by Dash4ClCo {__version__} on {time.strftime('%Y-%m-%d %H:%M')}.
Hosts: {', '.join(hosts)}

## What it does

For the configured user on each host:

- installs `curl`, `jq`, `tmux`, `procps` (apt or dnf)
- installs Claude Code with the official installer (version from `host_vars/`,
  `latest` by default; an exact version pins it and disables auto-updates) or
  updates an existing installation
- installs the Dash4ClCo hook + heartbeat to `~/.local/bin`, writes
  `~/.config/d4cc/client.env` and merges the hooks into `~/.claude/settings.json`
  (existing settings are kept)
- enables the `d4cc-heartbeat.timer` systemd user unit (+ `loginctl enable-linger`)
- sends a first heartbeat so the host appears on the dashboard

## Token

{token_note}
## Run

Requirements: Ansible >= 2.15 on your machine, SSH access and sudo on the host.

    ansible-playbook playbook.yml --ask-become-pass

Check `inventory.ini` first (SSH address and user).

## After the run

Claude Code login is interactive: log in once on the host as the configured user
(`claude`, then follow the login). The playbook prints a reminder if no login exists.
"""


def build_bundle(title: str, hosts: list[tuple[str, DeploySpec]], url: str, tokens: dict[str, str]) -> bytes:
    root = assets_dir() / "ansible"
    top = f"d4cc-{title}"
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz", dereference=True) as tar:

        def add_text(path: str, text: str, mode: int = 0o644) -> None:
            data = text.encode()
            info = tarfile.TarInfo(f"{top}/{path}")
            info.size, info.mode, info.mtime = len(data), mode, int(time.time())
            tar.addfile(info, io.BytesIO(data))

        for path in sorted((root / "roles").rglob("*")):
            if path.is_file() and not SKIP.intersection(path.parts):
                tar.add(path, arcname=f"{top}/{path.relative_to(root)}", recursive=False)
        tar.add(root / "playbook.yml", arcname=f"{top}/playbook.yml")
        tar.add(root / "ansible.cfg", arcname=f"{top}/ansible.cfg")
        add_text("inventory.ini", _inventory(hosts))
        for name, spec in hosts:
            token = tokens.get(name)
            add_text(f"host_vars/{name}.yml", _host_vars(name, url, spec, token), 0o600 if token else 0o644)
        add_text("README.md", _readme(title, [n for n, _ in hosts], bool(tokens)))
    return buf.getvalue()
