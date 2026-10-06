"""Month-end reporting pages: report plans, the review queue, the review screen and approval.

Admin and analyst review, comment, assign and curate recommendations and notes; only admins edit
plans and approve, send or re-issue. Clients never reach these pages; on their report they see the
commentary, recommendations and client-visible notes (``report_extras``). See ``month_end.py``.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from flask import Blueprint, abort, current_app, flash, g, redirect, render_template, request, url_for

from .. import delivery, month_end
from ..db import audit, transaction, utcnow
from ..reports import monthly_review, summary
from ..reports.data_reports import grouped as report_groups
from ..text import plural
from .access import roles_required, tenant_or_404, visible_tenants

bp = Blueprint("reporting", __name__)

STAFF = ("admin", "analyst")


def _cfg():
    return current_app.extensions["officesentry.config"]


def _vault():
    return current_app.extensions["officesentry.vault"]


def _period_now() -> str:
    return month_end.current_period(_cfg().timezone)


def _period_or_404(period: str) -> str:
    if not month_end.valid_period(period):
        abort(404)
    return period


def report_extras(tenant, *, page: bool) -> dict:
    """Template context for the monthly review page (``page``) or its PDF: this month's commentary,
    recommendations and notes. Staff-only notes only reach staff on the page; clients and PDFs never get them."""
    staff = g.user["role"] in STAFF
    period = _period_now()
    extras = month_end.report_context(g.db, tenant["id"], period, staff=staff and page)
    if staff and page:
        extras["review_href"] = url_for("reporting.review", period=period, tenant_id=tenant["id"])
    return extras


# -- Plans --------------------------------------------------------------------------------------


def _blank_plan() -> dict:
    return {"id": None, "name": "Monthly security report", "reports": ["review"], "include_xlsx": 1,
            "day_of_month": 1, "hour": 8, "timezone": "", "subject": month_end.DEFAULT_SUBJECT, "message": "",
            "mode": "approval", "delivery": "email", "bcc_team": 0}


def _plan_values() -> dict:
    f = request.form
    valid = {o.key for o in delivery.catalog()}
    values = {
        "name": f.get("name", "").strip()[:100],
        "reports": [k for k in dict.fromkeys(f.getlist("reports")) if k in valid],
        "include_xlsx": 1 if f.get("include_xlsx") else 0,
        "day_of_month": f.get("day_of_month", type=int, default=0),
        "hour": f.get("hour", type=int, default=-1),
        "timezone": f.get("timezone", "").strip(),
        "subject": f.get("subject", "").strip()[:200],
        "message": f.get("message", "").strip()[:1000],
        "mode": f.get("mode", ""),
        "delivery": f.get("delivery", ""),
        "bcc_team": 1 if f.get("bcc_team") else 0,
    }
    if not 1 <= values["day_of_month"] <= 28 or not 0 <= values["hour"] <= 23 \
            or values["mode"] not in month_end.MODES or values["delivery"] not in month_end.DELIVERIES:
        abort(400)
    if values["timezone"] and not delivery.valid_timezone(values["timezone"]):
        abort(400)
    return values


def _render_plan(values: dict, chosen: set[int]):
    cfg = _cfg()
    zones = list(dict.fromkeys([cfg.timezone, *delivery.COMMON_TIMEZONES] +
                               ([values["timezone"]] if values["timezone"] else [])))
    tenants = [t for t in visible_tenants(g.db, g.user)]
    on_plan = {r["tenant_id"]: r["name"] for r in g.db.execute(
        "SELECT pt.tenant_id, p.name FROM report_plan_tenants pt JOIN report_plans p ON p.id = pt.plan_id"
        " WHERE p.id IS NOT ?", (values["id"],))}
    contacts = {t["id"]: len(delivery.tenant_contacts(g.db, t["id"])) for t in tenants}
    return render_template(
        "report_plan_form.html", p=values, chosen=chosen, tenants=tenants, on_plan=on_plan, contacts=contacts,
        catalog=delivery.catalog(), report_groups=report_groups(), zones=zones, default_tz=cfg.timezone,
        ordinal=delivery.ordinal, modes=month_end.MODES, deliveries=month_end.DELIVERIES,
        pdf_available=delivery.export.pdf_available())


def _save_plan(existing=None):
    values = _plan_values()
    allowed = {t["id"] for t in visible_tenants(g.db, g.user)}
    chosen = [int(i) for i in request.form.getlist("tenants") if i.isdigit() and int(i) in allowed]
    errors = []
    if not values["name"]:
        errors.append("Give the plan a name.")
    if not values["reports"]:
        errors.append("Choose at least one report.")
    if errors:
        for e in errors:
            flash(e, "error")
        values["id"] = existing["id"] if existing else None
        return _render_plan(values, set(chosen))
    user = g.user["username"]
    with transaction(g.db):
        plan_id = month_end.save_plan(g.db, values, user, existing["id"] if existing else None)
        plan = month_end.get_plan(g.db, plan_id)
        dropped = [t for t in month_end.plan_tenant_ids(g.db, plan_id) if t not in chosen and t in allowed]
        for tenant_id in dropped:
            month_end.remove_from_plan(g.db, tenant_id)
        applied = month_end.apply_plan(g.db, plan, chosen, user, _cfg().timezone)
        audit(g.db, user, "report_plan_saved" if existing else "report_plan_created",
              detail=f"{values['name']}: {month_end.MODES[values['mode']].lower()}, applied to"
                     f" {plural(applied, 'tenant')}" + (f", {len(dropped)} removed" if dropped else ""))
    flash(f"Saved. {plural(applied, 'client')} on “{values['name']}”.", "success")
    return redirect(url_for("reporting.plans"))


@bp.get("/reporting/plans")
@roles_required("admin")
def plans():
    tenants = visible_tenants(g.db, g.user)
    return render_template("report_plans.html", plans=month_end.plans(g.db), modes=month_end.MODES,
                           deliveries=month_end.DELIVERIES, coverage=month_end.coverage(g.db, tenants),
                           describe=lambda p: f"Monthly on the {delivery.ordinal(p['day_of_month'])} at "
                                              f"{p['hour']:02d}:00 ({p['timezone'] or _cfg().timezone})",
                           titles=lambda p: delivery.report_titles(p["reports"]))


@bp.route("/reporting/plans/new", methods=["GET", "POST"])
@roles_required("admin")
def new_plan():
    if request.method == "POST":
        return _save_plan()
    chosen = {int(i) for i in request.args.getlist("tenant") if i.isdigit()}
    return _render_plan(_blank_plan(), chosen)


@bp.route("/reporting/plans/<int:plan_id>", methods=["GET", "POST"])
@roles_required("admin")
def edit_plan(plan_id: int):
    plan = month_end.get_plan(g.db, plan_id)
    if plan is None:
        abort(404)
    if request.method == "POST":
        return _save_plan(plan)
    values = dict(plan)
    values["reports"] = json.loads(plan["reports"] or "[]")
    return _render_plan(values, set(month_end.plan_tenant_ids(g.db, plan_id)))


@bp.post("/reporting/plans/<int:plan_id>/delete")
@roles_required("admin")
def delete_plan(plan_id: int):
    plan = month_end.get_plan(g.db, plan_id)
    if plan is None:
        abort(404)
    with transaction(g.db):
        month_end.delete_plan(g.db, plan_id)
        audit(g.db, g.user["username"], "report_plan_deleted", detail=plan["name"])
    flash(f"Removed “{plan['name']}” and its scheduled emails. Reports already sent are kept.", "success")
    return redirect(url_for("reporting.plans"))


# -- Queue ----------------------------------------------------------------------------------------


FILTERS = {"": "All", "mine": "Mine", "review": "Needs review", "ready": "Ready"}


@bp.get("/reporting")
@roles_required(*STAFF)
def index():
    return redirect(url_for("reporting.queue", period=_period_now()))


@bp.get("/reporting/<period>")
@roles_required(*STAFF)
def queue(period: str):
    period = _period_or_404(period)
    show = request.args.get("show", "")
    if show not in FILTERS:
        show = ""
    rows = month_end.queue(g.db, visible_tenants(g.db, g.user), period)
    counts = {"": len(rows), "mine": sum(r.reviewer == g.user["username"] for r in rows),
              "review": sum(r.state in ("draft", "in_review") for r in rows), "ready": sum(r.ready for r in rows)}
    shown = [r for r in rows if show == "" or (show == "mine" and r.reviewer == g.user["username"])
             or (show == "review" and r.state in ("draft", "in_review")) or (show == "ready" and r.ready)]
    return render_template(
        "reporting_queue.html", period=period, label=month_end.issued.period_label(period), rows=shown,
        all_rows=rows, show=show, filters=FILTERS, counts=counts, states=month_end.STATES,
        prev=month_end.shift_period(period, -1), next=month_end.shift_period(period, 1),
        current=_period_now(), admin=g.user["role"] == "admin")


@bp.post("/reporting/<period>/approve")
@roles_required("admin")
def approve_many(period: str):
    period = _period_or_404(period)
    visible = {t["id"]: t for t in visible_tenants(g.db, g.user)}
    ids = [int(i) for i in request.form.getlist("tenants") if i.isdigit() and int(i) in visible]
    sent, failed, skipped = 0, [], 0
    for tenant_id in ids:
        t = visible[tenant_id]
        if month_end.state_of(g.db, tenant_id, period) in ("approved", "sent") or not t["enabled"]:
            skipped += 1  # bulk approval never re-issues; that needs a reason, one client at a time
            continue
        error = _approve(t, period)
        if error:
            failed.append(f"{t['name']}: {error}")
        else:
            sent += 1
    if sent:
        flash(f"Approved and sent {plural(sent, 'report')}.", "success")
    if skipped:
        flash(f"Skipped {skipped}: already approved or sent, or the client is paused.", "warning")
    for f in failed:
        flash(f, "error")
    if not ids:
        flash("Tick the clients to approve first.", "error")
    return redirect(url_for("reporting.queue", period=period, show=request.form.get("show", "")))


def _approve(tenant, period: str, reason: str | None = None) -> str | None:
    """Approve, freeze and send one client's month. Returns an error to show, or None."""
    user = g.user["username"]
    try:
        # Checked inside the write lock, so a double click or two admins at once approve the month once.
        with transaction(g.db, immediate=True):
            if month_end.state_of(g.db, tenant["id"], period) in ("approved", "sent") and not reason:
                return "It's already been issued. Say why it's being issued again."
            month_end.freeze(current_app, g.db, tenant, period, user, reason=reason)
            audit(g.db, user, "report_reissued" if reason else "report_approved", tenant["id"],
                  f"{period}" + (f": {reason[:200]}" if reason else ""))
    except month_end.ApprovalError as exc:
        return str(exc)
    except Exception as exc:  # a report bug mustn't take the whole batch down
        current_app.logger.exception("Approving %s for %s failed", period, tenant["id"])
        return f"Couldn't build the report: {type(exc).__name__}"
    error = month_end.deliver(current_app, g.db, _vault(), tenant, period, user)
    audit(g.db, user, "report_send_failed" if error else "report_sent", tenant["id"], period)
    return error


# -- Review screen ---------------------------------------------------------------------------------


def _review_url(tenant_id: int, period: str, anchor: str = ""):
    return redirect(url_for("reporting.review", period=period, tenant_id=tenant_id) + (f"#{anchor}" if anchor else ""))


def _planned_tenant(tenant_id: int):
    t = tenant_or_404(tenant_id)
    plan, _ = month_end.plan_for(g.db, t["id"])
    return t, plan


@bp.get("/reporting/<period>/<int:tenant_id>")
@roles_required(*STAFF)
def review(period: str, tenant_id: int):
    period = _period_or_404(period)
    t, plan = _planned_tenant(tenant_id)
    report = monthly_review.build(g.db, t)
    r = month_end.review(g.db, t["id"], period)
    state = month_end.state_of(g.db, t["id"], period, r)
    severe, flags = month_end.flags_for(g.db, t, report, period, datetime.now(UTC))
    taken = {a["rule_key"] for a in month_end.actions(g.db, t["id"]) if a["rule_key"]}
    staff_users = [u["username"] for u in g.db.execute(
        "SELECT username FROM users WHERE role IN ('admin', 'analyst') AND disabled = 0"
        " ORDER BY username COLLATE NOCASE")]
    return render_template(
        "reporting_review.html", tenant=t, plan=plan, period=period, label=month_end.issued.period_label(period),
        report=report, verdict=summary.monthly(report).verdict, review=r, state=state, states=month_end.STATES,
        severe=severe, flags=flags, actions=month_end.actions(g.db, t["id"]), statuses=month_end.ACTION_STATUSES,
        suggestions=[s for s in month_end.suggestions(report) if s["rule_key"] not in taken],
        notes=month_end.notes(g.db, t["id"], staff=True), visibility=month_end.VISIBILITY,
        frozen=month_end.frozen_files(g.db, t["id"], period), staff_users=staff_users,
        emailed=month_end.emailed(plan, g.db, t["id"]) if plan else False,
        months=[month_end.shift_period(period, i) for i in range(0, 7)], admin=g.user["role"] == "admin",
        period_label=month_end.issued.period_label)


@bp.post("/reporting/<period>/<int:tenant_id>/commentary")
@roles_required(*STAFF)
def save_commentary(period: str, tenant_id: int):
    period = _period_or_404(period)
    t = tenant_or_404(tenant_id)
    text = request.form.get("commentary", "").replace("\r\n", "\n").strip()[:4000]
    with transaction(g.db):
        month_end.start_review(g.db, t["id"], period, g.user["username"])
        month_end.update_review(g.db, t["id"], period, g.user["username"], commentary=text)
        audit(g.db, g.user["username"], "report_commentary_saved", t["id"], f"{period}, {len(text)} characters")
    flash("Commentary saved.", "success")
    return _review_url(t["id"], period, "commentary")


@bp.post("/reporting/<period>/<int:tenant_id>/assign")
@roles_required(*STAFF)
def assign(period: str, tenant_id: int):
    period = _period_or_404(period)
    t = tenant_or_404(tenant_id)
    who = request.form.get("reviewer", "").strip()
    if who == "me":
        who = g.user["username"]
    if who and not g.db.execute("SELECT 1 FROM users WHERE username = ? AND role IN ('admin', 'analyst')",
                                (who,)).fetchone():
        abort(400)
    with transaction(g.db):
        month_end.update_review(g.db, t["id"], period, g.user["username"], reviewer=who or None)
        audit(g.db, g.user["username"], "report_reviewer_assigned", t["id"], f"{period}: {who or 'nobody'}")
    flash(f"Assigned to {who}." if who else "Unassigned.", "success")
    if request.form.get("from") == "queue":
        return redirect(url_for("reporting.queue", period=period, show=request.form.get("show", "")))
    return _review_url(t["id"], period)


@bp.post("/reporting/<period>/<int:tenant_id>/state")
@roles_required(*STAFF)
def set_state(period: str, tenant_id: int):
    """Staff move a month between Draft and In review; approval has its own (admin) route."""
    period = _period_or_404(period)
    t = tenant_or_404(tenant_id)
    state = request.form.get("state", "")
    if state not in ("draft", "in_review"):
        abort(400)
    if month_end.state_of(g.db, t["id"], period) not in ("draft", "in_review"):
        flash("It's already approved. Re-issue it to change what the client gets.", "error")
        return _review_url(t["id"], period)
    with transaction(g.db):
        month_end.update_review(g.db, t["id"], period, g.user["username"], state=state)
        audit(g.db, g.user["username"], "report_review_state", t["id"], f"{period}: {state}")
    return _review_url(t["id"], period)


@bp.post("/reporting/<period>/<int:tenant_id>/approve")
@roles_required("admin")
def approve(period: str, tenant_id: int):
    period = _period_or_404(period)
    t, plan = _planned_tenant(tenant_id)
    if plan is None:
        abort(404)
    reason = request.form.get("reason", "").strip()[:500] or None
    error = _approve(t, period, reason)
    if error:
        flash(error, "error")
    else:
        flash("Approved and sent." if month_end.emailed(plan, g.db, t["id"])
              else "Approved and published to the client's report history.", "success")
    return _review_url(t["id"], period)


@bp.post("/reporting/<period>/<int:tenant_id>/send")
@roles_required("admin")
def send_again(period: str, tenant_id: int):
    """Send the frozen files again after a failed send. Nothing is rebuilt."""
    period = _period_or_404(period)
    t = tenant_or_404(tenant_id)
    if month_end.state_of(g.db, t["id"], period) != "approved":
        abort(400)
    error = month_end.deliver(current_app, g.db, _vault(), t, period, g.user["username"])
    audit(g.db, g.user["username"], "report_send_failed" if error else "report_sent", t["id"], period)
    flash(f"Sending failed: {error}" if error else "Sent.", "error" if error else "success")
    return _review_url(t["id"], period)


# -- Recommendations and notes ----------------------------------------------------------------------


def _back_to_review(tenant_id: int, anchor: str):
    period = request.form.get("period", "")
    return _review_url(tenant_id, period if month_end.valid_period(period) else _period_now(), anchor)


def _target(value: str) -> str | None:
    value = (value or "").strip()
    if value and not month_end.valid_period(value):
        abort(400)
    return value or None


@bp.post("/tenants/<int:tenant_id>/report-actions")
@roles_required(*STAFF)
def add_action(tenant_id: int):
    t = tenant_or_404(tenant_id)
    title = request.form.get("title", "").strip()[:300]
    detail = request.form.get("detail", "").strip()[:1000]
    rule_key = request.form.get("rule_key", "").strip()[:100] or None
    if rule_key and not title:  # picked from the failing checks: their wording
        report = monthly_review.build(g.db, t)
        found = next((s for s in month_end.suggestions(report) if s["rule_key"] == rule_key), None)
        if found is None:
            abort(400)
        title, detail = found["title"], detail or found["detail"]
    if not title:
        flash("Write the recommendation first.", "error")
        return _back_to_review(t["id"], "actions")
    with transaction(g.db):
        month_end.add_action(g.db, t["id"], title, detail, g.user["username"], rule_key=rule_key,
                             target_month=_target(request.form.get("target_month", "")))
        audit(g.db, g.user["username"], "report_action_added", t["id"], rule_key or "custom")
    return _back_to_review(t["id"], "actions")


def _action_or_404(action_id: int):
    a = g.db.execute("SELECT * FROM report_actions WHERE id = ?", (action_id,)).fetchone()
    if a is None:
        abort(404)
    tenant_or_404(a["tenant_id"])
    return a


@bp.post("/report-actions/<int:action_id>")
@roles_required(*STAFF)
def update_action(action_id: int):
    a = _action_or_404(action_id)
    op = request.form.get("op", "save")
    with transaction(g.db):
        if op in ("up", "down"):
            month_end.move_action(g.db, a, -1 if op == "up" else 1)
        elif op == "delete":
            g.db.execute("DELETE FROM report_actions WHERE id = ?", (a["id"],))
        else:
            status = request.form.get("status", "")
            if status not in month_end.ACTION_STATUSES:
                abort(400)
            month_end.update_action(g.db, a, status, _target(request.form.get("target_month", "")))
        audit(g.db, g.user["username"], f"report_action_{'moved' if op in ('up', 'down') else op + 'd'}"
              if op != "save" else "report_action_updated", a["tenant_id"], str(a["id"]))
    return _back_to_review(a["tenant_id"], "actions")


@bp.post("/tenants/<int:tenant_id>/finding-notes")
@roles_required(*STAFF)
def add_note(tenant_id: int):
    t = tenant_or_404(tenant_id)
    rule_key = request.form.get("rule_key", "").strip()[:100]
    body = request.form.get("body", "").strip()[:2000]
    visibility = request.form.get("visibility", "")
    if not rule_key or visibility not in month_end.VISIBILITY:
        abort(400)
    if not body:
        flash("Write the note first.", "error")
        return _back_to_review(t["id"], f"check-{rule_key}")
    with transaction(g.db):
        g.db.execute("INSERT INTO finding_notes (tenant_id, rule_key, subject, body, visibility, created_by,"
                     " created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                     (t["id"], rule_key, request.form.get("subject", "").strip()[:300], body, visibility,
                      g.user["username"], utcnow()))
        # The note itself can name the client's people, so the log says only where and who can see it.
        audit(g.db, g.user["username"], "finding_note_added", t["id"], f"{rule_key} ({visibility})")
    return _back_to_review(t["id"], f"check-{rule_key}")


@bp.post("/finding-notes/<int:note_id>/delete")
@roles_required(*STAFF)
def delete_note(note_id: int):
    n = g.db.execute("SELECT * FROM finding_notes WHERE id = ?", (note_id,)).fetchone()
    if n is None:
        abort(404)
    tenant_or_404(n["tenant_id"])
    with transaction(g.db):
        g.db.execute("DELETE FROM finding_notes WHERE id = ?", (n["id"],))
        audit(g.db, g.user["username"], "finding_note_removed", n["tenant_id"], f"{n['rule_key']} ({n['visibility']})")
    return _back_to_review(n["tenant_id"], f"check-{n['rule_key']}")
