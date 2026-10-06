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
  with a reason.

Reporting only: nothing here reads or writes Microsoft 365. Roles: staff
(admin and analyst) review, comment and assign; only admins edit plans and
approve, send or re-issue.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from . import delivery, issued
from .db import utcnow
from .reports import monthly_review, summary
from .text import plural

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
        " FROM report_plans p ORDER BY p.name COLLATE NOCASE").fetchall()


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


PLAN_COLUMNS = ("name", "reports", "include_xlsx", "day_of_month", "hour", "timezone", "subject", "message",
                "mode", "delivery", "bcc_team")


def save_plan(conn, values: dict, user: str, plan_id: int | None = None) -> int:
    params = [json.dumps(values["reports"]) if k == "reports" else values[k] for k in PLAN_COLUMNS]
    if plan_id is None:
        return conn.execute(
            f"INSERT INTO report_plans ({', '.join(PLAN_COLUMNS)}, created_by, created_at, updated_at)"
            f" VALUES ({', '.join('?' * len(PLAN_COLUMNS))}, ?, ?, ?)", (*params, user, utcnow(), utcnow())).lastrowid
    conn.execute(f"UPDATE report_plans SET {', '.join(c + ' = ?' for c in PLAN_COLUMNS)}, updated_at = ?"
                 " WHERE id = ?", (*params, utcnow(), plan_id))
    return plan_id


def _plan_recipients(conn, plan, tenant_id: int) -> list[int]:
    if plan["delivery"] != "email":
        return []
    ids = [c["id"] for c in delivery.tenant_contacts(conn, tenant_id)]
    if ids and plan["bcc_team"]:
        ids += [c["id"] for c in delivery.team_contacts(conn)]
    return ids[:delivery.mailer.MAX_RECIPIENTS]


def apply_plan(conn, plan, tenant_ids: list[int], user: str, default_tz: str, now: datetime | None = None) -> int:
    """Put tenants on ``plan``: create or update each one's plan schedule. Returns how many were applied.

    A tenant moves off any other plan. Like saving a schedule by hand, a new or re-timed schedule counts
    the current period as done, so applying never sends straight away."""
    now = now or datetime.now(UTC)
    tz = plan["timezone"] or default_tz
    fresh = delivery.initial_period("monthly", 0, plan["day_of_month"], plan["hour"], tz, now)
    cols = {"reports": plan["reports"], "include_xlsx": plan["include_xlsx"], "message": plan["message"],
            "subject": plan["subject"], "frequency": "monthly", "day_of_week": 0,
            "day_of_month": plan["day_of_month"], "hour": plan["hour"], "timezone": plan["timezone"],
            "enabled": 1, "plan_id": plan["id"], "on_approval": int(waits_for_approval(plan))}
    timing = ("day_of_month", "hour", "timezone")
    for tenant_id in tenant_ids:
        link = conn.execute("SELECT * FROM report_plan_tenants WHERE tenant_id = ?", (tenant_id,)).fetchone()
        schedule = conn.execute("SELECT * FROM email_schedules WHERE id = ?",
                                (link["schedule_id"],)).fetchone() if link and link["schedule_id"] else None
        if schedule is None:
            schedule_id = conn.execute(
                f"INSERT INTO email_schedules (kind, tenant_id, {', '.join(cols)}, last_period, created_by,"
                f" created_at) VALUES ('report', ?, {', '.join('?' * len(cols))}, ?, ?, ?)",
                (tenant_id, *cols.values(), fresh, user, utcnow())).lastrowid
        else:
            schedule_id = schedule["id"]
            conn.execute(f"UPDATE email_schedules SET {', '.join(c + ' = ?' for c in cols)} WHERE id = ?",
                         (*cols.values(), schedule_id))
            # Re-timed, or switched between automatic and send on approval: start counting from now, so
            # a month nobody approved isn't emailed by the timer. Never move back past a period already done.
            if (any(schedule[k] != cols[k] for k in timing) or schedule["frequency"] != "monthly"
                    or schedule["on_approval"] != cols["on_approval"]):
                start = fresh if schedule["frequency"] != "monthly" else max(fresh, schedule["last_period"] or "")
                conn.execute("UPDATE email_schedules SET last_period = ?, attempts = 0, retry_at = NULL"
                             " WHERE id = ?", (start, schedule_id))
        delivery.set_recipients(conn, "schedule", schedule_id, _plan_recipients(conn, plan, tenant_id))
        conn.execute(
            "INSERT INTO report_plan_tenants (tenant_id, plan_id, schedule_id, added_by, added_at)"
            " VALUES (?, ?, ?, ?, ?) ON CONFLICT(tenant_id) DO UPDATE SET plan_id = excluded.plan_id,"
            " schedule_id = excluded.schedule_id", (tenant_id, plan["id"], schedule_id, user, utcnow()))
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
    """A new client contact joins the tenant's plan email, so the plan keeps reaching everyone."""
    plan, link = plan_for(conn, tenant_id)
    if plan is not None and link["schedule_id"] and plan["delivery"] == "email":
        conn.execute("INSERT OR IGNORE INTO email_schedule_recipients (schedule_id, contact_id) VALUES (?, ?)",
                     (link["schedule_id"], contact_id))


# -- Coverage ---------------------------------------------------------------------------------


@dataclass
class CoverageRow:
    tenant: dict
    plan: str | None
    monthly: int            # enabled monthly report schedules (or a portal-only plan)
    contacts: int
    recipients: int         # people on those schedules
    gaps: list[str] = field(default_factory=list)   # no_report | no_contacts | no_recipients


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
        text = f"{self.covered} of {plural(self.total, 'client')} {'has' if self.total == 1 else 'have'}" \
               " a monthly report"
        parts = []
        if self.count("no_contacts"):
            n = self.count("no_contacts")
            parts.append(f"{n} {'has' if n == 1 else 'have'} no contacts")
        if self.count("no_recipients"):
            n = self.count("no_recipients")
            parts.append(f"{n} {'sends' if n == 1 else 'send'} to nobody")
        return text + ("; " + "; ".join(parts) if parts else "")


def coverage(conn, tenants) -> Coverage:
    """Which active clients get a monthly report, and what's missing for the rest."""
    rows, paused = [], 0
    for t in tenants:
        if not t["enabled"] or t["archived_at"]:
            paused += 1
            continue
        plan, _ = plan_for(conn, t["id"])
        schedules = conn.execute("SELECT * FROM email_schedules WHERE tenant_id = ? AND kind = 'report'"
                                 " AND enabled = 1 AND frequency = 'monthly'", (t["id"],)).fetchall()
        contacts = len(delivery.tenant_contacts(conn, t["id"]))
        people = {c["id"] for s in schedules for c in delivery.recipients(conn, "schedule", s["id"])}
        portal = plan is not None and plan["delivery"] == "portal"
        row = CoverageRow(dict(t), plan["name"] if plan else None, len(schedules) or int(portal), contacts,
                          len(people))
        if not row.monthly:
            row.gaps.append("no_report")
        if not contacts and not portal:
            row.gaps.append("no_contacts")
        elif schedules and not people and not portal:
            row.gaps.append("no_recipients")
        rows.append(row)
    return Coverage(rows, paused)


# -- Reviews ------------------------------------------------------------------------------------


def review(conn, tenant_id: int, period: str):
    return conn.execute("SELECT * FROM report_reviews WHERE tenant_id = ? AND period = ?",
                        (tenant_id, period)).fetchone()


def update_review(conn, tenant_id: int, period: str, user: str | None, **fields) -> None:
    """Create the month's review row if it's still implicit (Draft, or Sent by an automatic plan), keeping
    that state, then set ``fields``."""
    if review(conn, tenant_id, period) is None:
        conn.execute("INSERT INTO report_reviews (tenant_id, period, state, updated_by, updated_at)"
                     " VALUES (?, ?, ?, ?, ?) ON CONFLICT(tenant_id, period) DO NOTHING",
                     (tenant_id, period, state_of(conn, tenant_id, period), user, utcnow()))
    if fields:
        conn.execute(f"UPDATE report_reviews SET {', '.join(k + ' = ?' for k in fields)}, updated_by = ?,"
                     " updated_at = ? WHERE tenant_id = ? AND period = ?",
                     (*fields.values(), user, utcnow(), tenant_id, period))


def state_of(conn, tenant_id: int, period: str, row=None) -> str:
    row = row if row is not None else review(conn, tenant_id, period)
    if row is not None:
        return row["state"]
    # An automatic plan that already went out on its timer (another schedule's report doesn't count).
    sent = conn.execute(
        "SELECT 1 FROM issued_reports WHERE tenant_id = ? AND period = ? AND (trigger = 'approval' OR schedule_id ="
        " (SELECT schedule_id FROM report_plan_tenants WHERE tenant_id = ?)) LIMIT 1",
        (tenant_id, period, tenant_id)).fetchone()
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

    @property
    def state_label(self) -> str:
        if self.state == "sent" and self.plan["delivery"] == "portal":
            return "Published"
        return STATES[self.state]

    @property
    def ready(self) -> bool:
        return self.state in ("draft", "in_review") and not self.flags and not self.paused


def _last_report_at(conn, tenant_id: int, period: str) -> str:
    row = conn.execute("SELECT MAX(issued_at) FROM issued_reports WHERE tenant_id = ? AND period < ?",
                       (tenant_id, period)).fetchone()
    return row[0] or period_start(shift_period(period, -1))


def flags_for(conn, tenant, report, period: str, now: datetime) -> tuple[int, list[str]]:
    """New critical/high findings since the last report, and what needs a reviewer's eyes."""
    since = _last_report_at(conn, tenant["id"], period)
    counts = conn.execute(
        "SELECT SUM(severity IN ('critical', 'high')) AS severe, SUM(severity = 'critical') AS critical"
        " FROM findings WHERE tenant_id = :t AND resolved_at IS NULL AND accepted_at IS NULL"
        " AND (first_seen >= :since OR IFNULL(reopened_at, '') >= :since)",
        {"t": tenant["id"], "since": since}).fetchone()
    flags = []
    if counts["critical"]:
        flags.append(f"{plural(counts['critical'], 'new critical finding')}")
    finished = [f.finished_at for f in report.freshness if f.finished_at]
    cutoff = (now - STALE_DATA).strftime(ISO)
    if not finished:
        flags.append("No data collected yet")
    elif min(finished) < cutoff:
        flags.append("Data older than 24 hours")
    not_run = sum(1 for r in report.unavailable if r.summary.startswith("This check couldn't run"))
    if not_run:
        flags.append(f"{plural(not_run, 'check')} couldn't run")
    return counts["severe"] or 0, flags


def queue(conn, tenants, period: str, now: datetime | None = None) -> list[QueueRow]:
    """One row per visible tenant on a plan, for ``period``."""
    now = now or datetime.now(UTC)
    rows = []
    for t in tenants:
        plan, _ = plan_for(conn, t["id"])
        if plan is None or t["archived_at"]:
            continue
        r = review(conn, t["id"], period)
        state = state_of(conn, t["id"], period, r)
        report = monthly_review.build(conn, t, now)
        severe, flags = flags_for(conn, t, report, period, now)
        rows.append(QueueRow(dict(t), dict(plan), state, r["reviewer"] if r else None,
                             summary.monthly(report).verdict, severe, flags,
                             sent_at=r["sent_at"] if r else None, error=r["error"] if r else None,
                             paused=not t["enabled"]))
    rows.sort(key=lambda q: (list(STATES).index(q.state), -len(q.flags), q.tenant["name"].lower()))
    return rows


# -- Recommendations and notes ------------------------------------------------------------------------


def actions(conn, tenant_id: int) -> list:
    return conn.execute("SELECT * FROM report_actions WHERE tenant_id = ? ORDER BY position, id",
                        (tenant_id,)).fetchall()


def add_action(conn, tenant_id: int, title: str, detail: str, user: str, *, rule_key: str | None = None,
               status: str = "planned", target_month: str | None = None) -> int:
    position = conn.execute("SELECT COALESCE(MAX(position), 0) + 1 FROM report_actions WHERE tenant_id = ?",
                            (tenant_id,)).fetchone()[0]
    return conn.execute(
        "INSERT INTO report_actions (tenant_id, title, detail, rule_key, status, target_month, position,"
        " created_by, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (tenant_id, title, detail, rule_key, status, target_month, position, user, utcnow(), utcnow())).lastrowid


def update_action(conn, action, status: str, target_month: str | None) -> None:
    done_at = (action["done_at"] or utcnow()) if status == "done" else None
    conn.execute("UPDATE report_actions SET status = ?, target_month = ?, done_at = ?, updated_at = ? WHERE id = ?",
                 (status, target_month, done_at, utcnow(), action["id"]))


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


def suggestions(report) -> list[dict]:
    """Failing checks as recommendation wording, worst first, for the TL to pick from."""
    out = []
    for r in report.failing:
        text = getattr(r, "action_client", None) or r.action
        out.append({"rule_key": r.key, "title": text, "detail": r.summary, "severity": r.severity})
    return out


def notes(conn, tenant_id: int, staff: bool) -> list:
    where = "" if staff else " AND visibility = 'client'"
    return conn.execute(f"SELECT * FROM finding_notes WHERE tenant_id = ?{where} ORDER BY id",
                        (tenant_id,)).fetchall()


def report_context(conn, tenant_id: int, period: str, *, staff: bool, approved_only: bool = False) -> dict:
    """What the monthly review template shows on top of the built report, as ``month_end``.

    ``staff`` False for anything a client sees: their page and every PDF. Staff-only notes are then left out.
    ``approved_only`` (a client opening their live report) shows nothing until the month is approved or sent,
    so work in progress isn't read before it's signed off.
    """
    r = review(conn, tenant_id, period)
    if approved_only and state_of(conn, tenant_id, period, r) not in ("approved", "sent"):
        return {"month_end": {"commentary": "", "actions": [], "notes": {}, "staff": False, "period": period}}
    recent = period_start(shift_period(period, -1))
    items = [dict(a, status_label=ACTION_STATUSES[a["status"]],
                  target_label=issued.period_label(a["target_month"]) if a["target_month"] else "")
             for a in actions(conn, tenant_id) if a["status"] != "done" or (a["done_at"] or "") >= recent]
    by_rule: dict[str, list] = {}
    for n in notes(conn, tenant_id, staff):
        by_rule.setdefault(n["rule_key"], []).append(dict(n))
    return {"month_end": {"commentary": r["commentary"] if r else "", "actions": items, "notes": by_rule,
                          "staff": staff, "period": period}}


# -- Approval ----------------------------------------------------------------------------------------


class ApprovalError(Exception):
    pass


def frozen_files(conn, tenant_id: int, period: str) -> list:
    """The latest approved version of each file for the period."""
    return conn.execute(
        "SELECT * FROM issued_reports WHERE id IN (SELECT MAX(id) FROM issued_reports WHERE tenant_id = ?"
        " AND period = ? AND trigger = 'approval' GROUP BY filename) ORDER BY id", (tenant_id, period)).fetchall()


def freeze(app, conn, tenant, period: str, user: str, *, reason: str | None = None,
           now: datetime | None = None) -> list[int]:
    """Render the plan's files once, with the month's commentary and client-visible notes, into issued_reports."""
    now = now or datetime.now(UTC)
    plan, link = plan_for(conn, tenant["id"])
    if plan is None:
        raise ApprovalError(f"{tenant['name']} isn't on a report plan")
    keys = json.loads(plan["reports"] or "[]")
    report = monthly_review.build(conn, tenant, now)
    files = delivery._attachments(app, conn, tenant, keys, bool(plan["include_xlsx"]), now, report,
                                  review_context=report_context(conn, tenant["id"], period, staff=False))
    by_name = {name: key for key, name in delivery.attachment_plan(tenant, keys, bool(plan["include_xlsx"]))}
    known = delivery.catalog_map()
    stamp = now.strftime(ISO)
    ids = issued.archive(conn, tenant["id"], [
        issued.IssuedFile(by_name.get(name, ""), known[by_name[name]].title if by_name.get(name) in known else name,
                          name, content_type, data) for name, content_type, data in files],
        period=period, trigger="approval", user=user, schedule_id=link["schedule_id"], now=stamp)
    conn.execute(f"UPDATE issued_reports SET approved_by = ?, approved_at = ?, reason = ?"
                 f" WHERE id IN ({','.join('?' * len(ids))})", (user, stamp, reason, *ids))
    update_review(conn, tenant["id"], period, user, state="approved", approved_by=user, approved_at=stamp,
                  error=None)
    return ids


def deliver(app, conn, vault, tenant, period: str, user: str, now: datetime | None = None) -> str | None:
    """Send the frozen files per the plan, or publish them (they're already in the report history).
    Returns an error message, or None once the month is Sent."""
    now = now or datetime.now(UTC)
    plan, link = plan_for(conn, tenant["id"])
    rows = frozen_files(conn, tenant["id"], period)
    if plan is None or not rows:
        return "Approve it first"
    schedule = conn.execute("SELECT * FROM email_schedules WHERE id = ?",
                            (link["schedule_id"],)).fetchone() if link["schedule_id"] else None
    contacts = delivery.recipients(conn, "schedule", schedule["id"]) if schedule else []
    if plan["delivery"] == "email" and contacts:
        files = [(r["filename"], r["content_type"], bytes(r["data"])) for r in rows]
        error = delivery.send_frozen(app, conn, vault, schedule, tenant, period, files, user=user, now=now)
        if error:
            update_review(conn, tenant["id"], period, user, error=error[:1000])
            return error
        to, bcc = delivery.split_recipients(contacts)
        conn.execute(f"UPDATE issued_reports SET recipients = ?, bcc = ? WHERE id IN ({','.join('?' * len(rows))})",
                     (", ".join(to), ", ".join(bcc), *[r["id"] for r in rows]))
    elif schedule is not None:
        # Published only: the period is done, so an automatic schedule doesn't send it again.
        conn.execute(f"UPDATE email_schedules SET last_period = {delivery.LATER_PERIOD}, attempts = 0,"
                     " retry_at = NULL WHERE id = ?", (period, period, schedule["id"]))
    update_review(conn, tenant["id"], period, user, state="sent", sent_at=now.strftime(ISO), error=None)
    return None


def approve(app, conn, vault, tenant, period: str, user: str, *, reason: str | None = None,
            now: datetime | None = None) -> str | None:
    """Freeze and send (or publish). Approving a month that was already approved is a re-issue and
    needs ``reason``; it stores the next version. Returns a send error, or None."""
    state = state_of(conn, tenant["id"], period)
    if state in ("approved", "sent") and not (reason or "").strip():
        raise ApprovalError("Say why it's being issued again")
    freeze(app, conn, tenant, period, user, reason=(reason or "").strip() or None, now=now)
    return deliver(app, conn, vault, tenant, period, user, now=now)


def emailed(plan, conn, tenant_id: int) -> bool:
    """Whether approving emails anyone (else it only publishes to the report history)."""
    _, link = plan_for(conn, tenant_id)
    return plan["delivery"] == "email" and bool(
        link and link["schedule_id"] and delivery.recipients(conn, "schedule", link["schedule_id"]))
