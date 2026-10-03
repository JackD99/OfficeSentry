"""Read access to a tenant's latest collected data, for rules and reports."""

from __future__ import annotations

from datetime import datetime
from functools import cached_property

from . import store


class TenantSnapshot:
    """The latest successful run of each collector for one tenant.

    ``objects(collector, kind)`` returns ``None`` when the collector has never
    run successfully, so callers can tell "no data" from "nothing found".
    """

    def __init__(self, conn, tenant, as_of: datetime | None = None):
        self.conn = conn
        self.tenant = tenant
        # When set, the snapshot is the data as it stood then: runs finished and metrics captured by that time.
        self.as_of = as_of.strftime("%Y-%m-%dT%H:%M:%SZ") if as_of else None
        self._objects: dict[tuple[str, str], list[dict]] = {}

    @cached_property
    def runs(self) -> dict:
        # An older snapshot skips runs whose objects the retention policy has removed.
        cutoff = (" AND finished_at <= ? AND (object_count = 0 OR EXISTS"
                  " (SELECT 1 FROM objects o WHERE o.run_id = runs.id))") if self.as_of else ""
        rows = self.conn.execute(
            f"""
            SELECT r.* FROM runs r JOIN (
                SELECT collector, MAX(id) AS id FROM runs
                WHERE tenant_id = ? AND status IN ('succeeded', 'partial'){cutoff} GROUP BY collector
            ) latest ON latest.id = r.id
            """,
            (self.tenant["id"], *((self.as_of,) if self.as_of else ())),
        ).fetchall()
        return {r["collector"]: r for r in rows}

    @cached_property
    def capabilities(self) -> dict:
        return store.tenant_capabilities(self.tenant)

    @cached_property
    def own_app_ids(self) -> set[str]:
        """Client IDs of Office Sentry's own apps, so checks don't flag them.

        Every configured app connection counts (a tenant may not be linked to
        one yet), plus the app ``live-test`` signed in with.
        """
        rows = self.conn.execute("SELECT client_id FROM app_connections WHERE client_id IS NOT NULL").fetchall()
        ids = {r["client_id"].lower() for r in rows}
        live = self.conn.execute("SELECT value FROM settings WHERE key = 'live_test_client_id'").fetchone()
        if live:
            ids.add(live["value"].lower())
        return ids

    def has(self, collector: str) -> bool:
        return collector in self.runs

    def objects(self, collector: str, kind: str) -> list[dict] | None:
        run = self.runs.get(collector)
        if run is None:
            return None
        key = (collector, kind)
        if key not in self._objects:
            self._objects[key] = store.run_objects(self.conn, run["id"], kind)
        return self._objects[key]

    def metric(self, key: str) -> float | None:
        cutoff = " AND captured_at <= ?" if self.as_of else ""
        row = self.conn.execute(
            f"SELECT value FROM metrics WHERE tenant_id = ? AND key = ?{cutoff}"
            " ORDER BY captured_at DESC, rowid DESC LIMIT 1",
            (self.tenant["id"], key, *((self.as_of,) if self.as_of else ())),
        ).fetchone()
        return row["value"] if row else None

    def history(self, key: str, since: str | None = None) -> list[tuple[str, float]]:
        rows = store.metric_history(self.conn, self.tenant["id"], key, since)
        return [(r["captured_at"], r["value"]) for r in rows if not self.as_of or r["captured_at"] <= self.as_of]
