"""Cross-tenant rollups, the month-over-month change report, and CSV downloads.

Every download here is built from the same data as its page, through
``reports.tables``, so CSV and XLSX always match what people see. PDFs render
that same data through ``templates/reports/`` (see ``printing.pdf_response``).
"""

from __future__ import annotations

from datetime import UTC, datetime

from flask import Blueprint, Response, abort, flash, g, redirect, render_template, request, url_for

from ..db import audit, transaction, utcnow
from ..reports import (
    branding,
    data_reports,
    evidence,
    month_changes,
    questionnaires,
    rollups,
    tables,
    views,
    workbook,
)
from ..reports.monthly_review import build as build_review
from ..rules import RULES
from .access import login_required, roles_required, tenant_or_404
from .groups import group_aware, scope, scoped_tenants
from .printing import pdf_response
from .saved_views import MAX_VIEWS

bp = Blueprint("across", __name__)

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
RULE_TITLES = {r.key: r.title for r in RULES}


def _download(body: bytes, filename: str, ext: str) -> Response:
    mimetype = XLSX if ext == "xlsx" else "text/csv; charset=utf-8"
    return Response(body, mimetype=mimetype,
                    headers={"Content-Disposition": f'attachment; filename="{filename}.{ext}"'})


def _cover(title: str, client: str, intro: str = "", tenant_ref: str = "", facts=()) -> workbook.Cover:
    """The summary sheet of an XLSX download: what it is, for whom, by whom and when."""
    brand = branding.load(g.db)
    return workbook.Cover(title, client, datetime.now(UTC), brand.display_name, tenant_ref, brand.accent, intro,
                          list(facts))


def _days() -> int:
    days = request.args.get("days", type=int)
    return days if days in month_changes.PERIODS else month_changes.DEFAULT_DAYS


# -- Cross-tenant rollups ------------------------------------------------------------


@bp.get("/across")
@roles_required("admin", "analyst")
def index():
    key = request.args.get("key", "")
    if request.args.get("mode") == "rows" and key in data_reports.REPORTS:
        return redirect(url_for("across.rows_index", key=key))
    return redirect(url_for("across.rollup", key=key if key in dict(rollups.available()) else "review"))


def _rollup_or_404(key: str):
    if key not in dict(rollups.available()):
        abort(404)
    return rollups.build(g.db, scoped_tenants(), key)


def _picker() -> dict:
    """What the All tenants pages share: the summary tabs and the detail report picker."""
    return {"main": rollups.available()[:2], "groups": data_reports.grouped()}


@bp.get("/across/<key>")
@roles_required("admin", "analyst")
@group_aware
def rollup(key: str):
    data = _rollup_or_404(key)
    return render_template("rollup.html", rollup=data, key=key, **_picker())


# -- Every row of a detail report, across tenants ---------------------------------------


def _all_rows(key: str) -> rollups.AllRows:
    if key not in data_reports.REPORTS:
        abort(404)
    return rollups.all_rows(g.db, scoped_tenants(), key,
                            href=lambda t: url_for("reports.data_report", tenant_id=t["id"], key=key),
                            label=scope().label)


def _table_or_404(data: rollups.AllRows, table: str) -> int:
    slugs = views.table_slugs(data.report)
    if table not in slugs:
        abort(404)
    return slugs.index(table)


@bp.get("/across/<key>/rows")
@roles_required("admin", "analyst")
@group_aware
def rows_index(key: str):
    """Every row: ``?table=`` (a table slug) or the first table with rows. Keeps the table view."""
    data = _all_rows(key)
    slugs = views.table_slugs(data.report)
    table = request.args.get("table", "")
    if table not in slugs:
        table = next((s for s, sec in zip(slugs, data.report.sections, strict=True) if sec.rows),
                     slugs[0] if slugs else "")
    if not table:
        return render_template("rollup_rows.html", data=data, key=key, tv=None, table="", tables=[], saved=[],
                               view_query="", **_picker())
    query = views.View.parse(request.args).query()
    return redirect(url_for("across.rows", key=key, table=table) + (f"?{query}" if query else ""))


@bp.get("/across/<key>/rows/<table>")
@roles_required("admin", "analyst")
@group_aware
def rows(key: str, table: str):
    data = _all_rows(key)
    index = _table_or_404(data, table)
    view = views.View.parse(request.args)
    saved = g.db.execute("SELECT * FROM saved_views WHERE user_id = ? AND report_key = ? ORDER BY name COLLATE NOCASE",
                         (g.user["id"], key)).fetchall()
    return render_template(
        "rollup_rows.html", data=data, key=key, table=table,
        tv=views.table_view(data.report, index, view, request.path),
        tables=list(zip(views.table_slugs(data.report), data.report.sections, strict=True)),
        truncated=data.truncated(data.report.sections[index].title), max_rows=rollups.MAX_ROWS,
        view_query=view.query(), saved=saved, **_picker())


def _rows_report(key: str, table: str):
    data = _all_rows(key)
    title = data.report.sections[_table_or_404(data, table)].title
    return rollups.one_table(data, title, views.View.parse(request.args))


@bp.get("/across/<key>/rows/<table>.csv", defaults={"ext": "csv"})
@bp.get("/across/<key>/rows/<table>.xlsx", defaults={"ext": "xlsx"})
@roles_required("admin", "analyst")
@group_aware
def rows_download(key: str, table: str, ext: str):
    report = _rows_report(key, table)
    audit(g.db, g.user["username"], "rollup_downloaded", None, f"{key}-rows-{table}.{ext}")
    filename = f"all-tenants-{key}-{table}"
    if ext == "csv":
        return _download(tables.to_csv(data_reports.section_table(report.sections[0])), filename, ext)
    return _download(data_reports.to_xlsx(report, brand=branding.load(g.db)), filename, ext)


@bp.get("/across/<key>/rows/<table>.pdf")
@roles_required("admin", "analyst")
@group_aware
def rows_pdf(key: str, table: str):
    report = _rows_report(key, table)
    audit(g.db, g.user["username"], "rollup_downloaded", None, f"{key}-rows-{table}.pdf")
    return pdf_response("reports/detail.html", f"all-tenants-{key}-{table}", report=report)


@bp.post("/across/<key>/rows/<table>/views")
@roles_required("admin", "analyst")
def save_rows_view(key: str, table: str):
    """Save a view from the every-row page. Views are per report, so it also opens on each tenant's page."""
    if key not in data_reports.REPORTS:
        abort(404)
    page = url_for("across.rows", key=key, table=table)
    query = views.View.parse(request.form.get("query", "")).query()
    name = " ".join(request.form.get("name", "").split())[:80]
    if not query or not name:
        flash("Filter, sort or hide a column first, then give the view a name.", "error")
        return redirect(page)
    count = g.db.execute("SELECT COUNT(*) FROM saved_views WHERE user_id = ?", (g.user["id"],)).fetchone()[0]
    if count >= MAX_VIEWS:
        flash(f"You can keep up to {MAX_VIEWS} views. Delete one you don't use first.", "error")
        return redirect(f"{page}?{query}")
    with transaction(g.db):
        g.db.execute(
            "INSERT INTO saved_views (user_id, report_key, name, query, created_at) VALUES (?, ?, ?, ?, ?)"
            " ON CONFLICT(user_id, report_key, name) DO UPDATE SET query = excluded.query",
            (g.user["id"], key, name, query, utcnow()))
    flash(f"Saved “{name}”. It's under Views on this report, here and for every tenant.", "success")
    return redirect(f"{page}?{query}")


# -- Every check, for every tenant ---------------------------------------------------------


def _check_matrix():
    return rollups.check_matrix(g.db, scoped_tenants())


@bp.get("/across/checks")
@roles_required("admin", "analyst")
@group_aware
def checks():
    matrix = _check_matrix()
    categories = matrix.categories
    category = request.args.get("category", "")
    chosen = next((c for c in categories if views.slug(c[0]) == category), None)
    return render_template("check_matrix.html", matrix=matrix, categories=categories, chosen=chosen,
                           slugs={c[0]: views.slug(c[0]) for c in categories}, **_picker())


@bp.get("/across/checks.csv", defaults={"ext": "csv"})
@bp.get("/across/checks.xlsx", defaults={"ext": "xlsx"})
@roles_required("admin", "analyst")
@group_aware
def checks_download(ext: str):
    matrix = _check_matrix()
    table = matrix.to_table()
    audit(g.db, g.user["username"], "rollup_downloaded", None, f"checks.{ext}")
    body = tables.to_csv(table) if ext == "csv" else tables.to_xlsx([table], cover=_cover(
        "Checks", scope().label, "Every check's result for every tenant.",
        facts=[("Tenants", len(matrix.rows), ""), ("With failing checks", matrix.with_failures, "")]))
    return _download(body, "all-tenants-checks", ext)


@bp.get("/across/<key>.csv", defaults={"ext": "csv"})
@bp.get("/across/<key>.xlsx", defaults={"ext": "xlsx"})
@roles_required("admin", "analyst")
@group_aware
def rollup_download(key: str, ext: str):
    data = _rollup_or_404(key)
    table = data.to_table()
    audit(g.db, g.user["username"], "rollup_downloaded", None, f"{key}.{ext}")
    body = tables.to_csv(table) if ext == "csv" else tables.to_xlsx([table], cover=_cover(
        data.title, scope().label, data.description,
        facts=[("Tenants", len(data.rows), ""), ("Need attention", data.with_attention, ""),
               ("No data for this report", data.without_data, "")]))
    return _download(body, f"all-tenants-{tables.slug(data.title)}", ext)


@bp.get("/across/<key>.pdf")
@roles_required("admin", "analyst")
@group_aware
def rollup_pdf(key: str):
    data = _rollup_or_404(key)
    audit(g.db, g.user["username"], "rollup_downloaded", None, f"{key}.pdf")
    return pdf_response("reports/rollup.html", f"all-tenants-{tables.slug(data.title)}", rollup=data)


# -- Month-over-month changes ----------------------------------------------------------


def _across_changes(days: int):
    summaries = month_changes.across(g.db, scoped_tenants(), days)
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
@group_aware
def changes():
    days = _days()
    summaries, totals = _across_changes(days)
    return render_template("changes.html", summaries=summaries, totals=totals, days=days,
                           periods=month_changes.PERIODS)


@bp.get("/changes.pdf")
@roles_required("admin", "analyst")
@group_aware
def changes_pdf():
    days = _days()
    summaries, totals = _across_changes(days)
    audit(g.db, g.user["username"], "rollup_downloaded", None, f"changes-{days}d.pdf")
    return pdf_response("reports/changes.html", f"all-tenants-changes-{days}-days", summaries=summaries,
                        totals=totals, days=days, generated_at=datetime.now(UTC))


@bp.get("/changes.csv", defaults={"ext": "csv"})
@bp.get("/changes.xlsx", defaults={"ext": "xlsx"})
@roles_required("admin", "analyst")
@group_aware
def changes_download(ext: str):
    days = _days()
    table = month_changes.across_table(month_changes.across(g.db, scoped_tenants(), days), days)
    audit(g.db, g.user["username"], "rollup_downloaded", None, f"changes-{days}d.{ext}")
    body = tables.to_csv(table) if ext == "csv" else tables.to_xlsx([table], cover=_cover(
        f"What changed in the last {days} days", scope().label,
        f"Changes in every tenant over the last {days} days, most important first."))
    return _download(body, f"all-tenants-changes-{days}-days", ext)


def _tenant_changes(t, days: int) -> dict:
    report = month_changes.build(g.db, t, days)
    unchanged = [r for r in report.reports if not r.count and not r.note]
    return {"report": report, "days": days, "unchanged": unchanged, "titles": RULE_TITLES,
            "areas": _change_areas(report)}


def _change_areas(report) -> list[tuple[str, list]]:
    """The reports with changes, under their menu group (Identity and access, Email, ...), in menu order."""
    order = {key: (i, group.title) for i, group in enumerate(data_reports.grouped()) for key, _, _ in group.reports}
    areas: dict[str, list] = {}
    for r in sorted((r for r in report.reports if r.count), key=lambda r: order.get(r.key, (len(order), ""))[0]):
        areas.setdefault(order.get(r.key, (0, "More reports"))[1], []).append(r)
    return list(areas.items())


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
        body = tables.to_xlsx(month_changes.to_tables(report), cover=_cover(
            f"What changed in the last {days} days", t["name"], tenant_ref=t["tenant_ref"],
            facts=[("Changes", report.total, ""), ("New findings", len(report.new_findings), ""),
                   ("Fixed findings", len(report.fixed_findings), "")]))
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
    """With ``set=``, that question set's answers; without, every topic's answer and the controls."""
    t = tenant_or_404(tenant_id)
    pack = evidence.build(g.db, t)
    key = request.args.get("set")
    if key in questionnaires.SETS:
        table = tables.Table(questionnaires.SETS[key].title, questionnaires.COLUMNS,
                             questionnaires.rows(questionnaires.sheet(pack, key)))
        audit(g.db, g.user["username"], "report_downloaded", t["id"], f"evidence-{key}.csv")
        return _download(tables.to_csv(table), f"security-evidence-{tables.slug(t['name'])}-{key}", "csv")
    table = tables.Table("Evidence", ["Section", "Question", "Answer", "Why", "Answered by", "Evidence", "Gaps"], [
        ["Questionnaire", question, answer, why, by, source, ""]
        for question, answer, why, by, source in evidence.answer_rows(pack)] + [
        ["Controls", c.control.question, c.status, "", "", " ".join(c.evidence), " ".join(x.strip() for x in c.gaps)]
        for c in pack.controls])
    audit(g.db, g.user["username"], "report_downloaded", t["id"], "evidence.csv")
    return _download(tables.to_csv(table), f"security-evidence-{tables.slug(t['name'])}", "csv")
