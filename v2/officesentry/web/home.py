"""Home pages by role (``home.py``): Today for technicians, Clients for account managers.

Staff only, over the tenants they see narrowed to their chosen group. ``/home`` opens the page the person
picked in Account settings (``users.home``); sign-in lands there and the logo links to it. Clients keep their
own home (the dashboard).
"""

from __future__ import annotations

from flask import Blueprint, abort, current_app, flash, g, redirect, render_template, request, url_for

from .. import first_run, home, month_end
from ..issued import period_label
from ..reports.monthly_review import setting
from .access import access_of, can, current, is_staff, login_required, needs, safe_next
from .filters import money
from .groups import group_aware, scoped_tenants

bp = Blueprint("home", __name__)

# What each home page needs besides a staff role; Today also shows only the reasons its reader may open.
NEEDS = {home.TODAY: ("clients.compare",), home.CLIENTS: ("clients.compare", "reports.tenant")}
REASON_NEEDS = {"findings": "reports.security", "collection": "collect", "access": "collect", "due": "reports.tenant"}


def open_to(user) -> list[str]:
    """The home pages this person may open, in menu order."""
    if user is None or user["role"] == "client":
        return []
    a = access_of(user)
    return [key for key, caps in NEEDS.items() if a.can_all(caps)]


def home_url(user) -> str:
    """Where this person's home page is: their chosen Today or Clients (or the other one, when their choice
    is closed to them), or the client dashboard."""
    pages = open_to(user)
    if not pages:
        return url_for("tenants.dashboard")
    chosen = home.home_of(user)
    return url_for(f"home.{chosen if chosen in pages else pages[0]}")


def _link(reason: home.Reason) -> str:
    """Where a reason on Today points: the client's page that shows or fixes it."""
    if reason.target == "findings":
        return url_for("reports.tenant_findings", tenant_id=reason.tenant_id)
    if reason.target == "collection":
        return url_for("tenants.collection", tenant_id=reason.tenant_id)
    if reason.target == "tenant":
        return url_for("tenants.tenant", tenant_id=reason.tenant_id)
    if reason.target == "upcoming":
        return url_for("upcoming.index")
    return reason.target or url_for("tenants.tenant", tenant_id=reason.tenant_id)


def get_started() -> list[first_run.Step] | None:
    """The Get started checklist for an admin until the first collection has run (first_run.py), with links."""
    if not can("settings.manage"):
        return None
    steps = first_run.pending(g.db)
    for step in steps or ():
        kind, _, tenant_id = step.target.partition(":")
        if kind == "connections":
            step.href = url_for("admin.connections")
        elif kind == "new_tenant":
            step.href = url_for("tenants.new_tenant")
        elif kind == "tenant":
            step.href = url_for("tenants.tenant", tenant_id=int(tenant_id))
        elif kind == "collection":
            step.href = url_for("tenants.collection", tenant_id=int(tenant_id))
    return steps


@bp.get("/home")
@login_required
def index():
    return redirect(home_url(g.user))


def _homes() -> dict:
    return {k: v for k, v in home.HOMES.items() if k in open_to(g.user)}


@bp.get("/today")
@needs("clients.compare", kind="staff")
@group_aware
def today():
    a = current()
    data = home.today(g.db, scoped_tenants(), shows=lambda kind, tenant_id: a.can(REASON_NEEDS[kind], tenant_id))
    for row in data.rows:
        for reason in row.reasons:
            reason.href = _link(reason)
    return render_template(
        "home_today.html", data=data, homes=_homes(), mine=home.home_of(g.user), get_started=get_started()
    )


@bp.get("/clients")
@needs("clients.compare", "reports.tenant", kind="staff")
@group_aware
def clients():
    cfg = current_app.extensions["officesentry.config"]
    period = month_end.current_period(cfg.timezone)
    currency = setting(g.db, "currency", "GBP")
    data = home.clients(g.db, scoped_tenants(), period, period_label(period), currency)
    for r in data.rows:
        if not can("projects.view", r.tenant["id"]):
            r.projects, r.proposed, r.note = 0, 0, "You can't see this client's projects."
    return render_template(
        "home_clients.html",
        data=data,
        homes=_homes(),
        mine=home.home_of(g.user),
        sentence=data.sentence(lambda v: money(v, currency)),
        states=month_end.STATES,
        get_started=get_started(),
    )


@bp.post("/account/home")
@login_required
def choose():
    """Make Today or Clients this person's home page (staff only; nothing else changes)."""
    if not is_staff():
        abort(403)
    choice = request.form.get("home", "")
    if choice not in open_to(g.user):
        abort(400)
    g.db.execute("UPDATE users SET home = ? WHERE id = ?", (choice, g.user["id"]))
    flash(f"{home.HOMES[choice][0]} is now your home page. It opens when you sign in and from the logo.", "success")
    return redirect(safe_next(request.form.get("next"), url_for("auth.account")))
