"""MFA and sign-in protection."""

from __future__ import annotations

from ... import authmethods
from ..identity_reports import GAP_TEXT, METHOD_NAMES, Enforcement, Registration, _methods, _phishing_resistant
from .base import Part, Person, _yes, part


@part(20)
def mfa_part(p: Person) -> Part:
    result = Part("mfa", "MFA and sign-in protection")
    reg, enf = Registration(p.snap), Enforcement(p.snap)
    row = reg.get(p.id)
    registered = reg.registered(p.id)
    user_type = "Guest" if p.guest else "Member"
    sources, excluded = enf.for_user(p.id, p.label, user_type) if enf.known else ([], [])
    required = bool(sources) if enf.known else None
    cov = enf.coverage(p.id, p.label, user_type)

    if p.guest:
        result.facts.append(("MFA set up", "Set up in their own organisation, so not visible here"))
    else:
        result.facts.append(("MFA set up", _yes(registered)))
        if row is not None:
            result.facts.append(("Methods", ", ".join(_methods(row)) or "None"))
            default = row.get("defaultMfaMethod")
            if default and default.lower() != "none":
                result.facts.append(("Default method", METHOD_NAMES.get(default.lower(), default)))
            result.facts.append(("Phishing-resistant method", _yes(_phishing_resistant(row))))
            _sign_in_strength(p, row, result)
    result.facts.append(("MFA required by", ", ".join(sources) or ("Nothing" if enf.known else "Not checked")))
    if excluded:
        result.facts.append(("Excluded from", ", ".join(excluded)))
    if cov is not None:
        result.facts.append(("Conditional Access policies that apply", ", ".join(cov.policies) or "None"))
        result.facts.append(("Legacy sign-in blocked", "Yes" if cov.legacy_blocked or enf.security_defaults else "No"))
    elif enf.security_defaults:
        result.facts.append(("Legacy sign-in blocked", "Yes, by Security Defaults"))
    if enf.per_user_checked and p.id in enf.per_user:
        result.facts.append(("Per-user MFA", (enf.per_user[p.id] or "disabled").capitalize()))
    result.note = " ".join(n for n in (reg.note, enf.note) if n)

    if p.guest:
        result.summary, result.tone = ("Required", "ok") if required else (
            ("Not required", "warn") if required is False else ("Not checked", None))
        if required is False and p.enabled:
            p.flag("low", "This guest isn't asked for MFA when signing in to this tenant.", "mfa")
        return result
    if registered and required:
        result.summary, result.tone = "Protected", "ok"
        return result
    if required is False:
        kind = "none" if registered is False else "not_required"
    elif required and registered is False:
        kind = "not_registered"
    else:
        kind = "unknown"
    title, meaning = GAP_TEXT[kind]
    if kind == "not_required" and registered is None:
        title, meaning = "Not required", "Isn't asked for MFA, so a stolen password may be enough."
    result.summary = title
    result.tone = {"none": "bad", "not_required": "warn", "not_registered": "warn"}.get(kind)
    if p.enabled and kind != "unknown":
        severity = {"none": "critical" if p.admin else "high",
                    "not_required": "high" if p.admin else "medium",
                    "not_registered": "high" if p.admin else "medium"}[kind]
        p.flag(severity, f"{title}: {meaning}" + (" This account holds an admin role." if p.admin else ""), "mfa")
    if excluded and p.enabled:
        p.flag("medium" if p.admin else "low", f"Excluded from {', '.join(excluded)}.", "mfa")
    return result


def _sign_in_strength(p: Person, row: dict, result: Part) -> None:
    """How strong the account's methods are (Passwords and sign-in methods report), as facts and flags."""
    s = authmethods.strength(row)
    result.facts.append(("Strongest method", s.strongest))
    first = authmethods.preferred(row)
    if first:
        result.facts.append(("Asked for first", first))
    for key, label in (("isPasswordlessCapable", "Can sign in without a password"),
                       ("isSsprRegistered", "Can reset their own password")):
        if row.get(key) is not None:
            result.facts.append((label, _yes(row.get(key))))
    if not p.enabled:
        return
    privileged = bool(p.roles and p.roles["privileged"])
    if privileged and s.methods and not s.phishing_resistant:
        p.flag("medium", "Admin without a phishing-resistant method: register a passkey or security key. A fake "
                         "sign-in page can relay codes and push approvals.", "mfa")
    if s.phone_only:
        p.flag("medium" if privileged else "low", "A text or call is the only second factor: set up the "
                                                  "Authenticator app.", "mfa")
