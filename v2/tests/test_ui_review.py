"""Page behaviours from the second UI review: shortcuts, empty states, alignment and wording."""

from __future__ import annotations

from types import SimpleNamespace

from officesentry.reports import data_reports
from officesentry.reports.rollups import Column, RollupRow
from officesentry.web.filters import figure_columns
from tests import auditdata, autodeskdata
from tests.conftest import login, make_user
from tests.seed import seed_tenant


def test_tenants_page_opens_and_downloads_each_monthly_review(client, db, two_tenants):
    alpha, bravo = two_tenants
    seed_tenant(db, alpha)
    db.execute("UPDATE tenants SET consented_at = NULL WHERE id = ?", (bravo,))
    make_user(db, "admin")
    login(client, "admin")
    page = client.get("/").get_data(as_text=True)
    assert f'href="/tenants/{alpha}/report.pdf"' in page and f'href="/tenants/{alpha}"' in page  # PDF, overview
    assert f'href="/tenants/{bravo}/report.pdf"' not in page  # nothing to download before consent
    assert "across 1 client with a score" in page and "1 up to date" in page
    assert client.get(f"/tenants/{alpha}/report.pdf").status_code == 200


def test_overview_without_data_says_so_once(client, db, two_tenants):
    alpha, bravo = two_tenants
    seed_tenant(db, alpha)
    make_user(db, "admin")
    login(client, "admin")
    empty = client.get(f"/tenants/{bravo}").get_data(as_text=True)
    assert "No data yet" in empty
    assert "<h2>Needs attention</h2>" not in empty and "Since " not in empty and "Not collected yet" not in empty
    full = client.get(f"/tenants/{alpha}").get_data(as_text=True)
    assert "No data yet" not in full and "<h2>Needs attention</h2>" in full
    assert f'href="/tenants/{alpha}/changes"' in full  # the Since card leads to the Changes tab


def test_rollup_figures_line_up_with_numbers():
    rows = [
        RollupRow({"id": 1}, values={"a": "1 of 3", "b": "Yes", "c": "3.0 GB", "d": "42%"}),
        RollupRow({"id": 2}, values={"a": "0 of 0", "b": "No", "c": "", "d": "7%"}),
        RollupRow({"id": 3}, note="No data"),
    ]
    rollup = SimpleNamespace(columns=[Column(k, k, "text") for k in "abcd"] + [Column("n", "n")], rows=rows)
    assert figure_columns(rollup) == {"a", "c", "d"}


def test_schedule_form_folds_detail_reports_by_group(client, db, vault, two_tenants):
    alpha, _ = two_tenants
    auditdata.everywhere(db, vault)  # the add-ons' reports too
    autodeskdata.everywhere(db, vault)
    make_user(db, "admin")
    login(client, "admin")
    page = client.get(f"/tenants/{alpha}/schedules/new").get_data(as_text=True)
    assert page.count('<details class="report-choices"') == len(data_reports.grouped())
    for group in data_reports.grouped():
        assert f'<span class="strong">{group.title}</span>' in page
    for key in data_reports.REPORTS:
        assert f'value="data:{key}"' in page
    assert "Add a contact at Alpha Ltd" in page
    # A group with a chosen report opens and says so.
    page = client.post(
        f"/tenants/{alpha}/schedules/new",
        data={"reports": ["data:mailboxes"], "frequency": "monthly", "day_of_month": "1", "hour": "8"},
    ).get_data(as_text=True)
    assert '<details class="report-choices" open>' in page and "1 chosen" in page


def test_missing_pages_explain_and_offer_a_way_on(client, db, two_tenants):
    make_user(db, "analyst", "analyst")
    login(client, "analyst")
    resp = client.get(f"/tenants/{two_tenants[0]}")  # not granted: indistinguishable from missing
    page = resp.get_data(as_text=True)
    assert resp.status_code == 404 and "belongs to a client you haven" in page
    assert 'href="/reports"' in page and 'href="/"' in page
    missing = client.get("/no-such-page")
    assert missing.status_code == 404 and "belongs to a client you haven" in missing.get_data(as_text=True)
    client.post("/logout")
    assert 'href="/login"' in client.get("/no-such-page").get_data(as_text=True)


def test_findings_can_be_searched_and_downloaded(client, db, two_tenants):
    alpha, _ = two_tenants
    seed_tenant(db, alpha)
    make_user(db, "admin")
    login(client, "admin")
    page = client.get(f"/tenants/{alpha}/findings").get_data(as_text=True)
    assert 'data-filter="tbl-open-findings"' in page and 'id="tbl-open-findings"' in page
    assert f'href="/tenants/{alpha}/findings.pdf"' in page
    # Each check's heading carries its rows' words, so a search for a person keeps the heading.
    heading = page.split('<tr class="group-head" data-name="')[1].split('"')[0]
    row = page.split('class="group-head"')[1].split('<tr class="flag-')[1].split('data-name="')[1].split('"')[0]
    assert row.split(" ")[1] in heading
    assert client.get(f"/tenants/{alpha}/findings.csv").status_code == 200


def test_users_tab_counts_read_naturally(client, db, two_tenants):
    alpha, _ = two_tenants
    seed_tenant(db, alpha)
    make_user(db, "admin")
    login(client, "admin")
    page = client.get(f"/tenants/{alpha}/users").get_data(as_text=True)
    assert "2 members, 1 guest<" in page and "1 guests" not in page


def test_changes_use_the_same_labels_on_overview_and_changes_tab(client, db, two_tenants):
    from tests.test_collectors import USERS
    from tests.test_rollups import _backdate

    alpha, _ = two_tenants
    seed_tenant(db, alpha)
    _backdate(db, alpha, 40)
    seed_tenant(db, alpha, users=[u for u in USERS if u["id"] != "u3"])
    make_user(db, "admin")
    login(client, "admin")
    overview = client.get(f"/tenants/{alpha}").get_data(as_text=True)
    changes = client.get(f"/tenants/{alpha}/changes").get_data(as_text=True)
    for page in (overview, changes):
        assert '<span class="chg chg-removed">Gone</span>' in page
        assert ">removed<" not in page
    assert "<caption>" in changes
