"""Emailed reports: schedules, contacts, alerts, the MSP digest, delivery history and mail settings."""

from __future__ import annotations

import dataclasses
import json
from datetime import UTC, datetime, timedelta

from flask import Blueprint, Response, abort, current_app, flash, g, redirect, render_template, request, url_for

from .. import alerts, clouds, delivery, issued, mailer, month_end, store, tenant_groups
from ..certs import cert_der, generate_certificate
from ..db import audit, transaction, utcnow
from ..mailer import Email, MailError
from ..reports.data_reports import grouped as report_groups
from ..text import plural
from .access import login_required, roles_required, tenant_or_404, visible_tenants
from .filters import _strftime as datefmt

bp = Blueprint("email", __name__)

STAFF = ("admin", "analyst")
ISO = "%Y-%m-%dT%H:%M:%SZ"
# The rendered email is shown in a sandboxed frame: inline styles only, nothing else.
EMAIL_CSP = "default-src 'none'; style-src 'unsafe-inline'; img-src data:; frame-ancestors 'self'"


def _cfg():
    return current_app.extensions["officesentry.config"]


def _vault():
    return current_app.extensions["officesentry.vault"]


def _is_staff() -> bool:
    return g.user["role"] in STAFF


def _require_staff() -> None:
    if not _is_staff():
        abort(403)


def _settings() -> dict:
    return mailer.mail_settings(g.db)


# -- Loading things the user is allowed to touch ----------------------------------


def _schedule_or_404(schedule_id: int):
    s = g.db.execute("SELECT * FROM email_schedules WHERE id = ?", (schedule_id,)).fetchone()
    if s is None:
        abort(404)
    if s["kind"] == "digest":
        if g.user["role"] != "admin":
            abort(404)
    else:
        tenant_or_404(s["tenant_id"])
    return s


def _alert_or_404(alert_id: int):
    a = g.db.execute("SELECT * FROM email_alerts WHERE id = ?", (alert_id,)).fetchone()
    if a is None:
        abort(404)
    if a["tenant_id"] is None:
        if g.user["role"] != "admin":
            abort(404)
    else:
        tenant_or_404(a["tenant_id"])
    return a


def _back(tenant_id: int | None):
    return redirect(url_for("email.tenant_emails", tenant_id=tenant_id) if tenant_id else url_for("email.overview"))


def _decorate(s) -> dict:
    """A schedule row plus what the pages show about it."""
    d = dict(s)
    d["recipients"] = delivery.recipients(g.db, "schedule", s["id"])
    d["describe"] = delivery.describe(s)
    d["tz"] = delivery.schedule_tz(s, _cfg().timezone)
    # A plan schedule that waits for approval never sends on its timer (month_end.approve sends it).
    d["next"] = None if s["on_approval"] else delivery.next_send(s, _cfg().timezone)
    d["titles"] = delivery.schedule_titles(s, g.db)
    d["upcoming"] = delivery.is_upcoming(s)
    d["views"] = delivery.is_views(s)
    if d["kind"] == "digest" and not d["upcoming"]:
        group = tenant_groups.get(g.db, s["group_id"])
        what = " + ".join(d["titles"]) if d["views"] else "Tenant digest"
        d["titles"] = [f"{what} · {group['name']}" if group else f"{what} · every tenant"]
    d["due_now"] = d["next"] is not None and d["next"] <= datetime.now(UTC)
    return d


def _decorate_alert(a) -> dict | None:
    if a is None:
        return None
    d = dict(a)
    d["recipients"] = delivery.recipients(g.db, "alert", a["id"])
    return d


def _last_errors(schedule_ids: list[int]) -> dict[int, dict]:
    if not schedule_ids:
        return {}
    rows = g.db.execute(
        f"SELECT * FROM email_deliveries WHERE id IN (SELECT MAX(id) FROM email_deliveries"
        f" WHERE schedule_id IN ({','.join('?' * len(schedule_ids))}) GROUP BY schedule_id)", schedule_ids)
    return {r["schedule_id"]: r for r in rows}


# -- Tenant page ---------------------------------------------------------------------


@bp.get("/tenants/<int:tenant_id>/schedules")
@login_required
def tenant_emails(tenant_id: int):
    t = tenant_or_404(tenant_id)
    staff = _is_staff()
    schedules = [_decorate(s) for s in g.db.execute(
        "SELECT * FROM email_schedules WHERE tenant_id = ? ORDER BY id", (t["id"],))]
    last = _last_errors([s["id"] for s in schedules])
    deliveries = g.db.execute(
        "SELECT * FROM email_deliveries WHERE tenant_id = ? ORDER BY id DESC LIMIT 25", (t["id"],)).fetchall()
    alert = _decorate_alert(g.db.execute("SELECT * FROM email_alerts WHERE tenant_id = ?", (t["id"],)).fetchone())
    upcoming = sorted((s for s in schedules if s["next"] and s["recipients"]), key=lambda s: s["next"])
    failing = [s for s in schedules if s["last_status"] == "failed"]
    settings = _settings()
    return render_template(
        "schedules.html", tenant=t, staff=staff, schedules=schedules, last=last, deliveries=deliveries,
        alert=alert, contacts=delivery.tenant_contacts(g.db, t["id"]), team=delivery.team_contacts(g.db),
        upcoming=upcoming[0] if upcoming else None, failing=failing, mail_ready=mailer.is_configured(settings),
        sent_30=g.db.execute("SELECT COUNT(*) FROM email_deliveries WHERE tenant_id = ? AND status = 'sent'"
                             " AND created_at >= ?", (t["id"], _days_ago(30))).fetchone()[0])


@bp.get("/tenants/<int:tenant_id>/report-history")
@login_required
def report_history(tenant_id: int):
    """Every report file emailed for this tenant, as it was sent. Clients don't see who it went to."""
    t = tenant_or_404(tenant_id)
    return render_template("report_history.html", tenant=t, staff=_is_staff(), sends=issued.history(g.db, t["id"]))


@bp.get("/tenants/<int:tenant_id>/report-history/<int:report_id>")
@login_required
def issued_report(tenant_id: int, report_id: int):
    t = tenant_or_404(tenant_id)
    r = issued.get(g.db, t["id"], report_id)
    if r is None:
        abort(404)
    audit(g.db, g.user["username"], "report_downloaded", t["id"], f"issued:{r['filename']}")
    return Response(bytes(r["data"]), mimetype=r["content_type"],
                    headers={"Content-Disposition": f'attachment; filename="{r["filename"]}"'})


def _days_ago(days: int) -> str:
    return (datetime.now(UTC) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


# -- Schedule form (report bundles and digests) ------------------------------------------


def _blank(kind: str, reports=None) -> dict:
    return {"id": None, "kind": kind, "reports": reports or (["review"] if kind == "report" else []), "include_xlsx": 1,
            "frequency": "monthly", "day_of_week": 0, "day_of_month": 1, "hour": 8, "timezone": "",
            "message": "", "enabled": 1, "group_id": None}


def _form_values(kind: str) -> dict:
    f = request.form
    valid = {o.key for o in delivery.catalog()}
    values = {
        "kind": kind,
        "reports": [k for k in dict.fromkeys(f.getlist("reports")) if k in valid] if kind == "report" else [],
        "include_xlsx": 1 if f.get("include_xlsx") else 0,
        "frequency": f.get("frequency", ""),
        "day_of_week": f.get("day_of_week", type=int, default=0),
        "day_of_month": f.get("day_of_month", type=int, default=1),
        "hour": f.get("hour", type=int, default=-1),
        "timezone": f.get("timezone", "").strip(),
        "message": f.get("message", "").strip()[:1000],
        "enabled": 1 if f.get("enabled", "1") == "1" else 0,
        # A digest can cover one tenant group (tenant_groups.py); report emails are one tenant's already.
        "group_id": f.get("group_id", type=int) if kind == "digest" else None,
    }
    if values["group_id"] is not None and tenant_groups.get(g.db, values["group_id"]) is None:
        abort(400)
    if values["frequency"] not in delivery.FREQUENCIES or not 0 <= values["day_of_week"] <= 6 \
            or not 1 <= values["day_of_month"] <= 28 or not 0 <= values["hour"] <= 23:
        abort(400)
    if values["timezone"] and not delivery.valid_timezone(values["timezone"]):
        abort(400)
    return values


def _chosen_contacts(tenant_id: int | None) -> tuple[list[int], list[str]]:
    """Contact ids ticked on the form, plus a new one typed in. Only the tenant's and the team's count."""
    errors = []
    allowed = {c["id"] for c in delivery.team_contacts(g.db)}
    if tenant_id:
        allowed |= {c["id"] for c in delivery.tenant_contacts(g.db, tenant_id)}
    ids = [int(i) for i in request.form.getlist("contacts") if i.isdigit() and int(i) in allowed]
    email = request.form.get("new_email", "").strip()
    if email:
        if not mailer.valid_email(email):
            errors.append(f"“{email}” isn't a valid email address.")
        elif tenant_id is None and g.user["role"] != "admin":
            abort(403)
        else:
            ids.append(delivery.add_contact(g.db, tenant_id, request.form.get("new_name", "").strip()[:100], email))
    return list(dict.fromkeys(ids)), errors


def _my_views(across: bool) -> dict[int, dict]:
    """The signed-in person's saved views that a schedule can send, by id."""
    rows = g.db.execute("SELECT * FROM saved_views WHERE user_id = ? ORDER BY name COLLATE NOCASE",
                        (g.user["id"],)).fetchall()
    return {r["id"]: dict(r) for r in rows if delivery.can_schedule_view(r["report_key"], across)}


def _view_value(v: delivery.ScheduledView, mine: dict) -> str:
    """The form value of a view on a schedule: "s<saved view id>" for one of the person's own saved views,
    "k<id>" to keep the schedule's copy (someone else's view, or one since deleted)."""
    return f"s{v.saved_view_id}" if v.saved_view_id in mine else f"k{v.id}"


def _view_options(schedule_id: int | None, across: bool) -> list[dict]:
    """The saved views the form offers, by report: the person's own, then copies the schedule already sends."""
    mine = _my_views(across)
    options = [{"value": f"s{r['id']}", "name": r["name"], "report": delivery.view_report_title(r["report_key"]),
                "note": ""} for r in mine.values()]
    for v in delivery.schedule_views(g.db, schedule_id):
        if _view_value(v, mine).startswith("k"):
            options.append({"value": f"k{v.id}", "name": v.name, "report": v.report_title,
                            "note": "saved by someone else" if v.saved_view_id else "its saved view was deleted"})
    return sorted(options, key=lambda o: (o["report"].lower(), o["name"].lower()))


def _chosen_views(schedule_id: int | None, across: bool) -> list[delivery.ScheduledView]:
    """The saved views ticked on the form, each copied with its format. Only the person's own saved views,
    or copies the schedule already had, count."""
    mine = _my_views(across)
    kept = {v.id: v for v in delivery.schedule_views(g.db, schedule_id)}
    out = []
    for value in dict.fromkeys(request.form.getlist("views")):
        fmt = request.form.get(f"view_format_{value}", "pdf")
        if fmt not in delivery.VIEW_FORMATS:
            abort(400)
        kind, raw = value[:1], value[1:]
        if not raw.isdigit():
            continue
        if kind == "s" and int(raw) in mine:
            r = mine[int(raw)]
            out.append(delivery.ScheduledView(r["report_key"], r["name"], r["query"], fmt, r["id"]))
        elif kind == "k" and int(raw) in kept:
            out.append(dataclasses.replace(kept[int(raw)], format=fmt))
    return out


def _render_form(tenant, values: dict, chosen: set[int], errors=()):
    for e in errors:
        flash(e, "error")
    cfg = _cfg()
    across = values["kind"] == "digest"
    sends_views = not across or delivery.VIEWS_KEY in values["reports"]
    mine = _my_views(across)
    view_values = {_view_value(v, mine): v.format for v in values.get("views", [])}
    zones = list(dict.fromkeys([cfg.timezone, *delivery.COMMON_TIMEZONES] +
                               ([values["timezone"]] if values["timezone"] else [])))
    return render_template(
        "schedule_form.html", tenant=tenant, s=values, chosen=chosen, catalog=delivery.catalog(),
        report_groups=report_groups(),
        contacts=delivery.tenant_contacts(g.db, tenant["id"]) if tenant else [], team=delivery.team_contacts(g.db),
        frequencies=delivery.FREQUENCIES, days=delivery.DAYS, zones=zones, default_tz=cfg.timezone,
        ordinal=delivery.ordinal, pdf_available=delivery.export.pdf_available(),
        recipients_limit=mailer.MAX_RECIPIENTS, groups=tenant_groups.all_groups(g.db) if not tenant else [],
        view_options=_view_options(values.get("id"), across) if sends_views else [],
        view_values=view_values, view_formats=delivery.VIEW_FORMATS)


def _save_schedule(tenant, existing=None, coming_up: bool = False, saved_views: bool = False):
    kind = existing["kind"] if existing else ("report" if tenant else "digest")
    tenant_id = tenant["id"] if tenant else None
    values = _form_values(kind)
    if coming_up or (existing is not None and delivery.is_upcoming(existing)):
        values["reports"] = [delivery.upcoming.KEY]  # a digest schedule that sends the Coming up email
        values["group_id"] = None  # Coming up always covers every tenant
    views_email = saved_views or (existing is not None and delivery.is_views(existing))
    if views_email:
        values["reports"] = [delivery.VIEWS_KEY]  # a digest schedule that sends saved views across tenants
    sends_views = kind == "report" or views_email
    values["views"] = _chosen_views(existing["id"] if existing else None, kind == "digest") if sends_views else []
    with transaction(g.db):
        ids, errors = _chosen_contacts(tenant_id)
        if kind == "digest":
            team_ids = {c["id"] for c in delivery.team_contacts(g.db)}
            ids = [i for i in ids if i in team_ids]
        if kind == "report" and not values["reports"] and not values["views"]:
            errors.append("Choose at least one report or saved view to send.")
        if views_email and not values["views"]:
            errors.append("Choose at least one saved view to send.")
        if not ids:
            errors.append("Choose at least one person to send it to.")
        if len(ids) > mailer.MAX_RECIPIENTS:
            errors.append(f"An email can go to at most {mailer.MAX_RECIPIENTS} people.")
        if errors:
            # Keep a contact typed in this attempt, so it is ticked when the form comes back.
            values["id"] = existing["id"] if existing else None
            return _render_form(tenant, values, set(ids), errors)

        tz = values["timezone"] or _cfg().timezone
        timing = ("frequency", "day_of_week", "day_of_month", "hour", "timezone")
        now = datetime.now(UTC)
        fresh_period = delivery.initial_period(values["frequency"], values["day_of_week"], values["day_of_month"],
                                               values["hour"], tz, now)
        cols = ("reports", "include_xlsx", "message", "enabled", "group_id", *timing)
        params = [json.dumps(values["reports"]), values["include_xlsx"], values["message"], values["enabled"],
                  values["group_id"], *(values[k] for k in timing)]
        if existing is None:
            schedule_id = g.db.execute(
                f"INSERT INTO email_schedules (kind, tenant_id, {', '.join(cols)}, last_period, created_by,"
                f" created_at) VALUES (?, ?, {', '.join('?' * len(cols))}, ?, ?, ?)",
                (kind, tenant_id, *params, fresh_period, g.user["username"], utcnow())).lastrowid
            action = "email_schedule_created"
        else:
            schedule_id = existing["id"]
            g.db.execute(f"UPDATE email_schedules SET {', '.join(c + ' = ?' for c in cols)} WHERE id = ?",
                         (*params, schedule_id))
            if any(existing[k] != values[k] for k in timing):
                g.db.execute("UPDATE email_schedules SET last_period = ?, attempts = 0, retry_at = NULL WHERE id = ?",
                             (fresh_period, schedule_id))
            action = "email_schedule_updated"
        delivery.set_recipients(g.db, "schedule", schedule_id, ids)
        if sends_views:
            delivery.set_schedule_views(g.db, schedule_id, values["views"])
        what = "coming up" if delivery.upcoming.KEY in values["reports"] else "saved views" if views_email else kind
        views_note = f" with {plural(len(values['views']), 'saved view')}" if values["views"] and not views_email \
            else ""
        audit(g.db, g.user["username"], action, tenant_id,
              f"{what} {values['frequency']}{views_note} to {plural(len(ids), 'recipient')}")
    s = g.db.execute("SELECT * FROM email_schedules WHERE id = ?", (schedule_id,)).fetchone()
    nxt = delivery.next_send(s, _cfg().timezone)
    flash("Saved. " + (f"Next email: {datefmt(nxt, '%a %-d %b, %H:%M')} ({nxt.tzname()})." if nxt
                       else "It's paused, so nothing will be sent."), "success")
    return _back(tenant_id)


@bp.route("/tenants/<int:tenant_id>/schedules/new", methods=["GET", "POST"])
@roles_required(*STAFF)
def new_schedule(tenant_id: int):
    t = tenant_or_404(tenant_id)
    if request.method == "POST":
        return _save_schedule(t)
    team_default = {c["id"] for c in delivery.tenant_contacts(g.db, t["id"])}
    values = _blank("report")
    # "Email this view" from a report's Views menu: start with that view, as a PDF, and no other report.
    picked = _my_views(False).get(request.args.get("view", type=int) or 0)
    if picked:
        values["reports"] = []
        values["views"] = [delivery.ScheduledView(picked["report_key"], picked["name"], picked["query"], "pdf",
                                                  picked["id"])]
    return _render_form(t, values, team_default)


@bp.route("/emails/digests/new", methods=["GET", "POST"])
@roles_required("admin")
def new_digest():
    if request.method == "POST":
        return _save_schedule(None)
    values = _blank("digest")
    values["frequency"] = "weekly"
    return _render_form(None, values, {c["id"] for c in delivery.team_contacts(g.db)})


@bp.route("/emails/coming-up/new", methods=["GET", "POST"])
@roles_required("admin")
def new_upcoming():
    """The weekly Coming up email to the team: what falls due in the next 90 days (``upcoming.py``)."""
    if request.method == "POST":
        return _save_schedule(None, coming_up=True)
    values = _blank("digest", [delivery.upcoming.KEY])
    values["frequency"] = "weekly"
    return _render_form(None, values, {c["id"] for c in delivery.team_contacts(g.db)})


@bp.route("/emails/views/new", methods=["GET", "POST"])
@roles_required("admin")
def new_views():
    """Saved views across every tenant, or one group, for the team (``delivery.build_views_email``)."""
    if request.method == "POST":
        return _save_schedule(None, saved_views=True)
    values = _blank("digest", [delivery.VIEWS_KEY])
    picked = _my_views(True).get(request.args.get("view", type=int) or 0)
    if picked:
        values["views"] = [delivery.ScheduledView(picked["report_key"], picked["name"], picked["query"], "csv",
                                                  picked["id"])]
    return _render_form(None, values, {c["id"] for c in delivery.team_contacts(g.db)})


@bp.route("/schedules/<int:schedule_id>/edit", methods=["GET", "POST"])
@roles_required(*STAFF)
def edit_schedule(schedule_id: int):
    s = _schedule_or_404(schedule_id)
    tenant = tenant_or_404(s["tenant_id"]) if s["tenant_id"] else None
    if request.method == "POST":
        return _save_schedule(tenant, s)
    values = dict(s)
    values["reports"] = json.loads(s["reports"] or "[]")
    values["views"] = delivery.schedule_views(g.db, s["id"])
    chosen = {c["id"] for c in delivery.recipients(g.db, "schedule", s["id"])}
    return _render_form(tenant, values, chosen)


@bp.post("/schedules/<int:schedule_id>/delete")
@roles_required(*STAFF)
def delete_schedule(schedule_id: int):
    s = _schedule_or_404(schedule_id)
    g.db.execute("DELETE FROM email_schedules WHERE id = ?", (s["id"],))
    audit(g.db, g.user["username"], "email_schedule_deleted", s["tenant_id"], s["kind"])
    flash("Scheduled email removed.", "success")
    return _back(s["tenant_id"])


@bp.post("/schedules/<int:schedule_id>/toggle")
@roles_required(*STAFF)
def toggle_schedule(schedule_id: int):
    s = _schedule_or_404(schedule_id)
    enabled = 0 if s["enabled"] else 1
    g.db.execute("UPDATE email_schedules SET enabled = ? WHERE id = ?", (enabled, s["id"]))
    if enabled:  # resuming never sends a period that was missed while paused
        tz = delivery.schedule_tz(s, _cfg().timezone)
        period = delivery.initial_period(s["frequency"], s["day_of_week"], s["day_of_month"], s["hour"], tz,
                                         datetime.now(UTC))
        if period:
            g.db.execute("UPDATE email_schedules SET last_period = ?, attempts = 0, retry_at = NULL WHERE id = ?",
                         (period, s["id"]))
    audit(g.db, g.user["username"], "email_schedule_resumed" if enabled else "email_schedule_paused",
          s["tenant_id"], s["kind"])
    flash("Resumed." if enabled else "Paused. Nothing will be sent until you resume it.", "success")
    return _back(s["tenant_id"])


@bp.post("/schedules/<int:schedule_id>/send")
@roles_required(*STAFF)
def send_now(schedule_id: int):
    s = _schedule_or_404(schedule_id)
    if s["on_approval"]:
        flash("This report goes out when its month-end review is approved.", "error")
        return redirect(url_for("reporting.review", period=month_end.current_period(_cfg().timezone),
                                tenant_id=s["tenant_id"]))
    if not delivery.recipients(g.db, "schedule", s["id"]):
        flash("Add someone to send it to first.", "error")
        return _back(s["tenant_id"])
    error = delivery.send_schedule(current_app, g.db, _vault(), s, trigger="manual", user=g.user["username"])
    audit(g.db, g.user["username"], "email_sent", s["tenant_id"], s["kind"])
    flash(f"Sending failed: {error}" if error else "Sent.", "error" if error else "success")
    if request.form.get("from") == "preview":
        return redirect(url_for("email.preview_schedule", schedule_id=s["id"]))
    return _back(s["tenant_id"])


# -- Preview ----------------------------------------------------------------------------


def _build_preview(s):
    contacts = delivery.recipients(g.db, "schedule", s["id"])
    if s["kind"] == "report":
        tenant = tenant_or_404(s["tenant_id"])
        return delivery.build_report_email(current_app, g.db, s, tenant, contacts, attach=False)
    return delivery.digest_builder(s)(current_app, g.db, s, contacts, attach=False)


@bp.get("/schedules/<int:schedule_id>/preview")
@login_required
def preview_schedule(schedule_id: int):
    _require_staff()
    s = _schedule_or_404(schedule_id)
    built = _build_preview(s)
    tenant = tenant_or_404(s["tenant_id"]) if s["tenant_id"] else None
    return render_template("email_preview.html", tenant=tenant, s=_decorate(s), email=built.email,
                           attachments=built.attachment_names, sender=mailer.sender_label(_settings()),
                           body_url=url_for("email.preview_schedule_body", schedule_id=s["id"]))


def _email_body(html: str):
    g.csp_override = EMAIL_CSP
    return current_app.response_class(html, mimetype="text/html")


@bp.get("/schedules/<int:schedule_id>/preview/body")
@login_required
def preview_schedule_body(schedule_id: int):
    _require_staff()
    return _email_body(_build_preview(_schedule_or_404(schedule_id)).email.html_for_browser())


def _alert_example(a):
    """Open findings at the alert's level, as an example of what an alert looks like.
    An alert for every tenant uses the tenant with the most recent such finding."""
    levels = delivery.ALERT_LEVELS[a["min_severity"]]
    marks = ",".join("?" * len(levels))
    tenant_id = a["tenant_id"] or (g.db.execute(
        f"SELECT tenant_id FROM findings WHERE resolved_at IS NULL AND severity IN ({marks})"
        " ORDER BY first_seen DESC LIMIT 1", levels).fetchone() or [None])[0]
    if tenant_id is None:
        return None
    ids = [r[0] for r in g.db.execute(
        f"SELECT id FROM findings WHERE tenant_id = ? AND resolved_at IS NULL AND severity IN ({marks})"
        " ORDER BY first_seen DESC LIMIT 20", (tenant_id, *levels))]
    items = alerts.opened_items(g.db, tenant_id, ids, a["min_severity"])[:5]
    if not items:
        return None
    return delivery.build_alert_email(current_app, g.db, a, store.get_tenant(g.db, tenant_id), items,
                                      delivery.recipients(g.db, "alert", a["id"]), preview=True)


@bp.get("/alerts/<int:alert_id>/preview")
@login_required
def preview_alert(alert_id: int):
    _require_staff()
    a = _alert_or_404(alert_id)
    built = _alert_example(a)
    tenant = tenant_or_404(a["tenant_id"]) if a["tenant_id"] else None
    return render_template("email_preview.html", tenant=tenant, s=None, alert=_decorate_alert(a),
                           email=built.email if built else None, attachments=[],
                           sender=mailer.sender_label(_settings()),
                           body_url=url_for("email.preview_alert_body", alert_id=a["id"]))


@bp.get("/alerts/<int:alert_id>/preview/body")
@login_required
def preview_alert_body(alert_id: int):
    _require_staff()
    built = _alert_example(_alert_or_404(alert_id))
    if built is None:
        abort(404)
    return _email_body(built.email.html_for_browser())


# -- Contacts --------------------------------------------------------------------------------


def _add_contact(tenant_id: int | None):
    name = request.form.get("name", "").strip()[:100]
    email = request.form.get("email", "").strip()
    if not mailer.valid_email(email):
        flash("Enter a valid email address.", "error")
        return
    contact_id = delivery.add_contact(g.db, tenant_id, name, email)
    if tenant_id is not None:
        month_end.contact_added(g.db, tenant_id, contact_id)
    audit(g.db, g.user["username"], "contact_added", tenant_id, email)
    flash(f"Added {name or email}.", "success")


@bp.post("/tenants/<int:tenant_id>/contacts")
@roles_required(*STAFF)
def add_tenant_contact(tenant_id: int):
    t = tenant_or_404(tenant_id)
    _add_contact(t["id"])
    return _back(t["id"])


@bp.post("/emails/team")
@roles_required("admin")
def add_team_contact():
    _add_contact(None)
    return _back(None)


@bp.post("/contacts/<int:contact_id>/delete")
@roles_required(*STAFF)
def delete_contact(contact_id: int):
    c = g.db.execute("SELECT * FROM contacts WHERE id = ?", (contact_id,)).fetchone()
    if c is None:
        abort(404)
    if c["tenant_id"] is None:
        if g.user["role"] != "admin":
            abort(403)
    else:
        tenant_or_404(c["tenant_id"])
    g.db.execute("DELETE FROM contacts WHERE id = ?", (c["id"],))
    audit(g.db, g.user["username"], "contact_removed", c["tenant_id"], c["email"])
    flash(f"Removed {c['name'] or c['email']} from every email.", "success")
    return _back(c["tenant_id"]) if request.form.get("from") != "overview" else _back(None)


# -- Alerts ----------------------------------------------------------------------------------


def _save_alert(tenant_id: int | None):
    severity = request.form.get("min_severity", "critical")
    if severity not in delivery.ALERT_LEVELS:
        abort(400)
    with transaction(g.db):
        ids, errors = _chosen_contacts(tenant_id)
        if not ids:
            errors.append("Choose at least one person to alert.")
        for e in errors:
            flash(e, "error")
        if errors:
            return _back(tenant_id)
        existing = g.db.execute("SELECT * FROM email_alerts WHERE IFNULL(tenant_id, 0) = ?",
                                (tenant_id or 0,)).fetchone()
        if existing:
            alert_id = existing["id"]
            g.db.execute("UPDATE email_alerts SET min_severity = ?, enabled = 1 WHERE id = ?", (severity, alert_id))
        else:
            alert_id = g.db.execute(
                "INSERT INTO email_alerts (tenant_id, min_severity, created_by, created_at)"
                " VALUES (?, ?, ?, ?)", (tenant_id, severity, g.user["username"], utcnow())).lastrowid
        delivery.set_recipients(g.db, "alert", alert_id, ids)
        audit(g.db, g.user["username"], "email_alert_saved", tenant_id,
              f"{severity} to {plural(len(ids), 'recipient')}")
    flash("Alerts are on. You'll get an email when a scan finds something new.", "success")
    return _back(tenant_id)


@bp.post("/tenants/<int:tenant_id>/alert")
@roles_required(*STAFF)
def save_tenant_alert(tenant_id: int):
    return _save_alert(tenant_or_404(tenant_id)["id"])


@bp.post("/emails/alert")
@roles_required("admin")
def save_global_alert():
    return _save_alert(None)


@bp.post("/alerts/<int:alert_id>/delete")
@roles_required(*STAFF)
def delete_alert(alert_id: int):
    a = _alert_or_404(alert_id)
    g.db.execute("DELETE FROM email_alerts WHERE id = ?", (a["id"],))
    audit(g.db, g.user["username"], "email_alert_removed", a["tenant_id"])
    flash("Alerts are off.", "success")
    return _back(a["tenant_id"])


# -- History ---------------------------------------------------------------------------------


@bp.post("/deliveries/<int:delivery_id>/retry")
@roles_required(*STAFF)
def retry(delivery_id: int):
    d = g.db.execute("SELECT * FROM email_deliveries WHERE id = ?", (delivery_id,)).fetchone()
    if d is None:
        abort(404)
    if d["tenant_id"] is not None:
        tenant_or_404(d["tenant_id"])
    elif g.user["role"] != "admin":
        abort(404)
    error = delivery.retry_delivery(current_app, g.db, _vault(), d, g.user["username"])
    audit(g.db, g.user["username"], "email_retried", d["tenant_id"], d["subject"])
    flash(f"Sending failed again: {error}" if error else "Sent.", "error" if error else "success")
    return _back(d["tenant_id"]) if request.form.get("from") != "overview" else _back(None)


# -- Overview across tenants ----------------------------------------------------------------


@bp.get("/emails")
@roles_required(*STAFF)
def overview():
    admin = g.user["role"] == "admin"
    tenants = {t["id"]: t for t in visible_tenants(g.db, g.user)}
    ids = list(tenants)
    marks = ",".join("?" * len(ids)) or "NULL"
    rows = g.db.execute(f"SELECT * FROM email_schedules WHERE tenant_id IN ({marks}) ORDER BY id", ids).fetchall()
    schedules = [_decorate(s) for s in rows]
    schedules.sort(key=lambda s: (s["next"] is None, s["next"] or datetime.max.replace(tzinfo=UTC),
                                  tenants[s["tenant_id"]]["name"].lower()))
    digests = [_decorate(s) for s in g.db.execute(
        "SELECT * FROM email_schedules WHERE kind = 'digest' ORDER BY id")] if admin else []
    coming_up = [s for s in digests if s["upcoming"]]
    view_emails = [s for s in digests if s["views"]]
    scope = f"tenant_id IN ({marks})" + (" OR tenant_id IS NULL" if admin else "")
    deliveries = g.db.execute(f"SELECT * FROM email_deliveries WHERE {scope} ORDER BY id DESC LIMIT 30",
                              ids).fetchall()
    since = _days_ago(30)
    stats = g.db.execute(f"SELECT SUM(status = 'sent') AS sent, SUM(status = 'failed') AS failed"
                         f" FROM email_deliveries WHERE ({scope}) AND created_at >= ?", (*ids, since)).fetchone()
    every = schedules + digests
    failing = [s for s in every if s["last_status"] == "failed"]
    upcoming = sorted((s for s in every if s["next"] and s["recipients"]), key=lambda s: s["next"])
    global_alert = _decorate_alert(g.db.execute("SELECT * FROM email_alerts WHERE tenant_id IS NULL").fetchone()) \
        if admin else None
    tenant_alerts = g.db.execute(f"SELECT COUNT(*) FROM email_alerts WHERE enabled = 1 AND tenant_id IN ({marks})",
                                 ids).fetchone()[0]
    return render_template(
        "emails.html", schedules=schedules, digests=[s for s in digests if not s["upcoming"] and not s["views"]],
        coming_up=coming_up, view_emails=view_emails,
        deliveries=deliveries, tenants=tenants,
        failing=failing, last=_last_errors([s["id"] for s in failing]), upcoming=upcoming[0] if upcoming else None,
        sent=stats["sent"] or 0, failed=stats["failed"] or 0, team=delivery.team_contacts(g.db),
        global_alert=global_alert, tenant_alerts=tenant_alerts, admin=admin,
        mail_ready=mailer.is_configured(_settings()), coverage=month_end.coverage(g.db, tenants.values()))


# -- Settings ---------------------------------------------------------------------------------


def _store(values: dict) -> None:
    for key, value in values.items():
        g.db.execute("INSERT INTO settings (key, value) VALUES (?, ?)"
                     " ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))


@bp.route("/admin/email", methods=["GET", "POST"])
@roles_required("admin")
def settings():
    vault = _vault()
    current = _settings()
    if request.method == "POST":
        if request.form.get("action") == "test":
            to = mailer.parse_recipients(request.form.get("test_to", ""))
            if to is None:
                flash("Enter a valid address to send the test to.", "error")
                return redirect(url_for("email.settings"))
            transport = mailer.TRANSPORTS[mailer.transport(current)]
            look, inline = delivery.email_brand(g.db)
            html = delivery.render(current_app, "emails/test.html", subject="Office Sentry test email",
                                   transport=transport, **look)
            email = Email(to=to, subject="Office Sentry test email", html=html, inline=inline,
                          text=f"Email from Office Sentry is working. Sent through {transport}.")
            try:
                used = mailer.send(g.db, vault, email)
                error = None
                flash(f"Test email sent to {', '.join(to)}.", "success")
            except MailError as exc:
                used, error = None, str(exc)
                flash(f"Test failed: {exc}", "error")
            delivery.record(g.db, kind="test", email=email, subject=email.subject, error=error, transport=used,
                             trigger="manual", user=g.user["username"])
            return redirect(url_for("email.settings"))

        if request.form.get("action") == "mail_certificate":
            return _make_mail_certificate(vault, current)

        transport = request.form.get("mail_transport", "smtp")
        if transport not in mailer.TRANSPORTS:
            abort(400)
        values = {"mail_transport": transport, "msp_name": request.form.get("msp_name", "").strip()[:80]}
        if transport == "smtp":
            host = request.form.get("smtp_host", "").strip()
            port = request.form.get("smtp_port", type=int)
            security = request.form.get("smtp_security", "starttls")
            sender = request.form.get("smtp_from", "").strip()
            if security not in ("starttls", "ssl", "none") or port is None or not 1 <= port <= 65535:
                abort(400)
            if not host or not mailer.valid_email(sender.split("<")[-1].rstrip(">").strip()):
                flash("Enter the SMTP server and a valid From address.", "error")
                return redirect(url_for("email.settings"))
            values |= {"smtp_host": host, "smtp_port": str(port), "smtp_security": security,
                       "smtp_user": request.form.get("smtp_user", "").strip(), "smtp_from": sender}
            if security == "none" and values["smtp_user"] and not mailer.is_loopback(host):
                flash("With no encryption the password would be sent unencrypted, so Office Sentry won't sign in "
                      "that way. Choose STARTTLS or SSL/TLS, or leave the username empty for an internal relay "
                      "that doesn't need a sign-in.", "error")
                return redirect(url_for("email.settings"))
            password = request.form.get("smtp_password", "")
            # The stored password only goes to the server, port and security it was entered for.
            same_server = (current.get("smtp_host"), current.get("smtp_port"), current.get("smtp_security")) == (
                host, str(port), security)
            if password:
                values["smtp_password_enc"] = vault.encrypt(password)
            elif current.get("smtp_password_enc") and not same_server:
                values["smtp_password_enc"] = ""
                flash("The server, port or security changed, so enter the password again.", "warning")
        else:
            tenant = request.form.get("graph_tenant", "").strip().lower()
            client_id = request.form.get("graph_client_id", "").strip().lower()
            sender = request.form.get("graph_sender", "").strip()
            secret = request.form.get("graph_secret", "")
            tenant_ok = mailer.GUID_RE.match(tenant) or mailer.valid_email(f"x@{tenant}")
            if not tenant_ok or not mailer.GUID_RE.match(client_id) or not mailer.valid_email(sender):
                flash("Enter your tenant ID (or domain), the mail app's client ID and the mailbox to send from.",
                      "error")
                return redirect(url_for("email.settings"))
            auth = request.form.get("graph_auth") or mailer.graph_auth(current)
            cloud = request.form.get("graph_cloud", clouds.DEFAULT)
            if auth not in mailer.GRAPH_AUTH or not clouds.valid(cloud):
                abort(400)
            same_app = (current.get("graph_tenant"), current.get("graph_client_id"), mailer.graph_cloud(current).key
                        ) == (tenant, client_id, cloud)
            values |= {"graph_tenant": tenant, "graph_client_id": client_id, "graph_sender": sender,
                       "graph_auth": auth, "graph_cloud": cloud}
            if auth == "secret":
                # A secret only ever goes to the app it was entered for.
                if secret:
                    values["graph_secret_enc"] = vault.encrypt(secret)
                elif not same_app:
                    values["graph_secret_enc"] = ""
                if not (secret or values.get("graph_secret_enc", current.get("graph_secret_enc"))):
                    flash("Saved, but add the client secret before email can be sent.", "warning")
                if current.get("graph_key_enc"):  # nor a certificate key
                    values |= {"graph_cert_pem": "", "graph_key_enc": "", "graph_thumbprint": "",
                               "graph_cert_not_after": "", "graph_cert_warned": ""}
                    flash("The mail app's certificate was deleted from Office Sentry. Delete it from the mail app in "
                          "Entra too (Certificates & secrets → Certificates).", "warning")
            else:
                if current.get("graph_secret_enc"):  # a secret nothing uses shouldn't be kept
                    values["graph_secret_enc"] = ""
                    flash("The client secret was deleted from Office Sentry. Delete it from the mail app in Entra "
                          "too (Certificates & secrets → Client secrets).", "warning")
                if not current.get("graph_key_enc"):
                    flash("Saved. Next: make the mail app's certificate and upload it to the app.", "warning")
            # Limiting the app to one mailbox was confirmed for that app and that mailbox only.
            ticked, was_scoped = request.form.get("graph_scoped") == "1", mailer.scoped(current)
            if was_scoped and not (same_app and current.get("graph_sender") == sender):
                values |= {"graph_scoped_at": "", "graph_scoped_by": ""}
                flash("The mail app or mailbox changed, so limit the app to the new mailbox and tick that step "
                      "again.", "warning")
            elif ticked and not was_scoped:
                values |= {"graph_scoped_at": utcnow(), "graph_scoped_by": g.user["username"]}
            elif not ticked:
                values |= {"graph_scoped_at": "", "graph_scoped_by": ""}
        with transaction(g.db):
            _store(values)
            audit(g.db, g.user["username"], "email_settings_updated", detail=transport)
        flash("Email settings saved. Send a test to check them.", "success")
        return redirect(url_for("email.settings"))
    from ..cert_lifecycle import mail_expiry

    return render_template("email_settings.html", s=current, transport=mailer.transport(current),
                           ready=mailer.is_configured(current), has_password=bool(current.get("smtp_password_enc")),
                           has_secret=bool(current.get("graph_secret_enc")), base_url=_cfg().base_url,
                           auth=mailer.graph_auth(current) if current.get("graph_client_id") else "certificate",
                           cloud=mailer.graph_cloud(current), scoped=mailer.scoped(current),
                           certificate=mail_expiry(current | {"mail_transport": "graph", "graph_auth": "certificate"}))


def _make_mail_certificate(vault, current: dict):
    """A certificate for the mail app to sign in with. Only its public part is ever downloaded."""
    cert = generate_certificate("Office Sentry mail")
    replacing = bool(current.get("graph_thumbprint"))
    with transaction(g.db):
        _store({"graph_cert_pem": cert.cert_pem, "graph_key_enc": vault.encrypt(cert.key_pem),
                "graph_thumbprint": cert.thumbprint, "graph_cert_not_after": cert.not_after.strftime(ISO),
                "graph_cert_warned": ""})
        audit(g.db, g.user["username"], "mail_certificate_created",
              detail=f"{cert.thumbprint}" + (f" replacing {current['graph_thumbprint']}" if replacing else ""))
    flash("Certificate made. Download the .cer and upload it to the mail app (Certificates & secrets → "
          "Certificates), then send a test." + (" Email fails until the new one is uploaded; delete the old one "
                                               "from the app afterwards." if replacing else ""), "success")
    return redirect(url_for("email.settings") + "#mail-certificate")


@bp.get("/admin/email/mail-certificate.cer")
@roles_required("admin")
def mail_certificate():
    """The mail app's public certificate. Its private key never leaves the database."""
    pem = _settings().get("graph_cert_pem")
    if not pem:
        abort(404)
    return Response(cert_der(pem), mimetype="application/pkix-cert",
                    headers={"Content-Disposition": "attachment; filename=office-sentry-mail.cer"})
