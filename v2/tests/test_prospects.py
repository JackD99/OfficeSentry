"""Prospect assessments: public tenant discovery and DNS, page and access."""

from datetime import UTC, datetime
from unittest.mock import patch

import responses

from officesentry import dnscheck, prospect
from tests import graphdata as gd
from tests.conftest import login, make_user

TID = "11111111-2222-3333-4444-555555555555"


def mock_login(rsps, domain="x.com", federated=False):
    rsps.get(f"{prospect.LOGIN}/{domain}/v2.0/.well-known/openid-configuration",
             json={"issuer": f"https://login.microsoftonline.com/{TID}/v2.0"})
    rsps.get(f"{prospect.LOGIN}/getuserrealm.srf", json={
        "NameSpaceType": "Federated" if federated else "Managed", "FederationBrandName": "X Ltd",
        "AuthURL": "https://adfs.x.com/adfs/ls/" if federated else None})


def test_normalise_domain():
    assert prospect.normalise_domain(" https://www.Contoso.com/about ") == "contoso.com"
    assert prospect.normalise_domain("ann@contoso.co.uk") == "contoso.co.uk"
    for bad in ("", "localhost", "a b.com", "-x.com", "x.c"):
        assert prospect.normalise_domain(bad) is None, bad


@responses.activate
def test_assessment_grades_and_discovers_tenant():
    mock_login(responses, federated=True)
    a = prospect.run("x.com", "X Ltd", resolve=gd.fake_dns)
    assert (a.tenant.uses_microsoft_365, a.tenant.tenant_id, a.tenant.sign_in) == (True, TID, "Federated")
    assert a.tenant.federation_host == "adfs.x.com"
    assert a.grade == "C"  # DMARC p=none and DKIM half set up


@responses.activate
def test_unknown_domain_is_not_microsoft_365():
    responses.get(f"{prospect.LOGIN}/nobody.com/v2.0/.well-known/openid-configuration", status=400)
    a = prospect.run("nobody.com", resolve=lambda n, t: [])
    assert a.tenant.uses_microsoft_365 is False
    assert a.grade == "A"  # no MX: only low issues for a parked domain


def test_pages_and_access(client, db, two_tenants):
    make_user(db, "client", role="client", tenants=[two_tenants[0]])
    login(client, "client")
    assert client.get("/prospects").status_code == 403
    client.post("/logout")
    make_user(db, "analyst", role="analyst")
    login(client, "analyst")
    assert client.post("/prospects", data={"domain": "not a domain"}).status_code == 302
    assert db.execute("SELECT COUNT(*) FROM prospect_assessments").fetchone()[0] == 0
    with responses.RequestsMock() as rsps, patch("officesentry.dnscheck.lookup", gd.fake_dns):
        mock_login(rsps)
        resp = client.post("/prospects", data={"domain": "www.X.com", "company": "X Ltd"})
    assert resp.status_code == 302
    page = client.get(resp.headers["Location"]).get_data(as_text=True)
    assert "X Ltd" in page and "p=none" in page
    assert "X Ltd" in client.get("/prospects").get_data(as_text=True)
    pid = db.execute("SELECT id FROM prospect_assessments").fetchone()[0]
    client.post(f"/prospects/{pid}/delete")
    assert client.get(f"/prospects/{pid}").status_code == 404


def test_enforced_dmarc_grades_a_even_without_dkim():
    # contoso.com in October 2026: SPF -all, DMARC p=reject, mail on Microsoft 365 without its DKIM selectors.
    dns = {("contoso.com", "MX"): ["contoso-com.mail.protection.outlook.com"],
           ("contoso.com", "TXT"): ["v=spf1 include:spf.protection.outlook.com -all"],
           ("_dmarc.contoso.com", "TXT"): ["v=DMARC1; p=reject; pct=100; rua=mailto:d@rua.example; fo=1"]}
    health = dnscheck.check_domain("contoso.com", lambda n, t: dns.get((n, t), []))
    a = prospect.Assessment("contoso.com", "Contoso", datetime.now(UTC), prospect.TenantInfo(), health,
                            issues=dnscheck.issues(health))
    assert health.dmarc_policy == "reject" and [i.area for i in a.issues] == ["DKIM"]
    assert a.grade == "A"
    assert "blocked" in a.headline and "get through" not in a.headline
