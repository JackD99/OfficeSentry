"""Autodesk licences' pages (``autodesk/setup.py`` does the work).

* ``/admin/autodesk`` (settings, admins): the app the MSP registered with Autodesk, the Autodesk sign-ins and
  which teams each one sees, and which client each team was given to.
* ``/tenants/<id>/autodesk`` (the client's Settings tab): the client's Autodesk teams. Only someone who manages
  every client is offered the teams nobody has yet, since their names come from other clients' Autodesk accounts.

The app's secret and the sign-ins' tokens are written once and never sent back to the browser. Every page about
one client is only there once Autodesk is set up.
"""

from __future__ import annotations

from flask import Blueprint, abort, current_app, flash, g, redirect, render_template, request, url_for

from .. import store
from ..autodesk import setup
from ..db import audit
from .access import can, manages_groups, needs, tenant_or_404, visible_tenants

bp = Blueprint("autodesk", __name__)


def _vault():
    return current_app.extensions["officesentry.vault"]


def _to_settings(anchor: str = ""):
    return redirect(url_for("autodesk.settings") + anchor)


# -- Settings, Autodesk ----------------------------------------------------------------------------------------


@bp.get("/admin/autodesk")
@needs("settings.manage", kind="staff")
def settings():
    sign_ins = setup.sign_ins(g.db)
    tenants = {t["id"]: t for t in visible_tenants(g.db, g.user, archived=True)}
    given = {
        r["team_id"]: tenants.get(r["tenant_id"])
        for r in g.db.execute("SELECT team_id, tenant_id FROM autodesk_teams").fetchall()
    }
    return render_template(
        "autodesk.html",
        app=setup.app(g.db),
        configured=setup.configured(g.db),
        sign_ins=sign_ins,
        given=given,
        reporting=setup.REPORTING,
        clients=sum(1 for t in given.values() if t is not None),
    )


@bp.post("/admin/autodesk/app")
@needs("settings.manage", kind="staff")
def save_app():
    try:
        setup.save_app(
            g.db, _vault(), request.form.get("client_id", ""), request.form.get("secret", ""), g.user["username"]
        )
    except setup.SetupError as exc:
        flash(str(exc), "error")
        return _to_settings("#app")
    flash("Saved. Next: add an Autodesk sign-in, made by an admin of your clients' Autodesk teams.", "success")
    return _to_settings("#sign-ins")


@bp.post("/admin/autodesk/app/remove")
@needs("settings.manage", kind="staff")
def remove_app():
    setup.forget_app(g.db, g.user["username"])
    flash(
        "Autodesk removed: nothing more is read, and its sign-ins are deleted. Which team is whose is kept.", "success"
    )
    return _to_settings()


@bp.post("/admin/autodesk/sign-ins")
@needs("settings.manage", kind="staff")
def add_sign_in():
    if not setup.configured(g.db):
        flash("Save the Autodesk app's client ID and secret first.", "error")
        return _to_settings("#app")
    try:
        sign_in_id = setup.add_sign_in(
            g.db, _vault(), request.form.get("label", ""), request.form.get("token", ""), g.user["username"]
        )
    except setup.SetupError as exc:
        flash(str(exc), "error")
        return _to_settings("#sign-ins")
    _check(sign_in_id, added=True)
    return _to_settings(f"#sign-in-{sign_in_id}")


def _check(sign_in_id: int, added: bool = False) -> None:
    try:
        teams = setup.check(g.db, _vault(), sign_in_id)
    except setup.SetupError as exc:
        flash(("Added, but checking it failed: " if added else "") + str(exc), "error")
        return
    with_api = sum(1 for t in teams if setup.REPORTING in t["benefits"])
    if not teams:
        flash("Autodesk says this sign-in administers no team. Use a token made by a team admin.", "warning")
    else:
        flash(
            f"Autodesk lists {len(teams)} team{'s' if len(teams) != 1 else ''} for this sign-in, {with_api} with API "
            "reporting. Give each client its team from the client's Settings tab, Autodesk.",
            "success" if with_api else "warning",
        )


@bp.post("/admin/autodesk/sign-ins/<int:sign_in_id>/check")
@needs("settings.manage", kind="staff")
def check(sign_in_id: int):
    if setup.get_sign_in(g.db, sign_in_id) is None:
        abort(404)
    _check(sign_in_id)
    audit(g.db, g.user["username"], "autodesk_sign_in_checked", detail=str(sign_in_id))
    return _to_settings(f"#sign-in-{sign_in_id}")


@bp.post("/admin/autodesk/sign-ins/<int:sign_in_id>/remove")
@needs("settings.manage", kind="staff")
def remove_sign_in(sign_in_id: int):
    try:
        setup.remove_sign_in(g.db, sign_in_id, g.user["username"])
    except setup.SetupError as exc:
        flash(str(exc), "error")
        return _to_settings("#sign-ins")
    flash("Sign-in removed. Delete the token in Autodesk Account too (Profile, Security).", "success")
    return _to_settings("#sign-ins")


# -- One client -------------------------------------------------------------------------------------------------


def _client(tenant_id: int):
    """The tenant, once Autodesk is set up (its pages aren't there otherwise)."""
    t = tenant_or_404(tenant_id)
    if not setup.configured(g.db):
        abort(404)
    return t


def _back(t):
    return redirect(url_for("autodesk.client", tenant_id=t["id"]))


@bp.get("/tenants/<int:tenant_id>/autodesk")
@needs("clients.manage", kind="staff")
def client(tenant_id: int):
    t = _client(tenant_id)
    teams = setup.teams_for(g.db, t["id"])
    seen = setup.seen_teams(g.db)
    return render_template(
        "autodesk_client.html",
        tenant=t,
        teams=teams,
        seen=seen,
        free=setup.free_teams(g.db) if manages_groups() else [],
        picks=manages_groups(),
        reporting=setup.REPORTING,
        last_run=store.latest_run(g.db, t["id"], setup.COLLECTOR, successful=False),
        can_collect=can("collect", t["id"]),
    )


def _picker(t):
    """Only someone who manages every client gives or takes away an Autodesk team."""
    if not manages_groups():
        flash("Only someone who manages every client picks Autodesk teams, since a team may be any client's.", "error")
        return _back(t)
    return None


@bp.post("/tenants/<int:tenant_id>/autodesk/teams")
@needs("clients.manage", kind="staff")
def add_team(tenant_id: int):
    t = _client(tenant_id)
    if (refused := _picker(t)) is not None:
        return refused
    try:
        name = setup.assign(g.db, t, request.form.get("team", ""), g.user["username"])
    except setup.SetupError as exc:
        flash(str(exc), "error")
        return _back(t)
    queued = ""
    if t["enabled"] and not t["archived_at"] and can("collect", t["id"]):
        store.enqueue_run(g.db, t["id"], setup.COLLECTOR, requested_by=g.user["username"])
        queued = " Reading its licences now; reload in a few minutes."
    flash(f"{name} is now {t['name']}'s Autodesk team.{queued}", "success")
    return _back(t)


@bp.post("/tenants/<int:tenant_id>/autodesk/teams/remove")
@needs("clients.manage", kind="staff")
def remove_team(tenant_id: int):
    t = _client(tenant_id)
    if (refused := _picker(t)) is not None:
        return refused
    try:
        setup.unassign(g.db, t, request.form.get("team", ""), g.user["username"])
    except setup.SetupError as exc:
        flash(str(exc), "error")
        return _back(t)
    flash("Team removed: its licences aren't read for this client any more. What was read stays.", "success")
    return _back(t)


@bp.post("/tenants/<int:tenant_id>/autodesk/collect")
@needs("collect", kind="staff")
def collect(tenant_id: int):
    t = _client(tenant_id)
    if not setup.applies(g.db, t):
        flash("Give the client an Autodesk team first.", "error")
    elif not t["enabled"] or t["archived_at"]:
        flash("This client is paused or archived, so nothing is read.", "error")
    else:
        store.enqueue_run(g.db, t["id"], setup.COLLECTOR, requested_by=g.user["username"])
        audit(g.db, g.user["username"], "collect_requested", t["id"], setup.COLLECTOR)
        flash("Reading the client's Autodesk licences now. Reload in a few minutes.", "success")
    return _back(t)
