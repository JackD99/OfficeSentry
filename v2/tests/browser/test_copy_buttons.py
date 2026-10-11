"""Copy buttons (static/app.js): they copy, say so, and put their own label back afterwards, never parsing
anything read from the page as HTML."""

from __future__ import annotations

import pytest
import screenshots
from playwright.sync_api import expect

SAYS_COPIED_FOR = 2000  # milliseconds, as in app.js


@pytest.fixture
def evidence(browser, site):
    """The demo admin on the first client's Cyber Essentials page, whose form answers have copy buttons, with the
    clipboard allowed."""
    server, ids, passwords = site
    context = browser.new_context(viewport={"width": 1366, "height": 768}, reduced_motion="reduce")
    context.grant_permissions(["clipboard-read", "clipboard-write"], origin=server.url)
    screenshots.sign_in(context, server.url, screenshots.ADMIN, passwords[screenshots.ADMIN])
    page = context.new_page()
    page.goto(f"{server.url}/tenants/{ids[0]}/evidence/cyber-essentials")
    yield page
    context.close()


def test_copying_says_so_and_then_puts_the_label_back(evidence):
    button = evidence.locator("button[data-copy='#copy-all']")
    label = button.inner_html()
    assert "<svg" in label and "Copy all answers" in label  # the icon is part of the label

    button.click()
    expect(button).to_have_text("Copied all answers")
    assert evidence.evaluate("navigator.clipboard.readText()") == evidence.locator("#copy-all").input_value()
    button.click()  # again while it says "Copied": what it goes back to is still its own label
    evidence.wait_for_timeout(SAYS_COPIED_FOR + 1000)

    assert button.inner_html() == label
    assert button.get_attribute("data-label") is None and "copied" not in button.get_attribute("class")


def test_a_label_attribute_on_a_copy_button_is_never_turned_into_markup(evidence):
    """data-label also names table columns on phones; on a copy button it used to be put back as HTML."""
    evidence.evaluate(
        """() => {
          const button = document.createElement('button');
          button.type = 'button';
          button.id = 'planted';
          button.dataset.copy = '#copy-all';
          button.dataset.label = '<img id="injected" src="data:,">';
          button.textContent = 'Copy';
          document.querySelector('.copy-bar').append(button);
        }"""
    )
    button = evidence.locator("#planted")
    button.click()
    expect(button).to_have_text("Copied")
    evidence.wait_for_timeout(SAYS_COPIED_FOR + 1000)

    expect(button).to_have_text("Copy")
    assert evidence.locator("#injected").count() == 0
