"""One page per group or team (``reports/group_page.py``) and the Groups and teams page that lists them."""

from __future__ import annotations

import re

from flask import Blueprint, Response, abort, g, render_template

from ..db import audit
from ..reports import branding, data_reports, group_page
from .access import needs, tenant_or_404
from .printing import pdf_response

bp = Blueprint("group_pages", __name__)


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-") or "group"


def _page(tenant_id: int, gid: str):
    t = tenant_or_404(tenant_id)
    page = group_page.build(g.db, t, gid)
    if page is None:
        abort(404)
    return t, page


@bp.get("/tenants/<int:tenant_id>/groups")
@needs("reports.people")
def groups(tenant_id: int):
    t = tenant_or_404(tenant_id)
    found = group_page.directory(g.db, t)
    rows, tiles = found if found else ([], [])
    return render_template("groups_list.html", tenant=t, rows=rows, tiles=tiles, collected=found is not None)


@bp.get("/tenants/<int:tenant_id>/groups/<gid>")
@needs("reports.people")
def group(tenant_id: int, gid: str):
    t, page = _page(tenant_id, gid)
    return render_template("object_page.html", tenant=t, page=page, tab="group_pages")


@bp.get("/tenants/<int:tenant_id>/groups/<gid>.pdf")
@needs("reports.people", "export")
def group_pdf(tenant_id: int, gid: str):
    t, page = _page(tenant_id, gid)
    audit(g.db, g.user["username"], "report_downloaded", t["id"], "group.pdf")
    return pdf_response("reports/detail.html", f"group-{_slug(page.name)}", report=page.as_report())


@bp.get("/tenants/<int:tenant_id>/groups/<gid>.xlsx")
@needs("reports.people", "export")
def group_xlsx(tenant_id: int, gid: str):
    t, page = _page(tenant_id, gid)
    audit(g.db, g.user["username"], "report_downloaded", t["id"], "group.xlsx")
    return Response(
        data_reports.to_xlsx(page.as_report(), brand=branding.load(g.db)),
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename=group-{_slug(page.name)}.xlsx"},
    )
