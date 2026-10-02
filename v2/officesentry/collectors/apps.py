"""Apps and consents: third-party apps, what they were granted, and app credential expiry."""

from __future__ import annotations

from ..graph import GraphError
from .base import Collector, RunContext, register

# Tenants that own Microsoft's first-party apps.
MICROSOFT_TENANTS = {"f8cdef31-a31e-4b4a-93e4-5f571e91255a", "72f988bf-86f1-41af-91ab-2d7cd011db47"}
APP_LIMIT = 1000


def _credentials(app: dict) -> list[dict]:
    out = []
    for kind, key in (("secret", "passwordCredentials"), ("certificate", "keyCredentials")):
        for c in app.get(key) or []:
            out.append({"type": kind, "displayName": c.get("displayName"), "endDateTime": c.get("endDateTime")})
    return out


@register
class AppConsents(Collector):
    key = "app_consents"
    title = "Apps and consents"
    description = "Third-party apps, the permissions granted to them, and app secrets or certificates expiring."
    permissions = ("Application.Read.All", "Directory.Read.All")

    def collect(self, ctx: RunContext) -> None:
        sps = ctx.paged(ctx.graph.get_all("/servicePrincipals", params={
            "$select": "id,appId,displayName,appOwnerOrganizationId,publisherName,verifiedPublisher,"
                       "servicePrincipalType,accountEnabled,homepage", "$top": "999"}))
        by_id = {}
        for sp in sps:
            by_id[sp["id"]] = {
                "appId": sp.get("appId"), "displayName": sp.get("displayName"),
                "publisherName": sp.get("publisherName"),
                "verifiedPublisher": (sp.get("verifiedPublisher") or {}).get("displayName"),
                "ownerTenant": sp.get("appOwnerOrganizationId"),
                "microsoft": sp.get("appOwnerOrganizationId") in MICROSOFT_TENANTS,
                "ownTenant": sp.get("appOwnerOrganizationId") == ctx.tenant.get("tenant_ref"),
                "type": sp.get("servicePrincipalType"), "accountEnabled": sp.get("accountEnabled"),
            }

        grants = ctx.paged(ctx.graph.get_all("/oauth2PermissionGrants", params={"$top": "999"}))
        ctx.objects("delegated_grant", [(g["id"], {
            "clientId": g.get("clientId"), "consentType": g.get("consentType"), "principalId": g.get("principalId"),
            "resourceId": g.get("resourceId"),
            "resource": (by_id.get(g.get("resourceId")) or {}).get("displayName"),
            "scopes": sorted((g.get("scope") or "").split()),
        }) for g in grants])

        third_party = [sid for sid, sp in by_id.items() if not sp["microsoft"] and sp["type"] == "Application"]
        if len(third_party) > APP_LIMIT:
            ctx.warn(f"Read application permissions for the first {APP_LIMIT} of {len(third_party)} apps")
            third_party = third_party[:APP_LIMIT]
        role_names: dict[str, dict[str, str]] = {}
        rows = []
        for sid in third_party:
            try:
                assignments = ctx.paged(ctx.graph.get_all(f"/servicePrincipals/{sid}/appRoleAssignments"))
            except GraphError as exc:
                ctx.log(f"Could not read permissions for {by_id[sid]['displayName']}: {exc.message}", "warning")
                continue
            for a in assignments:
                rid = a.get("resourceId")
                if rid not in role_names:
                    role_names[rid] = self._app_roles(ctx, rid)
                rows.append((a["id"], {
                    "clientId": sid, "resourceId": rid, "resource": a.get("resourceDisplayName"),
                    "permission": role_names[rid].get(a.get("appRoleId"), a.get("appRoleId")),
                    "createdDateTime": a.get("createdDateTime"),
                }))
        ctx.objects("app_permission", rows)
        ctx.objects("service_principal", list(by_id.items()))

        apps = ctx.paged(ctx.graph.get_all("/applications", params={
            "$select": "id,appId,displayName,createdDateTime,passwordCredentials,keyCredentials", "$top": "999"}))
        ctx.objects("application", [(a["id"], {
            "appId": a.get("appId"), "displayName": a.get("displayName"),
            "createdDateTime": a.get("createdDateTime"), "credentials": _credentials(a),
        }) for a in apps])

        ctx.metric("apps.third_party", sum(1 for sp in by_id.values() if not sp["microsoft"]
                                           and sp["type"] == "Application"))
        ctx.metric("apps.application_permissions", len(rows))
        ctx.metric("apps.tenant_wide_delegated_grants", sum(g.get("consentType") == "AllPrincipals" for g in grants))

    @staticmethod
    def _app_roles(ctx: RunContext, resource_id: str) -> dict[str, str]:
        try:
            sp = ctx.graph.get(f"/servicePrincipals/{resource_id}", params={"$select": "appRoles"})
        except GraphError:
            return {}
        return {r["id"]: r.get("value") or r.get("displayName") for r in sp.get("appRoles") or []}
