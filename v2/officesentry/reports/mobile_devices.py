"""Mobile devices connected to mailboxes: which phones and tablets sync mail, how, and which shouldn't any more.

Reads ``collectors/mobile_devices.py`` (Get-MobileDevice and Get-MobileDeviceStatistics) and the account of
each mailbox's owner from the users snapshot. Leads with devices of people whose sign-in is blocked, devices
waiting for approval and partnerships nobody has used in months.
"""

from __future__ import annotations

from datetime import datetime

from ..devices import parse_ts
from ..snapshot import TenantSnapshot
from ..text import plural, verb
from .data_reports import DataReport, Rows, Section, Tile
from .mail_reports import _no_exchange
from .registry import report

NOT_COLLECTED = "Mobile devices haven't been collected yet. Refresh Mobile devices on the tenant page."
ACTIVE_DAYS = 30
STALE_DAYS = 90

EVERY = "Every device"
ATTENTION = "Devices that need attention"
PLATFORMS = "Devices by platform"

SEVERITY = ("high", "medium", "low")
APPS = {"EAS": "Mail app (ActiveSync)", "Outlook": "Outlook app", "REST": "Outlook app"}
MAILBOX_GONE = "Mailbox no longer exists"
ACCESS = {
    "Allowed": "Allowed",
    "Blocked": "Blocked",
    "Quarantined": "Waiting for approval",
    "DeviceDiscovery": "Being checked",
}


def _text(value) -> str:
    return value if isinstance(value, str) else ""


def platform(os_name) -> str:
    """ "iOS", "Android", "Windows" or "Other", from a device's DeviceOS ("iOS 17.5.1 21F90")."""
    text = _text(os_name).lower()
    for name, words in (
        ("iOS", ("ios", "iphone", "ipad")),
        ("Android", ("android",)),
        ("Windows", ("windows",)),
        ("macOS", ("mac",)),
    ):
        if text.startswith(words):
            return name
    return "Other"


def _device(d: dict) -> str:
    return _text(d.get("FriendlyName")) or _text(d.get("DeviceModel")) or _text(d.get("DeviceType")) or "Unknown device"


def _day(value) -> str:
    when = parse_ts(value)
    return when.date().isoformat() if when else ""


def _account(user: dict | None, gone: bool = False) -> str:
    if gone:
        return MAILBOX_GONE
    if user is None:
        return "Not found in the directory"
    return "Active" if user.get("accountEnabled") is not False else "Sign-in blocked"


@report(
    "mobile-devices",
    "Mobile devices",
    "Phones and tablets that sync mail, with which app, when each last synced, and leavers' "
    "devices that are still connected.",
    ("mobile_devices", "mailboxes", "users"),
    group="email",
    order=66,
    keywords="phones iphone android tablets activesync eas outlook mobile sync wipe quarantine",
    uses=("offboarding", "compromise"),
)
def devices_report(snap: TenantSnapshot, now: datetime, out: DataReport) -> None:
    found = snap.objects("mobile_devices", "mobile_device")
    if found is None:
        out.unavailable = NOT_COLLECTED
        return
    if _no_exchange(snap):
        out.unavailable = "This tenant has no Exchange Online licence."
        return
    users = {u["_id"]: u for u in snap.objects("users", "user") or []}

    every = Section(
        EVERY,
        [
            "Mailbox",
            "Device",
            "Platform",
            "Operating system",
            "App",
            "Access",
            "First synced",
            "Last synced",
            "Account",
        ],
        empty="No phone or tablet syncs mail with this tenant.",
    )
    attention = Section(
        ATTENTION,
        ["Mailbox", "Device", "Why", "Last synced"],
        empty="Nothing needs attention: no leaver's device still connected, none waiting for "
        "approval and none left unused for 90 days.",
    )
    by_platform: dict[str, list[int]] = {}
    needs: list[tuple[int, list, str]] = []
    active = blocked_users = stale = waiting = 0
    rows = sorted(found, key=lambda d: (_text(d.get("mailbox")) or _text(d.get("ownerName"))).lower())
    for d in rows:
        mailbox = _text(d.get("mailbox")) or _text(d.get("ownerName")) or "Unknown mailbox"
        user = users.get(_text(d.get("mailboxId")))
        last = parse_ts(d.get("LastSuccessSync"))
        last_day = last.date().isoformat() if last else ""
        days = (now - last).days if last else None
        access = _text(d.get("DeviceAccessState"))
        name, plat = _device(d), platform(d.get("DeviceOS"))
        reasons: list[tuple[str, str]] = []
        if user is not None and user.get("accountEnabled") is False:
            blocked_users += 1
            if days is not None and days < ACTIVE_DAYS:
                reasons.append(("high", "Still syncing mail although the person's sign-in is blocked"))
            else:
                reasons.append(("medium", "Belongs to someone whose sign-in is blocked"))
        if access == "Quarantined":
            waiting += 1
            reasons.append(("medium", "Waiting for an admin to allow or block it"))
        if d.get("DeviceWipeRequestTime") and not d.get("DeviceWipeAckTime"):
            reasons.append(("medium", "A wipe was requested but the device hasn't confirmed it"))
        if d.get("mailboxGone"):
            reasons.append(
                (
                    "low",
                    "Left over from a mailbox that no longer exists: remove it with "
                    "Remove-MobileDevice in Exchange Online PowerShell",
                )
            )
        if days is not None and days >= STALE_DAYS:
            stale += 1
            reasons.append(("low", f"Not synced for {days} days: the partnership can be removed"))
        if days is not None and days < ACTIVE_DAYS:
            active += 1
        counts = by_platform.setdefault(plat, [0, 0, 0, 0])
        counts[0] += 1
        counts[1 if d.get("ClientType") in ("Outlook", "REST") else 2] += 1
        counts[3] += days is not None and days < ACTIVE_DAYS
        flag = next((sev for sev in SEVERITY if any(s == sev for s, _ in reasons)), None)
        every.add(
            [
                mailbox,
                name,
                plat,
                _text(d.get("DeviceOS")),
                APPS.get(_text(d.get("ClientType")), _text(d.get("ClientType")) or "Unknown"),
                ACCESS.get(access, access or "Unknown"),
                _day(d.get("FirstSyncTime")),
                last_day,
                _account(user, bool(d.get("mailboxGone"))),
            ],
            flag,
        )
        if reasons:
            needs.append((SEVERITY.index(flag), [mailbox, name, [why for _, why in reasons], last_day], flag))

    for _, row, flag in sorted(needs, key=lambda n: n[0]):
        attention.add(row, flag)
    platforms = Section(
        PLATFORMS,
        ["Platform", "Devices", "Outlook app", "Mail app (ActiveSync)", f"Synced in the last {ACTIVE_DAYS} days"],
        empty="No devices.",
    )
    for plat, counts in sorted(by_platform.items(), key=lambda kv: -kv[1][0]):
        platforms.add([plat, *counts])

    n = len(found)
    mailboxes = len({_text(d.get("mailboxId")) for d in found})
    every.intro = (
        f"{plural(n, 'device')} on {plural(mailboxes, 'mailbox')}; {active} synced in the last "
        f"{ACTIVE_DAYS} days. A device keeps its partnership until it's removed, so old phones stay "
        "listed long after they're replaced."
    )
    attention.intro = (
        f"{plural(len(attention.rows), 'device')} {verb(len(attention.rows), 'needs', 'need')} a "
        "look. To remove a device someone no longer uses, or a leaver's, open the Exchange admin "
        "centre, Recipients > Mailboxes > the mailbox > Mobile devices, and remove it (or block it "
        "there first). Waiting devices are allowed or blocked in the same place."
    )
    platforms.intro = (
        "The Outlook app supports modern sign-in and app protection policies; the built-in mail "
        "app over ActiveSync is harder to control."
    )
    out.tiles = [
        Tile("Mobile devices", n, f"{active} synced in the last {ACTIVE_DAYS} days", rows=Rows(EVERY)),
        Tile(
            "Leavers' devices",
            blocked_users,
            "Sign-in blocked, device still connected",
            "bad" if blocked_users else "ok",
            Rows(EVERY, {"Account": "Sign-in blocked"}),
        ),
        Tile(
            "Waiting for approval",
            waiting,
            "Quarantined by ActiveSync access rules",
            "warn" if waiting else "ok",
            Rows(EVERY, {"Access": ACCESS["Quarantined"]}),
        ),
        Tile(
            f"Not synced in {STALE_DAYS} days",
            stale,
            "Old partnerships to remove",
            "warn" if stale else "ok",
            Rows(EVERY, age=("Last synced", "gt90")),
        ),
    ]
    out.sections = [attention, every, platforms]
