"""Email domain health: DNS parsing, issues, collector and checks."""

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
