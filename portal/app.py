"""
portal/app.py — M365 Audit Portal
Multi-tenant Flask web app for M365 auditing, security scanning, and breach
investigation.  Designed for MSPs managing hundreds of client tenants.

Run via start.cmd or: python portal/app.py
"""

import json
import logging
import logging.handlers
import secrets
from datetime import timedelta
from pathlib import Path

from flask import Flask, render_template
from flask_wtf.csrf import CSRFProtect
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

from database import init_db, list_tenants, list_running_jobs
from helpers import login_required, TENANTS_DIR

# ---------------------------------------------------------------------------
# Paths & config
# ---------------------------------------------------------------------------
ROOT        = Path(__file__).parent.parent.resolve()
CONFIG_PATH = ROOT / "config" / "portal.json"
LOG_DIR     = ROOT / "logs"


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
# Logging — rotating file handler (10 MB, 5 backups)
# ---------------------------------------------------------------------------
LOG_DIR.mkdir(parents=True, exist_ok=True)

_file_handler = logging.handlers.RotatingFileHandler(
    LOG_DIR / "portal.log",
    maxBytes=10 * 1024 * 1024,   # 10 MB
    backupCount=5,
)
_file_handler.setFormatter(
    logging.Formatter("%(asctime)s %(levelname)s %(name)s — %(message)s")
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    handlers=[_file_handler, logging.StreamHandler()],
)
log = logging.getLogger("portal")


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------

def create_app() -> Flask:
    app = Flask(__name__)
    app.secret_key = get_secret_key()

    # Session timeout — 8 hours
    app.permanent_session_lifetime = timedelta(hours=8)

    # CSRF protection
    csrf = CSRFProtect(app)

    # Rate limiting
    limiter = Limiter(
        get_remote_address,
        app=app,
        default_limits=[],
        storage_uri="memory://",
    )

    # ------------------------------------------------------------------
    # Register Blueprints
    # ------------------------------------------------------------------
    from routes.auth import bp as auth_bp
    from routes.tenants import bp as tenants_bp
    from routes.audits import bp as audits_bp
    from routes.jobs import bp as jobs_bp
    from routes.reports import bp as reports_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(tenants_bp)
    app.register_blueprint(audits_bp)
    app.register_blueprint(jobs_bp)
    app.register_blueprint(reports_bp)

    # Apply rate limit to login
    limiter.limit("10/minute")(
        app.view_functions["auth.login"]
    )

    # ------------------------------------------------------------------
    # Dashboard route (kept in app.py — root route)
    # ------------------------------------------------------------------
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

    # ------------------------------------------------------------------
    # CSP headers
    # ------------------------------------------------------------------
    @app.after_request
    def set_security_headers(response):
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self' https://cdn.jsdelivr.net 'unsafe-inline'; "
            "style-src 'self' https://cdn.jsdelivr.net 'unsafe-inline'; "
            "font-src 'self' https://cdn.jsdelivr.net; "
            "img-src 'self' data:; "
            "connect-src 'self'"
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        return response

    return app


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    app = create_app()
    init_db()
    TENANTS_DIR.mkdir(parents=True, exist_ok=True)
    log.info("M365 Audit Portal starting — http://127.0.0.1:5000")
    app.run(host="127.0.0.1", port=5000, debug=False, threaded=True)
