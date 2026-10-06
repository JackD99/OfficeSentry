"""Client tenant admin: connection health, every client at a glance, edit, pause, archive and offboard.

Health comes from stored state (``tenant_health``); "Check connection now"
queues the ``connection`` collector for the worker rather than calling
Microsoft from the page. Changes are admin-only and audited. Nothing here
writes to a client's tenant: the Microsoft side is done by their admin, from
the instructions on the pages.
"""

from __future__ import annotations

from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for

from .. import clouds, lifecycle, store, tenant_health
from ..db import audit
from ..prospect import tenant_id_for_domain
from ..text import plural
from .access import roles_required, tenant_or_404, visible_tenants
from .tenants import DOMAIN_RE, GUID_RE, _cfg, _consent_link, cloud_mismatch, not_connected

bp = Blueprint("tenant_admin", __name__)

# Worst first on the client list.
STATUS_ORDER = {"attention": 0, "onboarding": 1, "paused": 2, "healthy": 3, "archived": 4}
NOTES_MAX = 2000


def _manage(tenant_id: int):
    return redirect(url_for("tenant_admin.manage", tenant_id=tenant_id))


def _back(tenant_id: int):
    """Back to the page the form was on: the client list, the Overview or the Manage tab."""
    page = request.form.get("back")
    if page == "clients":
        return redirect(url_for("tenant_admin.clients"))
    if page == "overview":
        return redirect(url_for("tenants.tenant", tenant_id=tenant_id))
    return _manage(tenant_id)


# -- Every client at a glance ----------------------------------------------------------------


@bp.get("/admin/tenants")
@roles_required("admin", "analyst")
def clients():
    admin = g.user["role"] == "admin"
    tenants = visible_tenants(g.db, g.user, archived=admin)
    archived = [t for t in tenants if t["archived_at"]]
    show_archived = admin and request.args.get("show") == "archived"
    rows = [tenant_health.assess(g.db, t) for t in (archived if show_archived else
                                                    [t for t in tenants if not t["archived_at"]])]
    rows.sort(key=lambda h: (STATUS_ORDER[h.status], h.tenant["name"].lower()))
    totals = {s: sum(1 for h in rows if h.status == s) for s in STATUS_ORDER}
    return render_template("tenants_admin.html", rows=rows, totals=totals, archived_count=len(archived),
                           show_archived=show_archived, describe=tenant_health.describe,
                           cells=tenant_health.counts)


# -- One tenant ---------------------------------------------------------------------------------


@bp.get("/tenants/<int:tenant_id>/manage")
@roles_required("admin", "analyst")
def manage(tenant_id: int):
    t = tenant_or_404(tenant_id)
    connection = store.get_connection(g.db, t["connection_id"]) if t["connection_id"] else None
    return render_template(
        "manage.html", tenant=t, health=tenant_health.assess(g.db, t), connection=connection,
        connections=store.list_connections(g.db), consent_link=_consent_link(t, connection),
        redirect_uri=_cfg().base_url + url_for("tenants.consent_callback"),
        can_check=_check_problem(t) is None, check_problem=_check_problem(t), notes_max=NOTES_MAX,
    )


def _check_problem(t) -> str | None:
    """Why a connection check can't be queued, or None."""
    if t["archived_at"]:
        return "This tenant is archived. Restore it first."
    if not t["enabled"]:
        return "Collection is paused for this tenant. Resume it first."
    connection = store.get_connection(g.db, t["connection_id"]) if t["connection_id"] else None
    if not connection or not connection["client_id"] or cloud_mismatch(clouds.of(t).key, connection):
        return not_connected(t)
    return None


@bp.post("/tenants/<int:tenant_id>/check-connection")
@roles_required("admin", "analyst")
def check_connection(tenant_id: int):
    t = tenant_or_404(tenant_id)
    if problem := _check_problem(t):
        flash(problem, "error")
        return _back(t["id"])
    store.enqueue_run(g.db, t["id"], "connection", requested_by=g.user["username"])
    audit(g.db, g.user["username"], "connection_check_requested", t["id"])
    flash("Checking the connection. The result shows here within a minute or so; reload the page to see it.",
          "success")
    return _back(t["id"])


@bp.post("/tenants/<int:tenant_id>/edit")
@roles_required("admin")
def edit(tenant_id: int):
    t = tenant_or_404(tenant_id)
    name = request.form.get("name", "").strip()
    ref = request.form.get("tenant_ref", "").strip().lower()
    notes = request.form.get("notes", "").strip()
    conn_id = request.form.get("connection_id", type=int)
    cloud = request.form.get("cloud", clouds.of(t).key)
    if not clouds.valid(cloud):
        abort(400)
    connection = store.get_connection(g.db, conn_id) if conn_id is not None else None
    if conn_id is not None and connection is None:
        abort(400)
    moved_without_app = False
    if cloud != clouds.of(t).key and cloud_mismatch(cloud, connection):
        # Moving to another cloud: use that cloud's app when there's exactly one, else none for now.
        same = [c for c in store.list_connections(g.db) if clouds.of(c).key == cloud]
        connection = same[0] if len(same) == 1 else None
        conn_id = connection["id"] if connection else None
        moved_without_app = connection is None
    problem = None
    if not name or len(name) > 200:
        problem = "Enter a client name (up to 200 characters)."
    elif not (GUID_RE.match(ref) or DOMAIN_RE.match(ref)):
        problem = "Enter the tenant ID (GUID) or its domain, e.g. contoso.onmicrosoft.com."
    elif len(notes) > NOTES_MAX:
        problem = f"Keep notes under {NOTES_MAX} characters."
    elif mismatch := cloud_mismatch(cloud, connection):
        problem = mismatch
    elif ref != t["tenant_ref"].lower():
        has_data = g.db.execute("SELECT 1 FROM runs WHERE tenant_id = ? AND status IN ('succeeded', 'partial')"
                                " LIMIT 1", (t["id"],)).fetchone()
        # Once data has come in, the ID may only go from a domain to the GUID it stands for.
        if has_data and not (GUID_RE.match(ref) and not GUID_RE.match(t["tenant_ref"])):
            problem = ("Office Sentry has already collected data from this tenant, so its tenant ID can't change. "
                       "To report on a different tenant, add it as a new tenant.")
        elif has_data and (expected := tenant_id_for_domain(t["tenant_ref"].lower(), cloud=cloud)) != ref:
            # Data came from the domain's tenant: the GUID must be that same tenant, not another client's.
            problem = (f"{ref} isn't the tenant ID Microsoft gives for {t['tenant_ref']} ({expected})." if expected
                       else f"Office Sentry couldn't confirm the tenant ID for {t['tenant_ref']} with Microsoft just "
                            "now. Try again in a few minutes.")
        elif g.db.execute("SELECT 1 FROM tenants WHERE tenant_ref = ? AND id <> ?", (ref, t["id"])).fetchone():
            problem = "Another tenant already uses that tenant ID or domain."
    if problem:
        flash(problem, "error")
        return _manage(t["id"])

    changes = []
    reset_consent = False
    if name != t["name"]:
        changes.append(f"name {t['name']!r} -> {name!r}")
    if ref != t["tenant_ref"].lower():
        changes.append(f"tenant {t['tenant_ref']} -> {ref}")
        # Consent recorded for the old ID says nothing about the new one, unless data already proved it.
        reset_consent = not g.db.execute("SELECT 1 FROM runs WHERE tenant_id = ? AND status IN"
                                         " ('succeeded', 'partial') LIMIT 1", (t["id"],)).fetchone()
    if conn_id != t["connection_id"]:
        changes.append(f"connection {t['connection_id']} -> {conn_id}")
        reset_consent = reset_consent or _other_app(t["connection_id"], conn_id)
    if cloud != clouds.of(t).key:
        changes.append(f"cloud {clouds.of(t).key} -> {cloud}")
        reset_consent = True  # consent in one cloud says nothing about another
    if notes != (t["notes"] or ""):
        changes.append("notes")
    if not changes:
        flash("Nothing changed.", "success")
        return _manage(t["id"])
    g.db.execute("UPDATE tenants SET name = ?, tenant_ref = ?, connection_id = ?, notes = ?, cloud = ?,"
                 " consented_at = CASE WHEN ? THEN NULL ELSE consented_at END WHERE id = ?",
                 (name, ref, conn_id, notes, cloud, int(reset_consent), t["id"]))
    audit(g.db, g.user["username"], "tenant_updated", t["id"], "; ".join(changes))
    flash("Saved." + (" The tenant now needs admin consent for its new app or ID." if reset_consent else ""),
          "success")
    if moved_without_app:
        flash(clouds.app_needed(clouds.get(cloud)), "warning")
    return _manage(t["id"])


def _other_app(old_id: int | None, new_id: int | None) -> bool:
    """True when the tenant moves to an app with a different client ID, which needs its own consent."""
    old = store.get_connection(g.db, old_id) if old_id else None
    new = store.get_connection(g.db, new_id) if new_id else None
    return bool(old and old["client_id"] and (not new or new["client_id"] != old["client_id"]))


@bp.post("/tenants/<int:tenant_id>/pause")
@roles_required("admin")
def pause(tenant_id: int):
    t = tenant_or_404(tenant_id)
    if t["enabled"]:
        dropped = lifecycle.pause(g.db, t, g.user["username"])
        flash(f"Paused {t['name']}: no collection, emailed reports or alert emails until you resume it. "
              "Its data and reports stay." + (f" Cancelled {plural(dropped, 'queued run')}." if dropped else ""),
              "success")
    return _back(t["id"])


@bp.post("/tenants/<int:tenant_id>/resume")
@roles_required("admin")
def resume(tenant_id: int):
    t = tenant_or_404(tenant_id)
    if t["archived_at"]:
        flash("Restore the tenant before resuming it.", "error")
    elif not t["enabled"]:
        lifecycle.resume(g.db, t, g.user["username"])
        flash(f"Resumed {t['name']}. It's collected at the next nightly run; use Refresh all data to collect now.",
              "success")
    return _back(t["id"])


@bp.post("/tenants/<int:tenant_id>/archive")
@roles_required("admin")
def archive(tenant_id: int):
    t = tenant_or_404(tenant_id)
    if not t["archived_at"]:
        lifecycle.archive(g.db, t, g.user["username"])
        flash(f"Archived {t['name']}. It's hidden from tenant lists, dashboards and its client users until restored "
              "(Client tenants → Archived).", "success")
    return redirect(url_for("tenant_admin.clients"))


@bp.post("/tenants/<int:tenant_id>/restore")
@roles_required("admin")
def restore(tenant_id: int):
    t = tenant_or_404(tenant_id)
    if t["archived_at"]:
        lifecycle.restore(g.db, t, g.user["username"])
        flash(f"Restored {t['name']}. Collection is still paused; resume it when you're ready.", "success")
    return _manage(t["id"])


# -- Offboarding --------------------------------------------------------------------------------


@bp.get("/tenants/<int:tenant_id>/offboard")
@roles_required("admin")
def offboard(tenant_id: int):
    t = tenant_or_404(tenant_id)
    health = tenant_health.assess(g.db, t)
    client_users = lifecycle.client_only_users(g.db, t["id"])
    only = {u["id"] for u in client_users}
    # Everyone else granted this tenant just loses the grant.
    others = [u for u in g.db.execute(
        "SELECT u.* FROM users u JOIN user_tenants ut ON ut.user_id = u.id AND ut.tenant_id = ?"
        " ORDER BY u.username COLLATE NOCASE", (t["id"],)) if u["id"] not in only]
    return render_template("offboard.html", tenant=t, health=health, stored=lifecycle.stored(g.db, t["id"]),
                           client_users=client_users, other_users=others)


@bp.post("/tenants/<int:tenant_id>/delete")
@roles_required("admin")
def delete(tenant_id: int):
    t = tenant_or_404(tenant_id)
    if request.form.get("confirm_name", "").strip() != t["name"].strip():
        flash("Type the tenant's name exactly as shown to delete it.", "error")
        return redirect(url_for("tenant_admin.offboard", tenant_id=t["id"]))
    users = [int(x) for x in request.form.getlist("delete_users") if x.isdigit()]
    try:
        removed = lifecycle.delete_tenant(g.db, t, g.user["username"], users)
    except lifecycle.DeleteError as exc:
        flash(f"Nothing was deleted: {exc}.", "error")
        return redirect(url_for("tenant_admin.offboard", tenant_id=t["id"]))
    flash(f"Deleted {t['name']} and everything stored for it: {plural(removed['runs'], 'run')}, "
          f"{plural(removed['objects'], 'stored object')} and {plural(removed['findings'], 'finding')}"
          + (f", and {plural(removed['users'], 'client account')}" if removed["users"] else "") + ".", "success")
    return redirect(url_for("tenant_admin.clients"))
