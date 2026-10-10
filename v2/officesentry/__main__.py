"""Command line: ``python -m officesentry <command>``."""

from __future__ import annotations

import argparse
import getpass
import json
import logging
import os
import sys

from werkzeug.security import generate_password_hash

from . import alerts, logs, mailer, store, totp
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

    logs.setup(cfg.data_dir, "worker")
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
ENCRYPTED_SETTINGS = (*mailer.ENCRYPTED_KEYS, *alerts.ENCRYPTED_KEYS, *totp.ENCRYPTED_KEYS)


def unregistered_encrypted(conn) -> list[str]:
    """Encrypted-looking columns and settings that rotation doesn't know about."""
    known = {(t, c) for t, _k, c in ENCRYPTED_COLUMNS}
    found = []
    for (table,) in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall():
        found += [
            f"{table}.{c['name']}"
            for c in conn.execute(f"PRAGMA table_info({table})").fetchall()
            if c["name"].endswith("_enc") and (table, c["name"]) not in known
        ]
    found += [
        f"settings {r[0]}"
        for r in conn.execute(
            "SELECT key FROM settings WHERE key LIKE '%\\_enc' ESCAPE '\\' AND value <> ''"
        ).fetchall()
        if r[0] not in ENCRYPTED_SETTINGS
    ]
    return found


def reencrypt_all(conn, vault) -> int:
    """Re-encrypt every stored secret with the vault's first key. Returns how many values changed.

    Refuses (changing nothing) if the database holds an encrypted value that isn't
    registered above, rather than leaving it readable only with the old key.
    """
    count = 0
    with transaction(conn, immediate=True):
        if missing := unregistered_encrypted(conn):
            raise RuntimeError(
                "rotate-keys doesn't know how to re-encrypt "
                + ", ".join(missing)
                + "; add it to ENCRYPTED_COLUMNS or ENCRYPTED_SETTINGS. Nothing was changed."
            )
        for table, key, column in ENCRYPTED_COLUMNS:
            rows = conn.execute(f"SELECT {key}, {column} FROM {table} WHERE {column} IS NOT NULL AND {column} != ''")
            for row in rows.fetchall():
                conn.execute(f"UPDATE {table} SET {column} = ? WHERE {key} = ?", (vault.rotate(row[1]), row[0]))
                count += 1
        # Mail passwords and secrets, and the alert webhook, are encrypted with the same keys.
        secrets = conn.execute(
            f"SELECT key, value FROM settings WHERE key IN ({','.join('?' * len(ENCRYPTED_SETTINGS))}) AND value <> ''",
            ENCRYPTED_SETTINGS,
        ).fetchall()
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
            sys.exit(
                "Your keys come from environment variables, so change them there: put a new key first in "
                "OFFICESENTRY_ENCRYPTION_KEYS (keep the old one after it), run rotate-keys, then remove the "
                "old key. Set a new OFFICESENTRY_SECRET_KEY to sign everyone out."
            )
        fresh = new_secrets()
        keys = fresh["encryption_keys"] + keys
        # Saved next to the old keys before anything is re-encrypted, so a crash or a failed write part way
        # never leaves data under a key that only existed in memory.
        write_private(cfg.secrets_path, json.dumps({"secret_key": cfg.secret_key, "encryption_keys": keys}))
    try:
        count = reencrypt_all(conn, Vault(keys))
    except RuntimeError as exc:
        sys.exit(str(exc))
    if args.new:
        # Only now the data is readable with the new key alone is the old one dropped.
        write_private(
            cfg.secrets_path,
            json.dumps({"secret_key": fresh["secret_key"], "encryption_keys": fresh["encryption_keys"]}),
        )
        conn.execute("DELETE FROM sessions")
        audit(conn, "cli", "keys_rotated", detail=f"{plural(count, 'value')} re-encrypted")
        print(
            f"New keys written to {cfg.secrets_path} and {plural(count, 'value')} re-encrypted. "
            "Restart the web and worker processes; everyone will need to sign in again."
        )
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

    from .backups import BackupIncomplete, backup, record_status

    # The outcome goes in backup-status.json for the health check and the "backup failed" alert.
    try:
        dest, old = backup(cfg.db_path, Path(args.dir) if args.dir else cfg.data_dir / "backups", args.keep)
    except BackupIncomplete as exc:
        # The database copy is sound, but the backup as a whole isn't: the alert should say exactly that.
        error = f"The database was copied to {exc.dest.name}, but {exc}."
        record_status(cfg.data_dir, "local", False, error)
        sys.exit(error)
    except OSError as exc:
        record_status(cfg.data_dir, "local", False, f"No backup made: {exc.strerror or exc}.")
        sys.exit(f"No backup made: {exc.strerror or exc}.")
    except Exception as exc:  # a damaged database, say: record it, then fail loudly as before
        record_status(cfg.data_dir, "local", False, f"No backup made: {type(exc).__name__}: {exc}")
        raise
    record_status(cfg.data_dir, "local", True, file=dest)
    print(f"Saved {dest}" + (f" (removed {plural(len(old), 'older backup')})" if old else ""))
    print(
        "Also keep a copy of secrets.json (or your OFFICESENTRY_ENCRYPTION_KEYS) somewhere safe:"
        " without it the stored certificates can't be decrypted."
    )


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
    if result.get("damaged"):
        print(
            f"The database it replaced was damaged, so it couldn't be copied: it was moved aside whole as "
            f"{result['saved']}."
        )
    elif result["saved"]:
        print(f"The database it replaced is saved as {result['saved']}.")
    print(f"Restored {args.file} (database version {result['version']}).")
    if result.get("events"):
        print(f"Put back {plural(result['events'], 'missing activity history file')} from the backup folder.")
    if result["version"] < latest_version():
        print(f"It's upgraded to version {latest_version()} when the portal starts.")
    print(
        "Put back the secrets.json (or OFFICESENTRY_ENCRYPTION_KEYS) from the same time as the backup, "
        "then start the portal and the worker."
    )


def cmd_backup_status(args, cfg):
    """Show what each backup job last did, or record a job's result (deploy/jobs.sh does, for the off-site
    copy)."""
    from .backups import read_status, record_status

    if args.record:
        if args.ok + args.failed + args.off != 1:
            sys.exit("Say --ok, --failed or --off with --record.")
        record_status(cfg.data_dir, args.record, args.ok, args.error or None, detail=args.detail or None, off=args.off)
        return
    jobs = read_status(cfg.data_dir)
    if args.json:
        print(json.dumps(jobs, indent=2))
        return
    if not jobs:
        print("No backup has been recorded yet.")
    for name, job in sorted(jobs.items()):
        label = {"local": "Database copy", "offsite": "Off-site copy", "restore_check": "Restore check"}.get(name, name)
        if job.get("off"):
            print(f"{label}: not set up, so skipped at {job.get('last_attempt')}")
        elif job.get("ok"):
            print(f"{label}: OK at {job.get('last_attempt')}" + (f" ({job['detail']})" if job.get("detail") else ""))
        else:
            print(
                f"{label}: FAILED at {job.get('last_attempt')} "
                f"(last worked {job.get('last_success') or 'never'}): {job.get('error') or 'no details'}"
            )


def cmd_check_backup(args, cfg):
    """Check a backup is sound and say what's in it, without restoring it: the restore drill's last step."""
    import sqlite3
    from pathlib import Path

    from .backups import RestoreError, inspect, record_status

    source = Path(args.file)
    try:
        found = inspect(source)
    except (RestoreError, OSError, sqlite3.Error) as exc:
        if args.record:
            record_status(cfg.data_dir, "restore_check", False, f"{source.name}: {exc}")
        sys.exit(f"Check failed: {exc}")
    newest = (found["newest_run"] or "none")[:16].replace("T", " ")
    summary = f"database version {found['version']}, {plural(found['tenants'], 'tenant')}, newest collection {newest}"
    if args.record:
        record_status(cfg.data_dir, "restore_check", True, detail=f"{source.name}: {summary}")
    print(
        f"OK: {source.name} opens and passes SQLite's integrity check; {summary}, "
        f"{found['bytes'] / 2**20:,.1f} MB unpacked."
    )


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


def _size(n: float) -> str:
    return f"{n / 2**30:,.2f} GB" if n >= 2**30 else f"{n / 2**20:,.1f} MB"


def _duration(seconds: float) -> str:
    if seconds < 90:
        return f"{max(seconds, 1):.0f} seconds"
    if seconds < 90 * 60:
        return f"{seconds / 60:.0f} minutes"
    return f"{seconds / 3600:.1f} hours"


def cmd_history(args, cfg):
    """The daily history (state_history.py): progress, a dry run of the backfill, the backfill now, or a check."""
    import shutil
    import time

    from . import state_history

    conn = connect(cfg.db_path)
    try:
        migrate(conn)
        if args.dry_run:
            est = state_history.estimate(conn, seconds=args.seconds)
            if not est.pending:
                print("Nothing to record: every run with a snapshot is in the daily history.")
                return
            free = shutil.disk_usage(cfg.db_path.parent).free
            print(
                f"Dry run, nothing written. {plural(est.pending, 'run')} ({est.pending_objects:,} objects) to "
                f"record; sampled {plural(est.sampled_sources, 'data source')} ({est.share:.0%} of the objects) "
                f"in {_duration(est.seconds)}."
            )
            print(f"  Room needed: about {_size(est.total_bytes)}; {_size(free)} free next to the database.")
            print(
                f"  Time: about {_duration(est.total_seconds)} with `history --backfill`, or about "
                f"{_duration(est.total_seconds * 60 / state_history.SECONDS_PER_TICK)} in the worker's background "
                f"({state_history.SECONDS_PER_TICK:.0f} seconds a minute)."
            )
            if est.total_bytes * 2 > free:
                print(
                    "  There isn't twice that room free: free some disk space first. The worker pauses the "
                    "backfill while the disk is over 85% full."
                )
            return
        if args.backfill:
            if not state_history.room(conn):
                sys.exit("The disk is over 85% full, so the daily history wasn't backfilled. Free some space first.")
            started = time.monotonic()

            def progress(n: int) -> None:
                print(f"  {n:,} runs recorded ({_duration(time.monotonic() - started)})", flush=True)

            done, finished = state_history.catch_up(conn, seconds=None, report=progress)
            print(
                f"Recorded {plural(done, 'run')} in {_duration(time.monotonic() - started)}."
                + ("" if finished else " Some runs are still queued or running; the worker records them.")
            )
        if args.verify:
            checked = bad = 0
            for (run_id,) in conn.execute("SELECT run_id FROM state_runs ORDER BY run_id").fetchall():
                differ = state_history.verify_run(conn, run_id)
                if differ is None:
                    continue
                checked += 1
                if differ:
                    bad += 1
                    print(f"  run {run_id}: {', '.join(differ)} differ from the snapshot")
            print(
                f"Checked {plural(checked, 'run')} that still have their snapshot: "
                + (f"{plural(bad, 'run')} differ." if bad else "every one reads back exactly.")
            )
            if bad:
                sys.exit(1)
            return
        st = state_history.status(conn)
        print(
            f"Daily history: {plural(st['runs'], 'run')} recorded ({st['objects']:,} objects as {st['versions']:,} "
            f"versions), {_size(st['bytes'])}; the snapshots take {_size(st['snapshot_bytes'])}."
        )
        if st["oldest"]:
            print(f"  Reaches back to {st['oldest'][:10]}.")
        print(
            f"  {plural(st['pending'], 'run')} still to record"
            + (" (the worker records them a few seconds a minute)." if st["pending"] else ".")
        )
        if st["mismatches"]:
            print(f"  {plural(st['mismatches'], 'run')} didn't read back as stored; last: {st['last_mismatch']}.")
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
    print(
        f"{tenant['name']} ({tenant['tenant_ref']}): {plural(stored['runs'], 'collection run')}, "
        f"{plural(stored['objects'], 'stored item')}, {plural(stored['findings'], 'finding')}, "
        f"{plural(stored['schedules'], 'emailed report')}, {plural(stored['contacts'], 'contact')}, "
        f"access for {plural(stored['access'], 'user')}."
    )
    if clients:
        names = ", ".join(u["username"] for u in clients)
        print(
            f"Client accounts with no other tenant: {names}"
            + (" (deleted too)" if args.delete_client_users else " (kept; add --delete-client-users to delete)")
        )
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
    print(
        f"Deleted {tenant['name']}: {plural(removed['runs'], 'run')}, {plural(removed['objects'], 'stored item')}"
        + (f" and {plural(removed['users'], 'client account')}" if removed["users"] else "")
        + "."
    )
    print("Remember the client's side: they delete the Office Sentry enterprise app and its Global Reader role.")


def cmd_support_package(args, cfg, config_error: str | None = None):
    """Write the support package (support.py): a zip of versions, settings, health, failures and logs with
    secrets and client data taken out, to attach to a bug report. Works when the portal won't start: a
    database that won't open or a configuration that won't load leaves a note in the zip, not an error."""
    from pathlib import Path

    from . import support

    conn = None
    if cfg is not None:
        try:
            conn = connect(cfg.db_path)
            conn.execute("SELECT 1 FROM sqlite_master").fetchone()
        except Exception as exc:  # noqa: BLE001 (a broken database is exactly when this command is needed)
            print(f"The database couldn't be opened ({type(exc).__name__}); the package says so.", file=sys.stderr)
            conn = None
    pkg = support.build(conn, cfg, include_identifiers=args.include_identifiers, config_error=config_error)
    if conn is not None:
        conn.close()
    out = None if args.out == "-" else Path(args.out or pkg.filename)
    written = support.write(pkg, out)
    say = sys.stderr if out is None else sys.stdout
    print(f"Wrote {written or 'the package to standard output'} ({plural(len(pkg.files), 'file')}).", file=say)
    if pkg.include_identifiers:
        print("It includes client names, tenant IDs and domains: share it through a private channel only.", file=say)
    else:
        print("Clients appear as numbers; secrets, addresses and names were taken out.", file=say)
    for part in pkg.problems:
        print(f"  {part.name}: {part.note}", file=say)
    print("Attach it to a bug report: https://github.com/JackD99/OfficeSentry/issues/new?template=bug.yml", file=say)


def cmd_live_test(args, cfg):
    """Collect from a real test tenant with a client secret (development only)."""
    from pathlib import Path

    from .livetest import run

    sys.exit(
        run(
            cfg,
            args.collector or None,
            Path(args.out) if args.out else None,
            Path(args.record) if args.record else None,
            args.connection,
            args.dns_over_https,
            Path(args.canary) if args.canary else None,
        )
    )


def main(argv=None):
    parser = argparse.ArgumentParser(prog="officesentry")
    sub = parser.add_subparsers(dest="command", required=True)

    web = sub.add_parser("web", help="run the web portal (development server)")
    web.add_argument("--host", default="127.0.0.1")
    web.add_argument("--port", type=int, default=8000)
    web.set_defaults(func=cmd_web)

    serve = sub.add_parser(
        "serve",
        help="run the web portal on a production server (waitress on Windows, "
        "gunicorn elsewhere), for running without Docker",
    )
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
    app.add_argument(
        "--cloud",
        default="commercial",
        choices=["commercial", "gcc_high", "dod", "china"],
        help="the Microsoft cloud to register the app in (each national cloud needs its own app)",
    )
    app.set_defaults(func=cmd_create_app)

    rotate = sub.add_parser("rotate-keys", help="re-encrypt credentials with the newest key")
    rotate.add_argument(
        "--new", action="store_true", help="replace the keys in secrets.json with new ones (signs everyone out)"
    )
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

    status = sub.add_parser("backup-status", help="show what each backup job last did")
    status.add_argument("--json", action="store_true", help="print backup-status.json")
    status.add_argument(
        "--record", choices=["offsite", "restore_check"], help="record a backup job's result (for deploy/jobs.sh)"
    )
    status.add_argument("--ok", action="store_true")
    status.add_argument("--failed", action="store_true")
    status.add_argument("--off", action="store_true", help="the job isn't set up, so it skipped")
    status.add_argument("--detail", default="", help="what it did, in a few words")
    status.add_argument("--error", default="", help="why it failed, in a few words")
    status.set_defaults(func=cmd_backup_status)

    check = sub.add_parser("check-backup", help="check a backup is sound and say what's in it, without restoring it")
    check.add_argument("file", help="the backup to check (.db or .db.gz)")
    check.add_argument("--record", action="store_true", help="record the result as the restore check")
    check.set_defaults(func=cmd_check_backup)

    restore = sub.add_parser("restore", help="replace the database with a backup (stop the portal and worker first)")
    restore.add_argument("file", help="the backup to restore, e.g. /data/backups/officesentry-20261005-033000.db.gz")
    restore.add_argument("--force", action="store_true", help="restore even if the worker seems to be running")
    restore.set_defaults(func=cmd_restore)

    compact = sub.add_parser(
        "compact",
        help="compress old snapshot data now and shrink the database file (the worker compresses it gradually anyway)",
    )
    compact.set_defaults(func=cmd_compact)

    history = sub.add_parser(
        "history", help="show how far the daily history has been recorded (the worker records it in the background)"
    )
    what = history.add_mutually_exclusive_group()
    what.add_argument(
        "--dry-run", action="store_true", help="estimate the room and time recording the rest needs, writing nothing"
    )
    what.add_argument(
        "--backfill", action="store_true", help="record everything left now rather than in the background"
    )
    what.add_argument(
        "--verify",
        action="store_true",
        help="check every recorded run that still has its snapshot reads back exactly as stored",
    )
    history.add_argument("--seconds", type=float, default=20, help="with --dry-run: how long to sample for")
    history.set_defaults(func=cmd_history)

    delete = sub.add_parser("delete-tenant", help="delete a client tenant and everything stored for it")
    delete.add_argument("tenant_id", type=int, help="the number in the tenant's address, /tenants/<id>")
    delete.add_argument(
        "--delete-client-users", action="store_true", help="also delete client accounts that have no other tenant"
    )
    delete.add_argument("--yes", action="store_true", help="don't ask for the tenant's name (for scripts)")
    delete.set_defaults(func=cmd_delete_tenant)

    pkg = sub.add_parser(
        "support-package",
        help="write a zip of versions, settings, health checks, recent failures and logs, with secrets and client "
        "data taken out, to attach to a bug report",
    )
    pkg.add_argument(
        "--out", help="the file to write (default: officesentry-support-<date>.zip here; - for standard output)"
    )
    pkg.add_argument(
        "--include-identifiers",
        action="store_true",
        help="keep client names, Microsoft tenant IDs, domains and user names (for a private channel only)",
    )
    pkg.set_defaults(func=cmd_support_package)

    live = sub.add_parser("live-test", help="collect from a test tenant using OFFICESENTRY_TEST_* variables")
    live.add_argument("--collector", action="append", help="only run this collector (repeatable)")
    live.add_argument("--out", help="also write XLSX exports to this folder")
    live.add_argument(
        "--connection", type=int, help="app connection to link the tenant to (default: the only one, if there's one)"
    )
    live.add_argument("--record", help="save the responses, scrubbed of names, emails and IDs, as a test fixture")
    live.add_argument(
        "--canary",
        metavar="REPORT",
        help="compare the responses' shape with tests/fixtures/live_tenant.json and write a report "
        "(Markdown) here; exits 1 if Microsoft changed something",
    )
    live.add_argument(
        "--dns-over-https",
        action="store_true",
        help="look DNS records up over HTTPS, where this machine's DNS is blocked",
    )
    live.set_defaults(func=cmd_live_test)

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if args.command == "support-package":
        # The one command that must work when the configuration doesn't load: the package then says why.
        try:
            cfg = load_config()
        except ValueError as exc:
            print(f"The configuration didn't load ({exc}); the package says so.", file=sys.stderr)
            cmd_support_package(args, None, config_error=str(exc))
            return
        cmd_support_package(args, cfg)
        return
    args.func(args, load_config())


if __name__ == "__main__":
    main()
