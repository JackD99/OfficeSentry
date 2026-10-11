"""Precomputed cross-tenant rows: each tenant's line on What changed (``/changes``) and on the
check matrix (``/across/checks``).

Both pages show one row per tenant, and working a row out is the slow part: a tenant's change
summary builds every detail report twice, and its check-matrix row evaluates every check. At 200
tenants What changed took about 30 seconds. So each tenant's rows are kept in ``summary_cache``,
one per kind (``changes:7``, ``changes:30``, ``changes:90`` and ``checks``). The worker builds
them after the tenant's collection (``queue``) and once a day (``queue_all``), as ``precompute``
tasks, and the pages read them.

A stored row is used only while its fingerprint matches what the row would be built from now:

* every kind: the code, the day (checks and ages move with time), the tenant's latest finished
  run, licence flags and cloud, licence prices, currency and Office Sentry's own app IDs;
* ``changes:<days>``: the tenant's latest run finished by the start of the period, the data the
  period is compared with;
* ``checks``: the tenant's open findings and which of them are accepted risks now.

When it doesn't match, the page builds that tenant's row as before and stores it. New and fixed
findings and the key figures on What changed are always read live: they're cheap, and they move
as findings sync. The daily retention pass can remove a period's starting data, so after it
removes anything every row is built again.

Rows are per tenant, never per person: pages read them only for the tenants ``scoped_tenants()``
gives the person (and the group they picked), so a stored row never shows anyone a tenant they
couldn't open.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime

from .. import tasks
from ..db import utcnow
from .monthly_review import _code_version

log = logging.getLogger(__name__)

KIND = "precompute"  # the task kind
PRIORITY = tasks.PRIORITY_BACKGROUND - 1  # after approvals and emails, which people wait for
ISO = "%Y-%m-%dT%H:%M:%SZ"


def _last_run(conn, tenant_id: int, before: str | None = None) -> list:
    """The tenant's latest finished run (by ``before``, when given): one lookup on idx_runs_finished."""
    cutoff = " AND finished_at <= ?" if before else ""
    row = conn.execute(
        f"SELECT finished_at, id FROM runs WHERE tenant_id = ? AND finished_at IS NOT NULL{cutoff}"
        " ORDER BY finished_at DESC, id DESC LIMIT 1",
        (tenant_id, *((before,) if before else ())),
    ).fetchone()
    return list(row or ())


def _open_findings(conn, tenant_id: int, now: datetime) -> list:
    """The open findings and the accepted risks among them, as ``lifecycle_map`` sees them at ``now``."""
    row = conn.execute(
        "SELECT COUNT(*), MAX(first_seen), MAX(last_seen), MAX(reopened_at), MAX(accepted_at), MAX(accepted_until),"
        " TOTAL(accepted_at IS NOT NULL AND (accepted_until IS NULL OR accepted_until > ?))"
        " FROM findings WHERE tenant_id = ? AND resolved_at IS NULL",
        (now.strftime(ISO), tenant_id),
    ).fetchone()
    return list(row)


class Rows:
    """One kind's stored rows for a list of tenants, read in one query.

    ``fingerprint(tenant, since)`` is what the tenant's row would be built from now; ``get`` returns
    the stored data only when it was built from the same, and ``save`` stores a row just built.
    ``rebuild`` ignores what's stored (the daily refresh after retention removed something).
    """

    def __init__(self, conn, kind: str, tenants, now: datetime, rebuild: bool = False):
        self.conn, self.kind, self.now = conn, kind, now
        prices = conn.execute("SELECT COUNT(*), MAX(updated_at), TOTAL(monthly_price) FROM sku_prices").fetchone()
        apps = conn.execute("SELECT GROUP_CONCAT(client_id) FROM app_connections").fetchone()[0]
        settings = conn.execute(
            "SELECT key, value FROM settings WHERE key IN ('currency', 'live_test_client_id') ORDER BY key"
        ).fetchall()
        self.shared = [_code_version(), now.date().isoformat(), list(prices), apps, [list(r) for r in settings]]
        ids = [t["id"] for t in tenants]
        self.stored: dict[int, tuple[str, str]] = {}
        if ids and not rebuild:
            rows = conn.execute(
                f"SELECT tenant_id, fingerprint, data FROM summary_cache WHERE kind = ?"
                f" AND tenant_id IN ({','.join('?' * len(ids))})",
                (kind, *ids),
            )
            self.stored = {r["tenant_id"]: (r["fingerprint"], r["data"]) for r in rows}

    def fingerprint(self, tenant, since: datetime | None = None) -> str:
        parts = [*self.shared, tenant["capabilities"], tenant["cloud"], _last_run(self.conn, tenant["id"])]
        if since is not None:
            parts.append(_last_run(self.conn, tenant["id"], since.strftime(ISO)))
        if self.kind == "checks":
            parts.append(_open_findings(self.conn, tenant["id"], self.now))
        return json.dumps(parts)

    def get(self, tenant, fingerprint: str):
        hit = self.stored.get(tenant["id"])
        return json.loads(hit[1]) if hit and hit[0] == fingerprint else None

    def save(self, tenant, fingerprint: str, data) -> None:
        """Store a row just built. A busy database only costs the next page view a rebuild."""
        try:
            self.conn.execute(
                "INSERT INTO summary_cache (tenant_id, kind, fingerprint, data, built_at) VALUES (?, ?, ?, ?, ?)"
                " ON CONFLICT(tenant_id, kind) DO UPDATE SET fingerprint = excluded.fingerprint,"
                " data = excluded.data, built_at = excluded.built_at",
                (tenant["id"], self.kind, fingerprint, json.dumps(data), utcnow()),
            )
        except sqlite3.OperationalError:
            log.warning("Couldn't store the %s row for tenant %s", self.kind, tenant["id"], exc_info=True)


# -- Worker ------------------------------------------------------------------------------------


def refresh(conn, tenant, rebuild: bool = False) -> None:
    """Build the tenant's rows that are out of date (every row with ``rebuild``)."""
    from . import month_changes, rollups  # they import this module

    for days in month_changes.PERIODS:
        month_changes.across(conn, [tenant], days, rebuild=rebuild)
    rollups.check_matrix(conn, [tenant], rebuild=rebuild)


def queue(conn, tenant_id: int, rebuild: bool = False) -> int:
    """Queue a refresh of one tenant's rows (once: a refresh already queued is reused, and made a rebuild if
    this one is). A rebuild asked for while the tenant's refresh is already running is queued behind it under
    its own key, since the running one read its payload when it started and would finish without rebuilding."""
    key = f"{KIND}:{tenant_id}"
    if rebuild and (running := tasks.pending(conn, key)) and running["status"] == "running":
        key = f"{key}:again"
    task_id = tasks.enqueue(
        conn,
        KIND,
        key=key,
        tenant_id=tenant_id,
        priority=PRIORITY,
        payload={"rebuild": True} if rebuild else None,
    )
    if rebuild:
        conn.execute(
            "UPDATE tasks SET payload = ? WHERE id = ? AND status = 'queued'", (json.dumps({"rebuild": True}), task_id)
        )
    return task_id


def queue_all(conn, rebuild: bool = False) -> int:
    """Queue a refresh for every tenant that isn't archived (archived tenants are on no list)."""
    ids = [r["id"] for r in conn.execute("SELECT id FROM tenants WHERE archived_at IS NULL ORDER BY id")]
    for tenant_id in ids:
        queue(conn, tenant_id, rebuild)
    return len(ids)


@tasks.handler(KIND)
def _refresh_task(ctx: tasks.TaskContext) -> None:
    tenant = ctx.tenant
    if tenant is not None and tenant["archived_at"] is None:
        refresh(ctx.conn, tenant, rebuild=bool(ctx.payload.get("rebuild")))
