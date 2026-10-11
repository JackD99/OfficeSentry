"""Site access: who owns, administers and can reach each SharePoint site.

Reads the snapshots written by ``collectors/site_access.py`` (with the usage report for activity and
sharing counts) and leads with what needs attention: sites nobody owns, sites with a single owner,
guests, and sites open to everyone in the organisation.
"""

from __future__ import annotations

from datetime import datetime

from ..snapshot import TenantSnapshot
from ..text import plural
from .data_reports import Chart, DataReport, Link, Rows, Section, Tile, _date, metric_trend
from .registry import report
from .sharepoint import site_type

LISTED = 20  # sites named in a person's row before "and N more"

EVERYONE_WORDS = {"all": "Yes, including guests", "members": "Yes"}
WAITING_NOTE = (
    "Members of SharePoint groups on {sites} with no Microsoft 365 group can't be listed yet. That needs "
    "the SharePoint read permission, which arrives with the next consent batch; those sites show their "
    "site administrators and what the default library grants."
)


def _site_cell(row: dict):
    name = row.get("name") or row.get("url") or "?"
    url = row.get("url")
    return Link(name, url) if isinstance(url, str) and url else name


def _names(people: list, key: str = "name") -> list[str]:
    return [p.get(key) or p.get("upn") or "?" for p in people if isinstance(p, dict)]


def _problem(row: dict) -> str | None:
    count = row.get("ownerCount")
    if count is None:
        return None
    if count == 0:
        return "No owner"
    if count == 1:
        return "One owner"
    return None


def _owners(row: dict) -> list[str]:
    out = []
    for o in row.get("owners") or []:
        if isinstance(o, dict):
            out.append((o.get("name") or o.get("upn") or "?") + ("" if o.get("enabled", True) else " (disabled)"))
    return out


def _members(row: dict) -> str | int:
    if row.get("memberCount") is not None:
        return row["memberCount"]
    if row.get("group"):
        return "Unknown"
    return f"{plural(row.get('people') or 0, 'person')} recorded" if row.get("userList") else "Unknown"


def _access_rows(row: dict) -> list[tuple[list, str | None]]:
    """What the site grants and to whom, from the default library, the group and the site's user list."""
    rows: list[tuple[list, str | None]] = []
    seen: set[tuple[str, str]] = set()
    site = _site_cell(row)
    group = row.get("group") if isinstance(row.get("group"), dict) else None
    if group and row.get("ownerSource") == "group":
        owners = _owners(row)
        rows.append(
            (
                [
                    site,
                    f"Owners of {group.get('name') or 'the Microsoft 365 group'}: {', '.join(owners) or 'nobody'}",
                    "Microsoft 365 group",
                    "Full control",
                    False,
                ],
                None,
            )
        )
        members = row.get("memberCount")
        guests = row.get("guestCount") or 0
        if members is not None:
            text = f"Members of {group.get('name') or 'the Microsoft 365 group'}: {plural(members, 'person')}"
            if guests:
                text += f", {plural(guests, 'guest')}"
            rows.append(([site, text, "Microsoft 365 group", "Edit", guests > 0], "low" if guests else None))
    for a in row.get("access") or []:
        if not isinstance(a, dict):
            continue
        key = (str(a.get("who")), str(a.get("how")))
        if key in seen:
            continue
        seen.add(key)
        outside = bool(a.get("outside"))
        flag = "medium" if a.get("how") == "Sharing link" and outside else ("low" if outside else None)
        rows.append(([site, a.get("who") or "?", a.get("how") or "?", a.get("can") or "Unknown", outside], flag))
    if row.get("everyone") and not any(r[0][2] == "Everyone" for r in rows):
        who = "Everyone, including guests" if row["everyone"] == "all" else "Everyone in the organisation"
        rows.append(([site, who, "Everyone", "Given access on the site", row["everyone"] == "all"], None))
    listed = {r[0][1] for r in rows}
    for g in row.get("guests") or []:
        if isinstance(g, str) and g not in listed:
            rows.append(([site, g, "Guest", "Given access on the site", True], "low"))
    return rows


@report(
    "site-access",
    "Site access",
    "Who owns, administers and can reach each SharePoint site: ownerless sites, guests, and sites open to "
    "everyone in the organisation.",
    ("site_access", "sharepoint_usage", "groups", "users"),
    group="files",
    order=15,
    keywords="owners admins administrators guests permissions members everyone ownerless who can access",
    uses=("sharing",),
)
def site_access(snap: TenantSnapshot, now: datetime, report: DataReport) -> None:
    status = (snap.objects("site_access", "access_status") or [None])[0]
    if status is None:
        report.unavailable = 'Site access hasn\'t been collected yet. Refresh "Site access" on the tenant page.'
        return
    if not status.get("available"):
        report.unavailable = (
            "This tenant has no SharePoint licence."
            if status.get("reason") == "no_sharepoint"
            else "Office Sentry can't read this tenant's sites yet. It needs the Sites.Read.All permission: "
            "open the tenant page, send the consent link to the client's Global Admin again and ask them "
            "to accept."
        )
        return
    rows = [r for r in snap.objects("site_access", "site") or [] if isinstance(r, dict)]
    usage = {(s.get("url") or "").lower(): s for s in snap.objects("sharepoint_usage", "site") or [] if s.get("url")}

    def activity(r):
        return _date((usage.get((r.get("url") or "").lower()) or {}).get("lastActivityDate")) or "None recorded"

    def kind(r):
        return site_type({"template": r.get("template")}) if r.get("template") else "Unknown"

    ownerless = [r for r in rows if _problem(r) == "No owner"]
    single = [r for r in rows if _problem(r) == "One owner"]
    guests = {g for r in rows for g in (r.get("guests") or []) if isinstance(g, str)}
    open_sites = [r for r in rows if r.get("everyone")]
    unknown_owner = [r for r in rows if r.get("ownerCount") is None]
    report.tiles = [
        Tile(
            "Sites with no owner",
            len(ownerless),
            "Nobody enabled owns them",
            "bad" if ownerless else "ok",
            Rows("Sites needing an owner", {"Problem": "No owner"}),
            metric_trend(snap, "sites.ownerless", now),
        ),
        Tile(
            "One owner only",
            len(single),
            "A leaver would leave them orphaned",
            "warn" if single else None,
            Rows("Sites needing an owner", {"Problem": "One owner"}),
            metric_trend(snap, "sites.single_owner", now),
        ),
        Tile(
            "Guests with access",
            len(guests),
            f"On {plural(sum(1 for r in rows if r.get('guestCount')), 'site')}",
            "warn" if guests else None,
            Rows("Guests and the sites they can reach"),
            metric_trend(snap, "sites.guests", now),
        ),
        Tile(
            "Open to everyone in the organisation",
            len(open_sites),
            "Everyone or Everyone except external users",
            "warn" if any(r.get("everyone") == "all" for r in open_sites) else None,
            Rows("Who can access each site", {"Everyone in the organisation": list(EVERYONE_WORDS.values())}),
            metric_trend(snap, "sites.open_to_everyone", now),
        ),
        Tile(
            "Sites",
            len(rows),
            f"Owners unknown for {plural(len(unknown_owner), 'site')}" if unknown_owner else "Not counting OneDrives",
            rows=Rows("Who can access each site"),
        ),
    ]
    waiting = [r for r in rows if not r.get("group") and r.get("userList")]
    if waiting:
        report.notes.append(WAITING_NOTE.format(sites=plural(len(waiting), "site")))
    if status.get("unreadable"):
        report.notes.append(
            f"The user list of {plural(status['unreadable'], 'site')} couldn't be read; those sites show only "
            "what their default library grants."
        )

    needing = Section(
        "Sites needing an owner",
        ["Site", "Type", "Problem", "Owners", "Admins", "Last activity"],
        intro="A site with no enabled owner has nobody to approve access or answer for its content; one owner "
        "is one leaver away from that. Owners are the Microsoft 365 group's owners, or the site "
        "administrators of a site with no group.",
        empty="Every site has at least two enabled owners.",
    )
    for r in sorted(ownerless + single, key=lambda r: (_problem(r) != "No owner", (r.get("name") or "").lower())):
        needing.add(
            [
                _site_cell(r),
                kind(r),
                _problem(r),
                _owners(r) or "None",
                _names(r.get("admins") or []) or "None",
                activity(r),
            ],
            "high" if _problem(r) == "No owner" else "low",
        )

    every = Section(
        "Who can access each site",
        [
            "Site",
            "Type",
            "Owners",
            "Admins",
            "Members",
            "Guests",
            "Everyone in the organisation",
            "External sharing on",
            "Last activity",
        ],
        intro="Each site's owners, site administrators, how many people are in its group, which guests have "
        "access and whether everyone in the organisation can open it.",
        empty="No sites.",
        filter_columns=("Type", "Everyone in the organisation"),
    )
    every.chart = Chart("Type", ("Guests",), title="Guests by site type", tones=("bad",))
    for r in sorted(rows, key=lambda r: (-(r.get("guestCount") or 0), (r.get("name") or "").lower())):
        everyone = EVERYONE_WORDS.get(r.get("everyone") or "", "No")
        every.add(
            [
                _site_cell(r),
                kind(r),
                _owners(r) or ("Unknown" if r.get("ownerCount") is None else "None"),
                _names(r.get("admins") or []) or "None",
                _members(r),
                r.get("guestCount") or 0,
                everyone,
                "Unknown" if r.get("externalSharing") is None else bool(r.get("externalSharing")),
                activity(r),
            ],
            "medium" if r.get("everyone") == "all" else ("low" if r.get("guestCount") else None),
        )

    access = Section(
        "Access by site",
        ["Site", "Who", "How", "Can", "Outside the organisation"],
        intro="What each site grants and to whom: the Microsoft 365 group's owners and members, what the "
        "default document library gives to site groups, groups and people, and guests given access on "
        "the site.",
        empty="Nothing to show.",
        group="Site",
        filter_columns=("How",),
    )
    for r in sorted(rows, key=lambda r: (r.get("name") or "").lower()):
        for cells, flag in _access_rows(r):
            access.add(cells, flag)

    by_guest: dict[str, dict] = {}
    for r in rows:
        group_guests = {g.lower() for g in (r.get("group") and r.get("guests")) or [] if isinstance(g, str)}
        for g in r.get("guests") or []:
            if not isinstance(g, str):
                continue
            entry = by_guest.setdefault(g, {"sites": [], "how": set()})
            entry["sites"].append(r.get("name") or r.get("url") or "?")
            entry["how"].add(
                "Member of the site's group" if r.get("group") and g in group_guests else "Given access on the site"
            )
    guest_table = Section(
        "Guests and the sites they can reach",
        ["Guest", "Sites", "Which sites", "How"],
        intro="Every outside account with access to a site, through the site's group or by being given access "
        "to the site, a library or a folder. Check each one still needs it.",
        empty="No guest has access to a site.",
    )
    for g, entry in sorted(by_guest.items(), key=lambda kv: (-len(kv[1]["sites"]), kv[0])):
        sites = sorted(entry["sites"])
        guest_table.add(
            [
                g,
                len(sites),
                sites[:LISTED] + ([f"and {len(sites) - LISTED} more"] if len(sites) > LISTED else []),
                sorted(entry["how"]),
            ],
            "low",
        )

    by_admin: dict[str, dict] = {}
    for r in rows:
        for a in r.get("admins") or []:
            if not isinstance(a, dict):
                continue
            who = a.get("upn") or a.get("email") or a.get("name") or "?"
            entry = by_admin.setdefault(who, {"name": a.get("name") or who, "sites": []})
            entry["sites"].append(r.get("name") or r.get("url") or "?")
    admins = Section(
        "Site administrators",
        ["Person", "Sites", "Which sites"],
        intro="Site collection administrators have full control of everything on the site, whatever its groups "
        "say. People who administer many sites are usually IT staff; check anyone else.",
        empty="No site administrators recorded.",
    )
    for who, entry in sorted(by_admin.items(), key=lambda kv: (-len(kv[1]["sites"]), kv[0])):
        sites = sorted(entry["sites"])
        admins.add(
            [who, len(sites), sites[:LISTED] + ([f"and {len(sites) - LISTED} more"] if len(sites) > LISTED else [])]
        )

    report.sections = [needing, every, access, guest_table, admins]
    report.footnote = (
        f"Read {plural(status.get('sites') or 0, 'site')}' user lists and default libraries. "
        "Sites appear here once the nightly collection has read them; OneDrives are in the OneDrive report."
    )
