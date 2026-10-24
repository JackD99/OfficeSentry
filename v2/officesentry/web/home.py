"""Home pages by role (``home.py``): Today for technicians, Clients for account managers.

Staff only, over the tenants they see narrowed to their chosen group. ``/home`` opens the page the person
picked in Account settings (``users.home``); sign-in lands there and the logo links to it. Clients keep their
own home (the dashboard).

Someone who may annotate a client's findings (``findings.manage``) hands any of its lines on Today to a
colleague who sees that line there, or snoozes it out of the list for a while: a finding line through
``findings.hand`` and ``findings.snooze`` on each finding behind it, any other line through ``item_state``.
Nothing is written to a tenant, and the public demo's visitor never changes anything.
"""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from flask import Blueprint, abort, current_app, flash, g, redirect, render_template, request, url_for

from .. import findings, first_run, home, month_end
from ..db import audit, transaction, try_write
from ..formatting import parse
from ..formatting import strftime as datefmt
from ..issued import period_label
from ..reports.monthly_review import setting
from . import finding, public_demo
from .access import (
    access_of,
    can,
    can_see_tenant,
    current,
    is_staff,
    login_required,
    needs,
    require,
    safe_next,
    tenant_or_404,
)
from .filters import money
from .groups import group_aware, scoped_tenants

bp = Blueprint("home", __name__)

# What each home page needs besides a staff role; Today also shows only the lines its reader may open.
NEEDS = {home.TODAY: ("clients.compare",), home.CLIENTS: ("clients.compare", "reports.tenant")}
LINE_NEEDS = {
    "finding": "reports.security",
    "fixed": "reports.security",
    "collection": "collect",
    "access": "collect",
    "due": "reports.tenant",
}


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


def line_href(entry: home.Entry) -> str:
    """Where a line on Today (and in its emailed ledger, ``delivery.build_ledger_email``) points: the client's page
    that shows or fixes it."""
    tenant_id = entry.tenant["id"]
    if entry.target == "findings":
        return url_for("reports.tenant_findings", tenant_id=tenant_id) + entry.anchor
    if entry.target == "fixed":
        return url_for("reports.resolved_findings", tenant_id=tenant_id)
    if entry.target == "collection":
        return url_for("tenants.collection", tenant_id=tenant_id)
    if entry.target == "tenant":
        return url_for("tenants.tenant", tenant_id=tenant_id)
    if entry.target == "upcoming":
        return url_for("upcoming.index")
    return entry.target or url_for("tenants.tenant", tenant_id=tenant_id)


def _zone() -> ZoneInfo:
    try:
        return ZoneInfo(current_app.extensions["officesentry.config"].timezone)
    except (KeyError, ValueError):
        return ZoneInfo("UTC")


def stamp(when: datetime, now: datetime, zone: ZoneInfo) -> tuple[str, str]:
    """A line's time as the ledger shows it, in the MSP's timezone: the clock time today, the weekday this
    week, the date before that; and the whole date and time for its tooltip."""
    local, today = when.astimezone(zone), now.astimezone(zone).date()
    days = (today - local.date()).days
    short = local.strftime("%H:%M") if days <= 0 else (local.strftime("%a") if days < 7 else datefmt(local, "%-d %b"))
    return short, datefmt(local, "%a %-d %b %Y, %H:%M %Z")


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
    now = datetime.now(UTC)
    demo = public_demo.active(g.db)  # every visitor is one account: the line stays the last day for all
    since, label, keep = home.since_for({} if demo else g.user, now)
    data = home.today(
        g.db,
        scoped_tenants(),
        now,
        shows=lambda kind, tenant_id: a.can(LINE_NEEDS[kind], tenant_id),
        since=since,
        since_label=label,
    )
    if not demo:
        try_write(g.db, "UPDATE users SET today_opened_at = ?, today_since = ? WHERE id = ?", (*keep, g.user["id"]))
        for tenant_id, key in data.cleared:
            try_write(g.db, "DELETE FROM item_state WHERE tenant_id = ? AND item_key = ?", (tenant_id, key))
    zone = _zone()
    for entry in data.entries + data.snoozed:
        entry.href = line_href(entry)
        entry.stamp, entry.stamp_full = stamp(entry.when, now, zone)
        if entry.snoozed_until:
            entry.snooze_stamp = stamp(parse(entry.snoozed_until) or now, now, zone)[1]
    lines = [e for e in data.entries + data.snoozed if e.acts and can("findings.manage", e.tenant["id"])]
    return render_template(
        "home_today.html",
        data=data,
        since=stamp(data.since, now, zone)[1],
        homes=_homes(),
        mine=home.home_of(g.user),
        get_started=get_started(),
        holders=_holders(lines),
        snoozes=SNOOZES,
    )


# -- Handing a line on and snoozing it ------------------------------------------------------------

SNOOZES = {"tomorrow": "Until tomorrow morning", "week": "For a week"}
MORNING = time(6)  # a line snoozed until tomorrow comes back at this hour, in the MSP's timezone
KEY_LENGTH = 300


def _holders(lines) -> dict[tuple[int, str], list]:
    """For each client and kind of line the person can act on: the colleagues it can be handed to (those who
    see that client and that kind of line there), as ``finding.holders`` gives them."""
    staff = g.db.execute(
        "SELECT * FROM users WHERE role <> 'client' AND disabled = 0 ORDER BY username COLLATE NOCASE"
    ).fetchall()
    rights = [(u, access_of(u, g.db)) for u in staff]
    out = {}
    for tenant_id, kind in {(e.tenant["id"], e.kind) for e in lines}:
        out[(tenant_id, kind)] = [
            u for u, r in rights if can_see_tenant(g.db, u, tenant_id) and r.can(LINE_NEEDS[kind], tenant_id)
        ]
    return out


def snooze_until(choice: str, now: datetime, zone: ZoneInfo) -> str | None:
    """When a snooze picked on the page ends (ISO, UTC): tomorrow at MORNING in the MSP's timezone, or a week
    from now. Anything else wakes the line."""
    if choice == "tomorrow":
        day = now.astimezone(zone).date() + timedelta(days=1)
        return datetime.combine(day, MORNING, tzinfo=zone).astimezone(UTC).strftime(findings.ISO)
    if choice == "week":
        return (now + timedelta(days=7)).strftime(findings.ISO)
    return None


def _line(tenant_id: int) -> tuple[str, str, list[int]]:
    """The line the form names on this client: its kind, its key and, for a finding line, the open findings
    behind it (only this client's, of that check). The person must see that kind of line there."""
    key = request.form.get("key", "")[:KEY_LENGTH]
    kind, _, rest = key.partition(":")
    if kind not in LINE_NEEDS or kind == "fixed":
        abort(400)
    require(LINE_NEEDS[kind], tenant_id=tenant_id)
    if kind != "finding":
        return kind, key, []
    wanted = [int(i) for i in request.form.getlist("finding") if i.isdigit()]
    marks = ",".join("?" * len(wanted))
    ids = [
        r["id"]
        for r in g.db.execute(
            f"SELECT id FROM findings WHERE id IN ({marks}) AND tenant_id = ? AND rule_key = ? AND resolved_at IS NULL",
            (*wanted, tenant_id, rest),
        )
    ]
    if not ids:
        abort(404)
    return kind, key, ids


def _keep_item(tenant_id: int, key: str, column: str, values: tuple) -> None:
    """Set a non-finding line's hand-off or snooze (``column`` is handed or snoozed)."""
    cols = {"handed": ("handed_to", "handed_by", "handed_at"), "snoozed": ("snoozed_until", "snoozed_by", "snoozed_at")}
    names = cols[column]
    g.db.execute(
        f"INSERT INTO item_state (tenant_id, item_key, {', '.join(names)}) VALUES (?, ?, ?, ?, ?)"
        f" ON CONFLICT (tenant_id, item_key) DO UPDATE SET {', '.join(f'{n} = excluded.{n}' for n in names)}",
        (tenant_id, key, *values),
    )


def _back():
    return redirect(url_for("home.today") + "#ledger")


@bp.post("/tenants/<int:tenant_id>/today/hand")
@needs("findings.manage", kind="staff")
def hand_line(tenant_id: int):
    """Hand a line on Today to a colleague who sees it on this client, or back to nobody."""
    tenant_or_404(tenant_id)
    kind, key, ids = _line(tenant_id)
    to = request.form.get("to", "").strip()
    who = None
    if to:
        who = next((u for u in finding.holders(tenant_id, LINE_NEEDS[kind]) if str(u["id"]) == to), None)
        if who is None:
            flash("Pick someone who can see this on the client.", "error")
            return _back()
    by, now = g.user["username"], datetime.now(UTC).strftime(findings.ISO)
    with transaction(g.db):
        if ids:
            for finding_id in ids:
                findings.hand(g.db, finding_id, who["id"] if who else None, by)
        elif who:
            _keep_item(tenant_id, key, "handed", (who["id"], by, now))
        else:
            _keep_item(tenant_id, key, "handed", (None, None, None))
        what = f"{key} ({len(ids)} findings)" if ids else key
        audit(
            g.db,
            by,
            "finding_handed" if ids else "item_handed",
            tenant_id,
            f"{what} to {who['username'] if who else 'nobody'}",
        )
    flash(f"Handed to {who['username']}." if who else "Handed back: nobody holds it now.", "success")
    return _back()


@bp.post("/tenants/<int:tenant_id>/today/snooze")
@needs("findings.manage", kind="staff")
def snooze_line(tenant_id: int):
    """Keep a line out of Today's list until tomorrow morning or for a week, or bring it back now."""
    tenant_or_404(tenant_id)
    _, key, ids = _line(tenant_id)
    until = snooze_until(request.form.get("until", ""), datetime.now(UTC), _zone())
    by, now = g.user["username"], datetime.now(UTC).strftime(findings.ISO)
    with transaction(g.db):
        if ids:
            for finding_id in ids:
                findings.snooze(g.db, finding_id, until, by)
        else:
            _keep_item(tenant_id, key, "snoozed", (until, by, now) if until else (None, None, None))
        what = f"{key} ({len(ids)} findings)" if ids else key
        audit(g.db, by, "finding_snoozed" if ids else "item_snoozed", tenant_id, f"{what} until {until or 'now'}")
    flash("Snoozed: it's back in the list when the snooze ends." if until else "Back in the list.", "success")
    return _back()


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
