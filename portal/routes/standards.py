"""
portal/routes/standards.py — Standards, BPA, Alerts, Tenant Groups, and Secure Score routes.
Inspired by CIPP's standards & drift management, best practice analyzer, and alert system.
"""

import json
import logging
import urllib.request
from datetime import datetime

from flask import (
    Blueprint, render_template, request, jsonify, abort, redirect, url_for, flash,
)

from database import (
    get_tenant, list_tenants, log_action,
    # Tenant groups
    create_tenant_group, get_tenant_group, list_tenant_groups,
    update_tenant_group, delete_tenant_group,
    add_tenant_to_group, remove_tenant_from_group,
    get_group_tenants, set_group_members, get_tenant_groups_for_tenant,
    # Standards
    create_standards_template, get_standards_template, list_standards_templates,
    update_standards_template, delete_standards_template,
    assign_standard_to_tenant, get_tenant_standards, remove_tenant_standard,
    # BPA
    save_bpa_result, get_bpa_results, get_bpa_summary, clear_bpa_results,
    # Alerts
    create_alert_rule, get_alert_rule, list_alert_rules,
    update_alert_rule, delete_alert_rule,
    create_alert, list_alerts, update_alert_status, get_alert_counts,
    # Secure scores
    save_secure_score, get_latest_secure_score, get_all_latest_secure_scores,
    touch_tenant_audit,
)
from helpers import (
    login_required, admin_required, current_user,
    tenant_pfx_path, build_ps_env, start_job, get_tenant_pfx_password,
    sanitize_text, SCRIPTS_DIR, PWSH,
)

log = logging.getLogger("portal")

bp = Blueprint("standards", __name__)


# =========================================================================
# Pre-defined BPA checks (built-in best practices)
# =========================================================================

BPA_CHECKS = [
    {
        "key": "unified_audit_log",
        "name": "Unified Audit Log",
        "category": "Security",
        "description": "Verifies that tenant-level audit logging is enabled. Essential for compliance, security monitoring, and incident response.",
        "expected": "Enabled",
        "impact": "Critical",
    },
    {
        "key": "security_defaults",
        "name": "Security Defaults",
        "category": "Identity",
        "description": "Determines whether secure defaults are active, enforcing MFA and blocking legacy authentication.",
        "expected": "Enabled (or Conditional Access)",
        "impact": "High",
    },
    {
        "key": "mfa_registration",
        "name": "MFA Registration Campaign",
        "category": "Identity",
        "description": "Assesses if MFA registration nudges are enabled to encourage multi-factor authentication adoption.",
        "expected": "Enabled",
        "impact": "High",
    },
    {
        "key": "password_expiry",
        "name": "Password Expiry Policy",
        "category": "Identity",
        "description": "Evaluates whether password policies enforce expiry. NIST recommends no forced expiry when MFA is in use.",
        "expected": "No forced expiry (with MFA)",
        "impact": "Medium",
    },
    {
        "key": "self_service_password_reset",
        "name": "Self-Service Password Reset",
        "category": "Identity",
        "description": "Checks if SSPR is enabled for all users to reduce helpdesk burden while maintaining security.",
        "expected": "Enabled for all users",
        "impact": "Medium",
    },
    {
        "key": "admin_mfa",
        "name": "Global Admin MFA",
        "category": "Identity",
        "description": "Verifies that all Global Administrators have MFA enabled. Unprotected admin accounts are a critical risk.",
        "expected": "All admins MFA-enabled",
        "impact": "Critical",
    },
    {
        "key": "oauth_app_consent",
        "name": "OAuth App Consent",
        "category": "Security",
        "description": "Ensures OAuth app consent policies restrict unauthorized data access and prevent consent phishing.",
        "expected": "Admin consent required",
        "impact": "High",
    },
    {
        "key": "legacy_auth_blocked",
        "name": "Legacy Authentication",
        "category": "Identity",
        "description": "Checks whether legacy authentication protocols are blocked. Legacy auth bypasses MFA.",
        "expected": "Blocked",
        "impact": "High",
    },
    {
        "key": "mailbox_auditing",
        "name": "Mailbox Auditing",
        "category": "Exchange",
        "description": "Verifies that mailbox auditing is enabled (default since January 2019).",
        "expected": "Enabled by default",
        "impact": "Medium",
    },
    {
        "key": "external_forwarding",
        "name": "External Email Forwarding",
        "category": "Exchange",
        "description": "Checks if auto-forwarding to external recipients is blocked to prevent data exfiltration.",
        "expected": "Blocked",
        "impact": "High",
    },
    {
        "key": "sharepoint_external_sharing",
        "name": "SharePoint External Sharing",
        "category": "SharePoint",
        "description": "Reviews SharePoint external sharing settings to ensure appropriate access controls.",
        "expected": "Restricted or disabled",
        "impact": "High",
    },
    {
        "key": "onedrive_external_sharing",
        "name": "OneDrive External Sharing",
        "category": "SharePoint",
        "description": "Reviews OneDrive external sharing settings for appropriate restrictions.",
        "expected": "Restricted",
        "impact": "Medium",
    },
    {
        "key": "secure_score",
        "name": "Microsoft Secure Score",
        "category": "Overall",
        "description": "Retrieves and evaluates the tenant's Microsoft Secure Score as a measure of overall security posture.",
        "expected": ">= 70%",
        "impact": "High",
    },
]

IMPACT_COLORS = {
    "Critical": "danger",
    "High": "warning",
    "Medium": "info",
    "Low": "primary",
}

# Colors available for tenant groups
GROUP_COLORS = [
    "primary", "success", "danger", "warning", "info", "secondary",
    "light", "dark",
]


# =========================================================================
# Tenant Groups
# =========================================================================

@bp.route("/groups")
@login_required
def groups_page():
    groups = list_tenant_groups()
    tenants = list_tenants()
    return render_template("tenant_groups.html",
                           groups=groups, tenants=tenants,
                           group_colors=GROUP_COLORS)


@bp.route("/groups/new", methods=["POST"])
@login_required
def group_create():
    name = sanitize_text(request.form.get("name", ""), max_length=100)
    description = sanitize_text(request.form.get("description", ""), max_length=500)
    color = request.form.get("color", "primary")
    tenant_ids = request.form.getlist("tenant_ids")

    if not name:
        return jsonify({"ok": False, "error": "Group name is required."})

    if color not in GROUP_COLORS:
        color = "primary"

    gid = create_tenant_group(name, description=description, color=color)

    for tid in tenant_ids:
        add_tenant_to_group(gid, tid)

    log_action(current_user(), "group_created",
               details=f"Group: {name}, Members: {len(tenant_ids)}")

    return jsonify({"ok": True, "group_id": gid,
                    "message": f"Group '{name}' created with {len(tenant_ids)} tenant(s)."})


@bp.route("/groups/<group_id>")
@login_required
def group_detail(group_id):
    group = get_tenant_group(group_id)
    if not group:
        abort(404)
    members = get_group_tenants(group_id)
    all_tenants = list_tenants()
    return render_template("tenant_group_detail.html",
                           group=group, members=members,
                           all_tenants=all_tenants,
                           group_colors=GROUP_COLORS)


@bp.route("/groups/<group_id>/update", methods=["POST"])
@login_required
def group_update(group_id):
    group = get_tenant_group(group_id)
    if not group:
        return jsonify({"ok": False, "error": "Group not found."}), 404

    name = sanitize_text(request.form.get("name", ""), max_length=100)
    description = sanitize_text(request.form.get("description", ""), max_length=500)
    color = request.form.get("color", group["color"])
    tenant_ids = request.form.getlist("tenant_ids")

    fields = {}
    if name:
        fields["name"] = name
    if description is not None:
        fields["description"] = description
    if color in GROUP_COLORS:
        fields["color"] = color

    if fields:
        update_tenant_group(group_id, **fields)

    set_group_members(group_id, tenant_ids)

    log_action(current_user(), "group_updated",
               details=f"Group: {name or group['name']}, Members: {len(tenant_ids)}")

    return jsonify({"ok": True, "message": "Group updated."})


@bp.route("/groups/<group_id>/delete", methods=["POST"])
@login_required
def group_delete(group_id):
    group = get_tenant_group(group_id)
    if not group:
        return jsonify({"ok": False, "error": "Group not found."}), 404

    delete_tenant_group(group_id)
    log_action(current_user(), "group_deleted",
               details=f"Group: {group['name']}")
    flash(f"Group '{group['name']}' deleted.", "success")
    return redirect(url_for("standards.groups_page"))


# =========================================================================
# Standards Templates
# =========================================================================

@bp.route("/standards")
@login_required
def standards_page():
    templates = list_standards_templates()
    tenants = list_tenants()
    return render_template("standards.html",
                           templates=templates, tenants=tenants,
                           bpa_checks=BPA_CHECKS,
                           impact_colors=IMPACT_COLORS)


@bp.route("/standards/templates/new", methods=["POST"])
@login_required
def template_create():
    name = sanitize_text(request.form.get("name", ""), max_length=200)
    description = sanitize_text(request.form.get("description", ""), max_length=1000)
    check_keys = request.form.getlist("checks")

    if not name:
        return jsonify({"ok": False, "error": "Template name is required."})

    valid_keys = {c["key"] for c in BPA_CHECKS}
    checks = [k for k in check_keys if k in valid_keys]

    if not checks:
        return jsonify({"ok": False, "error": "Select at least one check."})

    tid = create_standards_template(
        name, description=description, checks=checks,
        created_by=current_user()
    )
    log_action(current_user(), "template_created",
               details=f"Template: {name}, Checks: {len(checks)}")

    return jsonify({"ok": True, "template_id": tid,
                    "message": f"Template '{name}' created with {len(checks)} check(s)."})


@bp.route("/standards/templates/<template_id>", methods=["GET"])
@login_required
def template_detail(template_id):
    template = get_standards_template(template_id)
    if not template:
        return jsonify({"ok": False, "error": "Template not found."}), 404

    check_details = []
    for check in BPA_CHECKS:
        if check["key"] in template["checks"]:
            check_details.append(check)

    return jsonify({"ok": True, "template": template, "check_details": check_details})


@bp.route("/standards/templates/<template_id>/update", methods=["POST"])
@login_required
def template_update(template_id):
    template = get_standards_template(template_id)
    if not template:
        return jsonify({"ok": False, "error": "Template not found."}), 404

    name = sanitize_text(request.form.get("name", ""), max_length=200)
    description = sanitize_text(request.form.get("description", ""), max_length=1000)
    check_keys = request.form.getlist("checks")

    valid_keys = {c["key"] for c in BPA_CHECKS}
    checks = [k for k in check_keys if k in valid_keys]

    fields = {}
    if name:
        fields["name"] = name
    if description is not None:
        fields["description"] = description
    if checks:
        fields["checks"] = checks

    if fields:
        update_standards_template(template_id, **fields)
        log_action(current_user(), "template_updated",
                   details=f"Template: {name or template['name']}")

    return jsonify({"ok": True, "message": "Template updated."})


@bp.route("/standards/templates/<template_id>/delete", methods=["POST"])
@login_required
def template_delete(template_id):
    template = get_standards_template(template_id)
    if not template:
        return jsonify({"ok": False, "error": "Template not found."}), 404

    delete_standards_template(template_id)
    log_action(current_user(), "template_deleted",
               details=f"Template: {template['name']}")

    return jsonify({"ok": True, "message": f"Template '{template['name']}' deleted."})


@bp.route("/standards/apply", methods=["POST"])
@login_required
def standards_apply():
    """Apply a standards template to one or more tenants."""
    template_id = request.form.get("template_id", "")
    tenant_ids = request.form.getlist("tenant_ids")
    action = request.form.get("action", "report")

    if not template_id:
        return jsonify({"ok": False, "error": "Template is required."})
    if not tenant_ids:
        return jsonify({"ok": False, "error": "Select at least one tenant."})
    if action not in ("report", "alert"):
        action = "report"

    template = get_standards_template(template_id)
    if not template:
        return jsonify({"ok": False, "error": "Template not found."})

    applied = 0
    for tid in tenant_ids:
        if get_tenant(tid):
            assign_standard_to_tenant(tid, template_id, action=action,
                                      applied_by=current_user())
            applied += 1

    log_action(current_user(), "standards_applied",
               details=f"Template: {template['name']}, Tenants: {applied}, Action: {action}")

    return jsonify({
        "ok": True,
        "message": f"Applied '{template['name']}' to {applied} tenant(s) ({action} mode).",
    })


# =========================================================================
# Best Practice Analyzer (BPA)
# =========================================================================

@bp.route("/bpa")
@login_required
def bpa_page():
    tenants = list_tenants()
    summary = get_bpa_summary()
    all_results = get_bpa_results()
    scores = get_all_latest_secure_scores()
    groups = list_tenant_groups()
    templates = list_standards_templates()

    # Build a matrix of tenant -> check results
    tenant_results = {}
    for r in all_results:
        tid = r["tenant_id"]
        if tid not in tenant_results:
            tenant_results[tid] = {}
        tenant_results[tid][r["check_key"]] = r

    # Score map for quick lookup
    score_map = {s["tenant_id"]: s for s in scores}

    return render_template("bpa.html",
                           tenants=tenants,
                           summary=summary,
                           bpa_checks=BPA_CHECKS,
                           tenant_results=tenant_results,
                           score_map=score_map,
                           impact_colors=IMPACT_COLORS,
                           groups=groups,
                           templates=templates)


@bp.route("/bpa/scan", methods=["POST"])
@login_required
def bpa_scan():
    """Run BPA against one or more tenants."""
    tenant_ids = request.form.getlist("tenant_ids")
    if not tenant_ids:
        return jsonify({"ok": False, "error": "Select at least one tenant."})

    job_ids = []
    for tid in tenant_ids:
        t = get_tenant(tid)
        if not t or not t["client_id"]:
            continue

        pfx_pass = get_tenant_pfx_password(t)
        if not pfx_pass:
            continue

        tenant_val = t["tenant_id"] or t["domain"]
        pfx_path = str(tenant_pfx_path(tid))
        env = build_ps_env(pfx_pass)

        cmd = [
            PWSH, "-NoProfile", "-ExecutionPolicy", "Bypass",
            str(SCRIPTS_DIR / "Get-TenantBPA.ps1"),
            "-Tenant", tenant_val,
            "-ClientId", t["client_id"],
            "-CertificatePath", pfx_path,
        ]

        label = f"BPA Scan — {t['name']}"
        jid = start_job(label, "bpa", cmd, tenant_id=tid, env=env)
        touch_tenant_audit(tid)
        job_ids.append(jid)

    log_action(current_user(), "bpa_scan",
               details=f"Tenants: {len(job_ids)}")

    return jsonify({
        "ok": True,
        "job_ids": job_ids,
        "message": f"Started BPA scan for {len(job_ids)} tenant(s).",
    })


@bp.route("/bpa/results/<tenant_id>")
@login_required
def bpa_tenant_results(tenant_id):
    """Get BPA results for a specific tenant."""
    t = get_tenant(tenant_id)
    if not t:
        return jsonify({"ok": False, "error": "Tenant not found."}), 404

    results = get_bpa_results(tenant_id=tenant_id)
    score = get_latest_secure_score(tenant_id)

    return jsonify({
        "ok": True,
        "tenant": {"id": t["id"], "name": t["name"], "domain": t["domain"]},
        "results": results,
        "secure_score": score,
        "checks": BPA_CHECKS,
    })


@bp.route("/bpa/import-results", methods=["POST"])
@login_required
def bpa_import_results():
    """Import BPA results from a completed job (called after PS script finishes)."""
    tenant_id = request.form.get("tenant_id", "")
    results_json = request.form.get("results", "")

    if not tenant_id or not results_json:
        return jsonify({"ok": False, "error": "tenant_id and results are required."})

    try:
        data = json.loads(results_json)
    except json.JSONDecodeError:
        return jsonify({"ok": False, "error": "Invalid JSON."})

    if not data.get("ok"):
        return jsonify({"ok": False, "error": data.get("error", "BPA scan failed.")})

    clear_bpa_results(tenant_id)
    count = 0
    for check in data.get("checks", []):
        save_bpa_result(
            tenant_id=tenant_id,
            check_key=check.get("check_key", ""),
            check_name=check.get("check_name", ""),
            status=check.get("status", "unknown"),
            category=check.get("category", ""),
            detail=check.get("detail", ""),
            expected=check.get("expected", ""),
            actual=check.get("actual", ""),
            impact=check.get("impact", "Medium"),
        )
        count += 1

    return jsonify({"ok": True, "message": f"Imported {count} BPA results."})


# =========================================================================
# Secure Score
# =========================================================================

@bp.route("/secure-scores")
@login_required
def secure_scores_page():
    tenants = list_tenants()
    scores = get_all_latest_secure_scores()
    return render_template("secure_scores.html",
                           tenants=tenants, scores=scores)


@bp.route("/secure-scores/fetch", methods=["POST"])
@login_required
def secure_score_fetch():
    """Fetch Secure Score for one or more tenants."""
    tenant_ids = request.form.getlist("tenant_ids")
    if not tenant_ids:
        return jsonify({"ok": False, "error": "Select at least one tenant."})

    job_ids = []
    for tid in tenant_ids:
        t = get_tenant(tid)
        if not t or not t["client_id"]:
            continue

        pfx_pass = get_tenant_pfx_password(t)
        if not pfx_pass:
            continue

        tenant_val = t["tenant_id"] or t["domain"]
        pfx_path = str(tenant_pfx_path(tid))
        env = build_ps_env(pfx_pass)

        cmd = [
            PWSH, "-NoProfile", "-ExecutionPolicy", "Bypass",
            str(SCRIPTS_DIR / "Get-SecureScore.ps1"),
            "-Tenant", tenant_val,
            "-ClientId", t["client_id"],
            "-CertificatePath", pfx_path,
        ]

        label = f"Secure Score — {t['name']}"
        jid = start_job(label, "secure_score", cmd, tenant_id=tid, env=env)
        job_ids.append(jid)

    log_action(current_user(), "secure_score_fetch",
               details=f"Tenants: {len(job_ids)}")

    return jsonify({
        "ok": True,
        "job_ids": job_ids,
        "message": f"Fetching Secure Score for {len(job_ids)} tenant(s).",
    })


# =========================================================================
# Alert Configuration
# =========================================================================

@bp.route("/alerts")
@login_required
def alerts_page():
    rules = list_alert_rules()
    recent_alerts = list_alerts(limit=50)
    counts = get_alert_counts()
    tenants = list_tenants()
    return render_template("alerts.html",
                           rules=rules, recent_alerts=recent_alerts,
                           counts=counts, tenants=tenants)


@bp.route("/alerts/rules/new", methods=["POST"])
@login_required
def alert_rule_create():
    name = sanitize_text(request.form.get("name", ""), max_length=200)
    description = sanitize_text(request.form.get("description", ""), max_length=1000)
    alert_type = request.form.get("alert_type", "audit_log")
    tenant_id = request.form.get("tenant_id", "") or None
    webhook_url = request.form.get("webhook_url", "").strip()
    email_to = request.form.get("email_to", "").strip()
    severity = request.form.get("severity", "info")

    if not name:
        return jsonify({"ok": False, "error": "Alert name is required."})

    if alert_type not in ("audit_log", "bpa_fail", "secure_score_drop", "job_failure"):
        return jsonify({"ok": False, "error": "Invalid alert type."})

    # Build conditions
    conditions = {
        "type": alert_type,
        "severity": severity,
    }
    if alert_type == "audit_log":
        conditions["action_pattern"] = request.form.get("action_pattern", "")
    elif alert_type == "bpa_fail":
        conditions["check_keys"] = request.form.getlist("check_keys")
    elif alert_type == "secure_score_drop":
        threshold = request.form.get("threshold", "5")
        try:
            conditions["threshold"] = float(threshold)
        except ValueError:
            conditions["threshold"] = 5.0

    # Build actions
    actions = []
    if webhook_url:
        actions.append({"type": "webhook", "url": webhook_url})
    if email_to:
        actions.append({"type": "email", "to": email_to})
    if not actions:
        actions.append({"type": "log"})

    rid = create_alert_rule(
        name=name, alert_type=alert_type, description=description,
        conditions=conditions, actions=actions,
        tenant_id=tenant_id, created_by=current_user()
    )

    log_action(current_user(), "alert_rule_created",
               details=f"Rule: {name}, Type: {alert_type}")

    return jsonify({"ok": True, "rule_id": rid,
                    "message": f"Alert rule '{name}' created."})


@bp.route("/alerts/rules/<rule_id>/toggle", methods=["POST"])
@login_required
def alert_rule_toggle(rule_id):
    rule = get_alert_rule(rule_id)
    if not rule:
        return jsonify({"ok": False, "error": "Rule not found."}), 404

    new_state = 0 if rule["enabled"] else 1
    update_alert_rule(rule_id, enabled=new_state)

    state_str = "enabled" if new_state else "disabled"
    log_action(current_user(), "alert_rule_toggled",
               details=f"Rule: {rule['name']} - {state_str}")

    return jsonify({"ok": True, "enabled": new_state,
                    "message": f"Rule '{rule['name']}' {state_str}."})


@bp.route("/alerts/rules/<rule_id>/delete", methods=["POST"])
@login_required
def alert_rule_delete(rule_id):
    rule = get_alert_rule(rule_id)
    if not rule:
        return jsonify({"ok": False, "error": "Rule not found."}), 404

    delete_alert_rule(rule_id)
    log_action(current_user(), "alert_rule_deleted",
               details=f"Rule: {rule['name']}")

    return jsonify({"ok": True, "message": f"Rule '{rule['name']}' deleted."})


@bp.route("/alerts/<alert_id>/resolve", methods=["POST"])
@login_required
def alert_resolve(alert_id):
    update_alert_status(alert_id, "resolved")
    return jsonify({"ok": True, "message": "Alert resolved."})


@bp.route("/alerts/<alert_id>/acknowledge", methods=["POST"])
@login_required
def alert_acknowledge(alert_id):
    update_alert_status(alert_id, "acknowledged")
    return jsonify({"ok": True, "message": "Alert acknowledged."})


@bp.route("/api/alerts/counts")
@login_required
def api_alert_counts():
    """Return alert counts for the navbar badge."""
    counts = get_alert_counts()
    return jsonify(counts)


# =========================================================================
# Alert Webhook Delivery
# =========================================================================

def deliver_webhook(url: str, payload: dict) -> bool:
    """Send an alert payload to a webhook URL."""
    try:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url, data=data,
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status < 400
    except Exception as exc:
        log.warning("Webhook delivery failed to %s: %s", url, exc)
        return False


def process_alert_rules(action: str, tenant_id: str = None,
                        tenant_name: str = "", details: str = ""):
    """Check all enabled alert rules against an event and fire matching alerts."""
    rules = list_alert_rules(tenant_id=tenant_id)
    for rule in rules:
        if not rule["enabled"]:
            continue
        conditions = rule["conditions"]
        if conditions.get("type") != "audit_log":
            continue

        pattern = conditions.get("action_pattern", "")
        if pattern and pattern not in action:
            continue

        # Match — create alert and deliver
        severity = conditions.get("severity", "info")
        aid = create_alert(
            rule_id=rule["id"],
            title=f"Alert: {action}",
            detail=details,
            severity=severity,
            tenant_id=tenant_id,
            tenant_name=tenant_name,
        )

        # Deliver via configured actions
        delivered = []
        for act in rule.get("actions", []):
            if act["type"] == "webhook":
                success = deliver_webhook(act["url"], {
                    "alert_id": aid,
                    "rule": rule["name"],
                    "action": action,
                    "tenant": tenant_name,
                    "detail": details,
                    "severity": severity,
                    "timestamp": datetime.now().isoformat(),
                })
                if success:
                    delivered.append("webhook")
            elif act["type"] == "log":
                delivered.append("log")

        if delivered:
            update_alert_status(aid, "delivered")
