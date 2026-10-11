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
from .access import needs

bp = Blueprint("roles", __name__, url_prefix="/admin/roles")

# What the menu offers a staff role, by the capabilities each entry needs (base.html), for the preview.
MENU = (
    ("Today and Clients home pages", ("clients.compare",)),
    ("All tenants, What changed and search", ("clients.compare",)),
    ("Coming up", ("clients.compare", "reports.tenant")),
    ("Email reports", ("emails.view",)),
    ("Month-end", ("month_end.review",)),
    ("Projects", ("projects.view", "clients.compare")),
    ("Client tenants and connection health", ()),
    ("Tenant groups and onboarding", ("clients.manage",)),
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
        f = request.form
        try:
            with transaction(g.db):
                changed = roles.update(
                    g.db,
                    g.user["username"],
                    role,
                    f.get("name", ""),
                    f.get("description", ""),
                    f.get("scope", permissions.GRANTED),
                    f.getlist("capabilities"),
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
    return render_template(
        "role_edit.html",
        role=role,
        template=role.template,
        groups=permissions.GROUPS,
        capabilities=_capabilities(role.kind),
        holders=_holders(role_id),
        menu=MENU if role.kind != permissions.CLIENT else (),
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
            roles.delete(g.db, g.user["username"], role)
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(url_for("roles.edit", role_id=role_id))
    flash(f"Deleted {role.name}.", "success")
    return redirect(url_for("roles.index"))
