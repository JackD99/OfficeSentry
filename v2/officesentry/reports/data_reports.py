"""Detail reports Microsoft doesn't give you in one place.

Each builder reads stored snapshots and returns titled tables; the page and
the XLSX export only format them. Nothing here calls Microsoft.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from urllib.parse import quote

from .. import adminactivity, coverage, devices, dnscheck, hybrid
from ..checks.apps import app_risks, custom_consent_policies, user_consent_level
from ..checks.groups import group_owner_problem, group_type, m365_groups
from ..checks.sharing import SHARING_LEVELS
from ..checks.tenant_settings import GUEST_ROLES, admin_consent_missing
from ..collectors import REGISTRY
from ..licences import last_sign_in
from ..mailflow import inbox_rule_risk, is_external, mailbox_forwarding, tenant_domains
from ..snapshot import TenantSnapshot
from ..text import plural, verb
from . import workbook
from .registry import GROUPS, REPORTS, ReportGroup, grouped, report  # noqa: F401  (read from here)
from .tables import Table


@dataclass(frozen=True)
class Chart:
    """A bar chart of a table's rows, declared on its ``Section`` and drawn after the table's filters apply.

    One bar per value of the ``by`` column (or "flag" for the flag column; each item of a list cell counts
    once), so the chart always matches the filtered table, and each bar opens the table filtered to it.
    ``series`` are number columns (counts, sizes or money) summed and stacked in each bar, with ``tones``
    from ``charts.TONE_FILL`` ("1", "2", "3", "bad"); with none, each bar counts rows. Bars are largest
    first unless ``ranked`` is False (the table's own order), and past ``top`` the rest are folded into
    "Other" (``other=False`` leaves them out). ``views.chart`` does the drawing.
    """

    by: str
    series: tuple[str, ...] = ()
    total: str = ""  # a number column to show as each bar's value, when the series don't add up to it
    rest: str = "Other"  # what the grey part for the rest of ``total`` is called
    title: str = ""  # defaults to "By <column>"
    tones: tuple[str, ...] = ("1", "2", "3")
    ranked: bool = True
    top: int = 8
    other: bool = True


@dataclass
class Section:
    title: str
    columns: list[str]
    rows: list[list] = field(default_factory=list)
    flags: list[str | None] = field(default_factory=list)  # severity per row, for highlighting
    intro: str = ""
    empty: str = "Nothing to show."
    # False for event logs (e.g. admin activity), which the change report shouldn't diff like state.
    compare: bool = True
    # Columns that drift every night with use (sizes, file and item counts), which What changed ignores like
    # day counts: a row that only grew isn't a change. A row crossing into or out of the table still is.
    drifts: tuple[str, ...] = ()
    # Page-only extras, used by the Findings page: what the flag column is called, a column whose
    # value groups rows under collapsible headings (with an anchor id per value), columns that always
    # get a value filter however many values they have, and an id per row for row actions.
    flag_name: str = "Flag"
    group: str = ""
    group_ids: dict[str, str] = field(default_factory=dict)
    filter_columns: tuple[str, ...] = ()
    keys: list = field(default_factory=list)
    review: bool = True  # False: rows already dealt with (accepted, resolved), so no "<n> to review"
    chart: Chart | None = None  # drawn from the rows the view leaves (views.chart)
    figure: str = ""  # the chart as drawn for a download, set by views.apply()

    def add(self, row: list, flag: str | None = None, key=None) -> None:
        self.rows.append([_cell(v) for v in row])
        self.flags.append(flag)
        if key is not None:
            self.keys.append(key)

    @property
    def flagged(self) -> int:
        return sum(1 for f in self.flags if f in ("critical", "high", "medium"))


@dataclass(frozen=True)
class Link:
    """A cell that links somewhere (a file in SharePoint, say). Only https addresses become links.
    ``page_href`` is set by ``link_pages`` when the address is a site with a page in Office Sentry: pages
    open that, while PDFs and spreadsheets keep the address."""

    text: str
    href: str
    page_href: str = ""

    def __str__(self) -> str:
        return self.text

    @property
    def safe_href(self) -> str | None:
        return self.href if self.href.lower().startswith("https://") else None


class PageLink(str):
    """A name that opens a page in Office Sentry: an account's, a mailbox's, a group's or a site's.

    A ``str``, so comparisons, sorting and exports treat it as the name. Pages link ``page_href``
    (a path in the portal); ``safe_href`` is None so PDFs and spreadsheets show the name alone.
    """

    safe_href = None

    def __new__(cls, text: str, href: str):
        obj = super().__new__(cls, text)
        obj.page_href = href
        return obj

    def __getnewargs__(self):  # so copies and pickles keep the link
        return (str(self), self.page_href)

    @property
    def text(self) -> str:
        return str(self)


class UserLink(PageLink):
    """An account's name that opens its page."""


def user_path(tenant_id: int, user_id: str) -> str:
    return f"/tenants/{tenant_id}/users/{quote(str(user_id), safe='')}"


# How other things with a page of their own are named in reports: each function returns {lower-cased name
# or address: page path} for one tenant's latest snapshot. Page modules add theirs on import
# (``mailbox_page.py``, ...), and ``build()`` links every cell that is exactly one of those names.
@dataclass(frozen=True)
class PageIndex:
    """How one kind of page's objects are named in reports: ``build(snap)`` gives lower-cased name -> page path.
    ``where(table title, column)`` says which cells may be a name (None: any cell); a bare display name like
    "Sales" is only linked in a column about groups, while an address is unmistakable anywhere."""

    build: Callable[[TenantSnapshot], dict[str, str]]
    where: Callable[[str, str], bool] | None = None


PAGE_INDEXES: list[PageIndex] = []  # one per kind of page (mailbox_page, group_page, ...)


def page_index(snap: TenantSnapshot) -> list[tuple[dict[str, str], Callable[[str, str], bool] | None]]:
    """Every kind of page's names and where they may be linked, for ``link_pages``."""
    out = []
    for index in PAGE_INDEXES:
        try:
            found = index.build(snap)
        except Exception:  # one kind of page's odd data mustn't take every report down
            logging.getLogger(__name__).exception("Page index %s failed", index.build.__module__)
            continue
        if found:
            out.append((found, index.where))
    return out


def link_pages(sections: list[Section], indexes: list[tuple[dict[str, str], Callable | None]]) -> None:
    """Turn every plain-text cell that is exactly a name in an index into a link to that page, and every
    ``Link`` to an address in one (a site) into a link to its page. People and files are left alone, and a
    name is only linked where its index says it may be."""
    if not indexes:
        return
    for section in sections:
        lookups: list[dict[str, str]] = []
        for column in section.columns:
            allowed = [names for names, where in indexes if where is None or where(section.title, column)]
            lookups.append({k: v for names in reversed(allowed) for k, v in names.items()})
        for row in section.rows:
            for i, v in enumerate(row):
                if i >= len(lookups):
                    continue
                if type(v) is str and (href := lookups[i].get(v.lower())):
                    row[i] = PageLink(v, href)
                elif type(v) is Link and not v.page_href and (href := lookups[i].get(v.href.lower().rstrip("/"))):
                    row[i] = replace(v, page_href=href)


def people_index(snap: TenantSnapshot) -> dict[str, str]:
    """Account object IDs by lower-cased sign-in name and email address."""
    out: dict[str, str] = {}
    for u in snap.objects("users", "user") or []:
        for key in ("userPrincipalName", "mail"):
            if isinstance(u.get(key), str) and u[key]:
                out.setdefault(u[key].lower(), u["_id"])
    return out


def link_people(sections: list[Section], tenant_id: int, index: dict[str, str]) -> None:
    """Turn every cell that is exactly an account's sign-in name or address into a link to its page."""
    if not index:
        return
    for section in sections:
        for row in section.rows:
            for i, v in enumerate(row):
                if type(v) is str and (uid := index.get(v.lower())):
                    row[i] = UserLink(v, user_path(tenant_id, uid))


class Many(str):
    """A list cell ("A, B, C") that still knows its items, so a value filter can offer each one."""

    parts: tuple[str, ...] = ()

    def __new__(cls, items):
        parts = tuple(str(v) for v in items)
        obj = super().__new__(cls, ", ".join(parts))
        obj.parts = parts
        return obj


@dataclass
class Rows:
    """The rows behind a headline figure: a table, optionally filtered, that the figure links to.

    ``where`` maps column names to a value (or a list of values, any of which match);
    ``age`` is (date column, preset), a key of ``views.AGES`` such as "gt90";
    ``flagged`` keeps the rows flagged for review.
    """

    table: str
    where: dict = field(default_factory=dict)
    age: tuple[str, str] | None = None
    flagged: bool = False


@dataclass
class Trend:
    """A headline figure's history for its sparkline: one value per day with a collection, oldest first."""

    values: list[float]
    first: str  # the oldest value as people read it, for the sparkline's title
    last: str
    since: str  # "12 Jul 2026"

    @property
    def label(self) -> str:
        return f"Since {self.since}: from {self.first} to {self.last}"


TREND_DAYS = 90


def metric_trend(
    snap: TenantSnapshot, key: str, now: datetime, fmt: Callable[[float], str] | None = None, days: int = TREND_DAYS
) -> Trend | None:
    """The last ``days`` of a metric (``ctx.metric`` keys), the day's last value each day, for a sparkline.

    None with fewer than three days of history: a line through two points says nothing a delta doesn't.
    """
    since = (now - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    daily: dict[str, float] = {}
    for captured, value in snap.history(key, since):
        daily[captured[:10]] = value
    if len(daily) < 3:
        return None
    fmt = fmt or (lambda v: f"{v:,.0f}" if float(v).is_integer() else f"{v:,.1f}")
    first = min(daily)
    values = [daily[d] for d in sorted(daily)]
    day = datetime.fromisoformat(first)
    return Trend(values, fmt(values[0]), fmt(values[-1]), f"{day.day} {day:%b %Y}")


@dataclass
class Tile:
    """A headline figure above a report's tables. ``tone``: ok, warn or bad."""

    label: str
    value: object
    foot: str = ""
    tone: str | None = None
    rows: Rows | None = None  # what clicking it shows; a figure named like a table links to that table
    trend: Trend | None = None  # a sparkline of its history (``metric_trend()``)


@dataclass
class Highlight:
    """A key figure shown above the tables, so the page leads with the answer."""

    label: str
    value: object
    foot: str = ""
    tone: str | None = None  # bad | warn | ok, for the dot next to the label
    rows: Rows | None = None  # what clicking it shows, as for Tile
    trend: Trend | None = None  # a sparkline of its history, as for Tile


@dataclass
class DataReport:
    key: str
    title: str
    description: str
    tenant: dict
    generated_at: datetime
    sections: list[Section] = field(default_factory=list)
    unavailable: str | None = None
    sources: list[tuple[str, str | None]] = field(default_factory=list)  # (collector title, finished_at)
    tiles: list[Tile] = field(default_factory=list)
    notice: str = ""  # something to fix before the data is complete, shown above everything
    chart: str = ""  # server-side SVG
    chart_title: str = ""
    footnote: str = ""
    highlights: list[Highlight] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)  # what couldn't be checked, and why
    filtered: list[str] = field(default_factory=list)  # the table view a download applies, in words


def _cell(value):
    if value is None:
        return ""
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, (list, tuple, set)):
        return Many(value)
    return value


def _date(value: str | None) -> str:
    return value[:10] if value else ""


# -- Mailboxes ------------------------------------------------------------------


@report(
    "mailboxes",
    "Mail forwarding",
    "Every mailbox that sends mail on automatically, and the settings that allow it.",
    ("mailboxes", "inbox_rules"),
    group="email",
    order=10,
    keywords="forwarding redirect external smtp",
)
def mailboxes(snap: TenantSnapshot, now: datetime, report: DataReport) -> None:
    boxes = snap.objects("mailboxes", "mailbox")
    rules = snap.objects("inbox_rules", "inbox_rule")
    if boxes is None and rules is None:
        report.unavailable = (
            "Mailbox data hasn't been collected yet. Refresh Mailboxes and Inbox rules on the client page."
        )
        return
    status = snap.objects("mailboxes", "exchange_status")
    if status and not status[0].get("available"):
        report.unavailable = "This tenant has no Exchange Online licence."
        return
    domains = tenant_domains(snap) or set()

    fwd = Section(
        "Forwarding",
        ["Mailbox", "Forwards to", "Set by", "Outside the organisation", "Keeps a copy"],
        intro="Every place mail leaves a mailbox automatically: admin-set forwarding and inbox rules.",
        empty="No mailbox forwards mail.",
    )
    for m in boxes or []:
        target = mailbox_forwarding(m)
        if target:
            external = is_external(target, domains)
            fwd.add(
                [
                    m.get("PrimarySmtpAddress"),
                    target,
                    "Admin (mailbox setting)",
                    external,
                    m.get("DeliverToMailboxAndForward"),
                ],
                "high" if external else None,
            )
    for r in sorted(rules or [], key=lambda r: ((r.get("mailbox") or "").lower(), r.get("sequence") or 0)):
        risk = inbox_rule_risk(r, domains)
        for target in risk.forwards_to:
            external = target in risk.external
            fwd.add(
                [r.get("mailbox"), target, f'Inbox rule "{r.get("displayName")}"', external, True],
                ("high" if r.get("isEnabled", True) else "low") if external else None,
            )
    outside = sum(1 for row in fwd.rows if row[3] == "Yes")
    report.highlights = [
        Highlight(
            "Forwarding outside",
            outside,
            "Admin-set or by inbox rule",
            "bad" if outside else "ok",
            Rows("Forwarding", {"Outside the organisation": "Yes"}),
        ),
        Highlight(
            "Forwarding inside",
            len(fwd.rows) - outside,
            "To colleagues in the organisation",
            rows=Rows("Forwarding", {"Outside the organisation": "No"}),
        ),
    ]

    sections = [fwd]
    policies = snap.objects("mailboxes", "outbound_spam_policy")
    remote = snap.objects("mailboxes", "remote_domain")
    if policies is not None or remote is not None:
        modes = {"On": "Allowed", "Off": "Blocked", "Automatic": "Blocked (Microsoft's default)"}
        controls = Section(
            "What stops forwarding",
            ["Setting", "Applies to", "Automatic forwarding outside"],
            intro="Organisation settings that allow or block automatic forwarding to outside "
            "addresses. The strictest one wins.",
            empty="No settings found.",
        )
        allowed = False
        for p in sorted(policies or [], key=lambda p: (not p.get("IsDefault"), str(p.get("Name") or ""))):
            on = p.get("AutoForwardingMode") == "On"
            allowed |= on
            controls.add(
                [
                    f"Outbound spam policy: {p.get('Name')}",
                    "Everyone" if p.get("IsDefault") else "Mailboxes it's assigned to",
                    modes.get(p.get("AutoForwardingMode"), p.get("AutoForwardingMode")),
                ],
                "medium" if on else None,
            )
        for d in sorted(remote or [], key=lambda d: str(d.get("DomainName") or "")):
            target = "Any outside domain" if d.get("DomainName") == "*" else d.get("DomainName")
            controls.add(
                [f"Remote domain: {d.get('Name')}", target, "Allowed" if d.get("AutoForwardEnabled") else "Blocked"]
            )
        report.highlights.append(
            Highlight(
                "Automatic forwarding",
                "Allowed" if allowed else "Blocked",
                "By the outbound spam policy",
                "warn" if allowed else "ok",
                Rows("What stops forwarding"),
            )
        )
        sections.append(controls)
    report.sections = sections


def _type(value: str | None) -> str:
    return {
        "UserMailbox": "User",
        "SharedMailbox": "Shared",
        "RoomMailbox": "Room",
        "EquipmentMailbox": "Equipment",
    }.get(value or "", value or "")


def _describe_conditions(c: dict) -> str:
    parts = []
    for key, label in (("fromAddresses", "from"), ("sentToAddresses", "sent to")):
        addrs = [((a or {}).get("emailAddress") or {}).get("address") for a in c.get(key) or []]
        if addrs:
            parts.append(f"{label} {', '.join(a for a in addrs if a)}")
    for key, label in (
        ("subjectContains", "subject has"),
        ("bodyContains", "body has"),
        ("bodyOrSubjectContains", "subject or body has"),
        ("senderContains", "sender has"),
    ):
        if c.get(key):
            parts.append(f"{label} {', '.join(c[key])}")
    return "; ".join(parts) or "All messages"


def _describe_actions(r: dict) -> str:
    a = r.get("actions") or {}
    parts = []
    for key, label in (
        ("forwardTo", "forward to"),
        ("redirectTo", "redirect to"),
        ("forwardAsAttachmentTo", "forward as attachment to"),
    ):
        addrs = [((x or {}).get("emailAddress") or {}).get("address") for x in a.get(key) or []]
        if addrs:
            parts.append(f"{label} {', '.join(x for x in addrs if x)}")
    if a.get("moveToFolder"):
        parts.append(f"move to {r.get('moveToFolderName') or 'a folder'}")
    if a.get("delete") or a.get("permanentDelete"):
        parts.append("delete")
    if a.get("markAsRead"):
        parts.append("mark as read")
    if a.get("stopProcessingRules"):
        parts.append("stop processing rules")
    return "; ".join(parts) or "Other"


# -- Apps ----------------------------------------------------------------------------


@report(
    "apps",
    "Apps and consents",
    "Third-party apps with access to the tenant, and app credential expiry.",
    ("app_consents",),
    group="devices",
    order=20,
    keywords="enterprise applications oauth consent secrets certificates expiry",
)
def apps(snap: TenantSnapshot, now: datetime, report: DataReport) -> None:
    risks = app_risks(snap)
    if risks is None:
        report.unavailable = "App data hasn't been collected yet. Refresh Apps and consents on the client page."
        return
    third = Section(
        "Third-party apps with access",
        ["App", "Publisher", "Application permissions", "Consented for everyone", "Consented by users", "Risk"],
        intro="Apps from outside Microsoft that hold permissions in this tenant. Application permissions "
        "work without anyone signed in.",
        empty="No third-party app holds permissions.",
    )
    for a in risks:
        publisher = a["publisher"] + (
            " (verified)" if a["verified"] else (" (unverified)" if a["publisher"] else "Unverified")
        )
        users = f"{plural(a['users'], 'user')}: {', '.join(a['user_scopes'])}" if a["user_scopes"] else ""
        third.add(
            [a["name"], publisher, a["app_permissions"], a["tenant_scopes"], users, ", ".join(a["risky"]) or "Low"],
            a["severity"],
        )

    regs = Section(
        "App registrations and their credentials",
        ["App", "Created", "Credentials", "Next expiry", "Status"],
        intro="Apps registered in this tenant. Expired secrets usually mean an integration has stopped "
        "working, or the app is no longer used.",
        empty="No app registrations.",
    )
    soon = now.timestamp() + 30 * 86400
    registrations = snap.objects("app_consents", "application") or []
    for a in sorted(registrations, key=lambda a: (a.get("displayName") or "").lower()):
        creds = a.get("credentials") or []
        ends = sorted(c["endDateTime"] for c in creds if devices.parse_ts(c.get("endDateTime")))
        future = [e for e in ends if _ts(e) >= now.timestamp()]
        if not creds:
            status, flag = "No credentials", None
        elif not future:
            status, flag = "All expired", "low"
        elif _ts(future[-1]) < soon:
            status, flag = "Expiring soon", "medium"
        else:
            status, flag = "OK", None
        kinds = ", ".join(
            plural(sum(c["type"] == k for c in creds), k)
            for k in ("secret", "certificate")
            if any(c["type"] == k for c in creds)
        )
        regs.add(
            [
                a.get("displayName"),
                _date(a.get("createdDateTime")),
                kinds or "None",
                _date(future[0]) if future else (_date(ends[-1]) + " (expired)" if ends else ""),
                status,
            ],
            flag,
        )
    report.sections = [third, regs]


def _ts(iso: str) -> float:
    return devices.parse_ts(iso).timestamp()


# -- Admin activity ----------------------------------------------------------------------


def _when(iso: str | None) -> str:
    return f"{iso[:10]} {iso[11:16]}" if iso and len(iso) >= 16 else (iso or "")


def _targets(e: dict) -> str:
    names = ", ".join(e.get("targets") or [])
    return names + (f" and {e['more_targets']} more" if e.get("more_targets") else "")


@report(
    "admin-activity",
    "Admin activity",
    "Admin roles, Conditional Access, app credentials, consents, domains and account changes "
    "from the directory audit log, and who made them.",
    ("admin_activity",),
    group="tenant",
    order=70,
    keywords="audit log directory changes who changed",
)
def admin_activity(snap: TenantSnapshot, now: datetime, report: DataReport) -> None:
    events = snap.objects("admin_activity", "admin_event")
    window = (snap.objects("admin_activity", "audit_window") or [None])[0]
    if events is None:
        report.unavailable = "Admin activity hasn't been collected yet. Refresh Admin activity on the client page."
        return
    if window and not window.get("available", True):
        report.unavailable = f"The tenant's directory audit log couldn't be read: {window.get('reason') or ''}"
        return
    covered = adminactivity.window_text(window)
    end = devices.parse_ts(snap.as_of) or now
    history = adminactivity.from_history(
        snap.conn, snap.tenant["id"], end - timedelta(days=adminactivity.HISTORY_DAYS), end
    )
    if history is not None:
        # The activity history goes back further than the snapshot's 30 days once it has been kept a while.
        events, cov = history
        start = datetime.fromtimestamp(cov.detailed_from, UTC)
        span = (end - start).days
        if span > adminactivity.covered_days(window) + 1:
            covered = (
                "the last 12 months"
                if span >= adminactivity.HISTORY_DAYS
                else f"the {span} days since {start:%d %b %Y}, when Office Sentry started keeping this "
                "tenant's audit log"
            )
    events = sorted(events, key=lambda e: e.get("time") or "", reverse=True)

    review = Section(
        "Changes to review",
        ["When (UTC)", "Change", "By", "Target", "Detail", "Why it matters"],
        compare=False,
        intro=f"Changes in {covered} that are worth confirming with the client: domain sign-in "
        "(federation) changes, new app secrets and certificates, admin roles granted, and "
        "Conditional Access policies deleted or switched off. Failed attempts are left out.",
        empty="No change in the window needs a second look.",
    )
    every = Section(
        "All admin changes",
        ["When (UTC)", "Area", "Change", "By", "Target", "Detail", "Result", "IP address"],
        compare=False,
        intro=f"Every admin role, Conditional Access, policy, app, domain and account change "
        f"in {covered}, newest first.",
        empty="No admin changes in the window.",
    )
    actors: dict[str, dict] = {}
    for e in events:
        flag = adminactivity.review(e)
        if flag:
            review.add(
                [_when(e.get("time")), e.get("activity"), e.get("actor"), _targets(e), e.get("detail"), flag[1]],
                flag[0],
            )
        result = (
            "Succeeded"
            if e.get("result") == "success"
            else ("Failed" + (f": {e['reason']}" if e.get("reason") else ""))
        )
        every.add(
            [
                _when(e.get("time")),
                e.get("area"),
                e.get("activity"),
                e.get("actor"),
                _targets(e),
                e.get("detail"),
                result,
                e.get("ip"),
            ],
            flag[0] if flag else None,
        )
        a = actors.setdefault(e.get("actor") or "Unknown", {"type": e.get("actor_type"), "count": 0, "last": ""})
        a["count"] += 1
        a["last"] = max(a["last"], e.get("time") or "")
    review.rows, review.flags = _by_flag(review)

    who = Section(
        "Who made changes",
        ["Who", "Type", "Changes", "Last change (UTC)"],
        compare=False,
        intro="Everyone (and every app) that made one of the changes above, most active first.",
        empty="Nobody made admin changes in the window.",
    )
    for name, a in sorted(actors.items(), key=lambda kv: (-kv[1]["count"], kv[0].lower())):
        who.add([name, {"user": "User", "app": "App"}.get(a["type"], ""), a["count"], _when(a["last"])])
    report.sections = [review, every, who]


# -- Conditional Access coverage -------------------------------------------------------


@report(
    "conditional-access",
    "Conditional Access coverage",
    "Every policy in plain words, and which accounts each one actually covers.",
    ("conditional_access", "admin_roles", "users"),
    group="identity",
    order=40,
    keywords="ca policies policy require mfa block",
)
def conditional_access(snap: TenantSnapshot, now: datetime, report: DataReport) -> None:
    defaults = snap.objects("conditional_access", "security_defaults")
    d = coverage.directory(snap)
    cov = coverage.user_coverage(snap)
    if defaults is None or d is None or cov is None:
        report.unavailable = "Conditional Access data hasn't been collected yet."
        return
    policies = snap.objects("conditional_access", "ca_policy") or []
    if defaults and defaults[0].get("isEnabled"):
        report.sections.append(
            Section(
                "Security Defaults",
                ["Setting", "Value"],
                rows=[["Security Defaults", "On: MFA registration and sign-in prompts apply to everyone"]],
                flags=[None],
            )
        )
    order = {"enabled": 0, "enabledForReportingButNotEnforced": 1, "disabled": 2}
    pol = Section(
        "Policies",
        ["Policy", "State", "Applies to", "Except", "Apps", "Conditions", "Requires"],
        intro="Every policy in plain words, enforced ones first.",
        empty="No Conditional Access policies."
        + (" The tenant has no Entra ID P1." if snap.capabilities.get("entra_p1") is False else ""),
    )
    for p in sorted(policies, key=lambda p: (order.get(p.get("state"), 3), (p.get("displayName") or "").lower())):
        row = coverage.describe(p, d, snap.capabilities)
        state = f"{row.state}, but never applies (needs Entra ID P2)" if row.warning else row.state
        pol.add(
            [row.name, state, row.users, row.excluded, row.apps, row.conditions, row.grant],
            "medium" if row.warning else None,
        )
    report.sections.append(pol)

    covered = sum(c.mfa for c in cov)
    sd = bool(defaults and defaults[0].get("isEnabled"))
    accounts = Section(
        "Coverage by account",
        ["Account", "Type", "Admin roles", "MFA required by", "Legacy sign-in blocked", "Excluded from"],
        intro=(
            f"{covered} of {plural(len(cov), 'enabled account')} {verb(covered, 'is', 'are')} "
            "covered by an enforced policy that "
            "requires MFA for all apps." + (" Security Defaults also apply." if sd else "")
        ),
        empty="No enabled accounts.",
    )
    for c in cov:
        if c.mfa or sd:
            flag = None
        else:
            flag = "high" if c.roles else ("low" if c.user_type == "Guest" else "medium")
        accounts.add(
            [
                c.label,
                c.user_type,
                c.roles,
                c.mfa_policies or ("Security Defaults" if sd else "Nothing"),
                c.legacy_blocked or sd,
                c.excluded_from,
            ],
            flag,
        )
    report.sections.append(accounts)


# -- Guests and sharing -----------------------------------------------------------


INVITES = {
    "none": "Nobody",
    "adminsAndGuestInviters": "Admins and Guest Inviters",
    "adminsGuestInvitersAndAllMembers": "Admins and members",
    "everyone": "Everyone, including guests",
}


@report(
    "guests",
    "Guests and sharing",
    "Who can invite and share externally, and every guest account.",
    ("tenant_policies", "users"),
    group="identity",
    order=80,
    keywords="external users b2b invitations",
)
def guests(snap: TenantSnapshot, now: datetime, report: DataReport) -> None:
    users = snap.objects("users", "user")
    policy = (snap.objects("tenant_policies", "authorization_policy") or [None])[0]
    sharepoint = (snap.objects("tenant_policies", "sharepoint_settings") or [None])[0]
    if users is None and policy is None:
        report.unavailable = "User and sharing data hasn't been collected yet."
        return
    settings = Section("External access settings", ["Setting", "Value"])
    if policy:
        settings.add(
            ["Who can invite guests", INVITES.get(policy.get("allowInvitesFrom"), policy.get("allowInvitesFrom"))],
            "low" if policy.get("allowInvitesFrom") == "everyone" else None,
        )
        consent = user_consent_level(policy)
        custom = consent == "limited" and custom_consent_policies(policy)
        settings.add(
            [
                "User consent to apps",
                "Custom policy: " + ", ".join(custom)
                if custom
                else {"any": "Any app", "limited": "Verified publishers, low-risk permissions", "off": "Off"}[consent],
            ],
            "medium" if consent == "any" else "low" if custom else None,
        )
        settings.add(["Users can register apps", policy.get("allowedToCreateApps")])
    if sharepoint:
        level = sharepoint.get("sharingCapability")
        settings.add(
            ["SharePoint and OneDrive sharing", SHARING_LEVELS.get(level, level)],
            "medium" if level == "externalUserAndGuestSharing" else None,
        )
        mode = sharepoint.get("sharingDomainRestrictionMode")
        if mode == "allowList":
            settings.add(["Sharing limited to domains", sharepoint.get("sharingAllowedDomainList") or []])
        elif mode == "blockList":
            settings.add(["Sharing blocked for domains", sharepoint.get("sharingBlockedDomainList") or []])
        settings.add(["Guests can reshare", sharepoint.get("isResharingByExternalUsersEnabled")])
    sections = [settings]
    if users is not None:
        activity = any(u.get("signInActivityAvailable") for u in users)
        guest = Section(
            "Guest accounts",
            ["Guest", "Invited", "Invitation", "Last sign-in", "Sign-in allowed"],
            intro="Every external account, oldest activity first."
            + ("" if activity else " Sign-in dates need Entra ID P1."),
            empty="No guest accounts.",
        )
        rows = [u for u in users if u.get("userType") == "Guest"]
        rows.sort(key=lambda u: (last_sign_in(u) or datetime.min.replace(tzinfo=UTC), u.get("createdDateTime") or ""))
        cutoff = now.timestamp() - 90 * 86400
        for u in rows:
            last = last_sign_in(u)
            pending = u.get("externalUserState") == "PendingAcceptance"
            stale = activity and u.get("accountEnabled") and (last is None or last.timestamp() < cutoff)
            guest.add(
                [
                    u.get("mail") or u.get("userPrincipalName"),
                    _date(u.get("createdDateTime")),
                    "Not accepted" if pending else "Accepted",
                    last.date().isoformat() if last else ("Never" if activity else "Unknown"),
                    u.get("accountEnabled"),
                ],
                "low" if stale or pending else None,
            )
        sections.append(guest)
    report.sections = sections


# -- Tenant settings --------------------------------------------------------------------

GUEST_ACCESS = {
    "member": "Same as members",
    "limited": "Limited (the default)",
    "restricted": "Only their own objects (most restrictive)",
}


@report(
    "tenant-settings",
    "Tenant settings",
    "What users and guests can do by default, and whether users can ask an admin to approve apps.",
    ("tenant_policies",),
    group="tenant",
    order=60,
    keywords="defaults user consent authorization policy",
)
def tenant_settings(snap: TenantSnapshot, now: datetime, report: DataReport) -> None:
    policy = (snap.objects("tenant_policies", "authorization_policy") or [None])[0]
    if policy is None:
        report.unavailable = (
            "Tenant settings haven't been collected yet. Refresh Sharing and consent settings on the client page."
        )
        return

    settings = Section(
        "Settings",
        ["Setting", "Value", "Recommended"],
        intro="What users and guests can do without an admin. Most of these are allowed by default.",
    )

    def add(label: str, key: str, recommended: bool, severity: str | None = None) -> None:
        v = policy.get(key)
        flag = severity if v is not None and v != recommended else None
        settings.add([label, "Not reported" if v is None else v, recommended], flag)

    add("Users can create tenants", "allowedToCreateTenants", False, "low")
    add("Users can register apps", "allowedToCreateApps", False, "low")
    guest_role = policy.get("guestUserRoleId")
    level = GUEST_ROLES.get(guest_role)
    settings.add(
        [
            "Guest access to the directory",
            GUEST_ACCESS[level] if level else (f"Unrecognised ({guest_role})" if guest_role else "Not reported"),
            "Limited or most restrictive",
        ],
        "medium" if level == "member" else None,
    )
    add("People can join by verifying an email address", "allowEmailVerifiedUsersToJoinOrganization", False, "low")
    add("Users can sign up for trials and free subscriptions", "allowedToSignUpEmailBasedSubscriptions", False, "low")
    add("Users can create security groups", "allowedToCreateSecurityGroups", False)
    add("Users can look up other users in the directory", "allowedToReadOtherUsers", True)
    add("Self-service password reset allowed", "allowedToUseSSPR", True)
    add("Legacy MSOnline PowerShell blocked", "blockMsolPowerShell", True)

    consent = Section(
        "Admin consent requests",
        ["Setting", "Value", "Recommended"],
        intro="Whether users who can't consent to an app themselves can ask an admin to approve it.",
        empty=admin_consent_missing(policy) + ".",
    )
    workflow = (snap.objects("tenant_policies", "admin_consent_policy") or [None])[0]
    if workflow:
        on = bool(workflow.get("isEnabled"))
        stuck = not on and user_consent_level(policy) == "off"  # same as the admin_consent_workflow check
        consent.add(["Admin consent requests", "On" if on else "Off", "On"], "low" if stuck else None)
        if on:
            reviewers = len(workflow.get("reviewers") or [])
            consent.add(["Reviewers", reviewers, "At least one"], None if reviewers else "low")
            consent.add(["Reviewers are emailed about new requests", workflow.get("notifyReviewers"), True])
            consent.add(["Reviewers get reminders", workflow.get("remindersEnabled"), True])
            days = workflow.get("requestDurationInDays")
            consent.add(["Requests expire after", f"{days} days" if days else "Not set", "30 days (the default)"])
    report.sections = [settings, consent]


# -- Email domains --------------------------------------------------------------------


@report(
    "domains",
    "Email domains",
    "SPF, DMARC, DKIM, MTA-STS and TLS-RPT for each of the tenant's domains, from public DNS.",
    ("domain_health",),
    group="email",
    order=70,
    keywords="spf dkim dmarc dns mta-sts email authentication spoofing",
)
def domains(snap: TenantSnapshot, now: datetime, report: DataReport) -> None:
    rows = snap.objects("domain_health", "domain")
    if not rows:
        report.unavailable = "Domain checks haven't run yet, or the tenant has no custom domains."
        return
    overview = Section(
        "Domains",
        ["Domain", "Receives mail via", "SPF", "DMARC", "DKIM", "MTA-STS", "TLS-RPT", "What to fix"],
        intro="SPF, DMARC and DKIM stop other people sending email as the client's domains. "
        "MTA-STS and TLS-RPT make other servers deliver mail to them encrypted, and report "
        "when they can't.",
    )
    records = Section(
        "Published records",
        ["Domain", "SPF record", "DNS lookups", "DMARC record", "MTA-STS policy", "TLS-RPT record", "MX"],
    )
    healths = [dnscheck.DomainHealth.from_dict(row) for row in rows]
    for h in sorted(healths, key=lambda h: (not h.sends_mail, h.domain)):
        found = dnscheck.issues(h, transport=True)
        words = dnscheck.summary(h)
        delivery = dnscheck.transport_summary(h)
        via = (
            "Microsoft 365"
            if h.microsoft_365
            else (h.mx[0] if h.sends_mail else dnscheck.UNKNOWN if "MX" in h.failed else "Doesn't receive mail")
        )
        what = "" if "MX" in h.failed else f" for {' and '.join(h.failed)}"
        fix = [i.detail for i in found] + (
            [f"The DNS lookup{what} failed; the next collection tries again"] if h.failed else []
        )
        overview.add(
            [
                h.domain,
                via,
                words["SPF"],
                words["DMARC"],
                words["DKIM"],
                delivery["MTA-STS"],
                delivery["TLS-RPT"],
                "; ".join(fix) or "Nothing",
            ],
            found[0].severity if found else None,
        )
        records.add(
            [
                h.domain,
                h.spf or "None",
                h.spf_lookups if h.spf else "",
                h.dmarc or "None",
                _mta_sts_text(h),
                h.tls_rpt or ("None" if h.transport_checked else "Not checked"),
                h.mx if h.sends_mail or not h.mx else "Null MX (accepts no mail)",
            ]
        )
    report.sections = [overview, records]


# -- Groups and Teams --------------------------------------------------------------------

VISIBILITY = {"Public": "Public", "Private": "Private", "HiddenMembership": "Hidden membership"}
OTHER_GROUPS = (
    ("security", "Security groups"),
    ("mail_security", "Mail-enabled security groups"),
    ("distribution", "Distribution lists"),
)
FLAG_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4, None: 5}


def _owner_names(g: dict) -> str:
    if not g.get("examined"):
        return "Not checked"
    names = [
        (o.get("name") or "?") + ("" if o.get("enabled") else " (app)" if o.get("type") != "user" else " (disabled)")
        for o in g.get("owners") or []
    ]
    more = (g.get("ownerCount") or 0) - len(names)
    return ", ".join(names + ([f"{more} more"] if more > 0 else [])) or "None"


def _group_settings_section(snap: TenantSnapshot) -> Section:
    settings = (snap.objects("groups", "group_settings") or [{}])[0]
    policy = (snap.objects("groups", "expiration_policy") or [{}])[0]
    section = Section("Settings", ["Setting", "Value"])
    if settings.get("available"):
        if settings.get("source") == "default":
            section.intro = "No group settings have been saved in this tenant, so Microsoft's defaults apply."
        allowed = settings.get("allowedGroupName") or settings.get("allowedGroupId")
        if settings.get("enableGroupCreation"):
            section.add(["Who can create teams and groups", "Everyone"], "low")
        else:
            section.add(
                ["Who can create teams and groups", f"Admins and members of {allowed}" if allowed else "Admins only"]
            )
        section.add(["Guests can access group content", settings.get("allowGuestsToAccessGroups")])
        section.add(["Owners can add guests", settings.get("allowToAddGuests")])
        section.add(["Usage guidelines", settings.get("usageGuidelinesUrl") or "None"])
    else:
        section.add(["Who can create teams and groups", "Couldn't be read"])
    if policy.get("available"):
        days, managed = policy.get("groupLifetimeInDays"), policy.get("managedGroupTypes")
        if managed == "All":
            section.add(["Expiration", f"After {days} days unless in use or renewed"])
        elif managed == "Selected":
            section.add(["Expiration", f"After {days} days, selected groups only"], "low")
        else:
            section.add(
                ["Expiration", "Off: teams and groups are kept until someone deletes them"],
                "info" if snap.capabilities.get("entra_p1") is False else "low",
            )
        if policy.get("alternateNotificationEmails"):
            section.add(["Expiry notices for groups without an owner go to", policy["alternateNotificationEmails"]])
    else:
        section.add(["Expiration", "Couldn't be read"])
    return section


@report(
    "groups",
    "Groups and Teams",
    "Who can create teams and groups, which have no owner, and the guests in each.",
    ("groups",),
    group="files",
    order=50,
    keywords="teams microsoft 365 groups owners ownerless",
)
def group_report(snap: TenantSnapshot, now: datetime, report: DataReport) -> None:
    rows = snap.objects("groups", "group")
    if rows is None:
        report.unavailable = "Group data hasn't been collected yet. Refresh Groups and Teams on the client page."
        return
    groups = sorted(m365_groups(snap) or [], key=lambda g: (g.get("displayName") or "").lower())
    checked = [g for g in groups if g.get("examined")]

    attention = Section(
        "Needs attention",
        ["Team or group", "Type", "Problem", "Owners"],
        intro="Teams and groups without an enabled owner, or with only one. Owners approve members "
        "and guests and renew the group.",
        empty="Every team and group has at least two enabled owners.",
    )
    every = Section(
        "Teams and groups",
        ["Name", "Team", "Visibility", "Owners", "Members", "Guests", "Created"],
        intro="Every team and Microsoft 365 group."
        + (f" Owners and members were read for {len(checked)} of {len(groups)}." if len(checked) < len(groups) else ""),
        empty="The tenant has no teams or Microsoft 365 groups.",
    )
    outside = Section(
        "Guests in teams and groups",
        ["Guest", "Team or group", "Type"],
        intro="People from outside the organisation who are members of a team or group, and can read "
        "its files and conversations.",
        empty="No team or group has guest members.",
    )
    needs = []
    for g in groups:
        name, kind = g.get("displayName") or g["_id"], group_type(g)
        problem = group_owner_problem(g)
        flag = ("medium" if g.get("isTeam") else "low") if problem else None
        if problem:
            needs.append(([name, kind, problem, _owner_names(g)], flag))
        elif g.get("examined") and g.get("enabledOwnerCount") == 1:
            needs.append(([name, kind, "Single owner", _owner_names(g)], "info"))
        examined = g.get("examined")
        every.add(
            [
                name,
                g.get("isTeam"),
                VISIBILITY.get(g.get("visibility"), g.get("visibility") or ""),
                _owner_names(g),
                g.get("memberCount") if examined else "",
                g.get("guestCount") if examined else "",
                _date(g.get("createdDateTime")),
            ],
            flag,
        )
        for guest in g.get("guests") or []:
            outside.rows.append([guest, name, kind])
        if (g.get("guestCount") or 0) > len(g.get("guests") or []):
            outside.rows.append([plural(g["guestCount"] - len(g.get("guests") or []), "more guest"), name, kind])
    for row, flag in sorted(needs, key=lambda n: (FLAG_ORDER[n[1]], n[0][0].lower())):
        attention.add(row, flag)
    outside.rows.sort(key=lambda r: (r[0].lower(), r[1].lower()))
    outside.flags = [None] * len(outside.rows)

    other = Section(
        "Other groups",
        ["Kind", "Groups", "Synced from on-premises", "Dynamic membership"],
        intro="Groups that aren't teams or Microsoft 365 groups. They grant access and deliver mail, "
        "but have no shared files or conversations.",
        empty="No other groups.",
    )
    for kind, label in OTHER_GROUPS:
        of_kind = [g for g in rows if g.get("kind") == kind]
        if of_kind:
            other.add(
                [
                    label,
                    len(of_kind),
                    sum(bool(g.get("synced")) for g in of_kind),
                    sum(bool(g.get("dynamic")) for g in of_kind),
                ]
            )
    report.sections = [_group_settings_section(snap), attention, every, outside, other]


def _mta_sts_text(h: dnscheck.DomainHealth) -> str:
    if not h.transport_checked:
        return "Not checked"
    if not h.mta_sts:
        return "None"
    text = f"id={h.mta_sts_id}" if h.mta_sts_id else "Record without id="
    if h.mta_sts_records > 1:
        text += f" (and {plural(h.mta_sts_records - 1, 'more record')})"
    if h.mta_sts_error:
        return f"{text}; {h.mta_sts_error}"
    if h.mta_sts_mode:
        text += f"; mode: {h.mta_sts_mode}; mx: {', '.join(h.mta_sts_mx) or 'none'}; max_age: {h.mta_sts_max_age}"
    return text


# -- Devices -----------------------------------------------------------------------------

COMPLIANCE = {
    "compliant": "Compliant",
    "noncompliant": "Not compliant",
    "inGracePeriod": "Grace period",
    "conflict": "Conflict",
    "error": "Error",
    "unknown": "Unknown",
    "configManager": "Config Manager",
}
JOIN_TYPES = {"AzureAd": "Entra joined", "ServerAd": "Hybrid joined", "Workplace": "Registered (personal)"}
SUPPORT = {
    "supported": "Supported",
    "ending": "Ending soon",
    "pro_ended": "Ended (Home/Pro)",
    "ended": "Ended",
    "unknown": "Unknown",
}


def _device_issues(d: dict, now: datetime) -> tuple[list[str], str | None]:
    issues, flag = [], None
    rank = {"high": 0, "medium": 1, "low": 2}

    def add(text: str, severity: str) -> None:
        nonlocal flag
        issues.append(text)
        if flag is None or rank[severity] < rank[flag]:
            flag = severity

    if str(d.get("jailBroken")).lower() == "true":
        add("Jailbroken or rooted", "high")
    if d.get("complianceState") == "noncompliant":
        add("Not compliant", "medium")
    if devices.is_computer(d) and d.get("isEncrypted") is False:
        add("Not encrypted", "high")
    if devices.is_windows(d):
        support = devices.windows_support(d.get("osVersion"), now.date())
        if support.status in ("ended", "pro_ended", "ending"):
            add(support.detail.rstrip("."), {"ended": "high", "pro_ended": "medium", "ending": "low"}[support.status])
    days = devices.days_since(d.get("lastSyncDateTime"), now)
    if days is not None and days > devices.NO_SYNC_DAYS:
        add(f"No check-in for {days} days", "low")
    return issues, flag


def _seen(value: str | None, now: datetime) -> str:
    days = devices.days_since(value, now)
    if days is None:
        return "Never"
    # Always a date first, so the last-30-days filter and date sorting read "today" too.
    return f"{_date(value)} (today)" if days == 0 else f"{_date(value)} ({days}d)"


@report(
    "devices",
    "Devices",
    "Intune compliance, encryption and Windows support, and every Entra ID device record.",
    ("devices",),
    group="devices",
    order=10,
    keywords="intune compliance bitlocker encryption windows",
)
def device_report(snap: TenantSnapshot, now: datetime, report: DataReport) -> None:
    entra = devices.objects(snap, "entra_device")
    if entra is None:
        report.unavailable = "Device data hasn't been collected yet. Refresh Devices on the client page."
        return
    status = devices.objects(snap, "intune_status") or []
    intune = bool(status and status[0].get("available"))
    managed = (devices.objects(snap, "managed_device") or []) if intune else []

    # Operating systems: Intune's view when there is one, otherwise devices active in Entra.
    if managed:
        population, version_key = managed, "osVersion"
    else:
        # A device that has never signed in hasn't signed in within the period.
        ages = ((d, devices.days_since(d.get("approximateLastSignInDateTime"), now)) for d in entra)
        population = [
            d for d, days in ages if d.get("accountEnabled") and days is not None and days <= devices.STALE_DAYS
        ]
        version_key = "operatingSystemVersion"
    counts: dict[tuple[str, str], int] = {}
    for d in population:
        if devices.is_windows(d):
            support = devices.windows_support(d.get(version_key), now.date())
            key = (support.release, support.status)
        else:
            key = (d.get("operatingSystem") or "Unknown", "")
        counts[key] = counts.get(key, 0) + 1
    systems = Section(
        "Operating systems",
        ["Operating system", "Devices", "Security updates"],
        intro=(
            "Devices enrolled in Intune."
            if managed
            else f"Devices that signed in to Entra ID in the last {devices.STALE_DAYS} days."
        ),
        empty="No devices.",
    )
    flags = {"ended": "high", "pro_ended": "medium", "ending": "low"}
    for (name, state), n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0][0])):
        systems.add([name, n, SUPPORT.get(state, "")], flags.get(state))
    sections = [systems]

    if intune:
        attention = Section(
            "Needs attention",
            ["Device", "User", "Operating system", "Problems"],
            intro="Managed devices that aren't compliant, aren't encrypted, run an unsupported "
            "Windows release or have stopped checking in.",
            empty="Every managed device is compliant, encrypted, supported and checking in.",
        )
        every = Section(
            "Managed devices",
            [
                "Device",
                "User",
                "Owner",
                "Operating system",
                "Compliance",
                "Encrypted",
                "Last check-in",
                "Enrolled",
                "Model",
            ],
            empty="No devices are enrolled in Intune.",
        )
        every.chart = Chart("Compliance")
        for d in sorted(managed, key=lambda d: (d.get("deviceName") or "").lower()):
            issues, flag = _device_issues(d, now)
            if issues:
                attention.add(
                    [d.get("deviceName"), d.get("userPrincipalName"), devices.os_label(d), "; ".join(issues)], flag
                )
            every.add(
                [
                    d.get("deviceName"),
                    d.get("userPrincipalName"),
                    (d.get("managedDeviceOwnerType") or "").capitalize(),
                    devices.os_label(d),
                    COMPLIANCE.get(d.get("complianceState"), d.get("complianceState")),
                    d.get("isEncrypted") if devices.is_computer(d) else "",
                    _seen(d.get("lastSyncDateTime"), now),
                    _date(d.get("enrolledDateTime")),
                    " ".join(p for p in (d.get("manufacturer"), d.get("model")) if p),
                ],
                flag,
            )
        attention.rows, attention.flags = _by_flag(attention)
        sections += [attention, every]

    records = Section(
        "Entra ID device records",
        ["Device", "Operating system", "Join type", "In Intune", "Last seen", "Registered", "Enabled"],
        intro="Every device known to Entra ID, least recently seen first. "
        + ("" if intune else "The tenant has no Intune, so these are the only device data."),
        empty="No devices are registered in Entra ID.",
    )
    for d in sorted(entra, key=lambda d: d.get("approximateLastSignInDateTime") or ""):
        days = devices.days_since(d.get("approximateLastSignInDateTime"), now)
        stale = d.get("accountEnabled") and days is not None and days > devices.STALE_DAYS
        records.add(
            [
                d.get("displayName"),
                devices.os_label(d),
                JOIN_TYPES.get(d.get("trustType"), d.get("trustType") or "Unknown"),
                d.get("isManaged"),
                _seen(d.get("approximateLastSignInDateTime"), now),
                _date(d.get("registrationDateTime")),
                d.get("accountEnabled"),
            ],
            "low" if stale else None,
        )
    sections.append(records)
    report.sections = sections


def _by_flag(section: Section) -> tuple[list, list]:
    order = {"critical": 0, "high": 1, "medium": 2, "low": 3, None: 4}
    pairs = sorted(zip(section.rows, section.flags, strict=True), key=lambda p: order[p[1]])
    return [p[0] for p in pairs], [p[1] for p in pairs]


# -- Hybrid identity ---------------------------------------------------------------------


def _synced_at(value: str | None, now: datetime) -> str:
    hours = hybrid.hours_since(value, now)
    return "Never" if hours is None else f"{value[:16].replace('T', ' ')} UTC ({hybrid.age(hours)})"


@report(
    "hybrid",
    "Hybrid identity",
    "Directory sync from on-premises AD, password hash sync, how each domain signs in, "
    "and admin accounts synced from AD.",
    ("directory_sync", "users", "admin_roles"),
    group="identity",
    order=110,
    keywords="ad connect entra connect sync on-premises password hash",
)
def hybrid_report(snap: TenantSnapshot, now: datetime, report: DataReport) -> None:
    status = hybrid.sync_status(snap)
    if status is None:
        report.unavailable = "Directory sync data hasn't been collected yet. Refresh Directory sync on the client page."
        return
    users = snap.objects("users", "user")
    enabled = bool(status.get("enabled"))

    sync = Section("Sync status", ["Setting", "Value"])
    if not enabled and status.get("onPremisesSyncEnabled") is False:
        sync.intro = (
            "Cloud-only tenant: directory sync was turned off, so accounts that came from on-premises "
            "Active Directory are now managed in Microsoft 365."
        )
        sync.add(["Directory sync", "Turned off"])
    elif not enabled:
        sync.intro = (
            "Cloud-only tenant: accounts are created and managed in Microsoft 365, not synced from an "
            "on-premises Active Directory."
        )
        sync.add(["Directory sync", "Never turned on"])
    else:
        sync.intro = (
            "Accounts are synced from on-premises Active Directory, normally every 30 minutes by Entra Connect."
        )
        sync.add(["Directory sync", "On"])
        problem = hybrid.sync_problem(status, now)
        sync.add(["Last directory sync", _synced_at(status.get("lastSyncDateTime"), now)], problem and problem[0])
        if status.get("passwordSyncChecked"):
            problem = hybrid.password_sync_problem(status, now)
            sync.add(
                ["Last password hash sync", _synced_at(status.get("lastPasswordSyncDateTime"), now)],
                problem and problem[0],
            )
        else:
            sync.add(["Last password hash sync", "Couldn't be read"])
    federated = hybrid.federated_domains(snap)
    sync.add(["Federated domains", federated or "None"])

    domains = Section(
        "Domains",
        ["Domain", "Sign-in", "Verified", "Default", "Used for"],
        intro="Managed domains sign in through Entra ID (checking synced passwords in the cloud, or "
        "on-premises with pass-through authentication). Federated domains send sign-in to "
        "another identity provider, usually AD FS: if it is down, those users can't sign in.",
        empty="No domains.",
    )
    rows = snap.objects("directory_sync", "domain") or []
    for d in sorted(rows, key=lambda d: (not d.get("isDefault"), d["_id"].endswith(".onmicrosoft.com"), d["_id"])):
        domains.add(
            [
                d["_id"],
                d.get("authenticationType"),
                d.get("isVerified"),
                d.get("isDefault"),
                d.get("supportedServices") or [],
            ]
        )
    sections = [sync, domains]

    synced_users = [u for u in users or [] if u.get("onPremisesSyncEnabled")]
    if enabled or synced_users:
        admins = Section(
            "Synced admin accounts",
            ["Account", "Roles", "Enabled"],
            intro="Admins whose account comes from on-premises AD. Anyone who takes over AD can reset "
            "their password and sign in as them, so admin roles belong on cloud-only accounts.",
            empty="No admin account is synced from on-premises AD.",
        )
        found = hybrid.synced_admins(snap)
        if found is None:
            admins.empty = "Admin role and user data haven't been collected yet."
        for a in found or []:
            admins.add([a["label"], a["roles"], a["accountEnabled"]], "high" if a["accountEnabled"] else "medium")
        sections.append(admins)

    source = Section(
        "Accounts by source",
        ["Accounts", "Synced from AD", "Cloud-only", "Total"],
        intro="Synced accounts are managed in Active Directory: changes made to them in Microsoft 365 "
        "are overwritten at the next sync.",
        empty="User data hasn't been collected yet.",
    )
    if users is not None:
        for label, kind in (("Members", "Member"), ("Guests", "Guest"), ("All accounts", None)):
            group = [u for u in users if kind is None or (u.get("userType") or "Member") == kind]
            synced = sum(bool(u.get("onPremisesSyncEnabled")) for u in group)
            source.add([label, synced, len(group) - synced, len(group)])
    sections.append(source)
    report.sections = sections


# -- Registry ----------------------------------------------------------------------------

Builder = Callable[[TenantSnapshot, datetime, DataReport], None]


def build(
    conn,
    tenant,
    key: str,
    now: datetime | None = None,
    as_of: datetime | None = None,
    snap: TenantSnapshot | None = None,
) -> DataReport:
    """Build one detail report from the latest data, or from the data held at ``as_of``."""
    title, description, collectors, builder = REPORTS[key]
    now = as_of or now or datetime.now(UTC)
    snap = snap or TenantSnapshot(conn, tenant, as_of=as_of)
    report = DataReport(key, title, description, dict(tenant), now)
    report.sources = [
        (REGISTRY[c].title, (snap.runs.get(c) or {"finished_at": None})["finished_at"]) for c in collectors
    ]
    try:
        builder(snap, now, report)
    except Exception as exc:  # show the page with a reason rather than an error
        logging.getLogger(__name__).exception("Report %s failed", key)
        report.sections = []
        report.unavailable = f"This report couldn't be built: {type(exc).__name__}"
    if as_of is None:  # comparisons of older data don't need links
        link_people(report.sections, report.tenant["id"], people_index(snap))
        link_pages(report.sections, page_index(snap))
    elif snap.as_of:
        _no_longer_held(conn, snap, collectors, report)
    return report


def _no_longer_held(conn, snap: TenantSnapshot, collectors, report: DataReport) -> None:
    """On a past day, say which data was collected by then but isn't held any more (sign-in detail, which
    retention keeps for less time than the daily history), rather than that it was never collected."""
    from .. import retention, state_history  # here: the history imports the worker's schedule

    def held(collector: str) -> bool:
        run = snap.runs.get(collector)
        if run is None:  # collected by then, or not?
            return not conn.execute(
                "SELECT 1 FROM runs WHERE tenant_id = ? AND collector = ? AND status IN ('succeeded', 'partial')"
                " AND finished_at <= ? LIMIT 1",
                (report.tenant["id"], collector, snap.as_of),
            ).fetchone()
        return collector not in retention.SIGNIN_COLLECTORS or bool(
            conn.execute(f"SELECT 1 FROM runs WHERE id = ? AND {state_history.HOLDS_OBJECTS}", (run["id"],)).fetchone()
        )

    gone = [REGISTRY[c].title for c in collectors if not held(c)]
    if not gone:
        return
    note = (
        f"{' and '.join(gone)} from that day {verb(len(gone), 'is', 'are')} no longer held: the retention "
        f"policy keeps {'it' if len(gone) == 1 else 'them'} for less time than the daily history."
    )
    if report.unavailable or not report.sections:
        report.unavailable = note
    else:
        report.notice = f"{report.notice} {note}".strip()


def section_table(section: Section) -> Table:
    flagged = any(section.flags)
    rows = [
        [str(v) if isinstance(v, Link) else v for v in row] + ([flag or ""] if flagged else [])
        for row, flag in zip(section.rows, section.flags or [None] * len(section.rows), strict=False)
    ]
    return Table(section.title, section.columns + ([section.flag_name] if flagged else []), rows)


def to_tables(report: DataReport) -> list[Table]:
    if not report.sections:
        return [Table("No data", ["Note"], [[report.unavailable or "Nothing to show."]])]
    return [section_table(s) for s in report.sections]


# Flag cell colours in XLSX (background, text), matching the severity colours on the page.
FLAG_FILLS = workbook.FLAG_FILLS


def xlsx_cover(report: DataReport, brand=None) -> workbook.Cover:
    """The Summary sheet of a detail report's workbook: what it is, for whom, and its headline figures."""
    facts = [(t.label, t.value, t.foot) for t in report.tiles + report.highlights]
    notes = [("Note", report.notice)] if report.notice else []
    notes += [("Not checked", note) for note in report.notes]
    notes += [("Filtered view", line) for line in report.filtered]
    if report.unavailable:
        notes.append(("Not available", report.unavailable))
    done = [finished for _, finished in report.sources if finished]
    if done:
        last = datetime.fromisoformat(max(done).replace("Z", "+00:00"))
        notes.append(("Data collected", f"{last.day} {last:%b %Y, %H:%M} UTC"))
    return workbook.Cover(
        report.title,
        report.tenant.get("name", ""),
        report.generated_at,
        brand.display_name if brand else "",
        report.tenant.get("tenant_ref", ""),
        brand.accent if brand else "",
        report.description,
        facts,
        notes,
    )


def to_xlsx(report: DataReport, brand=None) -> bytes:
    book = workbook.Book(brand.accent if brand else "")
    for section in report.sections or [Section("No data", ["Note"], rows=[[report.unavailable or "Nothing to show."]])]:
        links = {
            (i, j): v.safe_href
            for i, row in enumerate(section.rows)
            for j, v in enumerate(row)
            if isinstance(v, Link) and v.safe_href
        }
        rows = [[str(v) if isinstance(v, Link) else v for v in row] for row in section.rows]
        book.table(
            section.title,
            section.columns,
            rows,
            flags=section.flags,
            links=links,
            description=section.intro,
            flag_name=section.flag_name,
        )
    return book.save(xlsx_cover(report, brand))
