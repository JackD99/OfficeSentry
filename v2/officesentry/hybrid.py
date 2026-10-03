"""Hybrid identity helpers shared by the directory sync checks and the Hybrid identity report.

Entra Connect syncs every 30 minutes, and with password hash sync on it also
sends password changes and a heartbeat every few minutes. So a last sync more
than a few hours old means the sync server has stopped, and a last password
sync far behind the last directory sync means password hash sync has stopped.
"""

from __future__ import annotations

from datetime import datetime

from .devices import parse_ts
from .snapshot import TenantSnapshot

SYNC_LATE_HOURS = 3           # several missed 30-minute cycles
SYNC_STOPPED_HOURS = 24       # a day without sync: accounts disabled in AD stay active in the cloud
PASSWORD_SYNC_GAP_HOURS = 24  # password sync this far behind directory sync has stopped


def hours_since(value: str | None, now: datetime) -> float | None:
    dt = parse_ts(value)
    return None if dt is None else max((now - dt).total_seconds() / 3600, 0.0)


def age(hours: float) -> str:
    if hours < 1:
        return "less than an hour ago"
    if hours < 48:
        return f"{int(hours)} hour{'s' if int(hours) != 1 else ''} ago"
    return f"{int(hours // 24)} days ago"


def sync_status(snap: TenantSnapshot) -> dict | None:
    """The tenant's directory sync status, or None if it hasn't been collected."""
    rows = snap.objects("directory_sync", "sync_status")
    if rows is None:
        return None
    return rows[0] if rows else {}


def sync_problem(status: dict, now: datetime) -> tuple[str, str] | None:
    """(severity, detail) when directory sync is on but not running on schedule."""
    hours = hours_since(status.get("lastSyncDateTime"), now)
    if hours is None:
        return "high", "Turned on, but Entra ID has no record of a completed sync"
    if hours > SYNC_STOPPED_HOURS:
        return "critical", (f"Last sync {age(hours)}. Accounts disabled or removed in Active Directory since then "
                            "are still active in Microsoft 365")
    if hours > SYNC_LATE_HOURS:
        return "high", f"Last sync {age(hours)}; Entra Connect normally syncs every 30 minutes"
    return None


def password_sync_problem(status: dict, now: datetime) -> tuple[str, str] | None:
    """(severity, detail) when password hash sync looks off or stopped. Needs the password sync time."""
    password = hours_since(status.get("lastPasswordSyncDateTime"), now)
    if password is None:
        return "medium", "Entra ID has no record of a password hash sync"
    directory = hours_since(status.get("lastSyncDateTime"), now)
    if directory is not None and password - directory > PASSWORD_SYNC_GAP_HOURS:
        return "medium", (f"Last password sync {age(password)}, though directory sync ran {age(directory)}: "
                          "password hash sync has stopped or been turned off")
    return None


def federated_domains(snap: TenantSnapshot) -> list[str]:
    return sorted(d["_id"] for d in snap.objects("directory_sync", "domain") or []
                  if d.get("authenticationType") == "Federated")


def privileged_principals(snap: TenantSnapshot) -> set[str] | None:
    """User IDs holding a privileged role, active or eligible."""
    roles = snap.objects("admin_roles", "role_assignment")
    if roles is None:
        return None
    return {r["principalId"] for r in roles if r.get("privileged") and r.get("principalType") == "user"}


def synced_admins(snap: TenantSnapshot) -> list[dict] | None:
    """Privileged role holders (active and eligible) whose account is synced from on-premises AD."""
    roles = snap.objects("admin_roles", "role_assignment")
    users = snap.objects("users", "user")
    if roles is None or users is None:
        return None
    synced = {u["_id"]: u for u in users if u.get("onPremisesSyncEnabled")}
    admins: dict[str, dict] = {}
    for r in roles:
        pid = r.get("principalId")
        if not r.get("privileged") or r.get("principalType") != "user" or pid not in synced:
            continue
        user = synced[pid]
        admin = admins.setdefault(pid, {
            "principalId": pid,
            "label": r.get("userPrincipalName") or user.get("userPrincipalName") or r.get("displayName") or pid,
            "accountEnabled": bool(user.get("accountEnabled")),
            "roles": [],
        })
        role = (r.get("roleName") or "Unknown role") + (" (eligible)" if r.get("assignment") == "eligible" else "")
        if role not in admin["roles"]:
            admin["roles"].append(role)
    return sorted(admins.values(), key=lambda a: a["label"].lower())
