"""Prospect assessments: public checks on a domain before it's a client."""

from __future__ import annotations

from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for

from .. import prospect
from ..db import audit
from .access import roles_required
from .printing import pdf_response

bp = Blueprint("prospects", __name__)


@bp.route("/prospects", methods=["GET", "POST"])
@roles_required("admin", "analyst")
def index():
    if request.method == "POST":
        domain = prospect.normalise_domain(request.form.get("domain", ""))
        company = request.form.get("company", "").strip()[:100]
        if domain is None:
            flash("Enter a domain such as contoso.com.", "error")
            return redirect(url_for("prospects.index"))
        assessment = prospect.run(domain, company)
        pid = prospect.save(g.db, assessment, g.user["username"])
        audit(g.db, g.user["username"], "prospect_assessed", detail=domain)
        return redirect(url_for("prospects.view", prospect_id=pid))
    rows = g.db.execute("SELECT * FROM prospect_assessments ORDER BY id DESC LIMIT 50").fetchall()
    return render_template("prospects.html", rows=[prospect.load(r) for r in rows])


def _assessment(prospect_id: int):
    row = g.db.execute("SELECT * FROM prospect_assessments WHERE id = ?", (prospect_id,)).fetchone()
    if row is None:
        abort(404)
    return prospect.load(row)


@bp.get("/prospects/<int:prospect_id>")
@roles_required("admin", "analyst")
def view(prospect_id: int):
    return render_template("reports/prospect.html", a=_assessment(prospect_id), pdf=False,
                           css_href=url_for("static", filename="report.css"))


@bp.get("/prospects/<int:prospect_id>.pdf")
@roles_required("admin", "analyst")
def pdf(prospect_id: int):
    a = _assessment(prospect_id)
    return pdf_response("reports/prospect.html", f"email-security-{a.domain}", a=a)


@bp.post("/prospects/<int:prospect_id>/delete")
@roles_required("admin", "analyst")
def delete(prospect_id: int):
    a = _assessment(prospect_id)
    g.db.execute("DELETE FROM prospect_assessments WHERE id = ?", (prospect_id,))
    audit(g.db, g.user["username"], "prospect_deleted", detail=a.domain)
    return redirect(url_for("prospects.index"))
