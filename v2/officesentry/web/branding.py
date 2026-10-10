"""MSP branding: the MSP's name, logo, colour and contact details on client reports and the portal.

Also gives every template ``brand`` (loaded only when a template uses it) and
the report helpers ``summary``, ``stacked_bar``, ``meter`` and ``sparkline``.

``/brand.css`` and ``/brand/logo`` (also ``/favicon.ico``) are deliberately
public: the sign-in page shows the MSP's colour and logo before anyone signs
in. They serve only the accent colours and the uploaded logo (or the default
icon), never any other file or setting.
"""

from __future__ import annotations

from pathlib import Path

from flask import Blueprint, Response, current_app, flash, g, redirect, render_template, request, url_for
from markupsafe import escape
from werkzeug.local import LocalProxy

from ..db import audit, connect, transaction
from ..mailer import valid_email
from ..reports import branding, charts, summary
from .access import roles_required

bp = Blueprint("branding", __name__)

DEFAULT_ICON = Path(__file__).parent / "static" / "favicon.svg"
# Public, versioned URLs (?v=<brand.version>), so browsers may keep them for a while.
CACHE = "public, max-age=3600"
TITLE_SUFFIX = " · " + branding.DEFAULT_NAME


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


def page_title(title: str) -> str:
    """A page's <title> with the MSP's name in place of "Office Sentry" ("Users · Office Sentry")."""
    name = current_brand().display_name
    title = (title or "").strip()
    if title == branding.DEFAULT_NAME or not title:
        return escape(name)  # a filter block's plain str isn't escaped
    if title.endswith(TITLE_SUFFIX):
        return title[: -len(TITLE_SUFFIX)] + " · " + name
    return title


@bp.record_once
def _globals(state):
    state.app.jinja_env.globals.update(summary=summary, stacked_bar=charts.stacked_bar, meter=charts.meter,
                                       sparkline=charts.sparkline)
    state.app.jinja_env.filters["page_title"] = page_title


@bp.get("/brand.css")
def brand_css():
    # Public (see the module docstring): colours only.
    resp = Response(current_brand().css, mimetype="text/css")
    resp.headers["Cache-Control"] = CACHE
    return resp


@bp.get("/favicon.ico")  # what browsers ask for on their own
@bp.get("/brand/logo")  # registered first, so url_for gives this one
def logo():
    """The uploaded logo (PNG or JPEG), or the default icon. Public: the sign-in page and favicon use it."""
    image = branding.logo_image(current_brand())
    if image and image[0] in branding.LOGO_TYPES.values():
        resp = Response(image[1], mimetype=image[0])
    else:
        resp = Response(DEFAULT_ICON.read_bytes(), mimetype="image/svg+xml")
    resp.headers["Cache-Control"] = CACHE
    return resp


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
            "brand_support_name": request.form.get("support_name", "").strip()[:80],
            "brand_support_email": request.form.get("support_email", "").strip()[:254],
            "brand_support_phone": request.form.get("support_phone", "").strip()[:40],
            "brand_report_footer": " ".join(request.form.get("report_footer", "").split())[:branding.MAX_FOOTER],
        }
        if values["brand_support_email"] and not valid_email(values["brand_support_email"]):
            flash("Enter a support email like help@harbourit.example.", "error")
            return redirect(url_for("branding.settings"))
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
        flash("Branding saved. The portal, new reports and emails use it straight away.", "success")
        return redirect(url_for("branding.settings"))
    sample = g.db.execute("SELECT id FROM tenants ORDER BY name LIMIT 1").fetchone()
    return render_template("branding.html", b=current_brand(), default_accent=branding.DEFAULT_ACCENT,
                           max_footer=branding.MAX_FOOTER, sample_tenant=sample["id"] if sample else None)
