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
    "site_owners",
    "Every site has at least two owners",
    "Sharing",
    "Give each site a second owner (in the Microsoft 365 group, or as a site administrator) so access "
    "requests and content questions have someone to go to when one person leaves. The Site access report "
    "lists each site's owners.",
)
def site_owners(snap: TenantSnapshot, now: datetime):
    status = (snap.objects("site_access", "access_status") or [None])[0]
    if status is None:
        return _unavailable("Site access hasn't been collected yet")
    if not status.get("available"):
        return _unavailable(
            "The tenant has no SharePoint licence"
            if status.get("reason") == "no_sharepoint"
            else "Office Sentry can't read this tenant's sites yet (Sites.Read.All)"
        )
    sites = [s for s in snap.objects("site_access", "site") or [] if isinstance(s, dict)]
    findings = []
    for s in sites:
        count = s.get("ownerCount")
        if count is None or count >= 2:
            continue
        where = "Microsoft 365 group" if s.get("ownerSource") == "group" else "site administrators"
        findings.append(
            Finding(
                "high" if count == 0 else "low",
                s.get("name") or s.get("url") or "?",
                f"No enabled owner ({where})" if count == 0 else f"One owner ({where})",
                s["_id"],
            )
        )
    known = sum(1 for s in sites if s.get("ownerCount") is not None)
    if findings:
        none = sum(1 for f in findings if f.severity == "high")
        summary = ", ".join(
            part
            for part in (
                f"{plural(none, 'site')} with no owner" if none else "",
                f"{plural(len(findings) - none, 'site')} with one owner" if len(findings) > none else "",
            )
            if part
        )
        return ("fail", summary, findings)
    return ("pass", f"{plural(known, 'site')} checked, each with two or more owners", [])


@rule(
    "site_guest_full_control",
    "Guests don't have full control of sites",
    "Sharing",
    "Take guests out of each site's owners group and away from Full Control; give them Edit or Read through the "
    "site's members or visitors group instead. Full Control lets someone outside the organisation change who "
    "can open the site and delete anything on it. The Site permissions report lists them.",
)
def site_guest_full_control(snap: TenantSnapshot, now: datetime):
    status = (snap.objects("site_permissions", "permissions_status") or [None])[0]
    if status is None:
        return _unavailable("Site permissions haven't been collected yet")
    if not status.get("available"):
        return _unavailable(
            "The tenant has no SharePoint licence"
            if status.get("reason") == "no_sharepoint"
            else "Waiting for the client to approve the SharePoint permission"
            if status.get("reason") == "consent"
            else "Office Sentry can't read this tenant's SharePoint permissions yet"
        )
    sites = [s for s in snap.objects("site_permissions", "site") or [] if isinstance(s, dict)]
    findings = []
    for s in sites:
        groups = {g.get("id"): g for g in s.get("groups") or [] if isinstance(g, dict)}
        held: dict[str, str] = {}
        for grant in s.get("grants") or []:
            p = grant.get("principal") if isinstance(grant, dict) else None
            if not isinstance(p, dict) or "Full Control" not in (grant.get("levels") or []):
                continue
            if p.get("kind") == "guest":
                held.setdefault(str(p.get("id")), "given Full Control directly")
            elif p.get("kind") == "sp_group":
                g = groups.get(p.get("id")) or {}
                for m in g.get("members") or []:
                    if isinstance(m, dict) and m.get("kind") == "guest":
                        held.setdefault(str(m.get("id")), f"in {g.get('name') or p.get('name')}")
        for guest, how in sorted(held.items()):
            findings.append(
                Finding(
                    "high",
                    f"{guest} on {s.get('name') or s.get('url') or '?'}",
                    f"Full Control, {how}",
                    f"{s['_id']}|{guest}",
                )
            )
    if findings:
        guests = len({f.object_id.split("|", 1)[1] for f in findings})
        on = len({f.object_id.split("|", 1)[0] for f in findings})
        return ("fail", f"{plural(guests, 'guest')} with full control of {plural(on, 'site')}", findings)
    return ("pass", f"{plural(len(sites), 'site')} checked; no guest has full control", [])
