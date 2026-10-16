"""One page per SharePoint site: everything Office Sentry has collected about one site.

Storage and how it has grown, what fills it (libraries, older versions, the recycle bin, the largest
files), how it is shared (Microsoft's link counts, external sharing, the files shared from it), the team
or group behind it and its activity, open findings and what changed between collections. Built from
stored data the way the mailbox and group pages are (``object_page``): each area is a
``@part("site", order)`` function returning a ``Part``; ``build()`` puts the page together and
``directory()`` lists every site for the Sites page.

Microsoft's site usage report is the list of sites (it names every site, with storage, activity and
sharing link counts); SharePoint storage and Shared files add detail when they've been collected. A
site's page key is Microsoft's site ID, or a hash of its address when the report doesn't give one, so
the URL holds no slashes. Sites whose names Microsoft conceals have no address, and so no page.

Who can open the site itself (owners, members, visitors, broken inheritance) is in the SharePoint
permission reports; the page links to them under "In the reports" and keeps to the headline figures.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, date, datetime

from .. import store
from ..collectors.sharepoint import anyone_can_edit, org_can_edit, outside_can_edit
from ..devices import parse_day, parse_ts
from ..snapshot import TenantSnapshot
from ..text import fold, plural
from .data_reports import PAGE_INDEXES, REPORTS, Highlight, Link, PageIndex, PageLink, Section
from .object_page import (
    ObjectPage,
    Subject,
    _day,
    attention_tile,
    build_parts,
    findings_for,
    object_path,
    part,
    sort_attention,
    sources_for,
)
from .profile.base import HISTORY_LIMIT, Event, Part
from .sharepoint import CONCEALED_NOTICE, INACTIVE_DAYS, site_type, size, storage_chart

KIND = "site"
URL = "sites"
NEAR_LIMIT = 0.9
TOP_FILES = 10
SHARED_ROWS = 200
SOURCES = ("sharepoint_usage", "sharepoint_storage", "shared_files", "groups")
# Reports about who can open a site and its files, linked when this install has them.
PERMISSION_REPORTS = ("site-access", "site-permissions", "file-permissions")
STATES = {"exceeded": "high", "critical": "high", "nearing": "medium"}


def _text(value) -> str:
    return value if isinstance(value, str) else ""


def _int(value) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def url_key(url) -> str:
    """A site address as compared everywhere: lower case, no trailing slash."""
    return _text(url).lower().rstrip("/")


def site_key(row: dict) -> str:
    """The page key: Microsoft's site ID, else a hash of the address."""
    sid = _text(row.get("siteId"))
    if sid:
        return sid.lower()
    return "u" + hashlib.sha256(url_key(row.get("url")).encode()).hexdigest()[:16]


def site_path(tenant_id: int, key: str) -> str:
    return object_path(tenant_id, URL, key)


def sites(snap: TenantSnapshot) -> list[dict] | None:
    """Every live site with an address, from Microsoft's usage report, or None before it's collected."""
    rows = snap.objects("sharepoint_usage", "site")
    if rows is None:
        return None
    return [r for r in rows if _text(r.get("url")) and not r.get("deleted")]


def find_site(snap: TenantSnapshot, key: str) -> dict | None:
    return next((s for s in sites(snap) or [] if site_key(s) == key.lower()), None)


def matches_group(site: dict, mail: str) -> bool:
    """Whether a site is a Microsoft 365 group's team site: a group template, owned by the group's address or
    at /sites/<the address's first part>."""
    if not _text(site.get("template")).lower().startswith("group") or site.get("deleted"):
        return False
    mail = mail.lower()
    local = mail.split("@")[0]
    return bool(mail) and (
        _text(site.get("ownerPrincipalName")).lower() == mail or url_key(site.get("url")).endswith("/sites/" + local)
    )


def _index(snap: TenantSnapshot) -> dict[str, str]:
    """Site addresses -> page path. Report cells that link to a site's address open its page instead."""
    tid = snap.tenant["id"]
    return {url_key(s["url"]): site_path(tid, site_key(s)) for s in sites(snap) or []}


PAGE_INDEXES.append(PageIndex(_index))


def _name(site: dict, storage: dict | None) -> str:
    """The site's name: SharePoint storage has it; the usage report only has the address, whose last part
    ("/sites/finance-team") reads as a name."""
    name = _text((storage or {}).get("site"))
    if name:
        return name
    url = url_key(site.get("url"))
    if "/sites/" in url or "/teams/" in url:
        return url.rsplit("/", 1)[-1].replace("-", " ").replace("_", " ").title()
    return _text(site.get("url"))


def _storage_rows(snap: TenantSnapshot) -> dict[str, dict]:
    return {url_key(r.get("url")): r for r in snap.objects("sharepoint_storage", "site_storage") or [] if r.get("url")}


class Site(Subject):
    kind = KIND

    def __init__(self, snap: TenantSnapshot, site: dict, now: datetime):
        super().__init__(snap, site, now)
        self.site = site
        self.url = _text(site.get("url"))
        self.key = site_key(site)
        self.type = site_type(site)
        self.storage_collected = snap.has("sharepoint_storage")
        self.storage = _storage_rows(snap).get(url_key(self.url))
        self.name = _name(site, self.storage)
        self.path = site_path(self.tenant_id, self.key)
        self.group = self._group()

    def is_me(self, href: str) -> bool:
        return href == self.path

    def mine(self, row: dict) -> bool:
        """Whether a storage or shared-files row is on this site."""
        return not row.get("personal") and url_key(row.get("siteUrl")) == url_key(self.url)

    def _group(self) -> dict | None:
        for g in self.snap.objects("groups", "group") or []:
            if g.get("kind") == "microsoft365" and _text(g.get("mail")) and matches_group(self.site, g["mail"]):
                return g
        return None

    @property
    def used(self) -> int:
        """Storage used: SharePoint's own quota figure when storage was read, else the usage report's."""
        if self.storage is not None and isinstance(self.storage.get("used"), int):
            return self.storage["used"]
        return _int(self.site.get("storageUsed"))

    @property
    def limit(self) -> int | None:
        limit = (self.storage or {}).get("limit")
        return limit if isinstance(limit, int) and limit > 0 else None


# -- Parts --------------------------------------------------------------------------------


def _storage_history(s: Site) -> list[tuple[datetime, int]]:
    """(collected at, storage used) for each retained usage collection, oldest first."""
    out = []
    for finished, data in store.object_copies(s.snap.conn, s.tenant_id, "sharepoint_usage", "site", s.id):
        when = parse_ts(finished)
        if when and isinstance(data.get("storageUsed"), int):
            out.append((when, data["storageUsed"]))
    return out


def _signed(n: int) -> str:
    if not n:
        return "No change"
    return ("+" if n > 0 else "−") + size(abs(n))


def _percent(part: int, whole: int | None) -> str:
    return f"{100 * part / whole:.0f}%" if whole else ""


@part(KIND, 10)
def storage_part(s: Site) -> Part:
    result = Part("storage", "Storage and growth")
    used, limit, st = s.used, s.limit, s.storage
    result.summary = size(used)
    result.facts.append(
        (
            "Storage used",
            size(used) + (f" of {size(limit)} ({_percent(used, limit)})" if limit else ""),
        )
    )
    if not limit:
        result.facts.append(("Site limit", "None of its own: it uses the client's SharePoint storage"))
    if st is not None:
        for key, label in (
            ("current", "Current files"),
            ("versions", "Older versions of files"),
            ("recycleBin", "Recycle bin"),
            ("preservationHold", "Preservation Hold library"),
            ("staleBytes", "Files untouched for two years"),
        ):
            if _int(st.get(key)):
                result.facts.append(
                    (label, f"{size(st[key])} ({_percent(st[key], used)} of the site)" if used else size(st[key]))
                )
    files = _int(s.site.get("files"))
    result.facts.append(("Files", f"{files:,}"))

    state = _text((st or {}).get("state"))
    if limit and (used >= NEAR_LIMIT * limit or state in STATES):
        severity = STATES.get(state) or ("high" if used >= limit else "medium")
        result.tone = "bad" if severity == "high" else "warn"
        s.flag(
            severity,
            f"The site is {_percent(used, limit)} full ({size(used)} of its {size(limit)} limit). Raise its limit "
            "or clear older versions and the recycle bin.",
            "storage",
        )
    versions = _int((st or {}).get("versions"))
    if used and versions >= used / 2 and versions >= 1024**3:
        s.flag(
            "low",
            f"Older versions of files take {size(versions)}, {_percent(versions, used)} of the site. A version "
            "limit on its libraries stops this growing.",
            "storage",
        )

    history = _storage_history(s)
    if len(history) >= 2:
        points = [(when.date(), float(n)) for when, n in history]
        result.chart = storage_chart(points, "Site storage over time", s.now, float(limit) if limit else None)
        months: dict[tuple[int, int], int] = {}
        for when, n in history:
            months[(when.year, when.month)] = n
        table = Section("Storage by month", ["Month", "Storage", "Change"], intro="The last collection of each month.")
        previous = None
        for (y, mo), n in sorted(months.items()):
            table.add([f"{date(y, mo, 1):%B %Y}", size(n), _signed(n - previous) if previous is not None else ""])
            previous = n
        table.rows.reverse()
        table.flags.reverse()
        result.tables.append(table)
        first, last = history[0], history[-1]
        days = (last[0] - first[0]).days
        if days >= 7 and last[1] != first[1]:
            result.note = f"{_signed(last[1] - first[1])} since {_day(first[0])} ({days} days)."
    else:
        result.note = "Growth shows once Office Sentry has two collections of this site's storage."

    if not s.storage_collected:
        result.note = (result.note + " " if result.note else "") + (
            "Libraries and the largest files show once SharePoint storage is collected."
        )
        return result
    libs = [x for x in s.snap.objects("sharepoint_storage", "library_storage") or [] if s.mine(x)]
    if libs:
        table = Section(
            "Libraries",
            ["Library", "Size", "Current files", "Older versions", "Files", "Last change"],
            intro="Every document library on the site, largest first.",
        )
        for x in sorted(libs, key=lambda x: -_int(x.get("total"))):
            name = _text(x.get("library")) or "Library"
            table.add(
                [
                    Link(name, x["webUrl"]) if _text(x.get("webUrl")) else name,
                    size(_int(x.get("total"))),
                    size(_int(x.get("current"))),
                    size(_int(x.get("versions"))),
                    f"{_int(x.get('files')):,}",
                    _day(_text(x.get("lastModified"))) if x.get("lastModified") else "None",
                ],
                "medium" if x.get("preservationHold") and _int(x.get("total")) else None,
            )
        result.tables.append(table)
    largest = sorted(
        (r for r in s.snap.objects("sharepoint_storage", "large_file") or [] if s.mine(r)),
        key=lambda r: -_int(r.get("total")),
    )[:TOP_FILES]
    if largest:
        table = Section(
            "Largest files",
            ["File", "Library", "Size", "Older versions", "Last change"],
            intro="Size includes older versions.",
        )
        for r in largest:
            text = _text(r.get("path")) or _text(r.get("name")) or "?"
            table.add(
                [
                    Link(text, r["webUrl"]) if _text(r.get("webUrl")) else text,
                    _text(r.get("library")),
                    size(_int(r.get("total"))),
                    size(max(_int(r.get("total")) - _int(r.get("current")), 0)),
                    _day(_text(r.get("lastModified"))) if r.get("lastModified") else "",
                ]
            )
        result.tables.append(table)
    if st is not None and not st.get("complete", True):
        result.note = (
            result.note + " " if result.note else ""
        ) + "The site was too large to read in full, so its figures are partial."
    return result


def _shared_items(s: Site) -> list[dict] | None:
    items = s.snap.objects("shared_files", "shared_item")
    if items is None:
        return None
    return [i for i in items if s.mine(i)]


def _shared_with(i: dict) -> list[str]:
    out = []
    if i.get("exposure") == "anyone":
        out.append("Anyone with the link")
    out += [p for p in i.get("externalPeople") or [] if isinstance(p, str)]
    if _int(i.get("orgLinks")):
        out.append("The whole organisation")
    return out


@part(KIND, 20)
def sharing_part(s: Site) -> Part:
    result = Part("sharing", "Sharing")
    site = s.site
    anyone, guest = _int(site.get("anyoneLinks")), _int(site.get("guestLinks"))
    company, member = _int(site.get("companyLinks")), _int(site.get("memberLinks"))
    result.facts.append(("External sharing", "On" if site.get("externalSharing") else "Off"))
    result.facts.append(("Anyone links", f"{anyone:,}" + (" (open without signing in)" if anyone else "")))
    result.facts.append(("Links for people outside", f"{guest:,}"))
    result.facts.append(("Links for the whole organisation", f"{company:,}"))
    result.facts.append(("Links for named people inside", f"{member:,}"))
    result.facts.append(("Sensitivity label", "Set" if _text(site.get("sensitivityLabel")) else "None"))
    policy = _text(site.get("unmanagedDevicePolicy"))
    if policy:
        result.facts.append(
            (
                "Unmanaged devices",
                {"None": "Full access", "AllowLimitedAccess": "Browser only", "BlockAccess": "Blocked"}.get(
                    policy, policy
                ),
            )
        )
    if anyone:
        result.summary, result.tone = plural(anyone, "anyone link"), "bad"
        s.flag(
            "high",
            f"{plural(anyone, 'anyone link')} on the site: whoever has the link opens the file without signing "
            "in, and links get forwarded. Remove them or make them expire.",
            "sharing",
        )
    elif guest:
        result.summary, result.tone = plural(guest, "link") + " outside", "warn"
    elif site.get("externalSharing"):
        result.summary, result.tone = "Shared outside", "warn"
    else:
        result.summary = "Inside only"
    if guest:
        s.flag(
            "low",
            f"{plural(guest, 'sharing link')} for named people outside the organisation. Check each still needs "
            "access.",
            "sharing",
        )

    items = _shared_items(s)
    if items is None:
        result.note = "The files behind these links show once Shared files is scanned."
    elif items := [i for i in items if i.get("exposure")]:  # not those shared only with colleagues
        table = Section(
            "Files shared from this site",
            ["File or folder", "Library", "Shared with", "Can edit", "Last modified"],
            intro="From the last Shared files scan: files and folders shared with anyone, people outside or "
            "the whole organisation.",
        )
        rank = {"anyone": 0, "external": 1, "organization": 2}
        for i in sorted(items, key=lambda i: (rank.get(_text(i.get("exposure")), 3), _text(i.get("path"))))[
            :SHARED_ROWS
        ]:
            text = (_text(i.get("path")) or _text(i.get("name")) or "?") + (" (folder)" if i.get("folder") else "")
            exposure = _text(i.get("exposure"))
            table.add(
                [
                    Link(text, i["webUrl"]) if _text(i.get("webUrl")) else text,
                    _text(i.get("library")),
                    _shared_with(i),
                    "Yes" if outside_can_edit(i) or org_can_edit(i) else "No",
                    _day(_text(i.get("lastModified"))) if i.get("lastModified") else "",
                ],
                "high" if anyone_can_edit(i) else ("medium" if exposure == "anyone" else None),
            )
        result.tables.append(table)
        if len(items) > SHARED_ROWS:
            result.note = f"The first {SHARED_ROWS} of {len(items):,} shared items; the Shared files report has all."
        editable = [i for i in items if anyone_can_edit(i)]
        if editable:
            s.flag(
                "high",
                f"{plural(len(editable), 'file or folder', 'files or folders')} can be changed by anyone with the "
                "link, without signing in.",
                "sharing",
            )
    else:
        result.note = (
            "The last Shared files scan found nothing on this site shared with anyone, people outside or the "
            "whole organisation."
        )
    extra = " Who can open the site itself (owners, members, visitors) is in the SharePoint permission reports."
    result.note = (result.note + extra) if result.note else extra.strip()
    return result


@part(KIND, 30)
def team_part(s: Site) -> Part:
    g = s.group
    if g is None:
        result = Part("owner", "Owner")
        owner = _text(s.site.get("ownerPrincipalName")) or _text(s.site.get("owner"))
        if s.type == "Group or Team site":
            result.summary = "Group not found"
            result.empty = "No Microsoft 365 group in the last Groups and Teams collection matches this team site."
        else:
            result.summary = "No group"
            result.empty = "This site isn't connected to a Microsoft 365 group."
        if owner:
            result.facts.append(("Primary owner", owner))
        return result
    team = bool(g.get("isTeam"))
    result = Part("team", "Team" if team else "Microsoft 365 group")
    name = _text(g.get("displayName")) or g["_id"]
    result.facts.append(("Group", PageLink(name, object_path(s.tenant_id, "groups", g["_id"]))))
    result.facts.append(("Type", "Team" if team else "Microsoft 365 group"))
    if _text(g.get("visibility")):
        result.facts.append(("Visibility", g["visibility"]))
    result.summary = name
    if g.get("examined"):
        owners, active = _int(g.get("ownerCount")), _int(g.get("enabledOwnerCount"))
        members, guests = _int(g.get("memberCount")), _int(g.get("guestCount"))
        result.facts.append(
            (
                "Owners",
                "None" if not owners else (f"{active} of {owners} active" if active != owners else f"{owners:,}"),
            )
        )
        result.facts.append(("Members", f"{members:,}"))
        result.facts.append(("Guests", f"{guests:,}"))
        if not active:
            result.tone = "bad"
            s.flag(
                "medium",
                f"The {'team' if team else 'group'} that owns the site has no active owner, so nobody approves "
                "who joins it or keeps the site tidy.",
                "team",
            )
        elif guests:
            result.tone = "warn"
    result.note = "Members of the group can open the site. The group's page has who they are."
    return result


@part(KIND, 40)
def activity_part(s: Site) -> Part:
    result = Part("activity", "Activity")
    site = s.site
    last = _text(site.get("lastActivityDate"))
    day = parse_day(last)
    days = (s.end.date() - day).days if day else None
    result.facts.append(("Last activity", _day(last) if last else "None recorded"))
    result.facts.append(("Files used in the last 30 days", f"{_int(site.get('activeFiles')):,}"))
    result.facts.append(("Page views in the last 30 days", f"{_int(site.get('pageViews')):,}"))
    if (st := s.storage) is not None and st.get("lastModified"):
        result.facts.append(("Last file change", _day(_text(st["lastModified"]))))
    if days is None or days >= INACTIVE_DAYS:
        result.summary, result.tone = "Inactive", "warn"
        if s.type != "System":
            s.flag(
                "low",
                f"Nobody has used the site in {INACTIVE_DAYS} days or more. Archive or delete it, or confirm "
                "someone still owns the content.",
                "activity",
            )
    else:
        result.summary = "Active" if days <= 30 else f"{days} days ago"
    if _text(site.get("reportDate")):
        result.note = f"Microsoft's figures as of {_day(site['reportDate'])}."
    return result


# -- History, about, reports --------------------------------------------------------------


def _history(s: Site) -> tuple[list[Event], str | None]:
    conn, tid = s.snap.conn, s.tenant_id
    events: list[Event] = []
    copies = store.object_copies(conn, tid, "sharepoint_usage", "site", s.id)
    since = copies[0][0] if copies else None
    for (_, old), (when, new) in zip(copies, copies[1:], strict=False):
        day = when[:10]
        if bool(old.get("externalSharing")) != bool(new.get("externalSharing")):
            on = bool(new.get("externalSharing"))
            events.append(
                Event(day, "Sharing", f"External sharing turned {'on' if on else 'off'}", "warn" if on else None)
            )
        for key, label in (("anyoneLinks", "Anyone links"), ("guestLinks", "Links for people outside")):
            a, b = _int(old.get(key)), _int(new.get(key))
            if a != b:
                events.append(Event(day, "Sharing", f"{label} went from {a:,} to {b:,}", "warn" if b > a else None))
        a, b = _text(old.get("ownerPrincipalName")), _text(new.get("ownerPrincipalName"))
        if a and b and a.lower() != b.lower():
            events.append(Event(day, "Owner", f"Primary owner changed from {a} to {b}"))
        a, b = _text(old.get("sensitivityLabel")), _text(new.get("sensitivityLabel"))
        if a != b:
            events.append(
                Event(day, "Settings", "Sensitivity label " + ("removed" if not b else "set" if not a else "changed"))
            )
        a, b = _int(old.get("storageUsed")), _int(new.get("storageUsed"))
        if a and b and abs(b - a) >= max(a // 4, 1024**3):  # big jumps only; the chart has the rest
            events.append(Event(day, "Storage", f"{_signed(b - a)} to {size(b)}", "warn" if b > a else None))

    runs = conn.execute(
        "SELECT id, finished_at FROM runs WHERE tenant_id = ? AND collector = 'shared_files' AND status IN"
        " ('succeeded', 'partial') AND EXISTS (SELECT 1 FROM objects o WHERE o.run_id = runs.id)"
        " ORDER BY finished_at, id",
        (tid,),
    ).fetchall()
    if len(runs) >= 2:
        held: dict[int, set[str]] = {r["id"]: set() for r in runs}
        for _, _, run_id, d in store.matching_objects(conn, tid, "shared_files", "shared_item", "siteUrl", s.url):
            if run_id in held and not d.get("personal") and d.get("exposure") in ("anyone", "external"):
                held[run_id].add(
                    f"{_text(d.get('path')) or _text(d.get('name'))} shared with "
                    + ("anyone" if d.get("exposure") == "anyone" else "people outside")
                )
        pairs = [(r["finished_at"], held[r["id"]]) for r in runs]
        for (_, old), (when, new) in zip(pairs, pairs[1:], strict=False):
            events.extend(Event(when[:10], "Shared files", x, "warn") for x in sorted(new - old))
            events.extend(Event(when[:10], "Shared files", f"{x}: no longer") for x in sorted(old - new))
    events.sort(key=lambda e: e.day, reverse=True)
    return events[:HISTORY_LIMIT], since


def _about(s: Site) -> list[tuple[str, object]]:
    site = s.site
    facts: list[tuple[str, object]] = [("Address", s.url), ("Type", s.type)]
    owner = _text(site.get("ownerPrincipalName")) or _text(site.get("owner"))
    if owner:
        facts.append(("Primary owner", owner))
    if _text(site.get("template")):
        facts.append(("Template", site["template"]))
    if s.storage is not None and isinstance(s.storage.get("libraries"), int):
        facts.append(("Libraries", f"{s.storage['libraries']:,}"))
    return facts


def _reports(s: Site) -> list[tuple[str, str]]:
    links = [("SharePoint sites", s.report("sharepoint", "All sites", "Site", s.url))]
    if s.storage_collected:
        links.append(("SharePoint storage", s.report("sharepoint-storage", "Sites by storage", "Site", s.name)))
    if s.snap.has("shared_files"):
        links.append(("Shared files", s.report("shared-files")))
    for key in PERMISSION_REPORTS:
        if key in REPORTS:
            links.append((REPORTS[key][0], s.report(key)))
    if s.group is not None:
        links.append(
            ("Groups and Teams", s.report("groups", "Teams and groups", "Name", _text(s.group.get("displayName"))))
        )
    return links


def build(conn, tenant, key: str, now: datetime | None = None) -> ObjectPage | None:
    """The page for one site, or None when the latest usage report doesn't have it."""
    now = now or datetime.now(UTC)
    snap = TenantSnapshot(conn, tenant)
    site = find_site(snap, key)
    if site is None:
        return None
    s = Site(snap, site, now)
    tid = tenant["id"]
    page = ObjectPage(
        KIND,
        dict(tenant),
        s.key,
        now,
        s.name,
        s.path,
        "Sites",
        f"/tenants/{tid}/{URL}",
        subtitle=s.url,
        description=f"Everything collected about the SharePoint site {s.url}.",
        about_title="Site",
        external=("Open in SharePoint", s.url) if s.url.lower().startswith("https://") else None,
        nothing="Nothing stands out for this site in the data collected.",
    )
    page.parts = build_parts(s)
    page.about = _about(s)
    page.attention = sort_attention(s.attention)
    ids = {s.id, s.key, url_key(s.url), _text((s.storage or {}).get("_id"))}
    ids |= {i["_id"] for i in _shared_items(s) or []}
    page.findings = findings_for(snap, ids - {""}, {url_key(s.url), s.url.lower()})
    page.history, page.history_since = _history(s)
    page.reports = _reports(s)
    page.sources = sources_for(snap, SOURCES)

    page.badges = [(s.type, None)]
    if site.get("externalSharing"):
        page.badges.append(("Shared outside", "warn"))
    if _text(site.get("sensitivityLabel")):
        page.badges.append(("Labelled", None))

    parts = {p.key: p for p in page.parts}
    storage = parts["storage"]
    foot = f"{_percent(s.used, s.limit)} of {size(s.limit)}" if s.limit else "No limit of its own"
    page.tiles = [attention_tile(page.attention), Highlight("Storage", size(s.used), foot, storage.tone)]
    sharing = parts["sharing"]
    page.tiles.append(Highlight("Sharing", sharing.summary, "From Microsoft's link counts", sharing.tone))
    team = parts.get("team")
    if team is not None:
        page.tiles.append(Highlight(team.title, team.summary, "Its members can open the site", team.tone))
    activity = parts["activity"]
    page.tiles.append(
        Highlight("Last activity", _day(_text(site.get("lastActivityDate"))) or "None", activity.summary, activity.tone)
    )
    return page


# -- The Sites page -----------------------------------------------------------------------


@dataclass
class SiteRow:
    key: str
    name: str
    url: str
    type: str
    storage: str
    storage_sort: int
    share: int | None  # percent of the site's own limit
    files: int
    anyone: int
    guest: int
    last_activity: str
    inactive: bool
    findings: int


def directory(conn, tenant, now: datetime | None = None) -> tuple[list[SiteRow], list[Highlight], str] | None:
    """Every site with the few facts that tell you where to look, the headline figures and a notice (names
    concealed by Microsoft). None before SharePoint usage is collected."""
    now = now or datetime.now(UTC)
    snap = TenantSnapshot(conn, tenant)
    rows = sites(snap)
    if rows is None:
        return None
    concealed = any(r.get("concealed") for r in snap.objects("sharepoint_usage", "site") or [])
    storage = _storage_rows(snap)
    keys: dict[str, str] = {}
    for r in rows:
        keys[url_key(r["url"])] = site_key(r)
        if r.get("siteId"):
            keys[_text(r["siteId"]).lower()] = site_key(r)
    # A finding about a shared file counts for the site it's on, as on the site's page.
    for i in snap.objects("shared_files", "shared_item") or []:
        if not i.get("personal") and (key := keys.get(url_key(i.get("siteUrl")))) and _text(i.get("_id")):
            keys[url_key(i["_id"])] = key
    counts: dict[str, int] = {}
    for f in conn.execute(
        "SELECT object_id, subject FROM findings WHERE tenant_id = ? AND resolved_at IS NULL", (tenant["id"],)
    ):
        key = keys.get(url_key(f["object_id"])) or keys.get(url_key(f["subject"]))
        if key:
            counts[key] = counts.get(key, 0) + 1
    end = parse_ts(snap.as_of) or now
    out = []
    for r in rows:
        st = storage.get(url_key(r["url"]))
        used = st["used"] if st is not None and isinstance(st.get("used"), int) else _int(r.get("storageUsed"))
        limit = (st or {}).get("limit")
        day = parse_day(r.get("lastActivityDate"))
        inactive = day is None or (end.date() - day).days >= INACTIVE_DAYS
        out.append(
            SiteRow(
                site_key(r),
                _name(r, st),
                r["url"],
                site_type(r),
                size(used),
                used,
                round(100 * used / limit) if isinstance(limit, int) and limit > 0 else None,
                _int(r.get("files")),
                _int(r.get("anyoneLinks")),
                _int(r.get("guestLinks")),
                _text(r.get("lastActivityDate")),
                inactive,
                counts.get(site_key(r), 0),
            )
        )
    out.sort(key=lambda r: (r.type == "System", -r.findings, -r.anyone, -r.storage_sort, fold(r.name)))
    user = [r for r in out if r.type != "System"]
    system = len(out) - len(user)
    shared = [r for r in user if r.anyone]
    idle = [r for r in user if r.inactive]
    full = [r for r in user if r.share is not None and r.share >= NEAR_LIMIT * 100]
    tiles = [
        Highlight("Sites", len(user), f"plus {plural(system, 'system site')}" if system else ""),
        Highlight("Storage used", size(sum(r.storage_sort for r in out)), "Every site together"),
        Highlight("With anyone links", len(shared), "Files open without signing in", "bad" if shared else "ok"),
        Highlight("Inactive", len(idle), f"No activity in {INACTIVE_DAYS} days", "warn" if idle else None),
        Highlight("Near their limit", len(full), f"Over {NEAR_LIMIT:.0%} of their own limit", "warn" if full else None),
    ]
    return out, tiles, CONCEALED_NOTICE if concealed else ""
