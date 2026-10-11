"""Evidence packs by scheme: one client judged against Cyber Essentials, a framework or a form, with downloads.

The client's Evidence page (``reports.evidence_pack``) lists every scheme with its verdict; each scheme has
its own page here, with the controls, the fixes, the form's answers to paste, and a comparison with an earlier
day (``?since=``). Saved answers post to ``reports.evidence_answer`` with the scheme to come back to.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from flask import Blueprint, Response, abort, g, render_template, request

from ..db import audit
from ..reports import (
    branding,
    evidence_answers,
    questionnaires,
    scheme_export,
    schemes,
    schemes_cis,  # noqa: F401  (registers the schemes)
    schemes_cyber_essentials,  # noqa: F401
    schemes_essential_eight,  # noqa: F401
    schemes_forms,  # noqa: F401
    schemes_iasme,  # noqa: F401
    schemes_iso27001,  # noqa: F401
    schemes_nist_csf,  # noqa: F401
    schemes_soc2,  # noqa: F401
    schemes_uk_gdpr,  # noqa: F401
    tables,
)
from ..reports.docx_writer import MIMETYPE as DOCX
from .access import login_required, tenant_or_404
from .printing import pdf_response
from .reports import STAFF, XLSX

bp = Blueprint("evidence", __name__)

SINCE = {"1m": ("last month", 30), "3m": ("3 months ago", 91), "12m": ("a year ago", 365)}


def _scheme_or_404(key: str) -> schemes.Scheme:
    if key not in schemes.SCHEMES:
        abort(404)
    return schemes.SCHEMES[key]


def since_day(raw: str | None, now: datetime | None = None) -> datetime | None:
    """``?since=1m|3m|12m`` or a day (``2025-10-01``), as the end of that UTC day; None for nothing."""
    if not raw:
        return None
    now = now or datetime.now(UTC)
    if raw in SINCE:
        return (now - timedelta(days=SINCE[raw][1])).replace(hour=23, minute=59, second=59, microsecond=0)
    try:
        day = date.fromisoformat(raw[:10])
    except ValueError:
        return None
    return datetime(day.year, day.month, day.day, 23, 59, 59, tzinfo=UTC)


def _result(tenant, key: str) -> schemes.SchemeResult:
    _scheme_or_404(key)
    return schemes.assess(g.db, tenant, key, since=since_day(request.args.get("since")))


def _name(tenant, key: str, ext: str) -> str:
    return f"evidence-{tables.slug(tenant['name'])}-{key}.{ext}"


@bp.get("/tenants/<int:tenant_id>/evidence/<scheme>")
@login_required
def scheme_page(tenant_id: int, scheme: str):
    t = tenant_or_404(tenant_id)
    r = _result(t, scheme)
    show = request.args.get("show") if request.args.get("show") in ("input", "attention") else ""
    since = request.args.get("since") if request.args.get("since") in SINCE else ""
    copy_all = questionnaires.to_text(r.sheet, branding.load(g.db).display_name) if r.sheet is not None else ""
    return render_template(
        "evidence_scheme.html",
        r=r,
        scheme=r.scheme,
        tenant=t,
        groups=schemes.grouped(),
        show=show,
        since=since,
        since_options=SINCE,
        copy_all=copy_all,
        answers=evidence_answers.ANSWERS,
        staff=g.user["role"] in STAFF,
        max_note=evidence_answers.MAX_NOTE,
        V=schemes,
    )


@bp.get("/tenants/<int:tenant_id>/evidence/<scheme>.pdf")
@login_required
def scheme_pdf(tenant_id: int, scheme: str):
    t = tenant_or_404(tenant_id)
    r = _result(t, scheme)
    audit(g.db, g.user["username"], "report_downloaded", t["id"], f"evidence-{scheme}.pdf")
    return pdf_response("reports/scheme_pack.html", _name(t, scheme, "pdf")[:-4], r=r, V=schemes)


@bp.get("/tenants/<int:tenant_id>/evidence/<scheme>.txt")
@login_required
def scheme_text(tenant_id: int, scheme: str):
    t = tenant_or_404(tenant_id)
    r = _result(t, scheme)
    audit(g.db, g.user["username"], "report_downloaded", t["id"], f"evidence-{scheme}.txt")
    return Response(
        scheme_export.to_text(r, branding.load(g.db).display_name),
        mimetype="text/plain; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{_name(t, scheme, "txt")}"'},
    )


@bp.get("/tenants/<int:tenant_id>/evidence/<scheme>.docx")
@login_required
def scheme_docx(tenant_id: int, scheme: str):
    t = tenant_or_404(tenant_id)
    r = _result(t, scheme)
    audit(g.db, g.user["username"], "report_downloaded", t["id"], f"evidence-{scheme}.docx")
    return Response(
        scheme_export.to_docx(r, branding.load(g.db).display_name),
        mimetype=DOCX,
        headers={"Content-Disposition": f'attachment; filename="{_name(t, scheme, "docx")}"'},
    )


@bp.get("/tenants/<int:tenant_id>/evidence/<scheme>.xlsx")
@login_required
def scheme_xlsx(tenant_id: int, scheme: str):
    t = tenant_or_404(tenant_id)
    r = _result(t, scheme)
    audit(g.db, g.user["username"], "report_downloaded", t["id"], f"evidence-{scheme}.xlsx")
    return Response(
        scheme_export.to_xlsx(r, brand=branding.load(g.db)),
        mimetype=XLSX,
        headers={"Content-Disposition": f'attachment; filename="{_name(t, scheme, "xlsx")}"'},
    )


@bp.get("/tenants/<int:tenant_id>/evidence/<scheme>.csv")
@login_required
def scheme_csv(tenant_id: int, scheme: str):
    t = tenant_or_404(tenant_id)
    r = _result(t, scheme)
    audit(g.db, g.user["username"], "report_downloaded", t["id"], f"evidence-{scheme}.csv")
    table = tables.Table(r.scheme.title, scheme_export.COLUMNS, scheme_export.rows(r))
    return Response(
        tables.to_csv(table),
        mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{_name(t, scheme, "csv")}"'},
    )
