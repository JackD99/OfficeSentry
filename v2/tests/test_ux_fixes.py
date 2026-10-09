"""Fixes from the October browser review: the settings menu, the month-end review screen, contrast,
long pages on phones and tagged PDFs."""

from __future__ import annotations

import re
import zlib
from datetime import UTC, datetime
from pathlib import Path

import pytest

from officesentry.reports import export
from tests.conftest import login, make_user
from tests.seed import seed_tenant

STATIC = Path(__file__).resolve().parents[1] / "officesentry" / "web" / "static"
NOW = datetime.now(UTC)
PERIOD = f"{NOW.year}-{NOW.month:02d}"
SETTINGS = ("/admin/tenants", "/admin/groups", "/admin/connections", "/admin/prices", "/admin/email", "/admin/alerts",
            "/admin/branding", "/admin/retention", "/admin/users", "/admin/activity", "/admin/status")


# -- Sidebar -----------------------------------------------------------------------------------


def test_settings_are_one_sidebar_entry_with_their_own_row(client, db):
    make_user(db, "admin")
    login(client, "admin")
    home = client.get("/").get_data(as_text=True)
    sidebar = home[home.index('<aside class="sidebar"'):home.index("</aside>")]
    assert sidebar.count(">Settings</span></a>") == 1
    assert "Client tenants" not in sidebar and "Activity log" not in sidebar
    assert 'class="settings-nav"' not in home  # only on settings pages
    for path in SETTINGS:
        page = client.get(path).get_data(as_text=True)
        nav = page[page.index('<nav class="settings-nav"'):]
        nav = nav[:nav.index("</nav>")]
        assert nav.count("<a ") == len(SETTINGS), path
        assert nav.count('aria-current="page"') == 1, path
        assert f'href="{path}" class="active"' in nav, path
        side = page[page.index('<aside class="sidebar"'):page.index("</aside>")]
        assert 'class="active" aria-current="page">' in side and side.count("aria-current") == 1, path


def test_editing_a_user_keeps_users_selected(client, db):
    make_user(db, "admin")
    other = make_user(db, "someone", role="analyst")
    login(client, "admin")
    page = client.get(f"/admin/users/{other}").get_data(as_text=True)
    assert 'href="/admin/users" class="active" aria-current="page">Users<' in page


def test_staff_who_are_not_admins_see_no_settings(client, db):
    make_user(db, "analyst", role="analyst", all_tenants=True)
    login(client, "analyst")
    page = client.get("/").get_data(as_text=True)
    assert ">Settings</span>" not in page and "settings-nav" not in page


def test_sidebar_scrolls_on_short_screens():
    css = (STATIC / "app.css").read_text(encoding="utf-8")
    rule = re.search(r"^\.sidebar \{[^}]*\}", css, re.M).group(0)
    assert "overflow-y: auto" in rule and "height: 100vh" in rule


# -- Month-end review screen ----------------------------------------------------------------------


def _review(client, db, tenant_id):
    client.post("/reporting/plans/new", data={
        "name": "Monthly", "reports": ["review"], "include_xlsx": "1", "day_of_month": "1", "hour": "8",
        "timezone": "UTC", "subject": "{client} report", "message": "Hello", "mode": "approval",
        "delivery": "email", "tenants": [str(tenant_id)]})
    return client.get(f"/reporting/{PERIOD}/{tenant_id}").get_data(as_text=True)


def test_suggestion_buttons_say_what_they_add(client, db, two_tenants):
    alpha, _ = two_tenants
    seed_tenant(db, alpha)
    make_user(db, "admin")
    login(client, "admin")
    page = _review(client, db, alpha)
    suggestions = page[page.index('<ul class="suggest-list">'):]
    suggestions = suggestions[:suggestions.index("</ul>")]
    buttons = re.findall(r"<button[^>]*>", suggestions)
    assert buttons and all('aria-label="Add: ' in b for b in buttons)


def test_approve_bar_submits_the_approve_form(client, db, two_tenants):
    alpha, _ = two_tenants
    seed_tenant(db, alpha)
    make_user(db, "admin")
    login(client, "admin")
    page = _review(client, db, alpha)
    assert 'id="approve-form"' in page
    bar = page[page.index('<div class="approve-bar">'):]
    assert 'form="approve-form"' in bar[:bar.index("</div>")]


def test_analysts_get_no_approve_bar(client, db, two_tenants):
    alpha, _ = two_tenants
    seed_tenant(db, alpha)
    make_user(db, "admin")
    login(client, "admin")
    _review(client, db, alpha)
    client.post("/logout")
    make_user(db, "analyst", role="analyst", all_tenants=True)
    login(client, "analyst")
    resp = client.get(f"/reporting/{PERIOD}/{alpha}")
    page = resp.get_data(as_text=True)
    assert resp.status_code == 200 and "An admin approves and sends it." in page
    assert "approve-bar" not in page


# -- Contrast ------------------------------------------------------------------------------------


def _tokens(block: str) -> dict[str, str]:
    return dict(re.findall(r"--([a-z0-9-]+):\s*(#[0-9a-f]{6})", block))


def _luminance(colour: str) -> float:
    def channel(c):
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (channel(int(colour[i:i + 2], 16) / 255) for i in (1, 3, 5))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _ratio(a: str, b: str) -> float:
    hi, lo = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def test_text_colours_meet_wcag_aa():
    css = (STATIC / "tokens.css").read_text(encoding="utf-8")
    light = _tokens(css[css.index(":root {"):css.index("}", css.index(":root {"))])
    dark_start = css.index(":root:not(", css.index("@media (prefers-color-scheme: dark)"))
    dark = {**light, **_tokens(css[dark_start:css.index("}", dark_start)])}
    for theme in (light, dark):
        for text in ("text", "text-2", "muted", "faint"):
            for background in ("bg", "surface", "surface-2", "sunken"):
                assert _ratio(theme[text], theme[background]) >= 4.5, (text, background, theme[text])
        for background in ("sidebar", "sidebar-active"):
            assert _ratio(theme["sidebar-muted"], theme[background]) >= 4.5, background


def test_check_matrix_squares_carry_a_letter():
    css = (STATIC / "app.css").read_text(encoding="utf-8")
    for severity, letter in (("critical", "C"), ("high", "H"), ("medium", "M"), ("low", "L")):
        assert f'.chk.fail.{severity}::after {{ content: "{letter}"; }}' in css
    assert '.chk.pass::after { content: "\\2713"; }' in css


# -- Wording -------------------------------------------------------------------------------------


def test_no_footnote_mentions_arrows(client, db, two_tenants):
    alpha, _ = two_tenants
    seed_tenant(db, alpha)
    make_user(db, "admin")
    login(client, "admin")
    for path in ("/", "/changes"):
        page = client.get(path).get_data(as_text=True)
        assert "Arrows show" not in page and "green is better, red is worse" in page, path


# -- Phones and the Changes tab -------------------------------------------------------------------


def test_every_page_loads_the_shared_scripts_once(client, db):
    make_user(db, "admin")
    login(client, "admin")
    page = client.get("/across/checks").get_data(as_text=True)  # also loads tables.js itself
    assert page.count("/static/app.js") == 1
    assert "window.officeSentryTables" in (STATIC / "tables.js").read_text(encoding="utf-8")


def test_changes_tab_groups_detail_reports_by_area(client, db, two_tenants):
    from tests.test_collectors import USERS
    from tests.test_rollups import _backdate

    alpha, _ = two_tenants
    seed_tenant(db, alpha)
    _backdate(db, alpha, 40)
    seed_tenant(db, alpha, users=[u for u in USERS if u["id"] != "u3"])
    make_user(db, "admin")
    login(client, "admin")
    page = client.get(f"/tenants/{alpha}/changes").get_data(as_text=True)
    areas = re.findall(r'<details class="card flush change-area"[^>]*>\s*<summary><h2>([^<]+?) <', page)
    assert areas and len(areas) == len(set(areas))
    assert set(areas) <= {"Identity and access", "Email", "Files and Teams", "Devices and apps",
                          "Tenant and licences", "More reports"}
    assert page.count('class="change-report"') >= len(areas)


# -- PDFs ----------------------------------------------------------------------------------------


@pytest.mark.skipif(not export.pdf_available(), reason="WeasyPrint isn't installed")
def test_pdfs_are_tagged(client, db, two_tenants):
    alpha, _ = two_tenants
    seed_tenant(db, alpha)
    make_user(db, "admin")
    login(client, "admin")
    pdf = client.get(f"/tenants/{alpha}/report.pdf").get_data()
    assert pdf.startswith(b"%PDF")
    # The catalogue may sit in a compressed object stream: inflate every stream to read it.
    parts = [pdf]
    for raw in re.findall(rb"stream\r?\n(.*?)\r?\nendstream", pdf, re.S):
        try:
            parts.append(zlib.decompress(raw))
        except zlib.error:
            pass
    text = b"".join(parts)
    assert b"/StructTreeRoot" in text and b"/Marked true" in text
    assert b"/Lang" in text


def test_long_check_cards_may_split_across_pages():
    css = (STATIC / "report.css").read_text(encoding="utf-8")
    avoid = re.search(r"^body\.pdf \.kpi, [^{]*\{ break-inside: avoid; \}", css, re.M).group(0)
    assert ".check," not in avoid
    assert "body.pdf .check { break-inside: auto; box-decoration-break: clone;" in css
