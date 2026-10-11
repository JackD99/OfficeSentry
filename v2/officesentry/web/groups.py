"""Tenant groups: the admin page, and the group picker on staff pages that list tenants.

The picker (``group_aware`` views, ``templates/_groups.html``) narrows a page to
one group of the tenants someone already sees. The choice is kept on their
sign-in session (``sessions.group_id``), so the dashboard, All tenants, What
changed, the month-end queue, Client tenants and their downloads all show the
same group until they pick another; ``?group=<id>`` (or ``?group=all``) on any
of those pages picks one and redirects to the plain URL, so links can carry a
group. Groups never add tenants: ``scoped_tenants`` filters
``visible_tenants``. See ``tenant_groups.py``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import wraps
from urllib.parse import urlencode

from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for

from .. import tenant_groups
from ..db import audit, transaction
from ..text import plural
from .access import is_staff, needs, visible_tenants

bp = Blueprint("groups", __name__)

# -- The picker ---------------------------------------------------------------------------------


@dataclass
class Scope:
    """What a group-aware page shows: the chosen group (or None) and the groups worth offering."""

    group: tenant_groups.Option | None = None
    options: list = field(default_factory=list)
    visible: int = 0  # tenants the person sees in all

    @property
    def by_category(self):
        return tenant_groups.by_category(self.options)

    @property
    def label(self) -> str:
        """Where a download or heading says which tenants it covers."""
        return self.group.name if self.group else "All clients"


def _save_choice(group_id: int | None) -> None:
    if g.get("session_row") is not None:
        g.db.execute("UPDATE sessions SET group_id = ? WHERE id = ?", (group_id, g.session_row["id"]))


def scope() -> Scope:
    """This request's group scope, worked out once. Only staff pages call it."""
    if "group_scope" in g:
        return g.group_scope
    tenants = visible_tenants(g.db, g.user)
    options = tenant_groups.options(g.db, tenants)
    chosen = g.session_row["group_id"] if g.get("session_row") is not None else None
    group = next((o for o in options if o.id == chosen), None)
    g.group_scope = Scope(group, options, len(tenants))
    g.scope_tenants = tenant_groups.only(g.db, tenants, group.id if group else None)
    return g.group_scope


def scoped_tenants() -> list:
    """The tenants this person sees, narrowed to the group they chose."""
    scope()
    return g.scope_tenants


def group_aware(view):
    """For staff pages that list tenants: ``?group=`` picks a group (kept on the session) and redirects to
    the same URL without it. Put it under ``@needs``."""

    @wraps(view)
    def wrapper(*args, **kwargs):
        if not is_staff():
            return view(*args, **kwargs)  # clients have no groups (the dashboard is their home page)
        if request.method == "GET" and "group" in request.args:
            raw = request.args.get("group", "")
            picked = int(raw) if raw.isdigit() else None
            # Only a group holding a tenant this person sees can be picked; anything else means all tenants.
            offered = {o.id for o in tenant_groups.options(g.db, visible_tenants(g.db, g.user))}
            _save_choice(picked if picked in offered else None)
            rest = [(k, v) for k, v in request.args.items(multi=True) if k != "group"]
            return redirect(request.path + (f"?{urlencode(rest)}" if rest else ""))
        scope()
        return view(*args, **kwargs)

    return wrapper


# -- Admin page ---------------------------------------------------------------------------------


def _group_or_404(group_id: int):
    group = tenant_groups.get(g.db, group_id)
    if group is None:
        abort(404)
    return group


def _all_tenants():
    # Archived tenants are listed too, so saving keeps their membership for when they're restored.
    return g.db.execute("SELECT id, name, tenant_ref, archived_at FROM tenants ORDER BY name COLLATE NOCASE").fetchall()


def _categories() -> list[str]:
    used = [
        r[0]
        for r in g.db.execute(
            "SELECT DISTINCT category FROM tenant_groups WHERE category <> '' ORDER BY category COLLATE NOCASE"
        )
    ]
    return list(dict.fromkeys(used + list(tenant_groups.SUGGESTED_CATEGORIES)))


@bp.get("/admin/groups")
@needs("clients.manage")
def index():
    groups = tenant_groups.all_groups(g.db)
    tenants = [t for t in _all_tenants() if not t["archived_at"]]
    grouped = {r[0] for r in g.db.execute("SELECT DISTINCT m.tenant_id FROM tenant_group_members m")}
    return render_template(
        "groups.html",
        groups=groups,
        categories=_categories(),
        tenant_count=len(tenants),
        ungrouped=sum(1 for t in tenants if t["id"] not in grouped),
        by_category=_by_category(groups),
    )


def _by_category(groups) -> list[tuple[str, list]]:
    out: dict[str, list] = {}
    for gr in groups:
        out.setdefault(gr["category"], []).append(gr)
    return sorted(out.items(), key=lambda kv: (kv[0] == "", kv[0].lower()))


@bp.post("/admin/groups")
@needs("clients.manage")
def create():
    f = request.form
    values, problem = tenant_groups.clean(f.get("name", ""), f.get("category", ""), f.get("description", ""))
    if problem is None and tenant_groups.name_taken(g.db, values["name"]):
        problem = f"There's already a group called “{values['name']}”."
    if problem:
        flash(problem, "error")
        return redirect(url_for("groups.index"))
    with transaction(g.db):
        group_id = tenant_groups.create(g.db, values, g.user["username"])
        audit(g.db, g.user["username"], "tenant_group_created", detail=values["name"])
    flash(f"Created “{values['name']}”. Now tick the clients that belong in it.", "success")
    return redirect(url_for("groups.edit", group_id=group_id))


@bp.route("/admin/groups/<int:group_id>", methods=["GET", "POST"])
@needs("clients.manage")
def edit(group_id: int):
    group = _group_or_404(group_id)
    tenants = _all_tenants()
    if request.method == "POST":
        f = request.form
        values, problem = tenant_groups.clean(f.get("name", ""), f.get("category", ""), f.get("description", ""))
        if problem is None and tenant_groups.name_taken(g.db, values["name"], group_id):
            problem = f"There's already a group called “{values['name']}”."
        if problem:
            flash(problem, "error")
            return redirect(url_for("groups.edit", group_id=group_id))
        valid = {t["id"] for t in tenants}
        chosen = {int(x) for x in f.getlist("tenants") if x.isdigit()} & valid
        with transaction(g.db):
            tenant_groups.update(g.db, group_id, values)
            added, removed = tenant_groups.set_members(g.db, group_id, chosen)
            audit(
                g.db,
                g.user["username"],
                "tenant_group_updated",
                detail=f"{values['name']}: {plural(len(chosen), 'tenant')} (+{added} -{removed})",
            )
        flash(f"Saved. “{values['name']}” has {plural(len(chosen), 'client')}.", "success")
        return redirect(url_for("groups.index"))
    analysts = g.db.execute(
        "SELECT u.username FROM users u JOIN user_tenant_groups ug ON ug.user_id = u.id"
        " WHERE ug.group_id = ? AND u.role = 'analyst' ORDER BY u.username COLLATE NOCASE",
        (group_id,),
    ).fetchall()
    return render_template(
        "group_edit.html",
        group=group,
        tenants=tenants,
        chosen=tenant_groups.members(g.db, group_id),
        categories=_categories(),
        usage=tenant_groups.usage(g.db, group_id),
        analysts=[a["username"] for a in analysts],
        others=tenant_groups.of_tenants(g.db, [t["id"] for t in tenants]),
    )


@bp.post("/admin/groups/<int:group_id>/delete")
@needs("clients.manage")
def delete(group_id: int):
    group = _group_or_404(group_id)
    usage = tenant_groups.usage(g.db, group_id)
    with transaction(g.db):
        tenant_groups.remove(g.db, group_id)
        audit(g.db, g.user["username"], "tenant_group_deleted", detail=group["name"])
    note = ""
    if usage["digests"]:
        note += f" {plural(usage['digests'], 'digest')} now cover{'s' if usage['digests'] == 1 else ''} every client."
    if usage["analysts"]:
        note += f" {plural(usage['analysts'], 'analyst')} lost the clients it gave them."
    flash(f"Deleted “{group['name']}”. Its clients are still here.{note}", "success")
    return redirect(url_for("groups.index"))
