import json
from datetime import UTC, datetime

import pytest
import responses

from officesentry import store
from officesentry.collectors import REGISTRY
from officesentry.graph import GRAPH_ROOT, GraphClient
from officesentry.worker import execute_run, schedule_tick

V1 = f"{GRAPH_ROOT}/v1.0"

SKUS = [
    {"skuId": "sku-bp", "skuPartNumber": "SPB", "appliesTo": "User", "capabilityStatus": "Enabled",
     "prepaidUnits": {"enabled": 10}, "consumedUnits": 7,
     "servicePlans": [{"servicePlanName": "AAD_PREMIUM", "provisioningStatus": "Success"},
                      {"servicePlanName": "INTUNE_A", "provisioningStatus": "Success"},
                      {"servicePlanName": "EXCHANGE_S_STANDARD", "provisioningStatus": "Success"},
                      {"servicePlanName": "SHAREPOINTSTANDARD", "provisioningStatus": "Success"}]},
    {"skuId": "sku-old", "skuPartNumber": "OLD", "appliesTo": "User", "capabilityStatus": "Suspended",
     "prepaidUnits": {"enabled": 0}, "consumedUnits": 0,
     "servicePlans": [{"servicePlanName": "AAD_PREMIUM_P2", "provisioningStatus": "Success"}]},
]

USERS = [
    {"id": "u1", "displayName": "Ann", "userPrincipalName": "ann@x.com", "userType": "Member",
     "accountEnabled": True, "assignedLicenses": [{"skuId": "sku-bp"}],
     "signInActivity": {"lastSignInDateTime": "2026-09-30T10:00:00Z"}},
    {"id": "u2", "displayName": "Bob", "userPrincipalName": "bob@x.com", "userType": "Member",
     "accountEnabled": False, "assignedLicenses": [{"skuId": "sku-bp"}], "signInActivity": {}},
    {"id": "u3", "displayName": "Guest", "userPrincipalName": "g#EXT#@x.com", "userType": "Guest",
     "accountEnabled": True, "assignedLicenses": []},
]


@pytest.fixture
def factory():
    return lambda tenant, connection: GraphClient(lambda: "t", sleep=lambda s: None)


def _metrics(db, run_id):
    return {r["key"]: r["value"] for r in db.execute("SELECT key, value FROM metrics WHERE run_id = ?", (run_id,))}


def _run(db, tenant_id, collector, factory):
    store.enqueue_run(db, tenant_id, collector)
    run = store.claim_next_run(db)
    return run["id"], execute_run(db, run, factory)


@responses.activate
def test_tenant_profile_detects_capabilities(db, two_tenants, factory):
    alpha, _ = two_tenants
    responses.get(f"{V1}/organization", json={"value": [{"id": "org1", "displayName": "Alpha"}]})
    responses.get(f"{V1}/subscribedSkus", json={"value": SKUS})
    run_id, status = _run(db, alpha, "tenant_profile", factory)
    assert status == "succeeded"
    caps = store.tenant_capabilities(store.get_tenant(db, alpha))
    assert caps["entra_p1"] and caps["intune"] and caps["exchange"]
    assert not caps["entra_p2"]  # only on a suspended SKU
    assert _metrics(db, run_id) == {"licences.purchased": 10, "licences.assigned": 7, "licences.unassigned": 3}


@responses.activate
def test_users_with_sign_in_activity(db, two_tenants, factory):
    alpha, _ = two_tenants
    responses.get(f"{V1}/users", json={"value": USERS})
    run_id, status = _run(db, alpha, "users", factory)
    assert status == "succeeded"
    assert "signInActivity" in responses.calls[0].request.url
    users = {u["_id"]: u for u in store.run_objects(db, run_id, "user")}
    assert users["u1"]["lastSignInDateTime"] == "2026-09-30T10:00:00Z"
    assert users["u2"]["signInActivityAvailable"] is True and users["u2"]["lastSignInDateTime"] is None
    m = _metrics(db, run_id)
    assert (m["users.total"], m["users.members"], m["users.guests"]) == (3, 2, 1)
    assert m["users.licensed_disabled"] == 1


@responses.activate
def test_users_fall_back_without_p1(db, two_tenants, factory):
    alpha, _ = two_tenants
    responses.get(f"{V1}/users", status=403, json={"error": {
        "code": "Authentication_RequestFromNonPremiumTenantOrB2CTenant", "message": "premium license"}})
    plain = [{k: v for k, v in u.items() if k != "signInActivity"} for u in USERS]
    responses.get(f"{V1}/users", json={"value": plain})
    run_id, status = _run(db, alpha, "users", factory)
    assert status == "partial"
    run = db.execute("SELECT error FROM runs WHERE id = ?", (run_id,)).fetchone()
    assert "Entra ID P1" in run["error"]
    users = store.run_objects(db, run_id, "user")
    assert all(u["signInActivityAvailable"] is False for u in users)


@responses.activate
def test_users_skip_activity_when_tenant_known_without_p1(db, two_tenants, factory):
    alpha, _ = two_tenants
    store.set_capabilities(db, alpha, {"entra_p1": False})
    responses.get(f"{V1}/users", json={"value": []})
    _, status = _run(db, alpha, "users", factory)
    assert status == "succeeded"
    assert len(responses.calls) == 1 and "signInActivity" not in responses.calls[0].request.url


@responses.activate
def test_permission_error_fails_run_with_reason(db, two_tenants, factory):
    alpha, _ = two_tenants
    responses.get(f"{V1}/organization", status=403, json={"error": {
        "code": "Authorization_RequestDenied", "message": "Insufficient privileges"}})
    run_id, status = _run(db, alpha, "tenant_profile", factory)
    assert status == "failed"
    assert "Insufficient privileges" in db.execute("SELECT error FROM runs WHERE id = ?", (run_id,)).fetchone()[0]


def test_collector_crash_does_not_escape(db, two_tenants):
    alpha, _ = two_tenants

    def broken(tenant, connection):
        raise RuntimeError("boom")

    _, status = _run(db, alpha, "users", broken)
    assert status == "failed"


def test_tenant_without_client_id_fails_cleanly(db, two_tenants, vault):
    from officesentry.worker import default_graph_factory

    alpha, _ = two_tenants
    db.execute("UPDATE app_connections SET client_id = NULL")
    run_id, status = _run(db, alpha, "users", default_graph_factory(vault))
    assert status == "failed"
    assert "client ID" in db.execute("SELECT error FROM runs WHERE id = ?", (run_id,)).fetchone()[0]


def test_enqueue_is_deduplicated_and_claim_is_fifo(db, two_tenants):
    alpha, bravo = two_tenants
    first = store.enqueue_run(db, alpha, "users")
    assert store.enqueue_run(db, alpha, "users") == first
    second = store.enqueue_run(db, bravo, "users")
    assert store.claim_next_run(db)["id"] == first
    assert store.claim_next_run(db)["id"] == second
    assert store.claim_next_run(db) is None


def test_orphaned_runs_are_failed_on_startup(db, two_tenants):
    alpha, _ = two_tenants
    store.enqueue_run(db, alpha, "users")
    store.claim_next_run(db)
    assert store.recover_orphaned_runs(db) == 1
    assert db.execute("SELECT status FROM runs").fetchone()[0] == "failed"


def test_schedule_queues_each_slot_once(db, cfg, two_tenants):
    now = datetime(2026, 10, 2, 3, 0, tzinfo=UTC)
    assert schedule_tick(db, cfg, now) == 2 * len(REGISTRY)
    assert schedule_tick(db, cfg, now) == 0
    # Even after the runs finish, the same slot is not queued again.
    db.execute("UPDATE runs SET status = 'succeeded'")
    assert schedule_tick(db, cfg, now) == 0
    order = [r["collector"] for r in db.execute("SELECT collector FROM runs WHERE tenant_id = ? ORDER BY id",
                                                (two_tenants[0],))]
    assert order[:2] == ["tenant_profile", "users"]  # capabilities first, then users


def test_disabled_tenants_are_not_scheduled(db, cfg, two_tenants):
    db.execute("UPDATE tenants SET enabled = 0")
    assert schedule_tick(db, cfg, datetime(2026, 10, 2, 3, 0, tzinfo=UTC)) == 0


def test_objects_are_json(db, two_tenants):
    alpha, _ = two_tenants
    rid = store.enqueue_run(db, alpha, "users")
    store.add_objects(db, rid, "user", [("u1", {"n": 1})])
    raw = db.execute("SELECT data FROM objects").fetchone()[0]
    assert json.loads(raw) == {"n": 1}
