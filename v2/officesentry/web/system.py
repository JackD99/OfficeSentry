"""System status: is Office Sentry itself working, the monitoring link for an uptime checker, and system alerts."""

from __future__ import annotations

from flask import Blueprint, current_app, flash, g, redirect, render_template, url_for

from .. import alerts, delivery, mailer, system_alerts, system_health
from ..db import audit, transaction
from .access import roles_required

bp = Blueprint("system", __name__, url_prefix="/admin")

# Where each problem is fixed in the portal, when it is.
FIX_LINKS = {
    "certificates": ("admin.connections", "Open App connection"),
    "collection": ("tenants.dashboard", "Open Tenants"),
}


def _page(new_token: str | None = None, status: int = 200):
    cfg = current_app.extensions["officesentry.config"]
    health = system_health.assess(g.db, cfg)
    s = alerts.settings(g.db)
    recent = g.db.execute("SELECT * FROM audit_log WHERE action = 'system_alert' ORDER BY id DESC LIMIT 8").fetchall()
    channels = {
        "email": len(delivery.team_contacts(g.db)) if mailer.is_configured(mailer.mail_settings(g.db)) else 0,
        "webhook": alerts.FORMATS.get(s["alerts.format"])
        if s["alerts.enabled"] == "1" and s["alerts.url_enc"]
        else None,
    }
    base = cfg.base_url.rstrip("/")
    return render_template(
        "system_status.html",
        health=health,
        checks={c.key: c for c in health.checks},
        fix_links=FIX_LINKS,
        system_on=s["alerts.system"] == "1",
        channels=channels,
        tracked=system_alerts.states(g.db),
        labels=system_alerts.LABELS,
        recent=recent,
        has_token=system_health.has_token(g.db),
        new_token=new_token,
        size=system_health.size_text,
        ready_url=f"{base}/health/ready",
        alive_url=f"{base}/health",
    ), status


@bp.get("/status")
@roles_required("admin")
def status():
    return _page()


@bp.post("/status/token")
@roles_required("admin")
def create_token():
    with transaction(g.db):
        replaced = system_health.has_token(g.db)
        token = system_health.new_token(g.db)
        audit(g.db, g.user["username"], "health_token_created", detail="replaced the old one" if replaced else None)
    # Shown on this response only: just its hash is stored.
    return _page(new_token=token)


@bp.post("/status/token/remove")
@roles_required("admin")
def remove_token():
    with transaction(g.db):
        system_health.remove_token(g.db)
        audit(g.db, g.user["username"], "health_token_removed")
    flash("Monitoring token removed. Uptime monitors now only see whether Office Sentry is ready.", "success")
    return redirect(url_for("system.status"))
