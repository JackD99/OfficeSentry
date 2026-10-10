"""Admin page for how long personal data is kept (``retention.py`` does the work)."""

from __future__ import annotations

from flask import Blueprint, current_app, flash, g, redirect, render_template, request, url_for

from .. import retention
from ..db import audit, transaction
from .access import roles_required

bp = Blueprint("retention", __name__, url_prefix="/admin")


def _cfg():
    return current_app.extensions["officesentry.config"]


@bp.get("/retention")
@roles_required("admin")
def settings():
    cfg = _cfg()
    policy = retention.load(g.db, cfg)
    return render_template(
        "retention.html",
        p=policy,
        d=retention.defaults(cfg),
        counts=retention.dry_run(g.db, policy, cfg.timezone),
        limits=retention.LIMITS,
        describe=retention.describe,
        audit_floor=retention.AUDIT_FLOOR_DAYS,
        issued_floor=retention.ISSUED_FLOOR_DAYS,
    )


@bp.post("/retention")
@roles_required("admin")
def save():
    policy, problem = retention.validate(request.form)
    if problem:
        flash(problem, "error")
        return redirect(url_for("retention.settings"))
    old = retention.load(g.db, _cfg())
    changed = retention.changes(old, policy)
    with transaction(g.db):
        retention.save(g.db, policy)
        if changed:
            audit(g.db, g.user["username"], "retention_updated", detail="; ".join(changed))
    flash(
        "Retention saved. The worker applies it in its next daily pass." if changed else "Nothing changed.", "success"
    )
    return redirect(url_for("retention.settings"))
