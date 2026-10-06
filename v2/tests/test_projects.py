"""Recommended projects: worked out from a tenant's checks, statuses kept per tenant, pages and access."""

import json

import pytest

from officesentry import findings, projects, store
from officesentry.lifecycle import delete_tenant
from officesentry.rules import RULES
from officesentry.snapshot import TenantSnapshot
from tests.conftest import login, make_user
from tests.seed import seed_tenant


@pytest.fixture
def seeded(db, two_tenants):
    alpha, _ = two_tenants
    seed_tenant(db, alpha)
    return alpha


def _recs(db, tenant_id):
    return {r.project.key: r for r in projects.recommend(db, store.get_tenant(db, tenant_id))}


def test_catalog_maps_each_check_to_one_project():
    keys = {r.key for r in RULES}
    seen = [k for p in projects.CATALOG for k in p.checks]
    assert set(seen) <= keys, set(seen) - keys
    assert len(seen) == len(set(seen)), "a check sells two projects"
    assert len(projects.PROJECTS) == len(projects.CATALOG)
    for p in projects.CATALOG:
        assert p.checks or p.signals, p.key
        assert p.pitch and p.covers, p.key


def test_seeded_tenant_gets_projects_worst_first(db, seeded):
    recs = projects.recommend(db, store.get_tenant(db, seeded))
    keys = [r.project.key for r in recs]
    assert keys[0] == "admin_hardening" and recs[0].severity == "critical"
    assert {"identity", "email_security", "device_management", "windows_upgrade"} <= set(keys)
    ranks = [r.weight for r in recs]
    assert ranks == sorted(ranks)
    identity = recs[keys.index("identity")]
    assert {r.check for r in identity.reasons} >= {"legacy_auth_blocked", "users_mfa_registered"}
    assert identity.size.endswith("users") and identity.status == "suggested"
    # Defender for Office 365 isn't licensed in the seed.
    upgrade = recs[keys.index("licence_upgrade")]
    assert [r.title for r in upgrade.reasons] == ["No Defender for Office 365"]


def test_no_data_means_no_recommendations(db, two_tenants):
    assert projects.recommend(db, store.get_tenant(db, two_tenants[1])) == []


def test_no_conditional_access_calls_for_identity_project(db, seeded):
    """The example from the brief: Entra ID P1 and no Conditional Access switched on."""
    tenant = store.get_tenant(db, seeded)
    snap = TenantSnapshot(db, tenant)
    run = snap.runs["conditional_access"]["id"]
    db.execute("DELETE FROM objects WHERE run_id = ? AND kind = 'ca_policy'", (run,))
    db.execute("INSERT INTO metrics (tenant_id, run_id, key, value, captured_at) VALUES (?, ?, ?, 0, ?)",
               (seeded, run, "ca.policies_enabled", "2099-01-01T00:00:00Z"))
    identity = _recs(db, seeded)["identity"]
    assert identity.reasons[0].title == "No Conditional Access policies"
    assert {r.check for r in identity.reasons} >= {"mfa_enforced", "legacy_auth_blocked"}


def test_missing_licences_call_for_an_upgrade(db, seeded):
    caps = json.loads(store.get_tenant(db, seeded)["capabilities"])
    store.set_capabilities(db, seeded, {**caps, "entra_p1": False, "intune": False})
    upgrade = _recs(db, seeded)["licence_upgrade"]
    assert [r.title for r in upgrade.reasons] == ["No Entra ID P1", "No Intune", "No Defender for Office 365"]
    assert upgrade.severity == "high"
    # Unknown (never read) isn't missing.
    store.set_capabilities(db, seeded, {})
    assert "licence_upgrade" not in _recs(db, seeded)


def test_accepted_risks_dont_count(db, seeded):
    tenant = store.get_tenant(db, seeded)
    findings.sync(db, tenant)
    assert "windows_upgrade" in _recs(db, seeded)
    for row in db.execute("SELECT id FROM findings WHERE tenant_id = ? AND rule_key = 'supported_windows'",
                          (seeded,)).fetchall():
        findings.accept(db, row["id"], "ann", "Being replaced next month", None)
    assert "windows_upgrade" not in _recs(db, seeded)


def test_status_is_kept_and_cleared(db, seeded):
    projects.set_status(db, seeded, "identity", "proposed", "Sent to Jo on Monday", "ann")
    rec = _recs(db, seeded)["identity"]
    assert (rec.status, rec.note, rec.updated_by) == ("proposed", "Sent to Jo on Monday", "ann")
    projects.set_status(db, seeded, "identity", "suggested", "", "ann")
    assert db.execute("SELECT COUNT(*) FROM project_status").fetchone()[0] == 0
    with pytest.raises(ValueError):
        projects.set_status(db, seeded, "identity", "won", "", "ann")
    projects.set_status(db, seeded, "identity", "declined", "", "ann")
    delete_tenant(db, store.get_tenant(db, seeded), "ann")
    assert db.execute("SELECT COUNT(*) FROM project_status").fetchone()[0] == 0


def test_pipeline_lists_open_and_declined_separately(db, seeded, two_tenants):
    projects.set_status(db, seeded, "windows_upgrade", "declined", "", "ann")
    projects.set_status(db, seeded, "identity", "agreed", "", "ann")
    data = projects.pipeline(db, [store.get_tenant(db, t) for t in two_tenants])
    assert [r.note for r in data.rows] == ["", "No data collected yet."]
    open_keys = [rec.project.key for _t, rec in data.items()]
    assert "windows_upgrade" not in open_keys and "identity" in open_keys
    assert [rec.project.key for _t, rec in data.items(declined=True)] == ["windows_upgrade"]
    assert [rec.project.key for _t, rec in data.items("identity")] == ["identity"]
    assert data.count(status="agreed") == 1 and data.count(status="declined") == 1
    assert data.tenants_with == 1
    table = projects.to_table(data.items())
    assert table.columns[:3] == ["Tenant", "Tenant ID", "Project"] and len(table.rows) == len(open_keys)


def test_pages(client, db, seeded, two_tenants):
    make_user(db, "analyst", role="analyst", tenants=[seeded])
    login(client, "analyst")
    page = client.get("/projects").get_data(as_text=True)
    assert "Alpha Ltd" in page and "Identity and Conditional Access" in page and "Bravo Inc" not in page
    assert "Identity and Conditional Access" in client.get("/projects?project=identity").get_data(as_text=True)
    page = client.get(f"/tenants/{seeded}/projects").get_data(as_text=True)
    assert "Legacy authentication is allowed" in page
    assert f"/tenants/{seeded}/findings#check-legacy_auth_blocked" in page
    resp = client.post(f"/tenants/{seeded}/projects/identity", data={"status": "proposed", "note": "Quote sent"})
    assert resp.status_code == 302 and resp.headers["Location"].endswith("#project-identity")
    assert "Quote sent" in client.get(f"/tenants/{seeded}/projects").get_data(as_text=True)
    resp = client.post(f"/tenants/{seeded}/projects/windows_upgrade",
                       data={"status": "declined", "back": "all", "project": "windows_upgrade"})
    assert resp.headers["Location"].endswith("/projects?project=windows_upgrade")
    assert "Alpha Ltd" in client.get("/projects?view=declined").get_data(as_text=True)
    audit = db.execute("SELECT detail FROM audit_log WHERE action = 'project_status_changed'").fetchall()
    assert [r["detail"] for r in audit] == ["identity: proposed", "windows_upgrade: declined"]  # never the note
    assert client.post(f"/tenants/{seeded}/projects/identity", data={"status": "won"}).status_code == 400
    assert client.post(f"/tenants/{seeded}/projects/nope", data={"status": "agreed"}).status_code == 400
    # Another tenant's projects aren't there for this analyst.
    assert client.get(f"/tenants/{two_tenants[1]}/projects").status_code == 404
    assert client.post(f"/tenants/{two_tenants[1]}/projects/identity", data={"status": "agreed"}).status_code == 404
    csv = client.get("/projects.csv").get_data(as_text=True)
    assert csv.lstrip("\ufeff").startswith("Tenant,Tenant ID,Project") and "Quote sent" in csv
    assert client.get("/projects.xlsx").status_code == 200


def test_proposal_leaves_out_notes_and_declined(client, db, seeded):
    projects.set_status(db, seeded, "identity", "proposed", "Internal: they're price sensitive", "ann")
    projects.set_status(db, seeded, "windows_upgrade", "declined", "", "ann")
    make_user(db, "admin")
    login(client, "admin")
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("officesentry.reports.export.pdf_available", lambda: False)  # the print view is the same template
        page = client.get(f"/tenants/{seeded}/projects.pdf").get_data(as_text=True)
    assert "Identity and Conditional Access" in page and "Admin account hardening" in page
    assert "price sensitive" not in page and "Windows upgrade" not in page and "Proposed" not in page


def test_clients_cant_see_projects(client, db, seeded):
    make_user(db, "client", role="client", tenants=[seeded])
    login(client, "client")
    assert client.get("/projects").status_code == 403
    assert client.get(f"/tenants/{seeded}/projects").status_code == 403
    assert client.get(f"/tenants/{seeded}/projects.pdf").status_code == 403
    assert client.post(f"/tenants/{seeded}/projects/identity", data={"status": "agreed"}).status_code == 403
    assert "/projects" not in client.get(f"/tenants/{seeded}/findings").get_data(as_text=True)
