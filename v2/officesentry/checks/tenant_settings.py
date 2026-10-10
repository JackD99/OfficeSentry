"""Checks on what users and guests can do by default."""

from __future__ import annotations

from datetime import datetime

from ..snapshot import TenantSnapshot
from ..text import plural
from .apps import _authorization_policy, custom_consent_policies, user_consent_level
from .base import Finding, _unavailable, rule

GUEST_ROLES = {
    "a0b1b346-4d3e-4e8b-98f8-753987be4970": "member",
    "10dae51f-b6af-4016-8d66-8c2a99b929b3": "limited",
    "2af84b1e-32c8-42b7-82bc-daa82404023b": "restricted",
}


def _setting(snap: TenantSnapshot, key: str) -> tuple[dict | None, str]:
    """(policy, "") when an authorization policy field can be checked, else (None, why not)."""
    policy = _authorization_policy(snap)
    if policy is None:
        return None, "Tenant settings haven't been collected yet"
    if key not in policy:
        return None, "Collected before this setting was recorded; refresh Sharing and consent settings"
    if policy[key] is None:
        return None, "Microsoft didn't return this setting"
    return policy, ""


@rule(
    "tenant_creation",
    "Users can't create new tenants",
    "Tenant settings",
    'Set "Restrict non-admin users from creating tenants" to Yes (Entra admin center > Users > User '
    "settings). A tenant a user creates sits outside the client's policies, with that user as its Global "
    "Administrator.",
)
def tenant_creation(snap: TenantSnapshot, now: datetime):
    policy, reason = _setting(snap, "allowedToCreateTenants")
    if policy is None:
        return _unavailable(reason)
    if policy["allowedToCreateTenants"]:
        return (
            "fail",
            "Any user can create a new Entra ID tenant",
            [Finding("low", "Tenant", "Users can create tenants outside the client's control")],
        )
    return ("pass", "Only admins can create tenants", [])


@rule(
    "app_registration",
    "Users can't register apps",
    "Tenant settings",
    'Set "Users can register applications" to No (Entra admin center > Users > User settings) and give '
    "the Application Developer role to the people who need it.",
)
def app_registration(snap: TenantSnapshot, now: datetime):
    policy, reason = _setting(snap, "allowedToCreateApps")
    if policy is None:
        return _unavailable(reason)
    if policy["allowedToCreateApps"]:
        return (
            "fail",
            "Any user can register an app",
            [Finding("low", "Tenant", "Users can register apps that nobody reviews")],
        )
    return ("pass", "Only admins and app developers can register apps", [])


@rule(
    "guest_access_level",
    "Guests have limited directory access",
    "Tenant settings",
    'Set guest user access to "Guest users have limited access to properties and memberships of directory '
    'objects", or to the most restrictive option (Entra admin center > External Identities > External '
    "collaboration settings).",
)
def guest_access_level(snap: TenantSnapshot, now: datetime):
    policy, reason = _setting(snap, "guestUserRoleId")
    if policy is None:
        return _unavailable(reason)
    role = policy["guestUserRoleId"]
    level = GUEST_ROLES.get(role) if isinstance(role, str) else None
    if level == "member":
        return (
            "fail",
            "Guests have the same directory access as members",
            [Finding("medium", "Tenant", "Guests can look up every user and group and their details")],
        )
    if level == "limited":
        return ("pass", "Guests have limited access to directory objects (the default)", [])
    if level == "restricted":
        return ("pass", "Guests can see only their own directory objects (the most restrictive setting)", [])
    return _unavailable(f"Unrecognised guest access setting {policy['guestUserRoleId']}")


@rule(
    "email_verified_join",
    "People can't join the tenant just by verifying an email address",
    "Tenant settings",
    "Turn off email-verified sign-up with Microsoft Graph PowerShell (Update-MgPolicyAuthorizationPolicy "
    "-AllowEmailVerifiedUsersToJoinOrganization:$false), so accounts are only created by admins or invitation.",
)
def email_verified_join(snap: TenantSnapshot, now: datetime):
    policy, reason = _setting(snap, "allowEmailVerifiedUsersToJoinOrganization")
    if policy is None:
        return _unavailable(reason)
    if policy["allowEmailVerifiedUsersToJoinOrganization"]:
        return (
            "fail",
            "People can join by verifying an email address",
            [Finding("low", "Tenant", "Anyone with a mailbox on one of the client's domains can create an account")],
        )
    return ("pass", "Nobody can join just by verifying an email address", [])


def admin_consent_missing(policy: dict) -> str:
    """Why a run that stored the authorization policy has no admin consent request settings."""
    if "allowedToCreateSecurityGroups" not in policy:  # stored by the same version of the collector
        return "Collected before admin consent requests were recorded; refresh Sharing and consent settings"
    return "Microsoft didn't return the admin consent request settings (see the run's warnings)"


@rule(
    "admin_consent_workflow",
    "Users have a way to get apps approved",
    "Tenant settings",
    "Turn on admin consent requests (Entra admin center > Enterprise apps > Consent and permissions > Admin "
    "consent settings) and choose reviewers, so users blocked from consenting can ask instead of looking for "
    "a workaround.",
)
def admin_consent_workflow(snap: TenantSnapshot, now: datetime):
    policy = _authorization_policy(snap)
    if policy is None:
        return _unavailable("Consent settings haven't been collected yet")
    rows = snap.objects("tenant_policies", "admin_consent_policy")
    if not rows:
        return _unavailable(admin_consent_missing(policy))
    workflow = rows[0]
    if workflow.get("isEnabled"):
        reviewers = len(workflow.get("reviewers") or [])
        if not reviewers:
            return (
                "fail",
                "Admin consent requests are on but have no reviewers",
                [Finding("low", "Tenant", "Users can send requests, but nobody can approve them")],
            )
        return ("pass", f"Users can ask an admin: requests are on, with {plural(reviewers, 'reviewer')}", [])
    consent = user_consent_level(policy)
    if consent == "any":
        return (
            "pass",
            "Users can consent to most apps themselves, so admin consent requests are off (see the user consent check)",
            [],
        )
    if consent == "limited":
        # Microsoft's default for new tenants: worth turning on, but not a standing failure.
        return (
            "pass",
            (
                "Users can consent to some apps under a custom policy"
                if custom_consent_policies(policy)
                else "Users can consent to low-risk apps themselves"
            )
            + "; admin consent requests are off, so they can't ask for anything else",
            [],
        )
    return (
        "fail",
        "Users can't consent to apps and have no way to ask",
        [
            Finding(
                "low",
                "Tenant",
                "User consent and admin consent requests are both off, so users are blocked from "
                "any app that needs consent, with no way to ask for it",
            )
        ],
    )
