"""Email domain health: DNS parsing, issues, collector and checks."""

from unittest.mock import patch

import dns.resolver
import pytest

from officesentry import dnscheck, store
from officesentry.rules import evaluate
from officesentry.snapshot import TenantSnapshot
from tests import graphdata as gd
from tests.seed import seed_tenant


def resolver(records):
    return lambda name, rdtype: list(records.get((name, rdtype), []))


def test_good_domain_has_no_issues():
    r = resolver({
        ("a.com", "MX"): ["a-com.mail.protection.outlook.com"],
        ("a.com", "TXT"): ["v=spf1 include:spf.protection.outlook.com -all"],
        ("_dmarc.a.com", "TXT"): ["v=DMARC1; p=reject; rua=mailto:x@a.com"],
        ("selector1._domainkey.a.com", "CNAME"): ["s1"], ("selector2._domainkey.a.com", "CNAME"): ["s2"],
    })
    h = dnscheck.check_domain("A.com", r)
    assert (h.spf_all, h.dmarc_policy, h.microsoft_365) == ("-all", "reject", True)
    assert dnscheck.issues(h) == []
    assert dnscheck.summary(h) == {"SPF": "Pass", "DMARC": "Pass", "DKIM": "Pass"}


def test_missing_and_weak_records():
    h = dnscheck.check_domain("b.com", resolver({("b.com", "MX"): ["mx.b.com"],
                                                 ("b.com", "TXT"): ["v=spf1 a mx ?all"]}))
    found = {(i.area, i.severity) for i in dnscheck.issues(h)}
    assert ("SPF", "medium") in found and ("DMARC", "high") in found
    assert not any(i.area == "DKIM" for i in dnscheck.issues(h))  # not a Microsoft 365 mail domain
    two = dnscheck.check_domain("c.com", resolver({("c.com", "MX"): ["m"], ("c.com", "TXT"): [
        "v=spf1 -all", "v=spf1 ~all"], ("_dmarc.c.com", "TXT"): ["v=DMARC1; p=none"]}))
    assert [i.detail for i in dnscheck.issues(two)][0].startswith("2 SPF records")
    assert {i.area for i in dnscheck.issues(two) if i.severity == "low"} == {"DMARC"}  # no rua


def test_spf_lookup_count_follows_includes():
    records = {("d.com", "MX"): ["m"], ("d.com", "TXT"): ["v=spf1 " + " ".join(
        f"include:i{n}.com" for n in range(6)) + " -all"]}
    for n in range(6):
        records[(f"i{n}.com", "TXT")] = ["v=spf1 include:deep.com a -all"]
    records[("deep.com", "TXT")] = ["v=spf1 ip4:1.2.3.4 -all"]
    h = dnscheck.check_domain("d.com", resolver(records))
    assert h.spf_lookups == 6 + 6 * 2  # six includes, each with its own include and a
    assert any("DNS lookups" in i.detail for i in dnscheck.issues(h))


def test_parked_domain_should_block_spoofing():
    h = dnscheck.check_domain("p.com", resolver({}))
    assert {i.area for i in dnscheck.issues(h)} == {"SPF", "DMARC"}
    assert all(i.severity == "low" for i in dnscheck.issues(h))


def test_dns_errors_are_recorded_not_raised():
    def broken(name, rdtype):
        raise dnscheck.DnsError("timeout")
    h = dnscheck.check_domain("e.com", broken)
    assert h.errors and h.mx == []


def test_collector_and_checks(db, two_tenants):
    alpha, _ = two_tenants
    seed_tenant(db, alpha)
    snap = TenantSnapshot(db, store.get_tenant(db, alpha))
    assert {d["domain"] for d in snap.objects("domain_health", "domain")} == {"x.com", "parked.com"}
    r = {x.key: x for x in evaluate(snap)}
    assert [(f.subject, f.severity) for f in r["dmarc_policy"].findings] == [("x.com", "medium"), ("parked.com", "low")]
    assert [f.subject for f in r["spf_record"].findings] == ["parked.com"]
    assert r["dkim_enabled"].findings[0].detail.endswith("(selector2 CNAME missing)")


def test_tenant_without_custom_domains(db, two_tenants):
    alpha, _ = two_tenants
    gd_org = dict(gd.ORGANIZATION, verifiedDomains=[{"name": "x.onmicrosoft.com"}])
    import tests.graphdata as g
    old, g.ORGANIZATION = g.ORGANIZATION, gd_org
    try:
        seed_tenant(db, alpha)
    finally:
        g.ORGANIZATION = old
    r = {x.key: x for x in evaluate(TenantSnapshot(db, store.get_tenant(db, alpha)))}
    assert r["spf_record"].status == "unavailable"


def test_domains_detail_report(db, two_tenants):
    from officesentry.reports.data_reports import build

    alpha, _ = two_tenants
    seed_tenant(db, alpha)
    report = build(db, store.get_tenant(db, alpha), "domains")
    rows = report.sections[0].rows
    assert rows[0][:5] == ["x.com", "Microsoft 365", "Pass", "Weak", "Weak"]
    assert rows[1][1] == "Doesn't receive mail"


def test_a_resolver_that_fails_is_an_error_not_a_missing_record():
    # SERVFAIL, REFUSED or an unreachable resolver: dnspython raises NoNameservers, which says nothing
    # about the domain's records.
    with patch.object(dns.resolver.Resolver, "resolve", side_effect=dns.resolver.NoNameservers()):
        with pytest.raises(dnscheck.DnsError):
            dnscheck.lookup("x.com", "MX")
    with patch.object(dns.resolver.Resolver, "resolve", side_effect=dns.resolver.NXDOMAIN()):
        assert dnscheck.lookup("x.com", "MX") == []


def failing(records, *broken):
    def resolve(name, rdtype):
        if (name, rdtype) in broken:
            raise dnscheck.DnsError(f"{name} {rdtype}: All nameservers failed to answer the query.")
        return list(records.get((name, rdtype), []))
    return resolve


def test_a_failed_mx_lookup_is_unknown_not_a_parked_domain():
    records = {("m.com", "TXT"): ["v=spf1 +all"], ("_dmarc.m.com", "TXT"): ["v=DMARC1; p=none"]}
    h = dnscheck.DomainHealth.from_dict(dnscheck.check_domain("m.com", failing(records, ("m.com", "MX"))).to_dict())
    assert h.failed == ["MX"] and not h.sends_mail
    assert dnscheck.issues(h, transport=True) == []  # not two low "doesn't receive mail" findings
    assert set(dnscheck.summary(h).values()) == set(dnscheck.transport_summary(h).values()) == {"Couldn't check"}
    # Only the area whose lookup failed is unknown.
    h = dnscheck.check_domain("m.com", failing({**records, ("m.com", "MX"): ["mx.m.com"]}, ("_dmarc.m.com", "TXT")))
    assert {i.area for i in dnscheck.issues(h)} == {"SPF"} and dnscheck.summary(h)["DMARC"] == "Couldn't check"


def test_findings_keep_the_worst_problem_and_survive_a_failed_lookup(db, two_tenants):
    alpha, _ = two_tenants
    # SPF ending in +all (high) that also uses ptr (low); DMARC at p=none (medium) with no rua (low).
    records = {**gd.DNS, ("x.com", "TXT"): ["v=spf1 ptr +all"], ("_dmarc.x.com", "TXT"): ["v=DMARC1; p=none"]}
    seed_tenant(db, alpha, dns=failing(records))

    def stored():
        return {r["object_id"]: (r["severity"], r["detail"], r["resolved_at"]) for r in db.execute(
            "SELECT * FROM findings WHERE tenant_id = ? AND object_id LIKE 'x.com:%'", (alpha,))}

    before = stored()
    assert before["x.com:SPF"][0] == "high" and before["x.com:DMARC"][0] == "medium"
    assert "+all" in before["x.com:SPF"][1] and "ptr" in before["x.com:SPF"][1]  # every problem, worst first
    assert before["x.com:SPF"][1].index("+all") < before["x.com:SPF"][1].index("ptr")
    mail = db.execute("SELECT value FROM metrics WHERE tenant_id = ? AND key = 'domains.mail'", (alpha,)).fetchall()

    # The next night the resolver fails for x.com: its findings stay as they were, not two low parked-domain ones.
    seed_tenant(db, alpha, dns=failing(records, ("x.com", "MX")))
    assert stored() == before
    snap = TenantSnapshot(db, store.get_tenant(db, alpha))
    r = {x.key: x for x in evaluate(snap)}
    assert "couldn't check x.com" in r["spf_record"].summary and r["spf_record"].keep == ["x.com:SPF"]
    assert db.execute("SELECT value FROM metrics WHERE tenant_id = ? AND key = 'domains.mail'",
                      (alpha,)).fetchall() == mail  # no drop to zero mail domains
