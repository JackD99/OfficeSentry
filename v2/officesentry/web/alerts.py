"""Admin page for alerts to Teams, Slack or a webhook."""

from __future__ import annotations

from flask import Blueprint, abort, current_app, flash, g, redirect, render_template, request, url_for

from .. import activity_alerts, alerts
from ..db import audit, transaction
from .access import needs

bp = Blueprint("alerts", __name__, url_prefix="/admin")

# The shortest signing secret accepted. Every signed request is a known body and timestamp with its HMAC, so a
# short secret can be found by trying them all, and then anyone can sign a request.
MIN_SECRET = 16


def _vault():
    return current_app.extensions["officesentry.vault"]


@bp.get("/alerts")
@needs("settings.manage")
def settings():
    s = alerts.settings(g.db)
    recent = g.db.execute(
        "SELECT a.*, t.name AS tenant_name FROM audit_log a LEFT JOIN tenants t ON t.id = a.tenant_id"
        " WHERE a.action IN ('alert_sent', 'alert_failed') ORDER BY a.id DESC LIMIT 10"
    ).fetchall()
    email_alerts = g.db.execute("SELECT COUNT(*) FROM email_alerts WHERE enabled = 1").fetchone()[0]
    return render_template(
        "alerts.html",
        s=s,
        url_hint=alerts.masked_url(_vault(), s),
        formats=alerts.FORMATS,
        severities=alerts.ALERT_SEVERITIES,
        activity=alerts.ACTIVITY_ALERTS,
        chosen=alerts.activity_kinds(s),
        recent=recent,
        email_alerts=email_alerts,
    )


@bp.post("/alerts")
@needs("settings.manage")
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
        "alerts.on_reports": "1" if request.form.get("on_reports") == "1" else "0",
        "alerts.system": "1" if request.form.get("system") == "1" else "0",
        "alerts.activity": ",".join(k for k in alerts.ACTIVITY_ALERTS if k in request.form.getlist("activity")),
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
        if not MIN_SECRET <= len(secret) <= 256:
            flash(
                f"Use a signing secret of {MIN_SECRET} to 256 characters. A short one can be guessed from a single "
                "signed request, and then anyone can sign their own.",
                "error",
            )
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
        audit(
            g.db,
            g.user["username"],
            "alert_settings_updated",
            detail=f"enabled={values['alerts.enabled']} format={fmt} severity={severity} "
            f"failures={values['alerts.on_failures']} recovery={values['alerts.on_recovery']} "
            f"reports={values['alerts.on_reports']} system={values['alerts.system']} "
            f"activity={values['alerts.activity'] or 'none'} "
            f"url={url_change} secret={signing_state}",
        )
    flash("Alert settings saved.", "success")
    return redirect(url_for("alerts.settings"))


@bp.post("/alerts/test")
@needs("settings.manage")
def send_test():
    s = alerts.settings(g.db)
    base_url = current_app.extensions["officesentry.config"].base_url
    samples = {
        "report": ("Sample report message", alerts.sample_report_message),
        "activity": ("Sample change", activity_alerts.sample_message),
    }
    sample = request.form.get("sample")
    what, build = samples.get(sample, ("Test alert", alerts.test_message))
    try:
        alerts.send(g.db, _vault(), build(base_url), s)
    except alerts.AlertError as exc:
        audit(g.db, g.user["username"], "alert_test_failed", detail=str(exc)[:500])
        flash(f"Test failed: {exc}", "error")
    else:
        detail = s["alerts.format"] + (f" {sample}" if sample in samples else "")
        audit(g.db, g.user["username"], "alert_test_sent", detail=detail)
        flash(f"{what} sent to {alerts.masked_url(_vault(), s)}", "success")
    return redirect(url_for("alerts.settings"))
