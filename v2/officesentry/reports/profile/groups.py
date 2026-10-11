"""Groups and teams."""

from __future__ import annotations

from ...text import plural as _plural
from ..data_reports import Section
from .base import Part, Person, part

GROUP_KINDS = {
    "microsoft365": "Microsoft 365 group",
    "security": "Security group",
    "mail_security": "Mail-enabled security group",
    "distribution": "Distribution list",
}


@part(40)
def groups_part(p: Person) -> Part:
    result = Part("groups", "Teams and groups")
    groups = p.snap.objects("groups", "group")
    if groups is None:
        result.unavailable = "Groups haven't been collected yet. Refresh Groups and Teams on the client page."
        return result
    ca = {g["_id"]: g for g in p.snap.objects("conditional_access", "ca_group") or []}
    rows, owned, orphaned = [], 0, []
    old_snapshot = any(g.get("examined") and "memberIds" not in g for g in groups)
    for g in groups:
        role = None
        if any(p.named(o.get("upn")) for o in g.get("owners") or []):
            role = "Owner"
            owned += 1
        elif p.id in (g.get("memberIds") or []) or p.id in ((ca.get(g["_id"]) or {}).get("members") or []):
            role = "Member"
        elif any(p.named(x) for x in g.get("guests") or []):
            role = "Guest member"
        if role is None:
            continue
        kind = "Team" if g.get("isTeam") else GROUP_KINDS.get(g.get("kind"), "Group")
        notes = []
        if role == "Owner" and g.get("examined"):
            enabled_owners = g.get("enabledOwnerCount") or 0
            if not p.enabled and enabled_owners == 0:
                notes.append("No active owner left")
                orphaned.append(g.get("displayName") or "a group")
            elif p.enabled and enabled_owners == 1:
                notes.append("Only owner")
        if g.get("dynamic"):
            notes.append("Dynamic membership")
        if g.get("synced"):
            notes.append("Synced from AD")
        rows.append(
            (
                (role != "Owner", not g.get("isTeam"), (g.get("displayName") or "").lower()),
                [g.get("displayName") or "Unnamed group", kind, role, "; ".join(notes)],
                "medium" if "No active owner left" in notes else None,
            )
        )
    if orphaned:
        p.flag(
            "medium",
            f"Owned {', '.join(orphaned[:3])}{' and more' if len(orphaned) > 3 else ''} and sign-in is "
            "now blocked, so nobody active looks after "
            f"{'it' if len(orphaned) == 1 else 'them'}. Add a new owner.",
            "groups",
        )
    result.note = (
        "Members are read for teams, Microsoft 365 groups and groups used by Conditional Access. "
        "For other security groups and distribution lists only owners show."
    )
    if old_snapshot:
        result.note += " Team and group members show after the next Groups and Teams collection."
    if not rows:
        result.summary, result.empty = "None", "Not an owner or member of any team or group Office Sentry reads."
        return result
    table = Section("Teams and groups", ["Name", "Type", "Role", "Note"])
    for _, row, flag in sorted(rows, key=lambda r: r[0]):
        table.add(row, flag)
    result.tables = [table]
    result.summary = f"Owner of {owned}, member of {len(rows) - owned}" if owned else _plural(len(rows), "group")
    return result
