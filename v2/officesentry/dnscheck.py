"""Email domain health from public DNS: MX, SPF, DMARC, DKIM (Microsoft 365 selectors), MTA-STS, TLS-RPT.

``lookup`` (DNS) and ``fetch_mta_sts`` (the MTA-STS policy file over HTTPS)
are the only functions that touch the network; everything else is pure, so
tests replace them.
"""

from __future__ import annotations

import re
import time
from dataclasses import asdict, dataclass, field

SPF_LOOKUP_LIMIT = 10
M365_MX = "mail.protection.outlook.com"
DKIM_SELECTORS = ("selector1", "selector2")
# Microsoft's own SPF include is always published; if it doesn't resolve, the problem is ours, not the domain's.
SPF_TRUSTED_INCLUDES = ("spf.protection.outlook.com",)
MTA_STS_TIMEOUT = 5  # seconds, per request and in total
MTA_STS_MAX_BYTES = 64 * 1024  # RFC 8461: senders may ignore larger policies
MTA_STS_MAX_AGE = 31557600  # RFC 8461 maximum, one year
MTA_STS_MODES = ("enforce", "testing", "none")
TRANSPORT_AREAS = ("MTA-STS", "TLS-RPT")  # encrypted delivery to the domain, not spoofing
UNKNOWN = "Couldn't check"  # a table's word for an area whose DNS lookups failed


class DnsError(Exception):
    pass


class FetchError(Exception):
    pass


def lookup(name: str, rdtype: str) -> list[str]:
    """Records as text: TXT strings joined, MX as 'host' (lowest preference first), CNAME targets.

    Returns [] when the name or record type doesn't exist. Raises DnsError when the answer is unknown: a
    timeout, or a resolver that answered SERVFAIL or REFUSED or couldn't be reached (dnspython's
    NoNameservers), which says nothing about the domain's records.
    """
    import dns.exception
    import dns.resolver

    resolver = dns.resolver.Resolver()
    resolver.lifetime = 8
    try:
        answer = resolver.resolve(name, rdtype)
    except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer):
        return []
    except dns.exception.DNSException as exc:
        raise DnsError(f"{name} {rdtype}: {exc}") from exc
    if rdtype == "TXT":
        return [b"".join(r.strings).decode("utf-8", "replace") for r in answer]
    if rdtype == "MX":
        return [str(r.exchange).rstrip(".").lower() for r in sorted(answer, key=lambda r: r.preference)]
    return [str(r.target if hasattr(r, "target") else r).rstrip(".").lower() for r in answer]


def fetch_mta_sts(domain: str) -> str:
    """The domain's MTA-STS policy file, fetched the way sending servers do (RFC 8461).

    HTTPS with a valid certificate, no redirects, a short timeout and a size
    cap. Raises FetchError with a plain reason; never anything else.
    """
    import requests

    from . import outbound

    host = f"mta-sts.{domain}"
    url = f"https://{host}/.well-known/mta-sts.txt"
    # Domains are typed by analysts: same address filter as webhooks, and connect to the vetted address.
    try:
        address = outbound.public_address(host)
    except outbound.LookupFailed:
        raise FetchError(f"couldn't connect to {host}") from None
    except outbound.NotPublic as exc:
        raise FetchError(f"{host} resolves to an internal address ({exc.address}), so it wasn't fetched") from None
    deadline = time.monotonic() + MTA_STS_TIMEOUT
    http, host_header = outbound.pinned_session(url, address)
    try:
        with (
            http,
            http.get(url, timeout=MTA_STS_TIMEOUT, allow_redirects=False, stream=True, headers=host_header) as resp,
        ):
            if resp.is_redirect:
                raise FetchError(
                    f"{url} redirects to {resp.headers.get('Location')}, and sending servers don't follow redirects"
                )
            if resp.status_code != 200:
                raise FetchError(f"{url} returned HTTP {resp.status_code}")
            body = b""
            for chunk in resp.iter_content(8192):
                body += chunk
                if len(body) > MTA_STS_MAX_BYTES:
                    raise FetchError(f"{url} is larger than {MTA_STS_MAX_BYTES // 1024} KB")
                if time.monotonic() > deadline:
                    raise FetchError(f"{url} took longer than {MTA_STS_TIMEOUT} seconds")
    except requests.exceptions.SSLError:
        raise FetchError(f"mta-sts.{domain} has no valid HTTPS certificate") from None
    except requests.exceptions.Timeout:
        raise FetchError(f"{url} took longer than {MTA_STS_TIMEOUT} seconds") from None
    except requests.exceptions.RequestException:
        raise FetchError(f"couldn't connect to mta-sts.{domain}") from None
    try:
        return body.decode("utf-8")
    except UnicodeDecodeError:
        raise FetchError(f"{url} isn't a text file") from None


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
    mta_sts: bool = False  # a v=STSv1 record at _mta-sts.<domain>
    # Added later: stored snapshots without these fields load with the defaults.
    spf_missing_includes: list[str] = field(default_factory=list)  # include/redirect targets with no SPF record
    dmarc_records: int = 0
    mta_sts_records: int = 0
    mta_sts_id: str | None = None
    mta_sts_mode: str | None = None  # enforce | testing | none, from the policy file
    mta_sts_mx: list[str] = field(default_factory=list)
    mta_sts_max_age: int | None = None
    mta_sts_error: str | None = None  # why the policy file can't be used
    tls_rpt: str | None = None
    tls_rpt_records: int = 0
    tls_rpt_reports: bool = False  # rua= has a mailto: or https: address
    transport_checked: bool = False  # MTA-STS policy and TLS-RPT were checked (not in older snapshots)
    dmarc_from: str | None = None  # the parent domain whose DMARC record covers this one, when it has none
    errors: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)  # areas whose lookups failed ("MX", "SPF", ...): unknown

    @property
    def sends_mail(self) -> bool:
        return any(self.mx)  # a null MX ("0 .") says the domain accepts no mail

    @property
    def sends_only(self) -> bool:
        """Receives no mail but sends it: its SPF record authorises servers (a marketing or notifications
        domain). Telling it to publish "v=spf1 -all" would stop its mail."""
        terms = (self.spf or "").lower().split()[1:]
        return not self.sends_mail and any(t.lstrip("+-~?") != "all" for t in terms)

    @property
    def microsoft_365(self) -> bool:
        """Mail for the domain arrives through Microsoft 365 (its MX records)."""
        return any(h.endswith(M365_MX) for h in self.mx)

    @property
    def sends_from_microsoft_365(self) -> bool:
        """Microsoft 365 sends mail as the domain: its MX is Microsoft 365, or its SPF record authorises
        Microsoft 365, as it does for a domain behind a third-party mail gateway whose users still send
        from Outlook. Either way Microsoft 365 should sign that mail with DKIM."""
        if self.microsoft_365:
            return True
        terms = (self.spf or "").lower().split()
        return any(t.lstrip("+-~?").partition(":")[2] in SPF_TRUSTED_INCLUDES for t in terms if ":" in t)

    @property
    def behind_gateway(self) -> bool:
        """Mail arrives through another provider (a gateway such as Mimecast or Proofpoint) and goes on to
        Microsoft 365, which still sends for the domain."""
        return self.sends_mail and not self.microsoft_365 and self.sends_from_microsoft_365

    def unknown(self, area: str) -> bool:
        """Whether a failed lookup leaves this area unknown. Without the MX records nothing is known, since
        whether the domain receives mail decides what it should publish."""
        if "MX" in self.failed or area in self.failed:
            return True
        # One failed MTA-STS or TLS-RPT lookup stops both being checked (``transport_checked``).
        return area in TRANSPORT_AREAS and not self.transport_checked and any(a in self.failed for a in TRANSPORT_AREAS)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> DomainHealth:
        """Rebuild from a stored snapshot; missing or mistyped fields (older or damaged rows) fall back to defaults."""
        defaults = cls(domain=str(data.get("domain") or data.get("_id") or "?"))
        values = {}
        for name in cls.__dataclass_fields__:
            value, default = data.get(name), getattr(defaults, name)
            if name == "domain":
                value = defaults.domain
            elif (
                value is None
                or (default is not None and not isinstance(value, type(default)))
                or (default is None and isinstance(value, (list, dict, bool)))
                or isinstance(value, bool) != isinstance(default, bool)
            ):
                value = default
            values[name] = value
        return cls(**values)


def _dmarc_records(answer: list[str]) -> list[str]:
    return [r for r in answer if r.lower().startswith("v=dmarc1")]


def _tag(record: str, name: str) -> str | None:
    m = re.search(rf"(?:^|;)\s*{name}\s*=\s*([^;]+)", record, re.I)
    return m.group(1).strip() if m else None


def _spf_record(records: list[str]) -> list[str]:
    return [r for r in records if r.lower().startswith("v=spf1")]


def count_spf_lookups(record: str, resolve=None, depth: int = 0, missing: list[str] | None = None) -> int:
    """DNS lookups an SPF check makes, following include/redirect (RFC 7208 limit is 10).

    A domain included twice is counted twice, as receivers do. Include and
    redirect targets that publish no SPF record, which makes the whole check
    fail, are added to ``missing`` when it's given.
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
                    nested = None  # unknown, not missing
                if nested:
                    count += count_spf_lookups(nested[0], resolve, depth + 1, missing)
                elif (
                    nested is not None
                    and missing is not None
                    and "%" not in target
                    and target not in SPF_TRUSTED_INCLUDES
                    and target not in missing
                ):
                    missing.append(target)
        elif t in ("a", "mx", "ptr") or t.startswith(("a:", "a/", "mx:", "mx/", "ptr:", "exists:")):
            count += 1
    return count


def check_domain(domain: str, resolve=None, fetch=None) -> DomainHealth:
    resolve = resolve or lookup
    h = DomainHealth(domain.lower())

    def safe(name, rdtype, area):
        try:
            return resolve(name, rdtype)
        except DnsError as exc:
            h.errors.append(str(exc))
            if area not in h.failed:
                h.failed.append(area)
            return []

    h.mx = safe(h.domain, "MX", "MX")
    spf = _spf_record(safe(h.domain, "TXT", "SPF"))
    h.spf_records = len(spf)
    if spf:
        h.spf = spf[0]
        terms = [t.lower() for t in h.spf.split()]
        h.spf_all = next((t if t[0] in "+-~?" else "+all" for t in terms if t.lstrip("+-~?") == "all"), None)
        if h.spf_all is None and any(t.startswith("redirect=") for t in terms):
            h.spf_all = "redirect"
        h.spf_lookups = count_spf_lookups(h.spf, resolve, missing=h.spf_missing_includes)
    dmarc = _dmarc_records(safe(f"_dmarc.{h.domain}", "TXT", "DMARC"))
    if not dmarc and "DMARC" not in h.failed:
        # A subdomain with no record of its own is covered by its parent's, with the parent's sp= policy.
        # Receivers walk up the names to find it (DMARCbis), so this does too, short of the top-level domain.
        # A public suffix's record (psd=y) isn't the client's, so it isn't taken as theirs.
        labels = h.domain.split(".")
        for i in range(1, len(labels) - 1):
            parent = ".".join(labels[i:])
            found = [
                r
                for r in _dmarc_records(safe(f"_dmarc.{parent}", "TXT", "DMARC"))
                if (_tag(r, "psd") or "").lower() != "y"
            ]
            if found:
                dmarc, h.dmarc_from = found, parent
                break
    h.dmarc_records = len(dmarc)
    if dmarc:
        h.dmarc = dmarc[0]
        h.dmarc_policy = (_tag(h.dmarc, "p") or "").lower() or None
        h.dmarc_subdomain_policy = (_tag(h.dmarc, "sp") or "").lower() or None
        if h.dmarc_from:
            h.dmarc_policy = h.dmarc_subdomain_policy or h.dmarc_policy
        pct = _tag(h.dmarc, "pct")
        h.dmarc_pct = int(pct) if pct and pct.isdigit() else 100
        h.dmarc_reports = bool(_tag(h.dmarc, "rua"))
    for selector in DKIM_SELECTORS:
        h.dkim[selector] = bool(safe(f"{selector}._domainkey.{h.domain}", "CNAME", "DKIM"))
    failed = len(h.errors)
    sts = [r for r in safe(f"_mta-sts.{h.domain}", "TXT", "MTA-STS") if r.lower().startswith("v=stsv1")]
    h.mta_sts, h.mta_sts_records = bool(sts), len(sts)
    if sts:
        h.mta_sts_id = _tag(sts[0], "id")
    rpt = [r for r in safe(f"_smtp._tls.{h.domain}", "TXT", "TLS-RPT") if r.lower().startswith("v=tlsrptv1")]
    h.tls_rpt_records = len(rpt)
    if rpt:
        h.tls_rpt = rpt[0]
        h.tls_rpt_reports = any(
            u.strip().lower().startswith(("mailto:", "https:")) for u in (_tag(rpt[0], "rua") or "").split(",")
        )
    if len(h.errors) > failed:
        return h  # unknown rather than missing: transport_checked stays False
    if h.mta_sts and h.sends_mail:
        try:
            policy = parse_mta_sts_policy((fetch or fetch_mta_sts)(h.domain))
        except FetchError as exc:
            h.mta_sts_error = str(exc)
        else:
            h.mta_sts_mode, h.mta_sts_mx, h.mta_sts_max_age = policy.mode, policy.mx, policy.max_age
            h.mta_sts_error = policy.error
    h.transport_checked = True
    return h


@dataclass
class StsPolicy:
    mode: str | None = None
    mx: list[str] = field(default_factory=list)
    max_age: int | None = None
    error: str | None = None  # why senders would ignore the policy


def parse_mta_sts_policy(text: str) -> StsPolicy:
    """Read an MTA-STS policy file: "key: value" lines (RFC 8461 section 3.2)."""
    fields: dict[str, list[str]] = {}
    for line in text.splitlines():
        key, sep, value = line.partition(":")
        if sep:
            fields.setdefault(key.strip().lower(), []).append(value.strip())
    p = StsPolicy()
    p.mode = (fields.get("mode") or [""])[0].lower() or None
    p.mx = [m.lower().rstrip(".") for m in fields.get("mx", []) if m]
    age = (fields.get("max_age") or [""])[0]
    p.max_age = int(age) if age.isdigit() else None
    if (fields.get("version") or [""])[0] != "STSv1":
        p.error = 'the policy file has no "version: STSv1" line'
    elif p.mode not in MTA_STS_MODES:
        p.error = "the policy's mode isn't enforce, testing or none"
    elif p.max_age is None or p.max_age > MTA_STS_MAX_AGE:
        p.error = "the policy's max_age is missing or isn't a number of seconds up to a year"
    elif p.mode != "none" and not p.mx:
        p.error = "the policy lists no mx servers"
    return p


def mx_matches(pattern: str, host: str) -> bool:
    """MTA-STS mx pattern match: exact, or "*." standing for exactly one leftmost label."""
    pattern, host = pattern.lower().rstrip("."), host.lower().rstrip(".")
    if pattern.startswith("*."):
        suffix = pattern[1:]
        label = host[: -len(suffix)] if host.endswith(suffix) else ""
        return bool(label) and "." not in label
    return host == pattern


@dataclass
class Issue:
    severity: str
    area: str  # SPF | DMARC | DKIM | MTA-STS | TLS-RPT
    detail: str


def issues(h: DomainHealth, transport: bool = False) -> list[Issue]:
    """What's wrong with a domain's email authentication, most serious first.

    With ``transport``, also MTA-STS and TLS-RPT: whether mail sent to the
    domain has to arrive encrypted.
    """
    out: list[Issue] = []
    if not h.sends_mail and not h.sends_only:
        # A domain that never sends mail should say so, or it can be spoofed freely.
        if not h.spf or h.spf_all != "-all":
            out.append(
                Issue("low", "SPF", 'Needs a "v=spf1 -all" record saying it sends no mail, or anyone can send as it')
            )
        if h.dmarc_policy != "reject":
            out.append(
                Issue("low", "DMARC", "Needs a DMARC p=reject record, or spoofed mail from it is still delivered")
            )
        return [i for i in out if not h.unknown(i.area)]

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
        if h.spf_missing_includes:
            they = "it has" if len(h.spf_missing_includes) == 1 else "they have"
            out.append(
                Issue(
                    "medium",
                    "SPF",
                    f"SPF includes {', '.join(h.spf_missing_includes)}, but {they} no "
                    "SPF record, so SPF fails for all mail",
                )
            )
        if any(t.lstrip("+-~?").lower().split(":")[0] == "ptr" for t in (h.spf or "").split()[1:]):
            out.append(Issue("low", "SPF", "SPF uses the ptr mechanism, which is slow and ignored by some receivers"))

    if not h.dmarc:
        out.append(Issue("high", "DMARC", "No DMARC record: spoofed mail from this domain is delivered"))
    elif h.dmarc_records > 1:
        out.append(Issue("high", "DMARC", f"{h.dmarc_records} DMARC records; receivers treat this as no DMARC at all"))
    elif h.dmarc_policy not in ("none", "quarantine", "reject"):
        out.append(
            Issue("medium", "DMARC", "DMARC record has no valid p= policy, so receivers treat it as p=none")
            if h.dmarc_reports
            else Issue("high", "DMARC", "DMARC record has no valid p= policy, so receivers ignore it")
        )
    elif h.dmarc_policy == "none":
        out.append(Issue("medium", "DMARC", "DMARC is monitor-only (p=none); spoofed mail is still delivered"))
    elif h.dmarc_policy in ("quarantine", "reject") and h.dmarc_pct < 100:
        out.append(Issue("low", "DMARC", f"DMARC applies to only {h.dmarc_pct}% of mail"))
    if h.dmarc_records <= 1 and h.dmarc_policy in ("quarantine", "reject") and h.dmarc_subdomain_policy == "none":
        out.append(
            Issue(
                "low",
                "DMARC",
                "DMARC doesn't cover subdomains (sp=none), so mail from made-up subdomains is still delivered",
            )
        )
    if h.dmarc and not h.dmarc_reports:
        out.append(Issue("low", "DMARC", "DMARC has no rua= address, so nobody sees the reports"))

    if h.sends_from_microsoft_365 and not all(h.dkim.get(s) for s in DKIM_SELECTORS):
        missing = [s for s in DKIM_SELECTORS if not h.dkim.get(s)]
        why = (
            ". Mail arrives through a gateway, but SPF says Microsoft 365 sends for this domain, so its DKIM "
            "signing is still needed"
            if h.behind_gateway
            else ""
        )
        out.append(
            Issue("medium", "DKIM", f"Microsoft 365 DKIM isn't set up ({', '.join(missing)} CNAME missing){why}")
        )
    if h.dmarc_from:
        out = [
            Issue(i.severity, i.area, f"{i.detail} (from {h.dmarc_from}'s record)") if i.area == "DMARC" else i
            for i in out
        ]
    if transport and h.transport_checked and h.sends_mail:  # MTA-STS and TLS-RPT are for receiving mail
        out += _transport_issues(h)
    rank = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    return sorted((i for i in out if not h.unknown(i.area)), key=lambda i: rank[i.severity])


def _valid_sts_record(h: DomainHealth) -> bool:
    return h.mta_sts_records == 1 and bool(re.fullmatch(r"[A-Za-z0-9]{1,32}", h.mta_sts_id or ""))


def _transport_issues(h: DomainHealth) -> list[Issue]:
    """At most one MTA-STS and one TLS-RPT issue for a domain that receives mail."""
    out: list[Issue] = []
    if not h.mta_sts:
        out.append(
            Issue(
                "low",
                "MTA-STS",
                "No MTA-STS policy, so mail sent to this domain can be forced onto an unencrypted connection",
            )
        )
    elif h.mta_sts_records > 1:
        out.append(Issue("low", "MTA-STS", f"{h.mta_sts_records} MTA-STS records; senders ignore them all"))
    elif not _valid_sts_record(h):
        out.append(
            Issue(
                "low", "MTA-STS", "MTA-STS record has no valid id= (1 to 32 letters and digits), so senders ignore it"
            )
        )
    elif h.mta_sts_error:
        out.append(Issue("low", "MTA-STS", f"MTA-STS policy can't be used: {h.mta_sts_error}"))
    elif h.mta_sts_mode == "none":
        out.append(Issue("low", "MTA-STS", "MTA-STS policy is switched off (mode: none)"))
    elif h.mta_sts_mode in ("enforce", "testing"):
        servers = [m for m in h.mx if m]
        unlisted = [m for m in servers if not any(mx_matches(p, m) for p in h.mta_sts_mx)]
        if h.mta_sts_mode == "testing":
            out.append(
                Issue(
                    "low",
                    "MTA-STS",
                    "MTA-STS is in testing mode, so senders still deliver unencrypted "
                    "when encryption fails" + (f"; the policy doesn't list {', '.join(unlisted)}" if unlisted else ""),
                )
            )
        elif unlisted and len(unlisted) == len(servers):
            out.append(
                Issue(
                    "medium",
                    "MTA-STS",
                    f"MTA-STS is enforced but doesn't list {', '.join(unlisted)}, "
                    "so senders that check it won't deliver mail",
                )
            )
        elif unlisted:
            those = "that server" if len(unlisted) == 1 else "those servers"
            out.append(
                Issue(
                    "low",
                    "MTA-STS",
                    f"MTA-STS doesn't list {', '.join(unlisted)}, so senders that check it won't use {those}",
                )
            )
    if h.tls_rpt_records > 1:
        out.append(Issue("low", "TLS-RPT", f"{h.tls_rpt_records} TLS-RPT records; senders ignore them all"))
    elif h.tls_rpt and not h.tls_rpt_reports:
        out.append(
            Issue("low", "TLS-RPT", "TLS-RPT record has no mailto: or https: rua= address, so no reports arrive")
        )
    elif not h.tls_rpt and h.mta_sts:
        out.append(
            Issue("low", "TLS-RPT", "No TLS-RPT record, so nobody hears when encrypted delivery to this domain fails")
        )
    return out


def summary(h: DomainHealth) -> dict[str, str]:
    """A word or two per area, for tables: Pass / Weak / Missing, or Couldn't check. DKIM is "Not needed" for a
    domain that sends no mail and "Not via Microsoft 365" when Microsoft 365 doesn't send as the domain."""
    found: dict[str, str] = {}
    for i in issues(h):  # most serious first
        found.setdefault(i.area, i.severity)

    def word(area: str, present: bool) -> str:
        if h.unknown(area):
            return UNKNOWN
        if not present:
            return "Missing"
        return {"high": "Fail", "medium": "Weak", "low": "OK"}.get(found.get(area), "Pass")

    if h.unknown("DKIM") or h.sends_from_microsoft_365:
        dkim = word("DKIM", any(h.dkim.values()))
    elif h.sends_mail or h.sends_only:
        dkim = "Not via Microsoft 365"
    else:
        dkim = "Not needed"
    return {"SPF": word("SPF", bool(h.spf)), "DMARC": word("DMARC", bool(h.dmarc)), "DKIM": dkim}


def transport_summary(h: DomainHealth) -> dict[str, str]:
    """MTA-STS (Enforced / Testing / Off / Fail / Missing) and TLS-RPT (Pass / Fail / Missing), for tables; both
    "Not needed" for a domain that receives no mail."""
    if "MX" in h.failed:
        return {"MTA-STS": UNKNOWN, "TLS-RPT": UNKNOWN}
    if not h.transport_checked:
        return {"MTA-STS": "Not checked", "TLS-RPT": "Not checked"}
    if not h.sends_mail:
        return {"MTA-STS": "Not needed", "TLS-RPT": "Not needed"}
    if not h.mta_sts:
        sts = "Missing"
    elif not _valid_sts_record(h) or h.mta_sts_error:
        sts = "Fail"
    else:
        sts = {"enforce": "Enforced", "testing": "Testing"}.get(h.mta_sts_mode or "", "Off")
    rpt = "Missing" if not h.tls_rpt else ("Pass" if h.tls_rpt_records == 1 and h.tls_rpt_reports else "Fail")
    return {"MTA-STS": sts, "TLS-RPT": rpt}
