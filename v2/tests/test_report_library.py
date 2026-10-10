"""The report library page, table views (filter, columns, sort, saved views) and headline links."""

from __future__ import annotations

import csv
import io
import re
from datetime import UTC, datetime
from html import unescape

from openpyxl import load_workbook

from officesentry.reports import data_reports, library, rollups, views
from officesentry.reports.data_reports import DataReport, Many, Rows, Section
from tests.conftest import login, make_user
from tests.seed import seed_tenant

NOW = datetime(2026, 10, 3, tzinfo=UTC)


def sample() -> DataReport:
    report = DataReport("x", "Sample", "", {"name": "T"}, NOW)
    people = Section("People", ["Name", "Roles", "Last sign-in", "Size"])
    people.add(["Ann", ["Global Administrator", "Exchange Administrator"], "2026-09-30", "2.0 GB"], "high")
    people.add(["Bob", ["Exchange Administrator"], "2026-01-02", "900.0 MB"])
    people.add(["Cara", [], "Never", "10.0 GB"], "medium")
    people.add(["Dev", ["Global Administrator"], "2025-06-01", ""], "low")
    report.sections = [people, Section("Other", ["Setting", "Value"], rows=[["A", "B"]], flags=[None])]
    return report


def names(report: DataReport) -> list[str]:
    return [r[0] for r in report.sections[0].rows]


def test_view_round_trips_through_the_query_string():
    q = "f=people/roles:Global+Administrator&age=people/last-sign-in:gt90&hide=people/size&sort=people/name:desc"
    view = views.View.parse(q)
    assert views.View.parse(view.query()).params() == view.params()
    assert view.filters == {("people", "roles"): ["Global Administrator"]}
    assert view.ages == {("people", "last-sign-in"): "gt90"}
    # Junk is ignored rather than failing.
    assert not views.View.parse("f=../x:1&age=a/b:forever&sort=a/b:sideways&hide=Bad Slug/x&other=1")


def test_filters_match_list_items_dates_and_flags():
    report = sample()
    pick = lambda q: names(views.apply(report, views.View.parse(q)))  # noqa: E731
    assert pick("f=people/roles:Global+Administrator") == ["Ann", "Dev"]  # each item of a list cell
    assert pick("f=people/roles:Global+Administrator&f=people/roles:Exchange+Administrator") == ["Ann", "Bob", "Dev"]
    assert pick("f=people/roles:") == ["Cara"]  # blank
    assert pick("age=people/last-sign-in:gt90") == ["Bob", "Dev"]
    assert pick("age=people/last-sign-in:lt30") == ["Ann"]
    assert pick("age=people/last-sign-in:none") == ["Cara"]
    assert pick("f=people/flag:high&f=people/flag:medium") == ["Ann", "Cara"]
    assert pick("f=people/flag:none") == ["Bob"]
    assert pick("f=people/roles:Global+Administrator&age=people/last-sign-in:gt90") == ["Dev"]  # AND across columns


def test_sort_and_hidden_columns():
    report = sample()
    out = views.apply(report, views.View.parse("sort=people/size:desc&hide=people/roles"))
    assert names(out) == ["Cara", "Ann", "Bob", "Dev"]  # sizes as bytes, blank last
    assert out.sections[0].columns == ["Name", "Last sign-in", "Size"]
    assert names(views.apply(report, views.View.parse("sort=people/flag:asc"))) == ["Ann", "Cara", "Dev", "Bob"]
    # The other table is untouched, and every column can't be hidden.
    out = views.apply(report, views.View.parse("hide=other/setting&hide=other/value"))
    assert out.sections[1].columns == ["Setting"]
    assert out.filtered == ["Other: 1 of 1 rows, 2 columns hidden."]


def test_table_view_offers_values_and_presets():
    tv = views.table_view(sample(), 0, views.View.parse("f=people/roles:Exchange+Administrator"), "/p")
    by = {c.slug: c for c in tv.columns}
    assert [o.label for o in by["roles"].options] == ["Exchange Administrator", "Global Administrator", "Blank"]
    assert [o.count for o in by["roles"].options] == [2, 2, 1]
    assert by["roles"].options[0].active and by["roles"].filtered
    assert by["name"].options == []  # every value is different: nothing to filter by
    assert [o.label for o in by["last-sign-in"].options] == [
        "In the last 30 days",
        "Older than 30 days",
        "Older than 90 days",
        "Older than 180 days",
        "Older than a year",
        "Never or blank",
    ]
    assert [o.label for o in tv.flag.options] == ["To review", "High", "Medium", "Low", "No flag"]
    assert [c.label for c in tv.chips] == ["Roles: Exchange Administrator"]
    assert tv.chips[0].href == "/p#t-people"  # removing the only filter
    assert [r[0][0] for r in tv.rows] == ["Ann", "Bob"]
    assert tv.columns[0].sort_href == "/p?f=people%2Froles%3AExchange+Administrator&sort=people%2Fname%3Aasc#t-people"


def test_many_cells_still_read_as_text():
    cell = data_reports._cell(["a", "b"])
    assert isinstance(cell, Many) and cell == "a, b" and cell.parts == ("a", "b")


def test_headline_links_point_at_rows():
    report = sample()
    admins = Rows("People", {"Roles": "Global Administrator"})
    report.highlights = [
        data_reports.Highlight("Global admins", 2, rows=admins),
        data_reports.Highlight("To review", 2, rows=Rows("People", flagged=True)),
        data_reports.Highlight("People", 4),
        data_reports.Highlight("Missing", 1, rows=Rows("Nope")),
        data_reports.Highlight("Plain", 1),
    ]
    links = views.headline_links(report, "/r")["highlights"]
    assert links[0] == "/r?f=people%2Froles%3AGlobal+Administrator#t-people"
    assert links[1] == "/r?f=people%2Fflag%3Ahigh&f=people%2Fflag%3Amedium#t-people"
    assert links[2] == "/r#t-people"  # named like a table
    assert links[3:] == ["", ""]
    assert views.flagged_link(report, "/r") == "/r?f=people%2Fflag%3Ahigh&f=people%2Fflag%3Amedium#t-people"


def test_every_seeded_headline_link_opens_a_table(client, db, two_tenants):
    alpha, _ = two_tenants
    seed_tenant(db, alpha)
    make_user(db, "admin")
    login(client, "admin")
    tenant = db.execute("SELECT * FROM tenants WHERE id = ?", (alpha,)).fetchone()
    linked = 0
    for key in data_reports.REPORTS:
        report = data_reports.build(db, tenant, key)
        path = f"/tenants/{alpha}/data/{key}"
        anchors = {f"t-{s}" for s in views.table_slugs(report)}
        for href in sum(views.headline_links(report, path).values(), []):
            if not href:
                continue
            linked += 1
            assert href.split("#")[1] in anchors, (key, href)
            page = client.get(href.split("#")[0])
            assert page.status_code == 200, href
            assert f'id="{href.split("#")[1]}"' in page.get_data(as_text=True), href
    assert linked >= 40


def test_report_page_filters_hides_and_downloads_the_same_rows(client, db, two_tenants):
    alpha, _ = two_tenants
    seed_tenant(db, alpha)
    make_user(db, "admin")
    login(client, "admin")
    base = f"/tenants/{alpha}/data/mailboxes"
    page = client.get(base).get_data(as_text=True)
    assert 'id="filter-forwarding"' in page and "Show only rows where" in page
    q = "f=forwarding/outside-the-organisation:Yes&hide=forwarding/keeps-a-copy"
    page = unescape(client.get(f"{base}?{q}").get_data(as_text=True))
    assert "2 of 3" in page and "Outside the organisation: Yes" in page and "Clear filters" in page
    table = page[page.index('id="tbl-forwarding"') :]
    table = table[: table.index("</table>")]
    assert "Keeps a copy" not in table and table.count("<tr") == 3  # header + two rows
    assert f"/tenants/{alpha}/data/mailboxes.pdf?{q}" in page.replace("%2F", "/").replace("%3A", ":")
    body = client.get(f"{base}.csv?section=1&{q}").get_data(as_text=True).lstrip("\ufeff")
    rows = list(csv.reader(io.StringIO(body)))
    assert rows[0] == ["Mailbox", "Forwards to", "Set by", "Outside the organisation", "Flag"]
    assert len(rows) == 3 and all(r[3] == "Yes" for r in rows[1:])
    wb = load_workbook(io.BytesIO(client.get(f"{base}.xlsx?{q}").data))
    assert wb["Forwarding"].max_row == 3
    assert any("Filtered view" == r[0] for r in wb["Summary"].iter_rows(values_only=True))
    # A filter that matches nothing says so and offers the way back.
    page = client.get(f"{base}?f=forwarding/set-by:Nobody").get_data(as_text=True)
    assert "No rows match this filter" in page and "Show all 3" in page


def test_saved_views(client, db, two_tenants):
    alpha, bravo = two_tenants
    seed_tenant(db, alpha)
    make_user(db, "admin")
    make_user(db, "other", role="analyst", all_tenants=True)
    login(client, "admin")
    q = "f=forwarding/outside-the-organisation:Yes"
    resp = client.post("/reports/mailboxes/views", data={"name": "  Leaving us  ", "query": q, "tenant_id": alpha})
    assert resp.status_code == 302 and resp.headers["Location"].endswith(
        f"/tenants/{alpha}/data/mailboxes?f=forwarding%2Foutside-the-organisation%3AYes"
    )
    row = db.execute("SELECT * FROM saved_views").fetchone()
    assert row["name"] == "Leaving us" and row["report_key"] == "mailboxes"
    # Saving the same name again replaces it; an empty view or unknown report isn't saved.
    client.post(
        "/reports/mailboxes/views",
        data={"name": "Leaving us", "query": q + "&sort=forwarding/mailbox:desc", "tenant_id": alpha},
    )
    assert db.execute("SELECT COUNT(*) FROM saved_views").fetchone()[0] == 1
    client.post("/reports/mailboxes/views", data={"name": "Empty", "query": "bogus=1", "tenant_id": alpha})
    assert client.post("/reports/nope/views", data={"name": "x", "query": q, "tenant_id": alpha}).status_code == 404
    assert db.execute("SELECT COUNT(*) FROM saved_views").fetchone()[0] == 1
    # It's offered on the report, and in the library for any tenant.
    assert "Leaving us" in client.get(f"/tenants/{alpha}/data/mailboxes").get_data(as_text=True)
    assert "Leaving us" in client.get(f"/reports?tenant={bravo}").get_data(as_text=True)
    view_id = db.execute("SELECT id FROM saved_views").fetchone()[0]
    # Only its owner can delete it, and "next" can't leave the portal.
    client.post("/logout")
    login(client, "other")
    assert "Leaving us" not in client.get(f"/tenants/{alpha}/data/mailboxes").get_data(as_text=True)
    assert client.post(f"/views/{view_id}/delete").status_code == 404
    client.post("/logout")
    login(client, "admin")
    resp = client.post(f"/views/{view_id}/delete", data={"next": "//evil.example/x"})
    assert resp.status_code == 302 and resp.headers["Location"] == "/reports"
    assert db.execute("SELECT COUNT(*) FROM saved_views").fetchone()[0] == 0
    # The same check as after sign-in: backslashes and control characters (which browsers drop) are refused too.
    uid = db.execute("SELECT id FROM users WHERE username = 'admin'").fetchone()[0]
    for target in ("/\\evil.example/path", "/\t/evil.example"):
        vid = db.execute(
            "INSERT INTO saved_views (user_id, report_key, name, query, created_at)"
            " VALUES (?, 'mailboxes', ?, 'a', 'now')",
            (uid, target),
        ).lastrowid
        resp = client.post(f"/views/{vid}/delete", data={"next": target})
        assert resp.headers["Location"] == "/reports", target


def test_saved_view_needs_a_tenant_the_user_can_see(client, db, two_tenants):
    alpha, bravo = two_tenants
    make_user(db, "client", role="client", tenants=[alpha])
    login(client, "client")
    data = {"name": "x", "query": "f=forwarding/set-by:x", "tenant_id": bravo}
    assert client.post("/reports/mailboxes/views", data=data).status_code == 404


def test_library_lists_every_report_by_area_and_use(client, db, two_tenants):
    alpha, bravo = two_tenants
    make_user(db, "admin")
    login(client, "admin")
    page = client.get("/reports").get_data(as_text=True)
    for key, (title, *_rest) in data_reports.REPORTS.items():
        assert f'href="/across/{key}"' in page, key
        assert unescape(title) in unescape(page)
    assert "Alpha Ltd" in page and "Bravo Inc" in page and "App connection" in page  # search finds these too
    assert library.count() == len(data_reports.REPORTS) + len(library.SUMMARIES)
    page = client.get(f"/reports?tenant={alpha}&use=offboarding").get_data(as_text=True)
    assert f'href="/tenants/{alpha}/data/inactive"' in page and f'href="/tenants/{alpha}/data/domains"' not in page
    assert 'href="/across/inactive"' in page  # the same report across every tenant
    for use in library.USES:  # every use names reports that exist
        assert all(k in data_reports.REPORTS or k in dict((s[0], s) for s in library.SUMMARIES) for k in use.reports)


def test_library_for_a_client_shows_only_their_tenant(client, db, two_tenants):
    alpha, bravo = two_tenants
    make_user(db, "client", role="client", tenants=[alpha])
    login(client, "client")
    page = client.get(f"/reports?tenant={bravo}").get_data(as_text=True)
    assert "Bravo Inc" not in page and f"/tenants/{bravo}/" not in page
    assert f'href="/tenants/{alpha}/data/mfa"' in page
    assert "/across/" not in page and "App connection" not in page


def test_rollup_figures_open_the_tenant_rows(client, db, two_tenants):
    alpha, _ = two_tenants
    seed_tenant(db, alpha)
    make_user(db, "admin")
    login(client, "admin")
    tenants = db.execute("SELECT * FROM tenants").fetchall()
    data = rollups.build(db, tenants, "mailboxes")
    row = next(r for r in data.rows if r.tenant["id"] == alpha)
    assert row.links["c:Forwarding outside"] == (
        "mailboxes",
        "?f=forwarding%2Foutside-the-organisation%3AYes#t-forwarding",
    )
    page = unescape(client.get("/across/mailboxes").get_data(as_text=True))
    assert f'href="/tenants/{alpha}/data/mailboxes?f=forwarding%2Foutside-the-organisation%3AYes#t-forwarding"' in page
    page = client.get("/across/review").get_data(as_text=True)
    assert re.search(rf'href="/tenants/{alpha}/data/inactive#t-inactive-members"', page)


def test_no_filter_with_only_one_choice():
    report = DataReport("x", "Sample", "", {"name": "T"}, NOW)
    s = Section("All high", ["Name", "Kind"])
    s.add(["Ann", "User"], "high")
    s.add(["Bob", "User"], "high")
    report.sections = [s]
    tv = views.table_view(report, 0, views.View(), "/p")
    assert tv.flag is not None and tv.flag.options == [] and tv.filters == [] and tv.flagged_href == ""
