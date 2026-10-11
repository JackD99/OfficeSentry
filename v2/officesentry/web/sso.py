"""Sign in with Microsoft (sso.py): the browser side, the Account page's connect button and the admin's
Staff sign-in settings page with its setup wizard."""

from __future__ import annotations

import re

from cryptography.fernet import InvalidToken
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
    session,
    url_for,
)

from .. import app_setup, cert_lifecycle, clouds, entra, sso
from ..certs import cert_der
from ..db import audit, transaction, utcnow
from . import sessions
from .access import login_required, needs, safe_next
from .auth import confirm_password
from .home import home_url

bp = Blueprint("sso", __name__)

FLOW_KEY = "microsoft"
GUID_RE = re.compile(r"^[0-9a-fA-F]{8}-([0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}$")


def _cfg():
    return current_app.extensions["officesentry.config"]


def _vault():
    return current_app.extensions["officesentry.vault"]


def _redirect_uri() -> str:
    return sso.redirect_uri(_cfg().base_url)


def _begin(flow: sso.Flow, s: dict, login_hint: str | None = None, prompt: str | None = None):
    session[FLOW_KEY] = flow.as_dict()
    return redirect(sso.authorize_url(s, _redirect_uri(), flow, login_hint=login_hint, prompt=prompt))


# -- Signing in --------------------------------------------------------------------------------------------


@bp.get("/login/microsoft")
def start():
    s = sso.settings(g.db)
    if not sso.enabled(s):
        abort(404)
    if g.user is not None:
        return redirect(home_url(g.user))  # already signed in: the sign-in page would do the same
    return _begin(sso.Flow.new(next_url=request.args.get("next")), s)


@bp.get(sso.CALLBACK_PATH)
def callback():
    s = sso.settings(g.db)
    flow = sso.Flow.from_dict(session.pop(FLOW_KEY, None))
    if not sso.is_configured(s):
        abort(404)
    linking = flow is not None and flow.link_user_id is not None
    back = url_for("auth.account") if linking else url_for("auth.login")
    if flow is None or flow.expired or request.args.get("state", "") != flow.state:
        flash("That sign-in didn't start in this browser, or took too long. Please try again.", "error")
        return redirect(back)
    if request.args.get("error"):
        flash(sso.problem(request.args.get("error"), request.args.get("error_description")), "error")
        return redirect(back)
    code = request.args.get("code", "")
    if not code:
        flash("Microsoft didn't send a sign-in code back. Please try again.", "error")
        return redirect(back)
    if not linking and not sso.enabled(s):
        abort(404)
    if linking and (g.user is None or g.user["id"] != flow.link_user_id):
        flash("Sign in first, then connect your Microsoft account from your account page.", "error")
        return redirect(url_for("auth.login"))

    try:
        try:
            key_pem = _vault().decrypt(s["sso_key_enc"])
        except InvalidToken as exc:  # the keys were rotated without re-encrypting the settings
            raise sso.SignInError(
                "Office Sentry can't read the key of its sign-in certificate. An admin should make a new"
                " certificate under Settings → Staff sign-in, or run rotate-keys again."
            ) from exc
        identity = sso.verify_id_token(s, sso.exchange_code(s, key_pem, code, _redirect_uri(), flow), flow)
    except sso.SignInError as exc:
        audit(g.db, g.user["username"] if g.user else None, "login_failed", detail=f"Microsoft: {exc}"[:300])
        flash(str(exc), "error")
        return redirect(back)

    if linking:
        try:
            with transaction(g.db):
                sso.link(g.db, g.user["id"], identity)
                audit(g.db, g.user["username"], "microsoft_connected", detail=identity.upn)
        except sso.Refused as exc:
            flash(str(exc), "error")
            return redirect(back)
        role = sso.role_for(identity, s)
        if role is None:
            flash(
                f"Connected to {identity.upn}. It has no Office Sentry role in Entra yet, so signing in with it won't "
                "work until an admin assigns you one; your password still does.",
                "warning",
            )
        else:
            flash(f"Connected to {identity.upn}. You can sign in with Microsoft from now on.", "success")
        return redirect(back)

    try:
        with transaction(g.db):
            user, outcome = sso.account_for(g.db, identity, s)
            if outcome == "created":
                audit(
                    g.db,
                    user["username"],
                    "user_created",
                    detail=f"{user['username']} ({user['role']}), Microsoft sign-in",
                )
            elif outcome == "linked":
                audit(g.db, user["username"], "microsoft_connected", detail=f"{identity.upn} (matched the username)")
            elif outcome == "role_changed":
                audit(g.db, user["username"], "user_updated", detail=f"role from Entra: {user['role']}")
    except sso.Refused as exc:
        audit(g.db, identity.username, "login_failed", detail=f"Microsoft: {exc}"[:300])
        flash(str(exc), "error")
        return redirect(url_for("auth.login"))
    g.db.execute(
        "UPDATE users SET failed_logins = 0, locked_until = NULL, last_login_at = ? WHERE id = ?",
        (utcnow(), user["id"]),
    )
    sessions.start(user, method="microsoft")
    audit(g.db, user["username"], "login", detail="Microsoft")
    target = flow.next
    return redirect(safe_next(target) if target and target != "/" else home_url(user))


# -- Account page ----------------------------------------------------------------------------------------


@bp.post("/account/microsoft/connect")
@login_required
def connect():
    s = sso.settings(g.db)
    if not sso.enabled(s) or g.user["role"] == "client" or g.get("public_demo"):
        abort(404)
    if not confirm_password():  # a stolen session can't attach the thief's Microsoft account
        return redirect(url_for("auth.account") + "#microsoft")
    hint = g.user["username"] if "@" in g.user["username"] else None
    return _begin(sso.Flow.new(link_user_id=g.user["id"]), s, login_hint=hint, prompt="select_account")


@bp.post("/account/microsoft/disconnect")
@login_required
def disconnect():
    if not g.user["entra_oid"]:
        return redirect(url_for("auth.account"))
    if not confirm_password():
        return redirect(url_for("auth.account") + "#microsoft")
    if sso.unlink(g.db, g.user["id"]):
        audit(g.db, g.user["username"], "microsoft_disconnected")
        flash("Your Microsoft account is disconnected. You sign in with your password from now on.", "success")
    else:
        flash(
            "This account has no password, so it can't be disconnected from Microsoft: you'd have no way to sign in.",
            "error",
        )
    return redirect(url_for("auth.account"))


@bp.post("/admin/users/<int:user_id>/microsoft/disconnect")
@needs("settings.manage")
def admin_disconnect(user_id: int):
    user = g.db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if user is None:
        abort(404)
    if sso.unlink(g.db, user_id):
        g.db.execute("UPDATE users SET session_version = session_version + 1 WHERE id = ?", (user_id,))
        audit(g.db, g.user["username"], "microsoft_disconnected", detail=user["username"])
        flash(f"{user['username']} is disconnected from Microsoft and signs in with a password.", "success")
    else:
        flash(
            f"{user['username']} has no password, so can't be disconnected. Set a password on this page first, "
            "or delete the account.",
            "error",
        )
    return redirect(url_for("admin.edit_user", user_id=user_id))


# -- Settings → Staff sign-in -------------------------------------------------------------------------


def _page():
    return redirect(url_for("sso.settings_page"))


@bp.get("/admin/sign-in")
@needs("settings.manage")
def settings_page():
    s = sso.settings(g.db)
    sign_in = app_setup.current(g.db, g.user["id"], "sign_in")
    if sign_in and sign_in["status"] == "done" and not sso.is_configured(s):
        app_setup.cancel(g.db, g.user["id"], "sign_in")  # the app it made was removed since
        sign_in = None
    linked, staff = sso.staff_counts(g.db)
    expiry = cert_lifecycle.sign_in_expiry(s)
    return render_template(
        "sign_in_settings.html",
        s=s,
        configured=sso.is_configured(s),
        enabled=sso.enabled(s),
        sign_in=sign_in,
        seconds_left=app_setup.seconds_left(sign_in) if sign_in else 0,
        redirect_uri=_redirect_uri(),
        default_app_name=sso.DEFAULT_NAME,
        setup_permissions=sso.SETUP_PERMISSIONS,
        roles=sso.APP_ROLE_DEFINITIONS,
        default_roles=sso.DEFAULT_ROLES,
        linked=linked,
        staff=staff,
        expiry=expiry,
        admin_centre=sso.admin_centre_url(s) if sso.is_configured(s) else clouds.get(clouds.DEFAULT).admin_centre,
        microsoft_session=g.session_row["method"] == "microsoft",
        me_linked=bool(g.user["entra_oid"]),
        notes=[n for n in (s.get("sso_notes") or "").split("\n") if n],
    )


@bp.post("/admin/sign-in")
@needs("settings.manage")
def save_settings():
    s = sso.settings(g.db)
    if not sso.is_configured(s):
        return _page()
    enabled = request.form.get("enabled") == "1"
    passwords = "off" if request.form.get("passwords") == "off" else "on"
    default_role = request.form.get("default_role", "")
    if default_role not in sso.DEFAULT_ROLES:
        abort(400)
    if passwords == "off" and s.get("sso_passwords", "on") != "off" and g.session_row["method"] != "microsoft":
        flash(
            "To turn passwords off for staff, first sign in with Microsoft yourself, so you can't lock yourself out.",
            "error",
        )
        passwords = "on"
    if not enabled:
        passwords = "on"  # with Microsoft sign-in off, passwords are the only way in
    changes = {"sso_enabled": "1" if enabled else "", "sso_passwords": passwords, "sso_default_role": default_role}
    before = {k: s.get(k) or ("on" if k == "sso_passwords" else "") for k in changes}
    with transaction(g.db):
        sso.save(g.db, changes)
        if changes != before:
            audit(
                g.db,
                g.user["username"],
                "staff_sign_in_changed",
                detail=f"microsoft={'on' if enabled else 'off'} passwords={passwords} "
                f"default_role={default_role or 'none'}",
            )
    flash("Saved.", "success")
    return _page()


# The setup wizard, the sign-in app's version (app_setup.py with purpose "sign_in").


def _to_setup():
    return redirect(url_for("sso.settings_page") + "#setup")


@bp.post("/admin/sign-in/setup")
@needs("settings.manage")
def start_setup():
    cloud = request.form.get("cloud", clouds.DEFAULT)
    if not clouds.valid(cloud):
        abort(400)
    tenant = app_setup.tenant_ref(request.form.get("tenant", ""))
    name = request.form.get("name", "").strip()[:100] or sso.DEFAULT_NAME
    if tenant is None:
        flash(
            "Enter your tenant's domain (like contoso.onmicrosoft.com) or its Directory (tenant) ID, or leave it "
            "blank.",
            "error",
        )
    elif sso.is_configured(sso.settings(g.db)):
        flash("The sign-in app is already set up. Remove it first to create a new one.", "error")
    else:
        try:
            app_setup.start(g.db, _vault(), g.user["id"], g.user["username"], tenant, cloud, name, purpose="sign_in")
        except entra.SignInError as exc:
            flash(str(exc), "error")
    return _to_setup()


@bp.post("/admin/sign-in/setup/check")
@needs("settings.manage")
def check_setup():
    row = app_setup.check(g.db, _vault(), g.user["id"], g.user["username"], _redirect_uri(), purpose="sign_in")
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


@bp.post("/admin/sign-in/setup/cancel")
@needs("settings.manage")
def cancel_setup():
    app_setup.cancel(g.db, g.user["id"], "sign_in")
    return _page()


# By hand: the admin made the app in Entra and pastes its IDs; the certificate is made here first.


@bp.post("/admin/sign-in/manual")
@needs("settings.manage")
def save_manual():
    s = sso.settings(g.db)
    tenant = request.form.get("tenant", "").strip().lower()
    client_id = request.form.get("client_id", "").strip().lower()
    cloud = request.form.get("cloud", s.get("sso_cloud") or clouds.DEFAULT)
    name = request.form.get("name", "").strip()[:100]
    if not clouds.valid(cloud):
        abort(400)
    if not GUID_RE.match(tenant):
        flash("The tenant ID is the Directory (tenant) ID on the app's overview page, a GUID.", "error")
        return _page()
    if not GUID_RE.match(client_id):
        flash("The client ID is the app registration's Application (client) ID, a GUID.", "error")
        return _page()
    with transaction(g.db):
        if not s.get("sso_key_enc"):
            sso.new_certificate(g.db, _vault())
        sso.store_manual(g.db, _vault(), tenant, client_id, cloud, name, g.user["username"])
        audit(g.db, g.user["username"], "sign_in_app_saved", detail=f"{client_id} in {tenant} ({cloud}), by hand")
    flash(
        "Saved. Upload the certificate to the app registration, then turn Microsoft sign-in on and try it.", "success"
    )
    return _page()


@bp.post("/admin/sign-in/certificate")
@needs("settings.manage")
def make_certificate():
    """The first certificate (for the hand-made path), or a replacement to switch to later."""
    s = sso.settings(g.db)
    replace = bool(s.get("sso_key_enc"))
    cert = sso.new_certificate(g.db, _vault(), replace=replace)
    audit(
        g.db,
        g.user["username"],
        "sign_in_certificate_made",
        detail=cert.thumbprint + (" (replacement)" if replace else ""),
    )
    flash(
        "New certificate made. Upload its .cer to the sign-in app in Entra (Certificates & secrets), then switch to it."
        if replace
        else "Certificate made. Download the .cer and upload it to the app registration (Certificates & secrets).",
        "success",
    )
    return _page()


@bp.post("/admin/sign-in/certificate/switch")
@needs("settings.manage")
def switch_certificate():
    if sso.switch_certificate(g.db):
        audit(g.db, g.user["username"], "sign_in_certificate_switched")
        flash(
            "Switched. Sign-ins use the new certificate from now on; you can remove the old one from Entra.", "success"
        )
    return _page()


@bp.post("/admin/sign-in/certificate/cancel")
@needs("settings.manage")
def cancel_certificate():
    sso.save(
        g.db, {"sso_next_cert_pem": "", "sso_next_key_enc": "", "sso_next_thumbprint": "", "sso_next_not_after": ""}
    )
    flash("Replacement cancelled. If you uploaded its .cer to Entra, delete it there too.", "success")
    return _page()


@bp.get("/admin/sign-in/certificate.cer")
@needs("settings.manage")
def download_certificate():
    """The public certificate only (``?which=next`` for the replacement). The private key never leaves."""
    s = sso.settings(g.db)
    pem = s.get("sso_next_cert_pem") if request.args.get("which") == "next" else s.get("sso_cert_pem")
    if not pem:
        abort(404)
    name = "office-sentry-sign-in" + ("-new" if request.args.get("which") == "next" else "")
    return Response(
        cert_der(pem),
        mimetype="application/pkix-cert",
        headers={"Content-Disposition": f"attachment; filename={name}.cer"},
    )


@bp.post("/admin/sign-in/remove")
@needs("settings.manage")
def remove():
    with transaction(g.db):
        sso.forget(g.db)
        app_setup.cancel(g.db, g.user["id"], "sign_in")
        audit(g.db, g.user["username"], "staff_sign_in_removed")
    flash(
        "Microsoft sign-in is removed. Accounts stay connected to their Microsoft accounts in case you set it up "
        "again; an account that had no password needs one set on its user page. Delete the app registration in "
        "Entra if you won't set it up again.",
        "success",
    )
    return _page()
