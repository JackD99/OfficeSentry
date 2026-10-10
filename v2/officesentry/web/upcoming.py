"""Coming up: what falls due in the next 90 days across the tenants a person can see (``upcoming.py``).

Staff only. Analysts see their granted tenants; Office Sentry's own certificates are for admins. The table is
the shared report table, so filters, sort and the PDF, XLSX and CSV downloads keep the same view.
"""

from __future__ import annotations

from flask import Blueprint, Response, g, render_template, request

from .. import upcoming
from ..db import audit
from ..reports import branding, data_reports, tables, views
from ..reports.data_reports import Rows
from .access import roles_required, visible_tenants
from .printing import pdf_response

bp = Blueprint("upcoming", __name__)

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _collect() -> upcoming.Upcoming:
    return upcoming.collect(g.db, visible_tenants(g.db, g.user), ours=g.user["role"] == "admin")


@bp.get("/upcoming")
@roles_required("admin", "analyst")
def index():
    up = _collect()
    report = upcoming.report(up)
    view = views.View.parse(request.args)
    return render_template(
        "upcoming.html",
        up=up,
        report=report,
        view_query=view.query(),
        tables=views.table_views(report, view, request.path),
        links=views.headline_links(report, request.path),
        counts=up.counts(),
        w=upcoming,
        types=[
            (t, n, views.rows_link(report, Rows(upcoming.TABLE, {"Type": t}), request.path)) for t, n in up.by_type()
        ],
    )


@bp.get("/upcoming.pdf", defaults={"ext": "pdf"})
@bp.get("/upcoming.xlsx", defaults={"ext": "xlsx"})
@bp.get("/upcoming.csv", defaults={"ext": "csv"})
@roles_required("admin", "analyst")
def download(ext: str):
    report = views.apply(upcoming.report(_collect()), views.View.parse(request.args))
    audit(g.db, g.user["username"], "report_downloaded", None, f"{upcoming.KEY}.{ext}")
    if ext == "pdf":
        return pdf_response("reports/detail.html", "coming-up", report=report)
    if ext == "xlsx":
        body, mimetype = data_reports.to_xlsx(report, brand=branding.load(g.db)), XLSX
    else:
        body, mimetype = tables.to_csv(data_reports.section_table(report.sections[0])), "text/csv; charset=utf-8"
    return Response(body, mimetype=mimetype, headers={"Content-Disposition": f'attachment; filename="coming-up.{ext}"'})
