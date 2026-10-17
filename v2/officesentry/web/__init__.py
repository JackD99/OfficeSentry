"""Flask application factory."""

from __future__ import annotations

import logging
import secrets
import sqlite3
from datetime import timedelta

from flask import Flask, g, render_template, request
from flask_wtf.csrf import CSRFError, CSRFProtect
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.routing import IntegerConverter

from .. import __version__, grades, logs, system_alerts, system_health, totp, worker
from .. import sso as staff_sign_in
from ..auditlog import addon as addon_switch
from ..config import Config, load_config, setup_token
from ..crypto import Vault
from ..db import audit_counted, connect, is_locked, migrate
from ..updates import available as release_available
from . import (
    access,
    across,
    activity,
    admin,
    alerts,
    appearance,
    auth,
    branding,
    client_access,
    data_retention,
    designs,
    email,
    evidence,
    filters,
    finding,
    grade,
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
    sessions,
    site_pages,
    styleguide,
    support,
    switcher,
    system,
    tenant_admin,
    tenants,
    timeline,
    upcoming,
    updates,
)
from . import (
    audit_addon as addon_pages,
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

# The largest integer SQLite stores. Python's are unbounded, and a bigger one stops the query with OverflowError.
SQLITE_MAX_INT = 2**63 - 1


class _Id(IntegerConverter):
    """Every ``<int:...>`` in a route: a number SQLite can't hold is a 404, like any other unknown id."""

    def __init__(self, map, *args, **kwargs):
        kwargs.setdefault("max", SQLITE_MAX_INT)
        super().__init__(map, *args, **kwargs)


def create_app(cfg: Config | None = None) -> Flask:
    cfg = cfg or load_config()
    logs.setup(cfg.data_dir, "web")
    app = Flask(__name__)
    app.url_map.converters["int"] = _Id  # before any route is added
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
                "No admin account yet. To create one, open %s/setup?token=%s",
                cfg.base_url,
                setup_token(cfg.data_dir),
                extra={
                    logs.IN_FILE: "No admin account yet. The link to create one is in the portal's own output "
                    "(docker compose logs web), or run: python -m officesentry setup-link"
                },
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
        report_catalogue=lambda tenant_id=None: library.catalogue(
            reports.hidden_reports(tenant_id) if g.get("db") is not None else frozenset()
        ),
        can=access.can,
        # Whether a link opens for the signed-in person: templates draw it only then (access.can_open).
        can_open=access.can_open,
        manages_groups=access.manages_groups,
        # The client switcher beside a client's name (_macros.tenant_header): this page for the other clients.
        client_switch=switcher.choices,
        report_areas_open=reports.report_groups,
        report_capability=reports.report_capability,
        entry_capability=reports.entry_capability,
        # The MSP's custom reports and the person's saved views, as the report menus list them.
        custom_reports=reports.custom_reports,
        my_views=reports.my_views,
        # Whether the audit log add-on is switched on for this install (``auditlog/addon.py``): until it is,
        # nothing about it shows.
        audit_addon_on=lambda: g.get("db") is not None and addon_switch.enabled(g.db),
        # A client's grade as last stored (grades.py), for the badge in the client header: never built there.
        client_grade=lambda tenant_id: grades.stored_letter(g.db, tenant_id) if g.get("db") is not None else None,
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
        # A newer release, for the note under the version at the foot of the menu (Settings → Updates).
        newer = release_available(g.db) if a and a.can("system.update") and not g.get("public_demo") else None
        return {
            "current_user": user,
            "current_role": a.role if a else None,
            # Light or dark and the colour theme this person chose (Account, Appearance): base.html's <html>.
            "look": appearance.look(user),
            "csp_nonce": g.get("csp_nonce", ""),
            "worker_down": worker_down,
            "expiring_certificates": certificates,
            "app_version": __version__,
            "update_available": newer,
            "group_scope": g.get("group_scope"),
            "public_demo": bool(g.get("public_demo")),
            "public_demo_link": public_demo.link() if g.get("public_demo") else "",
            # Sign in with Microsoft (sso.py): the sign-in page's button, who it's for, whether staff may still use
            # passwords, and whether someone without a password has just confirmed a change with Microsoft.
            "microsoft_sign_in": staff_sign_in.shown(sign_in),
            "microsoft_staff": staff_sign_in.enabled(sign_in),
            "microsoft_clients": staff_sign_in.clients_enabled(sign_in),
            "staff_passwords_off": staff_sign_in.enabled(sign_in) and sign_in.get("sso_passwords") == "off",
            "no_password": bool(user) and not staff_sign_in.has_password(user),
            "confirmed_with_microsoft": bool(user)
            and sessions.recently_confirmed(g.get("session_row"), staff_sign_in.CONFIRM_MINUTES),
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

    @app.errorhandler(OverflowError)
    def _too_big(exc):
        # A number in the address or a form (?page=, a client or role picked) past what SQLite stores: the
        # request is wrong, not the portal. Any other overflow stays a 500.
        if "SQLite INTEGER" not in str(exc):
            raise exc
        return _error(400, "A number in that request is too big.", "Go back and try again.")

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
    app.register_blueprint(finding.bp)
    app.register_blueprint(projects.bp)
    app.register_blueprint(grade.bp)
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
    app.register_blueprint(timeline.bp)
    app.register_blueprint(saved_views.bp)
    app.register_blueprint(tenant_admin.bp)
    app.register_blueprint(onboarding.bp)
    app.register_blueprint(addon_pages.bp)
    app.register_blueprint(activity.bp)
    app.register_blueprint(search.bp)
    app.register_blueprint(reporting.bp)
    app.register_blueprint(groups.bp)
    app.register_blueprint(upcoming.bp)
    app.register_blueprint(system.bp)
    app.register_blueprint(support.bp)
    app.register_blueprint(updates.bp)
    app.register_blueprint(styleguide.bp)
    app.register_blueprint(designs.bp)
    app.register_blueprint(evidence.bp)
    app.register_blueprint(sso_web.bp)
    app.register_blueprint(client_access.bp)
    return app
