"""SharePoint storage: what fills a tenant's quota and what can be cleaned up.

Walks every document library on every SharePoint site (Sites.Read.All) and
reads SharePoint's own storage metrics for each file and folder:

* ``SMTotalSize``: the item with all its versions (a folder: everything in it)
* ``SMTotalFileStreamSize``: the current version only
* ``SMTotalFileCount``: files in a folder, at any depth

so older versions are ``SMTotalSize - SMTotalFileStreamSize``. Each site's
default drive gives its quota and recycle bin size. OneDrives don't count
towards the tenant's SharePoint storage; they're only walked when nearly full.

Only totals per site and library and the largest few hundred files, folders
and recycle bin items are stored. Everything here is GET only.
"""

from __future__ import annotations

import heapq
import itertools
from datetime import UTC, datetime, timedelta
from urllib.parse import unquote, urlparse

from ..graph import GraphError, PermissionMissing
from ..snapshot import TenantSnapshot
from .base import Collector, RunContext, register

ITEM_FIELDS = ("FileLeafRef,FileRef,FSObjType,SMTotalSize,SMTotalFileStreamSize,SMTotalFileCount,"
               "_UIVersionString")
ITEM_PARAMS = {"$top": "999", "$select": "id,webUrl,lastModifiedDateTime,lastModifiedBy",
               "$expand": f"fields($select={ITEM_FIELDS})"}
LIST_PARAMS = {"$select": "id,name,displayName,webUrl,list,system,lastModifiedDateTime"}
# Libraries hold files; other lists (calendars, task lists, galleries) are small.
LIBRARY_TEMPLATES = {"documentLibrary", "mySiteDocumentLibrary", "webPageLibrary", "pictureLibrary",
                     "formLibrary", "assetLibrary"}
PRESERVATION_HOLD = "PreservationHoldLibrary"
# A site with no storage limit of its own shows 25 TB, SharePoint's ceiling.
NO_SITE_LIMIT = 25 * 1024 ** 4

STALE_DAYS = 730                  # "old" files: not changed in two years
NEARLY_FULL = 0.8                 # OneDrives this full get walked for cleanup detail
TOP_FILES = 200                   # per list of files kept (largest, most versions, oldest)
TOP_FOLDERS = 150
MAX_SITES = 3000
MAX_PAGES_PER_LIBRARY = 200       # 200 pages of 999 items: about 200,000 items per library
MAX_PAGES = 6000                  # per run, so one huge tenant can't hold the worker for a day
RECYCLE_SITES = 25                # sites whose recycle bins are listed item by item
RECYCLE_PAGES = 20
RECYCLE_TOP = 25                  # items kept per site


def _int(value) -> int:
    try:
        return int(float(value or 0))
    except (TypeError, ValueError):
        return 0


def version_number(value) -> int:
    """``_UIVersionString`` "512.0" -> 512. Minor (draft) versions count with their major."""
    return _int(str(value or "0").split(".", 1)[0])


def _who(identity: dict | None) -> str | None:
    user = (identity or {}).get("user") or {}
    return user.get("email") or user.get("displayName") or None


def is_library(lst: dict) -> bool:
    template = (lst.get("list") or {}).get("template") or ""
    return template in LIBRARY_TEMPLATES or lst.get("name") == PRESERVATION_HOLD


def quota_row(drive: dict | None) -> dict:
    q = (drive or {}).get("quota") or {}
    total = _int(q.get("total"))
    return {"used": _int(q.get("used")), "recycleBin": _int(q.get("deleted")),
            "limit": total if 0 < total < NO_SITE_LIMIT else None, "state": q.get("state")}


class _Top:
    """The ``n`` largest rows by a key, without holding every row."""

    def __init__(self, n: int):
        self.n, self.heap, self.tie = n, [], itertools.count()

    def add(self, value: int, row: dict) -> None:
        if value <= 0:
            return
        entry = (value, next(self.tie), row)
        if len(self.heap) < self.n:
            heapq.heappush(self.heap, entry)
        elif value > self.heap[0][0]:
            heapq.heapreplace(self.heap, entry)

    def rows(self) -> list[dict]:
        return [row for _, _, row in sorted(self.heap, key=lambda e: (-e[0], e[1]))]


class _Scan:
    """Running totals and top lists across every library walked in one run."""

    def __init__(self, now: datetime):
        self.stale_before = (now - timedelta(days=STALE_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.largest, self.versions, self.stale = _Top(TOP_FILES), _Top(TOP_FILES), _Top(TOP_FILES)
        self.folders = _Top(TOP_FOLDERS)
        self.onedrive = _Top(TOP_FILES)  # OneDrive files are kept apart: they don't use the tenant's storage
        self.pages = 0

    @property
    def out_of_pages(self) -> bool:
        return self.pages >= MAX_PAGES

    def library(self, ctx: RunContext, path: str, lst: dict, where: dict) -> dict | None:
        """Walk one library's items. Returns its totals, or None if it couldn't be read."""
        try:
            result = ctx.graph.get_all(f"{path}/items", ITEM_PARAMS,
                                       max_pages=min(MAX_PAGES_PER_LIBRARY, MAX_PAGES - self.pages))
        except GraphError as exc:
            ctx.log(f"Couldn't read {lst.get('webUrl') or lst.get('id')}: {exc.message}", "warning")
            return None
        self.pages += result.pages
        root = unquote(urlparse(lst.get("webUrl") or "").path).rstrip("/")
        lib = {**where, "library": lst.get("displayName") or lst.get("name"), "webUrl": lst.get("webUrl"),
               "preservationHold": lst.get("name") == PRESERVATION_HOLD, "files": 0, "folders": 0, "total": 0,
               "current": 0, "staleFiles": 0, "staleBytes": 0, "maxVersion": 0, "lastModified": None,
               "complete": not result.truncated}
        for item in result.items:
            f = item.get("fields") or {}
            ref = f.get("FileRef") or ""
            rel = ref[len(root):].lstrip("/") if root and ref.startswith(root) else (f.get("FileLeafRef") or "")
            total = _int(f.get("SMTotalSize"))
            modified = item.get("lastModifiedDateTime")
            base = {**where, "library": lib["library"], "name": f.get("FileLeafRef"), "path": rel,
                    "webUrl": item.get("webUrl"), "total": total, "lastModified": modified,
                    "modifiedBy": _who(item.get("lastModifiedBy"))}
            if str(f.get("FSObjType")) == "1":
                lib["folders"] += 1
                if where["personal"]:
                    continue
                self.folders.add(total, {**base, "files": _int(f.get("SMTotalFileCount")),
                                         "depth": rel.count("/") + 1})
                continue
            current = min(_int(f.get("SMTotalFileStreamSize")), total) if total else \
                _int(f.get("SMTotalFileStreamSize"))
            total = max(total, current)
            version = version_number(f.get("_UIVersionString"))
            row = {**base, "total": total, "current": current, "versions": total - current, "version": version}
            lib["files"] += 1
            lib["total"] += total
            lib["current"] += current
            lib["maxVersion"] = max(lib["maxVersion"], version)
            if modified and (lib["lastModified"] is None or modified > lib["lastModified"]):
                lib["lastModified"] = modified
            stale = bool(modified and modified < self.stale_before)
            if stale:
                lib["staleFiles"] += 1
                lib["staleBytes"] += total
            if where["personal"]:
                self.onedrive.add(total, row)
                continue
            self.largest.add(total, row)
            self.versions.add(total - current, row)
            if stale:
                self.stale.add(total, row)
        lib["versions"] = lib["total"] - lib["current"]
        if result.truncated:
            ctx.warn(f"Stopped reading {lst.get('webUrl')} after {result.pages} pages; its totals are partial")
            ctx.truncated = True
        return lib


@register
class SharePointStorage(Collector):
    key = "sharepoint_storage"
    title = "SharePoint storage"
    description = ("What fills each site's storage: libraries, folders, large and old files, version history and "
                   "recycle bins. Needs the Sites.Read.All permission.")
    permissions = ("Sites.Read.All",)

    def collect(self, ctx: RunContext) -> None:
        if ctx.capabilities.get("sharepoint") is False:
            ctx.log("Tenant has no SharePoint licence; nothing to scan")
            ctx.objects("storage_status", [("status", {"available": False, "reason": "no_sharepoint"})])
            return
        try:
            sites = ctx.paged(ctx.graph.get_all("/sites/getAllSites", {
                "$select": "id,displayName,webUrl,isPersonalSite"}))
        except PermissionMissing as exc:
            ctx.warn("Reading storage needs the Sites.Read.All permission. Re-send the consent link from the tenant "
                     f"page to add it. ({exc.message})")
            ctx.objects("storage_status", [("status", {"available": False, "reason": "permission"})])
            return

        scan = _Scan(datetime.now(UTC))
        team_sites = [s for s in sites if not s.get("isPersonalSite") and "-my.sharepoint." not in
                      (s.get("webUrl") or "")]
        site_rows, libraries, recycle_candidates, skipped = [], [], [], 0
        for site in team_sites[:MAX_SITES]:
            name = site.get("displayName") or site.get("webUrl")
            where = {"site": name, "siteUrl": site.get("webUrl"), "personal": False}
            try:
                drive = ctx.graph.get(f"/sites/{site['id']}/drive", {"$select": "id,quota"})
            except GraphError:
                drive = None  # some system sites have no default library
            try:
                lists = ctx.graph.get_all(f"/sites/{site['id']}/lists", LIST_PARAMS).items
            except GraphError as exc:
                skipped += 1
                ctx.log(f"Couldn't list libraries of {site.get('webUrl')}: {exc.message}", "warning")
                continue
            libs = []
            for lst in (x for x in lists if is_library(x)):
                if scan.out_of_pages:
                    break
                lib = scan.library(ctx, f"/sites/{site['id']}/lists/{lst['id']}", lst, where)
                if lib is not None:
                    libs.append(lib)
            libraries += libs
            q = quota_row(drive)
            row = {**where, "url": site.get("webUrl"), **q,
                   "files": sum(x["files"] for x in libs), "total": sum(x["total"] for x in libs),
                   "current": sum(x["current"] for x in libs if not x["preservationHold"]),
                   "versions": sum(x["versions"] for x in libs if not x["preservationHold"]),
                   "preservationHold": sum(x["total"] for x in libs if x["preservationHold"]),
                   "staleBytes": sum(x["staleBytes"] for x in libs),
                   "libraries": len(libs), "complete": all(x["complete"] for x in libs) and not scan.out_of_pages,
                   "lastModified": max((x["lastModified"] for x in libs if x["lastModified"]), default=None)}
            site_rows.append((site["id"], row))
            if q["recycleBin"]:
                recycle_candidates.append((q["recycleBin"], site))
        if scan.out_of_pages:
            ctx.warn(f"Stopped after reading {MAX_PAGES:,} pages of files; later sites aren't fully counted")
            ctx.truncated = True
        if len(team_sites) > MAX_SITES:
            ctx.warn(f"Scanned the first {MAX_SITES} of {len(team_sites)} sites")
            ctx.truncated = True
        if skipped:
            ctx.warn(f"{skipped} site(s) couldn't be read")

        recycled = self._recycle_bins(ctx, recycle_candidates)
        onedrives = self._onedrives(ctx, scan)

        ctx.objects("site_storage", site_rows)
        ctx.objects("library_storage", [(f"{x['siteUrl']}|{x['webUrl']}".lower(), x) for x in libraries])
        for kind, top in (("large_file", scan.largest), ("version_file", scan.versions), ("stale_file", scan.stale),
                          ("large_folder", scan.folders), ("onedrive_file", scan.onedrive)):
            ctx.objects(kind, [(r["webUrl"] or f"{r['siteUrl']}/{r['path']}", r) for r in top.rows()])
        ctx.objects("recycle_item", recycled)
        ctx.objects("onedrive_storage", onedrives)
        ctx.objects("storage_status", [("status", {
            "available": True, "sites": len(site_rows), "libraries": len(libraries), "pages": scan.pages,
            "files": sum(x["files"] for x in libraries if not x["personal"]),
            "staleDays": STALE_DAYS, "complete": not ctx.truncated,
        })])

        rows = [r for _, r in site_rows]
        ctx.metric("sharepoint.storage.used_bytes", sum(r["used"] for r in rows))
        ctx.metric("sharepoint.storage.versions_bytes", sum(r["versions"] for r in rows))
        ctx.metric("sharepoint.storage.recycle_bin_bytes", sum(r["recycleBin"] for r in rows))
        ctx.metric("sharepoint.storage.stale_bytes", sum(r["staleBytes"] for r in rows))
        ctx.metric("sharepoint.storage.preservation_hold_bytes", sum(r["preservationHold"] for r in rows))

    def _recycle_bins(self, ctx: RunContext, candidates: list[tuple[int, dict]]) -> list[tuple[str, dict]]:
        """The largest items in the fullest recycle bins (Microsoft lists them in beta only)."""
        stored = []
        for _, site in sorted(candidates, key=lambda c: -c[0])[:RECYCLE_SITES]:
            try:
                items = ctx.graph.get_all(f"/sites/{site['id']}/recycleBin/items", version="beta",
                                          max_pages=RECYCLE_PAGES).items
            except GraphError as exc:
                ctx.log(f"Couldn't list the recycle bin of {site.get('webUrl')}: {exc.message}", "warning")
                continue
            top = _Top(RECYCLE_TOP)
            for i in items:
                top.add(_int(i.get("size")), {
                    "name": i.get("name") or i.get("title"), "size": _int(i.get("size")),
                    "deleted": i.get("deletedDateTime"), "from": i.get("deletedFromLocation"),
                    "deletedBy": _who(i.get("lastModifiedBy")) or _who(i.get("deletedBy")),
                    "site": site.get("displayName") or site.get("webUrl"), "siteUrl": site.get("webUrl")})
            stored += [(f"{site['id']}:{i}", r) for i, r in enumerate(top.rows())]
        return stored

    def _onedrives(self, ctx: RunContext, scan: _Scan) -> list[tuple[str, dict]]:
        """Each licensed person's OneDrive quota; the nearly full ones are walked like a library."""
        stored = []
        for user in TenantSnapshot(ctx.conn, ctx.tenant).objects("users", "user") or []:
            if user.get("userType") != "Member" or not user.get("licensed"):
                continue
            try:
                drive = ctx.graph.get(f"/users/{user['_id']}/drive", {"$select": "id,webUrl,quota"})
            except GraphError:
                continue  # never opened, or no OneDrive licence
            q = quota_row(drive)
            upn = user.get("userPrincipalName")
            total = _int((drive.get("quota") or {}).get("total"))
            row = {"owner": upn, "url": drive.get("webUrl"), **q, "limit": total or None, "scanned": False}
            if total and q["used"] / total >= NEARLY_FULL and not scan.out_of_pages:
                try:
                    lst = ctx.graph.get(f"/drives/{drive['id']}/list", {"$select": "id,name,displayName,webUrl"})
                except GraphError:
                    lst = None
                if lst:
                    where = {"site": f"OneDrive of {upn}", "siteUrl": drive.get("webUrl"), "personal": True}
                    lib = scan.library(ctx, f"/drives/{drive['id']}/list", lst, where)
                    if lib:
                        row.update(scanned=True, current=lib["current"], versions=lib["versions"],
                                   files=lib["files"], staleBytes=lib["staleBytes"])
            stored.append(((upn or drive["id"]).lower(), row))
        return stored
