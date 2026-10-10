"""The style guide (/styleguide and /styleguide/report): staff only, every token shown, and tokens.css kept in
step with the places that can't read it (the SVG chart colours for PDFs)."""

from __future__ import annotations

import re

import pytest

from officesentry.reports import charts, export
from officesentry.reports.branding import contrast
from officesentry.web import styleguide
from tests.conftest import login, make_user


def test_staff_see_every_token(client, db):
    make_user(db, "admin")
    login(client, "admin")
    page = client.get("/styleguide").get_data(as_text=True)
    light, dark = styleguide.parse()
    assert len(light) > 60 and set(dark) <= set(light)
    for name in light:
        assert f">--{name}</code>" in page, name
    assert 'data-token="sev-critical"' in page and "Client report components" in page
    assert "4.5:1" in page  # the contrast note


def test_report_components_page(client, db):
    make_user(db, "analyst", role="analyst")
    login(client, "analyst")
    page = client.get("/styleguide/report").get_data(as_text=True)
    for part in ('class="verdict tone-critical"', 'class="verdict tone-ok"', 'class="kpi"', 'class="breakdown keep"',
                 'class="actions"', 'class="chg chg-resolved"', 'class="sev sev-info"', '<table class="data">'):
        assert part in page, part


@pytest.mark.skipif(not export.pdf_available(), reason="WeasyPrint isn't installed")
def test_report_components_pdf(client, db):
    make_user(db, "admin")
    login(client, "admin")
    resp = client.get("/styleguide/report.pdf")
    assert resp.status_code == 200 and resp.data.startswith(b"%PDF")


def test_clients_cannot_open_it(client, db, two_tenants):
    make_user(db, "client", role="client", tenants=[two_tenants[0]])
    login(client, "client")
    for path in ("/styleguide", "/styleguide/report", "/styleguide/report.pdf"):
        assert client.get(path).status_code == 403, path


def test_groups_follow_tokens_css():
    titles = [g.title for g in styleguide.groups()]
    assert titles[:2] == ["Surfaces and text", "Accent"] and "Spacing" in titles
    severity = next(g for g in styleguide.groups() if g.title.startswith("Severity"))
    assert severity.colours and {t.name for t in severity.tokens} >= {"sev-critical", "sev-critical-soft"}
    text = next(t for g in styleguide.groups() for t in g.tokens if t.name == "muted")
    assert all(r >= 4.5 for r in text.ratios())
    assert next(t for g in styleguide.groups() for t in g.tokens if t.name == "decor").ratios() is None


def test_severity_text_is_readable_on_its_own_background():
    light, dark = styleguide.parse()
    dark = {**light, **dark}
    for theme in (light, dark):
        for level in ("critical", "high", "medium", "low"):
            for background in (f"sev-{level}-soft", "surface", "bg", "sunken"):
                assert contrast(theme[f"sev-{level}"], theme[background]) >= 4.5, (level, background)


def test_pdf_chart_colours_match_the_tokens():
    """charts.py writes colours into the SVG for WeasyPrint and image viewers; they're the light tokens."""
    light, _ = styleguide.parse()
    for level in ("critical", "high", "medium", "low"):
        assert charts.STATUS_FILL[level] == light[f"sev-{level}-fill"], level
    assert charts.STATUS_FILL["ok"] == light["ok-fill"] and charts.STATUS_FILL["none"] == light["none-fill"]
    assert charts.TRACK == light["chart-track"]
    assert f'stroke="{light["chart-grid"]}"' in charts.GRID and f'stroke="{light["chart-series"]}"' in charts.SERIES
    # Bar chart series, in their fixed order, and the failure and remainder parts.
    assert [charts.TONE_FILL[t] for t in ("1", "2", "3")] == [light["chart-series"], light["chart-2"], light["chart-3"]]
    assert charts.TONE_FILL["bad"] == light["sev-critical-fill"] and charts.TONE_FILL["muted"] == light["sev-low-fill"]


def test_app_and_report_css_define_no_colour_tokens_of_their_own():
    from pathlib import Path

    static = Path(styleguide.TOKENS).parent
    for name in ("app.css", "report.css"):
        css = (static / name).read_text(encoding="utf-8")
        own = set(re.findall(r"(--[a-z0-9-]+)\s*:", css))
        assert own <= {"--brand", "--brand-ink", "--on-brand", "--brand-soft", "--brand-line", "--brand-cover-2"}, name
