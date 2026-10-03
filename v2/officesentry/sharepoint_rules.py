"""Checks over SharePoint and OneDrive snapshots. Registered into ``rules.RULES`` on import."""

from __future__ import annotations

from datetime import datetime

from .rules import Finding, _unavailable, rule
from .snapshot import TenantSnapshot


def _where(item: dict) -> str:
    place = (f"OneDrive of {item.get('owner')}" if item.get("owner") else "OneDrive") if item.get("personal") \
        else f"{item.get('site')} / {item.get('library')}"
    return f"{item.get('path') or item.get('name') or '?'} ({place})"


@rule("anyone_links", "No files are shared with anyone links", "Sharing",
      "Remove anyone links that are no longer needed, and set the rest to view-only with an expiry date. "
      "The Shared files report lists each one.")
def anyone_links(snap: TenantSnapshot, now: datetime):
    status = (snap.objects("shared_files", "scan_status") or [None])[0]
    if status and status.get("available"):
        items = [i for i in snap.objects("shared_files", "shared_item") or [] if i.get("exposure") == "anyone"]
        findings = [Finding("high" if i.get("editable") else "medium", _where(i),
                            ("Anyone can edit" if i.get("editable") else "Anyone can view")
                            + (", link never expires" if i.get("neverExpires") else ""), i["_id"])
                    for i in items]
        return ("fail", f"{len(findings)} file(s) or folder(s) shared with anyone", findings) if findings else \
            ("pass", f"No anyone links among {status.get('items', 0):,} scanned files and folders", [])
    usage = (snap.objects("sharepoint_usage", "usage_status") or [None])[0]
    if usage is None:
        return _unavailable("SharePoint usage hasn't been collected yet")
    if not usage.get("available"):
        return _unavailable("The tenant has no SharePoint licence")
    sites = [s for s in snap.objects("sharepoint_usage", "site") or [] if s.get("anyoneLinks") and not s.get("deleted")]
    findings = [Finding("medium", s.get("url") or f"Site with hidden name ({s.get('template')})",
                        f"{s['anyoneLinks']} anyone link(s)", s["_id"]) for s in sites]
    total = sum(s["anyoneLinks"] for s in sites)
    return ("fail", f"{total} anyone link(s) across {len(sites)} site(s)", findings) if findings else \
        ("pass", "No site has anyone links", [])

