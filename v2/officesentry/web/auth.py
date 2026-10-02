"""Login, logout, first-run setup and password change."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from flask import Blueprint, flash, g, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from ..db import audit, utcnow
from .access import login_required, safe_next

bp = Blueprint("auth", __name__)

MIN_PASSWORD = 12
MAX_FAILURES = 5
LOCKOUT = timedelta(minutes=15)
# Compared against when the username doesn't exist, so timing doesn't reveal it.
_DUMMY_HASH = generate_password_hash("not-a-real-password")


def password_problem(password: str, confirm: str | None = None) -> str | None:
    if len(password) < MIN_PASSWORD:
        return f"Password must be at least {MIN_PASSWORD} characters."
    if confirm is not None and password != confirm:
        return "Passwords don't match."
    return None


def _has_users() -> bool:
    return g.db.execute("SELECT 1 FROM users LIMIT 1").fetchone() is not None


@bp.route("/login", methods=["GET", "POST"])
def login():
    if not _has_users():
        return redirect(url_for("auth.setup"))
    if g.user is not None:
        return redirect(safe_next(request.args.get("next")))

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        user = g.db.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        now = datetime.now(UTC)
        locked = (user is not None and user["locked_until"]
                  and datetime.fromisoformat(user["locked_until"].replace("Z", "+00:00")) > now)
        ok = check_password_hash(user["password_hash"] if user else _DUMMY_HASH, password)

        if user and ok and not locked and not user["disabled"]:
            g.db.execute("UPDATE users SET failed_logins = 0, locked_until = NULL, last_login_at = ?"
                         " WHERE id = ?", (utcnow(), user["id"]))
            audit(g.db, user["username"], "login")
            session.clear()
            session.permanent = True
            session["uid"] = user["id"]
            session["sv"] = user["session_version"]
            return redirect(safe_next(request.args.get("next")))

        if user and not locked:
            failures = user["failed_logins"] + 1
            until = (now + LOCKOUT).strftime("%Y-%m-%dT%H:%M:%SZ") if failures >= MAX_FAILURES else None
            g.db.execute("UPDATE users SET failed_logins = ?, locked_until = ? WHERE id = ?",
                         (0 if until else failures, until, user["id"]))
        audit(g.db, username[:64], "login_failed")
        flash("Invalid username or password, or the account is temporarily locked.", "error")
    return render_template("login.html")


@bp.post("/logout")
def logout():
    session.clear()
    return redirect(url_for("auth.login"))


@bp.route("/setup", methods=["GET", "POST"])
def setup():
    """Create the first admin. Only available while there are no users."""
    if _has_users():
        return redirect(url_for("auth.login"))
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        problem = password_problem(password, request.form.get("confirm", ""))
        if not username:
            problem = "Enter a username."
        if problem:
            flash(problem, "error")
        else:
            g.db.execute(
                "INSERT INTO users (username, password_hash, role, all_tenants, created_at)"
                " VALUES (?, ?, 'admin', 1, ?)",
                (username, generate_password_hash(password), utcnow()),
            )
            audit(g.db, username, "setup_first_admin")
            flash("Admin account created. Sign in to continue.", "success")
            return redirect(url_for("auth.login"))
    return render_template("setup.html", min_password=MIN_PASSWORD)


@bp.route("/account/password", methods=["GET", "POST"])
@login_required
def change_password():
    if request.method == "POST":
        if not check_password_hash(g.user["password_hash"], request.form.get("current", "")):
            flash("Current password is wrong.", "error")
        elif problem := password_problem(request.form.get("password", ""), request.form.get("confirm", "")):
            flash(problem, "error")
        else:
            g.db.execute(
                "UPDATE users SET password_hash = ?, session_version = session_version + 1 WHERE id = ?",
                (generate_password_hash(request.form["password"]), g.user["id"]),
            )
            audit(g.db, g.user["username"], "password_changed")
            session["sv"] = g.user["session_version"] + 1
            flash("Password changed. Other sessions were signed out.", "success")
            return redirect(url_for("tenants.dashboard"))
    return render_template("change_password.html", min_password=MIN_PASSWORD)
