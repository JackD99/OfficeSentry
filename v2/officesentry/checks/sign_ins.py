"""Checks over the sign-in log summaries."""

from __future__ import annotations

from datetime import datetime

from .. import signins
from ..formatting import day
from ..snapshot import TenantSnapshot
from ..text import plural
from .base import Finding, _unavailable, rule


@rule(
    "risky_sign_ins",
    "No risky sign-ins succeeded",
    "Identity",
    "Check each sign-in with the person. If they don't recognise it, reset their password, sign them out "
    "everywhere (revoke sessions), and check their MFA methods, inbox rules and app consents; then confirm the "
    "account compromised in Entra ID Protection. Add a Conditional Access policy that asks for MFA on medium "
    "and high sign-in risk. The Sign-in activity report lists every risky sign-in.",
)
def risky_sign_ins(snap: TenantSnapshot, now: datetime):
    status = signins.status(snap)
    if status is None:
        return _unavailable("Sign-in activity hasn't been collected yet")
    if not status.get("available"):
        return _unavailable(
            "Office Sentry can't read the sign-in log (AuditLog.Read.All)"
            if status.get("reason") == "permission"
            else "The sign-in log needs Entra ID P1"
        )
    if not status.get("riskVisible"):
        return _unavailable("Sign-in risk needs Entra ID P2")
    days = status.get("days") if isinstance(status.get("days"), int) else signins.WINDOW_DAYS
    by_account: dict[str, list[tuple[str, dict]]] = {}
    for r in snap.objects("sign_ins", "risky_sign_in") or []:
        if r.get("succeeded") is not True:
            continue
        severity = signins.risk_severity(r.get("level"), r.get("state"))
        if severity:
            user = r.get("user") if isinstance(r.get("user"), str) and r.get("user") else "Unknown account"
            by_account.setdefault(user, []).append((severity, r))
    findings = []
    for user, hits in by_account.items():
        severity = "high" if any(s == "high" for s, _ in hits) else "medium"
        latest = max((r for _, r in hits), key=lambda r: r.get("time") if isinstance(r.get("time"), str) else "")
        when = day(latest.get("time"))
        level = latest.get("level") if isinstance(latest.get("level"), str) else "unknown"
        state = signins.RISK_STATES.get(latest.get("state") if isinstance(latest.get("state"), str) else "", "")
        detail = (
            f"{plural(len(hits), 'successful risky sign-in')}; latest{' ' + when if when else ''} from "
            f"{signins.country(latest.get('country'))} ({level} risk"
            f"{', ' + state.lower() if state else ''})"
        )
        uid = latest.get("userId") if isinstance(latest.get("userId"), str) else user
        findings.append(Finding(severity, user, detail, uid))
    if findings:
        return (
            "fail",
            f"{plural(len(findings), 'account')} signed in despite medium or high sign-in risk in the last {days} days",
            findings,
        )
    if status.get("truncated"):
        # The log stopped at the page cap, so a risky sign-in may be in the part that wasn't read.
        return _unavailable("The sign-in log was cut short, so this can't confirm no risky sign-in succeeded")
    return ("pass", f"No risky sign-ins succeeded in the last {days} days", [])
