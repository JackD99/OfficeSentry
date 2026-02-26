"""
portal/routes/email_security.py — Email security management routes.
Provides quarantine management, transport rules, anti-phishing policies,
safe links/attachments, and email threat reports.
Inspired by Microsoft Defender for Office 365 and CIPP's email security section.
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

bp = Blueprint("email_security", __name__)


# =========================================================================
# Quarantine Management
# =========================================================================

@bp.route("/email-security/quarantine")
@login_required
def quarantine_page():
    tenants = list_tenants()
    ready_tenants = [t for t in tenants if t["client_id"]]
    return render_template("email_quarantine.html", tenants=ready_tenants)


@bp.route("/tenants/<tenant_id>/quarantine/fetch", methods=["POST"])
@login_required
def quarantine_fetch(tenant_id):
    """Fetch quarantined messages for a tenant."""
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

    quarantine_type = request.form.get("quarantine_type", "all")
    start_date = request.form.get("start_date", "").strip()
    end_date = request.form.get("end_date", "").strip()

    cmd = [
        PWSH, "-NoProfile", "-ExecutionPolicy", "Bypass",
        str(SCRIPTS_DIR / "Get-QuarantineMessages.ps1"),
        "-Tenant", tenant_val,
        "-ClientId", t["client_id"],
        "-CertificatePath", pfx_path,
        "-OutputFolder", out_dir,
    ]
    if quarantine_type != "all":
        cmd += ["-QuarantineType", quarantine_type]
    if start_date:
        cmd += ["-StartDate", start_date]
    if end_date:
        cmd += ["-EndDate", end_date]

    jid = start_job(f"Quarantine Report — {t['name']}", "email_security", cmd,
                    tenant_id=tenant_id, env=env)
    log_action(current_user(), "quarantine_fetch",
               tenant_id=tenant_id, tenant_name=t["name"],
               details=f"Type: {quarantine_type}")
    return jsonify({"ok": True, "job_id": jid})


@bp.route("/tenants/<tenant_id>/quarantine/release", methods=["POST"])
@login_required
def quarantine_release(tenant_id):
    """Release or delete quarantined messages."""
    t = get_tenant(tenant_id)
    if not t or not t["client_id"]:
        return jsonify({"ok": False, "error": "Tenant not configured."})

    pfx_pass = get_tenant_pfx_password(t)
    if not pfx_pass:
        return jsonify({"ok": False, "error": "PFX password not configured."})

    action = request.form.get("action", "release")
    if action not in ("release", "delete", "deny"):
        return jsonify({"ok": False, "error": "Invalid action."})

    message_ids = request.form.getlist("message_ids")
    if not message_ids:
        return jsonify({"ok": False, "error": "No messages selected."})

    tenant_val = t["tenant_id"] or t["domain"]
    pfx_path = str(tenant_pfx_path(tenant_id))
    env = build_ps_env(pfx_pass)

    cmd = [
        PWSH, "-NoProfile", "-ExecutionPolicy", "Bypass",
        str(SCRIPTS_DIR / "Invoke-QuarantineAction.ps1"),
        "-Action", action,
        "-MessageIds", ",".join(message_ids),
        "-Tenant", tenant_val,
        "-ClientId", t["client_id"],
        "-CertificatePath", pfx_path,
    ]

    label = f"Quarantine {action.title()} ({len(message_ids)}) — {t['name']}"
    jid = start_job(label, "email_security", cmd, tenant_id=tenant_id, env=env)
    log_action(current_user(), f"quarantine_{action}",
               tenant_id=tenant_id, tenant_name=t["name"],
               details=f"Messages: {len(message_ids)}")
    return jsonify({"ok": True, "job_id": jid})


# =========================================================================
# Transport Rules
# =========================================================================

@bp.route("/email-security/transport-rules")
@login_required
def transport_rules_page():
    tenants = list_tenants()
    ready_tenants = [t for t in tenants if t["client_id"]]
    return render_template("email_transport_rules.html", tenants=ready_tenants)


@bp.route("/tenants/<tenant_id>/transport-rules/fetch", methods=["POST"])
@login_required
def transport_rules_fetch(tenant_id):
    """Fetch Exchange transport rules for a tenant."""
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
        str(SCRIPTS_DIR / "Get-TransportRules.ps1"),
        "-Tenant", tenant_val,
        "-ClientId", t["client_id"],
        "-CertificatePath", pfx_path,
    ]

    jid = start_job(f"Fetch Transport Rules — {t['name']}", "email_security", cmd,
                    tenant_id=tenant_id, env=env)
    log_action(current_user(), "transport_rules_fetch",
               tenant_id=tenant_id, tenant_name=t["name"])
    return jsonify({"ok": True, "job_id": jid})


# =========================================================================
# Anti-Phishing Policies
# =========================================================================

@bp.route("/email-security/anti-phishing")
@login_required
def anti_phishing_page():
    tenants = list_tenants()
    ready_tenants = [t for t in tenants if t["client_id"]]
    return render_template("email_anti_phishing.html", tenants=ready_tenants)


@bp.route("/tenants/<tenant_id>/anti-phishing/fetch", methods=["POST"])
@login_required
def anti_phishing_fetch(tenant_id):
    """Fetch anti-phishing policy configuration for a tenant."""
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
        str(SCRIPTS_DIR / "Get-EmailProtectionPolicies.ps1"),
        "-Tenant", tenant_val,
        "-ClientId", t["client_id"],
        "-CertificatePath", pfx_path,
        "-PolicyType", "antiphishing",
    ]

    jid = start_job(f"Fetch Anti-Phishing Policies — {t['name']}", "email_security",
                    cmd, tenant_id=tenant_id, env=env)
    log_action(current_user(), "anti_phishing_fetch",
               tenant_id=tenant_id, tenant_name=t["name"])
    return jsonify({"ok": True, "job_id": jid})


# =========================================================================
# Safe Links & Safe Attachments (Defender for Office 365)
# =========================================================================

@bp.route("/email-security/defender-policies")
@login_required
def defender_policies_page():
    tenants = list_tenants()
    ready_tenants = [t for t in tenants if t["client_id"]]
    return render_template("email_defender_policies.html", tenants=ready_tenants)


@bp.route("/tenants/<tenant_id>/defender-policies/fetch", methods=["POST"])
@login_required
def defender_policies_fetch(tenant_id):
    """Fetch Safe Links and Safe Attachments policies for a tenant."""
    t = get_tenant(tenant_id)
    if not t or not t["client_id"]:
        return jsonify({"ok": False, "error": "Tenant not configured."})

    pfx_pass = get_tenant_pfx_password(t)
    if not pfx_pass:
        return jsonify({"ok": False, "error": "PFX password not configured."})

    policy_type = request.form.get("policy_type", "all")
    if policy_type not in ("all", "safelinks", "safeattachments", "antimalware", "antispam"):
        return jsonify({"ok": False, "error": "Invalid policy type."})

    tenant_val = t["tenant_id"] or t["domain"]
    pfx_path = str(tenant_pfx_path(tenant_id))
    env = build_ps_env(pfx_pass)

    cmd = [
        PWSH, "-NoProfile", "-ExecutionPolicy", "Bypass",
        str(SCRIPTS_DIR / "Get-EmailProtectionPolicies.ps1"),
        "-Tenant", tenant_val,
        "-ClientId", t["client_id"],
        "-CertificatePath", pfx_path,
        "-PolicyType", policy_type,
    ]

    jid = start_job(f"Fetch Defender Policies ({policy_type}) — {t['name']}",
                    "email_security", cmd, tenant_id=tenant_id, env=env)
    log_action(current_user(), "defender_policies_fetch",
               tenant_id=tenant_id, tenant_name=t["name"],
               details=f"Type: {policy_type}")
    return jsonify({"ok": True, "job_id": jid})


# =========================================================================
# Email Threat Report (cross-tenant summary)
# =========================================================================

@bp.route("/email-security/threat-report")
@login_required
def threat_report_page():
    tenants = list_tenants()
    ready_tenants = [t for t in tenants if t["client_id"]]
    return render_template("email_threat_report.html", tenants=ready_tenants)


@bp.route("/email-security/threat-report/run", methods=["POST"])
@login_required
def threat_report_run():
    """Run email threat assessment across selected tenants."""
    tenant_ids = request.form.getlist("tenant_ids")
    if not tenant_ids:
        return jsonify({"ok": False, "error": "Select at least one tenant."})

    days = request.form.get("days", "30").strip()
    try:
        days = str(min(int(days), 90))
    except ValueError:
        days = "30"

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
            str(SCRIPTS_DIR / "Get-EmailThreatReport.ps1"),
            "-Tenant", tenant_val,
            "-ClientId", t["client_id"],
            "-CertificatePath", pfx_path,
            "-OutputFolder", out_dir,
            "-Days", days,
        ]

        jid = start_job(f"Email Threat Report — {t['name']}", "email_security",
                        cmd, tenant_id=tid, env=env)
        job_ids.append(jid)

    log_action(current_user(), "threat_report_run",
               details=f"Tenants: {len(job_ids)}, Days: {days}")
    return jsonify({
        "ok": True, "job_ids": job_ids,
        "message": f"Started threat report for {len(job_ids)} tenant(s).",
    })


# =========================================================================
# Spoof Intelligence
# =========================================================================

@bp.route("/tenants/<tenant_id>/spoof-intelligence/fetch", methods=["POST"])
@login_required
def spoof_intelligence_fetch(tenant_id):
    """Fetch spoof intelligence insights for a tenant."""
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
        str(SCRIPTS_DIR / "Get-EmailProtectionPolicies.ps1"),
        "-Tenant", tenant_val,
        "-ClientId", t["client_id"],
        "-CertificatePath", pfx_path,
        "-PolicyType", "spoofintelligence",
    ]

    jid = start_job(f"Spoof Intelligence — {t['name']}", "email_security",
                    cmd, tenant_id=tenant_id, env=env)
    log_action(current_user(), "spoof_intelligence_fetch",
               tenant_id=tenant_id, tenant_name=t["name"])
    return jsonify({"ok": True, "job_id": jid})
