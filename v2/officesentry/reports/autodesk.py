"""Autodesk licences: a client's Autodesk subscriptions and seats (``collectors/autodesk.py``) beside their
Microsoft 365 accounts.

Only there once Autodesk is set up and the client has been given an Autodesk team (``_shown``). It leads with what
an account manager acts on: seats bought but not given to anyone, seats nobody has used in ``USAGE_DAYS`` days,
and seats still held by someone whose Microsoft 365 account is blocked or gone (a leaver). People are matched to
Microsoft 365 by their Autodesk email address against each account's sign-in name and address; an alias isn't
matched. When the team's admin hashed personal data in Autodesk Account nobody can be matched, and it says so.
Prices and cost stay with the licence prices; this report counts seats.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from .. import devices
from ..autodesk import setup
from ..autodesk.setup import USAGE_DAYS
from ..snapshot import TenantSnapshot
from ..text import plural
from .data_reports import DataReport, Rows, Section, Tile, metric_trend
from .registry import report

KEY = "autodesk-licences"
BLOCKED = "Sign-in blocked"
GONE = "No account"
HIDDEN = "Can't tell"  # Autodesk hashed the person's details
LEAVERS = [BLOCKED, GONE]
USED = f"Used in {USAGE_DAYS} days"
DAYS = f"Days used ({USAGE_DAYS} days)"


def _shown(conn, tenant) -> bool:
    """Once Autodesk is set up, for a client given an Autodesk team."""
    return setup.configured(conn) if tenant is None else setup.applies(conn, tenant)


def accounts(snap: TenantSnapshot) -> dict[str, dict] | None:
    """Microsoft 365 accounts by lower-cased sign-in name and address; None before Users was collected."""
    users = snap.objects("users", "user")
    if users is None:
        return None
    out: dict[str, dict] = {}
    for u in users:
        for key in ("userPrincipalName", "mail"):
            if isinstance(u.get(key), str) and u[key]:
                out.setdefault(u[key].lower(), u)
    return out


def account_state(seat: dict, found: dict[str, dict] | None) -> str:
    """Whether the seat's holder has a Microsoft 365 account that can sign in."""
    person = str(seat.get("person") or "")
    if "@" not in person:
        return HIDDEN
    if found is None:
        return ""
    user = found.get(person.lower())
    if user is None:
        return GONE
    return "Enabled" if user.get("accountEnabled") else BLOCKED


def used_since(snap: TenantSnapshot, now: datetime) -> str:
    """The first day that counts as recent use: ``USAGE_DAYS`` before the data was read."""
    end = devices.parse_ts(snap.as_of) or now
    return (end - timedelta(days=USAGE_DAYS)).date().isoformat()


@report(
    KEY,
    "Autodesk licences",
    "Every Autodesk subscription with its seats bought and given out, who holds each seat and when they last used "
    "it, and seats still held by people whose Microsoft 365 account is blocked or gone.",
    ("autodesk",),  # the add-on's alone, as add-on reports are; accounts come from Users, which every client has
    group="tenant",
    order=25,
    keywords="autodesk autocad revit civil 3d inventor fusion maya 3ds max aec collection named user seat "
    "subscription renewal unused unassigned reclaim leaver business success plan premium",
    shown=_shown,
)
def build(snap: TenantSnapshot, now: datetime, report: DataReport) -> None:
    conn, tenant = snap.conn, snap.tenant
    if not _shown(conn, tenant):  # wherever it's built from: a design's table, a rollup, a saved view
        report.unavailable = "Autodesk licences aren't read for this client."
        return
    teams = snap.objects("autodesk", "autodesk_team")
    if teams is None:
        report.unavailable = (
            "Autodesk licences haven't been read yet. They're read every night with the rest of the client's data."
        )
        return
    subs = sorted(
        (s for s in snap.objects("autodesk", "autodesk_subscription") or [] if isinstance(s, dict)),
        key=lambda s: (str(s.get("product") or "").lower(), str(s.get("subscription") or "")),
    )
    seats = sorted(
        (s for s in snap.objects("autodesk", "autodesk_seat") or [] if isinstance(s, dict)),
        key=lambda s: (str(s.get("person") or "").lower(), str(s.get("product") or "").lower()),
    )
    found = accounts(snap)
    since = used_since(snap, now)
    usage_read = bool(teams) and all(t.get("usage") == "read" for t in teams)
    several = len({s.get("team") for s in [*subs, *seats]}) > 1

    for t in teams:
        if t.get("usage") != "read":
            report.notes.append(
                f"When each seat was last used couldn't be read for {t.get('name') or 'a team'}: "
                + (str(t.get("usageError") or "Autodesk didn't return its usage report.")[:300])
            )
    if any(t.get("hashed") for t in teams):
        report.notes.append(
            "Autodesk hides people's names and addresses for "
            + ", ".join(sorted(str(t.get("name") or "a team") for t in teams if t.get("hashed")))
            + ", so their seats can't be matched to Microsoft 365 accounts. A team admin can show them in Autodesk "
            "Account (Reporting, Personal data)."
        )
    if found is None:
        report.notes.append("Microsoft 365 accounts haven't been collected yet, so leavers can't be told apart.")

    table = Section(
        "Subscriptions",
        ["Product", "Subscription", "Seats", "Assigned", "Not assigned", "Status", "Ends", "Team"],
        intro="Each Autodesk subscription, with its seats and how many are given to someone. A seat nobody holds "
        "is paid for and unused: give it to someone who needs it, or drop it at renewal.",
        empty="Autodesk lists no subscription for this client's teams.",
        optional=() if several else ("Team",),
    )
    unassigned = 0
    for s in subs:
        bought, given = s.get("seats"), s.get("assigned")
        spare = max(bought - given, 0) if isinstance(bought, int) and isinstance(given, int) else None
        lapsed = str(s.get("status") or "").lower() in ("expired", "inactive", "cancelled", "canceled", "terminated")
        if spare and not lapsed:
            unassigned += spare
        table.add(
            [
                s.get("product") or "Unnamed product",
                s.get("subscription") or "",
                bought,
                given,
                spare,
                s.get("status") or "",
                s.get("end") or "",
                s.get("teamName") or s.get("team") or "",
            ],
            "medium" if spare and not lapsed else None,
        )

    held = Section(
        "Seats",
        ["Person", "Product", "Last used", USED, "Microsoft 365 account", DAYS, "Assigned on", "Name", "Team"],
        intro=f"Who holds each Autodesk seat, when they last used it and on how many of the last {USAGE_DAYS} days. "
        "A seat held by someone whose Microsoft 365 account is blocked or gone is usually a leaver's: take it back "
        "in Autodesk Account.",
        empty="Nobody holds a seat in this client's Autodesk teams.",
        drifts=("Last used", DAYS),  # use moves every night; going unused (the Yes/No column) is a change
        optional=("Assigned on", "Name", "Team"),
        filter_columns=("Microsoft 365 account", USED),
    )
    leavers = unused = 0
    for s in seats:
        state = account_state(s, found)
        last = str(s.get("lastUsed") or "")
        recent = (last >= since) if usage_read else None
        if state in LEAVERS:
            leavers += 1
        if recent is False:
            unused += 1
        held.add(
            [
                s.get("person") or "",
                s.get("product") or "",
                last,
                recent,
                state,
                s.get("daysUsed"),
                s.get("assignedOn") or "",
                s.get("name") or "",
                s.get("teamName") or s.get("team") or "",
            ],
            "high" if state in LEAVERS else ("medium" if recent is False else None),
        )

    owned = sum(s["seats"] for s in subs if isinstance(s.get("seats"), int))
    report.tiles = [
        Tile("Seats", owned, plural(len(subs), "subscription"), trend=metric_trend(snap, "autodesk.seats", now)),
        Tile("Assigned", len(seats), "seats someone holds", rows=Rows("Seats")),
        Tile(
            "Not assigned",
            unassigned,
            "bought, but nobody holds them",
            tone="warn" if unassigned else "ok",
            rows=Rows("Subscriptions", flagged=True),
            trend=metric_trend(snap, "autodesk.unassigned", now),
        ),
        Tile(
            f"Not used in {USAGE_DAYS} days",
            unused if usage_read else "Not read",
            "held, but not opened" if usage_read else "Autodesk's usage report couldn't be read",
            tone=("warn" if unused else "ok") if usage_read else None,
            rows=Rows("Seats", {USED: "No"}) if usage_read else None,
        ),
        Tile(
            "Held by leavers",
            leavers,
            "Microsoft 365 account blocked or gone",
            tone="bad" if leavers else "ok",
            rows=Rows("Seats", {"Microsoft 365 account": LEAVERS}),
        ),
    ]
    report.sections = [table, held]
