"""Mailbox holds and archiving: which mailboxes keep mail (litigation hold, archive) and which should.

Reads the mailbox rows from ``collectors/exchange.py`` (ArchiveStatus, LitigationHoldEnabled) and the
sizes from ``collectors/mail_usage.py``. Nothing new is collected: retention policies, hold dates and
archive sizes aren't read yet, so they aren't shown.
"""

from __future__ import annotations

from datetime import datetime

from ..collectors.exchange import SYSTEM_MAILBOX_TYPES
from ..licences import last_sign_in
from ..snapshot import TenantSnapshot
from ..text import plural, verb
from .data_reports import DataReport, Rows, Section, Tile, _type
from .mail_reports import NOT_COLLECTED, _no_exchange, _size, _text
from .registry import report

GB = 1024 ** 3
# A mailbox without an archive is "large" at 80% of what it may send (40 GB of a 50 GB quota), or at
# 40 GB when Exchange reported no quota.
NEAR_QUOTA = 0.8
LARGE_BYTES = 40 * GB

EVERY = "Every mailbox"
LEAVERS = "Leavers without a hold"
LARGE = "Large mailboxes without an archive"


def _archive_on(box: dict, size: dict) -> bool:
    status = box.get("ArchiveStatus")
    if status is None:
        return bool(size.get("archive"))
    return status not in ("", "None")


def _on_hold(box: dict, size: dict) -> bool:
    hold = box.get("LitigationHoldEnabled")
    return bool(size.get("litigationHold") if hold is None else hold)


def _account(user: dict | None) -> str:
    if user is None:
        return "Not found in the directory"
    return "Active" if user.get("accountEnabled") else "Sign-in blocked"


def _leaver(box: dict, user: dict | None) -> str | None:
    """Why this mailbox looks like someone who has left, or None."""
    if user is None:
        return None
    kind = box.get("RecipientTypeDetails")
    if kind == "UserMailbox" and not user.get("accountEnabled"):
        return "Sign-in blocked"
    if kind == "SharedMailbox" and last_sign_in(user):
        return "Shared mailbox that was signed in to as a person (likely converted from a leaver)"
    return None


def _large(size: dict) -> tuple[bool, float | None]:
    total, quota = size.get("totalBytes"), size.get("sendQuotaBytes")
    if total is None:
        return False, None
    if quota:
        return total >= NEAR_QUOTA * quota, total / quota
    return total >= LARGE_BYTES, None


@report(
    "mailbox-holds",
    "Mailbox holds and archiving",
    "Which mailboxes are on litigation hold or have an archive, leavers' mailboxes that aren't "
    "held, and large mailboxes that need an archive.",
    ("mailboxes", "users", "mailbox_usage"),
    group="email",
    order=60,
    keywords="litigation hold archive retention ediscovery",
)
def holds(snap: TenantSnapshot, now: datetime, report: DataReport) -> None:
    boxes = snap.objects("mailboxes", "mailbox")
    if boxes is None:
        report.unavailable = NOT_COLLECTED
        return
    if _no_exchange(snap):
        report.unavailable = "This tenant has no Exchange Online licence."
        return
    boxes = [m for m in boxes if m.get("RecipientTypeDetails") not in SYSTEM_MAILBOX_TYPES]
    users = {u["_id"]: u for u in snap.objects("users", "user") or []}
    size_rows = snap.objects("mailbox_usage", "mailbox_size")
    sizes = {s.get("mailboxId"): s for s in size_rows or []}

    every = Section(EVERY, ["Mailbox", "Type", "Account", "Litigation hold", "Archive", "Mailbox size"],
                    empty="No mailboxes.")
    leavers = Section(LEAVERS, ["Mailbox", "Type", "Why it looks like a leaver", "Archive", "Mailbox size"],
                      empty="Every leaver's mailbox is on litigation hold, or there are no leavers.")
    large = Section(LARGE, ["Mailbox", "Type", "Mailbox size", "Stops sending at", "Used"],
                    empty="No mailbox without an archive is close to its quota.")
    held = archived = 0
    for m in sorted(boxes, key=lambda m: _text(m.get("PrimarySmtpAddress")).lower()):
        size = sizes.get(m["_id"]) or {}
        user = users.get(m.get("ExternalDirectoryObjectId"))
        hold, archive = _on_hold(m, size), _archive_on(m, size)
        held += hold
        archived += archive
        address, kind, total = m.get("PrimarySmtpAddress"), _type(m.get("RecipientTypeDetails")), \
            _size(size.get("totalBytes"))
        why = _leaver(m, user)
        if why and not hold:
            leavers.add([address, kind, why, archive, total], "medium")
        every.add([address, kind, _account(user), hold, archive, total],
                  "medium" if why and not hold else None)
        big, used = _large(size)
        if big and not archive:
            large.add([address, kind, total, _size(size.get("sendQuotaBytes")) or "No limit",
                       f"{used:.0%}" if used is not None else ""], "low")

    n = len(boxes)
    every.intro = (f"{held} of {plural(n, 'mailbox')} {verb(held, 'is', 'are')} on litigation hold and "
                   f"{archived} {verb(archived, 'has', 'have')} an archive turned on.")
    leavers.intro = (f"{plural(len(leavers.rows), 'mailbox')} of people who seem to have left "
                     f"{verb(len(leavers.rows), 'is', 'are')} not on litigation hold. A hold keeps every message, "
                     "including deleted ones, after the leaver's licence is removed or the mailbox is converted; "
                     "without one, mail can be deleted and is gone for good once the licence goes. If the client "
                     "needs to keep a leaver's mail, put the mailbox on hold (Exchange admin centre, Recipients > "
                     "Mailboxes > the mailbox > Others > Manage litigation hold) before removing the licence.")
    if size_rows is None:
        large.intro = "Mailbox sizes haven't been collected yet. Refresh Mailbox size on the tenant page."
    else:
        large.intro = (f"{plural(len(large.rows), 'mailbox')} without an archive "
                       f"{verb(len(large.rows), 'is', 'are')} over {NEAR_QUOTA:.0%} of quota (or over "
                       f"{LARGE_BYTES // GB} GB). Turning on the archive moves old mail out of the mailbox before "
                       "sending stops.")

    report.tiles = [
        Tile("Mailboxes on litigation hold", f"{held} of {n}", "Mail kept even if deleted",
             rows=Rows(EVERY, {"Litigation hold": "Yes"})),
        Tile("Archive turned on", f"{archived} of {n}", "Mailboxes with an online archive",
             rows=Rows(EVERY, {"Archive": "Yes"})),
        Tile("Leavers' mailboxes without a hold", len(leavers.rows), "Blocked or converted, not on hold",
             "warn" if leavers.rows else "ok", Rows(LEAVERS)),
    ]
    report.sections = [every, leavers, large]
