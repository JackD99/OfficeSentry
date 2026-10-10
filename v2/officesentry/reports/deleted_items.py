"""Deleted users and groups: the Entra ID recycle bin, and how long each item can still be restored.

For a technician who has just been asked "can we get them back?": every deleted user and Microsoft 365
group or team Microsoft still holds, when it was deleted and how many days are left before it is gone
for good. Licensed members and teams about to go are flagged. Restoring is done in the Entra or
Microsoft 365 admin centre; Office Sentry only reads the recycle bin.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from ..collectors.deleted_items import EXPIRING_DAYS, RETENTION_DAYS, purge_date
from ..devices import parse_ts
from ..licences import sku_name
from ..snapshot import TenantSnapshot
from ..text import plural
from .data_reports import DataReport, Rows, Section, Tile, metric_trend
from .registry import report

USERS = "Deleted users"
GROUPS = "Deleted teams and groups"
SOON = f"{EXPIRING_DAYS} days"
LATER = f"{RETENTION_DAYS} days"


def _window(deleted: str | None, now: datetime) -> tuple[str, str, int | str, str]:
    """(deleted on, deleted for good on, days left, ends within) for a deletedDateTime."""
    when, purge = parse_ts(deleted), purge_date(deleted)
    if purge is None:
        return "", "", "", ""
    left = max(0, (purge - now).days)
    within = SOON if purge <= now + timedelta(days=EXPIRING_DAYS) else LATER
    return when.date().isoformat(), purge.date().isoformat(), left, within


def _group_kind(g: dict) -> str:
    if g.get("isTeam"):
        return "Team"
    if g.get("microsoft365"):
        return "Microsoft 365 group"
    if g.get("mailEnabled"):
        return "Mail-enabled security group" if g.get("securityEnabled") else "Distribution list"
    return "Security group"


@report(
    "deleted-items",
    "Deleted users and groups",
    "Users, teams and Microsoft 365 groups in the recycle bin, with the licences they held and "
    f"the days left to restore each before Microsoft deletes it for good after {RETENTION_DAYS} "
    "days.",
    ("deleted_items", "tenant_profile"),
    group="identity",
    order=90,
    keywords="recycle bin restore undelete recover soft-deleted removed accounts leavers deleted teams "
    "deleted mailbox 30 days purge",
)
def build(snap: TenantSnapshot, now: datetime, report: DataReport) -> None:
    status = (snap.objects("deleted_items", "deleted_status") or [None])[0]
    if status is None:
        report.unavailable = ("Deleted users and groups haven't been collected yet. Refresh Deleted users and "
                              "groups.")
        return
    if not status.get("users") and not status.get("groups"):
        report.unavailable = ("Microsoft didn't return the recycle bin on the last collection. The collection log "
                              "on the tenant page has the reason.")
        return

    skus = snap.objects("tenant_profile", "subscribed_sku")
    names = {s["skuId"]: sku_name(s.get("skuPartNumber")) for s in skus or [] if isinstance(s.get("skuId"), str)}
    if skus is None:
        report.notes.append("Licence names show after the next collection of Tenant profile and licences.")

    users = _users(snap.objects("deleted_items", "deleted_user") or [], names, now)
    groups = _groups(snap.objects("deleted_items", "deleted_group") or [], now)
    report.sections = []
    if status.get("users"):
        report.sections.append(users)
    else:
        report.notes.append("Deleted users weren't readable on the last collection.")
    if status.get("groups"):
        report.sections.append(groups)
    else:
        report.notes.append("Deleted teams and groups weren't readable on the last collection.")

    soon_users = sum(1 for r in users.rows if r[-1] == SOON) if status.get("users") else 0
    soon_groups = sum(1 for r in groups.rows if r[-1] == SOON) if status.get("groups") else 0
    soon = soon_users + soon_groups
    teams = sum(1 for r in groups.rows if r[1] == "Team")
    report.tiles = []
    if status.get("users"):
        guests = sum(1 for r in users.rows if r[2] == "Guest")
        report.tiles.append(Tile(USERS, len(users.rows), f"{plural(guests, 'guest')} among them",
                                 rows=Rows(USERS), trend=metric_trend(snap, "deleted.users", now)))
    if status.get("groups"):
        report.tiles.append(Tile("Deleted groups and teams", len(groups.rows), f"{plural(teams, 'team')} among them",
                                 rows=Rows(GROUPS), trend=metric_trend(snap, "deleted.groups", now)))
    # Link to a table that's on the page: the one with accounts ending soon, else whichever was read.
    target = (USERS if soon_users else GROUPS if soon_groups
              else USERS if status.get("users") else GROUPS if status.get("groups") else None)
    report.tiles.append(Tile(
        f"Permanently deleted within {EXPIRING_DAYS} days", soon,
        f"{plural(soon_users, 'user')}, {plural(soon_groups, 'group')}",
        ("warn" if users.flagged or groups.flagged else None) if soon else "ok",
        Rows(target, {"Ends within": SOON}) if target else None))
    report.footnote = (f"Microsoft keeps deleted users and Microsoft 365 groups for {RETENTION_DAYS} days, then "
                       "deletes them for good. Restore them in the Microsoft Entra admin center (Users > Deleted "
                       "users, Groups > Deleted groups) or the Microsoft 365 admin center; Office Sentry only reads "
                       "the recycle bin and never restores or deletes anything.")


def _users(rows: list[dict], names: dict, now: datetime) -> Section:
    section = Section(USERS, ["Name", "Sign-in name", "Type", "Department", "Licences it held", "Deleted on",
                              "Deleted for good on", "Days left", "Ends within"],
                      intro=f"Accounts deleted in the last {RETENTION_DAYS} days that can still be restored with "
                            "their mailbox, OneDrive, group memberships and licences. Licensed members with "
                            f"{EXPIRING_DAYS} days or less left are flagged.",
                      empty="No deleted users: the recycle bin is empty.")
    out = []
    for u in rows:
        deleted, purge, left, within = _window(u.get("deletedDateTime"), now)
        kind = "Guest" if u.get("userType") == "Guest" else "Member"
        licences = [names.get(s, "Unknown licence") for s in u.get("licenceSkuIds") or []]
        flag = None
        if within == SOON:
            flag = "medium" if kind == "Member" and licences else "low"
        name = u.get("displayName") or u.get("userPrincipalName") or u.get("_id")
        out.append(((left if isinstance(left, int) else RETENTION_DAYS + 1, str(name).lower()),
                    [name, u.get("userPrincipalName"), kind, u.get("department"), licences, deleted, purge, left,
                     within], flag))
    for _, row, flag in sorted(out, key=lambda r: r[0]):
        section.add(row, flag)
    return section


def _groups(rows: list[dict], now: datetime) -> Section:
    section = Section(GROUPS, ["Name", "Kind", "Email", "Deleted on", "Deleted for good on", "Days left",
                               "Ends within"],
                      intro=f"Microsoft 365 groups and teams deleted in the last {RETENTION_DAYS} days, which can "
                            "still be restored with their mailbox, files, chats and members. Teams with "
                            f"{EXPIRING_DAYS} days or less left are flagged. Only Microsoft 365 groups go to the "
                            "recycle bin: deleted security groups and distribution lists are gone straight away.",
                      empty="No deleted teams or groups.")
    out = []
    for g in rows:
        deleted, purge, left, within = _window(g.get("deletedDateTime"), now)
        kind = _group_kind(g)
        flag = None
        if within == SOON:
            flag = "medium" if kind == "Team" else "low"
        name = g.get("displayName") or g.get("mail") or g.get("_id")
        out.append(((left if isinstance(left, int) else RETENTION_DAYS + 1, str(name).lower()),
                    [name, kind, g.get("mail"), deleted, purge, left, within], flag))
    for _, row, flag in sorted(out, key=lambda r: r[0]):
        section.add(row, flag)
    return section
