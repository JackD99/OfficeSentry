"""Scheduled email delivery: what gets sent, to whom, and when.

* **Report schedules** send one tenant a bundle of reports (the monthly review,
  the insurance evidence pack, any detail report) weekly, monthly or quarterly,
  at a time in the schedule's own timezone.
* **Digests** send the MSP's own team one cross-tenant summary: the tenant
  digest, or (``reports`` is ``["upcoming"]``) the Coming up email of what falls
  due in the next 90 days (``upcoming.py``).
* **Alert emails** go out when a collection opens findings at or above a
  severity: the same event (``alerts.after_sync``) that posts to Teams, Slack
  or a webhook. Failed ones are retried by ``tick()``.

``tick()`` is the only entry point the worker needs. It is idempotent and
cheap, so it can run every minute: each schedule remembers the period it last
completed ("2026-10", "2026-W41", "2026-Q4"), and failures are retried with
backoff before the period is given up and shown as failed. In the worker it
queues each due email as a ``send_schedule`` task (``tasks.py``) rather than
building it, so a month-end morning of 200 reports never holds up the
worker's main loop.

Recipients are contacts. A tenant's contacts get the email in "To"; the MSP's
team (contacts with no tenant) get a hidden copy, so client contacts never see
internal addresses.
"""

from __future__ import annotations

import copy
import json
import logging
import re
from contextlib import nullcontext
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

from . import alerts, issued, mailer, schedule, store, tasks, tenant_groups, upcoming
from .checks import RULES
from .db import utcnow
from .mailer import Email, MailError
from .reports import (
    branding,
    data_reports,
    designs,
    export,
    findings_report,
    monthly_review,
    rollups,
    scheme_export,
    schemes,
    schemes_cis,  # noqa: F401  (registers the schemes)
    schemes_cyber_essentials,  # noqa: F401
    schemes_essential_eight,  # noqa: F401
    schemes_forms,  # noqa: F401
    schemes_iasme,  # noqa: F401
    schemes_iso27001,  # noqa: F401
    schemes_nist_csf,  # noqa: F401
    schemes_soc2,  # noqa: F401
    schemes_uk_gdpr,  # noqa: F401
    summary,
    tables,
    views,
    workbook,
)
from .text import day, plural

log = logging.getLogger(__name__)

ISO = "%Y-%m-%dT%H:%M:%SZ"
FREQUENCIES = {"weekly": "Weekly", "monthly": "Monthly", "quarterly": "Quarterly"}
DAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
ALERT_LEVELS = {"critical": ("critical",), "high": ("critical", "high")}
RETRY_DELAYS = (timedelta(minutes=5), timedelta(minutes=30), timedelta(hours=2))
MAX_ATTEMPTS = len(RETRY_DELAYS) + 1
DIGEST_LOOKBACK = {"weekly": timedelta(days=7), "monthly": timedelta(days=30), "quarterly": timedelta(days=91)}
RULE_TITLES = {r.key: r.title for r in RULES}
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
CSV = "text/csv; charset=utf-8"
VIEW_FORMATS = {"pdf": "PDF", "xlsx": "XLSX", "csv": "CSV"}
VIEWS_KEY = "views"  # a digest schedule whose reports is ["views"]: saved views across tenants, team only

# Offered in the schedule form; the server's own timezone and the schedule's
# current one are always added.
COMMON_TIMEZONES = (
    "UTC",
    "Europe/London",
    "Europe/Dublin",
    "Europe/Amsterdam",
    "Europe/Berlin",
    "Europe/Paris",
    "Europe/Madrid",
    "Europe/Stockholm",
    "Europe/Helsinki",
    "Africa/Johannesburg",
    "Asia/Dubai",
    "Asia/Kolkata",
    "Asia/Singapore",
    "Asia/Hong_Kong",
    "Asia/Tokyo",
    "Australia/Perth",
    "Australia/Brisbane",
    "Australia/Adelaide",
    "Australia/Sydney",
    "Australia/Melbourne",
    "Pacific/Auckland",
    "America/St_Johns",
    "America/Halifax",
    "America/New_York",
    "America/Toronto",
    "America/Chicago",
    "America/Denver",
    "America/Phoenix",
    "America/Los_Angeles",
    "America/Vancouver",
    "America/Anchorage",
    "Pacific/Honolulu",
)


# -- What can be sent ---------------------------------------------------------------


@dataclass(frozen=True)
class ReportOption:
    key: str
    title: str
    description: str
    pdf: bool
    xlsx: bool


def catalog(conn=None) -> list[ReportOption]:
    """Every report a schedule can include. Detail reports registered later appear automatically, and with
    ``conn`` the MSP's report designs (``design:<id>``, PDF only) too."""
    options = [
        ReportOption(
            "review",
            monthly_review.TITLE,
            "Key figures, what needs attention, what changed and licence waste.",
            True,
            True,
        ),
        ReportOption(
            monthly_review.SUMMARY_KEY,
            monthly_review.SUMMARY_TITLE,
            "The monthly review's two summary pages, without the appendix of detail.",
            True,
            False,
        ),
        ReportOption(
            "evidence",
            "Cyber insurance evidence pack",
            "The controls cyber insurers ask about, judged from the data, with the answers for the form.",
            True,
            True,
        ),
    ]
    if conn is not None:
        for row in designs.list_designs(conn):
            options.append(
                ReportOption(
                    designs.key_of(row["id"]),
                    row["name"],
                    row["description"] or "A report design: blocks you chose, as a PDF.",
                    True,
                    False,
                )
            )
    for key, (title, description, _collectors, _builder) in data_reports.REPORTS.items():
        options.append(ReportOption(f"data:{key}", title, description, False, True))
    return options


def catalog_map(conn=None) -> dict[str, ReportOption]:
    return {o.key: o for o in catalog(conn)}


def report_titles(reports_json: str, conn=None) -> list[str]:
    known = catalog_map(conn)
    return [known[k].title for k in json.loads(reports_json or "[]") if k in known]


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "tenant"


def attachment_plan(tenant, keys: list[str], include_xlsx: bool, conn=None) -> list[tuple[str, str]]:
    """(report key, filename) for every file a bundle attaches, in order. Builds nothing. A report design
    (``design:<id>``) is a PDF, so it needs WeasyPrint and ``conn`` to be known."""
    slug = _slug(tenant["name"])
    pdf = export.pdf_available()
    known = catalog_map(conn)
    out = []
    for key in keys:
        if key not in known:
            continue
        if key in ("review", "evidence"):
            stem = "security-review" if key == "review" else "security-evidence"
            if pdf:
                out.append((key, f"{stem}-{slug}.pdf"))
            if include_xlsx or not pdf:
                out.append((key, f"{stem}-{slug}.xlsx"))
        elif key == monthly_review.SUMMARY_KEY:
            if pdf:
                out.append((key, f"{monthly_review.SUMMARY_STEM}-{slug}.pdf"))
        elif designs.id_of(key) is not None:
            if pdf:
                out.append((key, f"{_slug(known[key].title)}-{slug}.pdf"))
        else:
            out.append((key, f"{key[5:]}-{slug}.xlsx"))
    return out


# -- Saved views ---------------------------------------------------------------------------
#
# A schedule can send people's saved views (web/saved_views.py): a report with its filters, hidden columns
# and sort, as a PDF, XLSX or CSV (one file per table the view shapes). A report schedule sends them for its
# tenant; a views schedule (a digest whose reports is ["views"]) stacks every tenant's rows, or one group's,
# like the All tenants "every row" page, and goes to the team only.


@dataclass(frozen=True)
class ScheduledView:
    report_key: str  # a detail report key, or findings_report.KEY / RESOLVED_KEY (tenant schedules only)
    name: str
    query: str
    format: str  # a key of VIEW_FORMATS
    saved_view_id: int | None = None
    id: int | None = None  # its email_schedule_views row, once saved

    @property
    def report_title(self) -> str:
        return view_report_title(self.report_key)

    @property
    def title(self) -> str:
        return f"{self.report_title}: {self.name}"

    def view(self) -> views.View:
        return views.View.parse(self.query)


def view_report_title(key: str) -> str:
    if key in findings_report.TITLES:
        return findings_report.TITLES[key]
    return data_reports.REPORTS[key][0] if key in data_reports.REPORTS else key


def can_schedule_view(key: str, across: bool = False) -> bool:
    """Whether a saved view of report ``key`` can be emailed: any detail report, and for one tenant the
    Findings pages too (they have no every-tenant version)."""
    return key in data_reports.REPORTS or (not across and key in findings_report.TITLES)


def schedule_views(conn, schedule_id: int | None) -> list[ScheduledView]:
    if not schedule_id:
        return []
    return [
        ScheduledView(r["report_key"], r["name"], r["query"], r["format"], r["saved_view_id"], r["id"])
        for r in conn.execute(
            "SELECT * FROM email_schedule_views WHERE schedule_id = ? ORDER BY position, id", (schedule_id,)
        )
    ]


def set_schedule_views(conn, schedule_id: int, chosen: list[ScheduledView]) -> None:
    conn.execute("DELETE FROM email_schedule_views WHERE schedule_id = ?", (schedule_id,))
    conn.executemany(
        "INSERT INTO email_schedule_views (schedule_id, saved_view_id, report_key, name, query, format, position)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        [(schedule_id, v.saved_view_id, v.report_key, v.name, v.query, v.format, i) for i, v in enumerate(chosen)],
    )


def view_format(fmt: str) -> str:
    """The format a view is sent in: a PDF needs WeasyPrint, so without it the spreadsheet goes instead."""
    return "xlsx" if fmt == "pdf" and not export.pdf_available() else fmt


def view_tables(v: ScheduledView) -> list[str]:
    """The slugs of the tables a view filters, sorts or hides columns of, in the order it names them."""
    return list(dict.fromkeys(value.partition("/")[0] for _, value in v.view().params()))


def view_plan(v: ScheduledView, stem: str) -> list[tuple[str, str]]:
    """(table slug or "", filename) for each file a view attaches. Builds nothing: a CSV is one file per
    table the view shapes."""
    fmt = view_format(v.format)
    if fmt != "csv":
        return [("", f"{stem}.{fmt}")]
    return [(t, f"{stem}-{t}.csv") for t in view_tables(v)]


def view_stem(v: ScheduledView, who: str) -> str:
    return f"{tables.slug(v.report_key)}-{tables.slug(v.name)}-{tables.slug(who)}"


def view_attachments(who: str, chosen: list[ScheduledView]) -> list[tuple[ScheduledView, str]]:
    """(view, filename) for every file the views attach, in order. ``who`` is the tenant or group name."""
    return [(v, name) for v in chosen for _, name in view_plan(v, view_stem(v, who))]


def view_report(conn, tenant, v: ScheduledView, now: datetime) -> data_reports.DataReport:
    """One tenant's report with the view applied, as its page, PDF and downloads show it."""
    if v.report_key in findings_report.TITLES:
        report = findings_report.build(conn, tenant, resolved=v.report_key == findings_report.RESOLVED_KEY, now=now)
    else:
        report = data_reports.build(conn, tenant, v.report_key, now)
    return views.apply(report, v.view())


def view_files(app, conn, report, v: ScheduledView, stem: str, brand=None) -> list[tuple[str, str, bytes]]:
    """The files for one view of an already viewed ``report``: the names ``view_plan`` promised (a CSV for a
    table the report no longer has is left out)."""
    files = []
    for table, filename in view_plan(v, stem):
        if filename.endswith(".pdf"):
            from .web.printing import cap_rows  # the web package imports this module

            files.append((filename, "application/pdf", _pdf(app, "reports/detail.html", report=cap_rows(report))))
        elif filename.endswith(".xlsx"):
            files.append((filename, XLSX, data_reports.to_xlsx(report, brand=brand or branding.load(conn))))
        else:
            slugs = views.table_slugs(report)
            if table in slugs:
                section = report.sections[slugs.index(table)]
                files.append((filename, CSV, tables.to_csv(data_reports.section_table(section))))
    return files


# -- When ------------------------------------------------------------------------------


@lru_cache(maxsize=1)
def _timezones() -> frozenset[str]:
    return frozenset(available_timezones())


def valid_timezone(name: str) -> bool:
    return bool(name) and name in _timezones()


def schedule_tz(schedule, default_tz: str) -> str:
    tz = schedule["timezone"] or default_tz
    try:
        ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError):
        return "UTC"
    return tz


def schedule_cron(frequency: str, day_of_week: int, day_of_month: int, hour: int) -> str:
    """A schedule's send time as a cron expression for ``schedule.py`` (cron counts Sunday as 0)."""
    if frequency == "weekly":
        return f"0 {hour} * * {(day_of_week + 1) % 7}"
    if frequency == "monthly":
        return f"0 {hour} {day_of_month} * *"
    return f"0 {hour} {day_of_month} 1,4,7,10 *"


def period_key(frequency: str, d: date) -> str:
    if frequency == "weekly":
        iso_year, week, _ = d.isocalendar()
        return f"{iso_year}-W{week:02d}"
    if frequency == "monthly":
        return f"{d.year}-{d.month:02d}"
    return f"{d.year}-Q{(d.month - 1) // 3 + 1}"


def slot(
    frequency: str, day_of_week: int, day_of_month: int, hour: int, tz: str, now: datetime
) -> tuple[str, datetime]:
    """The latest send time at or before ``now`` (UTC) and the period it belongs to.

    The schedule is due while its ``last_period`` isn't that period. Only the
    latest slot counts, so a worker that was down sends once, not once per
    missed period.
    """
    due = schedule.previous_fire(schedule_cron(frequency, day_of_week, day_of_month, hour), tz, now)
    return period_key(frequency, due.astimezone(ZoneInfo(tz)).date()), due


def period_done(last: str | None, period: str) -> bool:
    """Whether a schedule whose last finished period is ``last`` has done ``period``. Keys of one frequency
    sort in time order, so a period that was sent ahead of its slot (approved early) counts as done too."""
    if not last:
        return False

    def kind(key: str) -> str:  # "2026-10" monthly, "2026-W41" weekly, "2026-Q4" quarterly
        return "M" if key[5:6].isdigit() else key[5:6]

    return last == period or (kind(last) == kind(period) and last > period)


def schedule_slot(s, default_tz: str, now: datetime) -> tuple[str, datetime]:
    return slot(s["frequency"], s["day_of_week"], s["day_of_month"], s["hour"], schedule_tz(s, default_tz), now)


def initial_period(frequency: str, day_of_week: int, day_of_month: int, hour: int, tz: str, now: datetime) -> str:
    """A new or edited schedule counts its latest send time as done, so saving never sends straight away."""
    return slot(frequency, day_of_week, day_of_month, hour, tz, now)[0]


def next_send(s, default_tz: str, now: datetime | None = None) -> datetime | None:
    """When the schedule will next send, in its own timezone. None when paused."""
    if not s["enabled"]:
        return None
    now = now or datetime.now(UTC)
    tz = schedule_tz(s, default_tz)
    key, due = schedule_slot(s, default_tz, now)
    if not period_done(s["last_period"], key):  # due now, or waiting for a retry
        if s["retry_at"]:
            due = datetime.fromisoformat(s["retry_at"].replace("Z", "+00:00"))
        return due.astimezone(ZoneInfo(tz))
    cron = schedule_cron(s["frequency"], s["day_of_week"], s["day_of_month"], s["hour"])
    return schedule.next_fire(cron, tz, now).astimezone(ZoneInfo(tz))


def ordinal(n: int) -> str:
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def describe(s) -> str:
    at = f"at {s['hour']:02d}:00"
    if s["frequency"] == "weekly":
        return f"Every {DAYS[s['day_of_week']]} {at}"
    if s["frequency"] == "monthly":
        return f"Monthly on the {ordinal(s['day_of_month'])} {at}"
    return f"Quarterly on the {ordinal(s['day_of_month'])} of Jan, Apr, Jul and Oct {at}"


def period_label(period: str) -> str:
    """What an email calls its period: "October 2026", "week of 5 Oct 2026" or "Q4 2026" (a ``period_key``)."""
    year, rest = period[:4], period[5:]
    if rest.startswith("W"):
        monday = date.fromisocalendar(int(year), int(rest[1:]), 1)
        return f"week of {monday.day} {monday.strftime('%b %Y')}"
    if rest.startswith("Q"):
        return f"{rest} {year}"
    return date(int(year), int(rest), 1).strftime("%B %Y")


def email_period(schedule, default_tz: str, now: datetime) -> tuple[str, date]:
    """The period an email built at ``now`` is for, and the local day of its send time: the latest send time,
    not the clock. A retry that runs after midnight on the 1st, or a failed email sent again next week, still
    names the period it belongs to and carries that month's commentary."""
    period, due = schedule_slot(schedule, default_tz, now)
    return period, due.astimezone(ZoneInfo(schedule_tz(schedule, default_tz))).date()


def nothing_collected(conn, tenant_id: int) -> bool:
    """No collection has ever finished for the tenant, so a report would be empty."""
    return (
        conn.execute(
            "SELECT 1 FROM runs WHERE tenant_id = ? AND status IN ('succeeded', 'partial') LIMIT 1", (tenant_id,)
        ).fetchone()
        is None
    )


# -- Who ---------------------------------------------------------------------------------


def tenant_contacts(conn, tenant_id: int) -> list:
    return conn.execute(
        "SELECT * FROM contacts WHERE tenant_id = ? ORDER BY name COLLATE NOCASE, email", (tenant_id,)
    ).fetchall()


def team_contacts(conn) -> list:
    return conn.execute("SELECT * FROM contacts WHERE tenant_id IS NULL ORDER BY name COLLATE NOCASE, email").fetchall()


def add_contact(conn, tenant_id: int | None, name: str, email: str) -> int:
    """Add a contact, or return the existing one with that address (filling in a missing name)."""
    existing = conn.execute(
        "SELECT * FROM contacts WHERE IFNULL(tenant_id, 0) = ? AND email = ?", (tenant_id or 0, email)
    ).fetchone()
    if existing:
        if name and not existing["name"]:
            conn.execute("UPDATE contacts SET name = ? WHERE id = ?", (name, existing["id"]))
        return existing["id"]
    return conn.execute(
        "INSERT INTO contacts (tenant_id, name, email, created_at) VALUES (?, ?, ?, ?)",
        (tenant_id, name, email, utcnow()),
    ).lastrowid


def recipients(conn, owner: str, owner_id: int) -> list:
    table, col = (
        ("email_schedule_recipients", "schedule_id") if owner == "schedule" else ("email_alert_recipients", "alert_id")
    )
    return conn.execute(
        f"SELECT c.* FROM contacts c JOIN {table} r ON r.contact_id = c.id WHERE r.{col} = ?"
        " ORDER BY c.tenant_id IS NULL, c.name COLLATE NOCASE, c.email",
        (owner_id,),
    ).fetchall()


def set_recipients(conn, owner: str, owner_id: int, contact_ids: list[int]) -> None:
    table, col = (
        ("email_schedule_recipients", "schedule_id") if owner == "schedule" else ("email_alert_recipients", "alert_id")
    )
    conn.execute(f"DELETE FROM {table} WHERE {col} = ?", (owner_id,))
    conn.executemany(
        f"INSERT OR IGNORE INTO {table} ({col}, contact_id) VALUES (?, ?)", [(owner_id, cid) for cid in contact_ids]
    )


def split_recipients(contacts) -> tuple[list[str], list[str]]:
    """Client contacts in To, the MSP's team as a hidden copy (or in To when there's no client)."""
    to = [c["email"] for c in contacts if c["tenant_id"] is not None]
    team = [c["email"] for c in contacts if c["tenant_id"] is None]
    return (to, team) if to else (team, [])


def contact_label(c) -> str:
    return c["name"] or c["email"]


# -- Rendering --------------------------------------------------------------------------


def _cfg(app):
    return app.extensions["officesentry.config"]


def render(app, template: str, **context) -> str:
    """Render a template outside a web request too (the worker), with links to the portal."""
    from flask import has_request_context, render_template, url_for

    base = _cfg(app).base_url

    def link(endpoint, **values):
        return base + url_for(endpoint, **values)

    # Inside a web request ("Send now") reuse it: a nested request context would
    # run teardown and close the request's database connection.
    ctx = nullcontext() if has_request_context() else app.test_request_context(base_url=base)
    with ctx:
        return render_template(template, link=link, **context)


def _pdf(app, template: str, **context) -> bytes:
    static = Path(app.static_folder)
    html = render(app, template, pdf=True, css_href=(static / "report.css").as_uri(), pdf_available=True, **context)
    return export.to_pdf(html, static.as_uri() + "/")


LOGO_CID = "brand-logo"


def email_brand(conn) -> tuple[dict, list[tuple[str, str, bytes]]]:
    """The MSP's name, colour, contact line and logo for an email template, and the logo to attach inline.

    The logo is sent as an inline attachment (``cid:``): Outlook and Gmail don't show data: images.
    """
    brand = branding.load(conn)
    ctx = {
        "msp_name": mailer.mail_settings(conn).get("msp_name") or brand.display_name,
        "accent": brand.accent,
        "on_accent": brand.on_accent,
        "contact": brand.contact,
        "logo": None,
    }
    image = branding.logo_image(brand)
    size = branding.fit(branding.image_size(image[1]), 32, 200) if image else None
    if not size:
        return ctx, []
    ctx["logo"] = {"src": f"cid:{LOGO_CID}", "width": size[0], "height": size[1]}
    return ctx, [(LOGO_CID, image[0], image[1])]


def _attachments(
    app, conn, tenant, keys, include_xlsx, now, review=None, review_context: dict | None = None, chosen_views=()
) -> list[tuple[str, str, bytes]]:
    """Build every file a bundle attaches. ``review_context`` is extra template context for the review PDF
    (the month-end commentary, recommendations and notes, see ``month_end.report_context``), and what the
    reviewer left out of this client's month is left out of every file. Saved views (``chosen_views``) come
    after the reports."""
    files, built = [], {}
    brand = branding.load(conn)
    me = (review_context or {}).get("month_end") or {}

    def monthly():
        if "review" not in built:
            built["review"] = monthly_review.leave_out(
                review or monthly_review.build(conn, tenant, now), me.get("hidden")
            )
        return built["review"]

    for key, filename in attachment_plan(tenant, keys, include_xlsx, conn):
        if key == "review":
            report = monthly()
            data = (
                _pdf(app, "reports/monthly_review.html", report=report, **(review_context or {}))
                if filename.endswith(".pdf")
                else export.to_xlsx(report, brand=brand)
            )
        elif key == monthly_review.SUMMARY_KEY:
            data = _pdf(app, "reports/monthly_review.html", report=monthly(), appendix=False, **(review_context or {}))
        elif key == "evidence":
            result = built.setdefault(key, schemes.assess(conn, tenant, "insurance", now))
            data = (
                _pdf(app, "reports/scheme_pack.html", r=result, V=schemes)
                if filename.endswith(".pdf")
                else scheme_export.to_xlsx(result, brand=brand)
            )
        elif (design_id := designs.id_of(key)) is not None:
            row = designs.get(conn, design_id)
            data = _pdf(
                app, "reports/design.html", **designs.context(conn, tenant, row, now, brand=brand, month_end=me)
            )
        else:
            data = data_reports.to_xlsx(data_reports.build(conn, tenant, key[5:], now), brand=brand)
        files.append((filename, "application/pdf" if filename.endswith(".pdf") else XLSX, data))
    for v in chosen_views:
        files += view_files(app, conn, view_report(conn, tenant, v, now), v, view_stem(v, tenant["name"]), brand)
    return files


def _field(row, key: str, default=None):
    """A column that older rows (or a plain dict) may not have."""
    return row[key] if key in row.keys() else default


def fill_placeholders(template: str, client: str, month: str) -> str:
    """A report plan's subject or message with {client} and {month} filled in. Nothing else is expanded."""
    return template.replace("{client}", client).replace("{month}", month)


@dataclass
class Built:
    email: Email
    attachment_names: list[str]
    titles: list[str] = field(default_factory=list)  # what the attachments are, once each


def build_report_email(
    app,
    conn,
    schedule,
    tenant,
    contacts,
    *,
    now: datetime | None = None,
    attach: bool = True,
    period: str | None = None,
) -> Built:
    """``period`` names the month a month-end approval is for; otherwise it's the schedule's latest send time's."""
    now = now or datetime.now(UTC)
    keys = json.loads(schedule["reports"] or "[]")
    if period is None:
        period, sent_on = email_period(schedule, _cfg(app).timezone, now)
        month = f"{sent_on.year}-{sent_on.month:02d}"
    else:
        month = period
    label = period_label(period)
    from . import month_end  # imports this module

    # The month's commentary, recommendations and client-visible notes go in the PDF too, and what the reviewer
    # left out of this client's month is left out of the email as well as the files.
    context = month_end.report_context(conn, tenant["id"], month, staff=False)
    review = monthly_review.leave_out(monthly_review.build(conn, tenant, now), context["month_end"]["hidden"])
    known = catalog_map(conn)
    plan = attachment_plan(tenant, keys, bool(schedule["include_xlsx"]), conn)
    chosen = schedule_views(conn, schedule["id"])
    files = [(known[k].title, name) for k, name in plan] + [
        (v.title, name) for v, name in view_attachments(tenant["name"], chosen)
    ]
    failing = review.failing
    stale = any(f.stale for f in review.freshness)
    verdict = summary.monthly(review).verdict
    subject = (
        fill_placeholders(_field(schedule, "subject", "") or "", tenant["name"], label).strip()
        or f"{tenant['name']} security report · {label}"
    )
    message = fill_placeholders(schedule["message"] or "", tenant["name"], label)
    look, inline = email_brand(conn)
    msp = look["msp_name"]
    html = render(
        app,
        "emails/report.html",
        subject=subject,
        tenant=tenant,
        review=review,
        label=label,
        failing=failing[:6],
        more=max(0, len(failing) - 6),
        files=files,
        stale=stale,
        message=message,
        frequency=schedule["frequency"],
        **look,
        kpis=[k for k in review.kpis if k.value is not None],
    )

    lines = [f"{tenant['name']}: security report for {label}", ""]
    if message:
        lines += [message, ""]
    # The answer first, as the HTML has it.
    lines += [f"{verdict.label}: {verdict.headline}", ""]
    if stale:
        lines += ["Some of this tenant's data is more than 36 hours old, so a few figures may be out of date.", ""]
    for k in review.kpis:
        if k.value is None:
            continue
        value = f"{review.currency} {k.value:,.2f}" if k.unit == "currency" else f"{k.value:g}{k.unit}"
        change = f" ({k.delta:+g} in 4 weeks)" if k.delta else ""
        lines.append(f"  {k.label}: {value}{change}")
    lines += ["", f"Needs attention: {plural(len(failing), 'check')}"]
    for r in failing[:6]:
        lines.append(f"  [{(r.severity or 'info').upper()}] {r.summary.rstrip('.')}. {r.action}.")
    if review.new_count or review.resolved:
        lines.append(f"New: {review.new_count} · Fixed: {len(review.resolved)}")
    if files:
        lines += ["", "Attached:"] + [f"  {name} ({title})" for title, name in files]
    lines += ["", f"Sent by {msp} with Office Sentry."]

    to, bcc = split_recipients(contacts)
    attachments = []
    if attach:
        attachments = _attachments(
            app,
            conn,
            tenant,
            keys,
            bool(schedule["include_xlsx"]),
            now,
            review,
            review_context=context,
            chosen_views=chosen,
        )
    return Built(
        Email(
            to=to, bcc=bcc, subject=subject, text="\n".join(lines), html=html, attachments=attachments, inline=inline
        ),
        [name for _, name in files],
        list(dict.fromkeys(title for title, _ in files)),
    )


# A finding that isn't an accepted risk now: never accepted, or accepted until a date that has passed (``:now``).
NOT_ACCEPTED = "(accepted_at IS NULL OR (accepted_until IS NOT NULL AND accepted_until <= :now))"


def digest_data(conn, frequency: str, now: datetime, group_id: int | None = None) -> dict:
    """Cross-tenant summary for the MSP digest: the worst tenants first, and what's new.

    ``group_id`` (the schedule's) keeps it to one tenant group; None is every tenant."""
    since = (now - DIGEST_LOOKBACK[frequency]).strftime(ISO)
    group = tenant_groups.get(conn, group_id)
    in_group = " AND id IN (SELECT tenant_id FROM tenant_group_members WHERE group_id = :group)" if group else ""
    rows = []
    # Paused and archived tenants aren't being collected, so they stay out of the digest.
    for t in conn.execute(
        f"SELECT * FROM tenants WHERE enabled = 1 AND archived_at IS NULL{in_group} ORDER BY name COLLATE NOCASE",
        {"group": group_id},
    ):
        row = monthly_review.rollup_row(conn, t, now)
        counts = conn.execute(
            "SELECT"
            " SUM(resolved_at IS NULL AND severity IN ('critical', 'high')"
            "     AND (first_seen >= :since OR IFNULL(reopened_at, '') >= :since)"
            f"     AND {NOT_ACCEPTED}) AS new_severe,"
            " SUM(resolved_at >= :since) AS fixed"
            " FROM findings WHERE tenant_id = :tid",
            {"since": since, "tid": t["id"], "now": now.strftime(ISO)},
        ).fetchone()
        row["new_severe"] = counts["new_severe"] or 0
        row["fixed"] = counts["fixed"] or 0
        rows.append(row)
    rows.sort(
        key=lambda r: (-r["counts"]["critical"], -r["counts"]["high"], -r["new_severe"], r["tenant"]["name"].lower())
    )
    scores = [r["kpis"]["secure_score.pct"].value for r in rows if r["kpis"]["secure_score.pct"].value is not None]
    new_findings = conn.execute(
        "SELECT f.*, t.name AS tenant_name FROM findings f JOIN tenants t ON t.id = f.tenant_id"
        " WHERE t.enabled = 1 AND t.archived_at IS NULL"
        + (" AND t.id IN (SELECT tenant_id FROM tenant_group_members WHERE group_id = :group)" if group else "")
        + " AND f.resolved_at IS NULL AND f.severity IN ('critical', 'high')"
        " AND (f.first_seen >= :since OR IFNULL(f.reopened_at, '') >= :since)"
        f" AND {NOT_ACCEPTED}"
        " ORDER BY CASE f.severity WHEN 'critical' THEN 0 ELSE 1 END, t.name COLLATE NOCASE, f.rule_key"
        " LIMIT 10",
        {"since": since, "now": now.strftime(ISO), "group": group_id},
    ).fetchall()
    return {
        "rows": rows,
        "group": group["name"] if group else None,
        "since": since,
        "tenants": len(rows),
        "urgent": sum(bool(r["counts"]["critical"] or r["counts"]["high"]) for r in rows),
        "new_severe": sum(r["new_severe"] for r in rows),
        "fixed": sum(r["fixed"] for r in rows),
        "avg_score": round(sum(scores) / len(scores), 1) if scores else None,
        "stale": sum(r["stale_data"] for r in rows),
        "new_findings": new_findings,
        "currency": monthly_review.setting(conn, "currency", "GBP"),
    }


def digest_xlsx(data: dict, brand=None, now: datetime | None = None) -> bytes:
    book = workbook.Book(brand.accent if brand else "")
    header = [
        "Client",
        "Critical",
        "High",
        "Medium",
        "Low",
        "New critical/high",
        "Fixed",
        "Secure Score",
        "Secure Score change",
        "Users with MFA",
        f"Licence waste / month ({data['currency']})",
        "Data up to date",
    ]
    rows = []
    for r in data["rows"]:
        score, mfa, waste = (r["kpis"][k] for k in ("secure_score.pct", "mfa.registered_pct", "waste"))
        rows.append(
            [
                r["tenant"]["name"],
                r["counts"]["critical"],
                r["counts"]["high"],
                r["counts"]["medium"],
                r["counts"]["low"],
                r["new_severe"],
                r["fixed"],
                None if score.value is None else f"{score.value:g}%",
                score.delta,
                None if mfa.value is None else f"{mfa.value:g}%",
                waste.value,
                "No" if r["stale_data"] else "Yes",
            ]
        )
    book.table("Clients", header, rows, description="Every client, worst first")
    cover = workbook.Cover(
        "Client digest",
        data.get("group") or "All clients",
        now or datetime.now(UTC),
        brand.display_name if brand else "",
        accent=brand.accent if brand else "",
        intro=f"Since {day(data['since'])}.",
        facts=[
            ("Clients", data["tenants"], ""),
            ("Need attention", data["urgent"], "critical or high findings"),
            ("New critical or high findings", data["new_severe"], ""),
            ("Fixed", data["fixed"], ""),
            ("Average Secure Score", None if data["avg_score"] is None else f"{data['avg_score']:g}%", ""),
        ],
    )
    return book.save(cover)


def build_digest_email(app, conn, schedule, contacts, *, now: datetime | None = None, attach: bool = True) -> Built:
    now = now or datetime.now(UTC)
    local = now.astimezone(ZoneInfo(schedule_tz(schedule, _cfg(app).timezone)))
    label = period_label(email_period(schedule, _cfg(app).timezone, now)[0])
    data = digest_data(conn, schedule["frequency"], now, schedule["group_id"])
    look, inline = email_brand(conn)
    subject = f"Client digest · {data['group']} · {label}" if data["group"] else f"Client digest · {label}"
    filename = (
        f"client-digest-{tables.slug(data['group'])}-" if data["group"] else "client-digest-"
    ) + f"{local.strftime('%Y-%m-%d')}.xlsx"
    html = render(
        app,
        "emails/digest.html",
        subject=subject,
        d=data,
        label=label,
        **look,
        message=schedule["message"],
        titles=RULE_TITLES,
        frequency=schedule["frequency"],
        files=[("Every client's numbers", filename)] if schedule["include_xlsx"] else [],
    )
    lines = [
        f"Client digest for {label}" + (f": {data['group']}" if data["group"] else ""),
        "",
        f"Clients: {data['tenants']}",
        f"Needing attention (critical or high): {data['urgent']}",
        f"New critical or high findings: {data['new_severe']}",
        f"Fixed: {data['fixed']}",
    ]
    if data["avg_score"] is not None:
        lines.append(f"Average Secure Score: {data['avg_score']:g}%")
    lines += ["", "Clients needing attention:"]
    for r in data["rows"]:
        if r["counts"]["critical"] or r["counts"]["high"]:
            lines.append(f"  {r['tenant']['name']}: {r['counts']['critical']} critical, {r['counts']['high']} high")
    to, bcc = split_recipients(contacts)
    attachments = (
        [(filename, XLSX, digest_xlsx(data, branding.load(conn), now))] if attach and schedule["include_xlsx"] else []
    )
    names = [filename] if schedule["include_xlsx"] else []
    return Built(
        Email(
            to=to, bcc=bcc, subject=subject, text="\n".join(lines), html=html, attachments=attachments, inline=inline
        ),
        names,
    )


def is_upcoming(schedule) -> bool:
    """A digest schedule that sends the Coming up email rather than the tenant digest."""
    return schedule["kind"] == "digest" and upcoming.KEY in json.loads(schedule["reports"] or "[]")


def is_views(schedule) -> bool:
    """A digest schedule that sends saved views across every tenant (or one group) to the team."""
    return schedule["kind"] == "digest" and VIEWS_KEY in json.loads(schedule["reports"] or "[]")


def digest_builder(schedule):
    """What builds a digest schedule's email: the tenant digest, Coming up, or saved views."""
    if is_views(schedule):
        return build_views_email
    return build_upcoming_email if is_upcoming(schedule) else build_digest_email


def schedule_titles(schedule, conn=None) -> list[str]:
    """What the schedule sends, for lists. With ``conn`` its saved views are named too."""
    chosen = [v.title for v in schedule_views(conn, schedule["id"])] if conn is not None else []
    if schedule["kind"] == "report":
        return report_titles(schedule["reports"], conn) + chosen
    if is_views(schedule):
        return chosen or ["Saved views"]
    return [upcoming.TITLE] if is_upcoming(schedule) else ["Client digest"]


EMAIL_ROWS = 5  # items listed per window in the Coming up email; the page has the rest


def upcoming_subject(counts: dict[str, int]) -> str:
    """The answer first: what needs attention now and what's due within a month."""
    now, soon = counts[upcoming.NOW], counts[upcoming.DAYS_30]
    if not now and not soon:
        later = counts[upcoming.DAYS_60] + counts[upcoming.DAYS_90]
        return (
            f"Coming up: {plural(later, 'item')} in the next 90 days"
            if later
            else "Coming up: nothing due in the next 90 days"
        )
    parts = ([f"{now} need{'s' if now == 1 else ''} attention now"] if now else []) + (
        [f"{soon} due within 30 days"] if soon else []
    )
    return "Coming up: " + ", ".join(parts)


def build_upcoming_email(app, conn, schedule, contacts, *, now: datetime | None = None, attach: bool = True) -> Built:
    """The Coming up email: everything due in the next 90 days across every active tenant, soonest first.

    It goes to the MSP's team only (like the tenant digest), so it covers every tenant and Office Sentry's own
    certificates."""
    now = now or datetime.now(UTC)
    local = now.astimezone(ZoneInfo(schedule_tz(schedule, _cfg(app).timezone)))
    label = period_label(email_period(schedule, _cfg(app).timezone, now)[0])
    up = upcoming.collect(
        conn,
        conn.execute(
            "SELECT * FROM tenants WHERE enabled = 1 AND archived_at IS NULL ORDER BY name COLLATE NOCASE"
        ).fetchall(),
        now,
        ours=True,
    )
    counts = up.counts()
    look, inline = email_brand(conn)
    subject = upcoming_subject(counts)
    filename = f"coming-up-{local.strftime('%Y-%m-%d')}.xlsx"
    windows = [(w, up.in_window(w)) for w in upcoming.WINDOWS if counts[w]]
    html = render(
        app,
        "emails/upcoming.html",
        subject=subject,
        up=up,
        counts=counts,
        windows=windows,
        label=label,
        frequency=schedule["frequency"],
        message=schedule["message"],
        shown=EMAIL_ROWS,
        w=upcoming,
        base_url=_cfg(app).base_url.rstrip("/"),
        **look,
        files=[("Everything coming up", filename)] if schedule["include_xlsx"] else [],
    )
    lines = [f"Coming up, {label}", ""]
    if schedule["message"]:
        lines += [schedule["message"], ""]
    lines += [f"{w}: {counts[w]}" for w in upcoming.WINDOWS if w != upcoming.NO_DATE or counts[w]]
    for w, items in windows:
        lines += ["", f"{w}:"]
        for i in items[:EMAIL_ROWS]:
            when = f"{day(i.due.isoformat())} ({i.when})" if i.due else ""
            lines.append(f"  {when + ' ' if when else ''}{i.tenant_name}: {i.title}. {i.detail}")
        if len(items) > EMAIL_ROWS:
            lines.append(f"  And {len(items) - EMAIL_ROWS} more in Office Sentry.")
    to, bcc = split_recipients(contacts)
    attachments = (
        [(filename, XLSX, data_reports.to_xlsx(upcoming.report(up), brand=branding.load(conn)))]
        if attach and schedule["include_xlsx"]
        else []
    )
    return Built(
        Email(
            to=to, bcc=bcc, subject=subject, text="\n".join(lines), html=html, attachments=attachments, inline=inline
        ),
        [filename] if schedule["include_xlsx"] else [],
    )


@dataclass
class ViewResult:
    """One saved view across tenants, for the views email: its report narrowed to the tables the view shapes."""

    view: ScheduledView
    report: data_reports.DataReport
    counts: list[tuple[str, int]]  # (table title, rows after the filters)
    lines: list[str]  # each table in words: its filters and rows ("Forwarding: 7 of 9 rows, ...")
    table: str  # the first table's slug, for the link to its "every row" page
    without: int  # tenants with no data for the report


def views_data(conn, chosen: list[ScheduledView], group_id: int | None, now: datetime) -> dict:
    """Each view over every active tenant, or one group's, stacked like the All tenants "every row" page."""
    group = tenant_groups.get(conn, group_id)
    in_group = " AND id IN (SELECT tenant_id FROM tenant_group_members WHERE group_id = :group)" if group else ""
    tenants = conn.execute(
        f"SELECT * FROM tenants WHERE enabled = 1 AND archived_at IS NULL{in_group} ORDER BY name COLLATE NOCASE",
        {"group": group_id},
    ).fetchall()
    label = group["name"] if group else "All clients"
    results = []
    for v in chosen:
        if not can_schedule_view(v.report_key, across=True):
            continue
        rows = rollups.all_rows(conn, tenants, v.report_key, now, label=label)
        report = views.apply(rows.report, v.view())
        touched = set(view_tables(v))
        slugs = views.table_slugs(report)
        keep = [i for i, slug in enumerate(slugs) if slug in touched] or list(range(len(slugs)))
        out = copy.copy(report)
        out.sections = [report.sections[i] for i in keep]
        out.filtered = [line for line in report.filtered if any(line.startswith(f"{s.title}:") for s in out.sections)]
        if any(rows.truncated(s.title) for s in out.sections):
            out.notice = (
                f"Tables keep the first {rollups.MAX_ROWS:,} rows across clients. Filter the view further, "
                "or open each client's own report for the rest."
            )
        counts = [(s.title, len(s.rows)) for s in out.sections]
        lines = [
            next((line for line in out.filtered if line.startswith(f"{title}:")), f"{title}: {plural(n, 'row')}.")
            for title, n in counts
        ]
        results.append(ViewResult(v, out, counts, lines, slugs[keep[0]] if keep else "", len(rows.without)))
    return {"group": group["name"] if group else None, "label": label, "tenants": len(tenants), "views": results}


def build_views_email(app, conn, schedule, contacts, *, now: datetime | None = None, attach: bool = True) -> Built:
    """Saved views across every active tenant, or one group's, for the MSP's team. A CSV view sends one
    file per table it shapes; a PDF or XLSX has those tables."""
    now = now or datetime.now(UTC)
    label = period_label(email_period(schedule, _cfg(app).timezone, now)[0])
    data = views_data(conn, schedule_views(conn, schedule["id"]), schedule["group_id"], now)
    names = [r.view.name for r in data["views"]]
    lead = (
        names[0]
        if len(names) == 1
        else f"{names[0]} and {plural(len(names) - 1, 'more view')}"
        if names
        else "Saved views"
    )
    subject = f"{lead} · {data['label']} · {label}"
    brand = branding.load(conn)
    files, attachments = [], []
    for r in data["views"]:
        stem = view_stem(r.view, data["label"])
        files += [(r.view.title, name) for _, name in view_plan(r.view, stem)]
        if attach:
            attachments += view_files(app, conn, r.report, r.view, stem, brand)
    look, inline = email_brand(conn)
    html = render(
        app,
        "emails/views.html",
        subject=subject,
        d=data,
        label=label,
        frequency=schedule["frequency"],
        message=schedule["message"],
        files=files,
        **look,
    )
    lines = [f"Saved views for {label}: {data['label']}", ""]
    if schedule["message"]:
        lines += [schedule["message"], ""]
    for r in data["views"]:
        lines.append(r.view.title)
        lines += [f"  {line}" for line in r.lines]
    if files:
        lines += ["", "Attached:"] + [f"  {name} ({title})" for title, name in files]
    to, bcc = split_recipients(contacts)
    return Built(
        Email(
            to=to, bcc=bcc, subject=subject, text="\n".join(lines), html=html, attachments=attachments, inline=inline
        ),
        [name for _, name in files],
    )


def alert_items(conn, tenant_id: int, finding_ids, min_severity: str) -> list[alerts.Item]:
    """The findings an alert email covers: what ``alerts.opened_items`` would post to Teams or Slack,
    less anything from the tenant's very first scan, so onboarding a tenant doesn't email everything it
    already had. Reopened findings always count."""
    items = alerts.opened_items(conn, tenant_id, finding_ids, min_severity)
    first = conn.execute("SELECT MIN(first_seen) FROM findings WHERE tenant_id = ?", (tenant_id,)).fetchone()[0]
    return [i for i in items if i.data["reopened"] or i.data["first_seen"] != first]


def build_alert_email(app, conn, alert, tenant, items: list[alerts.Item], contacts, *, preview: bool = False) -> Built:
    n = len(items)
    level = "critical" if alert["min_severity"] == "critical" else "critical or high"
    subject = f"{n} new {level} finding{'s' if n != 1 else ''} in {tenant['name']}"
    look, inline = email_brand(conn)
    html = render(
        app, "emails/alert.html", subject=subject, tenant=tenant, items=items, level=level, preview=preview, **look
    )
    lines = [subject, ""]
    for i in items:
        lines.append(f"[{i.severity.upper()}] {i.title}: {i.subject}" + (f" ({i.detail})" if i.detail else ""))
    to, bcc = split_recipients(contacts)
    return Built(Email(to=to, bcc=bcc, subject=subject, text="\n".join(lines), html=html, inline=inline), [])


# -- Sending and history ------------------------------------------------------------------


def record(
    conn,
    *,
    kind: str,
    email: Email | None,
    subject: str,
    error: str | None,
    transport: str | None,
    trigger: str,
    user: str | None,
    schedule_id=None,
    alert_id=None,
    tenant_id=None,
    period=None,
    payload=None,
    attempt: int = 1,
    retry_at: str | None = None,
    to=(),
    bcc=(),
) -> int:
    if email is not None:
        to, bcc = email.to, email.bcc
    return conn.execute(
        "INSERT INTO email_deliveries (kind, schedule_id, alert_id, tenant_id, period, subject, recipients, bcc,"
        " attachments, payload, attempt, retry_at, transport, trigger, status, error, sent_by, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            kind,
            schedule_id,
            alert_id,
            tenant_id,
            period,
            subject,
            ", ".join(to),
            ", ".join(bcc),
            json.dumps([a[0] for a in email.attachments] if email else []),
            payload,
            attempt,
            retry_at if error else None,
            transport,
            trigger,
            "failed" if error else "sent",
            error[:1000] if error else None,
            user,
            utcnow(),
        ),
    ).lastrowid


def _attempt(conn, vault, build, kind: str) -> tuple[Email | None, str | None, str | None]:
    """Build and send one email. Returns (email, transport, error shown to people or None). Writes nothing."""
    email, transport = None, None
    try:
        email = build().email
        transport = mailer.send(conn, vault, email)
        error = None
    except MailError as exc:
        error = str(exc)
    except Exception as exc:  # a report bug must not stop other emails
        log.exception("Building %s email failed", kind)
        error = f"Couldn't build the email: {type(exc).__name__}: {exc}"
    return email, transport, error


def _send(
    conn, vault, build, *, kind: str, fallback_subject: str, trigger: str, user: str | None, **fields
) -> str | None:
    """Build and send one email, and log it. Returns the error shown to people, or None."""
    email, transport, error = _attempt(conn, vault, build, kind)
    record(
        conn,
        kind=kind,
        email=email,
        subject=email.subject if email else fallback_subject,
        error=error,
        transport=transport,
        trigger=trigger,
        user=user,
        **fields,
    )
    return error


def send_schedule(
    app, conn, vault, schedule, *, trigger: str = "manual", user: str | None = None, now: datetime | None = None
) -> str | None:
    """Send a report or digest schedule now. Returns an error message or None."""
    now = now or datetime.now(UTC)
    default_tz = _cfg(app).timezone
    period, _due = schedule_slot(schedule, default_tz, now)
    contacts = recipients(conn, "schedule", schedule["id"])
    built: list[Built] = []
    tenant = None
    if schedule["kind"] == "report":
        tenant = store.get_tenant(conn, schedule["tenant_id"])

        def build():
            if nothing_collected(conn, tenant["id"]):  # a report with no data would tell the client nothing true
                raise MailError(
                    f"Nothing has been collected for {tenant['name']} yet, so there's no report to send."
                    " Check its connection under Settings."
                )
            built.append(build_report_email(app, conn, schedule, tenant, contacts, now=now))
            return built[-1]

        fallback = f"{tenant['name']} security report"
    else:
        builder = digest_builder(schedule)

        def build():
            return builder(app, conn, schedule, contacts, now=now)

        fallback = schedule_titles(schedule, conn)[0]
    email, transport, error = _attempt(conn, vault, build, schedule["kind"])
    # Everything the send changes is written at once, so a worker that stops after sending doesn't leave the
    # period looking unsent (and send it again).
    with store.atomic(conn):
        _after_send(
            conn,
            schedule,
            email,
            transport,
            error,
            period=period,
            trigger=trigger,
            user=user,
            now=now,
            fallback=fallback,
            archive=built[-1].email if error is None and built else None,
            tenant=tenant,
        )
    if error is None and built:
        # After the commit, so a slow or failing webhook never holds the send's transaction.
        report_ready(app, conn, vault, tenant, period, built[-1].titles)
    return error


def report_ready(app, conn, vault, tenant, period: str | None, titles: list[str], *, emailed: bool = True) -> None:
    """Tell the webhook (Teams, Slack or your own) a client's report is in its report history, when the admin
    turned that on. A link only: the files stay behind the portal's sign-in. Never raises."""
    alerts.report_ready(conn, alerts.Alerting(_cfg(app).base_url, vault), tenant, period, titles, emailed=emailed)


def _after_send(
    conn,
    schedule,
    email,
    transport,
    error,
    *,
    period: str,
    trigger: str,
    user: str | None,
    now: datetime,
    fallback: str,
    archive: Email | None,
    tenant,
) -> None:
    record(
        conn,
        kind=schedule["kind"],
        email=email,
        subject=email.subject if email else fallback,
        error=error,
        transport=transport,
        trigger=trigger,
        user=user,
        schedule_id=schedule["id"],
        tenant_id=schedule["tenant_id"],
        period=period,
    )
    ts = now.strftime(ISO)
    if archive is not None:
        _archive(conn, schedule, tenant, archive, period=period, trigger=trigger, user=user, now=ts)
    if trigger == "schedule":
        if error is None:
            conn.execute(
                "UPDATE email_schedules SET last_period = ?, attempts = 0, retry_at = NULL,"
                " last_status = 'sent', last_sent_at = ? WHERE id = ?",
                (period, ts, schedule["id"]),
            )
        else:
            attempts = schedule["attempts"] + 1
            if attempts >= MAX_ATTEMPTS:  # give up on this period; it shows as failed with a Retry button
                conn.execute(
                    "UPDATE email_schedules SET last_period = ?, attempts = 0, retry_at = NULL,"
                    " last_status = 'failed' WHERE id = ?",
                    (period, schedule["id"]),
                )
            else:
                conn.execute(
                    "UPDATE email_schedules SET attempts = ?, retry_at = ?, last_status = 'failed' WHERE id = ?",
                    (attempts, (now + RETRY_DELAYS[attempts - 1]).strftime(ISO), schedule["id"]),
                )
    elif error is None:
        # Sent by hand: that counts as the latest send time's email, but never moves the schedule back from a
        # later period already done (a month approved and sent early), or the timer would send that again.
        last = conn.execute("SELECT last_period FROM email_schedules WHERE id = ?", (schedule["id"],)).fetchone()
        last = last["last_period"] if last else None
        conn.execute(
            "UPDATE email_schedules SET last_status = 'sent', last_sent_at = ?, last_period = ?,"
            " attempts = 0, retry_at = NULL WHERE id = ?",
            (ts, last if period_done(last, period) else period, schedule["id"]),
        )
    else:
        conn.execute("UPDATE email_schedules SET last_status = 'failed' WHERE id = ?", (schedule["id"],))


# SQL for "the later of last_period and ?" (twice the same parameter), for a monthly report plan schedule.
LATER_PERIOD = "CASE WHEN last_period IS NULL OR last_period < ? THEN ? ELSE last_period END"


def send_frozen(
    app,
    conn,
    vault,
    schedule,
    tenant,
    period: str,
    files: list[tuple[str, str, bytes]],
    *,
    user: str | None,
    now: datetime | None = None,
    on_sent=None,
) -> str | None:
    """Email a report schedule's recipients the files approved for ``period`` (``month_end.approve``), exactly
    as they were frozen into ``issued_reports``, and count the period as done. Returns an error or None.

    ``on_sent(conn)`` writes the caller's own record of the send in the same transaction as the email's."""
    now = now or datetime.now(UTC)
    contacts = recipients(conn, "schedule", schedule["id"])

    def build():
        built = build_report_email(app, conn, schedule, tenant, contacts, now=now, attach=False, period=period)
        built.email.attachments = list(files)
        return built

    email, transport, error = _attempt(conn, vault, build, "report")
    fallback = f"{tenant['name']} security report"
    with store.atomic(conn):
        record(
            conn,
            kind="report",
            email=email,
            subject=email.subject if email else fallback,
            error=error,
            transport=transport,
            trigger="manual",
            user=user,
            schedule_id=schedule["id"],
            tenant_id=tenant["id"],
            period=period,
        )
        if error is None:
            # Re-issuing an earlier month never moves the schedule back, or the next tick sends again.
            conn.execute(
                f"UPDATE email_schedules SET last_period = {LATER_PERIOD}, attempts = 0, retry_at = NULL,"
                " last_status = 'sent', last_sent_at = ? WHERE id = ?",
                (period, period, now.strftime(ISO), schedule["id"]),
            )
            if on_sent is not None:
                on_sent(conn)
        else:
            conn.execute("UPDATE email_schedules SET last_status = 'failed' WHERE id = ?", (schedule["id"],))
    return error


def _archive(conn, schedule, tenant, email: Email, *, period: str, trigger: str, user: str | None, now: str) -> None:
    """Keep a copy of every file a sent report email attached (``issued.py``). Never fails the send."""
    try:
        known = catalog_map(conn)
        plan = {
            name: (key, known[key].title if key in known else name)
            for key, name in attachment_plan(
                tenant, json.loads(schedule["reports"] or "[]"), bool(schedule["include_xlsx"]), conn
            )
        }
        plan |= {
            name: (f"view:{v.report_key}", v.title)
            for v, name in view_attachments(tenant["name"], schedule_views(conn, schedule["id"]))
        }
        files = [
            issued.IssuedFile(*plan.get(filename, ("", filename)), filename, content_type, data)
            for filename, content_type, data in email.attachments
        ]
        issued.archive(
            conn,
            tenant["id"],
            files,
            period=period,
            trigger=trigger,
            user=user,
            to=email.to,
            bcc=email.bcc,
            schedule_id=schedule["id"],
            now=now,
        )
    except Exception:
        log.exception("Keeping a copy of the reports sent to tenant %s failed", tenant["id"])


def send_alert(
    app,
    conn,
    vault,
    alert,
    tenant,
    items: list[alerts.Item],
    *,
    trigger: str = "schedule",
    user: str | None = None,
    attempt: int = 1,
    now: datetime | None = None,
) -> str | None:
    """Email one alert. An automatic send that fails is tried again by ``tick()`` with backoff."""
    now = now or datetime.now(UTC)
    contacts = recipients(conn, "alert", alert["id"])
    retry_at = None
    if trigger == "schedule" and attempt < MAX_ATTEMPTS:
        retry_at = (now + RETRY_DELAYS[attempt - 1]).strftime(ISO)
    return _send(
        conn,
        vault,
        lambda: build_alert_email(app, conn, alert, tenant, items, contacts),
        kind="alert",
        fallback_subject=f"New findings in {tenant['name']}",
        trigger=trigger,
        user=user,
        alert_id=alert["id"],
        tenant_id=tenant["id"],
        payload=json.dumps([i.data["id"] for i in items]),
        attempt=attempt,
        retry_at=retry_at,
    )


def after_sync(app, conn, vault, tenant, finding_ids, run_id: int | None = None) -> int:
    """Email everyone whose alert covers ``tenant`` about the findings one sync opened.

    Called from ``alerts.after_sync``, so emails go out on the same event as Teams, Slack and webhook
    alerts. Never raises. Returns the number of emails attempted.
    """
    if not finding_ids:
        return 0
    attempted = 0
    rows = conn.execute(
        "SELECT * FROM email_alerts WHERE enabled = 1 AND (tenant_id IS NULL OR tenant_id = ?) ORDER BY id",
        (tenant["id"],),
    ).fetchall()
    for a in rows:
        try:
            items = alert_items(conn, tenant["id"], finding_ids, a["min_severity"])
            if not items or not recipients(conn, "alert", a["id"]):
                continue
            error = send_alert(app, conn, vault, a, tenant, items)
            attempted += 1
            if run_id is not None:
                store.log_event(
                    conn,
                    run_id,
                    "warning" if error else "info",
                    f"Alert email not sent: {error}"[:500] if error else "Alert email sent",
                )
        except Exception:
            log.exception("Alert email %s failed", a["id"])
    return attempted


def _alert_resend(conn, delivery) -> tuple:
    """(alert, tenant, items) to send a failed alert again; items are only what is still open."""
    alert = conn.execute("SELECT * FROM email_alerts WHERE id = ?", (delivery["alert_id"],)).fetchone()
    tenant = store.get_tenant(conn, delivery["tenant_id"]) if delivery["tenant_id"] else None
    if alert is None or tenant is None:
        return alert, tenant, []
    ids = json.loads(delivery["payload"] or "[]")
    return alert, tenant, alerts.opened_items(conn, tenant["id"], ids, alert["min_severity"])


def retry_delivery(app, conn, vault, delivery, user: str) -> str | None:
    """Send a failed email again: the schedule's current content, or the same alert."""
    if delivery["schedule_id"]:
        schedule = conn.execute("SELECT * FROM email_schedules WHERE id = ?", (delivery["schedule_id"],)).fetchone()
        if schedule is None:
            return "That schedule has been removed"
        if _field(schedule, "on_approval", 0):
            return "This report goes out when its month-end review is approved; send it from Month-end reporting"
        if being_sent(conn, schedule, _cfg(app).timezone):
            return BEING_SENT
        return send_schedule(app, conn, vault, schedule, trigger="retry", user=user)
    if delivery["alert_id"] and delivery["payload"]:
        conn.execute("UPDATE email_deliveries SET retry_at = NULL WHERE id = ?", (delivery["id"],))
        alert, tenant, items = _alert_resend(conn, delivery)
        if alert is None or tenant is None:
            return "That alert has been removed"
        if not items:
            return "Those findings have been fixed or accepted since, so there's nothing to send"
        return send_alert(app, conn, vault, alert, tenant, items, trigger="retry", user=user)
    return "This email can't be sent again"


def _due(conn, s, cfg, now: datetime) -> bool:
    """Whether a schedule should send now (before refreshing its data). Writes nothing."""
    if not s["enabled"] or _field(s, "on_approval", 0):
        return False  # a report plan that waits for approval: month_end.approve sends it
    period, _ = schedule_slot(s, cfg.timezone, now)
    if period_done(s["last_period"], period):
        return False
    if s["retry_at"] and now.strftime(ISO) < s["retry_at"]:
        return False
    if not recipients(conn, "schedule", s["id"]):
        return False  # the schedules page shows "No recipients"
    if s["kind"] == "report":
        tenant = store.get_tenant(conn, s["tenant_id"])
        if tenant is None or not tenant["enabled"] or tenant["archived_at"]:
            return False
    return True


def send_key(s, period: str) -> str:
    return f"schedule:{s['id']}:{period}"


BEING_SENT = "It's being sent on its schedule right now. Check its history in a minute before sending it again."


def being_sent(conn, s, default_tz: str, now: datetime | None = None) -> bool:
    """Whether the timer has queued (or is sending) the schedule's latest period, so Send now or Retry would
    send it twice."""
    period, _ = schedule_slot(s, default_tz, now or datetime.now(UTC))
    return tasks.pending(conn, send_key(s, period)) is not None


def _tick_schedule(app, conn, vault, s, cfg, now: datetime, queue: bool = False) -> bool:
    if not _due(conn, s, cfg, now):
        return False
    period, due = schedule_slot(s, cfg.timezone, now)
    if queue and tasks.pending(conn, send_key(s, period)):
        return False  # already waiting for a task thread
    if s["kind"] == "report":
        tenant = store.get_tenant(conn, s["tenant_id"])
        # Collect anything older than OFFICESENTRY_MAX_AGE_HOURS first; checked again next minute.
        if tenant["connection_id"] and not schedule.refresh_for_report(conn, tenant["id"], due, cfg.max_age_hours, now):
            return False
    if queue:
        tasks.enqueue(
            conn,
            "send_schedule",
            key=send_key(s, period),
            tenant_id=s["tenant_id"],
            payload={"schedule_id": s["id"], "now": now.strftime(ISO)},
        )
    else:
        send_schedule(app, conn, vault, s, trigger="schedule", now=now)
    return True


@tasks.handler("send_schedule")
def _send_schedule_task(ctx: tasks.TaskContext) -> dict:
    """One due report or digest email, queued by ``tick``. Checked again first: it may have been sent, paused
    or deleted while it waited."""
    s = ctx.conn.execute("SELECT * FROM email_schedules WHERE id = ?", (ctx.payload["schedule_id"],)).fetchone()
    now = datetime.fromisoformat(ctx.payload["now"].replace("Z", "+00:00"))
    if s is None or not _due(ctx.conn, s, ctx.cfg, now):
        return {"skipped": True}
    ctx.progress("Building and sending the email")
    error = send_schedule(ctx.app, ctx.conn, ctx.vault, s, trigger="schedule", now=now)
    if error:
        raise tasks.TaskFailed(error)  # the schedule itself retries with backoff (send_schedule)
    return {"sent": True}


def _retry_alert(app, conn, vault, d, now: datetime) -> bool:
    conn.execute("UPDATE email_deliveries SET retry_at = NULL WHERE id = ?", (d["id"],))
    alert, tenant, items = _alert_resend(conn, d)
    if alert is None or not alert["enabled"] or tenant is None or not tenant["enabled"] or not items:
        return False  # a paused (or archived) tenant's alerts aren't retried
    send_alert(app, conn, vault, alert, tenant, items, attempt=d["attempt"] + 1, now=now)
    return True


def tick(app, conn, vault, cfg, now: datetime | None = None, queue: bool = False) -> int:
    """Send every schedule that is due and retry failed alerts. Safe to call every minute.

    With ``queue`` (the worker) a due schedule is queued as a ``send_schedule`` task instead of sent here.
    Returns emails attempted or queued."""
    now = now or datetime.now(UTC)
    attempted = 0
    for s in conn.execute("SELECT * FROM email_schedules WHERE enabled = 1 ORDER BY id").fetchall():
        try:
            attempted += _tick_schedule(app, conn, vault, s, cfg, now, queue)
        except Exception:
            log.exception("Scheduled email %s failed", s["id"])
    # Alert emails are sent when a collection opens findings (alerts.after_sync); here only failed ones retry.
    for d in conn.execute(
        "SELECT * FROM email_deliveries WHERE kind = 'alert' AND status = 'failed'"
        " AND retry_at IS NOT NULL AND retry_at <= ? ORDER BY id",
        (now.strftime(ISO),),
    ).fetchall():
        try:
            attempted += _retry_alert(app, conn, vault, d, now)
        except Exception:
            log.exception("Retrying alert email %s failed", d["id"])
    return attempted


@schedule.periodic("email_delivery")
def _email_job(ctx) -> None:
    """Registered with the worker through ``worker.JOB_MODULES``. Due emails become tasks for the task threads."""
    tick(ctx.app, ctx.conn, ctx.vault, ctx.cfg, ctx.now, queue=True)
