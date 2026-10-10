"""Tenant profile: organization details, subscriptions and licence capabilities.

Capabilities tell later collectors and reports what the tenant can provide,
e.g. sign-in activity and Conditional Access need Entra ID P1. Reports show
"not available on this tenant's licence" instead of failing.

``subscribedSkus`` gives licence counts per product; ``/directory/subscriptions``
gives each subscription behind them with its status, trial flag and the date it
next renews, lapses or is deleted.
"""

from __future__ import annotations

from .. import store
from ..graph import GraphError
from .base import Collector, RunContext, register

SUBSCRIPTION_FIELDS = (
    "commerceSubscriptionId",
    "skuId",
    "skuPartNumber",
    "status",
    "isTrial",
    "totalLicenses",
    "createdDateTime",
    "nextLifecycleDateTime",
)

# Service plan name -> capability flag.
CAPABILITY_PLANS = {
    "AAD_PREMIUM": "entra_p1",
    "AAD_PREMIUM_P2": "entra_p2",
    "INTUNE_A": "intune",
    "ATP_ENTERPRISE": "defender_o365_p1",
    "THREAT_INTELLIGENCE": "defender_o365_p2",
    "EXCHANGE_S_STANDARD": "exchange",
    "EXCHANGE_S_ENTERPRISE": "exchange",
    "EXCHANGE_S_DESKLESS": "exchange",
    "EXCHANGE_S_ESSENTIALS": "exchange",
    "SHAREPOINTSTANDARD": "sharepoint",
    "SHAREPOINTENTERPRISE": "sharepoint",
}
IMPLIED = {"entra_p2": "entra_p1", "defender_o365_p2": "defender_o365_p1"}


def capabilities_from_skus(skus: list[dict]) -> dict:
    caps = {flag: False for flag in set(CAPABILITY_PLANS.values())}
    for sku in skus:
        if sku.get("capabilityStatus") not in ("Enabled", "Warning"):
            continue
        if (sku.get("prepaidUnits") or {}).get("enabled", 0) <= 0:
            continue
        for plan in sku.get("servicePlans", []):
            flag = CAPABILITY_PLANS.get(plan.get("servicePlanName"))
            if flag and plan.get("provisioningStatus") in ("Success", "PendingActivation", "PendingProvisioning"):
                caps[flag] = True
    for higher, lower in IMPLIED.items():
        if caps.get(higher):
            caps[lower] = True
    return caps


@register
class TenantProfile(Collector):
    key = "tenant_profile"
    title = "Tenant profile and licences"
    description = "Organization details, verified domains, subscriptions with renewal dates and licence capabilities."
    permissions = ("Organization.Read.All", "Directory.Read.All")

    def collect(self, ctx: RunContext) -> None:
        orgs = ctx.graph.get(
            "/organization",
            params={
                "$select": "id,displayName,verifiedDomains,countryLetterCode,createdDateTime,onPremisesSyncEnabled",
            },
        ).get("value", [])
        ctx.objects("organization", [(o["id"], o) for o in orgs])

        skus = ctx.paged(ctx.graph.get_all("/subscribedSkus"))
        ctx.objects("subscribed_sku", [(s["skuId"], s) for s in skus])

        caps = capabilities_from_skus(skus)
        ctx.objects("capabilities", [("capabilities", caps)])
        ctx.capabilities.update(caps)
        store.set_capabilities(ctx.conn, ctx.tenant["id"], caps)

        enabled = sum((s.get("prepaidUnits") or {}).get("enabled", 0) for s in skus if s.get("appliesTo") == "User")
        consumed = sum(s.get("consumedUnits", 0) for s in skus if s.get("appliesTo") == "User")
        ctx.metric("licences.purchased", enabled)
        ctx.metric("licences.assigned", consumed)
        ctx.metric("licences.unassigned", max(enabled - consumed, 0))
        ctx.log("Capabilities: " + (", ".join(sorted(k for k, v in caps.items() if v)) or "none"))
        self._subscriptions(ctx)

    @staticmethod
    def _subscriptions(ctx: RunContext) -> None:
        try:
            subs = ctx.paged(ctx.graph.get_all("/directory/subscriptions"))
        except GraphError as exc:
            ctx.warn(f"Subscription renewal dates weren't readable: {exc.message}")
            ctx.objects("subscription_status", [("subscriptions", {"available": False})])
            return
        ctx.objects(
            "company_subscription", [(s["id"], {k: s.get(k) for k in SUBSCRIPTION_FIELDS}) for s in subs if s.get("id")]
        )
        ctx.objects("subscription_status", [("subscriptions", {"available": True})])
        ctx.metric("licences.trials", sum(1 for s in subs if s.get("isTrial") and s.get("status") == "Enabled"))
