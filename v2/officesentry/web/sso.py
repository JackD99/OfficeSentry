"""Sign in with Microsoft (sso.py): the browser side for staff and clients' people, confirming a change with
Microsoft, the Account page's connect button, the admin's Microsoft sign-in settings page with its setup
wizards, and the page a client's administrator comes back to after approving the sign-in app."""

from __future__ import annotations

import json
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
from itsdangerous import BadSignature, URLSafeSerializer

from .. import app_setup, cert_lifecycle, clouds, entra, permissions, roles, sso, store
from ..certs import cert_der
from ..db import audit, audit_counted, transaction, utcnow
from . import public_demo, sessions
from .access import login_required, needs, safe_next, visible_tenants
from .admin import _out_of_reach, _user_or_404
from .auth import confirm_password
from .home import home_url

bp = Blueprint("sso", __name__)

FLOW_KEY = "microsoft"
# Set when Microsoft said the sign-in app only takes the MSP's own accounts: the sign-in page then offers
# staff the way in through their own tenant (``?as=staff``) until client sign-in is finished.
FALLBACK_KEY = "microsoft_staff_fallback"
GUID_RE = re.compile(r"^[0-9a-fA-F]{8}-([0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}$")
HINT_RE = re.compile(r"^[^@\s]{1,100}@[A-Za-z0-9.-]{1,100}$")


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
    """ "Sign in with Microsoft". With client sign-in on it goes to ``organizations`` and the token's tenant
    decides who it is; ``?as=staff`` keeps to the MSP's own tenant. ``?hint=`` (an invitation's link) fills
    in the address on Microsoft's page."""
    s = sso.settings(g.db)
    if not sso.shown(s):
        abort(404)
    if g.user is not None:
        return redirect(home_url(g.user))  # already signed in: the sign-in page would do the same
    clients = sso.clients_enabled(s) and request.args.get("as") != "staff"
    hint = request.args.get("hint", "").strip()
    return _begin(
        sso.Flow.new(next_url=request.args.get("next"), clients=clients),
        s,
        login_hint=hint if HINT_RE.match(hint) else None,
    )


@bp.get(sso.CALLBACK_PATH)
def callback():
    s = sso.settings(g.db)
    if (tenant := _approval_tenant(request.args.get("state"))) is not None:
        return _approval_returned(s, tenant)  # a client's administrator, back from approving the sign-in app
    flow = sso.Flow.from_dict(session.pop(FLOW_KEY, None))
    if not sso.is_configured(s):
        abort(404)
    linking = flow is not None and flow.link_user_id is not None
    confirming = flow is not None and flow.confirm_user_id is not None
    if linking:
        back = url_for("auth.account")
    elif confirming:
        back = safe_next(flow.next, url_for("auth.account"))
    else:
        back = url_for("auth.login")
    if flow is None or flow.expired or request.args.get("state", "") != flow.state:
        flash("That sign-in didn't start in this browser, or took too long. Please try again.", "error")
        return redirect(back)
    if request.args.get("error"):
        description = request.args.get("error_description")
        if "AADSTS50194" in (description or "") and sso.enabled(s):
            session[FALLBACK_KEY] = True
        flash(sso.problem(request.args.get("error"), description, clients=flow.clients), "error")
        return redirect(back)
    code = request.args.get("code", "")
    if not code:
        flash("Microsoft didn't send a sign-in code back. Please try again.", "error")
        return redirect(back)
    if not (linking or confirming) and not sso.shown(s):
        abort(404)
    if linking and (g.user is None or g.user["id"] != flow.link_user_id):
        flash("Sign in first, then connect your Microsoft account from your account page.", "error")
        return redirect(url_for("auth.login"))
    if confirming and (g.user is None or g.user["id"] != flow.confirm_user_id):
        flash("Your session ended meanwhile. Sign in again, then make the change.", "error")
        return redirect(url_for("auth.login"))

    try:
        try:
            key_pem = _vault().decrypt(s["sso_key_enc"])
        except InvalidToken as exc:  # the keys were rotated without re-encrypting the settings
            raise sso.SignInError(
                "Office Sentry can't read the key of its sign-in certificate. An admin should make a new"
                " certificate under Settings → Microsoft sign-in, or run rotate-keys again."
            ) from exc
        identity = sso.verify_id_token(s, sso.exchange_code(s, key_pem, code, _redirect_uri(), flow), flow)
    except sso.SignInError as exc:
        audit(g.db, g.user["username"] if g.user else None, "login_failed", detail=f"Microsoft: {exc}"[:300])
        flash(str(exc), "error")
        return redirect(back)
    session.pop(FALLBACK_KEY, None)

    if confirming:
        return _confirmed(identity, back)
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

    if identity.tid != s["sso_tenant"].lower():
        return _client_signed_in(identity, s, flow)
    if not sso.enabled(s):
        audit(g.db, identity.username, "login_failed", detail="Microsoft: staff sign-in with Microsoft is off")
        flash("Microsoft sign-in is off for staff here. Sign in with your password.", "error")
        return redirect(url_for("auth.login"))
    try:
        with transaction(g.db):
            user, outcome = sso.account_for(g.db, identity, s)
            if outcome == "created":
                audit(
                    g.db,
                    user["username"],
                    "user_created",
                    detail=f"{user['username']} ({roles.role_of(g.db, user).name}), Microsoft sign-in",
                )
            elif outcome == "linked":
                audit(g.db, user["username"], "microsoft_connected", detail=f"{identity.upn} (matched the username)")
            elif outcome == "role_changed":
                audit(
                    g.db, user["username"], "user_updated", detail=f"role from Entra: {roles.role_of(g.db, user).name}"
                )
    except sso.Refused as exc:
        audit(g.db, identity.username, "login_failed", detail=f"Microsoft: {exc}"[:300])
        flash(str(exc), "error")
        return redirect(url_for("auth.login"))
    return _signed_in(user, flow)


def _signed_in(user, flow: sso.Flow):
    g.db.execute(
        "UPDATE users SET failed_logins = 0, locked_until = NULL, last_login_at = ? WHERE id = ?",
        (utcnow(), user["id"]),
    )
    sessions.start(user, method="microsoft")
    audit(g.db, user["username"], "login", detail="Microsoft")
    target = flow.next
    return redirect(safe_next(target) if target and target != "/" else home_url(user))


def _client_signed_in(identity: sso.Identity, s: dict, flow: sso.Flow):
    """Someone from a client's own tenant (only ever through ``organizations``): their client account, or
    nothing. The refusal never says whether an account by that name exists."""
    try:
        with transaction(g.db):
            user, _ = sso.client_account_for(g.db, identity, s)
    except sso.Refused as exc:
        if str(exc) in (sso.NOT_SET_UP, sso.OTHER_ORGANISATION):
            # Anyone with a work account anywhere gets this far: counted per address, without their name.
            audit_counted(g.db, "login_failed_unknown", (request.remote_addr or "")[:64], detail="Microsoft")
            if sso.enabled(s):
                session[FALLBACK_KEY] = True  # staff with a guest account go in through the MSP's tenant
        else:
            audit(g.db, identity.username, "login_failed", detail=f"Microsoft, tenant {identity.tid}: {exc}"[:300])
        flash(str(exc), "error")
        return redirect(url_for("auth.login"))
    return _signed_in(user, flow)


# -- Confirming a change with Microsoft (accounts with no password) ------------------------------------------


@bp.post("/account/microsoft/confirm")
@login_required
def confirm():
    """Sign in to Microsoft again (``prompt=login``) to confirm a security change, where someone with a
    password would type it. Comes back to ``?next=``, the page the change is made on."""
    s = sso.settings(g.db)
    if (
        not sso.is_configured(s)
        or not g.user["entra_oid"]
        or g.user["role"] == "client"
        or sso.has_password(g.user)
        or g.get("public_demo")
    ):
        abort(404)
    back = safe_next(request.args.get("next"), url_for("auth.account"))
    flow = sso.Flow.new(next_url=back, confirm_user_id=g.user["id"])
    return _begin(flow, s, login_hint=g.user["entra_upn"] or None, prompt="login")


def _confirmed(identity: sso.Identity, back: str):
    if identity.oid != (g.user["entra_oid"] or "").lower():
        audit(g.db, g.user["username"], "confirm_failed", detail=f"Microsoft: signed in as {identity.upn}"[:300])
        flash(
            f"That was {identity.upn or 'another Microsoft account'}, not the account you sign in with here. "
            "Nothing was confirmed.",
            "error",
        )
        return redirect(back)
    sessions.confirm()
    audit(g.db, g.user["username"], "confirmed_with_microsoft")
    flash(f"Confirmed. Make the change within {sso.CONFIRM_MINUTES} minutes.", "success")
    return redirect(back)


# -- A client's administrator approving the sign-in app for their organisation ----------------------------


def _approval_serializer() -> URLSafeSerializer:
    return URLSafeSerializer(current_app.config["SECRET_KEY"], salt="client-sign-in-approval")


def approval_link(s: dict, t) -> str | None:
    """The link to send a client's Microsoft 365 administrator so they approve the sign-in app for their
    organisation, or None until client sign-in is ready and the client's Directory ID is known. The state
    names the tenant and when it was added, as the consent link's does, so it never points at a later client
    that reuses the number."""
    tid = sso.directory_id(g.db, t)
    if not sso.clients_enabled(s) or tid is None or clouds.of(t).key != sso.cloud(s).key:
        return None
    state = _approval_serializer().dumps({"t": t["id"], "c": t["created_at"]})
    return sso.approval_url(s, tid, _redirect_uri(), state)


def _approval_tenant(state: str | None):
    if not state or "." not in state:
        return None
    try:
        data = _approval_serializer().loads(state)
    except BadSignature:
        return None
    if not isinstance(data, dict) or not isinstance(data.get("t"), int) or not isinstance(data.get("c"), str):
        return None
    tenant = store.get_tenant(g.db, data["t"])
    return tenant if tenant is not None and tenant["created_at"] == data["c"] else None


def _approval_returned(s: dict, t):
    """Public: a client's administrator back from Microsoft's approval page, with no Office Sentry sign-in.
    Notes the approval when Microsoft says it was given for this client's own tenant; shows none of
    Microsoft's text, only a known explanation."""
    error = request.args.get("error")
    returned = request.args.get("tenant", "").lower()
    approved = not error and request.args.get("admin_consent", "").lower() == "true"
    mine = approved and returned == (sso.directory_id(g.db, t) or "")
    audit_counted(
        g.db,
        "client_sign_in_approved" if mine else "client_sign_in_not_approved",
        (request.remote_addr or "")[:64],
    )
    if mine and not t["archived_at"] and not public_demo.active(g.db):
        with transaction(g.db):
            g.db.execute(
                "UPDATE tenants SET sign_in_approved_at = COALESCE(sign_in_approved_at, ?) WHERE id = ?",
                (utcnow(), t["id"]),
            )
            audit(g.db, None, "client_sign_in_approved", t["id"], f"tenant {returned}")
    problem = sso.known_problem(error, request.args.get("error_description"), clients=True) if error else None
    return render_template(
        "client_sign_in_approved.html",
        approved=mine,
        other_tenant=approved and not mine,
        problem=problem,
    )


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
@needs("users.manage")
def admin_disconnect(user_id: int):
    user = _user_or_404(user_id)
    if problem := _out_of_reach(user):
        flash(problem, "error")
    elif sso.unlink(g.db, user_id):
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


# -- Settings → Microsoft sign-in -------------------------------------------------------------------------


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
    update = app_setup.current(g.db, g.user["id"], "sign_in_update")
    if update and update["status"] == "done" and not sso.multi_tenant(s):
        app_setup.cancel(g.db, g.user["id"], "sign_in_update")
        update = None
    linked, staff = sso.staff_counts(g.db)
    expiry = cert_lifecycle.sign_in_expiry(s)
    return render_template(
        "sign_in_settings.html",
        s=s,
        configured=sso.is_configured(s),
        enabled=sso.enabled(s),
        multi_tenant=sso.multi_tenant(s),
        clients_enabled=sso.clients_enabled(s),
        client_rows=_client_rows(s) if sso.is_configured(s) else [],
        update=update,
        update_seconds_left=app_setup.seconds_left(update) if update else 0,
        sign_in=sign_in,
        seconds_left=app_setup.seconds_left(sign_in) if sign_in else 0,
        redirect_uri=_redirect_uri(),
        default_app_name=sso.DEFAULT_NAME,
        setup_permissions=sso.SETUP_PERMISSIONS,
        app_roles=_app_roles(),
        custom_roles=_custom_roles(s),
        default_roles=sso.DEFAULT_ROLES,
        linked=linked,
        staff=staff,
        expiry=expiry,
        admin_centre=sso.admin_centre_url(s) if sso.is_configured(s) else clouds.get(clouds.DEFAULT).admin_centre,
        microsoft_session=g.session_row["method"] == "microsoft",
        me_linked=bool(g.user["entra_oid"]),
        notes=[n for n in (s.get("sso_notes") or "").split("\n") if n],
    )


def _client_rows(s: dict) -> list[dict]:
    """Each client the admin sees, for the Clients card: who there can sign in, and whether their own
    administrator has approved the sign-in app."""
    rows = []
    for t in visible_tenants(g.db, g.user):
        people = sso.client_people(g.db, t["id"])
        rows.append(
            {
                "tenant": t,
                "people": len(people),
                "microsoft": sum(1 for u in people if sso.signs_in_with_microsoft(u)),
                "approved": t["sign_in_approved_at"],
                "known": sso.directory_id(g.db, t) is not None,
                "same_cloud": clouds.of(t).key == sso.cloud(s).key,
            }
        )
    return rows


def _app_roles() -> list[tuple[str, str]]:
    """(app role value, the Office Sentry role it gives, by its name here) for the sign-in app's own roles."""
    names = {r.key: r.name for r in roles.all_roles(g.db) if r.key}
    return [(value, names.get(key, permissions.TEMPLATE_BY_KEY[key].name)) for value, key in sso.APP_ROLES.items()]


def _custom_roles(s: dict) -> list[tuple[roles.Role, str]]:
    """Every custom staff role, with the app role value that reaches it from Entra ('' for none)."""
    value_of = {role_id: value for value, role_id in sso.custom_values(s).items()}
    return [
        (r, value_of.get(r.id, "")) for r in roles.all_roles(g.db) if not r.built_in and r.kind == permissions.STAFF
    ]


@bp.post("/admin/sign-in/roles")
@needs("settings.manage")
def save_role_values():
    """The app role value in Entra that gives each custom role, one form field per role (``value-<id>``)."""
    s = sso.settings(g.db)
    taken = {v.lower() for v in sso.APP_ROLES}
    values: dict[str, int] = {}
    for role, _old in _custom_roles(s):
        value = request.form.get(f"value-{role.id}", "").strip()
        if not value:
            continue
        if not sso.VALUE_RE.match(value):
            flash(
                f"“{value}” can't be an app role value: letters, digits and . _ : - only, no spaces, up to 60.",
                "error",
            )
            return redirect(url_for("sso.settings_page") + "#roles")
        if value.lower() in taken:
            flash(f"{value} is already an app role of the sign-in app, or given to another role.", "error")
            return redirect(url_for("sso.settings_page") + "#roles")
        taken.add(value.lower())
        values[value] = role.id
    if values != sso.custom_values(s):
        with transaction(g.db):
            sso.save(g.db, {"sso_role_values": json.dumps(values, sort_keys=True) if values else ""})
            names = {r.id: r.name for r, _v in _custom_roles(s)}
            audit(
                g.db,
                g.user["username"],
                "staff_sign_in_changed",
                detail="roles from Entra: "
                + (", ".join(f"{v}={names[i]}" for v, i in sorted(values.items())) or "none"),
            )
    flash("Saved.", "success")
    return redirect(url_for("sso.settings_page") + "#roles")


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


# Client sign-in: the switch, and the sign-in app accepting other organisations' accounts.


def _to_clients():
    return redirect(url_for("sso.settings_page") + "#clients")


@bp.post("/admin/sign-in/clients")
@needs("settings.manage")
def save_clients():
    s = sso.settings(g.db)
    if not sso.is_configured(s):
        return _page()
    on = request.form.get("clients") == "1"
    if on and not sso.multi_tenant(s):
        flash("First let the sign-in app accept your clients' accounts (step 1).", "error")
        return _to_clients()
    if on != (s.get("sso_clients") == "1"):
        with transaction(g.db):
            sso.save(g.db, {"sso_clients": "1" if on else ""})
            audit(g.db, g.user["username"], "staff_sign_in_changed", detail=f"clients={'on' if on else 'off'}")
    flash(
        "Client sign-in is on. Add each client's people on the client's Settings → Client sign-in page."
        if on
        else "Client sign-in is off. Client accounts sign in with a password.",
        "success",
    )
    return _to_clients()


@bp.post("/admin/sign-in/clients/by-hand")
@needs("settings.manage")
def clients_by_hand():
    """The admin changed the app's supported account types in Entra themselves."""
    s = sso.settings(g.db)
    if not sso.is_configured(s):
        return _page()
    with transaction(g.db):
        sso.save(g.db, {"sso_multi_tenant": "1", "sso_multi_tenant_by": f"{g.user['username']} (by hand)"})
        audit(
            g.db,
            g.user["username"],
            "sign_in_app_updated",
            detail=f"{s['sso_client_id']}: accepts other organisations (by hand)",
        )
    flash("Noted. Now turn client sign-in on (step 2).", "success")
    return _to_clients()


@bp.post("/admin/sign-in/clients/setup")
@needs("settings.manage")
def start_update():
    s = sso.settings(g.db)
    if not sso.is_configured(s):
        return _page()
    try:
        app_setup.start(
            g.db,
            _vault(),
            g.user["id"],
            g.user["username"],
            s["sso_tenant"],
            sso.cloud(s).key,
            s.get("sso_app_name") or sso.DEFAULT_NAME,
            purpose="sign_in_update",
        )
    except entra.SignInError as exc:
        flash(str(exc), "error")
    return _to_clients()


@bp.post("/admin/sign-in/clients/setup/check")
@needs("settings.manage")
def check_update():
    row = app_setup.check(g.db, _vault(), g.user["id"], g.user["username"], _redirect_uri(), purpose="sign_in_update")
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
    return _to_clients()


@bp.post("/admin/sign-in/clients/setup/cancel")
@needs("settings.manage")
def cancel_update():
    app_setup.cancel(g.db, g.user["id"], "sign_in_update")
    return _to_clients()


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
        app_setup.cancel(g.db, g.user["id"], "sign_in_update")
        audit(g.db, g.user["username"], "staff_sign_in_removed")
    flash(
        "Microsoft sign-in is removed. Accounts stay connected to their Microsoft accounts in case you set it up "
        "again; an account that had no password needs one set on its user page. Delete the app registration in "
        "Entra if you won't set it up again.",
        "success",
    )
    return _page()
