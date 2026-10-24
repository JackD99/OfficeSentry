"""A report table's Copy button and row search, and the keyboard shortcuts (static/keys.js), on the first demo
client's Findings."""

from __future__ import annotations

import re

import pytest
import screenshots
from playwright.sync_api import expect

from tests.browser.test_pages import _axe


@pytest.fixture
def findings(browser, site):
    """The demo admin on the first client's Findings at a laptop width, with the clipboard allowed."""
    server, ids, passwords = site
    context = browser.new_context(viewport={"width": 1366, "height": 768}, reduced_motion="reduce")
    context.grant_permissions(["clipboard-read", "clipboard-write"], origin=server.url)
    screenshots.sign_in(context, server.url, screenshots.ADMIN, passwords[screenshots.ADMIN])
    page = context.new_page()
    page.goto(f"{server.url}/tenants/{ids[0]}/findings")
    yield page
    context.close()


def _shown(table):
    return table.locator("tbody tr:not(.group-head):not(.row-note):visible")


def test_copy_puts_the_rows_shown_on_the_clipboard_under_their_headings(findings):
    card = findings.locator("section.table-card").first
    table = card.locator("table")
    button = card.locator("button[data-copy-rows]")
    rows = table.locator("tbody tr:not(.group-head):not(.row-note)").count()
    assert rows > 8

    button.click()
    expect(button).to_have_text(f"Copied {rows} rows")
    lines = findings.evaluate("navigator.clipboard.readText()").split("\n")
    headings = lines[0].split("\t")
    # Grouped by check, so each row gets its group's check first; the row's own Open and Undo stay behind.
    assert headings[:3] == ["Check", "Severity", "Account or item"] and "Details" not in headings
    assert len(lines) == rows + 1 and all(len(line.split("\t")) == len(headings) for line in lines)
    first = dict(zip(headings, lines[1].split("\t"), strict=True))
    assert first["Check"] and first["Severity"] in {"Critical", "High", "Medium", "Low"}
    assert not any(re.search(r"\bOpen\b$", line) for line in lines)

    # A search narrows what's copied, and the count beside the title says how many of the rows are left.
    word = first["Account or item"].split()[0].lower()
    badge = card.locator("h2 .count-badge")
    total = badge.inner_text()
    card.locator("input[data-filter]").fill(word)
    left = _shown(table).count()
    assert 0 < left < rows
    expect(badge).to_have_text(f"{left} of {rows}")
    findings.wait_for_timeout(2500)  # "Copied" gives way to the button's own label
    button.click()
    expect(button).to_have_text(f"Copied {left} row" + ("" if left == 1 else "s"))
    assert len(findings.evaluate("navigator.clipboard.readText()").split("\n")) == left + 1

    card.locator("input[data-filter]").fill("zzz nothing like this")
    expect(card.locator("p.no-rows")).to_have_text("No rows match “zzz nothing like this”.")
    card.locator("input[data-filter]").fill("")
    expect(badge).to_have_text(total)
    expect(card.locator("p.no-rows")).to_be_hidden()


def test_j_and_k_move_through_the_rows_and_enter_opens_the_finding(findings):
    rows = _shown(findings.locator("section.table-card table").first)
    findings.keyboard.press("j")
    expect(rows.nth(0)).to_have_class(re.compile(r"\brow-cursor\b"))
    findings.keyboard.press("j")
    findings.keyboard.press("j")
    findings.keyboard.press("k")
    second = rows.nth(1)
    expect(second).to_have_class(re.compile(r"\brow-cursor\b"))
    assert findings.locator("tr.row-cursor").count() == 1

    key = second.get_attribute("data-key")
    findings.keyboard.press("Enter")
    expect(findings.locator(f'#finding-sheet[data-finding="{key}"]')).to_be_visible()
    # Typing in a field is typing: j goes into the search box, not to the next row.
    findings.locator("section.table-card input[data-filter]").first.focus()
    findings.keyboard.press("j")
    expect(findings.locator("section.table-card input[data-filter]").first).to_have_value("j")


def test_question_mark_lists_the_shortcuts_and_g_opens_the_menu(findings):
    box = findings.locator("#keys")
    expect(box).to_be_hidden()
    findings.keyboard.press("?")
    expect(box).to_be_visible()
    go = box.locator("[data-keys-go]")
    expect(go.locator("dd", has_text="Clients")).to_have_count(1)
    expect(go.locator("dd", has_text="Settings")).to_have_count(1)
    assert _axe(findings) == []
    findings.keyboard.press("Escape")
    expect(box).to_be_hidden()

    findings.keyboard.press("g")
    findings.keyboard.press("r")
    findings.wait_for_url("**/reports")
