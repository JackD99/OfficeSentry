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

from ..graph import GraphError, PermissionMissing
from ..mailflow import is_external, tenant_domains
from ..snapshot import TenantSnapshot
from ..text import plural
from .base import Collector, RunContext, register
from .sharepoint_lookup import DRIVE_FIELDS, SITE_FIELDS, Lookups

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
MAX_DRIVES = 3000
MAX_PAGES_PER_DRIVE = 250  # 250 pages of up to 200 items: 50,000 items per library
MAX_PERMISSION_LOOKUPS = 5000  # shared items whose permissions are read per run
SCOPES = {
    "anonymous": "Anyone",
    "organization": "Organisation",
    "users": "Specific people",
    "existingAccess": "People with access",
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


def describe_permissions(perms: list[dict], domains: set[str]) -> dict:
    """Reduce an item's own (not inherited) permissions to what matters for exposure."""
    links, people = [], []
    for p in perms:
        if p.get("inheritedFrom"):
            continue
        roles = [r for r in p.get("roles") or [] if r != "owner"]
        link = p.get("link")
        if link:
            granted = [_identity_email(i) for i in p.get("grantedToIdentitiesV2") or []]
            external = [g for g in granted if g and "@" in g and is_external(g, domains)]
            links.append(
                {
                    "scope": link.get("scope") or "users",
                    "type": link.get("type") or "view",
                    "expires": p.get("expirationDateTime"),
                    "password": bool(p.get("hasPassword")),
                    "people": [g for g in granted if g],
                    "external": external,
                }
            )
            continue
        if "owner" in (p.get("roles") or []) and not roles:
            continue
        for identity in [p.get("grantedToV2")] + (p.get("grantedToIdentitiesV2") or []):
            who = _identity_email(identity)
            if not who:
                continue
            invited = ((p.get("invitation") or {}).get("email") or "").lower()
            external = (
                _is_guest(identity)
                or bool(invited and is_external(invited, domains))
                or ("@" in who and bool(domains) and is_external(who, domains))
            )
            people.append({"who": invited or who, "roles": roles, "external": external})
    anyone = [lk for lk in links if lk["scope"] == "anonymous"]
    org = [lk for lk in links if lk["scope"] == "organization"]
    outside = sorted({e for lk in links for e in lk["external"]} | {p["who"] for p in people if p["external"]})
    exposure = "anyone" if anyone else ("external" if outside else ("organization" if org else None))
    return {
        "exposure": exposure,
        "links": links,
        "people": people,
        "externalPeople": outside,
        "anyoneLinks": len(anyone),
        "orgLinks": len(org),
        "editable": any(lk["type"] == "edit" for lk in anyone + org)
        or any("write" in p["roles"] for p in people if p["external"]),
        "neverExpires": any(not lk["expires"] for lk in anyone),
    }


def _path(item: dict) -> str:
    parent = (item.get("parentReference") or {}).get("path") or ""
    parent = parent.split("root:", 1)[1] if "root:" in parent else ""
    return f"{parent}/{item.get('name')}".lstrip("/")


@register
class SharedFiles(Collector):
    key = "shared_files"
    title = "Shared files"
    description = (
        "Every file and folder in SharePoint and OneDrive shared with anyone, the whole organisation, "
        "or people outside it. Needs the Sites.Read.All permission."
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
            ctx.warn(
                "Reading files needs the Sites.Read.All permission. Re-send the consent link from the tenant "
                f"page to add it. ({exc.message})"
            )
            ctx.objects("scan_status", [("status", {"available": False, "reason": "permission"})])
            return

        snap = TenantSnapshot(ctx.conn, ctx.tenant)
        domains = tenant_domains(snap) or set()
        lookups = 0
        scanned = 0
        stored: list[tuple[str, dict]] = []
        skipped_drives = 0
        for drive in drives[:MAX_DRIVES]:
            try:
                result = ctx.graph.get_all(
                    f"/drives/{drive['id']}/root/delta", {"$select": ITEM_FIELDS}, max_pages=MAX_PAGES_PER_DRIVE
                )
            except GraphError as exc:
                skipped_drives += 1
                ctx.log(f"Skipped {drive['webUrl'] or drive['id']}: {exc.message}", "warning")
                continue
            if result.truncated:
                ctx.warn(f"Stopped scanning {drive['webUrl']} after {MAX_PAGES_PER_DRIVE} pages")
            items = [i for i in result.items if "deleted" not in i and "root" not in i]
            scanned += len(items)
            shared_ids = {i["id"] for i in items if i.get("shared") is not None}
            # Children of a shared folder report the folder's sharing too. Read
            # permissions only at the top of each shared branch.
            tops = [
                i
                for i in items
                if i["id"] in shared_ids and (i.get("parentReference") or {}).get("id") not in shared_ids
            ]
            for item in tops:
                if lookups >= MAX_PERMISSION_LOOKUPS:
                    break
                lookups += 1
                try:
                    perms = ctx.graph.get_all(f"/drives/{drive['id']}/items/{item['id']}/permissions").items
                except GraphError as exc:
                    ctx.log(f"Couldn't read sharing for {item.get('webUrl')}: {exc.message}", "warning")
                    continue
                info = describe_permissions(perms, domains)
                if not info["exposure"]:
                    continue
                stored.append(
                    (
                        f"{drive['id']}:{item['id']}",
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
                            **info,
                        },
                    )
                )
        if lookups >= MAX_PERMISSION_LOOKUPS:
            ctx.warn(f"Read sharing for the first {MAX_PERMISSION_LOOKUPS} shared items only")
            ctx.truncated = True
        if len(drives) > MAX_DRIVES:
            ctx.warn(f"Scanned the first {MAX_DRIVES} of {len(drives)} libraries")
            ctx.truncated = True
        if skipped_drives:
            ctx.warn(f"{plural(skipped_drives, 'library')} couldn't be read")
        catalog.save()
        ctx.objects("shared_item", stored)
        ctx.objects(
            "scan_status",
            [
                (
                    "status",
                    {
                        "available": True,
                        "drives": len(drives),
                        "items": scanned,
                        "lookups": lookups,
                    },
                )
            ],
        )
        rows = [r for _, r in stored]
        ctx.metric("sharing.items_scanned", scanned)
        ctx.metric("sharing.anyone_items", sum(r["exposure"] == "anyone" for r in rows))
        ctx.metric("sharing.external_items", sum(bool(r["externalPeople"]) for r in rows))
        ctx.metric("sharing.org_items", sum(r["orgLinks"] > 0 for r in rows))

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
