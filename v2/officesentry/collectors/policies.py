"""Tenant-wide sharing and consent settings: who can invite guests, consent to apps, share files."""

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
        "Guest invitations, user consent to apps, default user permissions, admin consent requests, and "
        "SharePoint external sharing."
    )
    permissions = ("Policy.Read.All", "SharePointTenantSettings.Read.All")

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

        if ctx.capabilities.get("sharepoint") is False:
            ctx.log("Tenant has no SharePoint licence; skipping sharing settings")
            return
        try:
            sp = ctx.graph.get("/admin/sharepoint/settings")
        except GraphError as exc:
            ctx.warn(f"SharePoint sharing settings unavailable: {exc.message}")
            return
        ctx.objects("sharepoint_settings", [("sharepoint", {f: sp.get(f) for f in SHAREPOINT_FIELDS})])
