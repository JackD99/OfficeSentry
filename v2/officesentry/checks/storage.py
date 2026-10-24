"""Checks on storage people run out of: mailboxes and OneDrives near their quota."""

from __future__ import annotations

from datetime import datetime

from ..snapshot import TenantSnapshot
from ..text import plural
from ..usage import owner_label
from .base import Finding, _unavailable, rule

NEAR = 0.9  # share of the quota that counts as nearly full
GB = 1024**3


def _gb(value: float) -> str:
    return f"{value / GB:,.1f} GB" if value < 1024 * GB else f"{value / GB / 1024:,.2f} TB"


@rule(
    "mailbox_quota",
    "No mailbox is nearly full",
    "Storage",
    "Turn on the online archive for each listed mailbox (Exchange admin center > Mailboxes > Manage mailbox "
    "archive) with a retention policy that moves old mail into it, or clear large old items. A mailbox at its "
    "quota can't send, and soon after can't receive.",
    needs=("exchange",),
)
def mailbox_quota(snap: TenantSnapshot, now: datetime):
    status = snap.objects("mailboxes", "exchange_status")
    if status and not status[0].get("available"):
        return _unavailable("The tenant has no Exchange Online licence")
    rows = snap.objects("mailbox_usage", "mailbox_size")
    if rows is None:
        return _unavailable("Mailbox sizes haven't been collected yet")
    sized = [
        r
        for r in rows
        if isinstance(r.get("totalBytes"), int | float)
        and isinstance(r.get("sendQuotaBytes"), int | float)
        and r["sendQuotaBytes"] > 0
    ]
    if not sized:
        return _unavailable("Mailbox sizes couldn't be read" if rows else "The tenant has no mailboxes")
    findings = []
    for r in sorted(sized, key=lambda r: -r["totalBytes"] / r["sendQuotaBytes"]):
        share = r["totalBytes"] / r["sendQuotaBytes"]
        if share < NEAR:
            continue
        archive = "archive on" if r.get("archive") else "no archive"
        findings.append(
            Finding(
                "high" if share >= 1 else "medium",
                r.get("mailbox") or r.get("mailboxId") or "?",
                f"{_gb(r['totalBytes'])} of {_gb(r['sendQuotaBytes'])} ({min(share, 9.99):.0%}), {archive}",
                str(r.get("mailboxId") or r.get("_id")),
            )
        )
    return (
        ("fail", f"{plural(len(findings), 'mailbox')} at or over {NEAR:.0%} of quota", findings)
        if findings
        else ("pass", f"None of {plural(len(sized), 'mailbox')} is over {NEAR:.0%} of its quota", [])
    )


@rule(
    "onedrive_quota",
    "No OneDrive is nearly full",
    "Storage",
    "Ask each listed person to clear or archive old files, or raise their OneDrive quota (SharePoint admin "
    "center > Settings > OneDrive Storage limit, up to 5 TB with a qualifying licence). A full OneDrive stops "
    "syncing and saving.",
    needs=("sharepoint",),
)
def onedrive_quota(snap: TenantSnapshot, now: datetime):
    status = (snap.objects("sharepoint_usage", "usage_status") or [None])[0]
    if status is None:
        return _unavailable("SharePoint and OneDrive usage hasn't been collected yet")
    if not status.get("available"):
        return _unavailable("The tenant has no SharePoint licence")
    rows = [
        r
        for r in snap.objects("sharepoint_usage", "onedrive") or []
        if not r.get("deleted")
        and isinstance(r.get("storageUsed"), int | float)
        and isinstance(r.get("storageAllocated"), int | float)
        and r["storageAllocated"] > 0
    ]
    if not rows:
        return _unavailable("Nobody has a OneDrive yet")
    findings = []
    for r in sorted(rows, key=lambda r: -r["storageUsed"] / r["storageAllocated"]):
        share = r["storageUsed"] / r["storageAllocated"]
        if share < NEAR:
            continue
        who = owner_label(r)
        findings.append(
            Finding(
                "high" if share >= 1 else "medium",
                who or r.get("url") or "?",
                f"{_gb(r['storageUsed'])} of {_gb(r['storageAllocated'])} ({min(share, 9.99):.0%})",
                r["_id"],
            )
        )
    return (
        ("fail", f"{plural(len(findings), 'OneDrive')} at or over {NEAR:.0%} of quota", findings)
        if findings
        else ("pass", f"None of {plural(len(rows), 'OneDrive')} is over {NEAR:.0%} of its quota", [])
    )
