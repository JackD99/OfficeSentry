"""The pages the browser tests capture: the ones people use most, plus the style guide.

``{t}`` is the showcase tenant (Fabrikam Dental Group in scripts/demo_data.py) and ``{o}`` the one being
onboarded. Each page is captured at a laptop and a phone width, in light and dark: four baselines.
A page added here needs ``pytest tests/browser --ui --update -k <name>`` once (README.md).
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Page:
    name: str
    path: str
    user: str = "demo"  # screenshots.ADMIN; "client" is the client user; "" is signed out
    mask: tuple[str, ...] = field(default=())  # selectors painted over: things that differ between runs
    full: bool = False  # capture the whole page, not just its top (test_pages.MAX_HEIGHT)


PAGES = [
    Page("login", "/login", user=""),
    Page("dashboard", "/"),
    Page("today", "/today"),
    Page("clients", "/clients"),
    Page("tenant-overview", "/tenants/{t}"),
    Page("findings", "/tenants/{t}/findings"),
    Page("tenant-changes", "/tenants/{t}/changes"),
    Page("detail-reports", "/tenants/{t}/data"),
    Page("sharepoint-storage", "/tenants/{t}/data/sharepoint-storage"),
    Page("mfa", "/tenants/{t}/data/mfa"),
    Page("mailboxes", "/tenants/{t}/data/mailboxes"),
    Page("licence-rightsizing", "/tenants/{t}/data/licence-rightsizing"),
    Page("people", "/tenants/{t}/users"),
    Page("person", "/tenants/{t}/users/u1"),
    Page("monthly-review", "/tenants/{t}/report"),
    Page("evidence", "/tenants/{t}/evidence"),
    Page("tenant-projects", "/tenants/{t}/projects"),
    Page("emailed-reports", "/tenants/{t}/schedules"),
    Page("schedule-form", "/tenants/{t}/schedules/new"),
    Page("manage-tenant", "/tenants/{t}/manage"),
    Page("onboarding", "/tenants/{o}"),
    Page("reports-library", "/reports"),
    Page("all-tenants", "/across/review"),
    Page("checks-matrix", "/across/checks"),
    Page("what-changed", "/changes"),
    Page("projects", "/projects"),
    Page("coming-up", "/upcoming"),
    Page("month-end", "/reporting"),
    Page("search", "/search?q=adele"),
    Page("client-tenants", "/admin/tenants"),
    Page("users", "/admin/users"),
    Page("branding", "/admin/branding"),
    Page("system-status", "/admin/status"),
    Page("client-home", "/", user="client"),
    Page("styleguide", "/styleguide", full=True),  # every component, so all of it
    Page("styleguide-report", "/styleguide/report", full=True),
]
