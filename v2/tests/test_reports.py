from datetime import UTC, datetime, timedelta

from openpyxl import load_workbook

from officesentry import store
from officesentry.reports.export import pdf_available
from officesentry.reports.monthly_review import build
from tests.conftest import login, make_user
from tests.seed import seed_tenant


def test_build_report(db, two_tenants):
    alpha, _ = two_tenants
    seed_tenant(db, alpha)
    report = build(db, store.get_tenant(db, alpha))
    kpis = {k.key: k for k in report.kpis}
    assert kpis["secure_score.pct"].value == 42
    assert kpis["mfa.registered_pct"].value == 33.3
    assert report.failing[0].severity == "critical"  # most severe first
    assert report.counts["critical"] == 1
    assert "<svg" in report.secure_score_chart  # Microsoft's daily history gives 2 points
    assert all(not f.stale for f in report.freshness)


def test_kpi_delta_against_four_weeks_ago(db, two_tenants):
    alpha, _ = two_tenants
    old = (datetime.now(UTC) - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    db.execute("INSERT INTO metrics (run_id, tenant_id, key, value, captured_at) VALUES (NULL, ?, ?, ?, ?)",
               (alpha, "secure_score.pct", 35.0, old))
    seed_tenant(db, alpha)
    k = {k.key: k for k in build(db, store.get_tenant(db, alpha)).kpis}["secure_score.pct"]
    assert (k.previous, k.delta, k.trend) == (35.0, 7.0, "better")


def test_report_pages(client, db, two_tenants):
    alpha, bravo = two_tenants
    seed_tenant(db, alpha)
    make_user(db, "admin")
    login(client, "admin")
    page = client.get(f"/tenants/{alpha}/report").get_data(as_text=True)
    assert "Monthly Security &amp; Licensing Review" in page and "Alpha Ltd" in page
    assert "Every admin has MFA registered" in page
    xlsx = client.get(f"/tenants/{alpha}/report.xlsx")
    wb = load_workbook(__import__("io").BytesIO(xlsx.data))
    assert wb.sheetnames == ["Summary", "Checks", "Findings", "Licences", "Data freshness"]
    overview = client.get("/reports").get_data(as_text=True)
    assert "Alpha Ltd" in overview and "Bravo Inc" in overview
    # A tenant with no data still renders (everything "not checked").
    assert client.get(f"/tenants/{bravo}/report").status_code == 200


def test_pdf_export(client, db, two_tenants):
    if not pdf_available():
        return
    alpha, _ = two_tenants
    seed_tenant(db, alpha)
    make_user(db, "admin")
    login(client, "admin")
    resp = client.get(f"/tenants/{alpha}/report.pdf")
    assert resp.status_code == 200 and resp.data[:4] == b"%PDF"


def test_client_sees_only_own_report(client, db, two_tenants):
    alpha, bravo = two_tenants
    make_user(db, "client", role="client", tenants=[alpha])
    login(client, "client")
    assert client.get(f"/tenants/{alpha}/report").status_code == 200
    for url in (f"/tenants/{bravo}/report", f"/tenants/{bravo}/report.xlsx", f"/tenants/{bravo}/report.pdf"):
        assert client.get(url).status_code == 404
    overview = client.get("/reports").get_data(as_text=True)
    assert "Alpha Ltd" in overview and "Bravo Inc" not in overview
    assert client.get("/admin/prices").status_code == 403


def test_prices_page(client, db, two_tenants):
    alpha, _ = two_tenants
    seed_tenant(db, alpha)
    make_user(db, "admin")
    login(client, "admin")
    assert "Microsoft 365 Business Premium" in client.get("/admin/prices").get_data(as_text=True)
    client.post("/admin/prices", data={"currency": "USD", "price:SPB": "22", "price:OLD": "-5"})
    assert db.execute("SELECT monthly_price FROM sku_prices WHERE sku_part_number = 'SPB'").fetchone()[0] == 22
    assert db.execute("SELECT 1 FROM sku_prices WHERE sku_part_number = 'OLD'").fetchone() is None
    assert db.execute("SELECT value FROM settings WHERE key = 'currency'").fetchone()[0] == "USD"
    report = build(db, store.get_tenant(db, alpha))
    assert report.licences.monthly_waste == 22 * (3 + 1)  # 3 unassigned + Bob (disabled)
