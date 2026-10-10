"""Distribution lists: members, owners, who can send to them, and how much mail they get.

Reads ``collectors/distribution_lists.py``. Owners (ManagedBy) are names Exchange stores; they're matched to
mailboxes and accounts so a leaver who still owns a list shows up. Activity is Office Sentry's own count of
the message trace, kept per day since the list was first watched, so "no mail in 90 days" is only claimed
once a list has been watched that long.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from ..collectors.mobile_devices import mailbox_index
from ..devices import parse_ts
from ..snapshot import TenantSnapshot
from ..text import day, plural, verb
from .data_reports import DataReport, Rows, Section, Tile
from .mail_reports import _no_exchange
from .registry import report

NOT_COLLECTED = "Distribution lists haven't been collected yet. Refresh Distribution lists on the tenant page."
RECENT_DAYS = 30
UNUSED_DAYS = 90
MIN_WATCHED = 30  # days a list is watched before "no mail" counts as unused
BIG_OPEN_LIST = 50  # members above which a list open to outside senders is worth a look

EVERY = "Every list"
ATTENTION = "Lists that need attention"
MEMBERS = "Members"
SEVERITY = ("high", "medium", "low")
NO_OWNER = "No owner"
LEAVER_OWNERS = "Only leavers own it"
TYPES = {
    "MailUniversalDistributionGroup": "Distribution list",
    "MailUniversalSecurityGroup": "Mail-enabled security",
    "MailNonUniversalGroup": "Distribution list",
}
MEMBER_TYPES = {
    "UserMailbox": "Mailbox",
    "SharedMailbox": "Shared mailbox",
    "MailContact": "External contact",
    "MailUser": "Mail user",
    "GuestMailUser": "Guest",
    "MailUniversalDistributionGroup": "List",
    "MailUniversalSecurityGroup": "Security group",
    "DynamicDistributionGroup": "Dynamic list",
    "GroupMailbox": "Microsoft 365 group",
    "RoomMailbox": "Room",
    "EquipmentMailbox": "Equipment",
}


def _text(value) -> str:
    return value if isinstance(value, str) else ""


class _Owners:
    """Turns a list's ManagedBy entries into (name people read, account or None)."""

    def __init__(self, snap: TenantSnapshot):
        self.boxes = mailbox_index(snap.objects("mailboxes", "mailbox") or [])
        self.users: dict[str, dict] = {}
        for u in snap.objects("users", "user") or []:
            for key in ("_id", "userPrincipalName", "mail", "displayName"):
                if isinstance(u.get(key), str) and u[key]:
                    self.users.setdefault(u[key].lower(), u)

    def resolve(self, name: str) -> tuple[str, dict | None]:
        box = self.boxes.get(name.lower())
        user = self.users.get(_text((box or {}).get("ExternalDirectoryObjectId")).lower()) if box else None
        user = user or self.users.get(name.lower())
        label = _text((user or {}).get("userPrincipalName")) or _text((box or {}).get("PrimarySmtpAddress")) or name
        return label, user


def _activity(snap: TenantSnapshot) -> tuple[dict, dict | None]:
    status = (snap.objects("distribution_lists", "dl_activity_status") or [None])[0]
    rows = {r["_id"]: r for r in snap.objects("distribution_lists", "dl_activity") or []}
    return rows, status if isinstance(status, dict) else None


def _recent(entry: dict, now: datetime, days: int) -> int:
    since = (now - timedelta(days=days)).date().isoformat()
    counts = entry.get("days") if isinstance(entry.get("days"), dict) else {}
    return sum(n for d, n in counts.items() if isinstance(n, int) and isinstance(d, str) and d >= since)


@report(
    "distribution-lists",
    "Distribution lists",
    "Every distribution list with its members and owners, who can mail it, and empty, ownerless or unused lists.",
    ("distribution_lists", "mailboxes", "users"),
    group="email",
    order=64,
    keywords="dl dls distribution groups mailing lists email groups members owners inactive unused",
    uses=("offboarding", "storage"),
)
def lists_report(snap: TenantSnapshot, now: datetime, out: DataReport) -> None:
    lists = snap.objects("distribution_lists", "distribution_list")
    if lists is None:
        out.unavailable = NOT_COLLECTED
        return
    if _no_exchange(snap):
        out.unavailable = "This tenant has no Exchange Online licence."
        return
    owners = _Owners(snap)
    activity, status = _activity(snap)
    traced = bool(status and status.get("available"))

    every = Section(
        EVERY,
        [
            "List",
            "Address",
            "Type",
            "Members",
            "Owners",
            "External members",
            "Accepts mail from outside",
            f"Mail in the last {RECENT_DAYS} days",
            "Last mail received",
            "Synced from AD",
            "Created",
        ],
        empty="This tenant has no distribution lists.",
    )
    attention = Section(
        ATTENTION,
        ["List", "Why", "Members", "Owners", "Last mail received"],
        empty="Every list has members and an owner who can still sign in, and none looks unused.",
    )
    members = Section(MEMBERS, ["List", "Member", "Address", "Member type"], empty="No members were read.")
    empty = ownerless = unused = open_outside = 0
    needs: list[tuple[int, list, str]] = []
    watched_short: list[str] = []
    for g in sorted(lists, key=lambda g: (_text(g.get("DisplayName")) or _text(g.get("Name"))).lower()):
        name = _text(g.get("DisplayName")) or _text(g.get("Name")) or "Unnamed list"
        address = _text(g.get("PrimarySmtpAddress"))
        read = g.get("membersRead") is True
        count = g.get("memberCount") if read and isinstance(g.get("memberCount"), int) else None
        resolved = [owners.resolve(o) for o in g.get("ManagedBy") or [] if isinstance(o, str) and o]
        labels = [label for label, _ in resolved]
        able = [u for _, u in resolved if u is None or u.get("accountEnabled") is not False]
        outside = g.get("RequireSenderAuthenticationEnabled") is False
        entry = activity.get(address.lower()) if address else None
        entry = entry if isinstance(entry, dict) else None
        watched_from = parse_ts((entry or {}).get("watchedFrom"))
        watched = (now - watched_from).days if watched_from else 0
        last = parse_ts((entry or {}).get("last"))
        recent = _recent(entry, now, RECENT_DAYS) if entry and traced else None

        reasons: list[tuple[str, str]] = []
        if count == 0:
            empty += 1
            reasons.append(("medium", "No members"))
        if not resolved:
            ownerless += 1
            reasons.append(("medium", NO_OWNER))
        elif not able:
            ownerless += 1
            reasons.append(("medium", LEAVER_OWNERS))
        if traced and entry and watched >= MIN_WATCHED and not _recent(entry, now, min(watched, UNUSED_DAYS)):
            unused += 1
            span = min(watched, UNUSED_DAYS)
            reasons.append(("low", f"No mail in {span} days" + (f" (last {day(last.isoformat())})" if last else "")))
        elif traced and entry and watched < MIN_WATCHED:
            watched_short.append(name)
        if outside:
            open_outside += 1
            if (count or 0) >= BIG_OPEN_LIST:
                reasons.append(("low", f"Anyone outside can mail all {count} members"))
        flag = next((sev for sev in SEVERITY if any(s == sev for s, _ in reasons)), None)
        last_day = last.date().isoformat() if last else ""
        every.add(
            [
                name,
                address,
                TYPES.get(_text(g.get("RecipientTypeDetails")), "Distribution list"),
                "" if count is None else count,
                labels or "None",
                g.get("externalCount") if read and isinstance(g.get("externalCount"), int) else "",
                outside,
                "Not read" if recent is None else recent,
                last_day,
                bool(g.get("IsDirSynced")),
                (_text(g.get("WhenCreatedUTC")) or "")[:10],
            ],
            flag,
        )
        if reasons:
            needs.append(
                (
                    SEVERITY.index(flag),
                    [name, [why for _, why in reasons], "" if count is None else count, labels or "None", last_day],
                    flag,
                )
            )
        for m in g.get("members") or [] if read else []:
            if isinstance(m, dict):
                kind = _text(m.get("type"))
                members.add([name, _text(m.get("name")), _text(m.get("address")), MEMBER_TYPES.get(kind, kind)])
    for _, row, flag in sorted(needs, key=lambda n: n[0]):
        attention.add(row, flag)

    n = len(lists)
    every.intro = (
        f"{plural(n, 'distribution list')}. Lists that accept mail from outside can be mailed by anyone "
        "on the internet; most lists only need mail from colleagues."
    )
    attention.intro = (
        f"{plural(len(attention.rows), 'list')} {verb(len(attention.rows), 'needs', 'need')} a look. "
        "Mail to an empty list reaches nobody, a list without an owner (or owned only by people whose "
        "sign-in is blocked) has nobody keeping its members right, and a big list anyone outside can "
        "mail is a phishing target. "
        "Give each list an owner who still works here, and delete lists nobody uses (Exchange admin "
        "centre, Recipients > Groups > Distribution list). To stop outside mail, open the list's "
        'Settings and untick "Allow people outside of my organisation to send to this group".'
    )
    members.intro = (
        "Each list's direct members (the first 200 of a big list). A member that is itself a list "
        "passes mail on to its own members."
    )
    if not traced:
        out.notes.append(
            "How much mail each list gets comes from the message trace, which isn't available here"
            + (f": {status.get('reason')}" if status and status.get("reason") else " yet")
            + "."
        )
    elif watched_short:
        out.notes.append(
            f"{plural(len(watched_short), 'list')} {verb(len(watched_short), 'has', 'have')} been "
            f"watched for less than {MIN_WATCHED} days, so unused lists aren't flagged for "
            f"{verb(len(watched_short), 'it', 'them')} yet."
        )
    out.tiles = [
        Tile("Distribution lists", n, "Lists that pass mail on to their members", rows=Rows(EVERY)),
        Tile("Empty lists", empty, "No members", "warn" if empty else "ok", Rows(EVERY, {"Members": 0})),
        Tile(
            "Without an owner",
            ownerless,
            "No owner, or only leavers",
            "warn" if ownerless else "ok",
            Rows(ATTENTION, {"Why": [NO_OWNER, LEAVER_OWNERS]}),
        ),
        Tile(
            f"No mail in {UNUSED_DAYS} days",
            unused if traced else "Not read",
            "Lists that look unused" if traced else "Needs the message trace",
            ("warn" if unused else "ok") if traced else None,
            Rows(EVERY, {f"Mail in the last {RECENT_DAYS} days": 0}) if traced else None,
        ),
        Tile(
            "Open to outside senders",
            open_outside,
            "Anyone can mail these lists",
            rows=Rows(EVERY, {"Accepts mail from outside": "Yes"}),
        ),
    ]
    out.sections = [attention, every, members]
