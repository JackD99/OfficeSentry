"""Admin page for alerts to Teams, Slack or a webhook."""

from __future__ import annotations

from flask import Blueprint, abort, current_app, flash, g, redirect, render_template, request, url_for

from .. import alerts
from ..db import audit, transaction
from .access import roles_required

bp = Blueprint("alerts", __name__, url_prefix="/admin")


def _vault():
    return current_app.extensions["officesentry.vault"]


@bp.get("/alerts")
@roles_required("admin")
def settings():
    s = alerts.settings(g.db)
    recent = g.db.execute(
        "SELECT a.*, t.name AS tenant_name FROM audit_log a LEFT JOIN tenants t ON t.id = a.tenant_id"
        " WHERE a.action IN ('alert_sent', 'alert_failed') ORDER BY a.id DESC LIMIT 10").fetchall()
    return render_template("alerts.html", s=s, url_hint=alerts.masked_url(_vault(), s),
                           formats=alerts.FORMATS, severities=alerts.ALERT_SEVERITIES, recent=recent)


@bp.post("/alerts")
@roles_required("admin")
def save():
    vault = _vault()
    current = alerts.settings(g.db)
    fmt = request.form.get("format", "")
    severity = request.form.get("min_severity", "")
    if fmt not in alerts.FORMATS or severity not in alerts.ALERT_SEVERITIES:
        abort(400)
    values = {
        "alerts.enabled": "1" if request.form.get("enabled") == "1" else "0",
        "alerts.format": fmt,
        "alerts.min_severity": severity,
        "alerts.on_failures": "1" if request.form.get("on_failures") == "1" else "0",
        "alerts.on_recovery": "1" if request.form.get("on_recovery") == "1" else "0",
    }
    url = request.form.get("url", "").strip()
    url_change = "kept"
    if url:
        if problem := alerts.url_problem(url):
            flash(problem, "error")
            return redirect(url_for("alerts.settings"))
        values["alerts.url_enc"] = vault.encrypt(url)
        url_change = "changed"
    elif request.form.get("clear_url") == "1" and current["alerts.url_enc"]:
        values["alerts.url_enc"] = ""
        url_change = "removed"
    secret = request.form.get("secret", "")
    signing_state = "kept"
    if secret:
        if len(secret) > 256:
            flash("The signing secret can be up to 256 characters.", "error")
            return redirect(url_for("alerts.settings"))
        values["alerts.secret_enc"] = vault.encrypt(secret)
        signing_state = "changed"
    elif request.form.get("clear_secret") == "1" and current["alerts.secret_enc"]:
        values["alerts.secret_enc"] = ""
        signing_state = "removed"
    has_url = bool(values.get("alerts.url_enc", current["alerts.url_enc"]))
    if values["alerts.enabled"] == "1" and not has_url:
        flash("Enter the webhook URL to turn alerts on.", "error")
        return redirect(url_for("alerts.settings"))
    with transaction(g.db):
        alerts.save_settings(g.db, values)
        # Never the URL or the secret: the URL usually carries its own access key.
        audit(g.db, g.user["username"], "alert_settings_updated",
              detail=f"enabled={values['alerts.enabled']} format={fmt} severity={severity} "
                     f"failures={values['alerts.on_failures']} recovery={values['alerts.on_recovery']} "
                     f"url={url_change} secret={signing_state}")
    flash("Alert settings saved.", "success")
    return redirect(url_for("alerts.settings"))


@bp.post("/alerts/test")
@roles_required("admin")
def send_test():
    s = alerts.settings(g.db)
    base_url = current_app.extensions["officesentry.config"].base_url
    try:
        alerts.send(g.db, _vault(), alerts.test_message(base_url), s)
    except alerts.AlertError as exc:
        audit(g.db, g.user["username"], "alert_test_failed", detail=str(exc)[:500])
        flash(f"Test failed: {exc}", "error")
    else:
        audit(g.db, g.user["username"], "alert_test_sent", detail=s["alerts.format"])
        flash(f"Test alert sent to {alerts.masked_url(_vault(), s)}", "success")
    return redirect(url_for("alerts.settings"))
