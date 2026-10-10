"""The Get started checklist (first_run.py): on admin home pages from a fresh install until the first collection."""

from officesentry import first_run, store
from officesentry.db import utcnow
from tests.conftest import login, make_user


def _run(db, tenant_id, status="succeeded"):
    db.execute(
        "INSERT INTO runs (tenant_id, collector, status, trigger, created_at, finished_at)"
        " VALUES (?, 'tenant_profile', ?, 'schedule', ?, ?)",
        (tenant_id, status, utcnow(), utcnow()),
    )


def test_the_steps_tick_off_from_stored_state(db, vault):
    from officesentry.certs import generate_certificate

    done = lambda: [s.key for s in first_run.steps(db) if s.done]  # noqa: E731
    assert done() == []
    assert first_run.current(first_run.steps(db)).key == "connection"
    assert first_run.steps(db)[2].target == ""  # nothing to open yet

    # A connection without its client ID isn't ready; with one it is.
    cid = store.create_connection(db, vault, "Office Sentry", generate_certificate("t"))
    assert done() == []
    db.execute("UPDATE app_connections SET client_id = '11111111-2222-3333-4444-555555555555' WHERE id = ?", (cid,))
    assert done() == ["connection"]

    a = store.create_tenant(db, "Alpha Ltd", "alpha.onmicrosoft.com", cid)
    steps = first_run.steps(db)
    assert done() == ["connection", "tenant"] and "Alpha Ltd is added" in steps[1].text
    assert steps[2].target == f"tenant:{a}" and steps[3].target == f"collection:{a}"

    # A failed run isn't a first collection; a partial one is.
    db.execute("UPDATE tenants SET consented_at = ? WHERE id = ?", (utcnow(), a))
    _run(db, a, "failed")
    assert done() == ["connection", "tenant", "consent"] and first_run.pending(db) is not None
    _run(db, a, "partial")
    assert done() == ["connection", "tenant", "consent", "collection"] and first_run.pending(db) is None


def test_an_archived_tenant_does_not_count(db, connection_id):
    a = store.create_tenant(db, "Gone Ltd", "gone.onmicrosoft.com", connection_id)
    db.execute("UPDATE tenants SET archived_at = ? WHERE id = ?", (utcnow(), a))
    assert first_run.current(first_run.steps(db)).key == "tenant"


def test_admins_see_the_checklist_on_every_home_page_until_the_first_collection(client, db, connection_id):
    make_user(db, "admin")
    make_user(db, "ana", role="analyst", all_tenants=True)
    login(client, "admin")
    for path in ("/today", "/clients", "/"):
        page = client.get(path).get_data(as_text=True)
        assert "Get started" in page and "1 of 4 done" in page, path
        assert 'href="/tenants/new"' in page and "Add a tenant" in page, path  # the current step's button
        assert "No clients" not in page and "No tenants yet" not in page, path

    a = store.create_tenant(db, "Alpha Ltd", "alpha.onmicrosoft.com", connection_id)
    page = client.get("/today").get_data(as_text=True)
    assert "2 of 4 done" in page and f'href="/tenants/{a}"' in page and "Open Alpha Ltd" in page
    db.execute("UPDATE tenants SET consented_at = ? WHERE id = ?", (utcnow(), a))
    page = client.get("/").get_data(as_text=True)
    assert "3 of 4 done" in page and f'href="/tenants/{a}/collection"' in page

    _run(db, a)
    for path in ("/today", "/clients", "/"):
        assert "Get started" not in client.get(path).get_data(as_text=True), path

    # Analysts never see it; without tenants they get the plain empty state.
    client.post("/logout")
    db.execute("DELETE FROM runs")
    login(client, "ana")
    page = client.get("/today").get_data(as_text=True)
    assert "Get started" not in page


def test_an_analyst_with_nothing_granted_sees_the_empty_state(client, db):
    make_user(db, "ana", role="analyst")
    login(client, "ana")
    page = client.get("/today").get_data(as_text=True)
    assert "Get started" not in page and "haven't been given access" in page


def test_the_report_library_does_not_count_zero_tenants(client, db):
    make_user(db, "admin")
    login(client, "admin")
    page = client.get("/reports").get_data(as_text=True)
    assert "across all 0" not in page and "No tenants yet" in page
