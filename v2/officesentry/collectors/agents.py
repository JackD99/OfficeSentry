"""AI agents: the agent identities in the tenant's Entra ID (Microsoft Entra Agent ID), and the application
permissions each was given.

Reads ``/servicePrincipals/microsoft.graph.agentIdentity``, which needs AgentIdentity.Read.All, added in Office
Sentry 2.6 (``permission_batch``): until the client approves it, nothing is read and the status says waiting, so
the run doesn't fail. Each agent's application permissions come from its app role assignments
(Application.Read.All, asked for from the start). Its admin roles and delegated consents are already stored by
Admin roles and Apps and consents, so the report joins those rather than reading them again. Sponsors aren't read:
Microsoft only gives them to an app that may also change them.
"""

from __future__ import annotations

from .. import clouds
from ..graph import GraphError, PermissionMissing
from ..text import plural
from .apps import AppConsents
from .base import Collector, RunContext, register
from .connection import awaiting

AGENTS = "AgentIdentity.Read.All"
PATH = "/servicePrincipals/microsoft.graph.agentIdentity"
FIELDS = (
    "id,appId,displayName,accountEnabled,agentIdentityBlueprintId,createdByAppId,createdDateTime,"
    "disabledByMicrosoftStatus,tags"
)
PAGE = 100  # Microsoft's largest page of agent identities
MAX_PAGES = 10
GONE = (400, 404)  # Microsoft answers these where it doesn't offer agent identities


def _text(value) -> str | None:
    return value if isinstance(value, str) and value else None


def agent(a: dict) -> dict:
    tags = a.get("tags")
    return {
        "displayName": _text(a.get("displayName")),
        "appId": _text(a.get("appId")),
        "enabled": a.get("accountEnabled") is not False,
        "blueprint": _text(a.get("agentIdentityBlueprintId")),
        "createdBy": _text(a.get("createdByAppId")),
        "created": _text(a.get("createdDateTime")),
        "disabledByMicrosoft": _text(a.get("disabledByMicrosoftStatus")),
        "tags": [t for t in tags if isinstance(t, str)] if isinstance(tags, list) else [],
    }


@register
class Agents(Collector):
    key = "agents"
    title = "AI agents"
    description = (
        "The AI agents with their own identity in Entra ID (Microsoft Entra Agent ID), and the application "
        "permissions each was given."
    )
    permissions = (AGENTS, "Application.Read.All")

    def collect(self, ctx: RunContext) -> None:
        if awaiting(ctx, AGENTS):
            ctx.log(f"Waiting for the client to approve {AGENTS}; AI agents are skipped")
            self._status(ctx, "waiting")
            return
        try:
            found = ctx.paged(
                ctx.graph.get_all(PATH, params={"$select": FIELDS, "$top": str(PAGE)}, max_pages=MAX_PAGES)
            )
        except PermissionMissing as exc:  # a new permission (BATCH_OF), so a refusal means not approved yet
            if AGENTS in clouds.of(ctx.tenant).no_permissions:  # or, in a cloud without it, never asked for
                ctx.log(f"AI agents skipped: not granted, and this cloud may not offer {AGENTS} ({exc.message})")
                self._status(ctx, "unavailable")
            else:
                ctx.log(f"AI agents skipped: {AGENTS} isn't approved yet ({exc.message})")
                self._status(ctx, "waiting")
            return
        except GraphError as exc:
            if exc.status in GONE:
                ctx.log(f"Microsoft doesn't offer agent identities for this tenant ({exc.message})")
                self._status(ctx, "unavailable")
            else:
                ctx.warn(f"AI agents weren't readable: {exc.message}")
                self._status(ctx, "failed")
            return
        agents = [(a["id"], agent(a)) for a in found if isinstance(a, dict) and _text(a.get("id"))]
        ctx.objects("agent", agents)

        role_names: dict[str, dict[str, str]] = {}
        rows, unread = [], 0
        for sid, a in agents:
            try:
                assignments = ctx.paged(ctx.graph.get_all(f"/servicePrincipals/{sid}/appRoleAssignments"))
            except GraphError as exc:
                unread += 1
                ctx.log(f"Could not read permissions for {a['displayName'] or sid}: {exc.message}", "warning")
                continue
            for g in assignments:
                if not isinstance(g, dict) or not _text(g.get("id")):
                    continue
                rid = g.get("resourceId")
                if rid not in role_names:
                    role_names[rid] = AppConsents._app_roles(ctx, rid)
                rows.append(
                    (
                        g["id"],
                        {
                            "agentId": sid,
                            "resourceId": rid,
                            "resource": _text(g.get("resourceDisplayName")),
                            "permission": role_names[rid].get(g.get("appRoleId"), g.get("appRoleId")),
                            "createdDateTime": _text(g.get("createdDateTime")),
                        },
                    )
                )
        ctx.objects("agent_permission", rows)
        ctx.metric("agents.count", len(agents))
        ctx.metric("agents.with_app_permissions", len({r["agentId"] for _, r in rows}))
        ctx.log(f"{plural(len(agents), 'AI agent')} with their own identity")
        self._status(ctx, "read", unread=unread)

    @staticmethod
    def _status(ctx: RunContext, state: str, **extra) -> None:
        ctx.objects("agents_status", [("status", {"state": state, **extra})])
