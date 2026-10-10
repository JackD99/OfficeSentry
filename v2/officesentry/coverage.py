"""Conditional Access coverage: which enabled policies apply to which users.

Microsoft shows policies one at a time; this answers "is everyone covered,
and who slips through?" from the stored snapshots. Group membership comes from
the CA collector's expansion of every group a policy names. Where membership
couldn't be read, the user is treated as not included and as excluded, so a
gap is never hidden.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .text import plural

STATE_NAMES = {"enabled": "On", "disabled": "Off", "enabledForReportingButNotEnforced": "Report-only"}
CLIENT_APPS = {
    "browser": "Browser",
    "mobileAppsAndDesktopClients": "Modern apps",
    "exchangeActiveSync": "ActiveSync",
    "other": "Other legacy",
}


@dataclass
class Directory:
    """What policies are matched against."""

    groups: dict[str, dict]  # id -> {"displayName", "members": [ids] | None}
    user_roles: dict[str, set[str]]  # user id -> role template ids held (active)
    role_names: dict[str, str]  # role template id -> display name
    names: dict[str, str]  # any object id -> label


def _in_groups(uid: str, ids, d: Directory, unknown: bool) -> bool:
    for gid in ids or []:
        members = (d.groups.get(gid) or {}).get("members")
        if members is None:
            if unknown:
                return True
            continue
        if uid in members:
            return True
    return False


def _matches(uid: str, user_type: str, spec: dict, prefix: str, d: Directory, unknown: bool) -> bool:
    users = spec.get(f"{prefix}Users") or []
    if "All" in users or uid in users:
        return True
    if user_type == "Guest" and ("GuestsOrExternalUsers" in users or spec.get(f"{prefix}GuestsOrExternalUsers")):
        return True
    if _in_groups(uid, spec.get(f"{prefix}Groups"), d, unknown):
        return True
    return bool(set(spec.get(f"{prefix}Roles") or []) & d.user_roles.get(uid, set()))


def applies(policy: dict, uid: str, user_type: str, d: Directory) -> bool:
    spec = (policy.get("conditions") or {}).get("users") or {}
    return _matches(uid, user_type, spec, "include", d, unknown=False) and not _matches(
        uid, user_type, spec, "exclude", d, unknown=True
    )


def grants(policy: dict) -> set[str]:
    grant = policy.get("grantControls") or {}
    controls = set(grant.get("builtInControls") or [])
    if grant.get("authenticationStrength"):
        controls.add("mfa")
    return controls


def all_apps(policy: dict) -> bool:
    return "All" in (((policy.get("conditions") or {}).get("applications") or {}).get("includeApplications") or [])


# Conditions that need Entra ID P2. On a tenant without P2 a policy using them never fires.
P2_CONDITIONS = {
    "userRiskLevels": "user risk",
    "signInRiskLevels": "sign-in risk",
    "servicePrincipalRiskLevels": "app risk",
    "insiderRiskLevels": "insider risk",
}


def narrowing_conditions(policy: dict) -> list[str]:
    """Conditions that limit when a policy fires (risk, location, platform, device filter).

    A policy with any of them only applies to some sign-ins, so it doesn't
    enforce anything for everyone even if it targets all users and apps.
    Device filters can include or exclude, so any filter counts.
    """
    cond = policy.get("conditions") or {}
    out = [label for key, label in P2_CONDITIONS.items() if cond.get(key)]
    # Excluded locations or platforms (e.g. trusted networks) still leave the policy enforced elsewhere.
    if set((cond.get("locations") or {}).get("includeLocations") or ["All"]) != {"All"}:
        out.append("location")
    if set((cond.get("platforms") or {}).get("includePlatforms") or ["all"]) != {"all"}:
        out.append("platform")
    if (cond.get("devices") or {}).get("deviceFilter"):
        out.append("device filter")
    if cond.get("authenticationFlows"):
        out.append("authentication flow")
    return out


def needs_p2(policy: dict) -> list[str]:
    cond = policy.get("conditions") or {}
    return [label for key, label in P2_CONDITIONS.items() if cond.get(key)]


def requires_mfa_everywhere(policy: dict) -> bool:
    client_apps = set((policy.get("conditions") or {}).get("clientAppTypes") or ["all"])
    return (
        all_apps(policy)
        and "mfa" in grants(policy)
        and not narrowing_conditions(policy)
        and bool(client_apps & {"all", "browser", "mobileAppsAndDesktopClients"})
    )


def blocks_legacy(policy: dict) -> bool:
    client_apps = set((policy.get("conditions") or {}).get("clientAppTypes") or [])
    return (
        all_apps(policy)
        and "block" in grants(policy)
        and not narrowing_conditions(policy)
        and {"exchangeActiveSync", "other"} <= client_apps
    )


def _labels(ids, d: Directory, kind: str = "") -> list[str]:
    out = []
    for i in ids or []:
        if i in ("All", "None", "GuestsOrExternalUsers"):
            out.append({"All": "All users", "None": "Nobody", "GuestsOrExternalUsers": "Guests"}[i])
        elif kind == "group":
            out.append((d.groups.get(i) or {}).get("displayName") or f"Group {i[:8]}")
        elif kind == "role":
            out.append(d.role_names.get(i) or f"Role {i[:8]}")
        else:
            out.append(d.names.get(i) or i)
    return out


@dataclass
class PolicyRow:
    id: str
    name: str
    state: str
    users: str
    excluded: str
    apps: str
    conditions: str
    grant: str
    warning: str = ""


def describe(policy: dict, d: Directory, capabilities: dict | None = None) -> PolicyRow:
    cond = policy.get("conditions") or {}
    u = cond.get("users") or {}
    include = (
        _labels(u.get("includeUsers"), d)
        + _labels(u.get("includeGroups"), d, "group")
        + _labels(u.get("includeRoles"), d, "role")
    )
    if u.get("includeGuestsOrExternalUsers"):
        include.append("Guests and external users")
    exclude = (
        _labels(u.get("excludeUsers"), d)
        + _labels(u.get("excludeGroups"), d, "group")
        + _labels(u.get("excludeRoles"), d, "role")
    )
    if u.get("excludeGuestsOrExternalUsers"):
        exclude.append("Guests and external users")
    apps = cond.get("applications") or {}
    inc_apps = apps.get("includeApplications") or []
    app_text = (
        "All apps"
        if "All" in inc_apps
        else (
            "Admin portals"
            if "MicrosoftAdminPortals" in inc_apps
            else (
                plural(len(inc_apps), "app")
                if inc_apps
                else ("User actions" if apps.get("includeUserActions") else "No apps")
            )
        )
    )
    if apps.get("excludeApplications"):
        app_text += f", {len(apps['excludeApplications'])} excluded"
    parts = []
    client = [CLIENT_APPS.get(c, c) for c in cond.get("clientAppTypes") or [] if c != "all"]
    if client:
        parts.append("Clients: " + ", ".join(client))
    if cond.get("signInRiskLevels"):
        parts.append("Sign-in risk: " + ", ".join(cond["signInRiskLevels"]))
    if cond.get("userRiskLevels"):
        parts.append("User risk: " + ", ".join(cond["userRiskLevels"]))
    if (cond.get("platforms") or {}).get("includePlatforms"):
        parts.append("Platforms: " + ", ".join(cond["platforms"]["includePlatforms"]))
    if (cond.get("locations") or {}).get("includeLocations"):
        parts.append("Locations")
    grant = policy.get("grantControls") or {}
    controls = sorted(grants(policy))
    strength = (grant.get("authenticationStrength") or {}).get("displayName")
    mfa_text = strength if strength and strength != "Multifactor authentication" else "MFA"
    grant_text = (" and " if grant.get("operator") == "AND" else " or ").join(
        {
            "mfa": mfa_text,
            "block": "Block",
            "compliantDevice": "Compliant device",
            "domainJoinedDevice": "Hybrid joined",
            "approvedApplication": "Approved app",
            "compliantApplication": "App protection",
            "passwordChange": "Password change",
        }.get(c, c)
        for c in controls
    ) or ("Session controls" if policy.get("sessionControls") else "None")
    warning = ""
    p2 = needs_p2(policy)
    if p2 and (capabilities or {}).get("entra_p2") is False and policy.get("state") != "disabled":
        warning = (
            f"Uses {' and '.join(p2)}, which needs Entra ID P2. "
            "This tenant doesn't have it, so the policy never applies."
        )
    return PolicyRow(
        policy.get("id", "?"),
        policy.get("displayName") or "?",
        STATE_NAMES.get(policy.get("state"), policy.get("state") or "?"),
        ", ".join(include) or "Nobody",
        ", ".join(exclude),
        app_text,
        "; ".join(parts),
        grant_text,
        warning,
    )


@dataclass
class UserCoverage:
    id: str
    label: str
    user_type: str
    roles: list[str]
    mfa_policies: list[str] = field(default_factory=list)
    legacy_blocked: bool = False
    policies: list[str] = field(default_factory=list)
    excluded_from: list[str] = field(default_factory=list)

    @property
    def mfa(self) -> bool:
        return bool(self.mfa_policies)


def policy_name(p: dict) -> str:
    return p.get("displayName") or p.get("id") or p.get("_id") or "Unnamed policy"


def directory(snap) -> Directory | None:
    policies = snap.objects("conditional_access", "ca_policy")
    if policies is None:
        return None
    groups = {g["_id"]: g for g in snap.objects("conditional_access", "ca_group") or []}
    definitions = {r["_id"]: r for r in snap.objects("admin_roles", "role_definition") or []}
    role_names, user_roles = {}, {}
    for rid, r in definitions.items():
        role_names[r.get("templateId") or rid] = r.get("displayName")
    for a in snap.objects("admin_roles", "role_assignment") or []:
        if a.get("assignment") != "active" or a.get("directoryScopeId") not in (None, "/") or not a.get("principalId"):
            continue
        template = (definitions.get(a.get("roleDefinitionId")) or {}).get("templateId") or a.get("roleDefinitionId")
        user_roles.setdefault(a["principalId"], set()).add(template)
    names = {}
    for u in snap.objects("users", "user") or []:
        names[u["_id"]] = u.get("userPrincipalName") or u.get("displayName") or u["_id"]
    return Directory(groups, user_roles, role_names, names)


def coverage_for(uid: str, label: str, user_type: str, policies: list[dict], d: Directory) -> UserCoverage:
    cov = UserCoverage(uid, label, user_type, sorted(d.role_names.get(r, r) for r in d.user_roles.get(uid, set())))
    for p in policies:
        if p.get("state") != "enabled":
            continue
        spec = (p.get("conditions") or {}).get("users") or {}
        name = policy_name(p)
        if not applies(p, uid, user_type, d):
            if _matches(uid, user_type, spec, "include", d, unknown=False):
                cov.excluded_from.append(name)
            continue
        cov.policies.append(name)
        if requires_mfa_everywhere(p):
            cov.mfa_policies.append(name)
        if blocks_legacy(p):
            cov.legacy_blocked = True
    return cov


def user_coverage(snap) -> list[UserCoverage] | None:
    """Coverage for every enabled account, gaps first. None when the data isn't there."""
    d = directory(snap)
    users = snap.objects("users", "user")
    if d is None or users is None:
        return None
    policies = snap.objects("conditional_access", "ca_policy") or []
    out = [
        coverage_for(u["_id"], d.names.get(u["_id"], u["_id"]), u.get("userType") or "Member", policies, d)
        for u in users
        if u.get("accountEnabled")
    ]
    out.sort(key=lambda c: (c.mfa, not c.roles, c.label.lower()))
    return out
