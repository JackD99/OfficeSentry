"""Client tenant admin: connection health, onboarding checklist, pause/archive and offboarding."""

import json
import shutil
import sqlite3
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
import responses

from officesentry import delivery, issued, lifecycle, store, system_health, tasks, tenant_health
from officesentry.graph import GraphClient
from officesentry.worker import execute_run, schedule_tick
from tests import graphdata as gd
from tests.conftest import login, make_user
from tests.seed import exchange_factory, mock_tenant, seed_tenant


def _graph(tenant, connection):
    return GraphClient(lambda: "t", sleep=lambda s: None)


def _check(db, tenant_id, **our_app):
    """Run only the connection collector against the seeded responses."""
    with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        mock_tenant(rsps, our_app=our_app)
        store.enqueue_run(db, tenant_id, "connection")
        status = execute_run(db, store.claim_next_run(db), _graph, exchange_factory)
        calls = [c.request for c in rsps.calls]
    return status, tenant_health.connection_result(db, tenant_id), calls


# -- The connection collector ------------------------------------------------------------------


def test_connection_check_all_present(db, two_tenants):
    alpha, _ = two_tenants
    status, result, calls = _check(db, alpha)
    assert status == "succeeded"
    assert result["app_found"] and result["global_reader"] and result["roles"] == ["Global Reader"]
    assert result["missing"] == [] and "Exchange.ManageAsApp" in result["granted"]
    assert all(r.method == "GET" for r in calls)  # never writes to the tenant
    assert any("appId+eq+%2711111111-2222-3333-4444-555555555555%27" in r.url for r in calls)


def test_connection_check_missing_permissions_and_no_global_reader(db, two_tenants):
    alpha, _ = two_tenants
    status, result, _ = _check(db, alpha, missing=("Sites.Read.All", "Exchange.ManageAsApp"), roles=[])
    assert status == "partial"
    assert result["missing"] == ["Sites.Read.All", "Exchange.ManageAsApp"]
    assert result["global_reader"] is False
    run = store.latest_run(db, alpha, "connection")
    assert "2 permissions not granted" in run["error"] and "Global Reader" in run["error"]

    health = tenant_health.assess(db, store.get_tenant(db, alpha))
    perms, reader = health.check("permissions"), health.check("global_reader")
    assert perms.state == "bad" and "Sites.Read.All" in perms.summary and perms.action == "consent"
    assert reader.state == "bad" and "Roles and admins → Global Reader → Add assignments" in reader.fix
    assert health.status == "onboarding"  # no full collection yet, so these are checklist steps
    assert [s.key for s in health.steps if not s.done] == ["global_reader", "collection", "permissions"]


def test_connection_check_flags_global_administrator(db, two_tenants):
    alpha, _ = two_tenants
    roles = gd.OUR_ROLE_ASSIGNMENTS + [
        {
            "id": "a-ga",
            "roleDefinitionId": "62e90394-69f5-4237-9190-012177145e10",
            "principalId": gd.OUR_SP["id"],
            "directoryScopeId": "/",
        }
    ]
    _, result, _ = _check(db, alpha, roles=roles)
    assert result["global_reader"] and result["too_powerful"] == ["Global Administrator"]
    check = tenant_health.assess(db, store.get_tenant(db, alpha)).check("global_reader")
    assert check.state == "warn" and "Remove assignments" in check.fix


def test_extra_permissions_and_roles_are_warnings(client, db, two_tenants, monkeypatch):
    """The read-only promise per tenant: anything granted beyond what Office Sentry asks for is shown."""
    alpha, _ = two_tenants
    granted = gd.our_assignments
    extra = [
        {"id": "our-mail", "resourceId": "sp-graph", "appRoleId": "r-mailread", "principalId": gd.OUR_SP["id"]},
        {"id": "our-exo-full", "resourceId": "sp-exchange", "appRoleId": "r-exo-full", "principalId": gd.OUR_SP["id"]},
    ]
    monkeypatch.setattr(gd, "our_assignments", lambda missing=(): granted(missing) + extra)
    roles = gd.OUR_ROLE_ASSIGNMENTS + [
        {"id": "a-dr", "roleDefinitionId": gd.READER, "principalId": gd.OUR_SP["id"], "directoryScopeId": "/"}
    ]
    _, result, _ = _check(db, alpha, roles=roles)
    assert result["other"] == ["Mail.Read", "full_access_as_app"] and result["missing"] == []

    health = tenant_health.assess(db, store.get_tenant(db, alpha))
    perms = health.check("extra_permissions")
    assert (
        perms.state == "warn"
        and "2 permissions Office Sentry doesn't ask for: Mail.Read, full_access_as_app" in perms.summary
        and "Enterprise applications" in perms.fix
    )
    reader = health.check("global_reader")  # any role besides Global Reader, not only the famous two
    assert reader.state == "warn" and "also holds Directory Readers" in reader.summary
    assert health.check("permissions").state == "ok"

    make_user(db, "admin")
    login(client, "admin")
    page = client.get("/admin/tenants").get_data(as_text=True)
    assert "2 extra" in page and "Not asked for: Mail.Read, full_access_as_app" in page
    assert "+ 1 other role" in page and "Also holds: Directory Readers" in page
    page = client.get(f"/tenants/{alpha}/manage").get_data(as_text=True)
    assert "Extra permissions" in page and "also holds Directory Readers" in page


def test_connection_check_app_not_in_tenant(db, two_tenants):
    alpha, _ = two_tenants
    status, result, _ = _check(db, alpha, found=False)
    assert status == "partial" and result["app_found"] is False
    assert tenant_health.assess(db, store.get_tenant(db, alpha)).check("permissions").state == "bad"


def test_connection_check_without_an_app_connection(db):
    t = store.create_tenant(db, "Loose", "loose.onmicrosoft.com", None)
    status, result, calls = _check(db, t)
    assert status == "partial" and result["client_id"] is None and not calls


def test_sign_in_failure_is_explained(db, two_tenants):
    from officesentry.graph import GraphError

    alpha, _ = two_tenants

    def broken(tenant, connection):
        def token():
            raise GraphError(401, "invalid_client", "AADSTS700027: Client assertion failed. Trace ID: x")

        return GraphClient(token)

    store.enqueue_run(db, alpha, "connection")
    assert execute_run(db, store.claim_next_run(db), broken) == "failed"
    health = tenant_health.assess(db, store.get_tenant(db, alpha))
    sign_in = health.check("sign_in")
    assert sign_in.state == "bad" and "certificate" in sign_in.summary and "Certificates" in sign_in.fix
    assert health.status == "attention"


# -- Onboarding checklist ------------------------------------------------------------------------


def test_checklist_ticks_itself_off(client, db, connection_id):
    make_user(db, "admin")
    login(client, "admin")
    t = store.create_tenant(db, "Contoso", "contoso.onmicrosoft.com", connection_id)
    health = tenant_health.assess(db, store.get_tenant(db, t))
    assert [s.done for s in health.steps] == [True, False, False, False, False]
    assert health.status == "onboarding" and health.check("consent").state == "todo"
    page = client.get(f"/tenants/{t}").get_data(as_text=True)
    assert "Set up Contoso" in page and "Grant admin consent" in page and "1 of 5 done" in page

    seed_tenant(db, t)  # consent outside the link: data arrives, so consent is recorded
    health = tenant_health.assess(db, store.get_tenant(db, t))
    assert all(s.done for s in health.steps) and health.status == "healthy", health.problems
    assert "Set up Contoso" not in client.get(f"/tenants/{t}").get_data(as_text=True)


def test_first_data_queues_the_first_collection(db, connection_id):
    t = store.create_tenant(db, "Contoso", "contoso.onmicrosoft.com", connection_id)
    with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        mock_tenant(rsps)
        store.enqueue_run(db, t, "connection")  # "Check connection now" after the client consented themselves
        assert execute_run(db, store.claim_next_run(db), _graph) == "succeeded"
    queued = [
        r["collector"]
        for r in db.execute("SELECT collector FROM runs WHERE tenant_id = ? AND status = 'queued' ORDER BY id", (t,))
    ]
    assert "users" in queued and "connection" not in queued
    assert store.get_tenant(db, t)["consented_at"]


def test_check_connection_now_queues_the_collector(client, db, two_tenants):
    alpha, _ = two_tenants
    make_user(db, "lead", role="analyst", all_tenants=True)
    login(client, "lead")
    page = client.get(f"/tenants/{alpha}/manage").get_data(as_text=True)
    assert "Connection health" in page and "Check connection now" in page
    assert 'name="tenant_ref"' not in page  # settings are admin-only
    client.post(f"/tenants/{alpha}/check-connection")
    assert [r["collector"] for r in db.execute("SELECT collector FROM runs")] == ["connection"]


def test_client_list_shows_what_needs_fixing(client, db, two_tenants):
    alpha, bravo = two_tenants
    seed_tenant(db, alpha)
    _check(db, bravo, missing=("Sites.Read.All",), roles=[])
    make_user(db, "admin")
    login(client, "admin")
    page = client.get("/admin/tenants").get_data(as_text=True)
    assert "Alpha Ltd" in page and "Bravo Inc" in page
    assert "1 missing" in page and "Missing" in page and "Healthy" in page
    assert page.index("Bravo Inc") < page.index("Alpha Ltd")  # worst first


# -- Pause and archive ------------------------------------------------------------------------------


def test_paused_and_archived_tenants_are_not_collected(db, cfg, two_tenants):
    alpha, bravo = two_tenants
    store.enqueue_run(db, alpha, "users")
    lifecycle.pause(db, store.get_tenant(db, alpha), "admin")
    assert db.execute("SELECT COUNT(*) FROM runs WHERE tenant_id = ?", (alpha,)).fetchone()[0] == 0
    lifecycle.archive(db, store.get_tenant(db, bravo), "admin")
    now = datetime(2026, 10, 2, 3, 0, tzinfo=UTC)
    assert schedule_tick(db, cfg, now) == 0

    # A run queued in the same moment as the pause is never claimed.
    db.execute(
        "INSERT INTO runs (tenant_id, collector, status, trigger, created_at)"
        " VALUES (?, 'users', 'queued', 'manual', '2026-10-01T00:00:00Z')",
        (alpha,),
    )
    assert store.claim_next_run(db) is None

    lifecycle.resume(db, store.get_tenant(db, alpha), "admin")
    assert store.claim_next_run(db)["tenant_id"] == alpha
    with pytest.raises(ValueError):
        lifecycle.resume(db, store.get_tenant(db, bravo), "admin")  # restore first
    lifecycle.restore(db, store.get_tenant(db, bravo), "admin")
    t = store.get_tenant(db, bravo)
    assert t["archived_at"] is None and not t["enabled"]  # restored, still paused


def test_paused_tenant_sends_no_scheduled_email(app, db, two_tenants, vault, cfg):
    alpha, _ = two_tenants
    sid = db.execute(
        "INSERT INTO email_schedules (kind, tenant_id, reports, frequency, day_of_month, hour,"
        " timezone, created_at) VALUES ('report', ?, '[\"review\"]', 'monthly', 1, 0, 'UTC',"
        " '2026-01-01T00:00:00Z')",
        (alpha,),
    ).lastrowid
    delivery.set_recipients(db, "schedule", sid, [delivery.add_contact(db, alpha, "", "a@x.com")])
    lifecycle.pause(db, store.get_tenant(db, alpha), "admin")
    assert delivery.tick(app, db, vault, cfg, datetime(2026, 10, 2, 9, 0, tzinfo=UTC)) == 0
    assert db.execute("SELECT COUNT(*) FROM email_deliveries").fetchone()[0] == 0
    assert delivery.digest_data(db, "weekly", datetime(2026, 10, 2, tzinfo=UTC))["tenants"] == 1


def test_archived_tenant_is_hidden(client, db, two_tenants):
    alpha, bravo = two_tenants
    make_user(db, "admin")
    make_user(db, "lead", role="analyst", all_tenants=True)
    make_user(db, "client", role="client", tenants=[alpha])
    lifecycle.archive(db, store.get_tenant(db, alpha), "admin")
    for who in ("lead", "client"):
        login(client, who)
        assert "Alpha Ltd" not in client.get("/").get_data(as_text=True)
        assert client.get(f"/tenants/{alpha}/report").status_code == 404
        client.post("/logout")
    login(client, "lead")
    assert "Alpha Ltd" not in client.get("/across/review").get_data(as_text=True)
    client.post("/logout")
    login(client, "admin")
    assert "Alpha Ltd" not in client.get("/").get_data(as_text=True)
    assert "Alpha Ltd" in client.get("/admin/tenants?show=archived").get_data(as_text=True)
    assert client.get(f"/tenants/{alpha}/manage").status_code == 200  # admins can still restore or delete
    client.post(f"/tenants/{alpha}/restore")
    assert store.get_tenant(db, alpha)["archived_at"] is None


def test_admin_pauses_and_resumes_from_the_portal(client, db, two_tenants):
    alpha, _ = two_tenants
    make_user(db, "admin")
    login(client, "admin")
    client.post(f"/tenants/{alpha}/pause")
    assert not store.get_tenant(db, alpha)["enabled"]
    assert "Paused" in client.get(f"/tenants/{alpha}/findings").get_data(as_text=True)
    assert client.post(f"/tenants/{alpha}/collect").status_code == 302
    assert db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0  # refused while paused
    client.post(f"/tenants/{alpha}/resume")
    assert store.get_tenant(db, alpha)["enabled"]
    actions = [r["action"] for r in db.execute("SELECT action FROM audit_log ORDER BY id")]
    assert "tenant_paused" in actions and "tenant_resumed" in actions


# -- Access control ---------------------------------------------------------------------------------


ADMIN_ONLY = ["edit", "pause", "resume", "archive", "restore", "delete"]


@pytest.mark.parametrize("role", ["analyst", "client"])
def test_only_admins_change_tenants(client, db, two_tenants, role):
    alpha, _ = two_tenants
    make_user(db, "someone", role=role, all_tenants=role == "analyst", tenants=[alpha])
    login(client, "someone")
    for action in ADMIN_ONLY:
        resp = client.post(
            f"/tenants/{alpha}/{action}",
            data={"name": "Hacked", "tenant_ref": "x.example.com", "confirm_name": "Alpha Ltd"},
        )
        assert resp.status_code == 403, action
    assert client.get(f"/tenants/{alpha}/offboard").status_code == 403
    t = store.get_tenant(db, alpha)
    assert t["name"] == "Alpha Ltd" and t["enabled"]
    if role == "client":
        assert client.get(f"/tenants/{alpha}/manage").status_code == 403
        assert client.get("/admin/tenants").status_code == 403
        assert client.post(f"/tenants/{alpha}/check-connection").status_code == 403


def test_analyst_only_sees_granted_tenants_in_the_client_list(client, db, two_tenants):
    alpha, bravo = two_tenants
    make_user(db, "analyst", role="analyst", tenants=[alpha])
    login(client, "analyst")
    page = client.get("/admin/tenants").get_data(as_text=True)
    assert "Alpha Ltd" in page and "Bravo Inc" not in page
    assert client.get(f"/tenants/{bravo}/manage").status_code == 404
    assert client.post(f"/tenants/{bravo}/check-connection").status_code == 404


# -- Edit ----------------------------------------------------------------------------------------------


def test_edit_tenant(client, db, two_tenants, connection_id):
    alpha, bravo = two_tenants
    make_user(db, "admin")
    login(client, "admin")
    form = {
        "name": "Alpha Group",
        "tenant_ref": "alpha.onmicrosoft.com",
        "connection_id": connection_id,
        "notes": "Contract renews in May",
    }
    client.post(f"/tenants/{alpha}/edit", data=form)
    t = store.get_tenant(db, alpha)
    assert t["name"] == "Alpha Group" and t["notes"] == "Contract renews in May" and t["consented_at"]

    # Before any data: the ID can change, and consent must be granted again.
    client.post(f"/tenants/{alpha}/edit", data=form | {"tenant_ref": "alpha-group.onmicrosoft.com"})
    t = store.get_tenant(db, alpha)
    assert t["tenant_ref"] == "alpha-group.onmicrosoft.com" and t["consented_at"] is None
    page = client.post(
        f"/tenants/{alpha}/edit", data=form | {"tenant_ref": "bravo.onmicrosoft.com"}, follow_redirects=True
    ).get_data(as_text=True)
    assert "Another client already uses" in page

    # Once data has come in, only a domain -> GUID change is allowed.
    seed_tenant(db, bravo)
    page = client.post(
        f"/tenants/{bravo}/edit",
        data=form | {"name": "Bravo", "tenant_ref": "other.example.com"},
        follow_redirects=True,
    ).get_data(as_text=True)
    assert "can&#39;t change" in page and store.get_tenant(db, bravo)["tenant_ref"] == "bravo.onmicrosoft.com"
    guid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    with responses.RequestsMock() as rsps:
        rsps.get(
            "https://login.microsoftonline.com/bravo.onmicrosoft.com/v2.0/.well-known/openid-configuration",
            json={"issuer": f"https://login.microsoftonline.com/{guid}/v2.0"},
        )
        # ...and only to the GUID Microsoft publishes for that domain, not another client's.
        page = client.post(
            f"/tenants/{bravo}/edit",
            data=form | {"name": "Bravo", "tenant_ref": "99999999-8888-7777-6666-555555555555"},
            follow_redirects=True,
        ).get_data(as_text=True)
        assert "isn&#39;t the tenant ID" in page
        assert store.get_tenant(db, bravo)["tenant_ref"] == "bravo.onmicrosoft.com"
        client.post(f"/tenants/{bravo}/edit", data=form | {"name": "Bravo", "tenant_ref": guid})
    assert store.get_tenant(db, bravo)["tenant_ref"] == guid and store.get_tenant(db, bravo)["consented_at"]


# -- Offboarding -----------------------------------------------------------------------------------------


def _tenant_rows(db, tenant_id) -> dict:
    return {
        t: db.execute(f"SELECT COUNT(*) FROM {t} WHERE tenant_id = ?", (tenant_id,)).fetchone()[0]
        for t in lifecycle._tenant_tables(db)
        if t not in lifecycle.KEPT
    }


def _populate(db, tenant_id):
    seed_tenant(db, tenant_id)
    contact = delivery.add_contact(db, tenant_id, "IT", f"it{tenant_id}@client.com")
    sid = db.execute(
        "INSERT INTO email_schedules (kind, tenant_id, reports, frequency, hour, created_at)"
        " VALUES ('report', ?, '[\"review\"]', 'monthly', 8, '2026-01-01T00:00:00Z')",
        (tenant_id,),
    ).lastrowid
    delivery.set_recipients(db, "schedule", sid, [contact])
    aid = db.execute(
        "INSERT INTO email_alerts (tenant_id, min_severity, created_at) VALUES (?, 'high', '2026-01-01T00:00:00Z')",
        (tenant_id,),
    ).lastrowid
    delivery.set_recipients(db, "alert", aid, [contact])
    db.execute(
        "INSERT INTO email_deliveries (kind, schedule_id, tenant_id, subject, recipients, status, created_at)"
        " VALUES ('report', ?, ?, 'x', 'it@client.com', 'sent', '2026-01-01T00:00:00Z')",
        (sid, tenant_id),
    )


def test_offboard_deletes_everything_tenant_scoped_and_nothing_else(client, db, two_tenants):
    alpha, bravo = two_tenants
    _populate(db, alpha)
    _populate(db, bravo)
    team = delivery.add_contact(db, None, "Me", "me@msp.com")
    admin = make_user(db, "admin")
    only_alpha = make_user(db, "alpha-it", role="client", tenants=[alpha])
    both = make_user(db, "group-it", role="client", tenants=[alpha, bravo])
    analyst = make_user(db, "lead", role="analyst", tenants=[alpha, bravo])
    db.execute(
        "INSERT INTO saved_views (user_id, report_key, name, query, created_at)"
        " VALUES (?, 'mailboxes', 'Mine', 'x=1', '2026-01-01T00:00:00Z')",
        (analyst,),
    )
    alpha_runs = [r["id"] for r in db.execute("SELECT id FROM runs WHERE tenant_id = ?", (alpha,))]
    before_bravo = _tenant_rows(db, bravo)
    bravo_objects = lifecycle.stored(db, bravo)["objects"]
    assert all(_tenant_rows(db, alpha)[t] for t in ("runs", "metrics", "findings", "contacts", "email_schedules"))

    login(client, "admin")
    page = client.get(f"/tenants/{alpha}/offboard").get_data(as_text=True)
    assert "alpha-it" in page and "group-it (Client viewer)" in page and "lead (Second line)" in page
    assert "Delete Alpha Ltd permanently" in page and "Enterprise applications" in page
    assert "Monthly review PDF" in page

    # A wrong name deletes nothing.
    client.post(f"/tenants/{alpha}/delete", data={"confirm_name": "alpha"})
    assert store.get_tenant(db, alpha) is not None
    # Someone who isn't a client-only account can't be deleted along with it.
    client.post(f"/tenants/{alpha}/delete", data={"confirm_name": "Alpha Ltd", "delete_users": [str(both)]})
    assert store.get_tenant(db, alpha) is not None

    resp = client.post(
        f"/tenants/{alpha}/delete", data={"confirm_name": "Alpha Ltd", "delete_users": [str(only_alpha)]}
    )
    assert resp.status_code == 302
    assert store.get_tenant(db, alpha) is None
    assert not any(_tenant_rows(db, alpha).values()) and not lifecycle.leftovers(db, alpha, alpha_runs)
    assert db.execute("SELECT COUNT(*) FROM users WHERE id = ?", (only_alpha,)).fetchone()[0] == 0

    # Nothing else is touched.
    assert _tenant_rows(db, bravo) == before_bravo and lifecycle.stored(db, bravo)["objects"] == bravo_objects
    users = {r["id"] for r in db.execute("SELECT id FROM users")}
    assert {admin, both, analyst} <= users
    assert db.execute("SELECT tenant_id FROM user_tenants WHERE user_id = ?", (both,)).fetchall()[0][0] == bravo
    assert db.execute("SELECT COUNT(*) FROM saved_views").fetchone()[0] == 1
    assert db.execute("SELECT COUNT(*) FROM contacts WHERE id = ?", (team,)).fetchone()[0] == 1
    assert client.get("/").status_code == 200  # the admin's own session is unaffected
    entry = db.execute("SELECT * FROM audit_log WHERE action = 'tenant_deleted'").fetchone()
    assert entry["tenant_id"] == alpha and "Alpha Ltd" in entry["detail"] and "alpha-it" in entry["detail"]


def test_offboard_names_the_report_history_it_deletes(client, db, two_tenants):
    """The files a client was sent go with it, so the page counts them and links to where they download, and
    big counts carry thousands separators."""
    alpha, _ = two_tenants
    make_user(db, "admin")
    login(client, "admin")
    sent = issued.IssuedFile("review", "Monthly review", "security-review-alpha.pdf", "application/pdf", b"%PDF-1.7")
    issued.archive(db, alpha, [sent], period="2026-09", trigger="schedule", user="admin", to=["it@alpha.example"])
    db.executemany(
        "INSERT INTO metrics (tenant_id, run_id, key, value, captured_at) VALUES (?, NULL, ?, 1, ?)",
        [(alpha, f"m{n}", "2026-10-01T00:00:00Z") for n in range(1234)],
    )
    page = client.get(f"/tenants/{alpha}/offboard").get_data(as_text=True)
    assert f'1 file in the <a href="/tenants/{alpha}/report-history">Report history</a>' in page
    assert "1,234 trend figures" in page
    client.post(f"/tenants/{alpha}/delete", data={"confirm_name": "Alpha Ltd"})
    assert not db.execute("SELECT 1 FROM issued_reports WHERE tenant_id = ?", (alpha,)).fetchone()


def _on_disk(cfg) -> bytes:
    return b"".join(p.read_bytes() for p in cfg.data_dir.glob("officesentry.db*"))


def test_offboarding_erases_client_people_from_the_log_and_the_database_file(app, db, cfg, two_tenants):
    alpha, bravo = two_tenants
    person = "dana.erase-me@alpha-client.example"
    run = store.enqueue_run(db, alpha, "users")
    db.execute(
        "INSERT INTO objects (run_id, kind, object_id, data) VALUES (?, 'user', 'u1', ?)",
        (run, json.dumps({"userPrincipalName": person, "pad": "x" * 5000})),
    )
    make_user(db, "admin")
    client_user = make_user(db, "it@alpha-client.example", role="client", tenants=[alpha])
    staff = app.test_client()
    login(staff, "admin")
    staff.post(f"/tenants/{alpha}/contacts", data={"name": "Dana", "email": person})
    audit_rows = [
        ("finding_accepted", f"mfa_registered:{person}"),
        ("contact_removed", person),
        ("alert_failed", f"Alert to {person} failed"),
    ]
    db.executemany(
        "INSERT INTO audit_log (ts, username, action, tenant_id, detail) VALUES (?, 'admin', ?, ?, ?)",
        [("2026-01-01T00:00:00Z", a, alpha, d) for a, d in audit_rows],
    )
    db.execute(
        "INSERT INTO audit_log (ts, username, action, tenant_id, detail) VALUES ('2026-01-01T00:00:00Z',"
        " 'admin', 'finding_accepted', ?, 'mfa_registered:kept@bravo.example')",
        (bravo,),
    )
    login(app.test_client(), "it@alpha-client.example")  # the client's own sign-ins, with their address
    assert person.encode() in _on_disk(cfg)

    staff.post(f"/tenants/{alpha}/delete", data={"confirm_name": "Alpha Ltd", "delete_users": [str(client_user)]})
    assert store.get_tenant(db, alpha) is None
    log = [dict(r) for r in db.execute("SELECT * FROM audit_log ORDER BY id")]
    assert not any(person in (r["detail"] or "") for r in log)
    # Who did what, and when, is kept; only the detail naming the client's people goes.
    accepted = next(r for r in log if r["action"] == "finding_accepted" and r["tenant_id"] == alpha)
    assert accepted["username"] == "admin" and accepted["ts"] == "2026-01-01T00:00:00Z"
    assert "removed when the client was deleted" in accepted["detail"]
    # The deleted client account's own activity no longer names them or their address.
    assert not any(r["username"] == "it@alpha-client.example" for r in log)
    assert any(
        r["action"] == "login" and r["username"].startswith("deleted client account") and r["ip"] is None for r in log
    )
    # The offboarding record itself stays, and other tenants' rows are untouched.
    deleted = next(r for r in log if r["action"] == "tenant_deleted")
    assert deleted["username"] == "admin" and "Alpha Ltd" in deleted["detail"]
    assert any(r["detail"] == "mfa_registered:kept@bravo.example" for r in log)
    # The request only deletes; the worker's erase task clears the old copies on disk. After it, nothing
    # readable is left in the database file or its write-ahead log.
    assert tasks.pending(db, lifecycle.ERASE_TASK) and lifecycle.erase_state(db)
    assert tasks.drain(app, db, None, cfg) == 1
    assert person.encode() not in _on_disk(cfg)
    assert lifecycle.erase_state(db) is None
    assert db.execute("SELECT 1 FROM audit_log WHERE action = 'tenant_data_erased'").fetchone()


def _delete_person(app, db, cfg, person):
    """Delete Alpha Ltd, which stored ``person``, as an admin would. Returns the admin's client."""
    alpha, _ = (r["id"] for r in db.execute("SELECT id FROM tenants ORDER BY id"))
    run = store.enqueue_run(db, alpha, "users")
    db.execute(
        "INSERT INTO objects (run_id, kind, object_id, data) VALUES (?, 'user', 'u1', ?)",
        (run, json.dumps({"userPrincipalName": person, "pad": "x" * 5000})),
    )
    make_user(db, "admin")
    staff = app.test_client()
    login(staff, "admin")
    resp = staff.post(f"/tenants/{alpha}/delete", data={"confirm_name": "Alpha Ltd"}, follow_redirects=True)
    assert "erased from the server in the background" in resp.get_data(as_text=True)
    return staff


def test_without_room_to_rebuild_the_file_deleted_data_is_still_erased(app, db, cfg, two_tenants, monkeypatch):
    # The shipped Docker setup: a 1 GB database, a nearly full disk. VACUUM's copy can't fit, so it isn't tried
    # (it used to run in the request, fail, and skip emptying the write-ahead log). The deleted rows are zeros
    # already; writing the log back is all that's left.
    from officesentry import packing

    person = "no-room@alpha-client.example"
    monkeypatch.setattr(packing.shutil, "disk_usage", lambda _p: shutil._ntuple_diskusage(10**9, 10**9, 1000))
    _delete_person(app, db, cfg, person)
    assert tasks.drain(app, db, None, cfg) == 1
    assert person.encode() not in _on_disk(cfg)
    note = db.execute("SELECT detail FROM audit_log WHERE action = 'tenant_data_erased'").fetchone()[0]
    assert "wasn't shrunk" in note and "MB free" in note
    assert lifecycle.erase_state(db) is None


def test_an_erase_that_fails_is_shown_alerted_and_tried_again(app, db, cfg, two_tenants, monkeypatch):
    from officesentry import schedule, system_alerts

    person = "busy@alpha-client.example"
    staff = _delete_person(app, db, cfg, person)
    monkeypatch.setattr(lifecycle, "CHECKPOINT_WAIT", 0)
    db.execute("PRAGMA busy_timeout = 200")  # each checkpoint try waits for the reader this long, not 30 s
    reader = sqlite3.connect(cfg.db_path)  # a portal request reading mid-way keeps the log from being emptied
    reader.execute("BEGIN")
    reader.execute("SELECT COUNT(*) FROM settings").fetchone()
    db.execute("INSERT INTO settings (key, value) VALUES ('written_after_the_reader_began', '1')")
    try:
        assert tasks.drain(app, db, None, cfg) == 1
    finally:
        reader.close()
    task = tasks.latest(db, lifecycle.ERASE_TASK)
    assert task["status"] == "failed" and "kept the database busy" in task["error"]
    failed = db.execute("SELECT * FROM audit_log WHERE action = 'tenant_erase_failed'").fetchone()
    assert failed["username"] == "admin" and "couldn't be cleared" in failed["detail"]

    # The admin sees it on System status, and the team is told.
    now = datetime.now(UTC)
    check = system_health.assess(db, cfg, now).check("erase")
    assert check.status == system_health.WARN and "isn't fully erased" in check.summary
    page = staff.get("/admin/status").get_data(as_text=True)
    assert "Deleted clients" in page and "isn&#39;t fully erased" in page
    problem = system_alerts.detect("erase", system_health.assess(db, cfg, now), None, now)
    assert problem is not None and "can still be read" in problem.text

    # Tried again after an hour, and then it works.
    tick = lambda at: schedule.run_periodic(schedule.TickContext(app=app, conn=db, vault=None, cfg=cfg, now=at))  # noqa: E731
    tick(now + timedelta(minutes=5))
    assert tasks.pending(db, lifecycle.ERASE_TASK) is None
    tick(now + lifecycle.ERASE_RETRY + timedelta(minutes=1))
    assert tasks.pending(db, lifecycle.ERASE_TASK)
    assert tasks.drain(app, db, None, cfg) == 1
    assert person.encode() not in _on_disk(cfg)
    assert system_health.assess(db, cfg).check("erase").status == system_health.OK


def test_a_delete_while_erasing_is_erased_too(app, db, cfg, two_tenants):
    from officesentry import schedule

    alpha, bravo = two_tenants
    lifecycle.delete_tenant(db, store.get_tenant(db, alpha), "admin")
    task = tasks.claim_next(db)
    # Bravo is deleted while Alpha's erase runs: that try mustn't count for Bravo.
    real = lifecycle.erase

    def erase_then_delete(conn, progress):
        note = real(conn, progress)
        lifecycle.delete_tenant(db, store.get_tenant(db, bravo), "admin")
        return note

    with patch.object(lifecycle, "erase", erase_then_delete):
        tasks.run(app, db, None, cfg, task)
    assert lifecycle.erase_state(db) is not None
    # The delete couldn't queue a second task while the first ran; the worker's next tick does.
    assert tasks.pending(db, lifecycle.ERASE_TASK) is None
    schedule.run_periodic(schedule.TickContext(app=app, conn=db, vault=None, cfg=cfg, now=datetime.now(UTC)))
    assert tasks.pending(db, lifecycle.ERASE_TASK)
    assert tasks.drain(app, db, None, cfg) == 1
    assert lifecycle.erase_state(db) is None


def test_delete_refuses_without_foreign_keys(db, two_tenants):
    alpha, _ = two_tenants
    db.execute("PRAGMA foreign_keys = OFF")
    with pytest.raises(lifecycle.DeleteError):
        lifecycle.delete_tenant(db, store.get_tenant(db, alpha), "admin")
    db.execute("PRAGMA foreign_keys = ON")
    assert store.get_tenant(db, alpha) is not None


def test_delete_rolls_back_if_anything_would_be_left(db, two_tenants):
    alpha, _ = two_tenants
    store.enqueue_run(db, alpha, "users")
    db.execute("CREATE TABLE stray (id INTEGER PRIMARY KEY, tenant_id INTEGER)")  # a table without a foreign key
    db.execute("INSERT INTO stray (tenant_id) VALUES (?)", (alpha,))
    with pytest.raises(lifecycle.DeleteError, match="stray"):
        lifecycle.delete_tenant(db, store.get_tenant(db, alpha), "admin")
    assert store.get_tenant(db, alpha) is not None and lifecycle.stored(db, alpha)["runs"] == 1


def test_delete_tenant_command(cfg, db, two_tenants, capsys):
    from officesentry.__main__ import main

    alpha, bravo = two_tenants
    make_user(db, "alpha-it", role="client", tenants=[alpha])
    with patch("officesentry.__main__.load_config", return_value=cfg), patch("builtins.input", return_value="no"):
        with pytest.raises(SystemExit):
            main(["delete-tenant", str(alpha)])
    assert store.get_tenant(db, alpha) is not None
    with (
        patch("officesentry.__main__.load_config", return_value=cfg),
        patch("builtins.input", return_value="Alpha Ltd"),
    ):
        main(["delete-tenant", str(alpha), "--delete-client-users"])
    assert store.get_tenant(db, alpha) is None and store.get_tenant(db, bravo) is not None
    assert db.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0
    assert "Deleted Alpha Ltd" in capsys.readouterr().out


def test_health_reads_stored_state_only(db, two_tenants):
    # Assessing a tenant never calls Microsoft; it only reads what collectors stored.
    alpha, _ = two_tenants
    with responses.RequestsMock() as rsps:
        tenant_health.assess(db, store.get_tenant(db, alpha))
        assert not rsps.calls
    assert json.dumps(tenant_health.counts(tenant_health.assess(db, store.get_tenant(db, alpha))), default=str)
