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
    log_action,
)
from helpers import login_required, current_user

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
            next_url = request.args.get("next", url_for("dashboard"))
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
    return render_template("settings.html")


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
