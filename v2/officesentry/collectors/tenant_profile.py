"""Tenant profile: organization details, subscriptions and licence capabilities.

Capabilities tell later collectors and reports what the tenant can provide,
e.g. sign-in activity and Conditional Access need Entra ID P1. Reports show
"not available on this tenant's licence" instead of failing; which licence
provides what, and what each one unlocks, is in ``officesentry.capabilities``.

``subscribedSkus`` gives licence counts per product; ``/directory/subscriptions``
gives each subscription behind them with its status, trial flag and the date it
next renews, lapses or is deleted.
"""

from __future__ import annotations

from .. import store
from ..capabilities import from_skus
from ..graph import GraphError
from ..licences import licence_summary
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

        caps = from_skus(skus)
        ctx.objects("capabilities", [("capabilities", caps)])
        ctx.capabilities.update(caps)
        store.set_capabilities(ctx.conn, ctx.tenant["id"], caps)

        enabled = sum((s.get("prepaidUnits") or {}).get("enabled", 0) for s in skus if s.get("appliesTo") == "User")
        consumed = sum(s.get("consumedUnits", 0) for s in skus if s.get("appliesTo") == "User")
        ctx.metric("licences.purchased", enabled)
        ctx.metric("licences.assigned", consumed)
        ctx.metric("licences.unassigned", max(enabled - consumed, 0))
        ctx.log("Capabilities: " + (", ".join(sorted(k for k, v in caps.items() if v)) or "none"))
        subs = self._subscriptions(ctx)
        # Unassigned as Licence waste counts them: paid licences only, not free allocations (Power Automate
        # Free's 10,000) or trial seats. What changed tracks this one.
        lines = licence_summary(skus, None, {}, subscriptions=subs).lines
        ctx.metric("licences.unassigned_paid", sum(line.unassigned for line in lines if not line.free))

    @staticmethod
    def _subscriptions(ctx: RunContext) -> list[dict] | None:
        try:
            subs = ctx.paged(ctx.graph.get_all("/directory/subscriptions"))
        except GraphError as exc:
            ctx.warn(f"Subscription renewal dates weren't readable: {exc.message}")
            ctx.objects("subscription_status", [("subscriptions", {"available": False})])
            return None
        ctx.objects(
            "company_subscription", [(s["id"], {k: s.get(k) for k in SUBSCRIPTION_FIELDS}) for s in subs if s.get("id")]
        )
        ctx.objects("subscription_status", [("subscriptions", {"available": True})])
        ctx.metric("licences.trials", sum(1 for s in subs if s.get("isTrial") and s.get("status") == "Enabled"))
        return subs
