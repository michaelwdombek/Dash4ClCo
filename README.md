# Dash4ClCo

Dashboard for Claude Code sessions running on many workstations (tmux, VPN).
Each session reports through Claude Code hooks; the dashboard shows one live card
per session with its state (working / needs you / idle / ended), current activity,
subagents, tmux pane, model and Claude Code version.

See [docs/DESIGN.md](docs/DESIGN.md) for the full design and roadmap.

**Status:** phase 2 — hook client, live board, heartbeat and health alerts
(stuck / dead / offline), version flags. History UI, card actions, Ansible
deployment and the podman quadlet follow.

## Server (development)

```bash
python3 -m venv .venv
.venv/bin/pip install -e 'server[test]'

export D4CC_DB=./data/d4cc.db D4CC_ADMIN_TOKEN=change-me
.venv/bin/d4cc-admin add-host ws1          # prints the host token (shown once)
.venv/bin/d4cc-server --host 0.0.0.0 --port 8787
```

Open `http://<host>:8787/` and sign in with the admin token.

| Variable | Default | Meaning |
|---|---|---|
| `D4CC_DB` | `./data/d4cc.db` | SQLite database path |
| `D4CC_ADMIN_TOKEN` | – (required) | UI login token |
| `D4CC_AUTH_DISABLED` | `0` | `1` disables UI auth (local dev only) |
| `D4CC_ENDED_VISIBLE_HOURS` | `24` | how long ended sessions stay on the board |
| `D4CC_LISTEN` / `D4CC_PORT` | `0.0.0.0` / `8787` | bind address (use the VPN IP) |
| `D4CC_STUCK_AFTER_MIN` | `30` | `working` without any event for this long → **stuck** |
| `D4CC_OFFLINE_AFTER_MIN` | `15` | no heartbeat for this long → host **offline** |
| `D4CC_DEAD_AFTER_HEARTBEATS` | `2` | session process missing from this many heartbeats → **dead** |
| `D4CC_HEALTH_INTERVAL_S` | `30` | how often alerts are re-evaluated |
| `D4CC_MIN_CLAUDE_VERSION` | – | reference version for "outdated" (default: newest seen in the fleet) |

Host management: `d4cc-admin add-host | list-hosts | revoke-host | rotate-token`.

## Client (per workstation, manual install)

Requires `bash`, `curl`, `jq` (Linux, uses `/proc`).

```bash
client/install.sh --url http://<dashboard>:8787 --token <host-token>
```

This installs `d4cc-hook` and `d4cc-heartbeat` to `~/.local/bin`, writes
`~/.config/d4cc/client.env` (0600), merges the hooks into `~/.claude/settings.json`
(existing settings and hooks are kept; a backup is written once to
`settings.json.d4cc-backup`) and enables the `d4cc-heartbeat.timer` systemd user
unit (every 60 s; `loginctl enable-linger` keeps it running without a login).
`d4cc-heartbeat --print` shows what would be sent.
New Claude Code sessions report automatically. Remove with `client/install.sh --uninstall`.

Privacy: prompts, full commands and tool output are **not** sent. Cards show
short hints only (Bash description or first word, file path relative to the
project, subagent description). `D4CC_SEND_PROMPTS=1` in `client.env` opts in
to 200-character prompt excerpts.

The hook never blocks Claude Code: it sends in the background with a 3 s
timeout and always exits 0. If the dashboard is down, events are dropped.

## Health alerts

| Alert | Condition | Clears when |
|---|---|---|
| **stuck** | session `working`, no event for `D4CC_STUCK_AFTER_MIN` | any new event |
| **dead** | host heartbeats arrive, but the session's claude PID is missing from 2 in a row and no `SessionEnd` came | process reappears or session ends |
| **offline** | no heartbeat for `D4CC_OFFLINE_AFTER_MIN` (stuck/dead are suppressed meanwhile) | next heartbeat |

Hosts that never sent a heartbeat (e.g. hook-only installs) are never marked offline.

Version flags: a host is **outdated** if its installed Claude Code is older than
the reference version; a session shows **restart pending** if it still runs an
older binary than the one installed on its host.

## Tests

```bash
.venv/bin/pytest
```

`tests/fixtures/` holds real hook payloads captured from Claude Code 2.1.330.
