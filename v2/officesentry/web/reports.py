"""Report pages: per-tenant review (HTML/PDF/XLSX), cross-tenant overview, licence prices."""

from __future__ import annotations

import re
from pathlib import Path

from flask import Blueprint, Response, abort, flash, g, redirect, render_template, request, url_for

from ..db import audit, transaction, utcnow
from ..licences import sku_name
from ..reports import export
from ..reports.monthly_review import build, rollup_row, setting
from .access import login_required, roles_required, tenant_or_404, visible_tenants

bp = Blueprint("reports", __name__)

STATIC_DIR = Path(__file__).parent / "static"
CURRENCIES = ("GBP", "USD", "EUR", "AUD", "CAD", "NZD")


def _filename(tenant, ext: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", tenant["name"].lower()).strip("-") or "tenant"
    return f"security-review-{slug}.{ext}"


@bp.get("/reports")
@login_required
def overview():
    rows = [rollup_row(g.db, t) for t in visible_tenants(g.db, g.user)]
    return render_template("reports_overview.html", rows=rows, currency=setting(g.db, "currency", "GBP"))


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
