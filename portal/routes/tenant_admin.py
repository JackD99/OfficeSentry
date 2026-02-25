"""
portal/routes/tenant_admin.py — Tenant administration routes.
Provides tenant lookup, domains, authentication methods, partner relationships,
app consent requests, application management, directory audit logs,
and tenant custom variables.
Inspired by CIPP's tenant administration section.
"""

import json
import logging

from flask import (
    Blueprint, render_template, request, jsonify, abort,
)

from database import (
    get_tenant, list_tenants, log_action,
    save_cached_data, get_cached_data,
    set_tenant_variable, get_tenant_variables, delete_tenant_variable,
)
from helpers import (
    login_required, current_user,
    tenant_pfx_path, build_ps_env, start_job, get_tenant_pfx_password,
    sanitize_text,
    SCRIPTS_DIR, PWSH,
)

log = logging.getLogger("portal")

bp = Blueprint("tenant_admin", __name__)


# =========================================================================
# Tenant Lookup (public Microsoft API — no cert needed)
# =========================================================================

@bp.route("/tenant-lookup")
@login_required
def tenant_lookup_page():
    return render_template("tenant_lookup.html")


@bp.route("/tenant-lookup/query", methods=["POST"])
@login_required
def tenant_lookup_query():
    """Look up a tenant via Microsoft's public OpenID configuration endpoint."""
    domain = request.form.get("domain", "").strip().lower()
    if not domain:
        return jsonify({"ok": False, "error": "Domain is required."})

    import urllib.request
    import urllib.error

    # Microsoft's public tenant discovery endpoint
    url = f"https://login.microsoftonline.com/{domain}/.well-known/openid-configuration"
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            data = json.loads(resp.read().decode())

        tenant_id = data.get("token_endpoint", "").split("/")[3] if data.get("token_endpoint") else ""
        region = data.get("tenant_region_scope", "Unknown")
        cloud = data.get("cloud_instance_name", "")

        # Also try the v2 endpoint for more info
        result = {
            "ok": True,
            "domain": domain,
            "tenantId": tenant_id,
            "region": region,
            "cloudInstance": cloud or data.get("cloud_graph_host_name", ""),
            "authorizationEndpoint": data.get("authorization_endpoint", ""),
            "tokenEndpoint": data.get("token_endpoint", ""),
            "issuer": data.get("issuer", ""),
        }
        return jsonify(result)
    except urllib.error.HTTPError as e:
        if e.code == 400:
            return jsonify({"ok": False, "error": f"Tenant '{domain}' not found."})
        return jsonify({"ok": False, "error": f"HTTP {e.code}: {e.reason}"})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)})


# =========================================================================
# Domains
# =========================================================================

@bp.route("/tenants/<tenant_id>/domains")
@login_required
def tenant_domains(tenant_id):
    t = get_tenant(tenant_id)
    if not t:
        abort(404)
    cached = get_cached_data(tenant_id, "domains")
    return render_template("tenant_domains.html", tenant=t, cached=cached)


@bp.route("/tenants/<tenant_id>/domains/fetch", methods=["POST"])
@login_required
def tenant_domains_fetch(tenant_id):
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
        str(SCRIPTS_DIR / "Get-TenantDomains.ps1"),
        "-Tenant", tenant_val,
        "-ClientId", t["client_id"],
        "-CertificatePath", pfx_path,
    ]

    jid = start_job(f"Fetch Domains — {t['name']}", "tenant_admin", cmd,
                    tenant_id=tenant_id, env=env)
    log_action(current_user(), "domains_fetch",
               tenant_id=tenant_id, tenant_name=t["name"])
    return jsonify({"ok": True, "job_id": jid})


# =========================================================================
# Authentication Methods
# =========================================================================

@bp.route("/tenants/<tenant_id>/auth-methods")
@login_required
def tenant_auth_methods(tenant_id):
    t = get_tenant(tenant_id)
    if not t:
        abort(404)
    cached = get_cached_data(tenant_id, "auth_methods")
    return render_template("tenant_auth_methods.html", tenant=t, cached=cached)


@bp.route("/tenants/<tenant_id>/auth-methods/fetch", methods=["POST"])
@login_required
def tenant_auth_methods_fetch(tenant_id):
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
        str(SCRIPTS_DIR / "Get-AuthMethods.ps1"),
        "-Tenant", tenant_val,
        "-ClientId", t["client_id"],
        "-CertificatePath", pfx_path,
    ]

    jid = start_job(f"Fetch Auth Methods — {t['name']}", "tenant_admin", cmd,
                    tenant_id=tenant_id, env=env)
    log_action(current_user(), "auth_methods_fetch",
               tenant_id=tenant_id, tenant_name=t["name"])
    return jsonify({"ok": True, "job_id": jid})


# =========================================================================
# Partner Relationships (GDAP/DAP)
# =========================================================================

@bp.route("/tenants/<tenant_id>/partner-relationships")
@login_required
def tenant_partner_relationships(tenant_id):
    t = get_tenant(tenant_id)
    if not t:
        abort(404)
    cached = get_cached_data(tenant_id, "partner_relationships")
    return render_template("tenant_partner_relationships.html", tenant=t, cached=cached)


@bp.route("/tenants/<tenant_id>/partner-relationships/fetch", methods=["POST"])
@login_required
def tenant_partner_relationships_fetch(tenant_id):
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
        str(SCRIPTS_DIR / "Get-PartnerRelationships.ps1"),
        "-Tenant", tenant_val,
        "-ClientId", t["client_id"],
        "-CertificatePath", pfx_path,
    ]

    jid = start_job(f"Fetch Partner Relationships — {t['name']}", "tenant_admin", cmd,
                    tenant_id=tenant_id, env=env)
    log_action(current_user(), "partner_relationships_fetch",
               tenant_id=tenant_id, tenant_name=t["name"])
    return jsonify({"ok": True, "job_id": jid})


# =========================================================================
# App Consent Requests
# =========================================================================

@bp.route("/tenants/<tenant_id>/app-consent")
@login_required
def tenant_app_consent(tenant_id):
    t = get_tenant(tenant_id)
    if not t:
        abort(404)
    cached = get_cached_data(tenant_id, "app_consent_requests")
    return render_template("tenant_app_consent.html", tenant=t, cached=cached)


@bp.route("/tenants/<tenant_id>/app-consent/fetch", methods=["POST"])
@login_required
def tenant_app_consent_fetch(tenant_id):
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
        str(SCRIPTS_DIR / "Get-AppConsentRequests.ps1"),
        "-Tenant", tenant_val,
        "-ClientId", t["client_id"],
        "-CertificatePath", pfx_path,
    ]

    jid = start_job(f"Fetch App Consent — {t['name']}", "tenant_admin", cmd,
                    tenant_id=tenant_id, env=env)
    log_action(current_user(), "app_consent_fetch",
               tenant_id=tenant_id, tenant_name=t["name"])
    return jsonify({"ok": True, "job_id": jid})


# =========================================================================
# Application Management (App Registrations + Enterprise Apps)
# =========================================================================

@bp.route("/tenants/<tenant_id>/applications")
@login_required
def tenant_applications(tenant_id):
    t = get_tenant(tenant_id)
    if not t:
        abort(404)
    cached_apps = get_cached_data(tenant_id, "app_registrations")
    cached_sp = get_cached_data(tenant_id, "enterprise_apps")
    return render_template("tenant_applications.html", tenant=t,
                           cached_apps=cached_apps, cached_sp=cached_sp)


@bp.route("/tenants/<tenant_id>/applications/fetch", methods=["POST"])
@login_required
def tenant_applications_fetch(tenant_id):
    t = get_tenant(tenant_id)
    if not t or not t["client_id"]:
        return jsonify({"ok": False, "error": "Tenant not configured."})

    data_type = request.form.get("data_type", "both")
    if data_type not in ("appRegistrations", "enterpriseApps", "both"):
        data_type = "both"

    pfx_pass = get_tenant_pfx_password(t)
    if not pfx_pass:
        return jsonify({"ok": False, "error": "PFX password not configured."})

    tenant_val = t["tenant_id"] or t["domain"]
    pfx_path = str(tenant_pfx_path(tenant_id))
    env = build_ps_env(pfx_pass)

    cmd = [
        PWSH, "-NoProfile", "-ExecutionPolicy", "Bypass",
        str(SCRIPTS_DIR / "Get-AppRegistrations.ps1"),
        "-Tenant", tenant_val,
        "-ClientId", t["client_id"],
        "-CertificatePath", pfx_path,
        "-DataType", data_type,
    ]

    jid = start_job(f"Fetch Applications — {t['name']}", "tenant_admin", cmd,
                    tenant_id=tenant_id, env=env)
    log_action(current_user(), "applications_fetch",
               tenant_id=tenant_id, tenant_name=t["name"],
               details=f"Type: {data_type}")
    return jsonify({"ok": True, "job_id": jid})


# =========================================================================
# Directory Audit Logs (Entra ID)
# =========================================================================

@bp.route("/tenants/<tenant_id>/directory-audits")
@login_required
def tenant_directory_audits(tenant_id):
    t = get_tenant(tenant_id)
    if not t:
        abort(404)
    cached = get_cached_data(tenant_id, "directory_audits")
    return render_template("tenant_directory_audits.html", tenant=t, cached=cached)


@bp.route("/tenants/<tenant_id>/directory-audits/fetch", methods=["POST"])
@login_required
def tenant_directory_audits_fetch(tenant_id):
    t = get_tenant(tenant_id)
    if not t or not t["client_id"]:
        return jsonify({"ok": False, "error": "Tenant not configured."})

    pfx_pass = get_tenant_pfx_password(t)
    if not pfx_pass:
        return jsonify({"ok": False, "error": "PFX password not configured."})

    tenant_val = t["tenant_id"] or t["domain"]
    pfx_path = str(tenant_pfx_path(tenant_id))
    env = build_ps_env(pfx_pass)

    start_date = request.form.get("start_date", "").strip()
    end_date = request.form.get("end_date", "").strip()

    cmd = [
        PWSH, "-NoProfile", "-ExecutionPolicy", "Bypass",
        str(SCRIPTS_DIR / "Get-DirectoryAuditLogs.ps1"),
        "-Tenant", tenant_val,
        "-ClientId", t["client_id"],
        "-CertificatePath", pfx_path,
    ]
    if start_date:
        cmd += ["-StartDate", start_date]
    if end_date:
        cmd += ["-EndDate", end_date]

    jid = start_job(f"Fetch Directory Audits — {t['name']}", "tenant_admin", cmd,
                    tenant_id=tenant_id, env=env)
    log_action(current_user(), "directory_audits_fetch",
               tenant_id=tenant_id, tenant_name=t["name"])
    return jsonify({"ok": True, "job_id": jid})


# =========================================================================
# Tenant Custom Variables (CIPP Global Variables)
# =========================================================================

@bp.route("/tenant-variables")
@login_required
def tenant_variables_page():
    tenants = list_tenants()
    all_vars = {}
    for t in tenants:
        tvars = get_tenant_variables(t["id"])
        if tvars:
            all_vars[t["id"]] = tvars
    return render_template("tenant_variables.html", tenants=tenants, all_vars=all_vars)


@bp.route("/tenants/<tenant_id>/variables", methods=["GET"])
@login_required
def tenant_variables_list(tenant_id):
    t = get_tenant(tenant_id)
    if not t:
        return jsonify({"ok": False, "error": "Tenant not found."}), 404
    variables = get_tenant_variables(tenant_id)
    return jsonify({"ok": True, "variables": variables})


@bp.route("/tenants/<tenant_id>/variables/set", methods=["POST"])
@login_required
def tenant_variable_set(tenant_id):
    t = get_tenant(tenant_id)
    if not t:
        return jsonify({"ok": False, "error": "Tenant not found."}), 404

    var_key = sanitize_text(request.form.get("var_key", ""), max_length=100)
    var_value = sanitize_text(request.form.get("var_value", ""), max_length=2000)

    if not var_key:
        return jsonify({"ok": False, "error": "Variable key is required."})

    set_tenant_variable(tenant_id, var_key, var_value, created_by=current_user())
    log_action(current_user(), "variable_set",
               tenant_id=tenant_id, tenant_name=t["name"],
               details=f"Key: {var_key}")
    return jsonify({"ok": True, "message": f"Variable '{var_key}' saved."})


@bp.route("/tenants/<tenant_id>/variables/delete", methods=["POST"])
@login_required
def tenant_variable_delete(tenant_id):
    t = get_tenant(tenant_id)
    if not t:
        return jsonify({"ok": False, "error": "Tenant not found."}), 404

    var_key = request.form.get("var_key", "").strip()
    if not var_key:
        return jsonify({"ok": False, "error": "Variable key is required."})

    deleted = delete_tenant_variable(tenant_id, var_key)
    if not deleted:
        return jsonify({"ok": False, "error": "Variable not found."})

    log_action(current_user(), "variable_deleted",
               tenant_id=tenant_id, tenant_name=t["name"],
               details=f"Key: {var_key}")
    return jsonify({"ok": True, "message": f"Variable '{var_key}' deleted."})


# =========================================================================
# Cache import endpoint (called after PS jobs complete)
# =========================================================================

@bp.route("/tenants/<tenant_id>/cache-import", methods=["POST"])
@login_required
def tenant_cache_import(tenant_id):
    """Import fetched data into the cache. Called by frontend after job completes."""
    t = get_tenant(tenant_id)
    if not t:
        return jsonify({"ok": False, "error": "Tenant not found."}), 404

    data_type = request.form.get("data_type", "").strip()
    data_json = request.form.get("data", "")

    if not data_type or not data_json:
        return jsonify({"ok": False, "error": "data_type and data are required."})

    try:
        data = json.loads(data_json)
    except json.JSONDecodeError:
        return jsonify({"ok": False, "error": "Invalid JSON."})

    save_cached_data(tenant_id, data_type, data)
    return jsonify({"ok": True, "message": f"Cached {data_type} data."})
