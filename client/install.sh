#!/usr/bin/env bash
# Manual client install for the current user (Ansible does the same in phase 4).
#   ./install.sh --url http://DASHBOARD:8787 --token d4cc_xxx
#   ./install.sh --uninstall
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
bin="$HOME/.local/bin"
confdir="$HOME/.config/d4cc"
url="" token="" uninstall=0

while [ $# -gt 0 ]; do
  case "$1" in
    --url) url="$2"; shift 2 ;;
    --token) token="$2"; shift 2 ;;
    --uninstall) uninstall=1; shift ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

for dep in jq curl; do
  command -v "$dep" >/dev/null || { echo "missing dependency: $dep" >&2; exit 1; }
done

if [ "$uninstall" = 1 ]; then
  "$here/d4cc-settings-merge" uninstall
  rm -f "$bin/d4cc-hook" "$bin/d4cc-settings-merge"
  echo "Removed d4cc client (config kept in $confdir)."
  exit 0
fi

[ -n "$url" ] && [ -n "$token" ] || { echo "usage: $0 --url URL --token TOKEN" >&2; exit 2; }

mkdir -p "$bin" "$confdir"
install -m 0755 "$here/d4cc-hook" "$bin/d4cc-hook"
install -m 0755 "$here/d4cc-settings-merge" "$bin/d4cc-settings-merge"
umask 077
printf 'D4CC_URL=%q\nD4CC_TOKEN=%q\n' "$url" "$token" >"$confdir/client.env"
chmod 0600 "$confdir/client.env"
"$bin/d4cc-settings-merge" install "$HOME/.claude/settings.json" "$bin/d4cc-hook"

if curl -fsS -m 5 "${url%/}/healthz" >/dev/null; then
  echo "Dashboard reachable at $url"
else
  echo "WARNING: dashboard not reachable at $url (events are dropped until it is)" >&2
fi
echo "Done. New Claude Code sessions will report to the dashboard."
