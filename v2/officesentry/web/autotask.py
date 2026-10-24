"""Settings, Autotask PSA: the connection, the ticket defaults, each client's company and the tickets made."""

from __future__ import annotations

import re

from flask import Blueprint, abort, current_app, flash, g, redirect, render_template, request, url_for

from .. import alerts, autotask
from ..db import audit, transaction
from .access import needs, visible_tenants

bp = Blueprint("autotask", __name__, url_prefix="/admin")

MAX_DUE_HOURS = 24 * 30
_LISTED_ID = re.compile(r"\(#(\d+)\)\s*$")


def _vault():
    return current_app.extensions["officesentry.vault"]


def listed_name(name: str, company_id: int) -> str:
    """How a company is written in the client table and its list, so two companies with one name differ."""
    return f"{name} (#{company_id})"


@bp.get("/autotask")
@needs("settings.manage")
def settings():
    s = autotask.settings(g.db)
    tenants = visible_tenants(g.db, g.user)
    mapped = autotask.mappings(g.db)
    rows = []
    for t in autotask.recent(g.db):
        state = (
            "cleared"
            if t["resolved_at"]
            else "created"
            if t["ticket_id"]
            else "retrying"
            if t["retry_at"]
            else "failed"
        )
        rows.append({"row": t, "state": state, "link": autotask.ticket_link(s, t["ticket_id"])})
    return render_template(
        "autotask.html",
        s=s,
        choices=autotask.choices(s),
        events=autotask.EVENTS,
        chosen=autotask.chosen_events(s),
        severities=alerts.ALERT_SEVERITIES,
        priority_severities=autotask.PRIORITY_SEVERITIES,
        missing=autotask.missing(s),
        companies=autotask.company_list(g.db),
        tenants=tenants,
        mapped=mapped,
        suggested=autotask.suggestions(g.db, tenants),
        unmapped=sum(1 for t in tenants if t["id"] not in mapped),
        tickets=rows,
        listed_name=listed_name,
    )


def _choice(form_key: str, field: str, found: dict) -> str | None:
    """A ticket field's chosen value: one of Autotask's choices, or empty. None when it's neither."""
    value = request.form.get(form_key, "").strip()
    if not value:
        return ""
    return value if any(str(v) == value for v, _, _ in found.get(field, [])) else None


CONNECTION = (
    "autotask.username",
    "autotask.secret_enc",
    "autotask.code_enc",
    "autotask.zone_api",
    "autotask.zone_web",
    "autotask.checked_at",
    "autotask.choices",
)


@bp.post("/autotask")
@needs("settings.manage")
def save():
    vault = _vault()
    current = autotask.settings(g.db)
    found = autotask.choices(current)
    severity = request.form.get("min_severity", "")
    unmapped = request.form.get("unmapped", "")
    username = request.form.get("username", "").strip()
    if severity not in alerts.ALERT_SEVERITIES or unmapped not in ("own", "skip") or len(username) > 200:
        abort(400)
    values = {
        "autotask.events": ",".join(k for k in autotask.EVENTS if k in request.form.getlist("events")),
        "autotask.min_severity": severity,
        "autotask.unmapped": unmapped,
    }
    # The ticket fields are offered once the connection was checked.
    fields = [(setting, field) for field, setting in autotask.TICKET_FIELDS.items()]
    fields += [(f"priority_{p}", "priority") for p in autotask.PRIORITY_SEVERITIES]
    for key, field in fields if found else ():
        value = _choice(key, field, found)
        if value is None:
            abort(400)
        values[f"autotask.{key}"] = value
    due = request.form.get("due_hours", "").strip()
    if due and not (due.isdigit() and 1 <= int(due) <= MAX_DUE_HOURS):
        flash(f"Give the due date as 1 to {MAX_DUE_HOURS} hours, or leave it empty.", "error")
        return redirect(url_for("autotask.settings"))
    values["autotask.due_hours"] = due

    changed = []
    if request.form.get("disconnect") == "1":
        values |= dict.fromkeys(CONNECTION, "")
        changed.append("disconnected")
    else:
        if username != current["autotask.username"]:
            # Another user can be in another Autotask database: it's checked again before it's used.
            values |= {"autotask.username": username, "autotask.zone_api": "", "autotask.zone_web": ""}
            changed.append("user")
        for field, key in (("secret", "autotask.secret_enc"), ("code", "autotask.code_enc")):
            value = request.form.get(field, "").strip()
            if len(value) > 500:
                abort(400)
            if value:
                values[key] = vault.encrypt(value)
                changed.append(field)
    with transaction(g.db):
        autotask.save_settings(g.db, values | {"autotask.enabled": "0"} if changed else values)
    flash("Autotask settings saved.", "success")
    s = autotask.settings(g.db)
    if changed and "disconnected" not in changed and autotask.credentials_saved(s):
        _check()  # new credentials: find the zone and read the choices now
        s = autotask.settings(g.db)
    enabled = "1" if request.form.get("enabled") == "1" else "0"
    if enabled == "1" and autotask.missing(s):
        flash("Tickets stay off until you choose " + " and ".join(autotask.missing(s)) + ".", "error")
        enabled = "0"
    with transaction(g.db):
        autotask.save_settings(g.db, {"autotask.enabled": enabled})
        # Never the secret or the tracking identifier.
        audit(
            g.db,
            g.user["username"],
            "autotask_settings_updated",
            detail=f"enabled={enabled} events={values['autotask.events'] or 'none'} severity={severity} "
            f"unmapped={unmapped} changed={','.join(changed) or 'none'}",
        )
    return redirect(url_for("autotask.settings"))


def _check() -> bool:
    try:
        checked = autotask.check(g.db, _vault())
    except alerts.AlertError as exc:
        audit(g.db, g.user["username"], "autotask_check_failed", detail=str(exc)[:500])
        flash(f"Couldn't connect to Autotask: {exc}", "error")
        return False
    audit(
        g.db,
        g.user["username"],
        "autotask_checked",
        detail=f"companies={checked.companies} queues={checked.queues}",
    )
    flash(
        f"Connected to Autotask: {checked.companies} active compan{'y' if checked.companies == 1 else 'ies'} "
        f"and {checked.queues} queue{'' if checked.queues == 1 else 's'} read.",
        "success",
    )
    return True


@bp.post("/autotask/check")
@needs("settings.manage")
def check():
    _check()
    return redirect(url_for("autotask.settings"))


def _company(text: str, by_id: dict, by_name: dict) -> dict | None:
    """The company typed in a client's box: "Name (#id)" as the list offers, or a name only one company has."""
    if m := _LISTED_ID.search(text):
        return by_id.get(int(m.group(1)))
    matches = by_name.get(text.strip().lower(), [])
    return matches[0] if len(matches) == 1 else None


@bp.post("/autotask/companies")
@needs("settings.manage")
def companies():
    listed = [dict(r) for r in autotask.company_list(g.db)]
    by_id = {c["company_id"]: c for c in listed}
    by_name: dict[str, list] = {}
    for c in listed:
        by_name.setdefault(c["name"].lower(), []).append(c)
    mapped = autotask.mappings(g.db)
    changes, unknown = [], []
    with transaction(g.db):
        # Only the clients this person sees, whatever the form names.
        for tenant in visible_tenants(g.db, g.user):
            field = f"company-{tenant['id']}"
            if field not in request.form:
                continue
            text = request.form[field].strip()[:300]
            current = mapped.get(tenant["id"])
            if current and text == listed_name(current["company_name"], current["company_id"]):
                continue  # unchanged, even when the company isn't in the list any more
            company = _company(text, by_id, by_name) if text else None
            if text and company is None:
                unknown.append(tenant["name"])
                continue
            if autotask.set_company(g.db, tenant["id"], company, g.user["username"]):
                changes.append(f"{tenant['name']}: {company['name'] if company else 'none'}")
        if changes:
            audit(g.db, g.user["username"], "autotask_companies_mapped", detail="; ".join(changes)[:500])
    if unknown:
        flash(
            "No single Autotask company matches what was typed for "
            + ", ".join(unknown[:5])
            + (f" and {len(unknown) - 5} more" if len(unknown) > 5 else "")
            + ". Pick one from the list.",
            "error",
        )
    if changes or not unknown:
        flash(f"{len(changes)} client{'' if len(changes) == 1 else 's'} changed.", "success")
    return redirect(url_for("autotask.settings") + "#clients")


@bp.post("/autotask/test")
@needs("settings.manage")
def test():
    base_url = current_app.extensions["officesentry.config"].base_url
    try:
        number = autotask.test_ticket(g.db, _vault(), base_url, g.user["username"])
    except alerts.AlertError as exc:
        audit(g.db, g.user["username"], "autotask_test_failed", detail=str(exc)[:500])
        flash(f"No ticket was created: {exc}", "error")
    else:
        audit(g.db, g.user["username"], "autotask_test_created", detail=number)
        flash(f"Test ticket {number} created in your own company.", "success")
    return redirect(url_for("autotask.settings"))


@bp.post("/autotask/tickets/<int:ticket_id>/retry")
@needs("settings.manage")
def retry(ticket_id: int):
    row = g.db.execute("SELECT tenant_id FROM autotask_tickets WHERE id = ?", (ticket_id,)).fetchone()
    if row is None:
        abort(404)
    try:
        number = autotask.retry_one(g.db, _vault(), ticket_id)
    except alerts.AlertError as exc:
        audit(
            g.db, g.user["username"], "alert_failed", row["tenant_id"], f"ticket via autotask, sent again: {exc}"[:500]
        )
        flash(f"Still not created: {exc}", "error")
    else:
        audit(g.db, g.user["username"], "alert_sent", row["tenant_id"], f"ticket via autotask, sent again: {number}")
        flash(f"Ticket {number} created.", "success")
    return redirect(url_for("autotask.settings") + "#tickets")
