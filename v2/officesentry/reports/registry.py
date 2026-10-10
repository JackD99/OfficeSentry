"""One declaration per detail report.

A builder decorated with ``@report(...)`` is a detail report: it gets a page, a
place in the menus and the report library, XLSX, CSV and PDF downloads, a
cross-tenant rollup, a place in What changed and in the email schedule form.
Nothing else needs to change::

    @report("mfa", "MFA coverage", "Who has set up MFA, who is required to use it, ...",
            ("users", "mfa_registration"), group="identity", order=10,
            keywords="multi-factor two-factor 2fa", uses=("insurance",))
    def mfa(snap, now, report): ...

``group`` is the menu group (a key of ``GROUP_TITLES``; none puts it under
"More reports") and ``order`` its place there, spaced by 10 so a new report
slots in between. ``keywords`` are words people search the library for that
aren't in the title or description, and ``uses`` adds it to jobs in
``library.USES``. A builder's module must be imported in ``reports/__init__.py``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

# Menu groups, in menu order.
GROUP_TITLES: tuple[tuple[str, str], ...] = (
    ("identity", "Identity and access"),
    ("email", "Email"),
    ("files", "Files and Teams"),
    ("devices", "Devices and apps"),
    ("tenant", "Tenant and licences"),
)

# The order ``REPORTS`` lists these reports in, which What changed follows within each area. It predates
# declarations; a report that isn't listed comes after these, in menu order, so new reports leave it alone.
LISTED_ORDER: tuple[str, ...] = (
    "mailboxes", "inbox-rules", "mailbox-permissions", "shared-mailboxes", "mailbox-size", "legacy-auth",
    "mailbox-holds", "admin-activity", "apps", "conditional-access", "tenant-settings", "domains", "guests",
    "deleted-items", "groups", "group-membership", "devices", "hybrid", "mfa", "auth-methods", "admins",
    "subscriptions", "licences", "usage", "licence-rightsizing", "inactive", "sign-ins", "risky-users",
    "secure-score", "sharepoint", "sharepoint-storage", "onedrive", "shared-files",
)


@dataclass(frozen=True)
class Declaration:
    key: str
    title: str
    description: str
    collectors: tuple[str, ...]
    builder: Callable
    group: str | None
    order: int
    uses: tuple[str, ...]
    keywords: str


@dataclass
class ReportGroup:
    key: str
    title: str
    reports: list[tuple[str, str, str]]  # (report key, title, description)


DECLARED: dict[str, Declaration] = {}
# Read by the rest of the app; rebuilt in place as reports are declared, so imports of them stay current.
REPORTS: dict[str, tuple[str, str, tuple[str, ...], Callable]] = {}  # key: (title, description, collectors, builder)
GROUPS: list[tuple[str, str, tuple[str, ...]]] = []  # (group key, title, report keys in menu order)
KEYWORDS: dict[str, str] = {}  # report key: extra search words for the library


def report(key: str, title: str, description: str, collectors: tuple[str, ...], *, group: str | None = None,
           order: int = 0, uses: tuple[str, ...] = (), keywords: str = ""):
    """Declare a detail report; see the module docstring."""
    if group is not None and group not in dict(GROUP_TITLES):
        raise ValueError(f"Report {key!r} names an unknown menu group {group!r}")

    def decorator(builder):
        if key in DECLARED:
            raise ValueError(f"Report {key!r} is declared twice")
        DECLARED[key] = Declaration(key, title, description, tuple(collectors), builder, group, order,
                                    tuple(uses), keywords)
        _rebuild()
        return builder
    return decorator


def _menu_position(d: Declaration) -> tuple[int, int]:
    groups = [g for g, _ in GROUP_TITLES]
    return (groups.index(d.group) if d.group else len(groups), d.order)


def _rebuild() -> None:
    listed = {k: i for i, k in enumerate(LISTED_ORDER)}
    ordered = sorted(DECLARED.values(), key=lambda d: (listed.get(d.key, len(listed)), _menu_position(d)))
    REPORTS.clear()
    REPORTS.update((d.key, (d.title, d.description, d.collectors, d.builder)) for d in ordered)
    GROUPS[:] = [(g, title, tuple(d.key for d in sorted(DECLARED.values(), key=_menu_position) if d.group == g))
                 for g, title in GROUP_TITLES]
    KEYWORDS.clear()
    KEYWORDS.update((d.key, d.keywords) for d in DECLARED.values() if d.keywords)


def declared_uses() -> dict[str, list[str]]:
    """Job key: report keys that name it in ``uses``, in menu order."""
    out: dict[str, list[str]] = {}
    for d in sorted(DECLARED.values(), key=_menu_position):
        for use in d.uses:
            out.setdefault(use, []).append(d.key)
    return out


def grouped() -> list[ReportGroup]:
    """Every detail report, once, under its menu group. Read at call time so new reports appear."""
    seen: set[str] = set()
    groups = []
    for key, title, keys in GROUPS:
        items = [(k, REPORTS[k][0], REPORTS[k][1]) for k in keys if k in REPORTS and k not in seen]
        seen.update(k for k, _, _ in items)
        if items:
            groups.append(ReportGroup(key, title, items))
    rest = [(k, v[0], v[1]) for k, v in REPORTS.items() if k not in seen]
    if rest:
        groups.append(ReportGroup("more", "More reports", rest))
    return groups
