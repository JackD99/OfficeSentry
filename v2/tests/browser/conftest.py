"""The demo site from scripts/screenshots.py, at a fixed date, and a Chromium to look at it with.

Only collected with ``pytest --ui`` (tests/conftest.py); README.md in this folder says how to run them.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

pytest.importorskip("playwright", reason="the browser tests need Playwright (tests/browser/requirements.txt)")
time_machine = pytest.importorskip("time_machine", reason="the browser tests need time-machine")

V2 = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(V2 / "scripts"))

import screenshots  # noqa: E402

# Every page shows the same dates on every run: the demo data and "3 days ago" are relative to this.
FROZEN = datetime(2026, 10, 5, 9, 30, tzinfo=UTC)

VIEWPORTS = {"laptop": {"width": 1366, "height": 768}, "phone": {"width": 390, "height": 844}}
THEMES = ("light", "dark")


@pytest.fixture(scope="session")
def site(tmp_path_factory):
    """(server, tenant ids, passwords): the demo portal on a free port, with time stopped at FROZEN."""
    traveller = time_machine.travel(FROZEN, tick=False)
    traveller.start()
    try:
        cfg, ids, passwords, _ = screenshots.build_site(tmp_path_factory.mktemp("ui") / "data")
        with screenshots.Server(cfg) as server:
            server.beat()  # a fresh worker heartbeat, so no "worker stopped" banner
            yield server, ids, passwords
    finally:
        traveller.stop()


@pytest.fixture(scope="session")
def browser():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        chromium = p.chromium.launch()  # the Chromium this Playwright version pins
        yield chromium
        chromium.close()


@pytest.fixture(scope="session")
def contexts(browser, site):
    """One signed-in browser context per (user, viewport, theme), made on first use."""
    server, _, passwords = site
    made = {}

    def get(user: str, viewport: str, theme: str):
        key = (user, viewport, theme)
        if key not in made:
            options = {
                "viewport": VIEWPORTS[viewport],
                "color_scheme": theme,
                "device_scale_factor": 1,
                "reduced_motion": "reduce",
                "locale": "en-GB",
                "timezone_id": "Europe/London",
            }
            if viewport == "phone":
                options.update(is_mobile=True, has_touch=True)
            made[key] = browser.new_context(**options)
            if user:
                screenshots.sign_in(made[key], server.url, user, passwords[user])
        return made[key]

    yield get
    for context in made.values():
        context.close()
