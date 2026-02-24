"""
portal/app.py — M365 Audit Portal
Multi-tenant Flask web app for M365 auditing, security scanning, and breach
investigation.  Designed for MSPs managing hundreds of client tenants.

Run via start.cmd or: python portal/app.py
"""

import os
import json
import logging
import secrets
import threading
import subprocess
from datetime import datetime
from functools import wraps
from pathlib import Path

from flask import (
    Flask, render_template, request, redirect, url_for,
    jsonify, send_file, session, flash, abort, g,
)

from database import (
    init_db, user_count, create_user, get_user, check_password,
    update_password, create_tenant, get_tenant, list_tenants,
    update_tenant, delete_tenant, touch_tenant_audit,
    create_job, get_job, append_job_output, finish_job,
    list_jobs, list_running_jobs,
    log_action, get_audit_log,
    encrypt_value, decrypt_value,
)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
ROOT           = Path(__file__).parent.parent.resolve()
TENANTS_DIR    = ROOT / "tenants"
SCRIPTS_DIR    = ROOT / "scripts"
INSPECTORS_DIR = ROOT / "inspectors"
MODULES_DIR    = ROOT / "modules"
LEGACY_OUTPUT  = ROOT / "output"

CONFIG_PATH    = ROOT / "config" / "portal.json"

_bundled = ROOT / "bin" / "pwsh" / "pwsh.exe"
PWSH = str(_bundled) if _bundled.exists() else "pwsh"

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
LOG_DIR = ROOT / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / "portal.log"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("portal")

# ---------------------------------------------------------------------------
# Portal config (persistent)
# ---------------------------------------------------------------------------

def read_portal_config() -> dict:
    if CONFIG_PATH.exists():
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    return {}


def write_portal_config(cfg: dict):
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


def get_secret_key() -> str:
    cfg = read_portal_config()
    if "secret_key" not in cfg:
        cfg["secret_key"] = secrets.token_hex(32)
        write_portal_config(cfg)
    return cfg["secret_key"]


# ---------------------------------------------------------------------------
# Flask app
# ---------------------------------------------------------------------------
app = Flask(__name__)
app.secret_key = get_secret_key()

# ---------------------------------------------------------------------------
# Tenant filesystem helpers
# ---------------------------------------------------------------------------

def tenant_dir(tenant_id: str) -> Path:
    return TENANTS_DIR / tenant_id


def tenant_certs_dir(tenant_id: str) -> Path:
    d = tenant_dir(tenant_id) / "certs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def tenant_output_dir(tenant_id: str) -> Path:
    d = tenant_dir(tenant_id) / "output"
    d.mkdir(parents=True, exist_ok=True)
    return d


def tenant_pfx_path(tenant_id: str) -> Path:
    return tenant_certs_dir(tenant_id) / "spo-app.pfx"


def tenant_cer_path(tenant_id: str) -> Path:
    return tenant_certs_dir(tenant_id) / "spo-app.cer"


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "username" not in session:
            return redirect(url_for("login", next=request.url))
        g.username = session["username"]
        return f(*args, **kwargs)
    return decorated


def current_user() -> str:
    return session.get("username", "anonymous")


# ---------------------------------------------------------------------------
# PowerShell runner (background thread -> SQLite)
# ---------------------------------------------------------------------------

def _ps_worker(job_id: str, cmd: list, env: dict = None):
    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace",
            env=env,
        )
        for line in proc.stdout:
            append_job_output(job_id, line.rstrip())
        rc = proc.wait()
        status = "done" if rc == 0 else "failed"
        finish_job(job_id, status)
        log.info("Job %s finished: %s (rc=%d)", job_id, status, rc)
    except Exception as exc:
        append_job_output(job_id, f"ERROR: {exc}")
        finish_job(job_id, "failed")
        log.error("Job %s error: %s", job_id, exc)


def start_job(label: str, job_type: str, cmd: list,
              tenant_id: str = None, env: dict = None) -> str:
    operator = current_user()
    job_id = create_job(label, job_type, tenant_id=tenant_id, operator=operator)
    log.info("Starting job %s: %s (tenant=%s, operator=%s)",
             job_id, label, tenant_id, operator)
    t = threading.Thread(target=_ps_worker, args=(job_id, cmd, env), daemon=True)
    t.start()
    return job_id


def run_ps_sync(script: Path, args: list, env: dict = None,
                timeout: int = 120) -> dict:
    cmd = [PWSH, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
           str(script)] + args
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout,
            env=env,
        )
        for line in reversed(result.stdout.splitlines()):
            line = line.strip()
            if line.startswith("{"):
                return json.loads(line)
        return {"ok": False, "error": result.stderr or result.stdout or "No output"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "Script timed out"}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def build_ps_env(pfx_password: str = "") -> dict:
    """Build environment for PowerShell subprocesses with local modules."""
    env = os.environ.copy()
    env["PSModulePath"] = f"{MODULES_DIR};{env.get('PSModulePath', '')}"
    if pfx_password:
        env["AUDIT_PFX_PASSWORD"] = pfx_password
    return env


def get_tenant_pfx_password(t: dict) -> str:
    """Decrypt the stored PFX password for a tenant."""
    enc = t.get("pfx_password_enc", "")
    if not enc:
        return ""
    try:
        return decrypt_value(enc, app.secret_key)
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Routes — Auth
# ---------------------------------------------------------------------------

@app.route("/login", methods=["GET", "POST"])
def login():
    if user_count() == 0:
        return redirect(url_for("initial_setup"))

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        user = get_user(username)
        if user and check_password(user["password_hash"], password):
            session["username"] = username
            session["display_name"] = user["display_name"]
            log.info("Login: %s", username)
            log_action(username, "login")
            next_url = request.args.get("next", url_for("dashboard"))
            return redirect(next_url)
        flash("Invalid username or password.", "danger")
    return render_template("login.html")


@app.route("/logout")
def logout():
    username = session.get("username", "")
    session.clear()
    if username:
        log.info("Logout: %s", username)
    return redirect(url_for("login"))


@app.route("/initial-setup", methods=["GET", "POST"])
def initial_setup():
    if user_count() > 0:
        return redirect(url_for("login"))
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        confirm  = request.form.get("confirm", "")
        if not username or not password:
            flash("Username and password are required.", "danger")
        elif password != confirm:
            flash("Passwords do not match.", "danger")
        elif len(password) < 8:
            flash("Password must be at least 8 characters.", "danger")
        else:
            create_user(username, password, display_name=username, is_admin=1)
            session["username"] = username
            session["display_name"] = username
            log.info("Initial setup: created admin user %s", username)
            log_action(username, "initial_setup", details="First admin account created")
            flash("Admin account created. Welcome!", "success")
            return redirect(url_for("dashboard"))
    return render_template("initial_setup.html")


# ---------------------------------------------------------------------------
# Routes — Dashboard
# ---------------------------------------------------------------------------

@app.route("/")
@login_required
def dashboard():
    tenants = list_tenants()
    running = list_running_jobs()

    total_tenants = len(tenants)
    configured = sum(1 for t in tenants if t["client_id"])
    recent_audits = sum(1 for t in tenants if t.get("last_audit_at"))

    return render_template("dashboard.html",
                           tenants=tenants,
                           running_jobs=running,
                           total_tenants=total_tenants,
                           configured_count=configured,
                           audited_count=recent_audits)


# ---------------------------------------------------------------------------
# Routes — Tenant Management
# ---------------------------------------------------------------------------

@app.route("/tenants")
@login_required
def tenants_list():
    tenants = list_tenants()
    return render_template("tenants.html", tenants=tenants)


@app.route("/tenants/new", methods=["POST"])
@login_required
def tenant_create():
    name   = request.form.get("name", "").strip()
    domain = request.form.get("domain", "").strip()
    tid    = request.form.get("tenant_id", "").strip()
    notes  = request.form.get("notes", "").strip()

    if not name or not domain:
        flash("Name and domain are required.", "danger")
        return redirect(url_for("tenants_list"))

    new_id = create_tenant(name, domain, tenant_id=tid, notes=notes)
    log_action(current_user(), "tenant_created",
               tenant_id=new_id, tenant_name=name,
               details=f"Domain: {domain}")
    flash(f"Tenant '{name}' created.", "success")
    return redirect(url_for("tenant_detail", tenant_id=new_id))


@app.route("/tenants/<tenant_id>")
@login_required
def tenant_detail(tenant_id):
    t = get_tenant(tenant_id)
    if not t:
        abort(404)
    cert_exists = tenant_pfx_path(tenant_id).exists()
    recent_jobs = list_jobs(tenant_id=tenant_id, limit=10)
    return render_template("tenant_detail.html", tenant=t,
                           cert_exists=cert_exists, recent_jobs=recent_jobs)


@app.route("/tenants/<tenant_id>/settings", methods=["POST"])
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
        fields["pfx_password_enc"] = encrypt_value(pfx_pass, app.secret_key)

    if fields:
        update_tenant(tenant_id, **fields)
        log_action(current_user(), "tenant_settings_updated",
                   tenant_id=tenant_id, tenant_name=t["name"],
                   details=f"Updated: {', '.join(fields.keys())}")

    return jsonify({"ok": True, "message": "Settings saved."})


@app.route("/tenants/<tenant_id>/delete", methods=["POST"])
@login_required
def tenant_delete(tenant_id):
    t = get_tenant(tenant_id)
    if not t:
        abort(404)

    import shutil
    td = tenant_dir(tenant_id)
    if td.exists():
        shutil.rmtree(td, ignore_errors=True)

    delete_tenant(tenant_id)
    log_action(current_user(), "tenant_deleted", tenant_name=t["name"],
               details=f"Domain: {t['domain']}")
    flash(f"Tenant '{t['name']}' deleted.", "success")
    return redirect(url_for("tenants_list"))


# ---------------------------------------------------------------------------
# Routes — Tenant Setup (cert, app)
# ---------------------------------------------------------------------------

@app.route("/tenants/<tenant_id>/cert", methods=["POST"])
@login_required
def tenant_cert(tenant_id):
    t = get_tenant(tenant_id)
    if not t:
        return jsonify({"ok": False, "error": "Tenant not found."}), 404

    password = request.form.get("pfx_password", "").strip()
    if not password:
        return jsonify({"ok": False, "error": "PFX password is required."})

    update_tenant(tenant_id,
                  pfx_password_enc=encrypt_value(password, app.secret_key))

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


@app.route("/tenants/<tenant_id>/app", methods=["POST"])
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


@app.route("/tenants/<tenant_id>/remove-app", methods=["POST"])
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


# ---------------------------------------------------------------------------
# Routes — SPO Audit
# ---------------------------------------------------------------------------

@app.route("/tenants/<tenant_id>/audit/spo")
@login_required
def audit_spo(tenant_id):
    t = get_tenant(tenant_id)
    if not t:
        abort(404)
    ready = bool(t["client_id"]) and tenant_pfx_path(tenant_id).exists()
    return render_template("audit_spo.html", tenant=t, ready=ready)


@app.route("/tenants/<tenant_id>/audit/spo/run", methods=["POST"])
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
# Routes — Security Scan
# ---------------------------------------------------------------------------

@app.route("/tenants/<tenant_id>/audit/security")
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


@app.route("/tenants/<tenant_id>/audit/security/run", methods=["POST"])
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
# Routes — Breach Investigation
# ---------------------------------------------------------------------------

@app.route("/tenants/<tenant_id>/audit/breach")
@login_required
def audit_breach(tenant_id):
    t = get_tenant(tenant_id)
    if not t:
        abort(404)
    ready = bool(t["client_id"]) and tenant_pfx_path(tenant_id).exists()
    return render_template("audit_breach.html", tenant=t, ready=ready)


@app.route("/tenants/<tenant_id>/audit/breach/run", methods=["POST"])
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
# Routes — Batch Operations
# ---------------------------------------------------------------------------

@app.route("/batch/audit", methods=["POST"])
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


# ---------------------------------------------------------------------------
# Routes — Jobs
# ---------------------------------------------------------------------------

@app.route("/jobs")
@login_required
def jobs_page():
    tenant_id = request.args.get("tenant_id")
    all_jobs = list_jobs(tenant_id=tenant_id, limit=100)
    tenants = {t["id"]: t["name"] for t in list_tenants()}
    return render_template("jobs.html", jobs=all_jobs, tenants=tenants,
                           filter_tenant=tenant_id)


@app.route("/jobs/<job_id>/status")
@login_required
def job_status(job_id):
    job = get_job(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    tail = job["output"][-80:]
    return jsonify({
        "status":  job["status"],
        "label":   job["label"],
        "type":    job["type"],
        "started": job["started_at"],
        "ended":   job["ended_at"],
        "output":  tail,
    })


# ---------------------------------------------------------------------------
# Routes — Reports (safe path handling)
# ---------------------------------------------------------------------------

@app.route("/reports")
@login_required
def reports():
    tenant_id = request.args.get("tenant_id")

    def classify(name: str) -> str:
        n = name.lower()
        if "securityscan" in n or "365inspect" in n: return "Security Scan"
        if "userperms"    in n: return "User Permissions"
        if "groups"       in n: return "Groups"
        if "siteperm"     in n: return "Site Permissions"
        if "external"     in n: return "External Sharing"
        if "ual" in n or "auditlog" in n: return "Audit Log"
        if "inboxrule"    in n: return "Inbox Rules"
        if "signin"       in n: return "Sign-In Logs"
        if "oauth"        in n: return "OAuth Consent"
        if "msgtrace" in n or "messagetrace" in n: return "Message Trace"
        return "Other"

    files = []
    tenants_map = {}

    if tenant_id:
        t = get_tenant(tenant_id)
        dirs = [(tenant_id, tenant_output_dir(tenant_id))]
        if t:
            tenants_map[tenant_id] = t["name"]
    else:
        dirs = []
        for t in list_tenants():
            td = tenant_output_dir(t["id"])
            if td.exists():
                dirs.append((t["id"], td))
                tenants_map[t["id"]] = t["name"]
        if LEGACY_OUTPUT.exists():
            dirs.append(("_legacy", LEGACY_OUTPUT))
            tenants_map["_legacy"] = "(Legacy)"

    for tid, out_dir in dirs:
        if not out_dir.exists():
            continue
        for f in sorted(out_dir.rglob("*"), key=lambda x: x.stat().st_mtime, reverse=True):
            if not f.is_file():
                continue
            rel = f.relative_to(out_dir)
            files.append({
                "name":        str(rel),
                "type":        classify(f.name),
                "mtime":       datetime.fromtimestamp(f.stat().st_mtime).strftime("%Y-%m-%d %H:%M"),
                "size":        f"{max(1, f.stat().st_size // 1024)} KB",
                "ext":         f.suffix.lower(),
                "tenant_id":   tid,
                "tenant_name": tenants_map.get(tid, ""),
                "full_path":   str(f),
            })

    files.sort(key=lambda x: x["mtime"], reverse=True)

    tenant_list = list_tenants()
    return render_template("reports.html", files=files, tenants=tenant_list,
                           filter_tenant=tenant_id)


@app.route("/reports/download")
@login_required
def report_download():
    """Safe file download — validates path is within an allowed directory."""
    file_path = request.args.get("path", "")
    if not file_path:
        abort(404)

    resolved = Path(file_path).resolve()

    allowed_bases = [TENANTS_DIR.resolve(), LEGACY_OUTPUT.resolve()]
    if not any(str(resolved).startswith(str(base)) for base in allowed_bases):
        log.warning("Path traversal attempt blocked: %s (user=%s)",
                    file_path, current_user())
        abort(403)

    if not resolved.exists() or not resolved.is_file():
        abort(404)

    if resolved.suffix.lower() == ".html":
        return send_file(str(resolved), mimetype="text/html")
    return send_file(str(resolved), as_attachment=True)


# ---------------------------------------------------------------------------
# Routes — Audit Trail
# ---------------------------------------------------------------------------

@app.route("/audit-log")
@login_required
def audit_trail():
    tenant_id = request.args.get("tenant_id")
    entries = get_audit_log(limit=500, tenant_id=tenant_id)
    tenants = list_tenants()
    return render_template("audit_trail.html", entries=entries,
                           tenants=tenants, filter_tenant=tenant_id)


# ---------------------------------------------------------------------------
# Routes — Settings
# ---------------------------------------------------------------------------

@app.route("/settings")
@login_required
def settings_page():
    return render_template("settings.html")


@app.route("/settings/password", methods=["POST"])
@login_required
def change_password():
    current  = request.form.get("current_password", "")
    new_pass = request.form.get("new_password", "")
    confirm  = request.form.get("confirm_password", "")

    user = get_user(current_user())
    if not user or not check_password(user["password_hash"], current):
        return jsonify({"ok": False, "error": "Current password is incorrect."})
    if new_pass != confirm:
        return jsonify({"ok": False, "error": "New passwords do not match."})
    if len(new_pass) < 8:
        return jsonify({"ok": False, "error": "Password must be at least 8 characters."})

    update_password(current_user(), new_pass)
    log_action(current_user(), "password_changed")
    return jsonify({"ok": True, "message": "Password updated."})


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    init_db()
    TENANTS_DIR.mkdir(parents=True, exist_ok=True)
    log.info("M365 Audit Portal starting — http://127.0.0.1:5000")
    app.run(host="127.0.0.1", port=5000, debug=False, threaded=True)
