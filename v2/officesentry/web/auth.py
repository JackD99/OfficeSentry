"""Sign-in (password, then two-step code), sign-out, first-run setup and the account page.

Sign-in has two stages. A correct password puts a short-lived "pending" marker
in the cookie; the browser is only signed in (a server-side session is created)
after the authenticator code, or straight away for accounts that don't need one.
Wrong passwords and wrong codes count towards the same per-account lockout.
"""

from __future__ import annotations

import hmac
import time
from datetime import UTC, datetime, timedelta

from flask import Blueprint, current_app, flash, g, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from .. import totp
from ..config import clear_setup_token, setup_token
from ..db import audit, transaction, utcnow
from . import sessions
from .access import login_required, safe_next

bp = Blueprint("auth", __name__)

MIN_PASSWORD = 12
MAX_FAILURES = 5
LOCKOUT = timedelta(minutes=15)
PENDING_SECONDS = 10 * 60
# Compared against when the username doesn't exist, so timing doesn't reveal it.
_DUMMY_HASH = generate_password_hash("not-a-real-password")
_SIGN_IN_FAILED = "Invalid username or password, or the account is temporarily locked."


def password_problem(password: str, confirm: str | None = None) -> str | None:
    if len(password) < MIN_PASSWORD:
        return f"Password must be at least {MIN_PASSWORD} characters."
    if confirm is not None and password != confirm:
        return "Passwords don't match."
    return None


def _cfg():
    return current_app.extensions["officesentry.config"]


def _vault():
    return current_app.extensions["officesentry.vault"]


def _has_users() -> bool:
    return g.db.execute("SELECT 1 FROM users LIMIT 1").fetchone() is not None


def _user(user_id: int):
    return g.db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()


def _locked(user, now: datetime | None = None) -> bool:
    now = now or datetime.now(UTC)
    return bool(user["locked_until"]
                and datetime.fromisoformat(user["locked_until"].replace("Z", "+00:00")) > now)


def _record_failure(user, action: str) -> bool:
    """Count a wrong password or code. Returns True when this locks the account."""
    failures = user["failed_logins"] + 1
    until = None
    if failures >= MAX_FAILURES:
        until = (datetime.now(UTC) + LOCKOUT).strftime("%Y-%m-%dT%H:%M:%SZ")
    g.db.execute("UPDATE users SET failed_logins = ?, locked_until = ? WHERE id = ?",
                 (0 if until else failures, until, user["id"]))
    audit(g.db, user["username"], action)
    if until:
        audit(g.db, user["username"], "account_locked", detail=f"until {until}")
    return until is not None


# -- Pending sign-in (password accepted, two-step still to do) ----------------


def _set_pending(user, enroll: bool, next_url: str | None) -> None:
    session.clear()
    session["pending"] = {"uid": user["id"], "sv": user["session_version"], "at": int(time.time()),
                          "enroll": enroll, "next": safe_next(next_url)}


def _pending_user(enroll: bool):
    p = session.get("pending")
    if not p or "uid" not in p or bool(p.get("enroll")) != enroll or time.time() - p.get("at", 0) > PENDING_SECONDS:
        return None
    user = _user(p["uid"])
    if user is None or user["disabled"] or user["session_version"] != p["sv"] or _locked(user):
        session.pop("pending", None)
        return None
    return user


def _finish_login(user, next_url: str | None = None) -> str:
    """Create the server-side session. Returns where to go next."""
    next_url = safe_next(next_url or (session.get("pending") or {}).get("next"))
    g.db.execute("UPDATE users SET failed_logins = 0, locked_until = NULL, last_login_at = ? WHERE id = ?",
                 (utcnow(), user["id"]))
    sessions.start(user)
    audit(g.db, user["username"], "login")
    return next_url


def _needs_enrolment(user) -> bool:
    return not user["totp_enabled_at"] and _cfg().mfa_required(user["role"])


@bp.route("/login", methods=["GET", "POST"])
def login():
    if not _has_users():
        return redirect(url_for("auth.setup"))
    if g.user is not None:
        return redirect(safe_next(request.args.get("next")))

    if request.method == "GET":
        session.pop("pending", None)  # coming back to the sign-in page starts over
        return render_template("login.html")

    username = request.form.get("username", "").strip()
    password = request.form.get("password", "")
    user = g.db.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
    locked = user is not None and _locked(user)
    ok = check_password_hash(user["password_hash"] if user else _DUMMY_HASH, password)

    if user and ok and not locked and not user["disabled"]:
        next_url = request.args.get("next")
        if user["totp_enabled_at"]:
            _set_pending(user, enroll=False, next_url=next_url)
            return redirect(url_for("auth.login_code"))
        if _needs_enrolment(user):
            _set_pending(user, enroll=True, next_url=next_url)
            return redirect(url_for("auth.two_step_setup"))
        return redirect(_finish_login(user, next_url))

    if user and not locked:
        _record_failure(user, "login_failed")
    else:
        audit(g.db, username[:64], "login_failed")
    flash(_SIGN_IN_FAILED, "error")
    return render_template("login.html", username=username), 200


@bp.route("/login/code", methods=["GET", "POST"])
def login_code():
    """Second step: a code from the authenticator app, or a recovery code."""
    user = _pending_user(enroll=False)
    if user is None:
        if request.method == "POST":
            flash("Your sign-in timed out. Please sign in again.", "warning")
        return redirect(url_for("auth.login"))

    if request.method == "POST":
        value = request.form.get("code", "")
        used_recovery = False
        ok = False
        if totp.looks_like_recovery_code(value):
            row = g.db.execute(
                "SELECT id FROM user_recovery_codes WHERE user_id = ? AND code_hash = ? AND used_at IS NULL",
                (user["id"], totp.hash_recovery_code(value)),
            ).fetchone()
            if row is not None:
                g.db.execute("UPDATE user_recovery_codes SET used_at = ? WHERE id = ?", (utcnow(), row["id"]))
                ok = used_recovery = True
        else:
            step = totp.verify(_vault().decrypt(user["totp_secret_enc"]), value, user["totp_last_step"])
            if step is not None:
                g.db.execute("UPDATE users SET totp_last_step = ? WHERE id = ?", (step, user["id"]))
                ok = True

        if ok:
            next_url = _finish_login(user)
            if used_recovery:
                audit(g.db, user["username"], "recovery_code_used")
                left = g.db.execute("SELECT COUNT(*) FROM user_recovery_codes WHERE user_id = ? AND used_at IS NULL",
                                    (user["id"],)).fetchone()[0]
                flash(f"You used a recovery code; {left} left. If you've lost your phone, set up two-step "
                      "sign-in again from your account page.", "warning")
            return redirect(next_url)

        if _record_failure(user, "login_code_failed"):
            session.pop("pending", None)
            flash(_SIGN_IN_FAILED, "error")
            return redirect(url_for("auth.login"))
        flash("That code didn't work. Use the newest code from your app.", "error")
    return render_template("login_code.html", username=user["username"])


@bp.route("/two-step/setup", methods=["GET", "POST"])
def two_step_setup():
    """Add an authenticator app. Used during sign-in (when it's required) and from the account page."""
    signed_in = g.user is not None
    user = g.user if signed_in else _pending_user(enroll=True)
    if user is None:
        return redirect(url_for("auth.login"))
    if user["totp_enabled_at"]:
        return redirect(url_for("auth.account"))
    vault = _vault()

    if request.method == "POST" and user["totp_pending_enc"]:
        secret = vault.decrypt(user["totp_pending_enc"])
        step = totp.verify(secret, request.form.get("code", ""))
        if step is not None:
            codes = totp.new_recovery_codes()
            with transaction(g.db):
                g.db.execute(
                    "UPDATE users SET totp_secret_enc = totp_pending_enc, totp_pending_enc = NULL,"
                    " totp_enabled_at = ?, totp_last_step = ?, session_version = session_version + 1"
                    " WHERE id = ?", (utcnow(), step, user["id"]))
                _store_recovery_codes(user["id"], codes)
                audit(g.db, user["username"], "two_step_enabled")
            user = _user(user["id"])
            if signed_in:
                sessions.keep_current(user["id"], user["session_version"])
                continue_url = url_for("auth.account")
            else:
                continue_url = _finish_login(user)
            return render_template("recovery_codes.html", codes=codes, continue_url=continue_url, first_time=True)
        if not signed_in and _record_failure(user, "two_step_setup_failed"):
            session.pop("pending", None)
            flash(_SIGN_IN_FAILED, "error")
            return redirect(url_for("auth.login"))
        flash("That code didn't match. Check your phone's clock is set automatically, then try the newest code.",
              "error")

    if user["totp_pending_enc"]:
        secret = vault.decrypt(user["totp_pending_enc"])
    else:
        secret = totp.new_secret()
        g.db.execute("UPDATE users SET totp_pending_enc = ? WHERE id = ?", (vault.encrypt(secret), user["id"]))
    return render_template(
        "two_step_setup.html", signed_in=signed_in, username=user["username"],
        qr=totp.qr_svg(totp.provisioning_uri(secret, user["username"])), secret=totp.grouped(secret),
    )


def _store_recovery_codes(user_id: int, codes: list[str]) -> None:
    g.db.execute("DELETE FROM user_recovery_codes WHERE user_id = ?", (user_id,))
    g.db.executemany("INSERT INTO user_recovery_codes (user_id, code_hash) VALUES (?, ?)",
                     [(user_id, totp.hash_recovery_code(c)) for c in codes])


@bp.post("/logout")
def logout():
    if g.user is not None:
        audit(g.db, g.user["username"], "logout")
    sessions.end()
    return redirect(url_for("auth.login"))


# -- First-run setup -------------------------------------------------------------


@bp.route("/setup", methods=["GET", "POST"])
def setup():
    """Create the first admin. Needs the one-time setup link printed when the portal starts."""
    if _has_users():
        return redirect(url_for("auth.login"))
    cfg = _cfg()
    expected = setup_token(cfg.data_dir)

    supplied = request.values.get("token", "").strip()
    if supplied:
        if hmac.compare_digest(supplied.encode(), expected.encode()):
            session["setup_token"] = expected
            return redirect(url_for("auth.setup"))  # keep the token out of the address bar and history
        flash("That setup code isn't right. Copy the newest link from the portal's log.", "error")
    if not hmac.compare_digest(session.get("setup_token", "").encode(), expected.encode()):
        return render_template("setup.html", need_token=True)

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        problem = password_problem(password, request.form.get("confirm", ""))
        if not username or len(username) > 64:
            problem = "Enter a username (up to 64 characters)."
        if problem:
            flash(problem, "error")
        else:
            cur = g.db.execute(
                "INSERT INTO users (username, password_hash, role, all_tenants, created_at)"
                " VALUES (?, ?, 'admin', 1, ?)",
                (username, generate_password_hash(password), utcnow()),
            )
            clear_setup_token(cfg.data_dir)
            audit(g.db, username, "setup_first_admin")
            user = _user(cur.lastrowid)
            if _needs_enrolment(user):
                _set_pending(user, enroll=True, next_url=None)
                return redirect(url_for("auth.two_step_setup"))
            return redirect(_finish_login(user))
    return render_template("setup.html", need_token=False, min_password=MIN_PASSWORD,
                           two_step=cfg.mfa_required("admin"))


# -- Account ------------------------------------------------------------------------


@bp.get("/account")
@login_required
def account():
    codes_left = g.db.execute("SELECT COUNT(*) FROM user_recovery_codes WHERE user_id = ? AND used_at IS NULL",
                              (g.user["id"],)).fetchone()[0]
    return render_template(
        "account.html", min_password=MIN_PASSWORD, sessions=sessions.list_for(g.user["id"]),
        codes_left=codes_left, two_step_required=_cfg().mfa_required(g.user["role"]),
    )


@bp.route("/account/password", methods=["GET", "POST"])
@login_required
def change_password():
    if request.method == "GET":
        return redirect(url_for("auth.account"))
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
        sessions.keep_current(g.user["id"], g.user["session_version"] + 1)
        flash("Password changed. You were signed out everywhere else.", "success")
    return redirect(url_for("auth.account"))


def _confirm_password() -> bool:
    if check_password_hash(g.user["password_hash"], request.form.get("password", "")):
        return True
    flash("That password isn't right.", "error")
    return False


@bp.post("/account/two-step/turn-off")
@login_required
def two_step_off():
    if _cfg().mfa_required(g.user["role"]):
        flash("Two-step sign-in is required for your account, so it can't be turned off.", "error")
    elif _confirm_password():
        with transaction(g.db):
            g.db.execute("UPDATE users SET totp_secret_enc = NULL, totp_pending_enc = NULL, totp_enabled_at = NULL,"
                         " totp_last_step = NULL, session_version = session_version + 1 WHERE id = ?",
                         (g.user["id"],))
            g.db.execute("DELETE FROM user_recovery_codes WHERE user_id = ?", (g.user["id"],))
            audit(g.db, g.user["username"], "two_step_disabled")
        sessions.keep_current(g.user["id"], g.user["session_version"] + 1)
        flash("Two-step sign-in is off.", "success")
    return redirect(url_for("auth.account"))


@bp.post("/account/two-step/recovery-codes")
@login_required
def new_recovery_codes():
    if not g.user["totp_enabled_at"]:
        return redirect(url_for("auth.account"))
    if not _confirm_password():
        return redirect(url_for("auth.account"))
    codes = totp.new_recovery_codes()
    with transaction(g.db):
        _store_recovery_codes(g.user["id"], codes)
        audit(g.db, g.user["username"], "recovery_codes_replaced")
    return render_template("recovery_codes.html", codes=codes, continue_url=url_for("auth.account"),
                           first_time=False)


@bp.post("/account/sessions/<int:session_id>/end")
@login_required
def end_session(session_id: int):
    if session_id == g.session_row["id"]:
        sessions.end()
        return redirect(url_for("auth.login"))
    if sessions.revoke(session_id, g.user["id"]):
        audit(g.db, g.user["username"], "session_ended")
        flash("That browser was signed out.", "success")
    return redirect(url_for("auth.account"))


@bp.post("/account/sessions/end-others")
@login_required
def end_other_sessions():
    ended = sessions.revoke_user(g.user["id"], except_current=True)
    audit(g.db, g.user["username"], "sessions_ended", detail=f"{ended} other session(s)")
    flash(f"Signed out of {ended} other browser{'s' if ended != 1 else ''}." if ended
          else "You weren't signed in anywhere else.", "success")
    return redirect(url_for("auth.account"))
