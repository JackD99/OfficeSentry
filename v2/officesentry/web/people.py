"""One page per account (``reports/profile/``) and the Users tab that lists them."""

from __future__ import annotations

import re

from flask import Blueprint, Response, abort, g, render_template, request

from ..db import audit
from ..reports import branding, data_reports
from ..reports import profile as profiles
from .access import needs, tenant_or_404
from .printing import pdf_response

bp = Blueprint("people", __name__)


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-") or "account"


def _profile(tenant_id: int, user_id: str):
    t = tenant_or_404(tenant_id)
    profile = profiles.build(g.db, t, user_id)
    if profile is None:
        abort(404)
    return t, profile


@bp.get("/tenants/<int:tenant_id>/users")
@needs("reports.people")
def people(tenant_id: int):
    t = tenant_or_404(tenant_id)
    found = profiles.directory(g.db, t)
    rows, tiles = found if found else ([], [])
    return render_template("people.html", tenant=t, rows=rows, tiles=tiles, collected=found is not None)


@bp.get("/tenants/<int:tenant_id>/users/<user_id>")
@needs("reports.people")
def person(tenant_id: int, user_id: str):
    t = tenant_or_404(tenant_id)
    # ?leaver=1 shows the leaver checklist for an account whose sign-in is still allowed.
    profile = profiles.build(g.db, t, user_id, leaver=request.args.get("leaver") == "1")
    if profile is None:
        abort(404)
    return render_template("user.html", tenant=t, profile=profile)


@bp.get("/tenants/<int:tenant_id>/users/<user_id>.pdf")
@needs("reports.people", "export")
def person_pdf(tenant_id: int, user_id: str):
    t, profile = _profile(tenant_id, user_id)
    audit(g.db, g.user["username"], "report_downloaded", t["id"], "user.pdf")
    return pdf_response(
        "reports/detail.html", f"account-{_slug(profile.upn or profile.name)}", report=profile.as_report()
    )


@bp.get("/tenants/<int:tenant_id>/users/<user_id>.xlsx")
@needs("reports.people", "export")
def person_xlsx(tenant_id: int, user_id: str):
    t, profile = _profile(tenant_id, user_id)
    audit(g.db, g.user["username"], "report_downloaded", t["id"], "user.xlsx")
    return Response(
        data_reports.to_xlsx(profile.as_report(), brand=branding.load(g.db)),
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename=account-{_slug(profile.upn or profile.name)}.xlsx"},
    )


@bp.get("/tenants/<int:tenant_id>/users/<user_id>/leaver.pdf")
@needs("reports.people", "export")
def leaver_pdf(tenant_id: int, user_id: str):
    """The 'Before you close this account' checklist on one page, for the client to sign off."""
    t = tenant_or_404(tenant_id)
    sheet = profiles.leaver_sheet(g.db, t, user_id)
    if sheet is None:
        abort(404)
    audit(g.db, g.user["username"], "report_downloaded", t["id"], "leaver.pdf")
    return pdf_response("reports/leaver.html", f"leaver-checklist-{_slug(sheet.upn or sheet.name)}", sheet=sheet)
