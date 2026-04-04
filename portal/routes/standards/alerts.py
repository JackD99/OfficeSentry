"""Alert configuration, management, and webhook delivery routes."""

import json
import logging
import urllib.request
from datetime import datetime

from flask import render_template, request, jsonify

from helpers import login_required, current_user, sanitize_text
from database import (
    get_tenant, list_tenants, log_action,
    create_alert_rule, get_alert_rule, list_alert_rules,
    update_alert_rule, delete_alert_rule,
    create_alert, list_alerts, update_alert_status, get_alert_counts,
)

from routes.standards import bp

log = logging.getLogger("portal")


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


# ---------------------------------------------------------------------------
# Alert Webhook Delivery
# ---------------------------------------------------------------------------

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
