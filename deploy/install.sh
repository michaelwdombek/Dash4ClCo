#!/usr/bin/env bash
# Build the Dash4ClCo image and install it as a podman quadlet.
#
#   deploy/install.sh [--user|--system] [--publish ADDR[:PORT]] [--public-url URL]
#
# --publish  host address (and port, default 8787) to bind, e.g. the VPN IP
#
# --user (default when not root): rootless, files under ~/.config, enables linger
# --system (default as root):     rootful, files under /etc
# Re-running rebuilds the image and restarts the service; the env file and the
# data volume are kept.
set -euo pipefail

repo=$(cd "$(dirname "$0")/.." && pwd)
mode=$([ "$(id -u)" = 0 ] && echo system || echo user)
publish="" public_url=""
while [ $# -gt 0 ]; do
  case "$1" in
    --user) mode=user ;;
    --system) mode=system ;;
    --publish) publish=$2; shift ;;
    --public-url) public_url=$2; shift ;;
    -h|--help) sed -n '2,13p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
  shift
done

if [ "$mode" = system ]; then
  [ "$(id -u)" = 0 ] || { echo "--system needs root" >&2; exit 1; }
  unit_dir=/etc/containers/systemd conf_dir=/etc/dash4clco sc=(systemctl)
else
  [ "$(id -u)" != 0 ] || { echo "--user should not run as root" >&2; exit 1; }
  unit_dir=${XDG_CONFIG_HOME:-$HOME/.config}/containers/systemd
  conf_dir=${XDG_CONFIG_HOME:-$HOME/.config}/dash4clco sc=(systemctl --user)
fi

echo "==> building localhost/dash4clco:latest"
version=$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$repo/server/d4cc_server/__init__.py")
podman build -f "$repo/deploy/Containerfile" -t localhost/dash4clco:latest \
  -t "localhost/dash4clco:$version" "$repo"

mkdir -p "$unit_dir" "$conf_dir"
env_file=$conf_dir/dash4clco.env
if [ ! -e "$env_file" ]; then
  token=$(head -c 32 /dev/urandom | base64 | tr -d '/+=' | head -c 40)
  (umask 077; sed "s|^D4CC_ADMIN_TOKEN=.*|D4CC_ADMIN_TOKEN=$token|" "$repo/deploy/dash4clco.env.example" >"$env_file")
  [ -n "$public_url" ] && sed -i "s|^#D4CC_PUBLIC_URL=.*|D4CC_PUBLIC_URL=$public_url|" "$env_file"
  echo "==> created $env_file"
  echo "    admin token: $token"
else
  echo "==> keeping existing $env_file"
fi

unit=$unit_dir/dash4clco.container
cp "$repo/deploy/dash4clco.container" "$unit.new"
if [ -n "$publish" ]; then
  case "$publish" in *:*) ;; *) publish=$publish:8787 ;; esac
  sed -i "s|^PublishPort=.*|PublishPort=$publish:8787|" "$unit.new"
elif [ -e "$unit" ]; then
  # keep the address chosen on an earlier install
  old=$(sed -n 's/^PublishPort=//p' "$unit")
  [ -n "$old" ] && sed -i "s|^PublishPort=.*|PublishPort=$old|" "$unit.new"
fi
mv "$unit.new" "$unit"
echo "==> installed $unit ($(sed -n 's/^PublishPort=//p' "$unit"))"

[ "$mode" = user ] && loginctl enable-linger "$USER" 2>/dev/null || true
"${sc[@]}" daemon-reload
"${sc[@]}" restart dash4clco.service
sleep 3
"${sc[@]}" --no-pager --lines=5 status dash4clco.service || true
echo
echo "Host management: podman exec dash4clco d4cc-admin list-hosts"
