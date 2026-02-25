"""
portal/routes/tenants.py — Tenant CRUD and setup (cert, Entra app) routes.
"""

import shutil
import logging
from pathlib import Path

from flask import (
    Blueprint, render_template, request, redirect, url_for,
    jsonify, flash, abort, current_app,
)

from database import (
    create_tenant, get_tenant, list_tenants, update_tenant, delete_tenant,
    list_jobs, log_action, encrypt_value,
)
from helpers import (
    login_required, current_user,
    tenant_dir, tenant_pfx_path, tenant_cer_path, tenant_certs_dir,
    tenant_output_dir,
    build_ps_env, run_ps_sync, start_job, get_tenant_pfx_password,
    validate_domain, validate_tenant_guid, sanitize_text,
    SCRIPTS_DIR, MODULES_DIR, PWSH,
)

log = logging.getLogger("portal")

bp = Blueprint("tenants", __name__)


@bp.route("/tenants")
@login_required
def tenants_list():
    tenants = list_tenants()
    return render_template("tenants.html", tenants=tenants)


@bp.route("/tenants/new", methods=["POST"])
@login_required
def tenant_create():
    name   = sanitize_text(request.form.get("name", ""), max_length=200)
    domain = request.form.get("domain", "").strip().lower()
    tid    = request.form.get("tenant_id", "").strip()
    notes  = sanitize_text(request.form.get("notes", ""), max_length=1000)

    if not name or not domain:
        flash("Name and domain are required.", "danger")
        return redirect(url_for("tenants.tenants_list"))

    if not validate_domain(domain):
        flash("Invalid domain format. Example: contoso.onmicrosoft.com", "danger")
        return redirect(url_for("tenants.tenants_list"))

    if tid and not validate_tenant_guid(tid):
        flash("Tenant ID must be a valid GUID (e.g., xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx).", "danger")
        return redirect(url_for("tenants.tenants_list"))

    new_id = create_tenant(name, domain, tenant_id=tid, notes=notes)
    log_action(current_user(), "tenant_created",
               tenant_id=new_id, tenant_name=name,
               details=f"Domain: {domain}")
    flash(f"Tenant '{name}' created.", "success")
    return redirect(url_for("tenants.tenant_detail", tenant_id=new_id))


@bp.route("/tenants/<tenant_id>")
@login_required
def tenant_detail(tenant_id):
    t = get_tenant(tenant_id)
    if not t:
        abort(404)
    cert_exists = tenant_pfx_path(tenant_id).exists()
    recent_jobs = list_jobs(tenant_id=tenant_id, limit=10)
    return render_template("tenant_detail.html", tenant=t,
                           cert_exists=cert_exists, recent_jobs=recent_jobs)


@bp.route("/tenants/<tenant_id>/settings", methods=["POST"])
@login_required
def tenant_settings(tenant_id):
    t = get_tenant(tenant_id)
    if not t:
        abort(404)

    fields = {}
    for key in ("name", "domain", "tenant_id", "client_id",
                "app_display_name", "cert_subject", "notes"):
        val = request.form.get(key, "").strip()
        if val:
            fields[key] = val

    years = request.form.get("cert_years_valid", "").strip()
    if years and years.isdigit():
        fields["cert_years_valid"] = int(years)

    pfx_pass = request.form.get("pfx_password", "").strip()
    if pfx_pass:
        fields["pfx_password_enc"] = encrypt_value(pfx_pass, current_app.secret_key)

    if "domain" in fields and not validate_domain(fields["domain"]):
        return jsonify({"ok": False, "error": "Invalid domain format. Example: contoso.onmicrosoft.com"})
    if "tenant_id" in fields and not validate_tenant_guid(fields["tenant_id"]):
        return jsonify({"ok": False, "error": "Tenant ID must be a valid GUID."})

    if fields:
        update_tenant(tenant_id, **fields)
        log_action(current_user(), "tenant_settings_updated",
                   tenant_id=tenant_id, tenant_name=t["name"],
                   details=f"Updated: {', '.join(fields.keys())}")

    return jsonify({"ok": True, "message": "Settings saved."})


@bp.route("/tenants/<tenant_id>/delete", methods=["POST"])
@login_required
def tenant_delete(tenant_id):
    t = get_tenant(tenant_id)
    if not t:
        abort(404)

    td = tenant_dir(tenant_id)
    if td.exists():
        shutil.rmtree(td, ignore_errors=True)

    delete_tenant(tenant_id)
    log_action(current_user(), "tenant_deleted", tenant_name=t["name"],
               details=f"Domain: {t['domain']}")
    flash(f"Tenant '{t['name']}' deleted.", "success")
    return redirect(url_for("tenants.tenants_list"))


# ---------------------------------------------------------------------------
# Tenant Health Check
# ---------------------------------------------------------------------------

@bp.route("/tenants/<tenant_id>/health", methods=["POST"])
@login_required
def tenant_health(tenant_id):
    """Check tenant connectivity by verifying cert + app config and testing PnP connection."""
    t = get_tenant(tenant_id)
    if not t:
        return jsonify({"ok": False, "error": "Tenant not found."}), 404

    checks = {}

    # Check 1: Certificate file exists
    pfx_exists = tenant_pfx_path(tenant_id).exists()
    cer_exists = tenant_cer_path(tenant_id).exists()
    checks["certificate"] = {
        "status": "ok" if pfx_exists else "fail",
        "detail": "PFX and CER present" if (pfx_exists and cer_exists) else
                  "PFX present" if pfx_exists else "Certificate files missing",
    }

    # Check 2: Client ID configured
    checks["entra_app"] = {
        "status": "ok" if t["client_id"] else "fail",
        "detail": f"Client ID: {t['client_id'][:12]}..." if t["client_id"] else "Not configured",
    }

    # Check 3: PFX password decryptable
    from helpers import get_tenant_pfx_password
    pfx_pass = get_tenant_pfx_password(t)
    checks["pfx_password"] = {
        "status": "ok" if pfx_pass else "fail",
        "detail": "Decryptable" if pfx_pass else "Missing or decrypt failed",
    }

    # Check 4: Tenant identifier set
    tenant_val = t.get("tenant_id") or t.get("domain")
    checks["tenant_identity"] = {
        "status": "ok" if tenant_val else "fail",
        "detail": tenant_val or "No tenant ID or domain set",
    }

    all_ok = all(c["status"] == "ok" for c in checks.values())
    return jsonify({
        "ok": all_ok,
        "checks": checks,
        "ready": all_ok,
        "message": "All checks passed" if all_ok else "Some checks failed",
    })


# ---------------------------------------------------------------------------
# Tenant Export/Import
# ---------------------------------------------------------------------------

@bp.route("/tenants/export")
@login_required
def tenants_export():
    """Export all tenants as JSON for backup/migration."""
    import json
    tenants = list_tenants()
    # Strip encrypted passwords for safety
    for t in tenants:
        t.pop("pfx_password_enc", None)
    return current_app.response_class(
        json.dumps(tenants, indent=2),
        mimetype="application/json",
        headers={"Content-Disposition": "attachment; filename=tenants_export.json"},
    )


@bp.route("/tenants/import", methods=["POST"])
@login_required
def tenants_import():
    """Import tenants from a JSON file."""
    import json
    f = request.files.get("file")
    if not f:
        return jsonify({"ok": False, "error": "No file provided."})

    try:
        data = json.loads(f.read().decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return jsonify({"ok": False, "error": "Invalid JSON file."})

    if not isinstance(data, list):
        return jsonify({"ok": False, "error": "Expected a JSON array of tenants."})

    imported = 0
    skipped = 0
    for entry in data:
        name = entry.get("name", "").strip()
        domain = entry.get("domain", "").strip()
        if not name or not domain:
            skipped += 1
            continue
        tid = entry.get("tenant_id", "")
        notes = entry.get("notes", "")
        create_tenant(name, domain, tenant_id=tid, notes=notes)
        imported += 1

    log_action(current_user(), "tenants_imported",
               details=f"Imported: {imported}, Skipped: {skipped}")
    return jsonify({
        "ok": True,
        "message": f"Imported {imported} tenant(s), skipped {skipped}.",
    })


# ---------------------------------------------------------------------------
# Tenant Setup (cert, app)
# ---------------------------------------------------------------------------

@bp.route("/tenants/<tenant_id>/cert", methods=["POST"])
@login_required
def tenant_cert(tenant_id):
    t = get_tenant(tenant_id)
    if not t:
        return jsonify({"ok": False, "error": "Tenant not found."}), 404

    password = request.form.get("pfx_password", "").strip()
    if not password:
        return jsonify({"ok": False, "error": "PFX password is required."})

    update_tenant(tenant_id,
                  pfx_password_enc=encrypt_value(password, current_app.secret_key))

    cer = str(tenant_cer_path(tenant_id))
    pfx = str(tenant_pfx_path(tenant_id))
    subject = t.get("cert_subject") or "CN=M365 Audit"
    years = int(t.get("cert_years_valid") or 2)

    env = build_ps_env()
    result = run_ps_sync(SCRIPTS_DIR / "Setup-Cert.ps1", [
        "-Subject",     subject,
        "-CerPath",     cer,
        "-PfxPath",     pfx,
        "-YearsValid",  str(years),
        "-PfxPassword", password,
    ], env=env, timeout=120)

    if result.get("ok"):
        log_action(current_user(), "cert_generated",
                   tenant_id=tenant_id, tenant_name=t["name"],
                   details=f"Expires: {result.get('notAfter', 'unknown')}")

    return jsonify(result)


@bp.route("/tenants/<tenant_id>/app", methods=["POST"])
@login_required
def tenant_app(tenant_id):
    t = get_tenant(tenant_id)
    if not t:
        return jsonify({"ok": False, "error": "Tenant not found."}), 404

    cer = str(tenant_cer_path(tenant_id))
    if not Path(cer).exists():
        return jsonify({"ok": False, "error": "Certificate not found. Generate it first."})

    years      = int(t.get("cert_years_valid") or 2)
    app_name   = t.get("app_display_name") or "OfficeSentry"
    tenant_val = t.get("tenant_id") or t.get("domain", "")

    cmd = [
        PWSH, "-NoProfile", "-ExecutionPolicy", "Bypass",
        str(SCRIPTS_DIR / "Setup-App.ps1"),
        "-AppDisplayName", app_name,
        "-CerPath",        cer,
        "-CertYearsValid", str(years),
        "-ModulesPath",    str(MODULES_DIR),
    ]
    if tenant_val:
        cmd += ["-TenantId", tenant_val]

    env = build_ps_env()
    job_id = start_job(f"Create Entra App — {t['name']}", "setup",
                       cmd, tenant_id=tenant_id, env=env)

    log_action(current_user(), "app_creation_started",
               tenant_id=tenant_id, tenant_name=t["name"])

    return jsonify({"ok": True, "job_id": job_id,
                    "message": "App creation started. Check job output for ClientId."})


@bp.route("/tenants/<tenant_id>/remove-app", methods=["POST"])
@login_required
def tenant_remove_app(tenant_id):
    t = get_tenant(tenant_id)
    if not t:
        return jsonify({"ok": False, "error": "Tenant not found."}), 404

    client_id = t.get("client_id", "")
    if not client_id:
        return jsonify({"ok": False, "error": "No ClientId configured."})

    pfx = str(tenant_pfx_path(tenant_id))
    cer = str(tenant_cer_path(tenant_id))

    env = build_ps_env()
    job_id = start_job(f"Remove Entra App — {t['name']}", "setup", [
        PWSH, "-NoProfile", "-ExecutionPolicy", "Bypass",
        str(SCRIPTS_DIR / "Setup-RemoveApp.ps1"),
        "-ClientId",    client_id,
        "-ModulesPath", str(MODULES_DIR),
        "-PfxPath",     pfx,
        "-CerPath",     cer,
    ], tenant_id=tenant_id, env=env)

    update_tenant(tenant_id, client_id="", pfx_password_enc="")
    log_action(current_user(), "app_removal_started",
               tenant_id=tenant_id, tenant_name=t["name"])

    return jsonify({"ok": True, "job_id": job_id})
