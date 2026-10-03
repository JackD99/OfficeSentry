"""Report pages: per-tenant review (HTML/PDF/XLSX), cross-tenant overview, licence prices."""

from __future__ import annotations

import re

from flask import Blueprint, Response, abort, flash, g, redirect, render_template, request, url_for

from .. import findings
from ..db import audit, transaction, utcnow
from ..licences import sku_name
from ..reports import data_reports, evidence, export
from ..reports.monthly_review import build, setting
from ..rules import RULES
from .access import login_required, roles_required, tenant_or_404
from .printing import pdf_response

RULE_TITLES = {r.key: r.title for r in RULES}

bp = Blueprint("reports", __name__)

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
                           css_href=url_for("static", filename="report.css"))


@bp.get("/tenants/<int:tenant_id>/report.pdf")
@login_required
def tenant_report_pdf(tenant_id: int):
    t = tenant_or_404(tenant_id)
    audit(g.db, g.user["username"], "report_downloaded", t["id"], "pdf")
    return pdf_response("reports/monthly_review.html", _filename(t, "pdf")[:-4], report=build(g.db, t))


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
    return render_template("evidence_page.html", pack=evidence.build(g.db, t))


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
        evidence.to_xlsx(evidence.build(g.db, t)),
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename={_evidence_name(t, 'xlsx')}"},
    )


@bp.get("/tenants/<int:tenant_id>/data")
@login_required
def data_index(tenant_id: int):
    """The Detail reports tab, and the report picker on phones: ``?key=`` or the first report."""
    t = tenant_or_404(tenant_id)
    key = request.args.get("key", "")
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
    return render_template("data_report.html", tenant=t, report=data_reports.build(g.db, t, key),
                           groups=groups, group=active)


@bp.get("/tenants/<int:tenant_id>/data/<key>.pdf")
@login_required
def data_report_pdf(tenant_id: int, key: str):
    t = tenant_or_404(tenant_id)
    if key not in data_reports.REPORTS:
        abort(404)
    audit(g.db, g.user["username"], "report_downloaded", t["id"], f"{key}.pdf")
    slug = re.sub(r"[^a-z0-9]+", "-", t["name"].lower()).strip("-") or "tenant"
    return pdf_response("reports/detail.html", f"{key}-{slug}", report=data_reports.build(g.db, t, key))


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
