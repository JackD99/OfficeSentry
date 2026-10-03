"""Mail-flow helpers shared by checks and reports: external addresses and inbox rule risk."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# Folders attackers move mail into so the victim doesn't see replies.
HIDING_FOLDERS = {"rss feeds", "rss subscriptions", "conversation history", "archive", "deleted items",
                  "junk email", "notes", "sync issues"}
BEC_KEYWORDS = {"invoice", "payment", "wire", "bank", "transfer", "remittance", "payroll", "iban",
                "swift", "password", "account", "urgent", "fraud", "hacked", "phish", "suspicious", "ceo"}


def smtp_address(value: str | None) -> str | None:
    """'smtp:Ann@X.com' or 'Ann@X.com' -> 'ann@x.com'."""
    if not value:
        return None
    value = value.strip()
    if ":" in value and value.split(":", 1)[0].lower() in ("smtp", "sip", "x500"):
        value = value.split(":", 1)[1]
    return value.lower() if "@" in value else None


def is_external(address: str | None, domains: set[str]) -> bool:
    if not address or "@" not in address:
        return False
    domain = address.rsplit("@", 1)[1].lower()
    return not any(domain == d or domain.endswith("." + d) for d in domains)


def tenant_domains(snap) -> set[str] | None:
    """Accepted domains from Exchange, else verified domains from the tenant profile."""
    accepted = snap.objects("mailboxes", "accepted_domain")
    if accepted:
        return {d["DomainName"] for d in accepted if d.get("DomainName")}
    orgs = snap.objects("tenant_profile", "organization")
    if orgs:
        return {d["name"].lower() for o in orgs for d in o.get("verifiedDomains") or [] if d.get("name")}
    return None


def mailbox_forwarding(mailbox: dict) -> str | None:
    """Where an admin-set mailbox forward sends mail, as an address, if anywhere."""
    if mailbox.get("ForwardingSmtpAddress"):
        return smtp_address(mailbox["ForwardingSmtpAddress"])
    recipient = mailbox.get("ForwardingRecipient") or {}
    return (smtp_address(recipient.get("ExternalEmailAddress"))
            or smtp_address(recipient.get("PrimarySmtpAddress"))
            or (mailbox.get("ForwardingAddress") or None))


@dataclass
class RuleRisk:
    severity: str | None  # None: nothing suspicious
    reasons: list[str] = field(default_factory=list)
    forwards_to: list[str] = field(default_factory=list)
    external: list[str] = field(default_factory=list)


def _addresses(recipients) -> list[str]:
    out = []
    for r in recipients or []:
        addr = smtp_address(((r or {}).get("emailAddress") or {}).get("address"))
        if addr:
            out.append(addr)
    return out


def inbox_rule_risk(rule: dict, domains: set[str]) -> RuleRisk:
    actions = rule.get("actions") or {}
    conditions = rule.get("conditions") or {}
    risk = RuleRisk(None)
    for key in ("forwardTo", "redirectTo", "forwardAsAttachmentTo"):
        risk.forwards_to += _addresses(actions.get(key))
    risk.external = [a for a in risk.forwards_to if is_external(a, domains)]

    folder = (rule.get("moveToFolderName") or "").lower()
    hides = bool(actions.get("delete") or actions.get("permanentDelete") or folder in HIDING_FOLDERS)
    words = " ".join(str(w) for k in ("bodyContains", "subjectContains", "bodyOrSubjectContains")
                     for w in conditions.get(k) or []).lower()
    keywords = sorted(k for k in BEC_KEYWORDS if re.search(rf"\b{k}", words))
    odd_name = not re.search(r"[A-Za-z0-9]{3}", rule.get("displayName") or "")

    severity = None
    if risk.external:
        risk.reasons.append("Forwards to " + ", ".join(risk.external))
        severity = "high"
    if hides:
        where = "deletes" if actions.get("delete") or actions.get("permanentDelete") else \
            f"moves to {rule.get('moveToFolderName')}"
        if keywords:
            risk.reasons.append(f"{where[0].upper()}{where[1:]} mail mentioning {', '.join(keywords)}")
            severity = "high"
        elif actions.get("markAsRead"):
            risk.reasons.append(f"{where[0].upper()}{where[1:]} and marks as read")
            severity = severity or "medium"
    if odd_name and (risk.forwards_to or hides):
        risk.reasons.append(f"Rule name {rule.get('displayName')!r} looks deliberately blank")
        severity = severity or "medium"
    risk.severity = severity if rule.get("isEnabled", True) else (severity and "low")
    return risk


def size_bytes(value) -> int | None:
    """Exchange sizes: '49.5 GB (53,150,220,288 bytes)' -> 53150220288; 'Unlimited' or unknown -> None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    match = re.search(r"\(([\d,]+) bytes\)", value or "") if isinstance(value, str) else None
    return int(match.group(1).replace(",", "")) if match else None
