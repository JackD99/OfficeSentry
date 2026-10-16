"""200 tenants stay fast: build the scale database and time the pages people open most.

``scripts/scale_data.py`` builds 200 tenants of about 110 accounts with ``NIGHTLY_SCALE_DAYS`` (90) nights of
history, as default retention leaves it. The build itself is timed, then each page below is opened twice as
an admin who sees every tenant: the first visit ("cold") works out what the worker would normally have
precomputed, the second ("warm") is what people get on a normal day. Either going over its budget fails.

The budgets are several times what a development machine takes today (Oct 2026: building 40 s; What
changed 27 s cold and 0.1 s warm; Clients 5 s cold and 0.3 s warm; Projects 2 s cold and 0.1 s warm), so a
slower GitHub runner and ordinary noise pass and a real slowdown doesn't. When a change makes a page
legitimately slower, raise its budget here and say why. ``scripts/timings.py`` times more pages, the
downloads and the worker's jobs on the same data, for the numbers in DEPLOY.md.
"""

from __future__ import annotations

import os
import subprocess  # noqa: S404 (the repository's own scale script)
import sys
import time

import pytest

from .conftest import V2, portal

TENANTS = 200
DAYS = int(os.environ.get("NIGHTLY_SCALE_DAYS", "90"))
BUILD_BUDGET = 240  # seconds, for 200 tenants and 90 nights
# Page: (cold, warm) budgets in seconds. {t} is the first tenant, {period} this month's month-end period.
PAGES = {
    "/": (20, 2),
    "/clients": (30, 2),
    "/across/checks": (20, 2),
    "/across/evidence": (20, 2),
    "/across/mfa": (10, 2),
    "/changes": (90, 2),
    "/projects": (10, 2),
    "/reporting/{period}": (20, 2),
    "/search?q=adele": (5, 2),
    "/search?q=203.0.113": (5, 2),
    "/upcoming": (5, 2),
    "/tenants/{t}": (5, 2),
    "/tenants/{t}/findings": (5, 2),
    "/tenants/{t}/changes": (5, 2),
    "/tenants/{t}/data/mfa": (5, 2),
}
USERNAME, PASSWORD = "timer", "nightly scale check"  # noqa: S105 (a throwaway database)
# The build runs in the first test's setup, so the suite's usual two minutes a test would stop it before its own
# budget does: give every test here room for the build and its slowest page.
pytestmark = pytest.mark.timeout(BUILD_BUDGET + max(cold for cold, _ in PAGES.values()) + 60)


@pytest.fixture(scope="module")
def scale(tmp_path_factory, summary):
    data = tmp_path_factory.mktemp("scale") / "data"
    env = {k: v for k, v in os.environ.items() if not k.startswith("OFFICESENTRY_")}
    start = time.monotonic()
    done = subprocess.run(  # noqa: S603
        [
            sys.executable,
            "scripts/scale_data.py",
            "--data-dir",
            str(data),
            "--tenants",
            str(TENANTS),
            "--days",
            str(DAYS),
            "--no-backup",
        ],
        cwd=V2,
        env=env,
        capture_output=True,
        text=True,
    )
    took = time.monotonic() - start
    assert done.returncode == 0, f"scale_data.py failed:\n{done.stdout[-2000:]}\n{done.stderr[-4000:]}"
    size = (data / "officesentry.db").stat().st_size / 1e9
    summary.append(
        f"### Scale: {TENANTS} tenants, {DAYS} nights ({size:.2f} GB)\n\n"
        f"Building it took {took:.0f} s (budget {BUILD_BUDGET} s).\n\n"
        "| Page | First visit | Normal visit | Budget |\n|---|---|---|---|"
    )
    from werkzeug.security import generate_password_hash

    from officesentry.db import connect, utcnow

    db = connect(data / "officesentry.db")
    db.execute(
        "INSERT INTO users (username, password_hash, role, all_tenants, created_at) VALUES (?, ?, 'admin', 1, ?)",
        (USERNAME, generate_password_hash(PASSWORD, method="pbkdf2:sha256:1"), utcnow()),
    )
    tenant = db.execute("SELECT MIN(id) FROM tenants").fetchone()[0]
    db.close()
    cfg, client = portal(data, USERNAME, PASSWORD)
    from officesentry.month_end import current_period

    return client, {"t": tenant, "period": current_period(cfg.timezone)}, took


def test_building_200_tenants_stays_within_budget(scale):
    _, _, took = scale
    assert took <= BUILD_BUDGET, f"scale_data.py took {took:.0f} s for {TENANTS} tenants (budget {BUILD_BUDGET} s)"


@pytest.mark.parametrize("page", PAGES)
def test_page_stays_fast_at_200_tenants(page, scale, summary):
    client, fill, _ = scale
    cold_budget, warm_budget = PAGES[page]
    url = page.format(**fill)
    times = []
    for _ in range(2):
        start = time.monotonic()
        resp = client.get(url)
        times.append(time.monotonic() - start)
        assert resp.status_code == 200, f"{url} returned {resp.status_code}"
    cold, warm = times
    summary.append(f"| `{page}` | {cold:.2f} s | {warm:.2f} s | {cold_budget} s / {warm_budget} s |")
    assert cold <= cold_budget and warm <= warm_budget, (
        f"{url} took {cold:.2f} s (first visit, budget {cold_budget} s) and {warm:.2f} s (budget {warm_budget} s)"
    )
