"""Users: every member and guest account with licence and sign-in activity."""

from __future__ import annotations

from ..graph import LicenseRequired, PermissionMissing
from .base import Collector, RunContext, register

# Microsoft's largest page when signInActivity is selected (999 without it). That listing is limited to
# 10 requests a minute for the whole app, so the shared limiter paces it (endpoints.LIMITS).
SIGN_IN_ACTIVITY_PAGE = "500"

BASE_FIELDS = [
    "id",
    "displayName",
    "userPrincipalName",
    "mail",
    "userType",
    "accountEnabled",
    "createdDateTime",
    "assignedLicenses",
    "lastPasswordChangeDateTime",
    "onPremisesSyncEnabled",
    "passwordPolicies",
    "externalUserState",
    "licenseAssignmentStates",
    "usageLocation",  # how each licence is assigned (directly or by a group)
]


def licence_states(user: dict) -> list[dict] | None:
    """Each licence assignment: the SKU, the group it comes from (None when assigned directly), its state
    (Active, ActiveWithError, Disabled, Error) and Microsoft's error code. None when Graph didn't return them."""
    states = user.get("licenseAssignmentStates")
    if not isinstance(states, list):
        return None
    return [
        {
            "skuId": s.get("skuId"),
            "group": s.get("assignedByGroup") or None,
            "state": s.get("state"),
            "error": s.get("error") if s.get("error") not in (None, "", "None") else None,
            "updated": s.get("lastUpdatedDateTime"),
        }
        for s in states
        if isinstance(s, dict) and s.get("skuId")
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
        "usageLocation": user.get("usageLocation"),
        "licenceStates": licence_states(user),
        # With signInActivityAvailable False, None means "unknown", not "never signed in".
        "lastSignInDateTime": activity.get("lastSignInDateTime"),
        "lastNonInteractiveSignInDateTime": activity.get("lastNonInteractiveSignInDateTime"),
        "lastSuccessfulSignInDateTime": activity.get("lastSuccessfulSignInDateTime"),
        # Microsoft sent the successful time (null when none), so the other two, which failed attempts also
        # move, aren't needed to tell when the account was last used (licences.last_sign_in).
        "successfulSignInKnown": "lastSuccessfulSignInDateTime" in activity,
        "signInActivityAvailable": activity_available,
    }


@register
class Users(Collector):
    key = "users"
    title = "Users"
    description = (
        "Members and guests with licences (and whether each comes directly or from a group), password age "
        "and last sign-in (sign-in needs Entra ID P1)."
    )
    permissions = ("User.Read.All", "AuditLog.Read.All")
    more_with = ("entra_p1",)

    def collect(self, ctx: RunContext) -> None:
        want_activity = ctx.capabilities.get("entra_p1") is not False
        users = None
        activity = False
        if want_activity:
            try:
                users = ctx.paged(
                    ctx.graph.get_all(
                        "/users",
                        params={
                            "$select": ",".join(BASE_FIELDS + ["signInActivity"]),
                            "$top": SIGN_IN_ACTIVITY_PAGE,
                        },
                    )
                )
                activity = True
            except LicenseRequired:
                ctx.warn("Sign-in activity needs Entra ID P1; collected users without it")
            except PermissionMissing as exc:
                # AuditLog.Read.All not consented (an older consent, or one granted outside Office Sentry):
                # the users themselves need only User.Read.All, so they're still collected.
                ctx.warn(
                    f"Sign-in activity needs the AuditLog.Read.All permission ({exc.message}); collected users "
                    "without it"
                )
        if users is None:
            users = ctx.paged(
                ctx.graph.get_all(
                    "/users",
                    params={
                        "$select": ",".join(BASE_FIELDS),
                        "$top": "999",
                    },
                )
            )

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
        ctx.metric(
            "licences.assignment_errors",
            sum(1 for u in data for s in u["licenceStates"] or [] if s["state"] in ("Error", "ActiveWithError")),
        )
