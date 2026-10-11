"""Every page (pages.py) at three laptop widths and a phone, checked for layouts that break: the page
scrolling sideways, text or controls cut off, things drawn on top of each other, controls past the edge of
the window and, at laptop widths, tables that scroll sideways inside the page (audit.js says how each is
found). A page with a problem gets a screenshot with the problem outlined in output/, and output/report.md
lists them all.
"""

from __future__ import annotations

from io import BytesIO
from pathlib import Path

import pytest

from .conftest import LAPTOPS, OUTPUT, PHONE
from .pages import NOT_PAGES, every_page, is_page

AUDIT = (Path(__file__).resolve().parent / "audit.js").read_text(encoding="utf-8")
PAGES = every_page()
MAX_SHOT = 3200  # the tallest screenshot kept, in pixels
OUTLINE = "#e5251b"


@pytest.mark.parametrize("spec", PAGES, ids=lambda p: p.name)
def test_layout(spec, open_as, browser, results):
    where = open_as(spec)
    found: dict[str, list[dict]] = {}
    shots: dict[str, str] = {}
    for phone in (False, True):
        page = where.site.context(browser, where.user, phone).new_page()
        try:
            response = page.goto(where.url, wait_until="load")
            status = response.status if response else 0
            assert status < 400, f"{where.url} returned {status}"
            page.evaluate("document.fonts.ready")
            for width, height in [PHONE] if phone else LAPTOPS:
                page.set_viewport_size({"width": width, "height": height})
                page.evaluate("() => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)))")
                problems = page.evaluate(AUDIT, {"desktop": not phone})
                label = f"{width}" if not phone else f"phone-{width}"
                if problems:
                    found[label] = problems
                    shots[label] = _screenshot(page, spec, label, problems)
                page.evaluate(
                    "() => document.querySelectorAll('[data-layout-problem]')"
                    ".forEach(e => { e.removeAttribute('data-layout-problem'); e.style.outline = ''; })"
                )
        finally:
            page.close()
    path = where.url.split("/", 3)[-1]
    results.append({"page": spec.name, "variant": spec.variant, "path": "/" + path, "found": found, "shots": shots})
    assert not found, _describe(spec, "/" + path, found)


def _describe(spec, path: str, found: dict) -> str:
    lines = [f"{spec.name} ({path}):"]
    for label, problems in found.items():
        for p in problems:
            times = f" (x{p['count']})" if p["count"] > 1 else ""
            lines.append(f"  {label}: {p['kind']}: {p['text']}{times}; {p['where']}")
    return "\n".join(lines)


def _screenshot(page, spec, label: str, problems: list[dict]) -> str:
    """The page with the problems outlined, from a little above the first one, as a 256-colour PNG."""
    from PIL import Image

    page.evaluate(
        "colour => document.querySelectorAll('[data-layout-problem]')"
        ".forEach(e => { e.style.outline = '3px solid ' + colour; e.style.outlineOffset = '-1px'; })",
        OUTLINE,
    )
    width = page.viewport_size["width"]
    height = page.evaluate("Math.max(document.documentElement.scrollHeight, document.body.scrollHeight)")
    top = min(p["top"] for p in problems)
    y = 0 if top < MAX_SHOT * 0.6 else max(0, top - 300)
    png = page.screenshot(
        full_page=True,
        animations="disabled",
        caret="hide",
        clip={
            "x": 0,
            "y": y,
            "width": max(width, page.evaluate("document.documentElement.scrollWidth")),
            "height": min(height - y, MAX_SHOT),
        },
    )
    OUTPUT.mkdir(exist_ok=True)
    name = spec.name.replace("/", "--").replace("[", "-").replace("]", "") + f"--{label}.png"
    image = Image.open(BytesIO(png)).convert("RGB")
    image.quantize(colors=256, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE).save(
        OUTPUT / name, optimize=True
    )
    return name


def test_every_route_is_a_page_or_says_why_not():
    """A route left out of the checks is a download or in NOT_PAGES; a NOT_PAGES entry names a real route."""
    from .pages import _routes

    rules, _, _ = _routes()
    endpoints = {r.endpoint for r in rules}
    assert not set(NOT_PAGES) - endpoints, f"NOT_PAGES names routes that no longer exist: {set(NOT_PAGES) - endpoints}"
    checked = {p.endpoint for p in PAGES}
    missed = sorted(r.rule for r in rules if is_page(r) and r.endpoint not in checked)
    assert not missed, f"pages never opened: {missed}"
