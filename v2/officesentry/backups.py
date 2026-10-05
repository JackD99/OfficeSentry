"""Database backups: the ``backup`` and ``restore`` commands and the automatic
copy taken before a database upgrade.

Copies always go through SQLite's online backup API, so they're consistent
while the portal and worker are running, and are written 0600.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from .db import connect, latest_version

log = logging.getLogger(__name__)

BACKUP_PREFIX = "officesentry-"
PRE_UPGRADE_PREFIX = "pre-upgrade-"
PRE_RESTORE_PREFIX = "pre-restore-"
KEEP_PRE_UPGRADE = 3
KEEP_PRE_RESTORE = 3
# Tables every Office Sentry database has had since the first migration.
CORE_TABLES = {"users", "tenants", "runs", "objects", "settings"}


class RestoreError(Exception):
    pass


def _stamp() -> str:
    return f"{datetime.now(UTC):%Y%m%d-%H%M%S}"


def copy_database(src: sqlite3.Connection, dest: Path) -> Path:
    """Write a consistent copy of ``src`` to ``dest`` (0600), via a temporary file so a
    half-written copy never carries a backup's name."""
    dest.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    part.unlink(missing_ok=True)
    out = sqlite3.connect(part)
    try:
        with out:
            src.backup(out)
        problem = out.execute("PRAGMA quick_check").fetchone()[0]
    finally:
        out.close()
    if problem != "ok":
        part.unlink(missing_ok=True)
        raise sqlite3.DatabaseError(f"the copy failed its integrity check: {problem}")
    os.chmod(part, 0o600)
    os.replace(part, dest)
    return dest


def rotate(folder: Path, prefix: str, keep: int) -> list[Path]:
    """Delete all but the newest ``keep`` backups named ``<prefix>*.db``. Returns the deleted files."""
    if not keep:
        return []
    old = sorted(folder.glob(f"{prefix}*.db"))[:-keep]
    for path in old:
        path.unlink()
    return old


def backup(db_path: Path, folder: Path, keep: int) -> tuple[Path, list[Path]]:
    """The ``backup`` command: a timestamped copy, keeping the newest ``keep``."""
    src = connect(db_path)
    try:
        dest = copy_database(src, folder / f"{BACKUP_PREFIX}{_stamp()}.db")
    finally:
        src.close()
    return dest, rotate(folder, BACKUP_PREFIX, keep)


def database_file(conn: sqlite3.Connection) -> Path | None:
    """The file behind a connection, or None for an in-memory database."""
    for row in conn.execute("PRAGMA database_list"):
        if row[1] == "main":
            return Path(row[2]) if row[2] else None
    return None


def before_upgrade(db_file: Path, current: int, target: int) -> Path:
    """Copy the database before migrations change it, into ``<data dir>/backups``.

    Called by ``db.migrate`` while it holds the write lock, so it reads through a
    separate connection (WAL lets it see the last committed state).
    """
    folder = db_file.parent / "backups"
    src = sqlite3.connect(db_file, timeout=30)
    try:
        dest = copy_database(src, folder / f"{PRE_UPGRADE_PREFIX}v{current}-to-v{target}-{_stamp()}.db")
    finally:
        src.close()
    rotate(folder, PRE_UPGRADE_PREFIX, KEEP_PRE_UPGRADE)
    log.info("Backed up the database to %s before upgrading it from version %s to %s", dest, current, target)
    return dest


def check_backup(path: Path) -> int:
    """Check that ``path`` is a sound Office Sentry database this version can open. Returns its schema version."""
    if not path.is_file():
        raise RestoreError(f"{path} doesn't exist.")
    try:
        conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        raise RestoreError(f"{path} can't be opened: {exc}") from exc
    try:
        result = conn.execute("PRAGMA integrity_check").fetchall()
        if [r[0] for r in result] != ["ok"]:
            raise RestoreError(f"{path} is damaged: {'; '.join(r[0] for r in result[:3])}")
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        version = conn.execute("PRAGMA user_version").fetchone()[0]
    except sqlite3.DatabaseError as exc:  # "file is not a database"
        raise RestoreError(f"{path} isn't an SQLite database: {exc}") from exc
    finally:
        conn.close()
    if version < 1 or not CORE_TABLES <= tables:
        raise RestoreError(f"{path} isn't an Office Sentry database.")
    if version > latest_version():
        raise RestoreError(f"{path} was made by a newer Office Sentry (database version {version}; this one "
                           f"knows up to {latest_version()}). Update Office Sentry first.")
    return version


def _remove_stale_journal(db_path: Path) -> None:
    """With no database file, a leftover -wal or -shm belongs to a database that's gone; SQLite
    would apply it to the new one."""
    if db_path.exists():
        return
    for suffix in ("-wal", "-shm", "-journal"):
        Path(f"{db_path}{suffix}").unlink(missing_ok=True)


def restore(db_path: Path, source: Path, worker_running: bool, force: bool = False) -> dict:
    """Replace the database at ``db_path`` with the backup ``source``.

    The current database is copied to ``backups/pre-restore-*.db`` first. The
    copy goes through the backup API into the database itself, so its WAL and
    shared-memory files stay consistent; nothing is copied file by file.
    """
    source = Path(source)
    if db_path.exists() and source.resolve() == db_path.resolve():
        raise RestoreError("That's the live database, not a backup.")
    version = check_backup(source)
    if worker_running and not force:
        raise RestoreError("The worker is running (it checked in within the last few minutes). Stop the portal "
                           "and the worker first (docker compose stop web worker), or add --force if it isn't "
                           "really running.")
    saved = None
    if db_path.exists():
        live = connect(db_path)
        try:
            saved = copy_database(live, db_path.parent / "backups" / f"{PRE_RESTORE_PREFIX}{_stamp()}.db")
        finally:
            live.close()
        rotate(db_path.parent / "backups", PRE_RESTORE_PREFIX, KEEP_PRE_RESTORE)
    _remove_stale_journal(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(f"{source.resolve().as_uri()}?mode=ro", uri=True)
    dest = connect(db_path)
    try:
        src.backup(dest)
        dest.execute("PRAGMA journal_mode = WAL")
        dest.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        problem = dest.execute("PRAGMA quick_check").fetchone()[0]
    finally:
        src.close()
        dest.close()
    if problem != "ok":
        raise RestoreError(f"The restored database failed its check ({problem}). The previous one is in {saved}.")
    return {"version": version, "saved": saved}
