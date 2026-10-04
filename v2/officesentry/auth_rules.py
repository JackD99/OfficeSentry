"""Checks on how strong people's sign-in methods are. Imported at the bottom of ``rules.py``.

Whether people have MFA at all is ``users_mfa_registered`` and ``admin_mfa``;
these go one step further: admins on phishing-resistant methods, and nobody
relying on a phone alone.
"""

from __future__ import annotations

from datetime import datetime

from .authmethods import PHONE, strength
from .rules import Finding, _label, _unavailable, rule
from .snapshot import TenantSnapshot
from .text import every, plural, verb


def _registration(snap: TenantSnapshot) -> tuple[dict[str, dict] | None, str]:
    rows = snap.objects("mfa_registration", "mfa_registration")
    if rows is None:
        return None, "MFA registration data hasn't been collected yet"
    if not rows:
        return None, "Microsoft returned no MFA registration data yet"
    return {r["_id"]: r for r in rows}, ""


def _privileged_people(snap: TenantSnapshot) -> dict[str, tuple[str, list[str]]] | None:
    """Enabled members holding a privileged role, active or PIM-eligible: label and role names.

    Guests are left out: they sign in with their own organisation's methods (``guest_admins`` covers them)."""
    rows = snap.objects("admin_roles", "role_assignment")
    if rows is None:
        return None
    disabled = {u["_id"] for u in snap.objects("users", "user") or [] if u.get("accountEnabled") is False}
    out: dict[str, tuple[str, list[str]]] = {}
    for r in rows:
        pid = r.get("principalId")
        if not r.get("privileged") or r.get("principalType") != "user" or not isinstance(pid, str) \
                or r.get("accountEnabled") is False or pid in disabled or r.get("userType") == "Guest":
            continue
        label, names = out.setdefault(pid, (_label(r), []))
        name = r.get("roleName") or "Unknown role"
        if name not in names:
            names.append(name)
    return out


@rule("admin_phishing_resistant", "Admins use phishing-resistant sign-in methods", "Admins",
      "Have each listed admin register a passkey (in Microsoft Authenticator or on a security key) or Windows Hello "
      "for Business, then require the Phishing-resistant MFA authentication strength for admin roles in "
      "Conditional Access. Codes and push approvals can be relayed by a fake sign-in page; these can't.")
def admin_phishing_resistant(snap: TenantSnapshot, now: datetime):
    admins = _privileged_people(snap)
    reg, reason = _registration(snap)
    if admins is None:
        return _unavailable("Admin role data hasn't been collected yet")
    if reg is None:
        return _unavailable(reason)
    findings, unknown, checked = [], 0, 0
    for pid, (label, roles) in admins.items():
        s = strength(reg.get(pid))
        if s is None:
            unknown += 1
            continue
        checked += 1
        if s.phishing_resistant:
            continue
        has = ", ".join(s.labels) if s.labels else "no MFA method at all"
        findings.append(Finding("medium", label, f"{', '.join(roles)}; signs in with {has}", pid))
    if findings:
        return ("fail", f"{plural(len(findings), 'admin')} without a phishing-resistant method", findings)
    if not checked:
        return _unavailable(f"MFA registration isn't known for {plural(unknown, 'admin')}" if unknown
                            else "Nobody holds a privileged role")
    extra = f" ({unknown} not known yet)" if unknown else ""
    return ("pass", f"{every(checked, 'admin', start=True)} {verb(checked, 'has', 'have')} a phishing-resistant "
                    f"method{extra}", [])


@rule("phone_only_mfa", "Nobody relies on a text or call as their only second factor", "Identity",
      "Ask these people to set up the Microsoft Authenticator app (the registration campaign in the authentication "
      "methods policy prompts them at sign-in), then turn off text message and voice call sign-in, or keep them "
      "only as a backup. Texts and calls can be intercepted or moved to an attacker's SIM.")
def phone_only_mfa(snap: TenantSnapshot, now: datetime):
    reg, reason = _registration(snap)
    if reg is None:
        return _unavailable(reason)
    users = snap.objects("users", "user")
    if users is None:
        return _unavailable("User data hasn't been collected yet")
    admins = _privileged_people(snap) or {}
    findings, checked = [], 0
    for u in users:
        if not u.get("accountEnabled") or (u.get("userType") or "Member") != "Member":
            continue
        s = strength(reg.get(u["_id"]))
        if s is None or not s.methods:
            continue  # unknown, or no MFA at all (users_mfa_registered covers that)
        checked += 1
        if s.phone_only:
            admin = u["_id"] in admins
            phones = ", ".join(m for m, tier in s.methods if tier == PHONE)
            findings.append(Finding("medium" if admin else "low", _label(u),
                                    f"Second factor is only {phones}" + (" (holds an admin role)" if admin else ""),
                                    u["_id"]))
    if not checked:
        return _unavailable("Nobody's sign-in methods are known yet")
    if findings:
        return ("fail", f"{plural(len(findings), 'person')} can only use a text or call as a second factor",
                findings)
    return ("pass", f"Everyone with MFA has something stronger than a text or call ({plural(checked, 'person')} "
                    "checked)", [])
