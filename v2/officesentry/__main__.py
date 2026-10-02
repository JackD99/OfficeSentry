"""Command line: ``python -m officesentry <command>``."""

from __future__ import annotations

import argparse
import getpass
import logging
import sys

from werkzeug.security import generate_password_hash

from . import store
from .config import load_config
from .crypto import Vault
from .db import audit, connect, migrate, transaction, utcnow
from .mailer import ENCRYPTED_KEYS as ENCRYPTED_SETTINGS


def cmd_web(args, cfg):
    from .web import create_app

    create_app(cfg).run(host=args.host, port=args.port, debug=False)


def cmd_worker(args, cfg):
    from .worker import run_worker

    run_worker(cfg, once=args.once)


def cmd_create_admin(args, cfg):
    from .web.auth import password_problem

    conn = connect(cfg.db_path)
    migrate(conn)
    password = getpass.getpass("Password: ")
    if problem := password_problem(password, getpass.getpass("Confirm: ")):
        sys.exit(problem)
    conn.execute(
        "INSERT INTO users (username, password_hash, role, all_tenants, created_at) VALUES (?, ?, 'admin', 1, ?)",
        (args.username, generate_password_hash(password), utcnow()),
    )
    audit(conn, "cli", "user_created", detail=f"{args.username} (admin)")
    print(f"Created admin {args.username}")


def cmd_create_app(args, cfg):
    """Generate a certificate and create the multi-tenant read-only app (device sign-in)."""
    from .certs import generate_certificate
    from .entra import create_app_registration, device_code_token

    conn = connect(cfg.db_path)
    migrate(conn)
    vault = Vault(cfg.encryption_keys)
    cert = generate_certificate(f"Office Sentry - {args.name}"[:64])
    print("Sign in as an admin of YOUR (the MSP's) tenant who can create app registrations.")
    token = device_code_token(args.tenant, print)
    redirect_uri = f"{cfg.base_url}/consent/callback"
    app = create_app_registration(token, cert.cert_pem, redirect_uri, display_name=args.name)
    conn_id = store.create_connection(conn, vault, args.name, cert, client_id=app["appId"])
    audit(conn, "cli", "connection_created", detail=f"{args.name} {app['appId']}")
    print(f"Created app {app['appId']} and app connection #{conn_id}.")
    print("Next: add tenants in the portal and send each tenant admin its consent link.")


def cmd_rotate_keys(args, cfg):
    """Re-encrypt stored private keys with the first key in OFFICESENTRY_ENCRYPTION_KEYS."""
    conn = connect(cfg.db_path)
    migrate(conn)
    vault = Vault(cfg.encryption_keys)
    with transaction(conn, immediate=True):
        rows = conn.execute("SELECT id, key_enc FROM app_connections").fetchall()
        for row in rows:
            conn.execute("UPDATE app_connections SET key_enc = ? WHERE id = ?",
                         (vault.rotate(row["key_enc"]), row["id"]))
        # Mail passwords and secrets are encrypted with the same keys.
        secrets = conn.execute(
            f"SELECT key, value FROM settings WHERE key IN ({','.join('?' * len(ENCRYPTED_SETTINGS))})"
            " AND value <> ''", ENCRYPTED_SETTINGS).fetchall()
        for row in secrets:
            conn.execute("UPDATE settings SET value = ? WHERE key = ?", (vault.rotate(row["value"]), row["key"]))
    print(f"Re-encrypted {len(rows)} key(s) and {len(secrets)} mail secret(s)."
          " Old keys can now be removed from the configuration.")


def cmd_live_test(args, cfg):
    """Collect from a real test tenant with a client secret (development only)."""
    from pathlib import Path

    from .livetest import run

    sys.exit(run(cfg, args.collector or None, Path(args.out) if args.out else None))


def main(argv=None):
    parser = argparse.ArgumentParser(prog="officesentry")
    sub = parser.add_subparsers(dest="command", required=True)

    web = sub.add_parser("web", help="run the web portal (development server)")
    web.add_argument("--host", default="127.0.0.1")
    web.add_argument("--port", type=int, default=8000)
    web.set_defaults(func=cmd_web)

    worker = sub.add_parser("worker", help="run collector jobs and the nightly schedule")
    worker.add_argument("--once", action="store_true", help="drain the queue and exit")
    worker.set_defaults(func=cmd_worker)

    admin = sub.add_parser("create-admin", help="create an admin user")
    admin.add_argument("username")
    admin.set_defaults(func=cmd_create_admin)

    app = sub.add_parser("create-app", help="create the read-only multi-tenant Entra app")
    app.add_argument("--name", default="Office Sentry (read-only)")
    app.add_argument("--tenant", default="organizations", help="your tenant ID or domain")
    app.set_defaults(func=cmd_create_app)

    rotate = sub.add_parser("rotate-keys", help="re-encrypt credentials with the newest key")
    rotate.set_defaults(func=cmd_rotate_keys)

    live = sub.add_parser("live-test", help="collect from a test tenant using OFFICESENTRY_TEST_* variables")
    live.add_argument("--collector", action="append", help="only run this collector (repeatable)")
    live.add_argument("--out", help="also write XLSX exports to this folder")
    live.set_defaults(func=cmd_live_test)

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    args.func(args, load_config())


if __name__ == "__main__":
    main()
