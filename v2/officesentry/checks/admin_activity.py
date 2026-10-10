"""Checks over the directory audit log."""

from __future__ import annotations

from datetime import datetime

from .. import adminactivity
from ..snapshot import TenantSnapshot
from ..text import day, plural
from .base import Finding, _unavailable, rule


@rule("domain_federation_changed", "No unexpected changes to how domains sign in", "Identity",
      "Confirm with the client that each change was planned (for example, setting up AD FS or another identity "
      "provider). If it wasn't, an attacker with admin rights may have federated the domain to their own identity "
      "provider so they can sign in as any user without a password or MFA: switch the domain back to managed "
      "sign-in, remove unknown federation settings, reset admin credentials and review the audit log.")
def domain_federation_changed(snap: TenantSnapshot, now: datetime):
    window = (snap.objects("admin_activity", "audit_window") or [None])[0]
    if window is None:
        return _unavailable("Admin activity hasn't been collected yet")
    if not window.get("available"):
        return _unavailable("The tenant's directory audit log couldn't be read")
    changes: dict[str, list[str]] = {}
    for e in sorted(snap.objects("admin_activity", "admin_event") or [], key=lambda e: e.get("time") or ""):
        if e.get("kind") == "federation" and e.get("result") == "success":
            domain = ", ".join(e.get("targets") or []) or "Unnamed domain"
            changes.setdefault(domain, []).append(
                f"{e.get('activity')} by {e.get('actor')} on {day(e.get('time'))}")
    findings = [Finding("high", domain, "; ".join(what), domain) for domain, what in changes.items()]
    days = adminactivity.covered_days(window)
    return ("fail", f"{plural(len(findings), 'domain')} had sign-in settings changed in the last {days} days",
            findings) \
        if findings else ("pass", f"No domain federation changes in the last {days} days", [])
