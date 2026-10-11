"""One page per group: a team, a Microsoft 365 group, a security group or a distribution list.

Everything Office Sentry has collected about one group, from stored snapshots only: who owns it and who
is in it (accounts linked to their pages, guests, groups inside it), what it gives its members (Conditional
Access policies, admin roles, licences), its mail settings and traffic for a distribution list, its team
site's storage and sharing, its structure (dynamic rule, nesting, expiry), admin changes that named it,
open findings and what changed between collections. Built the way the mailbox page is (``object_page``):
each area is a ``@part("group", order)`` function returning a ``Part``.

The Entra group object ID is the key: Groups and Teams and Group nesting store every group by it, and
Distribution lists stores Exchange's ExternalDirectoryObjectId, which is the same ID.

Orders are spaced by 10. Order 15 is kept free for Teams detail (channels, apps and the guests in each,
which a later collector will add) so it slots in between the people and what the group gives them.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from .. import store
from ..checks.groups import group_owner_problem
from ..collectors.groups import GROUP_LIMIT, MEMBER_IDS_KEPT
from ..devices import parse_ts
from ..licences import sku_name
from ..snapshot import TenantSnapshot
from ..text import plural
from .data_reports import PAGE_INDEXES, VISIBILITY, Highlight, Link, PageIndex, PageLink, Section
from .distribution_lists import MEMBER_TYPES, _activity, _Owners, _recent
from .distribution_lists import RECENT_DAYS as MAIL_DAYS
from .distribution_lists import UNUSED_DAYS as MAIL_UNUSED_DAYS
from .group_structure import NEW_DAYS, PROCESSING
from .mail_reports import _size
from .object_page import (
    ObjectPage,
    Subject,
    _day,
    _when,
    attention_tile,
    build_parts,
    findings_for,
    object_path,
    part,
    sort_attention,
    sources_for,
)
from .profile.base import HISTORY_LIMIT, Event, Part
from .site_page import matches_group, site_key, site_path

KIND = "group"
URL = "groups"
SOURCES = ("groups", "group_nesting", "distribution_lists", "conditional_access", "admin_roles", "sharepoint_usage")
KINDS = {
    "microsoft365": "Microsoft 365 group",
    "security": "Security group",
    "mail_security": "Mail-enabled security group",
    "distribution": "Distribution list",
}
EXPIRY_SOON_DAYS = 30
BIG_OPEN_LIST = 50  # members above which a list anyone outside can mail is worth a look
ADMIN_CHANGE_DAYS = 90
ADMIN_CHANGE_LIMIT = 30
MEMBER_ROWS = 500  # members listed on the page; the count is always complete
GROUP_COLUMN_WORDS = ("group", "team", "list", "member of", "owner of", "inside of")


def group_path(tenant_id: int, gid: str) -> str:
    return object_path(tenant_id, URL, gid)


def _text(value) -> str:
    return value if isinstance(value, str) else ""


def _lower(value) -> str:
    return _text(value).lower()


def group_type(g: dict) -> str:
    """Team, Microsoft 365 group, Security group, Mail-enabled security group or Distribution list."""
    if g.get("kind") == "microsoft365":
        return "Team" if g.get("isTeam") else "Microsoft 365 group"
    return KINDS.get(_text(g.get("kind")), "Group")


def groups(snap: TenantSnapshot) -> list[dict] | None:
    """Every group, from Groups and Teams, or None before it's collected. Groups that collector didn't see
    but Group nesting did (collected at different times) are added from there."""
    rows = snap.objects("groups", "group")
    if rows is None:
        return None
    out = {g["_id"]: g for g in rows if isinstance(g.get("_id"), str)}
    for g in snap.objects("group_nesting", "group") or []:
        if isinstance(g.get("_id"), str):
            out.setdefault(g["_id"], g)
    return list(out.values())


def find_list(snap: TenantSnapshot, gid: str, mail: str) -> dict | None:
    """Exchange's distribution list for a group: by object ID, or by address for a list collected before
    Exchange returned the ID."""
    lists = snap.objects("distribution_lists", "distribution_list") or []
    found = next((d for d in lists if d.get("_id") == gid), None)
    if found is None and mail:
        found = next((d for d in lists if _lower(d.get("PrimarySmtpAddress")) == mail.lower()), None)
    return found


def find_group(snap: TenantSnapshot, gid: str) -> dict | None:
    return next((g for g in groups(snap) or [] if g["_id"] == gid), None)


def group_column(title: str, column: str) -> bool:
    """Whether a report column may hold a bare group name: tables about groups, and columns named for them."""
    column = column.lower()
    return "group" in title.lower() or any(word in column for word in GROUP_COLUMN_WORDS)


def _index(snap: TenantSnapshot) -> dict[str, str]:
    """Group names that are unique in the tenant -> page path (names two groups share link nowhere)."""
    tid = snap.tenant["id"]
    seen: dict[str, str | None] = {}
    for g in groups(snap) or []:
        name = _lower(g.get("displayName"))
        if name:
            seen[name] = None if name in seen else group_path(tid, g["_id"])
    return {name: href for name, href in seen.items() if href}


def _mail_index(snap: TenantSnapshot) -> dict[str, str]:
    """Group addresses that aren't an account's -> page path; an address is unmistakable in any column."""
    tid = snap.tenant["id"]
    accounts = {_lower(u.get(k)) for u in snap.objects("users", "user") or [] for k in ("userPrincipalName", "mail")}
    out: dict[str, str] = {}
    for g in groups(snap) or []:
        mail = _lower(g.get("mail"))
        if mail and mail not in accounts:
            out.setdefault(mail, group_path(tid, g["_id"]))
    return out


PAGE_INDEXES.append(PageIndex(_index, group_column))
PAGE_INDEXES.append(PageIndex(_mail_index))


class _People:
    """The tenant's accounts by object ID and by address, for naming members and owners."""

    def __init__(self, snap: TenantSnapshot):
        self.by_id: dict[str, dict] = {}
        self.by_key: dict[str, dict] = {}
        for u in snap.objects("users", "user") or []:
            if isinstance(u.get("_id"), str):
                self.by_id[u["_id"]] = u
            for key in ("userPrincipalName", "mail"):
                if _text(u.get(key)):
                    self.by_key.setdefault(u[key].lower(), u)

    def find(self, uid=None, address=None) -> dict | None:
        return self.by_id.get(uid) if uid in self.by_id else self.by_key.get(_lower(address))

    @staticmethod
    def account(u: dict | None) -> str:
        """Active, Sign-in blocked, Guest, Guest (sign-in blocked)."""
        if u is None:
            return "Not in the user list"
        guest = u.get("userType") == "Guest"
        if u.get("accountEnabled") is False:
            return "Guest (sign-in blocked)" if guest else "Sign-in blocked"
        return "Guest" if guest else "Active"

    @staticmethod
    def label(u: dict | None, fallback: str) -> str:
        return _text((u or {}).get("userPrincipalName")) or fallback


class Group(Subject):
    kind = KIND

    def __init__(self, snap: TenantSnapshot, g: dict, now: datetime):
        super().__init__(snap, g, now)
        self.g = g
        self.name = _text(g.get("displayName")) or self.id
        self.mail = _text(g.get("mail"))
        self.type = group_type(g)
        self.m365 = g.get("kind") == "microsoft365"
        self.people = _People(snap)
        self.nesting = next((n for n in snap.objects("group_nesting", "group") or [] if n.get("_id") == self.id), None)
        self.dl = find_list(snap, self.id, self.mail)
        self.keys = {k for k in (self.name.lower(), self.mail.lower()) if k}
        self.path = group_path(self.tenant_id, self.id)

    def is_me(self, href: str) -> bool:
        return href == self.path

    def named(self, value) -> bool:
        return isinstance(value, str) and value.lower() in self.keys

    @property
    def examined(self) -> bool:
        """Whether owners and members were read (Groups and Teams reads them for teams and Microsoft 365 groups)."""
        return bool(self.g.get("examined"))

    @property
    def member_count(self) -> int | None:
        # Exchange counts a list's people and contacts; Group nesting counts every direct member, groups included.
        for source in (self.g,) if self.m365 else (self.dl, self.nesting):
            if (
                source
                and (source.get("examined") or source.get("membersRead"))
                and isinstance(source.get("memberCount"), int)
            ):
                return source["memberCount"]
        return None

    @property
    def created(self) -> str:
        return _text(self.g.get("createdDateTime")) or _text((self.dl or {}).get("WhenCreatedUTC"))

    @property
    def membership(self) -> str:
        n = self.nesting or {}
        if self.g.get("dynamic") or n.get("dynamic"):
            return "Dynamic"
        if self.g.get("synced") or n.get("synced") or (self.dl or {}).get("IsDirSynced"):
            return "Synced from on-premises AD"
        return "Assigned"


# -- Parts --------------------------------------------------------------------------------


@part(KIND, 10)
def owners_part(m: Group) -> Part:
    result = Part("owners", "Owners")
    g = m.g
    if m.m365:
        if not m.examined:
            result.summary = "Not checked"
            result.empty = (
                f"Owners and members are read for the first {GROUP_LIMIT} teams and Microsoft 365 groups "
                "(teams first); this one wasn't among them."
            )
            return result
        owners = g.get("owners") or []
        if not owners:
            noun = "team" if m.type == "Team" else m.type
            result.summary, result.tone, result.empty = "None", "bad", f"Nobody owns this {noun}."
        else:
            table = Section("Owners", ["Owner", "Account", "Status"])
            for o in owners:
                u = m.people.find(address=o.get("upn"))
                if o.get("type") != "user":
                    status = "App"
                elif u is None:
                    status = "Sign-in blocked" if o.get("enabled") is False else "Active"
                else:
                    status = m.people.account(u)
                table.add(
                    [_text(o.get("name")) or "?", _text(o.get("upn")) or "", status],
                    None if o.get("enabled") else "medium",
                )
            more = (g.get("ownerCount") or 0) - len(owners)
            if more > 0:
                table.add([f"{more} more", "", ""])
            result.tables.append(table)
            enabled = g.get("enabledOwnerCount") or 0
            result.summary = plural(g.get("ownerCount") or len(owners), "owner")
            if enabled < (g.get("ownerCount") or 0):
                result.summary += f", {enabled} active"
        problem = group_owner_problem(g)
        if problem:
            result.tone = "bad"
            m.flag(
                "medium" if g.get("isTeam") else "low",
                f"{problem[0].upper()}{problem[1:]}: nobody active approves members and guests or renews it. "
                "Add an owner.",
                "owners",
            )
        elif (g.get("enabledOwnerCount") or 0) == 1 and (g.get("memberCount") or 0) > 1:
            result.note = "One owner: if they leave, nobody looks after the group. Two is safer."
        return result
    # Security groups and distribution lists: Exchange's ManagedBy is the only owner list collected.
    if m.dl is not None:
        resolver = _Owners(m.snap)
        names = [o for o in m.dl.get("ManagedBy") or [] if isinstance(o, str) and o]
        if not names:
            result.summary, result.tone, result.empty = "None", "warn", "No owner is set on this list."
            m.flag("low", "No owner is set on this list, so nobody approves changes to it. Set one.", "owners")
            return result
        table = Section("Owners", ["Owner", "Account", "Status"])
        active = 0
        for name in names:
            label, u = resolver.resolve(name)
            status = m.people.account(u) if u is not None else "Not in the user list"
            ok = u is None or u.get("accountEnabled") is not False
            active += ok
            table.add(
                [name if label == name else _text(u.get("displayName")) or name, label, status],
                None if ok else "medium",
            )
        result.tables.append(table)
        result.summary = plural(len(names), "owner") + ("" if active == len(names) else f", {active} active")
        if not active:
            result.tone = "bad"
            m.flag(
                "low", "Only leavers own this list, so nobody active approves changes to it. Set an owner.", "owners"
            )
        return result
    result.summary = "Not read"
    if m.g.get("kind") in ("distribution", "mail_security"):
        result.empty = (
            "Distribution lists didn't return a list with this group's directory id or address, so Exchange's "
            "owners and settings for it aren't shown."
            if m.snap.has("distribution_lists")
            else "Exchange's owner list for it shows once Distribution lists has been collected."
        )
    else:
        result.empty = (
            "Owners of security groups aren't collected; Groups and Teams reads owners of teams and "
            "Microsoft 365 groups."
        )
    return result


# Order 15: Teams detail (channels, apps, guests per channel) goes here once collected.


@part(KIND, 20)
def members_part(m: Group) -> Part:
    result = Part("members", "Members")
    g = m.g
    tables = []
    if m.m365:
        if not m.examined:
            result.summary, result.empty = "Not checked", "Members weren't read for this group (see Owners)."
            return result
        table = Section("Members", ["Account", "Name", "Type", "Status"])
        ids = [i for i in g.get("memberIds") or [] if isinstance(i, str)]
        listed: set[str] = set()
        unmatched = 0
        for uid in ids[:MEMBER_ROWS]:
            u = m.people.find(uid=uid)
            if u is None:
                unmatched += 1
                continue
            listed.add(_lower(u.get("userPrincipalName")))
            listed.add(_lower(u.get("mail")))
            guest = u.get("userType") == "Guest"
            table.add(
                [
                    m.people.label(u, uid),
                    _text(u.get("displayName")),
                    "Guest" if guest else "Member",
                    m.people.account(u),
                ],
                "low" if u.get("accountEnabled") is False else None,
            )
        for address in g.get("guests") or []:
            if _lower(address) not in listed:
                u = m.people.find(address=address)
                table.add(
                    [address, _text((u or {}).get("displayName")), "Guest", m.people.account(u) if u else "Guest"]
                )
                if u is None and g.get("memberIdsComplete") is not False:
                    unmatched -= 1  # its object ID was among the unmatched ones
        unmatched = max(unmatched, 0)
        notes = []
        if len(ids) > MEMBER_ROWS:
            notes.append(f"The first {MEMBER_ROWS} of {len(ids):,} members are listed")
        if g.get("memberIdsComplete") is False:
            notes.append(f"Office Sentry keeps the first {MEMBER_IDS_KEPT:,} members of a group")
        if unmatched:
            notes.append(plural(unmatched, "other member") + " (groups, devices or contacts) not shown")
        if table.rows:
            tables.append(table)
        count = g.get("memberCount") or 0
        guests = g.get("guestCount") or 0
        result.summary = plural(count, "member") + (
            f", {guests} {'guest' if guests == 1 else 'guests'}" if guests else ""
        )
        result.note = "; ".join(notes) + ("." if notes else "")
        if guests and g.get("visibility") == "Private" and g.get("isTeam"):
            result.tone = "warn"
        if not count:
            result.empty = "Nobody is a member."
    elif m.dl is not None and m.dl.get("membersRead"):
        table = Section("Members", ["Member", "Address", "Type"])
        for mem in m.dl.get("members") or []:
            if not isinstance(mem, dict):
                continue
            kind = _text(mem.get("type"))
            u = m.people.find(uid=mem.get("id"), address=mem.get("address"))
            table.add(
                [
                    _text(mem.get("name")) or _text(mem.get("address")) or "?",
                    _text(mem.get("address")),
                    MEMBER_TYPES.get(kind, kind or "Unknown"),
                ],
                "low" if u is not None and u.get("accountEnabled") is False else None,
            )
        count = m.dl.get("memberCount") if isinstance(m.dl.get("memberCount"), int) else len(table.rows)
        if count > len(table.rows):
            table.add([f"{count - len(table.rows)} more", "", ""])
        if table.rows:
            tables.append(table)
        outside = m.dl.get("externalCount") or 0
        result.summary = plural(count, "member") + (f", {outside} outside" if outside else "")
        if not count:
            result.empty = "Nobody is a member."
    else:
        count = m.member_count
        result.summary = plural(count, "member") if count is not None else "Not read"
        if count is None:
            result.empty = "Members of this group weren't read. Group nesting counts them on its next collection."
        else:
            result.empty = (
                "Members of security groups are counted, not listed: Group nesting reads how many there are and "
                "which are groups."
            )
    # Groups used by Conditional Access have their people (including through nested groups) expanded.
    ca = next((c for c in m.snap.objects("conditional_access", "ca_group") or [] if c.get("_id") == m.id), None)
    if ca is not None and isinstance(ca.get("members"), list) and not m.m365:
        table = Section(
            "People in it, through any nesting",
            ["Account", "Name", "Status"],
            intro="Read for Conditional Access: everyone the group covers, including through groups inside it.",
        )
        for uid in ca["members"][:MEMBER_ROWS]:
            u = m.people.find(uid=uid)
            if u is not None:
                table.add([m.people.label(u, uid), _text(u.get("displayName")), m.people.account(u)])
        if table.rows:
            tables.append(table)
            result.summary = plural(len(ca["members"]), "person", "people") + (
                f" ({result.summary})" if count is not None else ""
            )
            result.empty = ""
    n = m.nesting or {}
    inner = [c for c in n.get("groupIds") or [] if isinstance(c, str)]
    if inner:
        names = {x["_id"]: x for x in groups(m.snap) or []}
        table = Section(
            "Groups inside it", ["Group", "Kind", "Members"], intro="Everyone in these gets what this group gives."
        )
        for gid in inner:
            child = names.get(gid)
            table.add(
                [
                    PageLink(_text((child or {}).get("displayName")) or gid, group_path(m.tenant_id, gid)),
                    group_type(child) if child else "Group",
                    (child or {}).get("memberCount") if (child or {}).get("examined") else "",
                ]
            )
        more = (n.get("groupCount") or 0) - len(inner)
        if more > 0:
            table.add([f"{more} more", "", ""])
        tables.append(table)
    result.tables = tables
    if not result.summary:
        result.summary = plural(m.member_count or 0, "member")
    return result


def _policy_users(p: dict) -> dict:
    conditions = p.get("conditions") if isinstance(p.get("conditions"), dict) else {}
    users = conditions.get("users")
    return users if isinstance(users, dict) else {}


@part(KIND, 30)
def access_part(m: Group) -> Part | None:
    """What being in the group gives: Conditional Access, admin roles, licences."""
    result = Part("access", "What it gives its members")
    tables = []
    policies = m.snap.objects("conditional_access", "ca_policy")
    used = []
    for p in policies or []:
        users = _policy_users(p)
        included = m.id in (users.get("includeGroups") or [])
        excluded = m.id in (users.get("excludeGroups") or [])
        if included or excluded:
            state = {"enabled": "On", "disabled": "Off", "enabledForReportingButNotEnforced": "Report-only"}.get(
                _text(p.get("state")), _text(p.get("state")) or "Unknown"
            )
            used.append(
                (
                    [_text(p.get("displayName")) or p.get("_id"), "Excluded" if excluded else "Included", state],
                    "medium" if excluded and state == "On" else None,
                )
            )
    if used:
        table = Section("Conditional Access policies", ["Policy", "How", "Policy state"])
        for row, flag in used:
            table.add(row, flag)
        tables.append(table)
        excluded = [row[0] for row, _ in used if row[1] == "Excluded" and row[2] == "On"]
        if excluded:
            m.flag(
                "medium",
                f"Members are excluded from "
                f"{plural(len(excluded), 'Conditional Access policy', 'Conditional Access policies')} "
                f"({', '.join(excluded[:3])}{', …' if len(excluded) > 3 else ''}). Check who is in the group.",
                "access",
            )
    roles = [
        r
        for r in m.snap.objects("admin_roles", "role_assignment") or []
        if r.get("principalType") == "group" and r.get("principalId") == m.id and not r.get("viaGroupId")
    ]
    if roles:
        table = Section("Admin roles held through it", ["Role", "Assignment", "Privileged"])
        for r in roles:
            table.add(
                [
                    _text(r.get("roleName")) or "Unknown role",
                    "Eligible" if r.get("assignment") == "eligible" else "Active",
                    bool(r.get("privileged")),
                ],
                "high" if r.get("privileged") else "medium",
            )
        tables.append(table)
        privileged = [_text(r.get("roleName")) for r in roles if r.get("privileged")]
        if privileged:
            m.flag(
                "high",
                f"Everyone in the group holds {', '.join(privileged)}; adding a member makes them an admin.",
                "access",
            )
    skus = {s.get("skuId"): s for s in m.snap.objects("tenant_profile", "subscribed_sku") or []}
    licences = [sid for sid in m.g.get("licenceSkuIds") or [] if isinstance(sid, str)]
    if licences:
        table = Section(
            "Licences given to members", ["Licence"], intro="Group-based licensing: everyone in the group gets these."
        )
        for sid in licences:
            part_number = (skus.get(sid) or {}).get("skuPartNumber")
            table.add([sku_name(part_number) if part_number else "Unknown licence"])
        tables.append(table)
    if not tables:
        if policies is None and m.snap.objects("admin_roles", "role_assignment") is None:
            return None
        result.summary = "Nothing recorded"
        result.empty = "No Conditional Access policy, admin role or licence is tied to this group."
        return result
    result.tables = tables
    bits = []
    if used:
        bits.append(plural(len(used), "policy", "policies"))
    if roles:
        bits.append(plural(len(roles), "admin role"))
    if licences:
        bits.append(plural(len(licences), "licence"))
    result.summary = ", ".join(bits)
    result.tone = (
        "bad"
        if any(f == "high" for t in tables for f in t.flags)
        else ("warn" if any(t.flags and any(t.flags) for t in tables) else None)
    )
    return result


@part(KIND, 40)
def mail_part(m: Group) -> Part | None:
    """A distribution list's or mail-enabled group's address, settings and traffic."""
    if not m.mail and m.dl is None:
        return None
    result = Part("mail", "Email")
    result.facts.append(("Address", m.mail or _text(m.dl.get("PrimarySmtpAddress"))))
    dl = m.dl
    if dl is None:
        result.summary = "Mail-enabled"
        if m.m365:
            result.note = "Mail to the address goes to the group's shared inbox and to members who follow it."
        return result
    outside = dl.get("RequireSenderAuthenticationEnabled") is False
    result.facts.append(("Accepts mail from outside", "Yes" if outside else "No"))
    result.facts.append(("Hidden from the address book", "Yes" if dl.get("HiddenFromAddressListsEnabled") else "No"))
    result.facts.append(("Messages are moderated", "Yes" if dl.get("ModerationEnabled") else "No"))
    joins = {"Open": "Anyone can join", "Closed": "Owners add members", "ApprovalRequired": "Owners approve requests"}
    result.facts.append(
        (
            "Joining",
            joins.get(
                _text(dl.get("MemberJoinRestriction")), _text(dl.get("MemberJoinRestriction")) or "Owners add members"
            ),
        )
    )
    result.facts.append(
        (
            "Leaving",
            joins.get(
                _text(dl.get("MemberDepartRestriction")),
                _text(dl.get("MemberDepartRestriction")) or "Owners remove members",
            )
            .replace("join", "leave")
            .replace("add", "remove"),
        )
    )
    count = m.member_count or 0
    if outside and count >= BIG_OPEN_LIST:
        m.flag(
            "low",
            f"Anyone outside can mail all {count} members: spam or a phishing run reaches them all at once.",
            "mail",
        )
        result.tone = "warn"
    activity, status = _activity(m.snap)
    entry = activity.get(_lower(m.mail or dl.get("PrimarySmtpAddress")))
    if status and status.get("available") and isinstance(entry, dict):
        watched_from = parse_ts(entry.get("watchedFrom"))
        watched = (m.now - watched_from).days if watched_from else 0
        last = parse_ts(entry.get("last"))
        recent = _recent(entry, m.now, MAIL_DAYS)
        result.facts.append((f"Mail in the last {MAIL_DAYS} days", f"{recent:,}"))
        result.facts.append(("Last mail received", _when(last, m.now) if last else "None seen"))
        result.facts.append(("Watched since", _day(watched_from) if watched_from else ""))
        result.summary = f"{recent:,} in {MAIL_DAYS} days"
        span = min(watched, MAIL_UNUSED_DAYS)
        if watched >= 30 and not _recent(entry, m.now, span):
            m.flag("low", f"No mail in {span} days. If nobody uses the list, delete it.", "mail")
            result.tone = result.tone or "warn"
        # Mail by week, from the per-day counts.
        days = entry.get("days") if isinstance(entry.get("days"), dict) else {}
        if days:
            table = Section("Mail by week", ["Week starting", "Messages"], compare=False)
            weeks: dict[str, int] = {}
            for day, n in days.items():
                when = parse_ts(day + "T00:00:00Z")
                if when and isinstance(n, int):
                    start = (when - timedelta(days=when.weekday())).date().isoformat()
                    weeks[start] = weeks.get(start, 0) + n
            for start in sorted(weeks, reverse=True)[:13]:
                table.add([start, weeks[start]])
            result.tables.append(table)
    else:
        result.summary = result.summary or "Settings"
        result.note = "Mail traffic shows once the message trace has been read (needs Global Reader)."
    return result


def _team_site(m: Group) -> dict | None:
    """The team site of a Microsoft 365 group, matched by the group's address or its site address."""
    if not m.m365 or not m.mail:
        return None
    return next((s for s in m.snap.objects("sharepoint_usage", "site") or [] if matches_group(s, m.mail)), None)


@part(KIND, 50)
def files_part(m: Group) -> Part | None:
    """The team site: storage, files, sharing."""
    if not m.m365:
        return None
    sites = m.snap.objects("sharepoint_usage", "site")
    result = Part("files", "Files")
    if sites is None:
        result.unavailable = "SharePoint usage hasn't been collected yet. Refresh SharePoint on the client's page."
        return result
    site = _team_site(m)
    if site is None:
        result.summary, result.empty = (
            "No site found",
            "No team site was matched to this group in the SharePoint usage report.",
        )
        return result
    url = _text(site.get("url"))
    storage = next(
        (
            r
            for r in m.snap.objects("sharepoint_storage", "site_storage") or []
            if _lower(r.get("url")).rstrip("/") == url.lower().rstrip("/")
        ),
        None,
    )
    result.facts.append(("Site", Link(url, url, page_href=site_path(m.tenant_id, site_key(site)))))
    used = (storage or {}).get("used") if storage else site.get("storageUsed")
    if isinstance(used, int):
        limit = (storage or {}).get("limit")
        result.facts.append(
            (
                "Storage",
                _size(used)
                + (f" of {_size(limit)} ({100 * used / limit:.0f}%)" if isinstance(limit, int) and limit else ""),
            )
        )
        result.summary = _size(used)
    if isinstance(site.get("files"), int):
        result.facts.append(("Files", f"{site['files']:,}"))
    if site.get("lastActivityDate"):
        result.facts.append(("Last activity", _day(site["lastActivityDate"])))
    sharing = []
    for key, label in (("anyoneLinks", "anyone"), ("guestLinks", "guests"), ("companyLinks", "the whole organisation")):
        n = site.get(key)
        if isinstance(n, int) and n:
            sharing.append(f"{n:,} with {label}")
    result.facts.append(("Sharing links", ", ".join(sharing) if sharing else "None"))
    result.facts.append(("External sharing", "On" if site.get("externalSharing") else "Off"))
    if isinstance(site.get("anyoneLinks"), int) and site["anyoneLinks"]:
        result.tone = "warn"
    result.note = "Who can open the site and its files is in the SharePoint reports, linked under In the reports."
    return result


@part(KIND, 60)
def structure_part(m: Group) -> Part:
    result = Part("structure", "How it's set up")
    g, n = m.g, m.nesting or {}
    result.facts.append(("Membership", m.membership))
    rule = _text(n.get("rule"))
    if rule:
        result.facts.append(("Rule", rule))
        state = _text(n.get("ruleState"))
        processing = PROCESSING.get(state, state) if state else ""
        if processing:
            result.facts.append(("Rule processing", processing))
        if processing == "Paused":
            result.tone = "bad"
            m.flag(
                "medium",
                "The dynamic membership rule is paused: joiners aren't added and leavers aren't removed.",
                "structure",
            )
    if m.created:
        result.facts.append(("Created", _day(m.created)))
    expires = _text(g.get("expirationDateTime"))
    if expires:
        when = parse_ts(expires)
        result.facts.append(("Expires", _day(expires) + " unless renewed"))
        if when and when <= m.now + timedelta(days=EXPIRY_SOON_DAYS):
            result.tone = result.tone or "warn"
            m.flag(
                "medium" if when > m.now else "high",
                f"Expires on {_day(expires)} unless an owner renews it; its files and conversations go with it.",
                "structure",
            )
    if n.get("roleAssignable"):
        result.facts.append(("Can be assigned admin roles", "Yes"))
    if m.m365:
        result.facts.append(
            ("Visibility", VISIBILITY.get(g.get("visibility"), _text(g.get("visibility")) or "Unknown"))
        )
        result.facts.append(("Team", "Yes" if g.get("isTeam") else "No"))
    parents = [x for x in m.snap.objects("group_nesting", "group") or [] if m.id in (x.get("groupIds") or [])]
    if parents:
        table = Section("Inside of", ["Group", "Kind"], intro="Members of this group also get what these give.")
        for p in sorted(parents, key=lambda x: _lower(x.get("displayName"))):
            table.add(
                [PageLink(_text(p.get("displayName")) or p["_id"], group_path(m.tenant_id, p["_id"])), group_type(p)]
            )
        result.tables.append(table)
    count = m.member_count
    created = parse_ts(m.created)
    if count == 0 and (created is None or created < m.now - timedelta(days=NEW_DAYS)):
        m.flag("low", "Nobody is a member and it's older than a month: delete it if nobody needs it.", "members")
    result.summary = m.membership if not rule else "Dynamic"
    return result


@part(KIND, 70)
def admin_changes_part(m: Group) -> Part | None:
    events = m.snap.objects("admin_activity", "admin_event")
    if events is None:
        return None
    result = Part("admin", "Admin changes")
    cutoff = (m.now - timedelta(days=ADMIN_CHANGE_DAYS)).strftime("%Y-%m-%d")
    mine = [
        e for e in events if any(m.named(t) for t in e.get("targets") or []) and _text(e.get("time"))[:10] >= cutoff
    ]
    mine.sort(key=lambda e: _text(e.get("time")), reverse=True)
    if not mine:
        result.summary = "None"
        result.empty = (
            f"No admin role, Conditional Access, app or account change named this group in the last "
            f"{ADMIN_CHANGE_DAYS} days. Changes to its members and owners are under History."
        )
        return result
    table = Section("Admin changes", ["When", "What", "By", "Detail"], compare=False)
    for e in mine[:ADMIN_CHANGE_LIMIT]:
        table.add(
            [
                _text(e.get("time"))[:16].replace("T", " "),
                _text(e.get("activity")),
                _text(e.get("actor")),
                _text(e.get("detail")),
            ]
        )
    result.tables.append(table)
    result.summary = plural(len(mine), "change")
    result.note = (
        f"Admin role, Conditional Access, app and account changes from the directory audit log that named this group, "
        f"the last {ADMIN_CHANGE_DAYS} days, newest first. Changes to its members and owners are under History."
    )
    return result


# -- History, side column, reports --------------------------------------------------------


def _history(m: Group) -> tuple[list[Event], str | None]:
    """What changed for the group between stored snapshots, newest first, and the oldest snapshot it's in."""
    conn, tid = m.snap.conn, m.tenant_id
    events: list[Event] = []
    copies = store.object_copies(conn, tid, "groups", "group", m.id)
    since = copies[0][0] if copies else None
    for (_, old), (when, new) in zip(copies, copies[1:], strict=False):
        day = when[:10]
        if old.get("displayName") and new.get("displayName") and old["displayName"] != new["displayName"]:
            events.append(Event(day, "Name", f"Renamed from {old['displayName']} to {new['displayName']}"))
        if not old.get("isTeam") and new.get("isTeam"):
            events.append(Event(day, "Type", "Became a team"))
        if old.get("visibility") != new.get("visibility") and new.get("visibility"):
            events.append(
                Event(
                    day,
                    "Settings",
                    f"Visibility changed to {VISIBILITY.get(new['visibility'], new['visibility'])}",
                    "warn" if new["visibility"] == "Public" else None,
                )
            )
        if old.get("examined") and new.get("examined"):
            before = {_lower(o.get("upn") or o.get("name")) for o in old.get("owners") or []}
            after = {_lower(o.get("upn") or o.get("name")) for o in new.get("owners") or []}
            events += [Event(day, "Owners", f"{w} added as an owner") for w in sorted(after - before)]
            events += [Event(day, "Owners", f"{w} no longer an owner", "warn") for w in sorted(before - after)]
            if old.get("enabledOwnerCount") and not new.get("enabledOwnerCount"):
                events.append(Event(day, "Owners", "No active owner left", "bad"))
            before_g = {_lower(x) for x in old.get("guests") or []}
            after_g = {_lower(x) for x in new.get("guests") or []}
            events += [Event(day, "Guests", f"{w} added as a guest", "warn") for w in sorted(after_g - before_g)]
            events += [Event(day, "Guests", f"{w} removed") for w in sorted(before_g - after_g)]
            a, b = old.get("memberCount"), new.get("memberCount")
            if isinstance(a, int) and isinstance(b, int) and a != b:
                events.append(Event(day, "Members", f"{a:,} → {b:,} members"))
        before_l = set(old.get("licenceSkuIds") or [])
        after_l = set(new.get("licenceSkuIds") or [])
        if before_l != after_l:
            events.append(Event(day, "Licences", "Licences given to members changed"))
    nests = store.object_copies(conn, tid, "group_nesting", "group", m.id)
    since = min(x for x in (since, nests[0][0] if nests else None) if x) if (since or nests) else None
    names = {x["_id"]: _text(x.get("displayName")) or x["_id"] for x in groups(m.snap) or []}
    for (_, old), (when, new) in zip(nests, nests[1:], strict=False):
        day = when[:10]
        if old.get("examined") and new.get("examined"):
            a, b = old.get("memberCount"), new.get("memberCount")
            if isinstance(a, int) and isinstance(b, int) and a != b:
                events.append(Event(day, "Members", f"{a:,} → {b:,} members"))
            before = set(old.get("groupIds") or [])
            after = set(new.get("groupIds") or [])
            events += [
                Event(day, "Nesting", f"{names.get(x, x)} added inside it", "warn") for x in sorted(after - before)
            ]
            events += [
                Event(day, "Nesting", f"{names.get(x, x)} removed from inside it") for x in sorted(before - after)
            ]
        if old.get("ruleState") != new.get("ruleState") and new.get("ruleState"):
            state = PROCESSING.get(new["ruleState"], new["ruleState"])
            events.append(Event(day, "Rule", f"Processing {state.lower()}", "bad" if state == "Paused" else None))
    lists = store.object_copies(conn, tid, "distribution_lists", "distribution_list", m.id)
    for (_, old), (when, new) in zip(lists, lists[1:], strict=False):
        day = when[:10]
        if old.get("membersRead") and new.get("membersRead"):
            before = {
                _lower(x.get("address") or x.get("name")) for x in old.get("members") or [] if isinstance(x, dict)
            }
            after = {_lower(x.get("address") or x.get("name")) for x in new.get("members") or [] if isinstance(x, dict)}
            events += [Event(day, "Members", f"{w} added") for w in sorted(after - before)]
            events += [Event(day, "Members", f"{w} removed") for w in sorted(before - after)]
        before_o = {_lower(x) for x in old.get("ManagedBy") or [] if isinstance(x, str)}
        after_o = {_lower(x) for x in new.get("ManagedBy") or [] if isinstance(x, str)}
        events += [Event(day, "Owners", f"{w} added as an owner") for w in sorted(after_o - before_o)]
        events += [Event(day, "Owners", f"{w} no longer an owner", "warn") for w in sorted(before_o - after_o)]
        if old.get("RequireSenderAuthenticationEnabled") != new.get("RequireSenderAuthenticationEnabled"):
            opened = new.get("RequireSenderAuthenticationEnabled") is False
            events.append(
                Event(
                    day,
                    "Email",
                    "Now accepts mail from outside" if opened else "No longer accepts mail from outside",
                    "warn" if opened else None,
                )
            )
    events.sort(key=lambda e: e.day, reverse=True)
    return events[:HISTORY_LIMIT], since


def _about(m: Group) -> list[tuple[str, object]]:
    facts: list[tuple[str, object]] = [("Type", m.type)]
    if m.mail:
        facts.append(("Email", m.mail))
    if m.m365:
        facts.append(("Visibility", VISIBILITY.get(m.g.get("visibility"), _text(m.g.get("visibility")) or "Unknown")))
    facts.append(("Membership", m.membership))
    count = m.member_count
    facts.append(("Members", f"{count:,}" if isinstance(count, int) else "Not read"))
    if m.created:
        facts.append(("Created", _day(m.created)))
    if _text(m.g.get("expirationDateTime")):
        facts.append(("Expires", _day(_text(m.g["expirationDateTime"]))))
    if _text(m.g.get("description")):
        facts.append(("Description", m.g["description"]))
    return facts


def _reports(m: Group) -> list[tuple[str, str]]:
    links = []
    if m.m365:
        links.append(("Groups and Teams", m.report("groups", "Teams and groups", "Name", m.name)))
        links.append(("Group membership", m.report("group-membership", "By team or group", "Name", m.name)))
    else:
        links.append(("Groups and Teams", m.report("groups")))
    if m.snap.has("group_nesting"):
        links.append(("Empty, nested and dynamic groups", m.report("group-structure", "Every group", "Group", m.name)))
    if m.dl is not None:
        links.append(("Distribution lists", m.report("distribution-lists", "Every list", "List", m.name)))
    if any(
        m.id in (_policy_users(p).get(k) or [])
        for p in m.snap.objects("conditional_access", "ca_policy") or []
        for k in ("includeGroups", "excludeGroups")
    ):
        links.append(("Conditional Access", m.report("conditional-access")))
    if any(
        r.get("principalType") == "group" and r.get("principalId") == m.id
        for r in m.snap.objects("admin_roles", "role_assignment") or []
    ):
        links.append(("Admin roles", m.report("admins")))
    site = _team_site(m)
    if site is not None and site.get("url"):
        links.append(("SharePoint sites", m.report("sharepoint", "All sites", "Site", site["url"])))
        if m.snap.has("sharepoint_storage"):
            links.append(
                ("SharePoint storage", m.report("sharepoint-storage", "Sites by storage", "Site", site["url"]))
            )
    if m.snap.has("admin_activity"):
        links.append(("Admin activity", m.report("admin-activity")))
    return links


def build(conn, tenant, gid: str, now: datetime | None = None) -> ObjectPage | None:
    """The page for one group, or None when the latest Groups and Teams snapshot doesn't have it."""
    now = now or datetime.now(UTC)
    snap = TenantSnapshot(conn, tenant)
    g = find_group(snap, gid)
    if g is None:
        return None
    m = Group(snap, g, now)
    tid = tenant["id"]
    page = ObjectPage(
        KIND,
        dict(tenant),
        gid,
        now,
        m.name,
        m.path,
        "Groups and teams",
        f"/tenants/{tid}/{URL}",
        subtitle=m.mail or "",
        description=f"Everything collected about the {m.type.lower()} {m.name}.",
        about_title="Group",
        nothing="Nothing stands out for this group in the data collected.",
    )
    page.parts = build_parts(m)
    page.about = _about(m)
    page.attention = sort_attention(m.attention)
    page.findings = findings_for(snap, {m.id}, {m.name.lower()})
    page.history, page.history_since = _history(m)
    page.reports = _reports(m)
    page.sources = sources_for(snap, SOURCES)

    page.badges = [(m.type, None)]
    if m.m365 and g.get("visibility"):
        page.badges.append((VISIBILITY.get(g["visibility"], g["visibility"]), None))
    if m.membership != "Assigned":
        page.badges.append((m.membership, None))
    if (m.nesting or {}).get("roleAssignable"):
        page.badges.append(("Role-assignable", "warn"))
    if m.m365 and m.examined and group_owner_problem(g):
        page.badges.append(("No active owner", "bad"))

    parts = {p.key: p for p in page.parts}
    page.tiles = [attention_tile(page.attention)]
    owners = parts.get("owners")
    if owners is not None:
        page.tiles.append(Highlight("Owners", owners.summary, "", owners.tone))
    members = parts.get("members")
    if members is not None:
        page.tiles.append(Highlight("Members", members.summary, "", members.tone))
    access = parts.get("access")
    if access is not None:
        page.tiles.append(Highlight("Gives members", access.summary, "", access.tone))
    mail = parts.get("mail")
    if mail is not None and m.dl is not None:
        page.tiles.append(Highlight("Email", mail.summary, "", mail.tone))
    files = parts.get("files")
    if files is not None and files.facts:
        page.tiles.append(Highlight("Files", files.summary, "Team site", files.tone))
    return page


# -- The Groups and teams page ------------------------------------------------------------


@dataclass
class GroupRow:
    id: str
    name: str
    type: str
    mail: str
    owners: str  # "2", "1 of 2 active", "None", "Not checked", ""
    owners_ok: bool
    members: int | None
    guests: int
    membership: str
    created: str
    findings: int


def directory(conn, tenant, now: datetime | None = None) -> tuple[list[GroupRow], list[Highlight]] | None:
    """Every group with the few facts that tell you where to look. None before Groups and Teams is collected."""
    now = now or datetime.now(UTC)
    snap = TenantSnapshot(conn, tenant)
    rows = groups(snap)
    if rows is None:
        return None
    nesting = {n["_id"]: n for n in snap.objects("group_nesting", "group") or [] if isinstance(n.get("_id"), str)}
    lists = {
        d["_id"]: d
        for d in snap.objects("distribution_lists", "distribution_list") or []
        if isinstance(d.get("_id"), str)
    }
    names = {_lower(g.get("displayName")): g["_id"] for g in rows if _text(g.get("displayName"))}
    counts: dict[str, int] = {}
    for r in conn.execute(
        "SELECT object_id, subject FROM findings WHERE tenant_id = ? AND resolved_at IS NULL", (tenant["id"],)
    ):
        key = r["object_id"] if r["object_id"] in names.values() else names.get(_lower(r["subject"]))
        if key:
            counts[key] = counts.get(key, 0) + 1

    out = []
    ownerless = with_guests = empty = dynamic = teams = m365 = 0
    for g in rows:
        m365_group = g.get("kind") == "microsoft365"
        n, dl = nesting.get(g["_id"]) or {}, lists.get(g["_id"])
        members = guests = None
        owners, ok = "", True
        if m365_group:
            m365 += 1
            teams += bool(g.get("isTeam"))
            if g.get("examined"):
                members, guests = g.get("memberCount") or 0, g.get("guestCount") or 0
                total, enabled = g.get("ownerCount") or 0, g.get("enabledOwnerCount") or 0
                owners = "None" if not total else (str(total) if enabled == total else f"{enabled} of {total} active")
                ok = bool(enabled)
            else:
                owners = "Not checked"
        elif dl is not None:
            if dl.get("membersRead") and isinstance(dl.get("memberCount"), int):
                members = dl["memberCount"]
            total = len([o for o in dl.get("ManagedBy") or [] if isinstance(o, str)])
            owners, ok = (str(total) if total else "None"), bool(total)
        if members is None and n.get("examined") and isinstance(n.get("memberCount"), int):
            members = n["memberCount"]
        ownerless += not ok
        with_guests += bool(guests)
        empty += members == 0
        is_dynamic = bool(g.get("dynamic") or n.get("dynamic"))
        dynamic += is_dynamic
        synced = g.get("synced") or n.get("synced") or (dl or {}).get("IsDirSynced")
        out.append(
            GroupRow(
                g["_id"],
                _text(g.get("displayName")) or g["_id"],
                group_type(g),
                _text(g.get("mail")) or _text((dl or {}).get("PrimarySmtpAddress")),
                owners,
                ok,
                members,
                guests or 0,
                "Dynamic" if is_dynamic else ("Synced" if synced else "Assigned"),
                (_text(g.get("createdDateTime")) or _text((dl or {}).get("WhenCreatedUTC")))[:10],
                counts.get(g["_id"], 0),
            )
        )
    out.sort(key=lambda r: (-r.findings, r.owners_ok, r.type != "Team", r.name.lower()))
    other = len(rows) - m365
    tiles = [
        Highlight(
            "Groups",
            len(rows),
            f"{teams} {'team' if teams == 1 else 'teams'}, {m365 - teams} Microsoft 365, {other} other",
        ),
        Highlight(
            "Without an active owner",
            ownerless,
            "Nobody approves members or renews them",
            "warn" if ownerless else "ok",
        ),
        Highlight("With guests", with_guests, "People from outside are members", "warn" if with_guests else None),
        Highlight("Empty", empty, "No members at all", "warn" if empty else None),
        Highlight("Dynamic", dynamic, "Members set by a rule"),
    ]
    return out, tiles
