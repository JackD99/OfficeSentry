"""A client's grade: the letter, how it's worked out, twelve months of history and the comparison with the
other clients this person sees (``grades.py``). Staff only; a client reads its grade in the monthly review."""

from __future__ import annotations

from datetime import UTC, datetime

from flask import Blueprint, g, render_template

from .. import grades
from ..checks import RULES
from ..reports.monthly_review import rollup_row, setting
from .access import can, needs, tenant_or_404, visible_tenants

bp = Blueprint("grade", __name__)

# Which report area each comparison figure belongs to: a figure the person can't open on this client stays out.
FIGURE_AREAS = {"grade": "reports.security", "severe": "reports.security", "score": "reports.tenant"}
FIGURE_AREAS |= {"waste": "reports.tenant", "mfa": "reports.identity"}


@bp.get("/tenants/<int:tenant_id>/grade")
@needs("reports.security", kind="staff")
def page(tenant_id: int):
    t = tenant_or_404(tenant_id)
    now = datetime.now(UTC)
    graded = grades.assess(g.db, t, now)
    months = grades.history(g.db, t["id"], graded, now)
    grades.rows(g.db, [t], graded={t["id"]: graded})  # keeps the stored row (the header's badge) current
    rollup_row(g.db, t, cached=True)  # this client's figures for the comparison, as the lists show them
    others = [o["id"] for o in visible_tenants(g.db, g.user) if o["id"] != t["id"] and o["enabled"]]
    comparison = grades.comparison(g.db, t["id"], graded.letter, others)
    if comparison:
        comparison.figures = [f for f in comparison.figures if can(FIGURE_AREAS[f.key], t["id"])]
    return render_template(
        "tenant_grade.html",
        tenant=t,
        graded=graded,
        months=months,
        trend=grades.trend(months),
        comparison=comparison,
        currency=setting(g.db, "currency", "GBP"),
        others=len(others),
        min_peers=grades.MIN_PEERS,
        parts=grades.PARTS,
        ungraded=sorted(grades.UNGRADED),
        titles={r.key: r.title for r in RULES},
        grade_of={s: grades.SEVERITY_GRADE[s] for s in ("critical", "high", "medium", "low")},
    )
