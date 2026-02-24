"""
portal/app.py — M365 Audit Portal
Local Flask web app that wraps the SPO audit scripts and OfficeSentry security scan.
Run via start.cmd; opens at http://127.0.0.1:5000
"""

import os
import glob
import json
import uuid
import threading
import subprocess
import time
from datetime import datetime
from pathlib import Path
from flask import (
    Flask, render_template, request, redirect, url_for,
    jsonify, send_file, session, flash
)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
ROOT        = Path(__file__).parent.parent.resolve()
CONFIG_PATH = ROOT / "config" / "settings.json"
OUTPUT_DIR  = ROOT / "output"
CERTS_DIR   = ROOT / "certs"
MODULES_DIR = ROOT / "modules"
SCRIPTS_DIR = ROOT / "scripts"
INSPECTORS_DIR = ROOT / "inspectors"

# Prefer bundled pwsh; fall back to system pwsh
_bundled = ROOT / "bin" / "pwsh" / "pwsh.exe"
PWSH = str(_bundled) if _bundled.exists() else "pwsh"

# ---------------------------------------------------------------------------
# Flask setup
# ---------------------------------------------------------------------------
app = Flask(__name__)
app.secret_key = os.urandom(24)   # fresh key each start (local-only, that's fine)

# ---------------------------------------------------------------------------
# In-memory job store  { job_id: { status, type, label, output, started } }
# ---------------------------------------------------------------------------
jobs: dict = {}
jobs_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

def read_config() -> dict:
    if CONFIG_PATH.exists():
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    return {}


def write_config(cfg: dict):
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


def get_cert_info(cfg: dict) -> dict:
    """Return cert expiry info from the PFX if it exists."""
    pfx_rel = cfg.get("Certificate", {}).get("PfxPath", "")
    if not pfx_rel:
        return {"exists": False}
    pfx = ROOT / pfx_rel.lstrip(".\\").lstrip("./")
    if not pfx.exists():
        return {"exists": False, "path": str(pfx)}
    try:
        import ssl, tempfile
        # Use Python's ssl to read the cert (no password in Python ssl for PFX easily)
        # Fall back to PS for expiry details
        return {"exists": True, "path": str(pfx)}
    except Exception:
        return {"exists": True, "path": str(pfx)}


def build_status(cfg: dict) -> dict:
    tenant   = cfg.get("TenantId") or cfg.get("TenantDomain") or ""
    client   = cfg.get("ClientId", "")
    cert     = get_cert_info(cfg)
    out_dir  = cfg.get("Defaults", {}).get("OutputFolder", "./output")
    return dict(
        tenant=tenant, client_id=client,
        cert=cert, output_dir=out_dir,
        ready=(bool(tenant) and bool(client) and cert.get("exists", False))
    )


# ---------------------------------------------------------------------------
# PowerShell runner (background thread)
# ---------------------------------------------------------------------------

def _ps_worker(job_id: str, cmd: list):
    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace"
        )
        for line in proc.stdout:
            with jobs_lock:
                jobs[job_id]["output"].append(line.rstrip())
        rc = proc.wait()
        with jobs_lock:
            jobs[job_id]["status"] = "done" if rc == 0 else "failed"
            jobs[job_id]["ended"]  = datetime.now().isoformat()
    except Exception as exc:
        with jobs_lock:
            jobs[job_id]["output"].append(f"ERROR: {exc}")
            jobs[job_id]["status"] = "failed"
            jobs[job_id]["ended"]  = datetime.now().isoformat()


def start_job(label: str, job_type: str, cmd: list) -> str:
    job_id = str(uuid.uuid4())[:8]
    with jobs_lock:
        jobs[job_id] = {
            "status":  "running",
            "type":    job_type,
            "label":   label,
            "output":  [],
            "started": datetime.now().isoformat(),
            "ended":   None,
        }
    t = threading.Thread(target=_ps_worker, args=(job_id, cmd), daemon=True)
    t.start()
    return job_id


def run_ps_sync(script: Path, args: list, timeout: int = 120) -> dict:
    """Run a PS script synchronously and return parsed JSON from last output line."""
    cmd = [PWSH, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
           str(script)] + args
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout
        )
        # Find last JSON line in stdout
        for line in reversed(result.stdout.splitlines()):
            line = line.strip()
            if line.startswith("{"):
                return json.loads(line)
        return {"ok": False, "error": result.stderr or result.stdout or "No output"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "Script timed out"}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# Routes — Dashboard
# ---------------------------------------------------------------------------

@app.route("/")
def dashboard():
    cfg    = read_config()
    status = build_status(cfg)

    # Recent reports
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    report_files = sorted(OUTPUT_DIR.glob("*"), key=lambda f: f.stat().st_mtime, reverse=True)
    recent = [
        {"name": f.name, "mtime": datetime.fromtimestamp(f.stat().st_mtime).strftime("%Y-%m-%d %H:%M"),
         "size": f"{f.stat().st_size // 1024} KB"}
        for f in report_files[:5] if f.is_file()
    ]

    with jobs_lock:
        active_jobs = [
            {"id": jid, **jdata}
            for jid, jdata in jobs.items()
            if jdata["status"] == "running"
        ]

    return render_template("dashboard.html", status=status, recent=recent, active_jobs=active_jobs)


# ---------------------------------------------------------------------------
# Routes — Setup
# ---------------------------------------------------------------------------

@app.route("/setup")
def setup():
    cfg = read_config()
    return render_template("setup.html", cfg=cfg, status=build_status(cfg))


@app.route("/setup/cert", methods=["POST"])
def setup_cert():
    cfg      = read_config()
    password = request.form.get("pfx_password", "")
    if not password:
        return jsonify({"ok": False, "error": "PFX password is required."})

    # Store in session for later audits
    session["pfx_password"] = password

    cert_cfg = cfg.get("Certificate", {})
    cer_path = str(ROOT / cert_cfg.get("CerPath", "certs/spo-app.cer").lstrip(".\\").lstrip("./"))
    pfx_path = str(ROOT / cert_cfg.get("PfxPath", "certs/spo-app.pfx").lstrip(".\\").lstrip("./"))
    subject  = cert_cfg.get("Subject", "CN=M365 Audit Portal")
    years    = int(cert_cfg.get("YearsValid", 2))

    result = run_ps_sync(SCRIPTS_DIR / "Setup-Cert.ps1", [
        "-Subject",     subject,
        "-CerPath",     cer_path,
        "-PfxPath",     pfx_path,
        "-YearsValid",  str(years),
        "-PfxPassword", password,
    ], timeout=60)

    return jsonify(result)


@app.route("/setup/app", methods=["POST"])
def setup_app():
    cfg = read_config()
    cert_cfg = cfg.get("Certificate", {})
    cer_path = str(ROOT / cert_cfg.get("CerPath", "certs/spo-app.cer").lstrip(".\\").lstrip("./"))
    years    = int(cert_cfg.get("YearsValid", 2))
    app_name = cfg.get("AppDisplayName", "M365 Audit Portal")

    # This triggers a Graph browser login — runs in background so UI doesn't time out
    job_id = start_job("Create Entra App", "setup", [
        PWSH, "-NoProfile", "-ExecutionPolicy", "Bypass",
        str(SCRIPTS_DIR / "Setup-App.ps1"),
        "-AppDisplayName", app_name,
        "-CerPath",        cer_path,
        "-CertYearsValid", str(years),
        "-ModulesPath",    str(MODULES_DIR),
    ])
    return jsonify({"ok": True, "job_id": job_id, "message": "App creation started. Check job output for ClientId."})


@app.route("/setup/settings", methods=["POST"])
def setup_settings():
    cfg = read_config()
    tenant_id     = request.form.get("tenant_id", "").strip()
    tenant_domain = request.form.get("tenant_domain", "").strip()
    site_url      = request.form.get("site_url", "").strip()
    output_folder = request.form.get("output_folder", "").strip()

    if tenant_id:     cfg["TenantId"]              = tenant_id
    if tenant_domain: cfg["TenantDomain"]           = tenant_domain
    if site_url:      cfg.setdefault("Defaults", {})["SiteUrl"]      = site_url
    if output_folder: cfg.setdefault("Defaults", {})["OutputFolder"] = output_folder

    write_config(cfg)
    return jsonify({"ok": True, "message": "Settings saved."})


@app.route("/setup/remove", methods=["POST"])
def setup_remove():
    cfg = read_config()
    client_id = cfg.get("ClientId", "")
    if not client_id:
        return jsonify({"ok": False, "error": "No ClientId configured."})

    cert_cfg = cfg.get("Certificate", {})
    pfx_path = str(ROOT / cert_cfg.get("PfxPath", "").lstrip(".\\").lstrip("./"))
    cer_path = str(ROOT / cert_cfg.get("CerPath", "").lstrip(".\\").lstrip("./"))

    job_id = start_job("Remove Entra App", "setup", [
        PWSH, "-NoProfile", "-ExecutionPolicy", "Bypass",
        str(SCRIPTS_DIR / "Setup-RemoveApp.ps1"),
        "-ClientId",    client_id,
        "-ModulesPath", str(MODULES_DIR),
        "-PfxPath",     pfx_path,
        "-CerPath",     cer_path,
    ])

    # Clear ClientId from config immediately (will be invalid once job runs)
    cfg["ClientId"] = ""
    write_config(cfg)
    session.pop("pfx_password", None)

    return jsonify({"ok": True, "job_id": job_id})


# ---------------------------------------------------------------------------
# Routes — SPO Audit
# ---------------------------------------------------------------------------

@app.route("/audit/spo")
def audit_spo():
    cfg = read_config()
    return render_template("audit_spo.html", cfg=cfg, status=build_status(cfg))


@app.route("/audit/spo/run", methods=["POST"])
def audit_spo_run():
    cfg        = read_config()
    audit_type = request.form.get("audit_type", "groups")
    site_url   = request.form.get("site_url", "").strip()
    user_email = request.form.get("user_email", "").strip()
    pfx_pass   = request.form.get("pfx_password") or session.get("pfx_password", "")

    if not pfx_pass:
        return jsonify({"ok": False, "error": "PFX password required. Enter it above."})
    session["pfx_password"] = pfx_pass

    tenant   = cfg.get("TenantId") or cfg.get("TenantDomain", "")
    client   = cfg.get("ClientId", "")
    cert_cfg = cfg.get("Certificate", {})
    pfx_path = str(ROOT / cert_cfg.get("PfxPath", "certs/spo-app.pfx").lstrip(".\\").lstrip("./"))
    out_dir  = str(OUTPUT_DIR)

    script_map = {
        "groups":       "Audit-Groups.ps1",
        "permissions":  "Audit-SitePermissions.ps1",
        "external":     "Audit-ExternalSharing.ps1",
        "user":         "Get-SharePointTenantPermissions.ps1",
    }
    script_name = script_map.get(audit_type, "Audit-Groups.ps1")
    label_map   = {
        "groups":      "Groups Audit",
        "permissions": "Site Permissions Audit",
        "external":    "External Sharing Audit",
        "user":        "User Permissions Audit",
    }

    stamp   = datetime.now().strftime("%Y%m%d-%H%M")
    csv_out = str(OUTPUT_DIR / f"UserPerms_{stamp}.csv")
    xlsx_out= str(OUTPUT_DIR / f"UserPerms_{stamp}.xlsx")

    base_args = [
        "-SiteUrl",            site_url,
        "-OutputFolder",       out_dir,
        "-ClientId",           client,
        "-Tenant",             tenant,
        "-CertificatePath",    pfx_path,
        "-CertificatePassword", pfx_pass,
    ]

    if audit_type == "user":
        args = [
            "-Tenant",              tenant,
            "-UserEmail",           user_email,
            "-CSVPath",             csv_out,
            "-XlsxPath",            xlsx_out,
            "-ClientId",            client,
            "-CertificatePath",     pfx_path,
            "-CertificatePassword", pfx_pass,
        ]
    else:
        args = base_args

    # Build env so local modules are available
    env = os.environ.copy()
    env["PSModulePath"] = f"{MODULES_DIR};{env.get('PSModulePath','')}"

    cmd = [PWSH, "-NoProfile", "-ExecutionPolicy", "Bypass",
           str(SCRIPTS_DIR / script_name)] + args

    job_id = start_job(label_map.get(audit_type, audit_type), "spo", cmd)
    return jsonify({"ok": True, "job_id": job_id})


# ---------------------------------------------------------------------------
# Routes — Security Scan
# ---------------------------------------------------------------------------

@app.route("/audit/security")
def audit_security():
    # List available inspectors
    inspector_dir = INSPECTORS_DIR / "Inspectors"
    inspectors = sorted(
        set(f.stem for f in inspector_dir.glob("*.ps1"))
    ) if inspector_dir.exists() else []
    cfg = read_config()
    return render_template("audit_security.html", inspectors=inspectors,
                           cfg=cfg, status=build_status(cfg))


@app.route("/audit/security/run", methods=["POST"])
def audit_security_run():
    cfg           = read_config()
    report_format = request.form.get("report_format", "HTML")
    selected      = request.form.getlist("inspectors")   # empty = all
    pfx_pass      = request.form.get("pfx_password") or session.get("pfx_password", "")

    if not pfx_pass:
        return jsonify({"ok": False, "error": "PFX password required."})
    session["pfx_password"] = pfx_pass

    tenant    = cfg.get("TenantId") or cfg.get("TenantDomain", "")
    client    = cfg.get("ClientId", "")
    cert_cfg  = cfg.get("Certificate", {})
    pfx_path  = str(ROOT / cert_cfg.get("PfxPath", "certs/spo-app.pfx").lstrip(".\\").lstrip("./"))

    # Get thumbprint from cert store by loading the PFX
    # 365Inspect APP auth uses thumbprint from local cert store
    get_thumb_script = (
        f"$c = New-Object System.Security.Cryptography.X509Certificates.X509Certificate2;"
        f"$c.Import('{pfx_path}', '{pfx_pass}', 'EphemeralKeySet');"
        f"Write-Output $c.Thumbprint"
    )
    try:
        thumb_result = subprocess.run(
            [PWSH, "-NoProfile", "-NonInteractive", "-Command", get_thumb_script],
            capture_output=True, text=True, timeout=15
        )
        thumbprint = thumb_result.stdout.strip()
    except Exception:
        thumbprint = ""

    stamp   = datetime.now().strftime("%Y%m%d-%H%M")
    out_dir = str(OUTPUT_DIR / f"SecurityScan_{stamp}")
    Path(out_dir).mkdir(parents=True, exist_ok=True)

    inspect_script = str(INSPECTORS_DIR / "365Inspect.ps1")
    user_upn       = cfg.get("TenantDomain", tenant)   # 365Inspect needs a UPN for some connections

    cmd = [
        PWSH, "-NoProfile", "-ExecutionPolicy", "Bypass",
        inspect_script,
        "-OutPath",           out_dir,
        "-UserPrincipalName", user_upn,
        "-Auth",              "APP",
        "-reportType",        report_format,
        "-AppDomain",         tenant,
        "-AppClientId",       client,
        "-AppThumbprint",     thumbprint,
        "-SkipModuleCheck",
    ]

    if selected:
        cmd += ["-SelectedInspectors"] + selected

    # Add local modules to PS path via env
    env = os.environ.copy()
    env["PSModulePath"] = f"{MODULES_DIR};{env.get('PSModulePath','')}"

    job_id = start_job(f"Security Scan ({report_format})", "security", cmd)
    return jsonify({"ok": True, "job_id": job_id})


# ---------------------------------------------------------------------------
# Routes — Jobs
# ---------------------------------------------------------------------------

@app.route("/jobs/<job_id>/status")
def job_status(job_id):
    with jobs_lock:
        job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404

    tail = job["output"][-50:]   # last 50 lines
    return jsonify({
        "status":  job["status"],
        "label":   job["label"],
        "type":    job["type"],
        "started": job["started"],
        "ended":   job["ended"],
        "output":  tail,
    })


@app.route("/jobs")
def jobs_list():
    with jobs_lock:
        all_jobs = {jid: dict(jdata, output=jdata["output"][-5:]) for jid, jdata in jobs.items()}
    return render_template("jobs.html", jobs=all_jobs)


# ---------------------------------------------------------------------------
# Routes — Reports
# ---------------------------------------------------------------------------

@app.route("/reports")
def reports():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    def classify(name: str) -> str:
        n = name.lower()
        if "securityscan" in n or "365inspect" in n: return "Security Scan"
        if "userperms"    in n: return "User Permissions"
        if "groups"       in n: return "Groups"
        if "siteperm"     in n: return "Site Permissions"
        if "external"     in n: return "External Sharing"
        return "Other"

    files = []
    for f in sorted(OUTPUT_DIR.rglob("*"), key=lambda f: f.stat().st_mtime, reverse=True):
        if not f.is_file(): continue
        rel = f.relative_to(OUTPUT_DIR)
        files.append({
            "name":  str(rel),
            "type":  classify(f.name),
            "mtime": datetime.fromtimestamp(f.stat().st_mtime).strftime("%Y-%m-%d %H:%M"),
            "size":  f"{max(1, f.stat().st_size // 1024)} KB",
            "ext":   f.suffix.lower(),
        })

    return render_template("reports.html", files=files)


@app.route("/reports/view/<path:filename>")
def report_view(filename):
    file_path = OUTPUT_DIR / filename
    if not file_path.exists() or not file_path.is_file():
        return "File not found", 404
    # Serve HTML inline; everything else as download
    if file_path.suffix.lower() == ".html":
        return send_file(str(file_path), mimetype="text/html")
    return send_file(str(file_path), as_attachment=True)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    CERTS_DIR.mkdir(parents=True, exist_ok=True)
    print(f"M365 Audit Portal starting — http://127.0.0.1:5000")
    app.run(host="127.0.0.1", port=5000, debug=False, threaded=True)
