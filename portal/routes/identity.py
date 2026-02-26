"""
portal/routes/identity.py — Identity protection management routes.
Provides risky users, risky sign-ins, MFA gap analysis, authentication
strengths, and password protection policy views.
Inspired by Microsoft Entra Identity Protection and CIPP's identity section.
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
    sanitize_text, validate_email,
    SCRIPTS_DIR, PWSH,
)

log = logging.getLogger("portal")

bp = Blueprint("identity", __name__)


# =========================================================================
# Risky Users
# =========================================================================

@bp.route("/identity/risky-users")
@login_required
def risky_users_page():
    tenants = list_tenants()
    ready_tenants = [t for t in tenants if t["client_id"]]
    return render_template("identity_risky_users.html", tenants=ready_tenants)


@bp.route("/tenants/<tenant_id>/risky-users/fetch", methods=["POST"])
@login_required
def risky_users_fetch(tenant_id):
    """Fetch risky users from Identity Protection."""
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

    risk_level = request.form.get("risk_level", "all")

    cmd = [
        PWSH, "-NoProfile", "-ExecutionPolicy", "Bypass",
        str(SCRIPTS_DIR / "Get-IdentityProtection.ps1"),
        "-Tenant", tenant_val,
        "-ClientId", t["client_id"],
        "-CertificatePath", pfx_path,
        "-OutputFolder", out_dir,
        "-ReportType", "riskyUsers",
    ]
    if risk_level != "all":
        cmd += ["-RiskLevel", risk_level]

    jid = start_job(f"Risky Users — {t['name']}", "identity", cmd,
                    tenant_id=tenant_id, env=env)
    log_action(current_user(), "risky_users_fetch",
               tenant_id=tenant_id, tenant_name=t["name"],
               details=f"Risk level: {risk_level}")
    return jsonify({"ok": True, "job_id": jid})


@bp.route("/tenants/<tenant_id>/risky-users/dismiss", methods=["POST"])
@login_required
def risky_user_dismiss(tenant_id):
    """Dismiss risk for a specific user."""
    t = get_tenant(tenant_id)
    if not t or not t["client_id"]:
        return jsonify({"ok": False, "error": "Tenant not configured."})

    pfx_pass = get_tenant_pfx_password(t)
    if not pfx_pass:
        return jsonify({"ok": False, "error": "PFX password not configured."})

    user_id = request.form.get("user_id", "").strip()
    if not user_id:
        return jsonify({"ok": False, "error": "User ID is required."})

    tenant_val = t["tenant_id"] or t["domain"]
    pfx_path = str(tenant_pfx_path(tenant_id))
    env = build_ps_env(pfx_pass)

    cmd = [
        PWSH, "-NoProfile", "-ExecutionPolicy", "Bypass",
        str(SCRIPTS_DIR / "Get-IdentityProtection.ps1"),
        "-Tenant", tenant_val,
        "-ClientId", t["client_id"],
        "-CertificatePath", pfx_path,
        "-ReportType", "dismissRisk",
        "-TargetUserId", user_id,
    ]

    jid = start_job(f"Dismiss Risk — {t['name']}", "identity", cmd,
                    tenant_id=tenant_id, env=env)
    log_action(current_user(), "risky_user_dismiss",
               tenant_id=tenant_id, tenant_name=t["name"],
               details=f"User: {user_id}")
    return jsonify({"ok": True, "job_id": jid})


# =========================================================================
# Risky Sign-Ins
# =========================================================================

@bp.route("/identity/risky-signins")
@login_required
def risky_signins_page():
    tenants = list_tenants()
    ready_tenants = [t for t in tenants if t["client_id"]]
    return render_template("identity_risky_signins.html", tenants=ready_tenants)


@bp.route("/tenants/<tenant_id>/risky-signins/fetch", methods=["POST"])
@login_required
def risky_signins_fetch(tenant_id):
    """Fetch risky sign-ins from Identity Protection."""
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

    days = request.form.get("days", "30").strip()
    risk_level = request.form.get("risk_level", "all")

    cmd = [
        PWSH, "-NoProfile", "-ExecutionPolicy", "Bypass",
        str(SCRIPTS_DIR / "Get-IdentityProtection.ps1"),
        "-Tenant", tenant_val,
        "-ClientId", t["client_id"],
        "-CertificatePath", pfx_path,
        "-OutputFolder", out_dir,
        "-ReportType", "riskySignIns",
        "-Days", days,
    ]
    if risk_level != "all":
        cmd += ["-RiskLevel", risk_level]

    jid = start_job(f"Risky Sign-Ins — {t['name']}", "identity", cmd,
                    tenant_id=tenant_id, env=env)
    log_action(current_user(), "risky_signins_fetch",
               tenant_id=tenant_id, tenant_name=t["name"],
               details=f"Days: {days}, Risk: {risk_level}")
    return jsonify({"ok": True, "job_id": jid})


# =========================================================================
# MFA Gap Analysis
# =========================================================================

@bp.route("/identity/mfa-report")
@login_required
def mfa_report_page():
    tenants = list_tenants()
    ready_tenants = [t for t in tenants if t["client_id"]]
    return render_template("identity_mfa_report.html", tenants=ready_tenants)


@bp.route("/identity/mfa-report/run", methods=["POST"])
@login_required
def mfa_report_run():
    """Run MFA gap analysis across selected tenants."""
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
            str(SCRIPTS_DIR / "Get-IdentityProtection.ps1"),
            "-Tenant", tenant_val,
            "-ClientId", t["client_id"],
            "-CertificatePath", pfx_path,
            "-OutputFolder", out_dir,
            "-ReportType", "mfaGap",
        ]

        jid = start_job(f"MFA Gap Analysis — {t['name']}", "identity",
                        cmd, tenant_id=tid, env=env)
        job_ids.append(jid)

    log_action(current_user(), "mfa_report_run",
               details=f"Tenants: {len(job_ids)}")
    return jsonify({
        "ok": True, "job_ids": job_ids,
        "message": f"Started MFA gap analysis for {len(job_ids)} tenant(s).",
    })


# =========================================================================
# Authentication Strengths
# =========================================================================

@bp.route("/tenants/<tenant_id>/auth-strengths")
@login_required
def auth_strengths_page(tenant_id):
    t = get_tenant(tenant_id)
    if not t:
        abort(404)
    cached = get_cached_data(tenant_id, "auth_strengths")
    return render_template("identity_auth_strengths.html", tenant=t, cached=cached)


@bp.route("/tenants/<tenant_id>/auth-strengths/fetch", methods=["POST"])
@login_required
def auth_strengths_fetch(tenant_id):
    """Fetch authentication strength policies for a tenant."""
    t = get_tenant(tenant_id)
    if not t or not t["client_id"]:
        return jsonify({"ok": False, "error": "Tenant not configured."})

    pfx_pass = get_tenant_pfx_password(t)
    if not pfx_pass:
        return jsonify({"ok": False, "error": "PFX password not configured."})

    tenant_val = t["tenant_id"] or t["domain"]
    pfx_path = str(tenant_pfx_path(tenant_id))
    env = build_ps_env(pfx_pass)

    cmd = [
        PWSH, "-NoProfile", "-ExecutionPolicy", "Bypass",
        str(SCRIPTS_DIR / "Invoke-GraphExplorer.ps1"),
        "-Tenant", tenant_val,
        "-ClientId", t["client_id"],
        "-CertificatePath", pfx_path,
        "-GraphUri", "https://graph.microsoft.com/v1.0/policies/authenticationStrengthPolicies",
        "-Method", "GET",
    ]

    jid = start_job(f"Fetch Auth Strengths — {t['name']}", "identity",
                    cmd, tenant_id=tenant_id, env=env)
    log_action(current_user(), "auth_strengths_fetch",
               tenant_id=tenant_id, tenant_name=t["name"])
    return jsonify({"ok": True, "job_id": jid})


# =========================================================================
# Password Protection Policies
# =========================================================================

@bp.route("/tenants/<tenant_id>/password-protection")
@login_required
def password_protection_page(tenant_id):
    t = get_tenant(tenant_id)
    if not t:
        abort(404)
    cached = get_cached_data(tenant_id, "password_protection")
    return render_template("identity_password_protection.html", tenant=t, cached=cached)


@bp.route("/tenants/<tenant_id>/password-protection/fetch", methods=["POST"])
@login_required
def password_protection_fetch(tenant_id):
    """Fetch password protection settings."""
    t = get_tenant(tenant_id)
    if not t or not t["client_id"]:
        return jsonify({"ok": False, "error": "Tenant not configured."})

    pfx_pass = get_tenant_pfx_password(t)
    if not pfx_pass:
        return jsonify({"ok": False, "error": "PFX password not configured."})

    tenant_val = t["tenant_id"] or t["domain"]
    pfx_path = str(tenant_pfx_path(tenant_id))
    env = build_ps_env(pfx_pass)

    cmd = [
        PWSH, "-NoProfile", "-ExecutionPolicy", "Bypass",
        str(SCRIPTS_DIR / "Get-IdentityProtection.ps1"),
        "-Tenant", tenant_val,
        "-ClientId", t["client_id"],
        "-CertificatePath", pfx_path,
        "-ReportType", "passwordProtection",
    ]

    jid = start_job(f"Password Protection — {t['name']}", "identity",
                    cmd, tenant_id=tenant_id, env=env)
    log_action(current_user(), "password_protection_fetch",
               tenant_id=tenant_id, tenant_name=t["name"])
    return jsonify({"ok": True, "job_id": jid})


# =========================================================================
# Cross-tenant Identity Risk Summary
# =========================================================================

@bp.route("/identity/risk-overview")
@login_required
def risk_overview_page():
    tenants = list_tenants()
    ready_tenants = [t for t in tenants if t["client_id"]]
    return render_template("identity_risk_overview.html", tenants=ready_tenants)


@bp.route("/identity/risk-overview/scan", methods=["POST"])
@login_required
def risk_overview_scan():
    """Run risk overview scan across multiple tenants."""
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
            str(SCRIPTS_DIR / "Get-IdentityProtection.ps1"),
            "-Tenant", tenant_val,
            "-ClientId", t["client_id"],
            "-CertificatePath", pfx_path,
            "-OutputFolder", out_dir,
            "-ReportType", "riskOverview",
        ]

        jid = start_job(f"Risk Overview — {t['name']}", "identity",
                        cmd, tenant_id=tid, env=env)
        job_ids.append(jid)

    log_action(current_user(), "risk_overview_scan",
               details=f"Tenants: {len(job_ids)}")
    return jsonify({
        "ok": True, "job_ids": job_ids,
        "message": f"Started risk overview for {len(job_ids)} tenant(s).",
    })
