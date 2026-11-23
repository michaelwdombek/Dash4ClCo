# Ansible: `d4cc_client` role

Installs Claude Code and the Dash4ClCo client (hook, heartbeat timer) for one
user per host. Normally you do not use this directory directly: the dashboard's
**Deploy** page generates a ready-to-run bundle (role + playbook + inventory +
host_vars with the host token).

Tested against Debian 12 (apt) and Rocky Linux 9 (dnf) systemd containers.

## Variables

| Variable | Default | |
|---|---|---|
| `d4cc_client_url` | – (required) | dashboard URL as reachable from the host |
| `d4cc_client_token` | `""` | host token; required on first install, empty keeps the existing `client.env` |
| `d4cc_client_user` | `ansible_user` | account that runs Claude Code |
| `d4cc_client_claude_install` | `true` | install / update Claude Code |
| `d4cc_client_claude_version` | `latest` | `latest`, `stable` or exact `x.y.z` (pins: auto-updater disabled via `DISABLE_AUTOUPDATER` in the user's Claude settings) |
| `d4cc_client_claude_update` | `true` | with latest/stable: run `claude update` on existing installs |
| `d4cc_client_send_prompts` | `false` | send 200-char prompt excerpts |
| `d4cc_client_enable_linger` | `true` | `loginctl enable-linger` so the heartbeat runs without login |
| `d4cc_client_send_test_heartbeat` | `true` | send a heartbeat at the end of the play |

Switching from a pinned version back to `latest` re-enables auto-updates (only
if d4cc set the pin) and updates Claude Code.

## Notes

- Requires privilege escalation (packages, linger); user files are written as
  `d4cc_client_user` (`pipelining = true` in `ansible.cfg` avoids ACL issues).
- EL9 ships `curl-minimal`; the role requests `/usr/bin/curl` so either variant works.
- Claude Code login stays manual; the play prints a reminder when no login exists.
- `roles/d4cc_client/files/` are symlinks to `client/` in the repo; bundles
  contain the resolved files.
