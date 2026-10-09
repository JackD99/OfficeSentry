"""SQLite access and schema migrations.

Migrations are the numbered ``migrations/NNNN_*.sql`` files. Each one is applied
inside a single explicit transaction together with the ``user_version`` bump,
so a failure leaves the database at the previous version.
"""

from __future__ import annotations

import re
import sqlite3
import time
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def utcnow() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def connect(db_path: Path | str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    # Zero deleted content when it costs no extra I/O (pruned snapshots, removed people);
    # tenant deletion turns it fully on (lifecycle.delete_tenant).
    conn.execute("PRAGMA secure_delete = FAST")
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection, immediate: bool = False) -> Iterator[sqlite3.Connection]:
    conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def _split_statements(sql: str) -> list[str]:
    statements, buf = [], ""
    for line in sql.splitlines(keepends=True):
        buf += line
        if sqlite3.complete_statement(buf):
            if buf.strip():
                statements.append(buf.strip())
            buf = ""
    if buf.strip() and not all(
        ln.strip().startswith("--") or not ln.strip() for ln in buf.splitlines()
    ):
        raise ValueError(f"incomplete SQL statement in migration: {buf[:80]!r}")
    return statements


def migration_files() -> list[tuple[int, Path]]:
    files = []
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        files.append((int(path.name.split("_", 1)[0]), path))
    # migrate() skips any file numbered at or below the database's version, so the second of two files with
    # one number would never run on a database the first reached. Refuse instead (test_migrations.py).
    if clashes := sorted(v for v, n in Counter(v for v, _ in files).items() if n > 1):
        names = ", ".join(path.name for v, path in files if v == clashes[0])
        raise RuntimeError(f"Two database upgrades share the number {clashes[0]:04d} ({names}). Renumber the "
                           f"newer one to the next free number above {files[-1][0]:04d}.")
    return files


def _use_wal(conn: sqlite3.Connection) -> None:
    """Switch to WAL once. Changing the journal mode doesn't wait on the busy
    timeout, so when another process (a second gunicorn worker) is doing the
    same at that moment, retry for a while rather than fail."""
    deadline = time.monotonic() + 30
    while True:
        try:
            if conn.execute("PRAGMA journal_mode").fetchone()[0].lower() != "wal":
                conn.execute("PRAGMA journal_mode = WAL")
            return
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc) or time.monotonic() > deadline:
                raise
            time.sleep(0.05)


def schema_version(conn: sqlite3.Connection) -> int:
    return conn.execute("PRAGMA user_version").fetchone()[0]


def latest_version() -> int:
    files = migration_files()
    return files[-1][0] if files else 0


def _backup_before_upgrade(conn: sqlite3.Connection, current: int) -> None:
    """Copy a database that already has a schema before upgrading it (``backups/pre-upgrade-*.db``),
    so a bad release can be rolled back. A failed copy stops the upgrade."""
    from . import backups

    db_file = backups.database_file(conn)
    if db_file is None:
        return
    try:
        backups.before_upgrade(db_file, current, latest_version())
    except (OSError, sqlite3.Error) as exc:
        raise RuntimeError(f"Couldn't back up the database before upgrading it, so it wasn't upgraded: {exc}. "
                           "Free some disk space (or fix the backups folder's permissions) and start again.") from exc


class DatabaseTooNew(RuntimeError):
    """The database was upgraded by a newer Office Sentry than this one (usually after going back a version)."""


def _too_new_message(conn: sqlite3.Connection, current: int, latest: int) -> str:
    """Say how to go back: restore the newest ``pre-upgrade-*`` copy this version can open."""
    from . import backups

    db_file = backups.database_file(conn)
    folder = db_file.parent / "backups" if db_file else None
    fits = []
    for path in folder.glob(f"{backups.PRE_UPGRADE_PREFIX}v*-to-v*.db") if folder and folder.is_dir() else ():
        if (m := re.fullmatch(rf"{backups.PRE_UPGRADE_PREFIX}v(\d+)-to-v\d+-(.+)\.db", path.name)) \
                and int(m[1]) <= latest:
            fits.append((m[2], path))
    if fits:
        how = f"run `python -m officesentry restore {max(fits)[1]}`"
    elif folder:
        how = (f"restore the {backups.PRE_UPGRADE_PREFIX}*.db copy from {folder} (or another backup from before "
               "the upgrade) with `python -m officesentry restore <file>`")
    else:
        how = (f"restore the {backups.PRE_UPGRADE_PREFIX}*.db copy taken before the upgrade with "
               "`python -m officesentry restore <file>`")
    return (f"This database was upgraded by a newer version of Office Sentry (database version {current}; this "
            f"version knows up to {latest}), so this version won't start on it. Either go back to the newer "
            f"version, or to stay on this one: stop the portal and the worker, {how}, then start them again. "
            "Anything collected since the upgrade is lost. DEPLOY.md, \"Rolling back an update\", has the steps.")


def migrate(conn: sqlite3.Connection) -> int:
    """Apply pending migrations. Returns the resulting schema version.

    A database that already has a schema is copied to ``backups/`` next to it
    first (see ``backups.before_upgrade``). A database a newer release upgraded
    is refused (``DatabaseTooNew``) rather than run with code that doesn't know
    its schema.

    Safe to call from several processes at once (gunicorn workers, the
    worker): each migration re-reads ``user_version`` inside its write
    transaction, so one another process already applied is skipped.
    """
    current = schema_version(conn)
    if current > latest_version():
        raise DatabaseTooNew(_too_new_message(conn, current, latest_version()))
    _use_wal(conn)
    backed_up = current == 0  # a new database has nothing to lose
    for version, path in migration_files():
        if version <= current:
            continue
        with transaction(conn, immediate=True):
            current = schema_version(conn)
            if version <= current:
                continue  # applied by another process since we looked
            if not backed_up:
                _backup_before_upgrade(conn, current)
                backed_up = True
            for stmt in _split_statements(path.read_text(encoding="utf-8")):
                conn.execute(stmt)
            conn.execute(f"PRAGMA user_version = {int(version)}")
        current = version
    return current


def _request_ip() -> str | None:
    """The portal visitor's address when called during a web request, else None (CLI, worker)."""
    try:
        from flask import has_request_context, request
    except ImportError:  # pragma: no cover
        return None
    if not has_request_context():
        return None
    return (request.remote_addr or "")[:64] or None


def audit(conn: sqlite3.Connection, username: str | None, action: str,
          tenant_id: int | None = None, detail: str = "") -> None:
    conn.execute(
        "INSERT INTO audit_log (ts, username, action, tenant_id, detail, ip) VALUES (?, ?, ?, ?, ?, ?)",
        (utcnow(), username, action, tenant_id, detail, _request_ip()),
    )


# Sign-in noise: kept for AUDIT_NOISE_DAYS, then pruned by the worker. Everything else is kept.
AUDIT_NOISE = ("login_failed", "login_failed_unknown", "login_throttled", "login_code_failed",
               "two_step_setup_failed", "consent_link_refused")
AUDIT_NOISE_DAYS = 90
COUNTED_WINDOW_MINUTES = 60


def audit_counted(conn: sqlite3.Connection, action: str, ip: str, username: str | None = None) -> None:
    """Record repeated, anonymous events (failed sign-ins with unknown names, throttled attempts) as one row per
    address, name and hour with a count, so they can't flood the log or store what strangers typed."""
    since = (datetime.now(UTC) - timedelta(minutes=COUNTED_WINDOW_MINUTES)).strftime("%Y-%m-%dT%H:%M:%SZ")
    row = conn.execute(
        "SELECT id, detail FROM audit_log WHERE action = ? AND ip = ? AND username IS ? AND ts >= ?"
        " ORDER BY id DESC LIMIT 1", (action, ip, username, since)).fetchone()
    if row is None:
        conn.execute("INSERT INTO audit_log (ts, username, action, detail, ip) VALUES (?, ?, ?, '1 attempt', ?)",
                      (utcnow(), username, action, ip))
        return
    count = int((row["detail"] or "1").split(" ", 1)[0] or 1) + 1
    conn.execute("UPDATE audit_log SET detail = ? WHERE id = ?", (f"{count} attempts", row["id"]))


def prune_audit(conn: sqlite3.Connection, now: datetime | None = None) -> int:
    """Drop sign-in noise older than AUDIT_NOISE_DAYS. Who changed what is kept for good."""
    cutoff = ((now or datetime.now(UTC)) - timedelta(days=AUDIT_NOISE_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return conn.execute(f"DELETE FROM audit_log WHERE ts < ? AND action IN ({','.join('?' * len(AUDIT_NOISE))})",
                        (cutoff, *AUDIT_NOISE)).rowcount
