"""Flask application factory."""

from __future__ import annotations

import secrets
from datetime import timedelta

from flask import Flask, g, render_template, request
from flask_wtf.csrf import CSRFProtect
from werkzeug.middleware.proxy_fix import ProxyFix

from ..config import Config, load_config, setup_token
from ..crypto import Vault
from ..db import connect, migrate
from . import access, admin, auth, filters, prospects, reports, tenants

csrf = CSRFProtect()


def create_app(cfg: Config | None = None) -> Flask:
    cfg = cfg or load_config()
    app = Flask(__name__)
    app.config.update(
        SECRET_KEY=cfg.secret_key,
        # "__Host-" makes browsers insist on Secure, path=/ and no Domain, so a
        # sibling subdomain can't plant or read the cookie.
        SESSION_COOKIE_NAME="__Host-officesentry" if cfg.secure_cookies else "officesentry",
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=cfg.secure_cookies,
        # The server-side session (web/sessions.py) enforces the real limits.
        PERMANENT_SESSION_LIFETIME=timedelta(hours=cfg.session_hours),
        WTF_CSRF_TIME_LIMIT=None,
        TESTING=cfg.testing,
        WTF_CSRF_ENABLED=not cfg.extra.get("disable_csrf", False),
        MAX_CONTENT_LENGTH=1 * 1024 * 1024,
    )
    app.extensions["officesentry.config"] = cfg
    app.extensions["officesentry.vault"] = Vault(cfg.encryption_keys)

    if cfg.trusted_proxies > 0:
        # Behind a reverse proxy: take the client IP, scheme and host from its headers.
        n = cfg.trusted_proxies
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=n, x_proto=n, x_host=n, x_port=n, x_prefix=n)

    conn = connect(cfg.db_path)
    migrate(conn)
    if conn.execute("SELECT 1 FROM users LIMIT 1").fetchone() is None:
        app.logger.warning("No admin account yet. To create one, open %s/setup?token=%s",
                           cfg.base_url, setup_token(cfg.data_dir))
    conn.close()

    csrf.init_app(app)
    filters.register(app)

    @app.before_request
    def _open_db():
        g.db = connect(cfg.db_path)
        g.csp_nonce = secrets.token_urlsafe(16)
        access.load_user()

    @app.teardown_request
    def _close_db(_exc):
        db = g.pop("db", None)
        if db is not None:
            db.close()

    @app.after_request
    def _security_headers(resp):
        nonce = getattr(g, "csp_nonce", "")
        resp.headers["Content-Security-Policy"] = (
            f"default-src 'self'; script-src 'self' 'nonce-{nonce}'; style-src 'self'; "
            "img-src 'self' data:; object-src 'none'; base-uri 'none'; "
            "frame-ancestors 'none'; form-action 'self'"
        )
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Referrer-Policy"] = "same-origin"
        resp.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        if cfg.secure_cookies:
            resp.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        if request.endpoint != "static":
            resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.context_processor
    def _inject():
        return {"current_user": g.get("user"), "csp_nonce": g.get("csp_nonce", "")}

    @app.errorhandler(403)
    def _forbidden(_e):
        return render_template("error.html", code=403, message="You don't have access to this page."), 403

    @app.errorhandler(404)
    def _not_found(_e):
        return render_template("error.html", code=404, message="Page not found."), 404

    @app.get("/health")
    def health():
        return {"ok": True}

    app.register_blueprint(auth.bp)
    app.register_blueprint(tenants.bp)
    app.register_blueprint(admin.bp)
    app.register_blueprint(reports.bp)
    app.register_blueprint(prospects.bp)
    return app
