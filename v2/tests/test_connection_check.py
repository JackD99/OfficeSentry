"""Telling sign-in problems apart, in plain words."""

import re

import pytest
import requests
import responses

from officesentry import connection_check, msauth, store
from officesentry.certs import generate_certificate
from officesentry.connection_check import explain
from officesentry.graph import GRAPH_ROOT
from officesentry.msauth import TokenError
from officesentry.web.tenants import data_health
from officesentry.worker import execute_run
from tests.conftest import login, make_user
from tests.seed import seed_tenant

CERT_MISSING = "AADSTS700027: Client assertion failed signature validation. Trace ID: x Correlation ID: y"


def test_explain_known_codes():
    assert "certificate" in explain(CERT_MISSING).title
    assert "consent" in explain("AADSTS700016: Application with identifier 'x' was not found").fix
    assert "multi-tenant" in explain("AADSTS50194: Application 'x' is not configured as a multi-tenant").fix
    assert "consent" in explain("AADSTS65001: The user or administrator has not consented").title.lower()
    assert "redirect URI" in explain("AADSTS50011: The redirect URI specified does not match").title
    unknown = explain("AADSTS999999: Something new happened. Trace ID: abc")
    assert unknown.code == "AADSTS999999" and "Something new happened" in unknown.fix
    assert explain("Graph 500 oops") is None


def _token_error(*args):
    def provider():
        raise TokenError("invalid_client", CERT_MISSING)

    return provider


def test_test_connection_explains_and_records_consent(client, db, two_tenants, monkeypatch):
    alpha, _ = two_tenants
    db.execute("UPDATE tenants SET consented_at = NULL")
    make_user(db, "admin")
    login(client, "admin")
    monkeypatch.setattr(connection_check, "certificate_token_provider", _token_error)
    page = client.post(f"/tenants/{alpha}/test-connection", follow_redirects=True).get_data(as_text=True)
    assert "doesn&#39;t have Office Sentry&#39;s certificate" in page
    assert store.get_tenant(db, alpha)["consented_at"] is None

    monkeypatch.setattr(connection_check, "certificate_token_provider", lambda *a: lambda: "tok")
    with responses.RequestsMock() as rsps:
        rsps.get(f"{GRAPH_ROOT}/v1.0/organization", json={"value": [{"displayName": "Alpha Ltd"}]})
        page = client.post(f"/tenants/{alpha}/test-connection", follow_redirects=True).get_data(as_text=True)
    assert "Connected to Alpha Ltd." in page
    assert store.get_tenant(db, alpha)["consented_at"] is not None


def test_signed_in_but_no_consent_for_permissions(monkeypatch):
    monkeypatch.setattr(connection_check, "certificate_token_provider", lambda *a: lambda: "tok")
    with responses.RequestsMock() as rsps:
        rsps.get(
            f"{GRAPH_ROOT}/v1.0/organization",
            status=403,
            json={"error": {"code": "Authorization_RequestDenied", "message": "Insufficient privileges"}},
        )
        result = connection_check.check("t", "c", "key", "thumb")
    assert not result.ok and "consent" in result.fix


def test_choose_app_connection_for_a_tenant(client, db, two_tenants, connection_id):
    t = store.create_tenant(db, "Live test", "live.onmicrosoft.com", None)
    make_user(db, "admin")
    login(client, "admin")
    page = client.get(f"/tenants/{t}/collection").get_data(as_text=True)
    assert "choose its app connection" in page
    assert 'name="connection_id"' in client.get(f"/tenants/{t}/manage").get_data(as_text=True)
    client.post(f"/tenants/{t}/connection", data={"connection_id": connection_id})
    assert store.get_tenant(db, t)["connection_id"] == connection_id
    assert client.post(f"/tenants/{t}/connection", data={"connection_id": 999}).status_code == 400


def test_failed_sign_in_shows_the_fix_not_awaiting_consent(db, two_tenants):
    alpha, _ = two_tenants
    db.execute("UPDATE tenants SET consented_at = '2026-10-01T00:00:00Z' WHERE id = ?", (alpha,))

    def graph(tenant, connection):
        from officesentry.graph import GraphClient, GraphError

        def token():
            raise GraphError(401, "invalid_client", CERT_MISSING)

        return GraphClient(token)

    for key in ("users", "admin_roles"):
        store.enqueue_run(db, alpha, key)
        assert execute_run(db, store.claim_next_run(db), graph) == "failed"
    latest = {r["collector"]: r for r in db.execute("SELECT * FROM runs WHERE tenant_id = ?", (alpha,))}
    assert latest["users"]["error"].startswith("Sign-in failed: The app doesn't have Office Sentry's certificate.")
    tone, label, detail = data_health(store.get_tenant(db, alpha), latest)
    assert (tone, label) == ("bad", "Can't sign in") and "Certificates & secrets" in detail


def test_successful_collection_records_consent(db, two_tenants):
    alpha, _ = two_tenants
    db.execute("UPDATE tenants SET consented_at = NULL")
    assert store.get_tenant(db, alpha)["consented_at"] is None
    seed_tenant(db, alpha)
    assert store.get_tenant(db, alpha)["consented_at"] is not None


def test_consent_callback_error_is_explained(client, db, two_tenants):
    from tests.test_onboarding import _consent_state

    alpha, _ = two_tenants
    make_user(db, "admin")
    login(client, "admin")
    state = _consent_state(client, alpha)["state"][0]
    page = client.get(
        "/consent/callback",
        query_string={
            "state": state,
            "error": "invalid_request",
            "error_description": "AADSTS50011: The redirect URI 'x' specified in the request does not match",
        },
        follow_redirects=True,
    ).get_data(as_text=True)
    assert "missing Office Sentry&#39;s redirect URI" in page


def test_saving_client_id_checks_the_certificate(client, db, connection_id, monkeypatch):
    make_user(db, "admin")
    login(client, "admin")
    monkeypatch.setattr(connection_check, "certificate_token_provider", _token_error)
    page = client.post(
        f"/admin/connections/{connection_id}/client-id",
        follow_redirects=True,
        data={
            "client_id": "11111111-2222-3333-4444-555555555555",
            "home_tenant": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        },
    ).get_data(as_text=True)
    assert "Client ID saved, but the check failed." in page and "Certificates &amp; secrets" in page
    page = client.post(
        f"/admin/connections/{connection_id}/client-id",
        follow_redirects=True,
        data={"client_id": "11111111-2222-3333-4444-555555555555"},
    ).get_data(as_text=True)
    assert "Client ID saved." in page


def test_live_test_links_the_only_connection(cfg, db, connection_id, monkeypatch):
    from unittest.mock import patch

    from officesentry import livetest
    from tests import graphdata as gd
    from tests.seed import mock_tenant

    for k in livetest.ENV:
        monkeypatch.setenv(k, "tid" if k.endswith("TENANT_ID") else "x")
    monkeypatch.setattr(livetest, "secret_token_provider", lambda *a: lambda: "tok")
    with (
        responses.RequestsMock(assert_all_requests_are_fired=False) as rsps,
        patch("officesentry.dnscheck.lookup", gd.fake_dns),
    ):
        mock_tenant(rsps)
        assert livetest.run(cfg, ["tenant_profile"]) == 0
    tenant = db.execute("SELECT * FROM tenants WHERE tenant_ref = 'tid'").fetchone()
    assert tenant["connection_id"] == connection_id and tenant["consented_at"]


def test_old_raw_sign_in_errors_are_translated_on_display():
    from officesentry.web.filters import run_error

    raw = "AADSTS500113: No reply address is registered for the application. Trace ID: x"
    assert run_error(raw).startswith("Sign-in failed: The app registration is missing Office Sentry's redirect URI.")
    assert run_error("Exchange said no") == "Exchange said no"
    assert "5 to 10 minutes" in str(explain("AADSTS700027: Client assertion contains an invalid signature"))


# MSAL looks the tenant up with Microsoft before it asks for a token. These answer that lookup the way
# Microsoft does, so the failures go through MSAL itself rather than a stand-in.
LOGIN = "https://login.microsoftonline.com"
UNKNOWN_TENANT = {
    "error": "invalid_tenant",
    "error_description": "AADSTS90002: Tenant 'contoso-typo.onmicrosoft.com' not found. Trace ID: x",
}


def _tenant_lookup(rsps, **answer):
    rsps.get(re.compile(re.escape(LOGIN) + r"/common/discovery/instance.*"), json={"metadata": []})
    rsps.get(re.compile(re.escape(LOGIN) + r"/[^/]+/v2\.0/\.well-known/openid-configuration.*"), **answer)


def _check(tenant="contoso-typo.onmicrosoft.com"):
    cert = generate_certificate("check")
    return connection_check.check(tenant, "11111111-2222-3333-4444-555555555555", cert.key_pem, cert.thumbprint)


def test_a_tenant_microsoft_doesnt_know_is_explained():
    with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        _tenant_lookup(rsps, status=400, json=UNKNOWN_TENANT)
        result = _check()
    assert not result.ok and result.title == "Microsoft can't find this tenant."


def test_not_reaching_microsoft_is_explained():
    with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        rsps.get(re.compile(re.escape(LOGIN) + r"/.*"), body=requests.ConnectionError("Name or service not known"))
        result = _check()
    assert not result.ok and result.title == "Office Sentry couldn't reach Microsoft."
    assert LOGIN in result.fix


def test_check_connection_now_without_internet_says_so(client, db, two_tenants):
    alpha, _ = two_tenants
    make_user(db, "admin")
    login(client, "admin")
    with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        rsps.get(re.compile(re.escape(LOGIN) + r"/.*"), body=requests.ConnectionError("Name or service not known"))
        resp = client.post(f"/tenants/{alpha}/test-connection", follow_redirects=True)
    assert resp.status_code == 200
    assert "Office Sentry couldn&#39;t reach Microsoft." in resp.get_data(as_text=True)


def test_a_provider_kept_between_runs_signs_in_once_microsoft_answers_again():
    cert = generate_certificate("retry")
    provider = msauth.certificate_token_provider(
        "contoso.onmicrosoft.com", "app-id", cert.key_pem, cert.thumbprint, GRAPH_ROOT
    )
    with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        rsps.get(re.compile(re.escape(LOGIN) + r"/.*"), body=requests.ConnectionError("Network is unreachable"))
        with pytest.raises(TokenError) as raised:
            provider()
    assert raised.value.code == "network_error"
    with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        _tenant_lookup(
            rsps,
            json={
                "token_endpoint": f"{LOGIN}/contoso.onmicrosoft.com/oauth2/v2.0/token",
                "authorization_endpoint": f"{LOGIN}/contoso.onmicrosoft.com/oauth2/v2.0/authorize",
                "issuer": f"{LOGIN}/contoso.onmicrosoft.com/v2.0",
            },
        )
        rsps.post(
            f"{LOGIN}/contoso.onmicrosoft.com/oauth2/v2.0/token",
            json={"access_token": "tok", "token_type": "Bearer", "expires_in": 3600},
        )
        assert provider() == "tok"


def test_a_collection_that_cant_reach_microsoft_fails_as_a_network_error(db, two_tenants, vault, monkeypatch):
    from officesentry.worker import default_graph_factory

    monkeypatch.setattr(msauth, "_PROVIDERS", type(msauth._PROVIDERS)())
    alpha, _ = two_tenants
    store.enqueue_run(db, alpha, "users")
    with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        rsps.get(re.compile(re.escape(LOGIN) + r"/.*"), body=requests.ConnectionError("Network is unreachable"))
        assert execute_run(db, store.claim_next_run(db), default_graph_factory(vault)) == "failed"
    error = db.execute("SELECT error FROM runs WHERE tenant_id = ?", (alpha,)).fetchone()[0]
    assert "Couldn't reach Microsoft's sign-in service" in error
