"""Flask application factory."""

from __future__ import annotations

import logging
import secrets
import sqlite3
from datetime import timedelta

from flask import Flask, g, render_template, request
from flask_wtf.csrf import CSRFError, CSRFProtect
from werkzeug.middleware.proxy_fix import ProxyFix

from .. import __version__, logs, system_alerts, system_health, totp, worker
from .. import sso as staff_sign_in
from ..config import Config, load_config, setup_token
from ..crypto import Vault
from ..db import audit_counted, connect, is_locked, migrate
from . import (
    access,
    across,
    activity,
    admin,
    alerts,
    auth,
    branding,
    data_retention,
    designs,
    email,
    evidence,
    filters,
    group_pages,
    groups,
    history,
    home,
    mailboxes,
    onboarding,
    people,
    projects,
    public_demo,
    reporting,
    reports,
    saved_views,
    search,
    site_pages,
    styleguide,
    support,
    system,
    tenant_admin,
    tenants,
    upcoming,
)
from . import (
    roles as role_pages,
)
from . import (
    sso as sso_web,
)

csrf = CSRFProtect()
log = logging.getLogger(__name__)

# How long a page's write waits for the write lock before the portal says it's busy (the 503 below). The
# worker waits its usual 30 seconds; a person at a browser shouldn't, and a lock held longer than this is a
# restore, a backup or an erase's VACUUM, which no wait of a few seconds outlasts.
WEB_BUSY_TIMEOUT_MS = 5_000


def create_app(cfg: Config | None = None) -> Flask:
    cfg = cfg or load_config()
    logs.setup(cfg.data_dir, "web")
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
        # Behind a reverse proxy: take the client IP, scheme and host from its headers. Not a path prefix: the
        # portal always runs at the root of its address, and proxies pass X-Forwarded-Prefix on from the visitor
        # untouched, which would let a request point every link on its own page somewhere else.
        n = cfg.trusted_proxies
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=n, x_proto=n, x_host=n, x_port=n)

    conn = connect(cfg.db_path)
    try:
        migrate(conn)
        try:
            if rekeyed := totp.rekey_old_codes(conn, app.extensions["officesentry.vault"]):
                log.info("Recovery codes stored before 2.2.0 now use the recovery code key (%d).", rekeyed)
        except Exception:  # those codes stop working until it succeeds; the portal still starts
            log.exception("Couldn't re-key recovery codes stored before 2.2.0")
        if conn.execute("SELECT 1 FROM users LIMIT 1").fetchone() is None:
            app.logger.warning(
                "No admin account yet. To create one, open %s/setup?token=%s", cfg.base_url, setup_token(cfg.data_dir)
            )
    finally:
        conn.close()

    # Before CSRFProtect's own check, so the page refusing a form sent with a stale token has a nonce for its
    # script too.
    @app.before_request
    def _csp_nonce():
        g.csp_nonce = secrets.token_urlsafe(16)

    csrf.init_app(app)
    filters.register(app)

    @app.before_request
    def _open_db():
        # The worker can't report that it has stopped, so the portal watches it (system_alerts.py).
        system_alerts.start_watchdog(app, cfg)
        g.db = connect(cfg.db_path, busy_timeout_ms=WEB_BUSY_TIMEOUT_MS)
        access.load_user()
        # Every line logged while handling this request names it (logs.py); the id goes back in the
        # X-Request-ID header and on the error page, so a report can quote it.
        g.request_id = logs.new_request_id()
        user = g.get("user")
        g.log_token = logs.push(
            req=g.request_id,
            user=user["username"] if user else None,
            tenant=(request.view_args or {}).get("tenant_id"),
        )

    # On the public demo site, nothing but reading (after load_user, which sets g.public_demo).
    app.before_request(public_demo.refuse_changes)
    app.after_request(public_demo.add_headers)

    @app.teardown_request
    def _close_db(_exc):
        db = g.pop("db", None)
        if db is not None:
            db.close()
        logs.pop(g.pop("log_token", None))

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
        if request_id := g.get("request_id"):
            resp.headers["X-Request-ID"] = request_id
        resp.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        # A page the portal opens can't keep a handle on it, and no other site can embed its pages, downloads,
        # brand logo or files (emails carry the logo as image data, not a link).
        resp.headers["Cross-Origin-Opener-Policy"] = "same-origin"
        resp.headers["Cross-Origin-Resource-Policy"] = "same-origin"
        resp.headers["X-Permitted-Cross-Domain-Policies"] = "none"
        # Only over HTTPS, as browsers only take it from there: a portal behind a proxy that doesn't say the
        # visitor came over HTTPS (X-Forwarded-Proto) then shows it by the header missing
        # (docs/securing-the-portal.md, "Checking it worked").
        if cfg.secure_cookies and request.is_secure:
            resp.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        # Static files and the public brand colours and logo may be cached; nothing else is.
        if request.endpoint not in ("static", "branding.brand_css", "branding.logo"):
            resp.headers["Cache-Control"] = "no-store"
        return resp

    from .. import cert_lifecycle, clouds
    from ..reports import library

    # National clouds (clouds.py): the choices for tenant and app connection forms, and a label for any key;
    # every report under its area, for the Reports tab's picker (_macros.report_picker).
    app.jinja_env.globals.update(
        cloud_choices=clouds.CHOICES,
        cloud_label=lambda key: clouds.get(key).label,
        cloud_of=clouds.of,
        report_catalogue=library.catalogue,
        can=access.can,
        report_areas_open=reports.report_groups,
        report_capability=reports.report_capability,
        entry_capability=reports.entry_capability,
    )

    @app.context_processor
    def _inject():
        user = g.get("user")
        # Admins are told when the background worker isn't running: without it
        # nothing is collected and no scheduled email goes out.
        admin = bool(user and "db" in g and access.can("settings.manage"))
        worker_down = admin and not worker.is_alive(g.db)
        # ...and when a certificate Office Sentry signs in with is within 60 days of expiring.
        # Not on the page that already says so in full.
        here = {"admin.connections": "connection", "email.settings": "mail", "sso.settings_page": "sign_in"}.get(
            request.endpoint
        )
        certificates = [e for e in cert_lifecycle.expiring(g.db) if e.kind != here] if admin else []
        a = access.current() if user and "db" in g else None
        sign_in = staff_sign_in.settings(g.db) if g.get("db") is not None else {}
        return {
            "current_user": user,
            "current_role": a.role if a else None,
            "csp_nonce": g.get("csp_nonce", ""),
            "worker_down": worker_down,
            "expiring_certificates": certificates,
            "app_version": __version__,
            "group_scope": g.get("group_scope"),
            "public_demo": bool(g.get("public_demo")),
            "public_demo_link": public_demo.link() if g.get("public_demo") else "",
            # Sign in with Microsoft (sso.py): the sign-in page's button, and whether staff may still use passwords.
            "microsoft_sign_in": staff_sign_in.enabled(sign_in),
            "staff_passwords_off": sign_in.get("sso_passwords") == "off",
        }

    def _error(code: int, message: str, detail: str = ""):
        return render_template("error.html", code=code, message=message, detail=detail), code

    @app.errorhandler(sqlite3.OperationalError)
    def _busy(exc):
        # A write that waited WEB_BUSY_TIMEOUT_MS for the lock and still found it held (a restore, a backup,
        # an erase's VACUUM): a page that says so and asks for another go, not a 500. Other errors stay errors.
        if not is_locked(exc):
            raise exc
        log.warning("The database was locked for %s %s; asked the person to try again", request.method, request.path)
        page = render_template(
            "error.html",
            code=503,
            message="Office Sentry is busy right now.",
            detail="Something else is writing to its database, such as a restore, a backup or a large delete. "
            "Wait a moment, then try again.",
        )
        return page, 503, {"Retry-After": "10"}

    @app.errorhandler(403)
    def _forbidden(_e):
        # Refusals raised with abort(403) outside needs() (a client on a staff page) are counted too.
        access.refused()
        return _error(403, "You don't have access to this page.", "Ask an admin if you need it.")

    @app.errorhandler(404)
    def _not_found(_e):
        # Tenants someone hasn't been granted are 404s too, so say both without confirming either.
        return _error(
            404,
            "Page not found.",
            "The link may be out of date, or the page belongs to a client you haven't been given access to.",
        )

    @app.errorhandler(CSRFError)
    def _csrf_failed(_e):
        # The form's token didn't match the session: usually a page left open past sign-out or the idle limit,
        # sometimes a request another site tried to make. Counted per address and hour, with the person's name
        # when a session is still there (the check runs before the request's user is loaded).
        if "db" not in g:
            g.db = connect(cfg.db_path)
        if "user" not in g:
            access.load_user()
        user = g.get("user")
        audit_counted(
            g.db, "csrf_failed", (request.remote_addr or "")[:64], user["username"] if user else None, request.path
        )
        return _error(
            400,
            "That form couldn't be sent.",
            "Your session ended, or the page was open too long. Go back, reload the page and try again.",
        )

    @app.errorhandler(400)
    def _bad_request(_e):
        return _error(400, "That request couldn't be understood.", "Go back and try again.")

    @app.errorhandler(405)
    def _method_not_allowed(_e):
        return _error(405, "That page can't be opened this way.", "Use the portal's own links and buttons.")

    @app.errorhandler(413)
    def _too_large(_e):
        limit = app.config["MAX_CONTENT_LENGTH"] // (1024 * 1024)
        return _error(413, "That file is too large.", f"Uploads are limited to {limit} MB.")

    @app.errorhandler(500)
    def _failed(_e):
        # Flask has already logged the traceback with this request's id in its context.
        return render_template(
            "error.html",
            code=500,
            message="Something went wrong on our side.",
            detail="It has been logged. Try again; if it keeps happening, quote the reference below when you ask "
            "for help.",
            reference=g.get("request_id"),
        ), 500

    @app.get("/health")
    def health():
        # The version lets an uptime monitor (or a person after an update) confirm what is running.
        return {"ok": True, "version": __version__}

    @app.get("/health/ready")
    def health_ready():
        # For uptime monitors: 503 when the portal can't do its job (database, its version, the worker, a full
        # disk). Anyone sees only {"ok": ...}; the checks need the monitoring token from System status (as
        # "Authorization: Bearer <token>", never in the address, which proxies log) or an admin's session.
        # No tenant data either way.
        health = system_health.assess(g.db, cfg)
        token = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
        user = g.get("user")
        detailed = (user is not None and access.can("system.manage")) or system_health.token_matches(g.db, token)
        return (health.as_json() if detailed else {"ok": health.ok}), (200 if health.ok else 503)

    app.register_blueprint(auth.bp)
    app.register_blueprint(tenants.bp)
    app.register_blueprint(home.bp)
    app.register_blueprint(admin.bp)
    app.register_blueprint(role_pages.bp)
    app.register_blueprint(reports.bp)
    app.register_blueprint(projects.bp)
    app.register_blueprint(email.bp)
    app.register_blueprint(alerts.bp)
    app.register_blueprint(branding.bp)
    app.register_blueprint(data_retention.bp)
    app.register_blueprint(across.bp)
    app.register_blueprint(people.bp)
    app.register_blueprint(mailboxes.bp)
    app.register_blueprint(group_pages.bp)
    app.register_blueprint(site_pages.bp)
    app.register_blueprint(history.bp)
    app.register_blueprint(saved_views.bp)
    app.register_blueprint(tenant_admin.bp)
    app.register_blueprint(onboarding.bp)
    app.register_blueprint(activity.bp)
    app.register_blueprint(search.bp)
    app.register_blueprint(reporting.bp)
    app.register_blueprint(groups.bp)
    app.register_blueprint(upcoming.bp)
    app.register_blueprint(system.bp)
    app.register_blueprint(support.bp)
    app.register_blueprint(styleguide.bp)
    app.register_blueprint(designs.bp)
    app.register_blueprint(evidence.bp)
    app.register_blueprint(sso_web.bp)
    return app
