"""Onboarding many clients: import tenants from a CSV, send each client their consent pack and follow where
each one has got (``onboarding.py`` does the work). Admin only, like adding a tenant and consent links.

A link sent to a client (``client_link``) differs from the one an admin opens themselves
(``tenants._consent_link``): it never expires, is the same every time it's shown (so a printed pack and
a copied link match), and coming back through it records nothing on its own. Microsoft returns the
client's admin to ``/consent/callback``, which hands the visit to ``client_returned``: a public thank-you
page that queues a sign-in check (``onboarding.confirm``). Only that check sets ``consented_at``, so a
forged return can at most make the tracker say "Approved, confirming" until the check says otherwise.
"""

from __future__ import annotations

from datetime import UTC, datetime

from flask import Blueprint, Response, abort, current_app, flash, g, redirect, render_template, request, url_for
from itsdangerous import BadSignature, URLSafeSerializer

from .. import clouds, onboarding, permission_batch, store, tenant_health
from ..auditlog import addon
from ..db import audit, audit_counted
from ..entra import GRAPH_APP_ID, PERMISSIONS, admin_consent_url
from ..reports import tables
from . import public_demo
from .access import can, current, manages_groups, needs, tenant_or_404, visible_tenants
from .branding import current_brand
from .printing import pdf_response
from .tenants import _cfg, cloud_mismatch

bp = Blueprint("onboarding", __name__, url_prefix="/admin/onboarding")

MAX_UPLOAD = onboarding.MAX_BYTES
FILTERS = {"waiting": "Still to do", "all": "All clients"}
ON_CLIENT = {"sent", "approved"}  # the tracker's "Waiting on client" tile, as the filter ?state=client
GROUPED = {"client": ON_CLIENT, "new": onboarding.NEW_STATES}  # ?state= values that stand for several states


def _client_serializer() -> URLSafeSerializer:
    return URLSafeSerializer(current_app.config["SECRET_KEY"], salt="client-consent")


def client_link(t, connection) -> str | None:
    """The admin-consent link to send to this tenant's Global Administrator, or None while no app connection
    in its cloud can be consented to. The state names the tenant and when it was added, so a link for a
    deleted tenant never points at a later one that reuses its number."""
    if not connection or not connection["client_id"] or cloud_mismatch(clouds.of(t).key, connection):
        return None
    state = _client_serializer().dumps({"t": t["id"], "c": t["created_at"]})
    return admin_consent_url(
        connection["client_id"],
        t["tenant_ref"],
        _cfg().base_url + url_for("tenants.consent_callback"),
        state,
        clouds.of(t).key,
    )


def client_link_tenant(state: str):
    """The tenant a client link's state belongs to, or None when it isn't one (an admin's own link, say)."""
    try:
        data = _client_serializer().loads(state or "")
    except BadSignature:
        return None
    if not isinstance(data, dict) or not isinstance(data.get("t"), int) or not isinstance(data.get("c"), str):
        return None
    tenant = store.get_tenant(g.db, data["t"])
    return tenant if tenant is not None and tenant["created_at"] == data["c"] else None


def client_returned(t):
    """A client's administrator back from Microsoft's consent page (no Office Sentry sign-in). Never records
    consent itself and never shows Microsoft's text back: only a known explanation, or a generic one."""
    from ..connection_check import explain

    error = request.args.get("error")
    approved = not error and request.args.get("admin_consent", "").lower() == "true"
    audit_counted(
        g.db, "client_consent_returned" if approved else "client_consent_not_granted", (request.remote_addr or "")[:64]
    )
    if approved and not t["archived_at"] and not public_demo.active(g.db):
        onboarding.client_approved(g.db, t)
    if g.get("user") is not None and can("clients.manage"):
        # An admin opened the client's link themselves: back to the tenant, as their own link would.
        flash(
            "Approved. Office Sentry is signing in to confirm; this page updates once it has."
            if approved
            else "Consent was not granted.",
            "success" if approved else "error",
        )
        return redirect(url_for("onboarding.send", tenant_id=t["id"]))
    connection = store.get_connection(g.db, t["connection_id"]) if t["connection_id"] else None
    problem = explain(request.args.get("error_description", "")) if error else None
    return render_template(
        "consent_returned.html",
        approved=approved,
        problem=problem,
        app=onboarding.app_name(connection, tenant_health.connection_result(g.db, t["id"])),
        cloud=clouds.of(t),
    )


# -- The tracker -------------------------------------------------------------------------------


@bp.get("")
@needs("clients.manage")
def index():
    everything = onboarding.tracker(g.db, visible_tenants(g.db, g.user))
    counts = {key: sum(1 for e in everything if e.state.key == key) for key in onboarding.STATES}
    show = request.args.get("show", "waiting" if any(e.state.key != "connected" for e in everything) else "all")
    show = show if show in FILTERS else "all"
    state = request.args.get("state", "")
    state = state if state in onboarding.STATES or state in GROUPED else ""
    wanted = GROUPED.get(state, {state})
    entries = [
        e for e in everything if (show == "all" or e.state.key != "connected") and (not state or e.state.key in wanted)
    ]
    links = {e.tenant["id"]: client_link(e.tenant, e.connection) for e in entries if e.needs_link}
    counts["new"] = sum(counts[k] for k in onboarding.NEW_STATES)
    # App registrations that don't ask for the new permissions yet: approving now would grant only the old ones.
    asking = {
        e.connection["id"]: e.connection for e in everything if e.state.key in onboarding.NEW_STATES and e.connection
    }
    behind = [c for c in asking.values() if not permission_batch.app_ready(g.db, c)]
    # The audit log add-on's column, while the add-on is switched on: where each client has got with it.
    addon_rows = addon.rows_for(g.db, [e.tenant["id"] for e in entries]) if addon.enabled(g.db) else None
    return render_template(
        "onboarding.html",
        addon=None
        if addon_rows is None
        else {e.tenant["id"]: addon.state(addon_rows.get(e.tenant["id"])) for e in entries},
        entries=entries,
        counts=counts,
        total=len(everything),
        show=show,
        state=state,
        filters=FILTERS,
        states=onboarding.STATES,
        links=links,
        behind=behind,
        batch=permission_batch.CURRENT,
    )


@bp.post("/bulk")
@needs("clients.manage")
def bulk():
    action = request.form.get("action", "")
    picked = {int(x) for x in request.form.getlist("tenant") if x.isdigit()}
    tenants = [t for t in visible_tenants(g.db, g.user) if t["id"] in picked]
    if not tenants:
        flash("Tick at least one client first.", "error")
    elif action in ("sent", "unsent"):
        n = onboarding.mark_sent(g.db, [t["id"] for t in tenants], g.user["username"], sent=action == "sent")
        flash(
            f"Marked {n} {'client' if n == 1 else 'clients'} as {'sent' if action == 'sent' else 'not sent'}.",
            "success",
        )
    elif action == "check":
        waiting = [t for t in tenants if not t["consented_at"] and not t["archived_at"]]
        for t in waiting:
            onboarding.queue_check(g.db, t["id"], priority=store.PRIORITY_WAITING, user=g.user["username"])
        # A client asked for new permissions: re-read what it granted.
        rereading = [
            t
            for t in tenants
            if t["consented_at"]
            and t["enabled"]
            and not t["archived_at"]
            and permission_batch.waiting(tenant_health.connection_result(g.db, t["id"]))
        ]
        for t in rereading:
            store.enqueue_run(g.db, t["id"], "connection", requested_by=g.user["username"])
        if waiting or rereading:
            audit(
                g.db, g.user["username"], "consent_check_requested", detail=f"{len(waiting) + len(rereading)} tenants"
            )
        n = len(waiting) + len(rereading)
        flash(
            f"Checking {n} {'client' if n == 1 else 'clients'} by signing in to each. "
            "Reload the page in a minute to see the results."
            if n
            else "Every client you ticked has already approved everything.",
            "success",
        )
    else:
        abort(400)
    return redirect(url_for("onboarding.index", show=request.form.get("show") or None))


@bp.get("/export.csv")
@needs("clients.manage")
def export():
    """Every client with something to approve (the app, or new permissions), with its contact and consent link,
    for a mail merge."""
    rows = []
    for e in onboarding.tracker(g.db, visible_tenants(g.db, g.user)):
        if not e.needs_link:
            continue
        rows.append(
            [
                e.tenant["name"],
                e.tenant["tenant_ref"],
                e.row["contact_name"] if e.row else "",
                e.row["contact_email"] if e.row else "",
                e.state.label,
                client_link(e.tenant, e.connection) or "",
                e.app,
            ]
        )
    table = tables.Table(
        "Consent links",
        ["Client", "Tenant", "Contact name", "Contact email", "State", "Consent link", "App name"],
        rows,
    )
    audit(g.db, g.user["username"], "report_downloaded", detail="onboarding consent links (CSV)")
    return Response(
        tables.to_csv(table),
        mimetype="text/csv",
        headers={"Content-Disposition": 'attachment; filename="consent-links.csv"'},
    )


# -- Importing ---------------------------------------------------------------------------------


@bp.get("/template.csv")
@needs("clients.manage")
def template():
    return Response(
        "﻿" + onboarding.TEMPLATE,
        mimetype="text/csv",
        headers={"Content-Disposition": 'attachment; filename="tenants-to-import.csv"'},
    )


@bp.route("/import", methods=["GET", "POST"])
@needs("clients.manage")
def import_tenants():
    connections = store.list_connections(g.db)
    if request.method == "GET":
        return render_template("onboarding_import.html", parsed=None, text="", connections=connections)
    upload = request.files.get("file")
    if upload and upload.filename:
        data = upload.read(MAX_UPLOAD + 1)
        text = onboarding.decode(data) if len(data) <= MAX_UPLOAD else "x" * (MAX_UPLOAD + 1)
    else:
        text = request.form.get("text", "")
    sees = None if current().sees_all else {t["id"] for t in visible_tenants(g.db, g.user, archived=True)}
    parsed = onboarding.parse(g.db, text, sees=sees, groups_allowed=manages_groups())
    if request.form.get("confirm") == "1" and not parsed.error and parsed.new:
        ids = onboarding.create(g.db, parsed, g.user["username"])
        flash(
            f"Added {len(ids)} {'client' if len(ids) == 1 else 'clients'}. Next: send each client their consent link.",
            "success",
        )
        return redirect(url_for("onboarding.index", show="waiting"))
    clouds_used = sorted({r.cloud for r in parsed.new})
    uses = {c: onboarding.connection_for(connections, c) for c in clouds_used}
    from .. import tenant_groups

    group_ids = {gr["name"].lower(): gr["id"] for gr in tenant_groups.all_groups(g.db)}
    granted = {}  # group name -> analysts who will see its imported tenants
    for name in {grp for r in parsed.new for grp in r.groups}:
        gid = group_ids.get(name.lower())
        if gid is not None:
            granted[name] = [
                u["username"]
                for u in g.db.execute(
                    "SELECT u.username FROM users u JOIN user_tenant_groups ug ON ug.user_id = u.id"
                    " WHERE ug.group_id = ? AND u.role = 'analyst' ORDER BY u.username COLLATE NOCASE",
                    (gid,),
                )
            ]
    return render_template(
        "onboarding_import.html",
        parsed=parsed,
        text=text if not parsed.error else "",
        connections=connections,
        uses=uses,
        cloud_of=clouds.get,
        granted=granted,
    )


# -- One client: the link, the email and the pack ----------------------------------------------


def _send_context(t):
    connection = store.get_connection(g.db, t["connection_id"]) if t["connection_id"] else None
    result = tenant_health.connection_result(g.db, t["id"])
    row = onboarding.row_for(g.db, t["id"])
    request_row = permission_batch.request_for(g.db, t["id"])
    waiting = permission_batch.waiting(result) if t["consented_at"] else []
    return {
        "tenant": t,
        "connection": connection,
        "row": row,
        "ask": request_row,
        "state": onboarding.state(t, row, result, request_row),
        "link": client_link(t, connection),
        "app": onboarding.app_name(connection, result),
        "cloud": clouds.of(t),
        "today": datetime.now(UTC),
        "batch": permission_batch.CURRENT,
        "new": [i for i in permission_batch.items(onboarding.CLIENT_WORDS) if i.permission in waiting],
    }


@bp.get("/<int:tenant_id>")
@needs("clients.manage")
def send(tenant_id: int):
    """One client's page: sending the consent link and pack, or, for a client that already consented and has new
    permissions to approve, What's new and why."""
    t = tenant_or_404(tenant_id)
    ctx = _send_context(t)
    whats_new = ctx["state"].key in onboarding.NEW_STATES
    email = render_template(
        "emails/permissions_update.txt" if whats_new else "emails/consent_request.txt", brand=current_brand(), **ctx
    )
    subject, _, body = email.partition("\n")
    extra = {}
    if whats_new:
        extra["app_ready"] = permission_batch.app_ready(g.db, ctx["connection"])
    return render_template(
        "onboarding_whats_new.html" if whats_new else "onboarding_send.html",
        subject=subject.removeprefix("Subject: ").strip(),
        body=body.strip(),
        **ctx,
        **extra,
    )


@bp.post("/<int:tenant_id>/contact")
@needs("clients.manage")
def contact(tenant_id: int):
    from ..mailer import valid_email

    t = tenant_or_404(tenant_id)
    name = " ".join(request.form.get("contact_name", "").split())
    email = request.form.get("contact_email", "").strip()
    if email and not valid_email(email):
        flash("That isn't an email address.", "error")
    else:
        onboarding.save_contact(g.db, t["id"], name, email)
        audit(g.db, g.user["username"], "onboarding_contact_saved", t["id"])
        flash("Contact saved.", "success")
    return redirect(url_for("onboarding.send", tenant_id=t["id"]))


@bp.post("/<int:tenant_id>/sent")
@needs("clients.manage")
def mark_sent(tenant_id: int):
    t = tenant_or_404(tenant_id)
    sent = request.form.get("sent", "1") == "1"
    onboarding.mark_sent(g.db, [t["id"]], g.user["username"], sent=sent)
    if not sent:
        flash("Marked as not sent.", "success")
    elif t["consented_at"]:
        flash("Marked as sent. Office Sentry re-reads the client's permissions as soon as they come back.", "success")
    else:
        flash("Marked as sent. The tracker checks for consent every hour from now on.", "success")
    return redirect(url_for("onboarding.send", tenant_id=t["id"]))


@bp.post("/<int:tenant_id>/check")
@needs("clients.manage")
def check(tenant_id: int):
    t = tenant_or_404(tenant_id)
    if t["consented_at"] and permission_batch.waiting(tenant_health.connection_result(g.db, t["id"])):
        if t["enabled"] and not t["archived_at"]:
            store.enqueue_run(g.db, t["id"], "connection", requested_by=g.user["username"])
            audit(g.db, g.user["username"], "connection_check_requested", t["id"])
            flash(
                "Checking which permissions the client has approved. Reload in a minute to see the result.", "success"
            )
        else:
            flash("This client is paused or archived, so Office Sentry doesn't sign in to it.", "error")
    elif t["consented_at"]:
        flash("Consent is already confirmed for this client.", "success")
    else:
        onboarding.queue_check(g.db, t["id"], priority=store.PRIORITY_WAITING, user=g.user["username"])
        audit(g.db, g.user["username"], "consent_check_requested", t["id"])
        flash("Checking: Office Sentry is signing in to the tenant. Reload in a minute to see the result.", "success")
    return redirect(url_for("onboarding.send", tenant_id=t["id"]))


@bp.get("/<int:tenant_id>/pack.pdf")
@needs("clients.manage")
def pack(tenant_id: int):
    """The "send this to your client" pack: how to approve, and what Office Sentry reads and never reads."""
    t = tenant_or_404(tenant_id)
    ctx = _send_context(t)
    audit(g.db, g.user["username"], "report_downloaded", t["id"], "consent pack.pdf")
    return pdf_response(
        "reports/consent_pack.html",
        f"approve-office-sentry-{tables.slug(t['name'])}",
        permissions=_permission_rows(),
        never=onboarding.NEVER_READS,
        **ctx,
    )


def _permission_rows() -> list[tuple[str, str, str]]:
    """(Microsoft's name, the API when it isn't Microsoft Graph, what it reads) for every permission."""
    words = onboarding.CLIENT_WORDS
    return [
        (name, "" if resource.app_id == GRAPH_APP_ID else resource.name, words[p])
        for p, (resource, name, _) in PERMISSIONS.items()
    ]


@bp.get("/<int:tenant_id>/whats-new.pdf")
@needs("clients.manage")
def whats_new_pack(tenant_id: int):
    """What's new and why, for a client asked to approve a new batch of permissions: what each one reads and
    what it adds, and how to approve. Lists the ones the client hasn't approved, or the whole batch."""
    t = tenant_or_404(tenant_id)
    ctx = _send_context(t)
    if not ctx["new"]:
        ctx["new"] = permission_batch.items(onboarding.CLIENT_WORDS)
    audit(g.db, g.user["username"], "report_downloaded", t["id"], "whats new pack.pdf")
    return pdf_response(
        "reports/whats_new_pack.html",
        f"office-sentry-new-permissions-{tables.slug(t['name'])}",
        never=onboarding.NEVER_READS,
        **ctx,
    )
