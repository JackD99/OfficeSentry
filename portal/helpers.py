"""
portal/helpers.py — Shared helpers used across Blueprints.
Provides auth decorators, PowerShell execution, tenant filesystem helpers,
and environment setup.
"""

import os
import json
import logging
import re
import subprocess
import threading
from functools import wraps
from pathlib import Path

from flask import session, redirect, url_for, request, g, current_app

from database import (
    create_job, append_job_output, finish_job,
    decrypt_value,
)

log = logging.getLogger("portal")

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
ROOT           = Path(__file__).parent.parent.resolve()
TENANTS_DIR    = ROOT / "tenants"
SCRIPTS_DIR    = ROOT / "scripts"
INSPECTORS_DIR = ROOT / "inspectors"
MODULES_DIR    = ROOT / "modules"
LEGACY_OUTPUT  = ROOT / "output"

_bundled = ROOT / "bin" / "pwsh" / "pwsh.exe"
PWSH = str(_bundled) if _bundled.exists() else "pwsh"

_CONFIG_PATH = ROOT / "config" / "portal.json"


def read_portal_config() -> dict:
    try:
        return json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def write_portal_config(cfg: dict):
    _CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    _CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


def get_admin_client_id() -> str:
    return read_portal_config().get("admin_client_id", "")


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
            return redirect(url_for("auth.login", next=request.url))
        g.username = session["username"]
        return f(*args, **kwargs)
    return decorated


def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "username" not in session:
            return redirect(url_for("auth.login", next=request.url))
        g.username = session["username"]
        from database import get_user
        user = get_user(session["username"])
        if not user or not user.get("is_admin"):
            from flask import abort
            abort(403)
        return f(*args, **kwargs)
    return decorated


def current_user() -> str:
    return session.get("username", "anonymous")


# ---------------------------------------------------------------------------
# PowerShell runner (background thread -> SQLite)
# ---------------------------------------------------------------------------

# Track running processes for cancellation support
_running_processes: dict[str, subprocess.Popen] = {}


def _ps_worker(job_id: str, cmd: list, env: dict = None):
    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace",
            env=env,
        )
        _running_processes[job_id] = proc
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
    finally:
        _running_processes.pop(job_id, None)


def cancel_job(job_id: str) -> bool:
    """Cancel a running job by terminating its subprocess."""
    proc = _running_processes.get(job_id)
    if proc is None:
        return False
    try:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        append_job_output(job_id, "--- Job cancelled by operator ---")
        finish_job(job_id, "cancelled")
        _running_processes.pop(job_id, None)
        log.info("Job %s cancelled by %s", job_id, current_user())
        return True
    except Exception as exc:
        log.error("Failed to cancel job %s: %s", job_id, exc)
        return False


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
        return decrypt_value(enc, current_app.secret_key)
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Input validation helpers
# ---------------------------------------------------------------------------

# Pattern for valid M365 domain names (e.g., contoso.onmicrosoft.com)
_DOMAIN_PATTERN = re.compile(
    r"^[a-zA-Z0-9]([a-zA-Z0-9\-]*[a-zA-Z0-9])?(\.[a-zA-Z0-9]([a-zA-Z0-9\-]*[a-zA-Z0-9])?)+$"
)

# Pattern for valid Azure AD / Entra tenant IDs (GUID format)
_GUID_PATTERN = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)

# Pattern for valid SharePoint site URLs
_SITE_URL_PATTERN = re.compile(
    r"^https://[a-zA-Z0-9\-]+\.sharepoint\.com(/.*)?$"
)


def validate_domain(domain: str) -> bool:
    """Check if a string looks like a valid domain name."""
    return bool(_DOMAIN_PATTERN.match(domain)) and len(domain) <= 253


def validate_tenant_guid(tenant_id: str) -> bool:
    """Check if a string is a valid GUID format (for Entra tenant IDs)."""
    return bool(_GUID_PATTERN.match(tenant_id))


def validate_site_url(url: str) -> bool:
    """Check if a string looks like a valid SharePoint site URL."""
    return bool(_SITE_URL_PATTERN.match(url)) and len(url) <= 2048


def validate_email(email: str) -> bool:
    """Basic email format validation."""
    return bool(re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email)) and len(email) <= 320


def sanitize_text(text: str, max_length: int = 500) -> str:
    """Strip and truncate user-provided text input."""
    return text.strip()[:max_length]
