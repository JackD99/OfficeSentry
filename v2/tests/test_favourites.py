"""Favourite reports (web/favourites.py) and recent clients in Switch client (_macros.tenant_header, app.js)."""

from __future__ import annotations

from officesentry.web import favourites
from tests.conftest import login, make_user
from tests.seed import seed_tenant
from tests.test_roles import role_user


def _favourites(db, username="admin"):
    return [
        r[0]
        for r in db.execute(
            "SELECT report_key FROM report_favourites f JOIN users u ON u.id = f.user_id WHERE u.username = ?"
            " ORDER BY f.created_at, f.rowid",
            (username,),
        )
    ]


def _group(page: str, label: str) -> str:
    """The text of one group of a report menu, from its label to the end of its list."""
    start = page.index(f">{label}<")
    return page[start : page.index("</ul>", start) if "</ul>" in page[start:] else len(page)]


def test_star_a_report_and_it_lists_first_on_every_client(client, db, two_tenants):
    alpha, bravo = two_tenants
    seed_tenant(db, alpha)
    make_user(db, "admin")
    login(client, "admin")
    page = client.get(f"/tenants/{alpha}/data/mfa").get_data(as_text=True)
    assert 'class="sm fav"' in page and 'aria-pressed="false"' in page and "Your favourites" not in page
    resp = client.post("/reports/mfa/favourite", data={"on": "1", "next": f"/tenants/{alpha}/data/mfa"})
    assert resp.status_code == 302 and resp.headers["Location"] == f"/tenants/{alpha}/data/mfa"
    client.post("/reports/licences/favourite", data={"on": "1", "next": f"/tenants/{alpha}/data/mfa"})
    client.post("/reports/mfa/favourite", data={"on": "1"})  # starred already: kept once
    assert _favourites(db) == ["mfa", "licences"]
    page = client.get(f"/tenants/{alpha}/data/mfa").get_data(as_text=True)
    assert 'class="sm fav on" aria-pressed="true"' in page
    menu = _group(page, "Your favourites")
    assert menu.index("MFA coverage") < menu.index("Licence waste")  # in the order they were starred
    # The same on another client's menu and Reports tab.
    other = client.get(f"/tenants/{bravo}/data").get_data(as_text=True)
    assert "Your favourites" in other and f'href="/tenants/{bravo}/data/licences"' in _group(other, "Your favourites")
    # Taking the star off.
    client.post("/reports/mfa/favourite", data={"on": "0"})
    assert _favourites(db) == ["licences"]


def test_only_real_reports_the_person_opens(client, db, two_tenants):
    make_user(db, "admin")
    login(client, "admin")
    assert client.post("/reports/nope/favourite", data={"on": "1"}).status_code == 404
    resp = client.post("/reports/mfa/favourite", data={"on": "1", "next": "//evil.example/x"})
    assert resp.headers["Location"] == "/reports"
    client.post("/logout")
    # First line doesn't open the tenant reports, so it can't star one.
    role_user(db, "first", "first_line", all_tenants=True)
    login(client, "first")
    assert client.post("/reports/licences/favourite", data={"on": "1"}).status_code == 403
    assert _favourites(db, "first") == []


def test_a_favourite_lists_only_where_the_person_may_open_it(client, db, two_tenants):
    alpha, _ = two_tenants
    make_user(db, "admin")
    first = role_user(db, "first", "first_line", all_tenants=True)
    db.execute(
        "INSERT INTO report_favourites (user_id, report_key, created_at) VALUES (?, 'licences', 'a'), (?, 'mfa', 'b')",
        (first, first),
    )
    login(client, "first")
    page = client.get(f"/tenants/{alpha}/data").get_data(as_text=True)
    favs = _group(page, "Your favourites")
    assert "MFA coverage" in favs and "Licence waste" not in favs


def test_favourites_are_each_persons_own_and_go_with_them(client, db, two_tenants):
    alpha, _ = two_tenants
    make_user(db, "admin")
    other = make_user(db, "other", role="analyst", all_tenants=True)
    login(client, "admin")
    client.post("/reports/mfa/favourite", data={"on": "1"})
    client.post("/logout")
    login(client, "other")
    assert "Your favourites" not in client.get(f"/tenants/{alpha}/data").get_data(as_text=True)
    client.post("/reports/licences/favourite", data={"on": "1"})
    db.execute("DELETE FROM users WHERE id = ?", (other,))
    assert _favourites(db, "admin") == ["mfa"]
    assert db.execute("SELECT COUNT(*) FROM report_favourites").fetchone()[0] == 1


def test_up_to_a_limit(client, db, two_tenants, monkeypatch):
    monkeypatch.setattr(favourites, "MAX_FAVOURITES", 2)
    make_user(db, "admin")
    login(client, "admin")
    for key in ("mfa", "licences", "mailboxes"):
        client.post(f"/reports/{key}/favourite", data={"on": "1"})
    assert _favourites(db) == ["mfa", "licences"]


def test_switch_client_carries_each_clients_id_for_recent(client, db, two_tenants):
    alpha, bravo = two_tenants
    make_user(db, "admin")
    login(client, "admin")
    page = client.get(f"/tenants/{alpha}/data").get_data(as_text=True)
    assert 'data-recent="' in page and f'data-here="{alpha}"' in page
    assert f'data-tenant="{bravo}"' in page and f'data-tenant="{alpha}"' in page
    assert "data-recent-group hidden" in page  # filled in by app.js from this browser's recent clients
