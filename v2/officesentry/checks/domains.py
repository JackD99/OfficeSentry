"""Checks on mail domains' DNS: SPF, DMARC, DKIM, MTA-STS and TLS-RPT."""

from __future__ import annotations

from datetime import datetime

from .. import dnscheck
from ..snapshot import TenantSnapshot
from ..text import every, plural, verb
from .base import Finding, _subjects, _unavailable, rule


def _domain_rule(snap: TenantSnapshot, area: str):
    rows = snap.objects("domain_health", "domain")
    if not rows:
        return None
    findings = []
    for row in rows:
        health = dnscheck.DomainHealth.from_dict(row)
        for issue in dnscheck.issues(health, transport=True):
            if issue.area == area:
                findings.append(Finding(issue.severity, health.domain, issue.detail, f"{health.domain}:{area}"))
    return findings, rows


def _transport_rule(snap: TenantSnapshot, area: str):
    """Findings and the mail domains checked for MTA-STS/TLS-RPT, or None and why not."""
    result = _domain_rule(snap, area)
    if result is None:
        return None, "Domain checks haven't run yet, or the tenant has no custom domains"
    findings, rows = result
    mail = [h for h in map(dnscheck.DomainHealth.from_dict, rows) if h.sends_mail]
    if not mail:
        return None, "No domain receives mail"
    checked = [h for h in mail if h.transport_checked]
    if not checked:
        return None, "Domains haven't been checked for MTA-STS yet; refresh Email domains on the tenant page"
    return (findings, checked), ""


@rule(
    "mta_sts",
    "Mail domains require encrypted delivery (MTA-STS)",
    "Email",
    "Publish an MTA-STS policy so other mail servers only deliver to the domain over a verified, encrypted "
    'connection: a TXT record at _mta-sts.<domain> ("v=STSv1; id=20260101") and a policy file at '
    "https://mta-sts.<domain>/.well-known/mta-sts.txt listing the domain's MX hosts (for Microsoft 365, "
    '"mx: *.mail.protection.outlook.com"). Start with "mode: testing" and TLS-RPT reports, then switch to '
    '"mode: enforce" and change the id.',
)
def mta_sts(snap: TenantSnapshot, now: datetime):
    result, reason = _transport_rule(snap, "MTA-STS")
    if result is None:
        return _unavailable(reason)
    findings, mail = result
    return (
        (
            "fail",
            f"{_subjects(findings)} of {plural(len(mail), 'mail domain')} "
            + verb(_subjects(findings), "doesn't", "don't")
            + " fully enforce MTA-STS",
            findings,
        )
        if findings
        else ("pass", f"MTA-STS is enforced on {every(len(mail), 'mail domain')}", [])
    )


@rule(
    "tls_rpt",
    "Encrypted delivery failures are reported (TLS-RPT)",
    "Email",
    'Publish a TXT record at _smtp._tls.<domain> such as "v=TLSRPTv1; rua=mailto:tls-reports@<domain>" '
    "(or a reporting service's address), so someone hears when other servers can't deliver mail encrypted, "
    "before and after MTA-STS is enforced.",
)
def tls_rpt(snap: TenantSnapshot, now: datetime):
    result, reason = _transport_rule(snap, "TLS-RPT")
    if result is None:
        return _unavailable(reason)
    findings, mail = result
    scope = [h for h in mail if h.mta_sts or h.tls_rpt]
    if not scope:
        return _unavailable("TLS-RPT is checked on mail domains that use MTA-STS, and none do yet")
    return (
        ("fail", f"{_subjects(findings)} of {plural(len(scope), 'domain')} without working TLS-RPT", findings)
        if findings
        else (
            "pass",
            f"TLS-RPT reports are set up on {every(len(scope), 'domain')} "
            f"that {verb(len(scope), 'uses', 'use')} MTA-STS or TLS-RPT",
            [],
        )
    )


@rule(
    "spf_record",
    "Every domain has a working SPF record",
    "Email",
    "Publish one SPF record per domain ending in -all (or ~all while testing), e.g. "
    '"v=spf1 include:spf.protection.outlook.com -all". Domains that never send mail should publish '
    '"v=spf1 -all".',
)
def spf_record(snap: TenantSnapshot, now: datetime):
    result = _domain_rule(snap, "SPF")
    if result is None:
        return _unavailable("Domain checks haven't run yet, or the tenant has no custom domains")
    findings, rows = result
    return (
        ("fail", f"{plural(_subjects(findings), 'domain')} with SPF problems", findings)
        if findings
        else ("pass", f"SPF is correct on {every(len(rows), 'domain')}", [])
    )


@rule(
    "dmarc_policy",
    "Every domain enforces DMARC",
    "Email",
    "Publish a DMARC record with a rua= reporting address, start at p=none to see who sends as the domain, "
    "then move to p=quarantine and p=reject. Without enforcement, spoofed mail from the domain is delivered.",
)
def dmarc_policy(snap: TenantSnapshot, now: datetime):
    result = _domain_rule(snap, "DMARC")
    if result is None:
        return _unavailable("Domain checks haven't run yet, or the tenant has no custom domains")
    findings, rows = result
    return (
        ("fail", f"{plural(_subjects(findings), 'domain')} without DMARC enforcement", findings)
        if findings
        else ("pass", f"DMARC is enforced on {every(len(rows), 'domain')}", [])
    )


@rule(
    "dkim_enabled",
    "DKIM signing is on for every mail domain",
    "Email",
    "Turn on DKIM for each domain in the Defender portal (Email authentication settings > DKIM), "
    "publishing the two selector CNAME records it shows.",
)
def dkim_enabled(snap: TenantSnapshot, now: datetime):
    result = _domain_rule(snap, "DKIM")
    if result is None:
        return _unavailable("Domain checks haven't run yet, or the tenant has no custom domains")
    findings, rows = result
    checked = [r for r in rows if dnscheck.DomainHealth.from_dict(r).microsoft_365]
    if not checked:
        return _unavailable("No domain receives mail through Microsoft 365")
    return (
        ("fail", f"{plural(len(findings), 'mail domain')} without DKIM", findings)
        if findings
        else ("pass", f"DKIM is on for {every(len(checked), 'Microsoft 365 mail domain')}", [])
    )
