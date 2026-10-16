"""Checks on mailboxes, forwarding, inbox rules and Exchange Online settings, and on the mail flow rules and
protection policies that filter mail (``mailprotection.py``)."""

from __future__ import annotations

from datetime import datetime

from .. import mailprotection as mp
from ..mailflow import inbox_rule_risk, is_default_policy, is_external, mailbox_forwarding, tenant_domains
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
    "Remove forwarding to outside addresses unless there's a documented business reason. "
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
    if not policies:  # every Exchange Online organisation has a default policy, so none means it wasn't read
        return _unavailable("Exchange returned no outbound spam policy")
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
        if is_default_policy(p)
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
    "audit_log",
    "The Microsoft 365 audit log is on",
    "Email",
    "Turn on the audit log in Microsoft Purview (Audit, then Start recording user and admin activity). Without "
    "it there's no record of who deleted, downloaded or shared files, who opened someone else's mailbox or who "
    "created an inbox rule, and Microsoft can take up to a day to start recording.",
)
def audit_log(snap: TenantSnapshot, now: datetime):
    if missing := _exchange_missing(snap):
        return missing
    rows = snap.objects("mailboxes", "mail_settings")
    if not rows:
        return _unavailable("Exchange data hasn't been collected yet")
    value = rows[0].get("UnifiedAuditLogIngestionEnabled")
    if not isinstance(value, bool):
        return _unavailable("Exchange Online didn't say whether the audit log is on")
    return (
        ("pass", "The audit log is recording user and admin activity", [])
        if value
        else (
            "fail",
            "The Microsoft 365 audit log is turned off",
            [Finding("high", "Organisation", "Audit log search isn't recording")],
        )
    )


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


PART_WORDS = {
    "rules": "Mail flow rules",
    "anti_spam": "Anti-spam policies",
    "anti_phish": "Anti-phishing policies",
    "anti_malware": "Anti-malware policies",
}


def _protection_missing(snap: TenantSnapshot, part: str):
    """Unavailable when the Mail rules and protection collection hasn't read ``part`` (or ran on a tenant
    without Exchange Online), else None."""
    if missing := _exchange_missing(snap):
        return missing
    status = mp.status(snap)
    if status is None:
        return _unavailable("Mail rules and protection haven't been collected yet")
    if status.get("available") is False:
        return _unavailable("The tenant has no Exchange Online licence")
    state = mp.parts(snap).get(part)
    if state == "read":
        return None
    if state == "unlicensed":
        return _unavailable("Needs Defender for Office 365")
    return _unavailable(f"{PART_WORDS.get(part, part)} couldn't be read; see the collection's log")


@rule(
    "mail_rule_spam_bypass",
    "No mail flow rule lets mail skip spam filtering",
    "Email",
    "Remove mail flow rules that skip spam filtering (set the spam confidence level to -1) for senders picked "
    "by their address, domain or a header, which anyone can fake. For mail arriving through another filtering "
    "service use enhanced filtering on its inbound connector instead, and for a partner limit the rule to the "
    "addresses of their sending servers.",
)
def mail_rule_spam_bypass(snap: TenantSnapshot, now: datetime):
    if missing := _protection_missing(snap, "rules"):
        return missing
    rules = mp.objects(snap, "transport_rule")
    findings = [
        Finding(
            "high" if mp.spoofable(r) else "medium",
            r.get("Name") or r["_id"],
            ("Skips spam filtering for mail picked out by: " if mp.spoofable(r) else "Skips spam filtering for: ")
            + "; ".join(mp.conditions(r)),
            r["_id"],
        )
        for r in rules
        if mp.is_live(r) and mp.skips_spam_filter(r)
    ]
    if findings:
        n = len(findings)
        return ("fail", f"{plural(n, 'mail flow rule')} {verb(n, 'skips', 'skip')} spam filtering", findings)
    return ("pass", f"No mail flow rule skips spam filtering ({plural(len(rules), 'rule')} checked)", [])


@rule(
    "mail_rule_external_copy",
    "No mail flow rule sends mail outside the organisation",
    "Email",
    "Check each rule that redirects mail or sends copies to an outside address, and remove it "
    "unless there's a documented reason (a journaling or archiving service). Attackers with admin access add "
    "these rules to read a whole organisation's mail.",
)
def mail_rule_external_copy(snap: TenantSnapshot, now: datetime):
    if missing := _protection_missing(snap, "rules"):
        return missing
    domains = tenant_domains(snap)
    if not domains:
        return _unavailable("Needs the tenant's accepted domains from Exchange data")
    rules = mp.objects(snap, "transport_rule")
    findings = [
        Finding(
            "high",
            r.get("Name") or r["_id"],
            f"Sends mail or copies to {', '.join(outside)}",
            r["_id"],
        )
        for r in rules
        if mp.is_live(r) and (outside := mp.external_copies(r, domains))
    ]
    if findings:
        n = len(findings)
        return (
            "fail",
            f"{plural(n, 'mail flow rule')} {verb(n, 'sends', 'send')} mail outside the organisation",
            findings,
        )
    return ("pass", f"No mail flow rule sends mail outside ({plural(len(rules), 'rule')} checked)", [])


@rule(
    "spam_allowed_domains",
    "No sender domains are allowed past the spam filter",
    "Email",
    "Remove the allowed sender domains from the anti-spam policies: anyone can send mail claiming to be from a "
    "domain, so it skips spam and spoofing checks. Where a partner's mail is wrongly caught, report it to "
    "Microsoft or add an allow entry for their spoofed sender in the Tenant Allow/Block List instead.",
)
def spam_allowed_domains(snap: TenantSnapshot, now: datetime):
    if missing := _protection_missing(snap, "anti_spam"):
        return missing
    domains = tenant_domains(snap)
    policies = [p for p in mp.policies(snap, "anti_spam") if mp.in_effect(p)]
    findings = []
    for p in policies:
        allowed = [d.lower() for d in mp.text_list(p.get("AllowedSenderDomains"))]
        if not allowed:
            continue
        risky = mp.risky_domains(allowed, domains)
        detail = f"Allows mail claiming to be from {', '.join(allowed)} past the spam filter"
        if risky:
            detail += f" ({', '.join(risky)} {verb(len(risky), 'is', 'are')} free mail or the organisation's own)"
        findings.append(Finding("high" if risky else "medium", p.get("Name") or p["_id"], detail, p["_id"]))
    if findings:
        n = sum(len(mp.text_list(p.get("AllowedSenderDomains"))) for p in policies)
        return (
            "fail",
            f"{plural(n, 'sender domain')} {verb(n, 'skips', 'skip')} the spam filter",
            findings,
        )
    return ("pass", f"No anti-spam policy allows sender domains ({plural(len(policies), 'policy', 'policies')})", [])


@rule(
    "spoof_protection",
    "Spoof protection is on",
    "Email",
    "Turn spoof intelligence and “Honour DMARC policy” back on in the anti-phishing policies (both are on "
    "by default). Without them, mail pretending to be from other organisations' domains is delivered.",
)
def spoof_protection(snap: TenantSnapshot, now: datetime):
    if missing := _protection_missing(snap, "anti_phish"):
        return missing
    policies = [
        p
        for p in mp.policies(snap, "anti_phish")
        if mp.in_effect(p) and (p.get("Enabled") is not False or mp.is_default(p))
    ]
    findings = []
    for p in policies:
        if p.get("EnableSpoofIntelligence") is False:
            findings.append(Finding("high", p.get("Name") or p["_id"], "Spoof intelligence is off", p["_id"]))
        elif p.get("HonorDmarcPolicy") is False:
            findings.append(
                Finding("medium", p.get("Name") or p["_id"], "Ignores what senders' DMARC policies ask for", p["_id"])
            )
    if findings:
        n = len(findings)
        return (
            "fail",
            f"{plural(n, 'anti-phishing policy', 'anti-phishing policies')} {verb(n, 'weakens', 'weaken')} "
            "spoof protection",
            findings,
        )
    if not policies:
        return _unavailable("No anti-phishing policy was read")
    return (
        "pass",
        f"Spoof protection is on in {every(len(policies), 'anti-phishing policy', 'anti-phishing policies')}",
        [],
    )


@rule(
    "malware_file_filter",
    "Risky attachment types are blocked",
    "Email",
    "Turn on the common attachments filter in the anti-malware policies (it's on by default), so program files, "
    "disk images and scripts (.exe, .iso, .js and the like) are quarantined rather than delivered.",
)
def malware_file_filter(snap: TenantSnapshot, now: datetime):
    if missing := _protection_missing(snap, "anti_malware"):
        return missing
    policies = [p for p in mp.policies(snap, "anti_malware") if mp.in_effect(p)]
    findings = [
        Finding("medium", p.get("Name") or p["_id"], "The common attachments filter is off", p["_id"])
        for p in policies
        if p.get("EnableFileFilter") is False
    ]
    if findings:
        n = len(findings)
        return (
            "fail",
            f"{plural(n, 'anti-malware policy', 'anti-malware policies')} {verb(n, 'lets', 'let')} risky "
            "attachment types through",
            findings,
        )
    if not policies:
        return _unavailable("No anti-malware policy was read")
    return (
        "pass",
        "The common attachments filter is on in "
        + every(len(policies), "anti-malware policy", "anti-malware policies"),
        [],
    )
