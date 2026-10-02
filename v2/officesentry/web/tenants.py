"""Tenant list, tenant detail, onboarding consent, and run views."""

from __future__ import annotations

import re

from flask import Blueprint, abort, current_app, flash, g, redirect, render_template, request, url_for
from itsdangerous import BadSignature, URLSafeTimedSerializer

from .. import store
from ..collectors import REGISTRY
from ..db import audit, transaction, utcnow
from ..entra import admin_consent_url
from .access import login_required, roles_required, tenant_or_404, visible_tenants

bp = Blueprint("tenants", __name__)

GUID_RE = re.compile(r"^[0-9a-fA-F]{8}-([0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}$")
DOMAIN_RE = re.compile(r"^(?=.{4,253}$)([a-zA-Z0-9-]{1,63}\.)+[a-zA-Z]{2,63}$")


def _cfg():
    return current_app.extensions["officesentry.config"]


def _consent_serializer():
    return URLSafeTimedSerializer(current_app.config["SECRET_KEY"], salt="admin-consent")


def _latest_runs(tenant_id: int) -> dict:
    rows = g.db.execute(
        "SELECT r.* FROM runs r JOIN (SELECT collector, MAX(id) AS id FROM runs WHERE tenant_id = ?"
        " GROUP BY collector) latest ON latest.id = r.id",
        (tenant_id,),
    ).fetchall()
    return {r["collector"]: r for r in rows}


@bp.get("/")
@login_required
def dashboard():
    tenants = visible_tenants(g.db, g.user)
    if g.user["role"] == "client":
        from ..reports.data_reports import REPORTS
        from ..reports.export import pdf_available
        from ..reports.monthly_review import rollup_row

        return render_template("client_home.html", rows=[rollup_row(g.db, t) for t in tenants],
                               data_reports=REPORTS, pdf_available=pdf_available())
    latest = {t["id"]: _latest_runs(t["id"]) for t in tenants}
    return render_template("dashboard.html", tenants=tenants, latest=latest, collectors=REGISTRY)


@bp.route("/tenants/new", methods=["GET", "POST"])
@roles_required("admin")
def new_tenant():
    connections = store.list_connections(g.db)
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        ref = request.form.get("tenant_ref", "").strip().lower()
        conn_id = request.form.get("connection_id", type=int)
        if not name or len(name) > 200:
            flash("Enter a display name.", "error")
        elif not (GUID_RE.match(ref) or DOMAIN_RE.match(ref)):
            flash("Enter the tenant ID (GUID) or its domain, e.g. contoso.onmicrosoft.com.", "error")
        elif conn_id is not None and store.get_connection(g.db, conn_id) is None:
            abort(400)
        elif g.db.execute("SELECT 1 FROM tenants WHERE tenant_ref = ?", (ref,)).fetchone():
            flash("That tenant is already added.", "error")
        else:
            tenant_id = store.create_tenant(g.db, name, ref, conn_id)
            audit(g.db, g.user["username"], "tenant_created", tenant_id, ref)
            return redirect(url_for("tenants.tenant", tenant_id=tenant_id))
    return render_template("tenant_new.html", connections=connections)


@bp.get("/tenants/<int:tenant_id>")
@login_required
def tenant(tenant_id: int):
    t = tenant_or_404(tenant_id)
    if g.user["role"] == "client":  # clients see reports, not collection internals
        return redirect(url_for("tenants.dashboard"))
    connection = store.get_connection(g.db, t["connection_id"]) if t["connection_id"] else None
    consent_link = None
    if g.user["role"] == "admin" and connection and connection["client_id"]:
        state = _consent_serializer().dumps({"t": t["id"]})
        consent_link = admin_consent_url(
            connection["client_id"], t["tenant_ref"],
            _cfg().base_url + url_for("tenants.consent_callback"), state,
        )
    runs = g.db.execute(
        "SELECT * FROM runs WHERE tenant_id = ? ORDER BY id DESC LIMIT 50", (t["id"],)
    ).fetchall()
    return render_template(
        "tenant.html", tenant=t, connection=connection, consent_link=consent_link,
        capabilities=store.tenant_capabilities(t), runs=runs, latest=_latest_runs(t["id"]),
        collectors=REGISTRY,
    )


@bp.post("/tenants/<int:tenant_id>/collect")
@roles_required("admin", "analyst")
def collect(tenant_id: int):
    t = tenant_or_404(tenant_id)
    keys = request.form.getlist("collector") or list(REGISTRY)
    for key in keys:
        if key not in REGISTRY:
            abort(400)
    for key in keys:
        store.enqueue_run(g.db, t["id"], key, requested_by=g.user["username"])
    audit(g.db, g.user["username"], "collect_requested", t["id"], ",".join(keys))
    flash(f"Queued {len(keys)} collection(s). The worker picks them up shortly.", "success")
    return redirect(url_for("tenants.tenant", tenant_id=t["id"]))


@bp.get("/consent/callback")
@roles_required("admin")
def consent_callback():
    try:
        data = _consent_serializer().loads(request.args.get("state", ""), max_age=3600)
    except BadSignature:
        abort(400)
    t = tenant_or_404(int(data["t"]))
    if request.args.get("error"):
        flash("Consent was not granted: " + request.args.get("error_description", request.args["error"])[:300],
              "error")
        return redirect(url_for("tenants.tenant", tenant_id=t["id"]))
    if request.args.get("admin_consent", "").lower() != "true":
        abort(400)

    returned = request.args.get("tenant", "").lower()
    with transaction(g.db):
        if GUID_RE.match(returned) and returned != t["tenant_ref"]:
            if GUID_RE.match(t["tenant_ref"]):
                flash("Consent came from a different tenant than this one.", "error")
                return redirect(url_for("tenants.tenant", tenant_id=t["id"]))
            # Replace the domain with the stable tenant GUID.
            g.db.execute("UPDATE tenants SET tenant_ref = ? WHERE id = ?", (returned, t["id"]))
        g.db.execute("UPDATE tenants SET consented_at = ? WHERE id = ?", (utcnow(), t["id"]))
        audit(g.db, g.user["username"], "consent_granted", t["id"], returned)
    store.enqueue_run(g.db, t["id"], "tenant_profile", requested_by=g.user["username"])
    flash("Consent recorded. A first collection has been queued.", "success")
    return redirect(url_for("tenants.tenant", tenant_id=t["id"]))


@bp.get("/runs/<int:run_id>")
@roles_required("admin", "analyst")
def run(run_id: int):
    r = g.db.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    if r is None:
        abort(404)
    t = tenant_or_404(r["tenant_id"])
    events = g.db.execute("SELECT * FROM run_events WHERE run_id = ? ORDER BY id", (run_id,)).fetchall()
    kinds = g.db.execute(
        "SELECT kind, COUNT(*) AS n FROM objects WHERE run_id = ? GROUP BY kind ORDER BY kind", (run_id,)
    ).fetchall()
    metrics = g.db.execute("SELECT key, value FROM metrics WHERE run_id = ? ORDER BY key", (run_id,)).fetchall()
    kind = request.args.get("kind")
    sample = store.run_objects(g.db, run_id, kind=kind, limit=200) if kind else []
    columns = sorted({k for row in sample for k in row if not k.startswith("_")})[:12]
    return render_template("run.html", run=r, tenant=t, events=events, kinds=kinds, metrics=metrics,
                           kind=kind, sample=sample, columns=columns, collectors=REGISTRY)
