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
    name   = request.form.get("name", "").strip()
    domain = request.form.get("domain", "").strip()
    tid    = request.form.get("tenant_id", "").strip()
    notes  = request.form.get("notes", "").strip()

    if not name or not domain:
        flash("Name and domain are required.", "danger")
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
    ], env=env, timeout=60)

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

    years    = int(t.get("cert_years_valid") or 2)
    app_name = t.get("app_display_name") or "M365-Audit-App"

    env = build_ps_env()
    job_id = start_job(f"Create Entra App — {t['name']}", "setup", [
        PWSH, "-NoProfile", "-ExecutionPolicy", "Bypass",
        str(SCRIPTS_DIR / "Setup-App.ps1"),
        "-AppDisplayName", app_name,
        "-CerPath",        cer,
        "-CertYearsValid", str(years),
        "-ModulesPath",    str(MODULES_DIR),
    ], tenant_id=tenant_id, env=env)

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
