"""Empty, nested and dynamic groups: the group clean-up report.

For a technician tidying a directory: groups with no members (left over, or a dynamic rule that matches
nobody), groups inside other groups (who really has access is hidden a level or more down), and dynamic
groups with their rules and whether Microsoft is still processing them. Built from what Group nesting stores
for security groups and distribution lists, and the member counts Groups and Teams stores for teams and
Microsoft 365 groups. Nothing here calls Microsoft.
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
EVERY = "Every group"
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
    "Groups with no members, groups inside other groups, and dynamic groups with their rules and "
    "whether they're paused.",
    ("group_nesting", "groups"),
    group="files",
    order=70,
    keywords="empty groups nested groups dynamic membership rule paused distribution lists security groups "
    "clean up cleanup",
)
def build(snap: TenantSnapshot, now: datetime, report: DataReport) -> None:
    groups = snap.objects("group_nesting", "group")
    if groups is None:
        report.unavailable = NOT_COLLECTED
        return
    teams = snap.objects("groups", "group")
    if teams is None:
        report.notes.append("Member counts of teams and Microsoft 365 groups show after Groups and Teams is collected.")
    m365 = {g["_id"]: g for g in teams or [] if isinstance(g.get("_id"), str)}
    by_id = {g["_id"]: g for g in groups if isinstance(g.get("_id"), str)}

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
    every = Section(
        EVERY,
        ["Group", "Kind", "Membership", "Members", "Groups inside", "Inside of", "Created"],
        intro="Every group in the tenant, with its direct members (people, devices, apps and groups).",
        empty="The tenant has no groups.",
    )
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
        every.add([name(gid), kind, _membership(g), shown, inner_count or "", outer, _date(g.get("createdDateTime"))])
    report.sections = [empty, nested, dynamic, every]

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
            DYNAMIC,
            len(dynamic.rows),
            f"{paused} paused" if dynamic.rows else "Members set by a rule",
            rows=Rows(DYNAMIC),
        ),
        Tile(
            "Paused dynamic rules",
            paused,
            "Not adding joiners or removing leavers",
            "bad" if paused else "ok",
            Rows(DYNAMIC, {"Processing": "Paused"}),
        ),
    ]
    report.footnote = (
        "Members are direct members. Teams and Microsoft 365 groups can't contain other groups. "
        "Delete or change groups in the Microsoft Entra admin center (Groups), or in on-premises AD "
        "for synced groups; Office Sentry only reads them."
    )
