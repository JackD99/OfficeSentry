"""Microsoft 365 national clouds: each tenant's sign-in, Graph and Exchange go to its own cloud's hosts."""

import json
import re
from urllib.parse import parse_qs, urlparse

import pytest
import responses

from officesentry import clouds, connection_check, discovery, msauth, store
from officesentry.certs import generate_certificate
from officesentry.entra import admin_consent_url
from officesentry.exchange import EXCHANGE_ROOT, ExchangeClient
from officesentry.graph import GRAPH_ROOT, GraphClient
from officesentry.worker import NotConnected, connection_cloud, execute_run
from tests.conftest import login, make_user

TENANT = "7c1e5f0a-3b9d-4e62-9a51-2f6c0d8e4b17"
CLIENT = "11111111-2222-3333-4444-555555555555"

EXPECTED = {
    "commercial": ("https://login.microsoftonline.com", "https://graph.microsoft.com", "https://outlook.office365.com"),
    "gcc_high": ("https://login.microsoftonline.us", "https://graph.microsoft.us", "https://outlook.office365.us"),
    "dod": ("https://login.microsoftonline.us", "https://dod-graph.microsoft.us", "https://webmail.apps.mil"),
    "china": (
        "https://login.chinacloudapi.cn",
        "https://microsoftgraph.chinacloudapi.cn",
        "https://partner.outlook.cn",
    ),
}


def test_endpoints_per_cloud():
    for key, (login_host, graph, exchange) in EXPECTED.items():
        c = clouds.get(key)
        assert (c.login, c.graph, c.exchange) == (login_host, graph, exchange)
        assert c.scope(c.graph) == f"{graph}/.default"
    # Unknown or missing means commercial, so rows from before the column keep working.
    assert clouds.get(None).key == clouds.get("nonsense").key == "commercial"
    assert clouds.of({"name": "no cloud key"}).key == "commercial"
    assert GRAPH_ROOT == "https://graph.microsoft.com" and EXCHANGE_ROOT == "https://outlook.office365.com"


def _mock_sign_in(rsps, login_host: str) -> list:
    """Answer MSAL's discovery and token requests on ``login_host``; return the token requests."""
    tokens = []
    discovery = {
        "token_endpoint": f"{login_host}/{TENANT}/oauth2/v2.0/token",
        "authorization_endpoint": f"{login_host}/{TENANT}/oauth2/v2.0/authorize",
        "issuer": f"{login_host}/{TENANT}/v2.0",
    }
    rsps.add(
        responses.GET,
        re.compile(r"https://[^/]+/common/discovery/instance.*"),
        json={
            "tenant_discovery_endpoint": f"{login_host}/{TENANT}/v2.0/.well-known/openid-configuration",
            "metadata": [],
        },
    )
    rsps.add(responses.GET, re.compile(re.escape(login_host) + r"/.*/openid-configuration.*"), json=discovery)

    def token(request):
        tokens.append(request)
        return 200, {}, json.dumps({"access_token": "tok", "token_type": "Bearer", "expires_in": 3600})

    rsps.add_callback(responses.POST, re.compile(r"https://[^/]+/.*/token"), callback=token)
    return tokens


@pytest.mark.parametrize("key", ["gcc_high", "china", "commercial"])
def test_token_goes_to_the_clouds_sign_in_host_with_its_graph_scope(key):
    c = clouds.get(key)
    cert = generate_certificate("cloud test")
    with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        tokens = _mock_sign_in(rsps, c.login)
        provider = msauth.certificate_token_provider(TENANT, CLIENT, cert.key_pem, cert.thumbprint, c.graph, key)
        assert provider() == "tok"
    assert len(tokens) == 1
    assert tokens[0].url == f"{c.login}/{TENANT}/oauth2/v2.0/token"
    assert parse_qs(tokens[0].body)["scope"] == [f"{c.graph}/.default"]


def test_token_for_another_clouds_resource_is_refused():
    with pytest.raises(ValueError):
        msauth.secret_token_provider(TENANT, CLIENT, "secret", "https://graph.microsoft.com", "gcc_high")
    with pytest.raises(ValueError):
        msauth.secret_token_provider(TENANT, CLIENT, "secret", "https://graph.microsoft.us", "china")


@pytest.mark.parametrize("key", ["gcc_high", "china"])
@responses.activate
def test_graph_base_url_and_next_link_stay_on_the_clouds_host(key):
    graph = clouds.get(key).graph
    client = GraphClient(lambda: "tok", cloud=key, sleep=lambda s: None)
    assert client.root == graph
    responses.get(f"{graph}/v1.0/users", json={"value": [{"id": "1"}], "@odata.nextLink": f"{graph}/v1.0/users?p=2"})
    responses.get(f"{graph}/v1.0/users?p=2", json={"value": [{"id": "2"}]})
    assert [u["id"] for u in client.get_all("/users").items] == ["1", "2"]
    responses.get(f"{graph}/beta/organization", json={"value": [{"id": "org"}]})
    assert client.get("/organization", version="beta")["value"] == [{"id": "org"}]

    # A nextLink to commercial Graph (or anywhere else) never gets this cloud's token.
    responses.get(
        f"{graph}/v1.0/groups", json={"value": [], "@odata.nextLink": "https://graph.microsoft.com/v1.0/groups?p=2"}
    )
    with pytest.raises(ValueError, match="refusing"):
        client.get_all("/groups")
    with pytest.raises(ValueError, match="refusing"):
        GraphClient.build_url(f"{graph}.evil.example/v1.0/users", root=graph)


@responses.activate
def test_commercial_client_refuses_national_cloud_links():
    client = GraphClient(lambda: "tok")
    assert client.root == GRAPH_ROOT
    with pytest.raises(ValueError, match="refusing"):
        GraphClient.build_url("https://graph.microsoft.us/v1.0/users")
    responses.get(
        f"{GRAPH_ROOT}/v1.0/users", json={"value": [], "@odata.nextLink": "https://graph.microsoft.us/v1.0/users?p=2"}
    )
    with pytest.raises(ValueError, match="refusing"):
        client.get_all("/users")


@pytest.mark.parametrize("key", ["gcc_high", "china", "commercial"])
@responses.activate
def test_exchange_root_per_cloud(key):
    root = clouds.get(key).exchange
    exo = ExchangeClient("tid", lambda: "tok", cloud=key)
    assert exo.url == f"{root}/adminapi/beta/tid/InvokeCommand"
    responses.post(
        exo.url,
        json={"value": [{"Name": "a"}], "@odata.nextLink": "https://outlook.office365.com/adminapi/beta/tid/x?p=2"},
    )
    if key == "commercial":
        responses.post("https://outlook.office365.com/adminapi/beta/tid/x?p=2", json={"value": []})
        assert exo.invoke("Get-Mailbox") == [{"Name": "a"}]
    else:
        with pytest.raises(ValueError, match="refusing"):
            exo.invoke("Get-Mailbox")


def test_admin_consent_url_per_cloud():
    for key in ("commercial", "gcc_high", "china"):
        url = admin_consent_url(CLIENT, "contoso.onmicrosoft.com", "https://os.example/consent/callback", "s", key)
        parsed = urlparse(url)
        assert f"{parsed.scheme}://{parsed.netloc}" == clouds.get(key).login
        assert parsed.path == "/contoso.onmicrosoft.com/adminconsent"
    assert admin_consent_url(CLIENT, "t", "r", "s").startswith("https://login.microsoftonline.com/t/adminconsent?")


def test_connection_check_signs_in_to_the_tenants_cloud(monkeypatch):
    seen = []
    monkeypatch.setattr(connection_check, "certificate_token_provider", lambda *a: seen.append(a) or (lambda: "tok"))
    with responses.RequestsMock() as rsps:
        rsps.get("https://graph.microsoft.us/v1.0/organization", json={"value": [{"displayName": "Gov Ltd"}]})
        result = connection_check.check("t", "c", "key", "thumb", "gcc_high")
    assert result.ok and result.organisation == "Gov Ltd"
    assert seen[0][4:] == ("https://graph.microsoft.us", "gcc_high")


def test_tenant_id_for_domain_uses_the_clouds_sign_in_host():
    with responses.RequestsMock() as rsps:
        rsps.get(
            "https://login.chinacloudapi.cn/contoso.partner.onmschina.cn/v2.0/.well-known/openid-configuration",
            json={"issuer": f"https://login.chinacloudapi.cn/{TENANT}/v2.0"},
        )
        assert discovery.tenant_id_for_domain("contoso.partner.onmschina.cn", cloud="china") == TENANT
    with responses.RequestsMock() as rsps:  # an answer naming another host isn't believed
        rsps.get(
            "https://login.microsoftonline.us/contoso.onmicrosoft.us/v2.0/.well-known/openid-configuration",
            json={"issuer": f"https://login.microsoftonline.com/{TENANT}/v2.0"},
        )
        assert discovery.tenant_id_for_domain("contoso.onmicrosoft.us", cloud="gcc_high") is None


# -- Tenants and app connections ---------------------------------------------------------------


def test_cloud_column_defaults_to_commercial(db, connection_id):
    tid = store.create_tenant(db, "Alpha", "alpha.onmicrosoft.com", connection_id)
    assert store.get_tenant(db, tid)["cloud"] == "commercial"
    assert store.get_connection(db, connection_id)["cloud"] == "commercial"


def test_worker_needs_an_app_from_the_tenants_cloud(db, vault, connection_id):
    commercial = dict(store.get_connection(db, connection_id))
    gov = {"id": 1, "tenant_ref": "gov.onmicrosoft.us", "cloud": "gcc_high"}
    with pytest.raises(NotConnected, match="set up for that cloud"):
        connection_cloud(gov, commercial)
    with pytest.raises(NotConnected, match="GCC High"):
        connection_cloud(gov, None)
    gov_app = store.create_connection(
        db, vault, "Gov app", generate_certificate("gov"), client_id=CLIENT, cloud="gcc_high"
    )
    assert connection_cloud(gov, dict(store.get_connection(db, gov_app))) == "gcc_high"
    assert connection_cloud({"cloud": "commercial"}, commercial) == "commercial"


def test_run_for_a_tenant_without_an_app_in_its_cloud_says_so(db, connection_id):
    from officesentry.worker import default_graph_factory

    tid = store.create_tenant(db, "Gov", "gov.onmicrosoft.us", connection_id, "gcc_high")
    store.enqueue_run(db, tid, "tenant_profile")
    run = store.claim_next_run(db)
    from cryptography.fernet import Fernet

    from officesentry.crypto import Vault

    status = execute_run(db, run, default_graph_factory(Vault([Fernet.generate_key().decode()])))
    assert status == "failed"
    error = db.execute("SELECT error FROM runs WHERE id = ?", (run["id"],)).fetchone()[0]
    assert "US Government GCC High" in error and "App connection" in error


def test_collector_microsoft_doesnt_offer_in_the_cloud_is_partial(db, vault):
    app = store.create_connection(db, vault, "China app", generate_certificate("cn"), client_id=CLIENT, cloud="china")
    tid = store.create_tenant(db, "CN", "cn.partner.onmschina.cn", app, "china")
    store.enqueue_run(db, tid, "secure_score")
    run = store.claim_next_run(db)
    status = execute_run(db, run, lambda t, c: GraphClient(lambda: "tok", cloud=clouds.of(t).key))
    assert status == "partial"
    error = db.execute("SELECT error FROM runs WHERE id = ?", (run["id"],)).fetchone()[0]
    assert "Secure Score" in error and "21Vianet" in error


def test_add_and_edit_tenant_with_a_cloud(client, db, vault, connection_id):
    make_user(db, "admin")
    login(client, "admin")
    page = client.get("/tenants/new").get_data(as_text=True)
    assert "US Government GCC High" in page and "China (operated by 21Vianet)" in page

    # No GCC High app yet: the tenant is added without one, and its page says to set one up.
    resp = client.post(
        "/tenants/new",
        data={
            "name": "Gov Ltd",
            "tenant_ref": "gov.onmicrosoft.us",
            "connection_id": connection_id,
            "cloud": "gcc_high",
        },
    )
    tid = int(resp.headers["Location"].rsplit("/", 1)[1])
    t = store.get_tenant(db, tid)
    assert t["cloud"] == "gcc_high" and t["connection_id"] is None
    page = client.get(f"/tenants/{tid}").get_data(as_text=True)
    assert "Set up an app for US Government GCC High" in page
    assert "adminconsent" not in page

    # With a GCC High app, the consent link is on the US Government sign-in host.
    gov_app = store.create_connection(
        db, vault, "Gov app", generate_certificate("gov"), client_id=CLIENT, cloud="gcc_high"
    )
    client.post(
        f"/tenants/{tid}/edit",
        data={"name": "Gov Ltd", "tenant_ref": "gov.onmicrosoft.us", "connection_id": gov_app, "cloud": "gcc_high"},
    )
    assert store.get_tenant(db, tid)["connection_id"] == gov_app
    page = client.get(f"/tenants/{tid}").get_data(as_text=True)
    assert "https://login.microsoftonline.us/gov.onmicrosoft.us/adminconsent" in page
    assert "Set up an app for" not in page

    # The commercial app can't be chosen for it.
    page = client.post(
        f"/tenants/{tid}/edit",
        data={
            "name": "Gov Ltd",
            "tenant_ref": "gov.onmicrosoft.us",
            "connection_id": connection_id,
            "cloud": "gcc_high",
        },
        follow_redirects=True,
    ).get_data(as_text=True)
    assert "can&#39;t sign in to a US Government GCC High tenant" in page
    assert store.get_tenant(db, tid)["connection_id"] == gov_app

    # An unknown cloud is refused outright.
    assert (
        client.post("/tenants/new", data={"name": "X", "tenant_ref": "x.onmicrosoft.com", "cloud": "mars"}).status_code
        == 400
    )


def test_commercial_tenant_unchanged(client, db, two_tenants):
    alpha, _ = two_tenants
    db.execute("UPDATE tenants SET consented_at = NULL")  # so the page offers the consent link
    make_user(db, "admin")
    login(client, "admin")
    page = client.get(f"/tenants/{alpha}").get_data(as_text=True)
    assert "https://login.microsoftonline.com/alpha.onmicrosoft.com/adminconsent" in page
    assert "Set up an app for" not in page and "Commercial (includes GCC)" not in page.split("<nav")[0]


def test_app_connection_created_for_a_cloud(client, db):
    make_user(db, "admin")
    login(client, "admin")
    client.post("/admin/connections", data={"name": "China", "cloud": "china"})
    row = db.execute("SELECT * FROM app_connections WHERE name = 'China'").fetchone()
    assert row["cloud"] == "china"
    assert "China (operated by 21Vianet)" in client.get("/admin/connections").get_data(as_text=True)
    assert client.post("/admin/connections", data={"name": "X", "cloud": "mars"}).status_code == 400


def test_reports_and_checks_say_when_microsoft_doesnt_offer_it(db, connection_id):
    from officesentry.reports import data_reports
    from officesentry.rules import evaluate
    from officesentry.snapshot import TenantSnapshot

    tid = store.create_tenant(db, "CN", "cn.partner.onmschina.cn", None, "china")
    tenant = store.get_tenant(db, tid)
    for key in ("secure-score", "risky-users"):
        assert "21Vianet" in data_reports.build(db, tenant, key).unavailable
    risky = next(r for r in evaluate(TenantSnapshot(db, tenant)) if r.key == "risky_users")
    assert risky.status == "unavailable" and "21Vianet" in risky.summary
    # A commercial tenant with no data still just hasn't been collected.
    commercial = store.get_tenant(db, store.create_tenant(db, "A", "a.onmicrosoft.com", connection_id))
    assert "21Vianet" not in data_reports.build(db, commercial, "secure-score").unavailable
