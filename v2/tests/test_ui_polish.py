"""Small page behaviours from the UI review: clickable headline numbers, compact empty tables, coloured flags."""

from __future__ import annotations

from io import BytesIO

from openpyxl import load_workbook

from officesentry.reports import data_reports
from tests.conftest import login, make_user
from tests.seed import seed_tenant


def test_overview_numbers_open_their_reports(client, db, two_tenants):
    alpha, _ = two_tenants
    seed_tenant(db, alpha)
    make_user(db, "admin")
    login(client, "admin")
    page = client.get(f"/tenants/{alpha}").get_data(as_text=True)
    for key in ("secure-score", "mfa", "admins", "licences", "inactive"):
        assert f'class="tile link" href="/tenants/{alpha}/data/{key}"' in page, key


def test_empty_tables_are_one_line(client, db, two_tenants):
    alpha, _ = two_tenants
    seed_tenant(db, alpha)
    make_user(db, "admin")
    login(client, "admin")
    tenant = db.execute("SELECT * FROM tenants WHERE id = ?", (alpha,)).fetchone()
    report = data_reports.build(db, tenant, "mfa")
    empty = [s for s in report.sections if not s.rows]
    assert empty, "the seeded MFA report has an empty table"
    page = client.get(f"/tenants/{alpha}/data/mfa").get_data(as_text=True)
    assert page.count('class="card flush empty-section"') == len(empty)
    assert empty[0].empty in page


def test_xlsx_flags_are_coloured(db, two_tenants):
    alpha, _ = two_tenants
    seed_tenant(db, alpha)
    tenant = db.execute("SELECT * FROM tenants WHERE id = ?", (alpha,)).fetchone()
    report = data_reports.build(db, tenant, "admins")
    wb = load_workbook(BytesIO(data_reports.to_xlsx(report)))
    flagged = [c for ws in wb.worksheets for row in ws.iter_rows(min_row=2) for c in row[-1:]
               if c.value in data_reports.FLAG_FILLS]
    assert flagged
    for cell in flagged:
        assert cell.fill.fgColor.rgb.endswith(data_reports.FLAG_FILLS[cell.value][0])


def test_reports_not_compared_are_grouped_by_reason(client, db, two_tenants):
    alpha, _ = two_tenants
    seed_tenant(db, alpha)  # collected today only: nothing from 30 days ago
    make_user(db, "admin")
    login(client, "admin")
    page = client.get(f"/tenants/{alpha}/changes").get_data(as_text=True)
    assert page.count("Not compared (no data from the start of the period to compare with)") == 1
