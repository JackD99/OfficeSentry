"""
portal/routes/devices.py — Intune device management routes.
Provides device inventory, compliance status, Autopilot profiles,
configuration policies, and device actions.
Inspired by Microsoft Intune admin center and CIPP's device management.
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
    tenant_pfx_path, tenant_output_dir,
    build_ps_env, start_job, get_tenant_pfx_password,
    sanitize_text,
    SCRIPTS_DIR, PWSH,
)

log = logging.getLogger("portal")

bp = Blueprint("devices", __name__)


# =========================================================================
# Device Inventory
# =========================================================================

@bp.route("/devices/inventory")
@login_required
def device_inventory_page():
    tenants = list_tenants()
    ready_tenants = [t for t in tenants if t["client_id"]]
    return render_template("devices_inventory.html", tenants=ready_tenants)


@bp.route("/tenants/<tenant_id>/devices/fetch", methods=["POST"])
@login_required
def devices_fetch(tenant_id):
    """Fetch managed device inventory from Intune."""
    t = get_tenant(tenant_id)
    if not t or not t["client_id"]:
        return jsonify({"ok": False, "error": "Tenant not configured."})

    pfx_pass = get_tenant_pfx_password(t)
    if not pfx_pass:
        return jsonify({"ok": False, "error": "PFX password not configured."})

    tenant_val = t["tenant_id"] or t["domain"]
    pfx_path = str(tenant_pfx_path(tenant_id))
    out_dir = str(tenant_output_dir(tenant_id))
    env = build_ps_env(pfx_pass)

    platform_filter = request.form.get("platform", "all")
    compliance_filter = request.form.get("compliance", "all")

    cmd = [
        PWSH, "-NoProfile", "-ExecutionPolicy", "Bypass",
        str(SCRIPTS_DIR / "Get-IntuneDevices.ps1"),
        "-Tenant", tenant_val,
        "-ClientId", t["client_id"],
        "-CertificatePath", pfx_path,
        "-OutputFolder", out_dir,
        "-ReportType", "inventory",
    ]
    if platform_filter != "all":
        cmd += ["-Platform", platform_filter]
    if compliance_filter != "all":
        cmd += ["-ComplianceFilter", compliance_filter]

    jid = start_job(f"Device Inventory — {t['name']}", "devices", cmd,
                    tenant_id=tenant_id, env=env)
    log_action(current_user(), "devices_fetch",
               tenant_id=tenant_id, tenant_name=t["name"],
               details=f"Platform: {platform_filter}, Compliance: {compliance_filter}")
    return jsonify({"ok": True, "job_id": jid})


# =========================================================================
# Device Compliance
# =========================================================================

@bp.route("/devices/compliance")
@login_required
def device_compliance_page():
    tenants = list_tenants()
    ready_tenants = [t for t in tenants if t["client_id"]]
    return render_template("devices_compliance.html", tenants=ready_tenants)


@bp.route("/tenants/<tenant_id>/devices/compliance/fetch", methods=["POST"])
@login_required
def device_compliance_fetch(tenant_id):
    """Fetch device compliance policies and status."""
    t = get_tenant(tenant_id)
    if not t or not t["client_id"]:
        return jsonify({"ok": False, "error": "Tenant not configured."})

    pfx_pass = get_tenant_pfx_password(t)
    if not pfx_pass:
        return jsonify({"ok": False, "error": "PFX password not configured."})

    tenant_val = t["tenant_id"] or t["domain"]
    pfx_path = str(tenant_pfx_path(tenant_id))
    out_dir = str(tenant_output_dir(tenant_id))
    env = build_ps_env(pfx_pass)

    cmd = [
        PWSH, "-NoProfile", "-ExecutionPolicy", "Bypass",
        str(SCRIPTS_DIR / "Get-IntuneDevices.ps1"),
        "-Tenant", tenant_val,
        "-ClientId", t["client_id"],
        "-CertificatePath", pfx_path,
        "-OutputFolder", out_dir,
        "-ReportType", "compliance",
    ]

    jid = start_job(f"Device Compliance — {t['name']}", "devices", cmd,
                    tenant_id=tenant_id, env=env)
    log_action(current_user(), "device_compliance_fetch",
               tenant_id=tenant_id, tenant_name=t["name"])
    return jsonify({"ok": True, "job_id": jid})


@bp.route("/devices/compliance/scan", methods=["POST"])
@login_required
def device_compliance_scan():
    """Run compliance check across multiple tenants."""
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
        out_dir = str(tenant_output_dir(tid))
        env = build_ps_env(pfx_pass)

        cmd = [
            PWSH, "-NoProfile", "-ExecutionPolicy", "Bypass",
            str(SCRIPTS_DIR / "Get-IntuneDevices.ps1"),
            "-Tenant", tenant_val,
            "-ClientId", t["client_id"],
            "-CertificatePath", pfx_path,
            "-OutputFolder", out_dir,
            "-ReportType", "compliance",
        ]

        jid = start_job(f"Device Compliance — {t['name']}", "devices",
                        cmd, tenant_id=tid, env=env)
        job_ids.append(jid)

    log_action(current_user(), "device_compliance_scan",
               details=f"Tenants: {len(job_ids)}")
    return jsonify({
        "ok": True, "job_ids": job_ids,
        "message": f"Started compliance check for {len(job_ids)} tenant(s).",
    })


# =========================================================================
# Autopilot Devices
# =========================================================================

@bp.route("/devices/autopilot")
@login_required
def autopilot_page():
    tenants = list_tenants()
    ready_tenants = [t for t in tenants if t["client_id"]]
    return render_template("devices_autopilot.html", tenants=ready_tenants)


@bp.route("/tenants/<tenant_id>/autopilot/fetch", methods=["POST"])
@login_required
def autopilot_fetch(tenant_id):
    """Fetch Autopilot device registrations and deployment profiles."""
    t = get_tenant(tenant_id)
    if not t or not t["client_id"]:
        return jsonify({"ok": False, "error": "Tenant not configured."})

    pfx_pass = get_tenant_pfx_password(t)
    if not pfx_pass:
        return jsonify({"ok": False, "error": "PFX password not configured."})

    tenant_val = t["tenant_id"] or t["domain"]
    pfx_path = str(tenant_pfx_path(tenant_id))
    out_dir = str(tenant_output_dir(tenant_id))
    env = build_ps_env(pfx_pass)

    cmd = [
        PWSH, "-NoProfile", "-ExecutionPolicy", "Bypass",
        str(SCRIPTS_DIR / "Get-IntuneDevices.ps1"),
        "-Tenant", tenant_val,
        "-ClientId", t["client_id"],
        "-CertificatePath", pfx_path,
        "-OutputFolder", out_dir,
        "-ReportType", "autopilot",
    ]

    jid = start_job(f"Autopilot Devices — {t['name']}", "devices", cmd,
                    tenant_id=tenant_id, env=env)
    log_action(current_user(), "autopilot_fetch",
               tenant_id=tenant_id, tenant_name=t["name"])
    return jsonify({"ok": True, "job_id": jid})


# =========================================================================
# Configuration Policies
# =========================================================================

@bp.route("/tenants/<tenant_id>/device-policies")
@login_required
def device_policies_page(tenant_id):
    t = get_tenant(tenant_id)
    if not t:
        abort(404)
    cached = get_cached_data(tenant_id, "device_policies")
    return render_template("devices_policies.html", tenant=t, cached=cached)


@bp.route("/tenants/<tenant_id>/device-policies/fetch", methods=["POST"])
@login_required
def device_policies_fetch(tenant_id):
    """Fetch Intune configuration policies for a tenant."""
    t = get_tenant(tenant_id)
    if not t or not t["client_id"]:
        return jsonify({"ok": False, "error": "Tenant not configured."})

    pfx_pass = get_tenant_pfx_password(t)
    if not pfx_pass:
        return jsonify({"ok": False, "error": "PFX password not configured."})

    policy_type = request.form.get("policy_type", "all")
    if policy_type not in ("all", "configuration", "compliance", "windowsUpdate",
                           "endpoint", "appProtection"):
        return jsonify({"ok": False, "error": "Invalid policy type."})

    tenant_val = t["tenant_id"] or t["domain"]
    pfx_path = str(tenant_pfx_path(tenant_id))
    env = build_ps_env(pfx_pass)

    cmd = [
        PWSH, "-NoProfile", "-ExecutionPolicy", "Bypass",
        str(SCRIPTS_DIR / "Get-IntuneDevices.ps1"),
        "-Tenant", tenant_val,
        "-ClientId", t["client_id"],
        "-CertificatePath", pfx_path,
        "-ReportType", "policies",
        "-PolicyType", policy_type,
    ]

    jid = start_job(f"Device Policies ({policy_type}) — {t['name']}", "devices",
                    cmd, tenant_id=tenant_id, env=env)
    log_action(current_user(), "device_policies_fetch",
               tenant_id=tenant_id, tenant_name=t["name"],
               details=f"Type: {policy_type}")
    return jsonify({"ok": True, "job_id": jid})


# =========================================================================
# Device Actions (Wipe, Retire, Sync, Restart)
# =========================================================================

@bp.route("/tenants/<tenant_id>/devices/action", methods=["POST"])
@login_required
def device_action(tenant_id):
    """Execute a remote action on a managed device."""
    t = get_tenant(tenant_id)
    if not t or not t["client_id"]:
        return jsonify({"ok": False, "error": "Tenant not configured."})

    pfx_pass = get_tenant_pfx_password(t)
    if not pfx_pass:
        return jsonify({"ok": False, "error": "PFX password not configured."})

    action = request.form.get("action", "").strip()
    device_id = request.form.get("device_id", "").strip()
    if not action or not device_id:
        return jsonify({"ok": False, "error": "Action and device ID are required."})

    allowed_actions = ("sync", "restart", "retire", "wipe", "resetPasscode",
                       "remoteLock", "enableLostMode", "disableLostMode")
    if action not in allowed_actions:
        return jsonify({"ok": False, "error": f"Invalid action: {action}"})

    tenant_val = t["tenant_id"] or t["domain"]
    pfx_path = str(tenant_pfx_path(tenant_id))
    env = build_ps_env(pfx_pass)

    cmd = [
        PWSH, "-NoProfile", "-ExecutionPolicy", "Bypass",
        str(SCRIPTS_DIR / "Invoke-DeviceAction.ps1"),
        "-Tenant", tenant_val,
        "-ClientId", t["client_id"],
        "-CertificatePath", pfx_path,
        "-DeviceId", device_id,
        "-Action", action,
    ]

    jid = start_job(f"Device {action.title()} — {t['name']}", "devices",
                    cmd, tenant_id=tenant_id, env=env)
    log_action(current_user(), f"device_action_{action}",
               tenant_id=tenant_id, tenant_name=t["name"],
               details=f"Device: {device_id}")
    return jsonify({"ok": True, "job_id": jid})


# =========================================================================
# Cross-tenant Device Summary
# =========================================================================

@bp.route("/devices/overview")
@login_required
def devices_overview_page():
    tenants = list_tenants()
    ready_tenants = [t for t in tenants if t["client_id"]]
    return render_template("devices_overview.html", tenants=ready_tenants)


@bp.route("/devices/overview/scan", methods=["POST"])
@login_required
def devices_overview_scan():
    """Gather device overview across multiple tenants."""
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
        out_dir = str(tenant_output_dir(tid))
        env = build_ps_env(pfx_pass)

        cmd = [
            PWSH, "-NoProfile", "-ExecutionPolicy", "Bypass",
            str(SCRIPTS_DIR / "Get-IntuneDevices.ps1"),
            "-Tenant", tenant_val,
            "-ClientId", t["client_id"],
            "-CertificatePath", pfx_path,
            "-OutputFolder", out_dir,
            "-ReportType", "overview",
        ]

        jid = start_job(f"Device Overview — {t['name']}", "devices",
                        cmd, tenant_id=tid, env=env)
        job_ids.append(jid)

    log_action(current_user(), "devices_overview_scan",
               details=f"Tenants: {len(job_ids)}")
    return jsonify({
        "ok": True, "job_ids": job_ids,
        "message": f"Started device overview for {len(job_ids)} tenant(s).",
    })
