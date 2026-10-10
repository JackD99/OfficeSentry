"""Risky users: accounts Entra ID Protection thinks someone else may be using.

Reads ``/identityProtection/riskyUsers`` (needs Entra ID P2 and
IdentityRiskyUser.Read.All). Keeps the accounts at risk now (at risk or
confirmed compromised) and those remediated, dismissed or confirmed safe in the
last ``RESOLVED_DAYS`` days; older resolved entries aren't stored.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from ..graph import LicenseRequired, PermissionMissing
from ..identity_protection import OPEN_RISK, RESOLVED_DAYS, RESOLVED_STATES
from ..text import plural
from .base import Collector, RunContext, register

PAGE_SIZE = 500
MAX_PAGES = 40  # 20,000 risky users
FIELDS = (
    "id,userPrincipalName,userDisplayName,riskLevel,riskState,riskDetail,riskLastUpdatedDateTime,isDeleted,isProcessing"
)


def _text(value) -> str | None:
    return value if isinstance(value, str) and value else None


def normalise(u: dict) -> dict:
    return {
        "user": _text(u.get("userPrincipalName")) or _text(u.get("userDisplayName")) or _text(u.get("id")),
        "userId": _text(u.get("id")),
        "displayName": _text(u.get("userDisplayName")),
        "level": _text(u.get("riskLevel")),
        "state": _text(u.get("riskState")),
        "detail": _text(u.get("riskDetail")),
        "updated": _text(u.get("riskLastUpdatedDateTime")),
        "deleted": u.get("isDeleted") is True,
        "processing": u.get("isProcessing") is True,
    }


@register
class RiskyUsers(Collector):
    key = "risky_users"
    title = "Risky users"
    description = (
        f"Accounts Entra ID Protection rates at risk or confirmed compromised, and those resolved in "
        f"the last {RESOLVED_DAYS} days (needs Entra ID P2)."
    )
    permissions = ("IdentityRiskyUser.Read.All",)

    def collect(self, ctx: RunContext) -> None:
        since = (datetime.now(UTC) - timedelta(days=RESOLVED_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
        if ctx.capabilities.get("entra_p2") is False:
            ctx.log("Tenant has no Entra ID P2; Identity Protection's risky users aren't available")
            ctx.objects("risky_user_status", [("status", {"available": False, "reason": "licence"})])
            return
        try:
            result = ctx.graph.get_all(
                "/identityProtection/riskyUsers",
                params={"$select": FIELDS, "$top": str(PAGE_SIZE)},
                max_pages=MAX_PAGES,
            )
        except (LicenseRequired, PermissionMissing) as exc:
            reason = "licence" if isinstance(exc, LicenseRequired) else "permission"
            why = (
                "the tenant's licence doesn't include Entra ID P2"
                if reason == "licence"
                else "IdentityRiskyUser.Read.All consent missing"
            )
            ctx.warn(f"Risky users weren't readable ({why}): {exc.message}")
            ctx.objects("risky_user_status", [("status", {"available": False, "reason": reason})])
            return
        items = ctx.paged(result)
        kept = []
        for u in items:
            if not isinstance(u, dict) or not _text(u.get("id")):
                continue
            row = normalise(u)
            if row["state"] in OPEN_RISK or (row["state"] in RESOLVED_STATES and (row["updated"] or "") >= since):
                kept.append((row["userId"], row))
        ctx.objects(
            "risky_user_status",
            [
                (
                    "status",
                    {
                        "available": True,
                        "read": len(items),
                        "since": since,
                        "days": RESOLVED_DAYS,
                        "truncated": result.truncated,
                    },
                )
            ],
        )
        ctx.objects("risky_user", kept)
        at_risk = [r for _, r in kept if r["state"] in OPEN_RISK and not r["deleted"]]
        ctx.metric("risky_users.at_risk", len(at_risk))
        ctx.metric("risky_users.high", sum(r["level"] == "high" for r in at_risk))
        ctx.metric("risky_users.compromised", sum(r["state"] == "confirmedCompromised" for r in at_risk))
        ctx.log(f"{plural(len(at_risk), 'account')} at risk; read {plural(len(items), 'risky user')}")
