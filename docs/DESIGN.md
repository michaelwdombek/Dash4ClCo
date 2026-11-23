# Dash4ClCo — Design

A single-user dashboard that shows the live state of every Claude Code session
running on a fleet of workstations (tmux, reachable over VPN), keeps a history,
flags sessions/hosts that need investigation, and generates Ansible bundles to
onboard new machines.

## Goals

- One card per Claude Code session: host, tmux pane, project, state, current tool.
- Subagents shown as child rows on their parent card.
- Detect stuck sessions, dead sessions and offline hosts.
- Per-session history timeline.
- Card interactions: "working on it" marker, acknowledge/resolve alerts with a note, archive.
- Deployment section: generate a ready-to-run Ansible bundle for a new machine.

## Non-goals

- Multi-user / RBAC (single user; the UI is protected by one admin token on the VPN).
- External alerting (Slack, mail, push). Alerts live in the dashboard only.
- Controlling Claude sessions remotely (read-only observation + local annotations).
- Automating Claude login (interactive, done by the user on each machine).

## Architecture

```
 workstation (Debian / RHEL-family)                       dashboard host
┌──────────────────────────────────────┐               ┌──────────────────────────┐
│ claude (tmux pane)                   │               │ podman quadlet           │
│   └─ hooks ─► d4cc-hook  ────────────┼── HTTPS/VPN ─►│  FastAPI + SQLite        │
│                                      │  POST /api/   │   /api/events            │
│ systemd --user timer (60s)           │  Bearer token │   /api/heartbeat         │
│   └─ d4cc-heartbeat ─────────────────┼──────────────►│   / (UI, SSE live feed)  │
└──────────────────────────────────────┘               └──────────────────────────┘
```

Repo layout:

```
server/     FastAPI app, SQLite schema, health evaluator, UI templates, bundle generator
client/     d4cc-hook, d4cc-heartbeat, settings merge helper, systemd units
ansible/    role `d4cc_client` (+ installs Claude Code) used by the deployment bundles
deploy/     Containerfile + quadlet units for the dashboard itself
tests/      fixtures (real hook payloads), server + client tests
```

## Hook payloads (verified against Claude Code 2.1.330)

Captured in `tests/fixtures/hook-payloads-2.1.330.jsonl`. Every payload carries
`session_id`, `cwd`, `hook_event_name`; most carry `prompt_id`, `permission_mode`.

| Event | Relevant fields | Dashboard meaning |
|---|---|---|
| `SessionStart` | `source` (startup/resume/clear/compact) | create / revive card |
| `UserPromptSubmit` | `prompt` | → working. **Also fires for subagent hand-backs** (`prompt` starts with `<agent-message`), so prompt text is not necessarily user input |
| `PreToolUse` | `tool_name`, `tool_input`, `tool_use_id`, optional `agent_id`/`agent_type` | current activity; if `agent_id` set → activity of that subagent |
| `PostToolUse` | as above + `duration_ms` | activity done |
| `SubagentStart` | `agent_id`, `agent_type` | add child row |
| `SubagentStop` | `agent_id`, `agent_type` | child row done |
| `Notification` | `notification_type` / `message` (**not yet captured** — does not fire in headless mode; verify in an interactive tmux session in phase 1) | permission prompt → **needs you** |
| `Stop` | `background_tasks`, `session_crons` | → idle (or "idle, background tasks running" if non-empty) |
| `SessionEnd` | `reason` | → ended |

Notes:
- `TMUX_PANE` is inherited by hook processes when Claude runs inside tmux.
- The hook payload does not include Claude's PID; the client walks up its process
  tree to find the `claude` process and reports its PID (used for liveness checks).
- Payload fields change between versions; the client forwards a whitelisted subset
  and the server tolerates unknown/missing fields.

## Client (per workstation)

### `d4cc-hook <event>`
- Reads hook JSON from stdin, builds a compact event:
  `host, session_id, event, ts, cwd, tmux_pane, tmux_session, claude_pid,
  source/reason, tool_name, tool_summary, tool_use_id, agent_id, agent_type,
  notification_type, background_task_count, claude_version`.
- `claude_version` = version of the **running** process: resolved from
  `/proc/<claude_pid>/exe` (native installer → `.../claude/versions/<ver>`);
  fallback `claude --version` (npm installs). Cached per PID so it costs nothing
  per event.
- **Privacy:** never sends full `prompt`, `tool_input` or `tool_response`.
  `tool_summary` is a short, truncated hint (Bash `description`, file path for
  Read/Edit/Write, subagent `description`). Prompt excerpts are opt-in
  (`D4CC_SEND_PROMPTS=1`, default off).
- POSTs with `curl -m 2`, always exits 0, never blocks Claude. Dashboard down =
  event lost; the heartbeat reconciles state.
- Config: `~/.config/d4cc/client.env` (`D4CC_URL`, `D4CC_TOKEN`, `D4CC_HOST` override).
- Dependencies: `bash`, `curl`, `jq`.

### `d4cc-heartbeat` (systemd user timer, every 60 s)
- Sends: host, client version, OS family, uptime, **installed Claude Code version**
  (`claude --version`), and the list of live Claude processes (PID, tmux pane,
  cwd, running version) from `ps` + `tmux list-panes -a`.
- Lets the server tell "idle but alive" from "died without SessionEnd" from "host offline".

### Settings integration
- Hooks are **merged** into `~/.claude/settings.json` (existing hooks/settings kept;
  our entries identified by the `d4cc-hook` command and replaced on update).
- Merge done by a small `jq`-based helper, idempotent, used by Ansible and manual install.

## Server

- Python 3.12+, FastAPI + uvicorn, SQLite (WAL), Jinja2 templates, small vanilla JS,
  live updates via Server-Sent Events. No frontend build step.
- Auth:
  - Ingest (`/api/events`, `/api/heartbeat`): per-host bearer token. Unknown or
    revoked token → 401. Host identity comes from the token, not the payload.
  - UI: single admin token (login form → cookie). Intended to listen on VPN only.

### Data model (SQLite)

- `hosts` — id, name, token_hash, created_at, revoked_at, last_heartbeat_at,
  client_version, claude_installed_version, os_family, last_heartbeat (JSON)
- `sessions` — id (session_id), host_id, tmux_pane, cwd, claude_pid, claude_version, started_at,
  last_event_at, state, current_activity, ended_at, end_reason, replaced_by,
  archived_at
- `subagents` — agent_id, session_id, agent_type, description, started_at, ended_at
- `events` — id, session_id, host_id, ts, type, data (JSON) — the history
- `annotations` — id, session_id or host_id, kind (working_on / ack / resolve / note),
  text, created_at
- `alerts` — id, subject (session|host), kind (stuck / dead / offline), opened_at,
  acked_at, resolved_at

Retention: events older than `D4CC_RETENTION_DAYS` (default 30) are pruned;
archived sessions too.

### Session state machine

```
           SessionStart
               │
               ▼
   ┌──────── idle ◄──────── Stop ─────────┐
   │           │                           │
   │    UserPromptSubmit / PreToolUse      │
   │           ▼                           │
   │        working ──── Notification ──► needs_you
   │           │        (permission)       │
   │           └──── PreToolUse ◄──────────┘ (after approval)
   │
   └── SessionEnd ──► ended
```

- A new `SessionStart` on the same host + tmux pane marks the previous session
  `ended (replaced)` (covers `/clear`).
- `resume` with a known `session_id` revives the existing card.

### Health evaluator (runs every 30 s; all thresholds configurable)

| Condition | Result | Default |
|---|---|---|
| state `working` and no event for > `D4CC_STUCK_AFTER` | alert **stuck** | 30 min |
| host heartbeat OK, but session's `claude_pid` not in live list, no `SessionEnd` | alert **dead** (investigate) | 2 heartbeats |
| no heartbeat for > `D4CC_OFFLINE_AFTER` | alert **offline** on host; its cards shown as "unknown" | 15 min |
| `needs_you` for > `D4CC_NEEDS_YOU_HIGHLIGHT` | escalate highlight | 10 min |

### Version tracking

- Every card shows the Claude Code version that session runs; every host shows
  the installed version.
- Reference version = newest version seen anywhere in the fleet (no external
  calls), optionally overridden by `D4CC_MIN_CLAUDE_VERSION`.
- Flags (badge, no alert): host **outdated** (installed < reference), session
  **restart pending** (running < installed on its host, i.e. updated on disk
  but the session still runs the old binary).
- Hosts page has a fleet version overview (version → hosts / sessions).

Offline default (15 min) intentionally exceeds the ~10 min VPN gateway restart.
Alerts auto-resolve when the condition clears (e.g. host comes back), unless
acknowledged with a note, which is kept in history.

### UI

- **Board:** cards grouped by host; filter by host / state / "needs attention";
  attention cards sorted first. Card shows state badge, project (cwd basename),
  pane, current activity, duration in state, subagent rows, markers.
- **Card actions:** mark "working on it" (+ note), acknowledge / resolve alert,
  archive.
- **Session detail:** timeline of events and annotations.
- **Hosts:** heartbeat status, client version, OS family, token revoke.
- **Deployment:** see below.
- **Settings:** thresholds, retention (stored in DB, env vars as defaults).

## Deployment section

1. "Add machine" → enter hostname (+ optional ansible_host / SSH user).
2. Server creates a host record with a fresh per-host token (shown once).
3. Download `d4cc-<host>.tar.gz` containing:
   - `inventory.ini` (prefilled), `playbook.yml`, `group_vars` with `d4cc_url`
     and the host's token (vault-encrypt recommended, documented),
   - the `d4cc_client` role (same version as the server), `README.md`.
4. Role `d4cc_client` (supports apt **and** dnf families):
   - packages: `curl`, `jq`, `tmux`, `procps`
   - installs Claude Code via the official native installer for the target user:
     latest by default; `d4cc_claude_version` (also a field in the "Add machine"
     form) pins a specific version. Re-running with a newer/other version
     updates an existing install.
   - installs `d4cc-hook`, `d4cc-heartbeat`, `client.env` (0600)
   - merges hooks into `~/.claude/settings.json`
   - enables the heartbeat systemd user timer (+ `loginctl enable-linger`)
   - sends a test event → host appears on the dashboard
   - prints the remaining manual step: run `claude` once and log in.
5. The same role is used to update existing machines (re-run with a new bundle
   or with a token supplied via vars).

## Dashboard deployment (podman quadlet)

- `deploy/Containerfile` (python slim, non-root).
- `deploy/dash4clco.container` quadlet: volume for `/data` (SQLite), env file for
  admin token + thresholds, `PublishPort` bound to the VPN IP, auto-update label,
  healthcheck on `/healthz`.
- Rootless (`~/.config/containers/systemd/`) or rootful (`/etc/containers/systemd/`).
- TLS: not required — plain HTTP bound to the VPN interface. A reverse proxy
  with TLS can be put in front optionally (documented, not built in).

## Build phases

1. **Core:** client hook + server ingest, sessions/subagents, board with live updates.
2. **Health:** heartbeat client + timer, health evaluator, alerts.
3. **History & interactions:** timeline, annotations, archive, settings page.
4. **Deployment:** host/token management, Ansible role (apt + dnf), bundle generator.
5. **Packaging:** Containerfile, quadlet, docs; end-to-end test in a container.

## Open points

- Verify `Notification` payload (permission prompt) in an interactive tmux session.
