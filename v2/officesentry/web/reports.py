"""Report pages: per-tenant review (HTML/PDF/XLSX), cross-tenant overview, licence prices."""

from __future__ import annotations

import re

from flask import Blueprint, Response, abort, flash, g, redirect, render_template, request, url_for

from .. import findings, store, usage
from ..db import audit, transaction, utcnow
from ..licences import sku_name
from ..reports import (
    branding,
    data_reports,
    evidence,
    evidence_answers,
    export,
    findings_report,
    library,
    tables,
    views,
)
from ..reports.monthly_review import build, setting
from .access import login_required, roles_required, tenant_or_404, visible_tenants
from .printing import pdf_response
from .reporting import report_extras
from .saved_views import page_url

bp = Blueprint("reports", __name__)

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
STAFF = ("admin", "analyst")  # may override the evidence pack's suggested answers
CURRENCIES = ("GBP", "USD", "EUR", "AUD", "CAD", "NZD")


def _filename(tenant, ext: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", tenant["name"].lower()).strip("-") or "tenant"
    return f"security-review-{slug}.{ext}"


SETTINGS_PAGES = (  # what the library's search finds besides reports and tenants (admins only)
    ("admin.connections", "App connection", "certificate consent entra app registration"),
    ("reports.prices", "Licence prices", "cost money currency sku"),
    ("email.settings", "Email delivery", "smtp mail send graph"),
    ("alerts.settings", "Alerts", "teams slack webhook notifications"),
    ("branding.settings", "Branding", "logo colours msp name report footer support contact favicon"),
    ("admin.users", "Users", "people access roles accounts sign-in"),
)


def _library_href(entry: library.Entry, tenant_id: int | None) -> str:
    if tenant_id is None:  # across every tenant
        if entry.kind == "changes":
            return url_for("across.changes")
        if entry.kind == "findings":
            return url_for("across.checks")
        return url_for("across.rollup", key=entry.key)
    return {
        "review": lambda: url_for("reports.tenant_report", tenant_id=tenant_id),
        "evidence": lambda: url_for("reports.evidence_pack", tenant_id=tenant_id),
        "changes": lambda: url_for("across.tenant_changes", tenant_id=tenant_id),
        "findings": lambda: url_for("reports.tenant_findings", tenant_id=tenant_id),
    }.get(entry.kind, lambda: url_for("reports.data_report", tenant_id=tenant_id, key=entry.key))()


@bp.get("/reports")
@login_required
def overview():
    """The report library: every report, searchable, for one tenant or across all of them."""
    staff = g.user["role"] in ("admin", "analyst")
    tenants = visible_tenants(g.db, g.user)
    choice = request.args.get("tenant", "")
    tenant = next((t for t in tenants if str(t["id"]) == choice), None)
    if tenant is None and not (staff and tenants):
        tenant = tenants[0] if tenants else None  # clients always look at one of their tenants
    use = next((u for u in library.USES if u.key == request.args.get("use")), None)
    areas = library.catalogue()
    for area in areas:
        if use:
            area.entries = [e for e in area.entries if use.key in e.uses]
        for e in area.entries:
            e.href = _library_href(e, tenant["id"] if tenant else None) if tenants else ""
            e.across = _library_href(e, None) if staff and tenant else ""
    saved = g.db.execute("SELECT * FROM saved_views WHERE user_id = ? ORDER BY report_key, name COLLATE NOCASE",
                         (g.user["id"],)).fetchall()
    titles = {k: v[0] for k, v in data_reports.REPORTS.items()} | findings_report.TITLES
    settings = [(url_for(ep), title, words) for ep, title, words in SETTINGS_PAGES] \
        if g.user["role"] == "admin" else []
    return render_template("library.html", areas=[a for a in areas if a.entries], tenants=tenants, tenant=tenant,
                           staff=staff, use=use, uses=library.uses(), total=library.count(), settings=settings,
                           saved=[v for v in saved if v["report_key"] in titles], titles=titles,
                           saved_view_url=page_url)


@bp.get("/tenants/<int:tenant_id>/report")
@login_required
def tenant_report(tenant_id: int):
    t = tenant_or_404(tenant_id)
    report = build(g.db, t)
    return render_template("reports/monthly_review.html", report=report, pdf=False,
                           css_href=url_for("static", filename="report.css"), **report_extras(t, page=True))


@bp.get("/tenants/<int:tenant_id>/report.pdf")
@login_required
def tenant_report_pdf(tenant_id: int):
    t = tenant_or_404(tenant_id)
    audit(g.db, g.user["username"], "report_downloaded", t["id"], "pdf")
    return pdf_response("reports/monthly_review.html", _filename(t, "pdf")[:-4], report=build(g.db, t),
                        **report_extras(t, page=False))


@bp.get("/tenants/<int:tenant_id>/report.xlsx")
@login_required
def tenant_report_xlsx(tenant_id: int):
    t = tenant_or_404(tenant_id)
    audit(g.db, g.user["username"], "report_downloaded", t["id"], "xlsx")
    return Response(
        export.to_xlsx(build(g.db, t), brand=branding.load(g.db)),
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename={_filename(t, 'xlsx')}"},
    )


def _evidence_name(tenant, ext: str) -> str:
    return _filename(tenant, ext).replace("security-review", "security-evidence")


@bp.get("/tenants/<int:tenant_id>/evidence")
@login_required
def evidence_pack(tenant_id: int):
    t = tenant_or_404(tenant_id)
    return render_template("evidence_page.html", pack=evidence.build(g.db, t), answers=evidence_answers.ANSWERS,
                           staff=g.user["role"] in STAFF, max_note=evidence_answers.MAX_NOTE)


@bp.post("/tenants/<int:tenant_id>/evidence/answers/<key>")
@roles_required(*STAFF)
def evidence_answer(tenant_id: int, key: str):
    """The technical lead's answer to one questionnaire question, in place of the suggested one (or back to it)."""
    t = tenant_or_404(tenant_id)
    if key not in evidence_answers.KEYS:
        abort(404)
    back = url_for("reports.evidence_pack", tenant_id=t["id"]) + f"#answer-{key}"
    if request.form.get("action") == "clear":
        if evidence_answers.clear_override(g.db, t["id"], key, g.user["username"]):
            flash("Back to the suggested answer.", "success")
        return redirect(back)
    answer = request.form.get("answer", "")
    if answer not in evidence_answers.ANSWERS:
        abort(400)
    suggested = next(a.suggested for a in evidence.build(g.db, t).answers if a.key == key)
    evidence_answers.set_override(g.db, t["id"], key, answer, request.form.get("note", ""), suggested,
                                  g.user["username"])
    flash("Answer saved. The pack, its PDF and downloads now show it.", "success")
    return redirect(back)


@bp.get("/tenants/<int:tenant_id>/evidence.pdf")
@login_required
def evidence_pdf(tenant_id: int):
    t = tenant_or_404(tenant_id)
    audit(g.db, g.user["username"], "report_downloaded", t["id"], "evidence.pdf")
    return pdf_response("reports/evidence.html", _evidence_name(t, "pdf")[:-4], pack=evidence.build(g.db, t))


@bp.get("/tenants/<int:tenant_id>/evidence.xlsx")
@login_required
def evidence_xlsx(tenant_id: int):
    t = tenant_or_404(tenant_id)
    audit(g.db, g.user["username"], "report_downloaded", t["id"], "evidence.xlsx")
    return Response(
        evidence.to_xlsx(evidence.build(g.db, t), brand=branding.load(g.db)),
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename={_evidence_name(t, 'xlsx')}"},
    )


@bp.get("/tenants/<int:tenant_id>/data")
@login_required
def data_index(tenant_id: int):
    """The Reports tab, and the report picker on phones: ``?key=`` or the first report."""
    t = tenant_or_404(tenant_id)
    key = request.args.get("key", "")
    if key == "review":
        return redirect(url_for("reports.tenant_report", tenant_id=t["id"]))
    if key == "evidence":
        return redirect(url_for("reports.evidence_pack", tenant_id=t["id"]))
    if key not in data_reports.REPORTS:
        key = data_reports.grouped()[0].reports[0][0]
    return redirect(url_for("reports.data_report", tenant_id=t["id"], key=key))


@bp.get("/tenants/<int:tenant_id>/data/<key>")
@login_required
def data_report(tenant_id: int, key: str):
    t = tenant_or_404(tenant_id)
    if key not in data_reports.REPORTS:
        abort(404)
    groups = data_reports.grouped()
    active = next(gr for gr in groups if any(k == key for k, _, _ in gr.reports))
    report = data_reports.build(g.db, t, key)
    view = views.View.parse(request.args)
    saved = g.db.execute("SELECT * FROM saved_views WHERE user_id = ? AND report_key = ? ORDER BY name COLLATE NOCASE",
                         (g.user["id"], key)).fetchall()
    return render_template("data_report.html", tenant=t, report=report, groups=groups, group=active,
                           tables=views.table_views(report, view, request.path), view_query=view.query(),
                           links=views.headline_links(report, request.path), saved=saved)


def _viewed(report):
    """The report with the table view in the query string applied, for a download that matches the page."""
    return views.apply(report, views.View.parse(request.args))


@bp.get("/tenants/<int:tenant_id>/data/<key>.pdf")
@login_required
def data_report_pdf(tenant_id: int, key: str):
    t = tenant_or_404(tenant_id)
    if key not in data_reports.REPORTS:
        abort(404)
    audit(g.db, g.user["username"], "report_downloaded", t["id"], f"{key}.pdf")
    slug = re.sub(r"[^a-z0-9]+", "-", t["name"].lower()).strip("-") or "tenant"
    return pdf_response("reports/detail.html", f"{key}-{slug}", report=_viewed(data_reports.build(g.db, t, key)))


@bp.get("/tenants/<int:tenant_id>/data/<key>.xlsx")
@login_required
def data_report_xlsx(tenant_id: int, key: str):
    t = tenant_or_404(tenant_id)
    if key not in data_reports.REPORTS:
        abort(404)
    audit(g.db, g.user["username"], "report_downloaded", t["id"], f"{key}.xlsx")
    slug = re.sub(r"[^a-z0-9]+", "-", t["name"].lower()).strip("-") or "tenant"
    return Response(
        data_reports.to_xlsx(_viewed(data_reports.build(g.db, t, key)), brand=branding.load(g.db)),
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename={key}-{slug}.xlsx"},
    )


def _known_skus() -> list[tuple[str, str]]:
    """Every paid SKU part number seen in any tenant's latest licence snapshot, plus right-sizing's targets."""
    runs = g.db.execute("SELECT MAX(id) AS id FROM runs WHERE collector = 'tenant_profile'"
                        " AND status IN ('succeeded', 'partial') GROUP BY tenant_id").fetchall()
    parts = {s.get("skuPartNumber") for r in runs for s in store.run_objects(g.db, r["id"], "subscribed_sku")}
    parts = {p for p in parts if p and isinstance(p, str)}
    parts |= {r["sku_part_number"] for r in g.db.execute("SELECT sku_part_number FROM sku_prices")}
    if parts:  # the cheaper plans right-sizing suggests, so its savings can be priced
        parts |= set(usage.TARGETS)
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


def _findings_page(t, resolved: bool):
    report = findings_report.build(g.db, t, resolved=resolved)
    view = views.View.parse(request.args)
    saved = g.db.execute("SELECT * FROM saved_views WHERE user_id = ? AND report_key = ? ORDER BY name COLLATE NOCASE",
                         (g.user["id"], report.key)).fetchall()
    rows = g.db.execute("SELECT * FROM findings WHERE tenant_id = ?", (t["id"],)).fetchall()
    unresolved = [f for f in rows if not f["resolved_at"]]
    current = [f for f in unresolved if not findings_report.is_accepted(f, report.generated_at)]
    lapsed = {f["id"]: f for f in current if f["accepted_at"]}  # past their review date, back among the open ones
    counts = {"open": len(current), "resolved": len(rows) - len(unresolved)}
    return render_template("findings.html", tenant=t, report=report, resolved=resolved, view_query=view.query(),
                           tables=views.table_views(report, view, request.path), saved=saved, lapsed=lapsed,
                           links=views.headline_links(report, request.path),
                           counts=counts)


@bp.get("/tenants/<int:tenant_id>/findings")
@login_required
def tenant_findings(tenant_id: int):
    """Open findings, then accepted risks, on the shared report table."""
    t = tenant_or_404(tenant_id)
    if request.args.get("view") == "resolved":  # the old tab link
        query = views.View.parse(request.args).query()
        return redirect(url_for("reports.resolved_findings", tenant_id=t["id"]) + (f"?{query}" if query else ""))
    return _findings_page(t, resolved=False)


@bp.get("/tenants/<int:tenant_id>/findings/resolved")
@login_required
def resolved_findings(tenant_id: int):
    return _findings_page(tenant_or_404(tenant_id), resolved=True)


@bp.get("/tenants/<int:tenant_id>/findings.<ext>", defaults={"resolved": False})
@bp.get("/tenants/<int:tenant_id>/findings/resolved.<ext>", defaults={"resolved": True})
@login_required
def findings_download(tenant_id: int, ext: str, resolved: bool):
    """The page's tables with its view applied: a PDF, a workbook with a sheet per table, or one table as CSV."""
    t = tenant_or_404(tenant_id)
    if ext not in ("pdf", "xlsx", "csv"):
        abort(404)
    report = views.apply(findings_report.build(g.db, t, resolved=resolved), views.View.parse(request.args))
    name = f"{report.key}-{tables.slug(t['name'])}"
    if ext == "csv":
        index = request.args.get("section", 1, type=int)
        if not 1 <= index <= len(report.sections):
            abort(404)
        section = report.sections[index - 1]
        name = f"{report.key}-{tables.slug(section.title)}-{tables.slug(t['name'])}"
    audit(g.db, g.user["username"], "report_downloaded", t["id"], f"{report.key}.{ext}")
    if ext == "pdf":
        return pdf_response("reports/detail.html", name, report=report)
    if ext == "xlsx":
        body, mimetype = data_reports.to_xlsx(report, brand=branding.load(g.db)), XLSX
    else:
        body, mimetype = tables.to_csv(data_reports.section_table(section)), "text/csv; charset=utf-8"
    return Response(body, mimetype=mimetype, headers={"Content-Disposition": f'attachment; filename="{name}.{ext}"'})


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
        flash("Risk accepted. It's under Accepted risks, here and in the monthly review.", "success")
        return redirect(url_for("reports.tenant_findings", tenant_id=row["tenant_id"]) + "#t-accepted-risks")
    return redirect(url_for("reports.tenant_findings", tenant_id=row["tenant_id"]))


@bp.post("/findings/<int:finding_id>/unaccept")
@roles_required("admin", "analyst")
def unaccept_finding(finding_id: int):
    row = _finding_or_404(finding_id)
    findings.unaccept(g.db, row["id"])
    audit(g.db, g.user["username"], "finding_unaccepted", row["tenant_id"], f"{row['rule_key']}:{row['subject']}")
    return redirect(url_for("reports.tenant_findings", tenant_id=row["tenant_id"]))
