"""Command line: ``python -m officesentry <command>``."""

from __future__ import annotations

import argparse
import getpass
import json
import logging
import os
import sys

from werkzeug.security import generate_password_hash

from . import store
from .config import clear_setup_token, load_config, new_secrets, setup_token, write_private
from .crypto import Vault
from .db import audit, connect, migrate, transaction, utcnow


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
    clear_setup_token(cfg.data_dir)
    print(f"Created admin {args.username}")


def cmd_setup_link(args, cfg):
    """Print the one-time link for creating the first admin in the browser."""
    conn = connect(cfg.db_path)
    migrate(conn)
    if conn.execute("SELECT 1 FROM users LIMIT 1").fetchone():
        sys.exit("An admin already exists, so first-run setup is closed. Sign in instead.")
    print(f"{cfg.base_url}/setup?token={setup_token(cfg.data_dir)}")


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


# Every encrypted value at rest, as (table, key column, encrypted column).
ENCRYPTED_COLUMNS = (
    ("app_connections", "id", "key_enc"),
    ("users", "id", "totp_secret_enc"),
    ("users", "id", "totp_pending_enc"),
)


def reencrypt_all(conn, vault) -> int:
    """Re-encrypt every stored secret with the vault's first key. Returns how many values changed."""
    count = 0
    with transaction(conn, immediate=True):
        for table, key, column in ENCRYPTED_COLUMNS:
            rows = conn.execute(f"SELECT {key}, {column} FROM {table} WHERE {column} IS NOT NULL AND {column} != ''")
            for row in rows.fetchall():
                conn.execute(f"UPDATE {table} SET {column} = ? WHERE {key} = ?", (vault.rotate(row[1]), row[0]))
                count += 1
        smtp = conn.execute("SELECT value FROM settings WHERE key = 'smtp_password_enc'").fetchone()
        if smtp and smtp["value"]:
            conn.execute("UPDATE settings SET value = ? WHERE key = 'smtp_password_enc'",
                         (vault.rotate(smtp["value"]),))
            count += 1
    return count


def cmd_rotate_keys(args, cfg):
    """Re-encrypt stored secrets with the newest encryption key.

    With --new and keys kept in secrets.json: make a new session key and a new
    encryption key, re-encrypt everything, then drop the old encryption key.
    Everyone has to sign in again.
    """
    conn = connect(cfg.db_path)
    migrate(conn)
    keys = list(cfg.encryption_keys)
    if args.new:
        from_env = bool(os.environ.get("OFFICESENTRY_SECRET_KEY") or os.environ.get("OFFICESENTRY_ENCRYPTION_KEYS"))
        if from_env:
            sys.exit("Your keys come from environment variables, so change them there: put a new key first in "
                     "OFFICESENTRY_ENCRYPTION_KEYS (keep the old one after it), run rotate-keys, then remove the "
                     "old key. Set a new OFFICESENTRY_SECRET_KEY to sign everyone out.")
        fresh = new_secrets()
        keys = fresh["encryption_keys"] + keys
    count = reencrypt_all(conn, Vault(keys))
    if args.new:
        # Only now the data is readable with the new key alone is the old one dropped.
        write_private(cfg.secrets_path, json.dumps({"secret_key": fresh["secret_key"],
                                                    "encryption_keys": fresh["encryption_keys"]}))
        conn.execute("DELETE FROM sessions")
        audit(conn, "cli", "keys_rotated", detail=f"{count} value(s) re-encrypted")
        print(f"New keys written to {cfg.secrets_path} and {count} value(s) re-encrypted. "
              "Restart the web and worker processes; everyone will need to sign in again.")
        return
    audit(conn, "cli", "keys_reencrypted", detail=f"{count} value(s)")
    print(f"Re-encrypted {count} value(s). Old keys can now be removed from the configuration.")


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
    rotate.add_argument("--new", action="store_true",
                        help="replace the keys in secrets.json with new ones (signs everyone out)")
    rotate.set_defaults(func=cmd_rotate_keys)

    link = sub.add_parser("setup-link", help="print the one-time link for creating the first admin")
    link.set_defaults(func=cmd_setup_link)

    live = sub.add_parser("live-test", help="collect from a test tenant using OFFICESENTRY_TEST_* variables")
    live.add_argument("--collector", action="append", help="only run this collector (repeatable)")
    live.add_argument("--out", help="also write XLSX exports to this folder")
    live.set_defaults(func=cmd_live_test)

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    args.func(args, load_config())


if __name__ == "__main__":
    main()
