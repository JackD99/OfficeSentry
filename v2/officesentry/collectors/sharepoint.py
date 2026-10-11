"""SharePoint and OneDrive.

Two collectors, so a missing permission only costs its own report:

* ``sharepoint_usage`` reads Microsoft's usage reports (Reports.Read.All):
  every site and OneDrive with storage, activity and how many anyone,
  company-wide and guest links it holds, plus 180 days of storage history.
* ``shared_files`` walks every document library and OneDrive (Sites.Read.All)
  and records each file or folder shared with anyone, the whole organisation
  or people outside it.

Everything here is GET only.
"""

from __future__ import annotations

import re
import time
from datetime import UTC, datetime, timedelta

from ..devices import parse_ts
from ..graph import GraphError, PermissionMissing
from ..mailflow import is_external, tenant_domains
from ..snapshot import TenantSnapshot
from ..text import plural
from .base import Collector, RunContext, register
from .sharepoint_lookup import DRIVE_FIELDS, SITE_FIELDS, Lookups, refused

# Microsoft's usage reports. v1.0 only returns CSV; beta returns JSON.
REPORT_PERIOD = "D30"
SITE_USAGE = f"/reports/getSharePointSiteUsageDetail(period='{REPORT_PERIOD}')"
ONEDRIVE_USAGE = f"/reports/getOneDriveUsageAccountDetail(period='{REPORT_PERIOD}')"
SITE_STORAGE = "/reports/getSharePointSiteUsageStorage(period='D180')"
ONEDRIVE_STORAGE = "/reports/getOneDriveUsageStorage(period='D180')"
JSON = {"$format": "application/json"}

ZERO_GUID = "00000000-0000-0000-0000-000000000000"
# When "Display concealed user, group, and site names" is on, Microsoft replaces
# names with a 32-character hex hash and blanks URLs and site IDs.
HASHED = re.compile(r"^[0-9A-F]{32}$")


def concealed(row: dict) -> bool:
    return not row.get("siteUrl") and bool(HASHED.match(row.get("ownerDisplayName") or ""))


def _int(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def site_row(r: dict) -> dict:
    return {
        "url": r.get("siteUrl"),
        "siteId": None if r.get("siteId") in (None, "", ZERO_GUID) else r["siteId"],
        "owner": r.get("ownerDisplayName"),
        "ownerPrincipalName": r.get("ownerPrincipalName"),
        "template": r.get("rootWebTemplate"),
        "deleted": bool(r.get("isDeleted")),
        "lastActivityDate": r.get("lastActivityDate"),
        "files": _int(r.get("fileCount")),
        "activeFiles": _int(r.get("activeFileCount")),
        "pageViews": _int(r.get("pageViewCount")),
        "storageUsed": _int(r.get("storageUsedInBytes")),
        "storageAllocated": _int(r.get("storageAllocatedInBytes")),
        "externalSharing": bool(r.get("externalSharing")),
        "anyoneLinks": _int(r.get("anonymousLinkCount")),
        "companyLinks": _int(r.get("companyLinkCount")),
        "guestLinks": _int(r.get("secureLinkForGuestCount")),
        "memberLinks": _int(r.get("secureLinkForMemberCount")),
        "sensitivityLabel": r.get("siteSensitivityLabelId") or None,
        "unmanagedDevicePolicy": r.get("unmanagedDevicePolicy"),
        "concealed": concealed(r),
        "reportDate": r.get("reportRefreshDate"),
    }


def onedrive_row(r: dict) -> dict:
    return {
        "url": r.get("siteUrl"),
        "owner": r.get("ownerDisplayName"),
        "ownerPrincipalName": (r.get("ownerPrincipalName") or "").lower() or None,
        "deleted": bool(r.get("isDeleted")),
        "lastActivityDate": r.get("lastActivityDate"),
        "files": _int(r.get("fileCount")),
        "activeFiles": _int(r.get("activeFileCount")),
        "storageUsed": _int(r.get("storageUsedInBytes")),
        "storageAllocated": _int(r.get("storageAllocatedInBytes")),
        "concealed": concealed(r),
        "reportDate": r.get("reportRefreshDate"),
    }


def _key(row: dict, fallback: str) -> str:
    """Stable object ID: the URL, else the site ID. Concealed rows have neither."""
    return (row.get("url") or row.get("siteId") or fallback).lower()


def _series(rows: list[dict]) -> list[list]:
    """Daily storage from Microsoft's D180 report, oldest first."""
    points = {
        r["reportDate"]: _int(r.get("storageUsedInBytes"))
        for r in rows
        if r.get("reportDate") and r.get("siteType") in (None, "All")
    }
    return [[d, points[d]] for d in sorted(points)]


@register
class SharePointUsage(Collector):
    key = "sharepoint_usage"
    title = "SharePoint and OneDrive usage"
    description = (
        "Every site and OneDrive with storage, activity and sharing link counts, from Microsoft's usage reports."
    )
    permissions = ("Reports.Read.All",)

    def collect(self, ctx: RunContext) -> None:
        if ctx.capabilities.get("sharepoint") is False:
            ctx.log("Tenant has no SharePoint licence; nothing to collect")
            ctx.objects("usage_status", [("status", {"available": False, "concealed": False})])
            return
        sites = ctx.paged(ctx.graph.get_all(SITE_USAGE, JSON, version="beta"))
        drives = ctx.paged(ctx.graph.get_all(ONEDRIVE_USAGE, JSON, version="beta"))
        site_rows = [site_row(r) for r in sites]
        drive_rows = [onedrive_row(r) for r in drives]
        ctx.objects("site", [(_key(r, f"hidden-site-{i}"), r) for i, r in enumerate(site_rows)])
        ctx.objects("onedrive", [(_key(r, f"hidden-onedrive-{i}"), r) for i, r in enumerate(drive_rows)])

        history = {}
        for name, path in (("sharepoint", SITE_STORAGE), ("onedrive", ONEDRIVE_STORAGE)):
            try:
                history[name] = _series(ctx.graph.get_all(path, JSON, version="beta").items)
            except GraphError as exc:
                ctx.warn(f"Storage history unavailable ({name}): {exc.message}")
        ctx.objects("storage_history", [(k, {"points": v}) for k, v in history.items()])

        hidden = any(r["concealed"] for r in site_rows + drive_rows)
        ctx.objects(
            "usage_status",
            [
                (
                    "status",
                    {
                        "available": True,
                        "concealed": hidden,
                        "reportDate": next((r["reportDate"] for r in site_rows + drive_rows if r["reportDate"]), None),
                    },
                )
            ],
        )
        if hidden:
            ctx.log("Microsoft is concealing names in usage reports; sites and OneDrives show as codes", "warning")

        live = [r for r in site_rows if not r["deleted"]]
        ctx.metric("sharepoint.sites", len(live))
        ctx.metric("sharepoint.storage_bytes", sum(r["storageUsed"] for r in live))
        ctx.metric("sharepoint.files", sum(r["files"] for r in live))
        ctx.metric("sharepoint.links.anyone", sum(r["anyoneLinks"] for r in live))
        ctx.metric("sharepoint.links.company", sum(r["companyLinks"] for r in live))
        ctx.metric("sharepoint.links.guest", sum(r["guestLinks"] for r in live))
        live_drives = [r for r in drive_rows if not r["deleted"]]
        ctx.metric("onedrive.accounts", len(live_drives))
        ctx.metric("onedrive.storage_bytes", sum(r["storageUsed"] for r in live_drives))


# -- Shared files ------------------------------------------------------------------

ITEM_FIELDS = "id,name,webUrl,parentReference,shared,file,folder,size,lastModifiedDateTime,deleted,root"
# What describe_permissions reads. Asking for everything returns grantedTo and grantedToIdentities too,
# which Microsoft retired in 2023 (it sends Deprecation and Sunset headers); the V2 fields replace them.
PERMISSION_FIELDS = (
    "id,roles,link,grantedToV2,grantedToIdentitiesV2,inheritedFrom,expirationDateTime,hasPassword,invitation"
)
MAX_DRIVES = 3000
MAX_PAGES_PER_DRIVE = 250  # 250 pages of up to 200 items: 50,000 items per library
MAX_PERMISSION_LOOKUPS = 5000  # shared items whose permissions are read per run
# One run's share of the night, so a huge client can't hold a collector thread until morning: this many
# delta pages across every library (about a million items), or this long, whichever comes first. The
# libraries left over keep their last results and are scanned first the next night.
MAX_PAGES = 6000
TIME_BUDGET = timedelta(hours=2)
# After a library's first full walk, each night asks Microsoft only for what changed since (its delta link,
# kept as a ``drive_cursor`` object) and re-reads the permissions of changed items only; the rest keep last
# night's answer. Every library is walked whole again after this long, so nothing drifts for good.
FULL_SCAN_DAYS = 30
MAX_STORED_ITEMS = 20000  # shared files and folders kept per tenant; the ones shared furthest come first
clock = time.monotonic
SCOPES = {
    "anonymous": "Anyone",
    "organization": "Organisation",
    "users": "Specific people",
    "existingAccess": "People with access",
}
ROLE_WORDS = {"read": "View", "write": "Edit", "owner": "Full control"}
LINK_WORDS = {
    "view": "View",
    "edit": "Edit",
    "review": "Review",
    "embed": "Embed",
    "blocksDownload": "View, no download",
    "createOnly": "Upload only",
    "addressBar": "View",
}


def _identity_email(identity: dict | None) -> str | None:
    """Best address for a grantedToV2 / grantedToIdentitiesV2 entry."""
    for part in ("user", "siteUser", "group", "siteGroup"):
        who = (identity or {}).get(part) or {}
        email = who.get("email") or who.get("loginName", "").split("|")[-1]
        if email and "@" in email:
            return email.lower()
        if who.get("displayName"):
            return who["displayName"]
    return None


def _is_guest(identity: dict | None) -> bool:
    for part in ("user", "siteUser"):
        login = ((identity or {}).get(part) or {}).get("loginName") or ""
        if "#ext#" in login.lower() or "urn:spo:guest" in login.lower():
            return True
    return False


def _is_group(identity: dict | None) -> bool:
    return any((identity or {}).get(part) for part in ("group", "siteGroup"))


def _can(roles: list, link: dict | None) -> str:
    if link:
        return LINK_WORDS.get(link.get("type"), link.get("type") or "View")
    if "owner" in roles:
        return ROLE_WORDS["owner"]
    return ROLE_WORDS["write"] if "write" in roles else ROLE_WORDS["read"]


def describe_permissions(perms: list[dict], domains: set[str], now: datetime | None = None) -> dict:
    """Reduce an item's own (not inherited) permissions to what matters for exposure, and list each grant.

    ``grants`` is one row per way in: a person, group or guest given access directly, each person a link
    was sent to, or an anyone or organisation-wide link on its own. ``kind`` is person, guest (a guest
    account or an outside address), group, anyone or organisation; ``how`` says direct or link. A grant
    past its expiry (at ``now``) is listed, so the report can say so, but no longer counts as access.
    """
    links, people, grants = [], [], []
    for p in perms:
        if p.get("inheritedFrom"):
            continue
        roles = [r for r in p.get("roles") or [] if r != "owner"] if isinstance(p.get("roles"), list) else []
        link = p.get("link") if isinstance(p.get("link"), dict) else None
        expires = p.get("expirationDateTime") or None
        ends = parse_ts(expires)
        expired = bool(now and ends and ends < now)
        if link:
            scope = link.get("scope") or "users"
            identities = [i for i in p.get("grantedToIdentitiesV2") or [] if isinstance(i, dict)]
            granted = [_identity_email(i) for i in identities]
            external = [g for g in granted if g and "@" in g and is_external(g, domains)]
            if expired:
                links_in = []  # listed as a grant below, but it opens nothing any more
            else:
                links_in = links
            links_in.append(
                {
                    "scope": scope,
                    "type": link.get("type") or "view",
                    "expires": expires,
                    "password": bool(p.get("hasPassword")),
                    "people": [g for g in granted if g],
                    "external": external,
                }
            )
            can = _can(roles, link)
            how = {"anonymous": "Anyone link", "organization": "Organisation link"}.get(scope, "Link")
            if scope in ("anonymous", "organization"):
                grants.append(
                    {
                        "who": "Anyone with the link" if scope == "anonymous" else "Everyone in the organisation",
                        "kind": "anyone" if scope == "anonymous" else "organisation",
                        "how": how,
                        "can": can,
                        "outside": scope == "anonymous",
                        "expires": expires,
                        "password": bool(p.get("hasPassword")),
                    }
                )
            for identity, who in zip(identities, granted, strict=True):
                if not who:
                    continue
                outside = _is_guest(identity) or who in external
                grants.append(
                    {
                        "who": who,
                        "kind": "group" if _is_group(identity) else ("guest" if outside else "person"),
                        "how": how,
                        "can": can,
                        "outside": outside,
                        "expires": expires,
                        "password": bool(p.get("hasPassword")),
                    }
                )
            continue
        if "owner" in (p.get("roles") or []) and not roles:
            continue
        for identity in [p.get("grantedToV2")] + (p.get("grantedToIdentitiesV2") or []):
            if not isinstance(identity, dict):
                continue
            who = _identity_email(identity)
            if not who:
                continue
            invited = ((p.get("invitation") or {}).get("email") or "").lower()
            external = (
                _is_guest(identity)
                or bool(invited and is_external(invited, domains))
                or ("@" in who and bool(domains) and is_external(who, domains))
            )
            if not expired:
                people.append({"who": invited or who, "roles": roles, "external": external})
            grants.append(
                {
                    "who": invited or who,
                    "kind": "group" if _is_group(identity) else ("guest" if external else "person"),
                    "how": "Direct",
                    "can": _can(roles, None),
                    "outside": external,
                    "expires": expires,
                    "password": False,
                }
            )
    anyone = [lk for lk in links if lk["scope"] == "anonymous"]
    org = [lk for lk in links if lk["scope"] == "organization"]
    outside = sorted({e for lk in links for e in lk["external"]} | {p["who"] for p in people if p["external"]})
    exposure = "anyone" if anyone else ("external" if outside else ("organization" if org else None))
    expiries = [g["expires"] for g in grants if g["expires"]]
    return {
        "exposure": exposure,
        "links": links,
        "people": people,
        "grants": grants,
        "externalPeople": outside,
        "internalPeople": sorted(
            {p["who"] for p in people if not p["external"]}
            | {g for lk in links for g in lk["people"] if g not in lk["external"]}
        ),
        "anyoneLinks": len(anyone),
        "orgLinks": len(org),
        "editable": any(lk["type"] == "edit" for lk in anyone + org)
        or any("write" in p["roles"] for p in people if p["external"]),
        "neverExpires": any(not lk["expires"] for lk in anyone),
        "expires": min(expiries) if expiries else None,
    }


def _shared_by(item: dict) -> tuple[str | None, str | None]:
    """Who shared the item and when, from its ``shared`` facet (Microsoft fills it on the item itself)."""
    shared = item.get("shared") if isinstance(item.get("shared"), dict) else {}
    by = shared.get("sharedBy") if isinstance(shared.get("sharedBy"), dict) else {}
    return _identity_email(by), shared.get("sharedDateTime") or None


class Budget:
    """One run's allowance of delta pages and time; the scan stops at whichever runs out first."""

    def __init__(self):
        self.started = clock()
        self.pages = 0
        self.lookups = 0

    @property
    def time_spent(self) -> str | None:
        if clock() - self.started >= TIME_BUDGET.total_seconds():
            return f"the {int(TIME_BUDGET.total_seconds() // 3600)}-hour limit"
        return None

    @property
    def spent(self) -> str | None:
        """What ran out, in words, or None while there's allowance left."""
        if self.pages >= MAX_PAGES:
            return f"the limit of {plural(MAX_PAGES, 'page')} a night"
        if self.lookups >= MAX_PERMISSION_LOOKUPS:
            return f"the limit of {MAX_PERMISSION_LOOKUPS:,} sharing lookups a night"
        return self.time_spent

    def over(self) -> bool:
        return self.spent is not None

    def no_lookups(self) -> bool:
        """True once the sharing lookups a library's items still need can't be made this run: the pages a
        run may read don't stop the lookups of a library already read."""
        return self.lookups >= MAX_PERMISSION_LOOKUPS or self.time_spent is not None


def _in_turn(drives: list[dict], next_drive: str | None) -> list[dict]:
    """The libraries in a fixed order, starting from where the last run stopped (if that library is still
    there), so a run that reaches its budget leaves the rest to go first the next night."""
    drives = sorted(drives, key=lambda d: d["id"])
    for n, drive in enumerate(drives):
        if drive["id"] == next_drive:
            return drives[n:] + drives[:n]
    return drives


def _path(item: dict) -> str:
    parent = (item.get("parentReference") or {}).get("path") or ""
    parent = parent.split("root:", 1)[1] if "root:" in parent else ""
    return f"{parent}/{item.get('name')}".lstrip("/")


def _ranked(rows: list[tuple[str, dict]]) -> list[tuple[str, dict]]:
    """The items shared furthest first, so a cut keeps what matters most."""
    rank = {"anyone": 0, "external": 1, "organization": 2}
    return sorted(rows, key=lambda kv: (rank.get(kv[1].get("exposure"), 3), kv[0]))


class _DriveScan:
    """What one library's walk found: its shared items, how many unchanged ones it kept, and its cursor."""

    def __init__(self):
        self.found: list[tuple[str, dict]] = []
        self.kept = 0
        self.items = 0  # items read this run
        self.complete = True
        self.cursor: dict | None = None
        self.full = False


@register
class SharedFiles(Collector):
    key = "shared_files"
    title = "Shared files"
    description = (
        "Every file and folder in SharePoint and OneDrive shared with anyone, the whole organisation, "
        "people outside it, or given its own permissions. Needs the Sites.Read.All permission."
    )
    permissions = ("Sites.Read.All",)

    def collect(self, ctx: RunContext) -> None:
        if ctx.capabilities.get("sharepoint") is False:
            ctx.log("Tenant has no SharePoint licence; nothing to scan")
            ctx.objects("scan_status", [("status", {"available": False, "reason": "no_sharepoint"})])
            return
        catalog = Lookups(ctx, self.key, SITE_FIELDS, DRIVE_FIELDS)
        try:
            drives = self._drives(ctx, catalog)
        except PermissionMissing as exc:
            reason, why = refused(ctx, "reading files", exc)
            ctx.warn(why)
            ctx.objects("scan_status", [("status", {"available": False, "reason": reason})])
            return

        snap = TenantSnapshot(ctx.conn, ctx.tenant)
        domains = tenant_domains(snap) or set()
        previous = (snap.objects("shared_files", "scan_status") or [None])[0] or {}
        last_items: dict[str, dict] = {}
        for item in snap.objects("shared_files", "shared_item") or []:
            last_items[item["_id"]] = {k: v for k, v in item.items() if k not in ("_id", "_kind")}
        cursors = {c["_id"]: c for c in snap.objects("shared_files", "drive_cursor") or []}
        now = datetime.now(UTC)
        self.now_dt = now
        self.now = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        rescan_before = (now - timedelta(days=FULL_SCAN_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
        drives = _in_turn(drives, previous.get("next_drive"))
        budget = Budget()
        scanned = kept = changed = 0
        full_scans = 0
        stored: list[tuple[str, dict]] = []
        new_cursors: list[tuple[str, dict]] = []
        skipped_drives = 0
        scanned_drives = 0
        stopped = None
        for n, drive in enumerate(drives):
            stopped = budget.spent or (f"the limit of {MAX_DRIVES:,} libraries a night" if n >= MAX_DRIVES else None)
            if stopped:
                break
            cursor = cursors.get(drive["id"])
            incremental = bool(cursor and cursor.get("deltaLink") and (cursor.get("fullScanAt") or "") > rescan_before)
            try:
                scan = self._scan_drive(ctx, drive, budget, domains, last_items, cursor if incremental else None)
            except GraphError as exc:
                skipped_drives += 1
                ctx.log(f"Skipped {drive['webUrl'] or drive['id']}: {exc.message}", "warning")
                continue
            if not scan.complete and (scanned_drives or not scan.items):
                # The budget ran out part-way through this library: it's scanned again, whole, next time.
                # (Unless it was the first and it did read something: then it keeps what was read, so one
                # library that alone takes longer than the budget can't stop the others ever being scanned.)
                stopped = budget.spent
                break
            stored.extend(scan.found)
            if scan.cursor:
                new_cursors.append((drive["id"], scan.cursor))
            scanned += scan.items
            kept += scan.kept
            changed += len(scan.found) - scan.kept
            full_scans += scan.full
            scanned_drives += 1
        left = drives[scanned_drives + skipped_drives :]
        next_drive = left[0]["id"] if left else None
        carried = 0
        if left:
            # Those libraries keep what the last scan found, so a report doesn't lose its rows for a night.
            left_ids = {d["id"] for d in left}
            for item_id, item in last_items.items():
                if item_id.split(":", 1)[0] in left_ids:
                    stored.append((item_id, item))
                    carried += 1
            for d in left:
                if d["id"] in cursors:
                    new_cursors.append((d["id"], {k: v for k, v in cursors[d["id"]].items() if k[0] != "_"}))
            ctx.warn(
                f"Scanned {plural(scanned_drives, 'library')} of {len(drives)} before reaching {stopped}; "
                f"the other {len(left)} keep their last results and are scanned first next time"
            )
            ctx.truncated = True
        if skipped_drives:
            ctx.warn(f"{plural(skipped_drives, 'library')} couldn't be read")
        if len(stored) > MAX_STORED_ITEMS:
            ctx.warn(
                f"Found {len(stored):,} shared files and folders; keeping the {MAX_STORED_ITEMS:,} shared furthest"
            )
            ctx.truncated = True
            stored = _ranked(stored)[:MAX_STORED_ITEMS]
        catalog.save()
        ctx.objects("shared_item", stored)
        ctx.objects("drive_cursor", new_cursors)
        ctx.objects(
            "scan_status",
            [
                (
                    "status",
                    {
                        "available": True,
                        "drives": len(drives),
                        "items": scanned,
                        "lookups": budget.lookups,
                        "scanned_drives": scanned_drives,
                        "full_scans": full_scans,
                        "kept": kept,
                        "changed": changed,
                        "next_drive": next_drive,
                        "carried": carried,
                        "at": self.now,
                    },
                )
            ],
        )
        if scanned_drives and full_scans < scanned_drives:
            ctx.log(
                f"Asked Microsoft for changes only in {scanned_drives - full_scans} of {scanned_drives} libraries: "
                f"{plural(changed, 'shared item')} changed, {kept:,} unchanged"
            )
        rows = [r for _, r in stored]
        ctx.metric("sharing.items_scanned", scanned)
        ctx.metric("sharing.anyone_items", sum(r["exposure"] == "anyone" for r in rows))
        ctx.metric("sharing.external_items", sum(bool(r["externalPeople"]) for r in rows))
        ctx.metric("sharing.org_items", sum(r["orgLinks"] > 0 for r in rows))
        ctx.metric("sharing.unique_items", len(rows))
        ctx.metric("sharing.external_people", len({p for r in rows for p in r["externalPeople"]}))

    def _scan_drive(
        self, ctx: RunContext, drive: dict, budget: Budget, domains: set[str], last_items: dict, cursor: dict | None
    ) -> _DriveScan:
        """Walk one library: whole (``cursor`` None), or only what changed since its delta link."""
        scan = _DriveScan()
        prefix = drive["id"] + ":"
        mine = {k: v for k, v in last_items.items() if k.startswith(prefix)}
        pages = min(MAX_PAGES_PER_DRIVE, MAX_PAGES - budget.pages)
        result = None
        if cursor:
            try:
                result = ctx.graph.get_all(cursor["deltaLink"], max_pages=pages, stop=budget.over)
            except GraphError as exc:
                if exc.status != 410:  # 410 Gone: the token expired, so walk the library whole again
                    raise
                ctx.log(f"Change tracking for {drive['webUrl'] or drive['id']} expired; scanning it whole")
                cursor = None
        if cursor is None:
            scan.full = True
            result = ctx.graph.get_all(
                f"/drives/{drive['id']}/root/delta", {"$select": ITEM_FIELDS}, max_pages=pages, stop=budget.over
            )
        budget.pages += result.pages
        if result.truncated:
            scan.complete = False
            if not budget.spent:
                ctx.warn(f"Stopped scanning {drive['webUrl']} after {MAX_PAGES_PER_DRIVE} pages")
        items = [i for i in result.items if "deleted" not in i and "root" not in i]
        scan.items = len(items)
        gone = {prefix + i["id"] for i in result.items if "deleted" in i}
        shared_ids = {i["id"] for i in items if i.get("shared") is not None}
        if not scan.full:
            # Unchanged items keep last night's answer; the shared folders among them still shelter their
            # changed children from a lookup of their own.
            unchanged = {
                k: v for k, v in mine.items() if k not in gone and k[len(prefix) :] not in {i["id"] for i in items}
            }
            shared_ids |= {k[len(prefix) :] for k in unchanged}
            scan.found.extend(unchanged.items())
            scan.kept = len(unchanged)
        # Children of a shared folder report the folder's sharing too. Read
        # permissions only at the top of each shared branch.
        tops = [
            i for i in items if i["id"] in shared_ids and (i.get("parentReference") or {}).get("id") not in shared_ids
        ]
        for item in tops:
            if budget.no_lookups():
                scan.complete = False
                break
            budget.lookups += 1
            try:
                perms = ctx.graph.get_all(
                    f"/drives/{drive['id']}/items/{item['id']}/permissions", {"$select": PERMISSION_FIELDS}
                ).items
            except GraphError as exc:
                ctx.log(f"Couldn't read sharing for {item.get('webUrl')}: {exc.message}", "warning")
                continue
            info = describe_permissions(perms, domains, self.now_dt)
            if not info["grants"]:
                continue
            key = prefix + item["id"]
            by, at = _shared_by(item)
            scan.found.append(
                (
                    key,
                    {
                        "name": item.get("name"),
                        "path": _path(item),
                        "webUrl": item.get("webUrl"),
                        "folder": "folder" in item,
                        "size": item.get("size"),
                        "lastModified": item.get("lastModifiedDateTime"),
                        "site": drive["site"],
                        "siteUrl": drive["siteUrl"],
                        "library": drive["name"],
                        "personal": drive["personal"],
                        "owner": drive["owner"],
                        "sharedBy": by,
                        "sharedAt": at,
                        "firstSeen": (mine.get(key) or {}).get("firstSeen") or self.now,
                        **info,
                    },
                )
            )
        if scan.complete and result.delta_link:
            scan.cursor = {
                "deltaLink": result.delta_link,
                "fullScanAt": self.now if scan.full else cursor.get("fullScanAt") or self.now,
                "checkedAt": self.now,
            }
        return scan

    def _drives(self, ctx: RunContext, catalog: Lookups) -> list[dict]:
        """Every document library and OneDrive, de-duplicated by drive ID."""
        drives: dict[str, dict] = {}
        owners: set[str] = set()  # IDs and addresses of people whose OneDrive a site listing already gave
        sites = catalog.sites()
        for site in sites:
            personal = bool(site.get("isPersonalSite")) or "/personal/" in (site.get("webUrl") or "")
            try:
                found = ctx.graph.get_all(
                    f"/sites/{site['id']}/drives", {"$select": "id,name,webUrl,driveType,owner"}
                ).items
            except GraphError as exc:  # a locked or broken site shouldn't stop the scan
                ctx.log(f"Couldn't list libraries of {site.get('webUrl')}: {exc.message}", "warning")
                continue
            for d in found:
                owner = (d.get("owner") or {}).get("user") or {}
                if personal or d.get("driveType") == "business":
                    owners.update(x.lower() for x in (owner.get("id"), owner.get("email")) if x)
                drives[d["id"]] = {
                    "id": d["id"],
                    "name": d.get("name"),
                    "webUrl": d.get("webUrl"),
                    "site": site.get("displayName") or site.get("webUrl"),
                    "siteUrl": site.get("webUrl"),
                    "personal": personal or d.get("driveType") == "business",
                    "owner": owner.get("email") or owner.get("displayName"),
                }
        # Site listings don't always include OneDrives, so ask for each licensed
        # person's OneDrive too. A 404 means they never opened it. People whose
        # OneDrive the listing already gave are skipped: asking again returns the
        # same drive, which the listing's entry would win over anyway.
        for user in TenantSnapshot(ctx.conn, ctx.tenant).objects("users", "user") or []:
            if user.get("userType") != "Member" or not user.get("licensed"):
                continue
            if owners & {(x or "").lower() for x in (user.get("_id"), user.get("userPrincipalName"), user.get("mail"))}:
                continue
            d = catalog.onedrive(user)
            if d is None:
                continue
            drives.setdefault(
                d["id"],
                {
                    "id": d["id"],
                    "name": d.get("name"),
                    "webUrl": d.get("webUrl"),
                    "site": f"OneDrive of {user.get('userPrincipalName')}",
                    "siteUrl": d.get("webUrl"),
                    "personal": True,
                    "owner": user.get("userPrincipalName"),
                },
            )
        return list(drives.values())
