"""The tenant's authentication methods policy: which sign-in methods are on, and for whom.

One GET of ``/policies/authenticationMethodsPolicy`` (Policy.Read.All), which
includes each method's settings, plus the names of the groups those settings
target. Nothing is changed.
"""

from __future__ import annotations

from ..graph import GraphError, PermissionMissing
from .base import Collector, RunContext, register

GROUP_LIMIT = 50
# Settings worth keeping per method; everything else (key restrictions, lifetimes) stays out.
METHOD_SETTINGS = (
    "isSelfServiceRegistrationAllowed",
    "isAttestationEnforced",
    "isOfficePhoneAllowed",
    "allowExternalIdToUseEmailOtp",
    "isSoftwareOathEnabled",
    "isUsableOnce",
)
FEATURES = (
    "displayAppInformationRequiredState",
    "displayLocationInformationRequiredState",
    "numberMatchingRequiredState",
)
ALL_USERS = "all_users"


def _targets(value) -> list[dict]:
    return [
        {
            "id": t.get("id"),
            "targetType": t.get("targetType"),
            "isRegistrationRequired": t.get("isRegistrationRequired"),
            "targetedAuthenticationMethod": t.get("targetedAuthenticationMethod"),
        }
        for t in value or []
        if isinstance(t, dict)
    ]


def _feature_states(config: dict) -> dict:
    features = config.get("featureSettings") or {}
    return {name: ((features.get(name) or {}).get("state")) for name in FEATURES if name in features}


def summarise(policy: dict) -> tuple[dict, list[tuple[str, dict]]]:
    """The policy's tenant-wide settings, and one row per sign-in method."""
    campaign = (policy.get("registrationEnforcement") or {}).get("authenticationMethodsRegistrationCampaign") or {}
    system = policy.get("systemCredentialPreferences") or {}
    report = policy.get("reportSuspiciousActivitySettings") or {}
    settings = {
        "policyMigrationState": policy.get("policyMigrationState"),
        "lastModifiedDateTime": policy.get("lastModifiedDateTime"),
        "methodsReturned": "authenticationMethodConfigurations" in policy,
        "registrationCampaign": {
            "state": campaign.get("state"),
            "snoozeDurationInDays": campaign.get("snoozeDurationInDays"),
            "enforceRegistrationAfterAllowedSnoozes": campaign.get("enforceRegistrationAfterAllowedSnoozes"),
            "includeTargets": _targets(campaign.get("includeTargets")),
            "excludeTargets": _targets(campaign.get("excludeTargets")),
        }
        if campaign
        else None,
        "systemCredentialPreferences": {
            "state": system.get("state"),
            "includeTargets": _targets(system.get("includeTargets")),
            "excludeTargets": _targets(system.get("excludeTargets")),
        }
        if system
        else None,
        "reportSuspiciousActivity": {"state": report.get("state")} if report else None,
    }
    methods = []
    for c in policy.get("authenticationMethodConfigurations") or []:
        if not isinstance(c, dict) or not c.get("id"):
            continue
        methods.append(
            (
                c["id"],
                {
                    "state": c.get("state"),
                    "includeTargets": _targets(c.get("includeTargets")),
                    "excludeTargets": _targets(c.get("excludeTargets")),
                    "settings": {k: c[k] for k in METHOD_SETTINGS if k in c},
                    "features": _feature_states(c),
                },
            )
        )
    return settings, methods


def target_groups(settings: dict, methods: list[tuple[str, dict]]) -> list[str]:
    """Group IDs the policy names (``all_users`` aside), in the order they first appear."""
    lists = [(settings.get(k) or {}) for k in ("registrationCampaign", "systemCredentialPreferences")]
    lists += [m for _, m in methods]
    ids: list[str] = []
    for item in lists:
        for t in (item.get("includeTargets") or []) + (item.get("excludeTargets") or []):
            gid = t.get("id")
            if gid and gid != ALL_USERS and (t.get("targetType") or "group") == "group" and gid not in ids:
                ids.append(gid)
    return ids


@register
class AuthMethodsPolicy(Collector):
    key = "auth_methods_policy"
    title = "Sign-in methods policy"
    description = (
        "Which sign-in methods the tenant allows and for whom, the registration campaign and system-preferred MFA."
    )
    permissions = ("Policy.Read.All", "Directory.Read.All")

    def collect(self, ctx: RunContext) -> None:
        try:
            policy = ctx.graph.get("/policies/authenticationMethodsPolicy")
        except PermissionMissing as exc:
            ctx.warn(f"The authentication methods policy wasn't readable: {exc.message}")
            return
        settings, methods = summarise(policy)
        ctx.objects("auth_methods_policy", [("auth_methods_policy", settings)])
        ctx.objects("auth_method_config", methods)

        ids = target_groups(settings, methods)
        if len(ids) > GROUP_LIMIT:
            ctx.log(f"Named the first {GROUP_LIMIT} of {len(ids)} groups the policy targets")
            ids = ids[:GROUP_LIMIT]
        groups = []
        for gid in ids:
            try:
                name = ctx.graph.get(f"/groups/{gid}", params={"$select": "id,displayName"}).get("displayName")
            except GraphError as exc:  # deleted group, say: the policy still names it
                ctx.log(f"Could not read group {gid}: {exc.message}", "warning")
                name = None
            groups.append((gid, {"displayName": name}))
        ctx.objects("auth_policy_group", groups)

        on = {mid for mid, m in methods if m.get("state") == "enabled"}
        ctx.metric("auth_methods.enabled", len(on))
        ctx.metric("auth_methods.sms_or_voice_enabled", int(bool(on & {"Sms", "Voice"})))
