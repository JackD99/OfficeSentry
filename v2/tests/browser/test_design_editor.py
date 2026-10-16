"""The report design editor (static/designer.js): what a number box shows is what the design keeps, and a design
that holds as many blocks as it can says so instead of dropping one."""

from __future__ import annotations

import pytest
import screenshots
from playwright.sync_api import expect

from officesentry.db import connect
from officesentry.reports import designs


@pytest.fixture
def editor(browser, site):
    """(page, the design as stored): the demo admin in the editor of a new design, removed again afterwards so
    the screenshots see the demo as it was."""
    server, _, passwords = site
    db = connect(server.cfg.db_path)
    design_id = designs.create(db, "Editor test", screenshots.ADMIN)
    context = browser.new_context(viewport={"width": 1366, "height": 900}, reduced_motion="reduce")
    screenshots.sign_in(context, server.url, screenshots.ADMIN, passwords[screenshots.ADMIN])
    page = context.new_page()

    def opened(blocks=()):
        designs.save(db, design_id, {"blocks": list(blocks)})
        page.goto(f"{server.url}/reporting/designs/{design_id}")
        return page

    yield opened, lambda: designs.document(designs.get(db, design_id))
    context.close()
    designs.remove(db, design_id)
    db.close()


def _saved(page):
    expect(page.locator("#designer-status")).to_have_text("Saved", timeout=10000)


def test_zero_in_a_number_box_is_saved(editor):
    """Affected items per check says "0 shows only how many are affected": a 0 typed there used to save the
    default (5) while the box still showed 0."""
    opened, stored = editor
    page = opened()
    page.click('button.palette-add[data-add="findings"]')
    box = page.locator('input[id$="-max_rows"]')
    box.fill("0")
    _saved(page)
    assert [b["settings"]["max_rows"] for b in stored()["blocks"]] == [0]

    box.fill("500")  # past the field's end: kept at its most, and the box says so once it's left
    box.press("Tab")
    _saved(page)
    most = next(f.max for f in designs.WIDGETS["findings"].fields if f.key == "max_rows")
    assert [b["settings"]["max_rows"] for b in stored()["blocks"]] == [most]
    assert box.input_value() == str(most)


def test_a_full_design_refuses_another_block(editor):
    opened, stored = editor
    full = [{"id": f"b{n}", "kind": "text", "settings": {"text": f"Block {n}"}} for n in range(designs.MAX_BLOCKS)]
    page = opened(full)
    expect(page.locator("#block-count")).to_have_text(f"{designs.MAX_BLOCKS} blocks, the most a design holds")
    page.click('button.palette-add[data-add="findings"]')
    expect(page.locator("#designer-status")).to_contain_text(f"holds {designs.MAX_BLOCKS} blocks at most")
    assert len(page.locator("#layout-list > li").all()) == designs.MAX_BLOCKS
    assert len(stored()["blocks"]) == designs.MAX_BLOCKS
