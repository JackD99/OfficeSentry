"""A tenant's Activity history page (``reports/activity_history.py``) and its month downloads. Staff only."""

from __future__ import annotations

import csv
import re
from io import StringIO

from flask import Blueprint, Response, abort, current_app, g, render_template

from .. import retention
from ..db import audit, connect
from ..reports import activity_history, tables
from .access import roles_required, tenant_or_404

bp = Blueprint("history", __name__)

MONTH = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


@bp.get("/tenants/<int:tenant_id>/activity")
@roles_required("admin", "analyst")
def activity(tenant_id: int):
    t = tenant_or_404(tenant_id)
    policy = retention.load(g.db, current_app.extensions["officesentry.config"])
    page = activity_history.build(g.db, t["id"], policy)
    return render_template(
        "activity_history.html", tenant=t, page=page, describe=retention.describe, ranges=activity_history.ranges_text
    )


@bp.get("/tenants/<int:tenant_id>/activity/sign-ins-<period>.csv")
@roles_required("admin", "analyst")
def signins_csv(tenant_id: int, period: str):
    t = tenant_or_404(tenant_id)
    if not MONTH.match(period):
        abort(404)
    audit(g.db, g.user["username"], "report_downloaded", t["id"], f"sign-ins-{period}.csv")
    db_path, tid = current_app.extensions["officesentry.config"].db_path, t["id"]

    def rows():
        # Streamed, so a big month never sits in memory; the request's own connection is closed by then.
        conn = connect(db_path)
        try:
            yield from _lines(conn, tid, period)
        finally:
            conn.close()

    filename = f"sign-ins-{period}-{tables.slug(t['name'])}.csv"
    return Response(
        rows(),
        mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _lines(conn, tenant_id: int, period: str):
    out = StringIO()
    writer = csv.writer(out, lineterminator="\r\n")
    writer.writerow(activity_history.SIGNIN_CSV_COLUMNS)
    yield "\ufeff" + out.getvalue()  # BOM so Excel reads UTF-8
    for row in activity_history.signin_rows(conn, tenant_id, period):
        out.seek(0)
        out.truncate()
        writer.writerow([tables.csv_cell(v) for v in row])
        yield out.getvalue()
