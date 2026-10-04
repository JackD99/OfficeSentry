"""Saved table views: a person's filters, columns and sort for a detail report, by name.

A view is the report page's query string (see ``reports.views``), so it isn't
tied to a tenant: the same view opens on every tenant the person can see.
"""

from __future__ import annotations

from urllib.parse import urlsplit

from flask import Blueprint, abort, flash, g, redirect, request, url_for

from ..db import transaction, utcnow
from ..reports import data_reports, views
from .access import login_required, tenant_or_404

bp = Blueprint("views", __name__)

MAX_VIEWS = 100  # per person


def _back(default: str) -> str:
    """Only ever redirect within the portal."""
    target = request.form.get("next", "")
    parts = urlsplit(target)
    return target if target.startswith("/") and not target.startswith("//") and not parts.netloc else default


@bp.post("/reports/<key>/views")
@login_required
def save(key: str):
    if key not in data_reports.REPORTS:
        abort(404)
    tenant = tenant_or_404(request.form.get("tenant_id", type=int) or 0)
    page = url_for("reports.data_report", tenant_id=tenant["id"], key=key)
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
    flash(f"Saved “{name}”. It's under Views on this report for every tenant.", "success")
    return redirect(f"{page}?{query}")


@bp.post("/views/<int:view_id>/delete")
@login_required
def delete(view_id: int):
    row = g.db.execute("SELECT * FROM saved_views WHERE id = ? AND user_id = ?", (view_id, g.user["id"])).fetchone()
    if row is None:
        abort(404)
    with transaction(g.db):
        g.db.execute("DELETE FROM saved_views WHERE id = ?", (view_id,))
    flash(f"Deleted the view “{row['name']}”.", "success")
    return redirect(_back(url_for("reports.overview")))
