"""Cross-tenant rollups, the month-over-month change report, and CSV downloads.

Every download here is built from the same data as its page, through
``reports.tables``, so CSV and XLSX always match what people see. PDFs render
that same data through ``templates/reports/`` (see ``printing.pdf_response``).
"""

from __future__ import annotations

from datetime import UTC, datetime

from flask import Blueprint, Response, abort, g, redirect, render_template, request, url_for

from ..db import audit
from ..reports import data_reports, evidence, month_changes, rollups, tables, views
from ..reports.monthly_review import build as build_review
from ..rules import RULES
from .access import login_required, roles_required, tenant_or_404, visible_tenants
from .printing import pdf_response

bp = Blueprint("across", __name__)

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
RULE_TITLES = {r.key: r.title for r in RULES}


def _download(body: bytes, filename: str, ext: str) -> Response:
    mimetype = XLSX if ext == "xlsx" else "text/csv; charset=utf-8"
    return Response(body, mimetype=mimetype,
                    headers={"Content-Disposition": f'attachment; filename="{filename}.{ext}"'})


def _days() -> int:
    days = request.args.get("days", type=int)
    return days if days in month_changes.PERIODS else month_changes.DEFAULT_DAYS


# -- Cross-tenant rollups ------------------------------------------------------------


@bp.get("/across")
@roles_required("admin", "analyst")
def index():
    key = request.args.get("key", "")
    return redirect(url_for("across.rollup", key=key if key in dict(rollups.available()) else "review"))


def _rollup_or_404(key: str):
    if key not in dict(rollups.available()):
        abort(404)
    return rollups.build(g.db, visible_tenants(g.db, g.user), key)


@bp.get("/across/<key>")
@roles_required("admin", "analyst")
def rollup(key: str):
    data = _rollup_or_404(key)
    return render_template("rollup.html", rollup=data, main=rollups.available()[:2], groups=data_reports.grouped())


@bp.get("/across/<key>.csv", defaults={"ext": "csv"})
@bp.get("/across/<key>.xlsx", defaults={"ext": "xlsx"})
@roles_required("admin", "analyst")
def rollup_download(key: str, ext: str):
    data = _rollup_or_404(key)
    table = data.to_table()
    audit(g.db, g.user["username"], "rollup_downloaded", None, f"{key}.{ext}")
    body = tables.to_csv(table) if ext == "csv" else tables.to_xlsx([table])
    return _download(body, f"all-tenants-{tables.slug(data.title)}", ext)


@bp.get("/across/<key>.pdf")
@roles_required("admin", "analyst")
def rollup_pdf(key: str):
    data = _rollup_or_404(key)
    audit(g.db, g.user["username"], "rollup_downloaded", None, f"{key}.pdf")
    return pdf_response("reports/rollup.html", f"all-tenants-{tables.slug(data.title)}", rollup=data)


# -- Month-over-month changes ----------------------------------------------------------


def _across_changes(days: int):
    summaries = month_changes.across(g.db, visible_tenants(g.db, g.user), days)
    scored = [s.figures["secure_score.pct"].delta for s in summaries
              if "secure_score.pct" in s.figures and s.figures["secure_score.pct"].delta is not None]
    totals = {
        "changed": sum(1 for s in summaries if s.changes or s.new_findings or s.fixed),
        "new_severe": sum(s.new_severe for s in summaries),
        "new": sum(s.new_findings for s in summaries),
        "fixed": sum(s.fixed for s in summaries),
        "score": round(sum(scored) / len(scored), 1) if scored else None,
        "no_baseline": sum(1 for s in summaries if not s.has_baseline),
    }
    return summaries, totals


@bp.get("/changes")
@roles_required("admin", "analyst")
def changes():
    days = _days()
    summaries, totals = _across_changes(days)
    return render_template("changes.html", summaries=summaries, totals=totals, days=days,
                           periods=month_changes.PERIODS)


@bp.get("/changes.pdf")
@roles_required("admin", "analyst")
def changes_pdf():
    days = _days()
    summaries, totals = _across_changes(days)
    audit(g.db, g.user["username"], "rollup_downloaded", None, f"changes-{days}d.pdf")
    return pdf_response("reports/changes.html", f"all-tenants-changes-{days}-days", summaries=summaries,
                        totals=totals, days=days, generated_at=datetime.now(UTC))


@bp.get("/changes.csv", defaults={"ext": "csv"})
@bp.get("/changes.xlsx", defaults={"ext": "xlsx"})
@roles_required("admin", "analyst")
def changes_download(ext: str):
    days = _days()
    table = month_changes.across_table(month_changes.across(g.db, visible_tenants(g.db, g.user), days), days)
    audit(g.db, g.user["username"], "rollup_downloaded", None, f"changes-{days}d.{ext}")
    body = tables.to_csv(table) if ext == "csv" else tables.to_xlsx([table])
    return _download(body, f"all-tenants-changes-{days}-days", ext)


def _tenant_changes(t, days: int) -> dict:
    report = month_changes.build(g.db, t, days)
    unchanged = [r for r in report.reports if not r.count and not r.note]
    return {"report": report, "days": days, "unchanged": unchanged, "titles": RULE_TITLES}


@bp.get("/tenants/<int:tenant_id>/changes")
@login_required
def tenant_changes(tenant_id: int):
    t = tenant_or_404(tenant_id)
    return render_template("tenant_changes.html", tenant=t, periods=month_changes.PERIODS,
                           **_tenant_changes(t, _days()))


@bp.get("/tenants/<int:tenant_id>/changes.pdf")
@login_required
def tenant_changes_pdf(tenant_id: int):
    t = tenant_or_404(tenant_id)
    days = _days()
    audit(g.db, g.user["username"], "report_downloaded", t["id"], f"changes-{days}d.pdf")
    return pdf_response("reports/tenant_changes.html", f"changes-{days}-days-{tables.slug(t['name'])}",
                        **_tenant_changes(t, days))


@bp.get("/tenants/<int:tenant_id>/changes.csv", defaults={"ext": "csv"})
@bp.get("/tenants/<int:tenant_id>/changes.xlsx", defaults={"ext": "xlsx"})
@login_required
def tenant_changes_download(tenant_id: int, ext: str):
    t = tenant_or_404(tenant_id)
    days = _days()
    report = month_changes.build(g.db, t, days)
    audit(g.db, g.user["username"], "report_downloaded", t["id"], f"changes-{days}d.{ext}")
    if ext == "csv":
        body = tables.to_csv(month_changes.all_changes_table(report))
    else:
        body = tables.to_xlsx(month_changes.to_tables(report))
    return _download(body, f"changes-{days}-days-{tables.slug(t['name'])}", ext)


# -- CSV for per-tenant reports -------------------------------------------------------


@bp.get("/tenants/<int:tenant_id>/data/<key>.csv")
@login_required
def data_report_csv(tenant_id: int, key: str):
    t = tenant_or_404(tenant_id)
    if key not in data_reports.REPORTS:
        abort(404)
    report = views.apply(data_reports.build(g.db, t, key), views.View.parse(request.args))
    report_tables = data_reports.to_tables(report)
    index = request.args.get("section", 1, type=int)
    if not 1 <= index <= len(report_tables):
        abort(404)
    table = report_tables[index - 1]
    audit(g.db, g.user["username"], "report_downloaded", t["id"], f"{key}.csv")
    return _download(tables.to_csv(table), f"{key}-{tables.slug(table.title)}-{tables.slug(t['name'])}", "csv")


@bp.get("/tenants/<int:tenant_id>/report.csv")
@login_required
def review_csv(tenant_id: int):
    """The monthly review's findings, one row per affected account or object."""
    t = tenant_or_404(tenant_id)
    report = build_review(g.db, t)
    table = tables.Table("Findings", ["Severity", "Category", "Check", "Subject", "Detail", "New", "Recommendation"], [
        [f.severity, r.category, r.title, f.subject, f.detail, getattr(f, "new", False), r.recommendation]
        for r in report.failing for f in r.findings])
    audit(g.db, g.user["username"], "report_downloaded", t["id"], "findings.csv")
    return _download(tables.to_csv(table), f"security-review-findings-{tables.slug(t['name'])}", "csv")


@bp.get("/tenants/<int:tenant_id>/evidence.csv")
@login_required
def evidence_csv(tenant_id: int):
    t = tenant_or_404(tenant_id)
    pack = evidence.build(g.db, t)
    table = tables.Table("Controls", ["Question", "Status", "Evidence", "Gaps"], [
        [c.control.question, c.status, " ".join(c.evidence), " ".join(x.strip() for x in c.gaps)]
        for c in pack.controls])
    audit(g.db, g.user["username"], "report_downloaded", t["id"], "evidence.csv")
    return _download(tables.to_csv(table), f"security-evidence-{tables.slug(t['name'])}", "csv")
