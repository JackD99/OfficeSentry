"""
portal/routes/auth.py — Authentication routes (login, logout, initial setup, settings).
"""

import logging
from flask import (
    Blueprint, render_template, request, redirect, url_for,
    jsonify, session, flash,
)

from database import (
    user_count, create_user, get_user, check_password, update_password,
    list_users, delete_user, update_user,
    log_action,
)
from helpers import (
    login_required, admin_required, current_user,
    read_portal_config, write_portal_config, get_admin_client_id,
    validate_tenant_guid,
)

log = logging.getLogger("portal")

bp = Blueprint("auth", __name__)


@bp.route("/login", methods=["GET", "POST"])
def login():
    if user_count() == 0:
        return redirect(url_for("auth.initial_setup"))

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        user = get_user(username)
        if user and check_password(user["password_hash"], password):
            session.permanent = True
            session["username"] = username
            session["display_name"] = user["display_name"]
            log.info("Login: %s", username)
            log_action(username, "login")
            next_url = request.args.get("next", "")
            if not next_url or not next_url.startswith("/"):
                next_url = url_for("dashboard")
            return redirect(next_url)
        flash("Invalid username or password.", "danger")
    return render_template("login.html")


@bp.route("/logout")
def logout():
    username = session.get("username", "")
    session.clear()
    if username:
        log.info("Logout: %s", username)
    return redirect(url_for("auth.login"))


@bp.route("/initial-setup", methods=["GET", "POST"])
def initial_setup():
    if user_count() > 0:
        return redirect(url_for("auth.login"))
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        confirm  = request.form.get("confirm", "")
        if not username or not password:
            flash("Username and password are required.", "danger")
        elif password != confirm:
            flash("Passwords do not match.", "danger")
        elif len(password) < 8:
            flash("Password must be at least 8 characters.", "danger")
        else:
            create_user(username, password, display_name=username, is_admin=1)
            session.permanent = True
            session["username"] = username
            session["display_name"] = username
            log.info("Initial setup: created admin user %s", username)
            log_action(username, "initial_setup", details="First admin account created")
            flash("Admin account created. Welcome!", "success")
            return redirect(url_for("dashboard"))
    return render_template("initial_setup.html")


@bp.route("/settings")
@login_required
def settings_page():
    from database import list_api_keys
    api_keys = list_api_keys()
    return render_template("settings.html",
                           admin_client_id=get_admin_client_id(),
                           api_keys=api_keys)


@bp.route("/settings/admin-app", methods=["POST"])
@admin_required
def settings_admin_app():
    client_id = request.form.get("admin_client_id", "").strip()
    if client_id and not validate_tenant_guid(client_id):
        return jsonify({"ok": False, "error": "Admin Client ID must be a valid GUID."})
    cfg = read_portal_config()
    cfg["admin_client_id"] = client_id
    write_portal_config(cfg)
    log_action(current_user(), "admin_app_configured",
               details=f"Admin Client ID: {'set' if client_id else 'cleared'}")
    return jsonify({"ok": True, "message": "Admin app settings saved."})


@bp.route("/settings/password", methods=["POST"])
@login_required
def change_password():
    current  = request.form.get("current_password", "")
    new_pass = request.form.get("new_password", "")
    confirm  = request.form.get("confirm_password", "")

    user = get_user(current_user())
    if not user or not check_password(user["password_hash"], current):
        return jsonify({"ok": False, "error": "Current password is incorrect."})
    if new_pass != confirm:
        return jsonify({"ok": False, "error": "New passwords do not match."})
    if len(new_pass) < 8:
        return jsonify({"ok": False, "error": "Password must be at least 8 characters."})

    update_password(current_user(), new_pass)
    log_action(current_user(), "password_changed")
    return jsonify({"ok": True, "message": "Password updated."})


# ---------------------------------------------------------------------------
# User management (admin only)
# ---------------------------------------------------------------------------

@bp.route("/users")
@admin_required
def users_page():
    users = list_users()
    from database import list_tenant_groups
    from permissions import ROLE_HIERARCHY, ROLE_LABELS
    tenant_groups = list_tenant_groups()
    return render_template("users.html", users=users,
                           tenant_groups=tenant_groups,
                           roles=ROLE_HIERARCHY,
                           role_labels=ROLE_LABELS)


@bp.route("/users/new", methods=["POST"])
@admin_required
def user_create():
    username = request.form.get("username", "").strip()
    password = request.form.get("password", "")
    display  = request.form.get("display_name", "").strip()
    role     = request.form.get("role", "operator").strip()
    is_admin = 1 if request.form.get("is_admin") else 0

    # Map role to is_admin for backward compat
    from permissions import ROLE_HIERARCHY
    valid_roles = list(ROLE_HIERARCHY.keys())
    if role not in valid_roles:
        role = "operator"
    if role in ("super_admin", "admin"):
        is_admin = 1

    if not username or not password:
        return jsonify({"ok": False, "error": "Username and password are required."})
    if len(password) < 8:
        return jsonify({"ok": False, "error": "Password must be at least 8 characters."})
    if get_user(username):
        return jsonify({"ok": False, "error": f"User '{username}' already exists."})

    create_user(username, password, display_name=display or username, is_admin=is_admin)
    update_user(username, role=role)
    log_action(current_user(), "user_created", details=f"User: {username}, Role: {role}")
    return jsonify({"ok": True, "message": f"User '{username}' created."})


@bp.route("/users/<username>/toggle-admin", methods=["POST"])
@admin_required
def user_toggle_admin(username):
    """Legacy toggle — kept for backward compatibility."""
    user = get_user(username)
    if not user:
        return jsonify({"ok": False, "error": "User not found."}), 404
    if username == current_user():
        return jsonify({"ok": False, "error": "Cannot change your own admin status."})
    new_val = 0 if user["is_admin"] else 1
    new_role = "super_admin" if new_val else "operator"
    update_user(username, is_admin=new_val, role=new_role)
    log_action(current_user(), "user_role_changed",
               details=f"User: {username}, Role: {new_role}")
    return jsonify({"ok": True, "message": f"'{username}' is now {new_role}."})


@bp.route("/users/<username>/set-role", methods=["POST"])
@admin_required
def user_set_role(username):
    """Set a user's RBAC role."""
    user = get_user(username)
    if not user:
        return jsonify({"ok": False, "error": "User not found."}), 404
    if username == current_user():
        return jsonify({"ok": False, "error": "Cannot change your own role."})

    from permissions import ROLE_HIERARCHY
    role = request.form.get("role", "").strip()
    if role not in ROLE_HIERARCHY:
        return jsonify({"ok": False, "error": f"Invalid role: {role}"})

    is_admin = 1 if role in ("super_admin", "admin") else 0
    update_user(username, role=role, is_admin=is_admin)
    log_action(current_user(), "user_role_changed",
               details=f"User: {username}, Role: {role}")
    from permissions import ROLE_LABELS
    label = ROLE_LABELS.get(role, role)
    return jsonify({"ok": True, "message": f"'{username}' is now {label}."})


@bp.route("/users/<username>/set-groups", methods=["POST"])
@admin_required
def user_set_groups(username):
    """Set a user's allowed tenant groups."""
    import json as _json
    user = get_user(username)
    if not user:
        return jsonify({"ok": False, "error": "User not found."}), 404
    groups_raw = request.form.get("groups", "[]")
    try:
        groups = _json.loads(groups_raw)
        if not isinstance(groups, list):
            raise ValueError
    except (ValueError, _json.JSONDecodeError):
        return jsonify({"ok": False, "error": "Invalid groups format."})
    update_user(username, allowed_tenant_groups=_json.dumps(groups))
    log_action(current_user(), "user_groups_changed",
               details=f"User: {username}, Groups: {groups}")
    return jsonify({"ok": True, "message": f"Tenant groups updated for '{username}'."})


@bp.route("/users/<username>/reset-password", methods=["POST"])
@admin_required
def user_reset_password(username):
    user = get_user(username)
    if not user:
        return jsonify({"ok": False, "error": "User not found."}), 404
    new_pass = request.form.get("new_password", "")
    if len(new_pass) < 8:
        return jsonify({"ok": False, "error": "Password must be at least 8 characters."})
    update_password(username, new_pass)
    log_action(current_user(), "user_password_reset",
               details=f"User: {username}")
    return jsonify({"ok": True, "message": f"Password reset for '{username}'."})


@bp.route("/users/<username>/delete", methods=["POST"])
@admin_required
def user_delete(username):
    if username == current_user():
        return jsonify({"ok": False, "error": "Cannot delete your own account."})
    if not delete_user(username):
        return jsonify({"ok": False, "error": "User not found."}), 404
    log_action(current_user(), "user_deleted", details=f"User: {username}")
    return jsonify({"ok": True, "message": f"User '{username}' deleted."})


# ---------------------------------------------------------------------------
# API Key management (admin only)
# ---------------------------------------------------------------------------

@bp.route("/settings/api-keys/create", methods=["POST"])
@admin_required
def api_key_create():
    from database import create_api_key
    name = request.form.get("name", "").strip()
    role = request.form.get("role", "viewer").strip()
    if not name:
        return jsonify({"ok": False, "error": "Key name is required."})
    key_id, raw_key = create_api_key(
        name=name, role=role, created_by=current_user()
    )
    log_action(current_user(), "api_key_created", details=f"Key: {name}")
    return jsonify({"ok": True, "message": "API key created.", "key": raw_key, "id": key_id})


@bp.route("/settings/api-keys/<key_id>/delete", methods=["POST"])
@admin_required
def api_key_delete(key_id):
    from database import delete_api_key
    if not delete_api_key(key_id):
        return jsonify({"ok": False, "error": "Key not found."}), 404
    log_action(current_user(), "api_key_deleted", details=f"Key: {key_id}")
    return jsonify({"ok": True, "message": "API key deleted."})
