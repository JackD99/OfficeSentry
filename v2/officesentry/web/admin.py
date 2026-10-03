"""Admin pages: users and app connections."""

from __future__ import annotations

import re

from flask import Blueprint, Response, abort, current_app, flash, g, redirect, render_template, request, url_for
from werkzeug.security import generate_password_hash

from .. import store
from ..certs import cert_der, generate_certificate
from ..db import audit, transaction, utcnow
from ..entra import EXCHANGE_PERMISSIONS, GRAPH_PERMISSIONS
from . import sessions
from .access import ROLES, roles_required
from .auth import password_problem

bp = Blueprint("admin", __name__, url_prefix="/admin")

GUID_RE = re.compile(r"^[0-9a-fA-F]{8}-([0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}$")


def _user_or_404(user_id: int):
    user = g.db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if user is None:
        abort(404)
    return user


# -- Users -------------------------------------------------------------------


@bp.get("/users")
@roles_required("admin")
def users():
    rows = g.db.execute("SELECT * FROM users ORDER BY username COLLATE NOCASE").fetchall()
    return render_template("users.html", users=rows, roles=ROLES)


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
        "INSERT INTO users (username, password_hash, role, created_at) VALUES (?, ?, ?, ?)",
        (username, generate_password_hash(password), role, utcnow()),
    )
    audit(g.db, g.user["username"], "user_created", detail=f"{username} ({role})")
    return redirect(url_for("admin.edit_user", user_id=cur.lastrowid))


@bp.route("/users/<int:user_id>", methods=["GET", "POST"])
@roles_required("admin")
def edit_user(user_id: int):
    user = _user_or_404(user_id)
    tenants = g.db.execute("SELECT id, name FROM tenants ORDER BY name COLLATE NOCASE").fetchall()
    if request.method == "POST":
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
        changed_access = (role != user["role"] or disabled != user["disabled"]
                          or all_tenants != user["all_tenants"])
        with transaction(g.db):
            g.db.execute(
                "UPDATE users SET role = ?, all_tenants = ?, disabled = ?,"
                " session_version = session_version + ? WHERE id = ?",
                (role, all_tenants, disabled, int(changed_access), user_id),
            )
            g.db.execute("DELETE FROM user_tenants WHERE user_id = ?", (user_id,))
            g.db.executemany("INSERT INTO user_tenants (user_id, tenant_id) VALUES (?, ?)",
                             [(user_id, t) for t in sorted(grants)])
            audit(g.db, g.user["username"], "user_updated",
                  detail=f"{user['username']}: role={role} all={all_tenants} disabled={disabled} "
                         f"tenants={sorted(grants)}")
        if request.form.get("password"):
            if problem := password_problem(request.form["password"]):
                flash(problem, "error")
            else:
                g.db.execute("UPDATE users SET password_hash = ?, session_version = session_version + 1,"
                             " failed_logins = 0, locked_until = NULL WHERE id = ?",
                             (generate_password_hash(request.form["password"]), user_id))
                audit(g.db, g.user["username"], "user_password_reset", detail=user["username"])
        flash("Saved.", "success")
        return redirect(url_for("admin.edit_user", user_id=user_id))
    granted = {r["tenant_id"] for r in g.db.execute(
        "SELECT tenant_id FROM user_tenants WHERE user_id = ?", (user_id,))}
    return render_template(
        "user_edit.html", user=user, tenants=tenants, granted=granted, roles=ROLES,
        sessions=sessions.list_for(user_id), locked=_is_locked(user),
        two_step_required=current_app.extensions["officesentry.config"].mfa_required(user["role"]),
    )


def _is_locked(user) -> bool:
    return bool(user["locked_until"]) and user["locked_until"] > utcnow()


@bp.post("/users/<int:user_id>/reset-two-step")
@roles_required("admin")
def reset_two_step(user_id: int):
    """For a lost phone: remove their authenticator and sign them out. They set it up again at next sign-in."""
    user = _user_or_404(user_id)
    with transaction(g.db):
        g.db.execute("UPDATE users SET totp_secret_enc = NULL, totp_pending_enc = NULL, totp_enabled_at = NULL,"
                     " totp_last_step = NULL, session_version = session_version + 1 WHERE id = ?", (user_id,))
        g.db.execute("DELETE FROM user_recovery_codes WHERE user_id = ?", (user_id,))
        g.db.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
        audit(g.db, g.user["username"], "user_two_step_reset", detail=user["username"])
    if user_id == g.user["id"]:
        return redirect(url_for("auth.login"))
    flash(f"Two-step sign-in reset for {user['username']}. They'll set it up again next time they sign in.",
          "success")
    return redirect(url_for("admin.edit_user", user_id=user_id))


@bp.post("/users/<int:user_id>/sign-out")
@roles_required("admin")
def sign_out_user(user_id: int):
    user = _user_or_404(user_id)
    ended = sessions.revoke_user(user_id, except_current=user_id == g.user["id"])
    audit(g.db, g.user["username"], "user_signed_out", detail=f"{user['username']}: {ended} session(s)")
    flash(f"Signed {user['username']} out of {ended} browser{'s' if ended != 1 else ''}.", "success")
    return redirect(url_for("admin.edit_user", user_id=user_id))


@bp.post("/users/<int:user_id>/unlock")
@roles_required("admin")
def unlock_user(user_id: int):
    user = _user_or_404(user_id)
    g.db.execute("UPDATE users SET failed_logins = 0, locked_until = NULL WHERE id = ?", (user_id,))
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


@bp.get("/connections")
@roles_required("admin")
def connections():
    return render_template(
        "connections.html", connections=store.list_connections(g.db), permissions=GRAPH_PERMISSIONS,
        exchange_permissions=EXCHANGE_PERMISSIONS,
        redirect_uri=current_app.extensions["officesentry.config"].base_url + url_for("tenants.consent_callback"),
    )


@bp.post("/connections")
@roles_required("admin")
def create_connection():
    name = request.form.get("name", "").strip() or "Office Sentry"
    cert = generate_certificate(f"Office Sentry - {name}"[:64])
    vault = current_app.extensions["officesentry.vault"]
    conn_id = store.create_connection(g.db, vault, name[:100], cert)
    audit(g.db, g.user["username"], "connection_created", detail=f"{name} {cert.thumbprint}")
    flash("Certificate generated. Upload the .cer to your app registration, then save its client ID.", "success")
    return redirect(url_for("admin.connections") + f"#conn-{conn_id}")


@bp.post("/connections/<int:conn_id>/client-id")
@roles_required("admin")
def set_client_id(conn_id: int):
    if store.get_connection(g.db, conn_id) is None:
        abort(404)
    client_id = request.form.get("client_id", "").strip().lower()
    home = request.form.get("home_tenant", "").strip().lower()
    if not GUID_RE.match(client_id):
        flash("The client ID is the app registration's Application (client) ID, a GUID.", "error")
    elif home and not GUID_RE.match(home):
        flash("The tenant ID is the Directory (tenant) ID on the app's overview page, a GUID.", "error")
    else:
        g.db.execute("UPDATE app_connections SET client_id = ? WHERE id = ?", (client_id, conn_id))
        audit(g.db, g.user["username"], "connection_client_id_set", detail=client_id)
        if not home:
            flash("Client ID saved.", "success")
        else:
            # Sign in to the tenant the app was created in, to prove the certificate is on the app.
            from ..connection_check import check

            connection = store.get_connection(g.db, conn_id)
            result = check(home, client_id, current_app.extensions["officesentry.vault"].decrypt(
                connection["key_enc"]), connection["thumbprint"])
            if result.ok:
                flash(f"Client ID saved and checked: {result.title} The certificate works.", "success")
            else:
                flash(f"Client ID saved, but the check failed. {result.title} {result.fix}", "error")
    return redirect(url_for("admin.connections"))


@bp.get("/connections/<int:conn_id>/certificate.cer")
@roles_required("admin")
def download_certificate(conn_id: int):
    """Public certificate only. The private key never leaves the database."""
    connection = store.get_connection(g.db, conn_id)
    if connection is None:
        abort(404)
    return Response(cert_der(connection["cert_pem"]), mimetype="application/pkix-cert",
                    headers={"Content-Disposition": f"attachment; filename=office-sentry-{conn_id}.cer"})
