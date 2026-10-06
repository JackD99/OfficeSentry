"""Prospect assessment: what anyone on the internet can see about a domain's Microsoft 365 and email security.

No consent and no credentials: Microsoft's public sign-in discovery endpoints
and public DNS only. Used to show a prospect where they stand before they
become a client.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime

import requests

from . import clouds, dnscheck
from .db import utcnow

LOGIN = clouds.get(clouds.DEFAULT).login  # public discovery for prospects; national cloud tenants answer here too
DOMAIN_RE = re.compile(r"^(?=.{4,253}$)([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
TENANT_GUID = re.compile(r"/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})/")


def normalise_domain(value: str) -> str | None:
    value = (value or "").strip().lower()
    value = re.sub(r"^[a-z]+://", "", value).split("/")[0].split("@")[-1].removeprefix("www.")
    return value if DOMAIN_RE.match(value) else None


@dataclass
class TenantInfo:
    uses_microsoft_365: bool | None = None  # None: lookup failed
    tenant_id: str | None = None
    brand: str | None = None
    sign_in: str | None = None  # Managed | Federated
    federation_host: str | None = None
    error: str | None = None
    cloud: str | None = None  # clouds.py key, from the discovery document; None when unknown


def cloud_from_discovery(data: dict) -> str | None:
    """Which Microsoft cloud a tenant is in, from its public OpenID discovery document."""
    instance = str(data.get("cloud_instance_name") or "").lower()
    sub_scope = str(data.get("tenant_region_sub_scope") or "").upper()
    if instance.endswith("chinacloudapi.cn"):
        return "china"
    if instance.endswith("microsoftonline.us"):
        return "dod" if sub_scope == "DOD" else "gcc_high"
    if instance.endswith("microsoftonline.com"):
        return "commercial"  # includes GCC (sub scope "GCC"), which uses the commercial endpoints
    return None


def tenant_id_for_domain(domain: str, session: requests.Session | None = None,
                         cloud: str | None = None) -> str | None:
    """The tenant GUID Microsoft publishes for ``domain``, or None when it can't be found out.

    Reads Microsoft's public sign-in metadata (no credentials, nothing from the
    tenant itself) on the sign-in host of ``cloud``, and only believes an answer
    issued by that host. Used to check that a tenant ID handed back by the
    consent callback really belongs to the domain an admin added.
    """
    if not DOMAIN_RE.match(domain or ""):
        return None
    login = clouds.get(cloud).login
    http = session or requests.Session()
    try:
        resp = http.get(f"{login}/{domain}/v2.0/.well-known/openid-configuration", timeout=15,
                        allow_redirects=False)
        if resp.status_code != 200:
            return None
        data = resp.json()
    except (requests.RequestException, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    for key in ("issuer", "token_endpoint"):
        value = data.get(key)
        if isinstance(value, str) and value.startswith(login + "/") and (m := TENANT_GUID.search(value + "/")):
            return m.group(1)
    return None


def tenant_lookup(domain: str, session: requests.Session | None = None) -> TenantInfo:
    http = session or requests.Session()
    info = TenantInfo()
    try:
        resp = http.get(f"{LOGIN}/{domain}/v2.0/.well-known/openid-configuration", timeout=15)
        if resp.status_code == 200:
            discovery = resp.json()
            m = TENANT_GUID.search(discovery.get("issuer", "") + "/")
            info.tenant_id = m.group(1) if m else None
            info.cloud = cloud_from_discovery(discovery) if info.tenant_id else None
            info.uses_microsoft_365 = info.tenant_id is not None
        elif resp.status_code in (400, 404):
            info.uses_microsoft_365 = False
        else:
            info.error = f"Microsoft sign-in discovery returned {resp.status_code}"
        if info.uses_microsoft_365:
            realm = http.get(f"{LOGIN}/getuserrealm.srf", params={"login": f"someone@{domain}", "json": "1"},
                             timeout=15).json()
            info.sign_in = realm.get("NameSpaceType") if realm.get("NameSpaceType") in ("Managed", "Federated") \
                else None
            info.brand = realm.get("FederationBrandName")
            auth_url = realm.get("AuthURL") or ""
            info.federation_host = auth_url.split("/")[2] if auth_url.startswith("https://") else None
    except (requests.RequestException, ValueError) as exc:
        info.error = f"Microsoft sign-in discovery failed: {exc}"
    return info


@dataclass
class Assessment:
    domain: str
    company: str
    checked_at: datetime
    tenant: TenantInfo
    health: dnscheck.DomainHealth
    id: int | None = None
    created_by: str = ""
    issues: list[dnscheck.Issue] = field(default_factory=list)  # includes MTA-STS and TLS-RPT

    @property
    def grade(self) -> str:
        """A, C or F for whether others can spoof the domain: simple enough to put on a sales slide.

        Only SPF and DMARC decide it. DMARC p=reject or p=quarantine stops spoofing on its
        own (SPF alignment is enough), so missing DKIM is listed but doesn't lower the grade.
        """
        severities = {i.severity for i in self.issues if i.area in ("SPF", "DMARC")}
        if "high" in severities:
            return "F"
        if "medium" in severities:
            return "C"
        return "A"

    @property
    def headline(self) -> str:
        if self.grade == "F":
            return "Criminals can send email that appears to come from this domain, and it will be delivered."
        if self.grade == "C":
            return "Email authentication is in place but not enforced, so spoofed mail can still get through."
        if any(i.severity in ("high", "medium") for i in self.issues):
            return "Spoofed email is blocked, but some email settings still need attention."
        return "Email authentication is set up and enforced."

    @property
    def delivery(self) -> dict[str, str]:
        """MTA-STS and TLS-RPT in one word each (Enforced, Testing, Missing...)."""
        return dnscheck.transport_summary(self.health)


def run(domain: str, company: str = "", resolve=None, session=None) -> Assessment:
    health = dnscheck.check_domain(domain, resolve or dnscheck.lookup)
    a = Assessment(domain, company or domain, datetime.now(UTC), tenant_lookup(domain, session), health)
    a.issues = dnscheck.issues(health, transport=True)
    return a


def save(conn, a: Assessment, username: str) -> int:
    data = {"tenant": a.tenant.__dict__, "health": a.health.to_dict(),
            "checked_at": a.checked_at.strftime("%Y-%m-%dT%H:%M:%SZ")}
    cur = conn.execute(
        "INSERT INTO prospect_assessments (domain, company, data, created_by, created_at) VALUES (?, ?, ?, ?, ?)",
        (a.domain, a.company, json.dumps(data), username, utcnow()))
    a.id, a.created_by = cur.lastrowid, username
    return cur.lastrowid


def load(row) -> Assessment:
    data = json.loads(row["data"])
    health = dnscheck.DomainHealth.from_dict(data["health"])
    a = Assessment(row["domain"], row["company"] or row["domain"],
                   datetime.fromisoformat(data["checked_at"].replace("Z", "+00:00")),
                   TenantInfo(**data["tenant"]), health, row["id"], row["created_by"])
    a.issues = dnscheck.issues(health, transport=True)
    return a
