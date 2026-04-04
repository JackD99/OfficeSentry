"""
portal/routes/service_health.py — M365 service health monitoring routes.
Provides service status, incident tracking, advisory monitoring, and
planned maintenance visibility across tenants.
Inspired by Microsoft 365 admin center Service Health and CIPP's health views.
"""

import json
import logging

from flask import (
    Blueprint, render_template, request, jsonify, abort,
)

from database import (
    get_tenant, list_tenants, log_action,
    save_cached_data, get_cached_data,
)
from helpers import (
    login_required, current_user,
    tenant_pfx_path, start_python_job, get_tenant_pfx_password,
)

log = logging.getLogger("portal")

bp = Blueprint("service_health", __name__)


# =========================================================================
# Service Status Dashboard
# =========================================================================

@bp.route("/service-health")
@login_required
def service_health_page():
    tenants = list_tenants()
    ready_tenants = [t for t in tenants if t["client_id"]]
    return render_template("service_health.html", tenants=ready_tenants)


@bp.route("/tenants/<tenant_id>/service-health/fetch", methods=["POST"])
@login_required
def service_health_fetch(tenant_id):
    """Fetch current service health status for a tenant."""
    t = get_tenant(tenant_id)
    if not t or not t["client_id"]:
        return jsonify({"ok": False, "error": "Tenant not configured."})

    pfx_pass = get_tenant_pfx_password(t)
    if not pfx_pass:
        return jsonify({"ok": False, "error": "PFX password not configured."})

    pfx_path = str(tenant_pfx_path(tenant_id))

    from graph_jobs import fetch_service_health
    jid = start_python_job(
        f"Service Health — {t['name']}", "service_health",
        fetch_service_health,
        args=(dict(t), pfx_path, pfx_pass, "status"),
        tenant_id=tenant_id,
    )
    log_action(current_user(), "service_health_fetch",
               tenant_id=tenant_id, tenant_name=t["name"])
    return jsonify({"ok": True, "job_id": jid})


@bp.route("/service-health/scan", methods=["POST"])
@login_required
def service_health_scan():
    """Fetch service health across multiple tenants."""
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

        pfx_path = str(tenant_pfx_path(tid))

        from graph_jobs import fetch_service_health
        jid = start_python_job(
            f"Service Health — {t['name']}", "service_health",
            fetch_service_health,
            args=(dict(t), pfx_path, pfx_pass, "status"),
            tenant_id=tid,
        )
        job_ids.append(jid)

    log_action(current_user(), "service_health_scan",
               details=f"Tenants: {len(job_ids)}")
    return jsonify({
        "ok": True, "job_ids": job_ids,
        "message": f"Started health check for {len(job_ids)} tenant(s).",
    })


# =========================================================================
# Service Incidents
# =========================================================================

@bp.route("/service-health/incidents")
@login_required
def incidents_list_page():
    tenants = list_tenants()
    ready_tenants = [t for t in tenants if t["client_id"]]
    return render_template("service_incidents.html", tenants=ready_tenants)


@bp.route("/tenants/<tenant_id>/service-health/incidents/fetch", methods=["POST"])
@login_required
def incidents_fetch(tenant_id):
    """Fetch active service incidents for a tenant."""
    t = get_tenant(tenant_id)
    if not t or not t["client_id"]:
        return jsonify({"ok": False, "error": "Tenant not configured."})

    pfx_pass = get_tenant_pfx_password(t)
    if not pfx_pass:
        return jsonify({"ok": False, "error": "PFX password not configured."})

    pfx_path = str(tenant_pfx_path(tenant_id))
    include_resolved = request.form.get("include_resolved", "false") == "true"

    from graph_jobs import fetch_service_health
    jid = start_python_job(
        f"Service Incidents — {t['name']}", "service_health",
        fetch_service_health,
        args=(dict(t), pfx_path, pfx_pass, "incidents", include_resolved),
        tenant_id=tenant_id,
    )
    log_action(current_user(), "service_incidents_fetch",
               tenant_id=tenant_id, tenant_name=t["name"])
    return jsonify({"ok": True, "job_id": jid})


# =========================================================================
# Message Center (Advisories & Planned Changes)
# =========================================================================

@bp.route("/service-health/message-center")
@login_required
def message_center_page():
    tenants = list_tenants()
    ready_tenants = [t for t in tenants if t["client_id"]]
    return render_template("service_message_center.html", tenants=ready_tenants)


@bp.route("/tenants/<tenant_id>/message-center/fetch", methods=["POST"])
@login_required
def message_center_fetch(tenant_id):
    """Fetch Message Center posts for a tenant."""
    t = get_tenant(tenant_id)
    if not t or not t["client_id"]:
        return jsonify({"ok": False, "error": "Tenant not configured."})

    pfx_pass = get_tenant_pfx_password(t)
    if not pfx_pass:
        return jsonify({"ok": False, "error": "PFX password not configured."})

    pfx_path = str(tenant_pfx_path(tenant_id))
    msg_type = request.form.get("message_type", "all")

    from graph_jobs import fetch_service_health
    jid = start_python_job(
        f"Message Center — {t['name']}", "service_health",
        fetch_service_health,
        args=(dict(t), pfx_path, pfx_pass, "messageCenter"),
        kwargs={"message_type": msg_type if msg_type != "all" else ""},
        tenant_id=tenant_id,
    )
    log_action(current_user(), "message_center_fetch",
               tenant_id=tenant_id, tenant_name=t["name"],
               details=f"Type: {msg_type}")
    return jsonify({"ok": True, "job_id": jid})


# =========================================================================
# Planned Maintenance
# =========================================================================

@bp.route("/service-health/maintenance")
@login_required
def maintenance_page():
    tenants = list_tenants()
    ready_tenants = [t for t in tenants if t["client_id"]]
    return render_template("service_maintenance.html", tenants=ready_tenants)


@bp.route("/tenants/<tenant_id>/maintenance/fetch", methods=["POST"])
@login_required
def maintenance_fetch(tenant_id):
    """Fetch planned maintenance windows for a tenant."""
    t = get_tenant(tenant_id)
    if not t or not t["client_id"]:
        return jsonify({"ok": False, "error": "Tenant not configured."})

    pfx_pass = get_tenant_pfx_password(t)
    if not pfx_pass:
        return jsonify({"ok": False, "error": "PFX password not configured."})

    pfx_path = str(tenant_pfx_path(tenant_id))

    from graph_jobs import fetch_service_health
    jid = start_python_job(
        f"Planned Maintenance — {t['name']}", "service_health",
        fetch_service_health,
        args=(dict(t), pfx_path, pfx_pass, "maintenance"),
        tenant_id=tenant_id,
    )
    log_action(current_user(), "maintenance_fetch",
               tenant_id=tenant_id, tenant_name=t["name"])
    return jsonify({"ok": True, "job_id": jid})


# =========================================================================
# Cross-tenant Health Overview
# =========================================================================

@bp.route("/service-health/overview")
@login_required
def health_overview_page():
    tenants = list_tenants()
    ready_tenants = [t for t in tenants if t["client_id"]]
    return render_template("service_health_overview.html", tenants=ready_tenants)


@bp.route("/service-health/overview/scan", methods=["POST"])
@login_required
def health_overview_scan():
    """Run health overview scan across multiple tenants."""
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

        pfx_path = str(tenant_pfx_path(tid))

        from graph_jobs import fetch_service_health
        jid = start_python_job(
            f"Health Overview — {t['name']}", "service_health",
            fetch_service_health,
            args=(dict(t), pfx_path, pfx_pass, "overview"),
            tenant_id=tid,
        )
        job_ids.append(jid)

    log_action(current_user(), "health_overview_scan",
               details=f"Tenants: {len(job_ids)}")
    return jsonify({
        "ok": True, "job_ids": job_ids,
        "message": f"Started health overview for {len(job_ids)} tenant(s).",
    })
