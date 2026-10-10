"""Email domain hardening: SPF and DMARC edge cases, MTA-STS, TLS-RPT and snapshots stored before them."""

import itertools
import json
import re
import socket
from unittest.mock import MagicMock, patch

import pytest
import requests
import responses

from officesentry import dnscheck, store
from officesentry.reports.data_reports import build
from officesentry.rules import evaluate
from officesentry.snapshot import TenantSnapshot
from tests import graphdata as gd
from tests.seed import seed_tenant
from tests.stored import unpack

POLICY = "version: STSv1\r\nmode: enforce\r\nmx: *.mail.protection.outlook.com\r\nmax_age: 604800\r\n"
POLICY_URL = "https://mta-sts.a.com/.well-known/mta-sts.txt"
PUBLIC_IP = "52.1.2.3"


@pytest.fixture(autouse=True)
def public_dns(monkeypatch):
    """mta-sts.<domain> resolves to a public address unless a test says otherwise (no real DNS in tests)."""
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda host, port, *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (PUBLIC_IP, port))],
    )


@pytest.mark.parametrize("ip", ["10.0.0.5", "127.0.0.1", "169.254.169.254", "::1"])
def test_policy_fetch_refuses_internal_addresses(monkeypatch, ip):
    family = socket.AF_INET6 if ":" in ip else socket.AF_INET
    monkeypatch.setattr(
        socket, "getaddrinfo", lambda host, port, *a, **k: [(family, socket.SOCK_STREAM, 6, "", (ip, port))]
    )
    with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        rsps.get(POLICY_URL, body=POLICY)
        with pytest.raises(dnscheck.FetchError, match="internal address"):
            dnscheck.fetch_mta_sts("a.com")
        assert not rsps.calls


def test_policy_fetch_connects_to_the_address_it_checked(monkeypatch):
    from tests.test_alerts import _record_connections

    answers = iter([PUBLIC_IP] + ["10.0.0.5"] * 10)  # DNS rebinding
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda host, port, *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (next(answers), port))],
    )
    targets = _record_connections(monkeypatch)
    with pytest.raises(dnscheck.FetchError, match="couldn't connect"):
        dnscheck.fetch_mta_sts("a.com")
    assert targets and all(t == (PUBLIC_IP, 443) for t in targets)


# A Microsoft 365 mail domain with everything set up. spf.protection.outlook.com is deliberately absent: Microsoft's
# own include is trusted rather than reported as missing.
HARDENED = {
    ("a.com", "MX"): ["a-com.mail.protection.outlook.com"],
    ("a.com", "TXT"): ["v=spf1 include:spf.protection.outlook.com -all"],
    ("_dmarc.a.com", "TXT"): ["v=DMARC1; p=reject; rua=mailto:d@a.com"],
    ("selector1._domainkey.a.com", "CNAME"): ["s1"],
    ("selector2._domainkey.a.com", "CNAME"): ["s2"],
    ("_mta-sts.a.com", "TXT"): ["v=STSv1; id=20260101T000000"],
    ("_smtp._tls.a.com", "TXT"): ["v=TLSRPTv1; rua=mailto:tls@a.com"],
}

# What DomainHealth.to_dict() stored before MTA-STS policies, TLS-RPT and the SPF/DMARC additions.
OLD_ROW = {
    "domain": "a.com",
    "mx": ["a-com.mail.protection.outlook.com"],
    "spf": "v=spf1 include:spf.protection.outlook.com -all",
    "spf_records": 1,
    "spf_all": "-all",
    "spf_lookups": 1,
    "dmarc": "v=DMARC1; p=none; rua=mailto:d@a.com",
    "dmarc_policy": "none",
    "dmarc_subdomain_policy": None,
    "dmarc_pct": 100,
    "dmarc_reports": True,
    "dkim": {"selector1": True, "selector2": True},
    "mta_sts": False,
    "errors": [],
}


def resolver(changes=None):
    data = {**HARDENED, **(changes or {})}
    return lambda name, rdtype: list(data.get((name, rdtype)) or [])


def policy_fetch(text=POLICY):
    def fetch(domain):
        if isinstance(text, Exception):
            raise text
        return text

    return fetch


def no_fetch(domain):
    raise AssertionError(f"policy fetched for {domain}")


def check(changes=None, policy=POLICY):
    return dnscheck.check_domain("a.com", resolver(changes), policy_fetch(policy))


def found(h, area=None):
    return [(i.severity, i.detail) for i in dnscheck.issues(h, transport=True) if area in (None, i.area)]


def test_hardened_domain_has_no_issues():
    h = check()
    assert found(h) == []
    assert (h.mta_sts_id, h.mta_sts_mode, h.mta_sts_mx, h.mta_sts_max_age) == (
        "20260101T000000",
        "enforce",
        ["*.mail.protection.outlook.com"],
        604800,
    )
    assert (h.tls_rpt_records, h.tls_rpt_reports, h.transport_checked) == (1, True, True)
    assert dnscheck.transport_summary(h) == {"MTA-STS": "Enforced", "TLS-RPT": "Pass"}
    assert dnscheck.summary(h) == {"SPF": "Pass", "DMARC": "Pass", "DKIM": "Pass"}
    assert dnscheck.issues(h) == []  # transport issues only when asked for


# -- SPF --------------------------------------------------------------------------------


def test_spf_permissive_all_and_ptr():
    assert found(check({("a.com", "TXT"): ["v=spf1 +all"]}), "SPF")[0][0] == "high"
    assert found(check({("a.com", "TXT"): ["v=spf1 mx ?all"]}), "SPF")[0][0] == "medium"
    for record in ("v=spf1 ptr -all", "v=spf1 ?ptr:a.com mx -all"):
        assert found(check({("a.com", "TXT"): [record]}), "SPF") == [
            ("low", "SPF uses the ptr mechanism, which is slow and ignored by some receivers")
        ]
    assert (
        found(
            check(
                {
                    ("a.com", "TXT"): ["v=spf1 include:ptr.example.com mx -all"],
                    ("ptr.example.com", "TXT"): ["v=spf1 -all"],
                }
            ),
            "SPF",
        )
        == []
    )


def test_spf_include_without_record_fails():
    h = check({("a.com", "TXT"): ["v=spf1 include:gone.example include:spf.protection.outlook.com -all"]})
    assert h.spf_missing_includes == ["gone.example"]
    assert h.spf_lookups == 2  # counting is unchanged
    assert found(h, "SPF") == [
        ("medium", "SPF includes gone.example, but it has no SPF record, so SPF fails for all mail")
    ]
    # Nested, a TXT record that isn't SPF, and redirect= all count; the summary shows the worst issue.
    h = check(
        {
            ("a.com", "TXT"): ["v=spf1 ptr include:provider.com redirect=old.com"],
            ("provider.com", "TXT"): ["v=spf1 include:dead.com -all"],
            ("dead.com", "TXT"): ["google-site-verification=abc"],
        }
    )
    assert h.spf_missing_includes == ["dead.com", "old.com"]
    assert "but they have no SPF record" in found(h, "SPF")[0][1]
    assert dnscheck.summary(h)["SPF"] == "Weak"


def test_spf_lookup_failures_and_macros_are_not_reported_missing():
    def flaky(name, rdtype):
        if name == "flaky.com":
            raise dnscheck.DnsError("timeout")
        return resolver({("a.com", "TXT"): ["v=spf1 include:flaky.com include:%{i}._spf.a.com -all"]})(name, rdtype)

    h = dnscheck.check_domain("a.com", flaky, policy_fetch())
    assert h.spf_missing_includes == []
    assert found(h, "SPF") == []


# -- DMARC ------------------------------------------------------------------------------


def test_dmarc_multiple_records_and_missing_policy():
    two = check({("_dmarc.a.com", "TXT"): ["v=DMARC1; p=reject; rua=mailto:d@a.com", "v=DMARC1; p=none"]})
    assert two.dmarc_records == 2
    assert found(two, "DMARC") == [("high", "2 DMARC records; receivers treat this as no DMARC at all")]

    no_p = check({("_dmarc.a.com", "TXT"): ["v=DMARC1; rua=mailto:d@a.com"]})
    assert found(no_p, "DMARC") == [("medium", "DMARC record has no valid p= policy, so receivers treat it as p=none")]
    for record in ("v=DMARC1; pct=100", "v=DMARC1; p=rejected"):
        assert found(check({("_dmarc.a.com", "TXT"): [record]}), "DMARC") == [
            ("high", "DMARC record has no valid p= policy, so receivers ignore it"),
            ("low", "DMARC has no rua= address, so nobody sees the reports"),
        ]


def test_dmarc_subdomain_policy():
    sp_none = "DMARC doesn't cover subdomains (sp=none), so mail from made-up subdomains is still delivered"
    for policy in ("reject", "quarantine"):
        h = check({("_dmarc.a.com", "TXT"): [f"v=DMARC1; p={policy}; sp=none; rua=mailto:d@a.com"]})
        assert found(h, "DMARC") == [("low", sp_none)]
    assert (
        found(check({("_dmarc.a.com", "TXT"): ["v=DMARC1; p=reject; sp=quarantine; rua=mailto:d@a.com"]}), "DMARC")
        == []
    )
    # p=none already says spoofed mail is delivered; sp=none adds nothing.
    assert [
        d
        for _, d in found(check({("_dmarc.a.com", "TXT"): ["v=DMARC1; p=none; sp=none; rua=mailto:d@a.com"]}), "DMARC")
    ] == ["DMARC is monitor-only (p=none); spoofed mail is still delivered"]


# -- MTA-STS ------------------------------------------------------------------------------


def test_mta_sts_missing_is_low_and_fetches_nothing():
    h = dnscheck.check_domain(
        "a.com", resolver({("_mta-sts.a.com", "TXT"): ["something-else"], ("_smtp._tls.a.com", "TXT"): []}), no_fetch
    )
    assert (h.mta_sts, h.transport_checked) == (False, True)
    assert found(h) == [
        ("low", "No MTA-STS policy, so mail sent to this domain can be forced onto an unencrypted connection")
    ]  # no TLS-RPT finding without MTA-STS
    assert dnscheck.transport_summary(h) == {"MTA-STS": "Missing", "TLS-RPT": "Missing"}


@pytest.mark.parametrize("mx", [[], [""]])
def test_domains_that_dont_receive_mail_get_no_transport_findings(mx):
    h = dnscheck.check_domain("a.com", resolver({("a.com", "MX"): mx, ("_smtp._tls.a.com", "TXT"): []}), no_fetch)
    assert not h.sends_mail  # a null MX ("0 .") means no mail
    assert {i.area for i in dnscheck.issues(h, transport=True)} <= {"SPF", "DMARC"}
    assert dnscheck.transport_summary(h) == {"MTA-STS": "n/a", "TLS-RPT": "n/a"}


@pytest.mark.parametrize(
    "record, detail",
    [
        (["v=STSv1; id=1", "v=STSv1; id=2"], "2 MTA-STS records; senders ignore them all"),
        (["v=STSv1;"], "MTA-STS record has no valid id= (1 to 32 letters and digits), so senders ignore it"),
        (
            ["v=STSv1; id=2026-01-01"],
            "MTA-STS record has no valid id= (1 to 32 letters and digits), so senders ignore it",
        ),
        (
            ["v=STSv1; id=" + "1" * 33],
            "MTA-STS record has no valid id= (1 to 32 letters and digits), so senders ignore it",
        ),
    ],
)
def test_mta_sts_record_problems(record, detail):
    h = check({("_mta-sts.a.com", "TXT"): record})
    assert found(h, "MTA-STS") == [("low", detail)]
    assert dnscheck.transport_summary(h)["MTA-STS"] == "Fail"


@pytest.mark.parametrize(
    "policy, error",
    [
        ("<html><body>Not here</body></html>", 'the policy file has no "version: STSv1" line'),
        (POLICY.replace("enforce", "strict"), "the policy's mode isn't enforce, testing or none"),
        (POLICY.replace("max_age: 604800", "max_age: soon"), "the policy's max_age is missing or isn't a number"),
        (POLICY.replace("604800", "99999999"), "the policy's max_age is missing or isn't a number"),
        ("version: STSv1\nmode: enforce\nmax_age: 86400\n", "the policy lists no mx servers"),
        (dnscheck.FetchError("https://mta-sts.a.com/.well-known/mta-sts.txt returned HTTP 404"), "returned HTTP 404"),
    ],
)
def test_unusable_policy(policy, error):
    h = check(policy=policy)
    assert error in h.mta_sts_error
    assert found(h, "MTA-STS") == [("low", f"MTA-STS policy can't be used: {h.mta_sts_error}")]
    assert dnscheck.transport_summary(h)["MTA-STS"] == "Fail"


def test_policy_modes_and_mx_coverage():
    off = check(policy="version: STSv1\nmode: none\nmax_age: 86400\n")
    assert found(off, "MTA-STS") == [("low", "MTA-STS policy is switched off (mode: none)")]
    assert dnscheck.transport_summary(off)["MTA-STS"] == "Off"

    testing = check(policy=POLICY.replace("enforce", "testing"))
    assert found(testing, "MTA-STS") == [
        ("low", "MTA-STS is in testing mode, so senders still deliver unencrypted when encryption fails")
    ]
    assert dnscheck.transport_summary(testing)["MTA-STS"] == "Testing"
    wrong_mx = POLICY.replace("*.mail.protection.outlook.com", "mx.old-host.com")
    assert found(check(policy=wrong_mx.replace("enforce", "testing")), "MTA-STS")[0][1].endswith(
        "; the policy doesn't list a-com.mail.protection.outlook.com"
    )

    blocked = check(policy=wrong_mx)
    assert found(blocked, "MTA-STS") == [
        (
            "medium",
            "MTA-STS is enforced but doesn't list "
            "a-com.mail.protection.outlook.com, so senders that check it "
            "won't deliver mail",
        )
    ]
    backup = check({("a.com", "MX"): ["a-com.mail.protection.outlook.com", "backup.mx.net"]})
    assert found(backup, "MTA-STS") == [
        ("low", "MTA-STS doesn't list backup.mx.net, so senders that check it won't use that server")
    ]


def test_mx_patterns():
    assert dnscheck.mx_matches("*.mail.protection.outlook.com", "a-com.mail.protection.outlook.com.")
    assert dnscheck.mx_matches("MX1.A.com", "mx1.a.com")
    assert not dnscheck.mx_matches("*.mail.protection.outlook.com", "mail.protection.outlook.com")
    assert not dnscheck.mx_matches("*.a.com", "x.y.a.com")
    assert not dnscheck.mx_matches("*.a.com", "evila.com")
    assert not dnscheck.mx_matches("mx1.a.com", "mx2.a.com")


# -- TLS-RPT ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "record, issue",
    [
        ([], "No TLS-RPT record, so nobody hears when encrypted delivery to this domain fails"),
        (
            ["v=TLSRPTv1; rua=mailto:a@a.com", "v=TLSRPTv1; rua=mailto:b@a.com"],
            "2 TLS-RPT records; senders ignore them all",
        ),
        (["v=TLSRPTv1;"], "TLS-RPT record has no mailto: or https: rua= address, so no reports arrive"),
        (["v=TLSRPTv1; rua=a@a.com"], "TLS-RPT record has no mailto: or https: rua= address, so no reports arrive"),
        (["v=TLSRPTv1; rua=https://reports.example.com/v1, mailto:x@a.com"], None),
    ],
)
def test_tls_rpt(record, issue):
    h = check({("_smtp._tls.a.com", "TXT"): record})
    assert found(h, "TLS-RPT") == ([("low", issue)] if issue else [])
    assert dnscheck.transport_summary(h)["TLS-RPT"] == (
        "Pass" if issue is None else "Missing" if not record else "Fail"
    )


def test_transport_dns_failure_is_not_reported_as_missing():
    def broken(name, rdtype):
        if name.startswith("_smtp._tls."):
            raise dnscheck.DnsError("timeout")
        return resolver()(name, rdtype)

    h = dnscheck.check_domain("a.com", broken, no_fetch)
    assert h.errors and not h.transport_checked
    assert found(h) == []
    assert dnscheck.transport_summary(h)["MTA-STS"] == "Not checked"


# -- Fetching the policy file ------------------------------------------------------------


@responses.activate
def test_fetch_policy():
    responses.get(POLICY_URL, body=POLICY, content_type="text/plain")
    assert dnscheck.fetch_mta_sts("a.com") == POLICY
    assert responses.calls[0].request.url == POLICY_URL


@pytest.mark.parametrize(
    "kwargs, error",
    [
        ({"status": 404}, "returned HTTP 404"),
        (
            {"status": 301, "headers": {"Location": "https://evil.example/mta-sts.txt"}},
            "redirects to https://evil.example/mta-sts.txt, and sending servers don't follow redirects",
        ),
        ({"status": 302, "headers": {"Location": "/other.txt"}}, "redirects to /other.txt"),
        ({"body": requests.exceptions.ConnectTimeout()}, "took longer than 5 seconds"),
        ({"body": requests.exceptions.ReadTimeout()}, "took longer than 5 seconds"),
        ({"body": requests.exceptions.SSLError()}, "mta-sts.a.com has no valid HTTPS certificate"),
        ({"body": requests.exceptions.ConnectionError()}, "couldn't connect to mta-sts.a.com"),
        ({"body": b"x" * (dnscheck.MTA_STS_MAX_BYTES + 1)}, "is larger than 64 KB"),
        ({"body": b"\xff\xfe\x00version"}, "isn't a text file"),
    ],
)
def test_fetch_failures_raise_fetch_error_only(kwargs, error):
    with responses.RequestsMock() as rsps:
        rsps.get(POLICY_URL, **kwargs)
        with pytest.raises(dnscheck.FetchError, match=re.escape(error)):
            dnscheck.fetch_mta_sts("a.com")
        assert len(rsps.calls) == 1  # redirects are never followed, to the same host or another


def test_fetch_gives_up_on_a_slow_server():
    clock = MagicMock()
    clock.monotonic.side_effect = itertools.count(0, 4)  # each chunk arrives four seconds after the last
    with responses.RequestsMock() as rsps, patch("officesentry.dnscheck.time", clock):
        rsps.get(POLICY_URL, body=POLICY + "#" * 30000)
        with pytest.raises(dnscheck.FetchError, match="took longer than 5 seconds"):
            dnscheck.fetch_mta_sts("a.com")


@responses.activate
def test_check_domain_records_fetch_failures_and_bad_content():
    responses.get(POLICY_URL, body="<html>Welcome to nginx</html>", content_type="text/html")
    h = dnscheck.check_domain("a.com", resolver())
    assert h.mta_sts_error == 'the policy file has no "version: STSv1" line' and h.mta_sts_mode is None
    responses.replace(responses.GET, POLICY_URL, body=requests.exceptions.ConnectTimeout())
    h = dnscheck.check_domain("a.com", resolver())
    assert h.mta_sts_error.endswith("took longer than 5 seconds") and h.errors == []


# -- Snapshots stored before these checks --------------------------------------------------


def test_old_snapshot_rows_still_load():
    h = dnscheck.DomainHealth.from_dict(dict(OLD_ROW, _id="a.com"))
    assert (h.transport_checked, h.dmarc_records, h.spf_missing_includes, h.mta_sts_mode, h.tls_rpt) == (
        False,
        0,
        [],
        None,
        None,
    )
    assert dnscheck.issues(h, transport=True) == dnscheck.issues(h)
    assert [(i.area, i.severity) for i in dnscheck.issues(h)] == [("DMARC", "medium")]
    assert dnscheck.summary(h) == {"SPF": "Pass", "DMARC": "Weak", "DKIM": "Pass"}
    assert dnscheck.transport_summary(h) == {"MTA-STS": "Not checked", "TLS-RPT": "Not checked"}
    assert dnscheck.DomainHealth.from_dict(h.to_dict()) == h


def test_old_snapshot_in_the_database(db, two_tenants):
    alpha, _ = two_tenants
    seed_tenant(db, alpha)
    run = store.latest_run(db, alpha, "domain_health")
    unpack(db)
    db.execute(
        "UPDATE objects SET data = ? WHERE run_id = ? AND kind = 'domain' AND object_id = 'x.com'",
        (json.dumps(dict(OLD_ROW, domain="x.com")), run["id"]),
    )
    tenant = store.get_tenant(db, alpha)
    r = {x.key: x for x in evaluate(TenantSnapshot(db, tenant))}
    assert r["mta_sts"].status == r["tls_rpt"].status == "unavailable"
    assert "refresh Email domains" in r["mta_sts"].summary
    assert [(f.subject, f.severity) for f in r["dmarc_policy"].findings] == [("x.com", "medium"), ("parked.com", "low")]
    rows = build(db, tenant, "domains").sections[0].rows
    assert rows[0][:7] == ["x.com", "Microsoft 365", "Pass", "Weak", "Pass", "Not checked", "Not checked"]


# -- Collector, checks and report -----------------------------------------------------------


def seeded_dns(changes):
    data = {**gd.DNS, **changes}
    return lambda name, rdtype: list(data.get((name, rdtype)) or [])


X_STS = {("_mta-sts.x.com", "TXT"): ["v=STSv1; id=1"], ("_smtp._tls.x.com", "TXT"): ["v=TLSRPTv1; rua=mailto:t@x.com"]}


def test_checks_on_the_seeded_tenant(db, two_tenants):
    alpha, _ = two_tenants
    seed_tenant(db, alpha)
    r = {x.key: x for x in evaluate(TenantSnapshot(db, store.get_tenant(db, alpha)))}
    assert r["mta_sts"].status == "fail"
    assert [(f.subject, f.severity, f.object_id) for f in r["mta_sts"].findings] == [("x.com", "low", "x.com:MTA-STS")]
    assert r["mta_sts"].summary == "1 of 1 mail domain doesn't fully enforce MTA-STS"
    assert (r["tls_rpt"].status, r["tls_rpt"].summary) == (
        "unavailable",
        "TLS-RPT is checked on mail domains that use MTA-STS, and none do yet",
    )
    keys = list(r)
    assert keys[keys.index("mta_sts") :][:3] == ["mta_sts", "tls_rpt", "spf_record"]


def test_enforced_mta_sts_passes(db, two_tenants):
    alpha, _ = two_tenants
    with patch("officesentry.dnscheck.fetch_mta_sts", policy_fetch()):
        seed_tenant(db, alpha, dns=seeded_dns(X_STS))
    tenant = store.get_tenant(db, alpha)
    r = {x.key: x for x in evaluate(TenantSnapshot(db, tenant))}
    assert (r["mta_sts"].status, r["mta_sts"].summary) == ("pass", "MTA-STS is enforced on the 1 mail domain")
    assert (r["tls_rpt"].status, r["tls_rpt"].summary) == (
        "pass",
        "TLS-RPT reports are set up on the 1 domain that uses MTA-STS or TLS-RPT",
    )
    report = build(db, tenant, "domains")
    overview, records = report.sections
    assert overview.columns[5:7] == ["MTA-STS", "TLS-RPT"] and records.columns[4:6] == [
        "MTA-STS policy",
        "TLS-RPT record",
    ]
    assert overview.rows[0][5:7] == ["Enforced", "Pass"] and overview.rows[1][5:7] == ["n/a", "n/a"]
    assert records.rows[0][4] == "id=1; mode: enforce; mx: *.mail.protection.outlook.com; max_age: 604800"
    assert records.rows[0][5] == "v=TLSRPTv1; rua=mailto:t@x.com"
    assert "MTA-STS and TLS-RPT" in report.description


def test_policy_fetch_failure_is_a_finding_not_a_collection_problem(db, two_tenants):
    alpha, _ = two_tenants
    error = dnscheck.FetchError("couldn't connect to mta-sts.x.com")
    with patch("officesentry.dnscheck.fetch_mta_sts", policy_fetch(error)):
        seed_tenant(db, alpha, dns=seeded_dns({("_mta-sts.x.com", "TXT"): ["v=STSv1; id=1"]}))
    assert store.latest_run(db, alpha, "domain_health")["status"] == "succeeded"
    r = {x.key: x for x in evaluate(TenantSnapshot(db, store.get_tenant(db, alpha)))}
    assert [f.detail for f in r["mta_sts"].findings] == [
        "MTA-STS policy can't be used: couldn't connect to mta-sts.x.com"
    ]
    assert [f.detail for f in r["tls_rpt"].findings] == [
        "No TLS-RPT record, so nobody hears when encrypted delivery to this domain fails"
    ]


def test_existing_checks_pick_up_new_spf_and_dmarc_issues(db, two_tenants):
    alpha, _ = two_tenants
    seed_tenant(
        db,
        alpha,
        dns=seeded_dns(
            {
                ("x.com", "TXT"): ["v=spf1 ptr include:spf.protection.outlook.com -all"],
                ("_dmarc.x.com", "TXT"): ["v=DMARC1; p=reject; rua=mailto:d@x.com", "v=DMARC1; p=none"],
            }
        ),
    )
    r = {x.key: x for x in evaluate(TenantSnapshot(db, store.get_tenant(db, alpha)))}
    assert ("x.com", "high", "2 DMARC records; receivers treat this as no DMARC at all") in [
        (f.subject, f.severity, f.detail) for f in r["dmarc_policy"].findings
    ]
    assert ("x.com", "low") in [(f.subject, f.severity) for f in r["spf_record"].findings]


def test_no_mail_domains(db, two_tenants):
    alpha, _ = two_tenants
    seed_tenant(db, alpha, dns=seeded_dns({("x.com", "MX"): []}))
    r = {x.key: x for x in evaluate(TenantSnapshot(db, store.get_tenant(db, alpha)))}
    assert (r["mta_sts"].status, r["mta_sts"].summary) == ("unavailable", "No domain receives mail")
    assert r["tls_rpt"].status == "unavailable"


@responses.activate
def test_encrypted_delivery_is_listed_after_spoofing():
    responses.get(POLICY_URL, body=POLICY.replace("*.mail.protection.outlook.com", "mx.old-host.com"))
    h = dnscheck.check_domain("a.com", resolver())
    assert [(i.area, i.severity) for i in dnscheck.issues(h, transport=True)] == [("MTA-STS", "medium")]
    assert dnscheck.issues(h) == []
    assert dnscheck.transport_summary(h) == {"MTA-STS": "Enforced", "TLS-RPT": "Pass"}
    h = dnscheck.check_domain(
        "a.com", resolver({("_mta-sts.a.com", "TXT"): [], ("_dmarc.a.com", "TXT"): ["v=DMARC1; rua=mailto:d@a.com"]})
    )
    assert [(i.area, i.severity) for i in dnscheck.issues(h, transport=True)] == [
        ("DMARC", "medium"),
        ("MTA-STS", "low"),
    ]


@responses.activate
def test_conflicting_dmarc_records_fail():
    responses.get(POLICY_URL, body=POLICY)
    h = dnscheck.check_domain("a.com", resolver({("_dmarc.a.com", "TXT"): ["v=DMARC1; p=reject", "v=DMARC1; p=none"]}))
    assert ("DMARC", "high") in [(i.area, i.severity) for i in dnscheck.issues(h)]
