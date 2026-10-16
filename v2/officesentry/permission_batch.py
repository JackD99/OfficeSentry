"""Permission batches: new read-only permissions released together, so each client approves once.

Adding a permission means every client's Global Administrator has to approve the app again, so new ones
wait for a batch (``BATCHES``), released with one version and explained on one page. Rolling a batch out
takes two steps, both from stored state:

1. The MSP's own app registration has to ask for the new permissions, or a client's approval grants only
   the old ones. ``app_ready`` says whether it does; the App connection page's update wizard
   (``app_setup.start(..., purpose="update")``) or the ``update-app`` command adds them.
2. Each client approves them with the consent link it was sent before (``onboarding``'s tracker and its
   "What's new" page, with a PDF that says what each one reads and what it adds). The next connection check
   reads what was granted.

Until then, nothing that needs a new permission fails or nags: the connection collector logs the batch's
missing permissions without making the run partial, ``tenant_health`` shows them as a step to do rather than
a problem, and reports and collectors that need one say they're waiting for approval (``awaiting``,
``notice``). Everything here reads stored state only; nothing calls Microsoft.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import store
from .db import audit, utcnow
from .entra import PERMISSIONS, SHAREPOINT_APP_ID, label


@dataclass(frozen=True)
class Batch:
    release: str  # the version that first asks for these ("2.2")
    title: str
    permissions: tuple[str, ...]  # labels, as in entra.PERMISSIONS


BATCHES: tuple[Batch, ...] = (
    Batch(
        "2.2",
        "Teams, Intune, BitLocker, PIM for groups and SharePoint access",
        (
            "Channel.ReadBasic.All",
            "ChannelMember.Read.All",
            "TeamsAppInstallation.ReadForTeam.All",
            "ReportSettings.Read.All",
            "PrivilegedEligibilitySchedule.Read.AzureADGroup",
            "BitLockerKey.ReadBasic.All",
            "DeviceManagementConfiguration.Read.All",
            "DeviceManagementApps.Read.All",
            label(SHAREPOINT_APP_ID, "Sites.Read.All"),
        ),
    ),
    Batch(
        "2.4",
        "Windows LAPS, Defender alerts and incidents, service health and the message centre",
        (
            "DeviceLocalCredential.ReadBasic.All",
            "SecurityAlert.Read.All",
            "SecurityIncident.Read.All",
            "ServiceHealth.Read.All",
            "ServiceMessage.Read.All",
        ),
    ),
)
CURRENT = BATCHES[-1]
BATCH_OF: dict[str, Batch] = {p: b for b in BATCHES for p in b.permissions}

# What approving each one adds, in words for the client (the What's new page and its PDF).
# test_permission_batch.py checks every permission in a batch has a line here.
ADDS: dict[str, str] = {
    "Channel.ReadBasic.All": "A Teams report listing every team's channels, including private and shared ones.",
    "ChannelMember.Read.All": "Who is in each private and shared channel, with guests and people from other "
    "organisations marked.",
    "TeamsAppInstallation.ReadForTeam.All": "The apps added to each team, so unexpected ones stand out.",
    "ReportSettings.Read.All": "Usage reports say when your Microsoft 365 hides people's names, instead of "
    "showing codes without explanation.",
    "PrivilegedEligibilitySchedule.Read.AzureADGroup": "Admin reports include people who can make themselves an "
    "admin through a group (Privileged Identity Management for groups).",
    "BitLockerKey.ReadBasic.All": "Device reports show whether each computer's BitLocker recovery key is saved, "
    "so a locked computer can be recovered.",
    "DeviceManagementConfiguration.Read.All": "Firewall, screen lock and update settings are read from Intune for "
    "the device and Cyber Essentials reports, instead of being typed in.",
    "DeviceManagementApps.Read.All": "App protection policies and app versions are read from Intune for the "
    "device and Cyber Essentials reports.",
    label(SHAREPOINT_APP_ID, "Sites.Read.All"): "Who has access to each SharePoint site and list, including "
    "members of SharePoint's own groups.",
    "DeviceLocalCredential.ReadBasic.All": "Device reports show which computers back up their local admin "
    "password with Windows LAPS, so a shared or never-changed admin password stands out.",
    "SecurityAlert.Read.All": "A security alerts report: what Microsoft Defender has flagged, how severe, and "
    "which alerts nobody has looked at yet.",
    "SecurityIncident.Read.All": "The same report groups alerts into Defender's incidents, with who is dealing "
    "with each.",
    "ServiceHealth.Read.All": "A service health report: Microsoft 365 outages and advisories affecting you, so "
    "a problem on Microsoft's side is known before anyone troubleshoots it.",
    "ServiceMessage.Read.All": "The same report lists Microsoft's upcoming changes, with the ones that need "
    "action by a date first.",
}


def release_of(permission: str) -> str | None:
    """The release whose batch added ``permission``, or None for the permissions asked for from the start."""
    batch = BATCH_OF.get(permission)
    return batch.release if batch else None


def _missing(result: dict | None) -> list[str]:
    r = result or {}
    return list(r.get("missing") or []) if r.get("permissions_checked") else []


def waiting(result: dict | None) -> list[str]:
    """New permissions (from a batch) the tenant hasn't approved yet, from a connection check's result."""
    return [p for p in _missing(result) if p in BATCH_OF]


def missing_original(result: dict | None) -> list[str]:
    """Permissions asked for from the start that the tenant hasn't granted: a real problem, not a new batch."""
    return [p for p in _missing(result) if p not in BATCH_OF]


def connection_result(snap) -> dict | None:
    """The latest connection check in a ``TenantSnapshot``, or None when there's none."""
    rows = snap.objects("connection", "connection") or []
    return rows[0] if rows else None


def awaiting(result: dict | None, *permissions: str) -> list[str]:
    """Which of ``permissions`` the tenant is still to approve, in the order given. Empty when they're granted
    or when it isn't known yet (no connection check), so a collector tries rather than waits for nothing."""
    pending = set(waiting(result))
    return [p for p in permissions if p in pending]


def notice(snap, *permissions: str) -> str | None:
    """A report's notice while the client hasn't approved a new permission it needs, or None."""
    pending = awaiting(connection_result(snap), *permissions)
    if not pending:
        return None
    return waiting_text(snap.tenant["name"], pending)


def releases(permissions) -> str:
    """The releases whose batches added ``permissions``, in words: "2.4", or "2.2 and 2.4" for a client that
    didn't approve an earlier batch before the next one came."""
    found = sorted({r for p in permissions if (r := release_of(p))}, key=_release_key) or [CURRENT.release]
    return found[0] if len(found) == 1 else ", ".join(found[:-1]) + " and " + found[-1]


def waiting_text(client: str, pending: list[str]) -> str:
    release = releases(pending)
    names = ", ".join(pending)
    return (
        f"Waiting for {client} to approve the new read-only permission{'s' if len(pending) > 1 else ''} this needs "
        f"({names}), added in Office Sentry {release}. An admin sends the approval link from the client's What's "
        "new page; this fills in after the next collection once it's approved."
    )


# -- The MSP's app registration -----------------------------------------------------------------------


def _release_key(release: str | None) -> tuple[int, ...]:
    try:
        return tuple(int(x) for x in (release or "0").split("."))
    except ValueError:
        return (0,)


def app_release(connection) -> str | None:
    return connection["permissions_release"] if connection is not None else None


def app_ready(conn, connection, batch: Batch = CURRENT) -> bool:
    """Whether the app registration asks for ``batch``: it was created or updated for it, or a client of this
    connection has granted every one of its permissions, which it couldn't have unless the app asked."""
    if connection is None or not connection["client_id"]:
        return False
    if _release_key(app_release(connection)) >= _release_key(batch.release):
        return True
    for t in conn.execute(
        "SELECT id FROM tenants WHERE connection_id = ? AND consented_at IS NOT NULL", (connection["id"],)
    ):
        run = store.latest_run(conn, t["id"], "connection")
        rows = store.run_objects(conn, run["id"], "connection") if run else []
        result = rows[0] if rows else {}
        if result.get("permissions_checked") and not set(batch.permissions) - set(result.get("granted") or []):
            return True
    return False


def mark_app(conn, connection_id: int, actor: str, how: str, batch: Batch = CURRENT) -> None:
    """Record that the app registration now asks for ``batch`` (``how``: wizard, command or by hand)."""
    conn.execute("UPDATE app_connections SET permissions_release = ? WHERE id = ?", (batch.release, connection_id))
    audit(conn, actor, "connection_permissions_updated", detail=f"{batch.release} ({how})")


# -- One client's approval ------------------------------------------------------------------------------


def request_for(conn, tenant_id: int, batch: Batch = CURRENT):
    return conn.execute(
        "SELECT * FROM permission_requests WHERE tenant_id = ? AND release = ?", (tenant_id, batch.release)
    ).fetchone()


def requests_for(conn, tenant_ids, batch: Batch = CURRENT) -> dict:
    ids = list(tenant_ids)
    if not ids:
        return {}
    return {
        r["tenant_id"]: r
        for r in conn.execute(
            f"SELECT * FROM permission_requests WHERE release = ? AND tenant_id IN ({','.join('?' * len(ids))})",
            (batch.release, *ids),
        )
    }


def mark_sent(conn, tenant_ids, user: str, sent: bool = True, batch: Batch = CURRENT) -> None:
    now = utcnow()
    for t in tenant_ids:
        if sent:
            conn.execute(
                "INSERT INTO permission_requests (tenant_id, release, sent_at, sent_by) VALUES (?, ?, ?, ?)"
                " ON CONFLICT (tenant_id, release) DO UPDATE SET sent_at = excluded.sent_at,"
                " sent_by = excluded.sent_by",
                (t, batch.release, now, user),
            )
        else:
            conn.execute(
                "UPDATE permission_requests SET sent_at = NULL, sent_by = NULL WHERE tenant_id = ? AND release = ?",
                (t, batch.release),
            )
        audit(conn, user, "permission_request_sent" if sent else "permission_request_unsent", t, batch.release)


def note_approved(conn, tenant_id: int, batch: Batch = CURRENT) -> None:
    """Someone came back from Microsoft's consent page through the client's link. Not proof: the connection
    check that follows reads what was actually granted."""
    conn.execute(
        "INSERT INTO permission_requests (tenant_id, release, approved_at) VALUES (?, ?, ?)"
        " ON CONFLICT (tenant_id, release) DO UPDATE SET approved_at = excluded.approved_at",
        (tenant_id, batch.release, utcnow()),
    )


@dataclass(frozen=True)
class Item:
    permission: str
    resource: str  # "Microsoft Graph", "Office 365 SharePoint Online"
    name: str  # the permission's name as Microsoft shows it
    reads: str  # what it lets Office Sentry read, for the client
    adds: str  # what approving it adds


def items(words: dict[str, str], batch: Batch = CURRENT, only=None) -> list[Item]:
    """The batch's permissions with what each reads (``words``: ``onboarding.CLIENT_WORDS``) and adds, for the
    What's new page and its PDF. ``only``: those permissions instead, from whichever batch added each, so a
    client that didn't approve an earlier batch is asked for it with this one."""
    wanted = None if only is None else set(only)
    chosen = batch.permissions if wanted is None else [p for b in BATCHES for p in b.permissions if p in wanted]
    out = []
    for p in chosen:
        resource, name, _ = PERMISSIONS[p]
        out.append(Item(p, resource.name, name, words.get(p, ""), ADDS.get(p, "")))
    return out
