"""Checks on third-party apps, their permissions and credentials, and user consent."""

from __future__ import annotations

from datetime import datetime

from .. import devices
from ..snapshot import TenantSnapshot
from ..text import day, every, plural
from .base import SEVERITY_RANK, Finding, _unavailable, rule

HIGH_RISK_PERMISSIONS = {
    "Mail.Read",
    "Mail.ReadWrite",
    "Mail.Send",
    "MailboxSettings.ReadWrite",
    "full_access_as_app",
    "Exchange.ManageAsApp",
    "Files.Read.All",
    "Files.ReadWrite.All",
    "Sites.Read.All",
    "Sites.ReadWrite.All",
    "Sites.FullControl.All",
    "Directory.ReadWrite.All",
    "User.ReadWrite.All",
    "Group.ReadWrite.All",
    "RoleManagement.ReadWrite.Directory",
    "Application.ReadWrite.All",
    "AppRoleAssignment.ReadWrite.All",
    "Policy.ReadWrite.ConditionalAccess",
    "UserAuthenticationMethod.ReadWrite.All",
    "Domain.ReadWrite.All",
}
SENSITIVE_DELEGATED = {
    "Mail.Read",
    "Mail.ReadWrite",
    "Mail.Send",
    "Mail.Read.Shared",
    "Mail.ReadWrite.Shared",
    "full_access_as_user",
    "EWS.AccessAsUser.All",
    "Files.Read.All",
    "Files.ReadWrite.All",
    "Sites.Read.All",
    "Sites.ReadWrite.All",
    "Directory.ReadWrite.All",
    "Directory.AccessAsUser.All",
    "User.ReadWrite.All",
    "MailboxSettings.ReadWrite",
}


def app_risks(snap: TenantSnapshot) -> list[dict] | None:
    """Third-party apps with what they hold and how risky it is (shared with the Apps report)."""
    sps = snap.objects("app_consents", "service_principal")
    if sps is None:
        return None
    own = snap.own_app_ids
    perms = snap.objects("app_consents", "app_permission") or []
    grants = snap.objects("app_consents", "delegated_grant") or []
    out = []
    for sp in sps:
        if sp.get("microsoft") or sp.get("type") != "Application" or (sp.get("appId") or "").lower() in own:
            continue
        app_perms = sorted({p["permission"] for p in perms if p.get("clientId") == sp["_id"] and p.get("permission")})
        tenant_scopes, user_scopes, users = set(), set(), set()
        for g in grants:
            if g.get("clientId") != sp["_id"]:
                continue
            if g.get("consentType") == "AllPrincipals":
                tenant_scopes.update(g.get("scopes") or [])
            else:
                user_scopes.update(g.get("scopes") or [])
                users.add(g.get("principalId"))
        if not (app_perms or tenant_scopes or user_scopes):
            continue
        risky_app = sorted(set(app_perms) & HIGH_RISK_PERMISSIONS)
        risky_tenant = sorted(tenant_scopes & SENSITIVE_DELEGATED)
        risky_user = sorted(user_scopes & SENSITIVE_DELEGATED)
        verified = bool(sp.get("verifiedPublisher"))
        if risky_app:
            severity = "medium" if verified else "high"
        elif risky_tenant:
            severity = "medium"
        elif risky_user and not verified:
            severity = "low"
        else:
            severity = None
        out.append(
            {
                "id": sp["_id"],
                "name": sp.get("displayName") or sp["_id"],
                "publisher": sp.get("verifiedPublisher") or sp.get("publisherName") or "",
                "verified": verified,
                "app_permissions": app_perms,
                "tenant_scopes": sorted(tenant_scopes),
                "user_scopes": sorted(user_scopes),
                "users": len(users),
                "severity": severity,
                "risky": risky_app + [s for s in risky_tenant + risky_user if s not in risky_app],
            }
        )
    out.sort(key=lambda a: (SEVERITY_RANK.get(a["severity"] or "info", 9), a["name"].lower()))
    return out


@rule(
    "risky_app_permissions",
    "Third-party apps hold only the access they need",
    "Apps",
    "Review each app with the client. Remove apps nobody recognises (Enterprise applications > Delete), "
    "and for the rest confirm the vendor and that it still needs mailbox or file access.",
)
def risky_app_permissions(snap: TenantSnapshot, now: datetime):
    apps = app_risks(snap)
    if apps is None:
        return _unavailable("App data hasn't been collected yet")
    findings = []
    for a in apps:
        if not a["severity"]:
            continue
        who = "unverified publisher" if not a["verified"] else f"verified publisher {a['publisher']}"
        if set(a["risky"]) & set(a["app_permissions"]):
            how = "application permission"
        elif set(a["risky"]) & set(a["tenant_scopes"]):
            how = "consented for all users"
        else:
            how = f"consented by {plural(a['users'], 'user')}"
        findings.append(Finding(a["severity"], a["name"], f"{', '.join(a['risky'])} ({how}; {who})", a["id"]))
    return (
        ("fail", f"{plural(len(findings), 'app')} with access to mail, files or the directory", findings)
        if findings
        else ("pass", f"No third-party app holds high-risk access ({plural(len(apps), 'app')} reviewed)", [])
    )


def _authorization_policy(snap: TenantSnapshot):
    rows = snap.objects("tenant_policies", "authorization_policy")
    return rows[0] if rows else None


def user_consent_level(policy: dict) -> str:
    """'any' (legacy: any app), 'limited' (verified publishers, low-risk permissions) or 'off'."""
    assigned = [p for p in policy.get("permissionGrantPolicies") or [] if "ManagePermissionGrantsForSelf" in p]
    if any(p.endswith("microsoft-user-default-legacy") for p in assigned):
        return "any"
    return "limited" if assigned else "off"


@rule(
    "user_consent",
    "Users can't consent to apps on their own",
    "Apps",
    'Set user consent to "Allow user consent for apps from verified publishers, for selected permissions" '
    "or turn it off and use the admin consent workflow. Consent phishing relies on this setting.",
)
def user_consent(snap: TenantSnapshot, now: datetime):
    policy = _authorization_policy(snap)
    if policy is None:
        return _unavailable("Consent settings haven't been collected yet")
    level = user_consent_level(policy)
    if level == "any":
        return (
            "fail",
            "Users can consent to any app",
            [Finding("medium", "Tenant", "Any user can grant any app access to their mail and files")],
        )
    if level == "limited":
        return ("pass", "Users can consent only to verified publishers' low-risk permissions", [])
    return ("pass", "User consent is turned off", [])


@rule(
    "app_credentials",
    "App secrets and certificates are current",
    "Apps",
    "Renew credentials before they expire to avoid an outage, and delete app registrations whose "
    "credentials have all expired if nothing uses them any more.",
)
def app_credentials(snap: TenantSnapshot, now: datetime):
    apps = snap.objects("app_consents", "application")
    if apps is None:
        return _unavailable("App data hasn't been collected yet")
    soon = now.timestamp() + 30 * 86400
    findings = []
    for a in apps:
        ends = [c["endDateTime"] for c in a.get("credentials") or [] if devices.parse_ts(c.get("endDateTime"))]
        if not ends:
            continue
        latest = max(ends)
        latest_ts = devices.parse_ts(latest).timestamp()
        if latest_ts < now.timestamp():
            findings.append(
                Finding(
                    "low",
                    a.get("displayName") or a["_id"],
                    f"{every(len(ends), 'credential', start=True)} expired, last on {day(latest)}",
                    a["_id"],
                )
            )
        elif latest_ts < soon:
            findings.append(
                Finding(
                    "medium", a.get("displayName") or a["_id"], f"Last valid credential expires {day(latest)}", a["_id"]
                )
            )
    return (
        ("fail", f"{plural(len(findings), 'app registration')} with expiring or expired credentials", findings)
        if findings
        else ("pass", "No app registration credentials expire in the next 30 days", [])
    )
