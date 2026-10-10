"""Checks over Entra ID Protection's risky users."""

from __future__ import annotations

from datetime import datetime

from .. import clouds
from .. import identity_protection as ip
from ..snapshot import TenantSnapshot
from ..text import plural
from .base import Finding, _unavailable, rule


@rule("risky_users", "No accounts are at risk in Entra ID Protection", "Identity",
      "Check each account with the person. If they don't recognise recent sign-ins, reset the password, sign them "
      "out everywhere (revoke sessions), and check their MFA methods, inbox rules and app consents; then confirm "
      "the account compromised or dismiss the risk in Entra ID Protection. Add a Conditional Access policy that "
      "asks for a secure password change on high user risk. The Risky users report lists each account.")
def risky_users(snap: TenantSnapshot, now: datetime):
    if why := clouds.missing(clouds.of(snap.tenant), "risky_users"):
        return _unavailable(why)
    status = ip.status(snap)
    if status is None:
        return _unavailable("Risky users haven't been collected yet")
    if not status.get("available"):
        return _unavailable("Office Sentry can't read risky users (IdentityRiskyUser.Read.All)"
                            if status.get("reason") == "permission" else "Risky users need Entra ID P2")
    findings = []
    for r in ip.rows(snap):
        if not ip.open_risk(r) or r.get("deleted") is True:
            continue
        when = r.get("updated")[:10] if isinstance(r.get("updated"), str) else ""
        detail = f"{ip.level(r)} risk, {ip.state(r).lower()}" + (f" since {when}" if when else "")
        user = r.get("user") if isinstance(r.get("user"), str) else "Unknown account"
        findings.append(Finding(ip.severity(r), user, detail, r.get("userId") or user))
    if not findings:
        if status.get("truncated"):
            # The list stopped at the page cap, so an account at risk may be past it.
            return _unavailable("Too many risk records to read them all, so this can't confirm none are at risk")
        return ("pass", "No accounts are at risk", [])
    high = sum(f.severity == "high" for f in findings)
    return ("fail", f"{plural(len(findings), 'account')} at risk in Entra ID Protection"
                    + (f", {high} high or confirmed compromised" if high else ""), findings)
