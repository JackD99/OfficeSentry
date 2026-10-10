"""Sign-in (password, then two-step code), sign-out, first-run setup and the account page.

Sign-in has two stages. A correct password puts a short-lived "pending" marker
in the cookie; the browser is only signed in (a server-side session is created)
after the authenticator code, or straight away for accounts that don't need one.

Wrong passwords are limited by address (``throttle.py``), never by locking the
account, so nobody can lock a person out from elsewhere just by knowing their
username. Wrong two-step codes (which need the password first) lock the
account's two-step step for LOCKOUT after MAX_FAILURES.
"""

from __future__ import annotations

import hmac
import time
from datetime import UTC, datetime, timedelta

from flask import Blueprint, current_app, flash, g, make_response, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from .. import home, throttle, totp
from ..config import clear_setup_token, setup_token
from ..db import audit, audit_counted, transaction, utcnow
from ..text import plural
from . import sessions
from .access import login_required, safe_next
from .home import home_url

bp = Blueprint("auth", __name__)

MIN_PASSWORD = 12
MAX_FAILURES = 5  # wrong two-step codes (sign-in or enrolment) before LOCKOUT
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
    return bool(user["locked_until"] and datetime.fromisoformat(user["locked_until"].replace("Z", "+00:00")) > now)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


class Attempt:
    """One counted password or code attempt (see ``_reserve_attempt``)."""

    def __init__(self, user, number: int, locks_until: str | None):
        self.user, self.number, self.locks_until = user, number, locks_until

    def succeeded(self) -> None:
        """The password or code was right: lift the lock this attempt set in advance, if it set one."""
        if self.locks_until:
            g.db.execute(
                "UPDATE users SET locked_until = NULL WHERE id = ? AND locked_until = ?",
                (self.user["id"], self.locks_until),
            )

    def failed(self, action: str) -> bool:
        """Audit the wrong password or code. Returns True when this attempt locked the account."""
        audit(g.db, self.user["username"], action)
        if self.locks_until:
            audit(g.db, self.user["username"], "account_locked", detail=f"until {self.locks_until}")
        return self.locks_until is not None


def _reserve_attempt(user) -> Attempt | None:
    """Count a password or code attempt *before* checking it. None when the account is locked.

    One UPDATE both counts and, at the MAX_FAILURESth attempt, locks: parallel
    requests each get their own number, so no more than MAX_FAILURES are ever
    checked per lockout, however many arrive at once. The attempt that sets the
    lock lifts it again if it turns out to be right (``Attempt.succeeded``).
    """
    now = datetime.now(UTC)
    until = _iso(now + LOCKOUT)
    # An expired lock starts the count again. SQLite evaluates every SET expression on the old row.
    row = g.db.execute(
        "UPDATE users SET"
        " failed_logins = CASE WHEN locked_until IS NOT NULL THEN 1 ELSE failed_logins + 1 END,"
        " locked_until = CASE WHEN (CASE WHEN locked_until IS NOT NULL THEN 1 ELSE failed_logins + 1 END) >= ?"
        "   THEN ? ELSE NULL END"
        " WHERE id = ? AND (locked_until IS NULL OR locked_until <= ?)"
        " RETURNING failed_logins, locked_until",
        (MAX_FAILURES, until, user["id"], _iso(now)),
    ).fetchone()
    if row is None:
        return None
    return Attempt(user, row["failed_logins"], row["locked_until"])


def _use_totp(user, secret: str, value: str) -> bool:
    """Check an authenticator code and claim its time step, so the same code can't sign in twice.

    The claim is one conditional UPDATE: of two requests carrying the same code,
    only the first to write gets the step.
    """
    step = totp.verify(secret, value, user["totp_last_step"])
    return (
        step is not None
        and g.db.execute(
            "UPDATE users SET totp_last_step = ? WHERE id = ? AND (totp_last_step IS NULL OR totp_last_step < ?)",
            (step, user["id"], step),
        ).rowcount
        == 1
    )


# -- Pending sign-in (password accepted, two-step still to do) ----------------


def _set_pending(user, enroll: bool, next_url: str | None, change: bool = False) -> None:
    session.clear()
    session["pending"] = {
        "uid": user["id"],
        "sv": user["session_version"],
        "at": int(time.time()),
        "enroll": enroll,
        "change": change,
        "next": safe_next(next_url),
    }


def _pending_user(enroll: bool, change: bool = False):
    p = session.get("pending")
    if (
        not p
        or "uid" not in p
        or bool(p.get("enroll")) != enroll
        or bool(p.get("change")) != change
        or time.time() - p.get("at", 0) > PENDING_SECONDS
    ):
        return None
    user = _user(p["uid"])
    if user is None or user["disabled"] or user["session_version"] != p["sv"] or _locked(user):
        session.pop("pending", None)
        return None
    return user


def _finish_login(user, next_url: str | None = None) -> str:
    """Create the server-side session. Returns where to go next."""
    # Signing in at the front door ("/" or no page asked for) opens the person's home page (web/home.py).
    target = next_url or (session.get("pending") or {}).get("next")
    next_url = safe_next(target) if target and target != "/" else home_url(user)
    g.db.execute(
        "UPDATE users SET failed_logins = 0, locked_until = NULL, last_login_at = ? WHERE id = ?",
        (utcnow(), user["id"]),
    )
    sessions.start(user)
    audit(g.db, user["username"], "login")
    return next_url


def _continue_sign_in(user, next_url: str | None, code_done: bool = False) -> str:
    """Where sign-in goes once the password (and the two-step code, ``code_done``) were right.

    The two-step code comes first for accounts that have it, so an admin-set
    password alone can't be changed. Then a password an admin chose is replaced,
    before two-step is set up for accounts that don't have it yet.
    """
    if user["totp_enabled_at"] and not code_done:
        _set_pending(user, enroll=False, next_url=next_url)
        return url_for("auth.login_code")
    if user["must_change_password"]:
        _set_pending(user, enroll=False, next_url=next_url, change=True)
        return url_for("auth.new_password")
    if _needs_enrolment(user):
        _set_pending(user, enroll=True, next_url=next_url)
        return url_for("auth.two_step_setup")
    return _finish_login(user, next_url)


@bp.route("/login/new-password", methods=["GET", "POST"])
def new_password():
    """First sign-in with a password an admin chose (or reset): pick your own before going on."""
    user = _pending_user(enroll=False, change=True)
    if user is None:
        return redirect(url_for("auth.login"))
    if request.method == "POST":
        password = request.form.get("password", "")
        problem = password_problem(password, request.form.get("confirm", ""))
        if not problem and check_password_hash(user["password_hash"], password):
            problem = "Choose a new password, not the one you were given."
        if problem:
            flash(problem, "error")
        else:
            next_url = session["pending"].get("next")
            g.db.execute(
                "UPDATE users SET password_hash = ?, must_change_password = 0,"
                " session_version = session_version + 1 WHERE id = ?",
                (generate_password_hash(password), user["id"]),
            )
            audit(g.db, user["username"], "password_changed", detail="replaced the password an admin set")
            user = _user(user["id"])
            return redirect(_continue_sign_in(user, next_url, code_done=bool(user["totp_enabled_at"])))
    return render_template("new_password.html", username=user["username"], min_password=MIN_PASSWORD)


def _needs_enrolment(user) -> bool:
    return not user["totp_enabled_at"] and _cfg().mfa_required(user["role"], g.db)


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
    ip = client_ip()
    reserved = throttle.reserve(g.db, ip, username, user["id"] if user else None)
    if isinstance(reserved, throttle.Refused):
        audit_counted(g.db, "login_throttled", ip, user["username"] if user else None)
        return _throttled(username, reserved)
    # Unknown names still pay for a hash, so timing doesn't tell them apart.
    ok = check_password_hash(user["password_hash"] if user else _DUMMY_HASH, password)
    ok = ok and user is not None and not user["disabled"]
    throttle.finish(g.db, reserved, ok)

    if ok and _locked(user):
        # Right password, but two-step codes are locked: someone who has the password is guessing codes.
        audit(g.db, user["username"], "login_failed", detail="two-step codes locked")
    elif ok:
        return redirect(_continue_sign_in(user, request.args.get("next")))
    elif user is not None:
        audit(g.db, user["username"], "login_failed")
    else:
        # Not one row per stranger's guess, and never what they typed (it is sometimes a password).
        audit_counted(g.db, "login_failed_unknown", ip)
    flash(_SIGN_IN_FAILED, "error")
    return render_template("login.html", username=username), 200


_proxy_warning = {"given": False}


def client_ip() -> str:
    """The address sign-in limits are keyed on.

    ``request.remote_addr``, which ProxyFix sets from X-Forwarded-For only when
    OFFICESENTRY_TRUSTED_PROXIES says how many proxies to trust; a header sent
    straight to the portal is ignored.
    """
    if "X-Forwarded-For" in request.headers and not _cfg().trusted_proxies and not _proxy_warning["given"]:
        _proxy_warning["given"] = True
        current_app.logger.warning(
            "Requests carry X-Forwarded-For but OFFICESENTRY_TRUSTED_PROXIES is 0, so every visitor appears to come "
            "from the proxy's address and shares its sign-in limits. Set OFFICESENTRY_TRUSTED_PROXIES to the number "
            "of proxies in front of Office Sentry (1 for the bundled Caddy)."
        )
    return (request.remote_addr or "unknown")[:64]


def _throttled(username: str, refused: throttle.Refused):
    wait = (
        plural(-(-refused.retry_after // 60), "minute")
        if refused.retry_after > 90
        else plural(refused.retry_after, "second")
    )
    if refused.reason == "name":
        flash(
            f"This account has had a lot of failed sign-ins from other places, so new devices are slowed down. "
            f"Try again in {wait}.",
            "error",
        )
    else:
        flash(f"Too many failed sign-ins from your network. Try again in {wait}.", "error")
    resp = make_response(render_template("login.html", username=username), 429)
    resp.headers["Retry-After"] = str(refused.retry_after)
    return resp


@bp.route("/login/code", methods=["GET", "POST"])
def login_code():
    """Second step: a code from the authenticator app, or a recovery code."""
    user = _pending_user(enroll=False)
    if user is None:
        if request.method == "POST":
            flash("Your sign-in timed out. Please sign in again.", "warning")
        return redirect(url_for("auth.login"))

    if request.method == "POST":
        attempt = _reserve_attempt(user)
        if attempt is None:  # locked meanwhile, e.g. by parallel wrong codes
            session.pop("pending", None)
            flash(_SIGN_IN_FAILED, "error")
            return redirect(url_for("auth.login"))
        value = request.form.get("code", "")
        used_recovery = False
        ok = False
        if totp.looks_like_recovery_code(value):
            # Claimed in one statement, so two requests can't both use the same code.
            ok = used_recovery = (
                g.db.execute(
                    "UPDATE user_recovery_codes SET used_at = ?"
                    " WHERE user_id = ? AND code_hash = ? AND used_at IS NULL",
                    (utcnow(), user["id"], totp.hash_recovery_code(value)),
                ).rowcount
                > 0
            )
        else:
            ok = _use_totp(user, _vault().decrypt(user["totp_secret_enc"]), value)

        if ok:
            attempt.succeeded()
            next_url = _continue_sign_in(user, (session.get("pending") or {}).get("next"), code_done=True)
            if used_recovery:
                audit(g.db, user["username"], "recovery_code_used")
                left = g.db.execute(
                    "SELECT COUNT(*) FROM user_recovery_codes WHERE user_id = ? AND used_at IS NULL", (user["id"],)
                ).fetchone()[0]
                flash(
                    f"You used a recovery code; {left} left. If you've lost your phone, set up two-step "
                    "sign-in again from your account page.",
                    "warning",
                )
            return redirect(next_url)

        if attempt.failed("login_code_failed"):
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
    if user["totp_enabled_at"] or g.get("public_demo"):
        # The demo's shared visitor never sets up an authenticator: even the pending secret is a change.
        return redirect(url_for("auth.account"))
    vault = _vault()

    if request.method == "POST" and user["totp_pending_enc"] and signed_in and not confirm_password():
        # From the account page, adding an authenticator needs the password: a stolen session isn't enough.
        return _setup_page(user, signed_in)
    if request.method == "POST" and user["totp_pending_enc"]:
        attempt = None
        if not signed_in and (attempt := _reserve_attempt(user)) is None:
            session.pop("pending", None)
            flash(_SIGN_IN_FAILED, "error")
            return redirect(url_for("auth.login"))
        step = totp.verify(vault.decrypt(user["totp_pending_enc"]), request.form.get("code", ""))
        if step is not None:
            codes = totp.new_recovery_codes()
            with transaction(g.db):
                # Only one request turns it on, even if the same code arrives twice at once.
                enabled = (
                    g.db.execute(
                        "UPDATE users SET totp_secret_enc = totp_pending_enc, totp_pending_enc = NULL,"
                        " totp_enabled_at = ?, totp_last_step = ?, session_version = session_version + 1"
                        " WHERE id = ? AND totp_enabled_at IS NULL AND totp_pending_enc = ?",
                        (utcnow(), step, user["id"], user["totp_pending_enc"]),
                    ).rowcount
                    == 1
                )
                if enabled:
                    _store_recovery_codes(user["id"], codes)
                    audit(g.db, user["username"], "two_step_enabled")
            if not enabled:
                return redirect(url_for("auth.account" if signed_in else "auth.login"))
            if attempt:
                attempt.succeeded()
            user = _user(user["id"])
            if signed_in:
                sessions.keep_current(user["id"], user["session_version"])
                continue_url = url_for("auth.account")
            else:
                continue_url = _finish_login(user)
            return render_template("recovery_codes.html", codes=codes, continue_url=continue_url, first_time=True)
        if attempt and attempt.failed("two_step_setup_failed"):
            session.pop("pending", None)
            flash(_SIGN_IN_FAILED, "error")
            return redirect(url_for("auth.login"))
        flash(
            "That code didn't match. Check your phone's clock is set automatically, then try the newest code.", "error"
        )
    return _setup_page(user, signed_in)


def _setup_page(user, signed_in: bool):
    vault = _vault()
    if user["totp_pending_enc"]:
        secret = vault.decrypt(user["totp_pending_enc"])
    else:
        secret = totp.new_secret()
        g.db.execute("UPDATE users SET totp_pending_enc = ? WHERE id = ?", (vault.encrypt(secret), user["id"]))
    return render_template(
        "two_step_setup.html",
        signed_in=signed_in,
        username=user["username"],
        qr=totp.qr_svg(totp.provisioning_uri(secret, user["username"])),
        secret=totp.grouped(secret),
    )


def _store_recovery_codes(user_id: int, codes: list[str]) -> None:
    g.db.execute("DELETE FROM user_recovery_codes WHERE user_id = ?", (user_id,))
    g.db.executemany(
        "INSERT INTO user_recovery_codes (user_id, code_hash) VALUES (?, ?)",
        [(user_id, totp.hash_recovery_code(c)) for c in codes],
    )


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
    return render_template(
        "setup.html", need_token=False, min_password=MIN_PASSWORD, two_step=cfg.mfa_required("admin", g.db)
    )


# -- Account ------------------------------------------------------------------------


@bp.get("/account")
@login_required
def account():
    codes_left = g.db.execute(
        "SELECT COUNT(*) FROM user_recovery_codes WHERE user_id = ? AND used_at IS NULL", (g.user["id"],)
    ).fetchone()[0]
    return render_template(
        "account.html",
        min_password=MIN_PASSWORD,
        sessions=sessions.list_for(g.user["id"]),
        codes_left=codes_left,
        two_step_required=_cfg().mfa_required(g.user["role"], g.db),
        homes=home.HOMES,
        home_choice=home.home_of(g.user),
    )


@bp.route("/account/password", methods=["GET", "POST"])
@login_required
def change_password():
    if request.method == "GET":
        return redirect(url_for("auth.account"))
    # Wrong guesses count towards the lock, like any other password check on a signed-in session.
    if not confirm_password("current", "Current password is wrong."):
        return redirect(url_for("auth.account"))
    if problem := password_problem(request.form.get("password", ""), request.form.get("confirm", "")):
        flash(problem, "error")
        return redirect(url_for("auth.account"))
    g.db.execute(
        "UPDATE users SET password_hash = ?, session_version = session_version + 1 WHERE id = ?",
        (generate_password_hash(request.form["password"]), g.user["id"]),
    )
    audit(g.db, g.user["username"], "password_changed")
    sessions.keep_current(g.user["id"], g.user["session_version"] + 1)
    flash("Password changed. You were signed out everywhere else.", "success")
    return redirect(url_for("auth.account"))


def confirm_password(field: str = "password", wrong: str = "That password isn't right.") -> bool:
    """The signed-in person's password, asked again before a security change.

    Wrong answers count towards the two-step lock (MAX_FAILURES), so a stolen
    session can't be used to guess the password here.
    """
    attempt = _reserve_attempt(g.user)
    if attempt is None:
        flash("Too many wrong passwords or codes. Try again in 15 minutes.", "error")
        return False
    if check_password_hash(g.user["password_hash"], request.form.get(field, "")):
        attempt.succeeded()
        g.db.execute("UPDATE users SET failed_logins = 0 WHERE id = ?", (g.user["id"],))
        return True
    attempt.failed("password_confirm_failed")
    flash(wrong, "error")
    return False


@bp.post("/account/two-step/turn-off")
@login_required
def two_step_off():
    if _cfg().mfa_required(g.user["role"], g.db):
        flash("Two-step sign-in is required for your account, so it can't be turned off.", "error")
    elif confirm_password():
        with transaction(g.db):
            g.db.execute(
                "UPDATE users SET totp_secret_enc = NULL, totp_pending_enc = NULL, totp_enabled_at = NULL,"
                " totp_last_step = NULL, session_version = session_version + 1 WHERE id = ?",
                (g.user["id"],),
            )
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
    if not confirm_password():
        return redirect(url_for("auth.account"))
    codes = totp.new_recovery_codes()
    with transaction(g.db):
        _store_recovery_codes(g.user["id"], codes)
        audit(g.db, g.user["username"], "recovery_codes_replaced")
    return render_template("recovery_codes.html", codes=codes, continue_url=url_for("auth.account"), first_time=False)


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
    audit(g.db, g.user["username"], "sessions_ended", detail=plural(ended, "other session"))
    flash(
        f"Signed out of {ended} other browser{'s' if ended != 1 else ''}."
        if ended
        else "You weren't signed in anywhere else.",
        "success",
    )
    return redirect(url_for("auth.account"))
