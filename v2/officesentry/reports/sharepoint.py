"""SharePoint, OneDrive and shared-file detail reports.

Builders read the snapshots written by ``collectors/sharepoint.py`` and fill a
``DataReport``; the page and the XLSX export only format them.
"""

from __future__ import annotations

import math
from datetime import date, datetime, timedelta

from .. import formatting
from ..collectors.sharepoint import anyone_can_edit, outside_can_edit
from ..collectors.sharepoint_lookup import DENIED_TEXT, is_system_site
from ..devices import parse_day
from ..snapshot import TenantSnapshot
from ..storage import tenant_storage
from ..text import plural
from ..usage import owner_label
from .charts import line_chart
from .data_reports import Chart, DataReport, Link, Rows, Section, Tile, _date, metric_trend
from .registry import report

INACTIVE_DAYS = 90
GROWTH_DAYS = 30

CONCEALED_NOTICE = (
    "Microsoft is hiding site and people's names in this tenant's usage reports, so they show as codes. "
    "To show names: sign in to admin.microsoft.com as a Global Admin, open Settings, then Org settings, "
    'then Reports on the Services tab. Untick "Display concealed user, group, and site names in all '
    'reports" and save. Names appear after the next collection.'
)

TEMPLATES = {
    "Group": "Group or Team site",
    "Team Site": "Team site (no group)",
    "Site Page Publishing": "Communication site",
    "Team Channel": "Teams channel site",
    "Publishing Site": "Publishing site",
}


SHARED_SITES = Rows("Sites shared outside the organisation")


def size(n: int | float | None) -> str:
    if n is None:
        return ""
    for unit in ("bytes", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            if unit == "bytes":
                return f"{n:.0f} {unit}"
            # Two decimals under 10 TB, so "1.28 TB of 1.45 TB" adds up with "178.1 GB free" next to it.
            return f"{n:.2f} {unit}" if unit == "TB" and abs(n) < 10 else f"{n:.1f} {unit}"
        n /= 1024
    return ""


def _signed_size(n: int | None) -> str:
    if n is None:
        return ""
    if n == 0:
        return "No change"
    return ("+" if n > 0 else "−") + size(abs(n))


def _days_since(value: str | None, now: datetime) -> int | None:
    day = parse_day(value)
    return None if day is None else (now.date() - day).days


def site_type(row: dict) -> str:
    template = row.get("template") or ""
    if is_system_site(row.get("url"), template):
        return "System"
    return TEMPLATES.get(template, template or "Unknown")


def url_key(url) -> str:
    """A site address as compared everywhere: lower case, no trailing slash."""
    return (url if isinstance(url, str) else "").lower().rstrip("/")


def site_names(snap: TenantSnapshot) -> dict[str, str]:
    """Site names by address (``url_key``), from SharePoint storage. Microsoft's usage report, which lists the
    sites, only has their addresses."""
    return {
        url_key(r.get("url")): r["site"]
        for r in snap.objects("sharepoint_storage", "site_storage") or []
        if r.get("url") and isinstance(r.get("site"), str) and r["site"]
    }


def site_name(row: dict, names: dict[str, str] | None = None) -> str:
    """A site's name: SharePoint storage has it (``names``); otherwise the last part of its address, which reads
    as a name ("/sites/finance-team" is "Finance Team"); the root site and admin sites keep their address."""
    url = row.get("url") if isinstance(row.get("url"), str) else ""
    key = url_key(url)
    if name := (names or {}).get(key):
        return name
    if "/sites/" in key or "/teams/" in key:
        last = url.rstrip("/").rsplit("/", 1)[-1].replace("-", " ").replace("_", " ")
        return last.title() if last == last.lower() else last
    return url


def site_label(row: dict, names: dict[str, str] | None = None) -> str:
    if row.get("url"):
        return site_name(row, names)
    return f"Name hidden by Microsoft ({site_type(row).lower()})"


def site_cell(row: dict, names: dict[str, str] | None = None):
    """The site as a cell: its name, opening the site in SharePoint (or its page, through ``link_pages``)."""
    label = site_label(row, names)
    return Link(label, row["url"]) if row.get("url") else label


def _objects_before(snap: TenantSnapshot, collector: str, kind: str, before: datetime) -> dict[str, dict] | None:
    """Objects from the latest successful run at or before ``before`` (for growth), read from the daily history
    once retention has removed its snapshot."""
    from .. import state_history  # here: the history imports the worker's schedule

    row = snap.conn.execute(
        "SELECT id FROM runs WHERE tenant_id = ? AND collector = ? AND status IN ('succeeded', 'partial')"
        " AND finished_at <= ? ORDER BY finished_at DESC, id DESC LIMIT 1",
        (snap.tenant["id"], collector, before.strftime("%Y-%m-%dT%H:%M:%SZ")),
    ).fetchone()
    if row is None:
        return None
    return {o["_id"]: o for o in state_history.objects_of(snap.conn, row["id"], kind)}


def storage_points(snap: TenantSnapshot, area: str) -> list[tuple[date, float]]:
    """Daily storage in bytes: Microsoft's own daily figures (its last 180 days) where it has them, and Office
    Sentry's history from before they start. The two are counted differently (Office Sentry's leaves out deleted
    sites and OneDrives, and the demo's are its own), so taking them day by day in turn made steps that read as
    growth."""
    own = {date.fromisoformat(captured[:10]): value for captured, value in snap.history(f"{area}.storage_bytes")}
    microsoft: dict[date, float] = {}
    for h in snap.objects("sharepoint_usage", "storage_history") or []:
        if h["_id"] == area:
            for day, value in h.get("points") or []:
                microsoft[date.fromisoformat(day)] = value
    if len(microsoft) < 2:
        return sorted({**own, **microsoft}.items())
    start = min(microsoft)
    return sorted({**{d: v for d, v in own.items() if d < start}, **microsoft}.items())


def storage_chart(
    points: list[tuple[date, float]], label: str, now: datetime | None = None, quota: float | None = None
) -> str:
    """Storage over time. With ``now``, the line carries on dashed at the last ``GROWTH_DAYS`` days' rate (to
    when it reaches ``quota``, at most a year ahead, or three months without a quota), and the quota is a line."""
    if len(points) < 2:
        return ""
    ahead = forecast(points, now, quota) if now else []
    top = max([v for _, v in points] + [v for _, v in ahead] + ([quota] if quota else []))
    unit, div = next(
        ((u, d) for u, d in ((" TB", 1024**4), (" GB", 1024**3), (" MB", 1024**2)) if top >= d), (" MB", 1024**2)
    )
    digits = 2 if top / div < 10 else 1
    scaled = [(d, round(v / div, digits)) for d, v in points]
    return line_chart(
        scaled,
        label=label,
        unit=unit,
        y_max=nice_max(top / div),
        forecast=[(d, round(v / div, digits)) for d, v in ahead],
        limit=(round(quota / div, digits), "Storage limit") if quota else None,
    )


def forecast(points: list[tuple[date, float]], now: datetime, quota: float | None = None) -> list[tuple[date, float]]:
    """Where storage is heading at the last ``GROWTH_DAYS`` days' rate: the point it reaches ``quota`` within a
    year, else a year ahead (three months without a quota). None when it isn't growing or the history is short."""
    growth = _growth(points, now)
    if not growth or growth <= 0:
        return []
    last_day, last = points[-1]
    per_day = growth / GROWTH_DAYS
    days = 365 if quota else 91
    if quota and last < quota:
        days = min(days, max(math.ceil((quota - last) / per_day), 1))
    elif quota:
        return []
    return [(last_day + timedelta(days=days), last + per_day * days)]


def nice_max(value: float) -> float:
    """An axis top above ``value`` whose half is a whole number (the chart labels 0, half and top)."""
    step = 2 if value < 20 else (10 if value < 200 else 100)
    return max(step, step * -(-value * 1.1 // step))


def _growth(points: list[tuple[date, float]], now: datetime) -> float | None:
    if not points:
        return None
    cutoff = now.date() - timedelta(days=GROWTH_DAYS)
    earlier = [v for d, v in points if d <= cutoff]
    return points[-1][1] - earlier[-1] if earlier else None


def _usage(snap: TenantSnapshot, report: DataReport) -> dict | None:
    """Usage status, setting ``report.unavailable`` or the concealed notice as needed."""
    status = (snap.objects("sharepoint_usage", "usage_status") or [None])[0]
    if status is None:
        report.unavailable = (
            'SharePoint usage hasn\'t been collected yet. Refresh "SharePoint and OneDrive usage" on the client page.'
        )
        return None
    if not status.get("available"):
        report.unavailable = "This tenant has no SharePoint licence."
        return None
    if status.get("concealed"):
        report.notice = CONCEALED_NOTICE
    return status


def _as_of(status: dict) -> str:
    when = formatting.day(status.get("reportDate"))
    return f" Microsoft's figures as of {when}." if when else ""


# -- SharePoint sites ---------------------------------------------------------------


@report(
    "sharepoint",
    "SharePoint sites",
    "Storage and growth, inactive sites, and which sites are shared outside.",
    ("sharepoint_usage",),
    group="files",
    order=10,
    keywords="sites storage sharing anyone links",
)
def sites(snap: TenantSnapshot, now: datetime, report: DataReport) -> None:
    status = _usage(snap, report)
    if status is None:
        return
    rows = [
        {
            **_counts(r, "anyoneLinks", "guestLinks", "companyLinks", "files", "storageUsed"),
            "externalSharing": r.get("externalSharing") or "Unknown",
        }
        for r in snap.objects("sharepoint_usage", "site") or []
        if not r.get("deleted")
    ]
    before = _objects_before(snap, "sharepoint_usage", "site", now - timedelta(days=GROWTH_DAYS - 2))
    names = site_names(snap)
    user_sites = [r for r in rows if site_type(r) != "System"]

    def inactive(r):
        days = _days_since(r.get("lastActivityDate"), now)
        return days is None or days >= INACTIVE_DAYS

    points = storage_points(snap, "sharepoint")
    growth = _growth(points, now)
    total = sum(r["storageUsed"] for r in rows)
    anyone = sum(r["anyoneLinks"] for r in rows)
    guest = sum(r["guestLinks"] for r in rows)
    stale = [r for r in user_sites if inactive(r)]
    report.tiles = [
        Tile(
            "Sites",
            len(user_sites),
            f"plus {plural(len(rows) - len(user_sites), 'system site')}" if len(rows) > len(user_sites) else "",
            rows=Rows("All sites", {"Type": sorted({site_type(r) for r in user_sites})}),
        ),
        Tile(
            "Storage used",
            size(total),
            (f"{_signed_size(growth)} in {GROWTH_DAYS} days" if growth is not None else ""),
            rows=Rows("All sites"),
            trend=metric_trend(snap, "sharepoint.storage_bytes", now, size),
        ),
        Tile(
            "Anyone links",
            anyone,
            "No sign-in needed to open",
            "bad" if anyone else "ok",
            SHARED_SITES,
            metric_trend(snap, "sharepoint.links.anyone", now),
        ),
        Tile(
            "Guest links",
            guest,
            "Shared with named people outside",
            rows=SHARED_SITES,
            trend=metric_trend(snap, "sharepoint.links.guest", now),
        ),
        Tile("Inactive sites", len(stale), f"No activity in {INACTIVE_DAYS} days", "warn" if stale else None),
    ]
    _, quota = tenant_storage(snap)
    report.chart = storage_chart(points, "SharePoint storage over time", now, quota.total if quota else None)
    report.chart_title = "SharePoint storage over time"

    shared = Section(
        "Sites shared outside the organisation",
        ["Site", "Anyone links", "Guest links", "Organisation links", "External sharing on", "Last activity"],
        intro="Sharing links on each site, from Microsoft's count. Anyone links open without signing "
        "in. The Shared files report lists the files behind them." + _as_of(status),
        empty="No site has anyone or guest links.",
    )
    for r in sorted(rows, key=lambda r: (-r["anyoneLinks"], -r["guestLinks"], site_label(r, names).lower())):
        if r["anyoneLinks"] or r["guestLinks"]:
            shared.add(
                [
                    site_cell(r, names),
                    r["anyoneLinks"],
                    r["guestLinks"],
                    r["companyLinks"],
                    r["externalSharing"],
                    _date(r.get("lastActivityDate")) or "None recorded",
                ],
                "high" if r["anyoneLinks"] else "medium",
            )

    idle = Section(
        "Inactive sites",
        ["Site", "Type", "Owner", "Last activity", "Storage", "Files"],
        drifts=("Storage", "Files"),
        intro=f"Sites nobody has used in {INACTIVE_DAYS} days. Archive or delete them, or confirm "
        "someone still owns the content.",
        empty=f"Every site has been used in the last {INACTIVE_DAYS} days.",
    )
    for r in sorted(stale, key=lambda r: (r.get("lastActivityDate") or "", site_label(r, names).lower())):
        idle.add(
            [
                site_cell(r, names),
                site_type(r),
                owner_label(r),
                _date(r.get("lastActivityDate")) or "None recorded",
                size(r["storageUsed"]),
                r["files"],
            ],
            "low",
        )

    every = Section(
        "All sites",
        [
            "Site",
            "Type",
            "Owner",
            "Storage",
            f"{GROWTH_DAYS}-day change",
            "Files",
            "Last activity",
            "External sharing on",
            "Address",
        ],
        drifts=("Storage", "Files"),
        optional=("Address",),
        intro="Largest first."
        + ("" if before is not None else f" Growth appears once Office Sentry has {GROWTH_DAYS} days of history."),
        empty="No sites.",
    )
    every.chart = Chart("Site", ("Storage",), title="Largest sites")
    for r in sorted(rows, key=lambda r: -r["storageUsed"]):
        prev = (before or {}).get(r["_id"]) if not r.get("concealed") else None
        every.add(
            [
                site_cell(r, names),
                site_type(r),
                owner_label(r),
                size(r["storageUsed"]),
                _signed_size(r["storageUsed"] - prev["storageUsed"]) if prev else "",
                r["files"],
                _date(r.get("lastActivityDate")) or "None recorded",
                r["externalSharing"],
                r.get("url") or "",
            ]
        )
    report.sections = [shared, idle, every]


# -- OneDrive --------------------------------------------------------------------


def _counts(row: dict, *keys: str) -> dict:
    """The row with missing or null counts read as 0 (Microsoft's usage reports sometimes leave them out)."""
    return {**row, **{k: row.get(k) or 0 for k in keys}}


@report(
    "onedrive",
    "OneDrive",
    "Each person's OneDrive: storage, activity, sharing, and OneDrives of people who've left.",
    ("sharepoint_usage", "users", "shared_files"),
    group="files",
    order=30,
    keywords="personal storage leavers",
)
def onedrive(snap: TenantSnapshot, now: datetime, report: DataReport) -> None:
    status = _usage(snap, report)
    if status is None:
        return
    rows = [
        _counts(r, "storageUsed", "storageAllocated", "files", "activeFiles")
        for r in snap.objects("sharepoint_usage", "onedrive") or []
        if not r.get("deleted")
    ]
    users = {(u.get("userPrincipalName") or "").lower(): u for u in snap.objects("users", "user") or []}
    hidden = any(r.get("concealed") for r in rows)

    def account(r):
        if r.get("concealed"):
            return "Unknown"
        u = users.get(r.get("ownerPrincipalName") or "")
        if u is None:
            return "Deleted" if users else "Unknown"
        return "Enabled" if u.get("accountEnabled") else "Disabled"

    shared = _shared_items(snap)
    personal = [s for s in shared or [] if s.get("personal")]
    left = [r for r in rows if account(r) in ("Disabled", "Deleted")]
    points = storage_points(snap, "onedrive")
    growth = _growth(points, now)
    report.tiles = [
        Tile("OneDrives", len(rows), rows=Rows("All OneDrives"), trend=metric_trend(snap, "onedrive.accounts", now)),
        Tile(
            "Storage used",
            size(sum(r["storageUsed"] for r in rows)),
            f"{_signed_size(growth)} in {GROWTH_DAYS} days" if growth is not None else "",
            rows=Rows("All OneDrives"),
            trend=metric_trend(snap, "onedrive.storage_bytes", now, size),
        ),
        Tile(
            "Belong to people who've left",
            len(left) if not hidden else "?",
            "Disabled or deleted accounts",
            "warn" if left else None,
            Rows("OneDrives of people who've left"),
        ),
        Tile(
            "Files shared outside",
            len({s["_id"] for s in personal if s.get("exposure") in ("anyone", "external")})
            if shared is not None
            else "?",
            "Anyone links or people outside" if shared is not None else "Needs the Shared files scan",
            rows=Rows("Sharing by person"),
        ),
    ]
    report.chart = storage_chart(points, "OneDrive storage over time", now)
    report.chart_title = "OneDrive storage over time"

    sections = []
    gone = Section(
        "OneDrives of people who've left",
        ["Person", "Account", "Storage", "Files", "Last activity"],
        drifts=("Storage", "Files"),
        intro="Files here may include client data nobody is looking after. Move what's still needed to a "
        "shared site, then remove the account.",
        empty="Every OneDrive belongs to an enabled account."
        if not hidden
        else "Can't match OneDrives to accounts while Microsoft hides names (see above).",
    )
    for r in sorted(left, key=lambda r: -r["storageUsed"]):
        gone.add(
            [
                owner_label(r),
                account(r),
                size(r["storageUsed"]),
                r["files"],
                _date(r.get("lastActivityDate")) or "None recorded",
            ],
            "low",
        )
    sections.append(gone)

    if shared is not None:
        per: dict[str, dict] = {}
        for s in personal:
            if not s.get("exposure"):  # shared only with named colleagues, kept for the File permissions report
                continue
            p = per.setdefault(
                (s.get("owner") or s.get("site") or "?").lower(), {"anyone": 0, "external": set(), "org": 0, "items": 0}
            )
            p["anyone"] += s.get("exposure") == "anyone"
            p["external"].update(s.get("externalPeople") or [])
            p["org"] += (s.get("orgLinks") or 0) > 0
            p["items"] += 1
        out = Section(
            "Sharing by person",
            ["Person", "Anyone links", "People outside", "Organisation-wide", "Shared items"],
            intro="Files and folders each person shares from their OneDrive beyond specific colleagues.",
            empty="Nobody shares OneDrive files with anyone, the whole organisation or people outside.",
        )
        for who, p in sorted(per.items(), key=lambda kv: (-kv[1]["anyone"], -len(kv[1]["external"]), kv[0])):
            out.add(
                [who, p["anyone"], sorted(p["external"]) or "None", p["org"], p["items"]],
                "high" if p["anyone"] else ("medium" if p["external"] else None),
            )
        sections.append(out)

    every = Section(
        "All OneDrives",
        ["Person", "Account", "Storage", "Of quota", "Files", "Active files (30 days)", "Last activity"],
        drifts=("Storage", "Of quota", "Files", "Active files (30 days)"),
        intro="Largest first." + _as_of(status),
        empty="No OneDrives.",
    )
    for r in sorted(rows, key=lambda r: -r["storageUsed"]):
        pct = 100 * r["storageUsed"] / r["storageAllocated"] if r["storageAllocated"] else None
        quota = "" if pct is None else ("Under 1%" if 0 < pct < 1 else f"{pct:.0f}%")
        every.add(
            [
                owner_label(r),
                account(r),
                size(r["storageUsed"]),
                quota,
                r["files"],
                r["activeFiles"],
                _date(r.get("lastActivityDate")) or "None recorded",
            ]
        )
    sections.append(every)
    report.sections = sections


# -- Shared files ----------------------------------------------------------------------


def _shared_items(snap: TenantSnapshot) -> list[dict] | None:
    status = (snap.objects("shared_files", "scan_status") or [None])[0]
    if not status or not status.get("available"):
        return None
    return snap.objects("shared_files", "shared_item") or []


def _location(item: dict) -> str:
    if item.get("personal"):
        return f"OneDrive of {item.get('owner')}" if item.get("owner") else "OneDrive"
    return f"{item.get('site')} / {item.get('library')}"


def _item_cell(item: dict):
    text = (item.get("path") or item.get("name") or "?") + (" (folder)" if item.get("folder") else "")
    return Link(text, item["webUrl"]) if item.get("webUrl") else text


def _link_words(link: dict) -> str:
    return {
        "view": "View",
        "edit": "Edit",
        "review": "Review",
        "embed": "Embed",
        "blocksDownload": "View, no download",
    }.get(link.get("type"), link.get("type") or "")


@report(
    "shared-files",
    "Shared files",
    "Every file and folder shared with anyone, people outside, or the whole organisation.",
    ("shared_files", "sharepoint_usage"),
    group="files",
    order=40,
    keywords="anyone links external sharing links",
)
def shared_files(snap: TenantSnapshot, now: datetime, report: DataReport) -> None:
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
    items = [
        {
            **i,
            "links": [x for x in i.get("links") or [] if isinstance(x, dict)],
            "externalPeople": i.get("externalPeople") or [],
        }
        for i in snap.objects("shared_files", "shared_item") or []
    ]
    anyone = [i for i in items if i.get("exposure") == "anyone"]
    outside = [i for i in items if i.get("externalPeople")]
    org = [i for i in items if i.get("orgLinks")]
    forever = [i for i in anyone if i.get("neverExpires")]
    report.tiles = [
        Tile(
            "Shared with anyone",
            len(anyone),
            "Open without signing in",
            "bad" if anyone else "ok",
            trend=metric_trend(snap, "sharing.anyone_items", now),
        ),
        Tile(
            "Shared with people outside",
            len(outside),
            plural(len({p for i in outside for p in i["externalPeople"]}), "outside address"),
            rows=Rows("Shared with people outside the organisation"),
            trend=metric_trend(snap, "sharing.external_items", now),
        ),
        Tile(
            "Shared organisation-wide",
            len(org),
            "Anyone in the organisation, and Copilot",
            rows=Rows("Shared with the whole organisation"),
            trend=metric_trend(snap, "sharing.org_items", now),
        ),
        Tile(
            "Anyone links with no expiry",
            len(forever),
            "These should expire",
            "warn" if forever else None,
            Rows("Shared with anyone", {"Expires": "Never"}),
        ),
    ]
    links = sum(i.get("anyoneLinks") or 0 for i in snap.objects("sharepoint_usage", "site") or [])
    if links and not anyone:
        report.notice = (
            f"Microsoft counts {plural(links, 'anyone link')} in this tenant, but the scan found none. "
            "Some libraries may not have been readable; check the last Shared files run on the client "
            "page."
        )
    report.footnote = (
        f"Scanned {status.get('items') or 0:,} files and folders in {status.get('drives') or 0} "
        "libraries and OneDrives. A shared folder is listed once; the files inside it are shared "
        "the same way."
    )

    a = Section(
        "Shared with anyone",
        ["File or folder", "Location", "Access", "Expires", "Password", "Last modified"],
        intro="Anyone who gets the link can open these without signing in, and links get forwarded. "
        "Remove the link, or set an expiry date and view-only.",
        empty="Nothing is shared with an anyone link.",
    )
    for i in sorted(anyone, key=lambda i: (not anyone_can_edit(i), _location(i), i.get("path") or "")):
        lk = [x for x in i["links"] if x.get("scope") == "anonymous"]
        expires = [x["expires"] for x in lk if x.get("expires")]
        a.add(
            [
                _item_cell(i),
                _location(i),
                sorted({_link_words(x) for x in lk}),
                _date(min(expires)) if expires and len(expires) == len(lk) else "Never",
                bool(lk) and all(x.get("password") for x in lk),
                _date(i.get("lastModified")),
            ],
            # As the anyone-links check rates it: an expiry date doesn't stop forwarding.
            "high" if anyone_can_edit(i) else "medium",
        )

    b = Section(
        "Shared with people outside the organisation",
        ["File or folder", "Location", "Shared with", "Can edit", "Last modified"],
        intro="Named people outside the organisation. Check each one still needs access.",
        empty="Nothing is shared with people outside the organisation.",
    )
    for i in sorted(outside, key=lambda i: (_location(i), i.get("path") or "")):
        edit = outside_can_edit(i)
        b.add(
            [_item_cell(i), _location(i), i["externalPeople"], edit, _date(i.get("lastModified"))],
            "high" if anyone_can_edit(i) else ("medium" if edit else "low"),
        )

    c = Section(
        "Shared with the whole organisation",
        ["File or folder", "Location", "Access", "Last modified"],
        intro="Everyone in the organisation can open these with the link, and Copilot can surface them to "
        "anyone who searches.",
        empty="Nothing is shared with the whole organisation.",
    )
    for i in sorted(org, key=lambda i: (_location(i), i.get("path") or "")):
        c.add(
            [
                _item_cell(i),
                _location(i),
                sorted({_link_words(x) for x in i["links"] if x.get("scope") == "organization"}),
                _date(i.get("lastModified")),
            ]
        )
    report.sections = [a, b, c]
