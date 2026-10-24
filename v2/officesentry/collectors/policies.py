"""Tenant-wide sharing and consent settings: who can invite guests, consent to apps, share files, and what
other organisations' people can do here (cross-tenant access)."""

from __future__ import annotations

from ..graph import GraphError
from .base import Collector, RunContext, register

SHAREPOINT_FIELDS = (
    "sharingCapability",
    "sharingDomainRestrictionMode",
    "sharingAllowedDomainList",
    "sharingBlockedDomainList",
    "isResharingByExternalUsersEnabled",
    "isRequireAcceptingUserToMatchInvitedUserEnabled",
    "isUnmanagedSyncAppForTenantRestricted",
    "siteCreationDefaultStorageLimitInMB",
)


@register
class TenantPolicies(Collector):
    key = "tenant_policies"
    title = "Sharing and consent settings"
    description = (
        "Guest invitations, user consent to apps, default user permissions, admin consent requests, cross-tenant "
        "access settings, and SharePoint external sharing."
    )
    permissions = ("Policy.Read.All", "SharePointTenantSettings.Read.All")
    more_with = ("sharepoint",)

    def collect(self, ctx: RunContext) -> None:
        auth = ctx.graph.get("/policies/authorizationPolicy")
        if "value" in auth:  # some tenants still return a collection
            auth = (auth["value"] or [{}])[0]
        perms = auth.get("defaultUserRolePermissions") or {}
        ctx.objects(
            "authorization_policy",
            [
                (
                    "authorization_policy",
                    {
                        "allowInvitesFrom": auth.get("allowInvitesFrom"),
                        "guestUserRoleId": auth.get("guestUserRoleId"),
                        "allowedToSignUpEmailBasedSubscriptions": auth.get("allowedToSignUpEmailBasedSubscriptions"),
                        "allowUserConsentForRiskyApps": auth.get("allowUserConsentForRiskyApps"),
                        "permissionGrantPolicies": perms.get("permissionGrantPoliciesAssigned") or [],
                        "allowedToCreateApps": perms.get("allowedToCreateApps"),
                        "allowedToCreateTenants": perms.get("allowedToCreateTenants"),
                        "allowedToCreateSecurityGroups": perms.get("allowedToCreateSecurityGroups"),
                        "allowedToReadOtherUsers": perms.get("allowedToReadOtherUsers"),
                        "allowEmailVerifiedUsersToJoinOrganization": auth.get(
                            "allowEmailVerifiedUsersToJoinOrganization"
                        ),
                        "allowedToUseSSPR": auth.get("allowedToUseSSPR"),
                        "blockMsolPowerShell": auth.get("blockMsolPowerShell"),
                    },
                )
            ],
        )

        try:
            consent = ctx.graph.get("/policies/adminConsentRequestPolicy")
        except GraphError as exc:
            ctx.warn(f"Admin consent request settings unavailable: {exc.message}")
        else:
            ctx.objects(
                "admin_consent_policy",
                [
                    (
                        "admin_consent_policy",
                        {
                            "isEnabled": consent.get("isEnabled"),
                            "notifyReviewers": consent.get("notifyReviewers"),
                            "remindersEnabled": consent.get("remindersEnabled"),
                            "requestDurationInDays": consent.get("requestDurationInDays"),
                            "reviewers": [r.get("query") for r in consent.get("reviewers") or [] if r.get("query")],
                        },
                    )
                ],
            )

        self._cross_tenant(ctx)

        if ctx.capabilities.get("sharepoint") is False:
            ctx.log("Tenant has no SharePoint licence; skipping sharing settings")
            return
        try:
            sp = ctx.graph.get("/admin/sharepoint/settings")
        except GraphError as exc:
            ctx.warn(f"SharePoint sharing settings unavailable: {exc.message}")
            return
        ctx.objects("sharepoint_settings", [("sharepoint", {f: sp.get(f) for f in SHAREPOINT_FIELDS})])

    @staticmethod
    def _cross_tenant(ctx: RunContext) -> None:
        """The cross-tenant access settings: the default for every other organisation, and each organisation
        given its own."""
        try:
            default = ctx.graph.get("/policies/crossTenantAccessPolicy/default")
            partners = ctx.paged(ctx.graph.get_all("/policies/crossTenantAccessPolicy/partners"))
        except GraphError as exc:
            ctx.warn(f"Cross-tenant access settings weren't readable: {exc.message}")
            return
        ctx.objects("cross_tenant_default", [("default", cross_tenant(default))])
        ctx.objects(
            "cross_tenant_partner",
            [(p["tenantId"], cross_tenant(p)) for p in partners if isinstance(p, dict) and p.get("tenantId")],
        )


B2B_SETTINGS = (
    "b2bCollaborationInbound",
    "b2bCollaborationOutbound",
    "b2bDirectConnectInbound",
    "b2bDirectConnectOutbound",
)
TRUST = ("isMfaAccepted", "isCompliantDeviceAccepted", "isHybridAzureADJoinedDeviceAccepted")
MAX_TARGETS = 50


def _side(value) -> dict | None:
    """One B2B setting: who and which apps it allows or blocks. None when an organisation's own settings leave
    it to the default."""
    if not isinstance(value, dict):
        return None
    out = {}
    for part in ("usersAndGroups", "applications"):
        rule = value.get(part)
        if isinstance(rule, dict):
            targets = [t for t in rule.get("targets") or [] if isinstance(t, dict) and isinstance(t.get("target"), str)]
            out[part] = {
                "access": rule.get("accessType"),
                "targets": [t["target"] for t in targets][:MAX_TARGETS],
                "count": len(targets),
            }
    return out or None


def cross_tenant(policy: dict) -> dict:
    """The default cross-tenant access settings or one organisation's, without Microsoft's own fields."""
    trust = policy.get("inboundTrust")
    consent = policy.get("automaticUserConsentSettings")
    return {
        **{key: _side(policy.get(key)) for key in B2B_SETTINGS},
        "trust": {k: trust.get(k) is True for k in TRUST} if isinstance(trust, dict) else None,
        "consentInbound": consent.get("inboundAllowed") if isinstance(consent, dict) else None,
        "consentOutbound": consent.get("outboundAllowed") if isinstance(consent, dict) else None,
        "serviceProvider": policy.get("isServiceProvider") is True,
        "multitenant": policy.get("isInMultiTenantOrganization") is True,
    }
