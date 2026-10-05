"""SQLite access and schema migrations.

Migrations are the numbered ``migrations/NNNN_*.sql`` files. Each one is applied
inside a single explicit transaction together with the ``user_version`` bump,
so a failure leaves the database at the previous version.
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def utcnow() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def connect(db_path: Path | str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
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


def migrate(conn: sqlite3.Connection) -> int:
    """Apply pending migrations. Returns the resulting schema version.

    Safe to call from several processes at once (gunicorn workers, the
    worker): each migration re-reads ``user_version`` inside its write
    transaction, so one another process already applied is skipped.
    """
    _use_wal(conn)
    current = schema_version(conn)
    for version, path in migration_files():
        if version <= current:
            continue
        with transaction(conn, immediate=True):
            current = schema_version(conn)
            if version <= current:
                continue  # applied by another process since we looked
            for stmt in _split_statements(path.read_text(encoding="utf-8")):
                conn.execute(stmt)
            conn.execute(f"PRAGMA user_version = {int(version)}")
        current = version
    return current


def audit(conn: sqlite3.Connection, username: str | None, action: str,
          tenant_id: int | None = None, detail: str = "") -> None:
    conn.execute(
        "INSERT INTO audit_log (ts, username, action, tenant_id, detail) VALUES (?, ?, ?, ?, ?)",
        (utcnow(), username, action, tenant_id, detail),
    )
