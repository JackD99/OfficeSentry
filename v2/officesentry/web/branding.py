"""Report branding: the MSP's name, logo, colour and contact line on client reports.

Also gives every template ``brand`` (loaded only when a template uses it) and
the report helpers ``summary``, ``stacked_bar`` and ``meter``.
"""

from __future__ import annotations

from flask import Blueprint, Response, current_app, flash, g, redirect, render_template, request, url_for
from werkzeug.local import LocalProxy

from ..db import audit, connect, transaction
from ..reports import branding, charts, summary
from .access import login_required, roles_required

bp = Blueprint("branding", __name__)


def current_brand() -> branding.Brand:
    """The brand for this request, or (outside a request, e.g. the worker emailing a report) from the database."""
    if "brand" in g:
        return g.brand
    if "db" in g:
        brand = branding.load(g.db)
    else:
        conn = connect(current_app.extensions["officesentry.config"].db_path)
        try:
            brand = branding.load(conn)
        finally:
            conn.close()
    g.brand = brand
    return brand


@bp.app_context_processor
def _inject():
    return {"brand": LocalProxy(current_brand)}


@bp.record_once
def _globals(state):
    state.app.jinja_env.globals.update(summary=summary, stacked_bar=charts.stacked_bar, meter=charts.meter)


@bp.get("/brand.css")
@login_required
def brand_css():
    return Response(current_brand().css, mimetype="text/css")


def _save(values: dict[str, str]) -> None:
    with transaction(g.db):
        for key, value in values.items():
            g.db.execute("INSERT INTO settings (key, value) VALUES (?, ?)"
                         " ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))
        audit(g.db, g.user["username"], "branding_updated", detail=", ".join(sorted(values)))


@bp.route("/admin/branding", methods=["GET", "POST"])
@roles_required("admin")
def settings():
    if request.method == "POST":
        values = {
            "brand_name": request.form.get("name", "").strip()[:80],
            "brand_contact": request.form.get("contact", "").strip()[:160],
        }
        accent = request.form.get("accent", "").strip()
        if accent and not branding.HEX_RE.match(accent):
            flash("Pick a colour like #2557d6.", "error")
            return redirect(url_for("branding.settings"))
        values["brand_accent"] = accent.lower() or branding.DEFAULT_ACCENT
        upload = request.files.get("logo")
        if request.form.get("remove_logo"):
            values["brand_logo"] = ""
        elif upload and upload.filename:
            uri = branding.logo_data_uri(upload.read(branding.MAX_LOGO_BYTES + 1))
            if uri is None:
                flash("The logo must be a PNG or JPEG under 512 KB.", "error")
                return redirect(url_for("branding.settings"))
            values["brand_logo"] = uri
        _save(values)
        flash("Report branding saved. New reports and emails use it straight away.", "success")
        return redirect(url_for("branding.settings"))
    sample = g.db.execute("SELECT id FROM tenants ORDER BY name LIMIT 1").fetchone()
    return render_template("branding.html", b=current_brand(), default_accent=branding.DEFAULT_ACCENT,
                           sample_tenant=sample["id"] if sample else None)
