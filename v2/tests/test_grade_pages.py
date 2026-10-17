"""The grade in the staff app (``web/grade.py``): the Grade page, the tile on the client's Overview and the badge in
the client header, shown only to staff who see findings, and figures only from the areas the person opens."""

from __future__ import annotations

from officesentry import grades, store
from officesentry.reports import monthly_review
from tests.conftest import login, make_user
from tests.seed import seed_tenant
from tests.test_roles import role_user


def _page(client, path: str) -> str:
    resp = client.get(path)
    assert resp.status_code == 200, (path, resp.status_code)
    return resp.get_data(as_text=True)


def test_the_grade_page_shows_the_parts_the_history_and_the_rules(client, db, two_tenants):
    alpha, _ = two_tenants
    seed_tenant(db, alpha)
    make_user(db, "admin")
    login(client, "admin")
    page = _page(client, f"/tenants/{alpha}/grade")
    assert "Alpha Ltd is an E because of Admin accounts" in page
    assert "Fix 1 check in Admin accounts and Alpha Ltd is a D." in page
    for part in grades.PARTS:
        assert f"<b>{part.title}</b>" in page
    assert 'class="grade e"' in page and 'class="grade xs e"' in page and 'class="grade xs d"' in page
    assert "Twelve months of grades" in page and "First graded this month" in page
    assert 'class="grade xs none"' in page  # the months before the first collection
    assert "How the grade is worked out" in page and "The rules" in page and "What each part checks" in page
    assert "Needs at least 2 other clients you can see with a grade" in page
    assert grades.stored_letter(db, alpha) == "E"  # the page keeps the stored row current


def test_the_page_before_any_collection(client, db, two_tenants):
    alpha, _ = two_tenants
    make_user(db, "admin")
    login(client, "admin")
    page = _page(client, f"/tenants/{alpha}/grade")
    assert "graded yet: no data has been collected yet." in page  # the apostrophe in "isn't" is escaped
    assert 'class="grade none"' in page and "No check could run" in page


def test_the_comparison_needs_two_other_clients_and_names_none(client, db, two_tenants, connection_id):
    alpha, bravo = two_tenants
    charlie = store.create_tenant(db, "Charlie Co", "charlie.onmicrosoft.com", connection_id)
    db.execute("UPDATE tenants SET consented_at = '2026-01-01T00:00:00Z'")
    for tid in (alpha, bravo, charlie):
        seed_tenant(db, tid)
    make_user(db, "admin")
    login(client, "admin")
    page = _page(client, f"/tenants/{alpha}/grade")
    assert "Needs at least 2 other clients" in page and "2 other clients have none stored yet" in page
    tenants = db.execute("SELECT * FROM tenants ORDER BY id").fetchall()
    grades.rows(db, tenants)
    for t in tenants:
        monthly_review.rollup_row(db, t, cached=True)
    page = _page(client, f"/tenants/{alpha}/grade")
    assert "Compared with your other clients" in page and "2 clients" in page
    for label in ("Secure Score", "Users with MFA", "Open critical and high findings", "Licence waste per user"):
        assert label in page, label
    assert "A better grade than 0 of the 2 with one." in page
    card = page.split('id="comparison"')[1].split("</section>")[0]  # the switcher in the header names clients
    assert "Bravo" not in card and "Charlie" not in card and "Median" in card
    # A paused client isn't a peer.
    db.execute("UPDATE tenants SET enabled = 0 WHERE id = ?", (charlie,))
    assert "Needs at least 2 other clients" in _page(client, f"/tenants/{alpha}/grade")


def test_figures_follow_the_areas_the_person_opens(client, db, two_tenants, connection_id):
    """An account manager sees Secure Score and licence waste (tenant reports) but not users with MFA
    (identity)."""
    alpha, bravo = two_tenants
    charlie = store.create_tenant(db, "Charlie Co", "charlie.onmicrosoft.com", connection_id)
    db.execute("UPDATE tenants SET consented_at = '2026-01-01T00:00:00Z'")
    for tid in (alpha, bravo, charlie):
        seed_tenant(db, tid)
    tenants = db.execute("SELECT * FROM tenants ORDER BY id").fetchall()
    grades.rows(db, tenants)
    for t in tenants:
        monthly_review.rollup_row(db, t, cached=True)
    role_user(db, "am", "account_manager", all_tenants=True)
    login(client, "am")
    page = _page(client, f"/tenants/{alpha}/grade")
    assert "Secure Score" in page and "Licence waste per user" in page and "Open critical and high" in page
    assert "Users with MFA" not in page


def test_the_overview_leads_with_the_grade_and_every_client_page_carries_it(client, db, two_tenants):
    alpha, _ = two_tenants
    seed_tenant(db, alpha)
    make_user(db, "admin")
    login(client, "admin")
    page = _page(client, f"/tenants/{alpha}")
    tiles = page.split('<div class="tiles">')[1].split("</div>\n</div>")[0]
    assert tiles.index('<div class="label">Grade') < tiles.index("Secure Score")
    assert f'href="/tenants/{alpha}/grade"' in tiles and 'class="grade e"' in tiles
    assert "First graded this month" in tiles
    # The header's badge reads the row the overview just stored, on this and every other client page.
    assert f'class="grade sm e" href="/tenants/{alpha}/grade"' in page
    assert f'class="grade sm e" href="/tenants/{alpha}/grade"' in _page(client, f"/tenants/{alpha}/findings")


def test_the_clients_page_has_a_grade_column_for_staff_who_see_findings(client, db, two_tenants):
    alpha, _ = two_tenants
    seed_tenant(db, alpha)
    make_user(db, "admin")
    login(client, "admin")
    assert 'class="grade xs' not in _page(client, "/")  # the list never builds a grade
    grades.rows(db, db.execute("SELECT * FROM tenants").fetchall())  # as the worker does
    page = _page(client, "/")
    assert '<button type="button" class="sort">Grade</button>' in page
    assert f'class="grade xs e" href="/tenants/{alpha}/grade"' in page
    assert 'title="Not graded yet"' in page  # Bravo has no data yet
    client.post("/logout")
    role_user(db, "first", "first_line", all_tenants=True)
    login(client, "first")
    page = _page(client, "/")
    assert ">Grade</button>" not in page and 'class="grade' not in page


def test_no_badge_until_a_grade_is_stored(client, db, two_tenants):
    alpha, _ = two_tenants
    seed_tenant(db, alpha)
    make_user(db, "admin")
    login(client, "admin")
    assert 'class="grade sm' not in _page(client, f"/tenants/{alpha}/findings")
    _page(client, f"/tenants/{alpha}/grade")
    assert 'class="grade sm e"' in _page(client, f"/tenants/{alpha}/findings")


def test_only_staff_who_see_findings_get_the_grade(client, db, two_tenants):
    alpha, _ = two_tenants
    seed_tenant(db, alpha)
    grades.rows(db, db.execute("SELECT * FROM tenants").fetchall())
    role_user(db, "first", "first_line", all_tenants=True)
    login(client, "first")
    assert client.get(f"/tenants/{alpha}/grade").status_code == 403
    page = _page(client, f"/tenants/{alpha}")
    assert 'class="grade' not in page and '<div class="label">Grade' not in page
    client.post("/logout")
    make_user(db, "client", role="client", tenants=[alpha])
    login(client, "client")
    assert client.get(f"/tenants/{alpha}/grade").status_code == 403
    assert 'class="grade' not in _page(client, f"/tenants/{alpha}/report")


def test_the_style_guide_shows_the_badge(client, db):
    make_user(db, "admin")
    login(client, "admin")
    page = _page(client, "/styleguide")
    assert 'class="grade a"' in page and 'class="grade xs e"' in page and 'class="grade sm none"' in page
