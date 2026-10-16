"""Acting on a finding: the sheet beside the Findings tab, and what staff do from it.

The sheet (``reports/finding_sheet.py``) opens from a row of the Findings tab, as a page of its own
(``reports.finding``, so a link to it works and phones get it first) or in place (``finding.sheet``, the
same aside fetched by ``static/findings.js``). From it staff copy the ticket text, email the client's
contact from their own mail app (a ``mailto:`` link: Office Sentry sends nothing), accept the risk, keep a
note and hand the finding to a colleague. Notes and hand-offs are ``findings`` columns (migration 0048),
staff-only, and never reach a client's reports, PDFs, emails or pages. Nothing is written to a tenant.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for

from .. import delivery, findings
from ..db import audit
from ..reports import finding_sheet
from .access import access_of, can, can_see_tenant, is_staff, needs, tenant_or_404, visible_tenants

bp = Blueprint("finding", __name__)


def finding_or_404(finding_id: int, tenant_id: int | None = None):
    """The finding, if the signed-in person may see its client (and it is ``tenant_id``'s, when given)."""
    row = g.db.execute("SELECT * FROM findings WHERE id = ?", (finding_id,)).fetchone()
    if row is None or (tenant_id is not None and row["tenant_id"] != tenant_id):
        abort(404)
    return tenant_or_404(row["tenant_id"]), row


def holders(tenant_id: int) -> list:
    """Enabled staff accounts a finding on this client can be handed to: those who see the client and open
    its findings there. Nobody else is offered, and ``hand`` refuses anyone else."""
    rows = g.db.execute(
        "SELECT * FROM users WHERE role <> 'client' AND disabled = 0 ORDER BY username COLLATE NOCASE"
    ).fetchall()
    return [
        u for u in rows if can_see_tenant(g.db, u, tenant_id) and access_of(u, g.db).can("reports.security", tenant_id)
    ]


def sheet_for(tenant, row) -> dict:
    """Everything ``_finding_sheet.html`` draws for this finding, for the signed-in person."""
    staff = is_staff()
    others = [t for t in visible_tenants(g.db, g.user) if t["id"] != tenant["id"]] if can("clients.compare") else []
    sheet = finding_sheet.build(
        g.db,
        tenant,
        row,
        others=others,
        staff=staff,
        link=url_for("reports.finding", tenant_id=tenant["id"], finding_id=row["id"], _external=True),
    )
    manages = can("findings.manage", tenant["id"])
    contacts = delivery.tenant_contacts(g.db, tenant["id"]) if staff and can("emails.view", tenant["id"]) else []
    return {
        "sheet": sheet,
        "finding": row,
        "manages": manages,
        "contacts": [(c, sheet.email(c)) for c in contacts],
        "holders": holders(tenant["id"]) if manages else [],
        "adds_contacts": staff and can("emails.manage", tenant["id"]),
        "first_review_day": first_review_day(),
    }


def first_review_day() -> str:
    """The earliest review date an accepted risk can have: tomorrow. The acceptance runs until midnight UTC at
    the start of its review date, so one of today or earlier would be over before it began."""
    return (datetime.now(UTC).date() + timedelta(days=1)).isoformat()


@bp.get("/findings/<int:finding_id>/sheet")
@needs("reports.security")
def sheet(finding_id: int):
    """The sheet alone, for opening in place beside the table (``findings.js``)."""
    tenant, row = finding_or_404(finding_id)
    return render_template("_finding_sheet.html", tenant=tenant, **sheet_for(tenant, row), fragment=True)


def _back(row):
    return redirect(url_for("reports.finding", tenant_id=row["tenant_id"], finding_id=row["id"]))


@bp.post("/findings/<int:finding_id>/note")
@needs("findings.manage")
def note(finding_id: int):
    """Keep a staff note on the finding (empty removes it). The text never goes in the activity log."""
    _, row = finding_or_404(finding_id)
    text = "" if request.form.get("remove") else request.form.get("note", "").strip()
    findings.note(g.db, row["id"], text, g.user["username"])
    audit(
        g.db,
        g.user["username"],
        "finding_noted" if text else "finding_note_removed",
        row["tenant_id"],
        f"{row['rule_key']}:{row['subject']}",
    )
    flash("Note saved. Only the team sees it." if text else "Note removed.", "success")
    return _back(row)


@bp.post("/findings/<int:finding_id>/hand")
@needs("findings.manage")
def hand(finding_id: int):
    """Hand the finding to a colleague who sees this client, or back to nobody."""
    tenant, row = finding_or_404(finding_id)
    to = request.form.get("to", "").strip()
    if to:
        who = next((u for u in holders(tenant["id"]) if str(u["id"]) == to), None)
        if who is None:
            flash("Pick someone who can open this client's findings.", "error")
            return _back(row)
        findings.hand(g.db, row["id"], who["id"], g.user["username"])
        audit(
            g.db,
            g.user["username"],
            "finding_handed",
            row["tenant_id"],
            f"{row['rule_key']}:{row['subject']} to {who['username']}",
        )
        flash(f"Handed to {who['username']}.", "success")
    else:
        findings.hand(g.db, row["id"], None, g.user["username"])
        audit(
            g.db,
            g.user["username"],
            "finding_handed",
            row["tenant_id"],
            f"{row['rule_key']}:{row['subject']} to nobody",
        )
        flash("Handed back: nobody holds it now.", "success")
    return _back(row)
