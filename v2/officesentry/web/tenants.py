"""Tenant list, tenant detail, onboarding consent, and run views."""

from __future__ import annotations

import hashlib
import re
import secrets
from datetime import UTC, datetime, timedelta

from flask import Blueprint, abort, current_app, flash, g, redirect, render_template, request, url_for
from itsdangerous import BadSignature, URLSafeTimedSerializer

from .. import clouds, endpoints, lifecycle, store, tenant_groups, tenant_health
from ..collectors import REGISTRY
from ..db import audit, transaction, utcnow
from ..discovery import tenant_id_for_domain
from ..entra import admin_consent_url
from ..text import plural
from .access import login_required, roles_required, tenant_or_404, visible_tenants
from .groups import group_aware, scoped_tenants

bp = Blueprint("tenants", __name__)

GUID_RE = re.compile(r"^[0-9a-fA-F]{8}-([0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}$")
DOMAIN_RE = re.compile(r"^(?=.{4,253}$)([a-zA-Z0-9-]{1,63}\.)+[a-zA-Z]{2,63}$")


def _cfg():
    return current_app.extensions["officesentry.config"]


def _consent_serializer():
    return URLSafeTimedSerializer(current_app.config["SECRET_KEY"], salt="admin-consent")


def _latest_runs(tenant_id: int) -> dict:
    return tenant_health.latest_runs(g.db, tenant_id)


def not_connected(tenant) -> str | None:
    """Why collection can't run for this tenant yet, in plain words, or None when it can."""
    if tenant["archived_at"]:
        return "This tenant is archived. Restore it under Manage to collect again."
    if not tenant["enabled"]:
        return "Collection is paused for this tenant. Resume it under Manage."
    connection = store.get_connection(g.db, tenant["connection_id"]) if tenant["connection_id"] else None
    cloud = clouds.of(tenant)
    if not connection:
        if not cloud.is_default and not any(clouds.of(c).key == cloud.key for c in store.list_connections(g.db)):
            return clouds.app_needed(cloud)
        return "This tenant isn't connected yet: choose its app connection under Manage."
    if clouds.of(connection).key != cloud.key:
        return clouds.app_needed(cloud)
    if not connection["client_id"]:
        return ("This tenant isn't connected yet: its app connection has no application (client) ID. "
                "Add it under App connection first.")
    if not tenant["consented_at"]:
        return "This tenant isn't connected yet: a tenant admin needs to grant consent first."
    return None


def cloud_mismatch(cloud_key: str, connection) -> str | None:
    """Why ``connection`` can't serve a tenant in ``cloud_key``, or None when it can (or there's none)."""
    if connection is None or clouds.of(connection).key == cloud_key:
        return None
    tenant_cloud, app_cloud = clouds.get(cloud_key), clouds.of(connection)
    return (f"The app connection {connection['name']} is registered in {app_cloud.label}, so it can't sign in to a "
            f"{tenant_cloud.label} tenant. Choose an app connection set up for {tenant_cloud.label}, or add one "
            "under App connection.")


def data_health(tenant, latest: dict) -> tuple[str, str, str]:
    """(tone, label, detail) summarising a tenant's collection state for people."""
    from ..reports.monthly_review import STALE_AFTER

    if tenant["archived_at"]:
        return "info", "Archived", "Hidden from lists and reports until restored."
    if not tenant["enabled"]:
        return "info", "Paused", "Collection and emailed reports are paused."
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
@group_aware
def dashboard():
    from ..reports.monthly_review import rollup_row, setting

    if g.user["role"] == "client":
        from . import client_home

        return client_home.render(visible_tenants(g.db, g.user))
    rows = [rollup_row(g.db, t, cached=True) for t in scoped_tenants()]
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
        "scored": len(scores),
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
        cloud = request.form.get("cloud", clouds.DEFAULT)
        if not clouds.valid(cloud):
            abort(400)
        connection = store.get_connection(g.db, conn_id) if conn_id is not None else None
        if not name or len(name) > 200:
            flash("Enter a display name.", "error")
        elif not (GUID_RE.match(ref) or DOMAIN_RE.match(ref)):
            flash("Enter the tenant ID (GUID) or its domain, e.g. contoso.onmicrosoft.com.", "error")
        elif conn_id is not None and connection is None:
            abort(400)
        elif g.db.execute("SELECT 1 FROM tenants WHERE tenant_ref = ?", (ref,)).fetchone():
            flash("That tenant is already added.", "error")
        else:
            if cloud_mismatch(cloud, connection):
                # Add it anyway with this cloud's app if there's exactly one, else none: the tenant page then
                # says to set one up for its cloud.
                same = [c for c in connections if clouds.of(c).key == cloud]
                conn_id = same[0]["id"] if len(same) == 1 else None
                if conn_id is None:
                    flash(clouds.app_needed(clouds.get(cloud)), "warning")
            tenant_id = store.create_tenant(g.db, name, ref, conn_id, cloud)
            tenant_groups.set_tenant_groups(g.db, tenant_id,
                                            {int(x) for x in request.form.getlist("groups") if x.isdigit()})
            audit(g.db, g.user["username"], "tenant_created", tenant_id,
                  ref + ("" if cloud == clouds.DEFAULT else f" ({cloud})"))
            return redirect(url_for("tenants.tenant", tenant_id=tenant_id))
    return render_template("tenant_new.html", connections=connections, groups=tenant_groups.all_groups(g.db))


CONSENT_LINK_LIFETIME = timedelta(hours=1)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _nonce_hash(nonce: str) -> str:
    return hashlib.sha256(nonce.encode()).hexdigest()


def _consent_link(t, connection):
    """A Microsoft admin-consent link for this tenant, or None if this user can't use one.

    Its signed state carries a random nonce. The callback accepts that nonce once,
    within the hour, from this admin's signed-in browser (see ``consent_requests``).
    """
    if g.user["role"] != "admin" or not connection or not connection["client_id"] or g.get("session_row") is None:
        return None
    if cloud_mismatch(clouds.of(t).key, connection):
        return None  # that app can't be consented to in this tenant's cloud
    nonce = secrets.token_urlsafe(24)
    now = datetime.now(UTC)
    g.db.execute("DELETE FROM consent_requests WHERE expires_at < ?", (_iso(now),))
    g.db.execute(
        "INSERT INTO consent_requests (nonce_hash, tenant_id, user_id, session_hash, created_at, expires_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (_nonce_hash(nonce), t["id"], g.user["id"], g.session_row["token_hash"], _iso(now),
         _iso(now + CONSENT_LINK_LIFETIME)),
    )
    state = _consent_serializer().dumps({"t": t["id"], "n": nonce})
    return admin_consent_url(connection["client_id"], t["tenant_ref"],
                             _cfg().base_url + url_for("tenants.consent_callback"), state, clouds.of(t).key)


def _use_consent_nonce(tenant_id: int, nonce) -> bool:
    """Mark the link's nonce used. False if it isn't this browser's, has expired or was used before."""
    if not isinstance(nonce, str) or not nonce or g.get("session_row") is None:
        return False
    now = _iso(datetime.now(UTC))
    return g.db.execute(
        "UPDATE consent_requests SET used_at = ? WHERE nonce_hash = ? AND tenant_id = ? AND user_id = ?"
        " AND session_hash = ? AND used_at IS NULL AND expires_at > ?",
        (now, _nonce_hash(nonce), tenant_id, g.user["id"], g.session_row["token_hash"], now),
    ).rowcount == 1


@bp.get("/tenants/<int:tenant_id>")
@login_required
def tenant(tenant_id: int):
    from ..reports.data_reports import grouped
    from ..reports.monthly_review import build

    t = tenant_or_404(tenant_id)
    if g.user["role"] == "client":  # clients see reports, not collection internals
        return redirect(url_for("tenants.dashboard"))
    connection = store.get_connection(g.db, t["connection_id"]) if t["connection_id"] else None
    latest = _latest_runs(t["id"])
    report = build(g.db, t)
    sev_rank = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}
    attention = sorted(report.failing, key=lambda r: (sev_rank.get(r.severity, 5), -len(r.findings)))
    from .tenant_admin import _check_problem

    setup = tenant_health.assess(g.db, t)  # the onboarding checklist, until every step is done
    cloud = clouds.of(t)
    cloud_problem = None
    if not cloud.is_default and (connection is None or cloud_mismatch(cloud.key, connection)):
        cloud_problem = clouds.app_needed(cloud)
    return render_template(
        "tenant.html", tenant=t, connection=connection, consent_link=_consent_link(t, connection),
        redirect_uri=_cfg().base_url + url_for("tenants.consent_callback"), report=report, attention=attention,
        health=data_health(t, latest), report_groups=grouped(), setup=None if setup.onboarded else setup,
        setup_can_check=_check_problem(t) is None, cloud_problem=cloud_problem,
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
        "collection.html", tenant=t, connection=connection, capabilities=store.tenant_capabilities(t), runs=runs,
        latest=latest, collectors=REGISTRY, health=data_health(t, latest), not_connected=not_connected(t),
        setup=tenant_health.assess(g.db, t),
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
    flash(f"Queued {plural(len(keys), 'collection')}. The worker picks them up shortly.", "success")
    if request.form.get("back") == "overview":  # the onboarding checklist's "Collect everything now"
        return redirect(url_for("tenants.tenant", tenant_id=t["id"]))
    return redirect(url_for("tenants.collection", tenant_id=t["id"]))


@bp.post("/tenants/<int:tenant_id>/connection")
@roles_required("admin")
def set_connection(tenant_id: int):
    t = tenant_or_404(tenant_id)
    conn_id = request.form.get("connection_id", type=int)
    if conn_id is not None and store.get_connection(g.db, conn_id) is None:
        abort(400)
    if problem := cloud_mismatch(clouds.of(t).key, store.get_connection(g.db, conn_id) if conn_id else None):
        flash(problem, "error")
        return redirect(url_for("tenant_admin.manage", tenant_id=t["id"]))
    from .tenant_admin import _other_app

    reset = conn_id != t["connection_id"] and _other_app(t["connection_id"], conn_id)
    g.db.execute("UPDATE tenants SET connection_id = ?, consented_at = CASE WHEN ? THEN NULL ELSE consented_at END"
                 " WHERE id = ?", (conn_id, int(reset), t["id"]))
    audit(g.db, g.user["username"], "tenant_connection_set", t["id"], str(conn_id))
    flash("App connection saved. Use Check connection now to check it works.", "success")
    return redirect(url_for("tenant_admin.manage", tenant_id=t["id"]))


@bp.post("/tenants/<int:tenant_id>/test-connection")
@roles_required("admin", "analyst")
def test_connection(tenant_id: int):
    from ..connection_check import check

    t = tenant_or_404(tenant_id)
    connection = store.get_connection(g.db, t["connection_id"]) if t["connection_id"] else None
    if not connection or not connection["client_id"] or cloud_mismatch(clouds.of(t).key, connection):
        flash(not_connected(t), "error")
        return redirect(url_for("tenant_admin.manage", tenant_id=t["id"]))
    vault = current_app.extensions["officesentry.vault"]
    result = check(t["tenant_ref"], connection["client_id"], vault.decrypt(connection["key_enc"]),
                   connection["thumbprint"], clouds.of(t).key)
    audit(g.db, g.user["username"], "connection_tested", t["id"], "ok" if result.ok else result.title)
    if result.ok:
        g.db.execute("UPDATE tenants SET consented_at = COALESCE(consented_at, ?) WHERE id = ?", (utcnow(), t["id"]))
        queued = 0
        if not t["consented_at"] and t["enabled"] and not t["archived_at"]:
            # Consent was given outside the link: start the first collection now.
            queued = lifecycle.queue_first_collection(g.db, t["id"], requested_by=g.user["username"])
        flash(f"{result.title} Office Sentry can sign in and read this tenant."
              + (" The first collection has been queued." if queued else ""), "success")
    else:
        flash(f"{result.title} {result.fix}", "error")
    return redirect(url_for("tenant_admin.manage", tenant_id=t["id"]))


@bp.get("/consent/callback")
@roles_required("admin")
def consent_callback():
    try:
        data = _consent_serializer().loads(request.args.get("state", ""),
                                           max_age=int(CONSENT_LINK_LIFETIME.total_seconds()))
    except BadSignature:
        abort(400)
    if not isinstance(data, dict) or not isinstance(data.get("t"), int):
        abort(400)
    t = tenant_or_404(data["t"])
    if not _use_consent_nonce(t["id"], data.get("n")):
        audit(g.db, g.user["username"], "consent_link_refused", t["id"],
              "used before, expired or opened in another browser")
        flash("That consent link has already been used or has expired, or it was opened in a different browser. "
              "Nothing was recorded. Open a new consent link from this page.", "error")
        return redirect(url_for("tenants.tenant", tenant_id=t["id"]))
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
    swap_to, unconfirmed = None, False
    if GUID_RE.match(returned) and returned != t["tenant_ref"].lower():
        refused = None
        if GUID_RE.match(t["tenant_ref"]):  # never re-point a tenant that already has its ID
            refused = "Consent came from a different tenant than this one."
        elif (expected := tenant_id_for_domain(t["tenant_ref"].lower(), cloud=clouds.of(t).key)) is None:
            unconfirmed = True  # record the consent, but keep signing in by domain
        elif expected != returned:
            # Anyone holding the link could otherwise point this client at another client's tenant.
            refused = (f"Consent came from a different tenant than {t['tenant_ref']}, so it wasn't recorded. "
                       "Open the consent link again and sign in as an administrator of that tenant.")
        elif g.db.execute("SELECT 1 FROM tenants WHERE tenant_ref = ? AND id <> ?", (returned, t["id"])).fetchone():
            refused = "Another tenant in Office Sentry already uses this tenant ID, so consent wasn't recorded here."
        else:
            swap_to = returned
        if refused:
            audit(g.db, g.user["username"], "consent_refused", t["id"], f"returned {returned}")
            flash(refused, "error")
            return redirect(url_for("tenants.tenant", tenant_id=t["id"]))
    with transaction(g.db):
        if swap_to:  # replace the domain with the stable tenant GUID Microsoft confirmed for it
            g.db.execute("UPDATE tenants SET tenant_ref = ? WHERE id = ?", (swap_to, t["id"]))
        g.db.execute("UPDATE tenants SET consented_at = ? WHERE id = ?", (utcnow(), t["id"]))
        audit(g.db, g.user["username"], "consent_granted", t["id"],
              returned + (" (tenant ID not confirmed, kept the domain)" if unconfirmed else ""))
    if unconfirmed:
        flash(f"Office Sentry couldn't confirm the tenant ID for {t['tenant_ref']} with Microsoft just now, so it "
              "keeps using the domain, which works too. You can enter the tenant ID under Manage later.", "warning")
    if t["enabled"] and not t["archived_at"]:
        for key in REGISTRY:  # tenant_profile is first, so licence-dependent collectors see its result
            store.enqueue_run(g.db, t["id"], key, requested_by=g.user["username"])
        flash("Consent recorded. A first collection has been queued.", "success")
    else:
        flash("Consent recorded. Collection is paused for this tenant; resume it under Manage.", "success")
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
        "SELECT kind, SUM(items) AS n FROM objects WHERE run_id = ? GROUP BY kind ORDER BY kind", (run_id,)
    ).fetchall()
    metrics = g.db.execute("SELECT key, value FROM metrics WHERE run_id = ? ORDER BY key", (run_id,)).fetchall()
    kind = request.args.get("kind")
    sample = store.run_objects(g.db, run_id, kind=kind, limit=200) if kind else []
    columns = sorted({k for row in sample for k in row if not k.startswith("_")})[:12]
    pruned = not kinds and r["object_count"] > 0 and r["status"] not in ("queued", "running")
    requests = g.db.execute("SELECT * FROM run_requests WHERE run_id = ? ORDER BY calls DESC, endpoint",
                            (run_id,)).fetchall()
    return render_template("run.html", run=r, tenant=t, events=events, kinds=kinds, metrics=metrics,
                           kind=kind, sample=sample, columns=columns, collectors=REGISTRY, pruned=pruned,
                           requests=requests, limits=endpoints.LIMITS)
