"""Checks on directory sync from on-premises AD."""

from __future__ import annotations

from datetime import datetime

from .. import (
    hybrid,
)
from ..snapshot import TenantSnapshot
from ..text import plural
from .base import Finding, _all_are, _unavailable, rule


def _sync_on(snap: TenantSnapshot):
    """Directory sync status when sync is on, or the reason the sync checks can't run."""
    status = hybrid.sync_status(snap)
    if status is None:
        return None, "Directory sync data hasn't been collected yet"
    if not status.get("enabled"):
        return None, "Cloud-only tenant: directory sync isn't turned on"
    return status, ""


@rule("directory_sync_running", "Directory sync is running", "Hybrid identity",
      "Check the Entra Connect server: that it is up, the Microsoft Azure AD Sync service is running and "
      "Synchronization Service Manager shows no errors. Until sync runs, accounts disabled or removed in Active "
      "Directory keep working in Microsoft 365. If the client has moved to cloud-only, turn directory sync off.")
def directory_sync_running(snap: TenantSnapshot, now: datetime):
    status, reason = _sync_on(snap)
    if status is None:
        return _unavailable(reason)
    hours = hybrid.hours_since(status.get("lastSyncDateTime"), now)
    summary = "Turned on but has never synced" if hours is None else f"Last sync {hybrid.age(hours)}"
    problem = hybrid.sync_problem(status, now)
    if problem:
        severity, detail = problem
        return ("fail", summary, [Finding(severity, "Directory sync", detail)])
    return ("pass", summary, [])


@rule("password_hash_sync", "Password hash sync is on", "Hybrid identity",
      "Turn on password hash synchronization in Entra Connect (Optional features), even when users sign in "
      "through federation or pass-through authentication. Entra ID can then detect leaked passwords, and "
      "sign-in can be switched to the cloud if the on-premises servers are down.")
def password_hash_sync(snap: TenantSnapshot, now: datetime):
    status, reason = _sync_on(snap)
    if status is None:
        return _unavailable(reason)
    if not status.get("passwordSyncChecked"):
        return _unavailable("The last password hash sync time couldn't be read")
    problem = hybrid.password_sync_problem(status, now)
    if problem:
        severity, detail = problem
        federated = hybrid.federated_domains(snap)
        if federated:
            detail += f". Sign-in for {', '.join(federated)} is federated and has no cloud fallback"
        summary = "No password hash sync recorded" if status.get("lastPasswordSyncDateTime") is None \
            else "Password hash sync has stopped"
        return ("fail", summary, [Finding(severity, "Password hash sync", detail)])
    hours = hybrid.hours_since(status["lastPasswordSyncDateTime"], now)
    return ("pass", f"Last password sync {hybrid.age(hours)}", [])


@rule("cloud_only_admins", "Admin accounts are cloud-only", "Hybrid identity",
      "Give each admin a separate cloud-only account (for example on the .onmicrosoft.com domain) for their admin "
      "roles, and remove the roles from accounts synced from Active Directory. Otherwise anyone who takes over "
      "on-premises AD can reset those passwords and become an admin in Microsoft 365.")
def cloud_only_admins(snap: TenantSnapshot, now: datetime):
    admins = hybrid.synced_admins(snap)
    if admins is None:
        return _unavailable("Needs admin role and user data")
    if not admins:
        status = hybrid.sync_status(snap) or {}
        if not status.get("enabled") and not any(u.get("onPremisesSyncEnabled") for u in snap.objects("users", "user")):
            return ("pass", "Cloud-only tenant: no accounts are synced from on-premises AD", [])
        return ("pass", _all_are(len(hybrid.privileged_principals(snap)), "privileged account") + " cloud-only", [])
    findings = []
    for a in admins:
        detail = f"{', '.join(a['roles'])}; synced from on-premises AD"
        if not a["accountEnabled"]:
            detail += " (disabled here, but AD can re-enable it)"
        findings.append(Finding("high" if a["accountEnabled"] else "medium", a["label"], detail, a["principalId"]))
    return ("fail", f"{plural(len(findings), 'admin account')} synced from on-premises AD", findings)
