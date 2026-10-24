"""Microsoft 365 usage and licence right-sizing reports.

Both read the per-person usage rows written by ``collectors/usage.py`` (through
``usage.py``). The usage report shows who uses each service; right-sizing
puts that next to the licences people hold and what the MSP pays for them.
"""

from __future__ import annotations

from collections import Counter
from datetime import date, datetime

from .. import devices, usage
from ..licences import is_stale, load_prices, sku_name
from ..snapshot import TenantSnapshot
from ..text import plural, verb
from ..usage import ACTIVE_DAYS, APPS, PLATFORMS, SERVICES
from .charts import line_chart
from .data_reports import DataReport, Highlight, Rows, Section, Tile
from .identity_reports import Money, _day, _parse
from .monthly_review import setting
from .registry import report

NOT_COLLECTED = 'Microsoft 365 usage hasn\'t been collected yet. Refresh "Microsoft 365 usage" on the client page.'
NO_PERMISSION = (
    "Office Sentry can't read this tenant's usage reports yet. It needs the Reports.Read.All "
    "permission: open the client page, send the consent link to the client's Global Admin again "
    "and ask them to accept."
)
MISSING = {
    "apps": "Microsoft's Office apps report couldn't be read, so desktop app use isn't known and isn't used for "
    "suggestions.",
    "activations": "Microsoft's Office activations report couldn't be read, so installs aren't listed.",
    "email": "Microsoft's email activity report couldn't be read, so email counts are missing.",
    "teams": "Microsoft's Teams activity report couldn't be read, so Teams counts are missing.",
}


def _load(snap: TenantSnapshot, report: DataReport) -> tuple[dict, dict[str, dict]] | None:
    """Usage status and rows, or None with ``report.unavailable`` set."""
    status = usage.status(snap)
    if status is None:
        report.unavailable = NOT_COLLECTED
        return None
    if not status.get("available"):
        report.unavailable = NO_PERMISSION
        return None
    if status.get("concealed"):
        report.notice = usage.CONCEALED_NOTICE
    for key, ok in (status.get("read") or {}).items():
        if not ok and key in MISSING:
            report.notes.append(MISSING[key])
    if status.get("reportDate"):
        report.footnote = (
            f"Usage covers the {ACTIVE_DAYS} days to {_day(status['reportDate'])}, the latest date "
            "Microsoft had processed. Microsoft's usage reports run two or three days behind."
        )
    return status, usage.people(snap)


def _when(value: str | None, licensed: bool = True) -> str:
    if value:
        return _day(value)
    return "Never" if licensed else ""


def _product(name: str) -> str:
    part = usage.part_for_product(name)
    return sku_name(part) if part else name.title()


def _share(part: int, whole: int) -> str:
    return f"{round(100 * part / whole)}%" if whole else "No one licensed"


def _office_licensed(row: dict) -> bool:
    parts = {usage.part_for_product(p) for p in row.get("products") or []}
    return bool(parts & (usage.MAIL_SUITES | set(usage.WITHOUT_DESKTOP)))


def _licensed(row: dict, key: str) -> bool:
    return _office_licensed(row) if key == "office" else bool((row.get("licensed") or {}).get(key))


def _platforms(row: dict, read: bool) -> str:
    platforms = row.get("platforms")
    if platforms is None:
        return "None" if read else "Not known"  # people missing from the apps report didn't use them
    used = [name for key, name in PLATFORMS if platforms.get(key)]
    return ", ".join(used) if used else "None"


def active_points(snap: TenantSnapshot) -> list[tuple[date, float]]:
    """Share of licensed people active in any service, per collection, from Office Sentry's own history."""
    people = dict(snap.history("usage.people"))
    points: dict[date, float] = {}
    for captured, active in snap.history("usage.active"):
        total = people.get(captured)
        if total:
            points[date.fromisoformat(captured[:10])] = round(100 * active / total, 1)
    return sorted(points.items())


# -- Usage ----------------------------------------------------------------------------------


@report(
    "usage",
    "Microsoft 365 usage",
    "Who actually uses email, Teams, OneDrive, SharePoint and the Office apps, and when each person was last active.",
    ("m365_usage", "users"),
    group="tenant",
    order=50,
    keywords="adoption activity active users teams onedrive outlook office apps copilot",
)
def usage_report(snap: TenantSnapshot, now: datetime, report: DataReport) -> None:
    loaded = _load(snap, report)
    if loaded is None:
        return
    status, rows = loaded
    read = status.get("read") or {}
    blocked = usage.blocked(snap)
    left_out = sum(1 for upn, r in rows.items() if upn in blocked and usage.licensed(r))
    rows = {upn: r for upn, r in rows.items() if upn not in blocked}
    if left_out:
        report.notes.append(
            f"{plural(left_out, 'disabled account')} still holding licences {verb(left_out, 'is', 'are')} left "
            f"out, as nobody can sign in to {verb(left_out, 'it', 'them')}. Licence waste counts "
            f"{verb(left_out, 'its', 'their')} licences."
        )
    licensed = [r for r in rows.values() if usage.licensed(r)]
    active = [r for r in licensed if usage.any_activity(r)]
    report.tiles.append(
        Tile(
            "Active people",
            _share(len(active), len(licensed)),
            f"{len(active)} of {len(licensed)} licensed used Microsoft 365",
            "ok" if len(active) == len(licensed) else "warn",
        )
    )

    by_service = Section(
        "Usage by service",
        ["Service", "Licensed", "Active", "Not active", "Active share"],
        intro=f"How many licensed people used each service in the last {ACTIVE_DAYS} days.",
        empty="Nobody is licensed for these services.",
    )
    for key, name in SERVICES:
        holders = [r for r in rows.values() if _licensed(r, key)]
        used = sum(1 for r in holders if (r.get("active") or {}).get(key))
        by_service.add([name, len(holders), used, len(holders) - used, _share(used, len(holders))])
        if key != "office":
            report.tiles.append(Tile(name, _share(used, len(holders)), f"{used} of {len(holders)} licensed"))
    if (status.get("read") or {}).get("apps"):
        # Only plans with the desktop apps: Business Basic, E1 and F3 people can't install them.
        desktop = [r for r in licensed if r.get("platforms") is not None and usage.desktop_licensed(r)]
        on_desktop = sum(1 for r in desktop if usage.desktop_use(r))
        report.tiles.append(
            Tile(
                "Desktop Office apps",
                _share(on_desktop, len(desktop)),
                f"{on_desktop} of {len(desktop)} with the desktop apps in their plan used them",
            )
        )

    last = Section(
        "Last active in each service",
        [
            "Person",
            "Last email",
            "Last Teams",
            "Last OneDrive",
            "Last SharePoint",
            "Last Office apps",
            "Office apps on",
            "Licences",
        ],
        intro=f"When each licensed person last used each service. Flagged: nothing used in the last "
        f"{ACTIVE_DAYS} days.",
        empty="Nobody holds a licence.",
    )
    for r in sorted(licensed, key=lambda r: usage.label(r).lower()):
        lasts = r.get("lastActive") or {}
        last.add(
            [usage.label(r)]
            + [_when(lasts.get(k), _licensed(r, k)) for k, _ in SERVICES]
            + [
                _platforms(r, bool(read.get("apps"))),
                [_product(p) for p in r.get("products") or [] if "FREE" not in p.upper()],
            ],
            None if usage.any_activity(r) else "low",
        )

    apps = Section(
        "Office apps by platform",
        ["App", "People"] + [name for _, name in PLATFORMS],
        intro=f"How many people used each Office app in the last {ACTIVE_DAYS} days, and where.",
        empty="Nobody used the Office apps."
        if (status.get("read") or {}).get("apps")
        else "Microsoft's Office apps report couldn't be read.",
    )
    for key, name in APPS:
        where = [r["apps"][key] for r in rows.values() if (r.get("apps") or {}).get(key) is not None]
        counts = Counter(p for platforms in where for p in platforms)
        if where:
            apps.add([name, len(where)] + [counts.get(p, 0) for p, _ in PLATFORMS])

    installs = Section(
        "Office installs",
        ["Person", "Product", "PCs", "Macs", "Phones and tablets", "Shared computer", "Last activated"],
        intro="Where each person has installed the Office apps. Microsoft allows five of each.",
        empty="No Office installs reported.",
    )
    for r in sorted(rows.values(), key=lambda r: usage.label(r).lower()):
        for a in r.get("activations") or []:
            installs.add(
                [
                    usage.label(r),
                    _product(a.get("product") or ""),
                    a.get("windows"),
                    a.get("mac"),
                    a.get("mobile"),
                    a.get("shared"),
                    _when(a.get("lastActivated"), False),
                ]
            )

    counts = Section(
        "Activity counts",
        ["Person", "Emails sent", "Emails received", "Teams messages", "Calls", "Meetings"],
        intro=f"What each person did in the last {ACTIVE_DAYS} days.",
        empty="No activity reported.",
        compare=False,
    )
    for r in sorted(licensed, key=lambda r: usage.label(r).lower()):
        # Someone missing from a report that was read did nothing there; a report that wasn't read leaves blanks.
        email = r.get("email") or ({"sent": 0, "received": 0} if read.get("email") else {})
        teams = r.get("teamsCounts") or ({"messages": 0, "calls": 0, "meetings": 0} if read.get("teams") else {})
        counts.add(
            [
                usage.label(r),
                email.get("sent", ""),
                email.get("received", ""),
                teams.get("messages", ""),
                teams.get("calls", ""),
                teams.get("meetings", ""),
            ]
        )
    report.sections = [by_service, last, apps, installs, counts]

    points = active_points(snap)
    if len(points) >= 2:
        report.chart = line_chart(points, label="Active people")
        report.chart_title = "Share of licensed people active, by collection"


# -- Right-sizing ---------------------------------------------------------------------------


@report(
    "licence-rightsizing",
    "Licence right-sizing",
    "People who could move to a cheaper licence, and services people are licensed for but don't use.",
    ("m365_usage", "users", "tenant_profile", "mailbox_usage", "devices", "admin_roles"),
    group="tenant",
    order=40,
    keywords="licenses downgrade cheaper plan savings cost copilot business basic standard e1 e3",
)
def rightsizing(snap: TenantSnapshot, now: datetime, report: DataReport) -> None:
    loaded = _load(snap, report)
    if loaded is None:
        return
    status, rows = loaded
    skus = snap.objects("tenant_profile", "subscribed_sku")
    users = snap.objects("users", "user")
    sku_parts = {s["skuId"]: s.get("skuPartNumber") or "" for s in skus or [] if s.get("skuId")}
    by_upn = {(u.get("userPrincipalName") or "").lower(): u for u in users or []}
    boxes = snap.objects("mailbox_usage", "mailbox_size")
    # A mailbox is the account's by its directory id; its address needn't be the sign-in name.
    by_id = {b["mailboxId"]: b for b in boxes or [] if b.get("mailboxId")}
    by_mail = {(b.get("mailbox") or "").lower(): b for b in boxes or []}
    prices = load_prices(snap.conn, snap.tenant)
    currency = setting(snap.conn, "currency", "GBP")
    # People who rely on a security suite's Intune and Entra ID P1: a device Intune manages, or an admin role.
    managed = {
        d["userPrincipalName"].lower()
        for d in devices.objects(snap, "managed_device") or []
        if isinstance(d.get("userPrincipalName"), str)
    }
    admins = {
        r.get("principalId")
        for r in snap.objects("admin_roles", "role_assignment") or []
        if r.get("principalType") == "user"
    }

    if users is None or skus is None:
        report.notes.append(
            "Users and licences haven't been collected yet, so licences are read from Microsoft's "
            "usage report and accounts that have left aren't skipped."
        )
    if boxes is None:
        report.notes.append(
            "Mailbox sizes haven't been collected, so Exchange Online Plan 2 isn't checked and "
            "mail-only suggestions assume Plan 1's 50 GB is enough."
        )

    moves = Section(
        "Could move to a cheaper licence",
        ["Person", "Has", f"Used in the last {ACTIVE_DAYS} days", "Could move to", "Saving / month", "Why"],
        intro="People whose use of Microsoft 365 fits a cheaper licence. Accounts that are disabled or "
        "haven't signed in for 90 days are in Licence waste instead, so they aren't counted twice. Someone with "
        "a device Intune manages or an admin role keeps a plan with Intune and Entra ID P1, and a mailbox that's "
        "large, archived or on hold keeps a plan that holds it.",
        empty="Everyone uses what their licence is for.",
    )
    unused = Section(
        "Licensed but not used",
        ["Person", "Licences", f"Not used in the last {ACTIVE_DAYS} days", "Uses"],
        intro="People who use Microsoft 365 but not every service their licence includes. Useful for "
        "adoption and training, and to spot licences bought for one service.",
        empty="Everyone uses every service they're licensed for.",
    )
    found, unpriced, people_moving = [], set(), set()
    withheld = 0  # people with hidden names whose licence would have been removed
    for upn, r in sorted(rows.items(), key=lambda x: usage.label(x[1]).lower()):
        user = by_upn.get(upn) if users is not None else None
        if user is not None and (
            not user.get("accountEnabled") or user.get("userType") == "Guest" or is_stale(user, now)
        ):
            continue
        created = _parse((user or {}).get("createdDateTime"))
        if created and (now - created).days < ACTIVE_DAYS:
            continue  # too new to judge
        holder = usage.paid_parts(r, user if users is not None and skus is not None else None, sku_parts)
        if not holder.parts:
            continue
        who = usage.label(r)
        uses = ", ".join(usage.used_services(r)) or "Nothing"
        box = (by_id.get(user["_id"]) if user is not None else None) or by_mail.get(upn)
        secured = upn in managed or (user is not None and user.get("_id") in admins)
        suggestions = usage.rightsize(r, holder.parts, box, relies_on_security=secured)
        if r.get("concealed"):
            # A hidden name can't be matched to its account, so a disabled account or a leaver, whose licences
            # Licence waste already counts, can't be told apart: removing a licence would count it twice.
            kept = [s for s in suggestions if s.target != ""]
            withheld += len(kept) < len(suggestions)
            suggestions = kept
        for s in suggestions:
            saving = s.saving(prices)
            if saving == 0 and s.target:
                continue  # the cheaper plan isn't cheaper at these prices
            if saving is None and s.target is not None:
                unpriced.update(p for p in (s.current, s.target) if p and p not in prices)
            people_moving.add(upn)
            why = s.why + (f". {s.caution}" if s.caution else "")
            found.append(
                (
                    -(saving or 0),
                    who,
                    [
                        who,
                        sku_name(s.current),
                        uses,
                        s.action,
                        Money(saving, currency)
                        if saving is not None
                        else ("Not priced" if s.target is not None else ""),
                        why,
                    ],
                    "medium" if saving or s.target == "" else "low",
                )
            )
        if usage.any_activity(r):
            missing = [name for key, name in SERVICES if _licensed(r, key) and not (r.get("active") or {}).get(key)]
            if missing:
                unused.add([who, [sku_name(p) for p in holder.parts], missing, uses])
    for _, _, row, flag in sorted(found, key=lambda x: (x[0], x[1].lower())):
        moves.add(row, flag)
    report.sections = [moves, unused]

    if withheld:
        report.notes.append(
            "Microsoft hides names in this tenant's usage reports, so disabled accounts and leavers can't be told "
            f"apart from the rest: removing a licence isn't suggested for {plural(withheld, 'person', 'people')}, "
            "since Licence waste already counts licences on disabled and inactive accounts."
        )
    if unpriced:
        report.notes.append(
            "Some suggestions have no price, so their saving isn't counted. An admin can add prices "
            f"for {', '.join(sorted(sku_name(p) for p in unpriced))} on the Licence prices page."
        )
    total = round(sum(-x[0] for x in found), 2)
    priced = any(isinstance(row[4], Money) for _, _, row, _ in found)
    report.highlights = [
        Highlight(
            "Saving / month",
            Money(total, currency) if priced or not found else "Not priced",
            "From the suggestions below, at your prices",
            "warn" if total else None,
            Rows(moves.title),
        ),
        Highlight(
            "Saving / year",
            Money(total * 12, currency) if priced or not found else "Not priced",
            "If every suggestion is taken",
            rows=Rows(moves.title),
        ),
        Highlight(
            "People who could move",
            len(people_moving),
            "To a cheaper licence, or none",
            "warn" if people_moving else "ok",
            Rows(moves.title),
        ),
        Highlight("Not using a service", len(unused.rows), f"In the last {ACTIVE_DAYS} days", rows=Rows(unused.title)),
    ]
