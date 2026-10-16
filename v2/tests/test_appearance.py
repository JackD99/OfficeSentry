"""Appearance under Account (web/appearance.py, static/themes.css, migration 0047): light, dark or the
device's setting for everyone, a colour theme for staff, and nothing of it on what clients receive."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from officesentry.reports.branding import contrast
from officesentry.web import appearance
from tests.conftest import login, make_user

STATIC = Path(__file__).resolve().parents[1] / "officesentry" / "web" / "static"
SURFACES = ("bg", "surface", "surface-2", "sunken")


def _decls(block: str) -> dict[str, str]:
    block = re.sub(r"/\*.*?\*/", "", block, flags=re.S)
    return dict(re.findall(r"--([a-z0-9-]+)\s*:\s*([^;]+?)\s*;", block))


def _block(css: str, selector: str) -> dict[str, str]:
    start = css.index(selector)
    return _decls(css[css.index("{", start) : css.index("}", start)])


def _tokens() -> tuple[dict[str, str], dict[str, str], dict[str, str]]:
    """tokens.css's light block, its dark block for a dark device, and its block for someone who chose Dark."""
    css = (STATIC / "tokens.css").read_text(encoding="utf-8")
    media = css.index("@media (prefers-color-scheme: dark)")
    return (
        _block(css, ":root {"),
        _block(css, ':root:not([data-theme="light"])'),
        _block(css[media:], ':root[data-theme="dark"]'),
    )


def _themes() -> dict[str, tuple[dict[str, str], dict[str, str], dict[str, str]]]:
    css = (STATIC / "themes.css").read_text(encoding="utf-8")
    out = {}
    for key in appearance.THEMES:
        light = _block(css, f'\n[data-accent="{key}"]')
        device = _block(css, f':root:not([data-theme="light"])[data-accent="{key}"]')
        chosen = _block(css, f':root[data-theme="dark"][data-accent="{key}"]')
        out[key] = (light, device, chosen)
    return out


def test_chosen_dark_is_the_dark_block_word_for_word():
    _, device, chosen = _tokens()
    assert chosen == device
    for key, (_, device, chosen) in _themes().items():
        assert chosen == device, key


def test_every_theme_changes_only_the_accent_and_the_menu():
    light, dark, _ = _tokens()
    themes = _themes()
    assert set(themes) == set(appearance.THEMES)
    for key, (theme_light, theme_dark, _) in themes.items():
        assert set(theme_light) <= {n for n in light if n.startswith(("accent", "sidebar", "logo"))}, key
        assert set(theme_dark) == {n for n in dark if n.startswith(("accent", "sidebar", "logo"))}, key
    # The default is tokens.css's own colours, so its swatch shows what the menu looks like without a theme.
    green_light, green_dark, _ = themes[appearance.DEFAULT_THEME]
    assert green_light == {n: v for n, v in light.items() if n in green_light}
    assert green_dark == {n: v for n, v in dark.items() if n in green_dark}


@pytest.mark.parametrize("key", list(appearance.THEMES))
def test_every_theme_meets_the_contrast_rules(key):
    light, dark, _ = _tokens()
    theme_light, theme_dark, _ = _themes()[key]
    for colours in ({**light, **theme_light}, {**light, **dark, **theme_light, **theme_dark}):
        for name in ("accent", "accent-hover"):
            for background in (*SURFACES, "accent-soft"):
                assert contrast(colours[name], colours[background]) >= 4.5, (name, background)
            assert contrast(colours["accent-text"], colours[name]) >= 4.5, name
        for text in ("sidebar-text", "sidebar-muted", "sidebar-strong"):
            for background in ("sidebar", "sidebar-active"):
                assert contrast(colours[text], colours[background]) >= 4.5, (text, background)


def test_the_portal_draws_the_choice_and_reports_keep_their_own(client, db):
    make_user(db, "ana", role="analyst", all_tenants=True)
    login(client, "ana")
    page = client.get("/account").get_data(as_text=True)
    assert '<html lang="en">' in page  # nothing chosen: the device decides, in bottle green
    assert 'data-accent="navy"' in page  # the swatches

    response = client.post("/account/appearance", data={"theme": "dark", "accent": "navy"})
    assert response.status_code == 302 and response.headers["Location"].endswith("/account#appearance")
    row = db.execute("SELECT theme, accent FROM users WHERE username = 'ana'").fetchone()
    assert (row["theme"], row["accent"]) == ("dark", "navy")
    page = client.get("/account").get_data(as_text=True)
    assert '<html lang="en" data-theme="dark" data-accent="navy">' in page
    assert re.search(r'value="navy" checked', page) and re.search(r'value="dark" checked', page)
    assert "themes.css" in page

    # The style guide's sample report is a client report: light or dark as chosen, never the colour theme.
    report = client.get("/styleguide/report").get_data(as_text=True)
    assert 'data-theme="dark"' in report and "data-accent" not in report and "themes.css" not in report

    client.post("/account/appearance", data={"theme": "light", "accent": "green"})
    page = client.get("/account").get_data(as_text=True)
    assert '<html lang="en" data-theme="light">' in page


def test_nonsense_is_refused(client, db):
    make_user(db, "ana", role="analyst", all_tenants=True)
    login(client, "ana")
    for form in ({"theme": "sepia"}, {"theme": "dark", "accent": "orange"}, {}):
        assert client.post("/account/appearance", data=form).status_code == 400
    row = db.execute("SELECT theme, accent FROM users WHERE username = 'ana'").fetchone()
    assert (row["theme"], row["accent"]) == ("system", "green")


def test_a_client_chooses_light_or_dark_only(client, db, two_tenants):
    alpha, _ = two_tenants
    make_user(db, "client", role="client", tenants=[alpha])
    login(client, "client")
    page = client.get("/account").get_data(as_text=True)
    assert "Light or dark" in page and 'name="accent"' not in page
    assert client.post("/account/appearance", data={"theme": "dark", "accent": "plum"}).status_code == 302
    row = db.execute("SELECT theme, accent FROM users WHERE username = 'client'").fetchone()
    assert (row["theme"], row["accent"]) == ("dark", "green")
    page = client.get("/account").get_data(as_text=True)
    assert 'data-theme="dark"' in page and 'data-accent="plum"' not in page


def test_look_for_nobody_and_for_old_rows():
    assert appearance.look(None) == appearance.Look("", "")
    assert appearance.look({"role": "admin"}) == appearance.Look("", "")
    assert appearance.look({"role": "admin", "theme": "light", "accent": "claret"}) == appearance.Look(
        "light", "claret"
    )
    assert appearance.look({"role": "client", "theme": "dark", "accent": "claret"}) == appearance.Look("dark", "")
    assert appearance.look({"role": "admin", "theme": "neon", "accent": "neon"}) == appearance.Look("", "")
