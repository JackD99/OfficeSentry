"""Packs snapshot rows stored before compression (one object of JSON text per row) into the compressed packs
``store.add_objects`` writes now, a little at a time.

The worker runs ``pack_some`` every minute for a few seconds, oldest runs first, one run and kind per short
transaction, so a large database converts over a few hours without holding up the portal or collections.
Progress is kept in ``settings`` (``PROGRESS_KEY``, the last run ID done). Readers take both formats, so
nothing waits for it. ``python -m officesentry compact`` finishes the job in one go and gives the freed space
back to the disk.
"""

from __future__ import annotations

import logging
import shutil
import sqlite3
import time
from collections.abc import Callable

from . import schedule, store
from .db import transaction
from .text import plural

log = logging.getLogger(__name__)

PROGRESS_KEY = "objects_packed_to"
SECONDS_PER_TICK = 5.0
RUNS_PER_QUERY = 50


def _progress(conn) -> int:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (PROGRESS_KEY,)).fetchone()
    return int(row["value"]) if row else 0


def pack_run(conn, run_id: int) -> int:
    """Pack a run's unpacked rows, one kind per transaction. Returns how many objects were packed."""
    kinds = [r["kind"] for r in conn.execute(
        "SELECT DISTINCT kind FROM objects WHERE run_id = ? AND typeof(data) = 'text'", (run_id,))]
    packed = 0
    for kind in kinds:
        with transaction(conn, immediate=True):
            lines = store.stored_lines(conn, run_id, kind)  # read again inside the lock: retention may have pruned
            if lines:
                store.write_packs(conn, run_id, kind, lines)
                packed += len(lines)
    return packed


def pack_some(conn, seconds: float | None = SECONDS_PER_TICK,
              clock: Callable[[], float] = time.monotonic) -> tuple[int, bool]:
    """Pack old rows, oldest run first, for about ``seconds`` (None: until done).
    Returns (objects packed, whether every run is now packed)."""
    stop = None if seconds is None else clock() + seconds
    done_to, packed = _progress(conn), 0
    try:
        while True:
            runs = [r[0] for r in conn.execute("SELECT DISTINCT run_id FROM objects WHERE run_id > ?"
                                               " ORDER BY run_id LIMIT ?", (done_to, RUNS_PER_QUERY))]
            if not runs:
                return packed, True
            for run_id in runs:
                if stop is not None and clock() >= stop:
                    return packed, False
                packed += pack_run(conn, run_id)
                done_to = run_id
    finally:
        conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (PROGRESS_KEY, str(done_to)))


@schedule.periodic("pack_snapshots")
def _pack_job(ctx) -> None:
    packed, _ = pack_some(ctx.conn)
    if packed:
        log.info("Compressed %s stored before compression", plural(packed, "snapshot object"))


def unpacked_objects(conn) -> int:
    """How many objects are still stored one per row."""
    return conn.execute("SELECT COUNT(*) FROM objects WHERE typeof(data) = 'text'").fetchone()[0]


def compact(conn, report: Callable[[str], None] = print) -> None:
    """The ``compact`` command: pack every old row now, then VACUUM so the database file shrinks.

    VACUUM builds a new copy of the database next to it (not in /tmp, which is memory in Docker) and writes it
    back through the WAL, so it needs about twice the packed size in free disk, and holds the write lock while
    it runs (collections and edits wait). Run it at a quiet time.
    """
    from .backups import database_file

    packed, _ = pack_some(conn, seconds=None)
    report(f"Compressed {plural(packed, 'snapshot object')}.")
    db_file = database_file(conn)
    if db_file is None:
        return
    pages = conn.execute("PRAGMA page_count").fetchone()[0] - conn.execute("PRAGMA freelist_count").fetchone()[0]
    needed = 2 * pages * conn.execute("PRAGMA page_size").fetchone()[0]
    free = shutil.disk_usage(db_file.parent).free
    if free < needed:
        raise RuntimeError(f"The data is compressed, but shrinking the file needs about {needed // 2**20:,} MB "
                           f"free next to the database and there's {free // 2**20:,} MB. Free some space and run "
                           "compact again.")
    before = db_file.stat().st_size
    conn.execute("PRAGMA temp_store_directory = " + _quote(str(db_file.parent)))
    try:
        conn.execute("VACUUM")
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except sqlite3.OperationalError as exc:
        raise RuntimeError(f"The data is compressed, but the file couldn't be shrunk: {exc}.") from exc
    finally:
        conn.execute("PRAGMA temp_store_directory = ''")
    report(f"Shrank the database file from {before // 2**20:,} MB to {db_file.stat().st_size // 2**20:,} MB.")


def _quote(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"
