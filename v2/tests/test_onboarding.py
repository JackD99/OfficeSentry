from urllib.parse import parse_qs, urlparse

from officesentry import store
from officesentry.collectors import REGISTRY
from officesentry.entra import GRAPH_PERMISSIONS, required_resource_access
from tests.conftest import login, make_user

GUID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


def _consent_state(client, tenant_id):
    page = client.get(f"/tenants/{tenant_id}").get_data(as_text=True)
    href = page.split('href="https://login.microsoftonline.com/', 1)[1].split('"', 1)[0].replace("&amp;", "&")
    return parse_qs(urlparse("https://x/" + href).query)


def test_admin_adds_tenant_and_records_consent(client, db, connection_id):
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
