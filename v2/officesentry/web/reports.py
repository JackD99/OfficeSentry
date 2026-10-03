"""Report pages: per-tenant review (HTML/PDF/XLSX), cross-tenant overview, licence prices."""

from __future__ import annotations

import re
import smtplib
from pathlib import Path

from flask import Blueprint, Response, abort, current_app, flash, g, redirect, render_template, request, url_for

from .. import findings, mailer
from ..db import audit, transaction, utcnow
from ..licences import sku_name
from ..reports import data_reports, evidence, export
from ..reports.monthly_review import build, setting
from ..rules import RULES
from .access import login_required, roles_required, tenant_or_404

RULE_TITLES = {r.key: r.title for r in RULES}

bp = Blueprint("reports", __name__)

STATIC_DIR = Path(__file__).parent / "static"
CURRENCIES = ("GBP", "USD", "EUR", "AUD", "CAD", "NZD")


def _filename(tenant, ext: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", tenant["name"].lower()).strip("-") or "tenant"
    return f"security-review-{slug}.{ext}"


@bp.get("/reports")
@login_required
def overview():
    return redirect(url_for("tenants.dashboard"))  # the dashboard now carries the overview


@bp.get("/tenants/<int:tenant_id>/report")
@login_required
def tenant_report(tenant_id: int):
    t = tenant_or_404(tenant_id)
    report = build(g.db, t)
    return render_template("reports/monthly_review.html", report=report, pdf=False,
                           css_href=url_for("static", filename="report.css"),
                           pdf_available=export.pdf_available())


@bp.get("/tenants/<int:tenant_id>/report.pdf")
@login_required
def tenant_report_pdf(tenant_id: int):
    t = tenant_or_404(tenant_id)
    if not export.pdf_available():
        abort(404)
    report = build(g.db, t)
    html = render_template("reports/monthly_review.html", report=report, pdf=True,
                           css_href=(STATIC_DIR / "report.css").as_uri(), pdf_available=True)
    audit(g.db, g.user["username"], "report_downloaded", t["id"], "pdf")
    return Response(export.to_pdf(html, STATIC_DIR.as_uri() + "/"), mimetype="application/pdf",
                    headers={"Content-Disposition": f"attachment; filename={_filename(t, 'pdf')}"})


@bp.get("/tenants/<int:tenant_id>/report.xlsx")
@login_required
def tenant_report_xlsx(tenant_id: int):
    t = tenant_or_404(tenant_id)
    audit(g.db, g.user["username"], "report_downloaded", t["id"], "xlsx")
    return Response(
        export.to_xlsx(build(g.db, t)),
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename={_filename(t, 'xlsx')}"},
    )


def _evidence_name(tenant, ext: str) -> str:
    return _filename(tenant, ext).replace("security-review", "security-evidence")


@bp.get("/tenants/<int:tenant_id>/evidence")
@login_required
def evidence_pack(tenant_id: int):
    t = tenant_or_404(tenant_id)
    return render_template("evidence_page.html", pack=evidence.build(g.db, t), pdf_available=export.pdf_available())


@bp.get("/tenants/<int:tenant_id>/evidence.pdf")
@login_required
def evidence_pdf(tenant_id: int):
    t = tenant_or_404(tenant_id)
    if not export.pdf_available():
        abort(404)
    html = render_template("reports/evidence.html", pack=evidence.build(g.db, t), pdf=True,
                           css_href=(STATIC_DIR / "report.css").as_uri(), pdf_available=True)
    audit(g.db, g.user["username"], "report_downloaded", t["id"], "evidence.pdf")
    return Response(export.to_pdf(html, STATIC_DIR.as_uri() + "/"), mimetype="application/pdf",
                    headers={"Content-Disposition": f"attachment; filename={_evidence_name(t, 'pdf')}"})


@bp.get("/tenants/<int:tenant_id>/evidence.xlsx")
@login_required
def evidence_xlsx(tenant_id: int):
    t = tenant_or_404(tenant_id)
    audit(g.db, g.user["username"], "report_downloaded", t["id"], "evidence.xlsx")
    return Response(
        evidence.to_xlsx(evidence.build(g.db, t)),
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename={_evidence_name(t, 'xlsx')}"},
    )


@bp.get("/tenants/<int:tenant_id>/data/<key>")
@login_required
def data_report(tenant_id: int, key: str):
    t = tenant_or_404(tenant_id)
    if key not in data_reports.REPORTS:
        abort(404)
    return render_template("data_report.html", tenant=t, report=data_reports.build(g.db, t, key),
                           reports=data_reports.REPORTS)


@bp.get("/tenants/<int:tenant_id>/data/<key>.xlsx")
@login_required
def data_report_xlsx(tenant_id: int, key: str):
    t = tenant_or_404(tenant_id)
    if key not in data_reports.REPORTS:
        abort(404)
    audit(g.db, g.user["username"], "report_downloaded", t["id"], f"{key}.xlsx")
    slug = re.sub(r"[^a-z0-9]+", "-", t["name"].lower()).strip("-") or "tenant"
    return Response(
        data_reports.to_xlsx(data_reports.build(g.db, t, key)),
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename={key}-{slug}.xlsx"},
    )


def _known_skus() -> list[tuple[str, str]]:
    """Every paid SKU part number seen in any tenant's latest licence snapshot."""
    rows = g.db.execute(
        """
        SELECT DISTINCT json_extract(o.data, '$.skuPartNumber') AS part
        FROM objects o JOIN runs r ON r.id = o.run_id
        WHERE o.kind = 'subscribed_sku' AND r.id IN (
            SELECT MAX(id) FROM runs WHERE collector = 'tenant_profile' AND status IN ('succeeded', 'partial')
            GROUP BY tenant_id)
        """
    ).fetchall()
    parts = {r["part"] for r in rows if r["part"]}
    parts |= {r["sku_part_number"] for r in g.db.execute("SELECT sku_part_number FROM sku_prices")}
    return sorted(((p, sku_name(p)) for p in parts), key=lambda x: x[1].lower())


@bp.route("/admin/prices", methods=["GET", "POST"])
@roles_required("admin")
def prices():
    if request.method == "POST":
        currency = request.form.get("currency", "GBP")
        if currency not in CURRENCIES:
            abort(400)
        with transaction(g.db):
            g.db.execute("INSERT INTO settings (key, value) VALUES ('currency', ?)"
                         " ON CONFLICT(key) DO UPDATE SET value = excluded.value", (currency,))
            for part, _ in _known_skus():
                raw = request.form.get(f"price:{part}", "").strip()
                if raw == "":
                    g.db.execute("DELETE FROM sku_prices WHERE sku_part_number = ?", (part,))
                    continue
                try:
                    price = round(float(raw), 2)
                except ValueError:
                    price = -1
                if price < 0 or price > 10000:
                    flash(f"Ignored invalid price for {sku_name(part)}.", "error")
                    continue
                g.db.execute(
                    "INSERT INTO sku_prices (sku_part_number, monthly_price, updated_at) VALUES (?, ?, ?)"
                    " ON CONFLICT(sku_part_number) DO UPDATE SET monthly_price = excluded.monthly_price,"
                    " updated_at = excluded.updated_at",
                    (part, price, utcnow()),
                )
            audit(g.db, g.user["username"], "prices_updated")
        flash("Prices saved.", "success")
        return redirect(url_for("reports.prices"))
    current = {r["sku_part_number"]: r["monthly_price"] for r in g.db.execute("SELECT * FROM sku_prices")}
    return render_template("prices.html", skus=_known_skus(), current=current,
                           currency=setting(g.db, "currency", "GBP"), currencies=CURRENCIES)


# -- Findings ------------------------------------------------------------------


@bp.get("/tenants/<int:tenant_id>/findings")
@login_required
def tenant_findings(tenant_id: int):
    t = tenant_or_404(tenant_id)
    view = request.args.get("view", "open")
    if view == "resolved":
        where = "resolved_at IS NOT NULL"
    elif view == "accepted":
        where = "resolved_at IS NULL AND accepted_at IS NOT NULL"
    else:
        view, where = "open", "resolved_at IS NULL"
    rows = g.db.execute(
        f"SELECT * FROM findings WHERE tenant_id = ? AND {where}"
        " ORDER BY CASE severity WHEN 'critical' THEN 0 WHEN 'high' THEN 1 WHEN 'medium' THEN 2"
        " WHEN 'low' THEN 3 ELSE 4 END, rule_key, subject",
        (t["id"],),
    ).fetchall()
    # Group by check, worst check first, so one check's findings sit together.
    groups: dict[str, list] = {}
    for row in rows:
        groups.setdefault(row["rule_key"], []).append(row)
    return render_template("findings.html", tenant=t, rows=rows, groups=list(groups.items()), view=view,
                           titles=RULE_TITLES)


def _finding_or_404(finding_id: int):
    row = g.db.execute("SELECT * FROM findings WHERE id = ?", (finding_id,)).fetchone()
    if row is None:
        abort(404)
    tenant_or_404(row["tenant_id"])
    return row


@bp.post("/findings/<int:finding_id>/accept")
@roles_required("admin", "analyst")
def accept_finding(finding_id: int):
    row = _finding_or_404(finding_id)
    note = request.form.get("note", "").strip()[:500]
    until = request.form.get("until", "").strip()
    if not note:
        flash("Give a reason for accepting the risk.", "error")
    elif until and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", until):
        abort(400)
    else:
        findings.accept(g.db, row["id"], g.user["username"], note, f"{until}T00:00:00Z" if until else None)
        audit(g.db, g.user["username"], "finding_accepted", row["tenant_id"], f"{row['rule_key']}:{row['subject']}")
        flash("Risk accepted. It moves to the report's accepted risks section.", "success")
    return redirect(url_for("reports.tenant_findings", tenant_id=row["tenant_id"]))


@bp.post("/findings/<int:finding_id>/unaccept")
@roles_required("admin", "analyst")
def unaccept_finding(finding_id: int):
    row = _finding_or_404(finding_id)
    findings.unaccept(g.db, row["id"])
    audit(g.db, g.user["username"], "finding_unaccepted", row["tenant_id"], f"{row['rule_key']}:{row['subject']}")
    return redirect(url_for("reports.tenant_findings", tenant_id=row["tenant_id"], view="accepted"))


# -- Emailed reports -------------------------------------------------------------


@bp.route("/tenants/<int:tenant_id>/schedules", methods=["GET", "POST"])
@login_required
def schedules(tenant_id: int):
    t = tenant_or_404(tenant_id)
    if request.method == "POST":
        if g.user["role"] not in ("admin", "analyst"):
            abort(403)
        recipients = mailer.parse_recipients(request.form.get("recipients", ""))
        day = request.form.get("day_of_month", type=int)
        hour = request.form.get("hour", type=int)
        if recipients is None:
            flash(f"Enter up to {mailer.MAX_RECIPIENTS} valid email addresses.", "error")
        elif day is None or not 1 <= day <= 28 or hour is None or not 0 <= hour <= 23:
            abort(400)
        else:
            g.db.execute(
                "INSERT INTO report_schedules (tenant_id, recipients, day_of_month, hour, created_by, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (t["id"], ", ".join(recipients), day, hour, g.user["username"], utcnow()),
            )
            audit(g.db, g.user["username"], "schedule_created", t["id"], ", ".join(recipients))
            flash("Monthly email scheduled.", "success")
        return redirect(url_for("reports.schedules", tenant_id=t["id"]))
    rows = g.db.execute("SELECT * FROM report_schedules WHERE tenant_id = ? ORDER BY id", (t["id"],)).fetchall()
    deliveries = g.db.execute(
        "SELECT * FROM report_deliveries WHERE tenant_id = ? ORDER BY id DESC LIMIT 20", (t["id"],)).fetchall()
    cfg = current_app.extensions["officesentry.config"]
    return render_template("schedules.html", tenant=t, rows=rows, deliveries=deliveries, timezone=cfg.timezone,
                           mail_ready=bool(mailer.smtp_settings(g.db).get("smtp_host")))


def _schedule_or_404(schedule_id: int):
    row = g.db.execute("SELECT * FROM report_schedules WHERE id = ?", (schedule_id,)).fetchone()
    if row is None:
        abort(404)
    tenant_or_404(row["tenant_id"])
    return row


@bp.post("/schedules/<int:schedule_id>/delete")
@roles_required("admin", "analyst")
def delete_schedule(schedule_id: int):
    row = _schedule_or_404(schedule_id)
    g.db.execute("DELETE FROM report_schedules WHERE id = ?", (row["id"],))
    audit(g.db, g.user["username"], "schedule_deleted", row["tenant_id"], row["recipients"])
    return redirect(url_for("reports.schedules", tenant_id=row["tenant_id"]))


@bp.post("/schedules/<int:schedule_id>/send")
@roles_required("admin", "analyst")
def send_now(schedule_id: int):
    row = _schedule_or_404(schedule_id)
    tenant = tenant_or_404(row["tenant_id"])
    error = mailer.deliver(current_app, g.db, current_app.extensions["officesentry.vault"], tenant,
                           mailer.parse_recipients(row["recipients"]) or [], row["id"])
    audit(g.db, g.user["username"], "report_sent", tenant["id"], row["recipients"])
    flash(f"Sending failed: {error}" if error else f"Report sent to {row['recipients']}.",
          "error" if error else "success")
    return redirect(url_for("reports.schedules", tenant_id=tenant["id"]))


@bp.route("/admin/email", methods=["GET", "POST"])
@roles_required("admin")
def email_settings():
    vault = current_app.extensions["officesentry.vault"]
    current = mailer.smtp_settings(g.db)
    if request.method == "POST":
        if request.form.get("action") == "test":
            to = mailer.parse_recipients(request.form.get("test_to", ""))
            if to is None:
                flash("Enter a valid address to send the test to.", "error")
            else:
                try:
                    mailer.send(g.db, vault, to, "Office Sentry test email",
                                "Email from Office Sentry is working.")
                    flash(f"Test email sent to {', '.join(to)}.", "success")
                except (mailer.MailNotConfigured, OSError, smtplib.SMTPException) as exc:
                    flash(f"Test failed: {exc}", "error")
            return redirect(url_for("reports.email_settings"))
        host = request.form.get("smtp_host", "").strip()
        port = request.form.get("smtp_port", type=int)
        security = request.form.get("smtp_security", "starttls")
        sender = request.form.get("smtp_from", "").strip()
        if security not in ("starttls", "ssl", "none") or port is None or not 1 <= port <= 65535:
            abort(400)
        if not host or not mailer.EMAIL_RE.match(sender.split("<")[-1].rstrip(">").strip()):
            flash("Enter the SMTP host and a valid From address.", "error")
            return redirect(url_for("reports.email_settings"))
        values = {"smtp_host": host, "smtp_port": str(port), "smtp_security": security,
                  "smtp_user": request.form.get("smtp_user", "").strip(), "smtp_from": sender}
        password = request.form.get("smtp_password", "")
        # A changed host must not receive the stored password: require it again.
        if password:
            values["smtp_password_enc"] = vault.encrypt(password)
        elif current.get("smtp_host") and current["smtp_host"] != host:
            values["smtp_password_enc"] = ""
        with transaction(g.db):
            for key, value in values.items():
                g.db.execute("INSERT INTO settings (key, value) VALUES (?, ?)"
                             " ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))
            audit(g.db, g.user["username"], "email_settings_updated", detail=host)
        flash("Email settings saved.", "success")
        return redirect(url_for("reports.email_settings"))
    return render_template("email_settings.html", s=current, has_password=bool(current.get("smtp_password_enc")))
