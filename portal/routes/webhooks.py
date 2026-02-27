"""
portal/routes/webhooks.py — Webhook management routes.
"""

import json
import secrets
import logging

from flask import Blueprint, render_template, request, jsonify

from database import (
    create_webhook, get_webhook, list_webhooks, update_webhook,
    delete_webhook, toggle_webhook, list_webhook_deliveries,
    log_action,
)
from helpers import admin_required, current_user

log = logging.getLogger("portal")

bp = Blueprint("webhooks", __name__)

WEBHOOK_EVENTS = [
    "job.completed",
    "job.failed",
    "alert.triggered",
    "bpa.drift_detected",
    "report.generated",
]


@bp.route("/webhooks")
@admin_required
def webhooks_page():
    hooks = list_webhooks()
    return render_template("webhooks.html", webhooks=hooks,
                           available_events=WEBHOOK_EVENTS)


@bp.route("/webhooks/create", methods=["POST"])
@admin_required
def webhook_create():
    name = request.form.get("name", "").strip()
    url = request.form.get("url", "").strip()
    events = request.form.getlist("events")

    if not name:
        return jsonify({"ok": False, "error": "Webhook name is required."})
    if not url or not url.startswith("http"):
        return jsonify({"ok": False, "error": "A valid URL is required."})

    secret = secrets.token_hex(32)
    wh_id = create_webhook(
        name=name, url=url, secret=secret,
        events=json.dumps(events),
        created_by=current_user(),
    )
    log_action(current_user(), "webhook_created",
               details=f"Webhook: {name} ({url})")
    return jsonify({
        "ok": True,
        "message": f"Webhook '{name}' created.",
        "secret": secret,
        "id": wh_id,
    })


@bp.route("/webhooks/<webhook_id>/update", methods=["POST"])
@admin_required
def webhook_update(webhook_id):
    wh = get_webhook(webhook_id)
    if not wh:
        return jsonify({"ok": False, "error": "Webhook not found."}), 404

    name = request.form.get("name", "").strip()
    url = request.form.get("url", "").strip()
    events = request.form.getlist("events")

    updates = {}
    if name:
        updates["name"] = name
    if url:
        updates["url"] = url
    if events is not None:
        updates["events"] = json.dumps(events)

    update_webhook(webhook_id, **updates)
    log_action(current_user(), "webhook_updated",
               details=f"Webhook: {webhook_id}")
    return jsonify({"ok": True, "message": "Webhook updated."})


@bp.route("/webhooks/<webhook_id>/delete", methods=["POST"])
@admin_required
def webhook_delete(webhook_id):
    if not delete_webhook(webhook_id):
        return jsonify({"ok": False, "error": "Webhook not found."}), 404
    log_action(current_user(), "webhook_deleted",
               details=f"Webhook: {webhook_id}")
    return jsonify({"ok": True, "message": "Webhook deleted."})


@bp.route("/webhooks/<webhook_id>/toggle", methods=["POST"])
@admin_required
def webhook_toggle(webhook_id):
    result = toggle_webhook(webhook_id)
    if result is None:
        return jsonify({"ok": False, "error": "Webhook not found."}), 404
    state = "enabled" if result else "disabled"
    log_action(current_user(), f"webhook_{state}",
               details=f"Webhook: {webhook_id}")
    return jsonify({"ok": True, "message": f"Webhook {state}.", "enabled": result})


@bp.route("/webhooks/<webhook_id>/test", methods=["POST"])
@admin_required
def webhook_test(webhook_id):
    """Send a test event to verify the webhook endpoint."""
    wh = get_webhook(webhook_id)
    if not wh:
        return jsonify({"ok": False, "error": "Webhook not found."}), 404

    payload = {
        "event": "test",
        "message": "This is a test webhook delivery from the M365 Audit Portal.",
        "webhook_id": webhook_id,
    }
    try:
        from tasks import deliver_webhook_task
        deliver_webhook_task.delay(webhook_id, "test", payload)
        return jsonify({"ok": True, "message": "Test event queued for delivery."})
    except Exception:
        # Celery not available — try sync
        from tasks import _deliver_sync
        success = _deliver_sync(wh, "test", payload)
        if success:
            return jsonify({"ok": True, "message": "Test event delivered."})
        return jsonify({"ok": False, "error": "Delivery failed. Check the URL."})


@bp.route("/webhooks/<webhook_id>/deliveries")
@admin_required
def webhook_deliveries(webhook_id):
    wh = get_webhook(webhook_id)
    if not wh:
        return jsonify({"ok": False, "error": "Webhook not found."}), 404
    deliveries = list_webhook_deliveries(webhook_id)
    return jsonify({"ok": True, "deliveries": deliveries})
