"""Checks on mailboxes, forwarding, inbox rules and Exchange Online settings."""

from __future__ import annotations

from datetime import datetime

from ..mailflow import inbox_rule_risk, is_external, mailbox_forwarding, tenant_domains
from ..snapshot import TenantSnapshot
from ..text import every, plural, verb
from .base import Finding, _unavailable, rule


def _exchange_missing(snap: TenantSnapshot):
    status = snap.objects("mailboxes", "exchange_status")
    if status and not status[0].get("available"):
        return _unavailable("The tenant has no Exchange Online licence")
    return None


@rule(
    "external_forwarding",
    "No mail is forwarded outside the organisation",
    "Email",
    "Remove forwarding to outside addresses unless the client has a documented business reason. "
    "Forwarding is the most common way attackers keep reading a mailbox after a password reset.",
)
def external_forwarding(snap: TenantSnapshot, now: datetime):
    if missing := _exchange_missing(snap):
        return missing
    mailboxes = snap.objects("mailboxes", "mailbox")
    rules = snap.objects("inbox_rules", "inbox_rule")
    domains = tenant_domains(snap)
    if (mailboxes is None and rules is None) or domains is None:
        return _unavailable("Mailbox and inbox rule data hasn't been collected yet")
    findings = []
    unresolved = 0
    for m in mailboxes or []:
        target = mailbox_forwarding(m)
        if target and "@" not in target:
            # ForwardingAddress names a recipient that Get-Recipient couldn't look up, so where it
            # leads (inside or outside the organisation) isn't known.
            unresolved += 1
            findings.append(
                Finding(
                    "medium",
                    m.get("PrimarySmtpAddress") or m["_id"],
                    f"Forwards to an address Office Sentry couldn't resolve ({target})",
                    f"mailbox:{m['_id']}",
                )
            )
        elif target and is_external(target, domains):
            keep = "keeps a copy" if m.get("DeliverToMailboxAndForward") else "doesn't keep a copy"
            findings.append(
                Finding(
                    "high",
                    m.get("PrimarySmtpAddress") or m["_id"],
                    f"Mailbox forwarding to {target} ({keep})",
                    f"mailbox:{m['_id']}",
                )
            )
    for r in rules or []:
        risk = inbox_rule_risk(r, domains)
        if risk.external:
            state = "" if r.get("isEnabled", True) else " (disabled)"
            findings.append(
                Finding(
                    "high" if r.get("isEnabled", True) else "low",
                    r.get("mailbox") or "?",
                    f'Inbox rule "{r.get("displayName")}" forwards to {", ".join(risk.external)}{state}',
                    f"rule:{r['_id']}",
                )
            )
    scope = "" if mailboxes is not None else " (inbox rules only; Exchange data not collected)"
    if not findings:
        return ("pass", f"Nothing forwards outside the organisation{scope}", [])
    external = len(findings) - unresolved
    parts = [plural(external, "external forward")] if external else []
    if unresolved:
        parts.append(f"{plural(unresolved, 'forward')} to an address Office Sentry couldn't resolve")
    return ("fail", ", ".join(parts) + scope, findings)


@rule(
    "suspicious_inbox_rules",
    "No inbox rules hide mail",
    "Email",
    "Check each rule with the mailbox owner. Rules that delete or bury messages about payments or "
    "security alerts are a sign of a compromised account: reset the password, revoke sessions and review sign-ins.",
)
def suspicious_inbox_rules(snap: TenantSnapshot, now: datetime):
    if missing := _exchange_missing(snap):
        return missing
    rules = snap.objects("inbox_rules", "inbox_rule")
    domains = tenant_domains(snap)
    if rules is None or domains is None:
        return _unavailable("Inbox rules haven't been collected yet")
    findings = []
    for r in rules:
        risk = inbox_rule_risk(r, domains)
        reasons = [x for x in risk.reasons if not x.startswith("Forwards to")]
        if risk.severity and reasons:
            findings.append(
                Finding(
                    risk.severity,
                    r.get("mailbox") or "?",
                    f'"{r.get("displayName")}": {"; ".join(reasons)}',
                    f"rule:{r['_id']}",
                )
            )
    return (
        ("fail", f"{plural(len(findings), 'suspicious inbox rule')}", findings)
        if findings
        else ("pass", f"No inbox rule hides mail ({plural(len(rules), 'rule')} checked)", [])
    )


@rule(
    "auto_forward_policy",
    "Automatic forwarding to outside addresses is off",
    "Email",
    "Set the outbound spam filter policy's automatic forwarding to Off (or Automatic, which Microsoft treats "
    "as Off), and allow forwarding only for named mailboxes with a separate policy.",
)
def auto_forward_policy(snap: TenantSnapshot, now: datetime):
    if missing := _exchange_missing(snap):
        return missing
    policies = snap.objects("mailboxes", "outbound_spam_policy")
    if policies is None:
        return _unavailable("Exchange data hasn't been collected yet")
    allowing = [p for p in policies if p.get("AutoForwardingMode") == "On"]
    # The default policy covers everyone no other policy names. A scoped policy that allows forwarding
    # is what the recommendation asks for: it opens forwarding only for the mailboxes it lists.
    findings = [
        Finding(
            "medium",
            p.get("Name") or p["_id"],
            "Default policy allows automatic forwarding to any outside address",
            p["_id"],
        )
        for p in allowing
        if _is_default_policy(p)
    ]
    if findings:
        return ("fail", "Automatic external forwarding is allowed for everyone", findings)
    scoped = len(allowing)
    return (
        "pass",
        "The default outbound policy blocks automatic external forwarding"
        + (
            f"; {plural(scoped, 'scoped policy', 'scoped policies')} {verb(scoped, 'allows', 'allow')} it "
            f"for the mailboxes {verb(scoped, 'it covers', 'they cover')}"
            if scoped
            else ""
        ),
        [],
    )


def _is_default_policy(policy: dict) -> bool:
    """Whether an outbound spam filter policy is the organisation-wide default. Without IsDefault, the
    default is the one Microsoft names "Default"."""
    if isinstance(policy.get("IsDefault"), bool):
        return policy["IsDefault"]
    return policy.get("Name") == "Default" or policy.get("_id") == "Default"


@rule(
    "shared_mailbox_signin",
    "Shared mailboxes can't be signed in to",
    "Email",
    "Block sign-in for each shared mailbox's account (it doesn't need a password). An enabled shared "
    "mailbox account usually has no MFA and nobody watching it.",
)
def shared_mailbox_signin(snap: TenantSnapshot, now: datetime):
    if missing := _exchange_missing(snap):
        return missing
    mailboxes = snap.objects("mailboxes", "mailbox")
    users = snap.objects("users", "user")
    if mailboxes is None or users is None:
        return _unavailable("Needs mailbox and user data")
    enabled = {u["_id"] for u in users if u.get("accountEnabled")}
    shared = [m for m in mailboxes if m.get("RecipientTypeDetails") == "SharedMailbox"]
    findings = [
        Finding("medium", m.get("PrimarySmtpAddress") or m["_id"], "Shared mailbox account allows sign-in", m["_id"])
        for m in shared
        if m.get("ExternalDirectoryObjectId") in enabled
    ]
    if findings:
        n = len(findings)
        return (
            "fail",
            f"{n} of {plural(len(shared), 'shared mailbox', 'shared mailboxes')} {verb(n, 'allows', 'allow')} sign-in",
            findings,
        )
    return (
        "pass",
        f"{every(len(shared), 'shared mailbox', 'shared mailboxes', start=True)} "
        f"{verb(len(shared), 'blocks', 'block')} sign-in",
        [],
    )


def _mail_setting(snap: TenantSnapshot, key: str):
    rows = snap.objects("mailboxes", "mail_settings")
    if not rows:
        return None
    return rows[0].get(key)


@rule(
    "mailbox_auditing",
    "Mailbox auditing is on",
    "Email",
    "Turn mailbox auditing back on for the organisation (Set-OrganizationConfig -AuditDisabled $false). "
    "Without it there's no record of who read or deleted mail during an incident.",
)
def mailbox_auditing(snap: TenantSnapshot, now: datetime):
    if missing := _exchange_missing(snap):
        return missing
    value = _mail_setting(snap, "AuditDisabled")
    if value is None:
        return _unavailable("Exchange data hasn't been collected yet")
    return (
        ("fail", "Mailbox auditing is turned off", [Finding("medium", "Organisation", "AuditDisabled is set")])
        if value
        else ("pass", "Mailbox auditing is on for the organisation", [])
    )


@rule(
    "smtp_auth",
    "SMTP AUTH is turned off",
    "Email",
    "Turn off SMTP AUTH for the organisation and enable it only on the mailboxes of devices or apps that "
    "still need it, such as scanners. It accepts passwords without MFA.",
)
def smtp_auth(snap: TenantSnapshot, now: datetime):
    if missing := _exchange_missing(snap):
        return missing
    value = _mail_setting(snap, "SmtpClientAuthenticationDisabled")
    if value is None:
        return _unavailable("Exchange data hasn't been collected yet")
    return (
        ("pass", "SMTP AUTH is off for the organisation", [])
        if value
        else ("fail", "SMTP AUTH is on for the organisation", [Finding("low", "Organisation", "SMTP AUTH is allowed")])
    )
