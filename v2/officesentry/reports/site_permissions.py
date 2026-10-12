"""Site permissions: who holds which permission level on each SharePoint site, and through what.

Reads the snapshots written by ``collectors/site_permissions.py`` (SharePoint's own API) and expands every
SharePoint group into its members, and Microsoft 365 and security groups into theirs where the Groups
collection has them, so "who can get into the Finance site, and how" has one answer per person. Leads with
what needs attention: guests with full control, people given access one by one instead of through a group,
sites open to everyone, and lists and subsites that don't follow their site's permissions.
"""

from __future__ import annotations

from datetime import datetime

from ..collectors.sharepoint_lookup import DENIED_TEXT
from ..snapshot import TenantSnapshot
from ..text import plural
from .data_reports import DataReport, Link, Rows, Section, Tile, metric_trend
from .registry import report

LISTED = 20  # sites named in a person's row before "and N more"
FULL = "Full Control"
# Most to least: what "highest level" means for a person who holds several.
RANK = ["Full Control", "Design", "Edit", "Contribute", "Read", "View Only"]
ROLE_TITLES = {"owners": "Owners", "members": "Members", "visitors": "Visitors"}
KIND_TITLES = {
    "person": "Person",
    "guest": "Guest",
    "group_members": "Microsoft 365 group",
    "group_owners": "Microsoft 365 group owners",
    "security_group": "Security group",
    "everyone": "Everyone",
    "everyone_but_guests": "Everyone",
    "sp_group": "SharePoint group",
}
WAITING = (
    "Waiting for the client to approve the SharePoint permission. Office Sentry reads SharePoint groups, "
    "permission levels and lists with their own permissions through SharePoint's own API, which needs "
    "Sites.Read.All on Office 365 SharePoint Online. It comes with the next permission update: send the consent "
    "link from the client page to their Global Admin. Until then, Site access shows owners, admins and guests."
)
UNREADABLE = (
    "No site's permissions could be read on the last collection: SharePoint answered every site with an error. "
    "The run log on the client page has Microsoft's messages; the next collection tries again."
)
DIRECT = "Given directly"


def _site_cell(row: dict):
    name = row.get("name") or row.get("url") or "?"
    url = row.get("url")
    return Link(name, url) if isinstance(url, str) and url else name


def _rank(level: str) -> int:
    return RANK.index(level) if level in RANK else len(RANK)


def highest(levels) -> str:
    named = [lv for lv in levels or [] if isinstance(lv, str)]
    return min(named, key=_rank) if named else "Unknown"


class _Directory:
    """People and groups from the Users and Groups collections, to expand groups into people."""

    def __init__(self, snap: TenantSnapshot):
        self.users = {u["_id"]: u for u in snap.objects("users", "user") or [] if isinstance(u, dict) and u.get("_id")}
        self.groups = {
            g["_id"]: g for g in snap.objects("groups", "group") or [] if isinstance(g, dict) and g.get("_id")
        }

    def name(self, kind: str, gid) -> str | None:
        g = self.groups.get(str(gid or "").lower()) or self.groups.get(str(gid or ""))
        return g.get("displayName") if g else None

    def people(self, kind: str, gid) -> tuple[list[tuple[str, bool]], bool]:
        """``([(address, is guest)], complete)`` for a Microsoft 365 group's members or owners, or a security
        group's members. Empty and incomplete when the group isn't known."""
        g = self.groups.get(str(gid or "").lower()) or self.groups.get(str(gid or ""))
        if not g or not g.get("examined"):
            return [], False
        if kind == "group_owners":
            out = []
            for o in g.get("owners") or []:
                if isinstance(o, dict) and o.get("upn"):
                    out.append((o["upn"].lower(), "#ext#" in o["upn"].lower()))
            return out, True
        out = []
        for uid in g.get("memberIds") or []:
            u = self.users.get(uid)
            if not u:
                continue
            address = (u.get("userPrincipalName") or u.get("mail") or "").lower()
            if address:
                out.append((address, u.get("userType") == "Guest" or "#ext#" in address))
        return out, bool(g.get("memberIdsComplete"))


def _who(p: dict, directory: _Directory) -> str:
    kind = p.get("kind")
    if kind in ("group_members", "group_owners", "security_group"):
        name = directory.name(kind, p.get("id")) or p.get("name") or "a group"
        return (
            f"Owners of {name}"
            if kind == "group_owners"
            else (name if kind == "security_group" else f"Members of {name}")
        )
    if kind in ("person", "guest") and isinstance(p.get("id"), str) and "@" in p["id"]:
        return p["id"]  # the address, which links to the person's page
    return p.get("name") or p.get("id") or "?"


def _expanded(row: dict, directory: _Directory):
    """Every way into one site: ``(who, type, how, levels, outside, principal, group)`` per person or
    principal, with SharePoint groups replaced by their members."""
    groups = {g.get("id"): g for g in row.get("groups") or [] if isinstance(g, dict)}
    for grant in row.get("grants") or []:
        if not isinstance(grant, dict) or not isinstance(grant.get("principal"), dict):
            continue
        p, levels = grant["principal"], [lv for lv in grant.get("levels") or [] if isinstance(lv, str)]
        if p.get("kind") == "sp_group":
            g = groups.get(p.get("id")) or {"name": p.get("name"), "members": []}
            members = [m for m in g.get("members") or [] if isinstance(m, dict)]
            how = f"Through {g.get('name') or p.get('name') or 'a SharePoint group'}"
            for m in members:  # an empty group lets nobody in: it shows under SharePoint groups only
                yield (
                    _who(m, directory),
                    KIND_TITLES.get(m.get("kind"), "Other"),
                    how,
                    levels,
                    bool(m.get("outside")),
                    m,
                    g,
                )
        else:
            yield (
                _who(p, directory),
                KIND_TITLES.get(p.get("kind"), "Other"),
                DIRECT,
                levels,
                bool(p.get("outside")),
                p,
                None,
            )


def _people_of(p: dict, directory: _Directory) -> list[tuple[str, bool]]:
    kind = p.get("kind")
    if kind in ("person", "guest"):
        return [(str(p.get("id") or p.get("name") or "?").lower(), kind == "guest")]
    if kind in ("group_members", "group_owners", "security_group"):
        return directory.people(kind, p.get("id"))[0]
    return []


def _grant_text(grants) -> list[str]:
    out = []
    for g in grants or []:
        if isinstance(g, dict) and isinstance(g.get("principal"), dict):
            who = g["principal"].get("name") or "?"
            out.append(f"{who} ({', '.join(g.get('levels') or []) or 'Unknown'})")
    return out


@report(
    "site-permissions",
    "Site permissions",
    "Who holds which permission level on each SharePoint site, through which SharePoint group, and the lists, "
    "libraries and subsites with permissions of their own.",
    ("site_permissions", "groups", "users"),
    group="files",
    order=17,
    keywords="sharepoint groups permission levels full control edit read inheritance broken unique lists "
    "libraries subsites who can access members visitors owners",
    uses=("sharing",),
)
def site_permissions(snap: TenantSnapshot, now: datetime, report: DataReport) -> None:
    status = (snap.objects("site_permissions", "permissions_status") or [None])[0]
    if status is None:
        report.unavailable = (
            'Site permissions haven\'t been collected yet. Refresh "Site permissions" on the client page.'
        )
        return
    # Older collections stored a run that read no site at all as available, with every site unreadable.
    nothing_read = status.get("available") and not status.get("sites") and status.get("unreadable")
    if not status.get("available") or nothing_read:
        reason = "unreadable" if nothing_read else status.get("reason")
        report.unavailable = {
            "no_sharepoint": "This tenant has no SharePoint licence.",
            "consent": WAITING,
            "no_certificate": "Office Sentry reads SharePoint permissions with the app's certificate, and this "
            "connection has none. Add the certificate under App connection.",
            "denied": DENIED_TEXT,
            "unreadable": UNREADABLE,
        }.get(
            reason,
            "Office Sentry can't read this tenant's sites yet. It needs the Sites.Read.All permission: open the "
            "client page, send the consent link to the client's Global Admin again and ask them to accept.",
        )
        return
    rows = sorted(
        (r for r in snap.objects("site_permissions", "site") or [] if isinstance(r, dict)),
        key=lambda r: (r.get("name") or "").lower(),
    )
    directory = _Directory(snap)

    access = Section(
        "Who has access to each site",
        ["Site", "Who", "Type", "How", "Permission level", "Outside the organisation"],
        intro="Every permission level on each site, with SharePoint groups opened up into their members. "
        "Permissions on single files and folders are in File and folder permissions.",
        empty="No site grants anything to anyone.",
        group="Site",
        filter_columns=("Type", "Permission level", "Outside the organisation"),
    )
    people: dict[str, dict] = {}
    direct_people: set[str] = set()
    guests_full: set[str] = set()
    everyone_sites: set[str] = set()
    for r in rows:
        site = r.get("name") or r.get("url") or "?"
        for who, kind, how, levels, outside, p, _g in _expanded(r, directory):
            level = highest(levels)
            flag = None
            if p.get("kind") == "guest" and level == FULL:
                flag = "high"
                guests_full.add(str(p.get("id")))
            elif p.get("kind") == "everyone":
                flag = "medium"
            elif outside or p.get("kind") == "everyone_but_guests":
                flag = "low"
            if p.get("kind") in ("everyone", "everyone_but_guests"):
                everyone_sites.add(site)
            if how == DIRECT and p.get("kind") in ("person", "guest"):
                direct_people.add(f"{site}|{p.get('id')}")
            access.add([_site_cell(r), who, kind, how, levels or ["Unknown"], outside], flag)
            for address, guest in _people_of(p, directory):
                entry = people.setdefault(address, {"guest": guest, "sites": {}})
                entry["guest"] = entry["guest"] or guest
                held = entry["sites"].setdefault(site, set())
                held.update(levels)

    groups = Section(
        "SharePoint groups",
        ["Site", "Group", "Role on the site", "Permission level", "Members", "Guests", "Group owner"],
        intro="Each site's SharePoint groups: its owners, members and visitors groups and any others, what each "
        "grants on the site and who is in it. A guest in an owners group has full control of the site.",
        empty="No SharePoint groups.",
        filter_columns=("Role on the site",),
    )
    for r in rows:
        for g in r.get("groups") or []:
            if not isinstance(g, dict):
                continue
            members = [m for m in g.get("members") or [] if isinstance(m, dict)]
            guests = sorted(str(m.get("id") or m.get("name")) for m in members if m.get("kind") == "guest")
            levels = [lv for lv in g.get("levels") or [] if isinstance(lv, str)]
            role = ROLE_TITLES.get(g.get("role") or "", "Other")
            flag = "high" if guests and (FULL in levels or g.get("role") == "owners") else ("low" if guests else None)
            groups.add(
                [
                    _site_cell(r),
                    g.get("name") or "?",
                    role,
                    levels or ("Nothing on the site itself" if not g.get("used") else "Lists or subsites only"),
                    g.get("memberCount", len(members)),
                    guests or "None",
                    g.get("owner") or "Unknown",
                ],
                flag,
            )

    lists = Section(
        "Lists and libraries with their own permissions",
        ["Site", "List or library", "Type", "Items", "Who can open it"],
        intro="Lists and libraries that stopped inheriting their site's permissions, so the site's groups don't "
        "say who can open them. Each is worth a look: the reason is often a one-off that was never undone.",
        empty="Every list and library follows its site's permissions.",
        drifts=("Items",),
    )
    for r in rows:
        for x in r.get("lists") or []:
            if not isinstance(x, dict):
                continue
            title = x.get("title") or "?"
            cell = Link(title, x["url"]) if isinstance(x.get("url"), str) and x["url"] else title
            who = _grant_text(x.get("grants")) if x.get("grants") is not None else "Not read this time"
            outside = any(
                isinstance(g, dict) and (g.get("principal") or {}).get("outside") for g in x.get("grants") or []
            )
            lists.add(
                [_site_cell(r), cell, "Library" if x.get("library") else "List", x.get("items"), who or "Nobody"],
                "low" if outside else None,
            )

    subsites = Section(
        "Subsites with their own permissions",
        ["Site", "Subsite", "Who can open it"],
        intro="Subsites that don't inherit their parent site's permissions.",
        empty="Every subsite follows its parent site's permissions.",
    )
    for r in rows:
        for w in r.get("subsites") or []:
            if not isinstance(w, dict) or not w.get("unique"):
                continue
            title = w.get("title") or w.get("url") or "?"
            cell = Link(title, w["url"]) if isinstance(w.get("url"), str) else title
            who = _grant_text(w.get("grants")) if w.get("grants") is not None else "Not read this time"
            subsites.add([_site_cell(r), cell, who or "Nobody"])

    by_person = Section(
        "Access by person",
        ["Person", "Outside the organisation", "Sites", "Highest level", "Which sites"],
        intro="Everyone who can get into at least one site, through any group or directly, with guests and "
        "outside accounts first. Microsoft 365 and security groups are opened up where the Groups collection "
        "has their members.",
        empty="Nobody is recorded with access to a site.",
        filter_columns=("Outside the organisation", "Highest level"),
    )
    for address, entry in sorted(people.items(), key=lambda kv: (not kv[1]["guest"], -len(kv[1]["sites"]), kv[0])):
        sites = sorted(entry["sites"])
        level = highest({lv for levels in entry["sites"].values() for lv in levels})
        flag = "high" if entry["guest"] and level == FULL else ("low" if entry["guest"] else None)
        by_person.add(
            [
                address,
                entry["guest"],
                len(sites),
                level,
                sites[:LISTED] + ([f"and {len(sites) - LISTED} more"] if len(sites) > LISTED else []),
            ],
            flag,
        )

    unique_lists = sum(int(r.get("uniqueListCount") or 0) for r in rows)
    report.tiles = [
        Tile(
            "Guests with full control",
            len(guests_full),
            "Can change permissions and delete anything",
            "bad" if guests_full else "ok",
            Rows("Access by person", {"Outside the organisation": "Yes", "Highest level": FULL}),
        ),
        Tile(
            "People given access directly",
            len(direct_people),
            "Not through a group, so easy to forget",
            "warn" if direct_people else None,
            Rows("Who has access to each site", {"How": DIRECT, "Type": ["Person", "Guest"]}),
            metric_trend(snap, "site_permissions.direct_grants", now),
        ),
        Tile(
            "Open to everyone",
            len(everyone_sites),
            "Everyone or Everyone except external users",
            "warn" if everyone_sites else None,
            Rows("Who has access to each site", {"Type": "Everyone"}),
        ),
        Tile(
            "Lists and libraries with their own permissions",
            unique_lists,
            f"On {plural(sum(1 for r in rows if r.get('uniqueListCount')), 'site')}",
            None,
            Rows("Lists and libraries with their own permissions"),
            metric_trend(snap, "site_permissions.unique_lists", now),
        ),
        Tile("Sites", len(rows), "Not counting OneDrives", rows=Rows("Who has access to each site")),
    ]
    report.sections = [access, groups, lists, subsites, by_person]
    if status.get("unreadable"):
        report.notes.append(f"The permissions of {plural(status['unreadable'], 'site')} couldn't be read.")
    if not status.get("complete", True):
        report.notes.append("The last collection stopped before every site was read; the rest follow next night.")
    report.footnote = (
        f"Read from SharePoint for {plural(len(rows), 'site')}. Hidden levels such as Limited Access, which "
        "SharePoint adds when a single file is shared, are left out."
    )
