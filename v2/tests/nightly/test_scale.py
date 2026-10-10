"""200 tenants stay fast: build the scale database and time the pages people open most.

``scripts/scale_data.py`` builds 200 tenants of about 110 accounts with ``NIGHTLY_SCALE_DAYS`` (90) nights of
history, as default retention leaves it. The build itself is timed, then each page below is opened twice as
an admin who sees every tenant: the first visit ("cold") works out what the worker would normally have
precomputed, the second ("warm") is what people get on a normal day. Either going over its budget fails.

The budgets are several times what a development machine takes today (Oct 2026: building 33 s; What
changed 15.5 s cold and 0.07 s warm; Projects 1.7 s), so a slower GitHub runner and ordinary noise pass and
a real slowdown doesn't. When a change makes a page legitimately slower, raise its budget here and say why.
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
# Page: (cold, warm) budgets in seconds. {t} is the first tenant.
PAGES = {
    "/": (20, 2),
    "/across/checks": (20, 2),
    "/changes": (90, 2),
    "/projects": (10, 8),
    "/search?q=adele": (5, 4),
    "/upcoming": (5, 2),
    "/tenants/{t}": (5, 2),
    "/tenants/{t}/findings": (5, 2),
    "/tenants/{t}/changes": (5, 2),
    "/tenants/{t}/data/mfa": (5, 2),
}
USERNAME, PASSWORD = "timer", "nightly scale check"  # noqa: S105 (a throwaway database)


@pytest.fixture(scope="module")
def scale(tmp_path_factory, summary):
    data = tmp_path_factory.mktemp("scale") / "data"
    env = {k: v for k, v in os.environ.items() if not k.startswith("OFFICESENTRY_")}
    start = time.monotonic()
    done = subprocess.run([sys.executable, "scripts/scale_data.py", "--data-dir", str(data), "--tenants",  # noqa: S603
                           str(TENANTS), "--days", str(DAYS), "--no-backup"],
                          cwd=V2, env=env, capture_output=True, text=True)
    took = time.monotonic() - start
    assert done.returncode == 0, f"scale_data.py failed:\n{done.stdout[-2000:]}\n{done.stderr[-4000:]}"
    size = (data / "officesentry.db").stat().st_size / 1e9
    summary.append(f"### Scale: {TENANTS} tenants, {DAYS} nights ({size:.2f} GB)\n\n"
                   f"Building it took {took:.0f} s (budget {BUILD_BUDGET} s).\n\n"
                   "| Page | First visit | Normal visit | Budget |\n|---|---|---|---|")
    from werkzeug.security import generate_password_hash

    from officesentry.db import connect, utcnow
    db = connect(data / "officesentry.db")
    db.execute("INSERT INTO users (username, password_hash, role, all_tenants, created_at)"
               " VALUES (?, ?, 'admin', 1, ?)",
               (USERNAME, generate_password_hash(PASSWORD, method="pbkdf2:sha256:1"), utcnow()))
    tenant = db.execute("SELECT MIN(id) FROM tenants").fetchone()[0]
    db.close()
    _, client = portal(data, USERNAME, PASSWORD)
    return client, tenant, took


def test_building_200_tenants_stays_within_budget(scale):
    _, _, took = scale
    assert took <= BUILD_BUDGET, f"scale_data.py took {took:.0f} s for {TENANTS} tenants (budget {BUILD_BUDGET} s)"


@pytest.mark.parametrize("page", PAGES)
def test_page_stays_fast_at_200_tenants(page, scale, summary):
    client, tenant, _ = scale
    cold_budget, warm_budget = PAGES[page]
    url = page.format(t=tenant)
    times = []
    for _ in range(2):
        start = time.monotonic()
        resp = client.get(url)
        times.append(time.monotonic() - start)
        assert resp.status_code == 200, f"{url} returned {resp.status_code}"
    cold, warm = times
    summary.append(f"| `{page}` | {cold:.2f} s | {warm:.2f} s | {cold_budget} s / {warm_budget} s |")
    assert cold <= cold_budget and warm <= warm_budget, (
        f"{url} took {cold:.2f} s (first visit, budget {cold_budget} s) and {warm:.2f} s (budget {warm_budget} s)")
