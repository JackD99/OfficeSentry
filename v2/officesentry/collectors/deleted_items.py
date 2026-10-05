"""Deleted users and groups: what is in the Entra ID recycle bin and how long each can still be restored.

Microsoft keeps a deleted user or Microsoft 365 group for 30 days from its deletion, then removes it for good.
Reads ``/directory/deletedItems`` only (Directory.Read.All). Restoring is done in the Entra or Microsoft 365
admin centre; Office Sentry never restores or purges anything.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from ..devices import parse_ts
from ..graph import GraphError
from .base import Collector, RunContext, register

RETENTION_DAYS = 30
EXPIRING_DAYS = 7
USER_FIELDS = ("id,displayName,userPrincipalName,mail,userType,deletedDateTime,assignedLicenses,accountEnabled,"
               "jobTitle,department")
GROUP_FIELDS = "id,displayName,mail,groupTypes,deletedDateTime,mailEnabled,securityEnabled,resourceProvisioningOptions"


def purge_date(deleted: str | None) -> datetime | None:
    """When Microsoft removes a deleted object for good: 30 days after it was deleted."""
    when = parse_ts(deleted)
    return when + timedelta(days=RETENTION_DAYS) if when else None


def _user(u: dict) -> dict:
    return {
        "displayName": u.get("displayName"),
        "userPrincipalName": u.get("userPrincipalName"),
        "mail": u.get("mail"),
        "userType": u.get("userType"),
        "deletedDateTime": u.get("deletedDateTime"),
        "accountEnabled": u.get("accountEnabled"),
        "jobTitle": u.get("jobTitle"),
        "department": u.get("department"),
        "licenceSkuIds": [lic.get("skuId") for lic in u.get("assignedLicenses") or []
                          if isinstance(lic, dict) and isinstance(lic.get("skuId"), str)],
    }


def _group(g: dict) -> dict:
    types = g.get("groupTypes") or []
    return {
        "displayName": g.get("displayName"),
        "mail": g.get("mail"),
        "deletedDateTime": g.get("deletedDateTime"),
        "microsoft365": "Unified" in types,
        "isTeam": "Team" in (g.get("resourceProvisioningOptions") or []),
        "mailEnabled": g.get("mailEnabled"),
        "securityEnabled": g.get("securityEnabled"),
    }


@register
class DeletedItems(Collector):
    key = "deleted_items"
    title = "Deleted users and groups"
    description = ("Users and Microsoft 365 groups in the Entra ID recycle bin, and how many days are left to "
                   "restore each before Microsoft deletes it for good.")
    permissions = ("Directory.Read.All",)

    def collect(self, ctx: RunContext) -> None:
        now = datetime.now(UTC)
        status = {"users": False, "groups": False}
        found: dict[str, list[dict]] = {}
        for kind, path, fields, summary in (
                ("users", "/directory/deletedItems/microsoft.graph.user", USER_FIELDS, _user),
                ("groups", "/directory/deletedItems/microsoft.graph.group", GROUP_FIELDS, _group)):
            try:
                items = ctx.paged(ctx.graph.get_all(path, params={"$select": fields, "$top": "999"}))
            except GraphError as exc:
                ctx.warn(f"Deleted {kind} weren't readable: {exc.message}")
                continue
            status[kind] = True
            rows = [(i["id"], summary(i)) for i in items if i.get("id")]
            found[kind] = [r for _, r in rows]
            ctx.objects("deleted_user" if kind == "users" else "deleted_group", rows)
        ctx.objects("deleted_status", [("deleted_status", status)])

        soon = now + timedelta(days=EXPIRING_DAYS)
        if status["users"]:
            ctx.metric("deleted.users", len(found["users"]))
        if status["groups"]:
            ctx.metric("deleted.groups", len(found["groups"]))
        if status["users"] or status["groups"]:
            every = found.get("users", []) + found.get("groups", [])
            ctx.metric("deleted.expiring_7d", sum(1 for r in every
                                                  if (p := purge_date(r["deletedDateTime"])) and p <= soon))
