"""SharePoint storage and cleanup: what fills the tenant's storage and what to clear first.

Reads the snapshot written by ``collectors/sharepoint_storage.py``. Written so
an MSP can hand it to a client whose SharePoint is full: where the space goes,
which sites, folders and files hold it, and how to free it.
"""

from __future__ import annotations

from datetime import datetime

from ..collectors.sharepoint_storage import NEARLY_FULL, STALE_DAYS
from ..snapshot import TenantSnapshot
from ..storage import tenant_storage
from ..text import nouns
from .data_reports import DataReport, Link, Rows, Section, Tile, _date
from .registry import report
from .sharepoint import GROWTH_DAYS, _growth, size, storage_points

SHOW_FOLDERS = 50
SHOW_FILES = 100
SHOW_RECYCLED = 100

HOW = {
    "current": "Archive or delete files nobody needs. See Largest files and Largest files untouched.",
    "versions": "Set a version limit (SharePoint admin centre, Settings, Version history limits) and trim old "
                "versions.",
    "recycle": "Empty the recycle bin and second-stage recycle bin of the largest sites. Otherwise items stay 93 "
               "days.",
    "hold": "Kept by a Microsoft Purview retention policy or hold. Review the policies that cover these sites.",
    "other": "Lists, pages, site assets, and figures SharePoint hasn't updated yet.",
}


SPACE = Rows("Where the space goes")


def _percent(part: float, whole: float) -> str:
    if not whole:
        return ""
    pct = 100 * part / whole
    return "Under 1%" if 0 < pct < 1 else f"{pct:.0f}%"


def _file_cell(r: dict):
    text = r.get("path") or r.get("name") or "?"
    return Link(text, r["webUrl"]) if r.get("webUrl") else text


def _site_cell(r: dict):
    name = r.get("site") or r.get("siteUrl") or "?"
    return Link(name, r["siteUrl"]) if r.get("siteUrl") and not r.get("personal") else name


def _location(r: dict):
    """Site and library, linking to the site."""
    text = f"{r.get('site') or r.get('siteUrl') or '?'} / {r.get('library')}" if r.get("library") else \
        (r.get("site") or "?")
    return Link(text, r["siteUrl"]) if r.get("siteUrl") and not r.get("personal") else text


def _top(snap: TenantSnapshot, kind: str, key: str, limit: int) -> list[dict]:
    rows = snap.objects("sharepoint_storage", kind) or []
    return sorted(rows, key=lambda r: (-(r.get(key) or 0), str(r.get("path") or r.get("name"))))[:limit]


def _older(r: dict) -> str:
    return size(r["versions"]) if r.get("versions") else "None"


def _version(r: dict) -> str:
    return str(r["version"]) if r.get("version") else ""


def _months_left(free: int, growth: float | None) -> str:
    if growth is None:
        return f"Growth shows after {GROWTH_DAYS} days of history"
    if growth <= 0:
        return f"Not growing over the last {GROWTH_DAYS} days"
    months = free / growth * GROWTH_DAYS / 30.4
    if months < 1:
        return "Full within a month at the current rate"
    if months > 36:
        return f"{size(growth)} a month; years of room left"
    return f"Full in about {months:.0f} months at {size(growth)} a month"


@report(
    "sharepoint-storage",
    "SharePoint storage",
    "What fills the tenant's SharePoint storage and what to clean up: "
    "sites, folders, large and old files, version history and recycle bins.",
    ("sharepoint_storage", "tenant_profile", "sharepoint_usage"),
    group="files",
    order=20,
    keywords="quota versions recycle bin large old files cleanup",
)
def storage(snap: TenantSnapshot, now: datetime, report: DataReport) -> None:
    status = (snap.objects("sharepoint_storage", "storage_status") or [None])[0]
    if status is None:
        report.unavailable = ("SharePoint storage hasn't been scanned yet. Refresh \"SharePoint storage\" on the "
                              "tenant page.")
        return
    if not status.get("available"):
        report.unavailable = ("This tenant has no SharePoint licence." if status.get("reason") == "no_sharepoint" else
                              "Office Sentry can't read this tenant's files yet. It needs the Sites.Read.All "
                              "permission: open the tenant page, send the consent link to the client's Global "
                              "Admin again and ask them to accept.")
        return
    sites = snap.objects("sharepoint_storage", "site_storage") or []
    libs = [x for x in snap.objects("sharepoint_storage", "library_storage") or [] if not x.get("personal")]
    used, quota = tenant_storage(snap)
    used = used or 0
    points = storage_points(snap, "sharepoint")
    growth = _growth(points, now)

    hold = sum(x.get("total") or 0 for x in libs if x.get("preservationHold"))
    current = sum(x.get("current") or 0 for x in libs if not x.get("preservationHold"))
    versions = sum(x.get("versions") or 0 for x in libs if not x.get("preservationHold"))
    recycle = sum(s.get("recycleBin") or 0 for s in sites)
    stale_bytes = sum(x.get("staleBytes") or 0 for x in libs)
    stale_files = sum(x.get("staleFiles") or 0 for x in libs)
    years = STALE_DAYS // 365

    if quota:
        pct = 100 * used / quota.total
        report.tiles.append(Tile("Storage used", _percent(used, quota.total) or "0%",
                                 f"{size(used)} of {size(quota.total)} (estimated)",
                                 "bad" if pct >= 95 else ("warn" if pct >= 85 else "ok"), SPACE))
        report.tiles.append(Tile("Free space", size(max(quota.total - used, 0)),
                                 _months_left(quota.total - used, growth), rows=SPACE))
    else:
        report.tiles.append(Tile("Storage used", size(used), "No SharePoint licence found to size the quota",
                                 rows=SPACE))
    report.tiles += [
        Tile("Older versions", size(versions), f"{_percent(versions, used) or '0%'} of storage",
             "warn" if used and versions / used >= 0.25 else None, Rows("Files with the most version history")),
        Tile("Recycle bins", size(recycle), "Freed when emptied", "warn" if used and recycle / used >= 0.1 else None,
             Rows("Largest items in recycle bins")),
        Tile(f"Untouched for {years} years", size(stale_bytes), f"{stale_files:,} {nouns(stale_files, 'file')}",
             rows=Rows(f"Largest files untouched for {years} years")),
    ]
    # No storage chart here: the SharePoint sites report has it, and the breakdown below matters more
    # on the first page of the client's PDF.
    if not status.get("complete"):
        report.notes.append("Some sites or libraries were too large to read in full, so their figures are partial. "
                            "The last SharePoint storage run on the tenant page says which.")

    parts = Section("Where the space goes", ["Part", "Size", "Share of storage", "How to free it"],
                    intro="Everything stored in SharePoint sites, by what it is." +
                          (f" The tenant has about {size(quota.total)} ({quota.explained})." if quota else ""),
                    empty="No SharePoint storage in use.")
    other = max(used - current - versions - recycle - hold, 0)
    for label, value, key in sorted([("Current files", current, "current"), ("Older versions of files", versions,
                                                                             "versions"),
                                     ("Recycle bins", recycle, "recycle"), ("Preservation Hold libraries", hold,
                                                                            "hold"),
                                     ("Lists, pages and other", other, "other")], key=lambda p: -p[1]):
        if value:
            parts.add([label, size(value), _percent(value, used), HOW[key]])

    by_site = Section("Sites by storage", ["Site", "Storage used", "Current files", "Older versions", "Recycle bin",
                                           f"Untouched {years}+ years", "Site limit", "Last change"],
                      intro="Largest first. Storage used includes versions, the recycle bin and everything else on "
                            "the site.", empty="No sites.")
    for s in sorted(sites, key=lambda s: -(s.get("used") or 0)):
        limit = s.get("limit")
        state = s.get("state")
        by_site.add([_site_cell(s), size(s.get("used") or 0), size(s.get("current") or 0),
                     size(s.get("versions") or 0), size(s.get("recycleBin") or 0), size(s.get("staleBytes") or 0),
                     f"{size(limit)} ({_percent(s.get('used') or 0, limit)} used)" if limit else "Tenant storage",
                     _date(s.get("lastModified")) or "None"],
                    {"exceeded": "critical", "critical": "high", "nearing": "medium"}.get(state) if limit else None)

    folders = Section("Largest folders", ["Folder", "Location", "Size", "Files", "Share of library"],
                      intro="Size includes every version of every file inside. Folders inside a listed folder can "
                            "appear too.", empty="No folders.", compare=False)
    lib_size = {(x.get("siteUrl"), x.get("library")): x.get("total") or 0 for x in libs}
    for r in _top(snap, "large_folder", "total", SHOW_FOLDERS):
        folders.add([_file_cell(r), _location(r), size(r.get("total")), f"{r.get('files') or 0:,}",
                     _percent(r.get("total") or 0, lib_size.get((r.get("siteUrl"), r.get("library"))))])

    largest = Section("Largest files", ["File", "Location", "Size", "Older versions", "Version", "Last change",
                                        "Changed by"],
                      intro="Size includes older versions.", empty="No files.", compare=False)
    for r in _top(snap, "large_file", "total", SHOW_FILES):
        largest.add([_file_cell(r), _location(r), size(r.get("total")), _older(r), _version(r),
                     _date(r.get("lastModified")), r.get("modifiedBy")])

    history = Section("Files with the most version history", ["File", "Location", "Older versions", "Current size",
                                                              "Version", "Last change"],
                      intro="Files saved many times, often by AutoSave or sync. A version limit on their library "
                            "stops this growing.", empty="No file keeps older versions.", compare=False)
    for r in _top(snap, "version_file", "versions", SHOW_FILES):
        history.add([_file_cell(r), _location(r), size(r.get("versions")), size(r.get("current")), _version(r),
                     _date(r.get("lastModified"))])

    old = Section(f"Largest files untouched for {years} years",
                  ["File", "Location", "Size", "Last change", "Changed by"],
                  intro="Candidates to archive or delete. Check with the site owner first.",
                  empty=f"No file has gone {years} years without a change.", compare=False)
    for r in _top(snap, "stale_file", "total", SHOW_FILES):
        old.add([_file_cell(r), _location(r), size(r.get("total")), _date(r.get("lastModified")),
                 r.get("modifiedBy")])

    libraries = Section("Libraries", ["Library", "Size", "Current files", "Older versions", "Files",
                                      "Highest version", "Last change"],
                        intro="Every document library, largest first. Preservation Hold libraries hold copies kept "
                              "by retention policies.", empty="No libraries.")
    for x in sorted(libs, key=lambda x: -(x.get("total") or 0)):
        name = f"{x.get('site')} / {x.get('library')}"
        libraries.add([Link(name, x["webUrl"]) if x.get("webUrl") else name, size(x.get("total") or 0),
                       size(x.get("current") or 0), size(x.get("versions") or 0), f"{x.get('files') or 0:,}",
                       x.get("maxVersion") or "", _date(x.get("lastModified")) or "None"],
                      "medium" if x.get("preservationHold") and x.get("total") else None)

    bins = Section("Largest items in recycle bins", ["Item", "Site", "Size", "Deleted", "Deleted from"],
                   intro="From the sites with the fullest recycle bins. These leave on their own 93 days after "
                         "deletion, or straight away when the bin is emptied.",
                   empty="Recycle bins are empty.", compare=False)
    for r in _top(snap, "recycle_item", "size", SHOW_RECYCLED):
        bins.add([r.get("name"), _site_cell(r), size(r.get("size")), _date(r.get("deleted")), r.get("from")])

    full = Section("OneDrives nearly full", ["Person", "Used", "Quota", "Share used", "Recycle bin", "Older versions"],
                   intro=f"OneDrives over {NEARLY_FULL:.0%} of their own quota. OneDrive storage doesn't count "
                         "towards the tenant's SharePoint storage.",
                   empty=f"No OneDrive is more than {NEARLY_FULL:.0%} full.")
    for d in sorted(snap.objects("sharepoint_storage", "onedrive_storage") or [],
                    key=lambda d: -((d.get("used") or 0) / (d.get("limit") or 1))):
        share = (d.get("used") or 0) / d["limit"] if d.get("limit") else 0
        if share >= NEARLY_FULL:
            full.add([d.get("owner"), size(d.get("used") or 0), size(d["limit"]), _percent(d.get("used") or 0,
                                                                                            d["limit"]),
                      size(d.get("recycleBin") or 0), size(d.get("versions") or 0) if d.get("scanned") else "Not read"],
                     "high" if share >= 0.95 else "medium")

    sections = [parts, by_site, folders, largest, history, old, libraries, bins, full]
    personal = _top(snap, "onedrive_file", "total", SHOW_FILES)
    if personal:
        mine = Section("Largest files in nearly full OneDrives", ["File", "Person", "Size", "Older versions",
                                                                  "Last change"],
                       intro="What the owner can move or delete to free their OneDrive.", compare=False)
        for r in personal:
            mine.add([_file_cell(r), (r.get("site") or "").removeprefix("OneDrive of "), size(r.get("total")),
                      _older(r), _date(r.get("lastModified"))])
        sections.append(mine)
    report.sections = sections
    report.footnote = (f"Read {status.get('files') or 0:,} files in {status.get('libraries') or 0} libraries on "
                       f"{status.get('sites') or 0} sites. Sizes come from SharePoint's own storage figures, which "
                       "it updates a few times a day, so they can differ slightly from the admin centre."
                       + (" The tenant's storage is estimated from its licences; the SharePoint admin centre shows "
                          "the exact figure." if quota else ""))
