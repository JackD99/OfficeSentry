"""What changed between two snapshots of a tenant (e.g. now vs 4 weeks ago)."""

from __future__ import annotations

from dataclasses import dataclass

from . import store
from .devices import clean as clean_device


@dataclass
class Change:
    area: str
    kind: str  # added | removed | changed
    subject: str
    detail: str


def _baseline_run(conn, tenant_id: int, collector: str, before: str):
    return conn.execute(
        "SELECT * FROM runs WHERE tenant_id = ? AND collector = ? AND status IN ('succeeded', 'partial')"
        " AND finished_at <= ? ORDER BY finished_at DESC, id DESC LIMIT 1",
        (tenant_id, collector, before),
    ).fetchone()


def _pair(conn, tenant_id: int, collector: str, kind: str, before: str):
    latest = store.latest_run(conn, tenant_id, collector)
    base = _baseline_run(conn, tenant_id, collector, before)
    if latest is None or base is None or latest["id"] == base["id"]:
        return None
    old = store.run_objects(conn, base["id"], kind)
    new = store.run_objects(conn, latest["id"], kind)
    return {o["_id"]: o for o in old}, {o["_id"]: o for o in new}


def _who(o: dict) -> str:
    return o.get("userPrincipalName") or o.get("displayName") or o.get("_id", "?")


def admin_changes(conn, tenant_id: int, before: str) -> list[Change]:
    pair = _pair(conn, tenant_id, "admin_roles", "role_assignment", before)
    if pair is None:
        return []

    def keyed(rows):
        return {(o.get("principalId"), o.get("roleName")): o for o in rows.values()
                if o.get("privileged") and o.get("assignment") == "active"}

    old, new = keyed(pair[0]), keyed(pair[1])
    out = [Change("Admins", "added", _who(o), f"Now holds {o['roleName']}") for k, o in new.items() if k not in old]
    out += [Change("Admins", "removed", _who(o), f"No longer holds {o['roleName']}")
            for k, o in old.items() if k not in new]
    return out


def user_changes(conn, tenant_id: int, before: str) -> list[Change]:
    pair = _pair(conn, tenant_id, "users", "user", before)
    if pair is None:
        return []
    old, new = pair
    out = []
    for uid, u in new.items():
        kind = "Guest" if u.get("userType") == "Guest" else "Member"
        if uid not in old:
            out.append(Change("Accounts", "added", _who(u), f"New {kind.lower()} account"))
        elif old[uid].get("accountEnabled") and not u.get("accountEnabled"):
            out.append(Change("Accounts", "changed", _who(u), "Disabled"))
        elif not old[uid].get("accountEnabled") and u.get("accountEnabled"):
            out.append(Change("Accounts", "changed", _who(u), "Re-enabled"))
    out += [Change("Accounts", "removed", _who(u), "Deleted") for uid, u in old.items() if uid not in new]
    return out


STATE_NAMES = {"enabled": "on", "disabled": "off", "enabledForReportingButNotEnforced": "report-only"}


def ca_changes(conn, tenant_id: int, before: str) -> list[Change]:
    pair = _pair(conn, tenant_id, "conditional_access", "ca_policy", before)
    if pair is None:
        return []
    old, new = pair
    out = []
    for pid, p in new.items():
        name = p.get("displayName") or pid
        if pid not in old:
            state = STATE_NAMES.get(p.get("state"), p.get("state"))
            out.append(Change("Conditional Access", "added", name, f"New policy ({state})"))
        elif old[pid].get("state") != p.get("state"):
            out.append(Change("Conditional Access", "changed", name,
                              f"Switched {STATE_NAMES.get(old[pid].get('state'), '?')} → "
                              f"{STATE_NAMES.get(p.get('state'), '?')}"))
        elif old[pid].get("modifiedDateTime") != p.get("modifiedDateTime"):
            out.append(Change("Conditional Access", "changed", name, "Policy edited"))
    out += [Change("Conditional Access", "removed", p.get("displayName") or pid, "Policy deleted")
            for pid, p in old.items() if pid not in new]
    return out


def forwarding_changes(conn, tenant_id: int, before: str) -> list[Change]:
    from .mailflow import mailbox_forwarding

    pair = _pair(conn, tenant_id, "mailboxes", "mailbox", before)
    if pair is None:
        return []
    old, new = pair
    out = []
    for mid, m in new.items():
        target, was = mailbox_forwarding(m), mailbox_forwarding(old.get(mid) or {})
        name = m.get("PrimarySmtpAddress") or mid
        if target and target != was:
            out.append(Change("Mail", "added", name, f"Now forwards to {target}"))
        elif was and not target:
            out.append(Change("Mail", "removed", name, f"Stopped forwarding to {was}"))
    return out


def app_changes(conn, tenant_id: int, before: str) -> list[Change]:
    pair = _pair(conn, tenant_id, "app_consents", "app_permission", before)
    if pair is None:
        return []
    latest = store.latest_run(conn, tenant_id, "app_consents")
    names = {o["_id"]: o.get("displayName") for o in store.run_objects(conn, latest["id"], "service_principal")}

    def keyed(rows):
        return {(o.get("clientId"), o.get("permission")) for o in rows.values()}

    added: dict[str, list[str]] = {}
    for client, perm in sorted(keyed(pair[1]) - keyed(pair[0])):
        added.setdefault(client, []).append(perm)
    return [Change("Apps", "added", names.get(c) or c, f"Granted {', '.join(p)}") for c, p in added.items()]


COMPLIANCE_WORDS = {"compliant": "compliant", "noncompliant": "not compliant", "inGracePeriod": "in grace period"}


def _device_pair(conn, tenant_id: int, kind: str, before: str):
    pair = _pair(conn, tenant_id, "devices", kind, before)
    return None if pair is None else tuple({k: clean_device(d) for k, d in side.items()} for side in pair)


def device_changes(conn, tenant_id: int, before: str) -> list[Change]:
    """Intune enrolments, retirements and compliance or encryption lost; Entra records without Intune."""
    pair = _device_pair(conn, tenant_id, "managed_device", before)
    if pair is not None and (pair[0] or pair[1]):
        old, new = pair
        out = []
        for did, d in new.items():
            name = d.get("deviceName") or did
            user = f" ({d['userPrincipalName']})" if d.get("userPrincipalName") else ""
            if did not in old:
                enrolled = f"Enrolled {d.get('operatingSystem') or ''}".strip()
                out.append(Change("Devices", "added", name + user, enrolled))
                continue
            was, now = old[did].get("complianceState"), d.get("complianceState")
            if was == "compliant" and now != "compliant" and now in COMPLIANCE_WORDS:
                out.append(Change("Devices", "changed", name + user, f"Now {COMPLIANCE_WORDS[now]}"))
            elif was != "compliant" and now == "compliant" and was in COMPLIANCE_WORDS:
                out.append(Change("Devices", "changed", name + user, "Compliant again"))
            if old[did].get("isEncrypted") and d.get("isEncrypted") is False:
                out.append(Change("Devices", "changed", name + user, "Disk no longer encrypted"))
        out += [Change("Devices", "removed", d.get("deviceName") or did, "Removed from Intune")
                for did, d in old.items() if did not in new]
        return out
    pair = _device_pair(conn, tenant_id, "entra_device", before)
    if pair is None:
        return []
    old, new = pair
    out = [Change("Devices", "added", d.get("displayName") or did, f"New {d.get('operatingSystem') or 'device'} "
                  "registered") for did, d in new.items() if did not in old]
    return out + [Change("Devices", "removed", d.get("displayName") or did, "Removed from Entra ID")
                  for did, d in old.items() if did not in new]


def all_changes(conn, tenant_id: int, before: str) -> list[Change]:
    return admin_changes(conn, tenant_id, before) + ca_changes(conn, tenant_id, before) + \
        user_changes(conn, tenant_id, before) + forwarding_changes(conn, tenant_id, before) + \
        app_changes(conn, tenant_id, before) + device_changes(conn, tenant_id, before)
