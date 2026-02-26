"""
portal/routes/licenses.py — License management routes.
Provides license inventory, usage analysis, optimization recommendations,
SKU mapping, and cross-tenant license tracking.
Inspired by Microsoft 365 admin center and CIPP's license management.
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

bp = Blueprint("licenses", __name__)


# =========================================================================
# License Inventory
# =========================================================================

@bp.route("/licenses")
@login_required
def license_inventory_page():
    tenants = list_tenants()
    ready_tenants = [t for t in tenants if t["client_id"]]
    return render_template("licenses_inventory.html", tenants=ready_tenants)


@bp.route("/tenants/<tenant_id>/licenses/fetch", methods=["POST"])
@login_required
def licenses_fetch(tenant_id):
    """Fetch subscribed SKUs and license assignments for a tenant."""
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
        str(SCRIPTS_DIR / "Get-LicenseReport.ps1"),
        "-Tenant", tenant_val,
        "-ClientId", t["client_id"],
        "-CertificatePath", pfx_path,
        "-OutputFolder", out_dir,
        "-ReportType", "inventory",
    ]

    jid = start_job(f"License Inventory — {t['name']}", "licenses", cmd,
                    tenant_id=tenant_id, env=env)
    log_action(current_user(), "licenses_fetch",
               tenant_id=tenant_id, tenant_name=t["name"])
    return jsonify({"ok": True, "job_id": jid})


# =========================================================================
# License Usage Analysis
# =========================================================================

@bp.route("/licenses/usage")
@login_required
def license_usage_page():
    tenants = list_tenants()
    ready_tenants = [t for t in tenants if t["client_id"]]
    return render_template("licenses_usage.html", tenants=ready_tenants)


@bp.route("/tenants/<tenant_id>/licenses/usage/fetch", methods=["POST"])
@login_required
def license_usage_fetch(tenant_id):
    """Fetch license utilization and inactive license data."""
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

    inactive_days = request.form.get("inactive_days", "90").strip()
    try:
        inactive_days = str(max(int(inactive_days), 1))
    except ValueError:
        inactive_days = "90"

    cmd = [
        PWSH, "-NoProfile", "-ExecutionPolicy", "Bypass",
        str(SCRIPTS_DIR / "Get-LicenseReport.ps1"),
        "-Tenant", tenant_val,
        "-ClientId", t["client_id"],
        "-CertificatePath", pfx_path,
        "-OutputFolder", out_dir,
        "-ReportType", "usage",
        "-InactiveDays", inactive_days,
    ]

    jid = start_job(f"License Usage — {t['name']}", "licenses", cmd,
                    tenant_id=tenant_id, env=env)
    log_action(current_user(), "license_usage_fetch",
               tenant_id=tenant_id, tenant_name=t["name"],
               details=f"Inactive days threshold: {inactive_days}")
    return jsonify({"ok": True, "job_id": jid})


# =========================================================================
# License Optimization Recommendations
# =========================================================================

@bp.route("/licenses/optimization")
@login_required
def license_optimization_page():
    tenants = list_tenants()
    ready_tenants = [t for t in tenants if t["client_id"]]
    return render_template("licenses_optimization.html", tenants=ready_tenants)


@bp.route("/tenants/<tenant_id>/licenses/optimize/fetch", methods=["POST"])
@login_required
def license_optimization_fetch(tenant_id):
    """Generate license optimization recommendations."""
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
        str(SCRIPTS_DIR / "Get-LicenseReport.ps1"),
        "-Tenant", tenant_val,
        "-ClientId", t["client_id"],
        "-CertificatePath", pfx_path,
        "-OutputFolder", out_dir,
        "-ReportType", "optimization",
    ]

    jid = start_job(f"License Optimization — {t['name']}", "licenses", cmd,
                    tenant_id=tenant_id, env=env)
    log_action(current_user(), "license_optimization_fetch",
               tenant_id=tenant_id, tenant_name=t["name"])
    return jsonify({"ok": True, "job_id": jid})


# =========================================================================
# Cross-tenant License Summary
# =========================================================================

@bp.route("/licenses/overview")
@login_required
def license_overview_page():
    tenants = list_tenants()
    ready_tenants = [t for t in tenants if t["client_id"]]
    return render_template("licenses_overview.html", tenants=ready_tenants)


@bp.route("/licenses/overview/scan", methods=["POST"])
@login_required
def license_overview_scan():
    """Run license summary across multiple tenants."""
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
            str(SCRIPTS_DIR / "Get-LicenseReport.ps1"),
            "-Tenant", tenant_val,
            "-ClientId", t["client_id"],
            "-CertificatePath", pfx_path,
            "-OutputFolder", out_dir,
            "-ReportType", "overview",
        ]

        jid = start_job(f"License Overview — {t['name']}", "licenses",
                        cmd, tenant_id=tid, env=env)
        job_ids.append(jid)

    log_action(current_user(), "license_overview_scan",
               details=f"Tenants: {len(job_ids)}")
    return jsonify({
        "ok": True, "job_ids": job_ids,
        "message": f"Started license overview for {len(job_ids)} tenant(s).",
    })


# =========================================================================
# User License Details
# =========================================================================

@bp.route("/tenants/<tenant_id>/licenses/user-detail")
@login_required
def license_user_detail_page(tenant_id):
    t = get_tenant(tenant_id)
    if not t:
        abort(404)
    cached = get_cached_data(tenant_id, "license_assignments")
    return render_template("licenses_user_detail.html", tenant=t, cached=cached)


@bp.route("/tenants/<tenant_id>/licenses/user-detail/fetch", methods=["POST"])
@login_required
def license_user_detail_fetch(tenant_id):
    """Fetch per-user license assignments."""
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
        str(SCRIPTS_DIR / "Get-LicenseReport.ps1"),
        "-Tenant", tenant_val,
        "-ClientId", t["client_id"],
        "-CertificatePath", pfx_path,
        "-OutputFolder", out_dir,
        "-ReportType", "userDetail",
    ]

    jid = start_job(f"User Licenses — {t['name']}", "licenses", cmd,
                    tenant_id=tenant_id, env=env)
    log_action(current_user(), "license_user_detail_fetch",
               tenant_id=tenant_id, tenant_name=t["name"])
    return jsonify({"ok": True, "job_id": jid})
