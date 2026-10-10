"""OneDrive: storage and what's shared."""

from __future__ import annotations

from ...text import plural as _plural
from ..data_reports import Link, Section
from .base import Part, Person, _day, part


def _size_text(n) -> str:
    from ..sharepoint import size

    return size(n)


@part(70)
def onedrive_part(p: Person) -> Part:
    result = Part("onedrive", "OneDrive")
    status = (p.snap.objects("sharepoint_usage", "usage_status") or [None])[0]
    drives = p.snap.objects("sharepoint_usage", "onedrive")
    scan = (p.snap.objects("shared_files", "scan_status") or [None])[0]
    shared = (p.snap.objects("shared_files", "shared_item") or []) if scan and scan.get("available") else None
    if drives is None and shared is None:
        result.unavailable = "OneDrive data hasn't been collected yet. Refresh SharePoint and OneDrive usage."
        return result
    if status and status.get("concealed"):
        result.note = (
            "Microsoft hides people's names in this tenant's usage reports, so OneDrive storage can't "
            "be matched to accounts. An admin can turn this off in the Microsoft 365 admin centre "
            "(Settings, Org settings, Reports)."
        )
    drive = next((r for r in drives or [] if not r.get("deleted") and p.named(r.get("ownerPrincipalName"))), None)
    if drive:
        used, quota = drive.get("storageUsed") or 0, drive.get("storageAllocated") or 0
        result.facts.append(("Storage used", _size_text(used) + (f" of {_size_text(quota)}" if quota else "")))
        result.facts.append(("Files", f"{drive.get('files') or 0:,}"))
        result.facts.append(("Files active in the last 30 days", f"{drive.get('activeFiles') or 0:,}"))
        result.facts.append(("Last activity", _day(drive.get("lastActivityDate")) or "None recorded"))
        result.summary = _size_text(used)
        if not p.enabled and used:
            p.flag(
                "low",
                f"Sign-in is blocked and the OneDrive still holds {_size_text(used)}. Move what's needed "
                "before the account is deleted.",
                "onedrive",
            )

    names = p.keys | ({p.user["displayName"].lower()} if p.user.get("displayName") else set())
    mine = [
        s for s in shared or [] if s.get("personal") and isinstance(s.get("owner"), str) and s["owner"].lower() in names
    ]
    if shared is None:
        result.note = (
            result.note + " " if result.note else ""
        ) + "Shared files haven't been scanned, so sharing isn't shown."
    elif mine:
        order = {"anyone": 0, "external": 1, "organization": 2}
        table = Section("Shared from this OneDrive", ["File or folder", "Shared with", "Can edit", "Link expires"])
        for s in sorted(mine, key=lambda s: (order.get(s.get("exposure"), 3), (s.get("path") or "").lower())):
            text = (s.get("path") or s.get("name") or "?") + (" (folder)" if s.get("folder") else "")
            who = (
                {"anyone": "Anyone with the link", "organization": "Everyone in the organisation"}.get(
                    s.get("exposure")
                )
                or ", ".join(s.get("externalPeople") or [])
                or "People outside"
            )
            expires = [lk.get("expires") for lk in s.get("links") or [] if lk.get("scope") == "anonymous"]
            table.add(
                [
                    Link(text, s["webUrl"]) if s.get("webUrl") else text,
                    who,
                    bool(s.get("editable")),
                    ("Never" if not all(expires) else _day(min(expires))) if expires else "",
                ],
                {"anyone": "high", "external": "medium"}.get(s.get("exposure")),
            )
        result.tables.append(table)
        anyone = sum(s.get("exposure") == "anyone" for s in mine)
        outside = sorted({e for s in mine for e in s.get("externalPeople") or []})
        if anyone:
            p.flag("high", f"Shares {_plural(anyone, 'item')} from OneDrive with anyone who has the link.", "onedrive")
        if outside:
            p.flag(
                "medium",
                f"Shares OneDrive files with {_plural(len(outside), 'person', 'people')} outside the "
                f"organisation ({', '.join(outside[:3])}{' and more' if len(outside) > 3 else ''}).",
                "onedrive",
            )
        if anyone:
            result.summary, result.tone = "Shared with anyone", "bad"
        elif outside:
            result.summary, result.tone = "Shared outside", "warn"
    if not drive and not result.tables:
        result.summary, result.empty = "None", "No OneDrive found for this account."
    return result
