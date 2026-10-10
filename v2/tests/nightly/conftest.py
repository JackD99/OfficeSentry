"""Shared pieces of the nightly suite (README.md in this folder): a portal client signed in as an admin,
a crawler that opens every page it can reach, and a summary file the workflow shows on the run's page.

Only collected with ``pytest --nightly`` (tests/conftest.py).
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

import pytest

V2 = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(V2 / "scripts"))

# Not opened in a crawl: static files, the readiness check (503 without a worker) and PDFs, which are slow
# (the PDF snapshot test renders them). Signing out, and everything else that changes something, is a POST,
# which a crawl never sends.
SKIP = re.compile(r"^/(static|health)/|\.pdf(\?|$)")
HREF = re.compile(r'href="(/[^"#]*)"')
# Path segments that are IDs (a tenant, a run, a person), so /tenants/3/users/u41 and /tenants/4/users/u7 are
# one kind of page.
ID_SEGMENT = re.compile(r"/[^/?]*\d[^/?]*")


def portal(data_dir: Path, username: str, password: str):
    """A test client of the portal on ``data_dir`` (which is upgraded first), signed in as ``username``."""
    from officesentry.config import load_config
    from officesentry.web import create_app

    cfg = load_config(data_dir=data_dir, testing=True, secure_cookies=False, require_mfa="off",
                      extra={"disable_csrf": True})
    client = create_app(cfg).test_client()
    resp = client.post("/login", data={"username": username, "password": password})
    assert resp.status_code == 302 and "/login" not in resp.headers.get("Location", ""), "couldn't sign in"
    return cfg, client


def crawl(client, start: list[str], limit: int = 3000, per_kind: int = 8) -> tuple[int, list[str]]:
    """Open ``start`` and every same-site link found on the pages, once each. Returns (pages opened, problems):
    a server error or an exception on any page. Each kind of page (its path with IDs blanked) is opened at
    most ``per_kind`` times, so a tenant's hundred people, or sorting and filter links, don't multiply the
    crawl, while every report and tab is still opened for each tenant."""
    import html

    seen: set[str] = set()
    per: dict[str, int] = {}
    queue = list(start)
    problems: list[str] = []
    opened = 0
    while queue and opened < limit:
        url = html.unescape(queue.pop(0))
        kind = ID_SEGMENT.sub("/#", url.split("?", 1)[0])
        if url in seen or SKIP.search(url) or per.get(kind, 0) >= per_kind:
            continue
        seen.add(url)
        per[kind] = per.get(kind, 0) + 1
        opened += 1
        try:
            resp = client.get(url)
        except Exception as exc:  # noqa: BLE001 (any crash is the finding)
            problems.append(f"{url}: {type(exc).__name__}: {exc}")
            continue
        if resp.status_code >= 500:
            problems.append(f"{url}: {resp.status_code}")
        if resp.status_code in (301, 302, 303) and resp.headers.get("Location", "").startswith("/"):
            queue.append(resp.headers["Location"])
        if resp.mimetype == "text/html":
            queue += HREF.findall(resp.get_data(as_text=True))
    return opened, problems


@pytest.fixture(scope="session")
def summary():
    """Lines for the workflow's run summary (``NIGHTLY_SUMMARY``, a Markdown file), written at the end."""
    lines: list[str] = []
    yield lines
    target = os.environ.get("NIGHTLY_SUMMARY")
    if target and lines:
        with open(target, "a", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n\n")
