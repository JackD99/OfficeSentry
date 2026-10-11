"""Admin pages: users and app connections."""

from __future__ import annotations

import re
from urllib.parse import urlsplit

from flask import (
    Blueprint,
    Response,
    abort,
    current_app,
    flash,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)
from werkzeug.security import generate_password_hash

from .. import app_setup, cert_lifecycle, clouds, entra, store, tenant_groups, throttle
from ..certs import cert_der, generate_certificate
from ..db import audit, transaction, utcnow
from ..entra import EXCHANGE_PERMISSIONS, GRAPH_PERMISSIONS
from ..text import plural
from . import sessions
from .access import ROLES, roles_required
from .auth import confirm_password, password_problem

bp = Blueprint("admin", __name__, url_prefix="/admin")

GUID_RE = re.compile(r"^[0-9a-fA-F]{8}-([0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}$")


def _user_or_404(user_id: int):
    user = g.db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if user is None:
        abort(404)
    return user


def _admin_password_ok() -> bool:
    """The admin's own password, asked again before resetting someone's password, two-step sign-in or
    sessions, as the account page asks before the same changes: a stolen admin session isn't enough.
    Wrong answers count towards the admin's own lock (``auth.confirm_password``)."""
    return confirm_password("admin_password", "Your password isn't right, so nothing was changed.")


# -- Users -------------------------------------------------------------------


@bp.get("/users")
@roles_required("admin")
def users():
    rows = g.db.execute("SELECT * FROM users ORDER BY username COLLATE NOCASE").fetchall()
    cfg = current_app.extensions["officesentry.config"]
    two_step = {
        "policy": cfg.mfa_policy(g.db),
        "from_env": cfg.require_mfa_explicit,
        "clients_without": sum(
            1 for u in rows if u["role"] == "client" and not u["totp_enabled_at"] and not u["disabled"]
        ),
    }
    group_grants = dict(g.db.execute("SELECT user_id, COUNT(*) FROM user_tenant_groups GROUP BY user_id").fetchall())
    return render_template("users.html", users=rows, roles=ROLES, two_step=two_step, group_grants=group_grants)


@bp.post("/users")
@roles_required("admin")
def create_user():
    username = request.form.get("username", "").strip()
    role = request.form.get("role", "")
    password = request.form.get("password", "")
    problem = password_problem(password)
    if not username or len(username) > 64:
        problem = "Enter a username (up to 64 characters)."
    elif role not in ROLES:
        problem = "Pick a role."
    elif g.db.execute("SELECT 1 FROM users WHERE username = ?", (username,)).fetchone():
        problem = "That username is taken."
    if problem:
        flash(problem, "error")
        return redirect(url_for("admin.users"))
    cur = g.db.execute(
        # The admin chose this password, so they replace it at first sign-in.
        "INSERT INTO users (username, password_hash, role, created_at, must_change_password) VALUES (?, ?, ?, ?, 1)",
        (username, generate_password_hash(password), role, utcnow()),
    )
    audit(g.db, g.user["username"], "user_created", detail=f"{username} ({role})")
    return redirect(url_for("admin.edit_user", user_id=cur.lastrowid))


@bp.route("/users/<int:user_id>", methods=["GET", "POST"])
@roles_required("admin")
def edit_user(user_id: int):
    user = _user_or_404(user_id)
    # Archived tenants are listed too, so saving the form keeps grants for when they're restored.
    tenants = g.db.execute("SELECT id, name, archived_at FROM tenants ORDER BY name COLLATE NOCASE").fetchall()
    if request.method == "POST":
        if request.form.get("password") and not _admin_password_ok():
            return redirect(url_for("admin.edit_user", user_id=user_id))
        role = request.form.get("role", user["role"])
        if role not in ROLES:
            abort(400)
        if user["id"] == g.user["id"] and role != "admin":
            flash("You can't remove your own admin role.", "error")
            return redirect(url_for("admin.edit_user", user_id=user_id))
        all_tenants = int(role == "analyst" and request.form.get("all_tenants") == "1")
        disabled = int(request.form.get("disabled") == "1" and user["id"] != g.user["id"])
        valid_ids = {t["id"] for t in tenants}
        grants = {int(x) for x in request.form.getlist("tenants") if x.isdigit()} & valid_ids
        # Only analysts can be granted a whole group (access.visible_tenants ignores it for anyone else).
        valid_groups = {gr["id"] for gr in tenant_groups.all_groups(g.db)}
        group_grants = (
            {int(x) for x in request.form.getlist("groups") if x.isdigit()} & valid_groups
            if role == "analyst"
            else set()
        )
        changed_access = role != user["role"] or disabled != user["disabled"] or all_tenants != user["all_tenants"]
        with transaction(g.db):
            g.db.execute(
                "UPDATE users SET role = ?, all_tenants = ?, disabled = ?,"
                " session_version = session_version + ? WHERE id = ?",
                (role, all_tenants, disabled, int(changed_access), user_id),
            )
            g.db.execute("DELETE FROM user_tenants WHERE user_id = ?", (user_id,))
            g.db.executemany(
                "INSERT INTO user_tenants (user_id, tenant_id) VALUES (?, ?)", [(user_id, t) for t in sorted(grants)]
            )
            tenant_groups.set_user_groups(g.db, user_id, group_grants)
            audit(
                g.db,
                g.user["username"],
                "user_updated",
                detail=f"{user['username']}: role={role} all={all_tenants} disabled={disabled} "
                f"tenants={sorted(grants)}" + (f" groups={sorted(group_grants)}" if group_grants else ""),
            )
        if request.form.get("password"):
            if problem := password_problem(request.form["password"]):
                flash(problem, "error")
            else:
                g.db.execute(
                    "UPDATE users SET password_hash = ?, session_version = session_version + 1,"
                    " failed_logins = 0, locked_until = NULL, must_change_password = 1 WHERE id = ?",
                    (generate_password_hash(request.form["password"]), user_id),
                )
                audit(g.db, g.user["username"], "user_password_reset", detail=user["username"])
        flash("Saved.", "success")
        return redirect(url_for("admin.edit_user", user_id=user_id))
    granted = {r["tenant_id"] for r in g.db.execute("SELECT tenant_id FROM user_tenants WHERE user_id = ?", (user_id,))}
    return render_template(
        "user_edit.html",
        user=user,
        tenants=tenants,
        granted=granted,
        roles=ROLES,
        groups=tenant_groups.all_groups(g.db),
        granted_groups=tenant_groups.user_groups(g.db, user_id),
        sessions=sessions.list_for(user_id),
        locked=_is_locked(user),
        failures=throttle.recent_failures(g.db, user["username"]),
        two_step_required=current_app.extensions["officesentry.config"].mfa_required(user["role"], g.db),
    )


def _is_locked(user) -> bool:
    return bool(user["locked_until"]) and user["locked_until"] > utcnow()


@bp.post("/users/<int:user_id>/reset-two-step")
@roles_required("admin")
def reset_two_step(user_id: int):
    """For a lost phone: remove their authenticator and sign them out. They set it up again at next sign-in."""
    user = _user_or_404(user_id)
    if not _admin_password_ok():
        return redirect(url_for("admin.edit_user", user_id=user_id))
    with transaction(g.db):
        g.db.execute(
            "UPDATE users SET totp_secret_enc = NULL, totp_pending_enc = NULL, totp_enabled_at = NULL,"
            " totp_last_step = NULL, session_version = session_version + 1 WHERE id = ?",
            (user_id,),
        )
        g.db.execute("DELETE FROM user_recovery_codes WHERE user_id = ?", (user_id,))
        g.db.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
        audit(g.db, g.user["username"], "user_two_step_reset", detail=user["username"])
    if user_id == g.user["id"]:
        return redirect(url_for("auth.login"))
    flash(f"Two-step sign-in reset for {user['username']}. They'll set it up again next time they sign in.", "success")
    return redirect(url_for("admin.edit_user", user_id=user_id))


@bp.post("/users/<int:user_id>/sign-out")
@roles_required("admin")
def sign_out_user(user_id: int):
    user = _user_or_404(user_id)
    if not _admin_password_ok():
        return redirect(url_for("admin.edit_user", user_id=user_id))
    ended = sessions.revoke_user(user_id, except_current=user_id == g.user["id"])
    audit(g.db, g.user["username"], "user_signed_out", detail=f"{user['username']}: {plural(ended, 'session')}")
    flash(f"Signed {user['username']} out of {ended} browser{'s' if ended != 1 else ''}.", "success")
    return redirect(url_for("admin.edit_user", user_id=user_id))


@bp.post("/users/<int:user_id>/unlock")
@roles_required("admin")
def unlock_user(user_id: int):
    user = _user_or_404(user_id)
    g.db.execute("UPDATE users SET failed_logins = 0, locked_until = NULL WHERE id = ?", (user_id,))
    throttle.clear(g.db, user["username"])
    audit(g.db, g.user["username"], "user_unlocked", detail=user["username"])
    flash(f"{user['username']} can sign in again.", "success")
    return redirect(url_for("admin.edit_user", user_id=user_id))


@bp.post("/users/<int:user_id>/delete")
@roles_required("admin")
def delete_user(user_id: int):
    user = _user_or_404(user_id)
    if user["id"] == g.user["id"]:
        flash("You can't delete your own account.", "error")
        return redirect(url_for("admin.edit_user", user_id=user_id))
    g.db.execute("DELETE FROM users WHERE id = ?", (user_id,))
    audit(g.db, g.user["username"], "user_deleted", detail=user["username"])
    flash(f"Deleted {user['username']}.", "success")
    return redirect(url_for("admin.users"))


# -- App connections ---------------------------------------------------------


def _redirect_uri() -> str:
    return current_app.extensions["officesentry.config"].base_url + url_for("tenants.consent_callback")


def _host(url: str) -> tuple[str, int | None]:
    parts = urlsplit(url)
    return (parts.hostname or "", None if parts.port in (80, 443) else parts.port)


def _address_problem() -> str | None:
    """The address this page was opened at, when it isn't the one consent links send people back to."""
    same = _host(current_app.extensions["officesentry.config"].base_url) == _host("//" + request.host)
    return None if same else request.host


@bp.get("/connections")
@roles_required("admin")
def connections():
    rows = store.list_connections(g.db)
    sign_in = app_setup.current(g.db, g.user["id"])
    if (
        sign_in
        and sign_in["status"] == "done"
        and g.db.execute("SELECT 1 FROM tenants WHERE connection_id = ?", (sign_in["connection_id"],)).fetchone()
    ):
        app_setup.cancel(g.db, g.user["id"])  # the first tenant is added: the wizard is finished
        sign_in = None
    return render_template(
        "connections.html",
        connections=rows,
        permissions=GRAPH_PERMISSIONS,
        exchange_permissions=EXCHANGE_PERMISSIONS,
        redirect_uri=_redirect_uri(),
        expiry={c["id"]: cert_lifecycle.connection_expiry(c) for c in rows},
        test_tenant={c["id"]: cert_lifecycle.test_tenant(g.db, c["id"]) for c in rows if c["next_thumbprint"]},
        sign_in=sign_in,
        seconds_left=app_setup.seconds_left(sign_in) if sign_in else 0,
        opened_at=_address_problem(),
        default_app_name=app_setup.DEFAULT_NAME,
        ready_clouds={clouds.of(c).key for c in rows if c["client_id"]},
        pending_clouds={clouds.of(c).key for c in rows if not c["client_id"]},
    )


@bp.post("/connections")
@roles_required("admin")
def create_connection():
    name = request.form.get("name", "").strip() or "Office Sentry"
    cloud = request.form.get("cloud", clouds.DEFAULT)
    if not clouds.valid(cloud):
        abort(400)
    cert = generate_certificate(f"Office Sentry - {name}"[:64])
    vault = current_app.extensions["officesentry.vault"]
    conn_id = store.create_connection(g.db, vault, name[:100], cert, cloud=cloud)
    audit(g.db, g.user["username"], "connection_created", detail=f"{name} {cert.thumbprint} {cloud}")
    flash("Certificate generated. Upload the .cer to your app registration, then save its client ID.", "success")
    return redirect(url_for("admin.connections") + f"#conn-{conn_id}")


# -- Setup wizard: sign in to Microsoft from this page and create the app (app_setup.py) -------------------


def _to_setup():
    return redirect(url_for("admin.connections") + "#setup")


@bp.post("/connections/setup")
@roles_required("admin")
def start_setup():
    cloud = request.form.get("cloud", clouds.DEFAULT)
    if not clouds.valid(cloud):
        abort(400)
    tenant = app_setup.tenant_ref(request.form.get("tenant", ""))
    name = request.form.get("name", "").strip()[:100] or app_setup.DEFAULT_NAME
    if tenant is None:
        flash(
            "Enter your tenant's domain (like contoso.onmicrosoft.com) or its Directory (tenant) ID, "
            "or leave it blank.",
            "error",
        )
    elif app_setup.ready_connection(g.db, cloud):
        flash(
            f"You already have an app for {clouds.get(cloud).label}, so there's nothing to create. "
            "Add tenants with it, or replace its certificate below if that's what you need.",
            "error",
        )
    else:
        try:
            app_setup.start(
                g.db,
                current_app.extensions["officesentry.vault"],
                g.user["id"],
                g.user["username"],
                tenant,
                cloud,
                name,
            )
        except entra.SignInError as exc:
            flash(str(exc), "error")
    return _to_setup()


@bp.post("/connections/setup/check")
@roles_required("admin")
def check_setup():
    """One step of the wizard. The page calls this every few seconds while it waits (JSON); without
    JavaScript, the "I've signed in" button posts it and comes back to the page."""
    row = app_setup.check(
        g.db, current_app.extensions["officesentry.vault"], g.user["id"], g.user["username"], _redirect_uri()
    )
    if request.accept_mimetypes.best == "application/json":
        if row is None:
            return jsonify(status="none")
        return jsonify(
            status=row["status"],
            note=row["error"] if row["status"] == "waiting" else None,
            wait=row["poll_seconds"],
            seconds_left=app_setup.seconds_left(row),
        )
    if row is not None and row["status"] == "waiting" and not row["error"]:
        flash(
            "Microsoft says sign-in hasn't finished yet. Enter the code on Microsoft's page, then check again.",
            "warning",
        )
    return _to_setup()


@bp.post("/connections/setup/cancel")
@roles_required("admin")
def cancel_setup():
    app_setup.cancel(g.db, g.user["id"])
    return redirect(url_for("admin.connections"))


def _consent_again(conn_id: int, old: str, new: str) -> int:
    """The connection now names a different app: consent its tenants gave the old one says nothing about it,
    and consent links already sent (``onboarding``) name the old one. Returns how many tenants use it."""
    ids = [r[0] for r in g.db.execute("SELECT id FROM tenants WHERE connection_id = ?", (conn_id,))]
    g.db.execute("UPDATE tenants SET consented_at = NULL WHERE connection_id = ?", (conn_id,))
    g.db.execute(
        "UPDATE onboarding SET sent_at = NULL, sent_by = NULL, approved_at = NULL, checked_at = NULL,"
        " check_error = NULL WHERE tenant_id IN (SELECT id FROM tenants WHERE connection_id = ?)",
        (conn_id,),
    )
    for tenant_id in ids:
        audit(g.db, g.user["username"], "tenant_consent_reset", tenant_id, f"its app's client ID changed from {old}")
    return len(ids)


@bp.post("/connections/<int:conn_id>/client-id")
@roles_required("admin")
def set_client_id(conn_id: int):
    connection = store.get_connection(g.db, conn_id)
    if connection is None:
        abort(404)
    client_id = request.form.get("client_id", "").strip().lower()
    home = request.form.get("home_tenant", "").strip().lower()
    if not GUID_RE.match(client_id):
        flash("The client ID is the app registration's Application (client) ID, a GUID.", "error")
    elif home and not GUID_RE.match(home):
        flash("The tenant ID is the Directory (tenant) ID on the app's overview page, a GUID.", "error")
    else:
        old = connection["client_id"]
        with transaction(g.db):
            g.db.execute("UPDATE app_connections SET client_id = ? WHERE id = ?", (client_id, conn_id))
            audit(g.db, g.user["username"], "connection_client_id_set", detail=client_id)
            again = _consent_again(conn_id, old, client_id) if old and old != client_id else 0
        if not home:
            flash("Client ID saved.", "success")
        else:
            # Sign in to the tenant the app was created in, to prove the certificate is on the app.
            from ..connection_check import check

            result = check(
                home,
                client_id,
                current_app.extensions["officesentry.vault"].decrypt(connection["key_enc"]),
                connection["thumbprint"],
                clouds.of(connection).key,
            )
            if result.ok:
                flash(f"Client ID saved and checked: {result.title} The certificate works.", "success")
            else:
                flash(f"Client ID saved, but the check failed. {result.title} {result.fix}", "error")
        if again:
            flash(
                f"The client ID changed, so {plural(again, 'client tenant')} must approve the app again before "
                "Office Sentry can read their data. Send each one its new consent link from Onboarding.",
                "warning",
            )
    return redirect(url_for("admin.connections"))


@bp.get("/connections/<int:conn_id>/certificate.cer")
@roles_required("admin")
def download_certificate(conn_id: int):
    """Public certificate only (the current one, or with ?which=next its replacement). The private key never
    leaves the database."""
    connection = store.get_connection(g.db, conn_id)
    if connection is None:
        abort(404)
    if request.args.get("which") == "next":
        if not connection["next_cert_pem"]:
            abort(404)
        pem, name = connection["next_cert_pem"], f"office-sentry-{conn_id}-new"
    else:
        pem, name = connection["cert_pem"], f"office-sentry-{conn_id}"
    return Response(
        cert_der(pem),
        mimetype="application/pkix-cert",
        headers={"Content-Disposition": f"attachment; filename={name}.cer"},
    )


# -- Replacing a connection's certificate (cert_lifecycle) -----------------------------------


def _to_connection(conn_id: int):
    return redirect(url_for("admin.connections") + f"#conn-{conn_id}")


def _rollover(conn_id: int, step) -> bool:
    """Run a rollover step, flashing why it can't be done. Returns whether it ran."""
    if store.get_connection(g.db, conn_id) is None:
        abort(404)
    try:
        step()
    except cert_lifecycle.RolloverError as exc:
        flash(str(exc), "error")
        return False
    return True


@bp.post("/connections/<int:conn_id>/replace")
@roles_required("admin")
def start_rollover(conn_id: int):
    vault = current_app.extensions["officesentry.vault"]
    if _rollover(conn_id, lambda: cert_lifecycle.start(g.db, vault, conn_id, g.user["username"])):
        flash(
            "New certificate made. Collection keeps using the current one until you switch. Next: upload the "
            "new .cer to the app registration.",
            "success",
        )
    return _to_connection(conn_id)


@bp.post("/connections/<int:conn_id>/replace/test")
@roles_required("admin")
def test_rollover(conn_id: int):
    tenant_ref = request.form.get("tenant_ref", "").strip().lower()
    if not GUID_RE.match(tenant_ref) and not re.fullmatch(r"[a-z0-9-]+(\.[a-z0-9-]+)+", tenant_ref):
        flash(
            "Enter a tenant ID or domain to sign in to: your own tenant's, or a client's that has consented.", "error"
        )
        return _to_connection(conn_id)
    vault = current_app.extensions["officesentry.vault"]
    outcome = []
    if _rollover(
        conn_id, lambda: outcome.append(cert_lifecycle.test(g.db, vault, conn_id, tenant_ref, g.user["username"]))
    ):
        result = outcome[0]
        if result.ok:
            flash(f"The new certificate works: {result.title} You can switch to it now.", "success")
        else:
            flash(f"The new certificate didn't work yet. {result.title} {result.fix}", "error")
    return _to_connection(conn_id)


@bp.post("/connections/<int:conn_id>/replace/switch")
@roles_required("admin")
def switch_rollover(conn_id: int):
    if _rollover(conn_id, lambda: cert_lifecycle.switch(g.db, conn_id, g.user["username"])):
        flash(
            "Switched. Collection signs in with the new certificate from now on. Last step: remove the old "
            "certificate from the app registration.",
            "success",
        )
    return _to_connection(conn_id)


@bp.post("/connections/<int:conn_id>/replace/cancel")
@roles_required("admin")
def cancel_rollover(conn_id: int):
    if _rollover(conn_id, lambda: cert_lifecycle.cancel(g.db, conn_id, g.user["username"])):
        flash(
            "Replacement cancelled. If you uploaded its .cer to the app registration, delete it there too.", "success"
        )
    return _to_connection(conn_id)


@bp.post("/connections/<int:conn_id>/old-removed")
@roles_required("admin")
def old_certificate_removed(conn_id: int):
    if _rollover(conn_id, lambda: cert_lifecycle.forget_old(g.db, conn_id, g.user["username"])):
        flash("Noted. The old certificate is no longer listed here.", "success")
    return _to_connection(conn_id)
