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
    decrypt_value, update_job_task_id, get_job_task_id,
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
        if not user:
            from flask import abort
            abort(403)
        # Support both legacy is_admin and new RBAC roles
        from permissions import has_role
        if not (user.get("is_admin") or has_role(user, "admin")):
            from flask import abort
            abort(403)
        g.current_user = user
        return f(*args, **kwargs)
    return decorated


def role_required(min_role):
    """Decorator that requires a minimum RBAC role level."""
    def decorator(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            if "username" not in session:
                return redirect(url_for("auth.login", next=request.url))
            g.username = session["username"]
            from database import get_user
            from permissions import has_role
            user = get_user(session["username"])
            if not user or not has_role(user, min_role):
                from flask import abort
                abort(403)
            g.current_user = user
            return f(*args, **kwargs)
        return decorated
    return decorator


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
            # Publish to Redis pub/sub if available (for SSE)
            _publish_job_line(job_id, line.rstrip())
        rc = proc.wait()
        status = "done" if rc == 0 else "failed"
        finish_job(job_id, status)
        _publish_job_line(job_id, None, status=status)
        _fire_job_webhook(job_id, status)
        log.info("Job %s finished: %s (rc=%d)", job_id, status, rc)
    except Exception as exc:
        append_job_output(job_id, f"ERROR: {exc}")
        finish_job(job_id, "failed")
        _fire_job_webhook(job_id, "failed")
        log.error("Job %s error: %s", job_id, exc)
    finally:
        _running_processes.pop(job_id, None)


def _publish_job_line(job_id: str, line: str | None, status: str = "running"):
    """Publish a job output line to Redis pub/sub (for SSE streaming)."""
    try:
        import redis as _redis
        r = _redis.Redis.from_url(
            os.environ.get("CELERY_BROKER_URL", "redis://localhost:6379/0")
        )
        import json as _json
        payload = _json.dumps({"line": line, "status": status})
        r.publish(f"job:{job_id}", payload)
    except Exception:
        pass  # Redis not available — SSE will fall back to DB polling


def _fire_job_webhook(job_id: str, status: str):
    """Fire webhook events when a job completes or fails."""
    try:
        from database import get_job
        job = get_job(job_id)
        if not job:
            return
        event = f"job.{status}" if status in ("completed", "failed") else f"job.{status}"
        if status == "done":
            event = "job.completed"
        payload = {
            "job_id": job_id,
            "label": job.get("label", ""),
            "type": job.get("type", ""),
            "tenant_id": job.get("tenant_id", ""),
            "status": status,
        }
        try:
            from tasks import fire_webhook_event
            fire_webhook_event(event, payload)
        except Exception:
            pass  # Celery not available
    except Exception:
        pass


def cancel_job(job_id: str) -> bool:
    """Cancel a running job. Tries Celery revoke first, falls back to subprocess."""
    # Try Celery revoke first
    task_id = get_job_task_id(job_id)
    if task_id:
        try:
            from celery_app import make_celery
            celery = make_celery()
            celery.control.revoke(task_id, terminate=True)
            append_job_output(job_id, "--- Job cancelled by operator ---")
            finish_job(job_id, "cancelled")
            log.info("Job %s cancelled via Celery by %s", job_id, current_user())
            return True
        except Exception:
            pass  # Fall through to subprocess termination

    # Fall back to subprocess termination
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
    """Start a background job. Uses Celery if available, falls back to threading."""
    operator = current_user()
    job_id = create_job(label, job_type, tenant_id=tenant_id, operator=operator)
    log.info("Starting job %s: %s (tenant=%s, operator=%s)",
             job_id, label, tenant_id, operator)

    # Try Celery dispatch first
    try:
        from tasks import run_ps_job
        result = run_ps_job.delay(job_id, cmd, env)
        update_job_task_id(job_id, result.id)
        log.info("Job %s dispatched to Celery (task=%s)", job_id, result.id)
    except Exception:
        # Fall back to thread-based execution
        t = threading.Thread(target=_ps_worker, args=(job_id, cmd, env), daemon=True)
        t.start()

    return job_id


# ---------------------------------------------------------------------------
# Python job runner (for migrated Graph API scripts)
# ---------------------------------------------------------------------------

def _python_worker(job_id: str, func, args: tuple, kwargs: dict):
    """Run a Python callable as a background job.

    The callable receives a ``log_line`` callback to write progress lines,
    and should return a dict result (matching the PS JSON contract).
    """
    def log_line(line: str):
        append_job_output(job_id, line)
        _publish_job_line(job_id, line)

    try:
        result = func(*args, log_line=log_line, **kwargs)
        # Write final JSON result as last output line
        if result is not None:
            import json as _json
            append_job_output(job_id, _json.dumps(result, default=str))
            _publish_job_line(job_id, _json.dumps(result, default=str))
        status = "done" if result and result.get("ok") else "failed"
        finish_job(job_id, status)
        _publish_job_line(job_id, None, status=status)
        _fire_job_webhook(job_id, status)
        log.info("Python job %s finished: %s", job_id, status)
    except Exception as exc:
        append_job_output(job_id, f"ERROR: {exc}")
        finish_job(job_id, "failed")
        _publish_job_line(job_id, None, status="failed")
        _fire_job_webhook(job_id, "failed")
        log.error("Python job %s error: %s", job_id, exc)


def start_python_job(
    label: str,
    job_type: str,
    func,
    args: tuple = (),
    kwargs: dict | None = None,
    tenant_id: str = None,
) -> str:
    """Start a background Python job. Falls back to threading (Celery support later)."""
    operator = current_user()
    job_id = create_job(label, job_type, tenant_id=tenant_id, operator=operator)
    log.info("Starting Python job %s: %s (tenant=%s, operator=%s)",
             job_id, label, tenant_id, operator)

    t = threading.Thread(
        target=_python_worker,
        args=(job_id, func, args, kwargs or {}),
        daemon=True,
    )
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
