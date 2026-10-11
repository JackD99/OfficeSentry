"""Checks on how accounts are kept: reporting lines that lead nowhere."""

from __future__ import annotations

from datetime import datetime

from ..snapshot import TenantSnapshot
from ..text import plural
from .base import Finding, _label, _unavailable, rule


@rule(
    "blocked_manager",
    "No account reports to a blocked manager",
    "Accounts",
    "Set a current manager on each listed account (Entra admin center > Users > Properties > Manager, or in "
    "on-premises AD for synced accounts). The manager is where approvals, leaver notices and access reviews "
    "go; when they've left, those go nowhere, and their blocked account is usually waiting to be deleted too.",
)
def blocked_manager(snap: TenantSnapshot, now: datetime):
    users = snap.objects("users", "user")
    people = snap.objects("user_details", "person")
    if users is None or people is None:
        return _unavailable("Departments and managers haven't been collected yet")
    by_id = {u["_id"]: u for u in users if isinstance(u.get("_id"), str)}
    managers = {
        p["_id"]: p["manager"] for p in people if isinstance(p.get("_id"), str) and isinstance(p.get("manager"), dict)
    }
    if not managers:
        return _unavailable("No account has a manager set")
    findings = []
    for uid, m in managers.items():
        account = by_id.get(uid)
        if account is None or account.get("accountEnabled") is not True or not m.get("id"):
            continue
        target = by_id.get(m["id"])
        enabled = target.get("accountEnabled") if target else m.get("enabled")
        if enabled is not False:
            continue
        name = (target or {}).get("displayName") or m.get("name") or m.get("upn") or "a blocked account"
        findings.append(Finding("low", _label(account), f"Reports to {name}, whose sign-in is blocked", uid))
    return (
        (
            "fail",
            f"{plural(len(findings), 'account')} report{'s' if len(findings) == 1 else ''} to a blocked manager",
            findings,
        )
        if findings
        else ("pass", f"Every manager set on {plural(len(managers), 'account')} can sign in", [])
    )
