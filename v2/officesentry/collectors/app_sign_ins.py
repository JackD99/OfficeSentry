"""App sign-ins: when each enterprise app last signed in, from Entra ID's service principal sign-in activity report.

One row per app that has signed in (Microsoft keeps only the latest sign-in of each kind), keyed by app ID so
the Unused apps report can match it to the service principals Apps and consents stores. An app with no row
has no sign-in on record. Whether the report was readable is ``app_sign_in_status``: not ``sign_in_status``,
which is the Sign-ins collector's detail that the daily history leaves out, so past days can read it too.
"""

from __future__ import annotations

from ..graph import LicenseRequired, PermissionMissing
from .base import Collector, RunContext, register

STATUS = "app_sign_in_status"
# Graph's names for the four kinds of sign-in, and ours.
KINDS = {
    "delegatedClientSignInActivity": "delegatedClient",  # signed a person in
    "applicationAuthenticationClientSignInActivity": "appClient",  # signed in as itself, nobody present
    "delegatedResourceSignInActivity": "delegatedResource",  # was called as an API for a person
    "applicationAuthenticationResourceSignInActivity": "appResource",  # was called as an API by another app
}


def _when(activity) -> str | None:
    value = activity.get("lastSignInDateTime") if isinstance(activity, dict) else None
    return value if isinstance(value, str) and value else None


def summarise(row: dict) -> dict:
    out = {"last": _when(row.get("lastSignInActivity"))}
    out.update({ours: _when(row.get(theirs)) for theirs, ours in KINDS.items()})
    return out


@register
class AppSignIns(Collector):
    key = "app_sign_ins"
    title = "App sign-ins"
    description = "When each enterprise app last signed in, for a person or on its own (needs Entra ID P1)."
    permissions = ("AuditLog.Read.All",)

    def collect(self, ctx: RunContext) -> None:
        try:
            rows = ctx.paged(ctx.graph.get_all("/reports/servicePrincipalSignInActivities", version="beta"))
        except (LicenseRequired, PermissionMissing) as exc:
            reason = "licence" if isinstance(exc, LicenseRequired) else "permission"
            ctx.warn(f"App sign-in activity wasn't readable: {exc.message}")
            ctx.objects(STATUS, [("status", {"available": False, "reason": reason})])
            return
        apps = {r["appId"]: summarise(r) for r in rows if isinstance(r.get("appId"), str) and r["appId"]}
        ctx.objects("app_activity", list(apps.items()))
        ctx.objects(STATUS, [("status", {"available": True, "apps": len(apps)})])
        ctx.metric("apps.with_sign_ins", len(apps))
