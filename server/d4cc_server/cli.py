"""Command line entry points: `d4cc-server` (run) and `d4cc-admin` (host management)."""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time

from . import db
from .config import Config


def serve() -> None:
    parser = argparse.ArgumentParser(prog="d4cc-server")
    parser.add_argument("--host", default=os.environ.get("D4CC_LISTEN", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("D4CC_PORT", "8787")))
    args = parser.parse_args()

    import uvicorn

    from .app import create_app

    uvicorn.run(create_app(), host=args.host, port=args.port, proxy_headers=True, log_level="info")


def _fmt(ts: float | None) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else "-"


def admin(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="d4cc-admin", description="Manage Dash4ClCo hosts")
    sub = parser.add_subparsers(dest="cmd", required=True)
    add = sub.add_parser("add-host", help="register a host and print its token")
    add.add_argument("name")
    sub.add_parser("list-hosts", help="list registered hosts")
    rev = sub.add_parser("revoke-host", help="revoke a host token")
    rev.add_argument("name")
    rot = sub.add_parser("rotate-token", help="issue a new token for a host")
    rot.add_argument("name")
    args = parser.parse_args(argv)

    conn = db.connect(Config.from_env().db_path)

    if args.cmd == "add-host":
        try:
            token = db.create_host(conn, args.name)
        except sqlite3.IntegrityError:
            sys.exit(f"host {args.name!r} already exists (use rotate-token)")
        print(token)
    elif args.cmd == "list-hosts":
        rows = conn.execute("SELECT name, created_at, revoked_at, last_seen_at, client_version FROM hosts"
                            " ORDER BY name").fetchall()
        print(f"{'NAME':24} {'CREATED':17} {'LAST SEEN':17} {'CLIENT':8} STATUS")
        for r in rows:
            status = "revoked" if r["revoked_at"] else "active"
            print(f"{r['name']:24} {_fmt(r['created_at']):17} {_fmt(r['last_seen_at']):17}"
                  f" {r['client_version'] or '-':8} {status}")
    elif args.cmd == "revoke-host":
        cur = conn.execute("UPDATE hosts SET revoked_at = ? WHERE name = ? AND revoked_at IS NULL",
                           (time.time(), args.name))
        sys.exit(0 if cur.rowcount else f"no active host {args.name!r}")
    elif args.cmd == "rotate-token":
        row = conn.execute("SELECT id FROM hosts WHERE name = ?", (args.name,)).fetchone()
        if row is None:
            sys.exit(f"no host {args.name!r}")
        print(db.rotate_token(conn, row["id"]))


if __name__ == "__main__":
    admin()
