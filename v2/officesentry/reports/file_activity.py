"""Files and mailbox access: the audit log add-on's report (``auditlog/``), from the activity history's
``activity`` store rather than a snapshot.

Only there while the add-on is switched on, and for a client only once the add-on was asked for there
(``_shown``). It covers the last ``DAYS`` days: what needs a look first (from ``operations.attention`` and mass
deletes spotted here), then a table per kind of activity, who did most, and OneDrive sync counted per person and
day. Its headline figures carry the trends the collector stores as metrics.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from urllib.parse import unquote, urlsplit

from .. import devices, events
from ..auditlog import addon, operations
from ..snapshot import TenantSnapshot
from .data_reports import DataReport, Link, Rows, Section, Tile, metric_trend
from .registry import report

KEY = "files-and-mailboxes"
DAYS = 30
MASS_DELETE = 100  # files one person deletes within an hour that make a mass delete
HIGH = {
    "An admin opened someone else's mailbox.",
    operations.FORWARDING_SET,
    "A rule forwards mail.",
    "The audit log was turned off.",
    "A mailbox or account was excluded from auditing.",
    "Mailbox auditing was turned off for the organisation.",
    "A copy of a mailbox was requested.",
    "Microsoft found malware in a file.",
}
TABLES = {  # operations.GROUPS key: (table title, columns, intro)
    "files": (
        "Files",
        ["When (UTC)", "What", "Who", "File", "Detail", "IP address"],
        "Files and folders deleted, restored, downloaded, moved and copied in SharePoint and OneDrive, newest first. "
        "Opening and editing files isn't listed.",
    ),
    "sharing": (
        "Sharing",
        ["When (UTC)", "What", "Who", "With", "Item", "Detail"],
        "Links created, used and removed, and people given or refused access to files and sites.",
    ),
    "mailbox": (
        "Mailboxes opened by someone else",
        ["When (UTC)", "What", "Who", "Mailbox", "Folders", "How"],
        "Mail read, changed or deleted by someone other than the mailbox's owner (an admin or a delegate), and mail "
        "sent as or on behalf of someone else. Owners reading their own mail aren't listed.",
    ),
    "rules": (
        "Inbox rules and forwarding",
        ["When (UTC)", "What", "Who", "Mailbox", "Rule", "What it does"],
        "Inbox rules created, changed or removed, mail forwarding set or removed, and mail flow rules.",
    ),
    "permissions": (
        "Mailbox and folder permissions",
        ["When (UTC)", "What", "Who", "Mailbox", "Given to", "Access"],
        "Mailbox, Send As and folder permissions granted or removed, and mailbox exports requested.",
    ),
    "audit": (
        "Audit settings",
        ["When (UTC)", "What", "Who", "Item", "Change"],
        "Changes to the audit log's own settings.",
    ),
}


def _shown(conn, tenant) -> bool:
    """While the add-on is on, for a client it was asked for (and so may hold its history)."""
    if not addon.enabled(conn):
        return False
    return tenant is None or addon.row_for(conn, tenant["id"]) is not None


def _when(ts: int) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%d %H:%M")


def _item(value: str):
    """A file's address as a link with its path for text; anything else as it is."""
    if value.lower().startswith("https://"):
        path = unquote(urlsplit(value).path) or value
        return Link(path, value)
    return value


def _mass_deletes(activities) -> list[tuple[int, str, int]]:
    """(start of the hour, who, files deleted) for each person and hour past ``MASS_DELETE``."""
    hours: Counter = Counter()
    for a in activities:
        if a.operation.lower() in operations.DELETES:
            hours[(a.ts - a.ts % 3600, a.actor)] += 1
    return sorted(((h, who, n) for (h, who), n in hours.items() if n >= MASS_DELETE), reverse=True)


def _rows(a: events.Activity) -> list:
    label = operations.label(a.operation)
    group = operations.group(a.operation)
    if group == "files":
        return [_when(a.ts), label, a.actor, _item(a.object), a.summary, a.ip]
    if group == "sharing":
        return [_when(a.ts), label, a.actor, a.owner, _item(a.object), a.summary]
    if group == "audit":
        return [_when(a.ts), label, a.actor, a.object, a.summary]
    item = "" if a.object.lower() == a.owner.lower() else a.object  # forwarding set on a mailbox names it twice
    return [_when(a.ts), label, a.actor, a.owner, item, a.summary]


@report(
    KEY,
    "Files and mailbox access",
    "Who deleted, downloaded and shared files, who opened someone else's mailbox, and inbox rules, forwarding "
    "and mailbox permissions set up, from the client's Microsoft 365 audit log.",
    ("audit_activity",),
    group="files",
    order=90,
    keywords="audit log unified purview deleted downloaded shared anyone link mailbox access forwarding inbox "
    "rule delegate full access send as mailitemsaccessed",
    shown=_shown,
)
def files_and_mailboxes(snap: TenantSnapshot, now: datetime, report: DataReport) -> None:
    conn, tenant = snap.conn, snap.tenant
    row = addon.row_for(conn, tenant["id"])
    if row is None:
        report.unavailable = "The audit log add-on isn't on for this client."
        return
    feed = (snap.objects("audit_activity", "audit_feed") or [None])[0]
    if feed is None and not row["started_at"]:
        report.unavailable = f"The audit log add-on isn't on for this client yet. {addon.state(row).detail}"
        return
    if feed is None:
        report.unavailable = (
            "Office Sentry hasn't read this client's audit log yet. Microsoft can take up to 12 hours after the "
            "feed is started to make the first records available; it's read every night."
        )
        return
    if row["stopped_at"]:
        report.notice = (
            f"The audit log add-on is off for this client: {row['stop_reason'] or 'switched off'} What was collected "
            "before stays until retention removes it."
        )

    end = devices.parse_ts(snap.as_of) or now
    start = end - timedelta(days=DAYS)
    since = devices.parse_ts(feed.get("historyFrom"))
    covered = f"the last {DAYS} days"
    if since and since > start:
        covered = f"the time since {since:%d %b %Y}, when Office Sentry started reading the audit log"
    activities = list(events.activity_between(conn, tenant["id"], start, end))
    for content_type, lo, hi in feed.get("lost") or []:
        report.notes.append(
            f"{content_type} records from {lo[:16].replace('T', ' ')} to {hi[:16].replace('T', ' ')} UTC couldn't be "
            "read: Microsoft only keeps them for 7 days, and Office Sentry didn't collect in that time."
        )

    attention = Section(
        "Needs attention",
        ["When (UTC)", "What", "Who", "Mailbox or person", "Item", "Why it matters"],
        compare=False,
        intro=f"What in {covered} is worth confirming with the client: mailboxes opened by an admin, forwarding, "
        "rules that forward or delete mail, anyone links, auditing turned off, mailbox exports, malware and mass "
        "deletes.",
        empty="Nothing in the window needs a second look.",
    )
    for hour, who, n in _mass_deletes(activities):
        attention.add(
            [_when(hour), "Mass delete", who, "", f"{n:,} files in an hour", f"{n:,} files deleted within an hour."],
            "high",
        )
    for a in activities:
        if a.attention:
            attention.add(
                [_when(a.ts), operations.label(a.operation), a.actor, a.owner, _item(a.object), a.attention],
                "high" if a.attention in HIGH else "medium",
            )
    attention.rows, attention.flags = _by_severity(attention)

    tables = {
        group: Section(
            title,
            columns,
            compare=False,
            intro=f"{intro} In {covered}.",
            empty="None in the window.",
            optional=("IP address",) if "IP address" in columns else (),
        )
        for group, (title, columns, intro) in TABLES.items()
    }
    people: dict[str, Counter] = defaultdict(Counter)
    for a in activities:
        group = operations.group(a.operation)
        if group in tables:
            tables[group].add(_rows(a), "medium" if a.attention else None)
        people[a.actor or "Unknown"][_column(a)] += 1

    sync = Section(
        "OneDrive sync",
        ["Day", "Who", "What", "Files"],
        compare=False,
        intro="Files OneDrive's sync app downloaded or uploaded whole, counted per person and day. A sudden "
        "large download can mean someone took a copy of a library.",
        empty="No sync downloads or uploads in the window.",
    )
    for day, who, operation, n in reversed(
        events.activity_counts(conn, tenant["id"], start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"))
    ):
        sync.add([day, who, operations.label(operation), n])
        people[who or "Unknown"]["Synced"] += n

    who = Section(
        "Who did most",
        ["Who", "Deleted", "Downloaded", "Synced", "Shared", "Mailboxes", "Rules and permissions"],
        compare=False,
        intro="Everyone who appears above, most active first.",
        empty="Nobody in the window.",
    )
    columns = who.columns[1:]
    for name, counts in sorted(people.items(), key=lambda kv: (-sum(kv[1].values()), kv[0].lower())):
        who.add([name, *(counts.get(c, 0) for c in columns)])

    figures = operations.figures(activities)
    rows = {
        "files_deleted": Rows("Files", {"What": sorted({operations.label(o) for o in operations.DELETES})}),
        "files_downloaded": Rows("Files", {"What": "File downloaded"}),
        "anyone_links": Rows("Sharing", {"What": "Anyone link created"}),
        "mailboxes_opened": Rows("Mailboxes opened by someone else"),
        "rules_changed": Rows(
            "Inbox rules and forwarding", {"What": sorted({operations.label(o) for o in operations.RULE_CHANGES})}
        ),
        "forwarding_set": Rows("Needs attention", {"Why it matters": operations.FORWARDING_SET}),
    }
    tones = {"anyone_links": "warn", "forwarding_set": "bad", "mailboxes_opened": "warn"}
    report.tiles = [
        Tile(
            title,
            figures[key],
            foot=f"Last {DAYS} days",
            tone=tones.get(key) if figures[key] else None,
            rows=rows[key],
            trend=metric_trend(snap, f"audit_activity.{key}", now),
        )
        for key, title in operations.FIGURES.items()
    ]
    report.sections = [attention, *tables.values(), who, sync]
    report.footnote = (
        "From the client's Microsoft 365 audit log, read every night through the audit log add-on. Email subjects "
        "are never stored."
    )


def _column(a: events.Activity) -> str:
    name, group = a.operation.lower(), operations.group(a.operation)
    if name in operations.DELETES:
        return "Deleted"
    if name == "filedownloaded":
        return "Downloaded"
    if group == "sharing":
        return "Shared"
    if group == "mailbox":
        return "Mailboxes"
    if group in ("rules", "permissions", "audit"):
        return "Rules and permissions"
    return "Other"


def _by_severity(section: Section) -> tuple[list, list]:
    order = {"high": 0, "medium": 1, None: 2}
    pairs = sorted(zip(section.rows, section.flags, strict=True), key=lambda p: order.get(p[1], 2))
    return [p[0] for p in pairs], [p[1] for p in pairs]
