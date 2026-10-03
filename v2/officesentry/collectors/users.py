"""Users: every member and guest account with licence and sign-in activity."""

from __future__ import annotations

from ..graph import LicenseRequired
from .base import Collector, RunContext, register

BASE_FIELDS = [
    "id", "displayName", "userPrincipalName", "mail", "userType", "accountEnabled",
    "createdDateTime", "assignedLicenses", "lastPasswordChangeDateTime",
    "onPremisesSyncEnabled", "passwordPolicies", "externalUserState",
]


def summarise(user: dict, activity_available: bool) -> dict:
    """``activity_available``: sign-in activity was requested successfully for the
    tenant. Graph omits ``signInActivity`` for users who never signed in."""
    activity = (user.get("signInActivity") or {}) if activity_available else {}
    return {
        "displayName": user.get("displayName"),
        "userPrincipalName": user.get("userPrincipalName"),
        "mail": user.get("mail"),
        "userType": user.get("userType") or "Member",
        "accountEnabled": bool(user.get("accountEnabled")),
        "createdDateTime": user.get("createdDateTime"),
        "licensed": bool(user.get("assignedLicenses")),
        "skuIds": [lic.get("skuId") for lic in user.get("assignedLicenses") or []],
        "lastPasswordChangeDateTime": user.get("lastPasswordChangeDateTime"),
        "passwordNeverExpires": "DisablePasswordExpiration" in (user.get("passwordPolicies") or ""),
        "onPremisesSyncEnabled": bool(user.get("onPremisesSyncEnabled")),
        "externalUserState": user.get("externalUserState"),
        # With signInActivityAvailable False, None means "unknown", not "never signed in".
        "lastSignInDateTime": activity.get("lastSignInDateTime"),
        "lastNonInteractiveSignInDateTime": activity.get("lastNonInteractiveSignInDateTime"),
        "lastSuccessfulSignInDateTime": activity.get("lastSuccessfulSignInDateTime"),
        "signInActivityAvailable": activity_available,
    }


@register
class Users(Collector):
    key = "users"
    title = "Users"
    description = "Members and guests with licences, password age and last sign-in (sign-in needs Entra ID P1)."
    permissions = ("User.Read.All", "AuditLog.Read.All")

    def collect(self, ctx: RunContext) -> None:
        want_activity = ctx.capabilities.get("entra_p1", True)
        users = None
        activity = False
        if want_activity:
            try:
                users = ctx.paged(ctx.graph.get_all("/users", params={
                    "$select": ",".join(BASE_FIELDS + ["signInActivity"]), "$top": "999",
                }))
                activity = True
            except LicenseRequired:
                ctx.warn("Sign-in activity needs Entra ID P1; collected users without it")
        if users is None:
            users = ctx.paged(ctx.graph.get_all("/users", params={
                "$select": ",".join(BASE_FIELDS), "$top": "999",
            }))

        rows = [(u["id"], summarise(u, activity)) for u in users]
        ctx.objects("user", rows)

        data = [r for _, r in rows]
        members = [u for u in data if u["userType"] == "Member"]
        guests = [u for u in data if u["userType"] == "Guest"]
        ctx.metric("users.total", len(data))
        ctx.metric("users.members", len(members))
        ctx.metric("users.guests", len(guests))
        ctx.metric("users.enabled", sum(u["accountEnabled"] for u in data))
        ctx.metric("users.licensed", sum(u["licensed"] for u in data))
        ctx.metric("users.licensed_disabled", sum(u["licensed"] and not u["accountEnabled"] for u in data))
