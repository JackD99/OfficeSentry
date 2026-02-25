"""
portal/routes/audits.py — SPO audit, security scan, breach investigation, and batch routes.
"""

import json
import logging
import re
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
    validate_site_url, validate_email, sanitize_text,
    SCRIPTS_DIR, INSPECTORS_DIR, PWSH,
)

log = logging.getLogger("portal")

# ---------------------------------------------------------------------------
# Inspector metadata (loaded once, cached for process lifetime)
# ---------------------------------------------------------------------------

IMPACT_COLORS = {
    "Critical": "danger",
    "High": "warning",
    "Medium": "info",
    "Low": "primary",
    "Informational": "secondary",
    "Review Required": "light",
}

SERVICE_DISPLAY = {
    "AzureAD": "Azure AD",
    "Exchange": "Exchange Online",
    "SharePoint": "SharePoint Online",
    "Teams": "Microsoft Teams",
    "Tenant": "Tenant Settings",
    "SecurityandCompliance": "Security & Compliance",
    "Exchange,SecurityandCompliance": "Exchange / Compliance",
    "Exchange,SharePoint,SecurityandCompliance": "Exchange / SharePoint / Compliance",
    "Exchange,Teams,SecurityandCompliance": "Exchange / Teams / Compliance",
    "Tenant,AzureAD": "Tenant / Azure AD",
    "AzureAD,Tenant": "Azure AD / Tenant",
}

SERVICE_ICONS = {
    "AzureAD": "person-badge",
    "Exchange": "envelope",
    "SharePoint": "folder2-open",
    "Teams": "chat-dots",
    "Tenant": "building-gear",
    "SecurityandCompliance": "shield-check",
}

_inspector_cache: list | None = None


def _load_inspector_metadata() -> list:
    """Load and cache metadata for all inspectors from their JSON sidecar files."""
    global _inspector_cache
    if _inspector_cache is not None:
        return _inspector_cache

    inspector_dir = INSPECTORS_DIR / "Inspectors"
    if not inspector_dir.exists():
        _inspector_cache = []
        return _inspector_cache

    inspectors = []
    for ps1 in sorted(inspector_dir.glob("*.ps1")):
        name = ps1.stem
        json_path = ps1.with_suffix(".json")
        meta = {
            "name": name,
            "finding_name": name.replace("Inspect-", "").replace("-", " "),
            "description": "",
            "impact": "Informational",
            "service": "Unknown",
        }
        if json_path.exists():
            try:
                data = json.loads(json_path.read_text(encoding="utf-8"))
                meta["finding_name"] = data.get("FindingName", meta["finding_name"])
                meta["description"] = data.get("Description", "")
                meta["impact"] = data.get("Impact", "Informational")
                meta["service"] = data.get("Service", "Unknown")
            except (json.JSONDecodeError, OSError):
                log.warning("Failed to load inspector metadata: %s", json_path)
        inspectors.append(meta)

    _inspector_cache = inspectors
    return _inspector_cache


def _get_service_icon(service: str) -> str:
    """Return a Bootstrap Icon name for a service string (uses first component for composites)."""
    first = service.split(",")[0] if "," in service else service
    return SERVICE_ICONS.get(first, "shield-check")

bp = Blueprint("audits", __name__)


# ---------------------------------------------------------------------------
# Audit Catalogue & Inspector Browser
# ---------------------------------------------------------------------------

# Shared audit-type metadata used by catalogue and batch templates
AUDIT_CATALOGUE = [
    # SPO Audits
    {
        "key": "groups", "category": "spo",
        "name": "Groups Audit", "icon": "people", "color": "primary",
        "desc": "Lists all SharePoint groups, their members, and permission levels for a specific site.",
    },
    {
        "key": "permissions", "category": "spo",
        "name": "Site Permissions", "icon": "shield-lock", "color": "warning",
        "desc": "Full snapshot of who has access — includes unique permissions at list and item level.",
    },
    {
        "key": "external", "category": "spo",
        "name": "External Sharing", "icon": "globe", "color": "info",
        "desc": "Identifies external users and anonymous sharing links on a site.",
    },
    {
        "key": "user", "category": "spo",
        "name": "User Permissions", "icon": "person-lines-fill", "color": "primary",
        "desc": "Scans all site collections for a specific user's access across the entire tenant.",
    },
    # Breach Investigation
    {
        "key": "ual", "category": "breach",
        "name": "Unified Audit Log", "icon": "journal-text", "color": "primary",
        "desc": "File access, admin actions, mailbox changes, login events.",
    },
    {
        "key": "inbox", "category": "breach",
        "name": "Inbox Rules", "icon": "inbox", "color": "danger",
        "desc": "Checks for suspicious forwarding, deletion, and redirect rules (BEC indicator).",
    },
    {
        "key": "signin", "category": "breach",
        "name": "Sign-In Logs", "icon": "box-arrow-in-right", "color": "warning",
        "desc": "Azure AD sign-in history — failed attempts, unusual locations, risky sign-ins.",
    },
    {
        "key": "oauth", "category": "breach",
        "name": "OAuth Consent", "icon": "key", "color": "info",
        "desc": "Reviews third-party app permissions — detects consent phishing.",
    },
    {
        "key": "msgtrace", "category": "breach",
        "name": "Message Trace", "icon": "envelope-arrow-up", "color": "secondary",
        "desc": "Traces email delivery — external recipients, delivery status.",
    },
]


@bp.route("/audits")
@login_required
def catalogue():
    tenants = list_tenants()
    ready_tenants = [t for t in tenants if t["client_id"]]
    inspectors = _load_inspector_metadata()

    impact_counts: dict[str, int] = {}
    for insp in inspectors:
        impact_counts[insp["impact"]] = impact_counts.get(insp["impact"], 0) + 1

    return render_template(
        "audits_catalogue.html",
        tenants=ready_tenants,
        audit_types=AUDIT_CATALOGUE,
        inspector_count=len(inspectors),
        impact_counts=impact_counts,
        impact_colors=IMPACT_COLORS,
        service_display=SERVICE_DISPLAY,
        focus_category=request.args.get("category", ""),
        focus_type=request.args.get("type", ""),
    )


@bp.route("/audits/inspectors")
@login_required
def inspectors_page():
    inspectors = _load_inspector_metadata()

    # Load full metadata from JSON sidecars
    inspector_dir = INSPECTORS_DIR / "Inspectors"
    full_inspectors = []
    for insp in inspectors:
        json_path = inspector_dir / f"{insp['name']}.json"
        full = dict(insp)
        full["remediation"] = ""
        full["references"] = []
        full["default_value"] = ""
        full["expected_value"] = ""
        full["powershell"] = ""
        if json_path.exists():
            try:
                data = json.loads(json_path.read_text(encoding="utf-8"))
                full["remediation"] = data.get("Remediation", "")
                full["references"] = data.get("References", [])
                full["default_value"] = data.get("DefaultValue", "")
                full["expected_value"] = data.get("ExpectedValue", "")
                full["powershell"] = data.get("PowerShell", "")
            except (json.JSONDecodeError, OSError):
                pass
        full_inspectors.append(full)

    service_groups: dict[str, list] = {}
    for insp in full_inspectors:
        svc = insp["service"]
        service_groups.setdefault(svc, []).append(insp)
    service_order = sorted(service_groups.keys(),
                           key=lambda s: SERVICE_DISPLAY.get(s, s))

    impact_counts: dict[str, int] = {}
    for insp in full_inspectors:
        impact_counts[insp["impact"]] = impact_counts.get(insp["impact"], 0) + 1

    return render_template(
        "audits_inspectors.html",
        inspectors=full_inspectors,
        service_groups=service_groups,
        service_order=service_order,
        impact_counts=impact_counts,
        impact_colors=IMPACT_COLORS,
        service_display=SERVICE_DISPLAY,
        service_icons=SERVICE_ICONS,
        get_service_icon=_get_service_icon,
    )


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

    if audit_type not in ("groups", "permissions", "external", "user"):
        return jsonify({"ok": False, "error": "Invalid audit type."})

    if audit_type in ("groups", "permissions", "external") and site_url:
        if not validate_site_url(site_url):
            return jsonify({"ok": False, "error": "Invalid SharePoint site URL format."})

    if audit_type == "user" and user_email:
        if not validate_email(user_email):
            return jsonify({"ok": False, "error": "Invalid email address format."})

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

    inspectors = _load_inspector_metadata()

    # Group by service
    service_groups: dict[str, list] = {}
    for insp in inspectors:
        svc = insp["service"]
        service_groups.setdefault(svc, []).append(insp)
    service_order = sorted(service_groups.keys(),
                           key=lambda s: SERVICE_DISPLAY.get(s, s))

    # Impact counts for filter badges
    impact_counts: dict[str, int] = {}
    for insp in inspectors:
        impact_counts[insp["impact"]] = impact_counts.get(insp["impact"], 0) + 1

    ready = bool(t["client_id"]) and tenant_pfx_path(tenant_id).exists()
    return render_template(
        "audit_security.html",
        tenant=t,
        inspectors=inspectors,
        service_groups=service_groups,
        service_order=service_order,
        impact_counts=impact_counts,
        impact_colors=IMPACT_COLORS,
        service_display=SERVICE_DISPLAY,
        service_icons=SERVICE_ICONS,
        get_service_icon=_get_service_icon,
        ready=ready,
    )


@bp.route("/tenants/<tenant_id>/audit/security/run", methods=["POST"])
@login_required
def audit_security_run(tenant_id):
    t = get_tenant(tenant_id)
    if not t:
        return jsonify({"ok": False, "error": "Tenant not found."}), 404

    report_format = request.form.get("report_format", "HTML")
    if report_format not in ("HTML", "CSV", "JSON", "All"):
        return jsonify({"ok": False, "error": "Invalid report format."})

    selected = [
        s for s in request.form.getlist("inspectors")
        if re.match(r'^[\w\-]+$', s)
    ]

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

    if not thumbprint:
        return jsonify({"ok": False, "error": "Failed to extract certificate thumbprint. Verify the PFX password and certificate."})

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

    if target_user and not validate_email(target_user):
        return jsonify({"ok": False, "error": "Invalid target user email format."})

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

@bp.route("/batch")
@login_required
def batch_page():
    tenants = list_tenants()
    ready_tenants = [t for t in tenants if t["client_id"]]
    return render_template("batch.html", tenants=ready_tenants)


BATCH_TYPES = {
    # Security
    "security":       "Security Scan",
    # SPO
    "spo_groups":     "Groups Audit",
    "spo_permissions": "Site Permissions",
    "spo_external":   "External Sharing",
    # Breach
    "breach_ual":     "Unified Audit Log",
    "breach_inbox":   "Inbox Rules",
    "breach_signin":  "Sign-In Logs",
    "breach_oauth":   "OAuth Consent",
    "breach_msgtrace": "Message Trace",
}

SPO_SCRIPT_MAP = {
    "spo_groups":      "Audit-Groups.ps1",
    "spo_permissions": "Audit-SitePermissions.ps1",
    "spo_external":    "Audit-ExternalSharing.ps1",
}

BREACH_SCRIPT_MAP = {
    "breach_ual":      "Search-UnifiedAuditLog.ps1",
    "breach_inbox":    "Audit-InboxRules.ps1",
    "breach_signin":   "Get-SignInLogs.ps1",
    "breach_oauth":    "Audit-OAuthConsent.ps1",
    "breach_msgtrace": "Get-MessageTrace.ps1",
}


@bp.route("/batch/audit", methods=["POST"])
@login_required
def batch_audit():
    tenant_ids  = request.form.getlist("tenant_ids")
    audit_type  = request.form.get("audit_type", "security")
    report_fmt  = request.form.get("report_format", "HTML")

    if not tenant_ids:
        return jsonify({"ok": False, "error": "No tenants selected."})

    if audit_type not in BATCH_TYPES:
        return jsonify({"ok": False, "error": "Invalid audit type."})

    if report_fmt not in ("HTML", "CSV", "JSON", "All"):
        return jsonify({"ok": False, "error": "Invalid report format."})

    batch_site_url  = request.form.get("site_url", "").strip()
    batch_target    = request.form.get("target_user", "").strip()
    batch_start     = request.form.get("date_start", "").strip()
    batch_end       = request.form.get("date_end", "").strip()

    # Validate SPO site URL if needed
    if audit_type in SPO_SCRIPT_MAP and batch_site_url:
        if not validate_site_url(batch_site_url):
            return jsonify({"ok": False, "error": "Invalid SharePoint site URL format."})
    if audit_type in SPO_SCRIPT_MAP and not batch_site_url:
        return jsonify({"ok": False, "error": "Site URL is required for SharePoint audits."})

    # Validate breach target email if provided
    if batch_target and audit_type.startswith("breach_"):
        if not validate_email(batch_target):
            return jsonify({"ok": False, "error": "Invalid target user email format."})

    job_ids = []
    tenant_jobs = []  # [{tenant_id, tenant_name, job_id}]

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
        label_name = BATCH_TYPES[audit_type]

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
        elif audit_type in SPO_SCRIPT_MAP:
            cmd = [
                PWSH, "-NoProfile", "-ExecutionPolicy", "Bypass",
                str(SCRIPTS_DIR / SPO_SCRIPT_MAP[audit_type]),
                "-SiteUrl",         batch_site_url,
                "-OutputFolder",    out_dir,
                "-ClientId",        t["client_id"],
                "-Tenant",          tenant_val,
                "-CertificatePath", pfx_path,
            ]
        elif audit_type in BREACH_SCRIPT_MAP:
            base_args = [
                "-Tenant",          tenant_val,
                "-ClientId",        t["client_id"],
                "-CertificatePath", pfx_path,
                "-OutputFolder",    out_dir,
            ]
            if batch_start:
                base_args += ["-StartDate", batch_start]
            if batch_end:
                base_args += ["-EndDate", batch_end]
            inv_key = audit_type.replace("breach_", "")
            if batch_target and inv_key in ("ual", "inbox", "signin", "msgtrace"):
                base_args += ["-TargetUser", batch_target]
            cmd = [
                PWSH, "-NoProfile", "-ExecutionPolicy", "Bypass",
                str(SCRIPTS_DIR / BREACH_SCRIPT_MAP[audit_type]),
            ] + base_args
        else:
            continue

        label = f"Batch {label_name} — {t['name']}"
        jid = start_job(label, "batch", cmd, tenant_id=tid, env=env)
        touch_tenant_audit(tid)
        job_ids.append(jid)
        tenant_jobs.append({
            "tenant_id": tid,
            "tenant_name": t["name"],
            "job_id": jid,
        })

    log_action(current_user(), "batch_audit",
               details=f"Type: {audit_type}, Tenants: {len(job_ids)}")

    return jsonify({
        "ok": True,
        "job_ids": job_ids,
        "tenant_jobs": tenant_jobs,
        "message": f"Started {len(job_ids)} {BATCH_TYPES[audit_type]} job(s).",
    })
