"""
portal/app.py — M365 Audit Portal
Multi-tenant Flask web app for M365 auditing, security scanning, and breach
investigation.  Designed for MSPs managing hundreds of client tenants.

Launch: python run.py (any OS), start.cmd (Windows), ./setup.sh (Linux/macOS)
"""

import json
import logging
import logging.handlers
import secrets
import uuid
from datetime import timedelta
from pathlib import Path

from flask import Flask, render_template, jsonify, request, g
from flask_wtf.csrf import CSRFProtect
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

from database import (
    init_db, list_tenants, list_running_jobs, get_audit_log, get_dashboard_stats,
    get_job_trends, get_job_type_distribution, get_report_type_counts,
    list_saved_views, list_schedules,
    get_bpa_summary, get_all_latest_secure_scores, get_alert_counts,
    list_tenant_groups,
)
from helpers import login_required, current_user, TENANTS_DIR, tenant_output_dir

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
    from routes.operations import bp as operations_bp
    from routes.standards import bp as standards_bp
    from routes.tenant_admin import bp as tenant_admin_bp
    from routes.conditional import bp as conditional_bp
    from routes.gdap import bp as gdap_bp
    from routes.tools import bp as tools_bp
    from routes.email_security import bp as email_security_bp
    from routes.identity import bp as identity_bp
    from routes.devices import bp as devices_bp
    from routes.service_health import bp as service_health_bp
    from routes.licenses import bp as licenses_bp
    from routes.incidents import bp as incidents_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(tenants_bp)
    app.register_blueprint(audits_bp)
    app.register_blueprint(jobs_bp)
    app.register_blueprint(reports_bp)
    app.register_blueprint(operations_bp)
    app.register_blueprint(standards_bp)
    app.register_blueprint(tenant_admin_bp)
    app.register_blueprint(conditional_bp)
    app.register_blueprint(gdap_bp)
    app.register_blueprint(tools_bp)
    app.register_blueprint(email_security_bp)
    app.register_blueprint(identity_bp)
    app.register_blueprint(devices_bp)
    app.register_blueprint(service_health_bp)
    app.register_blueprint(licenses_bp)
    app.register_blueprint(incidents_bp)

    # Apply rate limit to login and initial setup
    limiter.limit("10/minute")(
        app.view_functions["auth.login"]
    )
    limiter.limit("5/minute")(
        app.view_functions["auth.initial_setup"]
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
        recent_activity = get_audit_log(limit=10)

        # Chart data
        stats = get_dashboard_stats()
        job_trends = get_job_trends(30)
        job_types = get_job_type_distribution()
        schedules = list_schedules()
        saved_views = list_saved_views()

        # Report type distribution from filesystem
        out_dirs = [tenant_output_dir(t["id"]) for t in tenants
                    if tenant_output_dir(t["id"]).exists()]
        report_counts = get_report_type_counts(out_dirs)

        # Compliance data
        bpa_summary = get_bpa_summary()
        secure_scores = get_all_latest_secure_scores()
        alert_counts = get_alert_counts()
        tenant_groups = list_tenant_groups()

        return render_template("dashboard.html",
                               tenants=tenants,
                               running_jobs=running,
                               total_tenants=total_tenants,
                               configured_count=configured,
                               audited_count=recent_audits,
                               recent_activity=recent_activity,
                               stats=stats,
                               job_trends=job_trends,
                               job_types=job_types,
                               report_counts=report_counts,
                               schedules=schedules,
                               saved_views=saved_views,
                               bpa_summary=bpa_summary,
                               secure_scores=secure_scores,
                               alert_counts=alert_counts,
                               tenant_groups=tenant_groups)

    # ------------------------------------------------------------------
    # Dashboard stats API
    # ------------------------------------------------------------------
    @app.route("/api/stats")
    @login_required
    def api_stats():
        return jsonify(get_dashboard_stats())

    @app.route("/api/chart-data")
    @login_required
    def api_chart_data():
        """Return chart data for dashboard AJAX updates."""
        tenants = list_tenants()
        out_dirs = [tenant_output_dir(t["id"]) for t in tenants
                    if tenant_output_dir(t["id"]).exists()]
        return jsonify({
            "job_trends": get_job_trends(30),
            "job_types": get_job_type_distribution(),
            "report_counts": get_report_type_counts(out_dirs),
            "stats": get_dashboard_stats(),
        })

    # ------------------------------------------------------------------
    # Health check endpoint (unauthenticated, for monitoring)
    # ------------------------------------------------------------------
    @app.route("/health")
    def health_check():
        """Lightweight health check for load balancers and monitoring."""
        from database import get_db
        try:
            with get_db() as conn:
                conn.execute("SELECT 1").fetchone()
            db_ok = True
        except Exception:
            db_ok = False

        status = "healthy" if db_ok else "degraded"
        code = 200 if db_ok else 503
        return jsonify({
            "status": status,
            "database": "ok" if db_ok else "error",
        }), code

    # ------------------------------------------------------------------
    # Request ID tracking
    # ------------------------------------------------------------------
    @app.before_request
    def assign_request_id():
        g.request_id = request.headers.get("X-Request-ID", uuid.uuid4().hex[:12])

    # ------------------------------------------------------------------
    # Error handlers
    # ------------------------------------------------------------------
    @app.errorhandler(404)
    def not_found(e):
        if request.path.startswith("/api/") or request.accept_mimetypes.best == "application/json":
            return jsonify({"ok": False, "error": "Not found"}), 404
        return render_template("error.html", code=404,
                               message="Page not found",
                               detail="The page you're looking for doesn't exist or has been moved."), 404

    @app.errorhandler(500)
    def internal_error(e):
        log.error("Internal server error: %s (request_id=%s)",
                  e, getattr(g, "request_id", "unknown"))
        if request.path.startswith("/api/") or request.accept_mimetypes.best == "application/json":
            return jsonify({"ok": False, "error": "Internal server error"}), 500
        return render_template("error.html", code=500,
                               message="Something went wrong",
                               detail="An unexpected error occurred. Check the logs for details."), 500

    @app.errorhandler(403)
    def forbidden(e):
        if request.path.startswith("/api/") or request.accept_mimetypes.best == "application/json":
            return jsonify({"ok": False, "error": "Forbidden"}), 403
        return render_template("error.html", code=403,
                               message="Access denied",
                               detail="You don't have permission to access this resource."), 403

    # ------------------------------------------------------------------
    # Security headers
    # ------------------------------------------------------------------
    @app.after_request
    def set_security_headers(response):
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self' https://cdn.jsdelivr.net 'unsafe-inline'; "
            "style-src 'self' https://cdn.jsdelivr.net 'unsafe-inline'; "
            "font-src 'self' https://cdn.jsdelivr.net; "
            "img-src 'self' data: blob:; "
            "connect-src 'self' http://ip-api.com"
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = (
            "camera=(), microphone=(), geolocation=(), payment=()"
        )
        if hasattr(g, "request_id"):
            response.headers["X-Request-ID"] = g.request_id
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
