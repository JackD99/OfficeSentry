"""Email domain health (SPF, DMARC, DKIM, MX, MTA-STS, TLS-RPT) for each of the tenant's verified domains.

From public DNS, plus each mail domain's MTA-STS policy file over HTTPS when
its DNS says there is one. Nothing is sent to the tenant.
"""

from __future__ import annotations

from .. import dnscheck, store
from .base import Collector, RunContext, register

DOMAIN_LIMIT = 100


def tenant_domain_names(orgs: list[dict]) -> list[str]:
    names = []
    for org in orgs:
        for d in org.get("verifiedDomains") or []:
            name = (d.get("name") or "").lower()
            if name and not name.endswith(".onmicrosoft.com") and name not in names:
                names.append(name)
    return names


@register
class DomainHealth(Collector):
    key = "domain_health"
    title = "Email domains"
    description = "SPF, DMARC, DKIM, MTA-STS and TLS-RPT for each verified domain, checked in public DNS."
    permissions = ()

    def collect(self, ctx: RunContext) -> None:
        run = store.latest_run(ctx.conn, ctx.tenant["id"], "tenant_profile")
        orgs = store.run_objects(ctx.conn, run["id"], "organization") if run else []
        if not orgs:
            orgs = ctx.graph.get("/organization", params={"$select": "verifiedDomains"}).get("value", [])
        names = tenant_domain_names(orgs)
        if len(names) > DOMAIN_LIMIT:
            ctx.warn(f"Checked the first {DOMAIN_LIMIT} of {len(names)} domains")
            names = names[:DOMAIN_LIMIT]
        rows = []
        for name in names:
            health = dnscheck.check_domain(name, dnscheck.lookup)
            if health.errors:
                ctx.warn(f"DNS lookups failed for {name}: {health.errors[0]}")
            rows.append((name, health.to_dict()))
        ctx.objects("domain", rows)
        mail = [dnscheck.DomainHealth.from_dict(r) for _, r in rows]
        mail = [h for h in mail if h.sends_mail]
        ctx.metric("domains.mail", len(mail))
        ctx.metric("domains.dmarc_enforced", sum(h.dmarc_policy in ("quarantine", "reject") for h in mail))
