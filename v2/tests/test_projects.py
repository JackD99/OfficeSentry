"""Recommended projects: worked out from a tenant's checks, statuses kept per tenant, pages and access."""

import json

import pytest

from officesentry import findings, projects, store
from officesentry.lifecycle import delete_tenant
from officesentry.rules import RULES, Finding
from officesentry.snapshot import TenantSnapshot
from tests import graphdata as gd
from tests import hybriddata as hd
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
        assert all(isinstance(e, projects.Effort) for e in p.checks.values()), p.key
        # What it covers names only its own reasons, and whatever calls for it, the proposal has a scope.
        own = set(p.checks) | {f"signal:{s.__name__.lstrip('_')}" for s in p.signals}
        named = {k for c in p.covers for k in c.when}
        assert named <= own, (p.key, named - own)
        assert any(not c.when for c in p.covers) or set(p.checks) <= named, (p.key, set(p.checks) - named)
    # Repairs are quick fixes, never sold as a project.
    assert not {"directory_sync_running", "password_hash_sync"} & {k for p in projects.CATALOG for k in p.checks}
    assert not set(projects.MERGED) & set(projects.PROJECTS) and set(projects.MERGED.values()) <= set(projects.PROJECTS)


def test_effort():
    tenant, user, minor = Finding("high", "Tenant", ""), Finding("high", "ann", ""), Finding("low", "bob", "")
    assert projects.Effort(whole=4, each=0.25).hours([tenant]) == 4
    assert projects.Effort(whole=4, each=0.25).hours([user, user]) == 0.5
    assert projects.Effort(fixed=1, each=1.5, minor=0.25).hours([user, minor]) == 2.75
    assert projects.Effort(each=1, most=3).hours([user] * 10) == 3
    assert projects.Effort(fixed=0.5).hours([]) == 0.5 and projects.Effort(each=0.5).hours([]) == 0.5
    assert [projects.effort_label(h) for h in (0.5, 1, 4.4, 7.9, 8, 12, 20)] == [
        "Under an hour",
        "About 1 hour",
        "About 4 hours",
        "About 8 hours",
        "About 1 day",
        "About 1½ days",
        "About 2½ days",
    ]


def test_seeded_tenant_gets_projects_and_quick_fixes(db, seeded):
    found = projects.assess(db, store.get_tenant(db, seeded))
    recs = found.projects
    keys = [r.project.key for r in recs]
    assert keys == ["identity", "email_security"]
    assert all(r.hours >= projects.MIN_HOURS for r in recs)
    ranks = [r.weight for r in recs]
    assert ranks == sorted(ranks)
    identity = recs[0]
    assert {r.check for r in identity.reasons} >= {"legacy_auth_blocked", "users_mfa_registered", "guest_admins"}
    assert identity.size.endswith("users") and identity.status == "suggested" and identity.effort == "About 6 hours"
    # A critical fix that's small on its own (an admin without MFA) is done now, not held for the project.
    quick = {q.key: q for q in found.quick_fixes}
    assert "admin_mfa" not in {r.check for r in identity.reasons}
    assert quick["admin_mfa"].severity == "critical" and quick["admin_mfa"].project == "identity"
    assert found.quick_fixes[0].key == "admin_mfa"
    # Too little device work for a project: each failing device check is a quick fix, with its action.
    assert {"device_encryption", "device_compliance", "supported_windows"} <= set(quick)
    assert quick["device_encryption"].action and quick["device_encryption"].hours < projects.MIN_HOURS
    # Defender for Office 365 isn't licensed in the seed: on its own an add-on, not a licence upgrade project.
    assert quick["signal:missing_defender_o365_p1"].title == "No Defender for Office 365"
    # A failing check no project covers still has a home.
    assert "domain_federation_changed" in quick
    assert not {r.check for rec in recs for r in rec.reasons} & set(quick), "a finding in two places"


def test_one_small_fix_is_never_a_project(db, seeded, monkeypatch):
    """The example from Jack: MFA for one admin account is a quick fix, not an identity project."""
    real = projects.evaluate

    def only(*keys):
        monkeypatch.setattr(
            projects, "evaluate", lambda snap, now: [r for r in real(snap, now) if r.status != "fail" or r.key in keys]
        )
        return projects.assess(db, store.get_tenant(db, seeded))

    found = only("admin_mfa")
    assert found.projects == [] and [q.key for q in found.quick_fixes] == [
        "admin_mfa",
        "signal:missing_defender_o365_p1",
    ]
    # Enough identity work adds up to a project; the urgent admin fix stays a quick fix.
    found = only("admin_mfa", "legacy_auth_blocked", "admin_phishing_resistant", "users_mfa_registered")
    assert [r.project.key for r in found.projects] == ["identity"]
    assert "admin_mfa" in [q.key for q in found.quick_fixes]


def test_proposal_covers_only_what_was_found(db, seeded):
    """A proposal doesn't offer work the client doesn't need: nothing risky, no mailbox auditing to turn on."""
    recs = _recs(db, seeded)
    identity, email = recs["identity"].covers, recs["email_security"].covers
    assert identity[0].startswith("Conditional Access policies") and "Report-only rollout first" in identity[-1]
    assert "Risky sign-in and risky user handling" not in identity
    assert "SMTP AUTH turned off, once scanners and apps send mail another way" in email
    assert "Mailbox auditing on" not in email and "Sign-in blocked for shared mailboxes" not in email


def test_stopped_sync_is_a_repair_and_small_clients_can_go_cloud_only(db, two_tenants, monkeypatch):
    """Jack's example: nobody sells "Directory sync health". Sync that has stopped is a quick fix; the project
    is moving a small business's accounts off its office server."""
    monkeypatch.setattr(gd, "ORGANIZATION", {**hd.ORGANIZATION, "onPremisesLastSyncDateTime": hd.hours_ago(19)})
    monkeypatch.setattr(hd, "DOMAINS", hd.HYBRID_DOMAINS)
    alpha, _ = two_tenants
    seed_tenant(db, alpha, users=hd.USERS)
    found = projects.assess(db, store.get_tenant(db, alpha))
    quick = {q.key: q for q in found.quick_fixes}
    assert quick["directory_sync_running"].headline == "Not syncing: last sync 19 hours ago"
    assert quick["directory_sync_running"].project == ""
    cloud = {r.project.key: r for r in found.projects}["cloud_only"]
    assert (cloud.project.title, cloud.severity, cloud.effort) == (
        "Move to cloud-only accounts",
        "low",
        "About 6 hours",
    )
    assert [r.headline for r in cloud.reasons] == [
        "3 accounts synced from an office server",
        "1 computer joined to the office domain",
    ]
    assert "Computers moved from the office domain to Entra ID, keeping files and settings" in cloud.covers
    # A bigger organisation's AD usually does more than sync accounts: no project, and still the quick fix.
    monkeypatch.setattr(projects, "CLOUD_ONLY_MAX_USERS", 2)
    found = projects.assess(db, store.get_tenant(db, alpha))
    assert "cloud_only" not in {r.project.key for r in found.projects}
    assert "directory_sync_running" in {q.key for q in found.quick_fixes}


def test_no_data_means_no_recommendations(db, two_tenants):
    assert projects.recommend(db, store.get_tenant(db, two_tenants[1])) == []


def test_no_conditional_access_calls_for_identity_project(db, seeded):
    """The example from the brief: Entra ID P1 and no Conditional Access switched on."""
    tenant = store.get_tenant(db, seeded)
    snap = TenantSnapshot(db, tenant)
    run = snap.runs["conditional_access"]["id"]
    db.execute("DELETE FROM objects WHERE run_id = ? AND kind = 'ca_policy'", (run,))
    db.execute(
        "INSERT INTO metrics (tenant_id, run_id, key, value, captured_at) VALUES (?, ?, ?, 0, ?)",
        (seeded, run, "ca.policies_enabled", "2099-01-01T00:00:00Z"),
    )
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
    assert "identity" in _recs(db, seeded)
    for row in db.execute(
        "SELECT id FROM findings WHERE tenant_id = ? AND rule_key IN ('legacy_auth_blocked', 'supported_windows')",
        (seeded,),
    ).fetchall():
        findings.accept(db, row["id"], "ann", "Being replaced next month", None)
    found = projects.assess(db, tenant)
    # Without legacy authentication to block, what's left of identity is under half a day: quick fixes.
    assert "identity" not in {r.project.key for r in found.projects}
    quick = {q.key for q in found.quick_fixes}
    assert "users_mfa_registered" in quick
    assert not quick & {"legacy_auth_blocked", "supported_windows"}


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
    projects.set_status(db, seeded, "email_security", "declined", "", "ann")
    projects.set_status(db, seeded, "identity", "agreed", "", "ann")
    data = projects.pipeline(db, [store.get_tenant(db, t) for t in two_tenants])
    assert [r.note for r in data.rows] == ["", "No data collected yet."]
    open_keys = [rec.project.key for _t, rec in data.items()]
    assert "email_security" not in open_keys and "identity" in open_keys
    assert [rec.project.key for _t, rec in data.items(declined=True)] == ["email_security"]
    assert data.quick_fixes == len(data.rows[0].quick_fixes) > 0
    assert [rec.project.key for _t, rec in data.items("identity")] == ["identity"]
    assert data.count(status="agreed") == 1 and data.count(status="declined") == 1
    assert data.tenants_with == 1
    table = projects.to_table(data.items())
    assert table.columns[:3] == ["Client", "Tenant ID", "Project"] and len(table.rows) == len(open_keys)
    assert table.rows[0][table.columns.index("Estimated hours")] == 6


def test_pages(client, db, seeded, two_tenants):
    make_user(db, "analyst", role="analyst", tenants=[seeded])
    login(client, "analyst")
    page = client.get("/projects").get_data(as_text=True)
    assert "Alpha Ltd" in page and "Identity and Conditional Access" in page and "Bravo Inc" not in page
    assert "Identity and Conditional Access" in client.get("/projects?project=identity").get_data(as_text=True)
    page = client.get(f"/tenants/{seeded}/projects").get_data(as_text=True)
    assert "Legacy authentication is allowed" in page and "About 6 hours" in page
    assert "Goal: Legacy authentication is blocked" in page  # the check's goal, not a second finding
    assert f"/tenants/{seeded}/findings#check-legacy_auth_blocked" in page
    quick = page[page.index('id="quick-fixes"') :]
    assert "Every admin has MFA registered" in quick and "Have each listed admin register a strong method" in quick
    assert f"/tenants/{seeded}/findings#check-admin_mfa" in quick
    resp = client.post(f"/tenants/{seeded}/projects/identity", data={"status": "proposed", "note": "Quote sent"})
    assert resp.status_code == 302 and resp.headers["Location"].endswith("#project-identity")
    assert "Quote sent" in client.get(f"/tenants/{seeded}/projects").get_data(as_text=True)
    resp = client.post(
        f"/tenants/{seeded}/projects/email_security",
        data={"status": "declined", "back": "all", "project": "email_security"},
    )
    assert resp.headers["Location"].endswith("/projects?project=email_security")
    assert "Alpha Ltd" in client.get("/projects?view=declined").get_data(as_text=True)
    audit = db.execute("SELECT detail FROM audit_log WHERE action = 'project_status_changed'").fetchall()
    assert [r["detail"] for r in audit] == ["identity: proposed", "email_security: declined"]  # never the note
    assert client.post(f"/tenants/{seeded}/projects/identity", data={"status": "won"}).status_code == 400
    assert client.post(f"/tenants/{seeded}/projects/nope", data={"status": "agreed"}).status_code == 400
    # Another tenant's projects aren't there for this analyst.
    assert client.get(f"/tenants/{two_tenants[1]}/projects").status_code == 404
    assert client.post(f"/tenants/{two_tenants[1]}/projects/identity", data={"status": "agreed"}).status_code == 404
    csv = client.get("/projects.csv").get_data(as_text=True)
    assert csv.lstrip("\ufeff").startswith("Client,Tenant ID,Project") and "Quote sent" in csv
    assert client.get("/projects.xlsx").status_code == 200


def test_proposal_leaves_out_notes_and_declined(client, db, seeded):
    projects.set_status(db, seeded, "identity", "proposed", "Internal: they're price sensitive", "ann")
    projects.set_status(db, seeded, "email_security", "declined", "", "ann")
    make_user(db, "admin")
    login(client, "admin")
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("officesentry.reports.export.pdf_available", lambda: False)  # the print view is the same template
        page = client.get(f"/tenants/{seeded}/projects.pdf").get_data(as_text=True)
    assert "Identity and Conditional Access" in page and "Admin accounts" in page
    assert "price sensitive" not in page and "Email security" not in page and "Proposed" not in page
    assert "hours" not in page  # estimates are for staff, not the client


def test_clients_cant_see_projects(client, db, seeded):
    make_user(db, "client", role="client", tenants=[seeded])
    login(client, "client")
    assert client.get("/projects").status_code == 403
    assert client.get(f"/tenants/{seeded}/projects").status_code == 403
    assert client.get(f"/tenants/{seeded}/projects.pdf").status_code == 403
    assert client.post(f"/tenants/{seeded}/projects/identity", data={"status": "agreed"}).status_code == 403
    assert "/projects" not in client.get(f"/tenants/{seeded}/findings").get_data(as_text=True)


def test_merged_projects_keep_their_statuses(db, two_tenants):
    """migrations/0028: Admin account hardening and Teams and groups governance were folded into bigger projects."""
    from officesentry.db import _split_statements, migration_files

    alpha, bravo = two_tenants
    rows = [
        (alpha, "admin_hardening", "agreed", "Keys ordered"),
        (alpha, "identity", "proposed", "Quote sent"),
        (bravo, "teams_governance", "declined", ""),
        (bravo, "storage", "proposed", ""),
    ]
    db.executemany(
        "INSERT INTO project_status (tenant_id, project_key, status, note, updated_by, updated_at)"
        " VALUES (?, ?, ?, ?, 'ann', '2026-10-01T00:00:00Z')",
        rows,
    )
    path = dict(migration_files())[28]
    for stmt in _split_statements(path.read_text(encoding="utf-8")):
        db.execute(stmt)
    got = {
        (r["tenant_id"], r["project_key"]): (r["status"], r["note"]) for r in db.execute("SELECT * FROM project_status")
    }
    assert got == {
        (alpha, "identity"): ("proposed", "Quote sent Keys ordered"),
        (bravo, "external_sharing"): ("declined", ""),
        (bravo, "storage"): ("proposed", ""),
    }
