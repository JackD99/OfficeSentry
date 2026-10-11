"""Settings → Support: how to get help, and the support package to attach to a bug report (``support.py``)."""

from __future__ import annotations

from pathlib import Path

from flask import Blueprint, Response, current_app, g, render_template, request

from .. import __version__, support, system_health
from ..db import audit, transaction
from .access import roles_required

bp = Blueprint("support", __name__, url_prefix="/admin")

ISSUE_URL = "https://github.com/JackD99/OfficeSentry/issues/new?template=bug.yml"
SECURITY_URL = "https://github.com/JackD99/OfficeSentry/security/advisories/new"
TROUBLESHOOTING_URL = "https://officesentry.co.uk/docs/troubleshooting/"
HELP_URL = "https://officesentry.co.uk/docs/getting-help/"


@bp.get("/support")
@roles_required("admin")
def index():
    cfg = current_app.extensions["officesentry.config"]
    health = system_health.assess(g.db, cfg)
    return render_template(
        "support.html",
        version=__version__,
        health=health,
        figures=support.preview(g.db, cfg),
        parts=support.PARTS,
        failure_days=support.FAILURE_DAYS,
        issue_url=ISSUE_URL,
        security_url=SECURITY_URL,
        troubleshooting_url=TROUBLESHOOTING_URL,
        help_url=HELP_URL,
        log_dir=cfg.data_dir / "logs",
        docker=Path("/.dockerenv").exists(),
    )


@bp.post("/support/package")
@roles_required("admin")
def package():
    """Build the package now and send it; nothing is kept on the server."""
    cfg = current_app.extensions["officesentry.config"]
    identifiers = request.form.get("identifiers") == "1"
    pkg = support.build(g.db, cfg, include_identifiers=identifiers)
    with transaction(g.db):
        audit(
            g.db,
            g.user["username"],
            "support_package_created",
            detail="with client identifiers" if identifiers else "clients as numbers",
        )
    data = support.as_zip(pkg)
    return Response(
        data,
        mimetype="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{pkg.filename}"',
            "Content-Length": str(len(data)),
        },
    )
