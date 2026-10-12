"""Activity history: sign-ins and directory audit events kept for months or years, not Microsoft's 30 days.

Where it lives
  ``<data dir>/events/<store>/<YYYY-MM>.db``: one SQLite file per store and calendar month (UTC), outside
  the main database, so the main file and its nightly backups don't grow with the logs, a closed month
  never changes again, and dropping a month is deleting a file. The stores:

  * ``signins``: every sign-in the sign-in log returned, one row each;
  * ``daily``: per tenant, account, day, app and country: successes, failures, MFA, first and last time
    and the top addresses, rebuilt from the raw rows of each day a run touched, and kept much longer;
  * ``audits``: every directory audit event, its full text compressed;
  * ``activity``: the audit log add-on's records (``auditlog/``): the file, sharing and mailbox events it keeps,
    one row each with its full text compressed, and OneDrive sync traffic counted per person and day
    (``activity_count``, one row per batch Microsoft delivered it in, so reading a batch twice counts it once).

  Rows are keyed ``(tenant, time, id)`` in ``WITHOUT ROWID`` tables and written with ``INSERT OR IGNORE``,
  so reading the same event twice stores it once. Repeated text (names, apps, places) goes in a small
  per-tenant ``label`` table. ``tenant`` is the tenant's key in ``event_tenants`` (never reused, unlike
  ``tenants.id``) and every read filters on it, so one tenant's events never show under another.

How collection works (``ingest``)
  ``event_cursors`` says how far each tenant's log has been read: everything from ``since`` to
  ``read_to`` except its ``gaps``. Each run adds ``[read_to - OVERLAP, now]`` as the newest gap (Microsoft
  adds some events late, and re-reading a few hours costs little since duplicates are ignored), then reads
  the gaps newest first, each newest first. After every page the gap shrinks to what is still unread and
  the cursor is saved, after the page's events are, so a run that stops for any reason (a crash, the
  worker restarting, Microsoft timing out, the per-run page budget) loses nothing and the next run carries
  on from there. A range that times out is read again a day at a time; a day that still times out stays a
  gap; after the deadline the rest waits for the next run. The first run reads back as far as Microsoft
  keeps the log. A gap older than that is gone for good: it moves to ``lost``, which pages show.

Retention, deletion and backups are in ``retention.py``, ``lifecycle.py`` and ``backups.py``; the
helpers they use are at the bottom of this file.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import uuid
import zlib
from collections import Counter
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from . import signins
from .backups import database_file
from .db import transaction
from .devices import parse_ts
from .graph import GraphError, is_transient

log = logging.getLogger(__name__)

STORES = ("signins", "daily", "audits", "activity")
TABLE = {"signins": "signin", "daily": "daily", "audits": "audit", "activity": "activity"}
# A table beside a store's own that also holds tenants' rows, by ``tenant`` and ``day``.
COUNTS = {"activity": "activity_count"}
DAY = 86400
OVERLAP = 6 * 3600  # each run re-reads the last six hours: Microsoft can add an event an hour or two late
SLICE = DAY  # a range that times out is read again a day at a time
MAX_LOST = 20  # lost ranges remembered per log
MONTH = re.compile(r"^\d{4}-\d{2}$")


def clock() -> datetime:
    """Now, in UTC. Tests replaying fixed data set it to when the data was recorded."""
    return datetime.now(UTC)


def _iso(ts: int) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _epoch(value) -> int | None:
    dt = parse_ts(value)
    return int(dt.timestamp()) if dt else None


def _month(ts: int) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m")


def _day(ts: int) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%d")


def _month_bounds(month: str) -> tuple[int, int]:
    """First second of the month and first second of the next one."""
    start = datetime.strptime(month, "%Y-%m").replace(tzinfo=UTC)
    nxt = (start + timedelta(days=32)).replace(day=1)
    return int(start.timestamp()), int(nxt.timestamp())


def _months(start: int, end: int) -> list[str]:
    """Every month from the one holding ``start`` to the one holding ``end``, newest first."""
    out, month = [], _month(end)
    first = _month(max(start, 0))
    while month >= first:
        out.append(month)
        month = _month(_month_bounds(month)[0] - 1)
    return out


# -- Files ----------------------------------------------------------------------------------------

_LABEL = (
    "CREATE TABLE IF NOT EXISTS label (id INTEGER PRIMARY KEY, tenant INTEGER NOT NULL, value TEXT NOT NULL,"
    " UNIQUE (tenant, value))"
)
SCHEMA = {
    "signins": (
        _LABEL,
        """CREATE TABLE IF NOT EXISTS signin (
        tenant INTEGER NOT NULL, ts INTEGER NOT NULL, id NOT NULL,
        user INTEGER NOT NULL, user_id INTEGER NOT NULL, name INTEGER NOT NULL,
        app INTEGER NOT NULL, app_id INTEGER NOT NULL, client INTEGER NOT NULL, ip TEXT,
        country INTEGER NOT NULL, city INTEGER NOT NULL, os INTEGER NOT NULL,
        code INTEGER NOT NULL, reason INTEGER NOT NULL, mfa INTEGER, ca INTEGER NOT NULL,
        risk INTEGER NOT NULL, risk_state INTEGER NOT NULL, risk_detail INTEGER NOT NULL, risk_types TEXT,
        interactive INTEGER NOT NULL,
        PRIMARY KEY (tenant, ts, id)) WITHOUT ROWID""",
    ),
    "daily": (
        _LABEL,
        """CREATE TABLE IF NOT EXISTS daily (
        tenant INTEGER NOT NULL, day TEXT NOT NULL, user INTEGER NOT NULL, app INTEGER NOT NULL,
        country INTEGER NOT NULL, user_id INTEGER NOT NULL,
        succeeded INTEGER NOT NULL, failed INTEGER NOT NULL, interrupted INTEGER NOT NULL,
        mfa INTEGER NOT NULL, single_factor INTEGER NOT NULL, first_ts INTEGER NOT NULL, last_ts INTEGER NOT NULL,
        ips TEXT NOT NULL,
        PRIMARY KEY (tenant, day, user, app, country)) WITHOUT ROWID""",
    ),
    "audits": (
        _LABEL,
        """CREATE TABLE IF NOT EXISTS audit (
        tenant INTEGER NOT NULL, ts INTEGER NOT NULL, id NOT NULL,
        category INTEGER NOT NULL, activity INTEGER NOT NULL, result INTEGER NOT NULL,
        initiator INTEGER NOT NULL, target INTEGER NOT NULL, detail BLOB NOT NULL,
        PRIMARY KEY (tenant, ts, id)) WITHOUT ROWID""",
    ),
    "activity": (
        _LABEL,
        """CREATE TABLE IF NOT EXISTS activity (
        tenant INTEGER NOT NULL, ts INTEGER NOT NULL, id NOT NULL,
        operation INTEGER NOT NULL, actor INTEGER NOT NULL, owner INTEGER NOT NULL, object TEXT NOT NULL,
        ip TEXT, summary TEXT NOT NULL, attention TEXT NOT NULL, detail BLOB NOT NULL,
        PRIMARY KEY (tenant, ts, id)) WITHOUT ROWID""",
        """CREATE TABLE IF NOT EXISTS activity_count (
        tenant INTEGER NOT NULL, day TEXT NOT NULL, actor INTEGER NOT NULL, operation INTEGER NOT NULL,
        batch TEXT NOT NULL, n INTEGER NOT NULL,
        PRIMARY KEY (tenant, day, actor, operation, batch)) WITHOUT ROWID""",
    ),
}


def root(conn) -> Path | None:
    """The event folder beside the database file, or None for an in-memory database."""
    db = database_file(conn)
    return db.parent / "events" if db else None


def _path(base: Path, store: str, month: str) -> Path:
    return base / store / f"{month}.db"


def months(base: Path | None, store: str) -> list[str]:
    """The months a store has a file for, oldest first."""
    folder = base / store if base else None
    if folder is None or not folder.is_dir():
        return []
    return sorted(p.stem for p in folder.glob("*.db") if MONTH.match(p.stem))


@contextmanager
def _open(path: Path, store: str, create: bool = False) -> Iterator[sqlite3.Connection | None]:
    """A connection to one month file (None when it doesn't exist and ``create`` is false)."""
    if not path.exists():
        if not create:
            yield None
            return
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.close(os.open(path, os.O_CREAT | os.O_WRONLY, 0o600))
    db = sqlite3.connect(path, timeout=30, isolation_level=None)
    try:
        db.execute("PRAGMA busy_timeout = 30000")
        if create:
            db.execute("PRAGMA journal_mode = WAL")  # the portal reads while the worker writes
            for sql in SCHEMA[store]:
                db.execute(sql)
        elif not db.execute("SELECT 1 FROM sqlite_master WHERE name = ?", (TABLE[store],)).fetchone():
            yield None  # created but never written: nothing to read
            return
        db.execute("PRAGMA secure_delete = FAST")
        yield db
    finally:
        db.close()


def _remove(path: Path) -> None:
    for p in (path, path.with_name(path.name + "-wal"), path.with_name(path.name + "-shm")):
        p.unlink(missing_ok=True)


class _Labels:
    """Text to its id in a file's label table, for one tenant."""

    def __init__(self, db: sqlite3.Connection, tenant: int):
        self.db, self.tenant, self.cache = db, tenant, {}

    def __call__(self, value) -> int:
        text = value if isinstance(value, str) else ("" if value is None else str(value))
        found = self.cache.get(text)
        if found is None:
            self.db.execute("INSERT OR IGNORE INTO label (tenant, value) VALUES (?, ?)", (self.tenant, text))
            found = self.cache[text] = self.db.execute(
                "SELECT id FROM label WHERE tenant = ? AND value = ?", (self.tenant, text)
            ).fetchone()[0]
        return found


def _names(db: sqlite3.Connection, tenant: int) -> dict[int, str]:
    return dict(db.execute("SELECT id, value FROM label WHERE tenant = ?", (tenant,)).fetchall())


def _pack_id(value: str):
    """GUIDs as 16 bytes; anything else (directory audit ids) as text."""
    try:
        return uuid.UUID(value).bytes
    except ValueError:
        return value


def _unpack_id(value) -> str:
    return str(uuid.UUID(bytes=value)) if isinstance(value, bytes) else str(value)


def _text(value) -> str:
    return value if isinstance(value, str) else ""


def _dict(value) -> dict:
    return value if isinstance(value, dict) else {}


# -- Tenants and cursors (in the main database) ---------------------------------------------------


def tenant_key(conn, tenant_id: int, create: bool = False) -> int | None:
    """The tenant's key in the event files; None when it has none yet (and ``create`` is false)."""
    row = conn.execute("SELECT id FROM event_tenants WHERE tenant_id = ?", (tenant_id,)).fetchone()
    if row or not create:
        return row[0] if row else None
    with transaction(conn, immediate=True):
        conn.execute(
            "INSERT OR IGNORE INTO event_tenants (tenant_id, created_at) VALUES (?, ?)",
            (tenant_id, _iso(int(clock().timestamp()))),
        )
    return conn.execute("SELECT id FROM event_tenants WHERE tenant_id = ?", (tenant_id,)).fetchone()[0]


@dataclass
class Coverage:
    """How far one tenant's log has been read. Times are seconds since 1970 (UTC)."""

    source: str
    since: int | None = None  # the oldest time the history starts from
    read_to: int | None = None  # read up to here, apart from the gaps
    gaps: list[list[int]] = field(default_factory=list)  # [from, to] still to read, newest first
    lost: list[list[int]] = field(default_factory=list)  # gone from Microsoft before they were read
    pending: list[str] = field(default_factory=list)  # days whose daily summary needs rebuilding
    updated_at: str | None = None

    @property
    def detailed_from(self) -> int | None:
        """Where the unbroken history starts: ``since``, moved past any gap at the very bottom."""
        if self.since is None or self.read_to is None:
            return None
        start = self.since
        for lo, hi in sorted(self.gaps):
            if lo > start:
                break
            start = max(start, hi)
        return start if start < self.read_to else None

    @property
    def up_to_date(self) -> bool:
        return self.read_to is not None and not self.gaps

    def missing(self, start: int, end: int) -> list[tuple[int, int]]:
        """The parts of ``[start, end]`` that haven't been read, oldest first."""
        holes = [tuple(g) for g in self.gaps + self.lost]
        first = self.detailed_from
        holes.append((start, first if first is not None else end))
        if self.read_to is not None and self.read_to < end:
            holes.append((self.read_to, end))
        clipped = sorted((max(lo, start), min(hi, end)) for lo, hi in holes if hi > start and lo < end)
        merged: list[tuple[int, int]] = []
        for lo, hi in clipped:
            if lo >= hi:
                continue
            if merged and lo <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], hi))
            else:
                merged.append((lo, hi))
        return merged


def _times(raw: str) -> list[list[int]]:
    out = []
    for lo, hi in json.loads(raw or "[]"):
        a, b = _epoch(lo), _epoch(hi)
        if a is not None and b is not None:
            out.append([a, b])
    return out


def coverage(conn, tenant_id: int, source: str) -> Coverage:
    row = conn.execute("SELECT * FROM event_cursors WHERE tenant_id = ? AND source = ?", (tenant_id, source)).fetchone()
    if row is None:
        return Coverage(source)
    return Coverage(
        source,
        _epoch(row["since_at"]),
        _epoch(row["read_to"]),
        _times(row["gaps"]),
        _times(row["lost"]),
        json.loads(row["pending"] or "[]"),
        row["updated_at"],
    )


def save_coverage(conn, tenant_id: int, cov: Coverage) -> None:
    def times(ranges):
        return json.dumps([[_iso(lo), _iso(hi)] for lo, hi in ranges])

    cov.updated_at = _iso(int(clock().timestamp()))
    with transaction(conn, immediate=True):
        conn.execute(
            "INSERT INTO event_cursors (tenant_id, source, since_at, read_to, gaps, lost, pending, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (tenant_id, source) DO UPDATE SET"
            " since_at = excluded.since_at, read_to = excluded.read_to, gaps = excluded.gaps,"
            " lost = excluded.lost, pending = excluded.pending, updated_at = excluded.updated_at",
            (
                tenant_id,
                cov.source,
                None if cov.since is None else _iso(cov.since),
                None if cov.read_to is None else _iso(cov.read_to),
                times(cov.gaps),
                times(cov.lost),
                json.dumps(sorted(set(cov.pending))),
                cov.updated_at,
            ),
        )


def _merge(gaps: list[list[int]]) -> list[list[int]]:
    """Overlapping or touching ranges joined, newest first."""
    out: list[list[int]] = []
    for lo, hi in sorted((list(g) for g in gaps if g[0] <= g[1]), key=lambda g: -g[1]):
        if out and hi >= out[-1][0]:
            out[-1][0] = min(out[-1][0], lo)
        else:
            out.append([lo, hi])
    return out


def extend(cov: Coverage, now: int, floor: int, overlap: int = OVERLAP) -> None:
    """Add this run's range (from ``overlap`` before where the last one read to), and move what Microsoft no
    longer keeps (before ``floor``) out of the gaps."""
    if cov.read_to is None:
        cov.since, cov.gaps = floor, [[floor, now]]
    else:
        cov.gaps = _merge([[cov.read_to - overlap, now], *cov.gaps])
    cov.read_to = max(now, cov.read_to or now)
    kept = []
    for lo, hi in cov.gaps:
        if lo < floor:
            gone = [lo, min(hi, floor)]
            if cov.since is not None and gone[0] <= cov.since:
                cov.since = max(cov.since, gone[1])  # never read at the start: the history just starts later
            else:
                cov.lost = _merge([gone, *cov.lost])[:MAX_LOST]
        if hi > floor:
            kept.append([max(lo, floor), hi])
    cov.gaps = kept


# -- Writing --------------------------------------------------------------------------------------


@dataclass
class Added:
    rows: int = 0  # events with a time and an id
    new: int = 0  # of those, not stored before
    oldest: int | None = None
    days: set[str] = field(default_factory=set)


def _write(base: Path, store: str, key: int, items, time_key: str, row: Callable) -> Added:
    added = Added()
    by_month: dict[str, list[tuple[int, object, dict]]] = {}
    for item in items:
        if not isinstance(item, dict) or not _text(item.get("id")):
            continue
        ts = _epoch(item.get(time_key))
        if ts is None:
            continue
        added.rows += 1
        added.oldest = ts if added.oldest is None else min(added.oldest, ts)
        added.days.add(_day(ts))
        by_month.setdefault(_month(ts), []).append((ts, _pack_id(item["id"]), item))
    table = TABLE[store]
    for month, rows in by_month.items():
        with _open(_path(base, store, month), store, create=True) as db, transaction(db, immediate=True):
            label = _Labels(db, key)
            values = [(key, ts, eid, *row(label, item)) for ts, eid, item in rows]
            marks = ", ".join("?" * len(values[0]))
            for v in values:
                added.new += db.execute(f"INSERT OR IGNORE INTO {table} VALUES ({marks})", v).rowcount
    return added


MFA = "multiFactorAuthentication"
SINGLE = "singleFactorAuthentication"
SIGNIN_COLUMNS = (
    "ts",
    "id",
    "user",
    "user_id",
    "name",
    "app",
    "app_id",
    "client",
    "ip",
    "country",
    "city",
    "os",
    "code",
    "reason",
    "mfa",
    "ca",
    "risk",
    "risk_state",
    "risk_detail",
    "risk_types",
    "interactive",
)


def _signin_row(label: _Labels, s: dict) -> tuple:
    status, where, device = _dict(s.get("status")), _dict(s.get("location")), _dict(s.get("deviceDetail"))
    mfa = signins.mfa_used(s)
    types = s.get("riskEventTypes_v2") or s.get("riskEventTypes") or []
    types = [t for t in types if isinstance(t, str)] if isinstance(types, list) else []
    return (
        label(s.get("userPrincipalName")),
        label(s.get("userId")),
        label(s.get("userDisplayName")),
        label(s.get("appDisplayName")),
        label(s.get("appId")),
        label(s.get("clientAppUsed")),
        _text(s.get("ipAddress")) or None,
        label(where.get("countryOrRegion")),
        label(where.get("city")),
        label(device.get("operatingSystem")),
        signins.code(status.get("errorCode")),
        label(status.get("failureReason")),
        None if mfa is None else int(mfa),
        label(s.get("conditionalAccessStatus")),
        label(s.get("riskLevelDuringSignIn")),
        label(s.get("riskState")),
        label(s.get("riskDetail")),
        json.dumps(types) if types else None,
        0 if s.get("isInteractive") is False else 1,
    )


def add_signins(base: Path, key: int, items) -> Added:
    return _write(base, "signins", key, items, "createdDateTime", _signin_row)


def _initiator(e: dict) -> str:
    by = _dict(e.get("initiatedBy"))
    user, app = _dict(by.get("user")), _dict(by.get("app"))
    return (
        _text(user.get("userPrincipalName"))
        or _text(user.get("displayName"))
        or _text(app.get("displayName"))
        or _text(app.get("servicePrincipalName"))
        or _text(app.get("appId"))
    )


def _target(e: dict) -> str:
    for t in e.get("targetResources") or []:
        t = _dict(t)
        name = _text(t.get("userPrincipalName")) or _text(t.get("displayName"))
        if name:
            return name
    return ""


def _audit_row(label: _Labels, e: dict) -> tuple:
    detail = zlib.compress(json.dumps(e, separators=(",", ":"), sort_keys=True).encode(), 6)
    return (
        label(e.get("category")),
        label(e.get("activityDisplayName")),
        label(e.get("result")),
        label(_initiator(e)),
        label(_target(e)),
        detail,
    )


def add_audits(base: Path, key: int, items) -> Added:
    return _write(base, "audits", key, items, "activityDateTime", _audit_row)


@dataclass(frozen=True)
class Activity:
    """One audit record the audit log add-on keeps (``auditlog/operations.py``), as stored and read back."""

    id: str
    ts: int
    operation: str  # Microsoft's ``Operation``
    actor: str  # who did it
    owner: str  # whose mailbox, or who a file was shared with
    object: str  # the file, folder, link, rule or mailbox
    ip: str
    summary: str  # the detail worth a table cell
    attention: str  # why it needs a look, or ""
    record: dict | None = None  # the full record (email subjects taken out), when read with ``records=True``

    @property
    def when(self) -> str:
        return _iso(self.ts)


def add_activity(base: Path, key: int, kept: Iterable[Activity], counts: Iterable[tuple], batch: str) -> Added:
    """Store one batch's kept records, and its counted sync traffic ``(day, actor, operation, n)`` in place of
    whatever was counted for ``batch`` before, so a batch read twice is counted once."""
    added = Added()
    by_month: dict[str, list[Activity]] = {}
    for a in kept:
        added.rows += 1
        added.oldest = a.ts if added.oldest is None else min(added.oldest, a.ts)
        added.days.add(_day(a.ts))
        by_month.setdefault(_month(a.ts), []).append(a)
    counted: dict[str, list[tuple]] = {}
    for day, actor, operation, n in counts:
        counted.setdefault(day[:7], []).append((day, actor, operation, n))
    for month in sorted(set(by_month) | set(counted)):
        with _open(_path(base, "activity", month), "activity", create=True) as db, transaction(db, immediate=True):
            label = _Labels(db, key)
            for a in by_month.get(month, []):
                detail = zlib.compress(json.dumps(a.record or {}, separators=(",", ":"), sort_keys=True).encode(), 6)
                added.new += db.execute(
                    "INSERT OR IGNORE INTO activity VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        key,
                        a.ts,
                        _pack_id(a.id),
                        label(a.operation),
                        label(a.actor),
                        label(a.owner),
                        a.object,
                        a.ip or None,
                        a.summary,
                        a.attention,
                        detail,
                    ),
                ).rowcount
            db.execute("DELETE FROM activity_count WHERE tenant = ? AND batch = ?", (key, batch))
            for day, actor, operation, n in counted.get(month, []):
                db.execute(
                    "INSERT INTO activity_count VALUES (?, ?, ?, ?, ?, ?)",
                    (key, day, label(actor), label(operation), batch, n),
                )
    return added


# -- Reading --------------------------------------------------------------------------------------


def _where(conn, tenant_id: int) -> tuple[Path, int] | None:
    base = root(conn)
    key = tenant_key(conn, tenant_id) if base else None
    return (base, key) if key is not None else None


def _signin_dict(row, names: dict[int, str]) -> dict:
    (
        ts,
        sid,
        user,
        user_id,
        name,
        app,
        app_id,
        client,
        ip,
        country,
        city,
        os_,
        code,
        reason,
        mfa,
        ca,
        risk,
        risk_state,
        risk_detail,
        risk_types,
        interactive,
    ) = row

    def n(i):
        return names.get(i) or None

    return {
        "id": _unpack_id(sid),
        "createdDateTime": _iso(ts),
        "userPrincipalName": n(user),
        "userId": n(user_id),
        "userDisplayName": n(name),
        "appDisplayName": n(app),
        "appId": n(app_id),
        "clientAppUsed": n(client),
        "ipAddress": ip,
        "location": {"countryOrRegion": n(country), "city": n(city)},
        "deviceDetail": {"operatingSystem": n(os_)},
        "status": {"errorCode": code, "failureReason": n(reason)},
        "authenticationRequirement": {1: MFA, 0: SINGLE}.get(mfa),
        "conditionalAccessStatus": n(ca),
        "riskLevelDuringSignIn": n(risk),
        "riskState": n(risk_state),
        "riskDetail": n(risk_detail),
        "riskEventTypes_v2": json.loads(risk_types) if risk_types else [],
        "isInteractive": bool(interactive),
    }


def signins_between(conn, tenant_id: int, start: datetime, end: datetime, oldest_first: bool = False) -> Iterator[dict]:
    """Stored sign-ins from ``start`` to ``end`` (inclusive), newest first (or oldest first), in the sign-in
    log's own shape. Rows are read as they're used, so a month's download never holds the month in memory."""
    where = _where(conn, tenant_id)
    if where is None:
        return
    base, key = where
    lo, hi = int(start.timestamp()), int(end.timestamp())
    order = "ts, id" if oldest_first else "ts DESC, id"
    for month in (reversed if oldest_first else list)(_months(lo, hi)):
        with _open(_path(base, "signins", month), "signins") as db:
            if db is None:
                continue
            names = _names(db, key)
            yield from (
                _signin_dict(r, names)
                for r in db.execute(
                    f"SELECT {', '.join(SIGNIN_COLUMNS)} FROM signin WHERE tenant = ? AND ts BETWEEN ? AND ?"
                    f" ORDER BY {order}",
                    (key, lo, hi),
                )
            )


def audits_between(
    conn, tenant_id: int, start: datetime, end: datetime, keep: Callable[[str, str], bool] | None = None
) -> Iterator[dict]:
    """Stored directory audit events from ``start`` to ``end``, newest first, as Graph returned them.
    ``keep(category, activity)`` picks events before their text is unpacked."""
    where = _where(conn, tenant_id)
    if where is None:
        return
    base, key = where
    lo, hi = int(start.timestamp()), int(end.timestamp())
    for month in _months(lo, hi):
        with _open(_path(base, "audits", month), "audits") as db:
            if db is None:
                continue
            names = _names(db, key)
            chosen: dict[tuple[int, int], bool] = {}
            for category, activity, detail in db.execute(
                "SELECT category, activity, detail FROM audit WHERE tenant = ? AND ts BETWEEN ? AND ?"
                " ORDER BY ts DESC, id",
                (key, lo, hi),
            ):
                if keep is not None:
                    pair = (category, activity)
                    if pair not in chosen:
                        chosen[pair] = keep(names.get(category, ""), names.get(activity, ""))
                    if not chosen[pair]:
                        continue
                try:
                    yield json.loads(zlib.decompress(detail))
                except (zlib.error, ValueError):
                    log.warning("Unreadable directory audit event in %s", month)


def activity_between(
    conn, tenant_id: int, start: datetime, end: datetime, records: bool = False, people: set[str] | None = None
) -> Iterator[Activity]:
    """The audit log add-on's stored records from ``start`` to ``end`` (inclusive), newest first; with
    ``records``, each with its full record. With ``people`` (sign-in names or addresses, any case), only the
    records one of them did or that were about them (their mailbox, or something shared with them)."""
    where = _where(conn, tenant_id)
    if where is None:
        return
    base, key = where
    lo, hi = int(start.timestamp()), int(end.timestamp())
    wanted = None if people is None else {p.lower() for p in people if p}
    for month in _months(lo, hi):
        with _open(_path(base, "activity", month), "activity") as db:
            if db is None:
                continue
            names = _names(db, key)
            sql, args = "", []
            if wanted is not None:
                ids = [i for i, name in names.items() if name and name.lower() in wanted]
                if not ids:
                    continue
                marks = ", ".join("?" * len(ids))
                sql, args = f" AND (actor IN ({marks}) OR owner IN ({marks}))", ids + ids
            for ts, aid, operation, actor, owner, obj, ip, summary, attention, detail in db.execute(
                "SELECT ts, id, operation, actor, owner, object, ip, summary, attention, detail FROM activity"
                f" WHERE tenant = ? AND ts BETWEEN ? AND ?{sql} ORDER BY ts DESC, id",
                (key, lo, hi, *args),
            ):
                record = None
                if records:
                    try:
                        record = json.loads(zlib.decompress(detail))
                    except (zlib.error, ValueError):
                        log.warning("Unreadable audit record in %s", month)
                        record = {}
                yield Activity(
                    _unpack_id(aid),
                    ts,
                    names.get(operation, ""),
                    names.get(actor, ""),
                    names.get(owner, ""),
                    obj,
                    ip or "",
                    summary,
                    attention,
                    record,
                )


def activity_counts(conn, tenant_id: int, first_day: str, last_day: str) -> list[tuple[str, str, str, int]]:
    """The add-on's counted sync traffic from ``first_day`` to ``last_day`` (inclusive): ``(day, person,
    operation, count)``, oldest first."""
    where = _where(conn, tenant_id)
    if where is None:
        return []
    base, key = where
    lo, hi = datetime.fromisoformat(first_day).replace(tzinfo=UTC), datetime.fromisoformat(last_day).replace(tzinfo=UTC)
    out = []
    for month in reversed(_months(int(lo.timestamp()), int(hi.timestamp()))):
        with _open(_path(base, "activity", month), "activity") as db:
            if db is None:
                continue
            names = _names(db, key)
            out.extend(
                (day, names.get(actor, ""), names.get(operation, ""), n)
                for day, actor, operation, n in db.execute(
                    "SELECT day, actor, operation, SUM(n) FROM activity_count WHERE tenant = ? AND day BETWEEN ? AND ?"
                    " GROUP BY day, actor, operation ORDER BY day",
                    (key, first_day, last_day),
                )
            )
    return out


@dataclass
class DayRow:
    """One account's sign-ins on one day, from one app and country."""

    day: str
    user: str
    user_id: str | None
    app: str
    country: str | None
    succeeded: int
    failed: int
    interrupted: int
    mfa: int
    single_factor: int
    first: str
    last: str
    ips: list[str]

    @property
    def sign_ins(self) -> int:
        return self.succeeded + self.failed + self.interrupted


def daily_between(conn, tenant_id: int, first_day: str, last_day: str) -> list[DayRow]:
    """Daily sign-in summaries from ``first_day`` to ``last_day`` (YYYY-MM-DD, inclusive), oldest first."""
    where = _where(conn, tenant_id)
    if where is None:
        return []
    base, key = where
    out: list[DayRow] = []
    start = int(datetime.fromisoformat(first_day).replace(tzinfo=UTC).timestamp())
    end = int(datetime.fromisoformat(last_day).replace(tzinfo=UTC).timestamp())
    for month in reversed(_months(start, end)):
        with _open(_path(base, "daily", month), "daily") as db:
            if db is None:
                continue
            names = _names(db, key)
            for day, user, user_id, app, country, ok, failed, interrupted, mfa, single, first, last, ips in db.execute(
                "SELECT day, user, user_id, app, country, succeeded, failed, interrupted, mfa, single_factor,"
                " first_ts, last_ts, ips FROM daily WHERE tenant = ? AND day BETWEEN ? AND ? ORDER BY day",
                (key, first_day, last_day),
            ):
                out.append(
                    DayRow(
                        day,
                        names.get(user) or "Unknown",
                        names.get(user_id) or None,
                        names.get(app) or "Unknown app",
                        names.get(country) or None,
                        ok,
                        failed,
                        interrupted,
                        mfa,
                        single,
                        _iso(first),
                        _iso(last),
                        json.loads(ips),
                    )
                )
    return out


def daily_for(conn, tenant_id: int, names: Iterable[str], first_day: str, last_day: str) -> list[DayRow]:
    """``daily_between`` for one person: rows whose sign-in name or object ID is one of ``names``."""
    wanted = {n.lower() for n in names if isinstance(n, str) and n}
    where = _where(conn, tenant_id)
    if where is None or not wanted:
        return []
    base, key = where
    out: list[DayRow] = []
    start = int(datetime.fromisoformat(first_day).replace(tzinfo=UTC).timestamp())
    end = int(datetime.fromisoformat(last_day).replace(tzinfo=UTC).timestamp())
    for month in reversed(_months(start, end)):
        with _open(_path(base, "daily", month), "daily") as db:
            if db is None:
                continue
            names_of = _names(db, key)
            ids = [i for i, v in names_of.items() if v.lower() in wanted]
            if not ids:
                continue
            marks = ",".join("?" * len(ids))
            for day, user, user_id, app, country, ok, failed, interrupted, mfa, single, first, last, ips in db.execute(
                "SELECT day, user, user_id, app, country, succeeded, failed, interrupted, mfa, single_factor,"
                f" first_ts, last_ts, ips FROM daily WHERE tenant = ? AND day BETWEEN ? AND ?"
                f" AND (user IN ({marks}) OR user_id IN ({marks})) ORDER BY day",
                (key, first_day, last_day, *ids, *ids),
            ):
                out.append(
                    DayRow(
                        day,
                        names_of.get(user) or "Unknown",
                        names_of.get(user_id) or None,
                        names_of.get(app) or "Unknown app",
                        names_of.get(country) or None,
                        ok,
                        failed,
                        interrupted,
                        mfa,
                        single,
                        _iso(first),
                        _iso(last),
                        json.loads(ips),
                    )
                )
    return out


@dataclass
class Totals:
    """Sign-ins over a day or a month, from the daily summaries."""

    period: str  # YYYY-MM-DD or YYYY-MM
    succeeded: int = 0
    failed: int = 0
    interrupted: int = 0
    mfa: int = 0
    single_factor: int = 0
    people: int = 0
    countries: list[str] = field(default_factory=list)

    @property
    def sign_ins(self) -> int:
        return self.succeeded + self.failed + self.interrupted


_TOTALS = "SUM(succeeded), SUM(failed), SUM(interrupted), SUM(mfa), SUM(single_factor), COUNT(DISTINCT user)"


def totals(conn, tenant_id: int, first_day: str, last_day: str) -> tuple[list[Totals], list[Totals]]:
    """Per day and per month (oldest first) from ``first_day`` to ``last_day``: what the daily summaries hold."""
    where = _where(conn, tenant_id)
    if where is None:
        return [], []
    base, key = where
    days: list[Totals] = []
    by_month: list[Totals] = []
    start = int(datetime.fromisoformat(first_day).replace(tzinfo=UTC).timestamp())
    end = int(datetime.fromisoformat(last_day).replace(tzinfo=UTC).timestamp())
    for month in reversed(_months(start, end)):
        with _open(_path(base, "daily", month), "daily") as db:
            if db is None:
                continue
            args = (key, first_day, last_day)
            for day, *numbers in db.execute(
                f"SELECT day, {_TOTALS} FROM daily WHERE tenant = ? AND day BETWEEN ? AND ? GROUP BY day ORDER BY day",
                args,
            ):
                days.append(Totals(day, *numbers))
            row = db.execute(f"SELECT {_TOTALS} FROM daily WHERE tenant = ? AND day BETWEEN ? AND ?", args).fetchone()
            if row[0] is None:
                continue
            names = _names(db, key)
            countries = sorted(
                names.get(c) or ""
                for (c,) in db.execute(
                    "SELECT DISTINCT country FROM daily WHERE tenant = ? AND day BETWEEN ? AND ?", args
                )
            )
            by_month.append(Totals(month, *row, countries=[c for c in countries if c]))
    return days, by_month


def months_with_signins(conn, tenant_id: int) -> list[str]:
    """Months holding stored sign-ins for the tenant, newest first."""
    where = _where(conn, tenant_id)
    if where is None:
        return []
    base, key = where
    found = []
    for month in reversed(months(base, "signins")):
        with _open(_path(base, "signins", month), "signins") as db:
            if db is not None and db.execute("SELECT 1 FROM signin WHERE tenant = ? LIMIT 1", (key,)).fetchone():
                found.append(month)
    return found


def rebuild_daily(base: Path, key: int, days: Iterable[str]) -> int:
    """Rebuild the daily summaries of ``days`` from the raw sign-ins. Returns the rows written."""
    written = 0
    for day in sorted(set(days)):
        start = int(datetime.fromisoformat(day).replace(tzinfo=UTC).timestamp())
        groups: dict[tuple[str, str, str], dict] = {}
        with _open(_path(base, "signins", _month(start)), "signins") as db:
            names = _names(db, key) if db else {}
            rows = (
                db.execute(
                    "SELECT ts, user, user_id, app, country, code, mfa, ip FROM signin"
                    " WHERE tenant = ? AND ts BETWEEN ? AND ? AND interactive = 1 ORDER BY ts",
                    (key, start, start + DAY - 1),
                ).fetchall()
                if db
                else []
            )
        for ts, user, user_id, app, country, code, mfa, ip in rows:
            upn = names.get(user) or names.get(user_id) or "Unknown"
            k = (upn.lower(), names.get(app) or "Unknown app", names.get(country) or "")
            g = groups.get(k)
            if g is None:
                g = groups[k] = {
                    "user": upn,
                    "user_id": names.get(user_id) or "",
                    "succeeded": 0,
                    "failed": 0,
                    "interrupted": 0,
                    "mfa": 0,
                    "single": 0,
                    "first": ts,
                    "last": ts,
                    "ips": Counter(),
                }
            result = signins.outcome(code)
            g[{"success": "succeeded", "failure": "failed"}.get(result, "interrupted")] += 1
            if result == "success":
                g["mfa"] += mfa == 1
                g["single"] += mfa == 0
            g["first"], g["last"] = min(g["first"], ts), max(g["last"], ts)
            if ip:
                g["ips"][ip] += 1
        path = _path(base, "daily", _month(start))
        if not groups and not path.exists():
            continue
        with _open(path, "daily", create=True) as db, transaction(db, immediate=True):
            label = _Labels(db, key)
            db.execute("DELETE FROM daily WHERE tenant = ? AND day = ?", (key, day))
            for (_, app, country), g in groups.items():
                db.execute(
                    "INSERT INTO daily VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        key,
                        day,
                        label(g["user"]),
                        label(app),
                        label(country),
                        label(g["user_id"]),
                        g["succeeded"],
                        g["failed"],
                        g["interrupted"],
                        g["mfa"],
                        g["single"],
                        g["first"],
                        g["last"],
                        json.dumps([ip for ip, _ in g["ips"].most_common(5)]),
                    ),
                )
                written += 1
    return written


# -- Incremental collection -----------------------------------------------------------------------


@dataclass(frozen=True)
class Log:
    source: str
    path: str
    version: str
    time_field: str
    page_size: int
    write: Callable[[Path, int, list], Added]
    daily: bool = False  # keeps daily summaries
    select: str = ""  # the fields asked for; empty asks for all of them


# Beta, for authenticationRequirement (whether a sign-in needed MFA or only a password), which v1.0 doesn't
# have. Ask for only what ``_signin_row`` stores: Microsoft sends Deprecation and Sunset headers (sunset 31 August
# 2026, for fields it retired from beta sign-ins) when every field comes back, and none when these are selected.
SIGNIN_FIELDS = ",".join(
    (
        "id",
        "createdDateTime",
        "userPrincipalName",
        "userId",
        "userDisplayName",
        "appDisplayName",
        "appId",
        "clientAppUsed",
        "ipAddress",
        "location",
        "deviceDetail",
        "status",
        "authenticationRequirement",
        "conditionalAccessStatus",
        "riskLevelDuringSignIn",
        "riskState",
        "riskDetail",
        "riskEventTypes_v2",
        "isInteractive",
    )
)
SIGNIN_LOG = Log(
    "signins", "/auditLogs/signIns", "beta", "createdDateTime", 1000, add_signins, daily=True, select=SIGNIN_FIELDS
)
AUDIT_LOG = Log("audits", "/auditLogs/directoryAudits", "v1.0", "activityDateTime", 999, add_audits)


class _Budget(Exception):
    """The run has read as many pages as it may."""


@dataclass
class Ingest:
    """What one run read."""

    coverage: Coverage
    pages: int = 0
    read: int = 0  # events returned
    new: int = 0  # events not stored before
    budget_hit: bool = False
    gave_up: bool = False  # the deadline passed before every gap was read
    failed_windows: int = 0  # one-day windows that still timed out
    failures: list[GraphError] = field(default_factory=list)


class _Reader:
    def __init__(
        self,
        ctx,
        source: Log,
        base: Path,
        key: int,
        cov: Coverage,
        now: int,
        budget: int,
        retries: int | None,
        deadline: Callable[[], Callable[[], bool]],
    ):
        self.ctx, self.src, self.base, self.key, self.cov, self.now = ctx, source, base, key, cov, now
        self.budget, self.retries, self.deadline = budget, retries, deadline
        self.result = Ingest(cov)
        self.expired: Callable[[], bool] | None = None
        self.stopped = False

    def save(self) -> None:
        save_coverage(self.ctx.conn, self.ctx.tenant["id"], self.cov)

    def run(self) -> None:
        i = 0
        while i < len(self.cov.gaps) and not self.stopped:
            i = self.gap(i)

    def gap(self, i: int) -> int:
        lo, hi = self.cov.gaps[i]
        try:
            self.pages(i, lo, hi, open_top=hi == self.now)
        except _Budget:
            self.result.budget_hit = self.stopped = True
            return i
        except GraphError as exc:
            if not is_transient(exc):
                raise
            self.failed(exc)
            self.ctx.log(f"The {self.src.path} query from {_iso(lo)} failed ({exc}); reading it a day at a time")
            return self.by_day(i)
        del self.cov.gaps[i]
        self.save()
        return i

    def failed(self, exc: GraphError) -> None:
        self.result.failures.append(exc)
        if self.expired is None:
            self.expired = self.deadline()

    def pages(self, i: int, lo: int, hi: int, open_top: bool) -> None:
        f = self.src.time_field
        flt = f"{f} ge {_iso(lo)}" + ("" if open_top else f" and {f} le {_iso(hi)}")
        url: str | None = self.src.path
        params: dict | None = {"$filter": flt, "$top": str(self.src.page_size)}
        if self.src.select:
            params["$select"] = self.src.select
        while url:
            if self.result.pages >= self.budget:
                raise _Budget
            data = self.ctx.graph.get(url, params, version=self.src.version, transient_retries=self.retries)
            params = None
            self.result.pages += 1
            added = self.src.write(self.base, self.key, data.get("value") or [])
            self.result.read += added.rows
            self.result.new += added.new
            if self.src.daily and added.days:
                self.cov.pending = sorted(set(self.cov.pending) | added.days)
            gap = self.cov.gaps[i]
            if added.oldest is not None and added.oldest < gap[1]:
                gap[1] = max(added.oldest, gap[0])  # everything newer than the oldest event read is in
            self.save()
            url = data.get("@odata.nextLink")

    def by_day(self, i: int) -> int:
        lo, end = self.cov.gaps[i]
        while end > lo:
            if self.expired is not None and self.expired():
                self.result.gave_up = self.stopped = True
                return i
            start = max(end - SLICE, lo)
            self.cov.gaps[i] = [start, end]
            if start > lo:
                self.cov.gaps.insert(i + 1, [lo, start])
            self.save()
            try:
                self.pages(i, start, end, open_top=False)
            except _Budget:
                self.result.budget_hit = self.stopped = True
                return i
            except GraphError as exc:
                if not is_transient(exc):
                    raise
                self.failed(exc)
                self.result.failed_windows += 1
                self.ctx.log(f"{self.src.path} from {_iso(start)} to {_iso(end)} failed too: {exc}")
                i += 1
            else:
                del self.cov.gaps[i]
            self.save()
            end = start
        return i


def _no_deadline() -> Callable[[], bool]:
    return lambda: False


def ingest(
    ctx,
    source: Log,
    keep_days: int,
    budget: int,
    *,
    retries: int | None = None,
    deadline: Callable[[], Callable[[], bool]] = _no_deadline,
    now: datetime | None = None,
) -> Ingest:
    """Read the log from where the last run got to, into the event files. See the module docstring.

    ``keep_days`` is how far back Microsoft keeps the log, ``budget`` the most pages this run reads.
    ``deadline`` starts a timer at the first timeout; once it says so, the rest waits for the next run.
    ``now`` is the caller's own clock reading, so the window it then checks with ``coverage.missing`` starts
    exactly where this run's coverage does (reading the clock again can land a second later and leave a
    one-second "unread" gap at the start).
    Raises the first failure when every request failed, and anything that isn't a timeout or server
    error (no licence or permission); the cursor is saved first, so the next run carries on.
    """
    conn, tenant_id = ctx.conn, ctx.tenant["id"]
    base = root(conn)
    if base is None:
        raise RuntimeError("Activity history needs the database in a file")
    key = tenant_key(conn, tenant_id, create=True)
    now = int((now or clock()).timestamp())
    cov = coverage(conn, tenant_id, source.source)
    extend(cov, now, now - keep_days * DAY)
    reader = _Reader(ctx, source, base, key, cov, now, budget, retries, deadline)
    reader.save()
    try:
        reader.run()
    finally:
        if cov.pending:
            rebuild_daily(base, key, cov.pending)
            cov.pending = []
        reader.save()
    result = reader.result
    if result.failures and not result.pages:
        raise result.failures[0]
    return result


# -- Housekeeping: retention, deleting a tenant, counts -------------------------------------------


def _older(db: sqlite3.Connection, store: str, cutoff: int, count_only: bool, batch: int = 5000) -> int:
    """Rows older than ``cutoff`` in one file: counted, or deleted in batches."""
    table = TABLE[store]
    if store == "daily":
        where, arg = "day < ?", _day(cutoff)
    else:
        where, arg = "ts < ?", cutoff
    if count_only:
        return db.execute(f"SELECT COUNT(*) FROM {table} WHERE {where}", (arg,)).fetchone()[0]
    if store in COUNTS:
        with transaction(db, immediate=True):
            db.execute(f"DELETE FROM {COUNTS[store]} WHERE day < ?", (_day(cutoff),))
    done = 0
    while True:
        with transaction(db, immediate=True):
            cols = ", ".join(_key(store))
            n = db.execute(
                f"DELETE FROM {table} WHERE ({cols}) IN (SELECT {cols} FROM {table} WHERE {where} LIMIT ?)",
                (arg, batch),
            ).rowcount
        done += n
        if n < batch:
            return done


def _key(store: str) -> tuple[str, ...]:
    return ("tenant", "day", "user", "app", "country") if store == "daily" else ("tenant", "ts", "id")


def _rows(db: sqlite3.Connection, store: str) -> int:
    return db.execute(f"SELECT COUNT(*) FROM {TABLE[store]}").fetchone()[0]


def prune(conn, store: str, cutoff: datetime, dry_run: bool = False) -> int:
    """Delete (or count) a store's rows older than ``cutoff``: whole months by deleting their file."""
    base = root(conn)
    edge = int(cutoff.timestamp())
    total = 0
    for month in months(base, store):
        first, after = _month_bounds(month)
        if first >= edge:
            break
        path = _path(base, store, month)
        if after <= edge:
            with _open(path, store) as db:
                total += _rows(db, store) if db else 0
            if not dry_run:
                _remove(path)
            continue
        with _open(path, store) as db:
            if db is not None:
                total += _older(db, store, edge, dry_run)
    return total


def _delete_store(base: Path | None, store: str, key: int) -> int:
    """Delete one tenant key's rows from every file of one store. Returns the rows deleted."""
    deleted = 0
    tables = (TABLE[store], *((COUNTS[store],) if store in COUNTS else ()))
    for month in months(base, store):
        path = _path(base, store, month)
        with _open(path, store) as db:
            if db is None:
                continue
            db.execute("PRAGMA secure_delete = ON")
            with transaction(db, immediate=True):
                deleted += db.execute(f"DELETE FROM {TABLE[store]} WHERE tenant = ?", (key,)).rowcount
                for table in tables[1:]:
                    db.execute(f"DELETE FROM {table} WHERE tenant = ?", (key,))
                db.execute("DELETE FROM label WHERE tenant = ?", (key,))
            # The deleted rows' old pages are in the file until the WAL is written back, even while the
            # worker holds the file open.
            db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            empty = not any(db.execute(f"SELECT 1 FROM {t} LIMIT 1").fetchone() for t in tables)
        if empty:
            _remove(path)
    return deleted


def delete_tenant(base: Path | None, key: int) -> int:
    """Delete every event of one tenant key from every file. Returns the rows deleted."""
    return sum(_delete_store(base, store, key) for store in STORES)


def delete_activity(conn, tenant_id: int) -> int:
    """Delete what the audit log add-on collected for a tenant (its records and counts) and forget how far its
    feed was read. Returns the records deleted."""
    with transaction(conn, immediate=True):
        conn.execute("DELETE FROM event_cursors WHERE tenant_id = ? AND source LIKE 'activity:%'", (tenant_id,))
    where = _where(conn, tenant_id)
    if where is None:
        return 0
    base, key = where
    return _delete_store(base, "activity", key)


def sweep(conn) -> int:
    """Delete rows whose tenant key is no longer in ``event_tenants`` (a delete that stopped part-way)."""
    base = root(conn)
    known = {r[0] for r in conn.execute("SELECT id FROM event_tenants")}
    found: set[int] = set()
    for store in STORES:
        for month in months(base, store):
            with _open(_path(base, store, month), store) as db:
                if db is not None:
                    found |= {r[0] for r in db.execute("SELECT DISTINCT tenant FROM label")}
    return sum(delete_tenant(base, key) for key in sorted(found - known))


def counts(conn, tenant_id: int) -> dict[str, int]:
    """How many rows each store holds for a tenant."""
    where = _where(conn, tenant_id)
    out = dict.fromkeys(STORES, 0)
    if where is None:
        return out
    base, key = where
    for store in STORES:
        for month in months(base, store):
            with _open(_path(base, store, month), store) as db:
                if db is not None:
                    out[store] += db.execute(
                        f"SELECT COUNT(*) FROM {TABLE[store]} WHERE tenant = ?", (key,)
                    ).fetchone()[0]
    return out


def first_day(conn, tenant_id: int) -> date | None:
    """The first day the tenant has any stored event."""
    where = _where(conn, tenant_id)
    if where is None:
        return None
    base, key = where
    days = []
    for store in ("signins", "audits"):
        for month in months(base, store):
            with _open(_path(base, store, month), store) as db:
                row = (
                    db.execute(f"SELECT MIN(ts) FROM {TABLE[store]} WHERE tenant = ?", (key,)).fetchone()
                    if db
                    else None
                )
            if row and row[0] is not None:
                days.append(datetime.fromtimestamp(row[0], UTC).date())
                break
    return min(days) if days else None
