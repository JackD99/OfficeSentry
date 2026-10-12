"""The sites the layout checks open (the demo site with the stress data, and a new install), a Chromium,
and the report of what they found.

Only collected with ``pytest --layout`` (tests/conftest.py); README.md in this folder says how to run them.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import pytest

pytest.importorskip("playwright", reason="the layout checks need Playwright (tests/browser/requirements.txt)")

V2 = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(V2 / "scripts"))

import screenshots  # noqa: E402

from . import stress  # noqa: E402
from .pages import Page, path_for  # noqa: E402

HERE = Path(__file__).resolve().parent
OUTPUT = HERE / "output"

# Three common laptop and desktop widths, then a phone.
LAPTOPS = [(1366, 768), (1440, 900), (1920, 1080)]
PHONE = (390, 844)


def _worker(config) -> str:
    return getattr(config, "workerinput", {}).get("workerid", "")


def pytest_configure(config):
    if config.getoption("--layout", default=False) and not _worker(config):
        shutil.rmtree(OUTPUT, ignore_errors=True)


def pytest_sessionfinish(session):
    """Once every part has finished: report.md (and the run's summary page in CI) from each part's results."""
    if not session.config.getoption("--layout", default=False) or _worker(session.config):
        return
    from .report import write

    write(OUTPUT, os.environ.get("GITHUB_STEP_SUMMARY"))


@dataclass
class Site:
    """A portal on a free port, and who can sign in to it."""

    server: screenshots.Server
    passwords: dict[str, str]
    ids: dict[str, int] = field(default_factory=dict)  # stress, empty: tenant ids
    staff_id: int = 0
    contexts: dict = field(default_factory=dict)

    def context(self, browser, user: str, phone: bool):
        key = (user, phone)
        if key not in self.contexts:
            width, height = PHONE if phone else LAPTOPS[0]
            options = {
                "viewport": {"width": width, "height": height},
                "device_scale_factor": 1,
                "reduced_motion": "reduce",
                "locale": "en-GB",
                "timezone_id": "Europe/London",
            }
            if phone:
                options.update(is_mobile=True, has_touch=True)
            context = browser.new_context(**options)
            context.set_default_timeout(60_000)
            if user:
                screenshots.sign_in(context, self.server.url, user, self.passwords[user])
            self.contexts[key] = context
        return self.contexts[key]

    def query(self, sql: str, *params):
        from officesentry.db import connect

        db = connect(self.server.cfg.db_path)
        try:
            row = db.execute(sql, params).fetchone()
            return row[0] if row else None
        finally:
            db.close()

    def first_link(self, browser, path: str, pattern: str):
        """The first value of ``pattern``'s group in the links on ``path`` (an admin opening it)."""
        html = self.context(browser, screenshots.ADMIN, False).request.get(self.server.url + path).text()
        found = re.search(pattern, html)
        return found.group(1) if found else None


def _tenant_link(kind: str):
    def find(site, browser, page, tenant):
        return site.first_link(browser, f"/tenants/{tenant}/{kind}", rf'href="/tenants/{tenant}/{kind}/([^/"?#.]+)"')

    return find


def _period(site, browser, page, tenant):
    from officesentry import month_end

    return month_end.current_period("Europe/London")


# How each URL argument is filled in: (site, browser, page, tenant id) -> value. A route with an argument not
# listed here fails, naming it.
RESOLVERS = {
    "user_id": lambda site, browser, page, tenant: (
        site.staff_id if page.endpoint.startswith("admin.") else _tenant_link("users")(site, browser, page, tenant)
    ),
    "gid": _tenant_link("groups"),
    "mailbox_id": _tenant_link("mailboxes"),
    "site_id": _tenant_link("sites"),
    "run_id": lambda site, browser, page, tenant: site.query(
        "SELECT id FROM runs WHERE tenant_id = ? ORDER BY id DESC LIMIT 1", tenant
    ),
    "schedule_id": lambda site, browser, page, tenant: site.query(
        "SELECT id FROM email_schedules WHERE tenant_id = ? ORDER BY id LIMIT 1", tenant
    ),
    "alert_id": lambda site, browser, page, tenant: site.query("SELECT id FROM email_alerts ORDER BY id LIMIT 1"),
    "group_id": lambda site, browser, page, tenant: site.query(
        "SELECT id FROM tenant_groups ORDER BY length(name) DESC LIMIT 1"
    ),
    "role_id": lambda site, browser, page, tenant: site.query("SELECT id FROM roles WHERE key = 'first_line'"),
    "plan_id": lambda site, browser, page, tenant: site.query(
        "SELECT id FROM report_plans ORDER BY length(name) DESC LIMIT 1"
    ),
    "design_id": lambda site, browser, page, tenant: site.query("SELECT id FROM report_designs ORDER BY id LIMIT 1"),
    "period": _period,
}


@pytest.fixture(scope="session")
def sites(tmp_path_factory):
    """{"demo": the demo site with the stress data, "new": a new install}, each running."""
    from officesentry.crypto import Vault
    from officesentry.db import connect

    cfg, ids, passwords, _ = screenshots.build_site(tmp_path_factory.mktemp("layout") / "data")
    db = connect(cfg.db_path)
    try:
        added = stress.add(db, ids)
        stress.audit_addon(db, Vault(cfg.encryption_keys), added["tenant"])
        staff_id = db.execute("SELECT id FROM users WHERE username = ?", (stress.STAFF_USER,)).fetchone()[0]
    finally:
        db.close()
    passwords = {**passwords, stress.CLIENT_USER: added["client_password"]}
    new_cfg, new_password = stress.empty_site(tmp_path_factory.mktemp("layout-new") / "data")
    with screenshots.Server(cfg) as demo, screenshots.Server(new_cfg) as new:
        demo.beat()  # a fresh worker heartbeat, so no "worker stopped" banner
        new.beat()
        yield {
            "demo": Site(demo, passwords, {"stress": added["tenant"], "empty": ids[-1]}, staff_id),
            "new": Site(new, {screenshots.ADMIN: new_password}),
        }


@pytest.fixture(scope="session")
def browser():
    from playwright.sync_api import sync_playwright

    # The Chromium this Playwright pins (the CI image has it); elsewhere one under $PLAYWRIGHT_BROWSERS_PATH.
    path = screenshots.chromium_path()
    with sync_playwright() as p:
        chromium = p.chromium.launch(executable_path=path)
        yield chromium
        chromium.close()


@dataclass
class Opened:
    """Where a page in pages.py is opened: its site, who by, and its full URL."""

    site: Site
    user: str
    url: str


@pytest.fixture(scope="session")
def open_as(sites, browser):
    def where(page: Page) -> Opened:
        site = sites["new" if page.variant == "new" else "demo"]
        user = {"client": stress.CLIENT_USER, "signed-out": ""}.get(page.variant, screenshots.ADMIN)
        tenant = site.ids.get("empty" if page.variant == "empty" else "stress")
        values = {"tenant_id": tenant}
        for arg in re.findall(r"<(?:\w+:)?(\w+)>", page.rule):
            if arg in values or arg in dict(page.values):
                continue
            if arg not in RESOLVERS:
                pytest.fail(f"{page.rule}: no way to fill in <{arg}>; add one to RESOLVERS in tests/layout/conftest.py")
            value = RESOLVERS[arg](site, browser, page, tenant)
            if value is None:
                pytest.fail(f"{page.rule}: found nothing to use for <{arg}> on the demo site")
            values[arg] = value
        return Opened(site, user, site.server.url + path_for(page, values))

    return where


@pytest.fixture(autouse=True)
def _every_connection_is_closed(_every_connection_is_closed, monkeypatch):
    """tests/conftest.py's check that a test leaves no database connection open, once the portal has finished
    what it was doing. The portal serves pages from threads of its own and runs its worker watchdog
    (system_alerts.start_watchdog) beside them, so a check can end while one of those still has a connection
    open; each gets a few seconds to close it before the check counts it as left open."""
    connect, opened = sqlite3.connect, []

    def tracked(*args, **kwargs):
        conn = connect(*args, **kwargs)
        opened.append(conn)
        return conn

    monkeypatch.setattr(sqlite3, "connect", tracked)
    yield
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and not all(getattr(conn, "closed", True) for conn in opened):
        time.sleep(0.05)


@pytest.fixture(scope="session")
def results(request):
    """Each page's findings, written to output/results-<part>.json at the end for report.py."""
    found: list[dict] = []
    yield found
    OUTPUT.mkdir(exist_ok=True)
    name = _worker(request.config) or "main"
    (OUTPUT / f"results-{name}.json").write_text(json.dumps(found, indent=1), encoding="utf-8")
