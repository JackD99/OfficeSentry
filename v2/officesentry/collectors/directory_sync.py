"""Directory sync: whether accounts come from on-premises AD, when sync last ran, and how each domain signs in.

Reads the organization's sync timestamps and the domain list only. The Entra
Connect feature settings (/directory/onPremisesSynchronization) need a
permission Office Sentry doesn't request, so they aren't read.
"""

from __future__ import annotations

from datetime import UTC, datetime

from ..graph import GraphError
from ..hybrid import hours_since
from .base import Collector, RunContext, register

ORG_FIELDS = "id,onPremisesSyncEnabled,onPremisesLastSyncDateTime"
DOMAIN_FIELDS = "id,authenticationType,isVerified,isDefault,supportedServices"


@register
class DirectorySync(Collector):
    key = "directory_sync"
    title = "Directory sync"
    description = ("Whether accounts are synced from on-premises Active Directory, when directory and password "
                   "hash sync last ran, and how each domain signs in.")
    permissions = ("Organization.Read.All", "Directory.Read.All")

    def collect(self, ctx: RunContext) -> None:
        now = datetime.now(UTC)
        orgs = ctx.graph.get("/organization", params={"$select": ORG_FIELDS}).get("value", [])
        org = orgs[0] if orgs else {}
        raw = org.get("onPremisesSyncEnabled")
        status = {
            "onPremisesSyncEnabled": raw,  # None: never synced; False: sync was turned off after being used
            "enabled": raw is True,
            "lastSyncDateTime": org.get("onPremisesLastSyncDateTime"),
            "lastPasswordSyncDateTime": None,
            "passwordSyncChecked": False,
        }
        if status["enabled"]:
            self._password_sync(ctx, status)
        ctx.objects("sync_status", [("sync_status", status)])

        domains = ctx.paged(ctx.graph.get_all("/domains", params={"$select": DOMAIN_FIELDS}))
        ctx.objects("domain", [(d["id"], {k: d.get(k) for k in DOMAIN_FIELDS.split(",")[1:]}) for d in domains])

        ctx.metric("sync.enabled", int(status["enabled"]))
        ctx.metric("sync.federated_domains", sum(d.get("authenticationType") == "Federated" for d in domains))
        if status["enabled"]:
            last = hours_since(status["lastSyncDateTime"], now)
            if last is not None:
                ctx.metric("sync.hours_since_last_sync", round(last, 1))
            password = hours_since(status["lastPasswordSyncDateTime"], now)
            if password is not None:
                ctx.metric("sync.hours_since_password_sync", round(password, 1))

    @staticmethod
    def _password_sync(ctx: RunContext, status: dict) -> None:
        # onPremisesLastPasswordSyncDateTime is beta-only (v1.0 silently drops it from $select),
        # so this one call uses beta.
        try:
            orgs = ctx.graph.get("/organization", version="beta", params={
                "$select": "id,onPremisesLastPasswordSyncDateTime"}).get("value", [])
        except GraphError as exc:
            ctx.warn(f"The last password hash sync time wasn't readable: {exc.message}")
            return
        status["lastPasswordSyncDateTime"] = (orgs[0] if orgs else {}).get("onPremisesLastPasswordSyncDateTime")
        status["passwordSyncChecked"] = True
