"""Report scheduling, SMTP configuration, and branding routes."""

from flask import request, jsonify, render_template

from helpers import login_required, current_user
from database import (
    list_tenants, log_action,
    create_schedule, list_schedules, get_schedule, update_schedule, delete_schedule,
    get_tenant_branding, get_default_branding, upsert_tenant_branding,
)

from routes.reports import bp


# ---------------------------------------------------------------------------
# Report Scheduling
# ---------------------------------------------------------------------------

@bp.route("/reports/schedules")
@login_required
def schedules_page():
    """Report scheduling management page."""
    schedules = list_schedules()
    tenants = list_tenants()
    tenants_map = {t["id"]: t["name"] for t in tenants}
    return render_template("report_schedules.html",
                           schedules=schedules, tenants=tenants,
                           tenants_map=tenants_map)


@bp.route("/reports/schedules/create", methods=["POST"])
@login_required
def schedule_create():
    """Create a new report schedule."""
    name = request.form.get("name", "").strip()
    tenant_id = request.form.get("tenant_id", "")
    audit_type = request.form.get("audit_type", "")
    frequency = request.form.get("frequency", "weekly")
    day_of_week = int(request.form.get("day_of_week", 1))
    hour = int(request.form.get("hour", 9))

    if not name:
        return jsonify({"ok": False, "error": "Schedule name is required."})
    if not tenant_id:
        return jsonify({"ok": False, "error": "Tenant is required."})
    if not audit_type:
        return jsonify({"ok": False, "error": "Audit type is required."})
    if frequency not in ("daily", "weekly", "biweekly", "monthly"):
        return jsonify({"ok": False, "error": "Invalid frequency."})

    # Collect audit-specific parameters
    audit_params = {}
    for key in ("site_url", "user_email", "target_user", "report_format",
                "investigation", "date_range_days"):
        val = request.form.get(key, "").strip()
        if val:
            audit_params[key] = val

    sid = create_schedule(
        name=name, tenant_id=tenant_id, audit_type=audit_type,
        audit_params=audit_params, frequency=frequency,
        day_of_week=day_of_week, hour=hour,
        created_by=current_user()
    )
    log_action(current_user(), "schedule_created",
               tenant_id=tenant_id, details=f"Schedule: {name}")
    return jsonify({"ok": True, "schedule_id": sid, "message": f"Schedule '{name}' created."})


@bp.route("/reports/schedules/<schedule_id>/toggle", methods=["POST"])
@login_required
def schedule_toggle(schedule_id):
    """Enable or disable a schedule."""
    s = get_schedule(schedule_id)
    if not s:
        return jsonify({"ok": False, "error": "Schedule not found."}), 404
    new_enabled = 0 if s["enabled"] else 1
    update_schedule(schedule_id, enabled=new_enabled)
    state = "enabled" if new_enabled else "disabled"
    log_action(current_user(), f"schedule_{state}", details=f"Schedule: {s['name']}")
    return jsonify({"ok": True, "enabled": new_enabled, "message": f"Schedule {state}."})


@bp.route("/reports/schedules/<schedule_id>/delete", methods=["POST"])
@login_required
def schedule_delete(schedule_id):
    """Delete a schedule."""
    s = get_schedule(schedule_id)
    if not s:
        return jsonify({"ok": False, "error": "Schedule not found."}), 404
    delete_schedule(schedule_id)
    log_action(current_user(), "schedule_deleted", details=f"Schedule: {s['name']}")
    return jsonify({"ok": True, "message": f"Schedule '{s['name']}' deleted."})


# ---------------------------------------------------------------------------
# Branding Configuration
# ---------------------------------------------------------------------------

@bp.route("/reports/branding/<tenant_id>", methods=["GET"])
@login_required
def get_branding(tenant_id):
    """Get branding configuration for a tenant."""
    branding = get_tenant_branding(tenant_id)
    defaults = get_default_branding()
    return jsonify({"ok": True, "branding": branding or defaults})


@bp.route("/reports/branding/<tenant_id>", methods=["POST"])
@login_required
def set_branding(tenant_id):
    """Set branding configuration for a tenant."""
    company_name = request.form.get("company_name", "").strip()
    accent_color = request.form.get("accent_color", "#0d6efd").strip()
    logo_path = request.form.get("logo_path", "").strip()

    upsert_tenant_branding(
        tenant_id,
        company_name=company_name,
        accent_color=accent_color,
        logo_path=logo_path,
    )
    log_action(current_user(), "branding_updated", tenant_id=tenant_id,
               details=f"Company: {company_name}")
    return jsonify({"ok": True, "message": "Branding updated."})


@bp.route("/reports/branding/default", methods=["POST"])
@login_required
def set_default_branding():
    """Set default branding in portal.json."""
    from helpers import read_portal_config, write_portal_config

    company_name = request.form.get("company_name", "").strip()
    accent_color = request.form.get("accent_color", "#0d6efd").strip()

    cfg = read_portal_config()
    cfg["branding"] = {
        "company_name": company_name or "Office Sentry",
        "accent_color": accent_color,
        "logo_path": "",
    }
    write_portal_config(cfg)
    log_action(current_user(), "default_branding_updated",
               details=f"Company: {company_name}")
    return jsonify({"ok": True, "message": "Default branding updated."})


# ---------------------------------------------------------------------------
# SMTP Configuration
# ---------------------------------------------------------------------------

@bp.route("/reports/smtp-config", methods=["GET"])
@login_required
def get_smtp_config():
    """Get SMTP configuration (passwords redacted)."""
    from helpers import read_portal_config
    cfg = read_portal_config()
    smtp = cfg.get("smtp", {})
    # Redact password
    if smtp.get("password_enc"):
        smtp["has_password"] = True
        del smtp["password_enc"]
    else:
        smtp["has_password"] = False
    return jsonify({"ok": True, "smtp": smtp})


@bp.route("/reports/smtp-config", methods=["POST"])
@login_required
def set_smtp_config():
    """Save SMTP configuration."""
    from helpers import read_portal_config, write_portal_config
    from database import encrypt_value
    from flask import current_app

    host = request.form.get("smtp_host", "").strip()
    port = request.form.get("smtp_port", "587").strip()
    username = request.form.get("smtp_username", "").strip()
    password = request.form.get("smtp_password", "").strip()
    sender = request.form.get("smtp_sender", "").strip()
    use_tls = request.form.get("smtp_use_tls", "true") == "true"

    cfg = read_portal_config()
    smtp_cfg = cfg.get("smtp", {})
    smtp_cfg["host"] = host
    smtp_cfg["port"] = int(port) if port.isdigit() else 587
    smtp_cfg["username"] = username
    smtp_cfg["sender"] = sender or username
    smtp_cfg["use_tls"] = use_tls

    # Only update password if a new one was provided
    if password:
        smtp_cfg["password_enc"] = encrypt_value(password, current_app.secret_key)

    cfg["smtp"] = smtp_cfg
    write_portal_config(cfg)
    log_action(current_user(), "smtp_configured", details=f"Host: {host}")
    return jsonify({"ok": True, "message": "SMTP configuration saved."})


@bp.route("/reports/smtp-test", methods=["POST"])
@login_required
def test_smtp():
    """Send a test email using the configured SMTP settings."""
    recipient = request.form.get("recipient", "").strip()
    if not recipient:
        return jsonify({"ok": False, "error": "Recipient email is required."})

    try:
        from helpers import read_portal_config
        from database import decrypt_value
        from flask import current_app
        import smtplib
        from email.mime.text import MIMEText

        cfg = read_portal_config()
        smtp = cfg.get("smtp", {})
        if not smtp.get("host"):
            return jsonify({"ok": False, "error": "SMTP not configured."})

        password = ""
        if smtp.get("password_enc"):
            password = decrypt_value(smtp["password_enc"], current_app.secret_key)

        msg = MIMEText("This is a test email from Office Sentry.")
        msg["Subject"] = "Office Sentry — SMTP Test"
        msg["From"] = smtp.get("sender", smtp.get("username", ""))
        msg["To"] = recipient

        server = smtplib.SMTP(smtp["host"], smtp.get("port", 587))
        if smtp.get("use_tls", True):
            server.starttls()
        if smtp.get("username") and password:
            server.login(smtp["username"], password)
        server.send_message(msg)
        server.quit()

        log_action(current_user(), "smtp_test", details=f"To: {recipient}")
        return jsonify({"ok": True, "message": f"Test email sent to {recipient}."})
    except Exception as exc:
        return jsonify({"ok": False, "error": f"SMTP test failed: {exc}"})
