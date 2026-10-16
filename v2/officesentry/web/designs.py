"""Report designs: the list, the editor (blocks dragged into place, settings in a side panel, a live preview
on a real tenant), and each tenant's copy of every design as a page and a PDF.

Designs belong to the MSP, not to a tenant, so staff manage them (admins and analysts: account managers
are analysts). Nothing here reads from Microsoft; a design is rendered from stored data like every other
report. The editor saves with fetch as JSON; ``designs.clean`` decides what is stored, whatever arrives.
"""

from __future__ import annotations

from flask import Blueprint, abort, flash, g, jsonify, redirect, render_template, request, url_for

from .. import month_end
from ..db import audit
from ..reports import branding, data_reports, designs
from ..reports.export import pdf_available
from .access import can, needs, report_capability, tenant_or_404, visible_tenants
from .printing import pdf_response

bp = Blueprint("designs", __name__)
MAX_DOCUMENT_BYTES = 256 * 1024
# The preview is a report page framed by the editor: the portal's policy, except that it may be framed
# by the portal itself and runs no script at all.
PREVIEW_CSP = (
    "default-src 'self'; script-src 'none'; style-src 'self'; img-src 'self' data:; "
    "object-src 'none'; base-uri 'none'; frame-ancestors 'self'; form-action 'none'"
)


def _design_or_404(design_id: int):
    row = designs.get(g.db, design_id)
    if row is None:
        abort(404)
    return row


def _tenants():
    """The tenants a design can be previewed on: every block reads the security review, so ones whose
    findings the person may open."""
    return [t for t in visible_tenants(g.db, g.user) if not t["archived_at"] and can("reports.security", t["id"])]


def _preview_tenant(tenants):
    """The tenant the editor previews on: ``?tenant=`` when it's one the person sees, else the first."""
    wanted = request.args.get("tenant", type=int)
    return next((t for t in tenants if t["id"] == wanted), tenants[0] if tenants else None)


def _report_context(tenant, row, *, preview: bool = False, period: str | None = None, client_copy=False) -> dict:
    """What ``reports/design.html`` needs, with the month's client-safe commentary and notes, less what the
    month-end review left out of this client's copy. Tables of detail reports the person can't open on this
    tenant are left out."""
    period = period or month_end.current_period(_cfg().timezone)
    me = month_end.report_context(g.db, tenant["id"], period, staff=False)["month_end"]
    return designs.context(
        g.db,
        tenant,
        row,
        brand=branding.load(g.db),
        month_end=me,
        preview=preview,
        client_copy=client_copy,
        opens=lambda key: can(report_capability(key), tenant["id"]),
    )


def _cfg():
    from flask import current_app

    return current_app.extensions["officesentry.config"]


def _slug(text: str) -> str:
    import re

    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "report"


# -- The MSP's designs ----------------------------------------------------------------------------------


@bp.get("/reporting/designs")
@needs("designs.manage")
def index():
    rows = designs.list_designs(g.db)
    used = designs.usage(g.db)
    counts = {r["id"]: len(designs.document(r)["blocks"]) for r in rows}
    return render_template(
        "designs.html", designs=rows, used=used, counts=counts, starters=designs.STARTERS, tenants=_tenants()
    )


@bp.post("/reporting/designs")
@needs("designs.manage")
def create():
    name = request.form.get("name", "").strip()
    starter = request.form.get("starter", "blank")
    if starter not in designs.STARTERS:
        abort(400)
    label, description, doc = designs.STARTERS[starter]
    if not name:
        name = label if starter != "blank" else "Untitled report"
    design_id = designs.create(g.db, name, g.user["username"], doc, description if starter != "blank" else "")
    audit(g.db, g.user["username"], "report_design_created", None, f"{design_id}: {name}")
    return redirect(url_for("designs.edit", design_id=design_id))


@bp.get("/reporting/designs/<int:design_id>")
@needs("designs.manage")
def edit(design_id: int):
    row = _design_or_404(design_id)
    tenants = _tenants()
    tenant = _preview_tenant(tenants)
    return render_template(
        "design_editor.html",
        design=row,
        doc=designs.document(row),
        palette=designs.palette(data_reports.hidden(g.db)),
        tenants=tenants,
        tenant=tenant,
        pdf_available=pdf_available(),
        text_max=designs.TEXT_MAX,
        max_blocks=designs.MAX_BLOCKS,
        placeholders=designs.PLACEHOLDERS,
        preview_url=url_for("designs.preview", design_id=row["id"], tenant=tenant["id"]) if tenant else "",
    )


@bp.post("/reporting/designs/<int:design_id>")
@needs("designs.manage")
def save(design_id: int):
    """The editor's autosave: the whole document as JSON. Answers with what was stored."""
    row = _design_or_404(design_id)
    if request.content_length and request.content_length > MAX_DOCUMENT_BYTES:
        abort(413)
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        abort(400)
    if isinstance(body.get("blocks"), list) and len(body["blocks"]) > designs.MAX_BLOCKS:
        # Refused whole, rather than keeping the first ones and dropping the rest without a word.
        error = f"Not saved: a design holds {designs.MAX_BLOCKS} blocks at most. Remove some and it saves."
        return jsonify({"ok": False, "error": error}), 400
    name = body.get("name") if isinstance(body.get("name"), str) else None
    description = body.get("description") if isinstance(body.get("description"), str) else None
    doc = designs.save(g.db, row["id"], body, name=name, description=description)
    audit(g.db, g.user["username"], "report_design_saved", None, f"{row['id']}: {len(doc['blocks'])} blocks")
    return jsonify({"ok": True, "document": doc, "name": designs.get(g.db, row["id"])["name"]})


@bp.post("/reporting/designs/<int:design_id>/rename")
@needs("designs.manage")
def rename(design_id: int):
    row = _design_or_404(design_id)
    name = request.form.get("name", "").strip()
    if not name:
        flash("Give the design a name.", "error")
    else:
        designs.save(
            g.db, row["id"], designs.document(row), name=name, description=request.form.get("description", "").strip()
        )
        audit(g.db, g.user["username"], "report_design_renamed", None, f"{row['id']}: {name}")
        flash("Saved.", "success")
    return redirect(url_for("designs.edit", design_id=row["id"]))


@bp.post("/reporting/designs/<int:design_id>/duplicate")
@needs("designs.manage")
def duplicate(design_id: int):
    row = _design_or_404(design_id)
    new_id = designs.create(
        g.db, f"{row['name']} (copy)", g.user["username"], designs.document(row), row["description"]
    )
    audit(g.db, g.user["username"], "report_design_created", None, f"{new_id}: copy of {row['id']}")
    return redirect(url_for("designs.edit", design_id=new_id))


@bp.post("/reporting/designs/<int:design_id>/delete")
@needs("designs.manage", "settings.manage")  # it also drops the design from every plan and schedule
def delete(design_id: int):
    row = _design_or_404(design_id)
    emptied = designs.remove(g.db, row["id"])
    audit(g.db, g.user["username"], "report_design_deleted", None, f"{row['id']}: {row['name']}")
    flash(f"Removed “{row['name']}”. Plans and schedules that sent it no longer do.", "success")
    if emptied:
        names = ", ".join(f"“{n}”" for n in emptied)
        flash(
            f"{names} {'has' if len(emptied) == 1 else 'have'} no reports left, so nothing is approved or sent"
            f" from {'it' if len(emptied) == 1 else 'them'} until you choose one under Report plans.",
            "error",
        )
    return redirect(url_for("designs.index"))


@bp.get("/reporting/designs/<int:design_id>/preview")
@needs("designs.manage")
def preview(design_id: int):
    """The design on one tenant, as the editor's preview pane shows it (empty blocks named, no toolbar)."""
    row = _design_or_404(design_id)
    tenant = _preview_tenant(_tenants())
    g.csp_override = PREVIEW_CSP
    if tenant is None:
        return render_template("design_preview_empty.html"), 200
    return render_template(
        "reports/design.html",
        pdf=False,
        paper=True,  # light, as the PDF is
        css_href=url_for("static", filename="report.css"),
        **_report_context(tenant, row, preview=True),
    )


# -- A tenant's copy -------------------------------------------------------------------------------------


@bp.get("/tenants/<int:tenant_id>/designs")
@needs("reports.security", kind="staff")
def tenant_designs(tenant_id: int):
    t = tenant_or_404(tenant_id)
    rows = designs.list_designs(g.db)
    return render_template(
        "tenant_designs.html",
        tenant=t,
        designs=rows,
        pdf_available=pdf_available(),
        counts={r["id"]: len(designs.document(r)["blocks"]) for r in rows},
    )


@bp.get("/tenants/<int:tenant_id>/designs/<int:design_id>")
@needs("reports.security", kind="staff")
def tenant_design(tenant_id: int, design_id: int):
    t = tenant_or_404(tenant_id)
    row = _design_or_404(design_id)
    return render_template(
        "reports/design.html",
        pdf=False,
        css_href=url_for("static", filename="report.css"),
        pdf_available=pdf_available(),
        **_report_context(t, row),
    )


@bp.get("/tenants/<int:tenant_id>/designs/<int:design_id>.pdf")
@needs("reports.security", "export", kind="staff")
def tenant_design_pdf(tenant_id: int, design_id: int):
    t = tenant_or_404(tenant_id)
    row = _design_or_404(design_id)
    audit(g.db, g.user["username"], "report_downloaded", t["id"], f"design:{row['id']} pdf")
    return pdf_response("reports/design.html", f"{_slug(row['name'])}-{_slug(t['name'])}", **_report_context(t, row))
