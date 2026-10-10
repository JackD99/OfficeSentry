"""What changed for the account across retained snapshots."""

from __future__ import annotations

from ... import store
from ...licences import sku_name
from ...mailflow import mailbox_forwarding
from ...snapshot import TenantSnapshot
from ..data_reports import _type
from ..identity_reports import _methods
from .base import HISTORY_LIMIT, Event


def _object_history(conn, tenant_id: int, collector: str, kind: str, object_id: str) -> list[tuple[str, dict]]:
    return store.object_copies(conn, tenant_id, collector, kind, object_id)


def _role_history(conn, tenant_id: int, uid: str) -> list[tuple[str, set[str]]]:
    runs = conn.execute(
        "SELECT id, finished_at FROM runs WHERE tenant_id = ? AND collector = 'admin_roles'"
        " AND status IN ('succeeded', 'partial')"
        " AND EXISTS (SELECT 1 FROM objects o WHERE o.run_id = runs.id) ORDER BY finished_at, id",
        (tenant_id,)).fetchall()
    held: dict[int, set[str]] = {r["id"]: set() for r in runs}
    if runs:
        for _, _, run_id, d in store.matching_objects(conn, tenant_id, "admin_roles", "role_assignment",
                                                      "principalId", uid):
            if run_id in held:
                held[run_id].add((d.get("roleName") or "a role")
                                 + ("" if d.get("assignment") == "active" else " (eligible)"))
    return [(r["finished_at"], held[r["id"]]) for r in runs]


def history(snap: TenantSnapshot, uid: str, skus: dict) -> tuple[list[Event], str | None]:
    """What changed for the account between stored snapshots, newest first, and the oldest snapshot it's in."""
    conn, tid = snap.conn, snap.tenant["id"]
    events: list[Event] = []

    def sku(sid: str) -> str:
        part_number = (skus.get(sid) or {}).get("skuPartNumber")
        return sku_name(part_number) if part_number else "a licence"

    users = _object_history(conn, tid, "users", "user", uid)
    since = users[0][0] if users else None
    for (_, old), (when, new) in zip(users, users[1:], strict=False):
        day = when[:10]
        if old.get("accountEnabled") and not new.get("accountEnabled"):
            events.append(Event(day, "Account", "Sign-in blocked", "warn"))
        elif not old.get("accountEnabled") and new.get("accountEnabled"):
            events.append(Event(day, "Account", "Sign-in allowed again"))
        for key, label in (("userPrincipalName", "Sign-in name"), ("displayName", "Name"),
                           ("userType", "Account type")):
            if old.get(key) and new.get(key) and old[key] != new[key]:
                events.append(Event(day, "Account", f"{label} changed from {old[key]} to {new[key]}"))
        before, after = set(old.get("skuIds") or []), set(new.get("skuIds") or [])
        events += [Event(day, "Licences", f"{sku(s)} added") for s in sorted(after - before)]
        events += [Event(day, "Licences", f"{sku(s)} removed") for s in sorted(before - after)]
        if old.get("lastPasswordChangeDateTime") != new.get("lastPasswordChangeDateTime") and \
                new.get("lastPasswordChangeDateTime"):
            events.append(Event(new["lastPasswordChangeDateTime"][:10], "Account", "Password changed"))
        if old.get("onPremisesSyncEnabled") != new.get("onPremisesSyncEnabled"):
            events.append(Event(day, "Account", "Now synced from on-premises AD" if new.get("onPremisesSyncEnabled")
                                else "No longer synced from on-premises AD"))

    roles = _role_history(conn, tid, uid)
    for (_, old), (when, new) in zip(roles, roles[1:], strict=False):
        events += [Event(when[:10], "Admin roles", f"Given {r}", "warn") for r in sorted(new - old)]
        events += [Event(when[:10], "Admin roles", f"{r} removed") for r in sorted(old - new)]

    mfa = _object_history(conn, tid, "mfa_registration", "mfa_registration", uid)
    for (_, old), (when, new) in zip(mfa, mfa[1:], strict=False):
        before, after = set(_methods(old)), set(_methods(new))
        events += [Event(when[:10], "MFA", f"Registered {m}", "ok") for m in sorted(after - before)]
        events += [Event(when[:10], "MFA", f"Removed {m}", "warn") for m in sorted(before - after)]
        if before == after and bool(old.get("isMfaRegistered")) != bool(new.get("isMfaRegistered")):
            events.append(Event(when[:10], "MFA", "MFA set up" if new.get("isMfaRegistered") else "MFA removed",
                                "ok" if new.get("isMfaRegistered") else "warn"))

    boxes = _object_history(conn, tid, "mailboxes", "mailbox", uid)
    for (_, old), (when, new) in zip(boxes, boxes[1:], strict=False):
        a, b = mailbox_forwarding(old), mailbox_forwarding(new)
        if a != b:
            events.append(Event(when[:10], "Mailbox", f"Forwarding set to {b}" if b else "Forwarding removed",
                                "warn" if b else None))
        if old.get("RecipientTypeDetails") != new.get("RecipientTypeDetails") and new.get("RecipientTypeDetails"):
            events.append(Event(when[:10], "Mailbox", f"Converted to a {_type(new['RecipientTypeDetails']).lower()} "
                                                      "mailbox"))
    events.sort(key=lambda e: e.day, reverse=True)
    return events[:HISTORY_LIMIT], since
