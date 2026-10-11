"""One page per mailbox (``reports/mailbox_page.py``) and the Mailboxes page that lists them."""

from __future__ import annotations

import re

from flask import Blueprint, Response, abort, g, render_template

from ..db import audit
from ..reports import branding, data_reports, mailbox_page
from .access import login_required, tenant_or_404
from .printing import pdf_response

bp = Blueprint("mailboxes", __name__)


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-") or "mailbox"


def _page(tenant_id: int, mailbox_id: str):
    t = tenant_or_404(tenant_id)
    page = mailbox_page.build(g.db, t, mailbox_id)
    if page is None:
        abort(404)
    return t, page


@bp.get("/tenants/<int:tenant_id>/mailboxes")
@login_required
def mailboxes(tenant_id: int):
    t = tenant_or_404(tenant_id)
    found = mailbox_page.directory(g.db, t)
    rows, tiles = found if found else ([], [])
    return render_template("mailboxes.html", tenant=t, rows=rows, tiles=tiles, collected=found is not None)


@bp.get("/tenants/<int:tenant_id>/mailboxes/<mailbox_id>")
@login_required
def mailbox(tenant_id: int, mailbox_id: str):
    t, page = _page(tenant_id, mailbox_id)
    return render_template("object_page.html", tenant=t, page=page, tab="mailboxes")


@bp.get("/tenants/<int:tenant_id>/mailboxes/<mailbox_id>.pdf")
@login_required
def mailbox_pdf(tenant_id: int, mailbox_id: str):
    t, page = _page(tenant_id, mailbox_id)
    audit(g.db, g.user["username"], "report_downloaded", t["id"], "mailbox.pdf")
    return pdf_response("reports/detail.html", f"mailbox-{_slug(page.subtitle or page.name)}", report=page.as_report())


@bp.get("/tenants/<int:tenant_id>/mailboxes/<mailbox_id>.xlsx")
@login_required
def mailbox_xlsx(tenant_id: int, mailbox_id: str):
    t, page = _page(tenant_id, mailbox_id)
    audit(g.db, g.user["username"], "report_downloaded", t["id"], "mailbox.xlsx")
    return Response(
        data_reports.to_xlsx(page.as_report(), brand=branding.load(g.db)),
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename=mailbox-{_slug(page.subtitle or page.name)}.xlsx"},
    )
