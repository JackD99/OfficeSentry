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
VERSION = "2.1.0"  # what the menu says it's running, whatever the code's version is
SCHEMA = 44  # the database version System status names, whatever the latest migration is

VIEWPORTS = {"laptop": {"width": 1366, "height": 768}, "phone": {"width": 390, "height": 844}}
THEMES = ("light", "dark")


@pytest.fixture(scope="session")
def site(tmp_path_factory):
    """(server, tenant ids, passwords): the demo portal on a free port, with time stopped at FROZEN."""
    from officesentry import system_health, web

    traveller = time_machine.travel(FROZEN, tick=False)
    traveller.start()
    try:
        # The version at the foot of the menu is painted over, but a development version ("2.2.0.dev0") is
        # long enough to wrap the line and move everything above it, so the demo always runs as a release.
        # System status says which database version it's on, and every new migration would change its
        # screenshots, so that's pinned too.
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(web, "__version__", VERSION)
            mp.setattr(system_health, "schema_version", lambda conn: SCHEMA)
            mp.setattr(system_health, "latest_version", lambda: SCHEMA)
            cfg, ids, passwords, _ = screenshots.build_site(tmp_path_factory.mktemp("ui") / "data")
            with screenshots.Server(cfg) as server:
                server.beat()  # a fresh worker heartbeat, so no "worker stopped" banner
                yield server, ids, passwords
    finally:
        traveller.stop()


@pytest.fixture
def connection_grace():
    """tests/conftest.py's check that a test leaves no database connection open waits, as tests/layout's
    does, up to half a minute for the portal's own threads (pages, the worker watchdog) to close theirs: a
    page has passed and then failed it while the watchdog still had a connection open."""
    return 30


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
