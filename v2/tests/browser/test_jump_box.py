"""The jump box (static/jump.js, templates/_jump.html): "/" opens it over the page, two words find a report for a
client, the keyboard works, what was opened is remembered, and the open box passes axe."""

from __future__ import annotations

import pytest
import screenshots
from playwright.sync_api import expect

from tests.browser.test_pages import _axe


@pytest.fixture
def signed_in(browser, site):
    """A context signed in as the demo admin, at a laptop width unless asked for a phone."""
    server, ids, passwords = site
    contexts = []

    def make(phone: bool = False):
        options = {"viewport": {"width": 390, "height": 844}, "is_mobile": True, "has_touch": True} if phone else {}
        context = browser.new_context(reduced_motion="reduce", **options)
        screenshots.sign_in(context, server.url, screenshots.ADMIN, passwords[screenshots.ADMIN])
        contexts.append(context)
        page = context.new_page()
        page.goto(f"{server.url}/today")
        return page

    yield make
    for context in contexts:
        context.close()


def test_slash_opens_it_and_two_words_find_a_report_for_a_client(signed_in):
    page = signed_in()
    expect(page.locator(".sidebar .nav-search")).to_be_hidden()  # the form gave way to the button
    expect(page.locator(".sidebar .nav-search-btn")).to_be_visible()
    box = page.locator("#jump")
    expect(box).to_be_hidden()

    page.keyboard.press("/")
    expect(box).to_be_visible()
    expect(page.locator("#jump-q")).to_be_focused()
    expect(box.locator("[data-jump-hint]")).to_be_visible()  # nothing opened yet: the hint, no "Recent"
    page.keyboard.type("north mfa")
    options = box.locator("[role=option]")
    first = options.first
    expect(first).to_have_attribute("aria-selected", "true")
    expect(first).to_contain_text("MFA coverage · Northwind Traders")
    expect(first.locator("mark").first).to_have_text("MFA")  # what was typed, in bold
    expect(first.locator("mark").nth(1)).to_have_text("North")
    expect(box.locator(".jump-status")).to_contain_text("matches")
    expect(options.last).to_contain_text("Search everything for “north mfa”")
    assert _axe(page) == []

    page.keyboard.press("ArrowDown")
    expect(first).to_have_attribute("aria-selected", "false")
    expect(options.nth(1)).to_have_attribute("aria-selected", "true")
    page.keyboard.press("ArrowUp")
    expect(first).to_have_attribute("aria-selected", "true")
    page.keyboard.press("Enter")
    page.wait_for_url("**/data/mfa")
    expect(page.locator("h1")).to_contain_text("Northwind Traders")

    # Remembered: the box opens on what was last opened, and Escape closes it.
    page.keyboard.press("/")
    expect(box.locator(".jump-label")).to_have_text("Recent")
    expect(box.locator("[role=option]").first).to_contain_text("MFA coverage · Northwind Traders")
    page.keyboard.press("Escape")
    expect(box).to_be_hidden()

    # Nothing matched: the full search is the one row, and Enter goes there.
    page.keyboard.press("Control+k")
    expect(box).to_be_visible()
    page.keyboard.type("xyzzy")
    expect(box.locator(".jump-status")).to_have_text("Nothing matches “xyzzy”")
    expect(box.locator("[role=option]")).to_have_count(1)
    page.keyboard.press("Enter")
    page.wait_for_url("**/search?q=xyzzy")


def test_a_late_close_event_leaves_the_box_opened_again_alone(signed_in):
    """A dialog's close event comes a task after it closes, so after a quick Escape and Ctrl+K it can land while
    the open box is waiting to search what was typed; it mustn't cancel that search."""
    page = signed_in()
    page.keyboard.press("/")
    expect(page.locator("#jump")).to_be_visible()
    page.evaluate(
        """() => {
          document.getElementById('jump').close();
          document.dispatchEvent(new KeyboardEvent('keydown', {key: 'k', ctrlKey: true, bubbles: true}));
          const input = document.getElementById('jump-q');
          input.value = 'xyzzy';
          input.dispatchEvent(new Event('input', {bubbles: true}));
        }"""
    )
    expect(page.locator("#jump .jump-status")).to_have_text("Nothing matches “xyzzy”")


def test_the_menu_button_opens_it_on_a_laptop_and_a_phone(signed_in):
    laptop = signed_in()
    laptop.locator(".sidebar .nav-search-btn").click()
    expect(laptop.locator("#jump")).to_be_visible()
    expect(laptop.locator("#jump-q")).to_be_focused()
    laptop.locator("#jump .jump-close").click()
    expect(laptop.locator("#jump")).to_be_hidden()

    phone = signed_in(phone=True)
    menu = phone.locator(".mobile-bar")
    menu.locator("summary").click()
    menu.locator(".nav-search-btn").click()
    expect(phone.locator("#jump")).to_be_visible()
    assert not menu.evaluate("el => el.open")  # the menu closed behind the box
    phone.locator("#jump-q").fill("contoso")
    expect(phone.locator("#jump [role=option]").first).to_contain_text("Contoso Legal LLP")
    assert phone.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth")
