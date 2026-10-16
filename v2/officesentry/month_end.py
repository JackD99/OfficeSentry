"""Month-end reporting: report plans, the review queue, commentary, and approval.

* A **report plan** is one MSP-level recipe (which reports, the day and time,
  the subject and message with ``{client}`` and ``{month}``) applied to many
  tenants at once. Applying it creates or updates one ordinary
  ``email_schedules`` row per tenant, so ``delivery.tick`` sends it like any
  other schedule. A plan in "send on approval" mode marks its schedules
  ``on_approval``: the timer never sends them, approving does.
* Every tenant on a plan has an implicit **Draft** for each month ("2026-10",
  the schedule period it goes out in). Staff move it to **In review**, write
  commentary, curate recommendations and notes; an admin **approves**, which
  renders the files once into ``issued_reports`` (frozen) and then sends those
  exact files, or only publishes them to the tenant's report history when the
  plan doesn't email. Approving again ("re-issue") stores the next version
  with a reason. Approving is a background task (``queue_approval``, run by
  the worker's task threads), one per tenant and period, so a bulk approval
  of hundreds of clients returns at once and nothing renders in a request.

Reporting only: nothing here reads or writes Microsoft 365. Roles: staff
(admin and analyst) review, comment and assign; only admins edit plans and
approve, send or re-issue.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from . import delivery, issued, mailer, projects, store, tasks
from .checks import RULES
from .db import audit, transaction, utcnow
from .reports import designs, monthly_review, summary
from .reports.precomputed import Rows, stored_rows
from .text import plural

log = logging.getLogger(__name__)

ISO = "%Y-%m-%dT%H:%M:%SZ"
STATES = {"draft": "Draft", "in_review": "In review", "approved": "Approved", "sent": "Sent"}
ACTION_STATUSES = {"planned": "Planned", "in_progress": "In progress", "waiting": "Waiting on you", "done": "Done"}
MODES = {"auto": "Send automatically", "approval": "Send on approval"}
DELIVERIES = {"email": "Email the client's contacts", "portal": "Publish to the client portal only"}
VISIBILITY = {"client": "Client can see", "staff": "Staff only"}
STALE_DATA = timedelta(hours=24)
PERIOD_RE = re.compile(r"\d{4}-(0[1-9]|1[0-2])")
DEFAULT_SUBJECT = "{client} security report · {month}"


def valid_period(period: str) -> bool:
    return bool(PERIOD_RE.fullmatch(period or ""))


def current_period(tz: str, now: datetime | None = None) -> str:
    local = (now or datetime.now(UTC)).astimezone(ZoneInfo(tz))
    return f"{local.year}-{local.month:02d}"


def shift_period(period: str, months: int) -> str:
    year, month = int(period[:4]), int(period[5:])
    n = year * 12 + month - 1 + months
    return f"{n // 12}-{n % 12 + 1:02d}"


def period_start(period: str) -> str:
    return f"{period}-01T00:00:00Z"


# -- Plans ---------------------------------------------------------------------------------


def plans(conn) -> list:
    return conn.execute(
        "SELECT p.*, (SELECT COUNT(*) FROM report_plan_tenants pt WHERE pt.plan_id = p.id) AS tenants"
        " FROM report_plans p ORDER BY p.name COLLATE NOCASE"
    ).fetchall()


def get_plan(conn, plan_id: int):
    return conn.execute("SELECT * FROM report_plans WHERE id = ?", (plan_id,)).fetchone()


def plan_tenant_ids(conn, plan_id: int) -> list[int]:
    return [r[0] for r in conn.execute("SELECT tenant_id FROM report_plan_tenants WHERE plan_id = ?", (plan_id,))]


def plan_for(conn, tenant_id: int):
    """(plan, link row) for a tenant on a plan, else (None, None)."""
    link = conn.execute("SELECT * FROM report_plan_tenants WHERE tenant_id = ?", (tenant_id,)).fetchone()
    if link is None:
        return None, None
    return get_plan(conn, link["plan_id"]), link


def waits_for_approval(plan) -> bool:
    return plan["mode"] == "approval" or plan["delivery"] == "portal"


def needs_approval(conn, tenant_id: int) -> bool:
    """Whether the tenant's month-end reports wait for approval: then any other email of theirs only carries
    a month's commentary once it's approved."""
    plan, _ = plan_for(conn, tenant_id)
    return plan is not None and waits_for_approval(plan)


PLAN_COLUMNS = (
    "name",
    "reports",
    "include_xlsx",
    "day_of_month",
    "hour",
    "timezone",
    "subject",
    "message",
    "mode",
    "delivery",
    "bcc_team",
)


def save_plan(conn, values: dict, user: str, plan_id: int | None = None) -> int:
    params = [json.dumps(values["reports"]) if k == "reports" else values[k] for k in PLAN_COLUMNS]
    if plan_id is None:
        return conn.execute(
            f"INSERT INTO report_plans ({', '.join(PLAN_COLUMNS)}, created_by, created_at, updated_at)"
            f" VALUES ({', '.join('?' * len(PLAN_COLUMNS))}, ?, ?, ?)",
            (*params, user, utcnow(), utcnow()),
        ).lastrowid
    conn.execute(
        f"UPDATE report_plans SET {', '.join(c + ' = ?' for c in PLAN_COLUMNS)}, updated_at = ? WHERE id = ?",
        (*params, utcnow(), plan_id),
    )
    return plan_id


def _plan_recipients(conn, plan, tenant_id: int) -> list[int]:
    """Who a tenant's plan email goes to: its first ``MAX_RECIPIENTS`` contacts (by name, as the Emails page
    lists them), plus the team's hidden copy when the plan asks for one. The cap is on the client's own
    contacts, so a big client never pushes the team's copy out; ``coverage`` says who is left off."""
    if plan["delivery"] != "email":
        return []
    ids = [c["id"] for c in delivery.tenant_contacts(conn, tenant_id)][: delivery.mailer.MAX_RECIPIENTS]
    if ids and plan["bcc_team"]:
        ids += [c["id"] for c in delivery.team_contacts(conn)]
    return ids


def apply_plan(conn, plan, tenant_ids: list[int], user: str, default_tz: str, now: datetime | None = None) -> int:
    """Put tenants on ``plan``: create or update each one's plan schedule. Returns how many were applied.

    A tenant moves off any other plan. Like saving a schedule by hand, a new or re-timed schedule counts
    the current period as done, so applying never sends straight away."""
    now = now or datetime.now(UTC)
    tz = plan["timezone"] or default_tz
    fresh = delivery.initial_period("monthly", 0, plan["day_of_month"], plan["hour"], tz, now)
    cols = {
        "reports": plan["reports"],
        "include_xlsx": plan["include_xlsx"],
        "message": plan["message"],
        "subject": plan["subject"],
        "frequency": "monthly",
        "day_of_week": 0,
        "day_of_month": plan["day_of_month"],
        "hour": plan["hour"],
        "timezone": plan["timezone"],
        "enabled": 1,
        "plan_id": plan["id"],
        "on_approval": int(waits_for_approval(plan)),
    }
    timing = ("day_of_month", "hour", "timezone")
    for tenant_id in tenant_ids:
        link = conn.execute("SELECT * FROM report_plan_tenants WHERE tenant_id = ?", (tenant_id,)).fetchone()
        schedule = (
            conn.execute("SELECT * FROM email_schedules WHERE id = ?", (link["schedule_id"],)).fetchone()
            if link and link["schedule_id"]
            else None
        )
        if schedule is None:
            schedule_id = conn.execute(
                f"INSERT INTO email_schedules (kind, tenant_id, {', '.join(cols)}, last_period, created_by,"
                f" created_at) VALUES ('report', ?, {', '.join('?' * len(cols))}, ?, ?, ?)",
                (tenant_id, *cols.values(), fresh, user, utcnow()),
            ).lastrowid
        else:
            schedule_id = schedule["id"]
            conn.execute(
                f"UPDATE email_schedules SET {', '.join(c + ' = ?' for c in cols)} WHERE id = ?",
                (*cols.values(), schedule_id),
            )
            # Re-timed, or switched between automatic and send on approval: start counting from now, so
            # a month nobody approved isn't emailed by the timer. Never move back past a period already done.
            if (
                any(schedule[k] != cols[k] for k in timing)
                or schedule["frequency"] != "monthly"
                or schedule["on_approval"] != cols["on_approval"]
            ):
                start = fresh if schedule["frequency"] != "monthly" else max(fresh, schedule["last_period"] or "")
                conn.execute(
                    "UPDATE email_schedules SET last_period = ?, attempts = 0, retry_at = NULL WHERE id = ?",
                    (start, schedule_id),
                )
        delivery.set_recipients(conn, "schedule", schedule_id, _plan_recipients(conn, plan, tenant_id))
        conn.execute(
            "INSERT INTO report_plan_tenants (tenant_id, plan_id, schedule_id, added_by, added_at)"
            " VALUES (?, ?, ?, ?, ?) ON CONFLICT(tenant_id) DO UPDATE SET plan_id = excluded.plan_id,"
            " schedule_id = excluded.schedule_id",
            (tenant_id, plan["id"], schedule_id, user, utcnow()),
        )
    return len(tenant_ids)


def remove_from_plan(conn, tenant_id: int) -> None:
    """Take a tenant off its plan and remove the plan's schedule for it (its history stays)."""
    link = conn.execute("SELECT * FROM report_plan_tenants WHERE tenant_id = ?", (tenant_id,)).fetchone()
    if link is None:
        return
    if link["schedule_id"]:
        conn.execute("DELETE FROM email_schedules WHERE id = ? AND plan_id IS NOT NULL", (link["schedule_id"],))
    conn.execute("DELETE FROM report_plan_tenants WHERE tenant_id = ?", (tenant_id,))


def delete_plan(conn, plan_id: int) -> None:
    for tenant_id in plan_tenant_ids(conn, plan_id):
        remove_from_plan(conn, tenant_id)
    conn.execute("DELETE FROM report_plans WHERE id = ?", (plan_id,))


def contact_added(conn, tenant_id: int, contact_id: int) -> None:
    """A new client contact joins the tenant's plan email (within the plan's limit), so the plan keeps
    reaching everyone. The first contact also brings the team's hidden copy, if the plan sends one."""
    plan, link = plan_for(conn, tenant_id)
    if plan is not None and link["schedule_id"] and plan["delivery"] == "email":
        delivery.set_recipients(conn, "schedule", link["schedule_id"], _plan_recipients(conn, plan, tenant_id))


# -- Coverage ---------------------------------------------------------------------------------


@dataclass
class CoverageRow:
    tenant: dict
    plan: str | None
    monthly: int  # enabled monthly report schedules (or a portal-only plan)
    contacts: int
    recipients: int  # people on those schedules
    gaps: list[str] = field(default_factory=list)  # no_report | no_contacts | no_recipients | left_off
    left_off: int = 0  # client contacts a plan email can't include (over mailer.MAX_RECIPIENTS)


@dataclass
class Coverage:
    rows: list[CoverageRow]
    paused: int

    @property
    def total(self) -> int:
        return len(self.rows)

    @property
    def covered(self) -> int:
        return sum(1 for r in self.rows if r.monthly)

    def count(self, gap: str) -> int:
        return sum(1 for r in self.rows if gap in r.gaps)

    @property
    def gaps(self) -> list[CoverageRow]:
        return [r for r in self.rows if r.gaps]

    @property
    def headline(self) -> str:
        text = (
            f"{self.covered} of {plural(self.total, 'client')} {'has' if self.total == 1 else 'have'} a monthly report"
        )
        parts = []
        if self.count("no_contacts"):
            n = self.count("no_contacts")
            parts.append(f"{n} {'has' if n == 1 else 'have'} no contacts")
        if self.count("no_recipients"):
            n = self.count("no_recipients")
            parts.append(f"{n} {'sends' if n == 1 else 'send'} to nobody")
        if self.count("left_off"):
            n = self.count("left_off")
            parts.append(f"{n} {'has' if n == 1 else 'have'} more contacts than one email can go to")
        return text + ("; " + "; ".join(parts) if parts else "")


def coverage(conn, tenants) -> Coverage:
    """Which active clients get a monthly report, and what's missing for the rest."""
    rows, paused = [], 0
    for t in tenants:
        if not t["enabled"] or t["archived_at"]:
            paused += 1
            continue
        plan, _ = plan_for(conn, t["id"])
        schedules = conn.execute(
            "SELECT * FROM email_schedules WHERE tenant_id = ? AND kind = 'report'"
            " AND enabled = 1 AND frequency = 'monthly'",
            (t["id"],),
        ).fetchall()
        contacts = len(delivery.tenant_contacts(conn, t["id"]))
        people = {c["id"] for s in schedules for c in delivery.recipients(conn, "schedule", s["id"])}
        portal = plan is not None and plan["delivery"] == "portal"
        row = CoverageRow(dict(t), plan["name"] if plan else None, len(schedules) or int(portal), contacts, len(people))
        if not row.monthly:
            row.gaps.append("no_report")
        if not contacts and not portal:
            row.gaps.append("no_contacts")
        elif schedules and not people and not portal:
            row.gaps.append("no_recipients")
        if plan is not None and plan["delivery"] == "email" and contacts > delivery.mailer.MAX_RECIPIENTS:
            row.left_off = contacts - delivery.mailer.MAX_RECIPIENTS
            row.gaps.append("left_off")
        rows.append(row)
    return Coverage(rows, paused)


# -- Reviews ------------------------------------------------------------------------------------


def review(conn, tenant_id: int, period: str):
    return conn.execute(
        "SELECT * FROM report_reviews WHERE tenant_id = ? AND period = ?", (tenant_id, period)
    ).fetchone()


def update_review(conn, tenant_id: int, period: str, user: str | None, **fields) -> None:
    """Create the month's review row if it's still implicit (Draft, or Sent by an automatic plan), keeping
    that state, then set ``fields``."""
    if review(conn, tenant_id, period) is None:
        conn.execute(
            "INSERT INTO report_reviews (tenant_id, period, state, updated_by, updated_at)"
            " VALUES (?, ?, ?, ?, ?) ON CONFLICT(tenant_id, period) DO NOTHING",
            (tenant_id, period, state_of(conn, tenant_id, period), user, utcnow()),
        )
    if fields:
        conn.execute(
            f"UPDATE report_reviews SET {', '.join(k + ' = ?' for k in fields)}, updated_by = ?,"
            " updated_at = ? WHERE tenant_id = ? AND period = ?",
            (*fields.values(), user, utcnow(), tenant_id, period),
        )


def state_of(conn, tenant_id: int, period: str, row=None) -> str:
    row = row if row is not None else review(conn, tenant_id, period)
    if row is not None:
        return row["state"]
    # An automatic plan that already went out on its timer (another schedule's report doesn't count).
    sent = conn.execute(
        "SELECT 1 FROM issued_reports WHERE tenant_id = ? AND period = ? AND (trigger = 'approval' OR schedule_id ="
        " (SELECT schedule_id FROM report_plan_tenants WHERE tenant_id = ?)) LIMIT 1",
        (tenant_id, period, tenant_id),
    ).fetchone()
    return "sent" if sent else "draft"


def start_review(conn, tenant_id: int, period: str, user: str) -> None:
    """Draft -> In review. Any later state is left alone."""
    if state_of(conn, tenant_id, period) == "draft":
        update_review(conn, tenant_id, period, user, state="in_review")


@dataclass
class QueueRow:
    tenant: dict
    plan: dict
    state: str
    reviewer: str | None
    verdict: object
    new_severe: int
    flags: list[str]
    sent_at: str | None = None
    error: str | None = None
    paused: bool = False
    task: dict | None = None  # the latest approval task for the month (``approval_task``)
    edited: bool = False  # changed since it was approved (``changed_since_approval``)

    @property
    def state_label(self) -> str:
        if self.state == "sent" and self.plan["delivery"] == "portal":
            return "Published"
        return STATES[self.state]

    @property
    def approving(self) -> bool:
        return bool(self.task and self.task["status"] in tasks.PENDING)

    @property
    def approval_failed(self) -> str | None:
        """Why the last approval failed, while the month is still waiting for one."""
        if self.task and self.task["status"] == "failed" and self.state in ("draft", "in_review"):
            return self.task["error"] or "Something went wrong"
        return None

    @property
    def ready(self) -> bool:
        return self.state in ("draft", "in_review") and not self.flags and not self.paused and not self.approving


def _last_report_at(conn, tenant_id: int, period: str) -> str:
    row = conn.execute(
        "SELECT MAX(issued_at) FROM issued_reports WHERE tenant_id = ? AND period < ?", (tenant_id, period)
    ).fetchone()
    return row[0] or period_start(shift_period(period, -1))


@dataclass
class ReviewFacts:
    """What the queue needs from a tenant's monthly review, without building it on every view."""

    verdict: summary.Verdict
    oldest: str | None  # the oldest collector's data, or None when nothing has been collected
    not_run: int  # checks that couldn't run
    missing: tuple[str, ...] = ()  # data sources never collected, while others have been (titles)


def review_facts(conn, tenants, now: datetime | None = None, rebuild: bool = False) -> dict[int, ReviewFacts]:
    """Each tenant's facts by tenant id. Without ``now`` (the pages), from ``precomputed`` while that's current,
    else built from the monthly review and stored (always, with ``rebuild``)."""
    stored = stored_rows(conn, "review", tenants, now, rebuild)
    now = now or datetime.now(UTC)
    return {t["id"]: _review_facts(conn, t, now, stored) for t in tenants}


def _review_facts(conn, tenant, now: datetime, stored: Rows | None) -> ReviewFacts:
    fingerprint = stored.fingerprint(tenant) if stored else ""
    if stored and (data := stored.get(tenant, fingerprint)) is not None:
        return ReviewFacts(
            summary.Verdict(*data["verdict"]), data["oldest"], data["not_run"], tuple(data.get("missing", ()))
        )
    facts = facts_of(monthly_review.build(conn, tenant, now))
    if stored:
        verdict = facts.verdict
        stored.save(
            tenant,
            fingerprint,
            {
                "verdict": [verdict.tone, verdict.label, verdict.headline, verdict.detail],
                "oldest": facts.oldest,
                "not_run": facts.not_run,
                "missing": list(facts.missing),
            },
        )
    return facts


def facts_of(report) -> ReviewFacts:
    """The queue's facts from a monthly review already built (the review screen)."""
    finished = [f.finished_at for f in report.freshness if f.finished_at]
    return ReviewFacts(
        summary.monthly(report).verdict,
        min(finished) if finished else None,
        sum(1 for r in report.unavailable if r.summary.startswith("This check couldn't run")),
        # A source that has never come in (Exchange refusing the app, a permission never approved) has no age
        # to be old by, so it's named on its own: the report says "not collected yet" for it.
        tuple(f.title for f in report.freshness if not f.finished_at) if finished else (),
    )


def flags_for(conn, tenant, facts: ReviewFacts, period: str, now: datetime) -> tuple[int, list[str]]:
    """New critical/high findings since the last report, and what needs a reviewer's eyes."""
    since = _last_report_at(conn, tenant["id"], period)
    counts = conn.execute(
        "SELECT SUM(severity IN ('critical', 'high')) AS severe, SUM(severity = 'critical') AS critical"
        " FROM findings WHERE tenant_id = :t AND resolved_at IS NULL AND accepted_at IS NULL"
        " AND (first_seen >= :since OR IFNULL(reopened_at, '') >= :since)",
        {"t": tenant["id"], "since": since},
    ).fetchone()
    flags = []
    if counts["critical"]:
        flags.append(f"{plural(counts['critical'], 'new critical finding')}")
    cutoff = (now - STALE_DATA).strftime(ISO)
    if facts.oldest is None:
        flags.append("No data collected yet")
    elif facts.oldest < cutoff:
        flags.append("Data older than 24 hours")
    if facts.oldest and facts.verdict.label == summary.NOT_CHECKED:
        # Sign-in failing (the client removed the app): a few sources that need no answer from Microsoft
        # still finish, but the report would only say nothing could be checked.
        flags.append("No checks could run")
    elif len(facts.missing) == 1:
        flags.append(f"{facts.missing[0]} not collected")
    elif facts.missing:
        flags.append(f"{len(facts.missing)} data sources not collected")
    if facts.not_run:
        flags.append(f"{plural(facts.not_run, 'check')} couldn't run")
    plan, _ = plan_for(conn, tenant["id"])
    if plan is not None and plan["delivery"] == "email" and not emailed(plan, conn, tenant["id"]):
        flags.append("Nobody to email")
    return counts["severe"] or 0, flags


def queue(conn, tenants, period: str, now: datetime | None = None, *, cached: bool | None = None) -> list[QueueRow]:
    """One row per visible tenant on a plan, for ``period``.

    ``cached`` (by default, when ``now`` isn't given: the pages, which show the present) reads each tenant's
    review facts from the stored rows while those are current.
    """
    cached = now is None if cached is None else cached
    now = now or datetime.now(UTC)
    rows = []
    jobs = approval_tasks(conn, period)
    planned = [t for t in tenants if not t["archived_at"] and plan_for(conn, t["id"])[0] is not None]
    facts = review_facts(conn, planned, None if cached else now)
    for t in planned:
        plan, _ = plan_for(conn, t["id"])
        r = review(conn, t["id"], period)
        state = state_of(conn, t["id"], period, r)
        severe, flags = flags_for(conn, t, facts[t["id"]], period, now)
        rows.append(
            QueueRow(
                dict(t),
                dict(plan),
                state,
                r["reviewer"] if r else None,
                facts[t["id"]].verdict,
                severe,
                flags,
                sent_at=r["sent_at"] if r else None,
                error=r["error"] if r else None,
                paused=not t["enabled"],
                task=dict(jobs[t["id"]]) if t["id"] in jobs else None,
                edited=changed_since_approval(conn, t["id"], period, r, state),
            )
        )
    rows.sort(key=lambda q: (list(STATES).index(q.state), -len(q.flags), q.tenant["name"].lower()))
    return rows


# -- Recommendations and notes ------------------------------------------------------------------------


def actions(conn, tenant_id: int) -> list:
    return conn.execute(
        "SELECT * FROM report_actions WHERE tenant_id = ? ORDER BY position, id", (tenant_id,)
    ).fetchall()


def add_action(
    conn,
    tenant_id: int,
    title: str,
    detail: str,
    user: str,
    *,
    rule_key: str | None = None,
    status: str = "planned",
    target_month: str | None = None,
) -> int:
    position = conn.execute(
        "SELECT COALESCE(MAX(position), 0) + 1 FROM report_actions WHERE tenant_id = ?", (tenant_id,)
    ).fetchone()[0]
    return conn.execute(
        "INSERT INTO report_actions (tenant_id, title, detail, rule_key, status, target_month, position,"
        " created_by, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (tenant_id, title, detail, rule_key, status, target_month, position, user, utcnow(), utcnow()),
    ).lastrowid


def update_action(conn, action, status: str, target_month: str | None) -> None:
    done_at = (action["done_at"] or utcnow()) if status == "done" else None
    conn.execute(
        "UPDATE report_actions SET status = ?, target_month = ?, done_at = ?, updated_at = ? WHERE id = ?",
        (status, target_month, done_at, utcnow(), action["id"]),
    )


def move_action(conn, action, direction: int) -> None:
    """Swap with the neighbour above (-1) or below (+1)."""
    items = actions(conn, action["tenant_id"])
    ids = [a["id"] for a in items]
    i = ids.index(action["id"])
    j = i + direction
    if not 0 <= j < len(ids):
        return
    ids[i], ids[j] = ids[j], ids[i]
    conn.executemany("UPDATE report_actions SET position = ? WHERE id = ?", [(n, a) for n, a in enumerate(ids)])


def suggestions(conn, tenant) -> list[dict]:
    """What the review screen offers for "What we recommend next": projects worth proposing (half a day or
    more, from ``projects.assess``, not declined), then quick fixes, each worst first.

    Each is ``{"rule_key", "kind", "title", "detail", "severity", "effort"}``. ``rule_key`` is a project's
    ``project:<key>``, a check's key or ``signal:<key>``; the title is what lands on the client's report."""
    found = projects.assess(conn, tenant)
    out = [
        {
            "rule_key": f"project:{rec.project.key}",
            "kind": "project",
            "title": f"Project: {rec.project.title}",
            "detail": rec.project.pitch,
            "severity": rec.severity,
            "effort": rec.effort,
        }
        for rec in found.projects
        if rec.status != "declined"
    ]
    out += [
        {
            "rule_key": r.key,
            "kind": "quick",
            "title": r.action or r.title,
            "detail": r.summary,
            "severity": r.severity,
            "effort": r.effort,
        }
        for r in found.quick_fixes
    ]
    return out


def notes(conn, tenant_id: int, staff: bool) -> list:
    where = "" if staff else " AND visibility = 'client'"
    return conn.execute(f"SELECT * FROM finding_notes WHERE tenant_id = ?{where} ORDER BY id", (tenant_id,)).fetchall()


# -- Leaving things out of one client's month --------------------------------------------------------

# Parts of the monthly review a reviewer can leave out (``part:<key>``), in the order the report has them.
PARTS = {
    "summary": "At a glance: the plain-English summary",
    "kpis": "Key figures",
    "breakdown": "Checks by severity",
    "month_chart": "Findings found and fixed each month",
    "did": "What we did",
    "next": "What we recommend next",
    "changes": "What changed",
    "secure_score": "Secure Score chart",
    "licences": "Licences",
    "passing": "In good shape",
    "accepted": "Accepted risks",
    "not_checked": "Not checked",
}
ITEM_RE = re.compile(
    r"check:[a-z0-9_]{1,80}|kpi:[a-z0-9_.]{1,80}|action:\d{1,12}|part:[a-z_]{1,40}|block:\d{1,12}:[a-z0-9]{1,16}"
    r"|notes:\d{1,12}"
)


def hidden(conn, tenant_id: int, period: str) -> set[str]:
    """What's left out of the client's copy of ``period`` (``report_hidden``)."""
    return {
        r[0]
        for r in conn.execute("SELECT item FROM report_hidden WHERE tenant_id = ? AND period = ?", (tenant_id, period))
    }


def valid_item(conn, tenant_id: int, item: str) -> bool:
    """An item a reviewer may leave out of this client's report: well formed, and naming a part, figure,
    check, design or this client's own recommendation."""
    if not ITEM_RE.fullmatch(item or ""):
        return False
    kind, _, rest = item.partition(":")
    if kind == "part":
        return rest in PARTS
    if kind == "kpi":
        return rest in {k for k, _ in designs.KPI_OPTIONS}
    if kind == "check":
        return rest in {r.key for r in RULES}
    if kind == "action":
        return (
            conn.execute(
                "SELECT 1 FROM report_actions WHERE id = ? AND tenant_id = ?", (int(rest), tenant_id)
            ).fetchone()
            is not None
        )
    design_id, _, block_id = rest.partition(":")
    row = designs.get(conn, int(design_id))
    if row is None:
        return False
    return kind == "notes" or any(b["id"] == block_id for b in designs.document(row)["blocks"])


def leave_out(conn, tenant_id: int, period: str, item: str, user: str) -> None:
    conn.execute(
        "INSERT INTO report_hidden (tenant_id, period, item, created_by, created_at) VALUES (?, ?, ?, ?, ?)"
        " ON CONFLICT(tenant_id, period, item) DO NOTHING",
        (tenant_id, period, item, user, utcnow()),
    )


def put_back(conn, tenant_id: int, period: str, item: str | None = None) -> int:
    """Put one item back in the report, or every one (``item`` None). Returns how many came back."""
    if item is None:
        return conn.execute(
            "DELETE FROM report_hidden WHERE tenant_id = ? AND period = ?", (tenant_id, period)
        ).rowcount
    return conn.execute(
        "DELETE FROM report_hidden WHERE tenant_id = ? AND period = ? AND item = ?", (tenant_id, period, item)
    ).rowcount


def report_context(conn, tenant_id: int, period: str, *, staff: bool, approved_only: bool = False) -> dict:
    """What the monthly review template shows on top of the built report, as ``month_end``.

    ``staff`` False for anything a client sees: their page and every PDF. Staff-only notes are then left out.
    ``approved_only`` (a client opening their live report) shows nothing until the month is approved or sent,
    so work in progress isn't read before it's signed off, and then the copy that was approved
    (``approved_copy``): a change made since waits until the month is approved again. What the reviewer left
    out (``hidden``) is left out either way, and callers drop the checks and figures with
    ``monthly_review.leave_out``.
    """
    r = review(conn, tenant_id, period)
    if approved_only:
        if state_of(conn, tenant_id, period, r) not in ("approved", "sent"):
            return {
                "month_end": {
                    "commentary": "",
                    "actions": [],
                    "notes": {},
                    "staff": False,
                    "period": period,
                    "hidden": hidden(conn, tenant_id, period),
                }
            }
        if (copy := approved_copy(r)) is not None:
            return {"month_end": {**copy, "staff": False, "period": period}}
    return {"month_end": {**_month_parts(conn, tenant_id, period, r, staff), "staff": staff, "period": period}}


def _month_parts(conn, tenant_id: int, period: str, r, staff: bool) -> dict:
    """The month's commentary, recommendations, notes and what's left out, as they are now."""
    left_out = hidden(conn, tenant_id, period)
    recent = period_start(shift_period(period, -1))
    items = [
        dict(
            a,
            status_label=ACTION_STATUSES[a["status"]],
            target_label=issued.period_label(a["target_month"]) if a["target_month"] else "",
        )
        for a in actions(conn, tenant_id)
        if (a["status"] != "done" or (a["done_at"] or "") >= recent) and f"action:{a['id']}" not in left_out
    ]
    by_rule: dict[str, list] = {}
    for n in notes(conn, tenant_id, staff):
        by_rule.setdefault(n["rule_key"], []).append(dict(n))
    return {"commentary": r["commentary"] if r else "", "actions": items, "notes": by_rule, "hidden": left_out}


def _copy_json(month: dict) -> str:
    """A client's copy of the month (``report_context(..., staff=False)["month_end"]``) as stored at approval."""
    return json.dumps(
        {
            "commentary": month["commentary"],
            "actions": month["actions"],
            "notes": month["notes"],
            "hidden": sorted(month["hidden"]),
        },
        sort_keys=True,
    )


def approved_copy(r) -> dict | None:
    """The client's copy of the month as it was approved, or None: never approved, approved before the copy
    was kept, or sent by an automatic plan."""
    if r is None or not r["approved_copy"]:
        return None
    copy = json.loads(r["approved_copy"])
    return {**copy, "hidden": set(copy["hidden"])}


def changed_since_approval(conn, tenant_id: int, period: str, r=None, state: str | None = None) -> bool:
    """Whether the commentary, recommendations, client-visible notes or what's left out changed since the month
    was approved: the client still sees the approved copy until it's approved again."""
    r = r if r is not None else review(conn, tenant_id, period)
    state = state or state_of(conn, tenant_id, period, r)
    if state not in ("approved", "sent") or r is None or not r["approved_copy"]:
        return False
    now = _copy_json(_month_parts(conn, tenant_id, period, r, staff=False))
    return now != json.dumps(json.loads(r["approved_copy"]), sort_keys=True)


# -- Approval ----------------------------------------------------------------------------------------


class ApprovalError(Exception):
    pass


def frozen_files(conn, tenant_id: int, period: str) -> list:
    """The files of the period's latest approval: what a re-issue sends. A file an earlier approval had and
    this one doesn't (a report dropped from the plan, or named after the tenant's old name) isn't sent.

    One approval stores each of its files once, in one transaction (``freeze``), so its rows are the newest
    run of rows with one approval time and no file name twice."""
    batch: list = []
    for row in conn.execute(
        "SELECT * FROM issued_reports WHERE tenant_id = ? AND period = ? AND trigger = 'approval' ORDER BY id DESC",
        (tenant_id, period),
    ):
        if batch and (
            row["approved_at"] != batch[0]["approved_at"] or any(row["filename"] == b["filename"] for b in batch)
        ):
            break
        batch.append(row)
    return batch[::-1]


def render_files(
    app, conn, tenant, period: str, now: datetime | None = None, *, month: dict | None = None
) -> list[tuple[str, str, bytes]]:
    """Build the plan's files for the month (the slow part of approving). Reads only, so it runs outside
    the write transaction and the worker's collections aren't held up while PDFs render. ``month`` is the
    client's copy of the month to print (``report_context``), read now when not given."""
    now = now or datetime.now(UTC)
    if month is None:
        month = report_context(conn, tenant["id"], period, staff=False)["month_end"]
    plan, _ = plan_for(conn, tenant["id"])
    if plan is None:
        raise ApprovalError(f"{tenant['name']} isn't on a report plan")
    keys = json.loads(plan["reports"] or "[]")
    report = monthly_review.build(conn, tenant, now)
    return delivery._attachments(
        app,
        conn,
        tenant,
        keys,
        bool(plan["include_xlsx"]),
        now,
        report,
        review_context={"month_end": month},
    )


def freeze(
    app,
    conn,
    tenant,
    period: str,
    user: str,
    *,
    reason: str | None = None,
    now: datetime | None = None,
    files: list[tuple[str, str, bytes]] | None = None,
    month: dict | None = None,
) -> list[int]:
    """Store the plan's files for the month, with the month's commentary and client-visible notes, in
    issued_reports and mark the month approved. ``files`` from ``render_files`` (built here when not given),
    with ``month``, the client's copy they were built from: it's kept as the approved copy the client's live
    report shows (``approved_copy``)."""
    now = now or datetime.now(UTC)
    plan, link = plan_for(conn, tenant["id"])
    if plan is None:
        raise ApprovalError(f"{tenant['name']} isn't on a report plan")
    if month is None:
        if files is not None:
            raise ValueError("files need the month they were built from")
        month = report_context(conn, tenant["id"], period, staff=False)["month_end"]
    if files is None:
        files = render_files(app, conn, tenant, period, now, month=month)
    keys = json.loads(plan["reports"] or "[]")
    by_name = {name: key for key, name in delivery.attachment_plan(tenant, keys, bool(plan["include_xlsx"]), conn)}
    known = delivery.catalog_map(conn)
    stamp = now.strftime(ISO)
    ids = issued.archive(
        conn,
        tenant["id"],
        [
            issued.IssuedFile(
                by_name.get(name, ""),
                known[by_name[name]].title if by_name.get(name) in known else name,
                name,
                content_type,
                data,
            )
            for name, content_type, data in files
        ],
        period=period,
        trigger="approval",
        user=user,
        schedule_id=link["schedule_id"],
        now=stamp,
    )
    conn.execute(
        f"UPDATE issued_reports SET approved_by = ?, approved_at = ?, reason = ?"
        f" WHERE id IN ({','.join('?' * len(ids))})",
        (user, stamp, reason, *ids),
    )
    update_review(
        conn,
        tenant["id"],
        period,
        user,
        state="approved",
        approved_by=user,
        approved_at=stamp,
        approved_copy=_copy_json(month),
        error=None,
    )
    return ids


def deliver(app, conn, vault, tenant, period: str, user: str, now: datetime | None = None) -> str | None:
    """Send the frozen files per the plan, or publish them (they're already in the report history).
    Returns an error message, or None once the month is Sent. An email plan with nobody to send to returns
    an error too: the month stays Approved until it really goes out.

    The email's delivery record, the schedule, the recipients on the issued files and the Sent state are
    written in one transaction after the email goes, so a retry after a crash either sees Sent or sends
    again; only a crash in the moment between the mail server accepting it and that commit sends twice."""
    now = now or datetime.now(UTC)
    plan, link = plan_for(conn, tenant["id"])
    rows = frozen_files(conn, tenant["id"], period)
    if plan is None or not rows:
        return "Approve it first"
    schedule = (
        conn.execute("SELECT * FROM email_schedules WHERE id = ?", (link["schedule_id"],)).fetchone()
        if link["schedule_id"]
        else None
    )
    contacts = delivery.recipients(conn, "schedule", schedule["id"]) if schedule else []

    def sent(conn) -> None:
        update_review(conn, tenant["id"], period, user, state="sent", sent_at=now.strftime(ISO), error=None)
        audit(conn, user, "report_sent", tenant["id"], period)

    if plan["delivery"] == "email" and not contacts:
        # The files are in the report history, but nobody was emailed: that isn't Sent. The month stays
        # Approved with the reason, so Send again emails these same files once the client has a contact.
        error = f"Nobody to email: {tenant['name']} has no contacts on its report plan. Add one, then send it again."
        with store.atomic(conn):
            if schedule is not None:
                # The month is done, so an automatic plan's timer doesn't email a live copy of it later.
                conn.execute(
                    f"UPDATE email_schedules SET last_period = {delivery.LATER_PERIOD}, attempts = 0,"
                    " retry_at = NULL WHERE id = ?",
                    (period, period, schedule["id"]),
                )
            update_review(conn, tenant["id"], period, user, error=error)
            audit(conn, user, "report_send_failed", tenant["id"], period)
        return error
    if plan["delivery"] == "email":
        to, bcc = delivery.split_recipients(contacts)

        def emailed(conn) -> None:
            conn.execute(
                f"UPDATE issued_reports SET recipients = ?, bcc = ? WHERE id IN ({','.join('?' * len(rows))})",
                (", ".join(to), ", ".join(bcc), *[r["id"] for r in rows]),
            )
            sent(conn)

        files = [(r["filename"], r["content_type"], bytes(r["data"])) for r in rows]
        error = delivery.send_frozen(
            app, conn, vault, schedule, tenant, period, files, user=user, now=now, on_sent=emailed
        )
        if error:
            with store.atomic(conn):
                update_review(conn, tenant["id"], period, user, error=error[:1000])
                audit(conn, user, "report_send_failed", tenant["id"], period)
        else:
            delivery.report_ready(app, conn, vault, tenant, period, list(dict.fromkeys(r["title"] for r in rows)))
        return error
    with store.atomic(conn):
        if schedule is not None:
            # Published only: the period is done, so an automatic schedule doesn't send it again.
            conn.execute(
                f"UPDATE email_schedules SET last_period = {delivery.LATER_PERIOD}, attempts = 0,"
                " retry_at = NULL WHERE id = ?",
                (period, period, schedule["id"]),
            )
        sent(conn)
    delivery.report_ready(
        app, conn, vault, tenant, period, list(dict.fromkeys(r["title"] for r in rows)), emailed=False
    )
    return None


def approve(
    app, conn, vault, tenant, period: str, user: str, *, reason: str | None = None, now: datetime | None = None
) -> str | None:
    """Freeze and send (or publish) in this thread. Approving a month that was already approved is a re-issue
    and needs ``reason``; it stores the next version. Returns a send error, or None. The portal queues
    ``queue_approval`` instead, which does the same in the worker."""
    state = state_of(conn, tenant["id"], period)
    if state in ("approved", "sent") and not (reason or "").strip():
        raise ApprovalError("Say why it's being issued again")
    freeze(app, conn, tenant, period, user, reason=(reason or "").strip() or None, now=now)
    return deliver(app, conn, vault, tenant, period, user, now=now)


# -- Approval in the background ------------------------------------------------------------------


def approval_key(tenant_id: int, period: str) -> str:
    """One approval (or send) per tenant and period at a time."""
    return f"approve:{tenant_id}:{period}"


def queue_approval(
    conn,
    tenant,
    period: str,
    user: str,
    *,
    reason: str | None = None,
    send_only: bool = False,
    batch: str | None = None,
) -> int:
    """Queue approving (freeze, then send or publish) a client's month, or with ``send_only`` sending the
    approved files again. Asking while one is queued or running returns that one, so it happens once."""
    return tasks.enqueue(
        conn,
        "approve",
        key=approval_key(tenant["id"], period),
        tenant_id=tenant["id"],
        payload={"period": period, "user": user, "reason": reason, "send_only": send_only},
        batch=batch,
        priority=tasks.PRIORITY_WAITING,
        user=user,
    )


def approval_task(conn, tenant_id: int, period: str):
    """The latest approval task for the client's month (queued, running or finished), or None."""
    return tasks.latest(conn, approval_key(tenant_id, period))


def approval_tasks(conn, period: str) -> dict:
    """{tenant id: latest approval task} for the month."""
    rows = conn.execute(
        "SELECT * FROM tasks WHERE id IN (SELECT MAX(id) FROM tasks WHERE kind = 'approve'"
        " AND key LIKE ? GROUP BY tenant_id)",
        (f"approve:%:{period}",),
    ).fetchall()
    return {r["tenant_id"]: r for r in rows}


def new_batch(period: str) -> str:
    return f"approve:{period}:{datetime.now(UTC).strftime('%Y%m%d%H%M%S%f')}"


def latest_batch(conn, period: str) -> str | None:
    row = conn.execute(
        "SELECT batch FROM tasks WHERE batch LIKE ? ORDER BY id DESC LIMIT 1", (f"approve:{period}:%",)
    ).fetchone()
    return row["batch"] if row else None


@tasks.handler("approve")
def _approve_task(ctx: tasks.TaskContext) -> dict:
    """Freeze the month, then send or publish it. The freeze is checkpointed in its own transaction, so a
    worker that dies before the email goes doesn't freeze a second version when the task runs again."""
    conn, p = ctx.conn, ctx.payload
    period, user, reason = p["period"], p["user"], p.get("reason")
    tenant = ctx.tenant
    if tenant is None:
        raise tasks.TaskFailed("The client has been removed")
    if p.get("send_only"):
        if state_of(conn, tenant["id"], period) != "approved":
            return {"state": state_of(conn, tenant["id"], period)}
    elif ctx.done != "frozen":
        if not tenant["enabled"] or tenant["archived_at"]:
            raise tasks.TaskFailed("The client is paused")
        if state_of(conn, tenant["id"], period) in ("approved", "sent") and not reason:
            return {"state": state_of(conn, tenant["id"], period), "skipped": "already issued"}
        ctx.progress("Building the reports")
        # Read once: the files and the approved copy the client's live report shows are the same month, even
        # if someone edits the commentary while the files are built.
        month = report_context(conn, tenant["id"], period, staff=False)["month_end"]
        try:
            files = render_files(ctx.app, conn, tenant, period, month=month)
        except ApprovalError as exc:
            raise tasks.TaskFailed(str(exc)) from exc
        except Exception as exc:
            log.exception("Approving %s for tenant %s failed", period, tenant["id"])
            raise tasks.TaskFailed(f"Couldn't build the report: {type(exc).__name__}") from exc
        with transaction(conn, immediate=True):
            # Checked again inside the write lock: someone may have approved it while the files were built.
            if state_of(conn, tenant["id"], period) in ("approved", "sent") and not reason:
                return {"state": state_of(conn, tenant["id"], period), "skipped": "already issued"}
            freeze(ctx.app, conn, tenant, period, user, reason=reason, files=files, month=month)
            audit(
                conn,
                user,
                "report_reissued" if reason else "report_approved",
                tenant["id"],
                period + (f": {reason[:200]}" if reason else ""),
            )
            ctx.checkpoint("frozen")
    if state_of(conn, tenant["id"], period) == "sent":
        return {"state": "sent"}  # an earlier attempt sent it and then stopped
    plan, _ = plan_for(conn, tenant["id"])
    ctx.progress("Sending" if plan and emailed(plan, conn, tenant["id"]) else "Publishing")
    with mailer.patient():  # a bulk approval sends faster than Microsoft 365 takes them; wait our turn
        error = deliver(ctx.app, conn, ctx.vault, tenant, period, user)
    if error:
        raise tasks.TaskFailed(error)
    return {"state": "sent"}


def emailed(plan, conn, tenant_id: int) -> bool:
    """Whether approving emails anyone (else it only publishes to the report history)."""
    _, link = plan_for(conn, tenant_id)
    return plan["delivery"] == "email" and bool(
        link and link["schedule_id"] and delivery.recipients(conn, "schedule", link["schedule_id"])
    )
