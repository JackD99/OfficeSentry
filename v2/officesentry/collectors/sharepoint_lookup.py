"""Site and OneDrive lookups shared by the two SharePoint collectors.

``shared_files`` and ``sharepoint_storage`` both list every site and ask for
each licensed person's OneDrive. They run one after the other each night, so
the second reuses what the first read: each stores its lookups (``lookup_sites``
and ``lookup_onedrive`` objects, with the fields asked for) and the other
reads them back when that run finished within ``REUSE_HOURS`` and asked for
every field it needs. Both ask for ``SITE_FIELDS`` and ``DRIVE_FIELDS``, the
fields either one needs, so the second collector each night asks Microsoft for
no site list and no OneDrive at all. People with no OneDrive (Microsoft answers 404 until
they first open it) aren't asked again for ``NO_DRIVE_DAYS``, by either
collector. Everything here is GET only.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from .. import store
from ..graph import GraphError
from ..text import plural
from .base import RunContext

COLLECTORS = ("shared_files", "sharepoint_storage")
REUSE_HOURS = 6
NO_DRIVE_DAYS = 7
ISO = "%Y-%m-%dT%H:%M:%SZ"
# What both collectors ask for each site and OneDrive: shared_files needs root, name, driveType and owner,
# sharepoint_storage needs quota. Asking for the union costs nothing extra and lets every lookup be reused.
SITE_FIELDS = "id,displayName,webUrl,isPersonalSite,root"
DRIVE_FIELDS = "id,name,webUrl,driveType,owner,quota"


def _covers(stored: str | None, needed: str) -> bool:
    return set(needed.split(",")) <= set((stored or "").split(","))


class Lookups:
    """One run's site listing and OneDrive lookups, seeded from the SharePoint collectors' last runs.

    ``site_fields`` and ``drive_fields`` are the ``$select`` this collector asks
    Microsoft for; a stored lookup is reused only if it has all of them.
    """

    def __init__(
        self,
        ctx: RunContext,
        collector: str,
        site_fields: str = SITE_FIELDS,
        drive_fields: str = DRIVE_FIELDS,
        now: datetime | None = None,
    ):
        self.ctx = ctx
        self.site_fields, self.drive_fields = site_fields, drive_fields
        self.now = now or datetime.now(UTC)
        self._sites: list[dict] | None = None
        self._sites_complete = False
        self._drives: dict[str, dict] = {}  # user id -> {"drive": dict | None, "fields": str, "checked": iso}
        self._known: dict[str, dict] = {}
        self.reused_sites = False
        self.reused_drives = 0
        recent = (self.now - timedelta(hours=REUSE_HOURS)).strftime(ISO)
        no_drive_since = (self.now - timedelta(days=NO_DRIVE_DAYS)).strftime(ISO)
        for key in COLLECTORS:
            run = store.latest_run(ctx.conn, ctx.tenant["id"], key)
            if run is None or not run["finished_at"]:
                continue
            fresh = run["finished_at"] >= recent and key != collector
            if fresh and self._sites is None:
                rows = store.run_objects(ctx.conn, run["id"], "lookup_sites")
                if rows and rows[0].get("complete") and _covers(rows[0].get("fields"), site_fields):
                    self._sites, self._sites_complete, self.reused_sites = rows[0]["sites"], True, True
            for row in store.run_objects(ctx.conn, run["id"], "lookup_onedrive"):
                usable = (row.get("drive") is None and row["checked"] >= no_drive_since) or (
                    fresh and row.get("drive") and _covers(row.get("fields"), drive_fields)
                )
                current = self._known.get(row["_id"])
                if usable and (current is None or row["checked"] > current["checked"]):
                    self._known[row["_id"]] = {k: row.get(k) for k in ("drive", "fields", "checked")}

    def sites(self) -> list[dict]:
        """Every site (``/sites/getAllSites``). PermissionMissing propagates to the collector."""
        if self._sites is None:
            result = self.ctx.graph.get_all("/sites/getAllSites", {"$select": self.site_fields})
            self._sites = self.ctx.paged(result)
            self._sites_complete = not result.truncated
        return self._sites

    def onedrive(self, user: dict) -> dict | None:
        """A licensed person's OneDrive, or None if they have none (or it couldn't be read)."""
        uid = user["_id"]
        if uid not in self._drives:
            known = self._known.get(uid)
            if known is not None:
                self._drives[uid] = known
                self.reused_drives += 1
            else:
                try:
                    drive = self.ctx.graph.get(f"/users/{uid}/drive", {"$select": self.drive_fields})
                except GraphError as exc:
                    if exc.status != 404:
                        return None  # not "has none": don't remember it, ask again next time
                    drive = None
                self._drives[uid] = {"drive": drive, "fields": self.drive_fields, "checked": self.now.strftime(ISO)}
        return self._drives[uid]["drive"]

    def save(self) -> None:
        """Store this run's lookups for the other collector, and "has none" for the nights after."""
        if self._sites is not None:
            fields = self.site_fields
            self.ctx.objects(
                "lookup_sites", [("sites", {"sites": self._sites, "fields": fields, "complete": self._sites_complete})]
            )
        self.ctx.objects("lookup_onedrive", list(self._drives.items()))
        reused = (["the site list"] if self.reused_sites else []) + (
            [plural(self.reused_drives, "OneDrive lookup")] if self.reused_drives else []
        )
        if reused:
            self.ctx.log(f"Reused {' and '.join(reused)} from earlier SharePoint collections")
