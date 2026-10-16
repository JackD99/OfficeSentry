"""The client switcher beside a client's name (``_macros.tenant_header``): the page being read, for each
other client the signed-in person sees, narrowed to the group they chose (``groups.scoped_tenants``).

Those tenants are the ones where the page's declared capabilities hold (``access.visible_tenants`` reads
what the view declared). A page about one of a client's objects has no counterpart on another client, so it
opens that kind's list instead; a report another client doesn't have (an add-on's) opens the Reports tab, as
does one this person can't read on that client (``access.can_open``: a capability taken away there) when they
read any report there, and any other page that doesn't open there opens the client's overview, so every link
opens."""

from __future__ import annotations

from dataclasses import dataclass

from flask import current_app, g, request, url_for

from ..reports import registry
from .access import can_open, is_staff
from .groups import scoped_tenants
from .reports import report_groups

# An account, mailbox, group, site or emailed report is one client's own: the switcher opens its list.
LISTS = {
    "people.person": "people.people",
    "mailboxes.mailbox": "mailboxes.mailboxes",
    "group_pages.group": "group_pages.groups",
    "site_pages.site": "site_pages.sites",
    "email.issued_report": "email.report_history",
    "tenant_admin.offboard": "tenant_admin.manage",
}
# URL arguments that mean the same for every client: a report, a scheme, a report design.
SHARED = frozenset({"key", "scheme", "design_id"})
FALLBACK = "tenants.tenant"


@dataclass(frozen=True)
class Choice:
    name: str
    href: str
    current: bool


def _target(endpoint: str, args: dict) -> tuple[str, dict]:
    """The page to open on another client, and the URL arguments it keeps."""
    endpoint = LISTS.get(endpoint, endpoint)
    kept = {k: v for k, v in args.items() if k in SHARED}
    for rule in current_app.url_map.iter_rules(endpoint):
        if "GET" in rule.methods and rule.arguments == {"tenant_id", *kept}:
            return endpoint, kept
    return FALLBACK, {}


def choices(tenant_id: int) -> list[Choice]:
    """Every client to switch to (the current one marked), or nothing: for a client account, on a page
    that isn't this tenant's own (a sample header), or when there's no other client to go to."""
    args = request.view_args or {}
    if not is_staff() or args.get("tenant_id") != tenant_id or request.endpoint is None:
        return []
    tenants = scoped_tenants()
    if not any(t["id"] != tenant_id for t in tenants):
        return []
    endpoint, kept = _target(request.endpoint, args)
    report = registry.DECLARED.get(kept.get("key")) if "key" in kept else None
    out = []
    for t in tenants:
        if t["id"] == tenant_id:
            out.append(Choice(t["name"], request.path, True))
            continue
        here, extra = endpoint, kept
        missing = report is not None and report.shown is not None and not report.shown(g.db, t)
        if missing or not can_open(here, tenant_id=t["id"], **extra):
            here, extra = "reports.data_index" if report is not None and report_groups(t["id"]) else FALLBACK, {}
        out.append(Choice(t["name"], url_for(here, tenant_id=t["id"], **extra), False))
    return out
