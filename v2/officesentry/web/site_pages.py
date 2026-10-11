"""One page per SharePoint site (``reports/site_page.py``) and the Sites page that lists them."""

from __future__ import annotations

import re

from flask import Blueprint, Response, abort, g, render_template

from ..db import audit
from ..reports import branding, data_reports, site_page
from .access import login_required, tenant_or_404
from .printing import pdf_response

bp = Blueprint("site_pages", __name__)


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-") or "site"


def _page(tenant_id: int, site_id: str):
    t = tenant_or_404(tenant_id)
    page = site_page.build(g.db, t, site_id)
    if page is None:
        abort(404)
    return t, page


@bp.get("/tenants/<int:tenant_id>/sites")
@login_required
def sites(tenant_id: int):
    t = tenant_or_404(tenant_id)
    found = site_page.directory(g.db, t)
    rows, tiles, notice = found if found else ([], [], "")
    return render_template(
        "sites_list.html", tenant=t, rows=rows, tiles=tiles, notice=notice, collected=found is not None
    )


@bp.get("/tenants/<int:tenant_id>/sites/<site_id>")
@login_required
def site(tenant_id: int, site_id: str):
    t, page = _page(tenant_id, site_id)
    return render_template("object_page.html", tenant=t, page=page, tab="site_pages")


@bp.get("/tenants/<int:tenant_id>/sites/<site_id>.pdf")
@login_required
def site_pdf(tenant_id: int, site_id: str):
    t, page = _page(tenant_id, site_id)
    audit(g.db, g.user["username"], "report_downloaded", t["id"], "site.pdf")
    return pdf_response("reports/detail.html", f"site-{_slug(page.name)}", report=page.as_report())


@bp.get("/tenants/<int:tenant_id>/sites/<site_id>.xlsx")
@login_required
def site_xlsx(tenant_id: int, site_id: str):
    t, page = _page(tenant_id, site_id)
    audit(g.db, g.user["username"], "report_downloaded", t["id"], "site.xlsx")
    return Response(
        data_reports.to_xlsx(page.as_report(), brand=branding.load(g.db)),
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename=site-{_slug(page.name)}.xlsx"},
    )
