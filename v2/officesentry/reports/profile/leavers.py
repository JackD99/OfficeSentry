"""Before you close this account: what to reclaim, and the leaver sheet."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from ... import devices
from ...licences import assignments
from ...mailflow import mailbox_forwarding
from ...snapshot import TenantSnapshot
from ...text import plural as _plural
from ..data_reports import Section, _type
from ..identity_reports import Money
from ..mail_reports import _Directory, _grants, _size
from .base import Part, Person, _currency, part
from .licences import _licences
from .onedrive import _size_text
from .page import find_user

SHARED_MAILBOX_FREE_BYTES = 50 * 1024**3  # shared mailboxes need no licence up to 50 GB


@dataclass
class ReclaimItem:
    area: str
    found: str  # what's still there
    todo: str  # what to do about it, in plain English
    severity: str | None = None


@dataclass
class Reclaim:
    """What's still attached to a leaver's account, for the checklist part and its sign-off PDF."""

    items: list[ReclaimItem] = field(default_factory=list)
    monthly: float = 0.0  # licence cost a month, where priced
    currency: str = "GBP"
    unpriced: int = 0  # licences with no price yet
    missing: list[str] = field(default_factory=list)  # areas not collected, so not checked


def reclaim(p: Person) -> Reclaim:
    """Licences, mailbox, OneDrive, devices, groups, mailbox access and admin roles still tied to the account."""
    out = Reclaim(currency=_currency(p.snap))
    add = out.items.append

    held, _ = _licences(p)
    paths = assignments(p.user) or {}
    groups = {g["_id"]: g.get("displayName") for g in p.snap.objects("groups", "group") or []}
    for sid, name, price in held:
        a = paths.get(sid)
        via = [groups.get(g) or "a group" for g in a.groups] if a else []
        if price is None:
            out.unpriced += 1
        else:
            out.monthly += price
        cost = "free" if price == 0.0 else ("not priced" if price is None else f"{Money(price, out.currency)} a month")
        todo = "Remove the licence once the mailbox and files below are dealt with."
        if via:
            todo += (
                f" It comes from {', '.join(via)}, so take them out of "
                f"{'that group' if len(via) == 1 else 'those groups'}"
                + (" and remove the direct assignment too." if a and a.direct else ".")
            )
        add(ReclaimItem("Licence", f"{name} ({cost})", todo, None if price == 0.0 else "medium"))

    boxes = p.snap.objects("mailboxes", "mailbox")
    if boxes is None:
        out.missing.append("mailboxes")
    d = _Directory(boxes or [], p.snap.objects("users", "user") or [])
    box = d.mailbox(p.id) or next((d.mailbox(k) for k in p.keys if d.mailbox(k)), None)
    if box is not None:
        _reclaim_mailbox(p, box, held, add)
    grants = _grants(p.snap, d) if boxes else []
    into = sorted(
        {g[2] for g in grants if box is not None and g[0] is not None and g[0]["_id"] == box["_id"]}, key=str.lower
    )
    if into:
        add(
            ReclaimItem(
                "Mailbox access",
                f"{_plural(len(into), 'person', 'people')} can open or send from this mailbox: {', '.join(into)}",
                "Keep access for whoever takes over the mail and remove it for everyone else.",
                "low",
            )
        )
    out_of = sorted(
        {
            g[1]
            for g in grants
            if ((g[3] or {}).get("_id") == p.id or (g[3] is None and p.named(g[2])))
            and not (box is not None and g[0] is not None and g[0]["_id"] == box["_id"])
        },
        key=str.lower,
    )
    if out_of:
        add(
            ReclaimItem(
                "Mailbox access",
                f"Can open or send from {', '.join(out_of)}",
                f"Remove this person's access to {'that mailbox' if len(out_of) == 1 else 'those mailboxes'}.",
                "medium",
            )
        )

    drives = p.snap.objects("sharepoint_usage", "onedrive")
    if drives is None:
        out.missing.append("OneDrive")
    drive = next((r for r in drives or [] if not r.get("deleted") and p.named(r.get("ownerPrincipalName"))), None)
    if drive and (drive.get("storageUsed") or drive.get("files")):
        add(
            ReclaimItem(
                "OneDrive",
                f"{_size_text(drive.get('storageUsed') or 0)} in {_plural(drive.get('files') or 0, 'file')}",
                "Give their manager access or move what's needed elsewhere. Microsoft deletes the OneDrive "
                "some time after the account is deleted (30 days unless the tenant has changed it).",
                "medium",
            )
        )

    managed = devices.objects(p.snap, "managed_device")
    if managed is None:
        out.missing.append("devices")
    for dev in sorted(
        (x for x in managed or [] if p.named(x.get("userPrincipalName"))),
        key=lambda x: (x.get("deviceName") or "").lower(),
    ):
        add(
            ReclaimItem(
                "Device",
                f"{dev.get('deviceName') or 'A device'} ({devices.os_label(dev)})",
                "Collect it, then wipe or retire it in Intune."
                if devices.is_computer(dev)
                else "Collect it if it's a company device, or retire it in Intune to remove company data.",
                "medium",
            )
        )

    if p.snap.objects("groups", "group") is None:
        out.missing.append("groups")
    for g in sorted(p.snap.objects("groups", "group") or [], key=lambda g: (g.get("displayName") or "").lower()):
        if not g.get("examined") or not any(p.named(o.get("upn")) for o in g.get("owners") or []):
            continue
        others = (g.get("enabledOwnerCount") or 0) - (1 if p.enabled else 0)
        if others <= 0:
            kind = "team" if g.get("isTeam") else "group"
            add(
                ReclaimItem(
                    "Group owner",
                    f"Only owner of {g.get('displayName') or 'a group'} ({kind})",
                    f"Make someone else an owner, or nobody will be able to manage the {kind}.",
                    "medium",
                )
            )

    roles = p.snap.objects("admin_roles", "role_assignment")
    if roles is None:
        out.missing.append("admin roles")
    names = sorted(
        {
            (r.get("roleName") or "Unknown role") + ("" if r.get("assignment") == "active" else " (eligible)")
            for r in roles or []
            if r.get("principalId") == p.id
        }
    )
    for name in names:
        add(
            ReclaimItem(
                "Admin role",
                name,
                "Remove the role. Admin roles left on an unused account are a common way in for attackers.",
                "high",
            )
        )
    return out


def _reclaim_mailbox(p: Person, box: dict, held, add) -> None:
    kind = box.get("RecipientTypeDetails") or ""
    sizes = {s.get("mailboxId"): s for s in p.snap.objects("mailbox_usage", "mailbox_size") or []}
    total = (sizes.get(box["_id"]) or {}).get("totalBytes")
    found = [f"{_type(kind)} mailbox {box.get('PrimarySmtpAddress') or ''}".strip()]
    if total is not None:
        found.append(_size(total))
    from ... import usage

    row = usage.person(p.snap, p.upn) if p.upn else None
    received = ((row or {}).get("email") or {}).get("received")
    if received:
        days = (usage.status(p.snap) or {}).get("days") or usage.ACTIVE_DAYS
        found.append(f"still receiving mail ({received:,} in the last {days} days)")
    elif received == 0:
        found.append("no mail received recently")
    target = mailbox_forwarding(box)
    if target:
        found.append(f"forwards to {target}")
    hold = bool(box.get("LitigationHoldEnabled"))
    if kind == "SharedMailbox":
        todo = "Already a shared mailbox."
        if total is not None and total >= SHARED_MAILBOX_FREE_BYTES:
            todo += " Over 50 GB it still needs a licence, or move older mail to an archive."
    elif total is not None and total >= SHARED_MAILBOX_FREE_BYTES:
        todo = (
            f"At {_size(total)} it's over the 50 GB a shared mailbox can hold without a licence: it needs a "
            "licence (Exchange Online Plan 2) or an archive for older mail before you remove this licence."
        )
    else:
        todo = (
            "Convert it to a shared mailbox so colleagues can keep using it, then remove the licence. Shared "
            "mailboxes under 50 GB need no licence."
        )
        if total is None:
            todo += " Its size hasn't been collected, so check it's under 50 GB first."
    if hold:
        todo += " Litigation hold is on, so it still needs a licence that includes hold."
    if received:
        todo += " Decide who gets new mail: give someone access, forward it or set an automatic reply."
    add(ReclaimItem("Mailbox", ", ".join(found), todo, "medium" if held and kind != "SharedMailbox" else None))


@part(5)
def reclaim_part(p: Person) -> Part | None:
    if p.enabled and not getattr(p, "leaver", False):
        return None  # only for blocked accounts, or when someone asks for the leaver checklist
    r = reclaim(p)
    result = Part("reclaim", "Before you close this account")
    if not r.items:
        result.summary, result.tone = "Nothing left", "ok"
        result.empty = "Nothing is left on this account in the data collected."
    else:
        table = Section("Before you close this account", ["Area", "What's left", "What to do"], compare=False)
        for item in r.items:
            table.add([item.area, item.found, item.todo], item.severity)
        result.tables = [table]
        result.summary = (
            _plural(len(r.items), "thing")
            + " to do"
            + (f", {Money(r.monthly, r.currency)} a month" if r.monthly else "")
        )
        result.tone = "warn"
        if r.monthly:
            result.facts = [
                ("Licence cost", f"{Money(r.monthly, r.currency)} a month, {Money(r.monthly * 12, r.currency)} a year")
            ]
    notes = ["Office Sentry only reports; make these changes in the Microsoft 365 admin centre."]
    if r.unpriced:
        notes.append(
            f"{_plural(r.unpriced, 'licence')} {'has' if r.unpriced == 1 else 'have'} no price yet, so "
            "the cost leaves them out."
        )
    if r.missing:
        notes.append(f"Not checked because they haven't been collected: {', '.join(r.missing)}.")
    result.note = " ".join(notes)
    return result


@dataclass
class LeaverSheet:
    """The checklist on its own, for the sign-off PDF."""

    tenant: dict
    user_id: str
    name: str
    upn: str
    enabled: bool
    generated_at: datetime
    checklist: Reclaim
    sources: list[tuple[str, str | None]] = field(default_factory=list)


def leaver_sheet(conn, tenant, user_id: str, now: datetime | None = None) -> LeaverSheet | None:
    now = now or datetime.now(UTC)
    snap = TenantSnapshot(conn, tenant)
    user = find_user(snap, user_id)
    if user is None:
        return None
    p = Person(snap, user, now)
    from ...collectors import REGISTRY

    sources = [
        (REGISTRY[c].title, (snap.runs.get(c) or {"finished_at": None})["finished_at"])
        for c in ("users", "mailboxes", "mailbox_usage", "sharepoint_usage", "devices", "groups", "admin_roles")
        if c in REGISTRY
    ]
    return LeaverSheet(
        dict(tenant), user_id, user.get("displayName") or p.label, p.upn, p.enabled, now, reclaim(p), sources
    )
