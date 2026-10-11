"""Group membership: who is in which team and Microsoft 365 group.

Built from data the Groups and Teams and Users collectors already store: each
examined group's member object IDs (``memberIds``), owners and guest
addresses, matched to the tenant's accounts. Nothing here calls Microsoft.
"""

from __future__ import annotations

from datetime import datetime

from ..checks.groups import m365_groups
from ..collectors.groups import GROUP_LIMIT, MEMBER_IDS_KEPT
from ..snapshot import TenantSnapshot
from ..text import plural, verb
from .data_reports import VISIBILITY, DataReport, Rows, Section, Tile, _owner_names
from .registry import report

GUEST_MANY = 5  # a guest in this many teams and groups is flagged for review

BY_PERSON = "By person"
BY_GROUP = "By team or group"
EVERY = "Every membership"

SCOPE_NOTE = (
    "Only teams and Microsoft 365 groups are included. Security groups and distribution lists aren't, "
    "because Office Sentry doesn't read their members."
)


class _Person:
    def __init__(self, account: str, name: str, guest: bool, status: str):
        self.account, self.name, self.guest, self.status = account, name, guest, status
        self.groups: dict[str, str] = {}  # group id -> name

    @property
    def disabled(self) -> bool:
        return self.status == "Disabled"


def _sort_name(text: str) -> str:
    return (text or "").lower()


@report(
    "group-membership",
    "Group membership",
    "Who is in which team and Microsoft 365 group: each person's teams and groups, each "
    "group's members, and every membership in one table.",
    ("groups", "users"),
    group="files",
    order=60,
    keywords="members membership teams groups who is in",
)
def membership(snap: TenantSnapshot, now: datetime, report: DataReport) -> None:
    groups = m365_groups(snap)
    users = snap.objects("users", "user")
    if groups is None:
        report.unavailable = "Group data hasn't been collected yet. Refresh Groups and Teams on the client page."
        return
    if users is None:
        report.unavailable = "User data hasn't been collected yet. Refresh Users on the client page."
        return

    people: dict[str, _Person] = {}
    by_address: dict[str, str] = {}
    for u in users:
        account = u.get("userPrincipalName") or u.get("mail") or u["_id"]
        people[u["_id"]] = _Person(
            account,
            u.get("displayName") or "",
            u.get("userType") == "Guest",
            "Enabled" if u.get("accountEnabled") else "Disabled",
        )
        for key in ("mail", "userPrincipalName"):
            if isinstance(u.get(key), str) and u[key]:
                by_address.setdefault(u[key].lower(), u["_id"])

    groups = sorted(groups, key=lambda g: _sort_name(g.get("displayName")))
    checked = [g for g in groups if g.get("examined")]
    truncated = [g for g in checked if g.get("memberIdsComplete") is False]
    members_of: dict[str, list[str]] = {}  # group id -> person keys
    others: dict[str, int] = {}  # group id -> members that aren't accounts
    for g in checked:
        gid, name = g["_id"], g.get("displayName") or g["_id"]
        keys = [mid for mid in g.get("memberIds") or [] if mid in people]
        unmatched = len(g.get("memberIds") or []) - len(keys)
        # Guest addresses are stored separately: add any guest the ID list didn't reach (a truncated list),
        # or whose account isn't in the user list.
        for address in g.get("guests") or []:
            key = by_address.get(address.lower())
            if key is None:
                key = f"guest:{address.lower()}"
                people.setdefault(key, _Person(address, "", True, "Not in the user list"))
                if g.get("memberIdsComplete") is not False:
                    unmatched -= 1  # its object ID was among the unmatched ones
            if key not in keys:
                keys.append(key)
        # What's left isn't an account: a nested group, a device or a contact.
        others[gid] = max(unmatched, 0)
        for key in keys:
            people[key].groups[gid] = name
        members_of[gid] = keys

    # -- By person
    person = Section(
        BY_PERSON,
        ["Account", "Name", "Type", "Account status", "Teams and groups", "Member of"],
        intro="Every account and the teams and Microsoft 365 groups it's a member of. " + SCOPE_NOTE,
        empty="The tenant has no accounts.",
    )
    ordered = sorted(people.values(), key=lambda p: (_sort_name(p.name or p.account), _sort_name(p.account)))
    for p in ordered:
        count = len(p.groups)
        flag = None
        if count and p.disabled:
            flag = "low"  # a leaver who still has access when the account is re-enabled
        elif p.guest and count >= GUEST_MANY:
            flag = "low"
        person.add(
            [
                p.account,
                p.name,
                "Guest" if p.guest else "Member",
                p.status,
                count,
                sorted(p.groups.values(), key=_sort_name),
            ],
            flag,
        )

    # -- By team or group
    group = Section(
        BY_GROUP,
        ["Name", "Team", "Visibility", "Owners", "Members", "Member list", "Guests", "Note"],
        intro="Every team and Microsoft 365 group and the accounts in it.",
        empty="The tenant has no teams or Microsoft 365 groups.",
    )
    every = Section(
        EVERY,
        ["Account", "Name", "Type", "Account status", "Team or group", "Team", "Owner"],
        intro="One row per person per team or group, to filter and pivot in Excel.",
        empty="Nobody is a member of a team or Microsoft 365 group.",
        compare=False,
    )
    for g in groups:
        gid, name = g["_id"], g.get("displayName") or g["_id"]
        visibility = VISIBILITY.get(g.get("visibility"), g.get("visibility") or "")
        if not g.get("examined"):
            group.add(
                [
                    name,
                    g.get("isTeam"),
                    visibility,
                    _owner_names(g),
                    "Not checked",
                    [],
                    [],
                    f"Not checked: members are read for the first {GROUP_LIMIT} teams and groups only",
                ]
            )
            continue
        keys = members_of.get(gid, [])
        listed = sorted((people[k].account for k in keys), key=_sort_name)
        note = []
        if g.get("memberIdsComplete") is False:
            hidden = max((g.get("memberCount") or 0) - len(g.get("memberIds") or []), 0)
            listed.append(plural(hidden, "more member") + " not listed")
            note.append(f"Member list cut at the first {MEMBER_IDS_KEPT:,}")
        if others.get(gid, 0) > 0:
            listed.append(plural(others[gid], "other member") + " (groups, devices or contacts)")
        guests = list(g.get("guests") or [])
        if (g.get("guestCount") or 0) > len(guests):
            guests.append(plural(g["guestCount"] - len(guests), "more guest"))
        group.add(
            [
                name,
                g.get("isTeam"),
                visibility,
                _owner_names(g),
                g.get("memberCount") or 0,
                listed,
                guests,
                "; ".join(note),
            ]
        )
        owners = {(o.get("upn") or "").lower() for o in g.get("owners") or [] if o.get("upn")}
        for k in keys:
            p = people[k]
            every.add(
                [
                    p.account,
                    p.name,
                    "Guest" if p.guest else "Member",
                    p.status,
                    name,
                    g.get("isTeam"),
                    p.account.lower() in owners,
                ]
            )
    every_sorted = sorted(
        zip(every.rows, every.flags, strict=True),
        key=lambda rf: (_sort_name(rf[0][1] or rf[0][0]), _sort_name(rf[0][4])),
    )
    every.rows, every.flags = [r for r, _ in every_sorted], [f for _, f in every_sorted]

    # -- Headline figures
    in_some = [p for p in people.values() if p.groups]
    counts = sorted({str(len(p.groups)) for p in in_some}, key=int)
    guests_in = [p for p in in_some if p.guest]
    disabled_in = sum(p.disabled for p in in_some)
    report.tiles = [
        Tile(
            "Teams and groups checked",
            len(checked),
            f"of {plural(len(groups), 'team and Microsoft 365 group', 'teams and Microsoft 365 groups')}",
            "warn" if len(checked) < len(groups) else None,
            Rows(BY_GROUP),
        ),
        Tile(
            "People in at least one",
            len(in_some),
            plural(disabled_in, "disabled account") + " among them" if disabled_in else "",
            "warn" if disabled_in else None,
            Rows(BY_PERSON, {"Teams and groups": counts}) if counts else None,
        ),
        Tile(
            "People in none",
            len(people) - len(in_some),
            "Not a member of any team or Microsoft 365 group",
            rows=Rows(BY_PERSON, {"Teams and groups": "0"}),
        ),
        Tile(
            "Guests in teams",
            len(guests_in),
            f"{plural(sum(g.get('guestCount') or 0 for g in checked), 'guest membership')} in all",
            rows=Rows(BY_PERSON, {"Type": "Guest", "Teams and groups": counts}) if guests_in else None,
        ),
    ]

    report.notes.append(SCOPE_NOTE)
    if len(checked) < len(groups):
        report.notes.append(
            f"Members were read for {len(checked)} of {len(groups)} teams and Microsoft 365 groups "
            f"(the first {GROUP_LIMIT}, teams first). The rest show as Not checked, and their "
            "members aren't counted."
        )
    if truncated:
        report.notes.append(
            f"{plural(len(truncated), 'group')} with more than {MEMBER_IDS_KEPT:,} members "
            f"{verb(len(truncated), 'is', 'are')} listed only in part: "
            + ", ".join(g.get("displayName") or g["_id"] for g in truncated)
            + "."
        )
    report.sections = [person, group, every]
