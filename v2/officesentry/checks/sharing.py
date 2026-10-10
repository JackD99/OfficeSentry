"""Checks on external sharing and guest invitations."""

from __future__ import annotations

from datetime import datetime

from .. import (
    devices,
)
from ..snapshot import TenantSnapshot
from ..text import day, plural
from .apps import _authorization_policy
from .base import Finding, _label, _unavailable, rule

SHARING_LEVELS = {
    "disabled": "Only people in the organisation",
    "existingExternalUserSharingOnly": "Existing guests only",
    "externalUserSharingOnly": "New and existing guests",
    "externalUserAndGuestSharing": "Anyone, including anonymous links",
}


@rule("sharepoint_sharing", "Files can't be shared with anonymous links", "Sharing",
      "Change SharePoint external sharing to \"New and existing guests\" so every external person signs in. "
      "If the client relies on Anyone links, set them to expire and to view-only.")
def sharepoint_sharing(snap: TenantSnapshot, now: datetime):
    rows = snap.objects("tenant_policies", "sharepoint_settings")
    if snap.objects("tenant_policies", "authorization_policy") is None:
        return _unavailable("Sharing settings haven't been collected yet")
    if not rows:
        return _unavailable("SharePoint sharing settings weren't available")
    level = rows[0].get("sharingCapability")
    label = SHARING_LEVELS.get(level, level or "unknown")
    if level == "externalUserAndGuestSharing":
        return ("fail", label, [Finding("medium", "SharePoint and OneDrive", "Anyone links are allowed")])
    return ("pass", label, [])


@rule("guest_invites", "Only admins and members can invite guests", "Guests",
      "Restrict guest invitations to admins and the Guest Inviter role, or at least to members, under "
      "External collaboration settings.")
def guest_invites(snap: TenantSnapshot, now: datetime):
    policy = _authorization_policy(snap)
    if policy is None:
        return _unavailable("Guest settings haven't been collected yet")
    if policy.get("allowInvitesFrom") == "everyone":
        return ("fail", "Anyone, including guests, can invite guests", [Finding(
            "low", "Tenant", "Guests can invite other guests")])
    return ("pass", {"none": "Nobody can invite guests", "adminsAndGuestInviters": "Only admins can invite guests",
                     }.get(policy.get("allowInvitesFrom"), "Members and admins can invite guests"), [])


@rule("pending_guests", "No old, unaccepted guest invitations", "Guests",
      "Delete guest accounts whose invitation has gone unaccepted for 30 days; re-invite if still needed.")
def pending_guests(snap: TenantSnapshot, now: datetime):
    users = snap.objects("users", "user")
    if users is None:
        return _unavailable("User data hasn't been collected yet")
    cutoff = now.timestamp() - 30 * 86400
    findings = []
    for u in users:
        created = u.get("createdDateTime")
        if u.get("userType") != "Guest" or u.get("externalUserState") != "PendingAcceptance" \
                or not devices.parse_ts(created):
            continue
        if devices.parse_ts(created).timestamp() < cutoff:
            findings.append(Finding("low", _label(u), f"Invited {day(created)}, never accepted", u["_id"]))
    return ("fail", f"{plural(len(findings), 'guest invitation')} unaccepted for 30+ days", findings) if findings else \
        ("pass", "No stale guest invitations", [])
