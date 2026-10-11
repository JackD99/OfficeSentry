"""Search across tenants: what it finds, where results link, and who may search."""

import html

from officesentry import lifecycle, search_index, store
from officesentry.reports import data_reports, views
from officesentry.web.search import looks_like_ip, search, search_tenant
from tests import graphdata as gd
from tests.conftest import login, make_user
from tests.seed import seed_tenant
from tests.test_collectors import USERS

ZED = {
    "id": "u9",
    "displayName": "Zed Quinn",
    "userPrincipalName": "zed.quinn@x.com",
    "userType": "Member",
    "accountEnabled": True,
    "assignedLicenses": [],
    "signInActivity": {},
}


def _hits(app, db, tenant_id, q):
    with app.test_request_context():  # links are built with url_for
        return search_tenant(db, store.get_tenant(db, tenant_id), q.lower()).hits


def _rows_behind(db, href):
    """The first two cells of each row a result's report link keeps (the filtered table only)."""
    path, _, query = href.partition("?")
    tenant_id, key = int(path.split("/")[2]), path.split("/")[4]
    view = views.View.parse(query)
    report = views.apply(data_reports.build(db, store.get_tenant(db, tenant_id), key), view)
    table = next(iter(view.filters))[0]
    section = next(s for s, slug in zip(report.sections, views.table_slugs(report), strict=True) if slug == table)
    return [[str(c) if isinstance(c, str) else c for c in row[:2]] for row in section.rows]


def _page(client, q, **params):
    resp = client.get("/search", query_string={"q": q, **params})
    assert resp.status_code == 200
    return html.unescape(resp.get_data(as_text=True))


def test_finds_people_forwarding_addresses_and_ips(app, client, db, two_tenants):
    alpha, _ = two_tenants
    seed_tenant(db, alpha, users=[*USERS, gd.INFO_USER, ZED])

    person = _hits(app, db, alpha, "zed.quinn")
    assert [(h.label, h.title, h.href) for h in person] == [("Person", "Zed Quinn", f"/tenants/{alpha}/users/u9")]
    # Ann matches as a person and as a mailbox; she's listed once, on her own page.
    ann = [h for h in _hits(app, db, alpha, "ann@x.com") if h.href == f"/tenants/{alpha}/users/u1"]
    assert len(ann) == 1 and ann[0].label == "Person"
    # The shared info@ mailbox has an account too: one result, on that account's page.
    assert [h.label for h in _hits(app, db, alpha, "info@") if h.href == f"/tenants/{alpha}/users/u7"] == ["Person"]

    # Where mail goes: an admin-set forward and an inbox rule, each opening Mail forwarding filtered to it.
    fwd = _hits(app, db, alpha, "ann@gmail.com")
    assert [h.label for h in fwd] == ["Forwarding"]
    assert fwd[0].href == f"/tenants/{alpha}/data/mailboxes?f=forwarding%2Fforwards-to%3Aann%40gmail.com"
    assert _rows_behind(db, fwd[0].href) == [["ann@x.com", "ann@gmail.com"]]
    rule = _hits(app, db, alpha, "home@gmail")
    assert [h.label for h in rule] == ["Inbox rule"] and "Copy to home" in rule[0].detail
    assert _rows_behind(db, rule[0].href) == [["ann@x.com", "ann.home@gmail.com"]]

    # IP addresses match from the start: the spraying address and who signed in from it.
    failed = [h for h in _hits(app, db, alpha, "203.0.113") if h.label == "Failed sign-ins"]
    assert [h.title for h in failed] == ["203.0.113.66: 5 failed sign-ins against 5 accounts"]
    assert _rows_behind(db, failed[0].href) == [["203.0.113.66", 5]]
    office = _hits(app, db, alpha, "192.0.2.10")
    assert any(h.label == "Sign-in address" and h.href == f"/tenants/{alpha}/users/u1" for h in office)
    assert not _hits(app, db, alpha, "0.2.10")  # not the start of an address

    # Apps by name or ID, devices and domains, each filtered to its row.
    app_hit = next(h for h in _hits(app, db, alpha, "app-evil") if h.label == "App")
    assert app_hit.title == "Mail Sync Pro" and _rows_behind(db, app_hit.href)[0][0] == "Mail Sync Pro"
    device = next(h for h in _hits(app, db, alpha, "laptop-ann") if h.label == "Device")
    assert _rows_behind(db, device.href)[0][0] == "LAPTOP-ANN"
    domain = [h for h in _hits(app, db, alpha, "parked") if h.label == "Domain"]
    assert [h.title for h in domain] == ["parked.com"] and _rows_behind(db, domain[0].href)[0][0] == "parked.com"

    make_user(db, "admin")
    login(client, "admin")
    # The page groups by tenant and escapes what was typed.
    page = _page(client, "zed")
    assert "Alpha Ltd" in page and "Zed Quinn" in page and "Bravo Inc" not in page
    raw = client.get("/search", query_string={"q": "<b>zed</b>"}).get_data(as_text=True)
    assert "<b>zed</b>" not in raw and "&lt;b&gt;zed&lt;/b&gt;" in raw


def test_only_granted_tenants_are_searched(app, client, db, two_tenants):
    alpha, bravo = two_tenants
    seed_tenant(db, alpha, users=[*USERS, ZED])
    seed_tenant(db, bravo)
    make_user(db, "admin")
    make_user(db, "bravo-analyst", role="analyst", tenants=[bravo])
    # Without an account in the snapshot, a mailbox opens the shared mailboxes report.
    info = [h for h in _hits(app, db, alpha, "info@x.com") if h.label == "Mailbox"]
    assert [h.href for h in info] == [f"/tenants/{alpha}/data/shared-mailboxes"]

    login(client, "admin")
    page = _page(client, "ann@x.com")
    assert "Alpha Ltd" in page and "Bravo Inc" in page
    assert "Zed Quinn" in _page(client, "zed")
    client.post("/logout")

    login(client, "bravo-analyst")
    page = _page(client, "ann@x.com")
    assert "Bravo Inc" in page and "Alpha Ltd" not in page and f"/tenants/{alpha}/" not in page
    page = _page(client, "zed")
    assert "Zed Quinn" not in page and "Nothing found" in page
    # Narrowing to a tenant they can't see is a 404, as for any tenant page.
    assert client.get("/search", query_string={"q": "zed", "tenant": alpha}).status_code == 404
    assert "Ann" in _page(client, "ann", tenant=bravo)

    # Archived tenants drop out of search.
    db.execute("UPDATE tenants SET archived_at = '2026-10-01T00:00:00Z' WHERE id = ?", (bravo,))
    assert "Bravo Inc" not in _page(client, "ann@x.com")


def test_more_results_are_counted_and_listed_per_tenant(client, db, two_tenants):
    alpha, _ = two_tenants
    many = [
        {
            "id": f"m{i}",
            "displayName": f"Temp worker {i}",
            "userPrincipalName": f"temp{i}@x.com",
            "userType": "Member",
            "accountEnabled": True,
            "assignedLicenses": [],
        }
        for i in range(12)
    ]
    seed_tenant(db, alpha, users=[*USERS, *many])
    make_user(db, "admin")
    login(client, "admin")
    page = _page(client, "temp worker")
    assert "4 more in Alpha Ltd" in page and page.count("Temp worker") == 8
    assert _page(client, "temp worker", tenant=alpha).count("Temp worker") == 12


def test_clients_cannot_search(client, db, two_tenants):
    alpha, _ = two_tenants
    make_user(db, "client", role="client", tenants=[alpha])
    assert client.get("/search?q=ann").status_code == 302  # signed out: to the sign-in page
    login(client, "client")
    assert client.get("/search?q=ann").status_code == 403
    assert 'class="nav-search"' not in client.get(f"/tenants/{alpha}/report").get_data(as_text=True)


def test_empty_and_short_queries(client, db, two_tenants):
    make_user(db, "analyst", role="analyst", all_tenants=True)
    login(client, "analyst")
    assert 'class="nav-search"' in client.get("/").get_data(as_text=True)
    assert "Find something in any client" in _page(client, "")
    assert "Type at least 2 characters" in _page(client, "a")
    assert "Nothing found" in _page(client, "nobody-here")  # tenants without data are skipped


def test_ip_like_queries():
    assert looks_like_ip("203.0.113") and looks_like_ip("2001:db8") and looks_like_ip("10.")
    assert not looks_like_ip("ann") and not looks_like_ip("cafe") and not looks_like_ip("2026")


# -- The index ------------------------------------------------------------------------------------------


def _shown(hits):
    return [(h.label, h.title, h.detail, h.href) for h in hits]


def test_the_index_finds_what_a_scan_finds(app, db, two_tenants):
    alpha, bravo = two_tenants
    seed_tenant(db, alpha, users=[*USERS, gd.INFO_USER, ZED])
    seed_tenant(db, bravo)
    tenants = db.execute("SELECT * FROM tenants ORDER BY id").fetchall()
    assert search_index.current(db, tenants) == set()  # nothing indexed yet: every tenant is scanned
    for t in tenants:
        assert search_index.refresh(db, t)
    assert search_index.current(db, tenants) == {alpha, bravo}
    assert not search_index.refresh(db, tenants[0])  # up to date: nothing written
    with app.test_request_context():
        for q in (
            "zed",
            "ann@x.com",
            "ann@gmail.com",
            "home@gmail",
            "203.0.113",
            "192.0.2.10",
            "app-evil",
            "laptop",
            "parked",
        ):
            indexed, _ = search(db, tenants, q)
            scanned = [g for g in (search_tenant(db, t, q) for t in tenants) if g.hits]
            assert [(g.tenant["id"], _shown(g.hits)) for g in indexed] == [
                (g.tenant["id"], _shown(g.hits)) for g in scanned
            ], q
        assert not search(db, tenants, "0.2.10")[0]  # not the start of an address
        assert not search(db, tenants, "nobody-here")[0]


def test_an_index_behind_the_latest_collection_is_not_used(app, db, two_tenants):
    alpha, _ = two_tenants
    seed_tenant(db, alpha, users=USERS)
    tenant = store.get_tenant(db, alpha)
    search_index.refresh(db, tenant)
    seed_tenant(db, alpha, users=[*USERS, ZED])  # a new collection: Zed joined
    assert search_index.current(db, [tenant]) == set()
    with app.test_request_context():
        groups, _ = search(db, [tenant], "zed")  # scanned, so the newcomer is found
        assert [h.title for g in groups for h in g.hits] == ["Zed Quinn"]
        assert search_index.refresh(db, tenant)
        assert search_index.current(db, [tenant]) == {alpha}
        groups, _ = search(db, [tenant], "zed")
        assert [h.title for g in groups for h in g.hits] == ["Zed Quinn"]
    # Deleting the tenant takes its rows with it.
    assert db.execute("SELECT COUNT(*) FROM search_index WHERE tenant_id = ?", (alpha,)).fetchone()[0]
    lifecycle.delete_tenant(db, tenant, "admin")
    assert db.execute("SELECT COUNT(*) FROM search_index").fetchone()[0] == 0


def test_the_index_only_answers_for_the_tenants_asked_about(app, client, db, two_tenants):
    alpha, bravo = two_tenants
    seed_tenant(db, alpha, users=[*USERS, ZED])
    seed_tenant(db, bravo)
    for t in db.execute("SELECT * FROM tenants").fetchall():
        search_index.refresh(db, t)
    assert search_index.find(db, [bravo], "zed", False) == {}
    assert list(search_index.find(db, [alpha, bravo], "zed", False)) == [alpha]
    make_user(db, "bravo-analyst", role="analyst", tenants=[bravo])
    login(client, "bravo-analyst")
    page = _page(client, "zed")
    assert "Zed Quinn" not in page and "Nothing found" in page
    assert "Ann" in _page(client, "ann@x.com") and "Alpha Ltd" not in _page(client, "ann@x.com")


def test_searches_use_the_indexes(db, two_tenants):
    alpha, _ = two_tenants
    seed_tenant(db, alpha)
    search_index.refresh(db, store.get_tenant(db, alpha))
    db.execute("ANALYZE")
    plan = " ".join(
        r["detail"]
        for r in db.execute(
            "EXPLAIN QUERY PLAN SELECT * FROM search_index WHERE value >= ? AND value < ? AND tenant_id IN (?)",
            ("203.", "203/", alpha),
        )
    )
    assert "idx_search_index_value" in plan
    plan = " ".join(
        r["detail"] for r in db.execute("EXPLAIN QUERY PLAN DELETE FROM search_index WHERE tenant_id = ?", (alpha,))
    )
    assert "idx_search_index_tenant" in plan
