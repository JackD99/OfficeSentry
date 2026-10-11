"""Every page the layout checks open, worked out from the portal's routes so a new page is checked without
anyone adding it here.

Each GET route is a page unless it's a download (``.pdf``, ``.xlsx``, ...) or listed in ``NOT_PAGES`` with
the reason. A route's URL arguments are filled in when the test runs (``conftest.Site.url``): the tenant is
the stress client (stress.py), ``<key>`` is every detail report and ``<scheme>`` every evidence scheme, and
anything else comes from ``RESOLVERS`` in conftest.py, so a route with a new kind of argument fails until it
gets one. Pages are opened as:

* ``stress``: an admin, on the demo site with the stress client added (every page);
* ``empty``: an admin, on the client still being onboarded, which has no data (every client page once);
* ``client``: the stress client's own user (the pages a client sees);
* ``new``: an admin of a new install with nothing in it (every page without arguments);
* ``signed-out``: nobody signed in.
"""

from __future__ import annotations

import re
import tempfile
from dataclasses import dataclass
from functools import cache
from pathlib import Path

# Routes that aren't pages to look at, and why.
NOT_PAGES = {
    "static": "files",
    "health": "JSON for monitors",
    "health_ready": "JSON for monitors",
    "branding.brand_css": "a stylesheet",
    "branding.logo": "an image",
    "reporting.progress": "JSON polled by the queue page",
    "email.issued_report": "a file download",
    "auth.setup": "only before the first admin exists",
    "auth.login_code": "only part-way through signing in",
    "auth.new_password": "only part-way through signing in",
    "sso.start": "goes to Microsoft",
    "sso.callback": "Microsoft comes back here",
    "tenants.consent_callback": "Microsoft comes back here",
    "home.index": "redirects to the person's home page",
    "across.index": "redirects to the first All clients report",
    "reporting.index": "redirects to this month's queue",
    "across.rows": "opened through across.rows_index, which shows a report's first table",
}
DOWNLOAD = re.compile(r"\.(pdf|xlsx|csv|cer|txt|docx|zip|json|svg|ico|<ext>)$|<ext>")
ARG = re.compile(r"<(?:\w+:)?(\w+)>")

# What a client sees: their home page and their own client's pages.
CLIENT_PAGES = [
    "/",
    "/tenants/<int:tenant_id>",
    "/tenants/<int:tenant_id>/findings",
    "/tenants/<int:tenant_id>/report",
    "/tenants/<int:tenant_id>/data",
    "/tenants/<int:tenant_id>/data/<key>",  # Conditional Access: the long lists of roles
    "/tenants/<int:tenant_id>/evidence",
    "/tenants/<int:tenant_id>/report-history",
    "/account",
]
SIGNED_OUT_PAGES = ["/login"]
SIGNED_OUT_ONLY = {"auth.login"}  # someone signed in is sent on from these
# On the client with no data, a detail report and an evidence scheme stand for all of them.
EMPTY_KEY, EMPTY_SCHEME = "mfa", "cyber-essentials"


@dataclass(frozen=True)
class Page:
    name: str  # the test id
    rule: str  # the route, with <arguments>
    endpoint: str
    variant: str  # stress, empty, client, new or signed-out
    values: tuple[tuple[str, str], ...] = ()  # arguments fixed when collected (key, scheme)

    @property
    def area(self) -> str:
        """The first part of the path, for grouping the report."""
        return self.rule.split("/")[1] or "home"


@cache
def _routes() -> tuple[list, dict[str, list[str]], list[str]]:
    """(GET rules, the keys a route's <key> takes by endpoint, evidence scheme keys), from a throwaway
    portal."""
    import logging

    from officesentry.config import load_config
    from officesentry.reports import data_reports, rollups, schemes
    from officesentry.web import create_app

    logging.getLogger("officesentry").setLevel(logging.ERROR)  # "No admin account yet"
    with tempfile.TemporaryDirectory() as tmp:
        app = create_app(load_config(data_dir=Path(tmp) / "data", secure_cookies=False, require_mfa="off"))
        rules = sorted((r for r in app.url_map.iter_rules() if "GET" in r.methods), key=lambda r: r.rule)
    # All clients has the monthly review and insurance evidence besides every detail report.
    keys = {"across.rollup": [k for k, _ in rollups.available()]}
    return rules, keys | {"": sorted(data_reports.REPORTS)}, sorted(schemes.SCHEMES)


def is_page(rule) -> bool:
    return rule.endpoint not in NOT_PAGES and not DOWNLOAD.search(rule.rule)


def every_page() -> list[Page]:
    rules, reports, schemes = _routes()
    out: list[Page] = []

    def add(variant: str, rule, values: dict | None = None):
        values = values or {}
        suffix = "".join(f"[{v}]" for v in values.values())
        out.append(Page(f"{variant}/{rule.endpoint}{suffix}", rule.rule, rule.endpoint, variant, tuple(values.items())))

    for rule in rules:
        if not is_page(rule) or rule.endpoint in SIGNED_OUT_ONLY:
            continue
        args = set(rule.arguments)
        choices = [{}]
        if "key" in args:
            choices = [{"key": k} for k in reports.get(rule.endpoint, reports[""])]
        elif "scheme" in args:
            choices = [{"scheme": s} for s in schemes]
        for values in choices:
            add("stress", rule, values)
        if "tenant_id" in args and args <= {"tenant_id", "key", "scheme"}:
            add(
                "empty",
                rule,
                {"key": EMPTY_KEY} if "key" in args else {"scheme": EMPTY_SCHEME} if "scheme" in args else {},
            )
        if not args:
            add("new", rule)
    by_rule = {r.rule: r for r in rules}
    for path in CLIENT_PAGES:
        add("client", by_rule[path], {"key": "conditional-access"} if "<key>" in path else {})
    for path in SIGNED_OUT_PAGES:
        add("signed-out", by_rule[path])
    return out


def path_for(page: Page, values: dict) -> str:
    """The page's URL path with ``values`` for its arguments (they must all be there)."""
    known = {**values, **dict(page.values)}
    return ARG.sub(lambda m: str(known[m.group(1)]), page.rule)
