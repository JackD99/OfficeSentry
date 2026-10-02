from datetime import UTC, datetime, timedelta

from officesentry import findings, store
from officesentry.changes import all_changes
from officesentry.reports.monthly_review import build
from tests import graphdata as gd
from tests.conftest import login, make_user
from tests.seed import seed_tenant
from tests.test_collectors import USERS


def _open(db, tenant_id):
    return {(r["rule_key"], r["object_id"]): r for r in db.execute(
        "SELECT * FROM findings WHERE tenant_id = ? AND resolved_at IS NULL", (tenant_id,))}


def test_worker_syncs_findings(db, two_tenants):
    alpha, _ = two_tenants
    seed_tenant(db, alpha)
    open_ = _open(db, alpha)
    assert ("admin_mfa", "u4") in open_
    assert ("licensed_disabled", "u2") in open_


def test_fixed_findings_resolve_and_can_reopen(db, two_tenants):
    alpha, _ = two_tenants
    seed_tenant(db, alpha)
    fixed = [dict(u, accountEnabled=True) if u["id"] == "u2" else u for u in USERS]
    seed_tenant(db, alpha, users=fixed)
    row = db.execute("SELECT * FROM findings WHERE rule_key = 'licensed_disabled'").fetchone()
    assert row["resolved_at"] is not None
    seed_tenant(db, alpha)
    row = db.execute("SELECT * FROM findings WHERE rule_key = 'licensed_disabled'").fetchone()
    assert row["resolved_at"] is None and row["reopened_at"] is not None
    assert db.execute("SELECT COUNT(*) FROM findings WHERE rule_key = 'licensed_disabled'").fetchone()[0] == 1


def test_unavailable_checks_do_not_resolve(db, two_tenants):
    alpha, _ = two_tenants
    seed_tenant(db, alpha)
    before = _open(db, alpha)
    # A failed MFA collection leaves the old data in place; deleting all runs
    # makes every check unavailable, which must not resolve anything.
    db.execute("DELETE FROM runs")
    findings.sync(db, store.get_tenant(db, alpha))
    assert _open(db, alpha).keys() == before.keys()


def test_accepted_risk_moves_out_of_report(db, two_tenants):
    alpha, _ = two_tenants
    seed_tenant(db, alpha)
    fid = db.execute("SELECT id FROM findings WHERE rule_key = 'guest_admins'").fetchone()[0]
    findings.accept(db, fid, "jack", "Partner account, reviewed quarterly", None)
    report = build(db, store.get_tenant(db, alpha))
    by_key = {r.key: r for r in report.results}
    assert by_key["guest_admins"].status == "accepted"
    assert [(r.key, f.subject) for r, f, _ in report.accepted] == [("guest_admins", "g#EXT#@x.com")]
    assert report.counts["high"] == 2  # was 3


def test_expired_acceptance_counts_again(db, two_tenants):
    alpha, _ = two_tenants
    seed_tenant(db, alpha)
    fid = db.execute("SELECT id FROM findings WHERE rule_key = 'guest_admins'").fetchone()[0]
    findings.accept(db, fid, "jack", "temp", "2020-01-01T00:00:00Z")
    assert build(db, store.get_tenant(db, alpha)).accepted == []


def test_changes_since_baseline(db, two_tenants):
    alpha, _ = two_tenants
    seed_tenant(db, alpha, ca=[gd.CA_MFA_ALL])
    old = (datetime.now(UTC) - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    db.execute("UPDATE runs SET finished_at = ?", (old,))
    users = USERS + [{"id": "u7", "displayName": "Newbie", "userPrincipalName": "new@x.com", "userType": "Member",
                      "accountEnabled": True, "assignedLicenses": []}]
    users = [dict(u, accountEnabled=False) if u["id"] == "u1" else u for u in users]
    seed_tenant(db, alpha, users=users, ca=[dict(gd.CA_MFA_ALL, state="enabledForReportingButNotEnforced"),
                                            gd.CA_BLOCK_LEGACY])
    since = (datetime.now(UTC) - timedelta(days=28)).strftime("%Y-%m-%dT%H:%M:%SZ")
    changes = {(c.area, c.kind, c.subject) for c in all_changes(db, alpha, since)}
    assert ("Accounts", "added", "new@x.com") in changes
    assert ("Accounts", "changed", "ann@x.com") in changes
    assert ("Conditional Access", "added", "Block legacy auth") in changes
    assert ("Conditional Access", "changed", "Require MFA for all users") in changes


def test_no_changes_without_baseline(db, two_tenants):
    alpha, _ = two_tenants
    seed_tenant(db, alpha)
    assert all_changes(db, alpha, "2026-01-01T00:00:00Z") == []


def test_findings_page_and_accept_flow(client, db, two_tenants):
    alpha, bravo = two_tenants
    seed_tenant(db, alpha)
    seed_tenant(db, bravo)
    make_user(db, "analyst", role="analyst", tenants=[alpha])
    login(client, "analyst")
    assert "Guest holding" in client.get(f"/tenants/{alpha}/findings").get_data(as_text=True)
    assert client.get(f"/tenants/{bravo}/findings").status_code == 404
    mine = db.execute("SELECT id FROM findings WHERE tenant_id = ? AND rule_key = 'guest_admins'",
                      (alpha,)).fetchone()[0]
    other = db.execute("SELECT id FROM findings WHERE tenant_id = ? AND rule_key = 'guest_admins'",
                       (bravo,)).fetchone()[0]
    assert client.post(f"/findings/{other}/accept", data={"note": "x"}).status_code == 404
    client.post(f"/findings/{mine}/accept", data={"note": "Known partner", "until": "2027-01-01"})
    row = db.execute("SELECT * FROM findings WHERE id = ?", (mine,)).fetchone()
    assert row["accepted_by"] == "analyst" and row["accepted_until"] == "2027-01-01T00:00:00Z"
    client.post(f"/findings/{mine}/unaccept")
    assert db.execute("SELECT accepted_at FROM findings WHERE id = ?", (mine,)).fetchone()[0] is None


def test_client_cannot_accept(client, db, two_tenants):
    alpha, _ = two_tenants
    seed_tenant(db, alpha)
    make_user(db, "client", role="client", tenants=[alpha])
    login(client, "client")
    fid = db.execute("SELECT id FROM findings LIMIT 1").fetchone()[0]
    assert client.get(f"/tenants/{alpha}/findings").status_code == 200
    assert client.post(f"/findings/{fid}/accept", data={"note": "x"}).status_code == 403


def test_report_marks_new_findings(db, two_tenants):
    alpha, _ = two_tenants
    seed_tenant(db, alpha)
    report = build(db, store.get_tenant(db, alpha))
    assert report.new_count == sum(report.counts.values())
    db.execute("UPDATE findings SET first_seen = '2020-01-01T00:00:00Z'")
    assert build(db, store.get_tenant(db, alpha)).new_count == 0
