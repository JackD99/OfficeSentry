"""Per-user MFA: the legacy "enforced/enabled" switch on each account.

Tenants without Entra ID P1 can't use Conditional Access, so MFA is either
Security Defaults or this per-user setting. Tenants with P1 are skipped: they
enforce MFA with Conditional Access, and reading the setting costs one Graph
call per user.
"""

from __future__ import annotations

from .. import store
from ..graph import GraphError, PermissionMissing
from .base import Collector, RunContext, register

USER_LIMIT = 2000


@register
class PerUserMfa(Collector):
    key = "per_user_mfa"
    title = "Per-user MFA"
    description = "The per-user MFA setting on each member, for tenants without Entra ID P1."
    permissions = ("Policy.Read.All",)

    def collect(self, ctx: RunContext) -> None:
        if ctx.capabilities.get("entra_p1", True):
            ctx.log("Tenant has Entra ID P1, so MFA is enforced with Conditional Access; per-user MFA not read")
            ctx.objects("per_user_mfa_status", [("status", {"checked": False, "reason": "entra_p1"})])
            return
        users_run = store.latest_run(ctx.conn, ctx.tenant["id"], "users")
        if users_run:
            users = store.run_objects(ctx.conn, users_run["id"], "user")
        else:
            users = [
                dict(u, _id=u["id"])
                for u in ctx.paged(
                    ctx.graph.get_all(
                        "/users", params={"$select": "id,userPrincipalName,userType,accountEnabled", "$top": "999"}
                    )
                )
            ]
        members = [u for u in users if u.get("accountEnabled") and (u.get("userType") or "Member") == "Member"]
        if len(members) > USER_LIMIT:
            ctx.warn(f"Checked the first {USER_LIMIT} of {len(members)} enabled members")
            members = members[:USER_LIMIT]

        rows = []
        for u in members:
            try:
                state = ctx.graph.get(f"/users/{u['_id']}/authentication/requirements", version="beta").get(
                    "perUserMfaState"
                )
            except PermissionMissing:
                ctx.warn("Per-user MFA needs the Policy.Read.All permission; re-consent the app")
                return
            except GraphError as exc:
                ctx.log(f"Could not read per-user MFA for {u.get('userPrincipalName')}: {exc.message}", "warning")
                continue
            rows.append((u["_id"], {"userPrincipalName": u.get("userPrincipalName"), "state": state or "disabled"}))
        ctx.objects("per_user_mfa", rows)
        ctx.objects("per_user_mfa_status", [("status", {"checked": True})])
        ctx.metric("mfa.per_user_enforced", sum(r["state"] in ("enforced", "enabled") for _, r in rows))
