"""Identity security collectors: admin roles, Conditional Access, MFA registration."""

from __future__ import annotations

from .. import store
from ..graph import GraphError, LicenseRequired, PermissionMissing
from ..text import plural
from .base import Collector, RunContext, register

# Built-in roles treated as privileged. Graph v1.0 has no isPrivileged property
# (selecting it is a 400), so match by display name, which is stable for built-in roles.
PRIVILEGED_ROLE_NAMES = {
    "Global Administrator",
    "Privileged Role Administrator",
    "Privileged Authentication Administrator",
    "Security Administrator",
    "Conditional Access Administrator",
    "Exchange Administrator",
    "SharePoint Administrator",
    "User Administrator",
    "Authentication Administrator",
    "Application Administrator",
    "Cloud Application Administrator",
    "Intune Administrator",
    "Hybrid Identity Administrator",
    "Helpdesk Administrator",
    "Billing Administrator",
}

# Authentication method types that count as a second factor.
MFA_METHOD_TYPES = {
    "#microsoft.graph.microsoftAuthenticatorAuthenticationMethod",
    "#microsoft.graph.phoneAuthenticationMethod",
    "#microsoft.graph.fido2AuthenticationMethod",
    "#microsoft.graph.windowsHelloForBusinessAuthenticationMethod",
    "#microsoft.graph.softwareOathAuthenticationMethod",
    "#microsoft.graph.hardwareOathAuthenticationMethod",
    "#microsoft.graph.platformCredentialAuthenticationMethod",
    "#microsoft.graph.x509CertificateAuthenticationMethod",
    "#microsoft.graph.passkeyAuthenticationMethod",
}

PER_USER_MFA_LIMIT = 2000
CA_GROUP_LIMIT = 200


def _principal_summary(principal: dict | None, principal_id: str) -> dict:
    principal = principal or {}
    kind = (principal.get("@odata.type") or "").rsplit(".", 1)[-1] or "unknown"
    return {
        "principalId": principal_id,
        "principalType": kind,
        "displayName": principal.get("displayName"),
        "userPrincipalName": principal.get("userPrincipalName"),
        "accountEnabled": principal.get("accountEnabled"),
        "userType": principal.get("userType"),
        "appId": principal.get("appId"),
    }


@register
class AdminRoles(Collector):
    key = "admin_roles"
    title = "Admin roles"
    description = "Who holds each directory role, active and PIM-eligible."
    permissions = ("RoleManagement.Read.Directory",)

    def collect(self, ctx: RunContext) -> None:
        definitions = ctx.paged(ctx.graph.get_all("/roleManagement/directory/roleDefinitions", params={
            "$select": "id,displayName,isBuiltIn,templateId"}))
        roles = {}
        for d in definitions:
            roles[d["id"]] = {
                "displayName": d.get("displayName"),
                "isBuiltIn": d.get("isBuiltIn"),
                "templateId": d.get("templateId"),
                "privileged": d.get("displayName") in PRIVILEGED_ROLE_NAMES,
            }
        ctx.objects("role_definition", list(roles.items()))

        assignments = ctx.paged(ctx.graph.get_all("/roleManagement/directory/roleAssignments", params={
            "$expand": "principal"}))
        rows = []
        for a in assignments:
            role = roles.get(a.get("roleDefinitionId"), {})
            rows.append((a["id"], {
                **_principal_summary(a.get("principal"), a.get("principalId")),
                "roleDefinitionId": a.get("roleDefinitionId"),
                "roleName": role.get("displayName"),
                "privileged": role.get("privileged", False),
                "directoryScopeId": a.get("directoryScopeId"),
                "assignment": "active",
            }))

        try:
            eligible = ctx.paged(ctx.graph.get_all("/roleManagement/directory/roleEligibilitySchedules", params={
                "$expand": "principal"}))
            for e in eligible:
                role = roles.get(e.get("roleDefinitionId"), {})
                rows.append((f"eligible:{e['id']}", {
                    **_principal_summary(e.get("principal"), e.get("principalId")),
                    "roleDefinitionId": e.get("roleDefinitionId"),
                    "roleName": role.get("displayName"),
                    "privileged": role.get("privileged", False),
                    "directoryScopeId": e.get("directoryScopeId"),
                    "assignment": "eligible",
                }))
        except (LicenseRequired, PermissionMissing):
            ctx.log("PIM eligibility not available (needs Entra ID P2); active assignments only")

        ctx.objects("role_assignment", rows)
        data = [r for _, r in rows]
        priv_active = [r for r in data if r["privileged"] and r["assignment"] == "active"]
        ctx.metric("admins.privileged_active_assignments", len(priv_active))
        ctx.metric("admins.privileged_principals", len({r["principalId"] for r in priv_active}))
        ctx.metric("admins.global_admins", len({r["principalId"] for r in priv_active
                                               if r["roleName"] == "Global Administrator"}))


@register
class ConditionalAccess(Collector):
    key = "conditional_access"
    title = "Conditional Access"
    description = "Conditional Access policies and the Security Defaults setting."
    permissions = ("Policy.Read.All",)

    def collect(self, ctx: RunContext) -> None:
        defaults = ctx.graph.get("/policies/identitySecurityDefaultsEnforcementPolicy")
        enabled = bool(defaults.get("isEnabled"))
        ctx.objects("security_defaults", [("security_defaults", {"isEnabled": enabled})])
        ctx.metric("ca.security_defaults_enabled", int(enabled))

        if ctx.capabilities.get("entra_p1") is False:
            ctx.log("Tenant has no Entra ID P1; Conditional Access is not available")
            ctx.objects("ca_policy", [])
            ctx.metric("ca.policies_enabled", 0)
            return
        try:
            policies = ctx.paged(ctx.graph.get_all("/identity/conditionalAccess/policies"))
        except LicenseRequired:
            ctx.warn("Conditional Access needs Entra ID P1")
            return
        ctx.objects("ca_policy", [(p["id"], p) for p in policies])
        ctx.objects("ca_group", _expand_groups(ctx, policies))
        ctx.metric("ca.policies_total", len(policies))
        ctx.metric("ca.policies_enabled", sum(p.get("state") == "enabled" for p in policies))
        ctx.metric("ca.policies_report_only",
                   sum(p.get("state") == "enabledForReportingButNotEnforced" for p in policies))


def _policy_group_ids(policies: list[dict]) -> list[str]:
    ids = []
    for p in policies:
        users = (p.get("conditions") or {}).get("users") or {}
        for key in ("includeGroups", "excludeGroups"):
            for gid in users.get(key) or []:
                if gid not in ids:
                    ids.append(gid)
    return ids


def _expand_groups(ctx: RunContext, policies: list[dict]) -> list[tuple[str, dict]]:
    """Name and transitive user members of every group a policy includes or excludes."""
    ids = _policy_group_ids(policies)
    if len(ids) > CA_GROUP_LIMIT:
        ctx.warn(f"Expanded the first {CA_GROUP_LIMIT} of {len(ids)} groups used by Conditional Access")
        ids = ids[:CA_GROUP_LIMIT]
    rows = []
    for gid in ids:
        try:
            name = ctx.graph.get(f"/groups/{gid}", params={"$select": "displayName"}).get("displayName")
            members = ctx.paged(ctx.graph.get_all(f"/groups/{gid}/transitiveMembers", params={
                "$select": "id", "$top": "999"}))
        except GraphError as exc:
            ctx.log(f"Could not read group {gid}: {exc.message}", "warning")
            rows.append((gid, {"displayName": None, "members": None}))
            continue
        rows.append((gid, {"displayName": name, "members": sorted(
            m["id"] for m in members if (m.get("@odata.type") or "#microsoft.graph.user") == "#microsoft.graph.user")}))
    return rows


@register
class MfaRegistration(Collector):
    key = "mfa_registration"
    title = "MFA registration"
    description = "Which users have registered a second factor."
    permissions = ("AuditLog.Read.All", "UserAuthenticationMethod.Read.All")

    def collect(self, ctx: RunContext) -> None:
        rows = None
        if ctx.capabilities.get("entra_p1", True):
            try:
                details = ctx.paged(ctx.graph.get_all("/reports/authenticationMethods/userRegistrationDetails"))
                rows = [(d["id"], {
                    "userPrincipalName": d.get("userPrincipalName"),
                    "displayName": d.get("userDisplayName"),
                    "userType": d.get("userType"),
                    "isAdmin": d.get("isAdmin"),
                    "isMfaRegistered": bool(d.get("isMfaRegistered")),
                    "isMfaCapable": bool(d.get("isMfaCapable")),
                    "isPasswordlessCapable": bool(d.get("isPasswordlessCapable")),
                    "methodsRegistered": d.get("methodsRegistered") or [],
                    "defaultMfaMethod": d.get("defaultMfaMethod"),
                    "source": "registration_report",
                }) for d in details]
            except LicenseRequired:
                ctx.log("Registration report needs Entra ID P1; checking users' methods one by one")
        if rows is None:
            rows = self._per_user(ctx)
        else:
            # A newly licensed tenant's report can be empty or miss people for a day or two. Check
            # anyone it leaves out one by one, so they're never counted as registered or skipped.
            missing = self._per_user(ctx, skip={uid for uid, _ in rows}, fetch_users=not rows)
            if missing:
                ctx.log(f"Registration report left out {plural(len(missing), 'enabled member')}; checked their methods "
                        "one by one")
                rows += missing

        ctx.objects("mfa_registration", rows)
        data = [r for _, r in rows]
        ctx.metric("mfa.users_checked", len(data))
        ctx.metric("mfa.registered", sum(r["isMfaRegistered"] for r in data))
        ctx.metric("mfa.not_registered", sum(not r["isMfaRegistered"] for r in data))
        if data:
            ctx.metric("mfa.registered_pct", round(100 * sum(r["isMfaRegistered"] for r in data) / len(data), 1))

    def _per_user(self, ctx: RunContext, skip: set[str] = frozenset(),
                  fetch_users: bool = True) -> list[tuple[str, dict]]:
        users_run = store.latest_run(ctx.conn, ctx.tenant["id"], "users")
        if users_run:
            users = store.run_objects(ctx.conn, users_run["id"], "user")
        elif not fetch_users:
            return []
        else:
            users = [dict(u, _id=u["id"]) for u in ctx.paged(ctx.graph.get_all("/users", params={
                "$select": "id,userPrincipalName,displayName,userType,accountEnabled", "$top": "999"}))]
        members = [u for u in users if u.get("accountEnabled") and (u.get("userType") or "Member") == "Member"
                   and u["_id"] not in skip]
        if len(members) > PER_USER_MFA_LIMIT:
            ctx.warn(f"Checked the first {PER_USER_MFA_LIMIT} of {len(members)} enabled members")
            members = members[:PER_USER_MFA_LIMIT]
        rows = []
        for u in members:
            methods = ctx.paged(ctx.graph.get_all(f"/users/{u['_id']}/authentication/methods"))
            types = sorted({m.get("@odata.type", "") for m in methods})
            registered = any(t in MFA_METHOD_TYPES for t in types)
            rows.append((u["_id"], {
                "userPrincipalName": u.get("userPrincipalName"),
                "displayName": u.get("displayName"),
                "userType": u.get("userType") or "Member",
                "isAdmin": None,
                "isMfaRegistered": registered,
                "isMfaCapable": registered,
                "isPasswordlessCapable": None,
                "methodsRegistered": [t.rsplit(".", 1)[-1].replace("AuthenticationMethod", "") for t in types],
                "defaultMfaMethod": None,
                "source": "per_user_methods",
            }))
        return rows
