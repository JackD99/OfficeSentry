"""Home pages by role (``home.py``): Today for technicians, Clients for account managers.

Staff only, over the tenants they see narrowed to their chosen group. ``/home`` opens the page the person
picked in Account settings (``users.home``); sign-in lands there and the logo links to it. Clients keep their
own home (the dashboard).
"""

from __future__ import annotations

from flask import Blueprint, abort, current_app, flash, g, redirect, render_template, request, url_for

from .. import home, month_end
from ..issued import period_label
from ..reports.monthly_review import setting
from .access import login_required, roles_required, safe_next
from .filters import money
from .groups import group_aware, scoped_tenants

bp = Blueprint("home", __name__)

STAFF = ("admin", "analyst")


def home_url(user) -> str:
    """Where this person's home page is: their chosen Today or Clients, or the client dashboard."""
    if user is None or user["role"] not in STAFF:
        return url_for("tenants.dashboard")
    return url_for(f"home.{home.home_of(user)}")


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


@bp.get("/home")
@login_required
def index():
    return redirect(home_url(g.user))


@bp.get("/today")
@roles_required(*STAFF)
@group_aware
def today():
    data = home.today(g.db, scoped_tenants())
    for row in data.rows:
        for reason in row.reasons:
            reason.href = _link(reason)
    return render_template("home_today.html", data=data, homes=home.HOMES, mine=home.home_of(g.user))


@bp.get("/clients")
@roles_required(*STAFF)
@group_aware
def clients():
    cfg = current_app.extensions["officesentry.config"]
    period = month_end.current_period(cfg.timezone)
    currency = setting(g.db, "currency", "GBP")
    data = home.clients(g.db, scoped_tenants(), period, period_label(period), currency)
    return render_template("home_clients.html", data=data, homes=home.HOMES, mine=home.home_of(g.user),
                           sentence=data.sentence(lambda v: money(v, currency)), states=month_end.STATES)


@bp.post("/account/home")
@login_required
def choose():
    """Make Today or Clients this person's home page (staff only; nothing else changes)."""
    if g.user["role"] not in STAFF:
        abort(403)
    choice = request.form.get("home", "")
    if choice not in home.HOMES:
        abort(400)
    g.db.execute("UPDATE users SET home = ? WHERE id = ?", (choice, g.user["id"]))
    flash(f"{home.HOMES[choice][0]} is now your home page. It opens when you sign in and from the logo.", "success")
    return redirect(safe_next(request.form.get("next"), url_for("auth.account")))
