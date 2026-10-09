"""Command line: ``python -m officesentry <command>``."""

from __future__ import annotations

import argparse
import getpass
import json
import logging
import os
import sys

from werkzeug.security import generate_password_hash

from . import alerts, mailer, store
from .config import clear_setup_token, load_config, new_secrets, setup_token, write_private
from .crypto import Vault
from .db import audit, connect, migrate, transaction, utcnow
from .text import plural


def cmd_web(args, cfg):
    from .web import create_app

    create_app(cfg).run(host=args.host, port=args.port, debug=False)


def cmd_serve(args, cfg):
    from .serve import serve

    serve(cfg, host=args.host, port=args.port, threads=args.threads)


def cmd_worker(args, cfg):
    from .worker import run_worker

    run_worker(cfg, once=args.once)


def cmd_migrate(args, cfg):
    """Apply database upgrades now. The portal and worker also do this when they start."""
    from .db import schema_version

    conn = connect(cfg.db_path)
    before = schema_version(conn)
    after = migrate(conn)
    print(f"Database schema is at version {after}" + (f" (upgraded from {before})." if after != before else "."))


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
    """Create the multi-tenant read-only app (device sign-in). The portal's App connection page does the same."""
    from . import clouds
    from .app_setup import register_app
    from .entra import SignInError, device_code_token

    conn = connect(cfg.db_path)
    migrate(conn)
    vault = Vault(cfg.encryption_keys)
    cloud = clouds.get(args.cloud)
    print(f"Sign in as an admin of YOUR (the MSP's) {cloud.label} tenant who can create app registrations.")
    token = device_code_token(args.tenant, print, cloud.key)
    redirect_uri = f"{cfg.base_url}/consent/callback"
    try:
        conn_id, app = register_app(conn, vault, token, redirect_uri, args.name, cloud.key)
    except SignInError as exc:
        sys.exit(str(exc))
    audit(conn, "cli", "connection_created", detail=f"{args.name} {app['appId']}")
    print(f"Created app {app['appId']} and app connection #{conn_id}.")
    print("Next: add tenants in the portal and send each tenant admin its consent link.")


# Every encrypted value at rest, as (table, key column, encrypted column)...
ENCRYPTED_COLUMNS = (
    ("app_connections", "id", "key_enc"),
    ("app_connections", "id", "next_key_enc"),  # a replacement certificate's key, during rollover
    ("users", "id", "totp_secret_enc"),
    ("users", "id", "totp_pending_enc"),
    ("setup_sign_ins", "id", "device_code_enc"),  # the setup wizard's device code, for up to 15 minutes
)
# ...and every encrypted key in the settings table. Encrypted names end in "_enc";
# tests/test_key_rotation.py fails if one isn't listed here.
ENCRYPTED_SETTINGS = (*mailer.ENCRYPTED_KEYS, *alerts.ENCRYPTED_KEYS)


def unregistered_encrypted(conn) -> list[str]:
    """Encrypted-looking columns and settings that rotation doesn't know about."""
    known = {(t, c) for t, _k, c in ENCRYPTED_COLUMNS}
    found = []
    for (table,) in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall():
        found += [f"{table}.{c['name']}" for c in conn.execute(f"PRAGMA table_info({table})").fetchall()
                  if c["name"].endswith("_enc") and (table, c["name"]) not in known]
    found += [f"settings {r[0]}" for r in conn.execute(
        "SELECT key FROM settings WHERE key LIKE '%\\_enc' ESCAPE '\\' AND value <> ''").fetchall()
        if r[0] not in ENCRYPTED_SETTINGS]
    return found


def reencrypt_all(conn, vault) -> int:
    """Re-encrypt every stored secret with the vault's first key. Returns how many values changed.

    Refuses (changing nothing) if the database holds an encrypted value that isn't
    registered above, rather than leaving it readable only with the old key.
    """
    count = 0
    with transaction(conn, immediate=True):
        if missing := unregistered_encrypted(conn):
            raise RuntimeError("rotate-keys doesn't know how to re-encrypt " + ", ".join(missing)
                               + "; add it to ENCRYPTED_COLUMNS or ENCRYPTED_SETTINGS. Nothing was changed.")
        for table, key, column in ENCRYPTED_COLUMNS:
            rows = conn.execute(f"SELECT {key}, {column} FROM {table} WHERE {column} IS NOT NULL AND {column} != ''")
            for row in rows.fetchall():
                conn.execute(f"UPDATE {table} SET {column} = ? WHERE {key} = ?", (vault.rotate(row[1]), row[0]))
                count += 1
        # Mail passwords and secrets, and the alert webhook, are encrypted with the same keys.
        secrets = conn.execute(
            f"SELECT key, value FROM settings WHERE key IN ({','.join('?' * len(ENCRYPTED_SETTINGS))})"
            " AND value <> ''", ENCRYPTED_SETTINGS).fetchall()
        for row in secrets:
            conn.execute("UPDATE settings SET value = ? WHERE key = ?", (vault.rotate(row["value"]), row["key"]))
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
    try:
        count = reencrypt_all(conn, Vault(keys))
    except RuntimeError as exc:
        sys.exit(str(exc))
    if args.new:
        # Only now the data is readable with the new key alone is the old one dropped.
        write_private(cfg.secrets_path, json.dumps({"secret_key": fresh["secret_key"],
                                                    "encryption_keys": fresh["encryption_keys"]}))
        conn.execute("DELETE FROM sessions")
        audit(conn, "cli", "keys_rotated", detail=f"{plural(count, 'value')} re-encrypted")
        print(f"New keys written to {cfg.secrets_path} and {plural(count, 'value')} re-encrypted. "
              "Restart the web and worker processes; everyone will need to sign in again.")
        return
    audit(conn, "cli", "keys_reencrypted", detail=plural(count, "value"))
    print(f"Re-encrypted {plural(count, 'value')}. Old keys can now be removed from the configuration.")


def cmd_healthcheck(args, cfg):
    """Exit 0 if the web portal answers, or the worker has checked in recently (for Docker)."""
    if args.service == "web":
        import urllib.request

        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{args.port}/health", timeout=5) as resp:  # noqa: S310
                sys.exit(0 if resp.status == 200 else 1)
        except OSError as exc:
            sys.exit(f"web: {exc}")
    from .worker import is_alive, last_seen

    conn = connect(cfg.db_path)
    if not is_alive(conn):
        sys.exit(f"worker: last seen {last_seen(conn) or 'never'}")


def cmd_backup(args, cfg):
    """Write a consistent copy of the database, safe while the portal is running."""
    from pathlib import Path

    from .backups import backup, record_status

    # The outcome goes in backup-status.json for the health check and the "backup failed" alert.
    try:
        dest, old = backup(cfg.db_path, Path(args.dir) if args.dir else cfg.data_dir / "backups", args.keep)
    except OSError as exc:
        record_status(cfg.data_dir, "local", False, f"No backup made: {exc.strerror or exc}.")
        sys.exit(f"No backup made: {exc.strerror or exc}.")
    except Exception as exc:  # a damaged database, say: record it, then fail loudly as before
        record_status(cfg.data_dir, "local", False, f"No backup made: {type(exc).__name__}: {exc}")
        raise
    record_status(cfg.data_dir, "local", True, file=dest)
    print(f"Saved {dest}" + (f" (removed {plural(len(old), 'older backup')})" if old else ""))
    print("Also keep a copy of secrets.json (or your OFFICESENTRY_ENCRYPTION_KEYS) somewhere safe:"
          " without it the stored certificates can't be decrypted.")


def cmd_restore(args, cfg):
    """Replace the database with a backup. Stop the portal and the worker first."""
    import sqlite3
    from pathlib import Path

    from .backups import RestoreError, restore
    from .db import latest_version
    from .worker import is_alive

    running = False
    if cfg.db_path.exists():
        conn = connect(cfg.db_path)
        try:
            running = is_alive(conn)
        except sqlite3.Error:  # an empty or unreadable database has no heartbeat
            running = False
        finally:
            conn.close()
    try:
        result = restore(cfg.db_path, Path(args.file), worker_running=running, force=args.force)
    except RestoreError as exc:
        sys.exit(f"Not restored: {exc}")
    if result["saved"]:
        print(f"The database it replaced is saved as {result['saved']}.")
    print(f"Restored {args.file} (database version {result['version']}).")
    if result["version"] < latest_version():
        print(f"It's upgraded to version {latest_version()} when the portal starts.")
    print("Put back the secrets.json (or OFFICESENTRY_ENCRYPTION_KEYS) from the same time as the backup, "
          "then start the portal and the worker.")


def cmd_compact(args, cfg):
    """Compress every snapshot stored before compression now, then shrink the database file."""
    from .packing import compact

    conn = connect(cfg.db_path)
    try:
        migrate(conn)
        compact(conn)
    except RuntimeError as exc:
        sys.exit(str(exc))
    finally:
        conn.close()


def cmd_delete_tenant(args, cfg):
    """Delete a tenant and everything stored for it: the portal's Offboard → Delete permanently."""
    from . import lifecycle

    conn = connect(cfg.db_path)
    migrate(conn)
    tenant = store.get_tenant(conn, args.tenant_id)
    if tenant is None:
        sys.exit(f"No tenant with id {args.tenant_id}. The id is the number in the tenant's address (/tenants/<id>).")
    stored = lifecycle.stored(conn, tenant["id"])
    clients = lifecycle.client_only_users(conn, tenant["id"])
    print(f"{tenant['name']} ({tenant['tenant_ref']}): {plural(stored['runs'], 'collection run')}, "
          f"{plural(stored['objects'], 'stored item')}, {plural(stored['findings'], 'finding')}, "
          f"{plural(stored['schedules'], 'emailed report')}, {plural(stored['contacts'], 'contact')}, "
          f"access for {plural(stored['access'], 'user')}.")
    if clients:
        names = ", ".join(u["username"] for u in clients)
        print(f"Client accounts with no other tenant: {names}"
              + (" (deleted too)" if args.delete_client_users else " (kept; add --delete-client-users to delete)"))
    print("This can't be undone. Back up first with: python -m officesentry backup")
    if not args.yes:
        answer = input(f"Type the tenant's name ({tenant['name']}) to delete it: ")
        if answer.strip() != tenant["name"].strip():
            sys.exit("Not deleted.")
    users = [u["id"] for u in clients] if args.delete_client_users else []
    try:
        removed = lifecycle.delete_tenant(conn, tenant, "cli", users)
    except lifecycle.DeleteError as exc:
        sys.exit(f"Nothing was deleted: {exc}")
    print(f"Deleted {tenant['name']}: {plural(removed['runs'], 'run')}, {plural(removed['objects'], 'stored item')}"
          + (f" and {plural(removed['users'], 'client account')}" if removed["users"] else "") + ".")
    print("Remember the client's side: they delete the Office Sentry enterprise app and its Global Reader role.")


def cmd_live_test(args, cfg):
    """Collect from a real test tenant with a client secret (development only)."""
    from pathlib import Path

    from .livetest import run

    sys.exit(run(cfg, args.collector or None, Path(args.out) if args.out else None,
                 Path(args.record) if args.record else None, args.connection, args.dns_over_https))


def main(argv=None):
    parser = argparse.ArgumentParser(prog="officesentry")
    sub = parser.add_subparsers(dest="command", required=True)

    web = sub.add_parser("web", help="run the web portal (development server)")
    web.add_argument("--host", default="127.0.0.1")
    web.add_argument("--port", type=int, default=8000)
    web.set_defaults(func=cmd_web)

    serve = sub.add_parser("serve", help="run the web portal on a production server (waitress on Windows, "
                                         "gunicorn elsewhere), for running without Docker")
    serve.add_argument("--host", default="127.0.0.1", help="address to listen on (default: this machine only)")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--threads", type=int, default=8, help="requests handled at once")
    serve.set_defaults(func=cmd_serve)

    worker = sub.add_parser("worker", help="run collections, the schedule, emailed reports and retention")
    worker.add_argument("--once", action="store_true", help="drain the queue and exit")
    worker.set_defaults(func=cmd_worker)

    mig = sub.add_parser("migrate", help="apply database upgrades (the portal and worker also do this on start)")
    mig.set_defaults(func=cmd_migrate)

    admin = sub.add_parser("create-admin", help="create an admin user")
    admin.add_argument("username")
    admin.set_defaults(func=cmd_create_admin)

    app = sub.add_parser("create-app", help="create the read-only multi-tenant Entra app")
    app.add_argument("--name", default="Office Sentry (read-only)")
    app.add_argument("--tenant", default="organizations", help="your tenant ID or domain")
    app.add_argument("--cloud", default="commercial", choices=["commercial", "gcc_high", "dod", "china"],
                     help="the Microsoft cloud to register the app in (each national cloud needs its own app)")
    app.set_defaults(func=cmd_create_app)

    rotate = sub.add_parser("rotate-keys", help="re-encrypt credentials with the newest key")
    rotate.add_argument("--new", action="store_true",
                        help="replace the keys in secrets.json with new ones (signs everyone out)")
    rotate.set_defaults(func=cmd_rotate_keys)

    link = sub.add_parser("setup-link", help="print the one-time link for creating the first admin")
    link.set_defaults(func=cmd_setup_link)

    health = sub.add_parser("healthcheck", help="exit 0 if the web portal or worker is healthy (for Docker)")
    health.add_argument("service", choices=["web", "worker"])
    health.add_argument("--port", type=int, default=8000)
    health.set_defaults(func=cmd_healthcheck)

    backup = sub.add_parser("backup", help="save a consistent copy of the database")
    backup.add_argument("--dir", help="folder for backups (default: <data dir>/backups)")
    backup.add_argument("--keep", type=int, default=7, help="how many backups to keep (0 = all)")
    backup.set_defaults(func=cmd_backup)

    restore = sub.add_parser("restore", help="replace the database with a backup (stop the portal and worker first)")
    restore.add_argument("file", help="the backup to restore, e.g. /data/backups/officesentry-20261005-033000.db.gz")
    restore.add_argument("--force", action="store_true", help="restore even if the worker seems to be running")
    restore.set_defaults(func=cmd_restore)

    compact = sub.add_parser("compact", help="compress old snapshot data now and shrink the database file "
                                             "(the worker compresses it gradually anyway)")
    compact.set_defaults(func=cmd_compact)

    delete = sub.add_parser("delete-tenant", help="delete a client tenant and everything stored for it")
    delete.add_argument("tenant_id", type=int, help="the number in the tenant's address, /tenants/<id>")
    delete.add_argument("--delete-client-users", action="store_true",
                        help="also delete client accounts that have no other tenant")
    delete.add_argument("--yes", action="store_true", help="don't ask for the tenant's name (for scripts)")
    delete.set_defaults(func=cmd_delete_tenant)

    live = sub.add_parser("live-test", help="collect from a test tenant using OFFICESENTRY_TEST_* variables")
    live.add_argument("--collector", action="append", help="only run this collector (repeatable)")
    live.add_argument("--out", help="also write XLSX exports to this folder")
    live.add_argument("--connection", type=int,
                      help="app connection to link the tenant to (default: the only one, if there's one)")
    live.add_argument("--record", help="save the responses, scrubbed of names, emails and IDs, as a test fixture")
    live.add_argument("--dns-over-https", action="store_true",
                      help="look DNS records up over HTTPS, where this machine's DNS is blocked")
    live.set_defaults(func=cmd_live_test)

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    args.func(args, load_config())


if __name__ == "__main__":
    main()
