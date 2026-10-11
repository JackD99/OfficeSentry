"""Checks on mail domains' DNS: SPF, DMARC, DKIM, MTA-STS and TLS-RPT."""

from __future__ import annotations

from datetime import datetime

from .. import dnscheck
from ..snapshot import TenantSnapshot
from ..text import every, plural, verb
from .base import Finding, _subjects, _unavailable, rule

NOT_RUN = "Domain checks haven't run yet, or the tenant has no custom domains"
DNS_FAILED = "The DNS lookups for this failed on every domain; the next collection tries again"


def _domain_rule(snap: TenantSnapshot, area: str):
    """Findings, the stored rows and the domains whose lookups for ``area`` failed (unknown, not fine), or
    None when domains haven't been checked."""
    rows = snap.objects("domain_health", "domain")
    if not rows:
        return None
    findings, unknown = [], []
    for row in rows:
        health = dnscheck.DomainHealth.from_dict(row)
        if health.unknown(area):
            unknown.append(health.domain)
            continue
        for issue in dnscheck.issues(health, transport=True):
            if issue.area == area:
                findings.append(Finding(issue.severity, health.domain, issue.detail, f"{health.domain}:{area}"))
    return findings, rows, unknown


def _result(status: str, summary: str, findings: list, unknown: list[str], area: str) -> tuple:
    """A check's result that names the domains it couldn't check and leaves their findings as they are."""
    if unknown:
        summary += f"; couldn't check {', '.join(sorted(unknown))} (DNS lookup failed)"
    return status, summary, findings, None, [f"{d}:{area}" for d in unknown]


def _transport_rule(snap: TenantSnapshot, area: str):
    """Findings, the mail domains checked for MTA-STS/TLS-RPT and the domains that couldn't be, or None and
    why not."""
    result = _domain_rule(snap, area)
    if result is None:
        return None, NOT_RUN
    findings, rows, unknown = result
    healths = list(map(dnscheck.DomainHealth.from_dict, rows))
    mail = [h for h in healths if h.sends_mail and not h.unknown(area)]
    if not mail:
        return None, DNS_FAILED if unknown else "No domain receives mail"
    checked = [h for h in mail if h.transport_checked]
    if not checked:
        return None, "Domains haven't been checked for MTA-STS yet; refresh Email domains on the tenant page"
    return (findings, checked, unknown), ""


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
    findings, mail, unknown = result
    return (
        _result(
            "fail",
            f"{_subjects(findings)} of {plural(len(mail), 'mail domain')} "
            + verb(_subjects(findings), "doesn't", "don't")
            + " fully enforce MTA-STS",
            findings,
            unknown,
            "MTA-STS",
        )
        if findings
        else _result("pass", f"MTA-STS is enforced on {every(len(mail), 'mail domain')}", [], unknown, "MTA-STS")
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
    findings, mail, unknown = result
    scope = [h for h in mail if h.mta_sts or h.tls_rpt]
    if not scope:
        return _unavailable("TLS-RPT is checked on mail domains that use MTA-STS, and none do yet")
    return (
        _result(
            "fail",
            f"{_subjects(findings)} of {plural(len(scope), 'domain')} without working TLS-RPT",
            findings,
            unknown,
            "TLS-RPT",
        )
        if findings
        else _result(
            "pass",
            f"TLS-RPT reports are set up on {every(len(scope), 'domain')} that "
            f"{verb(len(scope), 'uses', 'use')} MTA-STS or TLS-RPT",
            [],
            unknown,
            "TLS-RPT",
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
        return _unavailable(NOT_RUN)
    findings, rows, unknown = result
    if len(unknown) == len(rows):
        return _unavailable(DNS_FAILED)
    return (
        _result("fail", f"{plural(_subjects(findings), 'domain')} with SPF problems", findings, unknown, "SPF")
        if findings
        else _result("pass", f"SPF is correct on {every(len(rows) - len(unknown), 'domain')}", [], unknown, "SPF")
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
        return _unavailable(NOT_RUN)
    findings, rows, unknown = result
    if len(unknown) == len(rows):
        return _unavailable(DNS_FAILED)
    return (
        _result(
            "fail", f"{plural(_subjects(findings), 'domain')} without DMARC enforcement", findings, unknown, "DMARC"
        )
        if findings
        else _result("pass", f"DMARC is enforced on {every(len(rows) - len(unknown), 'domain')}", [], unknown, "DMARC")
    )


@rule(
    "dkim_enabled",
    "DKIM signing is on for every mail domain",
    "Email",
    "Turn on DKIM for each domain in the Defender portal (Email authentication settings > DKIM), "
    "publishing the two selector CNAME records it shows. This applies to domains behind a mail gateway too "
    "when Microsoft 365 still sends for them (their SPF includes spf.protection.outlook.com); a gateway "
    "signs only the mail that goes out through it.",
)
def dkim_enabled(snap: TenantSnapshot, now: datetime):
    result = _domain_rule(snap, "DKIM")
    if result is None:
        return _unavailable(NOT_RUN)
    findings, rows, unknown = result
    healths = list(map(dnscheck.DomainHealth.from_dict, rows))
    checked = [h for h in healths if h.sends_from_microsoft_365 and not h.unknown("DKIM")]
    if not checked:
        return _unavailable(DNS_FAILED if unknown else "No domain sends mail through Microsoft 365")
    gateway = sum(h.behind_gateway for h in checked)
    via = f" ({gateway} behind a mail gateway)" if gateway else ""
    return (
        _result("fail", f"{plural(len(findings), 'mail domain')} without DKIM{via}", findings, unknown, "DKIM")
        if findings
        else _result(
            "pass",
            f"DKIM is on for {every(len(checked), 'domain')} Microsoft 365 sends for{via}",
            [],
            unknown,
            "DKIM",
        )
    )
