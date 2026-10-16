"""Connection health: is Office Sentry's app set up properly in this tenant?

Reads the app's own service principal in the client tenant (all GET, with
Application.Read.All and RoleManagement.Read.Directory, which the app already
has): which of the permissions Office Sentry asks for (``entra.PERMISSIONS``: Graph's,
Exchange's and SharePoint's) the tenant has actually granted, and which
directory roles the app holds, Global Reader in particular. That a run gets
this far also proves Office Sentry can sign in.

Problems are warnings (the run is ``partial``); ``tenant_health`` turns the
stored result into plain-English fixes for the Manage tab and the client list.
Permissions from a batch the client hasn't approved yet (``permission_batch``)
aren't a problem: they're logged, and the What's new page asks for them.
Nothing here changes the tenant: missing consent or roles are fixed by the
client's admin, from the instructions shown.
"""

from __future__ import annotations

from .. import clouds, permission_batch, store
from ..entra import PERMISSIONS, label
from ..graph import PermissionMissing
from ..text import plural
from .base import Collector, RunContext, register

GLOBAL_READER = "f2ef992c-3afb-46b9-b7cf-a126ee74c451"  # built-in role template IDs are the same in every tenant
# Roles that also let Exchange answer, but give the app far more than reporting needs.
TOO_POWERFUL = {
    "62e90394-69f5-4237-9190-012177145e10": "Global Administrator",
    "29232cdf-9323-42fd-ade2-1d097af3e4de": "Exchange Administrator",
}
KNOWN_ROLES = {GLOBAL_READER: "Global Reader", **TOO_POWERFUL}


def _quote(value: str) -> str:
    return value.replace("'", "''")


@register
class ConnectionHealth(Collector):
    key = "connection"
    title = "Connection and permissions"
    description = "Whether the app's permissions are granted in this tenant and it holds the Global Reader role."
    permissions = ("Application.Read.All", "RoleManagement.Read.Directory")

    def collect(self, ctx: RunContext) -> None:
        result: dict = {
            "client_id": ctx.client_id,
            "app_found": None,
            "permissions_checked": False,
            "roles_checked": False,
            "granted": [],
            "missing": [],
            "other": [],
            "roles": [],
            "global_reader": None,
            "too_powerful": [],
        }
        self._check(ctx, result)
        ctx.objects("connection", [("connection", result)])
        if result["missing"]:
            ctx.metric("connection.permissions_missing", len(result["missing"]))

    def _check(self, ctx: RunContext, result: dict) -> None:
        if not ctx.client_id:
            ctx.warn("This client has no app connection, so its permissions weren't checked")
            return
        sps = (
            ctx.graph.get(
                "/servicePrincipals",
                params={
                    "$filter": f"appId eq '{_quote(ctx.client_id)}'",
                    "$select": "id,appId,displayName,accountEnabled",
                },
            ).get("value")
            or []
        )
        sp = next((s for s in sps if (s.get("appId") or "").lower() == ctx.client_id.lower()), None)
        result["app_found"] = sp is not None
        if sp is None:
            ctx.warn(
                "Office Sentry's app isn't in this tenant: admin consent hasn't been granted, or the app was "
                "deleted from Enterprise applications"
            )
            return
        result.update(
            app_name=sp.get("displayName"),
            app_enabled=sp.get("accountEnabled") is not False,
            service_principal_id=sp["id"],
        )
        if not result["app_enabled"]:
            ctx.warn("Office Sentry's app is disabled in this tenant's Enterprise applications")
        self._permissions(ctx, sp["id"], result)
        self._roles(ctx, sp["id"], result)

    def _permissions(self, ctx: RunContext, sp_id: str, result: dict) -> None:
        try:
            assignments = ctx.paged(ctx.graph.get_all(f"/servicePrincipals/{sp_id}/appRoleAssignments"))
            resources: dict[str, dict] = {}
            for a in assignments:
                rid = a.get("resourceId")
                if rid and rid not in resources:
                    resources[rid] = ctx.graph.get(f"/servicePrincipals/{rid}", params={"$select": "appId,appRoles"})
        except PermissionMissing as exc:
            ctx.warn(f"Couldn't read which permissions this tenant granted ({exc.code}); re-grant admin consent")
            return
        granted: set[str] = set()
        other: set[str] = set()
        for a in assignments:
            resource = resources.get(a.get("resourceId")) or {}
            names = {r.get("id"): r.get("value") for r in resource.get("appRoles") or []}
            name = names.get(a.get("appRoleId"))
            if not name:
                continue
            name = label(resource.get("appId") or "", name)
            if name in PERMISSIONS:
                granted.add(name)
            else:
                other.add(name)
        result.update(
            permissions_checked=True,
            granted=sorted(granted),
            other=sorted(other),
            # Not ones Microsoft doesn't offer in the tenant's cloud: the app can't ask for those there.
            missing=[p for p in PERMISSIONS if p not in granted and p not in clouds.of(ctx.tenant).no_permissions],
        )
        problem = permission_batch.missing_original(result)
        if problem:
            ctx.warn(
                f"{plural(len(problem), 'permission')} not granted in this tenant: "
                + ", ".join(problem)
                + ". Re-grant admin consent"
            )
        new = permission_batch.waiting(result)
        if new:
            ctx.log(
                f"Waiting for the client to approve {plural(len(new), 'new permission')}: "
                + ", ".join(new)
                + ". Send the approval link from the client's What's new page"
            )

    def _roles(self, ctx: RunContext, sp_id: str, result: dict) -> None:
        try:
            assignments = ctx.paged(
                ctx.graph.get_all(
                    "/roleManagement/directory/roleAssignments", params={"$filter": f"principalId eq '{_quote(sp_id)}'"}
                )
            )
            assignments = [a for a in assignments if a.get("principalId") == sp_id]
            names = dict(KNOWN_ROLES)
            if any(a.get("roleDefinitionId") not in names for a in assignments):
                definitions = ctx.paged(
                    ctx.graph.get_all(
                        "/roleManagement/directory/roleDefinitions",
                        params={"$select": "id,displayName,isBuiltIn,templateId"},
                    )
                )
                for d in definitions:
                    names.setdefault(d.get("id"), d.get("displayName"))
                    names.setdefault(d.get("templateId"), d.get("displayName"))
        except PermissionMissing as exc:
            ctx.warn(f"Couldn't read the app's admin roles in this tenant ({exc.code}); re-grant admin consent")
            return
        roles = sorted({names.get(a.get("roleDefinitionId")) or "Another admin role" for a in assignments})
        result.update(
            roles_checked=True,
            roles=roles,
            global_reader="Global Reader" in roles,
            too_powerful=[r for r in roles if r in TOO_POWERFUL.values()],
        )
        if not result["global_reader"]:
            ctx.warn("The app doesn't hold the Global Reader role, so mailbox data can't be collected")


def _last_result(ctx: RunContext) -> dict:
    run = store.latest_run(ctx.conn, ctx.tenant["id"], "connection")
    rows = store.run_objects(ctx.conn, run["id"], "connection") if run else []
    return rows[0] if rows else {}


def granted(ctx: RunContext, permission: str) -> bool | None:
    """Whether the tenant granted ``permission``, from the last connection check (None when not known)."""
    result = _last_result(ctx)
    if not result.get("permissions_checked"):
        return None
    return permission in (result.get("granted") or [])


def awaiting(ctx: RunContext, *permissions: str) -> list[str]:
    """Which of ``permissions`` (new ones, from a batch) the client is still to approve, by the last connection
    check. A collector skips the part that needs them and stores why (``permission_batch.waiting_text``)
    rather than failing; empty when they're granted or it isn't known yet, so the collector tries."""
    return permission_batch.awaiting(_last_result(ctx), *permissions)
