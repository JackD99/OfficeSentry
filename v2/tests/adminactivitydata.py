"""Directory audit fixtures (shapes as Graph returns them), with times relative to now."""

import json
from datetime import UTC, datetime, timedelta


def ago(days: float) -> str:
    return (datetime.now(UTC) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S.%f0Z")


def by_user(upn: str, ip: str = "203.0.113.40", display: str | None = None) -> dict:
    return {"app": None, "user": {"id": f"id-{upn}", "displayName": display, "userPrincipalName": upn,
                                  "ipAddress": ip, "userType": None, "agentType": "notAgentic",
                                  "homeTenantId": None, "homeTenantName": None}}


def by_app(name: str) -> dict:
    return {"user": None, "app": {"appId": None, "displayName": name, "servicePrincipalId": None,
                                  "servicePrincipalName": None}}


def prop(name: str, new, old=None, raw: bool = False) -> dict:
    """Graph sends each value as JSON text: strings quoted, objects and arrays as-is (raw)."""
    enc = lambda v: None if v is None else (v if raw else json.dumps(v))  # noqa: E731
    return {"displayName": name, "oldValue": enc(old), "newValue": enc(new)}


def target(kind: str, name: str | None = None, upn: str | None = None, props=(), tid: str = "t") -> dict:
    return {"id": tid, "displayName": name, "type": kind, "userPrincipalName": upn, "groupType": None,
            "modifiedProperties": list(props)}


def event(eid: str, category: str, activity: str, when: str, initiated: dict, targets: list,
          result: str = "success", reason: str = "", service: str = "Core Directory") -> dict:
    return {"id": f"Directory_{eid}", "category": category, "correlationId": eid, "result": result,
            "resultReason": reason, "activityDisplayName": activity, "activityDateTime": when,
            "loggedByService": service, "operationType": "Update", "initiatedBy": initiated,
            "targetResources": targets, "additionalDetails": [{"key": "User-Agent", "value": "Mozilla/5.0"}]}


ADMIN = "admin@x.com"
KEY_SECRET = "[KeyIdentifier=c93f8688-1d16-426f-9b6e-2a87e096c551,KeyType=Password,KeyUsage=Verify,DisplayName=Old]"
KEY_CERT = "[KeyIdentifier=8e567d9e-bf93-42c0-ad07-f969662e376e,KeyType=AsymmetricX509Cert,KeyUsage=Verify,DisplayName=CN=New]"  # noqa: E501
CA_ON = {"id": "ca1", "displayName": "Require MFA for all users", "state": "enabled"}
CA_OFF = {**CA_ON, "state": "disabled"}

GLOBAL_ADMIN_GRANT = event(
    "role1", "RoleManagement", "Add member to role", ago(2), by_user(ADMIN),
    [target("User", None, "bob@x.com", [prop("Role.DisplayName", "Global Administrator"),
                                        prop("Role.TemplateId", "62e90394-69f5-4237-9190-012177145e10")]),
     target("Role", None, None, tid="role-ga")])
PIM_ELIGIBLE = event(
    "role2", "RoleManagement", "Add eligible member to role in PIM completed (permanent)", ago(3),
    by_user(ADMIN), [target("Role", "Exchange Administrator"), target("User", "Ann", "ann@x.com")], service="PIM")
ROLE_REMOVED = event(
    "role3", "RoleManagement", "Remove member from role", ago(4), by_user(ADMIN),
    [target("User", None, "old.admin@x.com", [prop("Role.DisplayName", None, "Global Administrator")])])
ROLE_GRANT_FAILED = event(
    "role4", "RoleManagement", "Add member to role", ago(1), by_user("mallory@x.com", "203.0.113.9"),
    [target("User", None, "mallory@x.com", [prop("Role.DisplayName", "Global Administrator")])],
    result="failure", reason="Insufficient privileges")
CA_DISABLED = event(
    "ca1", "Policy", "Update conditional access policy", ago(5), by_user(ADMIN),
    [target("Policy", "Require MFA for all users", props=[prop("ConditionalAccessPolicy", json.dumps(CA_OFF),
                                                               json.dumps(CA_ON), raw=True)])],
    service="Conditional Access")
CA_ADDED = event(
    "ca2", "Policy", "Add conditional access policy", ago(6), by_user(ADMIN),
    [target("Policy", "Block legacy authentication",
            props=[prop("ConditionalAccessPolicy", json.dumps({**CA_ON, "displayName": "Block legacy"}), raw=True)])],
    service="Conditional Access")
CA_DELETED = event(
    "ca3", "Policy", "Delete conditional access policy", ago(6.5), by_user(ADMIN),
    [target("Policy", "Block risky countries")], service="Conditional Access")
SECRET_ADDED = event(
    "cred1", "ApplicationManagement", "Update application – Certificates and secrets management ", ago(1.5),
    by_user(ADMIN), [target("Application", "Payroll Sync", props=[
        prop("KeyDescription", f"[\"{KEY_SECRET}\",\"{KEY_CERT}\"]", f"[\"{KEY_SECRET}\"]", raw=True),
        prop("Included Updated Properties", "KeyDescription")])])
SECRET_REMOVED = event(
    "cred2", "ApplicationManagement", "Update application – Certificates and secrets management ", ago(8),
    by_user(ADMIN), [target("Application", "Old Integration", props=[
        prop("KeyDescription", "[]", f"[\"{KEY_SECRET}\"]", raw=True)])])
SP_CREDENTIAL = event(
    "cred3", "ApplicationManagement", "Add service principal credentials", ago(0.5), by_app("Contoso Automation"),
    [target("ServicePrincipal", "Mail Sync Pro")])
CONSENT = event(
    "app1", "ApplicationManagement", "Consent to application", ago(9), by_user(ADMIN, display="Azure ESTS Service"),
    [target("ServicePrincipal", "Mail Sync Pro", props=[
        prop("ConsentContext.IsAdminConsent", "True"), prop("ConsentContext.OnBehalfOfAll", "True"),
        prop("ConsentAction.Permissions", "[] => [[Id: x, ClientId: y, ConsentType: AllPrincipals, "
                                          "Scope: Mail.Read User.Read, CreatedDateTime: ]]; ")])])
APP_ROLE = event(
    "app2", "ApplicationManagement", "Add app role assignment to service principal", ago(9.1), by_user(ADMIN),
    [target("ServicePrincipal", "Microsoft Graph", props=[
        prop("AppRole.Value", "Mail.ReadWrite"), prop("ServicePrincipal.DisplayName", "Mail Sync Pro")]),
     target("ServicePrincipal", "2f2585b1-3ee5-4e47-a72f-cd9deac07095")])
FEDERATION = event(
    "dom1", "DirectoryManagement", "Set domain authentication", ago(2.5), by_user("mallory@x.com", "203.0.113.9"),
    [target("Domain", "x.com", props=[prop("LiveType", "Federated", "Managed")])])
DOMAIN_VERIFIED = event(
    "dom2", "DirectoryManagement", "Verify domain", ago(12), by_user(ADMIN), [target("Domain", "new.x.com")])
PASSWORD_RESET = event(
    "usr1", "UserManagement", "Reset user password", ago(3.5), by_user(ADMIN), [target("User", None, "ann@x.com")])
USER_DELETED = event(
    "usr2", "UserManagement", "Delete user", ago(10), by_user(ADMIN),
    [target("User", None, f"leaver{i}@x.com", tid=f"u{i}") for i in range(7)])
ADMIN_SECURITY_INFO = event(
    "usr3", "UserManagement", "Admin registered security info", ago(4.5), by_user(ADMIN),
    [target("User", None, "ann@x.com")], service="Authentication Methods")
PARTNER_ADDED = event(
    "prt1", "DirectoryManagement", "Add partner to company", ago(11), by_app("Partner Center"),
    [target("Other", "Contoso IT Services")])

# Routine noise that must not be kept.
NOISE = [
    event("n1", "ApplicationManagement", "Update service principal", ago(0.2),
          by_app("Microsoft Azure AD Internal - Jit Provisioning"), [target("ServicePrincipal", "Agent 365")]),
    event("n2", "UserManagement", "Change user license", ago(0.3), by_user(ADMIN), [target("User", None, "ann@x.com")]),
    event("n3", "UserManagement", "User registered security info", ago(0.4), by_user("ann@x.com"),
          [target("User", None, "ann@x.com")], service="Authentication Methods"),
    event("n4", "UserManagement", "Reset password (self-service)", ago(0.6), by_user("bob@x.com"),
          [target("User", None, "bob@x.com")]),
    # Right name, wrong category: the allowlist is keyed on both.
    event("n5", "GroupManagement", "Add member to role", ago(0.7), by_user(ADMIN), [target("Group", "Sales")]),
]

KEPT = [SP_CREDENTIAL, ROLE_GRANT_FAILED, SECRET_ADDED, GLOBAL_ADMIN_GRANT, FEDERATION, PIM_ELIGIBLE,
        PASSWORD_RESET, ROLE_REMOVED, ADMIN_SECURITY_INFO, CA_DISABLED, CA_ADDED, CA_DELETED, SECRET_REMOVED,
        CONSENT, APP_ROLE, USER_DELETED, PARTNER_ADDED, DOMAIN_VERIFIED]

# Newest first, as Graph returns them.
DIRECTORY_AUDITS = sorted(KEPT + NOISE, key=lambda e: e["activityDateTime"], reverse=True)
