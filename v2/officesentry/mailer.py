"""Emailing reports over SMTP, and the monthly schedule."""

from __future__ import annotations

import re
import smtplib
import ssl
from datetime import UTC, datetime
from email.message import EmailMessage
from zoneinfo import ZoneInfo

from . import store
from .db import utcnow

EMAIL_RE = re.compile(r"^[^@\s,;<>]+@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+$")
MAX_RECIPIENTS = 20
SMTP_KEYS = ("smtp_host", "smtp_port", "smtp_security", "smtp_user", "smtp_password_enc", "smtp_from")


class MailNotConfigured(Exception):
    pass


def parse_recipients(raw: str) -> list[str] | None:
    """Split a comma/semicolon/newline separated list; None if any address is invalid."""
    addrs = [a.strip() for a in re.split(r"[,;\s]+", raw or "") if a.strip()]
    if not addrs or len(addrs) > MAX_RECIPIENTS or not all(EMAIL_RE.match(a) for a in addrs):
        return None
    return list(dict.fromkeys(addrs))


def smtp_settings(conn) -> dict:
    rows = conn.execute(
        f"SELECT key, value FROM settings WHERE key IN ({','.join('?' * len(SMTP_KEYS))})", SMTP_KEYS
    ).fetchall()
    return {r["key"]: r["value"] for r in rows}


def send(conn, vault, to: list[str], subject: str, body: str, attachments=()) -> None:
    s = smtp_settings(conn)
    if not s.get("smtp_host") or not s.get("smtp_from"):
        raise MailNotConfigured("Email isn't set up yet (Admin → Email settings)")
    msg = EmailMessage()
    msg["From"] = s["smtp_from"]
    msg["To"] = ", ".join(to)
    msg["Subject"] = subject
    msg.set_content(body)
    for filename, mimetype, data in attachments:
        maintype, subtype = mimetype.split("/", 1)
        msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=filename)

    port = int(s.get("smtp_port") or 587)
    security = s.get("smtp_security", "starttls")
    context = ssl.create_default_context()
    if security == "ssl":
        server = smtplib.SMTP_SSL(s["smtp_host"], port, context=context, timeout=60)
    else:
        server = smtplib.SMTP(s["smtp_host"], port, timeout=60)
    with server:
        if security == "starttls":
            server.starttls(context=context)
        if s.get("smtp_user"):
            password = vault.decrypt(s["smtp_password_enc"]) if s.get("smtp_password_enc") else ""
            server.login(s["smtp_user"], password)
        server.send_message(msg)


def report_email(app, conn, tenant) -> tuple[str, str, list]:
    """Subject, plain-text body and attachments for a tenant's monthly review."""
    from contextlib import nullcontext

    from flask import has_request_context, render_template

    from .reports import export
    from .reports.monthly_review import build

    report = build(conn, tenant)
    month = report.generated_at.strftime("%B %Y")
    subject = f"{report.title}: {tenant['name']} ({month})"
    lines = [f"{report.title} for {tenant['name']}, {month}.", ""]
    for k in report.kpis:
        if k.value is None:
            continue
        value = f"{report.currency} {k.value:,.2f}" if k.unit == "currency" else f"{k.value:g}{k.unit}"
        change = f" ({k.delta:+g} in 4 weeks)" if k.delta else ""
        lines.append(f"  {k.label}: {value}{change}")
    lines.append("")
    counts = ", ".join(f"{n} {sev}" for sev, n in report.counts.items() if n and sev != "info")
    lines.append(f"Checks needing attention: {len(report.failing)}" + (f" ({counts})" if counts else ""))
    if report.new_count:
        lines.append(f"New since last month: {report.new_count}")
    if report.resolved:
        lines.append(f"Fixed since last month: {len(report.resolved)}")
    lines += ["", "The full report is attached."]

    slug = re.sub(r"[^a-z0-9]+", "-", tenant["name"].lower()).strip("-") or "tenant"
    attachments = [(f"security-review-{slug}.xlsx",
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", export.to_xlsx(report))]
    if export.pdf_available():
        static = app.static_folder
        # Inside a web request ("Send now") reuse it: a nested request context
        # would run teardown and close the request's database connection.
        ctx = nullcontext() if has_request_context() else \
            app.test_request_context(base_url=app.extensions["officesentry.config"].base_url)
        with ctx:
            html = render_template("reports/monthly_review.html", report=report, pdf=True,
                                   css_href=f"file://{static}/report.css", pdf_available=True)
        attachments.insert(0, (f"security-review-{slug}.pdf", "application/pdf",
                               export.to_pdf(html, f"file://{static}/")))
    return subject, "\n".join(lines), attachments


def deliver(app, conn, vault, tenant, recipients: list[str], schedule_id: int | None = None) -> str | None:
    """Send the report; record the delivery. Returns an error message or None."""
    try:
        subject, body, attachments = report_email(app, conn, tenant)
        send(conn, vault, recipients, subject, body, attachments)
        error = None
    except (MailNotConfigured, smtplib.SMTPException, OSError) as exc:
        error = f"{type(exc).__name__}: {exc}"[:500]
    conn.execute(
        "INSERT INTO report_deliveries (schedule_id, tenant_id, recipients, sent_at, status, error)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (schedule_id, tenant["id"], ", ".join(recipients), utcnow(), "failed" if error else "sent", error),
    )
    return error


def due_schedules(conn, tz: str, now: datetime | None = None) -> list:
    """Enabled schedules whose day/hour has passed this month and haven't sent this month.

    A failed attempt is retried after an hour rather than every tick.
    """
    local = (now or datetime.now(UTC)).astimezone(ZoneInfo(tz))
    period = local.strftime("%Y-%m")
    retry_cutoff = ((now or datetime.now(UTC)).timestamp() - 3600)
    due = []
    for s in conn.execute("SELECT * FROM report_schedules WHERE enabled = 1"):
        if s["last_period"] == period:
            continue
        if (local.day, local.hour) < (s["day_of_month"], s["hour"]):
            continue
        last_fail = conn.execute(
            "SELECT sent_at FROM report_deliveries WHERE schedule_id = ? AND status = 'failed'"
            " ORDER BY id DESC LIMIT 1", (s["id"],)).fetchone()
        if last_fail and datetime.fromisoformat(last_fail["sent_at"].replace("Z", "+00:00")).timestamp() > retry_cutoff:
            continue
        due.append((s, period))
    return due


def run_due(app, conn, vault, tz: str, now: datetime | None = None) -> int:
    sent = 0
    for schedule, period in due_schedules(conn, tz, now):
        tenant = store.get_tenant(conn, schedule["tenant_id"])
        recipients = parse_recipients(schedule["recipients"]) or []
        if tenant is None or not recipients:
            continue
        if deliver(app, conn, vault, tenant, recipients, schedule["id"]) is None:
            conn.execute("UPDATE report_schedules SET last_period = ? WHERE id = ?", (period, schedule["id"]))
            sent += 1
    return sent
