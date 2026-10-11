"""Recommended projects: work each client's tenant shows they need, for account managers."""

from __future__ import annotations

from datetime import UTC, datetime

from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for

from .. import projects
from ..db import audit
from ..reports import tables
from ..snapshot import TenantSnapshot
from .access import roles_required, tenant_or_404, visible_tenants
from .across import _cover, _download
from .printing import pdf_response

bp = Blueprint("projects", __name__)


def _filters():
    project = request.args.get("project", "")
    project = projects.MERGED.get(project, project)  # links to a project since folded into a bigger one
    return (project if project in projects.PROJECTS else ""), request.args.get("view") == "declined"


@bp.get("/projects")
@roles_required("admin", "analyst")
def index():
    project, declined = _filters()
    data = projects.pipeline(g.db, visible_tenants(g.db, g.user))
    return render_template(
        "projects.html",
        data=data,
        items=data.items(project, declined),
        project=project,
        declined=declined,
        catalog=projects.CATALOG,
        statuses=projects.STATUSES,
    )


@bp.get("/projects.csv", defaults={"ext": "csv"})
@bp.get("/projects.xlsx", defaults={"ext": "xlsx"})
@roles_required("admin", "analyst")
def download(ext: str):
    project, declined = _filters()
    data = projects.pipeline(g.db, visible_tenants(g.db, g.user))
    table = projects.to_table(data.items(project, declined))
    audit(g.db, g.user["username"], "projects_downloaded", None, f"projects.{ext}")
    body = (
        tables.to_csv(table)
        if ext == "csv"
        else tables.to_xlsx(
            [table],
            cover=_cover(
                "Recommended projects",
                "All clients",
                "Projects each client's Microsoft 365 data shows they need, and where each one stands.",
                facts=[("Clients with a recommendation", data.tenants_with, ""), ("Projects", len(table.rows), "")],
            ),
        )
    )
    return _download(body, "recommended-projects", ext)


@bp.get("/tenants/<int:tenant_id>/projects")
@roles_required("admin", "analyst")
def tenant(tenant_id: int):
    t = tenant_or_404(tenant_id)
    snap = TenantSnapshot(g.db, t)
    found = projects.assess(g.db, t, snap=snap)
    return render_template(
        "tenant_projects.html",
        tenant=t,
        recs=found.projects,
        quick=found.quick_fixes,
        quick_hours=projects.effort_label(found.quick_hours),
        tenant_has_data=bool(snap.runs),
        statuses=projects.STATUSES,
        min_hours=projects.MIN_HOURS,
    )


@bp.get("/tenants/<int:tenant_id>/projects.pdf")
@roles_required("admin", "analyst")
def pdf(tenant_id: int):
    """The client-facing proposal: every recommendation they haven't declined, without staff notes."""
    t = tenant_or_404(tenant_id)
    recs = [r for r in projects.recommend(g.db, t) if r.status != "declined"]
    audit(g.db, g.user["username"], "report_downloaded", t["id"], "projects.pdf")
    return pdf_response(
        "reports/projects.html",
        f"recommended-projects-{tables.slug(t['name'])}",
        tenant=t,
        recs=recs,
        today=datetime.now(UTC),
    )


@bp.post("/tenants/<int:tenant_id>/projects/<key>")
@roles_required("admin", "analyst")
def set_status(tenant_id: int, key: str):
    t = tenant_or_404(tenant_id)
    status = request.form.get("status", "")
    if key not in projects.PROJECTS or status not in projects.STATUSES:
        abort(400)
    note = request.form.get("note", "").strip()[:500]
    projects.set_status(g.db, t["id"], key, status, note, g.user["username"])
    audit(g.db, g.user["username"], "project_status_changed", t["id"], f"{key}: {status}")
    flash(f"{projects.PROJECTS[key].title}: {projects.STATUSES[status]}.", "success")
    if request.form.get("back") == "all":
        return redirect(url_for("projects.index", project=request.form.get("project") or None))
    return redirect(url_for("projects.tenant", tenant_id=t["id"]) + f"#project-{key}")
