"""Data access for tenants, connections, runs and snapshots."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from .db import transaction, utcnow

# -- App connections ---------------------------------------------------------


def create_connection(conn, vault, name: str, cert, client_id: str | None = None,
                      multi_tenant: bool = True, cloud: str = "commercial") -> int:
    """An app registration Office Sentry signs in as, in one Microsoft cloud (``clouds.py``)."""
    cur = conn.execute(
        "INSERT INTO app_connections (name, client_id, multi_tenant, cert_pem, key_enc, thumbprint,"
        " not_after, created_at, cloud) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (name, client_id, int(multi_tenant), cert.cert_pem, vault.encrypt(cert.key_pem),
         cert.thumbprint, cert.not_after.strftime("%Y-%m-%dT%H:%M:%SZ"), utcnow(), cloud),
    )
    return cur.lastrowid


def get_connection(conn, connection_id: int):
    return conn.execute("SELECT * FROM app_connections WHERE id = ?", (connection_id,)).fetchone()


def list_connections(conn):
    return conn.execute("SELECT * FROM app_connections ORDER BY id").fetchall()


# -- Tenants -----------------------------------------------------------------


def create_tenant(conn, name: str, tenant_ref: str, connection_id: int | None,
                  cloud: str = "commercial") -> int:
    cur = conn.execute(
        "INSERT INTO tenants (name, tenant_ref, connection_id, created_at, cloud) VALUES (?, ?, ?, ?, ?)",
        (name, tenant_ref, connection_id, utcnow(), cloud),
    )
    return cur.lastrowid


def get_tenant(conn, tenant_id: int):
    return conn.execute("SELECT * FROM tenants WHERE id = ?", (tenant_id,)).fetchone()


def tenant_capabilities(tenant) -> dict:
    return json.loads(tenant["capabilities"] or "{}")


def set_capabilities(conn, tenant_id: int, caps: dict) -> None:
    conn.execute("UPDATE tenants SET capabilities = ? WHERE id = ?",
                 (json.dumps(caps, sort_keys=True), tenant_id))


# -- Runs --------------------------------------------------------------------


# Queue priorities: someone is waiting for these (1), or nobody is (0).
PRIORITY_WAITING = 1
PRIORITY_NIGHTLY = 0


def enqueue_run(conn, tenant_id: int, collector: str, trigger: str = "manual",
                requested_by: str | None = None, scheduled_for: str | None = None,
                priority: int | None = None) -> int:
    """Queue a run unless one for the same tenant+collector is already pending.

    Manual runs default to ``PRIORITY_WAITING``, so "Run now" goes ahead of the
    nightly backlog; asking again for a run already queued raises its priority.
    """
    if priority is None:
        priority = PRIORITY_WAITING if trigger == "manual" else PRIORITY_NIGHTLY
    with transaction(conn, immediate=True):
        existing = conn.execute(
            "SELECT id, status, priority FROM runs WHERE tenant_id = ? AND collector = ?"
            " AND status IN ('queued', 'running')",
            (tenant_id, collector),
        ).fetchone()
        if existing:
            if existing["status"] == "queued" and existing["priority"] < priority:
                conn.execute("UPDATE runs SET priority = ? WHERE id = ?", (priority, existing["id"]))
            return existing["id"]
        cur = conn.execute(
            "INSERT INTO runs (tenant_id, collector, status, trigger, requested_by, scheduled_for,"
            " created_at, priority) VALUES (?, ?, 'queued', ?, ?, ?, ?, ?)",
            (tenant_id, collector, trigger, requested_by, scheduled_for, utcnow(), priority),
        )
        return cur.lastrowid


def claim_next_run(conn):
    """Atomically move the next queued run to 'running' and return it.

    Highest priority first, then oldest. A tenant runs one collector at a time,
    so parallel workers spread over tenants (Microsoft throttles per tenant)
    and a tenant's findings are synced by one run at a time.

    Runs of paused or archived tenants stay queued (pausing removes them, so
    only a run queued in the same moment can be left) and are never claimed.
    """
    with transaction(conn, immediate=True):
        # Nearly every run has finished, so the planner's statistics make status look unselective;
        # INDEXED BY keeps this to the few queued and running rows rather than every run.
        row = conn.execute(
            "SELECT r.id FROM runs r INDEXED BY idx_runs_status CROSS JOIN tenants t ON t.id = r.tenant_id"
            " WHERE r.status = 'queued' AND t.enabled = 1 AND t.archived_at IS NULL"
            " AND r.tenant_id NOT IN (SELECT tenant_id FROM runs INDEXED BY idx_runs_status WHERE status = 'running')"
            " ORDER BY r.priority DESC, r.created_at, r.id LIMIT 1"
        ).fetchone()
        if not row:
            return None
        conn.execute("UPDATE runs SET status = 'running', started_at = ? WHERE id = ?",
                     (utcnow(), row["id"]))
        return conn.execute("SELECT * FROM runs WHERE id = ?", (row["id"],)).fetchone()


def finish_run(conn, run_id: int, status: str, error: str | None = None) -> None:
    conn.execute(
        "UPDATE runs SET status = ?, error = ?, finished_at = ?,"
        " object_count = (SELECT COUNT(*) FROM objects WHERE run_id = ?) WHERE id = ?",
        (status, error, utcnow(), run_id, run_id),
    )


def recover_orphaned_runs(conn) -> int:
    """Runs left 'running' by a crashed or restarted worker are marked failed."""
    cur = conn.execute(
        "UPDATE runs SET status = 'failed', finished_at = ?, error = 'worker stopped before the run finished'"
        " WHERE status = 'running'",
        (utcnow(),),
    )
    return cur.rowcount


def log_event(conn, run_id: int, level: str, message: str) -> None:
    conn.execute("INSERT INTO run_events (run_id, ts, level, message) VALUES (?, ?, ?, ?)",
                 (run_id, utcnow(), level, message[:2000]))


def latest_run(conn, tenant_id: int, collector: str, successful: bool = True):
    statuses = ("succeeded", "partial") if successful else ("succeeded", "partial", "failed")
    return conn.execute(
        f"SELECT * FROM runs WHERE tenant_id = ? AND collector = ? AND status IN ({','.join('?' * len(statuses))})"
        " ORDER BY finished_at DESC, id DESC LIMIT 1",
        (tenant_id, collector, *statuses),
    ).fetchone()


# -- Snapshot data -----------------------------------------------------------


def add_objects(conn, run_id: int, kind: str, items: list[tuple[str, dict]]) -> None:
    conn.executemany(
        "INSERT OR REPLACE INTO objects (run_id, kind, object_id, data) VALUES (?, ?, ?, ?)",
        [(run_id, kind, str(obj_id), json.dumps(data, sort_keys=True)) for obj_id, data in items],
    )


def run_objects(conn, run_id: int, kind: str | None = None, limit: int | None = None) -> list[dict]:
    sql = "SELECT kind, object_id, data FROM objects WHERE run_id = ?"
    args: list = [run_id]
    if kind:
        sql += " AND kind = ?"
        args.append(kind)
    sql += " ORDER BY kind, object_id"
    if limit:
        sql += " LIMIT ?"
        args.append(limit)
    return [dict(json.loads(r["data"]), _kind=r["kind"], _id=r["object_id"])
            for r in conn.execute(sql, args)]


def add_metric(conn, run_id: int, tenant_id: int, key: str, value: float) -> None:
    conn.execute(
        "INSERT INTO metrics (run_id, tenant_id, key, value, captured_at) VALUES (?, ?, ?, ?, ?)",
        (run_id, tenant_id, key, float(value), utcnow()),
    )


def metric_history(conn, tenant_id: int, key: str, since: str | None = None) -> list[sqlite3.Row]:
    sql = "SELECT value, captured_at FROM metrics WHERE tenant_id = ? AND key = ?"
    args: list = [tenant_id, key]
    if since:
        sql += " AND captured_at >= ?"
        args.append(since)
    return conn.execute(sql + " ORDER BY captured_at", args).fetchall()


def snapshots_to_keep(conn, keep_daily: int = 30, keep_monthly: int = 12, tz: str = "UTC") -> set[int]:
    """Run IDs whose objects the retention policy keeps.

    Per tenant and collector, among successful runs: the last run of each of
    the ``keep_daily`` most recent days with a run, and the last run of each
    of the ``keep_monthly`` most recent months. The latest run is always one
    of those. Days and months are in the portal's timezone.
    """
    zone = ZoneInfo(tz)
    keep: set[int] = set()
    days: dict[tuple, list] = {}
    months: dict[tuple, list] = {}
    # Runs already pruned can't be kept again, so they're skipped: after a year
    # nearly every run has been pruned and reading them all took seconds.
    rows = conn.execute(
        "SELECT id, tenant_id, collector, finished_at FROM runs"
        " WHERE status IN ('succeeded', 'partial') AND finished_at IS NOT NULL"
        " AND EXISTS (SELECT 1 FROM objects o WHERE o.run_id = runs.id)"
        " ORDER BY tenant_id, collector, finished_at DESC, id DESC"
    )
    for r in rows:
        group = (r["tenant_id"], r["collector"])
        local = datetime.fromisoformat(r["finished_at"].replace("Z", "+00:00")).astimezone(zone)
        # Rows arrive newest first, so the first run seen in a day/month is its last.
        for buckets, label, limit in ((days, local.date(), keep_daily),
                                      (months, (local.year, local.month), keep_monthly)):
            seen = buckets.setdefault(group, [])
            if len(seen) < limit and (not seen or seen[-1] != label):
                seen.append(label)
                keep.add(r["id"])
    return keep


def snapshots_to_prune(conn, keep_daily: int = 30, keep_monthly: int = 12, tz: str = "UTC",
                       grace_hours: int = 24) -> list[int]:
    """Run IDs whose objects ``prune_snapshots`` would delete now (also the settings page's dry run)."""
    keep = snapshots_to_keep(conn, keep_daily, keep_monthly, tz)
    cutoff = (datetime.now(UTC) - timedelta(hours=grace_hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return [r["id"] for r in conn.execute(
        "SELECT id FROM runs WHERE status IN ('succeeded', 'partial', 'failed') AND finished_at < ?"
        " AND EXISTS (SELECT 1 FROM objects o WHERE o.run_id = runs.id) ORDER BY id",
        (cutoff,),
    ) if r["id"] not in keep]


def prune_snapshots(conn, keep_daily: int = 30, keep_monthly: int = 12, tz: str = "UTC",
                    grace_hours: int = 24, batch: int = 200) -> int:
    """Delete the objects of runs the retention policy doesn't keep. Returns runs pruned.

    Metrics and the run rows are never pruned (run logs are, see
    ``prune_run_logs``; findings and sign-in detail follow ``retention``). Runs that
    finished within ``grace_hours`` are left alone, so today's extra runs stay
    inspectable until tomorrow. Deletes go in small batches so the portal
    stays responsive.
    """
    candidates = snapshots_to_prune(conn, keep_daily, keep_monthly, tz, grace_hours)
    for i in range(0, len(candidates), batch):
        chunk = candidates[i:i + batch]
        with transaction(conn, immediate=True):
            conn.execute(f"DELETE FROM objects WHERE run_id IN ({','.join('?' * len(chunk))})", chunk)
    return len(candidates)



# Run log lines older than this are deleted, except warnings and errors.
RUN_LOG_DAYS = 90
RUN_LOG_WATERMARK = "run_log_pruned_to"


def run_logs_to_prune(conn, days: int = RUN_LOG_DAYS, now: datetime | None = None) -> int:
    """How many run log lines ``prune_run_logs`` would delete now (the settings page's dry run)."""
    cutoff = ((now or datetime.now(UTC)) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    mark = conn.execute("SELECT value FROM settings WHERE key = ?", (RUN_LOG_WATERMARK,)).fetchone()
    start = int(mark["value"]) if mark else 0
    row = conn.execute("SELECT MAX(id) AS id FROM runs WHERE id > ? AND created_at < ?", (start, cutoff)).fetchone()
    last = row["id"] if row and row["id"] else start
    return conn.execute("SELECT COUNT(*) FROM run_events WHERE run_id > ? AND run_id <= ? AND level = 'info'",
                        (start, last)).fetchone()[0]


def prune_run_logs(conn, days: int = RUN_LOG_DAYS, batch: int = 500, now: datetime | None = None) -> int:
    """Delete the info lines of run logs older than ``days``. Returns lines deleted.

    Warnings and errors stay, so a run's page still says why it failed or was
    partial. Works through runs in id order from where the last prune stopped
    (kept in settings), in small batches so the portal stays responsive.
    """
    cutoff = ((now or datetime.now(UTC)) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    mark = conn.execute("SELECT value FROM settings WHERE key = ?", (RUN_LOG_WATERMARK,)).fetchone()
    start = int(mark["value"]) if mark else 0
    # Run ids grow with created_at, so only the runs since the last prune are read.
    row = conn.execute("SELECT MAX(id) AS id FROM runs WHERE id > ? AND created_at < ?", (start, cutoff)).fetchone()
    last = row["id"] if row and row["id"] else start
    deleted = 0
    while start < last:
        end = min(start + batch, last)
        with transaction(conn, immediate=True):
            deleted += conn.execute("DELETE FROM run_events WHERE run_id > ? AND run_id <= ? AND level = 'info'",
                                    (start, end)).rowcount
            conn.execute("INSERT INTO settings (key, value) VALUES (?, ?)"
                         " ON CONFLICT(key) DO UPDATE SET value = excluded.value", (RUN_LOG_WATERMARK, str(end)))
        start = end
    return deleted
