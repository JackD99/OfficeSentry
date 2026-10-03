"""Tenant list, tenant detail, onboarding consent, and run views."""

from __future__ import annotations

import re
from datetime import UTC, datetime

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


def not_connected(tenant) -> str | None:
    """Why collection can't run for this tenant yet, in plain words, or None when it can."""
    connection = store.get_connection(g.db, tenant["connection_id"]) if tenant["connection_id"] else None
    if not connection:
        return "This tenant isn't connected yet: choose its app connection under Connection."
    if not connection["client_id"]:
        return ("This tenant isn't connected yet: its app connection has no application (client) ID. "
                "Add it under App connection first.")
    if not tenant["consented_at"]:
        return "This tenant isn't connected yet: a tenant admin needs to grant consent first."
    return None


def data_health(tenant, latest: dict) -> tuple[str, str, str]:
    """(tone, label, detail) summarising a tenant's collection state for people."""
    from ..reports.monthly_review import STALE_AFTER

    if not tenant["consented_at"]:
        return "info", "Awaiting consent", "A tenant admin needs to grant access."
    if not latest:
        return "info", "Not collected", "No data collected yet."
    failed = [k for k, r in latest.items() if r["status"] == "failed"]
    sign_in = [latest[k]["error"] for k in failed if (latest[k]["error"] or "").startswith("Sign-in failed: ")]
    if sign_in and len(sign_in) == len(failed):
        return "bad", "Can't sign in", sign_in[0].removeprefix("Sign-in failed: ")
    if failed:
        names = ", ".join(REGISTRY[k].title for k in failed if k in REGISTRY)
        return "bad", f"{len(failed)} failed", names
    if any(r["status"] in ("queued", "running") for r in latest.values()):
        return "accent", "Collecting", "A collection is in progress."
    finished = [r["finished_at"] for r in latest.values() if r["finished_at"]]
    oldest = min(finished) if finished else None
    cutoff = (datetime.now(UTC) - STALE_AFTER).strftime("%Y-%m-%dT%H:%M:%SZ")
    if len(latest) < len(REGISTRY) or (oldest and oldest < cutoff):
        return "warn", "Out of date", "Some data is more than 36 hours old or missing."
    return "ok", "Up to date", ""


@bp.get("/")
@login_required
def dashboard():
    from ..reports.monthly_review import rollup_row, setting

    tenants = visible_tenants(g.db, g.user)
    rows = [rollup_row(g.db, t) for t in tenants]
    if g.user["role"] == "client":
        from ..reports.data_reports import REPORTS
        from ..reports.export import pdf_available

        return render_template("client_home.html", rows=rows, data_reports=REPORTS, pdf_available=pdf_available())
    for row in rows:
        row["health"] = data_health(row["tenant"], _latest_runs(row["tenant"]["id"]))
    rows.sort(key=lambda r: (-r["counts"]["critical"], -r["counts"]["high"], -r["counts"]["medium"],
                             r["tenant"]["name"].lower()))
    scores = [r["kpis"]["secure_score.pct"].value for r in rows if r["kpis"]["secure_score.pct"].value is not None]
    summary = {
        "up_to_date": sum(r["health"][0] == "ok" for r in rows),
        "urgent": sum(bool(r["counts"]["critical"] or r["counts"]["high"]) for r in rows),
        "severe": sum(r["counts"]["critical"] + r["counts"]["high"] for r in rows),
        "avg_score": sum(scores) / len(scores) if scores else None,
        "data_issues": sum(r["health"][0] in ("bad", "warn") for r in rows),
    }
    return render_template("dashboard.html", rows=rows, summary=summary, currency=setting(g.db, "currency", "GBP"))


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


def _consent_link(t, connection):
    if g.user["role"] != "admin" or not connection or not connection["client_id"]:
        return None
    state = _consent_serializer().dumps({"t": t["id"]})
    return admin_consent_url(connection["client_id"], t["tenant_ref"],
                             _cfg().base_url + url_for("tenants.consent_callback"), state)


@bp.get("/tenants/<int:tenant_id>")
@login_required
def tenant(tenant_id: int):
    from ..reports.data_reports import REPORTS
    from ..reports.monthly_review import build

    t = tenant_or_404(tenant_id)
    if g.user["role"] == "client":  # clients see reports, not collection internals
        return redirect(url_for("tenants.dashboard"))
    connection = store.get_connection(g.db, t["connection_id"]) if t["connection_id"] else None
    latest = _latest_runs(t["id"])
    report = build(g.db, t)
    sev_rank = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}
    attention = sorted(report.failing, key=lambda r: (sev_rank.get(r.severity, 5), -len(r.findings)))
    return render_template(
        "tenant.html", tenant=t, connection=connection, consent_link=_consent_link(t, connection),
        redirect_uri=_cfg().base_url + url_for("tenants.consent_callback"), report=report, attention=attention,
        health=data_health(t, latest), data_reports=REPORTS,
    )


@bp.get("/tenants/<int:tenant_id>/collection")
@roles_required("admin", "analyst")
def collection(tenant_id: int):
    t = tenant_or_404(tenant_id)
    connection = store.get_connection(g.db, t["connection_id"]) if t["connection_id"] else None
    runs = g.db.execute(
        "SELECT * FROM runs WHERE tenant_id = ? ORDER BY id DESC LIMIT 50", (t["id"],)
    ).fetchall()
    latest = _latest_runs(t["id"])
    return render_template(
        "collection.html", tenant=t, connection=connection, consent_link=_consent_link(t, connection),
        capabilities=store.tenant_capabilities(t), runs=runs, latest=latest,
        collectors=REGISTRY, health=data_health(t, latest), not_connected=not_connected(t),
        connections=store.list_connections(g.db),
    )


@bp.post("/tenants/<int:tenant_id>/collect")
@roles_required("admin", "analyst")
def collect(tenant_id: int):
    t = tenant_or_404(tenant_id)
    keys = request.form.getlist("collector") or list(REGISTRY)
    for key in keys:
        if key not in REGISTRY:
            abort(400)
    if reason := not_connected(t):
        flash(reason, "error")
        return redirect(url_for("tenants.collection", tenant_id=t["id"]))
    for key in keys:
        store.enqueue_run(g.db, t["id"], key, requested_by=g.user["username"])
    audit(g.db, g.user["username"], "collect_requested", t["id"], ",".join(keys))
    flash(f"Queued {len(keys)} collection(s). The worker picks them up shortly.", "success")
    return redirect(url_for("tenants.collection", tenant_id=t["id"]))


@bp.post("/tenants/<int:tenant_id>/connection")
@roles_required("admin")
def set_connection(tenant_id: int):
    t = tenant_or_404(tenant_id)
    conn_id = request.form.get("connection_id", type=int)
    if conn_id is not None and store.get_connection(g.db, conn_id) is None:
        abort(400)
    g.db.execute("UPDATE tenants SET connection_id = ? WHERE id = ?", (conn_id, t["id"]))
    audit(g.db, g.user["username"], "tenant_connection_set", t["id"], str(conn_id))
    flash("App connection saved. Use Test connection to check it works.", "success")
    return redirect(url_for("tenants.collection", tenant_id=t["id"]))


@bp.post("/tenants/<int:tenant_id>/test-connection")
@roles_required("admin", "analyst")
def test_connection(tenant_id: int):
    from ..connection_check import check

    t = tenant_or_404(tenant_id)
    connection = store.get_connection(g.db, t["connection_id"]) if t["connection_id"] else None
    if not connection or not connection["client_id"]:
        flash(not_connected(t), "error")
        return redirect(url_for("tenants.collection", tenant_id=t["id"]))
    vault = current_app.extensions["officesentry.vault"]
    result = check(t["tenant_ref"], connection["client_id"], vault.decrypt(connection["key_enc"]),
                   connection["thumbprint"])
    audit(g.db, g.user["username"], "connection_tested", t["id"], "ok" if result.ok else result.title)
    if result.ok:
        g.db.execute("UPDATE tenants SET consented_at = COALESCE(consented_at, ?) WHERE id = ?", (utcnow(), t["id"]))
        flash(f"{result.title} Office Sentry can sign in and read this tenant.", "success")
    else:
        flash(f"{result.title} {result.fix}", "error")
    return redirect(url_for("tenants.collection", tenant_id=t["id"]))


@bp.get("/consent/callback")
@roles_required("admin")
def consent_callback():
    try:
        data = _consent_serializer().loads(request.args.get("state", ""), max_age=3600)
    except BadSignature:
        abort(400)
    t = tenant_or_404(int(data["t"]))
    if request.args.get("error"):
        from ..connection_check import explain

        description = request.args.get("error_description", request.args["error"])
        problem = explain(description)
        flash(f"Consent was not granted. {problem}" if problem else "Consent was not granted: " + description[:300],
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
    for key in REGISTRY:  # tenant_profile is first, so licence-dependent collectors see its result
        store.enqueue_run(g.db, t["id"], key, requested_by=g.user["username"])
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
    pruned = not kinds and r["object_count"] > 0 and r["status"] not in ("queued", "running")
    return render_template("run.html", run=r, tenant=t, events=events, kinds=kinds, metrics=metrics,
                           kind=kind, sample=sample, columns=columns, collectors=REGISTRY, pruned=pruned)
