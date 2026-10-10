"""Everyone, for the Users tab."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from ...licences import is_stale, last_sign_in
from ...snapshot import TenantSnapshot
from ...text import plural as _plural
from ..data_reports import Highlight
from ..identity_reports import Enforcement, Registration, _roles
from .base import INACTIVE_DAYS, _day


@dataclass
class DirectoryRow:
    id: str
    name: str
    upn: str
    guest: bool
    enabled: bool
    last_sign_in: str  # "3 Sep 2026", "Never" or "Not checked"
    last_sign_in_sort: str
    mfa: str
    mfa_tone: str | None
    roles: list[str]
    licences: int
    findings: int


def directory(conn, tenant, now: datetime | None = None) -> tuple[list[DirectoryRow], list[Highlight]] | None:
    """Every account with the few facts that tell you where to look. None before Users is collected."""
    now = now or datetime.now(UTC)
    snap = TenantSnapshot(conn, tenant)
    users = snap.objects("users", "user")
    if users is None:
        return None
    reg, enf = Registration(snap), Enforcement(snap)
    roles = _roles(snap) or {}
    counts: dict[str, int] = {}
    labels: dict[str, str] = {}
    for u in users:
        for k in (u.get("userPrincipalName"), u.get("mail")):
            if isinstance(k, str) and k:
                labels.setdefault(k.lower(), u["_id"])
    ids = {u["_id"] for u in users}
    for r in conn.execute("SELECT object_id, subject FROM findings WHERE tenant_id = ? AND resolved_at IS NULL",
                          (snap.tenant["id"],)):
        uid = r["object_id"] if r["object_id"] in ids else labels.get((r["subject"] or "").lower())
        if uid:
            counts[uid] = counts.get(uid, 0) + 1

    rows = []
    no_mfa = inactive = 0
    for u in users:
        uid = u["_id"]
        guest = (u.get("userType") or "Member") == "Guest"
        enabled = bool(u.get("accountEnabled"))
        if not u.get("signInActivityAvailable"):
            seen, seen_sort = "Not checked", ""
        else:
            last = last_sign_in(u)
            seen, seen_sort = (_day(last), last.isoformat()) if last else ("Never", "0")
            inactive += bool(enabled and is_stale(u, now, INACTIVE_DAYS))
        label = u.get("userPrincipalName") or u.get("displayName") or uid
        registered = reg.registered(uid)
        sources, _ = enf.for_user(uid, label, "Guest" if guest else "Member") if enf.known else ([], [])
        required = bool(sources) if enf.known else None
        if guest:
            mfa, tone = ("Required", "ok") if required else (("Not required", "warn") if required is False
                                                             else ("Unknown", None))
        elif registered and required:
            mfa, tone = "Protected", "ok"
        elif required is False and registered is False:
            mfa, tone = "No MFA", "bad"
        elif required is False:
            mfa, tone = "Not required", "warn"
        elif required and registered is False:
            mfa, tone = "Not set up", "warn"
        else:
            mfa, tone = "Unknown", None
        if enabled and not guest and mfa == "No MFA":
            no_mfa += 1
        r = roles.get(uid)
        rows.append(DirectoryRow(uid, u.get("displayName") or label, u.get("userPrincipalName") or "", guest,
                                 enabled, seen, seen_sort, mfa if enabled else "–", tone if enabled else None,
                                 (r["active"] + [f"{n} (eligible)" for n in r["eligible"]]) if r else [],
                                 len(u.get("skuIds") or []), counts.get(uid, 0)))
    rows.sort(key=lambda r: (-r.findings, not r.roles, r.name.lower()))
    members = sum(not r.guest for r in rows)
    tiles = [
        Highlight("Accounts", len(rows), f"{_plural(members, 'member')}, {_plural(len(rows) - members, 'guest')}"),
        Highlight("Sign-in blocked", sum(not r.enabled for r in rows), "Can't sign in"),
        Highlight("No MFA at all", no_mfa if enf.known and not reg.note else "Not checked",
                  "Enabled members", "bad" if no_mfa else None),
        Highlight("Admins", sum(bool(r.roles) for r in rows), "Hold an admin role"),
        Highlight(f"Inactive {INACTIVE_DAYS}+ days", inactive if any(u.get("signInActivityAvailable") for u in users)
                  else "Not checked", "Enabled, no recent sign-in", "warn" if inactive else None),
    ]
    return rows, tiles
