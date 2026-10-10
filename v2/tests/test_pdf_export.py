"""PDF downloads for detail reports, All tenants and What changed, and the grouped report menu.

PDFs come from WeasyPrint when it's installed. Without it (a Windows PC running from Python) the
same template is served as a print-ready page that opens the browser's print window.
"""

from __future__ import annotations

import pytest

from officesentry.reports import data_reports, export, rollups
from tests.conftest import login, make_user
from tests.seed import seed_tenant

# Windows without Docker has WeasyPrint but not Pango, so PDF buttons fall back to print-ready pages there.
needs_pdf = pytest.mark.skipif(not export.pdf_available(), reason="PDF export needs WeasyPrint with Pango")


@pytest.fixture
def no_weasyprint(monkeypatch):
    monkeypatch.setattr(export, "pdf_available", lambda: False)


def _pdf_urls(alpha: int) -> list[str]:
    return (
        [f"/tenants/{alpha}/data/{key}.pdf" for key in data_reports.REPORTS]
        + [f"/across/{key}.pdf" for key, _ in rollups.available()]
        + [
            "/changes.pdf?days=7",
            f"/tenants/{alpha}/changes.pdf?days=30",
            f"/tenants/{alpha}/report.pdf",
            f"/tenants/{alpha}/evidence.pdf",
        ]
    )


# -- Report menu -------------------------------------------------------------------


def test_every_detail_report_is_in_one_menu_group():
    keys = [k for group in data_reports.grouped() for k, _, _ in group.reports]
    assert sorted(keys) == sorted(data_reports.REPORTS)
    assert all(k in data_reports.REPORTS for _, _, group in data_reports.GROUPS for k in group), (
        "a key in GROUPS has no report: rename it there"
    )


def test_new_report_without_a_group_still_shows(monkeypatch):
    monkeypatch.setitem(data_reports.REPORTS, "zz-new", ("New report", "Something new.", (), lambda *a: None))
    last = data_reports.grouped()[-1]
    assert last.key == "more" and last.reports == [("zz-new", "New report", "Something new.")]


def test_detail_page_shows_its_group_and_downloads(client, db, two_tenants):
    alpha, _ = two_tenants
    seed_tenant(db, alpha)
    make_user(db, "admin")
    login(client, "admin")
    page = client.get(f"/tenants/{alpha}/data/inbox-rules").get_data(as_text=True)
    # The report picker lists every group, the open report marked; phones get it as a grouped select.
    picker = page.split('<nav class="report-menu"')[1].split("</nav>")[0]
    assert ">Email</p>" in picker and "Mailbox size" in picker and "MFA coverage" in picker
    assert f'href="/tenants/{alpha}/data/inbox-rules" title="' in picker
    assert 'aria-current="page">Inbox rules<' in picker
    assert f'/tenants/{alpha}/data/inbox-rules.pdf"' in page
    assert f'/tenants/{alpha}/data/inbox-rules.xlsx"' in page
    assert '<optgroup label="Identity and access">' in page


def test_detail_reports_tab_opens_the_first_report_or_the_picked_one(client, db, two_tenants):
    alpha, bravo = two_tenants
    make_user(db, "client", role="client", tenants=[alpha])
    login(client, "client")
    first = data_reports.grouped()[0].reports[0][0]
    assert client.get(f"/tenants/{alpha}/data").headers["Location"].endswith(f"/tenants/{alpha}/data/{first}")
    resp = client.get(f"/tenants/{alpha}/data?key=devices")
    assert resp.headers["Location"].endswith(f"/tenants/{alpha}/data/devices")
    assert client.get(f"/tenants/{alpha}/data?key=../admin").headers["Location"].endswith(f"/data/{first}")
    assert client.get(f"/tenants/{bravo}/data").status_code == 404


# -- PDFs ---------------------------------------------------------------------------


def test_every_pdf_download_works_without_weasyprint(client, db, two_tenants, no_weasyprint):
    """No WeasyPrint: each PDF link gives the print-ready page, named for Save as PDF."""
    alpha, _ = two_tenants
    seed_tenant(db, alpha)
    make_user(db, "admin")
    login(client, "admin")
    for url in _pdf_urls(alpha):
        resp = client.get(url)
        assert resp.status_code == 200 and resp.mimetype == "text/html", url
        page = resp.get_data(as_text=True)
        assert 'class="report pdf print-view"' in page and "print.js" in page, url
        assert "Save as PDF" in page and "<style>" not in page, url  # the CSP blocks inline styles
    page = client.get(f"/tenants/{alpha}/data/mfa.pdf").get_data(as_text=True)
    assert "<title>mfa-alpha-ltd</title>" in page


@needs_pdf
def test_pdf_downloads_render(client, db, two_tenants):
    alpha, _ = two_tenants
    seed_tenant(db, alpha)
    make_user(db, "admin")
    login(client, "admin")
    for url in (
        f"/tenants/{alpha}/data/admins.pdf",
        f"/tenants/{alpha}/data/secure-score.pdf",
        "/across/review.pdf",
        "/across/inbox-rules.pdf",
        "/changes.pdf",
        f"/tenants/{alpha}/changes.pdf",
    ):
        resp = client.get(url)
        assert resp.status_code == 200 and resp.data[:4] == b"%PDF", url
        assert resp.headers["Content-Disposition"].startswith("attachment; filename="), url
    assert (
        'filename="admins-alpha-ltd.pdf"'
        in client.get(f"/tenants/{alpha}/data/admins.pdf").headers["Content-Disposition"]
    )


@needs_pdf
def test_every_detail_report_renders_as_pdf_with_no_data(client, db, two_tenants):
    """Before any collection every report is 'not available'; its PDF still renders."""
    alpha, _ = two_tenants
    make_user(db, "admin")
    login(client, "admin")
    for key in data_reports.REPORTS:
        resp = client.get(f"/tenants/{alpha}/data/{key}.pdf")
        assert resp.status_code == 200 and resp.data[:4] == b"%PDF", key


def test_pdf_detail_matches_the_page(client, db, two_tenants, no_weasyprint):
    alpha, _ = two_tenants
    seed_tenant(db, alpha)
    make_user(db, "admin")
    login(client, "admin")
    pdf = client.get(f"/tenants/{alpha}/data/admins.pdf").get_data(as_text=True)
    report = data_reports.build(db, db.execute("SELECT * FROM tenants WHERE id = ?", (alpha,)).fetchone(), "admins")
    for section in report.sections:
        assert section.title in pdf
    for tile in report.tiles + report.highlights:
        assert tile.label in pdf


def test_pdfs_respect_tenant_access(client, db, two_tenants, no_weasyprint):
    alpha, bravo = two_tenants
    make_user(db, "client", role="client", tenants=[alpha])
    login(client, "client")
    assert client.get(f"/tenants/{alpha}/data/mfa.pdf").status_code == 200
    assert client.get(f"/tenants/{alpha}/changes.pdf").status_code == 200
    for url in (f"/tenants/{bravo}/data/mfa.pdf", f"/tenants/{bravo}/changes.pdf", f"/tenants/{alpha}/data/nope.pdf"):
        assert client.get(url).status_code == 404, url
    # Cross-tenant PDFs are for staff only.
    for url in ("/across/review.pdf", "/changes.pdf"):
        assert client.get(url).status_code == 403, url


def test_pdf_downloads_are_audited(client, db, two_tenants, no_weasyprint):
    alpha, _ = two_tenants
    make_user(db, "admin")
    login(client, "admin")
    client.get(f"/tenants/{alpha}/data/mfa.pdf")
    client.get("/across/licences.pdf")
    client.get("/changes.pdf?days=90")
    details = {r["detail"] for r in db.execute("SELECT detail FROM audit_log")}
    assert {"mfa.pdf", "licences.pdf", "changes-90d.pdf"} <= details
