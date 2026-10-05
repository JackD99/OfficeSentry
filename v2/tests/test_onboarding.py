from urllib.parse import parse_qs, urlparse

import responses

from officesentry import store
from officesentry.collectors import REGISTRY
from officesentry.entra import GRAPH_PERMISSIONS, required_resource_access
from tests.conftest import login, make_user

GUID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
OTHER = "99999999-8888-7777-6666-555555555555"  # another client of the same MSP app


def _discovery(domain, tenant_guid=None, status=200):
    """Microsoft's public OpenID metadata for a domain (what the callback checks a returned GUID against)."""
    url = f"https://login.microsoftonline.com/{domain}/v2.0/.well-known/openid-configuration"
    if tenant_guid is None:
        responses.get(url, status=status, json={"error": "invalid_tenant"})
    else:
        responses.get(url, json={"issuer": f"https://login.microsoftonline.com/{tenant_guid}/v2.0",
                                 "token_endpoint": f"https://login.microsoftonline.com/{tenant_guid}/oauth2/v2.0/token"})


def _consent_state(client, tenant_id):
    page = client.get(f"/tenants/{tenant_id}/manage").get_data(as_text=True)
    href = page.split('href="https://login.microsoftonline.com/', 1)[1].split('"', 1)[0].replace("&amp;", "&")
    return parse_qs(urlparse("https://x/" + href).query)


@responses.activate
def test_admin_adds_tenant_and_records_consent(client, db, connection_id):
    _discovery("contoso.onmicrosoft.com", GUID)
    make_user(db, "admin")
    login(client, "admin")
    resp = client.post("/tenants/new", data={"name": "Contoso", "tenant_ref": "contoso.onmicrosoft.com",
                                             "connection_id": connection_id})
    tenant_id = int(resp.headers["Location"].rsplit("/", 1)[1])
    query = _consent_state(client, tenant_id)
    assert query["client_id"] == ["11111111-2222-3333-4444-555555555555"]
    assert query["redirect_uri"][0].endswith("/consent/callback")

    resp = client.get("/consent/callback", query_string={
        "admin_consent": "True", "tenant": GUID, "state": query["state"][0]})
    assert resp.status_code == 302
    t = store.get_tenant(db, tenant_id)
    assert t["consented_at"] and t["tenant_ref"] == GUID
    queued = db.execute("SELECT collector, status FROM runs WHERE tenant_id = ? ORDER BY id", (tenant_id,)).fetchall()
    assert [(r["collector"], r["status"]) for r in queued] == [(key, "queued") for key in REGISTRY]


def test_consent_callback_rejects_forged_state(client, db, two_tenants):
    make_user(db, "admin")
    login(client, "admin")
    resp = client.get("/consent/callback", query_string={"admin_consent": "True", "tenant": GUID, "state": "x"})
    assert resp.status_code == 400


def test_consent_from_wrong_tenant_is_refused(client, db, connection_id):
    make_user(db, "admin")
    login(client, "admin")
    tenant_id = store.create_tenant(db, "Pinned", GUID, connection_id)
    state = _consent_state(client, tenant_id)["state"][0]
    client.get("/consent/callback", query_string={
        "admin_consent": "True", "tenant": "ffffffff-bbbb-cccc-dddd-eeeeeeeeeeee", "state": state})
    assert store.get_tenant(db, tenant_id)["consented_at"] is None


def _callback(client, state, tenant=GUID):
    return client.get("/consent/callback", query_string={"admin_consent": "True", "tenant": tenant, "state": state})


def _ref(db, tenant_id):
    t = store.get_tenant(db, tenant_id)
    return t["tenant_ref"], t["consented_at"]


@responses.activate
def test_consent_callback_cannot_point_a_domain_at_another_tenant(client, db, connection_id):
    # A client's admin gets the consent link (and its state) and crafts a callback naming the GUID of a
    # different client that already consented to the same app. The domain's real tenant is GUID.
    _discovery("alpha.onmicrosoft.com", GUID)
    make_user(db, "admin")
    login(client, "admin")
    alpha = store.create_tenant(db, "Alpha Ltd", "alpha.onmicrosoft.com", connection_id)
    state = _consent_state(client, alpha)["state"][0]
    assert _callback(client, state, OTHER).status_code == 302
    assert _ref(db, alpha) == ("alpha.onmicrosoft.com", None)
    assert not db.execute("SELECT 1 FROM runs WHERE tenant_id = ?", (alpha,)).fetchone()
    shown = client.get(f"/tenants/{alpha}").get_data(as_text=True)
    assert "different tenant" in shown


@responses.activate
def test_consent_state_is_single_use(client, db, connection_id):
    _discovery("alpha.onmicrosoft.com", GUID)
    make_user(db, "admin")
    login(client, "admin")
    alpha = store.create_tenant(db, "Alpha Ltd", "alpha.onmicrosoft.com", connection_id)
    state = _consent_state(client, alpha)["state"][0]
    assert _callback(client, state).status_code == 302
    assert _ref(db, alpha)[0] == GUID
    runs = db.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
    db.execute("UPDATE tenants SET consented_at = NULL")
    page = _callback(client, state).status_code, client.get(f"/tenants/{alpha}").get_data(as_text=True)
    assert page[0] == 302 and "already been used or has expired" in page[1]
    assert store.get_tenant(db, alpha)["consented_at"] is None  # the replay recorded nothing
    assert db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == runs


@responses.activate
def test_consent_state_belongs_to_the_admin_session_that_opened_it(app, db, connection_id):
    _discovery("alpha.onmicrosoft.com", GUID)
    make_user(db, "admin")
    make_user(db, "admin2")
    alpha = store.create_tenant(db, "Alpha Ltd", "alpha.onmicrosoft.com", connection_id)
    first, second, other_admin = app.test_client(), app.test_client(), app.test_client()
    login(first, "admin")
    state = _consent_state(first, alpha)["state"][0]
    login(second, "admin")  # same admin, another browser
    login(other_admin, "admin2")
    for browser in (second, other_admin):
        _callback(browser, state)
        assert _ref(db, alpha) == ("alpha.onmicrosoft.com", None)
    assert _callback(first, state).status_code == 302
    assert _ref(db, alpha)[0] == GUID


@responses.activate
def test_consent_is_recorded_without_the_swap_when_microsoft_cannot_be_asked(client, db, connection_id):
    _discovery("alpha.onmicrosoft.com", status=503)
    make_user(db, "admin")
    login(client, "admin")
    alpha = store.create_tenant(db, "Alpha Ltd", "alpha.onmicrosoft.com", connection_id)
    state = _consent_state(client, alpha)["state"][0]
    page = _callback(client, state, OTHER)
    ref, consented = _ref(db, alpha)
    assert page.status_code == 302 and ref == "alpha.onmicrosoft.com" and consented
    shown = client.get(f"/tenants/{alpha}").get_data(as_text=True)
    assert "couldn&#39;t confirm" in shown


@responses.activate
def test_consent_refuses_a_guid_another_tenant_already_uses(client, db, connection_id):
    _discovery("alpha.onmicrosoft.com", GUID)
    make_user(db, "admin")
    login(client, "admin")
    store.create_tenant(db, "Existing", GUID, connection_id)
    alpha = store.create_tenant(db, "Alpha Ltd", "alpha.onmicrosoft.com", connection_id)
    state = _consent_state(client, alpha)["state"][0]
    assert _callback(client, state).status_code == 302  # not a 500 from the UNIQUE constraint
    assert _ref(db, alpha) == ("alpha.onmicrosoft.com", None)


def test_invalid_tenant_ref_rejected(client, db, connection_id):
    make_user(db, "admin")
    login(client, "admin")
    client.post("/tenants/new", data={"name": "Bad", "tenant_ref": "not a domain", "connection_id": connection_id})
    assert db.execute("SELECT COUNT(*) FROM tenants").fetchone()[0] == 0


def test_requested_permissions_are_read_only():
    for name in GRAPH_PERMISSIONS:
        assert ".Read" in name and "Write" not in name, name


def test_required_resource_access_maps_role_ids():
    sp = {"appRoles": [{"value": p, "id": f"id-{p}", "allowedMemberTypes": ["Application"]}
                       for p in GRAPH_PERMISSIONS]}
    access = required_resource_access(sp)[0]["resourceAccess"]
    assert {a["id"] for a in access} == {f"id-{p}" for p in GRAPH_PERMISSIONS}
    assert all(a["type"] == "Role" for a in access)


def test_admin_generates_connection_and_sets_client_id(client, db):
    make_user(db, "admin")
    login(client, "admin")
    client.post("/admin/connections", data={"name": "MSP app"})
    conn = store.list_connections(db)[0]
    assert conn["client_id"] is None and len(conn["thumbprint"]) == 40
    client.post(f"/admin/connections/{conn['id']}/client-id", data={"client_id": "not-a-guid"})
    assert store.get_connection(db, conn["id"])["client_id"] is None
    client.post(f"/admin/connections/{conn['id']}/client-id", data={"client_id": GUID.upper()})
    assert store.get_connection(db, conn["id"])["client_id"] == GUID
