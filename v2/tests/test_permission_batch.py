"""The 2.2 permission batch: new read-only permissions released together, what waits for them, the What's new
page and its PDF, and updating the MSP's app registration.

Microsoft is simulated at the HTTP level: Graph for the connection collector (``tests.seed``) and for the app
registration in the MSP's own tenant, and the sign-in service for the update wizard.
"""

import json
from types import SimpleNamespace

import jwt
import pytest
import responses
from responses import matchers

from officesentry import onboarding, permission_batch, store, tenant_health
from officesentry.entra import (
    GRAPH_APP_ID,
    GRAPH_PERMISSIONS,
    PERMISSIONS,
    SHAREPOINT_APP_ID,
    merged_resource_access,
    update_app_permissions,
)
from officesentry.graph import GraphClient
from officesentry.worker import execute_run
from tests.conftest import login, make_user
from tests.seed import exchange_factory, mock_tenant

BATCH = permission_batch.CURRENT
GRAPH = "https://graph.microsoft.com/v1.0"
LOGIN = "https://login.microsoftonline.com"
CLIENT_ID = "11111111-2222-3333-4444-555555555555"  # conftest's app connection
TOKEN = jwt.encode({"tid": "c0ffee00-1111-2222-3333-444455556666"}, "not-checked", algorithm="HS256")


def _graph(tenant, connection):
    return GraphClient(lambda: "t", sleep=lambda s: None)


def _check(db, tenant_id, **our_app):
    """Run only the connection collector against the seeded responses."""
    with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        mock_tenant(rsps, our_app=our_app)
        store.enqueue_run(db, tenant_id, "connection")
        status = execute_run(db, store.claim_next_run(db), _graph, exchange_factory)
    return status, tenant_health.connection_result(db, tenant_id)


def _queued(db, tenant_id) -> bool:
    return bool(
        db.execute(
            "SELECT 1 FROM runs WHERE tenant_id = ? AND collector = 'connection' AND status = 'queued'", (tenant_id,)
        ).fetchone()
    )


def _admin(client, db):
    make_user(db, "admin")
    login(client, "admin")


# -- The batch itself ---------------------------------------------------------------------------------


def test_the_batch_holds_the_nine_permissions_other_work_asked_for():
    assert BATCH.release == "2.2"
    assert set(BATCH.permissions) == {
        "Channel.ReadBasic.All",
        "ChannelMember.Read.All",
        "TeamsAppInstallation.ReadForTeam.All",
        "ReportSettings.Read.All",
        "PrivilegedEligibilitySchedule.Read.AzureADGroup",
        "BitLockerKey.ReadBasic.All",
        "DeviceManagementConfiguration.Read.All",
        "DeviceManagementApps.Read.All",
        "SharePoint Sites.Read.All",
    }
    for p in BATCH.permissions:  # each is asked for, explained to the client and says what it adds
        assert p in PERMISSIONS and onboarding.CLIENT_WORDS[p] and permission_batch.ADDS[p]
    assert set(permission_batch.ADDS) == set(permission_batch.BATCH_OF)
    # Nothing that writes: every name is a read permission.
    assert all(".Read" in name for _, name, _ in PERMISSIONS.values() if name != "Exchange.ManageAsApp")


def test_sharepoints_permission_is_not_mistaken_for_graphs_of_the_same_name():
    graph, spo = PERMISSIONS["Sites.Read.All"], PERMISSIONS["SharePoint Sites.Read.All"]
    assert graph[0].app_id == GRAPH_APP_ID and spo[0].app_id == SHAREPOINT_APP_ID
    assert graph[1] == spo[1] == "Sites.Read.All"
    assert permission_batch.release_of("Sites.Read.All") is None
    assert permission_batch.release_of("SharePoint Sites.Read.All") == "2.2"


# -- Waiting for approval is a step to do, not a problem ---------------------------------------------------


def test_a_client_that_hasnt_approved_the_batch_stays_healthy_and_says_what_waits(db, two_tenants):
    alpha, _ = two_tenants
    status, result = _check(db, alpha, missing=BATCH.permissions)
    assert status == "succeeded"  # not partial: nothing failed
    assert permission_batch.waiting(result) == list(BATCH.permissions)
    assert permission_batch.missing_original(result) == []
    run = store.latest_run(db, alpha, "connection")
    assert not run["error"]
    logged = " ".join(r["message"] for r in db.execute("SELECT message FROM run_events WHERE run_id = ?", (run["id"],)))
    assert "Waiting for the client to approve 9 new permissions" in logged

    health = tenant_health.assess(db, store.get_tenant(db, alpha))
    perms = health.check("permissions")
    assert perms.state == "todo" and perms.action == "whats_new" and not perms.problem
    assert "9 new permissions from Office Sentry 2.2" in perms.summary
    assert health.waiting == list(BATCH.permissions) and health.missing == []
    assert next(s for s in health.steps if s.key == "permissions").done  # still onboarded


def test_a_missing_original_permission_is_still_a_problem(db, two_tenants):
    alpha, _ = two_tenants
    status, result = _check(db, alpha, missing=("Sites.Read.All", "SharePoint Sites.Read.All"))
    assert status == "partial"
    assert permission_batch.missing_original(result) == ["Sites.Read.All"]
    assert (
        "1 permission not granted in this tenant: Sites.Read.All." in store.latest_run(db, alpha, "connection")["error"]
    )
    perms = tenant_health.assess(db, store.get_tenant(db, alpha)).check("permissions")
    assert perms.state == "bad" and "SharePoint" not in perms.summary


def test_reports_and_collectors_say_they_wait_rather_than_fail(db, two_tenants):
    from officesentry.collectors.connection import awaiting
    from officesentry.snapshot import TenantSnapshot

    alpha, bravo = two_tenants
    _check(db, alpha, missing=("Channel.ReadBasic.All",))
    _check(db, bravo)
    snap = TenantSnapshot(db, store.get_tenant(db, alpha))
    text = permission_batch.notice(snap, "Channel.ReadBasic.All", "ChannelMember.Read.All")
    assert text.startswith("Waiting for Alpha Ltd to approve the new read-only permission this needs")
    assert "(Channel.ReadBasic.All)" in text and "2.2" in text
    assert permission_batch.notice(TenantSnapshot(db, store.get_tenant(db, bravo)), "Channel.ReadBasic.All") is None
    ctx = SimpleNamespace(conn=db, tenant=store.get_tenant(db, alpha))
    assert awaiting(ctx, "ChannelMember.Read.All", "Channel.ReadBasic.All") == ["Channel.ReadBasic.All"]
    # Never checked: try rather than wait for nothing.
    db.execute("DELETE FROM runs WHERE collector = 'connection' AND tenant_id = ?", (alpha,))
    assert awaiting(ctx, "Channel.ReadBasic.All") == []


# -- The tracker and What's new ------------------------------------------------------------------------


def test_tracker_follows_the_batch_from_ask_to_approved(app, client, db, two_tenants):
    alpha, bravo = two_tenants
    _check(db, alpha, missing=BATCH.permissions)
    _check(db, bravo)

    def state(tenant_id):
        t = store.get_tenant(db, tenant_id)
        return onboarding.state(
            t,
            onboarding.row_for(db, tenant_id),
            tenant_health.connection_result(db, tenant_id),
            permission_batch.request_for(db, tenant_id),
        )

    assert state(alpha).key == "new_permissions" and state(bravo).key == "connected"
    onboarding.mark_sent(db, [alpha], "admin")
    assert state(alpha).key == "new_sent" and onboarding.row_for(db, alpha) is None  # the batch, not onboarding
    onboarding.client_approved(db, store.get_tenant(db, alpha))
    assert state(alpha).key == "new_approved"
    assert _queued(db, alpha)  # re-reads its permissions

    _admin(client, db)
    page = client.get("/admin/onboarding").get_data(as_text=True)
    assert "New permissions" in page and "Alpha Ltd" in page and "Approved, re-checking" in page
    # Bravo granted the whole batch, which proves the app asks for it: no "update the app first".
    assert "before you send the new links" not in page
    csv = client.get("/admin/onboarding/export.csv").get_data(as_text=True)
    assert "Alpha Ltd" in csv and "Bravo Inc" not in csv


def test_whats_new_page_and_pdf(client, db, two_tenants):
    alpha, _ = two_tenants
    _check(db, alpha, missing=("Channel.ReadBasic.All", "SharePoint Sites.Read.All"))
    _admin(client, db)
    page = client.get(f"/admin/onboarding/{alpha}").get_data(as_text=True)
    assert "What's new for Alpha Ltd" in page and "New permissions to approve" in page
    assert "Office Sentry 2.2 asks for 2 new read-only permissions" in page
    assert "Channel.ReadBasic.All" in page and "Office 365 SharePoint Online" in page
    assert "First, update your app." in page and "Copy message" not in page  # nothing to send yet

    db.execute("UPDATE app_connections SET permissions_release = '2.2'")
    page = client.get(f"/admin/onboarding/{alpha}").get_data(as_text=True)
    assert "First, update your app." not in page and "Copy message" in page
    assert "New read-only reports for Alpha Ltd: one approval needed" in page
    assert "adminconsent?client_id=" in page

    client.post(f"/admin/onboarding/{alpha}/sent", data={"sent": "1"})
    assert permission_batch.request_for(db, alpha)["sent_by"] == "admin"
    resp = client.post(f"/admin/onboarding/{alpha}/check", follow_redirects=True)
    assert "Checking which permissions the client has approved" in resp.get_data(as_text=True)
    assert _queued(db, alpha)

    pdf = client.get(f"/admin/onboarding/{alpha}/whats-new.pdf")
    assert pdf.status_code == 200
    from officesentry.reports import export

    if not export.pdf_available():
        text = pdf.get_data(as_text=True)
        assert "New read-only permissions to approve" in text and "Channel.ReadBasic.All" in text


def test_the_app_is_ready_once_a_client_has_granted_the_whole_batch(db, two_tenants, connection_id):
    alpha, bravo = two_tenants
    connection = store.get_connection(db, connection_id)
    _check(db, alpha, missing=BATCH.permissions)
    assert not permission_batch.app_ready(db, connection)
    _check(db, bravo)  # granted everything: the app must be asking for it
    assert permission_batch.app_ready(db, connection)


@pytest.mark.parametrize("url", ["/admin/onboarding/{t}", "/admin/onboarding/{t}/whats-new.pdf"])
def test_analysts_cannot_open_whats_new(app, db, two_tenants, url):
    alpha, _ = two_tenants
    make_user(db, "analyst", role="analyst", tenants=[alpha])
    c = app.test_client()
    login(c, "analyst")
    assert c.get(url.format(t=alpha)).status_code == 403


# -- Updating the MSP's app registration ------------------------------------------------------------------


def _resources():
    for app_id, roles in (
        (GRAPH_APP_ID, list(GRAPH_PERMISSIONS)),
        ("00000002-0000-0ff1-ce00-000000000000", ["Exchange.ManageAsApp"]),
        (SHAREPOINT_APP_ID, ["Sites.Read.All"]),
    ):
        prefix = "spo" if app_id == SHAREPOINT_APP_ID else ("exo" if app_id.startswith("00000002") else "graph")
        responses.get(
            f"{GRAPH}/servicePrincipals",
            json={
                "value": [
                    {
                        "id": f"sp-{prefix}",
                        "appRoles": [
                            {"id": f"{prefix}-{n}", "value": n, "allowedMemberTypes": ["Application"]}
                            for n in sorted(set(roles))
                        ],
                    }
                ]
            },
            match=[matchers.query_param_matcher({"$filter": f"appId eq '{app_id}'", "$select": "id,appRoles"})],
        )


def _app(access):
    return responses.get(
        f"{GRAPH}/applications",
        json={
            "value": [{"id": "obj-1", "appId": CLIENT_ID, "displayName": "Test app", "requiredResourceAccess": access}]
        },
    )


ORIGINAL = [
    {"resourceAppId": GRAPH_APP_ID, "resourceAccess": [{"id": "graph-User.Read.All", "type": "Role"}]},
    {"resourceAppId": "custom-api", "resourceAccess": [{"id": "x", "type": "Scope"}]},  # added by hand: kept
]


def test_merge_adds_what_is_missing_and_keeps_the_rest():
    wanted = [
        {
            "resourceAppId": GRAPH_APP_ID,
            "resourceAccess": [{"id": "graph-User.Read.All", "type": "Role"}, {"id": "n", "type": "Role"}],
        },
        {"resourceAppId": SHAREPOINT_APP_ID, "resourceAccess": [{"id": "s", "type": "Role"}]},
    ]
    merged, added = merged_resource_access(ORIGINAL, wanted)
    assert added == 2
    by_app = {m["resourceAppId"]: [a["id"] for a in m["resourceAccess"]] for m in merged}
    assert by_app == {GRAPH_APP_ID: ["graph-User.Read.All", "n"], "custom-api": ["x"], SHAREPOINT_APP_ID: ["s"]}
    assert merged_resource_access(merged, wanted)[1] == 0


@responses.activate
def test_update_adds_the_batch_to_the_app_and_only_patches_the_app():
    _resources()
    _app(ORIGINAL)
    patch = responses.patch(f"{GRAPH}/applications/obj-1", status=204)
    updated = update_app_permissions(TOKEN, CLIENT_ID)
    assert updated.added == len(PERMISSIONS) - 1  # User.Read.All was there
    body = json.loads(patch.calls[0].request.body)
    ids = {a["id"] for r in body["requiredResourceAccess"] for a in r["resourceAccess"]}
    assert {"spo-Sites.Read.All", "graph-Channel.ReadBasic.All", "x"} <= ids
    assert [c.request.method for c in responses.calls].count("PATCH") == 1


@responses.activate
def test_update_wizard_signs_in_updates_and_marks_the_connection(client, db, connection_id):
    make_user(db, "admin")
    login(client, "admin")
    page = client.get("/admin/connections").get_data(as_text=True)
    assert "Update this app for Office Sentry 2.2" in page and "New in 2.2" in page

    responses.post(
        f"{LOGIN}/organizations/oauth2/v2.0/devicecode",
        json={
            "device_code": "dc",
            "user_code": "ABCD-EFGH",
            "verification_uri": "https://microsoft.com/devicelogin",
            "expires_in": 900,
            "interval": 5,
        },
    )
    client.post(f"/admin/connections/{connection_id}/update", data={})
    page = client.get("/admin/connections").get_data(as_text=True)
    assert "ABCD-EFGH" in page and "Connect Office Sentry to Microsoft 365" not in page  # not the create wizard
    row = db.execute("SELECT purpose, connection_id FROM setup_sign_ins").fetchone()
    assert (row["purpose"], row["connection_id"]) == ("update", connection_id)

    responses.post(f"{LOGIN}/organizations/oauth2/v2.0/token", json={"access_token": TOKEN})
    _resources()
    _app(ORIGINAL)
    responses.patch(f"{GRAPH}/applications/obj-1", status=204)
    assert (
        client.post("/admin/connections/setup/check", headers={"Accept": "application/json"}).get_json()["status"]
        == "done"
    )
    assert store.get_connection(db, connection_id)["permissions_release"] == "2.2"
    assert not any(c.request.method == "POST" and c.request.url.startswith(GRAPH) for c in responses.calls)
    page = client.get("/admin/connections").get_data(as_text=True)
    assert "now asks for every permission" in page and "Update this app for Office Sentry 2.2" not in page
    actions = [r["action"] for r in db.execute("SELECT action FROM audit_log ORDER BY id")]
    assert "connection_permissions_updated" in actions and "app_updated" in actions


@responses.activate
def test_update_wizard_says_why_when_the_account_cant_change_the_app(client, db, connection_id):
    from officesentry import app_setup

    make_user(db, "admin")
    login(client, "admin")
    responses.post(
        f"{LOGIN}/organizations/oauth2/v2.0/devicecode",
        json={
            "device_code": "dc",
            "user_code": "ABCD-EFGH",
            "verification_uri": "https://x",
            "expires_in": 900,
            "interval": 5,
        },
    )
    client.post(f"/admin/connections/{connection_id}/update", data={})
    responses.post(f"{LOGIN}/organizations/oauth2/v2.0/token", json={"access_token": TOKEN})
    responses.get(f"{GRAPH}/applications", json={"value": []})
    client.post("/admin/connections/setup/check", headers={"Accept": "application/json"})
    row = app_setup.current(db, db.execute("SELECT id FROM users").fetchone()["id"], "update")
    assert row["status"] == "failed" and "no app registration with client ID" in row["error"]
    assert store.get_connection(db, connection_id)["permissions_release"] is None


def test_added_by_hand(client, db, connection_id):
    make_user(db, "admin")
    login(client, "admin")
    client.post(f"/admin/connections/{connection_id}/updated-by-hand")
    assert store.get_connection(db, connection_id)["permissions_release"] == "2.2"


def test_an_app_made_by_hand_from_the_steps_is_not_asked_to_update(client, db, vault):
    """The page's steps for making the app by hand list every permission, the batch's included."""
    from officesentry.certs import generate_certificate

    conn_id = store.create_connection(db, vault, "By hand", generate_certificate("h"))
    make_user(db, "admin")
    login(client, "admin")
    client.post(f"/admin/connections/{conn_id}/client-id", data={"client_id": CLIENT_ID})
    assert store.get_connection(db, conn_id)["permissions_release"] == BATCH.release
    assert "Update this app for Office Sentry" not in client.get("/admin/connections").get_data(as_text=True)

    # A connection that already had an app keeps what it knew: the new ID may be an older app.
    db.execute("UPDATE app_connections SET permissions_release = NULL WHERE id = ?", (conn_id,))
    db.commit()
    client.post(f"/admin/connections/{conn_id}/client-id", data={"client_id": CLIENT_ID.replace("1", "9")})
    assert store.get_connection(db, conn_id)["permissions_release"] is None


def test_a_new_app_asks_for_the_latest_batch(db, vault):
    from officesentry import app_setup
    from officesentry.certs import generate_certificate

    conn_id = store.create_connection(db, vault, "Pending", generate_certificate("p"))
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("officesentry.entra.create_app_registration", lambda *a, **k: {"appId": CLIENT_ID})
        made, _ = app_setup.register_app(db, vault, TOKEN, "https://x/consent/callback")
    assert made == conn_id and store.get_connection(db, conn_id)["permissions_release"] == "2.2"
