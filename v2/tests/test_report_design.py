"""Client report design: MSP branding, plain-English summaries and the shared report layout."""

import base64
import io
import re

from officesentry import store
from officesentry.reports import branding, charts, evidence, summary
from officesentry.reports.export import pdf_available
from officesentry.reports.monthly_review import build
from tests.conftest import login, make_user
from tests.seed import seed_tenant

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
)


def test_brand_defaults_and_colour_safety(db):
    b = branding.load(db)
    assert (b.display_name, b.accent, b.is_default) == ("Office Sentry", branding.DEFAULT_ACCENT, True)
    # A pale accent gets dark text on the cover and a darkened shade for text on white.
    yellow = branding.Brand(accent="#ffd400")
    assert yellow.on_accent == "#111827"
    ink = yellow.css.split("--brand-ink:")[1][:7]
    assert branding.contrast(ink, "#ffffff") >= 4.5
    assert branding.Brand(accent="#0d3b66").on_accent == "#ffffff"
    # A bad stored value never reaches the CSS.
    db.execute("INSERT INTO settings (key, value) VALUES ('brand_accent', 'red;}body{x')")
    assert branding.load(db).accent == branding.DEFAULT_ACCENT


def test_logo_validation():
    assert branding.logo_data_uri(PNG).startswith("data:image/png;base64,")
    assert branding.logo_data_uri(b"\xff\xd8\xff\xe0rest").startswith("data:image/jpeg;base64,")
    assert branding.logo_data_uri(b"<svg onload='alert(1)'/>") is None
    assert branding.logo_data_uri(PNG + b"\0" * branding.MAX_LOGO_BYTES) is None
    assert branding.logo_data_uri(b"") is None


def test_branding_settings_page(client, db, two_tenants):
    make_user(db, "analyst", role="analyst", all_tenants=True)
    login(client, "analyst")
    assert client.get("/admin/branding").status_code == 403
    client.post("/logout")
    make_user(db, "admin")
    login(client, "admin")
    assert client.get("/admin/branding").status_code == 200

    resp = client.post(
        "/admin/branding",
        data={
            "name": "Harbour IT",
            "contact": "help@harbourit.example",
            "accent": "#0D7A5F",
            "logo": (io.BytesIO(PNG), "logo.png"),
        },
        content_type="multipart/form-data",
    )
    assert resp.status_code == 302
    b = branding.load(db)
    assert (b.name, b.contact, b.accent) == ("Harbour IT", "help@harbourit.example", "#0d7a5f")
    assert b.logo.startswith("data:image/png")

    bad = client.post("/admin/branding", data={"name": "X", "accent": "not-a-colour"})
    assert bad.status_code == 302 and branding.load(db).name == "Harbour IT"
    svg = client.post(
        "/admin/branding",
        data={"name": "X", "accent": "#000000", "logo": (io.BytesIO(b"<svg/>"), "logo.svg")},
        content_type="multipart/form-data",
    )
    assert svg.status_code == 302 and branding.load(db).name == "Harbour IT"  # rejected as a whole

    client.post("/admin/branding", data={"name": "Harbour IT", "accent": "#0d7a5f", "remove_logo": "1"})
    assert branding.load(db).logo == ""
    assert db.execute("SELECT COUNT(*) FROM audit_log WHERE action = 'branding_updated'").fetchone()[0] == 2


def test_reports_carry_the_brand(client, db, two_tenants):
    alpha, _ = two_tenants
    seed_tenant(db, alpha)
    for key, value in (
        ("brand_name", "Harbour IT"),
        ("brand_accent", "#0d7a5f"),
        ("brand_contact", "help@harbourit.example"),
    ):
        db.execute("INSERT INTO settings (key, value) VALUES (?, ?)", (key, value))
    make_user(db, "client", role="client", tenants=[alpha])
    login(client, "client")
    page = client.get(f"/tenants/{alpha}/report").get_data(as_text=True)
    assert "Prepared by" in page and "Harbour IT" in page
    assert "/brand.css?v=" in page and "<style" not in page  # the page's CSP allows no inline styles
    assert "What we recommend next" in page and "Key figures" in page
    css = client.get("/brand.css")
    assert css.mimetype == "text/css" and "--brand:#0d7a5f" in css.get_data(as_text=True)
    if pdf_available():
        resp = client.get(f"/tenants/{alpha}/report.pdf")
        assert resp.status_code == 200 and resp.data.startswith(b"%PDF")


def test_brand_css_is_public_but_only_colours(client, db):
    # The sign-in page is branded, so the stylesheet is served before sign-in. It holds colours and nothing else.
    db.execute("INSERT INTO settings (key, value) VALUES ('brand_name', 'Harbour IT'), ('brand_contact', 'x@y.z')")
    resp = client.get("/brand.css")
    assert resp.status_code == 200 and resp.mimetype == "text/css"
    css = resp.get_data(as_text=True)
    assert "Harbour" not in css and "x@y.z" not in css
    assert set(re.findall(r"--[\w-]+:([^;}]+)", css)) <= set(re.findall(r"#[0-9a-f]{6}", css))


def test_monthly_summary_reads_plainly(db, two_tenants):
    alpha, _ = two_tenants
    seed_tenant(db, alpha)
    report = build(db, store.get_tenant(db, alpha))
    es = summary.monthly(report)
    assert es.verdict.tone == "critical" and es.verdict.label == "Action needed"
    assert es.verdict.headline.startswith("Alpha Ltd's Microsoft 365")
    assert es.paragraphs[0].startswith(f"We ran {len(report.results) - len(report.unavailable)} security checks")
    assert "1 critical" in es.paragraphs[0]
    assert 1 <= len(es.actions) <= 3 and es.actions[0].severity == "critical"
    assert es.actions[0].anchor == f"check-{report.failing[0].key}"


def test_monthly_summary_when_all_pass():
    class R:
        tenant = {"name": "Tidy Co"}
        failing, results, unavailable, kpis, resolved, changes = [], ["mfa_enforced"], [], [], [], []
        new_count, period_start, currency = 0, None, "GBP"

        class licences:
            lines, monthly_waste = [], 0

    es = summary.monthly(R())
    assert es.verdict.tone == "ok" and "passed every security check" in es.verdict.headline
    assert es.paragraphs[0] == "We ran 1 security check and it passed."
    assert es.actions == []

    R.results = []  # nothing collected yet: never "all 0 checks passed"
    es = summary.monthly(R())
    assert es.verdict.label == "Not checked yet" and not any("passed" in p for p in es.paragraphs)

    # Most checks couldn't run (only the tenant profile collected so far): never "in good shape".
    R.results, R.unavailable = ["mfa_enforced", "admin_count"] + ["x"] * 59, ["x"] * 59
    es = summary.monthly(R())
    assert es.verdict.label == "Partly checked" and es.verdict.tone == "medium"
    assert (
        es.verdict.headline == "Only 2 of 61 security checks could run for Tidy Co, so this isn't the full picture yet."
    )


def test_a_figure_that_is_missing_says_why(db, two_tenants):
    alpha, bravo = two_tenants
    seed_tenant(db, alpha)
    stale = {k.key: k for k in build(db, store.get_tenant(db, bravo)).kpis}["stale"]
    assert stale.value is None and stale.note == "User data hasn't been collected yet"  # not "Needs Entra ID P1"
    assert {k.key: k for k in build(db, store.get_tenant(db, alpha)).kpis}["stale"].note == ""


def test_evidence_summary(db, two_tenants):
    alpha, _ = two_tenants
    seed_tenant(db, alpha)
    pack = evidence.build(db, store.get_tenant(db, alpha))
    es = summary.evidence(pack)
    assert es.paragraphs[0].startswith(f"Of the {len(pack.controls)} controls")
    assert "backups" in es.paragraphs[1]
    assert all(a.anchor.startswith("control-") for a in es.actions)


def test_status_bar_and_meter():
    bar = charts.stacked_bar([("Critical", 1, "critical"), ("High", 0, "high"), ("Low", 3, "low")], label="Checks")
    assert bar.count("<rect") == 2 and "Critical: 1" in bar and 'aria-label="Checks"' in bar
    assert "track" in charts.stacked_bar([("High", 0, "high")], label="None")
    assert charts.meter(None) == "" and 'width="100.0"' in charts.meter(50, width=200)
