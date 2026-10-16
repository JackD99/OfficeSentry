"""Empty, nested and dynamic groups: the group clean-up report.

For a technician tidying a directory: groups with no members (left over, or a dynamic rule that matches
nobody), groups inside other groups (who really has access is hidden a level or more down), dynamic
groups with their rules and whether Microsoft is still processing them, teams and groups about to expire,
role-assignable groups (whose members hold admin roles) and blocked accounts still in groups. Built from
what Group nesting stores for security groups and distribution lists, what Groups and Teams stores for
teams and Microsoft 365 groups, and the Users list for who is blocked. Nothing here calls Microsoft.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from ..collectors.group_nesting import GROUP_LIMIT
from ..devices import parse_ts
from ..snapshot import TenantSnapshot
from ..text import plural
from .data_reports import DataReport, Rows, Section, Tile, _date
from .registry import report

EMPTY = "Empty groups"
NESTED = "Groups inside groups"
DYNAMIC = "Dynamic groups"
EXPIRING = "Expiring soon"
ROLE = "Role-assignable groups"
BLOCKED = "Blocked members"
EVERY = "Every group"
EXPIRY_DAYS = 90  # teams and groups expiring within this many days are listed
SOON_DAYS = 30  # and flagged within this many
DEEP = 3  # levels of groups below a group that make it hard to see who is in it
NEW_DAYS = 30  # an empty group this new may still be being set up
KINDS = {
    "security": "Security group",
    "mail_security": "Mail-enabled security group",
    "distribution": "Distribution list",
}
PROCESSING = {"On": "Running", "Paused": "Paused"}

NOT_COLLECTED = "Group nesting hasn't been collected yet. Refresh Group nesting on the client page."


def _kind(g: dict) -> str:
    if g.get("kind") == "microsoft365":
        return "Team" if g.get("isTeam") else "Microsoft 365 group"
    return KINDS.get(g.get("kind"), "Group")


def _membership(g: dict) -> str:
    return "Dynamic" if g.get("dynamic") else ("Synced from AD" if g.get("synced") else "Assigned")


@report(
    "group-structure",
    "Empty, nested and dynamic groups",
    "Groups with no members, groups inside other groups, dynamic groups with their rules and whether "
    "they're paused, teams and groups about to expire, role-assignable groups, and blocked accounts still "
    "in groups.",
    ("group_nesting", "groups", "users"),
    group="files",
    order=70,
    keywords="empty groups nested groups dynamic membership rule paused distribution lists security groups "
    "clean up cleanup expiring expiry expiration renew role-assignable privileged blocked disabled leavers",
)
def build(snap: TenantSnapshot, now: datetime, report: DataReport) -> None:
    groups = snap.objects("group_nesting", "group")
    if groups is None:
        report.unavailable = NOT_COLLECTED
        return
    teams = snap.objects("groups", "group")
    if teams is None:
        report.notes.append("Member counts of teams and Microsoft 365 groups show after Groups and Teams is collected.")
    people = snap.objects("users", "user")
    if people is None:
        report.notes.append("Blocked accounts still in groups show after Users is collected.")
    m365 = {g["_id"]: g for g in teams or [] if isinstance(g.get("_id"), str)}
    by_id = {g["_id"]: g for g in groups if isinstance(g.get("_id"), str)}
    accounts = {u["_id"]: u for u in people or [] if isinstance(u.get("_id"), str)}
    blocked_ids = {uid for uid, u in accounts.items() if u.get("accountEnabled") is False}
    guest_ids = {uid for uid, u in accounts.items() if u.get("userType") == "Guest"}

    def person(uid: str) -> str:
        u = accounts.get(uid) or {}
        return u.get("displayName") or u.get("userPrincipalName") or uid

    def member_ids(gid: str) -> list[str]:
        g = by_id.get(gid) or {}
        source = m365.get(gid) if g.get("kind") == "microsoft365" else g
        ids = (source or {}).get("memberIds" if g.get("kind") == "microsoft365" else "userIds")
        return [i for i in ids if isinstance(i, str)] if isinstance(ids, list) else []

    def owner_names(gid: str) -> list[str] | str:
        # A security group's owners are read only when it can be given admin roles (since 2.4.0).
        owners = next(
            (
                o
                for o in ((m365.get(gid) or {}).get("owners"), (by_id.get(gid) or {}).get("owners"))
                if isinstance(o, list)
            ),
            None,
        )
        if owners is None:
            return "Not read"
        return [o.get("name") or o.get("upn") or "?" for o in owners if isinstance(o, dict)] or "None"

    def members(gid: str) -> int | None:
        g = by_id.get(gid) or {}
        source = m365.get(gid) if g.get("kind") == "microsoft365" else g
        count = (source or {}).get("memberCount")
        return count if (source or {}).get("examined") and isinstance(count, int) else None

    def children(gid: str) -> list[str]:
        return [c for c in (by_id.get(gid) or {}).get("groupIds") or [] if isinstance(c, str)]

    parents: dict[str, list[str]] = {}
    for gid in by_id:
        for child in children(gid):
            parents.setdefault(child, []).append(gid)

    depths: dict[str, int] = {}

    def depth(gid: str, path: frozenset = frozenset()) -> tuple[int, bool]:
        """Levels of groups below this one (a group inside a group inside it is 2), and whether a loop was
        cut short on the way: each group in a loop is counted once, so those depths depend on where you start."""
        if gid in depths:
            return depths[gid], False
        best, looped = 0, False
        for child in children(gid):
            if child == gid or child in path:
                looped = True
                continue
            below, loop = depth(child, path | {gid})
            best, looped = max(best, below), looped or loop
        found = 1 + best if children(gid) else 0
        if not looped:
            depths[gid] = found
        return found, looped

    def name(gid: str) -> str:
        return (by_id.get(gid) or {}).get("displayName") or gid

    order = sorted(by_id, key=lambda gid: str(name(gid)).lower())
    others = [g for g in by_id.values() if g.get("kind") != "microsoft365"]
    unread = sum(1 for g in others if not g.get("examined"))
    if unread and len(others) > GROUP_LIMIT:
        report.notes.append(
            f"Members were read for the first {GROUP_LIMIT:,} security groups and distribution "
            f"lists, not the other {unread:,}."
        )
    elif unread:
        report.notes.append(
            f"Members of {plural(unread, 'group')} couldn't be read. The collection log on the client page says why."
        )

    empty = Section(
        EMPTY,
        ["Group", "Kind", "Membership", "Email", "Created"],
        intro="Groups with no members at all. Delete the ones nobody needs; a dynamic group here has a "
        f"rule that matches nobody. Empty groups older than {NEW_DAYS} days are flagged.",
        empty="Every group has members.",
    )
    nested = Section(
        NESTED,
        ["Group", "Kind", "Groups inside", "Inside of", "Levels below", "Members"],
        intro="Groups with other groups as members. Everyone in the inner groups gets what the outer "
        f"group gives, which is easy to miss; {DEEP} or more levels are flagged.",
        empty="No group has another group as a member.",
    )
    dynamic = Section(
        DYNAMIC,
        ["Group", "Kind", "Rule", "Processing", "Members"],
        intro="Groups whose members Entra ID works out from a rule (needs Entra ID P1). A paused rule "
        "stops adding joiners and removing leavers, so it's flagged.",
        empty="No dynamic groups.",
    )
    expiring = Section(
        EXPIRING,
        ["Group", "Kind", "Expires", "Days left", "Owners", "Members"],
        intro=f"Teams and Microsoft 365 groups the expiration policy will delete within {EXPIRY_DAYS} days. One "
        "that's used renews itself; otherwise its owners are emailed to renew it, so a group with no owner who "
        f"can sign in is flagged, as is one due within {SOON_DAYS} days. A deleted group can be restored for "
        "30 days.",
        empty=f"No team or group expires in the next {EXPIRY_DAYS} days.",
    )
    role = Section(
        ROLE,
        ["Group", "Kind", "Members", "Guests", "Owners", "Created"],
        intro="Groups that can be given admin roles, so being a member of one is being an admin. Their owners "
        "can add anyone; a guest in one is flagged.",
        empty="No group can be assigned admin roles.",
    )
    blocked = Section(
        BLOCKED,
        ["Group", "Kind", "Blocked accounts", "Blocked", "Members"],
        intro="Groups that still hold accounts whose sign-in is blocked, usually leavers. Access the group "
        "gives is handed back the moment the account is unblocked, and the member counts others see are "
        "wrong; blocked accounts in a role-assignable group are flagged higher.",
        empty="No blocked account is in a group.",
    )
    every = Section(
        EVERY,
        ["Group", "Kind", "Membership", "Members", "Groups inside", "Inside of", "Created", "Expires"],
        intro="Every group in the tenant, with its direct members (people, devices, apps and groups).",
        empty="The tenant has no groups.",
    )
    nobody_to_renew = 0
    blocked_total = 0
    for gid in order:
        g = by_id[gid]
        kind, count = _kind(g), members(gid)
        inner, outer = [name(c) for c in children(gid)], sorted(name(p) for p in parents.get(gid, []))
        inner_count = g.get("groupCount") if isinstance(g.get("groupCount"), int) else len(inner)
        shown = "" if count is None else (f"{count:,}+" if g.get("membersComplete") is False else count)
        created = parse_ts(g.get("createdDateTime"))
        if count == 0:
            old = created is None or created < now - timedelta(days=NEW_DAYS)
            empty.add(
                [name(gid), kind, _membership(g), g.get("mail"), _date(g.get("createdDateTime"))],
                "low" if old else None,
            )
        if inner:
            levels = depth(gid)[0]
            nested.add(
                [
                    name(gid),
                    kind,
                    sorted(inner) + ([f"and {inner_count - len(inner)} more"] if inner_count > len(inner) else []),
                    outer,
                    levels,
                    shown,
                ],
                "low" if levels >= DEEP else None,
            )
        if g.get("dynamic"):
            state = g.get("ruleState")
            processing = PROCESSING.get(state, state) if isinstance(state, str) and state else ""
            dynamic.add(
                [name(gid), kind, g.get("rule"), processing, shown], "medium" if processing == "Paused" else None
            )
        m = m365.get(gid) or {}
        expires = parse_ts(m.get("expirationDateTime"))
        if expires is not None and (days := (expires.date() - now.date()).days) <= EXPIRY_DAYS:
            renewers = m.get("enabledOwnerCount")
            nobody = m.get("examined") is True and isinstance(renewers, int) and renewers == 0
            nobody_to_renew += nobody
            expiring.add(
                [name(gid), kind, _date(m.get("expirationDateTime")), days, owner_names(gid), shown],
                "medium" if nobody or days < 0 else ("low" if days <= SOON_DAYS else None),
            )
        ids = member_ids(gid)
        if g.get("roleAssignable"):
            guests = sum(1 for i in ids if i in guest_ids)
            role.add(
                [name(gid), kind, shown, guests, owner_names(gid), _date(g.get("createdDateTime"))],
                "medium" if guests else None,
            )
        gone = sorted((person(i) for i in ids if i in blocked_ids), key=str.lower)
        if gone:
            blocked_total += len(gone)
            blocked.add([name(gid), kind, gone, len(gone), shown], "medium" if g.get("roleAssignable") else "low")
        every.add(
            [
                name(gid),
                kind,
                _membership(g),
                shown,
                inner_count or "",
                outer,
                _date(g.get("createdDateTime")),
                _date(m.get("expirationDateTime")),
            ]
        )
    report.sections = [empty, nested, dynamic, expiring, role, blocked, every]

    paused = sum(1 for r in dynamic.rows if r[3] == "Paused")
    deepest = max((r[4] for r in nested.rows if isinstance(r[4], int)), default=0)
    report.tiles = [
        Tile(
            EMPTY,
            len(empty.rows),
            f"{sum(1 for f in empty.flags if f)} older than {NEW_DAYS} days",
            "warn" if any(empty.flags) else "ok",
            Rows(EMPTY),
        ),
        Tile(
            NESTED,
            len(nested.rows),
            f"Deepest: {plural(deepest, 'level')}" if nested.rows else "None nested",
            "warn" if deepest >= DEEP else None,
            Rows(NESTED),
        ),
        Tile(
            "Paused dynamic rules",
            paused,
            "Not adding joiners or removing leavers",
            "bad" if paused else "ok",
            Rows(DYNAMIC, {"Processing": "Paused"}),
        ),
        Tile(
            EXPIRING,
            len(expiring.rows),
            f"{nobody_to_renew} with nobody to renew"
            if nobody_to_renew
            else ("Owners can renew them" if expiring.rows else f"Within {EXPIRY_DAYS} days"),
            "warn" if nobody_to_renew else None,
            Rows(EXPIRING),
        ),
        Tile(
            ROLE,
            len(role.rows),
            f"{sum(1 for f in role.flags if f)} with a guest" if any(role.flags) else "Members hold admin roles",
            "bad" if any(role.flags) else None,
            Rows(ROLE),
        ),
        Tile(
            BLOCKED,
            len(blocked.rows),
            f"{plural(blocked_total, 'blocked account')} still in groups" if blocked.rows else "None in any group",
            "warn" if blocked.rows else "ok",
            Rows(BLOCKED),
        ),
    ]
    report.footnote = (
        "Members are direct members. Teams and Microsoft 365 groups can't contain other groups. "
        "Delete or change groups in the Microsoft Entra admin center (Groups), or in on-premises AD "
        "for synced groups; Office Sentry only reads them."
    )
