"""Flask application factory."""

from __future__ import annotations

import secrets
from datetime import timedelta

from flask import Flask, g, render_template, request
from flask_wtf.csrf import CSRFProtect
from werkzeug.middleware.proxy_fix import ProxyFix

from .. import __version__, system_alerts, system_health, worker
from ..config import Config, load_config, setup_token
from ..crypto import Vault
from ..db import connect, migrate
from . import (
    access,
    across,
    activity,
    admin,
    alerts,
    auth,
    branding,
    data_retention,
    email,
    filters,
    groups,
    history,
    people,
    projects,
    public_demo,
    reporting,
    reports,
    saved_views,
    search,
    styleguide,
    system,
    tenant_admin,
    tenants,
    upcoming,
)

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
        # The worker can't report that it has stopped, so the portal watches it (system_alerts.py).
        system_alerts.start_watchdog(app, cfg)
        g.db = connect(cfg.db_path)
        g.csp_nonce = secrets.token_urlsafe(16)
        access.load_user()

    # On the public demo site, nothing but reading (after load_user, which sets g.public_demo).
    app.before_request(public_demo.refuse_changes)
    app.after_request(public_demo.add_headers)

    @app.teardown_request
    def _close_db(_exc):
        db = g.pop("db", None)
        if db is not None:
            db.close()

    @app.after_request
    def _security_headers(resp):
        nonce = getattr(g, "csp_nonce", "")
        # A rendered email preview sets its own, stricter policy and may be framed by the portal.
        override = g.get("csp_override")
        resp.headers["Content-Security-Policy"] = override or (
            f"default-src 'self'; script-src 'self' 'nonce-{nonce}'; style-src 'self'; "
            "img-src 'self' data:; object-src 'none'; base-uri 'none'; "
            "frame-ancestors 'none'; form-action 'self'"
        )
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "SAMEORIGIN" if override else "DENY"
        resp.headers["Referrer-Policy"] = "same-origin"
        resp.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        if cfg.secure_cookies:
            resp.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        # Static files and the public brand colours and logo may be cached; nothing else is.
        if request.endpoint not in ("static", "branding.brand_css", "branding.logo"):
            resp.headers["Cache-Control"] = "no-store"
        return resp

    from .. import cert_lifecycle, clouds
    from ..reports import library

    # National clouds (clouds.py): the choices for tenant and app connection forms, and a label for any key;
    # every report under its area, for the Reports tab's picker (_macros.report_picker).
    app.jinja_env.globals.update(cloud_choices=clouds.CHOICES, cloud_label=lambda key: clouds.get(key).label,
                                 cloud_of=clouds.of, report_catalogue=library.catalogue)

    @app.context_processor
    def _inject():
        user = g.get("user")
        # Admins are told when the background worker isn't running: without it
        # nothing is collected and no scheduled email goes out.
        admin = bool(user and user["role"] == "admin" and "db" in g)
        worker_down = admin and not worker.is_alive(g.db)
        # ...and when a certificate Office Sentry signs in with is within 60 days of expiring.
        # Not on the page that already says so in full.
        here = {"admin.connections": "connection", "email.settings": "mail"}.get(request.endpoint)
        certificates = [e for e in cert_lifecycle.expiring(g.db) if e.kind != here] if admin else []
        return {"current_user": user, "csp_nonce": g.get("csp_nonce", ""), "worker_down": worker_down,
                "expiring_certificates": certificates, "app_version": __version__,
                "group_scope": g.get("group_scope"),
                "public_demo": bool(g.get("public_demo")),
                "public_demo_link": public_demo.link() if g.get("public_demo") else ""}

    @app.errorhandler(403)
    def _forbidden(_e):
        return render_template("error.html", code=403, message="You don't have access to this page.",
                               detail="Ask an admin if you need it."), 403

    @app.errorhandler(404)
    def _not_found(_e):
        # Tenants someone hasn't been granted are 404s too, so say both without confirming either.
        return render_template("error.html", code=404, message="Page not found.",
                               detail="The link may be out of date, or the page belongs to a tenant you haven't been "
                                      "given access to."), 404

    @app.get("/health")
    def health():
        # The version lets an uptime monitor (or a person after an update) confirm what is running.
        return {"ok": True, "version": __version__}

    @app.get("/health/ready")
    def health_ready():
        # For uptime monitors: 503 when the portal can't do its job (database, its version, the worker, a full
        # disk). Anyone sees only {"ok": ...}; the checks need the monitoring token from System status (as
        # "Authorization: Bearer <token>" or ?token=) or an admin's session. No tenant data either way.
        health = system_health.assess(g.db, cfg)
        token = request.args.get("token") or request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
        user = g.get("user")
        detailed = (user is not None and user["role"] == "admin") or system_health.token_matches(g.db, token)
        return (health.as_json() if detailed else {"ok": health.ok}), (200 if health.ok else 503)

    app.register_blueprint(auth.bp)
    app.register_blueprint(tenants.bp)
    app.register_blueprint(admin.bp)
    app.register_blueprint(reports.bp)
    app.register_blueprint(projects.bp)
    app.register_blueprint(email.bp)
    app.register_blueprint(alerts.bp)
    app.register_blueprint(branding.bp)
    app.register_blueprint(data_retention.bp)
    app.register_blueprint(across.bp)
    app.register_blueprint(people.bp)
    app.register_blueprint(history.bp)
    app.register_blueprint(saved_views.bp)
    app.register_blueprint(tenant_admin.bp)
    app.register_blueprint(activity.bp)
    app.register_blueprint(search.bp)
    app.register_blueprint(reporting.bp)
    app.register_blueprint(groups.bp)
    app.register_blueprint(upcoming.bp)
    app.register_blueprint(system.bp)
    app.register_blueprint(styleguide.bp)
    return app
