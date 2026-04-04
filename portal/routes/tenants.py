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
    start_python_job, get_tenant_pfx_password,
    validate_domain, validate_tenant_guid, sanitize_text,
    get_admin_client_id,
)

log = logging.getLogger("portal")

bp = Blueprint("tenants", __name__)


# ---------------------------------------------------------------------------
# Cross-platform certificate generation (replaces Windows-only Setup-Cert.ps1)
# ---------------------------------------------------------------------------

def _generate_cert(subject: str, cer_path: Path, pfx_path: Path,
                   years_valid: int, pfx_password: str) -> dict:
    """Generate a self-signed certificate and export CER + PFX files.

    Uses the ``cryptography`` library (already a project dependency) so this
    works on macOS, Linux, and Windows — no PowerShell PKI module required.

    Returns a dict matching the JSON contract of the old Setup-Cert.ps1 script:
    ``{"ok": True, "thumbprint": "...", "notAfter": "...", "cerPath": "...", "pfxPath": "..."}``
    """
    try:
        from cryptography import x509
        from cryptography.x509.oid import NameOID
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.serialization.pkcs12 import (
            serialize_key_and_certificates,
        )
        from cryptography.hazmat.primitives.asymmetric import rsa
        import datetime

        # Parse CN from subject string (e.g. "CN=Office Sentry")
        cn = subject
        if cn.upper().startswith("CN="):
            cn = cn[3:]

        # Generate RSA 2048 key
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

        # Build certificate
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])
        now = datetime.datetime.now(datetime.timezone.utc)
        not_after = now + datetime.timedelta(days=years_valid * 365)

        cert = (
            x509.CertificateBuilder()
            .subject_name(name)
            .issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now)
            .not_valid_after(not_after)
            .sign(key, hashes.SHA256())
        )

        # Thumbprint (SHA-1 fingerprint, matching Windows convention)
        thumbprint = cert.fingerprint(hashes.SHA1()).hex().upper()

        # Ensure output directories exist
        cer_path.parent.mkdir(parents=True, exist_ok=True)
        pfx_path.parent.mkdir(parents=True, exist_ok=True)

        # Export CER (DER-encoded public certificate)
        cer_path.write_bytes(cert.public_bytes(serialization.Encoding.DER))

        # Export PFX / PKCS12 (private key + certificate, password-protected)
        pfx_bytes = serialize_key_and_certificates(
            name=cn.encode("utf-8"),
            key=key,
            cert=cert,
            cas=None,
            encryption_algorithm=serialization.BestAvailableEncryption(
                pfx_password.encode("utf-8")
            ),
        )
        pfx_path.write_bytes(pfx_bytes)

        return {
            "ok": True,
            "thumbprint": thumbprint,
            "notAfter": not_after.strftime("%Y-%m-%d"),
            "cerPath": str(cer_path),
            "pfxPath": str(pfx_path),
        }

    except Exception as exc:
        log.error("Certificate generation failed: %s", exc)
        return {"ok": False, "error": str(exc)}


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
                           cert_exists=cert_exists, recent_jobs=recent_jobs,
                           admin_connector_configured=bool(get_admin_client_id()))


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

    cer_path = tenant_cer_path(tenant_id)
    pfx_path = tenant_pfx_path(tenant_id)
    subject = t.get("cert_subject") or "CN=Office Sentry"
    years = int(t.get("cert_years_valid") or 2)

    result = _generate_cert(subject, cer_path, pfx_path, years, password)

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

    # Use configured admin connector app or fall back to the well-known
    # Microsoft Graph PowerShell client ID (supports device code flow)
    admin_client_id = get_admin_client_id() or "14d82eec-204b-4c2f-b7e8-296a70dab67e"

    app_name   = t.get("app_display_name") or "Office Sentry"
    tenant_val = t.get("tenant_id") or t.get("domain", "")

    from graph_jobs import create_entra_app
    job_id = start_python_job(
        f"Create Entra App — {t['name']}", "setup",
        create_entra_app,
        args=(admin_client_id, cer, app_name, tenant_val),
        tenant_id=tenant_id,
    )

    log_action(current_user(), "app_creation_started",
               tenant_id=tenant_id, tenant_name=t["name"])

    return jsonify({"ok": True, "job_id": job_id,
                    "message": "App creation started. Check job output for ClientId."})


@bp.route("/tenants/<tenant_id>/reset-setup", methods=["POST"])
@login_required
def tenant_reset_setup(tenant_id):
    """Reset tenant setup: delete local cert files and clear app/cert DB fields.

    This does NOT contact Entra — it only clears local state so the user can
    restart the setup wizard from scratch.  If the Entra app still exists in
    the client's tenant, the user should remove it manually or use Remove App.
    """
    t = get_tenant(tenant_id)
    if not t:
        return jsonify({"ok": False, "error": "Tenant not found."}), 404

    # Delete local cert files
    for p in (tenant_pfx_path(tenant_id), tenant_cer_path(tenant_id)):
        if p.exists():
            p.unlink()

    # Clear app/cert fields in the database
    update_tenant(tenant_id, client_id="", pfx_password_enc="")

    log_action(current_user(), "setup_reset",
               tenant_id=tenant_id, tenant_name=t["name"],
               details="Local certs deleted, client_id and pfx_password cleared")

    return jsonify({"ok": True, "message": "Setup reset. You can now start fresh from Step 1."})


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

    admin_cid = get_admin_client_id()
    from graph_jobs import remove_entra_app
    job_id = start_python_job(
        f"Remove Entra App — {t['name']}", "setup",
        remove_entra_app,
        args=(admin_cid, client_id, pfx, cer),
        tenant_id=tenant_id,
    )

    update_tenant(tenant_id, client_id="", pfx_password_enc="")
    log_action(current_user(), "app_removal_started",
               tenant_id=tenant_id, tenant_name=t["name"])

    return jsonify({"ok": True, "job_id": job_id})
