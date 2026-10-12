"""Roles (``/admin/roles``, under Users): the built-in roles, custom ones, and the editor.

A role is a set of capabilities and a scope (``permissions.py``, stored by ``roles.py``). The built-in
roles can be changed (not Administrator) and put back to what they ship with; custom roles are made from
scratch or as a copy of another. A role's kind (staff, or a client's people) is fixed once made, since
``users.role`` keeps it for the accounts that hold it.
"""

from __future__ import annotations

from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for

from .. import permissions, roles
from ..db import transaction
from .access import current, needs, out_of_reach

bp = Blueprint("roles", __name__, url_prefix="/admin/roles")

# What the menu offers a staff role, by the capabilities each entry needs (base.html), for the preview.
MENU = (
    ("Today and Accounts home pages", ("clients.compare",)),
    ("All clients, What changed and search", ("clients.compare",)),
    ("Coming up", ("clients.compare", "reports.tenant")),
    ("Email reports", ("emails.view",)),
    ("Month-end", ("month_end.review",)),
    ("Projects", ("projects.view", "clients.compare")),
    ("Client settings and connection health", ()),
    ("Client groups and onboarding", ("clients.manage",)),
    ("Users and roles", ("users.manage",)),
    ("App connection, email delivery, alerts, branding, licence prices, data retention", ("settings.manage",)),
    ("Activity log", ("activity_log.read",)),
    ("System status", ("system.manage",)),
)
# A client's tabs (``_macros.tenant_header``), for both kinds.
TABS = (
    ("Findings, the monthly review and evidence", ("reports.security",)),
    ("Changes", ("reports.changes",)),
    ("Reports", ("reports.identity|reports.email|reports.files|reports.devices|reports.tenant",)),
    ("Users (people)", ("reports.people",)),
    ("Emails", ("emails.view",)),
    ("Settings (data collection)", ("collect",)),
)


def _grantable() -> frozenset[str]:
    """The capabilities the signed-in person may put into a role: all of them for an Administrator,
    otherwise only the ones they hold everywhere themselves, on every client they see (the user page follows
    the same rule)."""
    return current().everywhere()


def _beyond(capabilities, scope: str, kind: str) -> bool:
    """Would a role like this hold more than the signed-in person?"""
    actor = current()
    if actor.is_admin:
        return False
    wider = scope == permissions.ALL_CLIENTS and kind == permissions.STAFF and not actor.sees_all
    return wider or bool(set(capabilities) - _grantable())


_HOLDS_MORE = "That role holds more than you do, so only an Administrator can {}."
_HELD_BY_MORE = "Someone holding this role can see or do more than you can, so only an Administrator can change it."


def _held_by_more(role: roles.Role) -> bool:
    """Is the role held by an account the signed-in person couldn't change on its own page (an
    Administrator, or someone who sees or does more than they do)? Changing the role would change that
    account, so only an Administrator may (``access.out_of_reach``, which the user page uses)."""
    if current().is_admin:
        return False
    holders = g.db.execute("SELECT * FROM users WHERE role_id = ?", (role.id,)).fetchall()
    return any(out_of_reach(u) for u in holders)


def _role_or_404(role_id: int) -> roles.Role:
    role = roles.get(g.db, role_id)
    if role is None:
        abort(404)
    return role


def _capabilities(kind: str) -> list[permissions.Capability]:
    allowed = permissions.allowed_for(kind, permissions.KEYS)
    return [c for c in permissions.CAPABILITIES if c.key in allowed]


def _holders(role_id: int) -> list:
    return g.db.execute(
        "SELECT id, username, disabled FROM users WHERE role_id = ? ORDER BY username COLLATE NOCASE", (role_id,)
    ).fetchall()


@bp.get("")
@needs("users.manage")
def index():
    all_roles = roles.all_roles(g.db)
    members = roles.members(g.db)
    return render_template(
        "roles.html",
        roles=all_roles,
        members=members,
        groups=permissions.GROUPS,
        capabilities=permissions.CAPABILITIES,
        kinds=((permissions.STAFF, "Staff"), (permissions.CLIENT, "A client's people")),
    )


@bp.post("")
@needs("users.manage")
def create():
    f = request.form
    kind = permissions.CLIENT if f.get("copy") == "client" else permissions.STAFF
    copy = roles.get(g.db, int(f["copy"])) if f.get("copy", "").isdigit() else None
    if copy is not None and copy.kind == permissions.ADMIN:
        copy = None  # an Administrator copy would be a staff role with everything; start from Team leader instead
    if copy is not None:
        kind = copy.kind
        if _beyond(copy.capabilities, copy.scope, copy.kind):
            flash(_HOLDS_MORE.format("copy it"), "error")
            return redirect(url_for("roles.index"))
    try:
        with transaction(g.db):
            role_id = roles.create(
                g.db,
                g.user["username"],
                f.get("name", ""),
                f.get("description", "") or (copy.description if copy else ""),
                kind,
                copy.scope if copy else permissions.GRANTED,
                copy.capabilities if copy else (),
            )
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(url_for("roles.index"))
    flash("Created. Now choose what the role can do.", "success")
    return redirect(url_for("roles.edit", role_id=role_id))


@bp.route("/<int:role_id>", methods=["GET", "POST"])
@needs("users.manage")
def edit(role_id: int):
    role = _role_or_404(role_id)
    if request.method == "POST":
        if not role.editable:
            abort(403)
        if _held_by_more(role):
            flash(_HELD_BY_MORE, "error")
            return redirect(url_for("roles.edit", role_id=role_id))
        f = request.form
        # What the signed-in person doesn't hold themselves stays as it is (those boxes are greyed out).
        grantable = _grantable()
        wanted = (set(f.getlist("capabilities")) & grantable) | (role.capabilities - grantable)
        scope = f.get("scope", permissions.GRANTED)
        if _beyond((), permissions.ALL_CLIENTS, role.kind):
            scope = role.scope  # only someone who sees every client changes a staff role's scope
        try:
            with transaction(g.db):
                changed = roles.update(
                    g.db, g.user["username"], role, f.get("name", ""), f.get("description", ""), scope, wanted
                )
        except ValueError as exc:
            flash(str(exc), "error")
            return redirect(url_for("roles.edit", role_id=role_id))
        holders = roles.members(g.db).get(role_id, 0)
        if changed and holders:
            noun = "person" if holders == 1 else "people"
            flash(f"Saved. The {holders} {noun} holding this role will sign in again to pick it up.", "success")
        else:
            flash("Saved." if changed else "Nothing changed.", "success")
        return redirect(url_for("roles.edit", role_id=role_id))
    held_by_more = role.editable and _held_by_more(role)
    return render_template(
        "role_edit.html",
        role=role,
        editable=role.editable and not held_by_more,
        held_by_more=held_by_more,
        template=role.template,
        groups=permissions.GROUPS,
        capabilities=_capabilities(role.kind),
        holders=_holders(role_id),
        menu=MENU if role.kind != permissions.CLIENT else (),
        grantable=_grantable(),
        scope_fixed=_beyond((), permissions.ALL_CLIENTS, role.kind),
        tabs=TABS,
        scopes=(
            (permissions.ALL_CLIENTS, "Every client, including ones added later"),
            (permissions.GRANTED, "The clients and groups granted on each person's page"),
        ),
    )


@bp.post("/<int:role_id>/reset")
@needs("users.manage")
def reset(role_id: int):
    role = _role_or_404(role_id)
    t = role.template
    if t is not None and _beyond(t.capabilities, t.scope, t.kind):
        flash(_HOLDS_MORE.format("put it back to its default"), "error")
        return redirect(url_for("roles.edit", role_id=role_id))
    if _held_by_more(role):
        flash(_HELD_BY_MORE, "error")
        return redirect(url_for("roles.edit", role_id=role_id))
    with transaction(g.db):
        changed = roles.reset(g.db, g.user["username"], role)
    flash(f"{role.template.name} is back to its default." if changed else "Already the default.", "success")
    return redirect(url_for("roles.edit", role_id=role_id))


@bp.post("/<int:role_id>/delete")
@needs("users.manage")
def delete(role_id: int):
    role = _role_or_404(role_id)
    try:
        with transaction(g.db):
            roles.remove(g.db, g.user["username"], role)
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(url_for("roles.edit", role_id=role_id))
    flash(f"Deleted {role.name}.", "success")
    return redirect(url_for("roles.index"))
