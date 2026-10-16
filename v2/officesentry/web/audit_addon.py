"""The audit log add-on's pages (``auditlog/addon.py`` does the work).

* ``/admin/audit-addon`` (settings): the switch, creating the add-on's app with the same device sign-in as the
  App connection page (``app_setup``, purpose "audit"), or by hand, and where each client has got. The app's
  certificate and client ID are kept on the App connection page with the shared app's.
* ``/tenants/<id>/audit-addon`` (the client's Settings tab): sending the client its add-on link, email text and
  consent pack, checking, switching the add-on on and off.

A client's add-on link (``client_link``) works like its main consent link (``web/onboarding.client_link``):
it never expires, comes back to ``/consent/callback`` without a sign-in, and the public thank-you page
(``client_returned``) records nothing but "they say they approved"; only signing in as the add-on app
(``addon.confirm``) confirms it. Its state is signed with its own salt, so the two links can't stand in for
each other. Every page about one client is only there while the add-on is switched on.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from flask import Blueprint, abort, current_app, flash, g, jsonify, redirect, render_template, request, url_for
from itsdangerous import BadSignature, URLSafeSerializer

from .. import app_setup, clouds, entra, events, store, tasks
from ..auditlog import addon, feed
from ..certs import generate_certificate
from ..db import audit, audit_counted
from ..entra import admin_consent_url
from ..reports import tables
from . import public_demo
from .access import can, needs, tenant_or_404, visible_tenants
from .branding import current_brand
from .printing import pdf_response
from .tenants import _cfg

bp = Blueprint("audit_addon", __name__)

PURPOSE = "audit"


def _serializer() -> URLSafeSerializer:
    return URLSafeSerializer(current_app.config["SECRET_KEY"], salt="client-consent-audit")


def _redirect_uri() -> str:
    return _cfg().base_url + url_for("tenants.consent_callback")


def client_link(t, connection) -> str | None:
    """The link that asks this client's Global Administrator to approve the add-on app, or None while there's
    no add-on app with a client ID in the client's cloud."""
    if connection is None or not connection["client_id"] or clouds.of(connection).key != clouds.of(t).key:
        return None
    state = _serializer().dumps({"t": t["id"], "c": t["created_at"]})
    return admin_consent_url(connection["client_id"], t["tenant_ref"], _redirect_uri(), state, clouds.of(t).key)


def client_link_tenant(state: str):
    """The tenant an add-on link's state names, or None when it isn't one."""
    try:
        data = _serializer().loads(state or "")
    except BadSignature:
        return None
    if not isinstance(data, dict) or not isinstance(data.get("t"), int) or not isinstance(data.get("c"), str):
        return None
    tenant = store.get_tenant(g.db, data["t"])
    return tenant if tenant is not None and tenant["created_at"] == data["c"] else None


def client_returned(t):
    """A client's administrator back from Microsoft's consent page for the add-on. Records only that they say
    they approved (and queues the sign-in that confirms it), and never shows Microsoft's text."""
    from ..connection_check import explain

    error = request.args.get("error")
    approved = not error and request.args.get("admin_consent", "").lower() == "true"
    audit_counted(
        g.db, "audit_consent_returned" if approved else "audit_consent_not_granted", (request.remote_addr or "")[:64]
    )
    if approved and addon.enabled(g.db) and not t["archived_at"] and not public_demo.active(g.db):
        addon.client_approved(g.db, t)
    if g.get("user") is not None and can("clients.manage"):
        flash(
            "Approved. Office Sentry is signing in as the add-on to confirm; this page updates once it has."
            if approved
            else "The add-on wasn't approved.",
            "success" if approved else "error",
        )
        if not addon.enabled(g.db):
            return redirect(url_for("tenants.tenant", tenant_id=t["id"]))
        return redirect(url_for("audit_addon.client", tenant_id=t["id"]))
    return render_template(
        "consent_returned.html",
        approved=approved,
        problem=explain(request.args.get("error_description", "")) if error else None,
        app=addon.AUDIT_NAME,
        cloud=clouds.of(t),
        addon=True,
    )


# -- The add-on's own page ---------------------------------------------------------------------------------


@bp.get("/admin/audit-addon")
@needs("settings.manage", kind="staff")
def settings():
    sign_in = app_setup.current(g.db, g.user["id"], PURPOSE)
    if sign_in and sign_in["status"] == "done":
        app_setup.cancel(g.db, g.user["id"], PURPOSE)  # created: say so once
        flash(
            f"{sign_in['name']} created and the add-on switched on. Next: ask a client to approve it, from the "
            "client's Settings tab, Audit log add-on.",
            "success",
        )
        sign_in = None
    connections = addon.connections(g.db)
    rows = g.db.execute("SELECT * FROM audit_feeds").fetchall()
    # Archived clients too: one whose feed is still on keeps the add-on from being switched off.
    tenants = {t["id"]: t for t in visible_tenants(g.db, g.user, archived=True)}
    clients = sorted(
        ((tenants[r["tenant_id"]], addon.state(r)) for r in rows if r["tenant_id"] in tenants),
        key=lambda pair: (list(addon.STATES).index(pair[1].key), pair[0]["name"].lower()),
    )
    counts = {key: sum(1 for _, s in clients if s.key == key) for key in addon.STATES}
    ready = {clouds.of(c).key for c in connections if c["client_id"]}
    return render_template(
        "audit_addon.html",
        on=addon.enabled(g.db),
        connections=connections,
        ready_clouds=ready,
        sign_in=sign_in,
        seconds_left=app_setup.seconds_left(sign_in) if sign_in else 0,
        clients=clients,
        counts=counts,
        states=addon.STATES,
        permission=entra.AUDIT_PERMISSIONS,
        default_name=addon.AUDIT_NAME,
        redirect_uri=_redirect_uri(),
        content_types=feed.CONTENT_TYPES,
        client_words=addon.CLIENT_WORDS,
        never=addon.NEVER_READS,
    )


def _to_settings(anchor: str = ""):
    return redirect(url_for("audit_addon.settings") + anchor)


@bp.post("/admin/audit-addon/switch")
@needs("settings.manage", kind="staff")
def switch():
    on = request.form.get("on") == "1"
    if on and not any(c["client_id"] for c in addon.connections(g.db)):
        flash("Create the add-on's app first: the add-on switches on when it's created.", "error")
        return _to_settings()
    try:
        addon.switch(g.db, on, g.user["username"])
    except addon.AddonError as exc:
        flash(str(exc), "error")
        return _to_settings()
    flash(
        "The audit log add-on is on." if on else "The audit log add-on is off and hidden everywhere.",
        "success",
    )
    return _to_settings()


@bp.post("/admin/audit-addon/setup")
@needs("settings.manage", kind="staff")
def start_setup():
    cloud = request.form.get("cloud", clouds.DEFAULT)
    if not clouds.valid(cloud):
        abort(400)
    tenant = app_setup.tenant_ref(request.form.get("tenant", ""))
    if tenant is None:
        flash("Enter your tenant's domain or Directory (tenant) ID, or leave it blank.", "error")
    elif why := clouds.missing(clouds.get(cloud), addon.COLLECTOR):
        flash(why, "error")
    elif addon.ready_connection(g.db, cloud):
        flash(f"The add-on already has its app for {clouds.get(cloud).label}.", "error")
    else:
        try:
            app_setup.start(
                g.db,
                current_app.extensions["officesentry.vault"],
                g.user["id"],
                g.user["username"],
                tenant,
                cloud,
                addon.AUDIT_NAME,
                purpose=PURPOSE,
            )
        except entra.SignInError as exc:
            flash(str(exc), "error")
    return _to_settings("#setup")


@bp.post("/admin/audit-addon/setup/check")
@needs("settings.manage", kind="staff")
def check_setup():
    """One step of the wizard: JSON for the page's script, a redirect without JavaScript."""
    row = app_setup.check(
        g.db,
        current_app.extensions["officesentry.vault"],
        g.user["id"],
        g.user["username"],
        _redirect_uri(),
        purpose=PURPOSE,
    )
    if request.accept_mimetypes.best == "application/json":
        if row is None:
            return jsonify(status="none")
        return jsonify(
            status=row["status"],
            note=row["error"] if row["status"] == "waiting" else None,
            wait=row["poll_seconds"],
            seconds_left=app_setup.seconds_left(row),
        )
    if row is not None and row["status"] == "waiting" and not row["error"]:
        flash(
            "Microsoft says sign-in hasn't finished yet. Enter the code on Microsoft's page, then check again.",
            "warning",
        )
    return _to_settings("#setup")


@bp.post("/admin/audit-addon/setup/cancel")
@needs("settings.manage", kind="staff")
def cancel_setup():
    app_setup.cancel(g.db, g.user["id"], PURPOSE)
    return _to_settings()


@bp.post("/admin/audit-addon/certificate")
@needs("settings.manage", kind="staff")
def create_by_hand():
    """A certificate for an add-on app created by hand in the Entra admin centre. Its client ID is saved on
    the App connection page, which switches the add-on on."""
    cloud = request.form.get("cloud", clouds.DEFAULT)
    if not clouds.valid(cloud):
        abort(400)
    if why := clouds.missing(clouds.get(cloud), addon.COLLECTOR):
        flash(why, "error")
        return _to_settings()
    found = addon.connection_for(g.db, cloud)
    if found is not None:
        return redirect(url_for("admin.connections") + f"#conn-{found['id']}")
    cert = generate_certificate(f"Office Sentry - {addon.AUDIT_NAME}"[:64])
    vault = current_app.extensions["officesentry.vault"]
    conn_id = store.create_connection(g.db, vault, addon.AUDIT_NAME, cert, cloud=cloud, kind=store.AUDIT)
    audit(g.db, g.user["username"], "connection_created", detail=f"{addon.AUDIT_NAME} {cert.thumbprint} {cloud}")
    flash(
        "Certificate generated. Upload the .cer to the add-on's app registration, then save its client ID.", "success"
    )
    return redirect(url_for("admin.connections") + f"#conn-{conn_id}")


# -- One client ---------------------------------------------------------------------------------------------


def _client(tenant_id: int):
    """The tenant, while the add-on is switched on (its pages aren't there otherwise)."""
    t = tenant_or_404(tenant_id)
    if not addon.enabled(g.db):
        abort(404)
    return t


def _context(t) -> dict:
    row = addon.row_for(g.db, t["id"])
    cloud = clouds.of(t)
    connection = addon.ready_connection(g.db, cloud.key)
    contact = g.db.execute("SELECT contact_name, contact_email FROM onboarding WHERE tenant_id = ?", (t["id"],))
    contact = contact.fetchone()
    last = store.latest_run(g.db, t["id"], addon.COLLECTOR, successful=False)
    try:
        said = json.loads(row["content_types"] or "{}") if row is not None else {}
    except ValueError:
        said = {}
    return {
        "feeds": [
            (feed.CONTENT_LABELS[c], said.get(c) if isinstance(said, dict) else None) for c in feed.CONTENT_TYPES
        ],
        "tenant": t,
        "row": row,
        "state": addon.state(row),
        "problem": addon.problem(g.db, t),
        "link": client_link(t, connection),
        "app": addon.AUDIT_NAME,
        "cloud": cloud,
        "contact": contact,
        "today": datetime.now(UTC),
        "words": addon.CLIENT_WORDS,
        "never": addon.NEVER_READS,
        "retention": _retention(),
        "last_run": last,
        "pending": tasks.pending(g.db, f"audit_stop:{t['id']}"),
    }


def _retention() -> str:
    from .. import retention

    months = retention.load(g.db, _cfg()).history_activity_months
    return (
        "for as long as you use the add-on"
        if months is None
        else f"for {retention.describe('history_activity_months', months)}"
    )


@bp.get("/tenants/<int:tenant_id>/audit-addon")
@needs("clients.manage", kind="staff")
def client(tenant_id: int):
    t = _client(tenant_id)
    ctx = _context(t)
    email = render_template("emails/audit_addon_request.txt", brand=current_brand(), **ctx)
    subject, _, body = email.partition("\n")
    return render_template(
        "audit_addon_client.html",
        subject=subject.removeprefix("Subject: ").strip(),
        body=body.strip(),
        stored=events.counts(g.db, t["id"])["activity"],
        **ctx,
    )


def _back(t):
    return redirect(url_for("audit_addon.client", tenant_id=t["id"]))


@bp.post("/tenants/<int:tenant_id>/audit-addon/sent")
@needs("clients.manage", kind="staff")
def mark_sent(tenant_id: int):
    t = _client(tenant_id)
    sent = request.form.get("sent", "1") == "1"
    if sent and (why := addon.problem(g.db, t)):
        flash(why, "error")
        return _back(t)
    addon.mark_sent(g.db, [t["id"]], g.user["username"], sent=sent)
    flash(
        "Marked as sent. Office Sentry checks every hour whether the client has approved, and switches the add-on "
        "on when they have."
        if sent
        else "Marked as not sent.",
        "success",
    )
    return _back(t)


@bp.post("/tenants/<int:tenant_id>/audit-addon/check")
@needs("clients.manage", kind="staff")
def check(tenant_id: int):
    t = _client(tenant_id)
    if addon.row_for(g.db, t["id"]) is None:
        flash("Send the client the add-on link first.", "error")
    elif not t["enabled"] or t["archived_at"]:
        flash("This client is paused or archived, so Office Sentry doesn't sign in to it.", "error")
    else:
        addon.queue_check(g.db, t["id"], priority=tasks.PRIORITY_WAITING, user=g.user["username"])
        audit(g.db, g.user["username"], "audit_check_requested", t["id"])
        flash("Checking: Office Sentry is signing in as the add-on. Reload in a minute to see the result.", "success")
    return _back(t)


@bp.post("/tenants/<int:tenant_id>/audit-addon/on")
@needs("clients.manage", kind="staff")
def switch_on(tenant_id: int):
    t = _client(tenant_id)
    try:
        addon.switch_on(g.db, t, g.user["username"])
    except addon.AddonError as exc:
        flash(str(exc), "error")
        return _back(t)
    flash(
        "Switching on: once Office Sentry has confirmed the client's approval, it starts the feed. Microsoft can "
        "take up to 12 hours to make the first records available.",
        "success",
    )
    return _back(t)


@bp.post("/tenants/<int:tenant_id>/audit-addon/off")
@needs("clients.manage", kind="staff")
def switch_off(tenant_id: int):
    t = _client(tenant_id)
    delete = request.form.get("delete") == "1"
    try:
        addon.switch_off(g.db, t, g.user["username"], delete=delete)
    except addon.AddonError as exc:
        flash(str(exc), "error")
        return _back(t)
    flash(
        "Switched off. Nothing more is collected; Office Sentry is stopping the feed at Microsoft"
        + (" and deleting what was collected." if delete else ". What was collected stays until retention removes it."),
        "success",
    )
    return _back(t)


@bp.get("/tenants/<int:tenant_id>/audit-addon/pack.pdf")
@needs("clients.manage", kind="staff")
def pack(tenant_id: int):
    """The add-on's consent pack: what it reads and never reads, how to approve it and how to remove it."""
    t = _client(tenant_id)
    audit(g.db, g.user["username"], "report_downloaded", t["id"], "audit log add-on pack.pdf")
    return pdf_response(
        "reports/audit_addon_pack.html",
        f"approve-audit-log-add-on-{tables.slug(t['name'])}",
        permission=entra.AUDIT_PERMISSIONS,
        **_context(t),
    )
