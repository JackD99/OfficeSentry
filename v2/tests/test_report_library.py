"""The report library page, table views (filter, columns, sort, saved views) and headline links."""

from __future__ import annotations

import csv
import io
import re
from datetime import UTC, datetime
from html import unescape

from openpyxl import load_workbook

from officesentry import roles, text
from officesentry.reports import data_reports, designs, library, rollups, views
from officesentry.reports.data_reports import DataReport, Many, Rows, Section
from tests import auditdata
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


def test_changes_sort_as_numbers_and_names_without_accents_or_case():
    report = DataReport("x", "Sample", "", {"name": "T"}, NOW)
    sites = Section("Sites", ["Site", "30-day change", "Share"])
    for row in (
        ["Ola", "+7.7 GB", "+4.6%"],
        ["Zoe", "+21.1 GB", "-1.5%"],
        ["Émile", "+936.7 MB", "No change"],
        ["ana", "No change", "+10%"],
        ["Åsa", "\u22121.2 GB", ""],
        ["Nan", "", "+0.5%"],
    ):
        sites.add(row)
    report.sections = [sites]

    def column(query: str, i: int) -> list:
        return [r[i] for r in views.apply(report, views.View.parse(query)).sections[0].rows]

    assert column("sort=sites/30-day-change:desc", 1) == [
        "+21.1 GB",
        "+7.7 GB",
        "+936.7 MB",
        "No change",
        "\u22121.2 GB",
        "",
    ]
    assert column("sort=sites/share:desc", 2) == ["+10%", "+4.6%", "+0.5%", "No change", "-1.5%", ""]
    assert column("sort=sites/site:asc", 0) == ["ana", "Åsa", "Émile", "Nan", "Ola", "Zoe"]  # "Nan" isn't a number
    tv = views.table_view(report, 0, views.View(), "/p")
    assert [c.kind for c in tv.columns] == ["text", "num", "num"]
    assert tv.columns[1].sort_href.endswith("sort=sites%2F30-day-change%3Adesc#t-sites")  # largest first
    assert [views.sort_key(v) for v in ("inf", "1e3", "1_000", "+2.2", "-3", "1,024")] == [
        "inf",
        "1e3",
        "1_000",
        2.2,
        -3.0,
        1024.0,
    ]
    assert sorted(["Zoe", "Émile", "eve", "Åsa", "ana"], key=text.fold) == ["ana", "Åsa", "Émile", "eve", "Zoe"]


def test_optional_columns_start_hidden_on_the_page_only():
    """A wide report's optional columns wait under Columns; downloads, and a filter by one, still have them."""
    report = sample()
    report.sections[0].optional = ("Roles", "Size")
    tv = views.table_view(report, 0, views.View(), "/p")
    assert [c.name for c in tv.visible] == ["Name", "Last sign-in"]
    assert tv.hidden_count == 2 and not tv.chosen  # the button counts them but isn't highlighted
    assert [len(cells) for cells, _ in tv.rows] == [2, 2, 2, 2]
    assert (
        views.apply(report, views.View.parse("sort=people/name:asc")).sections[0].columns
        == sample().sections[0].columns
    )  # downloads keep every column

    by = {c.slug: c for c in tv.columns}
    assert by["size"].toggle_href == "/p?show=people%2Fsize#t-people"
    shown = views.table_view(report, 0, views.View.parse("show=people/size"), "/p")
    assert [c.name for c in shown.visible] == ["Name", "Last sign-in", "Size"] and shown.chosen
    assert {c.slug: c for c in shown.columns}["size"].toggle_href == "/p#t-people"  # and hidden again
    everything = views.table_view(report, 0, views.View.parse(tv.show_all_href.split("?")[1].split("#")[0]), "/p")
    assert everything.hidden_count == 0

    # Filtering by one shows it; hiding it then hides it outright, and the filter stays.
    filtered = views.table_view(report, 0, views.View.parse("f=people/roles:Global+Administrator"), "/p")
    assert "Roles" in [c.name for c in filtered.visible]
    roles = {c.slug: c for c in filtered.columns}["roles"]
    hidden = views.View.parse(roles.toggle_href.split("?")[1].split("#")[0])
    assert hidden.hidden == [("people", "roles")] and hidden.filters
    assert "Roles" not in [c.name for c in views.table_view(report, 0, hidden, "/p").visible]


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


# Figures that count rows, so each opens exactly the rows it counted. (Others name a setting, or count something
# their table sums, like devices per Windows release.)
COUNTS_ROWS = {
    "admin-activity": ("Changes to review", "Admin changes", "People and apps making changes"),
    "apps": ("Third-party apps", "Apps to review", "Credentials expiring", "Every credential expired"),
    "conditional-access": ("Policies on", "MFA not required", "Legacy sign-in allowed"),
    "guests": ("Guests", "Guests to review"),
    "tenant-settings": ("Not as recommended",),
    "domains": ("Domains", "Domains to fix", "DMARC not enforced", "DKIM not signing"),
    "groups": ("Teams and groups", "Without an owner", "Only one owner"),
    "devices": ("Managed devices", "Not compliant", "Not encrypted", "Not checking in"),
    "hybrid": ("Federated domains",),
    "mfa": ("No MFA at all", "MFA not required", "Required, not set up", "Admins with a gap"),
    "admins": ("Admins without MFA",),
    "auth-methods": ("Password never expires",),
}


def test_headline_figures_count_the_rows_they_open(db, two_tenants):
    alpha, _ = two_tenants
    seed_tenant(db, alpha)
    tenant = db.execute("SELECT * FROM tenants WHERE id = ?", (alpha,)).fetchone()
    for key, labels in COUNTS_ROWS.items():
        report = data_reports.build(db, tenant, key)
        tiles = {t.label: t for t in report.tiles + report.highlights}
        assert set(labels) <= set(tiles), (key, list(tiles))
        for label in labels:
            view, _anchor = views.rows_view(report, tiles[label].rows)
            section = next(s for s in report.sections if s.title == tiles[label].rows.table)
            table = views.table_slugs(report)[report.sections.index(section)]
            shown = views._matching_rows(section, table, view, report.generated_at.date())
            assert len(shown) == tiles[label].value, (key, label)


def test_every_report_has_headline_figures(db, two_tenants):
    """All clients compares a report's headline figures, so a report without them only counts table rows."""
    alpha, _ = two_tenants
    seed_tenant(db, alpha)
    tenant = db.execute("SELECT * FROM tenants WHERE id = ?", (alpha,)).fetchone()
    missing = []
    for key in data_reports.REPORTS:
        report = data_reports.build(db, tenant, key)
        if not report.unavailable and not report.tiles and not report.highlights:
            missing.append(key)
    assert not missing


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
    assert rows[0] == ["Mailbox", "Forwards to", "Set by", "Outside the organisation", "Reaches outside", "Flag"]
    assert len(rows) == 3 and all(r[3] == "Yes" for r in rows[1:])
    wb = load_workbook(io.BytesIO(client.get(f"{base}.xlsx?{q}").data))
    assert wb["Forwarding"].max_row == 3
    assert any("Filtered view" == r[0] for r in wb["Summary"].iter_rows(values_only=True))
    # A filter that matches nothing says so and offers the way back.
    page = client.get(f"{base}?f=forwarding/set-by:Nobody").get_data(as_text=True)
    assert "No rows match this filter" in page and "Show all 3" in page


def test_a_wide_report_opens_on_its_default_columns(client, db, two_tenants):
    alpha, _ = two_tenants
    seed_tenant(db, alpha)
    make_user(db, "admin")
    login(client, "admin")
    base = f"/tenants/{alpha}/data/all-users"
    page = unescape(client.get(base).get_data(as_text=True))
    table = page[page.index('id="tbl-every-account"') :]
    table = table[: table.index("</thead>")]
    assert "Department" in table and "Job title" not in table and "Created" not in table
    assert "Downloads include the columns hidden to fit the page." in page and ">7/11<" in page
    body = client.get(f"{base}.csv?section=1").get_data(as_text=True).lstrip("﻿")
    assert "Job title" in body.splitlines()[0]  # the download has every column
    page = unescape(client.get(f"{base}?show=every-account/job-title").get_data(as_text=True))
    table = page[page.index('id="tbl-every-account"') :]
    assert "Job title" in table[: table.index("</thead>")]


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


def test_library_lists_every_report_by_area_and_use(client, db, vault, two_tenants):
    alpha, bravo = two_tenants
    auditdata.everywhere(db, vault)  # the add-on's report too
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


def test_library_lists_custom_reports_and_saved_views(client, db, two_tenants):
    """Custom reports and the person's saved views sit at the top of the Reports page, for one client or
    across all of them (a custom report then opens on the first client; a view on Every row)."""
    alpha, bravo = two_tenants
    make_user(db, "admin")
    login(client, "admin")
    page = client.get("/reports").get_data(as_text=True)
    assert "New custom report" in page and 'href="/reporting/designs"' in page and "Your views" not in page
    did = designs.create(db, "Board pack", "admin", designs.STARTERS["executive"][2], "For the board")
    client.post(
        "/reports/mailboxes/views",
        data={"name": "Leaving us", "query": "f=forwarding/outside-the-organisation:Yes", "tenant_id": alpha},
    )
    query = "f=forwarding%2Foutside-the-organisation%3AYes"
    page = client.get("/reports").get_data(as_text=True)
    assert f'href="/tenants/{alpha}/designs/{did}"' in page and "For the board" in page and "Not sent yet" in page
    assert f'href="/reporting/designs/{did}?tenant={alpha}"' in page  # Edit
    assert f'href="/across/mailboxes/rows?{query}"' in page and "Leaving us" in page
    page = client.get(f"/reports?tenant={bravo}").get_data(as_text=True)
    assert f'href="/tenants/{bravo}/designs/{did}"' in page
    assert f'href="/tenants/{bravo}/data/mailboxes?{query}"' in page
    assert "Board pack" not in client.get(f"/reports?tenant={bravo}&use=offboarding").get_data(as_text=True)
    # Someone who can't make one (an auditor) still finds the ones there are; a client account sees none.
    uid = make_user(db, "auditor", role="analyst", all_tenants=True)
    db.execute("UPDATE users SET role_id = ? WHERE id = ?", (roles.by_key(db, "auditor").id, uid))
    client.post("/logout")
    login(client, "auditor")
    page = client.get("/reports").get_data(as_text=True)
    assert "Board pack" in page and "New custom report" not in page and f"/reporting/designs/{did}" not in page
    client.post("/logout")
    make_user(db, "client", role="client", tenants=[alpha])
    login(client, "client")
    page = client.get("/reports").get_data(as_text=True)
    assert "Board pack" not in page and "Custom reports" not in page and "/reporting/designs" not in page


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
        "?f=forwarding%2Fflag%3Amedium#t-forwarding",  # active forwards outside, which the default policy blocks
    )
    page = unescape(client.get("/across/mailboxes").get_data(as_text=True))
    assert f'href="/tenants/{alpha}/data/mailboxes?f=forwarding%2Fflag%3Amedium#t-forwarding"' in page
    page = client.get("/").get_data(as_text=True)
    assert re.search(rf'href="/tenants/{alpha}/data/inactive#t-inactive-members"', page)


def test_no_filter_with_only_one_choice():
    report = DataReport("x", "Sample", "", {"name": "T"}, NOW)
    s = Section("All high", ["Name", "Kind"])
    s.add(["Ann", "User"], "high")
    s.add(["Bob", "User"], "high")
    report.sections = [s]
    tv = views.table_view(report, 0, views.View(), "/p")
    assert tv.flag is not None and tv.flag.options == [] and tv.filters == [] and tv.flagged_href == ""
