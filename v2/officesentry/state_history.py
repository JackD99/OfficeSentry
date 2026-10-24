"""State history: every collected object stored once per version, with the runs it was valid between.

Each successful run keeps a full copy of what it read in ``objects`` (store.py) until retention prunes it: the
last 30 days, and the last run of each of 12 months, by default. Here an object is stored again only when it
changes. A version row (``state_versions``) says from which run of its data source it held, and until which
(``to_run``: the first recorded run without it, NULL while it's current); its content is one line of a
compressed pack (``state_packs``) holding the objects that changed in that run. So any recorded run can be
rebuilt exactly, however long ago, for about the room of one copy plus what changed.

Fields that change nearly every night (``VOLATILE``: last sign-in times, mailbox sizes, usage report dates) stay
out of the versions, or every active account would get a new version each night. A run stores them in one
compressed row per kind (``state_volatile``): the values that changed since the kind's previous row, or every
object's values in a full row at least every ``KEYFRAME_ROWS`` rows. Reading puts them back, so a rebuilt
object is the one that was stored.

Shadow mode, for now: collections still write ``objects`` and every page reads that. Each successful run is
also recorded here as it finishes (``record_finished``), once its data source's earlier runs are. The worker
records the rest, oldest first, a few seconds a minute (``catch_up``), which backfills the snapshots kept from
before the upgrade; it pauses while the disk is over ``system_health.DISK_WARN`` full. Every recorded run is
read back and compared with its snapshot (``verify_run``); a difference is logged and counted (``status``).
``python -m officesentry history`` shows progress, and ``--dry-run`` estimates the room and time the backfill
needs without writing anything.

Sign-in detail (``retention.SIGNIN_KINDS``) isn't recorded: the activity history (events.py) keeps sign-ins,
under its own retention. Reading falls back to the snapshot for those kinds, and for runs not recorded yet.

Read through ``run_objects``, ``find_object``, ``object_copies`` and ``matching_objects``, which answer like
their namesakes in store.py.
"""

from __future__ import annotations

import hashlib
import json
import logging
import random
import time
import zlib
from collections.abc import Callable
from dataclasses import dataclass

from . import retention, schedule, store
from .text import plural

log = logging.getLogger(__name__)

# The snapshot readers, kept here so this module's fallbacks still reach them if store's are swapped for these.
_stored_objects = store.run_objects
_stored_object = store.find_object

SUCCESS = ("succeeded", "partial")
WATERMARK = "state_history.recorded_to"  # every run at or below this ID is recorded or never will be
MISMATCHES = "state_history.mismatches"  # runs that didn't read back as their snapshot
LAST_MISMATCH = "state_history.last_mismatch"
SECONDS_PER_TICK = 5.0
RUNS_PER_QUERY = 500
HASH_BYTES = 8
KEYFRAME_ROWS = 7  # a full row of fast-changing fields at least every 7 rows, so reading one takes at most 7

# Per kind, top-level fields that change nearly every night, stored per run instead of versioned.
VOLATILE: dict[str, tuple[str, ...]] = {
    # Entra ID sign-in activity and device check-ins.
    "user": ("lastSignInDateTime", "lastNonInteractiveSignInDateTime", "lastSuccessfulSignInDateTime"),
    "entra_device": ("approximateLastSignInDateTime",),
    "managed_device": ("lastSyncDateTime",),
    # Usage reports: a new report date every day, and the activity and sizes that move with it.
    "user_usage": ("reportDate", "lastActive", "active", "teamsCounts", "copilotActive", "copilotLastActive"),
    "onedrive": ("reportDate", "lastActivityDate", "storageUsed", "files", "activeFiles"),
    "site": ("reportDate", "lastActivityDate", "storageUsed", "files", "activeFiles", "pageViews"),
    "mailbox_size": ("totalBytes", "itemCount", "deletedBytes"),
    "team": ("lastActivity", "activeUsers", "messages", "meetings"),
}

# A run still holds its snapshot: it stored nothing, or retention hasn't removed what it stored.
HOLDS_OBJECTS = "(runs.object_count = 0 OR EXISTS (SELECT 1 FROM objects o WHERE o.run_id = runs.id))"
# A run whose data can be read: from its snapshot, or rebuilt from the history.
READABLE = f"({HOLDS_OBJECTS} OR EXISTS (SELECT 1 FROM state_runs s WHERE s.run_id = runs.id))"


def _skipped() -> frozenset[str]:
    return frozenset(retention.SIGNIN_KINDS)


# -- Versions --------------------------------------------------------------------------------------


def split(kind: str, data) -> tuple[object, dict | None]:
    """(what is versioned, the fast-changing fields or None) of one object."""
    names = VOLATILE.get(kind)
    if not names or not isinstance(data, dict):
        return data, None
    moving = {name: data[name] for name in names if name in data}
    if not moving:
        return data, None
    return {k: v for k, v in data.items() if k not in moving}, moving


def merge(stable, moving: dict | None):
    """The object ``split`` took apart, keys in the stored (sorted) order."""
    if not moving:
        return stable
    both = {**stable, **moving}
    return {k: both[k] for k in sorted(both)}


def _version(kind: str, object_id: str, data) -> tuple[bytes, bytes, dict | None]:
    """(hash, line, fast-changing fields) of one object's version."""
    stable, moving = split(kind, data)
    line = store._line(object_id, stable)
    return hashlib.blake2b(line, digest_size=HASH_BYTES).digest(), line, moving


def _packs(lines: dict[str, bytes]) -> list[tuple[list[str], bytes]]:
    """(object ids, compressed blob) of each pack of ``lines`` ({object id: line}), in object id order."""
    ids, start, out = sorted(lines), 0, []
    for _first, n, blob in store._pack_rows(lines):
        out.append((ids[start : start + n], blob))
        start += n
    return out


def _compress_moving(moving: dict[str, dict | None]) -> bytes:
    lines = b"\n".join(json.dumps([oid, moving[oid]], sort_keys=True).encode() for oid in sorted(moving))
    return store.PACK_ZLIB + zlib.compress(lines, store.PACK_LEVEL)


def _moving_at(conn, series_id: int, run_id: int) -> tuple[dict[str, dict], int]:
    """({object id: fast-changing fields} as of a run of the series, rows read since the last full one): the
    latest full row at or before the run, then each change since. A changed object's entry is all its fields,
    or None when it no longer has any."""
    state: dict[str, dict] = {}
    rows = 0
    for row in conn.execute(
        "SELECT full, data FROM state_volatile WHERE series_id = ? AND run_id <= ? AND run_id >= COALESCE("
        "(SELECT MAX(run_id) FROM state_volatile WHERE series_id = ? AND run_id <= ? AND full = 1), 0)"
        " ORDER BY run_id",
        (series_id, run_id, series_id, run_id),
    ):
        if row["full"]:
            state, rows = {}, 0
        for line in store.pack_lines(row["data"]):
            if not line:
                continue  # a full row of nothing: no object has any left
            oid, moving = json.loads(line)
            if moving is None:
                state.pop(oid, None)
            else:
                state[oid] = moving
        rows += 1
    return state, rows


def _write_moving(conn, series_id: int, run_id: int, previous: int | None, moving: dict[str, dict]) -> None:
    """Store a run's fast-changing fields for one series: what changed since ``previous`` (the series' last
    recorded run), or every value when a full row is due."""
    before, rows = _moving_at(conn, series_id, previous) if previous is not None else ({}, 0)
    changed: dict[str, dict | None] = {oid: m for oid, m in moving.items() if before.get(oid) != m}
    changed.update(dict.fromkeys(before.keys() - moving.keys()))
    if not changed:
        return
    full = not rows or rows >= KEYFRAME_ROWS or 2 * len(changed) > len(moving)
    conn.execute(
        "INSERT INTO state_volatile (series_id, run_id, full, data) VALUES (?, ?, ?, ?)",
        (series_id, run_id, int(full), _compress_moving(moving if full else changed)),
    )


def _line_id(line: bytes) -> str:
    """The object id at the start of a pack line (``["<id>", ...``), without parsing the rest."""
    return json.decoder.scanstring(line.decode(), 2)[0]


def _snapshot(conn, run_id: int) -> dict[str, dict[str, object]]:
    """{kind: {object id: data}} of what a run stored, sign-in detail left out."""
    skip = _skipped()
    kinds = [
        r[0]
        for r in conn.execute("SELECT DISTINCT kind FROM objects WHERE run_id = ? ORDER BY kind", (run_id,))
        if r[0] not in skip
    ]
    return {kind: dict(json.loads(line) for line in store.stored_lines(conn, run_id, kind).values()) for kind in kinds}


def _series(conn, tenant_id: int, collector: str) -> dict[str, int]:
    return {
        r["kind"]: r["id"]
        for r in conn.execute(
            "SELECT id, kind FROM state_series WHERE tenant_id = ? AND collector = ?", (tenant_id, collector)
        )
    }


def _last_recorded(conn, tenant_id: int, collector: str) -> int | None:
    return conn.execute(
        "SELECT MAX(run_id) FROM state_runs WHERE tenant_id = ? AND collector = ?", (tenant_id, collector)
    ).fetchone()[0]


def record_run(conn, run, objects: dict[str, dict[str, object]] | None = None) -> bool:
    """Record one successful run: close the versions it no longer has, and add what's new or changed.

    ``objects`` ({kind: {object id: data}}) is what the run stored, read from its snapshot by default. Returns
    False, recording nothing, when this run or a later one of its data source is recorded already: a data
    source's runs go in in order.
    """
    if objects is None:
        objects = _snapshot(conn, run["id"])
    versions = {
        kind: {oid: _version(kind, oid, data) for oid, data in items.items()} for kind, items in objects.items()
    }
    tenant_id, collector, run_id = run["tenant_id"], run["collector"], run["id"]
    with store.atomic(conn):
        last = _last_recorded(conn, tenant_id, collector)
        if last is not None and last >= run_id:
            return False
        series = _series(conn, tenant_id, collector)
        for kind in sorted(versions.keys() - series.keys()):
            series[kind] = conn.execute(
                "INSERT INTO state_series (tenant_id, collector, kind) VALUES (?, ?, ?)", (tenant_id, collector, kind)
            ).lastrowid
        ends, starts = [], []
        for kind, series_id in sorted(series.items()):
            now = versions.get(kind, {})
            current = {
                r["object_id"]: (r["from_run"], r["hash"])
                for r in conn.execute(
                    "SELECT object_id, from_run, hash FROM state_versions WHERE series_id = ? AND to_run IS NULL",
                    (series_id,),
                )
            }
            ends += [
                (run_id, series_id, oid, from_run)
                for oid, (from_run, digest) in current.items()
                if oid not in now or now[oid][0] != digest
            ]
            new = {oid: v[1] for oid, v in now.items() if oid not in current or current[oid][1] != v[0]}
            for ids, blob in _packs(new):
                pack = conn.execute(
                    "INSERT INTO state_packs (tenant_id, run_id, kind, data) VALUES (?, ?, ?, ?)",
                    (tenant_id, run_id, kind, blob),
                ).lastrowid
                starts += [(series_id, oid, run_id, now[oid][0], pack) for oid in ids]
            _write_moving(conn, series_id, run_id, last, {oid: v[2] for oid, v in now.items() if v[2] is not None})
        conn.executemany(
            "UPDATE state_versions SET to_run = ? WHERE series_id = ? AND object_id = ? AND from_run = ?", ends
        )
        conn.executemany(
            "INSERT INTO state_versions (series_id, object_id, from_run, hash, pack_id) VALUES (?, ?, ?, ?, ?)", starts
        )
        conn.execute(
            "INSERT INTO state_runs (run_id, tenant_id, collector, finished_at, objects) VALUES (?, ?, ?, ?, ?)",
            (run_id, tenant_id, collector, run["finished_at"], sum(len(items) for items in objects.values())),
        )
    return True


# -- Reading ---------------------------------------------------------------------------------------

_VALID = (
    "SELECT object_id, pack_id FROM state_versions WHERE series_id = ? AND from_run <= ?"
    " AND (to_run IS NULL OR to_run > ?)"
)


class _Reader:
    """Rebuilds objects of recorded runs, decompressing each pack once."""

    def __init__(self, conn):
        self.conn = conn
        self._packs: dict[int, dict[str, bytes]] = {}
        self._moving: dict[tuple[int, int], dict[str, dict]] = {}

    def _pack(self, pack_id: int) -> dict[str, bytes]:
        if pack_id not in self._packs:
            row = self.conn.execute("SELECT data FROM state_packs WHERE id = ?", (pack_id,)).fetchone()
            self._packs[pack_id] = {_line_id(line): line for line in store.pack_lines(row["data"])}
        return self._packs[pack_id]

    def _volatile(self, series_id: int, run_id: int) -> dict[str, dict]:
        if (series_id, run_id) not in self._moving:
            self._moving[series_id, run_id] = _moving_at(self.conn, series_id, run_id)[0]
        return self._moving[series_id, run_id]

    def object(self, series_id: int, run_id: int, object_id: str, pack_id: int):
        """A new copy of the object as run ``run_id`` stored it, from its version in ``pack_id``."""
        stable = json.loads(self._pack(pack_id)[object_id])[1]
        return merge(stable, self._volatile(series_id, run_id).get(object_id))

    def kind_at(self, series_id: int, run_id: int):
        """(object id, data) of every object of the series as of the run, by object id."""
        for row in self.conn.execute(_VALID + " ORDER BY object_id", (series_id, run_id, run_id)):
            yield row["object_id"], self.object(series_id, run_id, row["object_id"], row["pack_id"])


def recorded(conn, run_id: int):
    """The run's ``state_runs`` row, or None when it isn't recorded."""
    return conn.execute("SELECT * FROM state_runs WHERE run_id = ?", (run_id,)).fetchone()


def run_objects(conn, run_id: int, kind: str | None = None, limit: int | None = None) -> list[dict]:
    """``store.run_objects`` for a run, rebuilt from the history (from the snapshot if it isn't recorded)."""
    run = recorded(conn, run_id)
    if run is None:
        return _stored_objects(conn, run_id, kind, limit)
    skip, reader = _skipped(), _Reader(conn)
    series = _series(conn, run["tenant_id"], run["collector"])
    if kind:
        kinds = [kind]
    else:
        stored_skipped = {
            r[0] for r in conn.execute("SELECT DISTINCT kind FROM objects WHERE run_id = ?", (run_id,)) if r[0] in skip
        }
        kinds = sorted(series.keys() | stored_skipped)
    out: list[dict] = []
    for name in kinds:
        if name in skip:
            out += _stored_objects(conn, run_id, name)
        elif name in series:
            out += [dict(data, _kind=name, _id=oid) for oid, data in reader.kind_at(series[name], run_id)]
        if limit and len(out) >= limit:
            return out[:limit]
    return out


def objects_of(conn, run_id: int, kind: str | None = None) -> list[dict]:
    """A run's objects: from its snapshot while it has one, as every page reads them, else rebuilt from the
    history. How a past date is read (``TenantSnapshot(as_of=...)``, change feeds)."""
    if conn.execute(f"SELECT 1 FROM runs WHERE id = ? AND {HOLDS_OBJECTS}", (run_id,)).fetchone():
        return _stored_objects(conn, run_id, kind)
    return run_objects(conn, run_id, kind)


def earliest(conn, tenant_id: int) -> str | None:
    """When the oldest data that can still be read for a tenant was collected (None: nothing yet). Runs that
    stored nothing don't count: a data source with nothing in it (no inbox rules, say) has nothing for pruning
    to remove, so its first run would stay the earliest long after the rest of that day's data has gone."""
    found = [
        conn.execute(
            "SELECT MIN(finished_at) FROM state_runs WHERE tenant_id = ? AND objects > 0", (tenant_id,)
        ).fetchone()[0],
        conn.execute(
            "SELECT MIN(finished_at) FROM runs WHERE tenant_id = ? AND status IN ('succeeded', 'partial')"
            " AND object_count > 0 AND EXISTS (SELECT 1 FROM objects o WHERE o.run_id = runs.id)",
            (tenant_id,),
        ).fetchone()[0],
    ]
    return min((f for f in found if f), default=None)


def find_object(conn, run_id: int, kind: str, object_id: str):
    """``store.find_object`` for a run, rebuilt from the history (from the snapshot if it isn't recorded)."""
    run = recorded(conn, run_id)
    if run is None or kind in _skipped():
        return _stored_object(conn, run_id, kind, object_id)
    series_id = _series(conn, run["tenant_id"], run["collector"]).get(kind)
    row = series_id and conn.execute(_VALID + " AND object_id = ?", (series_id, run_id, run_id, object_id)).fetchone()
    return _Reader(conn).object(series_id, run_id, object_id, row["pack_id"]) if row else None


def _runs(conn, tenant_id: int, collector: str) -> list[tuple[int, str, bool]]:
    """(run ID, finished at, recorded) of a data source's runs that can be read: the recorded ones, then the
    successful runs since that aren't recorded yet and still hold their snapshot."""
    rows = [
        (r["run_id"], r["finished_at"], True)
        for r in conn.execute(
            "SELECT run_id, finished_at FROM state_runs WHERE tenant_id = ? AND collector = ? ORDER BY run_id",
            (tenant_id, collector),
        )
    ]
    after = rows[-1][0] if rows else 0
    rows += [
        (r["id"], r["finished_at"], False)
        for r in conn.execute(
            "SELECT id, finished_at FROM runs WHERE tenant_id = ? AND collector = ?"
            " AND status IN ('succeeded', 'partial')"
            f" AND id > ? AND {HOLDS_OBJECTS} ORDER BY id",
            (tenant_id, collector, after),
        )
    ]
    return rows


def object_copies(conn, tenant_id: int, collector: str, kind: str, object_id: str) -> list[tuple[str, dict]]:
    """``store.object_copies`` from the history: (finished at, data) of one object in each run that held it."""
    if kind in _skipped():
        return [
            (at, d)
            for run_id, at, _ in _runs(conn, tenant_id, collector)
            if (d := _stored_object(conn, run_id, kind, object_id)) is not None
        ]
    series_id = _series(conn, tenant_id, collector).get(kind)
    versions = (
        []
        if series_id is None
        else conn.execute(
            "SELECT from_run, to_run, pack_id FROM state_versions WHERE series_id = ? AND object_id = ?"
            " ORDER BY from_run",
            (series_id, object_id),
        ).fetchall()
    )
    reader, out = _Reader(conn), []
    for run_id, at, is_recorded in _runs(conn, tenant_id, collector):
        if not is_recorded:
            data = _stored_object(conn, run_id, kind, object_id)
        else:
            held = next(
                (v for v in versions if v["from_run"] <= run_id and (v["to_run"] is None or v["to_run"] > run_id)), None
            )
            data = reader.object(series_id, run_id, object_id, held["pack_id"]) if held else None
        if data is not None:
            out.append((at, data))
    return out


def matching_objects(
    conn, tenant_id: int, collector: str, kind: str, field: str, value: str
) -> list[tuple[str, str, int, dict]]:
    """``store.matching_objects`` from the history: (object id, finished at, run ID, data) of every object of
    the kind whose ``field`` is ``value``, oldest run first, then by object id."""
    skip = kind in _skipped()
    series_id = _series(conn, tenant_id, collector).get(kind)
    reader, out = _Reader(conn), []
    for run_id, at, is_recorded in _runs(conn, tenant_id, collector):
        if skip or not is_recorded:
            found = (
                (o["_id"], {k: v for k, v in o.items() if k not in ("_kind", "_id")})
                for o in _stored_objects(conn, run_id, kind)
            )
        elif series_id is None:
            continue
        else:
            found = reader.kind_at(series_id, run_id)
        out += [(oid, at, run_id, data) for oid, data in found if isinstance(data, dict) and data.get(field) == value]
    return out


# -- Checking --------------------------------------------------------------------------------------


def verify_run(conn, run_id: int) -> list[str] | None:
    """Kinds whose objects the history rebuilds differently from the run's snapshot ([] when they all match),
    or None when that can't be checked: the run isn't recorded, or retention removed its snapshot."""
    if (
        recorded(conn, run_id) is None
        or not conn.execute(f"SELECT 1 FROM runs WHERE id = ? AND {HOLDS_OBJECTS}", (run_id,)).fetchone()
    ):
        return None
    expected = {
        kind: {oid: store._line(oid, data) for oid, data in items.items()}
        for kind, items in _snapshot(conn, run_id).items()
    }
    skip = _skipped()
    differ = []
    for kind in sorted(expected.keys() | {k for k in _series_of_run(conn, run_id) if k not in skip}):
        try:
            got = {
                o["_id"]: store._line(o["_id"], {k: v for k, v in o.items() if k not in ("_kind", "_id")})
                for o in run_objects(conn, run_id, kind)
            }
        except (KeyError, TypeError, ValueError, zlib.error):  # a damaged pack reads as a difference
            got = None
        if got != expected.get(kind, {}):
            differ.append(kind)
    return differ


def _series_of_run(conn, run_id: int) -> set[str]:
    """Kinds with a version valid at a recorded run."""
    run = recorded(conn, run_id)
    return {
        kind
        for kind, series_id in _series(conn, run["tenant_id"], run["collector"]).items()
        if conn.execute(_VALID + " LIMIT 1", (series_id, run_id, run_id)).fetchone()
    }


def _check(conn, run_id: int) -> None:
    """Compare a run just recorded with its snapshot; a difference is a bug, logged and counted for ``status``."""
    differ = verify_run(conn, run_id)
    if differ:
        log.error("State history: run %s reads back differently from its snapshot (%s)", run_id, ", ".join(differ))
        with store.atomic(conn):
            count = int(_setting(conn, MISMATCHES) or 0) + 1
            _set(conn, MISMATCHES, str(count))
            _set(conn, LAST_MISMATCH, f"run {run_id}: {', '.join(differ)}")


def _setting(conn, key: str) -> str | None:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def _set(conn, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


# -- Recording as runs finish, and catching up -------------------------------------------------------


def room(conn) -> bool:
    """Whether there's room to write: the database's disk is under ``system_health.DISK_WARN`` full."""
    from . import backups, system_health

    path = backups.database_file(conn)
    if path is None:
        return True
    used, _free, _total = system_health.disk_usage(path.parent)
    return used < system_health.DISK_WARN


def record_finished(conn, run_id: int) -> bool:
    """Record a run that has just finished, once its data source's earlier runs are recorded (until then,
    ``catch_up`` records them all in order). Returns whether it was recorded."""
    run = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    if run is None or run["status"] not in SUCCESS or not room(conn):
        return False
    last = _last_recorded(conn, run["tenant_id"], run["collector"])
    if conn.execute(
        "SELECT 1 FROM runs WHERE tenant_id = ? AND collector = ? AND status IN ('succeeded', 'partial')"
        f" AND id > ? AND id < ? AND {HOLDS_OBJECTS} LIMIT 1",
        (run["tenant_id"], run["collector"], last or 0, run_id),
    ).fetchone():
        return False
    if not record_run(conn, run):
        return False
    _check(conn, run_id)
    return True


def catch_up(
    conn,
    seconds: float | None = SECONDS_PER_TICK,
    clock: Callable[[], float] = time.monotonic,
    report: Callable[[int], None] | None = None,
) -> tuple[int, bool]:
    """Record finished runs that aren't recorded yet, oldest first, for about ``seconds`` (None: until done).

    Runs whose snapshot retention removed are passed over: what's left of them is the run row. Progress is
    kept in ``settings`` (``WATERMARK``): every run up to it is recorded or never will be, so a run still
    queued or running holds it back until it finishes. Returns (runs recorded, whether nothing is left).
    """
    stop = None if seconds is None else clock() + seconds
    mark = position = int(_setting(conn, WATERMARK) or 0)
    held, done = False, 0
    try:
        while True:
            if not room(conn):
                return done, False
            rows = conn.execute(
                "SELECT id, tenant_id, collector, status, finished_at, object_count FROM runs WHERE id > ?"
                " ORDER BY id LIMIT ?",
                (position, RUNS_PER_QUERY),
            ).fetchall()
            if not rows:
                return done, not held
            for run in rows:
                if stop is not None and clock() >= stop:
                    return done, False
                if run["status"] in ("queued", "running"):
                    held = True
                elif (
                    run["status"] in SUCCESS
                    and recorded(conn, run["id"]) is None
                    and conn.execute(f"SELECT 1 FROM runs WHERE id = ? AND {HOLDS_OBJECTS}", (run["id"],)).fetchone()
                ):
                    if record_run(conn, run):
                        done += 1
                        _check(conn, run["id"])
                        if report and done % 1000 == 0:
                            report(done)
                position = run["id"]
                if not held:
                    mark = position
    finally:
        if mark != int(_setting(conn, WATERMARK) or 0):
            _set(conn, WATERMARK, str(mark))


_paused = False


@schedule.periodic("state_history")
def _history_job(ctx) -> None:
    global _paused
    if not room(ctx.conn):
        if not _paused:
            log.warning("State history: paused while the disk is nearly full; it carries on once there's room")
        _paused = True
        return
    _paused = False
    done, _ = catch_up(ctx.conn)
    if done:
        log.info("State history: recorded %s", plural(done, "run"))


# -- Retention -------------------------------------------------------------------------------------


def _cuts(conn, before: str) -> list[tuple[int, str, int, int]]:
    """(tenant ID, collector, first run kept, runs before it) of each data source with recorded runs that
    finished before ``before``. The runs from the first one finished since are kept, and each data source's
    latest recorded run always is: like the latest snapshot, it's the current state."""
    out = []
    for r in conn.execute(
        "SELECT tenant_id, collector, MIN(CASE WHEN finished_at >= ? THEN run_id END) AS cut,"
        " MAX(run_id) AS latest, MIN(run_id) AS first, COUNT(*) AS n FROM state_runs"
        " GROUP BY tenant_id, collector",
        (before,),
    ):
        cut = r["cut"] or r["latest"]
        if r["first"] < cut:
            old = conn.execute(
                "SELECT COUNT(*) FROM state_runs WHERE tenant_id = ? AND collector = ? AND run_id < ?",
                (r["tenant_id"], r["collector"], cut),
            ).fetchone()[0]
            out.append((r["tenant_id"], r["collector"], cut, old))
    return out


def prune(conn, before: str, dry_run: bool = False) -> int:
    """Forget the state before ``before``: the runs recorded before it (each data source's latest stays), the
    versions that ended by then and their fast-changing fields. Returns the recorded runs removed (or, with
    ``dry_run``, that would be).

    One short transaction per data source, so the portal and collections aren't held up.
    """
    cuts = _cuts(conn, before)
    if dry_run:
        return sum(n for *_, n in cuts)
    for tenant_id, collector, cut, _n in cuts:
        with store.atomic(conn):
            for series_id in _series(conn, tenant_id, collector).values():
                conn.execute("DELETE FROM state_versions WHERE series_id = ? AND to_run <= ?", (series_id, cut))
                # The first run kept gets a full row of fast-changing fields, so it reads without the older rows.
                if conn.execute(
                    "SELECT 1 FROM state_volatile WHERE series_id = ? AND run_id < ? LIMIT 1", (series_id, cut)
                ).fetchone():
                    moving, _ = _moving_at(conn, series_id, cut)
                    conn.execute("DELETE FROM state_volatile WHERE series_id = ? AND run_id <= ?", (series_id, cut))
                    if moving:
                        conn.execute(
                            "INSERT INTO state_volatile (series_id, run_id, full, data) VALUES (?, ?, 1, ?)",
                            (series_id, cut, _compress_moving(moving)),
                        )
            conn.execute(
                "DELETE FROM state_runs WHERE tenant_id = ? AND collector = ? AND run_id < ?",
                (tenant_id, collector, cut),
            )
    for tenant_id in sorted({c[0] for c in cuts}):
        with store.atomic(conn):  # then the packs no version of the tenant points at any more
            conn.execute(
                "DELETE FROM state_packs WHERE tenant_id = ? AND id NOT IN (SELECT v.pack_id"
                " FROM state_versions v JOIN state_series s ON s.id = v.series_id WHERE s.tenant_id = ?)",
                (tenant_id, tenant_id),
            )
    return sum(n for *_, n in cuts)


# -- Status and the dry run ------------------------------------------------------------------------


def _pending(conn) -> tuple[int, int]:
    """(runs, objects) still to record: finished successful runs past the watermark that hold a snapshot."""
    row = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(object_count), 0) FROM runs WHERE id > ? AND status IN ('succeeded', 'partial')"
        f" AND {HOLDS_OBJECTS} AND NOT EXISTS (SELECT 1 FROM state_runs s WHERE s.run_id = runs.id)",
        (int(_setting(conn, WATERMARK) or 0),),
    ).fetchone()
    return row[0], row[1]


def status(conn) -> dict:
    """What's recorded, what's left, the room it takes and any run that didn't read back as its snapshot."""
    pending, pending_objects = _pending(conn)
    one = lambda sql: conn.execute(sql).fetchone()[0]  # noqa: E731
    return {
        "runs": one("SELECT COUNT(*) FROM state_runs"),
        "versions": one("SELECT COUNT(*) FROM state_versions"),
        "objects": one("SELECT COALESCE(SUM(objects), 0) FROM state_runs"),
        "pending": pending,
        "pending_objects": pending_objects,
        "bytes": one("SELECT COALESCE(SUM(length(data)), 0) FROM state_packs")
        + one("SELECT COALESCE(SUM(length(data)), 0) FROM state_volatile"),
        "snapshot_bytes": one("SELECT COALESCE(SUM(length(data)), 0) FROM objects"),
        "oldest": one("SELECT MIN(finished_at) FROM state_runs"),
        "mismatches": int(_setting(conn, MISMATCHES) or 0),
        "last_mismatch": _setting(conn, LAST_MISMATCH),
    }


ROW_BYTES = 40  # a version row's room beside its object ID, measured with scripts/scale_data.py --history


@dataclass
class Estimate:
    """What recording the pending runs would take, from a sample of data sources (``estimate``)."""

    pending: int = 0
    pending_objects: int = 0
    sampled_runs: int = 0
    sampled_objects: int = 0
    sampled_sources: int = 0
    seconds: float = 0.0
    versions: int = 0
    bytes: int = 0

    @property
    def share(self) -> float:
        return self.sampled_objects / self.pending_objects if self.pending_objects else 1.0

    @property
    def total_bytes(self) -> int:
        return int(self.bytes / self.share) if self.share else 0

    @property
    def total_seconds(self) -> float:
        return self.seconds / self.share if self.share else 0.0


def estimate(
    conn, seconds: float = 20.0, clock: Callable[[], float] = time.monotonic, rng: random.Random | None = None
) -> Estimate:
    """The dry run: the room and time recording every pending run would take, worked out without writing
    anything. Whole data sources (each run of one tenant's collector) are recorded in memory, chosen at
    random, for about ``seconds``, and the totals scaled up by their share of the pending objects."""
    rng = rng or random.Random()  # noqa: S311 (sampling, not security)
    est = Estimate()
    est.pending, est.pending_objects = _pending(conn)
    if not est.pending:
        return est
    mark = int(_setting(conn, WATERMARK) or 0)
    sources = [
        (r["tenant_id"], r["collector"])
        for r in conn.execute(
            "SELECT DISTINCT tenant_id, collector FROM runs WHERE id > ? AND status IN ('succeeded', 'partial')"
            " AND NOT EXISTS (SELECT 1 FROM state_runs s WHERE s.run_id = runs.id)",
            (mark,),
        )
    ]
    rng.shuffle(sources)
    start = clock()
    for tenant_id, collector in sources:
        if clock() - start >= seconds and est.sampled_runs:
            break
        current = {}
        for kind, series_id in _series(conn, tenant_id, collector).items():
            current[kind] = {
                r["object_id"]: r["hash"]
                for r in conn.execute(
                    "SELECT object_id, hash FROM state_versions WHERE series_id = ? AND to_run IS NULL", (series_id,)
                )
            }
        runs = conn.execute(
            "SELECT id, object_count FROM runs WHERE tenant_id = ? AND collector = ? AND id > ?"
            f" AND status IN ('succeeded', 'partial') AND {HOLDS_OBJECTS}"
            " AND NOT EXISTS (SELECT 1 FROM state_runs s WHERE s.run_id = runs.id) ORDER BY id",
            (tenant_id, collector, _last_recorded(conn, tenant_id, collector) or 0),
        ).fetchall()
        for run in runs:
            objects = _snapshot(conn, run["id"])
            for kind in current.keys() | objects.keys():
                now = {oid: _version(kind, oid, data) for oid, data in objects.get(kind, {}).items()}
                before = current.get(kind, {})
                new = {oid: v[1] for oid, v in now.items() if before.get(oid) != v[0]}
                est.versions += len(new)
                est.bytes += sum(len(blob) for _ids, blob in _packs(new))
                est.bytes += sum(len(oid) + ROW_BYTES for oid in new)
                if moving := {oid: v[2] for oid, v in now.items() if v[2] is not None}:
                    est.bytes += len(_compress_moving(moving))
                current[kind] = {oid: v[0] for oid, v in now.items()}
            est.sampled_runs += 1
            est.sampled_objects += run["object_count"]
        est.sampled_sources += 1
    est.seconds = clock() - start
    return est
