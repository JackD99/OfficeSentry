"""MSP branding beyond reports: portal shell, sign-in pages, favicon, client home, report footer, email sender."""

import base64

from officesentry import mailer
from officesentry.reports import branding, export
from tests.conftest import login, make_user
from tests.seed import seed_tenant
from tests.test_email import fake_smtp, sends  # noqa: F401  (autouse: replaces smtplib)

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
)
JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00\xff\xd9"


def _brand(db, **values):
    for key, value in values.items():
        db.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (f"brand_{key}", value),
        )


def test_sign_in_page_says_office_sentry_until_branding_is_set_up(client, db):
    make_user(db, "admin")
    page = client.get("/login").get_data(as_text=True)
    assert "<title>Sign in · Office Sentry</title>" in page
    assert "Office Sentry</a>" in page and "Powered by" not in page
    assert "/brand.css" not in page and 'class="branded"' not in page
    assert 'rel="icon" href="/brand/logo?v=' in page


def test_sign_in_and_code_pages_show_the_msp_name_logo_and_colour(client, db):
    make_user(db, "admin")
    _brand(db, name="Harbour IT", logo=branding.logo_data_uri(PNG), accent="#0d7a5f")
    page = client.get("/login").get_data(as_text=True)
    assert "<title>Sign in · Harbour IT</title>" in page
    assert '<img src="/brand/logo?v=' in page and 'alt="Harbour IT"' in page
    assert "reports from Harbour IT" in page and "Powered by Office Sentry" in page
    assert 'class="branded"' in page and "/brand.css?v=" in page
    assert "data:image/png" not in page  # the logo is linked, not inlined into every page
    css = client.get("/brand.css")
    assert css.status_code == 200 and "--brand:#0d7a5f" in css.get_data(as_text=True)
    assert "--brand-ink-dark:" in css.get_data(as_text=True)


def test_portal_shell_uses_the_msp_name(client, db):
    make_user(db, "admin")
    _brand(db, name="Harbour IT")
    login(client, "admin")
    page = client.get("/admin/branding").get_data(as_text=True)
    assert "<title>Branding · Harbour IT</title>" in page
    assert "Harbour IT</a>" in page and "Powered by Office Sentry" in page


def test_favicon_is_the_logo_or_the_default_icon_and_needs_no_sign_in(client, db):
    for url in ("/favicon.ico", "/brand/logo"):
        resp = client.get(url)
        assert resp.status_code == 200 and resp.mimetype == "image/svg+xml"
        assert resp.data.startswith(b"<svg")
    _brand(db, logo=branding.logo_data_uri(PNG))
    resp = client.get("/favicon.ico")
    assert resp.mimetype == "image/png" and resp.data == PNG
    assert resp.headers["X-Content-Type-Options"] == "nosniff"
    assert "no-store" not in resp.headers["Cache-Control"]
    _brand(db, logo=branding.logo_data_uri(JPEG))
    resp = client.get("/brand/logo")
    assert resp.mimetype == "image/jpeg" and resp.data == JPEG


def test_logo_route_serves_nothing_else(client, db):
    _brand(db, logo="data:text/html;base64," + base64.b64encode(b"<script>x</script>").decode())
    resp = client.get("/brand/logo")
    assert resp.mimetype == "image/svg+xml" and b"script" not in resp.data  # a bad value falls back to the icon
    for url in ("/brand/logo/../app.css", "/brand/logo/x", "/brand/app.css", "/favicon.ico/x"):
        assert client.get(url).status_code in (302, 404)
    # Everything else still needs signing in.
    assert client.get("/admin/branding").status_code == 302


def test_branding_page_saves_support_contact_and_report_footer(client, db):
    make_user(db, "admin")
    login(client, "admin")
    resp = client.post(
        "/admin/branding",
        data={
            "name": "Harbour IT",
            "accent": "#0d7a5f",
            "support_name": "Service desk",
            "support_email": "help@harbour.it",
            "support_phone": "01234 567890",
            "report_footer": "For information only.\n  Not an audit.",
        },
    )
    assert resp.status_code == 302
    brand = branding.load(db)
    assert (brand.support_name, brand.support_email, brand.support_phone) == (
        "Service desk",
        "help@harbour.it",
        "01234 567890",
    )
    assert brand.report_footer == "For information only. Not an audit."
    client.post("/admin/branding", data={"support_email": "not an email"})
    assert branding.load(db).support_email == "help@harbour.it"
    page = client.get("/admin/branding").get_data(as_text=True)
    assert 'value="Service desk"' in page and "For information only. Not an audit.</textarea>" in page


def test_branding_page_is_admin_only(client, db, two_tenants):
    alpha, _ = two_tenants
    make_user(db, "client", role="client", tenants=[alpha])
    login(client, "client")
    assert client.post("/admin/branding", data={"name": "Evil"}).status_code == 403
    assert branding.load(db).name == ""


def test_client_home_leads_with_the_report_then_recommendations_then_contact(client, db, two_tenants):
    alpha, bravo = two_tenants
    seed_tenant(db, alpha)
    seed_tenant(db, bravo)
    _brand(
        db,
        name="Harbour IT",
        support_name="Harbour service desk",
        support_email="help@harbour.it",
        support_phone="01234 567890",
    )
    make_user(db, "client", role="client", tenants=[alpha])
    login(client, "client")
    page = client.get("/").get_data(as_text=True)
    report, recs, contact = (
        page.index(f'/tenants/{alpha}/report"'),
        page.index("<h2>Recommendations</h2>"),
        page.index("Questions about your reports?"),
    )
    assert report < recs < contact
    assert f'/tenants/{alpha}/report.pdf"' in page and "Based on data collected" in page
    assert "Harbour service desk" in page and 'href="mailto:help@harbour.it"' in page
    assert 'href="tel:01234567890"' in page
    assert ">Open</span>" in page or ">New</span>" in page  # each recommendation shows its status
    assert "Alpha Ltd" in page and "Bravo Inc" not in page and f"/tenants/{bravo}/" not in page


def test_client_home_without_support_details_has_no_contact_card(client, db, two_tenants):
    alpha, _ = two_tenants
    make_user(db, "client", role="client", tenants=[alpha])
    login(client, "client")
    page = client.get("/").get_data(as_text=True)
    assert "Questions about your reports?" not in page and "Monthly review" in page


def test_report_footer_prints_on_client_reports(client, db, two_tenants, monkeypatch):
    monkeypatch.setattr(export, "pdf_available", lambda: False)  # PDF routes return their print-ready HTML
    alpha, _ = two_tenants
    seed_tenant(db, alpha)
    make_user(db, "admin")
    login(client, "admin")
    assert 'class="report-footer"' not in client.get(f"/tenants/{alpha}/report").get_data(as_text=True)
    _brand(db, report_footer="Provided for information only & not an audit.")
    for url in (f"/tenants/{alpha}/report", f"/tenants/{alpha}/report.pdf", f"/tenants/{alpha}/data/devices.pdf"):
        page = client.get(url).get_data(as_text=True)
        assert '<footer class="report-footer">Provided for information only &amp; not an audit.</footer>' in page


def test_email_sender_name_falls_back_to_the_brand_name(db, vault):
    for key, value in {"smtp_host": "smtp.example.com", "smtp_from": "r@msp.com", "smtp_security": "none"}.items():
        db.execute("INSERT INTO settings (key, value) VALUES (?, ?)", (key, value))
    email = mailer.Email(to=["it@client.com"], subject="Hi", text="Hello")
    mailer.send(db, vault, email)
    assert sends()[-1][2]["From"] == "r@msp.com"
    _brand(db, name="Harbour IT")
    mailer.send(db, vault, email)
    assert sends()[-1][2]["From"] == "Harbour IT <r@msp.com>"
    db.execute("INSERT INTO settings (key, value) VALUES ('msp_name', 'Harbour Reports')")
    mailer.send(db, vault, email)
    assert sends()[-1][2]["From"] == "Harbour Reports <r@msp.com>"
