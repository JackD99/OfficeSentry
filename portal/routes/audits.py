"""
portal/routes/audits.py — SPO audit, security scan, breach investigation, and batch routes.
"""

import logging
import subprocess
from datetime import datetime
from pathlib import Path

from flask import (
    Blueprint, render_template, request, jsonify, abort,
)

from database import (
    get_tenant, list_tenants, touch_tenant_audit, log_action,
)
from helpers import (
    login_required, current_user,
    tenant_pfx_path, tenant_output_dir,
    build_ps_env, start_job, get_tenant_pfx_password,
    SCRIPTS_DIR, INSPECTORS_DIR, PWSH,
)

log = logging.getLogger("portal")

bp = Blueprint("audits", __name__)


# ---------------------------------------------------------------------------
# SPO Audit
# ---------------------------------------------------------------------------

@bp.route("/tenants/<tenant_id>/audit/spo")
@login_required
def audit_spo(tenant_id):
    t = get_tenant(tenant_id)
    if not t:
        abort(404)
    ready = bool(t["client_id"]) and tenant_pfx_path(tenant_id).exists()
    return render_template("audit_spo.html", tenant=t, ready=ready)


@bp.route("/tenants/<tenant_id>/audit/spo/run", methods=["POST"])
@login_required
def audit_spo_run(tenant_id):
    t = get_tenant(tenant_id)
    if not t:
        return jsonify({"ok": False, "error": "Tenant not found."}), 404

    audit_type = request.form.get("audit_type", "groups")
    site_url   = request.form.get("site_url", "").strip()
    user_email = request.form.get("user_email", "").strip()

    pfx_pass = get_tenant_pfx_password(t)
    if not pfx_pass:
        return jsonify({"ok": False, "error": "PFX password not configured for this tenant."})

    tenant_val = t["tenant_id"] or t["domain"]
    client     = t["client_id"]
    pfx_path   = str(tenant_pfx_path(tenant_id))
    out_dir    = str(tenant_output_dir(tenant_id))

    script_map = {
        "groups":       "Audit-Groups.ps1",
        "permissions":  "Audit-SitePermissions.ps1",
        "external":     "Audit-ExternalSharing.ps1",
        "user":         "Get-SharePointTenantPermissions.ps1",
    }
    label_map = {
        "groups":      "Groups Audit",
        "permissions": "Site Permissions Audit",
        "external":    "External Sharing Audit",
        "user":        "User Permissions Audit",
    }
    script_name = script_map.get(audit_type, "Audit-Groups.ps1")
    label = f"{label_map.get(audit_type, audit_type)} — {t['name']}"

    stamp    = datetime.now().strftime("%Y%m%d-%H%M")
    csv_out  = str(Path(out_dir) / f"UserPerms_{stamp}.csv")
    xlsx_out = str(Path(out_dir) / f"UserPerms_{stamp}.xlsx")

    env = build_ps_env(pfx_pass)

    if audit_type == "user":
        args = [
            "-Tenant",          tenant_val,
            "-UserEmail",       user_email,
            "-CSVPath",         csv_out,
            "-XlsxPath",       xlsx_out,
            "-ClientId",        client,
            "-CertificatePath", pfx_path,
        ]
    else:
        args = [
            "-SiteUrl",         site_url,
            "-OutputFolder",    out_dir,
            "-ClientId",        client,
            "-Tenant",          tenant_val,
            "-CertificatePath", pfx_path,
        ]

    cmd = [PWSH, "-NoProfile", "-ExecutionPolicy", "Bypass",
           str(SCRIPTS_DIR / script_name)] + args

    job_id = start_job(label, "spo", cmd, tenant_id=tenant_id, env=env)
    touch_tenant_audit(tenant_id)

    log_action(current_user(), f"spo_audit_{audit_type}",
               tenant_id=tenant_id, tenant_name=t["name"],
               details=f"Site: {site_url or 'all'}")

    return jsonify({"ok": True, "job_id": job_id})


# ---------------------------------------------------------------------------
# Security Scan
# ---------------------------------------------------------------------------

@bp.route("/tenants/<tenant_id>/audit/security")
@login_required
def audit_security(tenant_id):
    t = get_tenant(tenant_id)
    if not t:
        abort(404)
    inspector_dir = INSPECTORS_DIR / "Inspectors"
    inspectors = sorted(
        set(f.stem for f in inspector_dir.glob("*.ps1"))
    ) if inspector_dir.exists() else []
    ready = bool(t["client_id"]) and tenant_pfx_path(tenant_id).exists()
    return render_template("audit_security.html", tenant=t,
                           inspectors=inspectors, ready=ready)


@bp.route("/tenants/<tenant_id>/audit/security/run", methods=["POST"])
@login_required
def audit_security_run(tenant_id):
    t = get_tenant(tenant_id)
    if not t:
        return jsonify({"ok": False, "error": "Tenant not found."}), 404

    report_format = request.form.get("report_format", "HTML")
    selected      = request.form.getlist("inspectors")

    pfx_pass = get_tenant_pfx_password(t)
    if not pfx_pass:
        return jsonify({"ok": False, "error": "PFX password not configured."})

    tenant_val = t["tenant_id"] or t["domain"]
    client     = t["client_id"]
    pfx_path   = str(tenant_pfx_path(tenant_id))

    # Get thumbprint securely via env var (no password in command string)
    env = build_ps_env(pfx_pass)
    thumb_script = (
        "$pass = ConvertTo-SecureString $env:AUDIT_PFX_PASSWORD -AsPlainText -Force; "
        "$c = New-Object System.Security.Cryptography.X509Certificates.X509Certificate2("
        f"'{pfx_path}', $pass, 'EphemeralKeySet'); "
        "Write-Output $c.Thumbprint"
    )
    try:
        thumb_result = subprocess.run(
            [PWSH, "-NoProfile", "-NonInteractive", "-Command", thumb_script],
            capture_output=True, text=True, timeout=15, env=env
        )
        thumbprint = thumb_result.stdout.strip()
    except Exception:
        thumbprint = ""

    stamp   = datetime.now().strftime("%Y%m%d-%H%M")
    out_dir = str(tenant_output_dir(tenant_id) / f"SecurityScan_{stamp}")
    Path(out_dir).mkdir(parents=True, exist_ok=True)

    inspect_script = str(INSPECTORS_DIR / "365Inspect.ps1")

    cmd = [
        PWSH, "-NoProfile", "-ExecutionPolicy", "Bypass",
        inspect_script,
        "-OutPath",           out_dir,
        "-UserPrincipalName", t.get("domain", tenant_val),
        "-Auth",              "APP",
        "-reportType",        report_format,
        "-AppDomain",         tenant_val,
        "-AppClientId",       client,
        "-AppThumbprint",     thumbprint,
        "-SkipModuleCheck",
    ]
    if selected:
        cmd += ["-SelectedInspectors"] + selected

    label = f"Security Scan ({report_format}) — {t['name']}"
    job_id = start_job(label, "security", cmd, tenant_id=tenant_id, env=env)
    touch_tenant_audit(tenant_id)

    log_action(current_user(), "security_scan",
               tenant_id=tenant_id, tenant_name=t["name"],
               details=f"Format: {report_format}, Inspectors: {len(selected) or 'all'}")

    return jsonify({"ok": True, "job_id": job_id})


# ---------------------------------------------------------------------------
# Breach Investigation
# ---------------------------------------------------------------------------

@bp.route("/tenants/<tenant_id>/audit/breach")
@login_required
def audit_breach(tenant_id):
    t = get_tenant(tenant_id)
    if not t:
        abort(404)
    ready = bool(t["client_id"]) and tenant_pfx_path(tenant_id).exists()
    return render_template("audit_breach.html", tenant=t, ready=ready)


@bp.route("/tenants/<tenant_id>/audit/breach/run", methods=["POST"])
@login_required
def audit_breach_run(tenant_id):
    t = get_tenant(tenant_id)
    if not t:
        return jsonify({"ok": False, "error": "Tenant not found."}), 404

    investigation = request.form.get("investigation", "")
    date_start    = request.form.get("date_start", "").strip()
    date_end      = request.form.get("date_end", "").strip()
    target_user   = request.form.get("target_user", "").strip()

    pfx_pass = get_tenant_pfx_password(t)
    if not pfx_pass:
        return jsonify({"ok": False, "error": "PFX password not configured."})

    tenant_val = t["tenant_id"] or t["domain"]
    client     = t["client_id"]
    pfx_path   = str(tenant_pfx_path(tenant_id))
    out_dir    = str(tenant_output_dir(tenant_id))

    env = build_ps_env(pfx_pass)

    script_map = {
        "ual":        "Search-UnifiedAuditLog.ps1",
        "inbox":      "Audit-InboxRules.ps1",
        "signin":     "Get-SignInLogs.ps1",
        "oauth":      "Audit-OAuthConsent.ps1",
        "msgtrace":   "Get-MessageTrace.ps1",
    }
    label_map = {
        "ual":        "Unified Audit Log Search",
        "inbox":      "Inbox Rules Audit",
        "signin":     "Sign-In Log Analysis",
        "oauth":      "OAuth Consent Review",
        "msgtrace":   "Message Trace",
    }

    script_name = script_map.get(investigation)
    if not script_name:
        return jsonify({"ok": False, "error": f"Unknown investigation type: {investigation}"})

    label = f"{label_map[investigation]} — {t['name']}"

    base_args = [
        "-Tenant",          tenant_val,
        "-ClientId",        client,
        "-CertificatePath", pfx_path,
        "-OutputFolder",    out_dir,
    ]

    if date_start:
        base_args += ["-StartDate", date_start]
    if date_end:
        base_args += ["-EndDate", date_end]
    if target_user and investigation in ("ual", "inbox", "signin", "msgtrace"):
        base_args += ["-TargetUser", target_user]

    cmd = [PWSH, "-NoProfile", "-ExecutionPolicy", "Bypass",
           str(SCRIPTS_DIR / script_name)] + base_args

    job_id = start_job(label, "breach", cmd, tenant_id=tenant_id, env=env)
    touch_tenant_audit(tenant_id)

    log_action(current_user(), f"breach_{investigation}",
               tenant_id=tenant_id, tenant_name=t["name"],
               details=f"Target: {target_user or 'all'}, Range: {date_start} to {date_end}")

    return jsonify({"ok": True, "job_id": job_id})


# ---------------------------------------------------------------------------
# Batch Operations
# ---------------------------------------------------------------------------

@bp.route("/batch/audit", methods=["POST"])
@login_required
def batch_audit():
    tenant_ids  = request.form.getlist("tenant_ids")
    audit_type  = request.form.get("audit_type", "security")
    report_fmt  = request.form.get("report_format", "HTML")

    if not tenant_ids:
        return jsonify({"ok": False, "error": "No tenants selected."})

    job_ids = []
    for tid in tenant_ids:
        t = get_tenant(tid)
        if not t or not t["client_id"]:
            continue

        pfx_pass = get_tenant_pfx_password(t)
        if not pfx_pass:
            continue

        tenant_val = t["tenant_id"] or t["domain"]
        pfx_path   = str(tenant_pfx_path(tid))
        out_dir    = str(tenant_output_dir(tid))
        env = build_ps_env(pfx_pass)

        if audit_type == "security":
            stamp = datetime.now().strftime("%Y%m%d-%H%M")
            scan_dir = str(Path(out_dir) / f"SecurityScan_{stamp}")
            Path(scan_dir).mkdir(parents=True, exist_ok=True)

            cmd = [
                PWSH, "-NoProfile", "-ExecutionPolicy", "Bypass",
                str(INSPECTORS_DIR / "365Inspect.ps1"),
                "-OutPath",           scan_dir,
                "-UserPrincipalName", t.get("domain", tenant_val),
                "-Auth",              "APP",
                "-reportType",        report_fmt,
                "-AppDomain",         tenant_val,
                "-AppClientId",       t["client_id"],
                "-SkipModuleCheck",
            ]
            label = f"Batch Security Scan — {t['name']}"
        else:
            cmd = [
                PWSH, "-NoProfile", "-ExecutionPolicy", "Bypass",
                str(SCRIPTS_DIR / "Audit-Groups.ps1"),
                "-SiteUrl",         request.form.get("site_url", ""),
                "-OutputFolder",    out_dir,
                "-ClientId",        t["client_id"],
                "-Tenant",          tenant_val,
                "-CertificatePath", pfx_path,
            ]
            label = f"Batch SPO Audit — {t['name']}"

        jid = start_job(label, "batch", cmd, tenant_id=tid, env=env)
        touch_tenant_audit(tid)
        job_ids.append(jid)

    log_action(current_user(), "batch_audit",
               details=f"Type: {audit_type}, Tenants: {len(job_ids)}")

    return jsonify({"ok": True, "job_ids": job_ids,
                    "message": f"Started {len(job_ids)} audit(s)."})
