"""Keyboard shortcuts (static/keys.js, templates/_keys.html) and copying a report table's rows (app.js, tables.js):
what the pages carry for the scripts. tests/browser/test_keys_and_copy.py drives them in a browser."""

from __future__ import annotations

import re
from pathlib import Path

from tests.conftest import login, make_user
from tests.seed import seed_tenant

STATIC = Path(__file__).resolve().parents[1] / "officesentry" / "web" / "static"


def _sidebar(page: str) -> str:
    return page[page.index('<aside class="sidebar"') : page.index("</aside>")]


def test_staff_get_the_shortcuts_list_and_each_menu_entry_has_its_own_letter(client, db, two_tenants):
    make_user(db, "admin")
    login(client, "admin")
    page = client.get("/").get_data(as_text=True)
    assert '<dialog id="keys"' in page and "/static/keys.js" in page
    letters = re.findall(r'<a href="[^"]+" data-go="([a-z])"', _sidebar(page))
    assert len(letters) == len(set(letters)) >= 8  # Home, Clients, Reports, ... and Settings
    assert {"h", "c", "r", "s"} <= set(letters)
    assert "data-keys-go" in page  # keys.js fills "Go to" from the menu itself


def test_client_accounts_get_no_shortcuts(client, db, two_tenants):
    alpha, _ = two_tenants
    make_user(db, "viewer", role="client", tenants=[alpha])
    login(client, "viewer")
    page = client.get("/").get_data(as_text=True)
    assert 'id="keys"' not in page and "keys.js" not in page


def test_a_report_table_offers_copy_for_its_own_rows(client, db, two_tenants):
    alpha, _ = two_tenants
    seed_tenant(db, alpha)
    make_user(db, "admin")
    login(client, "admin")
    page = client.get(f"/tenants/{alpha}/findings").get_data(as_text=True)
    buttons = re.findall(r'data-copy-rows="([^"]+)"', page)
    assert buttons and all(re.search(rf'<table class="stack-sm[^"]*" id="{b}"', page) for b in buttons)
    # The row's own buttons (Open, Undo) stay behind: their heading is marked so the copy can leave the column out.
    assert '<th class="row-action">Details</th>' in page


def test_copying_is_wired_and_never_parses_the_page_as_markup():
    app = (STATIC / "app.js").read_text(encoding="utf-8")
    tables = (STATIC / "tables.js").read_text(encoding="utf-8")
    assert "[data-copy-rows]" in app and "window.officeSentryRows" in app
    assert "window.officeSentryRows = function" in tables
    rows = tables[tables.index("window.officeSentryRows = function") :]
    rows = rows[: rows.index("\n  };")]
    # Cells are written with textContent into a table made here; nothing read from the page goes in as HTML.
    assert "innerHTML" not in rows and "insertAdjacentHTML" not in rows and "cell.textContent = text" in rows
