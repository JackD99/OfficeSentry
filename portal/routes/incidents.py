"""
portal/routes/incidents.py — Incident response playbook routes.
Provides guided BEC response, account compromise investigation,
data exfiltration analysis, and incident timeline workflows.
Inspired by Microsoft Sentinel playbooks and CIPP's incident response features.
"""

import json
import logging

from flask import (
    Blueprint, render_template, request, jsonify, abort,
)

from database import (
    get_tenant, list_tenants, log_action,
    save_cached_data, get_cached_data,
    get_db,
)
from helpers import (
    login_required, current_user,
    tenant_pfx_path, tenant_output_dir,
    build_ps_env, start_job, get_tenant_pfx_password,
    sanitize_text, validate_email,
    SCRIPTS_DIR, PWSH,
)

log = logging.getLogger("portal")

bp = Blueprint("incidents", __name__)


# =========================================================================
# Incident Dashboard
# =========================================================================

@bp.route("/incidents")
@login_required
def incidents_dashboard():
    tenants = list_tenants()
    ready_tenants = [t for t in tenants if t["client_id"]]
    return render_template("incidents_dashboard.html", tenants=ready_tenants)


# =========================================================================
# BEC Response Playbook
# =========================================================================

@bp.route("/incidents/bec-response")
@login_required
def bec_response_page():
    tenants = list_tenants()
    ready_tenants = [t for t in tenants if t["client_id"]]
    return render_template("incidents_bec_response.html", tenants=ready_tenants)


@bp.route("/tenants/<tenant_id>/incidents/bec/run", methods=["POST"])
@login_required
def bec_response_run(tenant_id):
    """Run BEC investigation playbook for a compromised user."""
    t = get_tenant(tenant_id)
    if not t or not t["client_id"]:
        return jsonify({"ok": False, "error": "Tenant not configured."})

    pfx_pass = get_tenant_pfx_password(t)
    if not pfx_pass:
        return jsonify({"ok": False, "error": "PFX password not configured."})

    target_user = request.form.get("target_user", "").strip()
    if not target_user or not validate_email(target_user):
        return jsonify({"ok": False, "error": "Valid target user email is required."})

    days_back = request.form.get("days_back", "30").strip()
    try:
        days_back = str(min(int(days_back), 90))
    except ValueError:
        days_back = "30"

    tenant_val = t["tenant_id"] or t["domain"]
    pfx_path = str(tenant_pfx_path(tenant_id))
    out_dir = str(tenant_output_dir(tenant_id))
    env = build_ps_env(pfx_pass)

    cmd = [
        PWSH, "-NoProfile", "-ExecutionPolicy", "Bypass",
        str(SCRIPTS_DIR / "Invoke-IncidentResponse.ps1"),
        "-Tenant", tenant_val,
        "-ClientId", t["client_id"],
        "-CertificatePath", pfx_path,
        "-OutputFolder", out_dir,
        "-Playbook", "BEC",
        "-TargetUser", target_user,
        "-DaysBack", days_back,
    ]

    jid = start_job(f"BEC Response — {target_user} — {t['name']}", "incidents",
                    cmd, tenant_id=tenant_id, env=env)
    log_action(current_user(), "bec_response_run",
               tenant_id=tenant_id, tenant_name=t["name"],
               details=f"Target: {target_user}, Days: {days_back}")
    return jsonify({"ok": True, "job_id": jid})


# =========================================================================
# Account Compromise Investigation
# =========================================================================

@bp.route("/incidents/account-compromise")
@login_required
def account_compromise_page():
    tenants = list_tenants()
    ready_tenants = [t for t in tenants if t["client_id"]]
    return render_template("incidents_account_compromise.html", tenants=ready_tenants)


@bp.route("/tenants/<tenant_id>/incidents/account-compromise/run", methods=["POST"])
@login_required
def account_compromise_run(tenant_id):
    """Run account compromise investigation."""
    t = get_tenant(tenant_id)
    if not t or not t["client_id"]:
        return jsonify({"ok": False, "error": "Tenant not configured."})

    pfx_pass = get_tenant_pfx_password(t)
    if not pfx_pass:
        return jsonify({"ok": False, "error": "PFX password not configured."})

    target_user = request.form.get("target_user", "").strip()
    if not target_user or not validate_email(target_user):
        return jsonify({"ok": False, "error": "Valid target user email is required."})

    days_back = request.form.get("days_back", "30").strip()
    try:
        days_back = str(min(int(days_back), 90))
    except ValueError:
        days_back = "30"

    include_steps = request.form.getlist("steps")

    tenant_val = t["tenant_id"] or t["domain"]
    pfx_path = str(tenant_pfx_path(tenant_id))
    out_dir = str(tenant_output_dir(tenant_id))
    env = build_ps_env(pfx_pass)

    cmd = [
        PWSH, "-NoProfile", "-ExecutionPolicy", "Bypass",
        str(SCRIPTS_DIR / "Invoke-IncidentResponse.ps1"),
        "-Tenant", tenant_val,
        "-ClientId", t["client_id"],
        "-CertificatePath", pfx_path,
        "-OutputFolder", out_dir,
        "-Playbook", "AccountCompromise",
        "-TargetUser", target_user,
        "-DaysBack", days_back,
    ]
    if include_steps:
        cmd += ["-Steps", ",".join(include_steps)]

    jid = start_job(f"Account Compromise — {target_user} — {t['name']}",
                    "incidents", cmd, tenant_id=tenant_id, env=env)
    log_action(current_user(), "account_compromise_run",
               tenant_id=tenant_id, tenant_name=t["name"],
               details=f"Target: {target_user}, Days: {days_back}")
    return jsonify({"ok": True, "job_id": jid})


# =========================================================================
# Data Exfiltration Analysis
# =========================================================================

@bp.route("/incidents/data-exfiltration")
@login_required
def data_exfiltration_page():
    tenants = list_tenants()
    ready_tenants = [t for t in tenants if t["client_id"]]
    return render_template("incidents_data_exfiltration.html", tenants=ready_tenants)


@bp.route("/tenants/<tenant_id>/incidents/data-exfil/run", methods=["POST"])
@login_required
def data_exfiltration_run(tenant_id):
    """Run data exfiltration analysis for a user or site."""
    t = get_tenant(tenant_id)
    if not t or not t["client_id"]:
        return jsonify({"ok": False, "error": "Tenant not configured."})

    pfx_pass = get_tenant_pfx_password(t)
    if not pfx_pass:
        return jsonify({"ok": False, "error": "PFX password not configured."})

    target_user = request.form.get("target_user", "").strip()
    days_back = request.form.get("days_back", "30").strip()
    try:
        days_back = str(min(int(days_back), 90))
    except ValueError:
        days_back = "30"

    scope = request.form.get("scope", "user")
    if scope not in ("user", "site", "tenant"):
        return jsonify({"ok": False, "error": "Invalid scope."})

    tenant_val = t["tenant_id"] or t["domain"]
    pfx_path = str(tenant_pfx_path(tenant_id))
    out_dir = str(tenant_output_dir(tenant_id))
    env = build_ps_env(pfx_pass)

    cmd = [
        PWSH, "-NoProfile", "-ExecutionPolicy", "Bypass",
        str(SCRIPTS_DIR / "Invoke-IncidentResponse.ps1"),
        "-Tenant", tenant_val,
        "-ClientId", t["client_id"],
        "-CertificatePath", pfx_path,
        "-OutputFolder", out_dir,
        "-Playbook", "DataExfiltration",
        "-Scope", scope,
        "-DaysBack", days_back,
    ]
    if target_user:
        cmd += ["-TargetUser", target_user]

    label = f"Data Exfil Analysis ({scope}) — {t['name']}"
    jid = start_job(label, "incidents", cmd, tenant_id=tenant_id, env=env)
    log_action(current_user(), "data_exfiltration_run",
               tenant_id=tenant_id, tenant_name=t["name"],
               details=f"Scope: {scope}, Target: {target_user or 'all'}, Days: {days_back}")
    return jsonify({"ok": True, "job_id": jid})


# =========================================================================
# Malware Outbreak Investigation
# =========================================================================

@bp.route("/incidents/malware-outbreak")
@login_required
def malware_outbreak_page():
    tenants = list_tenants()
    ready_tenants = [t for t in tenants if t["client_id"]]
    return render_template("incidents_malware_outbreak.html", tenants=ready_tenants)


@bp.route("/tenants/<tenant_id>/incidents/malware/run", methods=["POST"])
@login_required
def malware_outbreak_run(tenant_id):
    """Run malware outbreak investigation."""
    t = get_tenant(tenant_id)
    if not t or not t["client_id"]:
        return jsonify({"ok": False, "error": "Tenant not configured."})

    pfx_pass = get_tenant_pfx_password(t)
    if not pfx_pass:
        return jsonify({"ok": False, "error": "PFX password not configured."})

    days_back = request.form.get("days_back", "7").strip()
    try:
        days_back = str(min(int(days_back), 90))
    except ValueError:
        days_back = "7"

    tenant_val = t["tenant_id"] or t["domain"]
    pfx_path = str(tenant_pfx_path(tenant_id))
    out_dir = str(tenant_output_dir(tenant_id))
    env = build_ps_env(pfx_pass)

    cmd = [
        PWSH, "-NoProfile", "-ExecutionPolicy", "Bypass",
        str(SCRIPTS_DIR / "Invoke-IncidentResponse.ps1"),
        "-Tenant", tenant_val,
        "-ClientId", t["client_id"],
        "-CertificatePath", pfx_path,
        "-OutputFolder", out_dir,
        "-Playbook", "MalwareOutbreak",
        "-DaysBack", days_back,
    ]

    jid = start_job(f"Malware Investigation — {t['name']}", "incidents",
                    cmd, tenant_id=tenant_id, env=env)
    log_action(current_user(), "malware_outbreak_run",
               tenant_id=tenant_id, tenant_name=t["name"],
               details=f"Days: {days_back}")
    return jsonify({"ok": True, "job_id": jid})


# =========================================================================
# Privilege Escalation Detection
# =========================================================================

@bp.route("/incidents/privilege-escalation")
@login_required
def privilege_escalation_page():
    tenants = list_tenants()
    ready_tenants = [t for t in tenants if t["client_id"]]
    return render_template("incidents_privilege_escalation.html", tenants=ready_tenants)


@bp.route("/tenants/<tenant_id>/incidents/priv-esc/run", methods=["POST"])
@login_required
def privilege_escalation_run(tenant_id):
    """Run privilege escalation detection."""
    t = get_tenant(tenant_id)
    if not t or not t["client_id"]:
        return jsonify({"ok": False, "error": "Tenant not configured."})

    pfx_pass = get_tenant_pfx_password(t)
    if not pfx_pass:
        return jsonify({"ok": False, "error": "PFX password not configured."})

    days_back = request.form.get("days_back", "30").strip()
    try:
        days_back = str(min(int(days_back), 90))
    except ValueError:
        days_back = "30"

    tenant_val = t["tenant_id"] or t["domain"]
    pfx_path = str(tenant_pfx_path(tenant_id))
    out_dir = str(tenant_output_dir(tenant_id))
    env = build_ps_env(pfx_pass)

    cmd = [
        PWSH, "-NoProfile", "-ExecutionPolicy", "Bypass",
        str(SCRIPTS_DIR / "Invoke-IncidentResponse.ps1"),
        "-Tenant", tenant_val,
        "-ClientId", t["client_id"],
        "-CertificatePath", pfx_path,
        "-OutputFolder", out_dir,
        "-Playbook", "PrivilegeEscalation",
        "-DaysBack", days_back,
    ]

    jid = start_job(f"Priv Escalation Check — {t['name']}", "incidents",
                    cmd, tenant_id=tenant_id, env=env)
    log_action(current_user(), "privilege_escalation_run",
               tenant_id=tenant_id, tenant_name=t["name"],
               details=f"Days: {days_back}")
    return jsonify({"ok": True, "job_id": jid})


# =========================================================================
# Incident Timeline Builder
# =========================================================================

@bp.route("/tenants/<tenant_id>/incidents/timeline/build", methods=["POST"])
@login_required
def incident_timeline_build(tenant_id):
    """Build a consolidated incident timeline from multiple data sources."""
    t = get_tenant(tenant_id)
    if not t or not t["client_id"]:
        return jsonify({"ok": False, "error": "Tenant not configured."})

    pfx_pass = get_tenant_pfx_password(t)
    if not pfx_pass:
        return jsonify({"ok": False, "error": "PFX password not configured."})

    target_user = request.form.get("target_user", "").strip()
    start_date = request.form.get("start_date", "").strip()
    end_date = request.form.get("end_date", "").strip()

    if not start_date or not end_date:
        return jsonify({"ok": False, "error": "Start and end dates are required."})

    data_sources = request.form.getlist("data_sources")
    if not data_sources:
        data_sources = ["signins", "audit", "mailbox", "sharepoint"]

    tenant_val = t["tenant_id"] or t["domain"]
    pfx_path = str(tenant_pfx_path(tenant_id))
    out_dir = str(tenant_output_dir(tenant_id))
    env = build_ps_env(pfx_pass)

    cmd = [
        PWSH, "-NoProfile", "-ExecutionPolicy", "Bypass",
        str(SCRIPTS_DIR / "Invoke-IncidentResponse.ps1"),
        "-Tenant", tenant_val,
        "-ClientId", t["client_id"],
        "-CertificatePath", pfx_path,
        "-OutputFolder", out_dir,
        "-Playbook", "Timeline",
        "-StartDate", start_date,
        "-EndDate", end_date,
        "-DataSources", ",".join(data_sources),
    ]
    if target_user:
        cmd += ["-TargetUser", target_user]

    jid = start_job(f"Incident Timeline — {t['name']}", "incidents",
                    cmd, tenant_id=tenant_id, env=env)
    log_action(current_user(), "incident_timeline_build",
               tenant_id=tenant_id, tenant_name=t["name"],
               details=f"Target: {target_user or 'all'}, "
                       f"Range: {start_date} to {end_date}")
    return jsonify({"ok": True, "job_id": jid})
