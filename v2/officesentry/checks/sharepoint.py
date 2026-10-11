"""Checks over SharePoint and OneDrive snapshots."""

from __future__ import annotations

from datetime import datetime

from ..snapshot import TenantSnapshot
from ..storage import tenant_storage
from ..text import plural
from .base import Finding, _unavailable, rule


def _where(item: dict) -> str:
    place = (
        (f"OneDrive of {item.get('owner')}" if item.get("owner") else "OneDrive")
        if item.get("personal")
        else f"{item.get('site')} / {item.get('library')}"
    )
    return f"{item.get('path') or item.get('name') or '?'} ({place})"


@rule(
    "anyone_links",
    "No files are shared with anyone links",
    "Sharing",
    "Remove anyone links that are no longer needed, and set the rest to view-only with an expiry date. "
    "The Shared files report lists each one.",
)
def anyone_links(snap: TenantSnapshot, now: datetime):
    status = (snap.objects("shared_files", "scan_status") or [None])[0]
    if status and status.get("available"):
        items = [i for i in snap.objects("shared_files", "shared_item") or [] if i.get("exposure") == "anyone"]
        findings = [
            Finding(
                "high" if i.get("editable") else "medium",
                _where(i),
                ("Anyone can edit" if i.get("editable") else "Anyone can view")
                + (", link never expires" if i.get("neverExpires") else ""),
                i["_id"],
            )
            for i in items
        ]
        return (
            ("fail", f"{plural(len(findings), 'file or folder')} shared with anyone", findings)
            if findings
            else ("pass", f"No anyone links among {status.get('items', 0):,} scanned files and folders", [])
        )
    usage = (snap.objects("sharepoint_usage", "usage_status") or [None])[0]
    if usage is None:
        return _unavailable("SharePoint usage hasn't been collected yet")
    if not usage.get("available"):
        return _unavailable("The tenant has no SharePoint licence")
    sites = [s for s in snap.objects("sharepoint_usage", "site") or [] if s.get("anyoneLinks") and not s.get("deleted")]
    findings = [
        Finding(
            "medium",
            s.get("url") or f"Site with hidden name ({s.get('template')})",
            plural(s["anyoneLinks"], "anyone link"),
            s["_id"],
        )
        for s in sites
    ]
    total = sum(s["anyoneLinks"] for s in sites)
    return (
        ("fail", f"{plural(total, 'anyone link')} across {plural(len(sites), 'site')}", findings)
        if findings
        else ("pass", "No site has anyone links", [])
    )


@rule(
    "sharepoint_storage_space",
    "SharePoint storage isn't nearly full",
    "Storage",
    "Free up space before the tenant runs out, when people can no longer save files. The SharePoint storage "
    "report shows where the space goes and what to clear first; otherwise buy Office 365 Extra File Storage.",
)
def sharepoint_storage_space(snap: TenantSnapshot, now: datetime):
    used, quota = tenant_storage(snap)
    if used is None:
        return _unavailable("SharePoint storage hasn't been collected yet")
    if quota is None:
        return _unavailable("The tenant has no licence that includes SharePoint")
    pct = 100 * used / quota.total
    share = "under 1%" if 0 < pct < 1 else f"{pct:.0f}%"
    summary = f"{share} of about {quota.total / 1024**4:.2f} TB used ({quota.explained})"
    if pct < 85:
        return ("pass", summary, [])
    sites = sorted(snap.objects("sharepoint_storage", "site_storage") or [], key=lambda s: -(s.get("used") or 0))
    findings = [Finding("high" if pct >= 95 else "medium", "Tenant SharePoint storage", summary, "tenant")]
    findings += [
        Finding(
            "low",
            s.get("url") or s.get("site") or "?",
            f"{(s.get('used') or 0) / 1024**3:,.1f} GB, of which "
            f"{(s.get('recycleBin') or 0) / 1024**3:,.1f} GB in the recycle bin and "
            f"{(s.get('versions') or 0) / 1024**3:,.1f} GB older versions",
            s["_id"],
        )
        for s in sites[:5]
    ]
    return ("fail", summary, findings)


@rule(
    "outside_edit_access",
    "People outside can only view shared files",
    "Sharing",
    "Change sharing with people outside the organisation to view-only unless they need to edit, and "
    "remove sharing that's no longer needed. The File and folder permissions report lists each person "
    "outside and what they can edit.",
)
def outside_edit_access(snap: TenantSnapshot, now: datetime):
    status = (snap.objects("shared_files", "scan_status") or [None])[0]
    if status is None:
        return _unavailable("Shared files haven't been scanned yet")
    if not status.get("available"):
        return _unavailable(
            "The tenant has no SharePoint licence"
            if status.get("reason") == "no_sharepoint"
            else "Office Sentry can't read this tenant's files (Sites.Read.All)"
        )
    items = [i for i in snap.objects("shared_files", "shared_item") or [] if isinstance(i, dict)]
    if items and not any("grants" in i for i in items):
        return _unavailable("The last scan was made by an earlier version; the next one records who can edit")
    findings = []
    for i in items:
        editors = sorted(
            {
                g["who"]
                for g in i.get("grants") or []
                if isinstance(g, dict)
                and g.get("who")
                and g.get("outside")
                and g.get("kind") == "guest"
                and g.get("can") in ("Edit", "Full control")
            }
        )
        if editors:
            findings.append(Finding("medium", _where(i), "Can be edited by " + ", ".join(editors), i["_id"]))
    outside = sum(1 for i in items if i.get("externalPeople"))
    return (
        ("fail", f"{plural(len(findings), 'file or folder')} outside people can edit", findings)
        if findings
        else ("pass", f"Nothing of the {plural(outside, 'item')} shared outside can be edited from outside", [])
    )
