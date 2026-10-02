"""Email domain health from public DNS: MX, SPF, DMARC, DKIM (Microsoft 365 selectors), MTA-STS.

``lookup`` is the only function that touches the network; everything else is
pure, so tests replace ``lookup`` and the same analysis serves both client
tenants and prospect assessments.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field

SPF_LOOKUP_LIMIT = 10
M365_MX = "mail.protection.outlook.com"
DKIM_SELECTORS = ("selector1", "selector2")


class DnsError(Exception):
    pass


def lookup(name: str, rdtype: str) -> list[str]:
    """Records as text: TXT strings joined, MX as 'host' (lowest preference first), CNAME targets.

    Returns [] when the name or record type doesn't exist.
    """
    import dns.exception
    import dns.resolver

    resolver = dns.resolver.Resolver()
    resolver.lifetime = 8
    try:
        answer = resolver.resolve(name, rdtype)
    except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer, dns.resolver.NoNameservers):
        return []
    except dns.exception.DNSException as exc:
        raise DnsError(f"{name} {rdtype}: {exc}") from exc
    if rdtype == "TXT":
        return [b"".join(r.strings).decode("utf-8", "replace") for r in answer]
    if rdtype == "MX":
        return [str(r.exchange).rstrip(".").lower() for r in sorted(answer, key=lambda r: r.preference)]
    return [str(r.target if hasattr(r, "target") else r).rstrip(".").lower() for r in answer]


@dataclass
class DomainHealth:
    domain: str
    mx: list[str] = field(default_factory=list)
    spf: str | None = None
    spf_records: int = 0
    spf_all: str | None = None  # -all, ~all, ?all, +all, or None
    spf_lookups: int = 0
    dmarc: str | None = None
    dmarc_policy: str | None = None
    dmarc_subdomain_policy: str | None = None
    dmarc_pct: int = 100
    dmarc_reports: bool = False
    dkim: dict[str, bool] = field(default_factory=dict)  # selector -> CNAME published
    mta_sts: bool = False
    errors: list[str] = field(default_factory=list)

    @property
    def sends_mail(self) -> bool:
        return bool(self.mx)

    @property
    def microsoft_365(self) -> bool:
        return any(h.endswith(M365_MX) for h in self.mx)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> DomainHealth:
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


def _tag(record: str, name: str) -> str | None:
    m = re.search(rf"(?:^|;)\s*{name}\s*=\s*([^;]+)", record, re.I)
    return m.group(1).strip() if m else None


def _spf_record(records: list[str]) -> list[str]:
    return [r for r in records if r.lower().startswith("v=spf1")]


def count_spf_lookups(record: str, resolve=None, depth: int = 0) -> int:
    """DNS lookups an SPF check makes, following include/redirect (RFC 7208 limit is 10).

    A domain included twice is counted twice, as receivers do.
    """
    resolve = resolve or lookup
    count = 0
    for term in record.split()[1:]:
        t = term.lstrip("+-~?").lower()
        if t.startswith(("include:", "redirect=")):
            count += 1
            target = t.split(":", 1)[1] if t.startswith("include:") else t.split("=", 1)[1]
            if depth < 5:
                try:
                    nested = _spf_record(resolve(target, "TXT"))
                except DnsError:
                    nested = []
                if nested:
                    count += count_spf_lookups(nested[0], resolve, depth + 1)
        elif t in ("a", "mx", "ptr") or t.startswith(("a:", "a/", "mx:", "mx/", "ptr:", "exists:")):
            count += 1
    return count


def check_domain(domain: str, resolve=None) -> DomainHealth:
    resolve = resolve or lookup
    h = DomainHealth(domain.lower())

    def safe(name, rdtype):
        try:
            return resolve(name, rdtype)
        except DnsError as exc:
            h.errors.append(str(exc))
            return []

    h.mx = safe(h.domain, "MX")
    spf = _spf_record(safe(h.domain, "TXT"))
    h.spf_records = len(spf)
    if spf:
        h.spf = spf[0]
        terms = [t.lower() for t in h.spf.split()]
        h.spf_all = next((t if t[0] in "+-~?" else "+all" for t in terms if t.lstrip("+-~?") == "all"), None)
        if h.spf_all is None and any(t.startswith("redirect=") for t in terms):
            h.spf_all = "redirect"
        h.spf_lookups = count_spf_lookups(h.spf, resolve)
    dmarc = [r for r in safe(f"_dmarc.{h.domain}", "TXT") if r.lower().startswith("v=dmarc1")]
    if dmarc:
        h.dmarc = dmarc[0]
        h.dmarc_policy = (_tag(h.dmarc, "p") or "").lower() or None
        h.dmarc_subdomain_policy = (_tag(h.dmarc, "sp") or "").lower() or None
        pct = _tag(h.dmarc, "pct")
        h.dmarc_pct = int(pct) if pct and pct.isdigit() else 100
        h.dmarc_reports = bool(_tag(h.dmarc, "rua"))
    for selector in DKIM_SELECTORS:
        h.dkim[selector] = bool(safe(f"{selector}._domainkey.{h.domain}", "CNAME"))
    h.mta_sts = any(r.lower().startswith("v=stsv1") for r in safe(f"_mta-sts.{h.domain}", "TXT"))
    return h


@dataclass
class Issue:
    severity: str
    area: str  # SPF | DMARC | DKIM
    detail: str


def issues(h: DomainHealth) -> list[Issue]:
    """What's wrong with a domain's email authentication, most serious first."""
    out: list[Issue] = []
    if not h.sends_mail:
        # A domain that never sends mail should say so, or it can be spoofed freely.
        if not h.spf or h.spf_all != "-all":
            out.append(Issue("low", "SPF", "Doesn't receive mail but has no \"v=spf1 -all\" record to stop spoofing"))
        if h.dmarc_policy != "reject":
            out.append(Issue("low", "DMARC", "Doesn't receive mail but has no DMARC p=reject record"))
        return out

    if h.spf_records == 0:
        out.append(Issue("high", "SPF", "No SPF record: anyone can send as this domain"))
    elif h.spf_records > 1:
        out.append(Issue("high", "SPF", f"{h.spf_records} SPF records; receivers treat this as no SPF at all"))
    else:
        if h.spf_all == "+all":
            out.append(Issue("high", "SPF", "SPF ends in +all, which allows every server on the internet"))
        elif h.spf_all in (None, "?all"):
            out.append(Issue("medium", "SPF", "SPF doesn't end in -all or ~all, so it doesn't reject anything"))
        if h.spf_lookups > SPF_LOOKUP_LIMIT:
            out.append(Issue("medium", "SPF", f"SPF needs {h.spf_lookups} DNS lookups (limit 10), so it fails"))

    if not h.dmarc:
        out.append(Issue("high", "DMARC", "No DMARC record: spoofed mail from this domain is delivered"))
    elif h.dmarc_policy == "none":
        out.append(Issue("medium", "DMARC", "DMARC is monitor-only (p=none); spoofed mail is still delivered"))
    elif h.dmarc_policy in ("quarantine", "reject") and h.dmarc_pct < 100:
        out.append(Issue("low", "DMARC", f"DMARC applies to only {h.dmarc_pct}% of mail"))
    if h.dmarc and not h.dmarc_reports:
        out.append(Issue("low", "DMARC", "DMARC has no rua= address, so nobody sees the reports"))

    if h.microsoft_365 and not all(h.dkim.get(s) for s in DKIM_SELECTORS):
        missing = [s for s in DKIM_SELECTORS if not h.dkim.get(s)]
        out.append(Issue("medium", "DKIM", f"Microsoft 365 DKIM isn't set up ({', '.join(missing)} CNAME missing)"))
    rank = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    return sorted(out, key=lambda i: rank[i.severity])


def summary(h: DomainHealth) -> dict[str, str]:
    """One plain word per area, for tables: Pass / Weak / Missing / n/a."""
    found = {i.area: i.severity for i in issues(h)}

    def word(area: str, present: bool) -> str:
        if not present:
            return "Missing"
        return {"high": "Fail", "medium": "Weak", "low": "OK"}.get(found.get(area), "Pass")

    dkim = "n/a" if not h.microsoft_365 else word("DKIM", any(h.dkim.values()))
    return {"SPF": word("SPF", bool(h.spf)), "DMARC": word("DMARC", bool(h.dmarc)), "DKIM": dkim}
