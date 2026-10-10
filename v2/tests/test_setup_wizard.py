"""The setup wizard on the App connection page: device sign-in from the browser, the app created, first tenant.

Microsoft is simulated at the HTTP level: the sign-in service (device code and token endpoints) and Graph
(the resource service principals and ``POST /applications``).
"""

import json
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs

import jwt
import pytest
import responses
from responses import matchers

from officesentry import app_setup, store
from officesentry.certs import generate_certificate, key_credential
from officesentry.entra import (
    EXCHANGE_APP_ID,
    EXCHANGE_PERMISSIONS,
    GRAPH_APP_ID,
    GRAPH_CLI_CLIENT_ID,
    GRAPH_PERMISSIONS,
)
from tests.conftest import PASSWORD, login, make_user

LOGIN = "https://login.microsoftonline.com"
GRAPH = "https://graph.microsoft.com/v1.0"
HOME = "c0ffee00-1111-2222-3333-444455556666"  # the MSP's own tenant
APP_ID = "AAAA1111-2222-3333-4444-555566667777"
DEVICE_CODE = "DAQABAAEAAAD-secret-device-code"
USER_CODE = "HXQ7-PL2M"
TOKEN = jwt.encode(
    {"tid": HOME, "scp": "Application.ReadWrite.All"}, "signature-isnt-checked-by-office-sentry", algorithm="HS256"
)
JSON = {"Accept": "application/json"}


def _device_code(status=200, body=None):
    return responses.post(
        f"{LOGIN}/organizations/oauth2/v2.0/devicecode",
        status=status,
        json=body
        or {
            "device_code": DEVICE_CODE,
            "user_code": USER_CODE,
            "verification_uri": "https://microsoft.com/devicelogin",
            "expires_in": 900,
            "interval": 5,
            "message": "To sign in, use a web browser...",
        },
    )


def _token(*bodies):
    """The token endpoint answering each check in turn: (status, json)."""
    calls = [
        responses.post(f"{LOGIN}/organizations/oauth2/v2.0/token", status=status, json=body) for status, body in bodies
    ]
    return calls


PENDING = (400, {"error": "authorization_pending", "error_description": "AADSTS70016: Pending end-user auth."})
SIGNED_IN = (
    200,
    {
        "token_type": "Bearer",
        "access_token": TOKEN,
        "expires_in": 3599,
        "scope": "https://graph.microsoft.com/Application.ReadWrite.All",
    },
)


def _roles(wanted, extra):
    return [{"id": f"role-{name}", "value": name, "allowedMemberTypes": ["Application"]} for name in [*wanted, *extra]]


# Microsoft Graph's and Exchange Online's service principals in the MSP's tenant, offering more than we ask for.
RESOURCES = {
    GRAPH_APP_ID: {
        "id": "sp-graph",
        "appId": GRAPH_APP_ID,
        "appRoles": _roles(GRAPH_PERMISSIONS, ["Mail.Read", "Directory.ReadWrite.All"]),
    },
    EXCHANGE_APP_ID: {
        "id": "sp-exo",
        "appId": EXCHANGE_APP_ID,
        "appRoles": _roles(EXCHANGE_PERMISSIONS, ["full_access_as_app"]),
    },
}


def _graph(create_status=201):
    for sp in RESOURCES.values():
        responses.get(
            f"{GRAPH}/servicePrincipals",
            json={"value": [sp]},
            match=[matchers.query_param_matcher({"$filter": f"appId eq '{sp['appId']}'", "$select": "id,appRoles"})],
        )
    body = (
        {"id": "obj-1", "appId": APP_ID}
        if create_status < 300
        else {"error": {"code": "Authorization_RequestDenied", "message": "Insufficient privileges."}}
    )
    return responses.post(f"{GRAPH}/applications", status=create_status, json=body)


@pytest.fixture
def admin(client, db):
    make_user(db, "admin")
    login(client, "admin")
    return client


def _check(client):
    """One check by the page, as its script makes it; a test moves time on with ``_next_poll_now``."""
    return client.post("/admin/connections/setup/check", headers=JSON).get_json()


def _next_poll_now(db):
    db.execute("UPDATE setup_sign_ins SET next_poll_at = NULL")


def _everything_stored(db) -> str:
    """Every value in every table, as text: what a stolen database would hold."""
    out = []
    for (table,) in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall():
        for row in db.execute(f'SELECT * FROM "{table}"').fetchall():
            out.extend(str(v) for v in row)
    return "\n".join(out)


@responses.activate
def test_fresh_install_reaches_first_tenant_consent_link_without_the_command_line(admin, db):
    page = admin.get("/admin/connections").get_data(as_text=True)
    assert "Connect Office Sentry to Microsoft 365" in page and "Sign in to Microsoft" in page
    assert "docker compose run --rm worker python -m officesentry create-app" in page  # W5: the Docker prefix

    start = _device_code()
    resp = admin.post("/admin/connections/setup", data={})
    assert resp.status_code == 302 and resp.headers["Location"].endswith("/admin/connections#setup")
    sent = parse_qs(start.calls[0].request.body)
    # The one delegated permission the CLI asks for too; no refresh token (offline_access) or profile.
    assert sent == {
        "client_id": [GRAPH_CLI_CLIENT_ID],
        "scope": ["https://graph.microsoft.com/Application.ReadWrite.All"],
    }

    page = admin.get("/admin/connections").get_data(as_text=True)
    assert USER_CODE in page and "https://microsoft.com/devicelogin" in page and "Waiting for you to sign in" in page
    assert DEVICE_CODE not in page  # the device code never reaches the browser...
    row = db.execute("SELECT * FROM setup_sign_ins").fetchone()
    assert row["device_code_enc"] and DEVICE_CODE not in row["device_code_enc"]  # ...and is stored encrypted

    token = _token(PENDING, SIGNED_IN)
    created = _graph()
    assert _check(admin)["status"] == "waiting"
    assert _check(admin)["status"] == "waiting"  # too soon: Microsoft isn't asked again before its interval
    assert len(token[0].calls) == 1 and not token[1].calls
    _next_poll_now(db)
    assert _check(admin)["status"] == "done"
    assert parse_qs(token[1].calls[0].request.body)["device_code"] == [DEVICE_CODE]

    # The app: multi-tenant, the consent redirect URI, exactly the read-only permissions, the stored certificate.
    body = json.loads(created.calls[0].request.body)
    connection = store.list_connections(db)[0]
    assert body["signInAudience"] == "AzureADMultipleOrgs" and body["displayName"] == "Office Sentry (read-only)"
    assert body["web"]["redirectUris"] == ["http://localhost:8000/consent/callback"]
    assert body["keyCredentials"] == [key_credential(connection["cert_pem"])]
    roles = {app_id: {r["id"]: r["value"] for r in sp["appRoles"]} for app_id, sp in RESOURCES.items()}
    asked = {
        access["resourceAppId"]: sorted(roles[access["resourceAppId"]][r["id"]] for r in access["resourceAccess"])
        for access in body["requiredResourceAccess"]
    }
    assert asked == {GRAPH_APP_ID: sorted(GRAPH_PERMISSIONS), EXCHANGE_APP_ID: sorted(EXCHANGE_PERMISSIONS)}
    assert created.calls[0].request.headers["Authorization"] == f"Bearer {TOKEN}"
    # Microsoft was only ever asked to read, apart from creating this one app.
    graph_calls = [c.request for c in responses.calls if c.request.url.startswith(GRAPH)]
    assert [r.method for r in graph_calls] == ["GET", "GET", "POST"]

    assert [(c["client_id"], c["cloud"]) for c in store.list_connections(db)] == [(APP_ID.lower(), "commercial")]
    row = db.execute("SELECT * FROM setup_sign_ins").fetchone()
    assert row["device_code_enc"] is None and row["home_tenant"] == HOME
    stored = _everything_stored(db)
    assert TOKEN not in stored and TOKEN.split(".")[1] not in stored and DEVICE_CODE not in stored
    actions = [r["action"] for r in db.execute("SELECT action FROM audit_log ORDER BY id")]
    assert actions[-2:] == ["app_setup_started", "connection_created"]

    page = admin.get("/admin/connections").get_data(as_text=True)
    assert "App created" in page and APP_ID.lower() in page and "Add your first client" in page
    assert f'data-value="{HOME}"' in page  # offers the MSP's own tenant

    resp = admin.post(
        "/tenants/new",
        data={
            "name": "Contoso",
            "tenant_ref": "contoso.onmicrosoft.com",
            "connection_id": connection["id"],
            "cloud": "commercial",
        },
    )
    tenant_id = int(resp.headers["Location"].rsplit("/", 1)[1])
    manage = admin.get(f"/tenants/{tenant_id}/manage").get_data(as_text=True)
    assert f"{LOGIN}/contoso.onmicrosoft.com/adminconsent?client_id={APP_ID.lower()}" in manage

    # With a tenant added, the wizard is finished and the page is back to the connection itself.
    page = admin.get("/admin/connections").get_data(as_text=True)
    assert "Connect Office Sentry to Microsoft 365" not in page and "Add a client" in page
    assert db.execute("SELECT COUNT(*) FROM setup_sign_ins").fetchone()[0] == 0


@responses.activate
def test_wizard_uses_a_certificate_generated_earlier_instead_of_a_second_connection(admin, db):
    # W4: "Generate certificate" then creating the app used to leave two connections, one never usable.
    admin.post("/admin/connections", data={"name": "Office Sentry", "cloud": "commercial"})
    pending = store.list_connections(db)[0]
    page = admin.get("/admin/connections").get_data(as_text=True)
    assert "It uses the certificate you already generated." in page
    _device_code()
    _token(SIGNED_IN)
    created = _graph()
    admin.post("/admin/connections/setup", data={})
    assert _check(admin)["status"] == "done"
    connections = store.list_connections(db)
    assert len(connections) == 1 and connections[0]["id"] == pending["id"]
    assert connections[0]["client_id"] == APP_ID.lower()
    assert json.loads(created.calls[0].request.body)["keyCredentials"] == [key_credential(pending["cert_pem"])]


@responses.activate
def test_cli_create_app_also_reuses_the_generated_certificate(db, cfg, vault, monkeypatch):
    from argparse import Namespace

    from officesentry import __main__ as cli
    from officesentry import entra

    store.create_connection(db, vault, "Office Sentry", generate_certificate("x"))
    monkeypatch.setattr(entra, "device_code_token", lambda tenant, say, cloud=None: TOKEN)  # MSAL's own loop
    _graph()
    cli.cmd_create_app(Namespace(name="Office Sentry (read-only)", tenant="organizations", cloud="commercial"), cfg)
    assert [c["client_id"] for c in store.list_connections(db)] == [APP_ID.lower()]


@pytest.mark.parametrize(
    ("body", "words"),
    [
        (
            {"error": "authorization_declined", "error_description": "AADSTS70000: The user declined."},
            "Sign-in was cancelled",
        ),
        (
            {
                "error": "invalid_grant",
                "error_description": "AADSTS53003: Access has been blocked by Conditional Access.",
            },
            "Conditional Access",
        ),
        ({"error": "expired_token", "error_description": "AADSTS70019: Verification code expired."}, "code expired"),
        (
            {"error": "invalid_grant", "error_description": "AADSTS99999: Something new.\r\nTrace ID: x"},
            "Microsoft refused the sign-in: AADSTS99999: Something new.",
        ),
    ],
)
@responses.activate
def test_sign_in_failures_say_what_to_do(admin, db, body, words):
    _device_code()
    _token((400, body))
    admin.post("/admin/connections/setup", data={})
    assert _check(admin)["status"] == "failed"
    row = db.execute("SELECT * FROM setup_sign_ins").fetchone()
    assert words in row["error"] and row["device_code_enc"] is None
    page = admin.get("/admin/connections").get_data(as_text=True)
    assert "The app wasn't created." in page and words.replace("'", "&#39;") in page
    assert "Sign in to Microsoft" in page  # start again from the same place
    assert not store.list_connections(db)


@responses.activate
def test_an_account_that_cant_create_apps_is_told_which_role_it_needs(admin, db):
    _device_code()
    _token(SIGNED_IN)
    _graph(create_status=403)
    admin.post("/admin/connections/setup", data={})
    assert _check(admin)["status"] == "failed"
    error = db.execute("SELECT error FROM setup_sign_ins").fetchone()["error"]
    assert "can't create app registrations" in error and "Application Administrator" in error
    assert not store.list_connections(db)  # nothing half-made


@responses.activate
def test_microsoft_refusing_to_start_is_shown_and_nothing_is_kept(admin, db):
    _device_code(
        status=400,
        body={"error": "invalid_request", "error_description": "AADSTS90002: Tenant 'nope.example' not found."},
    )
    resp = admin.post("/admin/connections/setup", data={}, follow_redirects=True)
    assert "Microsoft doesn&#39;t know that tenant" in resp.get_data(as_text=True)
    assert db.execute("SELECT COUNT(*) FROM setup_sign_ins").fetchone()[0] == 0


@responses.activate
def test_signing_in_to_a_named_tenant_and_cloud(admin, db):
    url = "https://login.microsoftonline.us/contoso.onmicrosoft.us/oauth2/v2.0/devicecode"
    start = responses.post(
        url,
        json={
            "device_code": DEVICE_CODE,
            "user_code": USER_CODE,
            "interval": 5,
            "verification_uri": "https://microsoft.com/deviceloginus",
            "expires_in": 900,
        },
    )
    admin.post("/admin/connections/setup", data={"cloud": "gcc_high", "tenant": "Contoso.onmicrosoft.us"})
    assert parse_qs(start.calls[0].request.body)["scope"] == ["https://graph.microsoft.us/Application.ReadWrite.All"]
    row = db.execute("SELECT cloud, tenant FROM setup_sign_ins").fetchone()
    assert (row["cloud"], row["tenant"]) == ("gcc_high", "contoso.onmicrosoft.us")
    assert "Create the app for US Government GCC High" in admin.get("/admin/connections").get_data(as_text=True)


@responses.activate
def test_bad_input_and_a_second_app_for_the_same_cloud_are_refused(admin, db, connection_id):
    page = admin.post("/admin/connections/setup", data={"tenant": "not a tenant"}, follow_redirects=True).get_data(
        as_text=True
    )
    assert "Enter your tenant&#39;s domain" in page
    page = admin.post("/admin/connections/setup", data={}, follow_redirects=True).get_data(as_text=True)
    assert "You already have an app for Commercial" in page
    assert admin.post("/admin/connections/setup", data={"cloud": "mars"}).status_code == 400
    assert not responses.calls and db.execute("SELECT COUNT(*) FROM setup_sign_ins").fetchone()[0] == 0
    # With the Commercial app made, the page offers the wizard for the other clouds only.
    page = admin.get("/admin/connections").get_data(as_text=True)
    assert "Create an app for another Microsoft cloud" in page
    another = page.split('id="another"', 1)[1].split("</details>", 1)[0]
    assert 'value="gcc_high"' in another and 'value="commercial"' not in another.split("More options")[0]


@responses.activate
def test_an_expired_code_fails_without_asking_microsoft(admin, db):
    _device_code()
    admin.post("/admin/connections/setup", data={})
    db.execute("UPDATE setup_sign_ins SET expires_at = '2020-01-01T00:00:00Z'")
    assert _check(admin)["status"] == "failed"
    assert "code expired" in db.execute("SELECT error FROM setup_sign_ins").fetchone()["error"]
    assert [c.request.url for c in responses.calls] == [f"{LOGIN}/organizations/oauth2/v2.0/devicecode"]


@responses.activate
def test_only_one_check_at_a_time_talks_to_microsoft(admin, db):
    _device_code()
    admin.post("/admin/connections/setup", data={})
    now = datetime.now(UTC)
    # Another portal process is mid-check: this one waits for it rather than redeeming the code twice.
    db.execute(
        "UPDATE setup_sign_ins SET status = 'working', updated_at = ?",
        ((now - timedelta(seconds=20)).strftime("%Y-%m-%dT%H:%M:%SZ"),),
    )
    assert _check(admin)["status"] == "working"
    assert len(responses.calls) == 1
    # A check whose process died while creating the app is given up, saying what to look for in Entra.
    stale = (now - timedelta(minutes=6)).strftime("%Y-%m-%dT%H:%M:%SZ")
    db.execute("UPDATE setup_sign_ins SET updated_at = ?", (stale,))
    assert _check(admin)["status"] == "failed"
    assert "App registrations" in db.execute("SELECT error FROM setup_sign_ins").fetchone()["error"]


@responses.activate
def test_microsoft_not_answering_keeps_waiting(admin, db):
    import requests

    _device_code()
    admin.post("/admin/connections/setup", data={})
    responses.post(f"{LOGIN}/organizations/oauth2/v2.0/token", body=requests.ConnectionError("down"))
    answer = _check(admin)
    assert answer["status"] == "waiting" and "keep checking" in answer["note"]
    _next_poll_now(db)
    responses.replace(
        responses.POST, f"{LOGIN}/organizations/oauth2/v2.0/token", status=400, json={"error": "slow_down"}
    )
    answer = _check(admin)
    assert (answer["status"], answer["note"], answer["wait"]) == ("waiting", None, 10)  # asked to slow down


@responses.activate
def test_without_javascript_the_button_checks_and_comes_back(admin, db):
    _device_code()
    _token(PENDING)
    admin.post("/admin/connections/setup", data={})
    resp = admin.post("/admin/connections/setup/check")
    assert resp.status_code == 302 and resp.headers["Location"].endswith("#setup")
    page = admin.get("/admin/connections").get_data(as_text=True)
    assert "sign-in hasn&#39;t finished yet" in page


@responses.activate
def test_cancel_forgets_the_code(admin, db):
    _device_code()
    admin.post("/admin/connections/setup", data={})
    admin.post("/admin/connections/setup/cancel")
    assert db.execute("SELECT COUNT(*) FROM setup_sign_ins").fetchone()[0] == 0
    assert _check(admin) == {"status": "none"}


@responses.activate
def test_each_admin_has_their_own_sign_in_and_only_admins_get_one(app, admin, db):
    _device_code()
    admin.post("/admin/connections/setup", data={})
    other = app.test_client()
    make_user(db, "admin2")
    login(other, "admin2")
    assert USER_CODE not in other.get("/admin/connections").get_data(as_text=True)
    assert other.post("/admin/connections/setup/check", headers=JSON).get_json() == {"status": "none"}
    analyst = app.test_client()
    make_user(db, "analyst", role="analyst", all_tenants=True)
    login(analyst, "analyst")
    for url in ("/admin/connections/setup", "/admin/connections/setup/check", "/admin/connections/setup/cancel"):
        assert analyst.post(url).status_code == 403
    assert db.execute("SELECT COUNT(*) FROM setup_sign_ins").fetchone()[0] == 1


def test_page_warns_when_opened_at_another_address_than_consent_links_use(app, db):
    # Consent links send the client's admin back to OFFICESENTRY_BASE_URL (http://localhost:8000 here).
    make_user(db, "admin")
    for address, warned in (("http://localhost:8000", False), ("https://portal.example.com", True)):
        client = app.test_client()
        client.post("/login", data={"username": "admin", "password": PASSWORD}, base_url=address)
        page = client.get("/admin/connections", base_url=address).get_data(as_text=True)
        assert ("Office Sentry's address doesn't match this one." in page) is warned
        assert (page.count("portal.example.com") > 0) is warned


def test_tenant_input_is_checked():
    assert app_setup.tenant_ref("") == "organizations"
    assert app_setup.tenant_ref(" Contoso.OnMicrosoft.com ") == "contoso.onmicrosoft.com"
    assert app_setup.tenant_ref(HOME.upper()) == HOME
    assert app_setup.tenant_ref("contoso") is None and app_setup.tenant_ref("../x") is None


@responses.activate
def test_codes_nobody_came_back_for_are_erased_daily(admin, db):
    from officesentry import worker

    _device_code()
    admin.post("/admin/connections/setup", data={})
    db.execute("UPDATE setup_sign_ins SET expires_at = '2020-01-01T00:00:00Z'")
    app_setup.prune(db)
    row = db.execute("SELECT status, device_code_enc FROM setup_sign_ins").fetchone()
    assert (row["status"], row["device_code_enc"]) == ("failed", None)
    db.execute("UPDATE setup_sign_ins SET updated_at = '2020-01-01T00:00:00Z'")
    worker.prune(db, admin.application.extensions["officesentry.config"])
    assert db.execute("SELECT COUNT(*) FROM setup_sign_ins").fetchone()[0] == 0
