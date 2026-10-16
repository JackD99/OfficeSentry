"""File and folder permissions: who can open each shared file or folder, how, since when, and until when.

Built from the ``shared_files`` scan (``collectors/sharepoint.py``): every file and folder with permissions
of its own, each way in (a person, group or guest given access directly, each person a link was sent to, an
anyone or organisation-wide link), who shared it and when, and when links expire. The Shared files report
answers "what's shared outside"; this one answers "who can access what".
"""

from __future__ import annotations

from datetime import datetime, timedelta

from ..devices import parse_ts
from ..snapshot import TenantSnapshot
from ..text import nouns
from .data_reports import Chart, DataReport, Rows, Section, Tile, _date, metric_trend
from .registry import report
from .sharepoint import DENIED_TEXT, _item_cell, _location

NEW_DAYS = 30
EXPIRING_DAYS = 30
MAX_GRANT_ROWS = 10000

KINDS = {
    "person": "Member",
    "group": "Group",
    "guest": "Guest or outside address",
    "anyone": "Anyone with the link",
    "organisation": "Everyone in the organisation",
}


def _grants(item: dict) -> list[dict]:
    return [g for g in item.get("grants") or [] if isinstance(g, dict) and g.get("who")]


def _days_left(expires: str | None, now: datetime) -> int | None:
    """Calendar days until the expiry date, as people count them: a link ending tomorrow morning has 1."""
    when = parse_ts(expires)
    return None if when is None else (when.date() - now.date()).days


def _expired(g: dict, now: datetime) -> bool:
    when = parse_ts(g.get("expires"))
    return when is not None and when < now


def _shared_with(item: dict) -> list[str]:
    """A short list of who an item is shared with, for the newly shared table."""
    names = []
    for g in _grants(item):
        name = g["who"] + (" (outside)" if g.get("outside") and g.get("kind") == "guest" else "")
        if name not in names:
            names.append(name)
    return names


def _flag(g: dict, now: datetime) -> str | None:
    if _expired(g, now):
        return None  # it opens nothing any more
    if g.get("kind") == "anyone":
        return "high" if g.get("can") == "Edit" else "medium"
    if g.get("outside"):
        return "medium" if g.get("can") == "Edit" else "low"
    return None


@report(
    "file-permissions",
    "File and folder permissions",
    "Who can open each shared file or folder, how they got access, who shared it and when links expire.",
    ("shared_files", "users"),
    group="files",
    order=45,
    keywords="unique permissions access grants who can access expiry expired guests shared by newly shared",
    uses=("sharing", "offboarding"),
)
def file_permissions(snap: TenantSnapshot, now: datetime, report: DataReport) -> None:
    status = (snap.objects("shared_files", "scan_status") or [None])[0]
    if status is None:
        report.unavailable = 'Shared files haven\'t been scanned yet. Refresh "Shared files" on the client page.'
        return
    if not status.get("available"):
        report.unavailable = (
            "This tenant has no SharePoint licence."
            if status.get("reason") == "no_sharepoint"
            else DENIED_TEXT
            if status.get("reason") == "denied"
            else "Office Sentry can't read this tenant's files yet. It needs the Sites.Read.All "
            "permission: open the client page, send the consent link to the client's Global "
            "Admin again and ask them to accept. The other SharePoint reports work without it."
        )
        return
    items = [i for i in snap.objects("shared_files", "shared_item") or [] if isinstance(i, dict)]
    if items and not any("grants" in i for i in items):
        report.notice = (
            "The last scan was made by an earlier version, which only recorded what's shared outside. The next "
            'Shared files collection records every file and folder with its own permissions; refresh "Shared '
            'files" on the client page to see them now.'
        )
    outside_people = {
        g["who"]
        for i in items
        for g in _grants(i)
        if g.get("outside") and g.get("kind") == "guest" and not _expired(g, now)
    }
    new_since = (now - timedelta(days=NEW_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")

    def shared_on(i: dict) -> str | None:
        return i.get("sharedAt") or i.get("firstSeen")

    new = [i for i in items if (shared_on(i) or "") >= new_since]
    # One row per link (or direct grant) with an expiry, however many people it was sent to.
    ends: dict[tuple, tuple[dict, dict, int, list[str]]] = {}
    for i in items:
        for g in _grants(i):
            days = _days_left(g.get("expires"), now)
            if days is not None and days <= EXPIRING_DAYS:
                key = (i.get("_id") or i.get("webUrl") or i.get("path"), g.get("how"), g.get("can"), g["expires"])
                ends.setdefault(key, (i, g, days, []))[3].append(g["who"])
    expiring = list(ends.values())
    expired = [e for e in expiring if _expired(e[1], now)]
    report.tiles = [
        Tile(
            "Files and folders with their own permissions",
            len(items),
            "Shared, or given access beyond their site",
            rows=Rows("Who can access what"),
            trend=metric_trend(snap, "sharing.unique_items", now),
        ),
        Tile(
            "People outside with access",
            len(outside_people),
            "Guests and outside addresses",
            "warn" if outside_people else None,
            Rows("Access by person", {"Type": KINDS["guest"]}),
            metric_trend(snap, "sharing.external_people", now),
        ),
        Tile(
            f"Newly shared in {NEW_DAYS} days",
            len(new),
            "By Microsoft's sharing date",
            rows=Rows("Newly shared"),
        ),
        Tile(
            "Links past their expiry",
            len(expired),
            "Still listed; remove them",
            "warn" if expired else None,
            Rows("Links expiring", {"Status": "Expired"}),
        ),
        Tile(
            f"Expiring in {EXPIRING_DAYS} days",
            len(expiring) - len(expired),
            "Extend or let them lapse",
            rows=Rows("Links expiring", {"Status": "Expires soon"}),
        ),
    ]
    scanned = status.get("items") or 0
    full = status.get("full_scans")
    incremental = (status.get("scanned_drives") or 0) - (full or 0) if full is not None else 0
    report.footnote = (
        f"{len(items):,} {nouns(len(items), 'file or folder')} with permissions of their own, from {scanned:,} "
        f"{nouns(scanned, 'file or folder')} read in {status.get('drives') or 0} libraries and OneDrives"
        + (
            f" ({incremental} asked for changes only since the last scan, with {status.get('kept') or 0:,} "
            "unchanged items kept)"
            if incremental
            else ""
        )
        + ". A shared folder is listed once; the files inside it are shared the same way. "
        "Files and folders that only inherit their site's permissions aren't listed; the Site access report "
        "covers those."
    )

    fresh = Section(
        "Newly shared",
        ["File or folder", "Location", "Shared with", "Shared by", "Shared on"],
        intro=f"Shared in the last {NEW_DAYS} days, newest first, by Microsoft's sharing date (or when Office "
        "Sentry first saw the item shared). Check each one was meant.",
        empty=f"Nothing new was shared in the last {NEW_DAYS} days.",
        compare=False,
    )
    for i in sorted(new, key=lambda i: (shared_on(i) or "", i.get("path") or ""), reverse=True):
        fresh.add(
            [_item_cell(i), _location(i), _shared_with(i), i.get("sharedBy") or "", _date(shared_on(i))],
            "medium" if i.get("exposure") == "anyone" else ("low" if i.get("externalPeople") else None),
        )

    ending = Section(
        "Links expiring",
        ["File or folder", "Location", "Who", "Can", "Expires", "Days left", "Status"],
        intro=f"Sharing that has expired or expires within {EXPIRING_DAYS} days. Expired links no longer open "
        "anything but still show on the file; remove them so the list stays honest.",
        empty=f"No sharing expires in the next {EXPIRING_DAYS} days.",
        filter_columns=("Status",),
    )
    for i, g, days, people in sorted(expiring, key=lambda e: (e[2], e[0].get("path") or "")):
        gone = _expired(g, now)
        ending.add(
            [
                _item_cell(i),
                _location(i),
                people,
                g.get("can") or "",
                _date(g.get("expires")),
                "" if gone else max(days, 0),  # an expired link has no days left, rather than 0
                "Expired" if gone else "Expires soon",
            ],
            "low" if gone else None,
        )

    who = Section(
        "Who can access what",
        ["File or folder", "Location", "Who", "How", "Can", "Outside", "Shared by", "Shared on", "Expires"],
        intro="One row per person, group or link with access to each shared file or folder, grouped by "
        "where it lives. Direct means they were given access on the item itself; a link means they "
        "were sent one.",
        empty="No file or folder has permissions of its own.",
        group="Location",
        filter_columns=("How", "Can", "Outside"),
    )
    who.chart = Chart("How", title="Ways in")
    rows = 0
    cut = False
    for i in sorted(items, key=lambda i: (_location(i), i.get("path") or "")):
        for g in _grants(i):
            if rows >= MAX_GRANT_ROWS:
                cut = True
                break
            rows += 1
            who.add(
                [
                    _item_cell(i),
                    _location(i),
                    g["who"],
                    g.get("how") or "",
                    g.get("can") or "",
                    bool(g.get("outside")),
                    i.get("sharedBy") or "",
                    _date(shared_on(i)),
                    _date(g.get("expires")) or "Never",
                ],
                _flag(g, now),
            )
    if cut:
        who.intro += f" Showing the first {MAX_GRANT_ROWS:,} rows; the XLSX export has every one."

    per: dict[str, dict] = {}
    for i in items:
        for g in _grants(i):
            if g.get("kind") in ("anyone", "organisation") or _expired(g, now):
                continue
            p = per.setdefault(
                g["who"].lower(),
                {"who": g["who"], "kind": g.get("kind"), "items": set(), "edit": set(), "places": set()},
            )
            p["items"].add(i.get("_id") or i.get("webUrl") or i.get("path"))
            if g.get("can") in ("Edit", "Full control"):
                p["edit"].add(i.get("_id") or i.get("webUrl") or i.get("path"))
            p["places"].add(_location(i))
    people = Section(
        "Access by person",
        ["Person or group", "Type", "Files and folders", "Of them, can edit", "Where"],
        intro="Everyone with item-level access, outside people first, with how many files and folders each "
        "can open and how many of those they can edit. Someone who's left or a guest whose work is done "
        "shouldn't still be here.",
        empty="Nobody has item-level access.",
        filter_columns=("Type",),
    )
    people.chart = Chart("Type", ("Files and folders",), title="Item-level access by type of person")
    for p in sorted(per.values(), key=lambda p: (p["kind"] != "guest", -len(p["items"]), p["who"])):
        people.add(
            [p["who"], KINDS.get(p["kind"], "Member"), len(p["items"]), len(p["edit"]), sorted(p["places"])],
            "medium" if p["kind"] == "guest" and p["edit"] else ("low" if p["kind"] == "guest" else None),
        )
    report.sections = [fresh, ending, who, people]
