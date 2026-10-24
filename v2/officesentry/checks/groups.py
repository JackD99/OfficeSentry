"""Checks on teams and Microsoft 365 groups: owners, who can create them, expiry."""

from __future__ import annotations

from datetime import datetime

from ..snapshot import TenantSnapshot
from ..text import every, plural, verb
from .base import Finding, _unavailable, rule


def m365_groups(snap: TenantSnapshot) -> list[dict] | None:
    """Teams and Microsoft 365 groups (shared with the Groups and Teams report)."""
    rows = snap.objects("groups", "group")
    return None if rows is None else [g for g in rows if g.get("kind") == "microsoft365"]


def group_type(g: dict) -> str:
    return "Team" if g.get("isTeam") else "Microsoft 365 group"


def group_owner_problem(g: dict) -> str | None:
    """Why nobody looks after a team or group (no enabled user owner), or None."""
    if not g.get("examined") or g.get("enabledOwnerCount"):
        return None
    owners = g.get("owners") or []
    if not owners:
        return "No owner"
    people = [o.get("name") or "?" for o in owners if o.get("type") == "user"]
    apps = [o.get("name") or "?" for o in owners if o.get("type") != "user"]
    if not apps:
        return ("Only owner is disabled" if len(people) == 1 else "All owners are disabled") + f" ({', '.join(people)})"
    if not people:
        return f"Owned only by an app ({', '.join(apps)})"
    return f"No enabled owner ({', '.join([p + ': disabled' for p in people] + [a + ': app' for a in apps])})"


def owner_is_guest(owner: dict) -> bool:
    """A guest from another organisation. Snapshots from before the flag was stored go by the sign-in name."""
    if "guest" in owner:
        return owner.get("guest") is True
    return owner.get("type") == "user" and "#EXT#" in (owner.get("upn") or "")


def _group_object(snap: TenantSnapshot, kind: str, what: str):
    rows = snap.objects("groups", kind)
    if rows is None:
        return None, "Group data hasn't been collected yet"
    if not rows or not rows[0].get("available"):
        return None, f"{what} couldn't be read"
    return rows[0], ""


@rule(
    "ownerless_groups",
    "Every team and Microsoft 365 group has an owner",
    "Groups",
    "Make an active person, ideally two, an owner of each listed team or group (Teams admin center or Microsoft "
    "365 admin center > Teams & groups). Owners approve members and guests and renew the group; without one, "
    "nobody looks after its files and conversations. The ownerless group policy (Microsoft 365 admin center > "
    "Settings > Org settings > Microsoft 365 Groups) asks members to take over when the last owner leaves.",
)
def ownerless_groups(snap: TenantSnapshot, now: datetime):
    groups = m365_groups(snap)
    if groups is None:
        return _unavailable("Group data hasn't been collected yet")
    if not groups:
        return _unavailable("The tenant has no teams or Microsoft 365 groups")
    checked = [g for g in groups if g.get("examined")]
    if not checked:
        return _unavailable("Owners of teams and groups couldn't be read")
    findings = []
    for g in checked:
        if problem := group_owner_problem(g):
            findings.append(
                Finding(
                    "medium" if g.get("isTeam") else "low",
                    g.get("displayName") or g["_id"],
                    f"{group_type(g)}, {problem[0].lower()}{problem[1:]}",
                    g["_id"],
                )
            )
    scope = f" ({len(checked)} of {len(groups)} checked)" if len(checked) < len(groups) else ""
    return (
        ("fail", f"{plural(len(findings), 'team or group')} without an owner{scope}", findings)
        if findings
        else (
            "pass",
            f"{every(len(checked), 'team or group', start=True)} {verb(len(checked), 'has', 'have')} an owner{scope}",
            [],
        )
    )


@rule(
    "guest_owners",
    "No guest owns a team or group",
    "Guests",
    "Make a member of the organisation the owner of each listed team or group and remove the guest as owner "
    "(Teams admin center, or Microsoft 365 admin center > Teams & groups). An owner from outside can add "
    "members and guests, change settings and see everything in it, and nobody here is accountable for that.",
)
def guest_owners(snap: TenantSnapshot, now: datetime):
    groups = m365_groups(snap)
    if groups is None:
        return _unavailable("Group data hasn't been collected yet")
    if not groups:
        return _unavailable("The tenant has no teams or Microsoft 365 groups")
    checked = [g for g in groups if g.get("examined")]
    if not checked:
        return _unavailable("Owners of teams and groups couldn't be read")
    findings = []
    for g in checked:
        guests = [o.get("name") or o.get("upn") or "?" for o in g.get("owners") or [] if owner_is_guest(o)]
        if guests:
            findings.append(
                Finding(
                    "medium" if g.get("isTeam") else "low",
                    g.get("displayName") or g["_id"],
                    f"{group_type(g)}, owned by {', '.join(guests)} (guest)",
                    g["_id"],
                )
            )
    scope = f" ({len(checked)} of {len(groups)} checked)" if len(checked) < len(groups) else ""
    return (
        ("fail", f"{plural(len(findings), 'team or group')} with a guest as owner{scope}", findings)
        if findings
        else ("pass", f"No guest owns any of {plural(len(checked), 'team or group')}{scope}", [])
    )


@rule(
    "group_creation",
    "Only chosen people can create teams and groups",
    "Groups",
    "Limit who can create teams and Microsoft 365 groups to a security group of people who will look after them "
    "(the Group.Unified directory setting, changed with Microsoft Graph PowerShell; needs Entra ID P1). "
    "Otherwise anyone can create a team, add guests to it and leave it behind.",
    needs=("entra_p1",),
)
def group_creation(snap: TenantSnapshot, now: datetime):
    settings, reason = _group_object(snap, "group_settings", "Group settings")
    if settings is None:
        return _unavailable(reason)
    if settings.get("enableGroupCreation"):
        if snap.capabilities.get("entra_p1") is False:
            return _unavailable("Anyone can create teams and groups; limiting who can needs Entra ID P1")
        default = " (Microsoft's default)" if settings.get("source") == "default" else ""
        return (
            "fail",
            "Anyone can create teams and groups",
            [Finding("low", "Tenant", f"Every user can create teams and Microsoft 365 groups{default}")],
        )
    allowed = settings.get("allowedGroupName") or settings.get("allowedGroupId")
    return (
        "pass",
        f"Only admins and members of {allowed} can create teams and groups"
        if allowed
        else "Only admins can create teams and groups",
        [],
    )


@rule(
    "group_expiration",
    "Unused groups and teams expire",
    "Groups",
    "Set a Microsoft 365 group expiration policy for all groups (Entra admin center > Groups > Expiration), for "
    "example 365 days. Groups in use renew automatically, owners are asked to renew the rest, and an expired "
    "group can be restored for 30 days. Needs Entra ID P1.",
    needs=("entra_p1",),
)
def group_expiration(snap: TenantSnapshot, now: datetime):
    policy, reason = _group_object(snap, "expiration_policy", "The group expiration policy")
    groups = m365_groups(snap)
    if policy is None or groups is None:
        return _unavailable(reason or "Group data hasn't been collected yet")
    days = policy.get("groupLifetimeInDays")
    managed = policy.get("managedGroupTypes")
    if managed == "All":
        return ("pass", f"Unused teams and groups expire after {days} days", [])
    if managed == "Selected":
        never = [g for g in groups if not g.get("expirationDateTime")]
        if not never:
            return ("pass", f"Expiration covers every team and group ({days} days)", [])
        return (
            "fail",
            "Expiration covers selected groups only",
            [
                Finding(
                    "low",
                    "Tenant",
                    f"{len(never)} of {plural(len(groups), 'team or group')} "
                    + verb(len(never), "never expires", "never expire"),
                )
            ],
        )
    if snap.capabilities.get("entra_p1") is False:
        return _unavailable("Group expiration needs Entra ID P1")
    if not groups:
        return (
            "fail",
            "No expiration policy",
            [Finding("info", "Tenant", "No expiration policy; the tenant has no teams or Microsoft 365 groups yet")],
        )
    return (
        "fail",
        "No expiration policy",
        [
            Finding(
                "low",
                "Tenant",
                f"No expiration policy: {plural(len(groups), 'team or group')} "
                f"{verb(len(groups), 'is', 'are')} kept until someone deletes "
                f"{verb(len(groups), 'it', 'them')}",
            )
        ],
    )
