"""Database backups: the ``backup`` and ``restore`` commands and the automatic
copy taken before a database upgrade.

Copies always go through SQLite's online backup API, so they're consistent
while the portal and worker are running, are checked and written 0600. Each
copy first checks there's room for it. Backups and pre-restore copies are
gzipped (``.db.gz``; ``restore`` takes both). Pre-upgrade copies stay plain
``.db``: they're restored after going back to an older release, whose
``restore`` may not read gzip.
"""

from __future__ import annotations

import errno
import gzip
import json
import logging
import os
import re
import shutil
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from .db import connect, latest_version

log = logging.getLogger(__name__)

BACKUP_PREFIX = "officesentry-"
PRE_UPGRADE_PREFIX = "pre-upgrade-"
PRE_RESTORE_PREFIX = "pre-restore-"
KEEP_PRE_UPGRADE = 3
KEEP_PRE_RESTORE = 3
COMPRESSED = ".gz"
GZIP_LEVEL = 6
# Room to leave on the disk besides the copy itself, so a backup never fills it.
SPARE_BYTES = 64 * 2**20
# Tables every Office Sentry database has had since the first migration.
CORE_TABLES = {"users", "tenants", "runs", "objects", "settings"}


class RestoreError(Exception):
    pass


def _stamp() -> str:
    return f"{datetime.now(UTC):%Y%m%d-%H%M%S}"


def _private(path: Path) -> None:
    """Create an empty file only its owner can read, so nothing written into it is ever readable by others."""
    path.unlink(missing_ok=True)
    os.close(os.open(path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600))


def database_bytes(conn: sqlite3.Connection) -> int:
    return conn.execute("PRAGMA page_count").fetchone()[0] * conn.execute("PRAGMA page_size").fetchone()[0]


def check_space(folder: Path, size: int, compress: bool = True) -> None:
    """Refuse (OSError ENOSPC, with a plain message) when ``folder``'s disk can't take a copy of a ``size``-byte
    database: the copy itself, and for a compressed backup the compressed file beside it."""
    needed = size + (size // 2 if compress else 0) + SPARE_BYTES
    free = shutil.disk_usage(folder).free
    if free < needed:
        raise OSError(
            errno.ENOSPC,
            f"a copy of the database needs about {needed // 2**20:,} MB free in {folder} "
            f"and there's {free // 2**20:,} MB",
        )


def copy_database(src: sqlite3.Connection, dest: Path) -> Path:
    """Write a consistent, checked copy of ``src`` to ``dest`` (0600), gzipped when ``dest`` ends in ``.gz``,
    via temporary files so a half-written copy never carries a backup's name."""
    dest.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    compress = dest.name.endswith(COMPRESSED)
    check_space(dest.parent, database_bytes(src), compress)
    part = dest.with_name(dest.name + ".part")
    raw = dest.with_name(dest.name + ".raw.part") if compress else part
    try:
        _private(raw)
        out = sqlite3.connect(raw)
        try:
            with out:
                src.backup(out)
            problem = out.execute("PRAGMA quick_check").fetchone()[0]
        finally:
            out.close()
        if problem != "ok":
            raise sqlite3.DatabaseError(f"the copy failed its integrity check: {problem}")
        if compress:
            _private(part)
            with (
                open(raw, "rb") as plain,
                open(part, "r+b") as packed,
                gzip.GzipFile(
                    filename=dest.name[: -len(COMPRESSED)], mode="wb", fileobj=packed, compresslevel=GZIP_LEVEL
                ) as zipped,
            ):
                shutil.copyfileobj(plain, zipped, 2**20)
            raw.unlink()
        os.chmod(part, 0o600)
        os.replace(part, dest)
    except BaseException:
        part.unlink(missing_ok=True)
        raw.unlink(missing_ok=True)
        raise
    return dest


def _taken_at(path: Path) -> str:
    """The time in a backup's name; it sorts by age, which the whole name doesn't once a pre-upgrade copy's
    database version reaches two digits (``v9-to-v10`` sorts after ``v10-to-v11``)."""
    return m[0] if (m := re.search(r"\d{8}-\d{6}", path.name)) else path.name


def backup_files(folder: Path, prefix: str) -> list[Path]:
    """Backups named ``<prefix>*.db`` or ``<prefix>*.db.gz``, oldest first (by the time in their names)."""
    return sorted([*folder.glob(f"{prefix}*.db"), *folder.glob(f"{prefix}*.db{COMPRESSED}")], key=_taken_at)


def rotate(folder: Path, prefix: str, keep: int) -> list[Path]:
    """Delete all but the newest ``keep`` backups named ``<prefix>*.db`` or ``.db.gz``. Returns the deleted files."""
    if not keep:
        return []
    old = backup_files(folder, prefix)[:-keep]
    for path in old:
        path.unlink()
    return old


def backup(db_path: Path, folder: Path, keep: int) -> tuple[Path, list[Path]]:
    """The ``backup`` command: a timestamped copy, keeping the newest ``keep``, and the activity history
    brought up to date in ``<folder>/events``."""
    src = connect(db_path)
    try:
        dest = copy_database(src, folder / f"{BACKUP_PREFIX}{_stamp()}.db{COMPRESSED}")
    finally:
        src.close()
    mirror_events(db_path.parent / EVENTS, folder / EVENTS)
    return dest, rotate(folder, BACKUP_PREFIX, keep)


# -- Activity history -----------------------------------------------------------------------------
#
# The activity history (events.py) is a folder of monthly SQLite files beside the database. A closed month
# never changes, so it isn't copied every night like the database: <backups>/events/ holds one gzipped copy
# of each file, made again only when the file changed (manifest.json remembers each file's size and time),
# and a file that retention or a tenant deletion removed loses its copy too. Restoring the database puts
# back any month file that's missing; the cursors then re-read whatever the copy is short of.

EVENTS = "events"
MANIFEST = "manifest.json"


def _stamp_of(path: Path) -> list:
    wal = path.with_name(path.name + "-wal")
    return [
        path.stat().st_size,
        path.stat().st_mtime_ns,
        wal.stat().st_size if wal.exists() else 0,
        wal.stat().st_mtime_ns if wal.exists() else 0,
    ]


def _event_files(folder: Path) -> dict[str, Path]:
    return {f"{p.parent.name}/{p.name}": p for p in sorted(folder.glob("*/*.db"))} if folder.is_dir() else {}


def mirror_events(source: Path, dest: Path) -> tuple[int, int]:
    """Bring the backup copy of the activity history up to date. Returns (files copied, copies removed)."""
    files = _event_files(source)
    if not files and not dest.exists():
        return 0, 0
    dest.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        manifest = json.loads((dest / MANIFEST).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        manifest = {}
    copied = removed = 0
    for rel, path in files.items():
        target = dest / f"{rel}{COMPRESSED}"
        if manifest.get(rel) == _stamp_of(path) and target.exists():
            continue
        src = connect(path)
        try:
            copy_database(src, target)
        finally:
            src.close()
        manifest[rel] = _stamp_of(path)  # after closing: the last connection to close may checkpoint the file
        _write_manifest(dest, manifest)
        copied += 1
    for rel in sorted(set(manifest) - set(files)):
        (dest / f"{rel}{COMPRESSED}").unlink(missing_ok=True)
        del manifest[rel]
        removed += 1
    for stray in dest.glob(f"*/*.db{COMPRESSED}"):
        if f"{stray.parent.name}/{stray.name[: -len(COMPRESSED)]}" not in files:
            stray.unlink()
    _write_manifest(dest, manifest)
    return copied, removed


def _write_manifest(dest: Path, manifest: dict) -> None:
    part = dest / f"{MANIFEST}.part"
    part.write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")
    os.replace(part, dest / MANIFEST)


def restore_events(copies: Path, folder: Path) -> int:
    """Put back each activity history file the data folder is missing from a backup copy. Returns how many."""
    restored = 0
    for packed in sorted(copies.glob(f"*/*.db{COMPRESSED}")) if copies.is_dir() else []:
        target = folder / packed.parent.name / packed.name[: -len(COMPRESSED)]
        if target.exists():
            continue
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        part = target.with_name(target.name + ".part")
        try:
            _private(part)
            with gzip.open(packed, "rb") as zipped, open(part, "r+b") as out:
                shutil.copyfileobj(zipped, out, 2**20)
            os.replace(part, target)
        except (OSError, EOFError) as exc:
            part.unlink(missing_ok=True)
            log.warning("Couldn't restore %s: %s", packed, exc)
            continue
        restored += 1
    return restored


# -- Backup status --------------------------------------------------------------
#
# Every backup job records how it went in ``<data dir>/backup-status.json`` (``/data`` in Docker), one
# entry per job, so the health check and the "backup failed" system alert (``system_health``,
# ``system_alerts``) see it whatever folder or target the copies go to. This is the format the
# deployment work (WS12) writes too: "local" is the nightly copy (the ``backup`` command), "offsite" the
# restic copy and "restore_check" the weekly restore drill. Each key is optional; absent means never
# run or not set up.
#
#   {"local": {"ok": true, "last_attempt": "2026-10-09T03:45:02Z", "last_success": "2026-10-09T03:45:02Z",
#              "detail": "officesentry-20261009-034500.db.gz, 412 MB", "error": ""},
#    "offsite": {"ok": false, "last_attempt": "2026-10-09T03:46:10Z", "last_success": "2026-10-08T03:46:01Z",
#                "detail": "", "error": "restic couldn't reach the bucket"}}
#
# ``ok`` is the last attempt's outcome; ``last_success`` is null until one works. Times are UTC ISO 8601
# with a Z. ``detail`` and ``error`` are short plain words with no secrets.

STATUS_FILE = "backup-status.json"
STATUS_KEYS = ("ok", "last_attempt", "last_success", "detail", "error")


def _raw_status(data_dir: Path) -> dict:
    try:
        raw = json.loads((Path(data_dir) / STATUS_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def read_status(data_dir: Path) -> dict[str, dict]:
    """Every job's last outcome from ``backup-status.json`` ({} when there is none or it can't be read)."""
    return {str(k): {f: v.get(f) for f in STATUS_KEYS} for k, v in _raw_status(data_dir).items() if isinstance(v, dict)}


def record_status(
    data_dir: Path,
    job: str,
    ok: bool,
    error: str | None = None,
    file: Path | None = None,
    detail: str | None = None,
    now: datetime | None = None,
) -> None:
    """Record one attempt of ``job``, keeping the other jobs' entries. ``detail`` (short plain words, no
    secrets) is stored as given; without it a successful ``file`` is described by name and size. Never
    raises: a backup's outcome matters more than its record."""
    stamp = (now or datetime.now(UTC)).strftime("%Y-%m-%dT%H:%M:%SZ")
    path = Path(data_dir) / STATUS_FILE
    try:
        status = _raw_status(data_dir)
        entry = status.get(job) if isinstance(status.get(job), dict) else {}
        entry.update(ok=bool(ok), last_attempt=stamp, error="" if ok else str(error or "")[:500])
        entry.setdefault("last_success", None)
        entry.setdefault("detail", "")
        if ok:
            entry["last_success"] = stamp
            if file is not None and detail is None:
                entry["detail"] = f"{file.name}, {max(1, round(file.stat().st_size / 2**20)):,} MB"
        if detail is not None:
            entry["detail"] = str(detail)[:300]
        status[job] = entry
        part = path.with_name(path.name + ".part")
        _private(part)
        part.write_text(json.dumps(status, indent=2), encoding="utf-8")
        os.replace(part, path)
    except OSError:
        log.warning("Could not record the backup's outcome in %s", path, exc_info=True)


def newest_backup(folder: Path) -> Path | None:
    """The newest ``backup`` copy in ``folder``, for installs whose backups ran before the status file existed."""
    try:
        found = backup_files(Path(folder), BACKUP_PREFIX)
    except OSError:
        return None
    return found[-1] if found else None


def database_file(conn: sqlite3.Connection) -> Path | None:
    """The file behind a connection, or None for an in-memory database."""
    for row in conn.execute("PRAGMA database_list"):
        if row[1] == "main":
            return Path(row[2]) if row[2] else None
    return None


def before_upgrade(db_file: Path, current: int, target: int, release: str | None = None) -> Path | None:
    """Copy the database before migrations change it, into ``<data dir>/backups``, named
    ``pre-upgrade-v<old>-to-v<new>-<time>[-from-<release>].db``: the database versions, and the Office Sentry
    release to go back to with this copy, when the database knows it (``db.stored_app_version``).

    ``db.migrate`` calls this before it takes the write lock, so the portal and worker keep working while a
    large database is copied. Two processes starting at once would both copy, so a copy of the same upgrade
    made in the last hour counts and returns None.
    """
    folder = db_file.parent / "backups"
    name = f"{PRE_UPGRADE_PREFIX}v{current}-to-v{target}-"
    recent = datetime.now(UTC).timestamp() - 3600
    if folder.is_dir() and any(p.stat().st_mtime > recent for p in backup_files(folder, name)):
        return None
    suffix = f"-from-{tag}" if release and (tag := re.sub(r"[^0-9A-Za-z.+-]", "", release)[:40]) else ""
    src = sqlite3.connect(db_file, timeout=30)
    try:
        dest = copy_database(src, folder / f"{name}{_stamp()}{suffix}.db")
    finally:
        src.close()
    rotate(folder, PRE_UPGRADE_PREFIX, KEEP_PRE_UPGRADE)
    log.info("Backed up the database to %s before upgrading it from version %s to %s", dest, current, target)
    return dest


def check_backup(path: Path, shown: Path | None = None) -> int:
    """Check that ``path`` is a sound Office Sentry database this version can open. Returns its schema version.
    Messages name ``shown``, the file the person gave, when ``path`` is that backup unpacked."""
    name = shown or path
    if not path.is_file():
        raise RestoreError(f"{name} doesn't exist.")
    try:
        conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        raise RestoreError(f"{name} can't be opened: {exc}") from exc
    try:
        result = conn.execute("PRAGMA integrity_check").fetchall()
        if [r[0] for r in result] != ["ok"]:
            raise RestoreError(f"{name} is damaged: {'; '.join(r[0] for r in result[:3])}")
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        version = conn.execute("PRAGMA user_version").fetchone()[0]
    except sqlite3.DatabaseError as exc:  # "file is not a database"
        raise RestoreError(f"{name} isn't an SQLite database: {exc}") from exc
    finally:
        conn.close()
    if version < 1 or not CORE_TABLES <= tables:
        raise RestoreError(f"{name} isn't an Office Sentry database.")
    if version > latest_version():
        raise RestoreError(
            f"{name} was made by a newer Office Sentry (database version {version}; this one "
            f"knows up to {latest_version()}). Update Office Sentry first."
        )
    return version


def _remove_stale_journal(db_path: Path) -> None:
    """With no database file, a leftover -wal or -shm belongs to a database that's gone; SQLite
    would apply it to the new one."""
    if db_path.exists():
        return
    for suffix in ("-wal", "-shm", "-journal"):
        Path(f"{db_path}{suffix}").unlink(missing_ok=True)


def _is_gzip(path: Path) -> bool:
    with open(path, "rb") as f:
        return f.read(2) == b"\x1f\x8b"


@contextmanager
def _unpacked(source: Path, folder: Path) -> Iterator[Path]:
    """``source`` itself, or for a gzipped backup a temporary uncompressed copy of it in ``folder``."""
    if not source.is_file() or not _is_gzip(source):
        yield source
        return
    folder.mkdir(parents=True, exist_ok=True)
    plain = folder / f"restore-{_stamp()}.db.part"
    try:
        _private(plain)
        with gzip.open(source, "rb") as zipped, open(plain, "r+b") as out:
            shutil.copyfileobj(zipped, out, 2**20)
    except (OSError, EOFError) as exc:  # a damaged or cut-short .gz, or a full disk
        plain.unlink(missing_ok=True)
        raise RestoreError(f"{source} couldn't be unpacked: {exc}") from exc
    try:
        yield plain
    finally:
        plain.unlink(missing_ok=True)


def restore(db_path: Path, source: Path, worker_running: bool, force: bool = False) -> dict:
    """Replace the database at ``db_path`` with the backup ``source`` (a ``.db`` or a gzipped ``.db.gz``).

    The current database is copied to ``backups/pre-restore-*.db.gz`` first. The
    copy goes through the backup API into the database itself, so its WAL and
    shared-memory files stay consistent; nothing is copied file by file.
    """
    source = Path(source)
    if db_path.exists() and source.resolve() == db_path.resolve():
        raise RestoreError("That's the live database, not a backup.")
    with _unpacked(source, db_path.parent) as plain:
        return _restore(db_path, source, plain, worker_running, force)


def _restore(db_path: Path, source: Path, plain: Path, worker_running: bool, force: bool) -> dict:
    version = check_backup(plain, shown=source)
    if worker_running and not force:
        raise RestoreError(
            "The worker is running (it checked in within the last few minutes). Stop the portal "
            "and the worker first (docker compose stop web worker), or add --force if it isn't "
            "really running."
        )
    saved = None
    if db_path.exists():
        live = connect(db_path)
        try:
            saved = copy_database(live, db_path.parent / "backups" / f"{PRE_RESTORE_PREFIX}{_stamp()}.db{COMPRESSED}")
        finally:
            live.close()
        rotate(db_path.parent / "backups", PRE_RESTORE_PREFIX, KEEP_PRE_RESTORE)
    _remove_stale_journal(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(f"{plain.resolve().as_uri()}?mode=ro", uri=True)
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
    events = restore_events(source.parent / EVENTS, db_path.parent / EVENTS)
    return {"version": version, "saved": saved, "events": events}


def inspect(source: Path) -> dict:
    """The ``check-backup`` command and the restore drill: check that a backup (``.db`` or ``.db.gz``) is a
    sound Office Sentry database without restoring it, and say what's in it. A gzipped one is unpacked next
    to itself for the check, so that folder needs room for the database."""
    source = Path(source)
    with _unpacked(source, source.parent) as plain:
        version = check_backup(plain, shown=source)
        conn = sqlite3.connect(f"{plain.resolve().as_uri()}?mode=ro", uri=True)
        try:
            tenants = conn.execute("SELECT COUNT(*) FROM tenants").fetchone()[0]
            newest = conn.execute("SELECT MAX(finished_at) FROM runs").fetchone()[0]
        finally:
            conn.close()
        size = plain.stat().st_size
    return {"version": version, "tenants": tenants, "newest_run": newest, "bytes": size}
