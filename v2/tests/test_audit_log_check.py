"""Whether a client's Microsoft 365 audit log is on: read with ``Get-AdminAuditLogConfig``, judged by the
``audit_log`` check and shown on the onboarding tracker before anyone asks the client to approve the audit
log add-on (Exchange simulated at the HTTP level)."""

from officesentry import onboarding, store
from officesentry.checks import evaluate
from officesentry.snapshot import TenantSnapshot
from tests import graphdata as gd
from tests.conftest import login, make_user
from tests.seed import seed_tenant


def _check(db, tenant_id):
    return next(r for r in evaluate(TenantSnapshot(db, store.get_tenant(db, tenant_id))) if r.key == "audit_log")


def test_audit_log_on_passes_and_off_is_a_high_finding(db, two_tenants, monkeypatch):
    alpha, bravo = two_tenants
    seed_tenant(db, alpha)
    monkeypatch.setitem(gd.EXCHANGE, "Get-AdminAuditLogConfig", [{"UnifiedAuditLogIngestionEnabled": False}])
    seed_tenant(db, bravo)

    assert _check(db, alpha).status == "pass"
    off = _check(db, bravo)
    assert off.status == "fail" and off.severity == "high"
    assert off.summary == "The Microsoft 365 audit log is turned off"
    assert "Microsoft Purview" in off.recommendation
    assert (onboarding.audit_log_on(db, alpha), onboarding.audit_log_on(db, bravo)) == (True, False)


def test_an_exchange_refusal_is_not_known_and_the_run_still_succeeds(db, two_tenants, monkeypatch):
    alpha, _ = two_tenants
    refused = (403, {"error": {"code": "Forbidden", "message": "The role assigned to application isn't supported"}})
    monkeypatch.setitem(gd.EXCHANGE, "Get-AdminAuditLogConfig", refused)
    seed_tenant(db, alpha)

    run = store.latest_run(db, alpha, "mailboxes")
    assert run["status"] == "succeeded"
    lines = [r["message"] for r in db.execute("SELECT message FROM run_events WHERE run_id = ?", (run["id"],))]
    assert any("Could not read whether the Microsoft 365 audit log is on" in m for m in lines)
    result = _check(db, alpha)
    assert result.status == "unavailable" and "didn't say" in result.summary
    assert onboarding.audit_log_on(db, alpha) is None


def test_not_collected_yet_is_unavailable(db, two_tenants):
    alpha, _ = two_tenants
    assert _check(db, alpha).status == "unavailable"
    assert onboarding.audit_log_on(db, alpha) is None


def test_the_tracker_shows_each_clients_audit_log(client, db, two_tenants, monkeypatch):
    alpha, bravo = two_tenants
    seed_tenant(db, alpha)
    monkeypatch.setitem(gd.EXCHANGE, "Get-AdminAuditLogConfig", [{"UnifiedAuditLogIngestionEnabled": False}])
    seed_tenant(db, bravo)
    third = store.create_tenant(db, "Charlie Co", "charlie.onmicrosoft.com", None)
    make_user(db, "admin")
    login(client, "admin")

    page = client.get("/admin/onboarding?show=all").get_data(as_text=True)
    assert "<th>Audit log</th>" in page
    rows = {name: page.split(name, 1)[1].split("</tr>", 1)[0] for name in ("Alpha Ltd", "Bravo Inc", "Charlie Co")}
    assert '<span class="pill ok">On</span>' in rows["Alpha Ltd"]
    assert '<span class="pill warn">Off</span>' in rows["Bravo Inc"] and "Microsoft Purview" in rows["Bravo Inc"]
    assert "Not known yet" in rows["Charlie Co"]
    assert onboarding.tracker(db, [store.get_tenant(db, third)])[0].audit_log is None
