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

from .. import (
    app_setup,
    cert_lifecycle,
    clouds,
    entra,
    permission_batch,
    permissions,
    roles,
    store,
    tenant_groups,
    throttle,
)
from ..certs import cert_der, generate_certificate
from ..db import audit, transaction, utcnow
from ..text import plural
from . import sessions
from .access import access_of, current, needs, tenant_ids, unseen_client_accounts, visible_tenants
from .auth import confirm_password, password_problem

bp = Blueprint("admin", __name__, url_prefix="/admin")

GUID_RE = re.compile(r"^[0-9a-fA-F]{8}-([0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}$")


def _user_or_404(user_id: int):
    """The account, unless it's missing or a client account of clients the signed-in person doesn't see."""
    user = g.db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if user is None or user_id in unseen_client_accounts(g.db, g.user):
        abort(404)
    return user


def _admin_password_ok() -> bool:
    """The admin's own password, asked again before resetting someone's password, two-step sign-in or
    sessions, as the account page asks before the same changes: a stolen admin session isn't enough.
    Too many wrong answers sign the admin's browser out (``auth.confirm_password``)."""
    return confirm_password("admin_password", "Your password isn't right, so nothing was changed.")


# -- Users -------------------------------------------------------------------


def _role_from_form(default=None) -> roles.Role:
    """The role the form picked: a role id, or (from older forms and tests) one of users.role's values,
    which means the built-in role that stands for it."""
    raw = request.form.get("role", "")
    if raw.isdigit():
        role = roles.get(g.db, int(raw))
    elif raw in permissions.UPGRADE_ROLE:
        role = roles.by_key(g.db, permissions.UPGRADE_ROLE[raw])
    else:
        role = default
    if role is None:
        abort(400)
    return role


def _last_admin(user_id: int) -> bool:
    """Is this the only enabled administrator? There must always be one."""
    row = g.db.execute(
        "SELECT COUNT(*) FROM users WHERE role = 'admin' AND disabled = 0 AND id <> ?", (user_id,)
    ).fetchone()
    return row[0] == 0


# Someone who manages users but isn't an Administrator (a custom role, or users.manage added for them)
# can only hand out what they hold themselves and only on the clients they see, can't change an account
# that holds more than they do, and can't change their own role or access. So nobody raises their own
# access, directly or by resetting the password of someone who has more, and nobody sees the names of
# clients outside their own scope on these pages.


def _out_of_reach(user) -> str | None:
    """Why the signed-in person may not change this account, or None."""
    actor = current()
    if actor.is_admin:
        return None
    target = access_of(user)
    if target.is_admin:
        return "Only an Administrator can change an Administrator's account."
    mine, theirs = tenant_ids(g.db, g.user), tenant_ids(g.db, user)
    wider = mine is not None and (theirs is None or not theirs <= mine)
    if wider or target.anywhere() - actor.everywhere():
        return f"{user['username']} can do or see more than you can, so only an Administrator can change their account."
    return None


def _own_access(user) -> str | None:
    """A non-Administrator can't change their own role or access."""
    if user["id"] == g.user["id"] and not current().is_admin:
        return "Ask an Administrator to change your own role or access."
    return None


def _givable(role: roles.Role) -> bool:
    """May the signed-in person give someone this role? An Administrator any; anyone else only a role that
    holds no more than they do."""
    actor = current()
    if actor.is_admin:
        return True
    if role.kind == permissions.ADMIN or role.capabilities - actor.everywhere():
        return False
    return role.scope != permissions.ALL_CLIENTS or role.kind != permissions.STAFF or actor.sees_all


_NOT_GIVABLE = "You can only give a role that holds no more than you do. Ask an Administrator for this one."
_NOT_GRANTABLE = "You can only add capabilities you hold yourself. Ask an Administrator for this one."


def _manageable_tenants():
    """The clients listed on the user pages: every one for whoever sees every client (archived ones too, so
    saving keeps grants for when they're restored), otherwise only the signed-in person's own."""
    if current().sees_all:
        return g.db.execute("SELECT id, name, archived_at FROM tenants ORDER BY name COLLATE NOCASE").fetchall()
    return visible_tenants(g.db, g.user, archived=True)


@bp.get("/users")
@needs("users.manage")
def users():
    unseen = unseen_client_accounts(g.db, g.user)
    rows = [u for u in g.db.execute("SELECT * FROM users ORDER BY username COLLATE NOCASE") if u["id"] not in unseen]
    cfg = current_app.extensions["officesentry.config"]
    two_step = {
        "policy": cfg.mfa_policy(g.db),
        "from_env": cfg.require_mfa_explicit,
        "clients_without": sum(
            1 for u in rows if u["role"] == "client" and not u["totp_enabled_at"] and not u["disabled"]
        ),
    }
    group_grants = dict(g.db.execute("SELECT user_id, COUNT(*) FROM user_tenant_groups GROUP BY user_id").fetchall())
    overrides = dict(g.db.execute("SELECT user_id, COUNT(*) FROM user_access GROUP BY user_id").fetchall())
    all_roles = roles.all_roles(g.db)
    return render_template(
        "users.html",
        users=rows,
        role_of={u["id"]: roles.role_of(g.db, u) for u in rows},
        roles=all_roles,
        givable={r.id for r in all_roles if _givable(r)},
        two_step=two_step,
        group_grants=group_grants,
        overrides=overrides,
        scope_of=lambda u: roles.access_for(g.db, u).scope,
    )


@bp.post("/users")
@needs("users.manage")
def create_user():
    username = request.form.get("username", "").strip()
    password = request.form.get("password", "")
    problem = password_problem(password, username=username)
    role = _role_from_form()
    if not _givable(role):
        problem = _NOT_GIVABLE
    if not username or len(username) > 64:
        problem = "Enter a username (up to 64 characters)."
    elif g.db.execute("SELECT 1 FROM users WHERE username = ?", (username,)).fetchone():
        problem = "That username is taken."
    if problem:
        flash(problem, "error")
        return redirect(url_for("admin.users"))
    cur = g.db.execute(
        # The admin chose this password, so they replace it at first sign-in.
        "INSERT INTO users (username, password_hash, role, role_id, created_at, must_change_password)"
        " VALUES (?, ?, ?, ?, ?, 1)",
        (username, generate_password_hash(password), role.column, role.id, utcnow()),
    )
    audit(g.db, g.user["username"], "user_created", detail=f"{username} ({role.name})")
    return redirect(url_for("admin.edit_user", user_id=cur.lastrowid))


# Someone who signs in with Microsoft gets their role from Entra at each sign-in (sso.account_for).
_SET_IN_ENTRA = (
    "{username} signs in with Microsoft, so their role is the app role assigned to them in Entra. Change it "
    "there; Settings → Staff sign-in says which app role gives which role."
)


@bp.route("/users/<int:user_id>", methods=["GET", "POST"])
@needs("users.manage")
def edit_user(user_id: int):
    user = _user_or_404(user_id)
    tenants = _manageable_tenants()
    current_role = roles.role_of(g.db, user)
    actor = current()
    if request.method == "POST":
        if problem := _out_of_reach(user) or _own_access(user):
            flash(problem, "error")
            return redirect(url_for("admin.edit_user", user_id=user_id))
        if request.form.get("password") and not _admin_password_ok():
            return redirect(url_for("admin.edit_user", user_id=user_id))
        role = _role_from_form(current_role)
        if user["entra_oid"] and role.id != current_role.id:
            flash(_SET_IN_ENTRA.format(username=user["username"]), "error")
            return redirect(url_for("admin.edit_user", user_id=user_id))
        if role.id != current_role.id and not _givable(role):
            flash(_NOT_GIVABLE, "error")
            return redirect(url_for("admin.edit_user", user_id=user_id))
        if user["id"] == g.user["id"] and role.kind != permissions.ADMIN:
            flash("You can't remove your own Administrator role.", "error")
            return redirect(url_for("admin.edit_user", user_id=user_id))
        disabled = int(request.form.get("disabled") == "1" and user["id"] != g.user["id"])
        if (
            current_role.kind == permissions.ADMIN
            and (role.kind != permissions.ADMIN or disabled)
            and _last_admin(user_id)
        ):
            flash("This is the only Administrator. Make someone else an Administrator first.", "error")
            return redirect(url_for("admin.edit_user", user_id=user_id))
        staff = role.kind == permissions.STAFF
        all_tenants = int(staff and actor.sees_all and request.form.get("all_tenants") == "1")
        valid_ids = {t["id"] for t in tenants}
        grants = {int(x) for x in request.form.getlist("tenants") if x.isdigit()} & valid_ids
        # Grants on clients the signed-in person doesn't see stay as they were.
        before = {r[0] for r in g.db.execute("SELECT tenant_id FROM user_tenants WHERE user_id = ?", (user_id,))}
        grants |= before - valid_ids
        # Only staff can be granted a whole group (access.visible_tenants ignores it for clients). A group can
        # hold clients outside the signed-in person's scope, so only someone who sees every client grants them.
        if actor.sees_all:
            valid_groups = {gr["id"] for gr in tenant_groups.all_groups(g.db)}
            group_grants = {int(x) for x in request.form.getlist("groups") if x.isdigit()} & valid_groups
        else:
            group_grants = set(tenant_groups.user_groups(g.db, user_id))
        group_grants = group_grants if staff else set()
        changed_access = disabled != user["disabled"] or all_tenants != user["all_tenants"]
        with transaction(g.db):
            role_changed = roles.assign(g.db, user_id, role)
            g.db.execute(
                "UPDATE users SET all_tenants = ?, disabled = ?, session_version = session_version + ? WHERE id = ?",
                (all_tenants, disabled, int(changed_access), user_id),
            )
            g.db.execute("DELETE FROM user_tenants WHERE user_id = ?", (user_id,))
            g.db.executemany(
                "INSERT INTO user_tenants (user_id, tenant_id) VALUES (?, ?)", [(user_id, t) for t in sorted(grants)]
            )
            tenant_groups.set_user_groups(g.db, user_id, group_grants)
            if role.kind != permissions.STAFF:
                # Overrides a client role may not hold (anything but reports) go with the staff role.
                roles.set_overrides(g.db, g.user["username"], user, roles.overrides(g.db, user_id))
            audit(
                g.db,
                g.user["username"],
                "user_updated",
                detail=f"{user['username']}: role={role.name}{' (changed)' if role_changed else ''} all={all_tenants}"
                f" disabled={disabled} tenants={sorted(grants)}"
                + (f" groups={sorted(group_grants)}" if group_grants else ""),
            )
        if request.form.get("password"):
            if problem := password_problem(request.form["password"], username=user["username"]):
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
    all_roles = roles.all_roles(g.db)
    return render_template(
        "user_edit.html",
        user=user,
        role=current_role,
        access=roles.access_for(g.db, user),
        tenants=tenants,
        granted=granted,
        roles=all_roles,
        givable={r.id for r in all_roles if _givable(r)},
        actor=actor,
        out_of_reach=_out_of_reach(user),
        own_access=_own_access(user),
        grantable=actor.everywhere(),
        capabilities=permissions.CAPABILITIES,
        groups_of_capabilities=permissions.GROUPS,
        overrides=roles.overrides(g.db, user_id),
        allowed_capabilities=permissions.allowed_for(current_role.kind, permissions.KEYS),
        capability_titles={c.key: c.title for c in permissions.CAPABILITIES},
        tenant_names={t["id"]: t["name"] for t in tenants},
        groups=tenant_groups.all_groups(g.db) if actor.sees_all else [],
        granted_groups=tenant_groups.user_groups(g.db, user_id),
        sessions=sessions.list_for(user_id),
        locked=_is_locked(user),
        failures=throttle.recent_failures(g.db, user["username"]),
        two_step_required=current_app.extensions["officesentry.config"].mfa_required(user["role"], g.db),
        last_admin=current_role.kind == permissions.ADMIN and _last_admin(user_id),
    )


@bp.post("/users/<int:user_id>/access")
@needs("users.manage")
def user_access(user_id: int):
    """Single capabilities added or taken away for this person: everywhere (one choice per capability) and
    on one client (rows kept, dropped or added). Administrators hold everything, so there's nothing to set."""
    user = _user_or_404(user_id)
    if problem := _out_of_reach(user) or _own_access(user):
        flash(problem, "error")
        return redirect(url_for("admin.edit_user", user_id=user_id))
    role = roles.role_of(g.db, user)
    if role.kind == permissions.ADMIN:
        flash("An Administrator holds every capability; give them another role to narrow what they can do.", "error")
        return redirect(url_for("admin.edit_user", user_id=user_id))
    f = request.form
    actor = current()
    allowed = permissions.allowed_for(role.kind, permissions.KEYS)
    before = roles.overrides(g.db, user_id)
    # Without the capability yourself you can take it away but not add it; what's set already stays.
    grantable = set(actor.everywhere())
    shown = {t["id"] for t in _manageable_tenants()}
    items = []
    for cap in allowed:
        choice = f.get(f"g_{cap}", "")
        if choice == "allow" and cap not in grantable:
            items += [o for o in before if o.capability == cap and o.tenant_id is None]
        elif choice in ("allow", "deny"):
            items.append(roles.Override(cap, None, choice == "allow"))
    dropped = set(f.getlist("drop"))
    for o in before:
        if o.tenant_id is not None and (o.tenant_id not in shown or f"{o.capability}|{o.tenant_id}" not in dropped):
            items.append(o)
    tenant = f.get("add_tenant", "")
    cap = f.get("add_capability", "")
    if tenant.isdigit() and cap in allowed:
        if int(tenant) not in shown:
            abort(400)
        if f.get("add_allow") == "allow" and cap not in grantable:
            flash(_NOT_GRANTABLE, "error")
            return redirect(url_for("admin.edit_user", user_id=user_id) + "#access")
        items = [o for o in items if not (o.capability == cap and o.tenant_id == int(tenant))]
        items.append(roles.Override(cap, int(tenant), f.get("add_allow") == "allow"))
    with transaction(g.db):
        changed = roles.set_overrides(g.db, g.user["username"], user, items)
    flash("Saved. They'll sign in again to pick it up." if changed else "Nothing changed.", "success")
    return redirect(url_for("admin.edit_user", user_id=user_id) + "#access")


def _is_locked(user) -> bool:
    return bool(user["locked_until"]) and user["locked_until"] > utcnow()


@bp.post("/users/<int:user_id>/reset-two-step")
@needs("users.manage")
def reset_two_step(user_id: int):
    """For a lost phone: remove their authenticator and sign them out. They set it up again at next sign-in."""
    user = _user_or_404(user_id)
    if problem := _out_of_reach(user):
        flash(problem, "error")
        return redirect(url_for("admin.edit_user", user_id=user_id))
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
@needs("users.manage")
def sign_out_user(user_id: int):
    user = _user_or_404(user_id)
    if problem := _out_of_reach(user):
        flash(problem, "error")
        return redirect(url_for("admin.edit_user", user_id=user_id))
    if not _admin_password_ok():
        return redirect(url_for("admin.edit_user", user_id=user_id))
    ended = sessions.revoke_user(user_id, except_current=user_id == g.user["id"])
    audit(g.db, g.user["username"], "user_signed_out", detail=f"{user['username']}: {plural(ended, 'session')}")
    flash(f"Signed {user['username']} out of {ended} browser{'s' if ended != 1 else ''}.", "success")
    return redirect(url_for("admin.edit_user", user_id=user_id))


@bp.post("/users/<int:user_id>/unlock")
@needs("users.manage")
def unlock_user(user_id: int):
    user = _user_or_404(user_id)
    if problem := _out_of_reach(user):
        flash(problem, "error")
        return redirect(url_for("admin.edit_user", user_id=user_id))
    g.db.execute("UPDATE users SET failed_logins = 0, locked_until = NULL WHERE id = ?", (user_id,))
    throttle.clear(g.db, user["username"])
    audit(g.db, g.user["username"], "user_unlocked", detail=user["username"])
    flash(f"{user['username']} can sign in again.", "success")
    return redirect(url_for("admin.edit_user", user_id=user_id))


@bp.post("/users/<int:user_id>/delete")
@needs("users.manage")
def delete_user(user_id: int):
    user = _user_or_404(user_id)
    if problem := _out_of_reach(user):
        flash(problem, "error")
        return redirect(url_for("admin.edit_user", user_id=user_id))
    if user["id"] == g.user["id"]:
        flash("You can't delete your own account.", "error")
        return redirect(url_for("admin.edit_user", user_id=user_id))
    if user["role"] == "admin" and not user["disabled"] and _last_admin(user_id):
        flash("This is the only Administrator. Make someone else an Administrator first.", "error")
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
@needs("settings.manage")
def connections():
    rows = store.list_connections(g.db)
    sign_in = app_setup.current(g.db, g.user["id"])
    update = app_setup.current(g.db, g.user["id"], "update")
    if update and update["status"] == "done":
        app_setup.cancel(g.db, g.user["id"], "update")  # updated: say so once
        flash(
            f"{update['name']} now asks for every permission. Next: each client approves the new ones with its "
            "link, from Onboarding.",
            "success",
        )
        update = None
    if (
        sign_in
        and sign_in["status"] == "done"
        and g.db.execute("SELECT 1 FROM tenants WHERE connection_id = ?", (sign_in["connection_id"],)).fetchone()
    ):
        app_setup.cancel(g.db, g.user["id"])  # the first tenant is added: the wizard is finished
        sign_in = None
    # An update under way takes the page's one wizard panel, in its connection's card.
    updating = update["connection_id"] if update else None
    sign_in = update or sign_in
    return render_template(
        "connections.html",
        connections=rows,
        permissions=entra.PERMISSIONS,
        batch=permission_batch.CURRENT,
        release_of=permission_batch.release_of,
        app_ready={c["id"]: permission_batch.app_ready(g.db, c) for c in rows if c["client_id"]},
        updating=updating,
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
@needs("settings.manage")
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


def _wizard_purpose() -> str:
    """Which sign-in the page's wizard panel shows: an app update under way, else creating the app."""
    return "update" if app_setup.current(g.db, g.user["id"], "update") else "connection"


@bp.post("/connections/setup")
@needs("settings.manage")
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
            "Add clients with it, or replace its certificate below if that's what you need.",
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
@needs("settings.manage")
def check_setup():
    """One step of the wizard. The page calls this every few seconds while it waits (JSON); without
    JavaScript, the "I've signed in" button posts it and comes back to the page."""
    row = app_setup.check(
        g.db,
        current_app.extensions["officesentry.vault"],
        g.user["id"],
        g.user["username"],
        _redirect_uri(),
        purpose=_wizard_purpose(),
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


@bp.post("/connections/<int:conn_id>/update")
@needs("settings.manage")
def update_app(conn_id: int):
    """Sign in to Microsoft to add a new batch of permissions to this connection's app registration."""
    connection = store.get_connection(g.db, conn_id)
    if connection is None:
        abort(404)
    if not connection["client_id"]:
        flash("Save this connection's client ID first.", "error")
        return redirect(url_for("admin.connections") + f"#conn-{conn_id}")
    tenant = app_setup.tenant_ref(request.form.get("tenant", ""))
    if tenant is None:
        flash("Enter your tenant's domain or Directory (tenant) ID, or leave it blank.", "error")
        return redirect(url_for("admin.connections") + f"#conn-{conn_id}")
    try:
        app_setup.start(
            g.db,
            current_app.extensions["officesentry.vault"],
            g.user["id"],
            g.user["username"],
            tenant,
            connection=connection,
        )
    except entra.SignInError as exc:
        flash(str(exc), "error")
    return redirect(url_for("admin.connections") + f"#conn-{conn_id}")


@bp.post("/connections/<int:conn_id>/updated-by-hand")
@needs("settings.manage")
def app_updated_by_hand(conn_id: int):
    """The admin added the new permissions to the app registration in the Entra admin centre themselves."""
    connection = store.get_connection(g.db, conn_id)
    if connection is None:
        abort(404)
    permission_batch.mark_app(g.db, conn_id, g.user["username"], "by hand")
    flash(
        "Noted. Each client approves the new permissions with its link, from Onboarding. If a client approves and "
        "they're still missing, the app registration doesn't ask for them yet.",
        "success",
    )
    return redirect(url_for("admin.connections") + f"#conn-{conn_id}")


@bp.post("/connections/setup/cancel")
@needs("settings.manage")
def cancel_setup():
    app_setup.cancel(g.db, g.user["id"], _wizard_purpose())
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
    g.db.execute(
        "DELETE FROM permission_requests WHERE tenant_id IN (SELECT id FROM tenants WHERE connection_id = ?)",
        (conn_id,),
    )
    for tenant_id in ids:
        audit(g.db, g.user["username"], "tenant_consent_reset", tenant_id, f"its app's client ID changed from {old}")
    return len(ids)


@bp.post("/connections/<int:conn_id>/client-id")
@needs("settings.manage")
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
@needs("settings.manage")
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
@needs("settings.manage")
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
@needs("settings.manage")
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
@needs("settings.manage")
def switch_rollover(conn_id: int):
    if _rollover(conn_id, lambda: cert_lifecycle.switch(g.db, conn_id, g.user["username"])):
        flash(
            "Switched. Collection signs in with the new certificate from now on. Last step: remove the old "
            "certificate from the app registration.",
            "success",
        )
    return _to_connection(conn_id)


@bp.post("/connections/<int:conn_id>/replace/cancel")
@needs("settings.manage")
def cancel_rollover(conn_id: int):
    if _rollover(conn_id, lambda: cert_lifecycle.cancel(g.db, conn_id, g.user["username"])):
        flash(
            "Replacement cancelled. If you uploaded its .cer to the app registration, delete it there too.", "success"
        )
    return _to_connection(conn_id)


@bp.post("/connections/<int:conn_id>/old-removed")
@needs("settings.manage")
def old_certificate_removed(conn_id: int):
    if _rollover(conn_id, lambda: cert_lifecycle.forget_old(g.db, conn_id, g.user["username"])):
        flash("Noted. The old certificate is no longer listed here.", "success")
    return _to_connection(conn_id)
