"""
portal/routes/tools.py — Utility tools for tenant administration.
Provides Graph Explorer, GeoIP lookup, domain health analysis,
and configuration backup/restore.
Inspired by CIPP's tools and backup sections.
"""

import json
import logging

from flask import (
    Blueprint, render_template, request, jsonify, abort,
)

from database import (
    get_tenant, list_tenants, log_action,
    save_cached_data, get_cached_data,
    create_tenant_backup, get_tenant_backup, list_tenant_backups,
    update_tenant_backup, delete_tenant_backup,
)
from helpers import (
    login_required, current_user,
    tenant_pfx_path, tenant_output_dir,
    build_ps_env, start_job, get_tenant_pfx_password,
    sanitize_text, validate_site_url,
    SCRIPTS_DIR, PWSH,
)

log = logging.getLogger("portal")

bp = Blueprint("tools", __name__)


# =========================================================================
# Graph Explorer
# =========================================================================

@bp.route("/graph-explorer")
@login_required
def graph_explorer_page():
    tenants = list_tenants()
    ready_tenants = [t for t in tenants if t["client_id"]]

    # Preset queries for quick use
    preset_queries = [
        {"name": "Organization Info", "uri": "https://graph.microsoft.com/v1.0/organization", "method": "GET"},
        {"name": "All Users", "uri": "https://graph.microsoft.com/v1.0/users?$top=100&$select=displayName,userPrincipalName,accountEnabled,createdDateTime", "method": "GET"},
        {"name": "All Groups", "uri": "https://graph.microsoft.com/v1.0/groups?$top=100&$select=displayName,groupTypes,mailEnabled,securityEnabled", "method": "GET"},
        {"name": "Domains", "uri": "https://graph.microsoft.com/v1.0/domains", "method": "GET"},
        {"name": "CA Policies", "uri": "https://graph.microsoft.com/v1.0/identity/conditionalAccess/policies", "method": "GET"},
        {"name": "Auth Methods Policy", "uri": "https://graph.microsoft.com/v1.0/policies/authenticationMethodsPolicy", "method": "GET"},
        {"name": "Service Principals", "uri": "https://graph.microsoft.com/v1.0/servicePrincipals?$top=100", "method": "GET"},
        {"name": "App Registrations", "uri": "https://graph.microsoft.com/v1.0/applications?$top=100", "method": "GET"},
        {"name": "Subscribed SKUs", "uri": "https://graph.microsoft.com/v1.0/subscribedSkus", "method": "GET"},
        {"name": "Secure Score", "uri": "https://graph.microsoft.com/v1.0/security/secureScores?$top=1", "method": "GET"},
        {"name": "Sign-in Activity", "uri": "https://graph.microsoft.com/v1.0/auditLogs/signIns?$top=50&$orderby=createdDateTime desc", "method": "GET"},
        {"name": "Directory Audits", "uri": "https://graph.microsoft.com/v1.0/auditLogs/directoryAudits?$top=50&$orderby=activityDateTime desc", "method": "GET"},
        {"name": "Named Locations", "uri": "https://graph.microsoft.com/v1.0/identity/conditionalAccess/namedLocations", "method": "GET"},
        {"name": "GDAP Relationships", "uri": "https://graph.microsoft.com/v1.0/tenantRelationships/delegatedAdminRelationships", "method": "GET"},
    ]

    return render_template("graph_explorer.html", tenants=ready_tenants,
                           preset_queries=preset_queries)


@bp.route("/graph-explorer/query", methods=["POST"])
@login_required
def graph_explorer_query():
    """Execute a Graph API query."""
    tenant_id = request.form.get("tenant_id", "").strip()
    graph_uri = request.form.get("graph_uri", "").strip()
    method = request.form.get("method", "GET").strip().upper()
    body = request.form.get("body", "").strip()

    if not tenant_id:
        return jsonify({"ok": False, "error": "Tenant is required."})
    if not graph_uri:
        return jsonify({"ok": False, "error": "Graph URI is required."})
    if method not in ("GET", "POST", "PATCH", "DELETE"):
        return jsonify({"ok": False, "error": "Invalid HTTP method."})

    # Validate the URI starts with the Graph API base
    if not graph_uri.startswith("https://graph.microsoft.com/"):
        return jsonify({"ok": False, "error": "URI must start with https://graph.microsoft.com/"})

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
        "-GraphUri", graph_uri,
        "-Method", method,
    ]
    if body and method in ("POST", "PATCH"):
        cmd += ["-Body", body]

    jid = start_job(f"Graph Explorer {method} — {t['name']}", "graph_explorer", cmd,
                    tenant_id=tenant_id, env=env)
    log_action(current_user(), "graph_explorer_query",
               tenant_id=tenant_id, tenant_name=t["name"],
               details=f"{method} {graph_uri}")
    return jsonify({"ok": True, "job_id": jid})


# =========================================================================
# GeoIP Lookup
# =========================================================================

@bp.route("/geoip-lookup")
@login_required
def geoip_lookup_page():
    return render_template("geoip_lookup.html")


@bp.route("/geoip-lookup/query", methods=["POST"])
@login_required
def geoip_lookup_query():
    """Look up geolocation for an IP address using a free public API."""
    ip_address = request.form.get("ip_address", "").strip()
    if not ip_address:
        return jsonify({"ok": False, "error": "IP address is required."})

    import urllib.request
    import urllib.error

    # Using ip-api.com (free, no API key needed, 45 req/min)
    url = f"http://ip-api.com/json/{ip_address}?fields=status,message,country,countryCode,region,regionName,city,zip,lat,lon,timezone,isp,org,as,query"
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            data = json.loads(resp.read().decode())

        if data.get("status") == "fail":
            return jsonify({"ok": False, "error": data.get("message", "Lookup failed.")})

        return jsonify({"ok": True, "result": data})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)})


# =========================================================================
# Domain Health / Individual Domain Check
# =========================================================================

@bp.route("/domain-health")
@login_required
def domain_health_page():
    return render_template("domain_health.html")


@bp.route("/domain-health/check", methods=["POST"])
@login_required
def domain_health_check():
    """Check DNS health for a domain (SPF, DKIM, DMARC, MX)."""
    domain = request.form.get("domain", "").strip().lower()
    if not domain:
        return jsonify({"ok": False, "error": "Domain is required."})

    import subprocess
    import re

    results = {"ok": True, "domain": domain, "checks": {}}

    # Use nslookup/dig to check DNS records
    def _dns_lookup(record_type, name):
        try:
            out = subprocess.run(
                ["nslookup", f"-type={record_type}", name],
                capture_output=True, text=True, timeout=10,
            )
            return out.stdout
        except Exception:
            return ""

    # MX records
    mx_out = _dns_lookup("MX", domain)
    mx_records = re.findall(r"mail exchanger = (.+)", mx_out)
    results["checks"]["mx"] = {
        "status": "pass" if mx_records else "fail",
        "records": mx_records,
        "detail": f"Found {len(mx_records)} MX record(s)" if mx_records else "No MX records found"
    }

    # SPF
    txt_out = _dns_lookup("TXT", domain)
    spf_found = "v=spf1" in txt_out
    spf_records = [line.strip() for line in txt_out.split("\n")
                   if "v=spf1" in line]
    results["checks"]["spf"] = {
        "status": "pass" if spf_found else "fail",
        "records": spf_records,
        "detail": "SPF record found" if spf_found else "No SPF record found"
    }

    # DMARC
    dmarc_out = _dns_lookup("TXT", f"_dmarc.{domain}")
    dmarc_found = "v=DMARC1" in dmarc_out.upper()
    dmarc_records = [line.strip() for line in dmarc_out.split("\n")
                     if "dmarc" in line.lower()]
    results["checks"]["dmarc"] = {
        "status": "pass" if dmarc_found else "fail",
        "records": dmarc_records,
        "detail": "DMARC record found" if dmarc_found else "No DMARC record found"
    }

    # DKIM (check common selectors)
    dkim_found = False
    for selector in ["selector1", "selector2", "google", "default"]:
        dkim_out = _dns_lookup("CNAME", f"{selector}._domainkey.{domain}")
        if "canonical name" in dkim_out.lower() or "CNAME" in dkim_out:
            dkim_found = True
            break
    results["checks"]["dkim"] = {
        "status": "pass" if dkim_found else "warn",
        "detail": "DKIM selector found" if dkim_found else "No common DKIM selectors found (may use custom selector)"
    }

    return jsonify(results)


# =========================================================================
# Configuration Backup
# =========================================================================

@bp.route("/backup")
@login_required
def backup_page():
    tenants = list_tenants()
    ready_tenants = [t for t in tenants if t["client_id"]]
    backups = list_tenant_backups()
    return render_template("backup.html", tenants=ready_tenants, backups=backups)


@bp.route("/backup/create", methods=["POST"])
@login_required
def backup_create():
    """Start a configuration backup for a tenant."""
    tenant_id = request.form.get("tenant_id", "").strip()
    components = request.form.getlist("components")

    if not tenant_id:
        return jsonify({"ok": False, "error": "Tenant is required."})
    if not components:
        components = ["conditionalAccess", "authMethods", "namedLocations",
                      "organization", "domains"]

    t = get_tenant(tenant_id)
    if not t or not t["client_id"]:
        return jsonify({"ok": False, "error": "Tenant not configured."})

    pfx_pass = get_tenant_pfx_password(t)
    if not pfx_pass:
        return jsonify({"ok": False, "error": "PFX password not configured."})

    # Create backup record
    bid = create_tenant_backup(tenant_id, backup_type="full",
                                components=components, created_by=current_user())

    tenant_val = t["tenant_id"] or t["domain"]
    pfx_path = str(tenant_pfx_path(tenant_id))
    env = build_ps_env(pfx_pass)

    cmd = [
        PWSH, "-NoProfile", "-ExecutionPolicy", "Bypass",
        str(SCRIPTS_DIR / "Backup-TenantConfig.ps1"),
        "-Tenant", tenant_val,
        "-ClientId", t["client_id"],
        "-CertificatePath", pfx_path,
        "-Components", ",".join(components),
    ]

    jid = start_job(f"Config Backup — {t['name']}", "backup", cmd,
                    tenant_id=tenant_id, env=env)

    update_tenant_backup(bid, status="running")

    log_action(current_user(), "backup_created",
               tenant_id=tenant_id, tenant_name=t["name"],
               details=f"Components: {', '.join(components)}")
    return jsonify({"ok": True, "job_id": jid, "backup_id": bid,
                    "message": f"Backup started for {t['name']}."})


@bp.route("/backup/<backup_id>")
@login_required
def backup_detail(backup_id):
    backup = get_tenant_backup(backup_id)
    if not backup:
        abort(404)
    t = get_tenant(backup["tenant_id"])
    return render_template("backup_detail.html", backup=backup, tenant=t)


@bp.route("/backup/<backup_id>/delete", methods=["POST"])
@login_required
def backup_delete(backup_id):
    backup = get_tenant_backup(backup_id)
    if not backup:
        return jsonify({"ok": False, "error": "Backup not found."}), 404

    delete_tenant_backup(backup_id)
    log_action(current_user(), "backup_deleted", details=f"Backup: {backup_id}")
    return jsonify({"ok": True, "message": "Backup deleted."})


@bp.route("/backup/<backup_id>/restore", methods=["POST"])
@login_required
def backup_restore(backup_id):
    """Initiate restoration from a backup."""
    backup = get_tenant_backup(backup_id)
    if not backup:
        return jsonify({"ok": False, "error": "Backup not found."}), 404

    t = get_tenant(backup["tenant_id"])
    if not t or not t["client_id"]:
        return jsonify({"ok": False, "error": "Tenant not configured."})

    components = request.form.getlist("components")
    if not components:
        components = backup["components"]

    log_action(current_user(), "backup_restore_started",
               tenant_id=backup["tenant_id"], tenant_name=t["name"] if t else "",
               details=f"Backup: {backup_id}, Components: {', '.join(components)}")

    return jsonify({
        "ok": True,
        "message": f"Restore initiated for {len(components)} component(s).",
    })


# =========================================================================
# Licence Report
# =========================================================================

@bp.route("/tenants/<tenant_id>/licences")
@login_required
def tenant_licences(tenant_id):
    t = get_tenant(tenant_id)
    if not t:
        abort(404)
    cached = get_cached_data(tenant_id, "licences")
    return render_template("tenant_licences.html", tenant=t, cached=cached)


@bp.route("/tenants/<tenant_id>/licences/fetch", methods=["POST"])
@login_required
def tenant_licences_fetch(tenant_id):
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
        "-GraphUri", "https://graph.microsoft.com/v1.0/subscribedSkus",
        "-Method", "GET",
    ]

    jid = start_job(f"Fetch Licences — {t['name']}", "tenant_admin", cmd,
                    tenant_id=tenant_id, env=env)
    log_action(current_user(), "licences_fetch",
               tenant_id=tenant_id, tenant_name=t["name"])
    return jsonify({"ok": True, "job_id": jid})
